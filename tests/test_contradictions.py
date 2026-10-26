"""Tests for contradictions.py: candidate banding, canonical pair orientation,
judge budget/failure accounting, verdict storage, report filtering.

Embeddings are injected deterministic vectors (same ``_vec`` angle pattern as
tests/test_cluster.py — unmapped text raises KeyError, loudly); the LLM judge
is a recorded fake callable with a strict scripted response list. No real
model, no real LLM. judge_pairs tests render the REAL repo prompt template,
so a placeholder drift in prompts/judge_contradiction.md fails here.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from self_improve.config import Config
from self_improve.contradictions import find_candidates, judge_pairs, report_open
from self_improve.store import Store, new_id, utc_now_iso

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPTS_DIR = REPO_ROOT / "prompts"

CAND_T = Config().contradiction_candidate_cosine  # 0.55
DUP_T = Config().cluster_dup_cosine  # 0.85


def _vec(deg: float) -> list[float]:
    """Unit vector at ``deg`` degrees: cosine(_vec(a), _vec(b)) == cos(a-b)."""
    r = math.radians(deg)
    return [math.cos(r), math.sin(r)]


# Deterministic text -> vector map. Band is [0.55, 0.85): cos40°≈0.766 and
# cos45°≈0.707 are candidates; cos60°=0.5 is below the floor; cos10°≈0.985
# and cos5°≈0.996 are near-duplicates above the ceiling. Every unit has >= 3
# content tokens so none is dropped by the tiny-unit filter.
U0 = "commit generated files into the repo"
U40 = "commit generated files sometimes maybe"
U45 = "keep generated files out of git"
U60 = "run linting before opening a pr"
U10 = "commit the generated files into repo"

_EMBED_MAP = {
    U0: _vec(0),
    U40: _vec(40),
    U45: _vec(45),
    U60: _vec(60),
    U10: _vec(10),
}


class FakeEmbedder:
    """Records cached_vector calls; unmapped text raises KeyError (fail loud)."""

    def __init__(self):
        self.calls: list[tuple[str, str, str]] = []

    def cached_vector(self, owner_kind: str, owner_key: str, text: str) -> list[float]:
        self.calls.append((owner_kind, owner_key, text))
        return _EMBED_MAP[text]


class FakeJudge:
    """Scripted llm_json callable; running past the script raises IndexError."""

    def __init__(self, responses: list):
        self.responses = list(responses)
        self.prompts: list[str] = []

    def __call__(self, prompt: str):
        self.prompts.append(prompt)
        return self.responses.pop(0)


COMPAT = {"contradicts": False, "explanation": "different scopes, never co-apply"}
CONTRA = {"contradicts": True, "explanation": "an agent cannot follow both"}


def _write(path: Path, *units: str) -> str:
    path.write_text("# Rules\n" + "".join(f"- {u}\n" for u in units), encoding="utf-8")
    return str(path)


def _cand(i: int = 0, cos: float = 0.7) -> dict:
    return {
        "file_a": f"/f/a{i}.md",
        "unit_a": f"unit a number {i}",
        "file_b": f"/f/b{i}.md",
        "unit_b": f"unit b number {i}",
        "cosine": cos,
    }


def _insert_row(store: Store, **overrides) -> str:
    row = {
        "id": new_id(),
        "run_id": "run-x",
        "file_a": "/a.md",
        "unit_a": "unit alpha text",
        "file_b": "/b.md",
        "unit_b": "unit beta text",
        "cosine": 0.7,
        "verdict": "contradicts",
        "explanation": "explained",
        "status": "new",
        "created_at": utc_now_iso(),
    }
    row.update(overrides)
    store.insert("contradictions", row)
    return row["id"]


def _rows(store: Store) -> list[dict]:
    return store.query("SELECT * FROM contradictions ORDER BY created_at")


# ------------------------------------------------------------ find_candidates

def test_pair_in_band_becomes_candidate(tmp_path):
    a = _write(tmp_path / "a.md", U0)
    b = _write(tmp_path / "b.md", U45)
    (cand,) = find_candidates(Config(), [a, b], FakeEmbedder())
    assert cand["file_a"] == a and cand["unit_a"] == U0
    assert cand["file_b"] == b and cand["unit_b"] == U45
    assert cand["cosine"] == pytest.approx(math.cos(math.radians(45)))
    assert CAND_T <= cand["cosine"] < DUP_T


def test_below_candidate_floor_excluded(tmp_path):
    a = _write(tmp_path / "a.md", U0)
    b = _write(tmp_path / "b.md", U60)  # cos60° = 0.5 < 0.55
    assert find_candidates(Config(), [a, b], FakeEmbedder()) == []


def test_near_duplicates_excluded_dedups_business(tmp_path):
    a = _write(tmp_path / "a.md", U0)
    b = _write(tmp_path / "b.md", U10)  # cos10° ≈ 0.985 >= 0.85
    assert find_candidates(Config(), [a, b], FakeEmbedder()) == []


def test_same_file_pairs_included(tmp_path):
    a = _write(tmp_path / "a.md", U0, U45)
    (cand,) = find_candidates(Config(), [a], FakeEmbedder())
    assert cand["file_a"] == a and cand["file_b"] == a
    assert {cand["unit_a"], cand["unit_b"]} == {U0, U45}


def test_sorted_by_cosine_desc(tmp_path):
    # U0-U40 (0.766) and U0-U45 (0.707) are in band; U40-U45 (cos5°≈0.996)
    # is a near-duplicate and excluded.
    a = _write(tmp_path / "a.md", U0)
    b = _write(tmp_path / "b.md", U40, U45)
    cands = find_candidates(Config(), [a, b], FakeEmbedder())
    assert [c["cosine"] for c in cands] == pytest.approx(
        [math.cos(math.radians(40)), math.cos(math.radians(45))]
    )
    assert {cands[0]["unit_a"], cands[0]["unit_b"]} == {U0, U40}


def test_tiny_units_and_missing_files_skipped_unreadable_raises(tmp_path):
    tiny = _write(tmp_path / "tiny.md", "ok")  # < 3 content tokens
    emb = FakeEmbedder()
    assert find_candidates(Config(), [tiny, str(tmp_path / "absent.md")], emb) == []
    assert emb.calls == []  # tiny units never reach the embedder
    bad = tmp_path / "bad.md"
    bad.write_bytes(b"\xff\xfe invalid utf8 \xff")
    with pytest.raises(UnicodeDecodeError):
        find_candidates(Config(), [str(bad)], emb)


def test_owner_key_scheme_matches_dedup_vector_cache(tmp_path):
    # Same owner_kind/owner_key as cluster.existing_rule_vectors, so the two
    # systems share cached vectors instead of double-encoding.
    import hashlib

    a = _write(tmp_path / "a.md", U0)
    emb = FakeEmbedder()
    find_candidates(Config(), [a], emb)
    expected_key = hashlib.sha1(f"{a}\n{U0}".encode("utf-8")).hexdigest()
    assert emb.calls == [("rule_unit", expected_key, U0)]


def test_canonical_orientation_independent_of_scan_order(tmp_path):
    zz = _write(tmp_path / "zz.md", U0)
    aa = _write(tmp_path / "aa.md", U45)
    (fwd,) = find_candidates(Config(), [zz, aa], FakeEmbedder())
    (rev,) = find_candidates(Config(), [aa, zz], FakeEmbedder())
    assert fwd == rev
    # canonical = sorted (file, unit) tuples: aa.md sorts first
    assert fwd["file_a"] == aa and fwd["file_b"] == zz
    assert (fwd["file_a"], fwd["unit_a"]) < (fwd["file_b"], fwd["unit_b"])


# --------------------------------------------------------------- judge_pairs

def test_contradicts_row_lands_with_status_new(tmp_path):
    store = Store(tmp_path / "s.db")
    judge = FakeJudge([CONTRA])
    cand = _cand(0, cos=0.71)
    stats = judge_pairs(store, Config(), [cand], judge, PROMPTS_DIR, "run-1")
    assert stats == {
        "candidates": 1,
        "judged": 1,
        "contradicts": 1,
        "compatible": 0,
        "judge_failed": 0,
        "deferred": 0,
        "skipped_already_judged": 0,
        "budget_deferred": 0,
    }
    (row,) = _rows(store)
    assert row["verdict"] == "contradicts"
    assert row["status"] == "new"
    assert row["run_id"] == "run-1"
    assert row["cosine"] == pytest.approx(0.71)
    assert row["explanation"] == CONTRA["explanation"]
    assert (row["file_a"], row["unit_a"], row["file_b"], row["unit_b"]) == (
        cand["file_a"], cand["unit_a"], cand["file_b"], cand["unit_b"],
    )
    # rendered from the real repo template: all four fields substituted in
    (prompt,) = judge.prompts
    for key in ("file_a", "unit_a", "file_b", "unit_b"):
        assert cand[key] in prompt
    assert "{{" not in prompt


def test_compatible_verdict_stored_and_prevents_rejudging(tmp_path):
    store = Store(tmp_path / "s.db")
    cand = _cand()
    stats1 = judge_pairs(store, Config(), [cand], FakeJudge([COMPAT]), PROMPTS_DIR, "r1")
    assert stats1["judged"] == 1 and stats1["compatible"] == 1
    (row,) = _rows(store)
    assert row["verdict"] == "compatible" and row["status"] == "new"
    # second run: same pair skipped, judge never called (empty script survives)
    judge2 = FakeJudge([])
    stats2 = judge_pairs(store, Config(), [cand], judge2, PROMPTS_DIR, "r2")
    assert stats2["skipped_already_judged"] == 1
    assert stats2["judged"] == 0 and stats2["judge_failed"] == 0
    assert judge2.prompts == []
    assert len(_rows(store)) == 1


def test_swapped_orientation_pair_also_skipped(tmp_path):
    store = Store(tmp_path / "s.db")
    cand = _cand()
    judge_pairs(store, Config(), [cand], FakeJudge([COMPAT]), PROMPTS_DIR, "r1")
    swapped = {
        "file_a": cand["file_b"],
        "unit_a": cand["unit_b"],
        "file_b": cand["file_a"],
        "unit_b": cand["unit_a"],
        "cosine": cand["cosine"],
    }
    judge2 = FakeJudge([])
    stats = judge_pairs(store, Config(), [swapped], judge2, PROMPTS_DIR, "r2")
    assert stats["skipped_already_judged"] == 1 and judge2.prompts == []
    assert len(_rows(store)) == 1


def test_judge_cap_defers_excess_never_silent(tmp_path):
    store = Store(tmp_path / "s.db")
    cfg = Config(contradiction_max_judgments_per_run=2)
    judge = FakeJudge([COMPAT, COMPAT])
    cands = [_cand(i) for i in range(5)]
    stats = judge_pairs(store, cfg, cands, judge, PROMPTS_DIR, "r1")
    assert stats["judged"] == 2 and stats["compatible"] == 2
    assert stats["deferred"] == 3
    assert len(judge.prompts) == 2
    assert len(_rows(store)) == 2


def test_failed_judgment_consumes_budget_counted_not_stored(tmp_path):
    # Budget counts LLM ATTEMPTS (a failed call still spends quota): cap 2,
    # one failure + one success -> third candidate deferred.
    store = Store(tmp_path / "s.db")
    cfg = Config(contradiction_max_judgments_per_run=2)
    judge = FakeJudge([None, CONTRA])
    stats = judge_pairs(store, cfg, [_cand(i) for i in range(3)], judge, PROMPTS_DIR, "r1")
    assert stats["judge_failed"] == 1
    assert stats["judged"] == 1 and stats["contradicts"] == 1
    assert stats["deferred"] == 1
    (row,) = _rows(store)  # only the successful judgment is stored
    assert (row["file_a"], row["file_b"]) == ("/f/a1.md", "/f/b1.md")


def test_invalid_judge_responses_counted_never_stored_never_guessed(tmp_path):
    bad_responses = [
        None,
        [],
        "contradicts",
        {},
        {"contradicts": "yes", "explanation": "x"},  # string, not bool
        {"contradicts": 1, "explanation": "x"},  # int, not bool
        {"contradicts": True},  # missing explanation
        {"explanation": "x"},  # missing contradicts
        {"contradicts": True, "explanation": 5},  # non-str explanation
    ]
    for i, bad in enumerate(bad_responses):
        store = Store(tmp_path / f"s{i}.db")
        stats = judge_pairs(store, Config(), [_cand()], FakeJudge([bad]), PROMPTS_DIR, "r")
        assert stats["judge_failed"] == 1, bad
        assert stats["judged"] == 0, bad
        assert _rows(store) == [], bad


def test_failed_pair_stays_eligible_next_run(tmp_path):
    # Nothing stored on failure -> the pair is re-judged (not skipped) later.
    store = Store(tmp_path / "s.db")
    cand = _cand()
    judge_pairs(store, Config(), [cand], FakeJudge([None]), PROMPTS_DIR, "r1")
    stats = judge_pairs(store, Config(), [cand], FakeJudge([CONTRA]), PROMPTS_DIR, "r2")
    assert stats["skipped_already_judged"] == 0 and stats["judged"] == 1
    (row,) = _rows(store)
    assert row["verdict"] == "contradicts"


def test_skipped_pairs_do_not_consume_budget(tmp_path):
    store = Store(tmp_path / "s.db")
    cfg = Config(contradiction_max_judgments_per_run=1)
    seen, fresh = _cand(0), _cand(1)
    _insert_row(store, file_a=seen["file_a"], unit_a=seen["unit_a"],
                file_b=seen["file_b"], unit_b=seen["unit_b"], verdict="compatible")
    stats = judge_pairs(store, cfg, [seen, fresh], FakeJudge([COMPAT]), PROMPTS_DIR, "r1")
    assert stats["skipped_already_judged"] == 1
    assert stats["judged"] == 1 and stats["deferred"] == 0


def test_end_to_end_candidates_judged_once_across_runs(tmp_path):
    # find_candidates + judge_pairs round trip: run 2 with reversed path order
    # produces the same canonical pair, which is skipped, not re-judged.
    store = Store(tmp_path / "s.db")
    a = _write(tmp_path / "a.md", U0)
    b = _write(tmp_path / "b.md", U45)
    cands1 = find_candidates(Config(), [a, b], FakeEmbedder())
    stats1 = judge_pairs(store, Config(), cands1, FakeJudge([CONTRA]), PROMPTS_DIR, "r1")
    assert stats1["contradicts"] == 1
    cands2 = find_candidates(Config(), [b, a], FakeEmbedder())
    judge2 = FakeJudge([])
    stats2 = judge_pairs(store, Config(), cands2, judge2, PROMPTS_DIR, "r2")
    assert stats2["skipped_already_judged"] == 1 and judge2.prompts == []
    assert len(_rows(store)) == 1


# --------------------------------------------------------------- report_open

def test_report_open_filters_status_and_verdict(tmp_path):
    store = Store(tmp_path / "s.db")
    keep_lo = _insert_row(store, cosine=0.6, unit_a="kept low cosine unit")
    _insert_row(store, status="dismissed")
    _insert_row(store, status="resolved")
    _insert_row(store, verdict="compatible")
    keep_hi = _insert_row(store, cosine=0.8, unit_a="kept high cosine unit")
    rows = report_open(store)
    # only new+contradicts, strongest overlap first
    assert [r["id"] for r in rows] == [keep_hi, keep_lo]


# ---------------------------------------------------------------------------
# clone copies must not multiply the candidate list (or the LLM bill)
# ---------------------------------------------------------------------------

#: A pair MEASURED to sit inside the [contradiction_candidate_cosine,
#: cluster_dup_cosine) = [0.55, 0.85) band, at 0.734. The obvious choice —
#: near-identical opposites ("Always X" / "Never X") — scores 0.869 and is
#: excluded as a near-duplicate, which made the first draft of both tests below
#: pass against an EMPTY candidate list. Worth knowing in its own right: the
#: detector cannot see a contradiction whose two sides are worded almost
#: identically.
_BANDED_PAIR = (
    "- **Always redirect CLI output to a file before parsing it.**\n",
    "- **Parse CLI output from the pipe directly rather than writing it to disk.**\n",
)


def test_one_logical_pair_is_emitted_once_across_clones(tmp_path):
    """Repeated rule pairs across working copies require one logical judgment."""
    import dataclasses

    from self_improve.config import Config
    from self_improve.contradictions import find_candidates
    from self_improve.embeddings import Embedder
    from self_improve.store import Store

    a, b = _BANDED_PAIR
    paths = []
    for i in range(6):
        d = tmp_path / f"clone-{i}"
        d.mkdir()
        (d / "AGENTS.md").write_text(a + b)
        paths.append(str(d / "AGENTS.md"))

    cfg = dataclasses.replace(Config(), state_dir=str(tmp_path / "state"))
    store = Store(cfg.state_path("state.db"))
    cands = find_candidates(cfg, paths, Embedder(cfg, store))

    # Order-independent: _canonical orients by file first, so the same two
    # rules come back as (A,B) from one clone pairing and (B,A) from another.
    logical = {tuple(sorted((c["unit_a"], c["unit_b"]))) for c in cands}
    assert len(cands) == len(logical), (
        f"{len(cands)} candidates for {len(logical)} logical pair(s); each "
        "redundant copy costs one LLM judge call"
    )


def test_distinct_rules_in_different_files_are_still_paired(tmp_path):
    """Dedup must not silently drop genuinely cross-file contradictions."""
    import dataclasses

    from self_improve.config import Config
    from self_improve.contradictions import find_candidates
    from self_improve.embeddings import Embedder
    from self_improve.store import Store

    p1 = tmp_path / "one" / "AGENTS.md"
    p1.parent.mkdir()
    p1.write_text(_BANDED_PAIR[0])
    p2 = tmp_path / "two" / "AGENTS.md"
    p2.parent.mkdir()
    p2.write_text(_BANDED_PAIR[1])

    cfg = dataclasses.replace(Config(), state_dir=str(tmp_path / "state"))
    store = Store(cfg.state_path("state.db"))
    cands = find_candidates(cfg, [str(p1), str(p2)], Embedder(cfg, store))
    assert cands, "a cross-file candidate pair was lost"
    assert cands[0]["file_a"] != cands[0]["file_b"]


def test_a_failed_judge_call_keeps_its_cause_in_the_stage_taxonomy():
    """BL-19: a judge call that never ran was counted judge_failed with no cause."""
    from self_improve.pipeline import MineCallFailed, _judge_with_causes

    causes = {}
    judge = _judge_with_causes(lambda prompt: (_ for _ in ()).throw(MineCallFailed("timeout", "slow")), causes)
    assert judge("prompt") is None
    assert causes == {"call_failed:timeout": 1}


def test_a_budget_refusal_mid_sweep_keeps_the_judged_work_and_its_counts(tmp_path):
    """BL-37: the pipeline replaced the stats with {'skipped': 'budget_exhausted'}
    although judged rows were stored, so the report said detection did not run."""
    from self_improve.contradictions import JudgeRefused

    store = Store(tmp_path / "s.db")
    calls = []

    def judge(prompt):
        calls.append(prompt)
        if len(calls) > 2:
            raise JudgeRefused("cheap pool spent")
        return COMPAT

    stats = judge_pairs(store, Config(), [_cand(i) for i in range(5)], judge, PROMPTS_DIR, "r1")
    assert stats["judged"] == 2 and stats["budget_deferred"] == 3 and stats["deferred"] == 3, stats
    assert "skipped" not in stats
    assert len(_rows(store)) == 2


def test_a_pair_judged_in_an_earlier_run_is_not_counted_as_budget_deferred(tmp_path):
    """BL-37 review: after a refusal every later candidate was counted as
    budget_deferred, including a pair an earlier run had already judged."""
    from self_improve.contradictions import JudgeRefused

    store = Store(tmp_path / "s.db")
    first = judge_pairs(store, Config(), [_cand(0)], lambda prompt: COMPAT, PROMPTS_DIR, "r1")
    assert first["judged"] == 1, first

    def refuse(prompt):
        raise JudgeRefused("cheap pool spent")

    stats = judge_pairs(
        store, Config(), [_cand(1), _cand(0), _cand(2)], refuse, PROMPTS_DIR, "r2"
    )
    assert stats["skipped_already_judged"] == 1, stats
    assert stats["budget_deferred"] == 2 and stats["deferred"] == 2, stats
    assert stats["judged"] == 0, stats


def _store_run_with(store: Store, entry, *, raw: str | None = None) -> str:
    """Insert one run whose stats hold ``entry``, or exactly ``raw``."""
    import json

    run_id = new_id()
    store.insert("runs", {
        "id": run_id, "started": utc_now_iso(), "finished": utc_now_iso(),
        "status": "ok", "report_path": "",
        "stats_json": raw if raw is not None else json.dumps({"contradictions": entry}),
    })
    store.commit()
    return run_id


def _refused_sweep(store: Store, *, judge_first: int, fail_first: int = 0) -> dict:
    """Stats from the real refusal path, with the pipeline's wrappers in its
    order. The first `fail_first` calls fail to launch, the next
    `judge_first` return a verdict, and _refusable turns the budget refusal
    of the call after them into JudgeRefused."""
    from self_improve.contradictions import JudgeRefused
    from self_improve.pipeline import (
        BudgetExhaustedSignal, MineCallFailed, _judge_with_causes, _refusable)

    calls = []

    def llm_json(prompt):
        calls.append(prompt)
        if len(calls) <= fail_first:
            raise MineCallFailed("spawn_error", "invented launch failure")
        if len(calls) > fail_first + judge_first:
            raise BudgetExhaustedSignal("cheap pool spent")
        return COMPAT

    causes: dict = {}
    stats = judge_pairs(
        store, Config(), [_cand(i) for i in range(4)],
        _judge_with_causes(_refusable(llm_json, JudgeRefused), causes), PROMPTS_DIR, "r1",
    )
    if causes:
        stats["taxonomy"] = dict(causes)
    return stats


def test_a_sweep_the_budget_refused_entirely_is_not_a_completed_check(tmp_path):
    """BL-37 review: a sweep that judged nothing still looked completed, so
    `selfimprove contradictions` printed 'No open contradictions.'"""
    from self_improve.contradictions import ever_completed

    store = Store(tmp_path / "s.db")
    stats = _refused_sweep(store, judge_first=0)
    assert stats["judged"] == 0 and stats["budget_deferred"] == 4, stats
    _store_run_with(store, stats)
    assert ever_completed(store) == (False, "call budget refused every judgment x1")


def test_a_sweep_that_judged_some_pairs_before_the_refusal_is_completed(tmp_path):
    """The partial case counts, as a sweep that reached its judgment cap does:
    its verdicts are stored and its deferred pairs are offered again."""
    from self_improve.contradictions import ever_completed

    store = Store(tmp_path / "s.db")
    stats = _refused_sweep(store, judge_first=1)
    assert stats["judged"] == 1 and stats["budget_deferred"] == 3, stats
    _store_run_with(store, stats)
    assert ever_completed(store) == (True, "")


def test_judgment_calls_that_failed_before_the_refusal_are_named(tmp_path):
    """Review fix (BL-37): two judgment calls failed, each spending a call
    from the pool, and then the budget refused the third. The CLI said "call
    budget refused every judgment" and the report said the mining pool "was
    already spent". Both were false: two calls ran."""
    from self_improve import report
    from self_improve.contradictions import ever_completed

    store = Store(tmp_path / "s.db")
    stats = _refused_sweep(store, judge_first=0, fail_first=2)
    assert (stats["judged"], stats["judge_failed"], stats["budget_deferred"]) == (0, 2, 2), stats
    assert stats["taxonomy"] == {"call_failed:spawn_error": 2}, stats
    _store_run_with(store, stats)
    assert ever_completed(store) == (
        False, "no pair judged: 2 judgment call(s) failed, then the call budget refused the next x1")
    lines = [l for l in report._contradiction_starved_lines({"contradictions": stats}, store, run_id="r1") if l]
    assert len(lines) == 1, lines
    assert lines[0].startswith(
        "- **Contradiction detection judged no pair: its judgment calls failed, then the "
        "mining pool ran out.** The call budget left 2 pair(s) unjudged; 0 judged. Those "
        "pairs are offered again next run. 2 judgment call(s) failed before the refusal, "
        "and those pairs are offered again too."), lines
    assert "already spent" not in lines[0]


