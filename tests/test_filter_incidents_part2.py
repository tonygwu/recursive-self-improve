"""Tests for instruction-edit and self-observation detectors.

Edited paths can appear in metadata, tool-input JSON, Codex patch headers,
or marked success results. These fixtures exercise those source shapes so
detectors do not depend on metadata alone."""

from __future__ import annotations

import json

from self_improve.config import Config
from self_improve.filter_incidents import (
    SIGNAL_INSTRUCTION_EDIT,
    SIGNAL_SELF_OBSERVATION,
    _edited_path,
    detect,
)
from self_improve.sources.base import (
    KIND_MESSAGE,
    KIND_TOOL_RESULT,
    KIND_TOOL_USE,
    ROLE_ASSISTANT,
    ROLE_HUMAN,
    ROLE_TOOL_RESULT,
    TurnEvent,
)

CFG = Config()


def ev(role, text, *, kind=KIND_MESSAGE, tool="", meta=None, headless=False, idx=0):
    return TurnEvent(
        source="claude",
        session_file="/f.jsonl",
        session_id="s",
        project_path="/proj",
        ts_utc="2026-08-16T00:00:%02dZ" % (idx % 60),
        role=role,
        kind=kind,
        text=text,
        tool_name=tool,
        headless=headless,
        meta=meta or {},
    )


def edit_event(path: str, content: str = "- **New rule.** Do the thing.", tool="Edit"):
    """An edit tool_use shaped like the real parser emits: input JSON in text."""
    return ev(
        ROLE_ASSISTANT,
        json.dumps({"file_path": path, "content": content}),
        kind=KIND_TOOL_USE,
        tool=tool,
    )


def by_signal(result, signal):
    return [i for i in result.incidents if i.signal_type == signal]


# ------------------------------------------------------- path resolution

def test_path_resolved_from_tool_input_not_meta():
    # The bug this suite anchors: meta has no path, text does.
    e = edit_event("/Users/x/proj/CLAUDE.md")
    assert "file_path" not in e.meta
    assert _edited_path(e) == "/Users/x/proj/CLAUDE.md"


def test_meta_path_wins_when_a_source_does_record_it():
    e = ev(
        ROLE_ASSISTANT,
        json.dumps({"file_path": "/from/text.md"}),
        kind=KIND_TOOL_USE,
        tool="Edit",
        meta={"file_path": "/from/meta.md"},
    )
    assert _edited_path(e) == "/from/meta.md"


def test_unparseable_or_pathless_input_yields_empty_not_raise():
    assert _edited_path(ev(ROLE_ASSISTANT, "not json at all", kind=KIND_TOOL_USE, tool="Edit")) == ""
    assert _edited_path(ev(ROLE_ASSISTANT, '{"content": "no path"}', kind=KIND_TOOL_USE, tool="Edit")) == ""
    assert _edited_path(ev(ROLE_ASSISTANT, "{bad json", kind=KIND_TOOL_USE, tool="Edit")) == ""


def test_notebook_path_key_also_resolved():
    e = ev(
        ROLE_ASSISTANT,
        json.dumps({"notebook_path": "/proj/CLAUDE.md"}),
        kind=KIND_TOOL_USE,
        tool="NotebookEdit",
    )
    assert _edited_path(e) == "/proj/CLAUDE.md"


# ------------------------------------------------------- instruction_edit

def test_instruction_file_edit_fires():
    events = [edit_event("/Users/x/proj/CLAUDE.md", "- **Never trust exit 0.**")]
    hits = by_signal(detect(events, CFG), SIGNAL_INSTRUCTION_EDIT)
    assert len(hits) == 1
    assert hits[0].score == 0.9
    assert "Never trust exit 0" in hits[0].matched_text
    assert hits[0].detail["file"].endswith("CLAUDE.md")


def test_rules_directory_edit_fires():
    events = [edit_event("/Users/x/proj/.claude/rules/testing.md", "- scoped rule")]
    assert len(by_signal(detect(events, CFG), SIGNAL_INSTRUCTION_EDIT)) == 1


def test_ordinary_source_file_edit_does_not_fire():
    events = [edit_event("/Users/x/proj/src/main.py", "print('hi')")]
    assert by_signal(detect(events, CFG), SIGNAL_INSTRUCTION_EDIT) == []


