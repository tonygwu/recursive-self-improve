"""Tests for the guard in conftest.py that keeps tests off the real ~/.claude.

Meta, but the guard is load-bearing: it caught nothing when first installed
(the suite was already clean), and a guard that has never fired is
indistinguishable from a guard that cannot fire.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import tests.conftest as guard


def test_forbidden_roots_cover_every_config_tree():
    """If a new source tree is added to Config, it must be listed here too."""
    home = Path.home()
    names = {p.name for p in guard._FORBIDDEN}
    assert {".claude", ".claude-b", ".claude-c", ".codex", ".self-improve"} <= names
    assert all(p.is_relative_to(home) for p in guard._FORBIDDEN)


def test_the_model_cache_is_deliberately_allowed():
    """model2vec weights live under ~/.cache and every embedding test loads them.

    Forbidding that would make the guard fire on legitimate work, and a guard
    that cries wolf gets disabled.
    """
    assert not any(p.name == ".cache" for p in guard._FORBIDDEN)


def test_the_hook_records_a_read_under_a_forbidden_root(monkeypatch):
    """Drive the audit callback directly rather than waiting for a real leak."""
    monkeypatch.setattr(guard, "_armed", True)
    guard._violations.clear()
    guard._audit("open", (str(Path.home() / ".claude" / "CLAUDE.md"), "r", 0))
    assert guard._violations == [str(Path.home() / ".claude" / "CLAUDE.md")]
    guard._violations.clear()


def test_the_hook_ignores_unrelated_paths(monkeypatch):
    monkeypatch.setattr(guard, "_armed", True)
    guard._violations.clear()
    guard._audit("open", ("/tmp/whatever.jsonl", "r", 0))
    guard._audit("close", ("/anything",))
    guard._audit("open", (7, "r", 0))  # a file descriptor, not a path
    assert guard._violations == []


@pytest.mark.reads_real_home
def test_the_marker_lets_a_test_opt_out():
    """The escape hatch must work, or someone will delete the guard instead."""
    p = Path.home() / ".claude"
    if p.is_dir():
        list(p.iterdir())  # a real read under a forbidden root
    assert True
