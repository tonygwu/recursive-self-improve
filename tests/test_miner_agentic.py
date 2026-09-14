"""Tests for the agentic mine path: sandbox build, dedup contract, persistence.

The injected agentic_call returns scripted responses without model calls.
Persistence tests mainly exercise the archived-window fallback. A public Claude
fixture with replacement text and placeholder identities exercises the full
transcript sandbox, including the learnings dump."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from self_improve.config import Config
from self_improve.miner import (
    AGED_OUT_NOTE,
    MINE_AGENTIC_EXTRA_KEYS,
    MineContractViolation,
    MinerError,
    _write_learnings_dump,
    MineParseFailure,
    mine_incident_agentic,
    validate_mine_json,
)
from self_improve.store import Store, new_id, utc_now_iso

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
CLAUDE_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "claude" / "main_cli.jsonl"


def agentic_payload(**over) -> dict:
    payload = {
        "is_real_learning": True,
        "incident_summary": "Agent trusted exit 0 from a substituted model.",
        "generalized_rule": "**Never trust exit 0 alone** — read one full raw response first.",
        "why": "Prevents silent model substitution from poisoning results.",
        "scope_guess": "global",
        "category": "verification",
        "duplicate_of_existing_rule": None,
        "confidence": 0.9,
        "dedup_decision": "new",
        "dedup_target_id": "",
        "amended_rule_text": "",
        "amended_why": "",
        # Part 2 contract additions: enforcement-gap provenance and
        # path-scoped routing globs.
        "violated_existing_rule": "",
        "path_globs": [],
    }
    payload.update(over)
    return payload


def seed_incident(
    store: Store, session_file: str, project="/proj/alpha",
    ts="2026-08-01T00:30:00Z",
) -> dict:
    store.upsert_session(
        {
            "file_path": session_file,
            "source": "claude",
            "session_id": "sess-ag",
            "project_path": project,
            "headless": 0,
            "is_subagent": 0,
            "first_ts": "2026-08-01T00:00:00Z",
            "last_ts": "2026-08-01T01:00:00Z",
            "mtime": 0.0,
            "file_size": 10,
            "bytes_scanned": 10,
            "lines_scanned": 5,
            "malformed_lines": 0,
            "status": "ok",
            "error": "",
            "last_scanned_at": utc_now_iso(),
        }
    )
    incident_id = store.insert_incident(
        {
            "session_file": session_file,
            "session_id": "sess-ag",
            "project_path": project,
            "ts": ts,
            "signal_type": "correction",
            "matched_text": "no, that's wrong",
            "window": [{"role": "human", "ts": "2026-08-01T00:30:00Z", "text": "no, that's wrong"}],
            "score": 0.8,
        }
    )
    store.commit()
    return store.query_one("SELECT * FROM incidents WHERE id = ?", (incident_id,))


def seed_learning(store: Store, status="proposed", rule="**Old rule** — original text.") -> dict:
    lid = new_id()
    store.insert(
        "learnings",
        {
            "id": lid,
            "rule_text": rule,
            "why": "old why",
            "status": status,
            "evidence_count": 2,
            "project_count": 1,
            "projects_json": json.dumps(["/proj/other"]),
            "created_at": utc_now_iso(),
        },
    )
    store.commit()
    return store.query_one("SELECT * FROM learnings WHERE id = ?", (lid,))


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "s.db")
    cfg = Config(global_claude_md=str(tmp_path / "gclaude.md"))
    # Session file that does NOT exist -> aged-out fallback sandbox (no
    # transcript parse needed); the stored window is the evidence.
    incident = seed_incident(store, str(tmp_path / "gone.jsonl"))
    return store, cfg, incident, tmp_path


def run(store, cfg, incident, tmp_path, payload):
    captured = {}

    def agentic_call(prompt, sandbox_dir):
        captured["prompt"] = prompt
        captured["sandbox"] = Path(sandbox_dir)
        return payload

    result = mine_incident_agentic(
        store, agentic_call, incident, cfg, PROMPTS_DIR, tmp_path / "mine"
    )
    return result, captured


# ------------------------------------------------------------ contract

def test_old_fast_contract_payload_is_violation(env):
    store, cfg, incident, tmp_path = env
    old_style = {
        k: v
        for k, v in agentic_payload().items()
        if k not in ("dedup_decision", "dedup_target_id", "amended_rule_text", "amended_why")
    }
    with pytest.raises(MineContractViolation, match="missing keys"):
        run(store, cfg, incident, tmp_path, old_style)
    assert store.query_one(
        "SELECT status FROM incidents WHERE id = ?", (incident["id"],)
    )["status"] == "new"


@pytest.mark.parametrize(
    "over, msg",
    [
        ({"dedup_decision": "merge"}, "dedup_decision must be one of"),
        ({"dedup_decision": "new", "dedup_target_id": "abc"}, "must be empty"),
        ({"dedup_decision": "duplicate"}, "must name a learning id"),
        ({"dedup_decision": "amend", "dedup_target_id": "x"}, "amended_rule_text"),
    ],
)
def test_validate_agentic_cross_field_matrix(over, msg):
    errors = validate_mine_json(agentic_payload(**over), agentic=True)
    assert any(msg in e for e in errors), errors


def test_fast_contract_unchanged_without_agentic_flag():
    # Strip via the module's own constant, so adding an agentic-only key
    # can never silently break this test's premise.
    old_style = {
        k: v
        for k, v in agentic_payload().items()
        if k not in MINE_AGENTIC_EXTRA_KEYS
    }
    assert validate_mine_json(old_style) == []
    # And the agentic keys are UNEXPECTED for the fast contract.
    assert validate_mine_json(agentic_payload())


# ------------------------------------------------------------ decisions

def test_decision_new_inserts_learning(env):
    store, cfg, incident, tmp_path = env
    result, captured = run(store, cfg, incident, tmp_path, agentic_payload())
    assert result["_dedup"] == "new"
    assert (captured["sandbox"] / "transcript.md").exists()
    assert (captured["sandbox"] / "learnings.jsonl").exists()
    assert "search-learnings" in captured["prompt"]
    rows = store.query("SELECT * FROM learnings")
    assert len(rows) == 1 and rows[0]["rule_text"].startswith("**Never trust exit 0")


def test_decision_duplicate_grows_target_evidence(env):
    store, cfg, incident, tmp_path = env
    target = seed_learning(store)
    result, _ = run(
        store, cfg, incident, tmp_path,
        agentic_payload(dedup_decision="duplicate", dedup_target_id=target["id"]),
    )
    assert result["_dedup"] == "duplicate"
    refreshed = store.query_one("SELECT * FROM learnings WHERE id = ?", (target["id"],))
    assert refreshed["evidence_count"] == 3
    assert "/proj/alpha" in json.loads(refreshed["projects_json"])
    assert refreshed["project_count"] == 2
    # No new learnings row; incident mined and linked to the target.
    assert store.query_one("SELECT COUNT(*) n FROM learnings")["n"] == 1
    assert store.query_one(
        "SELECT status FROM incidents WHERE id = ?", (incident["id"],)
    )["status"] == "mined"
    assert store.query_one(
        "SELECT COUNT(*) n FROM incident_learnings WHERE learning_id = ?",
        (target["id"],),
    )["n"] == 1


def test_duplicate_of_rejected_dismisses_without_growth(env):
    store, cfg, incident, tmp_path = env
    target = seed_learning(store, status="rejected")
    result, _ = run(
        store, cfg, incident, tmp_path,
        agentic_payload(dedup_decision="duplicate", dedup_target_id=target["id"]),
    )
    assert result["_dedup"] == "duplicate_of_rejected"
    refreshed = store.query_one("SELECT * FROM learnings WHERE id = ?", (target["id"],))
    assert refreshed["evidence_count"] == 2  # unchanged
    assert store.query_one(
        "SELECT status FROM incidents WHERE id = ?", (incident["id"],)
    )["status"] == "dismissed"


def test_amend_of_proposed_updates_text_and_supersedes_open_proposal(env):
    store, cfg, incident, tmp_path = env
    target = seed_learning(store, status="proposed")
    pid = new_id()
    store.insert(
        "proposals",
        {
            "id": pid,
            "learning_id": target["id"],
            "target_path": "/x",
            "target_kind": "global_claude_md",
            "action": "add",
            "status": "pending",
            "created_at": utc_now_iso(),
        },
    )
    result, _ = run(
        store, cfg, incident, tmp_path,
        agentic_payload(
            dedup_decision="amend",
            dedup_target_id=target["id"],
            amended_rule_text="**Old rule, broadened** — now covers the second mechanism.",
            amended_why="broader why",
        ),
    )
    assert result["_dedup"] == "amend"
    refreshed = store.query_one("SELECT * FROM learnings WHERE id = ?", (target["id"],))
    assert refreshed["rule_text"].startswith("**Old rule, broadened**")
    assert refreshed["why"] == "broader why"
    assert refreshed["status"] == "candidate"  # back to the pool for re-propose
    assert refreshed["evidence_count"] == 3
    prop = store.query_one("SELECT status FROM proposals WHERE id = ?", (pid,))
    assert prop["status"] == "superseded"
    events = store.query(
        "SELECT event FROM proposal_events WHERE proposal_id = ?", (pid,)
    )
    assert [e["event"] for e in events] == ["superseded"]


def test_amend_of_applied_updates_text_for_edit_proposal(env):
    # Part 2: amend-of-applied is no longer downgraded to duplicate. The text
    # IS updated and the learning returns to the candidate pool; the pipeline
    # then builds an in-place EDIT proposal against the live file's si: marker
    # (see propose.build_edit_proposal) rather than re-routing it as an add.
    store, cfg, incident, tmp_path = env
    target = seed_learning(store, status="applied")
    result, _ = run(
        store, cfg, incident, tmp_path,
        agentic_payload(
            dedup_decision="amend",
            dedup_target_id=target["id"],
            amended_rule_text="**Old rule, corrected** — now covers the real mechanism.",
            amended_why="corrected why",
        ),
    )
    assert result["_dedup"] == "amend_applied"
    refreshed = store.query_one("SELECT * FROM learnings WHERE id = ?", (target["id"],))
    assert refreshed["rule_text"].startswith("**Old rule, corrected**")
    assert refreshed["why"] == "corrected why"
    assert refreshed["status"] == "candidate"  # re-proposed as an edit
    assert refreshed["evidence_count"] == 3


def test_hallucinated_target_id_is_contract_violation(env):
    store, cfg, incident, tmp_path = env
    with pytest.raises(MineContractViolation, match="does not exist"):
        run(
            store, cfg, incident, tmp_path,
            agentic_payload(dedup_decision="duplicate", dedup_target_id="nope123"),
        )
    assert store.query_one(
        "SELECT status FROM incidents WHERE id = ?", (incident["id"],)
    )["status"] == "new"
    assert store.query_one("SELECT COUNT(*) n FROM incident_learnings")["n"] == 0


# ------------------------------------------------------------ sandbox

def test_full_sandbox_from_public_fixture_includes_learnings_dump(tmp_path):
    store = Store(tmp_path / "s.db")
    cfg = Config(global_claude_md=str(tmp_path / "gclaude.md"))
    session_file = tmp_path / "session.jsonl"
    shutil.copy(CLAUDE_FIXTURE, session_file)
    # The incident pointer must name a ts that exists in the fixture's events.
    incident = seed_incident(store, str(session_file), ts="2026-08-01T12:00:01.000Z")
    seed_learning(store, rule="**Existing** — visible to the agent sk-plantedsecret1234567890abc")
    result, captured = run(store, cfg, incident, tmp_path, agentic_payload())
    sandbox = captured["sandbox"]
    assert (sandbox / "transcript.md").exists()
    assert (sandbox / "environment.md").exists()
    dump = (sandbox / "learnings.jsonl").read_text()
    assert "sk-plantedsecret1234567890abc" not in dump  # defensively redacted
    assert "[REDACTED:" in dump
    assert result["_dedup"] == "new"


def test_learnings_dump_shape(tmp_path):
    store = Store(tmp_path / "s.db")
    seed_learning(store)
    _write_learnings_dump(store, tmp_path)
    rows = [json.loads(l) for l in (tmp_path / "learnings.jsonl").read_text().splitlines()]
    assert len(rows) == 1
    assert set(rows[0]) == {
        "id", "status", "rule_text", "why", "category", "scope",
        "evidence_count", "project_count",
    }


# ------------------------------------------------------------ idempotency

def test_relinking_same_incident_and_learning_is_idempotent(env):
    """Re-mining the same incident and learning must preserve their single link."""
    store, cfg, incident, tmp_path = env
    target = seed_learning(store)
    payload = agentic_payload(dedup_decision="duplicate", dedup_target_id=target["id"])
    run(store, cfg, incident, tmp_path, payload)
    # Second pass over the same incident (retry / rescan) — must not raise.
    store.update("incidents", "id", incident["id"], {"status": "new"})
    store.commit()
    result, _ = run(store, cfg, incident, tmp_path, payload)
    assert result["_dedup"] == "duplicate"
    assert store.query_one(
        "SELECT COUNT(*) n FROM incident_learnings WHERE incident_id = ? AND learning_id = ?",
        (incident["id"], target["id"]),
    )["n"] == 1  # exactly one link, not two


# ---------------------------------------------------------------------------
# the prompt must describe the tool the agent actually gets
# ---------------------------------------------------------------------------


def test_prompt_documents_the_search_output_shape_it_will_receive():
    """The dedup tool's output changed under the prompt once already.

    search-learnings started returning {results, meta} and including in-force
    instruction rules alongside mined learnings, while the prompt still said it
    "prints the closest existing learnings". An agent that put a rule_unit's
    `rule:`-prefixed id into dedup_target_id would trip MineContractViolation —
    a trap created by changing one side of a contract.
    """
    from pathlib import Path

    text = (
        Path(__file__).resolve().parent.parent / "prompts" / "mine_incident_agentic.md"
    ).read_text()

    assert '"results"' in text and "meta" in text, "output shape not described"
    assert "rule_unit" in text, "the agent is not told rule_unit results exist"
    assert "duplicate_of_existing_rule" in text
    # The specific trap: a rule_unit id is not a learning id.
    assert "dedup_target_id" in text
    lower = text.lower()
    assert "not a learning id" in lower or "is not a learning id" in lower


def test_prompt_tells_the_agent_how_to_notice_crowd_out():
    """Explain how returned_kinds and corpus counts reveal result crowding."""
    from pathlib import Path

    text = (
        Path(__file__).resolve().parent.parent / "prompts" / "mine_incident_agentic.md"
    ).read_text()
    assert "returned_kinds" in text
    assert "cut" in text


def test_prompt_turn_budget_is_derived_from_config_not_hardcoded(tmp_path):
    """The prompt's exploration allowance must reflect the enforced turn limit."""
    import dataclasses
    from pathlib import Path

    from self_improve.config import Config
    from self_improve.miner import TURN_RESERVE, render_prompt

    cfg = dataclasses.replace(Config(), mine_agent_max_turns=40)
    rendered = render_prompt(
        Path(__file__).resolve().parent.parent / "prompts" / "mine_incident_agentic.md",
        {
            "signal_type": "correction",
            "matched_text": "x",
            "start_line": "1",
            "project": "/p",
            "search_cli": "/bin/true",
            "max_turns": str(cfg.mine_agent_max_turns),
            "explore_budget": str(cfg.mine_agent_max_turns - TURN_RESERVE),
        },
    )
    assert "capped at 40 tool-use turns" in rendered
    assert f"by turn {40 - TURN_RESERVE}" in rendered
    assert "fifteen" not in rendered