def test_a_partial_sweep_names_its_failed_calls_too(tmp_path):
    from self_improve import report

    store = Store(tmp_path / "s.db")
    stats = _refused_sweep(store, judge_first=1, fail_first=1)
    assert (stats["judged"], stats["judge_failed"], stats["budget_deferred"]) == (1, 1, 2), stats
    lines = [l for l in report._contradiction_starved_lines({"contradictions": stats}, store, run_id="r1") if l]
    assert lines[0].startswith(
        "- **Contradiction detection stopped early: the mining pool ran out.** The call "
        "budget left 2 pair(s) unjudged; 1 judged. Those pairs are offered again next run. "
        "1 judgment call(s) failed before the refusal"), lines


def test_a_refusal_before_any_call_still_says_the_pool_was_already_spent(tmp_path):
    from self_improve import report

    store = Store(tmp_path / "s.db")
    stats = _refused_sweep(store, judge_first=0)
    lines = [l for l in report._contradiction_starved_lines({"contradictions": stats}, store, run_id="r1") if l]
    assert lines[0].startswith(
        "- **Contradiction detection judged no pair: the mining pool was already spent.** "
        "The call budget left 4 pair(s) unjudged; 0 judged. Those pairs are offered again "
        "next run. It bills to"), lines


def _capped_sweep(store: Store, *, fail_first: int) -> dict:
    """Five candidates under a judgment cap of 3. The first ``fail_first``
    calls fail to launch and the rest return a verdict, through the same
    wrappers the pipeline uses. A failed call counts toward the cap."""
    from self_improve.contradictions import JudgeRefused
    from self_improve.pipeline import MineCallFailed, _judge_with_causes, _refusable

    calls = []

    def llm_json(prompt):
        calls.append(prompt)
        if len(calls) <= fail_first:
            raise MineCallFailed("spawn_error", "invented launch failure")
        return COMPAT

    causes: dict = {}
    stats = judge_pairs(
        store, Config(contradiction_max_judgments_per_run=3), [_cand(i) for i in range(5)],
        _judge_with_causes(_refusable(llm_json, JudgeRefused), causes), PROMPTS_DIR, "r1",
    )
    assert len(calls) == 3, calls
    return stats


