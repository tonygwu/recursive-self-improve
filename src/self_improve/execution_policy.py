"""Shared automatic-write eligibility and human-review membership.

The persisted per-class policy replaces the legacy global config switch.
All classes start off, including when reading a database before migration.
Enabling a class does not authorize its historical backlog. Human approval
is a separate, revision-bound command, never an automatic-policy override.
This module imports no web framework, model client, or file writer.
"""

from __future__ import annotations

import json
from contextlib import nullcontext
from datetime import datetime

from .store import AUTO_APPLY_STATUSES, DECIDED_STATUSES, PROPOSAL_ACTIONS, PROPOSAL_STATUSES, new_id, utc_now_iso

POLICY_MIGRATION = "0009_execution_policy"
TARGET_CLASSES = ("global", "project", "skill", "hook")
TARGET_CLASS = {
    "global_claude_md": "global",
    "codex_global": "global",
    # The private file shares the global class switch: it is user-wide, and a
    # separate fifth class would need its own consent and dashboard row.
    "global_private": "global",
    "project_agents_md": "project",
    "project_claude_md": "project",
    "rule_file": "project",
    "skill": "skill",
    "hook": "hook",
}
# "delete" removes a delivered machine rule on A/B evidence alone (three
# without-rule trials, no majority vote), so it always needs a human.
MANDATORY_REVIEW_ACTIONS = frozenset({"convert_to_hook", "delete", "delete_human_line", "resolve_rollback", "reapply", "recover_rule"})
REASON_COPY = {
    "class_disabled": "automatic application is off for this target class.",
    "predates_class_enable": "this proposal predates the class switch and still needs your decision.",
    "review_only": "this proposal came from a run that explicitly required review.",
    "unknown_target_class": "this target kind has no execution policy. It cannot be written automatically.",
    "unknown_created_at": "the proposal has no valid creation time to compare with the class policy.",
    "status_not_appliable": "only a passing gate can qualify for automatic application.",
    "action_review_queue": "this kind of change always requires your approval.",
    "unknown_action": "this proposal action has no execution policy.",
    "prior_rollback": "this lesson was rolled back before. Reapplying it requires a human decision.",
    "target_rejected": "this lesson is rejected at this canonical target; other targets remain available.",
    "lesson_rejected": "this lesson is permanently rejected everywhere.",
    "fresh_approval_required": "this historical approval has no delivery command. Inspect the current combined edit and give fresh approval before anything can be written.",
    "automatic_delivery_missed": "the run that could deliver this automatically ended without recording a delivery, so it needs your decision.",
}


class PolicyError(ValueError):
    """Invalid or conflicting policy input. No policy change was recorded."""


