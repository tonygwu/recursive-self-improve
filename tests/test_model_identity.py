"""Real tiny models expose revision, content, and cached-vector mismatches."""
from __future__ import annotations

import dataclasses
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil

import numpy as np
import pytest
from model2vec import StaticModel
from tokenizers import Tokenizer, models, pre_tokenizers

from self_improve.config import Config
from self_improve.embeddings import Embedder, EmbeddingError
from self_improve.search import search_learnings
from self_improve.store import Store, utc_now_iso

REV_A = "a" * 40
REV_B = "b" * 40


def tiny_model(path, *, reverse=False):
    tokenizer = Tokenizer(models.WordLevel({"[UNK]": 0, "red": 1, "blue": 2}, unk_token="[UNK]"))
    tokenizer.pre_tokenizer = pre_tokenizers.WhitespaceSplit()
    vectors = [[0, 0], [1, 0], [0, 1]]
    if reverse:
        vectors[1:] = vectors[1:][::-1]
    StaticModel(np.array(vectors, dtype=np.float32), tokenizer, normalize=True).save_pretrained(path)
    return path


def model_cfg(tmp_path, model=None, **kwargs):
    return dataclasses.replace(Config(),
        embedding_model=str(model or tiny_model(tmp_path / "model")),
        state_dir=str(tmp_path / "state"),
        global_claude_md=str(tmp_path / "instructions/CLAUDE.md"),
        codex_global_agents_md=str(tmp_path / "instructions/AGENTS.md"),
        skills_dir=str(tmp_path / "skills"),
        **kwargs)


def put_cache(store, model, text="red", vec=None, owner="learning-1"):
    store.insert("embeddings", {"owner_kind": "learning", "owner_key": owner,
        "model": model, "text_sha": hashlib.sha1(text.encode()).hexdigest(),
        "vector_json": json.dumps([0.0, 1.0] if vec is None else vec), "created_at": utc_now_iso()})


def test_hub_loader_receives_the_pinned_snapshot_not_the_repository_name(tmp_path, monkeypatch):
    import huggingface_hub
    cfg = Config()
    snapshot = tiny_model(tmp_path / cfg.embedding_revision)
    calls = []
    def download(repo_id, **kwargs):
        calls.append((repo_id, kwargs))
        assert kwargs.get("revision") == cfg.embedding_revision, "loader selected a moving Hub ref"
        return str(snapshot)
    monkeypatch.setattr(huggingface_hub, "snapshot_download", download)
    assert Embedder(cfg, None).encode(["red"]) == [[1.0, 0.0]]
    assert calls and all(c[0] == cfg.embedding_model for c in calls)


@pytest.mark.parametrize("revision", ["", "main", "v1", "abc1234"])
def test_moving_or_missing_hub_revisions_fail_before_download(monkeypatch, revision):
    import huggingface_hub
    monkeypatch.setattr(huggingface_hub, "snapshot_download", lambda *a, **k: pytest.fail("download reached"))
    with pytest.raises(EmbeddingError, match="full.*commit"):
        Embedder(Config(embedding_revision=revision), None).encode(["red"])


def test_name_only_cache_rows_are_preserved_but_never_relabelled_as_pinned(tmp_path):
    cfg = model_cfg(tmp_path)
    with closing(Store(cfg.state_path("state.db"))) as store:
        put_cache(store, cfg.embedding_model)
        vector = Embedder(cfg, store).cached_vector("learning", "learning-1", "red")
        assert vector == [1.0, 0.0], "a legacy vector was reused under a pinned identity"
        rows = store.query("SELECT model, vector_json FROM embeddings")
        assert len(rows) == 2
        assert any(r["model"] == cfg.embedding_model and json.loads(r["vector_json"]) == [0, 1] for r in rows)


def test_changing_hub_revision_cannot_reuse_the_other_revisions_vectors(tmp_path, monkeypatch):
    import huggingface_hub
    paths = {REV_A: tiny_model(tmp_path / REV_A), REV_B: tiny_model(tmp_path / REV_B, reverse=True)}
    monkeypatch.setattr(huggingface_hub, "snapshot_download", lambda *a, **k: str(paths[k.get("revision", REV_A)]))
    cfg = Config(embedding_model="example/invented-model", embedding_revision=REV_A)
    with closing(Store(tmp_path / "state.db")) as store:
        assert Embedder(cfg, store).cached_vector("learning", "learning-1", "red") == [1, 0]
        changed = dataclasses.replace(cfg, embedding_revision=REV_B)
        assert Embedder(changed, store).cached_vector("learning", "learning-1", "red") == [0, 1]
        assert len(store.query("SELECT DISTINCT model FROM embeddings")) == 2


