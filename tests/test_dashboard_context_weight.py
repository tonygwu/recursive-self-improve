"""Tests for the PRD 8a context-weight measurement.

Every test drives the public entry point ``context_weight()``. Two helpers
(``find_imports``, the module's import list) are also asserted directly, but
never *instead of* the entry point: a helper that works and is never called is
the failure mode AGENTS.md records three times this month.

Temporary directories reproduce import chains, symlinked instruction and skill
trees, and projects with no instruction files. No personal checkouts are read.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

from self_improve.dashboard.context_weight import (
    CHARS_PER_TOKEN,
    context_weight,
    find_imports,
)

# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------


def _repo(tmp_path: Path, name: str = "repo") -> Path:
    """A directory that looks like a git checkout."""
    repo = tmp_path / name
    (repo / ".git").mkdir(parents=True)
    return repo


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _paths(result: dict) -> set[str]:
    return {f["path"] for f in result["files"]}


def _names(result: dict) -> set[str]:
    return {os.path.basename(f["path"]) for f in result["files"]}


def _tree(root: Path) -> dict[str, tuple[int, int]]:
    """(size, mtime_ns) for every file under ``root`` — a write detector."""
    out: dict[str, tuple[int, int]] = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            full = os.path.join(dirpath, name)
            st = os.lstat(full)
            out[full] = (st.st_size, st.st_mtime_ns)
    return out


# ----------------------------------------------------------------------------
# The PRD 8a separation: always-loaded vs the rest of the repo's markdown
# ----------------------------------------------------------------------------


def test_always_loaded_is_counted_apart_from_the_repos_other_markdown(tmp_path):
    """PRD 8a's central rule: the two numbers are never added together.

    A large invented transcript must not inflate the always-loaded instructions.
    """
    repo = _repo(tmp_path)
    _write(repo / "CLAUDE.md", "c" * 100)
    _write(repo / "AGENTS.md", "a" * 200)
    _write(repo / "docs" / "transcript.md", "t" * 50_000)
    _write(repo / "README.md", "r" * 3_000)

    result = context_weight(str(repo))

    assert _names(result) == {"CLAUDE.md", "AGENTS.md"}
    assert result["always_loaded_bytes"] == 300
    assert result["total_bytes"] == 300
    assert result["total_bytes"] == sum(f["bytes"] for f in result["files"])
    # The other markdown is reported, and is NOT part of total_bytes: the two
    # sets are disjoint and the totals add up only when added deliberately.
    assert result["other_md"]["file_count"] == 2
    assert result["other_md"]["bytes"] == 53_000
    assert result["total_bytes"] + result["other_md"]["bytes"] == 53_300
    assert result["total_bytes"] < result["other_md"]["bytes"]
    other_paths = {x["path"] for x in result["other_md"]["largest"]}
    assert other_paths == {str(repo / "docs" / "transcript.md"), str(repo / "README.md")}
    assert not (other_paths & _paths(result))
    assert result["has_instruction_files"] is True
    assert result["status"] == "ok"


def test_skills_are_in_force_but_never_always_loaded(tmp_path):
    """A SKILL.md loads on demand; only its name and description are resident."""
    repo = _repo(tmp_path)
    _write(repo / "CLAUDE.md", "c" * 100)
    _write(repo / ".claude" / "skills" / "deploy" / "SKILL.md", "s" * 900)

    result = context_weight(str(repo))

    assert _names(result) == {"CLAUDE.md", "SKILL.md"}
    assert result["always_loaded_bytes"] == 100
    assert result["on_demand_bytes"] == 900
    assert result["total_bytes"] == 1_000
    skill = next(f for f in result["files"] if f["kind"] == "skill")
    assert skill["always_loaded"] is False


def test_claude_rules_are_included_and_a_path_scoped_rule_is_not_always_loaded(
    tmp_path,
):
    """``.claude/rules/*.md`` counts; a ``paths:`` frontmatter scopes it.

    ``propose.render_rule_file()`` writes exactly this frontmatter, so a rule
    file this pipeline creates is path-scoped by construction.
    """
    repo = _repo(tmp_path)
    _write(repo / "CLAUDE.md", "c" * 10)
    _write(repo / ".claude" / "rules" / "always.md", "u" * 500)
    _write(
        repo / ".claude" / "rules" / "scoped.md",
        '---\npaths:\n  - "src/**/*.py"\n---\n\n' + "s" * 400,
    )

    result = context_weight(str(repo))

    by_name = {os.path.basename(f["path"]): f for f in result["files"]}
    assert set(by_name) == {"CLAUDE.md", "always.md", "scoped.md"}
    assert by_name["always.md"]["always_loaded"] is True
    assert by_name["scoped.md"]["always_loaded"] is False
    assert by_name["scoped.md"]["bytes"] in [
        f["bytes"] for f in result["files"]
    ]  # still in force
    assert result["total_bytes"] == sum(f["bytes"] for f in result["files"])
    assert result["always_loaded_bytes"] == 10 + 500


