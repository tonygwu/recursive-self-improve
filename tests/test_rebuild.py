"""Preserve retained evidence when rebuilding derived state.
A deleted transcript cannot regenerate its incidents. Keep those orphaned
records and export what survives before removing reproducible derived rows.
"""

from __future__ import annotations

import json

import pytest

from self_improve.rebuild import rebuild_state
from self_improve.store import Store, new_id, utc_now_iso


def _session(store, file_path, sid, project="/p"):
    store.upsert_session(
        {
            "file_path": file_path, "source": "claude", "session_id": sid,
            "project_path": project, "headless": 0, "is_subagent": 0,
            # Live fixture transcripts hold "{}\n": three scanned bytes. A
            # recorded size beyond the file now marks it truncated (BL-26).
            "first_ts": "", "last_ts": "", "mtime": 1.0, "file_size": 3,
            "bytes_scanned": 3, "lines_scanned": 1, "malformed_lines": 0,
            "status": "ok", "error": "", "last_scanned_at": utc_now_iso(),
        }
    )


def _incident(store, file_path, sid, window):
    iid = new_id()
    store.insert_incident(
        {
            "id": iid, "session_file": file_path, "session_id": sid,
            "project_path": "/p", "ts": "2026-08-10T00:00:00Z",
            "signal_type": "correction", "matched_text": "m", "window": window,
        }
    )
    return iid


def _produced(store, file_path, *incident_ids):
    """Record the provenance a real scan writes for these incidents.

    Rebuild deletes an incident only when a ``produced`` scan link names the
    observation that created it (BL-26). An incident inserted directly has no
    such link, which is the shape of one mined before migration 0022, so a
    fixture that means "a scan made this incident" calls this. It records one
    observation over the file's current bytes, a hash per line, and the
    producing link, as ``scan_observations.record_scan`` would.
    """
    import hashlib
    from pathlib import Path

    from self_improve.scan_observations import content_id

    data = Path(file_path).read_bytes()
    transcript = content_id(["fixture-transcript", file_path])
    observation = content_id(["fixture-observation", file_path, list(incident_ids)])
    now = utc_now_iso()
    store.conn.execute(
        "INSERT OR IGNORE INTO scan_manifests (id, compatibility_key, detector_key, parser_key, "
        "config_key, semantics_version, identifiable, manifest_json, created_at) "
        "VALUES ('fixture-manifest', 'fixture', 'fixture', 'fixture', 'fixture', 1, 1, '{}', ?)", (now,))
    store.conn.execute(
        "INSERT INTO scan_observations (id, content_hash, run_id, source, session_file, transcript_id, "
        "canonical_path, path_method, manifest_id, compatibility_key, outcome, projection, file_size, "
        "byte_end, line_end, record_json, observed_at) VALUES "
        "(?, ?, 'fixture-run', 'claude', ?, ?, ?, 'fixture', 'fixture-manifest', 'fixture', "
        "'succeeded', 'complete', ?, ?, ?, '{}', ?)",
        (observation, observation, file_path, transcript, file_path, len(data), len(data),
         data.count(b"\n"), now))
    start = 0
    for number, line in enumerate(data.splitlines(keepends=True), 1):
        digest = hashlib.sha256(line).hexdigest()
        store.conn.execute(
            "INSERT INTO scan_lines (id, revision_hash, transcript_id, compatibility_key, line_no, "
            "line_key, line_sha256, byte_start, byte_end, time_status, source, headless, is_subagent, "
            "category, detector_coverage, observation_id, active) VALUES "
            "(?, ?, ?, 'fixture', ?, ?, ?, ?, ?, 'missing', 'claude', 0, 0, 'record', 'full', ?, 1)",
            (content_id([observation, number]), digest, transcript, number,
             content_id([transcript, number, digest]), digest, start, start + len(line), observation))
        start += len(line)
    for incident in incident_ids:
        store.conn.execute(
            "INSERT INTO scan_incident_links (id, occurrence_id, incident_id, observation_id, "
            "link_kind, created_at) VALUES (?, ?, ?, ?, 'produced', ?)",
            (content_id([observation, incident]), content_id(["fixture-occurrence", incident]),
             incident, observation, now))


