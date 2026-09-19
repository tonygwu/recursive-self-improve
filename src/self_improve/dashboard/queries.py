"""Read-only data layer for the P0 dashboard (V1 Overview, V2 Rules, V4 Projects).

Every public function takes an **already-open, read-only** ``Store`` and returns
plain dicts/lists that ``json.dumps`` accepts. Deliberate properties:

- **This module never constructs a ``Store``.** The application opens a
  ``Store(path, read_only=True)`` for its lifespan and passes it in, so these
  queries cannot migrate the database.
- **No FastAPI import**, so the module imports and tests without the dashboard
  extra installed.
- **No SQLite-only SQL.** No ``json_each``, no ``SUM(bool)``, no
  ``group_concat``; ``substr`` is used because Postgres has it too. Grouping
  that needs JSON is done in Python.
- **Logical time comes from the data.** Every date the data defines is read out
  of the data (``incidents.ts``, ``runs.started``). The one genuinely
  wall-clock question ("did it run last night?") takes an explicit, UTC,
  timezone-aware ``now_utc`` from the caller and reports it back as
  ``reference_day``; this module never calls ``datetime.now()`` and never
  formats a local-time date.
- **Fail loud.** A missing key raises ``DashboardDataError`` naming the row.
  Malformed JSON raises. Nothing defaults to 0 or "".
- **Every cap reports what it cut**, in a sibling key naming the count and the
  reason.

Judgement calls made while implementing this file, each surfaced in the payload
it affects rather than buried here:

1. ``_stage_cell`` precedence: when a run records a stage, the stage's own
   numbers decide the cell; when it does not, the *run's* status decides
   (``running``/``abandoned``/``error`` are their own cell states, and only an
   ``ok`` run yields ``skipped``). A run that never reported completion is a
   real signal and must never read as success.
2. A stage that attempted work, reported no failures and produced nothing is
   ``refused``, not ``failed`` — ``AGENTS.md`` forbids reading a run with no
   eval verdicts as a gate that failed.
3. ``projects()`` measures context weight in the working copy with the **most
   sessions** that still exists on disk, not in ``routing.canonical_working_copy()``.
   Those answer different questions: canonical is where automation *writes*,
   while context weight is what an agent actually *carried*.
4. ``top_signal`` ties break on the alphabetically first ``signal_type`` and the
   tie is reported in ``tied_with``.
5. Historical fix metadata matches only an exact verified constraint key.
   A later run with that key stays visible; source commit time alone does not
   establish the deployed revision or prove the identical defect recurred.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from datetime import date, datetime, timedelta, timezone

# Constants only — never the Store class. `test_queries_never_constructs_a_store`
# enforces that distinction, because constructing one runs migrations.
from .. import project_measurements

from .scan_data import project_exposure
from ..execution_policy import waiting_proposals, proposal_dispositions, policy_snapshot, MANDATORY_REVIEW_ACTIONS
from ..store import (
    AUTO_APPLY_STATUSES,
    AWAITING_APPLY_STATUSES,
    DECIDED_STATUSES,
    QUEUEING_STATUSES,
    TERMINAL_STATUSES,
)

__all__ = [
    "DashboardDataError",
    "NOT_COMPUTABLE",
    "STAGES",
    "backlog",
    "data_freshness",
    "failure_panel",
    "fraction",
    "gate_health",
    "inbox",
    "incident_rate",
    "normalize_incident",
    "not_computable",
    "overview",
    "projects",
    "rule_families",
    "rules",
    "run_stage_grid",
    "status_line",
]


from .. import stage_accounting


class DashboardDataError(Exception):
    """A row the dashboard cannot read. Never recovered from with a default."""


#: The literal the UI must render where a number would assert something false.
#: PRD §7 V4: benefit stays "—", never 0, because 0 asserts "applied, no effect".
NOT_COMPUTABLE = "—"

STAGES = ("scan", "mine", "cluster", "gate", "apply")

_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
_ISO_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: Longest readable snippet a serialized incident carries. Anything cut is
#: reported in ``display_text_truncated``.
DISPLAY_TEXT_MAX = 400

# Import the status vocabulary from store.py. Tests reconcile KNOWN_STATUSES
# with PROPOSAL_STATUSES in both directions.
KNOWN_STATUSES = frozenset(
    QUEUEING_STATUSES + AUTO_APPLY_STATUSES + AWAITING_APPLY_STATUSES + TERMINAL_STATUSES
)

#: Cell states, worst first. Aggregating a night takes the worst state of every
#: run that night. ``skipped`` is last on purpose: absence of a signal must not
#: outrank a real outcome.
CELL_STATES_WORST_FIRST = (
    "unreadable",
    "unaccounted",
    "unknown_status",
    "failed",
    "error",
    # A run status can BECOME a cell state when the run recorded nothing for
    # that stage, so every member of RUN_STATUSES except `ok` has to be ranked
    # here. `_worst_state` drops what it cannot rank and falls through to
    # `skipped`, which the page renders as "did not run" — a broken run showing
    # as absence. `degraded` and `interrupted` did exactly that when they were
    # added to RUN_STATUSES.
    "degraded",
    "interrupted",
    "abandoned",
    "partial",
    "limited",
    "budget_exhausted",
    "refused",
    "running",
    "ok",
    "skipped",
)

#: runs.status values. The stale-run reaper writes 'abandoned' with a finished
#: timestamp, so a finish timestamp alone cannot establish success.
#: `degraded` means the run finished but a whole stage produced nothing —
#: see pipeline.derive_run_status. Omitting it here would render every such
#: run as `unknown_status`.
RUN_STATUSES = (
    "ok",
    "degraded",
    "running",
    "abandoned",
    "error",
    "interrupted",
    "budget_exhausted",
)

#: llm_calls.outcome values that are successes. `parse_recovered` delivered
#: valid contract JSON wrapped in prose and `oauth_transient_retried` completed
#: after one retry; counting them as failures understates every stage.
#: Same rule as report.py `_stage_asf`.
#: Re-exported, not redefined — see store.LLM_SUCCESS_OUTCOMES.
from ..store import LLM_SUCCESS_OUTCOMES  # noqa: E402  (kept beside its users)

# Compatibility exports; report generation uses the same dependency-free copy.
from ..failure_presentation import (  # noqa: E402
    FAILURE_COPY, FAILURE_PREFIX_COPY, NOT_FAILURE_TAXONOMY, failure_copy, taxonomy_rows,
)

# Code-fix metadata uses commit author time converted to UTC. Match exact
# failure keys, not broad families: fixing one constraint does not fix every
# future IntegrityError.
FIXED_FAILURES = {
    "IntegrityError:unique:incident_learnings": {
        "fixed_at": "2026-08-16T10:56:04Z",
        "fix_commit": "cfd6410b5469b2e2c718c16c1c042e2f1c3f123a",
        "fix": (
            "incident_learnings linking is idempotent (ON CONFLICT DO NOTHING) "
            "and each incident commits independently."
        ),
    },
}

#: How far back a still-open failure counts as "still happening", measured in
#: run days read out of runs.started — never the wall clock.
RECENT_FAILURE_WINDOW_DAYS = 7

#: A month needs at least this many sessions to be charted (PRD §8b: rows under
#: 20 sessions were dropped rather than shown as noise). What is dropped is
#: always reported.
MIN_SESSIONS_FOR_MONTH = 20


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------


def _require(row: dict, key: str, what: str):
    """Read a key or raise. Never defaults — a missing column is a bug."""
    if key not in row:
        raise DashboardDataError(f"{what}: required key {key!r} is missing")
    return row[key]


def _json_obj(raw, what: str) -> dict:
    if raw is None or raw == "":
        return {}
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise DashboardDataError(f"{what}: not strict JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise DashboardDataError(f"{what}: expected a JSON object, got {type(value).__name__}")
    return value


def _json_arr(raw, what: str) -> list:
    if raw is None or raw == "":
        return []
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise DashboardDataError(f"{what}: not strict JSON: {exc}") from exc
    if not isinstance(value, list):
        raise DashboardDataError(f"{what}: expected a JSON array, got {type(value).__name__}")
    return value


def _day(ts: str) -> str:
    """The UTC day of an ISO-UTC timestamp from the data. '' stays ''."""
    if not ts:
        return ""
    head = ts[:10]
    if not _ISO_DAY_RE.match(head):
        raise DashboardDataError(f"timestamp {ts!r} does not start with an ISO date")
    return head


def _utc_day(now_utc) -> str:
    """The UTC day of the caller's explicit clock reading.

    Refuses a naive datetime and refuses a string carrying a non-UTC offset.
    Local-time date formatting is the bug class this guard exists to block: it
    passes on a UTC CI box and fails on a developer machine depending on the
    hour.
    """
    if isinstance(now_utc, datetime):
        if now_utc.tzinfo is None:
            raise DashboardDataError(
                "now_utc must be timezone-aware; a naive datetime is local time "
                "and the data is UTC"
            )
        return now_utc.astimezone(timezone.utc).strftime("%Y-%m-%d")
    if isinstance(now_utc, str):
        text = now_utc.strip()
        if _ISO_DAY_RE.match(text):
            return text
        if not (text.endswith("Z") or text.endswith("+00:00")):
            raise DashboardDataError(
                f"now_utc={now_utc!r} is not UTC; pass a 'Z'/'+00:00' timestamp "
                "or a bare YYYY-MM-DD UTC day"
            )
        return _day(text)
    raise DashboardDataError(
        f"now_utc must be an aware datetime or an ISO-UTC string, got {type(now_utc).__name__}"
    )


def _days_between(earlier: str, later: str) -> int:
    return (date.fromisoformat(later) - date.fromisoformat(earlier)).days


def _day_range(end_day: str, days: int) -> list[str]:
    """``days`` calendar days ending at ``end_day`` inclusive, ascending."""
    if days < 1:
        raise DashboardDataError(f"days must be >= 1, got {days}")
    last = date.fromisoformat(end_day)
    return [(last - timedelta(days=offset)).isoformat() for offset in range(days - 1, -1, -1)]


def fraction(
    numerator: int,
    denominator: int,
    *,
    min_denominator: int,
    numerator_label: str,
    denominator_label: str,
    reason_when_small: str,
    small_display: str | None = None,
) -> dict:
    """A rate, or the raw fraction and a flag when the sample cannot carry one.

    Never returns ``0.0`` for "no data": PRD §5.2 — ``0% success`` on n=0 is a
    claim about the world that the data does not make. ``display`` contains no
    percent sign unless ``enough_data`` is true.
    """
    enough = denominator >= min_denominator and denominator > 0
    rate = (numerator / denominator) if enough else None
    if enough:
        display = f"{numerator}/{denominator} ({rate * 100:.0f}%)"
    else:
        # `small_display` exists because numerator/denominator is not always the
        # pair a reader needs. Survival passes applied+rolled_back as the
        # denominator, so the generic sentence below would render "1 applied ·
        # 2 rolled back" where PRD 5.2 asks for "1 applied · 1 rolled back".
        # The caller knows which two numbers mean something; let it say so.
        counts = small_display or (
            f"{numerator} {numerator_label} · {denominator} {denominator_label}"
        )
        display = f"{counts} — {reason_when_small}"
    return {
        "numerator": numerator,
        "denominator": denominator,
        "numerator_label": numerator_label,
        "denominator_label": denominator_label,
        "rate": rate,
        "enough_data": enough,
        "min_denominator": min_denominator,
        "display": display,
        "reason": "" if enough else reason_when_small,
    }


def not_computable(reason: str, **extra) -> dict:
    """The designed empty state: an em-dash and why, never a zero."""
    return {"value": NOT_COMPUTABLE, "computable": False, "reason": reason, **extra}


def _worst(states) -> str:
    order = {name: index for index, name in enumerate(CELL_STATES_WORST_FIRST)}
    present = [s for s in states if s in order]
    if not present:
        return "skipped"
    return min(present, key=lambda s: order[s])


# ---------------------------------------------------------------------------
# V1 — freshness
# ---------------------------------------------------------------------------


def data_freshness(store, *, now_utc) -> dict:
    """Describe data freshness from the newest transcript timestamp.

    Use ``incidents.ts`` rather than ``created_at``. A rebuild can insert many
    historical incidents together without changing when their source events occurred.
    """
    row = store.query_one(
        "SELECT MIN(ts) AS first_ts, MAX(ts) AS last_ts, COUNT(*) AS n "
        "FROM incidents WHERE ts <> ''"
    ) or {}
    last_ts = row.get("last_ts") or ""
    first_ts = row.get("first_ts") or ""
    reference_day = _utc_day(now_utc)
    as_of = _day(last_ts) if last_ts else None
    stale = _days_between(as_of, reference_day) if as_of else None
    if as_of is None:
        banner = "No incidents carry a timestamp yet — nothing to date this view from."
    elif stale == 0:
        banner = f"Data as of {as_of} (today)."
    else:
        night = "day" if stale == 1 else "days"
        banner = f"Data as of {as_of}, {stale} {night} stale."
    return {
        "as_of": as_of,
        "first_incident_ts": first_ts or None,
        "last_incident_ts": last_ts or None,
        "incidents_with_ts": int(row.get("n") or 0),
        "reference_day": reference_day,
        "days_stale": stale,
        "banner": banner,
        "timezone": "UTC",
        "source_column": "incidents.ts (transcript time, not created_at)",
    }


# ---------------------------------------------------------------------------
# V1 — run x stage grid
# ---------------------------------------------------------------------------


def _stage_numbers(stage: str, payload: dict) -> dict | None:
    """Compatibility adapter over the pure native-accounting reader."""
    try:
        return stage_accounting.numbers(stage, payload)
    except (ValueError, KeyError) as exc:
        raise DashboardDataError(str(exc)) from exc


def _state_from_numbers(nums: dict) -> str:
    return stage_accounting.state_from_numbers(nums)


def _stage_cell(run: dict, stage: str, stats: dict) -> dict:
    """One run's cell for one stage.

    Precedence: the stage's own numbers when the run recorded them, otherwise
    the run's status. Only an ``ok`` run with no record for the stage yields
    ``skipped``; a run that never reported completion keeps its own state, so a
    stuck run can never read as success.
    """
    status = run["status"]
    payload = stats.get(stage)
    if stage not in stats:
        if status == "ok":
            state = "skipped"
        elif status in RUN_STATUSES:
            state = status
        else:
            state = "unknown_status"
        return {
            "state": state,
            "recorded": False,
            "run_status": status,
            "number": None,
            "number_label": "",
        }
    try:
        nums = _stage_numbers(stage, payload)
    except DashboardDataError as exc:
        raise DashboardDataError(f"run {run['id']}.{exc}") from exc
    if nums is None:
        return {
            "state": "unreadable",
            "recorded": True,
            "run_status": status,
            "number": None,
            "number_label": "",
            "unreadable_keys": sorted(payload) if isinstance(payload, dict) else [],
            "missing_fields": stage_accounting.missing_fields(stage, payload) if isinstance(payload, dict) else [],
            "meaning": stage_accounting.MEANINGS[stage],
            "reason": "Required native outcome counters are missing." if isinstance(payload, dict) else "The retained stage is not an object.",
        }
    state = _state_from_numbers(nums)
    cell = {"state": state, "recorded": True, "run_status": status, **nums}
    if state == "ok" and nums["attempted"] == 0:
        cell["idle"] = True
    return cell


def run_stage_grid(store, *, now_utc, window_days: int | None = None) -> dict:
    """Aggregate stage outcomes by data day, including every run.

    The column key is the date in ``started``. Multiple rows can share an identical
    timestamp; aggregate them rather than using a timestamp-keyed dictionary that
    would discard duplicates. Report the worst outcome and per-state counts.
    """
    runs = store.query(
        "SELECT id, started, finished, status, stats_json FROM runs ORDER BY started, id"
    )
    reference_day = _utc_day(now_utc)

    by_day: dict[str, list[dict]] = {}
    unknown_statuses: dict[str, int] = {}
    unreadable: list[dict] = []
    for run in runs:
        started = _require(run, "started", f"runs.{run.get('id')}")
        day = _day(started)
        if not day:
            raise DashboardDataError(f"run {run['id']}: started is empty; no night to file it under")
        if run["status"] not in RUN_STATUSES:
            unknown_statuses[run["status"]] = unknown_statuses.get(run["status"], 0) + 1
        # Parse once per run, not once per stage.
        run["_stats"] = _json_obj(run["stats_json"], f"runs.{run['id']}.stats_json")
        by_day.setdefault(day, []).append(run)

    all_days = sorted(by_day)
    if window_days is None:
        days = all_days
        cut = {"applied": False}
    else:
        days = _day_range(reference_day, window_days)
        dropped = [d for d in all_days if d not in set(days)]
        cut = {
            "applied": True,
            "window_days": window_days,
            "ends_at": reference_day,
            "nights_dropped": len(dropped),
            "runs_dropped": sum(len(by_day[d]) for d in dropped),
            "oldest_night_kept": days[0],
            "reason": (
                f"caller asked for the last {window_days} nights ending {reference_day}"
            ),
        }

    columns = []
    for day in days:
        day_runs = by_day.get(day, [])
        run_states: dict[str, int] = {}
        for run in day_runs:
            run_states[run["status"]] = run_states.get(run["status"], 0) + 1
        cells: dict[str, dict] = {}
        for stage in STAGES:
            per_run = []
            for run in day_runs:
                cell = _stage_cell(run, stage, run["_stats"])
                cell["run_id"] = run["id"]
                if cell["state"] == "unreadable":
                    unreadable.append(
                        {
                            "run_id": run["id"],
                            "night": day,
                            "stage": stage,
                            "keys": cell.get("unreadable_keys", []),
                        }
                    )
                per_run.append(cell)
            states = [c["state"] for c in per_run]
            state_counts: dict[str, int] = {}
            for state in states:
                state_counts[state] = state_counts.get(state, 0) + 1
            recorded = [c for c in per_run if c["recorded"] and "attempted" in c]
            # Two tiers. When at least one run recorded this stage, the cell
            # reports the worst recorded outcome. A run without stage records
            # must not erase completed stage work from other runs that night.
            # Its status remains visible in states, per_run, and the column's worst.
            reported = [c["state"] for c in per_run if c["recorded"]] or states
            cells[stage] = {
                "state": _worst(reported) if reported else "skipped",
                "states": state_counts,
                "runs_without_record": sum(1 for c in per_run if not c["recorded"]),
                "runs": len(per_run),
                "accounted_runs": len(recorded),
                "unreadable_runs": sum(c["recorded"] and "attempted" not in c for c in per_run),
                "unit": stage_accounting.UNITS[stage],
                "attempted": sum(c["attempted"] for c in recorded) if recorded else None,
                "succeeded": sum(c["succeeded"] for c in recorded) if recorded else None,
                "failed": sum(c["failed"] for c in recorded) if recorded else None,
                "number": sum(c["number"] for c in recorded) if recorded else None,
                "number_label": recorded[0]["number_label"] if recorded else "",
                "per_run": per_run,
            }
        column_states = [cells[s]["state"] for s in STAGES]
        columns.append(
            {
                "night": day,
                "run_count": len(day_runs),
                "run_ids": [r["id"] for r in day_runs],
                "run_states": run_states,
                "worst": _worst(column_states + list(run_states)),
                "cells": cells,
            }
        )

    return {
        "stages": list(STAGES),
        "nights": [c["night"] for c in columns],
        "columns": columns,
        "night_timezone": "UTC",
        "reference_day": reference_day,
        "runs_total": len(runs),
        "nights_with_runs": len(all_days),
        "unknown_run_statuses": unknown_statuses,
        "unreadable": unreadable,
        "window": cut,
        "cell_states": list(CELL_STATES_WORST_FIRST),
    }


# ---------------------------------------------------------------------------
# V1 — status line
# ---------------------------------------------------------------------------


def status_line(store, cfg, *, now_utc, nights: int = 7) -> dict:
    """"Ran N of the last 7 nights. N rules learned, N applied, N waiting on you."

    The 7-night window is anchored on the caller's UTC clock, not on the last
    data day: the honest answer to "did it run last night?" changes when the
    loop is paused, and anchoring on the data would hide exactly that.
    """
    reference_day = _utc_day(now_utc)
    window = _day_range(reference_day, nights)
    window_set = set(window)
    rows = store.query("SELECT id, started, status FROM runs")
    ran_days: set[str] = set()
    statuses: dict[str, int] = {}
    for row in rows:
        if not row["started"]:
            raise DashboardDataError(f"run {row['id']}: started is empty")
        day = _day(row["started"])
        if day in window_set:
            ran_days.add(day)
            statuses[row["status"]] = statuses.get(row["status"], 0) + 1
    ran_days = sorted(ran_days)

    learned_total = int(
        (store.query_one("SELECT COUNT(*) AS n FROM learnings") or {"n": 0})["n"]
    )
    learned_window = sum(
        1
        for row in store.query("SELECT created_at FROM learnings")
        if _day(row["created_at"]) in window_set
    )
    prop_counts = _proposal_status_counts(store)
    applied = prop_counts.get("applied", 0)
    waiting = len(waiting_proposal_ids(store, cfg))

    rules_word = "rule" if learned_total == 1 else "rules"
    text = (
        f"Ran {len(ran_days)} of the last {nights} nights. "
        f"{learned_total} {rules_word} learned, {applied} applied, {waiting} waiting on you."
    )
    return {
        "text": text,
        "nights_ran": len(ran_days),
        "nights_window": nights,
        "nights_ran_days": ran_days,
        "window_start": window[0],
        "window_end": window[-1],
        "reference_day": reference_day,
        "run_statuses_in_window": statuses,
        "rules_learned_total": learned_total,
        "rules_learned_in_window": learned_window,
        "proposals_applied": applied,
        "waiting_on_you": waiting,
    }


# ---------------------------------------------------------------------------
# V1 — inbox
# ---------------------------------------------------------------------------


def _proposal_status_counts(store) -> dict[str, int]:
    return {
        row["status"]: int(row["n"])
        for row in store.query(
            "SELECT status, COUNT(*) AS n FROM proposals GROUP BY status"
        )
    }


def _latest_run_stats(store) -> tuple[dict | None, dict]:
    run = store.query_one(
        "SELECT id, started, finished, status, stats_json FROM runs "
        "ORDER BY started DESC, id DESC LIMIT 1"
    )
    if run is None:
        return None, {}
    return run, _json_obj(run["stats_json"], f"runs.{run['id']}.stats_json")


def inbox(store, cfg) -> dict:
    """The one number in the nav, per the trust model — plus its blocker.

    ``count`` comes from the shared human-review resolver. Automatic delivery
    requires a passing gate and current class permission, including the source
    run's review-only veto. Qualifying automatic work is counted separately.
    """
    counts = _proposal_status_counts(store)
    known = KNOWN_STATUSES
    unknown = {k: v for k, v in counts.items() if k not in known}
    queued = len(waiting_proposal_ids(store, cfg))
    auto_pending = sum(p["next_step"] == "automatic_delivery" for p in proposal_dispositions(store, cfg))

    run, stats = _latest_run_stats(store)
    if run is None:
        review_only = None
        review_reason = "no run has ever been recorded"
    elif "review_only" not in stats:
        review_only = None
        # Short id inline. This line renders in a ~180px nav column and fires
        # for the whole duration of every nightly (a running run's stats_json
        # is '{}' until it finishes), so the raw 32-char id wrapped across
        # three lines under the badge. The full id stays in
        # `review_only_source` for anyone who needs to look the run up.
        review_reason = f"run {run['id'][:8]} recorded no review_only flag"
    else:
        review_only = bool(stats["review_only"])
        review_reason = f"from run {run['id']} started {run['started']}"

    item = "item" if queued == 1 else "items"
    line = f"{queued} {item} waiting on you"
    second = f"{auto_pending} passing proposals await automatic delivery" if auto_pending else ""

    return {
        "count": queued,
        "line": line,
        "second_line": second,
        "by_status": counts,
        "queueing_statuses": list(QUEUEING_STATUSES),
        "auto_apply_pending": auto_pending,
        "auto_apply_statuses": list(AUTO_APPLY_STATUSES),
        "review_only": review_only,
        "review_only_source": (
            f"{review_reason} (run {run['id']})" if run is not None else review_reason
        ),
        "unknown_statuses": unknown,
    }


# ---------------------------------------------------------------------------
# V3 — the review queue, the product's only write surface (PRD §7 V3, D1)
# ---------------------------------------------------------------------------

#: Why a proposal in each queueing status needs a person, in the operator's
#: words rather than the schema's. Keyed by ``proposals.status``; every member
#: of ``QUEUEING_STATUSES`` must appear here, and a status that does not appear
#: raises rather than rendering a card with a blank reason. The SPA renders
#: what this sends — it does not keep its own copy of this vocabulary, which is
#: how ``GATE_VERDICTS`` ended up as a third copy of the gate's verdict list.
WHY_QUEUED = {
    "pending": "the eval never ran, so nothing has tested whether this rule helps.",
    "gated_fail": "the eval ran and could not show the rule helps.",
    "inconclusive": "the gate ran several scenarios and they disagreed.",
    "held": "the apply policy held this rather than writing it.",
    "ungated": "the eval did not establish usable evidence that this rule helps.",
    "gated_pass": "the gate passed, but automatic application is not authorized for this proposal.",
}

#: Why an ACTION queues regardless of its verdict (PRD §5.1, the two carve-out
#: rows). Keyed by ``proposals.action``; every member of
#: ``cfg.review_queue_actions`` must appear here or the lookup raises.
WHY_CARVE_OUT = {
    "add": "your configuration requires manual review for instruction additions.",
    "edit": "your configuration requires manual review for instruction edits.",
    "delete": "your configuration requires manual review for instruction deletions.",
    "new_skill": "your configuration requires manual review for new skills.",
    "new_rule_file": "your configuration requires manual review for new rule files.",
    "recover_rule": "this is a generated recovery proposal. Inspect its exact patch, selected source, and destination before approving.",
    "resolve_rollback": "this resolves a rollback conflict. Inspect the exact changes and retained application before approving.",
    "reapply": "this reapplies a rolled-back change. Inspect the new patch and original rollback before approving.",
    "convert_to_hook": (
        "this installs or changes a hook. Hooks run on every invocation, so a "
        "bad one is not advice, it is broken tooling."
    ),
    "delete_human_line": (
        "this deletes a line a human wrote. Only the author knows whether it "
        "is load-bearing."
    ),
}


def waiting_proposal_ids(store, cfg) -> list[str]:
    """Return the shared set of proposals waiting for a human decision.

    Navigation, overview, and queue views must use the same resolver, including
    review-only actions. Independent status lists can make their counts disagree.
    """
    return [row["id"] for row in waiting_proposals(store, cfg)]


def why_needs_you(status: str, *, action: str, cfg) -> tuple[str, str, bool]:
    """``(reason_code, copy, is_carve_out)`` for one proposal.

    Raises ``KeyError`` on a status or action nobody wrote copy for. That is
    deliberate: a queue whose cards cannot say why they are there is worse than
    a queue that fails to load, because the operator cannot tell the difference
    between "no reason" and "reason missing".
    """
    carve_outs = tuple(sorted(set(_cfg_attr(cfg, "review_queue_actions")) | MANDATORY_REVIEW_ACTIONS))
    if action in carve_outs:
        return action, WHY_CARVE_OUT[action], True
    return status, WHY_QUEUED[status], False


# Outcome keys distinguish an unexecuted trial from a judged failure. Zero
# successes alone proves neither. Unrecognized keys retain their taxonomy rather
# than receiving a guessed explanation.
from ..eval_evidence import EVAL_INFRA_ERRORS, EVAL_JUDGED_ERRORS, no_trial_ran as eval_no_trial_ran

#: How many example incidents a card carries. A cap, so it is REPORTED:
#: every family says how many it held back so a displayed sample cannot be
#: mistaken for the complete evidence count.
REVIEW_INCIDENT_EXAMPLES = 3

#: What the operator's two buttons DO about each queueing status, in prose.
#: Same keys as ``WHY_QUEUED``, deliberately as a SECOND dict rather than a
#: nested one: the reconciliation below reads both and raises on a key in
#: either that is missing from the other. Check both directions at import so
#: a new queue status cannot lack an explanation or retain obsolete copy.
MEANS_QUEUED = {
    "pending": (
        "No eval has run on this rule yet, so nothing has tested whether it "
        "helps. Approving records your decision without eval evidence. "
        "Reject selected targets suppresses the lesson at those files. "
        "Reject lesson everywhere suppresses it at every target."
    ),
    "gated_fail": (
        "The eval ran and did not show the rule helping. Read the arms below "
        "before treating that as evidence against the rule: a trial that "
        "never reproduced the mistake cannot show a fix for it."
    ),
    "inconclusive": (
        "The gate runs several scenarios and they disagreed with each other. "
        "Contrary evidence exists, so this rule needs a human decision."
    ),
    "held": (
        "The apply policy held this rather than writing it. The eval verdict "
        "is not the reason; the policy is."
    ),
    "ungated": (
        "The eval did not establish usable evidence. This rule requires your "
        "approval even when automatic application is enabled for its class."
    ),
    "gated_pass": (
        "The gate passed. This proposal still needs your decision because its "
        "class policy or originating run does not authorize automatic application."
    ),
}

_why_keys, _means_keys = set(WHY_QUEUED), set(MEANS_QUEUED)
if _why_keys != _means_keys:
    raise DashboardDataError(
        "WHY_QUEUED and MEANS_QUEUED disagree: "
        f"only in WHY_QUEUED {sorted(_why_keys - _means_keys)}, "
        f"only in MEANS_QUEUED {sorted(_means_keys - _why_keys)}"
    )


def _arm(metrics: dict, name: str) -> dict:
    """One trial arm's counts, or ``{}`` when the eval recorded no such arm."""
    raw = metrics.get(name)
    if not isinstance(raw, dict):
        return {}
    errors = raw.get("errors")
    return {
        "attempted": int(raw.get("attempted") or 0),
        "succeeded": int(raw.get("succeeded") or 0),
        "failed": int(raw.get("failed") or 0),
        "errors": errors if isinstance(errors, dict) else {},
        "transcripts_dir": str(raw.get("transcripts_dir") or ""),
    }