_CAP_TAIL = ("Deferred pairs are not lost. They are skipped this run and offered again "
             "next run")


@pytest.mark.parametrize("fail_first,judged", [(3, 0), (2, 1)])
def test_the_judgment_cap_line_names_failed_judgment_calls(tmp_path, fail_first, judged):
    """Review fix (BL-37): with a cap of 3, three judgment calls failed and two
    pairs were deferred. The report blamed the cap "rather than that anything
    failed", so an operator could raise the cap instead of fixing the judge.
    Each failed call counted toward the cap, so the line must name them."""
    from self_improve import report

    store = Store(tmp_path / "s.db")
    stats = _capped_sweep(store, fail_first=fail_first)
    assert {k: stats[k] for k in ("judged", "judge_failed", "deferred", "budget_deferred")} == {
        "judged": judged, "judge_failed": fail_first, "deferred": 2, "budget_deferred": 0}, stats
    lines = [l for l in report._contradiction_starved_lines({"contradictions": stats}, store, run_id="r1") if l]
    assert lines == [
        "- **Contradiction detection reached its judgment cap: 2 pair(s) deferred**, "
        f"{judged} judged. {fail_first} judgment call(s) failed, and those pairs are offered "
        "again too. Each failed call counted toward the cap. " + _CAP_TAIL + ". A standing "
        "backlog here can mean that the cap (`contradiction_max_judgments_per_run`) is "
        "below the candidate rate, or that judgment calls keep failing. Find out why the "
        "calls failed before you raise the cap."], lines
    assert "rather than that anything failed" not in lines[0]
    assert "—" not in lines[0]


