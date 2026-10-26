"""Scope decision-boundary eval for the shipped miner guidance.

Each case is one invented lesson plus its incident context, labeled with the
scope the guidance should pick: ``global`` (shared public instructions),
``personal`` (the private, machine-specific file), ``project`` (one
repository's instruction file) or ``skill``. The prompt carries the exact
"Choosing scope_guess" block from both mine prompts, so a score measures the
text the miner actually reads.

What a score does not measure: full mining (the miner also reads the
transcript and in-force rules), routing overrides (an ignored in-force rule
becomes a hook; personal is checked before project-count promotion), and the
hook, rule_path and codex_global scopes. A run over 32 invented cases is a
regression signal for the guidance, not an estimate of production accuracy.

Model calls go through the ordinary ``LLMRunner`` against a new disposable
Store in the output directory, never the configured state database.
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Callable

import yaml

from ..resources import bundled_path

SCOPES = ("global", "personal", "project", "skill")
CASE_KEYS = frozenset({"id", "boundary", "expected", "lesson", "context"})
GUIDANCE_RE = re.compile(r"<!-- scope-guidance:start -->\n(.*?)<!-- scope-guidance:end -->", re.DOTALL)
PROMPTS = ("mine_incident.md", "mine_incident_agentic.md")
STAGE = "scope_eval"
# Distinct answer causes; a failed call, an empty answer and an unparseable
# one are different events and are never merged.
CAUSES = ("ok", "call_failed", "model_mismatch", "empty", "unparseable", "invalid_scope")
# The runner reports a bad reply as a failed call (ok=False) with an outcome
# naming why; these outcomes are answer failures, not call failures.
_RUNNER_CAUSES = {"empty_output": "empty", "parse_error": "unparseable", "model_mismatch": "model_mismatch"}


class ScopeEvalError(ValueError):
    pass


def guidance_block(prompts_dir: Path | None = None) -> str:
    """The guidance both mine prompts share. Missing or diverged copies raise."""
    base = Path(prompts_dir) if prompts_dir else bundled_path("prompts")
    blocks = {}
    for name in PROMPTS:
        found = GUIDANCE_RE.findall((base / name).read_text(encoding="utf-8"))
        if len(found) != 1:
            raise ScopeEvalError(f"{base / name}: expected one scope-guidance block, found {len(found)}")
        blocks[name] = found[0]
    if len(set(blocks.values())) != 1:
        raise ScopeEvalError(f"scope-guidance blocks differ between {', '.join(PROMPTS)}")
    return blocks[PROMPTS[0]]


def load_cases(path: Path) -> list[dict]:
    """Strictly read a case file: exact keys, known scopes, unique ids."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or set(raw) != {"version", "cases"} or raw["version"] != 1:
        raise ScopeEvalError(f"{path}: expected a version-1 mapping with exactly 'version' and 'cases'")
    if not isinstance(raw["cases"], list) or not raw["cases"]:
        raise ScopeEvalError(f"{path}: 'cases' must be a non-empty list")
    seen = set()
    for i, case in enumerate(raw["cases"]):
        owner = f"{path} case {i}"
        if not isinstance(case, dict) or set(case) != CASE_KEYS:
            raise ScopeEvalError(f"{owner}: keys must be exactly {sorted(CASE_KEYS)}")
        if not all(isinstance(case[k], str) and case[k].strip() for k in CASE_KEYS):
            raise ScopeEvalError(f"{owner}: every value must be a non-empty string")
        if case["id"] in seen:
            raise ScopeEvalError(f"{owner}: duplicate id {case['id']!r}")
        seen.add(case["id"])
        if case["expected"] not in SCOPES:
            raise ScopeEvalError(f"{owner} ({case['id']}): expected {case['expected']!r} is not one of {SCOPES}")
        pair = case["boundary"].split("/")
        if len(pair) != 2 or not set(pair) <= set(SCOPES) or case["expected"] not in pair:
            raise ScopeEvalError(f"{owner} ({case['id']}): boundary must name two scopes including the expected one")
    return raw["cases"]


def build_prompt(guidance: str, case: dict) -> str:
    return (
        "You are deciding where a lesson learned from a coding-agent session\n"
        "should be written. Use only the guidance below.\n\n"
        f"{guidance}\n"
        "## The lesson\n\n"
        f"Rule: {case['lesson']}\n\n"
        f"Incident it came from: {case['context']}\n\n"
        "## Output\n\n"
        "Reply with exactly one JSON object and nothing else:\n"
        '{"scope_guess": "<one of global, personal, project, skill, hook, codex_global>"}\n'
    )


def parse_answer(result) -> tuple[str | None, str]:
    """(scope or None, cause) for one LLMResult-shaped answer."""
    if not result.ok:
        return None, _RUNNER_CAUSES.get(getattr(result, "outcome", ""), "call_failed")
    if not (result.text or "").strip() and result.parsed is None:
        return None, "empty"
    parsed = result.parsed
    if parsed is None:
        try:
            parsed = json.loads(result.text)
        except (TypeError, ValueError):
            return None, "unparseable"
    if not isinstance(parsed, dict) or set(parsed) != {"scope_guess"} or not isinstance(parsed["scope_guess"], str):
        return None, "unparseable"
    # Out-of-set scopes the guidance still offers (hook, codex_global) are a
    # real, scoreable wrong answer, not a parse failure.
    if parsed["scope_guess"] not in (*SCOPES, "hook", "codex_global"):
        return None, "invalid_scope"
    return parsed["scope_guess"], "ok"