def eval_story(row) -> dict:
    """Explain the recorded evaluation arms and their outcome taxonomy.

    Distinguish missing or unexecuted trials, unknown outcomes, no reproduction,
    both arms failing, and an informative comparison. A verdict label or a zero
    success count alone cannot establish which case occurred. State the without-rule
    arm's role explicitly: it must reproduce the mistake before a comparison can
    show whether the rule helps.
    """
    if row is None:
        return {}
    raw = row["metrics_json"] if "metrics_json" in row.keys() else ""
    # Attribute a parse error to its row before validating the decoded shape.
    try:
        metrics = json.loads(raw) if raw else {}
    except (TypeError, ValueError) as exc:
        raise DashboardDataError(
            f"eval_results {row['id']!r}: metrics_json is not valid JSON: {exc}"
        ) from exc
    if not isinstance(metrics, dict):
        raise DashboardDataError(
            f"eval_results {row['id']!r}: metrics_json is not an object, it is "
            f"{type(metrics).__name__}"
        )

    without, with_ = _arm(metrics, "without"), _arm(metrics, "with")
    attempted = int(row["attempted"] or 0)
    succeeded = int(row["succeeded"] or 0)
    trials = without.get("attempted", 0) + with_.get("attempted", 0)

    try:
        taxonomy = json.loads(row["error_taxonomy_json"] or "{}")
    except (TypeError, ValueError) as exc:
        raise DashboardDataError(
            f"eval_results {row['id']!r}: error_taxonomy_json is not valid "
            f"JSON: {exc}"
        ) from exc
    if not isinstance(taxonomy, dict):
        raise DashboardDataError(
            f"eval_results {row['id']!r}: error_taxonomy_json is not an "
            f"object, it is {type(taxonomy).__name__}"
        )
    causes = set(taxonomy)
    unknown = causes - EVAL_INFRA_ERRORS - EVAL_JUDGED_ERRORS

    if eval_no_trial_ran(row, taxonomy=taxonomy):
        shape = "no_trial_ran"
        summary = (
            f"No trial ran. The eval agent errored on all {attempted} "
            "attempts, so nothing about this rule was tested."
        )
    elif succeeded == 0 and attempted > 0 and unknown:
        # Say what was recorded, name no cause. A key nobody wrote copy for is
        # exactly where a confident wrong sentence gets written.
        shape = "unknown_outcome"
        summary = (
            f"No trial succeeded in {attempted} attempts. The recorded "
            f"outcomes are {', '.join(sorted(causes))}, which this view has no "
            "plain-language reading for. Read the trial directory below."
        )
    elif not without and not with_:
        shape = "no_arms"
        summary = "The eval recorded no trial arms, so nothing was compared."
    elif without.get("failed", 0) == 0 and without.get("attempted", 0) > 0:
        shape = "never_reproduced"
        summary = (
            f"The mistake never happened without the rule: the without-rule "
            f"arm passed {without['succeeded']} of {without['attempted']} "
            "trials. With nothing to fix, the trial cannot show this rule "
            "helps. That is not evidence against it."
        )
    elif with_.get("succeeded", 0) == 0 and without.get("succeeded", 0) == 0:
        shape = "both_arms_failed"
        summary = (
            f"Both arms failed: 0 of {with_.get('attempted', 0)} passed with "
            f"the rule and 0 of {without.get('attempted', 0)} without it. The "
            "eval could not reproduce the situation, so this is a harness "
            "result, not a judgement about the rule."
        )
    else:
        shape = "tested"
        summary = (
            f"Without the rule the agent made the mistake "
            f"{without.get('failed', 0)} of {without.get('attempted', 0)} "
            f"times. With the rule it passed {with_.get('succeeded', 0)} of "
            f"{with_.get('attempted', 0)}."
        )

    return {
        "id": row["id"],
        "verdict": row["verdict"],
        "shape": shape,
        "summary": summary,
        "attempted": attempted,
        "succeeded": succeeded,
        "failed": int(row["failed"] or 0),
        "trials": trials,
        "without": without,
        "with": with_,
        "trials_dir": without.get("transcripts_dir") or with_.get("transcripts_dir") or "",
        "finished": row["finished"] if "finished" in row.keys() else "",
    }


