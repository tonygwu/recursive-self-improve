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


def _in_run(store, proposal, run_id, status):
    # A running run is live while its recorded process exists (run_liveness).
    # This test process plays the pipeline that records itself.
    from tests.test_run_liveness import live_run_stats
    store.insert("runs", {"id": run_id, "started": "2026-09-13T01:00:00Z", "status": status,
                          "stats_json": live_run_stats() if status == "running" else "{}"})
    store.update("proposals", "id", proposal["id"], {"run_id": run_id})
    store.commit()
    proposal["run_id"] = run_id
    return proposal


def test_a_rolled_back_rule_mined_again_as_a_new_learning_is_still_vetoed(state, tmp_path):
    """BL-17: the prior-rollback veto matched learning ids only, so the same
    rule text under a fresh learning could reapply automatically."""
    from self_improve.execution_policy import automatic_permission, set_class_policy
    from self_improve.dashboard import queries as q

    store, cfg = state
    set_class_policy(store, "global", True)
    old = insert_proposal(store, target=tmp_path / "old.md", diff=make_diff(OLD, NEW), status="rolled_back")
    store.update("learnings", "id", old["learning_id"], {"rule_text": "Always   run the FULL suite."})
    new = _in_run(store, insert_proposal(store, target=tmp_path / "new.md", diff=make_diff(OLD, NEW)),
                  "live", "running")
    store.update("learnings", "id", new["learning_id"], {"rule_text": "always run the full suite."})
    store.commit()
    assert automatic_permission(store, cfg, new)["reason"] == "prior_rollback"
    assert new["id"] in q.waiting_proposal_ids(store, cfg)


@pytest.mark.parametrize("created_at", ["", "yesterday", "2026-10-12T00:00:00", None])
def test_an_unreadable_creation_time_never_writes_automatically(state, tmp_path, created_at):
    """A proposal is automatic only if created after its class was enabled.
    An unreadable or zone-less creation time must fail closed; this branch
    had no test."""
    from self_improve.execution_policy import policy_snapshot, automatic_eligibility, set_class_policy

    store, _ = state
    set_class_policy(store, "global", True)
    new = insert_proposal(store, target=tmp_path / "new.md", diff=make_diff(OLD, NEW))
    policy = policy_snapshot(store)
    assert automatic_eligibility(new, policy)["allowed"]
    decision = automatic_eligibility({**new, "created_at": created_at}, policy)
    assert (decision["allowed"], decision["reason"]) == (False, "unknown_created_at")


@pytest.mark.parametrize("stats_json, message", [
    ("{not json", "unreadable run policy"),
    ('{"review_only": "yes"}', "invalid run review_only policy"),
    ('["review_only"]', "invalid run review_only policy"),
])
def test_a_corrupt_run_veto_is_an_error_not_permission(state, tmp_path, stats_json, message):
    """A run's review_only veto that cannot be read must stop the writer."""
    from self_improve.execution_policy import PolicyError, originating_review_only

    store, _ = state
    store.insert("runs", {"id": "run-1", "started": "2026-10-12T00:00:00Z", "status": "ok",
                          "stats_json": stats_json})
    proposal = insert_proposal(store, target=tmp_path / "x.md", diff=make_diff(OLD, NEW))
    with pytest.raises(PolicyError, match=message):
        originating_review_only(store, {**proposal, "run_id": "run-1"})


def test_a_concurrent_policy_change_is_refused(state):
    from self_improve.execution_policy import PolicyError, policy_snapshot, set_class_policy

    store, _ = state
    revision = policy_snapshot(store)["classes"]["global"]["revision"]
    set_class_policy(store, "global", True, expected_revision=revision)
    with pytest.raises(PolicyError, match="changed in another request"):
        set_class_policy(store, "global", False, expected_revision=revision)
    assert policy_snapshot(store)["classes"]["global"]["enabled"] is True


