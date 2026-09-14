"""Per-rule behavior-regression evals: generation and the apply gate.

`generate_spec` turns a mined learning into an EvalSpec via an injected
``llm_json`` callable (strong-model in production) using the
``prompts/gen_regression_eval.md`` template. The caller supplies a private output
directory outside checkouts and installed packages.

`gate` runs the spec's two arms with the injected agent runner:

- without-rule arm: the eval must *detect the mistake* — at least
  ``cfg.gate_without_min_failures`` trials must end in ``graded_fail``.
  Infra errors (``agent_error``/``grader_error``) do NOT count as
  detection: a crashed trial proves nothing about the mistake. If the
  threshold is not met the verdict is ``ungated``. An entirely uninformative
  without-rule arm returns ``error``.
- with-rule arm: :func:`with_arm_verdict` checks clean passes and uninformative
  failures before deciding ``gated_pass``, ``ungated``, or ``gated_fail``.

The majority gate combines scenario outcomes. Execution permission belongs to
the separate shared policy; ``ungated`` does not authorize automatic writes.
"""

from __future__ import annotations

import dataclasses
import itertools
import json
import os
import tempfile
from pathlib import Path
from typing import Callable

import yaml

from ..config import Config
from ..data_boundary import DataBoundaryError, private_storage_path
from ..store import Store, new_id, utc_now_iso
from .harness import EvalSpec, SpecError, run_trials, spec_from_dict, spec_to_dict

PROMPT_TEMPLATE_NAME = "gen_regression_eval.md"

# Placeholders the template must contain; a template missing one would
# silently produce a prompt without that field, so its absence raises.
_PLACEHOLDERS = {
    "{{rule}}": "rule_text",
    "{{why}}": "why",
    "{{incident_summary}}": "incident_summary",
}

_REQUIRED_LEARNING_KEYS = ("id", "rule_text", "why", "incident_summary")

# Keys the LLM response must contain — exactly these; `id` comes from the
# learning, and anything extra is an unvalidated guess, so both raise.
_LLM_SPEC_KEYS = ("title", "scenario_prompt", "workspace_files", "success_criteria", "grader")


EVIDENCE_MAX_CHARS = 8000


