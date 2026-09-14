"""A partial session remains partial until its failed bytes are re-read.
A clean incremental tail cannot clear an earlier parse failure. A full clean
read can clear the flag, including when the transcript has stopped changing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from self_improve.scan import _parse_cause_json, mark_partial_for_reverify


class _Store:
    """Minimal Store stand-in: rows in, updates recorded."""

    def __init__(self, rows):
        self.rows = {r["file_path"]: dict(r) for r in rows}
        self.updates: list[tuple[str, dict]] = []
        self.committed = False
        self.deletes: list[str] = []

    def query(self, sql, params=()):
        assert "status = 'partial'" in sql, sql
        return [r for r in self.rows.values() if r.get("status") == "partial"]

    def update(self, table, key_col, key, values):
        assert table == "sessions" and key_col == "file_path"
        self.updates.append((key, dict(values)))
        self.rows[key].update(values)

    def commit(self):
        self.committed = True


# ----------------------------------------------------------------------
# the guarded reader
# ----------------------------------------------------------------------


def test_a_cause_column_that_is_not_json_names_the_session():
    """A bare json.loads here would surface as a character offset with no
    owner. Sixth instance of that class in this repo."""
    with pytest.raises(ValueError, match=r"/p/a\.jsonl.*not valid JSON"):
        _parse_cause_json({"file_path": "/p/a.jsonl", "malformed_by_cause": "{oops"})


def test_valid_json_of_the_wrong_shape_also_fails():
    """A JSON list parses fine and then merges into the cause totals as
    garbage. Guard the type as well as the parse."""
    with pytest.raises(ValueError, match=r"not an object.*list"):
        _parse_cause_json({"file_path": "/p/a.jsonl", "malformed_by_cause": "[1,2]"})
    with pytest.raises(ValueError, match=r"not an object.*str"):
        _parse_cause_json({"file_path": "/p/a.jsonl", "malformed_by_cause": '"x"'})


def test_absent_and_empty_are_both_just_empty():
    assert _parse_cause_json(None) == {}
    assert _parse_cause_json({}) == {}
    assert _parse_cause_json({"malformed_by_cause": ""}) == {}
    assert _parse_cause_json({"malformed_by_cause": '{"malformed_json": 2}'}) == {
        "malformed_json": 2
    }


# ----------------------------------------------------------------------
# the targeted re-verify
# ----------------------------------------------------------------------


def test_reverify_resets_the_offset_and_deletes_nothing(tmp_path):
    """Reverify partial sessions without deleting any incident."""
    f = tmp_path / "a.jsonl"
    f.write_text("{}\n")
    store = _Store([
        {"file_path": str(f), "status": "partial"},
        {"file_path": str(tmp_path / "clean.jsonl"), "status": "ok"},
    ])
    out = mark_partial_for_reverify(store)

    assert out["marked_for_full_reread"] == 1
    assert out["incidents_deleted"] == 0
    assert store.deletes == [], "re-verify must never delete an incident"
    (key, vals), = store.updates
    assert key == str(f)
    assert vals == {"bytes_scanned": 0, "lines_scanned": 0, "mtime": 0.0}
    assert "malformed_lines" not in vals, (
        "malformed_lines is the honest record that lines WERE lost; the "
        "re-read re-derives it, clearing it here would destroy the evidence"
    )
    assert store.committed


def test_a_session_whose_transcript_is_gone_is_skipped_and_counted(tmp_path):
    """There is nothing to re-read, so resetting the offset would only
    guarantee a future no-op. A silent skip and a silent reset look the same
    from outside, so it is counted."""
    store = _Store([{"file_path": str(tmp_path / "vanished.jsonl"), "status": "partial"}])
    out = mark_partial_for_reverify(store)
    assert out == {
        "partial_sessions": 1, "marked_for_full_reread": 0,
        "skipped_transcript_gone": 1, "incidents_deleted": 0,
    }
    assert store.updates == []


def test_an_ok_session_is_never_touched(tmp_path):
    f = tmp_path / "fine.jsonl"
    f.write_text("{}\n")
    store = _Store([{"file_path": str(f), "status": "ok"}])
    out = mark_partial_for_reverify(store)
    assert out["partial_sessions"] == 0 and store.updates == []


# ----------------------------------------------------------------------
# the flag itself, through the real scan
# ----------------------------------------------------------------------

from self_improve.scan import scan_all  # noqa: E402
from tests.test_scan import (  # noqa: E402
    CFG, FakeFile, FakeSource, detect_with_dropped, fake_window, jline, make_store,
)

BAD = b"this is not json\n"
PATH = "/virtual/sticky.jsonl"


def _scan(store, source, run_id):
    return scan_all(
        store, CFG, [source], run_id,
        detect_fn=detect_with_dropped({}), build_window_fn=fake_window,
    )


def _row(store):
    return store.query_one("SELECT * FROM sessions WHERE file_path = ?", (PATH,))


def test_a_bad_line_flags_the_session(tmp_path):
    src = FakeSource({PATH: FakeFile(jline("a") + BAD)})
    store = make_store(tmp_path)
    _scan(store, src, "run-1")
    r = _row(store)
    assert r["status"] == "partial" and r["malformed_lines"] == 1


def test_a_clean_TAIL_may_not_clear_the_flag(tmp_path):
    """THE trap, and the reason the naive fix is worse than the bug.

    An incremental pass reads only the bytes appended since last time. A clean
    tail says nothing about the bytes it never looked at, so deriving the flag
    from THIS pass alone would clear it the next time the file grew, having
    never re-read the line that failed.
    """
    f = FakeFile(jline("a") + BAD)
    src = FakeSource({PATH: f})
    store = make_store(tmp_path)
    _scan(store, src, "run-1")
    assert _row(store)["status"] == "partial"

    f.content += jline("b") + jline("c")   # append only clean lines
    f.mtime = 2.0
    _scan(store, src, "run-2")

    assert src.parse_calls[-1][1] > 0, "this must be an INCREMENTAL read, or it proves nothing"
    r = _row(store)
    assert r["status"] == "partial", (
        "a clean tail cleared a flag set by bytes this pass never re-read"
    )
    assert r["malformed_lines"] == 1


def test_a_FULL_re_read_of_a_now_clean_file_clears_the_flag(tmp_path):
    """A full clean re-read clears an earlier partial flag."""
    f = FakeFile(jline("a") + BAD)
    src = FakeSource({PATH: f})
    store = make_store(tmp_path)
    _scan(store, src, "run-1")
    assert _row(store)["status"] == "partial"

    f.content = jline("a") + jline("b")     # the bad line is gone
    store.update("sessions", "file_path", PATH,
                 {"bytes_scanned": 0, "lines_scanned": 0, "mtime": 0.0})
    store.commit()
    f.mtime = 3.0
    _scan(store, src, "run-3")

    assert src.parse_calls[-1][1] == 0, "this must be a FULL read, or it proves nothing"
    r = _row(store)
    assert r["status"] == "ok", "a full clean re-read must clear the flag"
    assert r["malformed_lines"] == 0


def test_a_full_re_read_that_is_still_bad_stays_flagged(tmp_path):
    """The control. Without it, a rule that always cleared on a full read
    would satisfy the test above forever."""
    f = FakeFile(jline("a") + BAD)
    src = FakeSource({PATH: f})
    store = make_store(tmp_path)
    _scan(store, src, "run-1")
    store.update("sessions", "file_path", PATH,
                 {"bytes_scanned": 0, "lines_scanned": 0, "mtime": 0.0})
    store.commit()
    f.mtime = 3.0
    _scan(store, src, "run-3")
    assert src.parse_calls[-1][1] == 0
    assert _row(store)["status"] == "partial"


def test_the_cause_column_is_written_and_is_valid_json(tmp_path):
    """The count survived a scan and the cause did not. Same bug report.py
    documents one level up, in the sessions table."""
    src = FakeSource({PATH: FakeFile(jline("a") + BAD)})
    store = make_store(tmp_path)
    _scan(store, src, "run-1")
    raw = _row(store)["malformed_by_cause"]
    assert isinstance(json.loads(raw), dict), raw
    # A clean session carries an empty object, never NULL: absent and
    # "nothing was wrong" must not be the same value.
    src2 = FakeSource({"/virtual/clean.jsonl": FakeFile(jline("x"))})
    _scan(store, src2, "run-2")
    clean = store.query_one(
        "SELECT malformed_by_cause, status FROM sessions WHERE file_path = ?",
        ("/virtual/clean.jsonl",),
    )
    assert clean["status"] == "ok" and json.loads(clean["malformed_by_cause"]) == {}


# ----------------------------------------------------------------------
# honesty about what the rewrite did and did not change
# ----------------------------------------------------------------------


def test_the_new_status_rule_is_behaviour_preserving_on_every_reachable_state():
    """Compare status rules on states consistent with the prior derivation.
    Explicit full re-verification supplies the clearing path for dormant files.
    """
    def old(resumed, prev_malformed, new_malformed, prev_status):
        total = (prev_malformed if resumed else 0) + new_malformed
        return "partial" if total > 0 else "ok"

    def new(resumed, prev_malformed, new_malformed, prev_status):
        if resumed:
            return "partial" if (new_malformed > 0 or prev_status == "partial") else "ok"
        return "partial" if new_malformed > 0 else "ok"

    checked = 0
    for resumed in (False, True):
        for pm in (0, 1, 5):
            for nm in (0, 1, 3):
                # The DB invariant: status is derived from the count, so
                # `partial` and `malformed_lines > 0` always agree.
                prev_status = "partial" if pm > 0 else "ok"
                checked += 1
                assert old(resumed, pm, nm, prev_status) == new(
                    resumed, pm, nm, prev_status
                ), (resumed, pm, nm, prev_status)
    assert checked == 18, checked  # the scan found something, not vacuously true

    # And the states where they DO differ are exactly the inconsistent ones.
    assert old(True, 1, 0, "ok") == "partial" and new(True, 1, 0, "ok") == "ok"


@dataclass(frozen=True)
class _TaxStats:
    """FakeParseStats plus the per-file cause taxonomy the real parser emits.

    A DATACLASS, not a plain object: scan._parse_stats_field accepts a Mapping
    or a dataclass and raises TypeError on anything else. The first version of
    this fake was a plain class and the scan correctly recorded the session as
    `error` — the guard working, caught here rather than in production.
    """

    bytes_consumed: int
    lines_consumed: int
    malformed_lines: int
    error_taxonomy: dict


class TaxSource(FakeSource):
    """A source that reports WHY its lines were malformed, as the real
    ClaudeCodeSource does. The shared fake in test_scan.py reports only a
    count, which is precisely why the first version of the cause test passed
    with the column hardcoded to '{}'."""

    def parse(self, file_path, start_offset=0):
        events = list(super().parse(file_path, start_offset))
        base = self.parse_stats
        self.parse_stats = _TaxStats(
            base.bytes_consumed, base.lines_consumed, base.malformed_lines,
            {"malformed_json": base.malformed_lines} if base.malformed_lines else {},
        )
        yield from events


def test_the_cause_is_recorded_per_session_not_only_per_run(tmp_path):
    """Persist both the malformed-line count and its cause taxonomy per session."""
    src = TaxSource({PATH: FakeFile(jline("a") + BAD + BAD)})
    store = make_store(tmp_path)
    _scan(store, src, "run-1")
    r = _row(store)
    assert r["status"] == "partial" and r["malformed_lines"] == 2
    assert json.loads(r["malformed_by_cause"]) == {"malformed_json": 2}, (
        "the session knows HOW MANY lines failed but not WHY"
    )


def test_causes_from_an_incremental_pass_are_merged_not_replaced(tmp_path):
    """Two passes, two bad lines, one total. Replacing would lose the first."""
    f = FakeFile(jline("a") + BAD)
    src = TaxSource({PATH: f})
    store = make_store(tmp_path)
    _scan(store, src, "run-1")
    assert json.loads(_row(store)["malformed_by_cause"]) == {"malformed_json": 1}

    f.content += BAD
    f.mtime = 2.0
    _scan(store, src, "run-2")
    assert src.parse_calls[-1][1] > 0, "must be incremental, or it proves nothing"
    assert json.loads(_row(store)["malformed_by_cause"]) == {"malformed_json": 2}
