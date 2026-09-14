"""Physical-line exposure: persistence, units, time, identity, and honest coverage.

Acceptance cases 1, 2, 3, 8, and 9 of docs/dashboard-parity/PARALLEL_WORK.md,
plus the largest synthetic fixture used to report volume.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from self_improve import scan_observations as so
from self_improve.scan import mark_for_rescan
from tests.test_scan_observations import (
    PROJECT,
    SID,
    SID_B,
    append,
    c_tool,
    c_user,
    correction_session,
    count,
    exposure,
    jsonl,
    make_env,
    make_repo,
    scan,
    ts,
    write_claude,
)
from tests.test_scan_occurrences import write_codex, x_call, x_meta, x_tokens, x_user

SID_C = "33333333-3333-4333-8333-333333333333"


def active_lines(store) -> int:
    return count(store, "SELECT id FROM scan_lines WHERE active = 1")


def latest_record(store, path) -> dict:
    return so.scan_history(store, session_file=str(path), limit=1)["records"][0]["record"]


# ---------------------------------------------------------------------------
# Case 1: real parsers through scan_all; every rescan path agrees.
# ---------------------------------------------------------------------------


def test_both_parsers_persist_and_unchanged_append_replay_and_rescan_agree(tmp_path):
    env = make_env(tmp_path)
    claude = write_claude(env, correction_session(env.repo))
    write_codex(env, [x_meta(ts(0), env.repo), x_user("go", ts(1)), x_call(ts(2), "c1"),
                      x_user("no, that's wrong", ts(3))])
    first = scan(env, "run-1")
    assert (first.measurement["observations_recorded"], first.measurement["lines_observed"]) == (2, 8)
    base = exposure(env.store)
    assert (base["computable"], base["eligible_lines"], base["occurrences"]) == (True, 8, 2)
    assert base["workload"]["eligible_lines"]["by_source"] == {"claude": 4, "codex": 4}

    unchanged = scan(env, "run-2")
    assert unchanged.files_skipped_unchanged == 2 and unchanged.measurement == {}
    assert exposure(env.store) == base

    append(claude, jsonl([c_user("thanks", ts(5), env.repo)]))
    codex = env.codex_dir / "2026" / "08" / "10" / "rollout-2026-08-10T10-00-00-a.jsonl"
    append(codex, jsonl([x_tokens(ts(5))]))
    scan(env, "run-3")
    grown = exposure(env.store)
    assert (grown["eligible_lines"], grown["occurrences"]) == (10, 2)

    env.store.conn.execute("UPDATE sessions SET mtime = 0")
    env.store.commit()
    assert scan(env, "run-4").files_succeeded == 2
    assert latest_record(env.store, claude)["reconciliation"]["lines"] == {
        "inserted": 0, "superseded": 0, "unchanged": 5, "removed": 0}
    assert exposure(env.store) == grown

    mark_for_rescan(env.store, env.cfg)
    scan(env, "run-5")
    assert exposure(env.store) == grown
    assert active_lines(env.store) == 10 and count(env.store, "SELECT id FROM scan_lines") == 10
    assert count(env.store, "SELECT id FROM incidents") == 2


# ---------------------------------------------------------------------------
# Case 2: one physical line is one unit; skipped lines keep exposure.
# ---------------------------------------------------------------------------


def test_one_line_is_one_unit_and_skipped_lines_keep_attributed_exposure(tmp_path):
    env = make_env(tmp_path)
    multi = {"type": "assistant", "timestamp": ts(1), "cwd": env.repo, "sessionId": SID,
             "entrypoint": "cli", "message": {"role": "assistant", "content": [
                 {"type": "text", "text": "Working."},
                 {"type": "tool_use", "id": "a", "name": "Bash", "input": {}},
                 {"type": "tool_use", "id": "b", "name": "Bash", "input": {}}]}}
    pr_link = {"type": "pr-link", "sessionId": SID, "prNumber": 1,
               "prUrl": "https://example.invalid/pull/1", "timestamp": ts(2)}
    mode = {"type": "mode", "sessionId": SID, "mode": "default"}
    write_claude(env, [c_user("hi", ts(0), env.repo), multi, pr_link, mode])
    write_codex(env, [x_meta(ts(0), env.repo), x_tokens(ts(1)), x_tokens(ts(2))])
    scan(env, "run-1")

    rows = env.store.query(
        "SELECT source, line_no, category, cause, events, exclusion, project_key "
        "FROM scan_lines WHERE active = 1 ORDER BY source, line_no"
    )
    by_position = {(r["source"], r["line_no"]): r for r in rows}
    assert by_position[("claude", 2)]["events"] == 3 and by_position[("claude", 2)]["exclusion"] == ""
    assert (by_position[("claude", 3)]["category"], by_position[("claude", 3)]["cause"],
            by_position[("claude", 3)]["exclusion"], by_position[("claude", 3)]["project_key"]) == (
        "skipped", "pr-link", "", PROJECT)
    assert by_position[("claude", 4)]["exclusion"] == "unknown_time"
    tokens = [r for r in rows if r["cause"] == "event_msg/token_count"]
    assert len(tokens) == 2 and all(r["exclusion"] == "" and r["project_key"] == PROJECT for r in tokens)

    result = exposure(env.store)
    assert result["eligible_lines"] == 6
    assert result["coverage"]["time_attribution"]["unknown_time_lines_for_project"] == 1


# ---------------------------------------------------------------------------
# Case 3: UTC partitioning, boundaries, unknown time, and zero exposure.
# ---------------------------------------------------------------------------


def test_timestamps_partition_by_utc_instant_with_explicit_unknowns(tmp_path):
    env = make_env(tmp_path)
    no_time = c_user("no time", ts(0), env.repo)
    del no_time["timestamp"]
    write_claude(env, [
        c_user("late august", "2026-08-31T23:59:59.999999Z", env.repo),
        c_user("offset august", "2026-09-01T01:30:00+02:00", env.repo),
        c_user("boundary", "2026-09-01T00:00:00Z", env.repo),
        c_user("september", "2026-09-15T08:00:00-07:00", env.repo),
        no_time,
        c_user("bad time", "yesterday", env.repo),
    ])
    scan(env, "run-1")
    august = exposure(env.store, start="2026-08-01T00:00:00Z", end="2026-09-01T00:00:00Z")
    september = exposure(env.store, start="2026-09-01T00:00:00Z", end="2026-10-01T00:00:00Z")
    assert (august["eligible_lines"], september["eligible_lines"]) == (2, 2)
    shifted = exposure(env.store, start="2026-09-01T02:00:00+02:00", end="2026-10-01T02:00:00+02:00")
    assert shifted["requested"]["start"] == "2026-09-01T00:00:00.000000Z"
    assert shifted["eligible_lines"] == 2
    assert august["coverage"]["time_attribution"]["unknown_time_lines_for_project"] == 2
    exclusions = {r["exclusion"]: r["n"] for r in env.store.query(
        "SELECT exclusion, COUNT(*) AS n FROM scan_lines WHERE exclusion <> '' GROUP BY exclusion")}
    assert exclusions == {"unknown_time": 1, "invalid_time": 1}

    empty = exposure(env.store, start="2025-01-01T00:00:00Z", end="2025-02-01T00:00:00Z")
    assert (empty["computable"], empty["reason"], empty["eligible_lines"], empty["rate_per_100k"]) == (
        False, "zero_eligible_exposure", 0, None)
    known_zero = exposure(env.store, start="2026-08-01T00:00:00Z", end="2026-09-01T00:00:00Z",
                          signal_types=("frustration",))
    assert (known_zero["computable"], known_zero["occurrences"], known_zero["rate_per_100k"]) == (True, 0, 0.0)


# ---------------------------------------------------------------------------
# Case 8: canonical projects, working copies, subagents, unknown identity.
# ---------------------------------------------------------------------------


def test_clones_merge_while_copies_subagents_and_unknowns_stay_distinct(tmp_path):
    env = make_env(tmp_path)
    clone = make_repo(tmp_path / "work" / "alpha-clone", "https://github.com/example/alpha")
    alias = tmp_path / "alias"
    alias.symlink_to(env.repo)
    write_claude(env, [c_user("a", ts(0), env.repo), c_user("b", ts(1), str(alias))])
    write_claude(env, [c_user("c", ts(2), clone, sid=SID_B)], name=SID_B, slug="-work-alpha-clone")
    subagents = env.claude_dir / "-work-alpha" / SID / "subagents"
    subagents.mkdir(parents=True)
    (subagents / "agent-1.jsonl").write_bytes(jsonl([c_user("d", ts(3), env.repo)]))
    unknown_session = c_user("e", ts(4), env.repo)
    del unknown_session["sessionId"]
    write_claude(env, [unknown_session, c_user("f", ts(5), str(tmp_path / "missing" / "dir"), sid=SID_C)],
                 name=SID_C, slug="-work-other")
    scan(env, "run-1")

    project = exposure(env.store)
    assert project["eligible_lines"] == 5
    assert project["sessions"] == {"logical_sessions": 2, "transcripts": 4, "unknown_session_lines": 1}
    assert project["session_size"] == {"total": 4, "median": 2.0, "max": 3}
    assert (project["workload"]["eligible_lines"]["subagent"], project["workload"]["eligible_lines"]["main"]) == (1, 4)
    assert project["coverage"]["time_attribution"]["unattributed_lines_in_scope_transcripts"] == {
        "unknown_project": 1}

    primary = so.working_copy_identity(PROJECT, env.repo)["id"]
    secondary = so.working_copy_identity(PROJECT, clone)["id"]
    assert primary != secondary
    assert exposure(env.store, working_copy_id=primary)["eligible_lines"] == 4, "symlink alias is one copy"
    assert exposure(env.store, working_copy_id=secondary)["eligible_lines"] == 1


# ---------------------------------------------------------------------------
# Case 9: rewritten, shrunk, replaced, malformed, truncated, denied, deleted.
# ---------------------------------------------------------------------------


def test_rewrites_shrinkage_and_same_size_replacement_never_mix_projections(tmp_path):
    env = make_env(tmp_path)
    path = write_claude(env, correction_session(env.repo))
    scan(env, "run-1")

    rewritten = correction_session(env.repo)
    rewritten[0] = c_user("please list every file now", ts(0), env.repo)
    path.write_bytes(jsonl(rewritten + [c_user("ok", ts(6), env.repo)]))
    scan(env, "run-2")
    assert active_lines(env.store) == 5
    # The longer first line shifts every later byte boundary, so lines 1-4 are
    # new revisions. Each position still has exactly one active unit.
    assert [r["line_no"] for r in env.store.query(
        "SELECT line_no FROM scan_lines WHERE active = 0 ORDER BY line_no")] == [1, 2, 3, 4]
    assert (exposure(env.store)["eligible_lines"], exposure(env.store)["occurrences"]) == (5, 1)

    path.write_bytes(jsonl(rewritten[:2]))
    scan(env, "run-3")
    assert active_lines(env.store) == 2
    assert (exposure(env.store)["eligible_lines"], exposure(env.store)["occurrences"]) == (2, 0)
    assert count(env.store, "SELECT id FROM scan_occurrences WHERE active = 1") == 0

    before = env.store.query_one("SELECT line_key FROM scan_lines WHERE active = 1 AND line_no = 1")
    replacement = jsonl([c_user("please list every file wow", ts(0), env.repo), c_tool(ts(1), env.repo)])
    assert len(replacement) == path.stat().st_size
    path.write_bytes(replacement)
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    scan(env, "run-4")
    after = env.store.query_one("SELECT line_key FROM scan_lines WHERE active = 1 AND line_no = 1")
    assert active_lines(env.store) == 2 and after != before


def test_malformed_truncated_and_denied_lines_are_counted_outside_rates(tmp_path):
    env = make_env(tmp_path)
    denied_cwd = str(tmp_path / "work" / "denied-tree")
    write_codex(env, [x_meta(ts(0), env.repo), x_call(ts(1), "c1"), x_meta(ts(2), denied_cwd),
                      x_call(ts(3), "c2"), x_user("no, that's wrong", ts(4))])
    path = write_claude(env, correction_session(env.repo))
    tail = b'{"type":"user","timestamp":"2026-08-10T10:09:00Z"'
    append(path, b"this is not json\n" + tail)
    scan(env, "run-1")

    result = exposure(env.store)
    assert (result["eligible_lines"], result["occurrences"]) == (6, 1)
    coverage = result["coverage"]
    assert coverage["time_attribution"]["unattributed_lines_in_scope_transcripts"] == {
        "denied": 3, "malformed": 1}
    assert coverage["coverage_complete"] is False
    assert coverage["counts_by_cause"]["pending_tail_bytes"] == len(tail)
    assert env.store.query(
        "SELECT DISTINCT project_key, logical_session_key, occurred_at, working_copy_id "
        "FROM scan_lines WHERE exclusion = 'denied'"
    ) == [{"project_key": "", "logical_session_key": "", "occurred_at": "", "working_copy_id": ""}]

    rest = (',"cwd":"%s","sessionId":"%s","entrypoint":"cli",'
            '"message":{"role":"user","content":"done"}}\n' % (env.repo, SID)).encode()
    append(path, rest)
    scan(env, "run-2")
    completed = exposure(env.store)
    assert completed["eligible_lines"] == 7
    assert completed["coverage"]["coverage_complete"] is False
    assert completed["coverage"]["counts_by_cause"]["pending_tail_bytes"] == 0
    assert completed["coverage"]["counts_by_cause"]["unattributed:malformed"] == 1
    assert completed["coverage"]["counts_by_cause"]["unattributed:denied"] == 3


def test_deleted_transcripts_keep_their_evidence_rather_than_reading_as_zero(tmp_path):
    env = make_env(tmp_path)
    path = write_claude(env, correction_session(env.repo))
    scan(env, "run-1")
    before = exposure(env.store)
    path.unlink()
    assert scan(env, "run-2").files_attempted == 0
    after = exposure(env.store)
    assert after == before and after["occurrences"] == 1
    assert after["coverage"]["retention"]["complete_history_known"] is False
    assert so.scan_history(env.store, session_file=str(path))["computable"] is True


# ---------------------------------------------------------------------------
# Largest synthetic fixture: volume for the handoff.
# ---------------------------------------------------------------------------

LARGE_LINES, APPENDED_LINES = 3000, 1000


def test_large_append_reconciles_linearly_without_duplicate_rows(tmp_path):
    env = make_env(tmp_path)
    start = datetime(2026, 8, 10, tzinfo=timezone.utc)

    def lines(first: int, n: int) -> bytes:
        return jsonl([
            c_user(f"line {i}", (start + timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%SZ"), env.repo)
            for i in range(first, first + n)])

    path = write_claude(env, [])
    path.write_bytes(lines(0, LARGE_LINES))
    scan(env, "run-1")
    append(path, lines(LARGE_LINES, APPENDED_LINES))
    stats = scan(env, "run-2")
    assert stats.measurement["full_reparse_bytes"] == path.stat().st_size
    assert count(env.store, "SELECT id FROM scan_lines") == LARGE_LINES + APPENDED_LINES
    assert active_lines(env.store) == LARGE_LINES + APPENDED_LINES
    assert latest_record(env.store, path)["reconciliation"]["lines"] == {
        "inserted": APPENDED_LINES, "superseded": 0, "unchanged": LARGE_LINES, "removed": 0}
