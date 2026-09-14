"""Resolve immutable embedding inputs without opening operational state."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import re


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def fingerprint(folder: Path) -> tuple[str, list[dict]]:
    """Hash a dedicated model directory, including metadata and alternate layouts.

    Git and download-cache metadata are not model inputs. File links (including
    Hub blob links) are hashed by their bytes. Directory links are refused so a
    hidden subtree cannot escape the inventory. No mtime shortcut is used.
    """
    if not folder.is_dir():
        raise ValueError(f"local model directory does not exist: {folder}")
    records = []
    def walk_error(error):
        raise error
    for directory, dirs, files in os.walk(folder, onerror=walk_error):
        dirs[:] = sorted(d for d in dirs if d not in {".git", ".cache"})
        for name in dirs:
            if (Path(directory) / name).is_symlink():
                raise ValueError("model directory links are not supported; use a complete model directory")
        for name in sorted(files):
            if name in {".git", ".cache"}:
                continue
            path = Path(directory) / name
            if not path.is_file():
                raise ValueError(f"model member is not a readable regular file: {path}")
            digest = hashlib.sha256()
            size = 0
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
                    size += len(chunk)
            records.append({"path": path.relative_to(folder).as_posix(),
                            "bytes": size, "sha256": digest.hexdigest()})
    if not records:
        raise ValueError("local model directory contains no model files")
    records.sort(key=lambda row: row["path"])
    return hashlib.sha256(_json(records).encode()).hexdigest(), records


@dataclass(frozen=True)
class ResolvedModel:
    folder: Path
    files_sha256: str
    cache_key: str
    _provenance_json: str

    @property
    def provenance(self) -> dict:
        # Callers cannot mutate the identity already selected by an Embedder.
        return json.loads(self._provenance_json)

    def check_unchanged(self) -> None:
        if fingerprint(self.folder)[0] != self.files_sha256:
            raise ValueError("embedding model changed during load; retry with a frozen model directory")


def resolve_model(model_name: str, revision: str, expected_sha256: str = "") -> ResolvedModel:
    if expected_sha256 and not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("embedding_model_sha256 must be a full lowercase SHA-256 digest")
    if not model_name:
        raise ValueError("embedding_model must name a model directory or Hub repository")
    candidate = Path(model_name).expanduser()
    if candidate.exists() or model_name.startswith(("/", ".", "~")):
        folder = candidate.resolve()
        if not folder.is_dir():
            raise ValueError(f"local model directory does not exist: {folder}")
        source = {"kind": "local"}
    else:
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ValueError("embedding_revision requires a full lowercase Hub commit ID; branches and tags are not pins")
        from huggingface_hub import snapshot_download
        from huggingface_hub.utils import validate_repo_id
        validate_repo_id(model_name)
        # model2vec's public loader has no revision argument. Resolve with the
        # Hub API, then pass only the resulting local folder to that loader.
        folder = Path(snapshot_download(model_name, repo_type="model", revision=revision))
        if folder.name != revision:
            raise ValueError("Hub returned a snapshot for a different embedding revision")
        source = {"kind": "huggingface", "repository": model_name, "revision": revision}
    digest, files = fingerprint(folder)
    if expected_sha256 and digest != expected_sha256:
        raise ValueError(f"embedding model content hash mismatch: expected {expected_sha256}, got {digest}")
    runtime = {name: version(name) for name in ("model2vec", "numpy", "tokenizers", "safetensors")}
    identity = {"schema_version": 1, "source": source, "files_sha256": digest,
                "files": files, "runtime": runtime, "encoding": "model2vec-defaults/plain-floats-v1"}
    key = "model2vec-v1:" + hashlib.sha256(_json(identity).encode()).hexdigest()
    return ResolvedModel(folder, digest, key, _json({**identity, "cache_key": key}))
