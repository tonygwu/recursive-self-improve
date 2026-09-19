"""End-to-end run orchestration: scan -> mine -> cluster -> route/propose -> gate -> apply -> report.

Owned by the integrator; module implementations live in their own files.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import Config, ConfigError
from .data_boundary import private_storage_path
from .llm import BudgetExhausted
from .resources import bundled_path
from .store import Store, new_id, utc_now_iso, actor_for

logger = logging.getLogger(__name__)

PROMPTS_DIR = bundled_path("prompts")
# The repo's checked-in seed specs. Read-only: the gate never writes here,
# because this path is inside a git checkout and the production checkout must
# stay clean. Generated specs go to cfg.regression_specs_dir.
SEED_REGRESSION_DIR = bundled_path("evals", "regression")


def regression_specs_dir(cfg) -> Path:
    """Where generated eval specs are written and read. Never a checkout."""
    configured = Path(cfg.regression_specs_dir).expanduser()
    # Relative resolves against state_dir, so redirecting state_dir redirects
    # this too. An absolute path is still honoured for an operator override.
    d = private_storage_path(configured if configured.is_absolute() else cfg.state_path(cfg.regression_specs_dir))
    d.mkdir(parents=True, exist_ok=True)
    return d

# Legal values for cfg.mine_mode; anything else fails the run before it starts.
MINE_MODES = ("agentic", "fast")


def _desc_str(s: str) -> tuple:
    """Sort strings lexicographically descending, with empty strings last.

    Invert each codepoint and prepend an empty-value flag. Prefix-related strings
    need special handling: this key places the shorter prefix first. Callers must
    use consistent timestamp formats.
    """
    return (1, ()) if not s else (0, tuple(-ord(c) for c in s))


MINE_ORDERS = ("age_out_risk", "signal_then_recent")


def new_gate_stats() -> dict:
    """The gate's counter shape, shared by its callers.

    `attempted == gated_pass + gated_fail + ungated + inconclusive + failed
    + refused` is the invariant `check_stage_invariants` enforces.
    """
    return {
        "attempted": 0, "gated_pass": 0, "gated_fail": 0, "ungated": 0,
        # The majority gate's fourth verdict: the scenarios disagreed, so the
        # rule is HELD rather than auto-applied. Without this key the
        # `gate_stats[verdict] += 1` below raises KeyError on the first mixed
        # result, which would surface as a gate crash.
        "inconclusive": 0,
        # A gate exception needs an outcome counter as well as its cause.
        # Budget refusal has its own counter below.
        "failed": 0,
        # Attempts the budget refused. Held apart from `failed` so
        # `attempted == verdicts + failed + refused` and a starved gate never
        # reads as a broken one.
        "refused": 0,
        "dup_dropped": 0,
        # Split out so the two dedup channels are separately visible: the miner
        # reading the instruction files vs the embedding check.
        "dup_dropped_by_miner": 0,
    }


#: Every gate counter, split by whether it is an OUTCOME of an attempt.
#: `dup_dropped` counts proposals dropped before the gate ever ran, so adding
#: it to the sum would let dedup work stand in for verdicts that were never
#: recorded — the exact failure the invariant exists to catch.
GATE_OUTCOMES = (
    "gated_pass", "gated_fail", "ungated", "inconclusive", "failed", "refused",
)
GATE_BOOKKEEPING = ("dup_dropped", "dup_dropped_by_miner")


def _gate_outcome_keys() -> tuple[str, ...]:
    """Reconcile GATE_OUTCOMES with new_gate_stats(), or raise.

    Keep independent declarations so a missing verdict or unclassified bookkeeping
    counter cannot make an invariant vacuously pass. This check runs at import time."""
    counters = set(new_gate_stats()) - {"attempted"}
    classified = set(GATE_OUTCOMES) | set(GATE_BOOKKEEPING)
    if counters - classified:
        raise RuntimeError(
            "gate counter(s) in new_gate_stats() but classified as neither an "
            f"outcome nor bookkeeping: {sorted(counters - classified)}. Add "
            "each to GATE_OUTCOMES (it is a result of an attempt, and must "
            "sum to `attempted`) or to GATE_BOOKKEEPING (it counts something "
            "that never reached the gate)."
        )
    if classified - counters:
        raise RuntimeError(
            "gate counter(s) classified but absent from new_gate_stats(): "
            f"{sorted(classified - counters)}. The invariant would sum a key "
            "nothing ever writes, which reads as zero and hides a real gap."
        )
    return GATE_OUTCOMES


#: Stage stats that must satisfy attempted == sum(outcome buckets), and which
#: buckets count as outcomes for each. Runtime checks expose missing or misplaced
#: counters in actual run records as well as in test fixtures.
STAGE_INVARIANTS = {
    "scan": ("files_attempted", ("files_succeeded", "files_failed")),
    "mine": ("attempted", ("succeeded", "failed")),
    "gate": ("attempted", _gate_outcome_keys()),
    "apply": ("attempted", ("applied", "held", "failed")),
}


def check_stage_invariants(stats: dict) -> list[str]:
    """Stages whose attempted count does not equal the sum of its outcomes.

    Reported, not raised: a violation means the ACCOUNTING is wrong, and
    discarding a completed run's real work to punish a miscount would trade a
    reporting bug for a data-loss one. The report renders these prominently.
    """
    out: list[str] = []
    for stage, (attempted_key, outcome_keys) in STAGE_INVARIANTS.items():
        s = stats.get(stage)
        if not isinstance(s, dict) or attempted_key not in s:
            # Historical stats can predate required counters. Skip a stage without its
            # attempted counter because this checker cannot distinguish replay from a live
            # run. The test for that missing key records this compatibility tradeoff.
            continue
        attempted = s.get(attempted_key, 0)
        total = sum(s.get(k, 0) for k in outcome_keys)
        if attempted != total:
            out.append(
                f"{stage}: {attempted_key}={attempted} but "
                + " + ".join(f"{k}={s.get(k, 0)}" for k in outcome_keys)
                + f" = {total}"
            )
    return out


def _now_utc() -> "datetime":
    """Read wall time for deadlines that include system sleep.

    On macOS, the monotonic clock pauses during sleep. A wall deadline limits how
    long a run can retain the nightly lock. The named function lets tests control
    that clock explicitly.
    """
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


def _parse_run_ts(run_id: str, label: str, raw: str) -> "datetime":
    """Parse one of OUR OWN ISO-Z timestamps, or raise naming which one.

    Shared by `wall_clock_stats` and the mine stage's wall-clock deadline, so
    there is one definition of what a run timestamp is. Every value it sees was
    written by `utc_now_iso()`, which means an unparseable one is a contract
    bug rather than untrusted input, and defaulting it to anything would turn a
    stalled run into a fast-looking one.
    """
    from datetime import datetime, timezone

    try:
        dt = datetime.fromisoformat((raw or "").replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            f"run {run_id}: {label} is not an ISO timestamp: {raw!r}. It is "
            "written by utc_now_iso(), so this is a contract bug, not input."
        ) from exc
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def wall_clock_stats(store, run_id: str, started_iso: str, finished_iso: str) -> dict:
    """Report wall time, model time, and the largest gap between calls.

    On macOS, monotonic time pauses during sleep. A long wall-clock run can still
    hold the nightly lock at the next scheduled start. Unaccounted time includes
    scan and sandbox setup as well as sleep; it is descriptive, not a sleep estimate
    or an automatic decision to keep the machine awake."""
    from datetime import timedelta

    run_start = _parse_run_ts(run_id, "started", started_iso)
    run_end = _parse_run_ts(run_id, "finished", finished_iso)
    wall = (run_end - run_start).total_seconds()

    rows = store.query(
        "SELECT created_at, duration_ms, stage FROM llm_calls WHERE run_id = ? "
        "ORDER BY created_at",
        (run_id,),
    )
    model = 0.0
    largest = 0.0
    largest_before = ""
    prev_end = run_start
    for row in rows:
        dur = (row["duration_ms"] or 0) / 1000.0
        model += dur
        # created_at is written AFTER the call returns (llm.py), so it is the
        # call's END; subtracting duration gives its start.
        end = _parse_run_ts(run_id, "llm_calls.created_at", row["created_at"])
        gap = (end - timedelta(seconds=dur) - prev_end).total_seconds()
        if gap > largest:
            largest = gap
            largest_before = f"{row['stage']}@{row['created_at']}"
        prev_end = end

    unaccounted = wall - model
    return {
        "wall_seconds": round(wall, 1),
        "model_seconds": round(model, 1),
        "unaccounted_seconds": round(unaccounted, 1),
        "unaccounted_pct": round(100.0 * unaccounted / wall, 1) if wall > 0 else 0.0,
        "largest_gap_seconds": round(largest, 1),
        "largest_gap_before_call": largest_before,
        "calls": len(rows),
    }


def order_incidents_for_mining(
    store, incidents: list[dict], cfg
) -> tuple[list[dict], dict]:
    """Order the mining queue by the configured sampling policy.

    A finite budget makes queue order consequential. Transcript retention can remove
    full source sessions before mining reaches them, leaving only archived windows.

    Both strategies put already-deleted transcripts last because their evidence
    cannot degrade further:

    * ``age_out_risk`` selects the oldest surviving session, then score.
    * ``signal_then_recent`` selects the highest score, then most recent session.

    The first prioritizes evidence retention; the second prioritizes stronger recent
    signals and can leave older incidents waiting. Returns ``(ordered, stats)`` so
    the report exposes how this policy changes the selected population.
    """
    from .config import ConfigError

    # cfg is REQUIRED, not defaulted. It was `cfg=None -> Config()`, and the
    # pipeline called this without it — so cfg.mine_order was silently ignored
    # while the run report printed the default order as though it had been
    # chosen. A defaulted config is how a setting goes missing without anyone
    # noticing.
    if cfg.mine_order not in MINE_ORDERS:
        raise ConfigError(
            f"mine_order must be one of {MINE_ORDERS}, got {cfg.mine_order!r}"
        )

    sessions = {
        r["file_path"]: r
        for r in store.query("SELECT file_path, last_ts FROM sessions")
    }
    present_cache: dict[str, bool] = {}

    def parts(inc):
        sf = inc.get("session_file", "")
        if sf not in present_cache:
            present_cache[sf] = Path(sf).exists() if sf else False
        session = sessions.get(sf) or {}
        last_ts = (session.get("last_ts") or "") or (inc.get("ts") or "")
        # Normalize legacy raw session-count scores onto the documented [0, 1] band.
        # Use the writer's scoring curve; clamping every out-of-range score to 1.0 would
        # leave all legacy rows ahead of normally scored incidents.
        raw = float(inc.get("score") or 0.0)
        if raw > 1.0:
            from .scan import repeated_error_score

            raw = repeated_error_score(int(raw))
        score = min(1.0, max(0.0, raw))
        return present_cache[sf], last_ts, score

    def age_out_key(inc):
        present, last_ts, score = parts(inc)
        # An unknown last_ts sorts as oldest-possible: unknown age is treated
        # as urgent rather than quietly parked at the back.
        return (0 if present else 1, last_ts, -score)

    def signal_key(inc):
        present, last_ts, score = parts(inc)
        # Unknown last_ts sorts LAST here (empty string reversed), the mirror
        # of the age-out case: under this strategy unknown age is not urgent.
        return (0 if present else 1, -score, _desc_str(last_ts))

    ordered = sorted(
        incidents,
        key=age_out_key if cfg.mine_order == "age_out_risk" else signal_key,
    )
    present = sum(1 for v in (present_cache.get(i.get("session_file", "")) for i in incidents) if v)
    stats = {
        "order": cfg.mine_order,
        "transcript_present": present,
        "transcript_already_gone": len(incidents) - present,
    }
    return ordered, stats


def finalize_stale_runs(store, cfg) -> dict:
    """Mark stale running rows abandoned after an interrupted process.

    A crash can skip in-process finalization. The configured age threshold permits
    overlapping manual work to finish before a later invocation abandons its row.
    Compare UTC timestamps; count and retain rows with unreadable timestamps instead
    of guessing their age.
    """
    from datetime import datetime, timedelta, timezone

    # cfg required, not defaulted: the same permissive shape on
    # order_incidents_for_mining let its call site drop cfg silently, so
    # mine_order was ignored while the report claimed otherwise.
    cutoff = datetime.now(timezone.utc) - timedelta(hours=cfg.run_stale_after_hours)
    stats = {"abandoned": 0, "left_running": 0, "unparseable_started": 0}
    for row in store.query("SELECT * FROM runs WHERE status = 'running'"):
        raw = (row["started"] or "").strip()
        try:
            started = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            stats["unparseable_started"] += 1
            continue
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        if started < cutoff:
            store.update(
                "runs", "id", row["id"],
                {"status": "abandoned", "finished": utc_now_iso()},
            )
            stats["abandoned"] += 1
        else:
            stats["left_running"] += 1
    store.commit()
    return stats


def run_pipeline(
    cfg: Config,
    store: Store,
    *,
    dry_run: bool = False,
    review_only: bool = False,
    max_cheap_calls: int | None = None,
    max_strong_calls: int | None = None,
    project_filter: str = "",
    _llm_factory: Callable[..., Any] | None = None,
    _embedder_factory: Callable[..., Any] | None = None,
) -> dict:
    """Execute one full mining run.

    dry_run: scan + filter-incidents + report only — zero LLM calls, zero target writes.
    review_only: full pipeline but nothing auto-applies (all proposals held).
    project_filter: substring restricting which incidents the MINE stage
    consumes this run (scan still indexes everything); recorded in stats so
    the narrowing is never silent.

    _llm_factory / _embedder_factory are TEST SEAMS (explicit, documented —
    preferred over monkeypatching module internals): when provided they are
    called as ``_llm_factory(cfg, store, run_id, raw_dir)`` /
    ``_embedder_factory(cfg, store)`` in place of the real
    :class:`~self_improve.llm.LLMRunner` / ``embeddings.Embedder``. Production
    callers never pass them.

    Returns the run stats dict (also persisted on the runs row).
    """
    from . import cluster, miner, propose, report, routing, scan
    from .sources.claude_code import ClaudeCodeSource
    from .sources.codex import CodexSource

    cfg.validate()
    # Fail loud BEFORE any side effect (even the runs row): a bogus mine_mode
    # is a config error, not something to discover mid-run or paper over.
    if cfg.mine_mode not in MINE_MODES:
        raise ConfigError(
            f"mine_mode must be one of {MINE_MODES}, got {cfg.mine_mode!r}"
        )
    run_id = new_id()
    run_dir = cfg.state_path("runs", run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    from datetime import datetime, timedelta, timezone

    stats_reaped = finalize_stale_runs(store, cfg)
    run_started = utc_now_iso()
    from .execution_policy import policy_snapshot, automatic_permission

    from .queue_history import capture as capture_queue
    queue_settings = {
        "mine_order": cfg.mine_order, "project_filter": project_filter, "dry_run": dry_run,
        "cheap_call_cap": cfg.max_cheap_calls_per_run if max_cheap_calls is None else max_cheap_calls,
        "strong_call_cap": cfg.max_strong_calls_per_run if max_strong_calls is None else max_strong_calls,
        "gate_call_cap": cfg.max_gate_calls_per_run,
    }
    stats: dict = {"run_id": run_id, "dry_run": dry_run, "review_only": review_only,
                  "execution_policy": policy_snapshot(store)}
    # Persist execution intent before any proposal exists. A concurrent reader
    # must not interpret a running review-only run's missing stats as permission.
    with store.transaction(write=True):
        store.insert("runs", {"id": run_id, "started": run_started,
                              "stats_json": json.dumps(stats)})
        capture_queue(store, run_id, phase="start", settings=queue_settings)
    stats["stale_runs_reaped"] = stats_reaped

    try:
        # ---- 1. scan (incremental; cheap; no LLM) ----
        sources = [ClaudeCodeSource(cfg), CodexSource(cfg)]
        scan_stats = scan.scan_all(store, cfg, sources, run_id)
        stats["scan"] = scan_stats.as_dict() if hasattr(scan_stats, "as_dict") else vars(scan_stats)
        store.commit()
        # Collect actual working-copy files before mining. Observation time is
        # this inspection, never the application event or session timestamp.
        from .rule_availability import collect_availability
        stats["availability"] = collect_availability(store, cfg, run_id=run_id)

        if not dry_run:
            import dataclasses

            from .llm import LLMRunner

            if max_cheap_calls is not None or max_strong_calls is not None:
                cfg = dataclasses.replace(
                    cfg,
                    max_cheap_calls_per_run=(
                        max_cheap_calls
                        if max_cheap_calls is not None
                        else cfg.max_cheap_calls_per_run
                    ),
                    max_strong_calls_per_run=(
                        max_strong_calls
                        if max_strong_calls is not None
                        else cfg.max_strong_calls_per_run
                    ),
                )
            # Probe provider startup before constructing the router or spending call budget.
            # The --version probe makes no model call. Exclude unusable providers from the
            # router's candidates, even if another provider can start.
            providers = provider_preflight(cfg)
            stats["providers"] = providers
            llm_cfg = cfg
            if not dry_run and providers["unusable"] and providers["any_usable"]:
                logger.warning(
                    "excluding provider(s) that cannot start: %s", providers["unusable"]
                )
                llm_cfg = dataclasses.replace(
                    cfg, allowed_providers=tuple(providers["usable"])
                )

            stats["budget_limits"] = {
                "cheap": llm_cfg.max_cheap_calls_per_run,
                "strong": llm_cfg.max_strong_calls_per_run,
                "gate": llm_cfg.max_gate_calls_per_run,
            }
            if _llm_factory is not None:
                llm = _llm_factory(llm_cfg, store, run_id, run_dir / "raw")
            else:
                llm = LLMRunner(llm_cfg, store, run_id, raw_dir=run_dir / "raw")

            # ---- 2. mine new incidents (cheap model, budgeted) ----
            # Nothing can start. Skip repeated provider calls and preserve
            # the scan work that still needs to be committed.
            mine_blocked = not dry_run and not providers["any_usable"]
            if mine_blocked:
                logger.error(
                    "no LLM provider can start; skipping the mine stage rather "
                    "than spending the budget to rediscover it. probed %s: %s",
                    providers["probed"],
                    providers["unusable"],
                )
            if project_filter:
                # Match the canonical identity as well as the raw cwd. A repo
                # rename leaves old sessions on the old path forever, so
                # filtering by the CURRENT name against project_path alone
                # silently mines a subset — a quiet under-count, not an error.
                incidents = store.query(
                    "SELECT * FROM incidents WHERE status = 'new' AND ("
                    " project_path LIKE ? OR project_key LIKE ? OR session_file IN ("
                    "  SELECT file_path FROM sessions WHERE project_display LIKE ?"
                    " ))",
                    (f"%{project_filter}%", f"%{project_filter}%", f"%{project_filter}%"),
                )
                stats["mine_project_filter"] = project_filter
            else:
                incidents = store.query(
                    "SELECT * FROM incidents WHERE status = 'new'"
                )
            # Queue order affects which incidents retain their full transcripts
            # until mining reaches them. Use the configured sampling policy.
            incidents, order_stats = order_incidents_for_mining(store, incidents, cfg)
            mine_stats = {"attempted": 0, "succeeded": 0, "failed": 0, "taxonomy": {}}
            mine_stats["queue"] = order_stats
            mine_stats["mode"] = cfg.mine_mode
            # Use wall time because macOS monotonic time pauses during sleep. Record the
            # deadline on every run so an unused limit differs from an unmeasured one.
            mine_deadline = _parse_run_ts(run_id, "started", run_started) + timedelta(
                seconds=cfg.max_run_wall_seconds
            )
            mine_stats["deadline"] = {
                "max_run_wall_seconds": cfg.max_run_wall_seconds,
                "stopped_early": False,
                "incidents_not_mined": 0,
            }
            mine_sandbox_root = run_dir / "mine"
            # Refuse unavailable work before counting attempts, as with
            # budget_exhausted, so a stage that never ran has no false failures.
            if mine_blocked:
                mine_stats["taxonomy"]["provider_unavailable"] = len(incidents)
                incidents = []
            for inc in incidents:
                # A deadline stop refuses work before attempted is incremented,
                # preserving attempted == succeeded + failed.
                if _now_utc() >= mine_deadline:
                    left = len(incidents) - mine_stats["attempted"]
                    mine_stats["deadline"]["stopped_early"] = True
                    mine_stats["deadline"]["incidents_not_mined"] = left
                    mine_stats["deadline"]["elapsed_wall_seconds"] = round(
                        (
                            _now_utc()
                            - _parse_run_ts(run_id, "started", run_started)
                        ).total_seconds(),
                        1,
                    )
                    mine_stats["taxonomy"]["wall_deadline_reached"] = left
                    logger.warning(
                        "run %s: mine stopped at the %.1f h wall-clock deadline "
                        "with %d incidents unmined; the nightly lock is released "
                        "so the next 02:30 can run",
                        run_id, cfg.max_run_wall_seconds / 3600.0, left,
                    )
                    break
                mine_stats["attempted"] += 1
                mine_provenance={'run_id':run_id}
                try:
                    if cfg.mine_mode == "agentic":
                        learning = miner.mine_incident_agentic(
                            store,
                            lambda prompt, sandbox_dir: _llm_agentic_json(
                                llm,
                                "mine_agentic",
                                cfg.cheap_model_class,
                                prompt,
                                sandbox_dir,
                                provenance=mine_provenance,
                            ),
                            inc,
                            cfg,
                            PROMPTS_DIR,
                            mine_sandbox_root,
                            provenance=mine_provenance,
                        )
                    else:  # "fast" — the only other value; validated at run start
                        learning = miner.mine_incident(
                            store,
                            lambda prompt: _llm_json(
                                llm, "mine", cfg.cheap_model_class, prompt, provenance=mine_provenance
                            ),
                            inc,
                            cfg,
                            PROMPTS_DIR,
                            provenance=mine_provenance,
                        )
                    mine_stats["succeeded"] += 1
                    if learning is not None and "_dedup" in learning:
                        dedup_tally = mine_stats.setdefault("dedup", {})
                        dedup_tally[learning["_dedup"]] = (
                            dedup_tally.get(learning["_dedup"], 0) + 1
                        )
                except BudgetExhaustedSignal:
                    # This incident WAS attempted — the call was refused, not
                    # skipped — so it must land in a bucket to preserve
                    # attempted == succeeded + failed.
                    mine_stats["failed"] += 1
                    mine_stats["taxonomy"]["budget_refused_this_incident"] = (
                        mine_stats["taxonomy"].get("budget_refused_this_incident", 0) + 1
                    )
                    # The remainder were never attempted at all; counted
                    # separately so neither number double-counts the other.
                    mine_stats["taxonomy"]["budget_exhausted"] = (
                        mine_stats["taxonomy"].get("budget_exhausted", 0)
                        + (len(incidents) - mine_stats["attempted"])
                    )
                    break
                except Exception as exc:  # one bad incident never kills the run
                    # Roll back this incident's partial writes. Without it, a
                    # mine that failed AFTER inserting (e.g. the
                    # incident_learnings link) leaves that row uncommitted, and
                    # the NEXT successful mine's commit() flushes it too —
                    # which can collide when the incident is retried. scan.py
                    # applies the same per-item rollback rule.
                    store.conn.rollback()
                    mine_stats["failed"] += 1
                    # A failed call carries its outcome class; naming it is the
                    # difference between "the model replied in prose" and "the
                    # CLI could not start".
                    if isinstance(exc, MineCallFailed):
                        key = f"call_failed:{exc.outcome}"
                    elif isinstance(exc, sqlite3.IntegrityError):
                        key = integrity_key(exc)
                    else:
                        key = type(exc).__name__
                    mine_stats["taxonomy"][key] = mine_stats["taxonomy"].get(key, 0) + 1
            stats["mine"] = mine_stats
            store.commit()

            # ---- 3. cluster + dedupe (strong model for merges) ----
            # One embedder per run; rule text is embedded locally (model2vec),
            # vectors cached in the store's embeddings table.
            if _embedder_factory is not None:
                embedder = _embedder_factory(cfg, store)
            else:
                from . import embeddings

                embedder = embeddings.Embedder(cfg, store)
            embed_fn = embedder.encode

            candidates = store.query("SELECT * FROM learnings WHERE status = 'candidate'")
            if cfg.mine_mode == "agentic":
                # The agentic miner already deduped each learning against the
                # WHOLE learnings table at mine time (duplicate/amend
                # decisions, with the search tool). Grouping, pending-pool
                # absorption, and LLM merging are fast-mode machinery — here
                # candidates pass straight through, and the deterministic
                # belt-check below still guards against agent misses.
                merged = [{**c, "absorbed_ids": []} for c in candidates]
                stats["cluster"] = {
                    "candidates": len(candidates),
                    "mode": "agentic_passthrough",
                }
                existing_vecs = cluster.existing_rule_vectors(
                    cfg, _target_paths(store, cfg), embedder
                )
                rejected_vecs = cluster.rejected_vectors(store, embedder)
            else:
                # Pool expansion (fast mode): cluster new candidates
                # TOGETHER WITH "open" learnings - already proposed, but
                # whose latest proposal is still awaiting a decision
                # (pending/held). A new observation of an open lesson
                # strengthens the open proposal's evidence instead of
                # spawning a competing duplicate proposal.
                open_learnings = store.query(
                    "SELECT l.* FROM learnings l WHERE l.status = 'proposed' AND ("
                    "  SELECT p.status FROM proposals p WHERE p.learning_id = l.id"
                    "  ORDER BY p.created_at DESC LIMIT 1"
                    ") IN ('pending', 'held')"
                )
                open_ids = {l["id"] for l in open_learnings}
                pool = candidates + open_learnings
                clusters = cluster.group_learnings(
                    pool, embed_fn, cfg.cluster_group_cosine
                )

                # Absorption pass: a cluster mixing new candidates with >= 1 open
                # learning folds the candidates into the open learning (no new
                # proposal, no LLM merge for that cluster). Clusters of only open
                # learnings pass through untouched; only-candidate clusters go on
                # to merge/propose as before.
                candidate_clusters: list[list[dict]] = []
                absorbed_into_pending = 0
                open_passthrough_clusters = 0
                for grp in clusters:
                    news = [l for l in grp if l["id"] not in open_ids]
                    opens = [l for l in grp if l["id"] in open_ids]
                    if news and opens:
                        # Absorber choice mirrors merge_clusters' representative
                        # rule: highest evidence_count, ties -> earliest first_seen.
                        absorber = sorted(
                            opens,
                            key=lambda l: (
                                -int(l.get("evidence_count") or 0),
                                l.get("first_seen") or "",
                            ),
                        )[0]
                        projects = set(_projects_list(absorber))
                        evidence = int(absorber.get("evidence_count") or 0)
                        for cand in news:
                            # A candidate row is at least one observation (same
                            # floor merge_clusters applies).
                            evidence += int(cand.get("evidence_count") or 0) or 1
                            projects.update(_projects_list(cand))
                            store.update(
                                "learnings",
                                "id",
                                cand["id"],
                                {"status": "superseded", "duplicate_of": absorber["id"]},
                            )
                            absorbed_into_pending += 1
                        store.update(
                            "learnings",
                            "id",
                            absorber["id"],
                            {
                                "evidence_count": evidence,
                                "project_count": len(projects),
                                "projects_json": json.dumps(sorted(projects)),
                            },
                        )
                    elif news:
                        candidate_clusters.append(news)
                    else:
                        open_passthrough_clusters += 1

                from . import mining_history
                merge_sources={}
                def _merge_call(items: list[dict]) -> dict | None:
                    prompt,template_sha=miner.render_prompt_revision(PROMPTS_DIR/'cluster_merge.md', {
                        'rules':json.dumps([{k:it.get(k,'') for k in ('rule_text','why','category')} for it in items],ensure_ascii=False,indent=2)})
                    provenance=mining_history.context('fast',prompt,template_sha,{'run_id':run_id})
                    provenance['stage']='cluster'
                    key=tuple(sorted(it['id'] for it in items))
                    merge_sources[key]=(provenance,items)
                    try:
                        return _llm_json(llm,'cluster',cfg.strong_model_class,prompt,provenance=provenance)
                    except BudgetExhaustedSignal:
                        return None

                merged, merge_stats = cluster.merge_clusters(candidate_clusters, _merge_call)
                store.commit()
                for m in merged:
                    if m.get("absorbed_ids"):
                        key=tuple(sorted([m['id'],*m['absorbed_ids']]))
                        provenance,source_learnings=merge_sources[key]
                        with store.transaction(write=True):
                            before=store.query_one('SELECT * FROM learnings WHERE id=?',(m['id'],))
                            for absorbed in m['absorbed_ids']:
                                store.update('learnings','id',absorbed,{'status':'superseded','duplicate_of':m['id']})
                            store.update('learnings','id',m['id'],{
                                'rule_text':m['rule_text'],'why':m.get('why',''),
                                'evidence_count':m.get('evidence_count',0),'project_count':m.get('project_count',0),
                                'projects_json':json.dumps(m.get('projects',[]))})
                            current=store.query_one('SELECT * FROM learnings WHERE id=?',(m['id'],))
                            slots=','.join('?' for _ in key)
                            incidents=store.query(f'SELECT DISTINCT i.* FROM incidents i JOIN incident_learnings il ON il.incident_id=i.id WHERE il.learning_id IN ({slots}) ORDER BY i.ts,i.id',key)
                            mining_history.append(store,current,before=before,kind='cluster_merge',incidents=incidents,
                                provenance=provenance,source_learnings=source_learnings)
                existing_vecs = cluster.existing_rule_vectors(
                    cfg, _target_paths(store, cfg), embedder
                )
                rejected_vecs = cluster.rejected_vectors(store, embedder)
                stats["cluster"] = {
                    "candidates": len(candidates),
                    "open_learnings": len(open_learnings),
                    "clusters": len(clusters),
                    "absorbed_into_pending": absorbed_into_pending,
                    "open_passthrough_clusters": open_passthrough_clusters,
                    "merged": len(merged),
                    **merge_stats,
                }

            # ---- 4. route + propose + gate + apply ----
            from .apply import APPLIABLE_STATUSES, apply_proposal
            from .evals import regression

            # Verified lazily on the first gated proposal, once per run. A
            # sandbox that has quietly stopped containing anything looks
            # exactly like one that works, so it is probed rather than assumed.
            sandbox_state: dict = {"ok": None, "error": "", "observed": {}}
            gate_stats = new_gate_stats()
            apply_stats = {"attempted": 0, "applied": 0, "held": 0, "failed": 0, "taxonomy": {}, "operation_ids": []}
            stats["apply"] = apply_stats

            def _bump_tax(key: str) -> None:
                apply_stats["taxonomy"][key] = apply_stats["taxonomy"].get(key, 0) + 1

            def gate_run(learning: dict, proposal: dict) -> None:
                # This stage owns source work; publish it before the history
                # recorder starts short, independent execution transactions.
                store.commit()
                gate_one_proposal(
                    store, cfg, llm, learning, proposal,
                    run_dir=run_dir, sandbox_state=sandbox_state,
                    gate_stats=gate_stats, bump_taxonomy=_bump_tax,
                    run_id=run_id,
                )

            for learning in merged:
                from .rejections import lesson_rejected, proposal_rejection
                if lesson_rejected(store, learning['id']):
                    _bump_tax('propose_LessonRejected')
                    continue
                # Amend-of-applied: a candidate whose prior proposal was
                # APPLIED means its rule already lives in a file — the fix is
                # an in-place EDIT at the si: marker, not the routing table.
                # Checked BEFORE the duplicate drop: the amended text is by
                # construction near-identical to its own applied line, so the
                # existing-rule dedupe would otherwise reject the amendment as
                # a duplicate of the very line it is meant to replace.
                prior_applied = store.query_one(
                    "SELECT * FROM proposals WHERE learning_id = ? "
                    "AND status = 'applied' ORDER BY created_at DESC LIMIT 1",
                    (learning["id"],),
                )
                if prior_applied is None:
                    # The miner's explicit duplicate judgment takes precedence over a cosine
                    # threshold. It names the existing instruction line; preserve that evidence
                    # instead of requiring a second, approximate similarity check to reproduce it.
                    miner_dup = (learning.get("duplicate_of") or "").strip()
                    if miner_dup:
                        store.update(
                            "learnings",
                            "id",
                            learning["id"],
                            {"status": "rejected", "duplicate_of": miner_dup},
                        )
                        gate_stats["dup_dropped"] += 1
                        gate_stats["dup_dropped_by_miner"] += 1
                        continue
                    # Dedupe against rules already in force, then against
                    # rules the user previously rejected. The matched text is
                    # recorded on the learning row so a drop is always
                    # attributable to a specific rule, never a bare "existing".
                    dup, matched_text = cluster.is_duplicate(
                        learning["rule_text"], existing_vecs, embed_fn, cfg.cluster_dup_cosine
                    )
                    if not dup:
                        dup, matched_text = cluster.is_duplicate(
                            learning["rule_text"], rejected_vecs, embed_fn, cfg.cluster_dup_cosine
                        )
                    if dup:
                        store.update(
                            "learnings",
                            "id",
                            learning["id"],
                            {"status": "rejected", "duplicate_of": matched_text},
                        )
                        gate_stats["dup_dropped"] += 1
                        continue
                # One unroutable/unproposable learning must not abort the rest.
                try:
                    if prior_applied is not None:
                        target = Path(prior_applied["target_path"])
                        rejection = proposal_rejection(store,cfg,learning,target,prior_applied['target_kind'],embedder)
                        if rejection:
                            _bump_tax('propose_' + rejection['code'])
                            continue
                        current = target.read_text() if target.exists() else ""
                        proposal = propose.build_edit_proposal(
                            learning,
                            target,
                            current,
                            cfg,
                            target_kind=prior_applied["target_kind"],
                        )
                    else:
                        route = routing.route(learning, cfg)
                        target = Path(route.target_path)
                        rejection = proposal_rejection(store,cfg,learning,target,route.target_kind,embedder)
                        if rejection:
                            _bump_tax('propose_' + rejection['code'])
                            continue
                        current = target.read_text() if target.exists() else ""
                        proposal = propose.build_proposal(learning, route, current, cfg)
                except Exception as exc:
                    _bump_tax(f"propose_{type(exc).__name__}")
                    continue
                proposal.setdefault("id", new_id())
                proposal["run_id"] = run_id
                gate_run(learning, proposal)
                _persist_proposal(store, proposal)
                store.update("learnings", "id", learning["id"], {"status": "proposed"})
                # Reaching this point IS the attempt; "held" is one of its
                # outcomes, not a skip. Count the attempt and its outcome
                # together so held work does not disappear from the total.
                apply_stats["attempted"] += 1
                permission = automatic_permission(store, cfg, proposal, review_only=review_only)
                if not permission["allowed"]:
                    apply_stats["held"] += 1
                    continue
                if proposal["status"] in APPLIABLE_STATUSES:
                    try:
                        # The writer must durably prepare its operation before
                        # changing a target. Commit this stage's source first;
                        # a nested writer may not commit its caller's work.
                        store.commit()
                        outcome = apply_proposal(store, cfg, proposal)
                        if outcome["outcome"] == "applied":
                            apply_stats["applied"] += 1
                            if outcome.get("operation_id"):
                                apply_stats["operation_ids"].append(outcome["operation_id"])
                            store.update(
                                "learnings", "id", learning["id"], {"status": "applied"}
                            )
                        else:
                            apply_stats["held"] += 1
                    except Exception as exc:
                        apply_stats["failed"] += 1
                        _bump_tax(getattr(exc, 'code', type(exc).__name__))
                else:
                    # gated_fail or a gate that raised (status back to
                    # 'pending'): not applied, so it is HELD. Without this the
                    # attempt increments with no outcome and the invariant
                    # breaks again in the auto-apply path — the same shape as
                    # the bug being fixed, one branch over.
                    apply_stats["held"] += 1
                    _bump_tax(f"not_appliable_{proposal['status']}")
            stats["gate"] = gate_stats
            stats["apply"] = apply_stats

            # ---- 4b. contradiction detection (Part 2) ----
            # Runs AFTER applies so this run's own additions are included in
            # the fleet being checked. Never fatal: a contradiction sweep
            # failing must not lose the run's mining work.
            try:
                from . import contradictions

                cands = contradictions.find_candidates(
                    cfg, _target_paths(store, cfg), embedder
                )
                stats["contradictions"] = contradictions.judge_pairs(
                    store,
                    cfg,
                    cands,
                    lambda prompt: _llm_json(
                        llm, "contradiction", cfg.cheap_model_class, prompt
                    ),
                    PROMPTS_DIR,
                    run_id,
                )
            except BudgetExhaustedSignal:
                stats["contradictions"] = {"skipped": "budget_exhausted"}
            except Exception as exc:
                stats["contradictions"] = {"error": f"{type(exc).__name__}: {exc}"}

            # ---- 4c. A/B pruning of applied rules (Part 2) ----
            # Re-tests applied rules WITHOUT them on the current model; rules
            # the model no longer needs become delete proposals. Proposals are
            # returned (not persisted by ab.py) and go through the SAME
            # propose/apply machinery, so review-only still holds them.
            try:
                from .evals import ab

                # Check the shared gate pool before starting an A/B comparison. A budget
                # refusal must not become a partially executed arm or a verdict about a rule.
                need_ab = ab_calls_needed(cfg)
                left_ab = cfg.max_gate_calls_per_run - llm.stats()["calls_made"].get("gate", 0)
                if need_ab > left_ab:
                    raise BudgetExhaustedSignal(
                        f"A/B sweep needs {need_ab} gate calls; {left_ab} left"
                    )

                store.commit()  # Publish stage work before the independent eval recorder.
                ab_result = ab.rerun_applied(
                    store,
                    cfg,
                    _make_sandbox_agent_runner(llm, cfg),
                    regression_specs_dir(cfg),
                    # Committed specs predate the move to the state dir.
                    fallback_spec_dir=SEED_REGRESSION_DIR,
                    work_dir=run_dir / "ab",
                    run_id=run_id,
                )
                prune_proposals = ab_result.pop("proposals", [])
                for pp in prune_proposals:
                    learning = store.query_one(
                        "SELECT * FROM learnings WHERE id = ?", (pp["learning_id"],)
                    )
                    if learning is None:
                        continue
                    current = (
                        Path(pp["target_path"]).read_text()
                        if Path(pp["target_path"]).exists()
                        else ""
                    )
                    route = routing.RouteDecision(
                        target_path=pp["target_path"],
                        target_kind="global_claude_md",
                        action="delete",
                    )
                    try:
                        proposal = propose.build_proposal(learning, route, current, cfg)
                    except Exception as exc:
                        _bump_tax(f"prune_{type(exc).__name__}")
                        continue
                    proposal.setdefault("id", new_id())
                    proposal["run_id"] = run_id
                    proposal["status"] = "gated_pass"  # the A/B run IS its gate
                    proposal["eval_result_id"] = pp.get("eval_result_id", "")
                    _persist_proposal(store, proposal)
                ab_result["proposals_persisted"] = len(prune_proposals)
                stats["ab_prune"] = ab_result
            except (BudgetExhaustedSignal, BudgetExhausted):
                # Same reason as the gate: a raw BudgetExhausted from a trial
                # is the same event as the preflight's signal.
                stats["ab_prune"] = {"skipped": "budget_exhausted"}
            except Exception as exc:
                stats["ab_prune"] = {"error": f"{type(exc).__name__}: {exc}"}

            stats["llm"] = llm.stats()
            store.commit()

        # Publish retained observations after mining links have committed. This
        # collector owns its transaction and never opens files or invokes models.
        store.commit()
        from .project_measurements import collect_project_measurements
        stats["project_measurements"] = collect_project_measurements(store, run_id=run_id)

        # Display membership is independent of mining dedupe and authorization.
        # The collector owns publication; dry runs may only reuse retained vectors.
        from .rule_families import collect_families
        stats["rule_families"] = collect_families(
            store, cfg, embedder=embedder if not dry_run else None, cache_only=dry_run)

        # ---- 5. report ----
        violations = check_stage_invariants(stats)
        if violations:
            stats["invariant_violations"] = violations
        report_path = run_dir / "report.md"
        stats["report_path"] = str(report_path)
        # Persist stats before report generation: report sections read the run row.
        # Record wall-clock gaps before deriving status and writing that row.
        run_finished = utc_now_iso()
        stats["wall_clock"] = wall_clock_stats(
            store, run_id, run_started, run_finished
        )
        run_status, status_reasons = derive_run_status(stats)
        stats["status_reasons"] = status_reasons
        if status_reasons:
            logger.warning(
                "run %s completed DEGRADED: %s", run_id, "; ".join(status_reasons)
            )
        store.update(
            "runs",
            "id",
            run_id,
            {
                # The SAME instant wall_clock_stats measured to, not a second
                # call: two utc_now_iso() calls would make the stored duration
                # and the reported one disagree by however long the stats dump
                # took, which is the kind of gap nobody would ever chase.
                "finished": run_finished,
                "status": run_status,
                "stats_json": json.dumps(stats, ensure_ascii=False),
                "report_path": str(report_path),
            },
        )
        capture_queue(store, run_id, phase="finish", settings=queue_settings)
        store.commit()
        report.generate(store, cfg, run_id, report_path)
        return stats
    except BaseException as exc:
        # BaseException, not Exception: KeyboardInterrupt and SystemExit are
        # NOT Exception subclasses, so a Ctrl-C skipped this handler entirely
        # and could leave a row 'running'. Preserve a terminal status that
        # distinguishes interruption from an ordinary exception.
        status = (
            "interrupted"
            if isinstance(exc, (KeyboardInterrupt, SystemExit))
            else "error"
        )
        store.update("runs", "id", run_id, {"finished": utc_now_iso(), "status": status})
        capture_queue(store, run_id, phase="finish", settings=queue_settings)
        store.commit()
        raise


#: What "this stage worked" means, per stage: ``(attempted, success_keys,
#: failed)``. There is no universal attempted/succeeded/failed triple — every
#: stage names its own outcomes, and assuming otherwise raised on `gate`, which
#: counts verdicts instead of successes, in 33 tests. A stage absent from this
#: map is not judged; adding one is a deliberate act, not a side effect of
#: adding a stage.
HEALTH_STAGES: dict[str, tuple[str, tuple[str, ...], str]] = {
    "scan": ("files_attempted", ("files_succeeded",), "files_failed"),
    "mine": ("attempted", ("succeeded",), "failed"),
    "rule_families": ("inference_attempted", ("inference_succeeded",), "inference_failed"),
    # A verdict of any kind means the gate ran. `gated_fail` is a working gate
    # doing its job, so it counts as success here; `failed` is the gate itself
    # breaking.
    "gate": (
        "attempted",
        ("gated_pass", "gated_fail", "ungated", "inconclusive"),
        "failed",
    ),
    # Under --review-only every proposal is held, which is the intended
    # outcome and must not read as a dead stage.
    "apply": ("attempted", ("applied", "held"), "failed"),
}


def derive_run_status(stats: dict) -> tuple[str, list[str]]:
    """Return (status, reasons) for a run that completed without raising.

    A stage is degraded only if it attempted work, failed at least once, and
    succeeded at none. Partial failure remains ok; budget refusal alone never
    degrades a run. Raise ValueError for counters that do not match HEALTH_STAGES
    instead of interpreting missing or renamed fields as zero."""
    reasons: list[str] = []
    for stage, (att_key, ok_keys, fail_key) in HEALTH_STAGES.items():
        payload = stats.get(stage)
        if not isinstance(payload, dict) or not payload:
            continue
        wanted = (att_key, *ok_keys, fail_key)
        if not any(k in payload for k in wanted):
            # A stage that reported none of its counters — `{"skipped": ...}`
            # is a real and legal shape. Nothing to judge.
            continue
        missing = [k for k in wanted if type(payload.get(k)) is not int or payload[k] < 0]
        if missing:
            raise ValueError(
                f"stage {stage!r} reported an incomplete health triple: "
                f"missing, non-numeric or invalid nonnegative counts {missing}, got keys {sorted(payload)}"
            )
        attempted = payload[att_key]
        succeeded = sum(payload[k] for k in ok_keys)
        failed = payload[fail_key]
        if stage == 'mine':
            from .stage_accounting import mining_failure_counts
            failed = mining_failure_counts(payload)['execution']
        if attempted > 0 and succeeded == 0 and failed > 0:
            reasons.append(
                f"{stage}: 0 of {attempted} attempts succeeded ({failed} failed)"
            )
    return ("degraded" if reasons else "ok"), reasons


class SandboxUnverified(Exception):
    """The eval sandbox could not be proven to contain an escape.

    Raised instead of running the trial. A proposal that hits this stays
    ``pending`` and is counted under ``gate_sandbox_unverified`` — never
    ``gated_fail``, which would claim the rule was tested and found wanting.
    """


def _ensure_sandbox(cfg, run_dir, state: dict) -> None:
    """Probe the eval sandbox once per run; raise on every later proposal.

    The probe checks both directions: an allowed write must succeed and an
    escape must fail. Checking only the escape would pass on a sandbox that
    blocks everything, which fails every trial for a reason unrelated to the
    rule under test.
    """
    if not cfg.eval_sandbox_enabled:
        return
    if state["ok"] is None:
        from .sandbox import SandboxError, verify_boundary

        try:
            state["observed"] = verify_boundary(
                cfg, probe_root=Path(run_dir) / "sandbox-probe"
            )
            state["ok"] = True
        except SandboxError as exc:
            state["ok"] = False
            state["error"] = str(exc)
    if not state["ok"]:
        raise SandboxUnverified(state["error"])


#: sqlite constraint-failure messages, mapped to a stable key fragment. The
#: message names the table and often the column; the values are never in it.
_INTEGRITY_PATTERNS = (
    (re.compile(r"^UNIQUE constraint failed: ([\w.]+)"), "unique"),
    (re.compile(r"^NOT NULL constraint failed: ([\w.]+)"), "not_null"),
    (re.compile(r"^CHECK constraint failed: ([\w.]+)"), "check"),
    (re.compile(r"^FOREIGN KEY constraint failed"), "foreign_key"),
    (re.compile(r"^PRIMARY KEY constraint failed"), "primary_key"),
)


def integrity_key(exc: BaseException) -> str:
    """Name the specific constraint behind a database integrity failure.

    IntegrityError covers multiple constraints. Classify only recognized messages;
    report an unknown message as ``unparsed`` instead of assigning a guessed cause.
    """
    text = str(exc)
    for pattern, kind in _INTEGRITY_PATTERNS:
        match = pattern.match(text)
        if not match:
            continue
        if not match.groups():
            return f"IntegrityError:{kind}"
        target = match.group(1)
        # UNIQUE names every column in the index; the table is the useful part
        # and the column list makes the key unstable across schema edits.
        return f"IntegrityError:{kind}:{target.split('.')[0] if kind == 'unique' else target}"
    return "IntegrityError:unparsed"


class MineCallFailed(Exception):
    """A call that produced no answer, distinct from an unparseable reply.

    Carry the call's outcome class through the mining report. Parse-failure labels
    require a reply and must not describe a provider process that never started.
    """

    def __init__(self, outcome: str, error: str = ""):
        super().__init__(f"{outcome}: {error}" if error else outcome)
        self.outcome = outcome
        self.error = error


class BudgetExhaustedSignal(Exception):
    pass


def _llm_json(llm, stage: str, model_class: str, prompt: str, *, provenance=None, call_trace=None) -> dict | None:
    """Adapter: LLMRunner.call -> parsed dict or None on parse failure.

    Budget exhaustion propagates as BudgetExhaustedSignal so the pipeline can
    stop the stage and report how much work was refused.
    """
    from .llm import BudgetExhausted

    try:
        result = llm.call(stage, model_class, prompt, expect_json=True,
                          **({'call_trace':call_trace} if call_trace is not None else {}))
    except BudgetExhausted as exc:
        raise BudgetExhaustedSignal(str(exc)) from exc
    from .mining_history import capture
    capture(provenance,result)
    return result.parsed if result.ok else None


def _llm_agentic_json(
    llm, stage: str, model_class: str, prompt: str, sandbox_dir: Path, *, provenance=None
) -> dict | None:
    """Adapter: LLMRunner.call_agentic -> parsed dict or None on failure.

    Mirrors :func:`_llm_json` exactly for budget handling: BudgetExhausted
    propagates as BudgetExhaustedSignal so the mine stage can stop and report
    how many incidents were refused. The agent runs with cwd inside the
    per-incident sandbox prepared by miner.mine_incident_agentic.
    """
    from .llm import BudgetExhausted

    try:
        result = llm.call_agentic(
            stage, model_class, prompt, cwd=str(sandbox_dir), expect_json=True
        )
    except BudgetExhausted as exc:
        raise BudgetExhaustedSignal(str(exc)) from exc
    if not result.ok:
        # The call failed before a parseable reply. Preserve its outcome class.
        raise MineCallFailed(
            getattr(result, "outcome", "") or "other", getattr(result, "error", "") or ""
        )
    from .mining_history import capture
    capture(provenance,result)
    return result.parsed


def _projects_list(learning: dict) -> list[str]:
    """Strict-parse a learning row's projects_json (our own DB invariant)."""
    raw = learning.get("projects_json") or "[]"
    # Guarded, not bare. The shape check below is thorough and could never
    # fire for the likeliest corruption — a value that is not JSON at all —
    # which escaped as a JSONDecodeError carrying a character offset and no
    # row id. The other four readers of this column name the learning; this
    # one is the fifth and was missed.
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"learning {learning.get('id', '<no id>')!r}: projects_json is "
            f"not valid JSON: {exc}"
        ) from exc
    if not isinstance(parsed, list):
        raise ValueError(
            f"learning {learning.get('id', '<no id>')!r}: projects_json is "
            f"not a list: {raw!r}"
        )
    return parsed


