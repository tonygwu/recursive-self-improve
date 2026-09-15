"""Test dashboard structure and behavior through its real application entry point.
Static checks reconcile markup, CSS, local URLs, and the allowed write routes.
Behavior tests run app.js in Node with a minimal DOM and an injected fetch.
Invented payloads exercise normal rendering, fallback states, and interactions.
Node is required; its absence fails these tests.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "src" / "self_improve" / "dashboard" / "static"
INDEX_HTML = STATIC / "index.html"
APP_JS = STATIC / "app.js"
TOKENS_CSS = STATIC / "tokens.css"
APP_CSS = STATIC / "app.css"

VOID_ELEMENTS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}

#: Class names app.js builds from a variable rather than a literal, so the
#: literal scanner cannot see them. Every one is asserted against app.css by
#: test_dynamic_class_names_are_also_defined, so this is not an escape hatch.
DYNAMIC_CLASS_NAMES = {
    "badge",
    "badge--lg",
    "run-grid",
    "run-grid--numbers",
    "failure--fixed",
    "empty--inline",
    "inspector",
    "workspace--split",
}


# ---------------------------------------------------------------------------
# Reading the three files under test
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def html_text() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def js_text() -> str:
    return APP_JS.read_text(encoding="utf-8")


def _strip_js_comments(source: str) -> str:
    """Remove /* */ and // comments so a scan sees code, not prose."""
    source = re.sub(r"/\*.*?\*/", " ", source, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", " ", source)


def _strip_css_comments(source: str) -> str:
    return re.sub(r"/\*.*?\*/", " ", source, flags=re.S)


class _Structure(HTMLParser):
    """Tracks tag balance and collects ids, classes and external references."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, int]] = []
        self.errors: list[str] = []
        self.ids: list[str] = []
        self.classes: set[str] = set()
        self.urls: list[str] = []

    def handle_starttag(self, tag, attrs):
        mapping = dict(attrs)
        if "id" in mapping and mapping["id"]:
            self.ids.append(mapping["id"])
        if mapping.get("class"):
            self.classes.update(mapping["class"].split())
        for name in ("src", "href", "action", "srcset", "data"):
            if mapping.get(name):
                self.urls.append(mapping[name])
        if tag not in VOID_ELEMENTS:
            self.stack.append((tag, self.getpos()[0]))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID_ELEMENTS and self.stack and self.stack[-1][0] == tag:
            self.stack.pop()

    def handle_endtag(self, tag):
        if tag in VOID_ELEMENTS:
            self.errors.append(f"line {self.getpos()[0]}: </{tag}> closes a void element")
            return
        if not self.stack:
            self.errors.append(f"line {self.getpos()[0]}: </{tag}> with nothing open")
            return
        open_tag, line = self.stack.pop()
        if open_tag != tag:
            self.errors.append(
                f"line {self.getpos()[0]}: </{tag}> closes <{open_tag}> opened on line {line}"
            )


@pytest.fixture(scope="session")
def parsed(html_text: str) -> _Structure:
    parser = _Structure()
    parser.feed(html_text)
    parser.close()
    return parser


# ---------------------------------------------------------------------------
# 1. The HTML parses, and its ids are unique
# ---------------------------------------------------------------------------


def test_index_html_parses_with_balanced_tags(parsed: _Structure):
    assert parsed.errors == [], "index.html has mismatched tags: " + "; ".join(parsed.errors)
    assert parsed.stack == [], f"index.html leaves tags open: {parsed.stack}"


def test_index_html_ids_are_unique(parsed: _Structure):
    duplicates = sorted({i for i in parsed.ids if parsed.ids.count(i) > 1})
    assert duplicates == [], f"index.html declares these ids more than once: {duplicates}"


def test_index_html_declares_a_shell_for_every_view_app_js_routes_to(js_text: str, parsed: _Structure):
    """Derived from VIEWS both ways: a view with no shell, and a shell with no
    view, are both failures. P0 asserted the absence of `view-review`; P1 ships
    it, so the guard now asks the question that survives the next view."""
    views = _views_declared_by_js(js_text)
    assert "review" in views, "V3 is built; VIEWS must route to it"
    missing = [f"view-{v}" for v in views if f"view-{v}" not in parsed.ids]
    assert missing == [], f"VIEWS routes to shells index.html does not declare: {missing}"
    shells = {i for i in parsed.ids if i.startswith("view-")}
    orphans = sorted(shells - {f"view-{v}" for v in views})
    assert orphans == [], f"index.html declares shells no view routes to: {orphans}"


# ---------------------------------------------------------------------------
# 2. Every element id app.js looks up exists in index.html
# ---------------------------------------------------------------------------


def _ids_used_by_js(js: str) -> set[str]:
    code = _strip_js_comments(js)
    used = set(re.findall(r"""byId\(\s*["']([^"'`]+)["']\s*\)""", code))
    used.update(re.findall(r"""setHTML\(\s*["']([^"'`]+)["']""", code))
    used.update(re.findall(r"""setText\(\s*["']([^"'`]+)["']""", code))
    used.update(re.findall(r"""getElementById\(\s*["']([^"'`]+)["']\s*\)""", code))
    # `view-${view}` and `nav-${view}` are built from app.js's own VIEWS list,
    # so expand them from THAT list rather than from a copy of it here. The
    # copy is what made this scanner a fourth place to remember a new view.
    views = _views_declared_by_js(js)
    for template in re.findall(r"byId\(\s*`([^`]+)`\s*\)", code):
        prefix = {"view-${view}": "view-", "nav-${view}": "nav-"}.get(template)
        if prefix is None:  # pragma: no cover - a new template must be taught here
            raise AssertionError(f"app.js builds an element id this test cannot expand: {template}")
        used.update(f"{prefix}{view}" for view in views)
    return used


def _views_declared_by_js(js: str) -> list[str]:
    """The VIEWS list, read out of app.js. Never restated in this file."""
    match = re.search(r"export const VIEWS = Object\.freeze\(\[([^\]]*)\]\)", js)
    assert match, "could not find the VIEWS declaration in app.js"
    views = re.findall(r"""["']([a-z-]+)["']""", match.group(1))
    assert views, "the VIEWS declaration parsed to nothing; the scanner is broken"
    return views


def test_every_element_id_app_js_uses_is_declared_in_index_html(js_text: str, parsed: _Structure):
    used = _ids_used_by_js(js_text)
    assert used, "found no element ids in app.js; the scanner is broken"
    # Dynamic inspectors declare controls in their rendered HTML. Their behavior
    # is exercised through the real renderer and browser as well as this lookup.
    rendered_ids = set(re.findall(r'id="([a-z][a-z0-9-]*)"', _strip_js_comments(js_text)))
    missing = sorted(used - set(parsed.ids) - rendered_ids)
    assert missing == [], f"app.js looks up undeclared static or rendered ids: {missing}"


def test_the_id_scanner_finds_the_ids_that_matter(js_text: str):
    """Guards the scanner itself: a regex that matches nothing passes vacuously."""
    used = _ids_used_by_js(js_text)
    for expected in ("main", "inspector", "theme-toggle", "rules-search-input", "ov-grid"):
        assert expected in used, f"the id scanner missed {expected}"


# ---------------------------------------------------------------------------
# 3. Nothing leaves this origin
# ---------------------------------------------------------------------------

_EXTERNAL = re.compile(r"""["'`](?:[a-z][a-z0-9+.-]*:)?//""", re.I)


def test_app_js_references_no_external_origin(js_text: str):
    code = _strip_js_comments(js_text)
    hits = [m.group(0) for m in _EXTERNAL.finditer(code)]
    assert hits == [], f"app.js contains a non-relative URL: {hits}"
    for scheme in ("http:", "https:", "ftp:", "ws:", "wss:"):
        assert f'"{scheme}' not in code and f"'{scheme}" not in code, (
            f"app.js names the {scheme} scheme; the page must work with the network off"
        )


def test_index_html_loads_only_same_origin_relative_assets(parsed: _Structure):
    for url in parsed.urls:
        if url.startswith("#"):
            continue
        assert not _EXTERNAL.match(f'"{url}'), f"index.html references an external origin: {url}"
        assert "://" not in url, f"index.html references an absolute URL: {url}"
        assert not url.startswith("//"), f"index.html references a protocol-relative URL: {url}"


def test_index_html_names_only_files_that_exist(parsed: _Structure):
    for url in parsed.urls:
        if url.startswith("#"):
            continue
        assert (STATIC / url).exists(), f"index.html references a missing file: {url}"


# ---------------------------------------------------------------------------
# 4. Read-only: no write verb, every fetch is a GET
# ---------------------------------------------------------------------------


def test_only_the_one_declared_write_verb_appears_in_app_js(js_text: str):
    """P0 named no write verb. P1 names exactly one, in exactly one function.

    Both halves are asserted. A second POST, or a PUT/PATCH/DELETE anywhere,
    fails; and so does the disappearance of the one that should be there, so
    deleting the write path cannot leave this passing.
    """
    code = _strip_js_comments(js_text)
    assert re.findall(r"\b(PUT|DELETE|PATCH)\b", code) == [], "app.js names a verb V3 does not use"
    posts = re.findall(r"""method\s*:\s*["']POST["']""", code)
    assert len(posts) == 1, f"app.js issues {len(posts)} POSTs; the product has one write"
    assert "postDecision" in code, "the one write function is gone"


def test_every_fetch_names_the_get_method(js_text: str):
    code = _strip_js_comments(js_text)
    fetches = re.findall(r"fetch\(([^;]*?)\)\s*;", code, flags=re.S)
    assert fetches, "found no fetch call in app.js; the scanner is broken"
    for call in fetches:
        assert "method:" in call, f"a fetch call does not state its method: {call.strip()}"
    methods = re.findall(r"""method\s*:\s*["']([A-Za-z]+)["']""", code)
    assert methods, "found no method: option in app.js; the scanner is broken"
    assert set(methods) == {"GET", "POST"}, f"app.js uses unexpected methods: {sorted(set(methods))}"
    assert methods.count("POST") == 1, "more than one write call"


def test_app_js_declares_only_the_endpoints_the_product_has(js_text: str):
    """Collection routes and parameterized resource routes are explicit."""
    urls = set(re.findall(r"""["'](/api/[a-z0-9/_-]+)["']""", js_text))
    assert urls == {"/api/overview", "/api/rules", "/api/projects", "/api/review-queue", "/api/commands", "/api/review-preview", "/api/operations", "/api/incidents", "/api/runs", "/api/incident-rate", "/api/eval-attempts", "/api/eval-results", "/api/eval-health"}, (
        f"app.js fetches an unexpected endpoint set: {sorted(urls)}"
    )
    code = _strip_js_comments(js_text)
    dynamic_urls = set(re.findall(r"""`(/api/[^`]*)`""", code))
    assert dynamic_urls == {"/api/proposals/${encodeURIComponent(proposalId)}/decision",
                            "/api/proposals/${encodeURIComponent(proposalId)}/rollback-preview",
                            "/api/proposals/${encodeURIComponent(proposalId)}/eval-preview",
                            "/api/proposals/${encodeURIComponent(proposalId)}/resolution-preview",
                            "/api/proposals/${encodeURIComponent(proposalId)}/reapplication-preview",
                            "/api/incidents/${encodeURIComponent(proposalId)}/mining-preview",
                            "/api/incidents/${encodeURIComponent(entry.proposalId)}/mining-preview?full=true",
                            "/api/learnings/${encodeURIComponent(selection.learning_id)}",
                            "/api/learnings/${encodeURIComponent(learningId)}/mining-history?limit=20",
                                "/api/incidents/${encodeURIComponent(id)}/scan-history?limit=20"}, (
        f"app.js builds an unexpected resource URL: {sorted(dynamic_urls)}"
    )


def test_app_js_contains_no_countdown(js_text: str):
    """The backlog is a race between two rates. The queue grows, so a drain
    date would be fiction (PRD section 7, V1)."""
    code = _strip_js_comments(js_text).lower()
    assert "drain" not in code, "app.js code mentions draining the queue"
    assert "nights_to" not in code and "eta" not in re.findall(r"\b\w+\b", code), (
        "app.js code names a countdown"
    )


# ---------------------------------------------------------------------------
# 5. The design system is used, not duplicated
# ---------------------------------------------------------------------------


def _defined_custom_properties(css: str) -> set[str]:
    return set(re.findall(r"(--[a-zA-Z][\w-]*)\s*:", _strip_css_comments(css)))


def _used_custom_properties(text: str) -> set[str]:
    stripped = _strip_js_comments(text)
    used = set(re.findall(r"var\(\s*(--[a-zA-Z][\w-]*)", stripped))
    used.update(re.findall(r"(--[a-zA-Z][\w-]*)\s*:", stripped))
    return used


TOKEN_PREFIXES = ("--c-", "--s-", "--r-", "--t-", "--z-", "--e-", "--font-")


@pytest.fixture(scope="session")
def tokens_defined() -> set[str]:
    return _defined_custom_properties(TOKENS_CSS.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def app_css_defined() -> set[str]:
    return _defined_custom_properties(APP_CSS.read_text(encoding="utf-8"))


def test_every_design_token_used_is_defined_in_tokens_css(
    js_text: str, html_text: str, tokens_defined: set[str]
):
    # Stylesheet consumers need the same check as inline markup, including
    # nested fallback values. A fallback naming an undefined token is invalid.
    used = set().union(*(
        _used_custom_properties(source)
        for source in (js_text, html_text, APP_CSS.read_text(), TOKENS_CSS.read_text())
    ))
    tokens = {p for p in used if p.startswith(TOKEN_PREFIXES)}
    assert tokens, "found no design tokens in the markup; the scanner is broken"
    missing = sorted(tokens - tokens_defined)
    assert missing == [], f"dashboard sources use undefined design tokens: {missing}"


def test_every_custom_property_used_is_defined_by_a_stylesheet(
    js_text: str, html_text: str, tokens_defined: set[str], app_css_defined: set[str]
):
    """The non-token properties (--pct, --v) are app.css furniture, declared by
    the markup and consumed by app.css. They must still be known, not invented."""
    used = _used_custom_properties(js_text) | _used_custom_properties(html_text)
    # An empty scan makes the missing-set assertion vacuous. Prove that the
    # intended construct was found without hardcoding its current population.
    assert "--pct" in used, (
        f"the custom-property scanner no longer finds --pct, which app.js "
        f"declares; it has stopped working and the check below would pass on "
        f"anything. Found: {sorted(used)}"
    )
    missing = sorted(used - tokens_defined - app_css_defined)
    assert missing == [], f"the markup uses custom properties no stylesheet defines: {missing}"


def _class_names(text: str) -> set[str]:
    """Class names from literal class="..." attributes. A value built from a
    variable contributes only its literal prefix; the rest is covered by
    DYNAMIC_CLASS_NAMES."""
    names: set[str] = set()
    for value in re.findall(r'class="([^"]*)"', _strip_js_comments(text)):
        literal = value.split("${")[0]
        names.update(token for token in literal.split() if token)
    return names


@pytest.fixture(scope="session")
def app_css_classes() -> set[str]:
    css = _strip_css_comments(APP_CSS.read_text(encoding="utf-8"))
    return set(re.findall(r"\.([A-Za-z][\w-]*)", css))


def test_every_class_the_markup_uses_is_defined_in_app_css(
    js_text: str, html_text: str, app_css_classes: set[str]
):
    used = _class_names(js_text) | _class_names(html_text)
    assert used, "found no class names in the markup; the scanner is broken"
    missing = sorted(used - app_css_classes)
    assert missing == [], (
        "the markup uses classes app.css does not define, which is how two "
        f"design systems start to drift: {missing}"
    )


def test_dynamic_class_names_are_also_defined(app_css_classes: set[str]):
    missing = sorted(DYNAMIC_CLASS_NAMES - app_css_classes)
    assert missing == [], f"app.js builds class names app.css does not define: {missing}"


#: Cell states the design system deliberately does NOT draw. Both mean the
#: database holds a shape this dashboard has never been taught, so the unmapped
#: alarm is the correct rendering and a neutral colour would hide a pipeline
#: change. Anything else the data layer can emit must be drawn.
DELIBERATELY_ALARMING_STATES = {"unreadable", "unknown_status"}

#: data-* attributes app.js emits to CHANGE HOW SOMETHING LOOKS, as opposed to
#: the behavioural hooks a click handler reads back (data-rule-id,
#: data-project-key, data-tab, data-focus). A visual mark app.css never styles
#: is not a mark at all.
VISUAL_DATA_MARKS = ("state", "verdict", "hidden-runs")


def _js_frozen_list(js: str, name: str) -> list[str]:
    """The string members of `export const <name> = Object.freeze([...])`."""
    match = re.search(
        rf"export const {name} = Object\.freeze\(\[(.*?)\]\)", js, flags=re.S
    )
    assert match, f"app.js no longer declares {name} as a frozen array"
    return re.findall(r'"([^"]+)"', match.group(1))


def test_the_id_of_every_drawn_state_scanner_works(js_text: str):
    """Guards the scanner the two contract tests below depend on."""
    drawn = _js_frozen_list(js_text, "DRAWN_STATES")
    assert "ok" in drawn and "skipped" in drawn, drawn
    assert len(drawn) == len(set(drawn)), f"DRAWN_STATES repeats a state: {drawn}"


def test_every_cell_state_the_data_layer_emits_is_drawn_or_deliberately_alarming(
    js_text: str,
):
    """Reconcile emitted cell states with drawn states and explicit unknown-state alarms."""
    from self_improve.dashboard import queries

    drawn = set(_js_frozen_list(js_text, "DRAWN_STATES"))
    emitted = set(queries.CELL_STATES_WORST_FIRST)
    undrawn = emitted - drawn
    assert undrawn == DELIBERATELY_ALARMING_STATES, (
        "app.js draws a different set of states than queries can emit. "
        f"undrawn: {sorted(undrawn)}; expected exactly "
        f"{sorted(DELIBERATELY_ALARMING_STATES)}. A state queries emits and "
        "app.css cannot draw renders as an unmapped alarm."
    )


def test_every_drawn_state_and_every_marker_has_a_rule_in_app_css(js_text: str):
    """app.js may only claim a state is drawn if app.css actually draws it."""
    css = _strip_css_comments(APP_CSS.read_text(encoding="utf-8"))
    for state in _js_frozen_list(js_text, "DRAWN_STATES"):
        assert f'[data-state="{state}"]' in css, (
            f'app.js lists "{state}" as drawn but app.css has no '
            f'[data-state="{state}"] rule, so it falls through to the '
            "unmapped alarm"
        )
    for attribute in VISUAL_DATA_MARKS:
        assert f"data-{attribute}=" in _strip_js_comments(js_text), (
            f"app.css styles data-{attribute} but app.js no longer emits it"
        )
        assert f"[data-{attribute}]" in css or f'[data-{attribute}="' in css, (
            f"app.js emits data-{attribute} but no app.css rule reads it, so "
            "the mark is invisible"
        )


def test_index_html_writes_no_css_of_its_own(html_text: str):
    """The design system owns the look. Inline style attributes may only wire
    layout (visibility and spacing tokens), never colour or type."""
    assert "<style" not in html_text.lower(), "index.html carries its own stylesheet"
    for value in re.findall(r'style="([^"]*)"', html_text):
        for declaration in value.split(";"):
            if not declaration.strip():
                continue
            prop = declaration.split(":")[0].strip()
            assert prop in {"display", "padding", "margin-top", "width"}, (
                f"index.html sets {prop} inline; that belongs to app.css"
            )


# ---------------------------------------------------------------------------
# 6. Behaviour, through the real entry point, in Node
# ---------------------------------------------------------------------------

HARNESS = r"""
// Minimal DOM shim. It gives app.js exactly the element ids index.html
// declares and nothing else, so an id app.js invents fails loudly here.
import fs from "node:fs";

const [appPath, htmlPath, mode] = process.argv.slice(2);
const html = fs.readFileSync(htmlPath, "utf8");
const declaredIds = new Set();
const idRe = /\sid="([^"]+)"/g;
let match;
while ((match = idRe.exec(html)) !== null) declaredIds.add(match[1]);

class El {
  constructor(id) {
    this.id = id;
    this.innerHTML = "";
    this._text = "";
    this.hidden = false;
    this.className = "";
    this.value = "";
    this.style = {};
    this.attributes = {};
    this.listeners = {};
    this.focused = false;
    const classes = new Set();
    this.classList = {
      add: (name) => classes.add(name),
      remove: (name) => classes.delete(name),
      contains: (name) => classes.has(name),
      toggle: (name, on) => (on ? classes.add(name) : classes.delete(name)),
      values: () => Array.from(classes),
    };
  }
  get textContent() { return this._text; }
  set textContent(value) { this._text = String(value); this.innerHTML = ""; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) {
    return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null;
  }
  removeAttribute(name) { delete this.attributes[name]; }
  addEventListener(type, fn) {
    if (!this.listeners[type]) this.listeners[type] = [];
    this.listeners[type].push(fn);
  }
  dispatch(type, event) {
    (this.listeners[type] || []).forEach((fn) => fn(event || {}));
  }
  focus() { this.focused = true; }
  // Everything app.js reads back out of an element:
  visible() { return this.style.display !== "none"; }
  html() { return this.innerHTML; }
}

const elements = new Map();
for (const id of declaredIds) elements.set(id, new El(id));
const root = new El("__documentElement__");

// Document-level listeners were a no-op stub until 2026-09-04, which meant the
// `/` and Escape shortcuts had never once been exercised. They are recorded now
// so a keyboard flow can be driven through the real entry point like any click.
const docListeners = new Map();
const querySelectorCalls = [];
let failPostFor = null;
let lastSelectedStub = null;
function makeCardStub() {
  return {
    scrolledInto: null,
    focused: null,
    scrollIntoView(opts) { this.scrolledInto = opts; },
    focus(opts) { this.focused = opts; },
  };
}
globalThis.document = {
  documentElement: root,
  activeElement: null,
  getElementById: (id) => elements.get(id) || null,
  // Recording stub: the app scrolls and focuses the selected card, and a
  // highlight the operator cannot see is the bug this exists to catch.
  querySelector: (sel) => {
    querySelectorCalls.push(sel);
    if (sel !== ".review-card--selected") return null;
    if (!lastSelectedStub) return null;
    return lastSelectedStub;
  },
  addEventListener: (type, fn) => {
    if (!docListeners.has(type)) docListeners.set(type, []);
    docListeners.get(type).push(fn);
  },
};
function pressKey(key, extra) {
  let prevented = false;
  const event = Object.assign(
    { key, target: null, preventDefault: () => { prevented = true; } },
    extra || {}
  );
  (docListeners.get("keydown") || []).forEach((fn) => fn(event));
  return prevented;
}
globalThis.window = { addEventListener: () => {} };
globalThis.location = { hash: "" };
const memory = new Map();
globalThis.localStorage = {
  getItem: (key) => (memory.has(key) ? memory.get(key) : null),
  setItem: (key, value) => memory.set(key, String(value)),
};

const fetchCalls = [];
globalThis.fetch = async (url, init) => {
  fetchCalls.push({ url, init });
  const broken = mode === "fail" && url === "/api/rules";
  if (broken) return { ok: false, status: 404, statusText: "Not Found", json: async () => ({}) };
  if (url.startsWith('/api/commands?')) {
    return { ok: true, status: 200, json: async () => ({ commands: [], next_cursor: null, more_available: false }) };
  }
  if (url.startsWith('/api/review-preview?')) {
    const ids = new URL('http://localhost' + url).searchParams.get('proposal_ids').split(',');
    const proposals = REVIEW.families.flatMap((f) => f.proposals).filter((p) => ids.includes(p.id)).sort((a,b) => a.id.localeCompare(b.id));
    const data = { ready: true, revision: 'd'.repeat(64),
      members: proposals.map((p) => ({ proposal_id: p.id, revision: p.revision, snapshot: { proposal: p, evidence: [], evaluation: null } })),
      targets: proposals.map((p) => ({ destination: { mode: 'file', target_path: p.target_path }, proposal_ids: [p.id], state: 'ready', diff_unified: p.diff_unified, budget: null })) };
    return { ok: true, status: 200, json: async () => data };
  }
  if (init && init.method === "POST") {
    const sent = JSON.parse(init.body);
    if (failPostFor && (url.indexOf(failPostFor) !== -1 || (sent.members || []).some((m) => m.proposal_id === failPostFor))) {
      return {
        ok: false, status: 409, statusText: "Conflict",
        json: async () => ({ detail: "proposal is not in the review queue" }),
      };
    }
    return {
      ok: true, status: 200, statusText: "OK",
      json: async () => (url === "/api/commands"
        ? { id: "command-fixture", action: sent.action, state: sent.action === "approve" ? "queued" : "completed", members: sent.members }
        : { proposal_id: "x", decision: sent.decision, applied: false }),
    };
  }
  return { ok: true, status: 200, statusText: "OK", json: async () => PAYLOAD[url] };
};

// -------------------------------------------------------------------- data
// Invented payloads for rendering and interaction tests. Unsupported values
// exercise explicit fallback states; these are not an operational snapshot.
const FRACTION_NO_DATA = {
  numerator: 0, denominator: 0, rate: null, enough_data: false, min_denominator: 20,
  display: "0 applied · 0 rolled back — no rule has ever been applied, so there is nothing to judge",
  reason: "no rule has ever been applied, so there is nothing to judge",
};

const GRID = {
  stages: ["scan", "mine", "cluster", "gate", "apply"],
  nights: ["2026-08-15", "2026-08-18"],
  cell_states: ["unreadable", "unknown_status", "failed", "error", "abandoned", "partial",
                "budget_exhausted", "refused", "running", "ok", "skipped"],
  columns: [
    {
      night: "2026-08-15", run_count: 6, run_ids: ["a"], run_states: { ok: 5, abandoned: 1 },
      worst: "abandoned",
      cells: {
        // An abandoned run has no recorded stage outcome. Mark it even when
        // completed runs make the cell's recorded outcome look healthy.
        scan: { state: "ok", states: { ok: 5, abandoned: 1 }, runs: 6, runs_without_record: 1, attempted: 240, succeeded: 240, failed: 0, number: 240, number_label: "files scanned" },
        mine: { state: "partial", states: { partial: 1, ok: 5 }, runs: 6, runs_without_record: 0, attempted: 15, succeeded: 9, failed: 6, number: 9, number_label: "incidents mined" },
        cluster: { state: "ok", states: { ok: 6 }, runs: 6, runs_without_record: 0, attempted: 4, succeeded: 4, failed: 0, number: 4, number_label: "candidate rules" },
        gate: { state: "refused", states: { refused: 6 }, runs: 6, runs_without_record: 0, attempted: 32, succeeded: 0, failed: 0, number: 0, number_label: "verdicts" },
        apply: { state: "skipped", states: { skipped: 6 }, runs: 6, runs_without_record: 6, attempted: null, succeeded: null, failed: null, number: null, number_label: "" }
      }
    },
    {
      night: "2026-08-18", run_count: 14, run_ids: ["b"], run_states: { ok: 7, running: 7 },
      worst: "running",
      cells: {
        scan: { state: "ok", states: { ok: 14 }, runs: 14, runs_without_record: 0, attempted: 560, succeeded: 560, failed: 0, number: 560, number_label: "files scanned" },
        // Partial outcomes outrank running, so the running members require a
        // separate visible mark in this mixed cell.
        mine: { state: "partial", states: { running: 7, skipped: 3, partial: 4 }, runs: 14, runs_without_record: 10, attempted: 30, succeeded: 16, failed: 14, number: 16, number_label: "incidents mined" },
        cluster: { state: "running", states: { running: 7 }, runs: 14, runs_without_record: 7, attempted: null, succeeded: null, failed: null, number: null, number_label: "" },
        gate: { state: "unreadable", states: { unreadable: 1 }, runs: 14, runs_without_record: 13, attempted: null, succeeded: null, failed: null, number: null, number_label: "" },
        apply: { state: "skipped", states: { skipped: 14 }, runs: 14, runs_without_record: 14, attempted: null, succeeded: null, failed: null, number: null, number_label: "" }
      }
    }
  ],
  night_timezone: "UTC",
  reference_day: "2026-08-22",
  runs_total: 25,
  nights_with_runs: 3,
  unknown_run_statuses: {},
  unreadable: [{ run_id: "b", night: "2026-08-18", stage: "gate", keys: ["mystery"] }],
  window: { applied: true, window_days: 14, ends_at: "2026-08-22", nights_dropped: 1, runs_dropped: 5, oldest_night_kept: "2026-08-09", reason: "caller asked" }
};

const OVERVIEW = {
  freshness: {
    as_of: "2026-08-18", first_incident_ts: "2026-05-01T00:00:00Z",
    last_incident_ts: "2026-08-18T12:00:00Z", incidents_with_ts: 1907,
    reference_day: "2026-08-22", days_stale: 4,
    banner: "Data as of 2026-08-18, 4 days stale.",
    timezone: "UTC", source_column: "incidents.ts (transcript time, not created_at)"
  },
  status_line: {
    text: "Ran 2 of the last 7 nights. 4 rules learned, 0 applied, 1 waiting on you.",
    nights_ran: 2, nights_window: 7, nights_ran_days: ["2026-08-16", "2026-08-18"],
    window_start: "2026-08-16", window_end: "2026-08-22", reference_day: "2026-08-22",
    run_statuses_in_window: { ok: 7, running: 7 }, rules_learned_total: 4,
    rules_learned_in_window: 0, proposals_applied: 0, waiting_on_you: 1
  },
  grid: GRID,
  // count is filled in from REVIEW below, not typed here: queries derives the
  // nav badge and the view header from one function, so a fixture that lets
  // them differ is testing a state the backend cannot produce.
  inbox: {
    count: null, line: "1 item waiting on you",
    second_line: "3 auto-applicable, blocked by --review-only",
    by_status: { pending: 1, ungated: 3, teleported: 2 },
    queueing_statuses: ["pending", "gated_fail", "inconclusive", "held"],
    auto_apply_pending: 3, auto_apply_statuses: ["gated_pass", "ungated"],
    review_only: true, review_only_source: "from run 00000001 started 2026-08-18T00:00:00Z",
    unknown_statuses: { teleported: 2 }
  },
  backlog: {
    queued: 1863,
    incidents_by_status: { new: 1863, mined: 41, dismissed: 3 },
    mine_capacity_per_run: 80, mine_order: "signal_then_recent",
    arrivals: {
      per_day: 125.5, total_in_window: 1757, window_days: 14,
      window_start: "2026-08-05", window_end: "2026-08-18",
      window_ends_at: "last data day", days_with_zero: 0, per_day_counts: {},
      source_column: "incidents.ts",
      why_not_created_at: "created_at is scan time; arrival rates use source event timestamps"
    },
    net_per_day: 45.5,
    race: "arrivals versus capacity, not a countdown: the queue grows"
  },
  failures: {
    open: [{ class: "parse_error", name: "No answer we could read", explanation: "The model's reply was not the JSON contract we asked for.", has_copy: true, stages: ["mine_agentic"], total: 17, recent: 17, recent_window_days: 7, recent_window_from: "2026-08-12", last_seen: { run_id: "b", started: "2026-08-18T00:00:00Z", count: 17 }, status: "open" }],
    quiet: [],
    fixed: [{ class: "IntegrityError", name: "Duplicate evidence link", explanation: "Two mine attempts tried to link the same incident to the same learning.", has_copy: true, stages: ["mine"], total: 8, recent: 0, recent_window_days: 7, recent_window_from: "2026-08-12", last_seen: null, status: "fixed", fixed_at: "2026-08-16T10:56:04Z", fix_commit: "cfd6410", fix: "linking is idempotent" }],
    regressed: [],
    unknown_classes: [],
    not_failures: [{ class: "budget_exhausted", count: 64, why: "incidents the run's cap never reached" }],
    llm_by_stage: {},
    success_outcomes: ["ok", "oauth_transient_retried", "parse_recovered"],
    latest_run_day: "2026-08-18",
    note: "Nothing was lost — every failed incident stays queued and is retried."
  },
  gate: {
    eval_rows_total: 10, proposal_evals: 2, seed_evals: 7, learning_subject_evals: 1,
    unknown_subject_evals: 0,
    by_subject_kind: { proposal: 2, learning: 1, seed: 7, unknown: 0 },
    by_class: { harness_broken: 1, eval_invalid: 6, rule_failed: 2, inconclusive: 1 },
    by_verdict: { gated_pass: 1, gated_fail: 2, ungated: 4, inconclusive: 1, teleported: 2 },
    rows: [
      { id: "e1", class: "rule_failed", label: "The rule failed", why: "both arms ran and the with-rule arm lost", verdict: "gated_fail" },
      { id: "e2", class: "eval_invalid", label: "The eval could not reproduce the mistake", why: "the with-rule arm never ran", verdict: "ungated" },
      { id: "e3", class: "harness_broken", label: "The harness broke", why: "3 of 3 failed trials were agent_error", verdict: "ungated" },
      { id: "e4", class: "inconclusive", label: "The scenarios disagreed", why: "the majority gate's scenarios disagreed", verdict: "inconclusive" }
    ],
    gate_running: true,
    sentence: "2 of 10 eval results belong to a proposal; the other 8 are seed scenarios and unlinked runs.",
    rate_suppressed: { reason: "a pass rate over a gate this small reads as a failing system rather than a gate that has barely run" },
    verdicts_held: ["gated_fail", "inconclusive"],
    verdicts_auto_applied: ["gated_pass", "ungated"]
  },
  confidence: { applied: 0, rolled_back: 0, survival: FRACTION_NO_DATA }
};

const RULE_ONE = {
  id: "a1111111", title: "Validate the response format",
  rule_text: "Check required response fields before saving a parsed record.",
  why: "An invented malformed response omitted a required field.",
  category: "verification", scope: "global", status: "proposed", confidence: 0.6,
  evidence_count: 4, evidence_linked: 4, project_count: 2,
  projects: ["github:1000000001", "path:/Users/example/Code/invented-client"],
  path_globs: [], primary_project_path: "/Users/example/Code/invented-client",
  first_seen: "2026-07-10T12:00:00Z", last_seen: "2026-08-10T00:00:00Z",
  created_at: "2026-08-15T08:00:00Z", incident_summary: "invented response omitted a required field",
  targets: [{
    proposal_id: "p1", target_path: "/Users/example/.claude/CLAUDE.md",
    target_kind: "global_claude_md", action: "add", proposal_status: "gated_fail",
    applied_at: "",
    diff: "--- a/CLAUDE.md\\n+++ b/CLAUDE.md\\n@@ -1,2 +1,3 @@\\n keep\\n+**Check required response fields**\\n", diff_reason: "",
    eval: { id: "e1", verdict: "gated_fail", attempted: 6, succeeded: 2, failed: 4,
            class: "rule_failed", label: "The rule failed", why: "both arms ran and the with-rule arm lost" }
  }],
  target_summary: "/Users/example/.claude/CLAUDE.md",
  gate_verdict: "gated_fail", gate_class: "rule_failed",
  gate_verdict_source: "proposals.eval_result_id", unlinked_subject_evals: [],
  miner_generation: { value: "—", computable: false, reason: "No mining history was recorded for this learning." },
  provenance: {
    repos: ["example/demo-service"], agent_product: "claude",
    agents_in_evidence: ["claude"], session_ids: ["11111111-1111-4111-8111-111111111111"],
    session_ids_total: 1,
    incidents: [
      { id: "i1", signal_type: "repeated_error", ts: "2026-07-10T12:00:00Z", status: "mined",
        score: 3, session_id: "11111111", project_key: "github:1000000001",
        window_kind: "occurrence", window_len: 3,
        display_text: "example-parser: required field missing",
        display_text_truncated: null, fingerprint: "0000000000000000000000000000000000000000",
        matched_text_is_fingerprint: true,
        occurrences: { sessions: 3, total_count: 9, first_ts: "2026-07-10T12:00:00Z", last_ts: "2026-08-10T00:00:00Z", project_paths: ["/x"] } }
    ],
    incidents_shown: 1, incidents_total: 4, incidents_cut: 3,
    cut_reason: "evidence sample capped at 5 incidents per rule"
  },
  enforcement_gap: { violated_existing_rule: "", flagged: false, label: "No prior rule violation was recorded" }
};

const RULE_TWO = {
  ...RULE_ONE,
  id: "b2222222", title: "<script>alert(1)</script> npm --json is not JSON",
  rule_text: "npm prints a banner before the JSON body.",
  why: "The parser accepted the banner and produced a silent wrong answer.",
  scope: "codex_global", status: "proposed", evidence_count: 1, evidence_linked: 1,
  project_count: 1, gate_verdict: "inconclusive", gate_class: "inconclusive",
  targets: [{
    proposal_id: "p2", target_path: "/Users/example/.codex/AGENTS.md",
    target_kind: "codex_global", action: "add", proposal_status: "inconclusive", applied_at: "",
    eval: { id: "e4", verdict: "inconclusive", attempted: 6, succeeded: 3, failed: 3,
            class: "inconclusive", label: "The scenarios disagreed", why: "the majority gate's scenarios disagreed" }
  }],
  target_summary: "/Users/example/.codex/AGENTS.md",
  enforcement_gap: { violated_existing_rule: "Never accept-and-guess", flagged: true, label: "Written but ignored — a rule already covered this" }
};

const RULE_THREE = {
  ...RULE_ONE, id: "c3333333", title: "",
  rule_text: "Resolve assets from the package directory. Check that each required file exists.",
  why: "The invented package test omitted a required asset.",
  miner_generation: { value: "—", computable: false, reason: 'No recorded "generation" & no producing call for this fixture.' },
  evidence_count: 12, evidence_linked: 12, gate_verdict: null, gate_class: null,
  targets: [], target_summary: "not routed yet (scope: global)"
};

const RULES = {
  rows: [RULE_ONE, RULE_TWO, RULE_THREE], count: 3,
  grouping: {
    available: false, groups: [], learnings_total: 3, learning_embeddings: 0,
    learnings_with_duplicate_of: 0,
    reason: "Duplicate families are not computable yet: no learning has a duplicate_of.",
    empty_state: "Family grouping is off. Showing every rule as its own row."
  },
  by_status: { proposed: 3 }, evidence_sample: 5
};

const WEIGHT_OK = {
  groups: [], observed_at: "2030-01-01T00:00:00Z", selection: "First recorded copy",
  computable: true, path: "/Users/example/Code/demo-service/repo-0",
  repo_path: "/Users/example/Code/demo-service/repo-0",
  resolved_path: "/Users/example/Code/demo-service/repo-0",
  status: "ok", reason: "", exists: true, is_git_repo: true,
  git_root: "/Users/example/Code/demo-service/repo-0", has_instruction_files: true,
  files: [
    { path: "/Users/example/Code/demo-service/repo-0/AGENTS.md", bytes: 12000, chars: 12000, always_loaded: true, kind: "agents_md", origin: "repo root", depth: 0, real_path: "/x/AGENTS.md", is_symlink: false, symlink_target: "", inside_repo: true },
    { path: "/Users/example/Code/demo-service/repo-0/.claude/skills/deploy/SKILL.md", bytes: 4000, chars: 4000, always_loaded: false, kind: "skill", origin: ".claude/skills", depth: 0, real_path: "/x/SKILL.md", is_symlink: false, symlink_target: "", inside_repo: true }
  ],
  file_count: 2, total_bytes: 16000, always_loaded_bytes: 12000, on_demand_bytes: 4000,
  always_loaded_file_count: 1, approx_tokens: 4000, always_loaded_approx_tokens: 3000,
  missing_imports: [], import_cycles: [], deduplicated: [], unreadable: [],
  topology: {
    label: "CLAUDE.md is a symlink to AGENTS.md", error: "", agents_md_exists: true,
    claude_md_exists: true, claude_md_is_symlink: true, claude_md_symlink_to_agents: true,
    claude_md_is_stub: false,
    write_target_general: "/Users/example/Code/demo-service/repo-0/AGENTS.md",
    write_target_claude_specific: "/Users/example/Code/demo-service/repo-0/AGENTS.md"
  },
  other_md: { scanned: true, file_count: 7, bytes: 48000, largest: [], pruned_dir_count: 4, skipped_dir_names: [".git"], stat_errors: [], stat_error_count: 0, note: "markdown in the repo that no session loads" },
  caps_applied: [], notes: []
};

const BENEFIT = {
  value: "—", computable: false,
  reason: "project_stats has no writer for incident recurrence"
};

const PROJECTS = {
  rows: [
    {
      project_key: "github:1000000001", label: "example/demo-service", label_is_fallback: false,
      displays: ["example/demo-service"], key_method: "gh_repo_id", key_methods: ["gh_repo_id"],
      sessions: 900, clones: 2, clone_paths: ["/a", "/b"], clones_on_disk: 2,
      lines_scanned: 2500000, incidents: 1895,
      exposure: { rate_per_100k: 75.8, occurrences: 1895, eligible_lines: 2500000, computable: true, coverage: {coverage_complete: false} },
      top_signal: { signal_type: "repeated_error", count: 1200, tied_with: [], tie_break: "highest count, then the alphabetically first signal_type" },
      signals: { repeated_error: 1200, friction_loop: 695 },
      rules_written_here: 2, rules_received: 0, rules_received_detail: [],
      context_weight: WEIGHT_OK, context_path: "/a",
      context_path_reason: "the working copy with the most sessions that is still on disk",
      benefit: BENEFIT
    },
    {
      project_key: "path:/Users/example/Documents/empty-project", label: "empty-project",
      label_is_fallback: false, displays: ["empty-project"], key_method: "path",
      key_methods: ["path"], sessions: 30, clones: 1, clone_paths: ["/c"], clones_on_disk: 1,
      lines_scanned: 500, incidents: 12,
      exposure: { rate_per_100k: null, occurrences: null, eligible_lines: null, computable: false, reason_text: "No physical-line observations are recorded for this scope." },
      top_signal: { signal_type: "friction_loop", count: 12, tied_with: [], tie_break: "highest count" },
      signals: { friction_loop: 12 },
      rules_written_here: 0, rules_received: 0, rules_received_detail: [],
      context_weight: { ...WEIGHT_OK, has_instruction_files: false, files: [], file_count: 0, total_bytes: 0, always_loaded_bytes: 0 },
      context_path: "/c", context_path_reason: "the only working copy on disk", benefit: BENEFIT
    },
    {
      project_key: "path:/", label: "path:/", label_is_fallback: true, displays: [],
      key_method: "unresolved", key_methods: ["unresolved"], sessions: 8, clones: 1,
      clone_paths: ["/"], clones_on_disk: 1, lines_scanned: 0, incidents: 0,
      exposure: { rate_per_100k: null, computable: false, reason_text: "Version unknown" },
      top_signal: null, signals: {}, rules_written_here: 0, rules_received: 0,
      rules_received_detail: [],
      context_weight: { value: "—", computable: false, reason: "not measured" },
      context_path: "", context_path_reason: "none", benefit: BENEFIT
    }
  ],
  count: 3, sessions_total: 938, incidents_total: 1907, clone_paths_total: 4,
  grouped_on: "sessions.project_key",
  not_a_repository: {
    count: 1234,
    by_method: { path: 678, unresolved: 556 },
    methods: ["path", "unresolved"],
    reason: "PRD D2: a project is the upstream repository.",
  }, unmatched_incident_keys: [],
  proposals_not_attributed: [{ proposal_id: "p1", target_path: "/Users/example/.claude/CLAUDE.md", target_kind: "global_claude_md", status: "gated_fail" }],
  proposals_not_attributed_reason: "proposals have no project_key",
  context_weight_errors: [], context_weight_available: true, context_weight_reason: "",
  context_weight_measured: 3, context_weight_capped: null
};

const REVIEW = {
  families: [
    {
      learning_id: "L1",
      title: "discover enabled adapters",
      rule_text: "Read enabled adapters from configuration before choosing one.",
      why: "An invented configuration added an adapter absent from a fixed list.",
      evidence_count: 4,
      project_count: 2,
      size: 2,
      targets: ["/Users/x/.claude/CLAUDE.md", "/Users/x/repo/AGENTS.md"],
      reason_codes: ["gated_fail", "inconclusive"],
      lead_reason: "The eval could not show that this rule helps.",
      has_carve_out: false,
      proposals: [
        { id: "P1", revision: "a".repeat(64), status: "gated_fail", target_path: "/Users/x/.claude/CLAUDE.md",
          target_kind: "global_claude_md", action: "add", diff_unified: "+ discover enabled adapters\n" + "x".repeat(2000),
          eval_result_id: "", created_at: "2026-08-18T08:00:00Z",
          reason_code: "gated_fail", why_needs_you: "The eval could not show that this rule helps.",
          carve_out: false },
        { id: "P2", revision: "b".repeat(64), status: "inconclusive", target_path: "/Users/x/repo/AGENTS.md",
          target_kind: "project_agents_md", action: "add", diff_unified: "",
          eval_result_id: "", created_at: "2026-08-18T08:00:00Z",
          reason_code: "inconclusive", why_needs_you: "The scenarios disagreed. Contrary evidence exists.",
          carve_out: false },
      ],
    },
    {
      learning_id: "a1111111",
      title: "second lesson",
      rule_text: "Check the input file's encoding before decoding its contents.",
      why: "The invented input used a different encoding.",
      evidence_count: 1,
      project_count: 1,
      size: 1,
      targets: ["/Users/x/.claude/CLAUDE.md"],
      reason_codes: ["pending"],
      lead_reason: "Not gated yet - no eval has run on this rule.",
      has_carve_out: false,
      proposals: [
        { id: "P3", revision: "c".repeat(64), status: "pending", target_path: "/Users/x/.claude/CLAUDE.md",
          target_kind: "global_claude_md", action: "add", diff_unified: "",
          eval_result_id: "", created_at: "2026-08-18T08:00:00Z",
          reason_code: "pending", why_needs_you: "Not gated yet - no eval has run on this rule.",
          carve_out: false },
      ],
    },
  ],
  family_count: 2,
  count: 3,
  queueing_statuses: ["pending", "gated_fail", "inconclusive", "held"],
  carve_out_actions: ["convert_to_hook", "delete_human_line"],
  auto_apply_pending: 30,
  auto_apply_note: "30 more auto-apply after the gate and are not cards here.",
  review_only: true,
  unknown_statuses: { superseded: 2 },
  empty_state: "Nothing needs you.",
};

OVERVIEW.inbox.count = REVIEW.count;
OVERVIEW.inbox.line = REVIEW.count + " items waiting on you";
OVERVIEW.status_line.text = OVERVIEW.status_line.text.replace(
  /\d+ waiting on you/, REVIEW.count + " waiting on you"
);

const PAYLOAD = {
  "/api/overview": OVERVIEW,
  "/api/rules": RULES,
  "/api/projects": PROJECTS,
  "/api/review-queue": REVIEW,
};

/** Reads the queue back so a decision check can see the refetch. */
const ENDPOINT_COUNT = Object.keys(PAYLOAD).length;

// ------------------------------------------------------------------- runner
const results = [];
function check(name, fn) {
  try {
    const detail = fn();
    if (detail && typeof detail.then === "function") {
      // Found the hard way on 2026-09-04: a check that returned a promise was
      // recorded as "[object Promise]", ok. Every assertion inside it ran after
      // the runner had already written the result, so the check could not fail.
      // ops/sabotage.sh caught it; nothing else would have.
      throw new Error(
        "check() was handed an async function. Use `await checkAsync(...)`: " +
        "a promise here is a test that can never fail."
      );
    }
    results.push({ name, ok: true, detail: detail === undefined ? "" : String(detail) });
  } catch (error) {
    results.push({ name, ok: false, detail: String(error && error.message ? error.message : error) });
  }
}
async function checkAsync(name, fn) {
  try {
    const detail = await fn();
    results.push({ name, ok: true, detail: detail === undefined ? "" : String(detail) });
  } catch (error) {
    results.push({ name, ok: false, detail: String(error && error.message ? error.message : error) });
  }
}
function expect(condition, message) {
  if (!condition) throw new Error(message);
}
function el(id) {
  const found = elements.get(id);
  if (!found) throw new Error("the shim has no element " + id);
  return found;
}

// A page may arrive with a theme already declared. Boot must respect it.
root.setAttribute("data-theme", "dark");

const app = await import(appPath);
// app.js boots itself when it finds #app-root, exactly as it does in a browser.
await new Promise((resolve) => setTimeout(resolve, 20));

// How many columns index.html declares for a table body, so a row that emits
// the wrong number of cells is caught rather than silently skewing the table.
function declaredColumns(tbodyId) {
  const upTo = html.slice(0, html.indexOf('id="' + tbodyId + '"'));
  const head = upTo.lastIndexOf("<thead>");
  if (head === -1) throw new Error("no thead before " + tbodyId);
  return (upTo.slice(head).match(/<th /g) || []).length;
}

const rowTarget = (attributes) => ({
  closest: (selector) => {
    const name = selector.slice(1, -1);
    if (!Object.prototype.hasOwnProperty.call(attributes, name)) return null;
    return { getAttribute: () => attributes[name] };
  },
});

if (mode === "ok") {
  check("boot_fetches_only_get_and_only_this_origin", () => {
    expect(fetchCalls.length === ENDPOINT_COUNT,
      "expected " + ENDPOINT_COUNT + " requests, saw " + fetchCalls.length);
    fetchCalls.forEach((call) => {
      expect(call.init && call.init.method === "GET", "a request was not a GET: " + JSON.stringify(call.init));
      expect(call.url.startsWith("/api/"), "a request left this origin: " + call.url);
      expect(call.url.indexOf("//") === -1, "a request looks absolute: " + call.url);
    });
    return fetchCalls.map((c) => c.url).join(",");
  });

  check("boot_leaves_no_error_and_finds_every_id", () => {
    expect(!el("global-error").visible(), "the error panel is showing: " + el("global-error").html());
  });

  check("overview_paints_the_status_line", () => {
    expect(el("ov-statusline").textContent.indexOf("Ran 2 of the last 7 nights") !== -1,
      "status line reads: " + el("ov-statusline").textContent);
  });

  check("staleness_banner_is_shown_when_the_data_is_old", () => {
    const banner = el("ov-banner");
    expect(banner.visible(), "the staleness banner is hidden");
    expect(banner.html().indexOf("4 days stale") !== -1, "banner reads: " + banner.html());
    expect(banner.getAttribute("data-state") === "partial", "banner state: " + banner.getAttribute("data-state"));
  });

  check("run_grid_renders_running_as_its_own_state", () => {
    const grid = el("ov-grid").html();
    expect(grid.indexOf('data-state="running"') !== -1, "no running cell was drawn");
    expect(grid.indexOf('<table class="run-grid"') !== -1, "the grid is not a run-grid table");
    const rows = (grid.match(/class="run-grid__stage"/g) || []).length;
    expect(rows === 5, "expected 5 stage rows, saw " + rows);
    const nights = (grid.match(/class="run-grid__night"/g) || []).length;
    expect(nights === 2, "expected 2 night columns, saw " + nights);
  });

  check("run_grid_undrawn_states_pass_through_rather_than_becoming_ok", () => {
    const grid = el("ov-grid").html();
    expect(grid.indexOf('data-state="refused"') !== -1, "the refused cell was dropped or remapped");
    expect(grid.indexOf('data-state="unreadable"') !== -1, "the unreadable cell was dropped or remapped");
  });

  check("run_grid_legend_names_every_state_present", () => {
    const legend = el("ov-grid-legend").html();
    ["ok", "partial", "running", "skipped", "refused", "unreadable"].forEach((state) => {
      expect(legend.indexOf('data-state="' + state + '"') !== -1, "legend omits " + state);
    });
    expect(legend.indexOf("unmapped") !== -1, "legend does not flag the unmapped states");
  });

  check("run_grid_footnote_states_the_two_caveats", () => {
    const foot = el("ov-grid-foot").html();
    expect(foot.indexOf("worst outcome") !== -1, "footnote omits the worst-outcome caveat");
    expect(foot.indexOf("never reported completion") !== -1, "footnote omits the stuck runs");
  });

  // A cell's colour is the worst RECORDED outcome, so a run that never
  // reported completion has no colour of its own on a night that also holds
  // finished runs. The fixture includes running members inside cells drawn
  // ok and partial. The mark and the tooltip are the only places
  // the operator can see them, so both are asserted.
  check("run_grid_marks_the_stuck_runs_its_colour_hides", () => {
    const grid = el("ov-grid").html();
    const cells = grid.match(/<td class="cell"[^>]*>/g) || [];
    const masked = cells.filter((c) => c.indexOf("data-hidden-runs=") !== -1);
    expect(masked.length >= 2, "expected the abandoned and the stuck cells to be marked, saw " + masked.length);
    const running = masked.filter((c) => c.indexOf("run status running") !== -1);
    expect(running.length >= 1, "no marked cell names the running run status: " + masked.join(" | "));
    running.forEach((c) => {
      expect(c.indexOf('data-state="running"') === -1,
        "the marked cell is itself drawn running, so it no longer exercises masking: " + c);
    });
    const abandoned = masked.filter((c) => c.indexOf("run status abandoned") !== -1);
    expect(abandoned.length >= 1, "no marked cell names the abandoned run status: " + masked.join(" | "));
    masked.forEach((c) => {
      expect(c.indexOf("worst RECORDED outcome") !== -1, "a marked cell does not say what its colour is: " + c);
    });
    return masked.length + " marked cell(s)";
  });

  check("run_grid_footnote_does_not_claim_a_visibility_the_grid_lacks", () => {
    const foot = el("ov-grid-foot").html();
    expect(foot.indexOf("render as their own state") === -1,
      "the footnote claims stuck runs render as their own state; on a night that also holds " +
      "finished runs they do not: " + foot);
    expect(foot.indexOf("corner mark") !== -1, "the footnote does not say where a stuck run IS visible: " + foot);
    expect(foot.indexOf("never counted as success") !== -1, "the footnote dropped the success caveat: " + foot);
  });

  // `refused` is a designed outcome (the call budget ran out before the stage
  // was asked), not a shape the dashboard failed to learn. AGENTS.md forbids
  // reading a run with no eval verdicts as a gate that failed, and the unmapped
  // alarm — red ink, dashed outline, "?" glyph — says exactly that.
  check("refused_is_a_drawn_state_not_an_unmapped_alarm", () => {
    const legend = el("ov-grid-legend").html();
    expect(legend.indexOf('data-state="refused"') !== -1, "legend omits refused");
    expect(/refused[^<]*\(unmapped\)/.test(legend) === false, "refused is labelled unmapped: " + legend);
    expect(legend.indexOf("the budget refused it") !== -1, "legend drops the refused meaning: " + legend);
    const grid = el("ov-grid").html();
    const refused = (grid.match(/<td class="cell"[^>]*data-state="refused"[^>]*>/g) || [])[0] || "";
    expect(refused !== "", "no refused cell was drawn");
    expect(refused.indexOf("no drawn state in the design system") === -1,
      "the refused cell's own tooltip calls it unmapped: " + refused);
  });

  check("grid_numbers_toggle_rewires_the_grid", () => {
    el("ov-grid-numbers").dispatch("click", {});
    expect(el("ov-grid").html().indexOf("run-grid run-grid--numbers") !== -1, "the numbers class was not applied");
    el("ov-grid-numbers").dispatch("click", {});
    expect(el("ov-grid").html().indexOf("run-grid--numbers") === -1, "the numbers class did not come off");
  });

  check("backlog_shows_two_rates_and_no_countdown", () => {
    const text = el("ov-backlog").html() + " " + el("ov-backlog-foot").html();
    expect(text.indexOf("1,863") !== -1, "queued is missing");
    expect(text.indexOf("80") !== -1, "capacity is missing");
    expect(text.indexOf("125.5") !== -1, "arrival rate is missing");
    expect(text.indexOf("+45.5") !== -1, "net rate is missing");
    expect(text.toLowerCase().indexOf("drain") === -1, "the backlog names a drain");
    expect(/\bin \d+ (nights|days)\b/.test(text) === false, "the backlog states a countdown");
    expect(text.indexOf("signal_then_recent") !== -1, "the queue order is not stated");
  });

  check("gate_health_shows_all_four_verdicts_and_no_rate", () => {
    const gate = el("ov-gate").html();
    ["gated_pass", "gated_fail", "ungated", "inconclusive"].forEach((verdict) => {
      expect(gate.indexOf('data-verdict="' + verdict + '"') !== -1, "verdict missing: " + verdict);
    });
    expect(gate.indexOf("%") === -1, "gate health rendered a percentage");
  });

  check("gate_health_renders_an_unknown_verdict_visibly", () => {
    const gate = el("ov-gate").html();
    expect(gate.indexOf('data-verdict="teleported"') !== -1, "the unknown verdict was dropped");
    expect(gate.indexOf("unknown verdict") !== -1, "the unknown verdict was not labelled as unknown");
  });

  check("gate_health_separates_harness_broken_from_rule_failed", () => {
    const gate = el("ov-gate").html();
    expect(gate.indexOf("The harness broke") !== -1, "harness failures are not named");
    expect(gate.indexOf("The rule failed") !== -1, "rule failures are not named");
    expect(gate.indexOf("could not reproduce") !== -1, "invalid evals are not named");
  });

  check("gate_health_reports_proposal_evals_not_the_table_count", () => {
    const gate = el("ov-gate").html();
    expect(gate.indexOf("2 of 10") !== -1, "the sentence does not separate proposal evals from the table");
  });

  check("confidence_renders_the_fraction_not_a_rate", () => {
    const text = el("ov-confidence").html();
    expect(text.indexOf("%") === -1, "a rate was rendered on a denominator of zero");
    expect(text.indexOf("no rule has ever been applied") !== -1, "the reason is missing");
  });

  check("inbox_follows_the_trust_model_and_names_the_blocker", () => {
    const inbox = el("ov-inbox").html();
    expect(inbox.indexOf(REVIEW.count + " items waiting on you") !== -1,
      "the queue line is missing: " + inbox);
    expect(inbox.indexOf("3 auto-applicable, blocked by --review-only") !== -1, "the blocker is not named");
    expect(inbox.indexOf('data-state="unknown_status"') !== -1, "an unknown proposal status was not flagged");
    expect(el("nav-inbox-count").textContent === String(REVIEW.count),
      "the nav count reads " + el("nav-inbox-count").textContent);
  });

  check("failure_panel_separates_fixed_from_still_happening", () => {
    const failures = el("ov-failures").html();
    expect(failures.indexOf("No answer we could read") !== -1, "the open failure is missing");
    expect(failures.indexOf("failure--fixed") !== -1, "the fixed failure is not muted");
    expect(failures.indexOf("budget_exhausted") !== -1, "budget keys are not reported");
    expect(failures.indexOf("Nothing was lost") !== -1, "the reassurance the panel needs is missing");
  });

  check("rules_table_renders_one_row_per_rule", () => {
    const rows = (el("rules-results").html().match(/data-rule-id="/g) || []).length;
    expect(rows === 3, "expected 3 rule rows, saw " + rows);
    expect(el("rules-meta").textContent.startsWith("3 rules"), "rule count missing from table header");
  });

  check("a_rule_with_no_title_shows_its_rule_not_its_uuid", () => {
    const html_ = el("rules-results").html();
    expect(html_.indexOf(">Resolve assets from the package directory.<") !== -1,
      "the heading was not derived from the rule text");
    expect(html_.indexOf(">c3333333<") !== -1, "the short id was dropped");
    expect(/<span class="strong">c3333333/.test(html_) === false,
      "a uuid is being used as the rule heading");
    // With no title the heading already IS the rule, so the second line must
    // carry the reason rather than repeat it.
    expect(html_.indexOf("Why: The invented package test omitted a required asset.") !== -1,
      "the row repeats the rule instead of giving its reason");
    // A rule that HAS a title still shows the rule text on the second line.
    expect(html_.indexOf("Check required response fields before saving a parsed record.") !== -1,
      "a titled rule lost its rule text");
  });

  check("every_table_row_emits_the_columns_index_html_declares", () => {
    const ruleColumns = declaredColumns("rules-results");
    const ruleRow = el("rules-results").html().split("</tr>")[0];
    const ruleCells = (ruleRow.match(/<td[ >]/g) || []).length;
    expect(ruleCells === ruleColumns,
      "the rules table declares " + ruleColumns + " columns and a row emits " + ruleCells);
    const projectColumns = declaredColumns("projects-tbody");
    const projectRow = el("projects-tbody").html().split("</tr>")[0];
    const projectCells = (projectRow.match(/<td[ >]/g) || []).length;
    expect(projectCells === projectColumns,
      "the projects table declares " + projectColumns + " columns and a row emits " + projectCells);
    return ruleColumns + "/" + projectColumns;
  });

  check("rules_table_escapes_hostile_text", () => {
    const html = el("rules-results").html();
    expect(html.indexOf("<script>") === -1, "an unescaped script tag reached the table");
    expect(html.indexOf("&lt;script&gt;") !== -1, "the hostile title was dropped instead of escaped");
    // A quote in data that lands inside title="..." would end the attribute and
    // let the rest of the value become markup. Ampersands must be escaped too,
    // or every entity in the data is silently re-interpreted.
    expect(html.indexOf('&quot;generation&quot;') !== -1, "a double quote reached an attribute unescaped");
    expect(html.indexOf('"generation"') === -1, "a raw double quote is sitting inside the markup");
    expect(html.indexOf("&amp; no producing call") !== -1, "an ampersand was not escaped");
    // Every quoted attribute must be balanced: an odd count means one was broken open.
    const quotes = (html.match(/"/g) || []).length;
    expect(quotes % 2 === 0, "the markup has an odd number of quotes, so an attribute is broken open");
  });

  check("rules_table_shows_the_four_verdicts_and_an_em_dash_for_none", () => {
    const html = el("rules-results").html();
    expect(html.indexOf('data-verdict="gated_fail"') !== -1, "gated_fail badge missing");
    expect(html.indexOf('data-verdict="inconclusive"') !== -1, "inconclusive badge missing");
    expect(html.indexOf("no proposal for this rule carries an eval_result_id") !== -1,
      "an ungated rule did not explain its em dash");
  });

  check("miner_generation_is_unknown_without_history", () => {
    const html = el("rules-results").html();
    expect(html.indexOf("No mining history was recorded for this learning.") !== -1,
      "the miner-generation gap is not explained");
    expect(html.indexOf("novalue") !== -1, "the miner generation is not rendered as an em dash");
  });

  check("search_narrows_the_table_and_reports_the_fraction", () => {
    const input = el("rules-search-input");
    input.value = "npm";
    input.dispatch("input", { target: input });
    const rows = (el("rules-results").html().match(/data-rule-id="/g) || []).length;
    expect(rows === 1, "expected 1 match for npm, saw " + rows);
    expect(el("rules-search-meta").textContent === "1 of 3",
      "search meta reads " + el("rules-search-meta").textContent);
  });

  check("search_with_no_match_explains_itself", () => {
    const input = el("rules-search-input");
    input.value = "zzzznotathing";
    input.dispatch("input", { target: input });
    expect(el("rules-results").html() === "", "rows survived a query that matches nothing");
    expect(el("rules-empty").visible(), "the empty state is hidden");
    expect(el("rules-empty").html().indexOf("does not do synonyms") !== -1,
      "the empty state does not explain how search works");
    input.value = "";
    input.dispatch("input", { target: input });
    expect(!el("rules-empty").visible(), "the empty state stayed after the query was cleared");
  });

  check("duplicate_families_render_the_designed_empty_state", () => {
    const html = el("rules-grouping").html();
    expect(html.indexOf("Family grouping is off") !== -1, "the empty state title is missing");
    expect(html.indexOf("no learning has a duplicate_of") !== -1, "the reason is missing");
  });

  check("clicking_a_rule_row_opens_the_inspector", () => {
    el("main").dispatch("click", { target: rowTarget({ "data-rule-id": "a1111111" }) });
    const inspector = el("inspector");
    expect(inspector.visible(), "the inspector stayed hidden");
    expect(inspector.className === "inspector", "the inspector class is " + inspector.className);
    expect(el("workspace").classList.contains("workspace--split"), "the workspace did not widen");
    const body = el("inspector-body").html();
    expect(body.indexOf("Why the machine believes it") !== -1, "the why section is missing");
    expect(body.indexOf("Enforcement gap") !== -1, "the enforcement-gap flag is missing");
    // Changed 2026-08-23: queries.rules() now selects diff_unified, so the
    // inspector shows the real patch instead of an empty state apologising for
    // a gap in the read layer.
    expect(body.indexOf("<pre class=\"scroll-x diff\">") !== -1, "the diff is not rendered: " + body.slice(0, 200));
    expect(body.indexOf("No diff in this payload") === -1, "the stale read-layer-gap empty state is still there");
    // The patch body itself, and the file it applies to, are both on screen.
    expect(body.indexOf("**Check required response fields**") !== -1,
      "the patch body is not rendered");
    expect(body.indexOf("/Users/example/.claude/CLAUDE.md") !== -1,
      "the diff does not say which file it patches");
  });

  check("the_inspector_verdict_names_what_the_verdict_was_about", () => {
    const body = el("inspector-body").html();
    expect(body.indexOf("The rule failed") !== -1, "the verdict class is not named");
    expect(body.indexOf("2 of 6 trials passed") !== -1, "the trial counts are missing");
  });

  check("inspector_tabs_switch_through_the_real_click_handler", () => {
    el("inspector-body").dispatch("click", { target: rowTarget({ "data-tab": "provenance" }) });
    const body = el("inspector-body").html();
    expect(body.indexOf("Where it came from") !== -1, "the provenance tab did not open");
    expect(body.indexOf("example/demo-service") !== -1, "provenance does not name the repo");
    expect(body.indexOf("11111111-1111-4111-8111-111111111111") !== -1, "provenance does not name the session");
  });

  check("evidence_tab_never_shows_the_sha1_as_the_error", () => {
    el("inspector-body").dispatch("click", { target: rowTarget({ "data-tab": "evidence" }) });
    const body = el("inspector-body").html();
    expect(body.indexOf("example-parser: required field missing") !== -1, "the readable text is missing");
    expect(/>[0-9a-f]{40}</.test(body) === false, "a bare sha1 was rendered as the error text");
    expect(body.indexOf("fingerprint") !== -1, "the fingerprint was not labelled");
    expect(body.indexOf("3 more incident(s) not shown") !== -1, "the evidence cap was not reported");
  });

  check("enforcement_gap_flag_shows_on_the_rule_that_has_one", () => {
    el("main").dispatch("click", { target: rowTarget({ "data-rule-id": "b2222222" }) });
    const body = el("inspector-body").html();
    expect(body.indexOf("Written but ignored") !== -1, "the enforcement gap is not flagged");
    expect(body.indexOf("Never accept-and-guess") !== -1, "the rule that was violated is not named");
  });

  check("projects_table_collapses_clones_and_offers_the_affordance", () => {
    const html = el("projects-tbody").html();
    const rows = (html.match(/<tr data-project-key="/g) || []).length;
    expect(rows === 3, "expected 3 repo rows, saw " + rows);
    expect(html.indexOf("2 clones") !== -1, "the clones affordance is missing");
    expect(el("projects-foot").html().indexOf("collapsed from 4 working copies") !== -1,
      "the footer does not say what was collapsed");
  });

  check("benefit_is_the_em_dash_the_data_sent_never_a_zero", () => {
    const html = el("projects-tbody").html();
    // Benefit is the last cell of every row, so read it positionally rather
    // than by a pattern that other em-dash cells also match.
    const rows = html.split("</tr>").filter((row) => row.indexOf("<tr ") !== -1);
    expect(rows.length === 3, "expected 3 repo rows, saw " + rows.length);
    rows.forEach((row) => {
      const cells = row.split("<td");
      const last = cells[cells.length - 1];
      expect(last.indexOf("novalue") !== -1, "the benefit cell is not a designed empty state: " + last);
      expect(last.indexOf("—") !== -1, "the benefit cell is not an em dash: " + last);
      expect(/>0</.test(last) === false, "the benefit cell rendered a zero: " + last);
      expect(last.indexOf("project_stats has no writer") !== -1,
        "the benefit cell gives no reason: " + last);
    });
  });

  check("exposure_distinguishes_missing_observations_and_partial_rates", () => {
    const html = el("projects-tbody").html();
    expect(html.indexOf("12 in 500 lines") === -1, "legacy queue/session totals became measurement");
    expect(html.indexOf("No physical-line observations") !== -1, "the reason is missing");
    expect(html.indexOf(" partial") !== -1, "partial coverage is hidden");
    expect(html.indexOf("75.8") !== -1, "the rate that IS supported was suppressed");
    // The unit lives in the column header, so it must not be re-stated per row.
    expect(html.indexOf("/100k lines<") === -1, "the unit is repeated on every row");
    expect(html.indexOf("1,200 incidents of this signal") !== -1,
      "the top-signal count was dropped instead of moved into the tooltip");
  });

  check("context_column_shows_observed_bytes_without_session_claims", () => {
    const html_ = el("projects-tbody").html();
    expect(html_.includes('16.0 kB</span><span class="caption muted"> observed'), 'observed bytes missing');
    expect(html_.includes('2 physical files observed at 2030-01-01'), 'observation time missing');
    expect(!html_.includes('loads into every session'), 'invented session receipt');
    expect(html_.includes('0 B'), 'observed empty inventory is not numeric zero');
  });

  check("a_repo_with_no_display_name_falls_back_to_its_key", () => {
    const html = el("projects-tbody").html();
    expect(html.indexOf("(no display name)") !== -1, "the fallback label is not marked");
    expect(html.indexOf('data-state="skipped"') !== -1, "the unresolved identity is not flagged");
  });

  check("clicking_a_project_opens_the_file_by_file_breakdown", () => {
    app.state.projectInventory["github:1000000001"] = {loaded:true, records:[{
      working_copy_id:"fixture-copy",working_copy:{normalized_path:"/fixture"},observed_at:"2030-01-01T00:00:00Z",status:"recorded",profile:"instruction-surfaces/2",issues:[],
      totals:{files:2,bytes:16000},context:{meaning:"Observed profile; session loading unverified",groups:[{provider:"claude",origin:"all",files:2,observed_bytes:16000,startup_bytes:12000,conditional_bytes:0,on_demand_bytes:4000,unresolved_bytes:0}]},
      files:WEIGHT_OK.files.map(f=>({...f,aliases:[f.path, "/fixture/CLAUDE.md"],loading_paths:[{provider:"claude",origin:"project",scope:{kind:f.always_loaded?"project_always_loaded":"on_demand"},conditions:[],import_chain:[],path:f.path,eligible_prefix_bytes:f.bytes}]}))
    }],count:1};
    el("main").dispatch("click", { target: rowTarget({ "data-project-key": "github:1000000001", "data-focus": "context" }) });
    const body = el("project-detail-body").html();
    expect(el("project-detail").visible() && !el("inspector").visible(), "project context did not use the dedicated page");
    expect(body.includes("Startup candidates"), "startup candidates missing");
    expect(body.includes("On-demand skill bodies"), "skill body budget missing");
    expect(!body.includes("every session"), "invented session receipt");
    expect(body.indexOf("12.0 kB") !== -1, "the always-loaded byte count is missing");
  });

  check("the_project_topology_tab_names_the_symlink", () => {
    el("project-detail-body").dispatch("click", { target: rowTarget({ "data-tab": "topology" }) });
    const body = el("project-detail-body").html();
    expect(body.includes("CLAUDE.md") && body.includes("Aliases:"), "recorded alias missing");
    expect(body.includes("Physical file:") && body.includes("AGENTS.md"), "physical target missing");
  });

  check("the_clones_chip_opens_the_working_copies_tab", () => {
    el("main").dispatch("click", {
      target: rowTarget({ "data-project-key": "github:1000000001", "data-focus": "copies" }),
    });
    const body = el("project-detail-body").html();
    expect(body.indexOf("2 working copies of one repository") !== -1, "the copies tab did not open");
    expect(body.indexOf("repository breadth used in routing") !== -1, "the reason clones collapse is not stated");
  });

  check("closing_the_inspector_gives_the_column_back", () => {
    el("inspector-close").dispatch("click", {});
    expect(!el("inspector").visible(), "the inspector stayed visible");
    expect(el("inspector").className === "", "the inspector kept its class: " + el("inspector").className);
    expect(!el("workspace").classList.contains("workspace--split"), "the workspace stayed wide");
  });

  check("boot_respects_a_theme_the_document_already_declares", () => {
    expect(root.getAttribute("data-theme") === "dark",
      "boot overwrote the declared theme with " + root.getAttribute("data-theme"));
    expect(el("theme-toggle").textContent === "Light theme",
      "the toggle label does not match the theme in force: " + el("theme-toggle").textContent);
    expect(el("theme-toggle").getAttribute("aria-pressed") === "true",
      "aria-pressed does not match the theme in force");
  });

  check("skip_navigation_focuses_content_without_changing_the_route", () => {
    const before = globalThis.location.hash;
    let prevented = false;
    el("skip-to-main").dispatch("click", { preventDefault() { prevented = true; } });
    expect(prevented, "the anchor would change the application's hash route");
    expect(el("main").focused === true, "skip navigation did not focus the content");
    expect(globalThis.location.hash === before, "skip navigation changed the selected view");
  });

  check("the_theme_toggle_sets_data_theme_both_ways", () => {
    const before = root.getAttribute("data-theme");
    el("theme-toggle").dispatch("click", {});
    const first = root.getAttribute("data-theme");
    expect(first === "dark" || first === "light", "no theme was set, saw " + first);
    el("theme-toggle").dispatch("click", {});
    const second = root.getAttribute("data-theme");
    expect(second !== first, "the second toggle did not flip the theme");
    expect(el("theme-toggle").textContent.length > 0, "the toggle label is empty");
    return before + "->" + first + "->" + second;
  });

  check("review_view_renders_one_card_per_family_leading_with_why", () => {
    globalThis.location.hash = "#/review";
    app.showRoute(app.parseRoute(globalThis.location.hash));
    const body = el("review-body").html();
    // Assert the LEAD element, not just the text: the same sentence also
    // appears in the per-proposal list, so a text-only check passed with the
    // lead paragraph deleted. ops/sabotage.sh found that.
    const lead = body.indexOf('class="review-card__why-label"');
    expect(lead !== -1, "the card has no lead-with-why element: " + body);
    expect(body.indexOf("Why this needs you:", lead) !== -1,
      "the lead element does not name itself: " + body);
    expect(body.indexOf("The eval could not show that this rule helps.", lead) !== -1,
      "the lead element does not carry the reason: " + body);
    expect(body.indexOf("Read enabled adapters from configuration before choosing one.") !== -1,
      "the card does not show the rule: " + body);
    // Open one family at a time and keep the remaining families in compact rows.
    // This fixture verifies both the open card and the next-family list.
    const open = (body.match(/review-card--open/g) || []).length;
    expect(open === 1, "expected exactly one open card, saw " + open);
    expect(body.indexOf('class="review-next"') !== -1,
      "the remaining families have no Next up list: " + body);
    expect(body.indexOf("/Users/x/repo/AGENTS.md") !== -1,
      "the open card does not name both of its targets: " + body);
    expect(el("review-count").textContent === "3",
      "the view count reads: " + el("review-count").textContent);
  });

  check("review_card_keeps_a_reason_for_every_proposal_in_the_family", () => {
    const body = el("review-body").html();
    expect(body.indexOf("The scenarios disagreed. Contrary evidence exists.") !== -1,
      "the second proposal's own reason is missing: " + body);
  });

  check("review_note_names_the_auto_appliable_and_the_unknown_statuses", () => {
    const note = el("review-note").textContent;
    expect(note.indexOf("30 more auto-apply") !== -1, "the note hides the auto-appliable: " + note);
    expect(note.indexOf("superseded") !== -1, "the note drops an unknown status: " + note);
  });

  await checkAsync("approving_a_family_submits_one_command_with_exact_revisions", async () => {
    const before = fetchCalls.length;
    el("main").dispatch("click", {
      target: rowTarget({ "data-decision": "approve", "data-learning-id": "L1" }),
    });
    // decideFamily submits one command, then refetches. Drain the microtask queue
    // until it settles rather than guessing a timeout.
    for (let i = 0; i < 50; i += 1) await new Promise((r) => setTimeout(r, 0));
    const posts = fetchCalls.slice(before).filter((c) => c.init && c.init.method === "POST");
    expect(posts.length === 1, "expected one atomic POST, saw " + posts.length);
    posts.forEach((call) => {
      expect(call.url === "/api/commands",
        "a decision went to the wrong URL: " + call.url);
      expect(JSON.parse(call.init.body).action === "approve",
        "a decision sent the wrong word: " + call.init.body);
      const members = JSON.parse(call.init.body).members;
      expect(members.length === 2 && members[0].proposal_id === "P1" && members[1].proposal_id === "P2", "wrong member set");
      expect(members[0].revision === "a".repeat(64) && members[1].revision === "b".repeat(64), "reviewed revisions missing");
    });
    expect(!el("global-error").visible(), "a successful decision raised an error panel");
    return posts.map((c) => c.url).join(",");
  });

  await checkAsync("reapproval_gets_a_new_key_but_an_uncertain_retry_reuses_its_key", async () => {
    const realFetch = globalThis.fetch;
    const requests = [];
    let uncertain = false;
    globalThis.fetch = async (url, init) => {
      if (url === "/api/commands" && init && init.method === "POST") {
        requests.push(JSON.parse(init.body));
        if (uncertain) throw new Error("connection ended before the response");
      }
      return realFetch(url, init);
    };
    async function approveAgain() {
      // Cancellation can return exactly the same proposal revision to Review.
      app.state.decided = {};
      el("main").dispatch("click", {
        target: rowTarget({ "data-decision": "approve", "data-learning-id": "L1" }),
      });
      for (let i = 0; i < 50; i += 1) await new Promise((r) => setTimeout(r, 0));
    }
    try {
      await approveAgain();
      await approveAgain();
      expect(requests.length === 2, "expected two explicit approval actions");
      expect(requests[0].request_key !== requests[1].request_key,
        "reapproval reused a completed request, so a cancelled command would be replayed");
      uncertain = true;
      await approveAgain();
      uncertain = false;
      await approveAgain();
      expect(requests.length === 4, "the uncertain approval was not retried");
      expect(requests[2].request_key === requests[3].request_key,
        "an uncertain response produced a second authorization instead of replaying the first");
    } finally {
      globalThis.fetch = realFetch;
    }
  });

  check("a_card_says_each_fact_once", () => {
    globalThis.location.hash = "#/review";
    app.showRoute(app.parseRoute(globalThis.location.hash));
    // L2 has ONE proposal: one reason, one target. Rendering the lead reason,
    // a per-proposal line repeating it, and a targets list repeating the path
    // is the same two facts three times.
    const card = app.renderReviewCard(
      app.state.review.families.find((f) => f.learning_id === "a1111111"), {}
    );
    const reason = "Not gated yet";
    const times = card.split(reason).length - 1;
    expect(times === 1, "the reason is printed " + times + " times on a one-proposal card");
    const path = "/Users/x/.claude/CLAUDE.md";
    const paths = card.split(path).length - 1;
    expect(paths === 1, "the target is printed " + paths + " times on a one-proposal card");
    // A single-proposal family has nothing a per-proposal list could add, so
    // the list must not render at all.
    expect(card.indexOf('class="review-proposal"') === -1,
      "a one-proposal card rendered a per-proposal breakdown: " + card);
  });

  check("a_card_whose_proposals_differ_still_shows_each_one", () => {
    // L1 has two proposals with DIFFERENT reasons and DIFFERENT targets, so
    // collapsing them would hide information the operator is deciding on.
    const card = app.renderReviewCard(
      app.state.review.families.find((f) => f.learning_id === "L1"), {}
    );
    expect(card.indexOf("The eval could not show") !== -1, "lost the first reason");
    expect(card.indexOf("The scenarios disagreed") !== -1, "lost the second reason");
    expect(card.indexOf("/Users/x/repo/AGENTS.md") !== -1, "lost the second target");
  });

  await checkAsync("a_refused_atomic_family_marks_no_member_approved", async () => {
    globalThis.location.hash = "#/review";
    app.showRoute(app.parseRoute(globalThis.location.hash));
    // A stale second member refuses the entire command.
    app.state.decided = {};
    failPostFor = "P2";
    el("main").dispatch("click", {
      target: rowTarget({ "data-decision": "approve", "data-learning-id": "L1" }),
    });
    for (let i = 0; i < 50; i += 1) await new Promise((res) => setTimeout(res, 0));
    failPostFor = null;

    const err = el("global-error");
    expect(err.visible(), "a failed decision was swallowed");
    expect(err.html().indexOf("2 of 2") !== -1,
      "the error does not say how many landed: " + err.html());
    expect(err.html().indexOf("P2") !== -1, "the error does not name the proposal: " + err.html());

    expect(!app.state.decided.P1 && !app.state.decided.P2, "a refused command marked a member approved");
    return err.html().slice(0, 80);
  });

  check("a_uniform_family_still_shows_which_proposal_landed", () => {
    // The collapse rule hides the per-proposal list when every proposal shares
    // a reason and a target, and the target list has ONE entry for two
    // proposals. On a partial failure that would show one mark for two
    // different outcomes — the operator could not tell which half to retry.
    const uniform = {
      learning_id: "U1", title: "t", rule_text: "a uniform family",
      why: "", evidence_count: 1, project_count: 1, size: 2,
      targets: ["/Users/x/.claude/CLAUDE.md"],
      reason_codes: ["inconclusive"], lead_reason: "The scenarios disagreed.",
      has_carve_out: false,
      proposals: [
        { id: "U-a", status: "inconclusive", target_path: "/Users/x/.claude/CLAUDE.md",
          target_kind: "global_claude_md", action: "add", diff_unified: "",
          eval_result_id: "", created_at: "2026-08-18T08:00:00Z",
          reason_code: "inconclusive", why_needs_you: "The scenarios disagreed.",
          carve_out: false },
        { id: "U-b", status: "inconclusive", target_path: "/Users/x/.claude/CLAUDE.md",
          target_kind: "global_claude_md", action: "add", diff_unified: "",
          eval_result_id: "", created_at: "2026-08-18T08:00:00Z",
          reason_code: "inconclusive", why_needs_you: "The scenarios disagreed.",
          carve_out: false },
      ],
    };
    const plain = app.renderReviewCard(uniform, {});
    expect((plain.match(/CLAUDE\.md/g) || []).length === 1,
      "an undecided uniform family should still say each fact once");

    const partly = app.renderReviewCard(uniform, { decided: { "U-a": "approved" } });
    // The card does not print proposal ids, so the operator distinguishes the
    // two by ROW: both proposals must appear, and exactly one must be marked.
    const rows = (partly.match(/class="review-proposal"/g) || []).length;
    expect(rows === 2, "expected a row per proposal on a partial decision, saw " + rows);
    const marks = (partly.match(/approved/g) || []).length;
    expect(marks === 1, "expected exactly one 'approved' mark, saw " + marks);
  });

  check("md_lite_renders_the_miners_emphasis_and_escapes_first", () => {
    // Invented inputs exercise bold, inline code, and unchanged plain text.
    // The following check verifies that formatting cannot introduce input tags.
    expect(app.mdLite("**do the thing**") === "<strong>do the thing</strong>",
      "bold not rendered: " + app.mdLite("**do the thing**"));
    expect(app.mdLite("use `--dry-run` first") === "use <code>--dry-run</code> first",
      "code not rendered: " + app.mdLite("use `--dry-run` first"));
    expect(app.mdLite("plain") === "plain", "plain text was altered");
  });

  check("md_lite_cannot_emit_a_tag_from_its_input", () => {
    // Escape THEN format. The reverse order is how a display helper becomes an
    // injection: these strings come from an LLM reading transcripts.
    const evil = '<img src=x onerror=alert(1)>**b**';
    const out = app.mdLite(evil);
    expect(out.indexOf("<img") === -1, "an input tag survived: " + out);
    expect(out.indexOf("&lt;img") !== -1, "the tag was not escaped: " + out);
    expect(out.indexOf("<strong>b</strong>") !== -1, "emphasis lost: " + out);
    const sneaky = app.mdLite("**<script>**");
    expect(sneaky.indexOf("<script") === -1, "a script tag survived: " + sneaky);
  });

  check("the_review_card_headline_uses_it", () => {
    globalThis.location.hash = "#/review";
    app.showRoute(app.parseRoute(globalThis.location.hash));
    const card = app.renderReviewCard(
      Object.assign({}, app.state.review.families[0], { rule_text: "**bolded rule**" }), {}
    );
    expect(card.indexOf("<strong>bolded rule</strong>") !== -1,
      "the card headline still shows raw markers: " + card.slice(0, 300));
  });

  check("v4_says_how_many_directories_it_left_out_and_why", () => {
    globalThis.location.hash = "#/projects";
    app.showRoute(app.parseRoute(globalThis.location.hash));
    const notes = el("projects-notes").html();
    expect(notes.indexOf("1,234") !== -1 || notes.indexOf("1234") !== -1,
      "the excluded count is not shown: " + notes.slice(0, 300));
    expect(notes.indexOf("678") !== -1 && notes.indexOf("556") !== -1,
      "the per-method split is missing: " + notes.slice(0, 300));
    expect(notes.indexOf("not recognized as Git repositories in this snapshot") !== -1,
      "the reason is missing: " + notes.slice(0, 300));
  });

  await checkAsync("a_double_click_does_not_report_a_false_failure", async () => {
    globalThis.location.hash = "#/review";
    app.showRoute(app.parseRoute(globalThis.location.hash));
    // The partial-failure check above deliberately leaves an error panel up;
    // clear it so this check measures its OWN effect and not that one's.
    el("global-error").style.display = "none";
    const before = fetchCalls.length;
    const target = rowTarget({ "data-decision": "approve", "data-learning-id": "L1" });
    // Two clicks with no await between them, which is what a double-click and a
    // held `a` key both produce.
    el("main").dispatch("click", { target: target });
    el("main").dispatch("click", { target: target });
    for (let i = 0; i < 60; i += 1) await new Promise((r) => setTimeout(r, 0));
    const posts = fetchCalls.slice(before).filter((c) => c.init && c.init.method === "POST");
    expect(posts.length === 1,
      "expected one atomic POST even with two clicks, saw " + posts.length);
    expect(!el("global-error").visible(),
      "a double-click raised an error panel: " + el("global-error").html());
    return String(posts.length);
  });

  await checkAsync("the_in_flight_guard_is_released_so_the_button_is_not_wedged", async () => {
    // A guard that never releases is worse than no guard: the operator decides
    // one family and every later click is silently ignored. ops/sabotage.sh
    // showed the release line was uncovered, so this asserts it directly.
    globalThis.location.hash = "#/review";
    app.showRoute(app.parseRoute(globalThis.location.hash));
    el("global-error").style.display = "none";
    el("main").dispatch("click", {
      target: rowTarget({ "data-decision": "reject", "data-learning-id": "L1" }),
    });
    for (let i = 0; i < 60; i += 1) await new Promise((r) => setTimeout(r, 0));
    const stuck = Object.keys(app.state.deciding);
    expect(stuck.length === 0,
      "the in-flight guard was never released for: " + stuck.join(", "));
    return "released";
  });

  check("every_review_diff_is_available_without_truncation", () => {
    globalThis.location.hash = "#/review";
    app.showRoute(app.parseRoute(globalThis.location.hash));
    const body = el("review-body").html();
    expect(body.indexOf("[object Object]") === -1,
      "a helper's return value was rendered instead of its text");
    expect(body.indexOf("+ discover enabled adapters") !== -1, "the diff did not render: " + body.slice(0, 400));
    const longDiff = '+ ' + 'all of the reviewed text '.repeat(1000) + 'LAST REVIEWED LINE';
    expect(app.renderReviewDiff(longDiff).includes('LAST REVIEWED LINE'), 'full diff is inaccessible');
    expect(body.includes('Complete combined edit'), 'there is no selected-edit preview');
  });

  check("the_view_and_the_nav_do_not_label_two_numbers_the_same_way", () => {
    const navCount = el("nav-inbox-count").textContent;
    const viewCount = el("review-count").textContent;
    expect(navCount === viewCount,
      "the nav says " + navCount + " waiting and the view says " + viewCount);
    const saved = app.state.review;
    try {
      app.state.review = { ...saved, count: 0, families: [] };
      app.paintReview();
      expect(el("nav-inbox-count").textContent === "0", "the queue emptied but the nav badge is stale");
      app.paintOverview();
      expect(el("nav-inbox-count").textContent === "0", "the older Overview response restored a stale badge");
    } finally {
      app.state.review = saved;
      app.paintReview();
    }
  });

  check("j_and_k_move_the_selection_and_stop_at_the_ends", () => {
    globalThis.location.hash = "#/review";
    app.showRoute(app.parseRoute(globalThis.location.hash));
    app.state.selectedFamily = "";
    expect(pressKey("j"), "j was not handled on the review view");
    expect(app.state.selectedFamily === "L1", "j did not select the first card: " + app.state.selectedFamily);
    pressKey("j");
    expect(app.state.selectedFamily === "a1111111", "j did not advance: " + app.state.selectedFamily);
    pressKey("j");
    expect(app.state.selectedFamily === "a1111111", "j ran off the end: " + app.state.selectedFamily);
    pressKey("k");
    expect(app.state.selectedFamily === "L1", "k did not go back: " + app.state.selectedFamily);
    pressKey("k");
    expect(app.state.selectedFamily === "L1", "k ran off the start: " + app.state.selectedFamily);
    return app.state.selectedFamily;
  });

  check("the_first_j_moves_off_the_card_that_is_already_open", () => {
    // The bug this pins was invisible to the two scenarios above, because they
    // set `selectedFamily = ""` by hand and then press a key -- driving the
    // handler without ever painting. In a real browser the view had ALREADY
    // fallen back to families[0] while the state still said "", so the first
    // `j` resolved indexOf("") === -1, moved to index 0, and re-rendered the
    // card that was open. Paint the empty selection before pressing the key
    // so this test exercises the initial visible state.
    globalThis.location.hash = "#/review";
    app.showRoute(app.parseRoute(globalThis.location.hash));
    app.state.selectedFamily = "";
    app.paintReview();
    const shown = app.state.selectedFamily;
    expect(shown === "L1",
      "painting did not seed the selection to the card it opened: " + shown);
    pressKey("j");
    expect(app.state.selectedFamily !== shown,
      "the first j left the selection on the card already open: " + app.state.selectedFamily);
    return shown + "->" + app.state.selectedFamily;
  });

  check("a_shared_target_is_not_reprinted_once_per_proposal", () => {
    // L1's proposals differ in REASON, so the per-proposal breakdown renders.
    // When they share one target the breakdown must not repeat it: the targets
    // table above already named it, and printing it per proposal put the same
    // path on screen three times. Found by looking at the rendered page, not
    // by a test.
    const family = app.state.review.families.find((f) => f.learning_id === "L1");
    const shared = JSON.parse(JSON.stringify(family));
    shared.proposals.forEach((p) => { p.target_path = "/Users/x/.claude/CLAUDE.md"; });
    shared.target_rows = [{ path: "/Users/x/.claude/CLAUDE.md", count: 2, kind: "global_claude_md", note: "" }];
    const card = app.renderTargetRows(shared) + app.renderProposalBreakdown(shared, {});
    const times = card.split("/Users/x/.claude/CLAUDE.md").length - 1;
    expect(times === 1, "a shared target is printed " + times + " times");
    // ...and when they genuinely differ, every path must still be visible.
    const differing = app.state.review.families.find((f) => f.learning_id === "L1");
    const both = app.renderReviewCard(differing, {});
    expect(both.indexOf("/Users/x/repo/AGENTS.md") !== -1,
      "a differing target vanished from the breakdown: " + both);
  });

  check("moving_the_selection_scrolls_to_it_and_focuses_it", () => {
    globalThis.location.hash = "#/review";
    app.showRoute(app.parseRoute(globalThis.location.hash));
    lastSelectedStub = makeCardStub();
    app.state.selectedFamily = "";
    pressKey("j");
    expect(querySelectorCalls.indexOf(".review-card--selected") !== -1,
      "the app never looked for the selected card");
    expect(lastSelectedStub.scrolledInto !== null,
      "the selected card was not scrolled into view");
    expect(lastSelectedStub.scrolledInto.block === "nearest",
      "scrolled with " + JSON.stringify(lastSelectedStub.scrolledInto) +
      "; nearest avoids jumping a card that is already visible");
    expect(lastSelectedStub.focused !== null, "the selected card was not focused");
    expect(lastSelectedStub.focused.preventScroll === true,
      "focus() was allowed to scroll again, undoing block:nearest");
    lastSelectedStub = null;
  });

  check("the_selected_card_is_marked_in_the_markup", () => {
    expect(el("review-body").html().indexOf("review-card--selected") !== -1,
      "nothing shows which card is selected");
  });

  check("a_review_key_is_ignored_while_typing_in_a_text_field", () => {
    app.state.selectedFamily = "L1";
    const handled = pressKey("a", { target: { tagName: "INPUT" } });
    expect(!handled, "`a` was stolen from a text input");
  });

  check("review_keys_do_nothing_on_another_view", () => {
    globalThis.location.hash = "#/rules";
    app.showRoute(app.parseRoute(globalThis.location.hash));
    expect(!pressKey("j"), "j acted while the rules view was showing");
    globalThis.location.hash = "#/review";
    app.showRoute(app.parseRoute(globalThis.location.hash));
  });

  check("a_and_r_refuse_to_decide_when_nothing_is_selected", () => {
    app.state.selectedFamily = "";
    const before = fetchCalls.length;
    expect(!pressKey("a"), "`a` decided something with no card selected");
    expect(fetchCalls.length === before, "a request went out with nothing selected");
  });

  await checkAsync("r_rejects_the_selected_family_over_the_wire", async () => {
    app.state.selectedFamily = "a1111111";
    const before = fetchCalls.length;
    pressKey("r");
    for (let i = 0; i < 50; i += 1) await new Promise((res) => setTimeout(res, 0));
    const posts = fetchCalls.slice(before).filter((c) => c.init && c.init.method === "POST");
    expect(posts.length === 1, "expected one POST for a one-proposal family, saw " + posts.length);
    expect(posts[0].url === "/api/commands" && JSON.parse(posts[0].init.body).action === "reject_target",
      "r sent: " + posts[0].init.body);
    expect(JSON.parse(posts[0].init.body).members[0].revision === "c".repeat(64), "r lost the reviewed revision");
    return posts[0].url;
  });

  check("the_rules_inspector_links_into_the_queue_and_states_the_reason", () => {
    globalThis.location.hash = "#/rules";
    app.showRoute(app.parseRoute(globalThis.location.hash));
    el("main").dispatch("click", { target: rowTarget({ "data-rule-id": "a1111111" }) });
    const html = el("inspector-body").html();
    expect(html.indexOf('href="#/review"') !== -1, "the inspector does not link into V3: " + html);
    expect(html.indexOf("1 decision from this rule need review") !== -1,
      "the link does not say how many: " + html);
    expect(html.indexOf("Not gated yet") !== -1, "the link does not state the reason: " + html);
  });

  check("the_rules_inspector_never_offers_to_decide", () => {
    const html = el("inspector-body").html();
    expect(html.indexOf("data-decision") === -1,
      "D1 says there is one write surface; the Rules inspector grew a second: " + html);
    expect(html.indexOf("<button") === -1 || html.indexOf("Approve") === -1,
      "the Rules inspector offers an approve control: " + html);
  });

  check("a_rule_with_nothing_waiting_says_so_rather_than_going_blank", () => {
    el("main").dispatch("click", { target: rowTarget({ "data-rule-id": "b2222222" }) });
    const html = el("inspector-body").html();
    expect(html.indexOf("Nothing from this rule is waiting on you.") !== -1,
      "the empty case renders blank: " + html);
  });

  await checkAsync("rollback_clicks_reuse_uncertain_requests_and_reload_saved_progress", async () => {
    if (app.state.operations.promise) await app.state.operations.promise;
    const originalFetch = globalThis.fetch;
    const destination = {mode: "file", target_path: "/invented/rules.md", branch_name: ""};
    const preview = {ready: true, revision: "a".repeat(64), diff_unified: "-ADDED\\n+before\\n",
      source: {destination, application_id: "application-1", affected_members: [{proposal_id: "rollback-p1"}]}};
    const operation = {id: "rollback-operation", kind: "rollback", proposal_id: "rollback-p1", state: "queued",
      created_at: "2026-09-13T01:00:00Z", updated_at: "2026-09-13T01:00:00Z", destination,
      controls_available: true, can_retry: false, can_cancel: true, result: {}};
    let accepted = false, uncertain = true;
    const requests = [];
    const response = (body) => ({ok: true, status: 200, json: async () => structuredClone(body)});
    globalThis.fetch = async (url, init) => {
      if (url.startsWith("/api/operations")) return response({operations: accepted ? [operation] : [], next_cursor: null});
      if (url.endsWith("/rollback-preview")) return response({...preview, active_operation: accepted ? operation : null});
      if (url === "/api/commands" && init && init.method === "POST") {
        requests.push(JSON.parse(init.body)); accepted = true;
        if (uncertain) throw new Error("response was lost after recording intent");
        return response(operation);
      }
      return originalFetch(url, init);
    };
    const settle = async () => { for (let i = 0; i < 50; i += 1) await new Promise((resolve) => setTimeout(resolve, 0)); };
    try {
      app.state.operations = {items: [], loaded: true, nextCursor: null, generation: 0};
      app.state.rollback = {proposalId: "", generation: 0};
      globalThis.location.hash = "#/review/rollback/rollback-p1";
      app.showRoute(app.parseRoute(globalThis.location.hash));
      await settle();
      expect(el("rollback-body").html().includes("Roll back this change"), "the route never loaded an actionable inverse");
      el("main").dispatch("click", {target: rowTarget({"data-rollback-submit": "true"})});
      await settle();
      expect(requests.length === 1 && requests[0].preview_revision === preview.revision, "the click did not bind its reviewed inverse");
      expect(el("rollback-body").html().includes("response was lost"), "an uncertain response vanished");
      uncertain = false;
      el("main").dispatch("click", {target: rowTarget({"data-rollback-submit": "true"})});
      await settle();
      expect(requests.length === 2 && requests[0].request_key === requests[1].request_key, "the uncertain inverse got a new request identity");
      expect(app.state.rollback.operationId === operation.id, "the saved operation was not displayed");
      app.state.rollback = {proposalId: "", generation: 0};
      app.showRoute(app.parseRoute(globalThis.location.hash));
      await settle();
      expect(app.state.rollback.operationId === operation.id, "reopening the route lost queued progress");
      expect(!el("rollback-body").html().includes("data-rollback-submit"), "a queued inverse reopened as a new actionable request");
      el("main").dispatch("click", {target: rowTarget({"data-rollback-submit": "true"})});
      await settle();
      expect(requests.length === 2, "an in-flight inverse submitted again");
    } finally { globalThis.fetch = originalFetch; }
  });

  await checkAsync("operation_controls_retain_uncertain_keys_and_do_not_trigger_review_shortcuts", async () => {
    const originalFetch = globalThis.fetch;
    const operation = {id: "control-operation", kind: "rollback", proposal_id: "control-p1", state: "failed",
      created_at: "2026-09-13T01:00:00Z", updated_at: "2026-09-13T01:00:00Z",
      destination: {mode: "file", target_path: "/invented/rules.md", branch_name: ""},
      controls_available: true, can_retry: true, can_cancel: true, result: {}};
    let uncertain = true;
    const requests = [];
    globalThis.fetch = async (url, init) => {
      if (url.startsWith("/api/operations")) return {ok:true, json:async()=>({operations:[operation],next_cursor:null})};
      if (url === "/api/commands" && init && init.method === "POST") {
        requests.push(JSON.parse(init.body));
        if (uncertain) throw new Error("response missing");
        return {ok:true,json:async()=>operation};
      }
      return originalFetch(url, init);
    };
    const settle = async () => { for (let i=0;i<50;i+=1) await new Promise((resolve)=>setTimeout(resolve,0)); };
    try {
      app.state.operations = {items:[operation],loaded:true,generation:0};
      app.state.rollback = {proposalId:"",generation:0};
      app.state.route="review";
      const click=()=>el("main").dispatch("click",{target:rowTarget({"data-operation-action":"retry_operation","data-operation-id":operation.id})});
      click(); await settle(); uncertain=false; click(); await settle();
      expect(requests.length===2 && requests[0].request_key===requests[1].request_key,"uncertain recovery did not reuse its key");
      click(); await settle();
      expect(requests.length===3 && requests[2].request_key!==requests[1].request_key,"a later explicit recovery reused acknowledged intent");
      const before=fetchCalls.length;
      for (const selector of ["#review-operations","#review-rollback"]) {
        pressKey("a",{target:{closest:(query)=>query===selector ? {} : null}});
        pressKey("r",{target:{closest:(query)=>query===selector ? {} : null}});
      }
      await settle();
      expect(fetchCalls.length===before,"a recovery-panel key decided an unrelated review proposal");
    } finally { globalThis.fetch=originalFetch; }
  });

  await checkAsync("target_rejection_uses_the_selection_and_lesson_rejection_is_explicit", async () => {
    const originalFetch=globalThis.fetch;
    const requests=[];
    let uncertain=true;
    globalThis.fetch=async(url,init)=>{
      if(url==="/api/commands" && init && init.method==="POST") {
        requests.push(JSON.parse(init.body));
        if(uncertain) throw new Error("decision response lost");
      }
      return originalFetch(url,init);
    };
    try {
      app.state.reviewExcluded={P2:true}; app.state.reviewPreviews={}; app.state.decided={}; app.state.commandRequests={};
      app.state.route="review"; app.state.selectedFamily="L1";
      await app.decideFamily("L1","reject");
      uncertain=false;
      await app.decideFamily("L1","reject");
      expect(requests.length===2 && requests[0].request_key===requests[1].request_key,"uncertain rejection lost its identity");
      expect(requests[0].action==="reject_target" && requests[0].members.length===1 && requests[0].members[0].proposal_id==="P1","target rejection ignored the selection");
      expect(!("preview_revision" in requests[0]),"rejection depended on an executable combined edit");
      await app.decideFamily("L1","reject_lesson");
      expect(requests[2].action==="reject_lesson" && requests[2].request_key!==requests[1].request_key,"lesson rejection reused target scope");
      const body=app.renderReviewActions(app.state.review.families.find(f=>f.learning_id==="L1"));
      expect(body.includes('data-decision="reject_lesson"') && body.includes("other targets stay available"),"the distinct scopes are not explained");
      const history=app.renderDeliveryHistory({loaded:true,items:[{id:"rejected",action:"reject_target",state:"completed",result:{suppressed_proposal_ids:["P1"]}}]});
      expect(history.includes("Targets rejected") && !history.includes("target delivered") && !history.includes("Upgrade the state"),"rejection history pretends to be file delivery");
    } finally { globalThis.fetch=originalFetch; app.state.reviewExcluded={}; }
  });


  await checkAsync("recovery_binds_known_target_and_reuses_uncertain_intent", async()=>{
    const originalFetch=globalThis.fetch,requests=[];let latest=null,uncertain=true;
    const selection={learning_id:"L1",mode:"correct_target",target_id:"target-id",proposal_ids:[]};
    const source={learning:{rule_text:"Inspect the original request."},mode:"correct_target",proposal_ids:[],destination:{target_path:"/synthetic/CLAUDE.md",mode:"file"}};
    const command={id:"recovery-command",action:"propose_recovery",selection,learning_id:"L1",state:"queued",created_at:"2026-01-01T00:00:00Z",max_model_calls:1,targets:[],result:{},budget:{consumed:{cheap:0,strong:0,gate:0}}};
    globalThis.fetch=async(url,init)=>{
      if(url.endsWith("/recovery-options"))return {ok:true,json:async()=>({rule_text:source.learning.rule_text,targets:[{id:"target-id",label:"Global Claude instructions",available:true,modes:["correct_target"],destination:source.destination}],unavailable:[]})};
      if(url.includes("/recovery-preview?"))return {ok:true,json:async()=>({revision:"e".repeat(64),source:{snapshot:source},latest_job:latest,max_model_calls:1,meaning:"Manual Review."})};
      if(url==="/api/commands" && init && init.method==="POST"){
        requests.push(JSON.parse(init.body));latest={id:command.id,state:command.state};if(uncertain)throw new Error("response lost");return {ok:true,json:async()=>command};
      }
      if(url.startsWith("/api/commands?"))return {ok:true,json:async()=>({commands:latest?[command]:[],next_cursor:null})};
      return originalFetch(url,init);
    };
    try {
      app.state.evalRequests={};app.state.route="review";
      await app.openRecoveryJob({...selection,target_id:""});
      expect(!requests.length && el("eval-job-body").html().includes("Global Claude instructions"),"target selection started work or omitted known destinations");
      app.state.reviewPreviews["hook-source"]={data:{members:[{}],targets:[{destination:{target_kind:"hook",target_path:"/synthetic/settings.json",mode:"file"},target_key:"hook-id",proposal_ids:["P-hook"],state:"conflict",detail:"empty diff"}]}};
      const hook=app.renderSelectedPreview({learning_id:"hook-source"});
      expect(hook.includes("generate this hook proposal") && hook.includes("%22mode%22%3A%22hook%22"),"a hook placeholder links to unsupported text regeneration");
      delete app.state.reviewPreviews["hook-source"];

      await app.openRecoveryJob(selection);
      expect(!requests.length && el("eval-job-body").html().includes("Maximum: 1 logical model call"),"recovery omitted its bound");
      await app.submitEvalJob();uncertain=false;await app.submitEvalJob();
      expect(requests.length===2 && requests[0].request_key===requests[1].request_key,"uncertain recovery created another request");
      expect(requests[0].target_id==="target-id" && requests[0].learning_id==="L1" && !requests[0].target_path,"recovery used an arbitrary path");
      await app.openRecoveryJob(selection);await app.submitEvalJob();expect(requests.length===2,"reloading a recovery renewed its reservation");
      const html=app.renderDeliveryHistory({loaded:true,items:[{...command,state:"completed",result:{proposal_id:"P-recovery",supported:true,explanation:"Selected target."}}]});
      expect(html.includes("#/review/proposal/P-recovery") && !html.includes("targets delivered"),"recovery omitted manual Review or claimed delivery");
    } finally {globalThis.fetch=originalFetch;}
  });

  await checkAsync("selected_mining_previews_one_incident_and_reuses_uncertain_intent", async()=>{
    const originalFetch=globalThis.fetch, requests=[];let latest=null, uncertain=true;
    const command={id:"mine-command",action:"mine_incident",incident_id:"I1",state:"queued",created_at:"2026-01-01T00:00:00Z",max_model_calls:1,members:[],targets:[],result:{},controls_available:true,
      budget:{maximum:{cheap:1,strong:0,gate:0},consumed:{cheap:0,strong:0,gate:0}},stages:[{stage:"mine_agentic",maximum:1}]};
    globalThis.fetch=async(url,init)=>{
      if(url.endsWith("/mining-preview"))return {ok:true,json:async()=>({ready:true,revision:"f".repeat(64),incident_id:"I1",latest_job:latest,max_model_calls:1,meaning:"Returns generated changes to manual Review.",source_summary:{incident:{id:"I1",signal_type:"correction",ts:"now",matched_text:"invented evidence"},coverage:{kind:"full_session",truncations:2},files:[{name:"transcript.md",bytes:100}],learning_count:2,instruction_count:3}})};
      if(url==="/api/commands" && init && init.method==="POST"){
        requests.push(JSON.parse(init.body));latest={id:command.id,state:command.state};if(uncertain)throw new Error("response lost");return {ok:true,json:async()=>command};
      }
      if(url.startsWith("/api/commands?"))return {ok:true,json:async()=>({commands:latest?[command]:[],next_cursor:null})};
      if(url.startsWith("/api/incidents?"))return {ok:true,json:async()=>({items:[],count:0,next_cursor:null})};
      return originalFetch(url,init);
    };
    try {
      app.state.evalRequests={};app.state.route="review";
      await app.openEvalJob("I1",{action:"mine_incident"});
      expect(!requests.length && el("eval-job-body").html().includes("Maximum: 1 logical model call"),"mining omitted its bound or started during preview");
      await app.submitEvalJob();uncertain=false;await app.submitEvalJob();
      expect(requests.length===2 && requests[0].request_key===requests[1].request_key,"mining lost-response replay renewed the reservation");
      expect(requests[0].incident_id==="I1" && !requests[0].proposal_id && requests[0].action==="mine_incident","mining named a proposal or an arbitrary path instead of the selected incident");
      await app.openEvalJob("I1",{action:"mine_incident"});await app.submitEvalJob();
      expect(requests.length===2 && app.state.evalJob.commandId===command.id,"reload created another mine request");
      const html=app.renderDeliveryHistory({loaded:true,items:[{...command,state:"completed",result:{outcome:"new",summary:"invented lesson",proposal_ids:["P-new"],learning_id:"L-new"}}]});
      expect(html.includes("Selected incident mining") && html.includes("#/review/proposal/P-new") && !html.includes("targets delivered"),"mining completion was shown as delivery or omitted its Review destination");
      expect(html.includes("Inspect mining result") && !html.includes('data-eval-new="true"'),"a completed mining job offers another attempt against an already handled incident");
      const n=requests.length;
      pressKey("a",{target:{closest:q=>q==="#review-incidents"?{}:null}});pressKey("r",{target:{closest:q=>q==="#review-eval"?{}:null}});
      await new Promise(resolve=>setTimeout(resolve,0));expect(requests.length===n,"mining-panel shortcuts decided another proposal");
    } finally {globalThis.fetch=originalFetch;}
  });

  await checkAsync("reapplication_returns_to_review_and_reuses_uncertain_intent", async () => {
    const originalFetch=globalThis.fetch, requests=[];
    let uncertain=true, latest=null;
    const created={id:"reapply-request",action:"request_reapplication",state:"completed",created_at:"2026-01-01T00:00:00Z",
      max_model_calls:0,members:[],targets:[],result:{proposal_id:"P2",original_proposal_id:"P1"},controls_available:true};
    globalThis.fetch=async(url,init)=>{
      if(url.endsWith("/reapplication-preview")) return {ok:true,json:async()=>({ready:true,revision:"e".repeat(64),max_model_calls:0,
        source:{proposal:{id:"P1"},destination:{target_path:"/synthetic/rules.md",mode:"file"}},diff_unified:"a retained proposed change",latest_request:latest})};
      if(url==="/api/commands" && init && init.method==="POST") {
        requests.push(JSON.parse(init.body));latest={id:created.id,proposal_id:"P2"};
        if(uncertain)throw new Error("response lost");
        return {ok:true,json:async()=>created};
      }
      if(url.startsWith("/api/commands?"))return {ok:true,json:async()=>({commands:latest?[created]:[],next_cursor:null})};
      return originalFetch(url,init);
    };
    try {
      app.state.evalRequests={};app.state.route="review";
      await app.openEvalJob("P1",{action:"request_reapplication"});
      expect(requests.length===0 && el("eval-job-body").html().includes("no model calls"),"the reapplication preview created work or omitted its zero-call contract");
      await app.submitEvalJob();uncertain=false;await app.submitEvalJob();
      expect(requests.length===2 && requests[0].request_key===requests[1].request_key,"lost reapplication response created another intent");
      expect(requests[0].action==="request_reapplication" && requests[0].preview_revision==="e".repeat(64),"reapplication authorized another action or unreviewed content");
      await app.openEvalJob("P1",{action:"request_reapplication"});await app.submitEvalJob();
      expect(requests.length===2 && app.state.evalJob.resultProposalId==="P2","reload lost the new Review proposal");
      const html=app.renderDeliveryHistory({loaded:true,items:[created]});
      expect(html.includes("Reapplication proposed") && html.includes("#/review/proposal/P2") && !html.includes("targets delivered"),"draft creation was described as instruction delivery");
    } finally {globalThis.fetch=originalFetch;}
  });

  await checkAsync("eval_jobs_preview_cost_recover_requests_and_isolate_shortcuts", async () => {
    const originalFetch=globalThis.fetch, requests=[];
    let uncertain=true, latest=null;
    const job={id:"job-fixture",action:"regenerate_eval",state:"running",proposal_id:"P1",max_model_calls:21,
      budget:{maximum:{cheap:0,strong:0,gate:21},consumed:{cheap:0,strong:0,gate:1},remaining:{cheap:0,strong:0,gate:20}},
      stages:[{stage:"eval_gen",maximum:3},{stage:"grade",maximum:18}],can_cancel:true,can_retry:false,result:{}};
    globalThis.fetch=async(url,init)=>{
      if(url.endsWith("/eval-preview"))return {ok:true,json:async()=>({proposal_id:"P1",revision:"f".repeat(64),max_model_calls:21,
        source:{snapshot:{learning:{rule_text:"An invented rule"}}},plan:{stages:job.stages},latest_job:latest})};
      if(url==="/api/commands" && init && init.method==="POST"){
        requests.push(JSON.parse(init.body));latest={id:job.id,state:job.state};
        if(uncertain)throw new Error("response lost");
        return {ok:true,json:async()=>job};
      }
      if(url.startsWith("/api/commands?"))return {ok:true,json:async()=>({commands:latest?[job]:[],next_cursor:null})};
      return originalFetch(url,init);
    };
    try {
      app.state.evalRequests={};app.state.route="review";
      await app.openEvalJob("P1");
      expect(requests.length===0 && el("eval-job-body").html().includes("At most 21 model calls"),"opening the cost preview authorized work or hid its limit");
      await app.submitEvalJob();uncertain=false;await app.submitEvalJob();
      expect(requests.length===2 && requests[0].request_key===requests[1].request_key,"an uncertain job obtained a fresh budget key");
      expect(requests[0].preview_revision==="f".repeat(64),"the request did not bind the reviewed cost");
      await app.openEvalJob("P1");await app.submitEvalJob();
      expect(requests.length===2 && app.state.evalJob.commandId===job.id,"reload offered a second paid request");
      await app.openEvalJob("P1",{newAttempt:true});await app.submitEvalJob();
      expect(requests.length===3 && requests[2].request_key!==requests[1].request_key,"an explicit new attempt reused old intent");
      const html=app.renderDeliveryHistory({loaded:true,items:[job]});
      expect(html.includes("Running") && !html.includes("Delivering") && html.includes("1 of at most 21"),"job progress describes file delivery");
      const failed=app.renderDeliveryHistory({loaded:true,items:[{...job,state:"failed",error_code:"GateNotRun",error_detail:"No verdict",failure_taxonomy:{gate_budget_exhausted:1}}]});
      expect(failed.includes("GateNotRun") && failed.includes("gate_budget_exhausted (1)"),"job history hid the failure or refusal cause");
      const before=requests.length;
      pressKey("a",{target:{closest:q=>q==="#review-eval"?{}:null}});
      pressKey("r",{target:{closest:q=>q==="#review-eval"?{}:null}});
      await new Promise(resolve=>setTimeout(resolve,0));expect(requests.length===before,"job-panel shortcuts decided another proposal");
    } finally {globalThis.fetch=originalFetch;}
  });


  check("an_unknown_route_is_loud", () => {
    globalThis.location.hash = "#/nope";
    app.showRoute(app.parseRoute(globalThis.location.hash));
    expect(el("global-error").visible(), "an unknown view was silently redirected");
    expect(el("global-error").html().indexOf("No view named") !== -1,
      "the error does not name the problem: " + el("global-error").html());
  });
}

if (mode === "fail") {
  check("a_failed_endpoint_is_loud_and_names_the_request", () => {
    const error = el("global-error");
    expect(error.visible(), "a 404 was swallowed");
    const html = error.html();
    expect(html.indexOf("/api/rules") !== -1, "the error does not name the URL: " + html);
    expect(html.indexOf("404") !== -1, "the error does not name the status: " + html);
    expect(html.indexOf("Could not read 1 of " + ENDPOINT_COUNT) !== -1,
      "the error does not say how many failed: " + html);
  });

  check("a_failed_endpoint_does_not_blank_the_ones_that_worked", () => {
    expect(el("ov-statusline").textContent.indexOf("Ran 2 of the last 7 nights") !== -1,
      "the overview was blanked by an unrelated failure");
    expect(el("rules-results").html() === "", "the rules table rendered from a failed request");
  });
}

process.stdout.write(JSON.stringify({ results }));
if (results.some((r) => !r.ok)) process.exitCode = 1;
"""


@pytest.fixture(scope="session")
def js_results(tmp_path_factory) -> dict:
    """Run app.js in Node against a DOM shim, once per mode."""
    node = shutil.which("node")
    assert node, (
        "node is required for the dashboard behaviour tests. They are not skipped "
        "when it is missing: a silently skipped test is the same as no test."
    )
    workdir = tmp_path_factory.mktemp("spa")
    # app.js is an ES module; Node treats a bare .js as CommonJS, so it is copied
    # under .mjs rather than edited. The bytes are identical.
    app_mjs = workdir / "app.mjs"
    app_mjs.write_bytes(APP_JS.read_bytes())
    harness = workdir / "harness.mjs"
    harness.write_text(HARNESS, encoding="utf-8")

    merged: dict = {}
    for mode in ("ok", "fail"):
        completed = subprocess.run(
            [node, str(harness), str(app_mjs), str(INDEX_HTML), mode],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert completed.stdout.strip(), (
            f"the node harness printed nothing in mode {mode}.\n"
            f"stderr:\n{completed.stderr}"
        )
        payload = json.loads(completed.stdout)
        for result in payload["results"]:
            merged[result["name"]] = result
    return merged


def _js(results: dict, name: str):
    assert name in results, (
        f"the node harness did not run a check named {name}; it ran: {sorted(results)}"
    )
    result = results[name]
    assert result["ok"], f"{name}: {result['detail']}"


JS_CHECKS = [
    "recovery_binds_known_target_and_reuses_uncertain_intent",
    "selected_mining_previews_one_incident_and_reuses_uncertain_intent",
    "reapplication_returns_to_review_and_reuses_uncertain_intent",
    "eval_jobs_preview_cost_recover_requests_and_isolate_shortcuts",
    "target_rejection_uses_the_selection_and_lesson_rejection_is_explicit",
    "rollback_clicks_reuse_uncertain_requests_and_reload_saved_progress",
    "operation_controls_retain_uncertain_keys_and_do_not_trigger_review_shortcuts",
    "boot_fetches_only_get_and_only_this_origin",
    "boot_leaves_no_error_and_finds_every_id",
    "overview_paints_the_status_line",
    "staleness_banner_is_shown_when_the_data_is_old",
    "run_grid_renders_running_as_its_own_state",
    "run_grid_undrawn_states_pass_through_rather_than_becoming_ok",
    "run_grid_legend_names_every_state_present",
    "run_grid_footnote_states_the_two_caveats",
    "run_grid_marks_the_stuck_runs_its_colour_hides",
    "run_grid_footnote_does_not_claim_a_visibility_the_grid_lacks",
    "refused_is_a_drawn_state_not_an_unmapped_alarm",
    "grid_numbers_toggle_rewires_the_grid",
    "backlog_shows_two_rates_and_no_countdown",
    "gate_health_shows_all_four_verdicts_and_no_rate",
    "gate_health_renders_an_unknown_verdict_visibly",
    "gate_health_separates_harness_broken_from_rule_failed",
    "gate_health_reports_proposal_evals_not_the_table_count",
    "confidence_renders_the_fraction_not_a_rate",
    "inbox_follows_the_trust_model_and_names_the_blocker",
    "failure_panel_separates_fixed_from_still_happening",
    "rules_table_renders_one_row_per_rule",
    "a_rule_with_no_title_shows_its_rule_not_its_uuid",
    "every_table_row_emits_the_columns_index_html_declares",
    "rules_table_escapes_hostile_text",
    "rules_table_shows_the_four_verdicts_and_an_em_dash_for_none",
    "miner_generation_is_unknown_without_history",
    "search_narrows_the_table_and_reports_the_fraction",
    "search_with_no_match_explains_itself",
    "duplicate_families_render_the_designed_empty_state",
    "clicking_a_rule_row_opens_the_inspector",
    "the_inspector_verdict_names_what_the_verdict_was_about",
    "inspector_tabs_switch_through_the_real_click_handler",
    "evidence_tab_never_shows_the_sha1_as_the_error",
    "enforcement_gap_flag_shows_on_the_rule_that_has_one",
    "projects_table_collapses_clones_and_offers_the_affordance",
    "benefit_is_the_em_dash_the_data_sent_never_a_zero",
    "exposure_distinguishes_missing_observations_and_partial_rates",
    "a_repo_with_no_display_name_falls_back_to_its_key",
    "context_column_shows_observed_bytes_without_session_claims",
    "clicking_a_project_opens_the_file_by_file_breakdown",
    "the_project_topology_tab_names_the_symlink",
    "the_clones_chip_opens_the_working_copies_tab",
    "closing_the_inspector_gives_the_column_back",
    "boot_respects_a_theme_the_document_already_declares",
    "skip_navigation_focuses_content_without_changing_the_route",
    "the_theme_toggle_sets_data_theme_both_ways",
    "review_view_renders_one_card_per_family_leading_with_why",
    "a_card_says_each_fact_once",
    "a_card_whose_proposals_differ_still_shows_each_one",
    "md_lite_renders_the_miners_emphasis_and_escapes_first",
    "md_lite_cannot_emit_a_tag_from_its_input",
    "the_review_card_headline_uses_it",
    "v4_says_how_many_directories_it_left_out_and_why",
    "a_double_click_does_not_report_a_false_failure",
    "the_in_flight_guard_is_released_so_the_button_is_not_wedged",
    "a_refused_atomic_family_marks_no_member_approved",
    "a_uniform_family_still_shows_which_proposal_landed",
    "every_review_diff_is_available_without_truncation",
    "the_view_and_the_nav_do_not_label_two_numbers_the_same_way",
    "the_rules_inspector_links_into_the_queue_and_states_the_reason",
    "the_rules_inspector_never_offers_to_decide",
    "a_rule_with_nothing_waiting_says_so_rather_than_going_blank",
    "j_and_k_move_the_selection_and_stop_at_the_ends",
    "the_first_j_moves_off_the_card_that_is_already_open",
    "a_shared_target_is_not_reprinted_once_per_proposal",
    "moving_the_selection_scrolls_to_it_and_focuses_it",
    "the_selected_card_is_marked_in_the_markup",
    "a_review_key_is_ignored_while_typing_in_a_text_field",
    "review_keys_do_nothing_on_another_view",
    "a_and_r_refuse_to_decide_when_nothing_is_selected",
    "r_rejects_the_selected_family_over_the_wire",
    "review_card_keeps_a_reason_for_every_proposal_in_the_family",
    "review_note_names_the_auto_appliable_and_the_unknown_statuses",
    "approving_a_family_submits_one_command_with_exact_revisions",
    "reapproval_gets_a_new_key_but_an_uncertain_retry_reuses_its_key",
    "an_unknown_route_is_loud",
    "a_failed_endpoint_is_loud_and_names_the_request",
    "a_failed_endpoint_does_not_blank_the_ones_that_worked",
]


@pytest.mark.parametrize("check_name", JS_CHECKS)
def test_spa_behaviour(js_results: dict, check_name: str):
    _js(js_results, check_name)


def test_every_node_check_was_reported(js_results: dict):
    """Guards the harness itself: a check that silently stops running would
    otherwise leave its pytest case passing on a lookup that never happened."""
    missing = sorted(set(JS_CHECKS) - set(js_results))
    assert missing == [], f"the node harness never ran: {missing}"
    extra = sorted(set(js_results) - set(JS_CHECKS))
    assert extra == [], f"the node harness ran checks pytest does not assert on: {extra}"


# ---------------------------------------------------------------------------
# novalue() is the guard against misreporting a measurement
# ---------------------------------------------------------------------------
#
# `.novalue` is app.css's treatment for "a value that does not exist": muted
# ink and a dotted underline, with the reason on hover. Found 2026-08-23 by
# audit: novalue() never read the `computable` flag it documents, so ANY
# payload that became computable would render its real number in that
# not-a-value treatment, under a tooltip reading "not computable".
#
# Render measured values as measurements and missing values with their reason.
# The probe covers both shapes independently of any current data producer.

_NOVALUE_PROBE = """
import { novalue } from "./app.mjs";
const cases = {
  computable_true:      novalue({ value: 42, computable: true, reason: "" }),
  computable_true_bare: novalue({ value: 42, computable: true }),
  not_computable:       novalue({ value: "\\u2014", computable: false, reason: "no writer yet" }),
  missing_flag:         novalue({ value: "\\u2014", reason: "legacy payload" }),
  bare_scalar:          novalue(3),
  nullish:              novalue(null),
  zero_is_a_real_value: novalue({ value: 0, computable: true, reason: "" }),
};
process.stdout.write(JSON.stringify(cases));
"""


@pytest.fixture(scope="session")
def novalue_cases(tmp_path_factory) -> dict:
    node = shutil.which("node")
    assert node, "node is required; a silently skipped test is the same as no test"
    workdir = tmp_path_factory.mktemp("novalue")
    (workdir / "app.mjs").write_bytes(APP_JS.read_bytes())
    probe = workdir / "probe.mjs"
    probe.write_text(_NOVALUE_PROBE, encoding="utf-8")
    done = subprocess.run(
        [node, str(probe)], capture_output=True, text=True, timeout=60, cwd=workdir
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_a_computable_value_is_not_dressed_as_an_absence(novalue_cases: dict):
    """Sabotage: remove the `computable === true` branch. This fails.

    Before the fix this returned
      <span class="novalue" title="not computable">42</span>
    """
    for key in ("computable_true", "computable_true_bare"):
        html = novalue_cases[key]
        assert "42" in html, html
        assert 'class="novalue"' not in html, (
            f"{key}: a real measurement rendered in the not-a-value treatment: {html}"
        )
        assert "not computable" not in html, f"{key}: {html}"


def test_zero_is_a_real_value_when_the_payload_says_it_is_computable(novalue_cases):
    """0 is the number this product is most careful about. When it IS measured,
    it must render as a measurement, not as the em-dash that means 'unknown'."""
    html = novalue_cases["zero_is_a_real_value"]
    assert ">0<" in html, html
    assert 'class="novalue"' not in html, html


def test_a_non_computable_payload_keeps_the_novalue_treatment(novalue_cases):
    html = novalue_cases["not_computable"]
    assert 'class="novalue"' in html
    assert "no writer yet" in html
    assert "—" in html


def test_a_payload_with_no_flag_stays_conservative(novalue_cases):
    """Absent `computable` is not 'yes'. Unknown means unknown."""
    html = novalue_cases["missing_flag"]
    assert 'class="novalue"' in html
    assert "legacy payload" in html


def test_a_bare_scalar_does_not_silently_lose_its_value(novalue_cases):
    """Before the fix novalue(3) returned an em-dash, discarding the 3."""
    html = novalue_cases["bare_scalar"]
    assert ">3<" in html, f"the value was dropped: {html}"


def test_nullish_still_renders_the_em_dash(novalue_cases):
    assert "—" in novalue_cases["nullish"]


# ---------------------------------------------------------------------------
# Colour is never the only channel — including in "Show numbers" mode
# ---------------------------------------------------------------------------
#
# tokens.css and docs/DASHBOARD_DESIGN.md both state the rule. The numbers
# toggle broke it: it hid the glyph, which is the only non-colour channel most
# cell states have. Measured in Chrome 2026-08-23, `ok` and `failed` in numbers
# mode came back with glyphDisplay "none", background-image "none" and an
# identical "1px solid" border — separated by hue alone, which is exactly the
# pair a red-green colourblind reader cannot resolve.


@pytest.fixture(scope="session")
def app_css() -> str:
    return APP_CSS.read_text(encoding="utf-8")


def _rule_body(css: str, selector: str) -> str:
    import re

    match = re.search(re.escape(selector) + r"\s*\{(.*?)\}", css, re.S)
    assert match, f"{selector} not found"
    return match.group(1)


def test_the_numbers_toggle_keeps_the_glyph(app_css: str):
    """Sabotage: set `display: none` back on .run-grid--numbers .cell::before."""
    body = _rule_body(app_css, ".run-grid--numbers .cell::before")
    assert "display: block" in body, (
        "numbers mode hides the glyph, leaving cell state encoded by hue alone"
    )
    assert "display: none" not in body


def test_the_two_amber_states_differ_by_more_than_their_glyph(app_css: str):
    """`refused` and `budget_exhausted` share a whole palette on purpose.

    Sharing it is right — same cause, same family — but the glyph was their
    ONLY difference, so anything that drops the glyph collapses them into one.
    """
    def tokens(selector: str) -> set[str]:
        return {
            line.strip()
            for line in _rule_body(app_css, selector).strip().splitlines()
            if line.strip() and not line.strip().startswith(("/*", "*"))
        }

    differences = tokens('[data-state="budget_exhausted"]') ^ tokens('[data-state="refused"]')
    non_glyph = [d for d in differences if "--st-glyph" not in d and ":" in d]
    assert non_glyph, (
        "budget_exhausted and refused are separated by their glyph alone, so "
        f"they render identically wherever the glyph is not drawn: {differences}"
    )


def test_every_state_consumer_has_a_fallback_or_is_state_scoped(app_css: str):
    """A stateless .cell must alarm, not vanish.

    `.cell` and `.swatch` consumed five --st-* properties with no var()
    fallback while every other consumer was either [data-state]-scoped or
    carried one. An element reaching them without a state attribute rendered as
    nothing: transparent fill, currentColor border, no outline.
    """
    for selector in (".cell", ".swatch"):
        body = _rule_body(app_css, selector)
        for prop in ("--st-fill", "--st-texture", "--st-line", "--st-ink"):
            if prop in body:
                assert f"var({prop}," in body, (
                    f"{selector} uses {prop} with no fallback, so a stateless "
                    "element renders as nothing instead of alarming"
                )


# ---------------------------------------------------------------------------
# A focusable row must be an activatable row
# ---------------------------------------------------------------------------


def test_rows_that_advertise_focus_can_be_activated_by_keyboard(js_text: str):
    """Sabotage: remove the keydown binding on #main. This fails.

    Rows carry tabindex="0", so tab reaches them and assistive technology calls
    them interactive. Until 2026-08-23 only a click could open the inspector,
    so V2 and V4's whole progressive-disclosure journey was mouse-only.
    """
    assert 'byId("main").addEventListener("keydown", handleMainKeydown)' in js_text, (
        "focusable rows have no keyboard activation path"
    )
    assert "export function handleMainKeydown" in js_text


def test_keyboard_activation_covers_both_row_kinds_and_stops_space_scrolling(js_text: str):
    import re

    body = re.search(
        r"export function handleMainKeydown\(event\) \{(.*?)\n\}", js_text, re.S
    ).group(1)
    assert '"Enter"' in body and '" "' in body
    assert "data-rule-id" in body and "data-project-key" in body
    assert "preventDefault" in body, "Space would scroll the page instead of opening the row"


def test_a_focusable_row_carries_an_interactive_role(js_text: str):
    """tabindex without a role tells assistive tech 'focus me' and nothing else."""
    assert js_text.count('tabindex="0" role="button"') >= 2


# ---------------------------------------------------------------------------
# The audit's remaining findings, pinned
# ---------------------------------------------------------------------------


def test_a_no_match_search_hides_the_table_not_just_its_rows(js_text: str):
    """Sabotage: drop the `setVisible(table, false)` call.

    Emptying the tbody left the seven-column header strip sitting on top of the
    explanation — measured at 29.8px tall, all of it thead — which reads as a
    broken table rather than as the designed empty state it is.
    """
    assert 'byId("rules-table-wrap")' in js_text
    assert "setVisible(table, false)" in js_text
    assert "setVisible(table, true)" in js_text


def test_the_rules_table_wrapper_is_addressable(parsed: _Structure):
    assert "rules-table-wrap" in parsed.ids


def test_benefit_uses_the_designed_empty_state(js_text: str):
    """It was the only designed empty state on the page rendered as a bare dash
    under a heading, with its 130-character explanation reachable only by
    hovering. Every sibling section in that panel already used emptyState()."""
    import re

    body = re.search(r'section\(\s*\n?\s*"Benefit",(.*?)\n      \);', js_text, re.S)
    assert body, "the Benefit section is not in the expected shape"
    assert "emptyState(" in body.group(1), (
        "Benefit still renders a bare em dash instead of the designed state"
    )


def test_inconclusive_does_not_share_the_unmapped_glyph(app_css: str):
    """"?" is the mark for a value the design system has never been taught.
    `inconclusive` is a real verdict — the majority gate's scenarios disagreed."""
    import re

    blocks = re.findall(r'\[data-verdict="inconclusive"\]\s*\{(.*?)\}', app_css, re.S)
    assert blocks, "no inconclusive block"
    # The LAST declaration wins in CSS, which is the one that was colliding.
    glyphs = [
        re.search(r'--st-glyph:\s*"([^"]*)"', b).group(1)
        for b in blocks
        if "--st-glyph" in b
    ]
    assert glyphs[-1] not in ("?", "\\003F"), (
        f"inconclusive still uses the unmapped-value glyph {glyphs[-1]!r}"
    )


def test_a_failed_cell_carries_weight_that_survives_a_standard_display(app_css: str):
    """1.5px rounds to 1px at DPR 1, so the rule drew nothing."""
    body = _rule_body(app_css, '.cell[data-state="failed"],\n.cell[data-state="error"]')
    assert "1.5px" not in body
    assert "border-width: 2px" in body


def test_a_file_path_cannot_starve_the_rule_column(app_css: str):
    """An absolute path has no break opportunities; auto layout gave Target
    nearly twice the width of the rule text and made a row 393px tall."""
    assert "overflow-wrap: anywhere" in _rule_body(app_css, ".data td.mono")
    assert "width: 38%" in _rule_body(
        app_css, ".data--wrap th:first-child,\n.data--wrap td:first-child"
    )


def test_an_eval_that_names_a_rule_but_links_to_no_proposal_is_surfaced(js_text: str):
    """Learning-subject evaluations remain visible without implying an edit gate."""
    assert "unlinked_subject_evals" in js_text, (
        "the payload computes it and nothing renders it"
    )
    assert "These evaluations are not linked to this proposal" in js_text


def test_the_inspector_renders_the_diff_rather_than_an_empty_state(js_text: str):
    """PRD 7 V2 asks for it, and the column was one SELECT away all along.

    Sabotage: put the "No diff in this payload" emptyState back.
    """
    assert "export function renderRuleDiffs" in js_text
    assert 'section(\n        "Diff",\n        renderRuleDiffs(row)' in js_text
    assert "No diff in this payload" not in js_text, (
        "the read-layer-gap empty state is stale; the layer no longer has that gap"
    )


def test_a_diff_scrolls_inside_its_own_container(js_text: str, app_css: str):
    """A long patch line must not widen the page."""
    assert 'class="scroll-x diff"' in js_text
    body = _rule_body(app_css, ".diff")
    assert "overflow: auto" in body
    assert "white-space: pre" in body


def test_every_run_status_that_can_reach_a_cell_is_ranked_and_drawn(js_text: str):
    """The second path into a cell state, which the test above does not cover.

    `_stage_cell` has two sources for its state. One is `_state_from_numbers`,
    whose outputs are `CELL_STATES_WORST_FIRST` and are already tied to the
    SPA. The other is the RUN's status, used verbatim when the run recorded
    nothing for that stage — and nothing tied THAT list to anything.

    Two failures follow from a status that is missing:

    - `_worst_state` ranks by `CELL_STATES_WORST_FIRST` and drops what it does
      not know. A column whose only state is unranked returns `skipped`, which
      the page renders as "did not run". A broken run reads as absence.
    - app.js cannot draw it, so it renders as the unmapped alarm.

    Adding `degraded` to RUN_STATUSES on 2026-08-23 did exactly this, and the
    whole suite stayed green.
    """
    from self_improve.dashboard import queries

    # `ok` is the one status that never becomes a cell state: a run that
    # succeeded and recorded nothing for a stage really did skip it.
    reachable = set(queries.RUN_STATUSES) - {"ok"}
    ranked = set(queries.CELL_STATES_WORST_FIRST)
    drawn = set(_js_frozen_list(js_text, "DRAWN_STATES"))

    unranked = reachable - ranked
    assert not unranked, (
        f"run statuses {sorted(unranked)} can become a cell state but are not "
        "in CELL_STATES_WORST_FIRST. _worst_state drops what it cannot rank "
        "and returns 'skipped', so the run renders as 'did not run'."
    )
    undrawn = reachable - drawn
    assert not undrawn, (
        f"run statuses {sorted(undrawn)} reach a cell but app.js cannot draw "
        "them, so they render as the unmapped alarm."
    )


def _js_frozen_object_keys(js: str, name: str) -> list[str]:
    """The keys of `export const <name> = Object.freeze({...})`."""
    match = re.search(
        rf"export const {name} = Object\.freeze\(\{{(.*?)\}}\)", js, flags=re.S
    )
    assert match, f"app.js no longer declares {name} as a frozen object"
    return re.findall(r"^\s*(\w+):", match.group(1), flags=re.M)


def test_the_spa_knows_exactly_the_verdicts_the_gate_can_return(js_text: str):
    """Three hand-maintained lists for one vocabulary.

    `majority_verdict` decides the verdict, `GATE_VERDICTS` decides which ones
    the gate panel draws, and `VERDICT_MEANING` supplies the words. `renderGate`
    indexes VERDICT_MEANING with an unguarded lookup, so a verdict in
    GATE_VERDICTS but not in VERDICT_MEANING renders the literal string
    "undefined" beside a count.

    The gate's side is enumerated exhaustively — it is a pure function of the
    tally — rather than restated here.
    """
    from itertools import product

    from self_improve.evals.regression import majority_verdict

    real = {
        majority_verdict({"gated_pass": p, "gated_fail": f, "ungated": u})
        for p, f, u in product(range(4), repeat=3)
    }
    drawn = set(_js_frozen_list(js_text, "GATE_VERDICTS"))
    worded = set(_js_frozen_object_keys(js_text, "VERDICT_MEANING"))

    assert drawn == real, (
        f"the gate panel draws {sorted(drawn)} but the gate returns "
        f"{sorted(real)}; a verdict it does not draw is invisible"
    )
    assert worded == real, (
        f"VERDICT_MEANING covers {sorted(worded)} but the gate returns "
        f"{sorted(real)}; renderGate would print 'undefined' for the rest"
    )


def test_every_run_status_the_pipeline_writes_is_one_the_schema_lists(js_text: str):
    """The run-status half of the same pair, from the writing end.

    `RUN_STATUSES` is what the dashboard understands. A status pipeline writes
    that is missing from it renders as `unknown_status`, the deliberate alarm —
    which is the right failure mode, but only if someone sees it. This catches
    it at the source instead.
    """
    import re as _re
    from pathlib import Path

    from self_improve import pipeline as pl
    from self_improve.dashboard import queries

    # Two sources, because a run status is written two ways. The normal path
    # goes through derive_run_status, so ASK it rather than pattern-match a
    # return expression — the first version of this test scanned for a literal
    # on its own line, never saw `degraded`, and passed while RUN_STATUSES was
    # missing it.
    written = set()
    for stats in ({}, {"mine": {"attempted": 1, "succeeded": 0, "failed": 1}}):
        written.add(pl.derive_run_status(stats)[0])
    assert written == {"ok", "degraded"}, (
        f"derive_run_status no longer returns both statuses: {written}"
    )
    # The abnormal paths write a literal into the runs table.
    source = Path(pl.__file__).read_text(encoding="utf-8")
    runs_writes = _re.findall(
        r'store\.update\(\s*"runs".*?"status": ([^,\n]+)', source, _re.S
    )
    assert runs_writes, "no writes to runs.status found; the scanner is broken"
    written |= {m.strip('"') for m in runs_writes if m.startswith('"')}
    written |= set(_re.findall(r'status = \(\s*"([a-z_]+)"', source))
    written |= set(_re.findall(r'else\s+"([a-z_]+)"\s*\)', source))
    written &= {"ok", "degraded", "error", "interrupted", "abandoned", "running",
                "budget_exhausted"}
    assert "degraded" in written and "interrupted" in written, (
        f"the scanner missed a status it must see: {sorted(written)}"
    )
    missing = sorted(written - set(queries.RUN_STATUSES))
    assert not missing, (
        f"pipeline writes run statuses {missing} that RUN_STATUSES does not "
        "list, so the dashboard renders them as unknown_status"
    )


# ---------------------------------------------------------------------------
# The SPA <-> API contract: two lists in two languages, previously unguarded
# ---------------------------------------------------------------------------


def _spa_constants(tmp_path) -> dict:
    """Read app.js's exported constants by IMPORTING it, not by regex.

    A regex over the source would pass on a value the module never exports.
    The module's boot is guarded on `document` existing, so importing it in
    bare Node is side-effect free.
    """
    node = shutil.which("node")
    assert node, "node is required; a silently skipped test is the same as no test."
    app_mjs = tmp_path / "app.mjs"
    app_mjs.write_bytes(APP_JS.read_bytes())
    dump = tmp_path / "dump.mjs"
    dump.write_text(
        f"import * as m from {json.dumps(str(app_mjs))};\n"
        "process.stdout.write(JSON.stringify({\n"
        "  api: m.API,\n"
        "  decisions: Object.keys(m.DECISIONS),\n"
        "  decision_url: m.decisionUrl('THE-ID'),\n"
        "  rollback_preview_url: m.rollbackPreviewUrl('THE-ID'),\n"
        "  drawn_states: m.DRAWN_STATES,\n"
        "  state_meaning: Object.keys(m.STATE_MEANING),\n"
        "  delivery_states: m.DELIVERY_STATES,\n"
        "}));\n",
        encoding="utf-8",
    )
    out = subprocess.run([node, str(dump)], capture_output=True, text=True, timeout=60)
    assert out.stdout.strip(), f"node printed nothing.\nstderr:\n{out.stderr}"
    return json.loads(out.stdout)


def test_delivery_state_labels_cover_the_executor_vocabulary(tmp_path):
    from self_improve.commands import COMMAND_STATES

    consts = _spa_constants(tmp_path)
    assert set(consts['delivery_states']) == COMMAND_STATES
    assert all(label and style in consts['drawn_states']
               for label, style in consts['delivery_states'].values())


def test_every_url_the_spa_fetches_is_a_route_the_app_serves(tmp_path):
    """Reconcile exported API paths and generated URLs with the served routes.

    Read the JavaScript exports, generate URLs for a placeholder proposal,
    and match their parameterised forms against the application routes.
    """
    from self_improve.config import Config
    from self_improve.dashboard.app import create_app

    consts = _spa_constants(tmp_path)
    app = create_app(Config(state_dir=str(tmp_path / "state")))
    served = {r.path for r in app.routes if getattr(r, "path", None)}

    assert consts["api"], "the SPA exports an empty API map"
    for name, path in consts["api"].items():
        assert path in served, (
            f"the SPA fetches {path!r} for view {name!r}, which this app does "
            f"not serve. It serves: {sorted(p for p in served if p.startswith('/api'))}"
        )

    # Match the legacy decision URL against its parameterised route.
    built = consts["decision_url"]
    assert built == "/api/proposals/THE-ID/decision", built
    template = re.sub(r"THE-ID", "{proposal_id}", built)
    assert template in served, (
        f"decisionUrl builds {built!r} -> {template!r}, which is not a served route"
    )
    preview = consts['rollback_preview_url']
    assert preview == '/api/proposals/THE-ID/rollback-preview', preview
    assert preview.replace('THE-ID', '{proposal_id}') in served


def test_the_decision_words_the_spa_offers_are_the_ones_the_endpoint_accepts(tmp_path):
    """Both sides keep their own copy of the decision vocabulary.

    `app.js` has `DECISIONS` (the button captions) and the endpoint has its
    own `DECISIONS` dict, local to the handler. A word in one and not the
    other is a button that fails at runtime. This drives the real endpoint
    rather than reading either list's text: what matters is what the server
    ACCEPTS, not what a dict literal says.
    """
    from fastapi.testclient import TestClient
    from self_improve.config import Config
    from self_improve.dashboard.app import create_app
    from self_improve.store import Store

    consts = _spa_constants(tmp_path)
    assert consts["decisions"], "the SPA exports no decision words"

    state = tmp_path / "state"
    state.mkdir()
    Store(state / "state.db").commit()
    app = create_app(Config(state_dir=str(state)))
    with TestClient(app) as client:
        for word in consts["decisions"]:
            r = client.post("/api/proposals/no-such-id/decision", json={"decision": word})
            # 404 = the word was understood and the proposal was not found.
            # 400 = the endpoint rejected the WORD, which is the drift.
            assert r.status_code != 400, (
                f"the SPA offers {word!r} and the endpoint refuses it: {r.json()}"
            )
        bogus = client.post(
            "/api/proposals/no-such-id/decision", json={"decision": "definitely-not-a-decision"}
        )
        assert bogus.status_code == 400, (
            "an unknown decision word must be refused, or the check above proves nothing"
        )
        assert set(bogus.json()["accepted"]) == set(consts["decisions"]), (
            f"endpoint accepts {sorted(bogus.json()['accepted'])}, "
            f"SPA offers {sorted(consts['decisions'])}"
        )


def test_every_drawn_state_also_has_a_plain_language_meaning(tmp_path):
    """A state can be drawn, styled, and still explain nothing.

    `test_every_drawn_state_and_every_marker_has_a_rule_in_app_css` pairs
    `DRAWN_STATES` with app.css, so a state without a VISUAL rule is caught.
    Nothing paired it with `STATE_MEANING`, so a state with a colour and no
    words was not — the reader sees a cell they cannot interpret, which is the
    failure `degraded` caused when `RUN_STATUSES` gained it and the SPA did
    not know.
    """
    consts = _spa_constants(tmp_path)
    drawn = consts["drawn_states"]
    meanings = set(consts["state_meaning"])
    assert len(drawn) >= 10, f"expected the real drawn-state list, got {drawn}"
    missing = [s for s in drawn if s not in meanings]
    assert not missing, f"drawn with no plain-language meaning: {missing}"


def test_a_decided_badge_is_never_paired_with_a_target_by_index(js_text: str):
    """Read each decision mark by the proposal's own ID.

    Target deduplication can change list lengths and positions. A target-list
    index cannot identify the proposal whose decision the badge describes.
    """
    body = _strip_js_comments(js_text)
    # Re-aimed 2026-09-12. The original guarded a `decidedMark(proposals[i])`
    # call in a branch that no longer exists: the card was rebuilt against the
    # design and the deduplicated target list it indexed into is gone. The
    # HAZARD is unchanged, so the guard now names the invariant instead of the
    # old call site -- a decided mark is looked up by the proposal's own id,
    # never by position in a list that was deduplicated on something else.
    start = body.index("export function renderProposalBreakdown")
    end = body.index("export function renderReviewCard")
    section = body[start:end]
    assert "opts.decided[proposal.id]" in section, (
        "the decided mark is no longer keyed by the proposal's own id; a mark "
        "paired by position attaches to whichever proposal sits there, which "
        "is only correct when no two proposals share a target"
    )
    assert "proposals[i]" not in section and "proposals[index]" not in section, (
        "the per-proposal list is indexing the proposals array rather than "
        "mapping over it"
    )
    # Anti-vacuity: a scan that found nothing makes this vacuously true, and
    # this repo has shipped exactly that. Both anchors must be real.
    assert len(section) > 400, f"the scanned section is too small to be real: {len(section)}"
    assert "proposals.map" in section, (
        "renderProposalBreakdown no longer maps over its proposals; re-read it "
        "before trusting this test"
    )


def test_every_reason_code_has_a_tint_and_every_tint_has_a_reason_code(app_css_classes):
    """Reconcile JavaScript modifier names with CSS rules in both directions.

    Runtime class names need their complete declared vocabulary; scanning a
    template prefix alone cannot find missing or orphaned modifier rules.
    """
    js = APP_JS.read_text(encoding="utf-8")
    css_modifiers = {
        name
        for name in app_css_classes
        if name.startswith("review-card__why--") or name.startswith("review-card__eval--")
    }
    declared = set(re.findall(r'"(review-card__(?:why|eval)--[a-z_]+)"', js))
    assert declared, "the scan found no modifier names in app.js; it is broken"

    assert not (declared - css_modifiers), (
        "app.js names modifier classes app.css does not define, so the card "
        f"renders untinted: {sorted(declared - css_modifiers)}"
    )
    assert not (css_modifiers - declared), (
        "app.css defines modifier classes nothing can produce: "
        f"{sorted(css_modifiers - declared)}"
    )


def test_every_queueing_reason_the_api_can_send_has_a_tint():
    """The tints must cover the vocabulary the DATA LAYER actually emits.

    The test above reconciles two files that could agree with each other and
    both be wrong. This one reconciles against `queries.WHY_QUEUED` and the
    carve-out copy, which is what `review_queue` puts in `reason_code`.
    """
    from self_improve.dashboard import queries as q

    js = APP_JS.read_text(encoding="utf-8")
    declared = set(re.findall(r'"(review-card__why--[a-z_]+)"', js))
    for code in list(q.WHY_QUEUED) + list(q.WHY_CARVE_OUT):
        assert f"review-card__why--{code}" in declared, (
            f"reason code {code!r} reaches the card with no tint declared in "
            "WHY_CLASS, so it renders with the neutral base and the operator "
            "cannot tell it apart from an unrelated reason"
        )


def test_every_carve_out_action_the_config_declares_has_an_english_label():
    """Require a readable label for every configured or mandatory review action.

    Reconcile the label map with both action sets. Missing labels and labels
    for actions outside those sets must each fail.
    """
    from self_improve.config import Config

    js = APP_JS.read_text(encoding="utf-8")
    block = js[js.index("export const CARVE_OUT_LABEL") : js.index("export function carveOutLabel")]
    labelled = set(re.findall(r"^\s+([a-z_]+):", block, re.M))
    assert labelled, "the scan found no labels; it is broken"

    from self_improve.execution_policy import MANDATORY_REVIEW_ACTIONS
    declared = set(Config(state_dir="/tmp/does-not-need-to-exist").review_queue_actions) | MANDATORY_REVIEW_ACTIONS
    assert not (declared - labelled), (
        "the config declares carve-out actions the panel has no English name "
        f"for, so it renders a raw identifier: {sorted(declared - labelled)}"
    )
    assert not (labelled - declared), (
        "the panel names carve-out actions the config does not declare: "
        f"{sorted(labelled - declared)}"
    )


def test_a_truncated_rule_row_says_it_was_truncated(js_text: str):
    """Require ruleOneLine to report the truncation returned by clamp.

    A shortened row must carry an ellipsis so it cannot look like a complete
    rule. Preserve the cut count instead of returning unmarked partial text.
    """
    body = _strip_js_comments(js_text)
    start = body.index("export function ruleOneLine")
    section = body[start : body.index("export const CARVE_OUT_LABEL")]
    assert "cut.cut" in section, (
        "ruleOneLine no longer consults clamp's cut count, so it can truncate "
        "a rule without saying so"
    )
    assert "\\u2026" in section or "…" in section, (
        "ruleOneLine truncates without appending an ellipsis"
    )


def test_every_eval_shape_the_data_layer_emits_has_a_class():
    """Third reader of the eval-shape vocabulary, reconciled against the source.

    `test_every_reason_code_has_a_tint_and_every_tint_has_a_reason_code`
    reconciles app.js against app.css -- two files that can agree with each
    other and both be wrong. `eval_story` in the DATA LAYER is what actually
    produces these strings, so it is the one that decides. Adding
    `unknown_outcome` there on 2026-09-12 left the view with no class for it,
    and nothing failed: the card rendered untinted and the reconciliation above
    stayed green because both files it reads were consistently incomplete.

    The question that finds this class is not "is this tested?" but "who else
    answers this question?"
    """
    from self_improve.dashboard import queries as _q

    source = Path(_q.__file__).read_text(encoding="utf-8")
    start = source.index("def eval_story(")
    end = source.index("\ndef ", start + 10)
    emitted = set(re.findall(r'shape = "([a-z_]+)"', source[start:end]))
    assert emitted, "the scan found no shapes in eval_story; it is broken"

    js = APP_JS.read_text(encoding="utf-8")
    block = js[js.index("export const EVAL_CLASS") : js.index("export function evalClass")]
    classed = set(re.findall(r"^\s+([a-z_]+):", block, re.M))

    assert not (emitted - classed), (
        "eval_story emits shapes the card has no class for, so they render "
        f"untinted: {sorted(emitted - classed)}"
    )
    assert not (classed - emitted), (
        "the card declares classes for shapes eval_story cannot produce: "
        f"{sorted(classed - emitted)}"
    )


def test_a_carve_out_says_the_eval_is_not_what_is_holding_it(js_text: str):
    """A carve-out queues on its ACTION, whatever the verdict says.

    `WHY_CARVE_OUT` explains the policy in the lead line, and the eval sentence
    renders directly underneath. With nothing between them, "No trial ran"
    reads as the reason and the operator looks for an eval to fix -- when
    approving would still need a person however the eval had gone. PRD S5.1 is
    explicit that the action is the trigger.
    """
    body = _strip_js_comments(js_text)
    start = body.index("export function renderEvalStory")
    section = body[start : body.index("export function renderTargetRows")]
    assert "has_carve_out" in section, (
        "renderEvalStory does not distinguish a carve-out, so its eval "
        "sentence sits under the lead line reading as the reason"
    )
    assert "not the " in section and "reason" in section, (
        "the carve-out framing no longer says the eval is not the reason"
    )
