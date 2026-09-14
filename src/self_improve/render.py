"""Render a session into the redacted sandbox files the agentic miner explores.

The agentic mine stage (miner.mine_incident_agentic) drops an LLM agent into a
sandbox directory containing ``transcript.md`` (the full session, one markdown
section per TurnEvent) and ``environment.md`` (project/session context plus the
incident pointer). THIS MODULE IS THE REDACTION GATE for those files: every
event body written by :func:`render_session` passes through
``redact.redact_text`` — the sandbox is exactly what the LLM agent reads, so
the "transcript excerpts are redacted before entering any LLM prompt" invariant
lives here, not in the callers.

Ordering invariant (same reasoning as archive_trajectory.py): redaction runs BEFORE
truncation, so a secret can never be split by the cut and leak a recognizable
prefix. Every truncation is marked inline with exactly how many characters it
removed, and counted in :class:`RenderStats`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .redact import redact_text
from .sources.base import TurnEvent

# Per-event body cap in transcript.md. Fixed by the render contract (not a
# config knob); every cut is marked inline and counted in RenderStats.
RENDER_BODY_TRUNCATE_CHARS = 4000

# Prefix every typed redaction placeholder shares (see redact.py).
_REDACTED_MARKER = "[REDACTED:"


@dataclass
class RenderStats:
    """What one render_session call wrote, per the reporting policy.

    ``redactions_estimated`` counts ``[REDACTED:`` occurrences in the redacted
    bodies BEFORE truncation, so a cut can never hide that a secret was
    present. It is an estimate: source text that already contained the literal
    marker (redaction is idempotent, so re-rendering redacted text is legal)
    counts too.
    """

    events_rendered: int = 0
    chars_written: int = 0
    truncations: int = 0
    redactions_estimated: int = 0


def render_session(events: list[TurnEvent], out_path: Path) -> RenderStats:
    """Write ``events`` as a redacted, human/agent-readable markdown transcript.

    One section per event::

        ## [<line_no>] <role>/<kind> <ts_utc> tool=<tool_name> ERROR

        <redacted, truncated body>

    ``tool=`` appears only when the event names a tool; ``ERROR`` only when
    ``is_error`` is set. ``<line_no>`` is the 1-based line number in the
    ORIGINAL transcript file (several events parsed from one JSONL line share
    it), so the bracketed number is a stable, greppable anchor independent of
    this file's own line numbering.

    Every body is redacted (this is the redaction gate — see module docstring)
    and then truncated to RENDER_BODY_TRUNCATE_CHARS kept characters with an
    explicit ``[truncated N chars]`` marker appended beyond the cap, so the
    count is never hidden.
    """
    doc, stats = session_document(events)
    Path(out_path).write_text(doc, encoding="utf-8")
    return stats


def session_document(events: list[TurnEvent]) -> tuple[str, RenderStats]:
    """Render in memory so read-only previews can freeze the exact redacted input."""
    stats = RenderStats()
    parts: list[str] = ["# Session transcript (redacted)"]
    for event in events:
        heading = f"## [{event.line_no}] {event.role}/{event.kind} {event.ts_utc}".rstrip()
        if event.tool_name:
            heading += f" tool={event.tool_name}"
        if event.is_error:
            heading += " ERROR"
        # This is the final redaction guard for every rendered event, including
        # archived-window fallback. Preserve it when refactoring earlier redaction.
        # Verify with tests/test_miner_agentic.py -k carries_no_secret.
        body = redact_text(event.text)
        stats.redactions_estimated += body.count(_REDACTED_MARKER)
        if len(body) > RENDER_BODY_TRUNCATE_CHARS:
            cut = len(body) - RENDER_BODY_TRUNCATE_CHARS
            body = body[:RENDER_BODY_TRUNCATE_CHARS] + f"[truncated {cut} chars]"
            stats.truncations += 1
        parts.append(f"{heading}\n\n{body}")
        stats.events_rendered += 1
    doc = "\n\n".join(parts) + "\n"
    stats.chars_written = len(doc)
    return doc, stats


def _transcript_size_note(transcript: Path) -> str:
    """One line telling the agent how big transcript.md is, and what that means.

    A complete transcript can exceed a tool's read limit. Report its measured
    size and section count so the agent can select relevant sections without
    first spending a tool call on an oversized read.
    """
    try:
        text = transcript.read_text(encoding="utf-8")
    except OSError:
        return ""
    return transcript_text_note(text)


def transcript_text_note(text: str) -> str:
    """Measured transcript coverage, shared by disk and frozen-job renderers."""
    size = len(text.encode("utf-8"))
    sections = text.count("\n## [")
    note = f"- transcript.md is {size / 1e6:.1f} MB across ~{sections} sections."
    if size > 400_000:
        note += (
            " Do NOT Read it whole — that will fail on token limits and cost you"
            " turns. Grep for `[<n>]` to jump straight to a section, and widen"
            " outward from the incident pointer above."
        )
    return note


def render_environment(store, incident_row: dict, cfg: Config, out_path: Path) -> None:
    """Write ``environment.md``: session context plus the incident pointer.

    Contents: the incident's project path; the session's source / headless /
    is_subagent flags and data-derived date range (from the sessions row — a
    missing row is a broken DB invariant and raises MinerError, matching
    mine_incident); the in-force instruction files via
    ``miner.gather_in_force_instructions``; and the incident pointer
    (signal_type, matched_text, and the transcript section to start from).

    ``incident_row`` must carry a ``start_line`` key naming the ``## [N]``
    transcript.md section where the signal fired. Incident DB rows do not
    store line numbers, so ``miner.build_mine_sandbox`` resolves the pointer
    against the parsed events and injects the key; calling without it is a
    programming error and raises (fail loud, never guess a pointer).

    Redaction: ``matched_text`` is passed through redact_text (idempotent —
    it is stored redacted; this is defense in depth). The in-force instruction
    files are the user's own instruction files and are included UNREDACTED,
    exactly as gather_in_force_instructions feeds them to the fast-path mine
    prompt: the duplicate check requires quoting existing lines verbatim.

    "Unredacted" is not "complete": each file is capped at
    ``miner.IN_FORCE_TRUNCATE_CHARS``. The global instruction budget is in
    lines, so it cannot establish how much a character cap omits. Account for
    that distinction when changing either limit.

    The agentic prompt says environment.md is NOT a complete copy and directs
    duplicate checks to the search tool, which indexes complete in-force files.
    Keep that explanation consistent with the renderer. This artifact is written
    per mined incident, so changes also affect retained private storage.
    """
    transcript = Path(out_path).parent / "transcript.md"
    text = transcript.read_text(encoding="utf-8") if transcript.exists() else ""
    Path(out_path).write_text(environment_document(store, incident_row, cfg, text), encoding="utf-8")


def environment_document(store, incident_row: dict, cfg: Config, transcript_text: str) -> str:
    """Read session context and instructions without creating a sandbox."""
    # Lazy import: miner imports this module at top level; importing miner
    # lazily here breaks the cycle without duplicating gather logic.
    from .miner import MinerError, gather_in_force_instructions

    incident_id = incident_row.get("id", "(unknown)")
    if "start_line" not in incident_row:
        raise MinerError(
            f"incident {incident_id}: render_environment needs a resolved "
            "'start_line' in incident_row (injected by build_mine_sandbox); "
            "refusing to write an environment without an incident pointer"
        )
    session = store.query_one(
        "SELECT * FROM sessions WHERE file_path = ?",
        (incident_row["session_file"],),
    )
    if session is None:
        raise MinerError(
            f"incident {incident_id}: session row missing for "
            f"{incident_row['session_file']!r} (DB invariant violation)"
        )
    project = incident_row.get("project_path") or "(unknown)"
    first_ts = session["first_ts"] or "(unknown)"
    last_ts = session["last_ts"] or "(unknown)"
    doc = "\n".join(
        [
            "# Environment",
            "",
            f"- Project: {project}",
            f"- Session source: {session['source']}",
            f"- Headless: {'yes' if session['headless'] else 'no'}",
            f"- Subagent session: {'yes' if session['is_subagent'] else 'no'}",
            f"- Session date range: {first_ts} .. {last_ts}",
            "",
            "## Incident pointer",
            "",
            f"- Signal type: {incident_row['signal_type']}",
            f"- Matched text (redacted): {redact_text(incident_row.get('matched_text', ''))}",
            f"- Start at the transcript.md section headed: ## [{incident_row['start_line']}]",
            transcript_text_note(transcript_text),
            "",
            "## Instruction files in force for this project",
            "",
            gather_in_force_instructions(incident_row.get("project_path", ""), cfg),
            "",
        ]
    )
    return doc
