"""Run report generation: pure store reads rendered to one markdown file.

The report combines stored rows, run statistics, configured budgets, and computed
ratios. Call outcomes retain their error taxonomy. Reported caps and truncation
markers show what was omitted. Current backlog sections are distinct from the
run's recorded measurements.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from .config import Config
from .store import Store, QUEUEING_STATUSES, LLM_SUCCESS_OUTCOMES

# llm_calls stages in pipeline order, for stable taxonomy rendering.
#: The two miner stages. `mine` is the cheap single-call miner; `mine_agentic`
#: is the agentic one, and in agentic mode EVERY mine call carries that name.
#: Count both stage names so agentic calls appear in the mining funnel.
MINE_STAGES = ("mine", "mine_agentic")

#: Ordering for the per-stage taxonomy table, in pipeline order. A stage the
#: code emits and this tuple does not know sorts to the end — which is how
#: `mine_agentic` and `contradiction` both went unnoticed. The drift guard in
#: tests/test_report.py reads the emitted names out of pipeline's source.
STAGES = (
    "mine",
    "mine_agentic",
    "cluster",
    "contradiction",
    "propose",
    "eval_gen",
    "grade",
)

# How many session-level scan errors are listed verbatim before eliding.
# The elision itself is reported ("showing X of Y") — a cap, surfaced.
MAX_SCAN_ERRORS_LISTED = 10


class ReportError(Exception):
    """Raised when the report cannot be generated faithfully (e.g. unknown run)."""


#: A per-signal drop rate this many times the smallest is called out as a bias.
#: AGENTS.md: "a limit that bites one group 10x more than another is a bias,
#: not a detail."
CAP_BIAS_RATIO = 10.0


def _gate_starved_lines(stats: dict) -> list[str]:
    """Distinguish "the gate rejected this" from "the gate never ran".

    The gate uses its own call pool. A preflight refusal means that pool could
    not cover the proposal's maximum call allowance. It is not evidence that
    any evaluation trial rejected the rule.
    """
    apply_stats = stats.get("apply") or {}
    n = (apply_stats.get("taxonomy") or {}).get("gate_budget_exhausted", 0)
    if not n:
        return []
    return [
        "",
        f"- **{n} proposal(s) were held because the gate never ran, which is "
        "not a verdict on them.** The gate needs "
        "`eval_scenarios x (1 + 2 x eval_trials)` free calls per proposal from "
        "its own pool (`max_gate_calls_per_run`). That pool had insufficient "
        "calls remaining. Mining uses a separate pool. See the gate outcomes "
        "and call taxonomy in this report.",
    ]


def _gate_majority_lines(stats: dict) -> list[str]:
    """Show the per-scenario split, so 2-1 does not read like 3-0.

    Show the outcome tally beside the verdict so the reader can distinguish
    mixed evidence from failures that supplied no pass/fail vote.
    """
    gate = stats.get("gate") or {}
    splits = gate.get("scenario_splits") or []
    if not splits:
        return []
    lines = [
        "",
        "**Gate scenarios** — each rule is judged on a majority of independently "
        "generated scenarios; an `ungated` scenario reproduced nothing and votes "
        "on nothing.",
        "",
        "| proposal | verdict | pass | fail | ungated | error | scenarios run |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in splits:
        t = row.get("tally") or {}
        lines.append(
            "| {id} | {v} | {p} | {f} | {u} | {e} | {n} |".format(
                id=str(row.get("proposal_id", ""))[:8],
                v=row.get("verdict", ""),
                p=t.get("gated_pass", 0),
                f=t.get("gated_fail", 0),
                u=t.get("ungated", 0),
                e=t.get("error", 0),
                n=row.get("scenarios_run", 0),
            )
        )
    n_inconclusive = int(gate.get("inconclusive", 0))
    if n_inconclusive:
        lines += [
            "",
            f"- **{n_inconclusive} rule(s) came back `inconclusive`**: the "
            "scenarios did not establish a passing or failing majority. The "
            "rule is HELD for review. The tally distinguishes mixed evidence "
            "from errors that supplied no verdict.",
        ]
    return lines


def _wall_clock_lines(stats: dict) -> list[str]:
    """Render wall time, model time, and gaps for every run.

    A long-held nightly lock can prevent the next scheduled run even when stage
    counts look healthy. Always render the section so absence cannot hide a fault."""
    wc = stats.get("wall_clock")
    if not isinstance(wc, dict) or "wall_seconds" not in wc:
        return []
    h = wc["wall_seconds"] / 3600.0
    mh = wc["model_seconds"] / 3600.0
    # With no model calls, a 100% unaccounted share follows by construction.
    # It cannot establish a delay or identify system sleep as the cause.
    if wc["calls"] == 0:
        out = [
            "## Wall clock",
            "",
            f"- Wall {h:.2f} h, and no model calls were made, so there is "
            "nothing to compare it against. A percentage here could only ever "
            "be 100.",
        ]
        return out
    out = [
        "## Wall clock",
        "",
        f"- Wall {h:.2f} h; inside a model call {mh:.2f} h "
        f"({wc['calls']} calls); unaccounted {wc['unaccounted_seconds'] / 3600.0:.2f} h "
        f"({wc['unaccounted_pct']}%).",
    ]
    gap_h = wc["largest_gap_seconds"] / 3600.0
    if wc.get("largest_gap_before_call"):
        out.append(
            f"- Largest single gap between calls: {gap_h:.2f} h, before "
            f"`{wc['largest_gap_before_call']}`."
        )
    out.append(
        "- Unaccounted time is every second outside a recorded call — system "
        "sleep, sandbox setup and scan alike. `time.monotonic()` pauses across "
        "macOS sleep, so `duration_ms` measures model time honestly and the "
        "wall clock does not; the gap between them is what this reports."
    )
    if h > 12:
        out.append(
            "- **This run exceeded 12 h of wall clock.** A process still holding "
            "the nightly lock at the next scheduled launch prevents that launch "
            "from starting a second run."
        )
    out.append("")
    return out


def _parser_blindspot_lines(stats: dict) -> list[str]:
    """Say WHY lines failed, and which record types we do not handle.

    Counts alone cannot distinguish invalid JSON from an unsupported record
    type. Preserve the parser's cause taxonomy through scanning and reporting.

    Unknown record types are the more important half. They are skipped rather
    than failed, which is right for bookkeeping records and WRONG the moment a
    new type carries human text. This block is the only thing standing between
    those two cases, so it renders whenever the count is nonzero.
    """
    scan = stats.get("scan") or {}
    causes = scan.get("malformed_by_cause") or {}
    unknown = scan.get("unknown_line_types") or {}
    skipped = scan.get("skipped_line_types") or {}
    if not causes and not unknown and not skipped:
        return []
    out = [""]
    if causes:
        total = sum(causes.values())
        top = ", ".join(
            f"`{k}` {v:,}" for k, v in sorted(causes.items(), key=lambda kv: -kv[1])[:6]
        )
        out.append(f"- **{total:,} malformed line(s), by cause:** {top}.")
    if unknown:
        total_u = sum(unknown.values())
        top_u = ", ".join(
            f"`{k}` {v:,}" for k, v in sorted(unknown.items(), key=lambda kv: -kv[1])[:6]
        )
        out.append(
            f"- **{total_u:,} line(s) had a record type this parser does not "
            f"handle:** {top_u}. These are SKIPPED, not failed. Check whether any "
            "of them carries human text, an assistant message, or a tool result "
            "— if one does, the miner is blind to it and the type needs a "
            "handler rather than a place on the bookkeeping list."
        )
    if skipped:
        top_s = ", ".join(
            f"`{k}` {v:,}" for k, v in sorted(skipped.items(), key=lambda kv: -kv[1])[:8]
        )
        out.append(
            f"- Skipped on purpose, by record type: {top_s}. Watch the SHAPE of "
            "this list rather than the totals: a type appearing here for the "
            "first time, or one whose volume jumps, is the transcript format "
            "moving under the parser."
        )
    return out


def _identity_split_lines(store) -> list[str]:
    """Incidents that disagree with their own session about which repo they are.

    An incident inherits its session's `project_key`, so the two agreeing is an
    invariant. Incidents determine the project counts used in routing, so a
    split can inflate apparent evidence breadth.

    A resolver failure or a moved checkout can split these identities.
    `backfill-project-keys --requalify` repairs keys when stronger evidence is
    available. A path that no longer identifies a repository needs separate
    review, so the report distinguishes these cases.
    """
    rows = store.query(
        "SELECT s.project_key_method AS method, COUNT(*) AS n "
        "FROM incidents i JOIN sessions s ON i.session_file = s.file_path "
        "WHERE i.project_key <> s.project_key "
        "GROUP BY s.project_key_method ORDER BY n DESC"
    )
    total = sum(int(r["n"]) for r in rows)
    if not total:
        return []
    breakdown = ", ".join(f"`{r['method'] or 'unset'}` {int(r['n']):,}" for r in rows)
    # A degraded method does not establish that stronger identity evidence is
    # available now. Only a new resolution can determine which keys can change.
    return [
        "",
        # Its own heading. Appended bare, these lines rendered under
        # "## Wall clock" — so the warning behind `project_count`, and
        # therefore behind escalation to the GLOBAL instruction file, read as
        # part of a section about system sleep. NOT "Project identity": the
        # error-taxonomy section already has a `### Project identity`
        # subsection holding the resolution-method table, and two headings of
        # the same name in one document is the confusion this was fixing.
        "## Incidents that disagree with their session",
        "",
        f"- **{total:,} incident(s) disagree with their own session about which "
        f"repo they belong to**, by the session's resolution method: {breakdown}. "
        "An incident inherits its session's key, so this is a broken invariant. "
        "Incident identities determine the project counts used in routing.",
        "  - A degraded resolution method alone does not establish that these "
        "keys are repairable. A missing or moved checkout can still lack stronger "
        "identity evidence. `selfimprove backfill-project-keys --requalify --dry-run` "
        "reports exactly how many would change, and writes nothing.",
    ]


def _routing_loss_lines(stats: dict) -> list[str]:
    """Explain learnings that were mined but could not be written anywhere.

    ``propose_RoutingError`` means target construction failed. Missing directories
    and invalid learning metadata are possible causes; the counter alone cannot
    establish which one occurred.
    """
    apply_stats = stats.get("apply") or {}
    taxonomy = apply_stats.get("taxonomy") or {}
    n = taxonomy.get("propose_RoutingError", 0)
    if not n:
        return []
    return [
        "",
        f"- **{n} learning(s) produced no proposal: routing found no writable "
        "target.** Missing or non-Git project directories and invalid learning "
        "metadata can prevent target construction. Inspect the proposal-stage "
        "error details before choosing a repair.",
    ]


def _rate_pct(rate: float) -> str:
    """Percent that keeps a significant digit, so a printed ratio checks out.

    Coarse rounding of a small rate can make the displayed rates disagree
    with their displayed ratio. Below 1% the precision grows to keep the
    leading significant digit.
    """
    if rate <= 0:
        return "0%"
    pct = rate * 100.0
    if pct >= 1:
        return f"{pct:.1f}%"
    # One extra decimal beyond the first significant digit: 0.059% keeps the
    # 9, 0.0012% keeps the 2. Capped so a pathological rate cannot print a
    # 20-character number.
    decimals = min(6, 2 + int(math.floor(-math.log10(pct))))
    rendered = f"{pct:.{decimals}f}".rstrip("0").rstrip(".")
    if float(rendered) == 0.0:
        # A non-zero rate must never look like zero — the same rule `_share`
        # follows for counts. Below the precision cap, say so explicitly
        # rather than rounding a real value out of existence.
        smallest = 10 ** -6
        return f"<{smallest:.6f}".rstrip("0").rstrip(".") + "%"
    return rendered + "%"


def _detector_skip_lines(scan_stats: dict) -> list[str]:
    """Render what each detector skipped rather than guessed.

    `filter_incidents` records a counter every time it declines to invent a
    value it could not read — a missing edit path, a missing file for a
    friction cycle. Both the counts and their causes must reach the report
    so a parser blind spot remains visible after aggregation.
    """
    tax = scan_stats.get("detector_taxonomy") or {}
    tax = {k: v for k, v in tax.items() if v}
    if not tax:
        return []
    lines = [
        "",
        "### Skipped rather than guessed",
        "",
        "Counts of values a detector could not read and refused to invent. A "
        "large number here is not noise — it is a detector silently not firing.",
        "",
    ]
    for name in sorted(tax, key=lambda k: (-int(tax[k]), k)):
        lines.append(f"- `{name}`: {int(tax[name]):,}")
    return lines


def _cap_bias_lines(scan_stats: dict) -> list[str]:
    """Flag a per-signal cap that lands far harder on one signal than another.

    Compares drop RATES (dropped / (dropped + kept)), not raw counts: a common
    signal would otherwise be flagged merely for being common, which is the
    opposite of informative. Reports the widest gap present — the worst rate
    against the smallest non-zero one.

    A near-zero denominator makes the ratio sensitive to small changes in
    the least-affected signal. Read both percentages; a falling multiple
    alone does not establish improvement. Signals the cap never touched are
    absent from ``dropped_by_cap`` and cannot be the ``best`` term. A zero
    rate would make the ratio infinite.
    """
    dropped = scan_stats.get("dropped_by_cap") or {}
    kept = scan_stats.get("incidents_by_signal") or {}
    if len(dropped) < 2:
        return []
    rates = {
        sig: n / (n + kept.get(sig, 0))
        for sig, n in dropped.items()
        if n + kept.get(sig, 0) > 0
    }
    if len(rates) < 2:
        return []
    worst = max(rates, key=rates.get)
    best = min(rates, key=rates.get)
    if rates[best] <= 0 or rates[worst] / rates[best] < CAP_BIAS_RATIO:
        return []
    return [
        "",
        f"- **The per-signal cap is not landing evenly.** `{worst}` loses "
        f"{_rate_pct(rates[worst])} of its incidents to the cap versus "
        f"{_rate_pct(rates[best])} for `{best}` — "
        f"{rates[worst] / rates[best]:.0f}x. A cap that bites one signal that "
        "much harder is a bias in what gets mined, not a detail: those "
        "incidents never reach the miner at all.",
    ]


def _share(n: int, total: int) -> str:
    """Percentage that never renders a non-zero count as 0%.

    A small group must stay visible even when whole-percent rounding would
    hide it.
    """
    if not total:
        return "-"
    pct = n / total
    if n and pct < 0.005:
        return "<1%"
    return f"{pct:.0%}"


def _cell(value: object) -> str:
    """Render one markdown table cell, escaping pipes so content can't break rows."""
    return str(value).replace("|", "\\|").replace("\n", " ")


