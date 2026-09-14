"""Tests for report.generate: markdown run report rendered from store reads.

Seeds a tmp Store with one full run's worth of rows (sessions, incidents,
learnings, proposals — one applied with a diff, one held —, llm_calls,
eval_results) and asserts the generated markdown carries the funnel numbers,
per-stage taxonomy, reduction ratio, token totals, the applied diff with its
evidence session ids, and the held queue. Every expected number is computed
in the test from the seeded values, never from the module under test.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from self_improve.config import Config
from self_improve.report import ReportError, generate
from self_improve.store import Store

CFG = Config()
RUN = "run-1"
STARTED = "2026-08-14T00:00:00Z"
FINISHED = "2026-08-14T02:00:00Z"

DIFF = (
    "--- a/CLAUDE.md\n"
    "+++ b/CLAUDE.md\n"
    "@@ -1,2 +1,3 @@\n"
    "+- Always run uv pytest before committing"
)

# Windows for the four run-1 incidents (insert_incident serializes these).
W1 = ["short window"]
W2 = [{"note": "boom happened [truncated 42 chars] tail"}]
W3: list = []
W4 = ["ctx"]

FILE_A = "/fake/a.jsonl"
FILE_B = "/fake/b.jsonl"
FILE_C = "/fake/c.jsonl"


def _session(file_path: str, **over) -> dict:
    row = {
        "file_path": file_path,
        "source": "claude",
        "session_id": "",
        "project_path": "/proj/alpha",
        "headless": 0,
        "is_subagent": 0,
        "first_ts": "",
        "last_ts": "",
        "mtime": 1.0,
        "file_size": 0,
        "bytes_scanned": 0,
        "lines_scanned": 0,
        "malformed_lines": 0,
        "status": "ok",
        "error": "",
        "last_scanned_at": "2026-08-14T01:00:00Z",
    }
    row.update(over)
    return row


def _llm(store: Store, call_id: str, stage: str, outcome: str,
         model_requested: str, model_reported: str,
         tokens_in: int, tokens_out: int) -> None:
    store.insert(
        "llm_calls",
        {
            "id": call_id,
            "run_id": RUN,
            "stage": stage,
            "provider": "claude",
            "model_requested": model_requested,
            "model_reported": model_reported,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "outcome": outcome,
            "created_at": "2026-08-14T01:30:00Z",
        },
    )


def seed(tmp_path) -> Store:
    store = Store(tmp_path / "state.db")
    store.insert(
        "runs",
        {
            "id": RUN,
            "started": STARTED,
            "finished": FINISHED,
            "status": "ok",
            "stats_json": json.dumps(
                # Shape must mirror what pipeline.run_pipeline persists:
                # scan stats nested under "scan" (ScanStats.as_dict keys),
                # LLM budget refusals under "llm".
                {
                    "scan": {
                        "files_skipped_unchanged": 7,
                        "dropped_by_cap": {"correction": 3},
                        "events_denylisted": 2,
                        # THIS run's own counters, which a real run always
                        # records. The funnel reads these; the session sums
                        # (150 lines, 2 malformed) are each session's LIFETIME
                        # total and are deliberately different here so the two
                        # cannot be confused again.
                        "files_succeeded": 3,
                        "files_failed": 0,
                        "lines_scanned": 120,
                        "malformed_lines": 1,
                    },
                    "llm": {"refused": {"cheap": 1, "strong": 0}},
                }
            ),
            "report_path": "",
        },
    )

    # Sessions: ok + partial + error inside the run's scan window, plus one
    # outside it that must NOT be counted.
    store.upsert_session(_session(
        FILE_A, session_id="sess-a", lines_scanned=100, bytes_scanned=5000,
        last_scanned_at="2026-08-14T01:00:00Z"))
    store.upsert_session(_session(
        FILE_B, session_id="sess-b", status="partial", lines_scanned=50,
        malformed_lines=2, bytes_scanned=2500,
        last_scanned_at="2026-08-14T01:10:00Z"))
    store.upsert_session(_session(
        FILE_C, status="error", error="parse: ValueError: boom",
        last_scanned_at="2026-08-14T01:20:00Z"))
    store.upsert_session(_session(
        "/fake/outside.jsonl", lines_scanned=999, bytes_scanned=99999,
        last_scanned_at="2026-08-13T23:00:00Z"))

    # Incidents for this run (4) + one belonging to another run (excluded).
    i1 = store.insert_incident({
        "session_file": FILE_A, "session_id": "sess-a",
        "ts": "2026-08-10T10:00:00Z", "signal_type": "correction",
        "matched_text": "no, use uv run", "window": W1, "score": 0.9,
        "status": "mined", "run_id": RUN})
    i2 = store.insert_incident({
        "session_file": FILE_B, "session_id": "sess-b",
        "ts": "2026-08-11T11:00:00Z", "signal_type": "repeated_error",
        "matched_text": "fp::boom", "window": W2, "score": 2.0,
        "status": "mined", "run_id": RUN})
    store.insert_incident({
        "session_file": FILE_A, "session_id": "sess-a",
        "ts": "2026-08-12T12:00:00Z", "signal_type": "frustration",
        "matched_text": "WHY", "window": W3, "score": 0.5,
        "status": "dismissed", "run_id": RUN})
    i4 = store.insert_incident({
        "session_file": FILE_B, "session_id": "sess-b",
        "ts": "2026-08-13T13:00:00Z", "signal_type": "correction",
        "matched_text": "wrong dir", "window": W4, "score": 0.7,
        "status": "new", "run_id": RUN})
    store.insert_incident({
        "session_file": FILE_A, "session_id": "sess-a",
        "ts": "2026-08-01T00:00:00Z", "signal_type": "correction",
        "matched_text": "other run", "window": ["other"], "score": 0.1,
        "status": "new", "run_id": "run-OTHER"})

    # Learnings + evidence links.
    store.insert("learnings", {
        "id": "L1", "title": "Always run uv pytest",
        "rule_text": "Run uv pytest before committing.",
        "status": "applied", "created_at": "2026-08-14T01:40:00Z"})
    store.insert("learnings", {
        "id": "L2", "title": "",  # falls back to rule_text in the report
        "rule_text": "Never hardcode config dir",
        "status": "proposed", "created_at": "2026-08-14T01:40:00Z"})
    store.insert("incident_learnings", {"incident_id": i1, "learning_id": "L1"})
    store.insert("incident_learnings", {"incident_id": i2, "learning_id": "L1"})
    store.insert("incident_learnings", {"incident_id": i4, "learning_id": "L2"})

    # Proposals: one applied with a diff, one held (with eval verdict), one
    # pending.
    store.insert("proposals", {
        "id": "prop-applied-1", "learning_id": "L1", "run_id": RUN,
        "target_path": "/fake/targets/CLAUDE.md",
        "target_kind": "global_claude_md", "action": "add",
        "diff_unified": DIFF, "status": "applied",
        "applied_at": "2026-08-14T01:50:00Z",
        "snapshot_commit_before": "abc123", "snapshot_commit_after": "def456",
        "created_at": "2026-08-14T01:45:00Z"})
    store.insert("eval_results", {
        "id": "eval-1", "kind": "self_eval", "subject_id": "prop-held-1",
        "attempted": 3, "succeeded": 1, "failed": 2, "verdict": "ungated"})
    store.insert("proposals", {
        "id": "prop-held-1", "learning_id": "L2", "run_id": RUN,
        "target_path": "/fake/targets/AGENTS.md",
        "target_kind": "project_agents_md", "action": "add",
        "diff_unified": "+- Never hardcode config dir", "status": "held",
        "eval_result_id": "eval-1", "created_at": "2026-08-14T01:46:00Z"})
    store.insert("proposals", {
        "id": "prop-pending-1", "learning_id": "L2", "run_id": RUN,
        "target_path": "/fake/targets/AGENTS.md",
        "target_kind": "project_agents_md", "action": "add",
        "status": "pending", "created_at": "2026-08-14T01:47:00Z"})

    # LLM calls: mine 3 ok + 1 parse_error + 1 parse_recovered (sonnet),
    # propose 1 ok (opus) + 1 quota_exhausted (opus, no model reported).
    # parse_recovered is a SUCCESS outcome (valid JSON recovered from prose)
    # kept distinct from ok in the taxonomy table.
    _llm(store, "c1", "mine", "ok", "sonnet", "claude-sonnet-4-5", 100, 20)
    _llm(store, "c2", "mine", "ok", "sonnet", "claude-sonnet-4-5", 100, 20)
    _llm(store, "c3", "mine", "ok", "sonnet", "claude-sonnet-4-5", 100, 20)
    _llm(store, "c4", "mine", "parse_error", "sonnet", "claude-sonnet-4-5", 50, 0)
    _llm(store, "c5", "propose", "ok", "opus", "claude-opus-4-6", 200, 80)
    _llm(store, "c6", "propose", "quota_exhausted", "opus", "", 0, 0)
    _llm(store, "c7", "mine", "parse_recovered", "sonnet", "claude-sonnet-4-5", 100, 20)

    store.commit()
    return store


def render(tmp_path) -> str:
    store = seed(tmp_path)
    out_path = tmp_path / "reports" / "run.md"
    returned = generate(store, CFG, RUN, out_path)
    assert returned == str(out_path)
    return out_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Funnel + taxonomy
# ---------------------------------------------------------------------------


def test_report_funnel_numbers(tmp_path):
    md = render(tmp_path)

    # Session counts scoped to the run window: the 2026-08-13 session is
    # excluded (ok would read 2 otherwise, lines 1149).
    assert "| Sessions scanned (ok) | 1 |" in md
    assert "| Sessions partial | 1 |" in md
    assert "| Sessions failed | 1 |" in md
    assert "| Sessions skipped | 7 |" in md
    # Use this run's counters. The invented session lifetime totals (150/2)
    # deliberately differ from the run's scan totals (120/1).
    assert "| Lines scanned (this run) | 120 |" in md
    assert "| Malformed lines (this run) | 1 |" in md
    assert "| Lines scanned | 150 |" not in md

    # Incidents: only run-1's four, by signal and by status.
    assert "| Incidents (this run) | 4 |" in md
    assert "| — incidents: correction | 2 |" in md
    assert "| — incidents: frustration | 1 |" in md
    assert "| — incidents: repeated_error | 1 |" in md
    # Scope these incident rows to the scanning run; mining can happen later.
    assert "| — of those, now mined | 2 |" in md
    assert "| — of those, dismissed | 1 |" in md
    assert "| — of those, still new | 1 |" in md
    assert "| Incidents mined | 2 |" not in md

    # Mine stage attempted/succeeded/failed from llm_calls outcomes.
    # succeeded = 3 ok + 1 parse_recovered (a SUCCESS outcome); failed = the
    # single parse_error.
    assert "| Mine calls attempted | 5 |" in md
    assert "| Mine calls succeeded | 4 |" in md
    assert "| Mine calls failed | 1 |" in md

    assert "| Learnings (this run) | 2 |" in md
    assert "| Proposals: applied | 1 |" in md
    assert "| Proposals: held | 1 |" in md
    assert "| Proposals: pending | 1 |" in md
    assert "| Applied | 1 |" in md


def test_report_per_stage_taxonomy_and_scan_errors(tmp_path):
    md = render(tmp_path)

    # LLM taxonomy table, pipeline-stage order then outcome.
    assert "| Stage | Outcome | Count |" in md
    assert "| mine | ok | 3 |" in md
    assert "| mine | parse_error | 1 |" in md
    # parse_recovered stays its own taxonomy row (visible prose-wrapping
    # frequency) even though _stage_asf counts it as succeeded.
    assert "| mine | parse_recovered | 1 |" in md
    assert "| propose | ok | 1 |" in md
    assert "| propose | quota_exhausted | 1 |" in md
    # mine rows come before propose rows (STAGES order).
    assert md.index("| mine | ok | 3 |") < md.index("| propose | ok | 1 |")

    # Scan error section lists the failing session verbatim.
    assert "- Sessions with status=error: 1" in md
    # Cumulative over the sessions in scope (2), with this run's own count
    # (1) beside it. The fixture makes them differ on purpose: they are
    # different quantities and a report that prints both must say which.
    assert (
        "- Malformed lines, cumulative over this run's sessions "
        "(counted, skipped, surfaced): 2 (this run itself saw 1)"
    ) in md
    assert "  - `/fake/c.jsonl`: parse: ValueError: boom" in md


# ---------------------------------------------------------------------------
# Reduction ratio, caps, token totals, budget
# ---------------------------------------------------------------------------


def test_report_reduction_ratio_and_caps(tmp_path):
    md = render(tmp_path)

    # Expected chars computed exactly as insert_incident serialized them.
    window_chars = sum(
        len(json.dumps(w, ensure_ascii=False)) for w in (W1, W2, W3, W4)
    )
    bytes_total = 5000 + 2500  # error session contributed 0; outside excluded
    # Keep the lifetime counter for the reduction ratio and label it separately
    # from this run's scan counter.
    assert "- Lines scanned, cumulative over this run's sessions: 150" in md
    assert "this run itself read 120" in md
    assert f"- Bytes scanned: {bytes_total}" in md
    assert (
        f"- Chars sent toward mining (sum of incident window_json): "
        f"{window_chars} across 4 incidents"
    ) in md
    assert (
        f"- Reduction ratio (window chars / bytes scanned): "
        f"{window_chars / bytes_total:.4%}"
    ) in md
    assert f"- Window chars per line scanned: {window_chars / 150:.2f}" in md

    # Truncation markers inside windows (only W2 carries one) + recorded caps.
    assert "- `[truncated N chars]` markers inside incident windows: 1" in md
    assert (
        '- `filter_incidents per-signal cap (incidents dropped)`: {"correction": 3}' in md
    )
    assert "- `events denylisted`: 2" in md
    assert '- `LLM calls refused by budget`: {"cheap": 1}' in md


def test_report_token_totals_and_budget(tmp_path):
    md = render(tmp_path)

    assert "| Provider | Model (reported) | Calls | Tokens in | Tokens out |" in md
    assert "| claude | (none reported) | 1 | 0 | 0 |" in md
    assert "| claude | claude-opus-4-6 | 1 | 200 | 80 |" in md
    assert "| claude | claude-sonnet-4-5 | 5 | 450 | 80 |" in md
    assert "| **total** |  | 7 | 650 | 160 |" in md

    assert (
        f"- Calls requested as `sonnet`: 5 of budget {CFG.max_cheap_calls_per_run}"
    ) in md
    assert (
        f"- Calls requested as `opus`: 2 of budget {CFG.max_strong_calls_per_run}"
    ) in md
    assert "- Budget/quota refusals (`quota_exhausted` outcomes): 1" in md
    assert "  - stage propose, provider claude: 1" in md


# ---------------------------------------------------------------------------
# Applied proposal (diff + evidence) and held queue
# ---------------------------------------------------------------------------


def test_report_applied_diff_with_evidence(tmp_path):
    md = render(tmp_path)

    assert "### `/fake/targets/CLAUDE.md` — add (global_claude_md)" in md
    assert "- **Proposal**: `prop-applied-1`" in md
    assert "- **Learning**: Always run uv pytest" in md
    assert "- **Applied at**: 2026-08-14T01:50:00Z" in md
    assert "- **Snapshots**: before `abc123`, after `def456`" in md

    # Evidence: both incidents linked to L1, ordered by incident ts.
    assert "- **Evidence**: 2 incident(s)" in md
    assert "  - session `sess-a` @ 2026-08-10T10:00:00Z (correction)" in md
    assert "  - session `sess-b` @ 2026-08-11T11:00:00Z (repeated_error)" in md
    assert md.index("session `sess-a`") < md.index("session `sess-b`")

    # The full unified diff appears in a diff fence.
    assert f"```diff\n{DIFF}\n```" in md


def test_report_held_queue(tmp_path):
    md = render(tmp_path)

    # Held proposal with its eval verdict; L2 has no title so rule_text shows.
    assert (
        "- [held] `/fake/targets/AGENTS.md` (add): "
        "Never hardcode config dir — eval verdict: ungated"
    ) in md
    # `pending` is now a LINE in the queue, not a footnote under it. The
    # section lists every status that awaits a decision (QUEUEING_STATUSES),
    # so the operator sees which proposals rather than only how many.
    assert "- [pending]" in md, md.split("## Held / review queue")[1][:400]
    assert "await your decision** (1 not yet gated)" in md
    # The applied proposal must not appear in the held queue.
    assert "- [applied]" not in md


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


def test_report_unknown_run_raises(tmp_path):
    store = seed(tmp_path)
    out = tmp_path / "nope.md"
    with pytest.raises(ReportError, match="not found"):
        generate(store, CFG, "no-such-run", out)
    assert not out.exists()  # nothing written on failure


def test_report_bad_stats_json_raises(tmp_path):
    store = seed(tmp_path)
    store.insert("runs", {"id": "run-bad", "started": STARTED,
                          "stats_json": "{not json"})
    store.insert("runs", {"id": "run-list", "started": STARTED,
                          "stats_json": "[]"})
    store.commit()
    with pytest.raises(ReportError, match="not strict JSON"):
        generate(store, CFG, "run-bad", tmp_path / "bad.md")
    with pytest.raises(ReportError, match="must be a JSON object"):
        generate(store, CFG, "run-list", tmp_path / "list.md")


def test_report_writes_to_nested_out_path(tmp_path):
    store = seed(tmp_path)
    out_path = tmp_path / "deep" / "nested" / "report.md"
    returned = generate(store, CFG, RUN, out_path)
    assert returned == str(out_path)
    assert Path(returned).read_text(encoding="utf-8").startswith(
        f"# self-improve run report — `{RUN}`"
    )


# ---------------------------------------------------------------------------
# stats added 2026-08-17 must be RENDERED, not just dumped in the appendix
# ---------------------------------------------------------------------------


def _body_without_appendix(text: str) -> str:
    """The report minus the raw stats_json dump.

    The appendix contains every stat verbatim, so a substring assertion against
    the whole document passes even when the report renders nothing. Two tests
    here originally passed that way.
    """
    marker = "## Appendix: raw run stats_json"
    return text.split(marker)[0]


def _run_with_stats(tmp_path, stats: dict, cfg=None):
    from self_improve.store import Store, new_id, utc_now_iso

    store = Store(tmp_path / "s.db")
    rid = new_id()
    store.insert(
        "runs",
        {
            "id": rid, "started": utc_now_iso(), "finished": utc_now_iso(),
            "status": "ok", "stats_json": json.dumps(stats),
        },
    )
    store.commit()
    out = tmp_path / "r.md"
    generate(store, cfg or Config(state_dir=str(tmp_path / "state")), rid, out)
    return _body_without_appendix(out.read_text())


def test_report_shows_how_project_identity_was_resolved(tmp_path):
    """A run keyed mostly on remote_url still collapses clones but is one repo
    rename away from fracturing. That is the operator's call to make, so it has
    to be visible without reading raw JSON."""
    text = _run_with_stats(
        tmp_path,
        {"scan": {"project_key_methods": {"remote_url": 33, "unresolved": 73, "path": 18}}},
    )
    assert "Project identity" in text
    assert "remote_url" in text and "33" in text
    assert "unresolved" in text


def test_report_warns_when_most_projects_are_unresolved(tmp_path):
    """Assert the WARNING SENTENCE, not just the word.

    The first version of this test passed on the method table alone, which
    contains the row label "unresolved" whether or not a warning fires. A test
    that cannot fail when the warning is deleted is not testing the warning.
    """
    text = _run_with_stats(
        tmp_path, {"scan": {"project_key_methods": {"unresolved": 90, "remote_url": 10}}}
    )
    assert "did not resolve to a repository" in text
    assert "90 of 100" in text
    assert "clones cannot collapse" in text


def test_report_does_not_cry_wolf_when_resolution_is_healthy(tmp_path):
    """The complement: a healthy run must NOT carry the alarm."""
    text = _run_with_stats(
        tmp_path, {"scan": {"project_key_methods": {"gh_repo_id": 120, "unresolved": 6}}}
    )
    assert "did not resolve to a repository" not in text
    assert "will split old clones from new" not in text


def test_report_splits_the_two_dedup_channels(tmp_path):
    """If either channel goes to zero we need to see WHICH."""
    text = _run_with_stats(
        tmp_path, {"gate": {"dup_dropped": 12, "dup_dropped_by_miner": 9}}
    )
    assert "miner verdict" in text.lower()
    assert "9" in text and "12" in text


def test_report_states_the_mine_queue_ordering(tmp_path):
    """Ordering decides WHICH incidents get mined inside a budget."""
    text = _run_with_stats(
        tmp_path,
        {"mine": {"attempted": 9, "succeeded": 7, "failed": 2, "taxonomy": {},
                  "queue": {"order": "age_out_risk", "transcript_present": 1234,
                            "transcript_already_gone": 6}}},
    )
    assert "age_out_risk" in text
    assert "1234" in text or "1,234" in text


def test_report_notes_runs_it_reaped(tmp_path):
    text = _run_with_stats(tmp_path, {"stale_runs_reaped": {"abandoned": 2, "left_running": 0,
                                                            "unparseable_started": 0}})
    assert "abandoned" in text.lower()


def test_a_small_but_nonzero_group_never_renders_as_zero_percent(tmp_path):
    """Keep a small non-zero group visible after whole-percent rounding."""
    text = _run_with_stats(
        tmp_path,
        {"scan": {"project_key_methods": {"remote_url": 4900, "git_root": 7,
                                          "path": 33, "unresolved": 60}}},
    )
    assert "| git_root | 7 | <1% |" in text
    assert "| 7 | 0% |" not in text
    assert "| remote_url | 4900 | 98% |" in text


def test_report_shouts_when_a_stage_cannot_account_for_its_attempts(tmp_path):
    """Render an accounting violation when a stage cannot reconcile its attempts."""
    text = _run_with_stats(
        tmp_path,
        {
            "invariant_violations": ["mine: attempted=81 but succeeded=80 + failed=0 = 80"],
            "mine": {"attempted": 81, "succeeded": 80, "failed": 0, "taxonomy": {}},
        },
    )
    assert "ACCOUNTING BROKEN" in text
    assert "attempted=81" in text


def test_a_healthy_run_carries_no_accounting_alarm(tmp_path):
    text = _run_with_stats(
        tmp_path, {"mine": {"attempted": 5, "succeeded": 5, "failed": 0, "taxonomy": {}}}
    )
    assert "ACCOUNTING BROKEN" not in text


def test_report_flags_a_cap_that_lands_on_one_signal_far_harder(tmp_path):
    """Show when a cap's drop rates differ substantially across signals."""
    text = _run_with_stats(
        tmp_path,
        {
            "scan": {
                "dropped_by_cap": {"self_observation": 50, "repeated_error": 2},
                "incidents_by_signal": {"self_observation": 450, "repeated_error": 3998},
            }
        },
    )
    assert "not landing evenly" in text
    assert "self_observation" in text and "repeated_error" in text