def test_a_policy_row_changed_between_the_snapshot_and_the_write_is_refused(state, monkeypatch):
    """Review fix (BL-51): the revision-guarded UPDATE (`changed.rowcount != 1`)
    had no test. The stale-page test above stops at the expected_revision check
    first, so this one changes the row after the in-transaction snapshot."""
    from self_improve import execution_policy
    from self_improve.execution_policy import PolicyError, policy_snapshot, set_class_policy

    store, _ = state
    real_snapshot = execution_policy.policy_snapshot
    calls = []

    def snapshot_then_concurrent_write(target_store):
        snapshot = real_snapshot(target_store)
        calls.append(target_store.conn.in_transaction)
        if target_store.conn.in_transaction:
            target_store.conn.execute(
                "UPDATE execution_policies SET revision=revision+1 WHERE target_class='global'")
        return snapshot
    monkeypatch.setattr(execution_policy, "policy_snapshot", snapshot_then_concurrent_write)
    with pytest.raises(PolicyError, match="changed in another request"):
        set_class_policy(store, "global", True)
    assert calls == [False, True]
    monkeypatch.undo()
    assert policy_snapshot(store)["classes"]["global"] == {
        "enabled": False, "enabled_at": "", "updated_at": "", "revision": 0}
    assert store.query("SELECT * FROM execution_policy_events") == []


def _next_step(store, cfg, proposal):
    from self_improve.execution_policy import proposal_dispositions

    return {p["id"]: p for p in proposal_dispositions(store, cfg)}[proposal["id"]]


def test_a_passing_proposal_waits_for_its_own_running_run(state, tmp_path):
    """Only the pipeline delivers automatically, inside the run that created
    the proposal. While that run is live, the proposal is not in Review."""
    from self_improve.execution_policy import set_class_policy

    store, cfg = state
    set_class_policy(store, "global", True)
    proposal = _in_run(store, insert_proposal(store, target=tmp_path / "rule.md", diff=make_diff(OLD, NEW)),
                       "live-run", "running")
    assert _next_step(store, cfg, proposal)["next_step"] == "automatic_delivery"
    assert proposal["id"] not in queries.waiting_proposal_ids(store, cfg)


@pytest.mark.parametrize("run_status", ["ok", "error", "abandoned", None])
def test_a_passing_proposal_its_run_never_delivered_returns_to_review(state, tmp_path, run_status):
    """BL-01: a gated_pass proposal in an enabled class whose run ended without
    recording a delivery was hidden from Review and never delivered."""
    from self_improve.execution_policy import set_class_policy

    store, cfg = state
    set_class_policy(store, "global", True)
    proposal = insert_proposal(store, target=tmp_path / "rule.md", diff=make_diff(OLD, NEW))
    if run_status is not None:
        _in_run(store, proposal, "ended-run", run_status)
    row = _next_step(store, cfg, proposal)
    assert row["next_step"] == "review"
    assert row["execution"]["reason"] == "automatic_delivery_missed"
    assert proposal["id"] in queries.waiting_proposal_ids(store, cfg)


def test_a_passing_regate_after_the_run_ended_is_reviewable_and_approvable(state, tmp_path):
    """BL-01 path B: a model job re-gates an inconclusive proposal to gated_pass
    long after its run ended. A human must still be able to approve it."""
    from self_improve.commands import submit_command
    from self_improve.execution_policy import set_class_policy
    from self_improve.review import preview_selection

    store, cfg = state
    cfg = replace(cfg, global_claude_md=str(tmp_path / "rule.md"))
    (tmp_path / "rule.md").write_text(OLD)
    set_class_policy(store, "global", True)
    proposal = _in_run(store, insert_proposal(store, target=tmp_path / "rule.md", diff=make_diff(OLD, NEW),
                                              status="inconclusive"), "old-run", "ok")
    with store.transaction(write=True):
        store.update("proposals", "id", proposal["id"], {"status": "gated_pass"})
    assert proposal["id"] in queries.waiting_proposal_ids(store, cfg)
    with store.transaction():
        shown = preview_selection(store, cfg, [proposal["id"]])
    command = submit_command(store, cfg, {
        "action": "approve", "request_key": "approve-regated", "preview_revision": shown["revision"],
        "members": [{"proposal_id": m["proposal_id"], "revision": m["revision"]} for m in shown["members"]]})
    assert command["id"]