def test_prompt_warns_that_environment_md_is_truncated():
    """Warn when environment.md contains only part of an instruction file.
    The agent must use full-file search before treating an apparent rule as new.
    """
    from pathlib import Path

    from self_improve.miner import IN_FORCE_TRUNCATE_CHARS

    text = (
        Path(__file__).resolve().parent.parent / "prompts" / "mine_incident_agentic.md"
    ).read_text()
    assert str(IN_FORCE_TRUNCATE_CHARS) in text.replace(",", ""), (
        "the prompt must state the actual truncation limit"
    )
    assert "NOT a complete copy" in text
    assert "rule_unit" in text


def test_aged_out_fallback_handles_the_promoted_repeated_error_window(tmp_path):
    """The aged-out fallback accepts both stored window shapes.

    Ordinary windows hold turns; promoted repeated errors hold occurrences with
    source paths and counts. Preserve that evidence when the transcript is gone."""
    store = Store(tmp_path / "s.db")
    cfg = Config(global_claude_md=str(tmp_path / "gclaude.md"))
    gone = str(tmp_path / "gone.jsonl")
    store.upsert_session(
        {
            "file_path": gone, "source": "claude", "session_id": "sess-p",
            "project_path": "/proj/alpha", "headless": 0, "is_subagent": 0,
            "first_ts": "2026-08-01T00:00:00Z", "last_ts": "2026-08-01T01:00:00Z",
            "mtime": 0.0, "file_size": 10, "bytes_scanned": 10, "lines_scanned": 5,
            "malformed_lines": 0, "status": "ok", "error": "",
            "last_scanned_at": utc_now_iso(),
        }
    )
    incident_id = store.insert_incident(
        {
            "session_file": gone, "session_id": "sess-p", "project_path": "/proj/alpha",
            "ts": "2026-01-02T10:00:00.000Z", "signal_type": "repeated_error",
            "matched_text": "deadbeef" * 5,
            # Invented occurrences. The incident timestamp must match one
            # occurrence so the rendered pointer resolves to that event.
            "window": [
                {"ts": "2026-01-02T10:00:00.000Z", "text": "File content (300.0KB) exceeds maximum allowed size",
                 "session_file": "/x/a.jsonl", "project_path": "/proj/alpha", "count_in_session": 3},
                {"ts": "2026-01-03T11:00:00.000Z", "text": "File content (400.0KB) exceeds maximum allowed size",
                 "session_file": "/x/b.jsonl", "project_path": "/proj/alpha", "count_in_session": 2},
            ],
            "score": 0.9,
        }
    )
    store.commit()
    incident = store.query_one("SELECT * FROM incidents WHERE id = ?", (incident_id,))

    captured = {}

    def agentic_call(prompt, sandbox_dir):
        captured["sandbox"] = Path(sandbox_dir)
        return None  # parse failure is fine; we are testing the sandbox build

    # The stub returns None, so the mine itself fails as a parse failure — the
    # point is that the sandbox got BUILT, which previously raised MinerError
    # before the agent was ever called.
    with pytest.raises(MineParseFailure):
        mine_incident_agentic(
            store, agentic_call, incident, cfg, PROMPTS_DIR, tmp_path / "mine"
        )

    assert "sandbox" in captured, "the agent was never called: the sandbox build failed"
    transcript = (captured["sandbox"] / "transcript.md").read_text()
    # The evidence must survive into the transcript the agent reads.
    assert "exceeds maximum allowed size" in transcript
    # And the thing that makes a promoted error meaningful — that it recurred,
    # and where — must not be silently dropped.
    assert "3" in transcript and "2" in transcript, transcript[:400]
    assert "a.jsonl" in transcript or "b.jsonl" in transcript, transcript[:400]
    store.close()


