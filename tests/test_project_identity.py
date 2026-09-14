"""Canonical project identity: multiple clones count as one repository.

A raw project_path names a working directory. Counting those paths as projects
can inflate the evidence used by the project-count promotion path. Resolution
must preserve repository identity across clones, aliases, and renames.

Tests inject command runners without invoking GitHub or the network."""

from __future__ import annotations

import json
import subprocess

import pytest

from self_improve.project_identity import (
    METHOD_GH_REPO_ID,
    METHOD_GIT_ROOT,
    METHOD_PATH,
    METHOD_REMOTE_URL,
    METHOD_UNRESOLVED,
    ProjectIdentity,
    normalize_remote_url,
    resolve,
)


def git(path, *args):
    subprocess.run(("git", *args), cwd=path, check=True, capture_output=True)


def make_repo(path, remote=None):
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q")
    if remote:
        git(path, "remote", "add", "origin", remote)
    return path


class FakeGh:
    """Stand-in for `gh api repos/<owner>/<name>`.

    Real GitHub resolves a RENAMED repo's old name to its new full_name and a
    numeric id that never changes — that is the whole reason the id is the key.
    """

    def __init__(self, mapping=None, fail=False):
        self.mapping = mapping or {}
        self.fail = fail
        self.calls = []

    def __call__(self, owner_repo: str):
        self.calls.append(owner_repo)
        if self.fail or owner_repo not in self.mapping:
            raise RuntimeError("gh unavailable")
        return self.mapping[owner_repo]


# ----------------------------------------------------------------------
# url normalization
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "git@github.com:example/demo-service.git",
        "git@github.com:example/demo-service",
        "https://github.com/example/demo-service.git",
        "https://github.com/example/demo-service",
        "ssh://git@github.com/example/demo-service.git",
    ],
)
def test_every_url_spelling_of_one_repo_normalizes_the_same(url):
    assert normalize_remote_url(url) == "github.com/example/demo-service"


def test_different_repos_do_not_normalize_together():
    a = normalize_remote_url("git@github.com:example/demo-service.git")
    b = normalize_remote_url("git@github.com:example/self-improve.git")
    assert a != b


# ----------------------------------------------------------------------
# the collapse
# ----------------------------------------------------------------------


def test_two_clones_of_one_repo_share_a_key(tmp_path):
    a = make_repo(tmp_path / "repo-0", "git@github.com:example/demo-service.git")
    b = make_repo(tmp_path / "repo-3", "git@github.com:example/demo-service.git")

    ida = resolve(str(a), gh=FakeGh())
    idb = resolve(str(b), gh=FakeGh())

    assert ida.key == idb.key
    assert ida.method == idb.method == METHOD_REMOTE_URL


def test_unrelated_repos_keep_distinct_keys(tmp_path):
    a = make_repo(tmp_path / "one", "git@github.com:example/demo-service.git")
    b = make_repo(tmp_path / "two", "git@github.com:example/other.git")
    assert resolve(str(a), gh=FakeGh()).key != resolve(str(b), gh=FakeGh()).key


def test_a_symlinked_path_collapses_onto_its_real_repo(tmp_path):
    """An invented renamed directory and its symlink resolve to one project.

    Stored session paths can retain an earlier spelling. Both paths must share
    identity without a repository-name mapping in the code."""
    real = make_repo(tmp_path / "demo-service", "git@github.com:example/demo-service.git")
    (tmp_path / "old-service").symlink_to(real)

    assert resolve(str(real), gh=FakeGh()).key == resolve(
        str(tmp_path / "old-service"), gh=FakeGh()
    ).key


def test_a_subdirectory_resolves_to_its_repo_root(tmp_path):
    """cwd is often a subdirectory of the repo, not the repo root."""
    repo = make_repo(tmp_path / "repo", "git@github.com:example/demo-service.git")
    sub = repo / "src" / "deep"
    sub.mkdir(parents=True)
    assert resolve(str(sub), gh=FakeGh()).key == resolve(str(repo), gh=FakeGh()).key


# ----------------------------------------------------------------------
# the rename trap — why the key is a number, not a name
# ----------------------------------------------------------------------


