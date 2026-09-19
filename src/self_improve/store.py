"""SQLite state store, designed Postgres/Supabase-portable.

Portability rules (enforced by convention here; do not violate in queries):
- TEXT UUID primary keys (uuid4 hex), never AUTOINCREMENT ints.
- Timestamps are ISO-8601 UTC TEXT (e.g. "2026-08-14T09:30:00Z").
- JSON columns are TEXT holding strict JSON (maps to jsonb on Postgres).
- Booleans stored as INTEGER 0/1.
- No SQLite-only SQL (no INSERT OR REPLACE in favor of explicit upserts via
  ON CONFLICT, which Postgres also supports; no AUTOINCREMENT; no ATTACH).
- schema_migrations table records applied migrations; the same files replay
  on Postgres.

All DB access in the codebase goes through this module.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing, contextmanager
import uuid
from datetime import datetime, timezone
from pathlib import Path


# How long a writer waits for a competing writer's transaction before raising
# "database is locked". Long enough to ride out a nightly run's per-file
# commit, short enough that a genuinely wedged DB still fails loudly.
BUSY_TIMEOUT_MS = 30_000


def new_id() -> str:
    return uuid.uuid4().hex


def utc_now_iso() -> str:
    # Microsecond precision: rows created in the same second (e.g. an apply
    # followed by an immediate rollback) must still order correctly by ts.
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


MIGRATIONS: list[tuple[str, str]] = [
    (
        "0001_initial",
        """