def _provenance(store, learning_ids: list[str]) -> dict:
    """Derive each lesson's observed time range from incident timestamps.

    Legacy learning summaries can carry stale or reversed ranges. Reading
    ``incidents.ts`` gives old and new rows the same evidence source without a
    backfill. Return transcript time in UTC and label it explicitly.
    """
    if not learning_ids:
        return {}
    from . import evidence_identity as identity
    identities=identity.metadata_index(store)
    slots = ",".join("?" * len(learning_ids))
    rows = store.query(
        "SELECT il.learning_id AS lid, "
        "       MIN(i.ts) AS first_seen, MAX(i.ts) AS last_seen, "
        "       COUNT(*) AS incident_count, "
        "       COUNT(DISTINCT i.project_path) AS path_count "
        "  FROM incident_learnings il "
        "  JOIN incidents i ON i.id = il.incident_id "
        f" WHERE il.learning_id IN ({slots}) AND i.ts <> '' "
        " GROUP BY il.learning_id",
        tuple(learning_ids),
    )
    out = {
        r["lid"]: {
            "first_seen": r["first_seen"],
            "last_seen": r["last_seen"],
            "incident_count": int(r["incident_count"]),
            "session_count": 0,
            "path_count": int(r["path_count"]),
            "sources": [],
            "examples": [],
            "examples_held_back": 0,
        }
        for r in rows
    }

    linked=store.query(
        'SELECT il.learning_id AS lid,i.* FROM incident_learnings il '
        'JOIN incidents i ON i.id=il.incident_id '+f'WHERE il.learning_id IN ({slots}) ORDER BY i.id',
        tuple(learning_ids))
    for lid,bucket in out.items():
        evidence=[r for r in linked if r['lid']==lid]
        summary=identity.session_summary(identities,[r for r in evidence if r['ts']])
        bucket.update(session_count=summary['known_session_count'],
                      unknown_session_records=summary['unknown_session_records'],
                      unknown_source_incidents=sum(not identity.session_ref(identities,r)['provider'] for r in evidence))
        bucket['sources']=sorted({identity.session_ref(identities,r)['provider'] for r in evidence}-{''})

    # Example incidents, newest first. Report each family's cap alongside the
    # sample so readers can distinguish displayed incidents from total evidence.
    for r in store.query(
        "SELECT il.learning_id AS lid, i.id, i.ts, i.project_path, i.project_key, i.session_id, i.session_file, "
        "       i.signal_type, i.matched_text, i.window_json "
        "  FROM incident_learnings il "
        "  JOIN incidents i ON i.id = il.incident_id "
        f" WHERE il.learning_id IN ({slots}) AND i.ts <> '' "
        " ORDER BY i.ts DESC",
        tuple(learning_ids),
    ):
        bucket = out.get(r["lid"])
        if bucket is None:
            continue
        if len(bucket["examples"]) >= REVIEW_INCIDENT_EXAMPLES:
            bucket["examples_held_back"] += 1
            continue
        bucket["examples"].append(
            {
                "id": r["id"],
                "ts": r["ts"],
                "project_path": r["project_path"],
                "signal_type": r["signal_type"],
                "matched_text": r["matched_text"] or "",
                "presentation": normalize_incident({**r, 'identity':identity.identity(identities,r)}),
            }
        )
    return out