# ----------------------------------------------------------------------------
# @-imports
# ----------------------------------------------------------------------------


def test_imports_are_followed_transitively_through_the_entry_point(tmp_path):
    """Three hops, all always-loaded, each counted once.

    This is the wiring test: it fails if ``context_weight`` stops queueing what
    the import scanner found, even though ``find_imports`` still works.
    """
    repo = _repo(tmp_path)
    _write(repo / "CLAUDE.md", "@AGENTS.md\n" + "c" * 89)
    _write(repo / "AGENTS.md", "@docs/style.md\n" + "a" * 186)
    _write(repo / "docs" / "style.md", "@../deep/rules.md\n" + "s" * 283)
    _write(repo / "deep" / "rules.md", "r" * 400)

    result = context_weight(str(repo))

    assert _names(result) == {"CLAUDE.md", "AGENTS.md", "style.md", "rules.md"}
    assert result["total_bytes"] == 100 + 201 + 301 + 400
    assert result["always_loaded_bytes"] == result["total_bytes"]
    assert all(f["always_loaded"] for f in result["files"])
    assert result["missing_imports"] == []
    depths = {os.path.basename(f["path"]): f["depth"] for f in result["files"]}
    assert depths == {"CLAUDE.md": 0, "AGENTS.md": 0, "style.md": 1, "rules.md": 2}
    # An imported file is in force, so it must not also be billed as "other".
    assert "style.md" not in {
        os.path.basename(x["path"]) for x in result["other_md"]["largest"]
    }


def test_a_relative_import_resolves_against_the_importing_file(tmp_path):
    """``@b.md`` inside ``docs/a.md`` means ``docs/b.md``, not ``<repo>/b.md``."""
    repo = _repo(tmp_path)
    _write(repo / "CLAUDE.md", "@docs/a.md\n")
    _write(repo / "docs" / "a.md", "@b.md\n")
    _write(repo / "docs" / "b.md", "b" * 77)
    _write(repo / "b.md", "WRONG" * 100)

    result = context_weight(str(repo))

    counted = _paths(result)
    assert str(repo / "docs" / "b.md") in counted
    assert str(repo / "b.md") not in counted
    assert result["missing_imports"] == []


def test_an_import_cycle_terminates_and_is_reported(tmp_path):
    """A → B → C → A must finish, count each file once, and name the cycle."""
    repo = _repo(tmp_path)
    _write(repo / "CLAUDE.md", "@a.md\n" + "c" * 10)
    _write(repo / "a.md", "@b.md\n" + "a" * 10)
    _write(repo / "b.md", "@CLAUDE.md\n" + "b" * 10)

    result = context_weight(str(repo))

    assert _names(result) == {"CLAUDE.md", "a.md", "b.md"}
    assert result["file_count"] == 3
    assert result["total_bytes"] == sum(f["bytes"] for f in result["files"])
    assert len(result["import_cycles"]) == 1
    cycle = result["import_cycles"][0]
    assert cycle["path"] == str(repo / "CLAUDE.md")
    assert cycle["same_file_as"] == str(repo / "CLAUDE.md")
    assert "@CLAUDE.md" in cycle["origin"]


