"""Tests for the dashboard's read-only data layer.
Temporary records exercise rebuild timestamps, repeated run timestamps,
opaque detector fingerprints, and multiple working copies of one repository.
The personal-resource audit hook guards every test. Counts and paths here
are regression inputs; they do not report operational measurements.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from datetime import datetime, timedelta, timezone

import pytest

from self_improve.config import Config
from self_improve.store import Store, new_id
from self_improve.dashboard import queries as q


NOW = "2026-08-22T20:00:00Z"
_NOW_DT = datetime(2026, 8, 22, 20, 0, tzinfo=timezone.utc)


class _Cfg:
    """Minimal stand-in for the parts of Config review_queue reads."""

    # Mirrors the real Config, and a test below asserts it stays complete.
    # `_cfg_attr` raises on a missing key on purpose, so a stand-in that omits
    # one lets the suite pass while the product raises on first open.
    review_queue_actions = ("convert_to_hook", "delete_human_line")
    # The review card draws a line-budget bar for the global instruction
    # file. This path deliberately does NOT exist: `_line_budget` must
    # REPORT an unreadable target rather than default it to 0 used, which
    # would draw "plenty of room" over a file nobody could read.
    global_claude_md = "/nonexistent/global_CLAUDE.md"
    global_claude_md_line_budget = 250
    global_promotion_min_projects = 3
    mine_order = "signal_then_recent"
    max_cheap_calls_per_run = 80
    project_branch_name = "self-improve/rules"


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _writable(tmp_path, name="state.db") -> tuple[Store, object]:
    path = tmp_path / name
    return Store(path), path


def _read_only(path) -> Store:
    return Store(path, read_only=True)


def _run(store, *, run_id=None, started, finished="", status="ok", stats=None):
    store.insert(
        "runs",
        {
            "id": run_id or new_id(),
            "started": started,
            "finished": finished,
            "status": status,
            "stats_json": json.dumps(stats if stats is not None else {}),
            "report_path": "",
        },
    )


def _session(store, *, file_path, project_key, project_display="", project_path="",
             source="claude", first_ts="2026-08-10T00:00:00Z", lines=1000,
             project_key_method="gh_repo_id"):
    store.upsert_session(
        {
            "file_path": file_path,
            "source": source,
            "session_id": file_path,
            "project_path": project_path or f"/repos/{project_key}",
            "project_key": project_key,
            "project_display": project_display,
            "project_key_method": project_key_method,
            "headless": 0,
            "is_subagent": 0,
            "first_ts": first_ts,
            "last_ts": first_ts,
            "mtime": 0.0,
            "file_size": 0,
            "bytes_scanned": 0,
            "lines_scanned": lines,
            "malformed_lines": 0,
            "status": "ok",
            "error": "",
            "last_scanned_at": first_ts,
        }
    )


def _incident(store, *, incident_id=None, session_file, ts, created_at,
              signal_type="frustration", matched_text="something broke",
              window=None, project_key="github:1", status="new"):
    iid = incident_id or new_id()
    store.insert(
        "incidents",
        {
            "id": iid,
            "session_file": session_file,
            "session_id": session_file,
            "project_path": "/repos/x",
            "project_key": project_key,
            "ts": ts,
            "signal_type": signal_type,
            "matched_text": matched_text,
            "window_json": json.dumps(
                window if window is not None else [{"role": "user", "ts": ts, "text": "hi"}]
            ),
            "score": 1.0,
            "status": status,
            "run_id": "",
            "created_at": created_at,
        },
    )
    return iid


def _learning(store, *, learning_id=None, rule_text="do the thing", status="proposed",
              scope="global", source="claude", evidence_count=1, duplicate_of="",
              violated_existing_rule="", created_at="2026-08-18T07:00:00Z",
              primary_project_path="", project_count=1):
    lid = learning_id or new_id()
    store.insert(
        "learnings",
        {
            "id": lid,
            "title": "t",
            "rule_text": rule_text,
            "why": "because",
            "category": "c",
            "scope": scope,
            "evidence_count": evidence_count,
            "project_count": project_count,
            "projects_json": "[]",
            "first_seen": created_at,
            "last_seen": created_at,
            "confidence": 0.6,
            "status": status,
            "duplicate_of": duplicate_of,
            "created_at": created_at,
            "incident_summary": "",
            "source": source,
            "violated_existing_rule": violated_existing_rule,
            "path_globs_json": "[]",
            "primary_project_path": primary_project_path,
        },
    )
    return lid


def _proposal(store, *, proposal_id=None, learning_id, status="pending",
              target_path="/Users/x/.claude/CLAUDE.md", eval_result_id="",
              target_kind="global_claude_md", created_at="2026-08-18T08:00:00Z",
              diff_unified="", action="add"):
    pid = proposal_id or new_id()
    store.insert(
        "proposals",
        {
            "id": pid,
            "learning_id": learning_id,
            "run_id": "",
            "target_path": target_path,
            "target_kind": target_kind,
            "action": action,
            "diff_unified": diff_unified,
            "status": status,
            "eval_result_id": eval_result_id,
            "applied_at": "",
            "snapshot_commit_before": "",
            "snapshot_commit_after": "",
            "created_at": created_at,
        },
    )
    return pid


def _eval(store, *, eval_id=None, subject_id, verdict, attempted=3, succeeded=3,
          failed=0, taxonomy=None, metrics=None, started="2026-08-18T08:00:00Z"):
    eid = eval_id or new_id()
    store.insert(
        "eval_results",
        {
            "id": eid,
            "kind": "regression",
            "subject_id": subject_id,
            "started": started,
            "finished": started,
            "attempted": attempted,
            "succeeded": succeeded,
            "failed": failed,
            "error_taxonomy_json": json.dumps(taxonomy or {}),
            "metrics_json": json.dumps(
                metrics if metrics is not None else {"without": {"attempted": 3}, "with": None}
            ),
            "verdict": verdict,
        },
    )
    return eid


def _llm_call(store, *, stage, outcome, run_id="", n=1):
    for _ in range(n):
        store.insert(
            "llm_calls",
            {
                "id": new_id(),
                "run_id": run_id,
                "stage": stage,
                "provider": "claude",
                "account": "a",
                "model_requested": "m",
                "model_reported": "m",
                "prompt_sha": "",
                "tokens_in": 0,
                "tokens_out": 0,
                "duration_ms": 1,
                "outcome": outcome,
                "error": "",
                "created_at": "2026-08-18T08:00:00Z",
            },
        )


def _all_keys(payload):
    """Every dict key anywhere in a JSON-shaped payload."""
    if isinstance(payload, dict):
        for key, value in payload.items():
            yield key
            yield from _all_keys(value)
    elif isinstance(payload, list):
        for item in payload:
            yield from _all_keys(item)


def _all_strings(payload):
    if isinstance(payload, dict):
        for value in payload.values():
            yield from _all_strings(value)
    elif isinstance(payload, list):
        for item in payload:
            yield from _all_strings(item)
    elif isinstance(payload, str):
        yield payload


#: Invented stats retain a legacy budget-refusal entry for compatibility.
_FULL_STATS = {
    "run_id": "r",
    "review_only": True,
    "scan": {"files_attempted": 10, "files_succeeded": 10, "files_failed": 0},
    "mine": {"attempted": 7, "succeeded": 6, "failed": 1,
             "taxonomy": {"MineParseFailure": 1, "budget_exhausted": 13}},
    "cluster": {"candidates": 3, "mode": "agentic_passthrough"},
    "gate": {"attempted": 3, "gated_pass": 0, "gated_fail": 0, "ungated": 3,
             "inconclusive": 0, "failed": 0},
    "apply": {"attempted": 3, "applied": 0, "held": 3, "failed": 0, "taxonomy": {}},
}


# ---------------------------------------------------------------------------
# The store contract: this module must never construct one
# ---------------------------------------------------------------------------


def test_queries_never_constructs_a_store(tmp_path, monkeypatch):
    """Inspect every Store construction so these read-only queries cannot migrate."""
    store, path = _writable(tmp_path)
    _run(store, started="2026-08-18T00:00:00Z", finished="2026-08-18T01:00:00Z", stats=_FULL_STATS)
    _session(store, file_path="s1", project_key="github:1", project_display="repo/one")
    _incident(store, session_file="s1", ts="2026-08-18T00:00:00Z", created_at="2026-08-18T00:00:00Z")
    store.close()

    ro = _read_only(path)
    constructions = []
    original = Store.__init__

    def _recording_init(self, db_path, *, read_only=False):
        constructions.append((str(db_path), read_only))
        original(self, db_path, read_only=read_only)

    monkeypatch.setattr(Store, "__init__", _recording_init)

    cfg = Config()
    q.overview(ro, cfg, now_utc=NOW)
    q.rules(ro)
    q.projects(ro, weigh_top_n=0, isdir=lambda p: False)
    q.incident_rate(ro, now_utc=_NOW_DT)

    assert constructions == [], (
        f"queries constructed {len(constructions)} Store(s): {constructions}. "
        "The dashboard process owns exactly one read-only Store."
    )


def test_queries_contains_no_store_construction_at_all():
    """The runtime recorder above cannot see a Store built at IMPORT time.

    That is the exact shape the RUNBOOK records: the writable handle already
    existed further up before the read-only one was created. This reads the
    source instead, so a module-level ``_store = Store(...)`` fails too.
    """
    import ast

    tree = _module_ast()
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                called.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                called.add(node.func.attr)
    assert "Store" not in called, "queries.py constructs a Store"

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and "store" in node.module:
            assert "Store" not in {a.name for a in node.names}, "queries.py imports Store"
        if isinstance(node, ast.Import):
            assert not any(a.name.endswith("store") for a in node.names)


def test_overview_leaves_the_database_byte_identical(tmp_path):
    store, path = _writable(tmp_path)
    _run(store, started="2026-08-18T00:00:00Z", finished="2026-08-18T01:00:00Z", stats=_FULL_STATS)
    _session(store, file_path="s1", project_key="github:1", project_display="repo/one")
    _incident(store, session_file="s1", ts="2026-08-18T00:00:00Z", created_at="2026-08-18T00:00:00Z")
    store.close()

    before = hashlib.sha256(path.read_bytes()).hexdigest()
    ro = _read_only(path)
    q.overview(ro, Config(), now_utc=NOW)
    q.rules(ro)
    q.projects(ro, weigh_top_n=0, isdir=lambda p: False)
    ro.close()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


class _BlockDashboardExtra:
    """A meta-path finder that pretends the dashboard extra is not installed."""

    BLOCKED = {"fastapi", "uvicorn", "starlette"}

    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in self.BLOCKED:
            raise ImportError(f"{name} is not installed (blocked by the test)")
        return None


def test_module_imports_with_the_dashboard_extra_uninstalled():
    """The data layer must import and work with no FastAPI present.

    In a SUBPROCESS, not in this one. Until 2026-09-12 this deleted
    `self_improve.dashboard.queries` from `sys.modules` and re-imported it
    in-process. `importlib.import_module` rebinds the attribute on the parent
    package, `monkeypatch.delitem` restores only the `sys.modules` entry, and
    `monkeypatch.setattr("a.b.c", ...)` resolves through the ATTRIBUTE. So
    every later test that patched a `queries` symbol by string patched a
    different module object than `app.py` was holding, and
    `test_the_conditional_write_is_reachable_and_does_not_invent_a_racer`
    failed whenever this file ran first while passing on its own.

    AGENTS.md records this exact class -- "a guard that fakes an environment
    must prove the fake is working, and must not do it in-process" -- from a
    2026-09-04 test that broke three neighbours the same way. The prescribed
    shape is a subprocess plus an anti-vacuity assertion, because an inert
    blocker and a satisfied invariant look identical.
    """
    import subprocess
    import sys
    import textwrap

    probe = textwrap.dedent(
        """
        import sys

        BLOCKED = {"fastapi", "uvicorn", "starlette"}

        class Block:
            def find_spec(self, name, path=None, target=None):
                if name.split(".")[0] in BLOCKED:
                    raise ImportError(name + " is blocked by the test")
                return None

        sys.meta_path.insert(0, Block())

        # Anti-vacuity: the blocker must actually block. `find_module` was
        # REMOVED in Python 3.12, so a blocker written against it is silently
        # inert and the probe reports success having blocked nothing.
        try:
            import fastapi
        except ImportError:
            pass
        else:
            raise SystemExit("BLOCKER-INERT: fastapi imported anyway")

        from self_improve.dashboard import queries
        assert queries.NOT_COMPUTABLE == "\u2014", queries.NOT_COMPUTABLE
        assert queries.STAGES == ("scan", "mine", "cluster", "gate", "apply"), queries.STAGES
        print("OK")
        """
    )
    done = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parent.parent),
    )
    assert done.returncode == 0, (
        f"the data layer does not import without the dashboard extra:\n"
        f"stdout={done.stdout}\nstderr={done.stderr}"
    )
    assert "OK" in done.stdout, done.stdout


def _module_ast():
    import ast
    import importlib

    source = importlib.import_module("self_improve.dashboard.queries").__file__
    with open(source, encoding="utf-8") as handle:
        return ast.parse(handle.read())


def test_the_data_layer_imports_no_web_framework():
    import ast

    imported = set()
    for node in ast.walk(_module_ast()):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    assert imported.isdisjoint({"fastapi", "uvicorn", "starlette", "pydantic"}), imported
    assert "self_improve.apply" not in imported, "P0 has no write path"


def test_no_sqlite_only_sql():
    """Supabase-portable (store.py): no json_each, no group_concat, no SUM(bool)."""
    import ast

    statements = [
        node.value
        for node in ast.walk(_module_ast())
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and "SELECT " in node.value
    ]
    assert statements, "no SQL found — the SQL guard is not looking at anything"
    for sql in statements:
        for banned in ("json_each", "group_concat", "strftime(", "AUTOINCREMENT", "OR REPLACE"):
            assert banned not in sql, f"{banned} is SQLite-only: {sql}"
        assert "SUM(status" not in sql, "SUM over a boolean is SQLite-only"


# ---------------------------------------------------------------------------
# Logical time
# ---------------------------------------------------------------------------


def test_now_utc_must_be_explicit_and_utc(tmp_path):
    store, path = _writable(tmp_path)
    store.close()
    ro = _read_only(path)
    with pytest.raises(q.DashboardDataError, match="timezone-aware"):
        q.data_freshness(ro, now_utc=datetime(2026, 8, 22, 20, 0, 0))
    with pytest.raises(q.DashboardDataError, match="not UTC"):
        q.data_freshness(ro, now_utc="2026-08-22T20:00:00-07:00")
    assert q.data_freshness(ro, now_utc="2026-08-22")["reference_day"] == "2026-08-22"
    aware = datetime(2026, 8, 22, 23, 30, tzinfo=timezone(timedelta(hours=-7)))
    # 23:30 on the 22nd in UTC-7 is 06:30 on the 23rd in UTC.
    assert q.data_freshness(ro, now_utc=aware)["reference_day"] == "2026-08-23"


def test_freshness_dates_from_incident_ts_not_created_at(tmp_path):
    store, path = _writable(tmp_path)
    _session(store, file_path="s1", project_key="github:1")
    _incident(store, session_file="s1", ts="2026-08-18T09:00:00Z", created_at="2026-08-21T00:00:00Z")
    store.close()
    fresh = q.data_freshness(_read_only(path), now_utc=NOW)
    assert fresh["as_of"] == "2026-08-18"
    assert fresh["days_stale"] == 4
    assert "2026-08-18" in fresh["banner"] and "4 days stale" in fresh["banner"]


# ---------------------------------------------------------------------------
# V1 — the run x stage grid
# ---------------------------------------------------------------------------


def test_run_grid_column_aggregates_many_runs_per_night(tmp_path):
    """Aggregate every run even when several share an identical start timestamp.
    A timestamp-keyed dictionary must not collapse the invented rows.
    """
    store, path = _writable(tmp_path)
    for index in range(5):
        _run(store, run_id=f"ok{index}", started="2026-08-18T00:00:00Z",
             finished="2026-08-18T01:00:00Z", status="ok", stats=_FULL_STATS)
    _run(store, run_id="stuck", started="2026-08-18T00:00:00Z", finished="", status="running")
    store.close()

    grid = q.run_stage_grid(_read_only(path), now_utc=NOW)
    assert grid["nights"] == ["2026-08-18"]
    column = grid["columns"][0]
    assert column["run_count"] == 6, "runs sharing one `started` were dropped by key collision"
    assert sorted(column["run_ids"]) == sorted([f"ok{i}" for i in range(5)] + ["stuck"])
    assert column["run_states"] == {"ok": 5, "running": 1}
    assert len(column["cells"]) == len(q.STAGES)
    assert column["cells"]["mine"]["runs"] == 6
    assert column["cells"]["mine"]["number"] == 30  # 5 runs x 6 mined
    assert column["worst"] == "partial"  # mine is 6/7 on every run


def test_run_grid_renders_running_and_abandoned_as_their_own_states(tmp_path):
    """Preserve running and abandoned states even when a finished timestamp exists.

    A completion timestamp alone does not establish that a run succeeded.
    """
    store, path = _writable(tmp_path)
    _run(store, run_id="a", started="2026-08-10T00:00:00Z",
         finished="2026-08-10T01:00:00Z", status="ok", stats=_FULL_STATS)
    _run(store, run_id="b", started="2026-08-11T00:00:00Z", finished="", status="running")
    _run(store, run_id="c", started="2026-08-12T00:00:00Z",
         finished="2026-08-18T00:00:00Z", status="abandoned")
    _run(store, run_id="d", started="2026-08-13T00:00:00Z",
         finished="2026-08-13T01:00:00Z", status="error")
    _run(store, run_id="e", started="2026-08-14T00:00:00Z",
         finished="2026-08-14T01:00:00Z", status="budget_exhausted")
    store.close()

    grid = q.run_stage_grid(_read_only(path), now_utc=NOW)
    states = {c["night"]: c["cells"]["mine"]["state"] for c in grid["columns"]}
    assert states == {
        "2026-08-10": "partial",
        "2026-08-11": "running",
        "2026-08-12": "abandoned",
        "2026-08-13": "error",
        "2026-08-14": "budget_exhausted",
    }
    assert len(set(states.values())) == 5, "the five run outcomes collapsed into fewer states"
    for column in grid["columns"]:
        run_status = column["cells"]["mine"]["per_run"][0]["run_status"]
        if run_status != "ok":
            assert column["cells"]["mine"]["state"] != "ok", (
                f"run status {run_status} rendered as a successful cell"
            )


def test_run_grid_absent_stage_key_is_skipped_not_failed(tmp_path):
    """A stage a completed run never reached is absence, not zero and not failure."""
    store, path = _writable(tmp_path)
    _run(store, run_id="scanonly", started="2026-08-15T00:00:00Z",
         finished="2026-08-15T01:00:00Z", status="ok",
         stats={"scan": {"files_attempted": 3, "files_succeeded": 3, "files_failed": 0}})
    store.close()

    cells = q.run_stage_grid(_read_only(path), now_utc=NOW)["columns"][0]["cells"]
    assert cells["scan"]["state"] == "ok"
    for stage in ("mine", "cluster", "gate", "apply"):
        assert cells[stage]["state"] == "skipped", stage
        assert cells[stage]["number"] is None, f"{stage} invented a number for a stage that never ran"


def test_run_grid_an_unknown_stage_shape_is_unreadable_never_ok(tmp_path):
    """A pipeline change that renames a stats key must be visible, not silent."""
    store, path = _writable(tmp_path)
    _run(store, run_id="new", started="2026-08-15T00:00:00Z",
         finished="2026-08-15T01:00:00Z", status="ok",
         stats={"mine": {"tried": 5, "worked": 4}})
    store.close()

    grid = q.run_stage_grid(_read_only(path), now_utc=NOW)
    assert grid["columns"][0]["cells"]["mine"]["state"] == "unreadable"
    assert grid["unreadable"] == [
        {"run_id": "new", "night": "2026-08-15", "stage": "mine", "keys": ["tried", "worked"]}
    ]


def test_run_grid_gate_with_no_verdicts_is_refused_not_failed(tmp_path):
    """AGENTS.md: a run with no eval verdicts is not a gate that failed."""
    store, path = _writable(tmp_path)
    _run(store, run_id="budget", started="2026-08-15T00:00:00Z",
         finished="2026-08-15T01:00:00Z", status="ok",
         stats={"gate": {"attempted": 32, "gated_pass": 0, "gated_fail": 0, "ungated": 0},
                "apply": {"attempted": 0, "applied": 0, "held": 32, "failed": 0,
                          "taxonomy": {"gate_budget_exhausted": 32}}})
    store.close()

    cells = q.run_stage_grid(_read_only(path), now_utc=NOW)["columns"][0]["cells"]
    assert cells["gate"]["state"] == "refused"
    # held is the correct outcome under --review-only, so apply did not fail.
    assert cells["apply"]["state"] == "ok"
    assert cells["apply"]["per_run"][0]["attempted_adjusted"]["used"] == 32


def test_run_grid_some_verdicts_and_budget_refusals_are_not_failures(tmp_path):
    store, path = _writable(tmp_path)
    _run(store, run_id="mixed-budget", started="2026-08-15T00:00:00Z",
         finished="2026-08-15T01:00:00Z", status="ok",
         stats={"gate": {"attempted": 3, "gated_pass": 1, "failed": 0, "refused": 2}})
    store.close()
    cell = q.run_stage_grid(_read_only(path), now_utc=NOW)["columns"][0]["cells"]["gate"]
    assert cell["state"] == "budget_exhausted"
    assert cell["per_run"][0]["refused"] == 2
    assert cell["per_run"][0]["unaccounted"] == 0


def test_run_grid_window_reports_what_it_cut(tmp_path):
    store, path = _writable(tmp_path)
    _run(store, run_id="old", started="2026-07-01T00:00:00Z",
         finished="2026-07-01T01:00:00Z", stats=_FULL_STATS)
    _run(store, run_id="new", started="2026-08-20T00:00:00Z",
         finished="2026-08-20T01:00:00Z", stats=_FULL_STATS)
    store.close()

    grid = q.run_stage_grid(_read_only(path), now_utc=NOW, window_days=7)
    assert len(grid["nights"]) == 7
    assert grid["window"]["nights_dropped"] == 1
    assert grid["window"]["runs_dropped"] == 1
    assert grid["window"]["ends_at"] == "2026-08-22"


# ---------------------------------------------------------------------------
# V1 — status line, inbox
# ---------------------------------------------------------------------------


def test_status_line_counts_nights_against_the_callers_clock(tmp_path):
    store, path = _writable(tmp_path)
    _run(store, started="2026-08-16T09:00:00Z", finished="2026-08-16T10:00:00Z")
    _run(store, started="2026-08-18T07:00:00Z", finished="2026-08-18T08:00:00Z")
    _run(store, started="2026-08-18T09:00:00Z", finished="2026-08-18T10:00:00Z")
    _run(store, started="2026-07-01T00:00:00Z", finished="2026-07-01T01:00:00Z")
    _learning(store)
    _learning(store)
    lid = _learning(store)
    _proposal(store, learning_id=lid, status="pending")
    _proposal(store, learning_id=lid, status="ungated")
    store.close()

    line = q.status_line(_read_only(path), _Cfg(), now_utc=NOW)
    assert line["nights_ran"] == 2, line["nights_ran_days"]
    assert line["window_start"] == "2026-08-16" and line["window_end"] == "2026-08-22"
    assert line["text"] == (
        "Ran 2 of the last 7 nights. 3 rules learned, 0 applied, 2 waiting on you."
    )


def test_inbox_includes_ungated_proposals_requiring_review(tmp_path):
    """Ungated proposals require a visible human decision regardless of run mode."""
    store, path = _writable(tmp_path)
    lid = _learning(store)
    _proposal(store, learning_id=lid, status="pending")
    _proposal(store, learning_id=lid, status="ungated")
    _proposal(store, learning_id=lid, status="ungated")
    _proposal(store, learning_id=lid, status="ungated")
    _run(store, started="2026-08-18T09:00:00Z", finished="2026-08-18T10:00:00Z",
         stats={"review_only": True})
    store.close()

    box = q.inbox(_read_only(path), _Cfg())
    assert box["count"] == 4
    assert box["line"] == "4 items waiting on you"
    assert box["second_line"] == ""
    assert box["review_only"] is True


def test_inbox_counts_the_fourth_verdict_as_held(tmp_path):
    """Inconclusive, failed, and disabled passing proposals all need a decision."""
    store, path = _writable(tmp_path)
    lid = _learning(store)
    _proposal(store, learning_id=lid, status="inconclusive")
    _proposal(store, learning_id=lid, status="gated_fail")
    _proposal(store, learning_id=lid, status="gated_pass")
    _run(store, started="2026-08-18T09:00:00Z", finished="2026-08-18T10:00:00Z",
         stats={"review_only": False})
    store.close()

    box = q.inbox(_read_only(path), _Cfg())
    assert box["count"] == 3, box["by_status"]
    assert "inconclusive" in box["queueing_statuses"]
    assert box["auto_apply_pending"] == 0
    assert box["second_line"] == ""


def test_inbox_names_an_unknown_proposal_status(tmp_path):
    store, path = _writable(tmp_path)
    lid = _learning(store)
    _proposal(store, learning_id=lid, status="quantum")
    store.close()
    box = q.inbox(_read_only(path), _Cfg())
    assert box["unknown_statuses"] == {"quantum": 1}


# ---------------------------------------------------------------------------
# V1 — backlog: two rates, never a countdown
# ---------------------------------------------------------------------------


def _spread_incidents(store, *, n, days, first_day="2026-06-20", rescan_day="2026-08-18"):
    """n incidents spread over `days` days of transcript time, all scanned once."""
    _session(store, file_path="s1", project_key="github:1")
    start = datetime.fromisoformat(first_day).date()
    for index in range(n):
        day = (start + timedelta(days=index % days)).isoformat()
        _incident(
            store,
            session_file="s1",
            ts=f"{day}T12:00:00Z",
            created_at=f"{rescan_day}T02:00:00Z",
        )


def test_arrival_rate_uses_incident_ts_not_created_at(tmp_path):
    """A rebuild's insertion date must not replace the incident's event date."""
    store, path = _writable(tmp_path)
    _spread_incidents(store, n=5000, days=60, first_day="2026-06-20", rescan_day="2026-08-18")
    store.close()

    plan = q.backlog(_read_only(path), Config())
    per_day = plan["arrivals"]["per_day"]
    expected = 5000 / 60
    assert per_day == pytest.approx(expected, rel=0.10), (
        f"{per_day}/day is not the ~{expected:.1f}/day the transcript timestamps show"
    )
    assert per_day != 5000
    assert plan["arrivals"]["source_column"] == "incidents.ts"


def test_backlog_emits_two_rates_and_never_a_countdown(tmp_path):
    store, path = _writable(tmp_path)
    _spread_incidents(store, n=2800, days=14, first_day="2026-08-05", rescan_day="2026-08-18")
    store.close()

    plan = q.backlog(_read_only(path), Config())
    assert plan["queued"] == 2800
    assert plan["mine_capacity_per_run"] == 80
    assert plan["arrivals"]["per_day"] == pytest.approx(200.0)
    assert plan["net_per_day"] == pytest.approx(120.0)
    assert plan["net_per_day"] > 0, "the queue grows; a non-positive net hides that"

    # No key anywhere in the payload may be countdown-shaped, and no string
    # value may carry a countdown number. PRD 7/V1: the queue grows, so any
    # "N nights to drain" figure is fiction that reads as reassurance.
    for key in _all_keys(plan):
        for forbidden in ("drain", "nights_to", "days_to", "empty_by", "eta", "countdown"):
            assert forbidden not in key.lower(), f"backlog emitted the field {key!r}"
    for value in _all_strings(plan):
        for forbidden in ("nights to drain", "days to drain", "will drain", "drains in"):
            assert forbidden not in value.lower(), f"backlog copy promises a drain: {value!r}"


def test_backlog_window_ends_at_the_last_data_day(tmp_path):
    """Four zero days caused by a paused loop are not an absence of incidents."""
    store, path = _writable(tmp_path)
    _spread_incidents(store, n=140, days=14, first_day="2026-08-05", rescan_day="2026-08-18")
    store.close()

    plan = q.backlog(_read_only(path), Config())
    assert plan["arrivals"]["window_end"] == "2026-08-18"
    assert plan["arrivals"]["window_start"] == "2026-08-05"
    assert plan["arrivals"]["days_with_zero"] == 0
    assert plan["arrivals"]["per_day"] == pytest.approx(10.0)


def test_backlog_raises_on_a_missing_config_key(tmp_path):
    store, path = _writable(tmp_path)
    store.close()

    class Bare:
        mine_order = "signal_then_recent"

    with pytest.raises(q.DashboardDataError, match="max_cheap_calls_per_run"):
        q.backlog(_read_only(path), Bare())


# ---------------------------------------------------------------------------
# V1 — failure panel (D3)
# ---------------------------------------------------------------------------


def test_failure_panel_splits_fixed_from_open_and_never_counts_the_cap_as_failure(tmp_path):
    store, path = _writable(tmp_path)
    _run(store, run_id="before", started="2026-08-15T06:00:00Z",
         finished="2026-08-15T06:20:00Z", status="ok",
         stats={"mine": {"attempted": 12, "succeeded": 5, "failed": 7,
                         "taxonomy": {"MineParseFailure": 3, "IntegrityError": 2,
                                      "MineContractViolation": 2,
                                      "budget_exhausted": 23}}})
    _run(store, run_id="after", started="2026-08-19T08:00:00Z",
         finished="2026-08-19T08:20:00Z", status="ok",
         stats={"mine": {"attempted": 4, "succeeded": 3, "failed": 1,
                         "taxonomy": {"MineParseFailure": 1, "budget_exhausted": 41}}})
    store.close()

    panel = q.failure_panel(_read_only(path))
    fixed = {row["class"]: row for row in panel["fixed"]}
    assert "IntegrityError" in fixed
    assert fixed["IntegrityError"]["status"] == "fixed"
    assert fixed["IntegrityError"]["fixed_at"] == "2026-08-16T10:56:04Z"
    assert panel["regressed"] == []

    open_classes = {row["class"]: row for row in panel["open"]}
    assert open_classes["MineParseFailure"]["name"] == "No answer we could read"
    assert open_classes["MineParseFailure"]["total"] == 4

    caps = {row["class"]: row["count"] for row in panel["not_failures"]}
    assert caps["budget_exhausted"] == 23 + 41
    all_failure_classes = {
        row["class"]
        for group in ("open", "quiet", "fixed", "regressed")
        for row in panel[group]
    }
    assert "budget_exhausted" not in all_failure_classes, (
        "unattempted incidents were counted as failures"
    )


def test_failure_panel_reports_a_regression_after_the_fix(tmp_path):
    """A 'fixed' class that reappears is loud, never re-hidden by the registry."""
    store, path = _writable(tmp_path)
    _run(store, run_id="later", started="2026-08-20T00:00:00Z",
         finished="2026-08-20T01:00:00Z", status="ok",
         stats={"mine": {"attempted": 2, "succeeded": 1, "failed": 1,
                         "taxonomy": {"IntegrityError": 1}}})
    store.close()

    panel = q.failure_panel(_read_only(path))
    assert panel["fixed"] == []
    assert [row["class"] for row in panel["regressed"]] == ["IntegrityError"]
    assert panel["regressed"][0]["occurrences_after_fix"][0]["run_id"] == "later"


def test_failure_panel_names_a_class_it_has_no_copy_for(tmp_path):
    store, path = _writable(tmp_path)
    _run(store, run_id="r", started="2026-08-20T00:00:00Z",
         finished="2026-08-20T01:00:00Z", status="ok",
         stats={"mine": {"attempted": 1, "succeeded": 0, "failed": 1,
                         "taxonomy": {"BrandNewExplosion": 3}}})
    store.close()

    panel = q.failure_panel(_read_only(path))
    assert panel["unknown_classes"] == [{"class": "BrandNewExplosion", "count": 3}]


def test_a_call_that_never_ran_is_not_labelled_unknown(tmp_path):
    """`call_failed:spawn_error` names a family and the outcome inside it.

    An exact-match lookup files the whole family under "unknown" with a blank
    explanation, so the operator reads a number with no words beside it. The
    family carries the copy; the suffix is the diagnosis.
    """
    store, path = _writable(tmp_path)
    _run(store, run_id="r", started="2026-08-23T02:30:00Z",
         finished="2026-08-23T02:31:00Z", status="ok",
         stats={"mine": {"attempted": 5, "succeeded": 0, "failed": 5,
                         "taxonomy": {"call_failed:spawn_error": 5}}})
    store.close()

    panel = q.failure_panel(_read_only(path))
    assert panel["unknown_classes"] == []
    row = next(r for r in panel["open"] + panel["quiet"]
               if r["class"] == "call_failed:spawn_error")
    assert row["has_copy"] is True
    assert "never produced an answer" in row["name"]


def test_the_prose_label_is_not_used_for_a_call_that_produced_no_answer(tmp_path):
    """Only an answered call can be described as an unparseable reply.

    A spawn failure produced no answer and needs a different explanation."""
    spawn = q.failure_copy("call_failed:spawn_error")
    assert "prose" not in spawn["explanation"]
    assert "prose" not in q.FAILURE_COPY["MineParseFailure"]["explanation"]
    # ...and the parse class still says an answer arrived, because it did.
    assert "answered" in q.FAILURE_COPY["MineParseFailure"]["explanation"]


def test_failure_panel_counts_parse_recovered_as_success(tmp_path):
    """parse_recovered delivered valid contract JSON; counting it as a failure
    understates every stage. Same rule as report.py."""
    store, path = _writable(tmp_path)
    _llm_call(store, stage="mine_agentic", outcome="ok", n=90)
    _llm_call(store, stage="mine_agentic", outcome="parse_recovered", n=8)
    _llm_call(store, stage="mine_agentic", outcome="parse_error", n=17)
    store.close()

    panel = q.failure_panel(_read_only(path))
    stage = panel["llm_by_stage"]["mine_agentic"]
    assert stage["attempted"] == 115
    assert stage["succeeded"] == 98
    assert stage["failed"] == 17
    assert "parse_recovered" not in {row["class"] for row in panel["open"] + panel["quiet"]}


# ---------------------------------------------------------------------------
# Gate health
# ---------------------------------------------------------------------------


def _gate_fixture(store):
    """Invented evals mix linked proposals, unlinked learnings, and seed subjects.

    Keep missing trial evidence, graded outcomes, and harness errors distinct.
    """
    lids = [_learning(store) for _ in range(3)]
    eids = []
    for lid in lids:
        eids.append(_eval(store, subject_id=lid, verdict="ungated"))
    for index in range(2):
        _proposal(store, learning_id=lids[index], status="ungated", eval_result_id=eids[index])
    for index in range(4):
        _eval(store, subject_id=f"seed-scenario-{index}", verdict="ungated")
    _eval(store, eval_id="rule_failed", subject_id="seed-silent-choices", verdict="gated_fail",
          attempted=6, succeeded=2, failed=4, taxonomy={"graded_fail": 4},
          metrics={"without": {"attempted": 3, "succeeded": 2, "failed": 1},
                   "with": {"attempted": 3, "succeeded": 0, "failed": 3}})
    _eval(store, eval_id="rule_helped", subject_id="seed-pricing", verdict="gated_pass",
          attempted=6, succeeded=4, failed=2, taxonomy={"graded_fail": 2},
          metrics={"without": {"attempted": 3, "succeeded": 1, "failed": 2},
                   "with": {"attempted": 3, "succeeded": 3, "failed": 0}})
    _eval(store, eval_id="harness", subject_id="seed-silent-choices", verdict="ungated",
          attempted=3, succeeded=0, failed=3, taxonomy={"agent_error": 3},
          metrics={"without": {"attempted": 3, "succeeded": 0, "failed": 3}, "with": None})
    return lids, eids


def test_gate_health_distinguishes_rule_failed_from_eval_invalid_from_harness_broken(tmp_path):
    store, path = _writable(tmp_path)
    _gate_fixture(store)
    store.close()

    health = q.gate_health(_read_only(path))
    by_id = {row["id"]: row for row in health["rows"]}
    assert by_id["rule_failed"]["class"] == "rule_failed"
    assert by_id["rule_helped"]["class"] == "rule_helped"
    assert by_id["harness"]["class"] == "harness_broken", (
        "an ungated verdict whose trials were all agent_error is a harness break"
    )
    invalid = [r for r in health["rows"] if r["class"] == "eval_invalid"]
    assert len(invalid) == 7
    assert len({by_id["rule_failed"]["class"], by_id["harness"]["class"],
                invalid[0]["class"]}) == 3


def test_a_harness_break_wearing_a_gated_fail_is_not_a_rule_judgement(tmp_path):
    """AGENTS.md: never let a harness failure be recorded as gated_fail."""
    store, path = _writable(tmp_path)
    _eval(store, eval_id="mixed", subject_id="seed-x", verdict="gated_fail",
          attempted=6, succeeded=2, failed=4, taxonomy={"agent_error": 3, "graded_fail": 1},
          metrics={"without": {"attempted": 3, "succeeded": 1, "failed": 2},
                   "with": {"attempted": 3, "succeeded": 1, "failed": 2}})
    store.close()

    row = q.gate_health(_read_only(path))["rows"][0]
    assert row["verdict"] == "gated_fail"
    assert row["class"] == "harness_broken"


def test_gate_health_never_returns_a_pass_rate(tmp_path):
    store, path = _writable(tmp_path)
    _gate_fixture(store)
    store.close()

    health = q.gate_health(_read_only(path))
    blob = json.dumps(health)
    assert "rate" not in health
    assert "pass_rate" not in blob
    assert health["rate_suppressed"]["reason"]


def test_eval_results_are_not_counted_as_proposal_evals_unless_linked(tmp_path):
    """Count proposal evals through their proposal links, not all eval subjects."""
    store, path = _writable(tmp_path)
    _gate_fixture(store)
    store.close()

    health = q.gate_health(_read_only(path))
    assert health["eval_rows_total"] == 10
    assert health["proposal_evals"] == 2, "counting all eval rows overstates the linked proposal evals"
    assert health["seed_evals"] == 7
    assert health["learning_subject_evals"] == 1
    assert health["gate_running"] is True


def test_gate_health_says_the_gate_is_not_running_when_nothing_is_linked(tmp_path):
    store, path = _writable(tmp_path)
    for index in range(5):
        _eval(store, subject_id=f"seed-{index}", verdict="ungated")
    store.close()

    health = q.gate_health(_read_only(path))
    assert health["gate_running"] is False
    assert "not running" in health["sentence"]
    assert "rate" not in health


def test_gate_health_classifies_the_fourth_verdict(tmp_path):
    store, path = _writable(tmp_path)
    _eval(store, eval_id="mixed", subject_id="seed-x", verdict="inconclusive",
          attempted=6, succeeded=4, failed=2, taxonomy={"graded_fail": 2},
          metrics={"without": {"attempted": 3, "succeeded": 1, "failed": 2},
                   "with": {"attempted": 3, "succeeded": 2, "failed": 1}})
    store.close()

    row = q.gate_health(_read_only(path))["rows"][0]
    assert row["class"] == "inconclusive"
    assert "inconclusive" in q.gate_health(_read_only(path))["verdicts_held"]


# ---------------------------------------------------------------------------
# Incidents — PRD 8c
# ---------------------------------------------------------------------------


# Invented fingerprint, messages, counts, and timestamps for window-shape tests.
_SHA1 = "0123456789abcdef0123456789abcdef01234567"

_OCCURRENCE_WINDOW = [
    {"session_file": "/p/a.jsonl", "project_path": "/p", "ts": "2026-01-05T09:10:00.250Z",
     "text": "File content (41000 tokens) exceeds maximum allowed tokens", "count_in_session": 4},
    {"session_file": "/p/b.jsonl", "project_path": "/p", "ts": "2026-01-06T09:10:00.250Z",
     "text": "File content (41000 tokens) exceeds maximum allowed tokens", "count_in_session": 2},
]
_TURN_WINDOW = [
    {"role": "assistant", "ts": "2026-01-05T08:00:00.125Z", "text": "I will inspect the example file."},
    {"role": "user", "ts": "2026-01-05T08:00:10.125Z", "text": "Stop and check the selected file."},
]


def test_incident_window_shape_is_sniffed_not_inferred_from_signal_type():
    """Infer window shape from its keys rather than the incident's signal type.

    The invented repeated-error incidents exercise both occurrence and turn
    windows. A frustration incident also uses the turn shape.
    """
    promoted = q.normalize_incident(
        {"id": "a", "signal_type": "repeated_error", "matched_text": _SHA1,
         "window_json": json.dumps(_OCCURRENCE_WINDOW), "ts": "2026-01-05T09:10:00Z"}
    )
    ordinary_repeat = q.normalize_incident(
        {"id": "b", "signal_type": "repeated_error", "matched_text": "the error text",
         "window_json": json.dumps(_TURN_WINDOW), "ts": "2026-01-05T08:00:00Z"}
    )
    frustration = q.normalize_incident(
        {"id": "c", "signal_type": "frustration", "matched_text": "Stop and check the selected file.",
         "window_json": json.dumps(_TURN_WINDOW), "ts": "2026-01-05T08:00:10Z"}
    )
    assert promoted["window_kind"] == "occurrence"
    assert ordinary_repeat["window_kind"] == "turn", (
        "a repeated_error with a turn window was typed from its signal, not its keys"
    )
    assert frustration["window_kind"] == "turn"
    for view in (promoted, ordinary_repeat, frustration):
        assert view["display_text"]
        assert not q._SHA1_RE.match(view["display_text"])


def test_the_occurrence_shape_carries_what_makes_it_interesting():
    view = q.normalize_incident(
        {"id": "a", "signal_type": "repeated_error", "matched_text": _SHA1,
         "window_json": json.dumps(_OCCURRENCE_WINDOW), "ts": "2026-01-05T09:10:00Z"}
    )
    assert view["occurrences"] == {
        "sessions": 2,
        "total_count": 6,
        "first_ts": "2026-01-05T09:10:00.250Z",
        "last_ts": "2026-01-06T09:10:00.250Z",
        "project_paths": ["/p"],
    }
    assert view["fingerprint"] == _SHA1
    assert view["matched_text_is_fingerprint"] is True


def test_matched_text_sha1_never_reaches_the_view(tmp_path):
    """Show readable evidence instead of an opaque detector fingerprint.

    Exercise occurrence windows, turn windows, and absent archived evidence.
    """
    store, path = _writable(tmp_path)
    _session(store, file_path="s1", project_key="github:1", project_display="repo/one")
    lid = _learning(store)
    for window, text in (
        (_OCCURRENCE_WINDOW, _SHA1),
        (_TURN_WINDOW, "readable trigger"),
        ([], _SHA1),
    ):
        iid = _incident(store, session_file="s1", ts="2026-08-01T00:00:00Z",
                        created_at="2026-08-18T00:00:00Z", signal_type="repeated_error",
                        matched_text=text, window=window)
        store.link_incident_learning(iid, lid)
    store.close()

    rows = q.rules(_read_only(path))["rows"]
    incidents = rows[0]["provenance"]["incidents"]
    assert len(incidents) == 3
    for view in incidents:
        assert not q._SHA1_RE.match(view["display_text"]), view["display_text"]
    # Even with no window at all, the fingerprint is labelled rather than shown raw.
    empty = [v for v in incidents if v["window_len"] == 0][0]
    assert "fingerprint" in empty["display_text"]
    assert empty["window_kind"] == "empty"


def test_incident_display_text_reports_what_it_truncated():
    long_text = "x" * (q.DISPLAY_TEXT_MAX + 250)
    view = q.normalize_incident(
        {"id": "a", "signal_type": "frustration", "matched_text": long_text,
         "window_json": "[]", "ts": "2026-08-01T00:00:00Z"}
    )
    assert len(view["display_text"]) == q.DISPLAY_TEXT_MAX
    assert view["display_text_truncated"] == {
        "cut_chars": 250,
        "original_chars": q.DISPLAY_TEXT_MAX + 250,
    }


def test_incident_missing_a_column_raises(tmp_path):
    with pytest.raises(q.DashboardDataError, match="window_json"):
        q.normalize_incident({"id": "a", "signal_type": "x", "matched_text": "y"})


def test_incident_with_broken_json_raises():
    with pytest.raises(q.DashboardDataError, match="not strict JSON"):
        q.normalize_incident(
            {"id": "a", "signal_type": "x", "matched_text": "y", "window_json": "{oops"}
        )


# ---------------------------------------------------------------------------
# V2 — Rules
# ---------------------------------------------------------------------------


def test_rules_carry_state_target_evidence_provenance_and_verdict(tmp_path):
    store, path = _writable(tmp_path)
    _session(store, file_path="s1", project_key="github:1", project_display="example/demo-service",
             source="codex")
    lid = _learning(store, source="codex", evidence_count=2, scope="global")
    for _ in range(2):
        iid = _incident(store, session_file="s1", ts="2026-08-01T00:00:00Z",
                        created_at="2026-08-18T00:00:00Z", signal_type="friction_loop",
                        matched_text="7 edit->error cycles")
        store.link_incident_learning(iid, lid)
    eid = _eval(store, subject_id=lid, verdict="ungated")
    _proposal(store, learning_id=lid, status="ungated", eval_result_id=eid)
    store.close()

    row = q.rules(_read_only(path))["rows"][0]
    assert row["status"] == "proposed"
    assert row["rule_text"] == "do the thing"
    assert row["target_summary"] == "/Users/x/.claude/CLAUDE.md"
    assert row["evidence_linked"] == 2
    assert row["provenance"]["repos"] == ["example/demo-service"]
    assert row["provenance"]["agent_product"] == "codex"
    assert row["provenance"]["agents_in_evidence"] == ["codex"]
    assert row["provenance"]["session_ids"] == ["s1"]
    assert row["gate_verdict"] == "ungated"
    assert row["gate_class"] == "eval_invalid"
    assert row["gate_verdict_source"] == "proposals.eval_result_id"


def test_rules_without_mining_history_have_unknown_generation_with_a_reason(tmp_path):
    """A learning without recorded mining history has unknown generation."""
    store, path = _writable(tmp_path)
    _learning(store)
    store.close()

    generation = q.rules(_read_only(path))["rows"][0]["miner_generation"]
    assert generation["value"] == q.NOT_COMPUTABLE == "—"
    assert generation["computable"] is False
    assert "run_id" in generation["reason"]
    assert generation["value"] not in (0, "0", None, "")


def test_rules_flag_the_enforcement_gap_only_when_a_rule_was_violated(tmp_path):
    store, path = _writable(tmp_path)
    _learning(store, learning_id="clean", created_at="2026-08-18T07:00:00Z")
    _learning(store, learning_id="gap", violated_existing_rule="Always read the file first",
              created_at="2026-08-18T08:00:00Z")
    store.close()

    rows = {row["id"]: row["enforcement_gap"] for row in q.rules(_read_only(path))["rows"]}
    assert rows["clean"]["flagged"] is False
    assert rows["clean"]["label"] == "No prior rule violation was recorded"
    assert rows["gap"]["flagged"] is True
    assert rows["gap"]["violated_existing_rule"] == "Always read the file first"


def test_rules_report_an_eval_that_no_proposal_points_at(tmp_path):
    """The authoritative link is proposals.eval_result_id; a subject_id match
    is weaker evidence and must not be folded in silently."""
    store, path = _writable(tmp_path)
    lid = _learning(store)
    _eval(store, eval_id="orphan", subject_id=lid, verdict="gated_fail",
          metrics={"without": {"attempted": 3}, "with": {"attempted": 3}})
    store.close()

    row = q.rules(_read_only(path))["rows"][0]
    assert row["gate_verdict"] is None
    assert row["gate_verdict_source"] == "none"
    assert row["unlinked_subject_evals"] == ["orphan"]


def test_rules_evidence_sample_reports_what_it_cut(tmp_path):
    store, path = _writable(tmp_path)
    _session(store, file_path="s1", project_key="github:1", project_display="repo/one")
    lid = _learning(store, evidence_count=12)
    for _ in range(12):
        iid = _incident(store, session_file="s1", ts="2026-08-01T00:00:00Z",
                        created_at="2026-08-18T00:00:00Z")
        store.link_incident_learning(iid, lid)
    store.close()

    provenance = q.rules(_read_only(path), evidence_sample=5)["rows"][0]["provenance"]
    assert provenance["incidents_shown"] == 5
    assert provenance["incidents_total"] == 12
    assert provenance["incidents_cut"] == 7
    assert "capped at 5" in provenance["cut_reason"]


def test_duplicate_families_return_a_designed_empty_state_that_says_so(tmp_path):
    """Absent computed learning families require an explicit unavailable state."""
    store, path = _writable(tmp_path)
    _learning(store)
    _learning(store)
    store.close()

    grouping = q.rules(_read_only(path))["grouping"]
    assert grouping["available"] is False
    assert grouping["groups"] == []
    assert grouping["learning_embeddings"] == 0
    assert "separate embedding comparison" in grouping["reason"]
    assert grouping["empty_state"]


def test_duplicate_families_group_when_duplicate_of_is_populated(tmp_path):
    store, path = _writable(tmp_path)
    _learning(store, learning_id="head", created_at="2026-08-18T07:00:00Z")
    _learning(store, learning_id="dup1", duplicate_of="head", created_at="2026-08-18T07:01:00Z")
    _learning(store, learning_id="dup2", duplicate_of="head", created_at="2026-08-18T07:02:00Z")
    store.close()

    grouping = q.rules(_read_only(path))["grouping"]
    assert grouping["available"] is True
    assert grouping["groups"] == [
        {"family_key": "head", "learning_ids": ["dup1", "dup2"], "size": 2}
    ]


# ---------------------------------------------------------------------------
# V4 — Projects
# ---------------------------------------------------------------------------


def _clone_fixture(store, *, clones=7):
    for index in range(clones):
        _session(
            store,
            file_path=f"dt-{index}",
            project_key="github:424242",
            project_display="example/demo-service",
            project_path=f"/Users/example/Code/old-service/repo-{index}",
            lines=100_000,
        )
    # An invented project with no display name exercises the key fallback.
    _session(store, file_path="blank", project_key="path:/", project_display="",
             project_path="/", lines=50_000)


def test_project_rollup_collapses_clones_and_survives_an_empty_display_name(tmp_path):
    """Group a repository's working copies by sessions.project_key.

    The invented clone set produces one project row. Raw directory paths do not
    establish distinct repositories; routing policy is a separate contract.
    """
    store, path = _writable(tmp_path)
    _clone_fixture(store)
    store.close()

    rollup = q.projects(_read_only(path), weigh_top_n=0, isdir=lambda p: False)
    assert rollup["grouped_on"] == "sessions.project_key"
    assert rollup["count"] == 2, "clones were counted as separate repos"
    demo_service = rollup["rows"][0]
    assert demo_service["project_key"] == "github:424242"
    assert demo_service["clones"] == 7
    assert demo_service["sessions"] == 7
    assert len(demo_service["clone_paths"]) == 7

    blank = [r for r in rollup["rows"] if r["project_key"] == "path:/"][0]
    assert blank["label"] == "path:/"
    assert blank["label_is_fallback"] is True
    assert blank["label"] != ""


def test_project_benefit_is_an_em_dash_when_no_proposal_has_been_applied(tmp_path):
    """0 would assert 'applied, no effect', which is false (PRD 7 / V4)."""
    store, path = _writable(tmp_path)
    _clone_fixture(store, clones=2)
    store.close()

    benefit = q.projects(_read_only(path), weigh_top_n=0, isdir=lambda p: False)["rows"][0][
        "benefit"
    ]
    assert benefit["value"] == "—"
    assert benefit["value"] is not None
    assert benefit["value"] != 0 and benefit["value"] != "0" and benefit["value"] != ""
    assert "project_stats" in benefit["reason"]


def test_project_legacy_session_totals_cannot_establish_physical_exposure(tmp_path):
    store, path = _writable(tmp_path)
    _session(store, file_path="big", project_key="big", project_display="big", lines=1_000_000)
    _incident(store, session_file="big", ts="2026-08-01T00:00:00Z",
              created_at="2026-08-18T00:00:00Z", project_key="big")
    store.close()
    reader = _read_only(path)
    row = q.projects(reader, weigh_top_n=0, isdir=lambda p: False,
                     now_utc=datetime(2026, 8, 20, tzinfo=timezone.utc))["rows"][0]
    reader.close()
    assert row["exposure"]["reason"] == "missing_observations"
    assert row["exposure"]["rate_per_100k"] is None
    assert "incident_rate" not in row


def test_project_top_signal_reports_its_tie_break(tmp_path):
    store, path = _writable(tmp_path)
    _session(store, file_path="s", project_key="k", project_display="k", lines=100_000)
    for signal in ("standing_instruction", "frustration"):
        for _ in range(10):
            _incident(store, session_file="s", ts="2026-08-01T00:00:00Z",
                      created_at="2026-08-18T00:00:00Z", signal_type=signal, project_key="k")
    store.close()

    top = q.projects(_read_only(path), weigh_top_n=0, isdir=lambda p: False)["rows"][0][
        "top_signal"
    ]
    assert top["signal_type"] == "frustration"
    assert top["count"] == 10
    assert top["tied_with"] == ["standing_instruction"]


def test_project_context_weight_uses_the_walker_and_names_a_broken_contract(tmp_path):
    store, path = _writable(tmp_path)
    _session(store, file_path="good", project_key="good", project_display="good",
             project_path="/repos/good", lines=100_000)
    _session(store, file_path="bad", project_key="bad", project_display="bad",
             project_path="/repos/bad", lines=90_000)
    store.close()

    def weigh(repo_path):
        if repo_path == "/repos/good":
            return {"total_bytes": 57_000, "always_loaded_bytes": 57_000, "files": ["AGENTS.md"]}
        return {"total_bytes": 1}  # contract violation: no always_loaded_bytes, no files

    rollup = q.projects(_read_only(path), weigh=weigh, isdir=lambda p: True)
    rows = {r["project_key"]: r for r in rollup["rows"]}
    assert rows["good"]["context_weight"]["total_bytes"] == 57_000
    assert rows["good"]["context_weight"]["computable"] is True
    assert rows["bad"]["context_weight"]["value"] == "—"
    assert "always_loaded_bytes" in rows["bad"]["context_weight"]["reason"]
    assert rollup["context_weight_errors"][0]["project_key"] == "bad"


def test_project_context_weight_cap_reports_what_it_skipped(tmp_path):
    store, path = _writable(tmp_path)
    for index in range(4):
        _session(store, file_path=f"s{index}", project_key=f"k{index}",
                 project_display=f"k{index}", project_path=f"/repos/{index}",
                 lines=100_000 - index)
    store.close()

    rollup = q.projects(
        _read_only(path), weigh=lambda p: {"total_bytes": 1, "always_loaded_bytes": 1, "files": []},
        isdir=lambda p: True, weigh_top_n=2,
    )
    assert rollup["context_weight_capped"] == {
        "measured": 2, "skipped": 2, "reason": "caller passed weigh_top_n=2",
    }
    assert rollup["rows"][0]["context_weight"]["computable"] is True
    assert rollup["rows"][3]["context_weight"]["value"] == "—"
    assert "capped" in rollup["rows"][3]["context_weight"]["reason"]


def test_project_context_weight_skips_a_working_copy_that_is_gone(tmp_path):
    store, path = _writable(tmp_path)
    _session(store, file_path="s", project_key="k", project_display="k",
             project_path="/repos/deleted", lines=100_000)
    store.close()

    row = q.projects(
        _read_only(path), weigh=lambda p: pytest.fail("measured a directory that is gone"),
        isdir=lambda p: False,
    )["rows"][0]
    assert row["context_weight"]["value"] == "—"
    assert "on disk" in row["context_weight"]["reason"]
    assert row["context_path"] == ""


def test_project_rows_report_incidents_whose_repo_has_no_sessions(tmp_path):
    store, path = _writable(tmp_path)
    _session(store, file_path="s", project_key="known", project_display="known")
    _incident(store, session_file="s", ts="2026-08-01T00:00:00Z",
              created_at="2026-08-18T00:00:00Z", project_key="ghost")
    store.close()

    rollup = q.projects(_read_only(path), weigh_top_n=0, isdir=lambda p: False)
    assert rollup["unmatched_incident_keys"] == [{"project_key": "ghost", "incidents": 1}]


def test_project_proposals_outside_every_clone_path_are_reported_not_dropped(tmp_path):
    store, path = _writable(tmp_path)
    _session(store, file_path="s", project_key="k", project_display="k",
             project_path="/repos/k", lines=100_000)
    lid = _learning(store)
    _proposal(store, proposal_id="local", learning_id=lid,
              target_path="/repos/k/AGENTS.md", target_kind="project_agents_md")
    _proposal(store, proposal_id="global", learning_id=lid,
              target_path="/Users/example/.claude/CLAUDE.md")
    store.close()

    rollup = q.projects(_read_only(path), weigh_top_n=0, isdir=lambda p: False)
    assert rollup["rows"][0]["rules_received"] == 1
    assert [p["proposal_id"] for p in rollup["proposals_not_attributed"]] == ["global"]


def test_rules_written_here_counts_distinct_learnings(tmp_path):
    store, path = _writable(tmp_path)
    _session(store, file_path="s", project_key="k", project_display="k", lines=100_000)
    lid = _learning(store)
    for _ in range(4):
        iid = _incident(store, session_file="s", ts="2026-08-01T00:00:00Z",
                        created_at="2026-08-18T00:00:00Z", project_key="k")
        store.link_incident_learning(iid, lid)
    store.close()

    row = q.projects(_read_only(path), weigh_top_n=0, isdir=lambda p: False)["rows"][0]
    assert row["rules_written_here"] == 1, "four evidence incidents counted as four rules"


# ---------------------------------------------------------------------------
# Incident-rate trend — PRD 8b
# ---------------------------------------------------------------------------


def _monthly_fixture(store):
    """Invented monthly totals separate incident counts from session size.

    Non-divisible line totals also exercise distribution across fixture sessions.
    The small April sample remains below the reporting floor.
    """
    plan = [
        ("2026-06", 120, 24001, 24),
        ("2026-07", 80, 16001, 16),
        ("2026-08", 60, 96001, 30),
        ("2026-04", 2, 900, 10),
    ]
    for month, sessions, lines, incidents in plan:
        per_session = lines // sessions
        remainder = lines - per_session * sessions
        for index in range(sessions):
            _session(store, file_path=f"{month}-{index}", project_key="k", project_display="k",
                     first_ts=f"{month}-05T00:00:00Z",
                     lines=per_session + (remainder if index == 0 else 0))
        for index in range(incidents):
            _incident(store, session_file=f"{month}-0", ts=f"{month}-06T00:00:00Z",
                      created_at="2026-08-18T00:00:00Z", project_key="k")


def test_legacy_session_and_queue_totals_never_become_observed_trends(tmp_path):
    store, path = _writable(tmp_path)
    _monthly_fixture(store)
    store.close()
    trend = q.incident_rate(_read_only(path), now_utc=_NOW_DT)
    assert trend["contract_version"] == 2
    assert trend["primary_series"] == "signal_occurrences_per_100k_physical_lines"
    assert trend["series"] == [] and trend["dropped_months"] == []
    assert trend["reason"] == "missing_observations"
    assert not trend["computable"]


# ---------------------------------------------------------------------------
# Small samples never become rates
# ---------------------------------------------------------------------------


def test_small_sample_returns_the_raw_fraction_and_a_flag_never_a_rate(tmp_path):
    """0 applied and 0 rolled back must not render as '0% success'."""
    store, path = _writable(tmp_path)
    lid = _learning(store)
    _proposal(store, learning_id=lid, status="pending")
    _run(store, started="2026-08-18T09:00:00Z", finished="2026-08-18T10:00:00Z",
         stats={"review_only": True})
    store.close()

    survival = q.overview(_read_only(path), Config(), now_utc=NOW)["confidence"]["survival"]
    assert survival == {
        "numerator": 0,
        "denominator": 0,
        "rate": None,
        "enough_data": False,
        "min_denominator": 20,
        "display": survival["display"],
        "reason": survival["reason"],
        # Added 2026-08-23: the labels travel WITH the numbers, so a caller
        # cannot silently attach the wrong word to a denominator.
        "numerator_label": "applied",
        "denominator_label": "ever applied",
    }
    assert survival["rate"] is None
    assert survival["rate"] is not 0.0  # noqa: F632 - identity is the point
    assert "%" not in survival["display"]
    assert "0 applied" in survival["display"] and "0 rolled back" in survival["display"]


def test_a_big_enough_sample_does_get_a_rate():
    """The complement: the guard suppresses rates, it does not forbid them."""
    result = q.fraction(
        18, 20, min_denominator=20, numerator_label="applied",
        denominator_label="decided", reason_when_small="too few",
    )
    assert result["enough_data"] is True
    assert result["rate"] == pytest.approx(0.9)
    assert "%" in result["display"]


# ---------------------------------------------------------------------------
# The whole V1 payload
# ---------------------------------------------------------------------------


def test_overview_is_json_serializable_and_carries_every_panel(tmp_path):
    store, path = _writable(tmp_path)
    _session(store, file_path="s1", project_key="github:1", project_display="repo/one")
    _incident(store, session_file="s1", ts="2026-08-18T09:00:00Z",
              created_at="2026-08-18T02:00:00Z")
    _run(store, started="2026-08-18T00:00:00Z", finished="2026-08-18T01:00:00Z",
         stats=_FULL_STATS)
    lid = _learning(store)
    _proposal(store, learning_id=lid, status="pending")
    store.close()

    payload = q.overview(_read_only(path), Config(), now_utc=NOW)
    assert sorted(payload) == [
        "backlog", "confidence", "failures", "freshness", "gate", "grid", "inbox", "status_line",
    ]
    json.dumps(payload)  # must survive the wire
    assert payload["freshness"]["days_stale"] == 4
    assert payload["inbox"]["count"] == 1
    assert payload["grid"]["columns"][-1]["night"] == "2026-08-22"


def test_overview_on_an_empty_database_says_so_rather_than_dividing(tmp_path):
    store, path = _writable(tmp_path)
    store.close()

    payload = q.overview(_read_only(path), Config(), now_utc=NOW)
    assert payload["freshness"]["as_of"] is None
    assert payload["freshness"]["days_stale"] is None
    assert payload["backlog"]["arrivals"]["per_day"] is None
    assert payload["backlog"]["net_per_day"] is None
    assert payload["status_line"]["nights_ran"] == 0
    assert payload["gate"]["gate_running"] is False
    json.dumps(payload)


def test_malformed_stats_json_raises_rather_than_rendering_a_blank_night(tmp_path):
    store, path = _writable(tmp_path)
    store.insert("runs", {"id": "bad", "started": "2026-08-18T00:00:00Z", "finished": "",
                          "status": "ok", "stats_json": "{not json", "report_path": ""})
    store.close()

    with pytest.raises(q.DashboardDataError, match="not strict JSON"):
        q.run_stage_grid(_read_only(path), now_utc=NOW)


# ---------------------------------------------------------------------------
# context_path must be the copy the operator actually works in
# ---------------------------------------------------------------------------
#
# Measure context in the existing working copy with the most sessions.
# Alphabetical path order does not establish which instructions an agent used.


def _busy_and_idle_clones(store):
    """repo-0 sorts first alphabetically and has ONE session.
    repo-9 sorts last and has fifty. The operator works in repo-9."""
    _session(store, file_path="idle", project_key="k", project_display="acme/thing",
             project_path="/repos/thing/repo-0", lines=1_000)
    for index in range(50):
        _session(store, file_path=f"busy-{index}", project_key="k",
                 project_display="acme/thing",
                 project_path="/repos/thing/repo-9", lines=1_000)


def test_context_path_is_the_busiest_on_disk_clone_not_the_alphabetical_first(tmp_path):
    """Sabotage: restore `context_path = on_disk[0]`. This fails, naming repo-0."""
    store, path = _writable(tmp_path)
    _busy_and_idle_clones(store)
    store.close()

    row = q.projects(_read_only(path), weigh_top_n=0, weigh=lambda path: {}, isdir=lambda p: True)["rows"][0]
    assert row["context_path"] == "/repos/thing/repo-9", (
        "context weight was measured in the alphabetically-first checkout, "
        "which is not what the payload's own reason string promises"
    )


def test_context_path_skips_a_busy_clone_that_is_no_longer_on_disk(tmp_path):
    """Most sessions is the rule, but only among copies that still exist."""
    store, path = _writable(tmp_path)
    _busy_and_idle_clones(store)
    store.close()

    row = q.projects(
        _read_only(path), weigh_top_n=0, weigh=lambda path: {},
        isdir=lambda p: p != "/repos/thing/repo-9",
    )["rows"][0]
    assert row["context_path"] == "/repos/thing/repo-0"


def test_context_path_ties_break_alphabetically_so_the_answer_is_stable(tmp_path):
    """Two copies, same session count: the choice must not wobble run to run."""
    store, path = _writable(tmp_path)
    _session(store, file_path="b", project_key="k", project_display="acme/thing",
             project_path="/repos/thing/repo-5", lines=1_000)
    _session(store, file_path="a", project_key="k", project_display="acme/thing",
             project_path="/repos/thing/repo-2", lines=1_000)
    store.close()

    row = q.projects(_read_only(path), weigh_top_n=0, weigh=lambda path: {}, isdir=lambda p: True)["rows"][0]
    assert row["context_path"] == "/repos/thing/repo-2"


def test_the_context_path_reason_describes_what_the_code_actually_does(tmp_path):
    """The reason string is a CLAIM. It was false for as long as it existed."""
    store, path = _writable(tmp_path)
    _busy_and_idle_clones(store)
    store.close()

    row = q.projects(_read_only(path), weigh_top_n=0, weigh=lambda path: {}, isdir=lambda p: True)["rows"][0]
    reason = row["context_path_reason"]
    assert "most sessions" in reason
    assert row["context_path"] == "/repos/thing/repo-9", (
        f"reason claims {reason!r} but the code chose {row['context_path']!r}"
    )


# ---------------------------------------------------------------------------
# The small-sample sentence must say what the numbers are
# ---------------------------------------------------------------------------
#
# Applied and rolled-back counts are separate quantities. Small-sample copy
# must label each count directly. The survival denominator includes both
# groups and must not be described as the rollback count.


def test_the_small_sample_sentence_labels_applied_and_rolled_back_counts(tmp_path):
    """Label applied and rolled-back counts directly in the small-sample sentence.

    Without small_display, the rollback label would describe the total ever
    applied rather than the count of rolled-back rules.
    """
    survival = q.fraction(
        1, 2, min_denominator=20,
        numerator_label="applied", denominator_label="ever applied",
        reason_when_small="not enough applied rules to judge the loop yet",
        small_display="1 applied · 1 rolled back",
    )
    assert survival["display"] == (
        "1 applied · 1 rolled back — not enough applied rules to judge the loop yet"
    )
    assert "%" not in survival["display"]
    assert survival["rate"] is None
    assert survival["enough_data"] is False


def test_the_survival_fraction_never_calls_the_total_a_rollback_count(tmp_path):
    """Exercise empty counts and a sample with one applied and one rolled-back rule."""
    store, path = _writable(tmp_path)
    _clone_fixture(store, clones=2)
    store.close()
    live = q.overview(_read_only(path), Config(), now_utc="2026-08-23T00:00:00Z")["confidence"]["survival"]
    assert "0 applied · 0 rolled back" in live["display"]
    assert live["denominator_label"] != "rolled back", (
        "the denominator is every rule ever applied, not the rollback count"
    )


def test_a_large_enough_sample_still_renders_a_rate(tmp_path):
    """small_display must not leak into the case where a rate is honest."""
    got = q.fraction(
        18, 20, min_denominator=20,
        numerator_label="applied", denominator_label="ever applied",
        reason_when_small="unused", small_display="never shown",
    )
    assert got["display"] == "18/20 (90%)"
    assert got["enough_data"] is True


# ---------------------------------------------------------------------------
# A grid cell's number must describe the same thing its tooltip does
# ---------------------------------------------------------------------------
#
# The cluster cell and tooltip must describe the same quantity. When
# candidates pass through without merging, zero merge counters must not
# replace the candidate count in the tooltip.


def _cluster_cell(payload):
    return q._stage_numbers("cluster", payload)


def test_a_cluster_cell_that_merged_nothing_agrees_with_its_own_tooltip():
    """Keep the cluster count and tooltip consistent when no merge was attempted.

    Passthrough candidates must not borrow zero attempted/succeeded values from
    the unused merge counters.
    """
    cell = _cluster_cell(
        {"candidates": 9, "merge_attempted": 0, "merge_succeeded": 0, "merge_failed": 0}
    )
    assert cell["number"] == 9
    assert cell["attempted"] == 9, (
        f"cell shows {cell['number']} above a tooltip built from "
        f"attempted={cell['attempted']}"
    )
    assert cell["succeeded"] == 9
    assert cell["failed"] == 0


def test_a_cluster_cell_that_really_merged_keeps_its_merge_counters():
    """The other branch must not be flattened by the fix."""
    cell = _cluster_cell(
        {"candidates": 10, "merge_attempted": 8, "merge_succeeded": 6, "merge_failed": 2}
    )
    assert cell["attempted"] == 8
    assert cell["succeeded"] == 6
    assert cell["failed"] == 2


def test_the_scan_cell_says_what_it_is_counting():
    """Label the sum of scan passes across runs, which can exceed distinct sessions."""
    cell = q._stage_numbers("scan", {
        "files_attempted": 100, "files_succeeded": 100, "files_failed": 0,
    })
    assert "session" not in cell["number_label"], (
        f"label {cell['number_label']!r} claims sessions; the figure is a sum "
        "over the night's runs and double-counts a rescan"
    )
    assert "file" in cell["number_label"]


def test_the_arrival_rate_excludes_the_partial_final_day_and_says_so(tmp_path):
    """Compare complete data days and report the excluded partial final day.
    Including that day in the denominator would understate the arrival rate.
    """
    store, path = _writable(tmp_path)
    _session(store, file_path="s1", project_key="github:1",
             project_display="acme/x", project_path="/repos/x", lines=1000)
    # Three full days at 100, then a partial day with 1.
    for day, count in (("2026-08-01", 100), ("2026-08-02", 100),
                       ("2026-08-03", 100), ("2026-08-04", 1)):
        for i in range(count):
            _incident(store, incident_id=f"{day}-{i}", session_file="s1",
                      ts=f"{day}T09:00:00Z", created_at="2026-08-05T00:00:00Z")
    store.close()

    backlog = q.overview(
        _read_only(path), Config(), now_utc="2026-08-04T23:00:00Z"
    )["backlog"]
    arrivals = backlog["arrivals"]
    # The window is 14 days, so ten of them are zero. What matters is that the
    # divisor is 13 complete days and not 14, and that dropping the partial day
    # RAISES the rate rather than leaving it understated.
    with_partial = 301 / 14
    assert arrivals["per_day"] == pytest.approx(300 / 13)
    assert arrivals["per_day"] > with_partial, (
        f"{arrivals['per_day']} is no better than {with_partial}, so the "
        "partial day is still being averaged against full ones"
    )
    excluded = arrivals["excluded_partial_day"]
    assert excluded["day"] == "2026-08-04"
    assert excluded["incidents"] == 1
    assert "cut short" in excluded["why"]
    # The cut is reported, never silent: total still counts every incident.
    assert arrivals["total_in_window"] == 301
    assert arrivals["rate_days"] < arrivals["window_days"]


def test_a_single_day_of_data_is_not_thrown_away(tmp_path):
    """Dropping the only day would leave no rate at all."""
    store, path = _writable(tmp_path)
    _session(store, file_path="s1", project_key="github:1",
             project_display="acme/x", project_path="/repos/x", lines=1000)
    for i in range(5):
        _incident(store, incident_id=f"only-{i}", session_file="s1",
                  ts="2026-08-01T09:00:00Z", created_at="2026-08-05T00:00:00Z")
    store.close()
    arrivals = q.overview(
        _read_only(path), Config(), now_utc="2026-08-01T23:00:00Z"
    )["backlog"]["arrivals"]
    assert arrivals["per_day"] is not None
    assert arrivals["total_in_window"] == 5


# ---------------------------------------------------------------------------
# The rule inspector receives the stored proposal diff
# ---------------------------------------------------------------------------
#
# The rule inspector must receive the stored proposal diff, as required by V2.


def test_a_rule_carries_the_diff_its_proposal_would_apply(tmp_path):
    """Sabotage: drop diff_unified from the proposals SELECT in rules()."""
    store, path = _writable(tmp_path)
    _session(store, file_path="s1", project_key="github:1",
             project_display="acme/x", project_path="/repos/x", lines=1000)
    lid = _learning(store, rule_text="**A rule**")
    _proposal(store, learning_id=lid, target_path="/repos/x/AGENTS.md",
              diff_unified="--- a\n+++ b\n@@ -1 +1,2 @@\n line\n+**A rule**\n")
    store.close()

    rows = q.rules(_read_only(path))["rows"]
    target = (rows[0].get("targets") or [{}])[0]
    assert target.get("diff"), "the inspector has no diff to show"
    assert "+**A rule**" in target["diff"]


def test_a_proposal_with_no_diff_says_so_rather_than_rendering_nothing(tmp_path):
    """An empty diff column and a diff nobody fetched are different facts."""
    store, path = _writable(tmp_path)
    _session(store, file_path="s1", project_key="github:1",
             project_display="acme/x", project_path="/repos/x", lines=1000)
    lid = _learning(store, rule_text="**A rule**")
    _proposal(store, learning_id=lid, target_path="/repos/x/AGENTS.md",
              diff_unified="")
    store.close()

    target = (q.rules(_read_only(path))["rows"][0].get("targets") or [{}])[0]
    assert target.get("diff") == ""
    assert target.get("diff_reason"), "an absent diff must name why it is absent"


def test_a_new_constraint_break_is_not_reported_as_an_old_fixed_bug(tmp_path):
    """`IntegrityError` was marked fixed on 2026-08-16, and it is a class.

    The fix — ON CONFLICT DO NOTHING on incident_learnings — closed exactly one
    constraint. Resolving FIXED_FAILURES by family would greet every future
    constraint break as that bug regressing, with its wording attached.
    """
    store, path = _writable(tmp_path)
    _run(store, run_id="r", started="2026-08-23T12:00:00Z",
         finished="2026-08-23T12:01:00Z", status="ok",
         stats={"mine": {"attempted": 9, "succeeded": 0, "failed": 9,
                         "taxonomy": {"IntegrityError:not_null:learnings.rule_text": 9}}})
    store.close()

    panel = q.failure_panel(_read_only(path))
    rows = {r["class"]: r for r in panel["open"] + panel["quiet"] + panel["regressed"]}
    row = rows["IntegrityError:not_null:learnings.rule_text"]
    assert row["status"] != "regressed", (
        "a brand-new constraint break was reported as a fixed bug regressing"
    )
    assert "fix_commit" not in row, "it was attached to someone else's fix"
    # It still gets words, from the family.
    assert row["has_copy"] is True
    assert "incident to the same learning" not in row["explanation"]
    assert panel["unknown_classes"] == []


def test_the_constraint_that_really_was_fixed_still_reads_as_fixed(tmp_path):
    """Narrowness guard: the specific key keeps its fix record."""
    store, path = _writable(tmp_path)
    _run(store, run_id="r", started="2026-08-15T12:00:00Z",
         finished="2026-08-15T12:01:00Z", status="ok",
         stats={"mine": {"attempted": 8, "succeeded": 0, "failed": 8,
                         "taxonomy": {"IntegrityError:unique:incident_learnings": 8}}})
    store.close()

    panel = q.failure_panel(_read_only(path))
    rows = {r["class"]: r for r in panel["fixed"]}
    row = rows["IntegrityError:unique:incident_learnings"]
    assert row["status"] == "fixed", row
    assert row["fix_commit"].startswith("cfd6410")


def test_the_empty_output_outcome_has_words_of_its_own(tmp_path):
    """A new llm outcome with no copy renders as a number with no explanation.

    `empty_output` was split out of `parse_error` because that label told the
    operator the model replied. Adding the outcome without adding the copy
    would trade one wrong sentence for no sentence.
    """
    store, path = _writable(tmp_path)
    _llm_call(store, stage="mine_agentic", outcome="empty_output", n=80)
    store.close()

    panel = q.failure_panel(_read_only(path))
    row = next(r for r in panel["open"] + panel["quiet"]
               if r["class"] == "empty_output")
    assert row["has_copy"] is True
    assert "wrote nothing" in row["explanation"]
    assert panel["unknown_classes"] == []


def test_every_llm_outcome_that_is_a_failure_has_operator_copy():
    """The contract behind the test above: llm.OUTCOMES is the source of truth
    and FAILURE_COPY must keep up with it."""
    from self_improve import llm as llm_mod

    failures = set(llm_mod.OUTCOMES) - set(q.LLM_SUCCESS_OUTCOMES)
    missing = sorted(failures - set(q.FAILURE_COPY))
    assert not missing, (
        f"llm outcomes {missing} can reach the failure panel with no copy, so "
        "they render as a count with nothing beside it"
    )


def test_every_taxonomy_key_the_pipeline_writes_has_operator_words():
    """Reconcile literal pipeline taxonomy keys with dashboard explanations.
    Exception-derived keys are covered by prefix families. A new literal key
    must not render as an unexplained count.
    """
    import re

    from pathlib import Path

    from self_improve import pipeline as pl

    source = Path(pl.__file__).read_text(encoding="utf-8")
    literal = set(re.findall(r'_bump_tax\("([a-z_]+)"\)', source))
    literal |= set(re.findall(r'mine_stats\["taxonomy"\]\["([a-z_]+)"\]', source))
    assert literal, "the scanner found no taxonomy keys; it has stopped working"

    missing = sorted(k for k in literal if q.failure_copy(k) is None
                     and k not in q.NOT_FAILURE_TAXONOMY)
    assert not missing, (
        f"pipeline writes taxonomy keys {missing} that the dashboard has no "
        "words for, so each renders as a bare count"
    )

    # A literal key must have its OWN entry, not fall back to a prefix family.
    # `gate_sandbox_unverified` matches the `gate_` prefix, whose words say
    # "the gate raised while evaluating a proposal" — and that is false: the
    # gate did not raise, the sandbox could not be verified and the trial was
    # never run. Generic words that describe the wrong thing are the same
    # failure as a wrong specific cause.
    by_prefix = sorted(
        k for k in literal
        if k not in q.FAILURE_COPY and k not in q.NOT_FAILURE_TAXONOMY
    )
    assert not by_prefix, (
        f"taxonomy keys {by_prefix} are written literally by pipeline but get "
        "their words from a prefix family, which describes a different event"
    )


def test_an_exception_family_key_gets_words_without_asserting_a_cause():
    """Resolve exception-class keys by stage prefix without inventing a cause.

    The prefix identifies the stage. An arbitrary exception class does not
    establish a more specific failure explanation.
    """
    for key, stage in (
        ("gate_ValueError", "gate"),
        ("propose_KeyError", "propose"),
        ("prune_TimeoutError", "prune"),
        ("not_appliable_pending", "apply"),
    ):
        copy = q.failure_copy(key)
        assert copy is not None, f"{key} has no words"
        assert stage in copy["explanation"] or stage in copy["name"].lower(), (
            f"{key} -> {copy}"
        )

    # The prefix must not swallow a key that has its own entry.
    assert q.failure_copy("gate_budget_exhausted") is None, (
        "gate_budget_exhausted is NOT a failure; it belongs to "
        "NOT_FAILURE_TAXONOMY and the prefix must not claim it"
    )


def test_the_bare_integrity_key_does_not_promise_a_suffix_it_lacks(tmp_path):
    """Explain historical bare IntegrityError keys without promising a suffix."""
    store, path = _writable(tmp_path)
    _run(store, run_id="r", started="2026-08-16T09:30:00Z",
         finished="2026-08-16T09:40:00Z", status="ok",
         stats={"mine": {"attempted": 8, "succeeded": 0, "failed": 8,
                         "taxonomy": {"IntegrityError": 8}}})
    store.close()

    panel = q.failure_panel(_read_only(path))
    row = next(r for r in panel["fixed"] if r["class"] == "IntegrityError")
    text = row["explanation"]
    assert "A row with NO suffix is older than that split" in text, text
    assert "cannot say which constraint it was" in text




# ---------------------------------------------------------------------------
# V3 — the review queue. Grouped by lesson, and its vocabulary must not be a
# second copy of the one the nav inbox already counts.
# ---------------------------------------------------------------------------


def _queue_fixture(tmp_path):
    store, path = _writable(tmp_path)
    _run(store, started="2026-08-22T02:30:00Z", finished="2026-08-22T05:00:00Z",
         stats={"review_only": True})
    # One lesson, two targets — the family V3 exists to collapse.
    fam = _learning(store, rule_text="always derive the account list")
    _proposal(store, learning_id=fam, status="inconclusive",
              target_path="/Users/x/.claude/CLAUDE.md")
    _proposal(store, learning_id=fam, status="gated_fail",
              target_path="/Users/x/repo/AGENTS.md", target_kind="project_agents_md")
    # An ungated lesson also needs a human decision.
    auto = _learning(store, rule_text="this one is ungated")
    _proposal(store, learning_id=auto, status="ungated")
    # An undecided carve-out still needs review after a passing gate.
    hook = _learning(store, rule_text="turn this into a hook")
    _proposal(store, learning_id=hook, status="ungated", action="convert_to_hook")
    store.close()
    return {"family": fam, "auto": auto, "hook": hook}, path


def test_review_queue_groups_by_lesson_not_by_proposal(tmp_path):
    _ids, path = _queue_fixture(tmp_path)
    out = q.review_queue(_read_only(path), _Cfg())
    assert out["family_count"] == 3, out["families"]
    assert out["count"] == 4
    biggest = max(out["families"], key=lambda f: f["size"])
    assert biggest["size"] == 2
    assert len(biggest["targets"]) == 2


def test_review_queue_uses_the_same_statuses_the_inbox_counts(tmp_path):
    """The inbox badge and its review queue must count the same proposal IDs.

    Both readers use waiting_proposal_ids, including carve-outs."""
    _ids, path = _queue_fixture(tmp_path)
    ro = _read_only(path)
    out = q.review_queue(ro, _Cfg())
    assert tuple(out["queueing_statuses"]) == q.QUEUEING_STATUSES
    assert q.inbox(ro, _Cfg())["count"] == out["count"]
    line = q.status_line(ro, _Cfg(), now_utc=NOW)
    assert f"{out['count']} waiting on you" in line["text"], line["text"]


def test_an_ungated_proposal_is_reachable_in_review(tmp_path):
    ids_, path = _queue_fixture(tmp_path)
    out = q.review_queue(_read_only(path), _Cfg())
    ids = {f["learning_id"] for f in out["families"]}
    assert ids_["auto"] in ids, "an ungated proposal has no review destination"
    assert out["auto_apply_pending"] == 0


def test_an_undecided_hook_proposal_queues_after_a_passing_gate(tmp_path):
    """An undecided hook proposal needs review even after its gate passes."""
    _ids, path = _queue_fixture(tmp_path)
    out = q.review_queue(_read_only(path), _Cfg())
    hooks = [
        p for f in out["families"] for p in f["proposals"]
        if p["action"] == "convert_to_hook"
    ]
    assert len(hooks) == 1
    assert hooks[0]["carve_out"] is True
    assert "hook" in hooks[0]["why_needs_you"].lower()


def test_a_mixed_family_keeps_a_reason_per_proposal(tmp_path):
    """A family with different gate verdicts retains each proposal's own reason."""
    _ids, path = _queue_fixture(tmp_path)
    out = q.review_queue(_read_only(path), _Cfg())
    biggest = max(out["families"], key=lambda f: f["size"])
    reasons = {p["reason_code"] for p in biggest["proposals"]}
    assert reasons == {"inconclusive", "gated_fail"}
    assert len(set(p["why_needs_you"] for p in biggest["proposals"])) == 2


