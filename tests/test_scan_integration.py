"""Codex integration regressions for the scanner contract, with invented data."""

import dataclasses
import json
import sqlite3

import pytest

from self_improve import filter_incidents, scan_observations as so, store as store_module
from self_improve.sources.codex import CodexSource
from tests.test_scan_observations import (
    SID, append, c_result, c_tool, c_user, correction_session, exposure, jsonl,
    make_env, scan, ts, write_claude,
)
from tests.test_scan_occurrences import write_codex, x_meta, x_tokens


@pytest.mark.parametrize("bad", [
    b'{"timestamp":"2026-08-10T10:01:00Z","type":"token_count",broken}\n',
    b'{"timestamp":"2026-08-10T10:01:00Z","type":"token_count","bad":"\xff"}\n',
])
def test_token_needle_cannot_make_a_malformed_line_eligible(tmp_path, bad):
    env = make_env(tmp_path)
    path = write_codex(env, [x_meta(ts(0), env.repo), x_tokens(ts(1))])
    append(path, bad)
    stats = scan(env, "scan-1")
    assert stats.files_failed == 0
    row = env.store.query_one("SELECT category, exclusion FROM scan_lines WHERE line_no = 3")
    assert row == {"category": "malformed", "exclusion": "malformed"}
    assert exposure(env.store)["eligible_lines"] == 2


@pytest.mark.parametrize("mutate", [
    lambda manifest: manifest["config"]["values"].update(correction_max_len=7),
    lambda manifest: manifest["parser"].update(modules=[{}]),
])
def test_exposure_checks_manifest_integrity_as_history_does(tmp_path, mutate):
    env = make_env(tmp_path)
    write_claude(env, correction_session(env.repo))
    scan(env, "scan-1")
    row = env.store.query_one("SELECT id, manifest_json FROM scan_manifests")
    manifest = json.loads(row["manifest_json"])
    mutate(manifest)
    env.store.conn.execute("UPDATE scan_manifests SET manifest_json = ?", (json.dumps(manifest),))
    env.store.commit()
    with pytest.raises(so.ScanObservationError, match=row["id"]):
        exposure(env.store)


def test_unknown_time_and_unattributed_lines_make_coverage_partial(tmp_path):
    env = make_env(tmp_path)
    path = write_claude(env, [c_user("hello", ts(0), env.repo), c_user("unstamped", "", env.repo)])
    append(path, b"malformed\n")
    scan(env, "scan-1")
    result = exposure(env.store)
    assert result["eligible_lines"] == 1 and result["rate_per_100k"] == 0
    assert result["coverage"]["coverage_complete"] is False
    assert result["coverage"]["counts_by_cause"]["unknown_time_lines"] == 1
    assert result["coverage"]["counts_by_cause"]["unattributed:malformed"] == 1


@pytest.mark.parametrize("change", ["parse", "config"])
def test_parser_manifest_accounts_for_executed_instance(tmp_path, change):
    env = make_env(tmp_path)
    source = CodexSource(env.cfg)
    if change == "parse":
        source.parse = lambda *args, **kwargs: iter(())
    else:
        source.config = dataclasses.replace(env.cfg, denylist_substrings=("different",))
    manifest = so.detector_manifest(env.cfg, source, filter_incidents.detect)
    assert manifest["identifiable"] is False
    assert manifest["parser"]["key"].startswith("unknown:")


def test_rolled_back_incidents_and_fingerprints_are_not_reported_as_recorded(tmp_path, monkeypatch):
    env = make_env(tmp_path)
    write_claude(env, correction_session(env.repo) + [c_result("Error: failed", ts(5), env.repo, is_error=True)])

    def fail(store, **kwargs):
        assert store.query_one("SELECT COUNT(*) AS n FROM incidents")["n"] == 1
        assert store.query_one("SELECT COUNT(*) AS n FROM error_fingerprints")["n"] == 1
        raise RuntimeError("publication refused")

    monkeypatch.setattr(so, "record_scan", fail)
    stats = scan(env, "scan-1")
    assert stats.files_failed == 1
    assert stats.incidents_by_signal == {}
    assert stats.fingerprints_recorded == 0
    assert stats.lines_scanned == 5, "parse work still happened even when the write rolled back"