def ab_calls_needed(cfg) -> int:
    """Worst-case gate-pool calls for the A/B prune sweep.

    One arm of ``eval_trials`` per applied rule it will re-test, capped by
    ``ab_prune_max_rules_per_run``. Billed to the gate pool, because its trials
    run through the shared ``grade`` stage.
    """
    return cfg.ab_prune_max_rules_per_run * cfg.eval_trials


#: How a provider is invoked to prove it can start. `--version` reaches no
#: model, spends no quota, and exits fast.
_PROVIDER_PROBE = {"claude": "claude_path", "codex": "codex_path"}


def provider_preflight(cfg, *, run=None) -> dict:
    """Probe whether each allowed provider CLI can start without a model call.

    A --version probe exercises executable and interpreter lookup in the actual
    environment. Report each provider's result so the caller can exclude unusable
    candidates. One usable provider is sufficient. This helper does not raise.
    """
    import subprocess

    runner = run or (
        lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=30)
    )
    usable: list[str] = []
    unusable: dict[str, str] = {}
    for name in cfg.allowed_providers:
        attr = _PROVIDER_PROBE.get(name)
        if attr is None:
            unusable[name] = "no probe defined for this provider"
            continue
        path = getattr(cfg, attr, "")
        try:
            proc = runner([path, "--version"])
        except Exception as exc:  # noqa: BLE001 - a broken CLI is a fact, not a crash
            unusable[name] = f"{type(exc).__name__}: {exc}"
            continue
        if proc.returncode == 0:
            usable.append(name)
        else:
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            unusable[name] = f"exit {proc.returncode}: {detail[0] if detail else '(no output)'}"
    return {
        "usable": usable,
        "unusable": unusable,
        "any_usable": bool(usable),
        "probed": list(cfg.allowed_providers),
    }


