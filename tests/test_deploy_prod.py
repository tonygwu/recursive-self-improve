"""Tests for the gated production deployment script.

Every test here EXECUTES ops/deploy-prod.sh against a real temporary git
topology (a bare origin, a production clone, a staging clone the script creates
itself) and fake `uv`. Nothing reads the operator's real checkouts, state
directory, or LaunchAgents, and no model or network call is made.

The script is copied to <prod>/ops/deploy-prod.sh and invoked THERE, so the
default derivation of the production root from the script's own location is the
thing under test. SI_PROD_REPO is deliberately not set in most tests: an
override would hide a broken default.
"""

from __future__ import annotations

import importlib.util
import json
import os
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

from self_improve import launchd
from self_improve.config import Config

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "ops" / "deploy-prod.sh"
DEPLOY_PLIST = REPO / "ops" / "com.tonygwu.self-improve.deploy.plist"
NIGHTLY = REPO / "ops" / "run-nightly.sh"

GIT = "/usr/bin/git"
PYTHON3 = "/usr/bin/python3"

# Every deploy in this module writes its self copy to a shared temporary
# directory. run_deploy passes no TMPDIR, so the script uses /tmp, and
# test_rejects_arguments passes on the caller's TMPDIR.
# test_temporary_self_copy_is_removed compares listings of /tmp and $TMPDIR, so
# a deploy running at the same moment in another xdist worker changes them.
# Under `--dist loadgroup`, one worker runs this module in turn. The group does
# not cover other processes on the same machine, such as another checkout's
# suite; those can still change the listings.
pytestmark = pytest.mark.xdist_group("deploy_prod_shared_tmp")

