"""Native Run backlog state, conditional comparison and read-only acceptance."""
from pathlib import Path
import json
import sqlite3
import tempfile
from playwright.sync_api import sync_playwright, expect
from visual_checks import VisualChecks
from keyboard_actions import KeyboardActions

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'reports/dashboard-parity/run-backlog/browser';OUT.mkdir(parents=True,exist_ok=True)
manifest=json.loads((OUT.parent/'manifest.json').read_text());assert 'si-run-backlog-' in manifest['db']
BASE='http://127.0.0.1:8876/'
result={'states':[],'requests':[],'errors':[]}
with tempfile.TemporaryDirectory(prefix='si-run-backlog-browser-') as temp, sync_playwright() as runtime:
    folder=Path(temp);extension=folder/'extension';extension.mkdir()
    (extension/'manifest.json').write_text(json.dumps({'manifest_version':3,'name':'Temporary queue zoom','version':'1.0',
      'host_permissions':['http://127.0.0.1/*'],'background':{'service_worker':'worker.js'}}))
    (extension/'worker.js').write_text('chrome.runtime.onInstalled.addListener(() => {});')
    browser=runtime.chromium.launch_persistent_context(str(folder/'profile'),channel='chromium',headless=True,
      args=[f'--disable-extensions-except={extension}',f'--load-extension={extension}'],viewport={'width':1280,'height':1024})
    page=browser.pages[0];worker=browser.service_workers[0] if browser.service_workers else browser.wait_for_event('serviceworker')
    page.on('pageerror',lambda e:result['errors'].append(str(e)))
    page.on('request',lambda req:result['requests'].append([req.method,req.url]))
    visual=VisualChecks(page,OUT/'contrast.json');keys=KeyboardActions(page,OUT/'keyboard.json')
    def visit(run):
        page.goto(BASE+'#/overview/run/'+run);page.wait_for_load_state('networkidle')
        expect(page.locator('#run-section-backlog')).to_be_visible()
    def capture(name):
        page.locator('#run-section-backlog').scroll_into_view_if_needed()
        page.screenshot(path=str(OUT/(name+'.png')))
        (OUT/(name+'.html')).write_text(page.content())
        assert page.locator('#run-detail').evaluate('e=>e.scrollWidth<=e.clientWidth')
        visual.check(name);result['states'].append(name)
    for width in (1280,1440):
        for theme in ('light','dark'):
            page.set_viewport_size({'width':width,'height':1024});visit('selected');keys.theme(theme)
            capture(f'{theme}-{width}-reconnaissance')
            panel=page.locator('#run-section-backlog')
            expect(panel).to_contain_text('3 unmined')
            expect(panel).to_contain_text('0.14 admissions and 1.14 processed exits per day')
            expect(panel).to_contain_text('3 days')
            expect(panel).to_contain_text('only if these observed rates continue')
            summary=page.locator('#run-backlog-settings');keys.reach(summary)
            if panel.locator('details').first.get_attribute('open') is None:page.keyboard.press('Enter')
            expect(panel).to_contain_text('END_FILTER')
            expect(panel).to_contain_text('cheap 80, strong 10, gate 78')
            expect(summary).to_be_focused();capture(f'{theme}-{width}-details')
            keys.activate(page.locator('#run-backlog-refresh'));page.wait_for_load_state('networkidle')
            expect(page.locator('#run-backlog-refresh')).to_be_focused()
            expect(panel.locator('details').first).to_have_attribute('open','')
            for run,expected in [('partial','Seven complete UTC days'),('maintenance','maintenance or unattributed'),
                ('growing','queue is growing'),('empty','recorded queue is empty'),('steady','queue is steady'),
                ('start-only','No terminal queue snapshot'),('old','Historical queue size unavailable')]:
                visit(run)
                if run in ('partial','maintenance','start-only'):
                    summary=page.locator('#run-backlog-settings');keys.reach(summary)
                    if page.locator('#run-section-backlog details').first.get_attribute('open') is None:page.keyboard.press('Enter')
                expect(page.locator('#run-section-backlog')).to_contain_text(expected)
                if run!='start-only':expect(page.locator('#run-section-backlog')).not_to_contain_text('only if these observed rates continue')
                capture(f'{theme}-{width}-{run}')
    # Hold one actual GET to inspect loading, then let it finish without polling.
    held=[]
    page.route('**/api/runs/selected/backlog',lambda route:held.append(route))
    page.goto(BASE+'#/overview/run/selected')
    expect(page.locator('#run-section-backlog')).to_contain_text('Loading queue history')
    capture('loading')
    assert len(held)==1;held[0].continue_();page.unroute('**/api/runs/selected/backlog');page.wait_for_load_state('networkidle')
    # Failure on refresh preserves evidence, Retry replaces it and keeps focus.
    page.route('**/api/runs/selected/backlog',lambda route:route.fulfill(status=503,content_type='application/json',body=json.dumps({'detail':'Invented queue read failure'})))
    keys.activate(page.locator('#run-backlog-refresh'))
    expect(page.locator('#run-section-backlog [role="alert"]')).to_contain_text('Invented queue read failure')
    expect(page.locator('#run-section-backlog')).to_contain_text('3 unmined');capture('failed-refresh')
    page.unroute('**/api/runs/selected/backlog')
    expect(page.locator('#run-backlog-refresh')).to_have_text('Retry')
    page.keyboard.press('Enter');page.wait_for_load_state('networkidle')
    expect(page.locator('#run-section-backlog [role="alert"]')).to_have_count(0)
    expect(page.locator('#run-backlog-refresh')).to_be_focused()
    expect(page.locator('#run-backlog-refresh')).to_have_attribute('aria-disabled','false')
    capture('recovered')
    page.set_viewport_size({'width':640,'height':1024});visit('selected');page.locator('#run-backlog-settings').focus()
    if page.locator('#run-section-backlog details').first.get_attribute('open') is None:page.keyboard.press('Enter')
    expect(page.locator('#run-section-backlog details').first).to_have_attribute('open','');capture('narrow-details')
    page.set_viewport_size({'width':1440,'height':1024})
    worker.evaluate('async()=>{const tabs=await chrome.tabs.query({active:true,currentWindow:true});await chrome.tabs.setZoom(tabs[0].id,2);}')
    assert worker.evaluate('async()=>{const tabs=await chrome.tabs.query({active:true,currentWindow:true});return chrome.tabs.getZoom(tabs[0].id);}')==2
    capture('native-200-percent')
    worker.evaluate('async()=>{const tabs=await chrome.tabs.query({active:true,currentWindow:true});await chrome.tabs.setZoom(tabs[0].id,1);}')
    assert not result['errors'],result['errors']
    assert all(method=='GET' for method,url in result['requests'])
    visual.assert_clean();keys.save();browser.close()
with sqlite3.connect('file:'+manifest['db']+'?mode=ro',uri=True) as conn:
    assert list(conn.iterdump())==manifest['snapshot']
result['models']=0;result['database_unchanged']=True
(OUT/'result.json').write_text(json.dumps(result,indent=2))
print('RUN_BACKLOG_BROWSER_OK',len(result['states']),'states;',len(result['requests']),'GETs; zero models; database unchanged')
