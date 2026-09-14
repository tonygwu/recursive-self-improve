"""One policy must govern the writer, Review, and the nightly report."""

from __future__ import annotations

import json
from contextlib import closing
from dataclasses import replace

import pytest

from self_improve.apply import apply_proposal
from self_improve.config import Config
from self_improve.dashboard import queries
from self_improve.store import Store
from tests.test_apply import OLD, NEW, insert_proposal, make_diff


@pytest.fixture
def state(tmp_path):
    cfg = Config(state_dir=str(tmp_path / "state"))
    with closing(Store(tmp_path / "state" / "state.db")) as store:
        yield store, cfg


def test_default_off_is_enforced_by_the_final_writer(state, tmp_path):
    store, cfg = state
    target = tmp_path / "CLAUDE.md"
    target.write_text(OLD)
    proposal = insert_proposal(store, target=target, diff=make_diff(OLD, NEW))

    outcome = apply_proposal(store, cfg, proposal)

    assert outcome["outcome"] == "held"
    assert outcome["reason"] == "class_disabled"
    assert target.read_text() == OLD
    assert not (tmp_path / "state" / "snapshots").exists()


def test_ungated_cannot_write_even_when_legacy_global_switch_is_on(state, tmp_path):
    store, cfg = state
    cfg = replace(cfg, auto_apply=True)
    target = tmp_path / "CLAUDE.md"
    target.write_text(OLD)
    proposal = insert_proposal(store, target=target, diff=make_diff(OLD, NEW), status="ungated")

    outcome = apply_proposal(store, cfg, proposal)

    assert outcome["outcome"] == "held"
    assert outcome["reason"] == "status_not_appliable"
    assert target.read_text() == OLD


def test_all_classes_start_off_and_hook_enable_is_refused(state):
    from self_improve.execution_policy import policy_snapshot, set_class_policy, PolicyError

    store, _ = state
    policy = policy_snapshot(store)
    assert set(policy["classes"]) == {"global", "project", "skill", "hook"}
    assert not any(row["enabled"] for row in policy["classes"].values())
    with pytest.raises(PolicyError, match="hooks always require"):
        set_class_policy(store, "hook", True)
    assert not policy_snapshot(store)["classes"]["hook"]["enabled"]
    assert store.query("SELECT * FROM execution_policy_events") == []


def test_advisory_evidence_does_not_lock_switch_and_restart_preserves_it(state):
    from self_improve.execution_policy import policy_snapshot, set_class_policy

    store, _ = state
    assert store.query("SELECT * FROM proposals") == []
    set_class_policy(store, "project", True)
    with closing(Store(store.db_path, read_only=True)) as reader:
        classes = policy_snapshot(reader)["classes"]
        assert classes["project"]["enabled"]
        assert not classes["global"]["enabled"]
    events = store.query("SELECT * FROM execution_policy_events")
    assert len(events) == 1 and events[0]["actor"] == "user"


def test_only_new_passing_proposals_in_enabled_class_are_automatic(state, tmp_path):
    from self_improve.execution_policy import policy_snapshot, automatic_eligibility, set_class_policy

    store, _ = state
    old = insert_proposal(store, target=tmp_path / "old.md", diff=make_diff(OLD, NEW))
    set_class_policy(store, "global", True)
    new = insert_proposal(store, target=tmp_path / "new.md", diff=make_diff(OLD, NEW))
    policy = policy_snapshot(store)

    assert automatic_eligibility(old, policy)["reason"] == "predates_class_enable"
    assert automatic_eligibility(new, policy)["allowed"]
    assert automatic_eligibility(new, policy, review_only=True)["reason"] == "review_only"
    for status in ("pending", "ungated", "gated_fail", "inconclusive", "approved_user", "applied", "unknown"):
        assert not automatic_eligibility({**new, "status": status}, policy)["allowed"]
    assert not automatic_eligibility({**new, "target_kind": "skill"}, policy)["allowed"]
    for action in ("convert_to_hook", "delete_human_line"):
        assert not automatic_eligibility({**new, "action": action}, policy)["allowed"]
    assert not automatic_eligibility({**new, "target_kind": "unknown"}, policy)["allowed"]