def test_the_judgment_cap_line_without_failures_still_blames_only_the_cap(tmp_path):
    from self_improve import report

    store = Store(tmp_path / "s.db")
    stats = _capped_sweep(store, fail_first=0)
    assert (stats["judged"], stats["judge_failed"], stats["deferred"]) == (3, 0, 2), stats
    lines = [l for l in report._contradiction_starved_lines({"contradictions": stats}, store, run_id="r1") if l]
    assert lines == [
        "- **Contradiction detection reached its judgment cap: 2 pair(s) deferred**, "
        "3 judged. " + _CAP_TAIL + ", so a standing backlog here means the cap "
        "(`contradiction_max_judgments_per_run`) is below the candidate rate rather than "
        "that anything failed."], lines
    assert "—" not in lines[0]


@pytest.mark.parametrize("entry,reason", [
    ({"judged": 0, "judge_failed": 0, "deferred": 4, "budget_deferred": "4"},
     "unreadable budget_deferred counter"),
    ({"judged": None, "judge_failed": 0, "deferred": 4, "budget_deferred": 4},
     "unreadable judged counter"),
])
def test_unreadable_refusal_counters_never_count_as_a_completed_check(tmp_path, entry, reason):
    from self_improve.contradictions import ever_completed

    store = Store(tmp_path / "s.db")
    run_id = _store_run_with(store, entry)
    assert ever_completed(store) == (False, f"{reason} in run {run_id} x1")


