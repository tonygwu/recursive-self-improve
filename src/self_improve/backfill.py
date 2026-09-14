"""Backfill canonical project identity onto rows written before migration 0006.

Migration 0006 added ``project_key`` with an empty default, which is correct
for the additive migration. Existing rows retain only their raw cwd until
backfill resolves them. Without canonical keys, old and new rows can group
separately and undercount evidence from the same project.

Design notes worth defending:

- **Idempotent.** Re-running after an interruption is the normal recovery move,
  and ``projects_json`` is rewritten in place; a non-idempotent version would
  feed already-canonical keys back through the resolver. Rows that already
  carry a key are skipped and counted as such.
- **One resolution per distinct path**, not per row.
- **Evidence is never collapsed.** Two incidents in two clones remain two
  pieces of evidence; only the project attribution was wrong.
- **Unresolvable paths still get a key** (``unresolved:<path>``, distinct per
  path). Skipping them would leave rows that group with nothing; merging them
  would invent a project.
- **Reports attempted / updated / skipped plus a method taxonomy**, so a run
  that silently degraded to coarser keys is visible rather than implied.
"""

from __future__ import annotations

import json

from .routing import canonical_working_copy
from .store import Store


def _resolve_all(store: Store, resolver, cache: dict) -> dict[str, object]:
    """Identity for every distinct project_path in sessions, resolved once."""
    paths = [
        r["project_path"]
        for r in store.query("SELECT DISTINCT project_path FROM sessions")
    ]
    return {p: resolver(p, cache=cache) for p in paths}


#: Resolution methods ranked best-first, mirroring project_identity's own
#: order. A key is only ever moved UP this list.
_METHOD_RANK = ("gh_repo_id", "remote_url", "git_root", "path", "unresolved")


def _is_better(new_method: str, old_method: str) -> bool:
    """True when `new_method` sits strictly earlier in the resolution order."""
    try:
        return _METHOD_RANK.index(new_method) < _METHOD_RANK.index(old_method)
    except ValueError:  # an unknown method is never an upgrade
        return False


