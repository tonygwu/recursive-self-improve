"""Native browser controls through bounded job failure, restart and manual Review."""
from pathlib import Path
from urllib.parse import quote
import json
import sqlite3
import subprocess
import sys

from playwright.sync_api import sync_playwright, expect
from keyboard_actions import KeyboardActions
from visual_checks import VisualChecks

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'reports/dashboard-parity/model-jobs'
info = json.loads((OUT / 'manifest.json').read_text())
root = Path(info['root']).resolve()
assert root.is_dir() and root.name.startswith('si-model-jobs-')
assert Path(info['db']).is_relative_to(root)
assert all(Path(p).is_relative_to(root) for p in info['targets'])
errors, requests, posts, workers, failed_responses = [], [], [], [], []


def rows(sql, args=()):
    with sqlite3.connect('file:' + info['db'] + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        return [dict(r) for r in db.execute(sql, args)]


def intact():
    assert all(Path(p).read_text()==text for p,text in info['targets'].items())
    assert not (root / 'state/snapshots').exists()
    assert not rows('SELECT id FROM command_members'), 'No instruction approval is authorized'


def worker(mode):
    result = subprocess.run([sys.executable, str(ROOT / 'tests/browser/model_jobs_demo.py'), mode],
                            capture_output=True, text=True, timeout=60)
    workers.append({'mode':mode, 'stdout':result.stdout, 'stderr':result.stderr, 'returncode':result.returncode})
    assert result.returncode == 0, workers[-1]
    intact()
    return json.loads(result.stdout.split('FIXTURE_MODEL_JOB ',1)[1])


with sync_playwright() as runtime:
    browser = runtime.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width':1280,'height':1024})
    keys = KeyboardActions(page, OUT / 'keyboard.json')
    visual = VisualChecks(page, OUT / 'contrast.json')
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.on('response', lambda r: failed_responses.append([r.status,r.url]) if r.status>=400 else None)

    def record(request):
        requests.append([request.method,request.url])
        if request.method == 'POST': posts.append(request.post_data_json)

    page.on('request', record)

    def capture(name, target):
        target.scroll_into_view_if_needed()
        page.screenshot(path=str(OUT / (name + '.png')))
        (OUT / (name + '.html')).write_text(page.content())

    def card(cid):
        return page.locator('#delivery-command-' + cid)

    def reload():
        before = len(rows('SELECT id FROM llm_calls'))
        commands = rows('SELECT id FROM commands ORDER BY id')
        page.reload(); page.wait_for_load_state('networkidle')
        expect(page.locator('#review-delivery')).to_be_visible()
        assert len(rows('SELECT id FROM llm_calls')) == before
        assert rows('SELECT id FROM commands ORDER BY id') == commands
        intact()

    def inspect(cid):
        item = card(cid); details = item.locator(':scope > details')
        if not details.evaluate('el=>el.open'):
            keys.activate(details.locator(':scope > summary'))
        keys.activate(item.get_by_role('button', name='Load job records', exact=True))
        expect(item.locator('pre').last).to_be_visible()
        return item

    def submit(action):
        before = {r['id'] for r in rows('SELECT id FROM commands')}
        keys.reach(page.locator('#eval-job-submit'))
        page.keyboard.press('Enter'); page.keyboard.press('Enter')
        expect(page.locator('#eval-job-body')).to_contain_text('job recorded' if action!='regenerate_eval' else 'Job recorded')
        after = rows('SELECT id,action FROM commands')
        new = [r for r in after if r['id'] not in before]
        assert len(new)==1 and new[0]['action']==action, new
        cid = new[0]['id']
        expect(card(cid).locator('header')).to_contain_text('Queued')
        intact()
        return cid

    def review_generated(cid, pid, link, expected):
        item = card(cid)
        expect(item.locator('header')).to_contain_text('Completed')
        expect(item).to_contain_text('1 of at most 1 calls consumed.')
        keys.activate(item.get_by_role('link', name=link, exact=True))
        expect(page.locator('.review-selected-preview')).to_contain_text(expected)
        expect(page.locator('.review-selected-preview > h4')).to_have_text('1 selected proposal · 1 destination')
        expect(page.locator('.review-selected-preview .review-targets > summary')).to_have_count(1)
        expect(page.locator('[data-decision="approve"]')).to_be_enabled()
        keys.activate(page.locator('.review-selected-preview .review-targets > summary'))
        keys.activate(page.get_by_text('Complete combined edit', exact=True))
        expect(page.locator('.review-selected-preview')).to_contain_text(expected)
        assert rows('SELECT status FROM proposals WHERE id=?',(pid,))[0]['status']=='pending'
        intact()

    try:
        page.goto('http://127.0.0.1:8876/#/review/eval/' + info['proposal_id'])
        page.wait_for_load_state('networkidle')
        capture('before', page.locator('#review-eval'))
        print('RECON MODEL JOB', page.locator('#review-eval').inner_text(), flush=True)
        expect(page.locator('#eval-job-body')).to_contain_text('At most 21 model calls. 3 scenario-generation calls and 18 trial calls.')
        expect(page.locator('#eval-job-body')).to_contain_text(info['proposal_id'])
        keys.activate(page.get_by_text('Exact source and existing evaluation', exact=True))
        expect(page.locator('#eval-job-body')).to_contain_text('- new rule from miner')
        visual.themes(keys, 'cost-source', OUT, target=page.locator('#review-eval'))
        with sqlite3.connect('file:' + info['db'] + '?mode=ro', uri=True) as db:
            assert list(db.iterdump())==info['before']
        assert not posts and not rows('SELECT id FROM llm_calls')

        cid = submit('regenerate_eval')
        reload()
        expect(page.locator('#eval-job-body')).to_contain_text('Job recorded')
        expect(page.locator('#eval-job-submit')).to_have_count(0)
        failed = worker('--fail')['command']
        assert failed['id']==cid and failed['state']=='failed'
        assert failed['budget']['remaining']=={'cheap':0,'strong':0,'gate':21}
        reload()
        expect(card(cid)).to_contain_text('Invented runner initialization failure')
        expect(card(cid).locator('header')).to_contain_text('Failed')
        keys.activate(card(cid).get_by_role('button', name='Resume reserved work', exact=True))
        expect(card(cid).locator('header')).to_contain_text('Queued')
        stopped = worker('--interrupt')
        assert stopped['command']['run_id']==failed['run_id']
        assert stopped['command']['budget']['remaining']['gate']==20
        reload()
        expect(card(cid).locator('header')).to_contain_text('Running')
        expect(card(cid)).to_contain_text('1 of at most 21 calls consumed.')
        visual.themes(keys, 'interrupted-checkpoint', OUT, target=card(cid))
        completed = worker('--complete')
        assert completed['command']['id']==cid and completed['command']['run_id']==failed['run_id']
        assert len(completed['synthetic_calls'])==20
        assert completed['command']['budget']['consumed']=={'cheap':0,'strong':0,'gate':21}
        assert completed['command']['result']['verdict']=='gated_pass'
        assert len(rows('SELECT id FROM runs'))==1
        assert len(rows('SELECT id FROM job_evaluations'))==3
        original_history = rows('SELECT * FROM proposal_eval_history')
        assert len(original_history)==1
        all_calls = stopped['synthetic_calls'] + completed['synthetic_calls']
        assert len({c['id'] for c in all_calls})==21
        reload()
        expect(card(cid)).to_contain_text('21 of at most 21 calls consumed.')
        expect(card(cid)).to_contain_text('Evaluation: gated_pass')
        expect(inspect(cid)).to_contain_text('Invented runner initialization failure')
        capture('completed-retained-failure', card(cid))

        keys.activate(card(cid).get_by_role('button', name='Preview a new attempt…', exact=True))
        expect(page.locator('#eval-job-submit')).to_be_enabled()
        cancelled = submit('regenerate_eval')
        keys.activate(card(cancelled).get_by_role('button', name='Cancel remaining work', exact=True))
        expect(card(cancelled).locator('header')).to_contain_text('Cancelled')
        assert not rows('SELECT id FROM job_calls WHERE command_id=?',(cancelled,))

        keys.activate(page.get_by_role('link', name='Preview mining this incident…', exact=True))
        expect(page.locator('#eval-job-body')).to_contain_text('Maximum: 1 logical model call.')
        expect(page.locator('#eval-job-body')).to_contain_text(info['incident_id'])
        keys.activate(page.get_by_role('button', name='Inspect complete mining input', exact=True))
        expect(page.locator('#eval-job-body')).to_contain_text('Complete frozen mining input')
        mining_id = submit('mine_incident')
        mined = worker('--complete')
        assert mined['command']['id']==mining_id and len(mined['synthetic_calls'])==1
        assert mined['command']['budget']['consumed']['gate']==0
        mined_proposals = mined['command']['result']['proposal_ids']
        assert len(mined_proposals)==1
        reload()
        review_generated(mining_id, mined_proposals[0], 'Review the generated change', 'Never trust exit 0 alone')
        capture('mined-manual-review', page.locator('.review-selected-preview'))

        for mode,expected in [('hook','forbidden'),('correct_target','Inspect the original request identifier before retrying.')]:
            selection={'learning_id':info['learning_id'],'mode':mode,'target_id':info['recovery_targets'][mode],'proposal_ids':[]}
            page.goto('http://127.0.0.1:8876/#/review/recovery/'+quote(json.dumps(selection),safe=''))
            page.wait_for_load_state('networkidle')
            expect(page.locator('#eval-job-body')).to_contain_text('Maximum: 1 logical model call.')
            keys.activate(page.get_by_text('Complete evidence, selected proposals, and current target', exact=True))
            expect(page.locator('#eval-job-body')).to_contain_text(info['learning_id'])
            recovery_id = submit('propose_recovery')
            result = worker('--complete')
            assert result['command']['id']==recovery_id and len(result['synthetic_calls'])==1
            assert result['command']['budget']['consumed']['gate']==0
            pid = result['command']['result']['proposal_id']
            reload()
            expect(page.locator('#eval-job-submit')).to_have_count(0)
            review_generated(recovery_id, pid, 'Review the recovery proposal', expected)
            visual.themes(keys, mode+'-manual-review', OUT, target=page.locator('.review-selected-preview'))

        assert len(rows('SELECT id FROM commands'))==5
        assert len(rows('SELECT id FROM llm_calls'))==24
        assert rows('SELECT * FROM proposal_eval_history')==original_history
        assert [p['action'] for p in posts]==['regenerate_eval','resume_job','regenerate_eval','cancel_job','mine_incident','propose_recovery','propose_recovery']
        assert all(url.startswith('http://127.0.0.1:8876/') for method,url in requests)
        assert not errors and not failed_responses, (errors,failed_responses)
        intact(); visual.assert_clean()
        print('MODEL_JOBS_BROWSER_OK: one 21-call gate across failure/resume/restart; queued cancellation uses zero; mining/hook/corrected-target each use one synthetic mining call and return to manual Review; reload and duplicate activation preserve reservations; keyboard/themes/widths; retained prior evidence; no target writes or external models')
    except Exception:
        capture('failure', page.locator('#main'))
        raise
    finally:
        keys.save()
        (OUT / 'result.json').write_text(json.dumps({'errors':errors,'failed_responses':failed_responses,'requests':requests,'posts':posts,'workers':workers},indent=2))
        browser.close()