def test_a_missing_import_is_reported_not_silently_skipped(tmp_path):
    """Fail loud: the operator must see that CLAUDE.md points at nothing."""
    repo = _repo(tmp_path)
    _write(repo / "CLAUDE.md", "@AGENTS.md\n" + "c" * 89)

    result = context_weight(str(repo))

    assert result["total_bytes"] == 100
    assert len(result["missing_imports"]) == 1
    missing = result["missing_imports"][0]
    assert missing["spec"] == "AGENTS.md"
    assert missing["resolved"] == str(repo / "AGENTS.md")
    assert missing["imported_by"] == str(repo / "CLAUDE.md")
    assert "does not exist" in missing["reason"]


def test_an_import_in_a_code_fence_or_code_span_is_not_an_import(tmp_path):
    """A document quoting ``@AGENTS.md`` is not importing it."""
    repo = _repo(tmp_path)
    _write(
        repo / "CLAUDE.md",
        "Write `@AGENTS.md` to import it.\n"
        "Run `selfimprove show @docs/span.md and quit` to print one.\n"
        "\n"
        "```markdown\n"
        "@docs/fenced.md\n"
        "```\n"
        "\n"
        "~~~\n"
        "@docs/other.md\n"
        "~~~\n",
    )
    _write(repo / "AGENTS.md", "a" * 4_000)

    result = context_weight(str(repo))

    # AGENTS.md is still counted — it is a repo-root seed — but nothing was
    # imported, so none of the three quoted paths may be reported missing.
    assert result["missing_imports"] == []
    assert _names(result) == {"CLAUDE.md", "AGENTS.md"}
    claude = next(f for f in result["files"] if f["path"].endswith("CLAUDE.md"))
    assert claude["kind"] == "claude_md"


def test_an_at_token_that_is_not_a_path_is_not_treated_as_an_import(tmp_path):
    """`@anthropic-ai/claude-code`, an email and a decorator are not imports."""
    repo = _repo(tmp_path)
    _write(
        repo / "CLAUDE.md",
        "Install @anthropic-ai/claude-code and mail noreply@anthropic.com.\n"
        "Ping @claude in the thread.\n"
        "@decorator\n",
    )

    result = context_weight(str(repo))

    assert result["missing_imports"] == []
    assert result["file_count"] == 1


def test_find_imports_qualifies_tokens_the_way_the_walker_relies_on():
    """The helper's contract, asserted directly as well as through the walker."""
    assert find_imports("@AGENTS.md\n") == ["AGENTS.md"]
    assert find_imports("see @./docs/x.md.") == ["./docs/x.md"]
    assert find_imports("@~/.claude/CLAUDE.md") == ["~/.claude/CLAUDE.md"]
    assert find_imports("@anthropic-ai/claude-code") == []
    assert find_imports("a@b.md") == []  # an address, not an import
    assert find_imports("`@AGENTS.md`") == []
    # A span whose @ follows a space is the case the blanking exists for.
    assert find_imports("run `si show @docs/x.md and quit` now") == []
    assert find_imports("```\n@docs/x.md\n```\n") == []


# ----------------------------------------------------------------------------
# Symlinks and topology
# ----------------------------------------------------------------------------


def test_a_claude_md_symlink_is_counted_once_and_named_in_the_topology(tmp_path):
    """Count the instruction file once when CLAUDE.md is its symlink.

    The fixture contains AGENTS.md and CLAUDE.md pointing at that same file.
    """
    repo = _repo(tmp_path)
    _write(repo / "AGENTS.md", "a" * 1_000)
    (repo / "CLAUDE.md").symlink_to(repo / "AGENTS.md")

    result = context_weight(str(repo))

    assert result["file_count"] == 1
    assert result["total_bytes"] == 1_000
    assert result["always_loaded_bytes"] == 1_000
    assert _names(result) == {"AGENTS.md"}
    assert len(result["deduplicated"]) == 1
    dup = result["deduplicated"][0]
    assert dup["path"] == str(repo / "CLAUDE.md")
    assert dup["same_file_as"] == str(repo / "AGENTS.md")
    assert result["topology"]["label"] == "CLAUDE.md symlink"
    assert result["topology"]["claude_md_symlink_to_agents"] is True


