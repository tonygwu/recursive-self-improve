"""Native ready-inverse intent, separate worker, reload and exact transport replay."""
from pathlib import Path
import json
import sqlite3
import subprocess
import sys
import tempfile

from playwright.sync_api import sync_playwright, expect
from keyboard_actions import KeyboardActions
from visual_checks import VisualChecks

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT/'reports/dashboard-parity/ordinary-rollback'
info = json.loads((OUT/'manifest.json').read_text())
root = Path(info['root']).resolve()
assert root.is_dir() and root.name.startswith('si-ordinary-rollback-')
assert all(Path(info[key]).resolve().is_relative_to(root) for key in ('db','target','snapshots'))
result = {'states':[],'requests':[],'posts':[],'workers':[],'errors':[]}


def rows(sql, args=()):
    with sqlite3.connect('file:'+info['db']+'?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row
        return [dict(r) for r in db.execute(sql,args)]


def snapshot():
    with sqlite3.connect('file:'+info['db']+'?mode=ro',uri=True) as db:
        database=list(db.iterdump())
    head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=info['snapshots'],text=True).strip()
    return {'database':database,'head':head,'target':Path(info['target']).read_bytes().decode()}


def worker():
    call=subprocess.run([sys.executable,str(ROOT/'tests/browser/ordinary_rollback_demo.py'),'--once'],
        capture_output=True,text=True,timeout=40)
    result['workers'].append({'stdout':call.stdout,'stderr':call.stderr,'returncode':call.returncode})
    assert call.returncode==0,result['workers'][-1]
    return json.loads(call.stdout.split('ORDINARY_ROLLBACK_WORKER ',1)[1])


