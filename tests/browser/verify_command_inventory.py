"""Inspect the shipped Project page against a temporary real collection."""
from pathlib import Path
from urllib.parse import quote
import json
import sqlite3
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[2]
out = ROOT/'reports/dashboard-parity'
info = json.loads((out/'commands-demo-manifest.json').read_text())
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width': 1440, 'height': 1024})
    errors, requests = [], []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.on('request', lambda r: requests.append((r.method, r.url)))
    try:
        url = 'http://127.0.0.1:8876/#/projects/'+quote(info['project_key'], safe='')+'?tab=topology'
        page.goto(url)
        page.wait_for_load_state('networkidle')
        page.screenshot(path=str(out/'commands-before.png'))
        (out/'commands-before.html').write_text(page.content())
        assert 'Observed' in page.locator('#project-detail-body').inner_text()
        command = page.locator('details').filter(has=page.locator('summary').get_by_text('.claude/commands/deploy.md', exact=True)).first
        command.locator(':scope > summary').focus()
        page.keyboard.press('Enter')
        assert 'legacy command' in command.inner_text()
        assert '/deploy' in command.inner_text()
        assert 'command shadowed by skill' in command.inner_text()
        assert 'Shadowed by:' in command.inner_text()
        assert 'invocation unverified' in command.inner_text()
        command.get_by_text('Complete recorded loading paths', exact=True).click()
        assert '"command_name": "deploy"' in command.inner_text()
        nested = page.locator('details').filter(has=page.locator('summary').get_by_text('.claude/commands/frontend/check.md', exact=True)).first
        nested.locator(':scope > summary').click()
        assert '/frontend:check' in nested.inner_text()
        assert 'on demand' in nested.inner_text()
        assert 'Conditions:' not in nested.inner_text()
        nested.locator(':scope > summary').focus()
        page.keyboard.press('End')
        page.wait_for_function('document.querySelector("#main").scrollTop > 0')
        assert page.evaluate('document.activeElement.tagName') == 'SUMMARY'
        for width in (1440, 1280):
            page.set_viewport_size({'width': width, 'height': 1024})
            for theme in ('light', 'dark'):
                page.evaluate('(t)=>document.documentElement.dataset.theme=t', theme)
                command.scroll_into_view_if_needed()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path=str(out/f'commands-{theme}-{width}.png'))
        page.reload()
        page.wait_for_load_state('networkidle')
        assert page.url == url
        command = page.locator('details').filter(has=page.locator('summary').get_by_text('.claude/commands/deploy.md', exact=True)).first
        command.locator(':scope > summary').click()
        assert 'command shadowed by skill' in command.inner_text()
        page.get_by_role('link', name='Instruction context', exact=True).click()
        expect(page.locator("#project-view-context")).to_have_attribute("aria-current", "page")
        assert 'On-demand skill and command bodies' in page.locator('#project-detail-body').inner_text()
        assert not errors, errors
        assert all(m == 'GET' for m, u in requests if u.startswith('http://127.0.0.1:8876'))
        assert all(Path(path).read_text() == text for path, text in info['targets'].items())
        with sqlite3.connect(info['db']) as db:
            assert list(db.iterdump()) == info['snapshot']
            assert db.execute('SELECT COUNT(*) FROM llm_calls').fetchone()[0] == 0
        print(f'COMMAND_INVENTORY_BROWSER_OK: {len(requests)} GET-only requests, unchanged Store/targets, zero models/errors; names, shadowing, complete paths, focus/scroll/reload, both themes/widths and architecture cards.')
    except Exception:
        page.screenshot(path=str(out/'commands-failure.png'))
        (out/'commands-failure.html').write_text(page.content())
        raise
    finally:
        browser.close()
