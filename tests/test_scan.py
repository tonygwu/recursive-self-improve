"""Tests for scan.scan_all: incremental indexing over a fake SessionSource.

The SessionSource is faked in-memory (virtual files: bytes + mtime), and
detect_fn / build_window_fn are injected per scan_all's signature, so these
tests exercise scan.py's own logic only: skip rules, resume offsets, per-file
failure isolation, denylisting, cross-session promotion, and stats shape.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from self_improve.config import Config
from self_improve.scan import ScanStats, scan_all
from self_improve.sources.base import SessionFileInfo, TurnEvent
from self_improve.store import Store

RUN_ID = "run-1"


# ---------------------------------------------------------------------------
# Fake SessionSource: virtual files (bytes + mtime), line format = JSON dicts.
# ---------------------------------------------------------------------------


@dataclass
class FakeFile:
    content: bytes
    mtime: float = 1.0
    slug: str = ""
    is_subagent: bool = False


@dataclass(frozen=True)
class FakeParseStats:
    bytes_consumed: int
    lines_consumed: int
    malformed_lines: int


class FakeSource:
    """Implements the SessionSource protocol over an in-memory dict of files.

    A file whose content starts with b"RAISE" fails parse() loudly; a line
    that is not valid JSON is counted malformed and skipped; a final line
    without a trailing newline is treated as truncated (not consumed).
    """

    name = "fake"

    def __init__(self, files: dict[str, FakeFile]):
        self.files = files
        self.parse_calls: list[tuple[str, int]] = []
        self.parse_stats: FakeParseStats | None = None

    def discover(self):
        for path, f in self.files.items():
            yield SessionFileInfo(
                source="claude",
                file_path=path,
                mtime=f.mtime,
                size=len(f.content),
                project_slug=f.slug,
                is_subagent=f.is_subagent,
            )

    def parse(self, file_path: str, start_offset: int = 0):
        self.parse_calls.append((file_path, start_offset))
        f = self.files[file_path]
        if f.content.startswith(b"RAISE"):
            raise ValueError("corrupt transcript header")
        chunk = f.content[start_offset:]
        nl = chunk.rfind(b"\n")
        complete = chunk[: nl + 1] if nl >= 0 else b""
        lines_consumed = 0
        malformed = 0
        for line in complete.decode("utf-8").splitlines():
            lines_consumed += 1
            try:
                rec = json.loads(line)
            except ValueError:
                malformed += 1
                continue
            yield TurnEvent(
                source="claude",
                session_file=file_path,
                session_id=rec.get("sid", ""),
                project_path=rec.get("proj", ""),
                ts_utc=rec.get("ts", ""),
                role=rec.get("role", "human"),
                kind="message",
                text=rec.get("text", ""),
                headless=bool(rec.get("headless", False)),
            )
        self.parse_stats = FakeParseStats(
            bytes_consumed=start_offset + len(complete),
            lines_consumed=lines_consumed,
            malformed_lines=malformed,
        )


def jline(
    text: str,
    role: str = "human",
    ts: str = "",
    sid: str = "sess-a",
    proj: str = "/proj/alpha",
    headless: bool = False,
) -> bytes:
    rec = {"sid": sid, "proj": proj, "ts": ts, "role": role, "text": text}
    if headless:
        rec["headless"] = True
    return (json.dumps(rec) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# Injected detect_fn / build_window_fn fakes (per scan.py's documented
# filter_incidents/archive_trajectory contracts).
# ---------------------------------------------------------------------------


# These fakes mirror the REAL filter_incidents contract (CandidateIncident carries
# event_index, no ts; ErrorFingerprint's per-session counter is named count) —
# scan.py once read attributes that only existed on divergent fakes, which is
# exactly the bug class this file must not reintroduce.
@dataclass(frozen=True)
class FakeIncident:
    signal_type: str
    matched_text: str
    score: float
    event_index: int


@dataclass(frozen=True)
class FakeFingerprint:
    fingerprint: str
    count: int
    sample_text: str
    first_ts: str


@dataclass
class FakeDetectResult:
    incidents: list = field(default_factory=list)
    fingerprints: list = field(default_factory=list)
    dropped: dict = field(default_factory=dict)
    stats: dict = field(default_factory=dict)


def fake_detect(events, cfg):
    """Deterministic marker-driven detector.

    "CORRECT: ..." human text -> one correction incident; "ERR: <key>" ->
    per-key fingerprint (count = occurrences, first_ts = first marked event's
    data timestamp).
    """
    incidents = []
    err_counts: dict[str, int] = {}
    err_first_ts: dict[str, str] = {}
    for i, ev in enumerate(events):
        if ev.text.startswith("CORRECT:"):
            incidents.append(
                FakeIncident("correction", ev.text, 0.9, i)
            )
        elif ev.text.startswith("ERR:"):
            key = ev.text[len("ERR:"):].strip()
            err_counts[key] = err_counts.get(key, 0) + 1
            err_first_ts.setdefault(key, ev.ts_utc)
    fingerprints = [
        FakeFingerprint(f"fp::{k}", n, f"sample::{k}", err_first_ts[k])
        for k, n in err_counts.items()
    ]
    return FakeDetectResult(incidents, fingerprints, {})


def detect_with_dropped(dropped: dict):
    def _detect(events, cfg):
        r = fake_detect(events, cfg)
        return FakeDetectResult(r.incidents, r.fingerprints, dict(dropped))

    return _detect


def fake_window(events, incident, cfg):
    return [{"text": incident.matched_text, "events_seen": len(events)}]


def make_store(tmp_path) -> Store:
    return Store(tmp_path / "state.db")


CFG = Config()


# ---------------------------------------------------------------------------
# First scan indexes everything
# ---------------------------------------------------------------------------

FILE_A = "/virtual/alpha.jsonl"
FILE_B = "/virtual/beta.jsonl"

CONTENT_A = (
    jline("working on it", role="assistant", ts="2026-08-01T00:00:01Z")
    + jline("CORRECT: no, use uv run", ts="2026-08-01T00:00:05Z")
    + jline("ERR: ModuleNotFoundError", role="tool_result", ts="2026-08-01T00:00:07Z")
)
CONTENT_B = (
    jline("hello", ts="2026-08-02T09:00:00Z", sid="sess-b", proj="/proj/beta")
    + b"this is not json\n"
    + jline(
        "ERR: TypeError",
        role="tool_result",
        ts="2026-08-02T09:00:09Z",
        sid="sess-b",
        proj="/proj/beta",
        headless=True,
    )
)


def two_file_source() -> FakeSource:
    return FakeSource(
        {FILE_A: FakeFile(CONTENT_A), FILE_B: FakeFile(CONTENT_B)}
    )


def test_first_scan_indexes_all_files(tmp_path):
    store = make_store(tmp_path)
    source = two_file_source()
    stats = scan_all(
        store,
        CFG,
        [source],
        RUN_ID,
        detect_fn=detect_with_dropped({"correction": 2}),
        build_window_fn=fake_window,
    )

    assert stats.files_attempted == 2
    assert stats.files_succeeded == 2
    assert stats.files_failed == 0
    assert stats.files_attempted == stats.files_succeeded + stats.files_failed
    assert stats.files_skipped_unchanged == 0
    assert stats.files_denylisted == 0
    assert stats.lines_scanned == 6
    assert stats.malformed_lines == 1
    assert stats.incidents_by_signal == {"correction": 1}
    assert stats.fingerprints_recorded == 2
    assert stats.dropped_by_cap == {"correction": 4}  # 2 per file, accumulated
    assert stats.error_taxonomy == {}
    assert stats.promoted_repeated_errors == 0

    # session rows: metadata from event DATA, bytes_scanned == full file.
    sa = store.get_session(FILE_A)
    assert sa is not None
    assert sa["status"] == "ok"
    assert sa["source"] == "claude"
    assert sa["session_id"] == "sess-a"
    assert sa["project_path"] == "/proj/alpha"
    assert sa["headless"] == 0
    assert sa["is_subagent"] == 0
    assert sa["first_ts"] == "2026-08-01T00:00:01Z"
    assert sa["last_ts"] == "2026-08-01T00:00:07Z"
    assert sa["bytes_scanned"] == len(CONTENT_A)
    assert sa["file_size"] == len(CONTENT_A)
    assert sa["lines_scanned"] == 3
    assert sa["malformed_lines"] == 0
    assert sa["error"] == ""

    sb = store.get_session(FILE_B)
    assert sb["status"] == "partial"  # malformed lines present
    assert sb["malformed_lines"] == 1
    assert sb["lines_scanned"] == 3
    assert sb["headless"] == 1
    assert sb["session_id"] == "sess-b"

    # incident row with the built window persisted as strict JSON.
    incidents = store.query("SELECT * FROM incidents")
    assert len(incidents) == 1
    inc = incidents[0]
    assert inc["session_file"] == FILE_A
    assert inc["session_id"] == "sess-a"
    assert inc["project_path"] == "/proj/alpha"
    assert inc["signal_type"] == "correction"
    assert inc["matched_text"] == "CORRECT: no, use uv run"
    assert inc["ts"] == "2026-08-01T00:00:05Z"
    assert inc["score"] == 0.9
    assert inc["status"] == "new"
    assert inc["run_id"] == RUN_ID
    assert json.loads(inc["window_json"]) == [
        {"text": "CORRECT: no, use uv run", "events_seen": 3}
    ]

    # fingerprint rows.
    fps = store.query(
        "SELECT * FROM error_fingerprints ORDER BY fingerprint"
    )
    assert len(fps) == 2
    assert fps[0]["fingerprint"] == "fp::ModuleNotFoundError"
    assert fps[0]["session_file"] == FILE_A
    assert fps[0]["count_in_session"] == 1
    assert fps[0]["sample_text"] == "sample::ModuleNotFoundError"
    assert fps[0]["first_ts"] == "2026-08-01T00:00:07Z"
    assert fps[1]["fingerprint"] == "fp::TypeError"
    assert fps[1]["session_file"] == FILE_B


# ---------------------------------------------------------------------------
# Unchanged files skipped
# ---------------------------------------------------------------------------


def test_unchanged_files_skipped_on_rescan(tmp_path):
    store = make_store(tmp_path)
    source = two_file_source()
    scan_all(store, CFG, [source], RUN_ID,
             detect_fn=fake_detect, build_window_fn=fake_window)

    stats2 = scan_all(store, CFG, [source], "run-2",
                      detect_fn=fake_detect, build_window_fn=fake_window)
    assert stats2.files_skipped_unchanged == 2
    assert stats2.files_attempted == 0
    assert stats2.files_succeeded == 0
    assert stats2.files_failed == 0
    assert stats2.lines_scanned == 0
    assert stats2.incidents_by_signal == {}
    # No files re-parsed: exactly the two first-run parse calls remain.
    assert source.parse_calls == [(FILE_A, 0), (FILE_B, 0)]
    # And no duplicated incidents.
    n = store.query_one("SELECT COUNT(*) AS n FROM incidents")["n"]
    assert n == 1


# ---------------------------------------------------------------------------
# Appended file re-parsed from stored byte offset
# ---------------------------------------------------------------------------


def test_appended_file_reparsed_from_offset(tmp_path):
    store = make_store(tmp_path)
    path = "/virtual/grow.jsonl"
    content1 = (
        jline("start", role="assistant", ts="2026-08-03T10:00:00Z", sid="sess-g")
        + jline("CORRECT: first fix", ts="2026-08-03T10:00:05Z", sid="sess-g")
        + jline("ERR: flaky", role="tool_result", ts="2026-08-03T10:00:06Z", sid="sess-g")
    )
    source = FakeSource({path: FakeFile(content1, mtime=1.0)})
    scan_all(store, CFG, [source], RUN_ID,
             detect_fn=fake_detect, build_window_fn=fake_window)
    bytes1 = len(content1)
    assert store.get_session(path)["bytes_scanned"] == bytes1

    appended = (
        jline("CORRECT: second fix", ts="2026-08-03T11:00:00Z", sid="sess-g")
        + jline("ERR: flaky", role="tool_result", ts="2026-08-03T11:00:01Z", sid="sess-g")
    )
    source.files[path] = FakeFile(content1 + appended, mtime=2.0)

    stats2 = scan_all(store, CFG, [source], "run-2",
                      detect_fn=fake_detect, build_window_fn=fake_window)

    # Parse resumed exactly at the stored offset, not from 0.
    assert source.parse_calls == [(path, 0), (path, bytes1)]
    assert stats2.files_attempted == 1
    assert stats2.files_succeeded == 1
    assert stats2.files_skipped_unchanged == 0
    assert stats2.lines_scanned == 2  # only the appended lines this pass

    row = store.get_session(path)
    assert row["bytes_scanned"] == len(content1) + len(appended)
    assert row["lines_scanned"] == 5  # cumulative
    assert row["first_ts"] == "2026-08-03T10:00:00Z"  # kept from first pass
    assert row["last_ts"] == "2026-08-03T11:00:01Z"   # advanced by append
    assert row["session_id"] == "sess-g"
    assert row["status"] == "ok"

    # Old incident not duplicated; new incident from appended events only.
    incidents = store.query(
        "SELECT matched_text, run_id, window_json FROM incidents ORDER BY created_at"
    )
    assert [i["matched_text"] for i in incidents] == [
        "CORRECT: first fix",
        "CORRECT: second fix",
    ]
    assert incidents[1]["run_id"] == "run-2"
    # Second pass's detect/window saw only the 2 appended events.
    assert json.loads(incidents[1]["window_json"]) == [
        {"text": "CORRECT: second fix", "events_seen": 2}
    ]

    # Fingerprint accumulated across passes; earliest data ts kept.
    fp = store.query_one(
        "SELECT * FROM error_fingerprints WHERE fingerprint = ?", ("fp::flaky",)
    )
    assert fp["count_in_session"] == 2
    assert fp["first_ts"] == "2026-08-03T10:00:06Z"


# ---------------------------------------------------------------------------
# A source whose parse() raises: error row, scan continues, taxonomy counted
# ---------------------------------------------------------------------------


def test_parse_error_recorded_and_scan_continues(tmp_path):
    store = make_store(tmp_path)
    bad = "/virtual/bad.jsonl"
    good = "/virtual/good.jsonl"
    source = FakeSource(
        {
            bad: FakeFile(b"RAISE not even close to jsonl\n", mtime=1.0),
            good: FakeFile(
                jline("CORRECT: still works", ts="2026-08-04T00:00:00Z", sid="sess-ok"),
                mtime=1.0,
            ),
        }
    )
    stats = scan_all(store, CFG, [source], RUN_ID,
                     detect_fn=fake_detect, build_window_fn=fake_window)

    assert stats.files_attempted == 2
    assert stats.files_failed == 1
    assert stats.files_succeeded == 1
    assert stats.files_attempted == stats.files_succeeded + stats.files_failed
    assert stats.error_taxonomy == {"parse:ValueError": 1}

    bad_row = store.get_session(bad)
    assert bad_row["status"] == "error"
    assert bad_row["error"] == "parse: ValueError: corrupt transcript header"
    assert bad_row["mtime"] == 0.0  # deliberately mismatched: retried next run

    # Scan continued past the failure: the good file was fully indexed.
    good_row = store.get_session(good)
    assert good_row["status"] == "ok"
    n = store.query_one(
        "SELECT COUNT(*) AS n FROM incidents WHERE session_file = ?", (good,)
    )["n"]
    assert n == 1

    # Next run: failed file retried (mtime=0 mismatch), good file skipped.
    stats2 = scan_all(store, CFG, [source], "run-2",
                      detect_fn=fake_detect, build_window_fn=fake_window)
    assert stats2.files_attempted == 1
    assert stats2.files_failed == 1
    assert stats2.files_skipped_unchanged == 1
    assert stats2.error_taxonomy == {"parse:ValueError": 1}


# ---------------------------------------------------------------------------
# Denylisted project_path dropped
# ---------------------------------------------------------------------------


def test_denylisted_project_path_dropped(tmp_path):
    cfg = Config(denylist_substrings=("secret-proj",))
    store = make_store(tmp_path)
    path = "/virtual/deny.jsonl"
    content = (
        jline(
            "CORRECT: should never be mined",
            ts="2026-08-05T00:00:00Z",
            sid="sess-d",
            proj="/Users/x/secret-proj/app",
        )
        + jline(
            "ERR: leaky",
            role="tool_result",
            ts="2026-08-05T00:00:01Z",
            sid="sess-d",
            proj="/Users/x/secret-proj/app",
        )
    )
    source = FakeSource({path: FakeFile(content)})
    stats = scan_all(store, cfg, [source], RUN_ID,
                     detect_fn=fake_detect, build_window_fn=fake_window)

    assert stats.files_denylisted == 1
    assert stats.denylisted_by_substring == {"secret-proj": 1}
    assert stats.files_succeeded == 1  # denylisted is a subset of succeeded
    assert stats.incidents_by_signal == {}
    assert stats.fingerprints_recorded == 0

    # Session row recorded (status ok) so it is skipped-unchanged next run...
    row = store.get_session(path)
    assert row["status"] == "ok"
    assert row["project_path"] == "/Users/x/secret-proj/app"
    # ...but zero incidents and zero fingerprints were persisted.
    assert store.query_one("SELECT COUNT(*) AS n FROM incidents")["n"] == 0
    assert store.query_one("SELECT COUNT(*) AS n FROM error_fingerprints")["n"] == 0

    stats2 = scan_all(store, cfg, [source], "run-2",
                      detect_fn=fake_detect, build_window_fn=fake_window)
    assert stats2.files_skipped_unchanged == 1
    assert stats2.files_attempted == 0


# ---------------------------------------------------------------------------
# Cross-session repeated-error promotion
# ---------------------------------------------------------------------------


def test_repeated_error_promoted_across_sessions_once(tmp_path):
    assert CFG.repeated_error_min_sessions == 2  # test relies on the default
    store = make_store(tmp_path)
    f1 = "/virtual/one.jsonl"
    f2 = "/virtual/two.jsonl"
    source = FakeSource(
        {
            f1: FakeFile(
                jline("ERR: boom", role="tool_result",
                      ts="2026-08-06T01:00:00Z", sid="sess-1", proj="/proj/p1")
                + jline("ERR: boom", role="tool_result",
                        ts="2026-08-06T01:00:05Z", sid="sess-1", proj="/proj/p1")
            ),
            f2: FakeFile(
                jline("ERR: boom", role="tool_result",
                      ts="2026-08-07T02:00:00Z", sid="sess-2", proj="/proj/p2")
                + jline("ERR: solo", role="tool_result",
                        ts="2026-08-07T02:00:01Z", sid="sess-2", proj="/proj/p2")
            ),
        }
    )
    stats = scan_all(store, CFG, [source], RUN_ID,
                     detect_fn=fake_detect, build_window_fn=fake_window)

    assert stats.promoted_repeated_errors == 1
    assert stats.incidents_by_signal == {"repeated_error": 1}

    promoted = store.query(
        "SELECT * FROM incidents WHERE signal_type = 'repeated_error'"
    )
    assert len(promoted) == 1
    inc = promoted[0]
    assert inc["matched_text"] == "fp::boom"  # dedupe key
    assert inc["session_file"] == f1  # anchored at earliest-by-data-time session
    assert inc["session_id"] == "sess-1"
    assert inc["ts"] == "2026-08-06T01:00:00Z"
    # Normalized into the documented [0, 1] band rather than the raw count:
    # the raw value made repeated_error outrank every other detector in any
    # score-based ordering. Still monotone in the session count.
    from self_improve.scan import repeated_error_score

    assert inc["score"] == repeated_error_score(2)
    assert 0.0 <= inc["score"] <= 1.0
    assert repeated_error_score(2) < repeated_error_score(9)
    assert inc["run_id"] == RUN_ID
    assert json.loads(inc["window_json"]) == [
        {
            "session_file": f1,
            "project_path": "/proj/p1",
            "ts": "2026-08-06T01:00:00Z",
            "count_in_session": 2,
            "text": "sample::boom",
        },
        {
            "session_file": f2,
            "project_path": "/proj/p2",
            "ts": "2026-08-07T02:00:00Z",
            "count_in_session": 1,
            "text": "sample::boom",
        },
    ]

    # Single-session fingerprint not promoted.
    assert store.query_one(
        "SELECT COUNT(*) AS n FROM incidents WHERE matched_text = ?", ("fp::solo",)
    )["n"] == 0

    # Re-scan: not duplicated.
    stats2 = scan_all(store, CFG, [source], "run-2",
                      detect_fn=fake_detect, build_window_fn=fake_window)
    assert stats2.promoted_repeated_errors == 0
    assert store.query_one(
        "SELECT COUNT(*) AS n FROM incidents WHERE signal_type = 'repeated_error'"
    )["n"] == 1


# ---------------------------------------------------------------------------
# ScanStats.as_dict shape
# ---------------------------------------------------------------------------


def test_scan_stats_as_dict_shape(tmp_path):
    store = make_store(tmp_path)
    path = "/virtual/tiny.jsonl"
    content = (
        jline("CORRECT: tiny", ts="2026-08-08T00:00:00Z", sid="sess-t")
        + jline("ERR: tiny", role="tool_result", ts="2026-08-08T00:00:01Z", sid="sess-t")
    )
    source = FakeSource({path: FakeFile(content)})
    stats = scan_all(store, CFG, [source], RUN_ID,
                     detect_fn=fake_detect, build_window_fn=fake_window)

    d = stats.as_dict()
    assert isinstance(d, dict)
    assert d == {
        "files_attempted": 1,
        "files_succeeded": 1,
        "files_failed": 0,
        "files_skipped_unchanged": 0,
        "files_denylisted": 0,
        "events_denylisted": 0,
        "denylisted_by_substring": {},
        "lines_scanned": 2,
        "malformed_lines": 0,
        # WHY, not just how many. Added 2026-08-22: both sources had computed
        # these per-cause counters all along and scan.py read only the integer,
        # so a run could report thousands of malformed lines and not say what
        # a single one of them was.
        "malformed_by_cause": {},
        "unknown_lines": 0,
        "unknown_line_types": {},
        "skipped_line_types": {},
        "incidents_by_signal": {"correction": 1},
        "fingerprints_recorded": 1,
        "incidents_deduped": 0,
        # Resolution method is recorded per session so a degraded resolution
        # (no gh -> remote_url, or a vanished dir -> unresolved) is visible in
        # the run report rather than silently collapsing projects.
        "project_key_methods": {"unresolved": 1},
        "promoted_repeated_errors": 0,
        "dropped_by_cap": {},
        # Detector "skipped, not guessed" counters. Present and empty here
        # because the fake detector reports none; the point of the key is that
        # scan.py forwards them at all, which it previously did not.
        "detector_taxonomy": {},
        "error_taxonomy": {},
        # Scan measurement. A source without line records still records an
        # observation, marked unavailable, so its absence is explicit.
        "measurement": {"observations_recorded": 1, "projections_unavailable": 1},
    }
    # Every counter is a plain int, every breakdown a plain dict (JSON-ready).
    for key, value in d.items():
        assert isinstance(value, (int, dict)), key

    # A fresh ScanStats round-trips to all-zero/empty with the same keys.
    empty = ScanStats().as_dict()
    assert set(empty) == set(d)
    assert all(v in (0, {}) for v in empty.values())


# ---------------------------------------------------------------------------
# rescan must not destroy evidence a re-scan cannot regenerate
# ---------------------------------------------------------------------------


def test_rescan_keeps_incidents_whose_transcript_is_gone(tmp_path):
    """Preserve incident archives when the original transcript cannot be re-read.
    A rescan can replace reproducible incidents but cannot recreate deleted evidence.
    """
    from self_improve.scan import mark_for_rescan
    from self_improve.store import Store, new_id, utc_now_iso

    store = make_store(tmp_path)
    live = tmp_path / "live.jsonl"
    live.write_text("{}\n")
    gone = str(tmp_path / "deleted.jsonl")  # never created

    for fp in (str(live), gone):
        store.upsert_session(
            {
                "file_path": fp, "source": "claude", "session_id": fp,
                "project_path": "/p", "headless": 0, "is_subagent": 0,
                "first_ts": "", "last_ts": "", "mtime": 1.0, "file_size": 10,
                "bytes_scanned": 10, "lines_scanned": 1, "malformed_lines": 0,
                "status": "ok", "error": "", "last_scanned_at": utc_now_iso(),
            }
        )
        store.insert_incident(
            {
                "id": new_id(), "session_file": fp, "session_id": fp,
                "project_path": "/p", "ts": "2026-08-10T00:00:00Z",
                "signal_type": "correction", "matched_text": "m",
                "window": [{"role": "user", "text": "irreplaceable"}],
            }
        )
    store.commit()

    stats = mark_for_rescan(store, CFG)

    remaining = store.query("SELECT * FROM incidents")
    kept = [i for i in remaining if i["session_file"] == gone]
    assert kept, "the only surviving evidence for a deleted transcript was erased"
    assert "irreplaceable" in kept[0]["window_json"]
    # ...and the still-present file WAS reset, so rescan still does its job.
    assert not [i for i in remaining if i["session_file"] == str(live)]
    assert stats.get("sessions_skipped_transcript_gone") == 1


def test_promoted_repeated_error_score_stays_in_the_documented_range(tmp_path):
    """Repeated-error scores must stay in [0, 1] and increase with recurrence.

    Raw recurrence counts would outrank bounded scores from other detectors."""
    from self_improve.scan import repeated_error_score

    scores = [repeated_error_score(n) for n in (2, 3, 5, 10, 50, 128, 5000)]
    assert all(0.0 <= s <= 1.0 for s in scores), scores
    # Still monotone: more sessions must still outrank fewer.
    assert scores == sorted(scores)
    assert scores[0] < scores[-1], "normalisation flattened the signal entirely"


def test_repeated_error_score_is_comparable_to_other_detectors(tmp_path):
    """It must land in the same band the other detectors occupy, or ranking is
    still decided by which detector fired rather than by strength."""
    from self_improve.scan import repeated_error_score

    # instruction_edit scores 0.9; a 2-session repeat must not beat it.
    assert repeated_error_score(2) < 0.9
    # An invented strong repeat can rank above that score.
    assert repeated_error_score(128) > 0.9


# ---------------------------------------------------------------------------
# every failure phase, not just `parse`
# ---------------------------------------------------------------------------


class _Boom(Exception):
    """Distinctive failure so the taxonomy key is unambiguous."""


@pytest.mark.parametrize(
    "phase,kwargs",
    [
        ("detect", {"detect_fn": lambda events, cfg: (_ for _ in ()).throw(_Boom("x"))}),
        ("window", {"build_window_fn": lambda e, i, c: (_ for _ in ()).throw(_Boom("x"))}),
    ],
)
def test_each_failure_phase_is_isolated_and_named(phase, kwargs, tmp_path):
    """scan.py wraps six phases in _PhaseError; the suite only ever exercised
    `parse`.

    Each handler has three jobs and only the first is obvious: name the phase in
    the taxonomy, roll the transaction back, and leave `bytes_scanned`
    unadvanced so the file is RETRIED. A handler that advances the offset would
    skip that file's content permanently, silently, on every future run.
    """
    store = make_store(tmp_path)
    path = "/virtual/a.jsonl"
    content = jline("CORRECT: x", ts="2026-08-08T00:00:00Z", sid="s1")
    source = FakeSource({path: FakeFile(content)})
    base = {"detect_fn": fake_detect, "build_window_fn": fake_window}
    base.update(kwargs)

    stats = scan_all(store, CFG, [source], RUN_ID, **base)

    assert stats.files_attempted == 1
    assert stats.files_failed == 1
    assert stats.files_succeeded == 0
    assert stats.error_taxonomy == {f"{phase}:_Boom": 1}, stats.error_taxonomy

    row = store.get_session(path)
    assert row is not None, "a failed file must still be recorded"
    assert row["status"] == "error"
    assert row["bytes_scanned"] == 0, (
        "a failed file advanced its resume offset; its content would be skipped "
        "permanently on every future run"
    )
    assert store.query("SELECT * FROM incidents") == [], "partial writes survived"


def test_a_mid_file_failure_rolls_back_the_partial_writes(tmp_path):
    """The rollback assertion only means something if something was WRITTEN.

    My first version of this failed in `detect`, before any insert — so
    deleting the rollback entirely left the test green. The failure has to land
    AFTER a partial write, which is what a per-incident phase like `window`
    does on its second call: incident one is already inserted when incident two
    blows up.
    """
    store = make_store(tmp_path)
    path = "/virtual/multi.jsonl"
    content = (
        jline("CORRECT: one", ts="2026-08-08T00:00:01Z", sid="s1")
        + jline("CORRECT: two", ts="2026-08-08T00:00:02Z", sid="s1")
    )
    source = FakeSource({path: FakeFile(content)})

    calls = {"n": 0}

    def window_fails_on_second(events, incident, cfg):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise _Boom("second incident")
        return fake_window(events, incident, cfg)

    stats = scan_all(store, CFG, [source], RUN_ID,
                     detect_fn=fake_detect, build_window_fn=window_fails_on_second)

    assert calls["n"] >= 2, "fixture did not reach a second incident"
    assert stats.files_failed == 1
    assert stats.error_taxonomy == {"window:_Boom": 1}
    assert store.query("SELECT * FROM incidents") == [], (
        "the first incident survived the failure — a partial file leaked into "
        "the DB, and the file will be re-scanned and duplicate it"
    )
    assert store.get_session(path)["bytes_scanned"] == 0


def test_a_failed_file_is_retried_on_the_next_run(tmp_path):
    """The point of not advancing the offset: the next run picks it up."""
    store = make_store(tmp_path)
    path = "/virtual/b.jsonl"
    source = FakeSource({path: FakeFile(jline("CORRECT: x", ts="2026-08-08T00:00:00Z"))})

    def boom(events, cfg):
        raise _Boom("first run only")

    scan_all(store, CFG, [source], RUN_ID, detect_fn=boom, build_window_fn=fake_window)
    assert store.get_session(path)["status"] == "error"

    stats2 = scan_all(store, CFG, [source], RUN_ID,
                      detect_fn=fake_detect, build_window_fn=fake_window)

    assert stats2.files_skipped_unchanged == 0, "the failed file was skipped, not retried"
    assert stats2.files_succeeded == 1
    assert store.get_session(path)["status"] == "ok"


def detect_with_stats(stats: dict):
    def _detect(events, cfg):
        r = fake_detect(events, cfg)
        return FakeDetectResult(r.incidents, r.fingerprints, {}, dict(stats))

    return _detect


def test_scan_aggregates_the_detector_taxonomy(tmp_path):
    """Aggregate detector counters across sessions, including reasons an edit path could not be resolved."""
    store = make_store(tmp_path)
    stats = scan_all(
        store,
        CFG,
        [two_file_source()],
        RUN_ID,
        detect_fn=detect_with_stats(
            {"instruction_edit_missing_path": 3, "friction_edit_missing_file": 1}
        ),
        build_window_fn=fake_window,
    )
    # two files, so each per-session counter lands twice
    assert stats.detector_taxonomy == {
        "instruction_edit_missing_path": 6,
        "friction_edit_missing_file": 2,
    }, stats.detector_taxonomy


def test_a_detector_result_without_stats_fails_loud(tmp_path):
    """Silently accepting a missing .stats would re-hide the discarded-counter bug.

    scan.py originally read it with getattr(..., None) — lenient in exactly the
    way this field exists to prevent, since a detect() that stopped returning
    stats would contribute nothing and say nothing.
    """
    import dataclasses as _dc

    @_dc.dataclass
    class NoStatsResult:
        incidents: list = _dc.field(default_factory=list)
        fingerprints: list = _dc.field(default_factory=list)
        dropped: dict = _dc.field(default_factory=dict)

    def _detect(events, cfg):
        r = fake_detect(events, cfg)
        return NoStatsResult(r.incidents, r.fingerprints, {})

    store = make_store(tmp_path)
    stats = scan_all(
        store, CFG, [two_file_source()], RUN_ID,
        detect_fn=_detect, build_window_fn=fake_window,
    )
    # Per-file failures are recorded, not raised — but they must be recorded.
    assert stats.files_failed == 2, stats.as_dict()
    assert any("detect" in k or "AttributeError" in k for k in stats.error_taxonomy), (
        stats.error_taxonomy
    )


# ---------------------------------------------------------------------------
# A failure count with no cause is the shape that hides parser blind spots
# ---------------------------------------------------------------------------
#
# Carry both source parsers' cause taxonomy through scan statistics so unknown
# record types can be distinguished from malformed JSON and decoding failures.


@dataclass
class _TaxonomyStats:
    """A parse_stats carrying the taxonomies a real source reports.

    A dataclass on purpose: `_parse_stats_field` accepts only a Mapping or a
    dataclass and raises TypeError otherwise, which is the contract both real
    sources satisfy.
    """

    bytes_consumed: int
    lines_consumed: int
    malformed_lines: int
    error_taxonomy: dict
    unknown_lines: int
    unknown_taxonomy: dict


class _TaxonomySource:
    """Minimal SessionSource whose parse_stats carries the taxonomies."""

    name = "fake"

    def __init__(self, path, stats):
        self._path, self._stats = path, stats
        self.parse_stats = None

    def discover(self):
        yield SessionFileInfo(
            source="claude",
            file_path=self._path,
            mtime=1.0,
            size=self._stats.bytes_consumed,
            project_slug="",
            is_subagent=False,
        )

    def parse(self, file_path: str, start_offset: int = 0):
        self.parse_stats = self._stats
        return iter(())


def _scan_with(tmp_path, stats):
    src = _TaxonomySource("/virtual/tax.jsonl", stats)
    return scan_all(make_store(tmp_path), CFG, [src], RUN_ID,
                    detect_fn=fake_detect, build_window_fn=fake_window)


def test_malformed_lines_are_reported_with_their_cause(tmp_path):
    """Sabotage: delete the `for key, count in dict(cause_tax)` loop in
    scan.py. The count survives, the cause vanishes, and this fails."""
    got = _scan_with(tmp_path, _TaxonomyStats(
        bytes_consumed=100, lines_consumed=100, malformed_lines=3,
        error_taxonomy={"malformed_json": 2, "decode_error": 1},
        unknown_lines=0, unknown_taxonomy={},
    ))
    assert got.malformed_lines == 3
    assert got.malformed_by_cause == {"malformed_json": 2, "decode_error": 1}


def test_an_unknown_record_type_is_counted_and_named_but_not_called_malformed(tmp_path):
    """The two halves that matter, together.

    Not malformed: calling a well-formed line corrupt marks its session
    `partial` and buries the news. Still counted: the moment a new record type
    carries human text, this number is the only thing that shows it.

    Sabotage: drop `unknown_n`/`unknown_tax` from scan.py's _parse_stats read.
    """
    got = _scan_with(tmp_path, _TaxonomyStats(
        bytes_consumed=100, lines_consumed=100, malformed_lines=0,
        error_taxonomy={},
        unknown_lines=4, unknown_taxonomy={"atis-latch": 3, "frame-link": 1},
    ))
    assert got.malformed_lines == 0, "an unknown type is not a corrupt line"
    assert got.unknown_lines == 4
    assert got.unknown_line_types == {"atis-latch": 3, "frame-link": 1}


def test_a_source_reporting_no_taxonomy_still_scans(tmp_path):
    """The taxonomies are optional on the SessionSource contract."""

    @dataclass
    class Bare:
        bytes_consumed: int = 50
        lines_consumed: int = 10
        malformed_lines: int = 2

    got = _scan_with(tmp_path, Bare())  # no taxonomy attributes at all
    assert got.malformed_lines == 2
    assert got.malformed_by_cause == {}
    assert got.unknown_line_types == {}


# ---------------------------------------------------------------------------
# The Codex half of the taxonomy read, with a taxonomy that is not empty
# ---------------------------------------------------------------------------
#
# scan.py resolves the per-cause breakdown with
#   getattr(stats, "error_taxonomy") or getattr(stats, "malformed_taxonomy")
# because the two sources named it differently. Claude has `error_taxonomy`;
# Codex has `malformed_taxonomy`, so the SECOND arm is Codex's only path in.
#
# Feed invented malformed records through the parser so the Codex arm must
# propagate a nonempty taxonomy. An empty result would not distinguish an
# absent attribute from an empty one.


def test_the_codex_taxonomy_reaches_scan_stats(tmp_path):
    """Sabotage: drop the `or getattr(raw_stats, "malformed_taxonomy", None)`
    arm in scan.py. Codex's causes vanish and only the bare count survives."""
    import json as _json

    from self_improve.config import Config
    from self_improve.sources.codex import CodexSource

    sessions = tmp_path / "codex-sessions"
    sessions.mkdir()
    (sessions / "rollout-2026-08-23T01-00-00-abc.jsonl").write_text(
        _json.dumps({"type": "session_meta", "payload": {"id": "s1", "cwd": "/tmp/x"}})
        + "\n"
        + "{ this is not json at all\n"
        + _json.dumps(["not", "an", "object"]) + "\n"
        + _json.dumps(
            {"type": "event_msg", "payload": {"type": "user_message", "message": "hi"}}
        )
        + "\n",
        encoding="utf-8",
    )
    cfg = Config(
        codex_sessions_dir=str(sessions),
        codex_archived_dir=str(tmp_path / "absent"),
        state_dir=str(tmp_path / "state"),
    )
    source = CodexSource(cfg)
    stats = scan_all(
        make_store(tmp_path), cfg, [source], RUN_ID,
        detect_fn=fake_detect, build_window_fn=fake_window,
    )

    assert stats.malformed_lines == 2
    assert stats.malformed_by_cause == {"json_error": 1, "bad_envelope": 1}, (
        f"Codex's causes did not reach the run stats: {stats.malformed_by_cause}"
    )
    # Codex has no unknown-type concept; the other arm must stay empty, not
    # inherit Claude's.
    assert stats.unknown_line_types == {}


