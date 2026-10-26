"""Fail when a web reader walks repositories instead of reading retained rows."""

from __future__ import annotations

import os
import pathlib


def forbid_live_walks(monkeypatch) -> None:
    """Block the mechanisms a context walk needs: directory listing and the walker.

    Apply it after the app is created, so that only request handling is guarded.
    Module imports are unaffected: the import system lists directories through
    ``posix`` directly, not through these ``os`` and ``pathlib`` names.
    """
    from self_improve.dashboard import context_weight as walker

    def forbidden(*args, **kwargs):
        raise AssertionError("Web reader attempted a live context walk")

    for name in ("walk", "scandir", "listdir"):
        monkeypatch.setattr(os, name, forbidden)
    for name in ("iterdir", "glob", "rglob"):
        monkeypatch.setattr(pathlib.Path, name, forbidden)
    monkeypatch.setattr(walker, "context_weight", forbidden)