def _table(headers: list[str], rows: list[list[object]]) -> str:
    """Render a markdown table."""
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join(" --- " for _ in headers) + "|",
    ]
    for row in rows:
        lines.append("| " + " | ".join(_cell(c) for c in row) + " |")
    return "\n".join(lines)


def _session_scope(run: dict) -> tuple[str, tuple]:
    """SQL condition + params scoping sessions to this run's scan window.

    The sessions table has no run_id; scoping uses last_scanned_at (an ISO-UTC
    processing timestamp written at scan time) against the run's started /
    finished bounds. For an unfinished run only the lower bound applies.
    """
    if run["finished"]:
        return "last_scanned_at >= ? AND last_scanned_at <= ?", (
            run["started"],
            run["finished"],
        )
    return "last_scanned_at >= ?", (run["started"],)


def _apply_posture_lines(cfg, stats: dict, store=None) -> list[str]:
    """Report recorded policy separately from actual application outcomes."""
    from .execution_policy import policy_snapshot

    policy = stats.get("execution_policy")
    historical = policy is not None
    if policy is None and store is not None:
        policy = policy_snapshot(store)
    enabled = [name for name, row in policy["classes"].items() if row["enabled"]] if policy else []
    classes = ", ".join(enabled) or "none"
    if stats.get("dry_run"):
        posture = "dry run; no instruction targets were written."
    elif stats.get("review_only"):
        posture = "--review-only; this run authorizes no automatic instruction edits."
    else:
        posture = "automatic application requires a passing gate and an enabled target class."
    source = "Automatic classes at run start" if historical else "Current automatic classes (run policy was not recorded)"
    return [f"- **Apply posture**: {posture}", f"- **{source}**: {classes}.", ""]


