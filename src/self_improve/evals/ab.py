"""Rule-effectiveness A/B: re-run applied rules' evals to find dead weight.

The idea (Cherny's "delete and see", made continuous): an applied rule whose
regression eval's WITHOUT-rule arm no longer fails on the current model is a
rule the model no longer needs — ``rerun_applied`` finds those and returns
pruning-proposal dicts for the pipeline to route through the normal
gate/apply/ledger machinery. ``effectiveness_delta`` is the pure helper for
comparing two arms' pass rates.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Callable

from ..config import Config
from ..store import Store, new_id, utc_now_iso
from .harness import SpecError, TrialStats, load_spec, run_trials


def _pass_rate(stats: TrialStats | Mapping) -> float:
    """succeeded/attempted for a TrialStats or its dict form; 0 attempted raises."""
    if isinstance(stats, TrialStats):
        attempted, succeeded = stats.attempted, stats.succeeded
    elif isinstance(stats, Mapping):
        attempted, succeeded = stats["attempted"], stats["succeeded"]
    else:
        raise TypeError(
            f"stats must be TrialStats or a mapping, got {type(stats).__name__}"
        )
    if attempted <= 0:
        raise ValueError(f"cannot compute a pass rate over {attempted} attempted trials")
    return succeeded / attempted


def effectiveness_delta(
    with_stats: TrialStats | Mapping, without_stats: TrialStats | Mapping
) -> float:
    """Pass-rate lift the rule provides: with-rule rate minus without-rule rate.

    Positive = the rule still helps; ~0 = the rule no longer moves its eval
    on the current model (pruning candidate); negative = the rule hurts.
    Either arm with 0 attempted trials raises rather than guessing a rate.
    """
    return _pass_rate(with_stats) - _pass_rate(without_stats)


def find_spec(
    learning_id: str, primary_dir: Path, *, fallback_dir: Path | None = None
) -> Path | None:
    """Locate a learning's eval spec, newest evidence first.

    Generated specs live under ``cfg.regression_specs_dir`` in the state dir,
    because the production checkout has to stay clean. Some specs were
    committed to ``<repo>/evals/regression/`` before that move and are named by
    learning id. Keep the read-only fallback for an explicitly supplied legacy
    directory so an existing specification remains discoverable.

    The state dir wins a tie: a freshly generated spec is newer evidence than a
    committed one.
    """
    primary = Path(primary_dir) / f"{learning_id}.yaml"
    if primary.is_file():
        return primary
    if fallback_dir is not None:
        candidate = Path(fallback_dir) / f"{learning_id}.yaml"
        if candidate.is_file():
            return candidate
    return None


def rerun_applied(
    store: Store,
    cfg: Config,
    agent_runner: Callable[[str, Path], str],
    regression_dir: Path,
    *,
    fallback_spec_dir: Path | None = None,
    model_grader: Callable[[str, str], bool] | None = None,
    work_dir: Path | None = None,
) -> dict:
    """Re-run applied rules' regression evals WITHOUT the rule; propose prunes.

    Selects up to ``cfg.ab_prune_max_rules_per_run`` applied learnings
    (learnings.status='applied', joined to their applied proposal for
    ``applied_at`` and ``target_path``), oldest ``applied_at`` first. A
    candidate whose spec YAML (``<regression_dir>/<learning-id>.yaml``) is
    missing is skipped and counted as ``skipped_no_spec`` — it does NOT
    consume a cap slot (a skip runs no trials, and the cap exists to bound
    trial cost). Once the cap is reached every remaining candidate is
    counted as ``deferred``.

    Per processed learning, ``cfg.eval_trials`` trials run via
    ``harness.run_trials`` with ``rule_text=None``. Verdict:

    - ``graded_fail >= cfg.gate_without_min_failures`` → ``'still_needed'``
      (the current model still makes the mistake without the rule);
    - zero graded failures AND zero infra errors → ``'prunable'``;
    - anything else (agent_error/grader_error trials, or a nonzero
      graded_fail count below the threshold) → ``'inconclusive'`` —
      crashed trials prove nothing, so no proposal is emitted.

    Each processed learning persists one ``eval_results`` row (kind='ab',
    subject_id=learning id, TrialStats accounting + taxonomy, metrics_json
    with the arm stats and verdict). That is the ONLY table this function
    writes: prunable rules yield proposal dicts in the returned result —
    ``{learning_id, target_path, action: 'delete', marker_id,
    eval_result_id}`` — for the pipeline/CLI to persist through the normal
    proposal machinery (propose/apply), never written here.

    Fail-loud: an applied learning with no applied proposal row raises
    (its target_path is unknowable — silent exclusion would hide the
    inconsistency); a spec file whose internal id differs from the learning
    id raises ``SpecError``.

    Returns ``{attempted, still_needed, prunable, inconclusive,
    skipped_no_spec, deferred, proposals}`` where
    attempted == still_needed + prunable + inconclusive.
    """
    regression_dir = Path(regression_dir)

    applied_ids = {
        r["id"] for r in store.query("SELECT id FROM learnings WHERE status = 'applied'")
    }
    # Latest applied proposal per applied learning (a learning re-applied
    # after a rollback keeps its most recent target_path), oldest applied
    # rules first so long-standing rules are re-tested before fresh ones.
    rows = store.query(
        "SELECT l.id AS learning_id, p.target_path, p.applied_at "
        "FROM learnings l JOIN proposals p ON p.learning_id = l.id "
        "WHERE l.status = 'applied' AND p.status = 'applied' "
        "AND p.applied_at = ("
        "  SELECT MAX(p2.applied_at) FROM proposals p2 "
        "  WHERE p2.learning_id = l.id AND p2.status = 'applied') "
        "ORDER BY p.applied_at ASC, l.id ASC"
    )
    candidates: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        if row["learning_id"] in seen:  # two applied proposals tied on applied_at
            continue
        seen.add(row["learning_id"])
        candidates.append(row)
    orphaned = sorted(applied_ids - seen)
    if orphaned:
        raise ValueError(
            f"{len(orphaned)} applied learning(s) have no applied proposal row, "
            f"so their target_path is unknown: {orphaned}"
        )

    counts = {
        "attempted": 0,
        "still_needed": 0,
        "prunable": 0,
        "inconclusive": 0,
        "skipped_no_spec": 0,
        "deferred": 0,
    }
    proposals: list[dict] = []
    for cand in candidates:
        lid = cand["learning_id"]
        if counts["attempted"] >= cfg.ab_prune_max_rules_per_run:
            counts["deferred"] += 1
            continue
        spec_path = find_spec(lid, regression_dir, fallback_dir=fallback_spec_dir)
        if spec_path is None:
            counts["skipped_no_spec"] += 1
            continue
        spec = load_spec(spec_path)
        if spec.id != lid:
            raise SpecError(
                f"{spec_path}: spec id {spec.id!r} does not match learning id {lid!r}"
            )

        started = utc_now_iso()
        stats = run_trials(
            spec,
            None,  # WITHOUT-rule arm: does the current model still need the rule?
            agent_runner,
            cfg.eval_trials,
            model_grader,
            work_dir=(Path(work_dir) / lid if work_dir is not None else None),
        )
        graded_failures = stats.errors.get("graded_fail", 0)
        infra_errors = sum(n for k, n in stats.errors.items() if k != "graded_fail")
        if graded_failures >= cfg.gate_without_min_failures:
            verdict = "still_needed"
        elif graded_failures == 0 and infra_errors == 0:
            verdict = "prunable"
        else:
            verdict = "inconclusive"
        counts["attempted"] += 1
        counts[verdict] += 1

        eval_result_id = new_id()
        store.insert(
            "eval_results",
            {
                "id": eval_result_id,
                "kind": "ab",
                "subject_id": lid,
                "started": started,
                "finished": utc_now_iso(),
                "attempted": stats.attempted,
                "succeeded": stats.succeeded,
                "failed": stats.failed,
                "error_taxonomy_json": json.dumps(stats.errors, ensure_ascii=False),
                "metrics_json": json.dumps(
                    {
                        "without": dataclasses.asdict(stats),
                        "graded_failures_without_rule": graded_failures,
                        "infra_errors": infra_errors,
                        "eval_trials": cfg.eval_trials,
                        "gate_without_min_failures": cfg.gate_without_min_failures,
                        "target_path": cand["target_path"],
                        "applied_at": cand["applied_at"],
                        "verdict": verdict,
                    },
                    ensure_ascii=False,
                ),
                "verdict": verdict,
            },
        )
        store.commit()  # per-rule durability: a crash mid-run keeps finished rows

        if verdict == "prunable":
            proposals.append(
                {
                    "learning_id": lid,
                    "target_path": cand["target_path"],
                    "action": "delete",
                    "marker_id": lid,
                    "eval_result_id": eval_result_id,
                }
            )
    # Explain a zero attempt count: no applied rules, no candidates, and missing
    # specifications are distinct outcomes that need distinct reasons.
    if not applied_ids:
        reason = "no_applied_rules: nothing has ever been applied, so there is nothing to re-test"
    elif not candidates:
        reason = (
            f"no_candidates: {len(applied_ids)} applied rule(s) exist but none "
            "has an applied proposal row to take a target_path from"
        )
    elif counts["attempted"] == 0:
        reason = (
            f"no_spec: {len(candidates)} candidate(s) considered, none had a "
            "regression spec to re-run"
        )
    else:
        reason = ""
    return {
        **counts,
        "candidates_considered": len(candidates),
        "applied_rules": len(applied_ids),
        "reason": reason,
        "proposals": proposals,
    }
