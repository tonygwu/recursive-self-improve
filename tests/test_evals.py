"""Tests for the eval cluster: harness, regression gate, self-eval, A/B.

All agent/LLM invocations are injected fakes; nothing here makes a real
call or touches any real config dir — every write is asserted to live
under pytest's tmp_path.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from self_improve.config import Config
from self_improve.evals import ab, harness, regression, self_eval
from self_improve.evals.harness import EvalSpec, SpecError, TrialStats, load_spec, run_trials
from self_improve.evals.self_eval import LabeledDataError
from self_improve.store import Store

RULE_MARKER = "Always write GOOD to answer.txt"


def code_spec_dict() -> dict:
    return {
        "id": "test-spec-001",
        "title": "Rule adherence smoke",
        "scenario_prompt": "Complete the task in this workspace.",
        "workspace_files": {"src/app.py": "print('hi')\n"},
        "success_criteria": "answer.txt contains GOOD",
        "grader": {"type": "code", "check": "grep -q GOOD answer.txt"},
    }


def model_spec_dict() -> dict:
    d = code_spec_dict()
    d["id"] = "test-spec-model"
    d["grader"] = {"type": "model", "rubric": "Did the agent write GOOD?"}
    return d


def code_spec() -> EvalSpec:
    return harness.spec_from_dict(code_spec_dict(), origin="test")


def model_spec() -> EvalSpec:
    return harness.spec_from_dict(model_spec_dict(), origin="test")


def rule_sensitive_runner(seen_sandboxes: list[Path] | None = None):
    """Fake agent: succeeds iff the sandbox CLAUDE.md contains the rule."""

    def runner(prompt: str, sandbox: Path) -> str:
        if seen_sandboxes is not None:
            seen_sandboxes.append(sandbox)
        claude_md = sandbox / "CLAUDE.md"
        text = claude_md.read_text(encoding="utf-8") if claude_md.exists() else ""
        if RULE_MARKER in text:
            (sandbox / "answer.txt").write_text("GOOD\n", encoding="utf-8")
            return "done: followed the rule"
        (sandbox / "answer.txt").write_text("BAD\n", encoding="utf-8")
        return "done: ignored the rule"

    return runner


def always_good_runner(prompt: str, sandbox: Path) -> str:
    (sandbox / "answer.txt").write_text("GOOD\n", encoding="utf-8")
    return "always good"


def always_bad_runner(prompt: str, sandbox: Path) -> str:
    (sandbox / "answer.txt").write_text("BAD\n", encoding="utf-8")
    return "always bad"


def crashing_runner(prompt: str, sandbox: Path) -> str:
    raise RuntimeError("agent exploded")


# ---------------------------------------------------------------------------
# load_spec / spec validation
# ---------------------------------------------------------------------------


class TestLoadSpec:
    def _write(self, tmp_path: Path, data: dict) -> Path:
        p = tmp_path / "spec.yaml"
        p.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        return p

    def test_roundtrip(self, tmp_path):
        spec = load_spec(self._write(tmp_path, code_spec_dict()))
        assert spec == code_spec()
        assert spec.workspace_files == {"src/app.py": "print('hi')\n"}

    @pytest.mark.parametrize("key", list(harness.SPEC_KEYS))
    def test_missing_key_raises(self, tmp_path, key):
        data = code_spec_dict()
        del data[key]
        with pytest.raises(SpecError, match=key):
            load_spec(self._write(tmp_path, data))

    def test_unknown_key_raises(self, tmp_path):
        data = code_spec_dict()
        data["surprise"] = "x"
        with pytest.raises(SpecError, match="surprise"):
            load_spec(self._write(tmp_path, data))

    def test_bad_grader_type_raises(self, tmp_path):
        data = code_spec_dict()
        data["grader"] = {"type": "vibes", "check": "true"}
        with pytest.raises(SpecError, match="grader"):
            load_spec(self._write(tmp_path, data))

    def test_code_grader_without_check_raises(self, tmp_path):
        data = code_spec_dict()
        data["grader"] = {"type": "code"}
        with pytest.raises(SpecError, match="check"):
            load_spec(self._write(tmp_path, data))

    def test_model_grader_without_rubric_raises(self, tmp_path):
        data = code_spec_dict()
        data["grader"] = {"type": "model"}
        with pytest.raises(SpecError, match="rubric"):
            load_spec(self._write(tmp_path, data))

    def test_empty_scenario_prompt_raises(self, tmp_path):
        data = code_spec_dict()
        data["scenario_prompt"] = "  "
        with pytest.raises(SpecError, match="scenario_prompt"):
            load_spec(self._write(tmp_path, data))

    @pytest.mark.parametrize("evil", ["../evil.txt", "/etc/evil.txt", "a/../../evil.txt"])
    def test_workspace_path_escape_raises(self, tmp_path, evil):
        data = code_spec_dict()
        data["workspace_files"] = {evil: "x"}
        with pytest.raises(SpecError, match="workspace_files"):
            load_spec(self._write(tmp_path, data))

    def test_path_unsafe_id_raises(self, tmp_path):
        data = code_spec_dict()
        data["id"] = "../oops"
        with pytest.raises(SpecError, match="id"):
            load_spec(self._write(tmp_path, data))


# ---------------------------------------------------------------------------
# run_trials: grading, rule injection, taxonomy, isolation
# ---------------------------------------------------------------------------


class TestRunTrials:
    def test_with_rule_all_pass(self, tmp_path):
        stats = run_trials(
            code_spec(), RULE_MARKER, rule_sensitive_runner(), 3, work_dir=tmp_path / "w"
        )
        assert (stats.attempted, stats.succeeded, stats.failed) == (3, 3, 0)
        assert stats.errors == {}

    def test_without_rule_all_graded_fail(self, tmp_path):
        stats = run_trials(
            code_spec(), None, rule_sensitive_runner(), 3, work_dir=tmp_path / "w"
        )
        assert (stats.attempted, stats.succeeded, stats.failed) == (3, 0, 3)
        assert stats.errors == {"graded_fail": 3}

    def test_sandbox_isolation_and_artifacts(self, tmp_path):
        seen: list[Path] = []
        work = tmp_path / "work"
        stats = run_trials(
            code_spec(), RULE_MARKER, rule_sensitive_runner(seen), 2, work_dir=work
        )
        # transcripts_dir and every sandbox the agent saw live under tmp_path
        assert Path(stats.transcripts_dir) == work
        assert len(seen) == 2 and len(set(seen)) == 2  # fresh sandbox per trial
        for sandbox in seen:
            assert sandbox.resolve().is_relative_to(tmp_path.resolve())
            # workspace materialized + rule CLAUDE.md written inside the sandbox
            assert (sandbox / "src" / "app.py").read_text() == "print('hi')\n"
            claude_md = (sandbox / "CLAUDE.md").read_text()
            assert RULE_MARKER in claude_md
            assert claude_md.startswith("# Sandbox project instructions")
        # per-trial artifacts retained
        for i in range(2):
            trial = work / f"trial-{i:02d}"
            assert (trial / "agent_output.txt").read_text() == "done: followed the rule"
            result = json.loads((trial / "result.json").read_text())
            assert result["outcome"] == "pass"
        assert json.loads((work / "stats.json").read_text())["succeeded"] == 2

    def test_no_claude_md_written_without_rule(self, tmp_path):
        seen: list[Path] = []
        run_trials(code_spec(), None, rule_sensitive_runner(seen), 1, work_dir=tmp_path / "w")
        assert not (seen[0] / "CLAUDE.md").exists()

    def test_rule_appended_to_spec_provided_claude_md(self, tmp_path):
        data = code_spec_dict()
        data["workspace_files"]["CLAUDE.md"] = "# Existing project notes\n"
        spec = harness.spec_from_dict(data, origin="test")
        seen: list[Path] = []
        run_trials(spec, RULE_MARKER, rule_sensitive_runner(seen), 1, work_dir=tmp_path / "w")
        content = (seen[0] / "CLAUDE.md").read_text()
        assert content.startswith("# Existing project notes")
        assert RULE_MARKER in content

    def test_agent_exception_is_agent_error(self, tmp_path):
        stats = run_trials(code_spec(), None, crashing_runner, 3, work_dir=tmp_path / "w")
        assert (stats.succeeded, stats.failed) == (0, 3)
        assert stats.errors == {"agent_error": 3}

    def test_agent_non_str_return_is_agent_error(self, tmp_path):
        stats = run_trials(
            code_spec(), None, lambda p, d: 42, 1, work_dir=tmp_path / "w"
        )
        assert stats.errors == {"agent_error": 1}

    def test_code_grader_timeout_is_grader_error(self, tmp_path):
        data = code_spec_dict()
        data["grader"] = {"type": "code", "check": "sleep 5"}
        spec = harness.spec_from_dict(data, origin="test")
        stats = run_trials(
            spec, None, always_good_runner, 1,
            work_dir=tmp_path / "w", grader_timeout_seconds=0.2,
        )
        assert stats.errors == {"grader_error": 1}

    def test_a_check_that_leaves_a_background_child_passes_and_the_child_dies(self, tmp_path):
        """Review of BL-08: subprocess.run waited for the background child's
        hold on stdout, so a check that exited 0 became a grader_error at the
        timeout, and the child lived on and wrote its marker. The check is the
        reviewer's, with the child's pid recorded."""
        pidfile, marker = tmp_path / "child.pid", tmp_path / "M"
        data = code_spec_dict()
        data["grader"] = {"type": "code", "check": (
            f"(sleep 4; touch {marker}) & echo $! > {pidfile}; echo graded-ok; exit 0")}
        spec = harness.spec_from_dict(data, origin="test")
        child = None
        try:
            started = time.monotonic()
            stats = run_trials(spec, None, always_good_runner, 1,
                               work_dir=tmp_path / "w", grader_timeout_seconds=2.0)
            elapsed = time.monotonic() - started
            child = int(pidfile.read_text())
            deadline = time.monotonic() + 3
            while True:
                try:
                    os.kill(child, 0)
                except ProcessLookupError:
                    child = None
                    break
                assert time.monotonic() < deadline, f"check child {child} outlived the grade"
                time.sleep(0.05)
        finally:
            if child is not None:
                os.kill(child, signal.SIGKILL)
        # The wait above outlasts the child's own 4 s. Without the group kill
        # it ends because the child finished, and the child wrote its marker.
        assert not marker.exists(), "the check's child ran on and wrote its marker"
        assert (stats.succeeded, stats.errors) == (1, {})
        result = json.loads((tmp_path / "w" / "trial-00" / "result.json").read_text())
        assert result["outcome"] == "pass"
        assert result["grader"] == {"returncode": 0, "stdout": "graded-ok\n", "stderr": "",
                                    "held_streams": []}
        assert elapsed < 4, f"grading took {elapsed:.2f} s"

    @pytest.mark.parametrize("shell", [
        "sh",
        pytest.param("dash", marks=pytest.mark.skipif(
            not os.path.exists("/bin/dash"), reason="/bin/dash is not installed")),
    ])
    def test_a_check_that_signals_its_own_group_is_still_graded(self, tmp_path, shell):
        """Review of the grader fix: `trap 'kill 0' EXIT` in a check signalled
        the provider guard too, and a correct trial became grader_error.
        The check ignores SIGTERM after it starts its background child: dash
        (/bin/sh on Ubuntu) dies of its own `kill 0` otherwise, which is a
        true graded fail. The dash case runs dash on a Mac too."""
        check = "trap 'kill 0' EXIT; sleep 30 & trap '' TERM; echo graded-ok; exit 0"
        if shell == "dash":
            check = f"exec /bin/dash -c {shlex.quote(check)}"
        data = code_spec_dict()
        data["grader"] = {"type": "code", "check": check}
        spec = harness.spec_from_dict(data, origin="test")
        stats = run_trials(spec, None, always_good_runner, 1,
                           work_dir=tmp_path / "w", grader_timeout_seconds=10.0)
        result = json.loads((tmp_path / "w" / "trial-00" / "result.json").read_text())
        assert (stats.succeeded, stats.errors, result["error"]) == (1, {}, "")
        assert result["grader"]["returncode"] == 0

    @pytest.mark.parametrize("check", [
        r"printf 'a\r\nb\rc'; exit 0",   # newlines translated as text=True did
        r"printf '\377'; exit 0",        # not decodable: still a grader crash
    ], ids=["newlines", "undecodable"])
    def test_check_output_decodes_as_subprocess_text_mode_did(self, tmp_path, check):
        """The grader now reads bytes from the provider runner and decodes
        them itself. Compare it with subprocess.run(text=True) on the same
        check, in this environment, for the stored outcome and output."""
        try:
            old = subprocess.run(["/bin/sh", "-c", check], capture_output=True, text=True)
            expected = ("pass", {"returncode": 0, "stdout": old.stdout, "stderr": old.stderr,
                                 "held_streams": []})
        except UnicodeDecodeError:
            expected = ("grader_error", {})
        data = code_spec_dict()
        data["grader"] = {"type": "code", "check": check}
        spec = harness.spec_from_dict(data, origin="test")
        run_trials(spec, None, always_good_runner, 1, work_dir=tmp_path / "w",
                   grader_timeout_seconds=10.0)
        result = json.loads((tmp_path / "w" / "trial-00" / "result.json").read_text())
        assert (result["outcome"], result["grader"]) == expected
        if expected[0] == "grader_error":
            assert result["error"].startswith("UnicodeDecodeError:")

    def test_a_check_whose_escaped_child_holds_the_pipes_records_them(self, tmp_path):
        """A child that calls setsid escapes the group kill. The grade still
        ends, and result.json names the pipes that child still held."""
        pidfile, script = tmp_path / "escaped.pid", tmp_path / "escape.py"
        # The child records its pid only after setsid, and the parent waits
        # for that, so the group kill can never reach the child first.
        script.write_text("import os, time\n"
                          "if os.fork() == 0:\n"
                          "    os.setsid()\n"
                          f"    open({str(pidfile)!r} + '.tmp', 'w').write(str(os.getpid()))\n"
                          f"    os.rename({str(pidfile)!r} + '.tmp', {str(pidfile)!r})\n"
                          "    time.sleep(30); os._exit(0)\n"
                          f"while not os.path.exists({str(pidfile)!r}):\n    time.sleep(0.01)\n")
        data = code_spec_dict()
        data["grader"] = {"type": "code",
                          "check": f"{sys.executable} {script}; echo graded-ok; exit 0"}
        spec = harness.spec_from_dict(data, origin="test")
        try:
            stats = run_trials(spec, None, always_good_runner, 1, work_dir=tmp_path / "w",
                               grader_timeout_seconds=2.0)
        finally:
            if pidfile.exists():
                escaped = int(pidfile.read_text())
                try:
                    os.kill(escaped, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                deadline = time.monotonic() + 3
                while True:
                    try:
                        os.kill(escaped, 0)
                    except ProcessLookupError:
                        break
                    assert time.monotonic() < deadline, f"could not clean up {escaped}"
                    time.sleep(0.05)
        result = json.loads((tmp_path / "w" / "trial-00" / "result.json").read_text())
        assert (stats.succeeded, result["outcome"]) == (1, "pass")
        assert result["grader"]["stdout"] == "graded-ok\n"
        assert result["grader"]["held_streams"] == ["stdout", "stderr"]

    def test_model_grader_receives_rubric_and_summary(self, tmp_path):
        calls: list[tuple[str, str]] = []

        def grader(rubric: str, summary: str) -> bool:
            calls.append((rubric, summary))
            return True

        stats = run_trials(
            model_spec(), None, always_good_runner, 1, grader, work_dir=tmp_path / "w"
        )
        assert (stats.succeeded, stats.failed) == (1, 0)
        rubric, summary = calls[0]
        assert rubric == "Did the agent write GOOD?"
        assert "always good" in summary          # agent final output
        assert "answer.txt" in summary           # sandbox file listing
        assert "GOOD" in summary                 # file contents included

    def test_model_grader_false_is_graded_fail_not_grader_error(self, tmp_path):
        stats = run_trials(
            model_spec(), None, always_good_runner, 2,
            lambda rubric, summary: False, work_dir=tmp_path / "w",
        )
        assert stats.errors == {"graded_fail": 2}

    def test_model_grader_crash_is_grader_error(self, tmp_path):
        def grader(rubric: str, summary: str) -> bool:
            raise ValueError("judge broke")

        stats = run_trials(model_spec(), None, always_good_runner, 2, grader,
                           work_dir=tmp_path / "w")
        assert stats.errors == {"grader_error": 2}

    def test_model_grader_non_bool_is_grader_error(self, tmp_path):
        stats = run_trials(
            model_spec(), None, always_good_runner, 1,
            lambda rubric, summary: "yes", work_dir=tmp_path / "w",
        )
        assert stats.errors == {"grader_error": 1}

    def test_model_spec_without_model_grader_raises_upfront(self, tmp_path):
        with pytest.raises(ValueError, match="model_grader"):
            run_trials(model_spec(), None, always_good_runner, 1, work_dir=tmp_path / "w")

    def test_zero_trials_raises(self, tmp_path):
        with pytest.raises(ValueError, match="n"):
            run_trials(code_spec(), None, always_good_runner, 0, work_dir=tmp_path / "w")


# ---------------------------------------------------------------------------
# regression.gate verdict matrix + persistence
# ---------------------------------------------------------------------------


class TestGate:
    cfg = Config()  # eval_trials=3, gate_without_min_failures=1, gate_with_min_passes=2

    def test_gated_pass(self, tmp_path):
        result = regression.gate(
            code_spec(), RULE_MARKER, rule_sensitive_runner(), self.cfg,
            work_dir=tmp_path / "g",
        )
        assert result["verdict"] == "gated_pass"
        assert result["without_stats"]["failed"] == 3
        assert result["without_stats"]["errors"] == {"graded_fail": 3}
        assert result["with_stats"]["succeeded"] == 3

    def test_ungated_when_eval_cannot_detect_mistake(self, tmp_path):
        # agent does the right thing even without the rule -> eval detects nothing
        result = regression.gate(
            code_spec(), RULE_MARKER, always_good_runner, self.cfg,
            work_dir=tmp_path / "g",
        )
        assert result["verdict"] == "ungated"
        assert result["without_stats"]["succeeded"] == 3
        assert result["with_stats"] is None

    def test_gated_fail_when_rule_does_not_help(self, tmp_path):
        result = regression.gate(
            code_spec(), RULE_MARKER, always_bad_runner, self.cfg,
            work_dir=tmp_path / "g",
        )
        assert result["verdict"] == "gated_fail"
        assert result["with_stats"]["errors"] == {"graded_fail": 3}

    def test_a_without_arm_that_never_ran_is_an_error_not_a_verdict(self, tmp_path):
        """An arm whose agent never ran cannot establish a verdict about the rule.
        Record infrastructure errors separately. They do not vote, so a gate with
        insufficient executed trials remains inconclusive and held.
        """
        result = regression.gate(
            code_spec(), RULE_MARKER, crashing_runner, self.cfg,
            work_dir=tmp_path / "g",
        )
        assert result["verdict"] == "error"
        assert result["without_stats"]["errors"] == {"agent_error": 3}
        assert result["with_stats"] is None, (
            "the with-arm must not run when the without-arm tested nothing"
        )

    def test_an_arm_that_detected_the_mistake_is_not_mistaken_for_a_dead_one(
        self, tmp_path
    ):
        """The control, and the reason `succeeded` is not the test.

        `succeeded` counts only `pass`. A healthy without-arm that reproduced
        the mistake in all three trials has succeeded == 0 and failed == 3,
        exactly like a dead one. Keying on it would have turned every strong
        detection into an error — the same bug pointing the other way.
        """
        result = regression.gate(
            code_spec(), RULE_MARKER, rule_sensitive_runner(), self.cfg,
            work_dir=tmp_path / "g",
        )
        assert result["without_stats"]["succeeded"] == 0
        assert result["without_stats"]["errors"] == {"graded_fail": 3}
        assert result["verdict"] == "gated_pass", (
            "a fully-detecting without-arm was misread as a dead one"
        )

    def test_one_infra_error_among_real_results_still_yields_a_verdict(
        self, tmp_path
    ):
        """Only an arm where EVERY trial was uninformative is an error. A
        partly-broken arm still carries real evidence and must still decide."""
        calls = {"n": 0}

        def flaky(prompt, sandbox, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("agent died on the first trial only")
            return always_good_runner(prompt, sandbox, **kw)

        result = regression.gate(
            code_spec(), RULE_MARKER, flaky, self.cfg, work_dir=tmp_path / "g",
        )
        assert result["without_stats"]["errors"].get("agent_error") == 1
        assert result["verdict"] == "ungated", (
            "two clean trials are real evidence; one crash must not erase them"
        )

    def test_empty_rule_raises(self, tmp_path):
        with pytest.raises(ValueError, match="rule_text"):
            regression.gate(code_spec(), "  ", always_good_runner, self.cfg,
                            work_dir=tmp_path / "g")

    def test_persists_eval_results_row(self, tmp_path):
        store = Store(tmp_path / "state.db")
        try:
            regression.gate(
                code_spec(), RULE_MARKER, rule_sensitive_runner(), self.cfg,
                store=store, work_dir=tmp_path / "g",
            )
            row = store.query_one("SELECT * FROM eval_results")
            assert row is not None
            assert row["kind"] == "regression"
            assert row["subject_id"] == "test-spec-001"
            assert row["verdict"] == "gated_pass"
            assert (row["attempted"], row["succeeded"], row["failed"]) == (6, 3, 3)
            assert json.loads(row["error_taxonomy_json"]) == {"graded_fail": 3}
            metrics = json.loads(row["metrics_json"])
            assert metrics["without"]["failed"] == 3
            assert metrics["with"]["succeeded"] == 3
            assert metrics["detected_failures_without_rule"] == 3
            assert row["started"] and row["finished"]
        finally:
            store.close()


# ---------------------------------------------------------------------------
# regression.generate_spec
# ---------------------------------------------------------------------------


TEMPLATE = (
    "Generate a regression eval.\n\nRULE:\n{{rule}}\n\nWHY:\n{{why}}\n\n"
    "INCIDENT:\n{{incident_summary}}\n"
)


def learning_dict() -> dict:
    return {
        "id": "learn-abc123",
        "rule_text": "Always read timestamps from the data, never mtime.",
        "why": "mtime records touch time, not content time.",
        "incident_summary": "Backfill stamped local-midnight dates from file mtimes.",
    }


class TestGenerateSpec:
    def _prompts_dir(self, tmp_path: Path, template: str = TEMPLATE) -> Path:
        d = tmp_path / "prompts"
        d.mkdir()
        (d / "gen_regression_eval.md").write_text(template, encoding="utf-8")
        return d

    def _llm_response(self) -> dict:
        return {k: v for k, v in code_spec_dict().items() if k != "id"}

    def test_happy_path_substitutes_and_writes_yaml(self, tmp_path):
        prompts = self._prompts_dir(tmp_path)
        out_dir = tmp_path / "out"
        prompts_seen: list[str] = []

        def llm_json(prompt: str) -> dict:
            prompts_seen.append(prompt)
            return self._llm_response()

        spec = regression.generate_spec(
            learning_dict(), llm_json, prompts, out_dir=out_dir
        )
        assert spec.id == "learn-abc123"
        prompt = prompts_seen[0]
        assert learning_dict()["rule_text"] in prompt
        assert learning_dict()["why"] in prompt
        assert learning_dict()["incident_summary"] in prompt
        assert "{{" not in prompt  # every placeholder substituted
        # written YAML round-trips through load_spec to the same spec
        written = out_dir / "learn-abc123.yaml"
        assert written.is_file()
        assert load_spec(written) == spec

    def test_missing_template_raises(self, tmp_path):
        empty = tmp_path / "prompts"
        empty.mkdir()
        with pytest.raises(FileNotFoundError, match="gen_regression_eval.md"):
            regression.generate_spec(
                learning_dict(), lambda p: self._llm_response(), empty,
                out_dir=tmp_path / "out",
            )

    def test_template_missing_placeholder_raises(self, tmp_path):
        prompts = self._prompts_dir(tmp_path, template="RULE: {{rule}} WHY: {{why}}\n")
        with pytest.raises(SpecError, match="incident_summary"):
            regression.generate_spec(
                learning_dict(), lambda p: self._llm_response(), prompts,
                out_dir=tmp_path / "out",
            )

    def test_missing_learning_key_raises(self, tmp_path):
        prompts = self._prompts_dir(tmp_path)
        learning = learning_dict()
        del learning["incident_summary"]
        with pytest.raises(SpecError, match="incident_summary"):
            regression.generate_spec(
                learning, lambda p: self._llm_response(), prompts,
                out_dir=tmp_path / "out",
            )

    def test_llm_response_missing_key_raises(self, tmp_path):
        prompts = self._prompts_dir(tmp_path)
        response = self._llm_response()
        del response["grader"]
        with pytest.raises(SpecError, match="grader"):
            regression.generate_spec(
                learning_dict(), lambda p: response, prompts, out_dir=tmp_path / "out"
            )

    def test_llm_response_extra_key_raises(self, tmp_path):
        prompts = self._prompts_dir(tmp_path)
        response = self._llm_response()
        response["id"] = "llm-tried-to-set-id"
        with pytest.raises(SpecError, match="unknown"):
            regression.generate_spec(
                learning_dict(), lambda p: response, prompts, out_dir=tmp_path / "out"
            )

    def test_llm_response_invalid_spec_raises(self, tmp_path):
        prompts = self._prompts_dir(tmp_path)
        response = self._llm_response()
        response["grader"] = {"type": "code"}  # missing 'check'
        with pytest.raises(SpecError, match="check"):
            regression.generate_spec(
                learning_dict(), lambda p: response, prompts, out_dir=tmp_path / "out"
            )


# ---------------------------------------------------------------------------
# self_eval: labeled loader strictness + recall math
# ---------------------------------------------------------------------------


def labeled_dict(item_id: str = "lab-1", gist: str = "read timestamps from data") -> dict:
    return {
        "id": item_id,
        "title": "mtime-as-logical-time bug",
        "date_range": {"start": "2026-08-01T00:00:00Z", "end": "2026-08-14T00:00:00Z"},
        "source_session_ids": ["sess-1", "sess-2"],
        "expected_rule_gist": gist,
        "already_encoded_in": "~/.claude/CLAUDE.md",
    }


class TestLoadLabeled:
    def _write(self, d: Path, name: str, data: dict) -> None:
        (d / name).write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    def test_valid_file_loads(self, tmp_path):
        self._write(tmp_path, "a.yaml", labeled_dict())
        items = self_eval.load_labeled(tmp_path)
        assert len(items) == 1
        assert items[0]["id"] == "lab-1"

    def test_sorted_by_filename(self, tmp_path):
        self._write(tmp_path, "b.yaml", labeled_dict("lab-b"))
        self._write(tmp_path, "a.yaml", labeled_dict("lab-a"))
        assert [i["id"] for i in self_eval.load_labeled(tmp_path)] == ["lab-a", "lab-b"]

    def test_missing_dir_raises(self, tmp_path):
        with pytest.raises(LabeledDataError, match="does not exist"):
            self_eval.load_labeled(tmp_path / "nope")

    def test_empty_dir_raises(self, tmp_path):
        with pytest.raises(LabeledDataError, match="no labeled"):
            self_eval.load_labeled(tmp_path)

    @pytest.mark.parametrize("key", list(self_eval.LABELED_KEYS))
    def test_missing_key_raises(self, tmp_path, key):
        data = labeled_dict()
        del data[key]
        self._write(tmp_path, "a.yaml", data)
        with pytest.raises(LabeledDataError, match=key):
            self_eval.load_labeled(tmp_path)

    def test_extra_key_raises(self, tmp_path):
        data = labeled_dict()
        data["notes"] = "extra"
        self._write(tmp_path, "a.yaml", data)
        with pytest.raises(LabeledDataError, match="notes"):
            self_eval.load_labeled(tmp_path)

    def test_bad_date_range_raises(self, tmp_path):
        data = labeled_dict()
        data["date_range"] = "2026-08-01..2026-08-14"
        self._write(tmp_path, "a.yaml", data)
        with pytest.raises(LabeledDataError, match="date_range"):
            self_eval.load_labeled(tmp_path)

    def test_bad_session_ids_raises(self, tmp_path):
        data = labeled_dict()
        data["source_session_ids"] = "sess-1"
        self._write(tmp_path, "a.yaml", data)
        with pytest.raises(LabeledDataError, match="source_session_ids"):
            self_eval.load_labeled(tmp_path)


def substring_judge(expected_gist: str, rule_text: str) -> bool:
    return expected_gist.lower() in rule_text.lower()


class TestEvaluate:
    labeled = [
        labeled_dict("lab-1", gist="timestamps from data"),
        labeled_dict("lab-2", gist="verify model identity"),
    ]

    def test_recall_counts_only_found_and_flagged(self):
        produced = [
            {"id": "l1", "rule_text": "Read timestamps from data, never mtime.",
             "duplicate_of": "fingerprint-1"},
            {"id": "l2", "rule_text": "Always verify model identity in responses.",
             "duplicate_of": ""},  # found but NOT flagged duplicate -> dedupe failure
        ]
        result = self_eval.evaluate(produced, self.labeled, substring_judge)
        assert result["recall"] == 0.5
        assert result["per_item"] == [
            {"id": "lab-1", "found": True, "flagged_duplicate": True},
            {"id": "lab-2", "found": True, "flagged_duplicate": False},
        ]
        assert result["dedupe_failures"] == 1

    def test_not_found_item(self):
        produced = [{"id": "l1", "rule_text": "unrelated rule", "duplicate_of": "x"}]
        result = self_eval.evaluate(produced, self.labeled, substring_judge)
        assert result["recall"] == 0.0
        assert result["per_item"][0] == {
            "id": "lab-1", "found": False, "flagged_duplicate": False,
        }
        assert result["dedupe_failures"] == 0

    def test_full_recall(self):
        produced = [
            {"id": "l1", "rule_text": "timestamps from data only", "duplicate_of": "a"},
            {"id": "l2", "rule_text": "verify model identity always", "duplicate_of": "b"},
        ]
        assert self_eval.evaluate(produced, self.labeled, substring_judge)["recall"] == 1.0

    def test_empty_labeled_raises(self):
        with pytest.raises(LabeledDataError, match="no labeled"):
            self_eval.evaluate([], [], substring_judge)

    def test_produced_missing_keys_raises(self):
        with pytest.raises(LabeledDataError, match="duplicate_of"):
            self_eval.evaluate(
                [{"id": "l1", "rule_text": "x"}], self.labeled, substring_judge
            )

    def test_precision_sample_deterministic_and_capped(self):
        produced = [
            {"id": f"l{i:03d}", "rule_text": f"rule {i}", "duplicate_of": ""}
            for i in range(30)
        ]
        r1 = self_eval.evaluate(produced, self.labeled, substring_judge)
        r2 = self_eval.evaluate(list(reversed(produced)), self.labeled, substring_judge)
        sample = r1["sample_for_precision"]
        assert len(sample) == 20
        assert sample == r2["sample_for_precision"]  # order-of-input independent
        assert all(s in produced for s in sample)

    def test_small_produced_sampled_entirely(self):
        produced = [{"id": "l1", "rule_text": "r", "duplicate_of": ""}]
        result = self_eval.evaluate(produced, self.labeled, substring_judge)
        assert result["sample_for_precision"] == produced


# ---------------------------------------------------------------------------
# ab: effectiveness delta
# ---------------------------------------------------------------------------


class TestAB:
    def _stats(self, succeeded: int, attempted: int) -> TrialStats:
        return TrialStats(
            attempted=attempted, succeeded=succeeded, failed=attempted - succeeded,
            errors={}, transcripts_dir="",
        )

    def test_delta_from_trialstats(self):
        delta = ab.effectiveness_delta(self._stats(3, 3), self._stats(1, 3))
        assert delta == pytest.approx(2 / 3)

    def test_delta_from_dicts(self):
        with_stats = dataclasses.asdict(self._stats(2, 3))
        without_stats = dataclasses.asdict(self._stats(2, 3))
        assert ab.effectiveness_delta(with_stats, without_stats) == pytest.approx(0.0)

    def test_negative_delta(self):
        assert ab.effectiveness_delta(self._stats(0, 3), self._stats(3, 3)) == pytest.approx(-1.0)

    def test_zero_attempted_raises(self):
        with pytest.raises(ValueError, match="attempted"):
            ab.effectiveness_delta(self._stats(0, 0), self._stats(1, 3))

    def test_bad_type_raises(self):
        with pytest.raises(TypeError, match="TrialStats"):
            ab.effectiveness_delta(3, self._stats(1, 3))


# ---------------------------------------------------------------------------
# ab.rerun_applied: the continuous pruning runtime
# ---------------------------------------------------------------------------


class TestRerunApplied:
    def _store(self, tmp_path: Path) -> Store:
        return Store(tmp_path / "state.db")

    def _seed_applied(
        self,
        store: Store,
        lid: str,
        applied_at: str,
        target: str = "/targets/CLAUDE.md",
        with_proposal: bool = True,
        target_kind: str = "global_claude_md",
    ) -> None:
        store.insert(
            "learnings",
            {"id": lid, "rule_text": RULE_MARKER, "status": "applied",
             "created_at": applied_at},
        )
        if with_proposal:
            store.insert(
                "proposals",
                {
                    "id": f"prop-{lid}",
                    "learning_id": lid,
                    "target_path": target,
                    "target_kind": target_kind,
                    "action": "add",
                    "status": "applied",
                    "applied_at": applied_at,
                    "created_at": applied_at,
                },
            )
        store.commit()

    def _write_spec(self, regression_dir: Path, lid: str) -> None:
        regression_dir.mkdir(parents=True, exist_ok=True)
        data = code_spec_dict()
        data["id"] = lid
        (regression_dir / f"{lid}.yaml").write_text(
            yaml.safe_dump(data, sort_keys=False), encoding="utf-8"
        )

    def test_still_needed_no_proposal(self, tmp_path):
        store = self._store(tmp_path)
        try:
            self._seed_applied(store, "learn-ab-1", "2026-08-01T00:00:00.000000Z")
            self._write_spec(tmp_path / "regr", "learn-ab-1")
            # rule-sensitive agent WITHOUT the rule fails every trial
            result = ab.rerun_applied(
                store, Config(), rule_sensitive_runner(), tmp_path / "regr",
                work_dir=tmp_path / "w",
            )
            assert result["attempted"] == 1
            assert result["still_needed"] == 1
            assert result["prunable"] == 0
            assert result["inconclusive"] == 0
            assert result["skipped_no_spec"] == 0
            assert result["deferred"] == 0
            assert result["proposals"] == []
            row = store.query_one("SELECT * FROM eval_results")
            assert row["kind"] == "ab"
            assert row["subject_id"] == "learn-ab-1"
            assert row["verdict"] == "still_needed"
            assert json.loads(row["error_taxonomy_json"]) == {"graded_fail": 3}
        finally:
            store.close()

    def test_prunable_yields_delete_proposal(self, tmp_path):
        store = self._store(tmp_path)
        try:
            self._seed_applied(store, "learn-ab-1", "2026-08-01T00:00:00.000000Z")
            self._write_spec(tmp_path / "regr", "learn-ab-1")
            # agent behaves even without the rule -> rule no longer needed
            result = ab.rerun_applied(
                store, Config(), always_good_runner, tmp_path / "regr",
                work_dir=tmp_path / "w",
            )
            assert result["attempted"] == 1
            assert result["prunable"] == 1
            assert result["still_needed"] == 0
            row = store.query_one("SELECT * FROM eval_results")
            assert row["verdict"] == "prunable"
            assert (row["attempted"], row["succeeded"], row["failed"]) == (3, 3, 0)
            assert result["proposals"] == [
                {
                    "learning_id": "learn-ab-1",
                    "target_path": "/targets/CLAUDE.md",
                    "target_kind": "global_claude_md",
                    "action": "delete",
                    "marker_id": "learn-ab-1",
                    "eval_result_id": row["id"],
                }
            ]
            # rerun_applied never writes proposals rows itself
            assert store.query("SELECT * FROM proposals WHERE action = 'delete'") == []
        finally:
            store.close()

    def test_pipeline_persists_the_prunable_rule_as_a_reviewed_delete(self, tmp_path):
        """Review of BL-11: a prune result names the rule by marker_id, but the
        pipeline built the delete from the bare learning row, which carries no
        delete_marker_id. Every prunable rule raised ProposalError and was
        counted as persisted anyway."""
        from self_improve import propose
        from self_improve.pipeline import _persist_prune_proposals

        lid = "learn-ab-1"
        target = tmp_path / "AGENTS.md"
        target.write_text(f"# Rules\n\n- keep tests green <!-- si:{lid} -->\n- unrelated rule\n")
        store = self._store(tmp_path)
        try:
            # Not the global kind: a hardcoded global_claude_md must not pass.
            self._seed_applied(store, lid, "2026-08-01T00:00:00.000000Z", target=str(target),
                               target_kind="project_agents_md")
            self._write_spec(tmp_path / "regr", lid)
            result = ab.rerun_applied(
                store, Config(), always_good_runner, tmp_path / "regr",
                work_dir=tmp_path / "w",
            )
            taxonomy: dict[str, int] = {}

            def bump(key: str) -> None:
                taxonomy[key] = taxonomy.get(key, 0) + 1

            persisted = _persist_prune_proposals(
                store, Config(), "run-1", result["proposals"], bump
            )
            assert taxonomy == {}
            assert persisted == 1
            row = store.query_one("SELECT * FROM proposals WHERE action = 'delete'")
            assert row["learning_id"] == lid
            assert row["target_kind"] == "project_agents_md"
            assert row["target_path"] == str(target)
            assert row["status"] == "gated_pass"
            assert row["run_id"] == "run-1"
            assert row["eval_result_id"] == result["proposals"][0]["eval_result_id"]
            after = propose.apply_unified_diff(target.read_text(), row["diff_unified"])
            assert after == "# Rules\n\n- unrelated rule\n"
        finally:
            store.close()

    def test_a_rule_with_an_open_delete_proposal_is_not_retested(self, tmp_path):
        """Second review of BL-11: a persisted delete proposal leaves the
        learning applied, so every later night re-tested the rule, spent its
        gate calls and filed another delete proposal for the same line."""
        from self_improve.pipeline import _persist_prune_proposals

        lid = "learn-ab-1"
        target = tmp_path / "CLAUDE.md"
        target.write_text(f"# Rules\n\n- keep tests green <!-- si:{lid} -->\n")
        store = self._store(tmp_path)
        try:
            self._seed_applied(store, lid, "2026-08-01T00:00:00.000000Z", target=str(target))
            self._write_spec(tmp_path / "regr", lid)
            nights = []
            for night in ("run-1", "run-2"):
                result = ab.rerun_applied(store, Config(), always_good_runner,
                                          tmp_path / "regr", work_dir=tmp_path / night)
                _persist_prune_proposals(store, Config(), night, result["proposals"], lambda k: None)
                store.commit()
                nights.append(result)
            assert nights[0]["prunable"] == 1
            assert (nights[1]["attempted"], nights[1]["skipped_open_delete"]) == (0, 1)
            assert nights[1]["proposals"] == []
            rows = store.query("SELECT run_id FROM proposals WHERE action = 'delete'")
            assert [r["run_id"] for r in rows] == ["run-1"]
            assert len(store.query("SELECT id FROM eval_results WHERE kind = 'ab'")) == 1
        finally:
            store.close()

    def test_the_zero_attempt_reason_names_every_skip_it_counts(self, tmp_path):
        """Third review of BL-11: with one rule skipped for an open delete
        proposal and another without a spec, the reason said that no
        candidate had a spec, while skipped_no_spec was 1."""
        from self_improve.pipeline import _persist_prune_proposals

        target = tmp_path / "CLAUDE.md"
        target.write_text("# Rules\n\n- one <!-- si:learn-a -->\n- two <!-- si:learn-b -->\n")
        store = self._store(tmp_path)
        try:
            self._seed_applied(store, "learn-a", "2026-08-01T00:00:00.000000Z", target=str(target))
            self._seed_applied(store, "learn-b", "2026-08-02T00:00:00.000000Z", target=str(target))
            self._write_spec(tmp_path / "regr", "learn-a")
            for night in ("run-1", "run-2"):
                result = ab.rerun_applied(store, Config(), always_good_runner,
                                          tmp_path / "regr", work_dir=tmp_path / night)
                _persist_prune_proposals(store, Config(), night, result["proposals"], lambda k: None)
                store.commit()
            assert (result["attempted"], result["skipped_open_delete"], result["skipped_no_spec"]) == (0, 1, 1)
            assert "none had a regression spec" not in result["reason"], result["reason"]
            assert "1 already have an open delete proposal" in result["reason"], result["reason"]
            assert "1 had no regression spec" in result["reason"], result["reason"]
        finally:
            store.close()

    @staticmethod
    def _seed_delete(store: Store, lid: str, status: str, applied_at: str) -> None:
        store.insert("proposals", {
            "id": f"del-{lid}", "learning_id": lid, "target_path": "/targets/CLAUDE.md",
            "target_kind": "global_claude_md", "action": "delete", "status": status,
            "applied_at": applied_at, "created_at": applied_at or "2026-08-09T00:00:00.000000Z"})
        store.commit()

    def test_a_delivered_delete_is_not_retested_every_night(self, tmp_path):
        """Fourth review of BL-11: nothing sets a learning to pruned, so after
        its delete was delivered the learning stayed applied and the delete
        became its latest applied proposal. Every night re-ran the
        without-rule arm against a rule no longer in the file."""
        from self_improve import propose
        from self_improve.pipeline import _persist_prune_proposals

        lid = "learn-ab-1"
        target = tmp_path / "CLAUDE.md"
        target.write_text(f"# Rules\n\n- keep tests green <!-- si:{lid} -->\n- other\n")
        store = self._store(tmp_path)
        try:
            self._seed_applied(store, lid, "2026-08-01T00:00:00.000000Z", target=str(target))
            self._write_spec(tmp_path / "regr", lid)
            first = ab.rerun_applied(store, Config(), always_good_runner,
                                     tmp_path / "regr", work_dir=tmp_path / "run-1")
            assert _persist_prune_proposals(store, Config(), "run-1", first["proposals"],
                                            lambda k: None) == 1
            # Deliver the delete the way an approved write leaves it.
            delete = store.query_one("SELECT * FROM proposals WHERE action = 'delete'")
            target.write_text(propose.apply_unified_diff(target.read_text(), delete["diff_unified"]))
            store.update("proposals", "id", delete["id"],
                         {"status": "applied", "applied_at": "2026-08-03T00:00:00.000000Z"})
            store.commit()
            bumps: list[str] = []
            for night in ("run-2", "run-3"):
                result = ab.rerun_applied(store, Config(), always_good_runner,
                                          tmp_path / "regr", work_dir=tmp_path / night)
                _persist_prune_proposals(store, Config(), night, result["proposals"], bumps.append)
                store.commit()
                assert (result["attempted"], result["skipped_delivered_delete"]) == (0, 1), result
                assert result["proposals"] == []
                assert result["reason"].startswith("delivered_delete:"), result["reason"]
                assert "1 already had their delete delivered" in result["reason"], result["reason"]
            assert bumps == []
            assert len(store.query("SELECT id FROM eval_results WHERE kind = 'ab'")) == 1
            assert store.query_one("SELECT status FROM learnings WHERE id = ?", (lid,))["status"] == "applied"
        finally:
            store.close()

    def test_delete_skips_never_consume_or_dodge_the_per_run_cap(self, tmp_path):
        """Fourth review of BL-11: no test held the claim that a skipped rule
        is skipped before the cap. With cap 1, skips placed before and after
        the one attempt must keep their own counts and leave the slot free."""
        cfg = dataclasses.replace(Config(), ab_prune_max_rules_per_run=1)
        store = self._store(tmp_path)
        try:
            # Oldest first: open, delivered, ATTEMPTED, deferred, open, delivered.
            plan = [("learn-a", "open"), ("learn-b", "delivered"), ("learn-c", "run"),
                    ("learn-d", "run"), ("learn-e", "open"), ("learn-f", "delivered")]
            for day, (lid, kind) in enumerate(plan, start=1):
                self._seed_applied(store, lid, f"2026-08-{day:02d}T00:00:00.000000Z")
                self._write_spec(tmp_path / "regr", lid)
                if kind == "open":
                    self._seed_delete(store, lid, "gated_pass", "")
                elif kind == "delivered":
                    self._seed_delete(store, lid, "applied", f"2026-08-{day:02d}T12:00:00.000000Z")
            assert len(plan) > cfg.ab_prune_max_rules_per_run
            result = ab.rerun_applied(store, cfg, always_good_runner, tmp_path / "regr",
                                      work_dir=tmp_path / "w")
            counts = {k: result[k] for k in ("attempted", "prunable", "deferred", "skipped_no_spec",
                                             "skipped_open_delete", "skipped_delivered_delete")}
            assert counts == {"attempted": 1, "prunable": 1, "deferred": 1, "skipped_no_spec": 0,
                              "skipped_open_delete": 2, "skipped_delivered_delete": 2}, counts
            assert [p["learning_id"] for p in result["proposals"]] == ["learn-c"]
        finally:
            store.close()

    def test_a_prune_result_without_a_learning_row_is_counted(self, tmp_path):
        """A vanished learning is a named outcome, not a silent skip."""
        from self_improve.pipeline import _persist_prune_proposals

        store = self._store(tmp_path)
        try:
            taxonomy: dict[str, int] = {}
            persisted = _persist_prune_proposals(
                store, Config(), "run-1",
                [{"learning_id": "gone", "target_path": str(tmp_path / "CLAUDE.md"),
                  "target_kind": "global_claude_md", "action": "delete",
                  "marker_id": "gone", "eval_result_id": "e1"}],
                lambda key: taxonomy.__setitem__(key, taxonomy.get(key, 0) + 1),
            )
            assert persisted == 0
            assert taxonomy == {"prune_learning_missing": 1}
            assert store.query("SELECT * FROM proposals") == []
        finally:
            store.close()

    @staticmethod
    def _whole_file_target(tmp_path: Path, kind: str, lid: str) -> tuple[Path, str]:
        """A delivered target whose rule does not live only in its marker line."""
        from self_improve import propose

        learning = {"id": lid, "title": "Grader reads answer", "rule_text": RULE_MARKER,
                    "why": "the grader reads answer.txt", "path_globs": ["src/**"]}
        if kind == "skill":
            return tmp_path / "skills" / "grader" / "SKILL.md", propose.render_skill_md(learning)
        if kind == "rule_file":
            return tmp_path / ".claude" / "rules" / "grader.md", propose.render_rule_file(learning)
        assert kind == "hook", kind
        # A hook lives in settings.json, which has no si: marker line at all.
        return tmp_path / "settings.json", json.dumps(
            {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
                {"type": "command", "command": "check-answer"}]}]}}, indent=2) + "\n"

    @pytest.mark.parametrize("kind", ["skill", "rule_file", "hook"])
    def test_a_prune_whose_rule_outlives_its_marker_line_is_refused_by_name(self, tmp_path, kind):
        """Fourth review of BL-11: for a skill the delete removed only the
        marker bullet. Approved, it left SKILL.md installed with the rule in
        its description. A rule file keeps its paths frontmatter, and a hook
        has no marker line to remove."""
        from self_improve.pipeline import _persist_prune_proposals

        lid = "learn-ab-1"
        target, content = self._whole_file_target(tmp_path, kind, lid)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        store = self._store(tmp_path)
        try:
            self._seed_applied(store, lid, "2026-08-01T00:00:00.000000Z",
                               target=str(target), target_kind=kind)
            self._write_spec(tmp_path / "regr", lid)
            result = ab.rerun_applied(store, Config(), always_good_runner,
                                      tmp_path / "regr", work_dir=tmp_path / "w")
            assert result["prunable"] == 1, result
            taxonomy: dict[str, int] = {}
            persisted = _persist_prune_proposals(
                store, Config(), "run-1", result["proposals"],
                lambda key: taxonomy.__setitem__(key, taxonomy.get(key, 0) + 1),
            )
            assert (persisted, taxonomy) == (0, {"prune_rule_outside_marker_line": 1})
            assert store.query("SELECT id FROM proposals WHERE action != 'add'") == []
            assert target.read_text(encoding="utf-8") == content
        finally:
            store.close()

    def test_refused_prune_copy_names_what_did_not_change(self, tmp_path):
        """Review fix (BL-11): both prune refusals said "nothing was written",
        but by then the A/B rerun had written its eval result and history and
        spent gate calls. The copy must name only what did not change."""
        from self_improve.failure_presentation import FAILURE_COPY, NOT_FAILURE_TAXONOMY
        from self_improve.pipeline import _persist_prune_proposals

        lid = "learn-ab-1"
        target, content = self._whole_file_target(tmp_path, "skill", lid)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        store = self._store(tmp_path)
        try:
            self._seed_applied(store, lid, "2026-08-01T00:00:00.000000Z",
                               target=str(target), target_kind="skill")
            self._write_spec(tmp_path / "regr", lid)
            result = ab.rerun_applied(store, Config(), always_good_runner,
                                      tmp_path / "regr", work_dir=tmp_path / "w")
            taxonomy: dict[str, int] = {}
            _persist_prune_proposals(
                store, Config(), "run-1", result["proposals"],
                lambda key: taxonomy.__setitem__(key, taxonomy.get(key, 0) + 1),
            )
            assert taxonomy == {"prune_rule_outside_marker_line": 1}
            written = store.query("SELECT id FROM eval_results WHERE subject_id=? AND kind='ab'", (lid,))
            assert len(written) == 1, written
        finally:
            store.close()
        for copy in (NOT_FAILURE_TAXONOMY["prune_rule_outside_marker_line"],
                     FAILURE_COPY["prune_learning_missing"]["explanation"]):
            assert "nothing was written" not in copy, copy
            assert "o delete proposal was created and no instruction file changed" in copy, copy

    def test_the_prune_target_kinds_cover_the_whole_vocabulary(self):
        """A new target kind must be classified before a prune can reach it."""
        from self_improve import pipeline
        from self_improve.execution_policy import TARGET_CLASS

        allowed, refused = pipeline.PRUNE_MARKER_LINE_KINDS, pipeline.PRUNE_REFUSED_KINDS
        assert not allowed & refused
        assert allowed | refused == set(TARGET_CLASS)
        assert {"skill", "rule_file", "hook"} <= refused

    def test_the_refused_prune_is_work_not_performed(self):
        from self_improve.failure_presentation import NOT_FAILURE_TAXONOMY, taxonomy_rows

        assert "prune_rule_outside_marker_line" in NOT_FAILURE_TAXONOMY
        [row] = taxonomy_rows({"prune_rule_outside_marker_line": 2}, owner="apply.taxonomy")
        assert (row["kind"], row["name"], row["count"]) == ("not_failure", "Work not performed", 2)

    def test_a_prune_with_an_unknown_target_kind_fails_by_name(self, tmp_path):
        """Fail loud: a kind outside the vocabulary is not a supported delete."""
        from self_improve.pipeline import _persist_prune_proposals

        lid = "learn-ab-1"
        target = tmp_path / "CLAUDE.md"
        target.write_text(f"# Rules\n\n- keep tests green <!-- si:{lid} -->\n", encoding="utf-8")
        store = self._store(tmp_path)
        try:
            self._seed_applied(store, lid, "2026-08-01T00:00:00.000000Z", target=str(target),
                               target_kind="future_unsupported_kind")
            taxonomy: dict[str, int] = {}
            persisted = _persist_prune_proposals(
                store, Config(), "run-1",
                [{"learning_id": lid, "target_path": str(target),
                  "target_kind": "future_unsupported_kind", "action": "delete",
                  "marker_id": lid, "eval_result_id": "e1"}],
                lambda key: taxonomy.__setitem__(key, taxonomy.get(key, 0) + 1),
            )
            assert (persisted, taxonomy) == (0, {"prune_ProposalError": 1})
            assert store.query("SELECT id FROM proposals WHERE action = 'delete'") == []
        finally:
            store.close()

    def test_an_undecodable_prune_target_is_counted_and_the_batch_continues(self, tmp_path):
        """Fourth review of BL-11: read_text ran outside the try, so one
        non-UTF-8 target raised out of the batch and the pipeline replaced
        every ab_prune count with one error string."""
        from self_improve.pipeline import _persist_prune_proposals

        bad, good = tmp_path / "bad" / "CLAUDE.md", tmp_path / "good" / "CLAUDE.md"
        bad.parent.mkdir()
        good.parent.mkdir()
        bad.write_bytes(b"# Rules\n\n- \xff\xfe broken <!-- si:learn-a -->\n")
        good.write_text("# Rules\n\n- fine <!-- si:learn-b -->\n", encoding="utf-8")
        store = self._store(tmp_path)
        try:
            self._seed_applied(store, "learn-a", "2026-08-01T00:00:00.000000Z", target=str(bad))
            self._seed_applied(store, "learn-b", "2026-08-02T00:00:00.000000Z", target=str(good))
            taxonomy: dict[str, int] = {}
            persisted = _persist_prune_proposals(
                store, Config(), "run-1",
                [{"learning_id": lid, "target_path": str(path), "target_kind": "global_claude_md",
                  "action": "delete", "marker_id": lid, "eval_result_id": "e-" + lid}
                 for lid, path in (("learn-a", bad), ("learn-b", good))],
                lambda key: taxonomy.__setitem__(key, taxonomy.get(key, 0) + 1),
            )
            assert (persisted, taxonomy) == (1, {"prune_UnicodeDecodeError": 1})
            rows = store.query("SELECT learning_id FROM proposals WHERE action = 'delete'")
            assert [r["learning_id"] for r in rows] == ["learn-b"]
        finally:
            store.close()

    def test_the_gates_scenario_spec_is_found_through_the_applied_proposal(self, tmp_path):
        """BL-11: the gate names specs <lid>-s<N>.yaml, but the A/B lookup only
        tried <lid>.yaml, so every applied rule was skipped as no_spec."""
        store = self._store(tmp_path)
        try:
            lid = "learn-ab-1"
            self._seed_applied(store, lid, "2026-08-01T00:00:00.000000Z",
                               target="/targets/AGENTS.md")
            store.update("proposals", "id", f"prop-{lid}",
                         {"eval_result_id": "gate-result", "target_kind": "project_agents_md"})
            store.insert("eval_results", {"id": "gate-result", "kind": "regression",
                                          "subject_id": f"{lid}-s1", "verdict": "gated_pass"})
            store.commit()
            self._write_spec(tmp_path / "regr", f"{lid}-s1")
            result = ab.rerun_applied(
                store, Config(), always_good_runner, tmp_path / "regr",
                work_dir=tmp_path / "w",
            )
            assert (result["attempted"], result["skipped_no_spec"]) == (1, 0), result
            assert result["proposals"][0]["target_kind"] == "project_agents_md"
        finally:
            store.close()

    def test_the_spec_the_majority_gate_generated_is_rerun_through_its_link(self, tmp_path, monkeypatch):
        """Review of BL-11: the test above writes `<lid>-s1.yaml` and the gate
        link by hand, so renaming the generator's files still passed. A later
        review found that this test re-created the pipeline's spec wiring, so
        changing `out_dir=regression_specs_dir(cfg)` in pipeline.py left it
        green. Here pipeline.gate_one_proposal, the gate call site that the
        pipeline and `gate-proposal` share, runs over a real LLMRunner whose
        provider hop (`_execute`) is scripted. The proposal then takes the
        majority's deciding eval_result_id, as the pipeline stores it, and
        rerun_applied reads regression_specs_dir(cfg). Scenario 0 reproduces
        nothing (ungated), so the deciding spec is s1, and the A/B arm must
        run s1's own prompt."""
        from self_improve import pipeline
        from self_improve.llm import LLMRunner, _ExecResult

        cfg = Config(state_dir=str(tmp_path / "state"), eval_sandbox_enabled=False)
        store = self._store(tmp_path)
        try:
            lid, run_id = "learn-chain-1", "run-chain-1"
            self._seed_applied(store, lid, "2026-08-01T00:00:00.000000Z")
            store.insert("runs", {"id": run_id, "started": "2026-08-02T00:00:00.000000Z"})
            store.commit()
            generated: list[int] = []
            gate_agent = rule_sensitive_runner()

            def execute(self, call_id, stage, model_class, prompt, expect_json, **kwargs):
                served = {"outcome": "ok", "provider": "codex", "model_reported": "gpt-5.6-terra",
                          "provider_attempts": 1}
                if stage == "eval_gen":
                    value = {**{k: v for k, v in code_spec_dict().items() if k != "id"},
                             "scenario_prompt": f"Complete the task (variant {len(generated)})."}
                    generated.append(len(generated))
                    return _ExecResult(ok=True, text=json.dumps(value), parsed=value, **served)
                assert stage == "grade", stage
                # Variant 0 has no mistake to catch, so its scenario is ungated.
                agent = always_good_runner if "(variant 0)" in prompt else gate_agent
                return _ExecResult(ok=True, text=agent(prompt, Path(kwargs["sandbox_dir"])),
                                   parsed=None, **served)

            monkeypatch.setattr(LLMRunner, "_execute", execute)
            llm = LLMRunner(cfg, store, run_id, tmp_path / "raw")
            learning = store.query_one("SELECT * FROM learnings WHERE id = ?", (lid,))
            proposal = store.query_one("SELECT * FROM proposals WHERE id = ?", (f"prop-{lid}",))
            gate_stats, taxonomy = pipeline.new_gate_stats(), {}
            pipeline.gate_one_proposal(
                store, cfg, llm, learning, proposal, run_dir=tmp_path / "run",
                sandbox_state={"ok": None, "error": "", "observed": {}}, gate_stats=gate_stats,
                bump_taxonomy=lambda key: taxonomy.__setitem__(key, taxonomy.get(key, 0) + 1),
                run_id=run_id)
            assert taxonomy == {} and generated == list(range(cfg.eval_scenarios)), (taxonomy, generated)
            [split] = gate_stats["scenario_splits"]
            assert (proposal["status"], split["tally"]["ungated"], split["tally"]["gated_pass"]) == (
                "gated_pass", 1, 2), (proposal, split)
            # Spec names are left to the rerun below: that is the link under test.
            # The pipeline stores the majority's deciding result on the proposal.
            store.update("proposals", "id", f"prop-{lid}", {"eval_result_id": proposal["eval_result_id"]})
            store.commit()

            seen = []

            def ab_runner(prompt, sandbox):
                seen.append(prompt)
                return always_good_runner(prompt, sandbox)

            result = ab.rerun_applied(store, cfg, ab_runner, pipeline.regression_specs_dir(cfg),
                                      work_dir=tmp_path / "ab")
            assert (result["attempted"], result["skipped_no_spec"]) == (1, 0), result
            assert result["prunable"] == 1, result
            assert seen and all("(variant 1)" in p for p in seen), seen
        finally:
            store.close()

    def test_agent_errors_are_inconclusive_not_prunable(self, tmp_path):
        store = self._store(tmp_path)
        try:
            self._seed_applied(store, "learn-ab-1", "2026-08-01T00:00:00.000000Z")
            self._write_spec(tmp_path / "regr", "learn-ab-1")
            result = ab.rerun_applied(
                store, Config(), crashing_runner, tmp_path / "regr",
                work_dir=tmp_path / "w",
            )
            assert result["attempted"] == 1
            assert result["inconclusive"] == 1
            assert result["prunable"] == 0
            assert result["proposals"] == []
            row = store.query_one("SELECT * FROM eval_results")
            assert row["verdict"] == "inconclusive"
            assert json.loads(row["error_taxonomy_json"]) == {"agent_error": 3}
        finally:
            store.close()

    def test_mixed_pass_and_infra_error_is_inconclusive(self, tmp_path):
        # zero graded failures but one crashed trial: NOT prunable
        calls = {"n": 0}

        def flaky(prompt: str, sandbox: Path) -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient")
            return always_good_runner(prompt, sandbox)

        store = self._store(tmp_path)
        try:
            self._seed_applied(store, "learn-ab-1", "2026-08-01T00:00:00.000000Z")
            self._write_spec(tmp_path / "regr", "learn-ab-1")
            result = ab.rerun_applied(
                store, Config(), flaky, tmp_path / "regr", work_dir=tmp_path / "w"
            )
            assert result["inconclusive"] == 1
            assert result["prunable"] == 0
            assert result["proposals"] == []
        finally:
            store.close()

    def test_missing_spec_is_counted_never_silent(self, tmp_path):
        store = self._store(tmp_path)
        try:
            self._seed_applied(store, "learn-ab-1", "2026-08-01T00:00:00.000000Z")
            (tmp_path / "regr").mkdir()  # no spec YAML for the learning
            result = ab.rerun_applied(
                store, Config(), always_good_runner, tmp_path / "regr",
                work_dir=tmp_path / "w",
            )
            assert result["skipped_no_spec"] == 1
            assert result["attempted"] == 0
            assert result["proposals"] == []
            assert store.query("SELECT * FROM eval_results") == []
        finally:
            store.close()

    def test_missing_spec_does_not_consume_cap_slot(self, tmp_path):
        cfg = dataclasses.replace(Config(), ab_prune_max_rules_per_run=1)
        store = self._store(tmp_path)
        try:
            # oldest learning has no spec; the next one must still be processed
            self._seed_applied(store, "learn-ab-1", "2026-08-01T00:00:00.000000Z")
            self._seed_applied(store, "learn-ab-2", "2026-08-02T00:00:00.000000Z")
            self._write_spec(tmp_path / "regr", "learn-ab-2")
            result = ab.rerun_applied(
                store, cfg, always_good_runner, tmp_path / "regr",
                work_dir=tmp_path / "w",
            )
            assert result["skipped_no_spec"] == 1
            assert result["attempted"] == 1
            assert result["prunable"] == 1
            assert result["deferred"] == 0
            assert [p["learning_id"] for p in result["proposals"]] == ["learn-ab-2"]
        finally:
            store.close()

    def test_cap_respected_oldest_first_remainder_deferred(self, tmp_path):
        cap = 2
        cfg = dataclasses.replace(Config(), ab_prune_max_rules_per_run=cap)
        store = self._store(tmp_path)
        try:
            lids = [f"learn-ab-{i}" for i in range(1, cap + 3)]  # cap + 2 learnings
            for i, lid in enumerate(lids, start=1):
                self._seed_applied(store, lid, f"2026-08-{i:02d}T00:00:00.000000Z")
                self._write_spec(tmp_path / "regr", lid)
            result = ab.rerun_applied(
                store, cfg, always_good_runner, tmp_path / "regr",
                work_dir=tmp_path / "w",
            )
            assert result["attempted"] == cap
            assert result["prunable"] == cap
            assert result["deferred"] == 2
            # oldest applied_at processed first
            assert [p["learning_id"] for p in result["proposals"]] == lids[:cap]
            subjects = [
                r["subject_id"]
                for r in store.query("SELECT subject_id FROM eval_results")
            ]
            assert sorted(subjects) == lids[:cap]
        finally:
            store.close()

    def test_eval_results_row_shape(self, tmp_path):
        store = self._store(tmp_path)
        try:
            self._seed_applied(
                store, "learn-ab-1", "2026-08-01T00:00:00.000000Z",
                target="/targets/AGENTS.md",
            )
            self._write_spec(tmp_path / "regr", "learn-ab-1")
            ab.rerun_applied(
                store, Config(), always_good_runner, tmp_path / "regr",
                work_dir=tmp_path / "w",
            )
            row = store.query_one("SELECT * FROM eval_results")
            assert row["kind"] == "ab"
            assert row["subject_id"] == "learn-ab-1"
            assert row["started"] and row["finished"]
            assert (row["attempted"], row["succeeded"], row["failed"]) == (3, 3, 0)
            assert json.loads(row["error_taxonomy_json"]) == {}
            metrics = json.loads(row["metrics_json"])
            assert metrics["without"]["attempted"] == 3
            assert metrics["without"]["succeeded"] == 3
            assert metrics["graded_failures_without_rule"] == 0
            assert metrics["eval_trials"] == 3
            assert metrics["gate_without_min_failures"] == 1
            assert metrics["target_path"] == "/targets/AGENTS.md"
            assert metrics["applied_at"] == "2026-08-01T00:00:00.000000Z"
            assert metrics["verdict"] == "prunable"
        finally:
            store.close()

    def test_applied_learning_without_applied_proposal_raises(self, tmp_path):
        store = self._store(tmp_path)
        try:
            self._seed_applied(
                store, "learn-ab-1", "2026-08-01T00:00:00.000000Z",
                with_proposal=False,
            )
            with pytest.raises(ValueError, match="applied proposal"):
                ab.rerun_applied(
                    store, Config(), always_good_runner, tmp_path / "regr",
                    work_dir=tmp_path / "w",
                )
        finally:
            store.close()

    def test_spec_id_mismatch_raises(self, tmp_path):
        store = self._store(tmp_path)
        try:
            self._seed_applied(store, "learn-ab-1", "2026-08-01T00:00:00.000000Z")
            regr = tmp_path / "regr"
            regr.mkdir()
            data = code_spec_dict()
            data["id"] = "some-other-id"
            (regr / "learn-ab-1.yaml").write_text(
                yaml.safe_dump(data, sort_keys=False), encoding="utf-8"
            )
            with pytest.raises(SpecError, match="does not match learning id"):
                ab.rerun_applied(
                    store, Config(), always_good_runner, regr,
                    work_dir=tmp_path / "w",
                )
        finally:
            store.close()


