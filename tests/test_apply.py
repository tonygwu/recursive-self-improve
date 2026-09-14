"""Tests for self_improve.apply: eval-gated apply, snapshots, rollback.

These tests build their unified diffs with difflib directly so they exercise
apply.py's protocol without depending on propose.py internals; apply.py itself
imports apply_unified_diff/PatchConflict from self_improve.propose, so a
missing propose module fails collection loudly (resolves at integration).
All git repos are real repos created inside pytest tmp dirs.
"""

from __future__ import annotations

import difflib
import subprocess
from pathlib import Path

import pytest

from self_improve.apply import (
    ApplyError,
    apply_proposal,
    mirror_rel_path,
    rollback,
    snapshot,
    snapshots_repo,
)
from self_improve.config import Config
from self_improve.store import Store, new_id, utc_now_iso

# ----------------------------------------------------------------------
# helpers / fixtures
# ----------------------------------------------------------------------


def run_git(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True
    )
    assert proc.returncode == 0, f"git {args} failed: {proc.stderr}"
    return proc.stdout.strip()


def make_diff(old: str, new: str, name: str = "target.md") -> str:
    """Unified diff between two contents, built with difflib only."""
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{name}",
            tofile=f"b/{name}",
        )
    )


def init_git_repo(path: Path, filename: str, content: str) -> Path:
    """Create a real git repo with one committed file; returns the repo path."""
    path.mkdir(parents=True, exist_ok=True)
    run_git(["init", "-q"], path)
    run_git(["config", "user.name", "test"], path)
    run_git(["config", "user.email", "test@example.com"], path)
    (path / filename).write_text(content, encoding="utf-8")
    run_git(["add", "--", filename], path)
    run_git(["commit", "-q", "-m", "initial"], path)
    return path


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(state_dir=str(tmp_path / "state"))


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "state" / "state.db")
    # These tests exercise file/branch mechanics after explicit class consent.
    # Default-off and historical-backlog policy have separate entry-point tests.
    from self_improve.execution_policy import set_class_policy

    for target_class in ("global", "project", "skill"):
        set_class_policy(s, target_class, True, now="2020-01-01T00:00:00Z")
    yield s
    s.close()


def insert_proposal(
    store: Store,
    *,
    target: Path,
    diff: str,
    status: str = "gated_pass",
    action: str = "add",
    target_kind: str = "global_claude_md",
) -> dict:
    learning_id = new_id()
    store.insert(
        "learnings",
        {"id": learning_id, "rule_text": "test rule", "created_at": utc_now_iso()},
    )
    pid = new_id()
    store.insert(
        "proposals",
        {
            "id": pid,
            "learning_id": learning_id,
            "target_path": str(target),
            "target_kind": target_kind,
            "action": action,
            "diff_unified": diff,
            "status": status,
            "created_at": utc_now_iso(),
        },
    )
    store.commit()
    row = store.query_one("SELECT * FROM proposals WHERE id = ?", (pid,))
    assert row is not None
    return row


def events_for(store: Store, proposal_id: str) -> list[dict]:
    return store.query(
        "SELECT * FROM proposal_events WHERE proposal_id = ? ORDER BY ts, id",
        (proposal_id,),
    )


OLD = "# CLAUDE.md\n\n- old rule\n"
NEW = "# CLAUDE.md\n\n- old rule\n- new rule from miner\n"


# ----------------------------------------------------------------------
# non-git targets
# ----------------------------------------------------------------------


