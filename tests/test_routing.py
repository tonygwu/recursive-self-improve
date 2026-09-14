"""Tests for routing: topology detection (lstat semantics) + decision table."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from self_improve.config import Config
from self_improve.routing import (
    STUB_CONTENT,
    RouteDecision,
    RoutingError,
    Topology,
    TopologyError,
    detect_topology,
    primary_project,
    route,
    rule_file_path,
    skill_slug,
)


def make_cfg(tmp_path: Path, **overrides) -> Config:
    """Config pointed entirely inside tmp_path so no real files are touched."""
    defaults = dict(
        global_claude_md=str(tmp_path / "dot-claude" / "CLAUDE.md"),
        codex_global_agents_md=str(tmp_path / "dot-codex" / "AGENTS.md"),
        skills_dir=str(tmp_path / "dot-claude" / "skills"),
        state_dir=str(tmp_path / "state"),
    )
    defaults.update(overrides)
    return Config(**defaults)


# ----------------------------------------------------------------------------
# detect_topology
# ----------------------------------------------------------------------------


class TestDetectTopology:
    def test_independent_files(self, tmp_path: Path) -> None:
        (tmp_path / "AGENTS.md").write_text("# Agents\n\nGeneral rules.\n")
        (tmp_path / "CLAUDE.md").write_text(
            "# Claude notes\n\nClaude-specific things that are definitely not a pointer.\n"
        )
        topo = detect_topology(tmp_path)
        assert topo.agents_md_exists is True
        assert topo.claude_md_exists is True
        assert topo.claude_md_is_symlink is False
        assert topo.claude_md_is_stub is False
        assert topo.write_target_general == tmp_path / "AGENTS.md"
        assert topo.write_target_claude_specific == tmp_path / "CLAUDE.md"
        assert topo.needs_claude_md_stub is False

    def test_symlink_to_agents(self, tmp_path: Path) -> None:
        (tmp_path / "AGENTS.md").write_text("# Agents\n\nGeneral rules.\n")
        os.symlink("AGENTS.md", tmp_path / "CLAUDE.md")  # real relative symlink
        topo = detect_topology(tmp_path)
        assert topo.claude_md_exists is True
        assert topo.claude_md_is_symlink is True
        assert topo.claude_md_symlink_to_agents is True
        # A symlink is classified by lstat, never read as content:
        assert topo.claude_md_is_stub is False
        # Both content kinds go to AGENTS.md — never through the link.
        assert topo.write_target_general == tmp_path / "AGENTS.md"
        assert topo.write_target_claude_specific == tmp_path / "AGENTS.md"

    def test_symlink_elsewhere_never_written_through(self, tmp_path: Path) -> None:
        (tmp_path / "AGENTS.md").write_text("# Agents\n")
        other = tmp_path / "OTHER.md"
        other.write_text("something else\n")
        os.symlink("OTHER.md", tmp_path / "CLAUDE.md")
        topo = detect_topology(tmp_path)
        assert topo.claude_md_is_symlink is True
        assert topo.claude_md_symlink_to_agents is False
        # Still never write through a symlink: everything goes to AGENTS.md.
        assert topo.write_target_general == tmp_path / "AGENTS.md"
        assert topo.write_target_claude_specific == tmp_path / "AGENTS.md"

    def test_stub_pointer(self, tmp_path: Path) -> None:
        (tmp_path / "AGENTS.md").write_text("# Agents\n")
        (tmp_path / "CLAUDE.md").write_text(STUB_CONTENT)  # the real 11-byte stub
        assert os.lstat(tmp_path / "CLAUDE.md").st_size == 11
        topo = detect_topology(tmp_path)
        assert topo.claude_md_is_stub is True
        assert topo.claude_md_is_symlink is False
        assert topo.write_target_general == tmp_path / "AGENTS.md"
        assert topo.write_target_claude_specific == tmp_path / "AGENTS.md"

    def test_small_non_pointer_file_is_not_stub(self, tmp_path: Path) -> None:
        (tmp_path / "AGENTS.md").write_text("# Agents\n")
        (tmp_path / "CLAUDE.md").write_text("Use tabs.\n")  # tiny but a real rule
        topo = detect_topology(tmp_path)
        assert topo.claude_md_is_stub is False
        assert topo.write_target_claude_specific == tmp_path / "CLAUDE.md"

    def test_large_file_mentioning_agents_md_is_not_stub(self, tmp_path: Path) -> None:
        (tmp_path / "AGENTS.md").write_text("# Agents\n")
        body = "See AGENTS.md for details.\n" + ("Real rule line here.\n" * 20)
        assert len(body.encode()) >= 200
        (tmp_path / "CLAUDE.md").write_text(body)
        topo = detect_topology(tmp_path)
        assert topo.claude_md_is_stub is False

    def test_neither_exists(self, tmp_path: Path) -> None:
        topo = detect_topology(tmp_path)
        assert topo.agents_md_exists is False
        assert topo.claude_md_exists is False
        assert topo.write_target_general == tmp_path / "AGENTS.md"
        assert topo.write_target_claude_specific == tmp_path / "AGENTS.md"
        assert topo.needs_claude_md_stub is True
        assert STUB_CONTENT == "@AGENTS.md\n"

    def test_detect_never_creates_files(self, tmp_path: Path) -> None:
        detect_topology(tmp_path)
        assert list(tmp_path.iterdir()) == []

    def test_missing_project_dir_raises(self, tmp_path: Path) -> None:
        with pytest.raises(TopologyError):
            detect_topology(tmp_path / "does-not-exist")


# ----------------------------------------------------------------------------
# route decision table
# ----------------------------------------------------------------------------


def learning(**kw) -> dict:
    base = {
        "id": "abc123",
        "title": "Verify model identity",
        "rule_text": "Assert the reported model matches the requested class.",
        "project_count": 1,
        "projects": ["/tmp/does-not-matter"],
        "source": "claude",
    }
    base.update(kw)
    return base


def _repo(path: Path) -> Path:
    """Construct a real temporary repository so ordinary routing tests pass its guard."""
    import subprocess

    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(("git", "init", "-q"), cwd=path, check=True, capture_output=True)
    return path


class TestRoute:
    def test_project_count_promotes_to_global(self, tmp_path: Path) -> None:
        cfg = make_cfg(tmp_path)
        d = route(learning(project_count=3), cfg)
        assert d.target_path == Path(cfg.global_claude_md)
        assert d.target_kind == "global_claude_md"
        assert d.action == "add"

    def test_scope_global_promotes_regardless_of_count(self, tmp_path: Path) -> None:
        cfg = make_cfg(tmp_path)
        d = route(learning(project_count=1, scope_guess="global"), cfg)
        assert d.target_path == Path(cfg.global_claude_md)
        assert d.target_kind == "global_claude_md"

    def test_below_threshold_not_global(self, tmp_path: Path) -> None:
        proj = _repo(tmp_path / "proj")
        cfg = make_cfg(tmp_path)
        d = route(learning(project_count=2, projects=[str(proj)]), cfg)
        assert d.target_kind == "project_agents_md"

    def test_hook_scope_routes_to_settings_json(self, tmp_path: Path) -> None:
        cfg = make_cfg(tmp_path)
        d = route(learning(scope_guess="hook"), cfg)
        assert d.action == "convert_to_hook"
        assert d.target_kind == "hook"
        assert d.target_path == Path(cfg.global_claude_md).parent / "settings.json"

    def test_skill_scope_routes_to_new_skill_dir(self, tmp_path: Path) -> None:
        cfg = make_cfg(tmp_path)
        d = route(learning(scope_guess="skill"), cfg)
        assert d.action == "new_skill"
        assert d.target_kind == "skill"
        assert d.target_path == Path(cfg.skills_dir) / "verify-model-identity" / "SKILL.md"

    def test_codex_global_scope(self, tmp_path: Path) -> None:
        cfg = make_cfg(tmp_path)
        d = route(learning(scope_guess="codex_global"), cfg)
        assert d.target_path == Path(cfg.codex_global_agents_md)
        assert d.target_kind == "codex_global"
        assert d.action == "add"

    def test_codex_source_cross_project(self, tmp_path: Path) -> None:
        cfg = make_cfg(tmp_path)
        d = route(learning(source="codex", project_count=2), cfg)
        assert d.target_path == Path(cfg.codex_global_agents_md)
        assert d.target_kind == "codex_global"

    def test_codex_source_single_project_stays_project_scoped(self, tmp_path: Path) -> None:
        proj = _repo(tmp_path / "proj")
        cfg = make_cfg(tmp_path)
        d = route(learning(source="codex", project_count=1, projects=[str(proj)]), cfg)
        assert d.target_kind == "project_agents_md"
        assert d.target_path == proj / "AGENTS.md"

    def test_project_route_uses_topology_general(self, tmp_path: Path) -> None:
        proj = _repo(tmp_path / "proj")
        (proj / "AGENTS.md").write_text("# Agents\n")
        os.symlink("AGENTS.md", proj / "CLAUDE.md")
        cfg = make_cfg(tmp_path)
        d = route(learning(projects=[str(proj)]), cfg)
        assert d.target_path == proj / "AGENTS.md"
        assert d.target_kind == "project_agents_md"
        assert isinstance(d.topology, Topology)
        assert d.topology.claude_md_is_symlink is True

    def test_project_route_claude_specific_independent_files(self, tmp_path: Path) -> None:
        proj = _repo(tmp_path / "proj")
        (proj / "AGENTS.md").write_text("# Agents\n")
        (proj / "CLAUDE.md").write_text("# Claude-only conventions live right here.\n")
        cfg = make_cfg(tmp_path)
        d = route(learning(projects=[str(proj)], claude_specific=True), cfg)
        assert d.target_path == proj / "CLAUDE.md"
        assert d.target_kind == "project_claude_md"

    def test_project_route_claude_specific_stub_goes_to_agents(self, tmp_path: Path) -> None:
        proj = _repo(tmp_path / "proj")
        (proj / "AGENTS.md").write_text("# Agents\n")
        (proj / "CLAUDE.md").write_text(STUB_CONTENT)
        cfg = make_cfg(tmp_path)
        d = route(learning(projects=[str(proj)], claude_specific=True), cfg)
        assert d.target_path == proj / "AGENTS.md"
        assert d.target_kind == "project_agents_md"

    def test_projects_json_is_accepted(self, tmp_path: Path) -> None:
        proj = _repo(tmp_path / "proj")
        cfg = make_cfg(tmp_path)
        lrn = learning()
        del lrn["projects"]
        lrn["projects_json"] = f'["{proj}"]'
        d = route(lrn, cfg)
        assert d.target_path == proj / "AGENTS.md"

    def test_no_project_raises(self, tmp_path: Path) -> None:
        cfg = make_cfg(tmp_path)
        lrn = learning()
        del lrn["projects"]
        with pytest.raises(RoutingError):
            route(lrn, cfg)

    def test_malformed_projects_json_raises(self, tmp_path: Path) -> None:
        cfg = make_cfg(tmp_path)
        lrn = learning()
        del lrn["projects"]
        lrn["projects_json"] = "not json ["
        with pytest.raises(RoutingError):
            route(lrn, cfg)

    def test_returns_route_decision(self, tmp_path: Path) -> None:
        cfg = make_cfg(tmp_path)
        d = route(learning(project_count=5), cfg)
        assert isinstance(d, RouteDecision)
        assert d.reason  # every decision carries an auditable reason


class TestHelpers:
    def test_skill_slug_from_title(self) -> None:
        assert skill_slug({"title": "Verify model identity, not liveness!"}) == (
            "verify-model-identity-not-liveness"
        )

    def test_skill_slug_falls_back_to_rule_text(self) -> None:
        assert skill_slug({"title": "", "rule_text": "Always read the raw response."}) == (
            "always-read-the-raw-response"
        )

    def test_skill_slug_caps_at_eight_words(self) -> None:
        slug = skill_slug({"title": "one two three four five six seven eight nine ten"})
        assert slug == "one-two-three-four-five-six-seven-eight"

    def test_skill_slug_unusable_raises(self) -> None:
        with pytest.raises(RoutingError):
            skill_slug({"title": "!!!", "rule_text": "???"})

    def test_primary_project_precedence(self) -> None:
        assert primary_project({"primary_project": "/a", "projects": ["/b"]}) == "/a"
        assert primary_project({"projects": ["/b", "/c"]}) == "/b"
        assert primary_project({"projects_json": '["/d"]'}) == "/d"

    def test_primary_project_empty_raises(self) -> None:
        with pytest.raises(RoutingError):
            primary_project({"projects": []})


# ---------------------------------------------------------------------------
# never write an instruction file into somewhere that is not a repository
# ---------------------------------------------------------------------------


def _project_learning(project: str) -> dict:
    return {
        "id": "L1",
        "rule_text": "**Some project-scoped rule** with a real body.",
        "why": "w",
        "scope": "project",
        "scope_guess": "project",
        "project_count": 1,
        "primary_project_path": project,
        "projects_json": "[]",
    }


def test_a_non_repo_directory_is_never_a_write_target(tmp_path):
    """A session working directory alone cannot authorize an instruction-file target."""
    plain = tmp_path / "Downloads"
    plain.mkdir()

    with pytest.raises(RoutingError, match="not a git repository"):
        route(_project_learning(str(plain)), Config())


def test_a_git_repo_is_still_a_valid_write_target(tmp_path):
    """The guard must not block real projects."""
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(("git", "init", "-q"), cwd=repo, check=True, capture_output=True)

    decision = route(_project_learning(str(repo)), Config())
    assert decision.target_path == repo / "AGENTS.md"


def test_a_subdirectory_of_a_repo_is_allowed(tmp_path):
    """cwd is often a subdirectory; that is still inside a project."""
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(("git", "init", "-q"), cwd=repo, check=True, capture_output=True)
    sub = repo / "src" / "deep"
    sub.mkdir(parents=True)

    assert route(_project_learning(str(sub)), Config()).target_path.is_relative_to(repo)


def test_rule_files_also_refuse_a_non_repo_directory(tmp_path):
    """Path-scoped rule files require the same repository guard as AGENTS.md."""
    plain = tmp_path / "invented-sync-folder" / "Documents"
    plain.mkdir(parents=True)
    learning = {
        "id": "L2",
        "rule_text": "**A path-scoped rule** with a real body to slug from.",
        "why": "w",
        "scope": "rule_path",
        "scope_guess": "rule_path",
        "path_globs": ["src/**/*.py"],
        "path_globs_json": '["src/**/*.py"]',
        "project_count": 1,
        "primary_project_path": str(plain),
        "projects_json": "[]",
    }
    with pytest.raises(RoutingError, match="not a git repository"):
        route(learning, Config())


def test_rule_files_are_fine_inside_a_repo(tmp_path):
    import subprocess

    repo = _repo(tmp_path / "repo")
    learning = {
        "id": "L3",
        "rule_text": "**A path-scoped rule** with a real body to slug from.",
        "why": "w",
        "scope": "rule_path",
        "scope_guess": "rule_path",
        "path_globs": ["src/**/*.py"],
        "path_globs_json": '["src/**/*.py"]',
        "project_count": 1,
        "primary_project_path": str(repo),
        "projects_json": "[]",
    }
    d = route(learning, Config())
    assert d.target_path == repo / ".claude" / "rules" / d.target_path.name


# ---------------------------------------------------------------------------
# Three guards on the write path that had never run
# ---------------------------------------------------------------------------


def test_a_rule_file_is_refused_outside_a_git_repo(tmp_path):
    """`.claude/rules/<slug>.md` is a project WRITE, and it reaches routing by a
    different path from AGENTS.md.

    Writing a rules directory into `~/Downloads` or a Box cloud-storage folder
    is no better than writing an AGENTS.md there: nothing versions it, nothing
    reviews it, and rollback has nothing to restore from. The guard existed and
    had never been reached by a test.
    """
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    learning = {"id": "L1", "title": "Some rule", "primary_project_path": str(plain)}
    # "rule file into", not just "refusing to write": the AGENTS.md branch
    # raises a near-identical sentence, and a match on the shared half would
    # pass if the wrong guard fired.
    with pytest.raises(RoutingError, match="refusing to write a rule file into"):
        rule_file_path(learning)


def test_a_rule_file_is_refused_when_the_project_is_gone(tmp_path):
    """Routing never aims at a directory that is not there."""
    learning = {"id": "L2", "title": "Some rule",
                "primary_project_path": str(tmp_path / "deleted")}
    with pytest.raises(RoutingError, match="does not exist"):
        rule_file_path(learning)


def test_a_learning_with_no_project_at_all_is_named_in_the_error(tmp_path):
    """The operator sees this in a taxonomy key. It must name the learning and
    say what was missing, not just fail."""
    with pytest.raises(RoutingError, match="has no primary project"):
        primary_project({"id": "L3"})


def test_a_slug_that_cannot_be_derived_says_so(tmp_path):
    """A title of pure punctuation yields no words. Guessing a slug would put
    the rule in a directory nobody can find again."""
    with pytest.raises(RoutingError, match="cannot derive a skill slug"):
        skill_slug({"id": "L4", "title": "!!! ---", "rule_text": "***"})


def test_corrupt_projects_json_raises_rather_than_routing_nowhere():
    """`projects_json` is a strict-JSON TEXT column. If it will not parse, the
    learning has no usable project list — and falling through to "no primary
    project" would report the wrong cause, because the projects ARE recorded,
    just unreadable. The operator sees this string in a taxonomy key."""
    with pytest.raises(RoutingError, match="projects_json is not valid JSON"):
        primary_project({"id": "L5", "projects_json": "{not json"})


def test_a_valid_projects_json_still_routes():
    """Narrowness: the guard must not swallow the working path."""
    import json as _json

    got = primary_project({"id": "L6", "projects_json": _json.dumps(
        ["/tmp/whatever/repo-1", "/tmp/whatever/repo-0"])})
    assert got.endswith("repo-0"), got


def test_every_scope_the_miner_can_emit_is_routed_deliberately():
    """`miner.MINE_SCOPES` is what the mine response may say; `routing` decides
    where each one is written. Nothing bound the two.

    A scope added to the miner with no routing branch does not fail — it falls
    through to the PROJECT path, so a rule meant for a skill or a hook quietly
    lands in someone's `AGENTS.md`. A routing branch for a scope the miner can
    never emit is dead code that reads as coverage.

    `project` is the deliberate fall-through and is named here rather than
    discovered, so adding a second implicit one fails.
    """
    import ast
    from pathlib import Path

    from self_improve import routing as routing_mod
    from self_improve.miner import MINE_SCOPES

    source = Path(routing_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    branched: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name):
            if node.left.id != "scope":
                continue
            for comparator in node.comparators:
                if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str):
                    branched.add(comparator.value)
    assert len(branched) >= 4, (
        f"the branch scanner found only {sorted(branched)}; it is looking at nothing"
    )

    FALL_THROUGH = {"project"}
    unrouted = set(MINE_SCOPES) - branched - FALL_THROUGH
    assert unrouted == set(), (
        f"the miner can emit {sorted(unrouted)} and routing has no branch for it, "
        "so it falls through to the project path"
    )
    dead = branched - set(MINE_SCOPES)
    assert dead == set(), (
        f"routing branches on {sorted(dead)}, which the miner's contract rejects"
    )