def test_every_queueing_status_has_copy_or_the_lookup_raises(tmp_path):
    """A status the view cannot explain must fail loud, not render blank."""
    for status in q.QUEUEING_STATUSES:
        assert q.WHY_QUEUED[status], f"{status} has no plain-language copy"
    with pytest.raises(KeyError):
        q.why_needs_you("a_status_nobody_wrote_copy_for", action="add", cfg=_Cfg())


def test_every_carve_out_action_has_copy_or_the_lookup_raises(tmp_path):
    class Bad:
        review_queue_actions = ("convert_to_hook", "an_action_with_no_copy")

    with pytest.raises(KeyError):
        q.why_needs_you("pending", action="an_action_with_no_copy", cfg=Bad())


def test_an_empty_queue_is_a_success_state_that_says_so(tmp_path):
    store, path = _writable(tmp_path)
    _run(store, started="2026-08-22T02:30:00Z", stats={"review_only": True})
    store.close()
    out = q.review_queue(_read_only(path), _Cfg())
    assert out["count"] == 0
    assert out["families"] == []
    assert out["empty_state"]
    assert "nothing" in out["empty_state"].lower()


def test_a_status_outside_every_bucket_is_surfaced_not_swallowed(tmp_path):
    store, path = _writable(tmp_path)
    _run(store, started="2026-08-22T02:30:00Z", stats={"review_only": True})
    lid = _learning(store)
    # NOT `superseded`: that is a real status now (store.PROPOSAL_STATUSES) and
    # using it here would make this guard assert the opposite of its name.
    _proposal(store, learning_id=lid, status="teleported")
    store.close()
    out = q.review_queue(_read_only(path), _Cfg())
    assert out["unknown_statuses"] == {"teleported": 1}
    assert out["count"] == 0