class TestNonGitApply:
    def test_apply_then_rollback_round_trips_byte_identically(
        self, store: Store, cfg: Config, tmp_path: Path
    ):
        target = tmp_path / "home" / ".claude" / "CLAUDE.md"
        target.parent.mkdir(parents=True)
        original_bytes = OLD.encode("utf-8")
        target.write_bytes(original_bytes)
        target.chmod(0o600)  # distinctive mode: must survive apply + rollback
        proposal = insert_proposal(store, target=target, diff=make_diff(OLD, NEW))

        outcome = apply_proposal(store, cfg, proposal)

        assert outcome["outcome"] == "applied"
        assert outcome["mode"] == "file"
        assert target.read_text(encoding="utf-8") == NEW
        assert (target.stat().st_mode & 0o7777) == 0o600
        row = store.query_one(
            "SELECT * FROM proposals WHERE id = ?", (proposal["id"],)
        )
        assert row["status"] == "applied"
        assert row["applied_at"] != ""
        assert len(row["snapshot_commit_before"]) == 40
        assert len(row["snapshot_commit_after"]) == 40
        assert row["snapshot_commit_before"] != row["snapshot_commit_after"]
        assert [e["event"] for e in events_for(store, proposal["id"])] == ["applied"]

        rb = rollback(store, cfg, proposal["id"])

        assert rb["outcome"] == "rolled_back"
        assert target.read_bytes() == original_bytes  # byte-identical
        assert (target.stat().st_mode & 0o7777) == 0o600
        row = store.query_one(
            "SELECT * FROM proposals WHERE id = ?", (proposal["id"],)
        )
        assert row["status"] == "rolled_back"
        assert [e["event"] for e in events_for(store, proposal["id"])] == [
            "applied",
            "rolled_back",
        ]

    def test_missing_target_file_is_created_and_rollback_removes_it(
        self, store: Store, cfg: Config, tmp_path: Path
    ):
        target = tmp_path / "home" / ".codex" / "AGENTS.md"  # parent doesn't exist
        assert not target.parent.exists()
        new_content = "# Codex global\n\n- never guess; fail loud\n"
        proposal = insert_proposal(
            store, target=target, diff=make_diff("", new_content)
        )

        outcome = apply_proposal(store, cfg, proposal)

        assert outcome["outcome"] == "applied"
        assert target.read_text(encoding="utf-8") == new_content

        rb = rollback(store, cfg, proposal["id"])

        assert rb["outcome"] == "rolled_back"
        assert rb["restored_absent"] is True
        assert not target.exists()  # absence restored

    def test_patch_conflict_holds_proposal_and_writes_nothing(
        self, store: Store, cfg: Config, tmp_path: Path
    ):
        target = tmp_path / "CLAUDE.md"
        original_bytes = OLD.encode("utf-8")
        target.write_bytes(original_bytes)
        # Diff built against entirely different base content: any correct
        # patcher must raise PatchConflict rather than guess.
        conflicting = make_diff(
            "totally\ndifferent\nbase\n", "totally\nCHANGED\nbase\n"
        )
        proposal = insert_proposal(store, target=target, diff=conflicting)

        outcome = apply_proposal(store, cfg, proposal)

        assert outcome["outcome"] == "held"
        assert outcome["reason"] == "patch_conflict"
        assert target.read_bytes() == original_bytes  # untouched
        row = store.query_one(
            "SELECT * FROM proposals WHERE id = ?", (proposal["id"],)
        )
        assert row["status"] == "held"
        events = events_for(store, proposal["id"])
        assert [e["event"] for e in events] == ["held"]
        assert events[0]["note"].startswith("patch_conflict: ")