def test_report_does_not_flag_an_evenly_landing_cap(tmp_path):
    text = _run_with_stats(
        tmp_path,
        {
            "scan": {
                "dropped_by_cap": {"a": 10, "b": 12},
                "incidents_by_signal": {"a": 100, "b": 100},
            }
        },
    )
    assert "not landing evenly" not in text


def test_cap_bias_compares_rates_not_raw_counts(tmp_path):
    """A common signal must not be flagged just for being common."""
    text = _run_with_stats(
        tmp_path,
        {
            "scan": {
                # 'common' drops 50x more in absolute terms but the SAME share.
                "dropped_by_cap": {"common": 500, "rare": 10},
                "incidents_by_signal": {"common": 5000, "rare": 100},
            }
        },
    )
    assert "not landing evenly" not in text


def test_cap_bias_percentages_divide_to_the_stated_ratio(tmp_path):
    """The displayed percentages must reproduce the displayed ratio.
    The invented smaller rate rounds incorrectly under fixed one-decimal
    formatting; keep enough precision for the arithmetic to remain consistent.
    """
    text = _run_with_stats(
        tmp_path,
        {
            "scan": {
                "dropped_by_cap": {
                    "instruction_edit": 13,
                    "standing_instruction": 4,
                    "frustration": 9,
                    "self_observation": 20,
                    "repeated_error": 2,
                },
                "incidents_by_signal": {
                    "self_observation": 380,
                    "repeated_error": 2498,
                    "instruction_edit": 87,
                    "frustration": 291,
                    "standing_instruction": 396,
                    "correction": 10,
                },
            }
        },
    )
    assert "not landing evenly" in text
    assert "versus 0.1%" not in text, "the small rate lost its significant digit"

    # Assert the property, not the literal: whatever precision the renderer
    # picks, the two percentages it prints must divide to the ratio printed
    # beside them. A hardcoded "0.06%" would pass while still permitting a
    # future formatting change to reintroduce arithmetic that does not work.
    line = next(ln for ln in text.splitlines() if "not landing evenly" in ln)
    worst, best = (float(x) for x in re.findall(r"(\d+\.?\d*)%", line)[:2])
    ratio = float(re.search(r"— (\d+)x", line).group(1))
    assert abs(worst / best - ratio) / ratio < 0.02, (
        f"{worst}% / {best}% = {worst / best:.0f}, but the line claims {ratio:.0f}x"
    )