def test_gh_id_survives_a_rename_that_splits_the_url_key(tmp_path):
    """Old clones keep the pre-rename URL forever; a fresh clone gets the new
    one. Keyed on the URL string those two fracture. Keyed on GitHub's numeric
    id — which the redirect resolves for both — they stay one project."""
    old = make_repo(tmp_path / "old", "git@github.com:example/old-service.git")
    fresh = make_repo(tmp_path / "fresh", "git@github.com:example/demo-service.git")
    gh = FakeGh(
        {
            "example/old-service": {"id": 424242, "full_name": "example/demo-service"},
            "example/demo-service": {"id": 424242, "full_name": "example/demo-service"},
        }
    )

    a, b = resolve(str(old), gh=gh), resolve(str(fresh), gh=gh)

    assert a.key == b.key == "github:424242"
    assert a.method == METHOD_GH_REPO_ID
    # The display name follows the rename even for the stale clone.
    assert a.display == b.display == "example/demo-service"


def test_url_key_alone_would_have_fractured_on_that_rename(tmp_path):
    """Pins WHY the gh step exists — delete it and this documents the loss."""
    old = make_repo(tmp_path / "old", "git@github.com:example/old-service.git")
    fresh = make_repo(tmp_path / "fresh", "git@github.com:example/demo-service.git")
    gh = FakeGh(fail=True)  # no network / not installed
    assert resolve(str(old), gh=gh).key != resolve(str(fresh), gh=gh).key


# ----------------------------------------------------------------------
# the fallback chain — degradation must be recorded, never silent
# ----------------------------------------------------------------------


def test_gh_failure_degrades_to_the_remote_url_and_says_so(tmp_path):
    repo = make_repo(tmp_path / "r", "git@github.com:example/demo-service.git")
    ident = resolve(str(repo), gh=FakeGh(fail=True))
    assert ident.method == METHOD_REMOTE_URL
    assert ident.key == "remote:github.com/example/demo-service"


def test_a_git_repo_with_no_remote_falls_back_to_its_root(tmp_path):
    repo = make_repo(tmp_path / "local-only")
    ident = resolve(str(repo), gh=FakeGh())
    assert ident.method == METHOD_GIT_ROOT
    assert ident.key.startswith("path:")
    assert ident.display == "local-only"


def test_a_plain_directory_falls_back_to_its_realpath(tmp_path):
    d = tmp_path / "not-a-repo"
    d.mkdir()
    ident = resolve(str(d), gh=FakeGh())
    assert ident.method == METHOD_PATH
    assert ident.display == "not-a-repo"


def test_a_vanished_directory_is_unresolved_not_silently_merged(tmp_path):
    """Keep distinct unresolved paths distinct rather than inventing a shared project."""
    a = resolve(str(tmp_path / "gone-a"), gh=FakeGh())
    b = resolve(str(tmp_path / "gone-b"), gh=FakeGh())
    assert a.method == b.method == METHOD_UNRESOLVED
    assert a.key != b.key


def test_empty_path_is_unresolved_and_does_not_crash(tmp_path):
    ident = resolve("", gh=FakeGh())
    assert ident.method == METHOD_UNRESOLVED
    assert isinstance(ident, ProjectIdentity)


# ----------------------------------------------------------------------
# caching — repeated sessions must reuse repository resolution
# ----------------------------------------------------------------------


def test_repeated_resolution_of_one_repo_calls_gh_once(tmp_path):
    repo = make_repo(tmp_path / "r", "git@github.com:example/demo-service.git")
    gh = FakeGh({"example/demo-service": {"id": 7, "full_name": "example/demo-service"}})
    cache: dict = {}

    for _ in range(5):
        resolve(str(repo), gh=gh, cache=cache)

    assert len(gh.calls) == 1, f"gh called {len(gh.calls)}x; cache is not working"


def test_two_clones_share_one_cache_entry(tmp_path):
    a = make_repo(tmp_path / "a", "git@github.com:example/demo-service.git")
    b = make_repo(tmp_path / "b", "git@github.com:example/demo-service.git")
    gh = FakeGh({"example/demo-service": {"id": 7, "full_name": "example/demo-service"}})
    cache: dict = {}
    resolve(str(a), gh=gh, cache=cache)
    resolve(str(b), gh=gh, cache=cache)
    assert len(gh.calls) == 1


# ----------------------------------------------------------------------
# the routing consequence — why this matters beyond a tidier table
# ----------------------------------------------------------------------