@pytest.fixture
def env(tmp_path):
    """Two sessions: one whose transcript still exists, one whose is gone."""
    live = tmp_path / "live.jsonl"
    live.write_text("{}\n")
    gone = str(tmp_path / "deleted.jsonl")  # never created

    store = Store(tmp_path / "s.db")
    _session(store, str(live), "s-live")
    _session(store, gone, "s-gone")
    live_inc = _incident(store, str(live), "s-live", [{"role": "user", "text": "a"}])
    _produced(store, str(live), live_inc)
    gone_inc = _incident(store, gone, "s-gone", [{"role": "user", "text": "irreplaceable"}])

    lid = new_id()
    store.insert(
        "learnings",
        {
            "id": lid, "rule_text": "r", "why": "w", "category": "c",
            "scope": "project", "evidence_count": 1, "project_count": 1,
            "projects_json": "[]", "first_seen": "", "last_seen": "",
            "confidence": 0.5, "status": "proposed", "duplicate_of": "",
            "created_at": utc_now_iso(),
        },
    )
    store.link_incident_learning(live_inc, lid)
    store.insert(
        "proposals",
        {
            "id": new_id(), "learning_id": lid, "target_path": "/t",
            "target_kind": "global_claude_md", "action": "add",
            "created_at": utc_now_iso(),
        },
    )
    store.commit()
    return store, tmp_path, gone, gone_inc, lid


def test_orphaned_evidence_survives_the_wipe(env):
    """The one thing a re-scan can never regenerate."""
    store, tmp_path, gone, gone_inc, _ = env
    rebuild_state(store, export_path=tmp_path / "backup")

    kept = store.query("SELECT * FROM incidents")
    assert [i["id"] for i in kept] == [gone_inc]
    assert "irreplaceable" in kept[0]["window_json"]
    assert [s["session_id"] for s in store.query("SELECT * FROM sessions")] == ["s-gone"]


def test_regenerable_rows_are_wiped(env):
    """Anything a re-scan can rebuild must go, or the rebuild is not a rebuild."""
    store, tmp_path, *_ = env
    rebuild_state(store, export_path=tmp_path / "backup")

    assert store.query("SELECT * FROM learnings") == []
    assert store.query("SELECT * FROM proposals") == []
    assert store.query("SELECT * FROM incident_learnings") == []
    live = [s for s in store.query("SELECT * FROM sessions") if s["session_id"] == "s-live"]
    assert live == [], "a session whose transcript still exists must be re-scanned"


def test_what_is_kept_is_also_exported(env):
    """A DB-only save is not a backup. The export is the audit artifact."""
    store, tmp_path, gone, gone_inc, _ = env
    path = tmp_path / "backup"
    rebuild_state(store, export_path=path)

    payload = json.loads((path / "preserved.json").read_text())
    assert len(payload["incidents"]) == 1
    assert payload["incidents"][0]["id"] == gone_inc
    assert "irreplaceable" in payload["incidents"][0]["window_json"]
    assert len(payload["sessions"]) == 1
    assert payload["reason"]


def test_dry_run_changes_nothing_but_still_reports(env):
    store, tmp_path, *_ = env
    before = len(store.query("SELECT * FROM learnings"))

    stats = rebuild_state(store, export_path=tmp_path / "backup", dry_run=True)

    assert stats["dry_run"] is True
    assert stats["preserved"]["incidents"] == 1
    assert stats["would_delete"]["learnings"] == before
    assert len(store.query("SELECT * FROM learnings")) == before
    assert not (tmp_path / "backup").exists()


def test_refuses_to_run_without_an_export_path(env):
    """Losing the orphans silently is the exact failure this exists to prevent."""
    store, *_ = env
    with pytest.raises(ValueError, match="export"):
        rebuild_state(store, export_path=None)


def test_reports_counts_for_every_table_it_touched(env):
    store, tmp_path, *_ = env
    stats = rebuild_state(store, export_path=tmp_path / "backup")
    assert stats["deleted"]["learnings"] == 1
    assert stats["deleted"]["proposals"] == 1
    assert stats["deleted"]["sessions"] == 1
    assert stats["preserved"]["sessions"] == 1
    assert stats["preserved"]["incidents"] == 1


def test_is_safe_to_run_twice(env):
    """A second pass finds nothing new to wipe and keeps the orphans."""
    store, tmp_path, gone, gone_inc, _ = env
    rebuild_state(store, export_path=tmp_path / "first-backup")
    second = rebuild_state(store, export_path=tmp_path / "second-backup")

    assert second["deleted"]["learnings"] == 0
    assert [i["id"] for i in store.query("SELECT * FROM incidents")] == [gone_inc]