# ---------------------------------------------------------------------------
# Optional fields with explicit, counted defaults
# ---------------------------------------------------------------------------


class TestOmittedOptionalKeys:
    """Missing optional fields use their documented empty defaults.
    Count every defaulted field so omission remains visible to the operator.
    """

    def _payload(self, **over):
        p = {
            "is_real_learning": True,
            "incident_summary": "s",
            "generalized_rule": "r",
            "why": "w",
            "scope_guess": "project",
            "category": "tooling",
            "duplicate_of_existing_rule": "",
            "confidence": 0.8,
            "dedup_decision": "new",
            "dedup_target_id": "",
            "amended_rule_text": "",
            "amended_why": "",
        }
        p.update(over)
        return p

    def test_a_response_missing_both_keys_is_accepted(self):
        from self_improve.miner import validate_mine_json

        assert validate_mine_json(self._payload(), agentic=True) == []

    def test_the_defaults_applied_are_the_documented_empties(self):
        from self_improve.miner import normalize_mine_payload

        p = self._payload()
        filled, defaulted = normalize_mine_payload(p, agentic=True)
        assert filled["violated_existing_rule"] == ""
        assert filled["path_globs"] == []
        assert sorted(defaulted) == ["path_globs", "violated_existing_rule"]

    def test_defaulting_is_counted_not_silent(self):
        """A cap or fallback that nobody can see is how a pipeline lies."""
        from self_improve.miner import normalize_mine_payload

        _, none_needed = normalize_mine_payload(
            self._payload(violated_existing_rule="", path_globs=[]), agentic=True
        )
        assert none_needed == []

    def test_a_present_value_is_never_overwritten(self):
        from self_improve.miner import normalize_mine_payload

        filled, _ = normalize_mine_payload(
            self._payload(violated_existing_rule="always run tests", path_globs=["*.py"]),
            agentic=True,
        )
        assert filled["violated_existing_rule"] == "always run tests"
        assert filled["path_globs"] == ["*.py"]

    def test_genuinely_missing_keys_still_fail_loud(self):
        """Only these two are optional. Dropping a real field is still a violation."""
        from self_improve.miner import validate_mine_json

        p = self._payload()
        del p["generalized_rule"]
        errs = validate_mine_json(p, agentic=True)
        assert errs and "generalized_rule" in errs[0]

    def test_the_fast_path_contract_is_unchanged(self):
        """Non-agentic responses never carried these keys in the first place."""
        from self_improve.miner import validate_mine_json

        p = {k: v for k, v in self._payload().items()
             if k not in {"dedup_decision", "dedup_target_id",
                          "amended_rule_text", "amended_why"}}
        assert validate_mine_json(p, agentic=False) == []