def _instant(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else None
    except (ValueError, AttributeError, TypeError):
        return None


def policy_snapshot(store) -> dict:
    """Read policy without creating a table or migrating an old database."""
    migrated = store.query_one("SELECT name FROM schema_migrations WHERE name = ?", (POLICY_MIGRATION,))
    if migrated is None:
        return {"available": False, "classes": {
            c: {"enabled": False, "enabled_at": "", "updated_at": "", "revision": 0}
            for c in TARGET_CLASSES
        }}
    rows = store.query("SELECT * FROM execution_policies ORDER BY target_class")
    if {r["target_class"] for r in rows} != set(TARGET_CLASSES):
        raise PolicyError("execution_policies does not contain exactly the four target classes")
    classes = {}
    for row in rows:
        if row["enabled"] not in (0, 1):
            raise PolicyError(f"invalid enabled value for {row['target_class']}")
        if row["enabled"] and (row["target_class"] == "hook" or _instant(row["enabled_at"]) is None):
            raise PolicyError(f"invalid enabled policy for {row['target_class']}")
        classes[row["target_class"]] = {k: v for k, v in row.items() if k != "target_class"}
        classes[row["target_class"]]["enabled"] = bool(row["enabled"])
    return {"available": True, "classes": classes}


def set_class_policy(store, target_class: str, enabled: bool, *, now: str | None = None,
                     expected_revision: int | None = None, commit: bool = True) -> dict:
    """Record an explicit user policy change, with optimistic concurrency."""
    if target_class not in TARGET_CLASSES or type(enabled) is not bool:
        raise PolicyError("policy requires a known target class and a boolean enabled value")
    if target_class == "hook" and enabled:
        raise PolicyError("hooks always require human approval")
    if not policy_snapshot(store)['available']:
        raise PolicyError('upgrade the state database before changing execution policy')
    if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 0):
        raise PolicyError('expected_revision must be a nonnegative integer')
    if not commit and (store.read_only or not store.conn.in_transaction):
        raise PolicyError('policy persistence requires the caller\'s active write transaction')
    ts = now if now is not None else utc_now_iso()
    if _instant(ts) is None:
        raise PolicyError("policy time must be an ISO timestamp with a timezone")
    with store.transaction(write=True) if commit else nullcontext():
        snapshot = policy_snapshot(store)
        if not snapshot["available"]:
            raise PolicyError("upgrade the state database before changing execution policy")
        before = snapshot["classes"][target_class]
        if expected_revision is not None and before['revision'] != expected_revision:
            raise PolicyError('the policy changed in another request; reload it before retrying')
        if before["enabled"] == enabled:
            return before
        after = {"enabled": enabled, "enabled_at": ts if enabled else "", "updated_at": ts,
                 "revision": before["revision"] + 1}
        changed = store.conn.execute(
            "UPDATE execution_policies SET enabled=?, enabled_at=?, updated_at=?, revision=? "
            "WHERE target_class=? AND revision=?",
            (int(enabled), after["enabled_at"], ts, after["revision"], target_class, before["revision"]),
        )
        if changed.rowcount != 1:
            raise PolicyError("the policy changed in another request; reload it before retrying")
        store.insert("execution_policy_events", {"id": new_id(), "target_class": target_class,
            "ts": ts, "actor": "user", "before_json": json.dumps(before), "after_json": json.dumps(after)})
    return after


def automatic_eligibility(proposal: dict, policy: dict, *, review_only: bool = False,
                          review_queue_actions=(), prior_rollback: str = "") -> dict:
    """Return permission and a reason. Unknown input never grants permission."""
    target_class = TARGET_CLASS.get(proposal.get("target_kind"))
    reason = ""
    if proposal.get("status") not in AUTO_APPLY_STATUSES:
        reason = "status_not_appliable"
    elif proposal.get("action") not in PROPOSAL_ACTIONS:
        reason = "unknown_action"
    elif (proposal.get("action") in MANDATORY_REVIEW_ACTIONS
          or proposal.get("action") in review_queue_actions or target_class == "hook"):
        reason = "action_review_queue"
    elif target_class is None:
        reason = "unknown_target_class"
    elif review_only:
        reason = "review_only"
    elif prior_rollback:
        reason = "prior_rollback"
    elif not policy["classes"][target_class]["enabled"]:
        reason = "class_disabled"
    else:
        created = _instant(proposal.get("created_at"))
        enabled = _instant(policy["classes"][target_class]["enabled_at"])
        if created is None or enabled is None:
            reason = "unknown_created_at"
        elif created < enabled:
            reason = "predates_class_enable"
    detail = REASON_COPY.get(reason, "the gate passed and this class was enabled before the proposal was created.")
    if reason == "prior_rollback":
        detail += f" Earlier proposal {prior_rollback} is rolled_back."
    return {"allowed": not reason, "reason": reason, "target_class": target_class, "detail": detail}


def originating_review_only(store, proposal: dict) -> bool:
    """Preserve an explicit run veto even for a later direct writer call."""
    row = store.query_one("SELECT stats_json FROM runs WHERE id=?", (proposal.get("run_id", ""),))
    if row is None:
        return False
    try:
        stats = json.loads(row["stats_json"])
    except (ValueError, TypeError) as exc:
        raise PolicyError(f"unreadable run policy for proposal {proposal['id']}") from exc
    if not isinstance(stats, dict) or ("review_only" in stats and type(stats["review_only"]) is not bool):
        raise PolicyError(f"invalid run review_only policy for proposal {proposal['id']}")
    return stats.get("review_only", False)