def test_temp_and_fixture_paths_denied():
    for path in (
        "/private/tmp/scratch/CLAUDE.md",
        "/var/folders/ab/xyz/CLAUDE.md",
        "/repo/tests/fixtures/CLAUDE.md",
        "/repo/.venv/lib/AGENTS.md",
    ):
        events = [edit_event(path)]
        assert by_signal(detect(events, CFG), SIGNAL_INSTRUCTION_EDIT) == [], path


def test_empty_edit_content_counted_not_fired():
    events = [edit_event("/proj/CLAUDE.md", "")]
    result = detect(events, CFG)
    assert by_signal(result, SIGNAL_INSTRUCTION_EDIT) == []
    assert result.stats.get("instruction_edit_empty") == 1


def test_missing_path_counted_not_guessed():
    events = [ev(ROLE_ASSISTANT, "not json", kind=KIND_TOOL_USE, tool="Edit")]
    result = detect(events, CFG)
    assert by_signal(result, SIGNAL_INSTRUCTION_EDIT) == []
    assert result.stats.get("instruction_edit_missing_path") == 1


def test_headless_instruction_edit_still_fires():
    # Agents encoding lessons headlessly are exactly the target.
    e = edit_event("/proj/AGENTS.md")
    headless = TurnEvent(**{**e.__dict__, "headless": True})
    assert len(by_signal(detect([headless], CFG), SIGNAL_INSTRUCTION_EDIT)) == 1


# ------------------------------------------------------- self_observation

def tool_result(text="ok"):
    return ev(ROLE_TOOL_RESULT, text, kind=KIND_TOOL_RESULT)


def test_self_observation_fires_after_evidence():
    events = [
        tool_result("stats.models: gemini-3.5-flash"),
        ev(ROLE_ASSISTANT, "The CLI silently served a different model than requested."),
    ]
    hits = by_signal(detect(events, CFG), SIGNAL_SELF_OBSERVATION)
    assert len(hits) == 1
    assert "silently_did" in hits[0].detail["patterns"]


def test_self_observation_requires_prior_tool_result():
    # Same sentence with no evidence in the window is speculation, not discovery.
    events = [ev(ROLE_ASSISTANT, "The CLI silently served the wrong model.")]
    assert by_signal(detect(events, CFG), SIGNAL_SELF_OBSERVATION) == []


def test_self_observation_ignores_human_text():
    events = [tool_result(), ev(ROLE_HUMAN, "turns out the root cause was mine")]
    assert by_signal(detect(events, CFG), SIGNAL_SELF_OBSERVATION) == []


def test_self_observation_skips_long_reports():
    events = [tool_result(), ev(ROLE_ASSISTANT, "turns out " + "x" * 5000)]
    assert by_signal(detect(events, CFG), SIGNAL_SELF_OBSERVATION) == []


def test_self_observation_multi_pattern_scores_higher():
    events = [
        tool_result(),
        ev(ROLE_ASSISTANT, "I was reading the wrong file; the root cause was a stale cache."),
    ]
    hits = by_signal(detect(events, CFG), SIGNAL_SELF_OBSERVATION)
    assert len(hits) == 1
    assert len(hits[0].detail["patterns"]) >= 2
    assert hits[0].score > 0.7


def test_self_observation_headless_still_fires():
    events = [
        tool_result(),
        ev(ROLE_ASSISTANT, "It never actually ran the migration.", headless=True),
    ]
    assert len(by_signal(detect(events, CFG), SIGNAL_SELF_OBSERVATION)) == 1


def test_ordinary_assistant_prose_does_not_fire():
    events = [tool_result(), ev(ROLE_ASSISTANT, "I'll run the tests and report back.")]
    assert by_signal(detect(events, CFG), SIGNAL_SELF_OBSERVATION) == []


# ---------------------------------------------------------------------------
# Codex apply_patch INVOCATION paths (2026-08-18)
# ---------------------------------------------------------------------------