# ---------------------------------------------------------------------------
# Distinguish graded task failures from missing evidence
# ---------------------------------------------------------------------------


def test_both_arms_failing_still_reports_gated_fail(tmp_path):
    """Graded task failures in both arms produce gated_fail.

    This helper receives task-failure counts and no infrastructure errors.
    Runner and grader failures are classified separately by the harness."""
    from self_improve.evals.regression import verdict_for

    v = verdict_for(without_failed=3, without_attempted=3,
                    with_succeeded=0, with_attempted=3, cfg=Config())
    assert v == "gated_fail", (
        "graded failures reproduced without the rule and still failed with it"
    )


def test_a_rule_that_genuinely_does_not_help_is_still_gated_fail(tmp_path):
    """The guard must not swallow real failures.

    without fails, with PARTIALLY succeeds but below the bar: the eval worked,
    the rule was insufficient. That is a real gated_fail.
    """
    from self_improve.evals.regression import verdict_for

    v = verdict_for(without_failed=3, without_attempted=3,
                    with_succeeded=1, with_attempted=3, cfg=Config())
    assert v == "gated_fail"


def test_a_rule_that_helps_still_passes(tmp_path):
    from self_improve.evals.regression import verdict_for

    v = verdict_for(without_failed=3, without_attempted=3,
                    with_succeeded=3, with_attempted=3, cfg=Config())
    assert v == "gated_pass"


