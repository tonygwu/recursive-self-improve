"""The eval agent's OS-level boundary.

These tests are hermetic: they check the policy we hand to the sandbox runtime
and the way we react to what it reports. The one test that actually launches
`srt` is opt-in via SELFIMPROVE_SANDBOX_E2E=1, because it needs npx and the
network, and a nightly run must not depend on npm being reachable.
"""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
from pathlib import Path

import pytest

from self_improve.config import Config
from self_improve.sandbox import (
    SandboxError,
    SandboxPolicy,
    instruction_paths_to_protect,
    policy_for_trial,
    verify_boundary,
    wrap_argv,
    write_settings,
)


def cfg_for(tmp_path: Path) -> Config:
    return Config(
        global_claude_md=str(tmp_path / "dot-claude" / "CLAUDE.md"),
        codex_global_agents_md=str(tmp_path / "dot-codex" / "AGENTS.md"),
        skills_dir=str(tmp_path / "dot-claude" / "skills"),
        state_dir=str(tmp_path / "state"),
    )


class TestPolicyShape:
    def test_settings_carry_every_key_the_schema_requires(self, tmp_path):
        """srt refuses to start without all four, and the README example omits two.

        The published example has no `network.deniedDomains` and no
        `filesystem.denyRead`. Copying it verbatim makes the runtime refuse to
        launch, so the dict is built here instead.
        """
        s = policy_for_trial(tmp_path, cfg_for(tmp_path)).to_settings()
        assert set(s["filesystem"]) == {"allowWrite", "denyWrite", "denyRead"}
        assert set(s["network"]) == {"allowedDomains", "deniedDomains"}

    def test_trial_dir_is_writable_and_absolute(self, tmp_path):
        s = policy_for_trial(tmp_path / "sandbox", cfg_for(tmp_path)).to_settings()
        allowed = s["filesystem"]["allowWrite"]
        assert any(Path(p).is_absolute() for p in allowed)
        assert any("sandbox" in p for p in allowed)

    def test_instruction_files_are_denied_even_inside_an_allowed_config_dir(self, tmp_path):
        """denyWrite beats allowWrite, which is the reverse of the read rules."""
        c = cfg_for(tmp_path)
        pol = policy_for_trial(tmp_path / "sandbox", c, config_dir=str(tmp_path / "dot-claude"))
        s = pol.to_settings()
        assert str(tmp_path / "dot-claude") in s["filesystem"]["allowWrite"]
        assert str(tmp_path / "dot-claude" / "CLAUDE.md") in s["filesystem"]["denyWrite"]

    def test_the_state_db_is_protected(self, tmp_path):
        """The agent must not be able to rewrite the evidence it is judged on.

        The DB FILE, not the directory: trials write under state_dir/runs/.
        """
        c = cfg_for(tmp_path)
        assert str(tmp_path / "state" / "state.db") in instruction_paths_to_protect(c)

    def test_writes_are_deny_by_default(self, tmp_path):
        """Nothing outside allowWrite is listed, because allowWrite is an allowlist."""
        s = policy_for_trial(tmp_path / "sandbox", cfg_for(tmp_path)).to_settings()
        assert str(Path.home()) not in s["filesystem"]["allowWrite"]

    def test_settings_file_is_valid_json_on_disk(self, tmp_path):
        p = write_settings(policy_for_trial(tmp_path, cfg_for(tmp_path)), tmp_path / "s.json")
        assert json.loads(p.read_text())["filesystem"]["denyRead"] == []


