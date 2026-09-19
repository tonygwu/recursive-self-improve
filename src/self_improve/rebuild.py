"""Preserve unrecoverable evidence before rebuilding derived state.

Sessions, incidents, fingerprints, and downstream learnings can be regenerated
only while their source transcripts remain available. Transcript retention can
delete those sources. An archived ``window_json`` can then be the only surviving
evidence, and a rescan cannot recover it.

The rebuild preserves sessions with missing transcripts and their incidents.
It requires a new private backup directory outside Git. It writes and verifies
the retained evidence before deletion, with all database changes in one transaction.

What is kept:
  - sessions without readable regular transcripts and their incident windows
  - command/evaluation/application history and its connected source evidence

What goes:
  - unrelated sessions and incidents whose unchanged transcripts remain readable
  - unrelated mining drafts, proposal/eval sources, contradictions and projections
  - cached learning embeddings whose owners are deleted

What is never touched:
  - ``runs`` and ``llm_calls`` — the cost/audit ledger. Those record what was
    actually spent, and rebuilding derived state does not un-spend it.
  - ``schema_migrations``.
"""

from __future__ import annotations

from pathlib import Path
import stat
import os

from .data_boundary import backup_rebuild_rows, private_destination
from .store import Store, utc_now_iso

#: Derived tables cleared wholesale, in FK-safe order (children first).
_DERIVED_TABLES = (
    "mining_history",
    "proposal_events",
    "proposals",
    "incident_learnings",
    "learnings",
    "contradictions",
    "eval_results",
    "project_stats",
    "error_fingerprints",
)


def _transcript_version(file_path: str | None):
    """Return readable regular-file metadata without reading transcript bytes."""
    if not (file_path or '').strip():
        return None
    fd = None
    try:
        fd = os.open(file_path, os.O_RDONLY | os.O_NONBLOCK)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    except OSError:
        return None
    finally:
        if fd is not None:
            os.close(fd)


def _transcript_exists(file_path: str | None) -> bool:
    return _transcript_version(file_path) is not None


def _orphaned_sessions(store: Store, exists=_transcript_exists) -> list[dict]:
    """Preserve when a readable regular transcript cannot be established."""
    return [dict(r) for r in store.query('SELECT * FROM sessions') if not exists(r['file_path'])]


def _marks(values) -> str:
    return ", ".join("?" for _ in values)


def _scan_retention(store: Store, orphan_files: set[str], history_files: set[str], exists=_transcript_exists,
                    measurement_scan_ids=()) -> dict:
    """Split scan measurement rows into rebuildable and unrecoverable.

    Observations and manifests are an audit ledger, like ``runs``, and are
    never deleted. Active and historical line/occurrence projections of a
    surviving transcript are rebuilt by rescanning it. A transcript is
    preserved when none of its recorded paths still exists, because nothing
    can regenerate its projection. An incident link survives only with its
    incident. An applied migration with a missing table raises a named error.
    """
    from .scan_observations import require_schema

    require_schema(store)
    paths = store.query("SELECT id, transcript_id, session_file FROM scan_observations")
    live = {r["transcript_id"] for r in paths if exists(r["session_file"])}
    orphans = sorted({r["transcript_id"] for r in paths} - live)
    preserved = sorted(set(orphans) | {r['transcript_id'] for r in paths
                       if r['session_file'] in history_files or r['id'] in measurement_scan_ids})
    orphan_set = set(preserved)

    def split(sql: str) -> tuple[int, int]:
        rows = store.query(sql)
        kept = sum(r["n"] for r in rows if r["transcript_id"] in orphan_set)
        return kept, sum(r["n"] for r in rows) - kept

    lines_kept, lines_gone = split(
        "SELECT transcript_id, COUNT(*) AS n FROM scan_lines GROUP BY transcript_id"
    )
    occ_kept, occ_gone = split(
        "SELECT transcript_id, COUNT(*) AS n FROM scan_occurrences GROUP BY transcript_id"
    )
    seen_kept, seen_gone = split(
        "SELECT o.transcript_id AS transcript_id, COUNT(*) AS n "
        "FROM scan_observation_occurrences x "
        "JOIN scan_observations o ON o.id = x.observation_id GROUP BY o.transcript_id"
    )
    link_rows = store.query(
        "SELECT i.session_file AS session_file, COUNT(*) AS n FROM scan_incident_links l "
        "JOIN incidents i ON i.id = l.incident_id GROUP BY i.session_file"
    )
    links_kept = sum(r["n"] for r in link_rows if r["session_file"] in orphan_files)
    return {
        "orphan_transcripts": orphans,
        "preserved_transcripts": preserved,
        "preserved_counts": {
            "scan_transcripts": len(preserved),
            "scan_observations": store.query_one("SELECT COUNT(*) AS n FROM scan_observations")["n"],
            "scan_lines": lines_kept,
            "scan_occurrences": occ_kept,
            "scan_observation_occurrences": seen_kept,
            "scan_incident_links": links_kept,
        },
        "delete_counts": {
            "scan_lines": lines_gone,
            "scan_occurrences": occ_gone,
            "scan_observation_occurrences": seen_gone,
            "scan_incident_links": sum(r["n"] for r in link_rows) - links_kept,
        },
    }


