"""Measure complete words in Run's absent counts without inferring missing totals."""
from pathlib import Path
import json
import sqlite3
import tempfile

from playwright.sync_api import sync_playwright, expect
from keyboard_actions import KeyboardActions
from visual_checks import VisualChecks

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'reports/dashboard-parity/run-unknown-labels'
OUT.mkdir(parents=True, exist_ok=True)
manifest = json.loads((OUT.parent/'run-backlog/manifest.json').read_text())
assert 'si-run-backlog-' in manifest['db']
result = {'states': [], 'requests': [], 'errors': []}

# Character ranges detect a split inside a word, regardless of DOM wrappers.
WORDS = r'''cells => cells.filter(e=>e.textContent==='Not recorded').map(cell=>{
 const box=cell.getBoundingClientRect(),words=[];
 const walker=document.createTreeWalker(cell,NodeFilter.SHOW_TEXT);
 for(let node;node=walker.nextNode();){
   for(const match of node.textContent.matchAll(/\S+/g)){
     const rects=[];
     for(let i=match.index;i<match.index+match[0].length;i++){
       const range=document.createRange();range.setStart(node,i);range.setEnd(node,i+1);
       rects.push(...[...range.getClientRects()].map(r=>({top:r.top,left:r.left,right:r.right})));
     }
     words.push({word:match[0],lines:[...new Set(rects.map(r=>r.top))],
       inside:rects.every(r=>r.left>=box.left&&r.right<=box.right)});
   }
 }
 return {stage:cell.closest('tr').id,column:cell.dataset.runCount,width:box.width,
   wrapping:getComputedStyle(cell).overflowWrap,words};
})'''

with tempfile.TemporaryDirectory(prefix='si-run-label-browser-') as temp, sync_playwright() as runtime:
    folder = Path(temp); extension = folder/'extension'; extension.mkdir()
    (extension/'manifest.json').write_text(json.dumps({'manifest_version':3,'name':'Temporary Run label zoom',
        'version':'1.0','host_permissions':['http://127.0.0.1/*'],'background':{'service_worker':'worker.js'}}))
    (extension/'worker.js').write_text('chrome.runtime.onInstalled.addListener(() => {});')
    browser = runtime.chromium.launch_persistent_context(str(folder/'profile'),channel='chromium',headless=True,
        args=[f'--disable-extensions-except={extension}',f'--load-extension={extension}'],viewport={'width':1280,'height':1024})
    page = browser.pages[0]
    zoom = browser.service_workers[0] if browser.service_workers else browser.wait_for_event('serviceworker')
    keys = KeyboardActions(page, OUT/'keyboard.json'); visual = VisualChecks(page, OUT/'contrast.json')
    page.on('pageerror',lambda e:result['errors'].append(str(e)))
    page.on('request',lambda r:result['requests'].append([r.method,r.url]))

    def capture(name):
        cells = page.locator('.run-stage-table [data-run-count]').evaluate_all(WORDS)
        result['states'].append({'name':name,'cells':cells})
        (OUT/'result.json').write_text(json.dumps(result,indent=2))
        page.locator('.run-stage-table').scroll_into_view_if_needed()
        page.screenshot(path=str(OUT/(name+'.png')))
        (OUT/(name+'.html')).write_text(page.content())
        assert cells, 'Missing historical counts must be shown explicitly'
        bad = [c for c in cells if any(len(w['lines'])!=1 or not w['inside'] for w in c['words'])]
        assert not bad, bad
        assert page.locator('#run-detail').evaluate('e=>e.scrollWidth<=e.clientWidth')
        visual.check(name)

    for run in ('selected','old'):
        page.goto('http://127.0.0.1:8876/#/overview/run/'+run);page.wait_for_load_state('networkidle')
        expect(page.locator('#run-title')).to_be_visible()
        for width in (1280,1440,640):
            page.set_viewport_size({'width':width,'height':1024})
            for theme in ('light','dark'):
                keys.theme(theme);capture(f'{run}-{theme}-{width}')
                if run=='selected':
                    expect(page.locator('#run-stage-scan [data-run-count="input"]')).to_have_text('0')
                    expect(page.locator('#run-stage-cluster [data-run-count="input"]')).to_have_text('Not recorded')
        if run=='selected':
            summary=page.locator('#run-stage-scan details > summary').last
            keys.activate(summary)
            expect(page.locator('#run-stage-scan')).to_contain_text('"files_attempted": 0')
            expect(summary).to_be_focused();capture('complete-native-record')
        page.set_viewport_size({'width':1440,'height':1024})
        receipt=zoom.evaluate('''async()=>{const [tab]=await chrome.tabs.query({active:true,currentWindow:true});
            await chrome.tabs.setZoom(tab.id,2);return chrome.tabs.getZoom(tab.id);}''')
        assert receipt==2
        for theme in ('light','dark'):
            keys.theme(theme)
            region=page.get_by_role('region',name='Stage counts and complete records',exact=True)
            keys.reach(region)
            before=region.evaluate('e=>({left:e.scrollLeft,width:e.clientWidth,total:e.scrollWidth})')
            assert before['total']>before['width']
            direction=-1 if before['left']>0 else 1
            page.keyboard.press('ArrowLeft' if direction<0 else 'ArrowRight')
            page.wait_for_function('p=>(document.querySelector(".run-stage-table").parentElement.scrollLeft-p.left)*p.direction>0',arg={**before,'direction':direction})
            capture(f'{run}-{theme}-native-200-percent')
        zoom.evaluate('''async()=>{const [tab]=await chrome.tabs.query({active:true,currentWindow:true});await chrome.tabs.setZoom(tab.id,1);}''')
    assert not result['errors'],result['errors']
    assert all(method=='GET' for method,url in result['requests'])
    visual.assert_clean();keys.save();browser.close()
with sqlite3.connect('file:'+manifest['db']+'?mode=ro',uri=True) as conn:
    assert list(conn.iterdump())==manifest['snapshot']
result.update(models=0,database_unchanged=True)
(OUT/'result.json').write_text(json.dumps(result,indent=2))
print('RUN_UNKNOWN_LABELS_OK',len(result['states']),'states; whole words; zero models; database unchanged')
