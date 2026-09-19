"""Capture current layout; no final visual or human-usability pass is inferred."""
from pathlib import Path
import hashlib
import json
import sqlite3
from urllib.parse import quote

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "reports/dashboard-parity/acceptance"
manifest = json.loads((OUT / "manifest.json").read_text())
assert "si-acceptance-ui-" in manifest["db"]
BASE = "http://127.0.0.1:8876/"
routes = [
    ("overview", "#/overview", "#view-overview"),
    ("rules", "#/rules/demo-rule-1", "#view-rules"),
    ("review", "#/review", "#view-review"),
    ("projects", "#/projects", "#view-projects"),
    ("project-detail", "#/projects/" + quote("demo:example-evals", safe=""), "#project-detail"),
    ("evals", "#/evals", "#view-evals"),
    ("run-detail", "#/overview/run/demo-run-11", "#run-title"),
]
result = {"fixture": manifest["fixture"], "captures": [], "errors": [], "requests": []}
with sync_playwright() as runtime:
    browser = runtime.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 1024})
    page.on("pageerror", lambda error: result["errors"].append(str(error)))
    page.on("request", lambda request: result["requests"].append([request.method, request.url]))
    try:
        for name, route, selector in routes:
            page.goto(BASE + route)
            page.wait_for_load_state("networkidle")
            page.locator(selector).wait_for(state="visible")
            # Keep actual rendered text alongside screenshots for later review.
            (OUT / f"{name}.html").write_text(page.content())
            (OUT / f"{name}.txt").write_text(page.locator("#main").inner_text())
            for width in (1440, 1280):
                page.set_viewport_size({"width": width, "height": 1024})
                for theme in ("light", "dark"):
                    current = page.evaluate('async()=>(await import("/app.js")).effectiveTheme()')
                    if current != theme:
                        page.get_by_role("button", name=f"{theme.title()} theme", exact=True).click()
                    page.locator("#main").evaluate("el => el.scrollTop = 0")
                    page.screenshot(path=str(OUT / f"{name}-{theme}-{width}.png"))
                    dimensions = page.locator("#main").evaluate("""el => ({
                      mainWidth: el.clientWidth, mainScrollWidth: el.scrollWidth,
                      mainHeight: el.clientHeight, mainScrollHeight: el.scrollHeight,
                      pageWidth: document.documentElement.scrollWidth,
                      viewportWidth: innerWidth,
                    })""")
                    result["captures"].append({"screen": name, "theme": theme, "width": width, **dimensions})
        assert len(result["captures"]) == 28
        assert not result["errors"], result["errors"]
        assert all(method == "GET" for method, _ in result["requests"])
        assert all(url.startswith(BASE) for _, url in result["requests"])
        with sqlite3.connect("file:" + manifest["db"] + "?mode=ro", uri=True) as db:
            assert list(db.iterdump()) == manifest["snapshot"]
            assert db.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == 0
        assert all(Path(path).read_text() == text for path, text in manifest["targets"].items())
        result["read_only"] = True
        result["model_calls"] = 0
        result["static_sha256"] = {
            str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted((ROOT / "src/self_improve/dashboard/static").glob("*")) if path.is_file()
        }
        (OUT / "baseline-result.json").write_text(json.dumps(result, indent=2))
        print("ACCEPTANCE_BASELINE_CAPTURED: 28 captures; unchanged Store/targets; zero model calls/page errors")
    except Exception:
        page.screenshot(path=str(OUT / "failure.png"), full_page=True)
        (OUT / "failure.html").write_text(page.content())
        raise
    finally:
        browser.close()
