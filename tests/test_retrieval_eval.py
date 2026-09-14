"""Public retrieval algorithm tests use invented inputs and no personal history."""
from __future__ import annotations

import dataclasses
import inspect
import json

import pytest
import yaml

from self_improve.config import Config
from self_improve.evals import retrieval as R


# ---------------------------------------------------------------------------
# Content addressing — the thing that makes the qrels survive a DB rebuild
# ---------------------------------------------------------------------------


def test_content_id_is_stable_under_reflow_and_changes_on_reword():
    a = "**Never suppress stderr** — read the real error\nbefore retrying."
    reflowed = "**Never suppress stderr** —   read the real error before   retrying."
    reworded = "**Never suppress stderr** — read the real error after retrying."
    assert R.content_id(a) == R.content_id(reflowed)
    assert R.content_id(a) != R.content_id(reworded)
    assert R.content_id(a).startswith("doc:")


def test_content_id_does_not_depend_on_the_database_uuid():
    # Two Docs with the same text but different observed_as must collide by
    # design: identity is the text, not the row it happened to live in.
    d1 = R.Doc(doc_id=R.content_id("x y"), kind="learning", text="x y", observed_as="uuid-1")
    d2 = R.Doc(doc_id=R.content_id("x y"), kind="learning", text="x y", observed_as="uuid-2")
    assert d1.doc_id == d2.doc_id


# ---------------------------------------------------------------------------
# Fail-loud integrity guards
# ---------------------------------------------------------------------------


def _write_corpus(path, docs):
    payload = [dataclasses.asdict(d) for d in docs]
    meta = {
        "_meta": {
            "docs": len(payload),
            "learnings": len(payload),
            "rule_units": 0,
            "instruction_files": [],
            "rule_units_skipped_under_3_tokens": 0,
            "docs_redacted": 0,
            "payload_sha256": R._payload_sha(payload),
        }
    }
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(meta) + "\n")
        for d in payload:
            fh.write(json.dumps(d) + "\n")


def _doc(text, **kw):
    return R.Doc(doc_id=R.content_id(text), kind="learning", text=text, **kw)


def test_tampered_corpus_payload_fails_the_sha_check(tmp_path):
    p = tmp_path / "corpus.jsonl"
    _write_corpus(p, [_doc("alpha rule"), _doc("beta rule")])
    lines = p.read_text().splitlines()
    lines[1] = json.dumps({**json.loads(lines[1]), "text": "alpha rule EDITED"})
    p.write_text("\n".join(lines) + "\n")
    with pytest.raises(R.RetrievalEvalError, match="payload sha256 mismatch"):
        R.load_corpus(p)


def test_corpus_row_count_mismatch_fails_loud(tmp_path):
    p = tmp_path / "corpus.jsonl"
    _write_corpus(p, [_doc("alpha rule"), _doc("beta rule")])
    lines = p.read_text().splitlines()
    p.write_text("\n".join(lines[:-1]) + "\n")  # drop a document
    with pytest.raises(R.RetrievalEvalError, match="row count mismatch"):
        R.load_corpus(p)


def test_qrels_must_carry_every_judged_document_verbatim(tmp_path):
    p = tmp_path / "qrels.yaml"
    p.write_text(
        yaml.dump(
            {
                "documents": {R.content_id("a rule"): {"text": "a rule"}},
                "judgments": [
                    {
                        "query_id": R.content_id("a rule"),
                        "doc_id": R.content_id("another rule"),
                        "label": "relevant",
                        "provenance": "miner_flagged",
                        "reason": "r",
                    }
                ],
            }
        )
    )
    with pytest.raises(R.RetrievalEvalError) as exc:
        R.load_qrels(p)
    assert "self-contained" in str(exc.value)
    assert R.content_id("another rule") in str(exc.value)


def test_qrels_document_text_edited_without_rekeying_fails_loud(tmp_path):
    p = tmp_path / "qrels.yaml"
    cid = R.content_id("a rule")
    p.write_text(
        yaml.dump(
            {
                "documents": {cid: {"text": "a rule THAT WAS EDITED"}},
                "judgments": [],
            }
        )
    )
    with pytest.raises(R.RetrievalEvalError, match="hashes to"):
        R.load_qrels(p)


