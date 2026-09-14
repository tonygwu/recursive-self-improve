"""Tests for propose: bullet diffs, strict patch application, budget demotion."""

from __future__ import annotations

from pathlib import Path

import pytest

from self_improve.config import Config
from self_improve.propose import (
    SECTION_HEADER,
    PatchConflict,
    ProposalError,
    append_learned_rule,
    apply_unified_diff,
    build_proposal,
    format_bullet,
    is_machine_line,
    line_marker_id,
    make_unified_diff,
)
from self_improve.routing import RouteDecision, route

BULLET = (
    "- **Verify model identity.** Assert the reported model matches the "
    "requested class. <!-- si:abc123 -->"
)


def make_cfg(tmp_path: Path, **overrides) -> Config:
    defaults = dict(
        global_claude_md=str(tmp_path / "dot-claude" / "CLAUDE.md"),
        codex_global_agents_md=str(tmp_path / "dot-codex" / "AGENTS.md"),
        skills_dir=str(tmp_path / "dot-claude" / "skills"),
        state_dir=str(tmp_path / "state"),
    )
    defaults.update(overrides)
    return Config(**defaults)


def learning(**kw) -> dict:
    base = {
        "id": "abc123",
        "title": "Verify model identity",
        "rule_text": "Assert the reported model matches the requested class.",
        "why": "CLIs silently substitute models.",
        "project_count": 5,
        "projects": ["/tmp/whatever"],
        "source": "claude",
    }
    base.update(kw)
    return base


# ----------------------------------------------------------------------------
# Marker-based line classification.
# ----------------------------------------------------------------------------


class TestMarkers:
    def test_machine_line(self) -> None:
        assert is_machine_line(BULLET) is True
        assert line_marker_id(BULLET) == "abc123"

    def test_human_line(self) -> None:
        assert is_machine_line("- **Show evidence, not assertions.** Include output.") is False
        assert line_marker_id("plain text") is None

    def test_marker_with_padding(self) -> None:
        assert line_marker_id("- rule <!--  si:deadbeef  -->") == "deadbeef"

    def test_format_bullet(self) -> None:
        assert format_bullet(learning()) == BULLET

    def test_format_bullet_lead_from_rule_text_when_no_title(self) -> None:
        b = format_bullet(
            {"id": "x1", "title": "", "rule_text": "Read the raw response first."}
        )
        assert b == (
            "- **Read the raw response first.** Read the raw response first. <!-- si:x1 -->"
        )

    def test_format_bullet_empty_rule_raises(self) -> None:
        with pytest.raises(ProposalError):
            format_bullet({"id": "x1", "title": "T", "rule_text": ""})


# ----------------------------------------------------------------------------
# Diff round-trips (build then apply reproduces expected content, byte-identical).
# ----------------------------------------------------------------------------


