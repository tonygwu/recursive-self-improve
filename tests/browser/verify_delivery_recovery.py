"""Native Review controls through delivery recovery and a reviewed inverse resolution."""
from pathlib import Path
import json
import sqlite3
import subprocess
import sys

from playwright.sync_api import sync_playwright, expect
from keyboard_actions import KeyboardActions
from visual_checks import VisualChecks

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'reports/dashboard-parity/delivery-recovery'
info = json.loads((OUT / 'manifest.json').read_text())
root = Path(info['root']).resolve()
assert root.is_dir() and root.name.startswith('si-delivery-recovery-')
assert Path(info['db']).is_relative_to(root)
assert all(Path(p).is_relative_to(root) for p in info['targets'].values())
commands = info['commands']
errors, requests, posts, workers = [], [], [], []


def rows(sql, args=()):
    with sqlite3.connect('file:' + info['db'] + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(sql, args)]


def worker(mode):
    result = subprocess.run([sys.executable, str(ROOT / 'tests/browser/delivery_recovery_demo.py'), mode],
                            capture_output=True, text=True, timeout=40)
    workers.append({'mode':mode, 'stdout':result.stdout, 'stderr':result.stderr, 'returncode':result.returncode})
    assert result.returncode == 0, workers[-1]
    return result.stdout


