"""Search both mined learnings and instructions already in force.
Temporary instruction files, skills, and learnings exercise the combined
corpus, duplicate handling, and redaction without personal history.
"""

from __future__ import annotations

import dataclasses

import pytest

from self_improve.config import Config
from self_improve.search import search_learnings
from self_improve.store import Store, new_id, utc_now_iso

RULE_IN_FORCE = (
    "- **Read one full raw response before trusting any parser**, and assert "
    "that the response telemetry names what you requested."
)


@pytest.fixture
def env(tmp_path):
    global_md = tmp_path / "global" / "CLAUDE.md"
    global_md.parent.mkdir(parents=True)
    global_md.write_text(
        "# How I want you to work\n\n"
        f"{RULE_IN_FORCE}\n"
        "- **Never derive logical time from the filesystem or the local wall "
        "clock** — read the timestamp out of the data.\n"
    )
    cfg = dataclasses.replace(
        Config(),
        state_dir=str(tmp_path / "state"),
        global_claude_md=str(global_md),
        codex_global_agents_md=str(tmp_path / "codex" / "AGENTS.md"),
        skills_dir=str(tmp_path / "skills"),
    )
    store = Store(cfg.state_path("state.db"))
    store.insert(
        "learnings",
        {
            "id": new_id(), "rule_text": "**Something entirely unrelated about CSS**",
            "why": "w", "category": "style", "scope": "project",
            "evidence_count": 1, "project_count": 1, "projects_json": "[]",
            "first_seen": "", "last_seen": "", "confidence": 0.5,
            "status": "candidate", "duplicate_of": "", "created_at": utc_now_iso(),
        },
    )
    store.commit()
    return cfg, store


def test_in_force_rules_are_searchable(env):
    cfg, store = env
    results = search_learnings(cfg, "read the raw response before parsing it", top_k=8)

    rule_hits = [r for r in results if r.get("kind") == "rule_unit"]
    assert rule_hits, (
        "no instruction-file rule in the results: the dedup tool still cannot "
        "see the files it must avoid duplicating"
    )
    assert any("raw response" in r["rule_text"] for r in rule_hits)


def test_a_rule_hit_says_which_file_it_is_in_force_in(env):
    """'Already written' is only actionable if the agent is told WHERE."""
    cfg, store = env
    results = search_learnings(cfg, "read the raw response before parsing it", top_k=8)
    hit = next(r for r in results if r.get("kind") == "rule_unit")

    assert hit["source_file"].endswith("CLAUDE.md")
    assert hit["status"] == "in_force"


def test_learnings_are_still_returned_and_labelled(env):
    """Adding a corpus must not displace the one that was already there."""
    cfg, store = env
    results = search_learnings(cfg, "CSS styling rules", top_k=8)
    kinds = {r.get("kind") for r in results}
    assert "learning" in kinds


def test_ranking_puts_the_true_duplicate_above_the_unrelated_learning(env):
    """The whole point of including the corpus is that it can WIN."""
    cfg, store = env
    results = search_learnings(cfg, "read one full raw response before parsing", top_k=8)
    assert results[0]["kind"] == "rule_unit"
    assert "raw response" in results[0]["rule_text"]


def test_the_search_connection_is_still_read_only(env):
    """This runs inside an autonomous agent's Bash allowlist.

    Widening the corpus must not widen write access — the agent must never be
    able to mutate state through its own dedup tool.
    """
    cfg, store = env
    from self_improve.search import open_store_readonly

    conn = open_store_readonly(cfg)
    with pytest.raises(Exception):
        conn.execute("CREATE TABLE t (x INT)")
    conn.close()


def test_missing_instruction_files_are_skipped_not_fatal(env, tmp_path):
    """A project whose AGENTS.md was deleted must not break the miner's dedup."""
    cfg, store = env
    cfg = dataclasses.replace(cfg, global_claude_md=str(tmp_path / "nope" / "CLAUDE.md"))
    results = search_learnings(cfg, "anything at all", top_k=8)
    assert isinstance(results, list)


def test_top_k_is_shared_across_both_corpora(env):
    """The agent sees exactly top_k rows in total, whatever their kind."""
    cfg, store = env
    results = search_learnings(cfg, "parser", top_k=2)
    assert len(results) == 2


# ----------------------------------------------------------------------
# clone copies must not eat the result slots
# ----------------------------------------------------------------------


def test_one_rule_copied_across_clones_occupies_one_slot(tmp_path):
    """Repeated instruction text across clones occupies one search-result slot."""
    rule = "- **Never json.load a wrapped CLI's stdout** — read the raw bytes first.\n"
    other = "- **Always run the linter before pushing** — CI is slower than you.\n"
    projects = []
    for i in range(12):
        proj = tmp_path / "clones" / f"repo-{i}"
        proj.mkdir(parents=True)
        (proj / "AGENTS.md").write_text(rule + other)
        (proj / "CLAUDE.md").write_text(rule + other)
        projects.append(proj)

    cfg = dataclasses.replace(
        Config(),
        state_dir=str(tmp_path / "state"),
        global_claude_md=str(tmp_path / "g" / "CLAUDE.md"),
        codex_global_agents_md=str(tmp_path / "c" / "AGENTS.md"),
        skills_dir=str(tmp_path / "skills"),
    )
    store = Store(cfg.state_path("state.db"))
    for i, proj in enumerate(projects):
        store.upsert_session(
            {
                "file_path": f"/v/{i}.jsonl", "source": "claude", "session_id": f"s{i}",
                "project_path": str(proj), "headless": 0, "is_subagent": 0,
                "first_ts": "", "last_ts": "", "mtime": 0.0, "file_size": 0,
                "bytes_scanned": 0, "lines_scanned": 0, "malformed_lines": 0,
                "status": "ok", "error": "", "last_scanned_at": utc_now_iso(),
            }
        )
    store.commit()

    results = search_learnings(cfg, "json.load raw CLI stdout", top_k=8)
    rules = [r for r in results if r["kind"] == "rule_unit"]
    texts = [r["rule_text"] for r in rules]

    assert len(texts) == len(set(texts)), (
        f"the same rule occupies {len(texts) - len(set(texts)) + 1} slots: {texts}"
    )
    # ...and the OTHER rule must therefore still be reachable.
    assert any("linter" in t for t in texts), (
        "clone copies crowded out every other rule in the corpus"
    )


