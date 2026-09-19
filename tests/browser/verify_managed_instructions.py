"""Verify managed origins, coverage and complete paths in the shipped Project UI."""
from pathlib import Path
from urllib.parse import quote
import json
import sqlite3
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[2]
out = ROOT/'reports/dashboard-parity'
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width':1440,'height':1024})
    errors, requests = [], []
    page.on('pageerror', lambda e:errors.append(str(e)))
    page.on('request', lambda r:requests.append((r.method,r.url)))
    try:
        for case, port in [('observed',8876), ('absent',8877)]:
            info = json.loads((out/f'managed-{case}-manifest.json').read_text())
            url = f'http://127.0.0.1:{port}/#/projects/'+quote(info['project_key'],safe='')+'?tab=topology'
            page.goto(url); page.wait_for_load_state('networkidle')
            page.screenshot(path=str(out/f'managed-{case}-before.png'))
            (out/f'managed-{case}-before.html').write_text(page.content())
            content = page.locator('#project-detail-body')
            expected = 'memory observed · enterprise skills failed' if case == 'observed' else 'memory absent · enterprise skills absent'
            assert expected in content.inner_text()
            assert 'effective managed policy' in content.inner_text()
            if case == 'observed':
                item = page.locator('details').filter(has=page.locator('summary').get_by_text(info['managed_root']+'/.claude/skills/check-21/SKILL.md',exact=True)).first
                item.locator(':scope > summary').focus(); page.keyboard.press('Enter')
                assert 'managed · on demand' in item.inner_text()
                assert '/check-21' in item.inner_text()
                item.get_by_text('Complete recorded loading paths',exact=True).click()
                assert '"origin": "managed"' in item.inner_text()
                item.locator(':scope > summary').focus(); page.keyboard.press('End')
                page.wait_for_function('document.querySelector("#main").scrollTop > 0')
                assert page.evaluate('document.activeElement.tagName') == 'SUMMARY'
                for width in (1440,1280):
                    page.set_viewport_size({'width':width,'height':1024})
                    for theme in ('light','dark'):
                        page.evaluate('(t)=>document.documentElement.dataset.theme=t',theme)
                        item.scroll_into_view_if_needed()
                        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                        page.screenshot(path=str(out/f'managed-{theme}-{width}.png'))
            page.reload(); page.wait_for_load_state('networkidle')
            assert expected in content.inner_text()
            page.get_by_role('link',name='Instruction context',exact=True).click()
            expect(page.locator("#project-view-context")).to_have_attribute("aria-current", "page")
            if case == 'observed':
                assert 'Managed sources:' in content.inner_text()
                assert 'Global sources:' in content.inner_text()
                assert 'Their rows can overlap' in content.inner_text()
            else:
                assert 'No supported instruction bytes were observed' in content.inner_text()
            page.screenshot(path=str(out/f'managed-{case}-context.png'))
            assert all(Path(path).read_text() == text for path,text in info['targets'].items())
            with sqlite3.connect(info['db']) as db:
                assert list(db.iterdump()) == info['snapshot']
                assert db.execute('SELECT COUNT(*) FROM llm_calls').fetchone()[0] == 0
        assert not errors, errors
        assert all(m == 'GET' for m,u in requests if u.startswith('http://127.0.0.1:'))
        print(f'MANAGED_INSTRUCTIONS_BROWSER_OK: {len(requests)} GET-only requests, unchanged Stores/targets, zero models/errors; managed origins, observed/absent/failed roots, complete paths, focus/scroll/reload, both themes/widths and data cards.')
    except Exception:
        page.screenshot(path=str(out/'managed-failure.png'))
        (out/'managed-failure.html').write_text(page.content())
        raise
    finally:
        browser.close()
