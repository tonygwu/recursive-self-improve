"""Tests for the dashboard process and CLI entry point.

The tests check three contracts against the database and HTTP behavior:

1. GET routes use a read-only Store. The declared writer uses migrate=False.
   Neither handle may migrate the schema. A probe migration and database
   readback detect unwanted migration; source checks cover both constructors.
2. Write routes must match WRITE_ROUTES in both directions. HTTP requests also
   check that read routes and static mounts reject write verbs. The dashboard
   records user decisions without importing the instruction-file writer.
3. The CLI reaches the app without opening its own database handle. The test
   drives cli.main with uvicorn.run stubbed and records Store construction.

Every database fixture is temporary. tests/conftest.py guards personal state
and transcript directories from test access.
"""

from __future__ import annotations

import ast
import hashlib
import importlib
import json
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from self_improve import cli
from self_improve import store as store_module
from self_improve.config import Config
from self_improve.dashboard import app as dashboard_app
from self_improve.dashboard import queries
from self_improve.store import Store, new_id

fastapi = pytest.importorskip("fastapi", reason="the dashboard extra is not installed")
from fastapi.testclient import TestClient  # noqa: E402


#: Frozen, timezone-aware, UTC. Two dashboard figures are genuine wall-clock
#: questions ("did it run last night?"), so the clock is injected rather than
#: read, and a naive datetime is refused by `queries`.
FROZEN = datetime(2026, 8, 25, 9, 30, 0, tzinfo=timezone.utc)

API_PATHS = (
    "/api",
    "/api/health",
    "/api/overview",
    "/api/rules",
    "/api/rule-families",
    "/api/projects",
    "/api/incident-rate",
    "/api/project-exposure",
    "/api/project-sessions",
    "/api/project-native-events",
    "/api/project-measurements",
    "/api/review-queue",
    "/api/eval-attempts",
    "/api/eval-results",
    "/api/eval-health",
    "/api/class-evidence",
    "/api/quality-samples",
)

WRITE_VERBS = ("POST", "PUT", "PATCH", "DELETE")

#: Declared HTTP routes that record user intent. Bidirectional guards reject
#: both undeclared write routes and allowlist entries with no matching route.
WRITE_ROUTES = {"/api/proposals/{proposal_id}/decision": {"POST"}, "/api/commands": {"POST"}}

#: Applied by Store._migrate if a handle allows schema migration.
PROBE_MIGRATION = (
    "9999_dashboard_readonly_probe",
    "CREATE TABLE probe_migration_ran (x TEXT);",
)

