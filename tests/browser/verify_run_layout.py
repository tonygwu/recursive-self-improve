"""Run hierarchy, real delivery evidence and read failure recovery in the browser."""
from pathlib import Path
import json
import sqlite3
from playwright.sync_api import sync_playwright, expect

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'reports/dashboard-parity/run-layout'
OUT.mkdir(parents=True,exist_ok=True)
info=json.loads((OUT.parent/'run-layout-manifest.json').read_text())
assert 'si-run-layout-' in info['db']
BASE='http://127.0.0.1:8877/'
result={'captures':[],'requests':[],'errors':[]}
with sync_playwright() as runtime:
    browser=runtime.chromium.launch(headless=True)
    page=browser.new_page(viewport={'width':1280,'height':1024})
    page.on('pageerror',lambda error:result['errors'].append(str(error)))
    page.on('request',lambda request:result['requests'].append([request.method,request.url]))
    def visit(name,stage=''):
        page.goto(BASE+'#/overview/run/'+name+('/'+stage if stage else ''))
        page.wait_for_load_state('networkidle')
        expect(page.locator('#run-title')).to_be_visible()
    def capture(name):
        page.locator('#main').evaluate('el=>el.scrollTop=0')
        page.screenshot(path=str(OUT/(name+'.png')))
        assert page.locator('#run-detail').evaluate('el=>el.scrollWidth<=el.clientWidth')
        result['captures'].append(name)
    def themes(name):
        for width in (1280,1440):
            page.set_viewport_size({'width':width,'height':1024})
            for theme in ('light','dark'):
                current=page.evaluate('async()=>(await import("/app.js")).effectiveTheme()')
                if current!=theme:page.get_by_role('button',name=theme.title()+' theme',exact=True).click()
                capture(f'{name}-{theme}-{width}')
                delivery=page.locator('#run-section-deliveries').bounding_box()
                assert delivery['y']+delivery['height']<1024,delivery
    try:
        visit('held-run')
        capture('initial')
        (OUT/'initial.html').write_text(page.content())
        delivery=page.locator('#run-section-deliveries').bounding_box()
        result['baseline_or_final_delivery']=delivery
        assert delivery['y']+delivery['height']<1024,delivery
        expect(page.locator('#run-delivery-heading')).to_have_text('No automatic edits recorded')
        expect(page.locator('.run-cap-counts')).to_contain_text('frustration: 7')
        expect(page.locator('.run-cap-counts')).to_contain_text('standing instruction: 2')
        expect(page.locator('#run-stage-gate')).to_contain_text('refused: 2')
        assert 'succeeded' not in page.locator('#run-stage-gate').inner_text()
        expect(page.locator('#run-stage-apply')).to_contain_text('held: 4')
        assert page.locator('#run-section-deliveries').bounding_box()['y']<page.locator('.run-budget').bounding_box()['y']
        themes('held')
        page.get_by_text('Call budgets and caps',exact=True).click()
        expect(page.locator('.run-budget')).to_contain_text('0 / 20')
        page.get_by_text('Time accounting',exact=True).click()
        expect(page.locator('#run-detail')).to_contain_text('Unaccounted: 90 s')
        page.get_by_text('Scan coverage',exact=True).click()
        page.get_by_text('Working-copy rule availability',exact=True).click()
        expect(page.locator('#run-detail')).to_contain_text('No working-copy availability collection was retained for this run.')
        for key in ('budget','timing','scan-coverage','availability-coverage'):
            control=page.locator('#run-'+key+'-summary')
            control.focus()
            page.evaluate('async()=>{const app=await import("/app.js");await app.loadRunRecords("calls");}')
            expect(control).to_be_focused()
        visit('delivered-run')
        expect(page.locator('#run-delivery-heading')).to_have_text('21 automatic edits recorded')
        themes('delivered')
        summary=page.locator('#run-delivery-records > summary')
        summary.focus();page.keyboard.press('Enter')
        expect(page.locator('#run-status-deliveries')).to_have_text('20 of 21 retained records shown')
        expect(page.locator('#run-section-deliveries')).to_contain_text('Complete delivered fixture text')
        expect(page.locator('#run-section-deliveries')).to_contain_text('Snapshot before:')
        expect(page.locator('#run-section-deliveries')).to_contain_text('Snapshot after:')
        expect(page.locator('#run-section-deliveries').get_by_role('link',name='Inspect rollback eligibility')).to_have_count(20)
        page.locator('#run-older-deliveries').focus();page.keyboard.press('Enter')
        expect(page.locator('#run-status-deliveries')).to_have_text('21 of 21 retained records shown')
        expect(page.locator('#run-status-deliveries')).to_be_focused()
        capture('complete-deliveries')
        summary.focus()
        page.evaluate('async()=>{const app=await import("/app.js");await app.loadRunRecords("calls");}')
        expect(summary).to_be_focused()
        assert page.locator('#run-delivery-records').evaluate('el=>el.open')
        page.reload();page.wait_for_load_state('networkidle')
        expect(page.locator('#run-delivery-heading')).to_have_text('21 automatic edits recorded')
        # Historical gaps must not acquire a zero-write claim.
        for name,reason in [('unknown-run','did not record exact delivery operation IDs'),('mismatch-run','does not reconcile'),('absent-run','did not record exact delivery operation IDs')]:
            visit(name)
            expect(page.locator('#run-delivery-heading')).to_have_text('Applied edits not established')
            expect(page.locator('#run-section-deliveries')).to_contain_text(reason)
            assert 'No automatic edits recorded' not in page.locator('#run-section-deliveries').inner_text()
            capture(name)
        visit(info['long_id'],'gate')
        expect(page.locator('#run-stage-gate')).to_be_focused()
        expect(page.locator('.run-heading')).to_contain_text(info['long_id'])
        capture('long-identity')
        # The real delivery reader fails once; retry cannot invent an empty result.
        page.route('**/api/runs/held-run/records?kind=deliveries*',lambda route:route.fulfill(status=503,json={'detail':'Invented delivery read failure'}))
        visit('held-run')
        expect(page.locator('#run-section-deliveries [role="alert"]')).to_contain_text('Invented delivery read failure')
        expect(page.locator('#run-delivery-heading')).to_have_text('Applied edits unavailable')
        expect(page.locator('#run-status-deliveries')).to_have_text('Records not loaded')
        assert 'Loading records' not in page.locator('#run-section-deliveries').inner_text()
        capture('delivery-reader-error')
        page.unroute('**/api/runs/held-run/records?kind=deliveries*')
        page.locator('#run-older-deliveries').focus();page.keyboard.press('Enter')
        expect(page.locator('#run-delivery-heading')).to_have_text('No automatic edits recorded')
        expect(page.locator('#run-status-deliveries')).to_be_focused()
        # A delayed delivery result cannot replace the next exact run's evidence.
        delayed=[]
        pattern='**/api/runs/held-run/records?kind=deliveries*'
        page.route(pattern,lambda route:delayed.append(route))
        page.locator('#run-refresh').click()
        expect(page.locator('#run-delivery-heading')).to_have_text('Loading applied edits…')
        assert delayed
        assert page.locator('#run-section-deliveries').inner_text().count('Loading records…')==1
        capture('delivery-loading')
        page.locator('#run-selector').select_option('unknown-run')
        expect(page.locator('#run-delivery-heading')).to_have_text('Applied edits not established')
        delayed[0].fulfill(status=200,json={'run_id':'held-run','kind':'deliveries','records':[],
            'count':0,'reason':'','next_cursor':None})
        page.wait_for_load_state('networkidle')
        expect(page.locator('#run-delivery-heading')).to_have_text('Applied edits not established')
        assert page.url.endswith('/unknown-run')
        page.unroute(pattern)
        # Back/Forward and the same-time chooser retain exact run and stage.
        visit('held-run','gate')
        page.locator('#run-selector').select_option('unknown-run');page.wait_for_load_state('networkidle')
        assert page.url.endswith('/unknown-run/gate')
        expect(page.locator('#run-delivery-heading')).to_have_text('Applied edits not established')
        page.go_back();page.wait_for_load_state('networkidle')
        expect(page.locator('#run-delivery-heading')).to_have_text('No automatic edits recorded')
        page.go_forward();page.reload();page.wait_for_load_state('networkidle')
        expect(page.locator('#run-delivery-heading')).to_have_text('Applied edits not established')
        assert page.url.endswith('/unknown-run/gate')
        with sqlite3.connect('file:'+info['db']+'?mode=ro',uri=True) as db:
            assert list(db.iterdump())==info['snapshot']
            assert db.execute('SELECT COUNT(*) FROM llm_calls').fetchone()[0]==0
        assert all(Path(path).read_text()==text for path,text in info['targets'].items())
        assert all(method=='GET' for method,url in result['requests'])
        assert not result['errors'],result['errors']
        result.update(store='unchanged',targets='unchanged',models=0)
        print('RUN_LAYOUT_OK',json.dumps({'captures':len(result['captures']),'requests':len(result['requests']),'models':0,'errors':result['errors'],'store':result['store'],'targets':result['targets']}))
    except BaseException:
        page.screenshot(path=str(OUT/'failure.png'))
        (OUT/'failure.html').write_text(page.content())
        raise
    finally:
        browser.close()
        (OUT/'result.json').write_text(json.dumps(result,indent=2))
