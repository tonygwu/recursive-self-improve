"""Backfill canonical project identity onto rows scanned before migration 0006.

Rows written with raw working-directory identities must group with newly
scanned repository identities. Every test constructs its own temporary store.
"""

from __future__ import annotations

import json

from self_improve.backfill import backfill_project_identity
from self_improve.project_identity import ProjectIdentity
from self_improve.store import Store, new_id, utc_now_iso

CLONE_A = "/Users/x/Code/demo-service/repo-0"
CLONE_B = "/Users/x/Code/old-service/repo-3"
OTHER = "/Users/x/Code/unrelated"
KEY = "remote:github.com/example/demo-service"
OTHER_KEY = "remote:github.com/example/unrelated"


def fake_resolver(path, **kw):
    """Deterministic stand-in for project_identity.resolve.

    The real resolver needs a real repo on disk; this test is about the
    backfill's bookkeeping, not about resolution (which test_project_identity
    covers against real git repos).
    """
    if path in (CLONE_A, CLONE_B):
        return ProjectIdentity(KEY, "demo-service", "remote_url")
    if path == OTHER:
        return ProjectIdentity(OTHER_KEY, "unrelated", "remote_url")
    return ProjectIdentity(f"unresolved:{path}", path or "(unknown)", "unresolved")


def seed(tmp_path) -> Store:
    """A DB in the pre-0006 state: real rows, all project_key empty."""
    store = Store(tmp_path / "s.db")
    for i, cwd in enumerate([CLONE_A, CLONE_B, OTHER]):
        sf = f"/virtual/s{i}.jsonl"
        store.upsert_session(
            {
                "file_path": sf, "source": "claude", "session_id": f"s{i}",
                "project_path": cwd, "headless": 0, "is_subagent": 0,
                "first_ts": "", "last_ts": "", "mtime": 0.0, "file_size": 0,
                "bytes_scanned": 0, "lines_scanned": 0, "malformed_lines": 0,
                "status": "ok", "error": "", "last_scanned_at": utc_now_iso(),
            }
        )
        store.insert_incident(
            {
                "id": new_id(), "session_file": sf, "session_id": f"s{i}",
                "project_path": cwd, "ts": "2026-08-10T00:00:00Z",
                "signal_type": "correction", "matched_text": "x", "window": [],
            }
        )
    # A learning attributed to two working copies of ONE repo — the shape that
    # made project_count read 2 for a single-repo lesson.
    store.insert(
        "learnings",
        {
            "id": new_id(), "rule_text": "r", "why": "w", "category": "tooling",
            "scope": "project", "evidence_count": 2, "project_count": 2,
            "projects_json": json.dumps([CLONE_A, CLONE_B]),
            "first_seen": "", "last_seen": "", "confidence": 0.7,
            "status": "candidate", "duplicate_of": "",
            "created_at": utc_now_iso(),
        },
    )
    store.commit()
    return store


def test_sessions_get_keys_and_clones_collapse(tmp_path):
    store = seed(tmp_path)
    backfill_project_identity(store, resolver=fake_resolver)

    rows = {r["session_id"]: r for r in store.query("SELECT * FROM sessions")}
    assert rows["s0"]["project_key"] == rows["s1"]["project_key"] == KEY
    assert rows["s2"]["project_key"] == OTHER_KEY
    assert rows["s0"]["project_display"] == "demo-service"
    assert rows["s0"]["project_key_method"] == "remote_url"


def test_incidents_inherit_their_sessions_key(tmp_path):
    store = seed(tmp_path)
    backfill_project_identity(store, resolver=fake_resolver)

    keys = {i["project_path"]: i["project_key"] for i in store.query("SELECT * FROM incidents")}
    assert keys[CLONE_A] == keys[CLONE_B] == KEY
    assert keys[OTHER] == OTHER_KEY


def test_learning_project_count_drops_to_the_number_of_REPOS(tmp_path):
    """Count distinct repositories independently of their working copies.

    The two fixture clones contribute one project to the learning. Selecting
    the instruction-file write target is a separate decision.
    """
    store = seed(tmp_path)
    backfill_project_identity(store, resolver=fake_resolver)

    lrn = store.query("SELECT * FROM learnings")[0]
    assert json.loads(lrn["projects_json"]) == [KEY]
    assert lrn["project_count"] == 1, "two clones of one repo is ONE project"
    # Routing still needs somewhere real to write.
    assert lrn["primary_project_path"] in (CLONE_A, CLONE_B)