def _hollow_eval_lines(store) -> list[str]:
    """Name absent eval evidence without implying automatic authorization."""
    if store is None:
        return []
    from .eval_evidence import no_trial_ran

    rows = store.query(
        "SELECT e.* FROM proposals p LEFT JOIN eval_results e ON e.id=p.eval_result_id "
        "WHERE p.status='ungated'"
    )
    hollow = sum(no_trial_ran(row) for row in rows if row["id"] is not None)
    if not hollow:
        return []
    return [
        f"- **Across the whole backlog, {hollow} of {len(rows)} ungated "
        "proposal(s) rest on an eval where no trial ran** (the agent errored "
        "on every attempt). Ungated proposals require human review. "
        "Across the whole backlog, not this run.",
        "",
    ]


def _counter(block: dict, key: str) -> int | None:
    """One integer counter out of a stats block, or None if it is unreadable.

    Returns None rather than raising: the caller says so in its own line and
    the rest of the report still renders. Absent is 0 — a stage that never
    incremented a counter genuinely has none.
    """
    raw = block.get(key)
    if raw is None:
        return 0
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return int(raw)


def _cfg_attr_or_raise(cfg, name: str):
    """A config value, or a raise. Never a default.

    `dashboard/queries.py` has `_cfg_attr` for exactly this and report.py
    cannot import it — the dashboard is an optional extra and this module runs
    in the headless nightly. Same rule, stated once here: "missing config keys
    raise (config.py)".
    """
    if not hasattr(cfg, name):
        raise ValueError(f"config has no attribute {name!r}")
    return getattr(cfg, name)


def _contradiction_starved_lines(stats: dict, store=None) -> list[str]:
    """The contradiction sweep refused for budget, and how often that happens.

    It bills to ``max_cheap_calls_per_run`` — the pool ``mine_agentic`` draws
    down first — and not to the gate's separate pool. So mining spends the
    budget and the sweep is refused, which reaches ``stats_json`` as
    ``{"skipped": "budget_exhausted"}`` and stopped there. In the taxonomy
    table the stage renders ``attempted 0``, which is also what "nothing to
    do" looks like.

    The corpus figure is COUNTED here rather than typed, because a number in
    a document ages the moment another run lands. Its scope is stated: it is
    every run in the database, not this one.
    """
    con = (stats or {}).get("contradictions")
    if not isinstance(con, dict):
        return []
    # Report work deferred by the judgment cap. Validate the stored counter
    # without letting one unreadable value suppress unrelated report sections.
    deferred = _counter(con, "deferred")
    if deferred is None:
        return [
            "- Contradiction stage counters are unreadable: `deferred` is "
            f"{con.get('deferred')!r}, not a number. Nothing is inferred from "
            "them here.",
            "",
        ]
    if deferred:
        judged = _counter(con, "judged") or 0
        return [
            f"- **Contradiction detection reached its judgment cap: "
            f"{deferred} pair(s) deferred**, {judged} judged. Deferred pairs "
            "are not lost — they are skipped this run and re-offered next "
            "time, so a standing backlog here means the cap "
            "(`contradiction_max_judgments_per_run`) is below the candidate "
            "rate rather than that anything failed.",
            "",
        ]
    if con.get("skipped") != "budget_exhausted":
        return []
    ever = ""
    if store is not None:
        starved = ran = 0
        for row in store.query("SELECT stats_json FROM runs WHERE stats_json <> ''"):
            try:
                d = json.loads(row["stats_json"])
            except (json.JSONDecodeError, TypeError):
                continue
            c = d.get("contradictions")
            if not isinstance(c, dict):
                continue
            if c.get("skipped") == "budget_exhausted":
                starved += 1
            elif "error" not in c:
                ran += 1
        if starved:
            ever = (
                f" Across every run in this database, {starved} reached this "
                f"stage and were refused the same way, and {ran} ran it."
            )
    return [
        "- **Contradiction detection did not run: the mining pool was already "
        "spent.** It bills to `max_cheap_calls_per_run`, the pool "
        "`mine_agentic` draws down first, rather than to the gate's separate "
        f"`max_gate_calls_per_run`.{ever} Increasing the shared cap does not "
        "reserve calls for contradiction detection; mining can consume them too.",
        "",
    ]