def test_an_eval_that_cannot_reproduce_the_mistake_is_still_ungated(tmp_path):
    from self_improve.evals.regression import verdict_for

    v = verdict_for(without_failed=0, without_attempted=3,
                    with_succeeded=0, with_attempted=3, cfg=Config())
    assert v == "ungated"


# ---------------------------------------------------------------------------
# Evidence reaches the regression-spec generator
# ---------------------------------------------------------------------------


class TestGeneratorGetsRealEvidence:
    """Pass retained incident evidence to the regression-spec generator.

    Invented evidence must reach the prompt. Missing evidence must leave no
    template hole, and truncation must be explicitly marked."""

    def _learning(self):
        return {"id": "L1", "rule_text": "r", "why": "w", "incident_summary": "s"}

    def _spec_json(self):
        return {
            "title": "t",
            "scenario_prompt": "do the thing",
            "workspace_files": {"a.txt": "x"},
            "success_criteria": "c",
            "grader": {"type": "code", "check": "true"},
        }

    def test_evidence_reaches_the_prompt(self, tmp_path):
        from self_improve.evals.regression import generate_spec

        seen = {}

        def llm_json(prompt):
            seen["prompt"] = prompt
            return self._spec_json()

        generate_spec(
            self._learning(), llm_json, Path("prompts"),
            out_dir=tmp_path, evidence="AGENT RAN cat missing.txt AND IGNORED THE ERROR",
        )
        assert "AGENT RAN cat missing.txt" in seen["prompt"]

    def test_absent_evidence_is_explicit_not_a_dangling_placeholder(self, tmp_path):
        """A template hole left unfilled would ship '{{evidence}}' to the model."""
        from self_improve.evals.regression import generate_spec

        seen = {}

        def llm_json(prompt):
            seen["prompt"] = prompt
            return self._spec_json()

        generate_spec(self._learning(), llm_json, Path("prompts"), out_dir=tmp_path)
        assert "{{evidence}}" not in seen["prompt"]
        assert "{{" not in seen["prompt"]

    def test_evidence_is_truncated_with_a_visible_marker(self, tmp_path):
        """Any truncation says what it cut, per the repo's cap rule."""
        from self_improve.evals.regression import generate_spec

        seen = {}

        def llm_json(prompt):
            seen["prompt"] = prompt
            return self._spec_json()

        generate_spec(
            self._learning(), llm_json, Path("prompts"),
            out_dir=tmp_path, evidence="Z" * 20000,
        )
        assert "[truncated" in seen["prompt"]