# ---------------------------------------------------------------------------
# The hard rule: nothing unredacted reaches the agent's context
# ---------------------------------------------------------------------------

SECRET = "sk-ant-api03-PLANTEDSECRETVALUE1234567890abcdefXYZ"


def _session_with_a_secret(path: Path) -> None:
    """A minimal Claude transcript whose tool output leaks a key."""
    lines = [
        {
            "type": "user", "isMeta": False, "uuid": "u-1",
            "timestamp": "2026-08-01T00:30:00.000Z",
            "message": {"role": "user", "content": "no, that's wrong"},
        },
        {
            "type": "assistant", "uuid": "a-1", "parentUuid": "u-1",
            "timestamp": "2026-08-01T00:30:05.000Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": f"I will export {SECRET} first."}],
            },
        },
    ]
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")


def test_the_transcript_the_agent_reads_carries_no_secret(tmp_path):
    """The full-transcript path. This is the biggest surface in the product:
    the agentic miner reads the WHOLE session, and AGENTS.md makes redaction
    before any LLM prompt a hard rule. The learnings dump had a test; the
    transcript did not."""
    store = Store(tmp_path / "s.db")
    cfg = Config(global_claude_md=str(tmp_path / "gclaude.md"))
    session_file = tmp_path / "leaky.jsonl"
    _session_with_a_secret(session_file)
    incident = seed_incident(store, str(session_file), ts="2026-08-01T00:30:00.000Z")

    _, captured = run(store, cfg, incident, tmp_path, agentic_payload())
    transcript = (captured["sandbox"] / "transcript.md").read_text()

    assert SECRET not in transcript, "an API key reached the agent's context"
    assert "[REDACTED:" in transcript, transcript[:400]
    # The surrounding evidence must survive; redaction that eats the context
    # would make the transcript useless.
    assert "export" in transcript
    # And it must not reach the PROMPT either.
    assert SECRET not in captured["prompt"]
    store.close()