def test_a_symlink_target_reached_twice_is_counted_once(tmp_path):
    """Dedup is on the real file, not on the path that reached it.

    AGENTS.md is a link to docs/INSTRUCTIONS.md and CLAUDE.md imports that same
    file by its real path. Two routes, one file, one set of bytes.
    """
    repo = _repo(tmp_path)
    real = _write(repo / "docs" / "INSTRUCTIONS.md", "i" * 3_000)
    (repo / "AGENTS.md").symlink_to(real)
    _write(repo / "CLAUDE.md", "@docs/INSTRUCTIONS.md\n")

    result = context_weight(str(repo))

    assert result["file_count"] == 2
    assert result["total_bytes"] == 3_022
    assert {f["real_path"] for f in result["files"]} == {
        str(real),
        str(repo / "CLAUDE.md"),
    }
    assert len(result["deduplicated"]) == 1
    assert result["deduplicated"][0]["same_file_as"] == str(repo / "AGENTS.md")


def test_a_skill_reached_through_a_symlinked_directory_is_not_billed_twice(tmp_path):
    """Shared skills: ``.claude -> .agents``, one file, two paths.

    The in-force set reaches the SKILL.md through ``.claude``; the
    other-markdown walk meets the same inode under ``.agents``. If the walker
    remembers the path it used instead of the real file, the same bytes land in
    both numbers.
    """
    repo = _repo(tmp_path)
    _write(repo / "CLAUDE.md", "c" * 10)
    real_skill = _write(repo / ".agents" / "skills" / "deploy" / "SKILL.md", "s" * 700)
    (repo / ".claude" / "skills").mkdir(parents=True)
    (repo / ".claude" / "skills" / "deploy").symlink_to(
        repo / ".agents" / "skills" / "deploy"
    )

    result = context_weight(str(repo))

    skills = [f for f in result["files"] if f["kind"] == "skill"]
    assert len(skills) == 1
    assert skills[0]["real_path"] == str(real_skill)
    assert result["total_bytes"] == 710
    assert result["other_md"]["file_count"] == 0
    assert result["other_md"]["bytes"] == 0


def test_a_stub_claude_md_reports_the_stub_topology_and_counts_both(tmp_path):
    """A CLAUDE.md that is only ``@AGENTS.md`` is a pointer, and both load."""
    repo = _repo(tmp_path)
    _write(repo / "CLAUDE.md", "@AGENTS.md\n")
    _write(repo / "AGENTS.md", "a" * 2_000)

    result = context_weight(str(repo))

    assert result["topology"]["label"] == "CLAUDE.md stub"
    assert result["file_count"] == 2
    assert result["total_bytes"] == 2_011
    assert result["always_loaded_bytes"] == 2_011


# ----------------------------------------------------------------------------
# Paths that are not repos, and the empty state
# ----------------------------------------------------------------------------


def test_a_missing_path_returns_a_result_that_says_so(tmp_path):
    result = context_weight(str(tmp_path / "no-such-repo"))

    assert result["status"] == "missing"
    assert "does not exist" in result["reason"]
    assert result["exists"] is False
    assert result["total_bytes"] == 0
    assert result["files"] == []
    # Full shape: a caller never has to guess whether a key is there.
    for key in (
        "always_loaded_bytes",
        "other_md",
        "topology",
        "caps_applied",
        "missing_imports",
        "has_instruction_files",
    ):
        assert key in result


def test_a_file_path_is_reported_as_not_a_directory(tmp_path):
    target = _write(tmp_path / "CLAUDE.md", "x")
    result = context_weight(str(target))
    assert result["status"] == "not_a_directory"
    assert result["exists"] is True
    assert result["total_bytes"] == 0


