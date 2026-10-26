"""Configured working directories are never instruction sources or targets.

A temporary folder stands in for a protected one (a cloud-storage folder that a
scheduled job cannot read). Its AGENTS.md exists but cannot be opened, so any
reader that still opens it fails the way the nightly did. Every reader that
derives instruction files from recorded session folders is reached through its
real entry point; none of them may open the excluded file.
"""
from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from self_improve import incident_jobs, instruction_exclusions, miner, recovery_jobs
from self_improve import rule_availability as availability
from self_improve.config import Config, ConfigError, load_config
from self_improve.pipeline import _target_paths
from self_improve.search import search_learnings
from tests.test_apply import cfg, store, init_git_repo, insert_proposal  # noqa: F401 - fixtures
from tests.test_miner_agentic import seed_incident, seed_learning
from tests.test_rule_availability import known_copy

SENTINEL = "ExcludedFolderSentinel: never read this rule."


def _protected(root: Path) -> Path:
    """A folder whose AGENTS.md exists but refuses to open, like CloudStorage under launchd."""
    root.mkdir(parents=True, exist_ok=True)
    agents = root / "AGENTS.md"
    agents.write_text(f"- {SENTINEL}\n")
    (root / "CLAUDE.md").symlink_to("AGENTS.md")
    agents.chmod(0)
    return root


@pytest.fixture
def env(cfg, store, tmp_path):  # noqa: F811 - pytest fixture injection
    if os.geteuid() == 0:
        pytest.skip("root can open a mode-0 file, so the stand-in is not protected")
    cfg = replace(cfg, global_claude_md=str(tmp_path / "home" / "CLAUDE.md"),
                  codex_global_agents_md=str(tmp_path / "codex" / "AGENTS.md"),
                  skills_dir=str(tmp_path / "home" / "skills"))
    protected = _protected(tmp_path / "Cloud" / "Taxes")
    nested = protected / "Handoff 2026-10-08"
    nested.mkdir()
    ordinary = tmp_path / "ordinary"
    ordinary.mkdir()
    (ordinary / "AGENTS.md").write_text("- Ordinary project rule about invented fixtures.\n")
    for i, path in enumerate((protected, nested, ordinary)):
        store.insert("sessions", {"file_path": str(tmp_path / f"s{i}.jsonl"), "source": "codex",
                                  "project_path": str(path), "project_key": "path:" + str(path),
                                  "first_ts": "2026-10-13T00:00:00Z"})
    store.commit()
    yield cfg, store, protected, ordinary
    (protected / "AGENTS.md").chmod(0o644)


def excluding(cfg, protected):
    return replace(cfg, instruction_excluded_dirs=(str(protected),))


def test_the_stand_in_reproduces_the_nightly_failure(env):
    cfg, store, protected, _ = env
    paths = _target_paths(store, cfg)
    assert str(protected / "AGENTS.md") in paths
    with pytest.raises(PermissionError):
        (protected / "AGENTS.md").read_text()


def test_pipeline_corpus_omits_the_excluded_folder_and_its_subfolders(env):
    cfg, store, protected, ordinary = env
    paths = _target_paths(store, excluding(cfg, protected))
    assert not [p for p in paths if p.startswith(str(protected))]
    assert str(ordinary / "AGENTS.md") in paths
    for p in paths:
        if Path(p).is_file():
            Path(p).read_text()


def test_miner_search_tool_never_reads_the_excluded_folder(env):
    cfg, store, protected, _ = env
    # Readable here: an unreadable file is already skipped with a warning, so
    # only a readable one shows whether the exclusion itself keeps it out.
    (protected / "AGENTS.md").chmod(0o644)
    found = search_learnings(cfg, "never read this rule", top_k=20)
    assert any(SENTINEL in json.dumps(r) for r in found)
    results = search_learnings(excluding(cfg, protected), "never read this rule", top_k=20)
    assert all(SENTINEL not in json.dumps(r) for r in results)


def test_in_force_block_marks_the_project_files_excluded_without_reading_them(env):
    cfg, _, protected, _ = env
    text = miner.gather_in_force_instructions(str(protected / "Handoff 2026-10-08"),
                                              excluding(cfg, protected))
    assert "EXCLUDED" in text and "UNREADABLE" not in text
    assert "instruction_excluded_dirs" in text


def test_incident_job_snapshot_excludes_the_folder(env):
    cfg, store, protected, _ = env
    cfg = replace(excluding(cfg, protected), mine_mode="fast")
    incident = seed_incident(store, str(protected.parent / "incident.jsonl"), project=str(protected))
    shown = incident_jobs.preview(store, cfg, incident["id"])
    frozen = json.dumps(shown, ensure_ascii=False)
    assert SENTINEL not in frozen
    assert "UNREADABLE" not in frozen
    assert shown["source"]["snapshot"]["coverage"]["unreadable_instructions"] == []


