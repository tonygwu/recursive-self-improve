"""Tests for the Codex rollout transcript source.

Fixtures under tests/fixtures/codex/ are real-structure rollout lines
(verified against live ~/.codex/sessions and ~/.codex/archived_sessions files
on 2026-08-15) with all content redacted and paths made generic. They are
compact JSON (no space after ':'), matching real rollout files — the
token_count pre-parse needle depends on that formatting.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import self_improve.sources.codex as codex_mod
from self_improve.config import Config
from self_improve.sources.base import (
    KIND_MESSAGE,
    KIND_PATCH_APPLY,
    KIND_TOOL_RESULT,
    KIND_TOOL_USE,
    ROLE_ASSISTANT,
    ROLE_HUMAN,
    ROLE_SYSTEM,
    ROLE_TOOL_RESULT,
)
from self_improve.sources.codex import CodexSource

FIXTURES = Path(__file__).parent / "fixtures" / "codex"
SID_A = "aaaaaaaa-aaaa-7aaa-8aaa-aaaaaaaaaaaa"
SID_B = "bbbbbbbb-bbbb-7bbb-8bbb-bbbbbbbbbbbb"
SID_C = "cccccccc-cccc-7ccc-8ccc-cccccccccccc"
SID_E = "eeeeeeee-eeee-7eee-8eee-eeeeeeeeeeee"
CWD_Y = "/Users/redacted/Code/project-y"
CWD_Z = "/Users/redacted/Code/project-z"

MAIN_NAME = "rollout-2026-08-05T10-00-00-aaaaaaaa-aaaa-7aaa-8aaa-aaaaaaaaaaaa.jsonl"
SUB_NAME = "rollout-2026-08-06T09-00-00-cccccccc-cccc-7ccc-8ccc-cccccccccccc.jsonl"
ARCH_NAME = "rollout-2026-02-21T09-15-39-eeeeeeee-eeee-7eee-8eee-eeeeeeeeeeee.jsonl"


def build_tree(tmp_path: Path) -> tuple[Path, Path]:
    """Materialize a fake ~/.codex tree: dated sessions tree + flat archive."""
    sessions = tmp_path / "sessions"
    day1 = sessions / "2026" / "08" / "05"
    day1.mkdir(parents=True)
    shutil.copy(FIXTURES / "main_session.jsonl", day1 / MAIN_NAME)
    day2 = sessions / "2026" / "08" / "06"
    day2.mkdir(parents=True)
    shutil.copy(FIXTURES / "subagent_source_object.jsonl", day2 / SUB_NAME)
    # Non-rollout entries must be ignored by the rollout-*.jsonl glob.
    (day1 / "stray.txt").write_text("not a rollout\n")
    (day1 / "notes.jsonl").write_text("{}\n")

    archived = tmp_path / "archived_sessions"
    archived.mkdir()
    shutil.copy(FIXTURES / "archived_old_schema.jsonl", archived / ARCH_NAME)
    return sessions, archived


def make_source(sessions: Path, archived: Path) -> CodexSource:
    return CodexSource(
        Config(codex_sessions_dir=str(sessions), codex_archived_dir=str(archived))
    )


def events_at(events, line_no):
    return [e for e in events if e.line_no == line_no]


# ----------------------------------------------------------------------
# discovery
# ----------------------------------------------------------------------


def test_discover_recursive_sessions_plus_flat_archive(tmp_path):
    sessions, archived = build_tree(tmp_path)
    src = make_source(sessions, archived)
    infos = list(src.discover())
    by_path = {i.file_path: i for i in infos}
    assert len(infos) == 3

    main = by_path[str(sessions / "2026" / "08" / "05" / MAIN_NAME)]
    assert main.source == "codex"
    assert main.project_slug == ""  # cwd only knowable from session_meta
    assert main.is_subagent is False  # ditto: resolved at parse time
    assert main.size == (FIXTURES / "main_session.jsonl").stat().st_size

    assert str(sessions / "2026" / "08" / "06" / SUB_NAME) in by_path
    assert str(archived / ARCH_NAME) in by_path

    stats = src.last_discover_stats
    assert stats.session_files == 2
    assert stats.archived_files == 1
    assert stats.archived_dir_missing is False


def test_discover_missing_sessions_dir_raises(tmp_path):
    src = make_source(tmp_path / "does-not-exist", tmp_path / "archived")
    with pytest.raises(FileNotFoundError, match="codex_sessions_dir does not exist"):
        list(src.discover())


def test_discover_missing_archived_dir_surfaced_not_fatal(tmp_path):
    sessions, _ = build_tree(tmp_path)
    src = make_source(sessions, tmp_path / "no-archive-here")
    infos = list(src.discover())
    assert len(infos) == 2  # session files still yielded
    assert src.last_discover_stats.archived_dir_missing is True
    assert src.last_discover_stats.archived_files == 0


# ----------------------------------------------------------------------
# parsing: stream classification
# ----------------------------------------------------------------------


@pytest.fixture()
def main_parsed(tmp_path):
    sessions, archived = build_tree(tmp_path)
    src = make_source(sessions, archived)
    path = str(sessions / "2026" / "08" / "05" / MAIN_NAME)
    events = list(src.parse(path))
    return src, path, events


def test_parse_main_classification(main_parsed):
    _, _, events = main_parsed
    assert len(events) == 11

    (meta1,) = events_at(events, 1)
    assert (meta1.role, meta1.kind) == (ROLE_SYSTEM, "session_meta")
    assert meta1.text == ""
    assert meta1.meta["originator"] == "Codex Desktop"
    assert meta1.meta["source"] == "vscode"
    assert meta1.meta["is_subagent"] is False

    (human,) = events_at(events, 5)
    assert (human.role, human.kind) == (ROLE_HUMAN, KIND_MESSAGE)
    assert human.text == "Redacted human request text."

    (agent,) = events_at(events, 7)
    assert (agent.role, agent.kind) == (ROLE_ASSISTANT, KIND_MESSAGE)
    assert agent.text == "Redacted assistant reply."
    assert agent.meta["phase"] == "final"

    (fn_call,) = events_at(events, 8)
    assert (fn_call.role, fn_call.kind) == (ROLE_ASSISTANT, KIND_TOOL_USE)
    assert fn_call.tool_name == "exec_command"
    assert json.loads(fn_call.text) == {"cmd": "redacted-command"}
    assert fn_call.meta["call_id"] == "call-1"

    (fn_out,) = events_at(events, 9)
    assert (fn_out.role, fn_out.kind) == (ROLE_TOOL_RESULT, KIND_TOOL_RESULT)
    assert fn_out.tool_name == "exec_command"  # resolved via call_id map
    assert fn_out.is_error is False  # "Process exited with code 0"
    assert fn_out.text.startswith("Chunk ID: 000000\nWall time:")

    (ct_call,) = events_at(events, 10)
    assert (ct_call.role, ct_call.kind) == (ROLE_ASSISTANT, KIND_TOOL_USE)
    assert ct_call.tool_name == "exec"
    assert ct_call.text == "// redacted js source"

    (ct_out,) = events_at(events, 11)
    assert (ct_out.role, ct_out.kind) == (ROLE_TOOL_RESULT, KIND_TOOL_RESULT)
    assert ct_out.tool_name == "exec"
    # list-of-blocks output union normalized to text; "Script failed" header
    assert ct_out.text == "Script failed\nredacted error detail"
    assert ct_out.is_error is True

    (patch_fail,) = events_at(events, 12)
    assert (patch_fail.role, patch_fail.kind) == (ROLE_TOOL_RESULT, KIND_PATCH_APPLY)
    assert patch_fail.tool_name == "apply_patch"
    assert patch_fail.is_error is True
    assert patch_fail.text == "redacted patch failure"  # stderr on failure
    assert patch_fail.meta["call_id"] == "call-3"

    (patch_ok,) = events_at(events, 13)
    assert patch_ok.is_error is False
    assert patch_ok.text.startswith("Success. Updated the following files:")

    (meta2,) = events_at(events, 16)
    assert (meta2.role, meta2.kind) == (ROLE_SYSTEM, "session_meta")

    (human2,) = events_at(events, 17)
    assert (human2.role, human2.kind) == (ROLE_HUMAN, KIND_MESSAGE)
    assert human2.text == "Redacted exec prompt."


def test_parse_envelope_and_timestamps(main_parsed):
    _, path, events = main_parsed
    for e in events:
        assert e.source == "codex"
        assert e.session_file == path
        assert e.ts_utc.startswith("2026-08-05T1")
        assert e.ts_utc.endswith("Z")
        assert e.meta["denylisted"] is False


def test_parse_stats_taxonomy(main_parsed):
    src, path, _ = main_parsed
    stats = src.parse_stats
    assert stats.lines_attempted == 18
    assert stats.lines_consumed == 18
    assert stats.last_line_no == 18
    assert stats.events_emitted == 11
    assert stats.token_count_skipped == 2
    assert stats.malformed_lines == 1
    assert dict(stats.malformed_taxonomy) == {"json_error": 1}
    assert dict(stats.skipped_by_type) == {
        "event_msg/task_started": 1,
        "response_item/message": 1,
        "response_item/reasoning": 1,
        "world_state": 1,
    }
    assert stats.skipped_encrypted == 0
    assert stats.denylisted_events == 0
    assert stats.missing_timestamp == 0
    assert stats.output_shape_other == 0
    assert stats.unmatched_call_id == 0
    assert stats.truncated_tail is False
    assert stats.bytes_consumed == Path(path).stat().st_size


def test_malformed_line_counted_not_fatal(main_parsed):
    """Line 14 is invalid JSON; parsing continues and later events still emit."""
    src, _, events = main_parsed
    assert src.parse_stats.malformed_taxonomy["json_error"] == 1
    # Events AFTER the malformed line prove the parse did not stop.
    assert events_at(events, 16) and events_at(events, 17)


# ----------------------------------------------------------------------
# token_count is skipped BEFORE json.loads
# ----------------------------------------------------------------------


def test_token_count_skipped_pre_json_loads(tmp_path, monkeypatch):
    """The module keeps json.loads as the `_loads` alias precisely so tests
    can wrap it and prove token_count lines never reach the JSON parser."""
    sessions, archived = build_tree(tmp_path)
    src = make_source(sessions, archived)
    path = str(sessions / "2026" / "08" / "05" / MAIN_NAME)

    seen: list[str] = []

    def counting_loads(s, *args, **kwargs):
        assert '"type":"token_count"' not in s, "token_count line reached json.loads"
        seen.append(s)
        return json.loads(s, *args, **kwargs)

    monkeypatch.setattr(codex_mod, "_loads", counting_loads)
    list(src.parse(path))
    stats = src.parse_stats
    assert stats.token_count_skipped == 2
    # 18 lines - 2 token_count skipped pre-parse = 16 lines actually parsed.
    assert len(seen) == 16


# ----------------------------------------------------------------------
# multi-session_meta: session_id / cwd / headless re-read from each record
# ----------------------------------------------------------------------


def test_multi_session_meta_updates_session_id_cwd_headless(main_parsed):
    _, _, events = main_parsed
    before = [e for e in events if e.line_no < 16]
    after = [e for e in events if e.line_no >= 16]
    assert before and after

    for e in before:
        assert e.session_id == SID_A
        assert e.project_path == CWD_Y
        assert e.headless is False  # Codex Desktop + "vscode"
    for e in after:
        assert e.session_id == SID_B
        assert e.project_path == CWD_Z
        assert e.headless is True  # codex_exec + "exec"

    (meta2,) = events_at(events, 16)
    assert meta2.meta["originator"] == "codex_exec"
    assert meta2.meta["source"] == "exec"
    assert meta2.meta["is_subagent"] is False


# ----------------------------------------------------------------------
# headless detection: source-as-object subagent variant
# ----------------------------------------------------------------------


def test_subagent_source_object_is_headless(tmp_path):
    sessions, archived = build_tree(tmp_path)
    src = make_source(sessions, archived)
    path = str(sessions / "2026" / "08" / "06" / SUB_NAME)
    events = list(src.parse(path))
    stats = src.parse_stats

    # session_meta + machine-generated user_message; both Fernet inter-agent
    # lines (send_message call, encrypted agent_message) are opaque skips.
    assert len(events) == 2
    (meta,) = events_at(events, 1)
    assert meta.meta["is_subagent"] is True
    assert meta.meta["source"] == "subagent"  # dict union flattened to label
    assert meta.meta["originator"] == "Codex Desktop"
    (human,) = events_at(events, 2)
    assert human.role == ROLE_HUMAN
    assert human.headless is True  # subagent stream is machine-generated
    assert human.session_id == SID_C
    assert stats.skipped_encrypted == 2


# ----------------------------------------------------------------------
# archived old schema: session_meta carries only "id"
# ----------------------------------------------------------------------


def test_archived_old_schema_id_fallback(tmp_path):
    sessions, archived = build_tree(tmp_path)
    src = make_source(sessions, archived)
    events = list(src.parse(str(archived / ARCH_NAME)))
    assert len(events) == 2
    assert all(e.session_id == SID_E for e in events)
    assert events[0].kind == "session_meta"
    assert events[1].role == ROLE_HUMAN
    assert events[1].headless is False
    assert events[1].project_path == "/Users/redacted/Code/old-project"
    assert src.parse_stats.malformed_lines == 0


# ----------------------------------------------------------------------
# truncated final line + byte-offset resume
# ----------------------------------------------------------------------


def test_truncated_final_line_stops_cleanly_then_resumes(tmp_path):
    path = tmp_path / "rollout-truncated.jsonl"
    shutil.copy(FIXTURES / "truncated.jsonl", path)
    raw = path.read_bytes()
    first_line_len = raw.index(b"\n") + 1

    src = make_source(tmp_path, tmp_path)
    events = list(src.parse(str(path)))
    stats = src.parse_stats
    assert [e.kind for e in events] == ["session_meta"]
    assert stats.truncated_tail is True
    assert stats.lines_attempted == 1  # the partial line is NOT counted
    assert stats.malformed_lines == 0  # a truncated tail is NOT malformed
    assert stats.bytes_consumed == first_line_len
    assert stats.last_line_no == 1

    # Complete the interrupted line and resume at exactly the reported offset.
    partial = raw[first_line_len:]
    completion = (FIXTURES / "truncated_completion.jsonl").read_bytes()
    assert completion.startswith(partial)
    with open(path, "ab") as fh:
        fh.write(completion[len(partial):])

    resumed = list(
        src.parse(
            str(path),
            start_offset=stats.bytes_consumed,
            start_line=stats.last_line_no,
            resume_state={
                "session_id": SID_A,
                "cwd": CWD_Y,
                "headless": False,
                "is_subagent": False,
            },
        )
    )
    rstats = src.parse_stats
    assert len(resumed) == 1
    assert resumed[0].role == ROLE_ASSISTANT
    assert resumed[0].text == "Redacted acknowledged."
    assert resumed[0].line_no == 2  # absolute, thanks to start_line
    assert resumed[0].session_id == SID_A  # carried via resume_state
    assert resumed[0].project_path == CWD_Y
    assert rstats.truncated_tail is False
    assert rstats.lines_consumed == 1
    assert rstats.bytes_consumed == path.stat().st_size


def test_resume_offset_after_append_yields_only_new_events(tmp_path):
    sessions, archived = build_tree(tmp_path)
    path = sessions / "2026" / "08" / "05" / MAIN_NAME
    src = make_source(sessions, archived)
    first_pass = list(src.parse(str(path)))
    assert len(first_pass) == 11
    end = src.parse_stats.bytes_consumed
    last_line = src.parse_stats.last_line_no
    assert end == path.stat().st_size

    appended = [
        {"timestamp": "2026-08-05T11:00:03.000Z", "type": "event_msg",
         "payload": {"type": "token_count", "info": None}},
        {"timestamp": "2026-08-05T11:00:04.000Z", "type": "event_msg",
         "payload": {"type": "agent_message", "message": "Redacted follow-up.",
                     "phase": "final"}},
    ]
    with open(path, "a") as fh:
        for row in appended:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")

    resumed = list(
        src.parse(
            str(path),
            start_offset=end,
            start_line=last_line,
            resume_state={
                "session_id": SID_B,
                "cwd": CWD_Z,
                "headless": True,
                "is_subagent": False,
            },
        )
    )
    stats = src.parse_stats
    assert len(resumed) == 1  # ONLY the new agent_message; nothing re-emitted
    assert resumed[0].role == ROLE_ASSISTANT
    assert resumed[0].text == "Redacted follow-up."
    assert resumed[0].line_no == 20  # absolute line number continues
    assert resumed[0].session_id == SID_B
    assert resumed[0].project_path == CWD_Z
    assert resumed[0].headless is True
    assert stats.lines_consumed == 2
    assert stats.token_count_skipped == 1
    assert stats.malformed_lines == 0
    assert stats.bytes_consumed == path.stat().st_size


def test_empty_file(tmp_path):
    path = tmp_path / "rollout-empty.jsonl"
    path.write_bytes(b"")
    src = make_source(tmp_path, tmp_path)
    assert list(src.parse(str(path))) == []
    stats = src.parse_stats
    assert stats.lines_attempted == 0
    assert stats.bytes_consumed == 0
    assert stats.truncated_tail is False


# ----------------------------------------------------------------------
# structural failures are counted with a taxonomy, never raised
# ----------------------------------------------------------------------


def test_bad_structure_counted_not_raised(tmp_path):
    path = tmp_path / "rollout-bad.jsonl"
    rows = [
        b'["a","json","array"]',  # not a dict -> bad_envelope
        b'{"timestamp":"2026-08-05T10:00:00.000Z","payload":{}}',  # no type
        b'{"timestamp":"t","type":"session_meta","payload":{"cwd":"/x"}}',  # no id
        b'{"timestamp":"t","type":"event_msg","payload":{"type":"user_message","message":123}}',
        b'{"timestamp":"t","type":"event_msg","payload":{"type":"patch_apply_end","success":"yes"}}',
        b'\xff\xfe not utf-8',
        b'',  # blank line: skipped, not malformed
    ]
    path.write_bytes(b"\n".join(rows) + b"\n")

    src = make_source(tmp_path, tmp_path)
    events = list(src.parse(str(path)))
    stats = src.parse_stats
    assert events == []
    assert stats.lines_attempted == 7
    assert stats.malformed_lines == 6
    assert dict(stats.malformed_taxonomy) == {
        "bad_envelope": 2,
        "session_meta_no_id": 1,
        "user_message_no_text": 1,
        "patch_apply_no_success": 1,
        "unicode_error": 1,
    }
    assert stats.skipped_by_type["blank_line"] == 1
    assert stats.events_emitted == 0


# ----------------------------------------------------------------------
# denylist marking (parse marks, scan layer drops)
# ----------------------------------------------------------------------


def test_denylisted_cwd_marks_events(tmp_path):
    path = tmp_path / "rollout-deny.jsonl"
    rows = [
        {"timestamp": "2026-08-05T10:00:00.000Z", "type": "session_meta",
         "payload": {"id": SID_A, "session_id": SID_A,
                     "cwd": "/private/tmp/eval-tree/workdir",
                     "originator": "codex_exec", "source": "exec"}},
        {"timestamp": "2026-08-05T10:00:01.000Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": "Redacted."}},
    ]
    with open(path, "w") as fh:
        for row in rows:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")

    src = make_source(tmp_path, tmp_path)
    events = list(src.parse(str(path)))
    assert len(events) == 2
    assert all(e.meta["denylisted"] is True for e in events)
    assert src.parse_stats.denylisted_events == 2