# Each shape is one the stage cannot have written. Review of BL-37 found
# ever_completed, the report line and the pool history each judged them by
# its own rule: a float counted, a negative count was "left -1 pair(s)
# unjudged", and a string `judged` was a completed check.
_BAD_COUNTERS = [
    ({"judged": 0, "judge_failed": 0, "deferred": 4, "budget_deferred": -1},
     "unreadable budget_deferred counter", "are unreadable"),
    ({"judged": 0, "judge_failed": 0, "deferred": 4, "budget_deferred": 4.0},
     "unreadable budget_deferred counter", "are unreadable"),
    ({"judged": 0.0, "judge_failed": 0, "deferred": 4, "budget_deferred": 4},
     "unreadable judged counter", "are unreadable"),
    ({"judged": 0, "judge_failed": 0, "deferred": 4, "budget_deferred": True},
     "unreadable budget_deferred counter", "are unreadable"),
    ({"judged": -3, "judge_failed": 0, "deferred": 0, "budget_deferred": 0},
     "unreadable judged counter", "are unreadable"),
    ({"judged": "x", "judge_failed": 0, "deferred": 0},
     "unreadable judged counter", "are unreadable"),
    ({"judged": 4, "deferred": 0},
     "missing judge_failed counter", "are unreadable"),
    ({"judged": 0, "judge_failed": 0, "deferred": 2, "budget_deferred": 5},
     "budget_deferred counter above deferred", "disagree"),
    ({"judged": 3, "judge_failed": 0, "deferred": 2, "budget_deferred": 5},
     "budget_deferred counter above deferred", "disagree"),
]