def test_evidence_count_is_not_touched(tmp_path):
    """Collapsing projects must not collapse EVIDENCE.

    Two incidents in two clones are still two pieces of evidence for the
    lesson; only the project attribution was wrong.
    """
    store = seed(tmp_path)
    before = store.query("SELECT * FROM learnings")[0]["evidence_count"]
    backfill_project_identity(store, resolver=fake_resolver)
    assert store.query("SELECT * FROM learnings")[0]["evidence_count"] == before


def test_dry_run_reports_but_writes_nothing(tmp_path):
    store = seed(tmp_path)
    stats = backfill_project_identity(store, resolver=fake_resolver, dry_run=True)

    assert stats["sessions"]["would_update"] == 3
    assert all(r["project_key"] == "" for r in store.query("SELECT * FROM sessions"))
    assert stats["dry_run"] is True


def test_is_idempotent(tmp_path):
    """A second pass must be a no-op, not a second rewrite.

    Re-running is the normal recovery move after an interrupted backfill, and
    projects_json is rewritten in place — a non-idempotent version would map
    already-canonical keys through the resolver again.
    """
    store = seed(tmp_path)
    first = backfill_project_identity(store, resolver=fake_resolver)
    lrn_after_first = store.query("SELECT * FROM learnings")[0]

    second = backfill_project_identity(store, resolver=fake_resolver)

    assert first["sessions"]["updated"] == 3
    assert second["sessions"]["updated"] == 0
    assert second["learnings"]["updated"] == 0
    assert store.query("SELECT * FROM learnings")[0] == lrn_after_first


def test_reports_resolution_methods_so_degradation_is_visible(tmp_path):
    store = seed(tmp_path)
    stats = backfill_project_identity(store, resolver=fake_resolver)
    assert stats["methods"] == {"remote_url": 3}


def test_resolves_each_distinct_path_once(tmp_path):
    """Resolve each distinct working directory once across repeated sessions."""
    store = seed(tmp_path)
    calls = []

    def counting(path, **kw):
        calls.append(path)
        return fake_resolver(path, **kw)

    backfill_project_identity(store, resolver=counting)
    assert sorted(calls) == sorted({CLONE_A, CLONE_B, OTHER})


def test_an_unresolvable_path_is_recorded_not_skipped(tmp_path):
    """A vanished directory still needs a key, or its rows never group at all."""
    store = seed(tmp_path)
    store.conn.execute(
        "UPDATE sessions SET project_path = '/Users/x/Code/retired-demo' "
        "WHERE session_id = 's2'"
    )
    store.commit()

    stats = backfill_project_identity(store, resolver=fake_resolver)

    row = store.query_one("SELECT * FROM sessions WHERE session_id = 's2'")
    assert row["project_key"].startswith("unresolved:")
    assert stats["methods"].get("unresolved") == 1


# --- write target: repo-0 is the automation checkout (operator convention) ---


def _clone_set(tmp_path, *names):
    """Real directories, because backfill's sibling probe stats the disk."""
    parent = tmp_path / "clones" / "demo-service"
    for n in names:
        (parent / n).mkdir(parents=True)
    return parent


def _seed_learning(store, projects):
    lid = new_id()
    store.insert(
        "learnings",
        {
            "id": lid, "rule_text": "r2", "why": "w", "category": "tooling",
            "scope": "project", "evidence_count": 2, "project_count": len(projects),
            "projects_json": json.dumps(projects),
            "first_seen": "", "last_seen": "", "confidence": 0.7,
            "status": "candidate", "duplicate_of": "", "created_at": utc_now_iso(),
        },
    )
    store.commit()
    return lid


def test_write_target_is_repo_0_even_when_no_evidence_came_from_it(tmp_path):
    """Find the verified repo-0 sibling when evidence names only repo-3/repo-5.

    The invented fixture has no evidence from repo-0. Backfill must discover it
    on disk and confirm its repository identity before selecting it.
    """
    parent = _clone_set(tmp_path, "repo-0", "repo-3", "repo-5")
    store = seed(tmp_path)
    lid = _seed_learning(store, [str(parent / "repo-3"), str(parent / "repo-5")])

    backfill_project_identity(
        store, resolver=lambda p, **kw: ProjectIdentity(KEY, "demo-service", "remote_url")
    )

    row = store.query("SELECT * FROM learnings WHERE id = ?", (lid,))[0]
    assert row["primary_project_path"] == str(parent / "repo-0")


