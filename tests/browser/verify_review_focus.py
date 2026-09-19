"""Review focus checks stay valid across real subtree repaints; no private state."""
from pathlib import Path
import argparse
import hashlib
import json
import sqlite3

from playwright.sync_api import sync_playwright, expect
from keyboard_actions import KeyboardActions
from review_focus import assert_review_card_focus, review_card_snapshot

ROOT = Path(__file__).resolve().parents[2]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--label', default='current', help='Evidence subdirectory for this run')
args = parser.parse_args()
assert args.label and all(c.isalnum() or c in '-_' for c in args.label)
OUT = ROOT / 'reports/dashboard-parity/review-focus' / args.label
OUT.mkdir(parents=True, exist_ok=True)
manifest = json.loads((ROOT / 'reports/dashboard-parity/overview-manifest.json').read_text())
assert 'si-overview-ui-' in manifest['db']
report = {'status': 'attempted', 'samples': [], 'negative_checks': [], 'requests': [], 'errors': [],
          'app_sha256': hashlib.sha256((ROOT / 'src/self_improve/dashboard/static/app.js').read_bytes()).hexdigest(),
          'helper_sha256': hashlib.sha256((Path(__file__).with_name('review_focus.py')).read_bytes()).hexdigest()}

with sync_playwright() as runtime:
    browser = runtime.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width': 1280, 'height': 1024})
    page.on('pageerror', lambda e: report['errors'].append(str(e)))
    page.on('request', lambda r: report['requests'].append([r.method, r.url]))
    keys = KeyboardActions(page, OUT / 'keyboard.json')
    try:
        page.goto('http://127.0.0.1:8876/#/review')
        page.wait_for_load_state('networkidle')
        health = page.request.get('http://127.0.0.1:8876/api/health').json()
        assert health['db_path'] == manifest['db'] and health['read_only']
        page.screenshot(path=str(OUT / 'before.png'))
        (OUT / 'before.html').write_text(page.content())
        for width in (1280, 1440):
            page.set_viewport_size({'width': width, 'height': 1024})
            for theme in ('light', 'dark'):
                label = f'{theme}-{width}'
                keys.theme(theme)
                keys.activate(page.locator('#skip-to-main'))
                page.keyboard.press('k')
                expect(page.locator('.review-card--selected')).to_be_focused()
                page.wait_for_load_state('networkidle')
                assert_review_card_focus(page)
                # The existing refresh boundary replaces the node, retaining focus.
                # Fixed sample count is a diagnostic stress condition, not a retry.
                page.evaluate('''async() => {
                    const app = await import('/app.js');
                    window.__reviewFocusPaint = setInterval(() => app.paintReview(), 1);
                }''')
                try:
                    for _ in range(100):
                        report['samples'].append({'state': label, **review_card_snapshot(page)})
                finally:
                    page.evaluate('clearInterval(window.__reviewFocusPaint)')
                assert_review_card_focus(page)
                page.screenshot(path=str(OUT / f'repaint-{label}.png'))
                # A current offscreen card must still fail the exact viewport test.
                page.keyboard.press('End')
                page.wait_for_function("document.querySelector('.review-card--selected').getBoundingClientRect().bottom <= 0")
                offscreen = review_card_snapshot(page)
                assert offscreen['focused'] and not offscreen['visible'], offscreen
                try:
                    assert_review_card_focus(page)
                except AssertionError:
                    report['negative_checks'].append({'state': label, 'case': 'offscreen', **offscreen})
                else:
                    raise AssertionError('Offscreen focus must be rejected')
                page.screenshot(path=str(OUT / f'offscreen-{label}.png'))
                page.keyboard.press('k')
                assert_review_card_focus(page)
                # Native Tab traversal moves focus outside the card. Background
                # repaint must preserve that location and the check must fail.
                keys.reach(page.locator('#theme-toggle'))
                page.evaluate("async() => (await import('/app.js')).paintReview()")
                expect(page.locator('#theme-toggle')).to_be_focused()
                try:
                    assert_review_card_focus(page)
                except AssertionError:
                    report['negative_checks'].append({'state': label, 'case': 'wrong-focus', **review_card_snapshot(page)})
                else:
                    raise AssertionError('Wrong focus must be rejected')
                page.screenshot(path=str(OUT / f'other-focus-{label}.png'))
        # A missing family has no selected card and must not satisfy the check.
        page.goto('http://127.0.0.1:8876/#/review/family/missing-fixture-family')
        page.wait_for_load_state('networkidle')
        expect(page.locator('[data-review-empty]')).to_contain_text('no longer in Review')
        try:
            assert_review_card_focus(page)
        except AssertionError:
            report['negative_checks'].append({'case': 'missing-card', **review_card_snapshot(page)})
        else:
            raise AssertionError('A missing card must be rejected')
        page.screenshot(path=str(OUT / 'missing-card.png'))
        with sqlite3.connect('file:' + manifest['db'] + '?mode=ro', uri=True) as db:
            assert list(db.iterdump()) == manifest['snapshot']
            assert db.execute('SELECT COUNT(*) FROM llm_calls').fetchone()[0] == 0
        assert all(Path(p).read_text() == value for p, value in manifest['targets'].items())
        assert all(method == 'GET' for method, _ in report['requests'])
        assert not report['errors'], report['errors']
        failures = [sample for sample in report['samples'] if not all(sample[k] for k in ('connected', 'focused', 'visible'))]
        report['failures'] = failures
        assert not failures, f'{len(failures)} failed focus snapshots: {failures[:3]}'
        assert len(report['samples']) == 400 and len(report['negative_checks']) == 9
        report.update(status='succeeded', store='unchanged', targets='unchanged', model_calls=0)
        print('REVIEW_FOCUS_OK 400 current snapshots; 9 negative checks; unchanged Store/targets; zero models')
    except BaseException:
        report['status'] = 'failed'
        page.screenshot(path=str(OUT / 'failure.png'))
        (OUT / 'failure.html').write_text(page.content())
        raise
    finally:
        page.evaluate('clearInterval(window.__reviewFocusPaint)')
        (OUT / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
        browser.close()