def test_zero_orphans_on_a_populated_corpus_is_flagged(tmp_path):
    """Finding nothing to preserve is legitimate, and also what a broken
    orphan check looks like.

    When every transcript still exists the wipe correctly takes everything —
    but that is byte-for-byte the same outcome as an orphan check that silently
    returned nothing, and the second case destroys evidence permanently. The
    two are indistinguishable from the outside, so the suspicious combination
    (a populated corpus with zero orphans) says so in the stats.
    """
    store, tmp_path_, gone, gone_inc, _ = env_all_live(tmp_path)
    stats = rebuild_state(store, export_path=tmp_path / "backup", dry_run=True)

    assert stats["preserved"]["sessions"] == 0
    assert "warning" in stats
    assert "orphan" in stats["warning"].lower()


def test_zero_orphan_warning_names_the_checks_rebuild_makes(tmp_path):
    """Review nit (BL-26): the warning said "still on disk", but an existing
    transcript is kept when its bytes changed or a scan never produced its
    incident."""
    store, *_ = env_all_live(tmp_path)
    warning = rebuild_state(store, export_path=tmp_path / "backup", dry_run=True)["warning"]
    assert "still on disk" not in warning and "already deleted" not in warning
    assert "matches its retained line hashes" in warning
    assert "a scan produced every incident" in warning


def test_no_warning_when_orphans_were_found(tmp_path):
    store, *_ = env_setup(tmp_path)
    stats = rebuild_state(store, export_path=tmp_path / "backup", dry_run=True)
    assert stats["preserved"]["sessions"] > 0
    assert "warning" not in stats


def env_all_live(tmp_path):
    """Same shape as the main fixture but with BOTH transcripts present."""
    a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    a.write_text("{}\n")
    b.write_text("{}\n")
    store = Store(tmp_path / "live.db")
    _session(store, str(a), "s-a")
    _session(store, str(b), "s-b")
    _produced(store, str(a), _incident(store, str(a), "s-a", [{"role": "user", "text": "x"}]))
    store.commit()
    return store, tmp_path, str(b), None, None


def env_setup(tmp_path):
    live = tmp_path / "live2.jsonl"
    live.write_text("{}\n")
    gone = str(tmp_path / "deleted2.jsonl")
    store = Store(tmp_path / "mixed.db")
    _session(store, str(live), "s-live")
    _session(store, gone, "s-gone")
    _incident(store, gone, "s-gone", [{"role": "user", "text": "keep me"}])
    store.commit()
    return (store,)


def test_a_session_with_no_path_is_treated_as_orphaned(tmp_path):
    """An empty transcript path must preserve evidence as an orphan.
    Path("") points at the current directory; its existence does not establish
    that the original transcript survives.
    """
    store = Store(tmp_path / "s.db")
    live = tmp_path / "live.jsonl"
    live.write_text("{}\n")
    _session(store, str(live), "s-live")
    _session(store, "", "s-nopath")
    _produced(store, str(live), _incident(store, str(live), "s-live", [{"role": "user", "text": "a"}]))
    nopath_inc = _incident(store, "", "s-nopath", [{"role": "user", "text": "irreplaceable"}])
    store.commit()

    stats = rebuild_state(store, export_path=tmp_path / "backup")

    assert stats["preserved"]["incidents"] == 1, stats
    surviving = {r["id"] for r in store.query("SELECT id FROM incidents")}
    assert nopath_inc in surviving, (
        "the incident whose session has no path was deleted; its window_json "
        "was the only evidence for it"
    )
    exported = json.loads((tmp_path / "backup/preserved.json").read_text())
    assert any(i["id"] == nopath_inc for i in exported["incidents"])


def test_queue_archive_is_exported_without_pinning_rebuildable_incidents(env):
    from self_improve import queue_history as q
    from tests.test_queue_history import SETTINGS
    store,tmp_path,gone,gone_inc,_=env
    live=store.query_one('SELECT * FROM incidents WHERE id!=?',(gone_inc,))
    q.set_processed_status(store,live['id'],'dismissed',outcome='negative')
    store.insert('runs',{'id':'queue-run','started':utc_now_iso()})
    snapshot=q.capture(store,'queue-run',phase='finish',settings=SETTINGS);store.commit()
    before={t:store.query('SELECT * FROM '+t) for t in q.TABLES}
    stats=rebuild_state(store,export_path=tmp_path/'queue-backup')
    exported=json.loads((tmp_path/'queue-backup'/'preserved.json').read_text())['queue_history']
    assert exported==before and stats['deleted']['incidents']==1
    assert store.query_one('SELECT * FROM incidents WHERE id=?',(live['id'],)) is None
    assert store.query_one('SELECT * FROM queue_snapshots')==snapshot
    removals=store.query("SELECT * FROM queue_events WHERE operation='delete'")
    assert len(removals)==1 and removals[0]['old_status']=='dismissed' and removals[0]['queue_delta']==0
    assert store.query('SELECT * FROM queue_processing')==before['queue_processing']
    assert q.read_run(store,'queue-run')['queue_count']==1