def _line_budget(cfg, target_path: str, pending: int) -> dict:
    """How full the target instruction file is, and what this card would add.

    Only the global CLAUDE.md has a declared budget
    (``cfg.global_claude_md_line_budget``); for any other target this returns
    ``{}`` rather than inventing a denominator. A budget bar drawn against a
    made-up cap is the "invented number" failure AGENTS.md records.

    A missing or unreadable target is reported, never defaulted to zero: a
    bar reading 0/250 on a file that does not exist says "plenty of room".
    """
    global_path = str(_cfg_attr(cfg, "global_claude_md"))
    if not target_path or os.path.abspath(target_path) != os.path.abspath(global_path):
        return {}
    budget = int(_cfg_attr(cfg, "global_claude_md_line_budget"))
    try:
        # Proposal generation budgets all lines, including blank ones.
        used = len(Path(global_path).read_text(encoding="utf-8").splitlines())
    except OSError as exc:
        return {
            "path": global_path,
            "budget": budget,
            "used": None,
            "pending": pending,
            "error": f"could not read the target file: {exc}",
        }
    return {
        "path": global_path,
        "budget": budget,
        "used": used,
        "pending": pending,
        "over": used + pending > budget,
    }


def review_queue(store, cfg) -> dict:
    """Every proposal that needs a person, grouped by the lesson behind it.

    Grouping is ``proposals.learning_id`` — one lesson, N targets — which is
    what PRD §7 V3 means by "grouped by lesson, not by proposal". It is NOT
    ``rule_families``: that groups learnings through valid ``duplicate_of``
    references and answers a different question.

    Membership uses ``waiting_proposals``, the same resolver behind the inbox
    count. It includes class policy, review-only runs, mandatory review actions,
    and rejection scope. Independent membership rules could make the views disagree.
    """
    carve_outs = tuple(sorted(set(_cfg_attr(cfg, "review_queue_actions")) | MANDATORY_REVIEW_ACTIONS))
    from ..commands import review_content, review_content_revision
    waiting = {row["id"]: row for row in waiting_proposals(store, cfg)}
    rows = store.query(
        "SELECT p.id, p.learning_id, p.status, p.target_path, p.target_kind, "
        "       p.action, p.diff_unified, p.created_at, p.eval_result_id, "
        "       l.title, l.rule_text, l.why, l.evidence_count, l.project_count, "
        "       l.incident_summary, l.category, l.scope "
        "  FROM proposals p JOIN learnings l ON l.id=p.learning_id "
        " ORDER BY p.created_at, p.id"
    )
    rows = [row for row in rows if row["id"] in waiting]

    families: dict[str, dict] = {}
    for row in rows:
        execution = waiting[row['id']]['execution']
        if execution['reason'] == 'fresh_approval_required':
            code, copy, carve = execution['reason'], execution['detail'], True
        else:
            code, copy, carve = why_needs_you(
                row["status"], action=row["action"], cfg=cfg
            )
        if row["status"] == "gated_pass" and not carve:
            copy = waiting[row["id"]]["execution"]["detail"]
        fam = families.setdefault(
            row["learning_id"],
            {
                "learning_id": row["learning_id"],
                "title": row["title"],
                "rule_text": row["rule_text"],
                "why": row["why"],
                "evidence_count": row["evidence_count"],
                "project_count": row["project_count"],
                "incident_summary": row["incident_summary"] or "",
                "category": row["category"] or "",
                "scope": row["scope"] or "",
                "proposals": [],
                "targets": [],
            },
        )
        fam["proposals"].append(
            {
                "id": row["id"],
                "content_revision": review_content_revision(review_content(store, row["id"])),
                "status": row["status"],
                "target_path": row["target_path"],
                "target_kind": row["target_kind"],
                "action": row["action"],
                "diff_unified": row["diff_unified"],
                "eval_result_id": row["eval_result_id"],
                "created_at": row["created_at"],
                "reason_code": code,
                "why_needs_you": copy,
                "carve_out": carve,
            }
        )
        if row["target_path"] not in fam["targets"]:
            fam["targets"].append(row["target_path"])

    out_families = []
    for fam in families.values():
        fam["size"] = len(fam["proposals"])
        fam["reason_codes"] = sorted({p["reason_code"] for p in fam["proposals"]})
        # A carve-out outranks a verdict in the lead line: it is the reason the
        # item can never auto-apply, while a verdict only says it did not.
        lead = next(
            (p for p in fam["proposals"] if p["carve_out"]), fam["proposals"][0]
        )
        fam["lead_reason"] = lead["why_needs_you"]
        fam["has_carve_out"] = any(p["carve_out"] for p in fam["proposals"])
        out_families.append(fam)
    out_families.sort(key=lambda f: (-f["size"], f["learning_id"]))

    # Decision support: expose when the evidence occurred, the competing evidence,
    # and the underlying example alongside each review item.
    prov = _provenance(store, [f["learning_id"] for f in out_families])
    eval_ids = sorted(
        {
            p["eval_result_id"]
            for f in out_families
            for p in f["proposals"]
            if p["eval_result_id"]
        }
    )
    evals: dict[str, object] = {}
    if eval_ids:
        evals = {
            r["id"]: r
            for r in store.query(
                "SELECT * FROM eval_results WHERE id IN ("
                + ",".join("?" * len(eval_ids))
                + ")",
                tuple(eval_ids),
            )
        }
    for fam in out_families:
        fam["provenance"] = prov.get(fam["learning_id"], {})
        for proposal in fam["proposals"]:
            proposal["eval"] = eval_story(evals.get(proposal["eval_result_id"]))
        # Use the lead proposal's eval for the family headline. Flag disagreement
        # so separately evaluated proposals do not appear to share one verdict.
        stories = [p["eval"] for p in fam["proposals"] if p["eval"]]
        fam["eval"] = stories[0] if stories else {}
        fam["eval_verdicts"] = sorted({s["verdict"] for s in stories})
        fam["evals_disagree"] = len(fam["eval_verdicts"]) > 1
        fam["means"] = MEANS_QUEUED.get(
            fam["proposals"][0]["reason_code"], ""
        ) if not fam["has_carve_out"] else ""
        # Targets with their multiplicity: N proposals writing one file is one
        # write after approval, and the card has to say so or the operator
        # reads N pending edits to the same line.
        tally: dict[str, int] = {}
        kinds: dict[str, str] = {}
        for proposal in fam["proposals"]:
            tally[proposal["target_path"]] = tally.get(proposal["target_path"], 0) + 1
            kinds[proposal["target_path"]] = proposal["target_kind"]
        fam["target_rows"] = [
            {
                "path": path,
                "count": n,
                "kind": kinds[path],
                "note": (
                    "the same lesson proposed %d times into this file; "
                    "approving writes it once" % n
                )
                if n > 1
                else "",
            }
            for path, n in sorted(tally.items(), key=lambda kv: (-kv[1], kv[0]))
        ]
        fam["budget"] = _line_budget(
            cfg, fam["target_rows"][0]["path"] if fam["target_rows"] else "",
            fam["target_rows"][0]["count"] if fam["target_rows"] else 0,
        )

    counts = _proposal_status_counts(store)
    known = KNOWN_STATUSES
    queued_ids = {p["id"] for f in out_families for p in f["proposals"]}
    # The nav badge, the overview sentence and this view must be the same
    # number. Asserted here rather than hoped for: a family grouping that
    # dropped or duplicated a proposal would show up as a mismatch.
    authoritative = set(waiting_proposal_ids(store, cfg))
    if queued_ids != authoritative:
        raise DashboardDataError(
            "the review queue holds a different set of proposals than "
            f"waiting_proposal_ids: {len(queued_ids)} grouped vs "
            f"{len(authoritative)} waiting"
        )
    automatic = [p for p in proposal_dispositions(store, cfg) if p["next_step"] == "automatic_delivery"]
    auto_pending = len(automatic)
    no_trial_ran = sum(eval_no_trial_ran(store.query_one(
        "SELECT * FROM eval_results WHERE id=?", (p["eval_result_id"],))) for p in automatic)
    threshold = int(_cfg_attr(cfg, "global_promotion_min_projects"))
    global_by_scope_guess = sum(1 for p in automatic if p["target_kind"] == "global_claude_md" and store.query_one(
        "SELECT id FROM learnings WHERE id=? AND project_count<?", (p["learning_id"], threshold)))
    run, stats = _latest_run_stats(store)
    review_only = bool(stats["review_only"]) if "review_only" in stats else None
    note = f"{auto_pending} passing proposals await automatic delivery." if auto_pending else ""
    review_no_trial = 0
    review_global_guess = 0
    for p in waiting.values():
        ev = store.query_one("SELECT * FROM eval_results WHERE id=?", (p["eval_result_id"],)) if p["eval_result_id"] else None
        if ev is not None and eval_story(ev)["shape"] == "no_trial_ran":
            review_no_trial += 1
        if p["target_kind"] == "global_claude_md" and store.query_one(
            "SELECT id FROM learnings WHERE id=? AND project_count<?", (p["learning_id"], threshold)
        ):
            review_global_guess += 1
    if review_no_trial:
        note += f" In Review, {review_no_trial} proposals rest on an eval where no trial ran."
    if review_global_guess:
        note += f" In Review, {review_global_guess} proposals target the global file via scope_guess rather than project-count promotion."
    note = note.strip()

    return {
        "profile": "review-content/1",
        "families": out_families,
        "family_count": len(out_families),
        "count": sum(f["size"] for f in out_families),
        "queueing_statuses": list(QUEUEING_STATUSES),
        "carve_out_actions": list(carve_outs),
        "carve_out_summary": {
            action: len(
                [
                    p
                    for f in out_families
                    for p in f["proposals"]
                    if p["action"] == action
                ]
            )
            for action in carve_outs
        },
        "incident_examples_cap": REVIEW_INCIDENT_EXAMPLES,
        "auto_apply_pending": auto_pending,
        "auto_apply_no_trial_ran": no_trial_ran,
        "auto_apply_global_by_scope_guess": global_by_scope_guess,
        "auto_apply_note": note,
        "review_no_trial_ran": review_no_trial,
        "review_global_by_scope_guess": review_global_guess,
        "review_only": review_only,
        "unknown_statuses": {k: v for k, v in counts.items() if k not in known},
        "empty_state": (
            "Nothing needs you. Every proposal has a decision or qualifies for automatic delivery."
        ),
    }