@pytest.mark.parametrize("entry,reason,line", _BAD_COUNTERS)
def test_every_reader_refuses_the_same_unreadable_counters(tmp_path, entry, reason, line):
    """Review fix (BL-37): one counter check serves ever_completed, the
    report's refusal line and the pool history, so none of them infers a
    cause from a record the others call unreadable."""
    from self_improve import report
    from self_improve.contradictions import ever_completed

    store = Store(tmp_path / "s.db")
    run_id = _store_run_with(store, entry)
    assert ever_completed(store) == (False, f"{reason} in run {run_id} x1")
    lines = [l for l in report._contradiction_starved_lines({"contradictions": entry}, store, run_id=run_id) if l]
    assert len(lines) == 1 and lines[0].startswith(f"- Contradiction stage counters {line}:"), lines
    assert "mining pool" not in lines[0] and "judgment cap" not in lines[0], lines
    # The pool history speaks only beside a refusal, so store one.
    _store_run_with(store, _REFUSED)
    history = report._contradiction_pool_history(store)
    assert "1 had contradiction judgments refused by this pool, and 0 ran" in history, history
    assert f"1 more had a record this count could not read, so it may miss refusals there: `{run_id}`." in history


_REFUSED = {"candidates": 4, "judged": 0, "contradicts": 0, "compatible": 0, "judge_failed": 0,
            "deferred": 4, "budget_deferred": 4, "skipped_already_judged": 0}


