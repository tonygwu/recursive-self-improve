"""Mixed-provider Project inspection, with native keyboard actions and no writes."""
from pathlib import Path
from urllib.parse import quote
import json
import sqlite3
from playwright.sync_api import sync_playwright

out = Path(__file__).resolve().parents[2] / 'reports/dashboard-parity'
info = json.loads((out / 'codex-reports-manifest.json').read_text())
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width':1440, 'height':1024})
    errors, requests = [], []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.on('request', lambda r: requests.append((r.method, r.url)))
    def activate(locator):
        locator.focus()
        page.keyboard.press('Enter')
    def shown(text):
        page.get_by_text(text, exact=True).wait_for()
    def snapshot(name):
        page.screenshot(path=str(out / (name + '.png')))
        (out / (name + '.html')).write_text(page.content())
    try:
        url = 'http://127.0.0.1:8876/#/projects/' + quote(info['project_key'], safe='') + '?tab=loads'
        page.goto(url)
        page.wait_for_load_state('networkidle')
        snapshot('codex-reports-before')
        shown('20 of 24 native reports shown')
        assert 'Codex reports do not identify loaded instruction files.' in page.locator('#project-detail-body').inner_text()
        activate(page.get_by_role('button', name='Load more reports', exact=True))
        shown('24 of 24 native reports shown')
        assert page.evaluate('document.activeElement.id') == 'native-loads-status'
        cards = page.locator('.session-record')
        assert cards.count() == 24
        assert cards.filter(has_text='Codex ·').count() == 21
        assert cards.filter(has_text='Claude ·').count() == 3
        codex = cards.filter(has_text='Codex · Turn interrupted').first
        activate(codex.locator(':scope > details > summary'))
        activate(codex.get_by_text('Complete native report and receiver provenance', exact=True))
        assert '"transcript_path": null' in codex.inner_text()
        assert '"loaded_content_hash": null' in codex.inner_text()
        assert '"occurred_at": null' in codex.inner_text()
        assert '"source_authentication": "local_unattested"' in codex.inner_text()
        activate(codex.get_by_role('link', name='All reports for this session', exact=True))
        shown('20 of 21 native reports shown')
        assert not page.locator('.session-record').filter(has_text='Claude ·').count()
        selected = page.url
        page.reload()
        page.wait_for_load_state('networkidle')
        shown('20 of 21 native reports shown')
        assert page.url == selected
        activate(page.get_by_role('button', name='Load more reports', exact=True))
        shown('21 of 21 native reports shown')
        activate(page.get_by_role('link', name='Show all project reports', exact=True))
        shown('20 of 24 native reports shown')
        activate(page.get_by_role('button', name='Load more reports', exact=True))
        shown('24 of 24 native reports shown')
        claude = page.locator('.session-record').filter(has_text='Claude ·').first
        activate(claude.locator(':scope > details > summary'))
        activate(claude.get_by_role('link', name='All reports for this session', exact=True))
        shown('3 of 3 native reports shown')
        assert not page.locator('.session-record').filter(has_text='Codex ·').count()
        snapshot('codex-reports-claude-separated')
        activate(page.get_by_role('link', name='Session evidence', exact=True))
        shown('2 of 2 session/copy records shown')
        session = page.locator('.session-record').filter(has_text='codex ·').first
        activate(session.locator(':scope > details > summary'))
        activate(session.get_by_role('link', name='Inspect native loading reports', exact=True))
        # Returning to a loaded session preserves its complete cached page.
        shown('21 of 21 native reports shown')
        for width in (1440, 1280):
            page.set_viewport_size({'width':width, 'height':1024})
            for theme in ('light','dark'):
                page.evaluate('(t)=>document.documentElement.dataset.theme=t', theme)
                page.locator('#main').evaluate('(e)=>e.scrollTop=0')
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                snapshot(f'codex-reports-{theme}-{width}')
        page.route('**/api/project-native-events?*', lambda route: route.abort())
        activate(page.get_by_role('button', name='Refresh loading reports', exact=True))
        page.locator('#project-detail-body [role="alert"]').wait_for()
        assert page.locator('.session-record').count() == 21
        page.unroute('**/api/project-native-events?*')
        activate(page.get_by_role('button', name='Refresh loading reports', exact=True))
        shown('20 of 21 native reports shown')
        page.wait_for_load_state('networkidle')
        assert not page.locator('#project-detail-body [role="alert"]').count()
        assert page.evaluate('document.activeElement.id') == 'native-loads-refresh'
        page.goto(url + '&logical_session_key=' + 'e'*64)
        page.wait_for_load_state('networkidle')
        shown('0 of 0 native reports shown')
        assert 'This does not mean no instructions loaded.' in page.locator('#project-detail-body').inner_text()
        assert all(method == 'GET' for method, address in requests if address.startswith('http://127.0.0.1:8876'))
        assert not errors, errors
        assert Path(info['target']).read_text() == info['target_text']
        with sqlite3.connect(info['db']) as db:
            assert list(db.iterdump()) == info['snapshot']
            assert db.execute('SELECT COUNT(*) FROM llm_calls').fetchone()[0] == 0
        print('CODEX_REPORTS_BROWSER_OK: 20+4 mixed receipts, 21 Codex and 3 Claude, provider-separated session links, actual Codex session entry, keyboard/focus, unknown bytes/time, reload/error/empty states, both themes/widths, GET-only, unchanged Store/target, zero models')
    except Exception:
        snapshot('codex-reports-failure')
        raise
    finally:
        browser.close()