def backfill_project_identity(
    store: Store,
    *,
    resolver=None,
    dry_run: bool = False,
    use_gh: bool = True,
    requalify: bool = False,
) -> dict:
    """Populate project_key/display/method across sessions, incidents, learnings.

    ``resolver`` defaults to :func:`project_identity.resolve` and is injected in
    tests. Returns a stats dict; nothing is written when ``dry_run``.
    """
    if resolver is None:
        from .project_identity import resolve as _resolve

        def resolver(path, **kw):  # noqa: ANN001
            return _resolve(path, use_gh=use_gh, **kw)

    cache: dict = {}
    identities = _resolve_all(store, resolver, cache)

    methods: dict[str, int] = {}
    for ident in identities.values():
        methods[ident.method] = methods.get(ident.method, 0) + 1

    # repo key -> every scanned working copy of it, sorted so the write target
    # a learning gets does not depend on dict iteration order.
    paths_by_key: dict[str, list[str]] = {}
    for path, ident in identities.items():
        paths_by_key.setdefault(ident.key, []).append(path)
    for paths in paths_by_key.values():
        paths.sort()

    stats: dict = {
        "dry_run": dry_run,
        "distinct_paths": len(identities),
        "methods": methods,
        "sessions": {"attempted": 0, "updated": 0, "skipped_already_set": 0,
                     "would_update": 0},
        "incidents": {"attempted": 0, "updated": 0, "skipped_already_set": 0,
                      "would_update": 0},
        "learnings": {"attempted": 0, "updated": 0, "skipped_already_set": 0,
                      "would_update": 0},
    }

    # Snapshot session provenance BEFORE the sessions loop rewrites it. The
    # incidents loop below needs the method that produced the INCIDENT's key,
    # and by then the session has already been upgraded — reading it back would
    # always compare a method against itself and never requalify anything.
    method_before = {
        r["file_path"]: r["project_key_method"] or ""
        for r in store.query("SELECT file_path, project_key_method FROM sessions")
    }

    # ---- sessions ----
    stats["sessions"].setdefault("requalified", 0)
    stats["sessions"].setdefault("would_requalify", 0)
    for row in store.query("SELECT * FROM sessions"):
        stats["sessions"]["attempted"] += 1
        if row["project_key"]:
            # Requalification can repair a key created by a degraded resolver.
            # It is an explicit migration option and only moves to a stronger
            # identity, so another resolver failure cannot weaken existing keys.
            if requalify:
                ident = identities.get(row["project_path"])
                if ident is not None and _is_better(
                    ident.method, row["project_key_method"] or ""
                ):
                    if dry_run:
                        stats["sessions"]["would_requalify"] += 1
                    else:
                        store.update("sessions", "file_path", row["file_path"], {
                            "project_key": ident.key,
                            "project_display": ident.display,
                            "project_key_method": ident.method,
                        })
                        stats["sessions"]["requalified"] += 1
                    continue
            stats["sessions"]["skipped_already_set"] += 1
            continue
        ident = identities.get(row["project_path"])
        if ident is None:
            continue
        if dry_run:
            stats["sessions"]["would_update"] += 1
            continue
        store.conn.execute(
            "UPDATE sessions SET project_key = ?, project_display = ?, "
            "project_key_method = ? WHERE file_path = ?",
            (ident.key, ident.display, ident.method, row["file_path"]),
        )
        stats["sessions"]["updated"] += 1

    # ---- incidents: inherit from their session, never re-resolve ----
    # Re-resolving could disagree with the session if a directory moved between
    # the two reads; the session row is the single source of truth.
    stats["incidents"].setdefault("requalified", 0)
    stats["incidents"].setdefault("would_requalify", 0)
    for row in store.query("SELECT * FROM incidents"):
        stats["incidents"]["attempted"] += 1
        if row["project_key"]:
        # Incidents inherit session keys and supply a learning's project count.
        # Repair both tables so they continue to identify the same repository.
            if requalify:
                ident = identities.get(row["project_path"])
                if ident is not None and _is_better(
                    ident.method, method_before.get(row["session_file"], "")
                ):
                    if dry_run:
                        stats["incidents"]["would_requalify"] += 1
                    else:
                        store.conn.execute(
                            "UPDATE incidents SET project_key = ? WHERE id = ?",
                            (ident.key, row["id"]),
                        )
                        stats["incidents"]["requalified"] += 1
                    continue
            stats["incidents"]["skipped_already_set"] += 1
            continue
        ident = identities.get(row["project_path"])
        if ident is None:
            continue
        if dry_run:
            stats["incidents"]["would_update"] += 1
            continue
        store.conn.execute(
            "UPDATE incidents SET project_key = ? WHERE id = ?",
            (ident.key, row["id"]),
        )
        stats["incidents"]["updated"] += 1

    # ---- learnings: paths -> canonical keys, plus a real write target ----
    for row in store.query("SELECT * FROM learnings"):
        stats["learnings"]["attempted"] += 1
        try:
            projects = json.loads(row["projects_json"] or "[]")
        except json.JSONDecodeError as exc:
            # Our own DB invariant: strict JSON. Fail loud rather than guess.
            raise ValueError(
                f"learning {row['id']}: projects_json is not valid JSON: {exc}"
            ) from exc
        if not isinstance(projects, list):
            raise ValueError(f"learning {row['id']}: projects_json is not a list")

        # Already canonical (or nothing to do) -> skip, so a second pass is a
        # no-op rather than a second rewrite.
        if row["primary_project_path"] or not projects:
            stats["learnings"]["skipped_already_set"] += 1
            continue

        keys, path_keys = [], []
        for p in projects:
            ident = identities.get(p)
            key = ident.key if ident is not None else resolver(p, cache=cache).key
            if key not in keys:
                keys.append(key)
            path_keys.append((p, key))

        # Which *repo* stays first-wins, as before. What changes is which
        # working copy of that repo we write into: repo-0 by convention, not
        # whichever cwd the miner happened to see first. Only same-repo paths
        # are candidates, so a learning spanning two repos can never have its
        # write target pulled into the other one.
        primary_key = path_keys[0][1]
        write_path = canonical_working_copy(
            [p for p, k in path_keys if k == primary_key],
            # Every other checkout of this same repo that we have ever scanned.
            # Needed because a clone set is not always discoverable from a
            # path alone: unrelated directory names can hold the same repo.
            # Use the identities already recorded in the database.
            also_consider=paths_by_key.get(primary_key, ()),
            project_key=primary_key,
            resolver=lambda p: resolver(p, cache=cache).key,
        )

        if dry_run:
            stats["learnings"]["would_update"] += 1
            continue
        store.conn.execute(
            "UPDATE learnings SET projects_json = ?, project_count = ?, "
            "primary_project_path = ? WHERE id = ?",
            (json.dumps(keys), len(keys), write_path, row["id"]),
        )
        stats["learnings"]["updated"] += 1

    if not dry_run:
        store.commit()
    return stats