class TestAddRoundTrip:
    def test_creates_section_at_eof(self, tmp_path: Path) -> None:
        cfg = make_cfg(tmp_path)
        content = "# My project\n\nSome intro.\n"
        decision = route(learning(project_count=5), cfg)  # -> global CLAUDE.md
        proposal = build_proposal(learning(), decision, content, cfg)
        assert proposal["action"] == "add"
        assert proposal["target_kind"] == "global_claude_md"
        assert proposal["marker"] == "si:abc123"
        expected = (
            "# My project\n\nSome intro.\n\n" + SECTION_HEADER + "\n\n" + BULLET + "\n"
        )
        applied = apply_unified_diff(content, proposal["diff_unified"])
        assert applied == expected  # byte-identical

    def test_appends_after_last_bullet_in_existing_section(self, tmp_path: Path) -> None:
        cfg = make_cfg(tmp_path)
        old_bullet = "- **Old.** Old rule. <!-- si:old1 -->"
        content = (
            "# P\n\n" + SECTION_HEADER + "\n\n" + old_bullet + "\n\n## Other\n\nstuff\n"
        )
        decision = route(learning(project_count=5), cfg)
        proposal = build_proposal(learning(), decision, content, cfg)
        expected = (
            "# P\n\n"
            + SECTION_HEADER
            + "\n\n"
            + old_bullet
            + "\n"
            + BULLET
            + "\n\n## Other\n\nstuff\n"
        )
        applied = apply_unified_diff(content, proposal["diff_unified"])
        assert applied == expected

    def test_empty_file_gets_section(self) -> None:
        result = append_learned_rule("", BULLET)
        assert result == SECTION_HEADER + "\n\n" + BULLET + "\n"

    def test_duplicate_marker_raises(self, tmp_path: Path) -> None:
        cfg = make_cfg(tmp_path)
        content = SECTION_HEADER + "\n\n" + BULLET + "\n"
        decision = route(learning(project_count=5), cfg)
        with pytest.raises(ProposalError, match="already present"):
            build_proposal(learning(), decision, content, cfg)

    def test_trailing_newline_normalization_is_recorded(self, tmp_path: Path) -> None:
        cfg = make_cfg(tmp_path)
        content = "# P\n\nIntro without trailing newline"
        decision = route(learning(project_count=5), cfg)
        proposal = build_proposal(learning(), decision, content, cfg)
        assert proposal["normalized_trailing_newline"] is True
        applied = apply_unified_diff(content, proposal["diff_unified"])
        assert applied.endswith(BULLET + "\n")

    def test_trailing_newline_flag_false_when_terminated(self, tmp_path: Path) -> None:
        cfg = make_cfg(tmp_path)
        decision = route(learning(project_count=5), cfg)
        proposal = build_proposal(learning(), decision, "# P\n", cfg)
        assert proposal["normalized_trailing_newline"] is False


# ----------------------------------------------------------------------------
# Deletions: marker-based human/machine classification.
# ----------------------------------------------------------------------------

DELETE_CONTENT = (
    "# H\n\n- human-authored rule\n\n" + SECTION_HEADER + "\n\n" + BULLET + "\n"
)


def delete_route(cfg: Config) -> RouteDecision:
    return RouteDecision(
        target_path=Path(cfg.global_claude_md),
        target_kind="global_claude_md",
        action="delete",
    )


class TestDelete:
    def test_marked_line_stays_delete_and_round_trips(self, tmp_path: Path) -> None:
        cfg = make_cfg(tmp_path)
        lrn = {"id": "del1", "delete_marker_id": "abc123"}
        proposal = build_proposal(lrn, delete_route(cfg), DELETE_CONTENT, cfg)
        assert proposal["action"] == "delete"
        expected = "# H\n\n- human-authored rule\n\n" + SECTION_HEADER + "\n\n"
        assert apply_unified_diff(DELETE_CONTENT, proposal["diff_unified"]) == expected

    def test_unmarked_line_becomes_delete_human_line(self, tmp_path: Path) -> None:
        cfg = make_cfg(tmp_path)
        lrn = {"id": "del2", "delete_line": "- human-authored rule"}
        proposal = build_proposal(lrn, delete_route(cfg), DELETE_CONTENT, cfg)
        assert proposal["action"] == "delete_human_line"
        assert "review queue" in proposal["note"]
        expected = "# H\n\n\n" + SECTION_HEADER + "\n\n" + BULLET + "\n"
        assert apply_unified_diff(DELETE_CONTENT, proposal["diff_unified"]) == expected

    def test_delete_by_exact_line_with_marker_stays_delete(self, tmp_path: Path) -> None:
        cfg = make_cfg(tmp_path)
        lrn = {"id": "del3", "delete_line": BULLET}
        proposal = build_proposal(lrn, delete_route(cfg), DELETE_CONTENT, cfg)
        assert proposal["action"] == "delete"

    def test_delete_target_missing_raises(self, tmp_path: Path) -> None:
        cfg = make_cfg(tmp_path)
        lrn = {"id": "del4", "delete_line": "- not in the file"}
        with pytest.raises(ProposalError, match="not found"):
            build_proposal(lrn, delete_route(cfg), DELETE_CONTENT, cfg)

    def test_delete_target_ambiguous_raises(self, tmp_path: Path) -> None:
        cfg = make_cfg(tmp_path)
        content = "- dup\n- dup\n"
        lrn = {"id": "del5", "delete_line": "- dup"}
        with pytest.raises(ProposalError, match="ambiguous"):
            build_proposal(lrn, delete_route(cfg), content, cfg)

    def test_delete_without_selector_raises(self, tmp_path: Path) -> None:
        cfg = make_cfg(tmp_path)
        with pytest.raises(ProposalError, match="delete_line"):
            build_proposal({"id": "del6"}, delete_route(cfg), DELETE_CONTENT, cfg)