# ---------------------------------------------------------------------------
# V1 — backlog: two competing rates, never a countdown
# ---------------------------------------------------------------------------


def backlog(store, cfg, *, window_days: int = 14) -> dict:
    """Compare queued incidents, arrival rate, and mining capacity.

    Do not infer a countdown to an empty queue: arrivals can continue to exceed
    capacity. End the arrival window at the last data day, not today, and report
    staleness separately through ``data_freshness``.
    """
    by_status = {
        row["status"]: int(row["n"])
        for row in store.query("SELECT status, COUNT(*) AS n FROM incidents GROUP BY status")
    }
    queued = by_status.get("new", 0)

    row = store.query_one("SELECT MAX(ts) AS last_ts FROM incidents WHERE ts <> ''") or {}
    last_ts = row.get("last_ts") or ""
    per_day_counts: dict[str, int] = {}
    # Declared before the branch below, which the empty-database path skips.
    excluded_partial_day: dict | None = None
    rate_days: list[str] = []
    if last_ts:
        last_day = _day(last_ts)
        window = _day_range(last_day, window_days)
        first_day = window[0]
        rows = store.query(
            "SELECT substr(ts, 1, 10) AS day, COUNT(*) AS n FROM incidents "
            "WHERE substr(ts, 1, 10) >= ? AND substr(ts, 1, 10) <= ? "
            "GROUP BY substr(ts, 1, 10)",
            (first_day, last_day),
        )
        counted = {r["day"]: int(r["n"]) for r in rows}
        per_day_counts = {day: counted.get(day, 0) for day in window}
        # Exclude the last covered day because a scan can stop partway through it.
        # Report that exclusion so a partial day cannot silently depress the daily rate.
        rate_days = list(window)
        if len(rate_days) > 1:
            excluded_partial_day = {
                "day": rate_days[-1],
                "incidents": per_day_counts[rate_days[-1]],
                "why": "the newest data day is cut short by when the scan ran, "
                       "so averaging it against full days understates the rate",
            }
            rate_days = rate_days[:-1]
        total = sum(per_day_counts.values())
        rate_total = sum(per_day_counts[d] for d in rate_days)
        arrivals_per_day = rate_total / len(rate_days) if rate_days else None
        window_start, window_end = window[0], window[-1]
    else:
        total = 0
        arrivals_per_day = None
        window_start = window_end = None

    capacity = int(_cfg_attr(cfg, "max_cheap_calls_per_run"))
    return {
        "queued": queued,
        "incidents_by_status": by_status,
        "mine_capacity_per_run": capacity,
        "capacity_unit": "model_calls_per_run",
        "capacity_note": "Configured mining-model call slots per run, not successful incidents. Calls can fail, retry or serve other mining-pool stages.",
        "mine_order": _cfg_attr(cfg, "mine_order"),
        "arrivals": {
            "per_day": arrivals_per_day,
            "total_in_window": total,
            # Every cut this module makes is reported with what it cut.
            "excluded_partial_day": excluded_partial_day if window_days else None,
            "rate_days": len(rate_days) if window_days else 0,
            "window_days": window_days,
            "window_start": window_start,
            "window_end": window_end,
            "window_ends_at": "last data day",
            "days_with_zero": sum(1 for n in per_day_counts.values() if n == 0),
            "per_day_counts": per_day_counts,
            "source_column": "incidents.ts",
            "why_not_created_at": (
                "created_at is scan time; a rebuild can insert historical incidents "
                "together, so arrival rates use the source event timestamps"
            ),
        },
        "net_per_day": None,
        "race": (
            "Transcript-time arrivals and execution-time mining use different windows. "
            "Model-call slots are not incident throughput; no net daily rate is inferred."
        ),
    }


def _cfg_attr(cfg, name: str):
    """Read a config value or raise — a missing key never defaults."""
    if not hasattr(cfg, name):
        raise DashboardDataError(f"config has no attribute {name!r}")
    return getattr(cfg, name)


# ---------------------------------------------------------------------------
# V1 — failure panel (D3)
# ---------------------------------------------------------------------------


def failure_panel(store) -> dict:
    """Failures in plain language, split by whether they still happen.

    Two sources, because neither alone is complete: ``llm_calls.outcome`` (per
    call) and ``runs.stats_json[stage].taxonomy`` (per stage, and the only
    place ``IntegrityError`` was ever recorded). Budget keys are not failures
    and are reported separately; a class with no copy is listed in
    ``unknown_classes`` rather than dropped.
    """
    runs = store.query("SELECT id, started, status, stats_json FROM runs ORDER BY started, id")
    run_days = [_day(r["started"]) for r in runs if r["started"]]
    latest_day = max(run_days) if run_days else None
    recent_from = (
        (date.fromisoformat(latest_day) - timedelta(days=RECENT_FAILURE_WINDOW_DAYS - 1)).isoformat()
        if latest_day
        else None
    )

    totals: dict[str, int] = {}
    recent: dict[str, int] = {}
    last_seen: dict[str, dict] = {}
    after_fix: dict[str, list[dict]] = {}
    not_failures: dict[str, int] = {}
    stages_seen: dict[str, set] = {}

    for run in runs:
        stats = _json_obj(run["stats_json"], f"runs.{run['id']}.stats_json")
        started = run["started"]
        day = _day(started)
        for stage, payload in stats.items():
            if not isinstance(payload, dict):
                continue
            if 'taxonomy' not in payload:
                continue
            try:
                descriptions = taxonomy_rows(payload['taxonomy'], owner=f"run {run['id']}.{stage}.taxonomy")
            except ValueError as exc:
                raise DashboardDataError(str(exc)) from exc
            for description in descriptions:
                cls, count = description['class'], description['count']
                if cls in NOT_FAILURE_TAXONOMY:
                    not_failures[cls] = not_failures.get(cls, 0) + count
                    continue
                totals[cls] = totals.get(cls, 0) + count
                stages_seen.setdefault(cls, set()).add(stage)
                if recent_from and day >= recent_from:
                    recent[cls] = recent.get(cls, 0) + count
                last_seen[cls] = {"run_id": run["id"], "started": started, "count": count}
                fixed = FIXED_FAILURES.get(cls)
                if fixed and started > fixed["fixed_at"]:
                    after_fix.setdefault(cls, []).append(
                        {"run_id": run["id"], "started": started, "count": count}
                    )

    llm_rows = store.query(
        "SELECT run_id, stage, outcome, COUNT(*) AS n FROM llm_calls GROUP BY run_id, stage, outcome"
    )
    run_by_id = {run['id']: run for run in runs}
    unlinked_failed_calls = 0
    llm_by_stage: dict[str, dict] = {}
    for row in llm_rows:
        bucket = llm_by_stage.setdefault(
            row["stage"], {"attempted": 0, "succeeded": 0, "failed": 0, "by_outcome": {}}
        )
        n = int(row["n"])
        bucket["attempted"] += n
        bucket["by_outcome"][row["outcome"]] = bucket["by_outcome"].get(row["outcome"], 0) + n
        if row["outcome"] in LLM_SUCCESS_OUTCOMES:
            bucket["succeeded"] += n
        else:
            bucket["failed"] += n
            totals[row["outcome"]] = totals.get(row["outcome"], 0) + n
            stages_seen.setdefault(row["outcome"], set()).add(row["stage"])
            owner = run_by_id.get(row['run_id'])
            if owner is None:
                unlinked_failed_calls += n
                continue
            cls = row['outcome']
            if recent_from and _day(owner['started']) >= recent_from:
                recent[cls] = recent.get(cls, 0) + n
            prior = last_seen.get(cls)
            if prior is None or (owner['started'], owner['id']) >= (prior['started'], prior['run_id']):
                last_seen[cls] = {'run_id': owner['id'], 'started': owner['started'], 'count': n}
            fixed = FIXED_FAILURES.get(cls)
            if fixed and owner['started'] > fixed['fixed_at']:
                after_fix.setdefault(cls, []).append({'run_id': owner['id'], 'started': owner['started'], 'count': n})

    open_rows, fixed_rows, quiet_rows, regressed_rows, unknown = [], [], [], [], []
    for cls in sorted(totals):
        copy = failure_copy(cls)
        if copy is None:
            unknown.append({"class": cls, "count": totals[cls]})
        entry = {
            "class": cls,
            "name": copy["name"] if copy else "Unclassified outcome",
            "explanation": copy["explanation"] if copy else "No explanation is registered for this recorded identifier.",
            "has_copy": copy is not None,
            "stages": sorted(stages_seen.get(cls, ())),
            "total": totals[cls],
            "recent": recent.get(cls, 0),
            "recent_window_days": RECENT_FAILURE_WINDOW_DAYS,
            "recent_window_from": recent_from,
            "last_seen": last_seen.get(cls),
        }
        fixed = FIXED_FAILURES.get(cls)
        if fixed and cls in after_fix:
            entry.update(status="regressed", **fixed, occurrences_after_fix=after_fix[cls])
            regressed_rows.append(entry)
        elif fixed:
            entry.update(status="fixed", **fixed)
            fixed_rows.append(entry)
        elif entry["recent"]:
            entry["status"] = "open"
            open_rows.append(entry)
        else:
            entry["status"] = "quiet"
            quiet_rows.append(entry)

    return {
        "open": sorted(open_rows, key=lambda e: -e["recent"]),
        "quiet": sorted(quiet_rows, key=lambda e: -e["total"]),
        "fixed": fixed_rows,
        "regressed": regressed_rows,
        "unknown_classes": unknown,
        "not_failures": [
            {"class": cls, "count": count, "why": NOT_FAILURE_TAXONOMY[cls]}
            for cls, count in sorted(not_failures.items())
        ],
        "llm_by_stage": llm_by_stage,
        "success_outcomes": list(LLM_SUCCESS_OUTCOMES),
        "latest_run_day": latest_day,
        "retained_runs": len(runs),
        "retained_calls": sum(row['n'] for row in llm_rows),
        "unlinked_failed_calls": unlinked_failed_calls,
        "recent_window_from": recent_from,
        "note": (
            "Counts combine stage taxonomy entries and call outcomes; they are not deduplicated incidents. "
            "Recent means the seven calendar days ending on the latest retained run day, using explicit run ownership. "
            "Unlinked calls have unknown run coverage. Retained records do not prove complete execution history."
        ),
    }