with sync_playwright() as runtime:
    browser = runtime.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width':1280, 'height':1024})
    keys = KeyboardActions(page, OUT / 'keyboard.json')
    visual = VisualChecks(page, OUT / 'contrast.json')
    page.on('pageerror', lambda e: errors.append(str(e)))

    def record(request):
        requests.append([request.method, request.url])
        if request.method == 'POST':
            posts.append(request.post_data_json)

    page.on('request', record)

    def card(name):
        return page.locator('#delivery-command-' + commands[name])

    def inspect(name):
        item = card(name)
        disclosure = item.locator(':scope > details')
        if not disclosure.evaluate('el=>el.open'):
            keys.activate(disclosure.locator(':scope > summary'))
        keys.activate(item.get_by_role('button', name='View reviewed edits and control history', exact=True))
        expect(item.locator('h4').first).to_be_visible()
        return item

    def capture(name, target):
        target.scroll_into_view_if_needed()
        page.screenshot(path=str(OUT / (name + '.png')))
        (OUT / (name + '.html')).write_text(page.content())

    try:
        page.goto('http://127.0.0.1:8876/#/review')
        page.wait_for_load_state('networkidle')
        capture('before', page.locator('#review-delivery'))
        print('RECON DELIVERY', page.locator('.delivery-command').all_inner_texts(), flush=True)
        for state, label in {'queued':'Queued', 'running':'Delivering', 'blocked':'Needs attention',
                             'failed':'Failed', 'cancelled':'Cancelled', 'completed':'Completed'}.items():
            expect(card(state).locator('header')).to_contain_text(label)
            item = inspect(state)
            assert any('diff' in e.get_attribute('class') for e in item.locator('pre').all())
        expect(card('failed')).to_contain_text('1 of 2 targets delivered.')
        expect(card('failed')).to_contain_text('Invented temporary disk failure')
        visual.themes(keys, 'six-states', OUT, target=card('failed'))
        with sqlite3.connect('file:' + info['db'] + '?mode=ro', uri=True) as db:
            assert list(db.iterdump()) == info['before']
        assert all(Path(p).read_text() == text for p,text in info['initial_targets'].items())
        assert not posts and not rows('SELECT id FROM llm_calls')

        keys.activate(card('failed').get_by_role('button', name='Retry unfinished', exact=True))
        expect(card('failed').locator('header')).to_contain_text('Queued')
        for name in ('queued', 'running'):
            keys.activate(card(name).get_by_role('button', name='Cancel remaining', exact=True))
            expect(card(name).locator('header')).to_contain_text('Cancellation requested')
        assert all(Path(p).read_text() == text for p,text in info['initial_targets'].items())
        assert 'FIXTURE_DELIVERY_SETTLED' in worker('--delivery')
        page.reload(); page.wait_for_load_state('networkidle')
        expect(card('failed').locator('header')).to_contain_text('Completed')
        for name in ('queued', 'running', 'cancelled'):
            expect(card(name).locator('header')).to_contain_text('Cancelled')
            assert Path(info['targets'][name]).read_text() == 'before\n'
        expect(card('blocked').locator('header')).to_contain_text('Needs attention')
        assert Path(info['targets']['blocked']).read_text() == 'Later human change.\n'
        assert all(Path(info['targets'][name]).read_text() == 'before\nafter\n' for name in ('partial-a','partial-b'))
        for pid in info['partial_proposals']:
            assert len(rows("SELECT id FROM proposal_events WHERE proposal_id=? AND event='applied'", (pid,))) == 1
        recovered = inspect('failed')
        expect(recovered).to_contain_text('Retry requested')
        expect(recovered).to_contain_text('Invented temporary disk failure')
        capture('partial-recovered', recovered)

        delivered = inspect('completed')
        keys.activate(delivered.get_by_role('link', name='Review rollback · ' + info['original_proposal'], exact=True))
        expect(page.locator('#rollback-body')).to_contain_text('RollbackConflict')
        keys.activate(page.get_by_text('Recorded application and current content', exact=True))
        expect(page.locator('#rollback-body')).to_contain_text(info['current'])
        visual.themes(keys, 'inverse-conflict', OUT, target=page.locator('#rollback-body'))
        keys.activate(page.get_by_role('button', name='Preview a resolution proposal…', exact=True))
        expect(page.locator('#eval-job-body')).to_contain_text('At most 1 model call.')
        keys.activate(page.get_by_role('button', name='Generate resolution proposal · up to 1 call', exact=True))
        expect(page.locator('#eval-job-body')).to_contain_text('Job recorded.')
        assert not rows('SELECT id FROM llm_calls')
        assert Path(info['targets']['conflict']).read_text() == info['current']
        output = worker('--models')
        generated = json.loads(output.split('FIXTURE_RESOLUTION_GENERATED ',1)[1])
        assert generated['synthetic_calls'] == 1
        assert Path(info['targets']['conflict']).read_text() == info['current']
        page.reload(); page.wait_for_load_state('networkidle')
        resolution = page.locator('#delivery-command-' + generated['command_id'])
        expect(resolution).to_contain_text('1 of at most 1 calls consumed.')
        keys.activate(resolution.get_by_role('link', name='Review the generated resolution', exact=True))
        approve = page.locator('[data-decision="approve"]')
        expect(approve).to_be_enabled()
        keys.activate(page.locator('.review-selected-preview .review-targets > summary'))
        keys.activate(page.get_by_text('Complete combined edit', exact=True))
        expect(page.locator('.review-selected-preview')).to_contain_text('- Rule A edited by a human.')
        capture('resolution-review', page.locator('.review-card--open'))
        keys.activate(approve)
        expect(page.locator('#review-selected-proposal')).to_contain_text('Recorded state:')
        assert Path(info['targets']['conflict']).read_text() == info['current']
        assert 'FIXTURE_DELIVERY_SETTLED' in worker('--delivery')
        assert Path(info['targets']['conflict']).read_text() == info['resolved']
        assert len(rows("SELECT id FROM proposal_events WHERE proposal_id=? AND event='rolled_back'", (info['original_proposal'],))) == 1
        page.goto('http://127.0.0.1:8876/#/review/rollback/' + info['original_proposal'])
        page.wait_for_load_state('networkidle')
        expect(page.locator('#rollback-body')).to_contain_text('AlreadyRolledBack')
        delivery_id = rows('SELECT command_id FROM command_members WHERE proposal_id=?', (generated['proposal_id'],))[0]['command_id']
        keys.activate(page.locator('#review-delivery [data-delivery-refresh]'))
        final_delivery = page.locator('#delivery-command-' + delivery_id)
        expect(final_delivery.locator('header')).to_contain_text('Completed')
        expect(final_delivery).to_contain_text('1 of 1 target delivered.')
        capture('resolution-completed', page.locator('#rollback-body'))
        assert all(event in rows('SELECT * FROM proposal_events') for event in info['initial_events'])
        assert len(rows('SELECT id FROM llm_calls')) == 1
        assert [p['action'] for p in posts] == ['retry_delivery','cancel_delivery','cancel_delivery','resolve_rollback','approve']
        assert all(url.startswith('http://127.0.0.1:8876/') for method,url in requests)
        assert not errors, errors
        visual.assert_clean()
        print('DELIVERY_RECOVERY_BROWSER_OK: six actual states; partial retry without duplicate writes; cancellation after preparation; complete conflict and one synthetic-call resolution; exact approval before separate delivery; preserved later rule/human text; keyboard, reload, themes/widths; zero external models')
    except Exception:
        capture('failure', page.locator('#main'))
        raise
    finally:
        keys.save()
        (OUT / 'result.json').write_text(json.dumps({'errors':errors,'requests':requests,'posts':posts,'workers':workers}, indent=2))
        browser.close()
