"""Scope decision-boundary eval: dataset contract, shipped guidance, scoring.

No model is called. Real-provider runs use ``selfimprove eval-scope``.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from pathlib import Path

import pytest

from self_improve import llm as llm_module
from self_improve.config import Config
from self_improve.evals import scope_boundary as sb

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "evals" / "scope" / "boundary.yaml"


@dataclass
class Result:
    ok: bool = True
    text: str = ""
    parsed: dict | None = None
    provider: str = "fake"
    model_reported: str = "fake-model"
    error: str = ""
    call_id: str = ""


def answer(scope):
    return Result(parsed={"scope_guess": scope}, text=f'{{"scope_guess": "{scope}"}}')


def test_both_mine_prompts_carry_one_identical_guidance_block():
    block = sb.guidance_block(ROOT / "prompts")
    assert "`personal`" in block and "`skill`" in block
    for name in sb.PROMPTS:
        assert (ROOT / "prompts" / name).read_text().count(block) == 1


def test_diverged_guidance_is_refused(tmp_path):
    for name in sb.PROMPTS:
        (tmp_path / name).write_text((ROOT / "prompts" / name).read_text())
    agentic = tmp_path / "mine_incident_agentic.md"
    agentic.write_text(agentic.read_text().replace("Ask\nthese questions in order", "Ask\nthe questions in order"))
    with pytest.raises(sb.ScopeEvalError, match="differ"):
        sb.guidance_block(tmp_path)


def test_dataset_covers_every_scope_and_every_pair():
    cases = sb.load_cases(DATASET)
    by_scope = {s: sum(c["expected"] == s for c in cases) for s in sb.SCOPES}
    assert min(by_scope.values()) >= 5, by_scope
    pairs = {tuple(sorted(c["boundary"].split("/"))) for c in cases}
    assert pairs == {tuple(sorted(p)) for p in itertools.combinations(sb.SCOPES, 2)}


@pytest.mark.parametrize("mutate, message", [
    (lambda c: c.update(expected="hook"), "is not one of"),
    (lambda c: c.update(boundary="global/project", expected="skill"), "boundary must name"),
    (lambda c: c.pop("context"), "keys must be exactly"),
])
def test_malformed_cases_raise(tmp_path, mutate, message):
    import yaml
    data = yaml.safe_load(DATASET.read_text())
    mutate(data["cases"][0])
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump(data))
    with pytest.raises(sb.ScopeEvalError, match=message):
        sb.load_cases(bad)


def test_prompt_quotes_the_guidance_and_the_case():
    case = sb.load_cases(DATASET)[0]
    guidance = sb.guidance_block(ROOT / "prompts")
    prompt = sb.build_prompt(guidance, case)
    assert guidance in prompt and case["lesson"] in prompt and case["context"] in prompt


@pytest.mark.parametrize("result, expected", [
    (answer("personal"), ("personal", "ok")),
    (answer("hook"), ("hook", "ok")),
    (Result(ok=False, error="timeout"), (None, "call_failed")),
    (Result(text="   "), (None, "empty")),
    (Result(text="I think personal"), (None, "unparseable")),
    (Result(parsed={"scope_guess": "personal", "why": "x"}), (None, "unparseable")),
    (Result(parsed={"scope_guess": "laptop"}), (None, "invalid_scope")),
])
def test_answer_causes_stay_distinct(result, expected):
    assert sb.parse_answer(result) == expected


@pytest.mark.parametrize("outcome, cause", [
    ("parse_error", "unparseable"), ("empty_output", "empty"),
    ("model_mismatch", "model_mismatch"), ("timeout", "call_failed"), ("quota_exhausted", "call_failed"),
])
def test_real_runner_failures_keep_their_cause(outcome, cause):
    result = llm_module.LLMResult(ok=False, text="", parsed=None, outcome=outcome, provider="claude",
                                  model_reported="", error=outcome)
    assert sb.parse_answer(result) == (None, cause)


def test_invalid_repeat_creates_nothing(tmp_path):
    with pytest.raises(sb.ScopeEvalError):
        sb.run_with_models(Config(state_dir=str(tmp_path / "state")), DATASET, tmp_path / "out", repeat=0)
    assert not (tmp_path / "out").exists()


def test_score_reports_confusion_boundaries_and_causes():
    cases = [
        {"id": "a", "boundary": "global/personal", "expected": "personal", "lesson": "x", "context": "y"},
        {"id": "b", "boundary": "global/personal", "expected": "global", "lesson": "x", "context": "y"},
        {"id": "c", "boundary": "project/skill", "expected": "skill", "lesson": "x", "context": "y"},
    ]
    replies = iter([answer("global"), answer("global"), Result(ok=False, error="quota")])
    answers, result = sb.run(cases, "guidance", lambda prompt: next(replies))
    assert result["attempted"] == 3 and result["answered"] == 2 and result["correct"] == 1
    assert result["accuracy"] == 0.5
    assert result["causes"]["call_failed"] == 1
    assert result["confusion"] == {"global": {"global": 1}, "personal": {"global": 1}}
    assert result["by_boundary"] == {"global/personal": {"correct": 1, "answered": 2}}
    assert result["misses"] == [{"case_id": "a", "expected": "personal", "predicted": "global"}]


def test_model_run_uses_a_disposable_store_and_an_exact_call_cap(tmp_path, monkeypatch):
    seen = {}
    expected = {c["lesson"]: c["expected"] for c in sb.load_cases(DATASET)}

    class FakeRunner:
        def __init__(self, cfg, store, run_id, raw_dir):
            seen.update(cap=cfg.max_cheap_calls_per_run, db=store.db_path)

        def call(self, stage, model_class, prompt, expect_json):
            seen["stage"] = stage
            lesson = next(l for l in expected if l in prompt)
            return answer(expected[lesson])

    monkeypatch.setattr(llm_module, "LLMRunner", FakeRunner)
    cfg = Config(state_dir=str(tmp_path / "state"))
    out = tmp_path / "out"
    report = sb.run_with_models(cfg, DATASET, out, repeat=2)

    n = len(expected)
    assert seen == {"cap": 2 * n, "db": out / "eval.db", "stage": sb.STAGE}
    assert report["attempted"] == 2 * n and report["accuracy"] == 1.0
    assert not (tmp_path / "state").exists(), "the eval must not open the configured state database"
    assert (out / "report.json").is_file() and len((out / "answers.jsonl").read_text().splitlines()) == 2 * n
    with pytest.raises(FileExistsError):
        sb.run_with_models(cfg, DATASET, out)