def test_a_transcript_shorter_than_its_scanned_bytes_keeps_its_evidence(tmp_path):
    """BL-26: a truncated or replaced transcript still counted as rebuildable,
    so rebuild deleted the only copy of its incidents."""
    import json as _json
    from self_improve.rebuild import rebuild_state
    from self_improve.store import Store, new_id, utc_now_iso

    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"x": "' + "a" * 5000 + '"}\n')
    store = Store(tmp_path / "s.db")
    try:
        size = transcript.stat().st_size
        store.upsert_session({"file_path": str(transcript), "source": "claude", "session_id": "s1",
            "project_path": "/p", "headless": 0, "is_subagent": 0, "first_ts": "", "last_ts": "",
            "mtime": 1.0, "file_size": size, "bytes_scanned": size, "lines_scanned": 1,
            "malformed_lines": 0, "status": "ok", "error": "", "last_scanned_at": utc_now_iso()})
        incident = new_id()
        store.insert_incident({"id": incident, "session_file": str(transcript), "session_id": "s1",
            "project_path": "/p", "ts": "2026-08-10T00:00:00Z", "signal_type": "correction",
            "matched_text": "m", "window": [{"role": "user", "text": "only surviving copy"}]})
        store.commit()
        transcript.write_text("")
        stats = rebuild_state(store, export_path=tmp_path / "backup")
        assert stats["deleted"]["incidents"] == 0
        assert store.query_one("SELECT id FROM incidents WHERE id=?", (incident,)) is not None
        exported = _json.loads((tmp_path / "backup" / "preserved.json").read_text())
        assert any(r["id"] == incident for r in exported["incidents"])
    finally:
        store.close()


# ---------------------------------------------------------------------------
# BL-26 review: the retained per-line sha256 in scan_lines decides whether a
# rescan can regenerate a transcript's evidence, whatever scans ran since.
# ---------------------------------------------------------------------------

def _scanned_correction(tmp_path):
    """One real scan of a four-line Claude transcript with one correction."""
    from tests.test_scan_observations import correction_session, make_env, scan, write_claude

    env = make_env(tmp_path)
    records = correction_session(env.repo)
    path = write_claude(env, records)
    scan(env, "run-1")
    incident = env.store.query_one("SELECT id FROM incidents WHERE session_file = ?", (str(path),))
    assert incident is not None, "the fixture must produce the incident under test"
    return env, path, records, incident["id"]


def _transcript_rows(store, path):
    transcript = store.query_one(
        "SELECT transcript_id FROM scan_observations WHERE session_file = ?", (str(path),))["transcript_id"]
    return {
        "scan_lines": store.query_one(
            "SELECT COUNT(*) n FROM scan_lines WHERE transcript_id = ?", (transcript,))["n"],
        "scan_occurrences": store.query_one(
            "SELECT COUNT(*) n FROM scan_occurrences WHERE transcript_id = ?", (transcript,))["n"],
        "scan_incident_links": store.query_one(
            "SELECT COUNT(*) n FROM scan_incident_links l JOIN incidents i ON i.id = l.incident_id "
            "WHERE i.session_file = ?", (str(path),))["n"],
    }, transcript


def _assert_kept_and_exported(env, path, incident, backup, stats, reason):
    store = env.store
    assert store.query_one("SELECT id FROM incidents WHERE id = ?", (incident,)) is not None
    assert store.get_session(str(path)) is not None
    exported = json.loads((backup / "preserved.json").read_text())
    assert incident in {r["id"] for r in exported["incidents"]}
    assert str(path) in {r["file_path"] for r in exported["sessions"]}
    rows, transcript = _transcript_rows(store, path)
    # The incident, its link and the projection the link names stay together.
    assert rows["scan_lines"] > 0 and rows["scan_occurrences"] > 0 and rows["scan_incident_links"] > 0, rows
    scan_export = exported["scan_observations"]
    assert transcript in scan_export["orphan_transcripts"]
    assert {r["transcript_id"] for r in scan_export["orphan_lines"]} == {transcript}
    assert stats["retention"]["unrebuildable_sessions"] == {reason: 1}, stats["retention"]