class TestPolicyGate:
    @pytest.mark.parametrize("action", ["convert_to_hook", "delete_human_line"])
    def test_carve_out_actions_go_to_review_queue(
        self, store: Store, cfg: Config, tmp_path: Path, action: str
    ):
        target = tmp_path / "CLAUDE.md"
        target.write_text(OLD, encoding="utf-8")
        proposal = insert_proposal(
            store, target=target, diff=make_diff(OLD, NEW), action=action
        )

        outcome = apply_proposal(store, cfg, proposal)

        assert outcome["outcome"] == "held"
        assert outcome["reason"] == "action_review_queue"
        assert target.read_text(encoding="utf-8") == OLD  # no write
        row = store.query_one(
            "SELECT * FROM proposals WHERE id = ?", (proposal["id"],)
        )
        assert row["status"] == "held"
        assert [e["event"] for e in events_for(store, proposal["id"])] == ["held"]

    @pytest.mark.parametrize("status", ["pending", "gated_fail", "rejected_user"])
    def test_non_appliable_status_is_held(
        self, store: Store, cfg: Config, tmp_path: Path, status: str
    ):
        target = tmp_path / "CLAUDE.md"
        target.write_text(OLD, encoding="utf-8")
        proposal = insert_proposal(
            store, target=target, diff=make_diff(OLD, NEW), status=status
        )

        outcome = apply_proposal(store, cfg, proposal)

        assert outcome["outcome"] == "held"
        assert outcome["reason"] == ("lesson_rejected" if status == "rejected_user" else "status_not_appliable")
        assert target.read_text(encoding="utf-8") == OLD

    def test_ungated_status_requires_human_approval(
        self, store: Store, cfg: Config, tmp_path: Path
    ):
        target = tmp_path / "CLAUDE.md"
        target.write_text(OLD, encoding="utf-8")
        proposal = insert_proposal(
            store, target=target, diff=make_diff(OLD, NEW), status="ungated"
        )

        outcome = apply_proposal(store, cfg, proposal)

        assert outcome["outcome"] == "held"
        assert outcome["reason"] == "status_not_appliable"
        assert target.read_text(encoding="utf-8") == OLD

    def test_rollback_of_unapplied_proposal_fails_loud(
        self, store: Store, cfg: Config, tmp_path: Path
    ):
        from self_improve.apply import ApplyError

        target = tmp_path / "CLAUDE.md"
        proposal = insert_proposal(
            store, target=target, diff=make_diff(OLD, NEW), status="pending"
        )
        with pytest.raises(ApplyError):
            rollback(store, cfg, proposal["id"])
        with pytest.raises(ApplyError):
            rollback(store, cfg, "no-such-id")


# ----------------------------------------------------------------------
# git-repo targets (branch commits via plumbing, worktree untouched)
# ----------------------------------------------------------------------

REPO_OLD = "# AGENTS.md\n\n- committed rule\n"
REPO_NEW = "# AGENTS.md\n\n- committed rule\n- mined rule\n"
REPO_NEWER = "# AGENTS.md\n\n- committed rule\n- mined rule\n- second mined rule\n"


class TestGitBranchApply:
    def test_apply_creates_branch_and_leaves_worktree_and_head_untouched(
        self, store: Store, cfg: Config, tmp_path: Path
    ):
        repo = init_git_repo(tmp_path / "project", "AGENTS.md", REPO_OLD)
        target = repo / "AGENTS.md"
        head_before = run_git(["rev-parse", "HEAD"], repo)
        branch_before = run_git(["symbolic-ref", "HEAD"], repo)
        # dirty, uncommitted worktree edit that must survive untouched
        dirty = REPO_OLD + "# uncommitted local scribble\n"
        target.write_text(dirty, encoding="utf-8")
        proposal = insert_proposal(
            store,
            target=target,
            diff=make_diff(REPO_OLD, REPO_NEW, "AGENTS.md"),
            target_kind="project_agents_md",
        )

        outcome = apply_proposal(store, cfg, proposal)

        assert outcome["outcome"] == "applied"
        assert outcome["mode"] == "git_branch"
        # worktree + HEAD + current branch untouched
        assert target.read_text(encoding="utf-8") == dirty
        assert run_git(["rev-parse", "HEAD"], repo) == head_before
        assert run_git(["symbolic-ref", "HEAD"], repo) == branch_before
        # branch exists, tip holds the patched file, parented on old HEAD
        branch = cfg.project_branch_name
        assert (
            run_git(["show", f"refs/heads/{branch}:AGENTS.md"], repo) + "\n"
            == REPO_NEW
        )
        assert run_git(["rev-parse", f"refs/heads/{branch}^"], repo) == head_before
        row = store.query_one(
            "SELECT * FROM proposals WHERE id = ?", (proposal["id"],)
        )
        assert row["status"] == "applied"

    def test_second_apply_stacks_on_existing_branch(
        self, store: Store, cfg: Config, tmp_path: Path
    ):
        repo = init_git_repo(tmp_path / "project", "AGENTS.md", REPO_OLD)
        target = repo / "AGENTS.md"
        p1 = insert_proposal(
            store, target=target, diff=make_diff(REPO_OLD, REPO_NEW, "AGENTS.md")
        )
        p2 = insert_proposal(
            store, target=target, diff=make_diff(REPO_NEW, REPO_NEWER, "AGENTS.md")
        )

        assert apply_proposal(store, cfg, p1)["outcome"] == "applied"
        assert apply_proposal(store, cfg, p2)["outcome"] == "applied"

        branch = cfg.project_branch_name
        # second commit based on the existing branch tip, not HEAD
        assert (
            run_git(["show", f"refs/heads/{branch}:AGENTS.md"], repo) + "\n"
            == REPO_NEWER
        )
        assert (
            run_git(["rev-list", "--count", f"HEAD..refs/heads/{branch}"], repo)
            == "2"
        )
        assert run_git(["rev-parse", "HEAD"], repo) == run_git(
            ["rev-parse", f"refs/heads/{branch}~2"], repo
        )

    def test_git_rollback_adds_revert_commit_restoring_before_content(
        self, store: Store, cfg: Config, tmp_path: Path
    ):
        repo = init_git_repo(tmp_path / "project", "AGENTS.md", REPO_OLD)
        target = repo / "AGENTS.md"
        head_before = run_git(["rev-parse", "HEAD"], repo)
        proposal = insert_proposal(
            store, target=target, diff=make_diff(REPO_OLD, REPO_NEW, "AGENTS.md")
        )
        assert apply_proposal(store, cfg, proposal)["outcome"] == "applied"

        rb = rollback(store, cfg, proposal["id"])

        assert rb["outcome"] == "rolled_back"
        assert rb["mode"] == "git_branch"
        branch = cfg.project_branch_name
        # branch tip content is byte-identical to the pre-apply base content
        show = subprocess.run(
            ["git", "show", f"refs/heads/{branch}:AGENTS.md"],
            cwd=str(repo),
            capture_output=True,
        )
        assert show.returncode == 0
        assert show.stdout == REPO_OLD.encode("utf-8")
        # apply + revert = 2 commits on the branch; worktree/HEAD untouched
        assert (
            run_git(["rev-list", "--count", f"HEAD..refs/heads/{branch}"], repo)
            == "2"
        )
        assert run_git(["rev-parse", "HEAD"], repo) == head_before
        assert target.read_text(encoding="utf-8") == REPO_OLD
        row = store.query_one(
            "SELECT * FROM proposals WHERE id = ?", (proposal["id"],)
        )
        assert row["status"] == "rolled_back"