def test_the_aged_out_fallback_transcript_carries_no_secret(tmp_path):
    """The other path, used when the transcript is gone and `window_json` is
    the only surviving evidence. It re-redacts on render; this proves it,
    including for a window row written before redaction existed."""
    store = Store(tmp_path / "s.db")
    cfg = Config(global_claude_md=str(tmp_path / "gclaude.md"))
    session_file = tmp_path / "gone.jsonl"
    _session_with_a_secret(session_file)
    incident = seed_incident(store, str(session_file), ts="2026-08-01T00:30:00.000Z")
    # An UNREDACTED window, as a row archived before redact.py covered this
    # pattern would look. Re-redacting on render is the only thing standing
    # between it and the agent.
    store.update(
        "incidents", "id", incident["id"],
        {"window_json": json.dumps(
            [{"role": "human", "ts": "2026-08-01T00:30:00.000Z",
              "text": f"no, that's wrong, use {SECRET}"}]
        )},
    )
    store.commit()
    incident = store.query_one("SELECT * FROM incidents WHERE id = ?", (incident["id"],))
    session_file.unlink()  # the transcript aged out

    _, captured = run(store, cfg, incident, tmp_path, agentic_payload())
    transcript = (captured["sandbox"] / "transcript.md").read_text()

    assert AGED_OUT_NOTE in transcript, "the agent was not told the evidence is partial"
    assert SECRET not in transcript, "an API key reached the agent's context"
    assert SECRET not in captured["prompt"]
    store.close()


