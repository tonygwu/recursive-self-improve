/* Dashboard single-page app. Vanilla ES module with same-origin requests.
 *
 * Overview, Rules, and Projects display recorded data. Review records explicit
 * commands and shows delivery, rollback, and model-job progress. File writes run
 * in separate workers. GET and POST requests use their shared transport helpers.
 *
 * Missing measurements retain their reasons; unknown states stay visible.
 * Search matches every whitespace-separated token across the loaded rule payload.
 * Theme selection uses localStorage when available and otherwise stays local
 * to the current page. Assets require no CDN, webfonts, or build step.
 */

/** Shared API routes; detail paths are built from record identifiers. */
export const API = Object.freeze({
  overview: "/api/overview",
  rules: "/api/rules",
  projects: "/api/projects",
  review: "/api/review-queue",
  reviewPreview: "/api/review-preview",
  commands: "/api/commands",
  operations: "/api/operations",
  incidents: "/api/incidents",
  runs: "/api/runs",
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

export const VIEWS = Object.freeze(["overview", "rules", "projects", "review"]);

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
  refused: "attempted work, reported no failures and produced nothing — the budget refused it",
  unreadable: "the run recorded a shape this dashboard cannot read",
  unknown_status: "the run carries a status the schema does not list",
  info: "a neutral note",
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
  if (meaning) {
    return DRAWN_STATES.indexOf(state) === -1
      ? `${state}: ${meaning} (no drawn state in the design system — shown as unmapped)`
      : `${state}: ${meaning}`;
  }
  return `${state}: this dashboard has no meaning for this state — shown as unmapped`;
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
        `<div class="banner__body">Nothing can be dated from the data, so no rate on this page has a window.</div></div>`,
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
      `<div class="banner__body">Rates below use a window ending at the last day the data covers, not today. ` +
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

export function renderTiles(data) {
  const backlog = data.backlog || {};
  const inbox = data.inbox || {};
  const statusLine = data.status_line || {};
  const confidence = data.confidence || {};
  const inboxState = (inbox.count || 0) > 0 ? "partial" : "ok";
  const tiles = [];

  tiles.push(
    `<div class="tile"><div class="tile__label">Incidents queued</div>` +
      `<div class="tile__value">${num(backlog.queued)}</div>` +
      `<div class="tile__sub">status <span class="mono">new</span>, never mined</div></div>`
  );

  tiles.push(
    `<div class="tile" data-state="${esc(inboxState)}"><div class="tile__label">Waiting on you` +
      infotip(
        "Proposals requiring your decision, including ungated proposals and passing proposals whose automatic policy is off. " +
          "Only passing proposals permitted by their target class and run policy await automatic delivery."
      ) +
      `</div>` +
      `<div class="tile__value">${num(inbox.count)}</div>` +
      `<div class="tile__sub"${inbox.second_line ? ' data-state="info"' : ""}>${esc(inbox.second_line || inbox.line || "")}</div></div>`
  );

  tiles.push(
    `<div class="tile"><div class="tile__label">Rules learned</div>` +
      `<div class="tile__value">${num(statusLine.rules_learned_total)}</div>` +
      `<div class="tile__sub">${num(statusLine.rules_learned_in_window)} in the last ${num(statusLine.nights_window)} nights</div></div>`
  );

  tiles.push(
    `<div class="tile"><div class="tile__label">Applied</div>` +
      `<div class="tile__value">${num(confidence.applied)}</div>` +
      `<div class="tile__sub">${fractionText(confidence.survival)}</div></div>`
  );

  return tiles.join("");
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
            parts.push(`${num(cell.succeeded)} of ${num(cell.attempted)} succeeded, ${num(cell.failed)} failed`);
            const refused = (cell.per_run || []).reduce((sum, run) => sum + (run.refused || 0), 0);
            if (refused) parts.push(`${num(refused)} refused by the call budget`);
          // The displayed number carries no unit on screen, and the same "0"
          // means "0 verdicts" under gate and "0 edits applied" under apply.
          // The payload has always supplied number_label; it was discarded.
          if (cell.number_label) parts.push(`the figure shown is ${cell.number_label}`);
          }
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
export function renderGridLegend(grid) {
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
      const label = drawn ? STATE_MEANING[state] || state : `${state} (unmapped)`;
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
      `${num(grid.unreadable.length)} stage record(s) had a shape this view cannot read and are drawn as unmapped, never as ok.`
    );
  }
  const unknown = Object.keys(grid.unknown_run_statuses || {});
  if (unknown.length) {
    notes.push(`Run status(es) the schema does not list: ${unknown.map((s) => esc(s)).join(", ")}.`);
  }
  return notes.join(" ");
}

/**
 * Backlog as two competing rates. Never a countdown: the queue grows, so any
 * "N nights to drain" number would be fiction.
 */