def automatic_permission(store, cfg, proposal: dict, *, review_only: bool = False) -> dict:
    """Resolve current permission for a writer, including earlier human undo."""
    from .rejections import rejection_reason
    rejection = rejection_reason(store, cfg, proposal)
    if rejection:
        return {'allowed':False, 'reason':rejection['reason'], 'target_class':TARGET_CLASS.get(proposal.get('target_kind')),
                'detail':rejection['detail']}
    learning = store.query_one("SELECT rule_text FROM learnings WHERE id=?", (proposal["learning_id"],))
    prior = _prior_rollback(_rolled_back_index(store, exclude=proposal["id"]), proposal["learning_id"],
                            learning["rule_text"] if learning else "")
    return automatic_eligibility(
        proposal, policy_snapshot(store), review_queue_actions=cfg.review_queue_actions,
        review_only=review_only or originating_review_only(store, proposal), prior_rollback=prior,
    )


def _rule_key(text: str) -> str:
    # Same normalization rejection_reason uses for rule text.
    return " ".join((text or "").split()).casefold()


def _rolled_back_index(store, *, exclude: str = "") -> tuple[dict, dict]:
    """Rolled-back proposals by learning id and by normalized rule text.

    A lesson mined again as a new learning carries a new id but the same
    rule; the human undo must still veto its automatic reapplication.
    """
    by_learning, by_text = {}, {}
    for row in store.query(
            "SELECT p.id, p.learning_id, l.rule_text FROM proposals p "
            "LEFT JOIN learnings l ON l.id = p.learning_id WHERE p.status='rolled_back' AND p.id != ?",
            (exclude,)):
        by_learning.setdefault(row["learning_id"], row["id"])
        key = _rule_key(row["rule_text"])
        if key:
            by_text.setdefault(key, row["id"])
    return by_learning, by_text


def _prior_rollback(index: tuple[dict, dict], learning_id: str, rule_text: str) -> str:
    by_learning, by_text = index
    return by_learning.get(learning_id) or by_text.get(_rule_key(rule_text), "")


def historical_approval_ids(store) -> set[str]:
    """Find old decisions that can return to Review, never grant permission.

    Any retained execution record keeps its existing recovery owner. Missing
    records do not prove that a write never happened: fresh approval must also
    bind a strict preview against the current target. Reads never migrate.
    """
    migrations = {r['name'] for r in store.query('SELECT name FROM schema_migrations')}
    exclusions = []
    for migration, table in (('0010_dashboard_commands', 'command_members'),
                             ('0012_instruction_operations', 'instruction_operations')):
        if migration in migrations:
            exclusions.append(f'NOT EXISTS (SELECT 1 FROM {table} e WHERE e.proposal_id=p.id)')
    sql = """SELECT p.id FROM proposals p WHERE p.status='approved_user'
        AND p.applied_at='' AND p.snapshot_commit_before='' AND p.snapshot_commit_after=''
        AND NOT EXISTS (SELECT 1 FROM proposal_events e WHERE e.proposal_id=p.id
                        AND e.event IN ('applied','rolled_back','rejected_user'))"""
    return {r['id'] for r in store.query(sql + ''.join(' AND ' + clause for clause in exclusions))}


