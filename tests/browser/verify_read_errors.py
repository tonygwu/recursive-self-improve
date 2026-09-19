"""Read-only failed-read explanations and native diagnostic/recovery controls."""
from pathlib import Path
from urllib.parse import quote, urlencode
import argparse
import hashlib
import json
import sqlite3

from playwright.sync_api import sync_playwright, expect
from keyboard_actions import KeyboardActions
from visual_checks import VisualChecks

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--fixture", choices=["review", "detail", "project"], required=True)
args = parser.parse_args()
ROOT = Path(__file__).resolve().parents[2]
name, prefix = {"review": ("review-layout", "si-review-layout-"),
                "detail": ("review-decisions", "si-review-decisions-"),
                "project": ("project-composition", "si-project-summary-ui-")}[args.fixture]
info = json.loads((ROOT / "reports/dashboard-parity" / name / "manifest.json").read_text())
assert prefix in info["db"]
with sqlite3.connect("file:" + info["db"] + "?mode=ro", uri=True) as db:
    retained_calls = db.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0]
OUT = ROOT / "reports/dashboard-parity/read-errors" / args.fixture
OUT.mkdir(parents=True, exist_ok=True)
BASE = "http://127.0.0.1:8876/"
cause = "Invented <unavailable> read"
report = {"fixture": args.fixture, "requests": [], "errors": [], "states": [], "models": 0,
          "source_sha256": hashlib.sha256((ROOT / "src/self_improve/dashboard/static/app.js").read_bytes()).hexdigest()}