def test_learning_counts_repos_not_working_copies(tmp_path):
    """Persist the canonical repository key on each learning.

    Multiple working directories with the same project_key must retain that key
    in projects_json, so later merges count repositories consistently."""
    from self_improve.miner import _persist_mine_payload
    from self_improve.store import Store, new_id, utc_now_iso

    store = Store(tmp_path / "s.db")
    key = "remote:github.com/example/demo-service"

    made = []
    for i, cwd in enumerate(
        ["/Users/x/Code/demo-service/repo-0", "/Users/x/Code/old-service/repo-3"]
    ):
        sf = f"/virtual/s{i}.jsonl"
        store.upsert_session(
            {
                "file_path": sf, "source": "claude", "session_id": f"s{i}",
                "project_path": cwd, "project_key": key,
                "project_display": "demo-service",
                "project_key_method": METHOD_REMOTE_URL,
                "headless": 0, "is_subagent": 0, "first_ts": "", "last_ts": "",
                "mtime": 0.0, "file_size": 0, "bytes_scanned": 0,
                "lines_scanned": 0, "malformed_lines": 0, "status": "ok",
                "error": "", "last_scanned_at": utc_now_iso(),
            }
        )
        iid = new_id()
        store.insert_incident(
            {
                "id": iid, "session_file": sf, "session_id": f"s{i}",
                "project_path": cwd, "project_key": key, "ts": "2026-08-10T00:00:00Z",
                "signal_type": "correction", "matched_text": "x", "window": [],
            }
        )
        made.append(store.query_one("SELECT * FROM incidents WHERE id = ?", (iid,)))
    store.commit()

    payload = {
        "is_real_learning": True, "incident_summary": "s", "generalized_rule": "r",
        "why": "w", "scope_guess": "project", "category": "tooling",
        "duplicate_of_existing_rule": None, "confidence": 0.7,
        "dedup_decision": "new", "dedup_target_id": "", "amended_rule_text": "",
        "amended_why": "", "violated_existing_rule": "", "path_globs": [],
    }
    learning = _persist_mine_payload(store, made[0], dict(payload), agentic=True)

    projects = json.loads(learning["projects_json"])
    assert projects == [key], (
        f"a learning must be attributed to the canonical repo, got {projects}"
    )


def test_identity_is_for_counting_the_write_target_stays_a_real_path(tmp_path):
    """Two different questions, two different fields.

    'Which repo is this?' must collapse clones, or global promotion misfires.
    'Where do I write the AGENTS.md line?' must stay a real directory —
    routing does Path(project) / 'AGENTS.md' and Path(project).is_dir(), and a
    canonical key like 'remote:github.com/o/n' is not a path. Conflating them
    was a latent break introduced with the collapse and caught here.
    """
    from self_improve.miner import _persist_mine_payload
    from self_improve.routing import primary_project
    from self_improve.store import Store, new_id, utc_now_iso

    store = Store(tmp_path / "s.db")
    cwd = str(tmp_path / "Code" / "demo-service" / "repo-0")
    key = "remote:github.com/example/demo-service"
    sf = "/virtual/s.jsonl"
    store.upsert_session(
        {
            "file_path": sf, "source": "claude", "session_id": "s",
            "project_path": cwd, "project_key": key,
            "project_display": "demo-service", "project_key_method": METHOD_REMOTE_URL,
            "headless": 0, "is_subagent": 0, "first_ts": "", "last_ts": "",
            "mtime": 0.0, "file_size": 0, "bytes_scanned": 0, "lines_scanned": 0,
            "malformed_lines": 0, "status": "ok", "error": "",
            "last_scanned_at": utc_now_iso(),
        }
    )
    iid = new_id()
    store.insert_incident(
        {
            "id": iid, "session_file": sf, "session_id": "s", "project_path": cwd,
            "project_key": key, "ts": "2026-08-10T00:00:00Z",
            "signal_type": "correction", "matched_text": "x", "window": [],
        }
    )
    store.commit()
    inc = store.query_one("SELECT * FROM incidents WHERE id = ?", (iid,))

    learning = _persist_mine_payload(
        store,
        inc,
        {
            "is_real_learning": True, "incident_summary": "s",
            "generalized_rule": "r", "why": "w", "scope_guess": "project",
            "category": "tooling", "duplicate_of_existing_rule": None,
            "confidence": 0.7, "dedup_decision": "new", "dedup_target_id": "",
            "amended_rule_text": "", "amended_why": "",
            "violated_existing_rule": "", "path_globs": [],
        },
        agentic=True,
    )

    assert json.loads(learning["projects_json"]) == [key], "counting uses the key"
    assert primary_project(learning) == cwd, (
        "routing must still get a real directory to write into, not the key"
    )


