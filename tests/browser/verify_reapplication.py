"""Native reapplication request, fresh approval, one explicit fixture delivery."""
from pathlib import Path
import json
import sqlite3
import subprocess
import sys

from playwright.sync_api import sync_playwright
from keyboard_actions import KeyboardActions
from visual_checks import VisualChecks

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT/'reports/dashboard-parity/reapplication'
manifest = json.loads((OUT/'manifest.json').read_text())
root = Path(manifest['root']).resolve()
assert root.is_dir() and root.name.startswith('si-reapplication-')
assert all(Path(manifest[key]).resolve().is_relative_to(root) for key in ('db', 'target'))
target = Path(manifest['target'])
base = 'http://127.0.0.1:8876/'
errors, requests, posts = [], [], []


def rows(sql, args=()):
    with sqlite3.connect('file:'+manifest['db']+'?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        return [dict(r) for r in db.execute(sql, args)]


def dump():
    with sqlite3.connect('file:'+manifest['db']+'?mode=ro', uri=True) as db:
        return list(db.iterdump())


def commands():
    return 'async()=>(await fetch("/api/commands").then(r=>r.json())).commands.length'


with sync_playwright() as runtime:
    browser = runtime.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width': 1280, 'height': 1024})
    keys = KeyboardActions(page, OUT/'keyboard.json')
    visual = VisualChecks(page, OUT/'contrast.json')
    page.on('pageerror', lambda error: errors.append(str(error)))

    def record(request):
        requests.append([request.method, request.url])
        if request.method == 'POST':
            posts.append(request.post_data_json)
    page.on('request', record)
    try:
        # 1. The reapplication preview is read-only.
        page.goto(base + '#/review/reapply/' + manifest['proposal'])
        page.wait_for_load_state('networkidle')
        submit = page.locator('#eval-job-submit')
        submit.wait_for(state='visible')
        assert submit.inner_text().strip() == 'Create reapplication proposal'
        patch = page.locator('details', has=page.get_by_text('Complete reapplication patch', exact=True))
        keys.activate(patch.locator('summary'))
        assert '+- Rule A.' in patch.locator('pre').inner_text()
        for theme in ('light', 'dark'):
            keys.theme(theme)
            page.locator('#main').evaluate('el=>el.scrollTop=0')
            visual.check('reapplication-preview')
            page.screenshot(path=str(OUT/f'preview-{theme}.png'))
        assert dump() == manifest['database'], 'browsing the preview changed the Store'
        assert target.read_text() == manifest['current']
        assert all(method == 'GET' for method, _ in requests)

        # 2. The request records a manual-review proposal and writes nothing.
        keys.activate(submit)
        page.wait_for_function(commands() + '===1')
        link = page.get_by_role('link', name='Review the reapplication')
        link.first.wait_for(state='visible')
        assert len(posts) == 1 and posts[0]['action'] == 'request_reapplication', posts
        assert len(posts[0]['preview_revision']) == 64
        drafts = rows("SELECT * FROM proposals WHERE action='reapply'")
        assert len(drafts) == 1 and drafts[0]['status'] == 'pending', drafts
        draft = drafts[0]['id']
        assert target.read_text() == manifest['current']

        # 3. Fresh approval of the new proposal through Review.
        keys.activate(link.first)
        page.wait_for_function('location.hash.includes(%s)' % json.dumps(draft))
        approve = page.locator('[data-decision="approve"]')
        approve.wait_for(state='visible')
        page.wait_for_function('!document.querySelector("[data-decision=approve]").disabled')
        body = page.locator('#review-body').inner_text()
        assert 'Reapplication' in body, body[:400]
        keys.activate(approve)
        page.wait_for_function(commands() + '===2')
        page.wait_for_load_state('networkidle')
        assert len(posts) == 2 and posts[1]['action'] == 'approve', posts
        assert [m['proposal_id'] for m in posts[1]['members']] == [draft]
        assert target.read_text() == manifest['current'], 'the web process wrote the target'

        # 4. One explicit fixture worker pass delivers it.
        call = subprocess.run([sys.executable, str(ROOT/'tests/browser/reapplication_demo.py'), '--once'],
                              capture_output=True, text=True, timeout=40)
        assert call.returncode == 0, call.stdout + call.stderr
        operation = json.loads(call.stdout.split('REAPPLICATION_WORKER ', 1)[1])
        assert operation['state'] == 'completed', operation
        assert target.read_text() == manifest['reapplied']
        page.reload(); page.wait_for_load_state('networkidle')
        page.wait_for_function('async()=>(await fetch("/api/review-queue").then(r=>r.json())).count===0')
        for theme in ('light', 'dark'):
            keys.theme(theme)
            page.locator('#main').evaluate('el=>el.scrollTop=0')
            page.screenshot(path=str(OUT/f'delivered-{theme}.png'))
        status = {r['id']: r['status'] for r in rows('SELECT id, status FROM proposals')}
        assert status == {manifest['proposal']: 'rolled_back', draft: 'applied'}, status
        events = rows('SELECT * FROM proposal_events')
        assert all(event in events for event in manifest['events']), 'an earlier event changed'
        assert not rows('SELECT id FROM llm_calls')
        assert not errors, errors
        assert all(url.startswith(base) for _, url in requests)
        report = {'requests': len(requests), 'posts': [p['action'] for p in posts], 'page_errors': errors,
                  'model_calls': 0, 'preview_read_only': True, 'explicit_fixture_writes': 1,
                  'keyboard_steps': len(keys.steps), 'final_status': status}
        (OUT/'result.json').write_text(json.dumps(report, indent=2))
        visual.assert_clean()
        print('REAPPLICATION_UI_OK', json.dumps(report))
    except Exception:
        page.screenshot(path=str(OUT/'failure.png'), full_page=True)
        (OUT/'failure.html').write_text(page.content())
        raise
    finally:
        keys.save()
        browser.close()