def test_write_target_ignores_a_repo_0_belonging_to_another_repo(tmp_path):
    """Same directory name, different repo — writing there would be wrong."""
    parent = _clone_set(tmp_path, "repo-0", "repo-3")
    store = seed(tmp_path)
    lid = _seed_learning(store, [str(parent / "repo-3")])

    def resolver(p, **kw):
        if p.endswith("repo-0"):
            return ProjectIdentity(OTHER_KEY, "unrelated", "remote_url")
        return ProjectIdentity(KEY, "demo-service", "remote_url")

    backfill_project_identity(store, resolver=resolver)
    row = store.query("SELECT * FROM learnings WHERE id = ?", (lid,))[0]
    assert row["primary_project_path"] == str(parent / "repo-3")


def test_write_target_does_not_cross_into_a_second_repo(tmp_path):
    """A learning spanning two repos writes into the first repo's clone set."""
    parent = _clone_set(tmp_path, "repo-0", "repo-3")
    store = seed(tmp_path)
    lid = _seed_learning(store, [str(parent / "repo-3"), OTHER])

    def resolver(p, **kw):
        if p == OTHER:
            return ProjectIdentity(OTHER_KEY, "unrelated", "remote_url")
        return ProjectIdentity(KEY, "demo-service", "remote_url")

    backfill_project_identity(store, resolver=resolver)
    row = store.query("SELECT * FROM learnings WHERE id = ?", (lid,))[0]
    assert row["primary_project_path"] == str(parent / "repo-0")
    assert OTHER not in row["primary_project_path"]


# ---------------------------------------------------------------------------
# Repairing a key written during a degraded resolution
# ---------------------------------------------------------------------------
#
# Normal backfill preserves resolved keys. Explicit requalification can
# upgrade a fallback identity when a stronger host lookup becomes available.
#
# If host identity lookup fails, sessions can fall back to remote_url.
# Requalification must merge that key with the canonical host ID without
# inflating project_count. All records below are invented.


class TestRequalifyRepairsADegradedKey:
    def _degraded(self, store):
        from self_improve.store import new_id

        for i in range(3):
            store.upsert_session({
                "file_path": f"s{i}", "source": "claude", "session_id": f"s{i}",
                "project_path": "/repos/skills", "first_ts": "2026-08-23T09:30:00Z",
                "last_ts": "2026-08-23T09:31:00Z", "lines_scanned": 10, "headless": 0, "is_subagent": 0, "mtime": 0.0, "file_size": 0, "bytes_scanned": 0, "malformed_lines": 0, "status": "ok", "error": "", "last_scanned_at": utc_now_iso(),
                "project_key": "remote:github.com/example/skills",
                "project_display": "example/skills",
                "project_key_method": "remote_url",
            })
        store.upsert_session({
            "file_path": "good", "source": "claude", "session_id": "good",
            "project_path": "/repos/skills", "first_ts": "2026-08-22T00:00:00Z",
            "last_ts": "2026-08-22T00:01:00Z", "lines_scanned": 10, "headless": 0, "is_subagent": 0, "mtime": 0.0, "file_size": 0, "bytes_scanned": 0, "malformed_lines": 0, "status": "ok", "error": "", "last_scanned_at": utc_now_iso(),
            "project_key": "github:42", "project_display": "example/skills",
            "project_key_method": "gh_repo_id",
        })
        store.commit()

    def _resolver(self):
        from self_improve.project_identity import ProjectIdentity

        def resolve(path, **kw):
            return ProjectIdentity("github:42", "example/skills", "gh_repo_id")

        return resolve

    def test_a_degraded_key_is_requalified_when_a_better_method_is_available(self, tmp_path):
        """Sabotage: drop the requalify branch. The 3 stay on remote_url and
        the repo keeps answering to two keys."""
        from self_improve.backfill import backfill_project_identity

        store = Store(tmp_path / "s.db")
        self._degraded(store)
        stats = backfill_project_identity(
            store, resolver=self._resolver(), requalify=True
        )
        store.commit()

        keys = {r["project_key"] for r in store.query("SELECT project_key FROM sessions")}
        assert keys == {"github:42"}, f"the repo still answers to {keys}"
        assert stats["sessions"]["requalified"] == 3

    def test_requalify_is_off_by_default(self, tmp_path):
        """Re-keying existing rows is a data migration, not a default."""
        from self_improve.backfill import backfill_project_identity

        store = Store(tmp_path / "s.db")
        self._degraded(store)
        backfill_project_identity(store, resolver=self._resolver())
        store.commit()

        keys = {r["project_key"] for r in store.query("SELECT project_key FROM sessions")}
        assert "remote:github.com/example/skills" in keys

    def test_it_never_downgrades_a_good_key(self, tmp_path):
        """If the resolver is ITSELF degraded — gh still missing — requalify
        must leave a gh_repo_id row alone rather than making things worse."""
        from self_improve.backfill import backfill_project_identity
        from self_improve.project_identity import ProjectIdentity

        store = Store(tmp_path / "s.db")
        self._degraded(store)

        def still_broken(path, **kw):
            return ProjectIdentity(
                "remote:github.com/example/skills", "example/skills", "remote_url"
            )

        stats = backfill_project_identity(store, resolver=still_broken, requalify=True)
        store.commit()

        good = store.query_one("SELECT project_key FROM sessions WHERE file_path='good'")
        assert good["project_key"] == "github:42", "a good key was downgraded"
        assert stats["sessions"]["requalified"] == 0

    def test_dry_run_changes_nothing(self, tmp_path):
        from self_improve.backfill import backfill_project_identity

        store = Store(tmp_path / "s.db")
        self._degraded(store)
        stats = backfill_project_identity(
            store, resolver=self._resolver(), requalify=True, dry_run=True
        )
        keys = {r["project_key"] for r in store.query("SELECT project_key FROM sessions")}
        assert "remote:github.com/example/skills" in keys
        assert stats["sessions"]["would_requalify"] == 3