def _scan_export(store: Store, scan: dict) -> dict:
    """All scan provenance plus the projections only deleted transcripts produced.

    Projections of surviving transcripts are not exported: a rescan rebuilds
    them, and they can be large.
    """
    orphans = scan["orphan_transcripts"]
    marks = _marks(orphans)

    def orphan_rows(sql: str) -> list[dict]:
        return store.query(sql.format(marks=marks), tuple(orphans)) if orphans else []

    return {
        "manifests": store.query("SELECT * FROM scan_manifests ORDER BY id"),
        "observations": store.query("SELECT * FROM scan_observations ORDER BY observed_at, id"),
        "incident_links": store.query("SELECT * FROM scan_incident_links ORDER BY created_at, id"),
        "orphan_transcripts": orphans,
        "orphan_lines": orphan_rows(
            "SELECT * FROM scan_lines WHERE transcript_id IN ({marks}) ORDER BY transcript_id, line_no, id"
        ),
        "orphan_occurrences": orphan_rows(
            "SELECT * FROM scan_occurrences WHERE transcript_id IN ({marks}) ORDER BY transcript_id, id"
        ),
        "orphan_observation_occurrences": orphan_rows(
            "SELECT x.* FROM scan_observation_occurrences x JOIN scan_observations o "
            "ON o.id = x.observation_id WHERE o.transcript_id IN ({marks}) "
            "ORDER BY x.observation_id, x.occurrence_id"
        ),
    }


def _delete_scan_projections(store: Store, scan: dict, orphan_files: set[str]) -> None:
    orphans = scan["preserved_transcripts"]
    files = tuple(orphan_files)
    if files:
        store.conn.execute(
            "DELETE FROM scan_incident_links WHERE incident_id NOT IN "
            f"(SELECT id FROM incidents WHERE session_file IN ({_marks(files)}))",
            files,
        )
    else:
        store.conn.execute("DELETE FROM scan_incident_links")
    if orphans:
        keep = _marks(orphans)
        params = tuple(orphans)
        store.conn.execute(
            "DELETE FROM scan_observation_occurrences WHERE observation_id IN "
            f"(SELECT id FROM scan_observations WHERE transcript_id NOT IN ({keep}))",
            params,
        )
        store.conn.execute(f"DELETE FROM scan_occurrences WHERE transcript_id NOT IN ({keep})", params)
        store.conn.execute(f"DELETE FROM scan_lines WHERE transcript_id NOT IN ({keep})", params)
    else:
        store.conn.execute("DELETE FROM scan_observation_occurrences")
        store.conn.execute("DELETE FROM scan_occurrences")
        store.conn.execute("DELETE FROM scan_lines")


