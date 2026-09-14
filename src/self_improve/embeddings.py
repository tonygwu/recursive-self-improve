"""Local static-embedding wrapper (model2vec) with a store-backed vector cache.

Rule text is embedded ON THIS MACHINE (`model2vec.StaticModel`); it never
leaves the machine for embedding. Vectors are cached in the ``embeddings``
table (migration 0003) keyed by (owner_kind, owner_key, model) with a
``text_sha`` staleness check, so re-runs don't re-encode unchanged text and a
model swap invalidates cleanly. The model key binds the source revision, model
file hashes, encoding policy, and numerical-library versions. Legacy name-only
rows are preserved but cannot satisfy this identity.

Fail-loud policy: a missing or undownloadable model raises
:class:`EmbeddingError` — there is deliberately NO fallback to token-overlap
similarity. A run without working embeddings must stop, not silently degrade
its clustering into a different (weaker) similarity metric.
"""

from __future__ import annotations

import hashlib
import json
import math
import os

from .config import Config
from .model_identity import resolve_model
from .store import Store, utc_now_iso


class EmbeddingError(Exception):
    """Model load / encode / cache-integrity failure. Never handled silently."""


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def parse_cached_vector(raw: str, owner: tuple[str, str, str]) -> list[float]:
    """Both cache readers reject malformed JSON and invalid vector shapes."""
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise EmbeddingError(f"corrupt embeddings cache row {owner!r}: vector_json is not valid JSON: {exc}") from exc
    if (not isinstance(parsed, list) or not parsed or
            not all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in parsed)):
        raise EmbeddingError(f"corrupt embeddings cache row {owner!r}: vector_json is not a non-empty list of finite numbers")
    return [float(x) for x in parsed]


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two vectors.

    Raises ValueError on length mismatch or a zero-norm input: a zero vector
    means degenerate (e.g. empty) text was embedded upstream, and returning an
    arbitrary similarity for it would be a silent wrong answer.
    """
    if len(a) != len(b):
        raise ValueError(f"cosine: length mismatch {len(a)} vs {len(b)}")
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        raise ValueError("cosine: zero-norm vector (embedded empty/degenerate text?)")
    return dot / math.sqrt(na * nb)


class Embedder:
    """Lazy wrapper around ``model2vec.StaticModel`` plus the embeddings cache.

    The model is loaded on first :meth:`encode`, not at construction, so
    pipeline stages that end up needing no vectors never pay the load (and a
    broken model config fails at the point of first real use, attributably).
    """

    def __init__(self, cfg: Config, store: Store | None):
        # store=None gives an encode-only Embedder (no cache): used by the
        # read-only search CLI, where the DB must not be written.
        self.cfg = cfg
        self.store = store
        self.model_name = cfg.embedding_model
        self._resolved = None
        self._model = None  # loaded lazily on first encode

    def _resolve(self):
        if self._resolved is None:
            try:
                os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
                self._resolved = resolve_model(self.model_name, self.cfg.embedding_revision,
                                               self.cfg.embedding_model_sha256)
            except Exception as exc:
                raise EmbeddingError(f"failed to resolve embedding model {self.model_name!r}: {exc}") from exc
        return self._resolved

    @property
    def cache_key(self) -> str:
        return self._resolve().cache_key

    @property
    def provenance(self) -> dict:
        return self._resolve().provenance

    def _load(self):
        if self._model is None:
            try:
                resolved = self._resolve()
                resolved.check_unchanged()
                from model2vec import StaticModel  # deferred: heavy import

                model = StaticModel.from_pretrained(resolved.folder)
                resolved.check_unchanged()
                self._model = model
            except Exception as exc:
                raise EmbeddingError(
                    f"failed to load embedding model {self.model_name!r}: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
        return self._model

    def encode(self, texts: list[str]) -> list[list[float]]:
        """Embed texts; returns plain-python float lists (JSON-serializable)."""
        model = self._load()
        try:
            raw = model.encode(texts)
        except Exception as exc:
            raise EmbeddingError(
                f"encode failed with model {self.model_name!r}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        vectors = [[float(x) for x in row] for row in raw]
        if len(vectors) != len(texts):
            raise EmbeddingError(
                f"model {self.model_name!r} returned {len(vectors)} vectors "
                f"for {len(texts)} texts"
            )
        return vectors

    def cached_vector(self, owner_kind: str, owner_key: str, text: str) -> list[float]:
        """Vector for ``text``, served from the embeddings table when fresh.

        Cache row is addressed by (owner_kind, owner_key, model); it is
        recomputed and upserted when missing OR when its ``text_sha`` differs
        from sha1(text) (the owner's text changed since it was cached).
        """
        if self.store is None:
            raise EmbeddingError(
                "cached_vector needs a store; this Embedder is encode-only"
            )
        sha = _sha1(text)
        row = self.store.query_one(
            "SELECT text_sha, vector_json FROM embeddings "
            "WHERE owner_kind = ? AND owner_key = ? AND model = ?",
            (owner_kind, owner_key, self.cache_key),
        )
        if row is not None and row["text_sha"] == sha:
            return parse_cached_vector(row["vector_json"], (owner_kind, owner_key, self.cache_key))
        vec = self.encode([text])[0]
        # Composite-PK upsert. Store's generic update() addresses single-column
        # keys only, so this is explicit ON CONFLICT SQL on the store's
        # connection — Postgres-compatible, same style as Store.upsert_session.
        self.store.conn.execute(
            "INSERT INTO embeddings "
            "(owner_kind, owner_key, model, text_sha, vector_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(owner_kind, owner_key, model) DO UPDATE SET "
            "text_sha = excluded.text_sha, "
            "vector_json = excluded.vector_json, "
            "created_at = excluded.created_at",
            (
                owner_kind,
                owner_key,
                self.cache_key,
                sha,
                json.dumps(vec),
                utc_now_iso(),
            ),
        )
        return vec