@pytest.mark.parametrize("raw", ["[]", "null", '"x"', '{"contradictions": []}', "{oops"])
def test_a_run_record_that_is_not_an_object_is_named_not_raised(tmp_path, raw):
    """Review fix (BL-37): a stats_json of [] raised AttributeError, without
    the run id, from ever_completed and the pool history. The pool history
    also skipped unreadable rows while it claimed to count every run."""
    from self_improve import report
    from self_improve.contradictions import ever_completed

    store = Store(tmp_path / "s.db")
    run_id = _store_run_with(store, None, raw=raw)
    completed, why = ever_completed(store)
    assert completed is False and why.endswith(f" in run {run_id} x1"), why
    _store_run_with(store, _REFUSED)
    history = report._contradiction_pool_history(store)
    assert f"could not read, so it may miss refusals there: `{run_id}`." in history, history


def test_the_pool_history_does_not_count_another_skip_as_a_run(tmp_path):
    """A stage skipped for another reason did not run the sweep, so it is not
    a run that "ran the stage without a refusal"."""
    from self_improve import report

    store = Store(tmp_path / "s.db")
    _store_run_with(store, {"skipped": "invented_reason"})
    _store_run_with(store, _REFUSED)
    history = report._contradiction_pool_history(store)
    assert "1 had contradiction judgments refused by this pool, and 0 ran" in history, history
    assert "could not read" not in history, history


def test_the_report_names_a_stage_entry_that_is_not_an_object(tmp_path):
    """ever_completed and the pool history call this entry unreadable, so the
    run's own report must say so too rather than print nothing."""
    from self_improve import report

    store = Store(tmp_path / "s.db")
    lines = [l for l in report._contradiction_starved_lines({"contradictions": []}, store, run_id="r1") if l]
    assert lines == ["- The contradiction stage recorded [], not an object. "
                     "Nothing is inferred from it here."], lines


def test_an_entry_from_before_the_refusal_counter_still_counts(tmp_path):
    """Before BL-37 a refusal replaced the whole entry, so a counters entry
    without budget_deferred had no refusal."""
    from self_improve.contradictions import ever_completed

    store = Store(tmp_path / "s.db")
    _store_run_with(store, {"candidates": 4, "judged": 4, "deferred": 0, "judge_failed": 0})
    assert ever_completed(store) == (True, "")


