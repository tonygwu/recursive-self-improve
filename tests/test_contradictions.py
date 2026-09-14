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
