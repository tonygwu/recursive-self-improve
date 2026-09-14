"""Exercise each stage's accounting invariant with invented counter combinations."""

from __future__ import annotations

from self_improve.pipeline import STAGE_INVARIANTS, check_stage_invariants


def test_a_clean_run_reports_nothing():
    stats = {
        "scan": {"files_attempted": 3, "files_succeeded": 2, "files_failed": 1},
        "mine": {"attempted": 5, "succeeded": 4, "failed": 1},
        "gate": {"attempted": 2, "gated_pass": 1, "gated_fail": 0, "ungated": 0, "failed": 1},
        "apply": {"attempted": 2, "applied": 1, "held": 1, "failed": 0},
    }
    assert check_stage_invariants(stats) == []


def test_each_stage_is_checked_independently():
    """A violation in one stage must not mask or be masked by another."""
    for stage, (attempted_key, outcomes) in STAGE_INVARIANTS.items():
        stats = {stage: {attempted_key: 9, **{k: 0 for k in outcomes}}}
        found = check_stage_invariants(stats)
        assert len(found) == 1 and found[0].startswith(stage), (stage, found)


def test_the_message_names_the_numbers_not_just_the_stage():
    """A violation you cannot act on is only slightly better than silence."""
    out = check_stage_invariants(
        {"mine": {"attempted": 11, "succeeded": 10, "failed": 0}}
    )
    assert len(out) == 1
    assert "attempted=11" in out[0]
    assert "succeeded=10" in out[0] and "failed=0" in out[0]


def test_a_missing_stage_is_not_a_violation():
    """dry_run has no mine/gate/apply; absence must not read as broken."""
    assert check_stage_invariants({"scan": {"files_attempted": 0,
                                            "files_succeeded": 0,
                                            "files_failed": 0}}) == []
    assert check_stage_invariants({}) == []


def test_a_stage_without_its_attempted_key_is_skipped():
    """Older runs' stats_json predates some counters; that is not a violation."""
    assert check_stage_invariants({"mine": {"succeeded": 3}}) == []


def test_extra_buckets_do_not_count_as_outcomes():
    """dup_dropped is bookkeeping, not a gate verdict.

    Counting it would make the invariant pass while real verdicts went
    unrecorded — the exact failure it exists to catch.
    """
    out = check_stage_invariants(
        {"gate": {"attempted": 5, "gated_pass": 0, "gated_fail": 0,
                  "ungated": 0, "failed": 0, "dup_dropped": 5}}
    )
    assert len(out) == 1, "dup_dropped was treated as an outcome"


def test_the_historical_violations_would_all_be_caught():
    """Invented counters reproduce missing outcomes and uncounted attempts."""
    examples = [
        {"mine": {"attempted": 3, "succeeded": 2, "failed": 0}},
        {"mine": {"attempted": 11, "succeeded": 10, "failed": 0}},
        {"mine": {"attempted": 9, "succeeded": 5, "failed": 3}},
        {"apply": {"attempted": 0, "applied": 0, "held": 4, "failed": 0}},
        {"gate": {"attempted": 4, "gated_pass": 0, "gated_fail": 0, "ungated": 0}},
    ]
    for stats in examples:
        assert check_stage_invariants(stats), stats


def test_the_gates_own_counter_shape_is_the_list_that_is_checked():
    """Reconcile the gate's declared outcomes and bookkeeping with its stored counters.
    Neither inconclusive nor refused may disappear from the sum, and no new
    bookkeeping counter may silently become an outcome.
    """
    from self_improve.pipeline import GATE_BOOKKEEPING, GATE_OUTCOMES, new_gate_stats

    attempted_key, outcome_keys = STAGE_INVARIANTS["gate"]
    assert attempted_key == "attempted"
    assert set(outcome_keys) == set(GATE_OUTCOMES)
    assert set(new_gate_stats()) == {attempted_key} | set(GATE_OUTCOMES) | set(
        GATE_BOOKKEEPING
    ), "a gate counter is neither an outcome nor declared bookkeeping"
    # And the bookkeeping half is named, so nothing can be quietly parked there
    # to make the invariant pass: dup_dropped counts proposals that never
    # reached the gate at all.
    assert set(GATE_BOOKKEEPING) == {"dup_dropped", "dup_dropped_by_miner"}


def test_an_unclassified_gate_counter_raises_rather_than_becoming_an_outcome():
    """The first version of this fix DERIVED the outcomes from the counter
    shape, and `ops/sabotage.sh` proved it hollow: adding a counter nobody
    classified passed, because the derivation silently made it an outcome.

    A bookkeeping counter treated as an outcome makes the invariant pass while
    verdicts go unrecorded, which is the quiet wrong answer. So the
    reconciliation raises, in both directions.
    """
    import pytest

    from self_improve import pipeline

    real = pipeline.new_gate_stats
    try:
        pipeline.new_gate_stats = lambda: {**real(), "some_new_counter": 0}
        with pytest.raises(RuntimeError, match="some_new_counter"):
            pipeline._gate_outcome_keys()

        missing = {k: v for k, v in real().items() if k != "refused"}
        pipeline.new_gate_stats = lambda: missing
        with pytest.raises(RuntimeError, match="refused"):
            pipeline._gate_outcome_keys()
    finally:
        pipeline.new_gate_stats = real
    # and the real pair still reconciles
    assert pipeline._gate_outcome_keys() == pipeline.GATE_OUTCOMES


def test_the_designed_steady_state_is_not_reported_as_broken():
    """Invented balanced outcomes include budget refusal and inconclusive votes."""
    cases = {
        "mixed-outcomes": {
            "attempted": 9, "gated_pass": 2, "gated_fail": 1, "ungated": 1,
            "inconclusive": 1, "failed": 0, "refused": 4,
        },
        "split-scenarios": {
            "attempted": 4, "gated_pass": 0, "gated_fail": 0, "ungated": 1,
            "inconclusive": 2, "failed": 0, "refused": 1,
        },
        "inconclusive-and-refused": {
            "attempted": 3, "gated_pass": 0, "gated_fail": 0, "ungated": 0,
            "inconclusive": 2, "failed": 0, "refused": 1,
        },
    }
    for case, gate in cases.items():
        assert sum(v for k, v in gate.items() if k != "attempted") == gate["attempted"], case
        assert check_stage_invariants({"gate": gate}) == [], case



def test_a_gate_that_really_does_not_add_up_is_still_caught():
    """The fix must not be 'sum everything'. One verdict going unrecorded
    stays a violation."""
    out = check_stage_invariants(
        {"gate": {"attempted": 5, "gated_pass": 0, "gated_fail": 0, "ungated": 0,
                  "inconclusive": 3, "failed": 0, "refused": 1}}
    )
    assert len(out) == 1 and "attempted=5" in out[0], out
    assert "inconclusive=3" in out[0] and "refused=1" in out[0], (
        "the message must show the counters the fix added, or it names numbers "
        "that do not explain the gap"
    )