# ---------------------------------------------------------------------------
# Gate health — three different things that all look like a bad verdict
# ---------------------------------------------------------------------------


def classify_eval_result(row: dict) -> dict:
    """"The rule failed" vs "the eval was invalid" vs "the harness broke".

    A verdict is only about the rule if the harness got out of the way
    (AGENTS.md). Order matters: a harness break can wear any verdict, including
    ``gated_fail``, and must never be recorded as a judgement about the rule.
    """
    metrics = _json_obj(row.get("metrics_json"), f"eval_results.{row.get('id')}.metrics_json")
    taxonomy = _json_obj(
        row.get("error_taxonomy_json"), f"eval_results.{row.get('id')}.error_taxonomy_json"
    )
    verdict = _require(row, "verdict", f"eval_results.{row.get('id')}")
    agent_errors = int(taxonomy.get("agent_error", 0))
    failed = int(_require(row, "failed", f"eval_results.{row.get('id')}"))
    with_arm = metrics.get("with")
    without_arm = metrics.get("without")

    if failed > 0 and agent_errors * 2 >= failed:
        return {
            "class": "harness_broken",
            "label": "The harness broke",
            "why": (
                f"{agent_errors} of {failed} failed trials were agent_error — the "
                "agent could not run, so this says nothing about the rule"
            ),
            "verdict": verdict,
            "agent_errors": agent_errors,
            "failed": failed,
        }
    if with_arm is None:
        return {
            "class": "eval_invalid",
            "label": "The eval could not reproduce the mistake",
            "why": (
                "the with-rule arm never ran, so the rule was never tested; the "
                "without-rule arm did not fail"
            ),
            "verdict": verdict,
            "without_arm": bool(without_arm),
        }
    if verdict == "gated_pass":
        label, klass = "The rule helped", "rule_helped"
        why = "both arms ran and the with-rule arm passed a majority with no failures"
    elif verdict == "gated_fail":
        label, klass = "The rule failed", "rule_failed"
        why = "both arms ran and the with-rule arm lost"
    elif verdict == "inconclusive":
        label, klass = "The scenarios disagreed", "inconclusive"
        why = "the majority gate's scenarios disagreed, so the proposal is held"
    else:
        label, klass = "Unclassified verdict", "unclassified"
        why = f"verdict {verdict!r} with both arms present has no classification"
    return {"class": klass, "label": label, "why": why, "verdict": verdict}


def gate_health(store) -> dict:
    """What the gate has actually said, and about whom.

    ``eval_results.subject_id`` can identify a seed scenario or a learning.
    Link proposal results explicitly. Return counts at every sample size;
    a pass rate alone would hide how much evaluation occurred.
    """
    rows = store.query(
        "SELECT id, kind, subject_id, started, finished, attempted, succeeded, "
        "failed, error_taxonomy_json, metrics_json, verdict FROM eval_results "
        "ORDER BY started, id"
    )
    linked = {
        r["eval_result_id"]: r["id"]
        for r in store.query(
            "SELECT id, eval_result_id FROM proposals WHERE eval_result_id <> ''"
        )
    }
    learning_ids = {r["id"] for r in store.query("SELECT id FROM learnings")}

    classes: dict[str, int] = {}
    verdicts: dict[str, int] = {}
    subjects = {"proposal": 0, "learning": 0, "seed": 0, "unknown": 0}
    out_rows = []
    for row in rows:
        info = classify_eval_result(row)
        subject_id = row["subject_id"]
        if row["id"] in linked:
            kind = "proposal"
        elif subject_id in learning_ids:
            kind = "learning"
        elif subject_id.startswith("seed-"):
            kind = "seed"
        else:
            kind = "unknown"
        subjects[kind] += 1
        classes[info["class"]] = classes.get(info["class"], 0) + 1
        verdicts[row["verdict"]] = verdicts.get(row["verdict"], 0) + 1
        out_rows.append(
            {
                "id": row["id"],
                "kind": row["kind"],
                "subject_id": subject_id,
                "subject_kind": kind,
                "proposal_id": linked.get(row["id"], ""),
                "attempted": row["attempted"],
                "succeeded": row["succeeded"],
                "failed": row["failed"],
                "started": row["started"],
                **info,
            }
        )

    proposal_evals = subjects["proposal"]
    running = proposal_evals > 0
    if not running:
        sentence = (
            f"The gate is not running: {len(rows)} eval results exist but none belongs "
            "to a proposal."
        )
    else:
        sentence = (
            f"{proposal_evals} of {len(rows)} eval results belong to a proposal; the "
            f"other {len(rows) - proposal_evals} are seed scenarios and unlinked runs."
        )
    return {
        "eval_rows_total": len(rows),
        "proposal_evals": proposal_evals,
        "seed_evals": subjects["seed"],
        "learning_subject_evals": subjects["learning"],
        "unknown_subject_evals": subjects["unknown"],
        "by_subject_kind": subjects,
        "by_class": classes,
        "by_verdict": verdicts,
        "rows": out_rows,
        "gate_running": running,
        "sentence": sentence,
        "rate_suppressed": {
            "reason": (
                "a pass rate over a gate this small reads as a failing system rather "
                "than a gate that has barely run; the classes are reported instead"
            )
        },
        "verdicts_held": ["gated_fail", "inconclusive", "ungated"],
        "verdicts_auto_applied": list(AUTO_APPLY_STATUSES),
    }


# ---------------------------------------------------------------------------
# Incidents — the two heterogeneous fields (PRD §8c)
# ---------------------------------------------------------------------------


def normalize_incident(row: dict) -> dict:
    """One incident, safe to render.

    A promoted ``repeated_error`` can carry a bare sha1 in ``matched_text``.
    Its window can contain either occurrences or turns. Select the shape from
    the window's keys because the signal type alone does not distinguish them.
    """
    what = f"incidents.{row.get('id')}"
    incident_id = _require(row, "id", what)
    signal_type = _require(row, "signal_type", what)
    matched_text = _require(row, "matched_text", what)
    from ..incident_evidence import present, IncidentEvidenceError
    try:
        evidence = present({**row, 'matched_text': matched_text,
                            'window_json': _require(row, 'window_json', what)},
                           owner=what, max_chars=DISPLAY_TEXT_MAX)
    except IncidentEvidenceError as exc:
        raise DashboardDataError(str(exc)) from exc
    return {'id': incident_id, 'signal_type': signal_type,
            'ts': row.get('ts', ''), 'status': row.get('status', ''),
            'score': row.get('score'), 'session_id': row.get('session_id', ''),
            'project_key': row.get('project_key', ''), **({'identity':row['identity']} if 'identity' in row else {}), **evidence}



# ---------------------------------------------------------------------------
# V2 — Rules
# ---------------------------------------------------------------------------


def rule_families(store) -> dict:
    """Return duplicate families supported by learning references.

    Only duplicate_of values that identify another learning can form these groups.
    Embedding-based grouping requires separate computation. If no valid references
    exist, return an explicit unavailable state and the observed counts.
    """
    learning_vectors = int(
        (
            store.query_one(
                "SELECT COUNT(*) AS n FROM embeddings WHERE owner_kind = ?", ("learning",)
            )
            or {"n": 0}
        )["n"]
    )
    rows = store.query("SELECT id, duplicate_of, title, rule_text FROM learnings")
    # duplicate_of is not a foreign key; the miner can store existing rule text
    # there. Group only values that identify a learning, or arbitrary text would
    # create misleading singleton families.
    known_ids = {r["id"] for r in rows}
    with_text = [r for r in rows if r["duplicate_of"]]
    grouped = [r for r in with_text if r["duplicate_of"] in known_ids]
    if not grouped:
        return {
            "available": False,
            "groups": [],
            "learnings_total": len(rows),
            "learning_embeddings": learning_vectors,
            "learnings_with_duplicate_of": len(with_text),
            "duplicate_of_referencing_a_learning": 0,
            "reason": (
                "Duplicate families are not computable yet. "
                f"{len(with_text)} learning(s) carry a `duplicate_of`, but none of "
                "those values identifies another learning in this table. "
                f"{learning_vectors} learning vectors exist. Similarity-based "
                "grouping requires a separate embedding comparison; this query "
                "only groups explicit learning references."
            ),
            "empty_state": "Family grouping is off. Showing every rule as its own row.",
        }
    families: dict[str, list[str]] = {}
    for row in grouped:
        families.setdefault(row["duplicate_of"], []).append(row["id"])
    return {
        "available": True,
        "groups": [
            {"family_key": key, "learning_ids": sorted(ids), "size": len(ids)}
            for key, ids in sorted(families.items())
        ],
        "learnings_total": len(rows),
        "learning_embeddings": learning_vectors,
        "learnings_with_duplicate_of": len(with_text),
        "duplicate_of_referencing_a_learning": len(grouped),
        "reason": "",
        "source": "learnings.duplicate_of (values that reference a learning id)",
    }


