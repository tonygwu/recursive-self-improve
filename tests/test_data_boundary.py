"""Drive the data boundary through the public CLI and on-disk artifacts."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from self_improve import cli
from self_improve.config import Config
from self_improve.evals import retrieval as R

ROOT = Path(__file__).resolve().parents[1]


def test_private_project_exclusions_load_from_config_without_entering_public_defaults(tmp_path):
    from self_improve.config import load_config
    from self_improve.sources.claude_code import ClaudeCodeSource
    root = tmp_path / "projects"
    for name in ("invented-excluded-project", "invented-included-project"):
        folder = root / name
        folder.mkdir(parents=True)
        (folder / "session.jsonl").write_text("{}\n")
    public = Config(claude_projects_dir=str(root))
    assert len(list(ClaudeCodeSource(public).discover())) == 2
    private = tmp_path / "private-config.toml"
    private.write_text(f"claude_projects_dir = {json.dumps(str(root))}\n"
                      'denylist_substrings = ["invented-excluded-project"]\n')
    selected = list(ClaudeCodeSource(load_config(private)).discover())
    assert len(selected) == 1
    assert selected[0].project_slug == "invented-included-project"


def test_refresh_requires_destination_before_reading_history(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid export read personal configuration or state")
    monkeypatch.setattr(cli, "load_config", forbidden)
    assert cli.main(["eval-retrieval", "--refresh-corpus"]) == 2
    assert "--private-destination" in capsys.readouterr().err


def test_snapshot_refuses_tracked_path_before_reading_database(monkeypatch):
    from self_improve import search
    def forbidden(*args, **kwargs):
        pytest.fail("export opened the DB before validating its destination")
    monkeypatch.setattr(search, "open_store_readonly", forbidden)
    with pytest.raises(R.RetrievalEvalError, match="checkout"):
        R.snapshot_corpus(Config(), [], ROOT / "evals/retrieval/corpus.jsonl")


def _freeze(tmp_path):
    from self_improve.data_boundary import SYNTHETIC_DATASET, freeze_dataset
    root = SYNTHETIC_DATASET
    target = tmp_path / "private" / "release-1"
    manifest = freeze_dataset(root / "retrieval/corpus.jsonl", root / "retrieval/qrels.yaml",
        root / "labeled", target, dataset_id="test-invented-private-export", version=1)
    return target, manifest


def test_private_export_reload_preserves_all_bytes_and_identity(tmp_path):
    from self_improve.data_boundary import SYNTHETIC_DATASET, load_dataset
    target, manifest = _freeze(tmp_path)
    assert load_dataset(target, kind="private") == manifest
    assert manifest["schema_versions"] == {"bundle": 1, "corpus": 1, "qrels": 1, "labels": 1}
    assert manifest["dataset_id"] == "test-invented-private-export"
    for name in manifest["files"]:
        assert (target / name).read_bytes() == (SYNTHETIC_DATASET / name).read_bytes()
    assert target.stat().st_mode & 0o077 == 0
    assert (target / "retrieval/corpus.jsonl").stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("name", ["retrieval/corpus.jsonl", "retrieval/qrels.yaml", "labeled/retry.yaml"])
@pytest.mark.parametrize("damage", ["edit", "missing"])
def test_every_private_member_is_required_and_checksummed(tmp_path, name, damage):
    from self_improve.data_boundary import DataBoundaryError, load_dataset
    target, _ = _freeze(tmp_path)
    if damage == "edit":
        with (target / name).open("ab") as f:
            f.write(b"\nchanged\n")
    else:
        (target / name).unlink()
    with pytest.raises(DataBoundaryError, match="mismatch|missing"):
        load_dataset(target)


def test_schema_version_and_expected_release_pin_are_enforced(tmp_path):
    from self_improve.data_boundary import DataBoundaryError, load_dataset, manifest_sha
    target, manifest = _freeze(tmp_path)
    pin = tmp_path / "pin.json"
    pin.write_text(json.dumps(manifest))
    manifest["schema_versions"]["qrels"] = 9
    manifest["sha256"] = manifest_sha(manifest)
    (target / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(DataBoundaryError, match="schema versions"):
        load_dataset(target)
    manifest["schema_versions"]["qrels"] = 1
    manifest["version"] = 2
    manifest["sha256"] = manifest_sha(manifest)
    (target / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(DataBoundaryError, match="pinned manifest"):
        load_dataset(target, expected=pin)


def test_freezing_mismatched_qrels_refuses_before_creating_bundle(tmp_path):
    import yaml
    from self_improve.data_boundary import DataBoundaryError, SYNTHETIC_DATASET, freeze_dataset
    root = SYNTHETIC_DATASET
    qrels = yaml.safe_load((root / "retrieval/qrels.yaml").read_text())
    doc = next(iter(qrels["documents"].values()))
    doc["text"] = "Different evidence cannot inherit an old judgment."
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump(qrels))
    dest = tmp_path / "new-release"
    with pytest.raises(DataBoundaryError, match="edited without re-keying"):
        freeze_dataset(root / "retrieval/corpus.jsonl", bad, root / "labeled", dest,
                       dataset_id="test-mismatch", version=1)
    assert not dest.exists()


@pytest.mark.parametrize("alias", ["direct", "ignored", "symlink-in", "symlink-out", "worktree"])
def test_exports_refuse_all_checkout_paths(tmp_path, alias):
    from self_improve.data_boundary import DataBoundaryError, private_destination
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    if alias == "worktree":
        (checkout / ".git").write_text("gitdir: /unused/example")
    else:
        subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    tracked = checkout / "corpus.jsonl"
    tracked.write_text("tracked fixture")
    subprocess.run(["git", "-C", str(checkout), "add", "corpus.jsonl"], check=True) if alias != "worktree" else None
    dest = tracked
    if alias == "ignored":
        (checkout / ".gitignore").write_text("ignored/\n")
        dest = checkout / "ignored" / "data.jsonl"
    if alias == "symlink-in":
        (tmp_path / "alias").symlink_to(checkout, target_is_directory=True)
        dest = tmp_path / "alias" / "data.jsonl"
    if alias == "symlink-out":
        (checkout / "alias").symlink_to(tmp_path, target_is_directory=True)
        dest = checkout / "alias" / "data.jsonl"
    with pytest.raises(DataBoundaryError, match="checkout"):
        private_destination(dest)
    assert tracked.read_text() == "tracked fixture"
    assert not (tmp_path / "data.jsonl").exists()


def test_export_cannot_overwrite_an_existing_file_or_hardlink(tmp_path):
    from self_improve.data_boundary import DataBoundaryError, private_destination, freeze_dataset, SYNTHETIC_DATASET
    existing = tmp_path / "existing"
    existing.write_text("keep")
    link = tmp_path / "hardlink"
    link.hardlink_to(existing)
    with pytest.raises(DataBoundaryError, match="already exists"):
        private_destination(link)
    target, _ = _freeze(tmp_path)
    root = SYNTHETIC_DATASET
    with pytest.raises(DataBoundaryError, match="already exists"):
        freeze_dataset(root / "retrieval/corpus.jsonl", root / "retrieval/qrels.yaml", root / "labeled",
                       target, dataset_id="another-version", version=2)
    assert existing.read_text() == "keep"


def test_manifest_cannot_escape_bundle_or_hide_unlisted_labels(tmp_path):
    from self_improve.data_boundary import DataBoundaryError, load_dataset, manifest_sha
    target, manifest = _freeze(tmp_path)
    extra = target / "labeled/extra.yaml"
    extra.write_text("unlisted source data")
    with pytest.raises(DataBoundaryError, match="unlisted"):
        load_dataset(target)
    extra.unlink()
    manifest["files"]["../outside"] = {"bytes": 0, "sha256": "0" * 64}
    manifest["sha256"] = manifest_sha(manifest)
    (target / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(DataBoundaryError, match="unsafe manifest"):
        load_dataset(target)


def test_missing_real_dataset_never_falls_back_to_demo(tmp_path, capsys):
    assert cli.main(["eval-retrieval", "--dataset", str(tmp_path / "absent")]) == 2
    output = capsys.readouterr()
    assert "manifest missing" in output.err
    assert not output.out
    assert cli.main(["self-eval", "--dataset", str(tmp_path / "absent")]) == 2
    assert "manifest missing" in capsys.readouterr().err


def test_synthetic_selection_is_explicit_and_runs_without_personal_history(monkeypatch, capsys):
    from self_improve.data_boundary import SYNTHETIC_DATASET
    def forbidden(*args, **kwargs):
        pytest.fail("synthetic evaluation read personal configuration")
    monkeypatch.setattr(cli, "load_config", forbidden)
    assert cli.main(["eval-retrieval"]) == 2
    assert "--synthetic" in capsys.readouterr().err
    assert cli.main(["eval-retrieval", "--dataset", str(SYNTHETIC_DATASET)]) == 2
    assert "expected private" in capsys.readouterr().err
    assert cli.main(["eval-retrieval", "--synthetic", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["dataset"]["kind"] == "synthetic"
    assert result["corpus_meta"]["docs"] == 6
    assert result["qrel_counts"]["total"] == 6
    assert "SYNTHETIC DEMO" in R.render_text_report(result)
    assert result["gold_detail"]


def test_refresh_cli_exports_and_leaves_database_bytes_unchanged(tmp_path, capsys):
    from self_improve.store import Store
    state = tmp_path / "state"
    store = Store(state / "state.db")
    store.insert("learnings", {"id": "invented-learning", "rule_text": "Retry a timeout at most three times.",
                               "created_at": "2026-01-01T00:00:00Z"})
    store.close()
    before = (state / "state.db").read_bytes()
    instructions = tmp_path / "instructions.md"
    instructions.write_text("- A missing configuration key must raise an error.\n")
    config = tmp_path / "config.toml"
    config.write_text(f'state_dir = "{state}"\nglobal_claude_md = "{instructions}"\n')
    target = tmp_path / "exports/corpus.jsonl"
    assert cli.main(["--config", str(config), "eval-retrieval", "--refresh-corpus",
                     "--private-destination", str(target)]) == 0
    assert "Private corpus exported" in capsys.readouterr().out
    docs, meta = R.load_corpus(target)
    assert meta["learnings"] == 1 and meta["rule_units"] == 1
    assert any(d.observed_as == "invented-learning" for d in docs)
    # SQLite mode=ro may create coordination sidecars for a WAL database.
    # The database bytes and logical contents must remain unchanged.
    assert (state / "state.db").read_bytes() == before
    ro = Store(state / "state.db", read_only=True)
    try:
        assert ro.query_one("SELECT COUNT(*) AS n FROM learnings")["n"] == 1
        assert ro.query_one("SELECT COUNT(*) AS n FROM runs")["n"] == 0
    finally:
        ro.close()


def test_checkout_backup_verifies_bytes_and_detects_corruption(tmp_path):
    from self_improve.data_boundary import backup_checkout, verify_files, DataBoundaryError
    checkout = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    (checkout / "evidence.txt").write_text("invented private evidence")
    subprocess.run(["git", "-C", str(checkout), "add", "evidence.txt"], check=True)
    subprocess.run(["git", "-C", str(checkout), "-c", "user.name=Example", "-c", "user.email=example@example.org",
                    "commit", "-qm", "fixture"], check=True)
    destination = tmp_path / "backup"
    manifest = backup_checkout(checkout, destination)
    assert verify_files(destination) == manifest
    assert (destination / "snapshot/evidence.txt").read_bytes() == (checkout / "evidence.txt").read_bytes()
    (destination / "snapshot/evidence.txt").write_text("changed")
    with pytest.raises(DataBoundaryError, match="mismatch"):
        verify_files(destination)


def test_dashboard_demo_seeds_without_personal_history(tmp_path):
    from examples.dashboard_demo import seed
    from self_improve.dashboard.app import create_app
    from fastapi.testclient import TestClient
    cfg = seed(tmp_path)
    with TestClient(create_app(cfg)) as client:
        response = client.get("/api/review-queue")
        assert response.status_code == 200
        assert response.json()["count"] == 4


@pytest.mark.parametrize("detection", [False, True])
def test_self_eval_uses_selected_labels_and_keeps_runtime_readonly(tmp_path, capsys, detection):
    from self_improve.store import Store
    target, manifest = _freeze(tmp_path)
    state = tmp_path / "state"
    store = Store(state / "state.db")
    store.close()
    before = (state / "state.db").read_bytes()
    config = tmp_path / "config.toml"
    config.write_text(f'state_dir = "{state}"\n')
    args = ["--config", str(config), "self-eval", "--dataset", str(target)]
    if detection:
        args.append("--detection-only")
    assert cli.main(args) == (1 if detection else 0)
    output = capsys.readouterr().out
    assert manifest["sha256"] in output
    assert "synthetic-retry" in output
    assert (state / "state.db").read_bytes() == before


def test_malformed_yaml_fails_clearly_without_publishing_a_bundle(tmp_path, capsys):
    from self_improve.data_boundary import main, SYNTHETIC_DATASET
    broken = tmp_path / "qrels.yaml"
    broken.write_text("judgments: [\n")
    root = SYNTHETIC_DATASET
    dest = tmp_path / "bad-export"
    assert main(["freeze", "--corpus", str(root / "retrieval/corpus.jsonl"),
        "--qrels", str(broken), "--labels", str(root / "labeled"),
        "--destination", str(dest), "--dataset-id", "test-bad", "--version", "1"]) == 2
    assert "dataset payload invalid" in capsys.readouterr().err
    assert not dest.exists()


def test_rebuild_refuses_checkout_before_loading_configuration(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid rebuild export reached personal configuration")
    monkeypatch.setattr(cli, "load_config", forbidden)
    assert cli.main(["rebuild-state", "--export", str(ROOT / "backup"), "--dry-run"]) == 2
    assert "checkout" in capsys.readouterr().err


@pytest.mark.parametrize("destination", ["checkout", "existing", "hardlink"])
def test_rebuild_refuses_unsafe_export_before_reading_state(tmp_path, destination):
    from self_improve.data_boundary import DataBoundaryError
    from self_improve.rebuild import rebuild_state
    sentinel = tmp_path / "keep.json"
    sentinel.write_text("keep this existing evidence")
    if destination == "checkout":
        checkout = tmp_path / "repo"
        subprocess.run(["git", "init", "-q", str(checkout)], check=True)
        target = checkout / "backup"
    elif destination == "hardlink":
        target = tmp_path / "alias.json"
        target.hardlink_to(sentinel)
    else:
        target = sentinel

    class UnreadableStore:
        def query(self, *args, **kwargs):
            pytest.fail("invalid rebuild export read the database")

    with pytest.raises(DataBoundaryError, match="checkout|already exists"):
        rebuild_state(UnreadableStore(), export_path=target)
    assert sentinel.read_text() == "keep this existing evidence"


@pytest.fixture
def rebuild_store(tmp_path):
    from self_improve.store import Store
    store = Store(tmp_path / "state" / "state.db")
    alive = tmp_path / "available.jsonl"
    alive.write_text("{}\n")
    for name, path in (("available", alive), ("missing", tmp_path / "missing.jsonl")):
        store.upsert_session({
            "file_path": str(path), "source": "codex", "session_id": name,
            "project_path": "/invented/project", "headless": 0, "is_subagent": 0,
            "first_ts": "", "last_ts": "", "mtime": 1, "file_size": 3,
            "bytes_scanned": 3, "lines_scanned": 1, "malformed_lines": 0,
            "status": "ok", "error": "", "last_scanned_at": "2026-01-01T00:00:00Z",
        })
        store.insert_incident({
            "id": name, "session_file": str(path), "session_id": name,
            "signal_type": "correction", "window": [{"text": "invented " + name}],
        })
    store.commit()
    try:
        yield store
    finally:
        store.close()


def test_rebuild_backup_preserves_rows_and_reloads_with_integrity(rebuild_store, tmp_path, capsys):
    from self_improve import data_boundary as boundary
    from self_improve.rebuild import rebuild_state
    store = rebuild_store
    sessions = [r for r in store.query("SELECT * FROM sessions") if r["session_id"] == "missing"]
    incidents = [r for r in store.query("SELECT * FROM incidents") if r["id"] == "missing"]
    target = tmp_path / "private-backup"
    stats = rebuild_state(store, export_path=target)
    manifest = boundary.verify_files(target)
    payload = json.loads((target / "preserved.json").read_text())
    assert manifest["kind"] == "rebuild-backup"
    assert manifest["schema_version"] == 1
    assert manifest["sha256"] == stats["backup_sha256"]
    assert payload["sessions"] == sessions
    assert payload["incidents"] == incidents
    assert store.query("SELECT * FROM sessions") == sessions
    assert store.query("SELECT * FROM incidents") == incidents
    assert target.stat().st_mode & 0o077 == 0
    assert (target / "preserved.json").stat().st_mode & 0o077 == 0
    assert boundary.main(["verify-backup", str(target)]) == 0
    assert json.loads(capsys.readouterr().out)["sha256"] == manifest["sha256"]
    (target / "preserved.json").write_text("changed")
    assert boundary.main(["verify-backup", str(target)]) == 2
    assert "mismatch" in capsys.readouterr().err


def test_rebuild_verifies_backup_before_any_deletion(rebuild_store, tmp_path, monkeypatch):
    from self_improve import data_boundary as boundary
    from self_improve.rebuild import rebuild_state
    before = list(rebuild_store.conn.iterdump())
    checked = []

    def corrupt_then_verify(path):
        checked.append(path)
        assert list(rebuild_store.conn.iterdump()) == before
        (path / "preserved.json").write_text("damaged before verification")
        return original(path)

    original = boundary.verify_files
    monkeypatch.setattr(boundary, "verify_files", corrupt_then_verify)
    with pytest.raises(boundary.DataBoundaryError, match="mismatch"):
        rebuild_state(rebuild_store, export_path=tmp_path / "broken-backup")
    assert len(checked) == 1
    assert list(rebuild_store.conn.iterdump()) == before
    assert not rebuild_store.conn.in_transaction


def test_rebuild_rolls_back_all_deletions_on_later_failure(rebuild_store, tmp_path):
    import sqlite3
    from self_improve.rebuild import rebuild_state
    store = rebuild_store
    # Fail after incidents would have been deleted, proving earlier changes
    # cannot be committed by Store.close() after the exception.
    store.conn.execute("CREATE TRIGGER reject_session_delete BEFORE DELETE ON sessions "
                       "BEGIN SELECT RAISE(ABORT, 'invented delete failure'); END")
    store.commit()
    before = list(store.conn.iterdump())
    with pytest.raises(sqlite3.IntegrityError, match="invented delete failure"):
        rebuild_state(store, export_path=tmp_path / "retained-backup")
    assert list(store.conn.iterdump()) == before
    assert not store.conn.in_transaction


def test_export_verification_binds_the_intended_manifest(tmp_path, monkeypatch):
    import hashlib
    from self_improve import data_boundary as boundary
    original = boundary.verify_files
    changed = []

    def replace_and_rehash(root):
        if not changed:
            changed.append(root)
            member = root / "retrieval/corpus.jsonl"
            replacement = member.read_bytes() + b"\n"
            member.write_bytes(replacement)
            manifest_path = root / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["files"]["retrieval/corpus.jsonl"] = {
                "bytes": len(replacement), "sha256": hashlib.sha256(replacement).hexdigest(),
            }
            manifest["sha256"] = boundary.manifest_sha(manifest)
            manifest_path.write_text(json.dumps(manifest))
        return original(root)

    monkeypatch.setattr(boundary, "verify_files", replace_and_rehash)
    with pytest.raises(boundary.DataBoundaryError, match="changed during"):
        _freeze(tmp_path)


@pytest.mark.parametrize("change", ["version", "boolean-version", "kind", "list-kind", "member"])
def test_rebuild_backup_verifier_enforces_its_format(tmp_path, capsys, change):
    from self_improve import data_boundary as boundary
    target = tmp_path / "backup"
    manifest = boundary.backup_rebuild_rows({
        "exported_at": "2026-01-01T00:00:00Z", "reason": "invented rebuild",
        "note": "invented empty archive", "sessions": [], "incidents": [],
    }, target)
    if change == "version":
        manifest["schema_version"] = 2
    elif change == "boolean-version":
        manifest["schema_version"] = True
    elif change == "kind":
        manifest["kind"] = "unknown-backup"
    elif change == "list-kind":
        manifest["kind"] = ["rebuild-backup"]
    else:
        (target / "preserved.json").rename(target / "other.json")
        manifest["files"]["other.json"] = manifest["files"].pop("preserved.json")
    manifest["sha256"] = boundary.manifest_sha(manifest)
    (target / "manifest.json").write_text(json.dumps(manifest))
    assert boundary.main(["verify-backup", str(target)]) == 2
    expected = "requires exactly preserved.json" if change == "member" else "unsupported backup"
    assert expected in capsys.readouterr().err


def test_rebuild_rolls_back_when_the_transaction_commit_fails(rebuild_store, tmp_path):
    import sqlite3
    from self_improve.rebuild import rebuild_state
    store = rebuild_store
    source = store.query_one("SELECT file_path FROM sessions WHERE session_id = 'available'")
    store.conn.execute("CREATE TABLE retained_reference (session_file TEXT REFERENCES sessions(file_path) "
                       "DEFERRABLE INITIALLY DEFERRED)")
    store.insert("retained_reference", {"session_file": source["file_path"]})
    store.commit()
    before = list(store.conn.iterdump())
    try:
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            rebuild_state(store, export_path=tmp_path / "retained-backup")
        assert list(store.conn.iterdump()) == before
        assert not store.conn.in_transaction
    finally:
        # Clean up the disposable fixture even while proving a broken commit path.
        store.conn.rollback()


def test_dataset_kind_shape_fails_with_a_boundary_error(tmp_path):
    from self_improve import data_boundary as boundary
    target, manifest = _freeze(tmp_path)
    manifest["kind"] = ["private"]
    manifest["sha256"] = boundary.manifest_sha(manifest)
    (target / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(boundary.DataBoundaryError, match="unknown dataset kind"):
        boundary.load_dataset(target)