def test_a_directory_outside_git_is_measured_and_flagged(tmp_path):
    """Not a checkout is a flag, not a refusal: the files still load."""
    plain = tmp_path / "plain"
    _write(plain / "CLAUDE.md", "c" * 42)

    result = context_weight(str(plain))

    assert result["status"] == "not_a_git_checkout"
    assert result["is_git_repo"] is False
    assert result["git_root"] == ""
    assert result["total_bytes"] == 42
    assert any("no .git" in note for note in result["notes"])


def test_a_repo_with_no_instruction_files_is_an_empty_state_not_a_zero(tmp_path):
    """A directory without instruction files is a distinct empty state."""
    repo = _repo(tmp_path)
    _write(repo / "README.md", "r" * 900)

    result = context_weight(str(repo))

    assert result["has_instruction_files"] is False
    assert result["file_count"] == 0
    assert result["total_bytes"] == 0
    assert any("no instruction files" in note for note in result["notes"])
    assert result["other_md"]["bytes"] == 900  # the repo is not empty


# ----------------------------------------------------------------------------
# Caps: every one reports what it cut
# ----------------------------------------------------------------------------


def test_the_import_depth_cap_names_the_import_it_did_not_follow(tmp_path):
    repo = _repo(tmp_path)
    _write(repo / "CLAUDE.md", "@one.md\n")
    _write(repo / "one.md", "@two.md\n")
    _write(repo / "two.md", "@three.md\n")
    _write(repo / "three.md", "never" * 100)

    result = context_weight(str(repo), max_import_depth=2)

    assert _names(result) == {"CLAUDE.md", "one.md", "two.md"}
    caps = [c for c in result["caps_applied"] if c["cap"] == "max_import_depth"]
    assert len(caps) == 1
    assert caps[0]["limit"] == 2
    assert caps[0]["path"] == str(repo / "two.md")
    assert "three.md" in caps[0]["cut"]


def test_the_import_scan_byte_cap_names_the_bytes_it_did_not_read(tmp_path):
    repo = _repo(tmp_path)
    _write(repo / "CLAUDE.md", "x" * 5_000 + "\n@AGENTS.md\n")
    _write(repo / "AGENTS.md", "a" * 10)

    result = context_weight(str(repo), max_import_scan_bytes=1_000)

    caps = [c for c in result["caps_applied"] if c["cap"] == "max_import_scan_bytes"]
    assert len(caps) == 1
    assert caps[0]["limit"] == 1_000
    assert "4012 bytes" in caps[0]["cut"]
    # The whole file is still counted; only the scan was capped.
    claude = next(f for f in result["files"] if f["path"].endswith("CLAUDE.md"))
    assert claude["bytes"] == 5_012
    assert claude["chars"] is None


def test_a_cap_that_splits_a_multibyte_character_is_not_called_bad_encoding(
    tmp_path,
):
    """The cap cut the character, so the cap must own the fault, not the file.

    Reporting this file under ``unreadable`` as "not UTF-8" would send the
    operator to fix a file that is fine.
    """
    repo = _repo(tmp_path)
    _write(repo / "CLAUDE.md", "x" * 999 + "é" + "y" * 100)

    result = context_weight(str(repo), max_import_scan_bytes=1_000)

    assert result["unreadable"] == []
    caps = [c for c in result["caps_applied"] if c["cap"] == "max_import_scan_bytes"]
    assert len(caps) == 1
    claude = result["files"][0]
    assert claude["bytes"] == 1_101  # 999 + 2 (é) + 100
    assert claude["chars"] is None


def test_a_file_that_is_not_utf8_is_reported_and_still_counted(tmp_path):
    repo = _repo(tmp_path)
    _write(repo / "CLAUDE.md", "c" * 10)
    (repo / ".claude" / "rules").mkdir(parents=True)
    (repo / ".claude" / "rules" / "binary.md").write_bytes(b"\xff\xfe" + b"z" * 98)

    result = context_weight(str(repo))

    assert len(result["unreadable"]) == 1
    bad = result["unreadable"][0]
    assert bad["path"] == str(repo / ".claude" / "rules" / "binary.md")
    assert "not UTF-8" in bad["reason"]
    assert result["total_bytes"] == 110  # still counted; the bytes are real


