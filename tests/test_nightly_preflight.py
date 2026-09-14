"""Execute the nightly quota preflight with fake provider binaries.

Tight quota can wait for reset. Unreadable OAuth credentials need interactive
login and a nonzero exit. Check the actual script exit and diagnostic."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "ops" / "run-nightly.sh"


def _fake_bin(path: Path, name: str, body: str) -> Path:
    p = path / name
    p.write_text("#!/bin/bash\n" + body + "\n")
    p.chmod(0o755)
    return p


@pytest.fixture
def rig(tmp_path):
    """Returns run(pick_json, uv_rc=0) -> CompletedProcess."""
    bins = tmp_path / "bin"
    bins.mkdir()
    state = tmp_path / "state"
    ran_marker = tmp_path / "uv_ran"
    caff_marker = tmp_path / "caffeinate_ran"

    def run(pick_payload: dict, uv_rc: int = 0, on_ac: bool = False):
        _fake_bin(
            bins, "quotapick", f"cat <<'EOF'\n{json.dumps(pick_payload)}\nEOF"
        )
        _fake_bin(bins, "uv", f'echo "$@" > "{ran_marker}"; exit {uv_rc}')
        # `pmset -g batt` prints one of these two lines; the script greps it.
        drawing = "AC Power" if on_ac else "Battery Power"
        _fake_bin(bins, "pmset", f"echo \"Now drawing from '{drawing}'\"")
        # A fake caffeinate that RECORDS that it wrapped the run and then execs
        # its argument, so the test sees both the assertion and the pipeline.
        _fake_bin(
            bins, "caffeinate",
            f'echo "$@" > "{caff_marker}"; shift; exec "$@"',
        )
        env = dict(os.environ)
        env.update(
            SI_STATE_DIR=str(state),
            SI_LOCK_DIR=str(tmp_path / "run.lock"),
            SI_QUOTAPICK=str(bins / "quotapick"),
            SI_PYTHON3="/usr/bin/python3",
            SI_UV=str(bins / "uv"),
            SI_PROJECT=str(tmp_path / "project"),
            SI_PMSET=str(bins / "pmset"),
            SI_CAFFEINATE=str(bins / "caffeinate"),
        )
        proc = subprocess.run(
            ["/bin/bash", str(SCRIPT)], capture_output=True, text=True, env=env
        )
        proc.pipeline_ran = ran_marker.exists()  # type: ignore[attr-defined]
        proc.caffeinated = caff_marker.exists()  # type: ignore[attr-defined]
        proc.uv_argv = (  # type: ignore[attr-defined]
            ran_marker.read_text().strip() if ran_marker.exists() else ""
        )
        return proc

    return run


def _decision(fits: bool, **extra) -> dict:
    return {"decision": {"fits": fits, "chosen": "claude_a"}, **extra}


def test_healthy_fleet_runs_the_pipeline(rig):
    proc = rig(_decision(True))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.pipeline_ran


def test_genuinely_tight_quota_skips_quietly_with_exit_0(rig):
    """A reset will fix this, so a quiet skip is correct — launchd should not alarm."""
    proc = rig(_decision(False, degraded=[{"account": "claude_a", "reason": "stale snapshot"}]))
    assert proc.returncode == 0
    assert "skip night: quota tight" in proc.stdout
    assert not proc.pipeline_ran


def test_a_dark_fleet_fails_loud_instead_of_skipping(rig):
    """The regression this file exists for.

    Before: printed 'quota tight' and exited 0, so launchd recorded success
    while nothing was mined, indefinitely.
    """
    proc = rig(
        _decision(
            False,
            degraded=[
                {"account": "claude_a", "reason": "unreadable: access token expired"},
                {"account": "codex", "reason": "unreadable: no credentials on disk"},
            ],
        )
    )
    assert proc.returncode == 1, (
        "a dark fleet must be a LOUD failure; exit 0 makes launchd record "
        "success while the miner does nothing every night"
    )
    assert not proc.pipeline_ran
    assert "DARK" in proc.stdout
    assert "claude_a" in proc.stdout and "codex" in proc.stdout
    assert "no reset clears this" in proc.stdout


def test_the_dark_message_says_what_to_do_about_it(rig):
    proc = rig(_decision(False, excluded=[{"id": "claude_b", "reason": "unreadable: revoked"}]))
    assert proc.returncode == 1
    assert "login" in proc.stdout.lower()


def test_unreadable_rows_in_excluded_count_too(rig):
    """quotapick reports them in degraded OR excluded depending on the path."""
    proc = rig(_decision(False, excluded=[{"id": "claude_c", "reason": "unreadable: expired"}]))
    assert proc.returncode == 1


def test_a_merely_stale_snapshot_is_not_treated_as_dark(rig):
    """Same conflation that made meets_policy lie once: stale != unreadable."""
    proc = rig(
        _decision(
            False,
            degraded=[{"account": "claude_a", "reason": "snapshot 3.0h stale"}],
        )
    )
    assert proc.returncode == 0
    assert "skip night: quota tight" in proc.stdout


def test_unparseable_quotapick_output_is_loud(rig, tmp_path):
    bins = tmp_path / "bin"
    proc = rig(_decision(True))  # seed the fakes
    _fake_bin(bins, "quotapick", "echo 'not json at all'")
    env = dict(os.environ)
    env.update(
        SI_STATE_DIR=str(tmp_path / "state"),
        SI_LOCK_DIR=str(tmp_path / "lock2"),
        SI_QUOTAPICK=str(bins / "quotapick"),
        SI_PYTHON3="/usr/bin/python3",
        SI_UV=str(bins / "uv"),
        SI_PROJECT=str(tmp_path / "project"),
    )
    proc = subprocess.run(
        ["/bin/bash", str(SCRIPT)], capture_output=True, text=True, env=env
    )
    assert proc.returncode == 1
    assert "quota preflight failed" in proc.stdout


def test_pipeline_exit_code_is_propagated(rig):
    """A failing run must not be reported as a successful night."""
    proc = rig(_decision(True), uv_rc=3)
    assert proc.returncode == 3


def test_default_home_paths_and_discovered_uv_run_without_machine_overrides(tmp_path):
    """Exercise actual defaults, first probing them before any filesystem write."""
    home = tmp_path / "example-user"
    bins = home / ".local" / "bin"
    bins.mkdir(parents=True)
    marker = tmp_path / "uv-arguments"
    _fake_bin(bins, "quotapick", "cat <<'EOF'\n" + json.dumps(_decision(True)) + "\nEOF")
    _fake_bin(bins, "uv", f'echo "$@" > "{marker}"')
    _fake_bin(bins, "pmset", "echo 'Battery Power'")
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(home),
        "SI_PATH_EXTRA": str(bins),
        "SI_PMSET": str(bins / "pmset"),
    }
    # Stop before mkdir. This must fail safely if a future edit restores a
    # literal home path; the test must never create logs or locks in that home.
    source = SCRIPT.read_text()
    first_write = '\n/bin/mkdir -p "${STATE_DIR}/logs"'
    assert source.count(first_write) == 1
    prefix = source.split(first_write)[0]
    probe = subprocess.run(
        ["/bin/bash", "-c", prefix + '\nprintf "%s\\n" "$STATE_DIR" "$QUOTAPICK" "$UV"'],
        env=env, capture_output=True, text=True,
    )
    assert probe.returncode == 0, probe.stdout + probe.stderr
    assert probe.stdout.splitlines() == [
        str(home / ".self-improve"), str(bins / "quotapick"), str(bins / "uv"),
    ]
    proc = subprocess.run(["/bin/bash", str(SCRIPT)], env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert marker.read_text().startswith(f"run --project {SCRIPT.parent.parent} ")
    assert (home / ".self-improve" / "logs").is_dir()
    assert not (home / ".self-improve" / "run.lock").exists()


# ---------------------------------------------------------------------------
# Default checkout derived from the script location
# ---------------------------------------------------------------------------
#
# Run copied scripts without SI_PROJECT so each invocation must derive its
# own checkout. An override in every test would hide a broken default.


def _run_with_default_project(tmp_path, checkout_name: str):
    """Copy the real script into a fake checkout and run it without SI_PROJECT."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    bins = tmp_path / "bin"
    bins.mkdir(exist_ok=True)
    checkout = tmp_path / checkout_name
    (checkout / "ops").mkdir(parents=True)
    script = checkout / "ops" / "run-nightly.sh"
    script.write_text(SCRIPT.read_text())
    script.chmod(0o755)

    ran_marker = tmp_path / "uv_argv"
    _fake_bin(bins, "quotapick", "cat <<'EOF'\n" + json.dumps(_decision(True)) + "\nEOF")
    _fake_bin(bins, "uv", f'echo "$@" > "{ran_marker}"; exit 0')

    env = dict(os.environ)
    env.update(
        SI_STATE_DIR=str(tmp_path / "state"),
        SI_LOCK_DIR=str(tmp_path / "run.lock"),
        SI_QUOTAPICK=str(bins / "quotapick"),
        SI_PYTHON3="/usr/bin/python3",
        SI_UV=str(bins / "uv"),
    )
    env.pop("SI_PROJECT", None)  # the whole point of this test
    proc = subprocess.run(
        ["/bin/bash", str(script)], capture_output=True, text=True, env=env
    )
    argv = ran_marker.read_text().strip() if ran_marker.exists() else ""
    return proc, argv, checkout