def _global_routing_lines(store, cfg) -> list[str]:
    """Compare current project counts with the configured promotion threshold.

    `routing.py` sends a learning there on `project_count >=
    global_promotion_min_projects` OR `scope_guess == "global"`. These current
    counts do not reconstruct the historical branch that selected each target.
    The scope is the whole backlog. Target selection does not grant permission
    to apply a proposal.
    """
    rows = store.query(
        "SELECT l.project_count AS pc FROM proposals p "
        "  JOIN learnings l ON l.id = p.learning_id "
        " WHERE p.target_kind = 'global_claude_md'"
    )
    if not rows:
        return []
    threshold = int(_cfg_attr_or_raise(cfg, "global_promotion_min_projects"))
    by_count = sum(1 for r in rows if int(r["pc"] or 0) >= threshold)
    by_scope = len(rows) - by_count
    if not by_scope:
        return []
    return [
        f"- **{by_scope} of {len(rows)} proposal(s) targeting the global "
        "instruction file currently have fewer projects than the configured "
        f"promotion threshold** ({by_count} meet `project_count >= {threshold}`). "
        "Routing also accepts `scope_guess == global`, which can come from one "
        "project. Current counts do not establish which branch originally selected "
        "each target. Target selection does not grant execution permission. "
        "Across the whole backlog, not this run.",
        "",
    ]


def _mine_coverage_lines(store) -> list[str]:
    """Signal types the miner has never once reached.

    `mine_order` controls sampling under a finite call budget. Current coverage
    counts do not establish arrival rates, future waiting time, or starvation.

    `_cap_bias_lines` covers the per-signal cap at SCAN time, which is a
    different mechanism. This section reports corpus-wide signals with no
    mined incidents. It does not invent a threshold for a low mining rate.
    """
    rows = store.query(
        "SELECT signal_type AS sig, "
        "       COUNT(*) AS n, "
        "       SUM(CASE WHEN status IN ('mined','dismissed') THEN 1 ELSE 0 END) AS mined "
        "  FROM incidents GROUP BY signal_type"
    )
    if len(rows) < 2:
        return []
    total = sum(int(r["n"]) for r in rows)
    if not total:
        return []
    never = [r for r in rows if not int(r["mined"] or 0)]
    if not never:
        return []
    best = max(rows, key=lambda r: (int(r["mined"] or 0) / int(r["n"])) if int(r["n"]) else 0)
    best_rate = 100.0 * int(best["mined"] or 0) / int(best["n"])
    named = ", ".join(
        f"`{r['sig']}` ({int(r['n']):,} incidents, {100.0 * int(r['n']) / total:.0f}% of the corpus)"
        for r in sorted(never, key=lambda r: -int(r["n"]))
    )
    # Show every rate, including low non-zero values, so the reader can assess
    # coverage without an arbitrary cutoff hiding a poorly reached signal.
    table = [
        "| signal | share of corpus | mined |",
        "| --- | ---: | ---: |",
    ]
    for r in sorted(rows, key=lambda r: -(int(r["mined"] or 0) / int(r["n"]) if int(r["n"]) else 0)):
        n_i, m_i = int(r["n"]), int(r["mined"] or 0)
        table.append(
            f"| `{r['sig']}` | {100.0 * n_i / total:.1f}% ({n_i:,}) | "
            f"{100.0 * m_i / n_i if n_i else 0:.1f}% |"
        )
    return [
        "## Mining coverage",
        "",
        f"- **{len(never)} signal type(s) have NEVER been mined**: {named}. "
        f"The most-reached signal is `{best['sig']}` at {best_rate:.0f}%. "
        "`mine_order` is a sampling policy under a finite call budget. These "
        "counts describe the current backlog, not future coverage or this run alone.",
        "",
        "  With `signal_then_recent`, score controls mining priority. "
        "`filter_incidents.py` assigns each `instruction_edit` a fixed `score=0.9`; "
        "other detectors calculate scores from their evidence.",
        "",
        "  Coverage can change as arrivals, remaining backlog, and available "
        "calls change. This table does not measure those rates. Changing scores "
        "or ordering changes the sampling policy; see `docs/RUNBOOK.md`.",
        "",
        *table,
        "",
    ]


def _unnamed_failure_lines(store, run_id: str) -> list[str]:
    """Failed calls this run that no classifier could name.

    Run-scoped, per PRD D4(a): this belongs to one run_id and says so by
    living in the run's own calls section. `other` is the taxonomy's explicit
    "no more specific class", and the classifiers behind it match provider
    WORDING, which changes outside this repo. So a pile of `other` is
    evidence about the CLASSIFIER first and about the providers second.
    No threshold is applied: any number above zero is stated and the operator
    judges it, because a cut-off here would be invented rather than measured.
    """
    from .store import LLM_SUCCESS_OUTCOMES

    holes = ",".join("?" * len(LLM_SUCCESS_OUTCOMES))
    failed = store.query_one(
        f"SELECT COUNT(*) AS n FROM llm_calls WHERE run_id = ? "
        f"AND outcome NOT IN ({holes})",
        (run_id, *LLM_SUCCESS_OUTCOMES),
    )["n"]
    unnamed = store.query_one(
        "SELECT COUNT(*) AS n FROM llm_calls WHERE run_id = ? AND outcome = 'other'",
        (run_id,),
    )["n"]
    if not unnamed:
        return []
    return [
        f"- **{unnamed} of {failed} failed call(s) this run could not be "
        "named** and were recorded as `other`, the taxonomy's \"no more "
        "specific class\". Provider wording can change and leave failures "
        "outside the known patterns. The quota classifier also controls "
        "account retries. Check `_QUOTA_PATTERNS` and `classify_failure_text` "
        "in `llm.py` before attributing these failures to a new provider fault.",
    ]