def rebuild_state(
    store: Store,
    *,
    export_path: str | Path | None,
    dry_run: bool = False,
    reason: str = "rebuild derived state after a scan/mine logic change",
) -> dict:
    """Preserve orphaned evidence, then wipe everything a re-scan can rebuild.

    ``export_path`` is a new private directory, not a JSON file. The backup
    contains preserved.json and its checksummed manifest. Returns per-table
    counts and the verified manifest hash. A dry run uses a read transaction.
    Pending caller work is refused; this function owns its transaction.
    """
    if export_path is None:
        raise ValueError(
            "export_path is required: the orphaned incidents this preserves "
            "cannot be regenerated from disk, so they must be written out "
            "before the wipe, not only moved around inside the DB"
        )

    destination = private_destination(Path(export_path))
    with store.transaction(write=not dry_run):
        return _rebuild_in_transaction(store, destination, dry_run=dry_run, reason=reason)


def _rebuild_in_transaction(store: Store, export_path: Path, *, dry_run: bool, reason: str) -> dict:
    """Read, export, verify, and delete under the caller's owned transaction."""

    transcript_versions = {}
    def exists(path):
        if path not in transcript_versions:
            transcript_versions[path] = _transcript_version(path)
        return transcript_versions[path] is not None
    orphan_sessions = _orphaned_sessions(store, exists)
    orphan_files = {s["file_path"] for s in orphan_sessions}
    orphan_incidents = [
        dict(r)
        for r in store.query("SELECT * FROM incidents")
        if r["session_file"] in orphan_files
    ]

    # Older databases can be previewed without migrating. An applied migration
    # with a missing table must still fail; do not hide schema corruption.
    from .mining_history import MIGRATION as MINING_HISTORY_MIGRATION
    from .scan_observations import MIGRATION as SCAN_MIGRATION
    has_history=bool(store.query_one('SELECT name FROM schema_migrations WHERE name=?',(MINING_HISTORY_MIGRATION,)))
    has_scan = bool(
        store.query_one("SELECT name FROM schema_migrations WHERE name = ?", (SCAN_MIGRATION,))
    )
    if has_scan:
        from .scan_observations import require_schema
        require_schema(store)
    from .rule_revisions import MIGRATION as AVAILABILITY_MIGRATION, TABLES as AVAILABILITY_TABLES, require_schema as require_availability
    has_availability = bool(store.query_one('SELECT name FROM schema_migrations WHERE name=?', (AVAILABILITY_MIGRATION,)))
    if has_availability:
        require_availability(store)
    from . import instruction_inventory
    has_inventory = bool(store.query_one('SELECT name FROM schema_migrations WHERE name=?', (instruction_inventory.MIGRATION,)))
    if has_inventory:
        instruction_inventory.require_schema(store)
    from . import instruction_text
    has_text = bool(store.query_one('SELECT name FROM schema_migrations WHERE name=?', (instruction_text.MIGRATION,)))
    if has_text:
        instruction_text.require_schema(store)
    from . import queue_history
    has_queue = bool(store.query_one('SELECT name FROM schema_migrations WHERE name=?', (queue_history.MIGRATION,)))
    if has_queue:
        queue_history.require_schema(store)
    queue_keys = {'queue_processing': 'event_id', 'queue_events': 'seq'}
    queue_before = {t: store.query('SELECT * FROM '+t+' ORDER BY '+queue_keys.get(t, 'id'))
                    for t in queue_history.TABLES} if has_queue else {}
    from .rebuild_history import plan_history, delete_unretained
    history = plan_history(store)
    initial_counts = {t: store.query_one('SELECT COUNT(*) n FROM '+t)['n'] for t in history['tables']}
    history_files = {r['file_path'] for r in history['kept']['sessions']}
    preserved_files = orphan_files | history_files
    extras = {'sessions': {(r['file_path'],) for r in orphan_sessions},
              'incidents': {(r['id'],) for r in orphan_incidents}}
    source_keys = {t: keys | extras.get(t, set()) for t, keys in history['source_keys'].items()}
    kept_sessions = [r for r in history['rows']['sessions'] if (r['file_path'],) in source_keys['sessions']]
    kept_incidents = [r for r in history['rows']['incidents'] if (r['id'],) in source_keys['incidents']]
    scan = _scan_retention(store, preserved_files, history_files, exists,
                           history['measurement_scan_ids']) if has_scan else None
    derived_tables=tuple(t for t in _DERIVED_TABLES if t!='mining_history' or has_history)
    counts = {
        t: store.query_one(f"SELECT COUNT(*) AS n FROM {t}")["n"]
        for t in derived_tables
    }
    for table in derived_tables:
        if table in source_keys:
            counts[table] = len(history['rows'][table]) - len(source_keys[table])
    fingerprints = store.query('SELECT * FROM error_fingerprints')
    deleted_fingerprints = [r for r in fingerprints if r['session_file'] not in preserved_files]
    counts['error_fingerprints'] = len(deleted_fingerprints)
    kept_learning_ids = {key[0] for key in source_keys['learnings']}
    deleted_embeddings = [r for r in store.query("SELECT owner_kind,owner_key,model FROM embeddings WHERE owner_kind='learning'")
                          if r['owner_key'] not in kept_learning_ids]
    counts['embeddings'] = len(deleted_embeddings)
    from .project_measurements import MIGRATION as MEASUREMENT_MIGRATION, PROFILE as MEASUREMENT_PROFILE
    has_measurements = bool(store.query_one('SELECT name FROM schema_migrations WHERE name=?', (MEASUREMENT_MIGRATION,)))
    if has_measurements:
        counts['project_stats'] = store.query_one('SELECT COUNT(*) n FROM project_stats WHERE record_type<>?', (MEASUREMENT_PROFILE,))['n']
    counts["sessions"] = (
        store.query_one("SELECT COUNT(*) AS n FROM sessions")["n"] - len(kept_sessions)
    )
    counts["incidents"] = (
        store.query_one("SELECT COUNT(*) AS n FROM incidents")["n"]
        - len(kept_incidents)
    )
    if scan is not None:
        counts.update(scan["delete_counts"])

    stats: dict = {
        "dry_run": dry_run,
        "reason": reason,
        "preserved": {
            **{t: len(keys) for t, keys in source_keys.items()},
            'error_fingerprints': len(fingerprints) - len(deleted_fingerprints),
            'embeddings': store.query_one('SELECT COUNT(*) n FROM embeddings')['n'] - len(deleted_embeddings),
            **(scan["preserved_counts"] if scan is not None else {}),
        },
        "would_delete" if dry_run else "deleted": counts,
        "export_path": str(export_path),
        'retention': {'orphaned_transcripts': len(orphan_sessions),
                      'orphaned_incidents': len(orphan_incidents),
                      'execution_history': history['counts'], 'reasons': history['reasons'],
                      'measurement_scan_observations': sorted(history['measurement_scan_ids'])},
    }
    # Zero orphans on a populated corpus is legitimate (every transcript still
    # exists) AND is byte-for-byte what a broken orphan check looks like. The
    # second case destroys evidence permanently, and from the outside the two
    # outcomes are identical — so the suspicious combination says so rather
    # than being left for the operator to infer.
    total_sessions = store.query_one("SELECT COUNT(*) AS n FROM sessions")["n"]
    if total_sessions and not kept_sessions:
        stats["warning"] = (
            f"zero orphan sessions found across {total_sessions} sessions, so "
            "NOTHING will be preserved. That is correct if every transcript is "
            "still on disk, and is also exactly what a failed orphan check "
            "looks like. Confirm before running without --dry-run: any "
            "incident whose transcript is already deleted cannot be recovered."
        )

    if dry_run:
        return stats

    payload = {
        "exported_at": utc_now_iso(),
        "reason": reason,
        "note": (
            'Preserved orphan windows and execution-history source dependencies. '
            'The retention report distinguishes their reasons; history may pin live transcripts.'
        ),
        'sessions': kept_sessions,
        'incidents': kept_incidents,
        'execution_history': history['kept'],
        'retention': stats['retention'],
        'error_fingerprints': [r for r in fingerprints if r['session_file'] in preserved_files],
    }
    if has_queue:
        payload['queue_history'] = queue_before
    if has_history:
        payload['mining_history']=store.query('SELECT * FROM mining_history ORDER BY created_at,id')
    if scan is not None:
        payload["scan_observations"] = _scan_export(store, scan)
    if has_availability:
        # Delivered revisions and actual-copy observations survive a derived-data
        # rebuild. Export them too, so their links remain auditable after it.
        payload['rule_availability'] = {table: store.query(f'SELECT * FROM {table} ORDER BY id')
                                        for table in AVAILABILITY_TABLES}
    if has_inventory:
        payload['instruction_inventories'] = store.query('SELECT * FROM instruction_inventories ORDER BY id')
    if has_text:
        payload['instruction_text_archives'] = store.query('SELECT * FROM instruction_text_archives ORDER BY id')
    from .session_context import MIGRATION as CONTEXT_MIGRATION, TABLES as CONTEXT_TABLES
    if store.query_one('SELECT name FROM schema_migrations WHERE name=?', (CONTEXT_MIGRATION,)):
        # Retain native context with the original scan observations. Rebuilding
        # derived lines must not erase the only surviving creation/scope evidence.
        payload['session_context'] = {table: store.query(f'SELECT * FROM {table} ORDER BY observation_id')
                                      for table in CONTEXT_TABLES}
    from .native_loads import MIGRATION as NATIVE_MIGRATION
    if store.query_one('SELECT name FROM schema_migrations WHERE name=?', (NATIVE_MIGRATION,)):
        payload['native_load_reports'] = store.query('SELECT * FROM native_load_reports ORDER BY received_at,id')
    if has_measurements:
        payload['project_measurements'] = store.query('SELECT * FROM project_stats WHERE record_type=? ORDER BY observed_at,id', (MEASUREMENT_PROFILE,))
    from .rule_families import MIGRATION as FAMILY_MIGRATION, TABLES as FAMILY_TABLES, family_snapshot
    if store.query_one('SELECT name FROM schema_migrations WHERE name=?', (FAMILY_MIGRATION,)):
        # Display membership is historical evidence, not an active learning FK.
        for row in store.query('SELECT id FROM rule_family_snapshots ORDER BY generation'):
            family_snapshot(store, row['id'])
        payload['rule_families'] = {
            table: store.query(f'SELECT * FROM {table} ORDER BY ' +
                               ({'rule_family_snapshots':'generation', 'rule_family_members':'snapshot_id,learning_id', 'rule_family_heads':'profile'}[table]))
            for table in FAMILY_TABLES}
    if scan is not None and set(scan['preserved_transcripts']) != set(scan['orphan_transcripts']):
        retained_scan = {**scan, 'orphan_transcripts': scan['preserved_transcripts']}
        retained_export = _scan_export(store, retained_scan)
        payload['execution_scan_projections'] = {k: v for k, v in retained_export.items() if k.startswith('orphan_')}
    payload['learning_embeddings'] = [r for r in store.query("SELECT * FROM embeddings WHERE owner_kind='learning'")
                                      if r['owner_key'] in kept_learning_ids]
    payload['execution_audit'] = {
        table: store.query('SELECT * FROM '+table)
        for table in ('runs', 'llm_calls', 'execution_policies', 'execution_policy_events',
                      'project_identity_cache', 'scan_working_copies') if table in history['tables']}
    manifest = backup_rebuild_rows(payload, export_path)
    stats["backup_sha256"] = manifest["sha256"]
    # A backup may take time. Refuse if a source counted as reconstructible
    # changed or disappeared before deletion; the verified backup remains.
    for path, version in transcript_versions.items():
        if version is not None and _transcript_version(path) != version:
            raise ValueError('Transcript changed during rebuild backup: '+path)

    # Scan links reference incidents, so they go before any incident delete.
    if scan is not None:
        _delete_scan_projections(store, scan, preserved_files)
    for table in derived_tables:
        if table == 'project_stats' and has_measurements:
            store.conn.execute('DELETE FROM project_stats WHERE record_type<>?', (MEASUREMENT_PROFILE,))
        elif table in source_keys:
            deleted = delete_unretained(store, history, table)
            if deleted != counts[table]:
                raise ValueError('Rebuild count changed for '+table)
        elif table == 'error_fingerprints':
            store.conn.executemany('DELETE FROM error_fingerprints WHERE fingerprint=? AND session_file=?',
                                   [(r['fingerprint'], r['session_file']) for r in deleted_fingerprints])
        else:
            store.conn.execute(f"DELETE FROM {table}")
    # Preserve vectors for surviving learning IDs. Other learning vectors are stale.
    store.conn.executemany('DELETE FROM embeddings WHERE owner_kind=? AND owner_key=? AND model=?',
                           [(r['owner_kind'], r['owner_key'], r['model']) for r in deleted_embeddings])
    for table in ('incidents', 'sessions'):
        deleted = delete_unretained(store, history, table, extra_keys=extras[table])
        if deleted != counts[table]:
            raise ValueError('Rebuild count changed for '+table)
    store.check_foreign_keys()
    if has_queue:
        # Every deleted incident appends a removal, including terminal members
        # with zero occupancy delta. No preserved queue record may change.
        old_seq = max((r['seq'] for r in queue_before['queue_events']), default=0)
        appended = store.query('SELECT * FROM queue_events WHERE seq>? ORDER BY seq', (old_seq,))
        deleted_incidents = {r['id']: r for r in history['rows']['incidents']
                             if (r['id'],) not in source_keys['incidents']}
        if (len(appended) != len(deleted_incidents) or
                {r['incident_id'] for r in appended} != set(deleted_incidents) or
                any(r['operation'] != 'delete' or r['new_status'] is not None or
                    r['old_status'] != deleted_incidents[r['incident_id']]['status'] or
                    r['queue_delta'] != -int(r['old_status'] == 'new') or r['seq'] != old_seq+i+1
                    for i, r in enumerate(appended))):
            raise ValueError('Rebuild queue removal history differs from planned deletion')
        for table, expected_rows in queue_before.items():
            actual_rows = store.query('SELECT * FROM '+table +
                (' WHERE seq<=?' if table == 'queue_events' else '') +
                ' ORDER BY '+queue_keys.get(table, 'id'),
                (old_seq,) if table == 'queue_events' else ())
            if actual_rows != expected_rows:
                raise ValueError('Rebuild changed retained queue history: '+table)
    for table, before_count in initial_counts.items():
        actual = store.query_one('SELECT COUNT(*) n FROM '+table)['n']
        added = counts['incidents'] if has_queue and table == 'queue_events' else 0
        if actual != before_count - counts.get(table, 0) + added:
            raise ValueError('Rebuild count differs from plan for '+table)
    for table, expected in history['kept'].items():
        columns = history['keys'][table]
        for row in expected:
            actual = store.query_one('SELECT * FROM '+table+' WHERE '+' AND '.join(c+'=?' for c in columns),
                                     tuple(row[c] for c in columns))
            if actual != row:
                raise ValueError('Rebuild changed retained '+table+' record '+repr(tuple(row[c] for c in columns)))
    return stats
