"""Test incident selection under retention-risk and signal/recency policies.
Insertion order can reflect a bulk scan rather than event time. Transcript
retention and signal priority are separate policy choices with separate tests.
"""

from __future__ import annotations

import pytest

from self_improve.pipeline import order_incidents_for_mining
from self_improve.store import Store, new_id, utc_now_iso


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "s.db")


def _add(store, tmp_path, *, name, last_ts, score, exists=True):
    path = tmp_path / f"{name}.jsonl"
    if exists:
        path.write_text("{}\n")
    store.upsert_session(
        {
            "file_path": str(path), "source": "claude", "session_id": name,
            "project_path": "/p", "headless": 0, "is_subagent": 0,
            "first_ts": last_ts, "last_ts": last_ts, "mtime": 1.0,
            "file_size": 1, "bytes_scanned": 1, "lines_scanned": 1,
            "malformed_lines": 0, "status": "ok", "error": "",
            "last_scanned_at": utc_now_iso(),
        }
    )
    iid = new_id()
    store.insert_incident(
        {
            "id": iid, "session_file": str(path), "session_id": name,
            "project_path": "/p", "ts": last_ts, "signal_type": "correction",
            "matched_text": "m", "window": [], "score": score,
        }
    )
    store.commit()
    return iid


def _order(store, names_by_id, cfg=None):
    """Order with age_out_risk unless a cfg says otherwise.

    age_out_risk stopped being the default on 2026-08-17, but it is still a
    supported strategy, so these tests pin it rather than inherit it.
    """
    import dataclasses

    from self_improve.config import Config

    rows = store.query("SELECT * FROM incidents WHERE status = 'new'")
    cfg = cfg or dataclasses.replace(Config(), mine_order="age_out_risk")
    ordered, _ = order_incidents_for_mining(store, rows, cfg)
    return [names_by_id[i["id"]] for i in ordered]


def test_the_oldest_surviving_transcript_is_mined_first(store, tmp_path):
    """Oldest surviving = closest to deletion = most urgent to mine."""
    ids = {}
    ids[_add(store, tmp_path, name="recent", last_ts="2026-08-15T00:00:00Z", score=0.9)] = "recent"
    ids[_add(store, tmp_path, name="ancient", last_ts="2026-02-01T00:00:00Z", score=0.9)] = "ancient"

    assert _order(store, ids) == ["ancient", "recent"]


def test_an_already_deleted_transcript_sinks_to_the_back(store, tmp_path):
    """Nothing more to lose: it already mines from the archived window.

    Putting it first would spend an urgent slot on the one incident whose
    quality cannot degrade any further.
    """
    ids = {}
    ids[_add(store, tmp_path, name="gone", last_ts="2026-01-01T00:00:00Z", score=0.99, exists=False)] = "gone"
    ids[_add(store, tmp_path, name="alive", last_ts="2026-08-15T00:00:00Z", score=0.1)] = "alive"

    assert _order(store, ids) == ["alive", "gone"]


def test_score_breaks_ties_within_the_same_risk(store, tmp_path):
    """Age decides urgency; signal strength decides among equally urgent ones."""
    ids = {}
    ids[_add(store, tmp_path, name="weak", last_ts="2026-05-01T00:00:00Z", score=0.2)] = "weak"
    ids[_add(store, tmp_path, name="strong", last_ts="2026-05-01T00:00:00Z", score=0.95)] = "strong"

    assert _order(store, ids) == ["strong", "weak"]


def test_the_ordering_is_reported_not_implicit(store, tmp_path):
    """A reordering that changes what gets mined must be visible in the report."""
    ids = {}
    ids[_add(store, tmp_path, name="a", last_ts="2026-02-01T00:00:00Z", score=0.5)] = "a"
    ids[_add(store, tmp_path, name="b", last_ts="2026-08-01T00:00:00Z", score=0.5, exists=False)] = "b"

    from self_improve.config import Config

    import dataclasses

    rows = store.query("SELECT * FROM incidents WHERE status = 'new'")
    cfg = dataclasses.replace(Config(), mine_order="age_out_risk")
    _, stats = order_incidents_for_mining(store, rows, cfg)

    assert stats["order"] == "age_out_risk"
    assert stats["transcript_present"] == 1
    assert stats["transcript_already_gone"] == 1


def test_an_incident_cannot_outlive_its_session_row(store, tmp_path):
    """The orphan case I set out to handle is impossible by construction.

    incidents.session_file is a FOREIGN KEY onto sessions.file_path, so the DB
    refuses the row outright. Recorded as a test rather than defended against
    in code: a guard for a state the schema forbids is dead code that implies
    the invariant is weaker than it is.
    """
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        store.insert_incident(
            {
                "id": new_id(), "session_file": "/nope/none.jsonl",
                "session_id": "x", "project_path": "/p",
                "ts": "2026-03-01T00:00:00Z", "signal_type": "correction",
                "matched_text": "m", "window": [],
            }
        )
    store.conn.rollback()


def test_an_unknown_session_age_sorts_as_urgent_not_as_last(store, tmp_path):
    """The age_out_risk policy puts an unknown session age first.

    An empty timestamp sorts before known timestamps within the same availability
    class, so an incident with unknown age does not wait behind dated incidents."""
    ids = {}
    ids[_add(store, tmp_path, name="known", last_ts="2026-05-01T00:00:00Z", score=0.5)] = "known"
    ids[_add(store, tmp_path, name="unknown", last_ts="", score=0.5)] = "unknown"

    assert _order(store, ids)[0] == "unknown"


