"""Read-only real-browser failure causes, fix evidence and exact-run selection."""
from pathlib import Path
import argparse
import json
import sqlite3
from playwright.sync_api import sync_playwright, expect
from keyboard_actions import KeyboardActions
from visual_checks import VisualChecks

parser = argparse.ArgumentParser()
parser.add_argument('--mode', choices=('fixed', 'later'), default='fixed')
args = parser.parse_args()
ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT/'reports/dashboard-parity/failures'/args.mode
info = json.loads((OUT/'manifest.json').read_text())
assert 'si-failure-ui-' in info['db'] and info['mode'] == args.mode
BASE = 'http://127.0.0.1:8876/'
result = {'requests':[], 'errors':[], 'captures':[]}
with sync_playwright() as runtime:
    browser = runtime.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width':1280, 'height':1024})
    keys = KeyboardActions(page, OUT/'keyboard.json')
    visual = VisualChecks(page, OUT/'contrast.json')
    page.on('pageerror', lambda error:result['errors'].append(str(error)))
    page.on('request', lambda request:result['requests'].append([request.method, request.url]))

    def capture(name, target):
        target.scroll_into_view_if_needed()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        visual.check(name)
        page.screenshot(path=str(OUT/(name+'.png')))
        result['captures'].append(name)

    def themes(name, target):
        for width in (1280, 1440):
            page.set_viewport_size({'width':width, 'height':1024})
            for theme in ('light', 'dark'):
                keys.theme(theme)
                capture(f'{name}-{theme}-{width}', target)

    try:
        page.goto(BASE+'#/overview')
        page.wait_for_load_state('networkidle')
        page.screenshot(path=str(OUT/'initial.png'))
        (OUT/'initial.html').write_text(page.content())
        result['initial_buttons'] = page.locator('button:visible').all_text_contents()
        # The old exact constraint never appears among the current seven-day classes.
        active = page.locator('#ov-failures .overview-failure')
        expect(active).to_have_count(3)
        assert all('incident_learnings' not in text for text in active.all_text_contents())
        keys.activate(page.locator('#ov-failure-history > summary'))
        history = page.locator('#ov-failure-history')
        fixed = history.locator('li.failure').filter(has=page.get_by_text('Recorded source fix', exact=True))
        expect(fixed).to_have_count(1)
        badge = 'source fix dated 2026-08-16' if args.mode=='fixed' else 'recorded after the source fix'
        expect(fixed).to_contain_text(badge)
        generic = history.locator('li.failure').filter(has=page.locator('button[aria-label^="IntegrityError: SQLite"]'))
        expect(generic).to_have_count(1)
        expect(generic).not_to_contain_text('source fix')
        summary = fixed.get_by_text('Recorded source fix', exact=True)
        keys.activate(summary)
        expect(fixed).to_contain_text('cfd6410b5469b2e2c718c16c1c042e2f1c3f123a')
        expect(fixed).to_contain_text('idempotent')
        expect(fixed).to_contain_text('do not establish deployment')
        if args.mode=='later':
            expect(fixed.get_by_role('link', name='old-later-occurrence', exact=True)).to_have_count(1)
        page.evaluate('async()=>(await import("/app.js")).paintOverview()')
        expect(summary).to_be_focused()
        expect(summary.locator('..')).to_have_attribute('open', '')
        expect(history).to_contain_text('Counted, but not failures')
        expect(history).to_contain_text('Unclassified outcome')
        themes('source-fix', fixed)
        keys.activate(page.locator('#ov-failures .overview-failure a[href="#/overview/run/active-errors"]').first)
        page.wait_for_load_state('networkidle')
        expect(page.locator('#run-title')).to_be_visible()
        mine = page.locator('#run-stage-mine')
        expect(mine).to_contain_text('The call never produced an answer: 2')
        keys.activate(page.locator('#run-causes-mine'))
        for text in ('failed to launch', 'No answer we could read', 'Answer was missing required fields',
                     'A database constraint rejected a write', 'call_failed:spawn_error',
                     'budget_exhausted', 'NovelCause:invented', 'not unique incident counts'):
            expect(mine).to_contain_text(text)
        themes('run-causes', mine.locator('details').first)
        keys.activate(page.locator('#run-causes-mine'))
        keys.activate(page.locator('#run-load-calls'))
        calls = page.locator('#run-section-calls')
        expect(calls).to_contain_text('The CLI would not start')
        expect(calls).to_contain_text('The agent process failed to launch')
        expect(calls).to_contain_text('The call produced no output')
        expect(calls).to_contain_text('No answer we could read')
        themes('call-causes', calls)
        keys.choose(page.locator('#run-selector'), 'same-time-neighbor')
        expect(page.locator('#run-stage-mine')).to_contain_text('The call exceeded its deadline: 99')
        expect(page.locator('#run-stage-mine')).not_to_contain_text('failed to launch')
        page.reload()
        page.wait_for_load_state('networkidle')
        expect(page.locator('#run-stage-mine')).to_contain_text('The call exceeded its deadline: 99')
        page.go_back()
        page.wait_for_load_state('networkidle')
        expect(page.locator('#run-stage-mine')).to_contain_text('The call never produced an answer: 2')
        assert not result['errors'], result['errors']
        assert all(method=='GET' for method, url in result['requests'])
        with sqlite3.connect('file:'+info['db']+'?mode=ro', uri=True) as db:
            assert list(db.iterdump()) == info['sql']
            assert db.execute('SELECT COUNT(*) FROM llm_calls').fetchone()[0] == info['synthetic_calls']
        assert all(Path(path).read_text()==text for path, text in info['targets'].items())
        visual.assert_clean()
        result.update(store='unchanged', targets='unchanged', synthetic_calls=4, provider_executions=0)
        print('FAILURE_BROWSER_OK: '+args.mode+'; exact fix, readable causes, native disclosure/focus, same-time selection/reload, unchanged Store/targets, zero provider executions')
    except BaseException:
        page.screenshot(path=str(OUT/'failure.png'))
        (OUT/'failure.html').write_text(page.content())
        raise
    finally:
        keys.save()
        browser.close()
        (OUT/'result.json').write_text(json.dumps(result, indent=2))
