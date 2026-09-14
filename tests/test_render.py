"""Tests for render.py: the redaction gate for the agentic-mine sandbox files."""

from __future__ import annotations

from pathlib import Path

import pytest

from self_improve.config import Config
from self_improve.miner import MinerError
from self_improve.render import (
    RENDER_BODY_TRUNCATE_CHARS,
    RenderStats,
    render_environment,
    render_session,
)
from self_improve.sources.base import TurnEvent
from self_improve.store import Store

# Matches redact.py's sk_api_key pattern; must NEVER appear in rendered output.
PLANTED_SECRET = "sk-plantedsecret1234567890abc"


def _event(**over) -> TurnEvent:
    base = dict(
        source="claude",
        session_file="/fake/projects/slug/abc.jsonl",
        session_id="sess-1",
        project_path="/proj",
        ts_utc="2026-08-01T12:00:04.000Z",
        role="human",
        kind="message",
        text="hello",
        line_no=6,
    )
    base.update(over)
    return TurnEvent(**base)


# ---------------------------------------------------------------------------
# render_session
# ---------------------------------------------------------------------------


def test_render_session_redacts_secret_in_body(tmp_path):
    out = tmp_path / "transcript.md"
    stats = render_session(
        [_event(text=f"my key is {PLANTED_SECRET} please use it")], out
    )
    content = out.read_text(encoding="utf-8")
    assert PLANTED_SECRET not in content
    assert "[REDACTED:sk_api_key]" in content
    assert stats.redactions_estimated == 1


def test_render_session_headings_carry_pointer_tool_and_error(tmp_path):
    out = tmp_path / "transcript.md"
    render_session(
        [
            _event(),
            _event(
                role="tool_result",
                kind="tool_result",
                ts_utc="2026-08-01T12:00:09.000Z",
                text="File does not exist.",
                tool_name="Read",
                is_error=True,
                line_no=11,
            ),
        ],
        out,
    )
    content = out.read_text(encoding="utf-8")
    assert "## [6] human/message 2026-08-01T12:00:04.000Z" in content
    assert (
        "## [11] tool_result/tool_result 2026-08-01T12:00:09.000Z tool=Read ERROR"
        in content
    )
    # ERROR marker only on is_error events.
    assert content.count(" ERROR") == 1


def test_render_session_truncates_long_body_with_exact_marker(tmp_path):
    out = tmp_path / "transcript.md"
    stats = render_session(
        [_event(text="x" * (RENDER_BODY_TRUNCATE_CHARS + 500))], out
    )
    content = out.read_text(encoding="utf-8")
    assert "[truncated 500 chars]" in content
    # Kept content is exactly the cap, not less.
    assert "x" * RENDER_BODY_TRUNCATE_CHARS in content
    assert "x" * (RENDER_BODY_TRUNCATE_CHARS + 1) not in content
    assert stats.truncations == 1


def test_render_session_redacts_before_truncating(tmp_path):
    # A secret sitting beyond the truncation cap must still never leak: the
    # body is redacted first, then cut.
    out = tmp_path / "transcript.md"
    stats = render_session(
        [_event(text="x" * (RENDER_BODY_TRUNCATE_CHARS + 100) + " " + PLANTED_SECRET)],
        out,
    )
    content = out.read_text(encoding="utf-8")
    assert PLANTED_SECRET not in content
    # The redaction is still counted even though truncation cut the placeholder.
    assert stats.redactions_estimated == 1


def test_render_session_stats_counts(tmp_path):
    out = tmp_path / "transcript.md"
    stats = render_session(
        [
            _event(text=f"key {PLANTED_SECRET}"),
            _event(text="y" * (RENDER_BODY_TRUNCATE_CHARS + 7), line_no=7),
            _event(text="plain", line_no=8),
        ],
        out,
    )
    assert isinstance(stats, RenderStats)
    assert stats.events_rendered == 3
    assert stats.truncations == 1
    assert stats.redactions_estimated == 1
    assert stats.chars_written == len(out.read_text(encoding="utf-8"))


def test_render_session_empty_events_writes_header_only(tmp_path):
    out = tmp_path / "transcript.md"
    stats = render_session([], out)
    assert stats.events_rendered == 0
    assert out.read_text(encoding="utf-8") == "# Session transcript (redacted)\n"


# ---------------------------------------------------------------------------
# render_environment
# ---------------------------------------------------------------------------

SESSION_FILE = "/fake/projects/slug/abc.jsonl"


