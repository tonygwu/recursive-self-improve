"""Read-only real-browser adequacy, arithmetic, workload, and keyboard checks."""
from pathlib import Path
from urllib.parse import quote
import json
import re
import sqlite3
from playwright.sync_api import sync_playwright, expect
from keyboard_actions import KeyboardActions
from visual_checks import VisualChecks

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'reports/dashboard-parity/rate-coverage'
info = json.loads((OUT / 'manifest.json').read_text())
assert 'si-rate-coverage-' in info['db']
BASE = 'http://127.0.0.1:8876/'
result = {'requests':[], 'errors':[], 'captures':[]}
with sync_playwright() as runtime:
    browser = runtime.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width':1280, 'height':1024})
    keys = KeyboardActions(page, OUT / 'keyboard.json')
    visual = VisualChecks(page, OUT / 'contrast.json')
    page.on('pageerror', lambda error:result['errors'].append(str(error)))
    page.on('request', lambda request:result['requests'].append([request.method, request.url]))

    def capture(name):
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        visual.check(name)
        page.screenshot(path=str(OUT / (name + '.png')))
        result['captures'].append(name)

    try:
        page.goto(BASE + '#/evals')
        page.wait_for_load_state('networkidle')
        page.screenshot(path=str(OUT / 'initial.png'))
        (OUT / 'initial.html').write_text(page.content())
        result['initial_buttons'] = page.locator('#view-evals button').all_text_contents()
        expect(page.locator('#trend-options-toggle')).to_contain_text('Choose detector configuration')
        expect(page.locator('.trend-matrix')).to_have_count(0)
        keys.choose(page.locator('#trend-compatibility_key'), info['version'])
        keys.activate(page.locator('#trend-submit'))
        page.wait_for_url('**/*compatibility_key=*')
        page.wait_for_load_state('networkidle')
        expect(page.locator('#trend-submit')).to_be_focused()
        matrix = page.locator('.trend-matrix')
        row = matrix.locator('tr').filter(has=page.get_by_role('rowheader', name='correction', exact=True))
        cells = row.locator('td[data-trend-month]')
        expect(cells).to_have_count(7)
        # May/June are empty; July has one session; August is an adequate zero.
        expect(cells.nth(3)).to_have_attribute('aria-label', re.compile('No eligible exposure; no rate'))
        expect(cells.nth(4)).to_have_attribute('aria-label', re.compile('Insufficient sessions; trend rate unavailable'))
        expect(cells.nth(4).locator('.spark__bar')).to_have_count(0)
        expect(cells.nth(5).locator('.trend-value')).to_have_text('0')
        expect(cells.nth(5).locator('.spark__bar')).to_have_count(1)
        expect(cells.nth(6).locator('.trend-value')).to_have_text('25,000')
        expect(cells.nth(6)).to_have_attribute('data-partial', 'true')
        keys.reach(cells.nth(4).locator('button'))
        expect(cells.nth(4).locator('button')).to_be_focused()
        expect(matrix).to_contain_text('Median lines / session')
        expect(matrix).to_contain_text('Largest session, lines')
        expect(matrix).to_contain_text('Unknown-session lines')
        keys.activate(page.locator('#trend-arithmetic-toggle'))
        raw = page.locator('#trend-arithmetic-toggle').locator('..')
        expect(raw).to_contain_text('does not make a small sample adequate')
        july = raw.locator('article').filter(has_text='2030-07')
        expect(july).to_contain_text('1 known sessions · 4 eligible lines')
        expect(july).to_contain_text('correction: 0 occurrences · 0 observed per 100,000 lines')
        capture('raw-arithmetic-light-1280')
        page.evaluate('async()=>(await import("/app.js")).paintTrends()')
        expect(page.locator('#trend-arithmetic-toggle')).to_be_focused()
        expect(raw).to_have_attribute('open', '')
        keys.activate(page.locator('#trend-arithmetic-toggle'))
        for width in (1280, 1440):
            page.set_viewport_size({'width':width, 'height':1024})
            for theme in ('light', 'dark'):
                keys.theme(theme)
                matrix.scroll_into_view_if_needed()
                zero = cells.nth(5).locator('.spark__bar').bounding_box()
                positive = cells.nth(6).locator('.spark__bar').bounding_box()
                assert zero['height'] == 0, ('Measured zero has positive geometry', zero)
                assert positive['height'] > 0, positive
                expect(cells.nth(4).locator('.spark__bar')).to_have_count(0)
                capture(f'months-{theme}-{width}')
        page.reload()
        page.wait_for_load_state('networkidle')
        expect(page.locator('#trend-options-toggle')).to_contain_text(info['version'][:12])
        sides = []
        for route, scope in [('#/evals', 'evals'), ('#/projects/'+quote(info['project_key'], safe='')+'?tab=recurrence', 'project')]:
            page.goto(BASE + route)
            page.wait_for_load_state('networkidle')
            expect(page.locator('#recurrence-status-'+scope)).to_contain_text('1 of 1 measurements shown')
            panel = page.locator('#project-detail-body' if scope == 'project' else '#trends-body')
            record = panel.locator('[data-measurement-id="'+info['measurement_id']+'"]').locator('..')
            expect(record).to_contain_text('Rate unavailable · insufficient sessions')
            expect(record).to_contain_text('19 sessions (minimum 20)')
            expect(record).to_contain_text('median 4 · largest 4 lines')
            expect(record).to_contain_text('codex: 76 lines')
            sides.append(record.locator('.field').all_text_contents())
            if scope == 'evals':
                keys.activate(record.locator('[data-measurement-id]'))
            else:
                expect(record.locator('[data-measurement-id]')).to_have_text('Measurement loaded')
            disclosure = record.locator('summary').filter(has_text='Complete windows, availability')
            keys.activate(disclosure)
            expect(record).to_contain_text('"rate_per_100k": 13157.894736842105')
            for theme in ('light', 'dark'):
                keys.theme(theme)
                record.scroll_into_view_if_needed()
                capture(f'recurrence-{scope}-{theme}')
        assert sides[0] == sides[1] and sides[0], sides
        assert not result['errors'], result['errors']
        assert all(method == 'GET' for method, url in result['requests'])
        with sqlite3.connect('file:'+info['db']+'?mode=ro', uri=True) as db:
            assert list(db.iterdump()) == info['sql']
            assert db.execute('SELECT COUNT(*) FROM llm_calls').fetchone()[0] == 0
        assert all(Path(path).read_text() == text for path, text in info['targets'].items())
        visual.assert_clean()
        print('RATE_COVERAGE_BROWSER_OK: small/empty/adequate zero and positive months; raw arithmetic; versions/reload/keyboard; identical Project/Evals workload; immutable Store and targets; zero calls')
    except BaseException:
        page.screenshot(path=str(OUT / 'failure.png'))
        (OUT / 'failure.html').write_text(page.content())
        raise
    finally:
        keys.save()
        browser.close()
        (OUT / 'result.json').write_text(json.dumps(result, indent=2))