def test_unresolvable_qrels_fail_loud_naming_every_missing_entry():
    """A state-DB rebuild must never silently shrink the qrel denominator.

    Recall over a silently-shrunken qrel set goes UP as data disappears. This
    simulates the rebuild: the corpus no longer contains some judged texts.
    """
    corpus, _ = R.load_corpus()
    qrels = R.load_qrels(corpus=corpus)
    dropped = [d for d in corpus if d.kind == "rule_unit"][:3]
    shrunken = [d for d in corpus if d not in dropped]
    with pytest.raises(R.RetrievalEvalError) as exc:
        R.assert_qrels_resolvable(qrels, shrunken, where="rebuilt corpus")
    msg = str(exc.value)
    for d in dropped:
        if any(
            d.doc_id in (j.query_id, j.doc_id) for j in qrels.judgments
        ):
            assert d.doc_id in msg, "every missing id must be named, not truncated"
    assert "re-adjudicated" in msg
    assert "do not 'fix' this by dropping" in msg


def test_qrels_reject_self_judgments_and_unknown_labels(tmp_path):
    cid = R.content_id("a rule")
    base = {"documents": {cid: {"text": "a rule"}}}
    p = tmp_path / "q.yaml"
    p.write_text(
        yaml.dump(
            {
                **base,
                "judgments": [
                    {
                        "query_id": cid,
                        "doc_id": cid,
                        "label": "relevant",
                        "provenance": "miner_flagged",
                        "reason": "r",
                    }
                ],
            }
        )
    )
    with pytest.raises(R.RetrievalEvalError, match="judged against itself"):
        R.load_qrels(p)

    other = R.content_id("other rule")
    p.write_text(
        yaml.dump(
            {
                "documents": {cid: {"text": "a rule"}, other: {"text": "other rule"}},
                "judgments": [
                    {
                        "query_id": cid,
                        "doc_id": other,
                        "label": "sort-of",
                        "provenance": "miner_flagged",
                        "reason": "r",
                    }
                ],
            }
        )
    )
    with pytest.raises(R.RetrievalEvalError, match="label 'sort-of' not in"):
        R.load_qrels(p)


def test_qrels_reject_an_empty_reason(tmp_path):
    a, b = R.content_id("a rule"), R.content_id("b rule")
    p = tmp_path / "q.yaml"
    p.write_text(
        yaml.dump(
            {
                "documents": {a: {"text": "a rule"}, b: {"text": "b rule"}},
                "judgments": [
                    {
                        "query_id": a,
                        "doc_id": b,
                        "label": "relevant",
                        "provenance": "hard_negative",
                        "reason": "   ",
                    }
                ],
            }
        )
    )
    with pytest.raises(R.RetrievalEvalError, match="reason must be non-empty"):
        R.load_qrels(p)


# ---------------------------------------------------------------------------
# Metric math
# ---------------------------------------------------------------------------


def test_metrics_math_on_a_known_ranking():
    ranked = ["a", "x", "b", "y", "c", "z", "w", "v"]
    relevant = {"a", "b", "c", "d"}  # "d" is never retrieved
    m = R._metrics_for_query(ranked, relevant, borderline=set(), ks=(1, 3, 5, 8))
    assert m["recall@1"] == pytest.approx(1 / 4)
    assert m["recall@3"] == pytest.approx(2 / 4)
    assert m["recall@5"] == pytest.approx(3 / 4)
    assert m["recall@8"] == pytest.approx(3 / 4)  # "d" ranked outside 8 == invisible
    assert m["precision@1"] == pytest.approx(1 / 1)
    assert m["precision@3"] == pytest.approx(2 / 3)
    assert m["precision@8"] == pytest.approx(3 / 8)
    assert m["rr"] == pytest.approx(1.0)


def test_mrr_uses_the_first_relevant_rank():
    m = R._metrics_for_query(["x", "y", "a"], {"a"}, set(), (3,))
    assert m["rr"] == pytest.approx(1 / 3)
    m = R._metrics_for_query(["x", "y", "z"], {"a"}, set(), (3,))
    assert m["rr"] == 0.0