# Invented counters retain the legacy taxonomy shape, including budget refusals.
_FULL_STATS = {
    "run_id": "run-ok",
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
# Fixtures: a small database with the SHAPE of the live one
# ---------------------------------------------------------------------------


def _fixture_db(tmp_path: Path) -> Path:
    """A migrated state DB. Written with a writable Store, then closed."""
    path = tmp_path / "state.db"
    store = Store(path)
    store.insert(
        "runs",
        {"id": "run-ok", "started": "2026-08-18T00:00:00Z",
         "finished": "2026-08-18T01:00:00Z", "status": "ok",
         "stats_json": json.dumps(_FULL_STATS), "report_path": ""},
    )
    # A second invented run on the same date remains distinct. Its unfinished
    # state and missing stats exercise the corresponding display fallbacks.
    store.insert(
        "runs",
        {"id": "run-stuck", "started": "2026-08-18T00:00:00Z", "finished": "",
         "status": "running", "stats_json": "{}", "report_path": ""},
    )
    store.upsert_session(
        {
            "file_path": "s1", "source": "claude", "session_id": "s1",
            "project_path": "/repos/one", "project_key": "github:1",
            "project_display": "owner/one", "project_key_method": "gh_repo_id",
            "headless": 0, "is_subagent": 0,
            "first_ts": "2026-08-17T00:00:00Z", "last_ts": "2026-08-18T00:00:00Z",
            "mtime": 0.0, "file_size": 0, "bytes_scanned": 0,
            "lines_scanned": 250_000, "malformed_lines": 0, "status": "ok",
            "error": "", "last_scanned_at": "2026-08-18T00:00:00Z",
        }
    )
    for day, count in (("2026-08-16", 3), ("2026-08-17", 2), ("2026-08-18", 1)):
        for index in range(count):
            store.insert(
                "incidents",
                {
                    "id": new_id(), "session_file": "s1", "session_id": "s1",
                    "project_path": "/repos/one", "project_key": "github:1",
                    "ts": f"{day}T0{index}:00:00Z", "signal_type": "frustration",
                    "matched_text": "that is not what I asked for",
                    "window_json": json.dumps(
                        [{"role": "user", "ts": f"{day}T0{index}:00:00Z", "text": "no"}]
                    ),
                    "score": 1.0, "status": "new", "run_id": "run-ok",
                    "created_at": "2026-08-18T09:00:00Z",
                },
            )
    learning_id = new_id()
    store.insert(
        "learnings",
        {
            "id": learning_id, "title": "t", "rule_text": "show the command output",
            "why": "assertions are not evidence", "category": "evidence",
            "scope": "global", "evidence_count": 3, "project_count": 1,
            "projects_json": "[]", "first_seen": "2026-08-17T00:00:00Z",
            "last_seen": "2026-08-18T00:00:00Z", "confidence": 0.6,
            "status": "proposed", "duplicate_of": "",
            "created_at": "2026-08-18T07:00:00Z", "incident_summary": "",
            "source": "claude", "violated_existing_rule": "",
            "path_globs_json": "[]", "primary_project_path": "",
        },
    )
    store.insert(
        "proposals",
        {
            "id": new_id(), "learning_id": learning_id, "run_id": "run-ok",
            "target_path": "/Users/x/.claude/CLAUDE.md",
            "target_kind": "global_claude_md", "action": "add",
            "diff_unified": "", "status": "pending", "eval_result_id": "",
            "applied_at": "", "snapshot_commit_before": "",
            "snapshot_commit_after": "", "created_at": "2026-08-18T08:00:00Z",
        },
    )
    store.close()
    return path


def _static_dir(tmp_path: Path, *, index: bool = True) -> Path:
    """A stand-in SPA. The real static files belong to another agent."""
    directory = tmp_path / "static"
    directory.mkdir()
    if index:
        (directory / "index.html").write_text(
            "<!doctype html><title>fixture spa</title><div id=root></div>",
            encoding="utf-8",
        )
    (directory / "app.js").write_text("export const marker = 'fixture-js';\n", encoding="utf-8")
    (directory / "tokens.css").write_text(":root{--c-bg-canvas:#fff}\n", encoding="utf-8")
    return directory


def _cfg(tmp_path: Path) -> Config:
    return Config(state_dir=str(tmp_path))


def _app(tmp_path: Path, *, index: bool = True, cfg: Config | None = None):
    return dashboard_app.create_app(
        cfg or _cfg(tmp_path),
        static_dir=_static_dir(tmp_path, index=index),
        clock=lambda: FROZEN,
    )


def _record_stores(monkeypatch) -> list[tuple[str, bool]]:
    """Every Store constructed in this process, with its read_only flag."""
    seen: list[tuple[str, bool]] = []
    original = Store.__init__

    def _recording_init(self, db_path, *, read_only=False, migrate=None):
        # Signature must track Store.__init__. A double that silently drops a
        # kwarg turns "the writer opened a migrating handle" into a TypeError
        # nobody reads, or worse, hides it.
        seen.append((str(db_path), read_only))
        original(self, db_path, read_only=read_only, migrate=migrate)

    monkeypatch.setattr(Store, "__init__", _recording_init)
    return seen


def _read_back(db: Path, sql: str) -> list[tuple]:
    """Read the file with an independent mode=ro connection."""
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return list(conn.execute(sql))
    finally:
        conn.close()


def _migration_names(db: Path) -> list[str]:
    return [row[0] for row in _read_back(db, "SELECT name FROM schema_migrations ORDER BY name")]


def _table_names(db: Path) -> list[str]:
    return [
        row[0]
        for row in _read_back(db, "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    ]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _app_ast() -> ast.Module:
    source = Path(dashboard_app.__file__)
    return ast.parse(source.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 1. The store contract
# ---------------------------------------------------------------------------


def test_get_routes_preserve_database_and_use_a_read_only_store(tmp_path, monkeypatch):
    """Keep the database unchanged while GET routes use a read-only Store.

    A probe migration detects a handle that allows schema migration. Schema
    readback, file bytes, modification time, and recorded constructor flags
    together check the GET request contract.
    """
    db = _fixture_db(tmp_path)
    monkeypatch.setattr(
        store_module, "MIGRATIONS", [*store_module.MIGRATIONS, PROBE_MIGRATION]
    )
    migrations_before = _migration_names(db)
    tables_before = _table_names(db)
    sha_before = _sha256(db)
    mtime_before = db.stat().st_mtime_ns
    assert PROBE_MIGRATION[0] not in migrations_before

    constructions = _record_stores(monkeypatch)
    app = _app(tmp_path)
    with TestClient(app) as client:
        for path in API_PATHS:
            assert client.get(path, params={"project_key": "fixture-project"} if path in {"/api/project-exposure", "/api/project-sessions", "/api/project-native-events"} else {}).status_code == 200, path

    # The file first. The recorder below cannot see a handle opened by code
    # this test never patched, and printed text cannot see one at all.
    assert _migration_names(db) == migrations_before
    assert "probe_migration_ran" not in _table_names(db)
    assert _table_names(db) == tables_before
    assert _sha256(db) == sha_before
    assert db.stat().st_mtime_ns == mtime_before
    assert constructions == [(str(db), True)], constructions


def test_exactly_one_store_serves_every_request(tmp_path, monkeypatch):
    """A per-request Store is the same bug wearing a different hat."""
    db = _fixture_db(tmp_path)
    constructions = _record_stores(monkeypatch)
    app = _app(tmp_path)
    with TestClient(app) as client:
        for _ in range(3):
            for path in API_PATHS:
                assert client.get(path, params={"project_key": "fixture-project"} if path in {"/api/project-exposure", "/api/project-sessions", "/api/project-native-events"} else {}).status_code == 200
    assert len(constructions) == 1, constructions
    assert constructions[0] == (str(db), True)


def test_app_source_constructs_only_the_two_intended_stores():
    """Check both Store constructors, including code outside request handling.

    The lifespan reader must set read_only=True, and the intent writer must set
    migrate=False. An unqualified Store could migrate on construction. The
    source guard rejects missing flags and additional Store construction sites.
    """
    calls = [
        node
        for node in ast.walk(_app_ast())
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "Store"
    ]
    assert len(calls) == 2, (
        f"app.py constructs {len(calls)} Stores; it owns exactly two — "
        "the lifespan reader and the V3 writer"
    )
    flags = []
    for call in calls:
        keywords = {kw.arg: kw.value for kw in call.keywords}
        assert keywords, "a Store was constructed with no flags; a plain Store migrates"
        for name, value in keywords.items():
            assert isinstance(value, ast.Constant), f"{name} is not a literal"
            flags.append((name, value.value))
    assert ("read_only", True) in flags, "no read-only Store: the reader is gone"
    assert ("migrate", False) in flags, (
        "no migrate=False Store: the writer can move the schema of the live DB"
    )


def test_the_store_is_closed_when_the_lifespan_ends(tmp_path):
    app = _app(tmp_path)
    _fixture_db(tmp_path)
    with TestClient(app) as client:
        assert client.get("/api/health").json()["read_only"] is True
        held = app.state.store
    assert app.state.store is None
    with pytest.raises(sqlite3.ProgrammingError):
        held.query_one("SELECT 1 AS n")


def test_a_route_without_a_lifespan_says_so_instead_of_opening_a_store(tmp_path, monkeypatch):
    """No lifespan means no Store — and a route must not improvise one."""
    _fixture_db(tmp_path)
    constructions = _record_stores(monkeypatch)
    app = _app(tmp_path)
    client = TestClient(app)  # deliberately NOT used as a context manager
    response = client.get("/api/overview")
    assert response.status_code == 500
    assert response.json()["error"] == "DashboardStartupError"
    assert "lifespan" in response.json()["detail"]
    assert constructions == []


# ---------------------------------------------------------------------------
# 2. Declared HTTP read and intent-write surface
# ---------------------------------------------------------------------------


def test_only_the_declared_intent_write_routes_exist(tmp_path):
    """Match the declared intent-write routes in both directions.

    A route with a write verb outside WRITE_ROUTES fails, and a WRITE_ROUTES
    entry the app does not serve fails too. Deleting an endpoint must not leave
    the allowlist guard passing.
    """
    from fastapi.staticfiles import StaticFiles
    from starlette.routing import Mount

    app = _app(tmp_path)
    read_only_verbs = {"GET", "HEAD", "OPTIONS"}
    found: dict[str, set[str]] = {}
    checked = 0
    for route in app.routes:
        methods = getattr(route, "methods", None)
        if methods is None:
            assert isinstance(route, Mount), f"{route} is neither a route nor a mount"
            assert isinstance(route.app, StaticFiles), (
                f"mount {route.name} serves {type(route.app).__name__}, which may write"
            )
            continue
        checked += 1
        writes = set(methods) - read_only_verbs
        if not writes:
            continue
        assert route.path in WRITE_ROUTES, (
            f"{route.path} exposes {sorted(writes)} and is not a declared write route"
        )
        found.setdefault(route.path, set()).update(writes)
    assert found == WRITE_ROUTES, (
        f"declared write routes {WRITE_ROUTES} but the app serves {found}"
    )
    assert checked >= len(API_PATHS), "the route table shrank; the guard is looking at nothing"


def test_no_write_verb_is_accepted_over_http(tmp_path):
    """The route table can be right while a mount underneath accepts a POST."""
    _fixture_db(tmp_path)
    app = _app(tmp_path)
    with TestClient(app) as client:
        for path in (*API_PATHS, "/", "/app.js", "/api/proposals/abc/approve"):
            for verb in WRITE_VERBS:
                response = client.request(verb, path)
                assert response.status_code in (404, 405), (
                    f"{verb} {path} returned {response.status_code}; "
                    "the only write path is the declared one"
                )


def test_the_one_write_path_accepts_only_its_declared_verb(tmp_path):
    """The complement of the test above: prove the allowlist is not vacuous."""
    _fixture_db(tmp_path)
    pid = _queued_proposal(tmp_path)
    path = next(iter(WRITE_ROUTES)).format(proposal_id=pid)
    with TestClient(_app(tmp_path)) as client:
        for verb in WRITE_VERBS:
            response = client.request(verb, path, json={"decision": "reject"})
            if verb in next(iter(WRITE_ROUTES.values())):
                assert response.status_code == 200, f"{verb} was refused: {response.text}"
            else:
                assert response.status_code in (404, 405), f"{verb} was accepted"


def test_the_dashboard_process_never_imports_apply(tmp_path):
    imported = set()
    for node in ast.walk(_app_ast()):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(f"{'.' * node.level}{node.module or ''}")
    assert not any("apply" in name for name in imported), imported


def test_no_web_framework_is_imported_at_module_level():
    """fastapi is an optional extra, so the module must import without it."""
    module_level = set()
    for node in _app_ast().body:
        if isinstance(node, ast.Import):
            module_level.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            module_level.add(node.module.split(".")[0])
    assert module_level.isdisjoint({"fastapi", "uvicorn", "starlette", "pydantic"}), module_level


def test_no_cross_origin_middleware_is_installed(tmp_path):
    app = _app(tmp_path)
    names = [type(mw.cls).__name__ if hasattr(mw, "cls") else str(mw) for mw in app.user_middleware]
    assert not any("CORS" in str(mw.cls) for mw in app.user_middleware), names


# ---------------------------------------------------------------------------
# 3. The payloads are what queries.py promises
# ---------------------------------------------------------------------------


def _expected(db: Path, cfg: Config, call):
    read_only = Store(db, read_only=True)
    try:
        return json.loads(json.dumps(call(read_only, cfg)))
    finally:
        read_only.close()


def test_every_route_returns_exactly_what_queries_promises(tmp_path):
    db = _fixture_db(tmp_path)
    cfg = _cfg(tmp_path)
    app = _app(tmp_path, cfg=cfg)
    with TestClient(app) as client:
        overview = client.get("/api/overview").json()
        rules = client.get("/api/rules").json()
        families = client.get("/api/rule-families").json()
        rate = client.get("/api/incident-rate").json()

    assert overview == _expected(
        db, cfg, lambda s, c: queries.overview(s, c, now_utc=FROZEN, window_days=14)
    )
    assert rules == _expected(db, cfg, lambda s, c: queries.rules(s))
    assert families == _expected(db, cfg, lambda s, c: queries.rule_families(s))
    assert rate == _expected(db, cfg, lambda s, c: queries.incident_rate(s, now_utc=FROZEN))

    # /api/projects adds one envelope key naming the caps it applied; the rest
    # must be byte-identical to the query with those caps passed through.
    with TestClient(app) as client:
        projects = client.get("/api/projects").json()
    assert projects.pop("request")["weigh_top_n"] == dashboard_app.DEFAULT_WEIGH_TOP_N
    assert projects == _expected(
        db,
        cfg,
        lambda s, c: queries.projects(s, weigh_top_n=dashboard_app.DEFAULT_WEIGH_TOP_N, now_utc=FROZEN),
    )


def test_overview_carries_the_v1_panels_and_dates_from_incident_ts(tmp_path):
    _fixture_db(tmp_path)
    app = _app(tmp_path)
    with TestClient(app) as client:
        payload = client.get("/api/overview").json()

    assert set(payload) == {
        "freshness", "status_line", "grid", "inbox", "backlog", "failures",
        "gate", "confidence", "audit",
    }
    # created_at on every fixture incident is 2026-08-18T09:00:00Z; ts is not.
    # days_stale is counted from the INJECTED clock (2026-08-25), so a handler
    # that read the wall clock instead would report a different number.
    assert payload["freshness"]["as_of"] == "2026-08-18"
    assert payload["freshness"]["days_stale"] == 7
    assert payload["backlog"]["arrivals"]["source_column"] == "incidents.ts"
    assert payload["backlog"]["net_per_day"] is None
    assert payload["backlog"]["capacity_unit"] == "model_calls_per_run"
    assert payload["audit"]["mining"]["window_days"] == 7
    forbidden = ("drain", "nights_to", "eta")
    flat = json.dumps(payload["backlog"]).lower()
    assert not any(word in flat for word in forbidden), flat


def test_the_window_days_knob_reaches_the_grid(tmp_path):
    _fixture_db(tmp_path)
    app = _app(tmp_path)
    with TestClient(app) as client:
        narrow = client.get("/api/overview", params={"window_days": 1}).json()
        wide = client.get("/api/overview", params={"window_days": 30}).json()
    assert len(narrow["grid"]["nights"]) < len(wide["grid"]["nights"])


def test_a_non_numeric_window_is_refused_not_coerced(tmp_path):
    _fixture_db(tmp_path)
    app = _app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/api/overview", params={"window_days": "fortnight"}).status_code == 422


def test_projects_uses_retained_context_and_ignores_legacy_walk_parameters(tmp_path, monkeypatch):
    def forbidden(**kwargs):
        raise AssertionError('Web reader attempted a live context walk')
    monkeypatch.setattr(dashboard_app, '_context_weigher', forbidden)
    _fixture_db(tmp_path)
    app = _app(tmp_path)
    with TestClient(app) as client:
        payload = client.get("/api/projects").json()
    request = payload["request"]
    assert request["weigh_top_n"] == dashboard_app.DEFAULT_WEIGH_TOP_N
    assert request["scan_other_md"] is False
    assert "other_md" in request["cut"]
    assert payload["context_source"] == "recorded_inventory"
    assert payload["context_weight_capped"] is None
    assert payload["rows"][0]["context_weight"]["reason"] == "no_inventory_observations"
    with TestClient(app) as client:
        other = client.get("/api/projects?weigh_top_n=1&other_md=1").json()
    assert other["rows"] == payload["rows"]
    assert other["request"]["scan_other_md"] is False
    assert payload["rows"][0]["project_key"] == "github:1"
    assert payload["rows"][0]["clones"] == 1
    assert payload["rows"][0]["benefit"]["value"] == queries.NOT_COMPUTABLE


def test_a_broken_row_becomes_a_named_500_not_a_zero(tmp_path):
    """Fail loud: a row the data layer refuses to guess about is reported."""
    db = _fixture_db(tmp_path)
    writable = Store(db)
    writable.insert(
        "runs",
        {"id": "run-bad", "started": "2026-08-19T00:00:00Z", "finished": "",
         "status": "ok", "stats_json": "{not json", "report_path": ""},
    )
    writable.close()
    app = _app(tmp_path)
    with TestClient(app) as client:
        response = client.get("/api/overview")
    assert response.status_code == 500
    body = response.json()
    assert body["error"] == "DashboardDataError"
    assert "run-bad" in body["detail"]


# ---------------------------------------------------------------------------
# 4. Startup failures name the artefact and the fix
# ---------------------------------------------------------------------------


def test_a_missing_database_names_the_path_and_the_fix(tmp_path):
    missing = tmp_path / "state.db"
    with pytest.raises(dashboard_app.DashboardStartupError) as excinfo:
        dashboard_app.open_read_only_store(missing)
    message = str(excinfo.value)
    assert str(missing) in message
    assert "selfimprove upgrade-state --database" in message
    assert "--initialize --dry-run" in message


def test_an_unmigrated_database_is_refused_never_migrated(tmp_path):
    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE unrelated (x TEXT)")
    conn.commit()
    conn.close()

    with pytest.raises(dashboard_app.DashboardStartupError) as excinfo:
        dashboard_app.open_read_only_store(db)
    assert "schema_migrations" in str(excinfo.value)
    assert "never migrates" in str(excinfo.value)
    assert "schema_migrations" not in _table_names(db)


def test_a_wal_open_failure_explains_the_shm_file_and_does_not_fall_back(tmp_path, monkeypatch):
    """The WAL trap: a mode=ro open of a WAL database needs state.db-shm."""
    db = _fixture_db(tmp_path)
    attempts: list[bool] = []
    original = Store.__init__

    def _failing_init(self, db_path, *, read_only=False):
        attempts.append(read_only)
        if read_only:
            raise sqlite3.OperationalError("unable to open database file")
        original(self, db_path, read_only=read_only)

    monkeypatch.setattr(Store, "__init__", _failing_init)
    with pytest.raises(dashboard_app.DashboardStartupError) as excinfo:
        dashboard_app.open_read_only_store(db)

    message = str(excinfo.value)
    assert "-shm" in message
    assert "WAL" in message
    assert "will NOT retry with a writable handle" in message
    assert attempts == [True], "a writable open was attempted as a fallback"


def test_a_naive_clock_is_refused(tmp_path):
    _fixture_db(tmp_path)
    app = dashboard_app.create_app(
        _cfg(tmp_path),
        static_dir=_static_dir(tmp_path),
        clock=lambda: datetime(2026, 8, 22, 20, 0, 0),  # naive: local time
    )
    with TestClient(app) as client:
        response = client.get("/api/overview")
    assert response.status_code == 500
    assert "timezone-aware" in response.json()["detail"]


# ---------------------------------------------------------------------------
# 5. The SPA
# ---------------------------------------------------------------------------


def test_the_spa_and_its_assets_are_served_from_the_static_directory(tmp_path):
    _fixture_db(tmp_path)
    app = _app(tmp_path)
    with TestClient(app) as client:
        index = client.get("/")
        assert index.status_code == 200
        assert "fixture spa" in index.text
        assert index.headers["content-type"].startswith("text/html")
        assert client.get("/app.js").text.strip() == "export const marker = 'fixture-js';"
        assert client.get("/tokens.css").status_code == 200
        assert client.get("/does-not-exist.js").status_code == 404


def test_a_missing_index_html_fails_loud_with_the_path(tmp_path):
    _fixture_db(tmp_path)
    app = _app(tmp_path, index=False)
    with TestClient(app) as client:
        response = client.get("/")
    assert response.status_code == 500
    assert str(tmp_path / "static" / "index.html") in response.text
    assert "no fallback page" in response.text


# ---------------------------------------------------------------------------
# 6. The optional extra
# ---------------------------------------------------------------------------


class _BlockDashboardExtra:
    """A meta-path finder that pretends the dashboard extra is not installed."""

    BLOCKED = {"fastapi", "uvicorn", "starlette"}

    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in self.BLOCKED:
            raise ImportError(f"{name} is not installed (blocked by the test)")
        return None


def _block_extra(monkeypatch):
    for name in list(sys.modules):
        if name.split(".")[0] in _BlockDashboardExtra.BLOCKED:
            monkeypatch.delitem(sys.modules, name)
    monkeypatch.setattr(sys, "meta_path", [_BlockDashboardExtra(), *sys.meta_path])


def test_the_module_imports_and_names_the_install_command_without_fastapi(tmp_path, monkeypatch):
    monkeypatch.delitem(sys.modules, "self_improve.dashboard.app", raising=False)
    _block_extra(monkeypatch)
    module = importlib.import_module("self_improve.dashboard.app")

    with pytest.raises(module.DashboardExtraMissing) as excinfo:
        module.create_app(_cfg(tmp_path))
    assert "uv sync --extra dashboard" in str(excinfo.value)

    with pytest.raises(module.DashboardExtraMissing):
        module.serve(_cfg(tmp_path))


# ---------------------------------------------------------------------------
# 7. The CLI entry point
# ---------------------------------------------------------------------------


def _config_toml(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(f'state_dir = "{tmp_path}"\n', encoding="utf-8")
    return path


def test_cli_dashboard_never_constructs_a_writable_store(tmp_path, monkeypatch):
    """main() opens its own Store for most subcommands; this one must not reach it."""
    import uvicorn

    db = _fixture_db(tmp_path)
    monkeypatch.setattr(
        store_module, "MIGRATIONS", [*store_module.MIGRATIONS, PROBE_MIGRATION]
    )
    migrations_before = _migration_names(db)
    served: dict = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: served.update(app=app, **kwargs))
    constructions = _record_stores(monkeypatch)

    code = cli.main(["--config", str(_config_toml(tmp_path)), "dashboard", "--port", "9999"])

    assert code == 0
    assert served["host"] == "127.0.0.1"
    assert served["port"] == 9999
    assert constructions == [], f"the CLI opened a database before serving: {constructions}"
    assert _migration_names(db) == migrations_before

    # The app the CLI handed uvicorn is the real one: drive its lifespan.
    with TestClient(served["app"]) as client:
        health = client.get("/api/health").json()
    assert health["read_only"] is True
    assert health["db_path"] == str(db)
    assert constructions == [(str(db), True)]
    assert _migration_names(db) == migrations_before
    assert "probe_migration_ran" not in _table_names(db)


def test_cli_dashboard_reports_a_missing_extra_without_a_traceback(tmp_path, monkeypatch, capsys):
    _fixture_db(tmp_path)
    monkeypatch.delitem(sys.modules, "self_improve.dashboard.app", raising=False)
    _block_extra(monkeypatch)

    code = cli.main(["--config", str(_config_toml(tmp_path)), "dashboard"])

    captured = capsys.readouterr()
    assert code == 2
    assert "uv sync --extra dashboard" in captured.err
    assert "Traceback" not in captured.err + captured.out


def test_cli_dashboard_refuses_a_non_loopback_bind(tmp_path, monkeypatch, capsys):
    """No auth in front of it, so widening the bind publishes the database."""
    import uvicorn

    _fixture_db(tmp_path)
    monkeypatch.setattr(
        uvicorn, "run", lambda *a, **k: pytest.fail("uvicorn.run was reached")
    )
    code = cli.main(
        ["--config", str(_config_toml(tmp_path)), "dashboard", "--host", "0.0.0.0"]
    )
    captured = capsys.readouterr()
    assert code == 2
    assert "no authentication" in captured.err
    assert "127.0.0.1" in captured.err


def test_cli_dashboard_defaults_to_loopback_and_a_fixed_port(tmp_path, monkeypatch):
    import uvicorn

    _fixture_db(tmp_path)
    served: dict = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: served.update(app=app, **kwargs))
    assert cli.main(["--config", str(_config_toml(tmp_path)), "dashboard"]) == 0
    assert served["host"] == dashboard_app.DEFAULT_HOST == "127.0.0.1"
    assert served["port"] == dashboard_app.DEFAULT_PORT


def test_dashboard_is_not_in_the_read_only_command_allowlist(tmp_path, monkeypatch):
    """The allowlist decides read_only for main()'s OWN Store.

    Adding 'dashboard' there would look like the fix and would not be one: it
    would still construct a second handle on the live database, which the app's
    lifespan already owns.
    """
    source = Path(cli.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    allowlists = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(getattr(t, "id", "") == "READ_ONLY_COMMANDS" for t in node.targets)
    ]
    assert len(allowlists) == 1
    names = {elt.value for elt in ast.walk(allowlists[0].value) if isinstance(elt, ast.Constant)}
    assert "dashboard" not in names


def test_review_queue_route_serves_the_v3_payload(tmp_path):
    """V3's read side. The write side is a separate route and a separate test."""
    _fixture_db(tmp_path)
    app = _app(tmp_path)
    with TestClient(app) as client:
        response = client.get("/api/review-queue")
    assert response.status_code == 200
    body = response.json()
    for key in (
        "families",
        "family_count",
        "count",
        "queueing_statuses",
        "carve_out_actions",
        "auto_apply_pending",
        "unknown_statuses",
        "empty_state",
    ):
        assert key in body, f"/api/review-queue dropped {key}"


def test_review_queue_route_is_listed_in_the_api_index(tmp_path):
    """The index is how a reader discovers the route; a hidden route is a bug."""
    _fixture_db(tmp_path)
    app = _app(tmp_path)
    with TestClient(app) as client:
        index = client.get("/api").json()
    paths = {route["path"] for route in index["routes"]}
    assert "/api/review-queue" in paths, sorted(paths)


# ---------------------------------------------------------------------------
# Compatibility proposal-decision endpoint
# ---------------------------------------------------------------------------

DECISION_PATH = "/api/proposals/{}/decision"


def _approval_body(client, pid, *, note=""):
    from uuid import uuid4
    reviewed = client.get(f"/api/proposals/{pid}/review")
    if reviewed.status_code == 404:
        return {"decision": "approve", "note": note}
    assert reviewed.status_code == 200, reviewed.text
    preview = client.get('/api/review-preview', params={'proposal_ids': pid})
    assert preview.status_code == 200, preview.text
    return {"decision": "approve", "note": note, "revision": reviewed.json()["revision"],
            "preview_revision": preview.json()['revision'], "request_key": str(uuid4())}


def _queued_proposal(tmp_path, *, status="inconclusive", pid="p-queued"):
    """Add one decidable proposal to the fixture DB."""
    store = Store(tmp_path / "state.db")
    target = tmp_path / (pid + '-CLAUDE.md')
    target.write_text('invented existing rule\n')
    from self_improve.propose import make_unified_diff
    if store.query_one("SELECT id FROM learnings WHERE id = ?", ("l-1",)) is None:
      store.insert(
        "learnings",
        {
            "id": "l-1", "title": "t", "rule_text": "always derive the list",
            "why": "because", "category": "c", "scope": "global",
            "evidence_count": 2, "project_count": 1, "projects_json": "[]",
            "first_seen": "2026-08-18T07:00:00Z", "last_seen": "2026-08-18T07:00:00Z",
            "confidence": 0.6, "status": "proposed", "duplicate_of": "",
            "violated_existing_rule": "", "created_at": "2026-08-18T07:00:00Z",
            "primary_project_path": "",
        },
      )
    store.insert(
        "proposals",
        {
            "id": pid, "learning_id": "l-1", "run_id": "", "action": "add",
            "target_path": str(target), "target_kind": "global_claude_md",
            "diff_unified": make_unified_diff('invented existing rule\n', 'invented existing rule\nnew rule\n', str(target)), "status": status, "eval_result_id": "",
            "applied_at": "", "snapshot_commit_before": "",
            "snapshot_commit_after": "", "created_at": "2026-08-18T08:00:00Z",
        },
    )
    store.close()
    return pid


def _rows(tmp_path, sql, args=()):
    store = Store(tmp_path / "state.db", read_only=True)
    try:
        return store.query(sql, args)
    finally:
        store.close()


def test_approving_appends_the_event_and_moves_the_status(tmp_path):
    _fixture_db(tmp_path)
    pid = _queued_proposal(tmp_path)
    with TestClient(_app(tmp_path)) as client:
        response = client.post(DECISION_PATH.format(pid),
                               json=_approval_body(client, pid, note='looks right'))
    assert response.status_code == 200, response.text
    events = _rows(tmp_path, "SELECT event, actor, note FROM proposal_events "
                             "WHERE proposal_id = ?", (pid,))
    assert [e["event"] for e in events] == ["approved_user"]
    assert events[0]["actor"] == "user"
    assert events[0]["note"] == "looks right"
    status = _rows(tmp_path, "SELECT status FROM proposals WHERE id = ?", (pid,))
    assert status[0]["status"] == "approved_user"


def test_rejecting_appends_the_event_and_moves_the_status(tmp_path):
    _fixture_db(tmp_path)
    pid = _queued_proposal(tmp_path)
    with TestClient(_app(tmp_path)) as client:
        response = client.post(DECISION_PATH.format(pid), json={"decision": "reject"})
    assert response.status_code == 200, response.text
    events = _rows(tmp_path, "SELECT event FROM proposal_events WHERE proposal_id = ?", (pid,))
    assert [e["event"] for e in events] == ["rejected_user"]
    status = _rows(tmp_path, "SELECT status FROM proposals WHERE id = ?", (pid,))
    assert status[0]["status"] == "rejected_user"


def test_an_unknown_decision_word_is_refused_and_writes_nothing(tmp_path):
    _fixture_db(tmp_path)
    pid = _queued_proposal(tmp_path)
    with TestClient(_app(tmp_path)) as client:
        response = client.post(DECISION_PATH.format(pid), json={"decision": "maybe"})
    assert response.status_code == 400, response.text
    assert _rows(tmp_path, "SELECT id FROM proposal_events WHERE proposal_id = ?", (pid,)) == []
    status = _rows(tmp_path, "SELECT status FROM proposals WHERE id = ?", (pid,))
    assert status[0]["status"] == "inconclusive", "a refused decision still moved the row"


def test_an_unknown_proposal_is_404_and_writes_nothing(tmp_path):
    _fixture_db(tmp_path)
    with TestClient(_app(tmp_path)) as client:
        response = client.post(DECISION_PATH.format("no-such-id"),
                               json=_approval_body(client, "no-such-id", note=''))
    assert response.status_code == 404, response.text
    assert _rows(tmp_path, "SELECT id FROM proposal_events") == []


def test_a_proposal_that_is_not_waiting_cannot_be_decided(tmp_path):
    """Deciding an already-applied rule would record a decision nobody made."""
    _fixture_db(tmp_path)
    pid = _queued_proposal(tmp_path, status="applied", pid="p-applied")
    with TestClient(_app(tmp_path)) as client:
        response = client.post(DECISION_PATH.format(pid), json=_approval_body(client, pid, note=''))
    assert response.status_code == 409, response.text
    assert _rows(tmp_path, "SELECT id FROM proposal_events WHERE proposal_id = ?", (pid,)) == []


def test_deciding_twice_is_refused_rather_than_double_recorded(tmp_path):
    _fixture_db(tmp_path)
    pid = _queued_proposal(tmp_path)
    with TestClient(_app(tmp_path)) as client:
        assert client.post(DECISION_PATH.format(pid), json=_approval_body(client, pid, note='')).status_code == 200
        second = client.post(DECISION_PATH.format(pid), json={"decision": "reject"})
    assert second.status_code == 409, second.text
    events = _rows(tmp_path, "SELECT event FROM proposal_events WHERE proposal_id = ?", (pid,))
    assert [e["event"] for e in events] == ["approved_user"]


def test_the_writer_never_opens_a_migrating_handle(tmp_path, monkeypatch):
    """The whole reason Store gained migrate=False. Proven, not asserted."""
    _fixture_db(tmp_path)
    pid = _queued_proposal(tmp_path)
    import self_improve.store as store_mod
    monkeypatch.setattr(store_mod, "MIGRATIONS",
                        list(store_mod.MIGRATIONS) + [PROBE_MIGRATION])
    with TestClient(_app(tmp_path)) as client:
        assert client.post(DECISION_PATH.format(pid), json=_approval_body(client, pid, note='')).status_code == 200
    applied = _rows(tmp_path, "SELECT name FROM schema_migrations WHERE name = ?",
                    (PROBE_MIGRATION[0],))
    assert applied == [], "the write endpoint migrated the operator's database"


def test_the_decision_route_rejects_every_other_verb(tmp_path):
    _fixture_db(tmp_path)
    pid = _queued_proposal(tmp_path)
    with TestClient(_app(tmp_path)) as client:
        for verb in ("GET", "PUT", "PATCH", "DELETE"):
            response = client.request(verb, DECISION_PATH.format(pid))
            assert response.status_code in (404, 405), f"{verb} was accepted"


def test_a_carve_out_card_can_actually_be_decided(tmp_path):
    """V3 renders review-only actions regardless of their gate verdict.
    The decision endpoint must accept every action that its queue renders.
    """
    _fixture_db(tmp_path)
    pid = _queued_proposal(tmp_path, status="ungated", pid="p-hook")
    store = Store(tmp_path / "state.db", migrate=False)
    store.update("proposals", "id", pid, {"action": "convert_to_hook"})
    store.commit()
    store.close()

    with TestClient(_app(tmp_path)) as client:
        shown = client.get("/api/review-queue").json()
        ids = {p["id"] for f in shown["families"] for p in f["proposals"]}
        assert pid in ids, "the queue does not show the carve-out; fixture is wrong"
        response = client.post(DECISION_PATH.format(pid), json={"decision": "reject"})
    assert response.status_code == 200, (
        f"the queue shows this card but the endpoint answered {response.status_code}"
    )
    assert _rows(tmp_path, "SELECT status FROM proposals WHERE id = ?", (pid,))[0][
        "status"
    ] == "rejected_user"


def test_everything_the_queue_shows_is_decidable(tmp_path):
    """The general form: no card may exist that the write endpoint refuses."""
    _fixture_db(tmp_path)
    _queued_proposal(tmp_path, status="inconclusive", pid="p-a")
    hook = _queued_proposal(tmp_path, status="ungated", pid="p-b")
    store = Store(tmp_path / "state.db", migrate=False)
    store.update("proposals", "id", hook, {"action": "convert_to_hook"})
    store.commit()
    store.close()

    with TestClient(_app(tmp_path)) as client:
        shown = client.get("/api/review-queue").json()
        ids = [p["id"] for f in shown["families"] for p in f["proposals"]]
        # Not an exact count: _fixture_db seeds its own queued proposal. What
        # must hold is that both shapes are present, so the loop below is not
        # asserting over an empty or one-sided list.
        assert {"p-a", "p-b"} <= set(ids), ids
        # A legacy rejection is lesson-wide, so siblings can disappear together.
        # Decide the current queue and prove strict progress to its terminal state.
        while ids:
            response = client.post(DECISION_PATH.format(ids[0]), json={"decision": "reject"})
            assert response.status_code == 200, response.text
            current = client.get("/api/review-queue").json()
            remaining = [p['id'] for f in current['families'] for p in f['proposals']]
            assert set(remaining) < set(ids), (ids, remaining)
            ids = remaining
        assert current['count'] == 0


def test_a_second_decision_is_refused_and_leaves_one_event(tmp_path):
    """Serial double-decide. Named for what it tests.

    An earlier version of this test was called "two concurrent decisions" and
    tested nothing of the sort — `TestClient` calls are sequential, so the
    second request reads AFTER the first has committed and is refused by the
    READ.

    The genuinely interleaved case — decided between this request's SELECT and
    its UPDATE — turns out to be prevented by SQLite itself: the handler's
    connection holds the database, so a competing writer blocks on
    `busy_timeout` rather than slipping in. An attempt to write that test got
    `sqlite3.OperationalError: database is locked`, which is the proof. The
    UPDATE carries its precondition anyway, as defence in depth and because a
    Postgres port (store.py's stated direction) would not lock the same way.
    """
    _fixture_db(tmp_path)
    pid = _queued_proposal(tmp_path)
    app = _app(tmp_path)

    with TestClient(app) as client:
        first = client.post(DECISION_PATH.format(pid), json=_approval_body(client, pid, note=''))
        # Second request arrives having "read" the same pre-decision state; the
        # write must be what refuses it, not the earlier read.
        second = client.post(DECISION_PATH.format(pid), json=_approval_body(client, pid, note=''))

    assert first.status_code == 200, first.text
    assert second.status_code == 409, second.text
    events = _rows(tmp_path, "SELECT event FROM proposal_events WHERE proposal_id = ?", (pid,))
    assert [e["event"] for e in events] == ["approved_user"], events


def test_a_row_decided_between_requests_is_refused(tmp_path):
    """Someone else decided it after this request would have read it."""
    _fixture_db(tmp_path)
    pid = _queued_proposal(tmp_path)
    store = Store(tmp_path / "state.db", migrate=False)
    try:
        # Simulate the row having been decided by someone else in between.
        store.update("proposals", "id", pid, {"status": "approved_user"})
        store.commit()
    finally:
        store.close()

    with TestClient(_app(tmp_path)) as client:
        response = client.post(DECISION_PATH.format(pid), json={"decision": "reject"})
    assert response.status_code == 409, response.text
    assert _rows(tmp_path, "SELECT id FROM proposal_events WHERE proposal_id = ?", (pid,)) == []


def test_rejecting_blocks_re_proposal_of_that_lesson(tmp_path):
    """PRD J2 step 4, the clause P1 exists to satisfy.

    "Rejecting records `rejected_user`, which permanently blocks re-proposal of
    that rule." Setting `proposals.status` alone does not do that. Both
    mechanisms that block a lesson key on the LEARNING:

    - `miner.py:522` dismisses a new incident that duplicates a learning whose
      status is `rejected`
    - `pipeline.py` builds `rejected_vecs` from `learnings WHERE status =
      'rejected'`, under a comment reading "rules the user previously rejected"

    Nothing set that on a user rejection, because until V3 there was no way for
    a user to reject anything. The pool was designed for a producer that did
    not exist yet.
    """
    _fixture_db(tmp_path)
    pid = _queued_proposal(tmp_path)
    with TestClient(_app(tmp_path)) as client:
        assert client.post(DECISION_PATH.format(pid), json={"decision": "reject"}).status_code == 200

    learning = _rows(tmp_path, "SELECT status FROM learnings WHERE id = ?", ("l-1",))
    assert learning[0]["status"] == "rejected", (
        "the lesson is not in the rejected pool, so the miner will propose it again"
    )


def test_approving_does_not_touch_the_learning_status(tmp_path):
    """Only rejection blocks. Approving must leave the lesson alive."""
    _fixture_db(tmp_path)
    pid = _queued_proposal(tmp_path)
    before = _rows(tmp_path, "SELECT status FROM learnings WHERE id = ?", ("l-1",))[0]["status"]
    with TestClient(_app(tmp_path)) as client:
        assert client.post(DECISION_PATH.format(pid), json=_approval_body(client, pid, note='')).status_code == 200
    after = _rows(tmp_path, "SELECT status FROM learnings WHERE id = ?", ("l-1",))[0]["status"]
    assert after == before, f"approving changed the learning status {before} -> {after}"


def test_the_conditional_write_is_reachable_and_does_not_invent_a_racer(tmp_path, monkeypatch):
    """Reach the conditional write guard by making the earlier queue check stale.
    The second decision must fail without recording another event. Its message
    must describe the stale state without inventing a concurrent actor.
    """
    _fixture_db(tmp_path)
    pid = _queued_proposal(tmp_path)
    with TestClient(_app(tmp_path)) as client:
        assert client.post(DECISION_PATH.format(pid), json=_approval_body(client, pid, note='')).status_code == 200
        # Layer 1 now answers "still waiting" even though it is not.
        monkeypatch.setattr(
            "self_improve.dashboard.queries.waiting_proposal_ids",
            lambda *a, **k: [pid],
        )
        second = client.post(DECISION_PATH.format(pid), json={"decision": "reject"})

    assert second.status_code == 409, second.text
    detail = second.json()["detail"]
    for invented in ("someone else", "another user", "raced"):
        assert invented not in detail.lower(), (
            f"the fallback claims a cause it cannot observe: {detail!r}"
        )
    assert "no longer" in detail.lower() or "not in a decidable" in detail.lower(), detail
    events = _rows(tmp_path, "SELECT event FROM proposal_events WHERE proposal_id = ?", (pid,))
    assert [e["event"] for e in events] == ["approved_user"], (
        "the second decision was recorded despite the 409"
    )


NIGHTLY_MODULES = (
    "self_improve.pipeline", "self_improve.report", "self_improve.store",
    "self_improve.llm", "self_improve.miner", "self_improve.scan",
    "self_improve.apply", "self_improve.routing", "self_improve.cli",
)


def test_the_nightly_s_own_modules_import_with_no_web_server_installed():
    """The invariant AGENTS.md states, which nothing checked.

    "fastapi/uvicorn are an OPTIONAL extra on purpose: the nightly runs
    headless and has no business carrying a web server, and the package must
    import without them."

    The guard above covers `dashboard.app`. It does not cover the modules the
    NIGHTLY imports, which is where the rule bites: one module-level
    `from .dashboard import ...` in `pipeline.py` would make every headless
    run depend on a web framework, and every test here would still pass
    because this environment has the extra installed.

    Runs in a SUBPROCESS, for two reasons learned by getting it wrong. An
    in-process version using `monkeypatch.delitem(sys.modules, ...)` (a) did
    not bite, because `self_improve.dashboard.app` stayed cached from earlier
    tests so the blocked import never fired, and (b) broke three unrelated
    tests, which had imported those modules and kept references that the
    re-import invalidated. A fresh interpreter has neither problem.
    """
    probe = (
        "import sys, importlib\n"
        # find_spec, not find_module: the legacy protocol was REMOVED in
        # Python 3.12, so a find_module blocker is silently inert here and
        # the probe would pass having blocked nothing. The BLOCKER-INERT
        # check below exists because that is exactly what happened first.
        "class Block:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] in ('fastapi','uvicorn','starlette'):\n"
        "            raise ImportError(name + ' blocked')\n"
        "        return None\n"
        "sys.meta_path.insert(0, Block())\n"
        "try:\n"
        "    importlib.import_module('fastapi')\n"
        "except ImportError:\n"
        "    pass\n"
        "else:\n"
        "    raise SystemExit('BLOCKER-INERT')\n"
        f"for m in {list(NIGHTLY_MODULES)!r}:\n"
        "    importlib.import_module(m)\n"
        "print('OK')\n"
    )
    proc = subprocess.run([sys.executable, "-c", probe],
                          capture_output=True, text=True, timeout=120)
    assert "BLOCKER-INERT" not in proc.stdout + proc.stderr, (
        "the import blocker did not block fastapi, so this test proves nothing"
    )
    assert proc.returncode == 0 and "OK" in proc.stdout, (
        "a module the nightly imports needs the dashboard extra, so a headless "
        f"run would need a web server:\n{proc.stderr[-1500:]}"
    )


def test_cli_install_launchd_never_touches_the_database(tmp_path, monkeypatch):
    """Installing a plist must not construct or migrate a database."""
    from self_improve import launchd as launchd_mod

    db = _fixture_db(tmp_path)
    monkeypatch.setattr(
        store_module, "MIGRATIONS", [*store_module.MIGRATIONS, PROBE_MIGRATION]
    )
    migrations_before = _migration_names(db)
    monkeypatch.setattr(launchd_mod, "install", lambda cfg: "installed")
    constructions = _record_stores(monkeypatch)

    code = cli.main(["--config", str(_config_toml(tmp_path)), "install-launchd"])

    assert code == 0
    assert constructions == [], (
        f"install-launchd opened a database it never reads: {constructions}"
    )
    assert _migration_names(db) == migrations_before
    assert "probe_migration_ran" not in _table_names(db)


def test_serving_a_copy_writes_to_the_copy_and_never_to_the_live_database(tmp_path):
    """The db_path override must redirect readers and decision writers together.
    Two invented databases make a write to the wrong one observable.
    """
    live_dir = tmp_path / "live"
    copy_dir = tmp_path / "copy"
    live_dir.mkdir()
    copy_dir.mkdir()

    _fixture_db(live_dir)
    pid = _queued_proposal(live_dir)
    shutil.copy2(live_dir / "state.db", copy_dir / "state.db")

    app = dashboard_app.create_app(
        Config(state_dir=str(live_dir)),
        static_dir=_static_dir(tmp_path),
        clock=lambda: FROZEN,
        db_path=copy_dir / "state.db",
    )
    with TestClient(app) as client:
        assert client.post(
            DECISION_PATH.format(pid), json=_approval_body(client, pid, note='')
        ).status_code == 200

    def status_in(db: Path) -> str:
        store = Store(db, migrate=False)
        try:
            return store.query_one(
                "SELECT status FROM proposals WHERE id = ?", (pid,)
            )["status"]
        finally:
            store.close()

    assert status_in(copy_dir / "state.db") == "approved_user", (
        "the decision did not reach the database being served"
    )
    assert status_in(live_dir / "state.db") == "inconclusive", (
        "the decision reached the LIVE database while a copy was being served"
    )


@pytest.mark.parametrize("status", ["gated_pass", "ungated"])
def test_default_off_cards_can_be_decided_until_review_is_empty(tmp_path, status):
    """A class-disabled passing card must not fail the endpoint's second guard."""
    _fixture_db(tmp_path)
    # Every success case reviews a real invented target, including the default
    # read-only fixture row that previously had no executable diff.
    from contextlib import closing
    from self_improve.propose import make_unified_diff
    with closing(Store(tmp_path / 'state.db')) as store:
        for row in store.query('SELECT id FROM proposals'):
            target = tmp_path / (row['id'] + '.md')
            target.write_text('invented existing rule\n')
            store.update('proposals', 'id', row['id'], {'target_path': str(target),
                'diff_unified': make_unified_diff('invented existing rule\n', 'invented existing rule\nnew rule\n', str(target))})
        store.commit()
    pid = _queued_proposal(tmp_path, status=status, pid="p-policy")
    with TestClient(_app(tmp_path)) as client:
        shown = client.get("/api/review-queue").json()
        assert pid in {p["id"] for f in shown["families"] for p in f["proposals"]}
        response = client.post(DECISION_PATH.format(pid), json=_approval_body(client, pid, note=''))
        assert response.status_code == 200, response.text
        remaining = client.get("/api/review-queue").json()
        assert pid not in {p["id"] for f in remaining["families"] for p in f["proposals"]}
        for family in remaining["families"]:
            for proposal in family["proposals"]:
                assert client.post(DECISION_PATH.format(proposal["id"]), json=_approval_body(client, proposal["id"], note='')).status_code == 200
        assert client.get("/api/review-queue").json()["count"] == 0