def test_report_explains_learnings_that_produced_no_proposal(tmp_path):
    """Report routing refusal without inferring its cause from the counter."""
    text = _run_with_stats(
        tmp_path,
        {
            "mine": {"attempted": 3, "succeeded": 3, "failed": 0, "taxonomy": {}},
            "apply": {
                "attempted": 0, "applied": 0, "held": 0, "failed": 0,
                "taxonomy": {"propose_RoutingError": 3},
            },
        },
    )
    assert "3 learning(s) produced no proposal" in text
    assert "no writable target" in text
    assert "Missing or non-Git project directories" in text
    assert "invalid learning metadata" in text
    assert "Inspect the proposal-stage error details" in text


def test_no_routing_note_when_nothing_failed_to_route(tmp_path):
    text = _run_with_stats(
        tmp_path,
        {"apply": {"attempted": 3, "applied": 3, "held": 0, "failed": 0, "taxonomy": {}}},
    )
    assert "no writable target" not in text


def test_report_explains_proposals_whose_gate_never_ran(tmp_path):
    """`gate_budget_exhausted` is not a gate failure; say which it is.

    The gate's call pool can refuse a proposal before any trial runs. A held
    count must retain that cause rather than imply an evaluation rejected it.
    """
    text = _run_with_stats(
        tmp_path,
        {
            "apply": {
                "attempted": 12, "applied": 0, "held": 12, "failed": 0,
                "taxonomy": {"gate_budget_exhausted": 12},
            },
        },
    )
    assert "12" in text
    assert "never ran" in text
    # Must not be readable as "the eval rejected these".
    assert "not a verdict" in text or "did not fail" in text


def test_no_gate_note_when_the_gate_actually_ran(tmp_path):
    text = _run_with_stats(
        tmp_path,
        {"apply": {"attempted": 2, "applied": 2, "held": 0, "failed": 0, "taxonomy": {}}},
    )
    assert "never ran" not in text


def test_report_shows_what_detectors_skipped_rather_than_guessed(tmp_path):
    """Render detector skip counts and causes from scan statistics."""
    text = _run_with_stats(
        tmp_path,
        {
            "scan": {
                "detector_taxonomy": {
                    "instruction_edit_missing_path": 1234,
                    "friction_edit_missing_file": 1234,
                    "headless_text_skipped": 2456,
                }
            }
        },
    )
    assert "skipped rather than guessed" in text.lower() or "skipped, not guessed" in text.lower()
    assert "instruction_edit_missing_path" in text
    assert "1,234" in text or "1234" in text


def test_no_detector_skip_section_when_nothing_was_skipped(tmp_path):
    text = _run_with_stats(tmp_path, {"scan": {"detector_taxonomy": {}}})
    assert "instruction_edit_missing_path" not in text


def test_a_nonzero_rate_never_renders_as_zero_percent():
    """A positive rate must remain distinguishable from zero, even below display precision."""
    from self_improve.report import _rate_pct

    assert _rate_pct(0) == "0%"
    for tiny in (1e-9, 1e-12, 5e-10):
        rendered = _rate_pct(tiny)
        assert rendered != "0%", f"{tiny} rendered as 0%"
        assert rendered.startswith("<"), rendered
    # Ordinary values are untouched.
    assert _rate_pct(0.15) == "15.0%"
    assert _rate_pct(0.0006) == "0.06%"