# ----------------------------------------------------------------------------
# New skill and hook conversion.
# ----------------------------------------------------------------------------


class TestSkillAndHook:
    def test_new_skill_diff_against_empty(self, tmp_path: Path) -> None:
        cfg = make_cfg(tmp_path)
        decision = route(learning(project_count=1, scope_guess="skill"), cfg)
        proposal = build_proposal(learning(project_count=1, scope_guess="skill"), decision, "", cfg)
        assert proposal["action"] == "new_skill"
        assert proposal["target_path"].endswith("verify-model-identity/SKILL.md")
        skill_md = apply_unified_diff("", proposal["diff_unified"])
        assert skill_md.startswith("---\nname: verify-model-identity\n")
        assert "<!-- si:abc123 -->" in skill_md
        assert "## Why" in skill_md

    def test_new_skill_against_existing_content_raises(self, tmp_path: Path) -> None:
        cfg = make_cfg(tmp_path)
        lrn = learning(project_count=1, scope_guess="skill")
        decision = route(lrn, cfg)
        with pytest.raises(ProposalError, match="already has content"):
            build_proposal(lrn, decision, "existing\n", cfg)

    def test_convert_to_hook_has_no_diff(self, tmp_path: Path) -> None:
        cfg = make_cfg(tmp_path)
        lrn = learning(project_count=1, scope_guess="hook")
        decision = route(lrn, cfg)
        proposal = build_proposal(lrn, decision, "{}\n", cfg)
        assert proposal["action"] == "convert_to_hook"
        assert proposal["diff_unified"] == ""
        assert "review queue" in proposal["note"]


# ----------------------------------------------------------------------------
# Global line-budget demotion.
# ----------------------------------------------------------------------------


class TestBudgetDemotion:
    def test_at_budget_demotes_to_skill(self, tmp_path: Path) -> None:
        cfg = make_cfg(tmp_path, global_claude_md_line_budget=3)
        content = "one\ntwo\nthree\n"  # 3 lines == budget -> demote
        decision = route(learning(project_count=5), cfg)
        proposal = build_proposal(learning(), decision, content, cfg)
        assert proposal["action"] == "new_skill"
        assert proposal["target_kind"] == "skill"
        assert proposal["target_path"] == str(
            Path(cfg.skills_dir) / "verify-model-identity" / "SKILL.md"
        )
        assert "demoted" in proposal["demotion_reason"]
        assert "3 lines >= budget 3" in proposal["demotion_reason"]
        skill_md = apply_unified_diff("", proposal["diff_unified"])
        assert "<!-- si:abc123 -->" in skill_md

    def test_under_budget_not_demoted(self, tmp_path: Path) -> None:
        cfg = make_cfg(tmp_path, global_claude_md_line_budget=10)
        decision = route(learning(project_count=5), cfg)
        proposal = build_proposal(learning(), decision, "one\ntwo\n", cfg)
        assert proposal["action"] == "add"
        assert proposal["demotion_reason"] == ""

    def test_budget_does_not_bite_other_targets(self, tmp_path: Path) -> None:
        cfg = make_cfg(tmp_path, global_claude_md_line_budget=1)
        lrn = learning(project_count=1, scope_guess="codex_global")
        decision = route(lrn, cfg)
        proposal = build_proposal(lrn, decision, "one\ntwo\nthree\n", cfg)
        assert proposal["action"] == "add"  # codex AGENTS.md has no such budget


# ----------------------------------------------------------------------------
# apply_unified_diff strictness.
# ----------------------------------------------------------------------------