def gate_calls_needed(cfg) -> int:
    """Worst-case gate-pool calls for one proposal.

    The majority gate runs ``eval_scenarios`` scenarios and does not stop at
    the first decisive one, so EVERY scenario can pay for both arms: one
    eval_gen call, ``eval_trials`` without-rule trials, and ``eval_trials``
    with-rule trials. At the defaults that is 3 x (1 + 3 + 3) = 21, up from the
    retry gate's 11.

    Under-counting here is not a rounding error, and it fails silently: the
    preflight passes, an arm dies halfway, harness.run_trials records the
    BudgetExhausted as ``agent_error``, and the verdict degrades to something
    that reads like a judgement about the rule. That is the exact class of
    false verdict this gate exists to remove.
    """
    from .call_budgets import gate_calls_needed as shared_bound
    return shared_bound(cfg)


def gate_one_proposal(
    store, cfg, llm, learning: dict, proposal: dict, *,
    run_dir, sandbox_state: dict, gate_stats: dict, bump_taxonomy,
    evidence_override: str | None = None, checkpoint=None,
    run_id: str = '', source_revision_id: str = '', history=None,
) -> None:
    """Run the majority gate for ONE proposal. THE gate call site.

    Extracted from `run_pipeline` so the `gate-proposal` CLI command re-gates
    an existing proposal through the SAME code, rather than growing a second
    gate that can drift from this one. An existing proposal can be evaluated
    without first spending a separate mining budget or creating a new proposal.

    Mutates `proposal["status"]` and `proposal["eval_result_id"]` in place and
    bumps `gate_stats`; the caller owns persistence, because the pipeline
    INSERTS a new proposal row and the CLI UPDATES an existing one.
    """
    from .evals import regression
    from . import eval_history
    from .evals.harness import spec_to_dict

    history = history or eval_history.begin(store,cfg,learning,proposal,run_id=run_id,
        command_id=checkpoint.cid if checkpoint is not None else '',source_revision_id=source_revision_id)

    gate_stats["attempted"] += 1
    try:
        # Pre-flight the trial budget: 2 arms x eval_trials cheap
        # calls. Refusing up front beats trials dying mid-arm and
        # polluting the verdict with agent_error noise.
        made = llm.stats()["calls_made"]
        # The gate has a separate budget pool. Mining cannot consume this
        # preflight allowance, and increasing mining capacity does not add to it.
        need = gate_calls_needed(cfg)
        left = (checkpoint.inspect()["budget"]["maximum"]["gate"] if checkpoint is not None
                else cfg.max_gate_calls_per_run - made.get("gate", 0))
        if need > left:
            raise BudgetExhaustedSignal(
                f"gate needs {need} gate calls; {left} left"
            )
        _ensure_sandbox(cfg, run_dir, sandbox_state)
        evidence = _learning_evidence(store, learning) if evidence_override is None else evidence_override
        def generate(scenario):
            history.event(f'scenario:{scenario}:generation:start','generation_started',
                          {'evidence':evidence},scenario=scenario)
            def build():
                try:
                    return regression.generate_spec(learning,
                        lambda prompt: _llm_json(llm,"eval_gen",cfg.strong_model_class,prompt,
                                                call_trace=history.call_trace(scenario=scenario)),
                        PROMPTS_DIR,out_dir=regression_specs_dir(cfg),evidence=evidence,scenario=scenario,
                        record_prompt=lambda data:history.event(f'scenario:{scenario}:prompt','generation_prompt',
                                                                 data,scenario=scenario))
                except Exception as exc:
                    history.event(f'scenario:{scenario}:generation:failed','generation_failed',
                                  {'code':type(exc).__name__,'detail':str(exc)},scenario=scenario)
                    raise
            if checkpoint is None:spec=build()
            else:
                from .evals.harness import spec_from_dict
                frozen=checkpoint.step(f'scenario:{scenario}:spec',{'learning':learning,'evidence':evidence},lambda:spec_to_dict(build()))
                spec=spec_from_dict(frozen,origin=f'job:{checkpoint.cid}:scenario:{scenario}')
            spec_record=spec_to_dict(spec)
            history.event(f'scenario:{scenario}:spec','specification',
                          {'spec':spec_record,'spec_revision':eval_history.digest(spec_record)},scenario=scenario)
            return spec

        def evaluate(spec,scenario):
            options={}
            if checkpoint is not None:
                options={'checkpoint':lambda key,inputs,fn:checkpoint.step(f'scenario:{scenario}:{key}',inputs,fn),
                         'record_result':lambda row:checkpoint.record_eval(scenario,row,history=history)}
            result=regression.gate(spec,learning['rule_text'],_make_sandbox_agent_runner(llm,cfg),cfg,
                store=store,work_dir=run_dir/'trials'/learning['id']/f'scenario-{scenario}',
                history=history,scenario=scenario,**options)
            return result

        verdict = regression.gate_majority(generate,evaluate,scenarios=cfg.eval_scenarios)
        proposal["status"] = verdict["verdict"]
        proposal["eval_result_id"] = verdict["eval_result_id"]
        gate_stats[verdict["verdict"]] += 1
        history.event('result','attempt_result',{k:verdict[k] for k in
                      ('verdict','eval_result_id','scenario_tally','scenarios_run')})
        # Preserve the native stage tally for reports. Detailed scenario
        # rows now join to the actual run through eval_attempt_events.
        gate_stats.setdefault("scenario_splits", []).append(
            {
                "proposal_id": proposal["id"],
                "learning_id": learning["id"],
                "verdict": verdict["verdict"],
                "tally": verdict.get("scenario_tally", {}),
                "scenarios_run": verdict.get("scenarios_run", 0),
            }
        )
    except (BudgetExhaustedSignal, BudgetExhausted) as exc:
        history.stop(exc)
        # BudgetExhausted arrives RAW from harness.run_trials, which
        # re-raises it rather than burying it as agent_error. Both
        # mean the same thing and must land in the same bucket:
        # report._gate_starved_lines keys on this exact string, so
        # filing the raw class name would leave the operator an
        # unfamiliar taxonomy key with no explanation beside it.
        proposal["status"] = "pending"
        # Budget refusal is distinct from a failed gate. A cap can prevent a
        # trial without providing a verdict or evidence of a harness fault.
        gate_stats["refused"] += 1
        bump_taxonomy("gate_budget_exhausted")
    except SandboxUnverified as exc:
        history.stop(exc)
        # NOT gated_fail. The rule was never tested, and recording
        # a verdict about it would repeat the exact mistake this
        # sandbox work exists to fix.
        proposal["status"] = "pending"
        gate_stats["failed"] += 1
        bump_taxonomy("gate_sandbox_unverified")
    except Exception as exc:
        history.stop(exc)
        proposal["status"] = "pending"
        gate_stats["failed"] += 1
        bump_taxonomy(f"gate_{type(exc).__name__}")
    except BaseException as exc:
        history.stop(exc)
        raise


