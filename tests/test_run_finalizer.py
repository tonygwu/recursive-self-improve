"""Finalize interrupted runs and later reap abandoned ones.
KeyboardInterrupt and SystemExit require explicit finalization. A process
killed before it can finalize leaves a row for a later age-based reaper.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

from self_improve.config import Config
from self_improve.pipeline import finalize_stale_runs
from self_improve.store import Store, new_id


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "s.db")


def _run(store, started: datetime, status="running", finished=""):
    rid = new_id()
    store.insert(
        "runs",
        {"id": rid, "started": _iso(started), "status": status, "finished": finished},
    )
    store.commit()
    return rid


def test_an_old_running_row_is_reaped(store):
    old = datetime.now(timezone.utc) - timedelta(hours=48)
    rid = _run(store, old)

    stats = finalize_stale_runs(store, Config())

    row = store.query_one("SELECT * FROM runs WHERE id = ?", (rid,))
    assert row["status"] == "abandoned"
    assert row["finished"], "a finalized run must carry a finished timestamp"
    assert stats["abandoned"] == 1


def test_a_run_that_started_moments_ago_is_left_alone(store):
    """A concurrent manual run is LIVE. Reaping it would be a lie.

    The nightly lock only serializes nightly-vs-nightly, so a manual run can
    genuinely be in flight while another process starts.
    """
    rid = _run(store, datetime.now(timezone.utc) - timedelta(minutes=2))
    stats = finalize_stale_runs(store, Config())

    assert store.query_one("SELECT * FROM runs WHERE id = ?", (rid,))["status"] == "running"
    assert stats["abandoned"] == 0


def test_finished_runs_are_never_touched(store):
    rid = _run(store, datetime.now(timezone.utc) - timedelta(days=9),
               status="ok", finished=_iso(datetime.now(timezone.utc)))
    finalize_stale_runs(store, Config())
    assert store.query_one("SELECT * FROM runs WHERE id = ?", (rid,))["status"] == "ok"


def test_the_threshold_is_configurable(store):
    rid = _run(store, datetime.now(timezone.utc) - timedelta(hours=3))
    cfg = dataclasses.replace(Config(), run_stale_after_hours=1)
    finalize_stale_runs(store, cfg)
    assert store.query_one("SELECT * FROM runs WHERE id = ?", (rid,))["status"] == "abandoned"


def test_a_malformed_started_timestamp_is_reported_not_guessed(store):
    """Our own DB invariant is ISO-UTC. A bad value must not silently reap."""
    rid = new_id()
    store.insert("runs", {"id": rid, "started": "not-a-timestamp", "status": "running"})
    store.commit()

    stats = finalize_stale_runs(store, Config())

    assert store.query_one("SELECT * FROM runs WHERE id = ?", (rid,))["status"] == "running"
    assert stats["unparseable_started"] == 1


def test_keyboard_interrupt_still_marks_the_run(tmp_path):
    """Cause 1: `except Exception` lets KeyboardInterrupt through untouched."""
    from self_improve.pipeline import run_pipeline

    for d in ("cp", "cs", "ca"):
        (tmp_path / d).mkdir()
    cfg = dataclasses.replace(
        Config(),
        state_dir=str(tmp_path / "state"),
        claude_projects_dir=str(tmp_path / "cp"),
        codex_sessions_dir=str(tmp_path / "cs"),
        codex_archived_dir=str(tmp_path / "ca"),
        claude_history_path=str(tmp_path / "h.jsonl"),
    )
    st = Store(cfg.state_path("state.db"))

    class Boom:
        def __init__(self, *a, **k):
            raise KeyboardInterrupt("operator hit ctrl-c")

    with pytest.raises(KeyboardInterrupt):
        run_pipeline(cfg, st, _llm_factory=Boom)

    row = st.query_one("SELECT * FROM runs ORDER BY started DESC LIMIT 1")
    assert row["status"] == "interrupted", (
        "an interrupted run must leave a terminal status in its stored row"
    )
    assert row["finished"]