# ----------------------------------------------------------------------
# the two defensible orderings, and why the choice is the operator's
# ----------------------------------------------------------------------


def test_signal_and_recency_ordering_prefers_recent_high_signal_work(store, tmp_path):
    """Signal and recency ordering must prefer recent, high-signal incidents."""
    import dataclasses

    from self_improve.config import Config

    ids = {}
    ids[_add(store, tmp_path, name="old_strong", last_ts="2026-02-01T00:00:00Z", score=0.9)] = "old_strong"
    ids[_add(store, tmp_path, name="new_strong", last_ts="2026-08-15T00:00:00Z", score=0.9)] = "new_strong"
    ids[_add(store, tmp_path, name="new_weak", last_ts="2026-08-15T00:00:00Z", score=0.1)] = "new_weak"

    cfg = dataclasses.replace(Config(), mine_order="signal_then_recent")
    rows = store.query("SELECT * FROM incidents WHERE status = 'new'")
    ordered, stats = order_incidents_for_mining(store, rows, cfg)
    names = [ids[i["id"]] for i in ordered]

    assert stats["order"] == "signal_then_recent"
    assert names[0] in ("new_strong", "old_strong")
    assert names.index("new_strong") < names.index("new_weak"), "score must lead"
    assert names.index("new_strong") < names.index("old_strong"), "then recency"


def test_already_gone_still_sinks_under_either_strategy(store, tmp_path):
    """Uncontroversial in both: nothing left to lose, so nothing to rush."""
    import dataclasses

    from self_improve.config import Config

    for order in ("age_out_risk", "signal_then_recent"):
        d = tmp_path / order
        d.mkdir()
        s = Store(tmp_path / f"{order}.db")
        ids = {}
        ids[_add(s, d, name="gone", last_ts="2026-08-16T00:00:00Z", score=0.99, exists=False)] = "gone"
        ids[_add(s, d, name="here", last_ts="2026-01-01T00:00:00Z", score=0.01)] = "here"
        cfg = dataclasses.replace(Config(), mine_order=order)
        rows = s.query("SELECT * FROM incidents WHERE status = 'new'")
        ordered, _ = order_incidents_for_mining(s, rows, cfg)
        assert [ids[i["id"]] for i in ordered] == ["here", "gone"], order


def test_an_unknown_ordering_fails_loud(store, tmp_path):
    import dataclasses

    import pytest as _pytest

    from self_improve.config import Config, ConfigError

    _add(store, tmp_path, name="a", last_ts="2026-05-01T00:00:00Z", score=0.5)
    cfg = dataclasses.replace(Config(), mine_order="whatever-i-typed")
    rows = store.query("SELECT * FROM incidents WHERE status = 'new'")
    with _pytest.raises(ConfigError):
        order_incidents_for_mining(store, rows, cfg)


def test_unknown_age_sorts_last_under_signal_then_recent(store, tmp_path):
    """The mirror of the age-out case, and it was backwards.

    _desc_str inverts each codepoint so an ascending sort reads descending.
    But () compares LESS than any non-empty tuple, so the empty string - a
    session with no last_ts - sorted FIRST, while the comment directly above it
    said last. Timestamps are fixed-length so no other prefix case arises;
    the empty string is the one that does, and it is the one that was wrong.

    This is precisely the failure _desc_str's own docstring warns about: an
    inverted comparator produces a plausible-looking order and never looks
    wrong.
    """
    import dataclasses

    from self_improve.config import Config

    ids = {}
    ids[_add(store, tmp_path, name="known_old", last_ts="2026-01-01T00:00:00Z", score=0.5)] = "known_old"
    ids[_add(store, tmp_path, name="known_new", last_ts="2026-08-16T00:00:00Z", score=0.5)] = "known_new"
    ids[_add(store, tmp_path, name="unknown", last_ts="", score=0.5)] = "unknown"

    cfg = dataclasses.replace(Config(), mine_order="signal_then_recent")
    assert _order(store, ids, cfg) == ["known_new", "known_old", "unknown"]


def test_legacy_out_of_range_scores_are_normalized_not_left_dominant(store, tmp_path):
    """Normalize legacy raw recurrence counts before comparing detector scores.

    The invented legacy counts straddle the bounded modern score after the
    writer's recurrence curve is applied. A plain clamp would put both legacy
    rows above the modern row and erase the strength distinction."""
    import dataclasses

    from self_improve.config import Config

    ids = {}
    ids[_add(store, tmp_path, name="weak_legacy", last_ts="2026-01-01T00:00:00Z", score=2.0)] = "weak_legacy"
    ids[_add(store, tmp_path, name="modern", last_ts="2026-08-16T00:00:00Z", score=0.9)] = "modern"
    ids[_add(store, tmp_path, name="strong_legacy", last_ts="2026-01-02T00:00:00Z", score=128.0)] = "strong_legacy"

    cfg = dataclasses.replace(Config(), mine_order="signal_then_recent")
    order = _order(store, ids, cfg)

    assert order.index("modern") < order.index("weak_legacy"), (
        f"a 2-session legacy repeat outranked a 0.9 incident: {order}"
    )
    assert order.index("strong_legacy") < order.index("modern"), (
        f"a 128-session recurrence should still lead: {order}"
    )


def test_default_mine_order_is_signal_then_recent():
    """The default policy ranks by signal strength, then recency."""
    from self_improve.config import Config

    assert Config().mine_order == "signal_then_recent"