def _mine_stat(stats: dict, key: str) -> int | str:
    """One counter from this run's own mine stage, or a stated absence."""
    mine = stats.get("mine")
    value = mine.get(key) if isinstance(mine, dict) else None
    return value if isinstance(value, int) else "not recorded"


def _run_scan_int(stats: dict, key: str) -> int | str:
    """One integer from this run's own scan stats, or a stated absence.

    Never a zero default: a missing counter and a counter that really is zero
    are different facts, and a funnel row is exactly where the difference gets
    lost.
    """
    scan = stats.get("scan")
    value = scan.get(key) if isinstance(scan, dict) else None
    return value if isinstance(value, int) else "not recorded"


def _scan_drift_lines(stats: dict, sessions_in_scope: int) -> list[str]:
    """Report when the current session scope differs from the recorded scan.

    Later scans overwrite last_scanned_at, so regenerating an old report can
    exclude previously scanned sessions. Compare with the run's recorded stats
    and disclose drift rather than presenting a smaller count as the original."""
    scan = stats.get("scan")
    if not isinstance(scan, dict):
        return []
    scanned = scan.get("files_succeeded")
    failed = scan.get("files_failed")
    if not isinstance(scanned, int) or not isinstance(failed, int):
        return []
    expected = scanned + failed
    if expected == sessions_in_scope or expected == 0:
        return []
    moved = expected - sessions_in_scope
    if moved > 0:
        return [
            f"- **{moved} of the {expected} sessions this run scanned have "
            "since been re-scanned by a later run.** Sessions are scoped by "
            "`last_scanned_at`, which the later scan overwrote, so every "
            "session number in this report is from the rows as they are NOW, "
            "not as this run left them. The run's own stats are in the "
            "appendix and are immutable.",
            "",
        ]
    return [
        f"- **{-moved} more sessions fall in this run's scan window than it "
        f"reported scanning** ({sessions_in_scope} in scope, {expected} "
        "recorded). Another process wrote `last_scanned_at` inside this run's "
        "window; the session numbers below are not this run's alone.",
        "",
    ]


