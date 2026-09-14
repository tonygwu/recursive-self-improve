"""Shared automatic-write eligibility and human-review membership.

The persisted per-class policy replaces the legacy global config switch.
All classes start off, including when reading a database before migration.
Enabling a class does not authorize its historical backlog. Human approval
is a separate, revision-bound command, never an automatic-policy override.
This module imports no web framework, model client, or file writer.
"""

from __future__ import annotations

import json
from datetime import datetime

from .store import AUTO_APPLY_STATUSES, DECIDED_STATUSES, PROPOSAL_ACTIONS, PROPOSAL_STATUSES, new_id, utc_now_iso

POLICY_MIGRATION = "0009_execution_policy"
TARGET_CLASSES = ("global", "project", "skill", "hook")
TARGET_CLASS = {
    "global_claude_md": "global",
    "codex_global": "global",
    "project_agents_md": "project",
    "project_claude_md": "project",
    "rule_file": "project",
    "skill": "skill",
    "hook": "hook",
}
MANDATORY_REVIEW_ACTIONS = frozenset({"convert_to_hook", "delete_human_line", "resolve_rollback", "reapply", "recover_rule"})
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


def set_class_policy(store, target_class: str, enabled: bool, *, now: str | None = None) -> dict:
    """Record an explicit user policy change, with optimistic concurrency."""
    if target_class not in TARGET_CLASSES or type(enabled) is not bool:
        raise PolicyError("policy requires a known target class and a boolean enabled value")
    if target_class == "hook" and enabled:
        raise PolicyError("hooks always require human approval")
    snapshot = policy_snapshot(store)
    if not snapshot["available"]:
        raise PolicyError("upgrade the state database before changing execution policy")
    before = snapshot["classes"][target_class]
    if before["enabled"] == enabled:
        return before
    ts = now if now is not None else utc_now_iso()
    if _instant(ts) is None:
        raise PolicyError("policy time must be an ISO timestamp with a timezone")
    after = {"enabled": enabled, "enabled_at": ts if enabled else "", "updated_at": ts,
             "revision": before["revision"] + 1}
    with store.conn:
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
    prior = store.query_one(
        "SELECT id FROM proposals WHERE learning_id=? AND status='rolled_back' AND id!=? LIMIT 1",
        (proposal["learning_id"], proposal["id"]),
    )
    return automatic_eligibility(
        proposal, policy_snapshot(store), review_queue_actions=cfg.review_queue_actions,
        review_only=review_only or originating_review_only(store, proposal), prior_rollback=prior["id"] if prior else "",
    )


def proposal_dispositions(store, cfg, *, run_id: str | None = None) -> list[dict]:
    """Give every known undecided proposal one next step, from the same policy."""
    policy = policy_snapshot(store)
    from .rejections import context, rejection_reason
    rejections = context(store)
    sql = "SELECT p.*, r.stats_json AS run_stats FROM proposals p LEFT JOIN runs r ON r.id=p.run_id"
    rows = store.query(sql + (" WHERE p.run_id=?" if run_id is not None else "") + " ORDER BY p.created_at, p.id",
                       (run_id,) if run_id is not None else ())
    result = []
    rolled_back = {p["learning_id"]: p["id"] for p in store.query("SELECT id, learning_id FROM proposals WHERE status='rolled_back'")}
    for row in rows:
        if row["status"] in DECIDED_STATUSES or row["status"] not in PROPOSAL_STATUSES:
            continue
        try:
            stats = json.loads(row.pop("run_stats") or "{}")
        except (ValueError, TypeError) as exc:
            raise PolicyError(f"unreadable run policy for proposal {row['id']}") from exc
        if not isinstance(stats, dict) or ("review_only" in stats and type(stats["review_only"]) is not bool):
            raise PolicyError(f"invalid run review_only policy for proposal {row['id']}")
        decision = automatic_eligibility(row, policy, review_only=stats.get("review_only", False),
                                         review_queue_actions=cfg.review_queue_actions,
                                         prior_rollback=rolled_back.get(row["learning_id"], ""))
        rejection = rejection_reason(store,cfg,row,ctx=rejections)
        if rejection:
            decision.update(allowed=False,reason=rejection['reason'],detail=rejection['detail'])
        result.append({**row, "execution": decision, "next_step": 'suppressed' if rejection else "automatic_delivery" if decision["allowed"] else "review"})
    return result


def waiting_proposals(store, cfg, *, run_id: str | None = None) -> list[dict]:
    return [p for p in proposal_dispositions(store, cfg, run_id=run_id) if p["next_step"] == "review"]
