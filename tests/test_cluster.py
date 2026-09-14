"""Tests for cluster.py: embedding-based grouping, dedupe vectors, merge/decline.

All cluster logic here runs against INJECTED deterministic embeddings — a
text -> vector map (unknown text raises KeyError, loudly) — so these tests
never load the real model. Real-model behavior (shape, cache, thresholds
calibration) lives in tests/test_embeddings.py.
"""

from __future__ import annotations

import hashlib
import json
import math

import pytest

from self_improve.cluster import (
    existing_rule_vectors,
    group_learnings,
    is_duplicate,
    jaccard,
    merge_clusters,
    normalize_tokens,
    rejected_vectors,
    split_rule_units,
)
from self_improve.config import Config
from self_improve.store import Store, new_id, utc_now_iso

GROUP_T = Config().cluster_group_cosine  # 0.80
DUP_T = Config().cluster_dup_cosine  # 0.85


def _vec(deg: float) -> list[float]:
    """Unit vector at ``deg`` degrees: cosine(_vec(a), _vec(b)) == cos(a-b)."""
    r = math.radians(deg)
    return [math.cos(r), math.sin(r)]


# Deterministic text -> vector map. cos(10°)≈0.985, cos(30°)≈0.866,
# cos(60°)=0.5, cos(90°)=0.
_EMBED_MAP = {
    "never derive time from mtime": _vec(0),
    "never derive logical time from file mtime": _vec(10),
    "assert the reported model matches the requested class": _vec(90),
    "verify model identity every call": _vec(0),
    "verify model identity in the envelope": _vec(30),
    "verify model identity in telemetry always": _vec(60),
    "run pytest before every commit": _vec(120),
    "solo rule": _vec(45),
    "rule variant one": _vec(0),
    "rule variant two": _vec(10),
    "variant a": _vec(0),
    "variant b": _vec(10),
}


def fake_embed(texts: list[str]) -> list[list[float]]:
    return [_EMBED_MAP[t] for t in texts]  # KeyError = unmapped text, fail loud


def learning(rule_text: str, **kw) -> dict:
    base = {
        "id": new_id(),
        "rule_text": rule_text,
        "why": "because",
        "title": "",
        "category": "",
        "scope": "project",
        "evidence_count": 1,
        "project_count": 1,
        "projects_json": "[]",
        "first_seen": "2026-08-01T00:00:00Z",
    }
    base.update(kw)
    return base


class FakeEmbedder:
    """Records cached_vector calls; returns a deterministic per-text vector."""

    def __init__(self):
        self.calls: list[tuple[str, str, str]] = []

    def cached_vector(self, owner_kind: str, owner_key: str, text: str) -> list[float]:
        self.calls.append((owner_kind, owner_key, text))
        return _EMBED_MAP.get(text, [float(len(text)), 1.0])


# ---------------------------------------------------------------- grouping

def test_near_identical_rules_group_together():
    a = learning("never derive time from mtime")
    b = learning("never derive logical time from file mtime")
    c = learning("assert the reported model matches the requested class")
    groups = group_learnings([a, b, c], fake_embed, GROUP_T)
    assert sorted(len(g) for g in groups) == [1, 2]
    pair = next(g for g in groups if len(g) == 2)
    assert {g["id"] for g in pair} == {a["id"], b["id"]}


def test_unrelated_rules_stay_separate():
    groups = group_learnings(
        [
            learning("never derive time from mtime"),
            learning("run pytest before every commit"),
        ],
        fake_embed,
        GROUP_T,
    )
    assert len(groups) == 2


def test_transitive_grouping_via_union_find():
    # a~b and b~c clear the threshold (cos30°≈0.866 >= 0.80) but a~c does not
    # (cos60°=0.5): union-find still puts all three in one group.
    a = learning("verify model identity every call")
    b = learning("verify model identity in the envelope")
    c = learning("verify model identity in telemetry always")
    groups = group_learnings([a, b, c], fake_embed, GROUP_T)
    assert len(groups) == 1 and len(groups[0]) == 3


def test_empty_input():
    assert group_learnings([], fake_embed, GROUP_T) == []


def test_embed_fn_count_mismatch_raises():
    with pytest.raises(ValueError, match="1 vectors for 2 learnings"):
        group_learnings(
            [learning("solo rule"), learning("variant a")],
            lambda texts: [[1.0, 0.0]],
            GROUP_T,
        )


# ---------------------------------------------------------------- rule units

def test_split_rule_units_bullets_headings_continuations():
    content = (
        "# Heading\n\n"
        "@AGENTS.md\n"
        "- **Fail loud.** Missing config raises\n"
        "  rather than defaulting.\n"
        "- Second rule here\n\n"
        "A bare paragraph line.\n"
    )
    units = split_rule_units(content)
    assert units == [
        "**Fail loud.** Missing config raises rather than defaulting.",
        "Second rule here",
        "A bare paragraph line.",
    ]


# ---------------------------------------------------------------- existing/rejected vectors