def test_learnings_are_counted_for_the_run_that_mined_them(tmp_path):
    """Attribute a learning to its producing mine run when the incident came from an earlier scan."""
    from self_improve.store import Store, new_id, utc_now_iso

    store = Store(tmp_path / "s.db")
    scan_run, mine_run = new_id(), new_id()
    store.insert("runs", {"id": scan_run, "started": "2026-08-17T10:00:00.000000Z",
                          "finished": "2026-08-17T10:05:00.000000Z", "status": "ok",
                          "stats_json": "{}"})
    store.insert("runs", {"id": mine_run, "started": "2026-08-17T11:00:00.000000Z",
                          "finished": "2026-08-17T11:05:00.000000Z", "status": "ok",
                          "stats_json": "{}"})
    store.upsert_session({
        "file_path": "/s.jsonl", "source": "claude", "session_id": "s1",
        "project_path": "/p", "headless": 0, "is_subagent": 0,
        "first_ts": "2026-08-17T09:00:00Z", "last_ts": "2026-08-17T09:30:00Z",
        "mtime": 0.0, "file_size": 1, "bytes_scanned": 1, "lines_scanned": 1,
        "malformed_lines": 0, "status": "ok", "error": "",
        "last_scanned_at": "2026-08-17T10:01:00.000000Z",
    })
    inc_id = store.insert_incident({
        "session_file": "/s.jsonl", "session_id": "s1", "project_path": "/p",
        "ts": "2026-08-17T09:10:00Z", "signal_type": "correction",
        "matched_text": "no, wrong", "window": [{"role": "human", "ts": "x", "text": "y"}],
        "score": 0.8, "run_id": scan_run,
    })
    lid = new_id()
    store.insert("learnings", {
        "id": lid, "rule_text": "**A rule**", "why": "w", "status": "proposed",
        "evidence_count": 1, "project_count": 1, "projects_json": "[]",
        # created during the MINE run's window
        "created_at": "2026-08-17T11:02:00.000000Z",
    })
    store.insert("incident_learnings", {"incident_id": inc_id, "learning_id": lid})
    store.commit()

    out = tmp_path / "r.md"
    generate(store, Config(state_dir=str(tmp_path / "st")), mine_run, out)
    body = _body_without_appendix(out.read_text())
    line = next(l for l in body.splitlines() if "Learnings (this run)" in l)
    assert "| 1 |" in line, f"the mining run must count its own learning: {line}"
    store.close()


def test_review_queue_does_not_say_empty_while_proposals_await_review(tmp_path):
    """A queue containing pending proposals must list them and state how many await review."""
    from self_improve.store import Store, new_id, utc_now_iso

    store = Store(tmp_path / "s.db")
    rid = new_id()
    store.insert("runs", {"id": rid, "started": utc_now_iso(), "finished": utc_now_iso(),
                          "status": "ok", "stats_json": "{}"})
    for _ in range(3):
        lid = new_id()
        store.insert("learnings", {
            "id": lid, "rule_text": "**R**", "why": "w", "status": "proposed",
            "evidence_count": 1, "project_count": 1, "projects_json": "[]",
            "created_at": utc_now_iso(),
        })
        store.insert("proposals", {
            "id": new_id(), "run_id": rid, "learning_id": lid,
            "target_path": "/p/AGENTS.md", "target_kind": "project_agents_md",
            "action": "add", "diff_unified": "--- a\n+++ b\n", "status": "pending",
            "created_at": utc_now_iso(),
        })
    store.commit()

    out = tmp_path / "r.md"
    generate(store, Config(state_dir=str(tmp_path / "st")), rid, out)
    body = _body_without_appendix(out.read_text())
    section = body.split("## Held / review queue")[1].split("## ")[0]
    assert "Empty." not in section, f"said Empty with 3 pending:\n{section[:300]}"
    assert "3" in section
    store.close()


# ---------------------------------------------------------------------------
# The majority split must actually render
# ---------------------------------------------------------------------------
#
# A stat nobody renders is a stat nobody sees. The whole reason the gate became
# a majority is that one verdict was a coin flip, so a report that prints only
# the verdict word puts the problem straight back.


def _majority_stats(splits, **gate_extra):
    gate = {
        "attempted": len(splits), "gated_pass": 0, "gated_fail": 0,
        "ungated": 0, "inconclusive": 0, "failed": 0,
        "scenario_splits": splits,
    }
    gate.update(gate_extra)
    return {"gate": gate}


def test_the_per_scenario_split_appears_in_the_report(tmp_path):
    """Through the REAL render, not the helper.

    Sabotage: delete the `out.extend(_gate_majority_lines(stats))` line in
    report.py. Calling the helper directly cannot see that; this can.
    """
    text = _run_with_stats(
        tmp_path,
        _majority_stats(
            [
                {
                    "proposal_id": "abcdef1234",
                    "learning_id": "L1",
                    "verdict": "gated_pass",
                    "tally": {"gated_pass": 2, "gated_fail": 0,
                              "ungated": 1, "error": 0},
                    "scenarios_run": 3,
                }
            ],
            gated_pass=1,
        ),
    )
    assert "Gate scenarios" in text
    assert "abcdef12" in text
    assert "gated_pass" in text
    # the counts themselves, so 2-1 cannot read like 3-0
    assert "| 2 | 0 | 1 | 0 | 3 |" in text


def test_inconclusive_is_explained_rather_than_shown_as_a_bare_count(tmp_path):
    text = _run_with_stats(
        tmp_path,
        _majority_stats(
            [
                {
                    "proposal_id": "p1", "learning_id": "L1",
                    "verdict": "inconclusive",
                    "tally": {"gated_pass": 2, "gated_fail": 1,
                              "ungated": 0, "error": 0},
                    "scenarios_run": 3,
                }
            ],
            inconclusive=1,
        ),
    )
    assert "HELD for review" in text
    assert "did not establish a passing or failing majority" in text
    assert "tally distinguishes mixed evidence from errors" in text


def test_no_split_means_no_section_rather_than_an_empty_table():
    from self_improve.report import _gate_majority_lines

    assert _gate_majority_lines({"gate": {"attempted": 0}}) == []


def test_the_starved_gate_note_no_longer_blames_the_miner():
    """The pools were split; the old text said mining eats the gate's budget.

    Leaving that in would send the reader to raise max_cheap_calls_per_run,
    which changes how many incidents are MINED and not one thing about how many
    are gated.
    """
    from self_improve.report import _gate_starved_lines

    text = "\n".join(
        _gate_starved_lines(
            {"apply": {"taxonomy": {"gate_budget_exhausted": 12}}}
        )
    )
    assert "12 proposal(s) were held" in text
    assert "max_gate_calls_per_run" in text
    assert "share one" not in text, "stale claim: the pools are separate now"


# ---------------------------------------------------------------------------
# A failure count with no cause is unactionable
# ---------------------------------------------------------------------------


def test_malformed_lines_are_rendered_with_their_cause(tmp_path):
    text = _run_with_stats(
        tmp_path,
        {"scan": {"malformed_by_cause": {"malformed_json": 1200, "decode_error": 34}}},
    )
    assert "1,234 malformed line(s), by cause" in text
    assert "`malformed_json` 1,200" in text
    assert "`decode_error` 34" in text


def test_unknown_record_types_are_named_and_flagged_for_review(tmp_path):
    """The half that matters. An unknown type is skipped, which is right for a
    bookkeeping record and wrong the moment one carries human text. This block
    is the only thing that distinguishes those two cases."""
    text = _run_with_stats(
        tmp_path,
        {"scan": {"unknown_lines": 42, "unknown_line_types": {
            "atis-latch": 20, "bridge-session": 13,
            "history-suppression": 7, "frame-link": 2,
        }}},
    )
    assert "record type this parser does not" in text
    assert "`atis-latch` 20" in text
    assert "SKIPPED, not failed" in text
    assert "the miner is blind to it" in text


def test_a_clean_scan_renders_no_parser_block(tmp_path):
    text = _run_with_stats(tmp_path, {"scan": {"malformed_by_cause": {}}})
    assert "by cause" not in text


def test_deliberately_skipped_record_types_are_named_in_the_report(tmp_path):
    """Render known skip volume by record type so format changes remain visible."""
    text = _run_with_stats(
        tmp_path,
        {"scan": {"skipped_line_types": {"attachment": 2345, "atis-latch": 67}}},
    )
    assert "Skipped on purpose, by record type" in text
    assert "`attachment` 2,345" in text
    assert "the transcript format" in text


# ---------------------------------------------------------------------------
# An incident that disagrees with its own session about which repo it is
# ---------------------------------------------------------------------------
#
# Session/incident identity disagreement can inflate project_count, which one
# global-routing path uses as evidence. Routing alone does not grant execution
# permission; the separate execution policy controls instruction writes.


def _split_fixture(store, *, method, n=1, incident_key="github:42"):
    from self_improve.store import new_id, utc_now_iso

    for i in range(n):
        fp = f"{method}-{i}"
        store.upsert_session({
            "file_path": fp, "source": "claude", "session_id": fp,
            "project_path": "/repos/x", "first_ts": "", "last_ts": "",
            "headless": 0, "is_subagent": 0, "mtime": 0.0, "file_size": 0,
            "bytes_scanned": 0, "lines_scanned": 0, "malformed_lines": 0,
            "status": "ok", "error": "", "last_scanned_at": utc_now_iso(),
            "project_key": "remote:github.com/x", "project_display": "x",
            "project_key_method": method,
        })
        store.insert_incident({
            "id": new_id(), "session_file": fp, "session_id": fp,
            "project_path": "/repos/x", "ts": "2026-08-23T09:30:00Z",
            "signal_type": "correction", "matched_text": "x", "window": [],
            "project_key": incident_key,
        })
    store.commit()


def test_a_degraded_split_points_to_a_backfill_preview(tmp_path):
    """Sabotage: drop the _identity_split_lines call from generate()."""
    from self_improve.report import _identity_split_lines
    from self_improve.store import Store

    store = Store(tmp_path / "s.db")
    _split_fixture(store, method="remote_url", n=3)
    text = "\n".join(_identity_split_lines(store))
    assert "3 incident(s) disagree" in text
    assert "`remote_url` 3" in text
    assert "--requalify --dry-run" in text
    assert "reports exactly how many would change" in text
    store.close()