CREATE TABLE sessions (
    file_path      TEXT PRIMARY KEY,
    source         TEXT NOT NULL,             -- 'claude' | 'codex'
    session_id     TEXT NOT NULL DEFAULT '',
    project_path   TEXT NOT NULL DEFAULT '',
    headless       INTEGER NOT NULL DEFAULT 0,
    is_subagent    INTEGER NOT NULL DEFAULT 0,
    first_ts       TEXT NOT NULL DEFAULT '',  -- ISO UTC from data
    last_ts        TEXT NOT NULL DEFAULT '',
    mtime          REAL NOT NULL DEFAULT 0,   -- scan trigger only
    file_size      INTEGER NOT NULL DEFAULT 0,
    bytes_scanned  INTEGER NOT NULL DEFAULT 0,
    lines_scanned  INTEGER NOT NULL DEFAULT 0,
    malformed_lines INTEGER NOT NULL DEFAULT 0,
    status         TEXT NOT NULL DEFAULT 'ok', -- ok|partial|error
    error          TEXT NOT NULL DEFAULT '',
    last_scanned_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX idx_sessions_project ON sessions(project_path);
CREATE INDEX idx_sessions_source ON sessions(source);

CREATE TABLE incidents (
    id            TEXT PRIMARY KEY,
    session_file  TEXT NOT NULL REFERENCES sessions(file_path),
    session_id    TEXT NOT NULL DEFAULT '',
    project_path  TEXT NOT NULL DEFAULT '',
    ts            TEXT NOT NULL DEFAULT '',
    signal_type   TEXT NOT NULL,             -- correction|standing_instruction|frustration|repeated_error|friction_loop
    matched_text  TEXT NOT NULL DEFAULT '',  -- redacted trigger text
    window_json   TEXT NOT NULL DEFAULT '[]',-- redacted context window; survives transcript age-out
    score         REAL NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'new', -- new|mined|dismissed
    run_id        TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL
);
CREATE INDEX idx_incidents_status ON incidents(status);
CREATE INDEX idx_incidents_project ON incidents(project_path);
CREATE INDEX idx_incidents_signal ON incidents(signal_type);

CREATE TABLE learnings (
    id             TEXT PRIMARY KEY,
    title          TEXT NOT NULL DEFAULT '',
    rule_text      TEXT NOT NULL,
    why            TEXT NOT NULL DEFAULT '',
    category       TEXT NOT NULL DEFAULT '',
    scope          TEXT NOT NULL DEFAULT '', -- global|project|skill|hook|rule_path|codex_global
    evidence_count INTEGER NOT NULL DEFAULT 0,
    project_count  INTEGER NOT NULL DEFAULT 0,
    projects_json  TEXT NOT NULL DEFAULT '[]',
    first_seen     TEXT NOT NULL DEFAULT '',
    last_seen      TEXT NOT NULL DEFAULT '',
    confidence     REAL NOT NULL DEFAULT 0,
    status         TEXT NOT NULL DEFAULT 'candidate', -- candidate|proposed|applied|rejected|pruned|superseded
    duplicate_of   TEXT NOT NULL DEFAULT '', -- existing-rule fingerprint or learning id
    created_at     TEXT NOT NULL
);
CREATE INDEX idx_learnings_status ON learnings(status);

CREATE TABLE incident_learnings (
    incident_id TEXT NOT NULL REFERENCES incidents(id),
    learning_id TEXT NOT NULL REFERENCES learnings(id),
    PRIMARY KEY (incident_id, learning_id)
);

CREATE TABLE proposals (
    id                     TEXT PRIMARY KEY,
    learning_id            TEXT NOT NULL REFERENCES learnings(id),
    run_id                 TEXT NOT NULL DEFAULT '',
    target_path            TEXT NOT NULL,
    target_kind            TEXT NOT NULL, -- global_claude_md|project_agents_md|project_claude_md|codex_global|skill|hook|rule_file
    action                 TEXT NOT NULL, -- see PROPOSAL_ACTIONS below
    diff_unified           TEXT NOT NULL DEFAULT '',
    status                 TEXT NOT NULL DEFAULT 'pending',
        -- See PROPOSAL_STATUSES below; the dashboard's buckets must partition
        -- it, and a test asserts that in both directions.
        -- `inconclusive`: the majority gate's scenarios disagreed. HELD, never
        -- auto-applied — mixed evidence is not absence of evidence.
    eval_result_id         TEXT NOT NULL DEFAULT '',
    applied_at             TEXT NOT NULL DEFAULT '',
    snapshot_commit_before TEXT NOT NULL DEFAULT '',
    snapshot_commit_after  TEXT NOT NULL DEFAULT '',
    created_at             TEXT NOT NULL
);
CREATE INDEX idx_proposals_status ON proposals(status);
CREATE INDEX idx_proposals_run ON proposals(run_id);

CREATE TABLE proposal_events (
    id          TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL REFERENCES proposals(id),
    ts          TEXT NOT NULL,
    event       TEXT NOT NULL,  -- created|gated|applied|held|rejected_user|rolled_back|approved_user
    actor       TEXT NOT NULL,  -- 'auto' | 'user'
    note        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX idx_proposal_events_proposal ON proposal_events(proposal_id);

CREATE TABLE eval_results (
    id             TEXT PRIMARY KEY,
    kind           TEXT NOT NULL,  -- self_eval|regression|ab
    subject_id     TEXT NOT NULL DEFAULT '', -- learning/proposal/labeled-incident id
    started        TEXT NOT NULL DEFAULT '',
    finished       TEXT NOT NULL DEFAULT '',
    attempted      INTEGER NOT NULL DEFAULT 0,
    succeeded      INTEGER NOT NULL DEFAULT 0,
    failed         INTEGER NOT NULL DEFAULT 0,
    error_taxonomy_json TEXT NOT NULL DEFAULT '{}',
    metrics_json   TEXT NOT NULL DEFAULT '{}',
    verdict        TEXT NOT NULL DEFAULT ''  -- gated_pass|gated_fail|ungated|inconclusive|pass|fail
);

CREATE TABLE llm_calls (
    id              TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL DEFAULT '',
    stage           TEXT NOT NULL,  -- mine|cluster|propose|eval_gen|grade
    provider        TEXT NOT NULL DEFAULT '',  -- claude|codex
    account         TEXT NOT NULL DEFAULT '',
    model_requested TEXT NOT NULL DEFAULT '',
    model_reported  TEXT NOT NULL DEFAULT '',
    prompt_sha      TEXT NOT NULL DEFAULT '',
    tokens_in       INTEGER NOT NULL DEFAULT 0,
    tokens_out      INTEGER NOT NULL DEFAULT 0,
    duration_ms     INTEGER NOT NULL DEFAULT 0,
    outcome         TEXT NOT NULL DEFAULT '',
        -- One of llm.OUTCOMES; tests/test_dashboard_queries.py asserts every
        -- failure value there has operator-facing copy. Do not re-list them here:
        -- this comment was stale for empty_output, parse_recovered and max_turns.
    error           TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL
);
CREATE INDEX idx_llm_calls_run ON llm_calls(run_id);

CREATE TABLE runs (
    id          TEXT PRIMARY KEY,
    started     TEXT NOT NULL,
    finished    TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'running', -- running|ok|degraded|error|budget_exhausted|interrupted|abandoned
    stats_json  TEXT NOT NULL DEFAULT '{}',
    report_path TEXT NOT NULL DEFAULT ''
);

CREATE TABLE project_stats (
    id            TEXT PRIMARY KEY,
    run_id        TEXT NOT NULL REFERENCES runs(id),
    project_path  TEXT NOT NULL,
    period_start  TEXT NOT NULL DEFAULT '',
    period_end    TEXT NOT NULL DEFAULT '',
    incidents_by_type_json TEXT NOT NULL DEFAULT '{}',
    sessions_active INTEGER NOT NULL DEFAULT 0,
    rules_applied  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_project_stats_project ON project_stats(project_path);

CREATE TABLE error_fingerprints (
    fingerprint   TEXT NOT NULL,
    session_file  TEXT NOT NULL,
    session_id    TEXT NOT NULL DEFAULT '',
    project_path  TEXT NOT NULL DEFAULT '',
    count_in_session INTEGER NOT NULL DEFAULT 0,
    sample_text   TEXT NOT NULL DEFAULT '',
    first_ts      TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (fingerprint, session_file)
);
CREATE INDEX idx_error_fp ON error_fingerprints(fingerprint);
""",
    ),
    (
        # Provenance needed downstream: regression-eval generation requires the
        # incident_summary, and routing rule 4 (codex-general) requires source.
        "0002_learning_provenance",
        """
ALTER TABLE learnings ADD COLUMN incident_summary TEXT NOT NULL DEFAULT '';
ALTER TABLE learnings ADD COLUMN source TEXT NOT NULL DEFAULT '';
""",
    ),
    (
        # Cached rule-text embeddings (local model2vec vectors). vector_json is
        # a strict-JSON array of floats -> maps to pgvector/jsonb on Supabase.
        # Keyed by (owner, model) so existing-rule units and learnings coexist
        # and a model swap invalidates cleanly.
        "0003_embeddings",
        """
CREATE TABLE embeddings (
    owner_kind  TEXT NOT NULL,   -- 'learning' | 'rule_unit'
    owner_key   TEXT NOT NULL,   -- learning id | sha1 of (path, unit text)
    model       TEXT NOT NULL,
    text_sha    TEXT NOT NULL,   -- sha1 of the embedded text (staleness check)
    vector_json TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (owner_kind, owner_key, model)
);
""",
    ),
    (
        # Part 2: enforcement-gap + path-scoped routing provenance. The
        # agentic miner reports which in-force rule an incident VIOLATED
        # (non-empty -> the rule exists but was ignored -> hook-conversion
        # proposal) and optional path globs for .claude/rules routing.
        "0004_enforcement_and_rule_paths",
        """
ALTER TABLE learnings ADD COLUMN violated_existing_rule TEXT NOT NULL DEFAULT '';
ALTER TABLE learnings ADD COLUMN path_globs_json TEXT NOT NULL DEFAULT '[]';
""",
    ),
    (
        # Part 2: cross-file contradiction findings (embedding-paired,
        # LLM-judged). status: new -> user dismisses or resolves via edits.
        "0005_contradictions",
        """
CREATE TABLE contradictions (
    id           TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL DEFAULT '',
    file_a       TEXT NOT NULL,
    unit_a       TEXT NOT NULL,
    file_b       TEXT NOT NULL,
    unit_b       TEXT NOT NULL,
    cosine       REAL NOT NULL DEFAULT 0,
    verdict      TEXT NOT NULL,             -- contradicts|compatible
    explanation  TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'new', -- new|dismissed|resolved
    created_at   TEXT NOT NULL
);
CREATE INDEX idx_contradictions_status ON contradictions(status);
""",
    ),
    (
        # Canonical project identity. project_path is a raw cwd, so N working
        # copies of one repo counted as N projects — and routing promotes to
        # the GLOBAL file at project_count >= 3, so a single-repo lesson could
        # escalate itself by being learned in three clones. project_key is the
        # collapse; project_key_method records HOW it was resolved so a
        # degraded resolution is visible in the run report rather than silent.
        #
        # Purely additive: every column has a default and the cache table is
        # new, so applying this to an existing DB cannot lose data. Existing
        # rows carry project_key='' until the backfill runs.
        "0006_canonical_project_identity",
        """
ALTER TABLE sessions ADD COLUMN project_key TEXT NOT NULL DEFAULT '';
ALTER TABLE sessions ADD COLUMN project_display TEXT NOT NULL DEFAULT '';
ALTER TABLE sessions ADD COLUMN project_key_method TEXT NOT NULL DEFAULT '';
ALTER TABLE incidents ADD COLUMN project_key TEXT NOT NULL DEFAULT '';
CREATE INDEX idx_sessions_project_key ON sessions(project_key);
CREATE INDEX idx_incidents_project_key ON incidents(project_key);
CREATE TABLE project_identity_cache (
    remote_norm TEXT PRIMARY KEY,   -- host/owner/name
    project_key TEXT NOT NULL,
    display     TEXT NOT NULL,
    method      TEXT NOT NULL,
    resolved_at TEXT NOT NULL
);
""",
    ),
    (
        # "Which repo is this?" and "where do I write the line?" are different
        # questions. projects_json now holds canonical KEYS (so project_count
        # counts repos, not working copies), but routing needs a real directory
        # for Path(project) / 'AGENTS.md'. This column carries the working copy
        # the incident actually came from.
        #
        # Note the write target is still one clone per learning, so a lesson
        # seen in two clones still proposes two writes. Consolidating that is a
        # policy call (which working copy is canonical?) and is deliberately
        # left to the operator rather than guessed here.
        "0007_learning_primary_project_path",
        """
ALTER TABLE learnings ADD COLUMN primary_project_path TEXT NOT NULL DEFAULT '';
""",
    ),
    (
        # Retain the cause breakdown on each session as well as in run stats.
        # A partial session needs its own explanation even outside a run report.
        #
        # Strict JSON TEXT per store.py's Supabase-portable rules: an object of
        # cause -> count, '{}' when the last read was clean.
        "0008_session_malformed_by_cause",
        """
ALTER TABLE sessions ADD COLUMN malformed_by_cause TEXT NOT NULL DEFAULT '{}';
""",
    ),
    (
        "0009_execution_policy",
        """
CREATE TABLE execution_policies (
    target_class TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
    enabled_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 0
);
INSERT INTO execution_policies (target_class) VALUES ('global'), ('project'), ('skill'), ('hook');
CREATE TABLE execution_policy_events (
    id TEXT PRIMARY KEY,
    target_class TEXT NOT NULL REFERENCES execution_policies(target_class),
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    before_json TEXT NOT NULL,
    after_json TEXT NOT NULL
);
""",
    ),
    (
        "0010_dashboard_commands",
        """
CREATE TABLE proposal_revisions (
    id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL REFERENCES proposals(id),
    fingerprint TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (proposal_id, fingerprint)
);
CREATE TABLE proposal_eval_history (
    id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL REFERENCES proposals(id),
    source_revision_id TEXT NOT NULL REFERENCES proposal_revisions(id),
    run_id TEXT NOT NULL REFERENCES runs(id),
    eval_result_id TEXT NOT NULL DEFAULT '',
    verdict TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_proposal_eval_history ON proposal_eval_history(proposal_id, created_at, id);
CREATE TABLE commands (
    id TEXT PRIMARY KEY,
    request_key TEXT NOT NULL UNIQUE,
    request_hash TEXT NOT NULL,
    action TEXT NOT NULL,
    state TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    result_json TEXT NOT NULL DEFAULT '{}',
    error_code TEXT NOT NULL DEFAULT '',
    error_detail TEXT NOT NULL DEFAULT '',
    max_model_calls INTEGER NOT NULL DEFAULT 0,
    claimed_by TEXT NOT NULL DEFAULT '',
    claim_token TEXT NOT NULL DEFAULT '',
    lease_expires_at TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_commands_queue ON commands(state, created_at, id);
CREATE TABLE command_targets (
    id TEXT PRIMARY KEY,
    command_id TEXT NOT NULL REFERENCES commands(id),
    target_key TEXT NOT NULL,
    destination_json TEXT NOT NULL,
    diff_unified TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued',
    checkpoint_json TEXT NOT NULL DEFAULT '{}',
    error_code TEXT NOT NULL DEFAULT '',
    error_detail TEXT NOT NULL DEFAULT '',
    UNIQUE (command_id, target_key)
);
CREATE TABLE command_members (
    id TEXT PRIMARY KEY,
    command_id TEXT NOT NULL REFERENCES commands(id),
    proposal_id TEXT NOT NULL REFERENCES proposals(id),
    revision_id TEXT NOT NULL REFERENCES proposal_revisions(id),
    target_id TEXT NOT NULL REFERENCES command_targets(id),
    decision_scope TEXT NOT NULL,
    UNIQUE (command_id, proposal_id)
);
""",
    ),
    (
        "0011_command_controls",
        """
ALTER TABLE commands ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0;
CREATE TABLE command_control_events (
    id TEXT PRIMARY KEY,
    request_key TEXT NOT NULL UNIQUE,
    request_hash TEXT NOT NULL,
    command_id TEXT NOT NULL REFERENCES commands(id),
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    before_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_command_controls ON command_control_events(command_id, created_at, id);
""",
    ),
    (
        "0012_instruction_operations",
        """
CREATE TABLE instruction_operations (
    id TEXT PRIMARY KEY,
    operation_key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    proposal_id TEXT NOT NULL REFERENCES proposals(id),
    record_json TEXT NOT NULL,
    record_hash TEXT NOT NULL,
    checkpoint_json TEXT NOT NULL,
    state TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    failures_json TEXT NOT NULL DEFAULT '[]',
    error_code TEXT NOT NULL DEFAULT '',
    error_detail TEXT NOT NULL DEFAULT '',
    result_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX idx_instruction_operations_state ON instruction_operations(state, created_at, id);
""",
    ),
    (
        "0013_instruction_requests",
        """
CREATE TABLE instruction_requests (
    request_key TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    operation_id TEXT NOT NULL REFERENCES instruction_operations(id),
    request_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
""",
    ),
    (
        "0014_instruction_controls",
        """
ALTER TABLE instruction_operations ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0;
CREATE TABLE instruction_operation_controls (
    request_key TEXT PRIMARY KEY REFERENCES instruction_requests(request_key),
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    before_json TEXT NOT NULL
);
""",
    ),
    (
        "0015_rejection_scopes",
        """
ALTER TABLE proposals ADD COLUMN decision_scope TEXT NOT NULL DEFAULT '';
CREATE TABLE rejection_members (
    id TEXT PRIMARY KEY,
    command_id TEXT NOT NULL REFERENCES commands(id),
    proposal_id TEXT NOT NULL REFERENCES proposals(id),
    revision_id TEXT NOT NULL REFERENCES proposal_revisions(id),
    learning_id TEXT NOT NULL REFERENCES learnings(id),
    decision_scope TEXT NOT NULL,
    target_identity_json TEXT NOT NULL,
    UNIQUE (command_id, proposal_id)
);
CREATE INDEX idx_rejections_learning ON rejection_members(learning_id, decision_scope);
""",
    ),
    (
        "0016_model_jobs",
        """
CREATE TABLE model_jobs (
    command_id TEXT PRIMARY KEY REFERENCES commands(id),
    source_revision_id TEXT NOT NULL REFERENCES proposal_revisions(id),
    plan_json TEXT NOT NULL,
    plan_hash TEXT NOT NULL,
    run_id TEXT NOT NULL DEFAULT ''
);
CREATE TABLE job_budgets (
    id TEXT PRIMARY KEY,
    command_id TEXT NOT NULL REFERENCES commands(id),
    pool TEXT NOT NULL,
    maximum INTEGER NOT NULL,
    UNIQUE (command_id, pool)
);
CREATE TABLE job_steps (
    id TEXT PRIMARY KEY,
    command_id TEXT NOT NULL REFERENCES commands(id),
    step_key TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    state TEXT NOT NULL,
    result_json TEXT NOT NULL DEFAULT '{}',
    result_hash TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (command_id, step_key)
);
CREATE TABLE job_calls (
    id TEXT PRIMARY KEY,
    command_id TEXT NOT NULL REFERENCES commands(id),
    step_id TEXT NOT NULL REFERENCES job_steps(id),
    call_index INTEGER NOT NULL,
    pool TEXT NOT NULL,
    input_json TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    state TEXT NOT NULL,
    result_json TEXT NOT NULL DEFAULT '{}',
    result_hash TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (step_id, call_index)
);
CREATE TABLE job_evaluations (
    id TEXT PRIMARY KEY,
    command_id TEXT NOT NULL REFERENCES commands(id),
    scenario_index INTEGER NOT NULL,
    eval_result_id TEXT NOT NULL REFERENCES eval_results(id),
    content_hash TEXT NOT NULL,
    UNIQUE (command_id, scenario_index)
);
""",
    ),
    (
        "0017_rollback_resolutions",
        """
CREATE TABLE proposal_resolutions (
    proposal_id TEXT PRIMARY KEY REFERENCES proposals(id),
    command_id TEXT NOT NULL UNIQUE REFERENCES commands(id),
    record_json TEXT NOT NULL,
    record_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
""",
    ),
    (
        "0018_reapplications",
        """
CREATE TABLE proposal_reapplications (
    proposal_id TEXT PRIMARY KEY REFERENCES proposals(id),
    command_id TEXT NOT NULL UNIQUE REFERENCES commands(id),
    source_key TEXT NOT NULL,
    record_json TEXT NOT NULL,
    record_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX proposal_reapplications_source ON proposal_reapplications(source_key);
""",
    ),
    (
        "0019_incident_jobs",
        """
CREATE TABLE incident_jobs (
    command_id TEXT PRIMARY KEY REFERENCES commands(id),
    incident_id TEXT NOT NULL REFERENCES incidents(id),
    source_json TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    plan_hash TEXT NOT NULL,
    run_id TEXT NOT NULL DEFAULT ''
);
CREATE INDEX incident_jobs_source ON incident_jobs(incident_id);
""",
    ),
    (
        "0020_recovery_jobs",
        """
CREATE TABLE recovery_jobs (
    command_id TEXT PRIMARY KEY REFERENCES commands(id),
    learning_id TEXT NOT NULL REFERENCES learnings(id),
    source_json TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    plan_hash TEXT NOT NULL,
    run_id TEXT NOT NULL DEFAULT ''
);
CREATE INDEX recovery_jobs_source ON recovery_jobs(learning_id);
CREATE TABLE proposal_recoveries (
    proposal_id TEXT PRIMARY KEY REFERENCES proposals(id),
    command_id TEXT NOT NULL REFERENCES commands(id),
    record_json TEXT NOT NULL,
    record_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
""",
    ),
    (
        "0021_mining_history",
        """
CREATE TABLE mining_history (
    id TEXT PRIMARY KEY,
    learning_id TEXT NOT NULL REFERENCES learnings(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    generation TEXT NOT NULL,
    run_id TEXT NOT NULL DEFAULT '',
    command_id TEXT NOT NULL DEFAULT '',
    call_id TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL,
    record_json TEXT NOT NULL,
    record_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX mining_history_learning ON mining_history(learning_id, created_at, id);
""",
    ),
    (
        # Scan measurement (slice S1; contract docs/dashboard-parity/PARALLEL_WORK.md).
        # Observations are an append-only audit ledger of scan attempts. Lines and
        # occurrences are content-free projections: exactly one ACTIVE revision per
        # transcript position (lines) or occurrence identity, per compatibility key.
        # A changed revision supersedes the old row; history rows are retained.
        # Independent of 0023-0025. Field meanings: scan_observations.py.
        "0022_scan_observations",
        """
CREATE TABLE scan_manifests (
    id TEXT PRIMARY KEY,
    compatibility_key TEXT NOT NULL,
    detector_key TEXT NOT NULL,
    parser_key TEXT NOT NULL,
    config_key TEXT NOT NULL,
    semantics_version INTEGER NOT NULL,
    identifiable INTEGER NOT NULL CHECK (identifiable IN (0, 1)),
    manifest_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX scan_manifests_compatibility ON scan_manifests(compatibility_key);
CREATE TABLE scan_working_copies (
    id TEXT PRIMARY KEY,
    project_key TEXT NOT NULL,
    normalized_path TEXT NOT NULL,
    normalization TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE scan_observations (
    id TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    run_id TEXT NOT NULL,
    source TEXT NOT NULL,
    session_file TEXT NOT NULL,
    transcript_id TEXT NOT NULL,
    canonical_path TEXT NOT NULL,
    path_method TEXT NOT NULL,
    manifest_id TEXT NOT NULL REFERENCES scan_manifests(id),
    compatibility_key TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('succeeded', 'failed')),
    failure_cause TEXT NOT NULL DEFAULT '',
    projection TEXT NOT NULL DEFAULT '',
    file_size INTEGER NOT NULL DEFAULT 0,
    byte_end INTEGER NOT NULL DEFAULT 0,
    line_end INTEGER NOT NULL DEFAULT 0,
    pending_bytes INTEGER NOT NULL DEFAULT 0,
    first_occurred_at TEXT NOT NULL DEFAULT '',
    last_occurred_at TEXT NOT NULL DEFAULT '',
    record_json TEXT NOT NULL,
    observed_at TEXT NOT NULL
);
CREATE INDEX scan_observations_file ON scan_observations(session_file, observed_at, id);
CREATE INDEX scan_observations_transcript
    ON scan_observations(transcript_id, compatibility_key, observed_at, id);
CREATE TABLE scan_lines (
    id TEXT PRIMARY KEY,
    revision_hash TEXT NOT NULL,
    transcript_id TEXT NOT NULL,
    compatibility_key TEXT NOT NULL,
    line_no INTEGER NOT NULL,
    line_key TEXT NOT NULL,
    line_sha256 TEXT NOT NULL,
    byte_start INTEGER NOT NULL,
    byte_end INTEGER NOT NULL,
    occurred_at TEXT NOT NULL DEFAULT '',
    time_status TEXT NOT NULL,
    source TEXT NOT NULL,
    project_key TEXT NOT NULL DEFAULT '',
    project_key_method TEXT NOT NULL DEFAULT '',
    working_copy_id TEXT NOT NULL DEFAULT '',
    logical_session_key TEXT NOT NULL DEFAULT '',
    headless INTEGER NOT NULL CHECK (headless IN (0, 1)),
    is_subagent INTEGER NOT NULL CHECK (is_subagent IN (0, 1)),
    category TEXT NOT NULL,
    cause TEXT NOT NULL DEFAULT '',
    exclusion TEXT NOT NULL DEFAULT '',
    detector_coverage TEXT NOT NULL,
    events INTEGER NOT NULL DEFAULT 0,
    observation_id TEXT NOT NULL REFERENCES scan_observations(id),
    superseded_by TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL CHECK (active IN (0, 1))
);
CREATE UNIQUE INDEX scan_lines_active_position
    ON scan_lines(transcript_id, compatibility_key, line_no) WHERE active = 1;
CREATE INDEX scan_lines_window ON scan_lines(project_key, compatibility_key, active, occurred_at);
CREATE INDEX scan_lines_transcript ON scan_lines(transcript_id, compatibility_key, active);
CREATE TABLE scan_occurrences (
    id TEXT PRIMARY KEY,
    revision_hash TEXT NOT NULL,
    occurrence_id TEXT NOT NULL,
    transcript_id TEXT NOT NULL,
    compatibility_key TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('signal', 'error_fingerprint')),
    signal_type TEXT NOT NULL,
    trigger_line_key TEXT NOT NULL,
    trigger_line_no INTEGER NOT NULL,
    supporting_lines_json TEXT NOT NULL,
    discriminator TEXT NOT NULL,
    occurred_at TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL,
    project_key TEXT NOT NULL DEFAULT '',
    working_copy_id TEXT NOT NULL DEFAULT '',
    logical_session_key TEXT NOT NULL DEFAULT '',
    headless INTEGER NOT NULL CHECK (headless IN (0, 1)),
    is_subagent INTEGER NOT NULL CHECK (is_subagent IN (0, 1)),
    exclusion TEXT NOT NULL DEFAULT '',
    evidence_json TEXT NOT NULL,
    observation_id TEXT NOT NULL REFERENCES scan_observations(id),
    superseded_by TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL CHECK (active IN (0, 1))
);
CREATE UNIQUE INDEX scan_occurrences_active ON scan_occurrences(occurrence_id) WHERE active = 1;
CREATE INDEX scan_occurrences_window
    ON scan_occurrences(project_key, compatibility_key, active, occurred_at);
CREATE INDEX scan_occurrences_transcript
    ON scan_occurrences(transcript_id, compatibility_key, active);
CREATE TABLE scan_observation_occurrences (
    observation_id TEXT NOT NULL REFERENCES scan_observations(id),
    occurrence_id TEXT NOT NULL,
    PRIMARY KEY (observation_id, occurrence_id)
);
CREATE INDEX scan_observation_occurrences_occurrence
    ON scan_observation_occurrences(occurrence_id);
CREATE TABLE scan_incident_links (
    id TEXT PRIMARY KEY,
    occurrence_id TEXT NOT NULL,
    incident_id TEXT NOT NULL REFERENCES incidents(id),
    observation_id TEXT NOT NULL REFERENCES scan_observations(id),
    link_kind TEXT NOT NULL CHECK (link_kind IN ('produced', 'corroborated')),
    created_at TEXT NOT NULL,
    UNIQUE (incident_id, observation_id)
);
CREATE INDEX scan_incident_links_occurrence ON scan_incident_links(occurrence_id);
""",
    ),
    (
        # Codex integration: preserve published 0022. The contract permits
        # several occurrences per incident within one observation. 0023-0025
        # remain reserved for eval, availability, and project measurements.
        "0026_scan_incident_links",
        """
CREATE TABLE scan_incident_links_next (
    id TEXT PRIMARY KEY,
    occurrence_id TEXT NOT NULL,
    incident_id TEXT NOT NULL REFERENCES incidents(id),
    observation_id TEXT NOT NULL REFERENCES scan_observations(id),
    link_kind TEXT NOT NULL CHECK (link_kind IN ('produced', 'corroborated')),
    created_at TEXT NOT NULL,
    UNIQUE (incident_id, observation_id, occurrence_id)
);
INSERT INTO scan_incident_links_next
    (id, occurrence_id, incident_id, observation_id, link_kind, created_at)
    SELECT id, occurrence_id, incident_id, observation_id, link_kind, created_at
    FROM scan_incident_links;
DROP TABLE scan_incident_links;
ALTER TABLE scan_incident_links_next RENAME TO scan_incident_links;
CREATE INDEX scan_incident_links_occurrence ON scan_incident_links(occurrence_id);
""",
    ),
    (
        # Codex: exact-run keyset history without scanning every observation.
        "0027_scan_run_index",
        "CREATE INDEX scan_observations_run ON scan_observations(run_id, observed_at, id);",
    ),
    (
        # Reserved by Codex: immutable delivered revisions and actual copy observations.
        "0024_rule_availability",
        """
CREATE TABLE rule_revisions (
    id TEXT PRIMARY KEY, learning_id TEXT NOT NULL, proposal_id TEXT NOT NULL,
    application_id TEXT NOT NULL, application_event_id TEXT NOT NULL,
    project_key TEXT NOT NULL, content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL, record_json TEXT NOT NULL, record_hash TEXT NOT NULL,
    UNIQUE(application_event_id, learning_id)
);
CREATE INDEX rule_revisions_project ON rule_revisions(project_key, learning_id);
CREATE TABLE rule_availability_observations (
    id TEXT PRIMARY KEY, rule_revision_id TEXT NOT NULL, project_key TEXT NOT NULL,
    working_copy_id TEXT NOT NULL, observed_at TEXT NOT NULL, status TEXT NOT NULL,
    run_id TEXT NOT NULL, record_json TEXT NOT NULL, record_hash TEXT NOT NULL
);
CREATE INDEX rule_availability_history ON rule_availability_observations(rule_revision_id, working_copy_id, observed_at, id);
CREATE INDEX rule_availability_project ON rule_availability_observations(project_key, observed_at, id);
CREATE TABLE rule_availability_collections (
    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, observed_at TEXT NOT NULL,
    status TEXT NOT NULL, record_json TEXT NOT NULL, record_hash TEXT NOT NULL
);
CREATE INDEX rule_availability_collection_run ON rule_availability_collections(run_id, observed_at, id);
""",
    ),
    (
        "0028_instruction_inventory",
        """
CREATE TABLE instruction_inventories (
    id TEXT PRIMARY KEY, project_key TEXT NOT NULL, working_copy_id TEXT NOT NULL,
    observed_at TEXT NOT NULL, run_id TEXT NOT NULL, status TEXT NOT NULL,
    record_json TEXT NOT NULL, record_hash TEXT NOT NULL
);
CREATE INDEX instruction_inventory_copy ON instruction_inventories(project_key, working_copy_id, observed_at, id);
""",
    ),
    (
        # Reserved Codex identifier; append without changing published migrations.
        "0023_eval_attempts",
        """
CREATE TABLE eval_attempts (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, proposal_id TEXT NOT NULL,
    learning_id TEXT NOT NULL, run_id TEXT NOT NULL, command_id TEXT NOT NULL,
    source_revision_id TEXT NOT NULL, created_at TEXT NOT NULL,
    record_json TEXT NOT NULL, record_hash TEXT NOT NULL
);
CREATE UNIQUE INDEX eval_attempt_command ON eval_attempts(command_id) WHERE command_id != '';
CREATE INDEX eval_attempt_proposal ON eval_attempts(proposal_id, created_at, id);
CREATE INDEX eval_attempt_learning ON eval_attempts(learning_id, created_at, id);
CREATE INDEX eval_attempt_run ON eval_attempts(run_id, created_at, id);
CREATE TABLE eval_attempt_events (
    id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL REFERENCES eval_attempts(id),
    event_key TEXT NOT NULL, kind TEXT NOT NULL, scenario_index INTEGER NOT NULL,
    arm TEXT NOT NULL, trial_index INTEGER NOT NULL, created_at TEXT NOT NULL,
    record_json TEXT NOT NULL, record_hash TEXT NOT NULL,
    UNIQUE(attempt_id, event_key)
);
CREATE INDEX eval_attempt_events_owner ON eval_attempt_events(attempt_id, created_at, id);
""",
    ),
    (
        "0029_quality_evidence",
        """
CREATE TABLE quality_subjects (
    id TEXT PRIMARY KEY, target_class TEXT NOT NULL, created_at TEXT NOT NULL,
    record_json TEXT NOT NULL, record_hash TEXT NOT NULL
);
CREATE TABLE quality_samples (
    id TEXT PRIMARY KEY, target_class TEXT NOT NULL, created_at TEXT NOT NULL,
    record_json TEXT NOT NULL, record_hash TEXT NOT NULL
);
CREATE INDEX quality_sample_history ON quality_samples(target_class, created_at, id);
CREATE TABLE quality_judgments (
    id TEXT PRIMARY KEY, subject_id TEXT NOT NULL REFERENCES quality_subjects(id),
    sample_id TEXT NOT NULL REFERENCES quality_samples(id), sequence INTEGER NOT NULL,
    created_at TEXT NOT NULL, record_json TEXT NOT NULL, record_hash TEXT NOT NULL,
    UNIQUE(subject_id, sequence)
);
CREATE INDEX quality_judgment_history ON quality_judgments(subject_id, sequence);
CREATE TABLE evidence_command_results (
    id TEXT PRIMARY KEY REFERENCES commands(id), created_at TEXT NOT NULL,
    record_json TEXT NOT NULL, record_hash TEXT NOT NULL
);
""",
    ),
    (
        "0030_session_context",
        """
CREATE TABLE session_context_batches (
    observation_id TEXT PRIMARY KEY REFERENCES scan_observations(id),
    profile TEXT NOT NULL, content_hash TEXT NOT NULL, record_count INTEGER NOT NULL,
    record_json TEXT NOT NULL
);
CREATE TABLE session_context_records (
    id TEXT PRIMARY KEY,
    observation_id TEXT NOT NULL REFERENCES session_context_batches(observation_id),
    line_key TEXT NOT NULL, line_no INTEGER NOT NULL, source TEXT NOT NULL,
    project_key TEXT NOT NULL, working_copy_id TEXT NOT NULL,
    logical_session_key TEXT NOT NULL, occurred_at TEXT NOT NULL,
    kind TEXT NOT NULL, record_json TEXT NOT NULL,
    UNIQUE(observation_id, line_no)
);
CREATE INDEX session_context_project ON session_context_records(project_key, logical_session_key);
CREATE INDEX session_context_session ON session_context_records(logical_session_key, observation_id);
""",
    ),
    (
        "0031_native_load_reports",
        """
CREATE TABLE native_load_reports (
    id TEXT PRIMARY KEY, profile TEXT NOT NULL, received_at TEXT NOT NULL,
    project_key TEXT NOT NULL, logical_session_key TEXT NOT NULL,
    working_copy_id TEXT NOT NULL, event_name TEXT NOT NULL,
    record_json TEXT NOT NULL, record_hash TEXT NOT NULL
);
CREATE INDEX native_load_project ON native_load_reports(project_key, received_at, id);
CREATE INDEX native_load_session ON native_load_reports(logical_session_key, received_at, id);
""",
    ),
    (
        "0025_project_measurements",
        """
ALTER TABLE project_stats ADD COLUMN record_type TEXT NOT NULL DEFAULT '';
ALTER TABLE project_stats ADD COLUMN project_key TEXT NOT NULL DEFAULT '';
ALTER TABLE project_stats ADD COLUMN rule_revision_id TEXT NOT NULL DEFAULT '';
ALTER TABLE project_stats ADD COLUMN compatibility_key TEXT NOT NULL DEFAULT '';
ALTER TABLE project_stats ADD COLUMN observed_at TEXT NOT NULL DEFAULT '';
ALTER TABLE project_stats ADD COLUMN record_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE project_stats ADD COLUMN record_hash TEXT NOT NULL DEFAULT '';
CREATE INDEX project_measurement_history ON project_stats(record_type, project_key, observed_at, id);
CREATE INDEX project_measurement_latest ON project_stats(record_type, project_key, rule_revision_id, compatibility_key, observed_at, id);
""",
    ),
    (
        "0032_rule_families",
        """
CREATE TABLE rule_family_snapshots (
    id TEXT PRIMARY KEY, profile TEXT NOT NULL, generation INTEGER NOT NULL UNIQUE,
    created_at TEXT NOT NULL, source_hash TEXT NOT NULL, config_hash TEXT NOT NULL,
    record_json TEXT NOT NULL, record_hash TEXT NOT NULL
);
CREATE INDEX rule_family_latest ON rule_family_snapshots(config_hash, source_hash, generation);
CREATE TABLE rule_family_members (
    snapshot_id TEXT NOT NULL REFERENCES rule_family_snapshots(id),
    learning_id TEXT NOT NULL, family_id TEXT NOT NULL, text_hash TEXT NOT NULL,
    PRIMARY KEY(snapshot_id, learning_id)
);
CREATE INDEX rule_family_members_page ON rule_family_members(snapshot_id, family_id, learning_id);
CREATE TABLE rule_family_heads (
    profile TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL REFERENCES rule_family_snapshots(id),
    selected_at TEXT NOT NULL
);
""",
    ),
    (
        "0033_instruction_text",
        """
CREATE TABLE instruction_text_archives (
    id TEXT PRIMARY KEY, inventory_id TEXT NOT NULL UNIQUE,
    project_key TEXT NOT NULL, working_copy_id TEXT NOT NULL,
    observed_at TEXT NOT NULL, run_id TEXT NOT NULL,
    record_json TEXT NOT NULL, record_hash TEXT NOT NULL
);
CREATE INDEX instruction_text_history ON instruction_text_archives(project_key, working_copy_id, observed_at, id);
""",
    ),
    ("0034_queue_history", """
-- Bounded SQLite-specific observation contract; text primary IDs remain portable.
-- A future backend needs equivalent transaction-local row observation.
CREATE TABLE queue_coverage (
    id TEXT PRIMARY KEY CHECK(id = 'global'), profile TEXT NOT NULL,
    started_at TEXT NOT NULL, opening_count INTEGER NOT NULL CHECK(opening_count >= 0),
    opening_unknown_count INTEGER NOT NULL CHECK(opening_unknown_count >= 0)
);
INSERT INTO queue_coverage VALUES ('global', 'run-backlog/1',
    CURRENT_TIMESTAMP || 'Z', (SELECT COUNT(*) FROM incidents WHERE status='new'),
    (SELECT COUNT(*) FROM incidents WHERE status NOT IN ('new','mined','dismissed')));
CREATE TABLE queue_events (
    id TEXT PRIMARY KEY, seq INTEGER NOT NULL UNIQUE CHECK(seq > 0),
    observed_at TEXT NOT NULL, incident_id TEXT NOT NULL,
    operation TEXT NOT NULL, old_status TEXT, new_status TEXT,
    queue_delta INTEGER NOT NULL
);
CREATE INDEX queue_event_time ON queue_events(observed_at,seq);
CREATE TABLE queue_processing (
    event_id TEXT PRIMARY KEY REFERENCES queue_events(id), profile TEXT NOT NULL,
    outcome TEXT NOT NULL, run_id TEXT NOT NULL, command_id TEXT NOT NULL, call_id TEXT NOT NULL,
    stage TEXT NOT NULL DEFAULT '', prompt_sha TEXT NOT NULL DEFAULT ''
);
CREATE TABLE queue_snapshots (
    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, phase TEXT NOT NULL CHECK(phase IN ('start','finish')),
    profile TEXT NOT NULL, observed_at TEXT NOT NULL, event_seq INTEGER NOT NULL CHECK(event_seq >= 0),
    queue_count INTEGER NOT NULL CHECK(queue_count >= 0),
    processing_count INTEGER NOT NULL CHECK(processing_count >= 0), settings_json TEXT NOT NULL,
    UNIQUE(run_id,phase)
);
CREATE TRIGGER queue_observe_insert AFTER INSERT ON incidents
BEGIN
    INSERT INTO queue_events(id,seq,observed_at,incident_id,operation,old_status,new_status,queue_delta)
    VALUES (lower(hex(randomblob(16))), (SELECT COALESCE(MAX(seq),0)+1 FROM queue_events),
        CURRENT_TIMESTAMP || 'Z', NEW.id,
        'insert', NULL, NEW.status, CASE WHEN NEW.status='new' THEN 1 ELSE 0 END);
END;
CREATE TRIGGER queue_observe_update AFTER UPDATE ON incidents WHEN OLD.status IS NOT NEW.status
BEGIN
    INSERT INTO queue_events(id,seq,observed_at,incident_id,operation,old_status,new_status,queue_delta)
    VALUES (lower(hex(randomblob(16))), (SELECT COALESCE(MAX(seq),0)+1 FROM queue_events),
        CURRENT_TIMESTAMP || 'Z', NEW.id,
        'update', OLD.status, NEW.status, (CASE WHEN NEW.status='new' THEN 1 ELSE 0 END) - (CASE WHEN OLD.status='new' THEN 1 ELSE 0 END));
END;
CREATE TRIGGER queue_observe_delete AFTER DELETE ON incidents
BEGIN
    INSERT INTO queue_events(id,seq,observed_at,incident_id,operation,old_status,new_status,queue_delta)
    VALUES (lower(hex(randomblob(16))), (SELECT COALESCE(MAX(seq),0)+1 FROM queue_events),
        CURRENT_TIMESTAMP || 'Z', OLD.id,
        'delete', OLD.status, NULL, CASE WHEN OLD.status='new' THEN -1 ELSE 0 END);
END;
CREATE TRIGGER queue_coverage_no_update BEFORE UPDATE ON queue_coverage
BEGIN
    SELECT RAISE(ABORT, 'Queue history is append-only');
END;
CREATE TRIGGER queue_coverage_no_delete BEFORE DELETE ON queue_coverage
BEGIN
    SELECT RAISE(ABORT, 'Queue history is append-only');
END;
CREATE TRIGGER queue_events_no_update BEFORE UPDATE ON queue_events
BEGIN
    SELECT RAISE(ABORT, 'Queue history is append-only');
END;
CREATE TRIGGER queue_events_no_delete BEFORE DELETE ON queue_events
BEGIN
    SELECT RAISE(ABORT, 'Queue history is append-only');
END;
CREATE TRIGGER queue_processing_no_update BEFORE UPDATE ON queue_processing
BEGIN
    SELECT RAISE(ABORT, 'Queue history is append-only');
END;
CREATE TRIGGER queue_processing_no_delete BEFORE DELETE ON queue_processing
BEGIN
    SELECT RAISE(ABORT, 'Queue history is append-only');
END;
CREATE TRIGGER queue_snapshots_no_update BEFORE UPDATE ON queue_snapshots
BEGIN
    SELECT RAISE(ABORT, 'Queue history is append-only');
END;
CREATE TRIGGER queue_snapshots_no_delete BEFORE DELETE ON queue_snapshots
BEGIN
    SELECT RAISE(ABORT, 'Queue history is append-only');
END;
"""),
]


#: Every value ``proposals.action`` may hold.
#:
#: The schema comment used to spell this out and had drifted: it listed
#: ``delete`` and omitted ``delete_human_line``, which is the value
#: ``propose.py`` actually writes for an unmarked line and the one PRD S5.1's
#: "deleting a human-authored line always queues" carve-out depends on. Reading
#: that comment is enough to conclude the carve-out cannot fire.
#:
#: ``config.review_queue_actions`` is validated against this, because that
#: tuple is matched to ``proposals.action`` by STRING: a typo there does not
#: fail, it silently turns a carve-out off and the proposal auto-applies.
PROPOSAL_ACTIONS = frozenset({
    "add",
    "edit",
    "delete",
    "delete_human_line",
    "convert_to_hook",
    "resolve_rollback",
    "reapply",
    "recover_rule",
    "new_skill",
    "new_rule_file",
})


#: LLM outcomes that delivered an answer. Shared here to avoid importing the
#: quota-router dependency into every reader; llm.py checks its OUTCOMES against
#: this set. Keep clean success, credential-race recovery, and prose-wrapped JSON
#: recovery distinct so retries and format recovery remain visible in reports.
LLM_SUCCESS_OUTCOMES = ("ok", "oauth_transient_retried", "parse_recovered")

#: Proposal status groups shared by report, dashboard, and application readers.
#: Reconcile them with the independently declared PROPOSAL_STATUSES so omissions
#: fail loudly; deriving both sides from one list would make that check vacuous.
QUEUEING_STATUSES = ("pending", "gated_fail", "inconclusive", "held", "ungated")
AUTO_APPLY_STATUSES = ("gated_pass",)
#: Decided by a person in V3 and waiting for `apply.py`. Distinct from TERMINAL
#: on purpose: an approval is not an application.
AWAITING_APPLY_STATUSES = ("approved_user",)
TERMINAL_STATUSES = ("applied", "rejected_user", "rolled_back", "superseded")
#: Already dealt with. A carve-out queues on its ACTION and an action never
#: changes, so without this a rejected carve-out returns to the queue forever.
DECIDED_STATUSES = AWAITING_APPLY_STATUSES + TERMINAL_STATUSES


#: Every value ``proposals.status`` may hold. The ONE declaration.
#:
#: Readers must include producer-written states such as `superseded`.
#: `tests/test_dashboard_queries.py` independently checks that the dashboard
#: buckets partition this set, so a new status needs an explicit classification.
PROPOSAL_STATUSES = frozenset({
    "pending",        # built, not yet gated
    "gated_pass",     # the majority gate passed it
    "gated_fail",     # the majority gate failed it
    "ungated",        # no usable gate evidence; requires human approval
    "inconclusive",   # the gate's scenarios disagreed; HELD, never auto-applied
    "held",           # the apply policy declined to write it
    "approved_user",  # a person approved it in V3; apply.py has not run yet
    "rejected_user",  # a person rejected it in V3
    "applied",        # written to the target file
    "rolled_back",    # written, then reverted
    "superseded",     # replaced by a newer proposal for the same learning
})


# Attribute proposal events to the actor responsible for the event.
# A CLI invocation can request a gate, but the gate supplies its own judgment.
# Classify by event type, not caller identity; unknown event types raise.
ACTOR_FOR_EVENT = {
    "created": "auto",
    "gated": "auto",
    "superseded": "auto",
    "applied": "auto",
    "held": "auto",
    "approved_user": "user",
    "approval_cancelled": "user",
    "rejected_user": "user",
    "rolled_back": "user",
}


def actor_for(event: str) -> str:
    """The actor for an event. Raises on one nobody has classified."""
    return ACTOR_FOR_EVENT[event]


class Store:
    """SQLite handle with explicit migration and write modes.

    ``read_only=True`` uses ``mode=ro`` and skips migrations. Both properties are
    required: ordinary construction can otherwise change the schema before the
    first query. A missing schema element fails rather than being added implicitly.

    ``migrate=False`` permits writes to an existing database without migrations or
    creation. Dashboard writers and delivery workers use this mode so operational
    requests cannot upgrade the schema. The operator runs schema upgrades explicitly.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        read_only: bool = False,
        migrate: bool | None = None,
    ):
        self.db_path = Path(db_path)
        self.read_only = read_only
        # `migrate` is tri-state on purpose. None means "derive it", which keeps
        # every existing call site meaning exactly what it meant. An EXPLICIT
        # migrate=True next to read_only=True is a contradiction the caller has
        # to resolve, so it raises instead of silently picking a winner.
        if read_only and migrate is True:
            raise ValueError(
                "read_only=True cannot migrate: a read-only connection has no "
                "way to apply one. Pass migrate=False, or drop read_only."
            )
        self.migrate = (not read_only) if migrate is None else migrate
        if read_only:
            if not self.db_path.exists():
                raise FileNotFoundError(f"state DB does not exist: {self.db_path}")
            self.conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            return
        if not self.migrate:
            # Read-write, but the schema does not move and the file is never
            # created. Creating it would hand back an empty, unmigrated
            # database that accepts writes — the caller would believe it wrote
            # to the operator's state while writing to a file nobody reads.
            if not self.db_path.exists():
                raise FileNotFoundError(f"state DB does not exist: {self.db_path}")
            self.conn = sqlite3.connect(str(self.db_path))
            self.conn.row_factory = sqlite3.Row
            self.conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            self.conn.execute("PRAGMA foreign_keys=ON")
            # journal_mode is deliberately NOT set here. It is a property of
            # the file, not of this connection, and a handle that refuses to
            # move the schema has no business moving that either.
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
            # Nightly and manual operations can open the database concurrently.
            # The nightly lock does not serialize every database caller.
            self.conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self._migrate()
        except BaseException:
            self.conn.close()
            raise

    def migration_plan(self) -> dict:
        """Read supported migration history without changing schema or data."""
        import hashlib
        names = [name for name, _ in MIGRATIONS]
        if len(names) != len(set(names)):
            raise ValueError('Migration definitions contain duplicate names')
        present = self.query_one("SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'")
        if present:
            applied = [row['name'] for row in self.query('SELECT name FROM schema_migrations')]
            if any(not isinstance(name, str) or not name for name in applied):
                raise ValueError('State contains an invalid migration name')
        else:
            if self.query("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
                raise ValueError('Existing state has tables but no migration history')
            applied = []
        if set(applied) - set(names):
            raise ValueError('Migration history contains unknown names')
        if set(applied) != set(names[:len(applied)]):
            self._validate_migration_subset(set(applied))
        return {'applied': [name for name in names if name in applied],
                'pending': [name for name in names if name not in applied],
                'migration_source_sha256': hashlib.sha256(json.dumps(MIGRATIONS).encode()).hexdigest()}

    def _validate_migration_subset(self, applied: set[str]) -> None:
        """Preserve supported additive omissions, never infer a missing receipt."""
        sql = ("SELECT type,name,tbl_name,sql FROM sqlite_master "
               "WHERE name NOT LIKE 'sqlite_%' AND name!='schema_migrations' "
               "AND tbl_name!='schema_migrations' ORDER BY type,name")
        actual = [tuple(row) for row in self.conn.execute(sql)]
        with closing(sqlite3.connect(':memory:')) as expected:
            try:
                for name, script in MIGRATIONS:
                    if name in applied:
                        expected.executescript(script)
            except sqlite3.Error as exc:
                raise ValueError('Migration history cannot form a supported schema: ' + str(exc)) from exc
            if actual != expected.execute(sql).fetchall():
                raise ValueError('Migration history does not match the recorded subset schema')

    def _apply_pending_migrations(self) -> None:
        """Execute complete SQL statements without executescript's implicit commit."""
        if not self.conn.in_transaction:
            raise RuntimeError('Migration application requires its caller transaction')
        plan = self.migration_plan()
        self.conn.execute('CREATE TABLE IF NOT EXISTS schema_migrations ('
                          'name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)')
        for name, sql in MIGRATIONS:
            if name not in plan['pending']:
                continue
            start = 0
            for offset, char in enumerate(sql):
                # SQLite decides whether semicolons are inside strings, comments
                # or trigger bodies. Never split SQL on semicolons alone.
                if char == ';' and sqlite3.complete_statement(sql[start:offset + 1]):
                    self.conn.execute(sql[start:offset + 1])
                    start = offset + 1
            if sql[start:].strip():
                self.conn.execute(sql[start:])
            self.conn.execute('INSERT INTO schema_migrations (name, applied_at) VALUES (?, ?)',
                              (name, utc_now_iso()))

    def _migrate(self) -> None:
        if not self.migration_plan()['pending']:
            return
        with self.transaction(write=True):
            # Re-read under the reservation: another opener can finish first.
            self._apply_pending_migrations()

    @staticmethod
    def _logical_digest(connection) -> str:
        import hashlib
        digest = hashlib.sha256()
        for line in connection.iterdump():
            digest.update(line.encode('utf-8'))
            digest.update(b'\n')
        return digest.hexdigest()

    def _backup_upgrade_state(self, destination: Path, plan: dict) -> dict:
        """Back up committed state while this handle reserves the only writer."""
        import hashlib
        import os
        from .data_boundary import private_destination, manifest_sha, verify_files
        destination = private_destination(destination)
        destination.mkdir(parents=True, mode=0o700)
        backup = destination / 'state.db'
        with backup.open('xb'):
            os.chmod(backup, 0o600)
        with closing(sqlite3.connect(self.db_path.resolve().as_uri() + '?mode=ro', uri=True)) as source, \
                closing(sqlite3.connect(backup)) as target:
            # Backing up the connection that holds BEGIN IMMEDIATE can wait on
            # itself forever. A second reader sees the same committed snapshot;
            # the reserved writer prevents concurrent commits for this interval.
            source.backup(target)
            target.execute('PRAGMA journal_mode=DELETE')
            if target.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                raise ValueError('Upgrade backup failed SQLite integrity verification')
            logical = self._logical_digest(source)
            if self._logical_digest(target) != logical:
                raise ValueError('Upgrade backup differs from committed source data')
        with backup.open('rb') as handle:
            digest = hashlib.file_digest(handle, 'sha256').hexdigest()
        manifest = {'kind': 'state-upgrade-backup', 'version': 1,
                    'source': str(self.db_path.resolve()), 'created_at': utc_now_iso(),
                    'migration_plan': plan, 'logical_sha256': logical,
                    'files': {'state.db': {'sha256': digest, 'bytes': backup.stat().st_size}}}
        manifest['sha256'] = manifest_sha(manifest)
        with (destination / 'manifest.json').open('x', encoding='utf-8') as handle:
            os.chmod(handle.name, 0o600)
            handle.write(json.dumps(manifest, indent=2) + '\n')
        if verify_files(destination) != manifest:
            raise ValueError('Upgrade backup changed during verification')
        return manifest

    def upgrade_with_backup(self, destination: Path | None) -> dict:
        """Own backup-before-mutation and all pending migrations in one transaction."""
        if self.read_only or self.conn.in_transaction:
            raise ValueError('Upgrade requires a writable handle without pending caller work')
        with self.transaction(write=True):
            plan = self.migration_plan()
            if not plan['pending']:
                return {'state': 'current', **plan, 'backup': None}
            if destination is None:
                raise ValueError('Pending migrations require --backup NEW_PRIVATE_DIRECTORY')
            manifest = self._backup_upgrade_state(destination, plan)
            self._apply_pending_migrations()
            after = self.migration_plan()
            return {'state': 'upgraded', **after, 'applied_now': plan['pending'],
                    'backup': str(destination.resolve()), 'backup_sha256': manifest['sha256']}

    # ------------------------------------------------------------------
    # Generic helpers — pipeline modules use these (plus the typed helpers
    # below) rather than editing this file. JSON-typed columns (*_json) are
    # passed as already-serialized strict-JSON strings by callers.
    # ------------------------------------------------------------------

    @contextmanager
    def transaction(self, *, write: bool = False):
        """Own one transaction. Writer reservation precedes authorization reads.

        SQLite's locking syntax stays in this storage adapter. A future
        backend must provide equivalent serialization, not copy this syntax.
        Nested use is refused so a helper cannot commit its caller's work.
        """
        if self.conn.in_transaction:
            raise RuntimeError("transaction requires a handle with no pending transaction")
        if write and self.read_only:
            raise ValueError("a read-only Store cannot start a write transaction")
        self.conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        try:
            yield
            # Deferred constraints can fail at commit after every statement
            # succeeded. Roll that failure back before a later close can commit.
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise

    def insert(self, table: str, row: dict) -> None:
        cols = list(row)
        self.conn.execute(
            f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
            [row[c] for c in cols],
        )

    def update(self, table: str, key_col: str, key: str, updates: dict) -> None:
        sets = ", ".join(f"{c} = ?" for c in updates)
        self.conn.execute(
            f"UPDATE {table} SET {sets} WHERE {key_col} = ?",
            [*updates.values(), key],
        )

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, params)]

    def query_one(self, sql: str, params: tuple = ()) -> dict | None:
        r = self.conn.execute(sql, params).fetchone()
        return dict(r) if r else None

    def schema_tables(self, *, expected_migrations=None) -> dict:
        """Read table keys and declared references without migrations or writes.

        SQLite-specific metadata stays in the storage adapter. Consumers receive
        grouped foreign keys, including composite keys in their declared order.
        """
        if expected_migrations is not None:
            # Reconstruct only the supplied applied schema in private memory.
            # This never opens, migrates or changes the selected database.
            memory = sqlite3.connect(':memory:')
            memory.row_factory = sqlite3.Row
            try:
                for name, sql in MIGRATIONS:
                    if name in expected_migrations:
                        memory.executescript(sql)
                return self._schema_tables(memory)
            except sqlite3.Error as exc:
                raise ValueError('Applied migration set cannot form a supported rebuild schema: '+str(exc)) from exc
            finally:
                memory.close()
        return self._schema_tables(self.conn)

    @staticmethod
    def _schema_tables(connection) -> dict:
        def query(sql):
            return [dict(r) for r in connection.execute(sql)]
        def quoted(name):
            return '"' + name.replace('"', '""') + '"'
        result = {}
        for row in query("SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
            name = row['name']
            columns = query('PRAGMA table_info(' + quoted(name) + ')')
            groups = {}
            for fk in query('PRAGMA foreign_key_list(' + quoted(name) + ')'):
                groups.setdefault(fk['id'], []).append(fk)
            result[name] = {
                'definition': row['sql'],
                'columns': [c['name'] for c in columns],
                'primary_key': [c['name'] for c in sorted(columns, key=lambda c: c['pk']) if c['pk']],
                'foreign_keys': [
                    {'table': rows[0]['table'], 'columns': [r['from'] for r in rows],
                     'references': [r['to'] for r in rows]}
                    for rows in (sorted(group, key=lambda r: r['seq']) for group in groups.values())],
            }
        return result

    def check_foreign_keys(self) -> None:
        """Refuse damaged references, including on a read-only connection."""
        errors = self.query('PRAGMA foreign_key_check')
        if errors:
            first = errors[0]
            raise ValueError(f"{first['table']} row {first['rowid']}: missing foreign-key parent {first['parent']}")

    # ------------------------------------------------------------------
    # Typed helpers.
    # ------------------------------------------------------------------

    def upsert_session(self, row: dict) -> None:
        cols = (
            "file_path source session_id project_path project_key project_display "
            "project_key_method headless is_subagent first_ts "
            "last_ts mtime file_size bytes_scanned lines_scanned malformed_lines "
            "malformed_by_cause status error last_scanned_at"
        ).split()
        # Reconcile this column list with the schema in test_store_schema.py so an
        # added counter cannot silently fall back to a database default.
        # Derived identity metadata has an explicit unresolved state. Other required
        # fields fail when omitted. Defaults are per column: strict-JSON fields need
        # valid JSON such as {}, not the empty string used by unresolved identity fields.
        optional = {
            "project_key": "",
            "project_display": "",
            "project_key_method": "",
            "malformed_by_cause": "{}",
        }
        placeholders = ", ".join("?" for _ in cols)
        updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "file_path")
        self.conn.execute(
            f"INSERT INTO sessions ({', '.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT(file_path) DO UPDATE SET {updates}",
            [row.get(c, optional[c]) if c in optional else row[c] for c in cols],
        )

    def link_incident_learning(self, incident_id: str, learning_id: str) -> None:
        """Idempotently link an incident to a learning.

        Retries or rescans can resolve to the same pair. Re-linking is a no-op through
        Postgres-compatible ON CONFLICT DO NOTHING.
        """
        self.conn.execute(
            "INSERT INTO incident_learnings (incident_id, learning_id) "
            "VALUES (?, ?) ON CONFLICT DO NOTHING",
            (incident_id, learning_id),
        )

    def get_session(self, file_path: str) -> dict | None:
        r = self.conn.execute(
            "SELECT * FROM sessions WHERE file_path = ?", (file_path,)
        ).fetchone()
        return dict(r) if r else None

    def insert_incident(self, row: dict) -> str:
        rid = row.get("id") or new_id()
        self.conn.execute(
            "INSERT INTO incidents (id, session_file, session_id, project_path, "
            " project_key, ts, "
            " signal_type, matched_text, window_json, score, status, run_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rid,
                row["session_file"],
                row.get("session_id", ""),
                row.get("project_path", ""),
                row.get("project_key", ""),
                row.get("ts", ""),
                row["signal_type"],
                row.get("matched_text", ""),
                json.dumps(row.get("window", []), ensure_ascii=False),
                row.get("score", 0.0),
                row.get("status", "new"),
                row.get("run_id", ""),
                utc_now_iso(),
            ),
        )
        return rid

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()
