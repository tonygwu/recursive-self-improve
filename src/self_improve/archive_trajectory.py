"""Archive a redacted evidence window alongside each incident.

**This is not the miner's input.** The agentic miner builds its sandbox from
the FULL session file (``miner.build_mine_sandbox``) and chooses its own
context; it never reads ``window_json`` on the happy path. This module exists
for one reason: Claude Code deletes transcripts on ``cleanupPeriodDays``, and
once the file is gone the incident is unminable forever. The archived window
is the only surviving evidence, and no amount of agent capability recovers a
deleted file.

Consequence for tuning: these parameters are a *retention* budget, not a
prompt budget. Widening them costs DB bytes and buys recoverability; the old
±6/1500 defaults were sized for prompt economy, which is the wrong objective
for a fallback that is read only when the original is gone.

Everything in the window is redacted here; callers never re-redact.

Windowing rules (documented, deterministic):

- The slice is ``cfg.context_turns_before`` events before the incident event
  through ``cfg.context_turns_after`` events after it, clamped to the
  session bounds.
- If the incident event is a ``tool_use``/``patch_apply``, the next
  ``tool_result``/``patch_apply`` event after it is pulled into the window
  even when it falls outside the after-slice — the result of the triggering
  tool call is "directly involved".
- Each message's text is redacted first, then truncated to
  ``cfg.context_max_chars_per_message`` kept characters with an explicit
  ``[truncated N chars]`` suffix (N = characters removed; the marker itself
  is appended beyond the limit so the count is never hidden). Redaction runs
  before truncation so a secret can never be split by the cut and leak a
  recognizable prefix.
"""

from __future__ import annotations

from .config import Config
from .filter_incidents import CandidateIncident
from .redact import redact_text
from .sources.base import (
    KIND_PATCH_APPLY,
    KIND_TOOL_RESULT,
    KIND_TOOL_USE,
    TurnEvent,
)


def _truncate(text: str, limit: int) -> str:
    """Cap ``text`` at ``limit`` chars with an explicit truncation marker."""
    if len(text) <= limit:
        return text
    cut = len(text) - limit
    return f"{text[:limit]}[truncated {cut} chars]"


def build_window(
    events: list[TurnEvent], incident: CandidateIncident, cfg: Config
) -> list[dict]:
    """Return the incident's context as ``[{role, ts, text}, ...]``.

    ``incident.event_index`` must point into ``events`` (the same list the
    filter_incidents ran over); anything else is a programming error and raises.
    """
    i = incident.event_index
    if not 0 <= i < len(events):
        raise IndexError(
            f"incident event_index {i} outside session of {len(events)} events"
        )

    lo = max(0, i - cfg.context_turns_before)
    hi = min(len(events), i + cfg.context_turns_after + 1)
    indices = set(range(lo, hi))

    # Pull in the tool_result of the triggering tool call if it fell outside.
    if events[i].kind in (KIND_TOOL_USE, KIND_PATCH_APPLY):
        for j in range(i + 1, len(events)):
            if events[j].kind in (KIND_TOOL_RESULT, KIND_PATCH_APPLY):
                indices.add(j)
                break

    window: list[dict] = []
    for j in sorted(indices):
        e = events[j]
        text = _truncate(redact_text(e.text), cfg.context_max_chars_per_message)
        window.append({"role": e.role, "ts": e.ts_utc, "text": text})
    return window
