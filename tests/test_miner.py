"""Tests for miner.py: strict template rendering, in-force gathering, mine_incident."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from self_improve.config import Config
from self_improve.miner import (
    IN_FORCE_TRUNCATE_CHARS,
    PLACEHOLDER_RE,
    MineContractViolation,
    MineParseFailure,
    MinerError,
    PromptRenderError,
    gather_in_force_instructions,
    mine_incident,
    render_prompt,
    validate_mine_json,
)
from self_improve.store import Store

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPTS_DIR = REPO_ROOT / "prompts"


# ---------------------------------------------------------------------------
# render_prompt
# ---------------------------------------------------------------------------


def _write_template(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "template.md"
    path.write_text(text, encoding="utf-8")
    return path


def test_render_prompt_substitutes_all_placeholders(tmp_path):
    path = _write_template(tmp_path, "A {{one}} B {{two}} C {{one}}")
    result = render_prompt(path, {"one": "1", "two": "2"})
    assert result == "A 1 B 2 C 1"


def test_render_prompt_missing_mapping_key_raises(tmp_path):
    path = _write_template(tmp_path, "A {{one}} B {{two}}")
    with pytest.raises(PromptRenderError, match="two"):
        render_prompt(path, {"one": "1"})


def test_render_prompt_unused_mapping_key_raises(tmp_path):
    path = _write_template(tmp_path, "A {{one}}")
    with pytest.raises(PromptRenderError, match="extra"):
        render_prompt(path, {"one": "1", "extra": "x"})


def test_render_prompt_non_str_value_raises(tmp_path):
    path = _write_template(tmp_path, "A {{one}}")
    with pytest.raises(PromptRenderError, match="one"):
        render_prompt(path, {"one": 42})


def test_render_prompt_value_containing_placeholder_syntax_is_literal(tmp_path):
    # A substituted value that itself looks like a placeholder must be inserted
    # literally, never re-expanded or flagged.
    path = _write_template(tmp_path, "code: {{snippet}}")
    result = render_prompt(path, {"snippet": "f'{{x}}' and {{not_a_key}}"})
    assert result == "code: f'{{x}}' and {{not_a_key}}"


def test_render_prompt_json_braces_in_template_are_not_placeholders(tmp_path):
    path = _write_template(tmp_path, '{"a": {"b": 1}} {{key}}')
    assert render_prompt(path, {"key": "v"}) == '{"a": {"b": 1}} v'


def test_repo_prompt_templates_have_exact_placeholder_sets():
    """Pin the placeholder contract of every shipped template.

    A typo'd placeholder in a template would raise at render time; this test
    catches it at test time instead.
    """
    expected = {
        "mine_incident.md": {"signal_type", "project", "window", "in_force_instructions"},
        "cluster_merge.md": {"rules"},
        "gen_regression_eval.md": {"rule", "why", "incident_summary", "evidence"},
        "grade_rubric.md": {"rubric", "final_state"},
    }
    for name, keys in expected.items():
        text = (PROMPTS_DIR / name).read_text(encoding="utf-8")
        assert set(PLACEHOLDER_RE.findall(text)) == keys, name


# ---------------------------------------------------------------------------
# gather_in_force_instructions
# ---------------------------------------------------------------------------


def _cfg(tmp_path: Path) -> Config:
    return Config(global_claude_md=str(tmp_path / "global_CLAUDE.md"))


def test_gather_includes_labeled_contents(tmp_path):
    (tmp_path / "global_CLAUDE.md").write_text("GLOBAL-RULE-LINE", encoding="utf-8")
    project = tmp_path / "proj"
    project.mkdir()
    (project / "AGENTS.md").write_text("AGENTS-CONTENT", encoding="utf-8")
    (project / "CLAUDE.md").write_text("PROJECT-CLAUDE-CONTENT", encoding="utf-8")
    result = gather_in_force_instructions(str(project), _cfg(tmp_path))
    assert "=== global CLAUDE.md" in result
    assert "GLOBAL-RULE-LINE" in result
    assert "=== project AGENTS.md" in result
    assert "AGENTS-CONTENT" in result
    assert "=== project CLAUDE.md" in result
    assert "PROJECT-CLAUDE-CONTENT" in result


def test_gather_notes_absent_files(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    result = gather_in_force_instructions(str(project), _cfg(tmp_path))
    assert result.count("ABSENT") == 3  # global + project AGENTS.md + project CLAUDE.md


def test_gather_truncates_with_exact_marker(tmp_path):
    (tmp_path / "global_CLAUDE.md").write_text(
        "x" * (IN_FORCE_TRUNCATE_CHARS + 500), encoding="utf-8"
    )
    project = tmp_path / "proj"
    project.mkdir()
    result = gather_in_force_instructions(str(project), _cfg(tmp_path))
    assert "[truncated 500 chars]" in result
    # The kept content is exactly the cap, not less.
    assert "x" * IN_FORCE_TRUNCATE_CHARS in result
    assert "x" * (IN_FORCE_TRUNCATE_CHARS + 1) not in result


def test_gather_without_project_path_says_so(tmp_path):
    (tmp_path / "global_CLAUDE.md").write_text("G", encoding="utf-8")
    result = gather_in_force_instructions("", _cfg(tmp_path))
    assert "no project path" in result
    assert "G" in result


# ---------------------------------------------------------------------------
# validate_mine_json
# ---------------------------------------------------------------------------


def _valid_payload(**over) -> dict:
    payload = {
        "is_real_learning": True,
        "incident_summary": "Agent trusted exit 0 from a substituted model.",
        "generalized_rule": "**Never trust exit 0 alone** — read one full raw response first.",
        "why": "Prevents silent model substitution from poisoning results.",
        "scope_guess": "global",
        "category": "verification",
        "duplicate_of_existing_rule": None,
        "confidence": 0.9,
    }
    payload.update(over)
    return payload


def test_validate_accepts_valid_payload():
    assert validate_mine_json(_valid_payload()) == []


def test_validate_accepts_duplicate_as_string():
    assert validate_mine_json(
        _valid_payload(duplicate_of_existing_rule="Existing line.")
    ) == []


@pytest.mark.parametrize(
    "bad",
    [
        {"is_real_learning": "yes"},          # not a bool
        {"is_real_learning": 1},              # int is not bool
        {"confidence": "high"},               # not a number
        {"confidence": 1.5},                  # out of range
        {"confidence": True},                 # bool is not a number
        {"scope_guess": "everywhere"},        # not in enum
        {"generalized_rule": 7},              # not a str
        {"duplicate_of_existing_rule": 3},    # not str-or-null
        {"generalized_rule": "   "},          # empty rule on a real learning
    ],
)
def test_validate_rejects_bad_fields(bad):
    assert validate_mine_json(_valid_payload(**bad))


def test_validate_rejects_missing_and_extra_keys():
    payload = _valid_payload()
    del payload["why"]
    payload["bonus"] = "x"
    errors = validate_mine_json(payload)
    assert any("missing" in e for e in errors)
    assert any("unexpected" in e for e in errors)


def test_validate_rejects_non_dict():
    assert validate_mine_json(["not", "a", "dict"])


# ---------------------------------------------------------------------------
# mine_incident
# ---------------------------------------------------------------------------

SESSION_FILE = "/fake/projects/slug/abc.jsonl"


def _seed_incident(store: Store, project_path: str = "/proj") -> str:
    store.upsert_session(
        {
            "file_path": SESSION_FILE,
            "source": "claude",
            "session_id": "sess-1",
            "project_path": project_path,
            "headless": 0,
            "is_subagent": 0,
            "first_ts": "2026-08-01T00:00:00Z",
            "last_ts": "2026-08-01T01:00:00Z",
            "mtime": 0.0,
            "file_size": 1000,
            "bytes_scanned": 1000,
            "lines_scanned": 50,
            "malformed_lines": 0,
            "status": "ok",
            "error": "",
            "last_scanned_at": "2026-08-14T02:00:00Z",
        }
    )
    incident_id = store.insert_incident(
        {
            "session_file": SESSION_FILE,
            "session_id": "sess-1",
            "project_path": project_path,
            "ts": "2026-08-01T00:30:00Z",
            "signal_type": "correction",
            "matched_text": "no, that's wrong",
            "window": [
                {"role": "assistant", "text": "I ran the parser"},
                {"role": "human", "text": "no, that's wrong — check the raw output"},
            ],
            "score": 0.8,
            "run_id": "run-1",
        }
    )
    store.commit()
    return incident_id


def _incident_row(store: Store, incident_id: str) -> dict:
    row = store.query_one("SELECT * FROM incidents WHERE id = ?", (incident_id,))
    assert row is not None
    return row


def _miner_cfg(tmp_path: Path) -> Config:
    (tmp_path / "global_CLAUDE.md").write_text(
        "# global\nEXISTING-GLOBAL-RULE", encoding="utf-8"
    )
    return Config(global_claude_md=str(tmp_path / "global_CLAUDE.md"))


def test_mine_incident_happy_path(tmp_path):
    store = Store(tmp_path / "state.db")
    incident_id = _seed_incident(store)
    cfg = _miner_cfg(tmp_path)
    payload = _valid_payload()
    captured: dict = {}

    def llm_call(prompt: str) -> dict:
        captured["prompt"] = prompt
        return dict(payload)

    result = mine_incident(store, llm_call, _incident_row(store, incident_id), cfg, PROMPTS_DIR)

    # Prompt was built from the real template with real substitutions.
    prompt = captured["prompt"]
    assert "correction" in prompt                       # {{signal_type}}
    assert "/proj" in prompt                            # {{project}}
    assert '"no, that\'s wrong — check the raw output"' in prompt  # pretty window
    assert "EXISTING-GLOBAL-RULE" in prompt             # in-force instructions
    assert "{{" not in prompt                           # nothing left unsubstituted

    # Learning row inserted and returned.
    assert result is not None
    assert result["rule_text"] == payload["generalized_rule"]
    assert result["incident_summary"] == payload["incident_summary"]
    learning = store.query_one("SELECT * FROM learnings WHERE id = ?", (result["id"],))
    assert learning is not None
    assert learning["rule_text"] == payload["generalized_rule"]
    assert learning["why"] == payload["why"]
    assert learning["scope"] == "global"
    assert learning["status"] == "candidate"
    assert learning["confidence"] == pytest.approx(0.9)
    assert json.loads(learning["projects_json"]) == ["/proj"]
    assert learning["first_seen"] == "2026-08-01T00:30:00Z"

    # Link row + incident marked mined.
    link = store.query_one(
        "SELECT * FROM incident_learnings WHERE incident_id = ? AND learning_id = ?",
        (incident_id, result["id"]),
    )
    assert link is not None
    assert _incident_row(store, incident_id)["status"] == "mined"


def test_mine_incident_parse_failure_raises_and_keeps_incident_new(tmp_path):
    # Typed exception (not a None return) so the pipeline counts this as a
    # failure rather than a success.
    store = Store(tmp_path / "state.db")
    incident_id = _seed_incident(store)
    with pytest.raises(MineParseFailure):
        mine_incident(
            store, lambda prompt: None, _incident_row(store, incident_id),
            _miner_cfg(tmp_path), PROMPTS_DIR,
        )
    assert _incident_row(store, incident_id)["status"] == "new"
    assert store.query("SELECT * FROM learnings") == []
    assert store.query("SELECT * FROM incident_learnings") == []


def test_mine_incident_contract_violation_raises_and_keeps_incident_new(tmp_path):
    store = Store(tmp_path / "state.db")
    incident_id = _seed_incident(store)
    bad = _valid_payload(confidence="high")
    with pytest.raises(MineContractViolation):
        mine_incident(
            store, lambda prompt: dict(bad), _incident_row(store, incident_id),
            _miner_cfg(tmp_path), PROMPTS_DIR,
        )
    assert _incident_row(store, incident_id)["status"] == "new"
    assert store.query("SELECT * FROM learnings") == []


def test_mine_incident_dismisses_non_learning(tmp_path):
    store = Store(tmp_path / "state.db")
    incident_id = _seed_incident(store)
    payload = _valid_payload(
        is_real_learning=False,
        incident_summary="",
        generalized_rule="",
        why="",
        confidence=0.1,
    )
    result = mine_incident(
        store, lambda prompt: dict(payload), _incident_row(store, incident_id),
        _miner_cfg(tmp_path), PROMPTS_DIR,
    )
    assert result is None
    assert _incident_row(store, incident_id)["status"] == "dismissed"
    assert store.query("SELECT * FROM learnings") == []


def test_mine_incident_malformed_window_json_raises(tmp_path):
    store = Store(tmp_path / "state.db")
    incident_id = _seed_incident(store)
    store.update("incidents", "id", incident_id, {"window_json": "not json"})
    store.commit()
    with pytest.raises(MinerError, match="window_json"):
        mine_incident(
            store, lambda prompt: None, _incident_row(store, incident_id),
            _miner_cfg(tmp_path), PROMPTS_DIR,
        )
