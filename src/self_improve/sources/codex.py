"""Codex rollout source: ~/.codex/sessions + ~/.codex/archived_sessions.

Supported payload shapes include interactive, headless, resumed, and archived
sessions. The regression fixtures use replacement text and placeholder
identities. Line envelope:

    {"timestamp": "<ISO-8601 Z>", "type": "<record_type>", "payload": {...}}

Observed record types: ``session_meta``, ``response_item``, ``event_msg``,
``turn_context``, ``compacted``, ``world_state``,
``inter_agent_communication_metadata``. ``event_msg/token_count`` records do not
contribute transcript events; a substring check skips compact records before
json.loads (see ``_TOKEN_COUNT_NEEDLE``).

Headless / `codex exec` detection:

    originator values observed: "codex_exec"   -> headless `codex exec` run
                                "Codex Desktop"-> desktop app / IDE
    source values observed:     "exec"    (str)  -> exec-mode thread
                                "vscode"  (str)  -> interactive IDE/Desktop chat
                                {"subagent": {...}} (dict) -> machine-spawned
                                    subagent thread; variants seen:
                                    {"thread_spawn": {"parent_thread_id": ...}}
                                    and {"other": "guardian"}
    RULE: headless = originator == "codex_exec" OR source == "exec" OR
          source is a dict containing "subagent". A subagent thread counts as
          headless because its user_message stream is machine-generated.

Error detection for tool outputs (empirical markers, from the fixed header the
Codex harness prepends to every output — never from body text):

    function_call_output (exec_command): header "Chunk ID: ...\\nWall time: ...\\n
        Process exited with code N" -> N != 0 is an error.
    custom_tool_call_output (name "exec", JS-source input): first line
        "Script completed" (ok) vs "Script failed" (error); older shape
        "Exit code: N" -> N != 0 is an error.
    collaboration tools: outputs starting "collab spawn failed:" /
        "collab tool failed:" are errors.

Fernet-encrypted inter-agent payloads (opaque, skipped as ``skipped_encrypted``):
    - response_item/function_call name=="send_message" whose arguments JSON
      carries a Fernet token (always prefixed "gAAAAAB").
    - response_item/agent_message whose content list has a block
      {"type": "encrypted_content", "encrypted_content": "gAAAAAB..."}.

Schema drift handled: older archived session_meta payloads carry only
``id`` (no ``session_id`` key); one rollout file may hold many session_meta
records after resume or fork, so session_id/cwd/headless are
re-read from each. ``custom_tool_call_output.output`` (and rarely
``function_call_output.output``) is a string-or-list union where the list holds
{"type": "input_text", "text": ...} blocks.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from ..config import Config
from .base import (
    KIND_MESSAGE,
    KIND_PATCH_APPLY,
    KIND_TOOL_RESULT,
    KIND_TOOL_USE,
    LINE_BLANK,
    LINE_ENCRYPTED,
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

# Kept as a module-level alias so tests can wrap it with a counter and prove
# token_count lines never reach the JSON parser.
_loads = json.loads

# Tight needle: real rollout lines are compact JSON (no space after ':'), and a
# quote character can never appear UNESCAPED inside a JSON string value, so
# this needle only matches an actual token_count record — never a transcript
# that merely *talks about* token_count, which a loose b"token_count" needle
# would also match. If Codex emits spaced
# JSON the line falls through to json.loads and is still counted via the
# event_msg dispatch below — no silent behavior change.
_TOKEN_COUNT_NEEDLE = b'"type":"token_count"'

# Fernet tokens are version byte 0x80 + timestamp, so base64 always starts
# "gAAAAAB" (verified on real send_message payloads).
_FERNET_PREFIX = "gAAAAAB"

# Exit-status markers scanned ONLY in the harness header region (first
# _HEADER_CHARS chars) so quoted phrases in command output can't false-match.
_EXIT_RE = re.compile(r"^(?:Process exited with code|Exit code:)\s+(\d+)\s*$", re.M)
_HEADER_CHARS = 200
_FAIL_PREFIXES = ("Script failed", "collab spawn failed:", "collab tool failed:")

# Top-level record types known to carry nothing mined. Any other unhandled
# type is reported as unknown in line records (stats keep their old grouping).
_KNOWN_SKIPPED_RECORD_TYPES = frozenset(
    {"turn_context", "compacted", "world_state", "inter_agent_communication_metadata"}
)
_KNOWN_SKIPPED_EVENT_TYPES = frozenset(
    {"task_started", "task_complete", "turn_aborted", "context_compacted", "agent_reasoning"}
)
_KNOWN_SKIPPED_RESPONSE_TYPES = frozenset({"message", "reasoning"})

@dataclass
class CodexParseStats:
    """Progress + error taxonomy for one parse() consumption.

    attempted = lines_attempted; succeeded = lines_attempted - malformed_lines;
    failed = malformed_lines. malformed_taxonomy counts *issues*, which is a
    superset: line-level failures (unicode_error/json_error/bad_envelope) AND
    field-level degradations on lines that still parsed (session_meta_no_cwd,
    session_meta_no_id, tool_output_missing), so its sum can exceed
    malformed_lines.
    """

    lines_attempted: int = 0        # complete lines seen (incl. token_count)
    token_count_skipped: int = 0    # skipped pre-json.loads (or via dispatch)
    malformed_lines: int = 0
    malformed_taxonomy: Counter = field(default_factory=Counter)
    events_emitted: int = 0
    skipped_by_type: Counter = field(default_factory=Counter)  # non-mined record kinds
    skipped_encrypted: int = 0      # opaque Fernet inter-agent payloads
    denylisted_events: int = 0      # emitted but marked meta["denylisted"]=True
    missing_timestamp: int = 0      # events emitted with ts_utc==""
    output_shape_other: int = 0     # tool output neither str nor block-list
    unmatched_call_id: int = 0      # tool output with no seen matching call
    truncated_tail: bool = False    # file ended mid-line (still being appended)
    bytes_consumed: int = 0         # absolute offset AFTER last complete line
    lines_consumed: int = 0         # complete lines consumed in THIS parse
    last_line_no: int = 0           # absolute 1-based line number of last line
    # Content-free physical-line records, collected only when parse() is called
    # with record_lines=True (scan measurement).
    line_records: list[PhysicalLine] = field(default_factory=list, repr=False)


@dataclass
class CodexDiscoverStats:
    """Progress for one discover() run."""

    session_files: int = 0
    archived_files: int = 0
    archived_dir_missing: bool = False  # surfaced, since archived dir is optional


def _output_to_text(output: object) -> tuple[str, bool, bool]:
    """Normalize a tool output union to text.

    Returns (text, has_encrypted_block, shape_other). Observed shapes: plain
    str; list of {"type": "input_text"|"encrypted_content", ...} blocks. Any
    other shape is dumped losslessly to JSON and flagged shape_other so the
    caller can count it (surfaced, never guessed away).
    """
    if isinstance(output, str):
        return output, False, False
    if isinstance(output, list):
        parts: list[str] = []
        encrypted = False
        for block in output:
            if isinstance(block, dict):
                if block.get("type") == "encrypted_content":
                    encrypted = True
                elif isinstance(block.get("text"), str):
                    parts.append(block["text"])
                else:
                    parts.append(json.dumps(block, ensure_ascii=False))
            else:
                parts.append(str(block))
        return "".join(parts), encrypted, False
    return json.dumps(output, ensure_ascii=False), False, True


def _output_is_error(text: str) -> bool:
    """Empirical error markers in the harness-written output header only."""
    header = text[:_HEADER_CHARS]
    if header.startswith(_FAIL_PREFIXES):
        return True
    m = _EXIT_RE.search(header)
    if m and int(m.group(1)) != 0:
        return True
    return False


def _classify_headless(originator: object, source: object) -> tuple[bool, bool]:
    """(headless, is_subagent) per the documented empirical rule."""
    is_subagent = isinstance(source, dict) and "subagent" in source
    headless = originator == "codex_exec" or source == "exec" or is_subagent
    return headless, is_subagent


def _source_label(source: object) -> str:
    """Flatten the string-or-object source union to a short label for meta."""
    if isinstance(source, str):
        return source
    if isinstance(source, dict):
        return "subagent" if "subagent" in source else json.dumps(source, ensure_ascii=False)
    return repr(source)


class CodexSource:
    """SessionSource implementation for Codex rollout transcripts."""

    name = "codex"
    #: parse(record_lines=True) reports PhysicalLine records for measurement.
    supports_line_records = True

    def __init__(self, config: Config):
        self.config = config
        self.parse_stats: CodexParseStats | None = None
        self.last_discover_stats: CodexDiscoverStats | None = None

    # ------------------------------------------------------------------
    # discover
    # ------------------------------------------------------------------

    def discover(self) -> Iterator[SessionFileInfo]:
        """Yield every rollout file on disk.

        Layout: ``<sessions_dir>/YYYY/MM/DD/rollout-*.jsonl`` (recursive) plus
        flat ``<archived_dir>/*.jsonl``. The cwd-based denylist CANNOT be
        applied here (a rollout's cwd lives inside session_meta records);
        parse() marks each event with meta["denylisted"] and the scan layer
        drops them. project_slug is "" for the same reason; is_subagent is
        only knowable from session_meta, so it is False here and carried on
        parsed events' meta instead.

        Fail-loud: a missing sessions dir raises. A missing archived dir is
        tolerated (config.validate() deliberately does not require it — fresh
        codex installs lack it) but surfaced via last_discover_stats.
        """
        stats = CodexDiscoverStats()
        self.last_discover_stats = stats

        sessions_dir = Path(self.config.codex_sessions_dir)
        if not sessions_dir.is_dir():
            raise FileNotFoundError(
                f"codex_sessions_dir does not exist: {sessions_dir}"
            )
        for p in sorted(sessions_dir.rglob("rollout-*.jsonl")):
            st = p.stat()
            stats.session_files += 1
            yield SessionFileInfo(
                source=self.name,
                file_path=str(p),
                mtime=st.st_mtime,
                size=st.st_size,
                project_slug="",
            )

        archived_dir = Path(self.config.codex_archived_dir)
        if not archived_dir.is_dir():
            stats.archived_dir_missing = True
            return
        for p in sorted(archived_dir.glob("*.jsonl")):
            st = p.stat()
            stats.archived_files += 1
            yield SessionFileInfo(
                source=self.name,
                file_path=str(p),
                mtime=st.st_mtime,
                size=st.st_size,
                project_slug="",
            )

    # ------------------------------------------------------------------
    # parse
    # ------------------------------------------------------------------

    def parse(
        self,
        file_path: str,
        start_offset: int = 0,
        *,
        start_line: int = 0,
        resume_state: dict | None = None,
        record_lines: bool = False,
    ) -> Iterator[TurnEvent]:
        """Stream TurnEvents from file_path starting at byte start_offset.

        Protocol conformance: tolerates a truncated final line (file still
        being appended) by stopping cleanly with stats.truncated_tail=True and
        bytes_consumed excluding the partial line, so the next scan resumes at
        exactly that offset; malformed lines never raise — they are counted in
        malformed_lines with a taxonomy.

        Resume extras (keyword-only, defaulted — protocol-compatible):
        ``start_line`` is the count of lines already consumed before
        start_offset (keeps line_no absolute); ``resume_state`` optionally
        carries {"session_id", "cwd", "headless", "is_subagent"} persisted by
        the scan layer, since a mid-file resume starts after the session_meta
        record(s) that established them.

        Stats for the consumption are on self.parse_stats (created
        eagerly, filled as the returned iterator is drained).
        """
        stats = CodexParseStats()
        stats.bytes_consumed = start_offset
        stats.last_line_no = start_line
        self.parse_stats = stats
        state = resume_state or {}
        return self._parse_inner(
            file_path,
            start_offset,
            start_line,
            stats,
            session_id=str(state.get("session_id", "")),
            cwd=str(state.get("cwd", "")),
            headless=bool(state.get("headless", False)),
            is_subagent=bool(state.get("is_subagent", False)),
            identity_known=resume_state is not None,
            record_lines=record_lines,
        )

    def _parse_inner(
        self,
        file_path: str,
        start_offset: int,
        start_line: int,
        stats: CodexParseStats,
        *,
        session_id: str,
        cwd: str,
        headless: bool,
        is_subagent: bool,
        identity_known: bool = False,
        record_lines: bool = False,
    ) -> Iterator[TurnEvent]:
        denylist = self.config.denylist_substrings
        denylisted = any(s in cwd for s in denylist) if cwd else False
        # call_id -> tool name, for matching outputs to their calls.
        call_names: dict[str, str] = {}
        pos = start_offset
        line_no = start_line

        def make_event(
            role: str,
            kind: str,
            text: str,
            ts: str,
            *,
            tool_name: str = "",
            is_error: bool = False,
            meta: dict | None = None,
        ) -> TurnEvent:
            m = dict(meta or {})
            m["denylisted"] = denylisted
            if denylisted:
                stats.denylisted_events += 1
            stats.events_emitted += 1
            return TurnEvent(
                source=self.name,
                session_file=file_path,
                session_id=session_id,
                project_path=cwd,
                ts_utc=ts,
                role=role,
                kind=kind,
                text=text,
                tool_name=tool_name,
                is_error=is_error,
                headless=headless,
                line_no=line_no,
                meta=m,
            )

        with open(file_path, "rb") as fh:
            fh.seek(start_offset)
            for raw in fh:
                if not raw.endswith(b"\n"):
                    # Truncated final line: writer mid-append. Stop cleanly,
                    # leave bytes_consumed pointing at the partial line so the
                    # next scan re-reads it once complete.
                    stats.truncated_tail = True
                    break
                byte_start = pos
                pos += len(raw)
                line_no += 1
                stats.lines_attempted += 1
                stats.lines_consumed += 1
                stats.last_line_no = line_no
                stats.bytes_consumed = pos
                # Content-free outcome of this physical line. The finally clause
                # records it after every branch, including each `continue`.
                outcome = {"category": LINE_MALFORMED, "cause": "", "ts": ""}
                events_before = stats.events_emitted
                try:
                    if raw.strip() == b"":
                        stats.skipped_by_type["blank_line"] += 1
                        outcome["category"] = LINE_BLANK
                        continue
                    # The queue-only fast path intentionally avoids decoding.
                    # Measurement must validate the whole record before counting
                    # a physical line as eligible; a prefix cannot prove JSON.
                    if not record_lines and _TOKEN_COUNT_NEEDLE in raw:
                        stats.token_count_skipped += 1
                        outcome.update(category=LINE_SKIPPED, cause="event_msg/token_count")
                        continue

                    try:
                        text_line = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        stats.malformed_lines += 1
                        stats.malformed_taxonomy["unicode_error"] += 1
                        outcome["cause"] = "unicode_error"
                        continue
                    try:
                        d = _loads(text_line)
                    except (json.JSONDecodeError, ValueError):
                        stats.malformed_lines += 1
                        stats.malformed_taxonomy["json_error"] += 1
                        outcome["cause"] = "json_error"
                        continue
                    if not isinstance(d, dict) or "type" not in d:
                        stats.malformed_lines += 1
                        stats.malformed_taxonomy["bad_envelope"] += 1
                        outcome["cause"] = "bad_envelope"
                        continue

                    rtype = d["type"]
                    ts = d.get("timestamp", "")
                    if not isinstance(ts, str):
                        ts = ""
                    outcome["ts"] = ts
                    payload = d.get("payload")
                    if not isinstance(payload, dict):
                        payload = {}

                    if rtype == "session_meta":
                        # One file holds many of these (resume/fork). Old archived
                        # schema has only "id"; new schema has "session_id" (+ a
                        # duplicate "id").
                        sid = payload.get("session_id") or payload.get("id")
                        if not sid:
                            stats.malformed_lines += 1
                            stats.malformed_taxonomy["session_meta_no_id"] += 1
                            outcome["cause"] = "session_meta_no_id"
                            continue
                        session_id = str(sid)
                        identity_known = True
                        new_cwd = payload.get("cwd")
                        if isinstance(new_cwd, str):
                            cwd = new_cwd
                        else:
                            stats.malformed_taxonomy["session_meta_no_cwd"] += 1
                        denylisted = any(s in cwd for s in denylist)
                        originator = payload.get("originator")
                        source_val = payload.get("source")
                        headless, is_subagent = _classify_headless(originator, source_val)
                        if not ts:
                            stats.missing_timestamp += 1
                        outcome["category"] = LINE_EVENT
                        yield make_event(
                            ROLE_SYSTEM,
                            "session_meta",
                            "",
                            ts,
                            meta={
                                "originator": str(originator or ""),
                                "source": _source_label(source_val),
                                "is_subagent": is_subagent,
                            },
                        )
                        continue

                    if rtype == "event_msg":
                        ptype = payload.get("type")
                        if ptype == "token_count":
                            # Needle missed (e.g. spaced JSON) but it is still a
                            # token_count record — same skip, still counted.
                            stats.token_count_skipped += 1
                            outcome.update(category=LINE_SKIPPED, cause="event_msg/token_count")
                            continue
                        if ptype == "user_message":
                            # This stream is clean human text (or the submitted
                            # `codex exec` prompt). The injected AGENTS.md /
                            # environment blocks live in response_item "message"
                            # records, which are deliberately NOT mined.
                            msg = payload.get("message")
                            if not isinstance(msg, str):
                                stats.malformed_lines += 1
                                stats.malformed_taxonomy["user_message_no_text"] += 1
                                outcome["cause"] = "user_message_no_text"
                                continue
                            if not ts:
                                stats.missing_timestamp += 1
                            outcome["category"] = LINE_EVENT
                            yield make_event(ROLE_HUMAN, KIND_MESSAGE, msg, ts)
                            continue
                        if ptype == "agent_message":
                            msg = payload.get("message")
                            if not isinstance(msg, str):
                                stats.malformed_lines += 1
                                stats.malformed_taxonomy["agent_message_no_text"] += 1
                                outcome["cause"] = "agent_message_no_text"
                                continue
                            if not ts:
                                stats.missing_timestamp += 1
                            outcome["category"] = LINE_EVENT
                            yield make_event(
                                ROLE_ASSISTANT,
                                KIND_MESSAGE,
                                msg,
                                ts,
                                meta={"phase": str(payload.get("phase") or "")},
                            )
                            continue
                        if ptype == "patch_apply_end":
                            success = payload.get("success")
                            if not isinstance(success, bool):
                                stats.malformed_lines += 1
                                stats.malformed_taxonomy["patch_apply_no_success"] += 1
                                outcome["cause"] = "patch_apply_no_success"
                                continue
                            stdout = payload.get("stdout", "") or ""
                            stderr = payload.get("stderr", "") or ""
                            if not ts:
                                stats.missing_timestamp += 1
                            outcome["category"] = LINE_EVENT
                            yield make_event(
                                ROLE_TOOL_RESULT,
                                KIND_PATCH_APPLY,
                                stderr if not success else stdout,
                                ts,
                                tool_name="apply_patch",
                                is_error=not success,
                                meta={"call_id": str(payload.get("call_id") or "")},
                            )
                            continue
                        stats.skipped_by_type[f"event_msg/{ptype}"] += 1
                        outcome.update(
                            category=LINE_SKIPPED if ptype in _KNOWN_SKIPPED_EVENT_TYPES else LINE_UNKNOWN_TYPE,
                            cause=f"event_msg/{ptype}",
                        )
                        continue

                    if rtype == "response_item":
                        ptype = payload.get("type")
                        if ptype in ("function_call", "custom_tool_call"):
                            name = str(payload.get("name") or "")
                            call_id = str(payload.get("call_id") or "")
                            # function_call carries "arguments" (JSON string);
                            # custom_tool_call carries "input" (JS source for name
                            # "exec", patch text for name "apply_patch").
                            args = payload.get("arguments", payload.get("input", ""))
                            if not isinstance(args, str):
                                args = json.dumps(args, ensure_ascii=False)
                            if call_id:
                                call_names[call_id] = name
                            if name == "send_message" and _FERNET_PREFIX in args:
                                # Opaque Fernet inter-agent payload.
                                stats.skipped_encrypted += 1
                                outcome.update(
                                    category=LINE_ENCRYPTED, cause=f"response_item/{ptype}:send_message"
                                )
                                continue
                            if not ts:
                                stats.missing_timestamp += 1
                            outcome["category"] = LINE_EVENT
                            yield make_event(
                                ROLE_ASSISTANT,
                                KIND_TOOL_USE,
                                args,
                                ts,
                                tool_name=name,
                                meta={"call_id": call_id},
                            )
                            continue
                        if ptype in ("function_call_output", "custom_tool_call_output"):
                            call_id = str(payload.get("call_id") or "")
                            if "output" not in payload:
                                stats.malformed_taxonomy["tool_output_missing"] += 1
                            out_text, encrypted, shape_other = _output_to_text(
                                payload.get("output", "")
                            )
                            if shape_other:
                                stats.output_shape_other += 1
                            if encrypted:
                                stats.skipped_encrypted += 1
                                outcome.update(category=LINE_ENCRYPTED, cause=f"response_item/{ptype}")
                                continue
                            tool_name = call_names.get(call_id, "")
                            if call_id and not tool_name:
                                stats.unmatched_call_id += 1
                            if not ts:
                                stats.missing_timestamp += 1
                            outcome["category"] = LINE_EVENT
                            yield make_event(
                                ROLE_TOOL_RESULT,
                                KIND_TOOL_RESULT,
                                out_text,
                                ts,
                                tool_name=tool_name,
                                is_error=_output_is_error(out_text),
                                meta={"call_id": call_id},
                            )
                            continue
                        if ptype == "agent_message":
                            # Inter-agent message (author/recipient threads), not
                            # this session's assistant text; usually encrypted.
                            content = payload.get("content")
                            if isinstance(content, list) and any(
                                isinstance(b, dict) and b.get("type") == "encrypted_content"
                                for b in content
                            ):
                                stats.skipped_encrypted += 1
                                outcome.update(
                                    category=LINE_ENCRYPTED, cause="response_item/agent_message"
                                )
                            else:
                                stats.skipped_by_type["response_item/agent_message"] += 1
                                outcome.update(
                                    category=LINE_SKIPPED, cause="response_item/agent_message"
                                )
                            continue
                        # response_item/"message" (role user/developer/system) is
                        # injected context — AGENTS.md blocks, permissions,
                        # environment — and must NOT be mined as human text.
                        stats.skipped_by_type[f"response_item/{ptype}"] += 1
                        outcome.update(
                            category=LINE_SKIPPED if ptype in _KNOWN_SKIPPED_RESPONSE_TYPES else LINE_UNKNOWN_TYPE,
                            cause=f"response_item/{ptype}",
                        )
                        continue

                    # turn_context / compacted / world_state /
                    # inter_agent_communication_metadata / future record types.
                    stats.skipped_by_type[str(rtype)] += 1
                    if rtype in _KNOWN_SKIPPED_RECORD_TYPES:
                        outcome.update(category=LINE_SKIPPED, cause=str(rtype))
                    else:
                        outcome.update(category=LINE_UNKNOWN_TYPE, cause=str(rtype))
                finally:
                    if record_lines:
                        stats.line_records.append(
                            PhysicalLine(
                                line_no=line_no,
                                byte_start=byte_start,
                                byte_end=pos,
                                sha256=hashlib.sha256(raw).hexdigest(),
                                ts_raw=outcome["ts"],
                                session_id=session_id,
                                project_path=cwd,
                                category=outcome["category"],
                                cause=outcome["cause"],
                                events=stats.events_emitted - events_before,
                                headless=headless if identity_known else None,
                                is_subagent=is_subagent if identity_known else None,
                                denylisted=denylisted,
                            )
                        )