class TestPipelineSuppliesTheEvidence:
    """generate_spec accepting evidence is useless if nothing passes any.

    Caught by sabotage: deleting the pipeline's `evidence=` argument broke no
    test, because the unit tests covered the function and not its caller.
    """

    def _store(self, tmp_path, window):
        from self_improve.store import Store, new_id, utc_now_iso

        s = Store(tmp_path / "s.db")
        sf = "/virtual/s.jsonl"
        s.upsert_session({
            "file_path": sf, "source": "claude", "session_id": "s1",
            "project_path": "/p", "headless": 0, "is_subagent": 0,
            "first_ts": "", "last_ts": "", "mtime": 0.0, "file_size": 0,
            "bytes_scanned": 0, "lines_scanned": 0, "malformed_lines": 0,
            "status": "ok", "error": "", "last_scanned_at": utc_now_iso(),
        })
        iid = new_id()
        s.insert_incident({
            "id": iid, "session_file": sf, "session_id": "s1",
            "project_path": "/p", "ts": "2026-08-10T00:00:00Z",
            "signal_type": "correction", "matched_text": "m", "window": window,
        })
        lid = new_id()
        s.insert("learnings", {
            "id": lid, "rule_text": "r", "why": "w", "category": "tooling",
            "scope": "project", "evidence_count": 1, "project_count": 1,
            "projects_json": "[]", "first_seen": "", "last_seen": "",
            "confidence": 0.7, "status": "candidate", "duplicate_of": "",
            "created_at": utc_now_iso(),
        })
        s.link_incident_learning(iid, lid)
        s.commit()
        return s, lid

    def test_it_returns_the_linked_incident_window(self, tmp_path):
        from self_improve.pipeline import _learning_evidence

        s, lid = self._store(tmp_path, [
            {"role": "human", "ts": "", "text": "why did you skip the test"},
            {"role": "assistant", "ts": "", "text": "I assumed it passed"},
        ])
        got = _learning_evidence(s, {"id": lid})
        assert "why did you skip the test" in got
        assert "I assumed it passed" in got
        s.close()

    def test_no_surviving_evidence_returns_empty_not_a_crash(self, tmp_path):
        from self_improve.pipeline import _learning_evidence

        s, lid = self._store(tmp_path, [])
        assert _learning_evidence(s, {"id": lid}) == ""
        s.close()

    def test_an_unknown_learning_returns_empty(self, tmp_path):
        from self_improve.pipeline import _learning_evidence

        s, _ = self._store(tmp_path, [{"role": "human", "ts": "", "text": "x"}])
        assert _learning_evidence(s, {"id": "no-such-learning"}) == ""
        s.close()