def test_every_proposal_status_the_schema_declares_has_exactly_one_bucket():
    """Status buckets must partition store.PROPOSAL_STATUSES in both directions."""
    from self_improve.store import PROPOSAL_STATUSES

    buckets = {
        "queueing": set(q.QUEUEING_STATUSES),
        "auto_apply": set(q.AUTO_APPLY_STATUSES),
        "awaiting_apply": set(q.AWAITING_APPLY_STATUSES),
        "terminal": set(q.TERMINAL_STATUSES),
    }
    union = set().union(*buckets.values())
    assert union - PROPOSAL_STATUSES == set(), (
        f"buckets name statuses the schema does not: {sorted(union - PROPOSAL_STATUSES)}"
    )
    assert PROPOSAL_STATUSES - union == set(), (
        f"the schema declares statuses no bucket claims: {sorted(PROPOSAL_STATUSES - union)}"
    )
    overlaps = [
        (a, b, sorted(buckets[a] & buckets[b]))
        for i, a in enumerate(buckets)
        for b in list(buckets)[i + 1:]
        if buckets[a] & buckets[b]
    ]
    assert overlaps == [], f"a status is in two buckets: {overlaps}"
    assert q.KNOWN_STATUSES == PROPOSAL_STATUSES


def test_the_status_vocabulary_is_not_empty():
    """A partition test over an empty set passes vacuously."""
    from self_improve.store import PROPOSAL_STATUSES

    assert len(PROPOSAL_STATUSES) >= 8, sorted(PROPOSAL_STATUSES)
    assert "superseded" in PROPOSAL_STATUSES
    assert "approved_user" in PROPOSAL_STATUSES


