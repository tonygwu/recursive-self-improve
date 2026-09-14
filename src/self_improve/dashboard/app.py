"""The dashboard process: FastAPI, nonmigrating Stores, explicit intent routes.

The lifespan reader uses ``Store(read_only=True)``. Request writers use
``Store(migrate=False)`` and the same resolved database, including when serving
a copy. Queries do not construct independent Store handles.

``POST /api/commands`` records exact reviewed revisions and typed intent.
``POST /api/proposals/{id}/decision`` supports compatibility decisions and
lesson-wide rejection. The worker performs file delivery; these endpoints do not
run models or apply instruction files. Tests reconcile the write-route allowlist.

FastAPI remains optional. Import it only where needed and report a missing extra
with the install command. Bind to loopback because the pages contain private
source evidence and have no authentication. Inject an aware UTC clock for
wall-time questions such as data freshness.

Async route handlers keep SQLite use on the connection's event-loop thread.
Slow queries still block that loop, so expensive filesystem walks have explicit
caps and report omissions. Project context weight covers the selected top-N
repositories; a repo-wide Markdown walk requires an explicit request.

Serialize through JSONResponse so unexpected objects fail instead of being
coerced. The browser filters the already-loaded rule rows; there is no server
embedding-search endpoint.
"""

from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from ..store import Store, actor_for, new_id
from . import queries

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from fastapi import FastAPI

__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "DashboardExtraMissing",
    "DashboardStartupError",
    "INSTALL_HINT",
    "create_app",
    "serve",
]

#: Loopback only. There is no authentication and the payloads carry redacted
#: transcript excerpts, so binding anywhere else would publish them.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

#: Hosts this process will bind. A name is resolved before the check, so
#: "localhost" is accepted only while it resolves to a loopback address.
_LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1", "0:0:0:0:0:0:0:1"})

#: The SPA. Another agent owns these files; this module only serves them.
STATIC_DIR = Path(__file__).resolve().parent / "static"
INDEX_FILENAME = "index.html"

#: Repos whose context weight is measured per request, ranked by sessions.
#: `queries.projects` reports the skipped count and the reason.
DEFAULT_WEIGH_TOP_N = 25

#: Nights of the run x stage grid returned by default. `None` means every
#: night in the data.
DEFAULT_WINDOW_DAYS = 14

INSTALL_HINT = (
    "The dashboard extra is not installed. Install it with:\n"
    "    uv sync --extra dashboard\n"
    "It is deliberately optional: the launchd nightly runs headless and has no "
    "business carrying a web server."
)


class DashboardExtraMissing(RuntimeError):
    """fastapi/uvicorn are not installed. Carries the install command."""


class DashboardStartupError(RuntimeError):
    """The process cannot start. Carries what is wrong and how to fix it."""


# ---------------------------------------------------------------------------
# Optional-extra imports
# ---------------------------------------------------------------------------


def _require_fastapi():
    """Import fastapi, or raise with the install command."""
    try:
        import fastapi  # noqa: PLC0415
        from fastapi import responses, staticfiles  # noqa: PLC0415
    except ImportError as exc:
        raise DashboardExtraMissing(f"{INSTALL_HINT}\n(import failed: {exc})") from exc
    return fastapi, responses, staticfiles


def _require_uvicorn():
    try:
        import uvicorn  # noqa: PLC0415
    except ImportError as exc:
        raise DashboardExtraMissing(f"{INSTALL_HINT}\n(import failed: {exc})") from exc
    return uvicorn


# ---------------------------------------------------------------------------
# The lifespan reader
# ---------------------------------------------------------------------------


def _sidecar_report(db_path: Path) -> str:
    lines = []
    for suffix in ("-wal", "-shm"):
        side = db_path.with_name(db_path.name + suffix)
        if side.exists():
            lines.append(f"    {side}: present ({side.stat().st_size} bytes)")
        else:
            lines.append(f"    {side}: absent")
    return "\n".join(lines)