class TestArgvWrapping:
    def test_settings_path_is_always_explicit(self, tmp_path):
        """Never rely on ~/.srt-settings.json.

        Without an explicit --settings, srt starts even when no policy loaded,
        using built-in defaults. A clean start would then not be evidence that
        our policy is in force.
        """
        argv = wrap_argv(["claude", "-p"], tmp_path / "s.json", cfg_for(tmp_path))
        assert "--settings" in argv
        assert argv[argv.index("--settings") + 1] == str(tmp_path / "s.json")

    def test_version_is_pinned(self, tmp_path):
        argv = wrap_argv(["claude"], tmp_path / "s.json", cfg_for(tmp_path))
        assert any("@anthropic-ai/sandbox-runtime@" in a for a in argv)

    def test_the_wrapped_command_survives_intact(self, tmp_path):
        inner = ["claude", "-p", "--model", "x", "--output-format", "json"]
        argv = wrap_argv(inner, tmp_path / "s.json", cfg_for(tmp_path))
        assert argv[-len(inner) :] == inner

    def test_srt_options_are_terminated_before_the_wrapped_command(self, tmp_path):
        """Without a `--`, srt eats flags that belong to the wrapped command.

        Measured 2026-09-04 against the pinned runtime (0.0.73), running the
        real wrapper:

            srt --settings S /bin/echo --version   -> "1.0.0"  (srt's own)
            srt --settings S /opt/.../codex --version -> "1.0.0"
            srt --settings S /opt/.../node  --version -> "1.0.0"
            srt --settings S -- /opt/.../codex --version -> "codex-cli 0.147.0"

        Three different programs reporting one version is the tell. srt never
        ran any of them; its own parser claimed the flag and answered, with
        exit code 0. `--settings X` was worse: exit 1 and no output at all.

        srt's own options are -V/--version, -d/--debug, -s/--settings, -c,
        --control-fd and -h/--help. None collides with what llm.py passes
        today (--model, --output-format, --max-turns, --json,
        --skip-git-repo-check, --sandbox, -p), so this fixes no live bug. It
        removes a trap: any future agent flag from that set would silently not
        reach the agent, and an intercepted call exits 0, which is the shape
        this repo has been bitten by repeatedly.

        `test_the_wrapped_command_survives_intact` cannot catch this. It
        inspects the argv list, where the flag does survive. Whether it
        survives to the PROGRAM is a different question.
        """
        inner = ["codex", "--version"]
        argv = wrap_argv(inner, tmp_path / "s.json", cfg_for(tmp_path))
        assert "--" in argv, argv
        assert argv[argv.index("--") + 1 :] == inner
        # After srt's own options, or it would terminate them too early.
        assert argv.index("--") > argv.index("--settings")

    def test_it_wraps_codex_too(self, tmp_path):
        """srt wraps arbitrary processes; verified against codex exec 2026-08-18."""
        inner = ["codex", "exec", "--json"]
        assert wrap_argv(inner, tmp_path / "s.json", cfg_for(tmp_path))[-3:] == inner


class TestBoundaryVerification:
    """verify_boundary must fail loud in BOTH directions."""

    def _fake_run(self, monkeypatch, *, inside: bool, outside: bool, rc: int = 0):
        def fake(argv, **kw):
            script = argv[-1]
            for token in script.split():
                if token.endswith("inside.txt") and inside:
                    Path(token).write_text("in")
                if token.endswith("OUTSIDE-must-not-exist.txt") and outside:
                    Path(token).write_text("out")
            return subprocess.CompletedProcess(argv, rc, "", "")

        monkeypatch.setattr(subprocess, "run", fake)

    def test_passes_when_inside_allowed_and_outside_refused(self, tmp_path, monkeypatch):
        self._fake_run(monkeypatch, inside=True, outside=False)
        got = verify_boundary(cfg_for(tmp_path), probe_root=tmp_path / "probe")
        assert got["inside_written"] is True
        assert got["outside_written"] is False

    def test_raises_when_the_escape_succeeds(self, tmp_path, monkeypatch):
        """The case the whole module exists to catch."""
        self._fake_run(monkeypatch, inside=True, outside=True)
        with pytest.raises(SandboxError, match="DID NOT CONTAIN"):
            verify_boundary(cfg_for(tmp_path), probe_root=tmp_path / "probe")

    def test_raises_when_an_allowed_write_is_refused(self, tmp_path, monkeypatch):
        """Treat refusal of an allowed write as a sandbox verification failure, before rule evaluation."""
        self._fake_run(monkeypatch, inside=False, outside=False, rc=1)
        with pytest.raises(SandboxError, match="refused an ALLOWED write"):
            verify_boundary(cfg_for(tmp_path), probe_root=tmp_path / "probe")

    def test_a_missing_runtime_is_a_loud_failure(self, tmp_path, monkeypatch):
        def boom(argv, **kw):
            raise FileNotFoundError("npx")

        monkeypatch.setattr(subprocess, "run", boom)
        with pytest.raises(SandboxError, match="not runnable"):
            verify_boundary(cfg_for(tmp_path), probe_root=tmp_path / "probe")

    def test_a_hung_runtime_is_a_loud_failure(self, tmp_path, monkeypatch):
        def boom(argv, **kw):
            raise subprocess.TimeoutExpired(argv, 1)

        monkeypatch.setattr(subprocess, "run", boom)
        with pytest.raises(SandboxError, match="timed out"):
            verify_boundary(cfg_for(tmp_path), probe_root=tmp_path / "probe")


