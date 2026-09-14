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
            "first_ts": "", "last_ts": "", "mtime": 1.0, "file_size": 10,
            "bytes_scanned": 10, "lines_scanned": 1, "malformed_lines": 0,
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
    _incident(store, str(a), "s-a", [{"role": "user", "text": "x"}])
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
    _incident(store, str(live), "s-live", [{"role": "user", "text": "a"}])
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
