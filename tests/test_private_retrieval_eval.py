"""Opt-in numerical regressions for the frozen private benchmark.

SI_PRIVATE_EVAL_DATASET must name the pinned bundle. An unset variable skips
these real-data checks; a supplied missing or changed bundle FAILS.
"""
from __future__ import annotations
import dataclasses
import os
from pathlib import Path
import pytest
from self_improve.config import Config
from self_improve.evals import retrieval as R
from self_improve.data_boundary import load_dataset

pytestmark = pytest.mark.reads_real_home

@pytest.fixture(scope="module")
def private_dataset():
    value = os.environ.get("SI_PRIVATE_EVAL_DATASET")
    if not value:
        pytest.skip("private benchmark requires explicit SI_PRIVATE_EVAL_DATASET; no synthetic substitution")
    root = Path(value).expanduser()
    load_dataset(root, kind="private", expected=Path(__file__).resolve().parents[1] / "evals/manifests/trajectory-eval-v1.json")
    return root

def test_private_corpus_and_qrels_load_and_cross_validate(private_dataset):
    corpus, meta = R.load_corpus(private_dataset / "retrieval/corpus.jsonl")
    qrels = R.load_qrels(private_dataset / "retrieval/qrels.yaml", corpus=corpus)
    assert meta["docs"] == len(corpus)
    assert meta["learnings"] + meta["rule_units"] == meta["docs"]
    assert len(qrels.judgments) > 0
    # Provenance split must contain the independent gold set.
    counts = R._qrel_counts(qrels)
    assert counts["by_provenance"]["miner_flagged"][R.RELEVANT] == 9
    assert counts["by_provenance"]["hard_negative"][R.NOT_RELEVANT] > 0
    # The qrel file must be self-contained: rebuildable with no corpus at all.
    judged = R.corpus_from_qrels(qrels)
    assert {d.doc_id for d in judged} >= {
        j.query_id for j in qrels.judgments
    } | {j.doc_id for j in qrels.judgments}


def test_every_judgment_carries_a_reason_and_a_known_provenance(private_dataset):
    qrels = R.load_qrels(private_dataset / "retrieval/qrels.yaml", corpus=R.load_corpus(private_dataset / "retrieval/corpus.jsonl")[0])
    for j in qrels.judgments:
        assert j.reason.strip(), f"{j.query_id}->{j.doc_id} has no adjudication reason"
        assert j.provenance in R.PROVENANCES


def test_gold_queries_target_instruction_rule_units_not_learnings(private_dataset):
    """The gold task is 'is this lesson already in the instruction file?'.

    If a gold target ever became a learnings row, the task would silently turn
    into learning-vs-learning dedup and the number would stop meaning what the
    report says it means.
    """
    corpus, _ = R.load_corpus(private_dataset / "retrieval/corpus.jsonl")
    by_id = {d.doc_id: d for d in corpus}
    gold = [j for j in R.load_qrels(private_dataset / "retrieval/qrels.yaml", corpus=corpus).judgments
            if j.provenance == "miner_flagged"]
    assert len(gold) == 9
    for j in gold:
        assert by_id[j.doc_id].kind == "rule_unit"
        assert by_id[j.query_id].kind == "learning"


@pytest.fixture(scope="module")
def gold_result(private_dataset):
    """Score the 9 gold queries (keeps the suite fast).

    Deliberately keeps EVERY judgment those 9 queries carry, not just their
    miner_flagged ones: 5 of the 9 are also members of an adjudicated family,
    so this fixture only reports the gold number correctly if provenance
    scoping actually isolates the gold relevant set. Pre-filtering the qrels
    here would hide exactly the bug the scoping exists to prevent.
    """
    import tempfile

    corpus, _ = R.load_corpus(private_dataset / "retrieval/corpus.jsonl")
    qrels = R.load_qrels(private_dataset / "retrieval/qrels.yaml", corpus=corpus)
    gold_ids = sorted(
        {j.query_id for j in qrels.judgments if j.provenance == "miner_flagged"}
    )
    scoped = R.Qrels(
        judgments=tuple(j for j in qrels.judgments if j.query_id in set(gold_ids)),
        documents=qrels.documents,
    )
    with tempfile.TemporaryDirectory() as tmp:
        cfg = dataclasses.replace(Config(), state_dir=tmp)
        R.build_corpus_store(cfg, corpus, cfg.state_path("state.db"))
        rankings = R.rank_all(cfg, corpus, gold_ids, maxk=8)
        result = R.evaluate(cfg, corpus, scoped, rankings=rankings)
        sweep = R.threshold_sweep(cfg, corpus, qrels)
    return result, sweep, scoped