def test_multiple_explicit_occurrence_links_page_once_per_observation(tmp_path, monkeypatch):
    env = make_env(tmp_path)
    block = {"type": "text", "text": "The root cause was a stale cache."}
    path = write_claude(env, [c_user("check it", ts(0), env.repo), c_tool(ts(1), env.repo),
        c_result("log output", ts(2), env.repo), {
        "type": "assistant", "timestamp": ts(3), "cwd": env.repo,
        "sessionId": SID, "entrypoint": "cli",
        "message": {"role": "assistant", "content": [block, block]},
    }])
    real = so.record_scan

    def publish(store, **kwargs):
        # The boundary receives explicit evidence links. The ordinary scanner
        # still leaves legacy timestamp-only ambiguity unlinked.
        incident = store.query_one("SELECT id, run_id FROM incidents")
        kind = "produced" if kwargs["observation"]["run_id"] == incident["run_id"] else "corroborated"
        for occurrence in kwargs["occurrences"]:
            occurrence["incident_links"] = [{"incident_id": incident["id"], "link_kind": kind}]
        return real(store, **kwargs)

    monkeypatch.setattr(so, "record_scan", publish)
    stats = scan(env, "scan-1")
    assert stats.files_failed == 0, stats.error_taxonomy
    append(path, jsonl([c_user("thanks", ts(4), env.repo)]))
    assert scan(env, "scan-2").files_failed == 0
    incident_id = env.store.query_one("SELECT id FROM incidents")["id"]
    first = so.scan_history(env.store, incident_id=incident_id, limit=1)
    second = so.scan_history(env.store, incident_id=incident_id, limit=1, cursor=first["next_cursor"])
    assert first["records"][0]["run_id"] == "scan-2"
    assert second["records"][0]["run_id"] == "scan-1" and second["next_cursor"] is None
    for page in (first, second):
        assert len(page["records"]) == 1
        assert len(page["records"][0]["occurrence_ids"]) == 2


def test_additive_link_upgrade_preserves_published_schema_data(tmp_path, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(store_module, "MIGRATIONS", [
            migration for migration in store_module.MIGRATIONS if migration[0] != so.LINK_MIGRATION
        ])
        env = make_env(tmp_path)
    store = env.store
    store.insert("sessions", {"file_path": "fixture.jsonl", "source": "claude"})
    store.insert_incident({"id": "incident", "session_file": "fixture.jsonl", "signal_type": "correction"})
    store.insert("scan_manifests", {
        "id": "manifest", "compatibility_key": "version", "detector_key": "detector",
        "parser_key": "parser", "config_key": "config", "semantics_version": 1,
        "identifiable": 0, "manifest_json": "{}", "created_at": ts(0),
    })
    store.insert("scan_observations", {
        "id": "observation", "content_hash": "hash", "run_id": "scan-1", "source": "claude",
        "session_file": "fixture.jsonl", "transcript_id": "transcript", "canonical_path": "fixture.jsonl",
        "path_method": "lexical_missing", "manifest_id": "manifest", "compatibility_key": "version",
        "outcome": "succeeded", "record_json": "{}", "observed_at": ts(0),
    })
    original = {"id": "link-a", "occurrence_id": "occurrence-a", "incident_id": "incident",
                "observation_id": "observation", "link_kind": "produced", "created_at": ts(0)}
    store.insert("scan_incident_links", original)
    store.commit()
    store.conn.close()
    upgraded = store_module.Store(store.db_path)
    assert upgraded.query("SELECT * FROM scan_incident_links") == [original]
    with upgraded.transaction(write=True):
        upgraded.insert("scan_incident_links", {**original, "id": "link-b", "occurrence_id": "occurrence-b"})
    with pytest.raises(sqlite3.IntegrityError):
        with upgraded.transaction(write=True):
            upgraded.insert("scan_incident_links", {**original, "id": "duplicate"})
    assert upgraded.query_one("SELECT COUNT(*) AS n FROM scan_incident_links")["n"] == 2
    upgraded.conn.close()


@pytest.mark.parametrize("coverage", [[], "invalid", None])
def test_malformed_coverage_fails_with_its_observation_owner(tmp_path, coverage):
    env = make_env(tmp_path)
    write_claude(env, correction_session(env.repo))
    scan(env, "scan-1")
    row = env.store.query_one("SELECT id, record_json FROM scan_observations")
    record = json.loads(row["record_json"])
    record["coverage"] = coverage
    env.store.conn.execute("UPDATE scan_observations SET record_json = ?", (json.dumps(record),))
    env.store.commit()
    with pytest.raises(so.ScanObservationError, match=row["id"]):
        exposure(env.store)


@pytest.mark.parametrize("record_type", ["event_msg", "response_item"])
def test_unknown_payload_types_keep_exposure_but_mark_detector_coverage_partial(tmp_path, record_type):
    env = make_env(tmp_path)
    write_codex(env, [x_meta(ts(0), env.repo), {
        "timestamp": ts(1), "type": record_type, "payload": {"type": "future_record"},
    }])
    scan(env, "scan-1")
    result = exposure(env.store)
    assert result["eligible_lines"] == 2
    assert result["coverage"]["coverage_complete"] is False
    assert result["coverage"]["counts_by_cause"]["partial_detector_coverage_lines"] == 1
