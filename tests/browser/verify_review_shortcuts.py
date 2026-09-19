"""Native Review shortcuts on the ordinary queue, using the Overview fixture."""
from pathlib import Path
import json
import sqlite3

from playwright.sync_api import sync_playwright, expect
from keyboard_actions import KeyboardActions
from review_focus import assert_review_card_focus

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'reports/dashboard-parity/review-shortcuts'
OUT.mkdir(parents=True, exist_ok=True)
manifest = json.loads((ROOT / 'reports/dashboard-parity/overview-manifest.json').read_text())
assert 'si-overview-ui-' in manifest['db']

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width': 1280, 'height': 1024})
    keys = KeyboardActions(page, OUT / 'keys.json')
    errors, requests, outcomes = [], [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('request', lambda request: requests.append((request.method, request.url)))
    card = page.locator('.review-card--selected')

    def selected():
        return card.get_attribute('data-learning-id')

    def ready():
        page.locator('[data-decision="approve"]:not([disabled])').wait_for()
        page.wait_for_load_state('networkidle')

    def order():
        # Observe the rendered successor list; no state or focus mutation.
        return [selected()] + page.locator('[data-open-family]').evaluate_all(
            '(rows)=>rows.map(row=>row.dataset.openFamily)')

    def focused():
        assert_review_card_focus(page)

    def capture(label):
        page.screenshot(path=str(OUT / f'{label}.png'))
        (OUT / f'{label}.html').write_text(page.content())

    def activation_state():
        return page.evaluate('''async() => {
            const {state}=await import('/app.js'), active=document.activeElement;
            return {url:location.href, route:state.route, selected:state.selectedFamily,
                deciding:state.deciding, request:state.commandRequests[state.selectedFamily],
                preview:state.reviewPreviews[state.selectedFamily],
                active:{tag:active.tagName,id:active.id,text:active.textContent.slice(0,100)},
                events:window.__reviewActivationTrace || [],
                error:document.getElementById('global-error').textContent};
        }''')

    def decide(key, expected):
        before = selected()
        page.keyboard.press(key)
        if expected:
            expect(card).to_have_attribute('data-learning-id', expected)
            ready()
            focused()
        else:
            page.locator('[data-review-empty]').wait_for()
            page.wait_for_load_state('networkidle')
            expect(page.locator('[data-review-empty]')).to_be_focused()
            expect(page.locator('#review-count')).to_have_text('0')
            expect(page.locator('#review-body')).to_contain_text('Nothing needs you')
        outcomes.append({'key': key, 'before': before, 'after': expected})

    try:
        page.goto('http://127.0.0.1:8876/#/review')
        ready(); capture('before')
        keys.activate(page.locator('#skip-to-main'))
        initial = order()
        assert len(initial) == 8
        page.keyboard.press('j'); expect(card).to_have_attribute('data-learning-id', initial[1]); ready(); focused()
        page.keyboard.press('k'); expect(card).to_have_attribute('data-learning-id', initial[0]); ready(); focused()
        decide('r', initial[1]); capture('after-first-reject')
        # Deciding a non-first card advances to its successor, not the first row.
        page.keyboard.press('j'); expect(card).to_have_attribute('data-learning-id', initial[2]); ready(); focused()
        decide('a', initial[3])
        # A refused command keeps the same card and the visible error.
        def stale(route):
            route.fulfill(status=409, json={'detail': 'Fixture stale revision; refresh before deciding'})
        page.route('**/api/commands', stale)
        held_refresh = []
        def hold_refresh(route):
            held_refresh.append(route)
        page.route('**/api/review-queue', hold_refresh)
        with page.expect_request('**/api/review-queue'):
            page.keyboard.press('r')
        expect(page.locator('#global-error')).to_contain_text('Fixture stale revision')
        expect(card.get_by_role('status')).to_contain_text('Processing this decision')
        for action in ('approve', 'reject', 'reject_lesson'):
            expect(page.locator(f'[data-decision="{action}"]')).to_be_disabled()
        focused(); capture('pending-refusal-refresh')
        assert len(held_refresh) == 1
        page.unroute('**/api/review-queue', hold_refresh)
        held_refresh[0].continue_()
        ready(); expect(card).to_have_attribute('data-learning-id', initial[3]); focused()
        page.unroute('**/api/commands', stale)
        capture('refused')
        # Repainting a selected card preserves its focused action button.
        reject = page.locator('[data-decision="reject"]')
        keys.reach(reject)
        page.evaluate('async()=>(await import("/app.js")).paintReview()')
        expect(reject).to_be_focused()
        # Background repaints must not pull focus away from another control.
        theme = page.locator('#theme-toggle')
        keys.reach(theme)
        page.evaluate('async()=>(await import("/app.js")).paintReview()')
        expect(theme).to_be_focused()
        # A late command response must not pull focus away from a new route.
        def navigate_before_reply(route):
            response = route.fetch()
            page.goto('http://127.0.0.1:8876/#/overview')
            page.locator('#view-overview').wait_for()
            keys.activate(page.locator('#skip-to-main'))
            route.fulfill(response=response)
        page.route('**/api/commands', navigate_before_reply)
        page.evaluate('''() => {
            window.__reviewActivationTrace=[];
            for(const type of ['keydown','keyup','click']) document.addEventListener(type,event=>{
                window.__reviewActivationTrace.push({type,key:event.key,tag:event.target.tagName,
                    decision:event.target.getAttribute('data-decision'),prevented:event.defaultPrevented});
                window.__reviewActivationTrace=window.__reviewActivationTrace.slice(-30);
            });
        }''')
        (OUT / 'activation-before.json').write_text(json.dumps(activation_state(), indent=2) + '\n')
        keys.activate(reject)
        (OUT / 'activation-after.json').write_text(json.dumps(activation_state(), indent=2) + '\n')
        page.wait_for_url('**/#/overview')
        page.wait_for_load_state('networkidle')
        assert page.url.endswith('#/overview')
        expect(page.locator('#main')).to_be_focused()
        page.unroute('**/api/commands', navigate_before_reply)
        page.goto('http://127.0.0.1:8876/#/review'); ready()
        # Consume every remaining family using the actual command endpoint.
        keys.activate(page.locator('#skip-to-main'))
        page.keyboard.press('k'); ready(); focused()
        while card.count():
            remaining = order()
            decide('r', remaining[1] if len(remaining) > 1 else None)
        capture('empty')
        assert all(Path(path).read_text() == value for path, value in manifest['targets'].items())
        with sqlite3.connect('file:' + manifest['db'] + '?mode=ro', uri=True) as db:
            assert db.execute('SELECT COUNT(*) FROM llm_calls').fetchone()[0] == 0
            commands = db.execute('SELECT action,state FROM commands ORDER BY created_at,id').fetchall()
            assert len(commands) == 8
            assert sum(action == 'approve' for action, _ in commands) == 1
            assert sum(action == 'reject_target' and state == 'completed' for action, state in commands) == 7
        assert not errors, errors
        report = {'status': 'succeeded', 'outcomes': outcomes, 'commands': commands,
                  'page_errors': errors, 'requests': len(requests), 'targets_unchanged': True, 'model_calls': 0}
        (OUT / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
        print('REVIEW_SHORTCUTS_OK', json.dumps(report), flush=True)
    except Exception:
        capture('failure')
        (OUT / 'failure.json').write_text(json.dumps({
            'state':activation_state(), 'errors':errors, 'requests':requests,
            'outcomes':outcomes}, indent=2) + '\n')
        raise
    finally:
        browser.close()