def test_borderline_docs_are_excluded_from_both_sides_of_precision():
    ranked = ["a", "bord", "x", "y"]
    m = R._metrics_for_query(ranked, {"a"}, borderline={"bord"}, ks=(4,))
    # 1 hit, and the borderline doc is removed from the denominator entirely:
    # 3 scoreable docs, not 4.
    assert m["precision@4"] == pytest.approx(1 / 3)
    assert m["recall@4"] == pytest.approx(1.0)


def test_union_interleaves_and_dedupes():
    assert R._union_at_k(["a", "b", "c"], ["b", "d", "e"], 4) == ["a", "b", "d", "c"]
    assert R._union_at_k(["a"], [], 3) == ["a"]


# ---------------------------------------------------------------------------
# Arms
# ---------------------------------------------------------------------------


def test_bm25_prefers_lexical_overlap_and_is_deterministic():
    corpus = [
        _doc("never suppress stderr on a failing command", why="hides the error"),
        _doc("run the linter before pushing to CI"),
        _doc("stderr suppression hides the diagnostic you need"),
    ]
    bm25 = R.BM25(corpus)
    ranked = bm25.rank("suppress stderr failing command", 3)
    assert ranked[0] == corpus[0].doc_id
    assert corpus[1].doc_id not in ranked  # zero lexical overlap -> not returned
    assert ranked == bm25.rank("suppress stderr failing command", 3)


def test_bm25_rejects_an_empty_query():
    with pytest.raises(R.RetrievalEvalError, match="non-empty"):
        R.BM25([_doc("a rule")]).rank("   ", 3)


def test_semantic_arm_excludes_the_query_document_itself(tmp_path):
    corpus = [
        _doc("never derive logical time from file mtime"),
        _doc("read timestamps out of the data, not the filesystem"),
        _doc("run the linter before pushing"),
    ]
    cfg = dataclasses.replace(Config(), state_dir=str(tmp_path / "state"))
    R.build_corpus_store(cfg, corpus, cfg.state_path("state.db"))
    ranked = R.rank_all(cfg, corpus, [corpus[0].doc_id], maxk=3)[corpus[0].doc_id]
    for arm in ("semantic", "keyword_bm25_proxy", "oracle_union"):
        assert corpus[0].doc_id not in ranked[arm], (
            f"{arm} returned the query itself, which would inflate every metric"
        )
    # ...and still returns a full k of other documents.
    assert len(ranked["semantic"]) == 2


def test_gold_detail_is_rendered_into_the_report():
    """The rendered report must include each gold query's ranking in every retrieval arm."""
    from self_improve.evals import retrieval as r

    assert callable(r.render_gold_detail)
    # run() has both the result and the qrels in scope; it must stash the
    # rendered table so render_text_report can append it without a signature
    # change or a second qrels load.
    src = inspect.getsource(r.run)
    assert "render_gold_detail" in src, "run() must render the gold detail"
    assert "gold_detail" in inspect.getsource(r.render_text_report), (
        "render_text_report must emit the stashed gold detail"
    )


def test_a_corrupt_corpus_snapshot_names_the_file_and_the_line(tmp_path):
    """Every other failure in `load_corpus` raises a RetrievalEvalError naming
    the path — missing, empty, no header, row-count mismatch. The two
    `json.loads` between them were bare, so a snapshot corrupted by a bad
    merge or a partial write surfaced as a JSONDecodeError with a character
    offset and no filename.

    Sixth instance of that shape on 2026-08-23.
    """
    from self_improve.evals.retrieval import RetrievalEvalError, load_corpus

    bad_header = tmp_path / "h.jsonl"
    bad_header.write_text("{not json\n")
    with pytest.raises(RetrievalEvalError, match="header is not valid JSON"):
        load_corpus(bad_header)

    bad_row = tmp_path / "r.jsonl"
    bad_row.write_text('{"_meta": {"docs": 1}}\n{not json\n')
    with pytest.raises(RetrievalEvalError, match="line 2 is not valid JSON"):
        load_corpus(bad_row)