def score(cases: list[dict], answers: list[dict]) -> dict:
    """Totals, per-boundary and per-scope results, and the confusion matrix.

    ``answers`` rows carry ``case_id``, ``predicted``, ``cause``, ``provider``
    and ``model_reported``. Accuracy divides by answered calls only; failed,
    empty, unparseable and invalid answers are reported by cause beside it.
    """
    by_id = {c["id"]: c for c in cases}
    causes = Counter(a["cause"] for a in answers)
    unknown = set(causes) - set(CAUSES)
    if unknown:
        raise ScopeEvalError(f"unknown answer causes {sorted(unknown)}")
    answered = [a for a in answers if a["cause"] == "ok"]
    correct = [a for a in answered if a["predicted"] == by_id[a["case_id"]]["expected"]]
    confusion: dict[str, Counter] = defaultdict(Counter)
    boundary: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    expected_scope: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for a in answered:
        case = by_id[a["case_id"]]
        hit = int(a["predicted"] == case["expected"])
        confusion[case["expected"]][a["predicted"]] += 1
        boundary[case["boundary"]][0] += hit
        boundary[case["boundary"]][1] += 1
        expected_scope[case["expected"]][0] += hit
        expected_scope[case["expected"]][1] += 1
    served = Counter(f"{a['provider']}:{a['model_reported'] or 'unreported'}" for a in answers
                     if a["cause"] not in ("call_failed", "model_mismatch"))
    return {
        "attempted": len(answers),
        "answered": len(answered),
        "correct": len(correct),
        "accuracy": round(len(correct) / len(answered), 4) if answered else None,
        "causes": {c: causes.get(c, 0) for c in CAUSES},
        "by_boundary": {k: {"correct": v[0], "answered": v[1]} for k, v in sorted(boundary.items())},
        "by_expected_scope": {k: {"correct": v[0], "answered": v[1]} for k, v in sorted(expected_scope.items())},
        "confusion": {k: dict(sorted(v.items())) for k, v in sorted(confusion.items())},
        "served": dict(sorted(served.items())),
        "misses": [{"case_id": a["case_id"], "expected": by_id[a["case_id"]]["expected"],
                    "predicted": a["predicted"]} for a in answered if a not in correct],
    }


def run(cases: list[dict], guidance: str, call: Callable[[str], object], *, repeat: int = 1) -> tuple[list[dict], dict]:
    """Ask once per case per repeat; return (answer rows, score)."""
    if repeat < 1:
        raise ScopeEvalError("repeat must be >= 1")
    answers = []
    for r in range(repeat):
        for case in cases:
            result = call(build_prompt(guidance, case))
            predicted, cause = parse_answer(result)
            answers.append({"case_id": case["id"], "repeat": r, "predicted": predicted, "cause": cause,
                            "outcome": getattr(result, "outcome", ""),
                            "provider": result.provider, "model_reported": result.model_reported,
                            "call_id": getattr(result, "call_id", ""), "error": result.error})
    return answers, score(cases, answers)


def run_with_models(cfg, dataset: Path, out_dir: Path, *, repeat: int = 1) -> dict:
    """Run against real providers with a disposable Store under ``out_dir``.

    ``out_dir`` must not exist yet. The cheap-call cap for this Store is set to
    exactly cases x repeat, so the eval cannot spend more than it announces.
    """
    from ..llm import LLMRunner
    from ..store import Store, new_id, utc_now_iso

    if repeat < 1:
        raise ScopeEvalError("repeat must be >= 1")
    cases = load_cases(dataset)
    guidance = guidance_block()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=False)
    calls = len(cases) * repeat
    eval_cfg = dataclasses.replace(cfg, max_cheap_calls_per_run=calls)
    store = Store(out_dir / "eval.db")
    run_id = new_id()
    store.insert("runs", {"id": run_id, "started": utc_now_iso(), "status": "running"})
    store.commit()
    runner = LLMRunner(eval_cfg, store, run_id, raw_dir=out_dir / "raw")
    answers, result = run(cases, guidance,
                          lambda prompt: runner.call(STAGE, eval_cfg.cheap_model_class, prompt, expect_json=True),
                          repeat=repeat)
    store.update("runs", "id", run_id, {"finished": utc_now_iso(), "status": "ok",
                                         "stats_json": json.dumps(result, ensure_ascii=False)})
    store.commit()
    report = {"dataset": str(dataset), "cases": len(cases), "repeat": repeat, "call_cap": calls,
              "model_class": eval_cfg.cheap_model_class, "store": str(out_dir / "eval.db"), **result}
    (out_dir / "answers.jsonl").write_text("".join(json.dumps(a, ensure_ascii=False) + "\n" for a in answers))
    (out_dir / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    return report
