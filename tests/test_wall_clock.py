"""Wall time and model time are distinct measurements.

Synthetic timestamps exercise gaps, missing data, and sleep-shaped delays.
An overlong run can retain the nightly lock beyond the next scheduled start."""

from __future__ import annotations

import pytest

from self_improve import report
from self_improve.pipeline import run_pipeline, wall_clock_stats

from tests.e2e_corpus import ScriptedLLM, build_corpus, mine_payload


@pytest.fixture
def corpus(tmp_path):
    return build_corpus(tmp_path)


class _FakeStore:
    def __init__(self, rows):
        self._rows = rows

    def query(self, sql, params=()):
        return self._rows


def test_it_finds_the_gap_that_is_outside_every_recorded_call():
    """Invented short calls cross midnight with a long gap between them."""
    rows = [
        {"created_at": "2026-01-01T23:55:00Z", "duration_ms": 60_000, "stage": "mine_agentic"},
        {"created_at": "2026-01-02T10:05:00Z", "duration_ms": 300_000, "stage": "mine_agentic"},
    ]
    out = wall_clock_stats(
        _FakeStore(rows), "r1", "2026-01-01T23:50:00Z", "2026-01-02T10:10:00Z"
    )
    assert out["calls"] == 2
    assert out["model_seconds"] == 360.0
    assert out["wall_seconds"] == 37_200.0
    # First call ends at 23:55; the second starts at 10:00 the next day.
    assert out["largest_gap_seconds"] == 36_300.0
    assert out["largest_gap_before_call"] == "mine_agentic@2026-01-02T10:05:00Z"
    assert out["unaccounted_pct"] > 98


def test_a_run_with_no_calls_still_reports_its_wall_clock():
    """A dry run makes zero LLM calls. Reporting nothing there would make
    "no calls" and "not measured" the same line."""
    out = wall_clock_stats(_FakeStore([]), "r2", "2026-08-30T00:00:00Z", "2026-08-30T00:05:00Z")
    assert out == {
        "wall_seconds": 300.0, "model_seconds": 0.0, "unaccounted_seconds": 300.0,
        "unaccounted_pct": 100.0, "largest_gap_seconds": 0.0,
        "largest_gap_before_call": "", "calls": 0,
    }


def test_a_non_iso_timestamp_raises_and_names_the_run():
    """Both timestamps come from utc_now_iso(), so a bad one is a contract bug.
    Silently reporting 0 h would make an unreadable clock look like a fast run."""
    with pytest.raises(ValueError, match=r"run r3: started is not an ISO timestamp"):
        wall_clock_stats(_FakeStore([]), "r3", "last tuesday", "2026-08-30T00:00:00Z")
    with pytest.raises(ValueError, match=r"run r3: finished is not an ISO timestamp"):
        wall_clock_stats(_FakeStore([]), "r3", "2026-08-30T00:00:00Z", "")


def test_run_pipeline_records_it_and_finished_is_the_same_instant(corpus):
    """Through the real entry point: the stats carry it AND the runs row's
    `finished` is the exact instant measured to.

    Two separate utc_now_iso() calls would make the stored run duration and the
    reported one disagree by however long the stats dump took — a discrepancy
    nobody would ever chase.
    """
    llm = ScriptedLLM(mine_responses=[mine_payload(f"**R{i}**") for i in range(8)])
    stats = run_pipeline(corpus.cfg, corpus.store, review_only=True, _llm_factory=llm.factory())

    wc = stats["wall_clock"]
    assert set(wc) == {
        "wall_seconds", "model_seconds", "unaccounted_seconds", "unaccounted_pct",
        "largest_gap_seconds", "largest_gap_before_call", "calls",
    }
    assert wc["wall_seconds"] >= 0 and wc["model_seconds"] >= 0

    n_calls = corpus.store.query_one(
        "SELECT COUNT(*) AS n FROM llm_calls WHERE run_id = ?", (stats["run_id"],)
    )["n"]
    assert wc["calls"] == n_calls