def test_truncation_then_an_ordinary_scan_keeps_the_evidence(tmp_path):
    """Review finding 1: the nightly scan re-parses a shrunk file from byte 0
    and resets bytes_scanned to the new size, which disarmed the size check."""
    from tests.test_scan_observations import jsonl, scan

    env, path, records, incident = _scanned_correction(tmp_path)
    path.write_bytes(jsonl(records[:2]))
    scan(env, "run-2")
    session = env.store.get_session(str(path))
    assert session["bytes_scanned"] == path.stat().st_size, "the scan reset the offset"

    backup = tmp_path / "backup"
    stats = rebuild_state(env.store, export_path=backup)
    _assert_kept_and_exported(env, path, incident, backup, stats, "content_changed")


def test_truncation_without_a_scan_keeps_the_projection_with_the_incident(tmp_path):
    """Review finding 3: the session and incident were kept while
    _scan_retention, deciding by existence alone, deleted their lines and
    occurrences."""
    from tests.test_scan_observations import jsonl

    env, path, records, incident = _scanned_correction(tmp_path)
    before, _ = _transcript_rows(env.store, path)
    path.write_bytes(jsonl(records[:2]))

    backup = tmp_path / "backup"
    stats = rebuild_state(env.store, export_path=backup)
    _assert_kept_and_exported(env, path, incident, backup, stats, "shorter_than_scanned")
    after, _ = _transcript_rows(env.store, path)
    assert after == before


@pytest.mark.parametrize("longer", [False, True], ids=["equal_length", "longer"])
@pytest.mark.parametrize("rescanned", [False, True], ids=["no_scan", "after_scan"])
def test_a_replaced_transcript_keeps_its_evidence(tmp_path, longer, rescanned):
    """Review finding 2: a file replaced by one of equal or greater length
    passed the size check, so its incidents were deleted."""
    from tests.test_scan_observations import c_user, jsonl, scan, ts

    env, path, records, incident = _scanned_correction(tmp_path)
    original = path.read_bytes()
    replaced = jsonl(records[:3]) + jsonl([c_user("ok, thanks a lot", ts(3), env.repo)])
    assert len(replaced) == len(original) and replaced != original
    if longer:
        replaced += jsonl([c_user("one more line", ts(4), env.repo)])
    path.write_bytes(replaced)
    if rescanned:
        scan(env, "run-2")

    backup = tmp_path / "backup"
    stats = rebuild_state(env.store, export_path=backup)
    _assert_kept_and_exported(env, path, incident, backup, stats, "content_changed")


@pytest.mark.parametrize("rescanned", [False, True], ids=["no_scan", "after_scan"])
def test_an_appended_transcript_is_still_rebuilt(tmp_path, rescanned):
    """Control: append-only growth keeps every retained line, so the rescan
    regenerates the evidence and the rebuild deletes it."""
    from tests.test_scan_observations import append, c_user, jsonl, scan, ts

    env, path, records, incident = _scanned_correction(tmp_path)
    append(path, jsonl([c_user("one more line", ts(4), env.repo)]))
    if rescanned:
        scan(env, "run-2")

    stats = rebuild_state(env.store, export_path=tmp_path / "backup")
    assert env.store.query_one("SELECT id FROM incidents WHERE id = ?", (incident,)) is None
    assert env.store.get_session(str(path)) is None
    assert stats["retention"]["unrebuildable_sessions"] == {}
    assert stats["retention"]["sessions_without_line_identity"] == 0
    rows, _ = _transcript_rows(env.store, path)
    assert rows == {"scan_lines": 0, "scan_occurrences": 0, "scan_incident_links": 0}


def test_a_preview_reads_transcripts_but_writes_nothing(tmp_path):
    """The content check runs in the dry run too, from a read-only Store."""
    from pathlib import Path
    from tests.test_scan_observations import c_user, jsonl, ts

    env, path, records, incident = _scanned_correction(tmp_path)
    original = path.read_bytes()
    path.write_bytes(jsonl(records[:3]) + jsonl([c_user("ok, thanks a lot", ts(3), env.repo)]))
    assert len(path.read_bytes()) == len(original)
    db = Path(env.store.db_path)
    env.store.close()
    before = db.read_bytes()
    reader = Store(db, read_only=True)
    try:
        stats = rebuild_state(reader, export_path=tmp_path / "backup", dry_run=True)
    finally:
        reader.close()
    assert stats["retention"]["unrebuildable_sessions"] == {"content_changed": 1}
    assert stats["preserved"]["incidents"] == 1
    assert db.read_bytes() == before
    assert not (tmp_path / "backup").exists()


