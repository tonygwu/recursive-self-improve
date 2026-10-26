"""Routing decision boundary for the four destination families.

The deterministic half of the scope decision-boundary evals: given the miner's
scope guess and the learning's evidence, which instruction file is chosen.
The model half (does the guidance produce the right guess) is
``selfimprove eval-scope``; see tests/test_scope_boundary.py.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from self_improve.config import Config
from self_improve.routing import RoutingError, route
from tests.e2e_corpus import ScriptedLLM, build_corpus, mine_payload
from self_improve.pipeline import run_pipeline


def _cfg(tmp_path: Path, private: bool = True) -> Config:
    targets = [("global_public", str(tmp_path / "skills-clone" / "AGENTS.md")),
               ("skills_dir", str(tmp_path / "skills-clone" / "skills"))]
    if private:
        targets.append(("global_private", str(tmp_path / "home" / "rules" / "personal.md")))
    return Config(global_claude_md=str(tmp_path / "home" / "CLAUDE.md"),
                  codex_global_agents_md=str(tmp_path / "codex" / "AGENTS.md"),
                  skills_dir=str(tmp_path / "home" / "skills"),
                  state_dir=str(tmp_path / "state"), targets=tuple(sorted(targets)))


def _repo(path: Path) -> Path:
    import subprocess
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(("git", "init", "-q"), cwd=path, check=True, capture_output=True)
    return path


def _learning(tmp_path, **kw):
    base = {"id": "l1", "title": "t", "rule_text": "Keep the rule short.", "project_count": 1,
            "projects": [str(_repo(tmp_path / "proj"))], "source": "claude", "scope_guess": "project"}
    base.update(kw)
    return base


@pytest.mark.parametrize("scope, project_count, source, expected_kind, expected_rel", [
    ("global", 1, "claude", "global_claude_md", "skills-clone/AGENTS.md"),
    ("personal", 1, "claude", "global_private", "home/rules/personal.md"),
    # Personal outranks the project-count promotion: a personal rule seen in
    # every project is still personal and must not reach the shared file.
    ("personal", 99, "claude", "global_private", "home/rules/personal.md"),
    ("personal", 5, "codex", "global_private", "home/rules/personal.md"),
    ("project", 1, "claude", "project_agents_md", None),
    ("skill", 1, "claude", "skill", None),
    # The documented promotion still lifts project and skill guesses.
    ("project", 99, "claude", "global_claude_md", "skills-clone/AGENTS.md"),
])
def test_scope_routes_to_its_destination_family(tmp_path, scope, project_count, source, expected_kind, expected_rel):
    cfg = _cfg(tmp_path)
    decision = route(_learning(tmp_path, scope_guess=scope, project_count=project_count, source=source), cfg)
    assert decision.target_kind == expected_kind
    if expected_rel:
        assert decision.target_path == tmp_path / expected_rel
    if expected_kind == "skill":
        assert decision.target_path.is_relative_to(tmp_path / "skills-clone" / "skills")
    if expected_kind == "project_agents_md":
        assert decision.target_path.is_relative_to(tmp_path / "proj")


def test_an_ignored_personal_rule_still_becomes_a_hook(tmp_path):
    decision = route(_learning(tmp_path, scope_guess="personal", violated_existing_rule="- quoted rule"), _cfg(tmp_path))
    assert decision.target_kind == "hook"


def test_personal_without_a_private_target_refuses_instead_of_publishing(tmp_path):
    with pytest.raises(RoutingError, match="global_private"):
        route(_learning(tmp_path, scope_guess="personal", project_count=99), _cfg(tmp_path, private=False))


def test_pipeline_writes_a_personal_lesson_as_a_private_proposal(tmp_path):
    corpus = build_corpus(tmp_path)
    private = tmp_path / "private" / "personal.md"
    private.parent.mkdir()
    private.write_text("# Personal\n")
    cfg = dataclasses.replace(corpus.cfg, targets=(("global_private", str(private)),))
    llm = ScriptedLLM(mine_responses=[mine_payload(f"**Use the work account alias {i}** for work mail",
                                                    scope_guess="personal") for i in range(8)])
    run_pipeline(cfg, corpus.store, review_only=True, _llm_factory=llm.factory())

    rows = corpus.store.query("SELECT target_kind, target_path FROM proposals")
    assert rows, "fixture produced no proposals"
    assert {(r["target_kind"], r["target_path"]) for r in rows} == {("global_private", str(private))}
    assert private.read_text() == "# Personal\n", "review-only must not write the private file"


def test_a_merged_personal_lesson_stays_personal_on_later_runs(tmp_path, monkeypatch):
    """The merge keeps a personal member personal, and the stored row must too.

    Routing reads the stored scope on every later run. No private target is
    configured, so a personal lesson must never be proposed at all.
    """
    import json
    from self_improve import miner
    from self_improve.llm import LLMRunner, _ExecResult
    from tests.test_miner_agentic import agentic_payload
    corpus = build_corpus(tmp_path)
    cfg = dataclasses.replace(corpus.cfg, mine_mode="fast", cluster_group_cosine=-1.0)
    base = {k: v for k, v in agentic_payload().items() if k not in miner.MINE_AGENTIC_EXTRA_KEYS}
    mined = []

    def execute(self, call_id, stage, *args, **kwargs):
        if stage == "mine":
            # Only the last mined lesson is personal, so the merge's
            # representative (the earliest) carries a global scope.
            value = {**base, "scope_guess": "global" if len(mined) < 3 else "personal"}
            mined.append(value["scope_guess"])
        elif stage == "cluster":
            value = {"decision": "merge", "generalized_rule": "Use the work alias for mail.",
                     "why": "w", "title": "T", "category": "process", "scope_guess": "global"}
        else:
            return _ExecResult.fail("parse_error")
        return _ExecResult(ok=True, text=json.dumps(value), parsed=value, outcome="ok",
                           provider="claude", model_reported="claude-sonnet-4-6")

    monkeypatch.setattr(LLMRunner, "_execute", execute)
    first = run_pipeline(cfg, corpus.store, review_only=True)
    assert first["cluster"]["merge_succeeded"] >= 1 and "personal" in mined, (first["cluster"], mined)
    merged = corpus.store.query_one(
        "SELECT * FROM learnings WHERE rule_text='Use the work alias for mail.'")
    assert merged["scope"] == "personal"
    assert (merged["title"], merged["category"]) == ("T", "process")
    run_pipeline(cfg, corpus.store, review_only=True)
    assert not corpus.store.query(
        "SELECT * FROM proposals WHERE learning_id=?", (merged["id"],))


def test_a_private_file_outside_rules_joins_the_corpus_once(tmp_path):
    from self_improve.miner import gather_in_force_instructions
    from self_improve.routing import instruction_target_paths
    private = tmp_path / "elsewhere" / "personal.md"
    private.parent.mkdir()
    private.write_text("- **A private rule kept outside rules/** for this machine.\n")
    cfg = Config(global_claude_md=str(tmp_path / "home" / "CLAUDE.md"),
                 codex_global_agents_md=str(tmp_path / "codex" / "AGENTS.md"),
                 skills_dir=str(tmp_path / "home" / "skills"), state_dir=str(tmp_path / "state"),
                 targets=(("global_private", str(private)),))
    assert instruction_target_paths([], cfg).count(str(private)) == 1
    text = gather_in_force_instructions("", cfg)
    assert f"=== private rules ({private}) ===" in text and "kept outside rules/" in text


def test_a_personal_candidate_is_not_absorbed_into_an_open_shared_lesson(tmp_path, monkeypatch):
    import json
    from self_improve import miner
    from self_improve.llm import LLMRunner, _ExecResult
    from tests.test_miner_agentic import agentic_payload, seed_learning
    corpus = build_corpus(tmp_path)
    cfg = dataclasses.replace(corpus.cfg, mine_mode="fast", cluster_group_cosine=-1.0)
    base = {k: v for k, v in agentic_payload().items() if k not in miner.MINE_AGENTIC_EXTRA_KEYS}

    def execute(self, call_id, stage, *args, **kwargs):
        if stage == "mine":
            value = {**base, "scope_guess": "global"}
        elif stage == "cluster":
            value = {"decision": "merge", "generalized_rule": "Shared rule.", "why": "w",
                     "title": "T", "category": "process", "scope_guess": "global"}
        else:
            return _ExecResult.fail("parse_error")
        return _ExecResult(ok=True, text=json.dumps(value), parsed=value, outcome="ok",
                           provider="claude", model_reported="claude-sonnet-4-6")

    monkeypatch.setattr(LLMRunner, "_execute", execute)
    run_pipeline(cfg, corpus.store, review_only=True)
    open_rows = corpus.store.query(
        "SELECT l.id FROM learnings l WHERE l.status='proposed' AND (SELECT p.status FROM proposals p "
        "WHERE p.learning_id=l.id ORDER BY p.created_at DESC LIMIT 1) IN ('pending','held')")
    assert open_rows, "fixture produced no open shared lesson"
    personal = seed_learning(corpus.store, status="candidate", rule="**Use the work alias** for this laptop.")
    corpus.store.update("learnings", "id", personal["id"], {"scope": "personal"}); corpus.store.commit()
    run_pipeline(cfg, corpus.store, review_only=True)
    row = corpus.store.query_one("SELECT status, duplicate_of FROM learnings WHERE id=?", (personal["id"],))
    assert row["status"] != "superseded" and not row["duplicate_of"], dict(row)


def test_a_second_personal_observation_joins_the_open_personal_lesson(tmp_path, monkeypatch):
    import json
    from self_improve import miner
    from self_improve.llm import LLMRunner, _ExecResult
    from tests.test_miner_agentic import agentic_payload, seed_learning
    corpus = build_corpus(tmp_path)
    private = tmp_path / "private" / "personal.md"
    private.parent.mkdir(); private.write_text("# Personal\n")
    cfg = dataclasses.replace(corpus.cfg, mine_mode="fast", cluster_group_cosine=-1.0,
                              targets=(("global_private", str(private)),))
    base = {k: v for k, v in agentic_payload().items() if k not in miner.MINE_AGENTIC_EXTRA_KEYS}

    def execute(self, call_id, stage, *args, **kwargs):
        if stage == "mine":
            value = {**base, "scope_guess": "global"}
        elif stage == "cluster":
            value = {"decision": "merge", "generalized_rule": "Shared rule.", "why": "w",
                     "title": "T", "category": "process", "scope_guess": "global"}
        else:
            return _ExecResult.fail("parse_error")
        return _ExecResult(ok=True, text=json.dumps(value), parsed=value, outcome="ok",
                           provider="claude", model_reported="claude-sonnet-4-6")

    monkeypatch.setattr(LLMRunner, "_execute", execute)
    run_pipeline(cfg, corpus.store, review_only=True)
    open_rows = corpus.store.query(
        "SELECT l.id FROM learnings l WHERE l.status='proposed' AND (SELECT p.status FROM proposals p "
        "WHERE p.learning_id=l.id ORDER BY p.created_at DESC LIMIT 1) IN ('pending','held')")
    assert open_rows, "fixture produced no open shared lesson"
    personal_proposals = lambda: corpus.store.query(
        "SELECT p.id FROM proposals p WHERE p.target_kind='global_private' AND p.status IN ('pending','held')")
    for rule in ("**Use the work alias** for this laptop.", "**Use the work alias** on this machine."):
        row = seed_learning(corpus.store, status="candidate", rule=rule)
        corpus.store.update("learnings", "id", row["id"], {"scope": "personal"}); corpus.store.commit()
        run_pipeline(cfg, corpus.store, review_only=True)
    assert len(personal_proposals()) == 1, "a second personal observation created a duplicate proposal"
    second = corpus.store.query_one("SELECT status, duplicate_of FROM learnings WHERE rule_text LIKE '%on this machine.'")
    assert second["status"] == "superseded" and second["duplicate_of"]