def generate(store: Store, cfg: Config, run_id: str, out_path: str | Path) -> str:
    """Generate the markdown run report for run_id, write it to out_path.

    Pure store reads (plus cfg for configured budgets); the only write is the
    markdown file itself. Returns str(out_path). Raises ReportError for an
    unknown run_id or a runs.stats_json that is not strict JSON.
    """
    run = store.query_one("SELECT * FROM runs WHERE id = ?", (run_id,))
    if run is None:
        raise ReportError(f"run {run_id!r} not found in runs table")
    try:
        stats = json.loads(run["stats_json"])
    except ValueError as exc:
        raise ReportError(f"run {run_id!r}: stats_json is not strict JSON: {exc}") from exc
    if not isinstance(stats, dict):
        raise ReportError(f"run {run_id!r}: stats_json must be a JSON object")

    scope_sql, scope_params = _session_scope(run)
    sessions = store.query(
        "SELECT status, COUNT(*) AS n, "
        " COALESCE(SUM(lines_scanned), 0) AS lines_scanned, "
        " COALESCE(SUM(malformed_lines), 0) AS malformed_lines, "
        " COALESCE(SUM(bytes_scanned), 0) AS bytes_scanned "
        f"FROM sessions WHERE {scope_sql} GROUP BY status",
        scope_params,
    )
    sess_by_status = {r["status"]: r for r in sessions}
    sessions_total = sum(r["n"] for r in sessions)
    lines_total = sum(r["lines_scanned"] for r in sessions)
    malformed_total = sum(r["malformed_lines"] for r in sessions)
    bytes_total = sum(r["bytes_scanned"] for r in sessions)
    sessions_skipped = stats.get("scan", {}).get("files_skipped_unchanged")
    scan_drift = _scan_drift_lines(stats, sessions_total)

    incidents_by_signal = store.query(
        "SELECT signal_type, COUNT(*) AS n FROM incidents WHERE run_id = ? "
        "GROUP BY signal_type ORDER BY signal_type",
        (run_id,),
    )
    incidents_by_status = store.query(
        "SELECT status, COUNT(*) AS n FROM incidents WHERE run_id = ? GROUP BY status",
        (run_id,),
    )
    incidents_total = sum(r["n"] for r in incidents_by_signal)
    inc_status = {r["status"]: r["n"] for r in incidents_by_status}

    llm_taxonomy = store.query(
        "SELECT stage, outcome, COUNT(*) AS n FROM llm_calls WHERE run_id = ? "
        "GROUP BY stage, outcome ORDER BY stage, outcome",
        (run_id,),
    )
    stage_counts: dict[str, dict[str, int]] = {}
    for row in llm_taxonomy:
        stage_counts.setdefault(row["stage"], {})[row["outcome"]] = row["n"]

    def _stage_asf(*stages: str) -> tuple[int, int, int]:
        outcomes: dict[str, int] = {}
        for stage in stages:
            for key, count in stage_counts.get(stage, {}).items():
                outcomes[key] = outcomes.get(key, 0) + count
        attempted = sum(outcomes.values())
        # Which outcomes are successes is store.LLM_SUCCESS_OUTCOMES, and the
        # reason each one counts is documented there. It was summed inline
        # here, with a second copy in dashboard.queries and the vocabulary
        # itself in llm.OUTCOMES — three lists, nothing reading two together.
        succeeded = sum(outcomes.get(k, 0) for k in LLM_SUCCESS_OUTCOMES)
        return attempted, succeeded, attempted - succeeded

    mine_att, mine_ok, mine_fail = _stage_asf(*MINE_STAGES)

    # Scoped by WHEN the learning was created, not by the run that created its
    # incident. `learnings` has no run_id, and the old join went through
    # `incidents.run_id`, which identifies the scan that found the incident.
    # A later run can mine it. Use the same time-window approach as _session_scope.
    if run["finished"]:
        learnings_run = store.query_one(
            "SELECT COUNT(*) AS n FROM learnings "
            "WHERE created_at >= ? AND created_at <= ?",
            (run["started"], run["finished"]),
        )["n"]
    else:
        learnings_run = store.query_one(
            "SELECT COUNT(*) AS n FROM learnings WHERE created_at >= ?",
            (run["started"],),
        )["n"]
    proposals_by_status = store.query(
        "SELECT status, COUNT(*) AS n FROM proposals WHERE run_id = ? "
        "GROUP BY status ORDER BY status",
        (run_id,),
    )
    applied_count = next(
        (r["n"] for r in proposals_by_status if r["status"] == "applied"), 0
    )

    # ---- assemble --------------------------------------------------------
    out: list[str] = []
    out.append(f"# self-improve run report — `{run_id}`")
    out.append("")
    out.append(f"- **Run id**: `{run_id}`")
    out.append(f"- **Started**: {run['started']}")
    out.append(f"- **Finished**: {run['finished'] or '(not finished)'}")
    out.append(f"- **Status**: {run['status']}")
    # A bare `degraded` is a word, not a diagnosis. The reasons name which
    # stage produced nothing, which is the whole reason the status exists.
    for reason in stats.get("status_reasons", []):
        out.append(f"  - **DEGRADED** — {reason}")
    out.append("")

    out.append("## Funnel")
    out.append("")
    # Before the numbers, not after: a caveat below a table is read second.
    out.extend(scan_drift)
    out.extend(_apply_posture_lines(cfg, stats, store))
    out.extend(_hollow_eval_lines(store))
    out.extend(_global_routing_lines(store, cfg))
    out.extend(_contradiction_starved_lines(stats, store))
    funnel: list[list[object]] = [
        ["Sessions scanned (ok)", sess_by_status.get("ok", {}).get("n", 0)],
        ["Sessions partial", sess_by_status.get("partial", {}).get("n", 0)],
        ["Sessions failed", sess_by_status.get("error", {}).get("n", 0)],
        [
            "Sessions skipped",
            sessions_skipped if sessions_skipped is not None else "not recorded",
        ],
        # Use this run's immutable counters. sessions.lines_scanned holds each
        # session's lifetime total, used separately under "Window efficiency".
        ["Lines scanned (this run)", _run_scan_int(stats, "lines_scanned")],
        ["Malformed lines (this run)", _run_scan_int(stats, "malformed_lines")],
        ["Incidents (this run)", incidents_total],
    ]
    for row in incidents_by_signal:
        funnel.append([f"— incidents: {row['signal_type']}", row["n"]])
    # The three "of those" rows belong DIRECTLY under the incidents they
    # describe. Placing them after the mine-call rows made "— of those, now
    # mined" read as a subdivision of "Mine calls failed", eight rows from
    # what it actually qualifies.
    #
    # These rows describe incidents this run found, even if a later run mined
    # them. `Incidents THIS RUN mined` below reports this run's own mining work.
    funnel += [
        ["— of those, now mined", inc_status.get("mined", 0)],
        ["— of those, dismissed", inc_status.get("dismissed", 0)],
        ["— of those, still new", inc_status.get("new", 0)],
        ["Mine calls attempted", mine_att],
        ["Mine calls succeeded", mine_ok],
        ["Mine calls failed", mine_fail],
        ["Incidents THIS RUN mined", _mine_stat(stats, "succeeded")],
        ["Learnings (this run)", learnings_run],
    ]
    for row in proposals_by_status:
        funnel.append([f"Proposals: {row['status']}", row["n"]])
    if not proposals_by_status:
        funnel.append(["Proposals (this run)", 0])
    funnel.append(["Applied", applied_count])
    out.append(_table(["Stage", "Count"], funnel))
    out.append("")

    out.extend(_routing_loss_lines(stats))
    out.extend(_gate_starved_lines(stats))
    out.extend(_gate_majority_lines(stats))
    out.extend(_parser_blindspot_lines(stats))
    out.extend(_wall_clock_lines(stats))
    out.extend(_identity_split_lines(store))
    out.append("")

    out.extend(_mine_coverage_lines(store))

    out.append("## Error taxonomy by stage")
    out.append("")
    out.append("### LLM calls")
    out.append("")
    if llm_taxonomy:
        ordered = sorted(
            llm_taxonomy,
            key=lambda r: (
                STAGES.index(r["stage"]) if r["stage"] in STAGES else len(STAGES),
                r["outcome"],
            ),
        )
        out.append(
            _table(
                ["Stage", "Outcome", "Count"],
                [[r["stage"], r["outcome"], r["n"]] for r in ordered],
            )
        )
    else:
        out.append("No LLM calls recorded for this run.")
    out.append("")
    out.append("### Scan")
    out.append("")
    error_sessions = store.query(
        f"SELECT file_path, error FROM sessions WHERE {scope_sql} AND status = 'error' "
        "ORDER BY file_path",
        scope_params,
    )
    out.append(f"- Sessions with status=error: {len(error_sessions)}")
    # Cumulative over the sessions in scope, like the lines figure below it —
    # NOT this run's own count, which the funnel reports separately.
    out.append(
        f"- Malformed lines, cumulative over this run's sessions (counted, "
        f"skipped, surfaced): {malformed_total} (this run itself saw "
        f"{_run_scan_int(stats, 'malformed_lines')})"
    )
    for row in error_sessions[:MAX_SCAN_ERRORS_LISTED]:
        out.append(f"  - `{row['file_path']}`: {row['error']}")
    if len(error_sessions) > MAX_SCAN_ERRORS_LISTED:
        out.append(
            f"  - (showing {MAX_SCAN_ERRORS_LISTED} of {len(error_sessions)} "
            "error sessions; full list in the sessions table)"
        )
    out.append("")

    from .scan_reporting import summary as scan_summary
    measurement = scan_summary(stats.get("scan"), owner=f"run {run_id}.scan")
    out.extend(["### Scan observations and coverage", ""])
    if measurement['recorded']:
        out.append(_table(["Measurement", "Count"], [
            [metric['label'], metric['count']] for metric in measurement['metrics']]))
    else:
        out.append(measurement['reason'])
    out.extend(["", measurement['meaning'], ""])
    errors = next(item for item in measurement['counter_maps'] if item['key'] == 'error_taxonomy')
    out.append("#### Scan failures by phase and cause")
    if not errors['recorded']:
        out.append("No scan failure taxonomy was retained.")
    elif errors['counts']:
        out.append(_table(["Cause", "Count"], [[key, value] for key, value in sorted(errors['counts'].items())]))
    else:
        out.append("No scan failures were recorded in this run's taxonomy.")
    out.append("")

    # --- project identity -------------------------------------------------
    # Rendered rather than left to the appendix: a run resolved mostly by
    # remote_url still collapses clones, but fractures the moment a repo is
    # renamed, and a run resolved mostly by 'unresolved' is not collapsing at
    # all. Both are the operator's call and neither is visible from the funnel.
    methods = (stats.get("scan") or {}).get("project_key_methods") or {}
    if methods:
        out.append("### Project identity")
        out.append("")
        total_m = sum(methods.values()) or 1
        from .project_identity import METHODS as _ORDER

        ordered = [m for m in _ORDER if m in methods] + [
            m for m in sorted(methods) if m not in _ORDER
        ]
        out.append(
            _table(
                ["method", "sessions", "share"],
                [[m, methods[m], _share(methods[m], total_m)] for m in ordered],
            )
        )
        unresolved = methods.get("unresolved", 0) + methods.get("path", 0)
        if unresolved > total_m / 2:
            out.append("")
            out.append(
                f"- **{unresolved} of {total_m} sessions ({unresolved / total_m:.0%}) "
                "did not resolve to a repository**, so their clones cannot "
                "collapse and each working copy counts as its own project. "
                "Usually means the directory no longer exists on disk."
            )
        elif not methods.get("gh_repo_id"):
            out.append("")
            out.append(
                "- No session resolved via `gh_repo_id`; identity is keyed on "
                "the remote URL. Clones still collapse, but a repo rename "
                "will split old clones from new ones (GitHub redirects the old "
                "URL forever, so existing checkouts never learn the new name)."
            )
        out.append("")

    reaped = stats.get("stale_runs_reaped") or {}
    if reaped.get("abandoned") or reaped.get("unparseable_started"):
        out.append(
            f"- Stale runs reaped at start: {reaped.get('abandoned', 0)} marked "
            f"`abandoned`, {reaped.get('unparseable_started', 0)} left alone "
            "with an unparseable `started` timestamp."
        )
        out.append("")

    queue = (stats.get("mine") or {}).get("queue") or {}
    if queue:
        out.append("### Mine queue")
        out.append("")
        out.append(
            f"- Order: `{queue.get('order', 'unknown')}`. Ordering controls which "
            "incidents a finite call budget reaches. The recorded queue counts "
            "follow."
        )
        out.append(
            f"- Queued: {queue.get('transcript_present', 0)} with a surviving "
            f"transcript, {queue.get('transcript_already_gone', 0)} minable only "
            "from the archived window."
        )
        out.append("")

    gate = stats.get("gate") or {}
    if gate.get("dup_dropped") or gate.get("dup_dropped_by_miner"):
        by_miner = gate.get("dup_dropped_by_miner", 0)
        total_d = gate.get("dup_dropped", 0)
        out.append("### Dedup channels")
        out.append("")
        out.append(
            f"- Dropped as duplicates: {total_d} total — {by_miner} by the "
            "**miner verdict** (it read the in-force files and named the line), "
            f"{total_d - by_miner} by the embedding check."
        )
        out.append(
            "- These counts describe separate deduplication paths. A zero in "
            "either path alone does not establish prompt, corpus, or threshold "
            "drift. Inspect the source incidents and evaluation evidence."
        )
        out.append("")

    violations = stats.get("invariant_violations") or []
    if violations:
        out.append("### ACCOUNTING BROKEN")
        out.append("")
        out.append(
            "A stage reported an attempted count that does not equal the sum of "
            "its outcomes. Every number below it in this report is suspect, and "
            "the bug is in the accounting rather than the work:"
        )
        out.append("")
        for v in violations:
            out.append(f"- `{v}`")
        out.append("")

    out.append("## FilterIncidents reduction")
    out.append("")
    window_stats = store.query_one(
        "SELECT COUNT(*) AS n, COALESCE(SUM(LENGTH(window_json)), 0) AS chars "
        "FROM incidents WHERE run_id = ?",
        (run_id,),
    )
    window_chars = window_stats["chars"]
    trunc_markers = sum(
        r["window_json"].count("[truncated ")
        for r in store.query(
            "SELECT window_json FROM incidents WHERE run_id = ?", (run_id,)
        )
    )
    out.append(
        f"- Lines scanned, cumulative over this run's sessions: {lines_total} "
        "(each session's lifetime total, which is what the ratio below needs; "
        f"this run itself read {_run_scan_int(stats, 'lines_scanned')})"
    )
    out.append(f"- Bytes scanned: {bytes_total}")
    out.append(
        f"- Chars sent toward mining (sum of incident window_json): {window_chars} "
        f"across {window_stats['n']} incidents"
    )
    if bytes_total > 0:
        out.append(
            f"- Reduction ratio (window chars / bytes scanned): "
            f"{window_chars / bytes_total:.4%}"
        )
    else:
        out.append("- Reduction ratio: n/a (0 bytes scanned)")
    if lines_total > 0:
        out.append(
            f"- Window chars per line scanned: {window_chars / lines_total:.2f}"
        )
    else:
        out.append("- Window chars per line scanned: n/a (0 lines scanned)")
    out.append("")

    out.append("## Caps that bit")
    out.append("")
    out.append(
        f"- `[truncated N chars]` markers inside incident windows: {trunc_markers}"
    )
    scan_stats = stats.get("scan", {})
    caps: dict = {}
    if scan_stats.get("dropped_by_cap"):
        caps["filter_incidents per-signal cap (incidents dropped)"] = scan_stats["dropped_by_cap"]
    if scan_stats.get("files_denylisted"):
        caps["files denylisted"] = scan_stats["files_denylisted"]
    if scan_stats.get("events_denylisted"):
        caps["events denylisted"] = scan_stats["events_denylisted"]
    if scan_stats.get("denylisted_by_substring"):
        caps["denylist hits by substring"] = scan_stats["denylisted_by_substring"]
    llm_stats = stats.get("llm", {})
    if llm_stats.get("refused"):
        refused = {k: v for k, v in llm_stats["refused"].items() if v}
        if refused:
            caps["LLM calls refused by budget"] = refused
    if caps:
        for name in sorted(caps):
            out.append(f"- `{name}`: {json.dumps(caps[name], ensure_ascii=False)}")
        out.extend(_cap_bias_lines(scan_stats))
    elif trunc_markers:
        # Window truncation is a run-scoped cap even when all other cap
        # counters are zero. Keep the summary consistent with its markers.
        out.append(
            "- The window truncation above is the only cap that bit: no "
            "incidents were dropped by a per-signal cap, nothing was "
            "denylisted, and no LLM call was refused by budget."
        )
    else:
        out.append(
            "- No caps bit this run (and denylist/budget counters were all zero)."
        )
    # Detector skips can occur without any cap firing, so report them outside
    # the caps branch.
    out.extend(_detector_skip_lines(scan_stats))
    out.append("")

    out.append("## LLM token usage")
    out.append("")
    usage = store.query(
        "SELECT provider, model_reported, COUNT(*) AS calls, "
        " COALESCE(SUM(tokens_in), 0) AS tokens_in, "
        " COALESCE(SUM(tokens_out), 0) AS tokens_out "
        "FROM llm_calls WHERE run_id = ? GROUP BY provider, model_reported "
        "ORDER BY provider, model_reported",
        (run_id,),
    )
    if usage:
        rows: list[list[object]] = [
            [u["provider"], u["model_reported"] or "(none reported)", u["calls"],
             u["tokens_in"], u["tokens_out"]]
            for u in usage
        ]
        rows.append(
            [
                "**total**",
                "",
                sum(u["calls"] for u in usage),
                sum(u["tokens_in"] for u in usage),
                sum(u["tokens_out"] for u in usage),
            ]
        )
        out.append(
            _table(["Provider", "Model (reported)", "Calls", "Tokens in", "Tokens out"], rows)
        )
    else:
        out.append("No LLM calls recorded for this run.")
    out.append("")

    out.append("## Applied proposals")
    out.append("")
    applied = store.query(
        "SELECT * FROM proposals WHERE run_id = ? AND status = 'applied' "
        "ORDER BY applied_at, id",
        (run_id,),
    )
    if not applied:
        out.append("None applied this run.")
        out.append("")
    for prop in applied:
        learning = store.query_one(
            "SELECT * FROM learnings WHERE id = ?", (prop["learning_id"],)
        )
        evidence = store.query(
            "SELECT i.session_id, i.ts, i.signal_type FROM incident_learnings il "
            "JOIN incidents i ON i.id = il.incident_id WHERE il.learning_id = ? "
            "ORDER BY i.ts",
            (prop["learning_id"],),
        )
        out.append(
            f"### `{prop['target_path']}` — {prop['action']} ({prop['target_kind']})"
        )
        out.append("")
        out.append(f"- **Proposal**: `{prop['id']}`")
        if learning is not None:
            title = learning["title"] or learning["rule_text"]
            out.append(f"- **Learning**: {title}")
        else:
            out.append(f"- **Learning**: `{prop['learning_id']}` (row missing!)")
        out.append(f"- **Applied at**: {prop['applied_at'] or 'not recorded'}")
        out.append(
            f"- **Snapshots**: before `{prop['snapshot_commit_before'] or '?'}`, "
            f"after `{prop['snapshot_commit_after'] or '?'}`"
        )
        out.append(f"- **Evidence**: {len(evidence)} incident(s)")
        for ev in evidence:
            out.append(
                f"  - session `{ev['session_id']}` @ {ev['ts'] or '(no ts)'} "
                f"({ev['signal_type']})"
            )
        out.append("")
        out.append("```diff")
        out.append(prop["diff_unified"] or "(no diff recorded)")
        out.append("```")
        out.append("")

    out.append("## Held / review queue")
    out.append("")
    # Use the shared permission resolver so reports and Review agree.
    from .execution_policy import waiting_proposals

    held = waiting_proposals(store, cfg, run_id=run_id)
    pending = sum(1 for p in held if p["status"] == "pending")
    if not held:
        out.append("Empty.")
    for prop in held:
        learning = store.query_one(
            "SELECT * FROM learnings WHERE id = ?", (prop["learning_id"],)
        )
        rule = (learning["title"] or learning["rule_text"]) if learning else prop["learning_id"]
        verdict = ""
        if prop["eval_result_id"]:
            ev = store.query_one(
                "SELECT verdict FROM eval_results WHERE id = ?", (prop["eval_result_id"],)
            )
            if ev is not None:
                verdict = f" — eval verdict: {ev['verdict']}"
        out.append(
            f"- [{prop['status']}] `{prop['target_path']}` ({prop['action']}): "
            f"{rule}{verdict}"
        )
    if held:
        out.append("")
        out.append(
            f"**{len(held)} proposal(s) await your decision** "
            f"({pending} not yet gated)."
        )
    out.append("")

    out.append("## Budget")
    out.append("")
    out.append(
        f"- Configured budgets: {cfg.max_cheap_calls_per_run} "
        f"`{cfg.cheap_model_class}` calls, {cfg.max_strong_calls_per_run} "
        f"`{cfg.strong_model_class}` calls per run."
    )
    by_class = store.query(
        "SELECT model_requested, COUNT(*) AS n FROM llm_calls WHERE run_id = ? "
        "GROUP BY model_requested ORDER BY model_requested",
        (run_id,),
    )
    def _budget_for(model_requested: str) -> int | None:
        # model_requested stores the provider-mapped model token (e.g.
        # "claude-sonnet-5"), so match by class-token containment — the same
        # rule the identity assertion uses.
        if cfg.cheap_model_class in model_requested:
            return cfg.max_cheap_calls_per_run
        if cfg.strong_model_class in model_requested:
            return cfg.max_strong_calls_per_run
        return None

    for row in by_class:
        budget = _budget_for(row["model_requested"] or "")
        suffix = f" of budget {budget}" if budget is not None else " (no configured budget)"
        out.append(f"- Calls requested as `{row['model_requested'] or '?'}`: {row['n']}{suffix}")
    refusals = store.query(
        "SELECT stage, provider, COUNT(*) AS n FROM llm_calls "
        "WHERE run_id = ? AND outcome = 'quota_exhausted' "
        "GROUP BY stage, provider ORDER BY stage, provider",
        (run_id,),
    )
    refusal_total = sum(r["n"] for r in refusals)
    out.append(f"- Budget/quota refusals (`quota_exhausted` outcomes): {refusal_total}")
    for row in refusals:
        out.append(f"  - stage {row['stage']}, provider {row['provider'] or '?'}: {row['n']}")
    out.extend(_unnamed_failure_lines(store, run_id))
    if run["status"] == "budget_exhausted":
        out.append("- **Run ended with status `budget_exhausted`.**")
    out.append("")

    out.append("## Appendix: raw run stats_json")
    out.append("")
    out.append("```json")
    out.append(json.dumps(stats, indent=2, ensure_ascii=False, sort_keys=True))
    out.append("```")
    out.append("")

    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out), encoding="utf-8")
    return str(path)