# ------------------------------------------------------- projects_json guard


def _corrupt_projects_json(store: Store, learning_id: str, raw: str) -> None:
    """Write a raw (invalid) value straight into the strict-JSON column."""
    store.conn.execute(
        "UPDATE learnings SET projects_json = ? WHERE id = ?", (raw, learning_id)
    )
    store.commit()


@pytest.mark.parametrize("decision", ["duplicate", "amend"])
def test_corrupt_projects_json_names_the_learning_not_a_character_offset(env, decision):
    """The sixth reader of this column, and the last one left unguarded.

    `projects_json` is one of our own strict-JSON columns. A bare `json.loads`
    surfaces the likeliest corruption — a row that is not JSON at all — as a
    JSONDecodeError carrying a character offset and no owner key, in the middle
    of a dedup merge. routing.py, cluster.py, search.py and the retrieval eval
    were each fixed for the same shape; this call site had the guard but no
    test, so deleting it broke nothing.

    Both dedup decisions reach the same parse, so both are covered: `amend`
    would otherwise be able to lose the guard silently.
    """
    store, cfg, incident, tmp_path = env
    target = seed_learning(store)
    _corrupt_projects_json(store, target["id"], "{not json")

    payload = agentic_payload(
        dedup_decision=decision,
        dedup_target_id=target["id"],
        **(
            {"amended_rule_text": "**New rule** — amended.", "amended_why": "new why"}
            if decision == "amend"
            else {}
        ),
    )
    with pytest.raises(MinerError) as exc:
        run(store, cfg, incident, tmp_path, payload)

    msg = str(exc.value)
    assert target["id"] in msg, f"the failure must name the row: {msg}"
    assert "projects_json" in msg
    assert "DB invariant" in msg