def generate_spec(
    learning: dict,
    llm_json: Callable[[str], dict],
    prompts_dir: Path,
    *,
    out_dir: Path | None = None,
    evidence: str = "",
    scenario: int | None = None,
) -> EvalSpec:
    """Generate a regression EvalSpec for a learning and write its YAML.

    ``learning`` must carry id, rule_text, why, and incident_summary
    (missing keys raise). The prompt template file
    ``<prompts_dir>/gen_regression_eval.md`` must exist and contain the
    {{rule}}, {{why}}, {{incident_summary}} placeholders — missing template
    or placeholder raises. ``llm_json(prompt)`` must return a dict with
    exactly title, scenario_prompt, workspace_files, success_criteria,
    grader; any deviation raises (LLM output is never guessed at).

    The validated spec is written to ``<out_dir>/<spec-id>.yaml``. An explicit
    private output directory outside checkouts and installed packages is required.

    ``scenario`` makes the spec id ``<learning-id>-s<N>``. The majority gate
    generates several scenarios for one learning, and without this every one
    of them writes ``<learning-id>.yaml`` and silently overwrites the last —
    leaving N-1 scenarios with no surviving evidence, and leaving
    ``ab.rerun_applied`` to re-test whichever happened to run last.
    """
    if out_dir is None:
        raise SpecError("generated evals require an explicit out_dir outside every checkout")
    try:
        target_dir = private_storage_path(Path(out_dir))
    except DataBoundaryError as exc:
        raise SpecError(str(exc)) from exc

    missing = [k for k in _REQUIRED_LEARNING_KEYS if k not in learning]
    if missing:
        raise SpecError(f"learning is missing required keys {missing} for eval generation")

    template_path = Path(prompts_dir) / PROMPT_TEMPLATE_NAME
    if not template_path.is_file():
        raise FileNotFoundError(
            f"regression eval prompt template not found: {template_path}"
        )
    template = template_path.read_text(encoding="utf-8")
    absent = [ph for ph in _PLACEHOLDERS if ph not in template]
    if absent:
        raise SpecError(
            f"prompt template {template_path} is missing placeholders {absent}"
        )
    prompt = template
    for placeholder, learning_key in _PLACEHOLDERS.items():
        prompt = prompt.replace(placeholder, str(learning[learning_key]))

    # Design the failure scenario from redacted evidence, not just a paraphrase.
    # The incident's window_json was redacted at scan time.
    ev = str(evidence or "").strip()
    if len(ev) > EVIDENCE_MAX_CHARS:
        cut = len(ev) - EVIDENCE_MAX_CHARS
        ev = ev[:EVIDENCE_MAX_CHARS] + f"\n[truncated {cut} chars]"
    prompt = prompt.replace(
        "{{evidence}}",
        ev or "(no transcript excerpt was retained for this incident)",
    )

    response = llm_json(prompt)
    if not isinstance(response, dict):
        raise SpecError(
            f"llm_json returned {type(response).__name__}, expected a mapping"
        )
    missing = [k for k in _LLM_SPEC_KEYS if k not in response]
    unknown = [k for k in response if k not in _LLM_SPEC_KEYS]
    if missing or unknown:
        raise SpecError(
            f"llm eval-spec response for learning {learning['id']!r}: "
            f"missing keys {missing}, unknown keys {unknown}"
        )

    spec_id = (
        learning["id"] if scenario is None else f"{learning['id']}-s{scenario}"
    )
    spec_dict = {"id": spec_id, **{k: response[k] for k in _LLM_SPEC_KEYS}}
    spec = spec_from_dict(spec_dict, origin=f"llm response for learning {learning['id']!r}")

    target_file = target_dir / f"{spec.id}.yaml"
    try:
        private_storage_path(target_file)
    except DataBoundaryError as exc:
        raise SpecError(str(exc)) from exc
    target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Replace the directory entry rather than truncating an existing inode:
    # an old artifact can be a hard link to a tracked fixture elsewhere.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target_dir,
                                         prefix=".eval-", suffix=".tmp", delete=False) as fh:
            temporary = Path(fh.name)
            yaml.safe_dump(spec_to_dict(spec), fh, sort_keys=False, allow_unicode=True)
        os.replace(temporary, target_file)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return spec


def verdict_for(
    *,
    without_failed: int,
    without_attempted: int,
    with_succeeded: int,
    with_attempted: int,
    cfg: Config,
    with_errors: dict | None = None,
) -> str:
    """The gate's verdict, as a pure function of the two arms' counts.

    ``without_failed`` counts graded task failures, not infrastructure errors.
    Too few detections return ``ungated``. Otherwise :func:`with_arm_verdict`
    uses the with-rule passes and error taxonomy.

    Zero successes alone cannot distinguish a task failure from a blocked runner.
    The harness must classify those causes separately. The caller :func:`gate`
    rejects an entirely uninformative without-rule arm before calling this helper.
    """
    if without_failed < cfg.gate_without_min_failures:
        return "ungated"
    return with_arm_verdict(
        {
            "succeeded": with_succeeded,
            "attempted": with_attempted,
            "errors": with_errors or {},
        },
        min_passes=cfg.gate_with_min_passes,
    )