class TestMajorityVerdictDecisionTable:
    """The complete majority verdict decision table, independent of scenario order."""

    @pytest.mark.parametrize(
        "n_pass,n_fail,n_ungated,n_error,expected",
        [
            # a clean majority, with and without a third supporting scenario
            (2, 0, 1, 0, "gated_pass"),
            (3, 0, 0, 0, "gated_pass"),
            (2, 0, 0, 1, "gated_pass"),
            # two failures condemn
            (0, 2, 1, 0, "gated_fail"),
            (1, 2, 0, 0, "gated_fail"),
            (0, 3, 0, 0, "gated_fail"),
            # MIXED: contrary evidence exists, so it is held, never auto-applied
            (2, 1, 0, 0, "inconclusive"),
            (1, 1, 1, 0, "inconclusive"),
            (1, 0, 2, 0, "inconclusive"),
            # nothing reproduced anywhere, but scenarios really ran
            (0, 0, 3, 0, "ungated"),
            (0, 0, 1, 2, "ungated"),
            # nothing ran at all: that is not evidence of harmlessness
            (0, 0, 0, 3, "inconclusive"),
        ],
    )
    def test_table(self, n_pass, n_fail, n_ungated, n_error, expected):
        from self_improve.evals.regression import majority_verdict

        got = majority_verdict(
            {
                "gated_pass": n_pass,
                "gated_fail": n_fail,
                "ungated": n_ungated,
                "error": n_error,
            }
        )
        assert got == expected

    @pytest.mark.parametrize(
        "n_pass,n_fail,n_ungated,n_error,expected",
        [
            # BL-02: with five scenarios, two passes are not a majority
            (2, 0, 3, 0, "inconclusive"),
            (2, 0, 0, 3, "inconclusive"),
            (3, 0, 2, 0, "gated_pass"),
            (3, 0, 0, 2, "gated_pass"),
            # a single scenario can never pass on its own
            (1, 0, 0, 0, "inconclusive"),
        ],
    )
    def test_a_pass_needs_a_majority_of_every_scenario_that_ran(
        self, n_pass, n_fail, n_ungated, n_error, expected
    ):
        from self_improve.evals.regression import majority_verdict

        got = majority_verdict(
            {
                "gated_pass": n_pass,
                "gated_fail": n_fail,
                "ungated": n_ungated,
                "error": n_error,
            }
        )
        assert got == expected

    def test_a_mixed_result_is_never_auto_appliable(self):
        """Mixed pass/fail evidence must remain inconclusive and require review.

        Returning ungated for this tally must fail the verdict assertion.
        Neither ungated nor inconclusive grants automatic execution permission."""
        from self_improve.apply import APPLIABLE_STATUSES
        from self_improve.evals.regression import majority_verdict

        mixed = majority_verdict(
            {"gated_pass": 2, "gated_fail": 1, "ungated": 0, "error": 0}
        )
        assert mixed == "inconclusive"
        assert mixed not in APPLIABLE_STATUSES

    def test_all_generations_failing_is_not_appliable_either(self):
        from self_improve.apply import APPLIABLE_STATUSES
        from self_improve.evals.regression import majority_verdict

        nothing_ran = majority_verdict(
            {"gated_pass": 0, "gated_fail": 0, "ungated": 0, "error": 3}
        )
        assert nothing_ran not in APPLIABLE_STATUSES

    def test_thresholds_are_module_constants_not_config_keys(self):
        """The evidentiary bar must not be lowerable from a TOML file."""
        from dataclasses import fields

        from self_improve.config import Config
        from self_improve.evals import regression

        assert regression.GATE_PASS_VOTES == 2
        assert regression.GATE_FAIL_VOTES == 2
        names = {f.name for f in fields(Config)}
        for forbidden in ("gate_pass_votes", "gate_fail_votes", "majority_threshold"):
            assert forbidden not in names, (
                f"{forbidden} must not be configurable: the vote threshold is "
                "the evidence bar behind auto-apply"
            )