def test_enabled_policy_reaches_the_real_writer(state, tmp_path):
    from self_improve.execution_policy import set_class_policy

    store, cfg = state
    set_class_policy(store, "global", True)
    target = tmp_path / "CLAUDE.md"
    target.write_text(OLD)
    proposal = insert_proposal(store, target=target, diff=make_diff(OLD, NEW))
    assert apply_proposal(store, cfg, proposal)["outcome"] == "applied"
    assert target.read_text() == NEW


def test_disabled_and_ungated_items_are_reachable_in_review_and_badge(state, tmp_path):
    store, cfg = state
    proposals = [insert_proposal(store, target=tmp_path / f"{s}.md", diff=make_diff(OLD, NEW), status=s)
                 for s in ("gated_pass", "ungated", "pending", "inconclusive")]
    cfg = replace(cfg, global_claude_md=str(tmp_path / "global.md"))
    payload = queries.review_queue(store, cfg)
    displayed = {p["id"] for family in payload["families"] for p in family["proposals"]}
    assert displayed == {p["id"] for p in proposals}
    assert queries.inbox(store, cfg)["count"] == payload["count"] == 4
    assert payload["auto_apply_pending"] == 0


def test_explicit_review_only_run_stays_in_review_when_class_enabled(state, tmp_path):
    from self_improve.execution_policy import set_class_policy

    store, cfg = state
    set_class_policy(store, "global", True)
    store.insert("runs", {"id": "manual-run", "started": "2026-09-13T01:00:00Z", "stats_json": json.dumps({"review_only": True})})
    proposal = insert_proposal(store, target=tmp_path / "rule.md", diff=make_diff(OLD, NEW))
    store.update("proposals", "id", proposal["id"], {"run_id": "manual-run"})
    store.commit()
    assert proposal["id"] in queries.waiting_proposal_ids(store, cfg)


def test_unknown_class_and_non_boolean_policy_do_not_change_settings(state):
    from self_improve.execution_policy import set_class_policy, PolicyError

    store, _ = state
    for cls, enabled in (("typo", True), ("global", "false"), ("project", 1)):
        with pytest.raises(PolicyError):
            set_class_policy(store, cls, enabled)
    assert store.query("SELECT * FROM execution_policy_events") == []


def test_prior_rollback_is_visible_in_review_even_with_automatic_class_enabled(state, tmp_path):
    from self_improve.execution_policy import set_class_policy

    store, cfg = state
    set_class_policy(store, "global", True)
    old = insert_proposal(store, target=tmp_path / "rule.md", diff=make_diff(OLD, NEW), status="rolled_back")
    new = insert_proposal(store, target=tmp_path / "rule.md", diff=make_diff(OLD, NEW))
    store.update("proposals", "id", new["id"], {"learning_id": old["learning_id"]})
    store.commit()
    assert new["id"] in queries.waiting_proposal_ids(store, cfg)
    assert queries.inbox(store, cfg)["auto_apply_pending"] == 0


def test_a_stale_caller_cannot_write_a_rejected_proposal(state, tmp_path):
    from self_improve.apply import ApplyError
    from self_improve.execution_policy import set_class_policy

    store, cfg = state
    set_class_policy(store, "global", True)
    target = tmp_path / "rule.md"
    target.write_text(OLD)
    stale = insert_proposal(store, target=target, diff=make_diff(OLD, NEW))
    store.update("proposals", "id", stale["id"], {"status": "rejected_user"})
    store.commit()
    with pytest.raises(ApplyError, match="changed"):
        apply_proposal(store, cfg, stale)
    assert target.read_text() == OLD
    assert store.query_one("SELECT status FROM proposals WHERE id=?", (stale["id"],))["status"] == "rejected_user"


