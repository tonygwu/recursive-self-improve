"""Local frozen datasets and verified backups. Never opens the runtime database.

Run ``python -m self_improve.data_boundary --help`` for migration commands.
Manifests bind exact bytes, identity, and schema versions. Hashes detect drift;
they do not establish that a dataset is safe to publish or authenticate its owner.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

import yaml

from .resources import PACKAGE_ROOT, SOURCE_ROOT, bundled_path

SCHEMA_VERSIONS = {"bundle": 1, "corpus": 1, "qrels": 1, "labels": 1}
SYNTHETIC_DATASET = bundled_path("evals", "synthetic")


class DataBoundaryError(Exception):
    """Missing, incompatible, changed, or unsafe dataset. Never a fallback."""


def private_storage_path(path: Path) -> Path:
    """Refuse checkout and package paths, including both spellings of symlinks.

    Existing private directories are allowed for mutable operational artifacts.
    Frozen exports use private_destination, which also requires a new path.
    """
    lexical = Path(os.path.abspath(path.expanduser()))
    resolved = lexical.resolve()
    for candidate in (lexical, resolved):
        if SOURCE_ROOT is not None and candidate.is_relative_to(SOURCE_ROOT):
            raise DataBoundaryError(f"private export destination is inside the source checkout: {path}")
        if candidate.is_relative_to(PACKAGE_ROOT):
            raise DataBoundaryError(f"private export destination is inside the installed package: {path}")
        for parent in (candidate, *candidate.parents):
            if (parent / ".git").exists() or ((parent / "HEAD").is_file() and (parent / "objects").is_dir()):
                raise DataBoundaryError(f"private export destination is inside a Git checkout: {path}")
    return resolved


def private_destination(path: Path) -> Path:
    """Require a new private path; never overwrite existing files or hard links."""
    resolved = private_storage_path(path)
    lexical = Path(os.path.abspath(path.expanduser()))
    if lexical.exists() or lexical.is_symlink():
        raise DataBoundaryError(f"private destination already exists; choose a new path: {path}")
    return resolved


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def manifest_sha(manifest: dict) -> str:
    return _sha(json.dumps({k: v for k, v in manifest.items() if k != "sha256"},
                           sort_keys=True, separators=(",", ":")).encode())


def _relative(name: str) -> Path:
    if not isinstance(name, str):
        raise DataBoundaryError("manifest file names must be strings")
    p = PurePosixPath(name)
    if not name or p.is_absolute() or ".." in p.parts or str(p) != name or "\\" in name:
        raise DataBoundaryError(f"unsafe manifest file name: {name!r}")
    return Path(name)


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DataBoundaryError(f"manifest missing or unreadable: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise DataBoundaryError(f"manifest must be an object: {path}")
    return value


def _write_bundle(destination: Path, files: dict[str, bytes], metadata: dict) -> dict:
    destination = private_destination(destination)
    manifest = {**metadata, "files": {
        name: {"sha256": _sha(data), "bytes": len(data)} for name, data in sorted(files.items())}}
    manifest["sha256"] = manifest_sha(manifest)
    destination.mkdir(parents=True, mode=0o700)
    for name, data in files.items():
        target = destination / _relative(name)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with target.open("xb") as fh:
            os.chmod(target, 0o600)
            fh.write(data)
    # Publish the manifest last. An interrupted export cannot load as complete.
    with (destination / "manifest.json").open("x", encoding="utf-8") as fh:
        os.chmod(destination / "manifest.json", 0o600)
        fh.write(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    if verify_files(destination) != manifest:
        raise DataBoundaryError(f"export changed during verification: {destination}; choose a new destination")
    return manifest


def verify_files(root: Path) -> dict:
    root = root.expanduser().resolve()
    manifest = _read_json(root / "manifest.json")
    if manifest.get("sha256") != manifest_sha(manifest):
        raise DataBoundaryError(f"manifest sha256 mismatch: {root}")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise DataBoundaryError(f"manifest files must be a non-empty object: {root}")
    expected = {"manifest.json"}
    for name, entry in files.items():
        path = root / _relative(name)
        expected.add(name)
        if any(p.is_symlink() for p in (path, *path.parents) if p != root and p.is_relative_to(root)):
            raise DataBoundaryError(f"dataset member is a symlink: {name}")
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise DataBoundaryError(f"dataset file missing or unreadable: {name}: {exc}") from exc
        if not isinstance(entry, dict) or entry != {"sha256": _sha(data), "bytes": len(data)}:
            raise DataBoundaryError(f"dataset file sha256/size mismatch: {name}")
    actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() or p.is_symlink()}
    if actual != expected:
        raise DataBoundaryError(f"dataset has unlisted or missing files: {sorted(actual ^ expected)}")
    return manifest


def _validate_payload(corpus: Path, qrels: Path, labels: Path) -> None:
    from .evals.retrieval import load_corpus, load_qrels, RetrievalEvalError
    from .evals.self_eval import load_labeled, LabeledDataError
    try:
        docs, _ = load_corpus(corpus)
        judgments = load_qrels(qrels, corpus=docs)
        labeled = load_labeled(labels)
        if not docs or not judgments.judgments:
            raise DataBoundaryError("dataset must contain documents and judgments")
        if len({x["id"] for x in labeled}) != len(labeled):
            raise DataBoundaryError("dataset has duplicate label ids")
    except (RetrievalEvalError, LabeledDataError, yaml.YAMLError, ValueError, TypeError, KeyError) as exc:
        raise DataBoundaryError(f"dataset payload invalid: {exc}") from exc


def load_dataset(root: Path, *, expected: Path | None = None, kind: str | None = None) -> dict:
    """Verify every member, version, and optional trusted manifest before scoring."""
    root = root.expanduser().resolve()
    manifest = verify_files(root)
    versions = manifest.get("schema_versions")
    if versions != SCHEMA_VERSIONS or any(type(v) is not int for v in versions.values()):
        raise DataBoundaryError(f"unsupported dataset schema versions: {root}; expected {SCHEMA_VERSIONS}")
    if not isinstance(manifest.get("kind"), str) or manifest["kind"] not in {"private", "synthetic"}:
        raise DataBoundaryError(f"unknown dataset kind: {root}")
    if kind is not None and manifest["kind"] != kind:
        raise DataBoundaryError(f"expected {kind} dataset, got {manifest['kind']}; select synthetic explicitly")
    if not isinstance(manifest.get("dataset_id"), str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", manifest["dataset_id"]):
        raise DataBoundaryError("dataset_id must be a stable lowercase name; avoid private identifiers")
    if type(manifest.get("version")) is not int or manifest["version"] < 1:
        raise DataBoundaryError("dataset version must be a positive integer")
    names = set(manifest["files"])
    required = {"retrieval/corpus.jsonl", "retrieval/qrels.yaml"}
    labels = {n for n in names if re.fullmatch(r"labeled/[^/]+\.yaml", n)}
    if not labels or names != required | labels:
        raise DataBoundaryError("dataset must contain corpus, qrels, and labels together, with no extra files")
    if expected is not None:
        pin = _read_json(expected)
        if pin != manifest:
            raise DataBoundaryError(f"dataset does not match the pinned manifest: {expected}")
    _validate_payload(root / "retrieval/corpus.jsonl", root / "retrieval/qrels.yaml", root / "labeled")
    return manifest


def freeze_dataset(corpus: Path, qrels: Path, labels: Path, destination: Path,
                   *, dataset_id: str, version: int) -> dict:
    private_destination(destination)  # Refuse unsafe targets before reading private input.
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", dataset_id) or type(version) is not int or version < 1:
        raise DataBoundaryError("use a stable lowercase dataset id and positive integer version")
    _validate_payload(corpus, qrels, labels)
    label_files = sorted(labels.iterdir())
    if any(not p.is_file() or p.suffix != ".yaml" or p.is_symlink() for p in label_files):
        raise DataBoundaryError("label directory must contain only regular .yaml files; no files are silently omitted")
    files = {"retrieval/corpus.jsonl": corpus.read_bytes(), "retrieval/qrels.yaml": qrels.read_bytes()}
    files.update({f"labeled/{p.name}": p.read_bytes() for p in label_files})
    manifest = _write_bundle(destination, files, {
        "dataset_id": dataset_id, "version": version, "kind": "private", "schema_versions": dict(SCHEMA_VERSIONS)})
    load_dataset(destination)  # Verify the copied payload, not just the sources.
    return manifest


def backup_checkout(checkout: Path, destination: Path) -> dict:
    """Back up all tracked worktree bytes before a migration. No Git mutation."""
    private_destination(destination)
    checkout = checkout.resolve()
    names = subprocess.check_output(["git", "ls-files", "-z"], cwd=checkout).decode().split("\0")
    files = {}
    for name in filter(None, names):
        path = checkout / _relative(name)
        if path.is_symlink() or not path.is_file():
            raise DataBoundaryError(f"tracked path is missing or not a regular file: {name}")
        files[f"snapshot/{name}"] = path.read_bytes()
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout, text=True).strip()
    manifest = _write_bundle(destination, files, {"kind": "checkout-backup", "schema_version": 1, "revision": revision})
    # Detect concurrent edits: every source must still match the verified backup.
    for name, data in files.items():
        if (checkout / name.removeprefix("snapshot/")).read_bytes() != data:
            raise DataBoundaryError(f"source changed during backup: {name}; make a new backup")
    return manifest


def backup_rebuild_rows(payload: dict, destination: Path) -> dict:
    """Freeze preserved operational rows before a destructive state rebuild.

    Schema version 1 describes the backup envelope. The row dictionaries retain
    the columns returned by the source database; this is not a restore operation.
    """
    data = (json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    return _write_bundle(destination, {"preserved.json": data},
                         {"kind": "rebuild-backup", "schema_version": 1})


def verify_backup(root: Path) -> dict:
    """Verify a supported checkout or rebuild backup, including every member."""
    manifest = verify_files(root)
    if (not isinstance(manifest.get("kind"), str)
            or manifest["kind"] not in {"checkout-backup", "rebuild-backup"}
            or type(manifest.get("schema_version")) is not int
            or manifest["schema_version"] != 1):
        raise DataBoundaryError("unsupported backup kind or schema version")
    if manifest["kind"] == "rebuild-backup" and set(manifest["files"]) != {"preserved.json"}:
        raise DataBoundaryError("rebuild backup requires exactly preserved.json")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    freeze = commands.add_parser("freeze", help="freeze corpus, qrels, and labels together outside Git")
    for name in ("corpus", "qrels", "labels", "destination"):
        freeze.add_argument(f"--{name}", type=Path, required=True)
    freeze.add_argument("--dataset-id", required=True)
    freeze.add_argument("--version", type=int, required=True)
    verify = commands.add_parser("verify", help="verify a complete frozen dataset")
    verify.add_argument("dataset", type=Path)
    verify.add_argument("--manifest", type=Path)
    backup = commands.add_parser("backup-checkout", help="copy and verify every tracked file before migration")
    backup.add_argument("--checkout", type=Path, default=Path.cwd())
    backup.add_argument("--destination", type=Path, required=True)
    check = commands.add_parser("verify-backup", help="verify every byte in a checkout or rebuild backup")
    check.add_argument("backup", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "freeze":
            result = freeze_dataset(args.corpus, args.qrels, args.labels, args.destination,
                                    dataset_id=args.dataset_id, version=args.version)
        elif args.command == "verify":
            result = load_dataset(args.dataset, expected=args.manifest)
        elif args.command == "backup-checkout":
            result = backup_checkout(args.checkout, args.destination)
        else:
            result = verify_backup(args.backup)
        print(json.dumps({"verified": True, "files": len(result["files"]), "sha256": result["sha256"]}))
        return 0
    except (DataBoundaryError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
