"""Inspect v2 Run order, native-unit columns and real read recovery on scratch state."""
from pathlib import Path
import hashlib
import json
import sqlite3
import tempfile

from playwright.sync_api import sync_playwright, expect
from keyboard_actions import KeyboardActions
from visual_checks import VisualChecks

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'reports/dashboard-parity/run-composition-v2/browser'
OUT.mkdir(parents=True, exist_ok=True)
info = json.loads((OUT.parent.parent / 'run-layout-manifest.json').read_text())
assert 'si-run-layout-' in info['db']
BASE = 'http://127.0.0.1:8877/'
inputs = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
          for p in [ROOT/'src/self_improve/dashboard/static/app.js', ROOT/'src/self_improve/dashboard/static/app.css']}
result = {'source_hashes':inputs, 'states':[], 'requests':[], 'errors':[]}
with tempfile.TemporaryDirectory(prefix='si-run-v2-browser-') as temp, sync_playwright() as runtime:
    folder = Path(temp)
    extension = folder / 'extension'
    extension.mkdir()
    (extension/'manifest.json').write_text(json.dumps({'manifest_version':3,'name':'Temporary Run fixture zoom',
        'version':'1.0','host_permissions':['http://127.0.0.1/*'],'background':{'service_worker':'worker.js'}}))
    (extension/'worker.js').write_text('chrome.runtime.onInstalled.addListener(() => {});')
    browser = runtime.chromium.launch_persistent_context(str(folder/'profile'),channel='chromium',headless=True,
        args=[f'--disable-extensions-except={extension}',f'--load-extension={extension}'],viewport={'width':1280,'height':1024})
    page = browser.pages[0]
    worker = browser.service_workers[0] if browser.service_workers else browser.wait_for_event('serviceworker')
    keys = KeyboardActions(page, OUT/'keyboard.json')
    visual = VisualChecks(page, OUT/'contrast.json')
    page.on('pageerror',lambda error:result['errors'].append(str(error)))
    page.on('request',lambda request:result['requests'].append([request.method,request.url]))

    def visit(name):
        page.goto(BASE+'#/overview/run/'+name)
        page.wait_for_load_state('networkidle')
        expect(page.locator('#run-title')).to_be_visible()

    def capture(name):
        page.screenshot(path=str(OUT/(name+'.png')))
        (OUT/(name+'.html')).write_text(page.content())
        assert page.locator('#run-detail').evaluate('e=>e.scrollWidth<=e.clientWidth')
        visual.check(name)

    def check_layout(name):
        page.locator('#main').evaluate('e=>e.scrollTop=0')
        capture(name)
        boxes={key:page.locator(selector).bounding_box() for key,selector in [
            ('identity','.run-heading'),('stages','.run-stage-table'),('usage','.run-usage'),('delivery','#run-section-deliveries')]}
        result['states'].append({'name':name,'geometry':boxes})
        assert boxes['identity']['height']<160,boxes
        assert boxes['identity']['y']+boxes['identity']['height']<=boxes['stages']['y'],boxes
        assert boxes['stages']['y']+boxes['stages']['height']<boxes['usage']['y'],boxes
        assert boxes['usage']['y']+boxes['usage']['height']<boxes['delivery']['y'],boxes
        assert boxes['delivery']['y']+boxes['delivery']['height']<1024,boxes
        assert page.locator('.run-stage-table thead th').all_text_contents()==['Stage','Input units','Completed','Failures','Held / other','What happened']
        # Counts belong to different native units; do not turn held work into applied edits.
        expected={'scan':['12','11','1','—'],'mine':['5','4','1','refused: 0'],
                  'cluster':['0','0','0','—'],'gate':['2','0','0','refused: 2']}
        expected['apply']=['21','21','0','held: 0'] if name.startswith('delivered') else ['0','0','0','held: 4']
        for stage,values in expected.items():
            row=page.locator('#run-stage-'+stage)
            assert row.locator('[data-run-count]').all_text_contents()==values,(stage,row.inner_text())
        expect(page.locator('#run-stage-cluster')).to_contain_text('4 candidate rules')
        expect(page.locator('#run-stage-gate')).to_contain_text('0 verdicts')

    try:
        visit('held-run')
        page.screenshot(path=str(OUT/'recon.png'))
        (OUT/'recon.html').write_text(page.content())
        for run in ['held-run','delivered-run']:
            visit(run)
            for width in [1280,1440]:
                page.set_viewport_size({'width':width,'height':1024})
                for theme in ['light','dark']:
                    keys.theme(theme)
                    check_layout(f'{run}-{theme}-{width}')
        stage_summary=page.locator('#run-stage-gate details > summary').last
        keys.activate(stage_summary)
        expect(page.locator('#run-stage-gate')).to_contain_text('"gated_pass": 0')
        keys.reach(stage_summary)
        page.evaluate('async()=>{await (await import("/app.js")).loadRunRecords("calls")}')
        expect(stage_summary).to_be_focused()
        assert stage_summary.evaluate('e=>e.parentElement.open')
        capture('complete-stage-retains-focus')
        stage_region=page.get_by_role('region',name='Stage counts and complete records',exact=True)
        keys.reach(stage_region)
        page.evaluate('async()=>{await (await import("/app.js")).loadRunRecords("calls")}')
        expect(stage_region).to_be_focused()
        capture('stage-region-retains-focus')
        # Exact same-time selection by native keys, with no route inference from time.
        keys.choose(page.locator('#run-selector'),'unknown-run')
        expect(page.locator('#run-delivery-heading')).to_have_text('Applied edits not established')
        expect(page.locator('#run-stage-mine [data-run-count="input"]')).to_have_text('Not recorded')
        visit(info['long_id'])
        for width in [1280,1440]:
            page.set_viewport_size({'width':width,'height':1024})
            for theme in ['light','dark']:
                keys.theme(theme);page.locator('#main').evaluate('e=>e.scrollTop=0')
                expect(page.locator('.run-heading__summary')).to_contain_text(info['long_id'])
                capture(f'long-identity-{theme}-{width}')
        # Browser-level 200% zoom, not CSS zoom or a narrower screenshot.
        visit('held-run')
        for width in [1280,1440]:
            page.set_viewport_size({'width':width,'height':1024})
            receipt=worker.evaluate('''async()=>{const tabs=await chrome.tabs.query({url:'http://127.0.0.1:8877/*'});
                if(tabs.length!==1)throw Error('Expected one scratch tab');await chrome.tabs.setZoom(tabs[0].id,2);
                return {zoom:await chrome.tabs.getZoom(tabs[0].id),tab:tabs[0].url};}''')
            assert receipt['zoom']==2
            for theme in ['light','dark']:
                keys.theme(theme);keys.reach(page.locator('#run-refresh'))
                capture(f'zoom-200-{theme}-{width}')
                scroll=page.locator('.run-stage-table').locator('..')
                keys.reach(scroll)
                before=scroll.evaluate('e=>({left:e.scrollLeft,width:e.clientWidth,total:e.scrollWidth})')
                assert before['total']>before['width'],before
                # Tabbing through the complete-record disclosures can scroll to
                # the right edge already. Prove movement into available space.
                direction=-1 if before['left']>0 else 1
                observation={'name':f'zoom-{theme}-{width}','receipt':receipt,
                             'scroll_before':before,'direction':direction}
                result['states'].append(observation)
                page.keyboard.press('ArrowLeft' if direction<0 else 'ArrowRight')
                page.wait_for_function('p=>(document.querySelector(".run-stage-table").parentElement.scrollLeft-p.left)*p.direction>0',arg={**before,'direction':direction})
                capture(f'zoom-200-stage-{theme}-{width}')
                observation['scroll_after']=scroll.evaluate('e=>e.scrollLeft')
            worker.evaluate('''async()=>{const tabs=await chrome.tabs.query({url:'http://127.0.0.1:8877/*'});await chrome.tabs.setZoom(tabs[0].id,1);}''')
        pattern='**/api/runs/held-run/records?kind=deliveries*'
        page.route(pattern,lambda route:route.fulfill(status=503,json={'error':'Invented delivery reader unavailable','detail':'Preserved <complete> diagnostic'}))
        visit('held-run')
        # The same hash intentionally retains an already loaded run. Use the
        # user's native Refresh control to request the intercepted read.
        keys.activate(page.locator('#run-refresh'))
        delivery=page.locator('#run-section-deliveries')
        expect(delivery.locator('[role="alert"]')).to_contain_text('Could not read applied edits.')
        assert 'GET /api' not in delivery.locator('[role="alert"]').inner_text()
        expect(delivery).to_contain_text('Use Retry below')
        keys.activate(delivery.get_by_text('Request details',exact=True))
        expect(delivery.locator('pre')).to_contain_text('GET /api/runs/held-run/records')
        expect(delivery.locator('pre')).to_contain_text('<complete>')
        assert delivery.locator('complete').count()==0
        diagnostic_summary=delivery.get_by_text('Request details',exact=True)
        page.evaluate('async()=>{await (await import("/app.js")).loadRunRecords("calls")}')
        expect(diagnostic_summary).to_be_focused()
        assert diagnostic_summary.evaluate('e=>e.parentElement.open')
        delivery.locator('pre').scroll_into_view_if_needed()
        capture('complete-error-diagnostic')
        page.unroute(pattern)
        keys.activate(page.locator('#run-older-deliveries'))
        expect(page.locator('#run-delivery-heading')).to_have_text('No automatic edits recorded')
        assert delivery.locator('[role="alert"]').count()==0
        expect(page.locator('#run-status-deliveries')).to_be_focused()
        capture('delivery-read-recovered')
        page.route('**/api/runs/absent-run',lambda route:route.fulfill(status=503,json={'error':'Invented run unavailable'}))
        page.goto(BASE+'#/overview/run/absent-run');page.wait_for_load_state('networkidle')
        expect(page.locator('#run-detail [role="alert"]')).to_contain_text('Could not read this run.')
        assert 'GET /api' not in page.locator('#run-detail [role="alert"]').inner_text()
        capture('initial-run-error')
        page.unroute('**/api/runs/absent-run')
        keys.activate(page.locator('#run-detail').get_by_role('button',name='Retry',exact=True))
        expect(page.locator('#run-title')).to_be_visible()
        expect(page.locator('#run-stage-mine [data-run-count="input"]')).to_have_text('Not recorded')
        capture('initial-run-recovered')
        with sqlite3.connect('file:'+info['db']+'?mode=ro',uri=True) as db:
            assert list(db.iterdump())==info['snapshot']
            assert db.execute('SELECT COUNT(*) FROM llm_calls').fetchone()[0]==0
        assert all(Path(path).read_text()==text for path,text in info['targets'].items())
        assert all(hashlib.sha256((ROOT/p).read_bytes()).hexdigest()==h for p,h in inputs.items())
        assert all(method=='GET' for method,url in result['requests'])
        assert not result['errors'],result['errors']
        visual.assert_clean()
        result.update(status='succeeded',store='unchanged',targets='unchanged',source='unchanged',models=0)
        print('RUN_V2_COMPOSITION_OK',json.dumps({'states':len(result['states']),'GETs':len(result['requests']),'models':0,'unchanged':True}))
    except BaseException:
        page.screenshot(path=str(OUT/'failure.png'));(OUT/'failure.html').write_text(page.content())
        raise
    finally:
        keys.save();browser.close();(OUT/'result.json').write_text(json.dumps(result,indent=2)+'\n')
