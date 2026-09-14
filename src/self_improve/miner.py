"""Incident mining: turn one filtered incident window into a candidate learning.

One cheap-model LLM call per incident. The prompt is built from
``prompts/mine_incident.md`` plus the instruction files in force for the
incident's project; the response must be the strict JSON contract described in
that template. Fail-loud policy throughout:

- Template rendering raises on any placeholder/mapping mismatch — a typo'd
  ``{{placeholder}}`` never reaches an LLM.
- An LLM parse failure (``llm_call`` returns None) or a response that violates
  the JSON contract in any field is a FAILED call: the incident stays
  ``status='new'`` (retryable next run) with the reason logged; nothing is
  guessed or defaulted. The call-level failure taxonomy lives in ``llm_calls``
  (written by the llm layer, not here).
- Malformed ``window_json`` in our own DB is a broken invariant and raises.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from pathlib import Path

from .config import Config
from .redact import redact_text
from .render import render_environment, render_session
from .sources.base import TurnEvent
from .sources.claude_code import ClaudeCodeSource
from .sources.codex import CodexSource
from .store import Store, new_id, utc_now_iso

logger = logging.getLogger(__name__)

# Turns held back from exploration so the agent can still write its final JSON
# after it stops looking. A truncated agentic session yields NOTHING, so the
# reserve is deliberately generous.
TURN_RESERVE = 6

# Marker prepended to a fallback transcript.md built from window_json because
# the full transcript aged out (see mine_incident_agentic).
AGED_OUT_NOTE = "# NOTE: full transcript aged out; redacted incident window only"

# {{key}} placeholders in prompt templates. Word-characters only, so JSON
# braces inside templates (single { }) never collide with the syntax.
PLACEHOLDER_RE = re.compile(r"\{\{([A-Za-z0-9_]+)\}\}")

# Per-file truncation for in-force instruction files fed into the mine prompt.
# Fixed by the miner contract (not a config knob); every cut is marked inline.
IN_FORCE_TRUNCATE_CHARS = 4000

# Legal values for the mine response's scope_guess field.
MINE_SCOPES = ("global", "project", "skill", "hook", "rule_path", "codex_global")

# Exact key set of the strict mine-response JSON contract (fast path).
MINE_JSON_KEYS = frozenset(
    {
        "is_real_learning",
        "incident_summary",
        "generalized_rule",
        "why",
        "scope_guess",
        "category",
        "duplicate_of_existing_rule",
        "confidence",
    }
)

# The agentic miner additionally decides dedup against the learnings DB
# (it has the search-learnings tool + learnings.jsonl in its sandbox), reports
# the enforcement gap (violated_existing_rule: an in-force rule that was
# written AND ignored — distinct from duplicate_of_existing_rule, which says
# the LESSON is already written), and may scope a rule to path globs
# (path_globs, required non-empty iff scope_guess == "rule_path").
MINE_DEDUP_DECISIONS = ("new", "duplicate", "amend")
MINE_AGENTIC_EXTRA_KEYS = frozenset(
    {
        "dedup_decision",
        "dedup_target_id",
        "amended_rule_text",
        "amended_why",
        "violated_existing_rule",
        "path_globs",
    }
)
MINE_AGENTIC_JSON_KEYS = MINE_JSON_KEYS | MINE_AGENTIC_EXTRA_KEYS

# These two response keys have documented empty defaults. Record each omitted
# key that uses its default; every other missing required key remains an error.
MINE_AGENTIC_OPTIONAL_DEFAULTS: dict[str, object] = {
    "violated_existing_rule": "",
    "path_globs": [],
}


def normalize_mine_payload(payload: dict, *, agentic: bool = False) -> tuple[dict, list[str]]:
    """Fill the two omissible keys with their empty defaults.

    Returns ``(payload, defaulted_key_names)``. The caller reports the names so
    the substitution stays visible in the run stats.
    """
    if not agentic or not isinstance(payload, dict):
        return payload, []
    defaulted = [k for k in MINE_AGENTIC_OPTIONAL_DEFAULTS if k not in payload]
    for k in defaulted:
        v = MINE_AGENTIC_OPTIONAL_DEFAULTS[k]
        payload[k] = list(v) if isinstance(v, list) else v
    return payload, sorted(defaulted)


class PromptRenderError(Exception):
    """Raised when a prompt template and its mapping disagree (fail loud)."""


class MineParseFailure(Exception):
    """LLM response was unparseable — the incident stays 'new' (retryable)."""


class MineContractViolation(Exception):
    """LLM response parsed but violated the mine JSON contract — stays 'new'."""


class MinerError(Exception):
    """Raised when a miner invariant is broken (e.g. malformed window_json in our DB)."""


class TranscriptAgedOut(MinerError):
    """The incident's full transcript file no longer exists on disk.

    Raised by :func:`build_mine_sandbox`. The caller
    (:func:`mine_incident_agentic`) catches exactly this and builds the
    sandbox from the incident's stored ``window_json`` instead — the window
    was persisted precisely so evidence survives transcript age-out.
    """


def render_prompt(template_path: str | Path, mapping: dict[str, str], *, template_text: str | None = None) -> str:
    """Render a prompt template by substituting ``{{key}}`` placeholders.

    Strict by design: the set of placeholders in the template must equal the
    set of mapping keys exactly. A placeholder with no mapping key (would be
    left in the prompt), a mapping key with no placeholder (silently dropped
    input), or a non-string mapping value all raise PromptRenderError.

    Substitution is a single pass over the template, so mapping values that
    themselves contain ``{{...}}``-looking text (e.g. code excerpts in a
    transcript window) are inserted literally and never re-expanded.
    """
    template = Path(template_path).read_text(encoding="utf-8") if template_text is None else template_text
    placeholders = set(PLACEHOLDER_RE.findall(template))
    missing = placeholders - mapping.keys()
    if missing:
        raise PromptRenderError(
            f"{template_path}: placeholders with no mapping key: {sorted(missing)}"
        )
    unused = mapping.keys() - placeholders
    if unused:
        raise PromptRenderError(
            f"{template_path}: mapping keys with no placeholder in template: {sorted(unused)}"
        )
    bad_types = sorted(k for k, v in mapping.items() if not isinstance(v, str))
    if bad_types:
        raise PromptRenderError(
            f"{template_path}: mapping values must be str, got non-str for: {bad_types}"
        )
    return PLACEHOLDER_RE.sub(lambda m: mapping[m.group(1)], template)


def render_prompt_revision(template_path, mapping):
    """Render and fingerprint the same captured template, even if its file changes."""
    import hashlib
    text=Path(template_path).read_text(encoding='utf-8')
    return render_prompt(template_path,mapping,template_text=text),hashlib.sha256(text.encode()).hexdigest()


def gather_in_force_instructions(project_path: str, cfg: Config) -> str:
    """Collect the instruction files in force for a project, labeled per file.

    Includes the global CLAUDE.md plus the project's AGENTS.md and CLAUDE.md.
    Each file is truncated to IN_FORCE_TRUNCATE_CHARS with an explicit
    ``[truncated N chars]`` marker recording exactly how much was cut. Missing
    files are noted as ABSENT (that is real information for the duplicate
    check, not an error); unreadable files are surfaced with the error rather
    than silently skipped or guessed at.
    """
    entries: list[tuple[str, Path]] = [("global CLAUDE.md", Path(cfg.global_claude_md))]
    blocks: list[str] = []
    if project_path:
        entries.append(("project AGENTS.md", Path(project_path) / "AGENTS.md"))
        entries.append(("project CLAUDE.md", Path(project_path) / "CLAUDE.md"))
    else:
        blocks.append(
            "(incident has no project path — project AGENTS.md/CLAUDE.md not resolvable)"
        )
    for label, path in entries:
        if not path.is_file():
            blocks.append(f"=== {label} ({path}) — ABSENT ===")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            blocks.append(f"=== {label} ({path}) — UNREADABLE: {exc} ===")
            continue
        if len(text) > IN_FORCE_TRUNCATE_CHARS:
            cut = len(text) - IN_FORCE_TRUNCATE_CHARS
            text = text[:IN_FORCE_TRUNCATE_CHARS] + f"\n[truncated {cut} chars]"
        blocks.append(f"=== {label} ({path}) ===\n{text}")
    return "\n\n".join(blocks)


def validate_mine_json(payload: object, *, agentic: bool = False) -> list[str]:
    """Validate a mine-call response against the strict JSON contract.

    Returns a list of human-readable violations; empty list means valid. Any
    violation is treated by mine_incident as a parse failure — never partially
    accepted. With ``agentic=True`` the contract additionally requires the
    dedup keys (MINE_AGENTIC_EXTRA_KEYS) with their cross-field constraints.
    """
    if not isinstance(payload, dict):
        return [f"payload is {type(payload).__name__}, not a JSON object"]
    expected = MINE_AGENTIC_JSON_KEYS if agentic else MINE_JSON_KEYS
    errors: list[str] = []
    missing = expected - payload.keys()
    if agentic:
        missing -= MINE_AGENTIC_OPTIONAL_DEFAULTS.keys()
    if missing:
        errors.append(f"missing keys: {sorted(missing)}")
    extra = payload.keys() - expected
    if extra:
        errors.append(f"unexpected keys: {sorted(extra)}")

    def _check_str(key: str) -> None:
        if key in payload and not isinstance(payload[key], str):
            errors.append(f"{key} must be a string, got {type(payload[key]).__name__}")

    if "is_real_learning" in payload and not isinstance(payload["is_real_learning"], bool):
        errors.append(
            "is_real_learning must be a boolean, got "
            f"{type(payload['is_real_learning']).__name__}"
        )
    for key in ("incident_summary", "generalized_rule", "why", "category"):
        _check_str(key)
    if "scope_guess" in payload and payload["scope_guess"] not in MINE_SCOPES:
        errors.append(f"scope_guess must be one of {MINE_SCOPES}, got {payload['scope_guess']!r}")
    dup = payload.get("duplicate_of_existing_rule")
    if "duplicate_of_existing_rule" in payload and dup is not None and not isinstance(dup, str):
        errors.append(
            f"duplicate_of_existing_rule must be a string or null, got {type(dup).__name__}"
        )
    conf = payload.get("confidence")
    if "confidence" in payload:
        if isinstance(conf, bool) or not isinstance(conf, (int, float)):
            errors.append(f"confidence must be a number, got {type(conf).__name__}")
        elif not 0 <= conf <= 1:
            errors.append(f"confidence must be in [0, 1], got {conf!r}")
    # When the model claims a real learning, the load-bearing strings must not
    # be empty — an empty rule is not a rule.
    if payload.get("is_real_learning") is True:
        for key in ("incident_summary", "generalized_rule", "why"):
            value = payload.get(key)
            if isinstance(value, str) and not value.strip():
                errors.append(f"{key} must be non-empty when is_real_learning is true")

    if agentic:
        for key in ("dedup_target_id", "amended_rule_text", "amended_why",
                    "violated_existing_rule"):
            _check_str(key)
        violated = payload.get("violated_existing_rule")
        if (
            payload.get("is_real_learning") is False
            and isinstance(violated, str)
            and violated.strip()
        ):
            errors.append(
                "violated_existing_rule must be '' when is_real_learning is false"
            )
        if "path_globs" in payload:
            globs = payload["path_globs"]
            if not isinstance(globs, list):
                errors.append(
                    f"path_globs must be a list of strings, got {type(globs).__name__}"
                )
            elif not all(isinstance(g, str) and g.strip() for g in globs):
                errors.append(
                    "path_globs must contain only non-empty strings"
                )
            elif payload.get("scope_guess") == "rule_path":
                if payload.get("is_real_learning") is True and not globs:
                    errors.append(
                        "path_globs must be non-empty when scope_guess is 'rule_path'"
                    )
            elif globs:
                errors.append(
                    "path_globs must be [] unless scope_guess is 'rule_path', "
                    f"got {globs!r} for scope {payload.get('scope_guess')!r}"
                )
        decision = payload.get("dedup_decision")
        if "dedup_decision" in payload and decision not in MINE_DEDUP_DECISIONS:
            errors.append(
                f"dedup_decision must be one of {MINE_DEDUP_DECISIONS}, got {decision!r}"
            )
        target = payload.get("dedup_target_id")
        if decision == "new" and isinstance(target, str) and target.strip():
            errors.append("dedup_target_id must be empty when dedup_decision is 'new'")
        if (
            decision in ("duplicate", "amend")
            and payload.get("is_real_learning") is True
            and isinstance(target, str)
            and not target.strip()
        ):
            errors.append(
                f"dedup_target_id must name a learning id when dedup_decision is {decision!r}"
            )
        if decision == "amend" and payload.get("is_real_learning") is True:
            for key in ("amended_rule_text", "amended_why"):
                value = payload.get(key)
                if isinstance(value, str) and not value.strip():
                    errors.append(f"{key} must be non-empty when dedup_decision is 'amend'")
    return errors


def mine_incident(
    store: Store,
    llm_call: Callable[[str], dict | None],
    incident_row: dict,
    cfg: Config,
    prompts_dir: str | Path,
    *, provenance: dict | None = None,
) -> dict | None:
    """Mine one incident into a candidate learning via a single LLM call.

    ``llm_call`` takes the rendered prompt and returns the parsed strict-JSON
    response, or None on parse failure (the llm layer records the call-level
    outcome taxonomy in llm_calls).

    Outcomes, all reflected in the store:
    - LLM parse failure -> raises :class:`MineParseFailure`; contract
      violation -> raises :class:`MineContractViolation`. In both cases the
      incident stays ``status='new'`` (retryable) and nothing is guessed —
      typed exceptions let the pipeline count these as failures, not successes.
    - ``is_real_learning: false`` -> incident ``status='dismissed'``, returns None.
    - real learning -> inserts a learnings row (including ``incident_summary``
      and the session ``source``, both persisted) plus the incident_learnings
      link, incident ``status='mined'``, and returns the inserted row dict.

    Raises MinerError if the incident's ``window_json`` is not strict JSON
    (our own DB invariant, so this is a hard failure, not a counted one).
    """
    incident_id = incident_row["id"]
    try:
        window = json.loads(incident_row["window_json"])
    except (ValueError, TypeError) as exc:
        raise MinerError(
            f"incident {incident_id}: window_json is not strict JSON: {exc}"
        ) from exc
    prompt, template_sha = render_prompt_revision(
        Path(prompts_dir) / "mine_incident.md",
        {
            "signal_type": incident_row["signal_type"],
            "project": incident_row["project_path"] or "(unknown)",
            "window": json.dumps(window, indent=2, ensure_ascii=False),
            "in_force_instructions": gather_in_force_instructions(
                incident_row["project_path"], cfg
            ),
        },
    )
    from . import mining_history
    mining_history.require_schema(store)
    source=mining_history.context('fast',prompt,template_sha,provenance)
    payload = llm_call(prompt)
    source.update(provenance or {})
    return _persist_mine_payload(store, incident_row, payload, provenance=source)


def _persist_mine_payload(
    store: Store, incident_row: dict, payload: dict | None, *, agentic: bool = False, commit: bool = True,
    provenance: dict | None = None,
) -> dict | None:
    try:
        return _persist_mine_body(store,incident_row,payload,agentic=agentic,commit=commit,provenance=provenance)
    except BaseException:
        if commit:store.conn.rollback()
        raise


def _persist_mine_body(
    store: Store, incident_row: dict, payload: dict | None, *, agentic: bool = False, commit: bool = True,
    provenance: dict | None = None,
) -> dict | None:
    """Shared validation + persistence tail of both mine paths.

    :func:`mine_incident` (fast, single-turn) and
    :func:`mine_incident_agentic` (sandboxed agent) share this single
    implementation. None payload raises MineParseFailure, a contract violation
    raises MineContractViolation (in both cases the incident stays 'new'),
    is_real_learning=false dismisses.

    A real learning's persistence depends on the agentic dedup decision
    (fast path behaves as decision 'new'):

    - ``new`` — insert a learnings row + incident_learnings link, incident
      'mined'. Returned dict carries ``_dedup: 'new'``.
    - ``duplicate`` — the referenced learning MUST exist (a hallucinated id is
      a contract violation). Rejected target: the lesson was already declined
      by the user -> incident 'dismissed', evidence linked for audit only,
      no counters grow (``_dedup: 'duplicate_of_rejected'``). Any other
      status: evidence_count += 1, projects union, last_seen refreshed,
      incident 'mined' (``_dedup: 'duplicate'``). No new learnings row.
    - ``amend`` — target must exist. rule_text/why are replaced with the
      amended text, evidence/projects rolled up, and status reset to
      'candidate' so the propose stage regenerates a proposal from the
      amended text; every known undecided proposal of that learning is marked
      'superseded' with a ledger event (``_dedup: 'amend'``). An APPLIED
      target takes the same updates but is marked ``_dedup: 'amend_applied'``:
      the pipeline's propose loop sees the prior applied proposal and builds
      an in-place EDIT proposal against the live file's ``si:`` marker line
      instead of re-routing (propose.build_edit_proposal).

    The ``_dedup`` key is in-memory only (never a DB column); the pipeline
    tallies it into run stats.
    """
    incident_id = incident_row["id"]
    if payload is None:
        logger.warning(
            "mine incident %s: LLM parse failure; incident stays 'new'", incident_id
        )
        raise MineParseFailure(f"incident {incident_id}: LLM response unparseable")
    # Apply documented defaults before validation and report the affected keys.
    payload, _defaulted = normalize_mine_payload(payload, agentic=agentic)
    if _defaulted:
        logger.info(
            "mine incident %s: model omitted %s; using the documented empty "
            "defaults (counted in mine stats)",
            incident_id,
            _defaulted,
        )
    errors = validate_mine_json(payload, agentic=agentic)
    if errors:
        logger.warning(
            "mine incident %s: response violates mine JSON contract, treated as "
            "parse failure; incident stays 'new': %s",
            incident_id,
            "; ".join(errors),
        )
        raise MineContractViolation(
            f"incident {incident_id}: {'; '.join(errors)}"
        )
    if payload["is_real_learning"] is False:
        store.update("incidents", "id", incident_id, {"status": "dismissed"})
        if commit:
            store.commit()
        return None
    # Attribution is by canonical repo, NOT by cwd. routing.route promotes to
    # the global instruction file at project_count >= 3, so counting working
    # copies let one repo with three clones promote its own lesson to a global
    # rule. Falls back to the raw path for rows scanned before
    # 0006_canonical_project_identity (which carry project_key='').
    project = incident_row.get("project_key") or incident_row.get("project_path", "")
    session = store.query_one(
        "SELECT source FROM sessions WHERE file_path = ?",
        (incident_row["session_file"],),
    )
    if session is None:
        raise MinerError(
            f"incident {incident_id}: session row missing for "
            f"{incident_row['session_file']!r} (DB invariant violation)"
        )

    decision = payload.get("dedup_decision", "new") if agentic else "new"
    if decision in ("duplicate", "amend"):
        target = store.query_one(
            "SELECT * FROM learnings WHERE id = ?", (payload["dedup_target_id"],)
        )
        if target is None:
            # A hallucinated id must fail the response, not corrupt the DB.
            raise MineContractViolation(
                f"incident {incident_id}: dedup_target_id "
                f"{payload['dedup_target_id']!r} does not exist"
            )
        out = _persist_dedup_decision(
            store, incident_row, payload, target, decision, project, commit=commit, provenance=provenance
        )
        if isinstance(out, dict):
            out["_defaulted_keys"] = _defaulted
        return out

    learning = {
        "id": new_id(),
        "title": "",
        "incident_summary": payload["incident_summary"],
        "source": session["source"],
        "rule_text": payload["generalized_rule"],
        "why": payload["why"],
        "category": payload["category"],
        "scope": payload["scope_guess"],
        "evidence_count": 1,
        "project_count": 1 if project else 0,
        "projects_json": json.dumps([project] if project else []),
        # The working copy this incident came from — routing writes here.
        # Distinct from projects_json, which holds canonical repo keys.
        "primary_project_path": incident_row.get("project_path", ""),
        "first_seen": incident_row.get("ts", ""),
        "last_seen": incident_row.get("ts", ""),
        "confidence": float(payload["confidence"]),
        "status": "candidate",
        "duplicate_of": payload["duplicate_of_existing_rule"] or "",
        # Agentic-only fields; the fast contract has no such keys, so these
        # persist as their column defaults ('' / '[]').
        "violated_existing_rule": payload.get("violated_existing_rule", ""),
        "path_globs_json": json.dumps(payload.get("path_globs", [])),
        "created_at": utc_now_iso(),
    }
    store.insert("learnings", learning)
    store.link_incident_learning(incident_id, learning["id"])
    store.update("incidents", "id", incident_id, {"status": "mined"})
    from .mining_history import append
    append(store,learning,before=None,kind='new',incidents=[incident_row],provenance=provenance)
    if commit:
        store.commit()
    learning["_defaulted_keys"] = _defaulted
    return {**learning, "_dedup": "new"}


def _earlier(a: str, b: str) -> str:
    """The earlier of two ISO stamps, treating '' as unknown rather than zero.

    `min('', ts)` is `''` for every real timestamp, so a plain min() lets a
    missing stamp erase a known one and claim the lesson began at the epoch.
    """
    both = [s for s in (a, b) if s]
    return min(both) if both else ""


def _later(a: str, b: str) -> str:
    """The later of two ISO stamps, treating '' as unknown. See `_earlier`."""
    both = [s for s in (a, b) if s]
    return max(both) if both else ""


def _persist_dedup_decision(
    store: Store,
    incident_row: dict,
    payload: dict,
    target: dict,
    decision: str,
    project: str,
    *, commit: bool = True,
    provenance: dict | None = None,
) -> dict:
    """Apply an agentic 'duplicate' or 'amend' decision to the target learning.

    See :func:`_persist_mine_payload` for the full decision semantics.
    """
    incident_id = incident_row["id"]
    store.link_incident_learning(incident_id, target["id"])

    from .rejections import lesson_rejected
    if lesson_rejected(store, target['id']):
        # The user already said no to this lesson; new evidence is recorded
        # for audit but must not resurrect it or grow its counters.
        store.update("incidents", "id", incident_id, {"status": "dismissed"})
        from .mining_history import append
        append(store,target,before=target,kind='duplicate_of_rejected',incidents=[incident_row],provenance=provenance)
        if commit:
            store.commit()
        logger.info(
            "mine incident %s: evidence for REJECTED learning %s; dismissed",
            incident_id,
            target["id"],
        )
        return {**target, "_dedup": "duplicate_of_rejected"}

    amend_of_applied = decision == "amend" and target["status"] == "applied"
    if amend_of_applied:
        logger.info(
            "mine incident %s: amend of APPLIED learning %s -> candidate; the "
            "propose stage will build an in-place edit proposal against the "
            "live file's si: marker",
            incident_id,
            target["id"],
        )

    # Fourth reader of this column. routing.py, cluster.py and this all parse
    # it; a bare json.loads here would surface a broken DB invariant as a
    # JSONDecodeError with a character offset, in the middle of a dedup merge.
    raw_projects = target.get("projects_json") or "[]"
    try:
        parsed = json.loads(raw_projects)
    except (ValueError, TypeError) as exc:
        raise MinerError(
            f"learning {target.get('id', '<no id>')!r}: projects_json is not "
            f"valid JSON (DB invariant violation): {exc}"
        ) from exc
        # Validate the JSON shape before deriving project_count. A JSON string
        # is iterable and would count characters as projects, inflating evidence
        # used by the global-routing threshold.
    if not isinstance(parsed, list):
        raise MinerError(
            f"learning {target.get('id', '<no id>')!r}: projects_json is not a "
            f"list (DB invariant violation): {type(parsed).__name__} {raw_projects!r}"
        )
    projects = set(parsed)
    if project:
        projects.add(project)
    updates: dict = {
        "evidence_count": int(target.get("evidence_count") or 0) + 1,
        "project_count": len(projects),
        "projects_json": json.dumps(sorted(projects)),
            # Mining order is independent of event time. Preserve the earliest
            # and latest evidence timestamps even when an older incident arrives
            # after a newer one.
        "first_seen": _earlier(target.get("first_seen", ""), incident_row.get("ts", "")),
        "last_seen": _later(target.get("last_seen", ""), incident_row.get("ts", "")),
    }

    if decision == "amend":
        updates["rule_text"] = payload["amended_rule_text"]
        updates["why"] = payload["amended_why"]
        # Regenerate the proposal from the amended text: back to the candidate
        # pool, and any open proposal built from the old text is superseded.
        updates["status"] = "candidate"
        from .store import PROPOSAL_STATUSES, DECIDED_STATUSES
        undecided=sorted(PROPOSAL_STATUSES-set(DECIDED_STATUSES))
        slots=','.join('?' for _ in undecided)
        open_props = store.query(
            f"SELECT id FROM proposals WHERE learning_id = ? AND status IN ({slots})",
            (target["id"],*undecided),
        )
        for prop in open_props:
            store.update("proposals", "id", prop["id"], {"status": "superseded"})
            store.insert(
                "proposal_events",
                {
                    "id": new_id(),
                    "proposal_id": prop["id"],
                    "ts": utc_now_iso(),
                    "event": "superseded",
                    "actor": "auto",
                    "note": f"amended by incident {incident_id}",
                },
            )

    store.update("learnings", "id", target["id"], updates)
    store.update("incidents", "id", incident_id, {"status": "mined"})
    refreshed = store.query_one("SELECT * FROM learnings WHERE id = ?", (target["id"],))
    from .mining_history import append
    append(store,refreshed,before=target,kind='amend_applied' if amend_of_applied else decision,
           incidents=[incident_row],provenance=provenance)
    if commit:
        store.commit()
    refreshed = store.query_one("SELECT * FROM learnings WHERE id = ?", (target["id"],))
    return {
        **refreshed,
        "_dedup": "amend_applied" if amend_of_applied else decision,
    }


# ---------------------------------------------------------------------------
# Agentic mining: an autonomous agent explores a redacted rendering of the
# FULL session in a sandbox (root-causing failures that manifest many turns
# after their origin), instead of a single-turn call on a +/-N-turn window.
# ---------------------------------------------------------------------------


def _source_for(source_name: str, cfg: Config):
    """Map a sessions-row ``source`` value to its parser. Unknown raises."""
    if source_name == "claude":
        return ClaudeCodeSource(cfg)
    if source_name == "codex":
        return CodexSource(cfg)
    raise MinerError(f"unknown session source {source_name!r} (DB invariant violation)")


def _resolve_start_line(
    entries: list[tuple[int, str, str]], incident_row: dict
) -> int:
    """Resolve the incident pointer to a transcript.md section number.

    ``entries`` is ``[(line_no, ts_utc, redacted_text), ...]`` for every
    rendered event. Incident rows do not store line numbers, so the pointer is
    re-derived from the data: candidate events are those whose ts equals the
    incident's ts (logical time from the data, never guessed); among several
    same-ts events the one containing the redacted ``matched_text`` wins,
    falling back to the first ts match (matched_text was length-capped before
    redaction, so verbatim containment is not guaranteed). No ts match at all
    means the transcript no longer contains the incident — a broken
    invariant, raised loudly rather than pointing the agent somewhere wrong.
    """
    ts = incident_row["ts"]
    matched_text = incident_row.get("matched_text", "")
    ts_hits = [(line_no, text) for line_no, ets, text in entries if ets == ts]
    if not ts_hits:
        raise MinerError(
            f"incident {incident_row['id']}: no transcript event carries the "
            f"incident ts {ts!r}; pointer unresolvable"
        )
    if matched_text:
        for line_no, text in ts_hits:
            if matched_text in text:
                return line_no
    return ts_hits[0][0]


def _build_full_sandbox(
    store: Store, incident_row: dict, cfg: Config, sandbox_dir: Path
) -> tuple[Path, int]:
    """Build transcript.md + environment.md from the FULL session file.

    Returns ``(sandbox_dir, start_line)``. Raises :class:`TranscriptAgedOut`
    when the transcript file no longer exists on disk, and MinerError for
    broken DB invariants (missing session row, empty parse, unresolvable
    pointer).
    """
    events, start_line = _full_mine_events(store, incident_row, cfg)
    sandbox_dir = Path(sandbox_dir)
    sandbox_dir.mkdir(parents=True, exist_ok=True)
    render_session(events, sandbox_dir / "transcript.md")
    render_environment(
        store, {**incident_row, "start_line": start_line}, cfg,
        sandbox_dir / "environment.md",
    )
    _write_learnings_dump(store, sandbox_dir)
    return sandbox_dir, start_line


def _full_mine_events(store, incident_row, cfg):
    """Read full session events and resolve the incident pointer without writes."""
    incident_id = incident_row["id"]
    session_file = incident_row["session_file"]
    session = store.query_one(
        "SELECT * FROM sessions WHERE file_path = ?", (session_file,)
    )
    if session is None:
        raise MinerError(
            f"incident {incident_id}: session row missing for "
            f"{session_file!r} (DB invariant violation)"
        )
    if not Path(session_file).is_file():
        raise TranscriptAgedOut(
            f"incident {incident_id}: transcript no longer on disk: {session_file}"
        )
    source = _source_for(session["source"], cfg)
    events = list(source.parse(session_file, 0))
    if not events:
        raise MinerError(
            f"incident {incident_id}: transcript parsed to zero events: {session_file}"
        )
    start_line = _resolve_start_line(
        # Defense in depth: render.py also redacts every event body before the
        # final prompt. Preserve that final guard when changing this layer.
        [(e.line_no, e.ts_utc, redact_text(e.text)) for e in events], incident_row
    )
    return events, start_line


def _write_learnings_dump(store: Store, sandbox_dir: Path) -> None:
    """Write ``learnings.jsonl``: the whole learnings table for the agent's
    keyword-side dedup (Grep). Embedding-side dedup goes through the
    read-only ``search-learnings`` CLI. rule_text/why are redacted defensively
    (they should already be clean — they were mined from redacted windows —
    but this file leaves our process for an LLM's context)."""
    rows = store.query(
        "SELECT id, status, rule_text, why, category, scope, evidence_count, "
        "project_count FROM learnings ORDER BY created_at"
    )
    with open(sandbox_dir / "learnings.jsonl", "w", encoding="utf-8") as fh:
        for r in rows:
            r["rule_text"] = redact_text(r["rule_text"])
            r["why"] = redact_text(r["why"])
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def _build_fallback_sandbox(
    store: Store, incident_row: dict, cfg: Config, sandbox_dir: Path
) -> tuple[Path, int]:
    """Build the sandbox from the incident's stored ``window_json``.

    Used only when the full transcript aged out (:class:`TranscriptAgedOut`).
    transcript.md is marked with :data:`AGED_OUT_NOTE` so the agent knows it
    is seeing the redacted incident window only, not the full session.
    Window entries were written by archive_trajectory.py as ``{role, ts, text}``
    (already redacted; render re-redacts, which is an idempotent no-op) — any
    other shape is a broken DB invariant and raises.
    """
    events, start_line = _fallback_mine_events(store, incident_row)
    sandbox_dir = Path(sandbox_dir)
    sandbox_dir.mkdir(parents=True, exist_ok=True)
    transcript_path = sandbox_dir / "transcript.md"
    render_session(events, transcript_path)
    transcript_path.write_text(
        f"{AGED_OUT_NOTE}\n\n{transcript_path.read_text(encoding='utf-8')}",
        encoding="utf-8",
    )
    render_environment(
        store, {**incident_row, "start_line": start_line}, cfg,
        sandbox_dir / "environment.md",
    )
    _write_learnings_dump(store, sandbox_dir)
    return sandbox_dir, start_line


def _fallback_mine_events(store, incident_row):
    """Read both retained archive shapes without creating sandbox files."""
    incident_id = incident_row["id"]
    session = store.query_one(
        "SELECT * FROM sessions WHERE file_path = ?",
        (incident_row["session_file"],),
    )
    if session is None:
        raise MinerError(
            f"incident {incident_id}: session row missing for "
            f"{incident_row['session_file']!r} (DB invariant violation)"
        )
    try:
        window = json.loads(incident_row["window_json"])
    except (ValueError, TypeError) as exc:
        raise MinerError(
            f"incident {incident_id}: window_json is not strict JSON: {exc}"
        ) from exc
    if not isinstance(window, list) or not window:
        raise MinerError(
            f"incident {incident_id}: window_json must be a non-empty list to "
            f"build the aged-out fallback sandbox, got "
            f"{type(window).__name__} of len "
            f"{len(window) if isinstance(window, list) else 'n/a'}"
        )
    events: list[TurnEvent] = []
    for i, entry in enumerate(window, start=1):
        if not isinstance(entry, dict):
            raise MinerError(
                f"incident {incident_id}: window_json entry {i} is not an "
                "object (DB invariant violation)"
            )
        if {"role", "ts", "text"} <= entry.keys():
            role, text = str(entry["role"]), str(entry["text"])
        elif {"ts", "text"} <= entry.keys():
            # The OTHER legitimate shape: a promoted repeated_error stores one
            # entry per session it recurred in, not a turn window
            # (scan.py's cross-session promotion). Rendering it as a turn would
            # throw away the two things that make it evidence — how often it
            # recurred and where — so the count and the session are folded into
            # the text. Accept both archive shapes so an occurrence remains usable
            # after its source transcript is gone.
            where = str(entry.get("session_file") or "").rsplit("/", 1)[-1]
            count = entry.get("count_in_session")
            prefix_bits = []
            if count is not None:
                prefix_bits.append(f"{count}x")
            if where:
                prefix_bits.append(f"in {where}")
            prefix = f"[recurred {' '.join(prefix_bits)}]\n" if prefix_bits else ""
            role, text = "tool_result", prefix + str(entry["text"])
        else:
            raise MinerError(
                f"incident {incident_id}: window_json entry {i} is neither a "
                "{role, ts, text} turn nor a {ts, text, ...} occurrence "
                "(DB invariant violation)"
            )
        events.append(
            TurnEvent(
                source=session["source"],
                session_file=incident_row["session_file"],
                session_id=incident_row.get("session_id", ""),
                project_path=incident_row.get("project_path", ""),
                ts_utc=str(entry["ts"]),
                role=role,
                kind="window",
                text=text,
                line_no=i,
            )
        )
    start_line = _resolve_start_line(
        [(e.line_no, e.ts_utc, e.text) for e in events], incident_row
    )
    return events, start_line


def build_mine_sandbox(
    store: Store, incident_row: dict, cfg: Config, sandbox_dir: Path
) -> Path:
    """Build the agentic-mine sandbox from the incident's FULL session file.

    Parses the whole session with the right source parser (sessions-row
    ``source`` -> ClaudeCodeSource/CodexSource, from offset 0), writes
    ``sandbox_dir/transcript.md`` via render_session (the redaction gate) and
    ``sandbox_dir/environment.md`` via render_environment, and returns
    ``sandbox_dir``.

    Contract: raises :class:`TranscriptAgedOut` (a MinerError) if the
    transcript file no longer exists on disk. The caller —
    :func:`mine_incident_agentic` — then falls back to building transcript.md
    from the incident's stored ``window_json``, marked with
    :data:`AGED_OUT_NOTE`. Other MinerErrors (missing session row, zero
    events, unresolvable incident pointer) are hard invariant failures with
    no fallback.
    """
    path, _start_line = _build_full_sandbox(store, incident_row, cfg, Path(sandbox_dir))
    return path


def mine_incident_agentic(
    store: Store,
    agentic_call: Callable[[str, Path], dict | None],
    incident_row: dict,
    cfg: Config,
    prompts_dir: str | Path,
    sandbox_root: Path,
    *, provenance: dict | None = None,
) -> dict | None:
    """Mine one incident by letting an agent explore the full redacted session.

    Builds a sandbox under ``sandbox_root/<incident_id>`` (falling back to the
    stored window when the transcript aged out — see :func:`build_mine_sandbox`),
    renders ``prompts/mine_incident_agentic.md``, and calls
    ``agentic_call(prompt, sandbox_dir)``: a headless agent session with cwd
    inside the sandbox, restricted to cfg.mine_agent_allowed_tools, whose
    return value is the parsed strict-JSON final message. A reply that will not
    parse comes back as None; a CALL that never produced a reply raises
    ``pipeline.MineCallFailed`` carrying the outcome class. The two are
    separate because a parse failure requires a reply. The llm layer records
    call-level failure taxonomy before this reader validates the response.

    The response contract, validation, DB writes, and typed exceptions
    (MineParseFailure / MineContractViolation) are IDENTICAL to
    :func:`mine_incident` — both paths share :func:`_persist_mine_payload`.
    """
    incident_id = incident_row["id"]
    sandbox_dir = Path(sandbox_root) / incident_id
    try:
        sandbox_dir, start_line = _build_full_sandbox(
            store, incident_row, cfg, sandbox_dir
        )
    except TranscriptAgedOut as exc:
        logger.warning(
            "mine incident %s: %s; falling back to stored incident window",
            incident_id,
            exc,
        )
        sandbox_dir, start_line = _build_fallback_sandbox(
            store, incident_row, cfg, sandbox_dir
        )
    prompt, template_sha = render_prompt_revision(
        Path(prompts_dir) / "mine_incident_agentic.md",
        {
            "signal_type": incident_row["signal_type"],
            "matched_text": incident_row.get("matched_text", ""),
            "start_line": str(start_line),
            "project": incident_row["project_path"] or "(unknown)",
            "search_cli": cfg.mine_search_cli,
            # Derive the prompt's limit from the enforced configuration.
            # Reserve turns for emitting the final JSON after exploration.
            "max_turns": str(cfg.mine_agent_max_turns),
            "explore_budget": str(max(1, cfg.mine_agent_max_turns - TURN_RESERVE)),
        },
    )
    from . import mining_history
    mining_history.require_schema(store)
    source=mining_history.context('agentic',prompt,template_sha,provenance)
    payload = agentic_call(prompt, sandbox_dir)
    source.update(provenance or {})
    return _persist_mine_payload(store, incident_row, payload, agentic=True, provenance=source)