class TestMajorityGateRunsEveryScenario:
    def _spec(self, sid):
        from self_improve.evals.harness import EvalSpec

        return EvalSpec(
            id=sid, title="t", scenario_prompt="p", workspace_files={"a": "b"},
            success_criteria="c", grader={"type": "code", "check": "true"},
        )

    def _gate(self, verdicts):
        """A fake gate that returns a scripted verdict per scenario index."""
        def run_gate(spec, index):
            return {
                "verdict": verdicts[index],
                "without_stats": {},
                "with_stats": {},
                "eval_result_id": f"e{index}",
            }
        return run_gate

    def test_it_does_not_stop_at_the_first_decisive_scenario(self):
        """THE regression. The old gate returned gated_pass here and stopped.

        Sabotage: `break` as soon as a verdict is not "ungated". Scenario 2
        never runs, the tally reads 1-0, and the verdict flips to gated_pass —
        exactly the coin flip the majority replaced.
        """
        from self_improve.evals.regression import gate_majority

        seen = []

        def run_gate(spec, index):
            seen.append(index)
            return {
                "verdict": ["gated_pass", "gated_fail", "gated_fail"][index],
                "without_stats": {}, "with_stats": {}, "eval_result_id": "e",
            }

        got = gate_majority(lambda i: self._spec(f"s{i}"), run_gate, scenarios=3)
        assert seen == [0, 1, 2]
        assert got["verdict"] == "gated_fail"
        assert got["scenario_tally"]["gated_fail"] == 2

    def test_two_passes_and_no_fails_is_a_pass(self):
        from self_improve.evals.regression import gate_majority

        got = gate_majority(
            lambda i: self._spec(f"s{i}"),
            self._gate(["gated_pass", "ungated", "gated_pass"]),
            scenarios=3,
        )
        assert got["verdict"] == "gated_pass"
        assert got["scenario_tally"] == {
            "gated_pass": 2, "gated_fail": 0, "ungated": 1, "error": 0
        }

    def test_the_linked_evidence_is_a_scenario_that_supports_the_verdict(self):
        """BL-21: the deciding scenario skipped only `ungated`, so an `error`
        scenario, which tested nothing, became the proposal's evidence link."""
        from self_improve.evals.regression import gate_majority

        got = gate_majority(
            lambda i: self._spec(f"s{i}"),
            self._gate(["error", "gated_pass", "gated_pass"]),
            scenarios=3,
        )
        assert got["verdict"] == "gated_pass"
        assert got["eval_result_id"] == "e1", got["eval_result_id"]

    def test_a_failing_verdict_links_a_failing_scenario(self):
        from self_improve.evals.regression import gate_majority

        got = gate_majority(
            lambda i: self._spec(f"s{i}"),
            self._gate(["gated_pass", "gated_fail", "gated_fail"]),
            scenarios=3,
        )
        assert got["verdict"] == "gated_fail" and got["eval_result_id"] == "e1"

    def test_a_single_flake_does_not_condemn_a_rule(self):
        """FAIL_VOTES is 2, so one bad scenario holds rather than condemns."""
        from self_improve.evals.regression import gate_majority

        got = gate_majority(
            lambda i: self._spec(f"s{i}"),
            self._gate(["gated_pass", "gated_pass", "gated_fail"]),
            scenarios=3,
        )
        assert got["verdict"] == "inconclusive"

    def test_the_split_is_reported_so_2_1_does_not_read_like_3_0(self):
        from self_improve.evals.regression import gate_majority

        got = gate_majority(
            lambda i: self._spec(f"s{i}"),
            self._gate(["gated_pass", "gated_pass", "gated_pass"]),
            scenarios=3,
        )
        assert got["scenario_tally"]["gated_pass"] == 3
        assert got["scenarios_run"] == 3
        assert [r["verdict"] for r in got["scenarios"]] == ["gated_pass"] * 3

    @pytest.mark.parametrize("third", ["gated_pass", "gated_fail", "ungated", "error"])
    def test_settled_failure_still_retains_the_final_scenario(self, third):
        """A fixed verdict does not make the remaining evidence expendable."""
        from self_improve.evals.regression import gate_majority

        generated = []

        def generate(index):
            generated.append(index)
            if index == 2 and third == "error":
                raise ValueError("invented final scenario error")
            return self._spec(f"s{index}")

        got = gate_majority(generate, self._gate(["gated_fail", "gated_fail", third]), scenarios=3)
        assert got["verdict"] == "gated_fail"
        assert generated == [0, 1, 2]
        assert got["scenarios_run"] == 3
        assert [r["verdict"] for r in got["scenarios"]] == ["gated_fail", "gated_fail", third]
        assert sum(got["scenario_tally"].values()) == 3
        if third == "error":
            assert got["scenarios"][2]["error"] == "invented final scenario error"

    def test_a_generation_failure_does_not_lose_the_other_scenarios(self):
        from self_improve.evals.regression import gate_majority

        def gen(index):
            if index == 0:
                raise ValueError("bad spec")
            return self._spec(f"s{index}")

        got = gate_majority(
            gen, self._gate([None, "gated_pass", "gated_pass"]), scenarios=3
        )
        assert got["verdict"] == "gated_pass"
        assert got["scenario_tally"]["error"] == 1
        assert got["scenarios_run"] == 3

    def test_every_generation_failing_raises_rather_than_inventing_a_verdict(self):
        from self_improve.evals.regression import gate_majority

        def gen(index):
            raise ValueError(f"bad spec {index}")

        with pytest.raises(ValueError, match="bad spec 2"):
            gate_majority(gen, self._gate([]), scenarios=3)

    def test_an_unknown_scenario_verdict_raises_rather_than_being_ignored(self):
        from self_improve.evals.regression import gate_majority

        with pytest.raises(ValueError, match="unknown verdict"):
            gate_majority(
                lambda i: self._spec("s"),
                lambda spec, i: {"verdict": "probably_fine"},
                scenarios=3,
            )

    def test_the_retry_gate_is_gone(self):
        """Reverting the call site must fail loudly, not silently restore it."""
        from self_improve.evals import regression

        assert not hasattr(regression, "gate_with_retries")