def test_a_decided_carve_out_leaves_the_queue(tmp_path):
    """Exclude decided carve-outs from the review queue.

    A proposal's action stays the same after a decision. Action-based queue
    membership must therefore also exclude terminal statuses.
    """
    store, path = _writable(tmp_path)
    _run(store, started="2026-08-22T02:30:00Z", stats={"review_only": True})
    lid = _learning(store)
    open_hook = _proposal(store, learning_id=lid, status="ungated",
                          action="convert_to_hook", proposal_id="open")
    for status in ("rejected_user", "approved_user", "applied", "rolled_back", "superseded"):
        # Scope is a separate axis: a legacy rejection suppresses its whole lesson.
        _proposal(store, learning_id=_learning(store), status=status, action="convert_to_hook",
                  proposal_id=f"done-{status}")
    store.close()

    ro = _read_only(path)
    waiting = set(q.waiting_proposal_ids(ro, _Cfg()))
    assert waiting == {open_hook}, (
        f"a decided carve-out is still waiting: {sorted(waiting - {open_hook})}"
    )
    assert q.review_queue(ro, _Cfg())["count"] == 1


def test_a_decided_proposal_of_any_shape_leaves_the_queue(tmp_path):
    """The general form, over every status the schema declares."""
    from self_improve.store import PROPOSAL_STATUSES

    store, path = _writable(tmp_path)
    _run(store, started="2026-08-22T02:30:00Z", stats={"review_only": True})
    for status in sorted(PROPOSAL_STATUSES):
        _proposal(store, learning_id=_learning(store), status=status, action="add",
                  proposal_id=f"add-{status}")
    store.close()

    ro = _read_only(path)
    waiting = {i.split("-", 1)[1] for i in q.waiting_proposal_ids(ro, _Cfg())}
    assert waiting == set(q.QUEUEING_STATUSES) | {"gated_pass"}, sorted(waiting)


