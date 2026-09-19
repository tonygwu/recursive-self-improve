"""Native read-only Run relation and exact Review navigation on temporary evidence."""
from pathlib import Path
import hashlib
import json
import sqlite3
import tempfile
from playwright.sync_api import sync_playwright, expect
from visual_checks import VisualChecks
from keyboard_actions import KeyboardActions

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'reports/dashboard-parity/run-related-deliveries/browser'
OUT.mkdir(parents=True,exist_ok=True)
info=json.loads((OUT.parent/'manifest.json').read_text())
assert 'si-run-related-' in info['db']
BASE='http://127.0.0.1:8876/'
result={'requests':[],'errors':[],'states':[]}
with tempfile.TemporaryDirectory(prefix='si-run-related-browser-') as temp, sync_playwright() as runtime:
    folder=Path(temp);extension=folder/'extension';extension.mkdir()
    (extension/'manifest.json').write_text(json.dumps({'manifest_version':3,'name':'Temporary related-delivery zoom',
        'version':'1.0','host_permissions':['http://127.0.0.1/*'],'background':{'service_worker':'worker.js'}}))
    (extension/'worker.js').write_text('chrome.runtime.onInstalled.addListener(() => {});')
    browser=runtime.chromium.launch_persistent_context(str(folder/'profile'),channel='chromium',headless=True,
        args=[f'--disable-extensions-except={extension}',f'--load-extension={extension}'],viewport={'width':1280,'height':1024})
    page=browser.pages[0]
    worker=browser.service_workers[0] if browser.service_workers else browser.wait_for_event('serviceworker')
    page.on('pageerror',lambda error:result['errors'].append(str(error)))
    page.on('request',lambda request:result['requests'].append([request.method,request.url]))
    visual=VisualChecks(page,OUT/'contrast.json')
    keys=KeyboardActions(page,OUT/'keyboard.json')
    def visit(run='selected'):
        page.goto(BASE+'#/overview/run/'+run);page.wait_for_load_state('networkidle')
        expect(page.locator('#run-section-related_deliveries')).to_be_visible()
    def capture(name):
        page.screenshot(path=str(OUT/(name+'.png')))
        (OUT/(name+'.html')).write_text(page.content())
        assert page.locator('#run-detail').evaluate('e=>e.scrollWidth<=e.clientWidth')
        visual.check(name)
        result['states'].append(name)
    for width in (1280,1440):
        for theme in ('light','dark'):
            page.set_viewport_size({'width':width,'height':1024});visit()
            keys.theme(theme)
            # Reconnaissance before interaction: retain the actual DOM and screenshot.
            capture(f'{theme}-{width}-initial')
            panel=page.locator('#run-section-related_deliveries')
            expect(panel.locator('.run-record')).to_have_count(20)
            expect(panel).to_contain_text('20 of 21 retained records shown')
            expect(panel).to_contain_text('Other member of this combined delivery')
            expect(panel).to_contain_text('END_RETAINED_TEXT')
            expect(page.locator('#run-delivery-heading')).to_have_text('No automatic edits recorded')
            page.locator('#run-older-related_deliveries').scroll_into_view_if_needed()
            page.locator('#run-older-related_deliveries').focus();page.keyboard.press('Enter')
            expect(panel.locator('.run-record')).to_have_count(21)
            expect(panel).to_contain_text('21 of 21 retained records shown')
            # Refresh is a real keyboard action; unrelated reads retain its focus.
            refresh=page.locator('#run-refresh-related-deliveries');refresh.focus();page.keyboard.press('Enter')
            expect(panel.locator('.run-record')).to_have_count(20)
            expect(refresh).to_be_focused()
            refresh.scroll_into_view_if_needed();capture(f'{theme}-{width}-refreshed')
            first=panel.locator('.run-record').first
            first.scroll_into_view_if_needed();capture(f'{theme}-{width}-complete')
            diff=first.get_by_role('region',name='Complete combined delivered diff')
            diff.focus();page.keyboard.press('ArrowRight')
            page.wait_for_function('id=>document.getElementById(id).scrollLeft>0',arg=diff.get_attribute('id'))
            expect(diff).to_be_focused()
            summary=first.locator('summary');summary.focus();page.keyboard.press('Enter')
            expect(first.locator('details')).to_have_attribute('open','')
            page.evaluate('async()=>{const app=await import("/app.js");await app.loadRunRecords("calls")}')
            expect(summary).to_be_focused()
            expect(first).to_contain_text('observation_ids')
            capture(f'{theme}-{width}-evidence')
            summary.press('Enter')
            command_link=first.locator('h3 a');command_link.focus();page.keyboard.press('Enter')
            page.wait_for_load_state('networkidle')
            assert page.url.endswith('#/review/command/'+info['commands'][-1]['id'])
            expect(page.locator('#main')).to_contain_text(info['commands'][-1]['id'])
            visit();panel=page.locator('#run-section-related_deliveries')
            rollback=panel.locator('a[href^="#/review/rollback/"]').first
            rollback.focus();page.keyboard.press('Enter');page.wait_for_load_state('networkidle')
            assert '#/review/rollback/' in page.url
            expect(page.locator('#main')).to_contain_text('Rollback')
            visit('empty');expect(page.locator('#run-status-related_deliveries')).to_contain_text('0 of 0')
            page.locator('#run-section-related_deliveries').scroll_into_view_if_needed();capture(f'{theme}-{width}-empty')
            visit('unknown');expect(page.locator('#run-section-related_deliveries')).to_contain_text('coverage is unknown')
            expect(page.locator('#run-status-related_deliveries')).not_to_contain_text('of 0')
            page.locator('#run-section-related_deliveries').scroll_into_view_if_needed();capture(f'{theme}-{width}-unknown')
    # Fail an actual new read, then recover through the rendered control.
    page.route('**/api/runs/selected/related-deliveries?*',lambda route:route.fulfill(status=503,content_type='application/json',body=json.dumps({'detail':'Invented historical reader unavailable'})))
    visit();panel=page.locator('#run-section-related_deliveries')
    expect(panel.locator('[role="alert"]')).to_contain_text('Invented historical reader unavailable')
    panel.scroll_into_view_if_needed();capture('read-failed')
    page.unroute('**/api/runs/selected/related-deliveries?*')
    panel.get_by_role('button',name='Retry',exact=True).focus();page.keyboard.press('Enter')
    expect(panel.locator('.run-record')).to_have_count(20)
    expect(panel.locator('[role="alert"]')).to_have_count(0)
    # A failed refresh keeps the first page; Retry replaces it without duplicates.
    page.route('**/api/runs/selected/related-deliveries?*',lambda route:route.fulfill(status=503,content_type='application/json',body=json.dumps({'detail':'Invented refresh failure'})))
    panel.get_by_role('button',name='Refresh earlier deliveries',exact=True).click()
    expect(panel.locator('[role="alert"]')).to_contain_text('Invented refresh failure')
    expect(panel.locator('.run-record')).to_have_count(20)
    capture('refresh-failed-retained')
    page.unroute('**/api/runs/selected/related-deliveries?*')
    panel.get_by_role('button',name='Retry',exact=True).focus();page.keyboard.press('Enter')
    expect(panel.locator('[role="alert"]')).to_have_count(0)
    expect(panel.locator('.run-record')).to_have_count(20)
    expect(panel).to_contain_text('20 of 21 retained records shown')
    links=panel.locator('.run-record h3 a').all_text_contents()
    assert len(links)==len(set(links))==20
    capture('refresh-recovered')
    # Narrow CSS viewport stresses contained long edits independently of 1280/1440.
    page.set_viewport_size({'width':640,'height':1024});panel.scroll_into_view_if_needed();capture('narrow-long-content')
    result['zoom']=[]
    for width in (1280,1440):
        page.set_viewport_size({'width':width,'height':1024});visit()
        receipt=worker.evaluate('''async()=>{const tabs=await chrome.tabs.query({url:'http://127.0.0.1:8876/*'});
            if(tabs.length!==1)throw Error('Expected one temporary tab');await chrome.tabs.setZoom(tabs[0].id,2);
            return {zoom:await chrome.tabs.getZoom(tabs[0].id),tab:tabs[0].url};}''')
        assert receipt['zoom']==2;result['zoom'].append({'width':width,**receipt})
        for theme in ('light','dark'):
            keys.theme(theme)
            page.locator('#run-section-related_deliveries .run-record').first.scroll_into_view_if_needed()
            capture(f'zoom-200-{theme}-{width}')
        worker.evaluate('''async()=>{const tabs=await chrome.tabs.query({url:'http://127.0.0.1:8876/*'});await chrome.tabs.setZoom(tabs[0].id,1);}''')
    visual.assert_clean()
    browser.close()
assert not result['errors'],result['errors']
assert all(method=='GET' for method,url in result['requests']),result['requests']
with sqlite3.connect('file:'+info['db']+'?mode=ro',uri=True) as conn:assert list(conn.iterdump())==info['snapshot']
assert all(Path(path).read_text()==text for path,text in info['targets'].items())
result['database_unchanged']=result['targets_unchanged']=True
(OUT/'result.json').write_text(json.dumps(result,indent=2))
print('RUN_RELATED_DELIVERIES_BROWSER_OK',len(result['states']),'states',len(result['requests']),'GETs')