def gate(
    spec: EvalSpec,
    rule_text: str,
    agent_runner: Callable[[str, Path], str],
    cfg: Config,
    model_grader: Callable[[str, str], bool] | None = None,
    *,
    store: Store | None = None,
    work_dir: Path | None = None,
    checkpoint=None, record_result=None,
) -> dict:
    """Gate a rule on its regression eval; returns the verdict + both arms.

    Runs ``cfg.eval_trials`` trials without the rule; if fewer than
    ``cfg.gate_without_min_failures`` trials end in ``graded_fail`` the
    verdict is ``ungated`` and the with-rule arm is skipped. An entirely
    uninformative without-rule arm returns ``error`` instead. Otherwise the
    with-rule arm runs, and :func:`with_arm_verdict` checks its passes and errors.

    Returns ``{"verdict", "without_stats", "with_stats"}`` with stats as
    plain dicts (with_stats is None when ungated). When ``store`` is given,
    persists an eval_results row (kind='regression', subject_id=spec.id)
    with summed attempted/succeeded/failed, the merged error taxonomy, and
    both arms in metrics_json.
    """
    if not rule_text or not rule_text.strip():
        raise ValueError("gate requires a non-empty rule_text")
    started = utc_now_iso()

    without = run_trials(
        spec, None, agent_runner, cfg.eval_trials, model_grader,
        work_dir=(Path(work_dir) / "without" if work_dir is not None else None),
        **({"checkpoint":lambda key,inputs,fn:checkpoint("without:"+key,inputs,fn)} if checkpoint else {}),
    )
    detected = without.errors.get("graded_fail", 0)
    with_: object | None = None
    # An arm in which EVERY trial was uninformative tested nothing, and must
    # not produce a verdict about the rule.
    #
    # `detected` counts only graded failures. Zero detections can mean either
    # correct behavior or infrastructure failure, so inspect the trial outcomes.
    #
    # `error` is already a SCENARIO_OUTCOME: it is tallied and never voted
    # with, so a rule whose scenarios all errored lands on `inconclusive`,
    # which is HELD. Both arms share UNINFORMATIVE_FAILURES so infrastructure
    # failures receive the same treatment throughout the comparison.
    #
    # NOTE `succeeded` is NOT the test. It counts only `pass`, so a healthy
    # without-arm that reproduced the mistake in all three trials also has
    # succeeded == 0. Using it would have turned every strong detection into an
    # error — the same bug, pointing the other way.
    uninformative = sum(
        int(without.errors.get(k, 0)) for k in UNINFORMATIVE_FAILURES
    )
    if without.attempted and uninformative >= without.attempted:
        verdict = "error"
    elif detected < cfg.gate_without_min_failures:
        verdict = "ungated"
    else:
        with_ = run_trials(
            spec, rule_text, agent_runner, cfg.eval_trials, model_grader,
            work_dir=(Path(work_dir) / "with" if work_dir is not None else None),
            **({"checkpoint":lambda key,inputs,fn:checkpoint("with:"+key,inputs,fn)} if checkpoint else {}),
        )
        verdict = verdict_for(
            without_failed=detected,
            without_attempted=without.attempted,
            with_succeeded=with_.succeeded,
            with_attempted=with_.attempted,
            cfg=cfg,
            with_errors=dict(with_.errors or {}),
        )

    without_stats = dataclasses.asdict(without)
    with_stats = dataclasses.asdict(with_) if with_ is not None else None
    result = {
        "verdict": verdict,
        "without_stats": without_stats,
        "with_stats": with_stats,
        "eval_result_id": "",  # set below when a store is provided
    }

    if store is not None:
        arms = [without_stats] + ([with_stats] if with_stats else [])
        taxonomy: dict[str, int] = {}
        for arm in arms:
            for key, count in arm["errors"].items():
                taxonomy[key] = taxonomy.get(key, 0) + count
        result["eval_result_id"] = new_id()
        row = {
                "id": result["eval_result_id"],
                "kind": "regression",
                "subject_id": spec.id,
                "started": started,
                "finished": utc_now_iso(),
                "attempted": sum(a["attempted"] for a in arms),
                "succeeded": sum(a["succeeded"] for a in arms),
                "failed": sum(a["failed"] for a in arms),
                "error_taxonomy_json": json.dumps(taxonomy, ensure_ascii=False),
                "metrics_json": json.dumps(
                    {
                        "without": without_stats,
                        "with": with_stats,
                        "detected_failures_without_rule": detected,
                        "eval_trials": cfg.eval_trials,
                        "gate_without_min_failures": cfg.gate_without_min_failures,
                        "gate_with_min_passes": cfg.gate_with_min_passes,
                    },
                    ensure_ascii=False,
                ),
                "verdict": verdict,
            }
        if record_result is None:
            store.insert("eval_results",row)
        else:
            result["eval_result_id"]=record_result(row)
        store.commit()
    return result


