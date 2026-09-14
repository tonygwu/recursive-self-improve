

def test_every_verdict_the_gate_can_return_has_a_counter():
    """The fifth pair of lists that must agree, checked before it drifts.

    `pipeline` does `gate_stats[verdict] += 1`. A verdict the counter dict was
    not initialised with raises KeyError, which the surrounding handler files
    as `gate_KeyError` — so a new verdict would be recorded as the gate
    crashing rather than as itself. Enumerating the decision table exhaustively
    is cheap because `majority_verdict` is a pure function of the tally.
    """
    from itertools import product

    from self_improve.evals.regression import majority_verdict

    seen = set()
    for p, f, u in product(range(4), repeat=3):
        seen.add(
            majority_verdict({"gated_pass": p, "gated_fail": f, "ungated": u})
        )
    assert seen == {"gated_pass", "gated_fail", "ungated", "inconclusive"}, seen

    # The counters the pipeline actually initialises. This used to regex the
    # literal out of pipeline.py; it now calls the constructor, because a test
    # that greps source text is guessing. The scanner did fail loud when the
    # literal moved into `new_gate_stats()` — its own "update this scanner"
    # branch fired — which is the only reason this is a rewrite and not a
    # silent pass.
    from self_improve.pipeline import new_gate_stats

    counters = set(new_gate_stats())
    missing = sorted(seen - counters)
    assert not missing, (
        f"majority_verdict can return {missing}, which gate_stats does not "
        "count — pipeline would raise KeyError and file it as gate_KeyError"
    )