def test_sessions_without_retained_line_identity_are_counted(env):
    """A legacy session with no incident has no scan_lines. Existence and
    size alone decide it, and the report says how many were decided that way."""
    store, tmp_path, *_ = env
    bare = tmp_path / "bare.jsonl"
    bare.write_text("{}\n")
    _session(store, str(bare), "s-bare")
    store.commit()
    stats = rebuild_state(store, export_path=tmp_path / "backup", dry_run=True)
    assert stats["retention"]["sessions_without_line_identity"] == 1
    assert stats["retention"]["unrebuildable_sessions"] == {"unreadable": 1}


def test_retention_counts_readable_kept_transcripts_apart_from_unreadable(env):
    """BL-26 third review: retention.orphaned_transcripts counts every kept
    session, so a readable transcript kept for an unverified incident looked
    like a deleted one. The report now splits that total."""
    store, tmp_path, *_ = env
    pinned = tmp_path / "pinned.jsonl"
    pinned.write_text("{}\n")
    _session(store, str(pinned), "s-pinned")
    _incident(store, str(pinned), "s-pinned", [{"role": "user", "text": "no produced link"}])
    store.commit()
    retention = rebuild_state(store, export_path=tmp_path / "backup", dry_run=True)["retention"]
    assert retention["unrebuildable_sessions"] == {"unreadable": 1, "unverified_incident": 1}
    assert retention["orphaned_transcripts"] == 2
    assert retention["orphaned_transcripts_unreadable"] == 1
    assert retention["orphaned_transcripts_readable"] == 1


# ---------------------------------------------------------------------------
# BL-26 second review: an incident mined before migration 0022 has no
# producing scan link. A later scan hashes only the bytes it then reads, so
# matching hashes cannot vouch for the bytes the incident came from.
# ---------------------------------------------------------------------------

def _legacy(store, path):
    """Remove every scan row of this transcript: a session scanned before 0022."""
    transcript = store.query_one(
        "SELECT transcript_id FROM scan_observations WHERE session_file = ?", (str(path),))["transcript_id"]
    observations = "(SELECT id FROM scan_observations WHERE transcript_id = ?)"
    for sql in (
        f"DELETE FROM session_context_records WHERE observation_id IN {observations}",
        f"DELETE FROM session_context_batches WHERE observation_id IN {observations}",
        f"DELETE FROM scan_incident_links WHERE observation_id IN {observations}",
        f"DELETE FROM scan_observation_occurrences WHERE observation_id IN {observations}",
        "DELETE FROM scan_occurrences WHERE transcript_id = ?",
        "DELETE FROM scan_lines WHERE transcript_id = ?",
        "DELETE FROM scan_observations WHERE transcript_id = ?",
    ):
        store.conn.execute(sql, (transcript,))
    store.commit()
    store.check_foreign_keys()


def _legacy_change(name, records, cwd):
    """New transcript bytes for each change; None leaves the file alone."""
    from tests.test_scan_observations import c_user, jsonl, ts

    return {
        # The two scenarios of the review probes.
        "truncated": jsonl(records[:2]),
        "replaced_equal_length": jsonl(records[:3]) + jsonl([c_user("ok, thanks a lot", ts(3), cwd)])
        + jsonl(records[4:]),
        # The trigger line survives, so the rescan adds a corroborated link.
        # The context the window recorded does not survive.
        "tail_truncated_trigger_kept": jsonl(records[:4]),
        "context_replaced_trigger_kept": jsonl([c_user("please list the FILES", ts(0), cwd)])
        + jsonl(records[1:]),
        "appended": jsonl(records) + jsonl([c_user("one more line", ts(9), cwd)]),
        "unchanged": None,
    }[name]


@pytest.mark.parametrize("change", [
    "truncated", "replaced_equal_length", "tail_truncated_trigger_kept",
    "context_replaced_trigger_kept", "appended", "unchanged"])