with tempfile.TemporaryDirectory(prefix='si-ordinary-browser-') as folder, sync_playwright() as runtime:
    folder=Path(folder);extension=folder/'extension';extension.mkdir()
    (extension/'manifest.json').write_text(json.dumps({'manifest_version':3,'name':'Temporary inverse zoom',
        'version':'1.0','host_permissions':['http://127.0.0.1/*'],'background':{'service_worker':'worker.js'}}))
    (extension/'worker.js').write_text('chrome.runtime.onInstalled.addListener(() => {});')
    browser=runtime.chromium.launch_persistent_context(str(folder/'profile'),channel='chromium',headless=True,
        args=[f'--disable-extensions-except={extension}',f'--load-extension={extension}'],viewport={'width':1280,'height':1024})
    page=browser.pages[0]
    zoom=browser.service_workers[0] if browser.service_workers else browser.wait_for_event('serviceworker')
    keys=KeyboardActions(page,OUT/'keyboard.json');visual=VisualChecks(page,OUT/'contrast.json')
    previews=[]
    page.on('pageerror',lambda e:result['errors'].append(str(e)))

    def request(req):
        result['requests'].append([req.method,req.url])
        if req.method=='POST':result['posts'].append({'source':'native-keyboard','body':req.post_data_json})

    page.on('request',request)
    page.on('response',lambda r:previews.append(r.json()) if r.url.endswith('/rollback-preview') and r.status==200 else None)

    def capture(name):
        page.locator('#rollback-body').scroll_into_view_if_needed()
        page.screenshot(path=str(OUT/(name+'.png')))
        (OUT/(name+'.html')).write_text(page.content())
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        visual.check(name);result['states'].append(name)

    def layouts(label):
        def inspect_scroll(name):
            diff=page.locator('#rollback-body pre.review-card__diff')
            if not diff.count():return
            bounds=diff.evaluate('e=>({width:e.clientWidth,total:e.scrollWidth,left:e.scrollLeft})')
            if bounds['total']<=bounds['width']:return
            keys.reach(diff)
            # Native arrows must reach the far edge; no DOM scroll assignment.
            for _ in range(50):
                left=diff.evaluate('e=>e.scrollLeft')
                if left>=bounds['total']-bounds['width']-1:break
                page.keyboard.press('ArrowRight')
                page.wait_for_function('x=>document.querySelector("#rollback-body pre.review-card__diff").scrollLeft>x',arg=left)
            else:raise AssertionError('Native keys did not reach the complete inverse edge')
            result.setdefault('inverse_scroll',[]).append({'name':name,'before':bounds,'after':diff.evaluate('e=>e.scrollLeft')})
            expect(diff).to_be_focused();capture(name+'-inverse-scrolled')
        for width in (1280,1440,640):
            page.set_viewport_size({'width':width,'height':1024})
            for theme in ('light','dark'):
                keys.theme(theme);capture(f'{label}-{theme}-{width}')
                inspect_scroll(f'{label}-{theme}-{width}')
        page.set_viewport_size({'width':1440,'height':1024})
        receipt=zoom.evaluate('''async()=>{const [tab]=await chrome.tabs.query({active:true,currentWindow:true});
            await chrome.tabs.setZoom(tab.id,2);return chrome.tabs.getZoom(tab.id);}''')
        assert receipt==2
        for theme in ('light','dark'):
            keys.theme(theme);keys.reach(page.locator('#rollback-refresh'))
            capture(f'{label}-{theme}-native-200-percent')
            inspect_scroll(f'{label}-{theme}-native-200-percent')
        zoom.evaluate('''async()=>{const [tab]=await chrome.tabs.query({active:true,currentWindow:true});await chrome.tabs.setZoom(tab.id,1);}''')

    try:
        page.goto('http://127.0.0.1:8876/#/review');page.wait_for_load_state('networkidle')
        page.screenshot(path=str(OUT/'reconnaissance.png'))
        (OUT/'reconnaissance.html').write_text(page.content())
        command=page.locator('#delivery-command-'+info['commands'][0])
        expect(command.locator('header')).to_contain_text('Completed')
        keys.activate(command.locator(':scope > details > summary'))
        keys.activate(command.get_by_role('button',name='View reviewed edits and control history',exact=True))
        expect(command.locator('pre').first).to_contain_text('+\u002d Rule A.')
        keys.activate(command.get_by_role('link',name='Review rollback · '+info['proposals'][0],exact=True))
        expect(page.locator('#rollback-submit')).to_be_enabled()
        assert previews and previews[-1]['ready'],previews
        preview=previews[-1]
        expect(page.locator('#rollback-body')).to_contain_text(info['target'])
        expect(page.locator('#rollback-body pre')).to_have_text(preview['diff_unified'])
        keys.activate(page.get_by_text('Applied revision and affected proposals',exact=True))
        expect(page.locator('#rollback-body')).to_contain_text(preview['source']['application_id'])
        layouts('ready')
        assert not result['posts'] and not rows('SELECT id FROM llm_calls')
        assert snapshot()=={'database':info['database'],'head':info['head'],'target':info['current']}
        with page.expect_response(lambda r:r.url.endswith('/api/commands') and r.request.method=='POST') as response:
            keys.activate(page.locator('#rollback-submit'))
        assert response.value.status==202
        queued=response.value.json();oid=queued['id']
        assert queued['kind']=='rollback' and queued['state']=='queued',queued
        assert len(result['posts'])==1
        body=result['posts'][0]['body']
        assert body['action']=='rollback' and body['proposal_id']==info['proposals'][0]
        assert body['preview_revision']==preview['revision']
        page.wait_for_load_state('networkidle')
        before_worker=snapshot()
        assert before_worker['target']==info['current'] and before_worker['head']==info['head']
        assert not rows("SELECT id FROM proposal_events WHERE event='rolled_back'")
        assert len(rows('SELECT * FROM instruction_requests'))==1
        expect(page.locator('#rollback-body')).to_contain_text('Queued')
        page.reload();page.wait_for_load_state('networkidle')
        expect(page.locator('#rollback-body')).to_contain_text('Queued')
        expect(page.locator('#rollback-open-operation')).to_have_attribute('data-operation-focus',oid)
        expect(page.locator('#rollback-submit')).to_have_count(0)
        capture('queued-reloaded')
        assert snapshot()==before_worker
        delivered=worker()
        assert delivered['id']==oid and delivered['state']=='completed',delivered
        assert Path(info['target']).read_bytes()==info['reversed'].encode()
        inverse=rows("SELECT * FROM proposal_events WHERE event='rolled_back'")
        assert len(inverse)==1 and inverse[0]['proposal_id']==info['proposals'][0]
        assert all(event in rows('SELECT * FROM proposal_events') for event in info['events'])
        completed=snapshot();assert completed['head']!=info['head']
        page.reload();page.wait_for_load_state('networkidle')
        expect(page.locator('#rollback-body')).to_contain_text('Completed')
        expect(page.locator('#rollback-body')).to_contain_text('AlreadyRolledBack')
        expect(page.locator('#rollback-open-operation')).to_have_attribute('data-operation-focus',oid)
        layouts('completed')
        keys.activate(page.locator('#rollback-open-operation'))
        operation=page.locator('#operation-'+oid)
        expect(operation).to_contain_text('Completed')
        page.screenshot(path=str(OUT/'completed-history.png'))
        assert snapshot()==completed
        # Explicit transport replay is distinct from the initial native UI decision.
        replay=page.request.post('http://127.0.0.1:8876/api/commands',data=body)
        result['posts'].append({'source':'transport-replay','body':body,'status':replay.status})
        assert replay.status==202 and replay.json()['id']==oid
        assert worker() is None
        assert snapshot()==completed
        assert len(rows('SELECT * FROM instruction_requests'))==1
        assert rows("SELECT * FROM proposal_events WHERE event='rolled_back'")==inverse
        assert not rows('SELECT id FROM llm_calls')
        assert len(result['posts'])==2 and not result['errors'],result
        assert all(url.startswith('http://127.0.0.1:8876/') for method,url in result['requests'])
        visual.assert_clean()
        result.update(operation_id=oid,models=0,queue_did_not_write=True,replay_unchanged=True,
            separate_worker_completed=True,later_rule_and_human_preserved=True)
        print('ORDINARY_ROLLBACK_BROWSER_OK',len(result['states']),'states; native exact-revision intent; separate worker completed; one inverse; replay unchanged; zero models')
    except Exception:
        page.screenshot(path=str(OUT/'failure.png'))
        (OUT/'failure.html').write_text(page.content())
        raise
    finally:
        keys.save();(OUT/'result.json').write_text(json.dumps(result,indent=2));browser.close()
