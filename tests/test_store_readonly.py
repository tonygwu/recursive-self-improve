"""A read-only Store must not migrate during construction or queries."""

from __future__ import annotations

import sqlite3

import pytest

from self_improve.store import MIGRATIONS, Store


def test_read_only_store_does_not_migrate(tmp_path):
    db = tmp_path / "s.db"
    Store(db).close() if hasattr(Store(db), "close") else None
    # Roll the DB back to a pre-latest migration state.
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM schema_migrations WHERE name = ?", (MIGRATIONS[-1][0],))
    conn.commit()
    conn.close()

    before = _applied(db)
    Store(db, read_only=True)
    assert _applied(db) == before, "a read-only Store migrated the database"


def test_a_normal_store_still_migrates(tmp_path):
    """The complement — read_only must be the only thing that changed."""
    db = tmp_path / "s.db"
    Store(db)
    assert MIGRATIONS[-1][0] in _applied(db)


def test_read_only_store_cannot_write(tmp_path):
    db = tmp_path / "s.db"
    Store(db)
    ro = Store(db, read_only=True)
    with pytest.raises(sqlite3.OperationalError):
        ro.conn.execute("CREATE TABLE t (x INT)")


def test_read_only_store_can_read(tmp_path):
    db = tmp_path / "s.db"
    Store(db)
    ro = Store(db, read_only=True)
    assert ro.query("SELECT name FROM schema_migrations") != []


def test_read_only_store_on_a_missing_db_fails_loud(tmp_path):
    with pytest.raises(FileNotFoundError):
        Store(tmp_path / "nope.db", read_only=True)


def _applied(db) -> set[str]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The Supabase-portability rule, enforced across the whole repo
# ---------------------------------------------------------------------------
#
# AGENTS.md and store.py both state it as a HARD rule, and it was enforced by a
# lint over exactly one file — tests/test_dashboard_queries.py's
# test_no_sqlite_only_sql, which walks queries.py. 19 modules contain SQL.
#
# The rule is cheap to keep and expensive to reinstate: the whole point is that
# the driver swap is one module, and a single `json_each` buried in a report
# query would only be found when someone tried to move.
#
# Zero violations existed when this was written, so it is a guard rather than a
# fix.

_BANNED_SQL = {
    "json_each": "SQLite-only JSON table function; join through the real table instead",
    "json_tree": "SQLite-only JSON table function",
    "group_concat": "SQLite spelling; Postgres is string_agg",
    "strftime(": "SQLite date function; Postgres is to_char",
    "julianday(": "SQLite-only date arithmetic",
    "AUTOINCREMENT": "SQLite-only; this schema uses TEXT uuid4 primary keys",
    "OR REPLACE": "use an explicit ON CONFLICT upsert, which Postgres also has",
    "ATTACH ": "SQLite-only",
}


def _sql_literals():
    """Every string constant in src/ that is actually SQL, with its location."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src" / "self_improve"
    found = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            upper = node.value.upper()
            if "SELECT " in upper or "INSERT INTO" in upper or "UPDATE " in upper:
                found.append((path, node.lineno, node.value))
    return found


def test_no_module_uses_sqlite_only_sql():
    """Sabotage: put `group_concat(x)` in any SELECT in src/. This fails."""
    literals = _sql_literals()
    assert len(literals) > 40, (
        f"the SQL guard found only {len(literals)} statements across the repo; "
        "it is probably not looking at anything"
    )
    violations = []
    for path, line, sql in literals:
        for banned, why in _BANNED_SQL.items():
            if banned in sql:
                rel = path.relative_to(path.parents[3])
                violations.append(f"{rel}:{line} uses {banned!r} — {why}")
    assert not violations, "SQLite-only SQL:\n  " + "\n  ".join(violations)


def test_the_guard_covers_more_than_the_dashboard():
    """The lint it generalises walked one file. Prove this one does not."""
    modules = {path.name for path, _, _ in _sql_literals()}
    assert "store.py" in modules
    assert "report.py" in modules
    assert len(modules) >= 10, f"only {len(modules)} modules inspected: {sorted(modules)}"


def test_the_banned_list_is_detected_when_present():
    """A lint that cannot fire is not a lint. Feed it a known-bad statement."""
    bad = "SELECT group_concat(name) FROM sessions"
    assert any(b in bad for b in _BANNED_SQL), "the banned list would miss group_concat"


# --- migrate=False: the write mode that still must not move the schema -------
#
# Intent writers need a handle that can insert without migrating the schema.
# read_only=True already forbids migration; test the writable half separately.


def test_a_writable_store_can_refuse_to_migrate(tmp_path):
    db = tmp_path / "s.db"
    Store(db)
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM schema_migrations WHERE name = ?", (MIGRATIONS[-1][0],))
    conn.commit()
    conn.close()

    before = _applied(db)
    Store(db, migrate=False)
    assert _applied(db) == before, "migrate=False migrated the database"


def test_a_non_migrating_store_can_still_write(tmp_path):
    """The point of the mode. read_only=True cannot do this."""
    db = tmp_path / "s.db"
    Store(db)
    w = Store(db, migrate=False)
    w.conn.execute("CREATE TABLE t (x INT)")
    w.conn.commit()
    assert w.query("SELECT x FROM t") == []


def test_a_non_migrating_store_refuses_to_create_the_database(tmp_path):
    """Creating one would hand back an empty, unmigrated DB that accepts writes.

    That is the silent-wrong-answer shape: the caller believes it wrote to the
    operator's state, and it wrote to a file nobody reads.
    """
    with pytest.raises(FileNotFoundError):
        Store(tmp_path / "nope.db", migrate=False)


def test_read_only_and_migrate_true_is_a_contradiction_and_raises(tmp_path):
    """Fail loud rather than silently picking one of the two meanings."""
    db = tmp_path / "s.db"
    Store(db)
    with pytest.raises(ValueError, match="read_only"):
        Store(db, read_only=True, migrate=True)
