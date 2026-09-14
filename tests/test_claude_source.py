"""Tests for the Claude Code transcript source.

Fixtures retain transcript formats with replacement text and placeholder
identities. Tests build a temporary projects tree to exercise discovery,
configured exclusions, and subagent layout without personal history.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from self_improve.config import Config
from self_improve.sources.base import (
    KIND_MESSAGE,
    KIND_SLASH_COMMAND,
    KIND_TOOL_RESULT,
    KIND_TOOL_USE,
    ROLE_ASSISTANT,
    ROLE_HUMAN,
    ROLE_SYSTEM,
    ROLE_TOOL_RESULT,
)
from self_improve.sources.claude_code import (
    ClaudeCodeSource,
    ClaudeSourceError,
    matches_denylist,
)

FIXTURES = Path(__file__).parent / "fixtures" / "claude"
SID = "11111111-1111-1111-1111-111111111111"
HSID = "22222222-2222-2222-2222-222222222222"
MAIN_SLUG = "-Users-redacted-Code-project-x"
HEADLESS_SLUG = "-Users-redacted-Code-headless-proj"
DENYLISTED_SLUG = "-Users-example-Code-invented-eval-project-run1"


def build_tree(tmp_path: Path) -> Path:
    """Materialize a fake ~/.claude/projects tree from the fixtures."""
    root = tmp_path / "projects"
    main_dir = root / MAIN_SLUG
    main_dir.mkdir(parents=True)
    shutil.copy(FIXTURES / "main_cli.jsonl", main_dir / f"{SID}.jsonl")
    sub_dir = main_dir / SID / "subagents"
    sub_dir.mkdir(parents=True)
    shutil.copy(FIXTURES / "subagent.jsonl", sub_dir / "agent-1.jsonl")

    headless_dir = root / HEADLESS_SLUG
    headless_dir.mkdir()
    shutil.copy(FIXTURES / "headless_sdk.jsonl", headless_dir / f"{HSID}.jsonl")

    deny_dir = root / DENYLISTED_SLUG
    deny_dir.mkdir()
    shutil.copy(FIXTURES / "headless_sdk.jsonl", deny_dir / f"{HSID}.jsonl")

    (root / "stray.txt").write_text("not a slug dir\n")
    return root


def make_source(root: Path) -> ClaudeCodeSource:
    return ClaudeCodeSource(Config(claude_projects_dir=str(root),
        denylist_substrings=(*Config().denylist_substrings, "invented-eval-project")))


# ----------------------------------------------------------------------
# discovery
# ----------------------------------------------------------------------


def test_discover_finds_main_and_subagent_files(tmp_path):
    root = build_tree(tmp_path)
    src = make_source(root)
    infos = list(src.discover())
    by_path = {i.file_path: i for i in infos}
    assert len(infos) == 3

    main = by_path[str(root / MAIN_SLUG / f"{SID}.jsonl")]
    assert main.source == "claude"
    assert main.project_slug == MAIN_SLUG
    assert main.is_subagent is False
    assert main.size == (FIXTURES / "main_cli.jsonl").stat().st_size

    sub = by_path[str(root / MAIN_SLUG / SID / "subagents" / "agent-1.jsonl")]
    assert sub.is_subagent is True
    assert sub.project_slug == MAIN_SLUG

    headless = by_path[str(root / HEADLESS_SLUG / f"{HSID}.jsonl")]
    assert headless.is_subagent is False

    stats = src.discover_stats
    assert stats.slugs_seen == 3
    assert stats.main_files == 2
    assert stats.subagent_files == 1
    assert stats.non_dir_entries == 1


def test_discover_applies_denylist_and_reports_it(tmp_path):
    root = build_tree(tmp_path)
    src = make_source(root)
    infos = list(src.discover())
    assert all(DENYLISTED_SLUG not in i.file_path for i in infos)
    assert src.discover_stats.slugs_denylisted == [DENYLISTED_SLUG]


@pytest.mark.parametrize("letter", ["b", "c", "d", "e", "f"])
def test_discover_refuses_any_mirrored_config_dir(tmp_path, letter):
    """Recognize the account-directory pattern instead of enumerating accounts."""
    mirror = tmp_path / f".claude-{letter}" / "projects"
    mirror.mkdir(parents=True)
    src = make_source(mirror)
    with pytest.raises(ClaudeSourceError, match="double-counts"):
        src.discover()


def test_the_real_default_config_dir_is_not_refused(tmp_path):
    """The guard must not swallow ~/.claude itself, which is the one to read."""
    real = tmp_path / ".claude" / "projects"
    real.mkdir(parents=True)
    make_source(real).discover()


def test_an_unrelated_directory_that_merely_starts_with_claude_is_not_refused(tmp_path):
    """`.claude-code-cache` is not an account mirror. Derive, do not prefix-match."""
    other = tmp_path / ".claude-code-cache" / "projects"
    other.mkdir(parents=True)
    make_source(other).discover()


def test_discover_missing_root_raises(tmp_path):
    src = make_source(tmp_path / "does-not-exist")
    with pytest.raises(ClaudeSourceError, match="does not exist"):
        src.discover()


def test_matches_denylist_covers_cwd_prefixes():
    cfg = Config(denylist_substrings=(*Config().denylist_substrings, "invented-eval-project"))
    assert matches_denylist(cfg, "/private/tmp/foo/workdir") == "/private/tmp/"
    assert matches_denylist(cfg, "-private-tmp-seo-exp-workdirs") == "-private-tmp-"
    assert matches_denylist(cfg, "/Users/example/Code/invented-eval-project/x") == "invented-eval-project"
    assert matches_denylist(cfg, "/Users/redacted/Code/normal-project") is None


# ----------------------------------------------------------------------
# parsing: classification
# ----------------------------------------------------------------------


@pytest.fixture()
def main_events(tmp_path):
    root = build_tree(tmp_path)
    src = make_source(root)
    path = str(root / MAIN_SLUG / f"{SID}.jsonl")
    events = list(src.parse(path))
    return src, path, events


def events_at(events, line_no):
    return [e for e in events if e.line_no == line_no]


def test_parse_main_classification(main_events):
    _, _, events = main_events
    assert len(events) == 12

    (caveat,) = events_at(events, 3)
    assert (caveat.role, caveat.kind) == (ROLE_SYSTEM, "meta")
    assert caveat.text.startswith("<local-command-caveat>")

    (slash,) = events_at(events, 4)
    assert (slash.role, slash.kind) == (ROLE_SYSTEM, KIND_SLASH_COMMAND)
    assert slash.text.startswith("<command-name>/status</command-name>")

    (stdout,) = events_at(events, 5)
    assert (stdout.role, stdout.kind) == (ROLE_SYSTEM, "local_command_output")

    (human,) = events_at(events, 6)
    assert (human.role, human.kind) == (ROLE_HUMAN, KIND_MESSAGE)
    assert human.text == "Please fix the failing test in parser.py"
    assert human.headless is False

    # assistant thinking + text -> exactly one event (the text)
    (a_text,) = events_at(events, 7)
    assert (a_text.role, a_text.kind) == (ROLE_ASSISTANT, KIND_MESSAGE)
    assert a_text.text == "I'll look at the parser."

    (bash_use,) = events_at(events, 8)
    assert (bash_use.role, bash_use.kind) == (ROLE_ASSISTANT, KIND_TOOL_USE)
    assert bash_use.tool_name == "Bash"
    assert json.loads(bash_use.text) == {"command": "ls tests/", "description": "List test files"}

    (ok_result,) = events_at(events, 9)
    assert (ok_result.role, ok_result.kind) == (ROLE_TOOL_RESULT, KIND_TOOL_RESULT)
    assert ok_result.is_error is False
    assert ok_result.tool_name == "Bash"  # resolved via tool_use_id map
    assert ok_result.text == "test_parser.py\ntest_utils.py"

    (read_use,) = events_at(events, 10)
    assert read_use.tool_name == "Read"

    (err_result,) = events_at(events, 11)
    assert err_result.is_error is True
    assert err_result.tool_name == "Read"
    assert err_result.text == "File does not exist."  # list-shaped content normalized

    (notif,) = events_at(events, 12)
    assert (notif.role, notif.kind) == (ROLE_SYSTEM, "task_notification")

    (structured,) = events_at(events, 20)
    assert (structured.role, structured.kind) == (ROLE_SYSTEM, "user_structured")
    assert structured.text == "pasted structured content (redacted)"

    (final,) = events_at(events, 21)
    assert (final.role, final.kind) == (ROLE_ASSISTANT, KIND_MESSAGE)


def test_parse_carries_envelope_fields(main_events):
    _, path, events = main_events
    for e in events:
        assert e.source == "claude"
        assert e.session_file == path
        assert e.session_id == SID
        assert e.project_path == "/Users/redacted/Code/project-x"
        assert e.ts_utc.startswith("2026-08-01T12:00:")
        assert e.ts_utc.endswith("Z")
        assert e.headless is False
    # meta carries lineage
    (human,) = events_at(events, 6)
    assert human.meta["uuid"] == "u-6"
    assert human.meta["parent_uuid"] == "u-5"
    assert human.meta["is_sidechain"] is False


def test_parse_stats_taxonomy(main_events):
    src, path, _ = main_events
    stats = src.parse_stats
    assert stats.file_path == path
    assert stats.lines_attempted == 21
    assert stats.lines_succeeded == 19
    # An unknown record type needs its own counter. Malformed JSON is a parse
    # failure; a future bookkeeping record must not be reported as corruption.
    assert stats.lines_failed == 1
    assert stats.events_emitted == 12
    assert stats.error_taxonomy == {"malformed_json": 1}
    # ...but the unknown type is still REPORTED, not swallowed. This is the
    # half that matters: a future record type that DOES carry human text must
    # be visible, and the only thing standing between "we skip it" and "we
    # silently lose evidence" is that this number reaches the run report.
    assert stats.unknown_lines == 1
    assert stats.unknown_taxonomy == {"totally-new-type": 1}
    assert stats.skipped_taxonomy == {
        "mode": 1,
        "file-history-snapshot": 1,
        "attachment": 1,
        "ai-title": 1,
        "last-prompt": 1,
        "queue-operation": 1,
        "system": 1,  # turn_duration bookkeeping, no content
        "thinking": 1,
    }
    assert stats.truncated_final_line is False
    assert stats.end_offset == Path(path).stat().st_size


def test_parse_headless_flag(tmp_path):
    root = build_tree(tmp_path)
    src = make_source(root)
    events = list(src.parse(str(root / HEADLESS_SLUG / f"{HSID}.jsonl")))
    assert len(events) == 2
    assert all(e.headless is True for e in events)
    assert events[0].role == ROLE_HUMAN
    assert events[1].role == ROLE_ASSISTANT
    assert {e.session_id for e in events} == {HSID}


def test_parse_subagent_sidechain_meta(tmp_path):
    root = build_tree(tmp_path)
    src = make_source(root)
    events = list(src.parse(str(root / MAIN_SLUG / SID / "subagents" / "agent-1.jsonl")))
    assert len(events) == 2
    # The subagent prompt satisfies the human predicate; is_sidechain/agent_id
    # in meta let downstream discount it.
    assert events[0].role == ROLE_HUMAN
    assert events[0].meta["is_sidechain"] is True
    assert events[0].meta["agent_id"] == "agent-1"


# ----------------------------------------------------------------------
# parsing: truncation and offset resume
# ----------------------------------------------------------------------


def test_truncated_final_line_stops_cleanly_then_resumes(tmp_path):
    path = tmp_path / "session.jsonl"
    shutil.copy(FIXTURES / "truncated.jsonl", path)
    raw = path.read_bytes()
    first_line_len = raw.index(b"\n") + 1

    src = make_source(tmp_path)  # root irrelevant for parse
    events = list(src.parse(str(path)))
    stats = src.parse_stats
    assert [e.role for e in events] == [ROLE_HUMAN]
    assert events[0].text == "Start of session"
    assert stats.truncated_final_line is True
    assert stats.lines_attempted == 1
    assert stats.lines_failed == 0  # a truncated tail is NOT malformed
    assert stats.end_offset == first_line_len

    # Complete the interrupted line and resume at the reported offset.
    partial = raw[first_line_len:]
    completion = (FIXTURES / "truncated_completion.jsonl").read_bytes()
    assert completion.startswith(partial)
    with open(path, "ab") as fh:
        fh.write(completion[len(partial):])

    resumed = list(src.parse(str(path), start_offset=stats.end_offset))
    rstats = src.parse_stats
    assert len(resumed) == 1
    assert resumed[0].role == ROLE_ASSISTANT
    assert resumed[0].text == "Acknowledged."
    assert resumed[0].line_no == 2  # line number recovered from prefix newlines
    assert rstats.lines_attempted == 1
    assert rstats.truncated_final_line is False
    assert rstats.end_offset == path.stat().st_size


def test_resume_offset_after_append(tmp_path):
    root = build_tree(tmp_path)
    path = root / MAIN_SLUG / f"{SID}.jsonl"
    src = make_source(root)
    list(src.parse(str(path)))
    end = src.parse_stats.end_offset
    assert end == path.stat().st_size

    new_line = {
        "type": "user",
        "message": {"role": "user", "content": "One more thing, please."},
        "parentUuid": "a-21",
        "isSidechain": False,
        "userType": "external",
        "cwd": "/Users/redacted/Code/project-x",
        "sessionId": SID,
        "version": "2.0.0",
        "gitBranch": "main",
        "entrypoint": "cli",
        "uuid": "u-22",
        "timestamp": "2026-08-01T12:01:00.000Z",
    }
    with open(path, "a") as fh:
        fh.write(json.dumps(new_line) + "\n")

    resumed = list(src.parse(str(path), start_offset=end))
    stats = src.parse_stats
    assert len(resumed) == 1
    assert resumed[0].role == ROLE_HUMAN
    assert resumed[0].text == "One more thing, please."
    assert resumed[0].line_no == 22
    assert stats.lines_attempted == 1
    assert stats.error_taxonomy == {}
    assert stats.end_offset == path.stat().st_size


def test_empty_file(tmp_path):
    path = tmp_path / "empty.jsonl"
    path.write_bytes(b"")
    src = make_source(tmp_path)
    assert list(src.parse(str(path))) == []
    stats = src.parse_stats
    assert stats.lines_attempted == 0
    assert stats.end_offset == 0
    assert stats.truncated_final_line is False


# ----------------------------------------------------------------------
# parsing: structural failures are counted, never raised
# ----------------------------------------------------------------------


def test_bad_structure_counted_not_raised(tmp_path):
    path = tmp_path / "bad.jsonl"
    lines = [
        {"type": "user", "message": "not an object"},
        {"type": "user", "message": {"role": "user", "content": 123}},
        {"message": {"role": "user", "content": "no type field"}},
        ["a", "json", "array"],
    ]
    with open(path, "w") as fh:
        for row in lines:
            fh.write(json.dumps(row) + "\n")

    src = make_source(tmp_path)
    events = list(src.parse(str(path)))
    stats = src.parse_stats
    assert events == []
    assert stats.lines_attempted == 4
    assert stats.lines_failed == 4
    assert stats.lines_succeeded == 0
    assert stats.error_taxonomy == {
        "bad_structure:user_message_not_object": 1,
        "bad_structure:user_content_int": 1,
        "missing_type": 1,
        "bad_structure:line_not_object": 1,
    }


def test_the_two_types_found_on_2026_08_23_are_known_bookkeeping(tmp_path):
    """Recognize pr-link and fork-context-ref as bookkeeping records.

    The replacement fields preserve each supported record shape. Both must
    increment a known-skip counter without making the session partial.
    """
    root = tmp_path / "projects"
    d = root / "-Users-someone-repo"
    d.mkdir(parents=True)
    path = d / "s1.jsonl"
    path.write_text(
        json.dumps(
            {
                "type": "pr-link",
                "sessionId": "33333333-3333-4333-8333-333333333333",
                "prNumber": 15,
                "prUrl": "https://github.com/example/demo-service/pull/15",
                "prRepository": "example/demo-service",
                "timestamp": "2026-01-01T00:00:00.000Z",
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "fork-context-ref",
                "agentId": "example-agent",
                "parentSessionId": "44444444-4444-4444-8444-444444444444",
                "parentLastUuid": "55555555-5555-4555-8555-555555555555",
                "contextLength": 61,
            }
        )
        + "\n"
    )
    src = make_source(root)
    events = list(src.parse(str(path)))
    stats = src.parse_stats

    assert events == []
    assert stats.lines_attempted == 2
    assert stats.lines_succeeded == 2
    assert stats.lines_failed == 0, f"marked malformed: {stats.error_taxonomy}"
    assert stats.unknown_lines == 0, f"still unknown: {stats.unknown_taxonomy}"
    assert stats.skipped_taxonomy == {"pr-link": 1, "fork-context-ref": 1}