def open_read_only_store(db_path) -> Store:
    """Open the lifespan reader without a writable fallback.

    A failure names the artefact, the sidecar files and the fix. It never
    retries writable: ordinary writable construction can migrate the database.
    """
    path = Path(db_path)
    if not path.exists():
        raise DashboardStartupError(
            f"No state database at {path}.\n"
            "  The dashboard only reads; it never creates or migrates the schema.\n"
            "  Fix: run the miner once to create it, e.g.\n"
            "      uv run selfimprove run --dry-run"
        )
    try:
        store = Store(path, read_only=True)
    except sqlite3.OperationalError as exc:
        raise DashboardStartupError(_open_failure_message(path, exc)) from exc

    # sqlite3.connect is lazy: a WAL database that cannot build its shared
    # memory file fails on the FIRST READ, not on connect. Read now, so the
    # failure arrives at startup with a diagnosis instead of inside a request.
    try:
        row = store.query_one("SELECT COUNT(*) AS n FROM schema_migrations")
    except sqlite3.OperationalError as exc:
        store.close()
        if "no such table" in str(exc):
            raise DashboardStartupError(
                f"The state database at {path} has no schema_migrations table, so it "
                "has never been migrated.\n"
                "  The dashboard never migrates a database: that is a write, and a "
                "read-only view has no business deciding when the schema moves.\n"
                "  Fix: run the miner once, e.g.\n"
                "      uv run selfimprove run --dry-run"
            ) from exc
        raise DashboardStartupError(_open_failure_message(path, exc)) from exc
    store.migrations_applied = int((row or {}).get("n") or 0)
    return store


def _open_failure_message(path: Path, exc: Exception) -> str:
    return (
        f"Cannot open the state database read-only: {type(exc).__name__}: {exc}\n"
        f"    path: {path} ({path.stat().st_size} bytes)\n"
        f"{_sidecar_report(path)}\n"
        "  The database runs in WAL mode, and a mode=ro open still needs the -shm "
        "shared-memory file. SQLite creates that file in the database's own "
        "directory, so the user running this server must own the database and be "
        "able to write that directory.\n"
        "  Fixes, in order of preference:\n"
        f"    1. run the dashboard as the user who owns {path};\n"
        f"    2. make {path.parent} writable by this user;\n"
        f"    3. take the database out of WAL as its owner:\n"
        f"       sqlite3 {path} 'PRAGMA journal_mode=DELETE;'\n"
        "  This process will NOT retry with a writable handle. Constructing a "
        "writable Store migrates the live database (docs/RUNBOOK.md)."
    )


# ---------------------------------------------------------------------------
# Host policy
# ---------------------------------------------------------------------------


def _require_loopback(host: str) -> str:
    """Refuse a non-loopback bind. There is no auth to put in front of it."""
    candidate = (host or "").strip()
    if candidate in _LOOPBACK_NAMES:
        return candidate
    raise DashboardStartupError(
        f"Refusing to bind {host!r}: the dashboard has no authentication and "
        "serves redacted transcript excerpts, learning text and repository "
        "paths from the state database.\n"
        f"  Allowed hosts: {', '.join(sorted(_LOOPBACK_NAMES))}.\n"
        "  If another machine needs this view, put it behind something that can "
        "authenticate; do not widen the bind."
    )