def test_a_split_on_an_already_best_method_is_not_sold_as_repairable(tmp_path):
    """The method label alone does not establish that a split can be repaired."""
    from self_improve.report import _identity_split_lines
    from self_improve.store import Store

    store = Store(tmp_path / "s.db")
    _split_fixture(store, method="gh_repo_id", n=2)
    text = "\n".join(_identity_split_lines(store))
    assert "2 incident(s) disagree" in text
    # No count is claimed as repairable, in either direction: only the dry run
    # knows, and an earlier version promised a fix that would not have worked.
    assert "can be re-resolved upward" not in text
    assert "does not establish that these keys are repairable" in text
    store.close()


def test_a_clean_database_gets_no_line_at_all(tmp_path):
    from self_improve.report import _identity_split_lines
    from self_improve.store import Store

    store = Store(tmp_path / "s.db")
    assert _identity_split_lines(store) == []
    store.close()


def test_the_check_runs_through_the_real_report(tmp_path):
    """Through generate(), not the helper — the call site is what regresses."""
    import json as _json

    from self_improve.report import generate
    from self_improve.store import Store, new_id, utc_now_iso

    db = tmp_path / "s.db"
    store = Store(db)
    _split_fixture(store, method="remote_url", n=4)
    rid = new_id()
    store.insert("runs", {"id": rid, "started": utc_now_iso()})
    store.update("runs", "id", rid, {
        "finished": utc_now_iso(), "status": "ok", "stats_json": _json.dumps({}),
    })
    store.commit()
    out = tmp_path / "r.md"
    generate(store, Config(), rid, out)
    text = out.read_text(encoding="utf-8")
    assert "4 incident(s) disagree" in text, "the check never reached the report"
    store.close()


def test_report_says_why_a_run_was_degraded(tmp_path):
    """A degraded report must identify the stage that failed.

    Removing the status_reasons loop must make this regression fail."""
    text = _run_with_stats(
        tmp_path,
        {"status_reasons": ["mine: 0 of 5 attempts succeeded (5 failed)"]},
    )
    assert "DEGRADED" in text
    assert "mine: 0 of 5 attempts succeeded (5 failed)" in text


def test_report_carries_no_degraded_note_on_a_healthy_run(tmp_path):
    """The complement: a clean run must not carry the alarm."""
    text = _run_with_stats(tmp_path, {"status_reasons": []})
    assert "DEGRADED" not in text


def _run_with_llm_calls(tmp_path, calls, stats=None):
    """A run plus llm_calls rows, so the funnel has something real to count."""
    from self_improve.store import Store, new_id, utc_now_iso

    store = Store(tmp_path / "s.db")
    rid = new_id()
    store.insert(
        "runs",
        {
            "id": rid, "started": utc_now_iso(), "finished": utc_now_iso(),
            "status": "ok", "stats_json": json.dumps(stats or {}),
        },
    )
    for stage, outcome, n in calls:
        for _ in range(n):
            store.insert(
                "llm_calls",
                {
                    "id": new_id(), "run_id": rid, "stage": stage,
                    "provider": "codex", "account": "codex",
                    "model_requested": "gpt-5.6-terra", "model_reported": "",
                    "prompt_sha": "x", "tokens_in": 0, "tokens_out": 0,
                    "duration_ms": 97, "outcome": outcome, "error": "",
                    "created_at": utc_now_iso(),
                },
            )
    store.commit()
    out = tmp_path / "r.md"
    generate(store, Config(state_dir=str(tmp_path / "state")), rid, out)
    return out.read_text()


def test_the_funnel_counts_the_agentic_miner(tmp_path):
    """The funnel must include mine_agentic calls as well as fast-mode mine calls."""
    text = _run_with_llm_calls(tmp_path, [("mine_agentic", "other", 80)])
    assert "| Mine calls attempted | 80 |" in text, text[:1500]
    assert "| Mine calls failed | 80 |" in text
    assert "| Mine calls succeeded | 0 |" in text


def test_the_funnel_still_counts_the_cheap_miner(tmp_path):
    """Narrowness guard: the non-agentic stage must not be dropped."""
    text = _run_with_llm_calls(tmp_path, [("mine", "ok", 12)])
    assert "| Mine calls attempted | 12 |" in text
    assert "| Mine calls succeeded | 12 |" in text


def test_both_miners_in_one_run_are_added_not_replaced(tmp_path):
    """A run can use both — the cheap miner then an agentic retry."""
    text = _run_with_llm_calls(
        tmp_path, [("mine", "ok", 5), ("mine_agentic", "parse_error", 3)]
    )
    assert "| Mine calls attempted | 8 |" in text
    assert "| Mine calls succeeded | 5 |" in text
    assert "| Mine calls failed | 3 |" in text


def test_no_llm_stage_is_left_out_of_the_report_ordering(tmp_path):
    """The drift guard behind all three.

    `report.STAGES` orders the taxonomy table and `MINE_STAGES` feeds the
    funnel. A stage name the code emits but neither list knows sorts to the
    end and is counted nowhere, which is how `mine_agentic` went missing.
    Read the stage names out of pipeline's source.
    """
    import re
    from pathlib import Path

    from self_improve import pipeline as pl, report as rp

    source = Path(pl.__file__).read_text(encoding="utf-8")
    emitted = set(re.findall(r'_llm_(?:agentic_)?json\(\s*\w+,\s*"([a-z_]+)"', source))
    emitted |= set(re.findall(r'llm\.call(?:_agentic)?\(\s*"([a-z_]+)"', source))
    assert emitted, "the scanner found no stage names; it has stopped working"
    known = set(rp.STAGES) | set(rp.MINE_STAGES)
    missing = sorted(emitted - known)
    assert not missing, (
        f"pipeline emits llm stages {missing} that report.STAGES does not "
        "know, so they sort last and are counted in no funnel"
    )


def test_a_regenerated_report_says_its_rows_have_moved(tmp_path):
    """A later scan can move session timestamps outside an old run's window.

    Regeneration must disclose that drift against recorded run stats."""
    from self_improve.report import _scan_drift_lines

    lines = _scan_drift_lines(
        {"scan": {"files_succeeded": 1000, "files_failed": 0}}, 990
    )
    text = " ".join(lines)
    assert "10 of the 1000 sessions" in text, text
    assert "last_scanned_at" in text
    assert "NOW" in text


def test_a_report_generated_at_run_time_carries_no_drift_note(tmp_path):
    """Narrowness guard. The note must not appear on every report."""
    from self_improve.report import _scan_drift_lines

    assert _scan_drift_lines({"scan": {"files_succeeded": 990, "files_failed": 10}}, 1000) == []
    assert _scan_drift_lines({"scan": {"files_succeeded": 0, "files_failed": 0}}, 0) == []
    # A run with no scan stats at all is not accused of anything.
    assert _scan_drift_lines({}, 42) == []
    assert _scan_drift_lines({"scan": {"files_succeeded": "many"}}, 42) == []
    # The discriminating case: half a triple. Reading the missing half as zero
    # makes `expected` 1000 and emits "10 sessions have moved" — a specific
    # claim built on a number nobody recorded. The real expected total is
    # unknown, so the honest output is nothing at all.
    assert _scan_drift_lines(
        {"scan": {"files_succeeded": 1000, "files_failed": "unknown"}}, 990
    ) == [], "a missing counter was read as zero and produced a made-up count"


def test_the_drift_note_reaches_the_report(tmp_path):
    """The wiring, not the function."""
    text = _run_with_stats(
        tmp_path, {"scan": {"files_succeeded": 7, "files_failed": 0}}
    )
    # No sessions exist in the fixture, so 7 recorded vs 0 in scope.
    assert "7 of the 7 sessions this run scanned have since been re-scanned" in text


def test_the_funnel_reports_this_runs_lines_not_the_sessions_lifetimes(tmp_path):
    """Use the run's recorded counters with distinct session lifetimes present."""
    store = seed(tmp_path)
    stats = {"scan": {"files_succeeded": 3, "files_failed": 0,
                      "lines_scanned": 137, "malformed_lines": 1}}
    store.conn.execute("UPDATE runs SET stats_json = ? WHERE id = ?",
                       (json.dumps(stats), RUN))
    store.commit()
    out = tmp_path / "r.md"
    try:
        generate(store, Config(state_dir=str(tmp_path / "state")), RUN, out)
    finally:
        store.close()
    text = _body_without_appendix(out.read_text())
    funnel = text.split("## Funnel", 1)[1].split("\n## ", 1)[0]
    assert "| Lines scanned (this run) | 137 |" in funnel
    assert "| Malformed lines (this run) | 1 |" in funnel
    assert "| Lines scanned (this run) | 150 |" not in funnel
    assert "Lines scanned, cumulative over this run's sessions: 150 " in text


def test_a_missing_scan_counter_is_stated_not_zeroed(tmp_path):
    """A counter nobody recorded and a counter that is zero are different
    facts, and a funnel row is where that difference disappears."""
    text = _run_with_stats(tmp_path, {"scan": {"files_succeeded": 0, "files_failed": 0}})
    assert "| Lines scanned (this run) | not recorded |" in text
    assert "| Lines scanned (this run) | 0 |" not in text


def test_the_report_preserves_an_explicit_review_only_veto(tmp_path):
    cfg = Config(state_dir=str(tmp_path / "state"), auto_apply=True)
    text = _run_with_stats(tmp_path, {"review_only": True}, cfg)
    assert "Apply posture" in text
    assert "--review-only; this run authorizes no automatic instruction edits" in text
    assert "Current automatic classes (run policy was not recorded)**: none" in text