# --- majority thresholds -----------------------------------------------------
#
# DELIBERATELY NOT CONFIG KEYS. The number of scenarios is a cost knob and
# lives in config as `eval_scenarios`; these two are the evidentiary bar behind
# auto-apply, and a bar that can be lowered by editing a TOML file is not a
# bar. Changing them is a code change, reviewed like one.
#
# The asymmetry is the point, because the two errors are not equally bad.
# Auto-applying a rule that harms the agent is worse than holding a rule that
# helps it, so a pass needs a majority AND a clean sheet, while any two
# failures condemn.
GATE_PASS_VOTES = 2  # scenarios that must return gated_pass, with zero fails
GATE_FAIL_VOTES = 2  # scenarios returning gated_fail that condemn a rule

# What one scenario can return. An error can come from generation or an
# uninformative without-rule arm. It is counted but supplies no pass/fail vote.
SCENARIO_OUTCOMES = ("gated_pass", "gated_fail", "ungated", "error")


def majority_verdict(tally: dict) -> str:
    """The gate's verdict from a tally of per-scenario outcomes.

    Pure function of the counts so the decision is testable without running a
    single trial, and so the decision table can be read in one place:

        gated_pass    >= GATE_PASS_VOTES passes AND zero fails
        gated_fail    >= GATE_FAIL_VOTES fails
        ungated       nothing reproduced anywhere, but scenarios DID run
        inconclusive  everything else, including "no scenario ran at all"

    Mixed pass/fail evidence is ``inconclusive`` unless failures reach the
    rejection threshold. A tally containing only errors is also inconclusive.
    At least one ``ungated`` result distinguishes that case from scenarios that
    ran without qualifying pass/fail evidence. Neither verdict grants automatic
    execution permission.
    """
    n_pass = int(tally.get("gated_pass", 0))
    n_fail = int(tally.get("gated_fail", 0))
    n_ungated = int(tally.get("ungated", 0))
    if n_fail >= GATE_FAIL_VOTES:
        return "gated_fail"
    if n_pass >= GATE_PASS_VOTES and n_fail == 0:
        return "gated_pass"
    if n_pass == 0 and n_fail == 0:
        return "ungated" if n_ungated else "inconclusive"
    return "inconclusive"


def _settled_verdict(tally: dict, remaining: int) -> str | None:
    """The verdict if no completion of the remaining scenarios could change it.

    Returns ``None`` while the outcome is still open. Enumerating the
    completions is the only safe way to stop early: a rule of thumb like "stop
    once two agree" disagrees with the full run whenever a later scenario would
    have pushed the tally over ``GATE_FAIL_VOTES``. Early exit must save budget
    and never change an answer.
    """
    if remaining <= 0:
        return majority_verdict(tally)
    seen: set[str] = set()
    for combo in itertools.combinations_with_replacement(SCENARIO_OUTCOMES, remaining):
        future = dict(tally)
        for outcome in combo:
            future[outcome] = int(future.get(outcome, 0)) + 1
        seen.add(majority_verdict(future))
        if len(seen) > 1:
            return None
    return seen.pop() if seen else None