class TestRequalifyReachesIncidentsToo:
    """Requalifying a session must also repair its incidents.

    Both tables must agree on project identity so lesson project counts remain valid."""

    def _degraded(self, store):
        store.upsert_session({
            "file_path": "s0", "source": "claude", "session_id": "s0",
            "project_path": "/repos/skills", "first_ts": "2026-08-23T09:30:00Z",
            "last_ts": "2026-08-23T09:31:00Z", "lines_scanned": 10,
            "headless": 0, "is_subagent": 0, "mtime": 0.0, "file_size": 0,
            "bytes_scanned": 0, "malformed_lines": 0, "status": "ok", "error": "",
            "last_scanned_at": utc_now_iso(),
            "project_key": "remote:github.com/example/skills",
            "project_display": "example/skills", "project_key_method": "remote_url",
        })
        store.insert_incident({
            "id": new_id(), "session_file": "s0", "session_id": "s0",
            "project_path": "/repos/skills", "ts": "2026-08-23T09:30:30Z",
            "signal_type": "correction", "matched_text": "x", "window": [],
            "project_key": "remote:github.com/example/skills",
        })
        store.commit()

    def _resolver(self):
        from self_improve.project_identity import ProjectIdentity

        def resolve(path, **kw):
            return ProjectIdentity("github:42", "example/skills", "gh_repo_id")

        return resolve

    def test_incidents_are_requalified_with_their_sessions(self, tmp_path):
        """Sabotage: leave the incidents loop untouched. Sessions move to
        github:42, incidents stay on remote:, and the two tables disagree."""
        from self_improve.backfill import backfill_project_identity

        store = Store(tmp_path / "s.db")
        self._degraded(store)
        stats = backfill_project_identity(
            store, resolver=self._resolver(), requalify=True
        )
        store.commit()

        sess = {r["project_key"] for r in store.query("SELECT project_key FROM sessions")}
        inc = {r["project_key"] for r in store.query("SELECT project_key FROM incidents")}
        assert sess == {"github:42"}
        assert inc == {"github:42"}, f"incidents left behind on {inc}"
        assert stats["incidents"]["requalified"] == 1

    def test_the_two_tables_never_end_up_disagreeing(self, tmp_path):
        """The property that actually matters, stated directly."""
        from self_improve.backfill import backfill_project_identity

        store = Store(tmp_path / "s.db")
        self._degraded(store)
        backfill_project_identity(store, resolver=self._resolver(), requalify=True)
        store.commit()

        mismatched = store.query(
            "SELECT i.id FROM incidents i JOIN sessions s "
            "ON i.session_file = s.file_path WHERE i.project_key <> s.project_key"
        )
        assert not mismatched, "an incident disagrees with its own session"

    def test_incidents_are_left_alone_without_the_flag(self, tmp_path):
        from self_improve.backfill import backfill_project_identity

        store = Store(tmp_path / "s.db")
        self._degraded(store)
        backfill_project_identity(store, resolver=self._resolver())
        store.commit()
        inc = {r["project_key"] for r in store.query("SELECT project_key FROM incidents")}
        assert inc == {"remote:github.com/example/skills"}
