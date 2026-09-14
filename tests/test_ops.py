"""Tests for scheduler scripts, LaunchAgent plists, and launchd integration.

Launchd install, uninstall, and status calls use an injected command runner.
The suite also runs shell syntax checks, local plist linting, and scripts with
invented paths and executables. Script execution checks cover argument flow,
working-directory resolution, and the inherited PATH."""

from __future__ import annotations

import os
import plistlib
import re
import subprocess
import sys
from pathlib import Path

import pytest

from self_improve import launchd
from self_improve.config import Config

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "ops" / "run-nightly.sh"
PLIST = REPO / "ops" / "com.tonygwu.self-improve.plist"


# ---------------------------------------------------------------------------
# Shipped artifacts: syntax and lint
# ---------------------------------------------------------------------------


def test_script_exists_and_bash_syntax_ok():
    assert SCRIPT.is_file()
    result = subprocess.run(
        ["/bin/bash", "-n", str(SCRIPT)], capture_output=True, text=True
    )
    assert result.returncode == 0, f"bash -n failed:\n{result.stderr}"


def test_sabotage_usage_contains_instructions_without_executing_or_dumping_code(tmp_path):
    script = tmp_path / "sabotage.sh"
    script.write_text((REPO / "ops/sabotage.sh").read_text())
    result = subprocess.run(
        ["/bin/bash", str(script)], cwd=tmp_path, capture_output=True, text=True
    )
    assert result.returncode == 2
    assert "<file> <old-string> <new-string> <pytest -k expression>" in result.stderr
    assert all(not line or line.startswith("#") for line in result.stderr.splitlines())
    assert result.stdout == ""
    assert sorted(p.name for p in tmp_path.iterdir()) == ["sabotage.sh"]


@pytest.mark.skipif(sys.platform != "darwin", reason="native launchd lint requires macOS")
def test_plist_exists_and_plutil_lint_ok():
    assert PLIST.is_file()
    result = subprocess.run(
        ["/usr/bin/plutil", "-lint", str(PLIST)], capture_output=True, text=True
    )
    assert result.returncode == 0, f"plutil -lint failed:\n{result.stdout}{result.stderr}"


# ---------------------------------------------------------------------------
# render_plist <-> shipped plist
# ---------------------------------------------------------------------------


def _example_cfg() -> Config:
    return Config(
        state_dir="/path/to/private-state",
        production_repo_path="/path/to/self-improve/repo-prod",
    )


def test_render_plist_parses_and_matches_shipped_example():
    rendered = plistlib.loads(launchd.render_plist(_example_cfg()).encode("utf-8"))
    with open(PLIST, "rb") as fh:
        shipped = plistlib.load(fh)
    assert rendered == shipped


def test_plist_contract_fields():
    with open(PLIST, "rb") as fh:
        p = plistlib.load(fh)
    cfg = _example_cfg()
    assert p["Label"] == "com.tonygwu.self-improve"
    # Derive the script path from the example config. A fixed checkout
    # literal would miss a default that fails after repository relocation.
    assert p["ProgramArguments"] == [
        "/bin/bash",
        str(launchd.nightly_script_path(cfg)),
    ]
    assert p["StartCalendarInterval"] == {"Hour": 2, "Minute": 30}
    assert p["RunAtLoad"] is False
    for key in ("StandardOutPath", "StandardErrorPath"):
        assert p[key].startswith(str(cfg.state_path("logs"))), p[key]


# ---------------------------------------------------------------------------
# Absolute-path discipline (launchd's PATH excludes ~/.local/bin and
# /opt/homebrew/bin, so a single bare command means the job silently no-ops)
# ---------------------------------------------------------------------------


def test_plist_paths_are_absolute():
    with open(PLIST, "rb") as fh:
        p = plistlib.load(fh)
    for arg in p["ProgramArguments"]:
        assert arg.startswith("/"), f"non-absolute ProgramArguments entry: {arg}"
    for key in ("StandardOutPath", "StandardErrorPath"):
        assert p[key].startswith("/"), f"non-absolute {key}: {p[key]}"


def _script_code_lines() -> list[str]:
    """Script lines with comments stripped (the script uses '#' only for
    comments; the test would over-strip otherwise, which is fine for a
    paths-must-be-absolute check)."""
    lines = []
    for raw in SCRIPT.read_text().splitlines():
        code = raw.split("#", 1)[0]
        if code.strip():
            lines.append(code)
    return lines


