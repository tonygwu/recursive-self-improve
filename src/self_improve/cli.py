"""selfimprove CLI: mine trajectories into eval-gated instruction-file improvements."""

from __future__ import annotations

import argparse
import json
import sys

from .config import load_config
from .store import Store



def _last_run_health(last_run: dict | None) -> dict | None:
    """The last run's status and, when it is not ok, why — in plain words.

    Returns None when there is no run at all, which is a different fact from a
    run with nothing wrong. An unparseable `stats_json` is REPORTED rather than
    swallowed: a row whose stats cannot be read is a fact about that run, and
    hiding it would make it look healthy.
    """
    if not last_run:
        return None
    status = last_run.get("status", "unknown")
    raw = last_run.get("stats_json") or "{}"
    try:
        stats = json.loads(raw)
    except (ValueError, TypeError) as exc:
        return {"status": status, "reasons": [f"stats_json is unreadable: {exc}"]}
    if not isinstance(stats, dict):
        return {
            "status": status,
            "reasons": [f"stats_json is unreadable: not an object ({type(stats).__name__})"],
        }
    reasons = stats.get("status_reasons") or []
    if not isinstance(reasons, list):
        return {"status": status, "reasons": [f"status_reasons is unreadable: {reasons!r}"]}
    return {
        "status": status,
        "reasons": [str(r) for r in reasons],
        # Explicit run vetoes are independent of persisted class settings.
        # This records run intent, not evidence that any file was written.
        "wrote_nothing_because": (
            "dry_run" if stats.get("dry_run")
            else "review_only" if stats.get("review_only")
            else None
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="selfimprove",
        description="Mine Claude Code / Codex trajectories into eval-gated CLAUDE.md/AGENTS.md improvements.",
    )
    parser.add_argument("--config", help="path to config.toml override file")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="full pipeline: scan, mine, propose, gate, apply, report")
    p_run.add_argument("--dry-run", action="store_true", help="scan + filter-incidents + report only; no LLM calls, no writes to targets")
    p_run.add_argument("--review-only", action="store_true", help="full pipeline but nothing auto-applies")
    p_run.add_argument(
        "--max-cheap-calls",
        type=int,
        default=None,
        help=(
            "cap on non-gate cheap-model calls, including mining. Evaluation "
            "uses the separate max_gate_calls_per_run pool."
        ),
    )
    p_run.add_argument(
        "--max-strong-calls",
        type=int,
        default=None,
        help="cap on non-gate strong-model calls, including cluster merges; "
        "eval generation uses the separate gate pool",
    )
    p_run.add_argument(
        "--project",
        default="",
        help="substring filter: mine only incidents matching this cwd, canonical "
        "project key, or current repo name (so a renamed repo matches under "
        "either name)",
    )

    sub.add_parser("scan", help="incremental transcript indexing + filter-incidents only")
    sub.add_parser("status", help="DB and last-run summary")
    p_worker = sub.add_parser("worker", help="recover instruction writes and deliver approved commands using the existing state database")
    worker_mode = p_worker.add_mutually_exclusive_group()
    worker_mode.add_argument("--once", action="store_true", help="process one queued command or interrupted instruction operation, then exit")
    worker_mode.add_argument("--operation", metavar="ID", help="retry or reconcile one recorded instruction operation, then exit; no model calls")
    p_jobs = sub.add_parser('jobs',help='execute explicitly requested model jobs independently of instruction delivery')
    p_jobs.add_argument('--once',action='store_true',help='execute or resume one authorized model job, then exit')

    p_rb = sub.add_parser("rollback", help="revert an applied proposal")
    p_rb.add_argument("proposal_id")

    p_gp = sub.add_parser(
        "gate-proposal",
        help="put ONE existing proposal through the majority eval gate "
        "(SPENDS real model calls)",
    )
    p_gp.add_argument("proposal_id")
    p_gp.add_argument(
        "--yes",
        action="store_true",
        help="required. Without it the command prints the cost and exits 2 "
        "without spending anything.",
    )

    p_rep = sub.add_parser("report", help="regenerate the report for a run")
    p_rep.add_argument("run_id")

    p_se = sub.add_parser(
        "self-eval", help="run the miner self-eval against the labeled incident set"
    )
    p_se.add_argument("--dataset", help="private frozen dataset directory (required)")
    p_se.add_argument("--manifest", help="expected dataset manifest to reproduce a pinned evaluation")
    p_se.add_argument(
        "--detection-only",
        action="store_true",
        help="check only whether the labeled incidents produced candidates at "
        "all (the gate's precondition); no LLM calls, no cost",
    )

    p_sl = sub.add_parser(
        "search-learnings",
        help="embedding search over the learnings table (read-only; used by the "
        "sandboxed agentic miner for dedup, and by humans)",
    )
    p_sl.add_argument("query")
    p_sl.add_argument("--top", type=int, default=8)
    p_sl.add_argument(
        "--status",
        action="append",
        default=None,
        help="narrow to a status (repeatable); default searches all statuses",
    )

    p_rs = sub.add_parser(
        "rescan",
        help="mark sessions for re-filtering (use after adding detectors); "
        "clears scan offsets and drops un-mined incidents, then re-scans",
    )
    p_rs.add_argument("--project", default="", help="substring filter; empty = all sessions")
    p_rs.add_argument(
        "--mark-only",
        action="store_true",
        help="mark for rescan without immediately running the scan",
    )

    p_rvp = sub.add_parser(
        "reverify-partial",
        help="re-read only the sessions flagged 'partial', from offset 0, and "
        "re-derive that flag; deletes no incidents",
    )
    p_rvp.add_argument(
        "--mark-only",
        action="store_true",
        help="mark for a full re-read without immediately running the scan",
    )

    sub.add_parser(
        "contradictions",
        help="list open (unresolved) contradictions between instruction-file rules",
    )

    p_re = sub.add_parser(
        "eval-retrieval",
        help="score the dedup retrieval layer (semantic / keyword-proxy / oracle "
        "union) against an explicit frozen dataset; no LLM calls",
    )
    p_re.add_argument(
        "--refresh-corpus",
        action="store_true",
        help="export an unjudged corpus from the live DB (read-only); requires "
        "a new --private-destination file outside Git",
    )
    p_re.add_argument("--private-destination", help="new private corpus JSONL file outside all checkouts")
    p_re.add_argument("--instruction-file", action="append", help="instruction file to snapshot (repeatable)")
    selection = p_re.add_mutually_exclusive_group()
    selection.add_argument("--dataset", help="private frozen dataset directory")
    selection.add_argument("--synthetic", action="store_true", help="run the invented public demo; not a real benchmark")
    p_re.add_argument("--manifest", help="expected dataset manifest to reproduce a pinned evaluation")
    p_re.add_argument("--json", action="store_true", help="emit the full result as JSON")

    p_bf = sub.add_parser(
        "backfill-project-keys",
        help="populate canonical project identity on rows scanned before "
        "migration 0006 (idempotent; --dry-run reports without writing)",
    )
    p_bf.add_argument("--dry-run", action="store_true")
    p_bf.add_argument(
        "--no-gh",
        action="store_true",
        help="skip the `gh api` step: key on the normalized remote URL only. "
        "Collapses clones but fractures if a repo is later renamed.",
    )
    p_bf.add_argument(
        "--requalify",
        action="store_true",
        help="ALSO re-key rows whose identity was resolved by a WORSE method "
        "than is available now, e.g. sessions keyed remote_url during an "
        "outage where `gh` was unreachable. Only ever moves a key UP the "
        "resolution order, never down. Off by default: re-keying existing "
        "rows is a data migration, not a routine backfill.",
    )

    p_rbd = sub.add_parser(
        "rebuild-state",
        help="wipe derived state so a re-scan can rebuild it, preserving the "
        "incidents whose transcripts are already deleted (they cannot be "
        "regenerated). Requires --export; --dry-run reports without writing.",
    )
    p_rbd.add_argument(
        "--export",
        required=True,
        help="new private backup directory outside Git; verify preserved rows before deletion",
    )
    p_rbd.add_argument("--dry-run", action="store_true")
    p_rbd.add_argument("--reason", default="rebuild derived state after a scan/mine logic change")

    p_dash = sub.add_parser(
        "dashboard",
        help="serve the local dashboard on 127.0.0.1 (needs the "
        "dashboard extra: uv sync --extra dashboard)",
    )
    p_dash.add_argument("--port", type=int, default=8765)
    p_dash.add_argument(
        "--host",
        default="127.0.0.1",
        help="loopback only. The dashboard has no authentication and serves "
        "redacted transcript excerpts, so a non-loopback bind is refused.",
    )

    p_ld = sub.add_parser("install-launchd", help="install the nightly launchd job")
    p_ld.add_argument("--uninstall", action="store_true")

    args = parser.parse_args(argv)
    # Resolve dataset selection and export targets BEFORE reading configuration
    # or opening any Store. A missing private benchmark is never a demo result.
    if args.command == "eval-retrieval":
        return _retrieval_command(args)
    if args.command == "self-eval":
        from pathlib import Path
        from .data_boundary import DataBoundaryError, load_dataset
        try:
            if not args.dataset:
                raise DataBoundaryError("self-eval requires --dataset pointing to a private frozen dataset")
            args.dataset_manifest = load_dataset(Path(args.dataset).expanduser(), kind="private",
                expected=Path(args.manifest).expanduser() if args.manifest else None)
        except (DataBoundaryError, OSError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
    if args.command == "rebuild-state":
        from pathlib import Path
        from .data_boundary import DataBoundaryError, private_destination
        try:
            private_destination(Path(args.export))
        except (DataBoundaryError, OSError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
    cfg = load_config(args.config)

    # Construct read-only handles for query commands. A normal Store can migrate
    # before the command performs its first query.
    READ_ONLY_COMMANDS = {"status", "contradictions", "report"}

    def _is_read_only(a) -> bool:
        """Whether this command requires a read-only database handle.

        Both self-evaluation modes read existing rows. Decide before the shared handle
        is constructed so no earlier writable open can migrate their database.
        """
        if a.command in READ_ONLY_COMMANDS:
            return True
        return a.command == "self-eval"

    if args.command == "rebuild-state":
        from .rebuild import rebuild_state

        try:
            store = Store(cfg.state_path("state.db"), read_only=args.dry_run, migrate=False)
            try:
                stats = rebuild_state(
                    store,
                    export_path=args.export,
                    dry_run=args.dry_run,
                    reason=args.reason,
                )
            finally:
                store.close()
        except (DataBoundaryError, OSError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(json.dumps(stats, indent=2))
        return 0

    if args.command == "backfill-project-keys":
        from .backfill import backfill_project_identity

        try:
            store = Store(cfg.state_path("state.db"), read_only=args.dry_run)
        except FileNotFoundError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        try:
            stats = backfill_project_identity(
                store, dry_run=args.dry_run, use_gh=not args.no_gh,
                requalify=args.requalify
            )
        finally:
            store.close()
        print(json.dumps(stats, indent=2))
        return 0

    if args.command in {"worker","jobs"}:
        if args.command=='jobs':
            from .job_worker import serve
        else:
            from .worker import serve
        from .commands import CommandError
        try:
            return serve(cfg, once=args.once,**({'operation_id':args.operation} if args.command=='worker' else {}))
        except KeyboardInterrupt:
            return 0
        except (CommandError, FileNotFoundError) as exc:
            print(str(exc), file=sys.stderr)
            return 2

    if args.command == "dashboard":
        # The dashboard owns its database handles. Return before the CLI constructs
        # another Store, which could otherwise migrate before serving any request.
        from .dashboard import app as dashboard_app

        try:
            return dashboard_app.serve(cfg, host=args.host, port=args.port)
        except (
            dashboard_app.DashboardExtraMissing,
            dashboard_app.DashboardStartupError,
        ) as exc:
            # A clear sentence, not a traceback: every one of these says what
            # is wrong and what to run.
            print(str(exc), file=sys.stderr)
            return 2

    if args.command == "search-learnings":
        # Deliberately no Store(): this path is reachable from inside the
        # sandboxed agentic miner's Bash allowlist and must stay read-only —
        # search.py opens the DB with mode=ro.
        from .search import search_learnings

        results = search_learnings(
            cfg,
            args.query,
            top_k=args.top,
            statuses=tuple(args.status) if args.status else (),
            with_meta=True,
        )
        print(json.dumps(results, indent=2, ensure_ascii=False))
        return 0

    if args.command == "install-launchd":
        # Handle scheduler commands before opening a Store. They manage service files
        # and do not need a database connection or schema migration.
        from . import launchd as _launchd

        print(_launchd.uninstall(cfg) if args.uninstall else _launchd.install(cfg))
        return 0

    store = Store(
        cfg.state_path("state.db"),
        read_only=_is_read_only(args),
    )
    try:
        return _dispatch(args, cfg, store)
    finally:
        store.close()


def _dispatch(args, cfg, store) -> int:
    from . import pipeline

    if args.command == "run":
        stats = pipeline.run_pipeline(
            cfg,
            store,
            dry_run=args.dry_run,
            review_only=args.review_only,
            max_cheap_calls=args.max_cheap_calls,
            max_strong_calls=args.max_strong_calls,
            project_filter=args.project,
        )
        print(json.dumps(stats, indent=2, ensure_ascii=False))
        print(f"\nreport: {stats.get('report_path', '(none)')}")
        return 0

    if args.command == "scan":
        stats = pipeline.run_pipeline(cfg, store, dry_run=True)
        print(json.dumps(stats, indent=2, ensure_ascii=False))
        return 0

    if args.command == "status":
        from .execution_policy import policy_snapshot
        from .operations import operation_inventory
        counts = {}
        for table in ("sessions", "incidents", "learnings", "proposals", "llm_calls"):
            counts[table] = store.query_one(f"SELECT COUNT(*) AS n FROM {table}")["n"]
        last_run = store.query_one("SELECT * FROM runs ORDER BY started DESC LIMIT 1")
        by_status = store.query(
            "SELECT status, COUNT(*) AS n FROM proposals GROUP BY status ORDER BY n DESC"
        )
        print(
            json.dumps(
                {
                    "counts": counts,
                    # `last_run` carries stats_json as an escaped string, which
                    # is where the reasons a run was degraded became technically
                    # present and practically unreadable. This is the one
                    # command an operator runs to ask whether last night was
                    # fine, so it answers in words.
                    "execution_policy": policy_snapshot(store),
                    "instruction_operations": operation_inventory(store),
                    "legacy_auto_apply": cfg.auto_apply,
                    "last_run_health": _last_run_health(last_run),
                    "last_run": last_run,
                    "proposals_by_status": by_status,
                },
                indent=2,
            )
        )
        return 0

    if args.command == "gate-proposal":
        from .pipeline import (
            ProposalNotFound,
            gate_calls_needed,
            gate_existing_proposal,
        )

        need = gate_calls_needed(cfg)
        if not args.yes:
            # A command that spends the operator's quota states the cost and
            # refuses by default. Exit 2, not 1: nothing failed, and a caller
            # must be able to tell "you did not confirm" from "the gate ran
            # and the rule lost".
            print(
                json.dumps(
                    {
                        "refused": "confirmation required",
                        "would_spend_gate_calls": need,
                        "breakdown": (
                            f"{cfg.eval_scenarios} scenarios x "
                            f"(1 eval_gen + 2 arms x {cfg.eval_trials} trials)"
                        ),
                        "note": (
                            "worst case. A scenario whose without-rule arm "
                            "cannot reproduce the mistake returns `ungated` "
                            "and skips its with-rule arm, and the majority "
                            "exits early once the vote is settled."
                        ),
                        "rerun_with": "--yes",
                    },
                    indent=2,
                )
            )
            return 2
        try:
            stats = gate_existing_proposal(cfg, store, args.proposal_id)
        except ProposalNotFound as exc:
            print(json.dumps({"error": str(exc)}, indent=2))
            return 1
        print(json.dumps(stats, indent=2, default=str))
        # A verdict of any kind means the gate ran. `gated_fail` is the gate
        # working, so it is exit 0; only a gate that could not run is exit 1.
        g = stats["gate"]
        ran = g["gated_pass"] + g["gated_fail"] + g["ungated"] + g["inconclusive"]
        return 0 if ran else 1

    if args.command == "rollback":
        from .apply import rollback

        outcome = rollback(store, cfg, args.proposal_id)
        print(json.dumps(outcome, indent=2))
        # Indexing, not .get: a missing 'outcome' key is a contract bug and
        # should raise, never silently exit 1.
        return 0 if outcome["outcome"] == "rolled_back" else 1

    if args.command == "report":
        from . import report

        out = cfg.state_path("runs", args.run_id, "report.md")
        report.generate(store, cfg, args.run_id, out)
        print(f"report: {out}")
        return 0

    if args.command == "self-eval":
        from pathlib import Path

        from . import cluster
        from .evals import self_eval

        labeled_dir = Path(args.dataset).expanduser() / "labeled"
        labeled = self_eval.load_labeled(labeled_dir)
        if args.detection_only:
            # Read-only handle: this check must never migrate the DB (see
            # READ_ONLY_COMMANDS above).
            cov = self_eval.detection_coverage(store, labeled)
            cov["dataset"] = {k: args.dataset_manifest[k] for k in ("dataset_id", "version", "kind", "sha256")}
            print(json.dumps(cov, indent=2))
            print(
                f"\n{cov['covered']}/{cov['total']} labeled incidents have at "
                "least one candidate to mine.\nThis is the PRECONDITION for the "
                ">=4/5 rediscovery gate, not the gate itself."
            )
            return 0 if cov["covered"] == cov["total"] else 1
        produced = store.query(
            "SELECT * FROM learnings WHERE status != 'superseded'"
        )

        # Deterministic token-overlap judge (transparent MVP stand-in for an
        # LLM judge; threshold surfaced in the output so results are auditable).
        threshold = 0.35

        def judge(expected_gist: str, rule_text: str) -> bool:
            return (
                cluster.jaccard(
                    cluster.normalize_tokens(expected_gist),
                    cluster.normalize_tokens(rule_text),
                )
                >= threshold
            )

        result = self_eval.evaluate(produced, labeled, judge)
        result["dataset"] = {k: args.dataset_manifest[k] for k in ("dataset_id", "version", "kind", "sha256")}
        result["judge"] = f"token-jaccard>={threshold} (deterministic; not an LLM judge)"
        result["produced_learnings"] = len(produced)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    if args.command == "rescan":
        from . import pipeline, scan

        marked = scan.mark_for_rescan(store, cfg, args.project)
        print(json.dumps({"marked": marked}, indent=2))
        if args.mark_only:
            return 0
        stats = pipeline.run_pipeline(cfg, store, dry_run=True)
        print(json.dumps(stats.get("scan", {}), indent=2))
        return 0

    if args.command == "reverify-partial":
        from . import pipeline, scan

        # Partial status comes from the last full read. Re-read only flagged
        # sessions and preserve their incidents; a general rescan rebuilds
        # incidents and is unnecessarily broad for this check.
        before = store.query_one(
            "SELECT COUNT(*) AS n FROM sessions WHERE status = 'partial'"
        )["n"]
        marked = scan.mark_partial_for_reverify(store)
        print(json.dumps({"partial_before": before, "marked": marked}, indent=2))
        if args.mark_only:
            return 0
        stats = pipeline.run_pipeline(cfg, store, dry_run=True)
        after = store.query_one(
            "SELECT COUNT(*) AS n FROM sessions WHERE status = 'partial'"
        )["n"]
        still = store.query(
            "SELECT file_path, malformed_lines, malformed_by_cause FROM sessions "
            "WHERE status = 'partial'"
        )
        print(json.dumps({
            "partial_before": before,
            "partial_after": after,
            "cleared": before - after,
            "still_partial": [dict(r) for r in still],
            "scan": stats.get("scan", {}),
        }, indent=2, default=str))
        return 0

    if args.command == "contradictions":
        from . import contradictions

        rows = contradictions.report_open(store)
        if not rows:
            # Report no open contradictions only after the detector completed.
            # An empty table alone does not distinguish no findings from no check.
            completed, why = contradictions.ever_completed(store)
            if completed:
                print("No open contradictions.")
            else:
                print(
                    "No contradiction has ever been computed — the stage has "
                    "not completed in any recorded run, so this is NOT "
                    "'checked and found none'."
                )
                print(f"  what the runs recorded instead: {why}")
            return 0
        for r in rows:
            print(f"\n--- {r['file_a']}\n  A: {r['unit_a']}")
            print(f"--- {r['file_b']}\n  B: {r['unit_b']}")
            print(f"  cosine {r['cosine']:.3f}: {r['explanation']}")
        print(f"\n{len(rows)} open contradiction(s).")
        return 0


    raise AssertionError(f"unhandled command {args.command}")


def _retrieval_command(args) -> int:
    from pathlib import Path
    from .config import Config
    from .data_boundary import DataBoundaryError, SYNTHETIC_DATASET, private_destination
    from .evals import retrieval as reteval
    from .embeddings import EmbeddingError

    try:
        if args.refresh_corpus:
            if not args.private_destination:
                raise DataBoundaryError("--refresh-corpus requires --private-destination: a new file outside Git")
            if args.dataset or args.synthetic or args.manifest:
                raise DataBoundaryError("refresh exports an unjudged corpus; omit --dataset, --synthetic, and --manifest")
            target = private_destination(Path(args.private_destination))
            cfg = load_config(args.config)
            meta = reteval.snapshot_corpus(cfg, args.instruction_file or [cfg.global_claude_md], target)
            print(json.dumps(meta, indent=2))
            print("Private corpus exported. Adjudicate qrels and freeze corpus, qrels, and labels together before scoring.")
            return 0
        if args.private_destination or args.instruction_file:
            raise DataBoundaryError("--private-destination and --instruction-file require --refresh-corpus")
        if not args.dataset and not args.synthetic:
            raise DataBoundaryError("select --dataset PATH for a private benchmark or --synthetic for the invented demo")
        dataset = SYNTHETIC_DATASET if args.synthetic else Path(args.dataset).expanduser()
        # Ordinary evaluation does not inspect the operator's default config.
        # An explicit config may pin the embedding model for reproduction.
        if args.config and not Path(args.config).is_file():
            raise DataBoundaryError(f"explicit evaluation config does not exist: {args.config}")
        cfg = load_config(args.config) if args.config else Config()
        result = reteval.run(cfg, dataset,
            expected_manifest=Path(args.manifest).expanduser() if args.manifest else None,
            kind="synthetic" if args.synthetic else "private")
        print(json.dumps(result, indent=2) if args.json else reteval.render_text_report(result))
        return 0
    except (DataBoundaryError, reteval.RetrievalEvalError, EmbeddingError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
