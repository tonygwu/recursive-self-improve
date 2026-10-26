"""The in-force instruction corpus: user rule files, one entry per real file,
and unreadable files recorded instead of aborting the run.

Every path is a temporary tree; nothing reads the operator's home.
"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path

import pytest

from self_improve.cluster import existing_rule_vectors
from self_improve.config import Config
from self_improve.miner import gather_in_force_instructions
from self_improve.pipeline import derive_run_status, run_pipeline
from self_improve.routing import instruction_target_paths
from tests.e2e_corpus import ScriptedLLM, build_corpus, mine_payload


class FakeEmbedder:
    def cached_vector(self, owner_kind, owner_key, text):
        return [float(len(text))]


def _layout(tmp_path):
    """A home where both global entrypoints are symlinks to one shared body."""
    shared = tmp_path / "skills" / "context" / "global" / "AGENTS.md"
    shared.parent.mkdir(parents=True)
    shared.write_text("- **Shared public rule** applies to every harness.\n")
    claude = tmp_path / "home" / ".claude"
    codex = tmp_path / "home" / ".codex"
    (claude / "rules").mkdir(parents=True)
    codex.mkdir(parents=True)
    (claude / "CLAUDE.md").symlink_to(shared)
    (codex / "AGENTS.md").symlink_to(shared)
    personal = claude / "rules" / "personal.md"
    personal.write_text("- **Private laptop rule** names this machine's accounts.\n")
    cfg = Config(global_claude_md=str(claude / "CLAUDE.md"),
                 codex_global_agents_md=str(codex / "AGENTS.md"),
                 skills_dir=str(claude / "skills"))
    return cfg, personal


def test_corpus_lists_user_rules_and_one_entry_per_real_file(tmp_path):
    cfg, personal = _layout(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    (project / "AGENTS.md").write_text("- project rule\n")
    (project / "CLAUDE.md").symlink_to("AGENTS.md")

    paths = instruction_target_paths([str(project)], cfg)

    assert paths == [cfg.global_claude_md, str(personal), str(project / "AGENTS.md")]


def test_corpus_includes_nested_user_rules(tmp_path):
    cfg, personal = _layout(tmp_path)
    nested = personal.parent / "topic" / "time.md"
    nested.parent.mkdir()
    nested.write_text("- nested rule\n")

    assert str(nested) in instruction_target_paths([], cfg)


def test_miner_context_quotes_user_rules_and_marks_repeats(tmp_path):
    cfg, personal = _layout(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    (project / "AGENTS.md").write_text("- project rule text\n")
    (project / "CLAUDE.md").symlink_to("AGENTS.md")

    text = gather_in_force_instructions(str(project), cfg)

    assert f"=== global rule personal.md ({personal}) ===" in text
    assert "Private laptop rule" in text
    assert text.count("project rule text") == 1
    assert f"=== project CLAUDE.md ({project / 'CLAUDE.md'}) — SAME FILE AS project AGENTS.md ===" in text


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")
def test_unreadable_file_is_recorded_only_when_the_caller_asks(tmp_path):
    good = tmp_path / "good.md"
    good.write_text("- **Keep this readable rule** in the comparison.\n")
    locked = tmp_path / "locked.md"
    locked.write_text("- **A rule nobody may read** today.\n")
    locked.chmod(0)
    try:
        with pytest.raises(PermissionError):
            existing_rule_vectors(Config(), [str(good), str(locked)], FakeEmbedder())
        unreadable: list[dict] = []
        vecs = existing_rule_vectors(Config(), [str(good), str(locked)], FakeEmbedder(),
                                     unreadable=unreadable)
    finally:
        locked.chmod(0o644)
    assert [u for u, _ in vecs] == ["**Keep this readable rule** in the comparison."]
    assert [(u["path"], u["cause"]) for u in unreadable] == [(str(locked), "PermissionError")]


def test_unreadable_instruction_files_degrade_the_run_with_their_names():
    status, reasons = derive_run_status({"cluster": {
        "candidates": 0, "mode": "agentic_passthrough",
        "unreadable_instruction_files": [{"path": "/x/AGENTS.md", "cause": "PermissionError", "detail": "no"}]}})
    assert status == "degraded"
    assert reasons == ["cluster: 1 in-force instruction file(s) unreadable and left out of "
                       "duplicate detection: /x/AGENTS.md (PermissionError)"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")
def test_pipeline_finishes_degraded_when_a_session_folder_is_unreadable(tmp_path):
    corpus = build_corpus(tmp_path)
    locked = corpus.unrelated / "AGENTS.md"
    locked.write_text("- **A protected folder rule** nobody may read.\n")
    locked.chmod(0)
    llm = ScriptedLLM(mine_responses=[mine_payload(f"**Lesson {i}**") for i in range(8)])
    try:
        stats = run_pipeline(corpus.cfg, corpus.store, review_only=True, _llm_factory=llm.factory())
    finally:
        locked.chmod(0o644)

    assert stats["cluster"]["unreadable_instruction_files"][0]["path"] == str(locked)
    assert "error" not in stats["contradictions"], stats["contradictions"]
    assert stats["contradictions"]["unreadable_instruction_files"][0]["path"] == str(locked)
    row = corpus.store.query_one("SELECT status FROM runs WHERE id=?", (stats["run_id"],))
    assert row["status"] == "degraded"
    assert [r.split(":")[0] for r in stats["status_reasons"] if str(locked) in r] == ["cluster", "contradictions"]
