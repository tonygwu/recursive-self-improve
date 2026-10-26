"""Working directories that are never instruction sources or targets.

``cfg.instruction_excluded_dirs`` names absolute directories. A session whose
recorded working directory is one of them, or lies below one, stays in the
Store and is still scanned and mined; ``denylist_substrings`` governs that.
What changes is that no reader derives instruction files from that directory:
its AGENTS.md, CLAUDE.md and other instruction surfaces are never opened, it is
not inventoried, routing refuses it, and recovery offers no destination there.
A proposal created before the exclusion still names its old target, and the
writer does not check this setting, so such a proposal could still be approved
and written.

Matching is lexical. The path is normalized but symlinks are not resolved,
because resolving would touch the excluded directory, which is the access this
setting exists to avoid (a macOS CloudStorage folder refuses it under launchd).
A working directory reached through a symlink into an excluded tree, or spelled
with different letter case, is therefore not excluded.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import PurePath

from .config import Config


def excluded_by(cfg: Config, path: str) -> str:
    """The configured entry that excludes ``path``, or '' when none does."""
    if not path:
        return ""
    candidate = PurePath(os.path.normpath(path))
    for entry in cfg.instruction_excluded_dirs:
        if candidate.is_relative_to(os.path.normpath(entry)):
            return entry
    return ""


def included(cfg: Config, paths: Iterable[str]) -> list[str]:
    """``paths`` without the ones an excluded directory contains, order kept."""
    return [p for p in paths if not excluded_by(cfg, p)]