def gate_majority(generate, run_gate, *, scenarios: int) -> dict:
    """Gate a rule on a MAJORITY of independently generated scenarios.

    ``generate(i)`` returns an EvalSpec for scenario ``i``; ``run_gate(spec, i)``
    returns one scenario's verdict dict and MUST use a work directory unique to
    ``i``. ``run_trials`` creates its sandbox with ``exist_ok=False``, so reused
    directories fail. Independent scenarios reduce dependence on one trial's
    stochastic outcome; the gate's majority rule decides the result.

    A generation that raises consumes its scenario and does not abort the rest,
    so one malformed spec cannot collapse the gate. If EVERY scenario raises,
    the exception from the last one propagates rather than reporting a verdict
    nothing supports.

    Returns the deciding scenario's verdict dict with the majority ``verdict``
    substituted, plus ``scenario_tally``, ``scenarios_run`` and a per-scenario
    ``scenarios`` list, so a 2-1 result is visibly weaker than a 3-0 one
    instead of collapsing into a single word.
    """
    if scenarios < 1:
        raise ValueError(f"scenarios must be >= 1, got {scenarios}")
    tally = {k: 0 for k in SCENARIO_OUTCOMES}
    per_scenario: list[dict] = []
    results: list[dict] = []
    last_exc: Exception | None = None
    settled: str | None = None
    used = 0

    for index in range(scenarios):
        used += 1
        try:
            spec = generate(index)
        except Exception as exc:  # noqa: BLE001 - one bad spec is not fatal
            last_exc = exc
            tally["error"] += 1
            per_scenario.append(
                {"scenario": index, "verdict": "error", "error": str(exc)}
            )
        else:
            result = run_gate(spec, index)
            outcome = result.get("verdict", "")
            if outcome not in SCENARIO_OUTCOMES:
                raise ValueError(
                    f"scenario {index} returned an unknown verdict {outcome!r}; "
                    f"expected one of {SCENARIO_OUTCOMES}"
                )
            tally[outcome] += 1
            results.append(result)
            per_scenario.append(
                {
                    "scenario": index,
                    "verdict": outcome,
                    "spec_id": getattr(spec, "id", ""),
                    "eval_result_id": result.get("eval_result_id", ""),
                }
            )
        settled = _settled_verdict(tally, scenarios - used)
        if settled is not None:
            break

    if not results:
        # Nothing ran. Propagate the real cause rather than inventing a verdict.
        raise last_exc if last_exc else RuntimeError("no eval spec was generated")

    # Use the SETTLED verdict when the loop exited early, not a fresh reading of
    # the partial tally. They agree for every reachable tally at the current
    # thresholds — verified exhaustively in
    # TestEarlyExitCanNeverChangeTheAnswer — but that is a property of
    # GATE_PASS_VOTES and GATE_FAIL_VOTES, not of the algorithm, and it would
    # break silently if either constant moved. Early exit must save budget and
    # never change an answer, so take the answer it was proven to have.
    verdict = settled if settled is not None else majority_verdict(tally)
    # The deciding scenario is the first that actually reproduced the mistake,
    # since an `ungated` scenario tested nothing. `ab.rerun_applied` later
    # re-tests this rule from one spec, and it should be one that bites.
    deciding = next(
        (r for r in results if r.get("verdict") != "ungated"), results[0]
    )
    return {
        **deciding,
        "verdict": verdict,
        "scenario_tally": dict(tally),
        "scenarios_run": used,
        "scenarios": per_scenario,
    }


# Failures that say nothing about the rule. ``asked_operator``: the agent
# obeyed a rule telling it to ask and no human was there. ``agent_error`` /
# ``grader_error``: the harness broke. The gate already refuses to count infra
# errors as detection in the WITHOUT arm; the WITH arm uses the same guard.
UNINFORMATIVE_FAILURES = ("asked_operator", "agent_error", "grader_error")


def with_arm_verdict(with_stats: dict, *, min_passes: int) -> str:
    """Verdict from the with-rule arm, discounting failures that prove nothing.

    A rule is only condemned when the arm actually failed ON THE TASK. Failures
    where the agent asked a human, or where the runner or grader broke, never
    tested the rule at all, so an arm dominated by them returns ``ungated``
    rather than ``gated_fail``.
    """
    passes = int(with_stats.get("succeeded", 0))
    if passes >= min_passes:
        return "gated_pass"
    errors = with_stats.get("errors") or {}
    uninformative = sum(int(errors.get(k, 0)) for k in UNINFORMATIVE_FAILURES)
    failed = int(with_stats.get("attempted", 0)) - passes
    if failed and uninformative * 2 >= failed:
        return "ungated"
    return "gated_fail"