def test_a_recorded_automatic_operation_keeps_ownership_after_the_run(state, tmp_path, monkeypatch):
    """An automatic write that recorded its operation is reconciled by the
    worker, so it must not also be offered in Review."""
    from self_improve import apply
    from self_improve.execution_policy import set_class_policy

    store, cfg = state
    set_class_policy(store, "global", True)
    target = tmp_path / "rule.md"
    target.write_text(OLD)
    proposal = _in_run(store, insert_proposal(store, target=target, diff=make_diff(OLD, NEW)), "crash-run", "running")

    def crash(point, *_):
        if point == "prepared":
            raise KeyboardInterrupt("simulated crash after the operation was recorded")
    monkeypatch.setattr(apply, "_operation_checkpoint", crash)
    with pytest.raises(KeyboardInterrupt):
        apply_proposal(store, cfg, proposal)
    store.update("runs", "id", "crash-run", {"status": "abandoned"})
    store.commit()
    assert store.query_one("SELECT COUNT(*) n FROM instruction_operations WHERE proposal_id=?", (proposal["id"],))["n"] == 1
    assert _next_step(store, cfg, proposal)["next_step"] == "automatic_delivery"


def test_a_cancelled_automatic_operation_does_not_own_a_later_passing_regate(state, tmp_path, monkeypatch):
    """Review fix (BL-01): every auto_apply operation kept automatic ownership,
    even a cancelled one. The nightly crashed after preparing, the operator
    cancelled, the worker reconciled the proposal to held, and a later passing
    re-gate then hid it from Review with no writer left."""
    from self_improve import apply
    from self_improve.commands import submit_command
    from self_improve.execution_policy import set_class_policy
    from self_improve.review import preview_selection
    from self_improve.worker import run_once

    store, cfg = state
    target = tmp_path / "rule.md"
    target.write_text(OLD)
    cfg = replace(cfg, global_claude_md=str(target))
    set_class_policy(store, "global", True)
    proposal = _in_run(store, insert_proposal(store, target=target, diff=make_diff(OLD, NEW)), "crash-run", "running")

    def crash(point, *_):
        if point == "prepared":
            raise KeyboardInterrupt("simulated crash after the operation was recorded")
    monkeypatch.setattr(apply, "_operation_checkpoint", crash)
    with pytest.raises(KeyboardInterrupt):
        apply_proposal(store, cfg, proposal)
    store.update("runs", "id", "crash-run", {"status": "abandoned"})
    store.commit()
    operation = store.query_one("SELECT id FROM instruction_operations WHERE proposal_id=?", (proposal["id"],))
    submit_command(store, cfg, {"action": "cancel_operation", "operation_id": operation["id"],
                                "request_key": "cancel-crashed-operation"})
    monkeypatch.setattr(apply, "_operation_checkpoint", lambda *_: None)
    assert run_once(store, cfg)["state"] == "cancelled"
    assert store.query_one("SELECT status FROM proposals WHERE id=?", (proposal["id"],))["status"] == "held"
    assert proposal["id"] in queries.waiting_proposal_ids(store, cfg)

    with store.transaction(write=True):
        store.update("proposals", "id", proposal["id"], {"status": "gated_pass"})
    row = _next_step(store, cfg, proposal)
    assert (row["next_step"], row["execution"]["reason"]) == ("review", "automatic_delivery_missed")
    assert proposal["id"] in queries.waiting_proposal_ids(store, cfg)
    with store.transaction():
        shown = preview_selection(store, cfg, [proposal["id"]])
    command = submit_command(store, cfg, {
        "action": "approve", "request_key": "approve-after-cancel", "preview_revision": shown["revision"],
        "members": [{"proposal_id": m["proposal_id"], "revision": m["revision"]} for m in shown["members"]]})
    assert target.read_text() == OLD
    assert run_once(store, cfg)["id"] == command["id"]
    assert target.read_text() == NEW
    assert store.query_one("SELECT status FROM proposals WHERE id=?", (proposal["id"],))["status"] == "applied"


