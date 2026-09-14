"""Tests for embeddings.py against the REAL local model (potion-base-8M).

The model is a static-embedding model2vec artifact already in the HF cache
(~0.4s load, deterministic outputs), so exercising it directly is cheap and
verifies identity of what production runs will use — a fake here would test
nothing about the actual similarity space.

Includes threshold calibration: measured cosines for near-identical /
paraphrase / distinct-topic rule pairs, asserted against the Config defaults.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from self_improve.config import Config
from self_improve.embeddings import Embedder, EmbeddingError, cosine
from self_improve.store import Store

DIM = 256  # potion-base-8M output dimension


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "emb.db")
    yield s
    s.close()


# ---------------------------------------------------------------- encode

def test_encode_shape_and_plain_python_floats(store):
    emb = Embedder(Config(), store)
    vecs = emb.encode(["never trust exit 0 alone", "run pytest before committing"])
    assert isinstance(vecs, list) and len(vecs) == 2
    for v in vecs:
        assert isinstance(v, list) and len(v) == DIM
        # plain python floats (JSON-serializable), not numpy scalars
        assert all(type(x) is float for x in v)
    json.dumps(vecs)  # must round-trip as strict JSON


def test_encode_is_deterministic(store):
    emb = Embedder(Config(), store)
    a1 = emb.encode(["some rule text"])[0]
    a2 = emb.encode(["some rule text"])[0]
    assert a1 == a2


def test_model_loaded_lazily_not_at_construction(store):
    # constructing with a bogus model must not raise; first encode must.
    emb = Embedder(Config(embedding_model="///not a valid repo id"), store)
    with pytest.raises(EmbeddingError, match="not a valid repo id"):
        emb.encode(["x"])


def test_bogus_model_raises_embedding_error_no_fallback(store):
    # invalid HF repo-id shape fails locally (HFValidationError) — fast, no
    # network — and must surface as EmbeddingError, never a silent fallback.
    emb = Embedder(Config(embedding_model="///bogus"), store)
    with pytest.raises(EmbeddingError):
        emb.cached_vector("learning", "k1", "some text")
    # nothing was cached for the failed encode
    assert store.query("SELECT * FROM embeddings") == []


# ---------------------------------------------------------------- cosine

def test_cosine_basics_and_fail_loud_edges():
    assert cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)
    with pytest.raises(ValueError, match="length mismatch"):
        cosine([1.0], [1.0, 0.0])
    with pytest.raises(ValueError, match="zero-norm"):
        cosine([0.0, 0.0], [1.0, 0.0])


# ---------------------------------------------------------------- cache

def _count_encodes(monkeypatch):
    calls = {"n": 0}
    real = Embedder.encode

    def counting(self, texts):
        calls["n"] += 1
        return real(self, texts)

    monkeypatch.setattr(Embedder, "encode", counting)
    return calls


def test_cached_vector_second_call_reads_db_not_model(store, monkeypatch):
    calls = _count_encodes(monkeypatch)
    emb = Embedder(Config(), store)
    v1 = emb.cached_vector("learning", "owner-1", "read timestamps out of the data")
    assert calls["n"] == 1
    v2 = emb.cached_vector("learning", "owner-1", "read timestamps out of the data")
    assert calls["n"] == 1  # served from the embeddings table, no re-encode
    assert v1 == v2 and len(v1) == DIM
    row = store.query_one(
        "SELECT * FROM embeddings WHERE owner_kind = ? AND owner_key = ?",
        ("learning", "owner-1"),
    )
    assert row["model"] == emb.cache_key
    assert row["text_sha"] == hashlib.sha1(
        b"read timestamps out of the data"
    ).hexdigest()
    assert json.loads(row["vector_json"]) == v1


def test_cached_vector_text_sha_invalidation(store, monkeypatch):
    calls = _count_encodes(monkeypatch)
    emb = Embedder(Config(), store)
    v1 = emb.cached_vector("learning", "owner-1", "original text of the rule")
    v2 = emb.cached_vector("learning", "owner-1", "REWRITTEN text of the rule")
    assert calls["n"] == 2  # sha changed -> recompute
    assert v1 != v2
    rows = store.query("SELECT * FROM embeddings")
    assert len(rows) == 1  # upsert replaced, not duplicated
    assert rows[0]["text_sha"] == hashlib.sha1(
        b"REWRITTEN text of the rule"
    ).hexdigest()
    assert json.loads(rows[0]["vector_json"]) == v2


def test_cached_vector_distinct_owners_cached_separately(store, monkeypatch):
    calls = _count_encodes(monkeypatch)
    emb = Embedder(Config(), store)
    emb.cached_vector("learning", "owner-1", "same text")
    emb.cached_vector("rule_unit", "owner-1", "same text")
    assert calls["n"] == 2  # PK includes owner_kind
    assert len(store.query("SELECT * FROM embeddings")) == 2


def test_corrupt_cache_row_raises_not_guesses(store):
    emb = Embedder(Config(), store)
    text = "cached rule text"
    store.insert(
        "embeddings",
        {
            "owner_kind": "learning",
            "owner_key": "owner-x",
            "model": emb.cache_key,
            "text_sha": hashlib.sha1(text.encode()).hexdigest(),
            "vector_json": '"not-a-list"',
            "created_at": "2026-08-16T00:00:00Z",
        },
    )
    with pytest.raises(EmbeddingError, match="corrupt embeddings cache row"):
        emb.cached_vector("learning", "owner-x", text)


def test_a_cache_row_that_is_not_json_at_all_also_raises_clearly(store):
    """The existing test stores `'"not-a-list"'` — valid JSON of the wrong
    shape, which the validation below `json.loads` catches. A row that is not
    JSON at all took a different path: a bare JSONDecodeError with a character
    offset and no owner key, from a function whose next three lines are a
    carefully worded error naming exactly that row.

    Third instance of this shape on 2026-08-23 — routing.py, cluster.py, here.
    """
    emb = Embedder(Config(), store)
    text = "cached rule text"
    store.insert(
        "embeddings",
        {
            "owner_kind": "learning",
            "owner_key": "owner-y",
            "model": emb.cache_key,
            "text_sha": hashlib.sha1(text.encode()).hexdigest(),
            "vector_json": "{not json",
            "created_at": "2026-08-16T00:00:00Z",
        },
    )
    with pytest.raises(EmbeddingError, match="not valid JSON"):
        emb.cached_vector("learning", "owner-y", text)


# ------------------------------------------------- threshold calibration
#
# Measured 2026-08-16 on minishlab/potion-base-8M (static model, deterministic;
# values below are exact re-runs, asserted with margin):
#
#   near-identical phrasings: 0.9275, 0.9708, 0.9472   -> above dup 0.85
#   genuine paraphrases:      0.7315, 0.6189, 0.7725   -> BELOW group 0.80 (!)
#   distinct topics:          0.0426, 0.3717, 0.1493   -> below group 0.80
#
# So the defaults (group 0.80 / dup 0.85) cleanly separate near-identical
# restatements from distinct topics, but genuine paraphrases of the same
# lesson land in 0.62-0.77 and are NOT grouped/deduped at the defaults.
# Evidence for tuning: cluster_group_cosine ~0.55-0.60 would capture these
# paraphrases while keeping a >0.18 margin above the noisiest distinct pair.

NEAR_IDENTICAL_PAIRS = [
    (
        "Never derive logical time from file mtime; read timestamps from the data",
        "Never derive logical time from mtime — always read the timestamp from the data",
    ),
    (
        "Assert the reported model matches the requested class before trusting output",
        "Assert the reported model matches the requested model class before trusting any output",
    ),
    (
        "Redact transcript excerpts before they enter any LLM prompt or the DB",
        "Redact transcript excerpts before entering any LLM prompt or the database",
    ),
]

PARAPHRASE_PAIRS = [
    (
        "Never derive logical time from file mtime; read timestamps from the data itself",
        "Never use file modification time as logical time — always read the timestamp out of the data",
    ),
    (
        "Assert the reported model in the response envelope matches the requested model class",
        "Verify that the model named in the response telemetry is the model class you requested",
    ),
    (
        "Run the full test suite before every commit and never commit on red",
        "Always run pytest before committing; a failing suite blocks the commit",
    ),
]

DISTINCT_TOPIC_PAIRS = [
    (
        "Never derive logical time from file mtime; read timestamps from the data itself",
        "Assert the reported model in the response envelope matches the requested model class",
    ),
    (
        "Redact secrets from transcripts before sending them to any LLM",
        "Run the full test suite before every commit and never commit on red",
    ),
    (
        "Bake absolute binary paths into launchd plists because launchd has a minimal PATH",
        "Report progress as attempted, succeeded, and failed with an error taxonomy",
    ),
]


def test_calibration_config_defaults_vs_measured_pairs(store):
    cfg = Config()
    emb = Embedder(cfg, store)

    def sim(pair):
        va, vb = emb.encode(list(pair))
        return cosine(va, vb)

    near = [sim(p) for p in NEAR_IDENTICAL_PAIRS]
    para = [sim(p) for p in PARAPHRASE_PAIRS]
    dist = [sim(p) for p in DISTINCT_TOPIC_PAIRS]

    # The defaults separate near-identical restatements from distinct topics:
    assert min(near) >= cfg.cluster_dup_cosine, (near, cfg.cluster_dup_cosine)
    assert max(dist) < cfg.cluster_group_cosine, (dist, cfg.cluster_group_cosine)

    # Measured reality the thresholds must be tuned against: genuine
    # paraphrases sit strictly BETWEEN distinct topics and near-identicals —
    # i.e. below today's group default. If this ordering ever breaks, the
    # model (or the pairs) changed and both thresholds need re-calibration.
    assert max(dist) < min(para) < max(para) < min(near), (dist, para, near)
    assert max(para) < cfg.cluster_group_cosine, (
        "paraphrase pairs now clear cluster_group_cosine — recalibrate: "
        f"{para} vs {cfg.cluster_group_cosine}"
    )


def test_loading_the_model_silences_hub_progress_bars(monkeypatch):
    """Default model loading to disabled hub progress bars.

    After an encode with no environment override, the suppression setting
    must be present. The companion test preserves an explicit override."""
    import os

    from self_improve.config import Config
    from self_improve.embeddings import Embedder

    monkeypatch.delenv("HF_HUB_DISABLE_PROGRESS_BARS", raising=False)
    emb = Embedder(Config(), None)
    emb.encode(["a short rule"])
    assert os.environ.get("HF_HUB_DISABLE_PROGRESS_BARS") == "1"


def test_an_operator_override_is_respected(monkeypatch):
    """setdefault, not set: someone who wants the bars back can have them."""
    import os

    from self_improve.config import Config
    from self_improve.embeddings import Embedder

    monkeypatch.setenv("HF_HUB_DISABLE_PROGRESS_BARS", "0")
    emb = Embedder(Config(), None)
    emb.encode(["another rule"])
    assert os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] == "0"