def test_a_legacy_incident_is_kept_and_exported_after_any_later_scan(tmp_path, change):
    """Review finding 1: after the nightly scan hashed the NEW bytes, every
    retained hash matched, so rebuild deleted the incident without exporting
    it and reported the session as verified."""
    from tests.test_scan_observations import c_user, correction_session, make_env, scan, ts, write_claude

    env = make_env(tmp_path)
    records = correction_session(env.repo) + [c_user("context after the correction", ts(4), env.repo)]
    path = write_claude(env, records)
    scan(env, "run-1")
    incidents = env.store.query("SELECT * FROM incidents WHERE session_file = ?", (str(path),))
    assert len(incidents) == 1, "the fixture must produce exactly the incident under test"
    original = incidents[0]
    _legacy(env.store, path)
    data = _legacy_change(change, records, env.repo)
    if data is not None:
        assert data != path.read_bytes()
        path.write_bytes(data)
    scan(env, "run-2")
    links = {r["link_kind"] for r in env.store.query(
        "SELECT link_kind FROM scan_incident_links WHERE incident_id = ?", (original["id"],))}
    assert "produced" not in links
    if change.endswith("trigger_kept") or change == "appended":
        assert links == {"corroborated"}, "a later link must not count as provenance"
    lines_before = env.store.query_one("SELECT COUNT(*) n FROM scan_lines")["n"]

    backup = tmp_path / "backup"
    stats = rebuild_state(env.store, export_path=backup)
    assert stats["retention"]["unrebuildable_sessions"] == {"unverified_incident": 1}, stats["retention"]
    assert stats["retention"]["sessions_without_line_identity"] == 0
    assert env.store.query_one("SELECT * FROM incidents WHERE id = ?", (original["id"],)) == original
    assert env.store.get_session(str(path)) is not None
    exported = json.loads((backup / "preserved.json").read_text())
    assert original in exported["incidents"]
    assert str(path) in {r["file_path"] for r in exported["sessions"]}
    # The later scan's projection stays with the incident it now describes.
    assert env.store.query_one("SELECT COUNT(*) n FROM scan_lines")["n"] == lines_before
    observed = {r["transcript_id"] for r in env.store.query(
        "SELECT transcript_id FROM scan_observations WHERE session_file = ?", (str(path),))}
    assert observed <= set(exported["scan_observations"]["orphan_transcripts"])


def test_an_incident_the_scanner_could_not_link_is_kept(tmp_path):
    """Named cost of the produced-link rule: two corrections share one
    timestamp, so the scan hashes the bytes but records no link. The
    incident is kept and exported rather than deleted."""
    from tests.test_scan_observations import c_tool, c_user, make_env, scan, ts, write_claude

    env = make_env(tmp_path)
    path = write_claude(env, [
        c_tool(ts(0), env.repo),
        c_user("no, that's wrong", ts(1), env.repo),
        c_user("no, not that one", ts(1), env.repo),
    ])
    scan(env, "run-1")
    incident = env.store.query_one("SELECT * FROM incidents WHERE session_file = ?", (str(path),))
    assert env.store.query_one(
        "SELECT 1 FROM scan_incident_links WHERE incident_id = ?", (incident["id"],)) is None
    record = json.loads(env.store.query_one(
        "SELECT record_json FROM scan_observations WHERE session_file = ?", (str(path),))["record_json"])
    assert record["counts"]["incident_links_ambiguous"] == 1

    stats = rebuild_state(env.store, export_path=tmp_path / "backup")
    assert stats["retention"]["unrebuildable_sessions"] == {"unverified_incident": 1}
    assert env.store.query_one("SELECT * FROM incidents WHERE id = ?", (incident["id"],)) == incident


def _promotion_records(sid, day, cwd):
    """One failing tool call: the error fingerprint this session adds."""
    from tests.test_scan_observations import c_result, c_tool, c_user

    return [c_user("please run it", f"2026-08-{day}T09:00:00Z", cwd, sid=sid),
            c_tool(f"2026-08-{day}T09:00:30Z", cwd, sid=sid),
            c_result("Error: boom", f"2026-08-{day}T09:01:00Z", cwd, sid=sid, is_error=True),
            c_user("ok", f"2026-08-{day}T09:02:00Z", cwd, sid=sid)]


@pytest.mark.parametrize("change", ["anchor_legacy_truncated", "member_legacy_truncated",
                                    "member_truncated", "unchanged"])