@pytest.mark.skipif(
    os.environ.get("SELFIMPROVE_SANDBOX_E2E") != "1",
    reason="needs npx + network; set SELFIMPROVE_SANDBOX_E2E=1 to run",
)
def test_real_sandbox_contains_a_real_escape(tmp_path):
    """The only test that launches srt for real."""
    got = verify_boundary(Config(), probe_root=tmp_path / "probe")
    assert got["inside_written"] is True
    assert got["outside_written"] is False


class TestPipelineRefusesAnUnverifiedSandbox:
    """A sandbox failure must never be recorded as a verdict about a rule."""

    def test_a_failed_probe_raises_rather_than_running_the_trial(self, tmp_path, monkeypatch):
        from self_improve import pipeline

        def boom(cfg, **kw):
            from self_improve.sandbox import SandboxError

            raise SandboxError("probe escaped")

        monkeypatch.setattr("self_improve.sandbox.verify_boundary", boom)
        state = {"ok": None, "error": "", "observed": {}}
        with pytest.raises(pipeline.SandboxUnverified, match="probe escaped"):
            pipeline._ensure_sandbox(cfg_for(tmp_path), tmp_path, state)

    def test_the_probe_runs_once_per_run_not_once_per_proposal(self, tmp_path, monkeypatch):
        from self_improve import pipeline

        calls = []

        def spy(cfg, **kw):
            calls.append(1)
            return {"inside_written": True, "outside_written": False}

        monkeypatch.setattr("self_improve.sandbox.verify_boundary", spy)
        c, state = cfg_for(tmp_path), {"ok": None, "error": "", "observed": {}}
        for _ in range(5):
            pipeline._ensure_sandbox(c, tmp_path, state)
        assert len(calls) == 1

    def test_a_failure_keeps_raising_for_every_later_proposal(self, tmp_path, monkeypatch):
        """One bad probe must not let the next proposal through unsandboxed."""
        from self_improve import pipeline

        def boom(cfg, **kw):
            from self_improve.sandbox import SandboxError

            raise SandboxError("escaped")

        monkeypatch.setattr("self_improve.sandbox.verify_boundary", boom)
        c, state = cfg_for(tmp_path), {"ok": None, "error": "", "observed": {}}
        for _ in range(3):
            with pytest.raises(pipeline.SandboxUnverified):
                pipeline._ensure_sandbox(c, tmp_path, state)

    def test_disabling_the_sandbox_skips_the_probe_entirely(self, tmp_path, monkeypatch):
        from self_improve import pipeline

        def boom(cfg, **kw):
            raise AssertionError("probe must not run when the sandbox is off")

        monkeypatch.setattr("self_improve.sandbox.verify_boundary", boom)
        c = dataclasses.replace(cfg_for(tmp_path), eval_sandbox_enabled=False)
        pipeline._ensure_sandbox(c, tmp_path, {"ok": None, "error": "", "observed": {}})