def test_default_project_is_the_scripts_own_checkout(tmp_path):
    """--project must name the checkout the script itself lives in.

    Sabotage: replace the derived PROJECT default with a literal checkout path.
    The asserted --project then names a path outside tmp_path and this fails.
    """
    proc, argv, checkout = _run_with_default_project(tmp_path, "repo-prod")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"--project {checkout}" in argv, argv


def test_default_project_follows_the_script_between_checkouts(tmp_path):
    """Two checkouts, two different --project values, no literal in between.

    This is the property a hardcoded default cannot have, and it is what makes
    repo-prod (production) and repo-0 (agent working copy) safe to coexist.
    """
    _, argv_prod, prod = _run_with_default_project(tmp_path / "a", "repo-prod")
    _, argv_zero, zero = _run_with_default_project(tmp_path / "b", "repo-0")
    assert f"--project {prod}" in argv_prod
    assert f"--project {zero}" in argv_zero
    assert argv_prod != argv_zero


# ---------------------------------------------------------------------------
# launchd's PATH, one layer deeper than the absolute-binary rule
# ---------------------------------------------------------------------------
#
# The script resolves absolute paths for quotapick, uv, and /usr/bin/python3
# and /opt/homebrew/bin/uv precisely because launchd's PATH is minimal. That is
# enough for the SCRIPT to run and not enough for the pipeline: llm.py then
# spawns `codex` (an absolute path from config) and codex's own wrapper invokes
# `node` by bare name.
#
# An absolute path to a binary is not enough when the binary itself shells out.