export function renderBacklog(backlog) {
  if (!backlog) return "";
  const arrivals = backlog.arrivals || {};
  const perDay = arrivals.per_day;
  const capacity = backlog.mine_capacity_per_run;
  const net = backlog.net_per_day;
  const netState = net === null || net === undefined ? "info" : net > 0 ? "partial" : "ok";

  const rates =
    `<div class="rates">` +
    rate(num(backlog.queued), "queued") +
    `<span class="rate__sep">&middot;</span>` +
    rate(num(capacity), "mined per run (cap)") +
    `<span class="rate__sep">&middot;</span>` +
    rate(
      perDay === null || perDay === undefined
        ? novalue(null, "no incident carries a timestamp, so there is no arrival rate")
        : dec1(perDay),
      `arriving / day (${num(arrivals.window_days)}d)`
    ) +
    `<span class="rate__sep">&middot;</span>` +
    `<div class="rate rate--net" data-state="${esc(netState)}">` +
    `<span class="rate__value">${net === null || net === undefined ? novalue(null, "no arrival rate, so no net") : esc((net > 0 ? "+" : "") + dec1(net))}</span>` +
    `<span class="rate__label">net / day</span></div>` +
    `</div>`;

  let race;
  if (perDay === null || perDay === undefined) {
    race = emptyState(
      "No arrival rate",
      "No incident carries a transcript timestamp, so arrivals cannot be measured.",
      "A meter at zero would claim a measured zero. This is an absent measurement.",
      { inline: true }
    );
  } else {
    const peak = Math.max(perDay, capacity, 1);
    race =
      `<div class="race">` +
      `<span class="race__label">arriving</span>` +
      `<span class="meter"><span class="meter__fill" style="--pct: ${esc(dec1((perDay / peak) * 100))}"></span></span>` +
      `<span class="race__value">${esc(dec1(perDay))} / day</span>` +
      `<span class="race__label">mine capacity</span>` +
      `<span class="meter"><span class="meter__fill meter__fill--capacity" style="--pct: ${esc(dec1((capacity / peak) * 100))}"></span></span>` +
      `<span class="race__value">${num(capacity)} / run</span>` +
      `</div>`;
  }
  return rates + race;
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
      `${num(arrivals.window_days)} days ending ${esc(arrivals.window_end || EM_DASH)} (the last day the data covers, not today).`,
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
      if (entry.status === "fixed") marks.push(badge(`fixed ${String(entry.fixed_at || "").slice(0, 10)}`, { state: "ok" }));
      if (entry.status === "regressed") marks.push(badge("regressed after the fix", { state: "failed" }));
      if (entry.status === "quiet") {
        marks.push(badge(`not seen in ${num(entry.recent_window_days)} run-days`, { state: "info" }));
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
          `</li>`
      );
    });
  });

  if (items.length === 0) {
    return emptyState(
      "Nothing has failed",
      "No run recorded a failure class and no LLM call ended in a failing outcome.",
      "This is a measured zero, taken from runs.stats_json taxonomies and llm_calls.outcome.",
      { state: "ok" }
    );
  }

  const notFailures = (failures.not_failures || [])
    .map((entry) => `<li class="failure"><span class="failure__name muted">${esc(entry.class)}</span>` +
      `<span class="failure__count muted">${num(entry.count)}</span>` +
      `<span class="failure__note">${esc(entry.why)}</span></li>`)
    .join("");

  return (
    `<ul class="failures">${items.join("")}</ul>` +
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
      const selected = row.id === selectedId ? ' aria-selected="true"' : "";
      const text = ruleSnippet(row);
      const gate = row.gate_verdict
        ? badge(row.gate_verdict, {
            verdict: row.gate_verdict,
            title: VERDICT_MEANING[row.gate_verdict] || `verdict ${row.gate_verdict} is not one of the four this dashboard knows`,
          })
        : novalue(null, "no proposal for this rule carries an eval_result_id");
      const enforcement = (row.enforcement_gap || {}).flagged
        ? ` ${badge("written but ignored", { state: "partial", title: (row.enforcement_gap || {}).label })}`
        : "";
      return (
        `<tr data-rule-id="${esc(row.id)}" tabindex="0" role="button"${selected}>` +
        `<td><span class="strong">${esc(ruleHeading(row))}</span>${enforcement} ` +
        `<span class="id">${esc(String(row.id).slice(0, 8))}</span>` +
        `<div class="caption muted">${esc(text.prefix)}${esc(text.text)}` +
        `${text.cut ? esc(`… (${text.cut} more characters)`) : ""}</div></td>` +
        `<td>${badge(row.status, { state: row.status === "proposed" ? "info" : "" })}</td>` +
        `<td class="mono">${esc(row.target_summary)}</td>` +
        `<td class="num">${num(row.evidence_linked)}<span class="caption muted"> / ${num(row.evidence_count)}</span></td>` +
        `<td class="num">${num(row.project_count)}</td>` +
        `<td>${gate}</td>` +
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

export function renderTabs(tabs, active) {
  return (
    `<div class="tabs">` +
    tabs
      .map(
        (tab) =>
          `<button class="tab" type="button" data-tab="${esc(tab.id)}" aria-selected="${tab.id === active ? "true" : "false"}">${esc(tab.label)}</button>`
      )
      .join("") +
    `</div>`
  );
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
      section(
        "Enforcement gap",
        `<p class="field__value">${badge(gap.flagged ? "written but ignored" : "not recorded", {
          state: gap.flagged ? "partial" : "info",
          title: gap.label,
        })} ${esc(gap.label)}</p>` +
          (gap.violated_existing_rule
            ? stackField("The rule it violated", `<span class="mono">${esc(gap.violated_existing_rule)}</span>`)
            : `<p class="footnote">No violated rule was recorded for this learning.</p>`)
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
        field("Repos", (provenance.repos || []).map((r) => esc(r)).join(", ") || novalue(null, "no evidence incident carries a repo")) +
          field("Agent product", esc(provenance.agent_product || EM_DASH)) +
          field("Agents in evidence", (provenance.agents_in_evidence || []).map((a) => esc(a)).join(", ") || EM_DASH) +
          field("Evidence", `${num(provenance.incidents_total)} incident(s), ${num(row.project_count)} project(s)`) +
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
        (provenance.session_ids || []).length
          ? `<div class="kv">${(provenance.session_ids || []).map((id) => `<span class="id">${esc(id)}</span><span></span>`).join("")}</div>` +
              `<p class="footnote">${num(provenance.session_ids_total)} distinct session(s) in the evidence.</p>`
          : emptyState("No session ids", "The evidence incidents carry no session id.", "", { inline: true })
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

  return renderTabs(RULE_TABS, active) + body;
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
  if (history.error) html += `<p class="error-state" role="alert">${esc(history.error)}</p>`;
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
  history.loading = true; history.error = ""; state.scanHistories[id] = history;
  const paint = () => doc()?.querySelectorAll('[data-scan-history-root]').forEach(root => {
    if (root.getAttribute('data-scan-history-root') === id) preserveOperationView(root.id, () => scanHistoryBody(id, root.id));
  });
  paint();
  try {
    const result = await getJSON(`/api/incidents/${encodeURIComponent(id)}/scan-history?limit=20` + (older ? `&cursor=${encodeURIComponent(history.next_cursor)}` : ""));
    if (result.selector?.incident_id !== id) throw new Error("Scan history identifies a different incident");
    const records = older ? [...history.records, ...result.records] : result.records;
    Object.assign(history, result, {records: [...new Map(records.map(record => [record.id, record])).values()], loaded: true});
  } catch (error) { history.error = String(error.message || error); }
  finally {
    const canRestore = focusId && (doc()?.activeElement?.id === focusId || doc()?.activeElement?.closest?.('[data-scan-history-root]')?.id === focusRoot);
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
  const occurrences = incident.occurrences
    ? `<p class="footnote">Recurred ${num(incident.occurrences.total_count)} time(s) across ${num(incident.occurrences.sessions)} session(s), ` +
      `${esc(incident.occurrences.first_ts)} to ${esc(incident.occurrences.last_ts)}.</p>`
    : "";
  const cut = incident.display_text_truncated
    ? `<p class="footnote">${num(incident.display_text_truncated.cut_chars)} character(s) cut of ${num(incident.display_text_truncated.original_chars)}.</p>`
    : "";
  return (
    `<div class="field field--stack">` +
    `<span class="field__label">${marks.join(" ")} <span class="id">${esc(incident.id)}</span> ${esc(incident.ts || "")}</span>` +
    `<span class="field__value mono">${esc(incident.display_text)}</span>` +
    occurrences +
    cut +
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
        `<pre class="scroll-x diff"><code>${esc(target.diff)}</code></pre>` +
        (target.proposal_status === "applied" ? `<a class="review-link" href="#/review/rollback/${encodeURIComponent(target.proposal_id)}">Review rollback of this change →</a>` : "") +
        (target.proposal_status === "rolled_back" ? `<a class="review-link" href="#/review/reapply/${encodeURIComponent(target.proposal_id)}">Review reapplication →</a>` : "") +
        (target.proposal_id ? `<a class="review-link" href="#/review/eval/${encodeURIComponent(target.proposal_id)}">Review evaluation regeneration →</a>` : "") +
        `</div>`
      );
    })
    .join("");
}

export function renderRuleVerdicts(row) {
  const targets = row.targets || [];
  if (targets.length === 0) {
    return emptyState(
      "Not routed anywhere yet",
      row.target_summary || "No proposal has been created for this rule.",
      "A learning is what was learned; a proposal is one edit to one file. One learning can produce zero proposals or several.",
      { inline: true }
    );
  }
  return targets
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
  if (!weight || weight.computable !== true) return novalue(weight, "context weight was not measured");
  if (!weight.has_instruction_files) {
    return `<span class="novalue" title="a lesson routed here creates the first instruction file">none yet</span>`;
  }
  // Only the always-loaded number costs every session, so that is the number in
  // the column. The in-force total and the file count sit in the tooltip and in
  // the detail view, which is where a second number belongs.
  const detail =
    `${bytes(weight.always_loaded_bytes)} loads into every session in this repo; ` +
    `${bytes(weight.total_bytes)} is in force across ${weight.file_count} file(s), ` +
    `the rest read on demand`;
  return (
    `<span class="strong" title="${esc(detail)}">${esc(bytes(weight.always_loaded_bytes))}</span>` +
    `<span class="caption muted"> always</span>`
  );
}

export function renderProjectRows(rows, selectedKey) {
  return (rows || [])
    .map((row) => {
      const selected = row.project_key === selectedKey ? ' aria-selected="true"' : "";
      const label = row.label_is_fallback
        ? `<span class="muted mono">${esc(row.label)}</span> <span class="caption">(no display name)</span>`
        : esc(row.label);
      const clones =
        row.clones > 1
          ? `<button class="chip" type="button" data-project-key="${esc(row.project_key)}" data-focus="copies">${num(row.clones)} clones</button>`
          : num(row.clones);
      const signal = row.top_signal
        ? badge(row.top_signal.signal_type, {
            state: "info",
            title:
              `${num(row.top_signal.count)} incidents of this signal` +
              (row.top_signal.tied_with && row.top_signal.tied_with.length
                ? `; tied with ${row.top_signal.tied_with.join(", ")}; ${row.top_signal.tie_break}`
                : `; ${row.top_signal.tie_break}`),
          })
        : novalue(null, "this repo has no incidents");
      return (
        `<tr data-project-key="${esc(row.project_key)}" tabindex="0" role="button"${selected}>` +
        `<td>${label}</td>` +
        `<td>${badge(row.key_method || "unset", { state: keyMethodState(row.key_method), title: keyMethodTitle(row.key_method) })}</td>` +
        `<td class="num">${num(row.sessions)}</td>` +
        `<td class="num">${clones}</td>` +
        `<td class="num">${num(row.incidents)}</td>` +
        `<td class="num">${renderExposureRate(row.exposure)}</td>` +
        `<td>${signal}</td>` +
        `<td class="num">${num(row.rules_written_here)}</td>` +
        `<td>${renderContextWeightCell(row.context_weight)}</td>` +
        `<td class="num">${novalue(row.benefit)}</td>` +
        `</tr>`
      );
    })
    .join("");
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
        "PRD section 8a: context weight is the in-force set, not every markdown file in the repo.",
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
  { id: "exposure", label: "Exposure" },
  { id: "context", label: "Context weight" },
  { id: "topology", label: "Topology" },
  { id: "copies", label: "Working copies" },
  { id: "rules", label: "Rules" },
]);

export function renderProjectInspector(row, tab, exposureEntry = {}) {
  if (!row) return emptyState("No repository selected", "Pick a row from the table.", "");
  const active = PROJECT_TABS.some((t) => t.id === tab) ? tab : "context";
  const weight = row.context_weight || {};
  let body = "";

  if (active === "exposure") {
    body = renderProjectExposure(exposureEntry.data || row.exposure, exposureEntry);
  } else if (active === "context") {
    if (weight.computable !== true) {
      body = section(
        "Context weight",
        emptyState(
          "Not measured",
          weight.reason || "no reason was given",
          "PRD section 8a: only the in-force files count. Total markdown would rank repos by transcript size.",
          { inline: true }
        )
      );
    } else if (!weight.has_instruction_files) {
      body = section(
        "Context weight",
        emptyState(
          "No instruction files at all",
          "An agent working here carries nothing from this repo.",
          "A lesson routed here creates the first file. This is an empty state, not a zero.",
          { inline: true }
        )
      );
    } else {
      const files = (weight.files || [])
        .map(
          (file) =>
            `<tr><td class="mono">${esc(file.path)}</td>` +
            `<td>${badge(file.kind, { state: "" , title: `origin: ${file.origin}`})}</td>` +
            `<td>${file.always_loaded ? badge("every session", { state: "partial", title: "loaded into every session in this repo" }) : badge("on demand", { state: "info", title: "read only when the agent asks for it" })}</td>` +
            `<td class="num">${esc(bytes(file.bytes))}</td></tr>`
        )
        .join("");
      body =
        section(
          "In force here",
          field("Always loaded", `<span class="strong">${esc(bytes(weight.always_loaded_bytes))}</span> <span class="caption muted">approx ${num(weight.always_loaded_approx_tokens)} tokens, every session</span>`) +
            field("Read on demand", `${esc(bytes(weight.on_demand_bytes))}`) +
            field("In force total", `${esc(bytes(weight.total_bytes))} over ${num(weight.file_count)} file(s)`) +
            field(
        "Measured in",
        `<span class="mono">${esc(weight.path || weight.resolved_path || "")}</span>`
        // Show why this working copy was selected for measurement.
        + (row.context_path_reason
            ? `<span class="footnote">${esc(row.context_path_reason)}</span>`
            : "")
      )
        ) +
        section(
          "File by file",
          `<div class="scroll-x"><table class="data"><thead><tr><th scope="col">File</th><th scope="col">Kind</th>` +
            `<th scope="col">When</th><th scope="col" class="num">Bytes</th></tr></thead><tbody>${files}</tbody></table></div>`
        ) +
        section(
          "Markdown that is NOT in force",
          weight.other_md && weight.other_md.scanned
            ? field(
                "Other markdown",
                `${num(weight.other_md.file_count)} file(s), ${esc(bytes(weight.other_md.bytes))}` +
                  ` <span class="caption muted">${esc(weight.other_md.note)}</span>`
              )
            : `<p class="footnote">${esc((weight.other_md || {}).note || "not scanned")}</p>`
        ) +
        renderWeightCaveats(weight);
    }
  } else if (active === "topology") {
    const topology = weight.topology || {};
    body = section(
      "Instruction-file topology",
      topology.label && topology.label !== "unknown"
        ? field("Shape", badge(topology.label, { state: "info", title: "this decides where a proposal may be written" })) +
            field("AGENTS.md", topology.agents_md_exists ? "present" : "absent") +
            field("CLAUDE.md", topology.claude_md_exists ? (topology.claude_md_is_symlink ? "present, a symlink" : topology.claude_md_is_stub ? "present, a thin stub" : "present") : "absent") +
            field("Symlink to AGENTS.md", String(Boolean(topology.claude_md_symlink_to_agents))) +
            field("General write target", `<span class="mono">${esc(topology.write_target_general || EM_DASH)}</span>`) +
            field("Claude-specific target", `<span class="mono">${esc(topology.write_target_claude_specific || EM_DASH)}</span>`)
        : emptyState(
            "Topology unknown",
            topology.error || "no working copy of this repo could be inspected",
            "The topology comes from routing.detect_topology, the same detector the writer uses.",
            { inline: true }
          )
    );
  } else if (active === "copies") {
    const paths = row.clone_paths || [];
    body =
      section(
        "Working copies",
        `<p class="field__value">${num(row.clones)} working copies of one repository, ${num(row.clones_on_disk)} still on disk. ` +
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

  return renderTabs(PROJECT_TABS, active) + body;
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
  overview: null,
  rules: null,
  projects: null,
  route: "overview",
  query: "",
  numbers: false,
  selectedRule: "",
  selectedProject: "",
  inspectorKind: "",
  inspectorTab: "",
  miningHistories: {},
  scanHistories: {},
  projectExposures: {},
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
      const cut = clamp(ex.matched_text || "", 240);
      return (
        `<li class="review-incident">` +
        `<span class="review-incident__meta">${esc(String(ex.ts).slice(0, 19))} UTC ` +
        `&middot; ${esc(ex.signal_type || "")} &middot; ` +
        `<code>${esc(ex.project_path || "")}</code></span>` +
        `<pre class="review-incident__text">${esc(cut.text)}</pre>` +
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
    `<details class="review-disclose"><summary>What actually happened ` +
    `(${num(examples.length)} of ${num(prov.incident_count || examples.length)})</summary>` +
    `<ul class="review-incidents">${items}</ul>${note}</details>`
  );
}

/** The complete diff stays available in the product, behind a disclosure. */
export function renderReviewDiff(diff, key = "", label = "Complete proposed edit") {
  if (!diff) return "";
  return (
    `<details class="review-disclose" ${reviewDisclosure(key)}><summary>${esc(label)}</summary>` +
    `<pre class="review-card__diff">${esc(diff)}</pre></details>`
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
  return JSON.stringify(proposals.map((p) => [p.id, p.revision]).sort((a, b) => a[0].localeCompare(b[0])));
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
      const actual = data.members.map((m) => [m.proposal_id, m.revision]).sort((a, b) => a[0].localeCompare(b[0]));
      if (JSON.stringify(actual) !== signature) throw new Error("A proposal changed. Reload Review before deciding.");
      entry.data = data;
      data.members.forEach((m) => { state.reviewMembers[m.proposal_id] = m; });
    } catch (error) {
      entry.error = String(error && error.message ? error.message : error);
    } finally {
      entry.loading = false;
      entry.promise = null;
      if (state.route === "review" && state.selectedFamily === family.learning_id) paintReview();
    }
    return entry;
  })();
  return entry.promise;
}

function fullRecord(value) {
  return `<pre class="review-full-record">${esc(JSON.stringify(value, null, 2))}</pre>`;
}

export function renderRetainedEvidence(incident, prefix = "") {
  let archive, error = "";
  try {
    archive = JSON.parse(incident.window_json);
    if (!Array.isArray(archive)) throw new Error("The retained context is not a list.");
  } catch (exc) { error = String(exc.message || exc); }
  const fingerprint = /^[a-f0-9]{40}$/i.test(incident.matched_text || "");
  const text = fingerprint ? `Error fingerprint: ${incident.matched_text}` : incident.matched_text;
  const context = error
    ? `<p class="error-state">Retained context is unreadable: ${esc(error)}</p>${fullRecord(incident.window_json)}`
    : archive.length ? archive.map((entry) => {
      if (!entry || typeof entry !== "object") return fullRecord(entry);
      const label = entry.role || ("count_in_session" in entry ? `${entry.count_in_session} occurrences in this session` : "Retained event");
      return `<section class="review-evidence-entry"><p class="caption strong">${esc(label)}</p>` +
        (typeof entry.text === "string" ? `<pre class="review-full-record">${esc(entry.text)}</pre>` : "") +
        `<details><summary>Full retained record</summary>${fullRecord(entry)}</details></section>`;
    }).join("") : `<p class="muted">No context was retained for this incident.</p>`;
  return `<details class="review-disclose" ${reviewDisclosure(prefix + incident.id)}><summary>${esc(incident.signal_type)} · ${esc(incident.ts || "Time unknown")} · ${esc(incident.session_id || "Session unknown")}</summary>` +
    `<p class="caption">Incident ${esc(incident.id)} · <code>${esc(incident.project_path || "Project path unknown")}</code></p>` +
    (text ? `<pre class="review-full-record">${esc(text)}</pre>` : "") + context + renderScanHistory(incident.id, "review-" + prefix) + `</details>`;
}

export function renderReviewMembers(family) {
  if (!state.reviewIndividual[family.learning_id]) return "";
  return `<section class="review-member-list" aria-label="Individual proposals">` + (family.proposals || []).map((p, i) => {
    const saved = state.reviewMembers[p.id];
    const snapshot = saved && saved.revision === p.revision ? saved.snapshot : null;
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

export function renderSelectedPreview(family) {
  const entry = state.reviewPreviews[family.learning_id];
  if (!entry || entry.loading) return `<p class="caption muted" role="status">Reading the selected edits and destinations…</p>`;
  if (entry.error) return `<p class="error-state">${esc(entry.error)}</p><button class="btn" data-reload-review="true">Reload Review</button>`;
  return `<section class="review-selected-preview" aria-label="Selected edit preview"><h4>${entry.data.members.length} selected ${entry.data.members.length === 1 ? 'proposal' : 'proposals'} · ${entry.data.targets.length} ${entry.data.targets.length === 1 ? 'destination' : 'destinations'}</h4>` +
    entry.data.targets.map((target) => {
      const dest = target.destination;
      const budget = target.budget;
      const destination = dest.mode === "git_branch"
        ? `Branch ${dest.branch_name} · ${dest.repo_root} · ${dest.relative_path}. Integration into your working copy is a separate step.`
        : `Direct file delivery.`;
      return `<details class="review-targets" ${reviewDisclosure(family.learning_id + ':' + target.target_key, true)}><summary class="review-targets__head">${esc(dest.target_path)} · ${target.proposal_ids.length} proposal${target.proposal_ids.length === 1 ? "" : "s"} · collapse / expand</summary>` +
        `<p class="caption">${esc(destination)}</p>` +
        (target.state === "ready" ? `<details class="review-disclose"><summary>Complete combined edit</summary><pre class="review-card__diff">${esc(target.diff_unified)}</pre></details>`
          : `<p class="error-state">${esc(target.detail)}</p><p class="caption">Review individually to select one existing alternative, or <a class="review-link" href="${esc(recoveryLink(family.learning_id,dest.target_kind === "hook" ? "hook" : "regenerate_patch",target.target_key,target.proposal_ids))}">${dest.target_kind === "hook" ? "generate this hook proposal…" : "regenerate these selected patches…"}</a></p>`) +
        (budget ? `<p class="review-budget${budget.over ? " review-budget--over" : ""}">${budget.before} lines now → ${budget.after} after this selection · ${budget.limit}-line budget.` +
          (budget.over ? ` Above the budget; approval remains available.` : ``) + `</p>` : ``) + `</details>`;
    }).join("") + `</section>`;
}

/** Buttons, labelled with what they DO rather than with a verb. */
export function renderReviewActions(family) {
  const id = esc(family.learning_id);
  const size = (family.proposals || []).length;
  const files = (family.target_rows || []).length;
  const approve =
    size > 1 && files >= 1
      ? `Approve &mdash; write once per file`
      : `Approve`;
  const selected = selectedReviewProposals(family).length;
  const entry = state.reviewPreviews[family.learning_id];
  const ready = entry && !entry.loading && entry.data && entry.data.ready && entry.signature === reviewSignature(selectedReviewProposals(family));
  return (
    `<div class="review-card__actions">` +
    `<button type="button" class="btn btn--approve" data-decision="approve" ` +
    `data-learning-id="${id}" ${ready ? "" : "disabled"}>${selected === size ? approve : `Approve selected (${selected})`}</button>` +
    `<button type="button" class="btn" data-review-individual="${id}" aria-expanded="${Boolean(state.reviewIndividual[family.learning_id])}">` +
    `${state.reviewIndividual[family.learning_id] ? "Close individual review" : `Review the ${size} individually`}</button>` +
    `<button type="button" class="btn btn--reject" data-decision="reject" ` +
    `data-learning-id="${id}" ${entry && !entry.loading && entry.data && !entry.error && selected ? "" : "disabled"}>Reject at selected targets</button>` +
    `<button type="button" class="btn btn--reject" data-decision="reject_lesson" ` +
    `data-learning-id="${id}" ${entry && !entry.loading && entry.data && !entry.error && selected ? "" : "disabled"}>Reject lesson everywhere</button>` +
    `</div>` +
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
    `<div class="review-breakdown"><p class="caption muted">${esc(why)}</p>` +
    `<ul class="review-card__proposals">${rows}</ul></div>`
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
    return emptyState("Nothing needs you", esc(data.empty_state || ""), "");
  }
  const opts = options || {};
  const selectedId = opts.selectedId || families[0].learning_id;
  const open = families.find((f) => f.learning_id === selectedId) || families[0];
  const rest = families.filter((f) => f.learning_id !== open.learning_id);
  const nextUp = rest.length
    ? `<section class="review-next"><h3>Next up</h3>` +
      `<p class="caption muted">${num(rest.length)} more ` +
      `${rest.length === 1 ? "decision" : "decisions"}. Press ` +
      `<kbd>j</kbd> / <kbd>k</kbd> to move, or click one.</p>` +
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
  const card = d.querySelector(".review-card--selected");
  if (!card) return false;
  if (typeof card.scrollIntoView === "function") {
    card.scrollIntoView({ block: "nearest" });
  }
  // preventScroll: scrollIntoView above already chose the position; letting
  // focus() scroll again undoes "nearest" and centres the card.
  if (typeof card.focus === "function") card.focus({ preventScroll: true });
  return true;
}

/**
 * j/k/a/r. PRD S7 V3 calls this "the one repetitive task in the product", so
 * it is keyboard-first rather than keyboard-optional.
 *
 * Returns the key it acted on, or "" — a real return value so a test can tell
 * "handled" from "ignored" instead of inferring it from a side effect.
 */
export function handleReviewKeydown(event) {
  if (event.target && event.target.closest && event.target.closest("#review-delivery")) return null;
  if (event.target && event.target.closest && (event.target.closest("#review-eval") || event.target.closest("#review-incidents"))) return null;
  if (event.target && event.target.closest && (event.target.closest("#review-operations") || event.target.closest("#review-rollback"))) return null;
  if (state.route !== "review") return "";
  const target = event && event.target;
  const tag = target && target.tagName ? String(target.tagName).toLowerCase() : "";
  // Never steal a key from a text field. `a` and `r` are letters people type.
  if (tag === "input" || tag === "textarea" || tag === "select") return "";
  const key = event && event.key;
  if (["j", "k", "a", "r"].indexOf(key) === -1) return "";
  const order = reviewOrder();
  if (order.length === 0) return "";
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
    if (restored && restored.focus) restored.focus({ preventScroll: true });
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
      if (older || !delivery.olderLoaded) delivery.nextCursor = page.next_cursor;
      if (older) delivery.olderLoaded = true;
      delivery.loaded = true;
      delivery.error = "";
      const changed = previous !== JSON.stringify(delivery.items.map((c) => [c.id, c.state, c.cancel_requested]));
      if (miningChanged) {
        await Promise.all([load(),loadMiningIncidents()]);
      } else if (wasLoaded && changed) {
        state.review = await getJSON(API.review);
        state.decided = {};
        paintReview();
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
  const activeId = active && active.id;
  const card = active && active.closest ? active.closest(".delivery-command") : null;
  if (body.querySelectorAll) body.querySelectorAll("details[data-operation-key]").forEach((el) => { state.operationDisclosures[el.getAttribute("data-operation-key")] = el.open; });
  if (body.querySelectorAll) body.querySelectorAll("details[data-review-key]").forEach((el) => { state.reviewDisclosures[el.getAttribute("data-review-key")] = el.open; });
  // Render after reading disclosure state so polling does not close inspected evidence.
  const rendered = typeof html === "function" ? html() : html;
  if (body.innerHTML !== rendered) body.innerHTML = rendered;
    if (activeId && (activeId.startsWith("operation-") || activeId.startsWith("rollback-") || activeId.startsWith("eval-job-") || activeId.startsWith("mining-history-") || activeId.startsWith("scan-history-") || activeId.startsWith("run-"))) {
    const candidate = doc().getElementById(activeId);
    const runStatus = /^run-(older|load)-/.test(activeId) ? doc().getElementById(activeId.replace(/^run-(older|load)-/, "run-status-")) : null;
    const restored = candidate && !candidate.disabled ? candidate : runStatus || card && doc().getElementById(card.id);
    if (restored && restored.focus) restored.focus({ preventScroll: true });
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
    `<details ${reviewDisclosure("rollback-source:" + rollback.proposalId)}><summary>Applied revision and affected proposals</summary><p class="id">${esc(source.application_id)}</p>` + source.affected_members.map((m) => `<p class="id">${esc(m.proposal_id)}</p>`).join("") + `</details>` +
    (preview.diff_unified ? `<details class="review-disclose" ${reviewDisclosure("rollback:" + rollback.proposalId + ":" + preview.revision, true)}><summary>Full inverse change</summary><pre class="review-card__diff">${esc(preview.diff_unified)}</pre></details>` : "") +
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
      `<p class="mono">${esc(summary.incident.matched_text)}</p>` +
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
        `<p>${t.available ? `<a class="review-link" href="${esc(recoveryLink(job.selection.learning_id,job.selection.mode,t.id,job.selection.proposal_ids))}">${esc(t.label)}</a>` : `<strong>${esc(t.label)}</strong> · ${esc(t.reason.detail)}`}<br><code>${esc(t.destination.target_path)}</code></p>`).join("") +
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
  const projectQuery = raw.startsWith("/projects/") && raw.includes("?") ? raw.slice(raw.indexOf("?") + 1) : "";
  const ruleQuery = raw.startsWith("/rules/") && raw.includes("?") ? raw.slice(raw.indexOf("?") + 1) : "";
  const path = projectQuery || ruleQuery ? raw.slice(0, raw.indexOf("?")) : raw;
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
  return { view, id, unknown: "", ...(projectQuery ? {projectQuery} : {}), ...(ruleQuery ? {ruleQuery} : {}) };
}

export function showRoute(route) {
  state.route = route.view;
  const runRoute = route.view === "overview" && /^(run|night)\//.test(route.id);
  setVisible(byId("run-detail"), runRoute);
  if (runRoute) openRunRoute(route.id);
  else state.runDetail = null;
  VIEWS.forEach((view) => {
    setVisible(byId(`view-${view}`), view === route.view && !runRoute);
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
  if (route.view === "rules" && route.id) {
    const params = new URLSearchParams(route.ruleQuery || "");
    openRule(route.id, params.get("tab") || undefined);
    const incident = params.get("scan");
    if (incident && state.inspectorTab === "evidence" && Array.from(doc().querySelectorAll('[data-scan-history-root]')).some(root => root.getAttribute('data-scan-history-root') === incident)) loadIncidentScanHistory(incident);
  } else if (route.view === "projects" && route.id) {
    const params = new URLSearchParams(route.projectQuery || "");
    const tab = params.get("tab") || "context"; params.delete("tab");
    openProject(route.id, tab, params.toString());
  } else {
    closeInspector();
  }
  if (route.view === "review") {
    state.reviewFocusProposal = route.id.startsWith("proposal/") ? route.id.slice("proposal/".length) : "";
    paintReview();
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
}

/* Run detail is a child of Overview. An aggregate grid cell never picks a run
 * by timestamp: a night with several runs first offers every distinct ID. */
export function runHref(kind, id, stage = "") {
  return "#/overview/" + kind + "/" + encodeURIComponent(id) + (stage ? "/" + encodeURIComponent(stage) : "");
}

const RUN_RECORD_LABELS = Object.freeze({calls: "Model calls", evaluations: "Linked evaluations", deliveries: "Applied edits", proposals: "Created proposals", scans: "Detector observations"});

function runDisclosure(key, label, value) {
  return `<details ${reviewDisclosure(key)}><summary>${esc(label)}</summary>${fullRecord(value)}</details>`;
}

export function renderRunRecords(kind, entry) {
  const page = entry.pages[kind];
  if (!page) return `<button class="btn" id="run-load-${esc(kind)}" type="button" data-run-records="${esc(kind)}">Load ${esc(RUN_RECORD_LABELS[kind].toLowerCase())}</button>`;
  let html = page.reason ? `<p class="footnote">${esc(page.reason)}</p>` : "";
  if (page.error) html += `<p class="error-state" role="alert">${esc(page.error)}</p>`;
  html += `<p class="caption" id="run-status-${esc(kind)}" tabindex="-1" aria-live="polite">${page.loaded ? `${num(page.records.length)}${page.count == null ? "" : ` of ${num(page.count)}`} retained records shown` : "Loading records…"}</p>`;
  page.records.forEach((record) => {
    if (kind === "scans") { html += renderScanObservation(record, "run-scan:" + entry.id + ":" + record.id); return; }
    let label = record.id;
    if (kind === "calls") label = `${record.stage} · ${record.outcome} · ${record.model_reported || "model unknown"} · ${record.id}`;
    if (kind === "evaluations") label = `${record.verdict || "verdict not recorded"} · ${record.id}`;
    if (kind === "proposals") label = `${record.status} · ${record.id}`;
    if (kind === "deliveries") label = `${record.state} · ${record.id}`;
    html += `<article class="run-record">`;
    if (record.association) html += `<p class="footnote">${esc(record.association)}</p>`;
    if (kind === "deliveries") {
      html += `<p class="mono">${esc(record.record.destination.target_path)}</p><pre class="review-card__diff">${esc(record.record.proposal.diff_unified)}</pre>`;
      html += `<p class="footnote">Snapshot before: <span class="mono">${esc(record.result.snapshot_commit_before)}</span><br>Snapshot after: <span class="mono">${esc(record.result.snapshot_commit_after)}</span></p>`;
    }
    html += runDisclosure("run:" + entry.id + ":" + kind + ":" + record.id, label, record);
    if (kind === "proposals") html += `<a href="#/review/proposal/${encodeURIComponent(record.id)}">Inspect proposal in Review</a>`;
    if (kind === "deliveries" && record.proposal_id) html += `<a href="#/review/rollback/${encodeURIComponent(record.proposal_id)}">Inspect rollback eligibility</a>`;
    html += `</article>`;
  });
  if (page.loaded && !page.records.length && !page.reason) html += `<p>${kind === "deliveries" ? "No automatic edits were recorded for this run." : "No records linked to this run."}</p>`;
  if (page.loading) html += `<p role="status">Loading records…</p>`;
  if (page.next_cursor || page.error) html += `<button class="btn" id="run-older-${esc(kind)}" type="button" data-run-records="${esc(kind)}" data-run-older="${page.loaded ? "true" : "false"}" aria-disabled="${page.loading}">${page.error ? "Retry" : "Load more"}</button>`;
  return html;
}

export function renderRunDetail(entry) {
  let html = `<nav class="run-breadcrumb" aria-label="Breadcrumb"><a href="#/overview">Overview</a>`;
  if (entry.data) html += ` / <a href="${esc(runHref("night", entry.data.run.started.slice(0, 10), entry.stage))}">${esc(entry.data.run.started.slice(0, 10))} UTC</a>`;
  html += ` / ${entry.kind === "night" ? "Choose a run" : "Run detail"}</nav>`;
  if (entry.loading) return html + `<p role="status">Loading run…</p>`;
  if (entry.error) return html + `<p class="error-state" role="alert">${esc(entry.error)}</p><button class="btn" data-run-refresh="true">Retry</button>`;
  if (entry.kind === "night") {
    html += `<h1 class="view__title" id="run-title" tabindex="-1">Runs on ${esc(entry.id)} UTC</h1><p>Select the exact run. Times can be identical; IDs are distinct.</p><ul class="run-choices">`;
    entry.night.records.forEach((run) => { html += `<li><a href="${esc(runHref("run", run.id, entry.stage))}">${esc(run.started)} · ${esc(run.status)} · <span class="mono">${esc(run.id)}</span></a></li>`; });
    return html + `</ul>` + (entry.night.count ? "" : `<p>No run was recorded for this UTC day.</p>`);
  }
  const data = entry.data, run = data.run;
  const elapsed = run.finished ? Date.parse(run.finished) - Date.parse(run.started) : NaN;
  const duration = Number.isFinite(elapsed) && elapsed >= 0 ? `${num(Math.round(elapsed / 1000))} seconds wall time` : "duration not recorded";
  html += `<header class="view__head run-heading"><div><h1 class="view__title" id="run-title" tabindex="-1">Run · ${esc(run.started.slice(0, 10))}</h1>`;
  html += `<p class="mono">${esc(run.id)}</p><p class="view__desc">${esc(run.started)} → ${esc(run.finished || "completion not recorded")} · ${esc(duration)}</p>`;
  html += `<p class="view__desc">Status: ${esc(run.status)}${data.stats.review_only === true ? " · review only" : ""}${data.stats.dry_run === true ? " · dry run" : ""}</p></div>`;
  html += `<div class="run-heading__meta"><label for="run-selector">Run on this UTC day</label><select id="run-selector">`;
  data.night_runs.forEach((other) => { html += `<option value="${esc(other.id)}"${other.id === run.id ? " selected" : ""}>${esc(other.started)} · ${esc(other.status)} · ${esc(other.id)}</option>`; });
  html += `</select><span>Tokens: ${num(data.calls.tokens_in)} in / ${num(data.calls.tokens_out)} out</span><button class="btn btn--quiet" id="run-refresh" data-run-refresh="true">Refresh run</button></div></header>`;
  if (data.calls.reason) html += `<p class="run-notice">${esc(data.calls.reason)} Retained: ${num(data.calls.recorded)}; reported: ${data.calls.reported === null ? "unknown" : num(data.calls.reported)}.</p>`;
  html += `<section class="panel"><header class="panel__head"><h2 class="panel__title">Pipeline stages</h2><span class="panel__meta">Recorded outcomes and causes</span></header><div class="scroll-x"><table class="run-stage-table"><thead><tr><th scope="col">Stage</th><th scope="col">Recorded counters</th><th scope="col">What happened</th></tr></thead><tbody>`;
  data.stages.forEach((stage) => {
    const payload = stage.payload || {};
    const counters = Object.entries(payload).filter(([, value]) => typeof value === "number").map(([name, value]) => `${name.replaceAll("_", " ")}: ${num(value)}`);
    const causes = Object.entries(payload.taxonomy || {}).map(([cause, count]) => `${cause}: ${count}`).join(" · ");
    html += `<tr id="run-stage-${esc(stage.name)}" tabindex="-1"${entry.stage === stage.name ? ' aria-selected="true"' : ""}><th scope="row">${esc(stage.name)}</th><td>${stage.recorded ? esc(counters.join(" · ") || "No numeric counters recorded") : "Not recorded"}</td><td>`;
    if (causes) html += `<p>${esc(causes)}</p>`;
    html += stage.recorded ? runDisclosure("run:" + run.id + ":stage:" + stage.name, "Complete stage record", stage.payload) : "No stage measurement was retained; this is not a zero result.";
    html += `</td></tr>`;
  });
  html += `</tbody></table></div></section>`;
  html += renderScanSummary(data.scan_summary);
  const llm = data.stats.llm || {}, limits = data.budget_limits;
  html += `<section class="run-notice"><h2 class="section__title">Call budgets and caps</h2>`;
  if (limits === null) html += `<p>Budget limits were not recorded for this run.</p>`;
  else Object.entries(limits).forEach(([pool, limit]) => { html += `<p>${esc(pool)}: ${llm.calls_made && pool in llm.calls_made ? num(llm.calls_made[pool]) : "usage unknown"} / ${num(limit)} calls</p>`; });
  html += runDisclosure("run:" + run.id + ":budgets", "Recorded usage, refusals, and waits", llm) + `</section>`;
  Object.entries(RUN_RECORD_LABELS).forEach(([kind, label]) => {
    html += `<section class="panel"><header class="panel__head"><h2 class="panel__title">${esc(label)}</h2></header><div class="panel__body">${renderRunRecords(kind, entry)}</div></section>`;
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
  } catch (error) { entry.error = String(error.message || error); }
  finally {
    entry.loading = false;
    if (state.runDetail === entry) {
      paintRunDetail();
      const focus = doc().getElementById(stage && entry.data ? "run-stage-" + stage : "run-title");
      if (focus && focus.focus) focus.focus();
    }
  }
}

export async function loadRunRecords(kind, older = false) {
  const entry = state.runDetail;
  if (!entry || !entry.data || !(kind in RUN_RECORD_LABELS)) return;
  const page = entry.pages[kind] || {records: [], loaded: false, count: 0, reason: "", next_cursor: null, error: "", loading: false};
  if (page.loading) return;
  page.loading = true; page.error = ""; entry.pages[kind] = page;
  paintRunDetail();
  try {
    const url = API.runs + "/" + encodeURIComponent(entry.id) + "/records?kind=" + kind + "&limit=20" + (older && page.next_cursor ? "&cursor=" + encodeURIComponent(page.next_cursor) : "");
    const data = await getJSON(url);
    if (state.runDetail !== entry) return;
    if (data.run_id !== entry.id || data.kind !== kind) throw new Error("The records identify a different run or record kind.");
    Object.assign(page, data, {records: older ? [...page.records, ...data.records] : data.records, loaded: true});
  } catch (error) { page.error = String(error.message || error); }
  finally { page.loading = false; if (state.runDetail === entry) paintRunDetail(); }
}

/* ------------------------------ inspector -------------------------------- */

export function renderMiningHistory(row) {
  const history=state.miningHistories[row.id] || {records:[],loading:false,loaded:false,error:"",nextCursor:null};
  return `<p>Content generation and later evidence are separate events. Incident time is shown inside each retained record.</p>` +
    `<button id="mining-history-load-${esc(row.id)}" class="btn" data-mining-history="${esc(row.id)}" ${history.loading ? "disabled" : ""}>${history.loading ? "Loading mining history…" : history.loaded ? "Refresh mining history" : "Load mining history"}</button>` +
    (history.error ? `<p role="alert">${esc(history.error)}</p>` : "") +
    (history.loaded && !history.records.length ? `<p>No mining history was recorded for this learning. Its historical generation remains unknown.</p>` : "") +
    history.records.map(r=>`<article class="delivery-command"><h4>${esc(r.kind === "new" ? "Created content" : r.kind === "cluster_merge" ? "Merged content" : r.kind.startsWith("amend") ? "Amended content" : "Linked evidence")}</h4>` +
      `<p>${esc(r.generation || "Generation not recorded")} · ${esc(r.created_at)}</p>` +
      `<p>Run: <span class="id">${esc(r.run_id || "not recorded")}</span><br>Reported model: ${esc(r.call && r.call.model_reported || "not recorded")}</p>` +
      `<details ${reviewDisclosure("mining-history:"+r.id)}><summary>Complete content, source evidence, and call record</summary>${fullRecord(r)}</details></article>`).join("") +
    (history.nextCursor ? `<button id="mining-history-older-${esc(row.id)}" class="btn" data-mining-history="${esc(row.id)}" data-mining-history-older="true" ${history.loading ? "disabled" : ""}>Load older mining history</button>` : "");
}

export async function loadRuleMiningHistory(learningId,{older=false}={}) {
  const old=state.miningHistories[learningId];if(old && old.loading)return;
  const focusId=doc().activeElement && doc().activeElement.id;
  const history=old || {records:[],loaded:false,nextCursor:null,error:""};
  if(older && !history.nextCursor)return;
  history.loading=true;history.error="";state.miningHistories[learningId]=history;
  const paint=()=>{
    const row=((state.rules || {}).rows || []).find(r=>r.id===learningId);
    if(row && state.inspectorKind==="rule" && state.selectedRule===learningId && state.inspectorTab==="provenance")preserveOperationView("inspector-body",()=>renderRuleInspector(row,"provenance"));
  };
  paint();
  try {
    const page=await getJSON(`/api/learnings/${encodeURIComponent(learningId)}/mining-history?limit=20`+(older ? `&cursor=${encodeURIComponent(history.nextCursor)}` : ""));
    const records=older ? [...history.records,...page.records] : page.records;
    history.records=[...new Map(records.map(r=>[r.id,r])).values()];history.nextCursor=page.next_cursor;history.loaded=true;
  } catch(error){history.error=String(error.message || error);}
  finally{
    history.loading=false;paint();
    if(focusId && focusId.startsWith("mining-history-") && state.inspectorKind==="rule" && state.selectedRule===learningId && state.inspectorTab==="provenance"){
      const candidate=doc().getElementById(focusId) || doc().getElementById("mining-history-load-"+learningId);
      if(candidate && candidate.focus)candidate.focus({preventScroll:true});
    }
  }
}

export function closeInspector() {
  const inspector = byId("inspector");
  inspector.className = "";
  setVisible(inspector, false);
  byId("workspace").classList.remove("workspace--split");
  state.inspectorKind = "";
  state.selectedRule = "";
  state.selectedProject = "";
  paintRuleRows();
  paintProjectRows();
}

function openInspector(title, html) {
  const inspector = byId("inspector");
  inspector.className = "inspector";
  setVisible(inspector, true);
  byId("workspace").classList.add("workspace--split");
  setText("inspector-title", title);
  setHTML("inspector-body", html);
}

export function openRule(ruleId, tab) {
  const rules = (state.rules && state.rules.rows) || [];
  const row = rules.find((r) => r.id === ruleId);
  state.inspectorKind = "rule";
  state.inspectorTab = tab || "why";
  state.selectedRule = ruleId;
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
      const row = state.projects?.rows.find(row => row.project_key === projectKey);
      if (row) preserveOperationView("inspector-body", () => renderProjectInspector(row, "exposure", entry));
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

export function openProject(projectKey, tab, exposureParams = "") {
  const rows = (state.projects && state.projects.rows) || [];
  const row = rows.find((r) => r.project_key === projectKey);
  state.inspectorKind = "project";
  state.inspectorTab = tab || "context";
  state.selectedProject = projectKey;
  state.projectExposureParams = exposureParams;
  if (!row) {
    openInspector(
      state.projects ? "Repository not found" : "Loading",
      state.projects
        ? emptyState("No such repository", `No repo has key ${projectKey}.`, "The link may be older than the current sessions table.")
        : `<div class="loading"><span class="loading__bar"></span><span class="loading__bar"></span></div>`
    );
    return;
  }
  const entry = state.projectExposures[projectExposureUrl(projectKey, exposureParams)] || {};
  openInspector(row.label || row.project_key, renderProjectInspector(row, state.inspectorTab, entry));
  if (state.inspectorTab === "exposure") loadProjectExposure(projectKey, exposureParams);
  paintProjectRows();
}

/* ------------------------------ painting --------------------------------- */

export function paintOverview() {
  const data = state.overview;
  if (!data) return;
  const banner = renderBanner(data.freshness);
  const bannerElement = byId("ov-banner");
  bannerElement.innerHTML = banner.html;
  if (banner.state) bannerElement.setAttribute("data-state", banner.state);
  else bannerElement.removeAttribute("data-state");
  setVisible(bannerElement, Boolean(banner.show));

  setText("ov-statusline", (data.status_line || {}).text || "");
  setHTML("ov-statusline-sub", renderStatusSub(data.status_line, data.freshness));
  setHTML("ov-tiles", renderTiles(data));

  const grid = data.grid || {};
  setHTML("ov-grid", renderGrid(grid, { numbers: state.numbers }));
  setHTML("ov-grid-legend", renderGridLegend(grid));
  setHTML("ov-grid-foot", renderGridFoot(grid));
  setText(
    "ov-grid-meta",
    `${num(grid.runs_total)} runs · ${(grid.nights || []).length} nights drawn · times are UTC`
  );

  setHTML("ov-backlog", renderBacklog(data.backlog));
  setHTML("ov-backlog-foot", renderBacklogFoot(data.backlog));
  setHTML("ov-failures", renderFailures(data.failures));
  setText("ov-failures-meta", `${((data.failures || {}).open || []).length} still happening`);
  setHTML("ov-gate", renderGate(data.gate));
  setHTML("ov-inbox", renderInbox(data.inbox));
  setText("ov-inbox-meta", `${num((data.inbox || {}).count)} waiting`);
  setHTML("ov-confidence", renderConfidence(data.confidence));

  const inbox = data.inbox || {};
  // Review refreshes after every decision. An older Overview response must
  // not put its pre-decision count back into the shared navigation.
  paintInboxCount(state.review || inbox);
  setText("nav-freshness", (data.freshness || {}).banner || "");
}

export function paintRules() {
  const data = state.rules;
  if (!data) return;
  setText("rules-meta", `${num(data.count)} rules · evidence sample capped at ${num(data.evidence_sample)} per rule`);
  setHTML("rules-grouping", renderGrouping(data.grouping));
  paintRuleRows();
}

/** Re-paint just the result rows. Called on every keystroke, so it must be cheap. */
export function paintRuleRows() {
  const data = state.rules;
  if (!data) return;
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
  if (!data) return;
  setText(
    "projects-meta",
    `${num(data.count)} repos · ${num(data.clone_paths_total)} working copies`
  );
  setHTML("projects-foot", renderProjectsFoot(data));
  setHTML("projects-notes", renderProjectNotes(data));
  paintProjectRows();
}

export function paintProjectRows() {
  const data = state.projects;
  if (!data) return;
  setHTML("projects-tbody", renderProjectRows(data.rows, state.selectedProject));
}

/* ------------------------------ loading ---------------------------------- */

export function showError(title, detail) {
  const element = byId("global-error");
  element.innerHTML =
    `<strong>${esc(title)}</strong><span class="error-state__detail">${esc(detail)}</span>` +
    `<span class="error-state__detail">Reload the dashboard to request current data.</span>`;
  setVisible(element, true);
}

export function clearError() {
  const element = byId("global-error");
  element.innerHTML = "";
  setVisible(element, false);
}

export function paintReview() {
  const document = doc();
  const activeId = document && document.activeElement && document.activeElement.id;
  const body = byId('review-body');
  if (body.querySelectorAll) body.querySelectorAll('details[data-review-key]').forEach((el) => {
    state.reviewDisclosures[el.getAttribute('data-review-key')] = el.open;
  });
  const data = state.review;
  let reveal = false;
  const linked=data && (data.families || []).find(f=>(f.proposals || []).some(p=>p.id===state.reviewFocusProposal));
  if (linked) {
    state.selectedFamily=linked.learning_id;state.reviewIndividual[linked.learning_id]=true;
    linked.proposals.forEach(p=>{state.reviewExcluded[p.id]=p.id!==state.reviewFocusProposal;});
    state.reviewFocusProposal="";reveal=true;
  }

  // Keep the selected identifier aligned with the card rendered on screen.
  // Otherwise the first navigation key can reselect the already open card.
  if (data && data.families && data.families.length) {
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
  setHTML("review-body", renderReviewQueue(data, { selectedId: state.selectedFamily, decided: state.decided }));
  setText("review-note", renderReviewNote(data));
  const count = data && typeof data.count === "number" ? num(data.count) : "\u2014";
  setText("review-count", count);
  if (data) paintInboxCount(data);
  if (reveal) revealSelectedCard();
  if (activeId && activeId.startsWith('review-include-')) {
    const selected = document.getElementById(activeId);
    if (selected && selected.focus) selected.focus({ preventScroll: true });
  }
}

function paintInboxCount(data) {
  const count = data && typeof data.count === "number" ? data.count : undefined;
  setText("nav-inbox-count", count === undefined ? EM_DASH : num(count));
  byId("nav-inbox-count").setAttribute("data-state", count === undefined ? "unknown" : count > 0 ? "partial" : "ok");
}

/** Shared POST transport for explicit commands and compatibility decisions. */
export async function postJSON(url, payload) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json", accept: "application/json" },
    body: JSON.stringify(payload),
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || `${url} answered ${response.status}`);
  return body;
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
  state.deciding[learningId] = true;
  let ok = 0;
  const failed = [];
  const subjects = selectedReviewProposals(family);
  const action = decision === "reject" ? "reject_target" : decision;
  if (["approve", "reject_target", "reject_lesson"].includes(action)) {
    try {
      const entry = await loadReviewPreview(family);
      if (!entry.data || entry.error || (action === "approve" && !entry.data.ready)) throw new Error(entry.error || "Resolve the selected edit conflicts before approving.");
      const members = entry.data.members.map((m) => ({ proposal_id: m.proposal_id, revision: m.revision }));
      const signature = JSON.stringify({action, members, preview: action === "approve" ? entry.data.revision : ""});
      let request = state.commandRequests[learningId];
      if (!request || request.signature !== signature) {
        request = { signature: signature, key: crypto.randomUUID() };
        state.commandRequests[learningId] = request;
      }
      state.lastCommand = await postJSON(API.commands, {
        request_key: request.key, action, members, ...(action === "approve" ? {preview_revision: entry.data.revision} : {}), note: "",
      });
      // Keep uncertain requests replayable; a confirmed action has ended.
      // Cancellation may return this exact revision for a new approval.
      delete state.commandRequests[learningId];
      members.forEach((m) => { state.decided[m.proposal_id] = action === "approve" ? "approved" : "rejected"; });
      ok = members.length;
    } catch (error) {
      const message = String(error && error.message ? error.message : error);
      subjects.forEach((p) => failed.push({ id: p.id, message: message }));
    }
  } else {
    subjects.forEach((p) => failed.push({id:p.id, message:"Unknown review decision."}));
  }
  if (failed.length) {
    showError(
      `${failed.length} of ${subjects.length} decision results could not be confirmed`,
      failed.map((f) => `${f.id}: ${f.message}`).join(" | ")
    );
  }
  try {
    state.review = await getJSON(API.review);
  } catch (error) {
    showError("The decision result could not be refreshed", String(error && error.message ? error.message : error));
  } finally {
    delete state.deciding[learningId];
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
    throw new Error(`GET ${url} answered ${response.status} ${detail.error || ""} ${detail.detail || response.statusText || ""}`.trim());
  }
  return response.json();
}

export async function load() {
  const wanted = [
    { key: "overview", url: API.overview, paint: paintOverview },
    { key: "rules", url: API.rules, paint: paintRules },
    { key: "projects", url: API.projects, paint: paintProjects },
    { key: "review", url: API.review, paint: paintReview },
  ];
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
    if (result.error) return;
    state[result.entry.key] = result.data;
    result.entry.paint();
  });
  if (failures.length) {
    showError(
      `Could not read ${failures.length} of ${wanted.length} endpoints`,
      failures.map((f) => String(f.error && f.error.message ? f.error.message : f.error)).join(" | ")
    );
  } else {
    clearError();
  }
  return { loaded: results.length - failures.length, failed: failures.length };
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
  const scanId = findAttr(event.target, "data-scan-history");
  if (scanId) {loadIncidentScanHistory(scanId, {older: Boolean(findAttr(event.target, "data-scan-history-older"))}); return;}
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
    const focus = findAttr(target, "data-focus");
    setHash(`#/projects/${encodeURIComponent(projectKey)}`);
    openProject(projectKey, focus || "context");
    return;
  }
  if (ruleId) {
    setHash(`#/rules/${encodeURIComponent(ruleId)}`);
    openRule(ruleId, tab || "why");
  }
}

/**
 * Enter and Space on a focused row do what a click does.
 *
 * Rules and Projects rows carry tabindex="0", so they advertise themselves to
 * assistive technology and to the tab key as interactive. Until 2026-08-23 the
 * only activation path was a click listener, so a keyboard user could focus a
 * row and press Enter forever: the inspector — the whole progressive-disclosure
 * journey of V2 and V4 — was unreachable without a mouse.
 */
export function handleMainKeydown(event) {
  if (event.key !== "Enter" && event.key !== " " && event.key !== "Spacebar") return;
  const target = event.target;
  if (!findAttr(target, "data-rule-id") && !findAttr(target, "data-project-key")) return;
  // Space scrolls the page by default, which is the wrong thing once the key
  // means "open this row".
  event.preventDefault();
  handleMainClick(event);
}

export function handleInspectorClick(event) {
  const scanId = findAttr(event.target, "data-scan-history");
  if (scanId) {
    if (state.inspectorKind === "rule" && !findAttr(event.target, "data-scan-history-older")) setHash(`#/rules/${encodeURIComponent(state.selectedRule)}?tab=evidence&scan=${encodeURIComponent(scanId)}`);
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
    setHash(`#/rules/${encodeURIComponent(state.selectedRule)}?tab=${encodeURIComponent(tab)}`);
    openRule(state.selectedRule, tab);
  }
  else if (state.inspectorKind === "project") {
    setHash(`#/projects/${encodeURIComponent(state.selectedProject)}?tab=${encodeURIComponent(tab)}`);
    openProject(state.selectedProject, tab);
  }
}

export function handleSearchInput(event) {
  state.query = event && event.target ? String(event.target.value || "") : "";
  paintRuleRows();
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

export function wire() {
  const d = doc();
  byId("skip-to-main").addEventListener("click", (event) => {
    event.preventDefault();
    byId("main").focus();
  });
  byId("theme-toggle").addEventListener("click", () => toggleTheme());
  byId("inspector-close").addEventListener("click", () => closeInspector());
  byId("inspector-body").addEventListener("click", handleInspectorClick);
  byId("inspector-body").addEventListener("submit", submitProjectExposure);
  byId("main").addEventListener("click", handleMainClick);
  byId("main").addEventListener("keydown", handleMainKeydown);
  byId("main").addEventListener("change", (event) => {
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
      if (event.key === "/" && state.route === "rules") {
        const input = byId("rules-search-input");
        if (d.activeElement !== input) {
          if (typeof event.preventDefault === "function") event.preventDefault();
          if (typeof input.focus === "function") input.focus();
        }
      }
      handleReviewKeydown(event);
      if (event.key === "Escape") closeInspector();
    });
  }
}

export async function start() {
  applyTheme(readStoredTheme());
  wire();
  showRoute(parseRoute(currentHash()));
  const result = await load();
  showRoute(parseRoute(currentHash()));
  return result;
}

const bootDocument = doc();
if (bootDocument && bootDocument.getElementById && bootDocument.getElementById("app-root")) {
  start().catch((error) => {
    showError("The dashboard failed to start", String(error && error.message ? error.message : error));
  });
}
