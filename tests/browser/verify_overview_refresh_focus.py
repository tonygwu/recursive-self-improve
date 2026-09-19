"""Native focus ownership across delayed Overview reads using disposable state."""
from pathlib import Path
from urllib.parse import quote
import argparse
import hashlib
import json
import sqlite3

from playwright.sync_api import sync_playwright, expect

from keyboard_actions import KeyboardActions
from visual_checks import VisualChecks

ROOT = Path(__file__).resolve().parents[2]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--fixture', choices=['overview', 'review'], required=True)
args = parser.parse_args()
OUT = ROOT / 'reports/dashboard-parity/overview-refresh-focus' / args.fixture
OUT.mkdir(parents=True, exist_ok=True)
manifest = ROOT / ('reports/dashboard-parity/overview-manifest.json' if args.fixture == 'overview'
                   else 'reports/dashboard-parity/review-layout/manifest.json')
info = json.loads(manifest.read_text())
assert ('si-overview-ui-' if args.fixture == 'overview' else 'si-review-layout-') in info['db']
BASE = 'http://127.0.0.1:8876/'
report = {'status': 'attempted', 'fixture': args.fixture, 'cases': [], 'errors': [], 'posts': 0,
          'app_sha256': hashlib.sha256((ROOT / 'src/self_improve/dashboard/static/app.js').read_bytes()).hexdigest()}


def snapshot():
    with sqlite3.connect('file:' + info['db'] + '?mode=ro', uri=True) as db:
        return list(db.iterdump())


with sync_playwright() as runtime:
    browser = runtime.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width': 1280, 'height': 1024})
    page.on('pageerror', lambda e: report['errors'].append(str(e)))
    page.on('request', lambda r: report.update(posts=report['posts'] + 1) if r.method == 'POST' else None)
    keys = KeyboardActions(page, OUT / 'keyboard.json')
    visual = VisualChecks(page, OUT / 'contrast.json')
    refresh = page.locator('#ov-refresh')

    def capture(label):
        visual.check(label)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.screenshot(path=str(OUT / (label + '.png')))
        report['cases'].append(label)

    def hold():
        pending = []
        page.route('**/api/overview', lambda route: pending.append(route))
        keys.activate(refresh)
        expect(refresh).to_be_disabled()
        assert len(pending) == 1
        return pending[0]

    def finish(route, failed):
        if failed:
            route.fulfill(status=503, json={'detail': 'Invented delayed Overview failure'})
        else:
            route.continue_()
        expect(refresh).to_be_enabled()
        page.unroute('**/api/overview')

    try:
        before = snapshot()
        for width in (1280, 1440):
            page.set_viewport_size({'width': width, 'height': 1024})
            for theme in ('light', 'dark'):
                if args.fixture == 'overview':
                    for failed in (False, True):
                        for destination in ('unmoved', 'same-view', 'another-view'):
                            page.goto(BASE + '#/overview')
                            page.wait_for_load_state('networkidle')
                            assert page.request.get(BASE + 'api/health').json()['db_path'] == info['db']
                            page.screenshot(path=str(OUT / 'before.png'))
                            keys.theme(theme)
                            route = hold()
                            target = refresh
                            if destination == 'same-view':
                                target = page.locator('#ov-grid-numbers')
                                keys.reach(target)
                            elif destination == 'another-view':
                                target = page.locator('#nav-projects')
                                keys.activate(target)
                                expect(page).to_have_url(BASE + '#/projects')
                            finish(route, failed)
                            expect(target).to_be_focused()
                            keys.focused(target)
                            if failed:
                                expect(page.locator('#global-error')).to_contain_text('Invented delayed Overview failure')
                            else:
                                expect(page.locator('#global-error')).to_be_hidden()
                            capture(f'{destination}-{"failed" if failed else "success"}-{theme}-{width}')
                else:
                    family = info['families']['mixed']
                    page.goto(BASE + '#/review/family/' + quote(family, safe=''))
                    page.wait_for_load_state('networkidle')
                    page.screenshot(path=str(OUT / 'before.png'))
                    keys.theme(theme)

                    def refuse(route):
                        body = route.request.post_data_json
                        body['members'][0]['revision'] = '0' * 64
                        response = route.fetch(post_data=json.dumps(body))
                        assert response.status == 409
                        route.fulfill(response=response)

                    page.route('**/api/commands', refuse)
                    lesson = page.locator('[data-decision="reject_lesson"]')
                    expect(lesson).to_be_enabled()
                    keys.activate(lesson)
                    diagnostic = page.locator('#global-error details')
                    expect(diagnostic).to_be_visible()
                    expect(lesson).to_be_enabled()
                    page.unroute('**/api/commands')
                    if diagnostic.get_attribute('open') is None:
                        keys.activate(diagnostic.locator('summary'), 'Space')
                    expect(diagnostic).to_have_attribute('open', '')
                    keys.activate(page.locator('#nav-overview'))
                    route = hold()
                    target = diagnostic.get_by_role('region', name='Complete decision diagnostic', exact=True)
                    keys.reach(target)
                    finish(route, True)
                    expect(target).to_be_focused()
                    keys.focused(target)
                    expect(page.locator('#global-error')).to_contain_text('Invented delayed Overview failure')
                    expect(target).to_contain_text('Reload before rejecting')
                    capture(f'diagnostic-failed-{theme}-{width}')
        assert snapshot() == before == info['snapshot']
        assert all(Path(p).read_text() == text for p, text in info['targets'].items())
        assert report['posts'] == (0 if args.fixture == 'overview' else 4)
        assert not report['errors'], report['errors']
        visual.assert_clean()
        report.update(status='succeeded', store='unchanged', targets='unchanged', model_calls=0)
        print(f'OVERVIEW_REFRESH_BROWSER_OK: {len(report["cases"])} {args.fixture} cases; native focus; unchanged Store and targets; zero models')
    except BaseException:
        report['status'] = 'failed'
        page.screenshot(path=str(OUT / 'failure.png'))
        (OUT / 'failure.html').write_text(page.content())
        raise
    finally:
        (OUT / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
        browser.close()