def test_corrupt_projects_json_leaves_the_row_untouched(env):
    """A guard that raises AFTER a partial write would be worse than none.

    The parse sits above every `store.update` in `_persist_dedup_decision`, so
    a corrupt row must abort the merge with the learning and the incident
    exactly as they were.
    """
    store, cfg, incident, tmp_path = env
    target = seed_learning(store)
    _corrupt_projects_json(store, target["id"], "{not json")

    with pytest.raises(MinerError):
        run(
            store, cfg, incident, tmp_path,
            agentic_payload(dedup_decision="duplicate", dedup_target_id=target["id"]),
        )

    row = store.query_one("SELECT * FROM learnings WHERE id = ?", (target["id"],))
    assert row["evidence_count"] == 2, "evidence grew despite the abort"
    assert row["project_count"] == 1
    assert row["projects_json"] == "{not json", "the corrupt value was rewritten"
    assert store.query_one(
        "SELECT status FROM incidents WHERE id = ?", (incident["id"],)
    )["status"] == "new", "the incident must stay retryable"


def test_valid_projects_json_of_the_wrong_shape_also_fails(env):
    """Valid JSON of the WRONG SHAPE, which is the case that hid this guard.

    A JSON string parses fine, so it never reaches the JSONDecodeError branch —
    and a string is iterable, so `set(json.loads(...))` unions its CHARACTERS
    into the project set. `_persist_dedup_decision` then WRITES the derived
    `project_count` back, and routing promotes a rule to the global
    instruction file at `project_count >= global_promotion_min_projects` (3).

    Measured before the fix: `'"/proj/single"'` plus the incident's own project
    yields a set of 12 — one project silently becoming twelve, and a
    project-scoped lesson clearing the global-promotion bar.

    Four of the six readers of this column already check the shape
    (pipeline.py, cluster.py, backfill.py, and routing.py via `isinstance`).
    This was the one that did not, and the only one that writes a derived
    count back.
    """
    store, cfg, incident, tmp_path = env
    target = seed_learning(store)
    _corrupt_projects_json(store, target["id"], '"/proj/single"')

    with pytest.raises(MinerError) as exc:
        run(
            store, cfg, incident, tmp_path,
            agentic_payload(dedup_decision="duplicate", dedup_target_id=target["id"]),
        )
    msg = str(exc.value)
    assert target["id"] in msg, msg
    assert "list" in msg, f"the failure must say what shape was wrong: {msg}"

    row = store.query_one("SELECT * FROM learnings WHERE id = ?", (target["id"],))
    assert row["project_count"] == 1, "an inflated count was persisted"