def rules(store, *, evidence_sample: int = 5, learning_ids: list[str] | None = None) -> dict:
    """Every learning with state, target, evidence, provenance and verdict.

    Miner generation comes from immutable content history. Historical or
    subsequently changed content stays unknown; scan time is not mining time.
    """
    from ..mining_history import summaries as mining_summaries, summary as mining_summary
    if learning_ids is not None and (not isinstance(learning_ids,list) or not 1 <= len(learning_ids) <= 50 or not all(isinstance(lid,str) and lid for lid in learning_ids)):
        raise DashboardDataError('Selected rule IDs must be a nonempty list of at most 50 IDs')
    params = tuple(learning_ids or ())
    slots = ','.join('?' for _ in params)
    def selected(column):
        return f" WHERE {column} IN ({slots})" if learning_ids is not None else ''

    learnings = store.query(
        "SELECT id, title, rule_text, why, category, scope, evidence_count, "
        "project_count, projects_json, first_seen, last_seen, confidence, status, "
        "duplicate_of, created_at, incident_summary, source, violated_existing_rule, "
        "path_globs_json, primary_project_path FROM learnings" + selected("id") + " ORDER BY created_at, id", params
    )
    mining={row["id"]:mining_summary(store,row) for row in learnings} if learning_ids is not None else mining_summaries(store,learnings)
    proposals = store.query(
        "SELECT id, learning_id, run_id, target_path, target_kind, action, status, "
        # Include the stored diff for the V2 proposal inspector.
        "eval_result_id, applied_at, created_at, diff_unified "
        "FROM proposals" + selected("learning_id") + " ORDER BY created_at, id", params
    )
    evals = {
        row["id"]: row
        for row in store.query(
            "SELECT id, kind, subject_id, attempted, succeeded, failed, "
            "error_taxonomy_json, metrics_json, verdict, started FROM eval_results" +
            (f" WHERE subject_id IN ({slots}) OR id IN (SELECT eval_result_id FROM proposals WHERE learning_id IN ({slots}))" if learning_ids is not None else ''), params+params
        )
    }
    from . import evidence_identity as identity
    identities=identity.metadata_index(store)
    evidence = store.query(
        "SELECT il.learning_id AS learning_id, i.id AS incident_id, i.signal_type AS signal_type, "
        "i.ts AS ts, i.project_key AS project_key, i.session_id AS session_id, i.session_file AS session_file, "
        "i.matched_text AS matched_text, i.window_json AS window_json, i.status AS status, "
        "i.score AS score "
        "FROM incident_learnings il "
        "JOIN incidents i ON i.id = il.incident_id "
        + selected("il.learning_id") + " ORDER BY il.learning_id, i.ts, i.id", params
    )
    by_learning: dict[str, list[dict]] = {}
    for row in evidence:
        row['identity']=identity.identity(identities,row)
        by_learning.setdefault(row["learning_id"], []).append(row)

    props_by_learning: dict[str, list[dict]] = {}
    for row in proposals:
        props_by_learning.setdefault(row["learning_id"], []).append(row)

    subject_evals: dict[str, list[str]] = {}
    for eid, row in evals.items():
        subject_evals.setdefault(row["subject_id"], []).append(eid)

    families = rule_families(store) if learning_ids is None else None
    out = []
    for learning in learnings:
        lid = learning["id"]
        evidence_rows = by_learning.get(lid, [])
        projects=[identity.project_ref(identities,key) for key in sorted({r['project_key'] for r in evidence_rows}-{''})]
        repos=[p['label'] for p in projects]
        sessions=identity.session_summary(identities,evidence_rows)
        agents=sorted({r['identity']['session']['provider'] for r in evidence_rows}-{''})
        sessions_seen=[r['native_session_id'] for r in sessions['sessions'] if r['identity_kind']=='native_session']
        sample = [normalize_incident(r | {"id": r["incident_id"]}) for r in evidence_rows[:evidence_sample]]
        sample_cut = max(0, len(evidence_rows) - len(sample))

        learning_props = props_by_learning.get(lid, [])
        verdicts = []
        for prop in learning_props:
            eval_row = evals.get(prop["eval_result_id"]) if prop["eval_result_id"] else None
            verdicts.append(
                {
                    "proposal_id": prop["id"],
                    "target_path": prop["target_path"],
                    "target_kind": prop["target_kind"],
                    "action": prop["action"],
                    "proposal_status": prop["status"],
                    # An empty diff column and a diff nobody fetched are
                    # different facts, and the reader cannot tell them apart
                    # from a blank panel. Say which this is.
                    "diff": prop["diff_unified"] or "",
                    "diff_reason": (
                        ""
                        if prop["diff_unified"]
                        else "this proposal has no diff stored; propose.py "
                             "records one for every edit it generates, so an "
                             "empty column means the proposal never reached "
                             "diff generation"
                    ),
                    "applied_at": prop["applied_at"],
                    "eval": (
                        {
                            "id": eval_row["id"],
                            "verdict": eval_row["verdict"],
                            "attempted": eval_row["attempted"],
                            "succeeded": eval_row["succeeded"],
                            "failed": eval_row["failed"],
                            **classify_eval_result(eval_row),
                        }
                        if eval_row
                        else None
                    ),
                }
            )
        gated = [v for v in verdicts if v["eval"]]
        # An eval whose subject_id is this learning but which no proposal
        # points at is reported, not folded in: the authoritative link is
        # proposals.eval_result_id.
        unlinked = [
            eid
            for eid in subject_evals.get(lid, [])
            if eid not in {p["eval_result_id"] for p in learning_props}
        ]

        violated = learning["violated_existing_rule"]
        out.append(
            {
                "id": lid,
                "title": learning["title"],
                "rule_text": learning["rule_text"],
                "why": learning["why"],
                "category": learning["category"],
                "scope": learning["scope"],
                "status": learning["status"],
                "confidence": learning["confidence"],
                "evidence_count": learning["evidence_count"],
                "evidence_linked": len(evidence_rows),
                "project_count": len({r["project_key"] for r in evidence_rows if r["project_key"]}) if learning_ids is not None else learning["project_count"],
                **({"recorded_project_count":learning["project_count"], "unknown_project_incidents":sum(not r["project_key"] for r in evidence_rows)} if learning_ids is not None else {}),
                "projects": _json_arr(learning["projects_json"], f"learnings.{lid}.projects_json"),
                "path_globs": _json_arr(
                    learning["path_globs_json"], f"learnings.{lid}.path_globs_json"
                ),
                "primary_project_path": learning["primary_project_path"],
                "first_seen": learning["first_seen"],
                "last_seen": learning["last_seen"],
                "created_at": learning["created_at"],
                "incident_summary": learning["incident_summary"],
                "targets": verdicts,
                "target_summary": _target_summary(verdicts, learning["scope"]),
                "gate_verdict": gated[-1]["eval"]["verdict"] if gated else None,
                "gate_class": gated[-1]["eval"]["class"] if gated else None,
                "gate_verdict_source": "proposals.eval_result_id" if gated else "none",
                "unlinked_subject_evals": unlinked,
                "miner_generation": mining[lid],
                "provenance": {
                    "repos": repos, "projects":projects,
                    **{k:v for k,v in sessions.items() if k!='sessions'},
                    "sessions":sessions['sessions'][:evidence_sample],
                    "session_records_total":len(sessions['sessions']),
                    "session_records_cut":max(0,len(sessions['sessions'])-evidence_sample),
                    "learning_source":identity.source_product(learning['source']),
                    "agent_product": learning["source"],
                    "agents_in_evidence": agents,
                    "session_ids": sessions_seen[:evidence_sample],
                    "session_ids_total": len(sessions_seen),
                    "incidents": sample,
                    "incidents_shown": len(sample),
                    "incidents_total": len(evidence_rows),
                    "incidents_cut": sample_cut,
                    "cut_reason": (
                        f"evidence sample capped at {evidence_sample} incidents per rule"
                        if sample_cut
                        else ""
                    ),
                },
                "enforcement_gap": {
                    "violated_existing_rule": violated,
                    "flagged": bool(violated),
                    "label": (
                        "The miner reported an existing rule. Agent receipt and violation are unverified."
                        if violated
                        else "No violation report was retained. Prior rule receipt and violation are unknown."
                    ),
                },
            }
        )
    return {
        "rows": out,
        "count": len(out),
        "grouping": families,
        "by_status": _count_by(out, "status"),
        "evidence_sample": evidence_sample,
    }


def _target_summary(verdicts: list[dict], scope: str) -> str:
    if not verdicts:
        return f"not routed yet (scope: {scope or 'unset'})"
    paths = sorted({v["target_path"] for v in verdicts})
    if len(paths) == 1:
        return paths[0]
    return f"{len(paths)} targets"


def _count_by(rows: list[dict], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row[key]] = counts.get(row[key], 0) + 1
    return counts


# ---------------------------------------------------------------------------
# V4 — Projects
# ---------------------------------------------------------------------------


def _import_context_weight():
    try:
        from .context_weight import context_weight  # noqa: PLC0415
    except ImportError as exc:
        return None, f"src/self_improve/dashboard/context_weight.py is not importable ({exc})"
    return context_weight, ""


# Separate repository identity methods from path-only or unresolved methods.
# Only a repository is an actionable project under the routing contract. Tests
# reconcile both method groups with project_identity.METHODS so new methods
# cannot be silently included or excluded.
REPO_METHODS = ("gh_repo_id", "remote_url", "git_root")
NON_REPO_METHODS = ("path", "unresolved")