def test_repeated_sessions_in_one_directory_shell_out_to_git_once(tmp_path, monkeypatch):
    """Cache raw paths before Git subprocesses, then cache remote resolution.
    Repeated sessions should pay for Git only once per distinct directory.
    """
    import self_improve.project_identity as pi

    repo = make_repo(tmp_path / "r", "git@github.com:example/demo-service.git")
    calls = []
    real_run_git = pi._run_git

    def counting(cwd, *args):
        calls.append((cwd, args))
        return real_run_git(cwd, *args)

    monkeypatch.setattr(pi, "_run_git", counting)

    cache: dict = {}
    path_cache: dict = {}
    for _ in range(50):
        pi.resolve(str(repo), gh=FakeGh(), cache=cache, path_cache=path_cache)

    assert len(calls) == 2, (
        f"{len(calls)} git subprocesses for 50 sessions in ONE directory; "
        "expected 2 (rev-parse + remote get-url), the rest served from cache"
    )


def test_distinct_directories_still_each_resolve(tmp_path, monkeypatch):
    """The path cache must not merge two different directories."""
    import self_improve.project_identity as pi

    a = make_repo(tmp_path / "a", "git@github.com:example/one.git")
    b = make_repo(tmp_path / "b", "git@github.com:example/two.git")
    path_cache: dict = {}
    ida = pi.resolve(str(a), gh=FakeGh(), path_cache=path_cache)
    idb = pi.resolve(str(b), gh=FakeGh(), path_cache=path_cache)
    assert ida.key != idb.key


def test_a_gh_failure_logs_why_not_just_that(tmp_path, caplog):
    """'no gh_repo_id' has several causes with different fixes.

    Not installed, not authenticated, rate limited and private-repo all land in
    the same fallback, and the method field cannot tell them apart. The reason
    has to reach the log or the operator is guessing.
    """
    import logging

    repo = make_repo(tmp_path / "r", "git@github.com:example/demo-service.git")

    class Boom:
        def __call__(self, owner_repo):
            raise RuntimeError("gh: not authenticated; run gh auth login")

    with caplog.at_level(logging.WARNING):
        ident = resolve(str(repo), gh=Boom())

    assert ident.method == METHOD_REMOTE_URL
    assert "not authenticated" in caplog.text
    assert "example/demo-service" in caplog.text


def test_the_warning_fires_once_per_repository_not_once_per_session(tmp_path, caplog):
    """Warn once per repository even when many sessions share its failure."""
    import logging

    repo = make_repo(tmp_path / "r", "git@github.com:example/demo-service.git")

    class Boom:
        def __call__(self, owner_repo):
            raise RuntimeError("nope")

    cache: dict = {}
    with caplog.at_level(logging.WARNING):
        for _ in range(20):
            resolve(str(repo), gh=Boom(), cache=cache)

    assert caplog.text.count("gh lookup failed") == 1


def test_a_hanging_git_degrades_instead_of_wedging_the_scan(tmp_path, monkeypatch):
    """A Git subprocess timeout must produce a coarser identity and let scanning continue."""
    import subprocess

    import self_improve.project_identity as pi

    d = tmp_path / "boxish"
    d.mkdir()

    def hang(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="git", timeout=5)

    monkeypatch.setattr(pi.subprocess, "run", hang)

    ident = pi.resolve(str(d), gh=FakeGh())

    assert ident.method == METHOD_PATH, "a git timeout must not raise"
    assert ident.key.startswith("path:")


def test_git_calls_carry_a_timeout_at_all():
    """The degradation above only exists because a timeout is passed."""
    import inspect

    import self_improve.project_identity as pi

    src = inspect.getsource(pi._run_git)
    assert "timeout=" in src, "git could block forever in a cloud-sync directory"