class TestTrialDirIsNotDeniedByTheStateDirGuard:
    """A trial workspace inside state must remain writable while the database is protected.
    A broad state-directory denial would override the trial's write allowance.
    """

    def test_the_run_tree_under_the_state_dir_stays_writable(self, tmp_path):
        c = cfg_for(tmp_path)
        trial = tmp_path / "state" / "runs" / "r1" / "trials" / "L1" / "with" / "trial-00" / "sandbox"
        trial.mkdir(parents=True)
        s = policy_for_trial(trial, c).to_settings()
        for denied in s["filesystem"]["denyWrite"]:
            assert not str(trial).startswith(denied.rstrip("/") + "/"), (
                f"trial dir {trial} sits under denyWrite entry {denied}"
            )

    def test_the_state_db_itself_is_still_protected(self, tmp_path):
        """Narrowing the guard must not expose the evidence DB."""
        c = cfg_for(tmp_path)
        denied = instruction_paths_to_protect(c)
        assert any(d.endswith("state.db") for d in denied), denied

    def test_the_whole_state_dir_is_no_longer_blanket_denied(self, tmp_path):
        c = cfg_for(tmp_path)
        assert str(tmp_path / "state") not in instruction_paths_to_protect(c)


class TestTheProbeUsesTheRealPolicy:
    """Verify the probe uses the same sandbox policy as trials, including denied write paths."""

    def test_the_probe_policy_carries_the_real_deny_list(self, tmp_path, monkeypatch):
        import subprocess as sp

        seen = {}

        def fake(argv, **kw):
            idx = argv.index("--settings")
            seen.update(json.loads(Path(argv[idx + 1]).read_text()))
            script = argv[-1]
            for tok in script.split():
                if tok.endswith("inside.txt"):
                    Path(tok).write_text("in")
            return sp.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(sp, "run", fake)
        c = cfg_for(tmp_path)
        verify_boundary(c, probe_root=tmp_path / "probe")
        assert seen["filesystem"]["denyWrite"], "probe ran with an empty deny list"
        assert any("CLAUDE.md" in d for d in seen["filesystem"]["denyWrite"])

    def test_a_deny_entry_covering_the_probe_dir_is_caught(self, tmp_path, monkeypatch):
        """The exact 2026-08-18 failure, as a regression test."""
        import subprocess as sp

        def fake(argv, **kw):
            # Simulate the kernel refusing the allowed write because a
            # denyWrite entry sits above it.
            return sp.CompletedProcess(argv, 1, "", "operation not permitted")

        monkeypatch.setattr(sp, "run", fake)
        with pytest.raises(SandboxError, match="refused an ALLOWED write"):
            verify_boundary(cfg_for(tmp_path), probe_root=tmp_path / "probe")