class TestCodexApplyPatchInvocationPaths:
    """Read paths from patch invocation headers as well as success results.
    A failed apply_patch has no success result, so friction detection requires
    the invocation's path.
    """

    def test_update_file_header_resolves(self):
        from self_improve.filter_incidents import _edited_paths
        from self_improve.sources.base import TurnEvent

        e = TurnEvent(
            source="codex", session_file="/f.jsonl", session_id="s", project_path="/p", ts_utc="",
            role="assistant", kind="tool_use", tool_name="apply_patch",
            text="*** Begin Patch\n*** Update File: backlog/channels.md\n@@\n-old\n+new\n*** End Patch",
        )
        assert _edited_paths(e) == ["backlog/channels.md"]

    def test_add_and_delete_headers_resolve(self):
        from self_improve.filter_incidents import _edited_paths
        from self_improve.sources.base import TurnEvent

        e = TurnEvent(
            source="codex", session_file="/f.jsonl", session_id="s", project_path="/p", ts_utc="",
            role="assistant", kind="tool_use", tool_name="apply_patch",
            text="*** Begin Patch\n*** Add File: /tmp/x.txt\n+hi\n*** Delete File: old//y.py\n*** End Patch",
        )
        assert _edited_paths(e) == ["/tmp/x.txt", "old//y.py"]

    def test_a_multi_file_patch_yields_every_path(self):
        from self_improve.filter_incidents import _edited_paths
        from self_improve.sources.base import TurnEvent

        e = TurnEvent(
            source="codex", session_file="/f.jsonl", session_id="s", project_path="/p", ts_utc="",
            role="assistant", kind="tool_use", tool_name="apply_patch",
            text=("*** Begin Patch\n*** Update File: a/CLAUDE.md\n@@\n+x\n"
                  "*** Update File: b/AGENTS.md\n@@\n+y\n*** End Patch"),
        )
        assert _edited_paths(e) == ["a/CLAUDE.md", "b/AGENTS.md"]

    def test_the_result_text_form_still_works(self):
        """The existing success-result parser must not regress."""
        from self_improve.filter_incidents import _edited_paths
        from self_improve.sources.base import TurnEvent

        e = TurnEvent(
            source="codex", session_file="/f.jsonl", session_id="s", project_path="/p", ts_utc="",
            role="tool_result", kind="tool_result", tool_name="apply_patch",
            text="Success. Updated the following files:\nM /Users/t/repo/x.py\n",
        )
        assert _edited_paths(e) == ["/Users/t/repo/x.py"]

    def test_a_patch_body_line_is_not_mistaken_for_a_path(self):
        """`+*** Update File: ...` inside the diff body is content, not a header."""
        from self_improve.filter_incidents import _edited_paths
        from self_improve.sources.base import TurnEvent

        e = TurnEvent(
            source="codex", session_file="/f.jsonl", session_id="s", project_path="/p", ts_utc="",
            role="assistant", kind="tool_use", tool_name="apply_patch",
            text="*** Begin Patch\n*** Update File: real.md\n@@\n+*** Update File: fake.md\n*** End Patch",
        )
        assert _edited_paths(e) == ["real.md"]


class TestCodexRelativeResultPaths:
    """Marked Codex success results accept relative and absolute paths.

    The result header distinguishes edited-file rows from unrelated prose.
    Multiple rows must resolve every path."""

    def _ev(self, text):
        from self_improve.sources.base import TurnEvent

        return TurnEvent(
            source="codex", session_file="/f.jsonl", session_id="s",
            project_path="/p", ts_utc="", role="tool_result",
            kind="patch_apply", tool_name="apply_patch", text=text,
        )

    def test_relative_result_path_resolves(self):
        from self_improve.filter_incidents import _edited_paths

        e = self._ev("Success. Updated the following files:\nM backlog/channels.md\n")
        assert _edited_paths(e) == ["backlog/channels.md"]

    def test_absolute_result_path_still_resolves(self):
        from self_improve.filter_incidents import _edited_paths

        e = self._ev("Success. Updated the following files:\nM /Users/t/x.py\n")
        assert _edited_paths(e) == ["/Users/t/x.py"]

    def test_several_files_in_one_result(self):
        from self_improve.filter_incidents import _edited_paths

        e = self._ev("Success. Updated the following files:\nA a.md\nM b/c.py\nD d.txt\n")
        assert _edited_paths(e) == ["a.md", "b/c.py", "d.txt"]

    def test_a_status_like_line_without_the_header_is_ignored(self):
        """Without the header, `M something` is prose, not an edited file."""
        from self_improve.filter_incidents import _edited_paths

        assert _edited_paths(self._ev("M maybe/not/a/path.md\nI Q sentence\n")) == []
