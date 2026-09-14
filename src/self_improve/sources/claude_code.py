"""Claude Code transcript source (~/.claude/projects).

Supported layout on disk:

- Main sessions:      ``<projects_dir>/<slug>/<session-uuid>.jsonl``
- Subagent sessions:  ``<projects_dir>/<slug>/<session-uuid>/subagents/agent-*.jsonl``

Every user/assistant line carries an envelope with ``uuid, parentUuid,
timestamp`` (ISO-8601 UTC with ``Z``), ``cwd, sessionId, gitBranch,
entrypoint`` ("cli" interactive | "sdk-cli" headless), ``isSidechain`` and
sometimes ``isMeta``. ``type:"user"`` lines are a union: human text (string
content), slash-command XML, local-command output, task notifications, and
tool_results (list content with ``{"type": "tool_result", ...}`` items).
Assistant ``message.content`` is a list of ``text`` / ``tool_use`` /
``thinking`` items. Bookkeeping line types (``ai-title``, ``last-prompt``,
``mode``, ``file-history-snapshot``, ...) carry no conversational content.

Fail-loud policy: malformed lines and unknown line types are never guessed at;
they are counted per-taxonomy in ``ParseStats`` and skipped. A truncated final
line (file still being appended) stops the parse cleanly without counting as
malformed and without advancing the resume offset past it.

Logical time comes from the ``timestamp`` field inside each line, never from
file mtime (mtime is carried on ``SessionFileInfo`` purely as a scan trigger).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from ..config import Config
from .base import (
    KIND_MESSAGE,
    KIND_SLASH_COMMAND,
    KIND_TOOL_RESULT,
    KIND_TOOL_USE,
    LINE_BLANK,
    LINE_EVENT,
    LINE_MALFORMED,
    LINE_SKIPPED,
    LINE_UNKNOWN_TYPE,
    ROLE_ASSISTANT,
    ROLE_HUMAN,
    ROLE_SYSTEM,
    ROLE_TOOL_RESULT,
    PhysicalLine,
    SessionFileInfo,
    TurnEvent,
)

SOURCE_NAME = "claude"

# Line types that are pure bookkeeping: known, deliberately skipped, counted.
BOOKKEEPING_TYPES = frozenset(
    {
        "mode",
        "permission-mode",
        "file-history-snapshot",
        "file-history-delta",
        "ai-title",
        "last-prompt",
        "queue-operation",
        "agent-name",
        "attachment",
        "summary",
        # Bookkeeping envelopes without conversation text or tool results.
        # Supported key sets include:
        #   atis-latch          {type, atis, sessionId}
        #   bridge-session      {type, sessionId, bridgeSessionId,
        #                        lastSequenceNum, ownerAccountUuid,
        #                        ownerOrganizationUuid}
        #   history-suppression {type, sessionId, cause,
        #                        vetoedAgainstAccountUuid, ts}
        #   frame-link          {type, sessionId, path, frameUrl, title,
        #                        timestamp}
        "atis-latch",
        "bridge-session",
        "history-suppression",
        "frame-link",
        # Additional bookkeeping envelopes:
        #   pr-link          {type, sessionId, prNumber, prUrl, prRepository,
        #                     timestamp}
        #   fork-context-ref {type, agentId, parentSessionId, parentLastUuid,
        #                     contextLength}
        # `fork-context-ref` names the parent session and the exact fork point.
        # Nothing reads it yet; it is the link a future subagent-to-parent join
        # would need.
        "pr-link",
        "fork-context-ref",
    }
)

# String-content user lines starting with these tags are machinery, not human
# text. Classify them even when the envelope does not set isMeta.
_SLASH_COMMAND_PREFIXES = ("<command-name>", "<command-message>", "<command-args>")
_LOCAL_STDOUT_PREFIX = "<local-command-stdout>"
_LOCAL_CAVEAT_PREFIX = "<local-command-caveat>"
_TASK_NOTIFICATION_PREFIX = "<task-notification>"

# Exclude alternate account-directory mirrors to avoid counting shared sessions
# twice. Derive the recognized account pattern rather than enumerating letters.
# Matches `.claude-<single letter>` only: `.claude` itself is the directory we
# are supposed to read, and `.claude-code-cache` is not an account mirror.
_MIRRORED_CONFIG_DIR = re.compile(r"^\.claude-[a-z]$")


def _is_mirrored_config_dir(part: str) -> bool:
    """True for a non-default Claude config dir whose projects/ mirrors ~/.claude."""
    return bool(_MIRRORED_CONFIG_DIR.match(part))


class ClaudeSourceError(Exception):
    """Raised for whole-source failures (bad root dir), never per-line ones."""


def matches_denylist(config: Config, value: str) -> str | None:
    """Return the first denylist substring found in ``value``, else None.

    Used both here (against the project slug at discovery time) and by the
    scan stage (against the session cwd once parsing reveals it).
    """
    for sub in config.denylist_substrings:
        if sub in value:
            return sub
    return None


@dataclass
class DiscoverStats:
    """Progress + what the denylist cut, per the reporting policy."""

    root: str = ""
    slugs_seen: int = 0
    slugs_denylisted: list[str] = field(default_factory=list)
    non_dir_entries: int = 0
    main_files: int = 0
    subagent_files: int = 0


@dataclass
class ParseStats:
    """Attempted/succeeded/failed line accounting for one parse() call.

    - ``lines_attempted``: complete lines examined (a truncated final line is
      NOT attempted; it is flagged via ``truncated_final_line``).
    - ``lines_succeeded``: JSON-parsed and routed to a known handler
      (event-emitting or a known bookkeeping skip).
    - ``lines_failed``: malformed JSON, undecodable bytes, a missing ``type``,
      or structurally broken known types; per-cause counts in
      ``error_taxonomy``. An UNKNOWN ``type`` is not counted here — see
      ``unknown_lines``.
    - ``skipped_taxonomy``: known-and-deliberately-skipped material — one key
      per bookkeeping line type, plus content-item keys such as ``thinking``.
    - ``end_offset``: byte offset parsing stopped at; pass it back as
      ``start_offset`` (with ``lines_attempted`` accumulated into
      ``start_line_no``) to resume an append-only file exactly where this
      call left off.
    """

    file_path: str = ""
    start_offset: int = 0
    end_offset: int = 0
    lines_attempted: int = 0
    lines_succeeded: int = 0
    lines_failed: int = 0
    events_emitted: int = 0
    truncated_final_line: bool = False
    error_taxonomy: dict[str, int] = field(default_factory=dict)
    skipped_taxonomy: dict[str, int] = field(default_factory=dict)
    # A line type we have never seen. Well-formed JSON, unknown `type`. NOT a
    # malformed line: calling it one says the file is corrupt, marks the whole
    # session `partial`, and buries the actual news, which is that the
    # transcript format grew a record we do not handle. Counted here and
    # surfaced in the run report so a new type that DOES carry content is
    # visible, rather than silently accepted.
    unknown_lines: int = 0
    unknown_taxonomy: dict[str, int] = field(default_factory=dict)
    # Content-free physical-line records, collected only when parse() is called
    # with record_lines=True (scan measurement).
    line_records: list[PhysicalLine] = field(default_factory=list, repr=False)
    last_error: str = field(default="", repr=False, compare=False)

    # SessionSource-contract aliases: scan.py reads bytes_consumed /
    # lines_consumed / malformed_lines off every source's parse stats.
    @property
    def bytes_consumed(self) -> int:
        return self.end_offset

    @property
    def lines_consumed(self) -> int:
        return self.lines_attempted

    @property
    def malformed_lines(self) -> int:
        return self.lines_failed

    def _error(self, key: str) -> None:
        self.last_error = key
        self.lines_failed += 1
        self.error_taxonomy[key] = self.error_taxonomy.get(key, 0) + 1

    def _unknown(self, line_type: str) -> None:
        self.unknown_lines += 1
        self.unknown_taxonomy[line_type] = self.unknown_taxonomy.get(line_type, 0) + 1

    def _skip(self, key: str) -> None:
        self.skipped_taxonomy[key] = self.skipped_taxonomy.get(key, 0) + 1


class ClaudeCodeSource:
    """SessionSource implementation for Claude Code transcripts.

    ``discover()`` materializes its listing eagerly so ``discover_stats`` is
    complete as soon as it returns. ``parse()`` streams lazily; its
    ``parse_stats`` (a fresh object per call, also returned generator-side via
    the attribute) is only final once the generator is exhausted. One instance
    should not interleave two parses.
    """

    name = SOURCE_NAME
    #: parse(record_lines=True) reports PhysicalLine records for measurement.
    supports_line_records = True

    def __init__(self, config: Config):
        self.config = config
        self.discover_stats = DiscoverStats()
        self.parse_stats = ParseStats()

    # ------------------------------------------------------------------
    # discovery
    # ------------------------------------------------------------------

    def discover(self) -> Iterator[SessionFileInfo]:
        """Yield every main + subagent transcript file, denylist applied to slug.

        Raises ClaudeSourceError if the configured root does not exist or
        resolves into a ``.claude-<single letter>`` account-directory mirror.
        Scanning a mirror would double-count the default directory's sessions.
        """
        root = Path(self.config.claude_projects_dir)
        resolved = root.resolve()
        for part in resolved.parts:
            if _is_mirrored_config_dir(part):
                raise ClaudeSourceError(
                    f"Refusing to scan {resolved}: {part} mirrors ~/.claude via "
                    "symlinks; scanning it double-counts sessions."
                )
        if not root.is_dir():
            raise ClaudeSourceError(f"claude_projects_dir does not exist: {root}")

        stats = DiscoverStats(root=str(root))
        infos: list[SessionFileInfo] = []
        for slug_dir in sorted(root.iterdir()):
            if not slug_dir.is_dir():
                stats.non_dir_entries += 1
                continue
            stats.slugs_seen += 1
            slug = slug_dir.name
            if matches_denylist(self.config, slug) is not None:
                stats.slugs_denylisted.append(slug)
                continue
            for f in sorted(slug_dir.glob("*.jsonl")):
                if not f.is_file():
                    continue
                st = f.stat()
                stats.main_files += 1
                infos.append(
                    SessionFileInfo(
                        source=SOURCE_NAME,
                        file_path=str(f),
                        mtime=st.st_mtime,
                        size=st.st_size,
                        project_slug=slug,
                        is_subagent=False,
                    )
                )
            for f in sorted(slug_dir.glob("*/subagents/*.jsonl")):
                if not f.is_file():
                    continue
                st = f.stat()
                stats.subagent_files += 1
                infos.append(
                    SessionFileInfo(
                        source=SOURCE_NAME,
                        file_path=str(f),
                        mtime=st.st_mtime,
                        size=st.st_size,
                        project_slug=slug,
                        is_subagent=True,
                    )
                )
        self.discover_stats = stats
        return iter(infos)

    # ------------------------------------------------------------------
    # parsing
    # ------------------------------------------------------------------

    def parse(
        self,
        file_path: str,
        start_offset: int = 0,
        *,
        start_line_no: int | None = None,
        record_lines: bool = False,
    ) -> Iterator[TurnEvent]:
        """Stream TurnEvents from ``file_path`` starting at byte ``start_offset``.

        ``start_line_no`` is the 1-based line number of the line beginning at
        ``start_offset``; when omitted and ``start_offset > 0`` it is computed
        by counting newlines in the prefix (exact, at the cost of re-reading
        the prefix bytes). ``start_offset`` must be a value previously returned
        as ``ParseStats.end_offset`` (or 0); an offset landing mid-line surfaces
        as a malformed_json count, not silence.

        A final line without a trailing newline is treated as possibly
        mid-append: it is never consumed (even if it happens to parse),
        ``truncated_final_line`` is set, and ``end_offset`` points at its start
        so a resumed parse picks it up once complete.

        ``record_lines=True`` also collects one content-free ``PhysicalLine``
        per complete line in ``parse_stats.line_records``.
        """
        stats = ParseStats(file_path=file_path, start_offset=start_offset)
        self.parse_stats = stats
        if start_line_no is None:
            start_line_no = (
                1 if start_offset == 0 else _count_lines_before(file_path, start_offset) + 1
            )
        return self._parse_gen(file_path, start_offset, start_line_no, stats, record_lines)

    def _parse_gen(
        self,
        file_path: str,
        start_offset: int,
        start_line_no: int,
        stats: ParseStats,
        record_lines: bool = False,
    ) -> Iterator[TurnEvent]:
        # tool_use id -> tool name, for naming tool_results. Best-effort within
        # this parse window: a result whose tool_use predates start_offset gets
        # tool_name "" (documented, not guessed).
        tool_names: dict[str, str] = {}
        offset = start_offset
        line_no = start_line_no - 1
        # Identity carried forward to later records for line attribution. It is
        # never carried backward, and a missing timestamp is never carried.
        carried: dict = {"session_id": "", "cwd": "", "headless": None}
        with open(file_path, "rb") as fh:
            fh.seek(start_offset)
            for raw in fh:
                if not raw.endswith(b"\n"):
                    # Last line of the file with no terminator: possibly still
                    # being appended. Stop cleanly; do not consume.
                    stats.truncated_final_line = True
                    break
                line_no += 1
                byte_start = offset
                offset += len(raw)
                stats.end_offset = offset
                stats.lines_attempted += 1
                # Content-free outcome of this physical line. The finally clause
                # records it after every branch, including each `continue`.
                category, cause, ts_raw, emitted = LINE_MALFORMED, "", "", 0
                try:
                    try:
                        text = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        stats._error("decode_error")
                        cause = "decode_error"
                        continue
                    try:
                        data = json.loads(text)
                    except json.JSONDecodeError:
                        stats._error("malformed_json")
                        if text.strip():
                            cause = "malformed_json"
                        else:
                            category = LINE_BLANK
                        continue
                    if not isinstance(data, dict):
                        stats._error("bad_structure:line_not_object")
                        cause = "bad_structure:line_not_object"
                        continue
                    if isinstance(data.get("timestamp"), str):
                        ts_raw = data["timestamp"]
                    _carry_identity(carried, data)
                    line_type = data.get("type")
                    if line_type is None:
                        stats._error("missing_type")
                        cause = "missing_type"
                        continue
                    if line_type in BOOKKEEPING_TYPES:
                        stats.lines_succeeded += 1
                        stats._skip(str(line_type))
                        category, cause = LINE_SKIPPED, str(line_type)
                        continue
                    if line_type == "user":
                        events = self._user_events(data, file_path, line_no, tool_names, stats)
                    elif line_type == "assistant":
                        events = self._assistant_events(data, file_path, line_no, tool_names, stats)
                    elif line_type == "system":
                        events = self._system_events(data, file_path, line_no, stats)
                    else:
                        stats._unknown(str(line_type))
                        category, cause = LINE_UNKNOWN_TYPE, str(line_type)
                        continue
                    if events is None:
                        cause = stats.last_error
                        continue  # structural failure already counted
                    stats.lines_succeeded += 1
                    if events:
                        category = LINE_EVENT
                    else:
                        category, cause = LINE_SKIPPED, f"{line_type}:no_events"
                    for ev in events:
                        emitted += 1
                        stats.events_emitted += 1
                        yield ev
                finally:
                    if record_lines:
                        stats.line_records.append(
                            PhysicalLine(
                                line_no=line_no,
                                byte_start=byte_start,
                                byte_end=offset,
                                sha256=hashlib.sha256(raw).hexdigest(),
                                ts_raw=ts_raw,
                                session_id=carried["session_id"],
                                project_path=carried["cwd"],
                                category=category,
                                cause=cause,
                                events=emitted,
                                headless=carried["headless"],
                            )
                        )
        stats.end_offset = max(stats.end_offset, start_offset)

    # -- per-line-type handlers -----------------------------------------

    def _envelope(self, data: dict, file_path: str, line_no: int) -> dict:
        """Common TurnEvent kwargs read from a user/assistant/system envelope."""
        return {
            "source": SOURCE_NAME,
            "session_file": file_path,
            "session_id": str(data.get("sessionId") or ""),
            "project_path": str(data.get("cwd") or ""),
            "ts_utc": str(data.get("timestamp") or ""),
            "headless": data.get("entrypoint") == "sdk-cli",
            "line_no": line_no,
        }

    def _base_meta(self, data: dict) -> dict:
        meta = {
            "uuid": data.get("uuid", ""),
            "parent_uuid": data.get("parentUuid", ""),
            "is_sidechain": bool(data.get("isSidechain", False)),
        }
        if data.get("agentId"):
            meta["agent_id"] = data["agentId"]
        return meta

    def _user_events(
        self,
        data: dict,
        file_path: str,
        line_no: int,
        tool_names: dict[str, str],
        stats: ParseStats,
    ) -> list[TurnEvent] | None:
        message = data.get("message")
        if not isinstance(message, dict):
            stats._error("bad_structure:user_message_not_object")
            return None
        content = message.get("content")
        env = self._envelope(data, file_path, line_no)
        meta = self._base_meta(data)

        if isinstance(content, str):
            if content.startswith(_SLASH_COMMAND_PREFIXES):
                return [
                    TurnEvent(**env, role=ROLE_SYSTEM, kind=KIND_SLASH_COMMAND, text=content, meta=meta)
                ]
            if content.startswith(_LOCAL_STDOUT_PREFIX):
                return [
                    TurnEvent(**env, role=ROLE_SYSTEM, kind="local_command_output", text=content, meta=meta)
                ]
            if content.startswith(_TASK_NOTIFICATION_PREFIX):
                return [
                    TurnEvent(**env, role=ROLE_SYSTEM, kind="task_notification", text=content, meta=meta)
                ]
            if content.startswith(_LOCAL_CAVEAT_PREFIX) or data.get("isMeta") is True:
                return [TurnEvent(**env, role=ROLE_SYSTEM, kind="meta", text=content, meta=meta)]
            # The verified human predicate: type==user, isMeta!=true, string
            # content, no machinery prefix.
            return [TurnEvent(**env, role=ROLE_HUMAN, kind=KIND_MESSAGE, text=content, meta=meta)]

        if isinstance(content, list):
            events: list[TurnEvent] = []
            texts: list[str] = []
            for item in content:
                if not isinstance(item, dict):
                    stats._skip("content_item:non_object")
                    continue
                item_type = item.get("type")
                if item_type == "tool_result":
                    tool_use_id = str(item.get("tool_use_id") or "")
                    events.append(
                        TurnEvent(
                            **env,
                            role=ROLE_TOOL_RESULT,
                            kind=KIND_TOOL_RESULT,
                            text=_tool_result_text(item.get("content"), stats),
                            tool_name=tool_names.get(tool_use_id, ""),
                            is_error=bool(item.get("is_error", False)),
                            meta={**meta, "tool_use_id": tool_use_id},
                        )
                    )
                elif item_type == "text":
                    texts.append(str(item.get("text") or ""))
                else:
                    stats._skip(f"content_item:{item_type}")
            if texts:
                # List-shaped user content with text items (paste/attachment
                # messages, or text riding alongside tool_results). Fails the
                # "content is text" clause of the human predicate, so kept as
                # system, not human — surfaced under a dedicated kind rather
                # than dropped.
                events.append(
                    TurnEvent(
                        **env,
                        role=ROLE_SYSTEM,
                        kind="user_structured",
                        text="\n".join(texts),
                        meta=meta,
                    )
                )
            return events

        stats._error(f"bad_structure:user_content_{type(content).__name__}")
        return None

    def _assistant_events(
        self,
        data: dict,
        file_path: str,
        line_no: int,
        tool_names: dict[str, str],
        stats: ParseStats,
    ) -> list[TurnEvent] | None:
        message = data.get("message")
        if not isinstance(message, dict):
            stats._error("bad_structure:assistant_message_not_object")
            return None
        content = message.get("content")
        env = self._envelope(data, file_path, line_no)
        meta = self._base_meta(data)
        if isinstance(message.get("model"), str):
            meta["model"] = message["model"]

        if isinstance(content, str):
            # Accept plain assistant text as well as content-block lists.
            return [TurnEvent(**env, role=ROLE_ASSISTANT, kind=KIND_MESSAGE, text=content, meta=meta)]
        if not isinstance(content, list):
            stats._error(f"bad_structure:assistant_content_{type(content).__name__}")
            return None

        events: list[TurnEvent] = []
        for item in content:
            if not isinstance(item, dict):
                stats._skip("content_item:non_object")
                continue
            item_type = item.get("type")
            if item_type == "text":
                events.append(
                    TurnEvent(
                        **env,
                        role=ROLE_ASSISTANT,
                        kind=KIND_MESSAGE,
                        text=str(item.get("text") or ""),
                        meta=meta,
                    )
                )
            elif item_type == "tool_use":
                name = str(item.get("name") or "")
                tool_use_id = str(item.get("id") or "")
                if tool_use_id:
                    tool_names[tool_use_id] = name
                events.append(
                    TurnEvent(
                        **env,
                        role=ROLE_ASSISTANT,
                        kind=KIND_TOOL_USE,
                        text=json.dumps(
                            item.get("input", {}), ensure_ascii=False, sort_keys=True
                        ),
                        tool_name=name,
                        meta={**meta, "tool_use_id": tool_use_id},
                    )
                )
            elif item_type in ("thinking", "redacted_thinking"):
                stats._skip(str(item_type))
            else:
                stats._skip(f"content_item:{item_type}")
        return events

    def _system_events(
        self, data: dict, file_path: str, line_no: int, stats: ParseStats
    ) -> list[TurnEvent] | None:
        """type=="system" lines: bookkeeping subtypes (turn_duration) carry no
        content and are skip-counted; drift variants with string content are
        kept as system events rather than dropped."""
        content = data.get("content")
        if isinstance(content, str) and content:
            env = self._envelope(data, file_path, line_no)
            meta = self._base_meta(data)
            if data.get("subtype"):
                meta["subtype"] = data["subtype"]
            return [TurnEvent(**env, role=ROLE_SYSTEM, kind="system", text=content, meta=meta)]
        stats._skip("system")
        return []


def _tool_result_text(content: object, stats: ParseStats) -> str:
    """Normalize a tool_result payload's str-or-list content union to text.

    Non-text items (images, tool_references) are replaced with an explicit
    marker naming what was cut, and counted in skipped_taxonomy.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
            else:
                item_type = item.get("type") if isinstance(item, dict) else type(item).__name__
                stats._skip(f"tool_result_item:{item_type}")
                parts.append(f"[non-text tool_result item: {item_type}]")
        return "\n".join(parts)
    if content is None:
        return ""
    stats._skip(f"tool_result_content:{type(content).__name__}")
    return f"[unhandled tool_result content type: {type(content).__name__}]"


def _carry_identity(carried: dict, data: dict) -> None:
    """Advance session, cwd, and headless state from one record.

    Bookkeeping records often lack cwd or entrypoint; later line attribution
    reuses the most recent value an earlier record established.
    """
    session_id = data.get("sessionId")
    if isinstance(session_id, str) and session_id:
        carried["session_id"] = session_id
    cwd = data.get("cwd")
    if isinstance(cwd, str) and cwd:
        carried["cwd"] = cwd
    if "entrypoint" in data:
        carried["headless"] = data.get("entrypoint") == "sdk-cli"


def _count_lines_before(file_path: str, offset: int) -> int:
    """Count newline-terminated lines in the first ``offset`` bytes."""
    count = 0
    remaining = offset
    with open(file_path, "rb") as fh:
        while remaining > 0:
            chunk = fh.read(min(remaining, 1 << 20))
            if not chunk:
                break
            count += chunk.count(b"\n")
            remaining -= len(chunk)
    return count