with sync_playwright() as runtime:
    browser = runtime.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1280, "height": 1024})
    page.on("request", lambda r: report["requests"].append([r.method, r.url]))
    page.on("pageerror", lambda e: report["errors"].append(str(e)))
    keys = KeyboardActions(page, OUT / "keyboard.json")
    visual = VisualChecks(page, OUT / "contrast.json")

    def go(fragment):
        page.goto(BASE + fragment)
        page.wait_for_load_state("networkidle")
        page.screenshot(path=str(OUT / "recon.png"))
        (OUT / "recon.html").write_text(page.content())

    def fail(route):
        route.fulfill(status=503, json={"detail": cause})

    def capture(label):
        visual.check(label)
        page.screenshot(path=str(OUT / (label + ".png")))
        report["states"].append(label)

    def check(surface, label, request_path, *, retained=False):
        for width in (1280, 1440):
            page.set_viewport_size({"width": width, "height": 1024})
            for theme in ("light", "dark"):
                keys.theme(theme)
                alert = surface.get_by_role("alert")
                expect(alert).to_contain_text(cause)
                assert "GET /api/" not in alert.inner_text()
                assert "Could not read" in alert.inner_text()
                if retained:
                    expect(alert).to_contain_text("Last successful summary remains below")
                    expect(surface).to_contain_text("2030-01-06 00:00:00 UTC")
                disclosure = surface.get_by_text("Request details", exact=True).locator("..")
                assert not disclosure.evaluate("el=>el.open")
                expect(disclosure.locator("pre")).not_to_be_visible()
                alert.scroll_into_view_if_needed()
                capture(f"{label}-{theme}-{width}")
                keys.activate(disclosure.locator(":scope > summary"), "Space")
                expect(disclosure.locator("pre")).to_be_visible()
                diagnostic = disclosure.locator("pre").inner_text()
                assert "GET " + request_path in diagnostic and "answered 503" in diagnostic
                assert cause in diagnostic
                assert disclosure.locator("script, unavailable").count() == 0
                assert surface.evaluate("el=>el.scrollWidth<=el.clientWidth+1")
                if args.fixture == "review":
                    assert all(pid in diagnostic for pid in info["mixed"])
                # Repaint without replacing the native disclosure's state or focus.
                if args.fixture != "project":
                    page.evaluate("async()=>{(await import('/app.js')).paintReview()}")
                    expect(disclosure.locator(":scope > summary")).to_be_focused()
                    assert disclosure.evaluate("el=>el.open")
                capture(f"{label}-details-{theme}-{width}")
                keys.activate(disclosure.locator(":scope > summary"), "Space")

    try:
        health = page.request.get(BASE + "api/health").json()
        assert health["db_path"] == info["db"] and health["read_only"]
        if args.fixture == "review":
            pattern = "**/api/review-preview?*"
            page.route(pattern, fail)
            for fragment, label in [("#/review", "queue"),
                                    ("#/review/family/" + quote(info["families"]["mixed"]), "full-preview")]:
                go(fragment)
                if label == "queue":
                    page.evaluate("async id=>{const a=await import('/app.js');a.state.selectedFamily=id;a.paintReview()}", info["families"]["mixed"])
                    page.wait_for_load_state("networkidle")
                surface = page.locator(".review-card--open" if label == "queue" else ".review-detail")
                check(surface, label, "/api/review-preview?")
                for action in ("approve", "reject", "reject_lesson"):
                    expect(surface.locator('[data-decision="' + action + '"]')).to_be_disabled()
            page.unroute(pattern)
            keys.activate(page.get_by_role("button", name="Reload Review", exact=True))
            expect(page.locator('[data-decision="approve"]')).to_be_enabled()
            expect(page.get_by_role("alert")).to_have_count(0)
            capture("recovered")
        elif args.fixture == "detail":
            pattern = "**/api/eval-results/" + info["result"]
            page.route(pattern, fail)
            go("#/review/family/" + quote(info["family"]))
            surface = page.locator('[id="review-evaluation-' + info["result"] + '"]')
            check(surface, "evaluation", "/api/eval-results/")
            expect(page.locator('[data-decision="approve"]')).to_be_enabled()
            page.unroute(pattern)
            keys.activate(page.get_by_role("button", name="Retry evaluation evidence", exact=True))
            expect(surface.get_by_role("alert")).to_have_count(0)
            expect(surface).to_be_focused()
            expect(surface).to_contain_text("3 / 3 passed with the rule; 0 / 3 without it")
            capture("recovered")
        else:
            fragment = "#/projects/" + quote(info["project_key"], safe="") + "?" + urlencode({"working_copy_id": info["copies"]["alpha"]})
            pattern = "**/api/project-summary?*"
            page.route(pattern, fail)
            go(fragment)
            surface = page.locator("#project-rule-summary")
            check(surface, "initial", "/api/project-summary?")
            expect(surface).to_contain_text("Rule summary has not been read for this copy")
            page.unroute(pattern)
            keys.activate(page.get_by_role("button", name="Read rule summary", exact=True))
            expect(surface.get_by_role("alert")).to_have_count(0)
            expect(surface).to_contain_text("2030-01-06 00:00:00 UTC")
            page.route(pattern, fail)
            keys.activate(page.get_by_role("button", name="Refresh rule summary", exact=True))
            check(surface, "refresh", "/api/project-summary?", retained=True)
            page.unroute(pattern)
            keys.activate(page.get_by_role("button", name="Refresh rule summary", exact=True))
            expect(surface.get_by_role("alert")).to_have_count(0)
            expect(page.locator("#project-rules-refresh")).to_be_focused()
            capture("recovered")
        with sqlite3.connect("file:" + info["db"] + "?mode=ro", uri=True) as db:
            assert list(db.iterdump()) == info["snapshot"]
            # Full Review seeds invented producing-call records before browsing.
            assert db.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == retained_calls
        assert all(Path(path).read_text() == content for path, content in info["targets"].items())
        assert all(method == "GET" for method, url in report["requests"])
        assert not report["errors"], report["errors"]
        visual.assert_clean()
        report.update(status="succeeded", store="unchanged", targets="unchanged")
        print("READ_ERRORS_BROWSER_OK", args.fixture, len(report["states"]), "states; GET only; unchanged Store/targets; zero models")
    except BaseException:
        report["status"] = "failed"
        page.screenshot(path=str(OUT / "failure.png"))
        (OUT / "failure.html").write_text(page.content())
        raise
    finally:
        (OUT / "result.json").write_text(json.dumps(report, indent=2) + "\n")
        browser.close()