class TestApplyStrictness:
    def test_conflict_on_drifted_content(self, tmp_path: Path) -> None:
        cfg = make_cfg(tmp_path)
        content = "# P\n\nIntro.\n"
        decision = route(learning(project_count=5), cfg)
        proposal = build_proposal(learning(), decision, content, cfg)
        drifted = "# P\n\nIntro was edited by a human.\n"
        with pytest.raises(PatchConflict, match="mismatch"):
            apply_unified_diff(drifted, proposal["diff_unified"])

    def test_empty_diff_raises(self) -> None:
        with pytest.raises(PatchConflict, match="empty diff"):
            apply_unified_diff("content\n", "")

    def test_missing_headers_raises(self) -> None:
        with pytest.raises(PatchConflict, match="headers"):
            apply_unified_diff("a\n", "@@ -1,1 +1,1 @@\n-a\n+b\n")

    def test_headers_without_hunks_raises(self) -> None:
        with pytest.raises(PatchConflict, match="no hunks"):
            apply_unified_diff("a\n", "--- x\n+++ x\n")

    def test_tampered_hunk_counts_raise(self) -> None:
        old = "a\nb\nc\n"
        diff = make_unified_diff(old, "a\nB\nc\n", "f")
        tampered = "\n".join(
            ln for ln in diff.splitlines() if ln != " c"
        ) + "\n"  # drop a declared context line
        with pytest.raises(PatchConflict):
            apply_unified_diff(old, tampered)

    def test_delete_mismatch_raises(self) -> None:
        diff = make_unified_diff("a\nb\n", "a\n", "f")
        with pytest.raises(PatchConflict, match="mismatch"):
            apply_unified_diff("a\nX\n", diff)

    def test_new_file_diff_uses_dev_null_header(self) -> None:
        diff = make_unified_diff("", "line1\nline2\n", "f")
        assert diff.startswith("--- /dev/null\n+++ f\n")
        assert apply_unified_diff("", diff) == "line1\nline2\n"

    def test_identical_content_yields_empty_diff(self) -> None:
        assert make_unified_diff("same\n", "same\n", "f") == ""

    def test_multi_hunk_round_trip(self) -> None:
        old = "\n".join(f"line{i}" for i in range(30)) + "\n"
        new_lines = old.splitlines()
        new_lines[2] = "CHANGED-A"
        new_lines[25] = "CHANGED-B"
        new = "\n".join(new_lines) + "\n"
        diff = make_unified_diff(old, new, "f")
        assert diff.count("@@") >= 4  # two separate hunks (each has 2 '@@')
        assert apply_unified_diff(old, diff) == new

    def test_no_newline_marker_rejected(self) -> None:
        diff = (
            "--- f\n+++ f\n@@ -1,1 +1,1 @@\n-a\n+b\n"
            "\\ No newline at end of file\n"
        )
        with pytest.raises(PatchConflict, match="No newline"):
            apply_unified_diff("a\n", diff)


def test_diff_round_trips_for_a_seeded_corpus_of_adversarial_pairs():
    """make_unified_diff -> apply_unified_diff must reproduce the target exactly.

    This is the write path: the diff produced here is what eventually patches a
    real instruction file, and the applier is strict by design (no fuzzing, no
    guessing), so a construction bug shows up as either a PatchConflict on a
    diff we just generated or as silently wrong content.

    Seeded rather than random - a property test that fails only sometimes is
    worse than none. The alphabet deliberately includes lines that look like
    diff syntax (`@@ ...`, `--- ...`, `+x`, `-x`), blank lines and indented
    lines, because those are what a naive line-prefix parser mishandles.
    """
    import random

    alphabet = [
        "alpha", "beta", "gamma", "- **rule**: never X", "",
        "  indented", "@@ fake hunk", "--- notheader", "+plus", "-minus",
    ]
    rng = random.Random(7)
    checked = 0
    for _ in range(500):
        a = [rng.choice(alphabet) for _ in range(rng.randint(0, 8))]
        b = [rng.choice(alphabet) for _ in range(rng.randint(0, 8))]
        old = "\n".join(a) + ("\n" if a else "")
        new = "\n".join(b) + ("\n" if b else "")
        diff = make_unified_diff(old, new, "t.md")
        if not diff:
            assert old == new, "empty diff for differing content"
            continue
        assert apply_unified_diff(old, diff) == new, (
            f"round-trip lost content\nold={old!r}\nnew={new!r}\ndiff={diff!r}"
        )
        checked += 1
    # Guard the guard: if the generator stopped producing differing pairs this
    # test would pass while asserting nothing.
    assert checked > 400, f"only {checked} non-trivial pairs exercised"
