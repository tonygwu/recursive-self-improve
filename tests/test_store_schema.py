"""The insert column lists and the real table schemas, reconciled.

`Store.upsert_session` spells out its columns. That tuple and the CREATE TABLE
(plus every ALTER in MIGRATIONS) are two lists that must agree, and on
2026-09-01 they did not: migration 0008 added `sessions.malformed_by_cause` and
the tuple was not updated, so scan.py computed the cause taxonomy correctly and
the INSERT silently discarded it. Every row took the column default, no
exception was raised, and the only symptom was a column that was always '{}'.

Eleventh instance of this class in this repo, and the reason the rule is "a
list that must match another list gets a test that reads BOTH", never a comment
saying to keep them in sync.
"""

from __future__ import annotations

import pytest

from self_improve.config import Config
from self_improve.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(Config(state_dir=str(tmp_path)).state_path("state.db"))


def _table_columns(store: Store, table: str) -> set[str]:
    return {r[1] for r in store.conn.execute(f"PRAGMA table_info({table})")}


def test_upsert_session_writes_every_column_the_table_has(store):
    """The regression, driven rather than grepped.

    Build a row with a DISTINGUISHABLE value in every column the table has,
    upsert it, and read every column back. A column the insert forgot comes
    back as its schema default instead of the sentinel — which is exactly what
    `malformed_by_cause` did, silently, for every row.

    Reading the source instead would be guessing: the first version of this
    test sliced the `cols = (...)` literal out of the function and died on the
    indentation, and a version that survived would still pass on a name that
    merely CONTAINS the right substring.
    """
    cols = [(r[1], (r[2] or "TEXT").upper()) for r in store.conn.execute(
        "PRAGMA table_info(sessions)"
    )]
    assert cols, "the scanner found no columns, so this test cannot fail"

    row, expected = {}, {}
    for i, (name, decl) in enumerate(cols):
        if name == "file_path":
            value = "/p/sentinel.jsonl"
        elif "INT" in decl:
            value = 100 + i
        elif "REAL" in decl or "FLOA" in decl or "DOUB" in decl:
            value = 100.0 + i
        else:
            value = f"sentinel-{name}"
        row[name] = value
        expected[name] = value

    store.upsert_session(row)
    store.commit()
    got = store.query_one(
        "SELECT * FROM sessions WHERE file_path = ?", ("/p/sentinel.jsonl",)
    )
    dropped = [n for n, _ in cols if got[n] != expected[n]]
    assert not dropped, (
        "upsert_session did not write these columns, so they silently hold the "
        f"schema default on every row: {dropped}"
    )


def test_the_new_column_actually_round_trips(store):
    """The behavioural half. The reconciliation above would pass on a tuple
    that is correct and an INSERT that ignores it."""
    store.upsert_session({
        "file_path": "/p/a.jsonl", "source": "claude", "session_id": "s",
        "project_path": "/p", "headless": 0, "is_subagent": 0,
        "first_ts": "", "last_ts": "", "mtime": 1.0, "file_size": 10,
        "bytes_scanned": 10, "lines_scanned": 2, "malformed_lines": 2,
        "malformed_by_cause": '{"malformed_json": 2}',
        "status": "partial", "error": "", "last_scanned_at": "2026-09-01T00:00:00Z",
    })
    store.commit()
    row = store.query_one(
        "SELECT malformed_by_cause, status FROM sessions WHERE file_path = ?",
        ("/p/a.jsonl",),
    )
    assert row["malformed_by_cause"] == '{"malformed_json": 2}', (
        "the value reached the row dict and did not reach the table"
    )
    assert row["status"] == "partial"


def test_a_missing_substantive_column_still_fails_loud(store):
    """Omitting required session fields must raise. Derived identity metadata and the malformed-cause JSON have explicit defaults."""
    with pytest.raises(KeyError):
        store.upsert_session({"file_path": "/p/b.jsonl", "source": "claude"})


def test_the_optional_json_column_defaults_to_something_a_reader_can_parse(store):
    """A caller that omits `malformed_by_cause` must not poison every later
    reader.

    The three identity columns default to "". This one is strict JSON, so the
    same blanket default would make `scan._parse_cause_json` raise on any row
    written by a caller that did not set it — turning one omission here into a
    failure somewhere else entirely, which is the shape this repo keeps losing
    to. The default is '{}' and this drives the real reader to prove it.
    """
    from self_improve.scan import _parse_cause_json

    store.upsert_session({
        "file_path": "/p/no-cause.jsonl", "source": "claude", "session_id": "s",
        "project_path": "/p", "headless": 0, "is_subagent": 0,
        "first_ts": "", "last_ts": "", "mtime": 1.0, "file_size": 1,
        "bytes_scanned": 1, "lines_scanned": 1, "malformed_lines": 0,
        "status": "ok", "error": "", "last_scanned_at": "2026-09-01T00:00:00Z",
    })  # malformed_by_cause deliberately absent
    store.commit()
    row = store.query_one(
        "SELECT * FROM sessions WHERE file_path = ?", ("/p/no-cause.jsonl",)
    )
    assert row["malformed_by_cause"] == "{}", row["malformed_by_cause"]
    assert _parse_cause_json(dict(row)) == {}, (
        "the default this writer chose is not readable by the reader that "
        "consumes it"
    )


def test_a_database_with_a_newer_migration_is_refused_by_the_ordinary_open(tmp_path):
    """BL-27: DEPLOYMENT.md said older code tolerates a newer database. Since
    7fec020 the ordinary open calls migration_plan, which refuses unknown
    names; read-only and non-migrating opens still read it."""
    import pytest as _pytest
    from self_improve.store import Store

    path = tmp_path / "state.db"
    store = Store(path)
    store.insert("schema_migrations", {"name": "9999_from_newer_code", "applied_at": "2026-10-12T00:00:00Z"})
    store.commit()
    store.close()
    with _pytest.raises(ValueError, match="unknown names"):
        Store(path)
    Store(path, migrate=False).close()
    Store(path, read_only=True).close()


def test_store_as_a_context_manager_commits_on_success_and_rolls_back_on_error(tmp_path):
    """BL-54: Store had close() but no context manager. Leaving the block
    closes the connection; an exception discards the block's pending writes
    instead of committing them as close() would."""
    import sqlite3

    import pytest

    from self_improve.store import Store

    db = tmp_path / "state.db"
    with Store(db) as store:
        store.insert("runs", {"id": "kept", "started": "2026-10-12T00:00:00Z", "status": "ok"})
    with pytest.raises(sqlite3.ProgrammingError):
        store.conn.execute("SELECT 1")
    with pytest.raises(RuntimeError, match="invented failure"):
        with Store(db) as store:
            store.insert("runs", {"id": "dropped", "started": "2026-10-12T00:00:00Z", "status": "ok"})
            raise RuntimeError("invented failure")
    with pytest.raises(sqlite3.ProgrammingError):
        store.conn.execute("SELECT 1")
    with Store(db, read_only=True) as reader:
        assert [r["id"] for r in reader.query("SELECT id FROM runs ORDER BY id")] == ["kept"]
