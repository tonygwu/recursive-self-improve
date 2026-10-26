"""Reject test access to personal configuration, transcripts, and state.

Config resolves HOME at import time. Changing an environment variable later
does not redirect its defaults, so tests must supply temporary resources.
An audit hook checks the actual file, SQLite, subprocess, and directory access
paths and names any forbidden resource. This prevents personal contents from
silently changing test results, including making an invalid assertion pass.

The explicit ``reads_real_home`` marker opts out of this guard when a separately
authorized test requires personal resources.
"""

from __future__ import annotations

import sys
import os
from pathlib import Path

import pytest

_REAL_HOME = Path.home()
#: Trees a test must never read. Deliberately NOT ~/.cache: the model2vec
#: weights live there and every embedding test legitimately loads them.
#:
#: Matching is by string PREFIX, and that is load-bearing rather than
#: incidental: the `.claude` entry therefore also covers `~/.claude-b`,
#: `-c`, `-d` and any future account dir, plus `.claude.json`. Do NOT
#: change this into exact-match or into an enumerated list of account letters.
#: The entries below for -b and -c are redundant under the prefix rule and
#: are kept only because they document what they are.
_FORBIDDEN = (
    _REAL_HOME / ".claude",
    _REAL_HOME / ".claude-b",
    _REAL_HOME / ".claude-c",
    _REAL_HOME / ".codex",
    _REAL_HOME / ".self-improve",
)

# Native system policy is a separate resource from personal configuration.
_SYSTEM_POLICY_ROOTS = ("/Library/Application Support/ClaudeCode", "/etc/claude-code", "C:/Program Files/ClaudeCode")

_violations: list[str] = []
_armed = False


#: SQLite emits sqlite3.connect rather than open. Inspect both events so
#: constructing a Store cannot bypass the guard before its first query.
_PATH_EVENTS = frozenset({"open", "sqlite3.connect"})

#: The third way to touch a path, and the one neither event above can see.
#: apply.py drives git through subprocess, so a test that ran git inside
#: ~/.claude would write to the operator's real tree with this guard silent.
#: Same shape as the sqlite3 hole: the mechanism did not match the intent.
#: Audit args are (executable, args, cwd, env) — cwd is index 2, VERIFIED
#: empirically rather than from memory. The first version of this read index
#: 3, which is `env`, so the cwd branch caught nothing and the survey that
#: said "no test trips this" was partly surveying the wrong slot.
_SUBPROCESS_EVENT = "subprocess.Popen"

#: Directory creation, removal, renaming, and listing do not emit open events.
#: Guard their audit events too. These events carry paths as positional
#: arguments, so the same prefix check protects personal directories.
_FS_EVENTS = frozenset({
    "os.mkdir", "os.rmdir", "os.remove", "os.rename", "os.replace",
    "os.symlink", "os.link", "os.truncate", "os.scandir", "os.listdir",
    "shutil.rmtree", "shutil.copyfile", "shutil.move",
})


def _system_policy_path(text) -> bool:
    normalized = os.fsdecode(text).replace("\\", "/").casefold()
    return any(normalized.startswith(root.replace("\\", "/").casefold()) for root in _SYSTEM_POLICY_ROOTS)


def _path_text(value) -> str:
    """A path argument as text: bytes and PathLike decode, never str(b'...')."""
    if isinstance(value, (bytes, os.PathLike)):
        return os.fsdecode(value)
    return str(value)


def _forbidden_prefix(text) -> bool:
    text = _path_text(text)
    return any(text.startswith(str(root)) for root in _FORBIDDEN) or _system_policy_path(text)


def _audit(event: str, args) -> None:
    if not _armed:
        return
    if event == _SUBPROCESS_EVENT:
        argv = args[1] if len(args) > 1 else None
        parts: list[str] = []
        if isinstance(argv, (list, tuple)):
            parts.extend(_path_text(a) for a in argv)
        elif isinstance(argv, (str, bytes)):
            parts.append(_path_text(argv))
        cwd = args[2] if len(args) > 2 else None
        if cwd:
            parts.append(_path_text(cwd))
        for part in parts:
            if _forbidden_prefix(part):
                _violations.append(part)
                return
        return
    if event in _FS_EVENTS:
        for arg in args:
            if isinstance(arg, (str, bytes, Path)) and _forbidden_prefix(arg):
                _violations.append(_path_text(arg))
                return
        return
    if event not in _PATH_EVENTS:
        return
    try:
        path = args[0]
    except (IndexError, TypeError):
        return
    if not isinstance(path, (str, bytes, Path)):
        return  # a file descriptor or a connection object, not a path
    text = _path_text(path)
    # sqlite3 accepts a URI: file:/abs/path?mode=ro, or Path.as_uri()'s
    # file:///abs%23path?mode=ro. Strip the scheme, an empty or localhost
    # authority and the query, and percent-decode, so the prefix comparison
    # below sees the same string `open` would.
    if text.startswith("file:"):
        from urllib.parse import unquote
        text = text[len("file:"):].split("?", 1)[0]
        if text.startswith("//"):
            authority, _, rest = text[2:].partition("/")
            text = "/" + rest if authority in ("", "localhost") else text
        text = unquote(text)
    # Fast reject before touching the filesystem: str comparison only.
    if _forbidden_prefix(text):
        _violations.append(text)


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "reads_real_home: test legitimately reads the real ~/.claude or ~/.codex",
    )
    sys.addaudithook(_audit)


@pytest.fixture(autouse=True)
def _no_real_home_reads(request, monkeypatch):
    """Fail any test that touched the operator's real config trees."""
    global _armed
    if request.node.get_closest_marker("reads_real_home"):
        yield
        return
    # stat/lstat do not emit Python audit events. Refuse system-policy metadata
    # reads before they occur, even when the default path happens not to exist.
    for name in ('stat', 'lstat'):
        original = getattr(os, name)
        def checked(path, *args, _original=original, **kwargs):
            if isinstance(path, (str, bytes, Path)) and _system_policy_path(path):
                raise AssertionError('Test attempted native system policy access: '+str(path))
            return _original(path, *args, **kwargs)
        monkeypatch.setattr(os, name, checked)
    _violations.clear()
    _armed = True
    try:
        yield
    finally:
        _armed = False
        hits = list(_violations)
        _violations.clear()
    if hits:
        shown = "\n  ".join(sorted(set(hits))[:5])
        raise AssertionError(
            f"test read {len(set(hits))} path(s) under the real home:\n  {shown}\n"
            "Redirect every Config path into tmp_path (global_claude_md, "
            "skills_dir, codex_global_agents_md, state_dir, claude_projects_dir, "
            "codex_sessions_dir, codex_archived_dir). A test that reads this "
            "machine passes or fails for reasons nobody else can reproduce."
        )
