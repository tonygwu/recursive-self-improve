"""Preserve unrecoverable evidence before rebuilding derived state.

Sessions, incidents, fingerprints, and downstream learnings can be regenerated
only while their source transcripts remain available. Transcript retention can
delete those sources. An archived ``window_json`` can then be the only surviving
evidence, and a rescan cannot recover it.

The rebuild preserves sessions with missing transcripts and their incidents.
It requires a new private backup directory outside Git. It writes and verifies
the retained evidence before deletion, with all database changes in one transaction.

What is kept:
  - sessions whose ``file_path`` no longer exists on disk
  - incidents belonging to those sessions (their window_json IS the evidence)

What goes:
  - every other session and incident, which a rescan can reconstruct
  - all learnings, proposals, incident_learnings, contradictions and eval
    results derived from them
  - cached learning embeddings, which point at learning ids that no longer exist

What is never touched:
  - ``runs`` and ``llm_calls`` — the cost/audit ledger. Those record what was
    actually spent, and rebuilding derived state does not un-spend it.
  - ``schema_migrations``.
"""

from __future__ import annotations

from pathlib import Path

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


def _transcript_exists(file_path: str | None) -> bool:
    """Whether this session's transcript is still readable on disk.

    A blank path is NOT "exists". ``Path("").exists()`` is True — it resolves
    to the current directory — so asking the naive question about a session
    with no recorded path answers "the transcript is still there", and the wipe
    below then deletes its incidents. That is the dangerous direction, and it
    is the failure this whole module exists to prevent.

    Everything unprovable fails toward PRESERVATION. The cost of being wrong
    that way is a row kept that did not need keeping; the cost of being wrong
    the other way is evidence destroyed permanently, silently, and looking like
    a clean rebuild while it happens.
    """
    if not (file_path or "").strip():
        return False
    return Path(file_path).exists()


def _orphaned_sessions(store: Store) -> list[dict]:
    """Sessions whose transcript file is gone — the only unrebuildable rows."""
    return [
        dict(r)
        for r in store.query("SELECT * FROM sessions")
        if not _transcript_exists(r["file_path"])
    ]


def _marks(values) -> str:
    return ", ".join("?" for _ in values)


def _scan_retention(store: Store, orphan_files: set[str]) -> dict:
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
    paths = store.query("SELECT DISTINCT transcript_id, session_file FROM scan_observations")
    live = {r["transcript_id"] for r in paths if _transcript_exists(r["session_file"])}
    orphans = sorted({r["transcript_id"] for r in paths} - live)
    orphan_set = set(orphans)

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
        "preserved_counts": {
            "scan_transcripts": len(orphans),
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
    orphans = scan["orphan_transcripts"]
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

    orphan_sessions = _orphaned_sessions(store)
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
    scan = _scan_retention(store, orphan_files) if has_scan else None
    derived_tables=tuple(t for t in _DERIVED_TABLES if t!='mining_history' or has_history)
    counts = {
        t: store.query_one(f"SELECT COUNT(*) AS n FROM {t}")["n"]
        for t in derived_tables
    }
    counts["sessions"] = (
        store.query_one("SELECT COUNT(*) AS n FROM sessions")["n"] - len(orphan_sessions)
    )
    counts["incidents"] = (
        store.query_one("SELECT COUNT(*) AS n FROM incidents")["n"]
        - len(orphan_incidents)
    )
    if scan is not None:
        counts.update(scan["delete_counts"])

    stats: dict = {
        "dry_run": dry_run,
        "reason": reason,
        "preserved": {
            "sessions": len(orphan_sessions),
            "incidents": len(orphan_incidents),
            **(scan["preserved_counts"] if scan is not None else {}),
        },
        "would_delete" if dry_run else "deleted": counts,
        "export_path": str(export_path),
    }
    # Zero orphans on a populated corpus is legitimate (every transcript still
    # exists) AND is byte-for-byte what a broken orphan check looks like. The
    # second case destroys evidence permanently, and from the outside the two
    # outcomes are identical — so the suspicious combination says so rather
    # than being left for the operator to infer.
    total_sessions = store.query_one("SELECT COUNT(*) AS n FROM sessions")["n"]
    if total_sessions and not orphan_sessions:
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
            "These rows' transcripts no longer exist on disk. window_json is "
            "the only surviving evidence for them and cannot be re-derived."
        ),
        "sessions": orphan_sessions,
        "incidents": orphan_incidents,
    }
    if has_history:
        payload['mining_history']=store.query('SELECT * FROM mining_history ORDER BY created_at,id')
    if scan is not None:
        payload["scan_observations"] = _scan_export(store, scan)
    manifest = backup_rebuild_rows(payload, export_path)
    stats["backup_sha256"] = manifest["sha256"]

    # Scan links reference incidents, so they go before any incident delete.
    if scan is not None:
        _delete_scan_projections(store, scan, orphan_files)
    for table in derived_tables:
        store.conn.execute(f"DELETE FROM {table}")
    # Learning vectors point at ids that are about to stop existing. Rule-unit
    # vectors are keyed by file+text and stay valid, so they are kept.
    store.conn.execute("DELETE FROM embeddings WHERE owner_kind = 'learning'")

    if orphan_files:
        marks = ", ".join("?" for _ in orphan_files)
        store.conn.execute(
            f"DELETE FROM incidents WHERE session_file NOT IN ({marks})",
            tuple(orphan_files),
        )
        store.conn.execute(
            f"DELETE FROM sessions WHERE file_path NOT IN ({marks})",
            tuple(orphan_files),
        )
    else:
        store.conn.execute("DELETE FROM incidents")
        store.conn.execute("DELETE FROM sessions")

    return stats