# ----------------------------------------------------------------------
# shadow snapshots repo
# ----------------------------------------------------------------------


class TestSnapshots:
    def test_snapshot_returns_sha_and_mirrors_absolute_path(
        self, cfg: Config, tmp_path: Path
    ):
        target = tmp_path / "some" / "dir" / "CLAUDE.md"
        target.parent.mkdir(parents=True)
        target.write_text(OLD, encoding="utf-8")

        sha = snapshot(cfg, target, "before")

        assert len(sha) == 40 and all(c in "0123456789abcdef" for c in sha)
        repo = snapshots_repo(cfg)
        expected_rel = str(target).lstrip("/")
        assert mirror_rel_path(target) == expected_rel
        listed = run_git(["ls-tree", "-r", "--name-only", sha], repo)
        assert expected_rel in listed.splitlines()
        assert f"before: {target}" in run_git(["log", "-1", "--format=%s", sha], repo)

    def test_snapshot_repo_accumulates_history_across_apply_and_rollback(
        self, store: Store, cfg: Config, tmp_path: Path
    ):
        target = tmp_path / "CLAUDE.md"
        target.write_text(OLD, encoding="utf-8")
        proposal = insert_proposal(store, target=target, diff=make_diff(OLD, NEW))

        apply_proposal(store, cfg, proposal)
        rollback(store, cfg, proposal["id"])

        repo = snapshots_repo(cfg)
        assert int(run_git(["rev-list", "--count", "HEAD"], repo)) >= 3
        subjects = run_git(["log", "--format=%s"], repo).splitlines()
        assert any(s.startswith("before: ") for s in subjects)
        assert any(s.startswith("after: ") for s in subjects)
        assert any(s.startswith("rollback: ") for s in subjects)
        # mirror at HEAD holds the rolled-back (original) content
        rel = mirror_rel_path(target)
        show = subprocess.run(
            ["git", "show", f"HEAD:{rel}"], cwd=str(repo), capture_output=True
        )
        assert show.returncode == 0
        assert show.stdout == OLD.encode("utf-8")

    def test_snapshot_records_absence_of_missing_target(
        self, cfg: Config, tmp_path: Path
    ):
        target = tmp_path / "not-yet-created.md"
        sha = snapshot(cfg, target, "before")
        repo = snapshots_repo(cfg)
        listed = run_git(["ls-tree", "-r", "--name-only", sha], repo)
        assert mirror_rel_path(target) not in listed.splitlines()


