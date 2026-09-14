"""Verify each access mechanism covered by the personal-resource guard.
SQLite emits sqlite3.connect instead of open. The guard must stop database
construction before migrations or queries can access personal state.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import tests.conftest as conftest


def _capture(event: str, arg) -> list[str]:
    """Run the guard's own hook for one event and return what it recorded."""
    before = list(conftest._violations)
    conftest._violations.clear()
    was_armed = conftest._armed
    conftest._armed = True
    try:
        conftest._audit(event, (arg,))
        return list(conftest._violations)
    finally:
        conftest._armed = was_armed
        conftest._violations.clear()
        conftest._violations.extend(before)


LIVE_DB = str(Path.home() / ".self-improve" / "state.db")


def test_the_guard_catches_a_plain_open_of_the_live_state_dir():
    """The behaviour that already worked, pinned so the fix cannot lose it."""
    assert _capture("open", LIVE_DB) == [LIVE_DB]


def test_the_guard_catches_a_sqlite_connect_to_the_live_state_db():
    """The hole. sqlite3 never raises `open`, so this was invisible.

    Sabotage: restore `if event != "open" or not _armed: return`. This fails.
    """
    assert _capture("sqlite3.connect", LIVE_DB) == [LIVE_DB], (
        "a test could open the operator's live state.db and the hermeticity "
        "guard would not notice"
    )


def test_the_guard_catches_a_read_only_uri_connect_too():
    """A read-only SQLite URI still targets a forbidden personal resource."""
    uri = f"file:{LIVE_DB}?mode=ro"
    # The violation is recorded as the PATH, not the raw URI: the scheme and
    # query are stripped so the report names the same artefact an `open`
    # violation would, and so the prefix match against _FORBIDDEN works at all.
    assert _capture("sqlite3.connect", uri) == [LIVE_DB]


def test_an_innocent_path_is_still_ignored(tmp_path):
    assert _capture("sqlite3.connect", str(tmp_path / "scratch.db")) == []
    assert _capture("open", str(tmp_path / "scratch.txt")) == []


def test_a_non_path_argument_does_not_crash_the_hook():
    """sqlite3.connect can be handed a fd or an object; the hook must survive."""
    assert _capture("sqlite3.connect", 7) == []
    assert _capture("sqlite3.connect", None) == []


def test_the_guard_is_wired_to_the_real_sqlite3_module(tmp_path):
    """Not just the callback: the audit event name must be the real one.

    Asserting on the hook in isolation would pass even if sqlite3 raised some
    other event. Drive the real library and confirm the guard hears it.
    """
    seen: list[tuple[str, str]] = []

    def spy(event, args):
        if event == "sqlite3.connect":
            seen.append((event, str(args[0])))

    import sys

    sys.addaudithook(spy)
    con = sqlite3.connect(tmp_path / "probe.db")
    con.close()
    assert seen, "sqlite3.connect did not raise the audit event this guard relies on"


# ---------------------------------------------------------------------------
# The forbidden list must cover accounts nobody has created yet
# ---------------------------------------------------------------------------


def test_every_account_config_dir_is_forbidden_including_future_ones():
    """Prefix matching must cover future account directories without enumeration."""
    for letter in ("b", "c", "d", "e", "z"):
        target = str(LIVE_DB.replace(".self-improve/state.db", f".claude-{letter}/projects/x"))
        assert _capture("open", target) == [target], (
            f"a test could read ~/.claude-{letter} unnoticed"
        )


def test_the_default_config_dir_is_forbidden_too():
    from pathlib import Path

    target = str(Path.home() / ".claude" / "projects" / "x.jsonl")
    assert _capture("open", target) == [target]


def test_an_unrelated_home_directory_is_not_forbidden(tmp_path):
    """Over-blocking everything would make the guard useless by being ignored."""
    from pathlib import Path

    assert _capture("open", str(Path.home() / "Code" / "something")) == []


# ---------------------------------------------------------------------------
# The third way to touch a path: subprocess
# ---------------------------------------------------------------------------
#
# apply.py drives git through subprocess. Neither `open` nor `sqlite3.connect`
# fires for that, so a test that ran `git -C ~/.claude ...` would have written
# to the operator's real tree with this guard silent — the same shape as the
# sqlite3 hole above.
#
# Measured before changing anything: across the whole suite, ZERO subprocess
# launches name a path under a forbidden root, so closing this costs nothing.