FAKE_UV = """#!/bin/bash
# Fake uv. Dispatches on the subcommand the deploy script actually issues and
# reports results the test fixture controls through FAKE_* environment values.
#
# Dispatch matches WHOLE ARGUMENTS, never a substring of the joined argv. A
# `*pytest*` glob also matches the `uv sync --project <tmp>` call, because
# pytest's own temporary directories are named `pytest-of-<user>/pytest-<n>`,
# so the sync would silently report the pytest fixture's exit code instead.
set -u
SUB="${1:-}"
WANT=""
LOCKED=""
PROJECT_DIR="$(pwd)"
PREV=""
for arg in "$@"; do
    case "${arg}" in
        pytest) WANT="pytest" ;;
        selfimprove) WANT="selfimprove" ;;
        --locked|--frozen) LOCKED="yes" ;;
    esac
    if [ "${PREV}" = "--project" ]; then PROJECT_DIR="${arg}"; fi
    PREV="${arg}"
done
# A stale committed lock: real uv re-resolves and REWRITES uv.lock unless told
# --locked (which refuses) or --frozen (which uses it as committed).
if [ "${FAKE_STALE_LOCK:-0}" = "1" ] && [ "${SUB}" = "run" -o "${SUB}" = "sync" ]; then
    if [ -n "${LOCKED}" ]; then
        echo "error: The lockfile at uv.lock needs to be updated, but --locked was provided." >&2
        exit 2
    fi
    printf 're-resolved\n' >> "${PROJECT_DIR}/uv.lock"
fi
case "${SUB}:${WANT}" in
    run:pytest)
        printf '%s\\n' "${FAKE_PYTEST_OUTPUT}"
        exit "${FAKE_PYTEST_RC}"
        ;;
    run:selfimprove)
        /usr/bin/python3 -c "
import json, os
pending = [name for name in os.environ['FAKE_PENDING'].split(',') if name]
print(json.dumps({'state': 'pending' if pending else 'current',
                  'database': '/invented/state.db',
                  'applied': ['0001_invented'],
                  'pending': pending,
                  'dry_run': True}))
"
        exit 0
        ;;
    sync:*)
        exit "${FAKE_SYNC_RC}"
        ;;
esac
echo "fake uv: unrecognized invocation: $*" >&2
exit 97
"""


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        [GIT, *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _commit(repo: Path, relative: str, text: str, message: str) -> str:
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", message, cwd=repo)
    return _git("rev-parse", "HEAD", cwd=repo)


@pytest.fixture
def topology(tmp_path: Path):
    """A bare origin, a production clone at its first commit, and a state dir.

    The production clone is one commit BEHIND origin/main, which is the ordinary
    situation the deployer exists to resolve.
    """
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    prod = tmp_path / "repo-prod"
    state = tmp_path / "state"
    state.mkdir()

    subprocess.run([GIT, "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run([GIT, "init", "-q", "-b", "main", str(work)], check=True)
    for key, value in (("user.email", "t@invented.test"), ("user.name", "Test")):
        _git("config", key, value, cwd=work)

    _commit(work, "README.md", "first\n", "first")
    _commit(work, "ops/deploy-prod.sh", SCRIPT.read_text(), "ship deploy script")
    base = _git("rev-parse", "HEAD", cwd=work)
    _git("remote", "add", "origin", str(origin), cwd=work)
    _git("push", "-q", "-u", "origin", "main", cwd=work)

    subprocess.run([GIT, "clone", "-q", str(origin), str(prod)], check=True)
    for key, value in (("user.email", "t@invented.test"), ("user.name", "Test")):
        _git("config", key, value, cwd=prod)

    candidate = _commit(work, "README.md", "second\n", "second")
    _git("push", "-q", "origin", "main", cwd=work)

    fake_uv = tmp_path / "fake-uv"
    fake_uv.write_text(FAKE_UV)
    fake_uv.chmod(0o755)

    return {
        "origin": origin,
        "work": work,
        "prod": prod,
        "state": state,
        "base": base,
        "candidate": candidate,
        "uv": fake_uv,
        "script": prod / "ops" / "deploy-prod.sh",
    }


def run_deploy(topology, **overrides):
    env = {
        "HOME": str(topology["state"].parent),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "SI_STATE_DIR": str(topology["state"]),
        "SI_UV": str(topology["uv"]),
        "SI_GIT": GIT,
        "SI_PYTHON3": PYTHON3,
        "SI_PATH_EXTRA": "/usr/bin",
        "FAKE_PYTEST_RC": "0",
        "FAKE_PYTEST_OUTPUT": "3454 passed, 12 warnings in 900.00s",
        "FAKE_PENDING": "",
        "FAKE_SYNC_RC": "0",
    }
    env.update({key: str(value) for key, value in overrides.items()})
    return subprocess.run(
        ["/bin/bash", str(topology["script"])],
        env=env,
        capture_output=True,
        text=True,
    )


def last_record(topology) -> dict:
    return json.loads((topology["state"] / "deploy" / "last-deploy.json").read_text())


def head(repo: Path) -> str:
    return _git("rev-parse", "HEAD", cwd=repo)


# ---------------------------------------------------------------------------
# Shipped artifacts
# ---------------------------------------------------------------------------


def test_script_exists_and_bash_syntax_ok():
    assert SCRIPT.is_file()
    result = subprocess.run(["/bin/bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, f"bash -n failed:\n{result.stderr}"


@pytest.mark.skipif(sys.platform != "darwin", reason="native launchd lint requires macOS")
def test_deploy_plist_lints():
    assert DEPLOY_PLIST.is_file()
    result = subprocess.run(
        ["/usr/bin/plutil", "-lint", str(DEPLOY_PLIST)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_rendered_plist_runs_the_production_copy_before_the_nightly(tmp_path):
    cfg = Config(state_dir=str(tmp_path / "state"), production_repo_path=str(tmp_path / "repo-prod"))
    content = plistlib.loads(launchd.render_deploy_plist(cfg).encode("utf-8"))
    assert content["Label"] == "com.tonygwu.self-improve.deploy"
    assert content["ProgramArguments"] == [
        "/bin/bash",
        str(tmp_path / "repo-prod" / "ops" / "deploy-prod.sh"),
    ]
    # Must precede the nightly's 02:30, or the deployed code waits a whole day.
    nightly = launchd.plist_content(cfg)["StartCalendarInterval"]
    deploy = content["StartCalendarInterval"]
    assert (deploy["Hour"], deploy["Minute"]) < (nightly["Hour"], nightly["Minute"])
    assert content["RunAtLoad"] is False
    assert content["StandardErrorPath"].endswith("deploy.stderr.log")


def test_deploy_and_nightly_agree_on_the_same_lock_path():
    """Both scripts must serialize against each other, not against two locks."""
    expected = '${SI_LOCK_DIR:-${STATE_DIR}/run.lock}'
    assert expected in SCRIPT.read_text()
    assert expected in NIGHTLY.read_text()


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_green_suite_fast_forwards_production(topology):
    assert head(topology["prod"]) == topology["base"]
    result = run_deploy(topology)
    assert result.returncode == 0, result.stdout + result.stderr
    assert head(topology["prod"]) == topology["candidate"]

    record = last_record(topology)
    assert record["status"] == "deployed"
    assert record["from_sha"] == topology["base"]
    assert record["to_sha"] == topology["candidate"]
    assert record["tests_summary"].startswith("3454 passed")
    assert record["started_at"].endswith("Z") and record["finished_at"].endswith("Z")
    # The production tree stays clean; deployment is a fast-forward, not an edit.
    assert _git("status", "--porcelain", cwd=topology["prod"]) == ""


def test_failing_suite_leaves_production_untouched(topology):
    result = run_deploy(topology, FAKE_PYTEST_RC="1", FAKE_PYTEST_OUTPUT="7 failed, 3447 passed")
    assert result.returncode != 0
    assert head(topology["prod"]) == topology["base"]
    record = last_record(topology)
    assert record["status"] == "blocked_tests_failed"
    assert record["to_sha"] == topology["candidate"]


def test_green_exit_without_any_passing_test_is_not_a_green_gate(topology):
    """pytest exits 0 when it collects nothing; that must not deploy."""
    result = run_deploy(topology, FAKE_PYTEST_RC="0", FAKE_PYTEST_OUTPUT="no tests ran in 0.01s")
    assert result.returncode != 0
    assert head(topology["prod"]) == topology["base"]
    assert last_record(topology)["status"] == "blocked_tests_failed"


def test_pending_migrations_block_the_deploy(topology):
    result = run_deploy(topology, FAKE_PENDING="0035_invented,0036_invented")
    assert result.returncode != 0
    assert head(topology["prod"]) == topology["base"]
    record = last_record(topology)
    assert record["status"] == "blocked_pending_migrations"
    assert "0035_invented" in record["detail"] and "0036_invented" in record["detail"]
    assert "upgrade-state --backup" in record["detail"]


def test_dirty_production_tree_refuses_before_touching_git(topology):
    (topology["prod"] / "README.md").write_text("edited by hand\n")
    result = run_deploy(topology)
    assert result.returncode != 0
    assert head(topology["prod"]) == topology["base"]
    assert last_record(topology)["status"] == "blocked_dirty_tree"
    assert (topology["prod"] / "README.md").read_text() == "edited by hand\n"


def test_already_current_is_a_quiet_success(topology):
    assert run_deploy(topology).returncode == 0
    second = run_deploy(topology)
    assert second.returncode == 0
    assert last_record(topology)["status"] == "noop_already_current"


def test_non_fast_forward_candidate_is_refused(topology):
    """A production checkout carrying a commit origin does not have."""
    _commit(topology["prod"], "local.md", "diverged\n", "local only")
    diverged = head(topology["prod"])
    result = run_deploy(topology)
    assert result.returncode != 0
    assert head(topology["prod"]) == diverged
    assert last_record(topology)["status"] == "blocked_not_fast_forward"


def test_live_pipeline_lock_defers_the_deploy(topology):
    lock = topology["state"] / "run.lock"
    lock.mkdir()
    (lock / "pid").write_text(str(os.getpid()))
    result = run_deploy(topology)
    # Exit 0: a night the pipeline is busy is not a deployment failure.
    assert result.returncode == 0
    assert head(topology["prod"]) == topology["base"]
    assert last_record(topology)["status"] == "failed_lock"
    assert lock.exists(), "a deferred deploy must not remove the holder's lock"


def test_stale_lock_is_reclaimed(topology):
    lock = topology["state"] / "run.lock"
    lock.mkdir()
    (lock / "pid").write_text("999999")
    result = run_deploy(topology)
    assert result.returncode == 0, result.stdout + result.stderr
    assert head(topology["prod"]) == topology["candidate"]
    assert not lock.exists(), "the lock must be released on exit"


def test_lock_is_released_after_a_blocked_deploy(topology):
    run_deploy(topology, FAKE_PYTEST_RC="1")
    assert not (topology["state"] / "run.lock").exists()


# ---------------------------------------------------------------------------
# The self-modification guard
# ---------------------------------------------------------------------------


def test_runs_from_a_temporary_copy_never_from_the_checkout_it_updates(topology):
    """The discriminating test for the re-exec guard.

    It pins the mechanism, not the corruption. Whether bash actually executes
    garbage when a script is rewritten under it depends on the file's size and
    on how much of it the shell has already buffered, so the corruption itself
    is not reliably reproducible in a test. What IS deterministic is that the
    running file must never be the one git is about to rewrite.
    """
    result = run_deploy(topology)
    assert result.returncode == 0, result.stdout + result.stderr
    executing = [line for line in result.stdout.splitlines() if line.startswith("deploy: executing ")]
    assert len(executing) == 1, result.stdout
    running_file = executing[0][len("deploy: executing ") :].split(" (production")[0]

    assert running_file != str(topology["script"])
    assert not Path(running_file).is_relative_to(topology["prod"])
    assert "si-deploy-prod" in Path(running_file).name
    assert str(topology["prod"]) in executing[0], "the production checkout is still reported"


def test_deploy_survives_a_candidate_that_rewrites_the_deploy_script(topology):
    """bash reads a script incrementally, so the fast-forward must not be able
    to rewrite the file the running shell is still reading. The candidate here
    replaces ops/deploy-prod.sh with a much shorter file, which without the
    re-exec guard makes bash resume at a byte offset past its new end."""
    _commit(
        topology["work"],
        "ops/deploy-prod.sh",
        "#!/bin/bash\necho replaced\n",
        "rewrite the deploy script",
    )
    _git("push", "-q", "origin", "main", cwd=topology["work"])
    candidate = _git("rev-parse", "HEAD", cwd=topology["work"])

    result = run_deploy(topology)
    assert result.returncode == 0, result.stdout + result.stderr
    assert head(topology["prod"]) == candidate
    record = last_record(topology)
    assert record["status"] == "deployed"
    assert record["to_sha"] == candidate
    # The new, shorter script is what is now on disk for the next run.
    assert (topology["prod"] / "ops" / "deploy-prod.sh").read_text() == "#!/bin/bash\necho replaced\n"


def test_every_ops_mktemp_template_is_portable_to_gnu():
    """GNU mktemp refuses a template without at least three trailing X's, so
    `mktemp -t si-deploy-prod` failed every deploy test on Linux while BSD
    mktemp on macOS accepted it (BL-65). Each mktemp call with arguments must
    pass an explicit template ending in XXXXXX, which both implementations
    accept. A bare `mktemp` takes no template and is portable as written."""
    import re
    import shlex

    calls = []
    for script in sorted((REPO / "ops").glob("*.sh")):
        for number, line in enumerate(script.read_text().splitlines(), 1):
            # A comment starts at a word-initial #, so $# and ${#x} stay code.
            code = re.sub(r"(^|\s)#.*$", "", line)
            for match in re.finditer(r"\$\(\s*(?:/usr/bin/)?mktemp\b([^)]*)\)", code):
                calls.append((script.name, number, shlex.split(match.group(1))))
    deploy_calls = [call for call in calls if call[0] == "deploy-prod.sh"]
    assert deploy_calls, "the deploy script's self-copy mktemp call was not found"
    assert any(not args for name, _, args in calls if name == "sabotage.sh"), "the bare mktemp was not found"
    for name, number, args in calls:
        if not args or args == ["-d"]:
            continue  # no template: both implementations pick their own name
        template = args[-1]
        assert not template.startswith("-"), f"{name}:{number} passes no template: {args}"
        assert template.endswith("XXXXXX"), f"{name}:{number} template {template!r} is not GNU-portable"


def test_temporary_self_copy_is_removed(topology, tmp_path):
    before = set(Path("/tmp").glob("si-deploy-prod*")) | set(
        Path(os.environ.get("TMPDIR", "/tmp")).glob("si-deploy-prod*")
    )
    run_deploy(topology)
    after = set(Path("/tmp").glob("si-deploy-prod*")) | set(
        Path(os.environ.get("TMPDIR", "/tmp")).glob("si-deploy-prod*")
    )
    assert after == before


# ---------------------------------------------------------------------------
# Staging isolation and reporting
# ---------------------------------------------------------------------------


def test_tests_run_in_a_separate_stage_not_in_production(topology):
    run_deploy(topology)
    stage = topology["prod"].parent / ".deploy-stage"
    assert (stage / ".git").is_dir()
    assert stage.resolve() != topology["prod"].resolve()
    assert _git("rev-parse", "HEAD", cwd=stage) == topology["candidate"]


def test_stage_is_never_placed_inside_the_state_directory(topology):
    """The stage is a checkout the suite runs IN, so it must not sit under a
    root the suite's own hermeticity guard forbids.

    tests/conftest.py forbids reads under `~/.self-improve` by prefix. A stage
    inside the state directory therefore makes every test that reads its own
    source tree fail. That is not hypothetical: the first real deploy on
    2026-09-20 reported `1 failed, 3463 passed, 975 errors` for exactly this
    reason, and correctly refused to move repo-prod.
    """
    run_deploy(topology)
    stage = topology["prod"].parent / ".deploy-stage"
    assert (stage / ".git").is_dir(), "the default stage must still be created"
    assert not stage.resolve().is_relative_to(topology["state"].resolve())
    assert not (topology["state"] / "deploy" / "stage").exists()

    # Load the suite's own guard by path; "conftest" is not importable by name.
    spec = importlib.util.spec_from_file_location(
        "_deploy_conftest", Path(__file__).resolve().parent / "conftest.py"
    )
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)

    assert any(str(root).endswith(".self-improve") for root in guard._FORBIDDEN), (
        "conftest no longer forbids the state directory; re-check where the "
        "stage may live before relaxing this test"
    )


def test_stage_pointing_at_another_repository_is_refused(topology, tmp_path):
    other = tmp_path / "other.git"
    subprocess.run([GIT, "init", "-q", "--bare", "-b", "main", str(other)], check=True)
    stage = topology["prod"].parent / ".deploy-stage"
    stage.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([GIT, "clone", "-q", str(topology["origin"]), str(stage)], check=True)
    _git("remote", "set-url", "origin", str(other), cwd=stage)

    result = run_deploy(topology)
    assert result.returncode != 0
    assert head(topology["prod"]) == topology["base"]
    assert last_record(topology)["status"] == "failed_stage"


def test_history_records_every_attempt_with_a_closed_status_vocabulary(topology):
    run_deploy(topology, FAKE_PYTEST_RC="1")
    run_deploy(topology, FAKE_PENDING="0035_invented")
    run_deploy(topology)
    run_deploy(topology)

    lines = (topology["state"] / "deploy" / "history.jsonl").read_text().splitlines()
    statuses = [json.loads(line)["status"] for line in lines]
    assert statuses == [
        "blocked_tests_failed",
        "blocked_pending_migrations",
        "deployed",
        "noop_already_current",
    ]
    known = {
        "deployed", "noop_already_current", "blocked_dirty_tree",
        "blocked_not_fast_forward", "blocked_tests_failed",
        "blocked_pending_migrations", "failed_lock", "failed_precondition",
        "failed_stage", "failed_merge",
    }
    assert set(statuses) <= known
    for line in lines:
        record = json.loads(line)
        assert set(record) == {
            "started_at", "finished_at", "status", "detail", "exit_code",
            "prod_repo", "from_sha", "to_sha", "tests_summary",
        }


def test_dependency_sync_failure_does_not_roll_back_tested_code(topology):
    """The code is already the code that passed. A resolve failure is reported,
    and the nightly's own `uv run` retries it."""
    result = run_deploy(topology, FAKE_SYNC_RC="1")
    assert result.returncode == 0
    assert head(topology["prod"]) == topology["candidate"]
    assert "WARNING: uv sync" in result.stdout
    assert last_record(topology)["status"] == "deployed"


def test_rejects_arguments(topology):
    env = dict(os.environ)
    env.update({"SI_STATE_DIR": str(topology["state"]), "SI_UV": str(topology["uv"])})
    result = subprocess.run(
        ["/bin/bash", str(topology["script"]), "--review-only"],
        env=env, capture_output=True, text=True,
    )
    assert result.returncode == 2
    assert "usage:" in result.stderr


def test_a_stale_committed_lock_blocks_the_deploy_and_never_dirties_production(topology):
    """BL-28: uv run and uv sync ran without --locked, so a stale lock was
    re-resolved for the gate and rewritten in repo-prod after the clean check,
    blocking every later deploy at blocked_dirty_tree."""
    result = run_deploy(topology, FAKE_STALE_LOCK="1")
    assert result.returncode != 0
    assert head(topology["prod"]) == topology["base"]
    assert _git("status", "--porcelain", cwd=topology["prod"]) == ""
    assert last_record(topology)["status"] == "blocked_tests_failed"


def test_the_nightly_runs_the_pipeline_from_the_committed_lock():
    script = (Path(__file__).resolve().parents[1] / "ops" / "run-nightly.sh").read_text()
    calls = [line for line in script.splitlines() if '"${UV}" run' in line]
    assert calls and all("--locked" in line for line in calls), calls


def test_a_lock_naming_a_reused_pid_is_reclaimed(topology):
    """BL-47: kill -0 on the recorded PID trusted any live process, so a PID
    reused after a crash made the deploy (and the nightly) skip every night.
    The lock also records the holder's start time; a mismatch is stale."""
    lock = topology["state"] / "run.lock"
    lock.mkdir()
    (lock / "pid").write_text(str(os.getpid()))
    (lock / "started").write_text("Thu Jan  1 00:00:00 1970\n")
    result = run_deploy(topology)
    assert result.returncode == 0, result.stdout + result.stderr
    assert head(topology["prod"]) == topology["candidate"]


def test_a_lock_written_before_the_utc_format_is_still_respected(topology):
    """A lock taken by the previous script holds lstart in the writer's local
    format. Reading it as stale would remove a live holder's lock once, on the
    first run after deployment, so the checker also accepts that format."""
    lock = topology["state"] / "run.lock"
    lock.mkdir()
    (lock / "pid").write_text(str(os.getpid()))
    local = {"PATH": "/usr/bin:/bin", "TZ": "Asia/Tokyo"}
    started = subprocess.run(["/bin/ps", "-o", "lstart=", "-p", str(os.getpid())],
                             capture_output=True, text=True, check=True, env=local).stdout
    (lock / "started").write_text(started)
    result = run_deploy(topology, TZ="Asia/Tokyo")
    assert result.returncode == 0
    assert last_record(topology)["status"] == "failed_lock"
    assert lock.exists()


@pytest.mark.parametrize("tz", ["", "Asia/Tokyo"])
def test_a_lock_whose_holder_start_time_matches_is_respected(topology, tz):
    """Review of BL-47: the start time is read in C locale and UTC, so a lock
    taken under one TZ is not mistaken for stale under another."""
    lock = topology["state"] / "run.lock"
    lock.mkdir()
    (lock / "pid").write_text(str(os.getpid()))
    started = subprocess.run(["/bin/ps", "-o", "lstart=", "-p", str(os.getpid())],
                             capture_output=True, text=True, check=True,
                             env={"PATH": "/usr/bin:/bin", "LC_ALL": "C", "TZ": "UTC"}).stdout
    (lock / "started").write_text(started)
    result = run_deploy(topology, **({"TZ": tz} if tz else {}))
    assert result.returncode == 0
    assert last_record(topology)["status"] == "failed_lock"
    assert lock.exists()