@pytest.mark.parametrize("status", ["applied", "approved_user", "rejected_user", "rolled_back", "superseded"])
def test_refusing_a_terminal_proposal_preserves_its_state(state, tmp_path, status):
    store, cfg = state
    proposal = insert_proposal(store, target=tmp_path / "rule.md", diff=make_diff(OLD, NEW), status=status)
    assert apply_proposal(store, cfg, proposal)["outcome"] == "held"
    assert store.query_one("SELECT status FROM proposals WHERE id=?", (proposal["id"],))["status"] == status


@pytest.mark.parametrize("taxonomy,expected", [
    ({"agent_error": 3}, True), ({"graded_fail": 3}, False),
    ({"asked_operator": 3}, False), ({"agent_error": 2, "graded_fail": 1}, False),
    ({"agent_error": 1}, False), ({"future_error": 3}, False), ({}, False),
])
def test_report_and_dashboard_agree_whether_trials_ran(taxonomy, expected):
    from self_improve.eval_evidence import no_trial_ran

    row = {"id": "eval", "verdict": "ungated", "attempted": 3, "succeeded": 0, "failed": 3,
           "metrics_json": "{}", "error_taxonomy_json": json.dumps(taxonomy)}
    assert no_trial_ran(row) is expected
    assert (queries.eval_story(row)["shape"] == "no_trial_ran") is expected


def test_reading_a_pre_policy_database_is_default_off_without_migrating(tmp_path, monkeypatch):
    from self_improve import store as store_module
    from self_improve.execution_policy import policy_snapshot, PolicyError, set_class_policy

    original = store_module.MIGRATIONS
    monkeypatch.setattr(store_module, "MIGRATIONS", [m for m in original if m[0] != "0009_execution_policy"])
    path = tmp_path / "legacy.db"
    with closing(Store(path)) as old:
        before = old.query("SELECT name FROM schema_migrations ORDER BY name")
    monkeypatch.setattr(store_module, "MIGRATIONS", original)
    with closing(Store(path, read_only=True)) as reader:
        snapshot = policy_snapshot(reader)
        assert not snapshot["available"]
        assert all(not c["enabled"] for c in snapshot["classes"].values())
        assert reader.query("SELECT name FROM schema_migrations ORDER BY name") == before
        with pytest.raises(PolicyError, match="upgrade the state database"):
            set_class_policy(reader, "global", True)
    with closing(Store(path)) as upgraded:
        assert policy_snapshot(upgraded)["available"]
        assert all(not c["enabled"] for c in policy_snapshot(upgraded)["classes"].values())


def test_reenabling_does_not_authorize_the_old_backlog(state, tmp_path):
    from self_improve.execution_policy import set_class_policy, automatic_permission

    store, cfg = state
    set_class_policy(store, "global", True)
    proposal = insert_proposal(store, target=tmp_path / "rule.md", diff=make_diff(OLD, NEW))
    assert automatic_permission(store, cfg, proposal)["allowed"]
    set_class_policy(store, "global", False)
    set_class_policy(store, "global", True)
    assert automatic_permission(store, cfg, proposal)["reason"] == "predates_class_enable"


def test_originating_review_only_is_enforced_by_a_later_direct_writer(state, tmp_path):
    from self_improve.execution_policy import set_class_policy

    store, cfg = state
    set_class_policy(store, "global", True)
    target = tmp_path / "rule.md"
    target.write_text(OLD)
    proposal = insert_proposal(store, target=target, diff=make_diff(OLD, NEW))
    store.insert("runs", {"id": "review", "started": "2026-09-13T01:00:00Z", "stats_json": '{"review_only":true}'})
    store.update("proposals", "id", proposal["id"], {"run_id": "review"})
    store.commit()
    proposal["run_id"] = "review"
    assert apply_proposal(store, cfg, proposal)["reason"] == "review_only"
    assert target.read_text() == OLD
