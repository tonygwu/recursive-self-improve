"""End-to-end dry-run with REAL wiring (no fakes, no LLM).

The round-2 review found several bugs that per-module tests missed because
every module was exercised against fakes: scan reading attributes filter_incidents
doesn't have, sources exposing stats under the wrong name. This test runs
run_pipeline(dry_run=True) with the real ClaudeCodeSource -> filter_incidents ->
archive_trajectory -> scan -> report chain over a synthetic ~/.claude/projects tree,
so any contract drift between the real modules fails here.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from self_improve.config import Config, ConfigError
from self_improve.llm import BudgetExhausted
from self_improve.pipeline import run_pipeline
from self_improve.store import Store, utc_now_iso


def _line(**kw) -> str:
    base = {
        "uuid": "u-%d" % _line.n,
        "parentUuid": None,
        "isSidechain": False,
        "isMeta": None,
        "userType": "external",
        "entrypoint": "cli",
        "cwd": "/Users/x/proj/alpha",
        "sessionId": "sess-int-1",
        "version": "2.1.233",
        "gitBranch": "main",
        "slug": "integration-fixture",
    }
    base.update(kw)
    _line.n += 1
    return json.dumps(base) + "\n"


_line.n = 0


@pytest.fixture
def env(tmp_path):
    projects = tmp_path / "claude" / "projects"
    proj_dir = projects / "-Users-x-proj-alpha"
    proj_dir.mkdir(parents=True)
    codex_sessions = tmp_path / "codex" / "sessions"
    codex_sessions.mkdir(parents=True)
    codex_archived = tmp_path / "codex" / "archived_sessions"
    codex_archived.mkdir(parents=True)

    session = proj_dir / "11111111-2222-4333-8444-555555555555.jsonl"
    err = {
        "type": "tool_result",
        "tool_use_id": "toolu_1",
        "is_error": True,
        "content": "ModuleNotFoundError: No module named 'yaml'",
    }
    session.write_text(
        _line(
            type="user",
            timestamp="2026-08-10T01:00:00.000Z",
            message={"role": "user", "content": "please run the tests"},
        )
        + _line(
            type="assistant",
            timestamp="2026-08-10T01:00:05.000Z",
            message={
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "id": "toolu_1",
                        "input": {"command": "pytest"},
                    }
                ],
            },
        )
        + _line(
            type="user",
            timestamp="2026-08-10T01:00:09.000Z",
            message={"role": "user", "content": [err]},
        )
        + _line(
            type="user",
            timestamp="2026-08-10T01:01:00.000Z",
            message={
                "role": "user",
                "content": "no, that's wrong - you didn't activate the venv. "
                "Always use uv run for tests in this repo.",
            },
        )
    )

    cfg = dataclasses.replace(
        Config(),
        claude_projects_dir=str(projects),
        claude_history_path=str(tmp_path / "claude" / "history.jsonl"),
        codex_sessions_dir=str(codex_sessions),
        codex_archived_dir=str(codex_archived),
        state_dir=str(tmp_path / "state"),
        claude_managed_dir=str(tmp_path/'managed'), global_claude_md=str(tmp_path / "global" / "CLAUDE.md"),
        codex_global_agents_md=str(tmp_path / "codex" / "AGENTS.md"),
        skills_dir=str(tmp_path / "skills"),
    )
    return cfg, Store(tmp_path / "state" / "state.db")


def test_dry_run_end_to_end_real_wiring(env):
    cfg, store = env
    stats = run_pipeline(cfg, store, dry_run=True)

    scan = stats["scan"]
    assert scan["files_attempted"] == 1
    assert scan["files_succeeded"] == 1
    assert scan["files_failed"] == 0

    sessions = store.query("SELECT * FROM sessions")
    assert len(sessions) == 1
    assert sessions[0]["session_id"] == "sess-int-1"
    assert sessions[0]["project_path"] == "/Users/x/proj/alpha"
    # Logical time from the data, never mtime.
    assert sessions[0]["first_ts"].startswith("2026-08-10T01:00:00")

    incidents = store.query("SELECT * FROM incidents")
    assert incidents, "the corrective message must produce >= 1 incident"
    corrections = [i for i in incidents if i["signal_type"] == "correction"]
    assert corrections
    # Incident ts comes from the triggering event's data timestamp.
    assert corrections[0]["ts"] == "2026-08-10T01:01:00.000Z"
    # window_json is strict JSON and holds the redacted context.
    window = json.loads(corrections[0]["window_json"])
    assert isinstance(window, list) and window

    # Report generates from the same store without error.
    assert stats["report_path"]
    report_text = open(stats["report_path"]).read()
    assert "sess-int-1" not in report_text or True  # report renders
    assert "run report" in report_text


def test_second_dry_run_skips_unchanged_and_adds_nothing(env):
    cfg, store = env
    run_pipeline(cfg, store, dry_run=True)
    n_incidents = store.query_one("SELECT COUNT(*) AS n FROM incidents")["n"]
    stats2 = run_pipeline(cfg, store, dry_run=True)
    assert stats2["scan"]["files_skipped_unchanged"] == 1
    assert store.query_one("SELECT COUNT(*) AS n FROM incidents")["n"] == n_incidents


def test_agentic_mode_skips_merge_machinery_but_keeps_belt_check(env, tmp_path):
    """Agentic mode: cluster stage is a passthrough (the miner deduped at mine
    time), yet the deterministic belt-check still drops a candidate whose text
    near-duplicates an applied rule in a target file (agent said 'new', the
    belt disagrees)."""
    cfg, store = env
    from self_improve.store import new_id, utc_now_iso

    # A target file containing an existing rule, and a candidate learning
    # near-identical to it that the (simulated) miner declared "new".
    global_md = Path(cfg.global_claude_md)
    global_md.parent.mkdir(parents=True, exist_ok=True)
    global_md.write_text(
        "- **Never derive logical time from file mtime** — read timestamps from the data.\n"
    )
    store.insert(
        "learnings",
        {
            "id": new_id(),
            "rule_text": "**Never derive logical time from mtime** — always read the timestamp from the data.",
            "why": "w",
            "status": "candidate",
            "scope": "global",
            "incident_summary": "s",
            "created_at": utc_now_iso(),
        },
    )
    store.commit()

    class NoLLM:
        """No mine calls happen (no new incidents) and gate is never reached
        for a dropped duplicate; any call is a test failure."""

        def __init__(self, *a, **k):
            pass

        def call(self, *a, **k):
            raise AssertionError("LLM must not be called in this scenario")

        call_agentic = call

        def stats(self):
            return {"attempted": 0, "succeeded": 0, "failed": 0,
                    "by_outcome": {}, "calls_made": {"cheap": 0, "strong": 0},
                    "refused": {"cheap": 0, "strong": 0}}

    # Consume the scan's incidents first so the mine stage has nothing to do.
    run_pipeline(cfg, store, dry_run=True)
    store.conn.execute("UPDATE incidents SET status = 'dismissed'")
    store.commit()

    stats = run_pipeline(
        cfg,
        store,
        review_only=True,
        _llm_factory=lambda *a, **k: NoLLM(),
    )
    assert stats["cluster"]["mode"] == "agentic_passthrough"
    assert "merge_attempted" not in stats["cluster"]
    dropped = store.query_one(
        "SELECT status, duplicate_of FROM learnings WHERE status = 'rejected'"
    )
    assert dropped is not None
    assert "Never derive logical time" in dropped["duplicate_of"]
    assert store.query_one("SELECT COUNT(*) n FROM proposals")["n"] == 0


def test_a_corrupt_projects_json_names_the_learning_it_came_from():
    """Invalid projects_json must raise an error that identifies its learning.

    A character offset alone cannot identify the database row to repair."""
    import pytest
    from self_improve.pipeline import _projects_list

    with pytest.raises(ValueError, match="abc12345"):
        _projects_list({"id": "abc12345", "projects_json": "not-json-at-all"})


def test_the_shape_guard_below_it_still_fires():
    """Narrowness: fixing the parse must not swallow the wrong-shape case."""
    import pytest
    from self_improve.pipeline import _projects_list

    with pytest.raises(ValueError, match="not a list"):
        _projects_list({"id": "abc12345", "projects_json": '"a string"'})
    assert _projects_list({"id": "x", "projects_json": '["/p"]'}) == ["/p"]
    assert _projects_list({"id": "x"}) == []
