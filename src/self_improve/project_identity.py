"""Resolve a session's cwd to canonical project identity.

One repository can have multiple working copies and remote URL spellings.
Counting paths as independent projects would inflate evidence breadth and can
change global routing decisions. ``scope_guess == "global"`` is a separate
routing path; canonical counts alone do not enforce breadth on that path.

Prefer the host's numeric repository ID, which survives renames. URL fallback
collapses clones with the same normalized remote but may split after a rename.
Resolution order is recorded in ``ProjectIdentity.method``:

1. ``gh_repo_id``: ``github:<numeric id>``
2. ``remote_url``: ``remote:<normalized url>``
3. ``git_root``: ``path:<realpath of root>`` for a repository with no remote
4. ``path``: ``path:<realpath>`` for a directory outside Git
5. ``unresolved``: ``unresolved:<raw path>`` for a missing or empty path

Keep unresolved paths distinct rather than inventing a shared project. Failed
resolution degrades to the next method with evidence, rather than aborting the scan.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

METHOD_GH_REPO_ID = "gh_repo_id"
METHOD_REMOTE_URL = "remote_url"
METHOD_GIT_ROOT = "git_root"
METHOD_PATH = "path"
METHOD_UNRESOLVED = "unresolved"

#: Ordered best -> worst; the run report renders counts in this order.
METHODS = (
    METHOD_GH_REPO_ID,
    METHOD_REMOTE_URL,
    METHOD_GIT_ROOT,
    METHOD_PATH,
    METHOD_UNRESOLVED,
)

_GIT_TIMEOUT_S = 5
_GH_TIMEOUT_S = 10

# scp-like: git@host:owner/name(.git)
_SCP_RE = re.compile(r"^(?:(?P<user>[^@/]+)@)?(?P<host>[^:/]+):(?P<path>.+)$")
# url-like: scheme://[user@]host/owner/name(.git)
_URL_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://(?:[^@/]+@)?(?P<host>[^:/]+)(?::\d+)?/(?P<path>.+)$")


@dataclass(frozen=True)
class ProjectIdentity:
    """A stable grouping key, a human name, and how we got there."""

    key: str
    display: str
    method: str


def normalize_remote_url(url: str) -> str:
    """``host/owner/name`` for every spelling of one remote.

    Handles scp-like (``git@github.com:o/n.git``), url-like
    (``https://github.com/o/n``), and ``ssh://git@github.com/o/n.git``.
    Returns "" for anything unrecognizable rather than guessing.
    """
    url = (url or "").strip()
    if not url:
        return ""
    m = _URL_RE.match(url) or _SCP_RE.match(url)
    if not m:
        return ""
    host = m.group("host").lower()
    path = m.group("path").strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    return f"{host}/{path}" if path else ""


def _run_git(cwd: str, *args: str) -> str:
    try:
        out = subprocess.run(
            ("git", *args),
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def _default_gh(owner_repo: str) -> dict:
    """``gh api repos/<owner>/<name>`` -> the parsed JSON (raises on failure).

    Callers pass their own ``gh`` in tests; this is the only place that would
    touch the network, and it is never reached when ``gh`` is injected.
    """
    out = subprocess.run(
        ("gh", "api", f"repos/{owner_repo}"),
        capture_output=True,
        text=True,
        timeout=_GH_TIMEOUT_S,
    )
    if out.returncode != 0:
        raise RuntimeError(f"gh api repos/{owner_repo} failed: {out.stderr.strip()[:200]}")
    return json.loads(out.stdout)


def _github_owner_repo(normalized: str) -> str:
    """``owner/name`` iff this normalized remote is on github.com, else ""."""
    parts = normalized.split("/")
    if len(parts) == 3 and parts[0] == "github.com":
        return f"{parts[1]}/{parts[2]}"
    return ""


def _remember(path_cache, raw, identity):
    """Populate the path cache and return the identity (used at every exit)."""
    if path_cache is not None:
        path_cache[raw] = identity
    return identity


def resolve(
    project_path: str,
    *,
    gh=None,
    cache: dict | None = None,
    path_cache: dict | None = None,
    use_gh: bool = True,
) -> ProjectIdentity:
    """Canonical identity for one session's cwd.

    Two caches, because they save different things and are consulted at
    different depths:

    * ``path_cache`` is keyed by the raw cwd and is checked FIRST, before any
      subprocess runs. Without it every session pays ``git rev-parse`` plus
      ``git remote get-url``. The remote cache below cannot avoid those calls
      because it is only reachable once both have returned. In-memory and
      per-run: a directory can change remote between runs.
    * ``cache`` is keyed by normalized remote URL and saves the ``gh`` network
      call across repositories AND across runs (persisted in
      ``project_identity_cache``).

    Pass the same dicts across a whole scan.
    """
    raw = (project_path or "").strip()
    if path_cache is not None and raw in path_cache:
        return path_cache[raw]
    if not raw:
        return _remember(path_cache, raw, ProjectIdentity("unresolved:", "(unknown)", METHOD_UNRESOLVED))

    # Resolve compatibility symlinks before identifying the checkout.
    try:
        real = os.path.realpath(raw)
    except OSError:
        real = raw
    if not Path(real).is_dir():
        return _remember(
            path_cache, raw,
            ProjectIdentity(f"unresolved:{raw}", Path(raw).name or raw, METHOD_UNRESOLVED),
        )

    root = _run_git(real, "rev-parse", "--show-toplevel")
    if not root:
        return _remember(path_cache, raw, ProjectIdentity(f"path:{real}", Path(real).name, METHOD_PATH))
    root = os.path.realpath(root)

    normalized = normalize_remote_url(_run_git(root, "remote", "get-url", "origin"))
    if not normalized:
        return _remember(path_cache, raw, ProjectIdentity(f"path:{root}", Path(root).name, METHOD_GIT_ROOT))

    fallback = ProjectIdentity(
        f"remote:{normalized}", normalized.split("/")[-1], METHOD_REMOTE_URL
    )

    if not use_gh:
        return _remember(path_cache, raw, fallback)
    if cache is not None and normalized in cache:
        return _remember(path_cache, raw, cache[normalized])

    owner_repo = _github_owner_repo(normalized)
    identity = fallback
    if owner_repo:
        try:
            data = (gh or _default_gh)(owner_repo)
            repo_id = data["id"]
            identity = ProjectIdentity(
                f"github:{repo_id}",
                str(data.get("full_name") or normalized),
                METHOD_GH_REPO_ID,
            )
        except Exception as exc:
            # Degrade, never raise: no gh, no network, private repo, rate
            # limit. The method field records THAT we degraded; this logs WHY,
            # because "gh is not installed", "gh is not authenticated" and
            # "rate limited" need different fixes and are indistinguishable
            # from the method field alone. Once per repository, not per
            # session — the cache below sees to that.
            logger.warning(
                "project identity: gh lookup failed for %s (%s: %s); falling "
                "back to the remote URL, which will split if the repo is "
                "renamed",
                owner_repo,
                type(exc).__name__,
                str(exc)[:160],
            )
            identity = fallback

    if cache is not None:
        cache[normalized] = identity
    return _remember(path_cache, raw, identity)