class TestTheProbeItselfMustBeAValidTest:
    """The probe's escape target must be somewhere the policy actually denies.

    Found 2026-08-22 by calling verify_boundary(cfg) with no probe_root and
    reading the raw output. The default probe_root is tempfile.mkdtemp(), which
    lands inside tempfile.gettempdir() — and policy_for_trial puts
    gettempdir() in allowWrite, because the agent needs a temp directory. So
    the "escape" was written to a path the sandbox was told to permit, the
    sandbox correctly permitted it, and verify_boundary reported
    "SANDBOX DID NOT CONTAIN THE PROBE".

    That is the same class of bug as the one this module was written to fix, in
    the opposite direction: a check that verifies a different artefact than the
    one that matters. Production passes an explicit probe_root under the state
    dir and is unaffected, which is precisely why nobody noticed.

    These tests use a fake sandbox that READS THE POLICY FILE and honours it.
    The older fakes above ignore the policy on purpose — they test what
    verify_boundary CONCLUDES from an outcome. These test whether the outcome
    it gets is a meaningful one in the first place.
    """

    def _policy_respecting_sandbox(self, monkeypatch):
        """A fake srt: writes a path only if the settings file permits it."""

        def fake(argv, **kw):
            settings = json.loads(Path(argv[argv.index("--settings") + 1]).read_text())
            fs = settings["filesystem"]
            allow = [Path(p).resolve() for p in fs["allowWrite"]]
            deny = [Path(p).resolve() for p in fs["denyWrite"]]

            def permitted(target: Path) -> bool:
                inside = any(target == a or a in target.parents for a in allow)
                blocked = any(target == d or d in target.parents for d in deny)
                return inside and not blocked

            rc = 0
            for token in argv[-1].split():
                if not token.endswith(".txt"):
                    continue
                target = Path(token).resolve()
                if permitted(target):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text("x")
                else:
                    rc = 1
            return subprocess.CompletedProcess(argv, rc, "", "operation not permitted")

        monkeypatch.setattr(subprocess, "run", fake)

    def test_the_default_probe_is_a_real_containment_test(self, tmp_path, monkeypatch):
        """verify_boundary(cfg) with NO probe_root must not report a false breach.

        Sabotage: put the escape target back inside the probe root
        (`outside = root / "OUTSIDE-must-not-exist.txt"`). The default root is
        under gettempdir(), which the policy allows, so the write succeeds and
        this raises "DID NOT CONTAIN".
        """
        self._policy_respecting_sandbox(monkeypatch)
        got = verify_boundary(cfg_for(tmp_path))
        assert got["outside_written"] is False, (
            "the probe's escape target was inside a directory policy_for_trial "
            "ALLOWS, so this proves nothing about containment"
        )
        assert got["inside_written"] is True

    def test_an_explicit_probe_root_is_also_a_real_test(self, tmp_path, monkeypatch):
        """The production call shape. pytest's tmp_path is itself under
        gettempdir() on macOS, so deriving the escape target from probe_root
        would be invalid here too."""
        self._policy_respecting_sandbox(monkeypatch)
        got = verify_boundary(cfg_for(tmp_path), probe_root=tmp_path / "probe")
        assert got["outside_written"] is False
        assert got["inside_written"] is True

    def test_the_escape_target_is_outside_every_allowed_path(self, tmp_path):
        """State the invariant directly, so it cannot regress silently."""
        from self_improve.sandbox import (
            agent_config_dir,
            escape_target_for,
            policy_for_trial,
        )

        cfg = cfg_for(tmp_path)
        work = tmp_path / "probe" / "work"
        policy = policy_for_trial(work, cfg, config_dir=agent_config_dir())
        target = escape_target_for(policy).resolve()
        for allowed in policy.allow_write:
            a = Path(allowed).resolve()
            assert not (target == a or a in target.parents), (
                f"escape target {target} sits inside allowWrite entry {a}"
            )

    def test_no_valid_escape_target_is_a_broken_probe_not_a_breach(self, tmp_path):
        """Without a valid forbidden target, report a broken probe rather than an observed sandbox escape."""
        from self_improve.sandbox import SandboxPolicy, escape_target_for

        everything = SandboxPolicy(allow_write=("/",))
        with pytest.raises(SandboxError, match="no valid escape target"):
            escape_target_for(everything)

    def test_a_breach_does_not_leave_the_escape_file_behind(self, tmp_path, monkeypatch):
        """A real breach writes a stray file outside the sandbox. Clean it up."""
        written: list[Path] = []

        def fake(argv, **kw):
            for token in argv[-1].split():
                if token.endswith(".txt"):
                    p = Path(token)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text("x")
                    written.append(p)
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(subprocess, "run", fake)
        with pytest.raises(SandboxError, match="DID NOT CONTAIN"):
            verify_boundary(cfg_for(tmp_path))
        escapes = [p for p in written if "OUTSIDE" in p.name]
        assert escapes, "fixture did not exercise the escape path"
        for p in escapes:
            assert not p.exists(), f"stray escape file left behind at {p}"