def test_a_lock_conflict_during_the_run_returns_the_proposal_to_review_after_it(state, tmp_path):
    """BL-01 path C: the nightly met WriteBusy, recorded no operation, and the
    proposal was stranded for good once the run ended."""
    from self_improve.delivery_lock import WriteBusy, instruction_write_lock
    from self_improve.execution_policy import set_class_policy

    store, cfg = state
    set_class_policy(store, "global", True)
    target = tmp_path / "rule.md"
    target.write_text(OLD)
    proposal = _in_run(store, insert_proposal(store, target=target, diff=make_diff(OLD, NEW)), "busy-run", "running")
    with instruction_write_lock(cfg):
        with pytest.raises(WriteBusy):
            apply_proposal(store, cfg, proposal)
    store.update("runs", "id", "busy-run", {"status": "ok"})
    store.commit()
    assert proposal["id"] in queries.waiting_proposal_ids(store, cfg)
    assert target.read_text() == OLD


def test_a_reconciliation_refusal_during_the_run_returns_the_proposal_to_review_after_it(state, tmp_path, monkeypatch):
    """BL-01 path C: another prepared write to the same target made the
    nightly refuse with ReconciliationRequired before it recorded any
    operation. Once the run ended, nothing owned the proposal."""
    from self_improve import apply
    from self_improve.apply import DeliveryBlocked
    from self_improve.execution_policy import set_class_policy

    store, cfg = state
    set_class_policy(store, "global", True)
    target = tmp_path / "rule.md"
    target.write_text(OLD)
    blocker = _in_run(store, insert_proposal(store, target=target, diff=make_diff(OLD, NEW)), "blocker-run", "running")

    def crash(point, *_):
        if point == "prepared":
            raise KeyboardInterrupt("leave a prepared write on the same target")
    monkeypatch.setattr(apply, "_operation_checkpoint", crash)
    with pytest.raises(KeyboardInterrupt):
        apply_proposal(store, cfg, blocker)
    monkeypatch.setattr(apply, "_operation_checkpoint", lambda *_: None)

    proposal = _in_run(store, insert_proposal(store, target=target, diff=make_diff(OLD, NEW)), "refused-run", "running")
    with pytest.raises(DeliveryBlocked) as refused:
        apply_proposal(store, cfg, proposal)
    assert refused.value.code == "ReconciliationRequired"
    operations = "SELECT COUNT(*) n FROM instruction_operations WHERE proposal_id=?"
    assert store.query_one(operations, (proposal["id"],))["n"] == 0
    assert proposal["id"] not in queries.waiting_proposal_ids(store, cfg)
    store.update("runs", "id", "refused-run", {"status": "ok"})
    store.commit()
    row = _next_step(store, cfg, proposal)
    assert (row["next_step"], row["execution"]["reason"]) == ("review", "automatic_delivery_missed")
    assert proposal["id"] in queries.waiting_proposal_ids(store, cfg)
    assert target.read_text() == OLD


def test_a_rule_deletion_from_the_ab_sweep_always_needs_review(state, tmp_path):
    """BL-11: A/B prune proposals were persisted as gated_pass from three
    without-rule trials and no majority vote, and qualified for automatic
    delivery in an enabled class."""
    from self_improve.execution_policy import automatic_permission, set_class_policy

    store, cfg = state
    set_class_policy(store, "global", True)
    proposal = insert_proposal(store, target=tmp_path / "rule.md", diff=make_diff(NEW, OLD),
                               action="delete")
    assert automatic_permission(store, cfg, proposal)["reason"] == "action_review_queue"


