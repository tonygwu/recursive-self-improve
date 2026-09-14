"""Enforce the mining deadline using wall time, including machine sleep.
A long-running job can retain the nightly lock across another scheduled run.
On macOS, monotonic time pauses during sleep, so it cannot enforce this wall-time limit.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

from self_improve import pipeline
from self_improve.pipeline import run_pipeline

from tests.e2e_corpus import ScriptedLLM, build_corpus, mine_payload


@pytest.fixture
def corpus(tmp_path):
    return build_corpus(tmp_path)


def _run(corpus, cfg, n=8):
    llm = ScriptedLLM(mine_responses=[mine_payload(f"**R{i}**") for i in range(n)])
    return run_pipeline(cfg, corpus.store, review_only=True, _llm_factory=llm.factory())


def test_a_deadline_already_past_mines_nothing_and_says_what_it_cut(corpus):
    """A zero-second budget: every incident is REFUSED, none attempted."""
    cfg = dataclasses.replace(corpus.cfg, max_run_wall_seconds=0)
    stats = _run(corpus, cfg)
    mine, dl = stats["mine"], stats["mine"]["deadline"]

    assert dl["stopped_early"] is True
    assert mine["attempted"] == 0, "a deadline stop must not count as an attempt"
    assert dl["incidents_not_mined"] > 0, "nothing was cut, so this proves nothing"
    assert mine["taxonomy"]["wall_deadline_reached"] == dl["incidents_not_mined"]
    assert "elapsed_wall_seconds" in dl


def test_the_deadline_stop_keeps_the_stage_invariant(corpus):
    """A deadline refusal must preserve the stage accounting invariant."""
    cfg = dataclasses.replace(corpus.cfg, max_run_wall_seconds=0)
    stats = _run(corpus, cfg)
    assert pipeline.check_stage_invariants(stats) == [], stats.get("mine")


def test_a_normal_run_records_the_deadline_without_firing_it(corpus):
    """Recorded EVERY run. Otherwise "it did not bite" and "nobody measured"
    are the same absence in the report."""
    stats = _run(corpus, corpus.cfg)
    dl = stats["mine"]["deadline"]
    assert dl["stopped_early"] is False
    assert dl["incidents_not_mined"] == 0
    assert dl["max_run_wall_seconds"] == 12 * 3600
    assert stats["mine"]["attempted"] > 0, "nothing was mined, so this proves nothing"
    assert "wall_deadline_reached" not in stats["mine"]["taxonomy"]


def test_the_deadline_is_wall_clock_and_survives_a_frozen_monotonic(corpus, monkeypatch):
    """Enforce the wall-time deadline while the monotonic clock is frozen.

    Advancing wall time past the deadline must stop mining even when the
    monotonic clock records no elapsed time."""
    import time as time_mod

    monkeypatch.setattr(time_mod, "monotonic", lambda: 1000.0)

    fired_at = datetime.now(timezone.utc) + timedelta(hours=20)
    monkeypatch.setattr(pipeline, "_now_utc", lambda: fired_at)

    cfg = dataclasses.replace(corpus.cfg, max_run_wall_seconds=12 * 3600)
    stats = _run(corpus, cfg)

    assert stats["mine"]["deadline"]["stopped_early"] is True, (
        "20 h of WALL time passed against a 12 h deadline and the mine stage "
        "kept going — the deadline is reading a clock that sleep can freeze"
    )
    assert stats["mine"]["attempted"] == 0
    assert time_mod.monotonic() == 1000.0, "the frozen monotonic clock was the premise"


def test_the_config_default_is_twelve_hours():
    """Pin the configured wall-time policy without an operational run sample."""
    from self_improve.config import Config

    assert Config().max_run_wall_seconds == 12 * 3600