def test_the_shipped_default_reports_all_automatic_classes_off(tmp_path):
    text = _run_with_stats(tmp_path, {"review_only": False})
    assert "Current automatic classes (run policy was not recorded)**: none" in text
    assert "requires a passing gate and an enabled target class" in text


def test_a_dry_run_never_claims_instruction_file_writes(tmp_path):
    """A dry run must not claim instruction-target writes.

    The pipeline can still write its state database; this assertion concerns target files."""
    text = _run_with_stats(tmp_path, {"dry_run": True, "review_only": False})
    assert "written to instruction files" not in text
    assert "dry run" in text.lower()


def test_the_report_does_not_infer_actual_writes_from_config(tmp_path):
    cfg = Config(state_dir=str(tmp_path / "state"), auto_apply=True)
    text = _run_with_stats(tmp_path, {"review_only": False}, cfg)
    assert "written to instruction files" not in text
    assert "Current automatic classes (run policy was not recorded)**: none" in text


def test_every_narrative_section_is_actually_wired_into_the_report():
    """Check that generate calls each narrative helper, not just that helpers work."""
    import re
    from pathlib import Path

    from self_improve import report as rp

    source = Path(rp.__file__).read_text(encoding="utf-8")
    helpers = re.findall(r"^def (_\w+_lines)\(", source, re.M)
    assert len(helpers) >= 8, f"only found {helpers}; the scanner is broken"

    start = source.index("\ndef generate(")
    nxt = re.search(r"\n(?:def|class) ", source[start + 1 :])
    body = source[start : start + 1 + nxt.start()] if nxt else source[start:]
    assert "out.append" in body, "generate's body was not isolated correctly"

    unwired = [h for h in helpers if f"{h}(" not in body]
    assert not unwired, (
        f"report.py defines {unwired} but generate() never calls them, so the "
        "section silently never appears"
    )


def test_the_two_malformed_numbers_say_which_is_which(tmp_path):
    """Distinguish this run's malformed count from lifetime totals over its selected sessions."""
    text = _run_with_stats(
        tmp_path,
        {"scan": {"files_succeeded": 0, "files_failed": 0,
                  "lines_scanned": 120, "malformed_lines": 0}},
    )
    assert "| Malformed lines (this run) | 0 |" in text
    assert "cumulative over this run's sessions (counted, skipped, surfaced)" in text
    assert "this run itself saw 0" in text


def test_the_funnel_separates_incidents_found_from_incidents_mined(tmp_path):
    """A scanning run's incident rows do not identify which run later mined them.
    Label those rows precisely and report the current run's mining counter separately.
    """
    text = _run_with_stats(
        tmp_path,
        {"mine": {"attempted": 9, "succeeded": 7, "failed": 2},
         "scan": {"files_succeeded": 0, "files_failed": 0}},
    )
    assert "| Incidents THIS RUN mined | 7 |" in text, (
        [ln for ln in text.splitlines() if "mined" in ln]
    )
    assert "| — of those, now mined | 0 |" in text
    assert "| Incidents mined |" not in text


def test_a_missing_mine_counter_is_stated_not_zeroed(tmp_path):
    text = _run_with_stats(tmp_path, {"scan": {"files_succeeded": 0, "files_failed": 0}})
    assert "| Incidents THIS RUN mined | not recorded |" in text


def test_the_of_those_rows_sit_under_the_incidents_they_describe(tmp_path):
    """Row order is meaning in a two-column table.

    Placing "— of those, now mined" after the mine-call rows made it read as
    a subdivision of "Mine calls failed", eight rows from the "Incidents (this
    run)" it actually qualifies.
    """
    text = _run_with_stats(
        tmp_path,
        {"mine": {"attempted": 9, "succeeded": 7, "failed": 2},
         "scan": {"files_succeeded": 0, "files_failed": 0}},
    )
    lines = [ln for ln in text.splitlines() if ln.startswith("| ")]
    def idx(label):
        for i, ln in enumerate(lines):
            if ln.startswith(f"| {label} "):
                return i
        raise AssertionError(f"{label!r} not in the funnel: {lines}")

    assert idx("— of those, still new") < idx("Mine calls attempted"), (
        "the 'of those' rows must precede the mine-call rows"
    )
    assert idx("Incidents (this run)") < idx("— of those, now mined")
    assert idx("Mine calls failed") < idx("Incidents THIS RUN mined")



def test_the_report_counts_every_status_that_awaits_a_decision(tmp_path):
    """Inconclusive proposals await a decision and must appear in the report."""
    from self_improve.store import Store, new_id, utc_now_iso

    store = Store(tmp_path / "s.db")
    rid = new_id()
    store.insert("runs", {"id": rid, "started": utc_now_iso(), "finished": utc_now_iso(),
                          "status": "ok", "stats_json": "{}"})
    for status in ("inconclusive", "inconclusive", "inconclusive", "inconclusive", "pending"):
        lid = new_id()
        store.insert("learnings", {
            "id": lid, "rule_text": "**R**", "why": "w", "status": "proposed",
            "evidence_count": 1, "project_count": 1, "projects_json": "[]",
            "created_at": utc_now_iso(),
        })
        store.insert("proposals", {
            "id": new_id(), "run_id": rid, "learning_id": lid,
            "target_path": "/p/AGENTS.md", "target_kind": "project_agents_md",
            "action": "add", "diff_unified": "--- a\n+++ b\n", "status": status,
            "created_at": utc_now_iso(),
        })
    store.commit()

    out = tmp_path / "r.md"
    generate(store, Config(state_dir=str(tmp_path / "st")), rid, out)
    body = _body_without_appendix(out.read_text())
    section = body.split("## Held / review queue")[1].split("## ")[0]
    assert "Empty." not in section, section
    assert "inconclusive" in section, (
        f"the fourth verdict is invisible in the section that drives review:\n{section}"
    )
    assert "5" in section, f"expected all five awaiting decisions counted:\n{section}"
    store.close()


def test_wall_clock_says_nothing_alarming_when_there_were_no_model_calls(tmp_path):
    """No-model runs must not present a necessarily 100% unaccounted time as an alarm."""
    from self_improve.store import Store, new_id, utc_now_iso

    store = Store(tmp_path / "s.db")
    rid = new_id()
    stats = {
        "wall_clock": {
            "wall_seconds": 103.0, "model_seconds": 0.0, "calls": 0,
            "unaccounted_seconds": 103.0, "unaccounted_pct": 100.0,
            "largest_gap_seconds": 0.0, "largest_gap_before_call": "",
        }
    }
    store.insert("runs", {"id": rid, "started": utc_now_iso(),
                          "finished": utc_now_iso(), "status": "ok",
                          "stats_json": json.dumps(stats), "report_path": ""})
    store.commit()
    out = tmp_path / "r.md"
    generate(store, Config(state_dir=str(tmp_path / "st")), rid, out)
    section = _body_without_appendix(out.read_text())
    section = section.split("## Wall clock")[1].split("\n## ")[0]
    assert "100.0%" not in section, f"reported a vacuous percentage:\n{section}"
    assert "no model calls" in section, f"did not say why there is nothing to compare:\n{section}"
    assert "Wall 0.03 h" in section, section
    store.close()


def test_wall_clock_still_reports_the_percentage_when_calls_were_made(tmp_path):
    """An invented long run with model calls reports its unaccounted share."""
    from self_improve.store import Store, new_id, utc_now_iso

    store = Store(tmp_path / "s.db")
    rid = new_id()
    stats = {
        "wall_clock": {
            "wall_seconds": 72000.0, "model_seconds": 18000.0, "calls": 4,
            "unaccounted_seconds": 54000.0, "unaccounted_pct": 75.0,
            "largest_gap_seconds": 36000.0, "largest_gap_before_call": "mine#3",
        }
    }
    store.insert("runs", {"id": rid, "started": utc_now_iso(),
                          "finished": utc_now_iso(), "status": "ok",
                          "stats_json": json.dumps(stats), "report_path": ""})
    store.commit()
    out = tmp_path / "r.md"
    generate(store, Config(state_dir=str(tmp_path / "st")), rid, out)
    section = _body_without_appendix(out.read_text())
    section = section.split("## Wall clock")[1].split("\n## ")[0]
    assert "75.0%" in section, section
    assert "exceeded 12 h" in section, section
    store.close()


def test_the_project_identity_warning_has_its_own_heading(tmp_path):
    """Project-identity warnings need their own section rather than a wall-clock heading."""
    from self_improve.store import Store, new_id, utc_now_iso

    store = Store(tmp_path / "s.db")
    rid = new_id()
    store.insert("runs", {"id": rid, "started": utc_now_iso(), "finished": utc_now_iso(),
                          "status": "ok", "stats_json": "{}", "report_path": ""})
    store.upsert_session({
        "file_path": "s1.jsonl", "source": "claude", "session_id": "s1",
        "project_path": "/repo", "project_key": "github:1", "project_display": "",
        "project_key_method": "path", "headless": 0, "is_subagent": 0,
        "first_ts": "2026-08-10T00:00:00Z", "last_ts": "2026-08-10T01:00:00Z",
        "mtime": 0.0, "file_size": 1, "bytes_scanned": 1, "lines_scanned": 10,
        "malformed_lines": 0, "status": "ok", "error": "",
        "last_scanned_at": utc_now_iso(), "malformed_by_cause": "{}",
    })
    store.insert_incident({
        "session_file": "s1.jsonl", "project_key": "github:DIFFERENT",
        "signal_type": "repeated_error", "matched_text": "x",
        "ts": "2026-08-10T00:30:00Z", "window_json": "[]", "status": "new",
        "project_path": "/repo", "created_at": utc_now_iso(),
    })
    store.commit()

    out = tmp_path / "r.md"
    generate(store, Config(state_dir=str(tmp_path / "st")), rid, out)
    body = _body_without_appendix(out.read_text())
    assert "disagree with their own session" in body, "the warning did not render at all"
    wall = body.split("## Wall clock")[1].split("\n## ")[0] if "## Wall clock" in body else ""
    assert "disagree with their own session" not in wall, (
        f"the project-identity warning is filed under Wall clock:\n{wall[:400]}"
    )
    assert "## Incidents that disagree with their session" in body, body[:200]
    # And it must not collide with the existing `### Project identity`
    # subsection under the error taxonomy, which is a different thing.
    assert body.count("# Project identity") <= 1, (
        "two headings named 'Project identity' in one report"
    )
    store.close()