class TestEachScenarioGetsItsOwnDirectoryAndSpecId:
    """Give every scenario its own directory and spec ID.
    Shared directories collide, and shared IDs overwrite evidence needed for a later sweep.
    """

    def _spec(self, sid):
        from self_improve.evals.harness import EvalSpec

        return EvalSpec(
            id=sid, title="t", scenario_prompt="p", workspace_files={"a": "b"},
            success_criteria="c", grader={"type": "code", "check": "true"},
        )

    def test_run_gate_receives_the_scenario_index(self):
        from self_improve.evals.regression import gate_majority

        seen = []

        def run_gate(spec, index):
            seen.append(index)
            return {"verdict": "ungated", "without_stats": {}, "with_stats": None}

        gate_majority(lambda i: self._spec(f"s{i}"), run_gate, scenarios=3)
        assert seen == [0, 1, 2]

    def test_scenarios_do_not_collide_on_disk(self, tmp_path):
        from self_improve.evals.regression import gate_majority

        made = []

        def run_gate(spec, index):
            d = tmp_path / "trials" / f"scenario-{index}" / "sandbox"
            d.mkdir(parents=True, exist_ok=False)  # raises on collision
            made.append(d)
            return {"verdict": "ungated", "without_stats": {}, "with_stats": None}

        gate_majority(lambda i: self._spec("same-id"), run_gate, scenarios=3)
        assert len(made) == 3 and len(set(made)) == 3

    def test_each_scenario_writes_its_own_spec_file(self, tmp_path):
        """Sabotage: drop `scenario=` from generate_spec's call in pipeline.py,
        or ignore it here. All three scenarios then write one file."""
        from self_improve.evals.regression import generate_spec

        learning = {
            "id": "L1", "rule_text": "r", "why": "w", "incident_summary": "i",
        }
        response = {
            "title": "t", "scenario_prompt": "p", "workspace_files": {"a": "b"},
            "success_criteria": "c", "grader": {"type": "code", "check": "true"},
        }
        prompts = tmp_path / "prompts"
        prompts.mkdir()
        (prompts / "gen_regression_eval.md").write_text(
            "{{rule}} {{why}} {{incident_summary}}"
        )
        out = tmp_path / "specs"
        ids = []
        for i in range(3):
            spec = generate_spec(
                learning, lambda _p: dict(response), prompts,
                out_dir=out, scenario=i,
            )
            ids.append(spec.id)
        assert ids == ["L1-s0", "L1-s1", "L1-s2"]
        assert sorted(p.name for p in out.glob("*.yaml")) == [
            "L1-s0.yaml", "L1-s1.yaml", "L1-s2.yaml"
        ]


class TestGateBudgetPreflightPricesTheMajority:
    """The preflight must price the WORST case, or trials die mid-arm.

    Under-counting fails SILENTLY: the preflight passes, an arm runs out of
    budget halfway, harness.run_trials records the BudgetExhausted as
    `agent_error`, and the verdict degrades into something that reads like a
    judgement about the rule.
    """

    def test_the_formula_prices_every_scenarios_two_arms(self):
        from self_improve.config import Config
        from self_improve.pipeline import gate_calls_needed

        cfg = Config(eval_trials=3, eval_scenarios=3)
        # 3 scenarios x (1 eval_gen + 3 without-arm + 3 with-arm) = 21
        assert gate_calls_needed(cfg) == 21

    def test_one_scenario_is_one_generation_and_two_arms(self):
        from self_improve.config import Config
        from self_improve.pipeline import gate_calls_needed

        assert gate_calls_needed(Config(eval_trials=3, eval_scenarios=1)) == 7

    def test_the_pool_covers_three_proposals_and_the_ab_sweep(self):
        """78 is not a round number; it is 3 x 21 + the A/B sweep's 15.

        ab.rerun_applied bills to this same pool through the shared `grade`
        stage and has NO preflight of its own, so a pool sized only for the
        gate makes the sweep report `inconclusive` about rules it never tested.
        """
        from self_improve.config import Config
        from self_improve.pipeline import gate_calls_needed

        cfg = Config()
        per_proposal = gate_calls_needed(cfg)
        ab_sweep = cfg.ab_prune_max_rules_per_run * cfg.eval_trials
        assert cfg.max_gate_calls_per_run == 3 * per_proposal + ab_sweep == 78


class TestAskingTheOperatorIsNotAGradedFailure:
    """A headless trial has no human to answer a request for clarification.
    If a failed trial ends by asking the operator, record asked_operator.
    An arm dominated by that outcome is inconclusive rather than evidence that
    the rule caused harm.
    """

    def test_a_failure_that_ends_in_a_question_is_classified_separately(self):
        from self_improve.evals.harness import classify_failure

        out = "Option 1 truncates, option 2 marks them null. Which do you want?"
        assert classify_failure(out) == "asked_operator"

    def test_an_ordinary_wrong_answer_stays_graded_fail(self):
        from self_improve.evals.harness import classify_failure

        assert classify_failure("Done. I wrote report.json with all 12 scores.") == "graded_fail"

    def test_a_question_early_on_does_not_count(self):
        """Only the END of the output decides; agents ask rhetorical questions
        mid-reasoning and then get on with the work."""
        from self_improve.evals.harness import classify_failure

        out = "Should I truncate? I'll truncate and document it. " + ("x" * 400) + " Done."
        assert classify_failure(out) == "graded_fail"

    def test_empty_output_is_not_treated_as_asking(self):
        from self_improve.evals.harness import classify_failure

        assert classify_failure("") == "graded_fail"

    def test_the_gate_calls_an_ask_dominated_arm_inconclusive(self):
        from self_improve.evals.regression import with_arm_verdict

        # 3 trials, all failed by asking: the rule was never actually tested.
        assert with_arm_verdict(
            {"succeeded": 0, "attempted": 3, "errors": {"asked_operator": 3}},
            min_passes=2,
        ) == "error"

    def test_an_untested_with_arm_does_not_vote_like_an_unreproduced_mistake(self):
        """BL-22: a with-rule arm where every trial broke returned "ungated",
        the word for "the mistake did not reproduce", although the without
        arm had reproduced it. Infrastructure failure in either arm is a
        non-voting "error"."""
        from self_improve.evals.regression import majority_verdict, with_arm_verdict

        crashed = with_arm_verdict(
            {"succeeded": 0, "attempted": 3, "errors": {"agent_error": 3}}, min_passes=2)
        assert crashed == "error"
        # Three such scenarios hold the rule as inconclusive. They must not
        # read as "ungated", which says the mistake never reproduced.
        tally = {k: (3 if k == crashed else 0) for k in ("gated_pass", "gated_fail", "ungated", "error")}
        assert majority_verdict(tally) == "inconclusive"

    def test_a_genuinely_failing_rule_still_gates_fail(self):
        from self_improve.evals.regression import with_arm_verdict

        assert with_arm_verdict(
            {"succeeded": 0, "attempted": 3, "errors": {"graded_fail": 3}},
            min_passes=2,
        ) == "gated_fail"

    def test_a_passing_rule_still_gates_pass(self):
        from self_improve.evals.regression import with_arm_verdict

        assert with_arm_verdict(
            {"succeeded": 3, "attempted": 3, "errors": {}}, min_passes=2
        ) == "gated_pass"


class TestRunTrialsUsesTheFailureClassifier:
    """Drive the classifier through run_trials so bypassing its call site fails."""

    def _spec(self):
        from self_improve.evals.harness import EvalSpec

        return EvalSpec(
            id="s", title="t", scenario_prompt="p", workspace_files={"a.txt": "x"},
            success_criteria="c",
            grader={"type": "code", "check": "exit 1"},  # always fails
        )

    def test_an_asking_agent_is_recorded_as_asked_operator(self, tmp_path):
        from self_improve.evals.harness import run_trials

        stats = run_trials(
            self._spec(), None,
            lambda prompt, sandbox: "I could truncate or mark null. Which do you want?",
            1, work_dir=tmp_path / "w",
        )
        assert stats.errors.get("asked_operator") == 1, stats.errors

    def test_an_ordinary_failure_is_still_graded_fail(self, tmp_path):
        from self_improve.evals.harness import run_trials

        stats = run_trials(
            self._spec(), None,
            lambda prompt, sandbox: "Done. Wrote the report.",
            1, work_dir=tmp_path / "w",
        )
        assert stats.errors.get("graded_fail") == 1, stats.errors


class TestInfraErrorsInTheWithArmAreNotEvidence:
    """Infrastructure failures in either arm cannot count as evidence about a rule."""

    def test_an_arm_dominated_by_agent_error_is_inconclusive(self):
        from self_improve.evals.regression import with_arm_verdict

        assert with_arm_verdict(
            {"succeeded": 1, "attempted": 3, "errors": {"agent_error": 2}},
            min_passes=2,
        ) == "error"

    def test_infra_and_asking_failures_combine(self):
        """Both are 'the rule was never tested', so they count together."""
        from self_improve.evals.regression import with_arm_verdict

        assert with_arm_verdict(
            {"succeeded": 0, "attempted": 3, "errors": {"agent_error": 1, "asked_operator": 1,
                                                        "graded_fail": 1}},
            min_passes=2,
        ) == "error"

    def test_a_real_failure_majority_still_gates_fail(self):
        from self_improve.evals.regression import with_arm_verdict

        assert with_arm_verdict(
            {"succeeded": 0, "attempted": 3, "errors": {"agent_error": 1, "graded_fail": 2}},
            min_passes=2,
        ) == "gated_fail"

    def test_grader_error_counts_as_infra_too(self):
        from self_improve.evals.regression import with_arm_verdict

        assert with_arm_verdict(
            {"succeeded": 0, "attempted": 3, "errors": {"grader_error": 3}},
            min_passes=2,
        ) == "error"


class TestABudgetFailureIsNeverAVerdict:
    """Propagate budget exhaustion instead of grading an unexecuted trial.

    The trial runner must re-raise BudgetExhausted while retaining ordinary
    runner crashes as agent_error outcomes. The sweep's preflight is tested
    separately through the pipeline."""

    def _spec(self):
        from self_improve.evals.harness import EvalSpec

        return EvalSpec(
            id="s", title="t", scenario_prompt="p", workspace_files={"a": "b"},
            success_criteria="c", grader={"type": "code", "check": "true"},
        )

    def test_run_trials_propagates_budget_exhaustion_instead_of_burying_it(self, tmp_path):
        """Sabotage: remove the `except BudgetExhausted: raise` clause.

        The exception is then caught by the generic handler below it and the
        trial is recorded as agent_error.
        """
        from self_improve.evals.harness import run_trials
        from self_improve.llm import BudgetExhausted
        from self_improve.config import Config

        def broke(prompt, sandbox):
            raise BudgetExhausted("gate", 30, 5)

        with pytest.raises(BudgetExhausted):
            run_trials(
                self._spec(), None, broke, 3, None,
                work_dir=tmp_path / "arm",
            )

    def test_an_ordinary_agent_failure_is_still_recorded_not_raised(self, tmp_path):
        """The guard must be narrow. A crashed agent is a trial outcome."""
        from self_improve.evals.harness import run_trials
        from self_improve.config import Config

        def broke(prompt, sandbox):
            raise RuntimeError("the agent died")

        stats = run_trials(
            self._spec(), None, broke, 2, None, work_dir=tmp_path / "arm2",
        )
        assert stats.errors.get("agent_error") == 2
        assert stats.attempted == 2


