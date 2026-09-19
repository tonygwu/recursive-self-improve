"""Scan observations: versions, transactions, failure history, readers, retention.

Acceptance cases 6, 10, 11, and 12 of docs/dashboard-parity/PARALLEL_WORK.md.
Everything runs against temporary Stores, invented JSONL transcripts, and
temporary Git repositories. Project lookup uses the remote URL (no `gh`).
The shared helpers here are imported by test_scan_occurrences.py and
test_scan_exposure.py.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from self_improve import filter_incidents, rebuild
from self_improve import scan_observations as so
from self_improve.config import Config
from self_improve.data_boundary import verify_backup
from self_improve.scan import mark_for_rescan, scan_all
from self_improve.sources.claude_code import ClaudeCodeSource
from self_improve.sources.codex import CodexSource
from self_improve.store import Store

SID = "11111111-1111-4111-8111-111111111111"
SID_B = "22222222-2222-4222-8222-222222222222"
REMOTE = "git@github.com:example/alpha.git"
PROJECT = "remote:github.com/example/alpha"
START, END = "2026-08-01T00:00:00Z", "2026-09-01T00:00:00Z"


# ---------------------------------------------------------------------------
# Shared fixture helpers.
# ---------------------------------------------------------------------------


def make_repo(path: Path, remote: str = REMOTE) -> str:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "remote", "add", "origin", remote], check=True)
    return str(path)


@dataclass
class Env:
    tmp: Path
    cfg: Config
    store: Store
    claude_dir: Path
    codex_dir: Path
    repo: str


def make_env(tmp_path: Path, **overrides) -> Env:
    claude = tmp_path / "claude-projects"
    codex = tmp_path / "codex-sessions"
    claude.mkdir()
    codex.mkdir()
    repo = make_repo(tmp_path / "work" / "alpha")
    settings = {
        "claude_projects_dir": str(claude),
        "codex_sessions_dir": str(codex),
        "codex_archived_dir": str(tmp_path / "codex-archived"),
        "state_dir": str(tmp_path / "state"),
        "project_identity_use_gh": False,
        # pytest's tmp_path sits under /var/folders on macOS, which the default
        # denylist excludes. Denial is tested explicitly with its own marker.
        "denylist_substrings": ("denied-tree",),
    }
    settings.update(overrides)
    cfg = Config(**settings)
    return Env(tmp_path, cfg, Store(tmp_path / "state" / "state.db"), claude, codex, repo)


def ts(minute: int, day: int = 10, month: int = 8) -> str:
    return f"2026-{month:02d}-{day:02d}T10:{minute:02d}:00Z"


def c_user(text: str, when: str, cwd: str, sid: str = SID, **extra) -> dict:
    return {"type": "user", "timestamp": when, "cwd": cwd, "sessionId": sid,
            "entrypoint": "cli", "message": {"role": "user", "content": text}, **extra}


def c_tool(when: str, cwd: str, sid: str = SID, tool_id: str = "t1") -> dict:
    return {"type": "assistant", "timestamp": when, "cwd": cwd, "sessionId": sid,
            "entrypoint": "cli", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": tool_id, "name": "Bash", "input": {"command": "ls"}}]}}


def c_result(text: str, when: str, cwd: str, sid: str = SID, is_error: bool = False,
             tool_id: str = "t1") -> dict:
    return {"type": "user", "timestamp": when, "cwd": cwd, "sessionId": sid, "entrypoint": "cli",
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tool_id, "content": text, "is_error": is_error}]}}


def jsonl(records: list[dict]) -> bytes:
    return "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records).encode()


def correction_session(cwd: str, day: int = 10) -> list[dict]:
    """Four eligible lines and exactly one correction occurrence."""
    return [
        c_user("please list the files", ts(0, day), cwd),
        c_tool(ts(1, day), cwd),
        c_result("a.txt", ts(2, day), cwd),
        c_user("no, that's wrong", ts(3, day), cwd),
    ]


def write_claude(env: Env, records: list[dict], name: str = SID, slug: str = "-work-alpha") -> Path:
    directory = env.claude_dir / slug
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.jsonl"
    path.write_bytes(jsonl(records))
    return path


def append(path: Path, data: bytes) -> None:
    with open(path, "ab") as fh:
        fh.write(data)


def scan(env: Env, run_id: str, cfg: Config | None = None, **kwargs):
    cfg = cfg or env.cfg
    return scan_all(env.store, cfg, [ClaudeCodeSource(cfg), CodexSource(cfg)], run_id, **kwargs)


def count(store: Store, sql: str, params: tuple = ()) -> int:
    return store.query_one(f"SELECT COUNT(*) AS n FROM ({sql})", params)["n"]


def exposure(store: Store, **kwargs) -> dict:
    return so.exposure_window(store, project_key=kwargs.pop("project_key", PROJECT),
                              start=kwargs.pop("start", START), end=kwargs.pop("end", END), **kwargs)


# ---------------------------------------------------------------------------
# Case 6: versions partition history.
# ---------------------------------------------------------------------------


def test_real_modules_are_identifiable_and_keys_ignore_machine_paths(tmp_path):
    env = make_env(tmp_path)
    first = so.detector_manifest(env.cfg, ClaudeCodeSource(env.cfg), filter_incidents.detect)
    assert first["identifiable"] is True, (first["detector"]["key"], first["parser"]["key"])
    moved = dataclasses.replace(env.cfg, claude_projects_dir="/elsewhere", state_dir="/other")
    second = so.detector_manifest(moved, ClaudeCodeSource(moved), filter_incidents.detect)
    assert second["compatibility_key"] == first["compatibility_key"]
    codex = so.detector_manifest(env.cfg, CodexSource(env.cfg), filter_incidents.detect)
    # Both sources share one compatibility key so a mixed project has one rate;
    # the manifest still records which source implementation ran.
    assert codex["compatibility_key"] == first["compatibility_key"]
    assert codex["parser"]["implementation"] != first["parser"]["implementation"]
    assert codex["manifest_id"] != first["manifest_id"]
    assert first["config"]["values"]["correction_max_len"] == env.cfg.correction_max_len


def test_disk_edit_after_import_cannot_relabel_loaded_code(tmp_path, monkeypatch):
    module_path = tmp_path / "identity_probe.py"
    module_path.write_text("LIMIT = 3\n\ndef limit():\n    return LIMIT\n")
    spec = importlib.util.spec_from_file_location("identity_probe", module_path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "identity_probe", module)
    spec.loader.exec_module(module)
    loaded = so.module_identity(module)
    assert loaded["verified"] is True

    module_path.write_text("LIMIT = 4\n\ndef limit():\n    return LIMIT\n")
    edited = so.module_identity(module)
    assert edited["verified"] is False
    assert edited["reason"] == "loaded_code_differs_from_source:LIMIT"
    assert so._component([edited])["key"].startswith(so.UNKNOWN_PREFIX)


def test_config_change_partitions_history_and_unchanged_files_stay_uncovered(tmp_path):
    env = make_env(tmp_path)
    write_claude(env, correction_session(env.repo))
    scan(env, "run-1")
    old = exposure(env.store)
    assert old["computable"] is True and old["occurrences"] == 1 and old["eligible_lines"] == 4

    changed = dataclasses.replace(env.cfg, correction_max_len=1500)
    new_key = so.detector_manifest(changed, ClaudeCodeSource(changed), filter_incidents.detect)[
        "compatibility_key"
    ]
    assert new_key != old["compatibility_key"]
    stats = scan(env, "run-2", cfg=changed)
    assert stats.files_skipped_unchanged == 1
    assert stats.measurement == {"unchanged_uncovered_version": 1}
    assert count(env.store, "SELECT id FROM scan_observations") == 1, "no invented observation"
    uncovered = exposure(env.store, compatibility_key=new_key)
    assert (uncovered["computable"], uncovered["reason"], uncovered["occurrences"]) == (
        False, "uncovered_version", None)

    mark_for_rescan(env.store, changed)
    scan(env, "run-3", cfg=changed)
    pooled = exposure(env.store)
    assert (pooled["computable"], pooled["reason"], pooled["rate_per_100k"]) == (
        False, "incompatible_versions", None)
    assert sorted((g["eligible_lines"], g["occurrences"]) for g in pooled["version_groups"]) == [
        (4, 1), (4, 1)]
    for key in (old["compatibility_key"], new_key):
        separate = exposure(env.store, compatibility_key=key)
        assert separate["computable"] is True and separate["rate_per_100k"] == 25000.0


def test_custom_detector_has_a_named_unknown_version_and_no_rate(tmp_path):
    env = make_env(tmp_path)
    write_claude(env, correction_session(env.repo))
    scan(env, "run-1", detect_fn=lambda events, cfg: filter_incidents.detect(events, cfg))
    result = exposure(env.store)
    assert result["compatibility_key"].startswith(so.UNKNOWN_PREFIX)
    assert (result["computable"], result["reason"], result["rate_per_100k"]) == (
        False, "unknown_version", None)
    assert result["eligible_lines"] == 4, "observed counts are still reported"


# ---------------------------------------------------------------------------
# Case 10: failure rollback, restart, and retained cause.
# ---------------------------------------------------------------------------


def test_failure_after_partial_publish_rolls_back_all_then_restart_publishes_once(tmp_path, monkeypatch):
    env = make_env(tmp_path)
    path = write_claude(env, correction_session(env.repo))
    real = so.record_scan
    seen = {}

    def publish_then_fail(store, **kwargs):
        real(store, **kwargs)
        seen["partial_lines"] = store.query_one("SELECT COUNT(*) AS n FROM scan_lines")["n"]
        raise RuntimeError("injected after partial publish")

    monkeypatch.setattr(so, "record_scan", publish_then_fail)
    stats = scan(env, "run-1")
    assert seen["partial_lines"] == 4, "the failure must follow real inserts"
    assert stats.files_failed == 1 and stats.error_taxonomy == {"measure:RuntimeError": 1}
    assert stats.measurement == {"observation_failures_recorded": 1}
    for table in ("scan_lines", "scan_occurrences", "scan_incident_links", "incidents",
                  "error_fingerprints", "scan_observation_occurrences"):
        assert count(env.store, f"SELECT * FROM {table}") == 0, table
    session = env.store.get_session(str(path))
    assert (session["status"], session["mtime"], session["bytes_scanned"]) == ("error", 0.0, 0)
    failed = env.store.query("SELECT outcome, failure_cause FROM scan_observations")
    assert [row["outcome"] for row in failed] == ["failed"]
    assert "injected after partial publish" in failed[0]["failure_cause"]

    monkeypatch.undo()
    stats = scan(env, "run-2")
    assert stats.files_succeeded == 1 and stats.files_failed == 0
    assert count(env.store, "SELECT * FROM scan_lines") == 4
    assert count(env.store, "SELECT * FROM incidents") == 1
    assert env.store.query("SELECT link_kind FROM scan_incident_links") == [{"link_kind": "produced"}]
    history = so.scan_history(env.store, session_file=str(path))
    assert [r["outcome"] for r in history["records"]] == ["succeeded", "failed"]
    assert "injected" in history["records"][1]["failure_cause"]

    assert scan(env, "run-3").files_skipped_unchanged == 1
    assert count(env.store, "SELECT * FROM scan_lines") == 4


def test_a_failed_reconciliation_keeps_the_old_projection_but_marks_it_stale(tmp_path):
    env = make_env(tmp_path)
    path = write_claude(env, correction_session(env.repo))
    scan(env, "run-1")
    append(path, jsonl([c_tool(ts(8), env.repo, tool_id="t2"), c_user("no, still wrong", ts(9), env.repo)]))

    def broken_window(events, incident, cfg):
        # The window builder is outside the manifest, so this failure is
        # recorded under the same compatibility key as the live projection.
        raise RuntimeError("window refused")

    stats = scan(env, "run-2", build_window_fn=broken_window)
    assert stats.error_taxonomy == {"window:RuntimeError": 1}
    stale = exposure(env.store)
    assert stale["computable"] is True and stale["eligible_lines"] == 4, "old projection intact"
    assert stale["coverage"]["coverage_complete"] is False
    assert stale["coverage"]["counts_by_cause"]["stale_failed_reconciliation_transcripts"] == 1

    scan(env, "run-3")
    fresh = exposure(env.store)
    assert (fresh["eligible_lines"], fresh["occurrences"]) == (6, 2)
    assert fresh["coverage"]["coverage_complete"] is True


# ---------------------------------------------------------------------------
# Writer contract: transactions, idempotency, and validation.
# ---------------------------------------------------------------------------


def _captured_publish(env: Env, monkeypatch) -> dict:
    captured = {}
    real = so.record_scan

    def spy(store, **kwargs):
        captured.update(kwargs)
        return real(store, **kwargs)

    monkeypatch.setattr(so, "record_scan", spy)
    write_claude(env, correction_session(env.repo))
    scan(env, "run-1")
    monkeypatch.undo()
    return captured


def test_writer_requires_the_callers_transaction_and_replays_idempotently(tmp_path, monkeypatch):
    env = make_env(tmp_path)
    publish = _captured_publish(env, monkeypatch)
    with pytest.raises(so.ScanObservationError, match="active write transaction"):
        so.record_scan(env.store, **publish)
    before = count(env.store, "SELECT * FROM scan_lines")
    with env.store.transaction(write=True):
        assert so.record_scan(env.store, **publish) == publish["observation"]["id"]
    assert count(env.store, "SELECT * FROM scan_lines") == before
    assert count(env.store, "SELECT * FROM scan_observations") == 1

    changed = json.loads(json.dumps(publish))
    changed["observation"]["counts"]["events"] += 1
    with pytest.raises(so.ScanObservationError, match="already owned by run run-1"):
        with env.store.transaction(write=True):
            so.record_scan(env.store, **changed)


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda p: p["lines"][1].update(line_no=5), "complete ordered file"),
        (lambda p: p["lines"][0].update(exclusion="unknown_time"), "contradicts its fields"),
        (lambda p: p["lines"][0].update(extra=1), "unknown fields"),
        (lambda p: p["lines"][0].update(headless=0), "must be bool"),
        (lambda p: p["observation"]["manifest"]["config"]["values"].update(correction_max_len=1),
         "config key does not match"),
        (lambda p: p["occurrences"][0].update(trigger_line_no=1), "trigger line"),
        (lambda p: p["occurrences"][0]["incident_links"][0].update(incident_id="missing"),
         "does not exist"),
    ],
)
def test_writer_rejects_inconsistent_input_naming_the_owner(tmp_path, monkeypatch, mutate, message):
    env = make_env(tmp_path)
    publish = json.loads(json.dumps(_captured_publish(env, monkeypatch)))
    publish["observation"]["id"] = "replacement-attempt"
    mutate(publish)
    with pytest.raises(so.ScanObservationError, match=message) as error:
        with env.store.transaction(write=True):
            so.record_scan(env.store, **publish)
    assert "replacement-attempt" in str(error.value)


# ---------------------------------------------------------------------------
# Case 11: readers on copies, cursors, corruption, and missing schema.
# ---------------------------------------------------------------------------


def _two_observations(env: Env) -> Path:
    path = write_claude(env, correction_session(env.repo))
    scan(env, "run-1")
    append(path, jsonl([c_user("thanks", ts(9), env.repo)]))
    scan(env, "run-2")
    return path


def test_readers_work_on_a_read_only_copy_and_never_write(tmp_path):
    env = make_env(tmp_path)
    path = _two_observations(env)
    copy = tmp_path / "copy.db"
    target = sqlite3.connect(copy)
    env.store.conn.backup(target)
    target.close()
    before = copy.read_bytes()
    reader = Store(copy, read_only=True)
    assert so.scan_history(reader, session_file=str(path)) == so.scan_history(env.store, session_file=str(path))
    assert exposure(reader) == exposure(env.store)
    assert exposure(reader)["eligible_lines"] == 5
    reader.conn.close()
    assert copy.read_bytes() == before


def test_history_pages_by_cursor_and_rejects_foreign_or_bad_cursors(tmp_path):
    env = make_env(tmp_path)
    path = _two_observations(env)
    first = so.scan_history(env.store, session_file=str(path), limit=1)
    assert len(first["records"]) == 1 and first["next_cursor"]
    second = so.scan_history(env.store, session_file=str(path), limit=1, cursor=first["next_cursor"])
    assert second["next_cursor"] is None
    assert {first["records"][0]["id"], second["records"][0]["id"]} == {
        r["id"] for r in env.store.query("SELECT id FROM scan_observations")}
    incident = env.store.query_one("SELECT id FROM incidents")["id"]
    with pytest.raises(ValueError, match="different selector"):
        so.scan_history(env.store, incident_id=incident, cursor=first["next_cursor"])
    for bad in ("not-a-cursor", ""):
        with pytest.raises(ValueError, match="cursor"):
            so.scan_history(env.store, session_file=str(path), cursor=bad)
    for limit in (0, 101, True):
        with pytest.raises(ValueError, match="limit"):
            so.scan_history(env.store, session_file=str(path), limit=limit)
    with pytest.raises(ValueError, match="exactly one selector"):
        so.scan_history(env.store, session_file=str(path), incident_id=incident)


def test_incident_history_separates_produced_corroborated_and_legacy(tmp_path):
    env = make_env(tmp_path)
    _two_observations(env)
    incident = env.store.query_one("SELECT id, run_id FROM incidents")
    history = so.scan_history(env.store, incident_id=incident["id"])
    assert history["incident"]["provenance"] == "observed"
    assert [(r["run_id"], r["link_kind"]) for r in history["records"]] == [
        ("run-2", "corroborated"), ("run-1", "produced")]
    env.store.insert_incident({"id": "legacy", "session_file": incident and env.store.query_one(
        "SELECT session_file FROM incidents")["session_file"], "signal_type": "correction", "run_id": "old"})
    env.store.commit()
    legacy = so.scan_history(env.store, incident_id="legacy")
    assert (legacy["computable"], legacy["reason"], legacy["incident"]["provenance"]) == (
        False, "legacy_unknown_provenance", "legacy_unknown")
    assert so.scan_history(env.store, incident_id="nope")["reason"] == "incident_not_found"


@pytest.mark.parametrize(
    "sql, message",
    [
        ("UPDATE scan_observations SET record_json = 'not json'", "record_json is not valid JSON"),
        ("UPDATE scan_observations SET record_json = '[]'", "record_json is not a JSON object"),
        ("UPDATE scan_manifests SET manifest_json = replace(manifest_json, "
         "'\"correction_max_len\":2000', '\"correction_max_len\":7')", "config key does not match"),
    ],
)
def test_corrupt_stored_evidence_fails_naming_its_owner(tmp_path, sql, message):
    env = make_env(tmp_path)
    path = write_claude(env, correction_session(env.repo))
    scan(env, "run-1")
    env.store.conn.execute(sql)
    env.store.commit()
    with pytest.raises(so.ScanObservationError, match=message):
        so.scan_history(env.store, session_file=str(path))


def test_missing_schema_is_a_named_error_not_empty_history(tmp_path):
    env = make_env(tmp_path)
    path = write_claude(env, correction_session(env.repo))
    scan(env, "run-1")
    env.store.conn.execute("DROP TABLE scan_observation_occurrences")
    env.store.commit()
    with pytest.raises(so.ScanObservationError, match="scan_observation_occurrences is unreadable"):
        exposure(env.store)
    with pytest.raises(so.ScanObservationError, match="scan_observation_occurrences is unreadable"):
        so.scan_history(env.store, session_file=str(path))
    for migration in (so.MIGRATION, '0026_scan_incident_links', '0027_scan_run_index', '0030_session_context'):
        env.store.conn.execute("DELETE FROM schema_migrations WHERE name = ?", (migration,))
    env.store.commit()
    with pytest.raises(so.ScanObservationError, match="require migration 0022_scan_observations"):
        exposure(env.store)


def test_reader_arguments_fail_explicitly(tmp_path):
    env = make_env(tmp_path)
    with pytest.raises(ValueError, match="start < end"):
        exposure(env.store, start=END, end=START)
    with pytest.raises(ValueError, match="UTC offset"):
        exposure(env.store, start="2026-08-01T00:00:00")
    with pytest.raises(ValueError, match="unknown signal types"):
        exposure(env.store, signal_types=("typo",))
    assert exposure(env.store)["reason"] == "missing_observations"


# ---------------------------------------------------------------------------
# Case 12: rebuild retention.
# ---------------------------------------------------------------------------


def _live_and_deleted(env: Env) -> tuple[Path, Path]:
    live = write_claude(env, correction_session(env.repo), name=SID)
    gone = write_claude(env, correction_session(env.repo, day=11), name=SID_B)
    scan(env, "run-1")
    gone.unlink()
    return live, gone


def test_rebuild_exports_verified_provenance_then_keeps_orphan_projections(tmp_path):
    env = make_env(tmp_path)
    live, gone = _live_and_deleted(env)
    recorded = env.store.query_one(
        "SELECT transcript_id FROM scan_observations WHERE session_file = ?", (str(gone),))["transcript_id"]

    backup = tmp_path / "private-backup"
    stats = rebuild.rebuild_state(env.store, export_path=backup)
    verify_backup(backup)
    exported = json.loads((backup / "preserved.json").read_text())["scan_observations"]
    assert exported["orphan_transcripts"] == [recorded]
    assert len(exported["observations"]) == 2 and len(exported["incident_links"]) == 2
    assert {row["transcript_id"] for row in exported["orphan_lines"]} == {recorded}
    assert stats["preserved"]["scan_lines"] == 4 and stats["deleted"]["scan_lines"] == 4
    assert stats["deleted"]["scan_incident_links"] == 1 and stats["preserved"]["scan_incident_links"] == 1

    assert {r["transcript_id"] for r in env.store.query("SELECT transcript_id FROM scan_lines")} == {recorded}
    assert count(env.store, "SELECT * FROM scan_observations") == 2, "the audit ledger survives"
    orphan_incident = env.store.query_one("SELECT id FROM incidents")["id"]
    history = so.scan_history(env.store, incident_id=orphan_incident)
    assert history["incident"]["provenance"] == "observed"


def test_rebuild_deletes_nothing_when_the_backup_cannot_be_written(tmp_path, monkeypatch):
    env = make_env(tmp_path)
    _live_and_deleted(env)

    def refuse(payload, destination):
        raise OSError("disk full")

    monkeypatch.setattr(rebuild, "backup_rebuild_rows", refuse)
    with pytest.raises(OSError):
        rebuild.rebuild_state(env.store, export_path=tmp_path / "backup")
    assert count(env.store, "SELECT * FROM scan_lines") == 8
    assert count(env.store, "SELECT * FROM scan_incident_links") == 2


def test_older_schema_dry_run_reads_without_migrating(tmp_path):
    env = make_env(tmp_path)
    _live_and_deleted(env)
    for table in ("session_context_records", "session_context_batches",
                  "scan_incident_links", "scan_observation_occurrences", "scan_occurrences",
                  "scan_lines", "scan_observations", "scan_manifests", "scan_working_copies"):
        env.store.conn.execute(f"DROP TABLE {table}")
    for migration in (so.MIGRATION, '0026_scan_incident_links', '0027_scan_run_index', '0030_session_context'):
        env.store.conn.execute("DELETE FROM schema_migrations WHERE name = ?", (migration,))
    env.store.commit()
    db = Path(env.store.db_path)
    env.store.conn.close()
    reader = Store(db, read_only=True)
    stats = rebuild.rebuild_state(reader, export_path=tmp_path / "backup", dry_run=True)
    assert "scan_lines" not in stats["would_delete"]
    assert not reader.query_one("SELECT name FROM schema_migrations WHERE name = ?", (so.MIGRATION,))


def test_rebuild_names_a_missing_table_under_an_applied_migration(tmp_path):
    env = make_env(tmp_path)
    _live_and_deleted(env)
    env.store.conn.execute("DROP TABLE scan_observation_occurrences")
    env.store.commit()
    with pytest.raises(so.ScanObservationError, match="scan_observation_occurrences is unreadable"):
        rebuild.rebuild_state(env.store, export_path=tmp_path / "backup", dry_run=True)