def test_gold_relevant_sets_are_not_polluted_by_family_judgments(gold_result):
    """Provenance scoping must keep the uncontaminated set uncontaminated.

    Each gold query has exactly ONE miner-flagged relevant doc (the CLAUDE.md
    line the miner named). Several of those queries are also family members
    with 11 human-adjudicated positives; if the two pooled, the gold row would
    silently become a mixed-evidence number.
    """
    _, _, scoped = gold_result
    gold_ids = {j.query_id for j in scoped.judgments if j.provenance == "miner_flagged"}
    also_in_a_family = 0
    for qid in gold_ids:
        assert len(scoped.relevant(qid, provenance="miner_flagged")) == 1
        if scoped.relevant(qid, provenance="adjudicated_family"):
            also_in_a_family += 1
        # The unscoped (product-question) set is a superset, never equal by luck.
        assert scoped.relevant(qid) >= scoped.relevant(qid, provenance="miner_flagged")
    assert also_in_a_family == 5, (
        "expected 5 gold queries to double as family members; if this changes, "
        "the pollution risk this test guards has changed too"
    )


def test_gold_recall_at_8_is_pinned(gold_result):
    """Headline regression gate: 5 of 9 gold duplicates reachable in top-8.

    recall@8 is THE number — search_learnings returns 8 rows, so a duplicate
    ranked 9th is invisible to the miner at any cosine.
    """
    result, _, _ = gold_result
    gold = result["by_provenance"]["miner_flagged"]
    assert gold["n_queries_with_positives"] == 9
    assert gold["semantic"]["recall@8"] == pytest.approx(5 / 9, abs=1e-6)
    assert gold["keyword_bm25_proxy"]["recall@8"] == pytest.approx(6 / 9, abs=1e-6)
    assert gold["oracle_union"]["recall@8"] == pytest.approx(7 / 9, abs=1e-6)
    # Blending is worth more than either arm alone on the gold set.
    assert gold["oracle_union"]["recall@8"] > gold["semantic"]["recall@8"]


def test_gold_recall_ceiling_is_one_so_the_shortfall_is_not_arithmetic(gold_result):
    result, _, _ = gold_result
    ceiling = result["by_provenance"]["miner_flagged"]["recall_ceiling"]
    assert ceiling["recall@8"] == pytest.approx(1.0)


def test_no_cosine_threshold_separates_gold_duplicates_from_unrelated_rules(gold_result):
    """On this pinned private benchmark, no cosine threshold separates every gold duplicate from unrelated rules."""
    _, sweep, _ = gold_result
    scope = sweep["scopes"]["miner_flagged"]
    sep = scope["separable"]
    assert sep["separable"] is False
    assert sep["min_positive_cosine"] < sep["max_negative_cosine"]
    # Negatives are derived, not hand-picked: gold query x every non-target unit.
    assert sweep["derived_gold_negatives"] == 9 * 25


def test_live_threshold_catches_none_of_the_nine_gold_duplicates(gold_result):
    _, sweep, _ = gold_result
    table = {r["threshold"]: r for r in sweep["scopes"]["miner_flagged"]["table"]}
    assert sweep["live_threshold"] == 0.85
    assert table[0.85]["true_duplicates_caught"] == 0
    assert table[0.85]["true_duplicates_total"] == 9
    # Catching all 9 requires <=0.45, which admits 40 of 225 unrelated pairs.
    assert table[0.45]["true_duplicates_caught"] == 9
    assert table[0.45]["false_positives_admitted"] == 40


def test_sweep_reports_what_each_threshold_cuts_never_a_bare_count(gold_result):
    _, sweep, _ = gold_result
    for scope in sweep["scopes"].values():
        for row in scope["table"]:
            assert {"true_duplicates_caught", "true_duplicates_total",
                    "false_positives_admitted", "negatives_total"} <= set(row)