def test_the_report_renders_it(corpus, tmp_path):
    """Through report.generate, not the helper. Deleting the `out.extend`
    call site is the sabotage this test exists to catch."""
    llm = ScriptedLLM(mine_responses=[mine_payload(f"**R{i}**") for i in range(8)])
    stats = run_pipeline(corpus.cfg, corpus.store, review_only=True, _llm_factory=llm.factory())
    out = tmp_path / "r.md"
    report.generate(corpus.store, corpus.cfg, stats["run_id"], out)
    text = out.read_text()
    assert "## Wall clock" in text
    assert "inside a model call" in text
    assert "time.monotonic()" in text


def test_long_run_names_the_lock_at_the_next_scheduled_launch():
    """An invented long run must name the scheduling risk from its held lock."""
    lines = report._wall_clock_lines(
        {"wall_clock": {
            "wall_seconds": 70_200.0, "model_seconds": 9_000.0,
            "unaccounted_seconds": 61_200.0, "unaccounted_pct": 87.2,
            "largest_gap_seconds": 46_800.0,
            "largest_gap_before_call": "mine_agentic@2026-01-02T09:00:00Z",
            "calls": 12,
        }}
    )
    body = "\n".join(lines)
    assert "19.50 h" in body
    assert "2.50 h" in body
    assert "13.00 h" in body and "2026-01-02T09:00:00Z" in body
    assert "nightly lock at the next scheduled launch" in body
    assert "prevents that launch from starting a second run" in body


def test_a_short_run_does_not_claim_it_ate_the_next_night():
    """The control. Without it, a builder that always emitted the warning
    would pass the test above forever."""
    body = "\n".join(report._wall_clock_lines(
        {"wall_clock": {
            "wall_seconds": 3600.0, "model_seconds": 3500.0, "unaccounted_seconds": 100.0,
            "unaccounted_pct": 2.8, "largest_gap_seconds": 30.0,
            "largest_gap_before_call": "grade@2026-08-30T01:00:00Z", "calls": 12,
        }}
    ))
    assert "## Wall clock" in body
    assert "single-instance lock" not in body


def test_stats_without_the_block_render_nothing_rather_than_zero_hours():
    """Historical runs' stats_json predates this. A rendered `0.00 h` would be
    a measurement nobody took."""
    assert report._wall_clock_lines({}) == []
    assert report._wall_clock_lines({"wall_clock": {}}) == []


def test_the_runs_row_finished_is_the_instant_the_wall_clock_measured_to(corpus, monkeypatch):
    """The runs row's `finished` must be the SAME instant `wall_clock_stats`
    measured to, not a second clock read.

    A real clock makes this untestable — two `utc_now_iso()` calls a few
    statements apart differ by microseconds, and `ops/sabotage.sh` proved that:
    swapping `run_finished` back for a fresh `utc_now_iso()` left the first
    version of this assertion green. So the clock is replaced by one that
    advances a visible 10 s per read; a second read then lands 10 s after the
    instant reported, and `finished - started` no longer equals `wall_seconds`.
    """
    import itertools
    from datetime import datetime, timedelta, timezone

    from self_improve import pipeline, store as store_mod

    base = datetime(2026, 8, 30, 2, 30, 0, tzinfo=timezone.utc)
    tick = itertools.count()

    def fake_now():
        return (base + timedelta(seconds=10 * next(tick))).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    monkeypatch.setattr(pipeline, "utc_now_iso", fake_now)
    monkeypatch.setattr(store_mod, "utc_now_iso", fake_now)

    llm = ScriptedLLM(mine_responses=[mine_payload(f"**R{i}**") for i in range(8)])
    stats = run_pipeline(corpus.cfg, corpus.store, review_only=True, _llm_factory=llm.factory())

    row = corpus.store.query_one(
        "SELECT started, finished FROM runs WHERE id = ?", (stats["run_id"],)
    )
    P = lambda t: datetime.fromisoformat(t.replace("Z", "+00:00"))
    stored = (P(row["finished"]) - P(row["started"])).total_seconds()
    assert stored == pytest.approx(stats["wall_clock"]["wall_seconds"], abs=0.05), (
        f"runs row says {stored}s, wall_clock says "
        f"{stats['wall_clock']['wall_seconds']}s — two different clock reads"
    )