def test_availability_collection_never_inspects_an_excluded_copy(env, tmp_path, monkeypatch):
    cfg, store, protected, _ = env
    cfg = replace(cfg, claude_managed_dir=str(tmp_path / "managed"))
    inside = init_git_repo(protected / "repo", "AGENTS.md", "# Human\n")
    outside = init_git_repo(tmp_path / "control", "AGENTS.md", "# Human\n")
    known_copy(store, inside)
    known_copy(store, outside)
    seen = []
    real = availability.inspect_surfaces
    def spy(cfg_, path, **kw):
        seen.append(path)
        return real(cfg_, path, **kw)
    monkeypatch.setattr(availability, "inspect_surfaces", spy)
    availability.collect_availability(store, excluding(cfg, protected), observed_at="2030-01-01T00:00:00Z")
    assert str(outside) in seen
    assert not [p for p in seen if p.startswith(str(protected))]
    records = [r["record_json"] for r in store.query("SELECT record_json FROM instruction_inventories")]
    assert records and all(str(protected) not in r for r in records)


def test_recovery_never_offers_a_destination_in_an_excluded_copy(env):
    cfg, store, protected, _ = env
    repo = init_git_repo(protected / "repo", "AGENTS.md", "# Human\n")
    known_copy(store, repo)
    learning = seed_learning(store, rule="Inspect the invented identifier before retrying.")
    Path(cfg.global_claude_md).parent.mkdir(parents=True, exist_ok=True)
    Path(cfg.global_claude_md).write_text("# Rules\n")
    included = recovery_jobs.options(store, cfg, learning["id"])
    assert any(str(repo) in t["destination"]["target_path"] for t in included["targets"])
    excluded = recovery_jobs.options(store, excluding(cfg, protected), learning["id"])
    assert not [t for t in excluded["targets"] if str(protected) in t["destination"]["target_path"]]


def test_recovery_never_offers_an_earlier_proposal_target_in_an_excluded_copy(env):
    cfg, store, protected, _ = env
    repo = init_git_repo(protected / "repo", "AGENTS.md", "# Human\n")
    learning = seed_learning(store, rule="Inspect the invented identifier before retrying.")
    earlier = insert_proposal(store, target=repo / "AGENTS.md", diff="", target_kind="project_agents_md")
    store.update("proposals", "id", earlier["id"], {"learning_id": learning["id"]})
    store.commit()
    Path(cfg.global_claude_md).parent.mkdir(parents=True, exist_ok=True)
    Path(cfg.global_claude_md).write_text("# Rules\n")
    included = recovery_jobs.options(store, cfg, learning["id"])
    assert any(str(repo) in t["destination"]["target_path"] for t in included["targets"])
    excluded = recovery_jobs.options(store, excluding(cfg, protected), learning["id"])
    assert not [t for t in excluded["targets"] if str(protected) in t["destination"]["target_path"]]


def test_matching_is_by_whole_path_components(tmp_path):
    cfg = Config(state_dir=str(tmp_path), instruction_excluded_dirs=("/a/Taxes",))
    assert instruction_exclusions.excluded_by(cfg, "/a/Taxes") == "/a/Taxes"
    assert instruction_exclusions.excluded_by(cfg, "/a/Taxes/2025/x") == "/a/Taxes"
    assert instruction_exclusions.excluded_by(cfg, "/a/Taxes/../Other") == ""
    assert instruction_exclusions.excluded_by(cfg, "/a/Taxes2025") == ""
    assert instruction_exclusions.excluded_by(cfg, "") == ""


@pytest.mark.parametrize("bad", ["relative/dir", "", "~/Taxes"])
def test_entries_must_be_absolute(tmp_path, bad):
    with pytest.raises(ConfigError, match="instruction_excluded_dirs"):
        Config(state_dir=str(tmp_path), instruction_excluded_dirs=(bad,))


def test_private_toml_sets_the_exclusion(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('instruction_excluded_dirs = ["/invented/Cloud/Taxes"]\n')
    assert load_config(path).instruction_excluded_dirs == ("/invented/Cloud/Taxes",)


@pytest.mark.parametrize("scope,written", [("project", "AGENTS.md"),
                                           ("rule_path", ".claude/rules")])
def test_routing_refuses_a_project_write_into_an_excluded_repository(cfg, tmp_path, scope, written):  # noqa: F811
    from self_improve.routing import RoutingError, route
    repo = init_git_repo(tmp_path / "Cloud" / "Taxes", "AGENTS.md", "# Human\n")
    learning = {"id": "invented", "scope": scope, "scope_guess": scope, "project_count": 1,
                "rule_text": "Invented rule.", "projects_json": json.dumps([str(repo)]),
                "primary_project_path": str(repo)}
    assert str(route(learning, cfg).target_path).startswith(str(repo / written))
    with pytest.raises(RoutingError, match="instruction_excluded_dirs"):
        route(learning, excluding(cfg, repo))