# ---------------------------------------------------------------------------
# The app
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def create_app(cfg, *, static_dir=None, clock=None, db_path=None) -> "FastAPI":
    """Build local views and explicit nonmigrating intent routes.

    ``cfg`` supplies the state directory and the mining knobs the backlog panel
    reads. The keyword arguments exist so a test can drive the real app against
    a fixture database, a fixture SPA directory and a frozen clock; the CLI
    passes none of them.
    """
    fastapi, responses, staticfiles = _require_fastapi()

    resolved_db = Path(db_path) if db_path else Path(cfg.state_path("state.db"))
    resolved_static = Path(static_dir) if static_dir else STATIC_DIR
    tick = clock or _utc_now

    @asynccontextmanager
    async def lifespan(app):
        # One lifespan reader serves the queries. Intent routes use a separate
        # short-lived writer over the same resolved database, without migration.
        store = open_read_only_store(resolved_db)
        app.state.store = store
        try:
            yield
        finally:
            app.state.store = None
            store.close()

    app = fastapi.FastAPI(
        title="self-improve dashboard",
        description=(
            "Local views and durable review decisions. The dashboard records "
            "intent; a separate worker delivers instruction edits."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.cfg = cfg
    app.state.clock = tick
    app.state.db_path = str(resolved_db)
    app.state.static_dir = str(resolved_static)
    app.state.store = None

    JSONResponse = responses.JSONResponse
    from .. import commands

    def _json(payload: dict):
        # json.dumps semantics on purpose: an unexpected type raises here
        # rather than being coerced into something plausible.
        return JSONResponse(content=payload)

    def store():
        """The lifespan's Store. Never opens one; says so if it is missing.

        Read handlers obtain the lifespan reader through this closure.
        Intent handlers obtain a separate writer through ``writer_store``.
        """
        held = app.state.store
        if held is None:
            raise DashboardStartupError(
                "The read-only Store is missing: this app's lifespan did not "
                "run. Serve it with uvicorn, or drive it with "
                "`with TestClient(app) as client:`. A route never opens its own."
            )
        return held

    def writer_store():
        """A short-lived writer for intent endpoints; never migrates."""
        return Store(resolved_db, migrate=False)

    def now():
        """The injected clock, refused unless it is timezone-aware UTC."""
        reading = app.state.clock()
        if not isinstance(reading, datetime) or reading.tzinfo is None:
            raise DashboardStartupError(
                f"the injected clock returned {reading!r}; it must return a "
                "timezone-aware datetime, because a naive one is local time "
                "and every date in the database is UTC"
            )
        return reading.astimezone(timezone.utc)

    @app.exception_handler(queries.DashboardDataError)
    async def _data_error(request, exc):  # noqa: ANN001
        return JSONResponse(
            status_code=500,
            content={
                "error": "DashboardDataError",
                "detail": str(exc),
                "meaning": (
                    "a row in the state database has a shape the dashboard "
                    "refuses to guess about; it is reported rather than "
                    "rendered as a zero"
                ),
            },
        )

    @app.exception_handler(DashboardStartupError)
    async def _startup_error(request, exc):  # noqa: ANN001
        return JSONResponse(
            status_code=500,
            content={"error": "DashboardStartupError", "detail": str(exc)},
        )

    @app.exception_handler(commands.CommandError)
    async def _command_error(request, exc):
        return JSONResponse(status_code=exc.status_code,
                            content={"error": exc.code, "detail": str(exc)})

    # -- API -----------------------------------------------------------------

    @app.get("/api", summary="Index of every route this process serves")
    async def api_index():
        routes = []
        for route in app.routes:
            methods = getattr(route, "methods", None)
            if not methods:
                continue
            routes.append(
                {
                    "path": route.path,
                    "methods": sorted(methods),
                    "summary": getattr(route, "summary", "") or "",
                }
            )
        routes.sort(key=lambda item: item["path"])
        return _json(
            {
                "routes": routes,
                "read_only": False,
                "write_endpoints": 2,
                "phase": "dashboard-parity",
                "note": (
                    "Commands and the compatibility decision endpoint write "
                    "local intent. This process never applies instruction files or runs models."
                ),
                "db_path": str(resolved_db),
            }
        )

    @app.get("/api/health", summary="Is the read-only database open and readable")
    async def api_health():
        held = store()
        return _json(
            {
                "ok": True,
                "db_path": str(held.db_path),
                "read_only": bool(held.read_only),
                "schema_migrations": getattr(held, "migrations_applied", None),
                "static_dir": str(resolved_static),
                "index_html_present": (resolved_static / INDEX_FILENAME).is_file(),
                "now_utc": now().isoformat().replace("+00:00", "Z"),
            }
        )

    @app.get("/api/overview", summary="V1 Overview: freshness, grid, backlog, failures")
    async def api_overview(window_days: int | None = DEFAULT_WINDOW_DAYS):
        return _json(
            queries.overview(store(), cfg, now_utc=now(), window_days=window_days)
        )

    from . import run_data

    @app.exception_handler(run_data.RunDataError)
    async def _run_data_error(request, exc):
        return JSONResponse(status_code=404 if isinstance(exc,run_data.RunNotFound) else 400,
                            content={'error':type(exc).__name__,'detail':str(exc)})

    @app.get('/api/runs', summary='Every distinct run on a selected UTC day')
    async def api_runs(day: str):
        with store().transaction():
            return _json(run_data.list_runs(store(),day))

    @app.get('/api/runs/{run_id}', summary='Exact run, native stage outcomes, and recorded call totals')
    async def api_run(run_id: str):
        with store().transaction():
            return _json(run_data.detail(store(),run_id))

    @app.get('/api/runs/{run_id}/records', summary='Paginated records explicitly associated with one run')
    async def api_run_records(run_id: str, kind: str, limit: int = 50, cursor: str | None = None):
        with store().transaction():
            return _json(run_data.records(store(),run_id,kind=kind,limit=limit,cursor=cursor))

    @app.get("/api/rules", summary="V2 Rules: every learning with provenance and verdict")
    async def api_rules(evidence_sample: int = 5):
        return _json(queries.rules(store(), evidence_sample=evidence_sample))

    @app.get(
        "/api/rule-families",
        summary="V2 Rules: duplicate families, or the reason there are none",
    )
    async def api_rule_families():
        return _json(queries.rule_families(store()))

    @app.get(
        "/api/review-queue",
        summary="V3 Review queue: every proposal that needs a person, by lesson",
    )
    async def api_review_queue():
        with store().transaction():
            return _json(queries.review_queue(store(), cfg))

    @app.get("/api/proposals/{proposal_id}/review", summary="Full current proposal revision and retained evidence")
    async def api_review_proposal(proposal_id: str):
        with store().transaction():
            return _json(commands.review_snapshot(store(), proposal_id, cfg))

    @app.get('/api/review-preview', summary='Complete selected members and consolidated edits, without recording a decision')
    async def api_review_preview(proposal_ids: str):
        from ..review import preview_selection
        with store().transaction():
            return _json(preview_selection(store(), cfg, proposal_ids.split(',')))

    @app.post("/api/commands", summary="Record a typed command over exact reviewed proposal revisions")
    async def api_command(body: dict):
        writer = writer_store()
        try:
            result = commands.submit_command(writer, cfg, body, now=now().isoformat().replace("+00:00", "Z"))
            return JSONResponse(content=result, status_code=202)
        finally:
            writer.close()

    @app.get('/api/proposals/{proposal_id}/rollback-preview', summary='Preview the inverse of one recorded applied contribution')
    async def api_rollback_preview(proposal_id: str):
        from ..rollback import rollback_view
        with store().transaction():
            return _json(rollback_view(store(),cfg,proposal_id))

    @app.get('/api/proposals/{proposal_id}/eval-preview', summary='Frozen evaluation source, stages, and maximum authorized calls')
    async def api_eval_preview(proposal_id: str):
        from ..jobs import preview
        with store().transaction():
            return _json(preview(store(),cfg,proposal_id))

    @app.get('/api/proposals/{proposal_id}/resolution-preview', summary='Frozen rollback conflict and maximum generation calls')
    async def api_resolution_preview(proposal_id: str):
        from ..jobs import preview
        with store().transaction():
            return _json(preview(store(),cfg,proposal_id,action='resolve_rollback'))

    @app.get('/api/proposals/{proposal_id}/reapplication-preview', summary='Recorded rollback and exact reapplication preview')
    async def api_reapplication_preview(proposal_id: str):
        from ..reapplications import view
        with store().transaction():
            return _json(view(store(),cfg,proposal_id))

    @app.get('/api/learnings/{learning_id}/recovery-options', summary='Known destinations for a recovery proposal')
    async def api_recovery_options(learning_id: str):
        from ..recovery_jobs import options
        with store().transaction():
            return _json(options(store(),cfg,learning_id))

    @app.get('/api/learnings/{learning_id}/mining-history', summary='Complete retained mining revisions with stable pagination')
    async def api_mining_history(learning_id: str, limit: int = 20, cursor: str | None = None):
        from ..mining_history import page, HistoryError
        from ..commands import CommandError
        with store().transaction():
            if not store().query_one('SELECT id FROM learnings WHERE id=?',(learning_id,)):
                raise CommandError('NoSuchLearning','No learning with that ID.',404)
            try:return _json(page(store(),learning_id,limit=limit,cursor=cursor))
            except HistoryError as exc:raise CommandError('MiningHistoryError',str(exc),409) from exc

    @app.get('/api/learnings/{learning_id}/recovery-preview', summary='Frozen recovery subject, target, and one-call limit')
    async def api_recovery_preview(learning_id: str, mode: str, target_id: str, proposal_ids: str = '[]', fresh: bool = False):
        from ..recovery_jobs import view
        from ..commands import CommandError
        import json
        try:ids=json.loads(proposal_ids)
        except ValueError as exc:raise CommandError('InvalidRecovery','Proposal IDs must be a JSON list.') from exc
        with store().transaction():
            return _json(view(store(),cfg,learning_id,mode,target_id,ids,fresh=fresh))

    @app.get('/api/incidents', summary='Paginated unmined incidents for selected recovery')
    async def api_incidents(limit: int = 25, cursor: str | None = None):
        from ..incident_jobs import page
        with store().transaction():
            return _json(page(store(),limit=limit,cursor=cursor))

    @app.get('/api/incidents/{incident_id}/mining-preview', summary='Selected incident, retained coverage, and maximum mining calls')
    async def api_incident_mining(incident_id: str, full: bool = False):
        from ..incident_jobs import view
        with store().transaction():
            return _json(view(store(),cfg,incident_id,full=full))

    @app.get('/api/operations/{operation_id}', summary='Durable instruction operation and its exact reviewed source')
    async def api_operation(operation_id: str, summary: bool = False):
        from ..operations import operation_status, operation_summary
        with store().transaction():
            operation = operation_status(store(), operation_id)
            return _json(operation_summary(operation) if summary else operation)

    @app.get('/api/operations', summary='Paginated automatic-delivery and rollback history')
    async def api_operations(limit: int = 50, cursor: str | None = None, summary: bool = False):
        from ..operations import list_operations
        with store().transaction():
            return _json(list_operations(store(), limit=limit, cursor=cursor, summary=summary))

    @app.get("/api/commands/{command_id}", summary="Durable command and per-target delivery state")
    async def api_command_status(command_id: str, summary: bool = False):
        with store().transaction():
            command = commands.command_status(store(), command_id)
            return _json(commands.command_summary(command) if summary else command)

    @app.get("/api/commands", summary="Recent durable commands, including incomplete delivery")
    async def api_commands(limit: int = 50, cursor: str | None = None, summary: bool = False):
        with store().transaction():
            return _json(commands.list_commands(store(), limit=limit, cursor=cursor, summary=summary))

    #: The product's entire write vocabulary. A word outside this map is
    #: refused; it is never coerced to the nearest plausible one.
    DECISIONS = {
        "approve": ("approved_user", "approved_user"),
        "reject": ("rejected_user", "rejected_user"),
    }

    @app.post(
        "/api/proposals/{proposal_id}/decision",
        summary="Compatibility decision endpoint; rejection retains lesson-wide scope",
    )
    async def api_decide(proposal_id: str, body: dict):
        decision = body.get("decision")
        if decision not in DECISIONS:
            return JSONResponse(
                status_code=400,
                content={
                    "error": "UnknownDecision",
                    "detail": f"{decision!r} is not a decision this product makes",
                    "accepted": sorted(DECISIONS),
                },
            )
        event, new_status = DECISIONS[decision]
        note = body.get("note", "")
        if not isinstance(note, str):
            return JSONResponse(
                status_code=400,
                content={"error": "UnknownDecision", "detail": "note must be a string"},
            )

        # Open only for this request and close in `finally`. The writer uses
        # resolved_db, just like the reader, so a redirected app records its
        # decision in the database that supplied the displayed proposal.
        writer = writer_store()
        try:
            if decision == 'approve' and ('revision' in body or 'request_key' in body):
                request = {'request_key': body.get('request_key'), 'action': 'approve', 'note': note,
                           'members': [{'proposal_id': proposal_id, 'revision': body.get('revision')}]}
                if 'preview_revision' in body:
                    request['preview_revision'] = body['preview_revision']
                result = commands.submit_command(writer, cfg, request, now=now().isoformat().replace('+00:00', 'Z'))
                return _json({'proposal_id': proposal_id, 'decision': decision, 'event': event,
                              'status': new_status, 'applied': False, 'command': result,
                              'means': 'The approval command is queued. No instruction file has been written.'})
            row = writer.query_one(
                "SELECT id, status, learning_id FROM proposals WHERE id = ?",
                (proposal_id,),
            )
            if row is None:
                return JSONResponse(
                    status_code=404,
                    content={
                        "error": "NoSuchProposal",
                        "detail": f"no proposal {proposal_id!r}",
                    },
                )
            # Resolve decision eligibility from the same waiting set that renders the
            # queue, including review-only actions and their current decision state.
            if proposal_id not in set(queries.waiting_proposal_ids(writer, cfg)):
                return JSONResponse(
                    status_code=409,
                    content={
                        "error": "NotWaiting",
                        "detail": (
                            f"proposal {proposal_id!r} is {row['status']!r} and is "
                            "not in the review queue. Recording a decision here "
                            "would claim someone made one."
                        ),
                        "status": row["status"],
                        "waiting_statuses": list(queries.QUEUEING_STATUSES),
                        "also_waiting_actions": list(
                            queries._cfg_attr(cfg, "review_queue_actions")
                        ),
                    },
                )
            if decision == 'approve':
                raise commands.CommandError('ReviewRequired',
                    'Approval requires the revision returned by GET /api/proposals/{id}/review and a unique request_key. No decision was recorded.', 409)
            # Compare the observed state and independently exclude decided or
            # unknown states. A passing gate can now be in Review because its
            # class is off; a hardcoded old queue-status list would reject the
            # same card this route just admitted. Revision-bound content checks
            # belong to the command protocol; this guard protects status races.
            from ..store import PROPOSAL_STATUSES, DECIDED_STATUSES

            undecided = tuple(sorted(PROPOSAL_STATUSES - set(DECIDED_STATUSES)))
            slots = ",".join("?" * len(undecided))
            cur = writer.conn.execute(
                f"UPDATE proposals SET status=? WHERE id=? AND status=? AND status IN ({slots})",
                (new_status, proposal_id, row["status"], *undecided),
            )
            if cur.rowcount != 1:
                writer.conn.rollback()
                return JSONResponse(
                    status_code=409,
                    content={
                        "error": "NotWaiting",
                        # Says what it OBSERVED, not why. It cannot know
                        # whether another request raced, or the check above
                        # was stale for some other reason, and the common
                        # real case is one person clicking twice. Naming a
                        # racer there would be a confident wrong cause.
                        "detail": (
                            f"proposal {proposal_id!r} was no longer waiting "
                            "when the write ran, so no decision was recorded. "
                            "Reload the queue to see its current state."
                        ),
                        "status": row["status"],
                    },
                )
            # PRD J2 step 4: "Rejecting records `rejected_user`, which
            # permanently blocks re-proposal of that rule." Moving
            # `proposals.status` alone does NOT do that. Both mechanisms that
            # block a lesson key on the LEARNING: `miner.py` dismisses a new
            # incident that duplicates a learning whose status is `rejected`,
            # and `pipeline.py` builds its dedup pool from
            # `learnings WHERE status='rejected'` under a comment reading
            # "rules the user previously rejected". That pool was designed for
            # this producer, which did not exist until V3 shipped.
            #
            # Approving deliberately does NOT touch the learning: an approved
            # lesson stays alive so `apply.py` can act on it.
            if decision == "reject":
                writer.conn.execute(
                    "UPDATE learnings SET status = 'rejected' WHERE id = ?",
                    (row["learning_id"],),
                )
            stamp = now().isoformat().replace("+00:00", "Z")
            writer.insert(
                "proposal_events",
                {
                    "id": new_id(),
                    "proposal_id": proposal_id,
                    "ts": stamp,
                    "event": event,
                    # Derived, not literal: the event decides the actor, so a
                    # new event cannot quietly claim to be a person.
                    "actor": actor_for(event),
                    "note": note,
                },
            )
            writer.commit()
        finally:
            writer.close()
        return _json(
            {
                "proposal_id": proposal_id,
                "decision": decision,
                "event": event,
                "status": new_status,
                "applied": False,
                "means": (
                    "The decision is recorded. Nothing has been written to an "
                    "instruction file: applying is apply.py's job, not this "
                    "endpoint's."
                ),
            }
        )

    from . import scan_data
    from ..scan_observations import ScanObservationError

    @app.exception_handler(scan_data.ExposureRequestError)
    async def _exposure_request_error(request, exc):
        return JSONResponse(status_code=400, content={"error": "ExposureRequestError", "detail": str(exc)})

    @app.exception_handler(ScanObservationError)
    async def _scan_data_error(request, exc):
        return JSONResponse(status_code=409, content={"error": "ScanObservationError", "detail": str(exc)})

    @app.exception_handler(scan_data.ScanHistoryRequestError)
    async def _scan_history_request_error(request, exc):
        return JSONResponse(status_code=400, content={"error": "ScanHistoryRequestError", "detail": str(exc)})

    from ..scan_reporting import ScanReportingError

    @app.exception_handler(ScanReportingError)
    async def _scan_reporting_error(request, exc):
        return JSONResponse(status_code=409, content={"error": "ScanReportingError", "detail": str(exc)})

    @app.get('/api/incidents/{incident_id}/scan-history', summary='Recorded detector observations linked to this incident')
    async def api_incident_scan_history(incident_id: str, limit: int = 20, cursor: str | None = None):
        from ..commands import CommandError
        with store().transaction():
            if not store().query_one('SELECT id FROM incidents WHERE id=?', (incident_id,)):
                raise CommandError('NoSuchIncident', 'No incident with that ID.', 404)
            return _json(scan_data.history_page(store(), incident_id=incident_id, limit=limit, cursor=cursor))

    @app.get("/api/project-exposure", summary="Timestamped signal occurrences per physical-line exposure")
    async def api_project_exposure(project_key: str, start: str | None = None,
                                   end: str | None = None, compatibility_key: str | None = None,
                                   working_copy_id: str | None = None, signal_type: str | None = None):
        with store().transaction():
            return _json(scan_data.project_exposure(
                store(), project_key=project_key, now_utc=now(), start=start, end=end,
                compatibility_key=compatibility_key, working_copy_id=working_copy_id,
                signal_types=(signal_type,) if signal_type is not None else None,
                include_options=True))

    @app.get("/api/projects", summary="V4 Projects: one row per repo, clones collapsed")
    async def api_projects(
        weigh_top_n: int | None = DEFAULT_WEIGH_TOP_N,
        other_md: bool = False,
    ):
        weigher, weigh_reason = _context_weigher(scan_other_md=other_md)
        with store().transaction():
            payload = queries.projects(store(), weigh=weigher, weigh_top_n=weigh_top_n, now_utc=now())
        payload["request"] = {
            "weigh_top_n": weigh_top_n,
            "weigh_top_n_default": DEFAULT_WEIGH_TOP_N,
            "scan_other_md": bool(other_md),
            "cut": (
                "context weight is measured for the top-N repos by session "
                "count and the rest carry the em-dash and the reason. The "
                "repo-wide markdown walk (other_md) is off unless other_md=1: "
                "it is the expensive half, and PRD section 8a says that number "
                "must never be added into context weight."
            ),
            "walker_unavailable_reason": weigh_reason,
        }
        return _json(payload)

    @app.get("/api/incident-rate", summary="Incidents per 100k lines by month (PRD 8b)")
    async def api_incident_rate(project_key: str | None = None):
        return _json(
            queries.incident_rate(store(), now_utc=now(), project_key=project_key)
        )

    # -- SPA -----------------------------------------------------------------

    @app.get("/", summary="The single-page app")
    async def spa_index():
        index = resolved_static / INDEX_FILENAME
        if not index.is_file():
            return responses.PlainTextResponse(
                status_code=500,
                content=(
                    f"The dashboard's {INDEX_FILENAME} is missing.\n"
                    f"  expected at: {index}\n"
                    "  The SPA ships inside the package, so a missing file "
                    "means an incomplete install or an incomplete build, not a "
                    "configuration choice. There is no fallback page: an empty "
                    "shell would look like a dashboard with no data.\n"
                    "  The API is unaffected. GET /api lists every route."
                ),
            )
        return responses.FileResponse(index, media_type="text/html")

    if resolved_static.is_dir():
        # Mounted last, so every /api route matches first. StaticFiles answers
        # GET and HEAD only; it exposes no write verb.
        app.mount(
            "/",
            staticfiles.StaticFiles(directory=str(resolved_static), html=True),
            name="static",
        )

    return app


def _context_weigher(*, scan_other_md: bool):
    """The context-weight walker, or ``None`` and the reason it is unavailable.

    ``scan_other_md=False`` skips the repo-wide markdown walk, which is the
    only expensive part and is never added into context weight (PRD 8a).
    """
    try:
        from .context_weight import context_weight  # noqa: PLC0415
    except ImportError as exc:
        return None, (
            f"src/self_improve/dashboard/context_weight.py is not importable ({exc})"
        )

    def weigh(path):
        return context_weight(path, scan_other_md=scan_other_md)

    return weigh, ""


def serve(cfg, *, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> int:
    """Run the dashboard until interrupted. Returns a process exit code."""
    bind = _require_loopback(host)
    uvicorn = _require_uvicorn()
    app = create_app(cfg)
    # flush: stdout is block-buffered when it is a pipe, and a startup
    # banner that only appears when the process dies is not a banner.
    print(
        f"dashboard: http://{bind}:{port}  "
        "(reads only, except V3 approve/reject; ctrl-c to stop)",
        flush=True,
    )
    uvicorn.run(app, host=bind, port=port, log_level="warning")
    return 0
