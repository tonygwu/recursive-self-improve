"""V2 native shortcuts through real temporary Review commands and retained scope."""
from pathlib import Path
from urllib.parse import quote
import argparse
import hashlib
import json
import sqlite3

from playwright.sync_api import sync_playwright, expect
from keyboard_actions import KeyboardActions
from review_focus import assert_review_card_focus
from visual_checks import VisualChecks

ROOT = Path(__file__).resolve().parents[2]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--output', type=Path, default=ROOT / 'reports/dashboard-parity/review-shortcuts-v2/browser')
OUT = parser.parse_args().output
OUT.mkdir(parents=True, exist_ok=True)
info = json.loads((ROOT / 'reports/dashboard-parity/review-layout/manifest.json').read_text())
assert 'si-review-layout-' in info['db']
BASE = 'http://127.0.0.1:8876/'
family = info['families']['mixed']
full = BASE + '#/review/family/' + quote(family, safe='')
report = {'status': 'attempted', 'requests': [], 'errors': [], 'captures': [],
          'app_sha256': hashlib.sha256((ROOT / 'src/self_improve/dashboard/static/app.js').read_bytes()).hexdigest()}

with sync_playwright() as runtime:
    browser = runtime.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width': 1280, 'height': 1024})
    page.on('pageerror', lambda e: report['errors'].append(str(e)))
    page.on('request', lambda r: report['requests'].append({'method': r.method, 'url': r.url, 'body': r.post_data_json if r.method == 'POST' else None}))
    keys = KeyboardActions(page, OUT / 'keyboard.json')
    visual = VisualChecks(page, OUT / 'contrast.json')
    lesson = page.locator('[data-decision="reject_lesson"]')

    def posts():
        return [r for r in report['requests'] if r['method'] == 'POST']

    def capture(label):
        if lesson.count():
            assert lesson.locator('kbd').evaluate("el=>{const r=document.createRange();r.selectNodeContents(el);return r.getClientRects().length===1;}"), 'Shortcut badge wrapped'
        visual.check(label)
        page.screenshot(path=str(OUT / f'{label}.png'))
        report['captures'].append(label)

    def queue():
        page.goto(full)
        page.wait_for_load_state('networkidle')
        page.keyboard.press('Escape')
        expect(page).to_have_url(BASE + '#/review')
        expect(page.locator('.review-detail')).to_have_count(0)
        expect(page.locator('.review-card--selected')).to_have_attribute('data-learning-id', family)
        expect(lesson).to_be_enabled()
        assert_review_card_focus(page)

    def no_action(keys_to_press):
        before, url = len(posts()), page.url
        for key in keys_to_press:
            page.keyboard.press(key)
        assert len(posts()) == before and page.url == url

    try:
        page.goto(full)
        page.wait_for_load_state('networkidle')
        health = page.request.get(BASE + 'api/health').json()
        assert health['db_path'] == info['db'] and health['read_only']
        page.screenshot(path=str(OUT / 'before.png'))
        (OUT / 'before.html').write_text(page.content())
        for width in (1280, 1440):
            page.set_viewport_size({'width': width, 'height': 1024})
            for theme in ('light', 'dark'):
                queue()
                keys.theme(theme)
                link = page.get_by_role('link', name='Open full review', exact=True)
                keys.reach(link)
                expect(link).to_have_attribute('aria-keyshortcuts', 'o')
                expect(link.locator('kbd')).to_have_text('o')
                expect(lesson).to_have_attribute('aria-keyshortcuts', 'Shift+r')
                expect(lesson.locator('kbd')).to_have_text('⇧r')
                expect(lesson).to_have_accessible_name('Reject lesson everywhere')
                capture(f'queue-{theme}-{width}')
                no_action(['Control+o', 'Alt+o', 'Meta+o', 'Shift+o'])
                page.keyboard.press('o')
                expect(page).to_have_url(full)
                expect(page.locator('.review-detail')).to_be_focused()
                expect(page.locator('.review-detail')).to_contain_text('20 selected proposals')
                capture(f'full-{theme}-{width}')
                page.keyboard.press('Escape')
                expect(page).to_have_url(BASE + '#/review')
                # The hash changes before its event renders the queue. Full
                # Review also has the selected-card class, so focus alone does
                # not prove this navigation has completed.
                expect(page.locator('.review-detail')).to_have_count(0)
                assert_review_card_focus(page)
                assert page.locator('#review-body').evaluate('el=>el.scrollWidth<=el.clientWidth')
        assert not posts()
        # An open native modal consumes navigation and rejection keys as text.
        page.keyboard.press('Control+k')
        expect(page.locator('#navigation-input')).to_be_focused()
        no_action(['o', 'Shift+R'])
        expect(page.locator('#navigation-input')).to_have_value('oR')
        page.keyboard.press('Escape')
        # Existing auxiliary panels keep their keyboard ownership.
        for panel in ('#review-incidents', '#review-delivery'):
            keys.reach(page.locator(panel).get_by_role('button', name='Refresh', exact=True))
            no_action(['o', 'Shift+R'])
        # A pending preview and its read failure never authorize rejection.
        held = []
        pattern = '**/api/review-preview?*'
        def hold_preview(route):
            held.append(route)
        page.route(pattern, hold_preview)
        page.goto(full, wait_until='domcontentloaded')
        # Hash navigation preserves the cached preview; reload the document so
        # this scenario actually holds a new read rather than a valid cache.
        page.reload(wait_until='domcontentloaded')
        expect(lesson).to_be_disabled()
        expect(page.locator('.review-detail')).to_contain_text('Reading the selected edits')
        keys.activate(page.locator('#skip-to-main'))
        no_action(['Shift+R'])
        capture('preview-pending')
        assert len(held) == 1
        held[0].fulfill(status=503, json={'detail': 'Invented preview unavailable'})
        expect(page.locator('.review-detail').get_by_role('alert')).to_contain_text('Invented preview unavailable')
        no_action(['Shift+R'])
        capture('preview-error')
        page.unroute(pattern, hold_preview)
        keys.activate(page.get_by_role('button', name='Reload Review', exact=True))
        expect(lesson).to_be_enabled()
        # A changed retained-content binding is refused before a command.
        def stale_preview(route):
            response = route.fetch()
            data = response.json()
            data['members'][0]['content_revision'] = 'invented-changed-content'
            route.fulfill(response=response, json=data)
        page.route(pattern, stale_preview)
        page.reload()
        expect(page.locator('.review-detail').get_by_role('alert')).to_contain_text('A proposal changed')
        expect(lesson).to_be_disabled()
        keys.activate(page.locator('#skip-to-main'))
        no_action(['Shift+R'])
        capture('preview-stale')
        page.unroute(pattern, stale_preview)
        keys.activate(page.get_by_role('button', name='Reload Review', exact=True))
        expect(lesson).to_be_enabled()
        page.keyboard.press('Escape')
        expect(page).to_have_url(BASE + '#/review')
        # Retain only 19 reviewed members, while the global consequence remains
        # explicit. Keys typed at the native checkbox must do nothing.
        keys.activate(page.locator('[data-review-individual]'))
        checkbox = page.locator('#review-include-' + info['mixed'][0])
        keys.activate(checkbox, 'Space')
        expect(checkbox).not_to_be_checked()
        expect(page.locator('[data-decision="approve"]')).to_contain_text('(19)')
        no_action(['o', 'Shift+R'])
        expect(lesson).to_be_enabled()
        expected = page.evaluate('''async id => (await import('/app.js')).state.reviewPreviews[id].data.members.map(m=>({proposal_id:m.proposal_id,revision:m.revision})).sort((a,b)=>a.proposal_id.localeCompare(b.proposal_id))''', family)
        assert len(expected) == 19 and info['mixed'][0] not in [m['proposal_id'] for m in expected]
        keys.reach(lesson)
        # Caps Lock's uppercase key without Shift is ignored.
        page.dispatch_event('[data-decision="reject_lesson"]', 'keydown', {'key': 'R', 'shiftKey': False})
        assert not posts()
        with sqlite3.connect('file:' + info['db'] + '?mode=ro', uri=True) as db:
            assert list(db.iterdump()) == info['snapshot']
        # The real server must reject an obsolete authorization revision.
        def stale_command(route):
            body = route.request.post_data_json
            body['members'][0]['revision'] = '0' * 64
            response = route.fetch(post_data=json.dumps(body))
            assert response.status == 409
            report['stale_response'] = response.json()
            route.fulfill(response=response)
        page.route('**/api/commands', stale_command)
        with page.expect_response(lambda r: r.url.endswith('/api/commands') and r.request.method == 'POST'):
            page.keyboard.press('Shift+R')
        expect(page.locator('#global-error')).to_contain_text('changed. Reload before rejecting')
        expect(lesson).to_be_enabled()
        assert len(posts()) == 1
        capture('command-stale')
        page.unroute('**/api/commands', stale_command)
        with sqlite3.connect('file:' + info['db'] + '?mode=ro', uri=True) as db:
            assert db.execute('SELECT COUNT(*) FROM commands').fetchone()[0] == 0
        # Native Shift+R submits exactly one command; repeats during its pending
        # response do not submit another. O remains read-only navigation.
        pending = []
        def hold_command(route):
            pending.append(route)
        page.route('**/api/commands', hold_command)
        keys.reach(lesson)
        with page.expect_request(lambda r: r.url.endswith('/api/commands') and r.method == 'POST'):
            page.keyboard.press('Shift+R')
        expect(lesson).to_be_disabled()
        page.keyboard.down('Shift')
        page.keyboard.down('R')
        page.keyboard.down('R')
        page.keyboard.up('R')
        page.keyboard.up('Shift')
        assert len(pending) == 1 and len(posts()) == 2
        capture('command-pending')
        page.keyboard.press('o')
        expect(page).to_have_url(full)
        expect(lesson).to_be_disabled()
        pending[0].continue_()
        page.unroute('**/api/commands', hold_command)
        expect(page.locator('[data-review-empty]')).to_contain_text('no longer in Review')
        page.keyboard.press('Escape')
        expect(page.locator('.review-card--selected')).to_have_attribute('data-learning-id', info['families']['conflict'])
        expect(page.locator('#review-count')).to_have_text('2')
        assert_review_card_focus(page)
        expect(page.locator('#global-error')).to_be_hidden()
        capture('lesson-rejected')
        page.reload()
        expect(page.locator('#review-count')).to_have_text('2')
        expect(page.locator('.review-card--selected')).to_have_attribute('data-learning-id', info['families']['conflict'])
        with sqlite3.connect('file:' + info['db'] + '?mode=ro', uri=True) as db:
            command = db.execute('SELECT id,action,state,payload_json,result_json,max_model_calls FROM commands').fetchall()
            assert len(command) == 1
            cid, action, state, payload, result, calls = command[0]
            assert (action, state, calls) == ('reject_lesson', 'completed', 0)
            assert json.loads(payload)['members'] == expected
            scopes = db.execute('SELECT proposal_id,decision_scope FROM rejection_members WHERE command_id=? ORDER BY proposal_id', (cid,)).fetchall()
            assert scopes == [(m['proposal_id'], 'lesson') for m in expected]
            assert set(json.loads(result)['suppressed_proposal_ids']) == set(info['mixed'])
            assert db.execute('SELECT status FROM learnings WHERE id=?', (family,)).fetchone()[0] == 'rejected'
            assert db.execute('SELECT COUNT(*) FROM llm_calls').fetchone()[0] == 0
            assert db.execute('SELECT COUNT(*) FROM command_targets').fetchone()[0] == 0
        assert all(Path(p).read_text() == value for p, value in info['targets'].items())
        assert len(posts()) == 2 and all(r['url'].endswith('/api/commands') for r in posts())
        assert not report['errors'], report['errors']
        visual.assert_clean()
        report.update(status='succeeded', reviewed_members=expected, suppressed=len(info['mixed']), commands=1,
                      stale_commands_refused=1, targets='unchanged', model_calls=0)
        print('V2_SHORTCUTS_OK navigation; guarded lesson rejection; 19 exact members; 20 suppressed; one command; zero models')
    except BaseException:
        report['status'] = 'failed'
        page.screenshot(path=str(OUT / 'failure.png'))
        (OUT / 'failure.html').write_text(page.content())
        raise
    finally:
        (OUT / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
        browser.close()