def test_a_policy_table_missing_a_class_or_with_a_bad_flag_fails_loud(state):
    """BL-51: the corrupt-policy guards had no test."""
    from self_improve.execution_policy import PolicyError, policy_snapshot

    store, _ = state
    # A CHECK constraint already refuses enabled values other than 0/1; an
    # enabled hook class passes it and must still be refused by the reader.
    store.conn.execute("UPDATE execution_policies SET enabled=1, enabled_at='2026-01-01T00:00:00Z' WHERE target_class='hook'")
    with pytest.raises(PolicyError, match="invalid enabled policy for hook"):
        policy_snapshot(store)
    store.conn.execute("UPDATE execution_policies SET enabled=0 WHERE target_class='hook'")
    store.conn.execute("DELETE FROM execution_policies WHERE target_class='skill'")
    with pytest.raises(PolicyError, match="exactly the four target classes"):
        policy_snapshot(store)
    store.conn.rollback()


# Review of BL-51: the policy guards below had reader-only tests or none. Each
# case corrupts one row while the global class is properly enabled. With the
# enabled-value, hook or class-set guard removed, the final writer applied the
# eligible proposal. With the enabled_at guard removed, it held the proposal as
# unknown_created_at, so only the named refusal tells the two apart.
def _corrupt_policy(store, statement, params=()):
    # The schema's CHECK refuses enabled values other than 0/1. A row written
    # by an older or foreign tool can still hold one; reproduce it directly.
    store.conn.execute("PRAGMA ignore_check_constraints = ON")
    try:
        store.conn.execute(statement, params)
        store.commit()
    finally:
        store.conn.execute("PRAGMA ignore_check_constraints = OFF")


@pytest.mark.parametrize("statement,params,message", [
    # :75, the enabled value. 2 is truthy, so without the guard it enables.
    ("UPDATE execution_policies SET enabled=2 WHERE target_class='global'", (),
     "invalid enabled value for global"),
    ("UPDATE execution_policies SET enabled='yes' WHERE target_class='global'", (),
     "invalid enabled value for global"),
    # :77, the enabled_at half: an enabled class with no readable enable time.
    ("UPDATE execution_policies SET enabled_at='' WHERE target_class='global'", (),
     "invalid enabled policy for global"),
    ("UPDATE execution_policies SET enabled_at='yesterday' WHERE target_class='global'", (),
     "invalid enabled policy for global"),
    ("UPDATE execution_policies SET enabled_at='2020-01-01T00:00:00' WHERE target_class='global'", (),
     "invalid enabled policy for global"),
    # :77, the hook half: a corrupt hook row refuses the whole table.
    ("UPDATE execution_policies SET enabled=1, enabled_at='2020-01-01T00:00:00Z' WHERE target_class='hook'", (),
     "invalid enabled policy for hook"),
    # :71, the class set: a missing or an extra class refuses the table.
    ("DELETE FROM execution_policies WHERE target_class='skill'", (),
     "execution_policies does not contain exactly the four target classes"),
    ("INSERT INTO execution_policies (target_class) VALUES (?)", ("plugin",),
     "execution_policies does not contain exactly the four target classes"),
])
def test_a_corrupt_policy_row_stops_the_final_writer_by_name(state, tmp_path, statement, params, message):
    from self_improve.execution_policy import PolicyError, policy_snapshot, set_class_policy

    store, cfg = state
    set_class_policy(store, "global", True, now="2020-01-01T00:00:00Z")
    target = tmp_path / "CLAUDE.md"
    target.write_text(OLD)
    proposal = insert_proposal(store, target=target, diff=make_diff(OLD, NEW))
    assert policy_snapshot(store)["classes"]["global"]["enabled"] is True  # valid before the corruption
    _corrupt_policy(store, statement, params)
    with pytest.raises(PolicyError) as err:
        policy_snapshot(store)
    assert str(err.value) == message
    with pytest.raises(PolicyError) as err:
        apply_proposal(store, cfg, proposal)
    assert str(err.value) == message
    assert target.read_text() == OLD
    assert store.query_one("SELECT status FROM proposals WHERE id=?", (proposal["id"],))["status"] == "gated_pass"
    assert store.query("SELECT * FROM instruction_operations") == []
    assert not (tmp_path / "state" / "snapshots").exists()