def test_the_actor_of_an_event_is_decided_by_the_event_not_the_caller():
    """Attribute the decision to the event actor.

    A person invoking the CLI does not turn an automated gate judgment into a
    human decision. Unknown event types must fail instead of guessing an actor."""
    from self_improve.store import ACTOR_FOR_EVENT, actor_for

    machine = {"created", "gated", "superseded", "applied", "held"}
    person = {"approved_user", "approval_cancelled", "rejected_user", "rolled_back"}
    assert set(ACTOR_FOR_EVENT) == machine | person, sorted(ACTOR_FOR_EVENT)
    for event in machine:
        assert actor_for(event) == "auto", event
    for event in person:
        assert actor_for(event) == "user", event
    with pytest.raises(KeyError):
        actor_for("an_event_nobody_classified")


def test_an_orphan_proposal_fails_loud_and_names_what_disagreed(tmp_path):
    """Detect a mismatch when a proposal's learning is absent.

    review_queue joins learnings while waiting_proposal_ids counts proposals.
    The fixture disables foreign-key checks on a raw connection to construct
    the orphan that normal Store writes refuse. The count invariant must still
    detect that corrupted state.
    """
    import sqlite3

    store, path = _writable(tmp_path)
    _run(store, started="2026-08-22T02:30:00Z", stats={"review_only": True})
    lid = _learning(store)
    _proposal(store, learning_id=lid, status="inconclusive", proposal_id="kept")
    store.close()

    raw = sqlite3.connect(path)
    raw.execute("PRAGMA foreign_keys=OFF")
    raw.execute(
        "INSERT INTO proposals (id, learning_id, run_id, target_path, target_kind,"
        " action, diff_unified, status, eval_result_id, applied_at,"
        " snapshot_commit_before, snapshot_commit_after, created_at)"
        " VALUES ('orphan', 'a-learning-that-was-deleted', '', '/x', "
        "'global_claude_md', 'add', '', 'inconclusive', '', '', '', '',"
        " '2026-08-18T08:00:00Z')"
    )
    raw.commit()
    raw.close()

    ro = _read_only(path)
    assert "orphan" in q.waiting_proposal_ids(ro, _Cfg()), (
        "the un-joined counter should still see it; that is the disagreement"
    )
    with pytest.raises(q.DashboardDataError) as excinfo:
        q.review_queue(ro, _Cfg())
    message = str(excinfo.value)
    assert "1 grouped" in message and "2 waiting" in message, message
    assert "review queue" in message, message