def _run_with_truncated_windows(tmp_path, marker_count: int):
    from self_improve.store import Store, new_id, utc_now_iso

    store = Store(tmp_path / "s.db")
    rid = new_id()
    store.insert("runs", {"id": rid, "started": utc_now_iso(), "finished": utc_now_iso(),
                          "status": "ok", "stats_json": "{}", "report_path": ""})
    store.upsert_session({
        "file_path": "s1.jsonl", "source": "claude", "session_id": "s1",
        "project_path": "/repo", "project_key": "github:1", "project_display": "",
        "project_key_method": "gh_repo_id", "headless": 0, "is_subagent": 0,
        "first_ts": "2026-08-10T00:00:00Z", "last_ts": "2026-08-10T01:00:00Z",
        "mtime": 0.0, "file_size": 1, "bytes_scanned": 1, "lines_scanned": 10,
        "malformed_lines": 0, "status": "ok", "error": "",
        "last_scanned_at": utc_now_iso(), "malformed_by_cause": "{}",
    })
    # `insert_incident` reads `window` (a list) and serialises it into the
    # `window_json` column. Passing `window_json` is silently ignored and
    # stores `[]` — which is how the first version of this test "reproduced"
    # zero markers against correct code.
    window = [{"role": "user", "ts": "t", "text": "[truncated 40 chars]"}]
    for _ in range(marker_count):
        store.insert_incident({
            "session_file": "s1.jsonl", "project_key": "github:1",
            "signal_type": "frustration", "matched_text": "x",
            "ts": "2026-08-10T00:30:00Z", "window": window, "status": "new",
            "project_path": "/repo", "created_at": utc_now_iso(), "run_id": rid,
        })
    store.commit()
    out = tmp_path / "r.md"
    generate(store, Config(state_dir=str(tmp_path / "st")), rid, out)
    body = _body_without_appendix(out.read_text())
    store.close()
    return body.split("## Caps that bit")[1].split("\n## ")[0]


def test_the_caps_line_does_not_deny_the_truncation_it_just_reported(tmp_path):
    """Count run-scoped window truncation even when other cap counters are zero."""
    section = _run_with_truncated_windows(tmp_path, 3)
    assert "markers inside incident windows: 3" in section, section
    assert "No caps bit this run" not in section, (
        f"denied the truncation it just reported:\n{section}"
    )


def test_the_caps_line_still_says_nothing_bit_when_nothing_did(tmp_path):
    """The complement: with no truncation and no counters, say so plainly."""
    section = _run_with_truncated_windows(tmp_path, 0)
    assert "markers inside incident windows: 0" in section, section
    assert "No caps bit this run" in section, section


def test_the_report_warns_when_ungated_review_has_no_eval_evidence(tmp_path):
    """An absent trial stays visible even though ungated no longer auto-applies."""
    from self_improve.store import Store, new_id, utc_now_iso

    store = Store(tmp_path / "s.db")
    rid = new_id()
    store.insert("runs", {"id": rid, "started": utc_now_iso(), "finished": utc_now_iso(),
                          "status": "ok", "stats_json": "{}", "report_path": ""})
    lid = new_id()
    store.insert("learnings", {
        "id": lid, "title": "t", "rule_text": "r", "why": "w", "category": "c",
        "scope": "global", "evidence_count": 1, "project_count": 1,
        "projects_json": "[]", "first_seen": utc_now_iso(), "last_seen": utc_now_iso(),
        "confidence": 0.5, "status": "proposed", "duplicate_of": "",
        "violated_existing_rule": "", "created_at": utc_now_iso(),
        "primary_project_path": "",
    })
    hollow = new_id()
    store.insert("eval_results", {
        "id": hollow, "kind": "regression", "subject_id": lid,
        "started": utc_now_iso(), "finished": utc_now_iso(),
        "attempted": 3, "succeeded": 0, "failed": 3,
        "error_taxonomy_json": '{"agent_error": 3}', "metrics_json": "{}",
        "verdict": "ungated",
    })
    store.insert("proposals", {
        "id": new_id(), "learning_id": lid, "run_id": rid, "action": "add",
        "target_path": "/tmp/x/CLAUDE.md", "target_kind": "global_claude_md",
        "diff_unified": "", "status": "ungated", "eval_result_id": hollow,
        "applied_at": "", "snapshot_commit_before": "", "snapshot_commit_after": "",
        "created_at": utc_now_iso(),
    })
    store.commit()

    out = tmp_path / "r.md"
    generate(store, Config(state_dir=str(tmp_path / "st")), rid, out)
    body = _body_without_appendix(out.read_text())
    assert "no trial ran" in body, body[:1200]
    assert "1 of 1" in body, body[:1200]
    assert "Ungated proposals require human review" in body
    assert "and auto-applies" not in body
    # D4(a): a run report must not present a corpus-wide number as this run's.
    assert "Across the whole backlog" in body, body[:1200]
    # Same phrase as the global-routing line: one scope caveat, one wording.
    assert "Across the whole backlog, not this run" in body, body[:1200]
    store.close()


def test_the_report_stays_quiet_when_every_auto_appliable_eval_ran(tmp_path):
    """Do not print a warning that is always there."""
    from self_improve.store import Store, new_id, utc_now_iso

    store = Store(tmp_path / "s.db")
    rid = new_id()
    store.insert("runs", {"id": rid, "started": utc_now_iso(), "finished": utc_now_iso(),
                          "status": "ok", "stats_json": "{}", "report_path": ""})
    store.commit()
    out = tmp_path / "r.md"
    generate(store, Config(state_dir=str(tmp_path / "st")), rid, out)
    body = _body_without_appendix(out.read_text())
    assert "no trial ran" not in body, body[:600]
    store.close()


def test_a_recovered_parse_counts_as_a_successful_call(tmp_path):
    """parse_recovered and oauth_transient_retried are SUCCESSES.

    Through report.generate, not the helper: the funnel is what the operator
    reads, and the definition of "succeeded" used to be an inline sum here
    with two more copies elsewhere.
    """
    text = _run_with_llm_calls(
        tmp_path,
        [("mine", "ok", 3), ("mine", "parse_recovered", 2),
         ("mine", "oauth_transient_retried", 1), ("mine", "parse_error", 4)],
    )
    assert "| Mine calls attempted | 10 |" in text
    assert "| Mine calls succeeded | 6 |" in text
    assert "| Mine calls failed | 4 |" in text


def test_one_definition_of_a_successful_llm_call():
    """Three copies of this list existed; a fourth was about to be written.

    `llm.OUTCOMES` is the vocabulary, `report` summed three names inline for
    its funnel, and `dashboard.queries` kept its own tuple. Nothing read two
    of them together, which is the shape AGENTS.md records ten instances of.
    """
    from self_improve.store import LLM_SUCCESS_OUTCOMES
    from self_improve.llm import OUTCOMES
    from self_improve.dashboard import queries

    assert set(LLM_SUCCESS_OUTCOMES) <= set(OUTCOMES), (
        "a success outcome the call path cannot produce: "
        f"{sorted(set(LLM_SUCCESS_OUTCOMES) - set(OUTCOMES))}"
    )
    assert queries.LLM_SUCCESS_OUTCOMES is LLM_SUCCESS_OUTCOMES, (
        "dashboard.queries must reuse the canonical tuple, not redefine it"
    )
    import pathlib
    import self_improve.report as report_mod

    src = pathlib.Path(report_mod.__file__).read_text()
    assert '"oauth_transient_retried", 0)' not in src, (
        "report.py is summing the success outcomes inline again"
    )


def test_unnamed_failures_are_surfaced_as_a_possible_wording_drift(tmp_path):
    """Invented counts expose unclassified failures and their next diagnostic."""
    text = _run_with_llm_calls(
        tmp_path,
        [("mine_agentic", "other", 3), ("mine_agentic", "parse_error", 2),
         ("mine_agentic", "ok", 4)],
    )
    assert "3 of 5" in text, text[:2000]
    assert "_QUOTA_PATTERNS" in text


def test_a_run_with_every_failure_named_says_nothing(tmp_path):
    """Narrowness guard: no `other`, no line. A clean run stays quiet."""
    text = _run_with_llm_calls(
        tmp_path, [("mine", "ok", 5), ("mine", "parse_error", 2)]
    )
    assert "could not be named" not in text
    assert "_QUOTA_PATTERNS" not in text


def test_a_starved_contradiction_stage_says_so_and_names_the_pool(tmp_path):
    """Name budget refusal and the shared pool, rather than imply no work existed."""
    text = _run_with_llm_calls(
        tmp_path, [("mine_agentic", "ok", 3)],
        stats={"contradictions": {"skipped": "budget_exhausted"}},
    )
    assert "Contradiction detection did not run" in text, text[:1500]
    assert "max_cheap_calls_per_run" in text
    assert "does not reserve calls for contradiction detection" in text


def test_a_contradiction_stage_that_ran_says_nothing(tmp_path):
    """Narrowness guard: only the starved case gets the note."""
    text = _run_with_llm_calls(
        tmp_path, [("mine_agentic", "ok", 3)],
        stats={"contradictions": {"judged": 4, "deferred": 0}},
    )
    assert "Contradiction detection did not run" not in text


