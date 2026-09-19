import {initNavigation, isEditing} from "./navigation.js";

/* Dashboard single-page app. Vanilla ES module with same-origin requests.
 *
 * Overview, Rules, and Projects display recorded data. Review records explicit
 * commands and shows delivery, rollback, and model-job progress. File writes run
 * in separate workers. GET and POST requests use their shared transport helpers.
 *
 * Missing measurements retain their reasons; unknown states stay visible.
 * Rules search reads retained sources through bounded, revision-bound pages.
 * Theme selection uses localStorage when available and otherwise stays local
 * to the current page. Assets require no CDN, webfonts, or build step.
 */

/** Shared API routes; detail paths are built from record identifiers. */
export const API = Object.freeze({
  overview: "/api/overview",
  rules: "/api/rules/browse",
  evidence: "/api/evidence",
  projects: "/api/projects",
  review: "/api/review-queue",
  reviewPreview: "/api/review-preview",
  commands: "/api/commands",
  operations: "/api/operations",
  incidents: "/api/incidents",
  runs: "/api/runs",
  evalAttempts: "/api/eval-attempts",
  evalResults: "/api/eval-results",
  evalHealth: "/api/eval-health",
  classEvidence: "/api/class-evidence",
  qualityPreview: "/api/quality-preview",
  qualitySamples: "/api/quality-samples",
});

/** Compatibility decision route, built from the proposal identifier. */
export function decisionUrl(proposalId) {
  return `/api/proposals/${encodeURIComponent(proposalId)}/decision`;
}

export function rollbackPreviewUrl(proposalId) {
  return `/api/proposals/${encodeURIComponent(proposalId)}/rollback-preview`;
}

/** What a decision word means, in the words the button uses. */
export const DECISIONS = Object.freeze({
  approve: "Approval recorded. The selected edit is queued for delivery.",
  reject: "Rejected. This rule will not be applied.",
});

export const VIEWS = Object.freeze(["overview", "rules", "projects", "review", "evals"]);

/** The four gate verdicts, in the order the trust model reads them. */
export const GATE_VERDICTS = Object.freeze([
  "gated_pass",
  "gated_fail",
  "ungated",
  "inconclusive",
]);

export const VERDICT_MEANING = Object.freeze({
  gated_pass: "gate passed — automatic delivery also requires an enabled target class",
  gated_fail: "HELD — the gate failure threshold was met",
  ungated: "needs review — the eval did not establish that this rule prevents the mistake",
  inconclusive: "HELD — scenarios did not establish a passing or failing majority",
});

/**
 * Cell states app.css draws. Anything else renders as an unmapped alarm.
 *
 * Every state `queries.CELL_STATES_WORST_FIRST` can emit belongs here EXCEPT
 * `unreadable` and `unknown_status`, for which the alarm is the whole point:
 * both mean the database holds a shape this dashboard has never been taught,
 * and a neutral colour would hide a pipeline change. `refused` is not one of
 * those. It is a designed outcome the gate produces on real nights — the call
 * budget ran out before the stage was asked — and AGENTS.md forbids reading it
 * as a failure. It is drawn, not alarmed.
 * `tests/test_dashboard_spa.py` holds this list against the data layer's own.
 */
export const DRAWN_STATES = Object.freeze([
  "ok",
  "partial",
  "failed",
  "error",
  "running",
  "skipped",
  "degraded",
  "interrupted",
  "abandoned",
  "budget_exhausted",
  "refused",
  "unaccounted",
  "limited",
  "info",
]);

export const STATE_MEANING = Object.freeze({
  ok: "ran, and the stage succeeded",
  partial: "ran, with failures",
  failed: "the stage failed",
  error: "the run ended in error",
  running: "never reported completion",
  skipped: "did not run",
  degraded: "the run finished, but a whole stage attempted work and produced nothing",
  interrupted: "stopped by Ctrl-C or a shutdown",
  abandoned: "closed by the stale-run reaper",
  budget_exhausted: "stopped on the call budget",
  refused: "a recorded call-budget refusal left no completed outcomes; the budget refused it",
  unaccounted: "recorded attempts and native outcomes do not balance; no cause is inferred",
  limited: "the retained record names work left unattempted; inspect the coverage cause",
  unreadable: "recorded stage outcomes could not be read",
  unknown_status: "the recorded outcome is not recognized",
  info: "a neutral note",
});

// Brief Overview labels; full meanings stay in each cell and the state disclosure.
const GRID_LABELS = Object.freeze({
  ok:"Succeeded", partial:"Ran with failures", failed:"Stage failed", error:"Run error",
  running:"Completion unknown", skipped:"Did not run", degraded:"No output",
  interrupted:"Interrupted", abandoned:"Abandoned", budget_exhausted:"Budget exhausted",
  refused:"Budget refused", unaccounted:"Accounting gap", limited:"Work omitted", info:"Note",
});

const EM_DASH = "—";

/* ---------------------------------------------------------------------------
   Text helpers. Everything that reaches the page goes through esc().
   --------------------------------------------------------------------------- */

/** Escape text for use in element content AND in a quoted attribute. */
export function esc(value) {
  if (value === null || value === undefined) return "";
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/** Integer with thousands separators. Deterministic; no locale involved. */
export function num(value) {
  if (value === null || value === undefined || Number.isNaN(value)) return "";
  const negative = value < 0;
  const digits = String(Math.abs(Math.round(value)));
  let out = "";
  for (let i = 0; i < digits.length; i += 1) {
    if (i > 0 && (digits.length - i) % 3 === 0) out += ",";
    out += digits[i];
  }
  return (negative ? "-" : "") + out;
}

/** One decimal place, for rates the data layer says are safe to show. */
export function dec1(value) {
  if (value === null || value === undefined || Number.isNaN(value)) return "";
  return (Math.round(value * 10) / 10).toFixed(1);
}

/** Byte count, binary-free: kB and MB are 1000-based so the number matches `wc -c`. */
export function bytes(value) {
  if (value === null || value === undefined) return "";
  if (value < 1000) return `${num(value)} B`;
  if (value < 1000000) return `${dec1(value / 1000)} kB`;
  return `${dec1(value / 1000000)} MB`;
}

/**
 * Format bold and code spans after escaping the complete input.
 * Input can contain transcript-derived text. Only tags introduced by this
 * helper may reach the result; markup from the input remains escaped.
 */
export function mdLite(value) {
  return esc(String(value === null || value === undefined ? "" : value))
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`]+)`/g, "<code>$1</code>");
}

/** Cut a long string, and say that it was cut. */
export function clamp(text, limit) {
  const value = String(text === null || text === undefined ? "" : text);
  if (value.length <= limit) return { text: value, cut: 0 };
  return { text: value.slice(0, limit), cut: value.length - limit };
}

/* ---------------------------------------------------------------------------
   Atoms that carry the correctness rules.
   --------------------------------------------------------------------------- */

/**
 * Render a not-computable payload from the data layer: {value, computable,
 * reason}. The em-dash is the data layer's own, never a locally invented zero.
 */
export function novalue(cell, fallbackReason) {
  // `.novalue` means "this value does not exist". Rendering a real measurement
  // in it, under a tooltip reading "not computable", would misreport the one
  // thing this product is most careful about. So the flag is READ, not merely
  // documented: before 2026-08-23 it was ignored, and any payload that became
  // computable would have shown its true number dressed as an absence.
  if (cell && typeof cell === "object" && cell.computable === true) {
    return `<span class="strong">${esc(cell.value)}</span>`;
  }
  // A bare scalar is not a payload, but discarding it loses a number the caller
  // clearly meant to show. Keep it and stay conservative about the treatment:
  // absent `computable` is not the same as `computable: true`.
  const isPayload = cell !== null && typeof cell === "object";
  const raw = isPayload ? cell.value : cell;
  const reason = (isPayload && cell.reason) || fallbackReason || "not computable";
  const value = raw === undefined || raw === null ? EM_DASH : raw;
  return `<span class="novalue" title="${esc(reason)}">${esc(value)}</span>`;
}

/**
 * Render a fraction payload: {numerator, denominator, rate, enough_data,
 * display}. When enough_data is false the data layer's `display` string is
 * printed verbatim and carries no percent sign; this function never divides.
 */
export function fractionText(fraction) {
  if (!fraction) return novalue(null, "no fraction was returned");
  if (!fraction.enough_data) {
    return `<span class="novalue" title="${esc(fraction.reason || "sample too small for a rate")}">${esc(fraction.display)}</span>`;
  }
  return `<span class="strong">${esc(fraction.display)}</span>`;
}

/** A badge. `state` and `verdict` are passed through so unmapped values alarm. */
export function badge(text, options) {
  const opts = options || {};
  const bits = [];
  if (opts.state !== undefined && opts.state !== null && opts.state !== "") {
    bits.push(`data-state="${esc(opts.state)}"`);
  }
  if (opts.verdict !== undefined && opts.verdict !== null && opts.verdict !== "") {
    bits.push(`data-verdict="${esc(opts.verdict)}"`);
  }
  const title = opts.title ? ` title="${esc(opts.title)}" aria-label="${esc(opts.title)}"` : "";
  const cls = opts.large ? "badge badge--lg" : "badge";
  return `<span class="${cls}" ${bits.join(" ")}${title}>${esc(text)}</span>`;
}

const RULE_PROPOSAL_STATES = {
  pending: ["Pending", "info", "Proposal built; no gate result yet"],
  gated_pass: ["Gate passed", null, "The gate passed; this does not authorize delivery"],
  gated_fail: ["Gate failed", null, "The gate failure threshold was met"],
  ungated: ["Ungated", null, "No usable gate evidence; manual review is required"],
  inconclusive: ["Inconclusive", null, "No passing or failing gate majority; held for review"],
  held: ["Held", "partial", "Delivery policy declined the write"],
  approved_user: ["Approved", "info", "Human approval recorded; approval alone does not establish completed delivery"],
  rejected_user: ["Rejected", "abandoned", "Human rejection recorded"],
  applied: ["Applied", "ok", "A completed write was recorded; current availability is separate"],
  rolled_back: ["Rolled back", "abandoned", "The recorded application was reversed"],
  superseded: ["Superseded", "abandoned", "This proposal is no longer current"],
};
const RULE_LEARNING_STATES = {
  candidate: ["Candidate", "info", "Learning retained; no proposal recorded"],
  proposed: ["Proposed", "info", "Learning marked proposed; this is not a gate or delivery result"],
  applied: ["Applied", "ok", "Learning marked applied; current availability is separate"],
  rejected: ["Rejected", "abandoned", "Learning marked rejected"],
  pruned: ["Pruned", "abandoned", "Learning marked pruned"],
  superseded: ["Superseded", "abandoned", "Learning replaced by another learning"],
};
export function renderRuleStatus(status, domain = "proposal") {
  const map = domain === "learning" ? RULE_LEARNING_STATES : RULE_PROPOSAL_STATES;
  const [label, state, meaning] = Object.hasOwn(map, status) ? map[status] :
    [`Unknown status: ${status ?? "not recorded"}`, "unknown", "No presentation meaning is defined for this recorded status"];
  return badge(label, {state, verdict:state === null ? status : null,
    title:`${label}: ${meaning}. Recorded ${domain} status: ${status ?? "not recorded"}.`})
    .replace('<span ', `<span data-rule-status="${esc(status ?? "")}" `);
}

/** Decorate retained patch text only. Hunk counts disambiguate header-like content. */
export function renderUnifiedDiff(diff) {
  let oldLeft = 0, newLeft = 0;
  return (String(diff ?? "").match(/[^\n]*(?:\n|$)/g) || []).filter(Boolean).map(line => {
    const hunk = line.match(/^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@/);
    let kind = "context";
    if (hunk) {
      oldLeft = Number(hunk[1] ?? 1); newLeft = Number(hunk[2] ?? 1); kind = "hunk";
    } else if (line.startsWith("diff --git ")) {
      oldLeft = newLeft = 0; kind = "header";
    } else if (line.startsWith("\\ No newline at end of file")) {
      kind = "note";
    } else if (!oldLeft && !newLeft && /^(---|\+\+\+)[ \t]/.test(line)) {
      kind = "header";
    } else if (line.startsWith("+")) {
      kind = "add"; newLeft = Math.max(0, newLeft - 1);
    } else if (line.startsWith("-")) {
      kind = "remove"; oldLeft = Math.max(0, oldLeft - 1);
    } else if (line.startsWith(" ")) {
      oldLeft = Math.max(0, oldLeft - 1); newLeft = Math.max(0, newLeft - 1);
    }
    // Character references retain CR bytes through the HTML parser. The span also
    // prevents PRE's special removal of an initial newline from retained text.
    return `<span class="diff-line" data-diff="${kind}">${esc(line).replaceAll("\r", "&#13;")}</span>`;
  }).join("");
}

/** A designed empty state: a title, a body, and the reason it is empty. */
export function emptyState(title, body, reason, options) {
  const opts = options || {};
  const state = opts.state ? ` data-state="${esc(opts.state)}"` : "";
  const inline = opts.inline ? " empty--inline" : "";
  return (
    `<div class="empty${inline}"${state}>` +
    `<span class="empty__glyph"></span>` +
    `<span class="empty__title">${esc(title)}</span>` +
    `<span class="empty__body">${esc(body)}</span>` +
    (reason ? `<span class="empty__reason">${esc(reason)}</span>` : "") +
    `</div>`
  );
}

export function field(label, valueHtml) {
  return (
    `<div class="field"><span class="field__label">${esc(label)}</span>` +
    `<span class="field__value">${valueHtml}</span></div>`
  );
}

export function stackField(label, valueHtml) {
  return (
    `<div class="field field--stack"><span class="field__label">${esc(label)}</span>` +
    `<span class="field__value">${valueHtml}</span></div>`
  );
}

export function section(title, bodyHtml) {
  return `<section class="section"><h3 class="section__title">${esc(title)}</h3>${bodyHtml}</section>`;
}

/** Plain-language meaning for a cell state, including the ones app.css cannot draw. */
export function stateTitle(state) {
  const meaning = STATE_MEANING[state];
  return `${state}: ${meaning || "the recorded outcome is not recognized; inspect the stage details"}`;
}

/* ---------------------------------------------------------------------------
   V1 — Overview
   --------------------------------------------------------------------------- */

/** "Data as of <day>, N days stale." Shown whenever the data is not today's. */
export function renderBanner(freshness) {
  if (!freshness) return { html: "", show: false };
  const stale = freshness.days_stale;
  if (stale === null || stale === undefined) {
    return {
      html:
        `<div><div class="banner__title">${esc(freshness.banner)}</div>` +
        `<div class="banner__body">No transcript incident can be dated, so incident arrivals have no dated window. Mining throughput uses its separately stated execution window.</div></div>`,
      show: true,
      state: "partial",
    };
  }
  if (stale === 0) {
    return {
      html:
        `<div><div class="banner__title">${esc(freshness.banner)}</div>` +
        `<div class="banner__body">Dates come from <span class="mono">incidents.ts</span>, the transcript timestamp, never from this machine's clock.</div></div>`,
      show: true,
      state: "ok",
    };
  }
  return {
    html:
      `<div><div class="banner__title">${esc(freshness.banner)}</div>` +
      `<div class="banner__body">Incident arrivals use the last dated transcript coverage, not today. Mining throughput has a separate execution window. ` +
      `Days beyond the observed data are not measured zeroes. ` +
      `Reference day ${esc(freshness.reference_day)} (UTC); newest incident ${esc(freshness.last_incident_ts || EM_DASH)}.</div></div>`,
    show: true,
    state: stale >= 2 ? "partial" : "info",
  };
}

export function renderStatusLine(statusLine) {
  if (!statusLine) return "";
  return esc(statusLine.text);
}

export function renderStatusSub(statusLine, freshness) {
  if (!statusLine) return "";
  const nights = (statusLine.nights_ran_days || []).map((d) => esc(d)).join(", ") || "none";
  return (
    `The 7-night window is anchored on the clock (${esc(statusLine.window_start)} to ${esc(statusLine.window_end)}, UTC), ` +
    `because "did it run last night?" is the one question the data cannot answer for itself. Nights with a run: ${nights}. ` +
    (freshness ? `Data itself ends ${esc(freshness.as_of || EM_DASH)}.` : "")
  );
}

export function overviewReview(data=state.overview) {
  return state.review || data?.audit?.review || data?.inbox || {};
}
function overviewDelivered(delivery) {
  return delivery?.manual_targets==null || delivery?.automatic_operations==null?null:delivery.manual_targets+delivery.automatic_operations;
}
export function renderTiles(data, review=overviewReview(data)) {
  const line=data.status_line || {},backlog=data.backlog || {},delivery=data.audit?.loop?.delivery;
  return [
    ["Nights with a run", `${num(line.nights_ran)} / ${num(line.nights_window)}`, "UTC days · schedule unknown", ""],
    ["Target deliveries",overviewDelivered(delivery)==null?'Unknown':num(overviewDelivered(delivery)),"Current use unverified",""],
    ["Waiting on you",num(review.count),review.family_count==null?"Proposals requiring your decision":`${num(review.count)} proposals · ${num(review.family_count)} lessons`,"partial"],
    ["Incidents queued",num(backlog.queued),"Arrivals and throughput below",""],
  ].map(([label,value,note,tone])=>`<div class="tile"${tone?' data-state="'+tone+'"':""}><div class="tile__label">${label}</div><div class="tile__value">${value}</div><div class="tile__sub">${esc(note)}</div></div>`).join("");
}

export function renderOverviewPolicy(policy) {
  if(!policy?.available)return `<a class="overview-policy" href="#/evals">Automatic policy unavailable</a>`;
  const enabled=Object.entries(policy.classes).filter(([,row])=>row.enabled).map(([key])=>({global:"global rules",project:"project rules",skill:"skills",hook:"hooks"}[key] || key));
  return `<a class="overview-policy" href="#/evals"><img src="/assets/evidence-dot.svg" width="8" height="8" alt="">Auto-apply: ${enabled.length?esc(enabled.join(", ")):"all classes off"}</a>`;
}
export function renderOverviewLoop(loop) {
  if(!loop)return '<p class="caption">Recorded populations unavailable.</p>';
  const sessions=loop.sessions || {},evaluation=loop.evaluations || {},delivery=loop.delivery || {};
  const value=x=>x==null?'Unknown':num(x);
  const delivered=overviewDelivered(delivery);
  const items=[
    ['Known session IDs',value(sessions.known),evidenceHref('','',{kinds:'session'})],
    ['Retained incidents',value(loop.incidents),evidenceHref('','',{kinds:'incident'})],
    ['Rules learned',value(loop.learnings),'#/rules'],
    ['Eval attempts · pass',`${value(evaluation.attempts)} · ${value(evaluation.passed)}`,'#/evals'],
    ['Deliveries · rollbacks',`${value(delivered)} · ${value(delivery.rollback_operations)}`,'#/review'],
    ['Comparable trend','Inspect by version →','#/evals'],
  ];
  return `<ol class="overview-loop__steps" aria-label="Recorded populations">${items.map(([label,count,href])=>`<li><a href="${esc(href)}"><span>${label}</span><strong>${count}</strong></a></li>`).join('')}</ol>`;
}
export function renderOverviewLoopCoverage(loop) {
  if(!loop)return '<p>Recorded populations unavailable.</p>';
  const sessions=loop.sessions || {},evaluation=loop.evaluations || {},delivery=loop.delivery || {};
  const value=x=>x==null?'unknown':num(x);
  return `<p class="caption">Independent retained totals, not one run or conversion rates. ${num(sessions.indexed_transcripts)} indexed transcripts contain ${value(sessions.known)} distinct provider-qualified session IDs; ${num(sessions.unknown_transcripts)} transcripts have unknown session identity. Incidents are retained findings, not signal occurrences. A rule can have several proposals.</p>`+
    `<p class="caption">Passing evaluations require complete comparable evidence. Attempts include repeated tests; ${num(evaluation.unlinked_results)} historical results are unlinked. Deliveries count ${value(delivery.manual_targets)} reviewed target writes and ${value(delivery.automatic_operations)} automatic operations. Rollbacks count completed inverse operations; a target may contain several rule contributions. Manual resolution deliveries and ${num(delivery.legacy_application_events)} unlinked application events are not a complete reversal count. Do not subtract these units or infer current availability or improvement.</p>`;
}
export function renderOverviewDeliveries(delivery) {
  if(delivery?.manual_targets==null)return '<p class="caption">Reviewed delivery history unavailable.</p><a href="#/review">Open Review history</a>';
  const rows=delivery.recent || [];
  if(!rows.length)return '<p class="caption">No completed reviewed target writes are retained. Approval alone is not delivery.</p><a href="#/review">Open Review history</a>';
  return rows.map(row=>`<article class="overview-delivery"><a class="overview-delivery__title" href="#/review/command/${encodeURIComponent(row.command_id)}">${esc(row.title)}${row.title_cut?'…':''}</a><p class="caption overview-delivery__target"><span>${esc(row.destination.target_path.split(/[\\/]/).pop())}</span><span title="${esc(row.destination.target_path)}">${esc(row.destination.target_path)}</span></p><p class="caption">${num(row.member_count)} proposal${row.member_count===1?'':'s'} · delivered ${esc(row.completed_at)} · <a href="#/review/command/${encodeURIComponent(row.command_id)}">Inspect delivery →</a></p></article>`).join('')+
    `<p class="caption overview-delivery__foot">Latest ${num(rows.length)} of ${num(delivery.manual_targets)} reviewed target writes. Titles are shortened; open the command for all reviewed members, exact snapshots and rollback review. Present use is unverified.</p>`;
}
export function renderOverviewQueue(review) {
  if(review.count==null)return '<p>Review membership has not been read.</p><a class="btn btn--primary" href="#/review">Open review queue</a>';
  const families=review.families || [],shown=families.slice(0,3),count=review.family_count ?? families.length;
  return `<p class="caption">${num(review.count)} items waiting on you · ${num(count)} lessons. Open Review to inspect and select complete members.</p>`+
    (review.auto_apply_pending?`<p class="caption">${num(review.auto_apply_pending)} passing proposals await automatic delivery.</p>`:"")+
    Object.entries(review.unknown_statuses || {}).map(([status,count])=>`<p>${badge("Unknown status: "+status,{state:"unknown_status"})} ${num(count)} proposals outside the known queue policy.</p>`).join("")+
    shown.map(f=>{const title=String(f.title || f.learning_id || "");const size=f.size ?? f.proposals?.length;return `<article class="overview-family"><a href="#/review/proposal/${encodeURIComponent(f.proposal_id || f.proposals?.[0]?.id || "")}">${esc(title.slice(0,120))}${f.title_cut || title.length>120?"…":""}</a><p class="caption">${num(size)} proposal${size===1?"":"s"}</p></article>`;}).join("")+
    (count>shown.length?`<p class="caption">${num(count-shown.length)} more lessons in Review.</p>`:"")+
    (!review.count?'<p>Nothing needs a decision. Delivery and paid jobs have their own recorded progress.</p>':"")+
    `<a class="btn btn--primary" href="#/review">Open review queue</a>`;
}
export function renderOverviewRunSummary(latest) {
  if(!latest)return '<p>No retained run. A blank history is not a successful run.</p>';
  const run=latest.run;
  return `<p class="caption">Latest recorded run · <a href="${esc(runHref("run",run.id))}">${esc(run.started)} · ${esc(run.id)}</a> · ${esc(run.status)}${latest.review_only===true?" · review-only run":""}${run.finished?"":" · completion not recorded"}</p>`+
    `<dl class="overview-latest">`+latest.stages.map(stage=>{
      const payload=stage.payload || {},cell=stage.cell || {};
      const counters=Object.entries(payload).filter(([,v])=>typeof v==="number").slice(0,4).map(([k,v])=>`${k.replaceAll("_"," ")}: ${num(v)}`).join(" · ");
      return `<dt><a href="${esc(runHref("run",run.id,stage.name))}">${esc(stage.name)}</a></dt><dd>${stage.recorded?esc(counters || "No numeric counters retained"):"Not recorded"}${stage.recorded && cell.state!=="ok"?" · "+`<span class="overview-state" title="${esc(stateTitle(cell.state))}">${esc(GRID_LABELS[cell.state] || stateTitle(cell.state))}</span>`:""}</dd>`;
    }).join("")+`</dl><p class="caption">Open a stage for complete evidence.</p>`;
}
function recentFailureClasses(failures) {
  return [...(failures?.regressed || []),...(failures?.open || [])].filter(row=>(row.recent || 0)>0);
}
export function renderOverviewFailures(failures) {
  if(!failures)return "";
  const active=recentFailureClasses(failures),shown=active.slice(0,3);
  return `<p class="caption">Counts in the seven calendar days ending ${esc(failures.latest_run_day || EM_DASH)}; source entries may overlap.</p>`+(shown.length?shown.map(row=>`<article class="overview-failure"><div><strong>${esc(row.name)}</strong> ${infotip(row.class+": "+row.explanation)}<p class="caption">${esc(row.explanation || "No explanation retained")}</p>${row.last_seen?`<a class="caption" href="${esc(runHref("run",row.last_seen.run_id))}">Last recorded run · ${esc(row.last_seen.started)}</a>`:""}</div><span class="meter" aria-hidden="true"><span class="meter__fill" style="--pct: ${100*(row.recent ?? 0)/Math.max(...active.map(x=>x.recent ?? 0),1)}"></span></span><strong class="num">${num(row.recent ?? 0)}</strong></article>`).join(""):'<p>No recent failure class is retained. Inspect history for quiet, fixed and unlinked call outcomes.</p>')+
    (active.length>shown.length?`<p class="caption">${num(active.length-shown.length)} more active failure classes in full history.</p>`:"")+
    `<details id="ov-failure-history" class="overview-detail"><summary>Full failure history and counting definitions</summary>${renderFailures(failures)}</details>`;
}

export function infotip(text) {
  return (
    `<span class="infotip"><button class="infotip__btn" type="button" aria-label="${esc(text)}">?</button>` +
    `<span class="infotip__pop">${esc(text)}</span></span>`
  );
}

/**
 * Aggregate the run grid by night. Each column shows the worst recorded
 * outcome, the number of runs, and markers for hidden run-level states.
 */
/**
 * Run statuses a cell can carry for a run that recorded nothing for the stage.
 * A run with status `ok` that recorded nothing is `skipped`, which is absence
 * and needs no mark; the following states retain an outcome.
 */
const RUN_ONLY_STATES = Object.freeze([
  "running",
  "degraded",
  "interrupted",
  "abandoned",
  "error",
  "budget_exhausted",
  "unknown_status",
]);

/**
 * Count run-level states hidden by a cell's displayed outcome.
 * A night can include both completed and unfinished runs. The marker and
 * tooltip retain the hidden states without replacing recorded stage results.
 */
export function hiddenRunStates(cell) {
  const counts = (cell && cell.states) || {};
  const states = RUN_ONLY_STATES.filter(
    (state) => state !== cell.state && (counts[state] || 0) > 0
  );
  let total = 0;
  states.forEach((state) => {
    total += counts[state];
  });
  return { states, total };
}

export function renderGrid(grid, options) {
  const opts = options || {};
  if (!grid || !grid.columns || grid.columns.length === 0) {
    return emptyState(
      "No runs to show",
      "The runs table is empty for this window, so there is no night to draw.",
      "This is absence of a run, not a run that failed."
    );
  }
  const stages = grid.stages || [];
  const head = grid.columns
    .map((column) => {
      const night = String(column.night || "");
      const runs = column.run_count ? num(column.run_count) : "";
      const label = `${night}: ${column.run_count} run(s), worst outcome ${column.worst}`;
      return (
        `<th class="run-grid__night" scope="col" title="${esc(label)}">` +
        `<span class="run-grid__mon">${esc(night.slice(5, 7))}</span>` +
        `<span class="run-grid__day">${esc(night.slice(8, 10))}</span>` +
        `<span class="run-grid__runs">${esc(runs)}</span></th>`
      );
    })
    .join("");

  const body = stages
    .map((stage) => {
      const cells = grid.columns
        .map((column) => {
          const cell = (column.cells || {})[stage] || { state: "skipped", runs: 0 };
          const state = cell.state;
          const parts = [`${stage} on ${column.night}`, stateTitle(state)];
          if (cell.attempted !== null && cell.attempted !== undefined) {
            parts.push(`${num(cell.succeeded)} completed outcomes / ${num(cell.attempted)} ${cell.unit || 'recorded attempts'}, ${num(cell.failed)} in the recorded failed bucket`);
            const refused = (cell.per_run || []).reduce((sum, run) => sum + (run.refused || 0), 0);
            if (refused) parts.push(`${num(refused)} refused by the call budget`);
            const nestedRefused = (cell.per_run || []).reduce((sum, run) => sum + (run.refused_in_failed || 0), 0);
            if (nestedRefused) parts.push(`${num(nestedRefused)} of the failed-bucket entries were budget refusals`);
          // The displayed number carries no unit on screen, and the same "0"
          // means "0 verdicts" under gate and "0 edits applied" under apply.
          // The payload has always supplied number_label; it was discarded.
          if (cell.number_label) parts.push(`the figure shown is ${cell.number_label}`);
          }
          if (cell.unreadable_runs) parts.push(`Counts cover ${num(cell.accounted_runs)} run(s); ${num(cell.unreadable_runs)} stage record(s) have incomplete accounting`);
          (cell.per_run || []).filter(run=>run.state==='unaccounted').forEach(run=>parts.push(`${run.run_id}: ${run.unaccounted>0?`${num(run.unaccounted)} attempts lack outcomes`:`outcomes exceed attempts by ${num(-run.unaccounted)}`}`));
          if (cell.runs > 1) parts.push(`worst of ${cell.runs} runs that night`);
          if (cell.runs_without_record) {
            parts.push(`${cell.runs_without_record} run(s) recorded nothing for this stage`);
          }
          const hidden = hiddenRunStates(cell);
          if (hidden.total) {
            parts.push(
              `${num(hidden.total)} of those carry run status ${hidden.states.join(", ")}, ` +
                `which this cell's colour does not show: the colour is the worst RECORDED outcome`
            );
          }
          const title = parts.join(" · ");
          const shown = cell.number === null || cell.number === undefined ? "" : num(cell.number);
          const mark = hidden.total ? ` data-hidden-runs="${esc(String(hidden.total))}"` : "";
          const runIds = column.run_ids || [];
          const href = runIds.length === 1 ? runHref("run", runIds[0], stage) : runHref("night", column.night, stage);
          return (
            `<td class="cell" data-state="${esc(state)}"${mark} title="${esc(title)}" aria-label="${esc(title)}">` +
            `<a class="run-grid__link" href="${esc(href)}" aria-label="${esc(title + '; open run detail')}"><span class="cell__n">${esc(shown)}</span></a></td>`
          );
        })
        .join("");
      return `<tr><th class="run-grid__stage" scope="row">${esc(stage)}</th>${cells}</tr>`;
    })
    .join("");

  const cls = opts.numbers ? "run-grid run-grid--numbers" : "run-grid";
  return (
    `<table class="${cls}">` +
    `<caption class="visually-hidden">Pipeline stages by night. Each cell is the night's worst outcome for that stage.</caption>` +
    `<thead><tr><th class="run-grid__corner" scope="col">Stage</th>${head}</tr></thead>` +
    `<tbody>${body}</tbody></table>`
  );
}

/** A legend of exactly the states this grid actually contains. */
export function renderGridLegend(grid, {compact = false} = {}) {
  if (!grid || !grid.columns) return "";
  const present = new Set();
  grid.columns.forEach((column) => {
    Object.keys(column.cells || {}).forEach((stage) => {
      const cell = column.cells[stage];
      Object.keys(cell.states || {}).forEach((state) => present.add(state));
      if (cell.state) present.add(cell.state);
    });
  });
  const order = grid.cell_states && grid.cell_states.length ? grid.cell_states : DRAWN_STATES;
  const ordered = order.filter((state) => present.has(state));
  Array.from(present)
    .sort()
    .forEach((state) => {
      if (ordered.indexOf(state) === -1) ordered.push(state);
    });
  if (ordered.length === 0) return "";
  return ordered
    .map((state) => {
      const drawn = DRAWN_STATES.indexOf(state) !== -1;
      const label = drawn ? (compact ? GRID_LABELS[state] : STATE_MEANING[state]) || state
        : compact ? `Outcome unknown (${state})` : stateTitle(state);
      return (
        `<li class="legend__item"><span class="swatch" data-state="${esc(state)}" ` +
        `title="${esc(stateTitle(state))}" aria-label="${esc(stateTitle(state))}"></span>${esc(label)}</li>`
      );
    })
    .join("");
}

/** The two caveats a column of many runs must state, plus anything unreadable. */
export function renderGridFoot(grid) {
  if (!grid) return "";
  const columns = grid.columns || [];
  const multi = columns.filter((c) => (c.run_count || 0) > 1).length;
  let stuck = 0;
  let masked = 0;
  const stages = grid.stages || [];
  columns.forEach((column) => {
    stuck += (column.run_states || {}).running || 0;
    stages.forEach((stage) => {
      const cell = (column.cells || {})[stage];
      if (cell && hiddenRunStates(cell).total) masked += 1;
    });
  });
  const notes = [
    `A column shows the night's worst outcome across every run that night.`,
    `${num(grid.runs_total)} run(s) over ${num(grid.nights_with_runs)} night(s) with a run.`,
  ];
  if (multi) notes.push(`${num(multi)} night(s) hold more than one run.`);
  if (stuck) {
    // Say what the grid actually does. A cell's colour is the worst RECORDED
    // outcome, so on a night that also holds finished runs a stuck run has no
    // colour of its own: it is marked on the cell, named in the cell's tooltip
    // and counted here. Claiming it "renders as its own state" would be false
    // on exactly the night that matters.
    notes.push(
      `${num(stuck)} run(s) never reported completion. They are never counted as success; ` +
        `where a night also holds finished runs, the cell keeps the worst RECORDED outcome ` +
        `and carries a corner mark instead` +
        (masked ? `, on ${num(masked)} cell(s)` : "") +
        `.`
    );
  }
  if (grid.window && grid.window.applied) {
    notes.push(
      `Window: the last ${num(grid.window.window_days)} nights ending ${esc(grid.window.ends_at)}; ` +
        `${num(grid.window.nights_dropped)} older night(s) and ${num(grid.window.runs_dropped)} run(s) are not drawn.`
    );
  }
  if (grid.unreadable && grid.unreadable.length) {
    notes.push(
      `${num(grid.unreadable.length)} stage record(s) could not be read. Their outcomes remain unknown, never counted as success. Open a stage to inspect its retained record.`
    );
  }
  const unknown = Object.keys(grid.unknown_run_statuses || {});
  if (unknown.length) {
    notes.push(`Unrecognized recorded run status(es): ${unknown.map((s) => esc(s)).join(", ")}.`);
  }
  return notes.join(" ");
}

/**
 * Backlog as two competing rates. Never a countdown: the queue grows, so any
 * "N nights to drain" number would be fiction.
 */
export function renderBacklog(backlog, mining) {
  if(!backlog)return "";
  const arrivals=backlog.arrivals || {},measured=mining?.per_day;
  return `<div class="rates">`+rate(num(backlog.queued),"incidents queued")+
    rate(arrivals.per_day==null?EM_DASH:dec1(arrivals.per_day),"incident arrivals / day")+
    rate(measured==null?EM_DASH:dec1(measured),"recorded mining successes / day")+`</div>`+
    `<p class="caption">Configured mining-model limit: ${num(backlog.mine_capacity_per_run)} calls per run. A call slot is not a successfully mined incident.</p>`+
    (mining?`<p>${num(mining.succeeded)} successful mining outcomes / ${num(mining.recorded_runs)} runs with mining counters over ${num(mining.window_days)} full UTC days (${esc(mining.window_start)} to ${esc(mining.window_end_exclusive)}, end excluded).</p><p class="caption">${num(mining.missing_stage_run_ids.length)} runs without mining counters · ${num(mining.unfinished_run_ids.length)} without completion · ${num(mining.days_without_runs)} days without a run record. ${measured==null?"No measured mining throughput.":"Observed throughput is not future capacity."}</p>`:'<p class="caption">Recorded mining throughput is unavailable.</p>');
}

function rate(valueHtml, label) {
  return `<div class="rate"><span class="rate__value">${valueHtml}</span><span class="rate__label">${esc(label)}</span></div>`;
}

export function renderBacklogFoot(backlog) {
  if (!backlog) return "";
  const arrivals = backlog.arrivals || {};
  const parts = [
    esc(backlog.race),
    `Arrivals read <span class="mono">${esc(arrivals.source_column || "incidents.ts")}</span> over ` +
      `${num(arrivals.rate_days)} rate days within the ${num(arrivals.window_days)}-day display window ending ${esc(arrivals.window_end || EM_DASH)}.`,
    arrivals.excluded_partial_day ? `Newest partial day ${esc(arrivals.excluded_partial_day.day)} is excluded from the rate (${num(arrivals.excluded_partial_day.incidents)} incidents).` : "No partial day is excluded.",
    esc(arrivals.why_not_created_at || ""),
    `Queue order is <span class="mono">${esc(backlog.mine_order)}</span>, not first-in-first-out.`,
  ];
  if (arrivals.days_with_zero) {
    parts.push(`${num(arrivals.days_with_zero)} day(s) in the window recorded no incident.`);
  }
  return parts.filter(Boolean).join(" ");
}

export function renderFailures(failures) {
  if (!failures) return "";
  const groups = [
    { key: "open", label: "" },
    { key: "regressed", label: "regressed" },
    { key: "quiet", label: "quiet" },
    { key: "fixed", label: "fixed" },
  ];
  const items = [];
  groups.forEach((group) => {
    (failures[group.key] || []).forEach((entry) => {
      const fixedClass = entry.status === "fixed" ? " failure--fixed" : "";
      const marks = [];
      if (entry.status === "fixed") marks.push(badge(`source fix dated ${String(entry.fixed_at || "").slice(0, 10)}`, { state: "ok" }));
      if (entry.status === "regressed") marks.push(badge("recorded after the source fix", { state: "failed" }));
      if (entry.status === "quiet") {
        marks.push(badge(`not seen in this ${num(entry.recent_window_days)}-day window`, { state: "info" }));
      }
      if (!entry.has_copy) marks.push(badge("no plain-language name yet", { state: "partial" }));
      const note = [entry.explanation, entry.stages && entry.stages.length ? `Seen in: ${entry.stages.join(", ")}.` : ""]
        .filter(Boolean)
        .join(" ");
      items.push(
        `<li class="failure${fixedClass}">` +
          `<span class="failure__name">${esc(entry.name)} ${marks.join(" ")} ` +
          infotip(`${entry.class}: ${entry.explanation || "no explanation recorded"}`) +
          `</span>` +
          `<span class="failure__count">${num(entry.total)}${entry.recent ? ` <span class="caption">(${num(entry.recent)} recent)</span>` : ""}</span>` +
          (note ? `<span class="failure__note">${esc(note)}</span>` : "") +
          (entry.fix_commit ? `<details class="failure__note" ${reviewDisclosure("failure-fix:"+entry.class)}><summary>Recorded source fix</summary><p>${esc(entry.fix)}</p><p>Commit <code>${esc(entry.fix_commit)}</code> · ${esc(entry.fixed_at)}</p><p>The retained records do not establish deployment of this revision. A later occurrence of the same constraint does not prove the identical defect returned.</p>${(entry.occurrences_after_fix||[]).map(row=>`<p><a href="${esc(runHref("run",row.run_id))}">${esc(row.run_id)}</a> · ${esc(row.started)} · ${num(row.count)} recorded entries</p>`).join("")}</details>` : "") +
          `</li>`
      );
    });
  });

  const empty = items.length ? "" : emptyState(
      "No failure entries retained",
      `${num(failures.retained_runs)} retained runs and ${num(failures.retained_calls)} calls.`,
      "Missing history or taxonomy coverage cannot establish a healthy run.",
      { state: "partial" }
    );

  const notFailures = (failures.not_failures || [])
    .map((entry) => `<li class="failure"><span class="failure__name muted">${esc(entry.class)}</span>` +
      `<span class="failure__count muted">${num(entry.count)}</span>` +
      `<span class="failure__note">${esc(entry.why)}</span></li>`)
    .join("");

  return (
    (items.length ? `<ul class="failures">${items.join("")}</ul>` : empty) +
    `<p class="footnote" style="margin-top:var(--s-5)">${esc(failures.note)}</p>` +
    (notFailures
      ? `<h3 class="section__title" style="margin-top:var(--s-6)">Counted, but not failures</h3><ul class="failures">${notFailures}</ul>`
      : "")
  );
}

/**
 * Gate health: counts and a sentence, never a pass rate.
 *
 * All four verdicts are shown even at zero, so the vocabulary is visible. A
 * verdict the data carries that is not one of the four is shown with its raw
 * name and the design system's unmapped treatment.
 */
export function renderGate(gate) {
  if (!gate) return "";
  const byVerdict = gate.by_verdict || {};
  const known = GATE_VERDICTS.map((verdict) => {
    const held = (gate.verdicts_held || []).indexOf(verdict) !== -1;
    const title = `${VERDICT_MEANING[verdict]} (${held ? "requires review" : "eligible only when execution policy permits"})`;
    return (
      `<div class="field"><span class="field__label">${badge(verdict, { verdict, title })}</span>` +
      `<span class="field__value"><span class="strong">${num(byVerdict[verdict] || 0)}</span> ` +
      `<span class="caption muted">${esc(VERDICT_MEANING[verdict])}</span></span></div>`
    );
  }).join("");

  const unknown = Object.keys(byVerdict)
    .filter((verdict) => GATE_VERDICTS.indexOf(verdict) === -1)
    .sort()
    .map((verdict) => {
      const title = `Unknown verdict: this dashboard knows four verdicts and ${verdict} is not one of them`;
      return (
        `<div class="field"><span class="field__label">${badge(verdict, { verdict, title })}</span>` +
        `<span class="field__value"><span class="strong">${num(byVerdict[verdict])}</span> ` +
        `<span class="caption">unknown verdict &mdash; shown rather than dropped</span></span></div>`
      );
    })
    .join("");

  const classes = Object.keys(gate.by_class || {})
    .sort()
    .map((klass) => {
      const row = (gate.rows || []).find((r) => r.class === klass);
      const label = row ? row.label : klass;
      const why = row ? row.why : "";
      return `<li class="failure"><span class="failure__name">${esc(label)}</span>` +
        `<span class="failure__count">${num(gate.by_class[klass])}</span>` +
        (why ? `<span class="failure__note">${esc(why)}</span></li>` : "</li>");
    })
    .join("");

  const tone = gate.gate_running ? "info" : "partial";
  return (
    `<div class="banner" data-state="${esc(tone)}"><div>` +
    `<div class="banner__title">${esc(gate.sentence)}</div>` +
    `<div class="banner__body">${esc((gate.rate_suppressed || {}).reason || "")}</div></div></div>` +
    section("Verdicts", known + unknown) +
    section(
      "What the verdict was actually about",
      classes
        ? `<ul class="failures">${classes}</ul>` +
            `<p class="footnote">A verdict is only about the rule if the harness got out of the way. ` +
            `"The harness broke" and "the eval could not reproduce the mistake" are separated here on purpose.</p>`
        : emptyState(
            "No eval has been classified",
            "eval_results is empty, so there is nothing to classify.",
            "This is an absent gate, not a failing one.",
            { inline: true }
          )
    ) +
    section(
      "Whose evals these are",
      field("Belong to a proposal", `<span class="strong">${num(gate.proposal_evals)}</span>`) +
        field("Seed scenarios", `<span class="strong">${num(gate.seed_evals)}</span>`) +
        field("Named a learning", `<span class="strong">${num(gate.learning_subject_evals)}</span>`) +
        field("Unknown subject", `<span class="strong">${num(gate.unknown_subject_evals)}</span>`) +
        `<p class="footnote">eval_results.subject_id is not always a learning id, so counting the table would ` +
        `report ${num(gate.eval_rows_total)} evals where ${num(gate.proposal_evals)} belong to a proposal.</p>`
    )
  );
}

export function renderInbox(inbox) {
  if (!inbox) return "";
  const queueing = inbox.queueing_statuses || [];
  const autoApply = inbox.auto_apply_statuses || [];
  const counts = inbox.by_status || {};
  const rows = Object.keys(counts)
    .sort()
    .map((status) => {
      const isQueue = queueing.indexOf(status) !== -1;
      const isAuto = autoApply.indexOf(status) !== -1;
      const unknown = Object.prototype.hasOwnProperty.call(inbox.unknown_statuses || {}, status);
      const tone = unknown ? "unknown_status" : isQueue ? "partial" : isAuto ? "info" : "ok";
      const meaning = unknown
        ? "a proposal status this dashboard does not know"
        : isQueue
        ? "queues for a human"
        : isAuto
        ? "passing gate; delivery depends on target class and run policy"
        : status === "approved_user"
        ? "approval recorded; a retained command determines delivery, otherwise fresh review may be needed"
        : "already decided";
      const marker = unknown
        ? badge("unknown status", { state: "unknown_status", title: meaning })
        : "";
      return (
        `<div class="field"><span class="field__label">${badge(status, { state: tone, title: meaning })}</span>` +
        `<span class="field__value"><span class="strong">${num(counts[status])}</span> ` +
        `<span class="caption muted">${esc(meaning)}</span> ${marker}</span></div>`
      );
    })
    .join("");

  const reviewOnly =
    inbox.review_only === null || inbox.review_only === undefined
      ? novalue(null, inbox.review_only_source)
      : esc(String(inbox.review_only));

  return (
    `<p class="statusline">${esc(inbox.line)}</p>` +
    (inbox.second_line ? `<p class="statusline__sub">${esc(inbox.second_line)}</p>` : "") +
    section("By status", rows || emptyState("No proposals", "The proposals table is empty.", "", { inline: true })) +
    section(
      "Why the count is not the row total",
      field("--review-only", `${reviewOnly} <span class="caption muted">${esc(inbox.review_only_source)}</span>`) +
        `<p class="footnote">Ungated proposals require review. Passing proposals also require review when their ` +
        `class is off, they predate class consent, or their run explicitly required review. Automatic delivery ` +
        `is counted separately from proposals requiring your decision.</p>` +
        `<p class="footnote">Open Review to inspect each proposal and record a decision.</p>`
    )
  );
}

export function renderConfidence(confidence) {
  if (!confidence) return "";
  return (
    `<div class="rates">` +
    rate(num(confidence.applied), "applied") +
    `<span class="rate__sep">&middot;</span>` +
    rate(num(confidence.rolled_back), "rolled back") +
    `</div>` +
    `<p class="statusline__sub">${fractionText(confidence.survival)}</p>` +
    `<p class="footnote">Where the sample is too small to carry a rate, the raw fraction is shown and says so. ` +
    `A percentage over n=0 would be a claim about the world that the data does not make.</p>`
  );
}

/* ---------------------------------------------------------------------------
   V2 — Rules. Search is the entry point.
   --------------------------------------------------------------------------- */

/** Everything about a rule that the search box can match, lowercased. */
export function ruleHaystack(row) {
  const parts = [
    row.id,
    row.title,
    row.rule_text,
    row.why,
    row.category,
    row.scope,
    row.status,
    row.incident_summary,
    row.primary_project_path,
    row.gate_verdict,
    (row.projects || []).join(" "),
    (row.path_globs || []).join(" "),
    (row.targets || []).map((t) => `${t.target_path} ${t.target_kind} ${t.action} ${t.proposal_status}`).join(" "),
  ];
  const provenance = row.provenance || {};
  parts.push((provenance.repos || []).join(" "));
  parts.push(provenance.agent_product);
  if (row.miner_generation && row.miner_generation.computable) parts.push(row.miner_generation.value,row.miner_generation.run_id,row.miner_generation.model_reported);
  parts.push((provenance.agents_in_evidence || []).join(" "));
  parts.push((provenance.session_ids || []).join(" "));
  (provenance.incidents || []).forEach((incident) => {
    parts.push(incident.display_text);
    parts.push(incident.signal_type);
    parts.push(incident.fingerprint);
  });
  return parts
    .filter((p) => p !== null && p !== undefined && p !== "")
    .join(" \n ")
    .toLowerCase();
}

/**
 * Rank rules against a query. Every whitespace-separated token must appear
 * somewhere in the row (AND), which makes narrowing predictable. Exact
 * substring only; this does not find "backoff" from "retry".
 */
export function matchRules(rows, query) {
  const all = rows || [];
  const tokens = String(query || "")
    .toLowerCase()
    .split(/\s+/)
    .filter(Boolean);
  if (tokens.length === 0) {
    const sorted = all.slice().sort(byEvidenceThenId);
    return { rows: sorted, tokens, searched: false };
  }
  const scored = [];
  all.forEach((row) => {
    const hay = ruleHaystack(row);
    const strong = `${row.title || ""} ${row.rule_text || ""}`.toLowerCase();
    const why = String(row.why || "").toLowerCase();
    let score = 0;
    let everyToken = true;
    tokens.forEach((token) => {
      if (hay.indexOf(token) === -1) {
        everyToken = false;
        return;
      }
      score += 1;
      if (strong.indexOf(token) !== -1) score += 3;
      if (why.indexOf(token) !== -1) score += 2;
    });
    if (everyToken) scored.push({ row, score });
  });
  scored.sort((a, b) => b.score - a.score || byEvidenceThenId(a.row, b.row));
  return { rows: scored.map((s) => s.row), tokens, searched: true };
}

function byEvidenceThenId(a, b) {
  const ea = a.evidence_count || 0;
  const eb = b.evidence_count || 0;
  if (ea !== eb) return eb - ea;
  return String(a.id).localeCompare(String(b.id));
}

/**
 * Use the learning's title when present, otherwise the first rule sentence.
 * The identifier remains available separately for record lookup.
 */
export function ruleHeading(row) {
  if (row.title) return row.title;
  const text = String(row.rule_text || "").replace(/\s+/g, " ").trim();
  if (!text) return row.id;
  const stop = text.indexOf(". ");
  const head = stop > 20 ? text.slice(0, stop + 1) : text;
  const cut = clamp(head, 90);
  return cut.cut ? `${cut.text}…` : cut.text;
}

/**
 * The second line of a rule row. When the heading already IS the rule text
 * (because the learning has no title), repeating it wastes the line, so the
 * row shows the reason instead. Both are searchable either way.
 */
export function ruleSnippet(row) {
  if (row.title) return { prefix: "", ...clamp(String(row.rule_text || "").replace(/\s+/g, " ").trim(), 180) };
  return { prefix: "Why: ", ...clamp(String(row.why || "").replace(/\s+/g, " ").trim(), 180) };
}

export function renderRuleRows(rows, selectedId) {
  return (rows || [])
    .map((row) => {
      const selected = row.id === selectedId ? ' aria-current="true"' : "";
      const text = ruleSnippet(row);
      const gate = row.gate_verdict
        ? badge(row.gate_verdict, {
            verdict: row.gate_verdict,
            title: VERDICT_MEANING[row.gate_verdict] || `verdict ${row.gate_verdict} is not one of the four this dashboard knows`,
          })
        : novalue(null, "no proposal for this rule carries an eval_result_id");
      const enforcement = (row.enforcement_gap || {}).flagged
        ? ` ${badge("Reported violation", { state: "partial", title: (row.enforcement_gap || {}).label })}`
        : "";
      return (
        `<tr data-rule-row="${esc(row.id)}" data-selected="${row.id===selectedId}">` +
        `<td>${badge(row.status, { state: row.status === "proposed" ? "info" : "" })}</td>` +
        `<td><a class="strong rule-open" data-rule-id="${esc(row.id)}" href="${esc(rulesHref(row.id))}"${selected}>${esc(ruleHeading(row))}</a>${enforcement} ` +
        `<span class="id">${esc(String(row.id).slice(0, 8))}</span>` +
        `<div class="caption muted">${esc(text.prefix)}${esc(text.text)}` +
        `${text.cut ? esc(`… (${text.cut} more characters)`) : ""}</div>${gate}<div class="caption">${num(row.evidence_linked)} / ${num(row.evidence_count)} evidence · ${num(row.project_count)} projects</div></td>` +
        `<td class="mono">${esc(row.target_summary)}</td>` +
        `<td>${novalue(row.miner_generation)}</td>` +
        `</tr>`
      );
    })
    .join("");
}

export function renderRulesEmpty(query, total) {
  if (total === 0) {
    return emptyState(
      "No rules yet",
      "The miner has not produced a learning, so there is nothing to believe.",
      "This is an empty learnings table, not a failed query."
    );
  }
  return emptyState(
    "Nothing matches that",
    `None of the ${total} rules contains every word of "${query}".`,
    "Search is exact substring matching over the rule, its reason, its targets, its repos and its evidence text. " +
      "It does not do synonyms: try one distinctive word from the error instead of a sentence."
  );
}

export function renderGrouping(grouping) {
  if (!grouping) return "";
  if (!grouping.available) {
    return emptyState(
      grouping.empty_state || "Family grouping is off",
      `Every rule is its own row. ${grouping.learnings_total} rule(s), ${grouping.learning_embeddings} learning embedding(s).`,
      grouping.reason
    );
  }
  const rows = (grouping.groups || [])
    .map(
      (group) =>
        `<div class="field"><span class="field__label mono">${esc(group.family_key)}</span>` +
        `<span class="field__value">${num(group.size)} rules: ${group.learning_ids.map((id) => `<span class="id">${esc(id)}</span>`).join(", ")}</span></div>`
    )
    .join("");
  return (
    rows +
    `<p class="footnote">Source: <span class="mono">${esc(grouping.source || "learnings.duplicate_of")}</span>. ` +
    `${num(grouping.learnings_with_duplicate_of)} of ${num(grouping.learnings_total)} rules carry a family.</p>`
  );
}

const RULE_TABS = Object.freeze([
  { id: "why", label: "Why" },
  { id: "provenance", label: "Provenance" },
  { id: "evidence", label: "Evidence" },
]);

export function renderTabs(tabs, active, href = id => `#/projects/${encodeURIComponent(state.selectedProject)}?tab=${encodeURIComponent(id)}`) {
  return `<nav class="tabs" aria-label="Inspector sections">` + tabs.map(tab =>
    `<a class="tab" data-tab="${esc(tab.id)}" href="${esc(href(tab.id))}"${tab.id === active ? ' aria-current="page"' : ""}>${esc(tab.label)}</a>`
  ).join("") + `</nav>`;
}

/** The rule inspector. Progressive disclosure: row, then why, then the raw chain. */
/**
 * D1: the Rules browser answers "what is true?" and never mutates. When a rule
 * has proposals waiting, it says so and links into V3 with the reason — one
 * doorway to the decision, not two.
 *
 * The count is derived from the queue already in state, not from a new field on
 * the rules payload, so the two views cannot disagree about what is waiting.
 */
export function renderRuleReviewLink(row) {
  const data = state.review;
  if (!data || !data.families) return "";
  const family = data.families.find((f) => f.learning_id === row.id);
  if (!family) {
    return `<p class="footnote">Nothing from this rule is waiting on you.</p>`;
  }
  const what = family.size === 1 ? "1 decision" : `${family.size} decisions`;
  return (
    `<p class="field__value"><a class="review-link" href="#/review">` +
    `${esc(what)} from this rule need review &rarr;</a></p>` +
    `<p class="footnote">${esc(family.lead_reason)}</p>`
  );
}

function projectReference(project, prefix = "") {
  if (typeof prefix !== "string") prefix = "";
  if (!project?.key) return "Project unknown";
  return `<a ${prefix ? `id="${esc(prefix)}"` : ""} href="#/projects/${encodeURIComponent(project.key)}">${esc(project.label)}</a>`+
    (project.label_status === "unavailable" ? ` <span class="caption">(repository name not retained)</span>` : "")+
    (project.aliases?.length>1 ? `<details ${prefix ? reviewDisclosure(prefix + ":aliases") : ""}><summary ${prefix ? `id="${esc(prefix + ":aliases")}"` : ""}>${num(project.aliases.length)} retained names for this repository</summary>${project.aliases.map(esc).join(" · ")}</details>` : "");
}
function sessionReference(session, prefix = "") {
  if (typeof prefix !== "string") prefix = "";
  const label=`${session.provider_label || "Agent unknown"} · ${session.native_session_id || "Native ID unknown"}`;
  return `<a ${prefix ? `id="${esc(prefix)}"` : ""} href="${esc(evidenceHref("session",session.key,{tab:"source"}))}">${esc(label)}</a>`+
    (session.identity_kind!=="native_session" ? ` <span class="caption">(transcript reference; native identity incomplete)</span>` : "");
}
export function renderSourceIdentity(identity, {prefix = ""} = {}) {
  if (!identity) return "";
  const projects=identity.projects || (identity.project ? [identity.project] : []);
  return `<div data-source-identity="true"><p>Repository: ${projects.length ? projects.map((project,i)=>projectReference(project,prefix ? prefix+":project:"+i : "")).join(" · ") : "Project unknown"}</p>`+
    (identity.session ? `<p>Source: ${sessionReference(identity.session,prefix ? prefix+":session" : "")}</p>` : "")+`</div>`;
}
function ruleSessions(provenance) {
  if (!provenance.sessions) return (provenance.session_ids || []).map(esc).join(" · ") || "No retained session identity.";
  return provenance.sessions.map(s=>`<p>${sessionReference(s)}</p>`).join("")+
    `<p class="footnote">${num(provenance.known_session_count)} known native sessions · ${num(provenance.unknown_session_records)} transcript references with incomplete identity.`+
    (provenance.session_records_cut ? ` ${num(provenance.session_records_cut)} more references are available through complete linked evidence.` : "")+`</p>`;
}

export function renderRuleInspector(row, tab) {
  if (!row) return emptyState("No rule selected", "Pick a rule from the table.", "");
  /* The heading is derived by ruleHeading(); the id stays visible below. */
  const active = RULE_TABS.some((t) => t.id === tab) ? tab : "why";
  const gap = row.enforcement_gap || {};
  let body = "";

  if (active === "why") {
    body =
      section("Rule", `<p class="field__value">${mdLite(row.rule_text)}</p>`) +
      section("Why the machine believes it", `<p class="field__value">${esc(row.why || "no reason was recorded")}</p>`) +
      section("Provenance", `<p class="field__value">${num(row.evidence_linked)} linked incidents · ${num(row.project_count)} projects</p>` +
        `<p class="footnote">${esc((row.provenance?.agents_in_evidence || []).join(" · ") || "Agent not recorded")} · Miner: ${novalue(row.miner_generation)}</p>` +
        `<button class="btn btn--quiet" data-tab="provenance">Inspect full provenance →</button>`) +
      section(
        "Enforcement gap",
        `<p class="field__value">${badge(gap.flagged ? "Reported violation" : "not recorded", {
          state: gap.flagged ? "partial" : "info",
          title: gap.label,
        })} ${esc(gap.label)}</p>` +
          (gap.violated_existing_rule
            ? stackField("Rule named in the report", `<span class="mono">${esc(gap.violated_existing_rule)}</span>`)
            : `<p class="footnote">No violation report was retained for this learning.</p>`)
      ) +
      section("Gate verdict", renderRuleVerdicts(row)) +
      section("Waiting on you", renderRuleReviewLink(row)) +
      section("Recovery", `<p><a class="review-link" href="${esc(recoveryLink(row.id,"hook"))}">Request a hook proposal…</a></p><p><a class="review-link" href="${esc(recoveryLink(row.id,"correct_target"))}">Request a corrected target…</a></p>`) +
      section(
        "Diff",
        renderRuleDiffs(row)
      );
  } else if (active === "provenance") {
    const provenance = row.provenance || {};
    body =
      section(
        "Where it came from",
        field("Repositories", provenance.projects ? provenance.projects.map(projectReference).join(" · ") || "Project unknown" : (provenance.repos || []).map(esc).join(", ") || "Project unknown") +
          field("Recorded learning source", esc(provenance.learning_source?.label || (provenance.agent_product ? sourceLabel(provenance.agent_product) : "Agent unknown"))) +
          field("Agents in linked evidence", (provenance.agents_in_evidence || []).map(a=>esc(sourceLabel(a))).join(", ") || "Agent unknown") +
          (provenance.unknown_source_incidents ? field("Unknown source",`${num(provenance.unknown_source_incidents)} linked incident(s)`) : "") +
          field("Evidence", `${num(provenance.incidents_total)} incident(s), ${num(row.project_count)} project(s)`) +
          (row.unknown_project_incidents ? field("Unknown project",`${num(row.unknown_project_incidents)} linked incident(s)`) : "") +
          field("Miner generation", novalue(row.miner_generation)) +
          (row.miner_generation && row.miner_generation.computable ?
            field("Content produced",esc(row.miner_generation.created_at)) +
            field("Mining run",esc(row.miner_generation.run_id || "Not recorded")) +
            field("Reported model",esc(row.miner_generation.model_reported || "Not recorded")) : "") +
          field("Confidence", esc(String(row.confidence)))
      ) +
      section("Mining history",renderMiningHistory(row)) +
      section(
        "Identity and timing",
        field("Learning id", `<span class="id">${esc(row.id)}</span>`) +
          field("Scope", esc(row.scope || EM_DASH)) +
          field("Category", esc(row.category || EM_DASH)) +
          field("First seen", esc(row.first_seen || EM_DASH)) +
          field("Last seen", esc(row.last_seen || EM_DASH)) +
          field("Created", esc(row.created_at || EM_DASH)) +
          field("Primary path", `<span class="mono">${esc(row.primary_project_path || EM_DASH)}</span>`)
      ) +
      section(
        "Sessions",
        ruleSessions(provenance)+`<p class="footnote">Source products describe retained evidence. Their distribution does not measure relative agent quality. Miner generation and model telemetry are separate facts.</p>`
      );
  } else {
    const provenance = row.provenance || {};
    const incidents = provenance.incidents || [];
    body =
      section(
        "Evidence incidents",
        incidents.length
          ? incidents.map(renderIncident).join("")
          : emptyState(
              "No evidence is linked",
              "No row in incident_learnings points at this rule.",
              "evidence_count says " + String(row.evidence_count) + ", so the link table and the counter disagree.",
              { inline: true }
            )
      ) +
      (provenance.incidents_cut
        ? `<p class="footnote">${num(provenance.incidents_cut)} more incident(s) not shown: ${esc(provenance.cut_reason)}.</p>`
        : "") +
      section("Targets", renderRuleVerdicts(row));
  }

  return renderTabs(RULE_TABS, active, tab => rulesHref(row.id, {...Object.fromEntries(new URLSearchParams(state.ruleQuery)),tab,scan:""})) + `<p><a class="review-link" href="${esc(evidenceHref("learning",row.id,{mode:"evidence",tab:active==="evidence" ? "linked" : "diagnosis"}))}">Inspect complete evidence and diagnosis</a></p>` + body;
}

/** Render one retained observation with its recorded configuration. */
export function renderScanObservation(record, key) {
  const cause = record.failure_cause || "";
  const coverage = record.record?.coverage || {};
  const issues = coverage.incomplete_causes || [];
  const association = record.link_kind === "produced" ? " · producing observation" : record.link_kind === "corroborated" ? " · corroborating observation" : "";
  return `<article class="run-record"><details data-scan-observation="${esc(record.id)}" ${reviewDisclosure(key + ":observation")}>` +
    `<summary>${esc(record.observed_at)} · ${esc(record.source)} · ${esc(record.outcome)}${esc(association)}</summary>` +
    `<p>${badge(record.outcome, {state: record.outcome === "failed" ? "fail" : "info"})} <span class="mono">${esc(record.id)}</span></p>` +
    (record.link_kind ? `<p>${record.link_kind === "produced" ? "Producing observation" : "Later corroborating observation"} · ${num(record.occurrence_ids?.length)} linked occurrence(s)</p>` : "") +
    field("Observed at", esc(record.observed_at)) +
    field("Source", esc(record.source)) +
    field("Transcript", `<span class="mono">${esc(record.session_file)}</span>`) +
    field("Run", record.run_id ? `<a class="mono" href="${esc(runHref("run", record.run_id))}">${esc(record.run_id)}</a>` : "Unknown") +
    field("Timestamp range in data", `${esc(record.first_occurred_at || "Unknown")} – ${esc(record.last_occurred_at || "Unknown")}`) +
    field("Projection at scan", esc(record.projection)) +
    field("Physical line boundary", num(record.line_end)) +
    field("Pending tail bytes", num(record.pending_bytes)) +
    (cause ? `<p class="error-state">${esc(cause)}</p>` : "") +
    (issues.length ? `<p class="footnote">Coverage: ${esc(issues.join(", "))}</p>` : "") +
    field("Detector / parser version", `<span class="mono">${esc(record.compatibility_key)}</span>`) +
    (record.manifest?.identifiable === false ? `<p class="footnote">The executed detector or parser identity is unknown.</p>` : "") +
    runDisclosure(key, "Complete scan observation, coverage, links and recorded configuration", record) + `</details></article>`;
}

function scanHistoryBody(id, rootId) {
  const history = state.scanHistories[id] || {records: [], loaded: false};
  let html = `<h4>Detector observations</h4>`;
  if (history.incident) {
    const provenance = history.incident.provenance;
    html += `<p class="footnote">${provenance === "observed" ? "The producing scan observation is retained." : provenance === "legacy_unknown_corroborated" ? "The producing detector is unknown. Retained rescans corroborate this incident; they do not establish its original provenance." : "The producing detector is unknown."}</p>`;
  }
  if (history.reason_text) html += `<p class="footnote">${esc(history.reason_text)}</p>`;
  if (history.error) html += readFailure(history, {key:rootId, summaryId:rootId+"-error", title:"Could not read detector observations.",
    guidance:(history.loaded ? "Showing the previous observations. " : "")+"Use the detector observations button to retry."});
  if (history.loaded) html += `<p class="caption" aria-live="polite">${num(history.records.length)} observations shown${history.count == null ? "" : ` of ${num(history.count)}`}</p>`;
  html += (history.records || []).map(record => renderScanObservation(record, rootId + ":" + record.id)).join("");
  if (history.loading) html += `<p role="status">Reading detector observations…</p>`;
  html += `<button class="btn" type="button" id="${esc(rootId)}-load" data-scan-history="${esc(id)}" aria-disabled="${Boolean(history.loading)}">${history.loaded ? "Refresh detector observations" : "Load detector observations"}</button>`;
  if (history.next_cursor) html += ` <button class="btn" type="button" id="${esc(rootId)}-older" data-scan-history="${esc(id)}" data-scan-history-older="true" aria-disabled="${Boolean(history.loading)}">Load older observations</button>`;
  return html;
}

export function renderScanHistory(id, scope) {
  const rootId = "scan-history-" + scope + "-" + id;
  return `<section data-scan-history-root="${esc(id)}" id="${esc(rootId)}">${scanHistoryBody(id, rootId)}</section>`;
}

export async function loadIncidentScanHistory(id, {older = false} = {}) {
  const history = state.scanHistories[id] || {records: [], loaded: false, next_cursor: null};
  if (history.loading || (older && !history.next_cursor)) return;
  const focusId = doc()?.activeElement?.id;
  const focusRoot = doc()?.activeElement?.closest?.('[data-scan-history-root]')?.id;
  history.loading = true; history.error = ""; history.errorDetail = ""; state.scanHistories[id] = history;
  const paint = () => doc()?.querySelectorAll('[data-scan-history-root]').forEach(root => {
    if (root.getAttribute('data-scan-history-root') === id) preserveOperationView(root.id, () => scanHistoryBody(id, root.id));
  });
  paint();
  try {
    const result = await getJSON(`/api/incidents/${encodeURIComponent(id)}/scan-history?limit=20` + (older ? `&cursor=${encodeURIComponent(history.next_cursor)}` : ""));
    if (result.selector?.incident_id !== id) throw new Error("Scan history identifies a different incident");
    const records = older ? [...history.records, ...result.records] : result.records;
    Object.assign(history, result, {records: [...new Map(records.map(record => [record.id, record])).values()], loaded: true});
  } catch (error) { history.error = String(error.message || error); history.errorDetail = error?.readDetail || ""; }
  finally {
    const canRestore = focusId && doc()?.activeElement?.id === focusId;
    history.loading = false; paint();
    if (canRestore) {
      const button = doc().getElementById(focusId) || doc().getElementById(focusRoot + "-load");
      if (button) button.focus({preventScroll: true});
    }
  }
}

export function renderScanSummary(summary) {
  if (!summary) return "";
  return `<section class="panel"><div class="panel__head"><h2 class="panel__title">Scan observations and coverage</h2></div><div class="panel__body">` +
    (summary.recorded ? `<div class="scroll-x"><table class="data"><thead><tr><th>Measurement</th><th class="num">Count</th></tr></thead><tbody>` +
      summary.metrics.map(metric => `<tr><td>${esc(metric.label)}</td><td class="num">${num(metric.count)}</td></tr>`).join("") +
      `</tbody></table></div>` : `<p>${esc(summary.reason)}</p>`) +
    `<p class="footnote">${esc(summary.meaning)}</p>` +
    summary.counter_maps.map(map => runDisclosure("scan-counters:" + map.key, map.label,
      map.recorded ? map.counts : "No counters were retained for this category.")).join("") + `</div></section>`;
}

/** Presentation is derived outside frozen records. Complete archives remain inspectable. */
export function renderOccurrenceSummary(value) {
  const o = value.occurrences, c = value.occurrence_coverage;
  if (!o) return "";
  const locations = c?.locations || o.project_paths || [];
  return `<p class="footnote">Recurred ${o.total_count == null ? "an unknown number of" : num(o.total_count)} time(s) across ${o.sessions == null ? "an unknown number of" : num(o.sessions)} session(s). ` +
    `${esc(o.first_ts || "Time unknown")} to ${esc(o.last_ts || "Time unknown")}.</p>` +
    (o.count_reason ? `<p class="footnote">${esc(o.count_reason)}</p>` : "") +
    (o.session_reason ? `<p class="footnote">${esc(o.session_reason)}</p>` : "") +
    (locations.length ? `<p class="footnote">Observed projects: ${locations.map(p => `<code>${esc(p)}</code>`).join(" · ")}</p>` : "") +
    (c?.locations_omitted ? `<p class="footnote">${num(c.locations_omitted)} more project locations in the complete archive.</p>` : "") +
    (c?.unknown_times ? `<p class="footnote">Time unknown for ${num(c.unknown_times)} occurrence entries; the range covers known times only.</p>` : "") +
    (c?.unknown_projects ? `<p class="footnote">Project location unknown for ${num(c.unknown_projects)} occurrence entries.</p>` : "") +
    (c?.other_entries ? `<p class="footnote">${num(c.other_entries)} other archived entries are excluded from occurrence totals.</p>` : "");
}

function incidentPrimary(row, presentation = row.presentation) {
  if (presentation) return presentation;
  // Compatibility for unannotated frozen snapshots and already-open responses.
  const fingerprint = /^[a-f0-9]{40}$/i.test(row.matched_text || "") ? row.matched_text : "";
  let window = [], error = "";
  if (row.window_json != null) {
    try {
      window = JSON.parse(row.window_json);
      if (!Array.isArray(window)) throw new Error("Archive must be a list.");
      window.forEach((entry, index) => {
        if (!entry || typeof entry !== "object" || Array.isArray(entry)) throw new Error(`Entry ${index + 1} must be an object.`);
        for (const key of ["role", "text", "ts", "session_file", "project_path"]) {
          if (key in entry && typeof entry[key] !== "string") throw new Error(`Entry ${index + 1}: ${key} must be text.`);
        }
        if ("count_in_session" in entry && (!Number.isSafeInteger(entry.count_in_session) || entry.count_in_session < 0)) throw new Error(`Entry ${index + 1}: invalid occurrence count.`);
      });
    } catch (exc) { error = String(exc.message || exc); window = []; }
  }
  const text = (!fingerprint && row.matched_text) || window.find(e => e.text)?.text || "No readable text was retained.";
  const cut = clamp(text, 400);
  return {fingerprint, display_text: cut.text, display_text_truncated: cut.cut ? {cut_chars:cut.cut,original_chars:text.length} : null, archive_error: error};
}

export function renderIncidentPrimary(row, presentation = row.presentation) {
  const value = incidentPrimary(row, presentation);
  return `<p data-incident-readable="true">${esc(value.display_text)}</p>` +
    (value.fingerprint ? `<p class="footnote">Detector fingerprint: <code>${esc(value.fingerprint)}</code></p>` : "") +
    (value.archive_error ? `<p class="error-state">Retained context is unreadable: ${esc(value.archive_error)}</p>` : "") +
    renderOccurrenceSummary(value) +
    (value.display_text_truncated ? `<p class="footnote">${num(value.display_text_truncated.cut_chars)} more characters in the complete retained evidence.</p>` : "");
}

/** Render normalized incident text; a detector fingerprint is not an excerpt. */
export function renderIncident(incident) {
  const marks = [badge(incident.signal_type, { state: "info", title: `signal_type ${incident.signal_type}` })];
  if (incident.matched_text_is_fingerprint) {
    marks.push(
      badge("fingerprint", {
        state: "partial",
        title: `matched_text is a bare sha1 (${incident.fingerprint}); the readable text comes from window_json`,
      })
    );
  }
  marks.push(badge(`window: ${incident.window_kind}`, { state: incident.window_kind === "unknown" ? "partial" : "", title: "the window shape was sniffed from its own keys, not inferred from signal_type" }));
  const occurrences = renderOccurrenceSummary(incident);
  const cut = incident.display_text_truncated
    ? `<p class="footnote">${num(incident.display_text_truncated.cut_chars)} character(s) cut of ${num(incident.display_text_truncated.original_chars)}.</p>`
    : "";
  return (
    `<div class="field field--stack">` +
    `<span class="field__label">${marks.join(" ")} <span class="id">${esc(incident.id)}</span> ${esc(incident.ts || "")}</span>` +
    `<span class="field__value mono">${esc(incident.display_text)}</span>` +
    renderSourceIdentity(incident.identity) + occurrences +
    cut +
    `<p><a href="${esc(evidenceHref("incident",incident.id,{mode:"evidence",tab:"source"}))}">Inspect complete incident evidence</a></p>` +
    renderScanHistory(incident.id, "rule") +
    (incident.status === "new" ? `<a class="review-link" href="#/review/mine/${encodeURIComponent(incident.id)}">Preview mining this incident…</a>` : "") +
    `</div>`
  );
}

/**
 * The unified diff each proposal would apply. PRD 7 V2 asks the inspector for
 * it, and until 2026-08-23 queries.rules() did not select the column, so this
 * showed an empty state for data that was one SELECT away.
 *
 * A proposal with an EMPTY diff column and a diff nobody fetched are different
 * facts, and a blank panel cannot tell them apart. The data layer sends
 * `diff_reason` for the former.
 */
export function renderRuleDiffs(row) {
  const targets = (row.targets || []).filter((t) => t.diff || t.diff_reason);
  if (targets.length === 0) {
    return emptyState(
      "No proposal to diff",
      "This learning has not been routed to a file yet, so there is no patch.",
      "A learning is what was learned; a proposal is one edit to one file.",
      { inline: true }
    );
  }
  return targets
    .map((target) => {
      if (!target.diff) {
        return (
          `<div class="field field--stack">` +
          `<span class="field__value mono">${esc(target.target_path)}</span>` +
          `<p class="footnote">${esc(target.diff_reason)}</p>` +
          `</div>`
        );
      }
      return (
        `<div class="field field--stack">` +
        `<span class="field__value mono">${esc(target.target_path)}</span>` +
        `<pre class="scroll-x diff"><code>${renderUnifiedDiff(target.diff)}</code></pre>` +
        (target.proposal_status === "applied" ? `<a class="review-link" href="#/review/rollback/${encodeURIComponent(target.proposal_id)}">Review rollback of this change →</a>` : "") +
        (target.proposal_status === "rolled_back" ? `<a class="review-link" href="#/review/reapply/${encodeURIComponent(target.proposal_id)}">Review reapplication →</a>` : "") +
        (target.proposal_id ? `<a class="review-link" href="#/review/eval/${encodeURIComponent(target.proposal_id)}">Review evaluation regeneration →</a>` : "") +
        `</div>`
      );
    })
    .join("");
}

export function renderRuleVerdicts(row) {
  const historyLink = `<p><a href="${esc(ruleEvaluationHref(row.id))}">Every recorded evaluation attempt →</a></p>`;
  const targets = row.targets || [];
  if (targets.length === 0) {
    return historyLink + emptyState(
      "Not routed anywhere yet",
      row.target_summary || "No proposal has been created for this rule.",
      "A learning is what was learned; a proposal is one edit to one file. One learning can produce zero proposals or several.",
      { inline: true }
    );
  }
  return historyLink + targets
    .map((target) => {
      const evaluation = target.eval;
      const verdict = evaluation
        ? badge(evaluation.verdict, {
            verdict: evaluation.verdict,
            title: VERDICT_MEANING[evaluation.verdict] || `verdict ${evaluation.verdict} is not one of the four this dashboard knows`,
          })
        : novalue(null, "this proposal carries no eval_result_id, so the gate has not spoken about it");
      const detail = evaluation
        ? `<p class="footnote"><span class="strong">${esc(evaluation.label)}</span> &mdash; ${esc(evaluation.why)} ` +
          `(${num(evaluation.succeeded)} of ${num(evaluation.attempted)} trials passed, ${num(evaluation.failed)} failed)</p>`
        : `<p class="footnote">No eval result is linked to this proposal.` +
          // Learning-subject evaluations can exist without a proposal link.
          // Display that relationship without treating them as this edit's gate.
          ((row.unlinked_subject_evals || []).length
            ? ` But ${num(row.unlinked_subject_evals.length)} eval result(s) name this ` +
              `learning as their subject and are not linked to this learning's proposals: ` +
              `<span class="mono">${esc(row.unlinked_subject_evals.join(", "))}</span>. ` +
              `These evaluations are not linked to this proposal. Inspect their recorded sources before using them to assess its edit.`
            : "") +
          `</p>`;
      return (
        `<div class="field field--stack">` +
        `<span class="field__label">${verdict} ${badge(target.proposal_status, { state: "" })} ${esc(target.action)} ${esc(target.target_kind)}</span>` +
        `<span class="field__value mono">${esc(target.target_path)}</span>` +
        detail +
        `</div>`
      );
    })
    .join("");
}

/* ---------------------------------------------------------------------------
   V4 — Projects. One row per repo, clones collapsed.
   --------------------------------------------------------------------------- */

export function keyMethodState(method) {
  if (method === "gh_repo_id") return "";
  if (method === "unresolved" || method === "") return "skipped";
  return "partial";
}

export function keyMethodTitle(method) {
  if (method === "gh_repo_id") return "keyed on GitHub's numeric repo id, which survives a rename";
  if (method === "remote_url") return "keyed on the remote URL: this identity can change when the remote URL changes";
  if (method === "git_root") return "keyed on the git root path: clones do not collapse across machines";
  if (method === "path") return "keyed on the raw path: every clone is its own row";
  if (method === "unresolved") return "identity was not resolved; inspect the recorded path and resolution details";
  return `identity method ${method} is not one this dashboard knows`;
}

export function renderExposureRate(measure) {
  if (!measure || measure.computable !== true) return novalue({reason: measure?.reason_text || "No physical-line measurement was returned."});
  const partial = measure.coverage?.coverage_complete === false;
  const rate = measure.rate_per_100k;
  const label = rate > 0 && rate < 0.1 ? "<0.1" : dec1(rate);
  return `<span class="strong" title="${esc(num(measure.occurrences))} signal occurrences in ${esc(num(measure.eligible_lines))} eligible physical lines">${esc(label)}</span>` +
    (partial ? `<span class="caption muted"> partial</span>` : "");
}

function exposureCounts(counts) {
  return Object.entries(counts || {}).map(([key, value]) => field(key.replaceAll("_", " "), num(value))).join("");
}

export function renderProjectExposure(measure, entry = {}) {
  if (entry.loading) return `<p role="status">Reading scan exposure…</p>`;
  if (entry.error) return emptyState("Exposure could not be read", entry.error, "") + `<button class="btn" type="button" data-exposure-retry="true">Retry exposure</button> <button class="btn" type="button" data-exposure-reset="true">Reset to last 30 days</button>`;
  if (!measure) return emptyState("Not measured", "No scan exposure was returned.", "");
  const requested = measure.requested || {};
  const select = (name, label, all, options, value) => `<label>${esc(label)}<select name="${name}"><option value="">${esc(all)}</option>` +
    options.map(([id, text]) => `<option value="${esc(id)}"${id === value ? " selected" : ""}>${esc(text)}</option>`).join("") + `</select></label>`;
  const versions = (measure.version_groups || []).map(group => [group.compatibility_key, `${group.identifiable ? "Known" : "Unknown"} · ${group.compatibility_key}`]);
  if (requested.compatibility_key && !versions.some(([key]) => key === requested.compatibility_key)) versions.push([requested.compatibility_key, `Uncovered · ${requested.compatibility_key}`]);
  const copies = (measure.options?.working_copies || []).map(copy => [copy.id, copy.normalized_path]);
  if (requested.working_copy_id && !copies.some(([key]) => key === requested.working_copy_id)) copies.push([requested.working_copy_id, requested.working_copy_id]);
  const controls = `<form id="project-exposure-form" class="exposure-form">` +
    `<label>Start (UTC, inclusive)<input name="start" required value="${esc(requested.start || "")}" placeholder="2026-08-01T00:00:00Z"></label>` +
    `<label>End (UTC, exclusive)<input name="end" required value="${esc(requested.end || "")}" placeholder="2026-09-01T00:00:00Z"></label>` +
    select("compatibility_key", "Detector / parser configuration", "Choose automatically only when one version exists", versions, requested.compatibility_key) +
    select("working_copy_id", "Recorded working copy", "All working copies", copies, requested.working_copy_id) +
    select("signal_type", "Signal", "All signals", (measure.options?.signal_types || []).map(signal => [signal, signal.replaceAll("_", " ")]), requested.signal_types?.length === 1 ? requested.signal_types[0] : "") +
    `<button class="btn" type="submit" id="exposure-submit">Measure interval</button></form>`;
  const rawCounts = measure.eligible_lines == null ? "" :
    field("Signal occurrences", num(measure.occurrences)) + field("Eligible physical lines", num(measure.eligible_lines)) +
    field("Observed timestamped lines", num(measure.observed_lines)) +
    field("Logical sessions", num(measure.sessions?.logical_sessions)) +
    field("Median / largest session (lines)", `${measure.session_size?.median == null ? EM_DASH : num(measure.session_size.median)} / ${measure.session_size?.max == null ? EM_DASH : num(measure.session_size.max)}`);
  const rate = measure.computable ? field("Occurrences / 100k lines", renderExposureRate(measure)) :
    emptyState("Rate unavailable", measure.reason_text || measure.reason, "", {inline: true});
  const coverage = measure.coverage;
  return controls + section("Observed exposure", rate + rawCounts +
    `<p class="footnote">The numerator counts retained signal occurrences; the denominator counts eligible physical transcript lines. Each record uses its own timestamp in this UTC interval. These are observations, not evidence that a rule caused an improvement.</p>` +
    field("Version", `<span class="mono">${esc(measure.compatibility_key || "No single version selected")}</span>`)) +
    section("Coverage", coverage ?
      field("Observed records", coverage.coverage_complete ? "Complete detector coverage for observed records" : "Partial coverage") +
      exposureCounts(Object.fromEntries(Object.entries(coverage.counts_by_cause || {}).filter(([, n]) => n > 0))) +
      `<p class="footnote">Unknown-time and unattributed records remain unallocated; their counts are not assigned to this interval. Pending tails and observation failures describe the retained transcript scope.</p>` +
      `<p class="footnote">${esc(coverage.retention?.reason || "Historical retention is unknown.")}</p>` +
      field("Earliest timestamp retained", esc(coverage.retention?.earliest_active_line_at || "Unknown")) :
      `<p class="footnote">${esc(measure.reason_text || "Select a single version to inspect coverage.")}</p>`) +
    section("Signals and exclusions", exposureCounts(measure.occurrences_by_signal) + exposureCounts(measure.excluded_lines) + exposureCounts(measure.diagnostics)) +
    section("Workload", ["eligible_lines", "occurrences"].map(kind => {
      const values = measure.workload?.[kind];
      return values ? `<h4>${kind === "eligible_lines" ? "Eligible lines" : "Signal occurrences"}</h4>` + exposureCounts(values.by_source) +
        exposureCounts({headless: values.headless, interactive: values.interactive, subagent: values.subagent, main: values.main}) : "";
    }).join("") + `<p class="footnote">Source, interaction mode and agent role are separate breakdowns of the same total.</p>`) +
    section("Detector and parser versions", (measure.version_groups || []).map(group =>
      (group.manifests || []).map(manifest => runDisclosure("scan-manifest:" + manifest.manifest_id,
        `Recorded configuration · ${group.compatibility_key}`, manifest)).join("")).join("")) +
    runDisclosure("project-exposure-record", "Complete measurement record and version groups", measure);
}

export function renderContextWeightCell(weight) {
  if (!weight || weight.computable !== true || !weight.groups) return novalue(weight, "No current source-profile inventory is retained");
  const detail = `${weight.file_count} instruction sources observed at ${weight.observed_at}. ${weight.selection} This is not session-loaded context.`;
  return `<span class="strong" title="${esc(detail)}">${esc(bytes(weight.total_bytes))}</span><span class="caption muted"> observed</span>`;
}

export const PROJECT_PAGE_SIZE = 10;
export const PROJECT_SORTS = Object.freeze({
  repository: {label: "Repository", direction: "asc"},
  sessions: {label: "Indexed sessions", direction: "desc"},
  incidents: {label: "Queue incidents", direction: "desc"},
  rate: {label: "Signals per 100k lines", direction: "desc"},
  signal: {label: "Top signal share", direction: "desc"},
  context: {label: "Observed context bytes", direction: "desc"},
  rules: {label: "Applied lessons", direction: "desc"},
});

export function projectListOptions(query = "") {
  const params = new URLSearchParams(query);
  const sort = Object.hasOwn(PROJECT_SORTS, params.get("sort")) ? params.get("sort") : "sessions";
  const direction = ["asc", "desc"].includes(params.get("dir")) ? params.get("dir") : PROJECT_SORTS[sort].direction;
  const rawPage = params.get("page") || "1";
  const page = /^[1-9]\d*$/.test(rawPage) && Number.isSafeInteger(Number(rawPage)) ? Number(rawPage) : 1;
  return {query: (params.get("q") || "").trim(), sort, direction, page};
}

export function projectListPage(rows = [], query = "") {
  const options = projectListOptions(query);
  const compare = (a, b) => a < b ? -1 : a > b ? 1 : 0;
  const numeric = value => typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
  const value = row => {
    if (options.sort === "repository") return (row.label || row.project_key).toLowerCase();
    if (options.sort === "rate") return row.exposure?.computable === true ? numeric(row.exposure.rate_per_100k) : null;
    if (options.sort === "signal") return row.top_signal && row.incidents > 0 ? numeric(row.top_signal.count / row.incidents) : null;
    if (options.sort === "context") return row.context_weight?.computable === true ? numeric(row.context_weight.total_bytes) : null;
    return numeric(row[options.sort === "rules" ? "rules_applied_here" : options.sort]);
  };
  const needle = options.query.toLowerCase();
  const filtered = rows.filter(row => [row.label, row.project_key, row.key_method,
    ...(row.displays || []), ...(row.key_methods || []), ...(row.clone_paths || [])]
    .some(text => String(text || "").toLowerCase().includes(needle)));
  filtered.sort((a, b) => {
    const left = value(a), right = value(b);
    if (left === null && right !== null) return 1;
    if (right === null && left !== null) return -1;
    const order = compare(left, right);
    return (options.direction === "desc" ? -order : order) || compare(a.project_key, b.project_key);
  });
  const pages = Math.max(1, Math.ceil(filtered.length / PROJECT_PAGE_SIZE));
  const page = Math.min(options.page, pages), offset = (page - 1) * PROJECT_PAGE_SIZE;
  return {...options, page, pages, count: filtered.length, total: rows.length,
    start: filtered.length ? offset + 1 : 0, end: Math.min(offset + PROJECT_PAGE_SIZE, filtered.length),
    rows: filtered.slice(offset, offset + PROJECT_PAGE_SIZE)};
}

export function renderProjectCopies(row) {
  const key = encodeURIComponent(row.project_key);
  return `<tr class="project-copies" id="project-copies-${key}"><td colspan="8"><div>` +
    `<h3>Indexed working copies · ${esc(row.label || row.project_key)}</h3>` +
    `<p>Canonical identity: <code>${esc(row.project_key)}</code></p>` +
    `<p>Identity methods: ${esc((row.key_methods?.length ? row.key_methods : [row.key_method || "unknown"]).join(", "))}</p>` +
    (row.displays?.length > 1 ? `<p>Recorded names: ${row.displays.map(esc).join(" · ")}</p>` : "") +
    `<ul>${(row.clone_paths || []).map(path => `<li><code>${esc(path)}</code></li>`).join("")}</ul>` +
    `<p class="caption">These are retained index paths. Aliases, current disk presence and runtime loading are not inferred.</p>` +
    `<a href="#/projects/${key}?tab=copies">Inspect working-copy evidence</a> · <a href="#/projects/${key}?tab=inventory">Inspect observed instruction sources</a>` +
    `</div></td></tr>`;
}

export function renderProjectBenefit(row) {
  const key = encodeURIComponent(row.project_key), benefit = row.benefit;
  const known = benefit?.computable === true;
  // Stop the enclosing row's project shortcut at this native disclosure boundary.
  return `<details class="project-benefit-detail" name="project-benefit" data-project-key="" ${reviewDisclosure("project-benefit:"+row.project_key)}>` +
    `<summary id="project-benefit-${key}"><span${known ? "" : ' class="novalue"'}>${esc(known ? benefit.value : "—")}</span> <span class="project-benefit-hint">${known ? "Details" : "Why?"}</span></summary>` +
    `<div class="project-benefit-content"><strong>${esc(row.label || row.project_key)}</strong>` +
    `<p class="project-benefit-reason">${esc(benefit?.reason || "Benefit was not measured")}</p>` +
    (benefit?.meaning ? `<p class="project-benefit-meaning">${esc(benefit.meaning)}</p>` : "") +
    `<a id="project-benefit-evidence-${key}" href="#/projects/${key}?tab=recurrence">Inspect recurrence evidence</a></div></details>`;
}

export function renderProjectRows(rows, selectedKey, expanded = new Set()) {
  return (rows || []).map(row => {
    const key = encodeURIComponent(row.project_key), open = expanded.has(row.project_key);
    const name = row.label || row.project_key;
    const method = badge(({remote_url:"remote",gh_repo_id:"GitHub"})[row.key_method] || row.key_method || "unset", {state: keyMethodState(row.key_method), title: keyMethodTitle(row.key_method)});
    const copies = `${num(row.clones)} ${row.clones === 1 ? "path" : "paths"}`;
    const signal = row.top_signal;
    const share = signal && row.incidents > 0 ? 100 * signal.count / row.incidents : null;
    const signalTitle = signal ? `${num(signal.count)} incidents of this signal out of ${num(row.incidents)} queue incidents` +
      (signal.tied_with?.length ? `; tied with ${signal.tied_with.join(", ")}; ${signal.tie_break}` : `; ${signal.tie_break}`) : "No queue incidents";
    const weight = row.context_weight;
    const applied = Number.isInteger(row.rules_applied_here) ? num(row.rules_applied_here) : novalue(null, "Applied lesson count was not returned");
    return `<tr data-project-key="${esc(row.project_key)}"${row.project_key === selectedKey ? ' aria-selected="true"' : ""}>` +
      `<td class="project-repository"><div class="project-repository__head">` +
      `<button class="project-expand" id="project-expand-${key}" type="button" data-project-expand="${esc(row.project_key)}" aria-expanded="${open}"${open ? ` aria-controls="project-copies-${key}"` : ""} aria-label="${open ? "Collapse" : "Expand"} indexed copies of ${esc(name)}"><img src="/icons/project-expand.svg" alt="" width="8" height="8"></button>` +
      `<a class="project-name" href="#/projects/${key}" title="${esc(name)}">${esc(name)}</a></div>` +
      `<div class="project-repository__meta"><span title="Retained indexed working-copy paths; current disk presence is separate">${copies}</span>${method}</div>` +
      (row.displays?.length > 1 ? `<span class="project-aliases" title="${esc(row.displays.join(" · "))}">${esc(row.displays.filter(alias => alias !== name).join(" · "))}</span>` : "") +
      (row.label_is_fallback ? `<span class="caption muted">(no display name)</span>` : "") + `</td>` +
      `<td class="num">${num(row.sessions)}</td><td class="num">${num(row.incidents)}</td>` +
      `<td class="num"><a class="project-measure" href="#/projects/${key}?tab=exposure" data-project-key="${esc(row.project_key)}" data-focus="exposure">${renderExposureRate(row.exposure)}</a></td>` +
      `<td class="project-signal" title="${esc(signalTitle)}">${signal ? `<span>${esc(signal.signal_type.replaceAll("_", " "))}</span>` +
        (share === null ? "" : `<small>${dec1(share)}% of queue</small><span class="project-signal__bar" aria-hidden="true"><span style="width:${share}%"></span></span>`) : novalue(null, "this repo has no incidents")}</td>` +
      `<td class="project-context"><a class="project-measure" href="#/projects/${key}?tab=context" data-project-key="${esc(row.project_key)}" data-focus="context">${renderContextWeightCell(weight)}</a>` +
      (weight?.computable === true && Number.isInteger(weight.file_count) ? `<small>${num(weight.file_count)} observed sources</small>` : "") + `</td>` +
      `<td class="num project-rules"><a class="project-measure" href="#/projects/${key}?tab=rules" data-project-key="${esc(row.project_key)}" data-focus="rules" title="Unique lessons with a recorded applied proposal in this repository; current presence is a separate observation">${applied} applied</a><small>${num(row.rules_written_here)} contributed</small></td>` +
      `<td class="project-benefit">${renderProjectBenefit(row)}</td></tr>` +
      (open ? renderProjectCopies(row) : "");
  }).join("");
}

export function renderProjectsFoot(payload) {
  if (!payload) return "";
  return (
    `${num(payload.count)} repositories, collapsed from ${num(payload.clone_paths_total)} working copies on ` +
    `<span class="mono">${esc(payload.grouped_on)}</span>. ` +
    `${num(payload.sessions_total)} indexed sessions, ${num(payload.incidents_total)} queue incidents (all retained history). Rates use the last 30 days of timestamped scan observations; select Exposure for coverage.`
  );
}

export function renderProjectNotes(payload) {
  if (!payload) return "";
  const notes = [];
  // A silent filter is worse than the noise it removes. Say how many rows were
  // left out, split by why, so the operator can tell "no friction there" from
  // "not shown".
  const excluded = payload.not_a_repository;
  if (excluded && excluded.count) {
    const split = Object.keys(excluded.by_method || {})
      .sort()
      .map((m) => `${num(excluded.by_method[m])} ${esc(m)}`)
      .join(", ");
    notes.push(
      emptyState(
        `${num(excluded.count)} director${excluded.count === 1 ? "y is" : "ies are"} not shown`,
        `They were not recognized as Git repositories in this snapshot (${esc(split)}).`,
        excluded.reason,
        { inline: true }
      )
    );
  }
  if (!payload.context_weight_available) {
    notes.push(
      emptyState(
        "Context weight was not measured",
        payload.context_weight_reason || "the walker is not available in this process",
        "Observed source bytes require a retained inventory. They do not prove session loading.",
        { inline: true }
      )
    );
  } else if (payload.context_weight_capped) {
    notes.push(
      `<p class="footnote">Context weight measured for ${num(payload.context_weight_capped.measured)} repo(s); ` +
        `${num(payload.context_weight_capped.skipped)} skipped. Reason: ${esc(payload.context_weight_capped.reason)}.</p>`
    );
  }
  (payload.context_weight_errors || []).forEach((error) => {
    notes.push(
      `<div class="field"><span class="field__label">${badge("weigh failed", { state: "failed" })}</span>` +
        `<span class="field__value mono">${esc(error.path)} &mdash; ${esc(error.error)}</span></div>`
    );
  });
  if ((payload.proposals_not_attributed || []).length) {
    notes.push(
      section(
        "Proposals that belong to no repo",
        (payload.proposals_not_attributed || [])
          .map(
            (proposal) =>
              `<div class="field"><span class="field__label">${badge(proposal.status, { state: "" })}</span>` +
              `<span class="field__value mono">${esc(proposal.target_path)}</span></div>`
          )
          .join("") + `<p class="footnote">${esc(payload.proposals_not_attributed_reason)}</p>`
      )
    );
  }
  if ((payload.unmatched_incident_keys || []).length) {
    notes.push(
      section(
        "Incidents whose repo has no session",
        (payload.unmatched_incident_keys || [])
          .map(
            (entry) =>
              `<div class="field"><span class="field__label mono">${esc(entry.project_key)}</span>` +
              `<span class="field__value">${num(entry.incidents)} incident(s)</span></div>`
          )
          .join("")
      )
    );
  }
  notes.push(`<p class="footnote">Rates describe observed signals per 100,000 physical lines. Missing observations and incompatible versions have no rate. Small samples, unknown-time records and retention gaps remain visible in Exposure.</p>`);
  return notes.join("");
}

const PROJECT_TABS = Object.freeze([
  { id: "summary", label: "Project overview" },
  { id: "inventory", label: "Instruction ownership" },
  { id: "availability", label: "Availability" },
  { id: "sessions", label: "Session evidence" },
  { id: "loads", label: "Loading reports" },
  { id: "recurrence", label: "Recurrence" },
  { id: "exposure", label: "Exposure" },
  { id: "context", label: "Instruction context" },
  { id: "topology", label: "Topology" },
  { id: "copies", label: "Working copies" },
  { id: "rules", label: "Rules" },
]);

const PROJECT_SECTIONS = Object.freeze([
  {id:"overview", label:"Project overview", views:["summary"]},
  {id:"instructions", label:"Instructions", views:["context","inventory","topology"]},
  {id:"copies", label:"Working copies", views:["copies","availability"]},
  {id:"sessions", label:"Sessions", views:["sessions","loads"]},
  {id:"signals", label:"Signals & rules", views:["rules","exposure","recurrence"]},
]);

export function renderProjectNavigation(key, active = "summary") {
  const selected = PROJECT_SECTIONS.find(section => section.views.includes(active)) || PROJECT_SECTIONS[0];
  const href = tab => projectReadHref(key, tab);
  const sections = PROJECT_SECTIONS.map(section => {
    const current = section === selected;
    const tab = current && section.views.includes(active) ? active : section.views[0];
    return `<a id="project-section-${section.id}" class="tab" href="${esc(href(tab))}"${current ? ` aria-current="${section.views.length === 1 ? "page" : "true"}"` : ""}>${esc(section.label)}</a>`;
  }).join("");
  const views = selected.views.length === 1 ? "" : `<nav class="project-views" aria-label="${esc(selected.label)} views">` + selected.views.map(id =>
    `<a id="project-view-${id}" href="${esc(href(id))}"${id === active ? ` aria-current="page"` : ""}>${esc(PROJECT_TABS.find(tab => tab.id === id).label)}</a>`
  ).join("") + `</nav>`;
  return `<div class="project-navigation"><nav class="tabs project-sections" aria-label="Project sections">${sections}</nav>${views}</div>`;
}

export function renderProjectInventory(entry) {
  const owners = {human_managed: "Human-managed", machine: "Exact machine text", edited: "Edited marker", unknown: "Unknown marker"};
  let html = `<h3>Instruction ownership</h3><p>Unmarked text is human-managed. This protects it from automatic deletion; it does not prove who wrote it. Machine ownership requires an exact retained delivery.</p>`;
  if (entry.error) html += `<p class="error-state" role="alert">${esc(entry.error)}</p>`;
  if (entry.loading) html += `<p role="status">Reading recorded inventories…</p>`;
  if (entry.reason === "schema_unavailable") html += `<p>This database predates instruction inventories. An explicit database upgrade is required.</p>`;
  else if (entry.loaded && !entry.records?.length) html += `<p>No instruction inventory has been collected for this project. Its ownership is unknown.</p>`;
  html += `<p id="${esc(entry.statusId || "inventory-status")}" tabindex="-1" class="caption" aria-live="polite">${entry.loaded ? `${num(entry.records.length)} shown · ${entry.count == null ? "copy count unknown" : `${num(entry.count)} working copies`}` : "No recorded inventories loaded yet"}</p>`;
  for (const record of entry.records || []) {
    const name = record.working_copy.normalized_path.split("/").filter(Boolean).pop();
    html += `<article class="run-record"><details ${reviewDisclosure("inventory:" + record.working_copy_id)}><summary>${esc(name)} · ${esc(record.status)} · ${esc(record.observed_at)}</summary>` +
      `<p class="mono">${esc(record.working_copy.normalized_path)}</p>`;
    if (record.status === "conflicting") {
      html += `<p class="error-state">Different inventories share this timestamp. Ownership is unknown.</p>` + runDisclosure("inventory-conflict:" + record.working_copy_id, "Conflicting retained observations", record.observations) + `</details></article>`;
      continue;
    }
    html += `<p>${num(record.totals.files)} instruction sources · ${esc(bytes(record.totals.bytes))} observed · ${num(record.totals.lines)} lines. Symlink and import aliases count once.</p>`;
    if (!record.files.length) html += `<p>${record.status === "recorded" ? "No supported instruction files were found at this observation." : "No files could be counted completely; inspect the coverage issues below."}</p>`;
    html += `<div class="scroll-x"><table class="data"><thead><tr><th scope="col">Ownership</th><th scope="col" class="num">Lines</th><th scope="col" class="num">Bytes</th></tr></thead><tbody>` +
      Object.entries(owners).map(([key,label]) => `<tr><th scope="row">${label}</th><td class="num">${num(record.totals.ownership[key].lines)}</td><td class="num">${num(record.totals.ownership[key].bytes)}</td></tr>`).join("") + `</tbody></table></div>`;
    html += `<h4>Provider and loading scope</h4><p class="footnote">Scopes can share files, so these rows must not be added together. Eligible bytes follow the observed source profile; session loading remains unverified.</p>` +
      record.scopes.map(scope => `<p>${esc(scope.provider)} · ${esc(scope.scope.replaceAll("_", " "))}: ${num(scope.files)} file(s), ${esc(bytes(scope.eligible_prefix_bytes))} eligible of ${esc(bytes(scope.observed_bytes))} observed.</p>`).join("");
    html += `<h4>File ownership and wiring</h4>`;
    for (const file of record.files) {
      html += `<details ${reviewDisclosure("inventory-file:" + record.working_copy_id + ":" + file.real_path)}><summary><span class="mono">${esc(file.path)}</span> · ${esc(bytes(file.bytes))}</summary>` +
        `<p>${Object.entries(owners).map(([key,label]) => `${label}: ${num(file.ownership[key].lines)} lines`).join(" · ")}</p>` +
        file.units.map(unit => `<p>${esc(owners[unit.ownership])} · lines ${num(unit.start_line)}–${num(unit.end_line)}` +
          (unit.learning_ids || (unit.learning_id && unit.ownership !== "unknown" ? [unit.learning_id] : [])).map(id => ` · <a href="#/rules/${encodeURIComponent(id)}">Inspect rule</a>`).join("") +
          (unit.cause ? ` · ${esc(unit.cause.replaceAll("_", " "))}` : "") + `</p>`).join("") +
        runDisclosure("inventory-file-record:" + record.working_copy_id + ":" + file.real_path, "Aliases, imports, scopes, hashes and delivery references", file) + `</details>`;
    }
    if (record.issues.length) html += `<h4>Incomplete coverage</h4>` + record.issues.map(issue => `<p>${esc(issue.cause.replaceAll("_", " "))}${issue.path ? ` · <span class="mono">${esc(issue.path)}</span>` : ""}</p>`).join("");
    html += runDisclosure("inventory-record:" + record.id, "Complete retained inventory and collection limits", record) + `</details></article>`;
  }
  html += `<button id="inventory-load" type="button" class="btn" data-inventory-refresh="true" aria-disabled="${Boolean(entry.loading)}">Refresh recorded inventories</button>`;
  if (entry.next_cursor) html += ` <button id="inventory-older" type="button" class="btn" data-inventory-older="true" aria-disabled="${Boolean(entry.loading)}">Load more inventories</button>`;
  return html;
}

export function renderProjectAvailability(entry) {
  const labels = {available: "Exact delivered content observed", changed: "Marked content has changed", absent: "Delivered content not found", unknown: "Availability unknown"};
  let html = `<h3>Recorded rule availability</h3><p class="footnote">These checks inspect actual working-copy files. They do not prove that an agent loaded a rule or that an already-running session received it.</p>`;
  if (entry.error) html += `<p class="error-state" role="alert">${esc(entry.error)}</p>`;
  if (entry.loading) html += `<p role="status">Reading recorded availability…</p>`;
  if (entry.reason === "schema_unavailable") html += `<p>This database predates availability observations. An explicit database upgrade is required.</p>`;
  if (entry.loaded && !entry.records?.length && entry.reason !== "schema_unavailable") html += `<p>No availability observations are retained for this project. This is unknown history.</p>`;
  html += `<p id="availability-status" tabindex="-1" class="caption" aria-live="polite">${entry.loaded ? `${num(entry.records.length)}${entry.count == null ? "" : ` of ${num(entry.count)}`} rule/copy checks shown` : "No recorded checks loaded yet"}</p>`;
  (entry.records || []).forEach(record => {
    const revision = record.revision;
    const copyPath = record.observations[0].working_copy.normalized_path;
    const copyName = copyPath.split("/").filter(Boolean).slice(-1)[0] || copyPath;
    html += `<article class="run-record"><details ${reviewDisclosure("availability:" + revision.id + ":" + record.working_copy_id)}><summary>${esc(copyName)} · ${esc(labels[record.status] || "Unknown status")} · rule ${esc(revision.learning_id.slice(0, 8))}</summary>` +
      `<p><a href="#/rules/${encodeURIComponent(revision.learning_id)}">Inspect rule</a> · observed ${esc(record.observed_at)}</p>`;
    if (record.conflicting_observations) html += `<p class="error-state">Conflicting checks share this timestamp; availability is unknown.</p>`;
    for (const check of record.observations) {
      html += field("Working copy", `<span class="mono">${esc(check.working_copy.normalized_path)}</span>`) +
        (check.cause ? field("Reason", esc(check.cause.replaceAll("_", " "))) : "") +
        field("Delivered revision", `<span class="mono">${esc(check.rule_revision_id)}</span>`);
      for (const match of check.matches) {
        html += field("Observed file", `<span class="mono">${esc(match.path)}</span>`);
        for (const path of match.loading_paths) html += field("Source scope", `${esc(path.provider)} · ${esc(path.scope.kind.replaceAll("_", " "))}${path.scope.paths.length ? ` · ${esc(path.scope.paths.join(", "))}` : ""}`);
      }
      html += runDisclosure("availability-check:" + check.id, "Complete retained check and file evidence", check);
    }
    html += runDisclosure("availability-revision:" + revision.id, "Delivered content and snapshot provenance", revision) + `</details></article>`;
  });
  if (entry.last_collection) {
    html += `<p class="footnote">Latest collection across known projects: ${esc(entry.last_collection.observed_at)} · ${esc(entry.last_collection.status)}.</p>` +
      runDisclosure("availability-collection", "Collection coverage and failures", entry.last_collection);
  }
  html += `<button id="availability-load" type="button" class="btn" data-availability-refresh="true" aria-disabled="${Boolean(entry.loading)}">Refresh recorded checks</button>`;
  if (entry.next_cursor) html += ` <button id="availability-older" type="button" class="btn" data-availability-older="true" aria-disabled="${Boolean(entry.loading)}">Load more checks</button>`;
  return html;
}

export function renderProjectInspector(row, tab, exposureEntry = {}, {includeTabs = true} = {}) {
  if (!row) return emptyState("No repository selected", "Pick a row from the table.", "");
  const active = PROJECT_TABS.some((t) => t.id === tab) ? tab : "context";
  const weight = row.context_weight || {};
  let body = "";

  if (active === "inventory") {
    body = renderProjectInventory(state.projectInventory[row.project_key] || {});
  } else if (active === "availability") {
    body = renderProjectAvailability(state.projectAvailability[row.project_key] || {});
  } else if (active === "exposure") {
    body = renderProjectExposure(exposureEntry.data || row.exposure, exposureEntry);
  } else if (active === "context") {
    body = renderRecordedContext(row.project_key);
  } else if (active === "topology") {
    body = renderRecordedTopology(row.project_key);
  } else if (active === "copies") {
    const paths = row.clone_paths || [];
    body =
      section(
        "Working copies",
        `<p class="field__value">${num(row.clones)} working copies of one repository. Current filesystem presence is not checked by this reader. ` +
          `They are one row because they are one repository. Counting copies separately would inflate ` +
          `the repository breadth used in routing.</p>` +
          `<div class="kv">${paths.map((path) => `<span class="mono">${esc(path)}</span><span></span>`).join("")}</div>`
      ) +
      section(
        "Identity",
        field("Key", `<span class="mono">${esc(row.project_key)}</span>`) +
          field("Method", badge(row.key_method || "unset", { state: keyMethodState(row.key_method), title: keyMethodTitle(row.key_method) })) +
          field("Display names seen", (row.displays || []).map((d) => esc(d)).join(", ") || novalue(null, "no session recorded a display name")) +
          field("Context measured in", `<span class="mono">${esc(row.context_path || EM_DASH)}</span>`) +
          `<p class="footnote">${esc(row.context_path_reason || "")}</p>`
      );
  } else {
    const received = row.rules_received_detail || [];
    body =
      section(
        "Rules this repo produced",
        `<p class="field__value"><span class="strong">${num(row.rules_written_here)}</span> rule(s) were learned from incidents in this repo.</p>`
      ) +
      section(
        "Rules aimed at this repo",
        received.length
          ? received
              .map(
                (proposal) =>
                  `<div class="field"><span class="field__label">${badge(proposal.status, { state: "" })}</span>` +
                  `<span class="field__value mono">${esc(proposal.target_path)}</span></div>`
              )
              .join("")
          : emptyState(
              "No proposal targets this repo",
              "No recorded proposal is attributed to this repository.",
              "proposals has no project_key; a target is attributed by longest matching clone path.",
              { inline: true }
            )
      ) +
      section(
        "Signals seen here",
        Object.keys(row.signals || {}).length
          ? `<div class="kv">${Object.keys(row.signals)
              .sort((a, b) => row.signals[b] - row.signals[a])
              .map((signal) => `<span>${esc(signal)}</span><span class="num">${num(row.signals[signal])}</span>`)
              .join("")}</div>`
          : emptyState("No incidents", "Nothing was detected in this repo.", "", { inline: true })
      ) +
      // The one designed empty state on this page that was not built with
      // emptyState(): a heading followed by a bare em dash reads as a broken
      // widget, and the 130-character explanation was reachable only by
      // hovering the dash. Every sibling section in this panel already does it
      // this way.
      section(
        "Benefit",
        row.benefit && row.benefit.computable
          ? `<p class="field__value">${novalue(row.benefit)}</p>`
          : emptyState(
              "Benefit is not computable yet",
              "The required recurrence measurements are unavailable.",
              (row.benefit && row.benefit.reason) || "",
              { inline: true }
            )
      );
  }

  return (includeTabs ? renderTabs(PROJECT_TABS, active, tab => projectReadHref(row.project_key,tab)) : "") + body;
}

export function renderWeightCaveats(weight) {
  const notes = [];
  (weight.caps_applied || []).forEach((cap) => {
    notes.push(`<div class="field"><span class="field__label">${badge(`cap: ${cap.cap}`, { state: "partial" })}</span>` +
      `<span class="field__value">${esc(cap.cut)}</span></div>`);
  });
  (weight.missing_imports || []).forEach((missing) => {
    const text = typeof missing === "string" ? missing : missing.spec || JSON.stringify(missing);
    notes.push(`<div class="field"><span class="field__label">${badge("missing import", { state: "failed" })}</span>` +
      `<span class="field__value mono">${esc(text)}</span></div>`);
  });
  (weight.unreadable || []).forEach((entry) => {
    notes.push(`<div class="field"><span class="field__label">${badge("unreadable", { state: "failed" })}</span>` +
      `<span class="field__value mono">${esc(entry.path)} &mdash; ${esc(entry.reason)}</span></div>`);
  });
  (weight.notes || []).forEach((note) => {
    notes.push(`<p class="footnote">${esc(note)}</p>`);
  });
  if (notes.length === 0) return "";
  return section("What the measurement cut or could not read", notes.join(""));
}

/* ---------------------------------------------------------------------------
   The DOM layer. Everything above is a pure function over data.
   --------------------------------------------------------------------------- */

export const state = {
  characterShortcuts: {enabled: true, notice: ""},
  dashboardRetrying: false,
  dashboardErrors: new Map(),
  overview: null,
  overviewLoading: false,
  overviewError: "",
  overviewLoadGeneration: 0,
  reviewMutationGeneration: 0,
  reviewReadGeneration: 0,
  rules: null,
  projects: null,
  projectListQuery: "",
  projectExpanded: new Set(),
  projectsLoading: false,
  projectsError: "",
  projectsReadGeneration: 0,
  projectsFocus: "",
  route: "overview",
  query: "",
  ruleQuery: "",
  rulePage: null,
  ruleDetails: {},
  evidencePage: null,
  evidenceDetail: null,
  linkedEvidence: null,
  selectedEvidence: null,
  evidenceFocus: false,
  reviewRouteProposal: "",
  selectedProposalRead: null,
  executionRoute: null,
  ruleMembers: null,
  rulesSearchTimer: null,
  initialLoading: false,
  numbers: false,
  selectedRule: "",
  selectedProject: "",
  inspectorKind: "",
  inspectorTab: "",
  miningHistories: {},
  scanHistories: {},
  projectExposures: {},
  projectAvailability: {},
  projectInventory: {},
  projectInventoryCopies: {},
  projectInventorySelection: {},
  projectContextHistory: {},
  projectDetails: {},
  projectRuleSummaries: {},
  projectSessions: {},
  projectNativeEvents: {},
  measurementHistory: {},
  measurementDetails: {},
  trends: null,
  evaluationHistory: null,
  evaluationHealth: null,
  evaluationDetail: null,
  classEvidence: null,
  qualityDetail: null,
  qualitySamples: null,
  qualityRequests: {},
  evaluationQuery: "",
  projectRecords: {},
  projectExposureParams: "",
  projectExposureFocus: "",
  runDetail: null,
  review: null,
  selectedFamily: "",
  // Decisions this page has landed, so a card can say what happened to it
  // before the refetch returns. Cleared by a reload, which is correct: the
  // server is the record, this is only the echo.
  decided: {},
  // Families with a decision in flight, keyed by learning id. See decideFamily.
  deciding: {},
  commandRequests: {},
  evalJob: {proposalId: "", preview: null, loading: false, busy: false, error: "", commandId: ""},
  evalRequests: {},
  incidents: {items:[],count:0,loaded:false,loading:false,nextCursor:null,error:""},
  lastCommand: null,
  delivery: { items: [], loaded: false, nextCursor: null, olderLoaded: false, loading: false, error: "", promise: null, generation: 0 },
  deliveryBusy: {},
  deliveryRequests: {},
  deliveryDetails: {},
  deliveryDisclosures: {},
  deliveryTimer: null,
  deliveryRendered: "",
  operations: { items: [], loaded: false, nextCursor: null, olderLoaded: false, loading: false, error: "", promise: null, generation: 0 },
  operationBusy: {},
  operationRequests: {},
  operationDetails: {},
  operationDisclosures: {},
  rollback: { proposalId: "", loading: false, preview: null, error: "", busy: false, operationId: "", generation: 0 },
  rollbackRequests: {},
  reviewDetailFamily: "",
  reviewEvidencePages: {},
  reviewEvaluations: {},
  reviewPreviews: {},
  reviewMembers: {},
  reviewExcluded: {},
  reviewIndividual: {},
  reviewDisclosures: {},
};

function doc() {
  return typeof document === "undefined" ? null : document;
}

/** Look up a declared element. A missing id is a bug in index.html, so it raises. */
export function byId(id) {
  const d = doc();
  if (!d) throw new Error("no document");
  const element = d.getElementById(id);
  if (!element) throw new Error(`index.html declares no element with id ${id}`);
  return element;
}

function setHTML(id, html) {
  byId(id).innerHTML = html;
}

function setText(id, text) {
  byId(id).textContent = text;
}

/**
 * Show or hide. `.view`, `.banner`, `.empty`, `.inspector` and `.error-state`
 * all set `display` in app.css, which beats the hidden attribute's user-agent
 * rule, so visibility has to be an inline style. This is layout wiring, not a
 * second opinion about the design.
 */
export function setVisible(element, visible) {
  element.style.display = visible ? "" : "none";
  element.hidden = !visible;
}

/* ------------------------------ theme ------------------------------------ */

function storage() {
  try {
    if (typeof localStorage === "undefined") return null;
    return localStorage;
  } catch (error) {
    return null;
  }
}

export function readStoredTheme() {
  const store = storage();
  if (!store) return "";
  try {
    const value = store.getItem("self-improve-theme");
    return value === "dark" || value === "light" ? value : "";
  } catch (error) {
    return "";
  }
}

export function storeTheme(theme) {
  const store = storage();
  if (!store) return;
  try {
    store.setItem("self-improve-theme", theme);
  } catch (error) {
    /* a private window refuses to write; the toggle still works for this page */
  }
}

export function systemPrefersDark() {
  if (typeof matchMedia !== "function") return false;
  try {
    return matchMedia("(prefers-color-scheme: dark)").matches === true;
  } catch (error) {
    return false;
  }
}

/** The theme in force: an explicit data-theme, else what the system asks for. */
export function effectiveTheme() {
  const d = doc();
  const declared = d && d.documentElement.getAttribute("data-theme");
  if (declared === "dark" || declared === "light") return declared;
  return systemPrefersDark() ? "dark" : "light";
}

/**
 * Apply a theme. An empty string means "no opinion of my own": the attribute
 * the document already declares is left exactly as it is, and with no attribute
 * at all the operating system decides. Removing it here would silently override
 * a theme the page was served with, which is how a dark page renders light.
 */
export function applyTheme(theme) {
  const d = doc();
  if (!d) return "";
  if (theme === "dark" || theme === "light") {
    d.documentElement.setAttribute("data-theme", theme);
  }
  const dark = effectiveTheme() === "dark";
  const button = byId("theme-toggle");
  button.setAttribute("aria-pressed", dark ? "true" : "false");
  button.textContent = dark ? "Light theme" : "Dark theme";
  return dark ? "dark" : "light";
}

export function toggleTheme() {
  const next = effectiveTheme() === "dark" ? "light" : "dark";
  applyTheme(next);
  storeTheme(next);
  return next;
}

/* ----------------------- character shortcuts ---------------------------- */

export function readCharacterShortcuts(store = storage()) {
  try {
    if (!store) throw new Error("Storage unavailable");
    const value = store.getItem("self-improve-character-shortcuts");
    if (value === null || value === "on" || value === "off") {
      return {enabled: value !== "off", notice: ""};
    }
  } catch { /* An unreadable preference must not enable decision shortcuts. */ }
  return {enabled: false, notice: "Could not read saved shortcuts. Character shortcuts are off until you choose."};
}

function characterHint(content) {
  return `<span data-character-hint${state.characterShortcuts.enabled ? "" : ' hidden style="display:none"'}>${content}</span>`;
}

function characterKey(key) {
  return `data-character-key="${key}"${state.characterShortcuts.enabled ? ` aria-keyshortcuts="${key}"` : ""}`;
}

/** Update hints in place so toggling cannot discard previews, drafts or focus. */
function paintCharacterShortcuts() {
  const d = doc();
  if (!d) return;
  const {enabled, notice} = state.characterShortcuts;
  const button = byId("character-shortcuts-toggle");
  button.textContent = `Character shortcuts: ${enabled ? "on" : "off"}`;
  button.setAttribute("aria-pressed", String(enabled));
  const status = byId("character-shortcuts-notice");
  status.textContent = notice;
  setVisible(status, Boolean(notice));
  for (const element of d.querySelectorAll("[data-character-key]")) {
    if (enabled) element.setAttribute("aria-keyshortcuts", element.getAttribute("data-character-key"));
    else element.removeAttribute("aria-keyshortcuts");
  }
  for (const element of d.querySelectorAll("[data-character-hint]")) setVisible(element, enabled);
}

export function loadCharacterShortcuts(store = storage()) {
  state.characterShortcuts = readCharacterShortcuts(store);
  paintCharacterShortcuts();
}

export function toggleCharacterShortcuts(store = storage()) {
  const enabled = !state.characterShortcuts.enabled;
  let notice = "";
  try {
    if (!store) throw new Error("Storage unavailable");
    store.setItem("self-improve-character-shortcuts", enabled ? "on" : "off");
  } catch {
    notice = "Shortcut setting changed for this page only; it could not be saved.";
  }
  state.characterShortcuts = {enabled, notice};
  paintCharacterShortcuts();
}

/* --------------------------- V3 review queue ------------------------------ */

/**
 * The modifier class for one queueing reason.
 *
 * A lookup rather than string concatenation at the call site: the class
 * attribute then holds a single literal prefix, and
 * `test_every_reason_code_has_a_tint` reconciles these names against app.css
 * in both directions. An unknown code gets the neutral base and SAYS nothing
 * false, rather than pointing at a rule that does not exist.
 */
export const WHY_CLASS = Object.freeze({
  add: "review-card__why--add",
  edit: "review-card__why--edit",
  delete: "review-card__why--delete",
  new_skill: "review-card__why--new_skill",
  new_rule_file: "review-card__why--new_rule_file",
  pending: "review-card__why--pending",
  gated_fail: "review-card__why--gated_fail",
  inconclusive: "review-card__why--inconclusive",
  held: "review-card__why--held",
  gated_pass: "review-card__why--gated_pass",
  ungated: "review-card__why--ungated",
  convert_to_hook: "review-card__why--convert_to_hook",
  delete_human_line: "review-card__why--delete_human_line",
  resolve_rollback: "review-card__why--resolve_rollback",
  reapply: "review-card__why--reapply",
  recover_rule: "review-card__why--recover_rule",
});

export function whyClass(code) {
  return WHY_CLASS[code] || "";
}

/** The tint for one eval shape. Same contract as `whyClass`. */
export const EVAL_CLASS = Object.freeze({
  tested: "review-card__eval--tested",
  never_reproduced: "review-card__eval--never_reproduced",
  both_arms_failed: "review-card__eval--both_arms_failed",
  no_trial_ran: "review-card__eval--no_trial_ran",
  no_arms: "review-card__eval--no_arms",
  unknown_outcome: "review-card__eval--unknown_outcome",
});

export function evalClass(shape) {
  return EVAL_CLASS[shape] || "";
}

/** What each transcript source is CALLED, rather than its column value. */
export const SOURCE_LABEL = Object.freeze({
  claude: "Claude Code",
  codex: "Codex",
});

/** Name a source, and say plainly when we have no name for it. */
export function sourceLabel(source) {
  return SOURCE_LABEL[source] || `${source} (a source this dashboard has no name for)`;
}

/**
 * The window the evidence actually spans, as ISO dates labelled UTC.
 *
 * Deliberately NOT converted to the reader's local zone. The stamps are
 * transcript time in UTC; `%Y-%m-%d` formatting of a UTC instant in local time
 * silently shifts a date by one day either side of midnight, which is the
 * logical-time trap AGENTS.md records twice. Labelling the zone costs four
 * characters and removes the ambiguity.
 */
export function reviewWhen(prov) {
  const first = (prov && prov.first_seen) || "";
  const last = (prov && prov.last_seen) || "";
  if (!first && !last) return "";
  const day = (s) => String(s).slice(0, 10);
  if (first && last && day(first) !== day(last)) {
    return `${day(first)} to ${day(last)} UTC`;
  }
  return `${day(first || last)} UTC`;
}

/** Describe the recorded frequency, time range, and transcript sources. */
export function reviewEvidence(family) {
  const prov = family.provenance || {};
  const incidents = prov.incident_count || family.evidence_count || 0;
  const sessions = prov.session_count || 0;
  const when = reviewWhen(prov);
  const sources = (prov.sources || []).map(sourceLabel);
  const bits = [];
  bits.push(incidents === 1 ? "Seen once" : `Seen ${num(incidents)} separate times`);
  if (sessions) bits.push(sessions === 1 ? "in 1 session" : `across ${num(sessions)} sessions`);
  if (when) bits.push(when);
  let text = `${bits.join(", ")}.`;
  if (sources.length) {
    text += ` ${sources.length === 1 ? "Agent" : "Agents"}: ${sources.join(" and ")}.`;
  }
  if (prov.unknown_session_records) text += ` ${num(prov.unknown_session_records)} transcript references have incomplete native identity.`;
  if (prov.unknown_source_incidents) text += ` Source product is unknown for ${num(prov.unknown_source_incidents)} incidents.`;
  return text;
}

/**
 * Display the data layer's evaluation explanation and available trial artifacts.
 * A verdict alone does not identify whether a trial supplied informative evidence.
 */
export function renderEvalStory(family) {
  const story = family.eval || {};
  const means = family.means || "";
  if (!story.summary && !means) return "";
  const rows = [];
  // On a carve-out the POLICY is what holds this, not the verdict. Without
  // saying so the eval sentence sits directly under the lead line and reads
  // as the reason -- "no trial ran" looks like the thing to fix, when
  // approving it would still require a person however the eval had gone.
  if (family.has_carve_out && story.summary) {
    rows.push(
      `<p class="review-card__means">The eval below is context, not the ` +
        `reason. This needs a person whatever the verdict says.</p>`
    );
  }
  if (means) rows.push(`<p class="review-card__means">${esc(means)}</p>`);
  if (story.summary) {
    rows.push(
      `<p class="review-card__eval ${evalClass(story.shape)}">` +
        `${esc(story.summary)}</p>`
    );
  }
  if (family.evals_disagree) {
    rows.push(
      `<p class="review-card__eval-split">These proposals were gated ` +
        `separately and their verdicts disagree: ` +
        `${esc((family.eval_verdicts || []).join(", "))}. The headline above is ` +
        `the first one, not a summary of both.</p>`
    );
  }
  if (story.trials_dir) {
    rows.push(
      `<p class="footnote">Recorded trial artifacts are available at ` +
        `<code>${esc(story.trials_dir)}</code>. Inspect trial outputs and ` +
        `failure causes alongside the verdict.</p>`
    );
  }
  return rows.join("");
}

/** Which files this decision writes, and how many proposals collapse into each. */
export function renderTargetRows(family) {
  // `target_rows` carries the per-file multiplicity; `targets` is the older
  // deduplicated list and is still part of the payload. Falling back to it
  // means a card can never silently name NO file, which is what this rendered
  // when only `target_rows` was read and the payload predated it. The fallback
  // loses the counts, not the paths -- it is the same data, not a guess.
  let rows = family.target_rows || [];
  if (!rows.length) {
    rows = (family.targets || []).map((path) => ({ path, count: 1, kind: "", note: "" }));
  }
  if (!rows.length) return "";
  const body = rows
    .map(
      (row) =>
        `<li class="review-target">` +
        `<code class="review-target__path">${esc(row.path)}</code>` +
        (row.count > 1 ? `<span class="review-target__count">&times;${num(row.count)}</span>` : "") +
        (row.note ? `<span class="review-target__note">${esc(row.note)}</span>` : "") +
        `</li>`
    )
    .join("");
  const n = rows.length;
  return (
    `<details class="review-targets" open><summary class="review-targets__head">` +
    `Writes into ${n === 1 ? "1 file" : `${num(n)} files`} <span class="muted">· collapse / expand</span></summary>` +
    `<ul class="review-targets__list">${body}</ul></details>`
  );
}

/**
 * How full the target instruction file is.
 *
 * Only drawn for a target with a DECLARED budget
 * (`cfg.global_claude_md_line_budget`). An unreadable file renders its error
 * rather than an empty bar: a bar at 0/250 over a file nobody could read says
 * "plenty of room", which is the opposite of the truth.
 */
export function renderBudgetBar(budget) {
  if (!budget || !budget.path) return "";
  if (budget.error) {
    return (
      `<p class="review-budget review-budget--error">Line budget unknown: ` +
      `${esc(budget.error)}</p>`
    );
  }
  const used = Number(budget.used) || 0;
  const cap = Number(budget.budget) || 0;
  const pending = Number(budget.pending) || 0;
  const pct = cap ? Math.min(100, Math.round((used / cap) * 100)) : 0;
  const after = used + pending;
  const verdict = budget.over
    ? `approving this would put it over the ${num(cap)}-line budget`
    : `${num(after)} of ${num(cap)} after this one`;
  return (
    `<div class="review-budget${budget.over ? " review-budget--over" : ""}">` +
    `<span class="review-budget__label">Line budget</span>` +
    `<span class="review-budget__track"><span class="review-budget__fill" ` +
    `style="width:${pct}%"></span></span>` +
    `<span class="review-budget__text">${num(used)} of ${num(cap)} lines used &middot; ` +
    `${esc(verdict)}</span></div>`
  );
}

/**
 * Show retained incident examples and report the examples or characters omitted.
 * A bounded sample must not imply that it contains the complete evidence.
 */
export function renderIncidentExamples(family) {
  const prov = family.provenance || {};
  const examples = prov.examples || [];
  if (!examples.length) return "";
  const items = examples
    .map((ex) => {
      const value = incidentPrimary(ex);
      const cut = clamp(value.display_text, 240);
      return (
        `<li class="review-incident">` +
        `<span class="review-incident__meta">${esc(String(ex.ts).slice(0, 19))} UTC ` +
        `&middot; ${esc(ex.signal_type || "")} &middot; ` +
        `<code>${esc(ex.project_path || "")}</code></span>` +
        `<pre class="review-incident__text">${esc(cut.text)}</pre>` +
        (value.fingerprint ? `<p class="footnote">Detector fingerprint: <code>${esc(value.fingerprint)}</code></p>` : "") +
        renderSourceIdentity(ex.presentation?.identity,{prefix:"review-example:"+family.learning_id+":"+ex.id}) + renderOccurrenceSummary(value) +
        (value.display_text_truncated ? `<p class="footnote">${num(value.display_text_truncated.cut_chars)} additional characters omitted by the summary.</p>` : "") +
        `<p><a href="${esc(evidenceHref("incident", ex.id, {mode:"evidence",tab:"source"}))}">Inspect complete incident evidence</a></p>` +
        (cut.cut ? `<p class="footnote">Cut here: ${num(cut.cut)} more characters.</p>` : "") +
        `</li>`
      );
    })
    .join("");
  const held = prov.examples_held_back || 0;
  const note = held
    ? `<p class="footnote">Showing the ${num(examples.length)} most recent. ` +
      `${num(held)} more ${held === 1 ? "incident is" : "incidents are"} behind this lesson.</p>`
    : "";
  return (
    `<details class="review-disclose" ${reviewDisclosure("incident-examples:" + family.learning_id)}><summary id="review-incident-examples:${esc(family.learning_id)}">What actually happened ` +
    `(${num(examples.length)} of ${num(prov.incident_count || examples.length)})</summary>` +
    `<ul class="review-incidents">${items}</ul>${note}</details>`
  );
}

/** The complete diff stays available in the product, behind a disclosure. */
export function renderReviewDiff(diff, key = "", label = "Complete proposed edit") {
  if (!diff) return "";
  return (
    `<details class="review-disclose" ${reviewDisclosure(key)}><summary>${esc(label)}</summary>` +
    `<pre class="review-card__diff">${renderUnifiedDiff(diff)}</pre></details>`
  );
}

function reviewDisclosure(key, open = false) {
  if (!key) return "";
  const shown = key in state.reviewDisclosures ? state.reviewDisclosures[key] : open;
  return `data-review-key="${esc(key)}" ${shown ? "open" : ""}`;
}

export function selectedReviewProposals(family) {
  return (family.proposals || []).filter((p) => !state.reviewExcluded[p.id]);
}

function reviewSignature(proposals) {
  return JSON.stringify(proposals.map((p) => [p.id, reviewBinding(p)]).sort((a, b) => a[0].localeCompare(b[0])));
}

function reviewBinding(proposal, member = proposal) {
  return proposal.content_revision !== undefined ? member.content_revision : member.revision;
}

function reviewPreviewCurrent(family, entry) {
  return state.reviewPreviews[family.learning_id] === entry &&
    entry.signature === reviewSignature(selectedReviewProposals(family));
}

/** Validate supplemental labels without making them an approval fingerprint. */
export async function validateReviewEvidenceIdentity(preview) {
  const packet = preview.evidence_identity;
  if (!packet) return null;
  if (packet.profile !== "review-evidence/1" || packet.preview_revision !== preview.revision || !Array.isArray(packet.records))
    throw new Error("Identity labels do not match this selected preview.");
  const sources = new Map();
  for (const member of preview.members) for (const incident of member.snapshot.evidence) {
    const encoded = JSON.stringify(Object.fromEntries(Object.keys(incident).sort().map(key=>[key,incident[key]])));
    if (sources.has(incident.id) && sources.get(incident.id) !== encoded) throw new Error("Selected incident sources disagree.");
    sources.set(incident.id,encoded);
  }
  const records = new Map();
  for (const record of packet.records) {
    if (!sources.has(record.incident_id) || records.has(record.incident_id)) throw new Error("Identity labels contain another or duplicate incident.");
    const bytes = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(sources.get(record.incident_id)));
    const hash = Array.from(new Uint8Array(bytes),n=>n.toString(16).padStart(2,"0")).join("");
    if (hash !== record.source_revision || !record.identity?.project || !record.identity?.session)
      throw new Error("Identity labels do not match the retained incident source.");
    records.set(record.incident_id,record.identity);
  }
  if (records.size !== sources.size) throw new Error("Some selected incident identities were not returned.");
  return Object.fromEntries(records);
}

export async function loadReviewPreview(family) {
  const selected = selectedReviewProposals(family);
  const signature = reviewSignature(selected);
  const previous = state.reviewPreviews[family.learning_id];
  if (previous && previous.signature === signature) return previous.promise || previous;
  const entry = { signature, loading: true, data: null, error: "" };
  state.reviewPreviews[family.learning_id] = entry;
  entry.promise = (async () => {
    try {
      if (!selected.length) throw new Error("Select at least one proposal to preview.");
      const data = await getJSON(API.reviewPreview + "?proposal_ids=" + encodeURIComponent(selected.map((p) => p.id).sort().join(",")));
      if (!reviewPreviewCurrent(family, entry)) throw new Error("The selection or Review queue changed. Review the current preview before deciding.");
      const actual = data.members.map((m) => {
        const proposal = selected.find((p) => p.id === m.proposal_id);
        return [m.proposal_id, proposal ? reviewBinding(proposal, m) : null];
      }).sort((a, b) => a[0].localeCompare(b[0]));
      if (JSON.stringify(actual) !== signature) throw new Error("A proposal changed. Reload Review before deciding.");
      try {entry.identities = await validateReviewEvidenceIdentity(data);}
      catch(error) {entry.identityError = String(error.message || error);}
      if (!reviewPreviewCurrent(family, entry)) throw new Error("The selection or Review queue changed. Review the current preview before deciding.");
      entry.data = data;
      data.members.forEach((m) => { state.reviewMembers[m.proposal_id] = m; });
    } catch (error) {
      entry.error = String(error && error.message ? error.message : error);
      entry.errorDetail = error?.readDetail || "";
    } finally {
      entry.loading = false;
      entry.promise = null;
      if (reviewPreviewCurrent(family, entry) && state.route === "review" && state.selectedFamily === family.learning_id) paintReview();
    }
    return entry;
  })();
  return entry.promise;
}

function fullRecord(value) {
  return `<pre class="review-full-record">${esc(JSON.stringify(value, null, 2))}</pre>`;
}

/** Concise read failure with the original, complete diagnostic kept inspectable. */
function readFailure(entry, {key, title, guidance, summaryId}) {
  const detail = entry.errorDetail || entry.error;
  const summary = detail.length > 240 ? detail.slice(0, 240) + "…" : detail;
  const explanation = /[.!?…]$/.test(summary) ? summary : summary + ".";
  return `<p class="error-state" role="alert">${esc(title)} ${esc(explanation)} ${esc(guidance)}</p>` +
    `<details class="review-disclose" ${reviewDisclosure("read-error:" + key)}><summary id="${esc(summaryId || "read-error-" + key)}">Request details</summary>` +
    `<pre class="review-full-record">${esc(entry.error)}</pre></details>`;
}

export function renderRetainedEvidence(incident, prefix = "") {
  let archive, error = incidentPrimary(incident).archive_error || "";
  try {
    archive = JSON.parse(incident.window_json);
    if (!Array.isArray(archive)) throw new Error("The retained context is not a list.");
  } catch (exc) { error = String(exc.message || exc); }
  const context = error
    ? `<p class="error-state">Retained context is unreadable: ${esc(error)}</p>${fullRecord(incident.window_json)}`
    : archive.length ? archive.map((entry, index) => {
      if (!entry || typeof entry !== "object") return fullRecord(entry);
      const label = entry.role || "Retained occurrence";
      return `<section class="review-evidence-entry"><p class="caption strong">${esc(label)}</p>` +
        ("count_in_session" in entry || "session_file" in entry ? `<p>${entry.count_in_session == null ? "Count unknown" : num(entry.count_in_session) + " occurrences"} · ${esc(entry.ts || "Time unknown")} · <code>${esc(entry.session_file || "Session path unknown")}</code> · <code>${esc(entry.project_path || "Project location unknown")}</code></p>` : "") +
        (typeof entry.text === "string" ? `<pre class="review-full-record">${esc(entry.text)}</pre>` : "") +
        `<details ${reviewDisclosure(prefix + incident.id + ":entry:" + index)}><summary id="review-record-${esc(prefix + incident.id + ":" + index)}">Full retained record</summary>${fullRecord(entry)}</details></section>`;
    }).join("") : `<p class="muted">No context was retained for this incident.</p>`;
  return `<details class="review-disclose" ${reviewDisclosure(prefix + incident.id)}><summary id="review-retained-${esc(prefix + incident.id)}">${esc(incident.signal_type)} · ${esc(incident.ts || "Time unknown")} · ${esc(incident.session_id || "Session unknown")}</summary>` +
    `<p class="caption">Incident ${esc(incident.id)} · <code>${esc(incident.project_path || "Project path unknown")}</code></p>` +
    renderIncidentPrimary(incident) + `<details ${reviewDisclosure(prefix + incident.id + ":full")}><summary id="review-full-incident-${esc(prefix + incident.id)}">Complete incident record</summary>${fullRecord(incident)}</details>` + context + renderScanHistory(incident.id, "review-" + prefix) + `</details>`;
}

export function renderReviewMembers(family) {
  if (!state.reviewIndividual[family.learning_id]) return "";
  return `<section class="review-member-list" aria-label="Individual proposals">` + (family.proposals || []).map((p, i) => {
    const saved = state.reviewMembers[p.id];
    const snapshot = saved && reviewBinding(p, saved) === reviewBinding(p) ? saved.snapshot : null;
    const evidence = snapshot ? snapshot.evidence : [];
    return `<article class="review-member"><div class="review-member__head">` +
      `<label><input id="review-include-${esc(p.id)}" type="checkbox" data-select-proposal="${esc(p.id)}" data-learning-id="${esc(family.learning_id)}" ${state.reviewExcluded[p.id] ? "" : "checked"}> Include proposal ${i + 1}</label>` +
      `<span class="caption">${esc(p.status)} · ${esc(p.action)}</span></div>` +
      `<p class="caption">${esc(p.why_needs_you)}</p><code>${esc(p.target_path)}</code>` +
      renderReviewDiff(p.diff_unified, p.id + ':diff') +
      (snapshot && snapshot.resolution ? `<p><strong>Proposed rollback resolution:</strong> ${esc(snapshot.resolution.explanation)}</p><details ${reviewDisclosure(p.id + ":resolution")}><summary>Original application and current conflict</summary>${fullRecord(snapshot.resolution.rollback)}</details>` : "") +
      (snapshot && snapshot.reapplication ? `<p><strong>Reapplication:</strong> This is a new proposal. The original rollback remains in history.</p><details ${reviewDisclosure(p.id + ":reapplication")}><summary>Original application and completed rollback</summary>${fullRecord(snapshot.reapplication)}</details>` : "") +
      renderEvalStory({ eval: p.eval || {}, means: "" }) +
      `<button class="btn" data-eval-preview="${esc(p.id)}">Regenerate evaluation…</button>` +
      `<p><a class="review-link" href="${esc(recoveryLink(family.learning_id,"hook","",[p.id]))}">Request a hook proposal…</a> · <a class="review-link" href="${esc(recoveryLink(family.learning_id,"correct_target","",[p.id]))}">Request a corrected target…</a></p>` +
      (snapshot && snapshot.recovery ? `<p>${esc(snapshot.recovery.explanation)}</p><details><summary>Recovery origin and generated patch</summary>${fullRecord(snapshot.recovery)}</details>` : "") +
      (snapshot ? `<details class="review-disclose" ${reviewDisclosure(p.id + ':eval')}><summary>Full eval record</summary>` +
        (snapshot.evaluation ? fullRecord(snapshot.evaluation) : `<p class="muted">No eval result is linked to this proposal.</p>`) + `</details>` +
        `<details class="review-disclose" ${reviewDisclosure(p.id + ':evidence')}><summary>All retained evidence (${evidence.length})</summary>` +
        (evidence.length ? evidence.map((e) => renderRetainedEvidence(e, p.id + ':')).join("") : `<p class="muted">No incidents are linked to this proposal's lesson.</p>`) + `</details>`
        : `<p class="muted">Full evidence loads with the selected preview.</p>`) + `</article>`;
  }).join("") + `</section>`;
}

/** Supplemental provenance is bound only to the selected preview's result IDs. */
export function reviewDetailMembers(family) {
  const entry = state.reviewPreviews[family.learning_id];
  return entry && !entry.loading && !entry.error && entry.data && reviewPreviewCurrent(family, entry)
    ? entry.data.members : [];
}
export async function loadReviewEvaluation(id, {refresh = false} = {}) {
  if (!refresh && state.reviewEvaluations[id]) return;
  const entry = {loading: true}; state.reviewEvaluations[id] = entry;
  try {
    entry.result = await getJSON(API.evalResults + "/" + encodeURIComponent(id));
    if (entry.result.record.id !== id) throw new Error("The evaluation reader returned a different result.");
    entry.attempts = await Promise.all((entry.result.attempt_links || []).map(async link => {
      const data = await getJSON(API.evalAttempts + "/" + encodeURIComponent(link.attempt_id));
      const scenario = data.scenarios.find(s => s.scenario === link.scenario);
      if (data.source.id !== link.attempt_id || scenario?.evaluation?.id !== id)
        throw new Error("The retained attempt does not match this exact evaluation result.");
      return {data, scenario};
    }));
  } catch(error) {entry.error = String(error.message || error);entry.errorDetail = error?.readDetail || "";}
  finally {
    entry.loading = false;
    const family = state.review?.families?.find(f=>f.learning_id === state.reviewDetailFamily);
    if (state.reviewEvaluations[id] === entry && state.route === "review" && family && reviewDetailMembers(family).some(m=>m.snapshot.evaluation?.id===id)) paintReview();
  }
}
function renderReviewEvaluation(id, members, index, count) {
  const entry = state.reviewEvaluations[id];
  const title = `Result ${index + 1} of ${count} · ${esc(members[0].snapshot.evaluation.verdict || "Unknown")} · ${members.length} selected proposal${members.length === 1 ? "" : "s"}`;
  let html = `<article class="review-detail-evaluation" id="review-evaluation-${esc(id)}" tabindex="-1">`;
  if (!entry || entry.loading) return html + `<p><strong>${title}</strong></p><p role="status">Reading exact evaluation provenance…</p></article>`;
  if (entry.error) return html + `<p><strong>${title}</strong></p>` + readFailure(entry, {key:"evaluation:" + id, title:"Could not read evaluation evidence.", guidance:"Retry evaluation evidence below."}) + `<button id="review-evaluation-retry-${esc(id)}" class="btn" data-review-evaluation-retry="${esc(id)}">Retry evaluation evidence</button></article>`;
  const sameRecord = value => JSON.stringify(Object.entries(value || {}).sort(([a],[b])=>a.localeCompare(b)));
  if (members.some(m=>sameRecord(m.snapshot.evaluation)!==sameRecord(entry.result.record)))
    return html + `<p><strong>${title}</strong></p><p role="alert">Evaluation evidence changed since the selected preview. Reload Review before using these results.</p><button id="review-evaluation-stale-${esc(id)}" class="btn" data-reload-review="true">Reload Review</button></article>`;
  const comparison = !entry.attempts.length ? "Paired comparison unknown · no exact attempt link" : entry.attempts.map(({scenario})=>scenario.comparison.computable ? `Scenario ${scenario.scenario + 1}: comparable paired trials` : `Scenario ${scenario.scenario + 1}: paired change unavailable · ${scenario.comparison.reason}`).join("; ");
  html += `<details class="review-evaluation-body" ${reviewDisclosure("review-evaluation:" + id,index === 0)}><summary id="review-evaluation-summary-${esc(id)}"><strong>${title}</strong><span class="review-evaluation-status">${esc(comparison)}</span></summary><details ${reviewDisclosure("review-eval-members:" + id)}><summary id="review-eval-members-${esc(id)}">${members.length} selected proposal${members.length === 1 ? "" : "s"} · exact result and members</summary><p class="caption">Result <code>${esc(id)}</code> · ${members.map(m=>`<code>${esc(m.proposal_id)}</code>`).join(", ")}</p></details>`;
  if (!entry.attempts.length) html += `<p>No exact attempt link was retained. Paired comparison and producing calls are unknown.</p><p>Historical reported trials: ${num(entry.result.summary?.story?.succeeded)} passed / ${num(entry.result.summary?.story?.attempted)} attempted. These unverified counts may include infrastructure failures.</p><a id="review-evaluation-legacy-${esc(id)}" href="${esc(evaluationHref("unlinked",id,""))}">Inspect complete historical result</a>`;
  for (const {data, scenario} of entry.attempts) {
    const index = scenario.scenario, key = id + ":" + data.source.id + ":" + index;
    html += `<h3>Scenario ${index + 1} of ${data.scenarios.length}${scenario.specification ? " · " + esc(scenario.specification.spec.title) : ""}</h3>`;
    for (const arm of ["without", "with"]) {
      const value = scenario.arms[arm];
      html += `<div class="review-detail-arm"><h4>${arm === "with" ? "With" : "Without"} the rule · ${num(value.observed_passes)} / ${num(value.completed)} observed passes</h4><p class="caption">${num(value.valid_trials)} / ${num(value.requested_trials)} valid planned trials · ${esc(value.served_models.map(m=>m.provider + " / " + m.model).join("; ") || "Served model unknown")}</p>`;
      const events = data.events.filter(e=>e.scenario_index === index && e.arm === arm && ["trial_result","trial_failed"].includes(e.kind));
      html += events.length ? `<ul class="review-detail-trials">` + events.map(e=>`<li><details ${reviewDisclosure("review-trial:" + key + ":" + e.id)}><summary id="review-trial-${esc(e.id)}">Trial ${e.trial_index + 1}: ${esc((e.data.outcome || e.data.code || "Interrupted").replaceAll("_"," "))}</summary>${fullRecord(e.data)}</details></li>`).join("") + `</ul>` : `<p>No trial result retained${value.skipped ? " · " + esc(value.skipped.reason) : ""}.</p>`;
      html += `</div>`;
    }
    html += `<p class="caption">${scenario.comparison.computable ? "Comparable paired trials for this scenario. " + num(scenario.arms.with.valid_passes) + " / " + num(scenario.arms.with.valid_trials) + " passed with the rule; " + num(scenario.arms.without.valid_passes) + " / " + num(scenario.arms.without.valid_trials) + " without it." : "Paired change unavailable · " + esc(scenario.comparison.reason) + ". Missing results are not zero."}</p>`;
    html += `<details ${reviewDisclosure("review-eval-source:" + key)}><summary id="review-eval-source-${esc(key)}">Producing run, specification and source</summary>${fullRecord({source:data.source,specification:scenario.specification})}</details><p class="caption">Retained evaluation source may differ from the current proposal.</p><a id="review-attempt-${esc(key)}" href="${esc(evaluationHref("attempt",data.source.id,""))}">All scenarios, transcripts and provenance</a>`;
  }
  return html + `</details></article>`;
}
export function renderReviewDetail(family) {
  const members = reviewDetailMembers(family), preview = state.reviewPreviews[family.learning_id];
  const evidence = [...new Map(members.flatMap(m=>m.snapshot.evidence || []).map(e=>[e.id,e])).values()];
  const page = Math.max(0, Math.min(state.reviewEvidencePages[family.learning_id] || 0, Math.ceil(evidence.length / 3) - 1));
  const evaluations = [...new Set(members.map(m=>m.snapshot.evaluation?.id).filter(Boolean))];
  const missing = members.filter(m=>!m.snapshot.evaluation).length;
  const panel = (title, body, name) => `<section class="review-detail-panel" data-review-panel="${name}"><h2>${title}</h2>${body}</section>`;
  const evidenceBody = !members.length ? `<p>Evidence loads with a valid selected preview.</p>` :
    `<p class="caption">${num(evidence.length)} retained incidents in this selection. Excerpts are redacted retained text. Source labels come from retained index metadata, not model telemetry or runtime loading.</p>` +
    (preview.identityError ? `<p role="alert">${esc(preview.identityError)} Reload Review to refresh source labels.</p><button id="review-identity-reload" class="btn" data-reload-review="true">Reload Review</button>` : "") +
    (evidence.length ? evidence.slice(page*3,page*3+3).map(e=>`<article class="review-detail-incident">${renderIncidentPrimary(e)}<p class="caption">${esc(e.signal_type)} · ${esc(e.ts || "Time unknown")} · session <code>${esc(e.session_id || "unknown")}</code> · <code>${esc(e.project_path || "Project unknown")}</code></p>${preview.identities?.[e.id] ? renderSourceIdentity(preview.identities[e.id],{prefix:"review-identity:"+e.id}) : `<p>Repository and source provider labels are unavailable for this selected preview.</p>`}<a id="review-incident-${esc(e.id)}" href="${esc(evidenceHref("incident",e.id))}">Inspect incident and source</a>${renderRetainedEvidence(e,"detail:")}</article>`).join("") : `<p>No incidents are linked to this selection.</p>`) +
    (evidence.length>3 ? `<nav class="review-detail-pages" aria-label="Incident pages"><button id="review-evidence-prev" class="btn" data-review-evidence-page="${page-1}" ${page===0 ? "disabled" : ""}>Previous incidents</button><span id="review-evidence-page" tabindex="-1">${page*3+1}–${Math.min(page*3+3,evidence.length)} of ${evidence.length}</span><button id="review-evidence-next" class="btn" data-review-evidence-page="${page+1}" ${(page+1)*3>=evidence.length ? "disabled" : ""}>Next incidents</button></nav>` : "");
  const evalBody = (missing ? `<p>${missing} selected proposal${missing===1?" has":"s have"} no linked evaluation. No tested benefit is established.</p>` : "") +
    (evaluations.length ? `<p class="caption">${evaluations.length} evaluation result${evaluations.length === 1 ? "" : "s"} · selected proposal order. Verdicts are retained from the selected preview; expand each result for complete evidence.</p>` + evaluations.map((id,index)=>renderReviewEvaluation(id,members.filter(m=>m.snapshot.evaluation?.id===id),index,evaluations.length)).join("") : !members.length ? `<p>Evaluation sources load with a valid selected preview.</p>` : "");
  return `<article class="review-detail review-card--selected" data-learning-id="${esc(family.learning_id)}" tabindex="-1"><header class="review-detail-head"><h1 class="view__title">${mdLite(family.rule_text)}</h1><p>Full review · ${selectedReviewProposals(family).length} selected proposals · ${characterHint("<kbd>j</kbd> / <kbd>k</kbd> moves between families · ")}<kbd>Esc</kbd> returns to queue.</p><p class="caption">Why this needs you: ${esc(family.lead_reason)}</p></header><div class="review-detail-grid"><div class="review-detail-main">` +
    panel("Complete selected edit", `<p class="caption">Approval binds these exact members, revisions and base. A changed target requires a fresh preview.</p>` + renderSelectedPreview(family,{full:true}) + renderProposalBreakdown(family) + `<details ${reviewDisclosure("detail-revisions:"+family.learning_id)}><summary id="review-detail-revisions">Selected member revisions and base</summary>${fullRecord(preview?.data ? {revision:preview.data.revision,members:members.map(m=>({proposal_id:m.proposal_id,revision:m.revision})),targets:preview.data.targets} : null)}</details><p class="caption">Delivery markers identify recorded contributions; they do not prove authorship, runtime loading or receipt.</p>` + renderReviewMembers(family),"edits") + panel("Evidence", evidenceBody,"evidence") + `</div><aside class="review-detail-side">` +
    panel("Evaluation evidence",evalBody,"evaluations") + panel("Your decision", `<p>${preview?.data?.ready && members.length ? "Selected edit ready for review." : "A valid, ready selection is required for approval."} Approval records intent; a separate worker delivers it.</p>` + renderReviewActions(family,{full:true}),"decision") + `</aside></div></article>`;
}

export function renderSelectedPreview(family, {full = false} = {}) {
  const entry = state.reviewPreviews[family.learning_id];
  if (!entry || entry.loading) return `<p class="caption muted" role="status">Reading the selected edits and destinations…</p>`;
  if (entry.error) return readFailure(entry, {key:"preview:" + family.learning_id, title:"Could not read the selected preview.", guidance:"Reload Review before deciding."}) + `<button class="btn" data-reload-review="true">Reload Review</button>`;
  return `<section class="review-selected-preview" aria-label="Selected edit preview"><h4>${entry.data.members.length} selected ${entry.data.members.length === 1 ? 'proposal' : 'proposals'} · ${entry.data.targets.length} ${entry.data.targets.length === 1 ? 'destination' : 'destinations'}</h4>` +
    entry.data.targets.map((target, index) => {
      const dest = target.destination;
      const budget = target.budget;
      const destination = dest.mode === "git_branch"
        ? `Branch ${dest.branch_name} · ${dest.repo_root} · ${dest.relative_path}. Integration into your working copy is a separate step.`
        : `Direct file delivery.`;
      const status = target.state === "ready" ? "Ready" : target.state === "conflict" ? "Conflict" : "Needs attention";
      const budgetText = budget ? `<span class="review-budget${budget.over ? " review-budget--over" : ""}">${budget.before} lines now → ${budget.after} after this selection · ${budget.limit}-line budget.` +
        (budget.over ? ` Above the budget; approval remains available.` : ``) + `</span>` : "";
      return `<details class="review-targets" ${reviewDisclosure((full ? 'detail:' : '') + family.learning_id + ':' + target.target_key, full && index === 0)}><summary class="review-targets__head" id="review-target-${esc(target.target_key)}"><span class="review-target-path">${esc(dest.target_path)}</span><span class="review-target-meta">${esc(status)} · ${target.proposal_ids.length} proposal${target.proposal_ids.length === 1 ? "" : "s"}</span>${budgetText}</summary>` +
        `<p class="caption">${esc(destination)}</p>` +
        (target.state === "ready" ? `<details class="review-disclose" ${reviewDisclosure((full ? "detail:" : "") + family.learning_id + ":combined:" + target.target_key, full && index === 0)}><summary id="review-combined-${esc(target.target_key)}">Complete combined edit</summary><pre class="review-card__diff">${renderUnifiedDiff(target.diff_unified)}</pre></details>`
          : `<p class="error-state">${esc(target.detail)}</p><p class="caption">Review individually to select one existing alternative, or <a class="review-link" href="${esc(recoveryLink(family.learning_id,dest.target_kind === "hook" ? "hook" : "regenerate_patch",target.target_key,target.proposal_ids))}">${dest.target_kind === "hook" ? "generate this hook proposal…" : "regenerate these selected patches…"}</a></p>`) + `</details>`;
    }).join("") + `</section>`;
}

/** Buttons, labelled with what they DO rather than with a verb. */
export function renderReviewActions(family, {full = false} = {}) {
  const id = esc(family.learning_id);
  const busy = Boolean(state.deciding[family.learning_id]);
  const size = (family.proposals || []).length;
  const files = (family.target_rows || []).length;
  const approve =
    size > 1 && files >= 1
      ? `Approve &mdash; write once per file`
      : `Approve`;
  const selected = selectedReviewProposals(family).length;
  const entry = state.reviewPreviews[family.learning_id];
  const ready = entry && !entry.loading && entry.data && entry.data.ready && entry.signature === reviewSignature(selectedReviewProposals(family));
  const individual = `<button type="button" class="btn" data-review-individual="${id}" aria-expanded="${Boolean(state.reviewIndividual[family.learning_id])}">${state.reviewIndividual[family.learning_id] ? "Close individual review" : `Review the ${size} individually`}</button>`;
  return (
    `<div class="review-card__actions${full ? " review-detail-actions" : ""}">` +
    `<button type="button" class="btn btn--approve btn--primary" data-decision="approve" ` +
    `data-learning-id="${id}" ${characterKey("a")} ${ready && !busy ? "" : "disabled"}>${!full && selected === size ? approve : `Approve selected (${selected})`} ${characterHint('<kbd class="kbd" aria-hidden="true">a</kbd>')}</button>` +
    (full ? "" : individual) +
    `<button type="button" class="btn btn--reject" data-decision="reject" ` +
    `data-learning-id="${id}" ${characterKey("r")} ${!busy && entry && !entry.loading && entry.data && !entry.error && selected ? "" : "disabled"}>Reject at selected targets ${characterHint('<kbd class="kbd" aria-hidden="true">r</kbd>')}</button>` +
    `<button type="button" class="btn btn--reject" data-decision="reject_lesson" ` +
    `data-learning-id="${id}" ${characterKey("Shift+r")} ${!busy && entry && !entry.loading && entry.data && !entry.error && selected ? "" : "disabled"}>Reject lesson everywhere ${characterHint('<kbd class="kbd" aria-hidden="true">⇧r</kbd>')}</button>` +
    (full ? individual : "") +
    `</div>` +
    (busy ? `<p role="status">Processing this decision and refreshing Review…</p>` : "") +
    `<p class="footnote" data-character-hint${state.characterShortcuts.enabled ? "" : ' hidden style="display:none"'}>Shortcuts for the selected reviewed targets: <kbd class="kbd">a</kbd> approves; <kbd class="kbd">r</kbd> rejects at those targets. <kbd class="kbd">Shift+r</kbd> rejects the lesson everywhere.</p>` +
    `<p class="footnote review-card__consequence">Approving records your ` +
    `decision and queues the selected edits for delivery. Reject selected targets permanently ` +
    `suppresses this lesson at those files, including sibling clones; other targets stay available. ` +
    `Reject lesson everywhere also suppresses unselected and future targets. Existing applied edits remain.</p>`
  );
}

/**
 * Show individual proposals when their reasons differ or some have been decided.
 * A family-level summary cannot identify a member with a different outcome.
 */
export function renderProposalBreakdown(family, options) {
  const opts = options || {};
  const proposals = family.proposals || [];
  const reasons = new Set(proposals.map((p) => p.why_needs_you));
  const anyDecided = proposals.some((p) => opts.decided && opts.decided[p.id]);
  if (reasons.size <= 1 && !anyDecided) return "";
  // Print the target here only when the proposals DISAGREE about it. When they
  // all write one file the targets table above already said so, and repeating
  // it per proposal printed the same path three times on the commonest card.
  const manyPaths = new Set(proposals.map((p) => p.target_path)).size > 1;
  const rows = proposals
    .map((proposal) => {
      const mark = opts.decided && opts.decided[proposal.id];
      return (
        `<li class="review-proposal">` +
        `<span class="review-proposal__why">${esc(proposal.why_needs_you)}</span>` +
        (manyPaths ? ` <code>${esc(proposal.target_path)}</code>` : "") +
        (mark ? ` <span class="badge">${esc(mark)}</span>` : "") +
        `</li>`
      );
    })
    .join("");
  const why = anyDecided
    ? "Each proposal in this lesson, and what happened to it:"
    : "These proposals do not share one reason, so each is listed:";
  return (
    `<details class="review-breakdown" ${reviewDisclosure(family.learning_id + ":reasons")}><summary id="review-reasons-${esc(family.learning_id)}">Review reasons for all ${proposals.length} proposals</summary><p class="caption muted">${esc(why)}</p>` +
    `<ul class="review-card__proposals">${rows}</ul></details>`
  );
}

/** One family, opened. */
export function renderReviewCard(family, options) {
  const opts = options || {};
  const proposals = family.proposals || [];
  return (
    `<article class="review-card review-card--open review-card--selected" ` +
    `data-learning-id="${esc(family.learning_id)}" tabindex="-1">` +
    `<p class="review-card__why ${whyClass(family.proposals[0].reason_code)}">` +
    `<span class="review-card__why-label">Why this needs you:</span> ` +
    `${esc(family.lead_reason)}</p>` +
    `<div class="review-card__body">` +
    `<h3 class="review-card__rule">${mdLite(family.rule_text)}</h3>` +
    `<p class="review-card__evidence">${esc(reviewEvidence(family))}</p>` +
    (family.incident_summary
      ? `<p class="review-card__summary">${esc(family.incident_summary)}</p>`
      : "") +
    renderEvalStory(family) +
    (state.reviewPreviews[family.learning_id]?.data ? "" : renderTargetRows(family)) +
    renderProposalBreakdown(family, opts) +
    renderIncidentExamples(family) +
    renderSelectedPreview(family) +
    renderReviewMembers(family) +
    renderReviewActions(family) +
    `<p class="review-full-link"><a id="review-open-${esc(family.learning_id)}" href="#/review/family/${encodeURIComponent(family.learning_id)}" ${characterKey("o")}>Open full review ${characterHint('<kbd class="kbd" aria-hidden="true">o</kbd>')}</a></p>` +
    `</div></article>`
  );
}

/** One family, closed: enough to choose it, nothing more. */
export function renderReviewRow(family, options) {
  const opts = options || {};
  const targets = family.target_rows || [];
  const where = targets.length === 1 ? targets[0].path : `${num(targets.length)} files`;
  const size = (family.proposals || []).length;
  return (
    `<li class="review-row${opts.selectedId === family.learning_id ? " review-row--selected" : ""}" ` +
    `data-learning-id="${esc(family.learning_id)}">` +
    `<button type="button" class="review-row__open" data-open-family="${esc(family.learning_id)}">` +
    `<span class="review-row__rule">${esc(ruleOneLine(family.rule_text))}</span>` +
    `<span class="review-row__meta"><code>${esc(where)}</code> &middot; ` +
    `${size === 1 ? "1 proposal" : `${num(size)} proposals`} &middot; ` +
    `${esc(family.lead_reason)}</span></button></li>`
  );
}

/** First sentence of a rule, for a one-line row. */
export function ruleOneLine(text) {
  const flat = String(text || "").replace(/\s+/g, " ").replace(/\*\*/g, "").trim();
  const stop = flat.search(/[.;:]\s/);
  const head = stop > 30 ? flat.slice(0, stop + 1) : flat;
  const cut = clamp(head, 110);
  if (!cut.cut) return cut.text;
  // Preserve a complete final word and mark the omitted text.
  const space = cut.text.lastIndexOf(" ");
  const body = space > 60 ? cut.text.slice(0, space) : cut.text;
  return body.replace(/[\s,;:\u2014-]+$/, "") + "\u2026";
}

/** What each carve-out action is called in English. */
export const CARVE_OUT_LABEL = Object.freeze({
  add: "instruction addition",
  edit: "instruction edit",
  delete: "instruction deletion",
  new_skill: "new skill",
  new_rule_file: "new rule file",
  convert_to_hook: "hook install",
  delete_human_line: "human-authored deletion",
  resolve_rollback: "rollback conflict resolution",
  reapply: "reapplication",
  recover_rule: "recovery proposal",
});

export function carveOutLabel(action, count) {
  const name = CARVE_OUT_LABEL[action];
  if (!name) return `${action} (an action this dashboard has no name for)`;
  return count === 1 ? name : `${name}s`;
}

/** The carve-out panel: what can never auto-apply, and whether any is waiting. */
export function renderCarveOuts(data) {
  const summary = (data && data.carve_out_summary) || {};
  const names = Object.keys(summary);
  if (!names.length) return "";
  const total = names.reduce((sum, k) => sum + (summary[k] || 0), 0);
  const items = names
    .map(
      (action) =>
        `<li><span class="dot"></span>${num(summary[action])} ` +
        `${esc(carveOutLabel(action, summary[action]))} waiting</li>`
    )
    .join("");
  return (
    `<aside class="review-carveouts"><h3>${total ? "Carve-outs waiting" : "Nothing here needs a carve-out"}</h3>` +
    `<p>These changes always require your review: installing or changing a hook, ` +
    `deleting a line you wrote yourself, generating a recovery proposal, resolving a rollback conflict, and reapplying a rolled-back change.</p>` +
    `<ul class="review-carveouts__list">${items}</ul></aside>`
  );
}

/**
 * Open one family and show the remaining families as compact rows.
 * Evidence, selected edits, and complete diffs remain available in disclosures.
 */
export function renderReviewQueue(data, options) {
  if (!data) return emptyState("The review queue has not loaded", "", "");
  const families = data.families || [];
  if (!families.length) {
    return `<div data-review-empty="true" tabindex="-1">${emptyState("Nothing needs you", esc(data.empty_state || ""), "")}</div>`;
  }
  const opts = options || {};
  const selectedId = opts.selectedId || families[0].learning_id;
  const open = families.find((f) => f.learning_id === selectedId) || families[0];
  const rest = families.filter((f) => f.learning_id !== open.learning_id);
  const nextUp = rest.length
    ? `<section class="review-next"><h3>Next up</h3>` +
      `<p class="caption muted">${num(rest.length)} more ` +
      `${rest.length === 1 ? "decision" : "decisions"}. ` +
      `${characterHint("Press <kbd>j</kbd> / <kbd>k</kbd> to move, or ")}click one.</p>` +
      `<ul class="review-next__list">` +
      rest.map((f) => renderReviewRow(f, { selectedId })).join("") +
      `</ul></section>`
    : "";
  return (
    `<div class="review-layout">` +
    renderReviewCard(open, opts) +
    `<div class="review-lower">${nextUp}${renderCarveOuts(data)}</div>` +
    `</div>`
  );
}

/** The families currently on screen, in the order they are rendered. */
export function reviewOrder() {
  const data = state.review;
  return data && data.families ? data.families.map((f) => f.learning_id) : [];
}

/**
 * Scroll and focus the selected card after keyboard navigation.
 * Nearest-edge scrolling leaves an already visible card in place.
 */
export function revealSelectedCard() {
  const d = doc();
  if (!d || typeof d.querySelector !== "function") return false;
  const card = d.querySelector(".review-card--selected") || d.querySelector("[data-review-empty]");
  if (!card) return false;
  if (state.reviewDetailFamily && card.classList?.contains("review-detail")) {
    byId("main").scrollTop = 0;
  } else if (typeof card.scrollIntoView === "function") {
    card.scrollIntoView({ block: "nearest" });
  }
  // preventScroll: scrollIntoView above already chose the position; letting
  // focus() scroll again undoes "nearest" and centres the card.
  if (typeof card.focus === "function") card.focus({ preventScroll: true });
  return true;
}

/**
 * j/k/o/a/r/Shift+r. PRD S7 V3 calls this "the one repetitive task in the product", so
 * it is keyboard-first rather than keyboard-optional.
 *
 * Returns the key it acted on, or "" — a real return value so a test can tell
 * "handled" from "ignored" instead of inferring it from a side effect.
 */
export function handleReviewKeydown(event) {
  if (event.defaultPrevented || event.isComposing || event.repeat || event.ctrlKey || event.metaKey || event.altKey) return "";
  if (event.key === "Escape" && state.route === "review" && state.reviewDetailFamily) {event.preventDefault();setHash("#/review");return event.key;}
  if (!state.characterShortcuts.enabled) return "";
  if (isEditing(event.target)) return "";
  if (event.target && event.target.closest && event.target.closest("#review-delivery")) return null;
  if (event.target && event.target.closest && (event.target.closest("#review-eval") || event.target.closest("#review-incidents"))) return null;
  if (event.target && event.target.closest && (event.target.closest("#review-operations") || event.target.closest("#review-rollback"))) return null;
  if (state.route !== "review") return "";
  const target = event && event.target;
  const tag = target && target.tagName ? String(target.tagName).toLowerCase() : "";
  // Never steal a key from a text field. `a` and `r` are letters people type.
  if (tag === "input" || tag === "textarea" || tag === "select") return "";
  const key = event && event.key;
  const lessonReject = event.shiftKey && (key === "R" || key === "r");
  if (["j", "k", "o", "a", "r"].indexOf(key) === -1 && !lessonReject) return "";
  if (key === "o" && event.shiftKey) return "";
  const order = reviewOrder();
  if (order.length === 0) return "";
  if (key === "o" || lessonReject) {
    const family = state.review.families.find(f => f.learning_id === state.selectedFamily);
    if (!family) return "";
    if (lessonReject) {
      const preview = state.reviewPreviews[family.learning_id];
      if (state.deciding[family.learning_id] || !selectedReviewProposals(family).length ||
          !preview || preview.loading || preview.error || !preview.data?.members?.length ||
          !reviewPreviewCurrent(family, preview)) return "";
    }
    event.preventDefault();
    if (lessonReject) decideFamily(family.learning_id, "reject_lesson");
    else setHash("#/review/family/" + encodeURIComponent(family.learning_id));
    return key;
  }
  // `a` and `r` do nothing until something is selected, and a key we do not act
  // on must not be swallowed either — preventDefault comes AFTER that decision,
  // not before it.
  if ((key === "a" || key === "r") && !state.selectedFamily) return "";
  if (key === "a") {
    const preview = state.reviewPreviews[state.selectedFamily];
    if (!preview || preview.loading || preview.error || !preview.data?.ready) return "";
  }
  if (typeof event.preventDefault === "function") event.preventDefault();

  if (key === "j" || key === "k") {
    const at = order.indexOf(state.selectedFamily);
    let next;
    if (at === -1) next = 0;
    else if (key === "j") next = Math.min(at + 1, order.length - 1);
    else next = Math.max(at - 1, 0);
    state.selectedFamily = order[next];
    if (state.reviewDetailFamily) {setHash("#/review/family/" + encodeURIComponent(state.selectedFamily));return key;}
    paintReview();
    revealSelectedCard();
    return key;
  }
  // Acting on an implicit "first card" would let one keystroke decide a rule
  // the operator never looked at; the guard for that is above, before
  // preventDefault.
  decideFamily(state.selectedFamily, key === "a" ? "approve" : "reject");
  return key;
}

/**
 * The line that keeps the auto-appliable proposals visible. They are not cards
 * because nobody has to decide them, and hiding them entirely would make the
 * queue look complete while N items sat stuck for a different reason.
 */
export function renderReviewNote(data) {
  if (!data) return "";
  const parts = [];
  if (data.auto_apply_note) parts.push(esc(data.auto_apply_note));
  if (state.lastCommand) {
    const message = state.lastCommand.action === "reject_lesson" ? "Lesson rejected everywhere." : state.lastCommand.action === "reject_target" ? "Selected targets rejected." : `Approval recorded. Delivery ${esc(state.lastCommand.state)}.`;
    parts.push(`${message} Command ${esc(state.lastCommand.id)}.`);
  }
  const unknown = Object.keys(data.unknown_statuses || {});
  if (unknown.length) {
    parts.push(
      `${unknown.length} proposal status(es) this view has no words for: ` +
      `${esc(unknown.join(", "))}. Reported rather than dropped.`
    );
  }
  return parts.join(" ");
}

/* ------------------------ delivery history ------------------------------- */

export const DELIVERY_STATES = Object.freeze({
  queued: ["Queued", "info"], running: ["Delivering", "running"],
  blocked: ["Needs attention", "partial"], failed: ["Failed", "failed"],
  cancelled: ["Cancelled", "skipped"], completed: ["Completed", "ok"],
});

function deliveryBadge(command) {
  const item = DELIVERY_STATES[command.state];
  if (!item) throw new Error(`Unknown delivery state: ${command.state}`);
  const label = command.cancel_requested && ["queued", "running"].includes(command.state) ? "Cancellation requested" : ["regenerate_eval", "resolve_rollback", "mine_incident", "propose_recovery"].includes(command.action) && command.state === "running" ? "Running" : item[0];
  return badge(label, { state: item[1] });
}

export function renderDeliveryHistory(delivery) {
  const items = delivery.items || [];
  const errorText = [delivery.error, delivery.controlError].filter(Boolean).join(" · ");
  const error = errorText ? `<p class="delivery-error" role="alert">${esc(errorText)} <button class="btn" data-delivery-refresh="true">Try again</button></p>` : "";
  if (!items.length) return error + `<p class="muted">${delivery.loaded ? "No delivery commands yet. Approve a proposal in Review to add one." : "Loading delivery history…"}</p>`;
  return error + items.map((command) => {
    const id = command.id;
    const busy = Boolean(state.deliveryBusy[id]);
    const detail = state.deliveryDetails[id];
    if (command.action === "request_reapplication") {
      return `<article class="delivery-command" id="delivery-command-${esc(id)}" tabindex="-1"><header><h3>Reapplication proposed</h3>${deliveryBadge(command)}</header>` +
        `<p>A fresh proposal is ready for Review. Creating it made no model calls or instruction changes.</p>` +
        `<p><a class="review-link" href="#/review/proposal/${encodeURIComponent(command.result.proposal_id)}">Review the reapplication</a></p>` +
        `<details data-delivery-key="${esc(id)}" ${state.deliveryDisclosures[id] ? "open" : ""}><summary>Recorded request and source</summary><button id="delivery-inspect-${esc(id)}" class="btn" data-delivery-inspect="${esc(id)}">Load reapplication records</button>` +
        (detail && detail.data ? fullRecord(detail.data) : detail && detail.error ? `<p class="delivery-error">${esc(detail.error)}</p>` : "") + `</details></article>`;
    }
    if (command.action === "propose_recovery") {
      const selection=command.selection, result=command.result || {}, used=Object.values(command.budget.consumed).reduce((a,b)=>a+b,0);
      return `<article class="delivery-command" id="delivery-command-${esc(id)}" tabindex="-1"><header><h3>Recovery proposal generation</h3>${deliveryBadge(command)}</header>` +
        `<p>${num(used)} of at most ${num(command.max_model_calls)} calls consumed. Generation makes no instruction changes.</p>` +
        (command.error_code ? `<p class="delivery-error">${esc(command.error_code)} · ${esc(command.error_detail)}</p>` : "") +
        (Object.keys(command.failure_taxonomy || {}).length ? `<p>Recorded causes: ${Object.entries(command.failure_taxonomy).map(([k,v])=>`${esc(k)} (${num(v)})`).join(" · ")}.</p>` : "") +
        `<p>${esc(result.explanation || "")}</p>` +
        (result.supported === false ? `<p>No concrete proposal was supported. The explanation is retained.</p>` : "") +
        (result.proposal_id ? `<p><a class="review-link" href="#/review/proposal/${encodeURIComponent(result.proposal_id)}">Review the recovery proposal</a></p>` : "") +
        `<p><a class="review-link" href="${esc(recoveryLink(selection.learning_id,selection.mode,selection.target_id,selection.proposal_ids))}">Inspect recovery request</a></p>` +
        `<div class="delivery-actions">` +
        (command.can_retry ? `<button class="btn" data-delivery-action="resume_job" data-command-id="${esc(id)}" ${busy ? "disabled" : ""}>Resume reserved work</button>` : "") +
        (command.can_cancel ? `<button class="btn" data-delivery-action="cancel_job" data-command-id="${esc(id)}" ${busy ? "disabled" : ""}>Cancel remaining work</button>` : "") + `</div>` +
        `<details data-delivery-key="${esc(id)}" ${state.deliveryDisclosures[id] ? "open" : ""}><summary>Source, calls, and failure history</summary><button class="btn" data-delivery-inspect="${esc(id)}">Load job records</button>` +
        (detail && detail.data ? fullRecord(detail.data) : detail && detail.error ? `<p class="delivery-error">${esc(detail.error)}</p>` : "") + `</details></article>`;
    }
    if (["regenerate_eval", "resolve_rollback", "mine_incident"].includes(command.action)) {
      const resolution=command.action === "resolve_rollback", mining=command.action === "mine_incident";
      const inspectResult=mining && command.state === "completed";
      const budget = command.budget, stages = command.stages || command.plan && command.plan.stages || [];
      const pid = mining ? command.incident_id : command.proposal_id || command.members && command.members[0].proposal_id;
      const used = budget ? Object.values(budget.consumed).reduce((a,b)=>a+b,0) : null;
      const controls = (command.can_retry ? `<button id="delivery-resume-${esc(id)}" class="btn" data-delivery-action="resume_job" data-command-id="${esc(id)}" ${busy ? "disabled" : ""}>Resume reserved work</button>` : "") +
        (command.can_cancel ? `<button id="delivery-cancel-${esc(id)}" class="btn" data-delivery-action="cancel_job" data-command-id="${esc(id)}" ${busy ? "disabled" : ""}>Cancel remaining work</button>` : "");
      return `<article class="delivery-command" id="delivery-command-${esc(id)}" tabindex="-1"><header><h3>${mining ? "Selected incident mining" : resolution ? "Rollback conflict resolution" : "Evaluation regeneration"}</h3>${deliveryBadge(command)}</header>` +
        `<p>${mining ? "Incident" : "Proposal"} ${esc(pid)} · ${used === null ? "Usage unavailable" : `${num(used)} of at most ${num(command.max_model_calls)} calls consumed`}.</p>` +
        `<p class="footnote">${stages.map(s=>`${s.stage === "eval_gen" ? "Scenario generation" : s.stage === "grade" ? "Trials" : s.stage === "resolve_rollback" ? "Resolution generation" : ["mine", "mine_agentic"].includes(s.stage) ? "Incident analysis" : esc(s.stage)}: at most ${num(s.maximum)}`).join(" · ")}. Internal provider retries belong to one logical call. Mining and gate reservations are separate.</p>` +
        (command.error_code ? `<p class="delivery-error">${esc(command.error_code)} · ${esc(command.error_detail)}</p>` : "") +
        (Object.keys(command.failure_taxonomy || {}).length ? `<p class="footnote">Recorded causes: ${Object.entries(command.failure_taxonomy).map(([cause,count])=>`${esc(cause)} (${num(count)})`).join(" · ")}.</p>` : "") +
        (command.result && command.result.verdict ? `<p>Evaluation: ${esc(command.result.verdict)}. ${command.result.status_update === "updated" ? "The undecided proposal now has this evaluation." : "The existing decision or changed proposal was preserved."}</p>` : "") +
        (mining && command.result ? `<p>${esc(command.result.summary || "")}</p>` +
          (command.result.proposal_error ? `<p class="delivery-error">${esc(command.result.proposal_error.code)} · ${esc(command.result.proposal_error.detail)}</p>` : "") +
          (command.result.outcome ? `<p>Mining result: ${esc(command.result.outcome)}. Instruction files were not changed.</p>` : "") +
          (command.result.proposal_ids || []).map(p=>`<p><a class="review-link" href="#/review/proposal/${encodeURIComponent(p)}">Review the generated change</a></p>`).join("") +
          (command.result.learning_id ? `<a class="review-link" href="#/rules/${encodeURIComponent(command.result.learning_id)}">Inspect the learning and its evidence</a>` : "") : "") +
        (resolution && command.result && command.result.proposal_id ? `<p>${esc(command.result.explanation)}</p><p><a class="review-link" href="#/review/proposal/${encodeURIComponent(command.result.proposal_id)}">Review the generated resolution</a></p>` : "") +
        `<div class="delivery-actions">${controls}<button id="delivery-new-eval-${esc(id)}" class="btn" data-eval-preview="${esc(pid)}" ${inspectResult ? "" : 'data-eval-new="true"'} data-job-action="${esc(command.action)}">${inspectResult ? "Inspect mining result" : "Preview a new attempt…"}</button></div>` +
        `<details ${state.deliveryDisclosures[id] ? "open" : ""} data-delivery-key="${esc(id)}"><summary>Source, calls, and failure history</summary>` +
        `<button id="delivery-inspect-${esc(id)}" class="btn" data-delivery-inspect="${esc(id)}">Load job records</button>` +
        (detail && detail.data ? fullRecord(detail.data) : detail && detail.error ? `<p class="delivery-error">${esc(detail.error)}</p>` : "") + `</details></article>`;
    }
    if (["reject_target", "reject_lesson"].includes(command.action)) {
      const lesson = command.action === "reject_lesson";
      const result = command.result || {};
      const members = detail && detail.data && detail.data.members || [];
      const suppressed = (result.suppressed_proposal_ids || []).length;
      return `<article class="delivery-command" id="delivery-command-${esc(id)}" tabindex="-1"><header><h3>${lesson ? "Lesson rejected everywhere" : "Targets rejected"}</h3>${deliveryBadge(command)}</header>` +
        `<p class="footnote">${esc(command.created_at)} · Command <span class="mono">${esc(id)}</span></p>` +
        `<p>${lesson ? "Future proposals for this lesson are suppressed at every target." : "Future proposals for this lesson are suppressed at the selected canonical files. Other targets remain available."} Existing applied edits remain.</p>` +
        `<p>${num(suppressed)} unapplied proposal${suppressed === 1 ? "" : "s"} suppressed.</p>` +
        `<details data-delivery-key="${esc(id)}" ${state.deliveryDisclosures[id] ? "open" : ""}><summary>Recorded decision and scope</summary>` +
        `<button class="btn" id="delivery-inspect-${esc(id)}" data-delivery-inspect="${esc(id)}">View reviewed revisions and scope</button>` +
        (detail && detail.loading ? `<p role="status">Loading decision…</p>` : detail && detail.error ? `<p class="delivery-error">${esc(detail.error)}</p>` :
          members.map((m) => `<h4>${esc(m.rule_text)}</h4><p class="mono delivery-path">${esc(m.proposal_id)} · ${esc(m.revision)}</p>` + fullRecord(m.target_identity) +
            (m.snapshot ? renderReviewDiff(m.snapshot.proposal.diff_unified, "rejection:" + id + ":" + m.proposal_id, "Rejected edit") + `<details><summary>Complete reviewed evidence and evaluation</summary>${fullRecord(m.snapshot)}</details>` : ``)).join("")) +
        `</details></article>`;
    }
    const targets = command.targets || [];
    const completed = targets.filter((t) => t.state === "completed").length;
    const cancelled = targets.filter((t) => t.state === "cancelled").length;
    const memberCount = command.member_count === undefined ? (command.members || []).length : command.member_count;
    const controls = (command.can_retry ? `<button id="delivery-retry-${esc(id)}" class="btn" data-delivery-action="retry_delivery" data-command-id="${esc(id)}" ${busy ? "disabled" : ""}>${command.cancel_requested ? "Retry cancellation" : "Retry unfinished"}</button>` : "") +
      (command.can_cancel ? `<button id="delivery-cancel-${esc(id)}" class="btn" data-delivery-action="cancel_delivery" data-command-id="${esc(id)}" ${busy ? "disabled" : ""}>Cancel remaining</button>` : "");
    const rows = targets.map((target) => {
      const destination = target.destination || {};
      const result = target.delivery_result || (target.checkpoint || {}).result || {};
      const branch = destination.mode === "git_branch" ? `Branch ${esc(destination.branch_name)} · ${esc(destination.repo_root)}` : "Direct file delivery";
      return `<li>${deliveryBadge(target)} <span class="mono delivery-path">${esc(destination.target_path)}</span>` +
        `<p class="footnote">${branch}</p>` +
        (target.error_code ? `<p class="delivery-error"><strong>${esc(target.error_code)}</strong> · ${esc(target.error_detail)}</p>` : "") +
        (result.branch_commit ? `<p class="footnote mono delivery-path">Commit ${esc(result.branch_commit)}</p>` : "") + `</li>`;
    }).join("");
    let reviewed = "";
    if (detail && detail.loading) reviewed = `<p class="muted">Loading reviewed edits…</p>`;
    else if (detail && detail.error) reviewed = `<p class="delivery-error" role="alert">${esc(detail.error)}</p>`;
    else if (detail && detail.data) {
      reviewed = detail.data.targets.map((t) => `<h4 class="mono delivery-path">${esc(t.destination.target_path)}</h4>` + renderReviewDiff(t.diff_unified, "delivery:" + id + ":" + t.id, "Reviewed edit")).join("");
      reviewed += detail.data.members.filter((member) => detail.data.targets.some((target) => target.id === member.target_id && target.state === "completed"))
        .map((member) => `<p><a class="review-link" href="#/review/rollback/${encodeURIComponent(member.proposal_id)}">Review rollback · ${esc(member.proposal_id)}</a></p>`).join("");
      const history = detail.data.control_history || [];
      if (history.length) reviewed += `<h4>Control history</h4>` + history.map((event) => `<p>${esc(event.action === "retry_delivery" ? "Retry requested" : "Cancellation requested")} · ${esc(event.created_at)} · ${esc(event.actor)}</p>` + fullRecord(event.before)).join("");
    }
    return `<article class="delivery-command" id="delivery-command-${esc(id)}" tabindex="-1">` +
      `<header><h3>Approval · ${num(memberCount)} proposal${memberCount === 1 ? "" : "s"}</h3>${deliveryBadge(command)}</header>` +
      `<p class="footnote">${esc(command.created_at || "Time unknown")} · Command <span class="mono">${esc(id)}</span></p>` +
      `<p>${completed} of ${targets.length} target${targets.length === 1 ? "" : "s"} delivered${cancelled ? ` · ${cancelled} cancelled` : ""}.</p>` +
      `<details data-delivery-key="${esc(id)}" ${state.deliveryDisclosures[id] ? "open" : ""}><summary>Targets and results (${targets.length})</summary><ul class="delivery-targets">${rows}</ul>` +
      `<button id="delivery-inspect-${esc(id)}" class="btn" data-delivery-inspect="${esc(id)}">View reviewed edits and control history</button>${reviewed}</details>` +
      `<div class="delivery-actions">${controls}${busy ? `<span role="status">Recording request…</span>` : ""}</div>` +
      (!command.controls_available ? `<p class="footnote">Upgrade the state database to use delivery controls.</p>` : "") + `</article>`;
  }).join("") + (delivery.nextCursor ? `<button class="btn" data-delivery-older="true" ${delivery.loading ? "disabled" : ""}>Load older commands</button>` : "");
}

export function paintDeliveryHistory() {
  const body = byId("delivery-body");
  const active = doc() && doc().activeElement;
  const activeId = active && active.id;
  const artifactScroll = activeId?.startsWith("artifact-text-") ? [active.scrollTop, active.scrollLeft] : null;
  const card = active && active.closest ? active.closest(".delivery-command") : null;
  if (body.querySelectorAll) body.querySelectorAll("details[data-delivery-key]").forEach((el) => {
    state.deliveryDisclosures[el.getAttribute("data-delivery-key")] = el.open;
  });
  if (body.querySelectorAll) body.querySelectorAll("details[data-review-key]").forEach((el) => {
    state.reviewDisclosures[el.getAttribute("data-review-key")] = el.open;
  });
  const html = renderDeliveryHistory(state.delivery);
  if (state.deliveryRendered !== html) { body.innerHTML = html; state.deliveryRendered = html; }
  const counts = state.delivery.items.reduce((sum, c) => sum + (c.targets || []).filter((t) => t.state === "completed").length, 0);
  setText("delivery-status", state.delivery.loaded ? `${state.delivery.items.length} command${state.delivery.items.length === 1 ? "" : "s"} shown · ${counts} target${counts === 1 ? "" : "s"} delivered${state.delivery.loading ? " · Refreshing…" : ""}` : "Loading delivery history…");
  if (activeId && activeId.startsWith("delivery-")) {
    const candidate = doc().getElementById(activeId);
    const restored = candidate && !candidate.disabled ? candidate : card && doc().getElementById(card.id);
    if (restored && restored.focus) { restored.focus({ preventScroll: true }); if (artifactScroll) {restored.scrollTop=artifactScroll[0];restored.scrollLeft=artifactScroll[1];} }
  }
}

function mergeDeliveries(items) {
  const merged = new Map(state.delivery.items.map((c) => [c.id, c]));
  items.forEach((c) => merged.set(c.id, c));
  state.delivery.items = Array.from(merged.values()).sort((a, b) => {
    const left = a.created_at + a.id, right = b.created_at + b.id;
    return left === right ? 0 : left < right ? 1 : -1;
  });
  if (state.lastCommand && merged.has(state.lastCommand.id)) state.lastCommand = merged.get(state.lastCommand.id);
}

export async function loadDeliveryHistory({ older = false } = {}) {
  const delivery = state.delivery;
  if (delivery.promise) return delivery.promise;
  const generation = delivery.generation;
  const wasLoaded = delivery.loaded;
  const previous = JSON.stringify(delivery.items.map((c) => [c.id, c.state, c.cancel_requested]));
  delivery.loading = true;
  delivery.promise = (async () => {
    try {
      let url = API.commands + "?summary=true&limit=10";
      if (older && delivery.nextCursor) url += "&cursor=" + encodeURIComponent(delivery.nextCursor);
      const page = await getJSON(url);
      if (!page || !Array.isArray(page.commands)) throw new Error("Delivery history returned an invalid response.");
      const incoming = page.commands.slice();
      if (!older) {
        const seen = new Set(incoming.map((c) => c.id));
        const pending = delivery.items.filter((c) => !seen.has(c.id) && ["queued", "running"].includes(c.state));
        const current = await Promise.all(pending.map((c) => getJSON(API.commands + "/" + encodeURIComponent(c.id) + "?summary=true")));
        incoming.push(...current);
      }
      if (generation !== delivery.generation) return;
      const miningChanged=incoming.some(c=>["mine_incident","propose_recovery"].includes(c.action) && ["completed","cancelled","failed","blocked"].includes(c.state) && !delivery.items.some(old=>old.id===c.id && old.state===c.state));
      mergeDeliveries(incoming);
      const inspected=incoming.filter(c=>state.deliveryDetails[c.id]?.data && state.deliveryDetails[c.id].data.updated_at!==c.updated_at);
      await Promise.all(inspected.map(async c=>{
        const entry={loading:true};state.deliveryDetails[c.id]=entry;
        try{entry.data=await getJSON(API.commands+"/"+encodeURIComponent(c.id));}
        catch(error){entry.error=String(error.message || error);}
        finally{entry.loading=false;}
      }));
      if(generation!==delivery.generation)return;
      if (older || !delivery.olderLoaded) delivery.nextCursor = page.next_cursor;
      if (older) delivery.olderLoaded = true;
      delivery.loaded = true;
      delivery.error = "";
      const changed = previous !== JSON.stringify(delivery.items.map((c) => [c.id, c.state, c.cancel_requested]));
      if (miningChanged) {
        await Promise.all([load(),loadMiningIncidents()]);
      } else if (wasLoaded && changed) {
        if (await refreshReviewSnapshot()) paintReview();
      }
      if (state.review) setText("review-note", renderReviewNote(state.review));
    } catch (error) {
      delivery.error = String(error && error.message ? error.message : error);
    } finally {
      delivery.loading = false;
      delivery.promise = null;
      if (state.route === "review") paintDeliveryHistory();
    }
  })();
  paintDeliveryHistory();
  return delivery.promise;
}

export async function requestDeliveryControl(id, action) {
  if (state.deliveryBusy[id]) return;
  state.deliveryBusy[id] = true;
  state.delivery.generation += 1;
  const key = id + ":" + action;
  if (!state.deliveryRequests[key]) state.deliveryRequests[key] = crypto.randomUUID();
  paintDeliveryHistory();
  try {
    const result = await postJSON(API.commands, { action: action, command_id: id, request_key: state.deliveryRequests[key] });
    mergeDeliveries([result]);
    delete state.deliveryRequests[key];
    delete state.deliveryDetails[id];
    state.delivery.controlError = "";
  } catch (error) {
    state.delivery.controlError = String(error && error.message ? error.message : error);
  } finally {
    delete state.deliveryBusy[id];
    if (state.delivery.promise) await state.delivery.promise;
    await loadDeliveryHistory();
    paintDeliveryHistory();
  }
}

export async function inspectDelivery(id) {
  state.deliveryDetails[id] = { loading: true };
  paintDeliveryHistory();
  try {
    state.deliveryDetails[id] = { data: await getJSON(API.commands + "/" + encodeURIComponent(id)) };
  } catch (error) {
    state.deliveryDetails[id] = { error: String(error && error.message ? error.message : error) };
  }
  paintDeliveryHistory();
}

/* ---------------------- instruction operations --------------------------- */

function operationDestination(operation) {
  return operation.destination || operation.record.destination;
}

function operationControls(operation) {
  const id = esc(operation.id), busy = state.operationBusy[operation.id] ? "disabled" : "";
  return (operation.can_retry ? `<button id="operation-retry-${id}" class="btn" data-operation-action="retry_operation" data-operation-id="${id}" ${busy}>${operation.cancel_requested ? "Retry cancellation" : "Retry operation"}</button>` : "") +
    (operation.can_cancel ? `<button id="operation-cancel-${id}" class="btn" data-operation-action="cancel_operation" data-operation-id="${id}" ${busy}>Cancel operation</button>` : "");
}

export function renderOperation(operation, { compact = false } = {}) {
  const destination = operationDestination(operation);
  const title = operation.kind === "rollback" ? "Rollback" : operation.kind === "auto_apply" ? "Automatic delivery" : null;
  if (!title) throw new Error(`Unknown instruction operation: ${operation.kind}`);
  const id = operation.id, detail = state.operationDetails[id];
  const branch = destination.mode === "git_branch" ? `Branch ${esc(destination.branch_name)} · ${esc(destination.repo_root)}` : "Direct file delivery";
  let contents = "";
  if (detail && detail.loading) contents = `<p class="muted">Loading recorded change…</p>`;
  else if (detail && detail.error) contents = `<p class="delivery-error" role="alert">${esc(detail.error)}</p>`;
  else if (detail && detail.data) {
    const record = detail.data.record;
    const inverse = record.rollback;
    contents = renderReviewDiff(inverse ? inverse.diff_unified : record.proposal.diff_unified, "operation:" + id, inverse ? "Rollback edit" : "Recorded applied edit") +
      (inverse ? `<p>Affected proposals: ${inverse.source.affected_members.map((m) => `<span class="id">${esc(m.proposal_id)}</span>`).join(", ")}</p>` : "") +
      `<details ${reviewDisclosure("operation-record:" + id)}><summary>Application record and recovery history</summary>${fullRecord({source: record, failures: detail.data.failures, controls: detail.data.control_history, result: detail.data.result})}</details>`;
  }
  const result = operation.result || {};
  return `<article class="delivery-command" id="${compact ? "rollback-result-" : "operation-"}${esc(id)}" tabindex="-1">` +
    `<header><h3>${title}</h3>${deliveryBadge(operation)}</header>` +
    `<p class="mono delivery-path">${esc(destination.target_path)}</p><p class="footnote">${branch}</p>` +
    (operation.error_code ? `<p class="delivery-error" role="alert"><strong>${esc(operation.error_code)}</strong> · ${esc(operation.error_detail)}</p>` : "") +
    (result.branch_commit ? `<p class="footnote mono delivery-path">Commit ${esc(result.branch_commit)}</p>` : "") +
    (operation.cancel_requested && operation.state === "completed" ? `<p class="footnote">The write had already completed; cancellation preserved its recorded result.</p>` : "") +
    (compact ? "" : `<details data-operation-key="${esc(id)}" ${state.operationDisclosures[id] ? "open" : ""}><summary>Recorded change and history</summary>` +
      `<p class="footnote">${esc(operation.created_at)} · <span class="id">${esc(id)}</span></p>` +
      `<button id="operation-inspect-${esc(id)}" class="btn" data-operation-inspect="${esc(id)}">Load recorded change</button>${contents}</details>`) +
    `<div class="delivery-actions">${compact ? "" : operationControls(operation)}` +
    (!compact && operation.kind === "auto_apply" && operation.state === "completed" ? `<a class="btn" href="#/review/rollback/${encodeURIComponent(operation.proposal_id)}">Review rollback</a>` : "") +
    (!compact && operation.kind === "rollback" && operation.state === "completed" ? `<a class="btn" href="#/review/reapply/${encodeURIComponent(operation.proposal_id)}">Review reapplication</a>` : "") + `</div>` +
    (!operation.controls_available ? `<p class="footnote">Upgrade the state database to use operation controls.</p>` : "") + `</article>`;
}

export function renderOperationHistory(history) {
  const error = [history.error, history.controlError].filter(Boolean).join(" · ");
  return (error ? `<p class="delivery-error" role="alert">${esc(error)}</p>` : "") +
    (history.items.length ? history.items.map((operation) => renderOperation(operation)).join("") : `<p class="muted">${history.loaded ? "No automatic delivery or rollback operations yet." : "Loading operations…"}</p>`) +
    (history.nextCursor ? `<button class="btn" data-operation-older="true" ${history.loading ? "disabled" : ""}>Load older operations</button>` : "");
}

function preserveOperationView(id, html) {
  const body = byId(id), active = doc().activeElement;
  const focusedSection=id==="inspector-body" && body.contains?.(active) ? active?.getAttribute?.("data-tab") : null;
  const activeId = active && active.id;
  const retainedTextScroll = activeId && (activeId.startsWith("artifact-text-") || activeId.startsWith("rollback-inverse-text-")) ? [active.scrollTop, active.scrollLeft] : null;
  const card = active && active.closest ? active.closest(".delivery-command") : null;
  if (body.querySelectorAll) body.querySelectorAll("details[data-operation-key]").forEach((el) => { state.operationDisclosures[el.getAttribute("data-operation-key")] = el.open; });
  if (body.querySelectorAll) body.querySelectorAll("details[data-review-key]").forEach((el) => { state.reviewDisclosures[el.getAttribute("data-review-key")] = el.open; });
  // Render after reading disclosure state so polling does not close inspected evidence.
  const rendered = typeof html === "function" ? html() : html;
  if (body.innerHTML !== rendered) body.innerHTML = rendered;
  if(focusedSection)Array.from(body.querySelectorAll?.(".tabs [data-tab]") || []).find(el=>el.getAttribute("data-tab")===focusedSection)?.focus({preventScroll:true});
    if (activeId && (activeId.startsWith("operation-") || activeId.startsWith("rollback-") || activeId.startsWith("eval-job-") || activeId.startsWith("evaluation-") || activeId.startsWith("quality-") || activeId.startsWith("policy-") || activeId.startsWith("trend-") || activeId.startsWith("mining-history-") || activeId.startsWith("scan-history-") || activeId.startsWith("availability-") || activeId.startsWith("sessions-") || activeId.startsWith("native-loads-") || activeId.startsWith("recurrence-") || activeId.startsWith("inventory-") || activeId.startsWith("run-") || activeId.startsWith("artifact-") || activeId.startsWith("project-") || activeId.startsWith("rules-read-"))) {
    const candidate = doc().getElementById(activeId);
    const runStatus = /^(run|project)-(older|load)-/.test(activeId) ? doc().getElementById(activeId.replace(/^(run|project)-(older|load)-/, "$1-status-")) : null;
    const restored = candidate && !candidate.disabled ? candidate : runStatus || (activeId.startsWith("availability-") ? doc().getElementById("availability-status") : activeId.startsWith("inventory-") ? doc().getElementById("inventory-status") : null) || card && doc().getElementById(card.id);
    if (restored && restored.focus) { restored.focus({ preventScroll: true }); if (retainedTextScroll) {restored.scrollTop=retainedTextScroll[0];restored.scrollLeft=retainedTextScroll[1];} }
  }
}

export function paintOperationHistory() {
  preserveOperationView("operations-body", () => renderOperationHistory(state.operations));
  setText("operations-status", `${state.operations.items.length} operation${state.operations.items.length === 1 ? "" : "s"} shown${state.operations.loading ? " · Refreshing…" : ""}`);
  if (state.rollback.proposalId) paintRollback();
}

function mergeOperations(incoming) {
  const merged = new Map(state.operations.items.map((o) => [o.id, o]));
  incoming.forEach((operation) => {
    merged.set(operation.id, operation);
  });
  state.operations.items = Array.from(merged.values()).sort((a, b) => (b.created_at + b.id).localeCompare(a.created_at + a.id));
}

export async function loadOperationHistory({ older = false } = {}) {
  const history = state.operations;
  if (history.promise) return history.promise;
  const generation = history.generation, wasLoaded = history.loaded;
  const before = JSON.stringify(history.items.map((o) => [o.id, o.state, o.cancel_requested]));
  history.loading = true;
  history.promise = (async () => {
    try {
      let url = API.operations + "?summary=true&limit=10";
      if (older && history.nextCursor) url += "&cursor=" + encodeURIComponent(history.nextCursor);
      const page = await getJSON(url);
      if (!page || !Array.isArray(page.operations)) throw new Error("Operation history returned an invalid response.");
      const incoming = page.operations.slice();
      if (!older) {
        const seen = new Set(incoming.map((o) => o.id));
        const pending = history.items.filter((o) => !seen.has(o.id) && (["queued", "running"].includes(o.state) || o.id === state.rollback.operationId));
        incoming.push(...await Promise.all(pending.map((o) => getJSON(API.operations + "/" + encodeURIComponent(o.id) + "?summary=true"))));
      }
      if (generation !== history.generation) return;
      mergeOperations(incoming);
      const inspected = incoming.filter((o) => state.operationDetails[o.id] && state.operationDetails[o.id].data && state.operationDetails[o.id].data.updated_at !== o.updated_at);
      const details = await Promise.all(inspected.map((o) => getJSON(API.operations + "/" + encodeURIComponent(o.id))));
      if (generation !== history.generation) return;
      details.forEach((data) => { state.operationDetails[data.id] = {data}; });
      if (older || !history.olderLoaded) history.nextCursor = page.next_cursor;
      if (older) history.olderLoaded = true;
      history.loaded = true;
      history.error = "";
      const after = JSON.stringify(history.items.map((o) => [o.id, o.state, o.cancel_requested]));
      if (wasLoaded && before !== after) await load();
    } catch (error) {
      history.error = String(error.message || error);
    } finally {
      history.loading = false;
      history.promise = null;
      if (state.route === "review") paintOperationHistory();
    }
  })();
  paintOperationHistory();
  return history.promise;
}

export async function requestOperationControl(id, action) {
  if (state.operationBusy[id]) return;
  state.operationBusy[id] = true;
  state.operations.generation += 1;
  const key = id + ":" + action;
  if (!state.operationRequests[key]) state.operationRequests[key] = crypto.randomUUID();
  paintOperationHistory();
  try {
    const operation = await postJSON(API.commands, {action, operation_id: id, request_key: state.operationRequests[key]});
    mergeOperations([operation]);
    delete state.operationRequests[key];
    state.operations.controlError = "";
  } catch (error) {
    state.operations.controlError = String(error.message || error);
  } finally {
    delete state.operationBusy[id];
    if (state.operations.promise) await state.operations.promise;
    await loadOperationHistory();
  }
}

export async function inspectOperation(id) {
  state.operationDetails[id] = {loading: true};
  paintOperationHistory();
  try {
    state.operationDetails[id] = {data: await getJSON(API.operations + "/" + encodeURIComponent(id))};
  } catch (error) {
    state.operationDetails[id] = {error: String(error.message || error)};
  }
  paintOperationHistory();
}

export function renderRollback(rollback) {
  if (rollback.loading) return `<p class="muted" role="status">Reading the applied change and current target…</p>`;
  const preview = rollback.preview;
  const error = rollback.error ? `<p class="delivery-error" role="alert">${esc(rollback.error)}</p>` : "";
  const refresh = `<button id="rollback-refresh" class="btn" data-rollback-refresh="true" ${rollback.busy ? "disabled" : ""}>Refresh preview</button>`;
  if (!preview) return error + refresh;
  const source = preview.source, destination = source.destination;
  const operation = state.operations.items.find((o) => o.id === rollback.operationId);
  const result = operation ? renderOperation(operation, {compact: true}) + `<button id="rollback-open-operation" class="btn" data-operation-focus="${esc(operation.id)}">Open operation controls and history</button>` : "";
  return error + result + (operation ? "" : `<p class="mono delivery-path">${esc(destination.target_path)}</p>` +
    `<p>${destination.mode === "git_branch" ? `Changes branch <strong>${esc(destination.branch_name)}</strong>. Working copies receive the change after repository integration.` : "Changes this instruction file."}</p>` +
    `<p>Reverses this applied contribution and preserves unrelated current content.</p>`) +
    (source.affected_members.length > 1 ? `<p><strong>${source.affected_members.length} proposals share this identical edit.</strong> Rolling it back affects all of them.</p>` : "") +
    `<details ${reviewDisclosure("rollback-source:" + rollback.proposalId)}><summary id="rollback-source-${esc(rollback.proposalId)}">Applied revision and affected proposals</summary><p class="id">${esc(source.application_id)}</p>` + source.affected_members.map((m) => `<p class="id">${esc(m.proposal_id)}</p>`).join("") + `</details>` +
    (preview.diff_unified ? `<details class="review-disclose" ${reviewDisclosure("rollback:" + rollback.proposalId + ":" + preview.revision, true)}><summary id="rollback-inverse-${esc(rollback.proposalId)}">Full inverse change</summary><pre id="rollback-inverse-text-${esc(rollback.proposalId)}" class="review-card__diff">${renderUnifiedDiff(preview.diff_unified)}</pre></details>` : "") +
    (!preview.ready ? `<p class="${preview.error_code === "AlreadyRolledBack" ? "footnote" : "delivery-error"}" role="status"><strong>${esc(preview.error_code)}</strong> · ${esc(preview.detail)}</p>` : "") +
    (!preview.ready && preview.error_code !== "AlreadyRolledBack" ? `<details ${reviewDisclosure("rollback-conflict:" + rollback.proposalId)}><summary>Recorded application and current content</summary>` +
      `<h4>Before application</h4><pre class="review-full-record">${esc(source.before)}</pre>` +
      `<h4>Applied content</h4><pre class="review-full-record">${esc(source.applied)}</pre>` +
      `<h4>Current content</h4><pre class="review-full-record">${esc(preview.base.content)}</pre></details>` : "") +
    (preview.error_code === "RollbackConflict" ? `<button id="rollback-resolve" class="btn" data-eval-preview="${esc(rollback.proposalId)}" data-job-action="resolve_rollback">Preview a resolution proposal…</button>` : "") +
    (preview.error_code === "AlreadyRolledBack" || operation && operation.state === "completed" ? `<a class="btn" href="#/review/reapply/${encodeURIComponent(rollback.proposalId)}">Review reapplication…</a>` : "") +
    `<div class="delivery-actions">${preview.ready && !rollback.operationId ? `<button id="rollback-submit" class="btn" data-rollback-submit="true" ${rollback.busy ? "disabled" : ""}>${rollback.busy ? "Recording rollback…" : "Roll back this change"}</button>` : ""}${refresh}</div>` +
    `<p class="footnote">Rollback uses recorded content and makes no model calls.</p>`;
}

export function paintRollback() {
  preserveOperationView("rollback-body", () => renderRollback(state.rollback));
}

export async function openRollback(proposalId, { refresh = false } = {}) {
  const rollback = state.rollback;
  const title = byId("rollback-title");
  const reveal = () => {
    if (title.scrollIntoView) title.scrollIntoView({block: "start"});
    if (title.focus) title.focus({preventScroll: true});
  };
  if (!refresh && rollback.proposalId === proposalId && (rollback.loading || rollback.preview || rollback.error)) { reveal(); return; }
  const generation = ++rollback.generation;
  Object.assign(rollback, {proposalId, loading: true, preview: null, error: "", operationId: ""});
  paintRollback();
  reveal();
  try {
    const preview = await getJSON(rollbackPreviewUrl(proposalId));
    if (generation !== rollback.generation) return;
    rollback.preview = preview;
    if (preview.active_operation) {
      mergeOperations([preview.active_operation]);
      rollback.operationId = preview.active_operation.id;
    } else if (preview.existing_operation_id) {
      const operation = await getJSON(API.operations + "/" + encodeURIComponent(preview.existing_operation_id) + "?summary=true");
      if (generation !== rollback.generation) return;
      mergeOperations([operation]);
      rollback.operationId = operation.id;
    }
  } catch (error) {
    if (generation === rollback.generation) rollback.error = String(error.message || error);
  } finally {
    if (generation === rollback.generation) { rollback.loading = false; paintRollback(); }
  }
}

export async function submitRollback() {
  const rollback = state.rollback, preview = rollback.preview;
  if (rollback.busy || !preview || !preview.ready || rollback.operationId) return;
  const generation = rollback.generation, proposalId = rollback.proposalId;
  const key = proposalId + ":" + preview.revision;
  if (!state.rollbackRequests[key]) state.rollbackRequests[key] = crypto.randomUUID();
  rollback.busy = true;
  state.operations.generation += 1;
  paintRollback();
  try {
    const operation = await postJSON(API.commands, {action: "rollback", proposal_id: proposalId,
      preview_revision: preview.revision, request_key: state.rollbackRequests[key]});
    mergeOperations([operation]);
    delete state.rollbackRequests[key];
    if (generation === rollback.generation) { rollback.operationId = operation.id; rollback.error = ""; }
  } catch (error) {
    if (generation === rollback.generation) rollback.error = String(error.message || error);
  } finally {
    rollback.busy = false;
    if (state.operations.promise) await state.operations.promise;
    await loadOperationHistory();
    paintOperationHistory();
  }
}

/* ------------------------------ routing ---------------------------------- */

export async function loadMiningIncidents({older=false}={}) {
  const entry=state.incidents;
  if (entry.loading) return;
  entry.loading=true;entry.error="";
  try {
    const data=await getJSON(API.incidents+"?limit=25"+(older && entry.nextCursor ? "&cursor="+encodeURIComponent(entry.nextCursor) : ""));
    entry.items=older ? [...entry.items,...data.items] : data.items;
    entry.count=data.count;entry.nextCursor=data.next_cursor;entry.loaded=true;
  } catch(error) {entry.error=String(error.message || error);}
  finally {
    entry.loading=false;
    if (entry.error && !entry.loaded) {byId("incident-body").innerHTML=`<p class="delivery-error">${esc(entry.error)}</p>`;return;}
    byId("incident-body").innerHTML=(entry.error ? `<p class="delivery-error">${esc(entry.error)}</p>` : "") +
      `<p>${num(entry.count)} unmined incident(s). Showing ${num(entry.items.length)}. Opening a preview makes no model calls.</p>` +
      (entry.items.length ? entry.items.map(renderIncident).join("") : `<p>No unmined incidents in this database.</p>`) +
      (entry.nextCursor ? `<button class="btn" data-incidents-older="true">Load more incidents</button>` : "");
  }
}

function paintMiningJob(job) {
  const preview=job.preview, summary=preview && preview.source_summary;
  preserveOperationView("eval-job-body",()=>
    (job.loading ? `<p role="status">Reading the incident, retained context, and call limit…</p>` : "") +
    (job.error ? `<p class="delivery-error" role="alert">${esc(job.error)}</p>` : "") +
    (job.commandId ? `<p role="status">Mining job recorded. Track its result in the history below. Command ${esc(job.commandId)}.</p>` : "") +
    (preview ? `<p>${esc(preview.meaning)}</p>` +
      `<p><strong>Maximum: ${num(preview.max_model_calls)} logical model call${preview.max_model_calls===1 ? "" : "s"}.</strong> Mining and evaluation have separate reservations.</p>` +
      `<p>Incident <span class="id">${esc(preview.incident_id)}</span> · ${esc(summary.incident.signal_type)} · ${esc(summary.incident.ts)}</p>` +
      renderIncidentPrimary(summary.incident, summary.presentation) +
      `<p>Evidence: ${summary.coverage.kind === "full_session" ? "full redacted session" : "retained incident window"}. ${num(summary.coverage.truncations || 0)} event bodies have explicit truncation markers.</p>` +
      `<p>Frozen deduplication context: ${num(summary.learning_count)} learnings and ${num(summary.instruction_count)} instruction units.</p>` +
      ((summary.coverage.unreadable_instructions || []).length ? `<p class="delivery-error">Some instruction files could not be read.</p>${fullRecord(summary.coverage.unreadable_instructions)}` : "") +
      `<p class="footnote">${summary.files.map(f=>`${esc(f.name)}: ${num(f.bytes)} bytes`).join(" · ")}</p>` +
      (preview.ready || job.commandId ? `<button class="btn" data-mining-source="true">Inspect complete mining input</button>` : "") +
      (job.sourceRecord ? `<details open><summary>Complete frozen mining input</summary>${fullRecord(job.sourceRecord)}</details>` : "") +
      (preview.ready && !job.commandId ? `<button id="eval-job-submit" class="btn" data-eval-submit="true" ${job.busy ? "disabled" : ""}>${job.busy ? "Recording job…" : "Mine this incident · up to 1 call"}</button>` : "") : "") +
    `<button id="eval-job-refresh" class="btn" data-eval-refresh="true" ${job.busy ? "disabled" : ""}>Refresh cost preview</button>`);
}

export async function inspectMiningSource() {
  const entry=state.evalJob;
  try {
    const record=entry.commandId ? await getJSON(`${API.commands}/${encodeURIComponent(entry.commandId)}`) : await getJSON(`/api/incidents/${encodeURIComponent(entry.proposalId)}/mining-preview?full=true`);
    if (!entry.commandId && record.revision!==entry.preview.revision) throw new Error("The incident input changed. Refresh the cost preview before continuing.");
    entry.sourceRecord=record.source;
  } catch(error) {entry.error=String(error.message || error);}
  if (state.evalJob===entry) paintEvalJob();
}

export function recoveryLink(learningId, mode, targetId="", proposalIds=[]) {
  const selection={learning_id:learningId,mode,target_id:targetId,proposal_ids:proposalIds};
  return "#/review/recovery/"+encodeURIComponent(JSON.stringify(selection));
}

function paintRecoveryJob(job) {
  const preview=job.preview, source=preview && preview.source.snapshot;
  preserveOperationView("eval-job-body",()=>
    (job.loading ? `<p role="status">Reading recovery destinations and evidence…</p>` : "") +
    (job.error ? `<p class="delivery-error" role="alert">${esc(job.error)}</p>` : "") +
    (job.commandId ? `<p role="status">Recovery job recorded. Track its result in history below.</p>` : "") +
    (!preview && job.options ? `<h3>${esc(job.options.rule_text)}</h3><p>Choose the destination for this proposal. No model calls occur until you submit its preview.</p>` +
      job.options.targets.filter(t=>t.modes.includes(job.selection.mode)).map(t=>
        `<p>${t.available ? `<a class="review-link" href="${esc(recoveryLink(job.selection.learning_id,job.selection.mode,t.id,job.selection.proposal_ids))}">${esc(t.label)}</a>` : `<strong>${esc(t.label)}</strong> · ${esc(t.reason.detail)}`}<br><code class="delivery-path">${esc(t.destination.target_path)}</code></p>`).join("") +
      (job.options.unavailable.length ? `<details><summary>Unavailable destinations (${job.options.unavailable.length})</summary>${fullRecord(job.options.unavailable)}</details>` : "") : "") +
    (source ? `<h3>${esc(source.learning.rule_text)}</h3><p><strong>Maximum: 1 logical model call.</strong> Proposal generation uses the mining budget; evaluation is a separate request.</p>` +
      `<p class="mono delivery-path">${esc(source.destination.target_path)}</p>` +
      `<p>${source.destination.mode === "git_branch" ? `After approval, delivery uses branch ${esc(source.destination.branch_name)}. Repository integration into working copies remains separate.` : "After approval, the instruction worker delivers to this file."}</p>` +
      `<p>${esc(preview.meaning)} Existing decisions remain in history.</p>` +
      `<p>${num(source.proposal_ids.length)} selected older proposal(s) will be superseded only if generation publishes a replacement. Other proposals and decisions remain intact.</p>` +
      (source.mode === "hook" ? `<p>This proposes a Claude command hook. Inspect its command before approval. Generation does not run the hook or prove enforcement.</p>` : "") +
      `<details ${reviewDisclosure("recovery-source:"+preview.revision)}><summary>Complete evidence, selected proposals, and current target</summary>${fullRecord(source)}</details>` +
      (!job.commandId ? `<button class="btn" id="eval-job-submit" data-eval-submit="true" ${job.busy ? "disabled" : ""}>${job.busy ? "Recording job…" : "Generate recovery proposal · up to 1 call"}</button>` : "") : "") +
    (job.selection.target_id ? `<button class="btn" data-recovery-refresh="true" ${job.busy ? "disabled" : ""}>${job.commandId ? "Preview a new recovery attempt…" : "Refresh recovery preview"}</button>` : ""));
}

export async function openRecoveryJob(selection,{newAttempt=false}={}) {
  if (state.evalJob.busy) return;
  const entry={action:"propose_recovery",selection,preview:null,options:null,loading:true,busy:false,error:"",commandId:""};
  state.evalJob=entry;
  if (globalThis.history && globalThis.history.replaceState) globalThis.history.replaceState(null,"",recoveryLink(selection.learning_id,selection.mode,selection.target_id,selection.proposal_ids));
  setText("eval-job-title",selection.mode === "hook" ? "Request a hook proposal" : selection.mode === "correct_target" ? "Request a corrected target" : "Regenerate selected patches");
  setVisible(byId("review-eval"),true);paintEvalJob();
  const title=byId("eval-job-title");if(title.scrollIntoView)title.scrollIntoView({block:"start"});if(title.focus)title.focus({preventScroll:true});
  try {
    const base=`/api/learnings/${encodeURIComponent(selection.learning_id)}`;
    if (!selection.target_id) entry.options=await getJSON(base+"/recovery-options");
    else {
      entry.preview=await getJSON(base+"/recovery-preview?mode="+encodeURIComponent(selection.mode)+"&target_id="+encodeURIComponent(selection.target_id)+"&proposal_ids="+encodeURIComponent(JSON.stringify(selection.proposal_ids))+"&fresh="+(newAttempt ? "true" : "false"));
      if (!newAttempt && entry.preview.latest_job)entry.commandId=entry.preview.latest_job.id;
    }
  } catch(error){entry.error=String(error.message || error);}
  finally{entry.loading=false;if(state.evalJob===entry)paintEvalJob();}
}

export function paintEvalJob() {
  const job = state.evalJob, preview = job.preview, resolution = job.action === "resolve_rollback";
  if (job.action === "mine_incident") { paintMiningJob(job); return; }
  if (job.action === "propose_recovery") { paintRecoveryJob(job); return; }
  if (job.action === "request_reapplication") {
    preserveOperationView("eval-job-body", () =>
      (job.loading ? `<p role="status">Reading the recorded rollback and current target…</p>` : "") +
      (job.error ? `<p class="delivery-error" role="alert">${esc(job.error)}</p>` : "") +
      (job.resultProposalId ? `<p role="status">Reapplication proposal recorded. <a href="#/review/proposal/${encodeURIComponent(job.resultProposalId)}">Review the reapplication</a></p>` : "") +
      (preview ? `<p class="mono delivery-path">${esc(preview.source.destination.target_path)}</p><p>This creates a fresh proposal for manual Review. It makes no model calls or instruction changes.</p>` +
        (preview.source.destination.mode === "git_branch" ? `<p>Delivery after approval uses branch <strong>${esc(preview.source.destination.branch_name)}</strong>. Working copies receive the change after repository integration.</p>` : "") +
        (!preview.ready ? `<p class="delivery-error"><strong>${esc(preview.error_code)}</strong> · ${esc(preview.detail)}</p>` : "") +
        (preview.successor_id ? `<p><a href="#/review/rollback/${encodeURIComponent(preview.successor_id)}">Inspect the later application</a></p>` : "") +
        renderReviewDiff(preview.diff_unified,"reapply:" + job.proposalId + ":" + preview.revision,"Complete reapplication patch") +
        `<details ${reviewDisclosure("reapply-source:" + job.proposalId)}><summary>Original application, rollback, and current content</summary>${fullRecord(preview)}</details>` +
        (preview.ready && !job.commandId ? `<button id="eval-job-submit" class="btn" data-eval-submit="true" ${job.busy ? "disabled" : ""}>${job.busy ? "Recording proposal…" : "Create reapplication proposal"}</button>` : "") : "") +
      `<button id="eval-job-refresh" class="btn" data-eval-refresh="true" ${job.busy ? "disabled" : ""}>Refresh preview</button>`);
    return;
  }
  preserveOperationView("eval-job-body", () => (job.loading ? `<p role="status">Reading the source and call limit…</p>` : "") +
    (job.error ? `<p class="delivery-error" role="alert">${esc(job.error)}</p>` : "") +
    (job.commandId ? `<p role="status">Job recorded. Track its result in the history below. Command ${esc(job.commandId)}.</p>` : "") +
    (preview ? `<h3>${esc(preview.source.snapshot.learning.rule_text)}</h3><p>Proposal ${esc(preview.proposal_id)}.</p>` +
      `<p><strong>At most ${num(preview.max_model_calls)} model call${preview.max_model_calls === 1 ? "" : "s"}.</strong> ${preview.plan.stages.map(s=>`${num(s.maximum)} ${s.stage === "eval_gen" ? "scenario-generation" : s.stage === "resolve_rollback" ? "resolution-generation" : "trial"} call${s.maximum === 1 ? "" : "s"}`).join(" and ")}.</p>` +
      `<p>${resolution ? "This creates a proposed resolution for Review. You must inspect and approve its exact patch before delivery." : "This creates new evaluation evidence."} Existing decisions and instruction files stay intact. Completed checkpoints reuse their recorded results; an interrupted call is never silently repeated.</p>` +
      `<details ${reviewDisclosure("eval-source:"+job.proposalId+":"+preview.revision)}><summary>Exact source and existing evaluation</summary>${fullRecord(preview.source.snapshot)}</details>` +
      (resolution ? `<details ${reviewDisclosure("resolution-cost:"+job.proposalId+":"+preview.revision)}><summary>Exact rollback conflict to resolve</summary>${fullRecord(preview.plan.rollback)}</details>` : "") +
      (!job.commandId ? `<button id="eval-job-submit" class="btn" data-eval-submit="true" ${job.busy ? "disabled" : ""}>${job.busy ? "Recording job…" : `${resolution ? "Generate resolution proposal" : "Regenerate evaluation"} · up to ${num(preview.max_model_calls)} call${preview.max_model_calls === 1 ? "" : "s"}`}</button>` : "") : "") +
    `<button id="eval-job-refresh" class="btn" data-eval-refresh="true" ${job.busy ? "disabled" : ""}>Refresh cost preview</button>`);
}

export async function openEvalJob(proposalId, {newAttempt = false, action = "regenerate_eval"} = {}) {
  if (state.evalJob.busy) return;
  if (globalThis.history && globalThis.history.replaceState) globalThis.history.replaceState(null,"",`#/review/${action === "resolve_rollback" ? "resolution" : action === "request_reapplication" ? "reapply" : action === "mine_incident" ? "mine" : "eval"}/${encodeURIComponent(proposalId)}`);
  state.evalJob = {proposalId, action, preview: null, loading: true, busy: false, error: "", commandId: ""};
  setText("eval-job-title", action === "resolve_rollback" ? "Generate a rollback resolution" : action === "request_reapplication" ? "Review reapplication" : action === "mine_incident" ? "Mine a selected incident" : "Regenerate evaluation");
  setVisible(byId("review-eval"), true); paintEvalJob();
  const title = byId("eval-job-title");
  if (title.scrollIntoView) title.scrollIntoView({block:"start"});
  if (title.focus) title.focus({preventScroll:true});
  const entry = state.evalJob;
  try {
    entry.preview = await getJSON(action === "mine_incident" ? `/api/incidents/${encodeURIComponent(proposalId)}/mining-preview` : action === "resolve_rollback" ? `/api/proposals/${encodeURIComponent(proposalId)}/resolution-preview` : action === "request_reapplication" ? `/api/proposals/${encodeURIComponent(proposalId)}/reapplication-preview` : `/api/proposals/${encodeURIComponent(proposalId)}/eval-preview`);
    if (!newAttempt && entry.preview.latest_job) entry.commandId = entry.preview.latest_job.id;
    if (!newAttempt && entry.preview.latest_request) {
      entry.commandId=entry.preview.latest_request.id;entry.resultProposalId=entry.preview.latest_request.proposal_id;
    }
  }
  catch (error) { entry.error = String(error.message || error); }
  finally { entry.loading = false; if (state.evalJob === entry) paintEvalJob(); }
}

export async function submitEvalJob() {
  const entry=state.evalJob, preview=entry.preview;
  if (entry.busy || !preview || entry.commandId) return;
  const signature=(entry.selection ? JSON.stringify(entry.selection) : entry.proposalId)+":"+preview.revision;
  if (!state.evalRequests[signature]) state.evalRequests[signature]=crypto.randomUUID();
  entry.busy=true;entry.error="";paintEvalJob();
  try {
    const result=await postJSON(API.commands,{action:entry.action || "regenerate_eval",...(entry.action === "propose_recovery" ? entry.selection : entry.action === "mine_incident" ? {incident_id:entry.proposalId} : {proposal_id:entry.proposalId}),
      preview_revision:preview.revision,request_key:state.evalRequests[signature]});
    entry.commandId=result.id;delete state.evalRequests[signature];mergeDeliveries([result]);
    if (entry.action === "request_reapplication") { entry.resultProposalId=result.result.proposal_id;await load(); }
  } catch (error) { entry.error="The request result could not be confirmed. Retrying this request reuses its key. "+String(error.message || error); }
  finally {
    entry.busy=false;paintEvalJob();await loadDeliveryHistory();
    const focus=doc().getElementById(entry.commandId ? "eval-job-title" : "eval-job-submit");
    if (focus && focus.focus) focus.focus({preventScroll:true});
  }
}

export function parseRoute(hash) {
  const raw = String(hash === null || hash === undefined ? "" : hash).replace(/^#/, "");
  const projectQuery = /^\/projects(?:\/|\?|$)/.test(raw) && raw.includes("?") ? raw.slice(raw.indexOf("?") + 1) : "";
  const ruleQuery = /^\/rules(?:\/|\?|$)/.test(raw) && raw.includes("?") ? raw.slice(raw.indexOf("?") + 1) : "";
  const trendQuery = raw.startsWith("/evals") && raw.includes("?") ? raw.slice(raw.indexOf("?") + 1) : "";
  const path = raw.includes("?") ? raw.slice(0, raw.indexOf("?")) : raw;
  const parts = path.split("/").filter(Boolean);
  const view = parts.length ? parts[0] : "overview";
  if (VIEWS.indexOf(view) === -1) return { view: "overview", id: "", unknown: view };
  const rest = parts.slice(1).join("/");
  let id = "";
  if (rest) {
    try {
      id = decodeURIComponent(rest);
    } catch (error) {
      id = rest;
    }
  }
  return { view, id, unknown: "", ...(projectQuery ? {projectQuery} : {}), ...(ruleQuery ? {ruleQuery} : {}), ...(trendQuery ? {trendQuery} : {}) };
}

export function showRoute(route) {
  // A pending same-page filter update must not dismiss an open search modal.
  const navigationKey=JSON.stringify([route.view,route.id]);
  if(navigationKey!==navigationRoute)navigation?.close(false);
  navigationRoute=navigationKey;
  clearTimeout(state.rulesSearchTimer);state.rulesSearchTimer=null;
  const oldRuleQuery=state.ruleQuery;
  const oldReviewDetail = state.reviewDetailFamily;
  state.reviewDetailFamily = route.view === "review" && route.id.startsWith("family/") ? route.id.slice(7) : "";
  byId("view-review").classList.toggle("review-detail-mode", Boolean(state.reviewDetailFamily));
  state.route = route.view;
  const runRoute = route.view === "overview" && /^(run|night)\//.test(route.id);
  const projectRoute = route.view === "projects" && Boolean(route.id);
  setVisible(byId("project-detail"), projectRoute);
  setVisible(byId("run-detail"), runRoute);
  if (runRoute) openRunRoute(route.id);
  else state.runDetail = null;
  VIEWS.forEach((view) => {
    setVisible(byId(`view-${view}`), view === route.view && !runRoute && !projectRoute);
  });
  // Derived from VIEWS, not a second list. A hand-written pair list is how a
  // fourth view ends up visible in the router and dead in the nav.
  VIEWS.forEach((view) => {
    const link = byId(`nav-${view}`);
    if (view === route.view) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
  if (route.unknown) {
    showError(`No view named "${route.unknown}"`, `The address bar asked for #/${route.unknown}. This app has ${VIEWS.length} views: ${VIEWS.join(", ")}.`);
  }
  if (route.view === "rules") {
    state.ruleQuery = route.ruleQuery || "";
    if (route.id.startsWith("evidence/")) {const params=new URLSearchParams(state.ruleQuery);params.set("mode","evidence");state.ruleQuery=params.toString();}
    state.query = new URLSearchParams(state.ruleQuery).get("query") || "";
    if(oldRuleQuery!==state.ruleQuery)byId("rules-search-input").value=state.query;
    if (evidenceMode()) {paintEvidenceBrowser(); if(!state.initialLoading){const focus=state.evidenceFocus;state.evidenceFocus=false;loadEvidencePage({focus});}}
    else if (!state.initialLoading && (state.rules?.mode === "paged" || state.rulePage)) loadRulePage();
  }
  if (route.view === "rules" && route.id.startsWith("evidence/")) {
    const path=route.id.slice(9), split=path.indexOf("/");
    openEvidence(path.slice(0,split),path.slice(split+1));
  } else if (route.view === "rules" && route.id) {
    const params = new URLSearchParams(route.ruleQuery || "");
    openRule(route.id, params.get("tab") || undefined);
    const incident = params.get("scan");
    if (incident && state.inspectorTab === "evidence" && Array.from(doc().querySelectorAll('[data-scan-history-root]')).some(root => root.getAttribute('data-scan-history-root') === incident)) loadIncidentScanHistory(incident);
  } else if (route.view === "projects" && route.id) {
    const params = new URLSearchParams(route.projectQuery || "");
    const tab = params.get("tab") || "summary"; params.delete("tab");
    openProject(route.id, tab, params.toString());
  } else {
    closeInspector();
  }
  if (route.view === "projects" && !route.id) {
    state.projectListQuery = route.projectQuery || "";
    byId("projects-search").value = projectListOptions(state.projectListQuery).query;
    paintProjects();
    if (state.projectsFocus) {
      const focus = doc().getElementById(state.projectsFocus);
      (focus?.querySelector?.("button") || focus)?.focus?.({preventScroll:true});
      state.projectsFocus = "";
    }
  }
  const qualityRoute = route.view === "evals" && route.id.startsWith("quality/");
  const evaluationRoute = route.view === "evals" && Boolean(route.id) && !qualityRoute;
  if (!qualityRoute) state.qualityDetail=null;
  if (!evaluationRoute) state.evaluationDetail=null;
  setVisible(byId("quality-detail"), qualityRoute);
  setVisible(byId("evaluation-detail"), evaluationRoute);
  setVisible(byId("view-evals"), route.view === "evals" && !evaluationRoute && !qualityRoute);
  if (route.view === "evals") {
    state.evaluationQuery = route.trendQuery || "";
    if (qualityRoute) {
      openQuality(route.id.slice("quality/".length));
    } else if (evaluationRoute) {
      const split = route.id.indexOf("/");
      openEvaluationDetail(route.id.slice(0, split), route.id.slice(split + 1), state.evaluationQuery);
    } else {
      state.evaluationDetail = null;
      loadTrends(evalQueryParts(state.evaluationQuery).trends.toString());
      loadEvaluationHistory(state.evaluationQuery);
      loadEvaluationHealth();
      loadRecurrence(recurrenceProject());
      loadClassEvidence();
      loadQualitySamples();
    }
  } else state.evaluationDetail = null;
  const executionMatch=route.view==="review" && /^(command|operation)\/(.+)$/.exec(route.id);
  if(executionMatch)openExecutionRoute(executionMatch[1],executionMatch[2]);
  else {state.executionRoute=null;setHTML("review-execution","");}
  if (route.view === "review") {
    state.reviewRouteProposal = route.id.startsWith("proposal/") ? route.id.slice("proposal/".length) : "";
    state.reviewFocusProposal = state.reviewRouteProposal;
    paintReview();
    if (oldReviewDetail || state.reviewDetailFamily) revealSelectedCard();
    if (!state.delivery.loaded && !state.delivery.promise) loadDeliveryHistory();
    if (!state.operations.loaded && !state.operations.promise) loadOperationHistory();
    if (!state.incidents.loaded && !state.incidents.loading) loadMiningIncidents();
    const rollbackId = route.id.startsWith("rollback/") ? route.id.slice("rollback/".length) : "";
    setVisible(byId("review-rollback"), Boolean(rollbackId));
    if (rollbackId) openRollback(rollbackId);
    const resolutionId = route.id.startsWith("resolution/") ? route.id.slice("resolution/".length) : "";
    const reapplyId = route.id.startsWith("reapply/") ? route.id.slice("reapply/".length) : "";
    const mineId = route.id.startsWith("mine/") ? route.id.slice("mine/".length) : "";
    const evalId = route.id.startsWith("eval/") ? route.id.slice("eval/".length) : resolutionId || reapplyId || mineId;
    const recoveryId=route.id.startsWith("recovery/") ? route.id.slice("recovery/".length) : "";
    setVisible(byId("review-eval"), Boolean(evalId || recoveryId));
    if (recoveryId) {
      try {openRecoveryJob(JSON.parse(recoveryId));} catch(error) {showError("Invalid recovery link",String(error.message || error));}
    }
    if (evalId) openEvalJob(evalId,{action:resolutionId ? "resolve_rollback" : reapplyId ? "request_reapplication" : mineId ? "mine_incident" : "regenerate_eval"});
  }
  setHTML("navigation-breadcrumb", renderNavigationBreadcrumb(route));
  if(route.id)navigation?.record(currentNavigationLink());
}

/* Run detail is a child of Overview. An aggregate grid cell never picks a run
 * by timestamp: a night with several runs first offers every distinct ID. */
export function runHref(kind, id, stage = "") {
  return "#/overview/" + kind + "/" + encodeURIComponent(id) + (stage ? "/" + encodeURIComponent(stage) : "");
}

const RUN_RECORD_LABELS = Object.freeze({artifacts: "Retained files", calls: "Model calls", attempts: "Evaluation attempts", evaluations: "Linked evaluations", deliveries: "Applied edits", related_deliveries: "Earlier reviewed deliveries", proposals: "Created proposals", scans: "Detector observations", learnings: "Rule relationships"});

function runDisclosure(key, label, value) {
  return `<details ${reviewDisclosure(key)}><summary id="run-disclosure-${esc(key)}">${esc(label)}</summary>${fullRecord(value)}</details>`;
}

function runReadFailure(entry, options) {
  // The existing Run repaint restores controls in the run-* ID family.
  return readFailure(entry, {...options, summaryId:"run-read-error:"+options.key});
}

export function renderRunRecords(kind, entry) {
  const page = entry.pages[kind];
  if (!page) return `<button class="btn" id="run-load-${esc(kind)}" type="button" data-run-records="${esc(kind)}">Load ${esc(RUN_RECORD_LABELS[kind].toLowerCase())}</button>`;
  let html = page.reason ? `<p class="footnote">${esc(page.reason)}</p>` : "";
  if (page.error) html += runReadFailure(page, {key:"run:"+entry.id+":"+kind, title:"Could not read "+RUN_RECORD_LABELS[kind].toLowerCase()+".", guidance:(page.loaded ? "Previously read records remain below. " : "")+"Use Retry below."});
  html += `<p class="caption" id="run-status-${esc(kind)}" tabindex="-1" aria-live="polite">${page.loaded ? `${num(page.records.length)}${page.count == null ? "" : ` of ${num(page.count)}`} retained records shown` : page.loading ? "Loading records…" : "Records not loaded"}</p>`;
  if (kind === "artifacts") html += `<p class="footnote">${esc(page.coverage || "")}</p><p class="footnote">Recorded report path: <span class="mono">${esc(page.recorded_report_path || "not recorded")}</span></p>`;
  page.records.forEach((record) => {
    if (kind === "artifacts") { html += renderRunArtifact(record, entry); return; }
    if (kind === "related_deliveries") { html += renderRelatedRunDelivery(record, entry); return; }
    if (kind === "attempts") {
      html += `<article class="run-record"><a href="${esc(evaluationHref("attempt", record.id))}">${esc(record.outcome.label)} · ${esc(record.id)}</a><p>${esc(record.outcome.reason)}</p><p class="caption">${num(record.completed_scenarios)} / ${num(record.scenarios)} scenarios with results · ${num(record.calls_recorded)} / ${num(record.calls_started)} calls with retained outcomes</p>${runDisclosure("run:"+entry.id+":attempt:"+record.id,"Complete attempt summary",record)}</article>`;
      return;
    }
    if (kind === "scans") { html += renderScanObservation(record, "run-scan:" + entry.id + ":" + record.id); return; }
    let label = record.id;
    if (kind === "calls") label = `${record.stage} · ${record.outcome_description?.name || record.outcome} · ${record.model_reported || "model unknown"} · ${record.id}`;
    if (kind === "evaluations") label = `${record.verdict || "verdict not recorded"} · ${record.id}`;
    if (kind === "proposals") label = `${record.status} · ${record.id}`;
    if (kind === "deliveries") label = `${record.state} · ${record.id}`;
    html += `<article class="run-record">`;
    if (kind === "learnings") {
      label = `Complete rule observations and proposals · ${record.id}`;
      const title=String(record.learning?.title || record.learning?.rule_text || "Rule "+record.id);
      html += `<h3><a href="#/rules/${encodeURIComponent(record.id)}">${esc(title.slice(0,180))}${title.length>180?"…":""}</a></h3>`;
      html += `<p>${record.mining_recorded?`${num(record.observations.length)} retained mining observations linked to this run`:"Mining observation coverage unknown"} · ${num(record.proposals.length)} proposals created by this run</p>`;
      html += `<p>${record.observations.map(r=>esc(r.kind)+" · "+esc(r.id)).join("<br>") || "No retained mining observation linked to this run."}</p>`;
      html += record.proposals.map(p=>`<p><a href="#/review/proposal/${encodeURIComponent(p.id)}">${esc(p.target_path)} · ${esc(p.id)}</a> · current state: ${esc(p.status)}</p>`).join("");
    }
    if (kind === "calls" && record.outcome_description) html += `<p>${esc(record.outcome_description.explanation)}</p>`;
    if (record.association) html += `<p class="footnote">${esc(record.association)}</p>`;
    if (kind === "deliveries") {
      html += `<p class="mono">${esc(record.record.destination.target_path)}</p><pre class="review-card__diff">${renderUnifiedDiff(record.record.proposal.diff_unified)}</pre>`;
      html += `<p class="footnote">Snapshot before: <span class="mono">${esc(record.result.snapshot_commit_before)}</span><br>Snapshot after: <span class="mono">${esc(record.result.snapshot_commit_after)}</span></p>`;
    }
    html += runDisclosure("run:" + entry.id + ":" + kind + ":" + record.id, label, record);
    if (kind === "evaluations") {
      const attempt = (record.links || []).find(link => link.attempt_id);
      html += `<p><a href="${esc(evaluationHref(attempt ? "attempt" : "unlinked", attempt?.attempt_id || record.id))}">Inspect complete evaluation</a></p>`;
    }
    if (kind === "proposals") html += `<a href="#/review/proposal/${encodeURIComponent(record.id)}">Inspect proposal in Review</a>`;
    if (kind === "deliveries" && record.proposal_id) html += `<a href="#/review/rollback/${encodeURIComponent(record.proposal_id)}">Inspect rollback eligibility</a>`;
    html += `</article>`;
  });
  if (kind === "related_deliveries") {
    if (page.loaded && !page.records.length && !page.reason) html += `<p>No matching earlier reviewed targets were found in the retained relationship.</p>`;
    if (page.loaded || page.error) html += `<button class="btn btn--quiet" id="run-refresh-related-deliveries" data-run-records="related_deliveries" aria-disabled="${page.loading}">Refresh earlier deliveries</button>`;
  }
  if (kind !== "related_deliveries" && page.loaded && !page.records.length && !page.reason) html += `<p>${kind === "deliveries" ? "No automatic edits were recorded for this run." : "No records linked to this run."}</p>`;
  if (page.loaded && page.loading) html += `<p role="status">Loading records…</p>`;
  if (page.next_cursor || page.error) html += `<button class="btn" id="run-older-${esc(kind)}" type="button" data-run-records="${esc(kind)}" data-run-older="${(kind === "related_deliveries" && page.error ? page.retryOlder : page.loaded) ? "true" : "false"}" aria-disabled="${page.loading}">${page.error ? "Retry" : "Load more"}</button>`;
  return html;
}

function measured(value) { return value == null ? "unknown" : num(value); }

function renderRunFlow(data) {
  const flow=data.flow;
  if (!flow) return "";
  return `<details class="run-timing" ${reviewDisclosure("run:"+data.run.id+":rule-flow")}><summary id="run-rule-flow-summary">Rules, proposals and duplicate handling</summary><div class="panel__body">`+
    `<p>A learning is a reusable rule. A proposal is one edit to one file. A rule can have zero or several proposals; these stages are not a one-to-one funnel.</p>`+
    `<p>${measured(flow.observation_count)} retained mining observations of ${measured(flow.observed_learning_count)} rules. ${num(flow.created_proposal_count)} proposals were created by this run for ${num(flow.proposal_learning_count)} rules.</p>`+
    `<p>${measured(flow.observed_without_proposal_count)} observed rules have no proposal created by this run. ${num(flow.multiple_proposal_learning_count)} rules have multiple proposals created here.</p>`+
    (flow.reason?`<p>${esc(flow.reason)}</p>`:"")+
    `<p>Observation kinds: ${flow.observation_kinds?esc(Object.entries(flow.observation_kinds).map(([kind,n])=>kind+": "+n).join(" · ") || "No retained observations"):"unknown"}.</p>`+
    `<p>${esc(flow.note)}</p><p>Candidate rules and duplicate drops are separate from merge calls and gate outcomes. Inspect the cluster and gate records for pending-rule absorption and duplicate channels.</p>`+
    `<button class="btn" id="run-inspect-learnings" data-run-section="learnings" data-run-records="learnings">Inspect exact rule and proposal links</button></div></details>`;
}

function renderStageRecord(runId, stage) {
  const a=stage.accounting || {};
  let note="";
  if (a.state==="unaccounted") note=a.unaccounted>0?`${num(a.unaccounted)} attempts have no recorded outcome.`:`Recorded outcomes exceed attempts by ${num(-a.unaccounted)}.`;
  if (a.state==="unreadable") note=`${a.reason || "Accounting is incomplete."} ${a.missing_fields?.length?"Missing: "+a.missing_fields.join(", "):""}`;
  return (note?`<p class="caption"><strong>${esc(note)}</strong> No cause is inferred.</p>`:"")+
    `<details ${reviewDisclosure("run:"+runId+":stage:"+stage.name)}><summary id="run-disclosure-run:${esc(runId)}:stage:${esc(stage.name)}">Complete stage record</summary>`+
    `<p>${esc(a.meaning || "Native outcome definitions were not supplied.")}</p>`+
    (a.unit?`<p>Attempt unit: ${esc(a.unit)}. The figure in the history grid counts ${esc(a.number_label)}.</p>`:"")+
    (a.refused_in_failed?`<p>${num(a.refused_in_failed)} entries in the failed bucket are recorded budget refusals.</p>`:"")+
    `<p>Stage causes can include routing or other work outside this stage's attempt total. Do not add them to outcome buckets.</p>`+
    fullRecord(stage.payload)+`</details>`;
}

function renderRunStageCounts(stage) {
  const a = stage.accounting || {};
  const known = value => Number.isSafeInteger(value) && value >= 0;
  // Use the reader's native accounting. A missing/old shape is not a zero;
  // held deliveries and refused mining attempts are not completed work.
  const absent = stage.recorded ? "unknown" : "Not recorded";
  const count = value => known(value) ? num(value) : absent;
  const completed = stage.name === "apply" ? a.applied : a.succeeded;
  const failed = stage.name === "mine"
    ? known(a.failed) && known(a.refused_in_failed) ? a.failed-a.refused_in_failed : null
    : a.failed;
  let other = "—";
  if (!known(a.attempted)) other = absent;
  else if (stage.name === "apply") other = "held: " + count(a.held);
  else if (stage.name === "mine" || stage.name === "gate") other = "refused: " + count(stage.name === "mine" ? a.refused_in_failed : a.refused);
  return [["input",count(a.attempted)],["completed",count(completed)],["failed",count(failed)],["other",other]]
    .map(([key,value])=>`<td class="run-count" data-run-count="${key}">${value === absent ? `<span class="run-count-label">${esc(value)}</span>` : esc(value)}</td>`).join("");
}

function renderRunStageContext(stage) {
  const a = stage.accounting || {}, payload = stage.payload || {};
  const parts = [];
  if (stage.name === "cluster" && Number.isSafeInteger(a.number)) parts.push(`${num(a.number)} candidate rules${a.passthrough ? " · no merge calls (agentic pass-through)" : ""}`);
  if (stage.name === "gate" && Number.isSafeInteger(a.succeeded)) {
    const verdicts = a.verdicts || {};
    parts.push(`${num(a.succeeded)} verdicts · `+["gated_pass","gated_fail","ungated","inconclusive"]
      .map(key=>`${key.replace("gated_","")}: ${measured(verdicts[key])}`).join(" · "));
  }
  if (stage.name === "scan" && Number.isSafeInteger(a.files_skipped_unchanged)) parts.push(`${num(a.files_skipped_unchanged)} unchanged files skipped`);
  if (stage.name === "apply" && Number.isSafeInteger(a.applied)) parts.push(`${num(a.applied)} applied edits · held proposals are not writes`);
  // Legacy/incomplete records still expose each retained counter in the full record.
  if (stage.recorded && !Object.keys(payload).length) parts.push("No numeric counters recorded");
  return parts.map(part=>`<p class="caption">${esc(part)}</p>`).join("");
}

function renderRunInspection(data) {
  const info = data.inspection || {pipeline_pools: [], jobs: [], policy_waits: null, wall_clock: null};
  let html = `<details class="run-timing run-budget" ${reviewDisclosure("run:"+data.run.id+":budget-panel")}><summary id="run-budget-summary">Call budgets and caps</summary><div class="panel__body">`;
  if (!info.pipeline_pools.length) html += `<p>Budget limits were not recorded for this run.</p>`;
  else {
    html += `<div class="scroll-x"><table class="run-budget-table"><thead><tr><th>Pool</th><th>Used / limit</th><th>Refused calls</th></tr></thead><tbody>`;
    info.pipeline_pools.forEach(pool => { html += `<tr><th scope="row">${esc(pool.pool)}</th><td>${measured(pool.used)} / ${measured(pool.limit)}</td><td>${measured(pool.refused)}</td></tr>`; });
    html += `</tbody></table></div>`;
  }
  info.jobs.forEach(job => {
    html += `<h3>Reserved job budget · <a href="#/review/command/${encodeURIComponent(job.id)}">${esc(job.id)}</a></h3><p>${esc(job.action.replaceAll("_"," "))} · ${esc(job.state)} · ${num(job.completed_calls)} completed calls · ${num(job.unresolved_calls)} unresolved reservations</p><div class="run-pools">`;
    Object.entries(job.budget.maximum).forEach(([pool,max]) => { html += `<p><strong>${esc(pool)}</strong> ${num(job.budget.consumed[pool])} / ${num(max)} reserved · ${num(job.budget.remaining[pool])} remaining</p>`; });
    html += `</div><p class="footnote">Unresolved reservations consume the frozen allowance. Reloading or retrying does not replenish it.</p>${runDisclosure("run:"+data.run.id+":job:"+job.id,"Job budget and recorded causes",job)}`;
  });
  html += `<p class="footnote">Provider token usage can be missing even for completed calls. Stored totals may be incomplete; a stored zero does not prove zero consumption.</p>`;
  html += `<p class="caption">Recorded policy waits: ${measured(info.policy_waits)}</p>`;
  html += runDisclosure("run:"+data.run.id+":budgets","Recorded usage, refusals, and waits",data.stats.llm || {})+`</div></details>`;
  const clock=info.wall_clock;
  html += `<details class="run-timing" ${reviewDisclosure("run:"+data.run.id+":timing")}><summary id="run-timing-summary">Time accounting</summary>`;
  if (!clock) html += `<p>Wall/model decomposition was not recorded.</p>`;
  else html += `<div class="run-pools"><p>Wall: <strong>${measured(clock.wall_seconds)} s</strong></p><p>Model: <strong>${measured(clock.model_seconds)} s</strong></p><p>Unaccounted: <strong>${measured(clock.unaccounted_seconds)} s</strong></p><p>Largest gap: <strong>${measured(clock.largest_gap_seconds)} s</strong></p></div><p>Unaccounted time includes scan, setup and pauses; it is not a measured sleep duration.</p>${fullRecord(clock)}`;
  return html+`</details>`;
}

export function renderRunUsage(data) {
  const pools = data.inspection?.pipeline_pools || [];
  let html = `<section class="run-usage" aria-label="Recorded call and token usage">`;
  if (!pools.length) html += `<div class="run-usage__pool"><span>Pipeline budget not recorded</span></div>`;
  for (const pool of pools) {
    const known = Number.isSafeInteger(pool.used) && pool.used >= 0 && Number.isSafeInteger(pool.limit) && pool.limit >= 0;
    const over = known && pool.used > pool.limit;
    const note = pool.limit === 0 ? `No calls permitted${!known ? " · usage unknown" : over ? ` · ${num(pool.used)} over limit` : ""}` : !known ? "Usage ratio unknown" :
      over ? `${num(pool.used - pool.limit)} over limit` : pool.used === pool.limit ? "Budget exhausted" : "";
    html += `<div class="run-usage__pool" data-pool="${esc(pool.pool)}"><span>${esc(pool.pool)} calls</span><span>${measured(pool.used)} / ${measured(pool.limit)}</span>` +
      (known && pool.limit > 0 ? `<span class="meter" aria-hidden="true" data-exhausted="${pool.used >= pool.limit}"><span class="meter__fill" style="--pct:${Math.min(100, 100 * pool.used / pool.limit)}"></span></span>` : `<span class="run-usage__gap">—</span>`) +
      ((note || pool.refused > 0) ? `<span class="run-usage__note">${esc(note)}${note && pool.refused > 0 ? " · " : ""}${pool.refused > 0 ? num(pool.refused)+" refused" : ""}</span>` : "") + `</div>`;
  }
  const calls = data.calls || {};
  const knownTokens = calls.recorded > 0 && [calls.tokens_in,calls.tokens_out].every(n => Number.isSafeInteger(n) && n >= 0);
  const total = knownTokens ? calls.tokens_in + calls.tokens_out : 0;
  html += `<div class="run-usage__tokens"><span>${calls.recorded ? `Stored token totals: ${measured(calls.tokens_in)} in / ${measured(calls.tokens_out)} out` : "Token usage not recorded · share unavailable"}</span>`;
  if (knownTokens && Number.isSafeInteger(total) && total > 0) {
    html += `<span class="token-share" aria-hidden="true"><span data-direction="in" style="--share:${100*calls.tokens_in/total}"></span><span data-direction="out" style="--share:${100*calls.tokens_out/total}"></span></span>`;
  } else if (calls.recorded) html += `<span>Token share unavailable</span>`;
  return html + `<span class="run-usage__caution">Share has no token cap; totals may be incomplete.</span></div></section>`;
}

export function renderRunStageState(state = "unknown_status") {
  const symbols = {ok:"✓",partial:"!",failed:"×",error:"×",running:"…",skipped:"—",degraded:"!",
    interrupted:"■",abandoned:"■",budget_exhausted:"□",refused:"□",unaccounted:"?",limited:"−",info:"i"};
  const label = Object.hasOwn(GRID_LABELS,state) ? GRID_LABELS[state] : state === "unreadable" ? "Record unreadable" : "Outcome unknown";
  const meaning = Object.hasOwn(STATE_MEANING,state) ? stateTitle(state) : `${state}: the recorded outcome is not recognized; inspect the stage details`;
  return `<span class="run-stage-state" title="${esc(meaning)}"><span class="run-dot" data-state="${esc(state)}" aria-hidden="true">${esc(Object.hasOwn(symbols,state) ? symbols[state] : "?")}</span><span>${esc(label)}</span></span>`;
}

function renderRunCaps(data) {
  const caps = data.scan_summary?.counter_maps?.dropped_by_cap || data.stats.scan?.dropped_by_cap;
  if (!caps || !Object.values(caps).some(value => typeof value === "number" && value > 0)) return "";
  const counts = Object.entries(caps).map(([kind,count]) => `${kind.replaceAll("_"," ")}: ${num(count)}`).join(" · ");
  return `<aside class="run-notice"><div class="run-cap-heading"><h2 class="section__title">Scan selection was capped</h2><p class="run-cap-counts">${esc(counts)}</p></div><details ${reviewDisclosure("run:"+data.run.id+":caps")}><summary>Recorded omissions by cap</summary><p>These detector candidates were omitted from the miner queue. Counts do not predict future processing time.</p>${fullRecord(caps)}</details></aside>`;
}

function renderRunDelivery(entry) {
  const page = entry.pages.deliveries;
  let heading = "Loading applied edits…";
  if (page?.loaded) heading = page.count > 0 ? `${num(page.count)} automatic edits recorded` : page.reason ? "Applied edits not established" : "No automatic edits recorded";
  else if (page?.error) heading = "Applied edits unavailable";
  let content = renderRunRecords("deliveries", entry);
  if (page?.loaded && page.count > 0) {
    const notice = (page.reason ? `<p class="footnote">${esc(page.reason)}</p>` : "") + (page.error ? runReadFailure(page, {key:"run:"+entry.id+":delivery-notice", title:"Could not read applied edits.", guidance:"Previously read edits remain in the disclosure. Open it and use Retry."}) : "");
    content = notice + `<details id="run-delivery-records" ${reviewDisclosure("run:"+entry.id+":delivery-records")}><summary id="run-delivery-summary">Inspect applied edits and snapshots</summary>${content}</details>`;
  }
  return `<section class="panel run-delivery" id="run-section-deliveries" tabindex="-1"><header class="panel__head"><h2 class="panel__title" id="run-delivery-heading">${esc(heading)}</h2></header><div class="panel__body">${content}</div></section>`;
}

function renderRelatedRunDelivery(record, entry) {
  const key = "run:"+entry.id+":earlier:"+record.id;
  const titles = record.members.filter(member=>member.observation_ids.length).map(member=>member.snapshot.learning.title || member.snapshot.learning.rule_text);
  return `<article class="run-record">`+
    `<h3><span class="badge" data-state="ok">Delivered</span><a href="#/review/command/${encodeURIComponent(record.command_id)}">${esc(titles.join(" · ") || "Reviewed command "+record.command_id)}</a></h3>`+
    `<p class="caption">Command <span class="mono">${esc(record.command_id)}</span> · delivered ${esc(record.completed_at)} · current command state: ${esc(record.command_state)}</p>`+
    `<p class="mono">${esc(record.target.destination.target_path)}</p>`+
    `<pre class="review-card__diff" id="run-earlier-diff-${esc(record.id)}" tabindex="0" role="region" aria-label="Complete combined delivered diff">${renderUnifiedDiff(record.target.diff_unified)}</pre>`+
    `<p class="footnote">Snapshot before: <span class="mono">${esc(record.snapshot_before)}</span><br>Snapshot after: <span class="mono">${esc(record.snapshot_after)}</span></p>`+
    record.members.map(member=>`<p><strong>${esc(member.snapshot.learning.title || member.snapshot.learning.rule_text)}</strong><br>`+
      `${member.observation_ids.length ? "Same learning observed by this run" : "Other member of this combined delivery"} · <span class="mono">${esc(member.snapshot.learning.id)}</span><br>`+
      `<a href="#/review/rollback/${encodeURIComponent(member.proposal_id)}">Inspect rollback eligibility for this contribution</a></p>`).join("")+
    runDisclosure(key,"Complete combined delivery, frozen members and relationship evidence",record)+`</article>`;
}

function renderRunRelatedDeliveries(entry) {
  return `<section class="panel run-related-deliveries" id="run-section-related_deliveries" tabindex="-1"><header class="panel__head"><h2 class="panel__title">Related earlier reviewed deliveries</h2></header><div class="panel__body">`+
    `<p>These reviewed applications preceded this run. A frozen member has the same learning ID as a retained mining observation from this run. They are separate from this run's automatic edits and stage totals.</p>`+
    `<p class="footnote">Unlinked historical writes are not included. This association does not establish receipt, violation or benefit. Rollback requires a fresh Review and can conflict with later edits.</p>`+
    renderRunRecords("related_deliveries",entry)+`</div></section>`;
}

function renderRunEvidenceNavigation() {
  return `<nav class="run-jumps" aria-label="Run evidence">` + Object.entries(RUN_RECORD_LABELS).map(([kind,label]) =>
    `<button class="btn btn--quiet" data-run-section="${kind}">${esc(label)}</button>`).join("") + `</nav>`;
}

function renderRunArtifact(record, entry) {
  const result=entry.artifactDetails?.[record.key];
  let html=`<article class="run-record"><h3>${esc(record.label)}</h3><p>${(result?.data || record.state === "available") ? num(result?.data?.bytes ?? record.bytes)+" bytes in the selected bundle" : esc(record.reason)}</p>`;
  // The server revalidates every read. A missing file can be retried after it is copied.
  html+=`<button class="btn" id="artifact-load-${esc(record.key)}" data-run-artifact="${esc(record.key)}" ${result?.loading ? "disabled" : ""}>${result?.loading ? "Inspecting…" : result?.data ? "Refresh inspection" : "Inspect file"}</button>`;
  if (result?.error) html+=runReadFailure(result,{key:"run:"+entry.id+":artifact:"+record.key,title:"Could not read this file.",guidance:"Use Inspect file above to retry."});
  if (result?.data) {
    const data=result.data;
    html+=`<div id="artifact-result-${esc(record.key)}" tabindex="-1"><p class="caption">${num(data.preview_bytes)} of ${num(data.bytes)} bytes shown · ${esc(data.encoding)}${data.omitted_bytes ? " · "+num(data.omitted_bytes)+" bytes omitted from preview" : ""}</p><pre id="artifact-text-${esc(record.key)}" class="run-artifact-text" tabindex="0" aria-label="Artifact text preview">${esc(data.text)}</pre><a class="btn" href="${esc(API.runs+"/"+encodeURIComponent(entry.id)+"/artifacts/"+encodeURIComponent(record.key)+"/download?version="+encodeURIComponent(data.version))}" download>Download complete file</a></div>`;
  }
  return html+`</article>`;
}

export async function inspectRunArtifact(key) {
  const entry=state.runDetail;
  if (!entry?.data || !entry.pages.artifacts?.records.some(record=>record.key === key)) return;
  entry.artifactDetails ||= {};
  if (entry.artifactDetails[key]?.loading) return;
  const result={loading:true,error:"",data:null};entry.artifactDetails[key]=result;paintRunDetail();
  try {
    const data=await getJSON(API.runs+"/"+encodeURIComponent(entry.id)+"/artifacts/"+encodeURIComponent(key));
    if (state.runDetail !== entry) return;
    if (data.run_id !== entry.id || data.key !== key) throw new Error("The artifact identifies a different run or file.");
    result.data=data;
  } catch(error) {result.error=String(error.message || error);result.errorDetail=error?.readDetail || "";}
  finally {
    result.loading=false;
    if (state.runDetail === entry) {paintRunDetail();doc().getElementById((result.data ? "artifact-result-" : "artifact-load-")+key)?.focus();}
  }
}

export function renderRunBacklog(entry) {
  const read = entry.backlog || {}, data = read.data;
  let body = "", summary = read.loading ? "Loading queue history…" : read.error ? "Queue history unavailable." : "Queue history has not been read.";
  if (read.error) body += runReadFailure(read, {key:"run:"+entry.id+":backlog", title:"Could not read queue history.", guidance:data ? "The previously read snapshot remains below. Use Retry to read again." : "Use Retry to read the retained queue snapshot."});
  if (data) {
    const snapshot = data.snapshot;
    summary = data.queue_count === null ? "Historical queue size unavailable." : `<strong>${num(data.queue_count)} unmined</strong> in the global queue at ${snapshot.phase === "finish" ? "the recorded finish" : "run start"}.`;
    if (data.rates) {
      const rate = value => Number(value).toLocaleString(undefined, {maximumFractionDigits:2});
      summary += ` Observed ${rate(data.rates.admissions_per_day)} admissions and ${rate(data.rates.processed_per_day)} processed exits per day over seven complete UTC days.`;
      if (data.scenario?.state === "shrinking") summary += ` Net drain ${rate(data.rates.net_drain_per_day)} per day; about <strong>${num(data.scenario.days)} days</strong> to drain only if these observed rates continue.`;
      else if (data.scenario?.state === "empty") summary += " The recorded queue is empty.";
      else summary += ` The queue is ${esc(data.scenario?.state || "not shrinking")}; no finite drain estimate at these rates.`;
    } else if (snapshot) summary += " Comparable rates and a drain estimate are unavailable.";
  }
  body += `<div class="run-backlog__heading"><p class="run-backlog__summary"${!data ? ' role="status"' : ''}>${summary}</p>`+
    `<button class="btn btn--quiet" id="run-backlog-refresh" data-run-backlog="true" aria-disabled="${Boolean(read.loading)}">${read.error ? "Retry" : "Refresh queue history"}</button></div>`;
  if (data) {
    body += `<details ${reviewDisclosure("run:"+entry.id+":backlog-settings")}><summary id="run-backlog-settings">Sampling, coverage and complete queue evidence</summary>`;
    (data.reasons || []).forEach(reason=>{body += `<p>${esc(reason)}</p>`;});
    if (data.snapshot) {
      const snapshot=data.snapshot, settings=snapshot.settings;
      body += `<p>Global snapshot: ${esc(snapshot.observed_at)}. ${snapshot.phase === "start" ? "No terminal queue snapshot was retained. " : "This is the first retained finish observation, before any later report failure. "}Both rates use ${esc(data.window.start)} ≤ observation time &lt; ${esc(data.window.end)}; the partial snapshot day is excluded.</p>`+
        `<p>Mining order: ${esc(settings.mine_order)}. Mining filter: ${esc(settings.project_filter || "all projects")}. ${settings.dry_run ? "Dry run; no model processing requested." : "Mining requested within the recorded call caps."}</p>`+
        `<p>Recorded call caps: cheap ${num(settings.cheap_call_cap)}, strong ${num(settings.strong_call_cap)}, gate ${num(settings.gate_call_cap)}. Call slots are not incident throughput.</p>`+
        `<p>Window observations: ${num(data.admissions)} admissions, ${num(data.processed_exits)} actual processed exits, ${num(data.adjustments.window)} maintenance or unattributed changes; ${num(data.adjustments.through_snapshot)} further changes through the snapshot. Rates include all queue producers and mining paths, including selected incident jobs.</p>`+
        `<p>These are queue admission times, separate from transcript-event arrivals. A processed exit does not measure lesson quality. No nightly schedule or future capacity is assumed.</p>`;
    }
    body += fullRecord(data)+`</details>`;
  }
  return `<section class="panel run-backlog" id="run-section-backlog" aria-label="Historical global queue backlog"><div class="panel__body">${body}</div></section>`;
}

export async function loadRunBacklog() {
  const entry=state.runDetail;
  if (!entry?.data || entry.backlog?.loading) return;
  const read=entry.backlog ||= {data:null,loading:false,error:""};
  read.loading=true;read.error="";read.errorDetail="";paintRunDetail();
  try {
    const data=await getJSON(API.runs+"/"+encodeURIComponent(entry.id)+"/backlog");
    if (state.runDetail !== entry) return;
    if (data.run_id !== entry.id || data.profile !== "run-backlog/1") throw new Error("The queue history identifies a different run or profile.");
    read.data=data;
  } catch(error) {read.error=String(error.message || error);read.errorDetail=error?.readDetail || "";}
  finally {read.loading=false;if(state.runDetail === entry)paintRunDetail();}
}

export function renderRunDetail(entry) {
  let html = `<nav class="run-breadcrumb" aria-label="Breadcrumb"><a href="#/overview">Overview</a>`;
  if (entry.data) html += ` / <a href="${esc(runHref("night", entry.data.run.started.slice(0, 10), entry.stage))}">${esc(entry.data.run.started.slice(0, 10))} UTC</a>`;
  html += ` / ${entry.kind === "night" ? "Choose a run" : "Run detail"}</nav>`;
  if (entry.loading) return html + `<p role="status">Loading run…</p>`;
  if (entry.error) return html + runReadFailure(entry,{key:"run:"+entry.id,title:entry.kind === "night" ? "Could not read runs for this day." : "Could not read this run.",guidance:"Use Retry below."})+`<button class="btn" data-run-refresh="true">Retry</button>`;
  if (entry.kind === "night") {
    html += `<h1 class="view__title" id="run-title" tabindex="-1">Runs on ${esc(entry.id)} UTC</h1><p>Select the exact run. Times can be identical; IDs are distinct.</p><ul class="run-choices">`;
    entry.night.records.forEach((run) => { html += `<li><a href="${esc(runHref("run", run.id, entry.stage))}">${esc(run.started)} · ${esc(run.status)} · <span class="mono">${esc(run.id)}</span></a></li>`; });
    return html + `</ul>` + (entry.night.count ? "" : `<p>No run was recorded for this UTC day.</p>`);
  }
  const data = entry.data, run = data.run;
  const elapsed = run.finished ? Date.parse(run.finished) - Date.parse(run.started) : NaN;
  const duration = Number.isFinite(elapsed) && elapsed >= 0 ? `${num(Math.round(elapsed / 1000))} seconds wall time` : "duration not recorded";
  html += `<header class="view__head run-heading"><div class="run-heading__summary"><h1 class="view__title" id="run-title" tabindex="-1">Run · ${esc(run.started.slice(0, 10))}</h1>`;
  html += `<p class="mono">${esc(run.id)}</p><p class="view__desc">${esc(run.started)} → ${esc(run.finished || "completion not recorded")} · ${esc(duration)}</p>`;
  html += `<p class="view__desc">Status: ${esc(run.status)}${data.stats.review_only === true ? " · review only" : ""}${data.stats.dry_run === true ? " · dry run" : ""}</p></div>`;
  html += `<div class="run-heading__meta"><label for="run-selector">Run on this UTC day</label><select id="run-selector">`;
  data.night_runs.forEach((other) => { html += `<option value="${esc(other.id)}"${other.id === run.id ? " selected" : ""}>${esc(other.id)} · ${esc(other.started)} · ${esc(other.status)}</option>`; });
  html += `</select><button class="btn btn--quiet" id="run-refresh" data-run-refresh="true">Refresh run</button></div></header>`;
  html += `<section class="panel"><header class="panel__head"><h2 class="panel__title">Pipeline stages</h2><span class="panel__meta">Recorded outcomes in each stage’s own unit</span></header><div class="scroll-x" id="run-stage-scroll" tabindex="0" role="region" aria-label="Stage counts and complete records"><table class="run-stage-table"><thead><tr><th scope="col">Stage</th><th scope="col">Input units</th><th scope="col">Completed</th><th scope="col">Failures</th><th scope="col">Held / other</th><th scope="col">What happened</th></tr></thead><tbody>`;
  data.stages.forEach((stage) => {
    const causes = stage.causes || [];
    const unit = {scan:"file scans",mine:"incidents",cluster:"merge calls",gate:"evaluations",apply:"deliveries"}[stage.name] || "unit unknown";
    html += `<tr id="run-stage-${esc(stage.name)}" tabindex="-1"${entry.stage === stage.name ? ' aria-selected="true"' : ""}><th scope="row">${esc(stage.name)} <span class="run-stage-unit" title="${esc(stage.accounting?.unit || "Unit not recorded")}">· ${esc(unit)}</span>${renderRunStageState(stage.state)}</th>${renderRunStageCounts(stage)}<td>${renderRunStageContext(stage)}`;
    if (causes.length) {
      html += `<p>${causes.slice(0,2).map(c=>`${esc(c.name)}: ${num(c.count)}`).join(" · ")}${causes.length>2?` · ${num(causes.length-2)} more below`:""}</p>`;
      html += `<details ${reviewDisclosure("run:"+run.id+":causes:"+stage.name)}><summary id="run-causes-${esc(stage.name)}">Read all recorded causes</summary>`+
        causes.map(c=>`<p><strong>${esc(c.name)} · ${num(c.count)}</strong><br>${esc(c.explanation)}<br><code>${esc(c.class)}</code></p>`).join("")+`<p class="caption">Stage entries can overlap call outcomes. These are not unique incident counts.</p></details>`;
    }
    html += stage.recorded ? renderStageRecord(run.id, stage) : "No stage measurement was retained; this is not a zero result.";
    html += `</td></tr>`;
  });
  html += `</tbody></table></div></section>`;
  html += renderRunUsage(data);
  if (data.calls.reason) html += `<p class="run-notice">${esc(data.calls.reason)} Retained: ${num(data.calls.recorded)}; reported: ${data.calls.reported === null ? "unknown" : num(data.calls.reported)}.</p>`;
  html += renderRunCaps(data) + renderRunBacklog(entry) + renderRunDelivery(entry) + renderRunRelatedDeliveries(entry) + renderRunFlow(data) + renderRunEvidenceNavigation();
  html += renderRunInspection(data);
  html += `<details class="run-timing" ${reviewDisclosure("run:"+run.id+":scan-coverage")}><summary id="run-scan-coverage-summary">Scan coverage</summary>${renderScanSummary(data.scan_summary)}</details>`;
  const availability = data.availability_summary;
  html += `<details class="run-timing" ${reviewDisclosure("run:"+run.id+":availability-coverage")}><summary id="run-availability-coverage-summary">Working-copy rule availability</summary>`;
  if (!availability || !availability.recorded) html += `<p>${esc(availability?.reason || "No availability collection was retained.")}</p>`;
  else {
    html += `<p>Collection: ${esc(availability.status)} · ${esc(availability.observed_at)}</p>`;
    availability.metrics.forEach(metric => { html += `<p>${esc(metric.label)}: ${num(metric.count)}</p>`; });
    html += `<p>Outcomes: ${esc(Object.entries(availability.outcomes).map(([key, value]) => `${key}: ${num(value)}`).join(" · ") || "No checks recorded")}</p>`;
    html += `<h3>Availability coverage and failures by cause</h3><p>${esc(Object.entries(availability.causes).map(([key, value]) => `${key}: ${num(value)}`).join(" · ") || "No coverage failures or unavailable matches recorded")}</p>`;
    html += runDisclosure("run:" + run.id + ":availability", "Complete availability collection", data.stats.availability);
    html += `<p>${esc(availability.meaning)}</p>`;
  }
  html += `</details>`;
  Object.entries(RUN_RECORD_LABELS).filter(([kind]) => !["deliveries", "related_deliveries"].includes(kind)).forEach(([kind, label]) => {
    html += `<section class="panel" id="run-section-${esc(kind)}" tabindex="-1"><header class="panel__head"><h2 class="panel__title">${esc(label)}</h2></header><div class="panel__body">${renderRunRecords(kind, entry)}</div></section>`;
  });
  html += `<section class="panel"><div class="panel__body">${runDisclosure("run:" + run.id + ":raw", "Full run statistics and artifact paths", data.stats)}<p class="footnote">${esc(data.provenance_note)}</p></div></section>`;
  return html;
}

function paintRunDetail() {
  if (state.runDetail) preserveOperationView("run-detail", () => renderRunDetail(state.runDetail));
}

export async function openRunRoute(route) {
  const [kind, id, stage = ""] = route.split("/");
  const entry = {route, kind, id, stage, loading: true, error: "", data: null, night: null, pages: {}};
  state.runDetail = entry;
  paintRunDetail();
  try {
    if (!id || !["run", "night"].includes(kind) || route.split("/").length > 3 || (stage && !["scan", "mine", "cluster", "gate", "apply"].includes(stage))) throw new Error("Invalid run address.");
    const data = await getJSON(API.runs + (kind === "night" ? "?day=" : "/") + encodeURIComponent(id));
    if (state.runDetail !== entry) return;
    if (kind === "night") entry.night = data;
    else {
      if (data.run.id !== id) throw new Error("The response identifies a different run.");
      entry.data = data;
    }
  } catch (error) { entry.error = String(error.message || error); entry.errorDetail = error?.readDetail || ""; }
  finally {
    entry.loading = false;
    if (state.runDetail === entry) {
      paintRunDetail();
      const focus = doc().getElementById(stage && entry.data ? "run-stage-" + stage : "run-title");
      if (focus && focus.focus) focus.focus();
      if (entry.data) {
        await loadRunRecords("deliveries");
        if (state.runDetail === entry) await loadRunRecords("related_deliveries");
        if (state.runDetail === entry) await loadRunBacklog();
      }
    }
  }
}

export async function loadRunRecords(kind, older = false) {
  const entry = state.runDetail;
  if (!entry || !entry.data || !(kind in RUN_RECORD_LABELS)) return;
  const page = entry.pages[kind] || {records: [], loaded: false, count: 0, reason: "", next_cursor: null, error: "", loading: false};
  if (page.loading) return;
  // This panel can refresh an already loaded page. Retry the failed request's
  // mode; a failed refresh must never append page one to retained records.
  const append = older && (kind !== "related_deliveries" || Boolean(page.next_cursor));
  if (kind === "related_deliveries") page.retryOlder = append;
  page.loading = true; page.error = ""; page.errorDetail = ""; entry.pages[kind] = page;
  paintRunDetail();
  try {
    const url = API.runs + "/" + encodeURIComponent(entry.id) + (kind === "artifacts" ? "/artifacts?limit=20" : kind === "related_deliveries" ? "/related-deliveries?limit=20" : "/records?kind=" + kind + "&limit=20") + (older && page.next_cursor ? "&cursor=" + encodeURIComponent(page.next_cursor) : "");
    const data = await getJSON(url);
    if (state.runDetail !== entry) return;
    if (data.run_id !== entry.id || (kind !== "artifacts" && data.kind !== kind)) throw new Error("The records identify a different run or record kind.");
    Object.assign(page, data, {records: append ? [...page.records, ...data.records] : data.records, loaded: true});
  } catch (error) { page.error = String(error.message || error); page.errorDetail = error?.readDetail || ""; }
  finally { page.loading = false; if (state.runDetail === entry) paintRunDetail(); }
}

/* ------------------------------ inspector -------------------------------- */

export function renderMiningHistory(row) {
  const history=state.miningHistories[row.id] || {records:[],loading:false,loaded:false,error:"",nextCursor:null};
  return `<p>Content generation and later evidence are separate events. Incident time is shown inside each retained record.</p>` +
    `<button id="mining-history-load-${esc(row.id)}" class="btn" data-mining-history="${esc(row.id)}" aria-disabled="${Boolean(history.loading)}">${history.loading ? "Loading mining history…" : history.loaded ? "Refresh mining history" : "Load mining history"}</button>` +
    (history.error ? readFailure(history,{key:"mining:"+row.id,summaryId:"mining-history-error-"+row.id,title:"Could not read mining history.",
      guidance:(history.loaded ? "Showing the previous mining history. " : "")+"Use the mining history button to retry."}) : "") +
    (history.loaded && !history.records.length ? `<p>No mining history was recorded for this learning. Its historical generation remains unknown.</p>` : "") +
    history.records.map(r=>`<article class="delivery-command"><h4>${esc(r.kind === "new" ? "Created content" : r.kind === "cluster_merge" ? "Merged content" : r.kind.startsWith("amend") ? "Amended content" : "Linked evidence")}</h4>` +
      `<p>${esc(r.generation || "Generation not recorded")} · ${esc(r.created_at)}</p>` +
      `<p>Run: <span class="id">${esc(r.run_id || "not recorded")}</span><br>Reported model: ${esc(r.call && r.call.model_reported || "not recorded")}</p>` +
      `<details ${reviewDisclosure("mining-history:"+r.id)}><summary>Complete content, source evidence, and call record</summary>${fullRecord(r)}</details></article>`).join("") +
    (history.nextCursor ? `<button id="mining-history-older-${esc(row.id)}" class="btn" data-mining-history="${esc(row.id)}" data-mining-history-older="true" aria-disabled="${Boolean(history.loading)}">Load older mining history</button>` : "");
}

export async function loadRuleMiningHistory(learningId,{older=false}={}) {
  const old=state.miningHistories[learningId];if(old && old.loading)return;
  const focusId=doc().activeElement && doc().activeElement.id;
  const history=old || {records:[],loaded:false,nextCursor:null,error:""};
  if(older && !history.nextCursor)return;
  history.loading=true;history.error="";history.errorDetail="";state.miningHistories[learningId]=history;
  const paint=()=>{
    const row=state.ruleDetails[learningId]?.data || ((state.rules || {}).rows || []).find(r=>r.id===learningId);
    if(row && state.inspectorKind==="rule" && state.selectedRule===learningId && state.inspectorTab==="provenance")preserveOperationView("inspector-body",()=>renderRuleDetail(row,"provenance"));
  };
  paint();
  try {
    const page=await getJSON(`/api/learnings/${encodeURIComponent(learningId)}/mining-history?limit=20`+(older ? `&cursor=${encodeURIComponent(history.nextCursor)}` : ""));
    const records=older ? [...history.records,...page.records] : page.records;
    history.records=[...new Map(records.map(r=>[r.id,r])).values()];history.nextCursor=page.next_cursor;history.loaded=true;
  } catch(error){history.error=String(error.message || error);history.errorDetail=error?.readDetail || "";}
  finally{
    const canRestore=focusId && doc().activeElement?.id===focusId;
    history.loading=false;paint();
    if(canRestore && focusId.startsWith("mining-history-") && state.inspectorKind==="rule" && state.selectedRule===learningId && state.inspectorTab==="provenance"){
      const candidate=doc().getElementById(focusId) || doc().getElementById("mining-history-load-"+learningId);
      if(candidate && candidate.focus)candidate.focus({preventScroll:true});
    }
  }
}

export function closeInspector() {
  const inspector = byId("inspector");
  inspector.className = "";
  if (inspector.parentElement?.id === "rules-inspector-host") byId("workspace").appendChild(inspector);
  setVisible(inspector, false);
  byId("workspace").classList.remove("workspace--split");
  state.inspectorKind = "";
  state.selectedRule = "";
  state.selectedEvidence = null;
  state.selectedProject = "";
  paintRuleRows();
  paintProjectRows();
}

function openInspector(title, html) {
  const focusedTab=byId("inspector-body").contains?.(doc().activeElement)
    ? doc().activeElement?.getAttribute?.("data-tab") : null;
  const inspector = byId("inspector");
  inspector.className = "inspector";
  setVisible(inspector, true);
  const docked = state.inspectorKind === "evidence" || state.inspectorKind === "rule" && state.rules?.mode === "paged";
  if (docked && doc().getElementById("rules-inspector-host")?.appendChild) {
    if(inspector.parentElement!==byId("rules-inspector-host"))byId("rules-inspector-host").appendChild(inspector);
    inspector.classList.add("inspector--rule");
    byId("workspace").classList.remove("workspace--split");
  } else byId("workspace").classList.add("workspace--split");
  setText("inspector-title", title);
  if(["rule","evidence"].includes(state.inspectorKind)) {
    const selected=state.inspectorKind==="rule" ? state.selectedRule : state.selectedEvidence?.kind+":"+state.selectedEvidence?.id;
    paintRulesReadView("inspector-body",html,state.inspectorKind+":"+selected+":"+state.inspectorTab);
  } else setHTML("inspector-body", html);
  if(focusedTab)Array.from(byId("inspector-body").querySelectorAll?.(".tabs [data-tab]") || [])
    .find(el=>el.getAttribute("data-tab")===focusedTab)?.focus({preventScroll:true});
}

function ruleDetailRefresh(id, loading, failed) {
  return `<p><button class="btn btn--quiet" id="rules-read-rule-${esc(id)}" data-rule-detail-refresh="true" aria-disabled="${Boolean(loading)}">${failed ? "Retry rule details" : "Refresh rule details"}</button></p>`;
}
function renderRuleDetail(row, tab) {
  return (state.rules?.mode==="paged" ? ruleDetailRefresh(row.id,false,false) : "")+renderRuleInspector(row,tab);
}

export function openRule(ruleId, tab) {
  if(state.selectedRule!==ruleId)delete state.ruleDetails[ruleId];
  const rules = (state.rules && state.rules.rows) || [];
  const row = rules.find((r) => r.id === ruleId);
  state.selectedEvidence=null;
  state.inspectorKind = "rule";
  state.inspectorTab = tab || "why";
  state.selectedRule = ruleId;
  if (state.rules?.mode === "paged") {
    if(state.ruleDetails[ruleId]?.revision !== state.rulePage?.data?.revision)delete state.ruleDetails[ruleId];
    const entry = state.ruleDetails[ruleId];
    openInspector(entry?.data ? ruleHeading(entry.data) : "Rule details", entry?.data ? renderRuleDetail(entry.data, state.inspectorTab) :
      ruleDetailRefresh(ruleId,entry?.loading || !entry,Boolean(entry?.error)) +
      renderTabs(RULE_TABS,state.inspectorTab, tab => rulesHref(ruleId,{...Object.fromEntries(new URLSearchParams(state.ruleQuery)),tab,scan:""})) +
      (entry?.error ? rulesReadFailure(entry,{key:"rule:"+ruleId,title:"Could not read this rule.",guidance:"Use Retry rule details."}) : `<p role="status">Reading rule details…</p>`));
    paintRuleRows();
    if (!entry) loadRuleDetail(ruleId);
    return;
  }
  if (!row) {
    openInspector(
      state.rules ? "Rule not found" : "Loading",
      state.rules
        ? emptyState("No such rule", `No learning has id ${ruleId}.`, "The link may be older than the current learnings table.")
        : `<div class="loading"><span class="loading__bar"></span><span class="loading__bar"></span></div>`
    );
    return;
  }
  openInspector(ruleHeading(row), renderRuleInspector(row, state.inspectorTab));
  paintRuleRows();
}

function projectExposureUrl(key, params) {
  return "/api/project-exposure?project_key=" + encodeURIComponent(key) + (params ? "&" + params : "");
}

export async function loadProjectExposure(projectKey, params, {retry = false} = {}) {
  const url = projectExposureUrl(projectKey, params);
  let entry = state.projectExposures[url];
  if (!retry && entry) return;
  entry = {loading: true}; state.projectExposures[url] = entry;
  const paint = () => {
    if (state.inspectorKind === "project" && state.selectedProject === projectKey && state.inspectorTab === "exposure" && state.projectExposureParams === params) {
      paintProjectDetail();
    }
  };
  paint();
  try {
    const response = await getJSON(url);
    if (response.requested?.project_key !== projectKey) throw new Error("Exposure response belongs to a different project");
    entry.data = response;
  } catch (error) {entry.error = String(error.message || error);}
  finally {
    entry.loading = false; paint();
    if (state.projectExposureFocus === url && state.selectedProject === projectKey && state.inspectorTab === "exposure" && state.projectExposureParams === params) {
      const button = doc()?.getElementById("exposure-submit");
      if (button) button.focus({preventScroll: true});
      state.projectExposureFocus = "";
    }
  }
}

export function submitProjectExposure(event) {
  if (event.target.id !== "project-exposure-form") return;
  event.preventDefault();
  const params = new URLSearchParams();
  for (const [key, value] of new FormData(event.target)) if (value) params.set(key, value);
  const key = state.selectedProject;
  const query = params.toString();
  state.projectExposureFocus = projectExposureUrl(key, query);
  delete state.projectExposures[state.projectExposureFocus];
  setHash(`#/projects/${encodeURIComponent(key)}?tab=exposure&${query}`);
  openProject(key, "exposure", query);
}

export async function loadProjectAvailability(projectKey, {refresh = false, older = false} = {}) {
  let entry = state.projectAvailability[projectKey];
  if (entry?.loading || (entry?.loaded && !refresh && !older) || (older && !entry?.next_cursor)) return;
  entry = entry || {records: [], loaded: false};
  entry.loading = true; entry.error = ""; state.projectAvailability[projectKey] = entry;
  const paint = () => {
    if (state.inspectorKind === "project" && state.selectedProject === projectKey && state.inspectorTab === "availability") {
      paintProjectDetail();
    }
  };
  paint();
  try {
    const url = "/api/project-availability?project_key=" + encodeURIComponent(projectKey) + "&limit=20" + (older ? "&cursor=" + encodeURIComponent(entry.next_cursor) : "");
    const result = await getJSON(url);
    if (result.project_key !== projectKey) throw new Error("Availability response belongs to a different project");
    const records = older ? [...entry.records, ...result.records] : result.records;
    Object.assign(entry, result, {loaded: true, records});
  } catch (error) {entry.error = String(error.message || error);}
  finally {entry.loading = false; paint();}
}

export async function loadProjectInventory(projectKey, {refresh = false, older = false} = {}) {
  let entry = state.projectInventory[projectKey];
  if (entry?.loaded && !refresh && !older) {
    await loadProjectInventoryCopy(projectKey);
    if (state.inspectorTab === "context") loadProjectContextHistory(projectKey);
    if (state.inspectorTab === "summary") loadProjectRuleSummary(projectKey);
    return;
  }
  if (entry?.loading || (older && !entry?.next_cursor)) return;
  entry = entry || {records: [], loaded: false};
  entry.loading = true; entry.error = ""; state.projectInventory[projectKey] = entry;
  const paint = () => {
    if (state.inspectorKind === "project" && state.selectedProject === projectKey && ["inventory", "summary", "context", "topology"].includes(state.inspectorTab)) {
      paintProjectDetail();
    }
  };
  paint();
  try {
    const url = "/api/project-inventory?project_key=" + encodeURIComponent(projectKey) + "&limit=20" + (older ? "&cursor=" + encodeURIComponent(entry.next_cursor) : "");
    const result = await getJSON(url);
    if (result.project_key !== projectKey) throw new Error("Inventory response belongs to a different project");
    const records = older ? [...entry.records, ...result.records] : result.records;
    Object.assign(entry, result, {loaded: true, records});
  } catch (error) {entry.error = String(error.message || error);}
  finally {entry.loading = false; paint();}
  await loadProjectInventoryCopy(projectKey, {refresh});
  if (state.selectedProject === projectKey && state.inspectorTab === "context") loadProjectContextHistory(projectKey, {refresh});
  if (state.selectedProject === projectKey && state.inspectorTab === "summary") loadProjectRuleSummary(projectKey, {refresh});
}

export async function loadProjectInventoryCopy(projectKey, {refresh=false} = {}) {
  const copyId=state.projectInventorySelection[projectKey];
  if(!copyId || state.projectInventory[projectKey]?.records?.some(r=>r.working_copy_id===copyId))return;
  const key=contextHistoryKey(projectKey,copyId);
  let entry=state.projectInventoryCopies[key];
  if(entry?.loading || (entry?.loaded && !refresh))return;
  entry={loading:true};state.projectInventoryCopies[key]=entry;
  const selected=()=>state.inspectorKind==="project" && state.selectedProject===projectKey && state.projectInventorySelection[projectKey]===copyId;
  if(selected())paintProjectDetail();
  try {
    const data=await getJSON("/api/project-inventory?"+new URLSearchParams({project_key:projectKey,working_copy_id:copyId,limit:"1"}));
    if(data.project_key!==projectKey || data.working_copy_id!==copyId || !Array.isArray(data.records) || data.records.length>1 || data.records.some(r=>r.working_copy_id!==copyId))throw new Error("Inventory response belongs to a different working copy");
    Object.assign(entry,data,{loaded:true});
  } catch(error){entry.error=String(error.message || error);}
  finally{entry.loading=false;if(selected())paintProjectDetail();}
}

function recurrenceProject() {
  return state.route === "projects" ? state.selectedProject : new URLSearchParams(evalQueryParts(state.evaluationQuery || "").trends).get("project_key") || "";
}
function recurrenceScope() {return state.route === "projects" ? "project" : "evals";}
function recurrenceUrl(key = "") {return "/api/project-measurements?limit=20" + (key ? "&project_key=" + encodeURIComponent(key) : "");}
function paintRecurrence() {
  if (state.route === "projects" && state.inspectorTab === "recurrence") paintProjectDetail();
  else if (state.route === "evals" && !state.evaluationDetail && !state.qualityDetail) paintTrends();
}
export async function loadRecurrence(key = "", {refresh=false, older=false} = {}) {
  const url=recurrenceUrl(key), scope=recurrenceScope();
  const restore=doc()?.activeElement?.id === "recurrence-older-"+scope;
  let entry=state.measurementHistory[url];
  if (entry?.loading || (entry?.loaded && !refresh && !older) || (older && !entry?.next_cursor)) return;
  entry ||= {records:[]}; state.measurementHistory[url]=entry;entry.loading=true;entry.error="";paintRecurrence();
  try {
    const data=await getJSON(url+(older?"&cursor="+encodeURIComponent(entry.next_cursor):""));
    if ((data.project_key || "")!==key) throw new Error("Measurements belong to a different project");
    Object.assign(entry,data,{records:older?[...entry.records,...data.records]:data.records,loaded:true});
  } catch(error) {entry.error=String(error.message||error);}
  finally {entry.loading=false;paintRecurrence();
    if(restore && recurrenceProject()===key) doc()?.getElementById("recurrence-"+(entry.next_cursor?"older-":"status-")+scope)?.focus({preventScroll:true});
  }
}
export async function loadMeasurement(id) {
  let entry=state.measurementDetails[id];if(entry?.loading || entry?.data) return;
  entry={loading:true,error:"",data:null};state.measurementDetails[id]=entry;paintRecurrence();
  try {const data=await getJSON("/api/project-measurements/"+encodeURIComponent(id));if(data.id!==id) throw new Error("Measurement identity changed");entry.data=data;}
  catch(error) {entry.error=String(error.message||error);}
  finally {entry.loading=false;paintRecurrence();}
}
export function renderRecurrence(key = "", scope = recurrenceScope()) {
  const entry=state.measurementHistory[recurrenceUrl(key)]||{};
  let html=`<p>Before/after comparisons use matched signals per 100,000 physical lines around observed rule availability. Each side needs at least 20 sessions with known starts and applicable scope.</p><p class="footnote">These are observed associations with incomplete coverage. The baseline does not prove the rule was absent. Separate detector versions and overlapping rules are never pooled into an overall percentage.</p>`;
  if(entry.error) html+=`<p class="error-state" role="alert">${esc(entry.error)}</p>`;
  if(entry.loading) html+=`<p role="status">Reading retained measurements…</p>`;
  if(entry.reason) html+=`<p>${entry.reason==="schema_unavailable"?"An explicit database upgrade is required before recurrence measurements can be recorded.":"No recurrence measurements are retained for this selection. The next pipeline run records comparisons from available evidence."}</p>`;
  html+=`<p id="recurrence-status-${scope}" tabindex="-1" aria-live="polite">${entry.loaded?num(entry.records.length)+" of "+(entry.count==null?"unknown":num(entry.count))+" measurements shown":"No measurements loaded yet"}</p>`;
  for(const row of entry.records||[]) {
    const detail=state.measurementDetails[row.id]||{};
    const rate=value=>value==null?"Unknown":num(value,2);
    const side=value=>{
      if(!value) return "Unavailable";
      const headline=value.rate_per_100k==null?"Rate unavailable · "+(value.reason||row.reason||"unknown coverage").replaceAll("_"," "):rate(value.rate_per_100k)+" per 100k";
      const size=value.session_size||{}, workload=value.workload||{};
      const sources=Object.entries(workload.by_source||{}).map(([source,lines])=>`${source}: ${num(lines)} lines`).join(" · ")||"Source workload unknown";
      return `${headline} · ${value.occurrences==null?"unknown":num(value.occurrences)} matched signals / ${num(value.eligible_lines)} lines · ${num(value.sessions)} sessions (minimum ${num(row.min_sessions||20)}). Session size: median ${rate(size.median)} · largest ${rate(size.max)} lines. ${sources} · ${rate(workload.headless)} headless · ${rate(workload.subagent)} subagent lines.`;
    };
    const outcome=row.computable?`${row.comparison === "lower"?"Lower":row.comparison === "higher"?"Higher":"Unchanged"} observed recurrence${row.relative_change_pct==null?" · relative change unavailable from a zero baseline":" · "+rate(row.relative_change_pct)+"%"}`:"Comparison unavailable · "+row.reason.replaceAll("_"," ");
    html+=`<article class="run-record"><p><strong>${esc(outcome)}</strong></p><p><a href="#/rules/${encodeURIComponent(row.learning_id)}?tab=why">Rule ${esc(row.learning_id)}</a> · <a href="#/projects/${encodeURIComponent(row.project_key)}?tab=recurrence">${esc(row.project_key)}</a></p>`+
      field("Before",esc(side(row.before)))+field("After",esc(side(row.after)))+
      `<p class="caption">Detector ${esc(row.compatibility_key.slice(0,16)||"unknown")} · revision ${esc(row.rule_revision_id.slice(0,12))} · recorded ${esc(row.observed_at.slice(0,19).replace("T"," "))} UTC</p>`;
    if(detail.error) html+=`<p role="alert">${esc(detail.error)}</p>`;
    html+=`<button id="recurrence-detail-${scope}-${esc(row.id)}" class="btn" data-measurement-id="${esc(row.id)}" aria-disabled="${Boolean(detail.loading||detail.data)}">${detail.loading?"Reading measurement…":detail.data?"Measurement loaded":"Inspect measurement and matching evidence"}</button>`;
    if(detail.data) html+=runDisclosure("recurrence-detail:"+scope+":"+row.id,"Complete windows, availability, workload, exclusions and matching sources",detail.data);
    html+='</article>';
  }
  html+=`<button id="recurrence-refresh-${scope}" class="btn" data-recurrence-refresh="true" aria-disabled="${Boolean(entry.loading)}">Refresh measurements</button>`;
  if(entry.next_cursor) html+=` <button id="recurrence-older-${scope}" class="btn" data-recurrence-older="true" aria-disabled="${Boolean(entry.loading)}">Load more measurements</button>`;
  return html;
}

function projectNativeEventsUrl(projectKey, query = "") {
  const input = new URLSearchParams(query), params = new URLSearchParams({project_key:projectKey, limit:"20"});
  for (const name of ["logical_session_key", "working_copy_id"]) if (input.get(name)) params.set(name,input.get(name));
  return "/api/project-native-events?" + params;
}

export async function loadProjectNativeEvents(projectKey, query = "", {refresh=false, older=false} = {}) {
  const url = projectNativeEventsUrl(projectKey,query);
  const restore = older && doc()?.activeElement?.id === "native-loads-older";
  let entry = state.projectNativeEvents[url];
  if (entry?.loading || (entry?.loaded && !refresh && !older) || (older && !entry?.next_cursor)) return;
  entry ||= {records:[]}; state.projectNativeEvents[url]=entry; entry.loading=true; entry.error="";
  const paint=()=>{if(state.selectedProject===projectKey && state.inspectorTab==="loads" && state.projectExposureParams===query) paintProjectDetail();};
  paint();
  try {
    const data=await getJSON(url+(older?"&cursor="+encodeURIComponent(entry.next_cursor):""));
    if (data.project_key!==projectKey) throw new Error("Loading reports belong to a different project");
    Object.assign(entry,data,{records:older?[...entry.records,...data.records]:data.records,loaded:true});
  } catch(error) {entry.error=String(error.message||error);}
  finally {entry.loading=false;paint();
    if(restore && state.selectedProject===projectKey && state.inspectorTab==="loads") doc()?.getElementById(entry.next_cursor?"native-loads-older":"native-loads-status")?.focus({preventScroll:true});
  }
}

export function renderProjectNativeEvents(key) {
  const entry=state.projectNativeEvents[projectNativeEventsUrl(key,state.projectExposureParams)]||{};
  const selected=new URLSearchParams(state.projectExposureParams);
  let body=`<p>Native reports retain Claude file-load and session events, and Codex session lifecycle events. Codex reports do not identify loaded instruction files. Times below are when this receiver heard the report, in UTC. Neither adapter supplies loaded bytes or original event time.</p>`+
    `<p class="footnote">Exact rule revision, continuous context and collection completeness remain unknown. Repeated loads are separate reports; these counts do not measure instructions in force or rule benefit.</p>`;
  if(selected.get("logical_session_key") || selected.get("working_copy_id")) body+=`<p>Filtered loading reports · <a href="#/projects/${encodeURIComponent(key)}?tab=loads">Show all project reports</a></p>`;
  if(entry.error) body+=`<p role="alert" class="error-state">${esc(entry.error)}</p>`;
  if(entry.loading) body+=`<p role="status">Reading retained loading reports…</p>`;
  if(entry.reason==="schema_unavailable") body+=`<p>An explicit database upgrade is required before native reports can be recorded.</p>`;
  if(entry.reason==="no_retained_reports") body+=`<p>No native reports are retained for this selection. Hook installation or collection coverage has not been established. This does not mean no instructions loaded.</p>`;
  body+=`<p id="native-loads-status" class="caption" tabindex="-1" aria-live="polite">${entry.loaded?num(entry.records.length)+(entry.count==null?"":" of "+num(entry.count))+" native reports shown":"No native reports loaded yet"}</p>`;
  for(const row of entry.records||[]) {
    const p=row.payload;
    const label={InstructionsLoaded:"Instruction loaded",SessionStart:"Session opened",PreCompact:"Compaction started",PostCompact:"Context compacted",SubagentStart:"Subagent started",SubagentStop:"Subagent stopped",Interrupt:"Turn interrupted",CwdChanged:"Working directory changed",SessionEnd:"Session ended"}[row.event_name] || row.event_name;
    body+=`<article class="run-record session-record"><details ${reviewDisclosure("native-load:"+row.id)}><summary>${esc(row.source === "codex" ? "Codex" : "Claude")} · ${esc(label)}${p.file_path ? " · " + esc(p.file_path.split("/").pop()) : ""} · session ${esc(p.session_id)} · received ${esc(row.received_at.slice(0,19).replace("T"," "))}</summary>`+
      field("Provider",esc(row.source === "codex" ? "Codex" : "Claude"))+
      field("Reported file",esc(p.file_path||"Not a file-load event"))+
      field("Load reason / scope",esc([p.load_reason,p.memory_type].filter(Boolean).join(" · ")||p.source||p.trigger||p.reason||p.agent_type||(row.event_name === "Interrupt" ? "Interrupted turn" : "Directory change")))+
      field("Working copy",esc(row.working_copy_path))+
      field("Agent",esc(row.agent_id||"Main session"))+
      `<p><a href="#/projects/${encodeURIComponent(key)}?tab=loads&logical_session_key=${encodeURIComponent(row.logical_session_key)}">All reports for this session</a> · <a href="#/projects/${encodeURIComponent(key)}?tab=loads&working_copy_id=${encodeURIComponent(row.working_copy_id)}">All reports for this working copy</a></p>`+
      runDisclosure("native-load-evidence:"+row.id,"Complete native report and receiver provenance",row)+`</details></article>`;
  }
  body+=`<button id="native-loads-refresh" class="btn" type="button" data-native-loads-refresh="true" aria-disabled="${Boolean(entry.loading)}">Refresh loading reports</button>`;
  if(entry.next_cursor) body+=` <button id="native-loads-older" class="btn" type="button" data-native-loads-older="true" aria-disabled="${Boolean(entry.loading)}">Load more reports</button>`;
  return body;
}

function projectSessionsUrl(projectKey, query = "") {
  const input = new URLSearchParams(query), params = new URLSearchParams({project_key: projectKey, limit: "20"});
  for (const name of ["compatibility_key", "rule_revision_id", "working_copy_id"]) if (input.get(name)) params.set(name, input.get(name));
  return "/api/project-sessions?" + params;
}

export async function loadProjectSessions(projectKey, query = "", {refresh = false, older = false} = {}) {
  const url = projectSessionsUrl(projectKey, query);
  const restoreOlderFocus = older && doc()?.activeElement?.id === "sessions-older";
  let entry = state.projectSessions[url];
  if (entry?.loading || (entry?.loaded && !refresh && !older) || (older && !entry?.next_cursor)) return;
  entry ||= {records: []}; state.projectSessions[url] = entry;
  entry.loading = true; entry.error = "";
  const paint = () => { if (state.selectedProject === projectKey && state.inspectorTab === "sessions" && state.projectExposureParams === query) paintProjectDetail(); };
  paint();
  try {
    const data = await getJSON(url + (older ? "&cursor=" + encodeURIComponent(entry.next_cursor) : ""));
    if (data.project_key !== projectKey) throw new Error("Session evidence belongs to a different project");
    const records = older ? [...entry.records, ...data.records] : data.records;
    Object.assign(entry, data, {records, loaded: true});
  } catch (error) {entry.error = String(error.message || error);}
  finally {entry.loading = false; paint();
    if (restoreOlderFocus && state.selectedProject === projectKey && state.inspectorTab === "sessions") doc()?.getElementById(entry.next_cursor ? "sessions-older" : "sessions-status")?.focus({preventScroll:true});
  }
}

export function renderProjectSessions(key) {
  const entry = state.projectSessions[projectSessionsUrl(key, state.projectExposureParams)] || {};
  const params = new URLSearchParams(state.projectExposureParams);
  const choices = (entry.version_groups || []).map(v => `<option value="${esc(v.compatibility_key)}" ${v.compatibility_key === (params.get("compatibility_key") || entry.compatibility_key) ? "selected" : ""}>${esc(v.compatibility_key.slice(0, 16))} · ${num(v.observations)} scans${v.identifiable ? "" : " · unidentifiable"}</option>`).join("");
  const revisions = (entry.rule_revisions || []).map(r => `<option value="${esc(r.id)}" ${r.id === params.get("rule_revision_id") ? "selected" : ""}>Rule ${esc(r.learning_id.slice(0, 8))} · ${esc(r.applied_at.slice(0, 10))} · ${esc(r.id.slice(0, 8))}</option>`).join("");
  const copies = (state.projectDetails[key]?.data?.observed_working_copies || []).map(c => `<option value="${esc(c.id)}" ${c.id === params.get("working_copy_id") ? "selected" : ""}>${esc(c.normalized_path)}</option>`).join("");
  let body = `<p>Inspect native session metadata beside actual working-copy checks. Rollout creation is not proof that a session received a rule. All times are UTC.</p>` +
    `<form id="project-sessions-form" class="session-filters"><div class="session-filter"><label for="sessions-version">Detector / parser version</label><select id="sessions-version" name="compatibility_key"><option value="">Choose a version</option>${choices}</select></div><div class="session-filter"><label for="sessions-revision">Delivered revision</label><select id="sessions-revision" name="rule_revision_id"><option value="">All session evidence</option>${revisions}</select></div><div class="session-filter"><label for="sessions-copy">Working copy</label><select id="sessions-copy" name="working_copy_id"><option value="">All observed copies</option>${copies}</select></div><button id="sessions-submit" type="submit" class="btn">Inspect sessions</button></form>`;
  if (entry.error) body += `<p role="alert" class="error-state">${esc(entry.error)}</p>`;
  if (entry.loading) body += `<p role="status">Reading retained session evidence…</p>`;
  const reasons = {schema_unavailable:"This database requires an explicit upgrade before session context can be recorded.",missing_observations:"No scan observations are retained for this project.",incompatible_versions:"Choose one detector / parser version. Incompatible versions cannot be combined.",uncovered_version:"This version has no retained project observations.",unidentifiable_version:"This version cannot establish reproducible context provenance.",no_complete_projection:"No complete scan projection is retained.",incomplete_physical_projection:"The physical-line projection is incomplete. Rebuild or missing data is not an observed zero."};
  if (entry.reason) body += `<p>${esc(reasons[entry.reason] || entry.reason)}</p>`;
  if (entry.coverage) {
    const c = entry.coverage;
    body += `<div class="session-coverage"><p>Project coverage across observed copies: <strong>${num(c.logical_sessions)}</strong> logical sessions · ${num(c.session_copy_pairs)} session/copy pairs · ${num(c.physical_lines)} retained physical lines.</p><p>${num(c.reported_creation_pairs)} pairs with consistent creation metadata · ${num(c.unknown_creation_pairs)} with unknown creation · ${num(c.unknown_time_lines)} lines without time · ${num(c.unknown_session_lines)} without session identity.</p></div>` +
      `<p class="footnote">Instructions received: unknown. File checks support candidates only; runtime loading and continuity between checks remain unverified. These counts do not measure rule benefit.</p>` +
      runDisclosure("session-coverage:" + key, "Complete scan coverage and source limits", c);
  }
  body += `<p id="sessions-status" class="caption" tabindex="-1" aria-live="polite">${entry.loaded ? `${num(entry.records.length)}${entry.count == null ? "" : " of " + num(entry.count)} session/copy records shown` : "No session records loaded yet"}</p>`;
  for (const row of entry.records || []) {
    const q = row.qualification;
    body += `<article class="run-record session-record"><details ${reviewDisclosure("session-record:" + row.id)}><summary>${esc(row.source)} · ${esc(row.working_copy_path.split("/").pop() || "Copy unknown")} · session ${esc(row.logical_session_key.slice(0, 8))} · ${row.reported_started_at ? "creation " + esc(row.reported_started_at.slice(0, 19).replace("T", " ")) : "creation unknown"}</summary>` +
      field("Logical session", `<span class="mono">${esc(row.logical_session_key)}</span>`) +
      (["claude","codex"].includes(row.source) ? `<p><a href="#/projects/${encodeURIComponent(key)}?tab=loads&logical_session_key=${encodeURIComponent(row.logical_session_key)}">Inspect native loading reports</a></p>` : "") +
      field("Working copy", `<span class="mono">${esc(row.working_copy_path || "Unknown")}</span>`) +
      field("Reported rollout creation", esc(row.reported_started_at || "Unknown")) +
      (row.start_reason ? `<p>${esc(row.start_reason.replaceAll("_", " "))}</p>` : "") +
      field("First / last retained line", `${esc(row.first_recorded_at || "Unknown")} / ${esc(row.last_recorded_at || "Unknown")}`) +
      field("Physical lines", `${num(row.physical_lines)} · ${num(row.unknown_time_lines)} without time`);
    if (q) {
      body += `<h4>${q.status === "candidate" ? "Startup candidate from file checks" : q.status === "excluded" ? "No startup credit" : "Startup eligibility unknown"}</h4>` +
        (q.reason ? `<p>${esc(q.reason.replaceAll("_", " "))}</p>` : "") +
        q.intervals.map(p => `<p class="caption">Candidate interval: ${esc(p.start)} ≤ time &lt; ${esc(p.end)}. Continuous runtime availability is unverified.</p>`).join("") +
        runDisclosure("session-qualification:" + row.id, "Supporting availability checks and interval limits", q);
    }
    body += runDisclosure("session-context:" + row.id, "Complete native metadata and scan identities", row) + `</details></article>`;
  }
  if (entry.loaded && entry.count === 0) body += `<p>This complete scan projection contains no session/copy records for the selection.</p>`;
  body += `<button id="sessions-refresh" class="btn" type="button" data-sessions-refresh="true" aria-disabled="${Boolean(entry.loading)}">Refresh session evidence</button>`;
  if (entry.next_cursor) body += ` <button id="sessions-older" class="btn" type="button" data-sessions-older="true" aria-disabled="${Boolean(entry.loading)}">Load more sessions</button>`;
  return body;
}

export function submitProjectSessions(event) {
  if (event.target.id !== "project-sessions-form") return;
  event.preventDefault();
  const query = new URLSearchParams();
  for (const [key, value] of new FormData(event.target)) if (value) query.set(key, value);
  const key = state.selectedProject;
  delete state.projectSessions[projectSessionsUrl(key, query.toString())];
  setHash(`#/projects/${encodeURIComponent(key)}?tab=sessions&${query}`);
  openProject(key, "sessions", query.toString());
}

export function openProject(projectKey, tab, exposureParams = "") {
  const changed = state.selectedProject !== projectKey;
  closeInspector();
  state.inspectorKind = "project";
  state.inspectorTab = tab || "summary";
  state.selectedProject = projectKey;
  state.projectInventorySelection[projectKey] = new URLSearchParams(exposureParams).get("working_copy_id") || "";
  state.projectExposureParams = exposureParams;
  setVisible(byId("view-projects"), false);
  setVisible(byId("project-detail"), true);
  paintProjectDetail();
  if (changed) doc()?.getElementById("project-title")?.focus({preventScroll: true});
  loadProjectDetail(projectKey, {refresh: changed});
  if (state.inspectorTab === "exposure") loadProjectExposure(projectKey, exposureParams);
  if (state.inspectorTab === "availability") loadProjectAvailability(projectKey);
  if (state.inspectorTab === "sessions") loadProjectSessions(projectKey, exposureParams);
  if (state.inspectorTab === "loads") loadProjectNativeEvents(projectKey, exposureParams);
  if (state.inspectorTab === "recurrence") loadRecurrence(projectKey);
  if (["summary", "inventory", "context", "topology"].includes(state.inspectorTab)) loadProjectInventory(projectKey);
  if (state.inspectorTab === "summary") ["contributed", "proposals", "deliveries"].forEach(kind => loadProjectRecords(kind, {refresh: changed}));
  paintProjectRows();
}

function projectPageId(kind, learningId = "") { return kind + (learningId ? ":" + learningId : ""); }

export async function loadProjectDetail(projectKey, {refresh = false} = {}) {
  let entry = state.projectDetails[projectKey];
  if (entry?.loading || (entry?.data && !refresh)) return;
  entry = {loading: true}; state.projectDetails[projectKey] = entry;
  paintProjectDetail();
  try {
    const data = await getJSON("/api/project-detail?project_key=" + encodeURIComponent(projectKey));
    if (data.project_key !== projectKey) throw new Error("Project summary belongs to a different project");
    entry.data = data;
  } catch (error) { entry.error = String(error.message || error); }
  finally { entry.loading = false; if (state.selectedProject === projectKey) paintProjectDetail(); }
}

export async function loadProjectRecords(kind, {older = false, refresh = false, learningId = ""} = {}) {
  const key = state.selectedProject, pageId = projectPageId(kind, learningId);
  const pages = state.projectRecords[key] ||= {};
  let entry = pages[pageId];
  if (entry?.loading || (entry?.loaded && !older && !refresh) || (older && !entry?.next_cursor)) return;
  entry = entry || {records: []}; pages[pageId] = entry;
  entry.loading = true; entry.error = ""; paintProjectDetail();
  try {
    const params = new URLSearchParams({project_key: key, kind, limit: "20"});
    if (learningId) params.set("learning_id", learningId);
    if (older) params.set("cursor", entry.next_cursor);
    const data = await getJSON("/api/project-records?" + params);
    if (data.project_key !== key || data.kind !== kind || (data.learning_id || "") !== learningId) throw new Error("Project records belong to a different selection");
    const records = older ? [...new Map([...entry.records, ...data.records].map(r => [r.id, r])).values()] : data.records;
    Object.assign(entry, data, {records, loaded: true});
  } catch (error) {entry.error = String(error.message || error);}
  finally {entry.loading = false; if (state.selectedProject === key) paintProjectDetail();}
}

function projectRuleSummaryKey(key) {
  return contextHistoryKey(key, state.projectInventorySelection[key] || selectedProjectInventory(key)?.working_copy_id || "");
}

export async function loadProjectRuleSummary(projectKey, {refresh=false} = {}) {
  const cacheKey=projectRuleSummaryKey(projectKey), copy=JSON.parse(cacheKey)[1];
  let entry=state.projectRuleSummaries[cacheKey];
  if(entry?.loading || (entry?.data && !refresh))return;
  entry ||= {};state.projectRuleSummaries[cacheKey]=entry;entry.loading=true;entry.error="";entry.errorDetail="";
  const selected=()=>state.inspectorKind==="project" && state.selectedProject===projectKey && state.inspectorTab==="summary" && projectRuleSummaryKey(projectKey)===cacheKey;
  const paint=()=>{
    if(!selected())return;
    const container=doc()?.getElementById("project-rule-summary");
    if(!container){paintProjectDetail();return;}
    const active=doc()?.activeElement, inside=container.contains?.(active);
    // A retained summary refresh must leave the native navigation controls alive.
    preserveOperationView("project-rule-summary",()=>renderProjectRuleSummary(projectKey));
    if(inside && !doc()?.getElementById(active.id))doc()?.getElementById("project-rules-refresh")?.focus({preventScroll:true});
    setText("project-rule-matched",entry.data?.counts.project.matched==null?"Unknown":num(entry.data.counts.project.matched));
  };
  paint();
  try {
    const params=new URLSearchParams({project_key:projectKey});if(copy)params.set("working_copy_id",copy);
    const data=await getJSON("/api/project-summary?"+params);
    if(data.project_key!==projectKey || (data.working_copy_id || "")!==copy)throw new Error("Rule summary belongs to a different selection");
    entry.data=data;
  } catch(error){entry.error=String(error.message||error);entry.errorDetail=error?.readDetail || "";}
  finally{entry.loading=false;paint();}
}

export function renderProjectRuleSummary(key) {
  const entry=state.projectRuleSummaries[projectRuleSummaryKey(key)] || {}, data=entry.data;
  let body=entry.error?readFailure(entry,{key:"project-summary:" + projectRuleSummaryKey(key),title:"Could not read the rule summary.",guidance:data?"Last successful summary remains below. Retry with Refresh rule summary.":"Retry with Read rule summary below."}):"";
  if(entry.loading)body+=`<p class="caption" role="status">Reading retained rule summary…</p>`;
  if(!data)return body+`<p class="muted">Rule summary has not been read for this copy.</p><button class="btn" id="project-rules-refresh" data-project-rules-refresh="true">Read rule summary</button>`;
  const project=data.counts.project, global=data.counts.global;
  body+=`<p class="caption">At recorded checks only; provider/path conditions apply. Current presence and receipt are unverified.</p>`;
  body+=`<p class="caption">${project.retained==null?"Project delivery history unknown":`${num(project.retained)} retained project revisions`} · ${global.retained==null?"Global delivery history unknown":`${num(global.retained)} global revisions with checks here`} · ${num(data.proposals.count)} proposed edits.</p>`;
  const labels={available:"Exact content observed",absent:"Content not found",changed:"Marked content changed",unknown:"Observation unknown"};
  const lifetimes={no_recorded_end:"No recorded end",rolled_back:"Rolled back",superseded:"Later application recorded",unknown:"Application lifetime unknown"};
  for(const row of data.deliveries.records) {
    const revision=row.revision, check=row.observation, text=revision.content.replace(/<!--\s*si:[^>]*-->/g, "").trim();
    const scopes=check?[...new Set(check.observations.flatMap(o=>o.matches.flatMap(m=>m.loading_paths.map(p=>p.provider+" · "+p.scope.kind.replaceAll("_"," ")+(p.scope.paths.length?" · "+p.scope.paths.join(", "):"")))))]:[];
    const preview=text.length>140?text.slice(0,140)+"…":text;
    body+=`<article class="project-summary-rule"><div class="project-summary-rule__headline"><span class="project-summary-rule__state">${esc(lifetimes[row.lifetime.state])} · ${esc(row.origin)}</span>`+
      `<a id="project-summary-title-${esc(revision.id)}" class="project-summary-rule__title" href="#/rules/${encodeURIComponent(revision.learning_id)}">${esc(preview)}</a></div>`+
      `<details ${reviewDisclosure("project-summary-revision:"+key+":"+data.working_copy_id+":"+revision.id)}><summary id="project-summary-evidence-${esc(revision.id)}">${check?`${esc(labels[check.status] || "Observation unknown")} · ${esc(check.observed_at.slice(0,19).replace("T"," "))} UTC`:"No check for this exact revision and copy"} · evidence</summary>`+
      `<p class="caption">${scopes.map(esc).join("; ") || "Provider scope unknown"}.</p><p class="mono">${esc(revision.destination.target_path)}</p><pre class="review-card__diff">${esc(revision.content)}</pre>`+
      runDisclosure("project-summary-record:"+revision.id+":"+data.working_copy_id,"Application, observations and lifecycle",row)+`</details></article>`;
  }
  if(!data.deliveries.records.length)body+=`<p class="muted">${data.deliveries.reason==="schema_unavailable"?"Retained delivery schema is unavailable.":"No delivery revisions are retained for this selection."}</p>`;
  for(const row of data.proposals.records) {
    const title=row.learning?.title || row.learning?.rule_text || row.learning_id;
    body+=`<article class="project-summary-rule"><div class="project-summary-rule__headline"><span class="project-summary-rule__state">Proposed · ${esc(row.status)} · ${esc(row.next_step.replaceAll("_"," "))}</span>`+
      `<a id="project-summary-proposal-${esc(row.id)}" class="project-summary-rule__title" href="#/review/proposal/${encodeURIComponent(row.id)}">${esc(title.slice(0,140))}${title.length>140?"…":""}</a></div>`+
      `<details ${reviewDisclosure("project-summary-proposal:"+row.id)}><summary id="project-summary-destination-${esc(row.id)}">${esc(row.target_path.split("/").pop())} · destination and disposition</summary><p class="mono">${esc(row.target_path)}</p><p>${esc(row.execution?.detail || row.execution?.reason || "")}</p></details></article>`;
  }
  body+=`<p class="caption">Previews: ${num(data.deliveries.records.length)} of ${data.deliveries.count==null?"unknown":num(data.deliveries.count)} retained revisions; ${num(data.proposals.records.length)} of ${num(data.proposals.count)} proposed edits. Text is shortened. Complete project lists remain below. <a id="project-summary-observations" href="${esc(projectReadHref(key,"availability"))}">All project and global copy observations</a>.</p>`;
  body+=`<details ${reviewDisclosure("project-summary-coverage:"+key+":"+data.working_copy_id)}><summary id="project-summary-coverage">Copy, times and coverage</summary><p>${esc(data.meaning)}</p><p class="mono">Copy: ${esc(data.working_copy_id || "not selected")}</p><p>Check times: ${data.observation_times.map(esc).join(" · ") || "none retained"}.</p>`+
    `<p>${num(project.known_matches)} known project matches; ${num(project.uncertain)} revisions with uncertain lifetime or coverage. ${num(global.known_matches)} known global matches. ${num(data.proposals.unresolved_count)} proposals could not be attributed to a project.</p>`+
    runDisclosure("project-summary-counts:"+key+":"+data.working_copy_id,"Complete population counts",data.counts)+`</details>`;
  return body+`<button class="btn" id="project-rules-refresh" data-project-rules-refresh="true" aria-disabled="${Boolean(entry.loading)}">Refresh rule summary</button>`;
}

export function renderProjectLocations(measurement, workingCopyPath) {
  if (!measurement) return `<p>Other Markdown and character measurements are unavailable for this observation.</p>`;
  const groups = new Map(), base = measurement.other_markdown?.root || workingCopyPath;
  for (const [kind, section] of [["Instructions", measurement.instructions], ["Other Markdown", measurement.other_markdown]]) {
    if (!section?.files || section.files.some(f => typeof f.real_path !== "string" || !Number.isSafeInteger(f.bytes) || f.bytes < 0)) {
      return `<p>Recorded location byte amounts are unavailable for this observation.</p>`;
    }
    for (const file of section.files) {
      const relative = base && file.real_path.startsWith(base+"/") ? file.real_path.slice(base.length+1) : null;
      const folder = relative === null ? "External / global" : relative.includes("/") ? relative.split("/")[0]+"/" : "(repository root)";
      const id = kind+"|"+folder, group = groups.get(id) || {folder, kind, bytes:0, files:0};
      group.bytes += file.bytes; group.files++; groups.set(id, group);
    }
  }
  const rows = [...groups.values()].sort((a,b) => a.folder.localeCompare(b.folder) || a.kind.localeCompare(b.kind));
  const largest = Math.max(0, ...rows.map(r => r.bytes));
  const table = rows.length ? `<div class="scroll-x"><table class="data project-context-summary"><thead><tr><th scope="col">Recorded location</th><th scope="col">Source</th><th class="num" scope="col">Bytes</th><th class="num" scope="col">Files</th></tr></thead><tbody>` +
    rows.slice(0,6).map(r => `<tr data-location="${esc(JSON.stringify([r.folder,r.kind]))}"><th scope="row" class="mono">${esc(r.folder)}</th><td>${esc(r.kind)}</td><td class="num">${num(r.bytes)} B<span class="project-location-bar" aria-hidden="true"><span style="width:${largest ? r.bytes/largest*100 : 0}%"></span></span></td><td class="num">${num(r.files)}</td></tr>`).join("") + `</tbody></table></div>` : `<p>No location byte measurements were retained.</p>`;
  return table + (rows.length ? `<p class="caption">Largest recorded group: ${num(largest)} bytes. Bars use this scale across all recorded groups.</p>` : "") +
    `<p class="caption">${num(Math.min(rows.length,6))} of ${num(rows.length)} location/source groups shown. Physical aliases count once. Instructions: ${esc(measurement.instructions.status)}. Other Markdown: ${esc(measurement.other_markdown.status)}; separate from instruction weight.</p>`;
}

function projectConfiguredContext(key, record) {
  if(!record?.files)return `<p class="muted">No unambiguous inventory is available for this copy.</p>`;
  let body=`<p class="caption">Observed ${esc(record.observed_at)} · ${esc(record.status)}. Configuration does not prove loading.</p>`;
  if(record.context) {
    for(const group of record.context.groups.filter(g=>g.origin==="all"))body+=`<p><strong>${group.provider==="codex"?"Codex":"Claude Code"}</strong> · ${esc(bytes(group.startup_bytes))} startup candidates · ${esc(bytes(group.conditional_bytes))} conditional · ${esc(bytes(group.on_demand_bytes))} on demand · ${esc(bytes(group.unresolved_bytes))} unresolved.</p>`;
    body+=`<p class="caption">Provider totals can overlap; do not add them. Global, managed and project origins remain separate in the complete context view.</p>`;
  } else body+=`<p>Provider scope was not recorded for this older inventory.</p>`;
  body += renderProjectLocations(record.measurements, record.working_copy.normalized_path);
  body+=`<p><a href="${esc(projectReadHref(key,"context"))}">Complete configured context, measurements and history</a></p>`;
  return body+`<details ${reviewDisclosure("project-summary-ownership:"+key)}><summary>Instruction ownership and complete inventory</summary>${projectInventoryOverview(key,{includeControl:false})}</details>`;
}

function projectPanel(title, body, meta = "") {
  return `<section class="panel"><header class="panel__head"><h2 class="panel__title">${esc(title)}</h2>${meta}</header><div class="panel__body">${body}</div></section>`;
}

function projectRecordList(key, kind, learningId = "") {
  const pageId = projectPageId(kind, learningId), entry = state.projectRecords[key]?.[pageId] || {};
  const target = `data-project-records="${kind}" data-project-learning="${esc(learningId)}"`;
  let body = entry.error ? `<p class="error-state" role="alert">${esc(entry.error)}</p>` : "";
  if (entry.loading) body += `<p role="status">Reading ${esc(kind)}…</p>`;
  if (entry.loaded && !entry.records.length) body += `<p class="muted">${kind === "deliveries" ? "No delivery revisions are retained for this project. Earlier delivery history may be unavailable." : kind === "proposals" ? "No undecided proposal has a verified destination in this project." : "No linked records are retained for this selection."}</p>`;
  if (entry.reason === "schema_unavailable") body += `<p>The database predates retained delivery revisions. Upgrade it explicitly; missing history cannot be inferred.</p>`;
  for (const row of entry.records || []) {
    if (kind === "contributed") {
      body += `<article class="project-record"><h3><a href="#/rules/${encodeURIComponent(row.id)}">${esc(row.title || row.id)}</a></h3><p>${esc(row.rule_text)}</p><p class="caption">${num(row.project_incidents)} linked incidents in this project · ${esc(row.scope)} · ${esc(row.status)}</p>` +
        `<details ${reviewDisclosure("project-evidence:" + key + ":" + row.id)}><summary>Evidence from this project</summary>` + projectRecordList(key, "evidence", row.id) + `</details></article>`;
    } else if (kind === "proposals") {
      body += `<article class="project-record"><h3><a href="#/review/proposal/${encodeURIComponent(row.id)}">${esc(row.learning?.title || row.learning_id)}</a></h3><p class="mono">${esc(row.target_path)}</p><p>${esc(row.status)} · ${esc(row.next_step.replaceAll("_", " "))}</p>` +
        `<p class="caption">${esc(row.execution?.detail || row.execution?.reason || "")}</p>` + runDisclosure("project-proposal:" + row.id, "Complete proposed edit and canonical destination", row) + `</article>`;
    } else if (kind === "deliveries") {
      body += `<article class="project-record"><h3><a href="#/rules/${encodeURIComponent(row.learning_id)}">Rule ${esc(row.learning_id.slice(0, 8))}</a></h3><p>${esc(row.applied_at)} · <span class="mono">${esc(row.destination.target_path)}</span></p>` +
        `<details ${reviewDisclosure("project-delivery:" + row.id)}><summary>Delivered content and provenance</summary><pre class="review-card__diff">${esc(row.content)}</pre>` + runDisclosure("project-delivery-record:" + row.id, "Complete application revision", row) + `</details></article>`;
    } else {
      body += `<article class="project-record"><p>${esc(row.signal_type)} · ${esc(row.ts || "Time unknown")}</p>${renderIncidentPrimary(row)}<p class="caption mono">Session ${esc(row.session_id || "ID unknown")}</p>` +
        renderScanHistory(row.id, "project-" + key) +
        runDisclosure("project-incident:" + row.id, "Complete retained incident and archived window", row) + `</article>`;
    }
  }
  if (entry.unresolved_proposals?.length) body += runDisclosure("project-unresolved:" + key, `${num(entry.unresolved_proposals.length)} proposals could not be assigned to any project`, entry.unresolved_proposals);
  body += `<p id="project-status-${esc(pageId)}" tabindex="-1" class="caption" aria-live="polite">${num(entry.records?.length || 0)} shown · ${entry.count == null ? (entry.loaded ? "total unknown" : "total not read") : `${num(entry.count)} retained`}</p>`;
  body += `<button id="project-load-${esc(pageId)}" class="btn" type="button" ${target} data-project-records-refresh="true" aria-disabled="${Boolean(entry.loading)}">${entry.loaded ? "Refresh" : "Read"} ${esc(kind)}</button>`;
  if (entry.next_cursor) body += ` <button id="project-older-${esc(pageId)}" class="btn" type="button" ${target} data-project-older="true" aria-disabled="${Boolean(entry.loading)}">Load more ${esc(kind)}</button>`;
  return body;
}

function projectCopyControl(key) {
  const entry = state.projectInventory[key] || {}, records = [...(entry.records || [])];
  const requested=state.projectInventorySelection[key], exact=state.projectInventoryCopies[contextHistoryKey(key,requested)] || {};
  const selected = selectedProjectInventory(key);
  if(selected && !records.some(r=>r.working_copy_id===selected.working_copy_id))records.push(selected);
  if (!records.length && !requested) return `<p class="muted">${esc(entry.error || (entry.loading ? "Reading recorded instruction files…" : "No instruction inventory is retained. Context and ownership are unknown."))}</p>`;
  let body = `<label class="caption" for="project-copy">Working copy</label> <select id="project-copy" data-copy-project-key="${esc(key)}" data-project-tab="${esc(state.inspectorTab)}">` +
    (requested && !selected ? `<option value="${esc(requested)}" selected>Requested copy · ${esc(requested)}</option>` : "") +
    records.map(record => `<option value="${esc(record.working_copy_id)}" ${selected?.working_copy_id === record.working_copy_id ? "selected" : ""}>${esc(record.working_copy.normalized_path)}</option>`).join("") + `</select>`;
  body += `<p id="inventory-status" tabindex="-1" class="caption" aria-live="polite">${num(records.length)} of ${num(entry.count)} copy inventories loaded</p>`;
  if (entry.next_cursor) body += `<button id="inventory-older" class="btn" type="button" data-inventory-older="true" aria-disabled="${Boolean(entry.loading)}">Load more copies</button>`;
  if (entry.error) body += `<p class="error-state" role="alert">${esc(entry.error)}</p>`;
  if(!selected) {
    const message=exact.error || (exact.loaded ? (exact.reason==="schema_unavailable" ? "The inventory schema is unavailable. Context and ownership are unknown." : "No inventory is retained for the requested working copy. Context and ownership are unknown.") : entry.loading || exact.loading ? "Reading requested working copy…" : "The requested working copy has not been read. Context and ownership are unknown.");
    return body+`<p id="project-copy-status" role="${exact.error ? "alert" : "status"}">${esc(message)}</p><button id="inventory-load" class="btn" data-inventory-refresh="true" aria-disabled="${Boolean(entry.loading || exact.loading)}">Retry requested copy</button>`;
  }
  if (selected.status === "conflicting") return body + `<p class="error-state">The latest inventory has conflicting observations. Inspect Instruction ownership.</p>`;
  body += `<p class="caption">Observed ${esc(selected.observed_at)} · ${esc(selected.status)}. Other copies can differ.</p>`;
  return body;
}

export function renderInstructionOwnership(file) {
  const owners = [["human_managed", "Human-managed"], ["machine", "Exact machine text"], ["edited", "Edited"], ["unknown", "Unknown"]];
  const amount = value => Number.isSafeInteger(value) && value >= 0;
  const valid = ["bytes", "lines"].every(unit => amount(file[unit]) &&
    owners.every(([key]) => amount(file.ownership?.[key]?.[unit])) &&
    owners.reduce((sum, [key]) => sum+file.ownership[key][unit], 0) === file[unit]);
  if (!valid) return `<p class="caption">Ownership shares unavailable: recorded byte or physical-line amounts are missing or inconsistent.</p>`;
  const bar = file.bytes ? `<div class="project-ownership-bar" aria-hidden="true">` +
    owners.map(([key]) => `<span data-owner="${key}" style="width:${file.ownership[key].bytes/file.bytes*100}%"></span>`).join("") + `</div>` : `<p class="caption">Empty source; no proportional byte shares.</p>`;
  return bar + `<p class="caption">${file.bytes ? "Left-to-right byte shares" : "Recorded ownership"} · physical-line counts listed separately.</p>` +
    `<ul class="project-ownership-legend">` + owners.map(([key, label]) => `<li data-owner="${key}"><span class="project-owner-key" aria-hidden="true"></span><span>${label}<span>${num(file.ownership[key].bytes)} bytes · ${num(file.ownership[key].lines)} lines</span></span></li>`).join("") + `</ul>`;
}

function projectInventoryOverview(key, {includeControl=true} = {}) {
  let body = includeControl ? projectCopyControl(key) : "";
  const selected = selectedProjectInventory(key);
  if (!selected?.files) return body;
  if (!selected.files.length) body += `<p>${selected.status === "recorded" ? "No supported instruction files were found." : "No complete file inventory is available. Inspect coverage issues."}</p>`;
  for (const file of selected.files.slice(0, 8)) {
    const total = file.bytes;
    const name = projectFileName(file.path, selected.working_copy.normalized_path);
    body += `<div class="project-file"><div><span class="mono" title="${esc(file.path)}">${esc(name)}</span><span>${esc(bytes(total))}</span></div>` +
      renderInstructionOwnership(file) + `</div>`;
  }
  if (selected.files.length > 8) body += `<p class="caption">8 of ${num(selected.files.length)} files shown. The complete inventory is linked below.</p>`;
  body += `<p class="caption">Human-managed means unmarked text protected from automatic deletion; it does not prove authorship. Exact machine text matches retained delivery evidence.</p><p class="caption">${num(selected.totals.files)} instruction sources · ${esc(bytes(selected.totals.bytes))} observed. Files and embedded fields have distinct identities; aliases count once. Observed bytes do not prove session loading.</p>`;
  body += `<a href="${esc(projectReadHref(key,"inventory"))}">Inspect every file, ownership, scope and working copy</a>`;
  return body;
}

function selectedProjectInventory(key) {
  const records = state.projectInventory[key]?.records || [];
  const requested=state.projectInventorySelection[key];
  return requested ? records.find(r=>r.working_copy_id===requested) || state.projectInventoryCopies[contextHistoryKey(key,requested)]?.records?.[0] : records[0];
}

export function projectReadHref(key,tab,values={}) {
  const params=new URLSearchParams({tab,...values});
  const copy=state.projectInventorySelection[key];
  if(!params.has("working_copy_id") && copy)params.set("working_copy_id",copy);
  return `#/projects/${encodeURIComponent(key)}?${params}`;
}

function projectFileName(path, copy) {
  if (path.startsWith("managed-text:") && path.endsWith("#/claudeMd")) {
    return decodeURIComponent(path.slice("managed-text:".length, -"#/claudeMd".length)) + " → claudeMd";
  }
  return path.startsWith(copy + "/") ? path.slice(copy.length + 1) : path;
}

function policyCoverage(record) {
  const policy = record.policy_discovery;
  if (!policy) return "";
  let body = `<h3>Embedded managed instructions</h3><p>Settings file ${esc(policy.main_status.replaceAll("_", " "))} · drop-ins ${esc(policy.dropins_status.replaceAll("_", " "))}.</p>`;
  if (policy.selection === "selected") {
    body += `<p>Local file-policy winner: <span class="mono">${esc(policy.selected_source)} → claudeMd</span><br>${esc(bytes(policy.selected_bytes))} of instruction text selected by file order.</p>`;
  } else if (policy.selection === "absent") {
    body += `<p>No embedded instruction field was found in the inspected policy files.</p>`;
  } else {
    body += `<p>Local file-policy selection ${policy.selection === "not_checked" ? "was not checked" : "is unknown because inspection was incomplete"}.</p>`;
  }
  if (policy.helper_present) body += `<p>A policy helper setting is present; its output was not observed.</p>`;
  return body + `<p class="caption">Only decoded instruction text counts here; unrelated settings are excluded. Earlier overridden fields remain inspectable. Effective policy selection, embedded imports and session loading are unverified, so these fields do not establish rule availability.</p>` +
    runDisclosure("project-policy:" + record.id, "Policy source order and field evidence", policy);
}

function pluginCoverage(record) {
  const plugins = record.plugin_discovery;
  if (!plugins) return "";
  let body = `<h3>Plugin instruction sources</h3><p>Registry ${esc(plugins.registry.status.replaceAll("_", " "))} · discovery ${esc(plugins.status.replaceAll("_", " "))} · ${num(plugins.instances.length)} installation records.</p>`;
  if (plugins.registry.path) body += `<p class="mono">${esc(plugins.registry.path)}</p>`;
  if (!plugins.instances.length) body += `<p>${plugins.status === "observed" ? "No plugins were found in the inspected registry and skills directories." : "Plugin coverage is incomplete; an empty list does not establish absence."}</p>`;
  for (const plugin of plugins.instances) {
    const setting = plugin.enablement;
    const enabled = setting.state === "unknown" ? "Unknown settings coverage" : setting.state === "explicit" ? `Explicit setting: ${setting.value === false ? "disabled" : setting.value === true ? "enabled" : setting.value}` : `Manifest default: ${setting.value ? "enabled" : "disabled"}`;
    body += `<details ${reviewDisclosure("project-plugin:" + record.id + ":" + plugin.id)}><summary><span class="mono">${esc(plugin.plugin_id)}</span> · ${esc(plugin.scope)} · ${esc(plugin.status.replaceAll("_", " "))}</summary>` +
      `<p>Installed version: ${esc(plugin.registry_version || "not recorded")} · manifest version: ${esc(plugin.manifest_version || "not recorded")}</p><p>${esc(enabled)}. ${plugin.applicability === "matching" ? "Matches this working copy's recorded scope." : "Belongs to another working-copy path; its instructions were not read."}</p>` +
      `<p class="mono">${esc(plugin.root)}</p><p>${num(plugin.components.length)} observed instruction paths.</p>` +
      runDisclosure("project-plugin-record:" + record.id + ":" + plugin.id, "Complete plugin source record", plugin) + `</details>`;
  }
  return body + `<p class="caption">These are observed installation and configuration inputs. Trust, effective policy, marketplace defaults, dependencies and runtime selection remain unverified. Plugin files do not establish rule availability from this observation alone.</p>` +
    runDisclosure("project-plugins:" + record.id, "Plugin settings sources and coverage", plugins);
}

function projectCoverage(record) {
  if (!record?.files) return "";
  return `<p class="caption">Source profile: ${esc(record.profile)}. Runtime loading remains unverified.</p>` +
    policyCoverage(record) + pluginCoverage(record) +
    (record.managed_discovery ? `<p>Managed instruction files: memory ${esc(record.managed_discovery.memory_status.replaceAll("_", " "))} · enterprise skills ${esc(record.managed_discovery.skills_status.replaceAll("_", " "))}.<br><span class="mono">${esc(record.managed_discovery.root || "Root not inspected")}</span></p>` : "") +
    (record.issues.length ? `<h3>Incomplete coverage</h3>` + record.issues.map(issue => `<p>${esc(issue.cause.replaceAll("_", " "))} · <span class="mono">${esc(issue.path || "")}</span></p>`).join("") : "") +
    `<p class="caption">Unmeasured: ${(record.unobserved_sources || ["runtime_loading"]).map(value => esc(value.replaceAll("_", " "))).join(", ")}.</p>`;
}

function contextHistoryKey(projectKey, copyId) {return JSON.stringify([projectKey, copyId]);}

export async function loadProjectContextHistory(projectKey, {older=false, refresh=false} = {}) {
  const copyId=selectedProjectInventory(projectKey)?.working_copy_id;
  if (!copyId) return;
  const key=contextHistoryKey(projectKey,copyId);
  let entry=state.projectContextHistory[key];
  if (entry?.loading || (entry?.loaded && !older && !refresh) || (older && !entry?.next_cursor)) return;
  entry=entry || {records:[],loaded:false};
  entry.loading=true;entry.error="";state.projectContextHistory[key]=entry;
  const selected=()=>state.inspectorKind==="project" && state.selectedProject===projectKey && state.inspectorTab==="context" && selectedProjectInventory(projectKey)?.working_copy_id===copyId;
  const paint=()=>{if(selected())paintProjectDetail();};
  paint();
  try {
    const data=await getJSON("/api/project-context-history?project_key="+encodeURIComponent(projectKey)+"&working_copy_id="+encodeURIComponent(copyId)+"&limit=20"+(older?"&cursor="+encodeURIComponent(entry.next_cursor):""));
    if(data.project_key!==projectKey || data.working_copy_id!==copyId)throw new Error("Context history response belongs to a different selection");
    Object.assign(entry,data,{records:older?[...entry.records,...data.records]:data.records,loaded:true});
  } catch(error) {entry.error=String(error.message || error);}
  finally {
    entry.loading=false;
    const restore=selected() && doc()?.activeElement?.id==="project-context-older";
    paint();
    if(restore && !entry.next_cursor)doc()?.getElementById("project-context-status")?.focus({preventScroll:true});
  }
}

function contextMeasurements(record, {current=false} = {}) {
  const measurement=record.measurements;
  if(!measurement)return `<p>Separate character and other-Markdown measurements were not recorded for this observation.</p>`;
  let body=`<p class="caption">${esc(measurement.meaning)}</p>`;
  for(const [name,label] of [["instructions","Observed instructions"],["other_markdown","Other repository Markdown"]]) {
    const section=measurement[name], total=section.totals;
    body+=`<section class="project-record"><h3>${label}</h3><p>${total?`${num(total.files)} sources · ${num(total.bytes)} UTF-8 bytes · ${num(total.characters)} characters`:"Measurement unavailable"}${section.status==="partial"?" · partial observation; omitted content is not counted":""}.</p>`;
    if(name==="other_markdown")body+=`<p class="caption">Separate from instruction weight. ${esc(section.scope)}</p>`+
      (section.issues.length?`<p class="error-state">Incomplete: ${section.issues.map(i=>esc(i.cause.replaceAll("_"," "))).join(" · ")}.</p>`:"")+
      runDisclosure(current?"project-context-coverage":"context-measurement:"+record.id,"Files, limits, skipped directories and coverage",section);
    body+="</section>";
  }
  return body;
}

function contextHistory(projectKey, record) {
  if(!record)return "";
  const entry=state.projectContextHistory[contextHistoryKey(projectKey,record.working_copy_id)] || {};
  let body=`<section id="project-context-history" class="project-record"><h3>Context history</h3><p>Changes between adjacent observations of this working copy. These are observed file sizes, not session receipt or a measured benefit.</p>`;
  if(entry.error)body+=`<p role="alert" class="error-state">${esc(entry.error)}</p>`;
  body+=`<p id="project-context-status" tabindex="-1" class="caption" aria-live="polite">${entry.loading?"Reading context history…":entry.loaded?`${num(entry.records.length)} shown · ${entry.count==null?"count unavailable":num(entry.count)+" observation times"}`:"No history loaded yet"}</p>`;
  if(entry.loaded && !entry.records.length)body+=`<p>Context history unavailable: ${esc((entry.reason || "no inventory observations").replaceAll("_"," "))}.</p>`;
  for(const row of entry.records || []) {
    body+=`<details class="project-record" ${reviewDisclosure("context-history:"+record.working_copy_id+":"+row.observed_at)}><summary>${esc(row.observed_at)} · ${esc(row.status)}</summary>`;
    for(const [name,label] of [["instructions","Instructions"],["other_markdown","Other Markdown"]]) {
      const change=row.comparison[name];
      const signed=n=>(n>0?"+":"")+num(n);
      body+=`<p>${label}: ${change.computable?`${signed(change.delta.bytes)} bytes · ${signed(change.delta.characters)} characters · ${signed(change.delta.files)} sources since ${esc(row.previous_observed_at)}`:`comparison unavailable — ${esc(change.reason.replaceAll("_"," "))}`}.</p>`;
    }
    body+=row.status==="conflicting"?`<p class="error-state">Different observations share this timestamp; no exact growth comparison is possible.</p>`:contextMeasurements(row);
    body+=runDisclosure("context-history-record:"+record.working_copy_id+":"+row.observed_at,"Complete retained observations",row.observations)+`</details>`;
  }
  body+=`<button id="project-context-refresh" class="btn" type="button" data-context-refresh="true" aria-disabled="${Boolean(entry.loading)}">Refresh context history</button>`;
  if(entry.next_cursor)body+=` <button id="project-context-older" class="btn" type="button" data-context-older="true" aria-disabled="${Boolean(entry.loading)}">Load older observations</button>`;
  return body+`</section>`;
}

export function renderRecordedContext(key) {
  const record = selectedProjectInventory(key);
  let body = projectCopyControl(key);
  if (!record?.files) return body + contextHistory(key, record);
  body += contextMeasurements(record, {current:true});
  if (!record.context) return body + `<p>This older inventory does not record global origins and source eligibility. Collect a new observation with the current collector. Historical observations remain unchanged.</p>` + contextHistory(key, record) + projectCoverage(record);
  body += `<p>${esc(record.context.meaning)}</p>`;
  if (!record.context.groups.length) body += `<p>${record.status === "partial" ? "No complete instruction inventory is available. Inspect the coverage issues below." : "No supported instruction bytes were observed. Runtime sources outside this profile remain unmeasured."}</p>`;
  for (const group of record.context.groups.filter(group => group.origin === "all")) {
    body += `<section class="project-record"><h3>${group.provider === "codex" ? "Codex" : "Claude Code"}</h3><p>${num(group.files)} instruction sources · ${esc(bytes(group.observed_bytes))} observed</p>` +
      [["Startup candidates", group.startup_bytes], ["Conditional rules", group.conditional_bytes], ["On-demand skill and command bodies", group.on_demand_bytes], ["Ineligible or unresolved", group.unresolved_bytes]].map(([label, value]) => field(label, esc(bytes(value)))).join("") +
      `<p class="caption">Each byte is assigned once per provider, to its broadest eligible loading path. Skills can contribute catalog metadata at startup; that metadata budget is not measured here.</p>`;
    for (const origin of record.context.groups.filter(item => item.provider === group.provider && item.origin !== "all")) {
      body += `<p>${origin.origin === "managed" ? "Managed sources" : origin.origin === "global" ? "Global sources" : "Project sources"}: ${esc(bytes(origin.observed_bytes))} observed · ${esc(bytes(origin.startup_bytes))} startup candidates · ${esc(bytes(origin.conditional_bytes))} conditional · ${esc(bytes(origin.on_demand_bytes))} on demand · ${esc(bytes(origin.unresolved_bytes))} ineligible or unresolved.</p>`;
    }
    body += `<p class="caption">Managed, global and project paths can reach the same physical file. Their rows can overlap; the provider total removes that overlap.</p></section>`;
  }
  return body + contextHistory(key, record) + projectCoverage(record);
}

function projectWiring(record, {limit = null} = {}) {
  if (!record?.files) return `<p class="muted">No unambiguous wiring observation is loaded.</p>`;
  let body = "";
  for (const file of limit ? record.files.slice(0, limit) : record.files) {
    body += `<details ${reviewDisclosure("project-wiring:" + record.working_copy_id + ":" + file.real_path)}><summary class="mono">${esc(projectFileName(file.path, record.working_copy.normalized_path))}</summary>`;
    for (const path of file.loading_paths) {
      body += `<p>${esc(path.provider)} · ${esc(path.origin || "origin unknown")} · ${esc(path.scope.kind.replaceAll("_", " "))}</p>` +
        `<p class="caption">${esc(bytes(path.eligible_prefix_bytes))} eligible of ${esc(bytes(file.bytes))} observed${path.eligibility_reason ? ` · ${esc(path.eligibility_reason.replaceAll("_", " "))}` : ""}.</p>`;
      if (path.command_name) body += `<p class="mono">/${esc(path.command_name)} <span class="caption">· ${["command", "plugin_command"].includes(path.source) ? "legacy command" : "skill"} · recorded candidate, invocation unverified</span></p>`;
      if (path.plugin_id) body += `<p class="mono">Plugin: ${esc(path.plugin_id)}</p>`;
      if (path.shadowed_by?.length) body += `<p class="caption mono">Shadowed by: ${path.shadowed_by.map(esc).join(" · ")}</p>`;
      if (path.conditions.length) body += `<p>Conditions: ${path.conditions.map(condition => condition.paths.map(esc).join(", ")).join("; ")}</p>`;
      if (path.import_chain.length) body += `<p class="mono">${path.import_chain.map(p => esc(projectFileName(p, record.working_copy.normalized_path))).join(" → ")} → ${esc(projectFileName(path.path, record.working_copy.normalized_path))}</p>`;
    }
    if (file.loading_paths.some(path => path.source === "embedded_memory")) body += `<p><a href="#/rules?mode=evidence&kinds=instruction&project_key=${encodeURIComponent(record.project_key)}&query=${encodeURIComponent(file.path)}">Browse retained text for this field</a></p>`;
    if (file.loading_paths.some(path => path.plugin_id)) body += `<p><a href="#/rules?mode=evidence&kinds=instruction&project_key=${encodeURIComponent(record.project_key)}&query=${encodeURIComponent(file.path)}">Browse retained plugin instruction text</a></p>`;
    body += `<p class="caption mono">${file.loading_paths.some(path => path.source === "embedded_memory") ? "Embedded field identity" : "Physical file"}: ${esc(file.real_path)}</p>` +
      (file.aliases.length > 1 ? `<p class="caption mono">Aliases: ${file.aliases.map(esc).join(" · ")}</p>` : "") +
      runDisclosure("project-wiring-record:" + record.working_copy_id + ":" + file.real_path, "Complete recorded loading paths", file.loading_paths) + `</details>`;
  }
  if (!record.files.length) body += `<p>No supported files were recorded. Inspect coverage before interpreting this as empty.</p>`;
  if (limit && record.files.length > limit) body += `<p class="caption">${num(limit)} of ${num(record.files.length)} files shown.</p>`;
  return body;
}

export function renderRecordedTopology(key) {
  const record = selectedProjectInventory(key);
  return projectCopyControl(key) + `<p>These paths were observed during collection. Imports and aliases share physical file counts. Instruction delivery revalidates its destination separately.</p>` + projectWiring(record) + projectCoverage(record);
}

export function renderProjectPage(key) {
  const entry = state.projectDetails[key] || {}, data = entry.data;
  const legacy = state.projects?.rows.find(row => row.project_key === key);
  const title = data?.label || legacy?.label || key;
  let body = `<nav class="run-breadcrumb" aria-label="Breadcrumb"><a href="#/projects">Projects</a> / ${esc(title)}</nav><header class="run-heading"><div><h1 id="project-title" tabindex="-1" class="view__title">${esc(title)}</h1><p class="mono caption">${esc(key)}</p>` +
    (data?.displays.length > 1 ? `<p class="caption">Recorded names: ${data.displays.map(esc).join(" · ")}</p>` : "") + `</div><button id="project-refresh" class="btn" type="button" data-project-summary-refresh="true">Refresh project</button></header>`;
  body += renderProjectNavigation(key, state.inspectorTab);
  if (state.inspectorTab !== "summary") {
    if (state.inspectorTab === "sessions") return body + projectPanel("Session evidence", renderProjectSessions(key));
    if (state.inspectorTab === "loads") return body + projectPanel("Native loading reports", renderProjectNativeEvents(key));
    if (state.inspectorTab === "recurrence") return body + projectPanel("Observed rule recurrence", renderRecurrence(key,"project"));
    if (["inventory", "availability", "exposure"].includes(state.inspectorTab)) {
      const selected=selectedProjectInventory(key);
      const inventory=state.projectInventorySelection[key] ? projectCopyControl(key) + (selected ? renderProjectInventory({records:[selected],loaded:true,count:1,statusId:"project-ownership-status"}) : "") : renderProjectInventory(state.projectInventory[key] || {});
      const content = state.inspectorTab === "inventory" ? inventory : state.inspectorTab === "availability" ? renderProjectAvailability(state.projectAvailability[key] || {}) : renderProjectExposure(state.projectExposures[projectExposureUrl(key, state.projectExposureParams)]?.data, state.projectExposures[projectExposureUrl(key, state.projectExposureParams)] || {});
      return body + projectPanel(PROJECT_TABS.find(t => t.id === state.inspectorTab).label, content);
    }
    if (state.inspectorTab === "context") return body + projectPanel("Instruction context", renderRecordedContext(key));
    if (state.inspectorTab === "topology") return body + projectPanel("Observed instruction wiring", renderRecordedTopology(key));
    return body + projectPanel("Indexed project context", `<p class="footnote">This summary uses retained index records. Current filesystem presence and session access are not inferred.</p>` + (legacy ? renderProjectInspector(legacy, state.inspectorTab, {}, {includeTabs: false}) : `<p>No legacy index row is retained. Use the observed inventory above.</p>`));
  }
  if (entry.error) return body + `<div class="error-state" role="alert">${esc(entry.error)}</div>`;
  if (!data) return body + `<p role="status">Reading project detail…</p>`;
  const first = selectedProjectInventory(key);
  const summary=state.projectRuleSummaries[projectRuleSummaryKey(key)]?.data;
  body += `<div class="tiles project-tiles">` + [["Known indexed sessions", num(data.indexed.known_session_ids), `${num(data.indexed.transcripts)} files · ${num(data.indexed.unknown_session_ids)} without IDs`], ["Retained incidents", num(data.incidents), "Queue evidence; inspect Exposure for rates"], ["Matched at last checks", summary?.counts.project.matched == null ? "Unknown" : num(summary.counts.project.matched), "Project revisions · exact selected copy"], ["Observed instruction size", first?.totals && !(first.status === "partial" && !first.files?.length) ? bytes(first.totals.bytes) : EM_DASH, first ? `${first.working_copy.normalized_path.split("/").pop()} · ${first.status}` : "No copy inventory loaded"]].map(([label, value, note],index) => `<div class="tile"><div class="tile__label">${label}</div><div class="tile__value"${index===2?` id="project-rule-matched"`:""}>${esc(value)}</div><div class="tile__sub">${esc(note)}</div></div>`).join("") + `</div>`;
  const copyRows = data.indexed_paths.map(path => `<tr><td><details ${reviewDisclosure("project-path:" + key + ":" + path.path)}><summary class="mono">${esc(path.path.split("/").pop() || "Path unknown")}</summary><p class="mono">${esc(path.path)}</p></details></td><td class="num">${num(path.transcripts)}</td><td class="num">${num(path.known_session_ids)}</td></tr>`).join("");
  const wiring = projectCopyControl(key) + projectWiring(first, {limit: 8});
  body += `<div class="project-columns"><div class="project-primary">` +
    projectPanel("Machine-written rules here", `<div id="project-rule-summary">${renderProjectRuleSummary(key)}</div>`) +
    projectPanel("Configured context and Markdown", projectConfiguredContext(key,first)) +
    projectPanel("Proposed edits", `<details ${reviewDisclosure("project-complete-proposals:"+key)}><summary>Inspect all proposed edits</summary><p class="caption">Undecided proposals use current canonical destinations and the shared execution policy.</p>` + projectRecordList(key, "proposals") + `</details>`) +
    projectPanel("Lessons contributed", `<details ${reviewDisclosure("project-complete-contributed:"+key)}><summary>Inspect all contributed lessons</summary>` + projectRecordList(key, "contributed") + `</details>`) +
    projectPanel("Recorded deliveries", `<details ${reviewDisclosure("project-complete-deliveries:"+key)}><summary>Inspect all retained deliveries</summary><p class="caption">${esc(data.delivery_note)} Historical revisions; availability is separate.</p>` + projectRecordList(key, "deliveries") + `</details>`) +
    `</div><aside class="project-secondary">` + projectPanel("How instructions are wired", wiring + `<p><a href="${esc(projectReadHref(key,"topology"))}">All loading paths and coverage</a></p>`) +
    projectPanel("Working copies and indexed paths", `<p class="caption">Raw index paths can include aliases. They are not counted as separate canonical projects.</p><div class="scroll-x"><table class="data"><thead><tr><th>Indexed path</th><th class="num">Files</th><th class="num">Known IDs</th></tr></thead><tbody>${copyRows}</tbody></table></div><p class="caption">${esc(data.session_note)}</p>` + runDisclosure("project-sessions:" + key, "Session coverage and observed copy identities", {indexed:data.indexed, observed:data.observed, working_copies:data.observed_working_copies})) +
    projectPanel("Exposure and availability", `<p>Compare timestamped signals with physical lines and inspect the exact delivered text observed in each working copy.</p><p><a href="${esc(projectReadHref(key,"exposure"))}">Inspect exposure and detector versions</a></p><p><a href="${esc(projectReadHref(key,"availability"))}">Inspect recorded rule availability</a></p><p><a href="${esc(projectReadHref(key,"sessions"))}">Inspect native session context and startup candidates</a></p><p><a href="${esc(projectReadHref(key,"recurrence"))}">Inspect recorded before/after recurrence</a></p><p class="caption">Runtime instruction receipt remains unverified; recurrence comparisons describe observed associations.</p>`) + `</aside></div>`;
  return body;
}

export function paintProjectDetail() {
  if (state.inspectorKind !== "project" || !state.selectedProject) return;
  const active=doc()?.activeElement;
  // Replacing a focused native select clears its type-ahead buffer mid-choice.
  // Keep the control alive until the user leaves it, then paint the latest state.
  const requestedCopy=state.projectInventorySelection[state.selectedProject] || selectedProjectInventory(state.selectedProject)?.working_copy_id || "";
  if(active?.id==="project-copy" && active.addEventListener && active.value===requestedCopy &&
      active.getAttribute("data-copy-project-key")===state.selectedProject && active.getAttribute("data-project-tab")===state.inspectorTab) {
    if(!active.projectPaintPending) {
      active.projectPaintPending=true;
      active.addEventListener("blur",()=>{
        active.projectPaintPending=false;
        // Let native Tab transfer focus before replacing and restoring its target.
        setTimeout(()=>paintProjectDetail(),0);
      },{once:true});
    }
    return;
  }
  preserveOperationView("project-detail-body", () => renderProjectPage(state.selectedProject));
}

/* ------------------------------ painting --------------------------------- */

function paintOverviewFailures(failures) {
  const body=byId("ov-failures");
  body.querySelectorAll?.("details[data-review-key]").forEach(el=>{
    state.reviewDisclosures[el.getAttribute("data-review-key")]=el.open;
  });
  const html=renderOverviewFailures(failures);
  if(body.innerHTML===html)return;
  const expanded=body.querySelector?.("details")?.open;
  const active=doc().activeElement,controls=Array.from(body.querySelectorAll?.("a,button,summary") || []);
  const index=controls.indexOf(active),identity=active?.getAttribute?.("href") || active?.getAttribute?.("aria-label");
  body.innerHTML=html;
  const disclosure=body.querySelector?.("details");if(disclosure)disclosure.open=Boolean(expanded);
  if(index>=0){
    const next=Array.from(body.querySelectorAll("a,button,summary"));
    const target=identity?next.find(el=>(el.getAttribute("href") || el.getAttribute("aria-label"))===identity):next[index];
    (target || disclosure?.querySelector("summary"))?.focus({preventScroll:true});
  }
}

function paintOverviewReadState() {
  const data=state.overview;
  const message=state.overviewLoading ? (data ? "Refreshing Overview…" : "") :
    state.overviewError ? `${state.overviewError}${data ? " Showing the previous read." : ""}` : "";
  setText("ov-read-state",message);
  setVisible(byId("ov-read-state"),Boolean(message));
  if (!data) {
    setText("ov-statusline",state.overviewLoading ? "Loading…" : "Overview unavailable");
    setHTML("ov-loop",`<p class="caption">${state.overviewLoading ? "Loading recorded populations…" : "Recorded populations unavailable."}</p>`);
    setHTML("ov-deliveries",`<p class="caption">${state.overviewLoading ? "Loading reviewed deliveries…" : "Reviewed delivery history unavailable."}</p>`);
    setText("nav-freshness",state.overviewLoading ? "Loading…" : "Data freshness unavailable");
    setHTML("ov-grid",state.overviewLoading
      ? '<div class="loading"><span class="loading__bar"></span></div>'
      : emptyState("Run history unavailable","Use Refresh to try the read again.",""));
  }
}

export function paintOverview() {
  paintOverviewReadState();
  const data = state.overview;
  if (!data) return;
  const banner = renderBanner(data.freshness);
  const bannerElement = byId("ov-banner");
  bannerElement.innerHTML = banner.html;
  if (banner.state) bannerElement.setAttribute("data-state", banner.state);
  else bannerElement.removeAttribute("data-state");
  setVisible(bannerElement, Boolean(banner.show));

  const review=overviewReview(data),line=data.status_line || {};
  const delivered=overviewDelivered(data.audit?.loop?.delivery);
  const status=`Ran ${num(line.nights_ran)} of the last ${num(line.nights_window)} nights. ${num(line.rules_learned_total)} rules learned, ${delivered==null?'delivery history unknown':num(delivered)+' target deliveries'}, ${num(review.count)} waiting on you.`;
  setText("ov-statusline",status);
  setHTML("ov-policy",renderOverviewPolicy(data.audit?.policy));
  setHTML("ov-loop",renderOverviewLoop(data.audit?.loop));
  setHTML("ov-loop-coverage",renderOverviewLoopCoverage(data.audit?.loop));
  setHTML("ov-deliveries",renderOverviewDeliveries(data.audit?.loop?.delivery));
  setHTML("ov-latest",renderOverviewRunSummary(data.audit?.latest_run));
  setHTML("ov-statusline-sub", renderStatusSub(data.status_line, data.freshness));
  setHTML("ov-tiles", renderTiles(data));

  const grid = data.grid || {};
  setHTML("ov-grid", renderGrid(grid, { numbers: state.numbers }));
  setHTML("ov-grid-legend", renderGridLegend(grid, {compact:true}));
  setHTML("ov-grid-foot", renderGridFoot(grid) + `<ul class="legend legend--definitions">${renderGridLegend(grid)}</ul>`);
  setText(
    "ov-grid-meta",
    `${num(grid.runs_total)} runs · ${(grid.nights || []).length} nights drawn · times are UTC`
  );

  setHTML("ov-backlog", renderBacklog(data.backlog,data.audit?.mining));
  setHTML("ov-backlog-foot", renderBacklogFoot(data.backlog));
  paintOverviewFailures(data.failures);
  const failureClasses=recentFailureClasses(data.failures).length;
  setText("ov-failures-meta", `${failureClasses} recent class${failureClasses===1 ? "" : "es"}`);
  setHTML("ov-gate", renderGate(data.gate));
  setHTML("ov-inbox", renderOverviewQueue(review));
  setText("ov-inbox-meta", `${num(review.count)} waiting`);
  setHTML("ov-confidence", renderConfidence(data.confidence));

  const inbox = data.inbox || {};
  // Review refreshes after every decision. An older Overview response must
  // not put its pre-decision count back into the shared navigation.
  paintInboxCount(state.review || inbox);
  setText("nav-freshness", (data.freshness || {}).banner || "");
  const trendGate = doc().getElementById("trend-gate");
  if (trendGate) trendGate.innerHTML = renderEvaluationHealth(state.evaluationHealth || {});
}

/* Complete retained evidence is a separate selection in the existing Rules browser. */
const EVIDENCE_LABELS={learning:"Rule",proposal:"Proposal",incident:"Incident",session:"Session",instruction:"Instruction source",revision:"Delivered revision"};
const EVIDENCE_TABS=[{id:"diagnosis",label:"Diagnosis"},{id:"source",label:"Source"},{id:"linked",label:"Linked evidence"}];
function evidenceMode(){return state.route==="rules" && new URLSearchParams(state.ruleQuery).get("mode")==="evidence";}
function evidenceValues(){return Object.fromEntries(new URLSearchParams(state.ruleQuery));}
export function evidenceHref(kind="",id="",values=evidenceValues()) {
  const params=new URLSearchParams();
  for(const key of ["query","kinds","project_key","cursor","tab","linked_cursor"])if(values[key])params.set(key,values[key]);
  params.set("mode","evidence");
  if(!kind){params.delete("tab");params.delete("linked_cursor");}
  return "#/rules"+(kind ? "/evidence/"+encodeURIComponent(kind)+"/"+encodeURIComponent(id) : "")+"?"+params;
}
function changeEvidenceSelection(changes){
  clearTimeout(state.rulesSearchTimer);state.rulesSearchTimer=null;
  setHash(evidenceHref("","",{...evidenceValues(),query:byId("rules-search-input").value,...changes,cursor:""}));
}
function evidencePageURL(){
  const all=new URLSearchParams(state.ruleQuery),params=new URLSearchParams();
  for(const key of ["query","kinds","project_key","cursor"])if(all.get(key))params.set(key,all.get(key));
  return API.evidence+(params.size ? "?"+params : "");
}
function paintEvidenceMode(active){
  if(!doc()?.getElementById("evidence-results"))return;
  for(const el of (doc().querySelectorAll?.("[data-rule-only]") || []))setVisible(el,!active);
  for(const el of (doc().querySelectorAll?.("[data-evidence-mode]") || []))el.setAttribute("aria-pressed",String(el.getAttribute("data-evidence-mode")===(active ? "evidence" : "rules")));
  setVisible(byId("evidence-filters"),active);setVisible(byId("evidence-results"),active);
}
export function renderEvidenceRows(rows,values=evidenceValues()) {
  return rows.map(r=>`<article class="evidence-result"><span class="evidence-kind">${esc(EVIDENCE_LABELS[r.kind] || r.kind)}</span>`+
    `<a class="evidence-title" data-evidence-key="${esc(r.kind+":"+r.source_id)}" href="${esc(evidenceHref(r.kind,r.source_id,{...values,tab:"diagnosis",linked_cursor:""}))}" ${state.selectedEvidence?.kind===r.kind && state.selectedEvidence?.id===r.source_id ? 'aria-current="true"' : ""}>${esc(r.title || r.source_id)}${r.title_cut ? "…" : ""}</a>`+
    `<p class="evidence-excerpt">${esc(r.excerpt.replace(/\s+/g," ").trim())}</p>${renderSourceIdentity(r.identity)}<p class="caption">${num(r.project_count)} known project${r.project_count===1 ? "" : "s"} · ${num(r.incident_count)} linked incident${r.incident_count===1 ? "" : "s"}${r.excerpt_cut ? " · Excerpt; open Source for complete retained content" : ""}</p></article>`).join("");
}
function evidencePagination(page,linked=false){
  if(!page)return "";const p=page.pagination,name=linked ? "linked" : "results";
  return `<span ${linked ? 'id="evidence-linked-status"' : 'id="evidence-results-status"'} tabindex="-1">${p.count ? num(p.offset+1)+"–"+num(p.offset+page.rows.length) : "0"} of ${num(p.count)} ${linked ? "linked incidents" : "results"}</span><div class="rules-member-pages">`+
    (p.offset ? `<button class="btn" data-evidence-page="first" data-evidence-list="${name}">First ${name}</button>` : "")+
    (p.next_cursor ? `<button class="btn" data-evidence-page="next" data-evidence-list="${name}">Next ${name}</button>` : "")+`</div>`;
}
export function renderEvidenceCoverage(c={}){
  const context=c.session_context || {},instruction=c.instruction_text || {};
  return `<p>${esc(c.note || "Coverage has not been read.")}</p><p>Instruction text: ${instruction.count == null ? "schema unavailable; observed-copy count unknown" : num(instruction.count)+" observed copies"+ (instruction.reason ? " · "+esc(instruction.reason) : "")}. ${Object.entries(c.instruction_copy_states || {}).map(([status,count])=>esc(status)+": "+num(count)).join(" · ")}</p>`+
    `<p>Session context: ${context.available ? `${num(context.records)} retained records · ${num(context.empty_batches)} recorded empty batches · ${num(context.missing_batches)} missing batches` : "schema unavailable; no coverage claim"}. ${esc(context.reason || "")}</p>`+
    `<p>Native reports: ${c.native_reports?.available ? num(c.native_reports.records)+" retained" : "unavailable"}. Delivered revisions: ${c.revision_archive?.available ? num(c.revision_archive.records)+" retained" : "unavailable"}.</p>`+
    `<details><summary>Complete coverage record</summary>${fullRecord(c)}</details>`;
}
function rulesReadFailure(entry, options) {
  return readFailure(entry,{...options,summaryId:"rules-read-error-"+options.key});
}

// Keep native disclosures and the currently focused recovery control through a
// same-record repaint. A different selected record never inherits that focus.
function paintRulesReadView(id, html, identity) {
  const root=byId(id),active=doc()?.activeElement;
  const same=root.getAttribute?.("data-rules-read-identity")===identity;
  const focused=same && root.contains?.(active) ? active?.id : "";
  const disclosures=new Map(same ? Array.from(root.querySelectorAll?.("details[data-review-key]") || [],el=>[el.getAttribute("data-review-key"),el.open]) : []);
  for(const [key,open] of disclosures)state.reviewDisclosures[key]=open;
  setHTML(id,typeof html==="function" ? html() : html);
  root.setAttribute?.("data-rules-read-identity",identity);
  for(const el of root.querySelectorAll?.("details[data-review-key]") || []) {
    const key=el.getAttribute("data-review-key");if(disclosures.has(key))el.open=disclosures.get(key);
  }
  if(focused && (focused==="rules-member-status" || ["rules-read-","scan-history-","mining-history-"].some(prefix=>focused.startsWith(prefix))))doc()?.getElementById(focused)?.focus({preventScroll:true});
}

export function paintEvidenceBrowser(){
  if(!evidenceMode() || !doc()?.getElementById("evidence-results"))return;
  const focusedKey=doc().activeElement?.getAttribute?.("data-evidence-key");
  paintEvidenceMode(true);setVisible(byId("rules-table-wrap"),false);setVisible(byId("rules-empty"),false);
  const entry=state.evidencePage || {},data=entry.data,values=evidenceValues(),input=byId("rules-search-input");
  if(doc().activeElement!==input)input.value=values.query || "";
  const kinds=byId("evidence-kind");
  // URLs may select multiple kinds even though the compact control offers one.
  const selectedKinds=values.kinds || "";
  kinds.innerHTML='<option value="">All sources</option>'+Object.entries(EVIDENCE_LABELS).map(([key,label])=>`<option value="${key}">${esc(label)}</option>`).join("")+
    (selectedKinds && !EVIDENCE_LABELS[selectedKinds] ? `<option value="${esc(selectedKinds)}">Selected sources: ${esc(selectedKinds)}</option>` : "");kinds.value=selectedKinds;
  const projects=new Map((state.projects?.rows || []).map(p=>[p.project_key,p.label || p.project_key]));
  if(values.project_key && !projects.has(values.project_key))projects.set(values.project_key,values.project_key);
  byId("evidence-project").innerHTML='<option value="">All projects</option>'+[...projects].filter(([key])=>key).map(([key,label])=>`<option value="${esc(key)}">${esc(label)}</option>`).join("");byId("evidence-project").value=values.project_key || "";
  byId("rules-refresh").disabled=false;byId("rules-refresh").setAttribute("aria-disabled",String(Boolean(entry.loading)));
  paintRulesReadView("rules-notice",()=>entry.loading ? '<p role="status">Reading retained evidence…</p>' : entry.error ? rulesReadFailure(entry,{key:"evidence-list",title:"Could not read retained evidence.",guidance:"Use Refresh to restart from current evidence."}) : "",entry.key);
  setText("rules-meta",data ? `${num(data.pagination.count)} matching sources · ${Object.entries(data.source_counts).map(([kind,count])=>num(count)+" "+EVIDENCE_LABELS[kind].toLowerCase()+(count===1 ? "" : "s")).join(" · ")}` : entry.error ? "Evidence could not be read" : "Reading retained evidence…");
  setText("rules-search-meta",data ? num(data.pagination.count)+" sources" : "");
  setHTML("evidence-results",data ? data.rows.length ? renderEvidenceRows(data.rows) : emptyState("No matching evidence","Try another query, source or project.","Only retained content is searched.") : "");
  setHTML("rules-foot",evidencePagination(data));setHTML("rules-grouping",data ? renderEvidenceCoverage(data.coverage) : "");
  if(focusedKey)(Array.from(doc().querySelectorAll("[data-evidence-key]")).find(el=>el.getAttribute("data-evidence-key")===focusedKey) || byId("rules-meta")).focus({preventScroll:true});
}
export async function loadEvidencePage({refresh=false,focus=false}={}){
  const key=evidencePageURL(),old=state.evidencePage;
  if(old?.key===key && old.loading)return;
  if(!refresh && old?.key===key){paintEvidenceBrowser();return;}
  const entry={key,loading:true};state.evidencePage=entry;paintEvidenceBrowser();
  const focused=doc()?.activeElement;
  try{entry.data=await getJSON(key);}catch(error){entry.error=String(error.message || error);entry.errorDetail=error?.readDetail || "";}
  finally{const canRestore=doc()?.activeElement===focused;entry.loading=false;if(state.evidencePage===entry && evidenceMode()){paintEvidenceBrowser();if(focus && canRestore)byId("rules-meta").focus({preventScroll:true});}}
}
function evidenceText(text){return `<pre class="evidence-text" tabindex="0">${esc(text ?? "")}</pre>`;}
function sourceIncident(s){
  return renderSourceIdentity(s.identity)+`<p>${esc(s.signal_type)} · ${esc(s.status)} · ${s.time_status==="known" ? esc(s.ts) : "Incident time unknown"}</p>`+
    renderIncidentPrimary(s, s.presentation || {display_text:s.matched_text || "No readable text was retained.",fingerprint:s.fingerprint})+
    section("Complete retained window",(s.window || []).map((w,i)=>`<article class="evidence-window"><h4>${esc(w.role || "Occurrence "+(i+1))}</h4>`+
      ("count_in_session" in w || "session_file" in w ? `<p>${w.count_in_session!=null ? num(w.count_in_session) + " recorded in session" : "Count unknown"} · ${esc(w.ts || "time unknown")} · <code>${esc(w.session_file || "Session path unknown")}</code> · <code>${esc(w.project_path || "Project location unknown")}</code></p>` : `<p>${esc(w.ts || "Time unknown")}</p>`)+evidenceText(w.text || "")+`<details><summary>Window metadata</summary>${fullRecord(w)}</details></article>`).join("") || '<p>No window content was retained.</p>')+`<p class="footnote">${esc(s.retention)}</p>`;
}
export function renderEvidenceSource(data){
  const s=data.source;let body="";
  if(data.kind==="incident")body=sourceIncident(s);
  if(data.kind==="learning")body=section("Complete rule",evidenceText(s.rule_text))+section("Reason",evidenceText(s.why))+section("Incident summary",evidenceText(s.incident_summary))+(s.violated_existing_rule ? section("Reported existing-rule violation",evidenceText(s.violated_existing_rule)) : "");
  if(data.kind==="proposal")body=`<p>${esc(s.status)} · ${esc(s.action)}</p>`+section("Proposed target",evidenceText(s.target_path))+`<p class="footnote">${esc(s.target_attribution)}</p>`+section("Complete proposed patch",`<pre class="evidence-text" tabindex="0">${renderUnifiedDiff(s.diff_unified)}</pre>`);
  if(data.kind==="instruction")body=section("Observed instruction source",evidenceText(s.file.path))+`<p>${esc(s.observed_at)} · ${s.latest_selected_observation ? "Latest retained observation" : "Historical observation"} · Latest inventory: ${esc(s.latest_inventory_status)}</p>`+section("Complete retained redacted text",evidenceText(s.file.text))+`<p class="footnote">${esc(s.meaning)}</p>`;
  if(data.kind==="revision")body=section("Delivered rule revision",evidenceText(s.content))+section("Recorded destination",fullRecord(s.destination))+`<p class="footnote">${esc(s.meaning)}</p>`;
  if(data.kind==="session")body=renderSourceIdentity(s.identity)+`<p>${esc(s.provider_label || sourceLabel(s.provider))} · ${esc(s.native_session_id || "Native identity unknown")}</p><p class="footnote">${esc(s.retention)}</p>`+
    [ ["Physical transcript records",s.transcripts],["Native context records",s.native_context],["Native reports",s.native_reports] ].map(([label,records])=>section(label,(records || []).map((r,i)=>`<details><summary>${esc(label)} ${i+1}</summary>${fullRecord(r)}</details>`).join("") || "<p>No records retained. This does not establish a recorded zero.</p>")).join("")+
    section("Retained incidents",(s.incidents || []).map(i=>`<details><summary>${esc(i.presentation?.display_text || i.matched_text || i.id)}</summary>${sourceIncident(i)}</details>`).join("") || "<p>No incidents linked.</p>");
  return body+`<details><summary>Complete source fields and identity</summary>${fullRecord(s)}</details>`;
}
function diagnosisLink(d){
  const id=encodeURIComponent(d.source_id), labels={mine_incident:"Preview mining this incident",review_proposal:"Review this proposal",command:"Inspect this command",operation:"Inspect this operation",project_availability:"Inspect working-copy availability",learning:"Inspect this rule"};
  const href=d.kind==="mine_incident" ? "#/review/mine/"+id : d.kind==="review_proposal" ? "#/review/proposal/"+id : ["command","operation"].includes(d.kind) ? "#/review/"+d.kind+"/"+id : d.kind==="project_availability" ? "#/projects/"+id+"?tab=availability" : d.kind==="learning" ? evidenceHref("learning",d.source_id) : d.kind==="recovery" && ["hook","correct_target"].includes(d.mode) ? "#/review/recovery/"+encodeURIComponent(JSON.stringify({learning_id:d.source_id,mode:d.mode,target_id:"",proposal_ids:[]})) : "";
  return href ? `<a class="review-link" href="${esc(href)}">${esc(d.kind==="recovery" ? d.mode==="hook" ? "Preview hook recovery" : "Preview a corrected target" : labels[d.kind])}</a>` : "";
}
export function renderEvidenceDiagnosis(data){
  const d=data.diagnosis;
  return `<p class="footnote">${esc(d.meaning)}</p>`+(d.facts.length ? d.facts.map(f=>section(f.title,
    `<p class="caption">${esc(f.certainty)}</p><p>${esc(f.detail)}</p>`+(f.report ? evidenceText(f.report) : "")+
    (f.working_copy ? `<p class="mono evidence-path">${esc(f.working_copy.normalized_path)}</p><p>Observed ${esc(f.observed_at)} · ${esc(f.cause)}</p>`+
      (f.incident_time_relations || []).map(r=>`<p class="footnote">Incident ${esc(r.incident_id)}: observation ${esc(r.relation.replaceAll("_"," "))}${r.incident_at ? " ("+esc(r.incident_at)+")" : ""}.</p>`).join("")+`<details><summary>Observed loading scope</summary>${fullRecord(f.scope)}</details>` : "")+
    `<div class="evidence-actions">${(f.destinations || []).map(diagnosisLink).join("")}</div><details><summary>Exact recorded fact and identifiers</summary>${fullRecord(f)}</details>`)).join("") : "<p>No diagnostic fact was retained for this source.</p>")+
    section("What remains unknown",d.limitations.map(l=>`<p>${esc(l)}</p>`).join("") || "<p>See the limits on each observation above.</p>")+`<details><summary>Mining provenance for linked rules</summary>${fullRecord(data.provenance)}</details>`;
}
function renderLinkedEvidence(data){
  let html="";
  if(data.kind==="learning"){
    const entry=state.linkedEvidence || {};html=entry.error ? rulesReadFailure(entry,{key:"linked:"+state.selectedEvidence.id,title:"Could not read linked incidents.",guidance:"Use Refresh evidence to restart the linked page."}) : entry.loading ? '<p role="status">Reading linked incidents…</p>' : entry.data ? `<p>${num(entry.data.project_count)} known projects · ${num(entry.data.unknown_project_incidents)} incidents with unknown project identity.</p>`+renderEvidenceRows(entry.data.rows)+evidencePagination(entry.data,true) : "";

  } else {
    html=section("Linked rules",data.learning_ids.map(id=>`<p><a href="${esc(evidenceHref("learning",id))}">${esc(id)}</a></p>`).join("") || "<p>No rule is linked.</p>")+section("Linked incidents",data.incident_ids.map(id=>`<p><a href="${esc(evidenceHref("incident",id))}">${esc(id)}</a></p>`).join("") || "<p>No incident is linked.</p>");
  }
  return html+section("Linked proposals",(data.proposal_ids || []).map(id=>`<p><a href="${esc(evidenceHref("proposal",id,{...evidenceValues(),tab:"diagnosis",linked_cursor:""}))}">${esc(id)}</a></p>`).join("") || "<p>No proposal is linked.</p>")+section("Canonical projects",data.project_keys.map(key=>`<p><a class="evidence-path" href="#/projects/${encodeURIComponent(key)}">${esc(key)}</a></p>`).join("") || "<p>Project identity is unknown.</p>");
}
function paintEvidenceInspector(){
  if(!evidenceMode() || state.inspectorKind!=="evidence")return;
  const entry=state.evidenceDetail,active=state.inspectorTab;
  const html=entry?.loading ? '<p role="status">Reading complete retained source…</p>' : entry?.error ? rulesReadFailure(entry,{key:"evidence:"+entry.key,title:"Could not read this evidence source.",guidance:"Use Refresh evidence to retry."}) : entry?.data ? (active==="source" ? renderEvidenceSource(entry.data) : active==="linked" ? renderLinkedEvidence(entry.data) : renderEvidenceDiagnosis(entry.data)) : "";
  openInspector(entry?.data?.title || "Evidence details",renderTabs(EVIDENCE_TABS,active, tab => evidenceHref(state.selectedEvidence.kind,state.selectedEvidence.id,{...evidenceValues(),tab}))+`<p><button id="rules-read-evidence-refresh" class="btn btn--quiet" data-evidence-detail-refresh="true" aria-disabled="${Boolean(entry?.loading)}">Refresh evidence</button></p>`+html);
}
export async function openEvidence(kind,id,{refresh=false}={}){
  const key=kind+":"+id,changed=state.selectedEvidence?.kind!==kind || state.selectedEvidence?.id!==id;
  state.selectedRule="";state.selectedEvidence={kind,id};state.inspectorKind="evidence";
  const tab=new URLSearchParams(state.ruleQuery).get("tab");state.inspectorTab=EVIDENCE_TABS.some(t=>t.id===tab) ? tab : "diagnosis";
  if(!changed && state.evidenceDetail?.key===key && state.evidenceDetail.loading){paintEvidenceInspector();return;}
  if(!refresh && !changed && state.evidenceDetail?.key===key){
    // Let the linked reader capture paging focus before replacing its button.
    if(state.inspectorTab==="linked" && kind==="learning")loadLinkedEvidence();else paintEvidenceInspector();return;
  }
  const entry={key,loading:true};state.evidenceDetail=entry;state.linkedEvidence=null;paintEvidenceInspector();paintEvidenceBrowser();
  if(changed){byId("inspector-title").setAttribute("tabindex","-1");byId("inspector-title").focus({preventScroll:true});}
  try{entry.data=await getJSON(API.evidence+"/"+encodeURIComponent(kind)+"/"+encodeURIComponent(id));}catch(error){entry.error=String(error.message || error);entry.errorDetail=error?.readDetail || "";}
  finally{entry.loading=false;if(state.evidenceDetail===entry && state.selectedEvidence?.kind===kind && state.selectedEvidence?.id===id){paintEvidenceInspector();if(evidenceMode() && state.inspectorTab==="linked" && kind==="learning" && entry.data)loadLinkedEvidence();}}
}
export async function loadLinkedEvidence(){
  const selected=state.selectedEvidence;if(!selected || selected.kind!=="learning")return;
  const cursor=new URLSearchParams(state.ruleQuery).get("linked_cursor"),key="/api/learnings/"+encodeURIComponent(selected.id)+"/evidence"+(cursor ? "?cursor="+encodeURIComponent(cursor) : "");
  if(state.linkedEvidence?.key===key){paintEvidenceInspector();return;}
  const focusedPage=byId("inspector-body").contains?.(doc().activeElement) && doc().activeElement?.getAttribute?.("data-evidence-page");
  const entry={key,loading:true};state.linkedEvidence=entry;paintEvidenceInspector();
  if(focusedPage)byId("inspector-body").querySelector?.('.tabs [data-tab="linked"]')?.focus({preventScroll:true});
  try{entry.data=await getJSON(key);}catch(error){entry.error=String(error.message || error);entry.errorDetail=error?.readDetail || "";}
  finally{entry.loading=false;if(state.linkedEvidence===entry && state.inspectorTab==="linked"){
    const stillPaging=focusedPage && byId("inspector-body").contains?.(doc().activeElement) && doc().activeElement?.getAttribute?.("data-tab")==="linked";
    paintEvidenceInspector();if(stillPaging)doc()?.getElementById("evidence-linked-status")?.focus({preventScroll:true});
  }}
}
function handleEvidenceInspector(event){
  const selected=state.selectedEvidence;if(!selected)return false;
  if(findAttr(event.target,"data-evidence-detail-refresh")){const values={...evidenceValues(),linked_cursor:""};state.ruleQuery=new URLSearchParams(values).toString();setHash(evidenceHref(selected.kind,selected.id,values));openEvidence(selected.kind,selected.id,{refresh:true});return true;}
  const tab=findAttr(event.target,"data-tab");if(tab){setHash(evidenceHref(selected.kind,selected.id,{...evidenceValues(),tab}));return true;}
  if(findAttr(event.target,"data-evidence-page")){const next=findAttr(event.target,"data-evidence-page")==="next";setHash(evidenceHref(selected.kind,selected.id,{...evidenceValues(),linked_cursor:next ? state.linkedEvidence?.data?.pagination.next_cursor : ""}));return true;}
  return false;
}
function handleEvidenceClick(event){
  const mode=findAttr(event.target,"data-evidence-mode");if(mode){setHash(mode==="evidence" ? evidenceHref("","",{query:byId("rules-search-input").value}) : rulesHref("",{query:byId("rules-search-input").value}));return true;}
  if(!evidenceMode())return false;
  const page=findAttr(event.target,"data-evidence-page");if(page){state.evidenceFocus=true;setHash(evidenceHref("","",{...evidenceValues(),cursor:page==="next" ? state.evidencePage?.data?.pagination.next_cursor : ""}));return true;}
  if(findAttr(event.target,"data-rules-refresh")){
    const selected=state.selectedEvidence,values={...evidenceValues(),cursor:"",linked_cursor:""};state.ruleQuery=new URLSearchParams(values).toString();setHash(evidenceHref(selected?.kind,selected?.id,values));loadEvidencePage({refresh:true});if(selected)openEvidence(selected.kind,selected.id,{refresh:true});return true;
  }
  return false;
}

function paintExcludedProposal(entry){
  if(entry.loading || entry.rendered || state.selectedProposalRead!==entry || state.route!=="review" || state.reviewRouteProposal!==entry.id)return;
  const id=entry.id;
  setHTML("review-selected-proposal",`<h2>Selected proposal</h2><p class="mono">${esc(id)}</p>`+
    (entry.error ? `<p role="alert">${esc(entry.error)}</p>` : `<p>Recorded state: ${esc(entry.data.snapshot.proposal.status)}. This proposal is not in the current Review queue; no decision controls are offered here.</p><p><a class="review-link" href="${esc(evidenceHref("proposal",id,{tab:"diagnosis"}))}">Inspect this proposal’s evidence and execution</a></p>`+renderReviewDiff(entry.data.snapshot.proposal.diff_unified,"selected:"+id,"Complete proposed change")+`<details><summary>Exact current proposal and retained evidence</summary>${fullRecord(entry.data)}</details>`)+
    `<button class="btn" data-selected-proposal-refresh="true">Refresh selected proposal</button>`);
  entry.rendered=true;byId("review-selected-proposal").focus({preventScroll:true});
}
export async function loadExcludedProposal(id,{refresh=false}={}){
  if(!refresh && state.selectedProposalRead?.id===id){paintExcludedProposal(state.selectedProposalRead);return;}
  const entry={id,loading:true};state.selectedProposalRead=entry;
  setHTML("review-selected-proposal",'<p role="status">Reading the selected proposal…</p>');
  try{entry.data=await getJSON("/api/proposals/"+encodeURIComponent(id)+"/review");}
  catch(error){entry.error=String(error.message || error);}
  finally{entry.loading=false;paintExcludedProposal(entry);}
}

/* Exact execution links must work even when their record is outside history's first page. */
export async function openExecutionRoute(kind,id,{refresh=false}={}){
  if(!refresh && state.executionRoute?.kind===kind && state.executionRoute.id===id)return;
  const entry={kind,id,loading:true};state.executionRoute=entry;
  setHTML("review-execution",'<p role="status">Reading the selected execution record…</p>');
  try{
    // Read detail after any in-flight summary so a prior snapshot cannot overwrite it.
    await (kind==="command" ? state.delivery.promise : state.operations.promise);
    if(state.executionRoute!==entry || state.route!=="review")return;
    const data=await getJSON((kind==="command" ? API.commands : API.operations)+"/"+encodeURIComponent(id));
    if(data.id!==id)throw new Error("The execution reader returned a different record.");
    if(state.executionRoute!==entry || state.route!=="review")return;
    // Finish an older summary request before publishing the explicit full detail.
    await (kind==="command" ? state.delivery.promise : state.operations.promise);
    if(state.executionRoute!==entry || state.route!=="review")return;
    const newest=(kind==="command" ? state.delivery.items : state.operations.items).find(r=>r.id===id);
    if(newest && newest.updated_at>data.updated_at)throw new Error("This execution changed while its detail was loading. Refresh the selected record.");
    if(kind==="command"){state.deliveryDetails[id]={data};mergeDeliveries([data]);paintDeliveryHistory();}
    else {state.operationDetails[id]={data};mergeOperations([data]);paintOperationHistory();}
    const card=doc().getElementById((kind==="command" ? "delivery-command-" : "operation-")+id);
    card?.querySelector("details")?.setAttribute("open","");
    (kind==="command" ? state.deliveryDisclosures : state.operationDisclosures)[id]=true;
    card?.focus();card?.scrollIntoView({block:"center"});
    setHTML("review-execution",`<p>Selected ${esc(kind)}: <span class="mono">${esc(id)}</span>. <button class="btn" data-execution-refresh="true">Refresh selected record</button></p>`);
  }catch(error){if(state.executionRoute===entry && state.route==="review")setHTML("review-execution",`<p role="alert">${esc(error.message || error)}</p><button class="btn" data-execution-refresh="true">Retry selected record</button>`);}
  finally{entry.loading=false;}
}

/* Display families only group browsing. Review retains its own authorization. */
export function ruleDataParams(query = state.ruleQuery) {
  const source = new URLSearchParams(query), params = new URLSearchParams();
  for (const key of ["query", "target", "sort", "grouping", "cursor"]) if (source.get(key)) params.set(key, source.get(key));
  return params;
}
function rulesPageURL(query = state.ruleQuery) {
  const params=new URLSearchParams(query).get("mode")==="evidence" ? "" : ruleDataParams(query).toString(); return API.rules + (params ? "?"+params : "");
}
export function rulesHref(id = "", values = Object.fromEntries(new URLSearchParams(state.ruleQuery))) {
  const params=new URLSearchParams();
  for (const [key,value] of Object.entries(values)) if(value !== "" && value != null) params.set(key,String(value));
  return "#/rules" + (id ? "/"+encodeURIComponent(id) : "") + (params.size ? "?"+params : "");
}
function changeRuleSelection(changes) {
  clearTimeout(state.rulesSearchTimer);state.rulesSearchTimer=null;
  const values={...Object.fromEntries(new URLSearchParams(state.ruleQuery)),query:byId("rules-search-input").value,...changes,cursor:"",member_cursor:"",family:"",tab:"",scan:""};
  setHash(rulesHref("",values));
}
function dismissInspector() {
  if(state.inspectorKind==="evidence") {
    const selected=state.selectedEvidence;closeInspector();setHash(evidenceHref());
    const row=Array.from(doc().querySelectorAll("[data-evidence-key]")).find(el=>el.getAttribute("data-evidence-key")===selected.kind+":"+selected.id);
    (row || byId("rules-meta")).focus({preventScroll:true});return;
  }
  const id=state.selectedRule, paged=state.rules?.mode === "paged" && state.inspectorKind === "rule";
  closeInspector();
  if(paged) {
    setHash(rulesHref("",{...Object.fromEntries(new URLSearchParams(state.ruleQuery)),tab:"",scan:""}));
    const row=Array.from(doc().querySelectorAll("[data-rule-id]")).find(el=>el.getAttribute("data-rule-id")===id);
    (row || byId("rules-meta")).focus({preventScroll:true});
  }
}
export async function loadRulePage({refresh=false,focus=""}={}) {
  const key=rulesPageURL(), old=state.rulePage;
  if(old?.key===key && old.loading)return;
  if(refresh)state.ruleDetails={};
  if(!refresh && old?.key===key) {paintRuleBrowser(); loadRuleMembers(); return;}
  const entry={key,loading:true,error:"",data:old?.key===key ? old.data : null};state.rulePage=entry;
  paintRuleBrowser();
  const focused=doc()?.activeElement;
  try {entry.data=await getJSON(key);} catch(error){entry.error=String(error.message || error);entry.errorDetail=error?.readDetail || "";}
  finally {
    const canRestore=doc()?.activeElement===focused;
    entry.loading=false;
    if(state.rulePage===entry) {
      if(entry.data) state.rules=entry.data;
      paintRuleBrowser();
      if(state.route==="rules" && state.selectedRule)openRule(state.selectedRule,state.inspectorTab);
      if(!entry.error)loadRuleMembers({refresh});
      if(state.route==="rules" && focus && canRestore) doc().getElementById(focus)?.focus({preventScroll:true});
    }
  }
}
export async function loadRuleDetail(id,{refresh=false}={}) {
  if(state.ruleDetails[id]?.loading)return;
  if(!refresh && state.ruleDetails[id])return;
  const entry={loading:true,revision:state.rulePage?.data?.revision};state.ruleDetails[id]=entry;
  if(state.selectedRule===id)openRule(id,state.inspectorTab);
  try {entry.data=await getJSON("/api/rules/"+encodeURIComponent(id));} catch(error){entry.error=String(error.message || error);entry.errorDetail=error?.readDetail || "";}
  finally {
    entry.loading=false;
    if(state.ruleDetails[id]===entry && state.route==="rules" && state.selectedRule===id) {
      openRule(id,state.inspectorTab);
      const scan=new URLSearchParams(state.ruleQuery).get("scan");
      if(scan && state.inspectorTab==="evidence" && Array.from(doc().querySelectorAll("[data-scan-history-root]")).some(el=>el.getAttribute("data-scan-history-root")===scan))loadIncidentScanHistory(scan);
    }
  }
}
function ruleMemberURL() {
  const all=new URLSearchParams(state.ruleQuery),family=all.get("family");if(!family)return "";
  const params=ruleDataParams();params.delete("cursor");
  if(all.get("member_cursor"))params.set("cursor",all.get("member_cursor"));
  return "/api/rule-families/"+encodeURIComponent(family)+"/members"+(params.size ? "?"+params : "");
}
export async function loadRuleMembers({refresh=false,focus=false}={}) {
  const key=ruleMemberURL(), revision=state.rulePage?.data?.revision;
  if(!key || state.rulePage?.loading || state.rulePage?.error)return;
  if(state.ruleMembers?.key===key && state.ruleMembers.revision===revision && state.ruleMembers.loading)return;
  if(!refresh && state.ruleMembers?.key===key && state.ruleMembers.revision===revision)return;
  const entry={key,revision,loading:true};state.ruleMembers=entry;paintRuleBrowser();
  if(focus)doc()?.getElementById("rules-member-status")?.focus({preventScroll:true});
  try {
    entry.data=await getJSON(key);
    if(entry.data.revision!==revision)throw new Error("Rules changed while opening this family. Refresh the results.");
  } catch(error){entry.error=String(error.message || error);entry.errorDetail=error?.readDetail || "";entry.data=null;}
  finally {
    const canRestore=doc()?.activeElement?.id==="rules-member-status";
    entry.loading=false;
    if(state.ruleMembers===entry) {
      paintRuleBrowser();
      if(focus && canRestore && state.route==="rules")doc().getElementById("rules-member-status")?.focus({preventScroll:true});
    }
  }
}
const RULE_TARGET_LABELS={global_claude_md:"Global CLAUDE.md",codex_global:"Global AGENTS.md",project_agents_md:"AGENTS.md",project_claude_md:"CLAUDE.md",rule_file:"Rule file",skill:"Skill",hook:"Hook"};
function rulePathLabel(path) {
  return path.split("/").map((part,index,all)=>`<span>${esc(part)}${index<all.length-1 ? "/" : ""}</span>`).join("");
}
function ruleTargetLabels(rows) {
  const paths=[...new Set(rows.flatMap(row=>(row.targets || []).map(t=>t.path)))];
  return new Map(paths.map(path=>{
    const parts=path.split("/");let count=Math.min(2,parts.length);
    while(count<parts.length && paths.some(other=>other!==path && other.split("/").slice(-count).join("/")===parts.slice(-count).join("/")))count++;
    return [path,count<parts.length ? "…/"+parts.slice(-count).join("/") : path];
  }));
}
function renderRuleSummary(row,child=false,targetLabels=new Map()) {
  const labels=Object.keys(row.proposal_statuses || {}), states=labels.length ? labels : [row.status];
  const statuses=states.map(status=>renderRuleStatus(status,labels.length ? "proposal" : "learning")).join("");
  const generation=row.miner_generation?.computable ? row.miner_generation.value : "Miner unknown";
  const targets=(row.targets || []).map(t=>`<details class="rules-target" data-rule-path="${esc(JSON.stringify([row.id,t.kind,t.path]))}"><summary>${esc(RULE_TARGET_LABELS[t.kind] || t.kind)}<small>${rulePathLabel(targetLabels.get(t.path) || t.path)}</small></summary><code>${esc(t.path)}</code></details>`).join("");
  return `<tr class="rules-member" data-child="${child}" data-rule-row="${esc(row.id)}" data-selected="${row.id===state.selectedRule}">`+
    `<td><div class="rules-statuses">${statuses}</div></td>`+
    `<td><a class="rule-open" data-rule-id="${esc(row.id)}" href="${esc(rulesHref(row.id,{...Object.fromEntries(new URLSearchParams(state.ruleQuery)),tab:"why",scan:""}))}"${row.id===state.selectedRule ? ' aria-current="true"' : ""}>${esc(row.title || row.rule_text)}</a><p>${esc(row.rule_text)}</p><small>${num(row.evidence_linked)} linked incidents · ${num(row.project_count)} projects${row.unknown_project_incidents ? ` · ${num(row.unknown_project_incidents)} unknown project` : ""}${row.enforcement_gap ? " · Reported violation" : ""}</small></td>`+
    `<td>${targets || `<span class="muted">No target</span>`}${row.target_paths>3 ? `<small>+${num(row.target_paths-3)} recorded paths</small>` : ""}</td>`+
    `<td>${esc((row.agent_products || []).map(sourceLabel).join(" · ") || "Agent unknown")}<small>${esc(generation)}</small>${row.unknown_source_incidents ? `<small>${num(row.unknown_source_incidents)} incidents: agent unknown</small>` : ""}</td></tr>`;
}
function rulePagination(data,member=false) {
  const p=data.pagination,attr=member ? "data-rule-member-page" : "data-rule-page";
  return `<span${member ? ' id="rules-member-status" tabindex="-1" role="status"' : ""}>${p.count ? p.offset+1 : 0}–${p.offset+data.rows.length} of ${num(p.count)} ${member ? "matching members" : data.options.grouping ? "families" : "rules"}</span><span class="rules-page-buttons">`+
    (p.offset ? `<button class="btn btn--quiet" ${attr}="first">First ${member ? "members" : "page"}</button>` : "")+
    (p.next_cursor ? `<button class="btn btn--quiet" ${attr}="next">Next ${member ? "members" : "page"}</button>` : "")+`</span>`;
}
export function renderRuleBrowserRows(data) {
  const expanded=new URLSearchParams(state.ruleQuery).get("family");
  const members=state.ruleMembers?.key===ruleMemberURL() ? state.ruleMembers : null;
  const labels=ruleTargetLabels([...data.rows.filter(r=>r.kind==="rule").map(r=>r.rule),...(members?.data?.rows || [])]);
  return data.rows.map(row=>{
    if(row.kind==="rule")return renderRuleSummary(row.rule,false,labels);
    const open=row.family_id===expanded, members=state.ruleMembers?.key===ruleMemberURL() ? state.ruleMembers : null;
    let html=`<tr class="rules-family"><td colspan="4"><button id="family-${esc(row.family_id)}" data-rule-family="${esc(row.family_id)}" aria-expanded="${open}" aria-controls="members-${esc(row.family_id)}"><img src="/icons/rules-expand.svg" width="8" height="8" alt=""><span><strong>${esc(row.representative.title || row.representative.rule_text)}</strong><small>${num(row.matched_members)} matching / ${num(row.total_members)} total members · ${num(row.target_paths)} recorded target paths · ${num(row.proposal_count)} proposals</small></span><span class="rules-family-label">Similar rules</span></button></td></tr>`;
    if(open) {
      html+=`<tr class="rules-member-notice" id="members-${esc(row.family_id)}"><td colspan="4">`+
        (members?.error ? `<div id="rules-member-status" tabindex="-1">${rulesReadFailure(members,{key:"members:"+row.family_id,title:"Could not read family members.",guidance:"Use Retry members. If the rules changed, use Refresh for current results."})}</div><button id="rules-read-members-retry" class="btn" data-rule-members-refresh="true">Retry members</button>` : members?.data ? `Display grouping: ${esc(row.basis)}. Each member keeps its own Review decisions.` : `<span id="rules-member-status" tabindex="-1" role="status">Reading family members…</span>`) + `</td></tr>`;
      if(members?.data)html+=members.data.rows.map(r=>renderRuleSummary(r,true,labels)).join("")+`<tr class="rules-member-notice"><td colspan="4"><div class="rules-member-pages">${rulePagination(members.data,true)}</div></td></tr>`;
    }
    return html;
  }).join("");
}
export function paintRuleBrowser() {
  if(evidenceMode()){paintEvidenceBrowser();return;}
  paintEvidenceMode(false);
  if(!doc()?.getElementById("rules-notice"))return;
  const active=doc().activeElement, focusId=active?.id, focusedRule=active?.getAttribute?.("data-rule-id");
  const paths=Array.from(byId("rules-results").querySelectorAll?.("details[data-rule-path]") || []);
  const openPaths=new Set(paths.filter(el=>el.open).map(el=>el.getAttribute("data-rule-path")));
  const focusedPath=paths.find(el=>el.contains?.(active))?.getAttribute("data-rule-path");
  const entry=state.rulePage || {},data=entry.data,params=new URLSearchParams(state.ruleQuery);
  const input=byId("rules-search-input");if(doc().activeElement!==input)input.value=params.get("query") || "";
  byId("rules-group-toggle").checked=params.get("grouping")!=="false";
  byId("rules-sort").value=params.get("sort") || "evidence";
  for(const el of (doc().querySelectorAll?.("[data-rule-target]") || []))el.setAttribute("aria-pressed",String(el.getAttribute("data-rule-target")===(params.get("target") || "all")));
  paintRulesReadView("rules-notice",()=>entry.loading ? `<p role="status">Reading rules…</p>` : entry.error ? rulesReadFailure(entry,{key:"rules-list",title:"Could not read rules.",guidance:(data ? "Showing the previous results. " : "")+"Use Refresh to start with current results."}) : "",entry.key);
  byId("rules-refresh").disabled=false;byId("rules-refresh").setAttribute("aria-disabled",String(Boolean(entry.loading)));
  if(!data) {
    setHTML("rules-results","");setHTML("rules-foot","");setText("rules-meta",entry.error ? "Rules could not be read" : "Reading rules…");
    setVisible(byId("rules-table-wrap"),false);setVisible(byId("rules-empty"),false);return;
  }
  setText("rules-meta",`${num(data.counts.members)} matching rules · ${num(data.counts.families)} families · ${num(data.counts.proposals)} proposals · ${num(data.counts.target_paths)} recorded target paths`);
  setText("rules-search-meta",`${num(data.counts.members)} / ${num(data.counts.all_members)}`);
  paintRulesReadView("rules-results",()=>renderRuleBrowserRows(data),entry.key+":"+params.get("family"));
  for(const el of (byId("rules-results").querySelectorAll?.("details[data-rule-path]") || [])) {
    const key=el.getAttribute("data-rule-path");el.open=openPaths.has(key);
    if(key===focusedPath)el.querySelector("summary")?.focus({preventScroll:true});
  }
  setVisible(byId("rules-table-wrap"),Boolean(data.rows.length));
  setVisible(byId("rules-empty"),!data.rows.length);
  setHTML("rules-empty",data.rows.length ? "" : emptyState(data.counts.all_members ? "No matching rules" : "No rules yet",data.counts.all_members ? "Try another search or target filter." : "No learning has been recorded.",""));
  setHTML("rules-foot",rulePagination(data));
  if(focusId?.startsWith("family-"))doc().getElementById(focusId)?.focus({preventScroll:true});
  if(focusedRule)Array.from(doc().querySelectorAll("[data-rule-id]")).find(el=>el.getAttribute("data-rule-id")===focusedRule)?.focus({preventScroll:true});
  const coverage=data.coverage;
  setHTML("rules-grouping",`<p>${esc(coverage.status)} · ${esc(coverage.reason || "Current retained grouping")}</p><p>${esc(data.search_coverage)}</p><p>Grouping only changes this list. It does not combine approvals or evidence projects. Missing vectors: ${num(coverage.missing_vector_count)}.</p>`);
}
function handleRuleBrowserFocus(event) {
  const family=event.target.closest?.(".rules-family button[data-rule-family]");
  if(!family)return;
  const viewport=family.closest("#rules-table-wrap"),row=family.getBoundingClientRect(),bounds=viewport.getBoundingClientRect();
  // A wide row may already intersect the scrollport while its identity is hidden.
  if(row.left<bounds.left || row.left>=bounds.right)
    family.scrollIntoView({block:"nearest",inline:"start",behavior:"instant"});
}
function handleRuleBrowserClick(event) {
  const target=event.target;
  if(findAttr(target,"data-rules-refresh")) {
    const values={...Object.fromEntries(new URLSearchParams(state.ruleQuery)),cursor:"",member_cursor:""};
    state.ruleQuery=new URLSearchParams(Object.entries(values).filter(([,v])=>v!=="")).toString();
    setHash(rulesHref(state.selectedRule,values));loadRulePage({refresh:true,focus:"rules-refresh"});
    if(state.selectedRule)loadRuleDetail(state.selectedRule,{refresh:true});return true;
  }
  const filter=findAttr(target,"data-rule-target");if(filter){changeRuleSelection({target:filter});return true;}
  const family=findAttr(target,"data-rule-family");
  if(family) {const values=Object.fromEntries(new URLSearchParams(state.ruleQuery));setHash(rulesHref(state.selectedRule,{...values,family:values.family===family ? "" : family,member_cursor:""}));return true;}
  const memberPage=findAttr(target,"data-rule-member-page"),page=findAttr(target,"data-rule-page");
  if(memberPage || page) {
    const values=Object.fromEntries(new URLSearchParams(state.ruleQuery)),member=Boolean(memberPage),pagination=(member ? state.ruleMembers?.data : state.rulePage?.data)?.pagination;
    values[member ? "member_cursor" : "cursor"]=(memberPage || page)==="next" ? pagination.next_cursor : "";
    if(!member){values.family="";values.member_cursor="";}
    state.ruleQuery=new URLSearchParams(Object.entries(values).filter(([,v])=>v!=="")).toString();setHash(rulesHref(state.selectedRule,values));
    if(member)loadRuleMembers({focus:true});else loadRulePage({focus:"rules-meta"});return true;
  }
  if(findAttr(target,"data-rule-members-refresh")){loadRuleMembers({refresh:true,focus:true});return true;}
  return false;
}

export function paintRules() {
  if(evidenceMode()){paintEvidenceBrowser();return;}
  const data = state.rules;
  if (!data) return;
  if (data.mode === "paged") {paintRuleBrowser(); return;}
  setText("rules-meta", `${num(data.count)} rules · evidence sample capped at ${num(data.evidence_sample)} per rule`);
  setHTML("rules-grouping", renderGrouping(data.grouping));
  paintRuleRows();
}

/** Re-paint just the result rows. Called on every keystroke, so it must be cheap. */
export function paintRuleRows() {
  if(evidenceMode()){paintEvidenceBrowser();return;}
  const data = state.rules;
  if (!data) return;
  if (data.mode === "paged") {paintRuleBrowser(); return;}
  const match = matchRules(data.rows, state.query);
  setHTML("rules-results", renderRuleRows(match.rows, state.selectedRule));
  const empty = byId("rules-empty");
  const table = byId("rules-table-wrap");
  if (match.rows.length === 0) {
    empty.innerHTML = renderRulesEmpty(state.query, data.count);
    setVisible(empty, true);
    // Hide the table too. Leaving it left a stranded seven-column header strip
    // sitting on top of the explanation, which reads as a broken table rather
    // than as the designed state it is.
    if (table) setVisible(table, false);
  } else {
    empty.innerHTML = "";
    setVisible(empty, false);
    if (table) setVisible(table, true);
  }
  setText(
    "rules-search-meta",
    match.searched ? `${match.rows.length} of ${data.count}` : `${data.count} rules`
  );
  const byStatus = Object.keys(data.by_status || {})
    .sort()
    .map((status) => `${status} ${data.by_status[status]}`)
    .join(" · ");
  setText(
    "rules-foot",
    `${match.rows.length} shown of ${data.count}. ${byStatus}. Open Review to decide proposals.`
  );
}

export function paintProjects() {
  const data = state.projects;
  byId("projects-refresh").disabled = state.projectsLoading;
  setHTML("projects-read-state", state.projectsLoading ? "Reading repositories…" : state.projectsError ?
    `<span class="error-state">Could not read repositories: ${esc(state.projectsError)}</span>${data ? " Showing the previous read." : ""} <button class="btn" type="button" data-project-refresh="true">Retry repositories</button>` : "");
  if (!data) { paintProjectRows(); return; }
  setText(
    "projects-meta",
    `${num(data.count)} repos · ${num(data.clone_paths_total)} working copies`
  );
  setHTML("projects-foot", renderProjectsFoot(data));
  setHTML("projects-notes", renderProjectNotes(data));
  setHTML("projects-collapse-note", `<img src="/icons/project-identity.svg" alt="" width="6" height="6"> ${num(data.clone_paths_total)} indexed working copies collapse to ${num(data.count)} repositories. Expand a row to inspect its retained paths.`);
  const missing = data.rows.filter(row => row.benefit?.computable !== true).length;
  setHTML("projects-benefit-note", `<img src="/assets/evidence-dot.svg" alt="" width="6" height="6"> ` +
    (!data.count ? "No repository comparisons are available yet." : missing ? `Benefit is unavailable for ${num(missing)} of ${num(data.count)} repositories. Open a project for its measurement requirements.` : "Recorded comparisons are available for every listed repository.") +
    " A lower recurrence rate is an observed association, not proof of causation.");
  setText("projects-coverage-count", `· ${num(data.not_a_repository?.count || 0)} non-repository paths · ${num(data.proposals_not_attributed?.length || 0)} unattributed proposals`);
  paintProjectRows();
  if(evidenceMode())paintEvidenceBrowser();
}

function handleProjectBenefitFocus(event) {
  // A raised explanation must not cover the next native keyboard target.
  // Focus outside the table (for example Refresh) keeps the inspected state.
  event.currentTarget.querySelectorAll(".project-benefit-detail[open]").forEach(detail => {
    if (detail.closest("tr") !== event.target.closest("tr")) detail.open = false;
  });
}

export function paintProjectRows() {
  byId("projects-tbody").addEventListener?.("focusin", handleProjectBenefitFocus);
  const data = state.projects;
  if (!data) {
    setHTML("projects-tbody", `<tr><td colspan="8" class="projects-empty">${state.projectsLoading || state.initialLoading ? "Reading repositories…" : state.projectsError ? "Repository data is unavailable. Retry the read above." : "No repositories have been read yet."}</td></tr>`);
    setText("projects-page-status", ""); setHTML("projects-pagination", ""); return;
  }
  const page = projectListPage(data.rows, state.projectListQuery);
  preserveOperationView("projects-tbody", () => page.rows.length ? renderProjectRows(page.rows, state.selectedProject, state.projectExpanded) :
    `<tr><td colspan="8" class="projects-empty">${page.total ? `No repositories match “${esc(page.query)}”. Clear the search to see all ${num(page.total)} repositories.` : "No indexed repositories. Repository rows appear after a transcript scan records project identities."}</td></tr>`);
  setText("projects-page-status", `${num(page.start)}–${num(page.end)} of ${num(page.count)} ${page.query ? "matching " : ""}repositories${page.query ? ` · ${num(page.total)} total` : ""} · Page ${page.page} of ${page.pages}`);
  setHTML("projects-pagination", `<button class="btn btn--quiet" type="button" data-project-page="${page.page-1}"${page.page === 1 ? " disabled" : ""}>Previous</button><button class="btn btn--quiet" type="button" data-project-page="${page.page+1}"${page.page === page.pages ? " disabled" : ""}>Next</button>`);
  for (const [key, spec] of Object.entries(PROJECT_SORTS)) {
    const header = byId("project-sort-"+key);
    if (key === page.sort) header.setAttribute("aria-sort", page.direction === "asc" ? "ascending" : "descending");
    else header.removeAttribute("aria-sort");
    const button = header.querySelector?.("button");
    button?.setAttribute("aria-label", `Sort by ${spec.label}, ${key === page.sort && page.direction === "asc" ? "descending" : key === page.sort ? "ascending" : spec.direction === "asc" ? "ascending" : "descending"}`);
  }
}

export function changeProjectSelection(changes, focus = "projects-page-status") {
  const params = new URLSearchParams(state.projectListQuery);
  params.delete("page");
  for (const [key, value] of Object.entries(changes)) value ? params.set(key, String(value)) : params.delete(key);
  state.projectsFocus = focus;
  const hash = "#/projects" + (params.size ? "?"+params : "");
  if (currentHash() === hash) showRoute(parseRoute(hash)); else setHash(hash);
}

export function submitProjectSearch(event) {
  if (event.target.id !== "projects-search-form") return;
  event.preventDefault();
  changeProjectSelection({q:byId("projects-search").value.trim()});
}

export async function loadProjects() {
  const generation = ++state.projectsReadGeneration;
  const initiator = doc().activeElement;
  const restoreFocus = initiator?.getAttribute?.("data-project-refresh");
  state.projectsLoading = true; state.projectsError = ""; paintProjects();
  try {
    const data = await getJSON(API.projects);
    if (generation === state.projectsReadGeneration) state.projects = data;
  } catch (error) {
    if (generation === state.projectsReadGeneration) state.projectsError = String(error.message || error);
  } finally {
    if (generation === state.projectsReadGeneration) {
      state.projectsLoading=false;paintProjects();
      if (restoreFocus && state.route === "projects" && !state.selectedProject &&
          (doc().activeElement === initiator || doc().activeElement === doc().body)) byId("projects-refresh").focus?.({preventScroll:true});
    }
  }
}

/* Evaluation reads are independent of trend filters and never enqueue model work. */
const EVAL_SELECTORS = ["learning_id", "proposal_id", "run_id", "command_id"];
export function evalQueryParts(query = "") {
  const all = new URLSearchParams(query), trends = new URLSearchParams(), history = new URLSearchParams();
  for (const [key, value] of all) if (!key.startsWith("quality_")) (key.startsWith("eval_") ? history : trends).set(key, value);
  return {all, trends, history};
}
export function evaluationHref(kind, id, query = "") {
  return `#/evals/${kind}/${encodeURIComponent(id)}` + (query ? "?" + query : "");
}
export function ruleEvaluationHref(id) {
  return "#/evals?eval_learning_id=" + encodeURIComponent(id);
}
function evaluationNotice(outcome) {
  return `<div class="eval-notice" data-state="${esc(outcome.tone)}"><strong>${esc(outcome.label)}</strong><p>${esc(outcome.reason)}</p></div>`;
}
export function renderEvaluationHistory(entry = {}) {
  const params = new URLSearchParams(entry.params || ""), kind = params.get("eval_kind") || "attempt";
  const selector = EVAL_SELECTORS.find(key => params.has("eval_" + key)) || "learning_id";
  let html = `<form id="evaluation-filter" class="eval-filter exposure-form"><label>History<select id="evaluation-kind" name="eval_kind"><option value="attempt"${kind === "attempt" ? " selected" : ""}>Recorded attempts</option><option value="unlinked"${kind === "unlinked" ? " selected" : ""}>Unlinked historical results</option></select></label>` +
    `<details ${reviewDisclosure("evaluation-filter")}><summary>Filter attempts by exact source ID</summary><label>Source<select name="selector" id="evaluation-selector">${EVAL_SELECTORS.map(key => `<option value="${key}"${key === selector ? " selected" : ""}>${esc(key.replace("_id", "").replaceAll("_", " "))}</option>`).join("")}</select></label><label>Exact ID<input id="evaluation-value" name="value" value="${esc(params.get("eval_" + selector) || "")}" placeholder="All recorded attempts"></label><p class="caption">Source filters apply to recorded attempts. Historical results have no inferred source revision.</p></details>` +
    `<button id="evaluation-submit" class="btn" type="submit">Show history</button></form>`;
  if (entry.loading) return html + `<p role="status">Reading evaluation history…</p>`;
  if (entry.error) return html + `<p role="alert" class="error-state">${esc(entry.error)}</p><button class="btn" id="evaluation-retry" data-evaluation-refresh="true">Retry history</button>`;
  const data = entry.data;
  if (!data) return html + `<p>Evaluation history has not been loaded.</p>`;
  html += `<p id="evaluation-page-status" tabindex="-1" aria-live="polite">${num(data.records.length)} shown · ${data.count == null ? "history count unknown" : `${num(data.count)} ${kind === "unlinked" ? "unlinked results" : "recorded attempts"}`}</p><p class="caption">${esc(data.reason)}</p>`;
  if (!data.records.length) html += `<p>${data.computable ? "No records match this selection." : "Complete attempt history is unavailable for this database."}</p>`;
  html += `<div class="eval-cards" tabindex="0" role="region" aria-label="Evaluation history records">`;
  html += data.records.map(row => `<article class="eval-card"><p class="caption"><time>${esc(row.created_at || "Time not recorded")}</time> · ${esc(row.state)}</p><h3>${esc(row.rule_text || "Historical evaluation · " + row.subject_id)}</h3>` +
    `<p><strong>${esc(row.outcome.label)}</strong></p>` +
    (row.kind === "attempt" ? `<p class="caption">${num(row.completed_scenarios)} of ${num(row.scenarios)} scenario results · ${num(row.paired_scenarios)} comparable pairs<br>${num(row.calls_recorded)} results / ${num(row.calls_started)} logical call starts</p>` : `<p class="caption">Exact source revision and model calls unknown.</p>`) +
    (row.observed_arms ? `<dl class="eval-card-arms">${["with", "without"].map(arm => { const a = row.observed_arms[arm]; return `<div><dt>${arm === "with" ? "With" : "Without"} the rule</dt><dd>${a.completed ? `${num(a.observed_passes)} / ${num(a.completed)} passed` : "No completed trials"}</dd></div>`; }).join("")}</dl>` : "") +
    `<p class="caption">Recorded verdict: ${esc(row.verdict || "none")}</p><a id="evaluation-link-${esc(row.id)}" data-evaluation-open="${esc(row.id)}" data-evaluation-kind="${esc(row.kind)}" href="${esc(evaluationHref(row.kind, row.id, state.evaluationQuery || ""))}">Inspect complete evaluation<span class="visually-hidden"> ${esc(row.id)}</span> →</a></article>`).join("") + `</div>`;
  html += `<div class="eval-pagination">` + (params.has("eval_cursor") ? `<button id="evaluation-first" class="btn" data-evaluation-page="first">Newest records</button>` : "") +
    (data.next_cursor ? `<button id="evaluation-older" class="btn" data-evaluation-page="older">Older records</button>` : "") +
    `<button id="evaluation-refresh" class="btn btn--quiet" data-evaluation-refresh="true">Refresh history</button></div>`;
  return html;
}
export function paintEvaluationHistory() {
  if (state.route === "evals" && !state.evaluationDetail && !state.qualityDetail && doc().getElementById("evaluation-history-body"))
    preserveOperationView("evaluation-history-body", () => renderEvaluationHistory(state.evaluationHistory || {}));
}
export async function loadEvaluationHistory(query = "", {refresh = false} = {}) {
  const {history} = evalQueryParts(query), params = history.toString(), old = state.evaluationHistory;
  if (!refresh && old?.params === params && (old.loading || old.data)) {paintEvaluationHistory(); return;}
  const focus = doc().activeElement?.id || "";
  const entry = {params, data:null, loading:true, error:""}; state.evaluationHistory = entry; paintEvaluationHistory();
  try {
    const kind = history.get("eval_kind") || "attempt", request = new URLSearchParams({limit:"20"});
    if (!["attempt", "unlinked"].includes(kind)) throw new Error("Unknown evaluation history kind.");
    if (history.get("eval_cursor")) request.set("cursor", history.get("eval_cursor"));
    if (kind === "attempt") {
      request.set("summary", "true");
      for (const key of EVAL_SELECTORS) if (history.has("eval_" + key)) request.set(key, history.get("eval_" + key));
    }
    entry.data = await getJSON((kind === "attempt" ? API.evalAttempts : API.evalResults) + "?" + request);
  } catch(error) {entry.error = String(error.message || error);}
  finally {
    entry.loading = false;
    if (state.evaluationHistory === entry) {
      paintEvaluationHistory();
      if (state.route === "evals" && !state.evaluationDetail && !state.qualityDetail && focus.startsWith("evaluation-")) {
        const target = doc().getElementById(["evaluation-older", "evaluation-first"].includes(focus) ? "evaluation-page-status" : focus);
        target?.focus?.({preventScroll:true});
      }
    }
  }
}
export function submitEvaluationFilter(event) {
  if (event.target.id !== "evaluation-filter") return;
  event.preventDefault();
  const fields = new FormData(event.target), params = evalQueryParts(state.evaluationQuery).trends;
  const kind = fields.get("eval_kind"), selector = fields.get("selector"), value = String(fields.get("value") || "").trim();
  if (kind === "unlinked") params.set("eval_kind", kind);
  else if (value && EVAL_SELECTORS.includes(selector)) params.set("eval_" + selector, value);
  const query = params.toString(), hash = "#/evals" + (query ? "?" + query : "");
  if (currentHash() === hash) loadEvaluationHistory(query, {refresh:true}); else setHash(hash);
}
function renderEvaluationArms(scenario) {
  let html = `<div class="scroll-x" tabindex="0" role="region" aria-label="Scenario ${scenario.scenario + 1} trial arms"><table class="data eval-arm-table"><thead><tr><th scope="col">Arm</th><th scope="col">Observed passes</th><th scope="col">Graded failures</th><th scope="col">Completed / planned</th><th scope="col">Valid / planned</th></tr></thead><tbody>`;
  for (const arm of ["without", "with"]) {
    const value = scenario.arms[arm];
    html += `<tr><th scope="row">${arm === "with" ? "With the rule" : "Without the rule"}</th><td>${num(value.observed_passes)}</td><td>${num(value.graded_failures)}</td><td>${num(value.completed)} / ${num(value.requested_trials)}</td><td>${num(value.valid_trials)} / ${num(value.requested_trials)}</td></tr>`;
  }
  html += `</tbody></table></div>`;
  for (const arm of ["without", "with"]) {
    const value = scenario.arms[arm];
    html += `<p><strong>${arm === "with" ? "With" : "Without"} the rule:</strong> ${value.skipped ? `skipped · ${esc(value.skipped.reason)}` : `${num(value.attempted)} trials started`}. ` +
      (value.served_models.map(model => `${esc(model.provider)} / ${esc(model.model)}`).join("; ") || "Served model unknown") + `.</p>`;
    if (Object.keys(value.exclusions).length) html += `<p class="footnote">Excluded from comparison: ${esc(Object.entries(value.exclusions).map(([cause, count]) => `${cause}: ${count}`).join(" · "))}</p>`;
  }
  if (scenario.comparison.computable) {
    const withRule = scenario.arms.with, withoutRule = scenario.arms.without;
    html += `<p class="eval-comparison">Comparable paired results: with rule <strong>${num(withRule.valid_passes)} / ${num(withRule.valid_trials)}</strong> passed; without rule <strong>${num(withoutRule.valid_passes)} / ${num(withoutRule.valid_trials)}</strong> passed. ` +
      (Math.min(withRule.valid_trials, withoutRule.valid_trials) < 20 ? "Fewer than 20 valid trials per arm; no percentage delta shown." : `Paired change: ${scenario.comparison.pass_rate_delta > 0 ? "+" : ""}${Math.round(scenario.comparison.pass_rate_delta * 100)} percentage points.`) + ` Counts describe this scenario only.</p>`;
  } else html += `<p class="eval-comparison">Paired change unavailable · ${esc(scenario.comparison.reason)}. Missing results are not zero.</p>`;
  return html;
}
function renderEvaluationCalls(events, prefix) {
  const starts = events.filter(e => e.kind === "call_started"), results = events.filter(e => e.kind === "call_result");
  if (!starts.length) return `<p>No logical call start was retained for this selection.</p>`;
  return starts.map(event => {
    const source = event.data, result = results.find(e => e.data.call.id === source.call_id)?.data;
    const call = result?.call;
    return `<article class="eval-call"><h4>${esc(source.stage)} · ${event.arm ? `${esc(event.arm)} rule · trial ${event.trial_index + 1}` : "scenario generation"}</h4><p class="mono">${esc(source.call_id)}</p>` +
      `<p>Requested: ${esc(source.model_class)}${call?.model_requested ? " / " + esc(call.model_requested) : ""}<br>Served: ${call?.model_reported ? `${esc(call.provider)} / ${esc(call.model_reported)}` : "unknown"}</p>` +
      `<p>Outcome: ${esc(call?.outcome || "No completed result retained")}. Provider invocations: ${result?.provider_attempts == null ? "unknown" : num(result.provider_attempts)}.</p>` +
      (call?.error ? `<p class="delivery-error">${esc(call.error)}</p>` : "") +
      runDisclosure(prefix + ":call:" + source.call_id, "Complete call record and execution inputs", {start:event, result:result || null}) + `</article>`;
  }).join("");
}
export function renderEvaluationDetail(entry) {
  const back = "#/evals" + (entry.query ? "?" + entry.query : "");
  let html = `<nav class="run-breadcrumb" aria-label="Breadcrumb"><a href="${esc(back)}">Evals &amp; trends</a> / Evaluation detail</nav>`;
  if (entry.loading) return html + `<p role="status">Reading retained evaluation evidence…</p>`;
  if (entry.error) return html + `<p class="error-state" role="alert">${esc(entry.error)}</p><button id="evaluation-detail-retry" class="btn" data-evaluation-detail-refresh="true">Retry evaluation</button>`;
  const data = entry.data, summary = data.summary;
  html += `<header class="view__head"><div><h1 id="evaluation-title" class="view__title" tabindex="-1">${entry.kind === "unlinked" ? "Historical evaluation" : "Evaluation attempt"}</h1><p class="mono">${esc(summary.id)}</p><p>${esc(summary.created_at || "Time not recorded")} · ${esc(summary.state)} · recorded verdict: ${esc(summary.verdict || "none")}</p></div><button id="evaluation-detail-refresh" class="btn" data-evaluation-detail-refresh="true">Refresh evidence</button></header>` + evaluationNotice(summary.outcome);
  if (entry.kind === "unlinked") {
    if (data.attempt_links?.length) html += projectPanel("Exact attempt records are available", data.attempt_links.map(link => `<p><a href="${esc(evaluationHref("attempt", link.attempt_id, entry.query))}">Inspect scenario ${link.scenario + 1} in its retained attempt</a></p>`).join(""));
    html += projectPanel("Retained historical result", `<p>Subject ID: <span class="mono">${esc(summary.subject_id)}</span>. This does not identify an exact rule revision.</p><p>Reported trial outcomes: ${num(summary.story.succeeded)} passed / ${num(summary.story.attempted)} attempted; ${num(summary.story.failed)} failed. Infrastructure failures may be included in those historical counts.</p><p>Comparison validity, exact source and producing model calls are unknown. No paired change is calculated.</p>` + runDisclosure("legacy-eval:" + summary.id, "Complete historical arms, metrics and failure taxonomy", data.record));
    return html;
  }
  const source = data.source;
  html += projectPanel("Rule and source at evaluation time", `<p class="eval-rule">${esc(source.learning.rule_text)}</p><p>Proposal state when frozen: <strong>${esc(source.proposal.status)}</strong>. Later decisions and delivered files are separate.</p><p class="eval-links"><a href="#/rules/${encodeURIComponent(source.learning_id)}?tab=why">Inspect rule</a><a href="#/review/proposal/${encodeURIComponent(source.proposal_id)}">Current proposal in Review</a><a href="#/review/eval/${encodeURIComponent(source.proposal_id)}">Preview regeneration cost…</a>` +
    (source.run_id ? `<a href="${esc(runHref("run", source.run_id))}">Producing run</a>` : "") + `</p><p class="caption">${num(summary.completed_scenarios)} of ${num(summary.scenarios)} scenario results; ${num(summary.calls_recorded)} results / ${num(summary.calls_started)} logical call starts.</p>` +
    runDisclosure("evaluation-source:" + source.id, "Complete frozen source, settings and revision identities", source));
  data.scenarios.forEach(scenario => {
    const index = scenario.scenario, events = data.events.filter(e => e.scenario_index === index);
    const key = source.id + ":" + index;
    let body = `<p>Recorded scenario verdict: <strong>${esc(scenario.evaluation?.verdict || "No result")}</strong>.</p>`;
    if (!scenario.specification) body += `<p>No complete specification was retained. Generation starts and failures remain below.</p>`;
    body += renderEvaluationArms(scenario);
    if (scenario.specification) body += runDisclosure("evaluation-spec:" + key, "Complete specification and content hash", scenario.specification);
    const generation = events.filter(e => ["generation_started", "generation_prompt", "generation_failed"].includes(e.kind));
    body += runDisclosure("evaluation-generation:" + key, "Generation prompt, input evidence and failures", generation);
    body += `<details ${reviewDisclosure("evaluation-trials:" + key)}><summary>Every trial outcome and interruption</summary>` + events.filter(e => ["trial_started", "trial_result", "trial_failed"].includes(e.kind)).map(e => `<article class="eval-call"><h4>${esc(e.arm)} rule · trial ${e.trial_index + 1} · ${esc(e.kind.replace("trial_", ""))}</h4>${fullRecord(e)}</article>`).join("") + `</details>`;
    body += `<details ${reviewDisclosure("evaluation-calls:" + key)}><summary>Model calls for this scenario (${events.filter(e => e.kind === "call_started").length})</summary>${renderEvaluationCalls(events, key)}</details>`;
    html += projectPanel(`Scenario ${index + 1}${scenario.specification ? " · " + scenario.specification.spec.title : ""}`, body);
  });
  html += projectPanel("Attempt outcome and retained stops", runDisclosure("evaluation-result:" + source.id, "Complete gate tally and stop history", {result:data.result, stops:data.events.filter(e => e.kind === "attempt_stopped")}));
  return html + projectPanel("Complete chronological evidence", `<p>Events can share timestamps. Their scenario, arm, trial and call IDs establish their relationships.</p>` + runDisclosure("evaluation-events:" + source.id, "All retained events without truncation", data.events));
}
export function paintEvaluationDetail() {
  if (state.route === "evals" && state.evaluationDetail) preserveOperationView("evaluation-detail", () => renderEvaluationDetail(state.evaluationDetail));
}
export async function openEvaluationDetail(kind, id, query = "", {refresh = false} = {}) {
  if (!refresh && state.evaluationDetail?.id === id && state.evaluationDetail.kind === kind && state.evaluationDetail.query === query) {paintEvaluationDetail(); return;}
  const entry = {kind, id, query, loading:true, data:null, error:""}; state.evaluationDetail = entry; paintEvaluationDetail();
  try {
    if (!["attempt", "unlinked"].includes(kind) || !id) throw new Error("Unknown evaluation detail address.");
    entry.data = await getJSON((kind === "attempt" ? API.evalAttempts : API.evalResults) + "/" + encodeURIComponent(id));
    if (entry.data.summary.id !== id) throw new Error("The response identifies a different evaluation.");
  } catch(error) {entry.error = String(error.message || error);}
  finally {
    entry.loading = false;
    if (state.evaluationDetail === entry) {
      paintEvaluationDetail();
      if (state.route === "evals") doc().getElementById(refresh ? "evaluation-detail-refresh" : "evaluation-title")?.focus?.({preventScroll:refresh});
    }
  }
}

/* Monthly observations are fetched on demand. The URL retains the exact filters. */
function trendNumber(value) {
  return value == null ? EM_DASH : value > 0 && value < 0.01 ? "<0.01" : Number(value.toFixed(2)).toLocaleString("en-US", {maximumFractionDigits: 2});
}

function trendPointDescription(data, point, signal) {
  const label = signal == null ? "all kinds" : signal.replaceAll("_", " ");
  const value = signal == null ? {count:point.occurrences, rate_per_100k:point.rate_per_100k} : point.signals[signal];
  const missing = point.reason === "unknown_version" ? "Unknown detector version; no rate." : point.reason === "insufficient_sessions" ? "Insufficient sessions; trend rate unavailable." : "No eligible exposure; no rate.";
  // String retains the API number's precision. The count/line pair is the exact
  // rational source; compact chart labels must not replace it in inspection.
  return `${point.month}: ${label} · ${num(value.count)} occurrences / ${point.eligible_lines} eligible physical lines · ${point.sessions} known sessions. ${value.rate_per_100k == null ? missing : `${String(value.rate_per_100k)} per 100,000 lines.`}${point.small_sample ? ` Fewer than ${data.min_sessions} sessions; small sample.` : ""}${point.partial ? " Partial month." : ""}`;
}

function trendMonthEvidence(data, month) {
  const versions = (data.version_groups || []).flatMap(v => {
    const observed = (v.months || []).find(m => m.month === month && m.eligible_lines > 0);
    return observed ? [{...v, eligible_lines:observed.eligible_lines}] : [];
  });
  const dates = Object.entries(data.deliveries?.by_day || {}).filter(([date]) => date.slice(0,7) === month).sort(([a],[b])=>a.localeCompare(b));
  return {versions, dates};
}

export function renderTrendContext(data) {
  const months = [...new Set([...(data.series || []).map(p=>p.month),
    ...(data.version_groups || []).flatMap(v=>(v.months || []).map(m=>m.month)),
    ...Object.keys(data.deliveries?.by_day || {}).map(day=>day.slice(0,7))])].sort();
  if (!months.length) return "";
  return `<section class="trend-context" aria-label="Monthly coverage and delivery context"><p class="caption">Bands mark months with observed eligible lines, not releases or continuous coverage. Rescans can overlap; their lines are never pooled. ▲ marks retained revision dates in UTC, not instruction loading or benefit.</p>` + months.map(month=>{
    const {versions,dates} = trendMonthEvidence(data,month);
    const point = (data.series || []).find(p=>p.month===month);
    return `<details id="trend-context-${esc(month)}" ${reviewDisclosure("trend-context:"+month)}><summary id="trend-context-toggle-${esc(month)}">${esc(month)} · rates, coverage and delivery dates</summary>` +
      `<p>${point?.partial ? "Partial month. " : ""}Selected trend window: ${esc(data.requested?.start || EM_DASH)} to ${esc(data.requested?.end || EM_DASH)} (exclusive).</p>` +
      (point ? `<div class="trend-exact-rates"><p><strong>Exact monthly rates</strong> · selected configuration <code>${esc(data.compatibility_key || EM_DASH)}</code>. Values retain the reader's numeric precision; counts and lines give the exact ratio.</p>` + [null,...data.signal_types].map(signal=>`<p>${esc(trendPointDescription(data,point,signal))}</p>`).join("") + `</div>` : `<p>No primary rates retained for this month under the current selection.</p>`) +
      (versions.length ? versions.map(v=>`<p><code>${esc(v.compatibility_key)}</code> · ${v.compatibility_key===data.compatibility_key ? "selected · " : ""}${num(v.eligible_lines)} eligible physical lines · ${v.identifiable ? "known configuration" : "unknown configuration; no rate"}</p>`).join("") : `<p>No eligible timed lines observed in this month. Detector absence is unknown.</p>`) +
      `<p>Unknown-time and skipped coverage stays in the coverage exclusions below; it is not assigned to this month.</p>` +
      (data.deliveries?.count == null ? `<p>Delivery history unavailable · ${esc(data.deliveries?.reason || "unknown coverage")}</p>` : dates.length ? `<ul class="trend-dates">${dates.map(([day,count])=>`<li><time datetime="${esc(day)}">${esc(day)}</time> UTC · ${num(count)} retained revisions</li>`).join("")}</ul>` : `<p>0 retained revisions in this month within the read interval. Older unretained applications remain unknown.</p>`) +
      `<p class="caption">These dates cover the complete retained interval selection. The separate record list below shows one page at a time. Revisions are not counts of distinct rules or successful writes.</p></details>`;
  }).join("") + `</section>`;
}

export function renderTrendMatrix(data) {
  const points = data.series || [];
  if (!points.length) return emptyState("Trend unavailable", data.reason_text, "");
  let html = `<div class="scroll-x" tabindex="0" role="region" aria-label="Monthly signal measurements"><table class="matrix trend-matrix"><caption class="visually-hidden">Signals per 100,000 eligible physical lines. Each row scales to its own peak.</caption><thead><tr><th scope="col">Signal / workload</th>` +
    points.map(p => `<th scope="col" aria-label="${esc(p.month)}${p.partial ? " · partial month" : ""}"><abbr title="${esc(p.month)}">${["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"][Number(p.month.slice(5,7))-1]}</abbr>${p.partial ? `<br><span class="caption">Partial</span>` : ""}</th>`).join("") + `<th scope="col" class="trend-peak-heading">Peak</th></tr>`;
  html += `<tr class="trend-bands"><th scope="row">Observed configs</th>` + points.map(p=>{
    const {versions} = trendMonthEvidence(data,p.month);
    const selected = versions.some(v=>v.compatibility_key===data.compatibility_key);
    const label = selected ? "chosen"+(versions.length>1 ? " +"+(versions.length-1) : "") : versions.length+" config"+(versions.length===1 ? "" : "s");
    return `<td data-selected="${selected}" data-observed="${versions.length > 0}">` + (versions.length ? `<button type="button" id="trend-band-${esc(p.month)}" data-trend-context="${esc(p.month)}" aria-controls="trend-context-${esc(p.month)}" aria-label="${esc(label)}. ${esc(p.month)}: ${versions.length} observed configurations${selected ? ', including selected' : ''}. Inspect monthly context.">${esc(label)}</button>` : `<span aria-label="${esc(p.month)}: no eligible timed coverage observed">—</span>`) + `</td>`;
  }).join("") + `<td></td></tr>`;
  html += `<tr class="trend-markers"><th scope="row">Delivery dates · UTC</th>` + points.map(p=>{
    const {dates} = trendMonthEvidence(data,p.month);
    const total = dates.reduce((n,[,count])=>n+count,0);
    const label = "▲ " + (dates.length===1 ? dates[0][0].slice(5) : num(dates.length)+" dates");
    return `<td>` + (dates.length ? `<button type="button" id="trend-marker-${esc(p.month)}" data-trend-context="${esc(p.month)}" aria-controls="trend-context-${esc(p.month)}" aria-label="${esc(label)}. ${esc(p.month)}: ${total} retained revisions on ${dates.length} UTC dates. Inspect all dates.">${esc(label)}</button>` : `<span aria-label="${esc(p.month)}: ${data.deliveries?.count == null ? 'delivery history unavailable' : 'zero retained revisions'}">${data.deliveries?.count == null ? EM_DASH : '0'}</span>`) + `</td>`;
  }).join("") + `<td></td></tr></thead><tbody>`;
  for (const signal of [null, ...data.signal_types]) {
    const label = signal == null ? "all kinds" : signal.replaceAll("_", " ");
    const valueOf = p => signal == null ? {count:p.occurrences, rate_per_100k:p.rate_per_100k} : p.signals[signal];
    const available = points.map(p=>valueOf(p).rate_per_100k).filter(rate=>rate!=null);
    const peak = available.length ? Math.max(...available) : null;
    html += `<tr${signal == null ? ' class="trend-aggregate"' : ''}><th scope="row" class="matrix__row-label">${esc(label)}</th>`;
    html += points.map(p => {
      const value = valueOf(p), rate = value.rate_per_100k;
      const explanation = trendPointDescription(data,p,signal);
      return `<td id="trend-value-${esc(signal || "all")}-${esc(p.month)}" tabindex="-1" title="${esc(explanation)}" aria-label="${esc(explanation)}" data-trend-month="${esc(p.month)}" data-partial="${p.partial}" data-small="${p.small_sample}">` +
        `<button type="button" class="trend-point" id="trend-point-${esc(signal || "all")}-${esc(p.month)}" data-trend-context="${esc(p.month)}" aria-controls="trend-context-${esc(p.month)}" aria-label="${esc(trendNumber(rate))}. ${esc(explanation)} Inspect exact monthly rates."><span class="trend-value"${rate > 0 ? " hidden" : ""}>${esc(trendNumber(rate))}</span>` +
        (rate == null ? `<span class="spark trend-gap" aria-hidden="true"></span>` : `<span class="spark" aria-hidden="true"><span class="spark__bar" data-zero="${rate === 0}" style="--v:${peak ? 100 * rate / peak : 0}"></span></span>`) + `</button></td>`;
    }).join("") + `<td class="trend-peak" id="trend-peak-${esc(signal || "all")}" tabindex="0" aria-label="${esc(label)}: ${peak == null ? 'peak unavailable; no primary rates' : esc(trendNumber(peak))+' per 100,000 eligible physical lines; highest available primary rate in this window'}">${esc(trendNumber(peak))}</td></tr>`;
  }
  for (const [label, field] of [["Eligible lines", p => p.eligible_lines], ["Known sessions", p => p.sessions], ["Median lines / session", p => p.session_size.median], ["Largest session, lines", p => p.session_size.max], ["Unknown-session lines", p => p.unknown_session_lines], ["Delivered revisions", p => p.deliveries]]) {
    html += `<tr class="trend-workload"><th scope="row">${label}</th>` + points.map(p => `<td data-trend-month="${esc(p.month)}">${esc(trendNumber(field(p)))}</td>`).join("") + `<td aria-label="Peak does not apply to workload">—</td></tr>`;
  }
  return html + `</tbody></table></div><p class="footnote">Select a monthly bar, zero or gap to inspect exact rates and counts. Each signal row scales to its own peak; peak labels are rounded. All kinds counts signal occurrences, not distinct mistakes. Shaded cells have fewer than ${num(data.min_sessions)} known sessions; their trend rates are unavailable. Raw observations remain inspectable below. Dashed columns mark an incomplete month. No month is dropped for a small sample. The session threshold does not establish representativeness or causality.</p>`;
}

export function renderEvaluationHealth(entry = {}) {
  if (entry.error) return `<p role="alert">${esc(entry.error)}</p><button class="btn" data-evaluation-health-refresh="true">Retry gate evidence</button>`;
  if (!entry.data) return `<p role="status">Reading retained gate evidence…</p>`;
  const data = entry.data;
  const rows = data.by_outcome;
  const coherent = data.computable === true && Number.isSafeInteger(data.attempts) && data.attempts >= 0 &&
    rows.every(row => Number.isSafeInteger(row.count) && row.count >= 0 && typeof row.code === "string" && row.code) &&
    new Set(rows.map(row => row.code)).size === rows.length && rows.reduce((total,row) => total+row.count,0) === data.attempts;
  const distribution = coherent && data.attempts > 0;
  const tone = row => ["ok","error","partial","info"].includes(row.tone) ? row.tone : "info";
  const swatch = (row,i) => `data-tone="${tone(row)}" data-pattern="${i % 4}"`;
  const strip = distribution ? `<div class="outcome-strip" aria-hidden="true">` + rows.map((row,i) =>
    `<span data-outcome="${esc(row.code)}" data-key="${i+1}" title="${esc((i+1)+'. '+row.label+': '+num(row.count))}" ${swatch(row,i)} style="--share:${100*row.count/data.attempts}"></span>`).join("") + `</div>` :
    `<p class="caption">${coherent && data.attempts === 0 ? "No recorded attempts to distribute." : "Distribution unavailable; the total and outcome counts must be known and consistent."}</p>`;
  return `<p>${data.attempts == null ? "Recorded attempt count unknown" : `${num(data.attempts)} recorded evaluation attempts`}.</p>` +
    strip + (distribution ? `<p class="caption outcome-order">Outcomes, in left-to-right order:</p>` : "") + `<ol class="outcome-legend">` + rows.map((row,i) => `<li data-key="${i+1}"><span class="outcome-key" aria-hidden="true" ${swatch(row,i)}></span><span>${i+1}. ${esc(row.label)}</span><strong>${num(row.count)}${distribution ? ` <span class="caption">(${esc(trendNumber(100*row.count/data.attempts))}%)</span>` : ""}</strong></li>`).join("") + `</ol>` +
    `<p>${num(data.unlinked_results)} historical result rows without an attempt link; comparison validity unknown.</p><p class="caption">${esc(data.reason)}</p>`;
}
export async function loadEvaluationHealth({refresh = false} = {}) {
  if (!refresh && state.evaluationHealth && (state.evaluationHealth.loading || state.evaluationHealth.data)) return;
  const entry = {loading:true, data:null, error:""}; state.evaluationHealth = entry;
  const paint = () => {if (state.route === "evals" && !state.evaluationDetail && !state.qualityDetail && doc().getElementById("trend-gate")) preserveOperationView("trend-gate", () => renderEvaluationHealth(state.evaluationHealth));};
  paint();
  try {entry.data = await getJSON(API.evalHealth);} catch(error) {entry.error = String(error.message || error);}
  finally {entry.loading = false; if (state.evaluationHealth === entry) paint();}
}

export function renderTrendControls(entry = {}) {
  if (!entry.data) return `<p class="caption">${entry.loading ? "Reading monthly selection…" : entry.error ? "Monthly selection unavailable; retry below." : "Monthly selection not loaded."}</p>`;
  const data = entry.data;
  const requested = data.requested;
  const select = (name, label, first, values, selected) => `<label>${label}<select name="${name}" id="trend-${name}"><option value="">${first}</option>` + values.map(([value, title]) => `<option value="${esc(value)}"${value === selected ? " selected" : ""}>${esc(title)}</option>`).join("") + `</select></label>`;
  const projects = data.project_options.map(key => [key, key]);
  if (requested.project_key && !projects.some(([key]) => key === requested.project_key)) projects.push([requested.project_key, requested.project_key]);
  const versions = data.version_groups.map(v => [v.compatibility_key, `${v.identifiable ? "Known" : "Unknown"} · ${v.compatibility_key.slice(0, 20)}`]);
  if (requested.compatibility_key && !versions.some(([key]) => key === requested.compatibility_key)) versions.push([requested.compatibility_key, "Uncovered · " + requested.compatibility_key]);
  let html = `<form id="trends-filter" class="exposure-form trend-form">` +
    select("project_key", "Project", "All observed projects", projects, requested.project_key) +
    select("compatibility_key", "Detector / parser configuration", "Select automatically only if unique", versions, requested.compatibility_key) +
    `<label>Last month (UTC)<input type="month" name="end_month" id="trend-end_month" required max="${esc(data.partial_month)}" value="${esc(requested.end_month)}"></label>` +
    `<button id="trend-submit" class="btn" type="submit">Show seven months</button></form>`;
  const label = `${requested.project_key || "All observed projects"} · through ${requested.end_month} · ` +
    (data.compatibility_key ? `${data.version_groups.length} detector version(s), selected ${data.compatibility_key.slice(0,12)}` : data.version_groups.length ? "Choose detector configuration" : "Detector coverage unavailable");
  return `<details class="trend-options" ${reviewDisclosure("trend-options", data.reason === "incompatible_versions")}><summary id="trend-options-toggle">${esc(label)}</summary>${html}</details>`;
}

export function renderTrends(entry) {
  const recurrencePanel=projectPanel("Observed rule recurrence",renderRecurrence(recurrenceProject(),"evals"));
  const historyPanel = projectPanel("Every eval so far", `<div id="evaluation-history-body">${renderEvaluationHistory(state.evaluationHistory || {})}</div>`);
  if (entry.loading) return `<p role="status">Reading monthly observations…</p>` + historyPanel + recurrencePanel;
  if (entry.error) return `<p class="error-state" role="alert">${esc(entry.error)}</p><button class="btn" data-trend-refresh="true">Retry monthly observations</button><p><a href="#/evals">Reset trend filters</a></p>` + historyPanel + recurrencePanel;
  const data = entry.data;
  if (!data) return `<p>Monthly observations have not been loaded.</p>`;
  let chart = (data.reason_text ? `<p class="footnote">${esc(data.reason_text)}</p>` : "") +
    ((data.coverage_by_project || []).some(p => p.coverage?.coverage_complete === false) ? `<p class="footnote">Incomplete scan coverage in ${num(data.coverage_by_project.filter(p => p.coverage?.coverage_complete === false).length)} project scope(s). Inspect exclusions before comparing these observed subsets.</p>` : "") +
    renderTrendMatrix(data) + renderTrendContext(data);
  chart += `<p class="trend-caution"><strong>Observed trends do not establish improvement.</strong> Inspect coverage and session sizes before comparing.</p>`;
  chart += `<details ${reviewDisclosure("trend-arithmetic")}><summary id="trend-arithmetic-toggle">Inspect observed arithmetic</summary><p>Raw counts divided by eligible physical lines describe the retained observations. This arithmetic does not make a small sample adequate. Lines and session sizes are imperfect workload proxies; they do not measure task difficulty.</p>`;
  chart += data.series.map(p=>`<article class="project-record"><p><strong>${esc(p.month)}</strong> · ${num(p.sessions)} known sessions · ${num(p.eligible_lines)} eligible lines · ${num(p.unknown_session_lines)} lines with unknown session identity${p.reason?" · "+esc(p.reason.replaceAll("_"," ")):""}</p><p>`+
    data.signal_types.map(signal=>{const value=p.signals[signal];return `${esc(signal.replaceAll("_"," "))}: ${num(value.count)} occurrences · ${value.observed_rate_per_100k==null?"arithmetic unavailable":esc(trendNumber(value.observed_rate_per_100k))+" observed per 100,000 lines"}`;}).join("<br>")+`</p></article>`).join("")+`</details>`;
  chart += `<details><summary>Detector coverage by month (${data.version_groups.length} versions)</summary><p class="caption">A version marks observed coverage, not its release date. The same transcript can have observations under several versions. Selecting one never joins their lines or rates.</p>`;
  chart += data.version_groups.map(v => `<div class="trend-version"><p class="mono">${esc(v.compatibility_key)}${v.compatibility_key === data.compatibility_key ? " · selected" : ""}</p><p>${v.months.map(m => `${esc(m.month)}: ${num(m.eligible_lines)} lines`).join(" · ") || "No eligible lines in these months"}</p>` + runDisclosure("trend-version:" + v.compatibility_key, "Recorded detector, parser and settings", v.manifests) + `</div>`).join("") + `</details>`;
  chart += runDisclosure("trend-coverage", "Coverage exclusions and workload details", {denominator_note:data.denominator_note, retention_note:data.retention_note, coverage_by_project: data.coverage_by_project, unassigned_lines: data.unassigned_lines, months: data.series});
  let html = `<div class="project-columns"><div class="project-primary">` + projectPanel("Signals per 100k eligible physical lines", chart) + `</div><aside class="project-secondary">` +
    projectPanel("Gate health", `<div id="trend-gate">${renderEvaluationHealth(state.evaluationHealth || {})}</div>`) +
    historyPanel +
    projectPanel("What the counts can show", `<p>These are deterministic signal occurrences. Several signals can refer to one mistake.</p><p>Workload rows describe only the part of each session observed within that month. Compare detector versions, sources and session sizes before interpreting a change.</p><p>Delivery markers show historical revisions. They do not prove when a working copy loaded the instruction.</p>`) + `</aside></div>`;
  const deliveries = data.deliveries;
  let deliveryBody = `<p>${esc(deliveries.note)}</p><p id="trend-delivery-status" tabindex="-1" aria-live="polite">${num(deliveries.records.length)} shown · ${deliveries.count == null ? "history unavailable" : `${num(deliveries.count)} retained revisions in this interval`}</p>`;
  deliveryBody += deliveries.records.map(r => `<article class="project-record"><p><time>${esc(r.applied_at)}</time> · ${esc(r.project_key || "Global instruction")}</p><p><a href="#/rules/${encodeURIComponent(r.learning_id)}?tab=why">Inspect rule ${esc(r.learning_id)}</a></p>` + runDisclosure("trend-delivery:" + r.id, "Exact application and revision identity", r) + `</article>`).join("");
  if (entry.params?.includes("delivery_cursor=")) deliveryBody += `<button id="trend-deliveries-first" class="btn" data-trend-deliveries-first="true">Newest deliveries</button> `;
  if (deliveries.next_cursor) deliveryBody += `<button id="trend-deliveries-next" class="btn" data-trend-deliveries-next="true">Older deliveries</button>`;
  return html + projectPanel("Recorded delivery dates", deliveryBody) + recurrencePanel;
}

export function paintTrends() {
  if (state.route === "evals" && !state.evaluationDetail && !state.qualityDetail) {
    preserveOperationView("trend-controls", () => renderTrendControls(state.trends || {}));
    preserveOperationView("trends-body", () => renderTrends(state.trends || {}));
  }
}

export async function loadTrends(params = "", {refresh = false} = {}) {
  const previous = state.trends;
  if (!refresh && previous?.params === params && (previous.loading || previous.data)) {paintTrends(); return;}
  const activeId = doc()?.activeElement?.id || "";
  const entry = {params, loading: true, data: null, error: "", focus: activeId.startsWith("trend-") ? activeId : ""};
  state.trends = entry; paintTrends();
  try {
    entry.data = await getJSON("/api/incident-rate" + (params ? "?" + params : ""));
  } catch (error) {entry.error = String(error.message || error);}
  finally {
    entry.loading = false;
    if (state.trends === entry) {
      paintTrends();
      if (state.route === "evals" && entry.focus) {
        const focus = doc().getElementById(entry.focus.startsWith("trend-deliveries-") ? "trend-delivery-status" : entry.focus);
        if (focus?.focus) {
          focus.focus({preventScroll:true});
          if (entry.focus.startsWith("trend-deliveries-") && focus.scrollIntoView) focus.scrollIntoView({block:"nearest"});
        }
      }
    }
  }
}

export function submitTrends(event) {
  if (event.target.id !== "trends-filter") return;
  event.preventDefault();
  const params = evalQueryParts(state.evaluationQuery).history;
  for (const [key, value] of new FormData(event.target)) if (value) params.set(key, String(value));
  const query = params.toString();
  const hash = "#/evals" + (query ? "?" + query : "");
  if (currentHash() === hash) loadTrends(query, {refresh:true});
  else setHash(hash);
}


/* ------------------------------ loading ---------------------------------- */

function paintGlobalErrors() {
  const element = byId("global-error"), document = doc(), active = document?.activeElement;
  const hadFocus = element.contains?.(active), focusedId = active?.id;
  const focusedScroll = hadFocus ? [active.scrollTop, active.scrollLeft] : null;
  for (const disclosure of element.querySelectorAll?.("details[data-error-owner]") || []) {
    const entry = state.dashboardErrors.get(disclosure.getAttribute("data-error-owner"));
    if (entry) entry.open = disclosure.open;
  }
  element.innerHTML = Array.from(state.dashboardErrors, ([owner, entry]) => {
    const id = encodeURIComponent(owner);
    return `<section data-error-owner="${esc(owner)}"><strong>${esc(entry.title)}</strong>` +
      `<p class="error-state__detail">${esc(entry.detail)}</p>` +
      (entry.diagnostic ? `<details data-error-owner="${esc(owner)}"${entry.open ? " open" : ""}><summary id="global-error-details-${esc(id)}">${esc(entry.diagnosticLabel || "Decision details")}</summary><pre id="global-error-diagnostic-${esc(id)}" class="review-full-record" tabindex="0" role="region" aria-label="${esc(entry.diagnosticRegion || "Complete decision diagnostic")}">${esc(entry.diagnostic)}</pre></details>` : "") +
      (entry.retry ? `<p class="error-state__detail">${esc(entry.recovery || "Some data could not be refreshed. Previously shown data may be out of date.")}</p>` +
        `<button id="global-error-retry-${esc(id)}" class="btn" type="button" data-dashboard-retry="true" aria-disabled="${state.dashboardRetrying}">${state.dashboardRetrying ? "Retrying reads…" : "Retry dashboard reads"}</button>`
        : `<p class="error-state__detail">${esc(entry.recovery || "Reload the dashboard to request current data.")}</p>`) + `</section>`;
  }).join("");
  setVisible(element, state.dashboardErrors.size > 0);
  if (hadFocus) {
    const replacement = state.dashboardErrors.size && focusedId && document.getElementById(focusedId);
    (replacement || byId("main")).focus({preventScroll:true});
    if (replacement) { replacement.scrollTop = focusedScroll[0]; replacement.scrollLeft = focusedScroll[1]; }
  }
}

/** A result may retire its own observed error, never a newer or unrelated one. */
export function showError(title, detail, {owner = "general", ...options} = {}) {
  const entry = {title, detail, ...options};
  state.dashboardErrors.set(owner, entry);
  paintGlobalErrors();
  return entry;
}

export function clearError(owner = "general", expected) {
  if (arguments.length > 1 && state.dashboardErrors.get(owner) !== expected) return false;
  const removed = state.dashboardErrors.delete(owner);
  paintGlobalErrors();
  return removed;
}

export async function retryDashboardReads() {
  if (state.dashboardRetrying) return;
  state.dashboardRetrying = true;
  paintGlobalErrors();
  try {
    return await load();
  } catch (error) {
    showError("Dashboard reads could not be retried", String(error.message || error), {owner:"dashboard-read",retry:true});
  } finally {
    state.dashboardRetrying = false;
    paintGlobalErrors();
  }
}

export function paintReview() {
  const document = doc();
  const active = document && document.activeElement;
  const activeId = active && active.id;
  const focusedCard = active?.closest?.(".review-card--selected");
  const focusedFamily = focusedCard?.getAttribute("data-learning-id");
  const focusedDisclosure = active?.tagName === "SUMMARY" ? active.parentElement?.getAttribute("data-review-key") : null;
  const focusedAction = active?.getAttribute?.("data-decision");
  const focusedIndividual = active?.getAttribute?.("data-review-individual");
  const focusedEmpty = active?.hasAttribute?.("data-review-empty");
  const body = byId('review-body');
  if (body.querySelectorAll) body.querySelectorAll('details[data-review-key]').forEach((el) => {
    state.reviewDisclosures[el.getAttribute('data-review-key')] = el.open;
  });
  const data = state.review;
  let reveal = false;
  const requested=state.route==="review" ? state.reviewRouteProposal : "";
  const detailFamily = state.reviewDetailFamily;
  const detail = detailFamily && data?.families?.find(f=>f.learning_id===detailFamily);
  const missingDetail = Boolean(detailFamily && data && !detail);
  if (detailFamily) state.selectedFamily = detail ? detailFamily : "";
  const excluded=Boolean(data && requested && !(data.families || []).some(f=>(f.proposals || []).some(p=>p.id===requested)));
  setVisible(byId("review-selected-proposal"),excluded);
  if(excluded)loadExcludedProposal(requested);
  const linked=data && (data.families || []).find(f=>(f.proposals || []).some(p=>p.id===state.reviewFocusProposal));
  if (linked) {
    state.selectedFamily=linked.learning_id;state.reviewIndividual[linked.learning_id]=true;
    linked.proposals.forEach(p=>{state.reviewExcluded[p.id]=p.id!==state.reviewFocusProposal;});
    state.reviewFocusProposal="";reveal=true;
  }

  // Keep the selected identifier aligned with the card rendered on screen.
  // Otherwise the first navigation key can reselect the already open card.
  if (!excluded && !missingDetail && data && data.families && data.families.length) {
    const ids = data.families.map((family) => family.learning_id);
    if (ids.indexOf(state.selectedFamily) === -1) {
      state.selectedFamily = ids[0];
    }
    if (state.route === "review") {
      const family = data.families.find((f) => f.learning_id === state.selectedFamily);
      const cached = state.reviewPreviews[family.learning_id];
      if (!cached || cached.signature !== reviewSignature(selectedReviewProposals(family))) loadReviewPreview(family);
    }
  }
  setHTML("review-body", detailFamily ? (detail ? renderReviewDetail(detail) : `<div data-review-empty="true" tabindex="-1"><h1 class="view__title">${data ? "This family is no longer in Review" : "Reading Review…"}</h1><p>${data ? "No decisions are offered for a missing or completed family." : "Waiting for the current queue."}</p><a href="#/review">Return to queue</a></div>`) : excluded ? `<p>The selected proposal is outside the current Review queue. Inspect its exact record below, or <a href="#/review">open the queue</a>.</p>` : renderReviewQueue(data, { selectedId: state.selectedFamily, decided: state.decided }));
  if (detail) {for (const member of reviewDetailMembers(detail)) {const id=member.snapshot.evaluation?.id;if(id)loadReviewEvaluation(id);}}
  setText("review-note", renderReviewNote(data));
  const count = data && typeof data.count === "number" ? num(data.count) : "\u2014";
  setText("review-count", count);
  if (data) {paintInboxCount(data);if(state.overview)paintOverview();}
  if (state.route === "review" && !excluded) {
    // Every preview and queue refresh replaces this subtree. Restore only
    // focus that was inside it at repaint time, never focus moved elsewhere
    // while a command was pending. A removed family advances to the next card.
    let replacement = null;
    if (focusedCard && focusedFamily === state.selectedFamily) {
      if (activeId) replacement = document.getElementById(activeId);
      else if (focusedDisclosure) replacement = Array.from(body.querySelectorAll("details[data-review-key]")).find(el=>el.getAttribute("data-review-key")===focusedDisclosure)?.querySelector("summary");
      else if (focusedAction) replacement = Array.from(body.querySelectorAll("[data-decision]")).find(el => el.getAttribute("data-decision") === focusedAction);
      else if (focusedIndividual) replacement = body.querySelector("[data-review-individual]");
    }
    if (replacement && !replacement.disabled) replacement.focus({preventScroll:true});
    else if (activeId?.startsWith("review-evaluation-retry-")) document.getElementById(activeId.replace("review-evaluation-retry-","review-evaluation-"))?.focus({preventScroll:true});
    else if (activeId?.startsWith("review-evidence-")) document.getElementById("review-evidence-page")?.focus({preventScroll:true});
    else if (reveal || focusedCard || focusedEmpty) revealSelectedCard();
  }
}

function paintInboxCount(data) {
  const count = data && typeof data.count === "number" ? data.count : undefined;
  setText("nav-inbox-count", count === undefined ? EM_DASH : num(count));
  byId("nav-inbox-count").setAttribute("data-state", count === undefined ? "unknown" : count > 0 ? "partial" : "ok");
}

/** Shared POST transport for explicit commands and compatibility decisions. */
export async function postJSON(url, payload) {
  state.reviewMutationGeneration++;
  try {
  const response = await fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json", accept: "application/json" },
    body: JSON.stringify(payload),
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(body.detail || `${url} answered ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return body;
  } finally { state.reviewMutationGeneration++; }
}

// All Review readers share ordering and the command boundary, including background polls.
function beginReviewRead() {
  return {read:++state.reviewReadGeneration,mutation:state.reviewMutationGeneration,
    errors:Array.from(state.dashboardErrors).filter(([,entry])=>entry.kind === "review-read")};
}
function reviewReadCurrent(ticket) {
  return ticket.read===state.reviewReadGeneration && ticket.mutation===state.reviewMutationGeneration;
}
function publishReview(ticket,data) {
  if(!reviewReadCurrent(ticket))return false;
  state.review=data;state.decided={};
  for (const [owner,entry] of ticket.errors) clearError(owner,entry);
  // Retained content can be unchanged while its actual destination has changed.
  state.reviewPreviews={};state.reviewMembers={};state.reviewEvaluations={};state.reviewEvidencePages={};return true;
}
export async function refreshReviewSnapshot() {
  const ticket=beginReviewRead();
  try {return publishReview(ticket,await getJSON(API.review));}
  catch(error){if(reviewReadCurrent(ticket))throw error;return false;}
}

export async function postDecision(proposalId, decision) {
  return postJSON(decisionUrl(proposalId), { decision: decision, note: "" });
}

/** Every decision binds one atomic command to the exact reviewed selection. */
export async function decideFamily(learningId, decision) {
  const data = state.review;
  if (!data) return { ok: 0, failed: [] };
  const family = (data.families || []).find((f) => f.learning_id === learningId);
  if (!family) return { ok: 0, failed: [] };
  if (state.deciding[learningId]) return { ok: 0, failed: [], skipped: "in flight" };
  const previousErrors = Array.from(state.dashboardErrors).filter(([,entry])=>entry.kind === "decision" && entry.learningId === learningId);
  state.deciding[learningId] = true;
  paintReview();
  let ok = 0;
  const failed = [];
  let request = null, failure = null, confirmed = null;
  const subjects = selectedReviewProposals(family);
  const action = decision === "reject" ? "reject_target" : decision;
  if (["approve", "reject_target", "reject_lesson"].includes(action)) {
    try {
      const entry = await loadReviewPreview(family);
      if (!reviewPreviewCurrent(family, entry)) throw new Error("The selection or Review queue changed. Review the current preview before deciding.");
      if (!entry.data || entry.error || (action === "approve" && !entry.data.ready)) throw new Error(entry.error || "Resolve the selected edit conflicts before approving.");
      const members = entry.data.members.map((m) => ({ proposal_id: m.proposal_id, revision: m.revision }));
      const signature = JSON.stringify({action, members, preview: action === "approve" ? entry.data.revision : ""});
      request = state.commandRequests[learningId];
      if (!request || request.signature !== signature) {
        request = previousErrors.map(([,entry])=>entry.request).find(prior=>prior?.signature === signature);
      }
      if (!request || request.signature !== signature) {
        request = { signature: signature, key: crypto.randomUUID() };
      }
      state.commandRequests[learningId] = request;
      confirmed = await postJSON(API.commands, {
        request_key: request.key, action, members, ...(action === "approve" ? {preview_revision: entry.data.revision} : {}), note: "",
      });
      state.lastCommand = confirmed;
      for (const [owner,entry] of previousErrors) {
        if (!entry.request || entry.refused || entry.request.key === request.key) clearError(owner,entry);
      }
      // Keep uncertain requests replayable; a confirmed action has ended.
      // Cancellation may return this exact revision for a new approval.
      delete state.commandRequests[learningId];
      members.forEach((m) => { state.decided[m.proposal_id] = action === "approve" ? "approved" : "rejected"; });
      ok = members.length;
    } catch (error) {
      failure = error;
      const message = String(error && error.message ? error.message : error);
      subjects.forEach((p) => failed.push({ id: p.id, message: message }));
    }
  } else {
    subjects.forEach((p) => failed.push({id:p.id, message:"Unknown review decision."}));
  }
  if (failed.length) {
    showError(
      `${failed.length} of ${subjects.length} decision results could not be confirmed`,
      request ? "Inspect decision history and the current preview before retrying." : "Reload Review and inspect the selected preview before deciding.",
      {owner:`review-decision:${learningId}:${request?.key || "preview"}`, kind:"decision", learningId, request,
        refused:Boolean(failure?.status >= 400 && failure.status < 500),
        diagnostic:JSON.stringify({proposal_ids:failed.map(f=>f.id),request_key:request?.key || null,action,
          status:failure?.status || null,error:failed[0].message},null,2),
        recovery:"A retry must use the exact reviewed selection. A different request cannot confirm an earlier lost response."}
    );
  }
  try {
    await refreshReviewSnapshot();
  } catch (error) {
    showError(confirmed ? "Decision recorded; Review could not be refreshed" : "The decision result could not be refreshed",
      String(error && error.message ? error.message : error),
      {owner:`review-refresh:${learningId}`,kind:"review-read",learningId,
        recovery:confirmed ? `Command ${confirmed.id} is ${confirmed.state}. Retry dashboard reads to refresh the queue.` : "Retry dashboard reads to refresh the queue.",retry:true});
  } finally {
    delete state.deciding[learningId];
  }
  if (ok && state.selectedFamily === learningId) {
    const remaining = new Set(reviewOrder());
    if (!remaining.has(learningId)) {
      const previous = (data.families || []).map(f => f.learning_id);
      const index = previous.indexOf(learningId);
      const neighbors = previous.slice(index + 1).concat(previous.slice(0, index).reverse());
      state.selectedFamily = neighbors.find(id => remaining.has(id)) || reviewOrder()[0] || "";
    }
  }
  paintReview();
  if (subjects.length) await loadDeliveryHistory();
  return { ok: ok, failed: failed };
}

/** Shared GET transport. Refuse unsuccessful responses with the request path. */
export async function getJSON(url) {
  const response = await fetch(url, { method: "GET", headers: { accept: "application/json" } });
  if (!response.ok) {
    const detail=response.json ? await response.json().catch(()=>({})) : {};
    const error = new Error(`GET ${url} answered ${response.status} ${detail.error || ""} ${detail.detail || response.statusText || ""}`.trim());
    error.readDetail = [detail.error, detail.detail].filter(value=>typeof value === "string" && value.trim()).join(" · ") || response.statusText || `Request failed (${response.status}).`;
    throw error;
  }
  return response.json();
}

export async function load() {
  const readError = state.dashboardErrors.get("dashboard-read");
  const loadGeneration=++state.overviewLoadGeneration,mutationGeneration=state.reviewMutationGeneration,reviewTicket=beginReviewRead();
  state.overviewLoading=true;state.overviewError="";paintOverviewReadState();
  const projectsGeneration=++state.projectsReadGeneration;
  state.projectsLoading=true;state.projectsError="";paintProjects();
  const wanted = [
    { key: "overview", url: API.overview, paint: paintOverview },
    { key: "rules", url: rulesPageURL(parseRoute(currentHash()).ruleQuery || ""), paint: paintRules },
    { key: "projects", url: API.projects, paint: paintProjects },
    { key: "review", url: API.review, paint: paintReview },
  ];
  const ruleToken={key:wanted.find(entry=>entry.key==="rules").url,loading:true};
  state.rulePage=ruleToken;
  const results = await Promise.all(
    wanted.map(async (entry) => {
      try {
        return { entry, data: await getJSON(entry.url), error: null };
      } catch (error) {
        return { entry, data: null, error };
      }
    })
  );
  const failures = results.filter((r) => r.error);
  results.forEach((result) => {
    if (result.entry.key === "overview" && loadGeneration === state.overviewLoadGeneration) {
      // A decision can invalidate this read without starting another Overview
      // request. Finish its progress but never publish the pre-decision values.
      state.overviewLoading=false;
      state.overviewError=mutationGeneration!==state.reviewMutationGeneration
        ? "Overview may have changed during this read. Refresh for current values."
        : result.error ? "Overview could not be refreshed. Use Refresh to try again." : "";
      paintOverviewReadState();
    }
    if (result.entry.key === "projects") {
      if (projectsGeneration === state.projectsReadGeneration) {
        state.projectsLoading=false;
        state.projectsError=result.error ? String(result.error.message || result.error) : "";
        if (!result.error) state.projects=result.data;
        paintProjects();
      }
      return;
    }
    if(loadGeneration!==state.overviewLoadGeneration || mutationGeneration!==state.reviewMutationGeneration)return;
    if (result.entry.key === "rules") {
      if(state.rulePage!==ruleToken || result.entry.url!==rulesPageURL())return;
      state.ruleDetails={};
      state.rulePage = result.error || result.data?.mode === "paged" ? {key:result.entry.url, data:result.data, error:result.error ? String(result.error.message || result.error) : "", errorDetail:result.error?.readDetail || "", loading:false} : null;
      if (result.error) paintRuleBrowser();
    }
    if (result.error) return;
    if(result.entry.key==="review") {if(!publishReview(reviewTicket,result.data))return;}
    else state[result.entry.key] = result.data;
    result.entry.paint();
    if(result.entry.key==="rules" && state.route==="rules" && state.selectedRule)openRule(state.selectedRule,state.inspectorTab);
  });
  if(loadGeneration!==state.overviewLoadGeneration || mutationGeneration!==state.reviewMutationGeneration)return {loaded:0,failed:0,superseded:true};
  // Projects has an independent retry and error surface; do not leave its old
  // failure in the shared banner after that retry succeeds.
  const sharedFailures = failures.filter(result => result.entry.key !== "projects");
  if (sharedFailures.length) {
    showError(
      `Could not read ${sharedFailures.length} of ${wanted.length} endpoints`,
      sharedFailures.map(f=>{const cause=String(f.error?.readDetail || f.error?.message || f.error);return cause.length>240 ? cause.slice(0,240)+"…" : cause;}).join(" · "),
      {owner:"dashboard-read",retry:true,diagnostic:sharedFailures.map(f=>String(f.error?.message || f.error)).join("\n"),
        diagnosticLabel:"Request details",diagnosticRegion:"Complete request diagnostic"}
    );
  } else {
    clearError("dashboard-read",readError);
  }
  return { loaded: results.length - failures.length, failed: failures.length };
}

/* Human assessment and explicit class consent. No worker is started here. */
const QUALITY_LABELS = {global:"Global rules", project:"Project rules", skill:"Skills", hook:"Hooks"};
function qualityHref(id, query = state.evaluationQuery) { return `#/evals/quality/${id}` + (query ? "?"+query : ""); }
function qualityFraction(q) {
  if (!q) return "Quality evidence unavailable";
  return `${num(q.precision_numerator)} / ${num(q.precision_denominator)} useful` + (q.precision == null ? " · fewer than 20 decided" : ` · ${Math.round(q.precision*100)}%`);
}
export function renderClassEvidence(entry = {}) {
  const readState = (entry.loading ? `<p role="status">Reading class evidence…</p>` : "") +
    (entry.error ? readFailure(entry, {key:"class-evidence", summaryId:"policy-read-error",
      title:entry.data ? "Refresh failed; showing the previous class evidence." : "Could not read class evidence.",
      guidance:"Retry class evidence to request current values."}) : "");
  if (!entry.data) return readState + (entry.error ? `<button class="btn" id="policy-refresh" data-policy-refresh="true">Retry class evidence</button>` : "");
  const data = entry.data;
  const count = value => value == null ? "Unknown" : num(value);
  const insufficient = data.classes.some(row => row.target_class !== "hook" && (!row.quality || row.quality.precision_denominator < 20));
  const notice = insufficient ? `<div class="eval-notice" data-state="partial"><strong><img src="/assets/evidence-dot.svg" width="8" height="8" alt=""> Not enough human evidence for a reliable percentage</strong><p>Review an applied-rule sample. The counts below keep human judgments separate from delivery and evaluations.</p></div>` : "";
  const rows = data.classes.map(row => {
    const cls = row.target_class, q = row.quality, gate = row.evaluations, manual = cls === "hook";
    const availability = row.availability ? `${num(row.availability.available)} observed available · ${num(row.availability.not_observed)} not observed · ${num(row.availability.unknown)} unknown · ${num(row.availability.not_available)} absent/changed` : "Availability history unavailable";
    const open = Boolean(state.operationDisclosures["policy-class:"+cls]);
    const quality = q ? `${count(q.precision_numerator)} / ${count(q.precision_denominator)} useful${q.precision != null && q.precision_denominator >= 20 ? ` · ${Math.round(q.precision*100)}%` : ""}` : "Quality evidence unavailable";
    let html = `<tr id="policy-row-${cls}"><th scope="row"><button id="policy-evidence-${cls}" class="policy-evidence" data-policy-evidence="${cls}" aria-label="${open ? "Collapse" : "Expand"} ${QUALITY_LABELS[cls].toLowerCase()} evidence" aria-expanded="${open}"${open ? ` aria-controls="policy-details-${cls}"` : ""}><img src="/icons/project-expand.svg" width="10" height="10" alt=""><strong>${QUALITY_LABELS[cls]}</strong></button>` +
      `</th>` +
      `<td>${count(row.applied_revisions)}</td><td>${count(row.rolled_back_revisions)}</td>` +
      `<td>${data.quality_available ? `<a class="policy-sample" aria-label="Review ${QUALITY_LABELS[cls].toLowerCase()} sample →" href="${esc(qualityHref("new/"+cls))}"><strong>${esc(quality)} →</strong></a>` : `<strong>${esc(quality)}</strong>`}<p class="caption">${gate ? `${count(gate.passed)} / ${count(gate.comparable)} comparable evals helped` : "Evaluation history unavailable"}</p></td>` +
      `<td class="caption">${manual ? "Always manual approval" : `${count(row.sample_advice.applied)} applied · ≥90% useful${cls === "skill" ? "" : "<br>≤1 rollback"}`}</td>` +
      `<td><button id="policy-switch-${cls}" class="policy-switch" role="switch" aria-label="Auto-apply ${QUALITY_LABELS[cls].toLowerCase()}" aria-checked="${Boolean(row.policy.enabled)}" data-policy-toggle="${cls}"${manual || !data.policy_available || !data.quality_available || entry.busy || entry.loading ? " disabled" : ""}><img src="/assets/${manual ? "policy-manual" : "policy-off"}.svg" width="38" height="22" alt=""><span>${manual ? "Manual" : row.policy.enabled ? "On" : "Off"}</span></button></td></tr>`;
    if (open) html += `<tr class="policy-details"><td colspan="6"><div id="policy-details-${cls}"><div class="policy-detail-grid"><section><h3>Working-copy availability</h3><p>${esc(availability)}</p><p>${row.latest_observation_at ? `Last check ${esc(row.latest_observation_at)}` : "No check time recorded."}</p><p>Observed availability does not prove a session loaded the instruction.</p></section>` +
      `<section><h3>Human quality and evaluations</h3><p>${esc(qualityFraction(q))}</p><p>${q ? `${count(q.uncertain)} uncertain · ${count(q.unreviewed)} unreviewed` : "Judgments unavailable"}</p><p>${gate ? `${count(gate.passed)} / ${count(gate.comparable)} comparable eval attempts helped · ${count(gate.attempts)} total attempts` : "Evaluation history unavailable"}</p></section>` +
      `<section><h3>Class guidance</h3><p>${manual ? "Hooks never auto-apply." : `Suggested: ${count(row.sample_advice.applied)} applied revisions, at least 90% judged useful${cls === "skill" ? "." : ", at most one rollback."} These thresholds are advisory. One explicit class decision authorizes only new eligible proposals.`}</p></section></div>` +
      runDisclosure("policy-class-record:"+cls, "Complete class evidence and counting definitions", {class:row, meaning:data.meaning, coverage:data.coverage}) + `</div></td></tr>`;
    return html;
  }).join("");
  return notice + `<section class="panel policy-panel"><header class="panel__head"><h2 class="panel__title">Auto-apply, one target class at a time</h2><p class="caption">Switches authorize only new eligible proposals. Existing backlog, hooks, human-line deletions and review-only work still need a decision.</p></header>` +
    `<div class="policy-table-scroll" tabindex="0" role="region" aria-label="Class policy and evidence"><table class="policy-table"><thead><tr><th scope="col">Target class</th><th scope="col">Applied revisions</th><th scope="col">Rolled back</th><th scope="col">Human quality &amp; evals</th><th scope="col">Guidance · advisory</th><th scope="col">Auto-apply</th></tr></thead><tbody>${rows}</tbody></table></div>` +
    `<div class="policy-feedback">${readState}<p id="policy-status" tabindex="-1" role="status">${esc(entry.message || "")}</p></div><footer class="policy-footer"><button id="policy-refresh" class="btn btn--quiet" data-policy-refresh="true"${entry.busy || entry.loading ? " disabled" : ""}>${entry.error ? "Retry class evidence" : "Refresh class evidence"}</button>` +
    runDisclosure("quality-class-coverage", "Coverage and counting definitions", {meaning:data.meaning,...data.coverage}) + `</footer></section>`;
}
export function paintClassEvidence() {
  if (state.route === "evals" && !state.evaluationDetail && !state.qualityDetail) preserveOperationView("class-evidence", () => renderClassEvidence(state.classEvidence || {}));
}
export async function loadClassEvidence() {
  const old = state.classEvidence;
  if (old?.busy || old?.loading) return;
  const focused = doc()?.activeElement?.id;
  const entry = {loading:true, data:old?.data || null, error:"", message:old?.message || ""}; state.classEvidence=entry; paintClassEvidence();
  try {entry.data=await getJSON(API.classEvidence);} catch(e) {entry.error=String(e.message || e);entry.errorDetail=e.readDetail || "";}
  finally {entry.loading=false; if(state.classEvidence===entry) {
    paintClassEvidence();
    if (focused === "policy-refresh" && (!doc()?.activeElement?.id || doc().activeElement.id === focused)) doc()?.getElementById(focused)?.focus?.({preventScroll:true});
  }}
}
// Retain only an opaque key and small command input through reload. Never store source text.
async function qualityCommand(body) {
  const signature=JSON.stringify(body);
  const digest=await crypto.subtle.digest("SHA-256",new TextEncoder().encode(signature));
  const storageKey="si-quality-request:"+Array.from(new Uint8Array(digest),v=>v.toString(16).padStart(2,"0")).join("");
  let key=state.qualityRequests[signature];
  try {key=key || window.sessionStorage.getItem(storageKey);} catch (_) { /* storage can be disabled */ }
  if (!key) key="quality-"+crypto.randomUUID();
  state.qualityRequests[signature]=key;
  try {window.sessionStorage.setItem(storageKey,key);} catch (_) { /* in-page retry still works */ }
  const result=await postJSON(API.commands,{...body,request_key:key});
  // Keep the successful key: a lost navigation or reload must recover the same command.
  return result;
}
export async function toggleClassPolicy(cls) {
  const entry=state.classEvidence, row=entry?.data?.classes.find(r=>r.target_class===cls);
  if (!row || entry.busy || cls==="hook") return;
  entry.busy=true;entry.message="Recording your class decision…";paintClassEvidence();
  try {
    const result=await qualityCommand({action:"set_class_policy",target_class:cls,enabled:!row.policy.enabled,expected_revision:row.policy.revision});
    row.policy=result.result.after;if(state.overview?.audit?.policy?.classes?.[cls]){state.overview.audit.policy.classes[cls]=row.policy;paintOverview();}entry.message=`${QUALITY_LABELS[cls]} auto-apply ${row.policy.enabled ? "enabled for new eligible proposals" : "disabled"}. Decision recorded.`;
  } catch(e) {entry.message="Decision not confirmed. Retry reuses its request key; if the revision changed, refresh and inspect it first. "+String(e.message || e);}
  finally {entry.busy=false;paintClassEvidence();doc().getElementById("policy-switch-"+cls)?.focus?.({preventScroll:true});}
}
export function renderQualitySamples(entry = {}) {
  const label = `Human assessment history${entry.data ? ` · ${num(entry.data.count)} retained samples` : entry.loading ? " · loading" : entry.error ? " · read failed" : ""}`;
  let html=`<details ${reviewDisclosure("quality-sample-history:"+(entry.cursor || "newest"), Boolean(entry.cursor || entry.error))}><summary id="quality-history-toggle">${esc(label)}</summary><p>Each sample retains its selection. Its current judgments include later revisions, with the complete judgment history preserved.</p>`;
  if(entry.loading) html+=`<p role="status">Reading human assessment samples…</p>`;
  if(entry.error) html+=`<p role="alert" class="error-state">${esc(entry.error)}</p>`;
  else if(entry.data) {
    html+=`<p id="quality-history-status" tabindex="-1">${num(entry.data.records.length)} samples shown · ${num(entry.data.count)} retained</p><div class="quality-sample-list">`;
    html+=entry.data.records.map(r=>`<article><a href="${esc(qualityHref(r.id))}">${QUALITY_LABELS[r.target_class]} · ${esc(r.created_at)}</a><p>${num(r.counts.reviewed)} / ${num(r.counts.selected)} reviewed · ${esc(qualityFraction(r.counts))}</p><p class="caption">Seed ${esc(r.seed)}</p></article>`).join("")+`</div>`;
    if(!entry.data.records.length) html+=`<p>No saved samples. Choose a class above to preview one.</p>`;
    if(entry.cursor) html+=`<button id="quality-first" class="btn" data-quality-page="first">Newest samples</button> `;
    if(entry.data.next_cursor) html+=`<button id="quality-older" class="btn" data-quality-page="older">Older samples</button> `;
  }
  return html+`<button id="quality-history-refresh" class="btn btn--quiet" data-quality-history-refresh="true"${entry.loading ? " disabled" : ""}>Refresh samples</button></details>`;
}
export async function loadQualitySamples() {
  const cursor=new URLSearchParams(state.evaluationQuery).get("quality_cursor") || "";
  const entry={loading:true,data:null,error:"",cursor};state.qualitySamples=entry;
  const paint=()=>{if(state.route==="evals"&&!state.evaluationDetail&&!state.qualityDetail)preserveOperationView("quality-samples",()=>renderQualitySamples(entry));};paint();
  try {entry.data=await getJSON(API.qualitySamples+(cursor ? "?cursor="+encodeURIComponent(cursor):""));} catch(e){entry.error=String(e.message || e);}
  finally{entry.loading=false;if(state.qualitySamples===entry)paint();}
}
function qualitySource(item, key) {
  const source=item.source;
  return `<p class="caption">${esc(source.destination.target_path || source.destination.relative_path || "Recorded instruction destination")}</p>` +
    runDisclosure(key+"-source","Complete applied content and source identities",source)+
    runDisclosure(key+"-links","Application and learning links",item.links);
}
export function renderQualityDetail(entry) {
  let html=`<a href="#/evals${state.evaluationQuery ? "?"+esc(state.evaluationQuery) : ""}">← Evals &amp; trends</a><header class="view__head"><div><h1 id="quality-title" class="view__title" tabindex="-1">${entry.isNew ? "Preview human assessment sample" : "Human assessment sample"}</h1><p>Assess whether each exact delivered change is useful. Approval and lack of rollback do not supply that judgment.</p></div><button id="quality-refresh" class="btn" data-quality-refresh="true"${entry.busy ? " disabled" : ""}>Refresh sample</button></header>`;
  if(entry.loading)return html+`<p role="status">Reading applied revisions…</p>`;
  if(entry.error)return html+`<p role="alert" class="error-state">${esc(entry.error)}</p>`;
  const data=entry.data;if(!data)return html;
  const shown=entry.isNew ? data : data.preview;
  html+=`<p><strong>${QUALITY_LABELS[shown.target_class]}</strong> · ${num(shown.selected.length)} selected from ${num(shown.eligible)} eligible applied revisions · ${num(shown.excluded_applications.length)} application records excluded across all classes.</p>`;
  if(entry.isNew) {
    html+=`<form id="quality-selection" class="exposure-form quality-selection"><label>Sample size<input id="quality-size" name="size" type="number" min="1" max="100" value="${shown.size}" required></label><label>Reproducible seed<input id="quality-seed" name="seed" minlength="1" maxlength="100" value="${esc(shown.seed)}" required></label><button id="quality-preview-submit" class="btn" type="submit">Preview selection</button></form>` +
      `<p>The same population and seed produce the same selection. Saving freezes these revisions and takes no model calls.</p><button id="quality-create" class="btn" data-quality-create="true"${entry.busy || !shown.selected.length ? " disabled" : ""}>Save sample for assessment</button>`;
    if(!shown.selected.length) html+=`<p>No verified applied revisions are eligible for this class.</p>`;
  } else {
    html+=`<p id="quality-progress">${num(data.counts.reviewed)} / ${num(data.counts.selected)} reviewed · ${num(data.counts.uncertain)} uncertain · ${esc(qualityFraction(data.counts))}</p>`;
  }
  html+=`<p id="quality-status" tabindex="-1" role="status">${esc(entry.message || "")}</p>`;
  html+=(entry.isNew ? shown.selected : data.subjects).map((item,index)=>{
    const id=item.source.id, current=item.judgment, draft=entry.drafts?.[id] || {judgment:current?.judgment || "",note:current?.note || ""};
    let body=`<p class="mono">Revision ${esc(id)}</p><div class="quality-content"><section><h3>Before this change</h3><pre tabindex="0">${esc(item.source.before)}</pre></section><section><h3>Applied content</h3><pre tabindex="0">${esc(item.source.applied)}</pre></section></div>`+qualitySource(item,"quality-"+id);
    if(!entry.isNew)body+=`<p class="caption">${current ? `Latest judgment: ${esc(current.judgment.replaceAll("_"," "))} · ${esc(current.created_at)} · local operator · revision ${current.sequence}` : "No human judgment recorded."}</p><form class="quality-judgment exposure-form" data-quality-subject="${id}"><label for="quality-judgment-${id}">Judgment for revision ${index+1}</label><select id="quality-judgment-${id}" name="judgment" required><option value="">Choose a judgment</option>${["useful","not_useful","uncertain"].map(v=>`<option value="${v}"${draft.judgment===v ? " selected" : ""}>${v==="not_useful" ? "Not useful" : v==="useful" ? "Useful" : "Uncertain"}</option>`).join("")}</select><label for="quality-note-${id}">Assessment note</label><textarea id="quality-note-${id}" name="note" rows="3">${esc(draft.note)}</textarea><button id="quality-save-${id}" type="submit" class="btn"${entry.busy ? " disabled" : ""}>${current ? "Revise judgment" : "Record judgment"}</button></form>`+runDisclosure("quality-"+id+"-judgments",`Complete judgment history (${item.history.length})`,item.history);
    return projectPanel(`Selected revision ${index+1}`,body);
  }).join("");
  return html+runDisclosure("quality-manifest-"+entry.id,"Complete frozen selection and excluded coverage",shown);
}
function paintQuality() {
  if(state.route!=="evals" || !state.qualityDetail)return;
  preserveOperationView("quality-detail",()=>renderQualityDetail(state.qualityDetail));
}
export async function openQuality(id,{refresh=false, selection=null}={}) {
  const previous=state.qualityDetail;
  if(previous?.busy)return;
  const entry={id,isNew:id.startsWith("new/"),data:null,loading:true,error:"",message:"",drafts:refresh && previous?.id===id ? previous.drafts : {}};
  state.qualityDetail=entry;paintQuality();
  const params=new URLSearchParams(selection || (refresh && previous?.data ? {size:previous.data.size || previous.data.preview?.size,seed:previous.data.seed || previous.data.preview?.seed}:{}));
  if(entry.isNew)params.set("target_class",id.slice(4));
  try {entry.data=await getJSON(entry.isNew ? API.qualityPreview+"?"+params : API.qualitySamples+"/"+encodeURIComponent(id));}
  catch(e){entry.error=String(e.message || e);}
  finally{entry.loading=false;if(state.qualityDetail===entry){paintQuality();doc().getElementById(refresh ? "quality-refresh" : "quality-title")?.focus?.({preventScroll:refresh});}}
}
export async function submitQuality(event) {
  const form=event.target, entry=state.qualityDetail;
  if(!entry || !(form.id==="quality-selection" || form.hasAttribute?.("data-quality-subject")))return;
  event.preventDefault();if(entry.busy)return;
  const fields=new FormData(form);
  if(form.id==="quality-selection"){openQuality(entry.id,{selection:{size:fields.get("size"),seed:fields.get("seed")}});return;}
  const id=form.getAttribute("data-quality-subject"), item=entry.data.subjects.find(r=>r.source.id===id);
  if(!item)return;
  const body={action:"judge_quality",sample_id:entry.id,subject_id:id,judgment:fields.get("judgment"),note:fields.get("note"),expected_revision:item.judgment?.sequence || 0};
  entry.busy=true;entry.message="Recording judgment…";paintQuality();
  try {await qualityCommand(body);entry.data=await getJSON(API.qualitySamples+"/"+encodeURIComponent(entry.id));delete entry.drafts[id];entry.message="Judgment recorded. Earlier judgments remain in history.";}
  catch(e){entry.message="Judgment not confirmed. Retrying keeps its request key. Refresh to inspect any newer judgment. "+String(e.message || e);}
  finally{entry.busy=false;paintQuality();doc().getElementById("quality-save-"+id)?.focus?.({preventScroll:true});}
}
async function createQualitySample() {
  const entry=state.qualityDetail;if(!entry?.isNew || !entry.data?.selected.length || entry.busy)return;
  const data=entry.data;entry.busy=true;entry.message="Saving the frozen selection…";paintQuality();
  try {const result=await qualityCommand({action:"create_quality_sample",target_class:data.target_class,size:data.size,seed:data.seed,preview_revision:data.revision});entry.busy=false;setHash(qualityHref(result.result.sample_id));}
  catch(e){entry.message="Sample not confirmed. Retry keeps its request key. If the population changed, refresh the preview. "+String(e.message || e);}
  finally{entry.busy=false;paintQuality();}
}

/* ------------------------------ wiring ----------------------------------- */

function findAttr(target, attribute) {
  if (!target) return null;
  if (typeof target.closest === "function") {
    const found = target.closest(`[${attribute}]`);
    if (!found) return null;
    return found.getAttribute ? found.getAttribute(attribute) : null;
  }
  return null;
}

export function handleMainClick(event) {
  if (event.target.closest?.("#inspector")) return;
  if (findAttr(event.target, "data-dashboard-retry")) return retryDashboardReads();
  const expandProject=findAttr(event.target,"data-project-expand");
  if (expandProject) {
    event.preventDefault();
    if(state.projectExpanded.has(expandProject))state.projectExpanded.delete(expandProject);else state.projectExpanded.add(expandProject);
    paintProjectRows();doc().getElementById("project-expand-"+encodeURIComponent(expandProject))?.focus?.({preventScroll:true});return;
  }
  const projectSort=findAttr(event.target,"data-project-sort");
  if (Object.hasOwn(PROJECT_SORTS,projectSort)) {
    const current=projectListOptions(state.projectListQuery);
    changeProjectSelection({sort:projectSort,dir:current.sort===projectSort ? current.direction==="asc" ? "desc" : "asc" : PROJECT_SORTS[projectSort].direction},"project-sort-"+projectSort);
    return;
  }
  const projectPage=findAttr(event.target,"data-project-page");
  if(projectPage){changeProjectSelection({page:projectPage});return;}
  if(findAttr(event.target,"data-project-clear")){changeProjectSelection({q:""},"projects-search");return;}
  if(findAttr(event.target,"data-context-refresh") || findAttr(event.target,"data-context-older")) {
    loadProjectContextHistory(state.selectedProject,{refresh:Boolean(findAttr(event.target,"data-context-refresh")),older:Boolean(findAttr(event.target,"data-context-older"))});return;
  }
  if(findAttr(event.target,"data-project-refresh")){loadProjects();return;}
  if (handleEvidenceClick(event)) return;
  if (handleRuleBrowserClick(event)) return;
  if(findAttr(event.target,"data-selected-proposal-refresh") && state.reviewRouteProposal){loadExcludedProposal(state.reviewRouteProposal,{refresh:true});return;}
  if(findAttr(event.target,"data-execution-refresh") && state.executionRoute){const {kind,id}=state.executionRoute;openExecutionRoute(kind,id,{refresh:true});return;}
  if (findAttr(event.target,"data-recurrence-refresh") || findAttr(event.target,"data-recurrence-older") || findAttr(event.target,"data-measurement-id")) {handleInspectorClick(event);return;}
  const evidence=findAttr(event.target,"data-policy-evidence");if(evidence){const key="policy-class:"+evidence;state.operationDisclosures[key]=!state.operationDisclosures[key];paintClassEvidence();return;}
  const cls=findAttr(event.target,"data-policy-toggle");if(cls){toggleClassPolicy(cls);return;}
  if(findAttr(event.target,"data-policy-refresh")){loadClassEvidence();return;}
  if(findAttr(event.target,"data-quality-history-refresh")){loadQualitySamples();return;}
  if(findAttr(event.target,"data-quality-refresh") && state.qualityDetail){openQuality(state.qualityDetail.id,{refresh:true});return;}
  if(findAttr(event.target,"data-quality-create")){createQualitySample();return;}
  const qualityPage=findAttr(event.target,"data-quality-page");
  if(qualityPage){
    const params=new URLSearchParams(state.evaluationQuery);
    if(qualityPage==="older" && state.qualitySamples?.data?.next_cursor)params.set("quality_cursor",state.qualitySamples.data.next_cursor);
    else params.delete("quality_cursor");
    setHash("#/evals"+(params.toString() ? "?"+params : ""));return;
  }
  if (findAttr(event.target, "data-evaluation-refresh")) {loadEvaluationHistory(state.evaluationQuery, {refresh:true}); loadEvaluationHealth({refresh:true}); return;}
  if (findAttr(event.target, "data-evaluation-health-refresh")) {loadEvaluationHealth({refresh:true}); return;}
  if (findAttr(event.target, "data-evaluation-detail-refresh") && state.evaluationDetail) {
    const entry = state.evaluationDetail; openEvaluationDetail(entry.kind, entry.id, entry.query, {refresh:true}); return;
  }
  const evaluationOpen = findAttr(event.target, "data-evaluation-open");
  if (evaluationOpen) {
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || (event.button != null && event.button !== 0)) return;
    event.preventDefault();
    const route = parseRoute(currentHash());
    setHash(evaluationHref(findAttr(event.target, "data-evaluation-kind"), evaluationOpen, route.trendQuery || "")); return;
  }
  const evaluationPage = findAttr(event.target, "data-evaluation-page");
  if (evaluationPage) {
    const params = new URLSearchParams(state.evaluationQuery);
    if (evaluationPage === "older" && state.evaluationHistory?.data?.next_cursor) params.set("eval_cursor", state.evaluationHistory.data.next_cursor);
    else params.delete("eval_cursor");
    setHash("#/evals" + (params.toString() ? "?" + params : "")); return;
  }
  const trendContext = findAttr(event.target, "data-trend-context");
  if (trendContext) {
    const detail = doc().getElementById("trend-context-"+trendContext);
    const summary = doc().getElementById("trend-context-toggle-"+trendContext);
    if (detail && summary) {detail.open=true; summary.focus(); summary.scrollIntoView({block:"nearest"});}
    return;
  }
  if (findAttr(event.target, "data-trend-refresh")) {loadTrends(state.trends?.params || "", {refresh:true}); return;}
  if (findAttr(event.target, "data-trend-deliveries-next") || findAttr(event.target, "data-trend-deliveries-first")) {
    const params = new URLSearchParams(state.evaluationQuery);
    if (findAttr(event.target, "data-trend-deliveries-next")) params.set("delivery_cursor", state.trends.data.deliveries.next_cursor);
    else params.delete("delivery_cursor");
    setHash("#/evals?" + params.toString()); return;
  }
  if (findAttr(event.target,"data-project-rules-refresh")) {loadProjectRuleSummary(state.selectedProject,{refresh:true});return;}
  const projectKind = findAttr(event.target, "data-project-records");
  if (projectKind) {loadProjectRecords(projectKind, {older: Boolean(findAttr(event.target, "data-project-older")), refresh: Boolean(findAttr(event.target, "data-project-records-refresh")), learningId: findAttr(event.target, "data-project-learning") || ""}); return;}
  if (findAttr(event.target, "data-project-summary-refresh")) {
    const key = state.selectedProject;
    loadProjectDetail(key, {refresh: true}); loadProjectInventory(key, {refresh: true});
    if (state.inspectorTab === "summary") ["contributed", "proposals", "deliveries"].forEach(kind => loadProjectRecords(kind, {refresh: true}));
    if (state.inspectorTab === "sessions") loadProjectSessions(key, state.projectExposureParams, {refresh: true});
    if (state.inspectorTab === "loads") loadProjectNativeEvents(key, state.projectExposureParams, {refresh: true});
    if (state.inspectorTab === "recurrence") loadRecurrence(key,{refresh:true});
    return;
  }
  const scanId = findAttr(event.target, "data-scan-history");
  if (scanId) {loadIncidentScanHistory(scanId, {older: Boolean(findAttr(event.target, "data-scan-history-older"))}); return;}
  const artifactKey = findAttr(event.target, "data-run-artifact");
  if (artifactKey) {inspectRunArtifact(artifactKey);return;}
  const runSection = findAttr(event.target, "data-run-section");
  if (runSection) {
    const node=doc().getElementById("run-section-"+runSection);node?.focus();node?.scrollIntoView({block:"start"});
    const records=findAttr(event.target,"data-run-records");
    if (records) loadRunRecords(records);
    return;
  }
  if (findAttr(event.target, "data-run-backlog")) { loadRunBacklog(); return; }
  const runKind = findAttr(event.target, "data-run-records");
  if (runKind) { loadRunRecords(runKind, findAttr(event.target, "data-run-older") === "true"); return; }
  if (findAttr(event.target, "data-run-refresh") && state.runDetail) { openRunRoute(state.runDetail.route); return; }
  const target = event.target;
  const evalId = findAttr(target, "data-eval-preview");
  if (evalId) { openEvalJob(evalId, {newAttempt:Boolean(findAttr(target,"data-eval-new")), action:findAttr(target,"data-job-action") || "regenerate_eval"}); return; }
  if (findAttr(target,"data-recovery-refresh")) {openRecoveryJob(state.evalJob.selection,{newAttempt:true});return;}
  if (findAttr(target, "data-mining-source")) { inspectMiningSource(); return; }
  if (findAttr(target, "data-incidents-refresh")) { loadMiningIncidents(); return; }
  if (findAttr(target, "data-incidents-older")) { loadMiningIncidents({older:true}); return; }
  if (findAttr(target, "data-eval-submit")) { submitEvalJob(); return; }
  if (findAttr(target, "data-eval-refresh")) { openEvalJob(state.evalJob.proposalId,{action:state.evalJob.action}); return; }
  const operationFocus = findAttr(target, "data-operation-focus");
  if (operationFocus) {
    const card = doc().getElementById("operation-" + operationFocus);
    if (card && card.scrollIntoView) card.scrollIntoView({block: "nearest"});
    if (card && card.focus) card.focus({preventScroll: true});
    return;
  }
  const operationAction = findAttr(target, "data-operation-action");
  if (operationAction) { requestOperationControl(findAttr(target, "data-operation-id"), operationAction); return; }
  if (findAttr(target, "data-operation-refresh")) { loadOperationHistory(); return; }
  if (findAttr(target, "data-operation-older")) { loadOperationHistory({ older: true }); return; }
  const operationInspect = findAttr(target, "data-operation-inspect");
  if (operationInspect) { inspectOperation(operationInspect); return; }
  if (findAttr(target, "data-rollback-submit")) { submitRollback(); return; }
  if (findAttr(target, "data-rollback-refresh")) { openRollback(state.rollback.proposalId, { refresh: true }); return; }
  const deliveryAction = findAttr(target, "data-delivery-action");
  if (deliveryAction) { requestDeliveryControl(findAttr(target, "data-command-id"), deliveryAction); return; }
  if (findAttr(target, "data-delivery-refresh")) { loadDeliveryHistory(); return; }
  if (findAttr(target, "data-delivery-older")) { loadDeliveryHistory({ older: true }); return; }
  const inspect = findAttr(target, "data-delivery-inspect");
  if (inspect) { inspectDelivery(inspect); return; }
  const evidencePage = findAttr(target,"data-review-evidence-page");
  if (evidencePage !== null && evidencePage !== "") {state.reviewEvidencePages[state.selectedFamily]=Number(evidencePage);paintReview();return;}
  const retryEvaluation = findAttr(target,"data-review-evaluation-retry");
  if (retryEvaluation) {state.reviewDisclosures["review-evaluation:" + retryEvaluation]=true;loadReviewEvaluation(retryEvaluation,{refresh:true});paintReview();return;}
  const individual = findAttr(target, "data-review-individual");
  if (individual) {
    state.reviewIndividual[individual] = !state.reviewIndividual[individual];
    paintReview();
    return;
  }
  const selected = findAttr(target, "data-select-proposal");
  if (selected) {
    state.reviewExcluded[selected] = !state.reviewExcluded[selected];
    paintReview();
    return;
  }
  if (findAttr(target, "data-reload-review")) {
    state.reviewPreviews = {};
    load();
    return;
  }
  const decision = findAttr(target, "data-decision");
  if (decision) {
    const familyId = findAttr(target, "data-learning-id");
    if (familyId) {
      state.selectedFamily = familyId;
      decideFamily(familyId, decision);
      return;
    }
  }
  const openFamily = findAttr(target, "data-open-family");
  if (openFamily) {
    state.selectedFamily = openFamily;
    paintReview();
    revealSelectedCard();
    return;
  }
  const tab = findAttr(target, "data-tab");
  const ruleId = findAttr(target, "data-rule-id");
  const projectKey = findAttr(target, "data-project-key");
  if (projectKey) {
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || (event.button != null && event.button !== 0)) return;
    event.preventDefault();
    const focus = findAttr(target, "data-focus");
    setHash(`#/projects/${encodeURIComponent(projectKey)}${focus ? "?tab=" + encodeURIComponent(focus) : ""}`);
    openProject(projectKey, focus || "summary");
    return;
  }
  if (ruleId) {
    if(target.closest?.("a[data-rule-id]"))return;
    setHash(rulesHref(ruleId));
    openRule(ruleId, tab || "why");
  }
}

/**
 * Enter and Space on a focused row do what a click does.
 *
 * Legacy interactive rows carry tabindex="0", so they advertise themselves to
 * assistive technology and to the tab key as interactive. Until 2026-08-23 the
 * only activation path was a click listener, so a keyboard user could focus a
 * row and press Enter forever: the inspector — the whole progressive-disclosure
 * journey of V2 and V4 — was unreachable without a mouse.
 */
export function handleMainKeydown(event) {
  if (event.key !== "Enter" && event.key !== " " && event.key !== "Spacebar") return;
  const target = event.target;
  // Native controls already synthesize their own click; do not activate twice.
  if (target.closest?.("a,button,input,select,textarea,summary")) return;
  if (!findAttr(target, "data-rule-id") && !findAttr(target, "data-project-key")) return;
  // Space scrolls the page by default, which is the wrong thing once the key
  // means "open this row".
  event.preventDefault();
  handleMainClick(event);
}

export function handleInspectorClick(event) {
  if(event.target.closest?.("a[data-tab]"))return;
  if(state.inspectorKind==="evidence" && handleEvidenceInspector(event))return;
  if(findAttr(event.target,"data-rule-detail-refresh")){loadRuleDetail(state.selectedRule,{refresh:true});return;}
  if (findAttr(event.target,"data-recurrence-refresh")) {loadRecurrence(recurrenceProject(),{refresh:true});return;}
  if (findAttr(event.target,"data-recurrence-older")) {loadRecurrence(recurrenceProject(),{older:true});return;}
  const measurementId=findAttr(event.target,"data-measurement-id");if(measurementId) {loadMeasurement(measurementId);return;}
  if (findAttr(event.target, "data-native-loads-refresh")) {loadProjectNativeEvents(state.selectedProject, state.projectExposureParams, {refresh:true}); return;}
  if (findAttr(event.target, "data-native-loads-older")) {loadProjectNativeEvents(state.selectedProject, state.projectExposureParams, {older:true}); return;}
  if (findAttr(event.target, "data-sessions-refresh")) {loadProjectSessions(state.selectedProject, state.projectExposureParams, {refresh: true}); return;}
  if (findAttr(event.target, "data-sessions-older")) {loadProjectSessions(state.selectedProject, state.projectExposureParams, {older: true}); return;}
  if (findAttr(event.target, "data-inventory-refresh")) {loadProjectInventory(state.selectedProject, {refresh: true}); return;}
  if (findAttr(event.target, "data-inventory-older")) {loadProjectInventory(state.selectedProject, {older: true}); return;}
  if (findAttr(event.target, "data-availability-refresh")) {loadProjectAvailability(state.selectedProject, {refresh: true}); return;}
  if (findAttr(event.target, "data-availability-older")) {loadProjectAvailability(state.selectedProject, {older: true}); return;}

  const scanId = findAttr(event.target, "data-scan-history");
  if (scanId) {
    if (state.inspectorKind === "rule" && !findAttr(event.target, "data-scan-history-older")) setHash(rulesHref(state.selectedRule, {...Object.fromEntries(new URLSearchParams(state.ruleQuery)),tab:"evidence",scan:scanId}));
    loadIncidentScanHistory(scanId, {older: Boolean(findAttr(event.target, "data-scan-history-older"))}); return;
  }
  if (findAttr(event.target, "data-exposure-reset")) {
    setHash(`#/projects/${encodeURIComponent(state.selectedProject)}?tab=exposure`);
    openProject(state.selectedProject, "exposure"); return;
  }
  if (findAttr(event.target, "data-exposure-retry")) {loadProjectExposure(state.selectedProject, state.projectExposureParams, {retry: true}); return;}
  const miningId=findAttr(event.target,"data-mining-history");
  if(miningId){loadRuleMiningHistory(miningId,{older:Boolean(findAttr(event.target,"data-mining-history-older"))});return;}
  const tab = findAttr(event.target, "data-tab");
  if (!tab) return;
  if (state.inspectorKind === "rule") {
    setHash(rulesHref(state.selectedRule, {...Object.fromEntries(new URLSearchParams(state.ruleQuery)),tab,scan:""}));
    openRule(state.selectedRule, tab);
  }
  else if (state.inspectorKind === "project") {
    setHash(`#/projects/${encodeURIComponent(state.selectedProject)}?tab=${encodeURIComponent(tab)}`);
    openProject(state.selectedProject, tab);
  }
}

export function handleSearchInput(event) {
  state.query = event && event.target ? String(event.target.value || "") : "";
  if (evidenceMode() || state.rules?.mode === "paged" || state.rulePage) {
    clearTimeout(state.rulesSearchTimer);
    const value = state.query;
    state.rulesSearchTimer = setTimeout(() => {state.rulesSearchTimer=null; if(state.route === "rules") (evidenceMode() ? changeEvidenceSelection : changeRuleSelection)({query:value});}, 220);
  } else paintRuleRows();
}

function setHash(hash) {
  if (typeof location === "undefined") return;
  try {
    location.hash = hash;
  } catch (error) {
    /* a shimmed location may refuse; the inspector is already open either way */
  }
}

export function currentHash() {
  if (typeof location === "undefined") return "";
  return location.hash || "";
}

let navigation=null;
let navigationRoute="";
const NAV_LABELS={overview:"Overview",rules:"Rules",review:"Review queue",projects:"Projects",evals:"Evals & trends"};
export function renderNavigationBreadcrumb(route) {
  if(!route.id || !["rules","review"].includes(route.view))return "";
  const parts=route.id.split("/"),kind=parts[0];
  const labels={family:"Full review",proposal:"Proposal",command:"Command",operation:"Operation",rollback:"Rollback preview",resolution:"Resolution preview",reapply:"Reapplication preview",mine:"Mining preview",eval:"Evaluation preview",recovery:"Recovery preview"};
  const label=route.view==="rules"?(kind==="evidence"?"Evidence / "+(EVIDENCE_LABELS[parts[1]] || parts[1]):"Rule"):labels[kind] || "Record";
  return `<nav class="run-breadcrumb" aria-label="Breadcrumb"><a href="#/${route.view}">${esc(NAV_LABELS[route.view])}</a> / <span aria-current="page">${esc(label)}</span></nav>`;
}
function currentNavigationLink() {
  const route=parseRoute(currentHash());
  return {href:currentHash() || "#/overview",label:NAV_LABELS[route.view]+(route.id?" · "+route.id:"")};
}
export function wire() {
  const d = doc();
  let storage=null;try {storage=window.localStorage;} catch { /* Session-only saved links remain available. */ }
  navigation=initNavigation({document:d,storage,
    navigate:href=>{if(currentHash()===href)showRoute(parseRoute(href));else setHash(href);byId("main").focus();},
    search:query=>getJSON(API.evidence+"?"+new URLSearchParams({query,limit:"8"})),
    projects:()=>state.projects,current:currentNavigationLink,toggleTheme,
  });
  byId("skip-to-main").addEventListener("click", (event) => {
    event.preventDefault();
    byId("main").focus();
  });
  byId("theme-toggle").addEventListener("click", () => toggleTheme());
  byId("character-shortcuts-toggle").addEventListener("click", () => toggleCharacterShortcuts());
  byId("inspector-close").addEventListener("click", dismissInspector);
  byId("inspector-body").addEventListener("click", handleInspectorClick);
  byId("inspector-body").addEventListener("submit", submitProjectExposure);
  byId("project-detail-body").addEventListener("click", handleInspectorClick);
  byId("project-detail-body").addEventListener("submit", submitProjectExposure);
  byId("project-detail-body").addEventListener("submit", submitProjectSessions);
  byId("main").addEventListener("click", handleMainClick);
  byId("main").addEventListener("focusin", handleRuleBrowserFocus);
  byId("main").addEventListener("submit", submitQuality);
  byId("main").addEventListener("submit", submitProjectSearch);
  byId("main").addEventListener("input", event => {
    const form=event.target.closest?.("form[data-quality-subject]"), entry=state.qualityDetail;
    if(form && entry){const fields=new FormData(form);entry.drafts[form.getAttribute("data-quality-subject")]={judgment:fields.get("judgment"),note:fields.get("note")};}
  });
  byId("main").addEventListener("submit", submitTrends);
  byId("main").addEventListener("submit", submitEvaluationFilter);
  byId("main").addEventListener("keydown", handleMainKeydown);
  byId("main").addEventListener("change", (event) => {
    if (event.target.id === "evidence-kind") changeEvidenceSelection({kinds:event.target.value});
    if (event.target.id === "evidence-project") changeEvidenceSelection({project_key:event.target.value});
    if (event.target.id === "rules-group-toggle") changeRuleSelection({grouping:String(event.target.checked)});
    if (event.target.id === "rules-sort") changeRuleSelection({sort:event.target.value});
    if (event.target.id === "project-copy" && state.selectedProject) {
      state.projectInventorySelection[state.selectedProject] = event.target.value;
      setHash(projectReadHref(state.selectedProject,state.inspectorTab,{...Object.fromEntries(new URLSearchParams(state.projectExposureParams)),working_copy_id:event.target.value}));
      paintProjectDetail();
      if(state.inspectorTab==="context")loadProjectContextHistory(state.selectedProject);
    }
    if (event.target.id === "run-selector" && state.runDetail) {
      location.hash = runHref("run", event.target.value, state.runDetail.stage);
    }
  });
  byId("rules-search-input").addEventListener("input", handleSearchInput);
  byId("ov-grid-numbers").addEventListener("click", () => {
    state.numbers = !state.numbers;
    byId("ov-grid-numbers").setAttribute("aria-pressed", state.numbers ? "true" : "false");
    if (state.overview) setHTML("ov-grid", renderGrid(state.overview.grid || {}, { numbers: state.numbers }));
  });
  byId("ov-refresh").addEventListener("click", async () => {
    const button = byId("ov-refresh");
    if (button.disabled) return;
    let ownsFocus = d.activeElement === button;
    // Disabling a focused native button can leave focus on the document body.
    // Remember any later focus choice even if that control subsequently blurs.
    const moved = event => { if (event.target !== button) ownsFocus = false; };
    d.addEventListener("focusin", moved);
    button.disabled = true;
    try { await load(); }
    finally {
      d.removeEventListener("focusin", moved);
      button.disabled = false;
      if (ownsFocus && state.route === "overview" && !state.runDetail) button.focus({preventScroll:true});
    }
  });
  if (typeof window !== "undefined" && window.addEventListener) {
    window.addEventListener("hashchange", () => showRoute(parseRoute(currentHash())));
    if (window.setInterval && state.deliveryTimer === null) {
      state.deliveryTimer = window.setInterval(() => {
        if (state.route === "review" && doc().visibilityState !== "hidden" && !Object.keys(state.deliveryBusy).length) loadDeliveryHistory();
        if (state.route === "review" && doc().visibilityState !== "hidden" && !Object.keys(state.operationBusy).length && !state.rollback.busy) loadOperationHistory();
      }, 2500);
    }
  }
  if (d && d.addEventListener) {
    d.addEventListener("keydown", (event) => {
      if (navigation?.handleShortcut(event)) return;
      if (event.key === "Escape" && state.reviewDetailFamily && handleReviewKeydown(event)) return;
      if (event.defaultPrevented || event.isComposing || event.repeat || event.ctrlKey || event.metaKey || event.altKey || isEditing(event.target)) return;
      if (state.characterShortcuts.enabled && event.key === "/" && state.route === "rules") {
        const input = byId("rules-search-input");
        if (d.activeElement !== input) {
          if (typeof event.preventDefault === "function") event.preventDefault();
          if (typeof input.focus === "function") input.focus();
        }
      }
      handleReviewKeydown(event);
      if (event.key === "Escape") dismissInspector();
    });
  }
}

export async function start() {
  applyTheme(readStoredTheme());
  loadCharacterShortcuts();
  wire();
  state.initialLoading=true;
  showRoute(parseRoute(currentHash()));
  const result = await load();
  state.initialLoading=false;
  showRoute(parseRoute(currentHash()));
  return result;
}

const bootDocument = doc();
if (bootDocument && bootDocument.getElementById && bootDocument.getElementById("app-root")) {
  start().catch((error) => {
    showError("The dashboard failed to start", String(error && error.message ? error.message : error));
  });
}