def test_script_path_tokens_are_absolute():
    """Every path in the script is absolute or rooted in a variable.

    launchd runs the script with a minimal PATH and an unspecified cwd, so a
    relative path registers fine and then fails every night. Defaults may be
    composed from ``${HOME}`` or another variable; those variables are checked
    here or set by launchd. Tokens split on ``:-`` and ``:`` so each default and
    each PATH entry is checked on its own.
    """
    checked: list[str] = []
    for line in _script_code_lines():
        for tok in re.split(r"[\s;()=|]+", line):
            tok = tok.strip("\"'")
            # "2>/dev/null" and ">>/log": check the redirect target.
            tok = re.sub(r"^\d*[<>&]+", "", tok)
            for piece in re.split(r":-|:", tok):
                piece = piece.strip("\"'")
                if "/" not in piece:
                    continue
                checked.append(piece)
                assert piece.startswith("/") or piece.startswith("${"), (
                    f"non-absolute path {piece!r} in line {line!r}"
                )
    # Prove the scan reached the constructs it exists for, not an empty list.
    for expected in ("${HOME}/.local/bin/quotapick", "/usr/bin/python3",
                     "/opt/homebrew/bin", "/usr/bin/dirname", "/dev/null"):
        assert any(p.startswith(expected) for p in checked), (expected, checked)


def test_script_verifies_every_command_v_lookup_is_absolute():
    """``$(command -v X)`` can return a shell builtin, an alias, or nothing.

    The path scan cannot see that, so each variable assigned this way must be
    checked at runtime for an absolute executable before it is used.
    """
    text = SCRIPT.read_text()
    lookups = re.findall(r'^\s*([A-Z_][A-Z0-9_]*)="[^"\n]*\$\(command -v ', text, re.M)
    assert lookups, "expected at least one command -v lookup (uv)"
    for name in lookups:
        guard = f'[[ "${{{name}}}" != /* || ! -x "${{{name}}}" ]]'
        assert guard in text, f"{name} comes from command -v without the guard {guard!r}"


def test_script_does_not_call_flock():
    # macOS ships no flock(1). Locking behavior is tested through the script.
    for line in _script_code_lines():
        assert "flock" not in line, f"flock referenced in code line: {line!r}"


def test_script_quota_skip_and_fail_loud_semantics():
    text = SCRIPT.read_text()
    # Genuinely tight quota skips quietly with the agreed message...
    assert "skip night: quota tight" in text
    # ...while a broken preflight (quotapick error / unparseable JSON) is loud.
    assert "quota preflight failed" in text
    # ...and a DARK fleet (expired tokens) is loud too, because no reset fixes
    # it and exit 0 would make launchd record success forever.
    assert "DARK" in text
    assert "unreadable:" in text
    # Behaviour, not just wording, is covered in test_nightly_preflight.py,
    # which executes this script against a fake quotapick.


# ---------------------------------------------------------------------------
# launchd.py install/uninstall/status via a scripted fake runner
# ---------------------------------------------------------------------------


class ScriptedRunner:
    """Fake RunCmd: pops one scripted (substring, rc, stdout) per call and
    asserts the call order matches the script."""

    def __init__(self, script: list[tuple[str, int, str]]):
        self.script = list(script)
        self.calls: list[str] = []

    def __call__(self, cmd: list[str]) -> subprocess.CompletedProcess:
        joined = " ".join(cmd)
        self.calls.append(joined)
        assert self.script, f"unexpected extra command: {joined}"
        substring, rc, stdout = self.script.pop(0)
        assert substring in joined, f"expected {substring!r} next, got {joined!r}"
        return subprocess.CompletedProcess(cmd, rc, stdout=stdout, stderr="")

    def assert_exhausted(self) -> None:
        assert not self.script, f"scripted commands never issued: {self.script}"


def _cfg(tmp_path: Path) -> Config:
    checkout = tmp_path / "production" / "repo-prod"
    (checkout / "ops").mkdir(parents=True, exist_ok=True)
    script = checkout / "ops" / "run-nightly.sh"
    script.write_text("#!/bin/bash\nexit 0\n")
    script.chmod(0o755)
    return Config(state_dir=str(tmp_path / "state"), production_repo_path=str(checkout))