# Before BL-37 the pipeline replaced the whole entry with this when the budget
# refused a judgment, even after the sweep had stored verdicts. Today's
# pipeline cannot write it, so these tests type the stored record as it is.
_OLD_REFUSAL = {"skipped": "budget_exhausted"}
# ever_completed's reason for such a run with no verdict in the database.
_OLD_NO_VERDICT = ("skipped: budget_exhausted, and this database holds no verdict from "
                   "that run; a state rebuild removes stored verdicts x1")


def test_an_older_refusal_after_stored_verdicts_is_a_completed_check(tmp_path):
    """Review fix (BL-37): a refusal stored before BL-37 has the older shape.
    A run that stored verdicts before it read "No contradiction has ever
    been computed" in the CLI, though its rows were in the table."""
    from self_improve.contradictions import ever_completed

    store = Store(tmp_path / "s.db")
    run_id = _store_run_with(store, _OLD_REFUSAL)
    assert ever_completed(store) == (False, _OLD_NO_VERDICT)
    for i in range(2):
        _insert_row(store, run_id=run_id, unit_a=f"unit a {i}", verdict="compatible")
    store.commit()
    assert ever_completed(store) == (True, "")


def test_verdicts_stored_by_another_run_do_not_complete_an_older_refusal(tmp_path):
    from self_improve.contradictions import ever_completed

    store = Store(tmp_path / "s.db")
    _store_run_with(store, _OLD_REFUSAL)
    _insert_row(store, run_id="invented-other-run", verdict="compatible")
    store.commit()
    assert ever_completed(store) == (False, _OLD_NO_VERDICT)


def test_the_report_counts_the_verdicts_an_older_refusal_left_stored(tmp_path):
    """Review fix (BL-37): a regenerated report for such a run said
    "Contradiction detection did not run"."""
    from self_improve import report

    store = Store(tmp_path / "s.db")
    run_id = _store_run_with(store, _OLD_REFUSAL)
    for i in range(2):
        _insert_row(store, run_id=run_id, unit_a=f"unit a {i}", verdict="compatible")
    _insert_row(store, run_id="invented-other-run", unit_a="unit a 9", verdict="compatible")
    store.commit()
    lines = [l for l in report._contradiction_starved_lines(
        {"contradictions": _OLD_REFUSAL}, store, run_id=run_id) if l]
    assert len(lines) == 1, lines
    assert lines[0].startswith(
        "- **Contradiction detection stopped early: the mining pool ran out.** It judged "
        "at least 2 pair(s) before the refusal, and their verdicts are stored. This run "
        "recorded the refusal in the older shape, without counters, so the number of "
        "pairs left unjudged is unknown. It bills to `max_cheap_calls_per_run`"), lines
    assert "did not run" not in lines[0]


def test_an_older_refusal_with_no_stored_verdict_claims_nothing_it_cannot_know(tmp_path):
    """The older shape lost judge_failed too, so the report cannot say the
    pool was already spent: judgment calls may have failed first."""
    from self_improve import report

    store = Store(tmp_path / "s.db")
    run_id = _store_run_with(store, _OLD_REFUSAL)
    _insert_row(store, run_id="invented-other-run", verdict="compatible")
    store.commit()
    lines = [l for l in report._contradiction_starved_lines(
        {"contradictions": _OLD_REFUSAL}, store, run_id=run_id) if l]
    assert lines[0].startswith(
        "- **Contradiction detection was refused by the call budget, and this database "
        "holds no verdict from this run.** This run recorded the refusal in the older "
        "shape, without counters, so it cannot say whether judgment calls failed before "
        "the refusal or how many pairs were left unjudged. A state rebuild removes every "
        "stored verdict, so this run may have judged pairs whose verdicts are gone. It "
        "bills to"), lines
    assert "did not run" not in lines[0] and "already spent" not in lines[0]


def test_a_state_rebuild_removes_the_verdicts_an_older_refusal_stored(tmp_path):
    """Review fix (BL-37): rebuild_state deletes every contradictions row. A
    regenerated report for an older-shape refusal then said the run "stored
    no verdict", though it had stored two. The run is no longer a completed
    check, because this database holds none of its verdicts, and both
    readers say that a rebuild removes verdicts."""
    from self_improve import report
    from self_improve.contradictions import ever_completed
    from self_improve.rebuild import rebuild_state

    store = Store(tmp_path / "s.db")
    run_id = _store_run_with(store, _OLD_REFUSAL)
    for i in range(2):
        _insert_row(store, run_id=run_id, unit_a=f"unit a {i}", verdict="compatible")
    store.commit()
    assert ever_completed(store) == (True, "")
    rebuild_state(store, export_path=tmp_path / "rebuild-backup")
    assert store.query("SELECT id FROM contradictions") == []
    assert ever_completed(store) == (False, _OLD_NO_VERDICT)
    lines = [l for l in report._contradiction_starved_lines(
        {"contradictions": _OLD_REFUSAL}, store, run_id=run_id) if l]
    assert lines[0].startswith(
        "- **Contradiction detection was refused by the call budget, and this database "
        "holds no verdict from this run.**"), lines
    assert "A state rebuild removes every stored verdict" in lines[0], lines
    assert "stored no verdict" not in lines[0], lines