def test_the_two_sources_name_their_taxonomy_differently_and_both_are_read():
    """The reason scan.py has a two-arm getattr at all. If either source is
    renamed to match the other, this says so before the counts go quiet."""
    from self_improve.sources.claude_code import ParseStats as ClaudeStats
    from self_improve.sources.codex import CodexParseStats

    assert hasattr(ClaudeStats(), "error_taxonomy")
    assert not hasattr(ClaudeStats(), "malformed_taxonomy")
    assert hasattr(CodexParseStats(), "malformed_taxonomy")
    assert not hasattr(CodexParseStats(), "error_taxonomy")


# ---------------------------------------------------------------------------
# The invariant the docstring has always claimed
# ---------------------------------------------------------------------------


def test_scan_stats_rejects_accounting_that_does_not_add_up():
    """`files_attempted == files_succeeded + files_failed` had been documented
    since ScanStats was written, and exactly one test with one two-file fixture
    ever checked it. A documented invariant nothing enforces is a comment.

    A file counted as attempted that reaches neither outcome is the shape of a
    lost `continue` or an exception between the counter bumps, and it surfaces
    to the operator as a total that quietly does not add up.
    """
    from self_improve.scan import ScanError, ScanStats

    ok = ScanStats(files_attempted=3, files_succeeded=2, files_failed=1)
    ok.check_invariants()  # does not raise

    lost = ScanStats(files_attempted=3, files_succeeded=2, files_failed=0)
    with pytest.raises(ScanError, match="does not add up"):
        lost.check_invariants()


