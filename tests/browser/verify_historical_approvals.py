"""Keyboard fresh review, explicit fixture delivery, and durable history."""
from pathlib import Path
import json
import sqlite3
import subprocess
import sys

from playwright.sync_api import sync_playwright
from keyboard_actions import KeyboardActions
from visual_checks import VisualChecks

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'reports/dashboard-parity/historical-approvals'
manifest = json.loads((OUT / 'manifest.json').read_text())
root = Path(manifest['root']).resolve()
assert root.name.startswith('si-historical-ui-')
base = 'http://127.0.0.1:8876/'
errors, requests, posts = [], [], []


def read_rows(sql):
    with sqlite3.connect('file:' + manifest['db'] + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(sql)]


with sync_playwright() as runtime:
    browser = runtime.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width': 1280, 'height': 1024})
    keys = KeyboardActions(page, OUT / 'keyboard.json')
    visual=VisualChecks(page,OUT/'review-contrast.json')
    page.on('pageerror', lambda error: errors.append(str(error)))
    def record(request):
        requests.append([request.method, request.url])
        if request.method == 'POST':
            posts.append(request.post_data_json)
    page.on('request', record)
    try:
        page.goto(base + '#/review/proposal/' + manifest['proposal'])
        page.wait_for_load_state('networkidle')
        page.screenshot(path=str(OUT / 'keyboard-before.png'))
        (OUT / 'keyboard-before.html').write_text(page.content())
        print('RECON J2', page.get_by_role('button').all_text_contents(), flush=True)
        approve = page.locator('[data-decision="approve"]')
        approve.wait_for(state='visible')
        page.wait_for_function('!document.querySelector("[data-decision=approve]").disabled')
        assert 'fresh approval' in page.locator('#review-body').inner_text()
        keys.activate(page.locator('.review-selected-preview .review-targets > summary'))
        complete = page.get_by_text('Complete combined edit', exact=True)
        keys.activate(complete)
        assert '+after' in page.locator('.review-selected-preview').inner_text()
        for width in (1280, 1440):
            page.set_viewport_size({'width': width, 'height': 1024})
            for theme in ('light', 'dark'):
                keys.theme(theme)
                page.locator('#main').evaluate('el=>el.scrollTop=0')
                visual.check('review')
                page.screenshot(path=str(OUT / f'review-{theme}-{width}.png'))
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        with sqlite3.connect('file:' + manifest['db'] + '?mode=ro', uri=True) as db:
            assert list(db.iterdump()) == manifest['before']
        assert (root / 'rules.md').read_text() == 'before\n'
        assert all(method == 'GET' for method, _ in requests)
        keys.activate(approve)
        page.wait_for_function('async()=>{const r=await fetch("/api/commands").then(r=>r.json());return r.commands.length===1}')
        page.locator('#review-selected-proposal').get_by_text('Recorded state:',exact=False).wait_for()
        page.wait_for_load_state('networkidle')
        focus_after_decision = page.evaluate('({tag:document.activeElement.tagName,id:document.activeElement.id})')
        assert focus_after_decision['id'] == 'review-selected-proposal', focus_after_decision
        assert len(posts) == 1 and posts[0]['action'] == 'approve'
        assert len(posts[0]['preview_revision']) == 64
        assert read_rows('SELECT state FROM commands') == [{'state': 'queued'}]
        assert (root / 'rules.md').read_text() == 'before\n'
        result = subprocess.run([sys.executable, str(ROOT / 'tests/browser/historical_approvals_demo.py'), '--worker'],
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
        assert 'FIXTURE_WORKER_COMPLETED_ONCE' in result.stdout
        page.reload(); page.wait_for_load_state('networkidle')
        page.wait_for_function('async()=>{const r=await fetch("/api/review-queue").then(r=>r.json());return r.count===0}')
        assert page.locator('[data-decision="approve"]').count() == 0
        for theme in ('light', 'dark'):
            keys.theme(theme)
            page.locator('#main').evaluate('el=>el.scrollTop=0')
            page.screenshot(path=str(OUT / f'delivered-{theme}.png'))
        assert read_rows('SELECT state FROM commands') == [{'state': 'completed'}]
        assert (root / 'rules.md').read_text() == 'before\nafter\n'
        events = read_rows('SELECT * FROM proposal_events')
        assert all(event in events for event in manifest['events'])
        assert len([event for event in events if event['event'] == 'approved_user']) == 2
        assert len([event for event in events if event['event'] == 'applied']) == 1
        assert not read_rows('SELECT id FROM llm_calls')
        assert not errors
        assert all(url.startswith(base) for _, url in requests)
        report = {'requests': len(requests), 'posts': len(posts), 'page_errors': errors,
                  'model_calls': 0, 'read_state_unchanged': True, 'explicit_fixture_writes': 1,
                  'keyboard_steps': len(keys.steps), 'focus_after_decision': focus_after_decision, 'old_events_preserved': True, 'review_count_after_delivery': 0}
        (OUT / 'result.json').write_text(json.dumps(report, indent=2))
        visual.assert_clean()
        print('HISTORICAL_APPROVAL_UI_OK', json.dumps(report))
    except Exception:
        page.screenshot(path=str(OUT / 'failure.png'), full_page=True)
        (OUT / 'failure.html').write_text(page.content())
        raise
    finally:
        keys.save()
        browser.close()