def projects(
    store,
    *,
    weigh=None,
    isdir=None,
    weigh_top_n: int | None = None,
    now_utc=None,
) -> dict:
    """One row per repo, clones collapsed on ``sessions.project_key``.

    Collapsing on ``project_path`` is a correctness bug, not a display
    preference: multiple working copies are one repo, and routing
    promotes a lesson to the global file at ``project_count >= 3``, so counting
    cwds would let one repo escalate its own rule.
    """
    session_rows = store.query(
        "SELECT project_key, project_display, project_key_method, project_path, "
        "COUNT(*) AS n, SUM(lines_scanned) AS lines FROM sessions "
        "GROUP BY project_key, project_display, project_key_method, project_path"
    )
    # PRD D2: a project IS a repository. Directories an agent merely ran in are
    # not projects, and `routing` refuses to write into them, so they can never
    # be where actionable friction lives. Excluded here and REPORTED below.
    # Counted by KEY, not by row: `session_rows` is grouped by
    # (key, display, method, path), so one repo-less directory reached by two
    # paths is two rows and one excluded project. Count distinct keys in both
    # the total and the per-method breakdown so those values reconcile.
    excluded_keys_by_method: dict[str, set[str]] = {}
    kept_rows = []
    for row in session_rows:
        method = row["project_key_method"]
        if method in NON_REPO_METHODS:
            excluded_keys_by_method.setdefault(method, set()).add(row["project_key"])
            continue
        kept_rows.append(row)
    session_rows = kept_rows
    excluded_by_method = {m: len(k) for m, k in sorted(excluded_keys_by_method.items())}
    excluded_keys = set().union(*excluded_keys_by_method.values()) if excluded_keys_by_method else set()
    incident_rows = store.query(
        "SELECT project_key, COUNT(*) AS n FROM incidents GROUP BY project_key"
    )
    signal_rows = store.query(
        "SELECT project_key, signal_type, COUNT(*) AS n FROM incidents "
        "GROUP BY project_key, signal_type"
    )
    rule_rows = store.query(
        "SELECT i.project_key AS project_key, il.learning_id AS learning_id "
        "FROM incident_learnings il JOIN incidents i ON i.id = il.incident_id"
    )
    proposal_rows = store.query(
        "SELECT id, learning_id, target_path, target_kind, status FROM proposals"
    )

    repos: dict[str, dict] = {}
    for row in session_rows:
        key = row["project_key"]
        repo = repos.setdefault(
            key,
            {
                "project_key": key,
                "displays": set(),
                "methods": set(),
                # path -> session count. A SET here loses the counts, and
                # context_path below promises "the working copy with the most
                # sessions". Lexical order could choose an unused checkout
                # instead of the one that generated the evidence.
                "clone_paths": {},
                "sessions": 0,
                "lines_scanned": 0,
            },
        )
        if row["project_display"]:
            repo["displays"].add(row["project_display"])
        if row["project_key_method"]:
            repo["methods"].add(row["project_key_method"])
        repo["clone_paths"][row["project_path"]] = (
            repo["clone_paths"].get(row["project_path"], 0) + int(row["n"])
        )
        repo["sessions"] += int(row["n"])
        repo["lines_scanned"] += int(row["lines"] or 0)

    incidents_by_key = {r["project_key"]: int(r["n"]) for r in incident_rows}
    signals_by_key: dict[str, dict[str, int]] = {}
    for row in signal_rows:
        signals_by_key.setdefault(row["project_key"], {})[row["signal_type"]] = int(row["n"])
    rules_by_key: dict[str, set] = {}
    for row in rule_rows:
        rules_by_key.setdefault(row["project_key"], set()).add(row["learning_id"])

    # Proposals carry no project_key — only target_path — so a proposal is
    # attributed to the repo whose clone path is the longest prefix of it.
    path_owner: list[tuple[str, str]] = []
    for key, repo in repos.items():
        for path in repo["clone_paths"]:
            if path:
                path_owner.append((path.rstrip(os.sep), key))
    path_owner.sort(key=lambda pair: -len(pair[0]))
    received: dict[str, list[dict]] = {}
    applied_lessons: dict[str, set[str]] = {}
    unattributed: list[dict] = []
    for prop in proposal_rows:
        target = prop["target_path"]
        owner = ""
        for prefix, key in path_owner:
            if target == prefix or target.startswith(prefix + os.sep):
                owner = key
                break
        entry = {
            "proposal_id": prop["id"],
            "target_path": target,
            "target_kind": prop["target_kind"],
            "status": prop["status"],
        }
        if owner:
            received.setdefault(owner, []).append(entry)
            if prop["status"] == "applied":
                applied_lessons.setdefault(owner, set()).add(prop["learning_id"])
        else:
            unattributed.append(entry)

    # The product reads retained inventories. An explicitly injected walker is
    # kept for offline compatibility checks; ordinary readers never scan files.
    weigher, weigh_reason = weigh, ""
    retained_context = weigher is None
    ranked = sorted(repos.values(), key=lambda r: (-r["sessions"], r["project_key"]))
    if retained_context:
        from ..instruction_context import project_summaries
        context_summaries = project_summaries(store, project_keys=repos)
    weigh_budget = len(ranked) if weigh_top_n is None else max(0, weigh_top_n)
    weight_errors: list[dict] = []

    rows = []
    for index, repo in enumerate(ranked):
        key = repo["project_key"]
        incidents = incidents_by_key.get(key, 0)
        signals = signals_by_key.get(key, {})
        top_signal = None
        if signals:
            best = min(signals.items(), key=lambda kv: (-kv[1], kv[0]))
            tied = sorted(name for name, n in signals.items() if n == best[1] and name != best[0])
            top_signal = {
                "signal_type": best[0],
                "count": best[1],
                "tied_with": tied,
                "tie_break": "highest count, then the alphabetically first signal_type",
            }
        clone_paths = sorted(repo["clone_paths"])
        on_disk = [p for p in clone_paths if p and isdir and isdir(p)]
        # Most sessions wins, ties broken alphabetically so the answer is
        # stable between runs. `on_disk` is already sorted, so the max() below
        # keeps the first of any tie.
        context_path = (
            max(on_disk, key=lambda p: repo["clone_paths"].get(p, 0)) if on_disk else ""
        )

        if retained_context:
            weight = context_summaries[key]
            context_path = weight.get('working_copy', {}).get('normalized_path', '')
        elif index >= weigh_budget:
            weight = not_computable(
                f"not measured: the caller capped context-weight measurement at "
                f"{weigh_budget} repos"
            )
        elif weigher is None:
            weight = not_computable(weigh_reason)
        elif not context_path:
            weight = not_computable(
                "no working copy of this repo is on disk any more, so nothing can be measured"
            )
        else:
            weight = _weigh(weigher, context_path, key, weight_errors)

        rows.append(
            {
                "project_key": key,
                "label": (sorted(repo["displays"])[0] if repo["displays"] else key),
                "label_is_fallback": not repo["displays"],
                "displays": sorted(repo["displays"]),
                "key_method": sorted(repo["methods"])[0] if repo["methods"] else "",
                "key_methods": sorted(repo["methods"]),
                "sessions": repo["sessions"],
                "clones": len(clone_paths),
                "clone_paths": clone_paths,
                "clones_on_disk": len(on_disk) if isdir is not None else None,
                "lines_scanned": repo["lines_scanned"],
                "incidents": incidents,
                "exposure": project_exposure(store, project_key=key, now_utc=now_utc),
                "top_signal": top_signal,
                "signals": signals,
                "rules_written_here": len(rules_by_key.get(key, ())),
                "rules_applied_here": len(applied_lessons.get(key, ())),
                "rules_received": len(received.get(key, ())),
                "rules_received_detail": received.get(key, []),
                "context_weight": weight,
                "context_path": context_path,
                "context_path_reason": weight.get('selection', 'No retained inventory selection is available.') if retained_context else (
                    "the working copy with the most sessions that is still on disk "
                    f"({repo['clone_paths'].get(context_path, 0)} of "
                    f"{repo['sessions']} sessions), ties broken alphabetically; "
                    "deliberately not routing.canonical_working_copy(), which answers "
                    "where automation WRITES, not what an agent carried"
                ),
                "benefit": project_measurements.project_benefit(store, project_key=key),
            }
        )

    orphans = sorted(set(incidents_by_key) - set(repos))
    return {
        "rows": rows,
        "count": len(rows),
        "sessions_total": sum(r["sessions"] for r in rows),
        "incidents_total": sum(r["incidents"] for r in rows),
        "clone_paths_total": sum(r["clones"] for r in rows),
        "grouped_on": "sessions.project_key",
        "not_a_repository": {
            "count": len(excluded_keys),
            "by_method": excluded_by_method,
            "counted_by": "distinct project_key, so this sums to `count`",
            "methods": list(NON_REPO_METHODS),
            "reason": (
                "PRD D2: a project is the upstream repository. `path` is the "
                "method chosen when `git rev-parse --show-toplevel` returns "
                "nothing and `unresolved` when the path did not resolve, so "
                "neither is a repository — and routing refuses to write an "
                "instruction file into one. A directory used as a session's "
                "working directory does not by itself establish a repository."
            ),
        },
        "unmatched_incident_keys": [
            {"project_key": key, "incidents": incidents_by_key[key]} for key in orphans
        ],
        "proposals_not_attributed": unattributed,
        "proposals_not_attributed_reason": (
            "proposals have no project_key; a target_path outside every known clone "
            "path (a global CLAUDE.md, say) belongs to no repo"
        ),
        "context_weight_errors": weight_errors,
        "context_source": "recorded_inventory" if retained_context else "explicit_legacy_walker",
        "context_weight_available": retained_context or weigher is not None,
        "context_weight_reason": weigh_reason,
        "context_weight_measured": sum(r['context_weight'].get('computable') is True for r in rows) if retained_context else min(weigh_budget, len(rows)),
        "context_weight_capped": (
            {
                "measured": min(weigh_budget, len(rows)),
                "skipped": max(0, len(rows) - weigh_budget),
                "reason": f"caller passed weigh_top_n={weigh_top_n}",
            }
            if weigh_top_n is not None and not retained_context
            else None
        ),
    }


def _weigh(weigher, path: str, key: str, errors: list[dict]) -> dict:
    """Call the context-weight walker, reporting a broken contract by name."""
    try:
        result = weigher(path)
    except Exception as exc:  # noqa: BLE001 - one bad repo must not blank the view
        errors.append({"project_key": key, "path": path, "error": f"{type(exc).__name__}: {exc}"})
        return not_computable(f"context_weight({path}) raised {type(exc).__name__}: {exc}")
    if not isinstance(result, dict):
        errors.append({"project_key": key, "path": path, "error": "did not return a dict"})
        return not_computable(f"context_weight({path}) did not return a dict")
    missing = [k for k in ("total_bytes", "always_loaded_bytes", "files") if k not in result]
    if missing:
        errors.append({"project_key": key, "path": path, "error": f"missing keys {missing}"})
        return not_computable(
            f"context_weight({path}) returned no {', '.join(missing)} — contract violated"
        )
    return {"computable": True, "path": path, **result}


# ---------------------------------------------------------------------------
# Incident-rate trend (PRD §8b) — the obvious denominator lies
# ---------------------------------------------------------------------------


def incident_rate(store, *, now_utc, min_sessions=MIN_SESSIONS_FOR_MONTH,
                  project_key=None, compatibility_key=None, months=7,
                  end_month=None, delivery_cursor=None) -> dict:
    """Version-3 monthly physical-line observations; no legacy session estimate."""
    _utc_day(now_utc)
    from .trend_data import monthly_exposure
    return monthly_exposure(store, now_utc=now_utc, min_sessions=min_sessions,
                            project_key=project_key, compatibility_key=compatibility_key,
                            months=months, end_month=end_month, delivery_cursor=delivery_cursor)


# ---------------------------------------------------------------------------
# V1 — the whole overview, which is what the app actually calls
# ---------------------------------------------------------------------------


def overview(store, cfg, *, now_utc, window_days: int | None = 14) -> dict:
    """Everything V1 renders, from one read-only Store."""
    from .overview_data import snapshot
    return {
        "audit": snapshot(store, cfg, now_utc=now_utc),
        "freshness": data_freshness(store, now_utc=now_utc),
        "status_line": status_line(store, cfg, now_utc=now_utc),
        "grid": run_stage_grid(store, now_utc=now_utc, window_days=window_days),
        "inbox": inbox(store, cfg),
        "backlog": backlog(store, cfg),
        "failures": failure_panel(store),
        "gate": gate_health(store),
        "confidence": _confidence(store),
    }


def _confidence(store) -> dict:
    """PRD §5.2: the loop's own track record, with an honest denominator."""
    counts = _proposal_status_counts(store)
    applied = counts.get("applied", 0)
    rolled_back = counts.get("rolled_back", 0)
    total = applied + rolled_back
    return {
        "applied": applied,
        "rolled_back": rolled_back,
        "survival": fraction(
            applied,
            total,
            min_denominator=20,
            numerator_label="applied",
            # The denominator is every rule EVER applied, not the rollback
            # count. Calling it "rolled back" made the sentence name the wrong
            # thing the moment either number stopped being zero.
            denominator_label="ever applied",
            small_display=f"{applied} applied · {rolled_back} rolled back",
            reason_when_small=(
                "no rule has ever been applied, so there is nothing to judge"
                if total == 0
                else "not enough applied rules to judge the loop yet"
            ),
        ),
    }