def test_denylisted_files_cannot_outnumber_the_successes():
    """Denylisted files are parsed and recorded, deliberately not mined — so
    they are a SUBSET of the successes, not a separate bucket."""
    from self_improve.scan import ScanError, ScanStats

    ScanStats(files_attempted=2, files_succeeded=2, files_denylisted=2).check_invariants()
    bad = ScanStats(files_attempted=2, files_succeeded=1, files_failed=1, files_denylisted=2)
    with pytest.raises(ScanError, match="exceeds"):
        bad.check_invariants()


def test_a_real_scan_actually_checks_its_own_accounting(tmp_path, monkeypatch):
    """The wiring, not the method.

    The two tests above drive `check_invariants` directly, and deleting the
    call from `scan_all` left both of them green — which is the whole reason
    this repo runs sabotages. A guard nothing calls is a guard that does not
    exist.
    """
    from self_improve import scan as scan_mod

    calls = []
    original = scan_mod.ScanStats.check_invariants

    def spy(self):
        calls.append(
            (self.files_attempted, self.files_succeeded, self.files_failed)
        )
        return original(self)

    monkeypatch.setattr(scan_mod.ScanStats, "check_invariants", spy)
    store = make_store(tmp_path)
    scan_all(
        store, CFG, [two_file_source()], RUN_ID,
        detect_fn=detect_with_dropped({"correction": 2}),
        build_window_fn=fake_window,
    )
    assert calls, "scan_all finished without checking its own accounting"
    attempted, succeeded, failed = calls[-1]
    assert attempted == succeeded + failed == 2, calls