def test_a_broken_symlink_in_the_other_markdown_walk_is_reported(tmp_path):
    repo = _repo(tmp_path)
    _write(repo / "CLAUDE.md", "c")
    (repo / "docs").mkdir()
    (repo / "docs" / "gone.md").symlink_to(repo / "docs" / "never-existed.md")

    result = context_weight(str(repo))

    assert result["other_md"]["stat_error_count"] == 1
    assert result["other_md"]["stat_errors"][0]["path"] == str(repo / "docs" / "gone.md")
    assert result["other_md"]["bytes"] == 0


def test_the_other_markdown_cap_says_the_total_is_a_floor(tmp_path):
    repo = _repo(tmp_path)
    _write(repo / "CLAUDE.md", "c")
    for i in range(20):
        _write(repo / "docs" / f"n{i:02d}.md", "x" * 10)

    result = context_weight(str(repo), max_other_md_files=5)

    caps = [c for c in result["caps_applied"] if c["cap"] == "max_other_md_files"]
    assert len(caps) == 1
    assert caps[0]["limit"] == 5
    assert "floor" in caps[0]["cut"]
    assert "TRUNCATED" in result["other_md"]["note"]
    assert result["other_md"]["file_count"] < 20


def test_scan_other_md_false_skips_the_walk_and_says_so(tmp_path):
    repo = _repo(tmp_path)
    _write(repo / "CLAUDE.md", "c" * 5)
    _write(repo / "docs" / "big.md", "x" * 900)

    result = context_weight(str(repo), scan_other_md=False)

    assert result["other_md"]["scanned"] is False
    assert result["other_md"]["file_count"] == 0
    assert "not scanned" in result["other_md"]["note"]
    assert result["total_bytes"] == 5


def test_approx_tokens_divides_characters_by_the_documented_constant(tmp_path):
    repo = _repo(tmp_path)
    _write(repo / "CLAUDE.md", "c" * 400)
    _write(repo / ".claude" / "skills" / "s" / "SKILL.md", "s" * 800)

    result = context_weight(str(repo))

    assert CHARS_PER_TOKEN == 4
    assert result["approx_tokens"] == 1_200 // 4
    assert result["always_loaded_approx_tokens"] == 400 // 4


# ----------------------------------------------------------------------------
# Read-only, and no forbidden dependency
# ----------------------------------------------------------------------------


def test_measuring_a_repo_writes_nothing(tmp_path):
    repo = _repo(tmp_path)
    _write(repo / "CLAUDE.md", "@AGENTS.md\n")
    _write(repo / "AGENTS.md", "a" * 50)
    _write(repo / ".claude" / "rules" / "r.md", "r" * 20)
    _write(repo / "docs" / "x.md", "x" * 20)

    before = _tree(repo)
    context_weight(str(repo))
    after = _tree(repo)

    assert before == after


