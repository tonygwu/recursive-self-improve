"""Review failures recover independently through real disposable commands and GETs."""
from pathlib import Path
from urllib.parse import quote
import hashlib
import json
import sqlite3

from playwright.sync_api import sync_playwright, expect
from keyboard_actions import KeyboardActions
from visual_checks import VisualChecks

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'reports/dashboard-parity/review-decision-errors/browser'
OUT.mkdir(parents=True, exist_ok=True)
info = json.loads((ROOT / 'reports/dashboard-parity/review-layout/manifest.json').read_text())
assert 'si-review-layout-' in info['db']
BASE = 'http://127.0.0.1:8876/'
first, second = info['families']['mixed'], info['families']['conflict']
report = {'status': 'attempted', 'posts': [], 'errors': [], 'captures': [],
          'app_sha256': hashlib.sha256((ROOT / 'src/self_improve/dashboard/static/app.js').read_bytes()).hexdigest()}

with sync_playwright() as runtime:
    browser = runtime.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width': 1280, 'height': 1024})
    page.on('pageerror', lambda e: report['errors'].append(str(e)))
    page.on('request', lambda r: report['posts'].append(r.post_data_json) if r.method == 'POST' else None)
    keys = KeyboardActions(page, OUT / 'keyboard.json')
    visual = VisualChecks(page, OUT / 'contrast.json')
    alert = page.locator('#global-error')
    lesson = page.locator('[data-decision="reject_lesson"]')

    def failure(family):
        return alert.locator(f'section[data-error-owner^="review-decision:{family}:"]')

    def ready(family):
        page.goto(BASE + '#/review/family/' + quote(family, safe=''))
        expect(page.locator('.review-detail')).to_have_attribute('data-learning-id', family)
        expect(lesson).to_be_enabled()

    def capture(label):
        visual.check(label)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.screenshot(path=str(OUT / f'{label}.png'))
        report['captures'].append(label)

    def stale(route):
        body = route.request.post_data_json
        body['members'][0]['revision'] = '0' * 64
        response = route.fetch(post_data=json.dumps(body))
        assert response.status == 409
        route.fulfill(response=response)

    try:
        ready(first)
        page.wait_for_load_state('networkidle')
        assert page.request.get(BASE + 'api/health').json()['db_path'] == info['db']
        page.screenshot(path=str(OUT / 'before.png'))
        page.route('**/api/commands', stale)
        keys.reach(lesson)
        page.keyboard.press('Shift+R')
        expect(failure(first)).to_be_visible()
        expect(lesson).to_be_enabled()
        page.unroute('**/api/commands', stale)
        details = failure(first).locator('details')
        for width in (1280, 1440):
            page.set_viewport_size({'width': width, 'height': 1024})
            for theme in ('light', 'dark'):
                keys.theme(theme)
                summary = details.locator('summary')
                if details.get_attribute('open') is not None:
                    keys.activate(summary, 'Space')
                keys.reach(summary)
                capture(f'refused-{theme}-{width}')
                keys.activate(summary, 'Space')
                expect(summary).to_be_focused()
                record = json.loads(details.locator('pre').inner_text())
                assert set(record['proposal_ids']) == set(info['mixed'])
                assert record['status'] == 409 and record['action'] == 'reject_lesson'
                assert 'changed. Reload before rejecting' in record['error']
                diagnostic = details.get_by_role('region', name='Complete decision diagnostic', exact=True)
                keys.reach(diagnostic)
                # Each theme reuses this native scroller. Reset to its beginning
                # before testing End; End at an already reached bottom correctly
                # chains scrolling to the surrounding page in Chromium.
                if diagnostic.evaluate('el=>el.scrollTop > 0'):
                    diagnostic.evaluate("el=>{el.dataset.scrollDone='false';el.addEventListener('scrollend',()=>el.dataset.scrollDone='true',{once:true});}")
                    page.keyboard.press('Home')
                    page.wait_for_function("el=>el.dataset.scrollDone === 'true' && el.scrollTop === 0", arg=diagnostic.element_handle())
                diagnostic.evaluate("el=>{el.dataset.scrollDone='false';el.addEventListener('scrollend',()=>el.dataset.scrollDone='true',{once:true});}")
                page.keyboard.press('End')
                page.wait_for_function("el=>el.dataset.scrollDone === 'true' && el.scrollTop + el.clientHeight >= el.scrollHeight - 2", arg=diagnostic.element_handle())
                capture(f'details-{theme}-{width}')
        with sqlite3.connect('file:' + info['db'] + '?mode=ro', uri=True) as db:
            assert db.execute('SELECT COUNT(*) FROM commands').fetchone()[0] == 0
        # A second family's failure must coexist with the first one.
        ready(second)
        second_pending = []
        page.route('**/api/commands', lambda route: second_pending.append(route))
        keys.reach(lesson)
        page.keyboard.press('Shift+R')
        expect(lesson).to_be_disabled()
        diagnostic = failure(first).get_by_role('region', name='Complete decision diagnostic', exact=True)
        keys.reach(diagnostic)
        diagnostic_scroll = diagnostic.evaluate('el=>el.scrollTop')
        assert len(second_pending) == 1
        stale(second_pending[0])
        expect(failure(second)).to_be_visible()
        expect(lesson).to_be_enabled()
        expect(diagnostic).to_be_focused()
        assert abs(diagnostic.evaluate('el=>el.scrollTop') - diagnostic_scroll) <= 2
        page.unroute('**/api/commands')
        expect(failure(first)).to_be_visible()
        # A lost response retains the original key; no command reaches the
        # fixture on this attempt. The later explicit retry uses that same key.
        ready(first)
        page.route('**/api/commands', lambda route: route.abort('failed'))
        keys.reach(lesson)
        page.keyboard.press('Shift+R')
        expect(failure(first).locator('pre')).to_contain_text('Failed to fetch')
        expect(lesson).to_be_enabled()
        page.unroute('**/api/commands')
        assert report['posts'][0]['request_key'] == report['posts'][2]['request_key']
        pending = []
        page.route('**/api/commands', lambda route: pending.append(route))
        keys.reach(lesson)
        with page.expect_request(lambda r: r.method == 'POST' and r.url.endswith('/api/commands')):
            page.keyboard.press('Shift+R')
        expect(lesson).to_be_disabled()
        assert len(pending) == 1
        # Exercise the real Overview Refresh while that decision is pending.
        page.route('**/api/overview', lambda route: route.fulfill(status=503, json={'detail': 'Invented current read failed'}))
        page.goto(BASE + '#/overview')
        keys.activate(page.locator('#ov-refresh'))
        expect(alert.locator('section[data-error-owner="dashboard-read"]')).to_contain_text('Invented current read failed')
        expect(failure(first).locator('details')).to_have_attribute('open', '')
        keys.reach(failure(first).locator('summary'))
        capture('pending-with-unrelated-read-error')
        with page.expect_response(lambda r: r.request.method == 'POST' and r.url.endswith('/api/commands')):
            pending[0].continue_()
        page.unroute('**/api/commands')
        expect(failure(first)).to_have_count(0)
        expect(page.locator('#main')).to_be_focused()
        expect(failure(second)).to_be_visible()
        expect(alert).to_contain_text('Invented current read failed')
        capture('confirmed-with-other-errors')
        assert report['posts'][0]['request_key'] == report['posts'][3]['request_key']
        page.unroute('**/api/overview')
        keys.activate(alert.get_by_role('button', name='Retry dashboard reads', exact=True))
        expect(alert.locator('section[data-error-owner="dashboard-read"]')).to_have_count(0)
        expect(failure(second)).to_be_visible()
        capture('read-recovered-other-family-retained')
        assert len(report['posts']) == 4
        # A confirmed second decision plus a failed queue read must keep its
        # recorded outcome explicit. GET recovery then reaches empty Review.
        ready(second)
        page.route('**/api/review-queue', lambda route: route.fulfill(status=503, json={'detail': 'Invented queue read failed'}))
        keys.reach(lesson)
        page.keyboard.press('Shift+R')
        expect(alert).to_contain_text('Decision recorded; Review could not be refreshed')
        expect(failure(second)).to_have_count(0)
        expect(alert).to_contain_text('is completed')
        capture('recorded-with-failed-queue-read')
        page.unroute('**/api/review-queue')
        keys.activate(alert.get_by_role('button', name='Retry dashboard reads', exact=True))
        expect(alert).to_be_hidden()
        expect(page.locator('#main')).to_be_focused()
        expect(page.locator('#review-count')).to_have_text('0')
        page.keyboard.press('Escape')
        expect(page).to_have_url(BASE + '#/review')
        expect(page.locator('.review-detail')).to_have_count(0)
        capture('empty-review-after-read-recovery')
        # Delivery history has its own read result and refresh control. Confirm
        # that reader separately instead of clearing its cause from a queue GET.
        keys.activate(page.locator('#review-delivery').get_by_role('button', name='Refresh', exact=True))
        expect(page.locator('#review-delivery .delivery-error')).to_have_count(0)
        page.locator('#review-count').scroll_into_view_if_needed()
        capture('delivery-history-recovered')
        assert len(report['posts']) == 5
        with sqlite3.connect('file:' + info['db'] + '?mode=ro', uri=True) as db:
            commands = db.execute('SELECT id,action,state,request_key,max_model_calls,payload_json FROM commands ORDER BY created_at').fetchall()
            assert len(commands) == 2
            assert all((c[1], c[2], c[4]) == ('reject_lesson', 'completed', 0) for c in commands)
            assert {c[3] for c in commands} == {report['posts'][3]['request_key'], report['posts'][4]['request_key']}
            assert sorted(len(json.loads(c[5])['members']) for c in commands) == [2, 20]
            assert db.execute('SELECT COUNT(*) FROM llm_calls').fetchone()[0] == 0
            assert db.execute('SELECT COUNT(*) FROM command_targets').fetchone()[0] == 0
            assert db.execute("SELECT COUNT(*) FROM learnings WHERE status='rejected'").fetchone()[0] == 2
        assert all(Path(p).read_text() == value for p, value in info['targets'].items())
        assert not report['errors'], report['errors']
        visual.assert_clean()
        report.update(status='succeeded', commands=2, reviewed_members=22, final_queue=0, targets='unchanged', model_calls=0)
        print('REVIEW_ERROR_RECOVERY_OK: independent errors; exact-key retry; two commands; 22 members; empty queue; zero models')
    except BaseException:
        report['status'] = 'failed'
        page.screenshot(path=str(OUT / 'failure.png'))
        (OUT / 'failure.html').write_text(page.content())
        raise
    finally:
        (OUT / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
        browser.close()