def _seed_session(store: Store, **over) -> None:
    row = {
        "file_path": SESSION_FILE,
        "source": "claude",
        "session_id": "sess-1",
        "project_path": "/proj",
        "headless": 1,
        "is_subagent": 0,
        "first_ts": "2026-08-01T00:00:00Z",
        "last_ts": "2026-08-01T01:00:00Z",
        "mtime": 0.0,
        "file_size": 1000,
        "bytes_scanned": 1000,
        "lines_scanned": 50,
        "malformed_lines": 0,
        "status": "ok",
        "error": "",
        "last_scanned_at": "2026-08-14T02:00:00Z",
    }
    row.update(over)
    store.upsert_session(row)
    store.commit()


def _incident_row(project_path: str, **over) -> dict:
    row = {
        "id": "inc-1",
        "session_file": SESSION_FILE,
        "session_id": "sess-1",
        "project_path": project_path,
        "ts": "2026-08-01T00:30:00Z",
        "signal_type": "correction",
        "matched_text": "no, that's wrong",
        "start_line": 6,
    }
    row.update(over)
    return row


def test_render_environment_writes_context_and_pointer(tmp_path):
    store = Store(tmp_path / "state.db")
    _seed_session(store)
    (tmp_path / "global_CLAUDE.md").write_text("GLOBAL-RULE-LINE", encoding="utf-8")
    project = tmp_path / "proj"
    project.mkdir()
    (project / "AGENTS.md").write_text("AGENTS-CONTENT", encoding="utf-8")
    cfg = Config(global_claude_md=str(tmp_path / "global_CLAUDE.md"))
    out = tmp_path / "environment.md"

    render_environment(store, _incident_row(str(project)), cfg, out)

    content = out.read_text(encoding="utf-8")
    assert f"Project: {project}" in content
    assert "Session source: claude" in content
    assert "Headless: yes" in content
    assert "Subagent session: no" in content
    assert "2026-08-01T00:00:00Z .. 2026-08-01T01:00:00Z" in content
    assert "Signal type: correction" in content
    assert "no, that's wrong" in content
    assert "## [6]" in content
    # In-force instruction files, gathered via miner.gather_in_force_instructions.
    assert "GLOBAL-RULE-LINE" in content
    assert "AGENTS-CONTENT" in content


def test_render_environment_redacts_matched_text(tmp_path):
    # matched_text is stored redacted; this is defense in depth for the one
    # transcript-derived string environment.md carries.
    store = Store(tmp_path / "state.db")
    _seed_session(store)
    cfg = Config(global_claude_md=str(tmp_path / "global_CLAUDE.md"))
    out = tmp_path / "environment.md"
    render_environment(
        store, _incident_row("", matched_text=f"use {PLANTED_SECRET} now"), cfg, out
    )
    content = out.read_text(encoding="utf-8")
    assert PLANTED_SECRET not in content
    assert "[REDACTED:sk_api_key]" in content


def test_render_environment_without_start_line_raises(tmp_path):
    store = Store(tmp_path / "state.db")
    _seed_session(store)
    cfg = Config(global_claude_md=str(tmp_path / "global_CLAUDE.md"))
    row = _incident_row("/proj")
    del row["start_line"]
    with pytest.raises(MinerError, match="start_line"):
        render_environment(store, row, cfg, tmp_path / "environment.md")
    assert not (tmp_path / "environment.md").exists()


def test_render_environment_missing_session_row_raises(tmp_path):
    store = Store(tmp_path / "state.db")  # no session row seeded
    cfg = Config(global_claude_md=str(tmp_path / "global_CLAUDE.md"))
    with pytest.raises(MinerError, match="session row missing"):
        render_environment(
            store, _incident_row("/proj"), cfg, tmp_path / "environment.md"
        )


def test_environment_tells_the_agent_the_transcript_size(tmp_path):
    """Report the invented transcript's size and section count, with advice against reading it whole."""
    from self_improve.render import _transcript_size_note

    big = tmp_path / "transcript.md"
    big.write_text("\n## [1] user/message\n" + ("x" * 500_000) + "\n## [2] a/b\n")
    note = _transcript_size_note(big)

    assert "MB" in note and "sections" in note
    assert "Do NOT Read it whole" in note


def test_a_small_transcript_gets_the_size_without_the_warning(tmp_path):
    """A warning attached to every transcript is a warning nobody reads."""
    from self_improve.render import _transcript_size_note

    small = tmp_path / "transcript.md"
    small.write_text("\n## [1] user/message\nhello\n")
    note = _transcript_size_note(small)

    assert "MB" in note
    assert "Do NOT Read" not in note


def test_a_missing_transcript_does_not_break_environment_rendering(tmp_path):
    from self_improve.render import _transcript_size_note

    assert _transcript_size_note(tmp_path / "nope.md") == ""