class TestRolledBackNeverSilentlyReapplies:
    """An undo the operator performed must not be reversed by the next run.

    Exercise the full apply, rollback, and subsequent-proposal path. A terminal
    rollback must prevent the same learning from being proposed again.
    """

    def _target(self, tmp_path: Path) -> Path:
        target = tmp_path / "home" / ".claude" / "CLAUDE.md"
        target.parent.mkdir(parents=True)
        target.write_text(OLD)
        return target

    def test_a_learning_with_a_rolled_back_proposal_is_held_not_applied(
        self, store: Store, cfg: Config, tmp_path: Path
    ):
        target = self._target(tmp_path)
        first = insert_proposal(store, target=target, diff=make_diff(OLD, NEW))
        apply_proposal(store, cfg, first)
        rollback(store, cfg, first["id"])

        # A later run proposes the same learning again.
        second_id = new_id()
        store.insert(
            "proposals",
            {
                "id": second_id,
                "learning_id": first["learning_id"],
                "target_path": str(target),
                "target_kind": "global_claude_md",
                "action": "add",
                "diff_unified": make_diff(OLD, NEW),
                "status": "gated_pass",
                "created_at": utc_now_iso(),
            },
        )
        store.commit()
        second = store.query_one("SELECT * FROM proposals WHERE id = ?", (second_id,))

        outcome = apply_proposal(store, cfg, second)

        assert outcome["outcome"] == "held", (
            "a rule the operator rolled back was re-applied without asking"
        )
        assert outcome["reason"] == "prior_rollback"
        assert target.read_text() == OLD, "the file was modified anyway"
        # The ledger has to say WHICH undo caused the hold, or a held proposal
        # is just an unexplained refusal.
        note = events_for(store, second_id)[-1]["note"]
        assert "rolled_back" in note and first["id"] in note

    def test_a_learning_with_no_rollback_history_still_applies(
        self, store: Store, cfg: Config, tmp_path: Path
    ):
        """The guard must not freeze the flywheel for everything else."""
        target = self._target(tmp_path)
        proposal = insert_proposal(store, target=target, diff=make_diff(OLD, NEW))
        assert apply_proposal(store, cfg, proposal)["outcome"] == "applied"