def test_install_fresh_copies_bootstraps_and_verifies(tmp_path):
    dst = tmp_path / "LaunchAgents" / "com.tonygwu.self-improve.plist"
    runner = ScriptedRunner(
        [
            ("/usr/bin/plutil -lint", 0, f"{PLIST}: OK"),
            ("/bin/launchctl print", 113, ""),  # probe: not yet registered
            ("/bin/launchctl bootstrap", 0, ""),
            ("/bin/launchctl print", 0, "state = waiting"),
        ]
    )
    cfg = _cfg(tmp_path)
    evidence = launchd.install(cfg, run_cmd=runner, agent_plist_path=dst)
    runner.assert_exhausted()
    # The installed plist is RENDERED FROM CONFIG, not copied from the repo, so
    # the job always names this machine's production checkout and this machine's
    # log directory. Copying the checked-in file is how a stale path survived.
    assert dst.read_text() == launchd.render_plist(cfg)
    installed = plistlib.loads(dst.read_bytes())
    assert installed["ProgramArguments"][1] == str(launchd.nightly_script_path(cfg))
    # log dir created (launchd creates files, not directories)
    assert (tmp_path / "state" / "logs").is_dir()
    # evidence carries the actual commands and their output, not assertions
    assert "plutil -lint" in evidence
    assert f"bootstrap gui/{__import__('os').getuid()}" in evidence
    assert "state = waiting" in evidence
    assert "rc=0" in evidence


def test_install_reinstall_boots_out_existing_first(tmp_path):
    dst = tmp_path / "agent.plist"
    runner = ScriptedRunner(
        [
            ("/usr/bin/plutil -lint", 0, "OK"),
            ("/bin/launchctl print", 0, "already loaded"),  # probe: registered
            ("/bin/launchctl bootout", 0, ""),
            ("/bin/launchctl bootstrap", 0, ""),
            ("/bin/launchctl print", 0, "state = waiting"),
        ]
    )
    evidence = launchd.install(_cfg(tmp_path), run_cmd=runner, agent_plist_path=dst)
    runner.assert_exhausted()
    assert "booting out" in evidence


def test_install_lint_failure_raises_before_copy(tmp_path):
    dst = tmp_path / "agent.plist"
    runner = ScriptedRunner([("/usr/bin/plutil -lint", 1, "invalid plist")])
    with pytest.raises(launchd.LaunchdError, match="plutil -lint failed"):
        launchd.install(_cfg(tmp_path), run_cmd=runner, agent_plist_path=dst)
    runner.assert_exhausted()
    assert not dst.exists(), "plist must not be copied when lint fails"


def test_install_unverified_registration_raises(tmp_path):
    dst = tmp_path / "agent.plist"
    runner = ScriptedRunner(
        [
            ("/usr/bin/plutil -lint", 0, "OK"),
            ("/bin/launchctl print", 113, ""),
            ("/bin/launchctl bootstrap", 0, ""),
            ("/bin/launchctl print", 113, ""),  # verify fails
        ]
    )
    with pytest.raises(launchd.LaunchdError, match="NOT verified"):
        launchd.install(_cfg(tmp_path), run_cmd=runner, agent_plist_path=dst)
    runner.assert_exhausted()


def test_install_bootstrap_failure_raises(tmp_path):
    dst = tmp_path / "agent.plist"
    runner = ScriptedRunner(
        [
            ("/usr/bin/plutil -lint", 0, "OK"),
            ("/bin/launchctl print", 113, ""),
            ("/bin/launchctl bootstrap", 5, "Bootstrap failed: 5: Input/output error"),
        ]
    )
    with pytest.raises(launchd.LaunchdError, match="bootstrap failed"):
        launchd.install(_cfg(tmp_path), run_cmd=runner, agent_plist_path=dst)
    runner.assert_exhausted()


def test_uninstall_boots_out_and_removes_plist(tmp_path):
    dst = tmp_path / "agent.plist"
    dst.write_bytes(PLIST.read_bytes())
    runner = ScriptedRunner(
        [
            ("/bin/launchctl print", 0, "loaded"),
            ("/bin/launchctl bootout", 0, ""),
        ]
    )
    evidence = launchd.uninstall(_cfg(tmp_path), run_cmd=runner, agent_plist_path=dst)
    runner.assert_exhausted()
    assert not dst.exists()
    assert "removed" in evidence