def test_local_model_content_changes_invalidate_vectors_at_the_same_path(tmp_path):
    cfg = model_cfg(tmp_path)
    with closing(Store(cfg.state_path("state.db"))) as store:
        assert Embedder(cfg, store).cached_vector("learning", "learning-1", "red") == [1, 0]
        path = Path(cfg.embedding_model)
        stamps = {p: p.stat() for p in path.iterdir()}
        tiny_model(path, reverse=True)
        for p, stamp in stamps.items():
            os.utime(p, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        assert Embedder(cfg, store).cached_vector("learning", "learning-1", "red") == [0, 1]


def test_missing_local_model_never_falls_through_to_a_hub_lookup(tmp_path, monkeypatch):
    import huggingface_hub
    monkeypatch.setattr(huggingface_hub, "snapshot_download", lambda *a, **k: pytest.fail("local path sent to Hub"))
    cfg = Config(embedding_model=str(tmp_path / "absent"))
    with pytest.raises(EmbeddingError, match="local.*(missing|exist)"):
        Embedder(cfg, None).encode(["red"])


def test_a_different_hub_snapshot_is_rejected_before_loading(tmp_path, monkeypatch):
    import huggingface_hub
    wrong = tiny_model(tmp_path / REV_B)
    monkeypatch.setattr(huggingface_hub, "snapshot_download", lambda *a, **k: str(wrong))
    monkeypatch.setattr(StaticModel, "from_pretrained", lambda *a, **k: pytest.fail("wrong snapshot loaded"))
    with pytest.raises(EmbeddingError, match="different embedding revision"):
        Embedder(Config(embedding_revision=REV_A), None).encode(["red"])


def test_file_links_are_identified_by_the_bytes_the_model_loads(tmp_path):
    cfg = model_cfg(tmp_path)
    weights = Path(cfg.embedding_model) / "model.safetensors"
    original = Embedder(cfg, None).provenance
    outside = tmp_path / "weights.safetensors"
    weights.rename(outside)
    weights.symlink_to(outside)
    linked = Embedder(cfg, None)
    assert linked.provenance == original
    assert linked.encode(["red"]) == [[1, 0]]
    alternate = tiny_model(tmp_path / "alternate", reverse=True)
    outside.write_bytes((alternate / "model.safetensors").read_bytes())
    changed = Embedder(cfg, None)
    assert changed.provenance["files_sha256"] != original["files_sha256"]
    assert changed.encode(["red"]) == [[0, 1]]


def test_directory_links_fail_before_any_model_is_loaded(tmp_path, monkeypatch):
    cfg = model_cfg(tmp_path)
    alternate = tiny_model(tmp_path / "alternate")
    (Path(cfg.embedding_model) / "nested").symlink_to(alternate, target_is_directory=True)
    monkeypatch.setattr(StaticModel, "from_pretrained", lambda *a, **k: pytest.fail("linked directory loaded"))
    with pytest.raises(EmbeddingError, match="directory links"):
        Embedder(cfg, None).encode(["red"])


def test_expected_model_digest_is_checked_before_loading(tmp_path, monkeypatch):
    cfg = model_cfg(tmp_path, embedding_model_sha256="0" * 64)
    monkeypatch.setattr(StaticModel, "from_pretrained", lambda *a, **k: pytest.fail("unchecked model loaded"))
    with pytest.raises(EmbeddingError, match="content.*mismatch"):
        Embedder(cfg, None).encode(["red"])


def test_a_model_changing_during_load_cannot_claim_the_earlier_digest(tmp_path, monkeypatch):
    cfg = model_cfg(tmp_path)
    original = StaticModel.from_pretrained
    def changing(path, **kwargs):
        model = original(path, **kwargs)
        config = Path(path) / "config.json"
        config.write_text(config.read_text() + " ")
        return model
    monkeypatch.setattr(StaticModel, "from_pretrained", changing)
    with pytest.raises(EmbeddingError, match="changed.*load"):
        Embedder(cfg, None).encode(["red"])


def test_search_reencodes_after_a_learning_text_changes(tmp_path):
    cfg = model_cfg(tmp_path)
    with closing(Store(cfg.state_path("state.db"))) as store:
        store.insert("learnings", {"id": "learning-1", "rule_text": "blue", "created_at": utc_now_iso()})
        Embedder(cfg, store).cached_vector("learning", "learning-1", "blue")
        store.update("learnings", "id", "learning-1", {"rule_text": "red"})
    assert search_learnings(cfg, "red", include_in_force=False)[0]["cosine"] == 1.0


def test_search_never_mixes_query_and_cached_vectors_from_different_models(tmp_path):
    cfg = model_cfg(tmp_path)
    with closing(Store(cfg.state_path("state.db"))) as store:
        for key, color in [("learning-1", "blue"), ("learning-2", "red")]:
            store.insert("learnings", {"id": key, "rule_text": color, "created_at": utc_now_iso()})
        Embedder(cfg, store).cached_vector("learning", "learning-1", "blue")
    tiny_model(Path(cfg.embedding_model), reverse=True)
    result = search_learnings(cfg, "red", include_in_force=False)
    assert result[0]["id"] == "learning-2", "query used new weights but cached candidate used old weights"


def test_relocated_local_models_share_identity_without_disclosing_their_paths(tmp_path):
    cfg = model_cfg(tmp_path)
    copy = shutil.copytree(cfg.embedding_model, tmp_path / "relocated")
    first = Embedder(cfg, None).provenance
    second = Embedder(dataclasses.replace(cfg, embedding_model=str(copy)), None).provenance
    assert first == second
    assert str(tmp_path) not in json.dumps(first)


def test_runtime_changes_recompute_vectors_instead_of_reusing_the_previous_runtime(tmp_path, monkeypatch):
    from self_improve import model_identity
    cfg = model_cfg(tmp_path)
    with closing(Store(cfg.state_path("state.db"))) as store:
        first = Embedder(cfg, store)
        put_cache(store, first.cache_key)  # deliberately wrong cached vector
        original = model_identity.version
        monkeypatch.setattr(model_identity, "version", lambda name: "999.0" if name == "model2vec" else original(name))
        second = Embedder(cfg, store)
        assert second.cached_vector("learning", "learning-1", "red") == [1, 0]
        assert second.cache_key != first.cache_key


@pytest.mark.parametrize("raw", ["{broken", "[]", '"string"', "[true]", "[NaN]"])
def test_search_rejects_corrupt_vectors_for_the_current_model_and_text(tmp_path, raw):
    cfg = model_cfg(tmp_path)
    with closing(Store(cfg.state_path("state.db"))) as store:
        store.insert("learnings", {"id": "learning-1", "rule_text": "red", "created_at": utc_now_iso()})
        put_cache(store, Embedder(cfg, None).cache_key)
        store.conn.execute("UPDATE embeddings SET vector_json = ?", (raw,))
    with pytest.raises(EmbeddingError, match="corrupt.*learning-1"):
        search_learnings(cfg, "red", include_in_force=False)


def test_cli_reports_a_missing_model_without_printing_a_benchmark(tmp_path, capsys):
    from self_improve import cli
    config = tmp_path / "model.toml"
    config.write_text(f"embedding_model = {json.dumps(str(tmp_path / 'missing-model'))}\n")
    assert cli.main(["--config", str(config), "eval-retrieval", "--synthetic", "--json"]) == 2
    captured = capsys.readouterr()
    assert "local model directory does not exist" in captured.err
    assert not captured.out


def test_cli_never_substitutes_defaults_for_a_missing_explicit_model_config(tmp_path, monkeypatch, capsys):
    from self_improve import cli
    from self_improve.evals import retrieval
    monkeypatch.setattr(retrieval, "run", lambda *a, **k: pytest.fail("evaluation reached with default model"))
    assert cli.main(["--config", str(tmp_path / "missing.toml"), "eval-retrieval", "--synthetic"]) == 2
    assert "config" in capsys.readouterr().err


def evaluation_cfg(tmp_path):
    from self_improve.data_boundary import SYNTHETIC_DATASET
    from self_improve.evals import retrieval
    corpus, _ = retrieval.load_corpus(SYNTHETIC_DATASET / "retrieval/corpus.jsonl")
    words = sorted({word for doc in corpus for word in doc.text.split()})
    tokenizer = Tokenizer(models.WordLevel({"[UNK]": 0, **{w: i + 1 for i, w in enumerate(words)}}, unk_token="[UNK]"))
    tokenizer.pre_tokenizer = pre_tokenizers.WhitespaceSplit()
    vectors = np.array([[1, 1]] + [[1, i + 1] for i in range(len(words))], dtype=np.float32)
    path = tmp_path / "evaluation-model"
    StaticModel(vectors, tokenizer, normalize=True).save_pretrained(path)
    return model_cfg(tmp_path, path)


def test_report_identity_matches_the_vectors_used_by_the_evaluation(tmp_path, monkeypatch):
    from self_improve.data_boundary import SYNTHETIC_DATASET
    from self_improve.evals import retrieval
    cfg = evaluation_cfg(tmp_path)
    keys = set()
    original = Embedder.encode
    def encode(self, texts):
        vectors = original(self, texts)
        keys.add(self.cache_key)
        return vectors
    monkeypatch.setattr(Embedder, "encode", encode)
    result = retrieval.run(cfg, SYNTHETIC_DATASET, kind="synthetic")
    identity = result["embedding_model_identity"]
    assert keys == {identity["cache_key"]}
    report = retrieval.render_text_report(result)
    assert identity["files_sha256"] in report
    assert identity["cache_key"] in report


def test_evaluation_refuses_a_model_changed_between_corpus_and_query_embedding(tmp_path, monkeypatch):
    from self_improve.data_boundary import SYNTHETIC_DATASET
    from self_improve.evals import retrieval
    cfg = evaluation_cfg(tmp_path)
    original = retrieval.build_corpus_store
    def change_after_corpus(*args, **kwargs):
        store = original(*args, **kwargs)
        config = Path(cfg.embedding_model) / "config.json"
        config.write_text(config.read_text() + " ")
        return store
    monkeypatch.setattr(retrieval, "build_corpus_store", change_after_corpus)
    with pytest.raises(EmbeddingError, match="content hash mismatch"):
        retrieval.run(cfg, SYNTHETIC_DATASET, kind="synthetic")