def proposal_dispositions(store, cfg, *, run_id: str | None = None, retained_only: bool = False) -> list[dict]:
    """Give undecided proposals and historical approvals one shared next step.

    Evidence readers can request retained-only facts. Applicable target rejections
    then remain unknown until current target inspection; this never grants a write.
    Default Review and delivery callers retain current target resolution.
    """
    if type(retained_only) is not bool:
        raise PolicyError('retained_only must be a boolean')
    policy = policy_snapshot(store)
    from .rejections import context, rejection_reason, TargetInspectionRequired
    rejections = context(store)
    sql = "SELECT p.*, r.stats_json AS run_stats FROM proposals p LEFT JOIN runs r ON r.id=p.run_id"
    rows = store.query(sql + (" WHERE p.run_id=?" if run_id is not None else "") + " ORDER BY p.created_at, p.id",
                       (run_id,) if run_id is not None else ())
    result = []
    historical = historical_approval_ids(store)
    rolled_back = _rolled_back_index(store)
    rule_texts = {r["id"]: r["rule_text"] for r in store.query("SELECT id, rule_text FROM learnings")}
    # Only the pipeline delivers automatically, and only inside the run that
    # created the proposal. After that run ends, an eligible proposal with no
    # recorded automatic operation has no writer left; it returns to Review
    # instead of waiting forever. A run has ended once its recorded process is
    # gone, even before the next pipeline start marks it abandoned; the reaper
    # uses the same rule (run_liveness). An unfinished recorded operation keeps
    # its recovery owner. A completed or cancelled one owns nothing any more:
    # the worker never acts on it again, so a later passing re-gate needs Review.
    from .commands import FINISHED_STATES
    from .run_liveness import RunLivenessError, live_run_ids
    try:
        live_runs = live_run_ids(store, cfg)
    except RunLivenessError as exc:
        raise PolicyError(f"cannot tell whether a running run is live: {exc}") from exc
    migrations = {r['name'] for r in store.query('SELECT name FROM schema_migrations')}
    finished = sorted(FINISHED_STATES)
    owned = ({r["proposal_id"] for r in store.query(
        "SELECT DISTINCT proposal_id FROM instruction_operations WHERE kind='auto_apply' "
        f"AND state NOT IN ({','.join('?' for _ in finished)})", tuple(finished))}
        if '0012_instruction_operations' in migrations else set())
    for row in rows:
        if (row["status"] in DECIDED_STATUSES and row['id'] not in historical) or row["status"] not in PROPOSAL_STATUSES:
            continue
        try:
            stats = json.loads(row.pop("run_stats") or "{}")
        except (ValueError, TypeError) as exc:
            raise PolicyError(f"unreadable run policy for proposal {row['id']}") from exc
        if not isinstance(stats, dict) or ("review_only" in stats and type(stats["review_only"]) is not bool):
            raise PolicyError(f"invalid run review_only policy for proposal {row['id']}")
        decision = automatic_eligibility(row, policy, review_only=stats.get("review_only", False),
                                         review_queue_actions=cfg.review_queue_actions,
                                         prior_rollback=_prior_rollback(rolled_back, row["learning_id"],
                                                                        rule_texts.get(row["learning_id"], "")))
        if row['id'] in historical:
            decision.update(allowed=False, reason='fresh_approval_required',
                            detail=REASON_COPY['fresh_approval_required'])
        elif decision["allowed"] and row["run_id"] not in live_runs and row["id"] not in owned:
            decision.update(allowed=False, reason='automatic_delivery_missed',
                            detail=REASON_COPY['automatic_delivery_missed'])
        try:
            rejection = rejection_reason(store,cfg,row,ctx=rejections,retained_only=retained_only)
        except TargetInspectionRequired as exc:
            decision.update(allowed=False, reason='target_identity_unobserved', detail=str(exc))
            result.append({**row, 'execution':decision, 'next_step':'unknown'})
            continue
        if rejection:
            decision.update(allowed=False,reason=rejection['reason'],detail=rejection['detail'])
        result.append({**row, "execution": decision, "next_step": 'suppressed' if rejection else "automatic_delivery" if decision["allowed"] else "review"})
    return result


def waiting_proposals(store, cfg, *, run_id: str | None = None) -> list[dict]:
    return [p for p in proposal_dispositions(store, cfg, run_id=run_id) if p["next_step"] == "review"]
