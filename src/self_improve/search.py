"""Embedding search over the learnings table — the miner agent's dedup tool.

Exposed via ``selfimprove search-learnings`` (see cli.py), which the sandboxed
agentic miner is allowed to call. The DB is opened READ-ONLY here: this module
is reachable from inside an autonomous agent's Bash allowlist, and that agent
must never be able to mutate state through it.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from pathlib import Path

from .config import Config

logger = logging.getLogger(__name__)


class SearchError(Exception):
    pass


def open_store_readonly(cfg: Config) -> sqlite3.Connection:
    """Read-only SQLite connection (URI mode=ro; raises if the DB is absent)."""
    db_path = cfg.state_path("state.db")
    if not db_path.exists():
        raise SearchError(f"state DB does not exist: {db_path}")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def search_learnings(
    cfg: Config,
    query: str,
    top_k: int = 8,
    statuses: tuple[str, ...] = (),
    include_in_force: bool | None = None,
    with_meta: bool = False,
):
    """Top-``top_k`` learnings by cosine similarity of ``rule_text`` to ``query``.

    Two corpora share one ranking and one ``top_k``: mined **learnings**
    (``kind='learning'``) and the rule units of the instruction files currently
    **in force** (``kind='rule_unit'``, carrying ``source_file``). The second
    is what lets the miner answer "this lesson is already written, and here is
    the line" — without it the tool can only find other mined learnings.

    Searches every learning regardless of status unless ``statuses`` narrows
    it (the agent needs to see applied, proposed, candidate AND rejected rows —
    a rejected near-match means "the user already said no to this"). Passing
    ``statuses`` also excludes in-force rules by default, since they carry no
    mining status; ``include_in_force`` overrides that either way.

    Embeds the query and any un-cached learnings locally (model2vec). The
    read-only connection cannot write the embeddings cache, so vectors for
    rows not yet cached are computed in memory here; the nightly pipeline is
    what persists them.
    """
    from .embeddings import Embedder, parse_cached_vector

    if not query.strip():
        raise SearchError("query must be non-empty")
    conn = open_store_readonly(cfg)
    try:
        sql = (
            "SELECT id, status, rule_text, why, category, scope, evidence_count, "
            "project_count, duplicate_of FROM learnings"
        )
        params: tuple = ()
        if statuses:
            sql += f" WHERE status IN ({', '.join('?' for _ in statuses)})"
            params = statuses
        rows = [dict(r) for r in conn.execute(sql, params)]
        text_hashes = {r["id"]: hashlib.sha1(r["rule_text"].encode()).hexdigest() for r in rows}
        embedder = Embedder(cfg, store=None)  # encode-only; cannot write the cache
        model_key = embedder.cache_key
        project_paths = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT project_path FROM sessions WHERE project_path != ''"
            )
        ]
        cached: dict[str, list[float]] = {}
        for r in conn.execute(
            "SELECT owner_key, text_sha, vector_json FROM embeddings "
            "WHERE owner_kind = 'learning' AND model = ?",
            (model_key,),
        ):
            if text_hashes.get(r["owner_key"]) == r["text_sha"]:
                cached[r["owner_key"]] = parse_cached_vector(
                    r["vector_json"], ("learning", r["owner_key"], model_key))
    finally:
        conn.close()

    want_in_force = (not statuses) if include_in_force is None else include_in_force
    units = list(_in_force_rule_units(cfg, project_paths)) if want_in_force else []
    return _rank(embedder, rows, cached, units, query, top_k, with_meta)


def search_corpus(cfg, corpus, query, top_k=8, statuses=(), include_in_force=None, with_meta=False):
    """Search a frozen corpus without opening any database or instruction file."""
    from .embeddings import Embedder
    if not isinstance(query,str) or not query.strip():
        raise SearchError("query must be non-empty")
    if type(top_k) is not int or top_k < 1:
        raise SearchError("top_k must be positive")
    if not isinstance(corpus,dict) or set(corpus)!={'learnings','rules'} or not all(isinstance(corpus[k],list) for k in corpus):
        raise SearchError("frozen corpus must contain learning and rule lists")
    rows=[r for r in corpus['learnings'] if not statuses or r['status'] in statuses]
    want_in_force=(not statuses) if include_in_force is None else include_in_force
    units=[(r['text'],r['path']) for r in corpus['rules']] if want_in_force else []
    return _rank(Embedder(cfg,store=None), rows, {}, units, query, top_k, with_meta)


def _rank(embedder, rows, cached, units, query, top_k, with_meta):
    from .embeddings import cosine
    (query_vec,) = embedder.encode([query])

    uncached = [r for r in rows if r["id"] not in cached]
    if uncached:
        vectors = embedder.encode([r["rule_text"] for r in uncached])
        for r, v in zip(uncached, vectors):
            cached[r["id"]] = v

    scored = [
        {**r, "kind": "learning", "cosine": round(cosine(query_vec, cached[r["id"]]), 4)}
        for r in rows
    ]

    # Include rules already present in instruction files in the shared ranking.
    # They have no mining status, so a status filter defaults to learnings only.
    # Callers can override that choice with include_in_force.
    first_seen: dict[str, Path] = {}
    copies: dict[str, int] = {}
    if units:
        # Deduplicate text before ranking. Identical rules in multiple clones
        # must not occupy every result slot. Preserve the copy count as context.
        # Redact instruction-file text because these results enter an agent's
        # context and can contain secrets even when no transcript was read.
        from .redact import redact_text

        for unit, path in units:
            unit = redact_text(unit)
            if unit not in first_seen:
                first_seen[unit] = path
            copies[unit] = copies.get(unit, 0) + 1

    # Embed the distinct rule units in one batch.
    unit_texts = list(first_seen)
    unit_vecs = embedder.encode(unit_texts) if unit_texts else []
    for unit, vec in zip(unit_texts, unit_vecs):
        path = first_seen[unit]
        scored.append(
            {
                "id": "rule:" + hashlib.sha1(unit.encode()).hexdigest()[:16],
                "kind": "rule_unit",
                # 'in_force' is deliberately not one of the learnings statuses:
                # this row is a line living in a file, not a mined candidate.
                "status": "in_force",
                "source_file": str(path),
                "in_force_copies": copies[unit],
                "rule_text": unit,
                "why": "",
                "category": "",
                "scope": "",
                "evidence_count": 0,
                "project_count": 0,
                "duplicate_of": "",
                "cosine": round(cosine(query_vec, vec), 4),
            }
        )

    # One shared top_k across both corpora: the agent sees a fixed number of
    # rows total, so a rule unit can displace a learning and vice versa.
    scored.sort(key=lambda r: -r["cosine"])
    top = scored[:top_k]
    if not with_meta:
        return top
    # Report what the cap cut. The caller sees 8 rows out of hundreds ranked,
    # and CROWD-OUT is the known dedup failure — near-duplicates filling every
    # slot so the rule that would have stopped them never appears. Without
    # this the agent cannot tell "nothing else matched" from "the slots were
    # full", and those need opposite responses.
    return {
        "results": top,
        "meta": {
            "ranked": len(scored),
            "returned": len(top),
            "cut": max(0, len(scored) - len(top)),
            "corpus": {
                "learnings": sum(1 for r in scored if r["kind"] == "learning"),
                "in_force_rules": sum(1 for r in scored if r["kind"] == "rule_unit"),
            },
            "returned_kinds": {
                "learnings": sum(1 for r in top if r["kind"] == "learning"),
                "in_force_rules": sum(1 for r in top if r["kind"] == "rule_unit"),
            },
            "lowest_returned_cosine": top[-1]["cosine"] if top else None,
            "highest_cut_cosine": scored[top_k]["cosine"] if len(scored) > top_k else None,
        },
    }


def _in_force_rule_units(cfg: Config, project_paths: list[str], *, unreadable=None):
    """(unit_text, path) for every rule unit in every in-force instruction file.

    Same unit splitting and the same <3-token noise filter as
    ``cluster.existing_rule_vectors``, so the dedup the miner does and the
    dedup the pipeline does are looking at the same units.

    A file that does not exist is skipped (projects come and go); a file that
    exists but cannot be read is skipped WITH the reason surfaced to the
    caller's stderr rather than raising — this runs inside an autonomous
    agent's tool call, where a hard failure would abort a mine that could
    otherwise have succeeded on the learnings corpus alone.
    """
    from .cluster import normalize_tokens, split_rule_units
    from .routing import instruction_target_paths

    for p in instruction_target_paths(project_paths, cfg):
        path = Path(p).expanduser()
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            # Logging, not print: every other module here uses it, and an
            # unconfigured logger still reaches stderr. What matters either way
            # is that this never touches STDOUT — the sandboxed agent parses
            # that as JSON, and a warning mixed into it is precisely the
            # "wrapped CLI corrupts piped output" failure this repo keeps
            # learning about.
            if unreadable is not None:
                unreadable.append({"path":str(path),"cause":type(exc).__name__,"detail":str(exc)})
            logger.warning("unreadable instruction file %s: %s", path, exc)
            continue
        for unit in split_rule_units(content):
            if len(normalize_tokens(unit)) < 3:
                continue
            yield unit, path