def test_a_deduped_rule_reports_how_many_copies_it_had(tmp_path):
    """'This is in force in 12 places' is information the miner should have."""
    rule = "- **Some repeated rule** — with a body long enough to survive the filter.\n"
    projects = []
    for i in range(3):
        proj = tmp_path / "c" / f"r{i}"
        proj.mkdir(parents=True)
        (proj / "AGENTS.md").write_text(rule)
        projects.append(proj)

    cfg = dataclasses.replace(
        Config(),
        state_dir=str(tmp_path / "state"),
        global_claude_md=str(tmp_path / "g" / "CLAUDE.md"),
        codex_global_agents_md=str(tmp_path / "cx" / "AGENTS.md"),
        skills_dir=str(tmp_path / "skills"),
    )
    store = Store(cfg.state_path("state.db"))
    for i, proj in enumerate(projects):
        store.upsert_session(
            {
                "file_path": f"/v/{i}.jsonl", "source": "claude", "session_id": f"s{i}",
                "project_path": str(proj), "headless": 0, "is_subagent": 0,
                "first_ts": "", "last_ts": "", "mtime": 0.0, "file_size": 0,
                "bytes_scanned": 0, "lines_scanned": 0, "malformed_lines": 0,
                "status": "ok", "error": "", "last_scanned_at": utc_now_iso(),
            }
        )
    store.commit()

    hit = next(
        r for r in search_learnings(cfg, "some repeated rule", top_k=8)
        if r["kind"] == "rule_unit"
    )
    assert hit["in_force_copies"] == 3
    assert hit["source_file"].endswith("AGENTS.md")


def test_rule_units_are_redacted_before_leaving_the_process(tmp_path):
    """Redact instruction-file content before it enters a search result or LLM context."""
    md = tmp_path / "g" / "CLAUDE.md"
    md.parent.mkdir(parents=True)
    md.write_text(
        "- **Deploy with the service key** — export "
        "AWS_SECRET_ACCESS_KEY=AKIAIOSFODNN7EXAMPLE and run the script, "
        "then mail results to owner@example.com.\n"
    )
    cfg = dataclasses.replace(
        Config(),
        state_dir=str(tmp_path / "state"),
        global_claude_md=str(md),
        codex_global_agents_md=str(tmp_path / "cx" / "AGENTS.md"),
        skills_dir=str(tmp_path / "skills"),
    )
    Store(cfg.state_path("state.db")).commit()

    hit = next(
        r for r in search_learnings(cfg, "deploy with the service key", top_k=8)
        if r["kind"] == "rule_unit"
    )
    assert "AKIAIOSFODNN7EXAMPLE" not in hit["rule_text"]
    assert "owner@example.com" not in hit["rule_text"]
    # ...and the rule is still recognisable, or redaction has destroyed its use.
    assert "Deploy with the service key" in hit["rule_text"]


def test_search_reports_what_its_top_k_cut(env):
    """8 rows out of hundreds ranked, with no signal that anything was cut.

    Crowd-out — near-duplicates filling every slot so the rule that would have
    stopped them never appears — is the known dedup failure. Without this the
    caller cannot distinguish "nothing else matched" from "the slots were
    full", and those call for opposite responses.
    """
    cfg, store = env
    out = search_learnings(cfg, "parser", top_k=1, with_meta=True)

    assert out["meta"]["returned"] == 1
    assert out["meta"]["ranked"] > 1
    assert out["meta"]["cut"] == out["meta"]["ranked"] - 1
    # The boundary is visible, so a caller can see it just missed the cut.
    assert out["meta"]["highest_cut_cosine"] is not None
    assert out["meta"]["lowest_returned_cosine"] >= out["meta"]["highest_cut_cosine"]
    assert set(out["meta"]["corpus"]) == {"learnings", "in_force_rules"}


def test_without_meta_the_return_shape_is_unchanged(env):
    """The sandboxed miner's existing contract must not shift underneath it."""
    cfg, store = env
    plain = search_learnings(cfg, "parser", top_k=2)
    assert isinstance(plain, list) and len(plain) == 2


def test_an_unreadable_instruction_file_never_touches_stdout(env, tmp_path, capsys):
    """Keep warnings off stdout so the sandboxed agent can parse the complete command output as JSON."""
    bad = tmp_path / "global" / "CLAUDE.md"
    bad.write_bytes(b"\xff\xfe not valid utf-8 \xff")
    cfg, store = env

    search_learnings(cfg, "anything", top_k=4)

    captured = capsys.readouterr()
    assert captured.out == "", f"wrote to stdout: {captured.out[:120]!r}"
