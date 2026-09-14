"""Frozen retrieval evaluation for semantic-search duplicate detection.

Three arms score the selected corpus and relevance judgments:

1. ``semantic`` calls the production ``search_learnings`` ranker against a
   temporary SQLite store seeded with the frozen documents and embeddings.
2. ``keyword_bm25_proxy`` scores lexical overlap over ``rule_text + why``.
   It is a deterministic proxy, not a measurement of an agent's grep choices.
3. ``oracle_union`` reports the recall available from the two result sets.
   It is an evaluation ceiling, not a deployed fusion policy.

The corpus can contain learnings and instruction-file rule units. Both become
rows in the temporary store, so the ranker does not read live instruction files.
Results measure this frozen retrieval task, not the complete mining pipeline.
Historical private scores require their matching code, dataset, and model inputs.

Missing documents, changed corpus bytes, incompatible manifests, and empty
queries fail explicitly. Private benchmarks never substitute the invented demo.
See ``docs/DATA_BOUNDARY.md`` and ``docs/EMBEDDING_MODELS.md`` for reproduction.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Callable, Iterable, Sequence

from ..config import Config
from ..resources import bundled_path

# Low-level parser defaults use invented fixtures. run() requires a dataset.
EVAL_DIR = bundled_path("evals", "synthetic", "retrieval")
CORPUS_PATH = EVAL_DIR / "corpus.jsonl"
QRELS_PATH = EVAL_DIR / "qrels.yaml"

#: k values reported. 8 is the headline: ``search_learnings`` returns 8 rows,
#: so a true duplicate ranked 9th is invisible to the miner at any score.
K_VALUES: tuple[int, ...] = (1, 3, 5, 8)
HEADLINE_K = 8

#: Relevance labels. ``borderline`` pairs were read and judged genuinely
#: ambiguous; they are excluded from both numerator and denominator by
#: default, and the sweep/report show the sensitivity both ways.
RELEVANT = "relevant"
NOT_RELEVANT = "not_relevant"
BORDERLINE = "borderline"
_LABELS = {RELEVANT, NOT_RELEVANT, BORDERLINE}

PROVENANCES = {"miner_flagged", "adjudicated_family", "hard_negative"}


class RetrievalEvalError(Exception):
    """Corpus/qrel integrity or configuration failure. Never swallowed."""


# ---------------------------------------------------------------------------
# Corpus
# ---------------------------------------------------------------------------


def normalize_for_id(text: str) -> str:
    """Whitespace-normalised text used as the content-address preimage.

    Deliberately conservative: runs of whitespace collapse to one space and
    the ends are stripped, but case, punctuation and markdown are preserved.
    Reflowing a rule keeps its identity; rewording it does not — which is the
    correct behaviour, because a reworded rule is a different document that
    needs re-adjudicating.
    """
    return re.sub(r"\s+", " ", text).strip()


def content_id(text: str) -> str:
    """Content address of a document: ``doc:`` + sha256 of the normalised text.

    THIS, NOT THE DATABASE UUID, is what the qrel file keys on. ``learnings.id``
    is regenerated whenever the state DB is rebuilt from scratch (re-scan,
    re-mine), so a UUID-keyed qrel file would silently stop matching anything
    the moment that happens — and a recall number computed over a
    silently-shrunken qrel set goes UP as data disappears. Content addressing
    makes a rebuild either match or fail loud.
    """
    return "doc:" + hashlib.sha256(normalize_for_id(text).encode("utf-8")).hexdigest()[:16]


@dataclasses.dataclass(frozen=True)
class Doc:
    """One retrievable document.

    ``kind`` is ``learning`` (a row of the learnings table) or ``rule_unit``
    (a bullet of an in-force instruction file). ``text`` is what the semantic
    arm embeds — for learnings that is ``rule_text``, matching search.py.

    ``doc_id`` is a content address (see :func:`content_id`). ``observed_as``
    records the ``learnings.id`` this text was seen under at snapshot time and
    is NON-AUTHORITATIVE: it is audit provenance only, may be stale, and
    nothing in the eval resolves through it.
    """

    doc_id: str
    kind: str
    text: str
    why: str = ""
    category: str = ""
    status: str = ""
    source_ref: str = ""
    observed_as: str = ""

    @property
    def keyword_text(self) -> str:
        """Text the keyword proxy indexes: what ``learnings.jsonl`` exposes."""
        return f"{self.text} {self.why}".strip()


def snapshot_corpus(
    cfg: Config,
    instruction_files: Sequence[str],
    out_path: Path,
) -> dict:
    """Freeze the live corpus to ``out_path`` (JSONL). READ-ONLY on the DB.

    Line 1 is a header record (``_meta``) carrying the row counts and a
    sha256 of the document payload; :func:`load_corpus` re-derives that hash
    and raises on mismatch, so a hand-edited corpus cannot drift away from the
    adjudication it was judged under.

    The destination must be new and outside every Git checkout. Redaction
    removes recognized secrets; the exported text still remains private.
    """
    from ..cluster import normalize_tokens, split_rule_units
    from ..redact import redact_text
    from ..search import open_store_readonly

    from ..data_boundary import DataBoundaryError, private_destination

    try:
        out_path = private_destination(out_path)
    except DataBoundaryError as exc:
        raise RetrievalEvalError(str(exc)) from exc
    conn = open_store_readonly(cfg)
    try:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT id, status, rule_text, why, category FROM learnings "
                "ORDER BY created_at, id"
            )
        ]
    finally:
        conn.close()

    docs: list[Doc] = []
    redacted = 0
    for r in rows:
        text = redact_text(r["rule_text"])
        why = redact_text(r["why"])
        if text != r["rule_text"] or why != r["why"]:
            redacted += 1
        docs.append(
            Doc(
                doc_id=content_id(text),
                kind="learning",
                text=text,
                why=why,
                category=r["category"],
                status=r["status"],
                source_ref="learnings",
                observed_as=r["id"],
            )
        )

    skipped_short = 0
    for spec in instruction_files:
        path = Path(spec).expanduser()
        if not path.exists():
            raise RetrievalEvalError(f"instruction file does not exist: {path}")
        for unit in split_rule_units(path.read_text(encoding="utf-8")):
            if len(normalize_tokens(unit)) < 3:
                skipped_short += 1
                continue
            clean = redact_text(unit)
            if clean != unit:
                redacted += 1
            docs.append(
                Doc(
                    doc_id=content_id(clean),
                    kind="rule_unit",
                    text=clean,
                    source_ref=spec,
                )
            )

    collisions: list[str] = []
    seen_ids: dict[str, str] = {}
    for d in docs:
        prior = seen_ids.get(d.doc_id)
        if prior is not None and prior != d.text:
            raise RetrievalEvalError(
                f"content-address collision on {d.doc_id}: {prior!r} vs {d.text!r}"
            )
        if prior is not None:
            collisions.append(d.doc_id)
        seen_ids[d.doc_id] = d.text
    if collisions:
        # Same text appearing twice (e.g. a learning whose rule_text is
        # verbatim an instruction-file line). Fail loud: silently deduping
        # would quietly merge two corpus rows the adjudication treated apart.
        raise RetrievalEvalError(
            f"duplicate document text in corpus (content ids {sorted(set(collisions))}) — "
            "two rows share identical normalised text; resolve before snapshotting"
        )

    payload = [dataclasses.asdict(d) for d in docs]
    meta = {
        "_meta": {
            "docs": len(payload),
            "learnings": sum(1 for d in payload if d["kind"] == "learning"),
            "rule_units": sum(1 for d in payload if d["kind"] == "rule_unit"),
            "instruction_files": list(instruction_files),
            "rule_units_skipped_under_3_tokens": skipped_short,
            "docs_redacted": redacted,
            "payload_sha256": _payload_sha(payload),
        }
    }
    import os
    out_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(out_path, "x", encoding="utf-8") as fh:
        os.chmod(out_path, 0o600)
        fh.write(json.dumps(meta, ensure_ascii=False) + "\n")
        for d in payload:
            fh.write(json.dumps(d, ensure_ascii=False) + "\n")
    return meta["_meta"]


def _payload_sha(payload: list[dict]) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def load_corpus(path: Path = CORPUS_PATH) -> tuple[list[Doc], dict]:
    """Load a frozen corpus, verifying its header against its contents."""
    if not path.exists():
        raise RetrievalEvalError(f"corpus snapshot missing: {path}")
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if not lines:
        raise RetrievalEvalError(f"corpus snapshot is empty: {path}")
    # Parse errors must name the corpus path, as the shape checks below do.
    try:
        meta = json.loads(lines[0]).get("_meta")
    except (ValueError, TypeError, AttributeError) as exc:
        raise RetrievalEvalError(
            f"corpus snapshot header is not valid JSON: {path}: {exc}"
        ) from exc
    if meta is None:
        raise RetrievalEvalError(f"corpus snapshot has no _meta header: {path}")
    payload = []
    for n, ln in enumerate(lines[1:], start=2):
        try:
            payload.append(json.loads(ln))
        except (ValueError, TypeError) as exc:
            raise RetrievalEvalError(
                f"corpus snapshot line {n} is not valid JSON: {path}: {exc}"
            ) from exc
    if len(payload) != meta["docs"]:
        raise RetrievalEvalError(
            f"corpus row count mismatch in {path}: header says {meta['docs']}, "
            f"file has {len(payload)}"
        )
    actual = _payload_sha(payload)
    if actual != meta["payload_sha256"]:
        raise RetrievalEvalError(
            f"corpus payload sha256 mismatch in {path}: header "
            f"{meta['payload_sha256']}, computed {actual} — regenerate with "
            "`selfimprove eval-retrieval --refresh-corpus` and re-adjudicate."
        )
    docs = [Doc(**d) for d in payload]
    ids = [d.doc_id for d in docs]
    dupes = [i for i, n in Counter(ids).items() if n > 1]
    if dupes:
        raise RetrievalEvalError(f"duplicate doc ids in {path}: {sorted(dupes)[:5]}")
    return docs, meta


# ---------------------------------------------------------------------------
# Qrels
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Judgment:
    """One adjudicated (query, doc) pair."""

    query_id: str
    doc_id: str
    label: str
    provenance: str
    reason: str
    hard_call: bool = False


@dataclasses.dataclass(frozen=True)
class Qrels:
    judgments: tuple[Judgment, ...]
    notes: str = ""
    #: content_id -> the full verbatim text of every document this file
    #: judges. The qrel file is self-contained: the judged corpus rebuilds
    #: from here with no database at all.
    documents: dict = dataclasses.field(default_factory=dict)

    def query_ids(self) -> list[str]:
        seen: dict[str, None] = {}
        for j in self.judgments:
            seen.setdefault(j.query_id, None)
        return list(seen)

    def for_query(self, query_id: str) -> list[Judgment]:
        return [j for j in self.judgments if j.query_id == query_id]

    def relevant(
        self,
        query_id: str,
        include_borderline: bool = False,
        provenance: str | None = None,
    ) -> set[str]:
        """Docs judged relevant for ``query_id``.

        ``provenance`` narrows to one evidence class. A query can belong to
        several classes; pooling them would inflate a class-specific metric.
        """
        ok = {RELEVANT} | ({BORDERLINE} if include_borderline else set())
        return {
            j.doc_id
            for j in self.judgments
            if j.query_id == query_id
            and j.label in ok
            and (provenance is None or j.provenance == provenance)
        }

    def borderline(self, query_id: str) -> set[str]:
        return {
            j.doc_id
            for j in self.judgments
            if j.query_id == query_id and j.label == BORDERLINE
        }

    def provenances_of(self, query_id: str) -> set[str]:
        """Every provenance under which this query has a POSITIVE judgment."""
        return {
            j.provenance
            for j in self.for_query(query_id)
            if j.label == RELEVANT
        }


def expand_lesson_classes(classes: list[dict]) -> list[dict]:
    """Turn adjudicated lesson classes into symmetric pair judgments.

    Class assignments carry a rationale for each member. Pairs derived from
    those assignments are not independent judgments. Each pair retains both
    members' rationales so the derivation can be reviewed.

    A class member listed under ``borderline`` pairs as ``borderline`` against
    every other member of the class: read, judged genuinely ambiguous, and
    excluded from both sides of the metric rather than silently forced.
    """
    out: list[dict] = []
    for cls in classes:
        cid = cls["id"]
        members = list(cls.get("members", []))
        borderline = list(cls.get("borderline", []))
        for a in members + borderline:
            for b in members + borderline:
                if a["doc_id"] == b["doc_id"]:
                    continue
                is_bord = a in borderline or b in borderline
                out.append(
                    {
                        "query_id": a["doc_id"],
                        "doc_id": b["doc_id"],
                        "label": BORDERLINE if is_bord else RELEVANT,
                        "provenance": "adjudicated_family",
                        "reason": (
                            f"lesson class '{cid}'. query: {a['reason']} "
                            f"doc: {b['reason']}"
                        ),
                        "hard_call": bool(a.get("hard_call") or b.get("hard_call")),
                    }
                )
    return out


def load_qrels(path: Path = QRELS_PATH, corpus: Sequence[Doc] | None = None) -> Qrels:
    """Parse a qrel file and (optionally) integrity-check it.

    Two sections, both optional but at least one required:

    * ``lesson_classes`` — human class assignments, expanded to symmetric
      ``adjudicated_family`` pairs by :func:`expand_lesson_classes`.
    * ``judgments`` — explicit pairs (the ``miner_flagged`` gold set and the
      hand-mined ``hard_negative`` probes). An explicit entry OVERRIDES an
      expanded one for the same (query, doc), so a cross-class exception can
      be recorded without editing the class lists.

    Raises when a judgment references a doc id or query id that is not in the
    corpus — a stale qrel must fail, never silently score zero.
    """
    import yaml

    if not path.exists():
        raise RetrievalEvalError(f"qrels file missing: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not ({"judgments", "lesson_classes"} & set(raw)):
        raise RetrievalEvalError(
            f"qrels file must be a mapping with 'judgments' and/or 'lesson_classes': {path}"
        )
    expanded = expand_lesson_classes(raw.get("lesson_classes") or [])
    explicit = list(raw.get("judgments") or [])
    explicit_keys = {(e.get("query_id"), e.get("doc_id")) for e in explicit}
    entries = [e for e in expanded if (e["query_id"], e["doc_id"]) not in explicit_keys]
    entries += explicit

    judgments: list[Judgment] = []
    seen: set[tuple[str, str]] = set()
    for i, entry in enumerate(entries):
        missing = {"query_id", "doc_id", "label", "provenance", "reason"} - set(entry)
        if missing:
            raise RetrievalEvalError(f"qrels entry {i}: missing keys {sorted(missing)}")
        if entry["label"] not in _LABELS:
            raise RetrievalEvalError(
                f"qrels entry {i}: label {entry['label']!r} not in {sorted(_LABELS)}"
            )
        if entry["provenance"] not in PROVENANCES:
            raise RetrievalEvalError(
                f"qrels entry {i}: provenance {entry['provenance']!r} not in "
                f"{sorted(PROVENANCES)}"
            )
        if not str(entry["reason"]).strip():
            raise RetrievalEvalError(f"qrels entry {i}: reason must be non-empty")
        key = (entry["query_id"], entry["doc_id"])
        if key in seen:
            raise RetrievalEvalError(f"qrels entry {i}: duplicate judgment for {key}")
        seen.add(key)
        if entry["query_id"] == entry["doc_id"]:
            raise RetrievalEvalError(f"qrels entry {i}: query judged against itself")
        judgments.append(
            Judgment(
                query_id=entry["query_id"],
                doc_id=entry["doc_id"],
                label=entry["label"],
                provenance=entry["provenance"],
                reason=str(entry["reason"]).strip(),
                hard_call=bool(entry.get("hard_call", False)),
            )
        )
    documents = dict(raw.get("documents") or {})
    referenced = {j.query_id for j in judgments} | {j.doc_id for j in judgments}
    undocumented = sorted(referenced - set(documents))
    if undocumented:
        raise RetrievalEvalError(
            f"{path}: {len(undocumented)} judged ids have no entry under "
            f"`documents:` — the qrel file must be self-contained. Missing: "
            f"{undocumented}"
        )
    for cid, entry in documents.items():
        if "text" not in entry:
            raise RetrievalEvalError(f"{path}: documents[{cid}] has no 'text'")
        actual = content_id(entry["text"])
        if actual != cid:
            raise RetrievalEvalError(
                f"{path}: documents[{cid}] text hashes to {actual} — the stored "
                "text was edited without re-keying, so the judgments no longer "
                "describe this document"
            )
    qrels = Qrels(
        judgments=tuple(judgments),
        notes=str(raw.get("notes", "")),
        documents=documents,
    )
    if corpus is not None:
        assert_qrels_resolvable(qrels, corpus, where=str(path))
    return qrels


def assert_qrels_resolvable(
    qrels: Qrels, corpus: Sequence[Doc], where: str = "corpus"
) -> None:
    """Every judged id must exist in ``corpus``, with matching text.

    FAIL LOUD, AND NAME THE ENTRIES. A missing qrel id must never be dropped
    from the denominator: recall computed over a silently-shrunken qrel set
    goes UP as data disappears, which is exactly the quiet wrong answer this
    project's rules exist to prevent. Every missing id is listed, not
    truncated, so the failure is actionable rather than suggestive.
    """
    by_id = {d.doc_id: d for d in corpus}
    referenced = sorted(
        {j.query_id for j in qrels.judgments} | {j.doc_id for j in qrels.judgments}
    )
    missing = [cid for cid in referenced if cid not in by_id]
    if missing:
        lines = [
            f"{len(missing)} of {len(referenced)} judged documents are absent from "
            f"{where}. These are CONTENT addresses, so this means the text itself is "
            "gone or changed — not that ids were reassigned. If the state DB was "
            "rebuilt and re-mined, the new rule texts are different documents and the "
            "qrels must be re-adjudicated against them; do not 'fix' this by dropping "
            "the entries.",
        ]
        for cid in missing:
            text = (qrels.documents.get(cid) or {}).get("text", "<no text recorded>")
            observed = (qrels.documents.get(cid) or {}).get("observed_as", "")
            lines.append(
                f"  {cid} (observed_as={observed or 'n/a'}): {text[:110]!r}"
            )
        raise RetrievalEvalError("\n".join(lines))
    mismatched = [
        cid
        for cid in referenced
        if normalize_for_id(by_id[cid].text)
        != normalize_for_id(qrels.documents[cid]["text"])
    ]
    if mismatched:
        raise RetrievalEvalError(
            f"text mismatch between qrels and {where} for: {mismatched}"
        )


def corpus_from_qrels(qrels: Qrels) -> list[Doc]:
    """Rebuild the judged corpus from the qrel file alone — no DB, no snapshot.

    This is what makes the eval survive a state-DB wipe: the qrel file carries
    every judged document verbatim, so the retrieval task is reproducible even
    if ``~/.self-improve/state.db`` and ``corpus.jsonl`` both vanish. Note it
    contains only JUDGED documents — the unjudged distractors that make
    precision meaningful live in ``corpus.jsonl``.
    """
    return [
        Doc(
            doc_id=cid,
            kind=entry.get("kind", "learning"),
            text=entry["text"],
            why=entry.get("why", ""),
            category=entry.get("category", ""),
            status=entry.get("status", ""),
            source_ref=entry.get("source_ref", ""),
            observed_as=entry.get("observed_as", ""),
        )
        for cid, entry in qrels.documents.items()
    ]


# ---------------------------------------------------------------------------
# Arms
# ---------------------------------------------------------------------------

Ranker = Callable[[str, int], list[str]]

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_.\-]*")
# Mirrors cluster._STOPWORDS but kept local and TF-preserving: BM25 needs term
# frequencies, and cluster.normalize_tokens returns a set.
_STOPWORDS = frozenset(
    "a an and are as at be been but by can for from had has have if in into is it its "
    "of on or so than that the their then there these this to was were what when "
    "which will with would you your never always must should".split()
)


def tokenize(text: str) -> list[str]:
    """Lowercased content tokens, duplicates preserved (BM25 needs TF)."""
    return [t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS]


class BM25:
    """Okapi BM25 over the corpus's ``keyword_text``.

    PROXY, NOT THE PRODUCT. The shipped keyword arm is an LLM writing grep
    patterns against ``learnings.jsonl``; that is not reproducible, so this
    stands in for "deterministic lexical retrieval". k1/b are the standard
    defaults (1.5 / 0.75) and are recorded in the report rather than tuned —
    tuning them against the evaluation judgments would risk overfitting.
    """

    K1 = 1.5
    B = 0.75

    def __init__(self, docs: Sequence[Doc]) -> None:
        self.docs = list(docs)
        self.tokens = [tokenize(d.keyword_text) for d in self.docs]
        self.lengths = [len(t) for t in self.tokens]
        self.avgdl = (sum(self.lengths) / len(self.lengths)) if self.lengths else 0.0
        self.tf = [Counter(t) for t in self.tokens]
        df: Counter[str] = Counter()
        for t in self.tokens:
            df.update(set(t))
        n = len(self.docs)
        self.idf = {
            term: math.log(1.0 + (n - freq + 0.5) / (freq + 0.5))
            for term, freq in df.items()
        }

    def rank(self, query: str, top_k: int) -> list[str]:
        if not query.strip():
            raise RetrievalEvalError("query must be non-empty")
        q = tokenize(query)
        scored: list[tuple[float, int, str]] = []
        for i, doc in enumerate(self.docs):
            score = 0.0
            dl = self.lengths[i] or 1
            for term in q:
                f = self.tf[i].get(term, 0)
                if not f:
                    continue
                denom = f + self.K1 * (1 - self.B + self.B * dl / self.avgdl)
                score += self.idf.get(term, 0.0) * f * (self.K1 + 1) / denom
            # Deterministic tie-break on doc_id so re-runs are byte-identical.
            scored.append((-score, i, doc.doc_id))
        scored.sort(key=lambda x: (x[0], x[2]))
        return [doc_id for neg, _, doc_id in scored if neg < 0][:top_k]


def build_corpus_store(cfg: Config, corpus: Sequence[Doc], db_path: Path) -> None:
    """Seed a throwaway store's ``learnings`` table with the frozen corpus.

    This is what lets the semantic arm be the REAL
    :func:`self_improve.search.search_learnings` rather than a reimplementation
    of its ranking. Instruction-file rule units are inserted as rows too.

    Populate the cache to exercise search.py's cache reader and avoid embedding
    every document again for each query. Cache keys bind the selected model.
    """
    from ..embeddings import Embedder
    from ..store import Store, utc_now_iso

    store = Store(db_path)
    try:
        now = utc_now_iso()
        for d in corpus:
            store.insert(
                "learnings",
                {
                    "id": d.doc_id,
                    "rule_text": d.text,
                    "why": d.why,
                    "category": d.category,
                    "status": d.status or "candidate",
                    "created_at": now,
                },
            )
        embedder = Embedder(cfg, store=None)
        vectors = embedder.encode([d.text for d in corpus])
        for d, vec in zip(corpus, vectors):
            store.insert(
                "embeddings",
                {
                    "owner_kind": "learning",
                    "owner_key": d.doc_id,
                    "model": embedder.cache_key,
                    "text_sha": hashlib.sha1(d.text.encode("utf-8")).hexdigest(),
                    "vector_json": json.dumps(vec),
                    "created_at": now,
                },
            )
    finally:
        store.close()


def semantic_ranker(cfg: Config, exclude_self: bool = True) -> Ranker:
    """Rank via the production ``search_learnings``.

    ``exclude_self`` drops the query's own document: every query in this eval
    is itself a corpus document, and a system trivially retrieving the query
    itself would inflate every metric. It over-fetches by one so dropping self
    never shortens the result list.
    """
    from ..search import search_learnings

    def rank(query: str, top_k: int, query_id: str = "") -> list[str]:
        # include_in_force=False: the eval builds its OWN corpus store, which
        # already contains instruction-file rule units as documents. Letting
        # search also read the live in-force files would double-count them AND
        # make the score depend on whatever is in the developer's ~/.claude
        # right now — the corpus under test must be the selected frozen one.
        rows = search_learnings(
            cfg, query, top_k=top_k + 1, include_in_force=False
        )
        ids = [r["id"] for r in rows]
        if exclude_self and query_id:
            ids = [i for i in ids if i != query_id]
        return ids[:top_k]

    return rank  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def _metrics_for_query(
    ranked: Sequence[str],
    relevant: set[str],
    borderline: set[str],
    ks: Iterable[int],
) -> dict:
    """recall@k / precision@k / reciprocal rank for one query.

    Precision convention (stated, not implied): a retrieved doc is a false
    positive if it is judged ``not_relevant`` OR unjudged (TREC's
    unjudged-as-non-relevant). ``borderline`` docs are removed from the
    retrieved list before precision is computed — they are neither credited
    nor penalised — so precision@k has a per-query denominator of
    ``k - borderline_retrieved``.
    """
    out: dict = {}
    for k in ks:
        topk = list(ranked[:k])
        hits = [d for d in topk if d in relevant]
        scoreable = [d for d in topk if d not in borderline]
        out[f"recall@{k}"] = (len(hits) / len(relevant)) if relevant else None
        out[f"precision@{k}"] = (len(hits) / len(scoreable)) if scoreable else None
        out[f"hits@{k}"] = len(hits)
    rr = 0.0
    for pos, doc_id in enumerate(ranked, start=1):
        if doc_id in relevant:
            rr = 1.0 / pos
            break
    out["rr"] = rr if relevant else None
    out["n_relevant"] = len(relevant)
    return out


def _mean(values: Iterable[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    return (sum(vals) / len(vals)) if vals else None


def rank_all(
    cfg: Config,
    corpus: Sequence[Doc],
    query_ids: Sequence[str],
    maxk: int,
) -> dict[str, dict[str, list[str]]]:
    """Run every arm over every query once. ``{query_id: {arm: [doc_id, ...]}}``.

    Reuse these rankings for every scoring scope and the borderline-sensitivity
    pass. This avoids repeated model work and keeps the compared rankings fixed.
    """
    by_id = {d.doc_id: d for d in corpus}
    bm25 = BM25(corpus)
    sem = semantic_ranker(cfg)
    out: dict[str, dict[str, list[str]]] = {}
    for qid in query_ids:
        doc = by_id.get(qid)
        if doc is None:
            raise RetrievalEvalError(f"query {qid} is not in the corpus")
        sem_ids = sem(doc.text, maxk, qid)  # type: ignore[call-arg]
        kw_ids = [d for d in bm25.rank(doc.text, maxk + 1) if d != qid][:maxk]
        out[qid] = {
            "semantic": sem_ids,
            "keyword_bm25_proxy": kw_ids,
            "oracle_union": _union_at_k(sem_ids, kw_ids, maxk),
        }
    return out


def evaluate(
    cfg: Config,
    corpus: Sequence[Doc],
    qrels: Qrels,
    ks: Sequence[int] = K_VALUES,
    include_borderline: bool = False,
    rankings: dict[str, dict[str, list[str]]] | None = None,
) -> dict:
    """Score all three arms over every query, once per scope it belongs to.

    Scopes are separate retrieval tasks, not slices of one:

    * ``overall`` — relevant set is the UNION of every positive for the query.
      This is the product question: "is this lesson already written down
      anywhere?"
    * ``miner_flagged`` / ``adjudicated_family`` — relevant set is restricted
      to that provenance. A query with no positive of that provenance is not
      scored in that scope at all (rather than scored as a zero).

    Ranking is computed ONCE per query and reused across scopes: the arms do
    not know what provenance is being scored, only the qrels do.
    """
    if rankings is None:
        rankings = rank_all(cfg, corpus, qrels.query_ids(), max(ks))
    ks = tuple(ks)
    arm_names = ("semantic", "keyword_bm25_proxy", "oracle_union")

    per_query: list[dict] = []
    for qid in qrels.query_ids():
        if qid not in rankings:
            raise RetrievalEvalError(f"no ranking computed for query {qid}")
        bord = set() if include_borderline else qrels.borderline(qid)
        rejected = {
            j.doc_id for j in qrels.for_query(qid) if j.label == NOT_RELEVANT
        }
        ranked_by_arm = rankings[qid]
        scopes: dict[str, dict] = {}
        for scope in ("overall",) + tuple(sorted(qrels.provenances_of(qid))):
            prov = None if scope == "overall" else scope
            rel = qrels.relevant(
                qid, include_borderline=include_borderline, provenance=prov
            )
            if not rel:
                continue
            scopes[scope] = {
                arm: _metrics_for_query(ranked_by_arm[arm], rel, bord, ks)
                for arm in arm_names
            }
        row: dict = {
            "query_id": qid,
            "scopes": scopes,
            "rejected_in_topk": {
                arm: len([d for d in ranked_by_arm[arm][:HEADLINE_K] if d in rejected])
                for arm in arm_names
            },
            "n_judged_negatives": len(rejected),
            "ranked": {arm: list(v) for arm, v in ranked_by_arm.items()},
        }
        per_query.append(row)

    scope_names = sorted({s for r in per_query for s in r["scopes"]} - {"overall"})
    return {
        "ks": list(ks),
        "headline_k": HEADLINE_K,
        "include_borderline": include_borderline,
        "corpus_size": len(corpus),
        "per_query": per_query,
        "overall": _aggregate(per_query, ks, "overall"),
        "by_provenance": {s: _aggregate(per_query, ks, s) for s in scope_names},
    }


def _union_at_k(a: Sequence[str], b: Sequence[str], k: int) -> list[str]:
    """Interleave two ranked lists (a first) and dedupe, truncated to k.

    Interleaving, not concatenation: the union's *recall* at k is what the
    section title promises, and interleaving is the fusion a blender with no
    score calibration would actually do (round-robin). Reported as a ceiling,
    not a proposal.
    """
    out: list[str] = []
    seen: set[str] = set()
    for i in range(max(len(a), len(b))):
        for lst in (a, b):
            if i < len(lst) and lst[i] not in seen:
                seen.add(lst[i])
                out.append(lst[i])
    return out[:k]


def _aggregate(per_query: list[dict], ks: Sequence[int], scope: str) -> dict:
    """Macro-average over the queries scored in ``scope``.

    Use the mean of per-query rates. Pooling hits across queries would give
    larger relevance families more influence over the aggregate.
    """
    scored = [r for r in per_query if scope in r["scopes"]]
    out: dict = {
        "n_queries_with_positives": len(scored),
        "n_relevant_per_query": [
            r["scopes"][scope]["semantic"]["n_relevant"] for r in scored
        ],
    }
    # At most k relevant documents fit in k slots. Report the structural limit
    # so a large relevant set does not look like an unexplained ranking failure.
    out["recall_ceiling"] = {
        f"recall@{k}": _mean(
            min(k, r["scopes"][scope]["semantic"]["n_relevant"])
            / r["scopes"][scope]["semantic"]["n_relevant"]
            for r in scored
        )
        for k in ks
    }
    for arm in ("semantic", "keyword_bm25_proxy", "oracle_union"):
        m: dict = {}
        for k in ks:
            m[f"recall@{k}"] = _mean(
                r["scopes"][scope][arm][f"recall@{k}"] for r in scored
            )
            m[f"precision@{k}"] = _mean(
                r["scopes"][scope][arm][f"precision@{k}"] for r in scored
            )
        m["mrr"] = _mean(r["scopes"][scope][arm]["rr"] for r in scored)
        # Judged-and-rejected docs surfaced in the top-8, summed over EVERY
        # query in the run (not just this scope's): a precision signal that
        # does not depend on which positives are in play.
        m["rejected_in_topk"] = sum(r["rejected_in_topk"][arm] for r in per_query)
        out[arm] = m
    out["judged_negatives_total"] = sum(r["n_judged_negatives"] for r in per_query)
    return out


# ---------------------------------------------------------------------------
# cluster_dup_cosine threshold sweep
# ---------------------------------------------------------------------------


def threshold_sweep(
    cfg: Config,
    corpus: Sequence[Doc],
    qrels: Qrels,
    thresholds: Sequence[float] | None = None,
) -> dict:
    """Sweep ``cluster_dup_cosine`` over the gold set + judged negatives.

    At each threshold: how many judged-relevant pairs does a cosine cut catch
    (true positives), and how many judged-``not_relevant`` pairs does it admit
    (false positives)? Scored on the *pair* level, because that is the level
    :func:`self_improve.cluster.is_duplicate` operates at.

    Reported for two scopes:

    * ``miner_flagged`` uses the selected gold pairs. Derived negatives compare
      each gold query with every corpus rule unit except its named target.
      These are dataset assumptions, not independent manual judgments, and
      the result identifies and counts them separately.
    * ``all_judged`` — every hand-judged pair, gold + adjudicated + hard
      negatives. Derived negatives are excluded here.
    """
    from ..embeddings import Embedder, cosine

    if thresholds is None:
        thresholds = [round(0.30 + 0.05 * i, 2) for i in range(14)]  # 0.30..0.95
    by_id = {d.doc_id: d for d in corpus}
    embedder = Embedder(cfg, store=None)

    pairs: list[tuple[Judgment, float]] = []
    texts_needed = sorted(
        {j.query_id for j in qrels.judgments} | {j.doc_id for j in qrels.judgments}
    )
    missing = [t for t in texts_needed if t not in by_id]
    if missing:
        raise RetrievalEvalError(f"sweep: ids absent from corpus: {missing[:5]}")
    vecs = dict(zip(texts_needed, embedder.encode([by_id[t].text for t in texts_needed])))
    for j in qrels.judgments:
        pairs.append((j, cosine(vecs[j.query_id], vecs[j.doc_id])))

    # Derived gold negatives: each gold query x every non-target corpus rule
    # unit. Counted and reported separately from explicit negative judgments.
    gold = [j for j in qrels.judgments if j.provenance == "miner_flagged"]
    gold_targets = {j.doc_id for j in gold}
    rule_units = [d for d in corpus if d.kind == "rule_unit"]
    unit_vecs = dict(
        zip(
            [d.doc_id for d in rule_units],
            embedder.encode([d.text for d in rule_units]),
        )
    )
    derived_neg: list[float] = []
    for j in gold:
        for unit in rule_units:
            if unit.doc_id == j.doc_id:
                continue
            derived_neg.append(cosine(vecs[j.query_id], unit_vecs[unit.doc_id]))

    def rows_for(scope: str) -> list[tuple[Judgment, float]]:
        if scope == "miner_flagged":
            return [(j, c) for j, c in pairs if j.provenance == "miner_flagged"]
        return pairs

    out: dict = {
        "thresholds": list(thresholds),
        "scopes": {},
        "derived_gold_negatives": len(derived_neg),
        "derived_gold_negatives_note": (
            f"{len(gold)} gold queries x {len(rule_units) - 1} non-target rule units "
            f"of {sorted({d.source_ref for d in rule_units})}"
        ),
    }
    for scope in ("miner_flagged", "all_judged"):
        rows = rows_for(scope)
        pos = [(j, c) for j, c in rows if j.label == RELEVANT]
        neg = [(j, c) for j, c in rows if j.label == NOT_RELEVANT]
        neg_scores = [c for _, c in neg]
        if scope == "miner_flagged":
            neg_scores = derived_neg
        table = []
        for t in thresholds:
            tp = sum(1 for _, c in pos if c >= t)
            fp = sum(1 for c in neg_scores if c >= t)
            table.append(
                {
                    "threshold": t,
                    "true_duplicates_caught": tp,
                    "true_duplicates_total": len(pos),
                    "false_positives_admitted": fp,
                    "negatives_total": len(neg_scores),
                }
            )
        out["scopes"][scope] = {
            "negative_kind": (
                "derived (gold query x non-target rule units)"
                if scope == "miner_flagged"
                else "hand-judged not_relevant pairs"
            ),
            "table": table,
            "positive_cosines": sorted((round(c, 4) for _, c in pos), reverse=True)[:20],
            "positive_cosines_truncated_from": len(pos),
            "negative_cosines": sorted((round(c, 4) for c in neg_scores), reverse=True)[:20],
            "separable": _is_separable([c for _, c in pos], neg_scores),
        }
    out["live_threshold"] = cfg.cluster_dup_cosine
    return out


def _is_separable(pos: Sequence[float], neg: Sequence[float]) -> dict:
    """Is there ANY threshold that takes every positive and no negative?

    That is exactly ``min(pos) > max(neg)``. Returns the margin so a
    near-miss is visible rather than collapsing to a bare False.
    """
    if not pos or not neg:
        return {"separable": None, "reason": "one side empty"}
    lo, hi = min(pos), max(neg)
    return {
        "separable": lo > hi,
        "min_positive_cosine": round(lo, 4),
        "max_negative_cosine": round(hi, 4),
        "margin": round(lo - hi, 4),
    }


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run(
    cfg: Config,
    dataset_dir: Path,
    ks: Sequence[int] = K_VALUES,
    *,
    expected_manifest: Path | None = None,
    kind: str = "private",
) -> dict:
    """Full eval: load, seed a throwaway store, score arms, sweep thresholds."""
    import tempfile

    from ..data_boundary import load_dataset

    dataset = load_dataset(dataset_dir, expected=expected_manifest, kind=kind)
    corpus, meta = load_corpus(dataset_dir / "retrieval/corpus.jsonl")
    qrels = load_qrels(dataset_dir / "retrieval/qrels.yaml", corpus=corpus)
    from ..embeddings import Embedder
    model_identity = Embedder(cfg, None).provenance
    with tempfile.TemporaryDirectory(prefix="selfimprove-reteval-") as tmp:
        eval_cfg = dataclasses.replace(cfg, state_dir=tmp,
                                       embedding_model_sha256=model_identity["files_sha256"])
        build_corpus_store(eval_cfg, corpus, eval_cfg.state_path("state.db"))
        rankings = rank_all(eval_cfg, corpus, qrels.query_ids(), max(ks))
        result = evaluate(eval_cfg, corpus, qrels, ks=ks, rankings=rankings)
        result["sensitivity_borderline_as_relevant"] = evaluate(
            eval_cfg,
            corpus,
            qrels,
            ks=ks,
            include_borderline=True,
            rankings=rankings,
        )["overall"]
        result["sweep"] = threshold_sweep(eval_cfg, corpus, qrels)
    result["corpus_meta"] = meta
    result["dataset"] = {k: dataset[k] for k in ("dataset_id", "version", "kind", "schema_versions", "sha256")}
    result["embedding_model"] = cfg.embedding_model
    result["embedding_model_identity"] = model_identity
    result["qrel_counts"] = _qrel_counts(qrels)
    # Retain per-query detail while both results and judgments are in scope.
    result["gold_detail"] = render_gold_detail(result, qrels)
    return result


def _qrel_counts(qrels: Qrels) -> dict:
    counts: dict = {"total": len(qrels.judgments), "by_provenance": {}, "by_label": {}}
    for j in qrels.judgments:
        counts["by_provenance"].setdefault(j.provenance, Counter())[j.label] += 1
        counts["by_label"][j.label] = counts["by_label"].get(j.label, 0) + 1
    counts["by_provenance"] = {
        p: dict(c) for p, c in counts["by_provenance"].items()
    }
    counts["hard_calls"] = sum(1 for j in qrels.judgments if j.hard_call)
    return counts


def _fmt(v: float | None) -> str:
    return "  n/a" if v is None else f"{v:5.3f}"


def render_text_report(result: dict) -> str:
    """Plain-text summary — what the CLI prints and the report doc quotes.

    Ends with the per-gold-query detail stashed by :func:`run`, so the arm-by-arm
    ranking of every miner-flagged duplicate is in the same output as the
    aggregate that summarises it.
    """
    lines: list[str] = []
    dataset = result.get("dataset")
    if dataset:
        lines.append(f"dataset: {dataset['dataset_id']} v{dataset['version']} ({dataset['kind']}) sha256={dataset['sha256']}")
        if dataset["kind"] == "synthetic":
            lines.append("SYNTHETIC DEMO — invented examples; these scores do not measure real-history retrieval.")
    identity = result.get("embedding_model_identity")
    if identity:
        source = identity["source"]
        model = (f"{source['repository']}@{source['revision']}"
                 if source["kind"] == "huggingface" else "local model directory")
        lines.append(f"embedding model: {model}")
        lines.append(f"model files sha256: {identity['files_sha256']}")
        lines.append(f"embedding cache identity: {identity['cache_key']}")
    else:
        lines.append("embedding model identity: unrecorded in this result")
    meta = result["corpus_meta"]
    qc = result["qrel_counts"]
    lines.append(
        f"corpus: {meta['docs']} docs "
        f"({meta['learnings']} learnings + {meta['rule_units']} instruction rule units)"
    )
    lines.append(
        f"qrels: {qc['total']} judged pairs "
        + ", ".join(f"{k}={v}" for k, v in sorted(qc["by_label"].items()))
        + f", hard_calls={qc['hard_calls']}"
    )
    ks = result["ks"]
    header = (
        f"{'scope':<20} {'arm':<20} "
        + " ".join(f"{'R@'+str(k):>6}" for k in ks)
        + " "
        + " ".join(f"{'P@'+str(k):>6}" for k in ks)
        + f" {'MRR':>6} {'n':>4}"
    )
    lines.append("")
    lines.append(header)
    lines.append("-" * len(header))
    for scope, agg in [("OVERALL", result["overall"])] + sorted(
        result["by_provenance"].items()
    ):
        for arm in ("semantic", "keyword_bm25_proxy", "oracle_union"):
            m = agg[arm]
            lines.append(
                f"{scope:<20} {arm:<20} "
                + " ".join(_fmt(m[f"recall@{k}"]) + " " for k in ks)
                + " ".join(_fmt(m[f"precision@{k}"]) + " " for k in ks)
                + _fmt(m["mrr"])
                + f" {agg['n_queries_with_positives']:>4}"
            )
        ceil = agg["recall_ceiling"]
        lines.append(
            f"{scope:<20} {'(recall ceiling)':<20} "
            + " ".join(_fmt(ceil[f"recall@{k}"]) + " " for k in ks)
            + "  -- max achievable: a query with more relevant docs than k "
            "cannot reach 1.0"
        )
        lines.append("")
    ov = result["overall"]
    lines.append(
        f"judged-negative leakage into top-{HEADLINE_K} "
        f"(of {ov['judged_negatives_total']} hand-judged not_relevant pairs): "
        + ", ".join(
            f"{arm}={ov[arm]['rejected_in_topk']}"
            for arm in ("semantic", "keyword_bm25_proxy", "oracle_union")
        )
    )
    sweep = result["sweep"]
    lines.append("")
    lines.append(f"cluster_dup_cosine sweep (live threshold = {sweep['live_threshold']})")
    lines.append(f"  derived gold negatives: {sweep['derived_gold_negatives']} "
                 f"= {sweep['derived_gold_negatives_note']}")
    for scope, data in sweep["scopes"].items():
        sep = data["separable"]
        lines.append("")
        lines.append(f"  [{scope}] negatives = {data['negative_kind']}")
        lines.append(f"  separable={sep.get('separable')} "
                     f"min(pos)={sep.get('min_positive_cosine')} "
                     f"max(neg)={sep.get('max_negative_cosine')} "
                     f"margin={sep.get('margin')}")
        lines.append(f"  {'thr':>6} {'caught':>8} {'of':>4} {'false_pos':>10} {'of':>6}")
        for row in data["table"]:
            lines.append(
                f"  {row['threshold']:>6.2f} {row['true_duplicates_caught']:>8} "
                f"{row['true_duplicates_total']:>4} "
                f"{row['false_positives_admitted']:>10} "
                f"{row['negatives_total']:>6}"
            )
        lines.append(
            f"  positive cosines (top {len(data['positive_cosines'])} of "
            f"{data['positive_cosines_truncated_from']}): {data['positive_cosines']}"
        )
        lines.append(f"  negative cosines (top 20): {data['negative_cosines']}")
    gold_detail = result.get("gold_detail")
    if gold_detail:
        lines.append("")
        lines.append("Per-gold-query detail (where each arm ranked the rule the")
        lines.append("miner itself named as the duplicate):")
        lines.append("")
        lines.append(gold_detail)
    return "\n".join(lines)


def render_gold_detail(result: dict, qrels: Qrels) -> str:
    """Per-gold-query table: where each arm ranked the rule the miner named."""
    docs = qrels.documents
    gold = {
        j.query_id: j.doc_id
        for j in qrels.judgments
        if j.provenance == "miner_flagged" and j.label == RELEVANT
    }
    lines = [
        f"{'query (new rule text)':<62} {'sem':>4} {'bm25':>5} {'union':>6}  target"
    ]
    lines.append("-" * 104)
    for row in result["per_query"]:
        qid = row["query_id"]
        if qid not in gold:
            continue
        target = gold[qid]

        def pos(arm: str) -> str:
            ranked = row["ranked"][arm]
            return str(ranked.index(target) + 1) if target in ranked else ">8"

        qtext = normalize_for_id(docs[qid]["text"])[:60]
        ttext = normalize_for_id(docs[target]["text"])[:46]
        lines.append(
            f"{qtext:<62} {pos('semantic'):>4} {pos('keyword_bm25_proxy'):>5} "
            f"{pos('oracle_union'):>6}  {ttext}"
        )
    return "\n".join(lines)