def test_existing_rule_vectors_units_keys_and_owner_kind(tmp_path):
    target = tmp_path / "CLAUDE.md"
    target.write_text(
        "# Rules\n- Never point tests at the live global CLAUDE.md file\n"
    )
    emb = FakeEmbedder()
    vecs = existing_rule_vectors(Config(), [str(target)], emb)
    assert len(vecs) == 1
    unit, vector = vecs[0]
    assert unit == "Never point tests at the live global CLAUDE.md file"
    assert vector == [float(len(unit)), 1.0]
    expected_key = hashlib.sha1(f"{target}\n{unit}".encode("utf-8")).hexdigest()
    assert emb.calls == [("rule_unit", expected_key, unit)]


def test_missing_file_skipped_but_unreadable_raises(tmp_path):
    emb = FakeEmbedder()
    assert existing_rule_vectors(Config(), [str(tmp_path / "absent.md")], emb) == []
    assert emb.calls == []
    bad = tmp_path / "bad.md"
    bad.write_bytes(b"\xff\xfe invalid utf8 \xff")
    with pytest.raises(UnicodeDecodeError):
        existing_rule_vectors(Config(), [str(bad)], emb)


def test_tiny_units_not_vectorized(tmp_path):
    target = tmp_path / "AGENTS.md"
    target.write_text("- ok\n- run tests\n")
    emb = FakeEmbedder()
    assert existing_rule_vectors(Config(), [str(target)], emb) == []
    assert emb.calls == []  # tiny units never reach the embedder


def test_rejected_vectors_from_store(tmp_path):
    store = Store(tmp_path / "s.db")
    now = utc_now_iso()
    rejected_id = new_id()
    store.insert(
        "learnings",
        {
            "id": rejected_id,
            "rule_text": "Never write files outside the repository sandbox area",
            "status": "rejected",
            "created_at": now,
        },
    )
    applied_id = new_id()
    store.insert(
        "learnings",
        {
            "id": applied_id,
            "rule_text": "Some applied rule with enough tokens",
            "status": "applied",
            "created_at": now,
        },
    )
    store.insert(
        "proposals",
        {
            "id": new_id(),
            "learning_id": applied_id,
            "target_path": "/x",
            "target_kind": "global_claude_md",
            "action": "add",
            "status": "rejected_user",
            "created_at": now,
        },
    )
    emb = FakeEmbedder()
    vecs = rejected_vectors(store, emb)
    assert len(vecs) == 2
    assert {t for t, _ in vecs} == {
        "Never write files outside the repository sandbox area",
        "Some applied rule with enough tokens",
    }
    # owner_kind 'learning', owner_key = the learning id
    assert {(kind, key) for kind, key, _ in emb.calls} == {
        ("learning", rejected_id),
        ("learning", applied_id),
    }


# ---------------------------------------------------------------- is_duplicate

def test_is_duplicate_returns_matched_text():
    vectors = [
        ("existing rule about mtime", _vec(0)),
        ("existing rule about redaction", _vec(90)),
    ]
    embed = lambda texts: [_vec(10) for _ in texts]  # cos10°≈0.985 vs entry 1
    dup, matched = is_duplicate("candidate rule", vectors, embed, DUP_T)
    assert dup is True
    assert matched == "existing rule about mtime"


def test_is_duplicate_below_threshold_returns_false_and_empty():
    vectors = [("existing rule about mtime", _vec(0))]
    embed = lambda texts: [_vec(45) for _ in texts]  # cos45°≈0.707 < 0.85
    assert is_duplicate("candidate rule", vectors, embed, DUP_T) == (False, "")


def test_is_duplicate_reports_best_match_not_first():
    vectors = [
        ("weaker match", _vec(20)),  # cos20°≈0.940
        ("stronger match", _vec(5)),  # cos5°≈0.996
    ]
    embed = lambda texts: [_vec(0) for _ in texts]
    dup, matched = is_duplicate("candidate rule", vectors, embed, DUP_T)
    assert dup is True and matched == "stronger match"


def test_is_duplicate_empty_vector_list():
    boom = lambda texts: (_ for _ in ()).throw(AssertionError("must not embed"))
    assert is_duplicate("anything", [], boom, DUP_T) == (False, "")


# ---------------------------------------------------------------- merging

def test_singleton_passes_through_without_llm_call():
    calls = []
    merged, stats = merge_clusters([[learning("solo rule")]], lambda items: calls.append(items))
    assert calls == []
    assert stats["singletons"] == 1 and stats["merge_attempted"] == 0
    assert merged[0]["absorbed_ids"] == []


def test_merge_rolls_up_evidence_and_absorbs_ids():
    a = learning(
        "rule variant one",
        evidence_count=3,
        projects_json=json.dumps(["/p/one", "/p/two"]),
    )
    b = learning("rule variant two", evidence_count=1, projects_json=json.dumps(["/p/three"]))
    merged, stats = merge_clusters(
        [[a, b]],
        lambda items: {
            "decision": "merge",
            "generalized_rule": "canonical rule",
            "why": "combined why",
            "title": "T",
            "category": "process",
            "scope_guess": "global",
        },
    )
    assert stats["merge_succeeded"] == 1 and stats["merge_declined"] == 0
    (m,) = merged
    assert m["id"] == a["id"]  # representative = highest evidence
    assert m["rule_text"] == "canonical rule"
    assert m["evidence_count"] == 4
    assert m["project_count"] == 3
    assert m["absorbed_ids"] == [b["id"]]