def test_a_cross_session_promotion_is_kept_and_exported(tmp_path, change):
    """BL-26 third review: rebuild skipped promoted repeated_errors, saying a
    rescan regenerates them from fingerprints. After the legacy anchor or a
    member transcript was truncated, only one session still held the error.
    Rebuild deleted the promotion without exporting it, and the rescan
    promoted nothing. Now every promotion keeps its anchor session, also when
    nothing changed; that is the cost of the rule."""
    from tests.test_scan_observations import SID, SID_B, jsonl, make_env, scan, write_claude

    env = make_env(tmp_path)
    a_records = _promotion_records(SID, 20, env.repo)
    b_records = _promotion_records(SID_B, 25, env.repo)
    anchor = write_claude(env, a_records, name=SID)
    member = write_claude(env, b_records, name=SID_B)
    assert scan(env, "run-1").promoted_repeated_errors == 1
    incidents = env.store.query("SELECT * FROM incidents")
    assert [(r["signal_type"], r["session_file"]) for r in incidents] == [("repeated_error", str(anchor))], \
        "the promotion must be the only incident, so only it can pin the anchor"
    promoted = incidents[0]["id"]
    assert env.store.query_one("SELECT 1 FROM scan_incident_links WHERE incident_id = ?", (promoted,)) is None
    if change == "anchor_legacy_truncated":
        _legacy(env.store, anchor)
        anchor.write_bytes(jsonl(a_records[:2]))
    elif change.startswith("member"):
        if change == "member_legacy_truncated":
            _legacy(env.store, member)
        member.write_bytes(jsonl(b_records[:2]))
    scan(env, "run-2")
    before = env.store.query_one("SELECT * FROM incidents WHERE id = ?", (promoted,))
    assert before is not None

    backup = tmp_path / "backup"
    stats = rebuild_state(env.store, export_path=backup)
    expected = {"unverified_incident": 1}
    if change == "member_truncated":
        expected["content_changed"] = 1
    assert stats["retention"]["unrebuildable_sessions"] == expected, stats["retention"]
    assert env.store.query_one("SELECT * FROM incidents WHERE id = ?", (promoted,)) == before
    assert env.store.get_session(str(anchor)) is not None
    exported = json.loads((backup / "preserved.json").read_text())
    assert before in exported["incidents"]
    assert str(anchor) in {r["file_path"] for r in exported["sessions"]}
    # The kept promotion is still the only one after a rescan: none is lost,
    # and the scanner's fingerprint dedupe makes no second copy.
    scan(env, "run-3")
    assert [r["id"] for r in env.store.query(
        "SELECT id FROM incidents WHERE signal_type = 'repeated_error'")] == [promoted]


def test_without_scan_tables_every_session_with_an_incident_is_kept(tmp_path):
    """BL-26 third review: no test covered a database without migration
    0022, where no produced link can exist. Replacing that branch with one
    that verifies everything went unnoticed. Here every session holding an
    incident is kept and exported, and a session without one is deleted."""
    from tests.test_scan_observations import (SID, SID_B, c_user, correction_session, make_env,
                                              scan, ts, write_claude)
    from self_improve import scan_observations as so

    env = make_env(tmp_path)
    first = write_claude(env, correction_session(env.repo), name=SID)
    second = write_claude(env, correction_session(env.repo, day=11), name=SID_B)
    quiet_sid = "33333333-3333-4333-8333-333333333333"
    quiet = write_claude(env, [c_user("please list the files", ts(0, 12), env.repo, sid=quiet_sid),
                               c_user("ok", ts(1, 12), env.repo, sid=quiet_sid)], name=quiet_sid)
    scan(env, "run-1")
    incidents = env.store.query("SELECT * FROM incidents ORDER BY session_file")
    assert sorted(r["session_file"] for r in incidents) == sorted([str(first), str(second)])
    assert all(r["signal_type"] != "repeated_error" for r in incidents)
    assert env.store.get_session(str(quiet)) is not None
    for table in ("session_context_records", "session_context_batches",
                  "scan_incident_links", "scan_observation_occurrences", "scan_occurrences",
                  "scan_lines", "scan_observations", "scan_manifests", "scan_working_copies"):
        env.store.conn.execute(f"DROP TABLE {table}")
    for migration in (so.MIGRATION, "0026_scan_incident_links", "0027_scan_run_index", "0030_session_context"):
        env.store.conn.execute("DELETE FROM schema_migrations WHERE name = ?", (migration,))
    env.store.commit()

    backup = tmp_path / "backup"
    stats = rebuild_state(env.store, export_path=backup)
    assert "scan_lines" not in stats["deleted"]
    assert stats["retention"]["unrebuildable_sessions"] == {"unverified_incident": 2}, stats["retention"]
    assert env.store.query("SELECT * FROM incidents ORDER BY session_file") == incidents
    kept = {r["file_path"] for r in env.store.query("SELECT file_path FROM sessions")}
    assert kept == {str(first), str(second)}
    exported = json.loads((backup / "preserved.json").read_text())
    assert sorted(exported["incidents"], key=lambda r: r["session_file"]) == incidents
    assert {r["file_path"] for r in exported["sessions"]} == {str(first), str(second)}