LIVE_CLAUDE = str(Path.home() / ".claude")


def _capture_subprocess(argv, cwd=None) -> list[str]:
    """Run the guard's hook for one subprocess.Popen event."""
    before = list(conftest._violations)
    conftest._violations.clear()
    was_armed = conftest._armed
    conftest._armed = True
    try:
        # (executable, args, cwd, env) — cwd is index 2. Verified against a
        # real subprocess.run rather than assumed; the first version of the
        # guard read index 3, which is env, and caught nothing.
        conftest._audit("subprocess.Popen", ("/usr/bin/git", argv, cwd, None))
        return list(conftest._violations)
    finally:
        conftest._armed = was_armed
        conftest._violations.clear()
        conftest._violations.extend(before)


def test_the_guard_catches_git_run_inside_the_operators_real_tree():
    """`git -C ~/.claude ...` is the exact call apply.py makes, pointed at the
    one tree no test may write to."""
    assert _capture_subprocess(["git", "-C", LIVE_CLAUDE, "commit"]) == [LIVE_CLAUDE]


def test_the_guard_catches_a_forbidden_cwd_even_with_innocent_args():
    """cwd is how `_run_git` actually targets a repo; the argv can be clean."""
    assert _capture_subprocess(["git", "status"], cwd=LIVE_CLAUDE) == [LIVE_CLAUDE]


def test_the_guard_ignores_a_subprocess_that_names_no_forbidden_path(tmp_path):
    """Narrowness. The suite runs git in tmp_path constantly; flagging that
    would make the guard useless within a day."""
    assert _capture_subprocess(["git", "-C", str(tmp_path), "init"]) == []


def test_running_a_binary_under_a_forbidden_root_is_also_caught(tmp_path):
    """The guard inspects argv[0] for forbidden paths, like other arguments."""
    exe = str(Path.home() / ".claude" / "local" / "claude")
    # This invented executable path exercises argv[0] inspection.
    # The test inspects the arguments without executing the binary.
    assert _capture_subprocess([exe, "--version"]) == [exe]


# ---------------------------------------------------------------------------
# The fourth door: the directory verbs
# ---------------------------------------------------------------------------
#
# Found by ops/sabotage.sh: disabling the `_FS_EVENTS` branch failed NOTHING.
# The branch was covered only indirectly — a test that leaked would error
# through the autouse fixture — so nothing named it and nothing would go red
# if it were deleted. That is the same hole this whole file exists to close.

LIVE_STATE = str(Path.home() / ".self-improve" / "evals" / "regression")


def _capture_fs(event: str, *args) -> list[str]:
    before = list(conftest._violations)
    conftest._violations.clear()
    was_armed = conftest._armed
    conftest._armed = True
    try:
        conftest._audit(event, args)
        return list(conftest._violations)
    finally:
        conftest._armed = was_armed
        conftest._violations.clear()
        conftest._violations.extend(before)


def test_the_guard_catches_a_mkdir_in_the_live_state_tree():
    """The mkdir audit event must reject a directory inside live state."""
    assert _capture_fs("os.mkdir", LIVE_STATE, 0o777) == [LIVE_STATE]


def test_the_guard_catches_a_delete_and_a_rename_too():
    """`open` fires for neither. A test that deleted the operator's state
    would have been as invisible as one that created it."""
    live_db = str(Path.home() / ".self-improve" / "state.db")
    assert _capture_fs("os.remove", live_db) == [live_db]
    # rename carries src and dst; either side under a forbidden root counts.
    assert _capture_fs("os.rename", "/tmp/x", live_db) == [live_db]
    assert _capture_fs("shutil.rmtree", LIVE_STATE) == [LIVE_STATE]


def test_the_fs_branch_ignores_paths_outside_the_forbidden_roots(tmp_path):
    """Narrowness: the suite makes directories constantly."""
    assert _capture_fs("os.mkdir", str(tmp_path / "anything")) == []
