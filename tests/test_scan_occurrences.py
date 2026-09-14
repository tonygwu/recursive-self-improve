"""Signal occurrences: identity, uncapped counts, fingerprints, and cross-append context.

Acceptance cases 4, 5, and 7 of docs/dashboard-parity/PARALLEL_WORK.md. Codex
record helpers here are shared with test_scan_exposure.py.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from self_improve import scan_observations as so
from self_improve.scan import mark_for_rescan
from self_improve.store import Store
from tests.test_scan_observations import (
    PROJECT,
    SID,
    SID_B,
    Env,
    append,
    c_result,
    c_tool,
    c_user,
    count,
    exposure,
    jsonl,
    make_env,
    make_repo,
    scan,
    ts,
    write_claude,
)

BETA = "remote:github.com/example/beta"


def x_meta(when: str, cwd: str, sid: str = SID) -> dict:
    return {"timestamp": when, "type": "session_meta", "payload": {
        "id": sid, "session_id": sid, "cwd": cwd, "originator": "Codex Desktop", "source": "vscode"}}


def x_user(text: str, when: str) -> dict:
    return {"timestamp": when, "type": "event_msg", "payload": {"type": "user_message", "message": text}}


def x_call(when: str, call_id: str) -> dict:
    return {"timestamp": when, "type": "response_item", "payload": {
        "type": "function_call", "name": "exec_command", "call_id": call_id, "arguments": "{}"}}


def x_output(when: str, call_id: str, code: int = 1) -> dict:
    return {"timestamp": when, "type": "response_item", "payload": {
        "type": "function_call_output", "call_id": call_id,
        "output": f"Chunk ID: a\nWall time: 1\nProcess exited with code {code}\nOutput:\nboom"}}


def x_tokens(when: str) -> dict:
    return {"timestamp": when, "type": "event_msg", "payload": {"type": "token_count", "info": None}}


def write_codex(env: Env, records: list[dict], name: str = "rollout-2026-08-10T10-00-00-a.jsonl") -> Path:
    directory = env.codex_dir / "2026" / "08" / "10"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(jsonl(records))
    return path


def active_signal_occurrences(store: Store) -> list[tuple[int, str]]:
    return [
        (row["trigger_line_no"], row["signal_type"])
        for row in store.query(
            "SELECT trigger_line_no, signal_type FROM scan_occurrences "
            "WHERE active = 1 AND kind = 'signal' ORDER BY trigger_line_no, signal_type"
        )
    ]


# ---------------------------------------------------------------------------
# Case 4: distinct occurrences survive; queue caps do not cap measurement.
# ---------------------------------------------------------------------------


def test_different_signals_and_distinct_same_time_occurrences_all_survive(tmp_path):
    env = make_env(tmp_path)
    repo = env.repo
    write_claude(env, [
        c_user("start the job", ts(0), repo),
        c_tool(ts(1), repo),
        c_result("ok", ts(2), repo),
        c_user("no, that's wrong. from now on run the tests", ts(3), repo),
        c_tool(ts(4), repo, tool_id="t2"),
        c_user("no, wrong file", ts(5), repo),
        c_user("no, wrong line", ts(5), repo),
    ])
    stats = scan(env, "run-1")
    assert active_signal_occurrences(env.store) == [
        (4, "correction"), (4, "standing_instruction"), (6, "correction"), (7, "correction")]
    # The incident queue keeps its (file, signal, timestamp) dedupe; measurement does not.
    assert stats.incidents_deduped == 1 and count(env.store, "SELECT id FROM incidents") == 3
    assert stats.measurement["incident_links"] == 2
    assert stats.measurement["incident_links_ambiguous"] == 1, "same-time pair is not guessed"
    result = exposure(env.store)
    assert result["occurrences"] == 4
    assert result["occurrences_by_signal"]["correction"] == 3
    assert result["occurrences_by_signal"]["standing_instruction"] == 1


def test_identical_detections_on_one_line_are_distinguished_by_their_position(tmp_path):
    env = make_env(tmp_path)
    text = {"type": "text", "text": "The root cause was a stale cache."}
    write_claude(env, [
        c_user("check it", ts(0), env.repo),
        c_tool(ts(1), env.repo),
        c_result("log output", ts(2), env.repo),
        {"type": "assistant", "timestamp": ts(3), "cwd": env.repo, "sessionId": SID,
         "entrypoint": "cli", "message": {"role": "assistant", "content": [text, text]}},
    ])
    scan(env, "run-1")
    rows = env.store.query(
        "SELECT occurrence_id, discriminator, evidence_json FROM scan_occurrences "
        "WHERE active = 1 AND signal_type = 'self_observation'"
    )
    assert len(rows) == 2
    assert rows[0]["evidence_json"] == rows[1]["evidence_json"]
    assert len({r["discriminator"] for r in rows}) == 2
    assert env.store.query_one("SELECT events FROM scan_lines WHERE line_no = 4")["events"] == 2


def test_the_mining_queue_cap_never_caps_measurement(tmp_path):
    env = make_env(tmp_path, max_incidents_per_signal_per_session=1)
    records = []
    for n, word in enumerate(("first", "second", "third")):
        records += [c_tool(ts(2 * n), env.repo, tool_id=f"t{n}"), c_user(f"no, {word}", ts(2 * n + 1), env.repo)]
    write_claude(env, records)
    stats = scan(env, "run-1")
    assert stats.dropped_by_cap == {"correction": 2}
    assert count(env.store, "SELECT id FROM incidents") == 1
    assert exposure(env.store)["occurrences"] == 3
    record = so.scan_history(env.store, session_file=str(
        env.claude_dir / "-work-alpha" / f"{SID}.jsonl"))["records"][0]["record"]
    assert record["counts"]["queue_dropped_by_cap"] == {"correction": 2}
    assert record["caps"] == {"max_incidents_per_signal_per_session": 1, "measurement_capped": False}


# ---------------------------------------------------------------------------
# Case 5: fingerprint occurrences and cross-session promotion.
# ---------------------------------------------------------------------------


def test_fingerprints_keep_each_occurrence_and_promotion_is_not_a_primary_count(tmp_path):
    env = make_env(tmp_path)
    repo = env.repo
    first = write_claude(env, [
        c_tool("2026-08-31T23:58:00Z", repo),
        c_result("Error: boom", "2026-08-31T23:59:00Z", repo, is_error=True),
        c_result("Error: boom", "2026-09-01T00:01:00Z", repo, is_error=True),
        c_result("Error: boom", "2026-09-01T00:02:00Z", repo, is_error=True),
    ])
    write_claude(env, [c_result("Error: boom", "2026-08-20T09:00:00Z", repo, sid=SID_B, is_error=True)],
                 name=SID_B)
    stats = scan(env, "run-1")
    assert stats.promoted_repeated_errors == 1

    def window(start: str, end: str) -> tuple[int, int]:
        result = exposure(env.store, start=start, end=end, signal_types=("repeated_error",))
        return result["occurrences"], result["diagnostics"]["error_fingerprint_occurrences"]

    assert window("2026-08-01T00:00:00Z", "2026-09-01T00:00:00Z") == (0, 2)
    assert window("2026-09-01T00:00:00Z", "2026-10-01T00:00:00Z") == (1, 2)
    assert window("2026-08-01T00:00:00Z", "2026-10-01T00:00:00Z") == (1, 4)

    promoted = env.store.query_one(
        "SELECT id FROM incidents WHERE signal_type = 'repeated_error' "
        "AND matched_text IN (SELECT fingerprint FROM error_fingerprints)"
    )["id"]
    assert count(env.store, "SELECT id FROM scan_incident_links WHERE incident_id = ?", (promoted,)) == 0
    assert so.scan_history(env.store, incident_id=promoted)["reason"] == "legacy_unknown_provenance"

    mark_for_rescan(env.store, env.cfg)
    scan(env, "run-2")
    assert window("2026-08-01T00:00:00Z", "2026-10-01T00:00:00Z") == (1, 4)
    assert env.store.query_one(
        "SELECT count_in_session FROM error_fingerprints WHERE session_file = ?", (str(first),)
    )["count_in_session"] == 3, "a complete re-read must not add the same errors again"
    assert count(env.store, "SELECT id FROM incidents WHERE signal_type = 'repeated_error'") == 2


# ---------------------------------------------------------------------------
# Case 7: incremental scans match one whole-file scan.
# ---------------------------------------------------------------------------


def _projection(store: Store) -> tuple[list, list]:
    lines = store.query(
        "SELECT line_no, line_key, exclusion, project_key, working_copy_id, logical_session_key, "
        "occurred_at, category, cause, events FROM scan_lines WHERE active = 1 "
        "ORDER BY transcript_id, line_no"
    )
    occurrences = store.query(
        "SELECT occurrence_id, kind, signal_type, trigger_line_no, supporting_lines_json, "
        "occurred_at, project_key, exclusion FROM scan_occurrences WHERE active = 1 "
        "ORDER BY occurrence_id"
    )
    return lines, occurrences


def test_incremental_scans_match_one_whole_file_scan(tmp_path):
    env = make_env(tmp_path)
    beta = make_repo(tmp_path / "work" / "beta", "git@github.com:example/beta.git")
    claude = write_claude(env, [c_user("run it", ts(0), env.repo), c_tool(ts(1), env.repo)])
    codex = write_codex(env, [
        x_meta(ts(0), env.repo), x_user("run the build", ts(1)), x_call(ts(2), "c1"), x_tokens(ts(3))])
    scan(env, "run-1")
    append(claude, jsonl([c_result("done", ts(2), env.repo), c_user("no, that's wrong", ts(3), env.repo)]))
    append(codex, jsonl([
        x_output(ts(4), "c1"), x_user("no, that's wrong", ts(5)),
        x_meta(ts(6), beta), x_call(ts(7), "c2"), x_user("no, still wrong", ts(8))]))
    stats = scan(env, "run-2")
    assert stats.measurement["full_reparses"] == 2

    whole = dataclasses.replace(env, store=Store(tmp_path / "whole.db"))
    scan(whole, "run-1")
    assert _projection(env.store) == _projection(whole.store)

    lines, occurrences = _projection(env.store)
    corrections = sorted(
        (o["trigger_line_no"], o["project_key"]) for o in occurrences if o["signal_type"] == "correction")
    assert corrections == [(4, PROJECT), (6, PROJECT), (9, BETA)]
    assert sorted(line["line_no"] for line in lines if line["project_key"] == BETA) == [7, 8, 9]
    fingerprint = [o for o in occurrences if o["kind"] == "error_fingerprint"]
    assert [(o["trigger_line_no"], o["project_key"]) for o in fingerprint] == [(5, PROJECT)]
    # Documented limit: the incremental incident queue keeps chunk-only context,
    # so it misses corrections whose tool call came before the append.
    assert count(env.store, "SELECT id FROM incidents") == 1
    assert count(whole.store, "SELECT id FROM incidents") == 3
    assert json.loads(occurrences[0]["supporting_lines_json"])