def test_a_gitignored_target_still_reaches_the_branch(store: Store, cfg: Config, tmp_path: Path):
    """Stage rule files even when the fixture repository ignores .claude/rules.

    Normal git add excludes the ignored path. The rule must still appear on the
    review branch, not only in the working copy.

    apply.py uses hash-object and update-index --cacheinfo with a temporary index
    to include the rule without changing the repository's ignore rules.
    """
    import subprocess

    repo = tmp_path / "proj"
    repo.mkdir()
    run = lambda *a: subprocess.run(a, cwd=repo, check=True, capture_output=True)
    run("git", "init", "-q")
    run("git", "config", "user.email", "t@e.st")
    run("git", "config", "user.name", "t")
    (repo / ".gitignore").write_text(".claude/*\n")
    run("git", "add", ".gitignore")
    run("git", "commit", "-qm", "init")

    target = repo / ".claude" / "rules" / "paths.md"
    proposal = insert_proposal(
        store, target=target, diff=make_diff("", "- **A path-scoped rule.**\n"),
        target_kind="rule_file",
    )

    outcome = apply_proposal(store, cfg, proposal)

    assert outcome["outcome"] == "applied", outcome
    branch = cfg.project_branch_name
    listed = subprocess.run(
        ("git", "ls-tree", "-r", "--name-only", branch),
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout
    assert ".claude/rules/paths.md" in listed, (
        "the rule file never reached the branch — gitignore swallowed it"
    )


def test_a_target_reached_through_a_symlink_is_refused_not_written(
    store: Store, cfg: Config, tmp_path: Path
):
    """Refuse a symlink alias that escapes the target repository's root.

    `relpath` compares `os.path.abspath(target)` — which keeps symlinks —
    against git's `--show-toplevel`, which resolves them. A target reached
    through a symlinked directory therefore lands OUTSIDE its own toplevel and
    the guard fires.

    Refusal must occur before a computed parent-relative path can write into
    an unnamed destination.
    """
    import subprocess

    real = tmp_path / "real"
    real.mkdir()
    run = lambda *a: subprocess.run(a, cwd=real, check=True, capture_output=True)
    run("git", "init", "-q")
    run("git", "config", "user.email", "t@e.st")
    run("git", "config", "user.name", "t")
    (real / "AGENTS.md").write_text("# rules\n")
    run("git", "add", "AGENTS.md")
    run("git", "commit", "-qm", "init")

    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    target = link / "AGENTS.md"
    before = (real / "AGENTS.md").read_bytes()

    proposal = insert_proposal(
        store, target=target, diff=make_diff("# rules\n", "# rules\n- **New.**\n"),
        target_kind="project_agents_md",
    )
    with pytest.raises(ApplyError, match="escapes its git toplevel"):
        apply_proposal(store, cfg, proposal)

    # And nothing was written through either path.
    assert (real / "AGENTS.md").read_bytes() == before
    branches = subprocess.run(
        ("git", "branch", "--list", cfg.project_branch_name),
        cwd=real, capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert branches == "", f"a branch was created for a refused apply: {branches!r}"


def test_a_project_repo_with_no_commits_is_refused_cleanly(
    store: Store, cfg: Config, tmp_path: Path
):
    """`git init` and nothing else is a real state for a new project.

    apply.py bases its branch on HEAD, which does not exist yet, and raises.
    Nothing had ever run that line. What matters is that it refuses BEFORE
    touching anything: no file written, no branch, no orphaned index.
    """
    import subprocess

    repo = tmp_path / "fresh"
    repo.mkdir()
    run = lambda *a: subprocess.run(a, cwd=repo, check=True, capture_output=True)
    run("git", "init", "-q")
    run("git", "config", "user.email", "t@e.st")
    run("git", "config", "user.name", "t")

    target = repo / "AGENTS.md"
    proposal = insert_proposal(
        store, target=target, diff=make_diff("", "- **A rule.**\n"),
        target_kind="project_agents_md",
    )
    with pytest.raises(ApplyError, match="has no commits"):
        apply_proposal(store, cfg, proposal)

    assert not target.exists(), "a refused apply created the target file"
    branches = subprocess.run(
        ("git", "branch", "--list"), cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    assert branches == "", f"a refused apply left a branch: {branches!r}"


def test_rollback_of_an_unknown_id_names_the_id(store: Store, cfg: Config):
    """`selfimprove rollback <id>` is what an operator runs when a rule made
    things worse. A typo must say which id was not found, not fail obscurely
    or — worse — succeed at nothing."""
    with pytest.raises(ApplyError, match="no proposal with id 'not-a-real-id'"):
        rollback(store, cfg, "not-a-real-id")


def test_rollback_without_a_before_snapshot_refuses_rather_than_guessing(
    store: Store, cfg: Config, tmp_path: Path
):
    """The snapshot IS the undo. Without it there is nothing to restore, and
    the only safe move is to refuse: writing an empty file, or the current
    content, would silently invent the "before" state."""
    target = tmp_path / "AGENTS.md"
    target.write_text("# rules\n- **Applied.**\n")
    proposal = insert_proposal(
        store, target=target, diff=make_diff("# rules\n", "# rules\n- **Applied.**\n"),
        target_kind="project_agents_md",
    )
    store.update(
        "proposals", "id", proposal["id"],
        {"status": "applied", "snapshot_commit_before": ""},
    )
    store.commit()
    before = target.read_bytes()

    with pytest.raises(ApplyError, match="has no snapshot_commit_before"):
        rollback(store, cfg, proposal["id"])
    assert target.read_bytes() == before, "a refused rollback still touched the file"