def test_uninstall_nothing_to_do_raises(tmp_path):
    dst = tmp_path / "agent.plist"  # never created
    runner = ScriptedRunner([("/bin/launchctl print", 113, "")])
    with pytest.raises(launchd.LaunchdError, match="nothing to uninstall"):
        launchd.uninstall(_cfg(tmp_path), run_cmd=runner, agent_plist_path=dst)
    runner.assert_exhausted()


def test_status_registered_and_not_registered():
    registered = launchd.status(
        run_cmd=ScriptedRunner([("/bin/launchctl print", 0, "state = waiting")])
    )
    assert "registered" in registered
    assert "NOT registered" not in registered
    assert "state = waiting" in registered

    missing = launchd.status(
        run_cmd=ScriptedRunner([("/bin/launchctl print", 113, "")])
    )
    assert "NOT registered" in missing
    assert "rc=113" in missing


def test_plist_uses_the_real_launchd_stderr_key():
    """Use StandardErrorPath so launchd captures stderr in the configured log."""
    with open(PLIST, "rb") as fh:
        p = plistlib.load(fh)
    assert "StandardErrorPath" in p, "plist must use launchd's StandardErrorPath"
    assert "StandardErrPath" not in p, (
        "StandardErrPath is not a launchd key; it is accepted by plistlib and "
        "ignored by launchd, so stderr is discarded"
    )
    assert p["StandardErrorPath"].startswith(str(_example_cfg().state_path("logs")))


def test_generated_plist_matches_the_checked_in_one_on_the_log_keys():
    """The installer writes the plist; it must not reintroduce the typo."""
    content = launchd.plist_content(Config())

    assert "StandardErrorPath" in content
    assert "StandardErrPath" not in content


def test_script_does_not_enable_errexit():
    """`set -e` would make the script's own error handling dead code.

    Every failure path reads `$?` after a command substitution and prints a
    diagnostic naming the cause. Under errexit the assignment aborts first, so
    launchd would record a bare non-zero exit with nothing explaining it. This
    pins the choice, which is otherwise the kind of thing someone tightens on
    sight.
    """
    text = SCRIPT.read_text()
    set_lines = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("set -")]
    assert set_lines, "script must set shell options"
    for ln in set_lines:
        assert "e" not in ln.split("-o")[0].replace("set", "").replace("-", ""), (
            f"errexit enabled in {ln!r}; the explicit $? checks become dead code"
        )


# ---------------------------------------------------------------------------
# The check whose absence let a dead job register
# ---------------------------------------------------------------------------


def test_rendered_plist_names_the_configured_checkout_not_the_example(tmp_path):
    cfg = _cfg(tmp_path)
    p = plistlib.loads(launchd.render_plist(cfg).encode())
    script = Path(p["ProgramArguments"][1])
    assert script == Path(cfg.production_repo_path) / "ops" / "run-nightly.sh"
    assert script.is_file()
    assert os.access(script, os.X_OK), f"{script} is not executable"


def test_install_refuses_when_the_nightly_script_is_missing(tmp_path):
    """install() must fail loud rather than register a job that cannot run."""
    dst = tmp_path / "LaunchAgents" / "com.tonygwu.self-improve.plist"
    cfg = Config(
        state_dir=str(tmp_path / "state"),
        production_repo_path=str(tmp_path / "no-such-checkout"),
    )
    runner = ScriptedRunner([])  # must never reach plutil or launchctl
    with pytest.raises(launchd.LaunchdError) as exc:
        launchd.install(cfg, run_cmd=runner, agent_plist_path=dst)
    assert "nightly script missing" in str(exc.value)
    assert not dst.exists(), "no plist may be installed when the script is absent"


def test_install_refuses_a_present_but_non_executable_script(tmp_path):
    checkout = tmp_path / "repo-prod"
    (checkout / "ops").mkdir(parents=True)
    script = checkout / "ops" / "run-nightly.sh"
    script.write_text("#!/bin/bash\ntrue\n")
    script.chmod(0o644)  # readable, not executable
    cfg = Config(
        state_dir=str(tmp_path / "state"), production_repo_path=str(checkout)
    )
    with pytest.raises(launchd.LaunchdError) as exc:
        launchd.install(
            cfg,
            run_cmd=ScriptedRunner([]),
            agent_plist_path=tmp_path / "LaunchAgents" / "x.plist",
        )
    assert "not executable" in str(exc.value)