class ProposalNotFound(Exception):
    """No proposal with that id, or its learning is gone. Raised, never guessed."""


def gate_existing_proposal(cfg, store, proposal_id: str, *, _llm_factory=None) -> dict:
    """Re-gate ONE proposal that already exists. Spends `gate_calls_needed(cfg)`.

    This command evaluates an existing proposal without mining new incidents.
    It spends only the gate pool.

    Runs through `gate_one_proposal`, the same function `run_pipeline` uses, so
    the two cannot drift apart.

    Appends eval history against the frozen source revision. Updates an
    undecided proposal only if that revision is still current when the eval
    finishes. A human decision or a delivered rule is never undone by an eval.
    Never applies: gating and applying are separate decisions, and this
    command is the gate.
    """
    from .apply import _record_event
    from .commands import review_snapshot, save_revision, require_schema
    from .store import DECIDED_STATUSES, PROPOSAL_STATUSES

    proposal = store.query_one("SELECT * FROM proposals WHERE id = ?", (proposal_id,))
    if proposal is None:
        raise ProposalNotFound(f"no proposal with id {proposal_id!r}")
    learning = store.query_one(
        "SELECT * FROM learnings WHERE id = ?", (proposal["learning_id"],)
    )
    if learning is None:
        raise ProposalNotFound(
            f"proposal {proposal_id!r} references learning "
            f"{proposal['learning_id']!r}, which does not exist"
        )
    if not (learning.get("rule_text") or "").strip():
        raise ProposalNotFound(
            f"learning {learning['id']!r} has no rule_text; there is nothing to gate"
        )
    if proposal['status'] not in PROPOSAL_STATUSES:
        raise ValueError(f"proposal {proposal_id!r} has unknown status {proposal['status']!r}")
    require_schema(store)

    run_id = new_id()
    run_dir = cfg.state_path("runs", run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    started = utc_now_iso()
    store.insert("runs", {"id": run_id, "started": started, "status": "running"})
    store.commit()
    with store.transaction(write=True):
        source = review_snapshot(store, proposal_id, cfg)
        source_revision_id = save_revision(store, source, started)
        proposal = dict(source['snapshot']['proposal'])
        learning = source['snapshot']['learning']

    if _llm_factory is None:
        from .llm import LLMRunner

        _llm_factory = LLMRunner
    from . import eval_history
    history=eval_history.begin(store,cfg,learning,proposal,run_id=run_id,source_revision_id=source_revision_id)
    try:
        llm = _llm_factory(cfg, store, run_id, run_dir / "raw")
    except BaseException as exc:
        history.stop(exc)
        with store.transaction(write=True):
            store.update('runs','id',run_id,{'status':'error' if isinstance(exc,Exception) else 'interrupted',
                         'finished':utc_now_iso(),'stats_json':json.dumps({'gate_initialization_error':str(exc)})})
        raise
    gate_stats = new_gate_stats()
    taxonomy: dict[str, int] = {}

    def bump(key: str) -> None:
        taxonomy[key] = taxonomy.get(key, 0) + 1

    before = proposal["status"]
    # A refused/failed attempt must not claim the previous eval as its result.
    proposal['eval_result_id'] = ''
    sandbox_state: dict = {"ok": None, "error": "", "observed": {}}
    gate_one_proposal(
        store, cfg, llm, learning, proposal,
        run_dir=run_dir, sandbox_state=sandbox_state,
        gate_stats=gate_stats, bump_taxonomy=bump,
        evidence_override=_evidence_excerpt(next((i['window_json'] for i in source['snapshot']['evidence'] if i['window_json']), '')),
        run_id=run_id,source_revision_id=source_revision_id,history=history,
    )
    store.commit()  # Finish trial persistence before reserving the decision write.
    with store.transaction(write=True):
        current = review_snapshot(store, proposal_id, cfg)
        after = current['snapshot']['proposal']['status']
        if after not in PROPOSAL_STATUSES:
            raise ValueError(f"proposal {proposal_id!r} has unknown status {after!r}")
        if after in DECIDED_STATUSES:
            status_update = 'preserved_decision'
        elif current['revision'] != source['revision']:
            status_update = 'preserved_changed_revision'
        else:
            status_update = 'updated'
            after = proposal['status']
            store.update('proposals', 'id', proposal_id,
                         {'status': after, 'eval_result_id': proposal['eval_result_id']})
        store.insert('proposal_eval_history', {
            'id': new_id(), 'proposal_id': proposal_id, 'source_revision_id': source_revision_id,
            'run_id': run_id, 'eval_result_id': proposal['eval_result_id'],
            'verdict': proposal['status'], 'created_at': utc_now_iso(),
        })
        # The gate made the judgement; actor=user would invent a human decision.
        _record_event(store, proposal_id, 'gated', actor_for('gated'),
                      f"gate-proposal: {before} -> {after}; eval {proposal['status']}; {status_update}")

    stats = {
        "gate": gate_stats,
        "apply": {"attempted": 0, "applied": 0, "held": 0, "failed": 0, "taxonomy": taxonomy, "operation_ids": []},
        "budget_limits": {"cheap": cfg.max_cheap_calls_per_run,
                          "strong": cfg.max_strong_calls_per_run,
                          "gate": cfg.max_gate_calls_per_run},
        "llm": llm.stats(),
        "gate_proposal": {
            "proposal_id": proposal_id,
            "learning_id": learning["id"],
            "status_before": before,
            "status_after": after,
            "eval_verdict": proposal["status"],
            "status_update": status_update,
            "trials_dir": str(run_dir / "trials" / learning["id"]),
        },
    }
    status, reasons = derive_run_status(stats)
    stats["status_reasons"] = reasons
    store.update(
        "runs", "id", run_id,
        {"finished": utc_now_iso(), "status": status,
         "stats_json": json.dumps(stats, ensure_ascii=False)},
    )
    store.commit()
    stats["run_id"] = run_id
    return stats


def _learning_evidence(store, learning: dict) -> str:
    """Redacted transcript excerpt for the learning's first linked incident.

    The eval designer used to see only a paraphrase of the failure. Reading the
    real exchange is what lets it build a trap that actually bites. window_json
    was redacted by redact.py at scan time, so nothing unredacted reaches the
    prompt. Returns "" when no evidence survives, and the prompt says so
    explicitly rather than shipping an empty hole.
    """
    row = store.query_one(
        "SELECT i.window_json FROM incidents i "
        "JOIN incident_learnings il ON il.incident_id = i.id "
        "WHERE il.learning_id = ? AND i.window_json != '' ORDER BY i.ts, i.id LIMIT 1",
        (learning["id"],),
    )
    return _evidence_excerpt(row['window_json'] if row else '')


def _evidence_excerpt(window_json: str) -> str:
    """Render the same retained archive for live and frozen eval inputs."""
    if not window_json:
        return ""
    try:
        entries = json.loads(window_json)
    except json.JSONDecodeError:
        return ""
    if not isinstance(entries, list):
        return ""
    out = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        out.append(f"[{e.get('role', '?')}] {str(e.get('text', ''))}")
    return "\n".join(out)


def _make_sandbox_agent_runner(llm, cfg: Config):
    """Agent runner for eval trials: headless call, OS-sandboxed to the trial dir.

    ``sandbox_dir`` is passed as well as ``cwd``. cwd only sets where the
    process starts; an absolute path walks straight out of it. ``sandbox_dir``
    is what puts a kernel boundary around the whole agent, file tools included.
    """

    def run(prompt: str, sandbox_dir: Path, *, call_trace=None) -> str:
        result = llm.call(
            "grade",
            cfg.cheap_model_class,
            prompt,
            expect_json=False,
            cwd=str(sandbox_dir),
            sandbox_dir=str(sandbox_dir),
            **({'call_trace':call_trace} if call_trace is not None else {}),
        )
        if not result.ok:
            raise RuntimeError(f"eval agent call failed: {result.outcome}")
        return result.text

    run.with_history = lambda history: lambda prompt, sandbox: run(prompt,sandbox,call_trace=history.call_trace())
    return run


def _target_paths(store: Store, cfg: Config) -> list[str]:
    """Every instruction file currently in force for projects we've seen.

    Delegates to routing.instruction_target_paths so the pipeline and the
    read-only search tool enumerate the SAME corpus — they disagreed before,
    and the miner's dedup tool could not see instruction files at all.
    """
    from .routing import instruction_target_paths

    projects = [
        r["project_path"]
        for r in store.query(
            "SELECT DISTINCT project_path FROM sessions WHERE project_path != ''"
        )
    ]
    return instruction_target_paths(projects, cfg)


def _persist_proposal(store: Store, proposal: dict) -> None:
    # The executor compares this creation time with the class's enable time.
    # Return the same value to the caller that is persisted for later readers.
    proposal.setdefault("created_at", utc_now_iso())
    row = {
        "id": proposal["id"],
        "learning_id": proposal["learning_id"],
        "run_id": proposal.get("run_id", ""),
        "target_path": proposal["target_path"],
        "target_kind": proposal["target_kind"],
        "action": proposal["action"],
        "diff_unified": proposal.get("diff_unified", ""),
        "status": proposal.get("status", "pending"),
        "eval_result_id": proposal.get("eval_result_id", ""),
        "created_at": proposal["created_at"],
    }
    store.insert("proposals", row)
    store.insert(
        "proposal_events",
        {
            "id": new_id(),
            "proposal_id": proposal["id"],
            "ts": utc_now_iso(),
            "event": "created",
            "actor": "auto",
            "note": f"status={row['status']}",
        },
    )