class TestTheABSweepPricesItselfToo:
    """The gate pool is sized 3 x 21 + 15, and the 15 is this sweep."""

    def test_the_formula_matches_the_pool_the_config_reserves(self):
        from self_improve.config import Config
        from self_improve.pipeline import ab_calls_needed, gate_calls_needed

        cfg = Config()
        assert ab_calls_needed(cfg) == cfg.ab_prune_max_rules_per_run * cfg.eval_trials == 15
        assert cfg.max_gate_calls_per_run == 3 * gate_calls_needed(cfg) + ab_calls_needed(cfg)

    def test_the_pipeline_refuses_the_sweep_rather_than_starving_it(self, tmp_path):
        """Sabotage: delete the `need_ab > left_ab` check in pipeline.py.

        The sweep then starts with too little budget, dies mid-arm, and reports
        `inconclusive` about rules it never tested.
        """
        import inspect

        from self_improve import pipeline as pl

        src = inspect.getsource(pl.run_pipeline)
        assert "ab_calls_needed(cfg)" in src, (
            "the A/B sweep runs with no budget preflight, so a drained gate "
            "pool turns into a verdict about rules that were never tested"
        )


class TestTheReportExplainsOnlyTheDocumentedKey:
    """Pin the coupling between the taxonomy key and the note that explains it.

    The behavioural half of this lives in tests/test_e2e_pipeline.py, driven
    through run_pipeline. A source-substring check was tried here first and was
    WORTHLESS: `BudgetExhaustedSignal` contains `BudgetExhausted`, so
    `assert "BudgetExhausted" in gate_block` passed against the bug. Recording
    that, because it is the same "watches the wrong thing" mistake this file is
    full of fixes for.
    """

    def test_only_the_snake_case_key_gets_the_explanation(self):
        from self_improve.report import _gate_starved_lines

        text = "\n".join(
            _gate_starved_lines({"apply": {"taxonomy": {"gate_budget_exhausted": 3}}})
        )
        assert "3 proposal(s) were held" in text
        assert _gate_starved_lines(
            {"apply": {"taxonomy": {"gate_BudgetExhausted": 3}}}
        ) == [], (
            "a raw exception-class name in the taxonomy gets no explanation, so "
            "the pipeline must translate it into the documented key"
        )


class TestCompleteScenarioEvidence:
    """Every ordered outcome sequence retains its full evidence and verdict."""

    def test_all_sequences_through_five_scenarios_keep_every_outcome(self):
        import itertools
        from self_improve.evals.regression import SCENARIO_OUTCOMES, gate_majority, majority_verdict
        from types import SimpleNamespace

        for scenarios in range(1, 6):
            for outcomes in itertools.product(SCENARIO_OUTCOMES, repeat=scenarios):
                visited = []

                def run_gate(spec, index):
                    visited.append(index)
                    return {"verdict": outcomes[index], "eval_result_id": f"e{index}"}

                result = gate_majority(lambda i: SimpleNamespace(id=f"s{i}"), run_gate,
                                       scenarios=scenarios)
                tally = {k: outcomes.count(k) for k in SCENARIO_OUTCOMES}
                assert visited == list(range(scenarios)), outcomes
                assert result["scenario_tally"] == tally, outcomes
                assert result["scenarios_run"] == scenarios
                assert [r["verdict"] for r in result["scenarios"]] == list(outcomes)
                assert [r["eval_result_id"] for r in result["scenarios"]] == [f"e{i}" for i in range(scenarios)]
                assert result["verdict"] == majority_verdict(tally)


class TestTheCheckedInSeedCorpusStillParses:
    """Keep every committed synthetic regression spec loadable.

    These fixtures exercise the gate without private history. Frozen private
    labels and retrieval benchmarks have separate versioned manifests."""

    def _specs(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[1] / "evals" / "regression"
        return sorted(root.glob("*.yaml"))

    def test_every_checked_in_spec_loads(self):
        import yaml

        from self_improve.evals.harness import spec_from_dict

        failures = []
        specs = self._specs()
        assert specs, "the committed synthetic regression corpus must not be empty"
        for path in specs:
            try:
                spec_from_dict(
                    yaml.safe_load(path.read_text(encoding="utf-8")), origin=str(path)
                )
            except Exception as exc:  # noqa: BLE001 - report them all, not the first
                failures.append(f"{path.name}: {type(exc).__name__}: {exc}")
        assert not failures, "specs that no longer load:\n  " + "\n  ".join(failures)

    def test_required_synthetic_seed_regressions_are_present(self):
        """Retain the five named synthetic regression scenarios.

        Check their presence separately from parsing so deleting a valid fixture
        cannot silently reduce the regression coverage."""
        names = {p.name for p in self._specs()}
        for rule in (
            "agent-cli-env-gotchas",
            "gemini-model-identity",
            "pricing-default-truncation-bias",
            "silent-implementation-choices",
            "wrong-artifact-verification",
        ):
            assert f"seed-2026-08-{rule}.yaml" in names, f"missing seed spec: {rule}"

    def test_every_spec_names_a_grader_the_harness_can_run(self):
        import yaml

        from self_improve.evals.harness import spec_from_dict

        for path in self._specs():
            spec = spec_from_dict(
                yaml.safe_load(path.read_text(encoding="utf-8")), origin=str(path)
            )
            assert spec.grader.get("type") in ("code", "model"), (
                f"{path.name} has grader type {spec.grader.get('type')!r}"
            )


class TestTheWholeGatePathRunsEndToEnd:
    """Compose gate_majority, gate, run_trials, and verdict persistence.

    A scripted agent exercises the actual gate composition without model calls."""

    def _spec_for(self, scenario):
        from self_improve.evals.harness import EvalSpec

        return EvalSpec(
            id=f"L1-s{scenario}",
            title=f"scenario {scenario}",
            scenario_prompt="write the answer into answer.txt",
            workspace_files={"seed.txt": "seed"},
            success_criteria="answer.txt says OK",
            # A code grader that passes only when the trial wrote the file.
            grader={"type": "code", "check": "test -f answer.txt"},
        )

    def _runner(self, script):
        """An agent that writes answer.txt on the turns `script` says to."""
        calls = {"n": 0}

        def run(prompt, sandbox):
            i = calls["n"]
            calls["n"] += 1
            if script[i % len(script)]:
                (sandbox / "answer.txt").write_text("OK", encoding="utf-8")
            return f"turn {i}"

        run.calls = calls
        return run

    def test_a_majority_of_passing_scenarios_persists_gated_pass(self, tmp_path):
        """Sabotage: revert pipeline's call to a single-scenario gate. The
        tally then reads 1-0 and `scenarios_run` is 1, not 3."""
        from self_improve.config import Config
        from self_improve.evals import regression
        from self_improve.store import Store

        store = Store(tmp_path / "s.db")
        cfg = Config(state_dir=str(tmp_path / "state"), eval_trials=2, eval_scenarios=3)
        work = tmp_path / "trials"

        # without-rule arm fails (writes nothing), with-rule arm passes.
        arms = {"n": 0}

        def runner(prompt, sandbox):
            # run_trials calls the without arm first, then the with arm.
            arms["n"] += 1
            if arms["n"] > cfg.eval_trials:
                (sandbox / "answer.txt").write_text("OK", encoding="utf-8")
            return "done"

        def run_gate(spec, scenario):
            arms["n"] = 0  # each scenario gets a fresh pair of arms
            return regression.gate(
                spec, "**the rule**", runner, cfg,
                store=store, work_dir=work / f"scenario-{scenario}",
            )

        got = regression.gate_majority(
            self._spec_for, run_gate, scenarios=cfg.eval_scenarios
        )
        store.commit()

        assert got["verdict"] == "gated_pass", got["scenario_tally"]
        assert got["scenario_tally"]["gated_pass"] >= 2
        # every scenario got its OWN directory; run_trials builds with
        # exist_ok=False, so a collision would have raised
        dirs = sorted(p.name for p in work.iterdir())
        assert dirs == [f"scenario-{i}" for i in range(cfg.eval_scenarios)], dirs
        # and the verdict reached the database, not just the return value
        rows = store.query("SELECT verdict, kind FROM eval_results")
        assert len(rows) == cfg.eval_scenarios, "each scenario must persist its own result"
        assert all(r["kind"] == "regression" for r in rows)
        store.close()

    def test_a_scenario_that_never_reproduces_leaves_the_rule_ungated(self, tmp_path):
        """A passing without-rule arm cannot establish that the rule helped.

        Return ungated and skip the with-rule arm. This verdict does not grant
        automatic execution permission."""
        from self_improve.config import Config
        from self_improve.evals import regression
        from self_improve.store import Store

        store = Store(tmp_path / "s.db")
        cfg = Config(state_dir=str(tmp_path / "state"), eval_trials=2, eval_scenarios=3)

        def always_passes(prompt, sandbox):
            (sandbox / "answer.txt").write_text("OK", encoding="utf-8")
            return "done"

        def run_gate(spec, scenario):
            return regression.gate(
                spec, "**the rule**", always_passes, cfg,
                store=store, work_dir=tmp_path / "t" / f"scenario-{scenario}",
            )

        got = regression.gate_majority(self._spec_for, run_gate, scenarios=3)
        assert got["verdict"] == "ungated"
        assert got["scenario_tally"]["ungated"] == 3
        assert got["with_stats"] is None, "the with-arm must be skipped when ungated"
        store.close()


class TestTheSweepStillFindsCheckedInSpecs:
    """The sweep must find public seed specs as well as private generated specs.
    Changing the generated-spec directory must not orphan retained seed regressions.
    """

    def _spec_yaml(self, path, spec_id):
        import yaml

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump({
            "id": spec_id, "title": "t", "scenario_prompt": "p",
            "workspace_files": {"a": "b"}, "success_criteria": "c",
            "grader": {"type": "code", "check": "true"},
        }), encoding="utf-8")

    def test_a_spec_only_in_the_repo_is_still_found(self, tmp_path):
        """Sabotage: drop the fallback. The lookup misses and the sweep
        records skipped_no_spec for a rule it was supposed to re-test."""
        from self_improve.evals.ab import find_spec

        state = tmp_path / "state" / "evals" / "regression"
        seeds = tmp_path / "repo" / "evals" / "regression"
        state.mkdir(parents=True)
        self._spec_yaml(seeds / "L1.yaml", "L1")

        found = find_spec("L1", state, fallback_dir=seeds)
        assert found is not None and found.name == "L1.yaml"
        assert found.parent == seeds

    def test_the_state_dir_wins_when_both_have_one(self, tmp_path):
        """A freshly generated spec is newer evidence than a committed one."""
        from self_improve.evals.ab import find_spec

        state = tmp_path / "state"
        seeds = tmp_path / "seeds"
        self._spec_yaml(state / "L1.yaml", "L1")
        self._spec_yaml(seeds / "L1.yaml", "L1")

        assert find_spec("L1", state, fallback_dir=seeds).parent == state

    def test_missing_everywhere_is_still_missing(self, tmp_path):
        from self_improve.evals.ab import find_spec

        state = tmp_path / "state"
        state.mkdir()
        assert find_spec("nope", state, fallback_dir=tmp_path / "seeds") is None

    def test_the_pipeline_passes_the_seed_directory_as_the_fallback(self):
        """The wiring, not the function: SEED_REGRESSION_DIR had ZERO readers
        after the move, which is what made this orphaning invisible."""
        import inspect

        from self_improve import pipeline as pl

        src = inspect.getsource(pl.run_pipeline)
        assert "SEED_REGRESSION_DIR" in src, (
            "the seed directory is not passed to the A/B sweep, so committed "
            "specs are unreachable"
        )


def test_ab_prune_says_why_it_attempted_nothing(tmp_path):
    """Explain that pruning has no work when no applied rules exist.

    Zero attempts must carry the no_applied_rules reason so an empty candidate
    set can be distinguished from a failure to execute."""
    from self_improve.config import Config
    from self_improve.evals import ab
    from self_improve.store import Store

    store = Store(tmp_path / "s.db")
    out = ab.rerun_applied(
        store,
        Config(state_dir=str(tmp_path)),
        agent_runner=lambda *a, **k: "",
        regression_dir=tmp_path / "specs",
    )
    store.close()
    assert out["attempted"] == 0
    assert out["candidates_considered"] == 0, out
    assert "no_applied_rules" in out["reason"], out


def test_ab_prune_reason_changes_when_there_is_something_to_do(tmp_path):
    """The complement, so the reason is not a constant string."""
    from self_improve.config import Config
    from self_improve.evals import ab
    from self_improve.store import Store, new_id, utc_now_iso

    store = Store(tmp_path / "s.db")
    lid = new_id()
    store.insert("learnings", {
        "id": lid, "title": "t", "rule_text": "r", "why": "w", "category": "c",
        "scope": "global", "evidence_count": 1, "project_count": 1,
        "projects_json": "[]", "first_seen": utc_now_iso(), "last_seen": utc_now_iso(),
        "confidence": 0.5, "status": "applied", "duplicate_of": "",
        "violated_existing_rule": "", "created_at": utc_now_iso(),
        "primary_project_path": "",
    })
    # An applied learning with no applied PROPOSAL is a broken invariant the
    # code already refuses loudly, so give it one.
    store.insert("proposals", {
        "id": new_id(), "learning_id": lid, "run_id": "", "action": "add",
        "target_path": "/tmp/x/CLAUDE.md", "target_kind": "global_claude_md",
        "diff_unified": "", "status": "applied", "eval_result_id": "",
        "applied_at": utc_now_iso(), "snapshot_commit_before": "",
        "snapshot_commit_after": "", "created_at": utc_now_iso(),
    })
    store.commit()
    out = ab.rerun_applied(
        store,
        Config(state_dir=str(tmp_path)),
        agent_runner=lambda *a, **k: "",
        regression_dir=tmp_path / "specs",
    )
    store.close()
    assert out["applied_rules"] == 1, out
    assert out["candidates_considered"] == 1, out
    assert "no_applied_rules" not in out["reason"], out
    assert "no_spec" in out["reason"], out