# ------------------------------------------------------- evidence timestamps


def test_a_merge_keeps_the_chronological_extremes_not_the_latest_mined(env):
    """first_seen and last_seen bound all evidence, regardless of mining order.
    An older incident arriving later must extend the range rather than reverse it.
    """
    store, cfg, incident, tmp_path = env
    target = seed_learning(store)
    store.update(
        "learnings",
        "id",
        target["id"],
        {"first_seen": "2026-05-01T00:00:00Z", "last_seen": "2026-08-24T00:00:00Z"},
    )
    store.commit()
    # The arriving incident sits strictly BETWEEN the two stored stamps, so a
    # correct merge moves neither. This is the shape that a min/max cannot
    # pass by accident.
    assert "2026-05-01T00:00:00Z" < incident["ts"] < "2026-08-24T00:00:00Z"

    run(
        store,
        cfg,
        incident,
        tmp_path,
        agentic_payload(dedup_decision="duplicate", dedup_target_id=target["id"]),
    )

    refreshed = store.query_one(
        "SELECT * FROM learnings WHERE id = ?", (target["id"],)
    )
    assert refreshed["first_seen"] == "2026-05-01T00:00:00Z"
    assert refreshed["last_seen"] == "2026-08-24T00:00:00Z"
    assert refreshed["first_seen"] <= refreshed["last_seen"]


def test_a_merge_extends_the_range_in_both_directions(env):
    """An incident outside the stored range moves the stamp on that side only."""
    store, cfg, incident, tmp_path = env
    target = seed_learning(store)
    # Both stamps sit AFTER the arriving incident, so `first_seen` must move
    # back to it and `last_seen` must stay put.
    store.update(
        "learnings",
        "id",
        target["id"],
        {"first_seen": "2026-08-10T00:00:00Z", "last_seen": "2026-08-24T00:00:00Z"},
    )
    store.commit()

    run(
        store,
        cfg,
        incident,
        tmp_path,
        agentic_payload(dedup_decision="duplicate", dedup_target_id=target["id"]),
    )

    refreshed = store.query_one(
        "SELECT * FROM learnings WHERE id = ?", (target["id"],)
    )
    assert refreshed["first_seen"] == incident["ts"]
    assert refreshed["last_seen"] == "2026-08-24T00:00:00Z"


def test_an_empty_stored_stamp_does_not_win_the_comparison(env):
    """'' means unknown, not the epoch.

    ``min('', ts)`` is ``''`` for every real ISO stamp, so a naive min() would
    let a learning with no recorded start silently claim it has none forever.
    """
    store, cfg, incident, tmp_path = env
    target = seed_learning(store)
    assert target["first_seen"] == "" and target["last_seen"] == ""

    run(
        store,
        cfg,
        incident,
        tmp_path,
        agentic_payload(dedup_decision="duplicate", dedup_target_id=target["id"]),
    )

    refreshed = store.query_one(
        "SELECT * FROM learnings WHERE id = ?", (target["id"],)
    )
    assert refreshed["first_seen"] == incident["ts"]
    assert refreshed["last_seen"] == incident["ts"]
