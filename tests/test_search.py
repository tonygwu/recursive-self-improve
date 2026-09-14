"""Tests for the miner agent's read-only dedup search.

These tests use the pinned embedding model and temporary stores with invented
learnings. Offline runs require that model in the cache; personal history and
instruction files are replaced by temporary fixtures."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import sqlite3

import pytest

from self_improve.config import Config
from self_improve.search import SearchError, open_store_readonly, search_learnings
from self_improve.store import Store, new_id, utc_now_iso


@pytest.fixture
def cfg(tmp_path):
    # The instruction-file paths MUST be redirected into tmp_path. The default
    # Config points at the developer's real ~/.claude/CLAUDE.md and
    # ~/.claude/skills, and search now reads them — a test whose result depends
    # on the machine it runs on is not a test.
    return dataclasses.replace(
        Config(),
        state_dir=str(tmp_path / "state"),
        global_claude_md=str(tmp_path / "global" / "CLAUDE.md"),
        codex_global_agents_md=str(tmp_path / "codex" / "AGENTS.md"),
        skills_dir=str(tmp_path / "skills"),
    )


def seed(cfg, rules: list[tuple[str, str]]):
    store = Store(cfg.state_path("state.db"))
    ids = []
    for rule_text, status in rules:
        lid = new_id()
        ids.append(lid)
        store.insert(
            "learnings",
            {
                "id": lid,
                "rule_text": rule_text,
                "why": "w",
                "status": status,
                "created_at": utc_now_iso(),
            },
        )
    store.close()
    return ids


def test_ranking_prefers_semantic_match(cfg):
    ids = seed(
        cfg,
        [
            ("**Never derive logical time from file mtime** — read timestamps from the data.", "applied"),
            ("**Run the linter before pushing** — CI failures waste a round trip.", "candidate"),
        ],
    )
    results = search_learnings(cfg, "never use mtime as the logical timestamp", top_k=2)
    assert [r["id"] for r in results][0] == ids[0]
    assert results[0]["cosine"] > results[1]["cosine"]
    assert set(results[0]) >= {"id", "status", "rule_text", "why", "cosine"}


def test_status_filter_narrows(cfg):
    seed(cfg, [("rule about testing things properly", "applied"),
               ("rule about testing things thoroughly", "rejected")])
    all_rows = search_learnings(cfg, "testing rules", top_k=10)
    assert len(all_rows) == 2
    only_rejected = search_learnings(cfg, "testing rules", top_k=10, statuses=("rejected",))
    assert len(only_rejected) == 1 and only_rejected[0]["status"] == "rejected"


def test_missing_db_and_empty_query_fail_loud(cfg):
    with pytest.raises(SearchError, match="does not exist"):
        search_learnings(cfg, "anything")
    seed(cfg, [("some rule text here", "candidate")])
    with pytest.raises(SearchError, match="non-empty"):
        search_learnings(cfg, "   ")


def test_connection_is_readonly(cfg):
    seed(cfg, [("a rule", "candidate")])
    conn = open_store_readonly(cfg)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("INSERT INTO learnings (id, rule_text, created_at) VALUES ('x', 'r', 'now')")
    conn.close()


def test_uses_cached_vectors_when_present(cfg):
    # A cached vector row for a learning must be used as-is (no re-encode):
    # plant a deliberately WRONG cached vector and observe it dominate.
    ids = seed(cfg, [("completely unrelated cooking recipe advice", "candidate"),
                     ("never use mtime for logical time", "candidate")])
    store = Store(cfg.state_path("state.db"))
    from self_improve.embeddings import Embedder

    emb = Embedder(Config(), None)
    (query_like_vec,) = emb.encode(["never use mtime for logical time"])
    # Cache the recipe learning's vector AS the query-like vector.
    store.insert(
        "embeddings",
        {
            "owner_kind": "learning",
            "owner_key": ids[0],
            "model": emb.cache_key,
            "text_sha": hashlib.sha1(b"completely unrelated cooking recipe advice").hexdigest(),
            "vector_json": json.dumps(query_like_vec),
            "created_at": utc_now_iso(),
        },
    )
    store.close()
    results = search_learnings(cfg, "never use mtime for logical time", top_k=2)
    # The poisoned cache row wins -> proves the cache short-circuits encoding.
    assert results[0]["id"] == ids[0]
    assert results[0]["cosine"] > 0.99