def test_the_production_checkout_is_one_routing_will_never_write_into():
    """repo-prod is where automation RUNS; repo-0 is where automation WRITES.

    These are different questions, and the dangerous way to get them wrong is
    to let them collapse: a proposal committed into the production checkout
    both dirties a tree that must stay clean and puts an unreviewed edit into
    the checkout that runs tomorrow night.

    Asserting `production_repo_path != <this repo>` would be the obvious test
    and is wrong — running the suite inside repo-prod to verify a deploy is a
    legitimate thing to do. The invariant that actually matters is that routing
    refuses to write there, whichever checkout the tests happen to run from.
    """
    from self_improve.routing import is_never_write

    cfg = Config()
    assert Path(cfg.production_repo_path).name == "repo-prod"
    assert is_never_write(cfg.production_repo_path), (
        "the checkout automation runs from must be one routing never writes to"
    )


# ---------------------------------------------------------------------------
# PATH setup inherited by child processes
# ---------------------------------------------------------------------------


def _path_block() -> str:
    """The shipped script's own PATH lines, not a copy of them."""
    lines = [
        ln for ln in SCRIPT.read_text().splitlines()
        if ln.startswith("PATH=") or ln.strip() == "export PATH"
    ]
    assert lines, "run-nightly.sh has no PATH assignment at all"
    return "\n".join(lines)


def test_the_path_line_prepends_and_exports():
    """Execute the script's PATH setup under a minimal launchd-like environment.
    The assertions cover assignment, prepending, and each default directory.
    PATH is already exported by the supplied environment, so this test cannot
    separately prove the defensive export keyword.
    """
    minimal = "/usr/bin:/bin:/usr/sbin:/sbin"
    proc = subprocess.run(
        ["/bin/bash", "-c", _path_block() + "\nprintenv PATH"],
        env={"PATH": minimal, "HOME": "/tmp/fake-home",
             "SI_PATH_EXTRA": "/sentinel/one:/sentinel/two"},
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    got = proc.stdout.strip()
    assert got.startswith("/sentinel/one:/sentinel/two:"), (
        f"the extra directories are not PREPENDED: {got!r}"
    )
    # Prepend, not replace: launchd's own entries still have to resolve.
    for entry in minimal.split(":"):
        assert entry in got.split(":"), f"{entry} was dropped from PATH: {got!r}"


def test_the_default_path_covers_the_interpreters_the_agents_need():
    """Without an override, PATH includes the default tool directories.

    An absolute CLI path can still require an interpreter resolved through PATH.
    The shell must expand HOME when adding the user-local binary directory."""
    proc = subprocess.run(
        ["/bin/bash", "-c", _path_block() + "\nprintenv PATH"],
        env={"PATH": "/usr/bin:/bin", "HOME": "/tmp/fake-home"},
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    entries = proc.stdout.strip().split(":")
    assert "/opt/homebrew/bin" in entries, entries
    assert "/tmp/fake-home/.local/bin" in entries, (
        f"~/.local/bin is not on the path, or HOME was not expanded: {entries}"
    )


def test_worker_service_uses_production_checkout_and_explicit_error_log():
    cfg=_example_cfg()
    data=plistlib.loads(launchd.render_worker_plist(cfg).encode())
    assert data['ProgramArguments']==['/bin/bash', str(Path(cfg.production_repo_path)/'ops'/'run-worker.sh')]
    assert data['StandardErrorPath']==str(cfg.state_path('logs')/'worker.stderr.log')
    assert data['KeepAlive']=={'SuccessfulExit':False}
    assert data['RunAtLoad'] is True


def test_worker_script_runs_its_own_checkout_without_syncing_dependencies(tmp_path):
    import shutil
    project=tmp_path/'relocated-production'
    (project/'ops').mkdir(parents=True)
    script=project/'ops'/'run-worker.sh'
    shutil.copyfile(REPO/'ops'/'run-worker.sh',script)
    fake=tmp_path/'uv'
    fake.write_text('#!/bin/bash\nprintf "%s\\n" "$PWD" "$@"\n')
    fake.chmod(0o755)
    result=subprocess.run(['/bin/bash',str(script),'--once'],env={'PATH':'/usr/bin:/bin','SI_PATH_EXTRA':str(tmp_path),'SI_UV':str(fake)},capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert result.stdout.splitlines()==[str(project),'run','--no-sync','--frozen','selfimprove','worker','--once']