def test_the_foreign_key_is_what_actually_prevents_an_orphan(tmp_path):
    """The invariant above is the second line of defence. This is the first."""
    import sqlite3

    store, path = _writable(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        _proposal(store, learning_id="no-such-learning", status="inconclusive")
    store.close()


def test_review_queue_on_a_database_with_no_runs_at_all(tmp_path):
    """The first-install path. `_latest_run_stats` returns None here."""
    store, path = _writable(tmp_path)
    lid = _learning(store)
    _proposal(store, learning_id=lid, status="inconclusive")
    store.close()

    out = q.review_queue(_read_only(path), _Cfg())
    assert out["count"] == 1
    assert out["review_only"] is None, "an unknown posture must be None, never False"
    assert out["families"][0]["lead_reason"]
    assert q.inbox(_read_only(path), _Cfg())["count"] == 1


def test_review_queue_with_no_carve_out_actions_configured(tmp_path):
    """An empty optional list cannot disable mandatory human review."""

    # Inherits _Cfg so it stays a complete stand-in: `_cfg_attr` raises on a
    # missing key, and three hand-rolled config classes drifting from the real
    # Config is the same "two lists that must agree" shape as everywhere else.
    class NoCarveOuts(_Cfg):
        review_queue_actions = ()

    store, path = _writable(tmp_path)
    _run(store, started="2026-08-22T02:30:00Z", stats={"review_only": True})
    lid = _learning(store)
    _proposal(store, learning_id=lid, status="inconclusive", proposal_id="queues")
    _proposal(store, learning_id=lid, status="ungated", action="convert_to_hook",
              proposal_id="mandatory-hook")
    store.close()

    ro = _read_only(path)
    ids = q.waiting_proposal_ids(ro, NoCarveOuts())
    assert set(ids) == {"queues", "mandatory-hook"}, ids
    out = q.review_queue(ro, NoCarveOuts())
    assert out["count"] == 2
    assert set(out["carve_out_actions"]) == {"convert_to_hook", "delete_human_line", "resolve_rollback", "reapply", "recover_rule"}


class _SqlRecorder:
    """A Store stand-in that captures the SQL instead of running it."""

    def __init__(self):
        self.sql = []

    def query(self, sql, params=()):
        self.sql.append(sql)
        return []

    def query_one(self, sql, params=()):
        self.sql.append(sql)
        return None


def test_an_empty_carve_out_list_never_composes_in_with_no_operands():
    """`IN ()` is legal in SQLite and a syntax error in PostgreSQL.

    `store.py` documents this schema as Supabase-portable, so the empty case is
    special-cased to `IN (NULL)`. Two reasons this needs its own test:

    * `ops/sabotage.sh` cannot prove it. Removing the special case leaves every
      test green, because SQLite happily evaluates `a IN ()` as no rows —
      verified: `sqlite_version 3.53.4` returns `[]` rather than raising.
    * `test_no_module_uses_sqlite_only_sql` cannot see it either. That lint
      scans string LITERALS, and this clause is composed at runtime.

    So the guard is on the composed SQL, which is the only place the mistake
    is visible without a PostgreSQL to run it against.
    """

    # Inherits _Cfg so it stays a complete stand-in: `_cfg_attr` raises on a
    # missing key, and three hand-rolled config classes drifting from the real
    # Config is the same "two lists that must agree" shape as everywhere else.
    class NoCarveOuts(_Cfg):
        review_queue_actions = ()

    recorder = _SqlRecorder()
    q.waiting_proposal_ids(recorder, NoCarveOuts())
    assert recorder.sql, "the recorder captured no SQL; this test proves nothing"
    for sql in recorder.sql:
        collapsed = " ".join(sql.split())
        assert "IN ()" not in collapsed, f"empty IN list is a Postgres syntax error: {collapsed}"

    # The populated case must also produce portable SQL.
    class WithCarveOuts:
        review_queue_actions = ("convert_to_hook",)

    recorder2 = _SqlRecorder()
    q.waiting_proposal_ids(recorder2, WithCarveOuts())
    assert recorder2.sql
    assert all("IN ()" not in " ".join(s.split()) for s in recorder2.sql)


def test_review_queue_composes_the_same_clause_and_has_the_same_hazard(tmp_path):
    """`review_queue` builds its own copy of the action clause. Both or neither."""

    # Inherits _Cfg so it stays a complete stand-in: `_cfg_attr` raises on a
    # missing key, and three hand-rolled config classes drifting from the real
    # Config is the same "two lists that must agree" shape as everywhere else.
    class NoCarveOuts(_Cfg):
        review_queue_actions = ()

    store, path = _writable(tmp_path)
    _run(store, started="2026-08-22T02:30:00Z", stats={"review_only": True})
    lid = _learning(store)
    _proposal(store, learning_id=lid, status="inconclusive")
    store.close()

    seen = []
    ro = _read_only(path)
    real = ro.query

    def spy(sql, params=()):
        seen.append(sql)
        return real(sql, params)

    ro.query = spy
    q.review_queue(ro, NoCarveOuts())
    assert seen, "no SQL was captured; this test proves nothing"
    for sql in seen:
        assert "IN ()" not in " ".join(sql.split()), sql


def test_one_list_decides_what_may_be_applied():
    """`apply.py` decides what gets written to the operator's instruction files.

    Three copies of that set existed: `store.AUTO_APPLY_STATUSES`,
    `apply.APPLIABLE_STATUSES`, and an inline tuple in `pipeline`'s apply loop.
    They agreed, which is the only reason nothing had broken. Divergence has a
    bad direction and a worse one: the dashboard calling a proposal
    auto-appliable while `apply` refuses it is confusing, and `apply` writing a
    status the trust model never cleared is a rule reaching a file it should
    not have.
    """
    from self_improve import apply as apply_mod
    from self_improve.store import AUTO_APPLY_STATUSES

    assert apply_mod.APPLIABLE_STATUSES is AUTO_APPLY_STATUSES, (
        "apply.py keeps its own copy of the appliable set"
    )


def test_the_pipeline_apply_loop_has_no_inline_copy_of_that_list():
    """Driving the pipeline cannot distinguish an inline tuple from the import
    while the two agree, so this reads the source — and asserts the scan found
    the real call site first, so it cannot pass vacuously."""
    import ast
    from pathlib import Path

    from self_improve import pipeline as pipeline_mod
    from self_improve.store import AUTO_APPLY_STATUSES

    source = Path(pipeline_mod.__file__).read_text(encoding="utf-8")
    assert "apply_proposal(store, cfg, proposal)" in source, (
        "the apply call site moved; this guard is looking at nothing"
    )
    tree = ast.parse(source)
    literals = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and any(
            isinstance(op, ast.In) for op in node.ops
        ):
            for comparator in node.comparators:
                if isinstance(comparator, ast.Tuple):
                    values = [
                        e.value for e in comparator.elts
                        if isinstance(e, ast.Constant) and isinstance(e.value, str)
                    ]
                    # EXACTLY the appliable set. `HEALTH_STAGES` legitimately
                    # names gate outcomes including `gated_pass`, and flagging
                    # that would be a guard that cries wolf until deleted.
                    if set(values) == set(AUTO_APPLY_STATUSES):
                        literals.append(values)
    assert literals == [], (
        f"pipeline.py inlines the appliable status list: {literals}. "
        "Import APPLIABLE_STATUSES instead."
    )


def test_duplicate_of_holding_rule_text_is_not_a_family(tmp_path):
    """Only a learning reference establishes a family relationship.
    The duplicate_of field can instead contain existing rule text. Such text
    must not become a family ID or a singleton group.
    """
    store, path = _writable(tmp_path)
    a = _learning(store, rule_text="rule A")
    _learning(store, learning_id="dup-1", rule_text="rule B",
              duplicate_of="**Some existing rule text that is not an id at all**")
    _learning(store, learning_id="dup-2", rule_text="rule C",
              duplicate_of="**Another in-force rule, also not an id**")
    store.close()

    out = q.rule_families(_read_only(path))
    assert out["available"] is False, (
        f"claimed families from non-referential text: {out.get('groups')}"
    )
    assert out["groups"] == []
    assert "id" in out["reason"], out["reason"]
    assert out["learnings_with_duplicate_of"] == 2, out
    assert out["duplicate_of_referencing_a_learning"] == 0, out


def test_a_real_learning_reference_still_forms_a_family(tmp_path):
    """The complement: when `duplicate_of` DOES point at a learning, group."""
    store, path = _writable(tmp_path)
    parent = _learning(store, rule_text="the original")
    _learning(store, learning_id="child-1", rule_text="dup 1", duplicate_of=parent)
    _learning(store, learning_id="child-2", rule_text="dup 2", duplicate_of=parent)
    _learning(store, learning_id="noise", rule_text="unrelated",
              duplicate_of="**free text, ignored**")
    store.close()

    out = q.rule_families(_read_only(path))
    assert out["available"] is True, out["reason"]
    assert len(out["groups"]) == 1, out["groups"]
    assert out["groups"][0]["size"] == 2
    assert out["groups"][0]["family_key"] == parent
    assert out["duplicate_of_referencing_a_learning"] == 2


REPO_METHODS = ("gh_repo_id", "remote_url", "git_root")
NON_REPO_METHODS = ("path", "unresolved")


def test_projects_lists_repositories_not_every_directory_an_agent_ran_in(tmp_path):
    """Project identity requires a repository, not merely a session directory.
    Path and unresolved methods are excluded with their counts and reasons.
    """
    store, path = _writable(tmp_path)
    _session(store, file_path="a.jsonl", project_key="github:1", project_key_method="gh_repo_id")
    _session(store, file_path="b.jsonl", project_key="git:no-remote", project_key_method="git_root")
    _session(store, file_path="c.jsonl", project_key="path:/Users/x", project_key_method="path")
    _session(store, file_path="d.jsonl", project_key="path:/", project_key_method="path")
    _session(store, file_path="e.jsonl", project_key="", project_key_method="unresolved")
    store.close()

    out = q.projects(_read_only(path), weigh=lambda *a, **k: {}, isdir=lambda p: False)
    keys = {r["project_key"] for r in out["rows"]}
    assert keys == {"github:1", "git:no-remote"}, sorted(keys)

    excluded = out["not_a_repository"]
    assert excluded["count"] == 3, excluded
    assert excluded["by_method"] == {"path": 2, "unresolved": 1}, excluded
    assert "rev-parse" in excluded["reason"] or "repository" in excluded["reason"], excluded
    assert set(excluded["methods"]) == set(NON_REPO_METHODS)


def test_the_repo_and_non_repo_method_lists_together_cover_every_method():
    """A method in neither list would be silently dropped or silently kept."""
    from self_improve.project_identity import METHODS

    assert set(REPO_METHODS) | set(NON_REPO_METHODS) == set(METHODS), (
        f"project_identity.METHODS is {sorted(METHODS)}; the dashboard classifies "
        f"{sorted(set(REPO_METHODS) | set(NON_REPO_METHODS))}"
    )
    assert set(q.REPO_METHODS) == set(REPO_METHODS)
    assert set(q.NON_REPO_METHODS) == set(NON_REPO_METHODS)


def test_the_excluded_project_counts_reconcile(tmp_path):
    """Count distinct project keys in both the total and the method breakdown.
    Different paths can represent the same excluded key.
    """
    store, path = _writable(tmp_path)
    _session(store, file_path="a.jsonl", project_key="github:1", project_key_method="gh_repo_id")
    # One non-repo project reached by two different paths.
    _session(store, file_path="b.jsonl", project_key="path:/x", project_path="/x",
             project_key_method="path")
    _session(store, file_path="c.jsonl", project_key="path:/x", project_path="/x/sub",
             project_key_method="path")
    store.close()

    out = q.projects(_read_only(path), weigh=lambda *a, **k: {}, isdir=lambda p: False)
    excluded = out["not_a_repository"]
    assert excluded["count"] == 1, excluded
    assert sum(excluded["by_method"].values()) == excluded["count"], excluded



def test_incident_rate_refuses_a_naive_clock(tmp_path):
    """Every other clock-taking query in this module refuses one; so must this."""
    store, path = _writable(tmp_path)
    _session(store, file_path="a.jsonl", project_key="github:1")
    store.close()
    # The SPECIFIC error, not `Exception`. A bare `Exception` here would pass
    # on a TypeError from a renamed argument, which is not what this asserts.
    with pytest.raises(q.DashboardDataError, match="timezone-aware"):
        q.incident_rate(_read_only(path), now_utc=datetime(2026, 9, 4, 12, 0))


def test_review_names_evals_where_no_trial_ran(tmp_path):
    """An eval with only agent errors remains visible in Review."""
    store, path = _writable(tmp_path)
    _run(store, started="2026-08-22T02:30:00Z", stats={"review_only": True})
    lid = _learning(store)
    ran = _eval(store, subject_id="s-ok", verdict="ungated", attempted=3, succeeded=3, failed=0)
    never = _eval(store, subject_id="s-broken", verdict="ungated", attempted=3,
                  succeeded=0, failed=3, taxonomy={"agent_error": 3})
    _proposal(store, learning_id=lid, status="ungated", eval_result_id=ran, proposal_id="ok")
    _proposal(store, learning_id=lid, status="ungated", eval_result_id=never, proposal_id="hollow")
    _proposal(store, learning_id=lid, status="inconclusive", proposal_id="queued")
    store.close()

    out = q.review_queue(_read_only(path), _Cfg())
    assert out["auto_apply_pending"] == 0, out
    assert out["review_no_trial_ran"] == 1, out
    assert "1" in out["auto_apply_note"], out["auto_apply_note"]
    assert "no trial" in out["auto_apply_note"].lower(), out["auto_apply_note"]


def test_the_note_stays_quiet_when_every_eval_actually_ran(tmp_path):
    """Do not add a scary clause that is always present."""
    store, path = _writable(tmp_path)
    _run(store, started="2026-08-22T02:30:00Z", stats={"review_only": True})
    lid = _learning(store)
    ran = _eval(store, subject_id="s-ok", verdict="ungated", attempted=3, succeeded=3, failed=0)
    _proposal(store, learning_id=lid, status="ungated", eval_result_id=ran, proposal_id="ok")
    _proposal(store, learning_id=lid, status="inconclusive", proposal_id="queued")
    store.close()

    out = q.review_queue(_read_only(path), _Cfg())
    assert out["auto_apply_no_trial_ran"] == 0, out
    assert "no trial" not in out["auto_apply_note"].lower(), out["auto_apply_note"]


def test_the_inbox_blocker_line_does_not_dump_a_full_run_id(tmp_path):
    """The badge has a concise count; provenance retains the full run ID."""
    store, path = _writable(tmp_path)
    _run(store, run_id="00000000000040008000000000000001",
         started="2026-01-01T00:00:00Z", stats={})
    lid = _learning(store)
    _proposal(store, learning_id=lid, status="ungated")
    _proposal(store, learning_id=lid, status="inconclusive")
    store.close()

    out = q.inbox(_read_only(path), _Cfg())
    assert out["review_only"] is None, out
    assert "00000000000040008000000000000001" not in out["second_line"], out["second_line"]
    assert out["count"] == 2
    assert out["second_line"] == ""
    # The full id stays available for anyone who needs it, just not inline.
    assert "00000000000040008000000000000001" in out["review_only_source"], out


def test_every_llm_outcome_has_plain_language_copy():
    """The two lists that decide what the operator reads about a failed call.

    `llm.OUTCOMES` is written by the call path; `FAILURE_COPY` is read by the
    failure panel. Nothing connected them, so an outcome added on one side
    rendered as a count with no words next to it — the exact shape
    docs/PRD.md D3 exists to forbid. This is the guard, not a comment asking
    the next person to remember.

    Success outcomes are excluded by name rather than skipped silently: they
    are not failures and must not acquire failure copy.
    """
    from self_improve.llm import OUTCOMES
    from self_improve.dashboard.queries import LLM_SUCCESS_OUTCOMES, failure_copy

    assert set(LLM_SUCCESS_OUTCOMES) <= set(OUTCOMES), (
        "LLM_SUCCESS_OUTCOMES names an outcome the call path cannot produce: "
        f"{sorted(set(LLM_SUCCESS_OUTCOMES) - set(OUTCOMES))}"
    )
    failures = [o for o in OUTCOMES if o not in LLM_SUCCESS_OUTCOMES]
    # A scanner that found nothing would make this test vacuously true.
    assert len(failures) >= 8, f"expected the real failure list, got {failures}"

    missing = [o for o in failures if failure_copy(o) is None]
    assert not missing, (
        "these llm outcomes reach the failure panel with no plain-language "
        f"copy: {missing}"
    )
    for o in failures:
        copy = failure_copy(o)
        assert copy["name"] and copy["explanation"], o
        assert o not in copy["name"], (
            f"the copy for {o!r} shows the raw class name to the operator: "
            f"{copy['name']!r}"
        )


def test_review_names_global_proposals_routed_by_scope_guess(tmp_path):
    """Routing evidence remains visible before a human authorizes a global write."""
    store, path = _writable(tmp_path)
    _run(store, started="2026-08-22T02:30:00Z", stats={"review_only": True})
    one_project = _learning(store, project_count=1)
    many = _learning(store, project_count=9)
    _proposal(store, learning_id=one_project, status="ungated",
              target_kind="global_claude_md", proposal_id="g-guess")
    _proposal(store, learning_id=many, status="ungated",
              target_kind="global_claude_md", proposal_id="g-breadth")
    _proposal(store, learning_id=one_project, status="ungated",
              target_kind="project_agents_md", proposal_id="p-scoped")
    store.close()

    out = q.review_queue(_read_only(path), _Cfg())
    assert out["auto_apply_pending"] == 0, out
    assert out["review_global_by_scope_guess"] == 1, out
    note = out["auto_apply_note"].lower()
    assert "scope_guess" in note, out["auto_apply_note"]
    assert "global" in note, out["auto_apply_note"]


def test_no_global_proposals_adds_nothing_to_the_note(tmp_path):
    """Narrowness: a queue with nothing aimed at the global file stays quiet."""
    store, path = _writable(tmp_path)
    _run(store, started="2026-08-22T02:30:00Z", stats={"review_only": True})
    lid = _learning(store, project_count=1)
    _proposal(store, learning_id=lid, status="ungated",
              target_kind="project_agents_md", proposal_id="p1")
    store.close()

    out = q.review_queue(_read_only(path), _Cfg())
    assert out["auto_apply_global_by_scope_guess"] == 0, out
    assert "scope_guess" not in out["auto_apply_note"]


def test_every_config_key_the_dashboard_reads_exists_on_the_real_config():
    """`_cfg_attr` raises on a missing key — but only at RUNTIME.

    Every test in this file passes a stand-in (`_Cfg`) with its own copy of
    the keys, so a rename in `config.py` leaves the whole suite green and the
    dashboard raising `DashboardDataError` the first time someone opens it.
    Two lists that must agree, with the tests shielding one of them.

    The dependency set is derived from the dashboard's own AST rather than
    listed here, so adding a `_cfg_attr` call is enough to be covered. The
    scan asserts it found something: a scanner that returns nothing makes
    "nothing is missing" vacuously true, which this repo has been bitten by.
    """
    import ast
    import pathlib
    from self_improve.config import Config
    from self_improve.dashboard import queries as _q

    pkg = pathlib.Path(_q.__file__).parent
    names: set[str] = set()
    for src in sorted(pkg.glob("*.py")):
        tree = ast.parse(src.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "_cfg_attr"
                    and len(node.args) >= 2
                    and isinstance(node.args[1], ast.Constant)
                    and isinstance(node.args[1].value, str)):
                names.add(node.args[1].value)

    assert len(names) >= 3, f"the scan found too few keys to be real: {names}"
    assert "review_queue_actions" in names, (
        f"the scan missed a key this file's own tests exercise: {sorted(names)}"
    )

    real = Config(state_dir="/tmp/does-not-need-to-exist")
    missing_on_config = sorted(n for n in names if not hasattr(real, n))
    assert not missing_on_config, (
        "the dashboard reads config keys that do not exist, and every test "
        f"here hides it behind a stand-in: {missing_on_config}"
    )
    missing_on_stub = sorted(n for n in names if not hasattr(_Cfg, n))
    assert not missing_on_stub, (
        "_Cfg is an incomplete stand-in, so tests using it exercise a config "
        f"the product never sees: {missing_on_stub}"
    )


def _eval_row(**over):
    """A real sqlite3.Row, because `eval_story` calls `.keys()` on its argument."""
    import sqlite3

    fields = {
        "id": "e1",
        "verdict": "gated_fail",
        "attempted": 6,
        "succeeded": 0,
        "failed": 6,
        "finished": "2026-09-01T00:00:00Z",
        "error_taxonomy_json": json.dumps({"agent_error": 6}),
        "metrics_json": json.dumps(
            {
                "without": {"attempted": 3, "succeeded": 0, "failed": 3, "errors": {}},
                "with": {"attempted": 3, "succeeded": 0, "failed": 3, "errors": {}},
            }
        ),
    }
    fields.update(over)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cols = ", ".join(fields)
    conn.execute(f"CREATE TABLE e ({cols})")
    conn.execute(
        f"INSERT INTO e VALUES ({', '.join('?' * len(fields))})", tuple(fields.values())
    )
    return conn.execute("SELECT * FROM e").fetchone()


def test_zero_successes_is_not_by_itself_an_agent_error():
    """A zero success count cannot distinguish infrastructure failure from a loss.
    Read the recorded error taxonomy before naming a cause.
    """
    never_ran = q.eval_story(_eval_row(error_taxonomy_json=json.dumps({"agent_error": 6})))
    assert never_ran["shape"] == "no_trial_ran", never_ran
    assert "errored" in never_ran["summary"]

    # Same zero, but the trials ran and a grader judged them.
    judged = q.eval_story(_eval_row(error_taxonomy_json=json.dumps({"graded_fail": 6})))
    assert judged["shape"] != "no_trial_ran", (
        f"a graded failure was reported as an agent error: {judged['summary']}"
    )
    assert "errored" not in judged["summary"], judged["summary"]

    mixed = q.eval_story(
        _eval_row(error_taxonomy_json=json.dumps({"asked_operator": 1, "graded_fail": 5}))
    )
    assert mixed["shape"] != "no_trial_ran", mixed


def test_an_unrecognised_eval_outcome_reports_itself_and_names_no_cause():
    """A taxonomy key nobody wrote copy for must not borrow another key's story."""
    story = q.eval_story(
        _eval_row(error_taxonomy_json=json.dumps({"something_new": 6}))
    )
    assert story["shape"] == "unknown_outcome", story
    assert "something_new" in story["summary"], story["summary"]
    for invented in ("errored", "never happened", "harness"):
        assert invented not in story["summary"], (
            f"the summary claims a cause it cannot observe: {story['summary']}"
        )


def test_a_malformed_eval_taxonomy_fails_loud_and_names_the_row():
    """Reject malformed JSON and wrong-shaped eval taxonomies with their owner ID."""
    with pytest.raises(q.DashboardDataError, match="e1"):
        q.eval_story(_eval_row(error_taxonomy_json="{not json"))
    with pytest.raises(q.DashboardDataError, match="not an object"):
        q.eval_story(_eval_row(error_taxonomy_json='"a string"'))