def test_keep_separate_passes_items_through_individually():
    a, b = learning("variant a"), learning("variant b")
    merged, stats = merge_clusters(
        [[a, b]],
        lambda items: {
            "decision": "keep_separate",
            "reason": "different lessons that merely share vocabulary",
        },
    )
    assert stats["merge_declined"] == 1
    assert stats["merge_failed"] == 0 and stats["merge_succeeded"] == 0
    assert {m["id"] for m in merged} == {a["id"], b["id"]}
    assert all(m["absorbed_ids"] == [] for m in merged)
    # rule_text untouched — a decline must not alter the learnings
    assert {m["rule_text"] for m in merged} == {"variant a", "variant b"}


def test_failed_merge_keeps_items_unmerged_never_guesses():
    a, b = learning("variant a"), learning("variant b")
    bad_responses = [
        None,
        {},
        # old contract without a decision field is now invalid: strict schema
        {"generalized_rule": "x", "why": "y"},
        {"decision": "merge", "generalized_rule": ""},
        {"decision": "merge", "generalized_rule": "x"},  # missing why
        {"decision": "keep_separate"},  # missing reason
        {"decision": "keep_separate", "reason": "   "},  # blank reason
        {"decision": "bogus", "generalized_rule": "x", "why": "y"},
    ]
    for bad in bad_responses:
        merged, stats = merge_clusters([[a, b]], lambda items, bad=bad: bad)
        assert stats["merge_failed"] == 1, bad
        assert stats["merge_declined"] == 0 and stats["merge_succeeded"] == 0, bad
        assert {m["id"] for m in merged} == {a["id"], b["id"]}, bad


# ------------------------------------------------- self-eval judge survivors

def test_jaccard_and_normalize_tokens_still_exported_for_self_eval_judge():
    # cli.py's deterministic self-eval judge is the sole remaining consumer.
    assert jaccard(frozenset(), frozenset({"a"})) == 0.0
    t = normalize_tokens("Verify the model identity")
    assert jaccard(t, t) == 1.0


def test_existing_rule_vectors_dedupes_clone_copies(tmp_path):
    """Repeated instruction text across working copies contributes one vector."""
    import dataclasses

    from self_improve.cluster import existing_rule_vectors
    from self_improve.config import Config
    from self_improve.embeddings import Embedder
    from self_improve.store import Store

    rule = "- **Never json.load a wrapped CLI's stdout** — read the raw bytes first.\n"
    other = "- **Always run the linter before pushing** — CI is slower than you.\n"
    paths = []
    for i in range(6):
        d = tmp_path / f"clone-{i}"
        d.mkdir()
        for name in ("AGENTS.md", "CLAUDE.md"):
            (d / name).write_text(rule + other)
            paths.append(str(d / name))

    cfg = dataclasses.replace(Config(), state_dir=str(tmp_path / "state"))
    store = Store(cfg.state_path("state.db"))
    vecs = existing_rule_vectors(cfg, paths, Embedder(cfg, store))

    texts = [t for t, _ in vecs]
    assert len(texts) == len(set(texts)), (
        f"{len(texts)} vectors for {len(set(texts))} distinct rules — 12 files "
        "of clone copies were all embedded separately"
    )
    assert len(texts) == 2, f"expected the 2 distinct rules, got {len(texts)}"


def test_a_misaligned_embedder_is_caught_before_it_clusters_the_wrong_rules():
    """`embed_fn` returning the wrong number of vectors would silently pair
    each learning with someone else's vector, and the clustering would look
    perfectly normal while merging unrelated rules. The guard existed and had
    never been reached."""
    learnings = [
        {"id": "a", "rule_text": "**One.**"},
        {"id": "b", "rule_text": "**Two.**"},
    ]
    with pytest.raises(ValueError, match="returned 1 vectors for 2 learnings"):
        group_learnings(learnings, lambda texts: [[0.1, 0.2]], threshold=0.9)


def test_corrupt_projects_json_names_the_learning_not_a_character_offset():
    """The same column routing.py guards, read by the other consumer. A bare
    `json.loads` leaves a JSONDecodeError with a character offset and no idea
    which learning it came from."""
    from self_improve.cluster import _projects_of

    with pytest.raises(ValueError, match="not valid JSON for learning 'L9'"):
        _projects_of({"id": "L9", "projects_json": "{not json"})

    # The working path is untouched, including the already-a-list shortcut.
    assert _projects_of({"id": "L", "projects_json": '["/a"]'}) == ["/a"]
    assert _projects_of({"id": "L", "projects_json": ["/b"]}) == ["/b"]
