"""Learning clustering + dedupe against existing/rejected rules, via embeddings.

Pure-ish module: similarity comes from injected callables (``embed_fn`` /
``Embedder``), the LLM merge step is an injected callable, and store access is
read-only queries plus the Embedder's own vector cache.

Similarity is cosine over local static embeddings (model2vec; see
``embeddings.py`` — no fallback metric: an unloadable model fails the run).
Two thresholds come from Config, deliberately different:

- ``cfg.cluster_group_cosine`` (default 0.80): two *candidate* learnings this
  similar are the same lesson observed twice -> merge cluster.
- ``cfg.cluster_dup_cosine`` (default 0.85): a candidate this similar to an
  *existing or previously-rejected* rule unit is a duplicate -> dropped before
  proposing, recording WHICH unit it duplicated. Higher bar because a false
  duplicate silently suppresses a real learning.

Measured calibration (potion-base-8M, 2026-08-16, asserted in
tests/test_embeddings.py): near-identical phrasings score ~0.93-0.97, genuine
paraphrases of the same lesson ~0.62-0.77, distinct-topic rules ~0.04-0.37.
The defaults therefore group only near-identical phrasings; lower
``cluster_group_cosine`` toward ~0.60 if paraphrase-level grouping is wanted.

``normalize_tokens`` / ``jaccard`` survive at the bottom of this module for
one external consumer (the deterministic self-eval judge); clustering itself
no longer uses Jaccard.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Callable

from .config import Config
from .embeddings import Embedder, cosine
from .store import Store

# Injected embedding callable: list of texts -> list of vectors (same order).
EmbedFn = Callable[[list[str]], list[list[float]]]


def group_learnings(
    learnings: list[dict],
    embed_fn: EmbedFn,
    threshold: float,
) -> list[list[dict]]:
    """Union-find grouping of candidate learnings at cosine >= ``threshold``.

    All ``rule_text`` values are embedded in one ``embed_fn`` batch, then
    compared pairwise. O(n^2) — fine at per-run candidate counts (dozens);
    revisit before feeding thousands.
    """
    if not learnings:
        return []
    vectors = embed_fn([l["rule_text"] for l in learnings])
    if len(vectors) != len(learnings):
        raise ValueError(
            f"embed_fn returned {len(vectors)} vectors for {len(learnings)} learnings"
        )
    n = len(learnings)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(n):
        for j in range(i + 1, n):
            if cosine(vectors[i], vectors[j]) >= threshold:
                parent[find(i)] = find(j)

    groups: dict[int, list[dict]] = {}
    for i, learning in enumerate(learnings):
        groups.setdefault(find(i), []).append(learning)
    return list(groups.values())


def split_rule_units(content: str) -> list[str]:
    """Split an instruction file into rule-sized units for fingerprinting.

    Bullets (- / * / numbered) are one unit each (continuation lines joined);
    non-bullet paragraph lines are one unit per line. Headings, blank lines,
    and import/pointer lines (@path) are skipped.
    """
    units: list[str] = []
    current: list[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("@"):
            if current:
                units.append(" ".join(current))
                current = []
            continue
        if re.match(r"^([-*]|\d+\.)\s+", stripped):
            if current:
                units.append(" ".join(current))
            current = [re.sub(r"^([-*]|\d+\.)\s+", "", stripped)]
        elif current:
            current.append(stripped)
        else:
            units.append(stripped)
    if current:
        units.append(" ".join(current))
    return units


def existing_rule_vectors(
    cfg: Config,
    paths: list[str],
    embedder: "Embedder",
) -> list[tuple[str, list[float]]]:
    """(unit_text, vector) for every rule unit across the given target files.

    Units under 3 content tokens are skipped (one/two-token units are noise
    that near-matches everything). Vectors come from the embeddings cache with
    owner_kind ``'rule_unit'`` and owner_key sha1 of ``"<path>\\n<unit text>"``
    (expanded absolute path; \\n separator disambiguates path/unit boundary).

    A listed file that does not exist is skipped; a file that exists but
    cannot be read raises (fail loud).
    """
    # Deduplicate unit text across files and working copies, as search.py and
    # contradictions.py do, so repeated copies add no comparison weight.
    #
    # The cache key stays (first path + unit) rather than becoming text-only,
    # so existing cached vectors still hit and rebuild.py's "rule-unit vectors
    # are keyed by file+text and stay valid" remains true.
    out: list[tuple[str, list[float]]] = []
    seen: set[str] = set()
    for p in paths:
        path = Path(p).expanduser()
        if not path.exists():
            continue
        content = path.read_text(encoding="utf-8", errors="strict")
        for unit in split_rule_units(content):
            if len(normalize_tokens(unit)) < 3:
                continue
            if unit in seen:
                continue
            seen.add(unit)
            key = hashlib.sha1(f"{path}\n{unit}".encode("utf-8")).hexdigest()
            out.append((unit, embedder.cached_vector("rule_unit", key, unit)))
    return out


def rejected_vectors(
    store: Store,
    embedder: "Embedder",
) -> list[tuple[str, list[float]]]:
    """(rule_text, vector) for every rule the user rejected — never re-propose.

    Covers learnings marked ``rejected`` and learnings whose proposal the user
    rejected (``rejected_user``). Vectors come from the embeddings cache with
    owner_kind ``'learning'`` and owner_key the learning id.
    """
    from .rejections import context, lesson_rejected
    ctx = context(store)
    rows = [r for r in ctx['learnings'].values() if lesson_rejected(store,r['id'],ctx=ctx)]
    out = [(r['rule_text'],embedder.cached_vector('learning',r['id'],r['rule_text'])) for r in rows]
    seen = {text for text,_ in out}
    # A later producer must not erase the text the person actually rejected.
    for d in ctx['decisions']:
        if d['decision_scope']=='lesson' and d['rule_text'] not in seen:
            out.append((d['rule_text'],embedder.cached_vector('rejected_revision',d['revision_id'],d['rule_text'])))
            seen.add(d['rule_text'])
    return out


def is_duplicate(
    rule_text: str,
    vectors: list[tuple[str, list[float]]],
    embed_fn: EmbedFn,
    threshold: float,
) -> tuple[bool, str]:
    """Is ``rule_text`` a near-duplicate of any (text, vector) entry?

    Returns ``(True, matched_text)`` for the highest-cosine entry at or above
    ``threshold`` — the caller records WHAT was duplicated, not just that a
    duplicate happened — or ``(False, "")`` when nothing reaches it.
    """
    if not vectors:
        return (False, "")
    (vec,) = embed_fn([rule_text])
    best_score = -1.0
    best_text = ""
    for text, v in vectors:
        score = cosine(vec, v)
        if score >= threshold and score > best_score:
            best_score = score
            best_text = text
    return (best_score >= threshold, best_text)


def merge_clusters(
    clusters: list[list[dict]],
    llm_merge: Callable[[list[dict]], dict | None],
) -> tuple[list[dict], dict]:
    """Collapse each cluster to one canonical learning — or decline to.

    Singleton clusters pass through without an LLM call. Multi-item clusters
    call ``llm_merge(items)``, which must return a strict decision dict:

    - ``{"decision": "merge", "generalized_rule": ..., "why": ..., "title":
      ..., "category": ..., "scope_guess": ...}`` -> the cluster collapses to
      one canonical learning (evidence rolled up, members absorbed).
    - ``{"decision": "keep_separate", "reason": ...}`` -> the model judged the
      rules to be genuinely different lessons that merely share vocabulary;
      every item passes through individually, counted ``merge_declined``.
      Merging distinct lessons destroys evidence, so declining is a valid
      outcome, not an error.
    - Anything else — None, a missing/unknown ``decision``, empty required
      fields — is a failed merge (``merge_failed``): the cluster's items pass
      through UNMERGED. Dropping them would silently lose evidence, and
      guessing a merge is forbidden.

    Returns (canonical_learnings, stats). Each canonical learning reuses the
    representative's id (highest evidence_count, ties -> earliest first_seen)
    and carries ``absorbed_ids`` listing cluster members it superseded, so the
    caller can mark those rows superseded in the store.
    """
    stats = {
        "singletons": 0,
        "merge_attempted": 0,
        "merge_succeeded": 0,
        "merge_declined": 0,
        "merge_failed": 0,
    }
    out: list[dict] = []
    for cluster in clusters:
        if len(cluster) == 1:
            stats["singletons"] += 1
            out.append({**cluster[0], "absorbed_ids": []})
            continue
        stats["merge_attempted"] += 1
        merged = llm_merge(cluster)
        decision = merged.get("decision") if isinstance(merged, dict) else None
        if (
            decision == "keep_separate"
            and isinstance(merged.get("reason"), str)
            and merged["reason"].strip()
        ):
            stats["merge_declined"] += 1
            out.extend({**l, "absorbed_ids": []} for l in cluster)
            continue
        if (
            decision != "merge"
            or not isinstance(merged.get("generalized_rule"), str)
            or not merged["generalized_rule"].strip()
            or not isinstance(merged.get("why"), str)
        ):
            stats["merge_failed"] += 1
            out.extend({**l, "absorbed_ids": []} for l in cluster)
            continue
        stats["merge_succeeded"] += 1
        rep = sorted(
            cluster,
            key=lambda l: (-int(l.get("evidence_count") or 0), l.get("first_seen") or ""),
        )[0]
        projects: set[str] = set()
        evidence = 0
        for l in cluster:
            evidence += int(l.get("evidence_count") or 0) or 1
            for p in _projects_of(l):
                projects.add(p)
        out.append(
            {
                **rep,
                "rule_text": merged["generalized_rule"].strip(),
                "why": merged["why"].strip(),
                "title": str(merged.get("title") or rep.get("title") or ""),
                "category": str(merged.get("category") or rep.get("category") or ""),
                "scope": str(merged.get("scope_guess") or rep.get("scope") or ""),
                "evidence_count": evidence,
                "project_count": len(projects),
                "projects": sorted(projects),
                "absorbed_ids": [l["id"] for l in cluster if l["id"] != rep["id"]],
            }
        )
    return out, stats


def _projects_of(learning: dict) -> list[str]:
    import json

    raw = learning.get("projects_json") or "[]"
    if isinstance(raw, list):
        return raw
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError) as exc:
        # Our own DB invariant: projects_json is a strict-JSON TEXT column.
        # A bare json.loads leaves the operator a JSONDecodeError with a
        # character offset and no idea which learning or column it came from.
        # routing.py raises a named error on the same column; this is the
        # other reader of it.
        raise ValueError(
            f"projects_json is not valid JSON for learning "
            f"{learning.get('id', '<no id>')!r}: {exc}"
        ) from exc
    if not isinstance(parsed, list):
        raise ValueError(f"projects_json is not a list: {raw!r}")
    return parsed


# ---------------------------------------------------------------------------
# Token-overlap similarity — KEPT ONLY for the deterministic self-eval judge
# (`selfimprove self-eval` in cli.py uses jaccard + normalize_tokens as a
# transparent stand-in for an LLM judge). Clustering/dedupe above use cosine
# embeddings and must not regress to these.
# ---------------------------------------------------------------------------

_STOPWORDS = frozenset(
    "a an and are as at be but by for from has have if in into is it its no not "
    "of on or so than that the their then there these this to was were what when "
    "which will with would you your never always must should".split()
)

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_.\-]*")


def normalize_tokens(text: str) -> frozenset[str]:
    """Lowercased content-token set (self-eval judge + tiny-unit filter)."""
    return frozenset(
        t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS
    )


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)
