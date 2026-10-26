"""Each delivery call site uses the configured delivery path, not the load path.

Every Config here loads instructions from one temporary folder and writes to a
different one, so a site that reads the load path fails its test.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

from self_improve.config import Config
from self_improve.propose import build_proposal
from self_improve.routing import route
from self_improve.store import Store
from tests.test_apply import insert_proposal, make_diff


def _cfg(tmp_path: Path, **kw) -> Config:
    return Config(global_claude_md=str(tmp_path / "load" / "CLAUDE.md"),
                  codex_global_agents_md=str(tmp_path / "load-codex" / "AGENTS.md"),
                  skills_dir=str(tmp_path / "load" / "skills"), state_dir=str(tmp_path / "state"),
                  targets=(("global_public", str(tmp_path / "clone" / "AGENTS.md")),
                           ("skills_dir", str(tmp_path / "clone" / "skills"))), **kw)


def _learning(**kw):
    base = {"id": "abc123", "title": "Verify model identity",
            "rule_text": "Assert the reported model matches the requested class.",
            "project_count": 5, "projects": ["/tmp/does-not-matter"], "source": "claude",
            "scope_guess": "global"}
    base.update(kw)
    return base


def test_budget_guard_demotes_an_add_at_the_delivery_target_into_the_delivery_skills(tmp_path):
    cfg = _cfg(tmp_path, global_claude_md_line_budget=3)
    decision = route(_learning(), cfg)
    assert decision.target_path == tmp_path / "clone" / "AGENTS.md"
    proposal = build_proposal(_learning(), decision, "one\ntwo\nthree\n", cfg)
    assert proposal["action"] == "new_skill"
    assert Path(proposal["target_path"]).is_relative_to(tmp_path / "clone" / "skills")


def test_dashboard_line_budget_measures_the_delivery_target(tmp_path):
    from self_improve.dashboard.queries import _line_budget
    cfg = _cfg(tmp_path)
    delivery = tmp_path / "clone" / "AGENTS.md"
    delivery.parent.mkdir(parents=True)
    delivery.write_text("a\nb\n")
    assert _line_budget(cfg, str(delivery), 1)["used"] == 2
    assert _line_budget(cfg, cfg.global_claude_md, 1) == {}


def test_review_preview_budgets_the_delivery_target(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from self_improve.dashboard.app import create_app
    target = tmp_path / "clone" / "AGENTS.md"
    target.parent.mkdir(parents=True)
    before = "one\ntwo\n"
    target.write_text(before)
    cfg = replace(_cfg(tmp_path), global_claude_md_line_budget=6)
    with closing(Store(cfg.state_path("state.db"))) as store:
        p = insert_proposal(store, target=target, diff=make_diff(before, before + "three\n"), status="pending")
        store.commit()
        with TestClient(create_app(cfg)) as client:
            shown = client.get("/api/review-preview", params={"proposal_ids": p["id"]}).json()
    assert shown["targets"][0]["budget"] == {"before": 2, "after": 3, "limit": 6, "over": False}


def test_recovery_offers_delivery_globals_and_the_private_file(tmp_path, monkeypatch):
    from self_improve import recovery_jobs
    from tests.test_miner_agentic import seed_learning
    private = tmp_path / "private" / "personal.md"
    private.parent.mkdir()
    private.write_text("# Personal\n")
    cfg = replace(_cfg(tmp_path), targets=(("global_private", str(private)),
                                           ("global_public", str(tmp_path / "clone" / "AGENTS.md")),
                                           ("skills_dir", str(tmp_path / "clone" / "skills"))))
    with closing(Store(cfg.state_path("state.db"))) as store:
        learning = seed_learning(store, rule="Inspect the original request identifier before retrying.")
        opts = recovery_jobs.options(store, cfg, learning["id"])
    paths = {(t["destination"]["target_kind"], t["destination"]["target_path"]) for t in opts["targets"]}
    assert ("global_claude_md", str(tmp_path / "clone" / "AGENTS.md")) in paths
    assert ("global_private", str(private)) in paths
    assert not any(p == cfg.global_claude_md for _, p in paths)
    skills = [p for k, p in paths if k == "skill"]
    assert skills and all(Path(p).is_relative_to(tmp_path / "clone" / "skills") for p in skills)