def test_the_module_imports_no_database_web_or_network_dependency():
    """Pure filesystem reads. Asserted on the source, not on a happy path.

    The source is derived from the IMPORTED MODULE, never from a literal path.
    A hardcoded checkout path would inspect a different copy of the module and
    could miss a forbidden import in the code under test.

    That is the "checked the wrong artifact" class docs/RUNBOOK.md records, and
    this is the only test that would notice context_weight.py acquiring a
    database, web or network dependency.
    """
    from self_improve.dashboard import context_weight as _module

    source = Path(_module.__file__)
    tree = ast.parse(source.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")

    assert imported == {"__future__", "os", "re", "pathlib", "typing", "self_improve"}
    forbidden = ("sqlite3", "fastapi", "uvicorn", "httpx", "requests", "socket", "store")
    for name in imported:
        assert not any(bad in name for bad in forbidden), name


# ----------------------------------------------------------------------------
# Generated repository topologies
# ----------------------------------------------------------------------------


def test_imported_agents_file_is_counted_without_phantom_imports(tmp_path):
    repo = _repo(tmp_path)
    _write(repo / "CLAUDE.md", "@AGENTS.md\n")
    _write(repo / "AGENTS.md", "Follow the tests.\nUse `@example` in a code span.\n")

    result = context_weight(str(repo), scan_other_md=False)

    assert result["status"] == "ok"
    assert result["is_git_repo"] is True
    assert _names(result) == {"CLAUDE.md", "AGENTS.md"}
    assert all(f["always_loaded"] for f in result["files"])
    assert result["always_loaded_bytes"] == result["total_bytes"]
    agents = next(f for f in result["files"] if f["path"].endswith("AGENTS.md"))
    assert agents["bytes"] == os.stat(repo / "AGENTS.md").st_size
    assert result["missing_imports"] == []
    assert result["import_cycles"] == []


def _symlinked_skills_repo(tmp_path):
    repo = _repo(tmp_path)
    _write(repo / "AGENTS.md", "Run the tests.\n")
    (repo / "CLAUDE.md").symlink_to("AGENTS.md")
    (repo / ".claude" / "skills").mkdir(parents=True)
    for name in ("audit", "review"):
        _write(repo / ".agents" / "skills" / name / "SKILL.md", f"# {name}\n")
        (repo / ".claude" / "skills" / name).symlink_to(
            f"../../.agents/skills/{name}", target_is_directory=True
        )
    return repo


def test_symlinked_instructions_are_counted_once_with_on_demand_skills(tmp_path):
    repo = _symlinked_skills_repo(tmp_path)

    result = context_weight(str(repo), scan_other_md=False)

    assert result["topology"]["label"] == "CLAUDE.md symlink"
    always = [f for f in result["files"] if f["always_loaded"]]
    assert len(always) == 1
    assert always[0]["path"] == str(repo / "AGENTS.md")
    assert result["always_loaded_bytes"] == os.stat(repo / "AGENTS.md").st_size
    assert [d["path"] for d in result["deduplicated"]] == [str(repo / "CLAUDE.md")]
    skills = [f for f in result["files"] if f["kind"] == "skill"]
    assert len(skills) == 2
    assert not any(f["always_loaded"] for f in skills)


def test_symlinked_skills_are_not_billed_again_as_other_markdown(tmp_path):
    repo = _symlinked_skills_repo(tmp_path)
    _write(repo / "docs" / "guide.md", "A separate guide.\n")

    result = context_weight(str(repo))

    in_force = {f["real_path"] for f in result["files"]}
    other = {os.path.realpath(x["path"]) for x in result["other_md"]["largest"]}
    assert other == {str(repo / "docs" / "guide.md")}
    assert not (in_force & other)
    skill_paths = list((repo / ".agents" / "skills").glob("*/SKILL.md"))
    assert len(skill_paths) == 2
    for skill_md in skill_paths:
        assert os.path.realpath(skill_md) in in_force


def test_skills_are_found_through_a_symlinked_claude_directory(tmp_path):
    repo = _repo(tmp_path)
    _write(repo / "AGENTS.md", "Test edits.\n")
    _write(repo / ".agents" / "skills" / "review" / "SKILL.md", "# Review\n")
    (repo / ".claude").symlink_to(".agents", target_is_directory=True)

    result = context_weight(str(repo), scan_other_md=False)

    skills = [f for f in result["files"] if f["kind"] == "skill"]
    assert len(skills) == 1
    assert skills[0]["always_loaded"] is False
    assert result["on_demand_bytes"] == skills[0]["bytes"]
    assert result["always_loaded_bytes"] < result["total_bytes"]


def test_directory_without_git_or_instruction_files_is_the_empty_state(tmp_path):
    repo = tmp_path / "empty-project"
    repo.mkdir()

    result = context_weight(str(repo))

    assert result["has_instruction_files"] is False
    assert result["total_bytes"] == 0
    assert result["status"] == "not_a_git_checkout"
    assert result["topology"]["label"] == "no instruction files yet"
    assert any("no instruction files" in note for note in result["notes"])