def test_a_run_that_never_reached_the_stage_says_nothing(tmp_path):
    """An absent stage does not establish that its budget was exhausted."""
    text = _run_with_llm_calls(tmp_path, [("mine_agentic", "ok", 3)], stats={})
    assert "Contradiction detection did not run" not in text


def _global_routing_report(tmp_path, learnings):
    """A run plus learnings/proposals targeting the global file."""
    import json as _json
    from self_improve.store import Store, new_id, utc_now_iso
    store = Store(tmp_path / "s.db")
    rid = new_id()
    store.insert("runs", {"id": rid, "started": utc_now_iso(), "finished": utc_now_iso(),
                          "status": "ok", "stats_json": _json.dumps({})})
    for scope, pcount, n in learnings:
        for _ in range(n):
            lid = new_id()
            store.insert("learnings", {
                "id": lid, "rule_text": "r", "why": "w", "category": "c",
                "scope": scope, "evidence_count": 1, "project_count": pcount,
                "projects_json": "[]", "first_seen": "", "last_seen": "",
                "confidence": 0.5, "status": "proposed", "duplicate_of": "",
                "created_at": utc_now_iso()})
            store.insert("proposals", {
                "id": new_id(), "learning_id": lid, "target_path": "/g/CLAUDE.md",
                "target_kind": "global_claude_md", "action": "add",
                "created_at": utc_now_iso()})
    store.commit()
    out = tmp_path / "r.md"
    generate(store, Config(state_dir=str(tmp_path / "state")), rid, out)
    return out.read_text()


def test_the_report_compares_current_global_project_counts_without_inventing_authorization(tmp_path):
    """Current counts are not historical evidence of authorization."""
    text = _global_routing_report(tmp_path, [("global", 1, 7), ("global", 5, 2)])
    assert "7 of 9" in text, text[:2500]
    assert "scope_guess" in text
    assert "Current counts do not establish which branch originally selected" in text
    assert "does not grant execution permission" in text


def test_no_global_proposals_means_no_note(tmp_path):
    """Narrowness guard: nothing targeting the global file, nothing said."""
    text = _global_routing_report(tmp_path, [])
    assert "scope_guess" not in text


def _coverage_report(tmp_path, incidents):
    """A run plus incidents at given (signal, status) so coverage is real."""
    import json as _json
    from self_improve.store import Store, new_id, utc_now_iso
    store = Store(tmp_path / "s.db")
    rid = new_id()
    store.insert("runs", {"id": rid, "started": utc_now_iso(), "finished": utc_now_iso(),
                          "status": "ok", "stats_json": _json.dumps({})})
    seq = 0
    for signal, status, n in incidents:
        for _ in range(n):
            seq += 1
            iid = new_id()
            # Distinct matched_text and session_file: incidents are
            # de-duplicated, and identical rows collide on the unique index.
            # The session must exist first — incidents carry a foreign key.
            store.upsert_session({
                "file_path": f"/f{seq}.jsonl", "source": "claude",
                "session_id": f"s{seq}", "project_path": "/p", "headless": 0,
                "is_subagent": 0, "first_ts": "", "last_ts": "", "mtime": 1.0,
                "file_size": 10, "bytes_scanned": 10, "lines_scanned": 1,
                "malformed_lines": 0, "status": "ok", "error": "",
                "last_scanned_at": utc_now_iso()})
            store.insert_incident({
                "id": iid, "session_file": f"/f{seq}.jsonl", "session_id": f"s{seq}",
                "project_path": "/p", "ts": "2026-08-10T00:00:00Z",
                "signal_type": signal, "matched_text": f"m{seq}",
                "window": [{"role": "user", "text": f"a{seq}"}]})
            if status != "new":
                store.conn.execute("UPDATE incidents SET status = ? WHERE id = ?", (status, iid))
    store.commit()
    out = tmp_path / "r.md"
    generate(store, Config(state_dir=str(tmp_path / "state")), rid, out)
    return out.read_text()


def test_a_signal_the_miner_has_never_reached_is_named(tmp_path):
    """Name signals whose incidents have not reached mining under the selected policy."""
    text = _coverage_report(tmp_path, [
        ("instruction_edit", "mined", 8),
        ("self_observation", "new", 20),
        ("frustration", "new", 12),
    ])
    assert "self_observation" in text, text[:2500]
    assert "frustration" in text
    assert "never" in text.lower()


def test_full_coverage_says_nothing(tmp_path):
    """Narrowness guard: every signal reached, no note."""
    text = _coverage_report(tmp_path, [
        ("instruction_edit", "mined", 4),
        ("frustration", "mined", 3),
    ])
    assert "never been mined" not in text


def test_a_contradiction_run_that_hit_its_cap_says_what_it_deferred(tmp_path):
    """A capped contradiction pass must state how many pairs it deferred."""
    text = _run_with_llm_calls(
        tmp_path, [("mine", "ok", 2)],
        stats={"contradictions": {"judged": 5, "judge_failed": 0, "deferred": 12}},
    )
    # A distinctive phrase, not the bare word: the raw stats_json appendix
    # dumps every counter, so "deferred" appears whatever the report says.
    # Asserting on it would pass with no narrative line at all.
    assert "12 pair(s) deferred" in text, text[:2000]


def test_a_contradiction_run_within_its_cap_says_nothing_about_deferral(tmp_path):
    """Narrowness guard: nothing deferred, no note."""
    text = _run_with_llm_calls(
        tmp_path, [("mine", "ok", 2)],
        stats={"contradictions": {"judged": 4, "judge_failed": 0, "deferred": 0}},
    )
    assert "pair(s) deferred" not in text


def test_a_malformed_counter_does_not_crash_the_whole_report(tmp_path):
    """`stats_json` is our own strict-JSON column, and `int()` on it can raise.

    AGENTS.md's rule names this exact construct: "check what happens ABOVE it:
    a `json.loads`, an `int()`, a `[0]` index on a list you did not measure."
    I wrote `int(con.get("deferred") or 0)` while fixing that class elsewhere
    the same night. A counter that is not a number raised ValueError out of
    report.generate, so one malformed value took the ENTIRE report with it —
    including the sections that had nothing to do with contradictions.
    """
    text = _run_with_llm_calls(
        tmp_path, [("mine", "ok", 2)],
        stats={"contradictions": {"deferred": "lots", "judged": 2}},
    )
    assert "## Funnel" in text, "a bad counter destroyed the report"
    assert "unreadable" in text.lower() or "pair(s) deferred" not in text


def test_the_global_routing_threshold_never_silently_defaults(tmp_path):
    """A missing config key raises here, as it does everywhere else.

    `_hollow_eval_lines`'s sibling reads the same class of value with
    `_cfg_attr`, which raises. This one used `getattr(cfg, name, 3)`, so a
    renamed key would quietly change which proposals count as evidence-backed
    — the "missing config keys raise" rule, broken in the module that reports
    on it.
    """
    import pytest
    from self_improve.report import _global_routing_lines

    class NoThreshold:
        pass

    with pytest.raises(Exception) as exc:
        _global_routing_lines(_StoreWithGlobalProposals(), NoThreshold())
    assert "global_promotion_min_projects" in str(exc.value)


class _StoreWithGlobalProposals:
    """Minimal store stub: one global proposal, so the helper gets past its
    early return and actually reaches the config read."""

    def query(self, sql, params=()):
        return [{"pc": 1}]


def test_the_held_section_matches_review_for_ungated_and_manual_actions(tmp_path):
    """Render every waiting proposal and exclude an eligible automatic delivery."""
    from self_improve.dashboard.queries import waiting_proposal_ids
    from self_improve.execution_policy import set_class_policy
    from self_improve.store import Store

    store = Store(tmp_path / "s.db")
    cfg = Config(state_dir=str(tmp_path / "state"))
    now = "2026-01-02T00:00:00Z"
    store.insert("runs", {"id": "run", "started": now, "finished": now,
                          "status": "ok", "stats_json": "{}"})
    store.insert("learnings", {
        "id": "learning", "rule_text": "An invented review rule.",
        "why": "An invented reason.", "category": "testing", "scope": "global",
        "evidence_count": 1, "project_count": 1, "projects_json": "[]",
        "first_seen": "", "last_seen": "", "confidence": 0.5, "status": "proposed",
        "duplicate_of": "", "created_at": now})
    store.commit()
    set_class_policy(store, "global", True, now="2026-01-01T00:00:00Z")
    proposals = (
        ("pending", "pending", "add"),
        ("manual-hook", "gated_pass", "convert_to_hook"),
        ("ungated", "ungated", "add"),
        ("automatic", "gated_pass", "add"),
    )
    for pid, status, action in proposals:
        store.insert("proposals", {
            "id": pid, "learning_id": "learning", "run_id": "run",
            "target_path": f"/invented/{pid}.md", "target_kind": "global_claude_md",
            "action": action, "status": status, "created_at": now})
    store.commit()
    out = tmp_path / "r.md"
    try:
        assert set(waiting_proposal_ids(store, cfg)) == {"pending", "manual-hook", "ungated"}
        generate(store, cfg, "run", out)
    finally:
        store.close()
    section = out.read_text().split("## Held / review queue", 1)[1].split("\n## ", 1)[0]
    rows = [line for line in section.splitlines() if line.startswith("- [")]
    assert rows == [
        "- [gated_pass] `/invented/manual-hook.md` (convert_to_hook): An invented review rule.",
        "- [pending] `/invented/pending.md` (add): An invented review rule.",
        "- [ungated] `/invented/ungated.md` (add): An invented review rule.",
    ]
    assert "/invented/automatic.md" not in section
    assert "**3 proposal(s) await your decision** (1 not yet gated)." in section