def _path_the_pipeline_inherits(tmp_path, *, path_extra=None):
    """Run the real script under launchd's PATH; capture what uv inherits."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    bins = tmp_path / "bin"
    bins.mkdir()
    seen = tmp_path / "inherited_path"
    _fake_bin(bins, "quotapick", "cat <<'EOF'\n" + json.dumps(_decision(True)) + "\nEOF")
    _fake_bin(bins, "uv", f'printenv PATH > "{seen}"; exit 0')

    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",       # launchd's, verbatim
        "HOME": os.environ["HOME"],
        "SI_STATE_DIR": str(tmp_path / "state"),
        "SI_LOCK_DIR": str(tmp_path / "run.lock"),
        "SI_QUOTAPICK": str(bins / "quotapick"),
        "SI_PYTHON3": "/usr/bin/python3",
        "SI_UV": str(bins / "uv"),
        "SI_PROJECT": str(tmp_path / "project"),
    }
    if path_extra is not None:
        env["SI_PATH_EXTRA"] = path_extra
    proc = subprocess.run(
        ["/bin/bash", str(SCRIPT)], capture_output=True, text=True, env=env
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return seen.read_text().strip()


def test_the_pipeline_inherits_a_path_that_can_find_node(tmp_path):
    """The spawned pipeline inherits a PATH that resolves the CLI interpreter."""
    path = _path_the_pipeline_inherits(tmp_path)
    assert "/opt/homebrew/bin" in path, (
        f"the default tool directory is missing from the inherited PATH: {path}"
    )


def test_the_pipeline_inherits_a_path_that_can_find_the_agent_clis(tmp_path):
    """The inherited PATH includes both configured default tool directories."""
    path = _path_the_pipeline_inherits(tmp_path)
    home = os.environ["HOME"]
    for needed in (f"{home}/.local/bin", "/opt/homebrew/bin"):
        assert needed in path, f"{needed} missing from the inherited PATH: {path}"


def test_launchds_own_path_entries_are_kept_not_replaced(tmp_path):
    """Prepending must not drop /usr/bin — /usr/bin/python3 is spelled out but
    plenty else resolves through it."""
    path = _path_the_pipeline_inherits(tmp_path)
    for base in ("/usr/bin", "/bin"):
        assert base in path.split(":"), f"{base} was dropped: {path}"


def test_child_clis_resolve_their_interpreter_from_the_configured_path(tmp_path):
    """An absolute CLI path still needs the interpreter named by its shebang."""
    bins = tmp_path / "tools"
    bins.mkdir()
    _fake_bin(bins, "node", 'echo "node received: $*"')
    wrapper = bins / "codex"
    wrapper.write_text("#!/usr/bin/env node\n// invented CLI wrapper\n")
    wrapper.chmod(0o755)
    path = _path_the_pipeline_inherits(tmp_path / "job", path_extra=str(bins))
    proc = subprocess.run(
        [str(wrapper), "--version"], env={"PATH": path}, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == f"node received: {wrapper} --version"


@pytest.mark.parametrize("uv_override", [None, "missing-absolute", "relative-uv"])
def test_missing_uv_is_reported_before_creating_state_or_running_the_preflight(tmp_path, uv_override):
    state = tmp_path / "state"
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    env = {
        "HOME": str(tmp_path), "PATH": str(empty_bin),
        "SI_PATH_EXTRA": str(empty_bin), "SI_STATE_DIR": str(state),
    }
    if uv_override is not None:
        env["SI_UV"] = str(tmp_path / "missing-uv") if uv_override == "missing-absolute" else "uv"
    proc = subprocess.run(
        ["/bin/bash", str(SCRIPT)],
        env=env,
        capture_output=True, text=True,
    )
    assert proc.returncode == 1
    assert "nightly setup failed" in proc.stdout and "SI_UV" in proc.stdout
    assert not state.exists()


# ---------------------------------------------------------------------------
# holding the machine awake, but only on AC (operator decision 2026-09-01)
# ---------------------------------------------------------------------------


def test_on_ac_the_run_is_wrapped_in_caffeinate(rig):
    """On AC power, caffeinate must keep the machine awake while executing the pipeline."""
    proc = rig(_decision(True), on_ac=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.caffeinated, "on AC the run must be held awake"
    assert proc.pipeline_ran, "caffeinate must still exec the pipeline, not replace it"
    assert "on AC" in proc.stdout


def test_on_battery_the_machine_is_left_alone(rig):
    """Battery power must not start the AC-only keep-awake process."""
    proc = rig(_decision(True), on_ac=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not proc.caffeinated, "on battery the machine must not be held awake"
    assert proc.pipeline_ran, "the run must still happen, just without the assertion"
    assert "on battery" in proc.stdout


def test_the_battery_branch_does_not_abort_under_set_u(rig):
    """The battery branch must reach the pipeline under Bash nounset mode.

    Bash 3.2 can reject an empty array expansion under set -u. This branch must
    avoid that failure when no keep-awake wrapper is required."""
    proc = rig(_decision(True), on_ac=False)
    assert "unbound variable" not in proc.stderr, proc.stderr
    assert proc.returncode == 0 and proc.pipeline_ran


def test_caffeinate_does_not_swallow_the_pipelines_exit_code(rig):
    """launchd reads this. If the wrapper returned its own status, a failed
    run would be recorded as success forever — the same shape as the dark-fleet
    bug this file was written for."""
    proc = rig(_decision(True), uv_rc=3, on_ac=True)
    assert proc.caffeinated
    assert proc.returncode == 3, proc.stdout + proc.stderr


def test_the_run_arguments_are_unchanged_by_the_wrapper(rig):
    """--review-only must survive being wrapped. Losing it would turn a
    review-only night into one that writes to the operator's CLAUDE.md."""
    for on_ac in (True, False):
        proc = rig(_decision(True), on_ac=on_ac)
        assert "--review-only" in proc.uv_argv, (on_ac, proc.uv_argv)
        assert "selfimprove run" in proc.uv_argv, (on_ac, proc.uv_argv)
