"""Verify Project field selection through actual retained text with GET-only reads."""
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
        for case, port in [('selected',8876),('failed',8877),('absent',8878)]:
            info = json.loads((out/f'policy-{case}-manifest.json').read_text())
            url = f'http://127.0.0.1:{port}/#/projects/'+quote(info['project_key'],safe='')+'?tab=topology'
            page.goto(url); page.wait_for_load_state('networkidle')
            page.screenshot(path=str(out/f'policy-{case}-before.png'))
            (out/f'policy-{case}-before.html').write_text(page.content())
            content = page.locator('#project-detail-body')
            expected = {'selected':'Local file-policy winner:', 'failed':'Local file-policy selection is unknown', 'absent':'No embedded instruction field was found'}[case]
            assert expected in content.inner_text()
            assert 'do not establish rule availability' in content.inner_text()
            assert 'PRIVATE_SETTINGS_SENTINEL' not in page.content()
            if case == 'selected':
                name = info['managed_root']+'/managed-settings.d/21-team.json → claudeMd'
                item = page.locator('details').filter(has=page.locator('summary').get_by_text(name,exact=True)).first
                item.locator(':scope > summary').focus(); page.keyboard.press('Enter')
                assert 'managed policy selection unverified' in item.inner_text()
                item.get_by_text('Complete recorded loading paths',exact=True).click()
                assert '"json_pointer": "/claudeMd"' in item.inner_text()
                item.locator(':scope > summary').focus(); page.keyboard.press('End')
                page.wait_for_function('document.querySelector("#main").scrollTop > 0')
                assert page.evaluate('document.activeElement.tagName') == 'SUMMARY'
                for width in (1440,1280):
                    page.set_viewport_size({'width':width,'height':1024})
                    for theme in ('light','dark'):
                        page.evaluate('(theme)=>document.documentElement.dataset.theme=theme',theme)
                        item.scroll_into_view_if_needed()
                        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                        page.screenshot(path=str(out/f'policy-{theme}-{width}.png'))
                item.get_by_role('link',name='Browse retained text for this field',exact=True).click()
                page.locator('#evidence-results .evidence-result').wait_for()
                assert page.locator('#evidence-results .evidence-result').count() == 1
                page.locator('#evidence-results a[data-evidence-key]').click()
                page.get_by_role('link',name='Source',exact=True).click()
                page.get_by_text('Complete retained redacted text',exact=True).wait_for()
                assert 'Retained policy text sentinel 21.' in page.locator('#inspector-body').inner_text()
                assert 'PRIVATE_SETTINGS_SENTINEL' not in page.content()
                page.screenshot(path=str(out/'policy-retained-text.png'))
                page.goto(url); page.wait_for_load_state('networkidle')
            page.reload(); page.wait_for_load_state('networkidle')
            assert expected in content.inner_text()
            page.get_by_role('link',name='Instruction context',exact=True).click()
            expect(page.locator("#project-view-context")).to_have_attribute("aria-current", "page")
            assert expected in content.inner_text()
            if case != 'absent':
                assert 'Managed sources:' in content.inner_text()
                assert '0 B startup candidates' in content.inner_text()
            page.screenshot(path=str(out/f'policy-{case}-context.png'))
            assert all(Path(path).read_text() == text for path,text in info['targets'].items())
            with sqlite3.connect(info['db']) as db:
                assert list(db.iterdump()) == info['snapshot']
                assert db.execute('SELECT COUNT(*) FROM llm_calls').fetchone()[0] == 0
        assert not errors, errors
        assert all(m == 'GET' for m,u in requests if u.startswith('http://127.0.0.1:'))
        print(f'EMBEDDED_POLICY_BROWSER_OK: {len(requests)} GET-only requests; selected/failed/absent policy, field identities, actual retained text, both themes/widths, focus/scroll/reload, unchanged Stores/targets, zero models/errors.')
    except Exception:
        page.screenshot(path=str(out/'policy-failure.png'))
        (out/'policy-failure.html').write_text(page.content())
        raise
    finally: browser.close()
