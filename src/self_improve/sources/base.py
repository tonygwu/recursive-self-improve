"""Normalized event model shared by both transcript sources.

Every parser (claude_code.py, codex.py) emits TurnEvent records; everything
downstream (filter_incidents, archive_trajectory, miner) consumes only TurnEvents and never
touches raw transcript JSON.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, Protocol

# role values
ROLE_HUMAN = "human"            # text the human actually typed
ROLE_ASSISTANT = "assistant"    # assistant text or tool_use
ROLE_TOOL_RESULT = "tool_result"
ROLE_SYSTEM = "system"          # meta/system/attachment lines worth keeping

# kind values (finer-grained; parser-specific values allowed beyond these)
KIND_MESSAGE = "message"
KIND_TOOL_USE = "tool_use"
KIND_TOOL_RESULT = "tool_result"
KIND_PATCH_APPLY = "patch_apply"   # codex patch_apply_end
KIND_SLASH_COMMAND = "slash_command"


@dataclass(frozen=True)
class TurnEvent:
    source: str          # "claude" | "codex"
    session_file: str    # absolute path of the transcript file
    session_id: str
    project_path: str    # session cwd; "" if unknown
    ts_utc: str          # ISO-8601 UTC read from the DATA (never file mtime); "" if absent
    role: str            # ROLE_* above
    kind: str            # KIND_* above or parser-specific
    text: str            # normalized text content ("" if none)
    tool_name: str = ""  # for tool_use / tool_result
    is_error: bool = False
    headless: bool = False   # claude: entrypoint=="sdk-cli"; codex: originator/exec
    line_no: int = 0         # 1-based line in the transcript file
    meta: dict = field(default_factory=dict)


@dataclass(frozen=True)
class SessionFileInfo:
    """One transcript file as discovered on disk (pre-parse)."""

    source: str        # "claude" | "codex"
    file_path: str     # absolute
    mtime: float       # scan-trigger only, never used as logical time
    size: int
    project_slug: str  # claude: projects/<slug>; codex: "" until parsed
    is_subagent: bool = False


# Physical-line categories reported by parsers that support line records.
LINE_EVENT = "event"                # emitted at least one TurnEvent
LINE_SKIPPED = "skipped"            # known record deliberately not mined; cause names it
LINE_BLANK = "blank"
LINE_MALFORMED = "malformed"        # cause is the parser's taxonomy key
LINE_ENCRYPTED = "encrypted"        # opaque payload; detectors cannot read it
LINE_UNKNOWN_TYPE = "unknown_type"  # cause is the unrecognized record type
LINE_CATEGORIES = frozenset(
    {LINE_EVENT, LINE_SKIPPED, LINE_BLANK, LINE_MALFORMED, LINE_ENCRYPTED, LINE_UNKNOWN_TYPE}
)


@dataclass(frozen=True)
class PhysicalLine:
    """One complete newline-terminated transcript line, without its content.

    Identity fields carry state established by an EARLIER record in the same
    parse when this record lacks them. A timestamp is never carried: ``ts_raw``
    is only this record's own field. ``headless``/``is_subagent`` are None when
    no record has established them yet.
    """

    line_no: int         # absolute, 1-based
    byte_start: int
    byte_end: int        # exclusive; includes the newline
    sha256: str          # digest of the complete line bytes
    ts_raw: str          # this record's own timestamp field; "" if absent
    session_id: str      # "" when unknown
    project_path: str    # cwd; "" when unknown
    category: str        # LINE_* above
    cause: str = ""
    events: int = 0      # TurnEvents emitted from this line
    headless: bool | None = None
    is_subagent: bool | None = None
    denylisted: bool = False


class SessionSource(Protocol):
    """A transcript store that can be discovered and parsed incrementally."""

    name: str

    def discover(self) -> Iterator[SessionFileInfo]:
        """Yield every transcript file, applying the config denylist."""
        ...

    def parse(self, file_path: str, start_offset: int = 0) -> Iterator[TurnEvent]:
        """Stream TurnEvents from file_path starting at byte start_offset.

        Must tolerate a truncated final line (file still being appended) by
        stopping cleanly; must never raise on malformed individual lines but
        count them (surfaced via parse_stats).
        """
        ...
