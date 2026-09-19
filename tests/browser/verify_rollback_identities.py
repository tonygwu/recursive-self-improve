"""Read complete retained rollback identities through native disclosures and zoom."""
from pathlib import Path
import hashlib
import json
import sqlite3
import subprocess
import tempfile

from playwright.sync_api import sync_playwright, expect
from keyboard_actions import KeyboardActions
from visual_checks import VisualChecks

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'reports/dashboard-parity/rollback-identities';OUT.mkdir(parents=True,exist_ok=True)
info=json.loads((OUT.parent/'ordinary-rollback/manifest.json').read_text())
root=Path(info['root']).resolve()
assert root.is_dir() and root.name.startswith('si-ordinary-rollback-')
assert all(Path(info[k]).resolve().is_relative_to(root) for k in ('db','target','snapshots'))
result={'states':[],'requests':[],'errors':[], 'source_hashes':{
    str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
    for p in [ROOT/'src/self_improve/dashboard/static/app.css',ROOT/'src/self_improve/dashboard/static/app.js',Path(__file__).resolve()]}}
GEOMETRY='''elements=>elements.map(el=>{
 const box=el.getBoundingClientRect(),panel=document.querySelector('#rollback-body').getBoundingClientRect();
 const walker=document.createTreeWalker(el,NodeFilter.SHOW_TEXT),rects=[];
 for(let node;node=walker.nextNode();){for(let i=0;i<node.length;i++){
   const range=document.createRange();range.setStart(node,i);range.setEnd(node,i+1);
   rects.push(...[...range.getClientRects()].map(r=>({left:r.left,right:r.right,top:r.top})));}}
 return {text:el.textContent,width:box.width,scrollWidth:el.scrollWidth,clientWidth:el.clientWidth,
   lines:[...new Set(rects.map(r=>r.top))].length,characters:rects.length,
   outside:rects.filter(r=>r.left<Math.max(box.left,panel.left)-.5||r.right>Math.min(box.right,panel.right)+.5)};
})'''

with tempfile.TemporaryDirectory(prefix='si-rollback-identity-browser-') as folder, sync_playwright() as runtime:
    folder=Path(folder);extension=folder/'extension';extension.mkdir()
    (extension/'manifest.json').write_text(json.dumps({'manifest_version':3,'name':'Temporary identity zoom',
        'version':'1.0','host_permissions':['http://127.0.0.1/*'],'background':{'service_worker':'worker.js'}}))
    (extension/'worker.js').write_text('chrome.runtime.onInstalled.addListener(() => {});')
    browser=runtime.chromium.launch_persistent_context(str(folder/'profile'),channel='chromium',headless=True,
        args=[f'--disable-extensions-except={extension}',f'--load-extension={extension}'],viewport={'width':1280,'height':1024})
    page=browser.pages[0];zoom=browser.service_workers[0] if browser.service_workers else browser.wait_for_event('serviceworker')
    keys=KeyboardActions(page,OUT/'keyboard.json');visual=VisualChecks(page,OUT/'contrast.json');previews=[]
    page.on('pageerror',lambda e:result['errors'].append(str(e)))
    page.on('request',lambda r:result['requests'].append([r.method,r.url]))
    page.on('response',lambda r:previews.append(r.json()) if r.url.endswith('/rollback-preview') and r.status==200 else None)
    source=page.locator('#rollback-body > details').filter(has=page.get_by_text('Applied revision and affected proposals',exact=True))
    summary=source.locator(':scope > summary')

    def capture(name):
        # History refresh can replace this subtree. Capture preparation and
        # character geometry use one current-DOM invocation, not a stale handle.
        ids=page.evaluate('''()=>{
            const source=document.querySelector('#rollback-body > details[data-review-key^="rollback-source:"]');
            source.scrollIntoView({block:'center'});
            return ('''+GEOMETRY+''')([...source.querySelectorAll('p.id')]);}''')
        expected=[previews[-1]['source']['application_id']]+[m['proposal_id'] for m in previews[-1]['source']['affected_members']]
        assert [item['text'] for item in ids]==expected
        result['states'].append({'name':name,'identities':ids})
        page.screenshot(path=str(OUT/(name+'.png')))
        (OUT/(name+'.html')).write_text(page.content())
        assert all(not item['outside'] and item['scrollWidth']<=item['clientWidth']+1 for item in ids),ids
        assert all(item['characters']==len(item['text']) for item in ids),ids
        visual.check(name)

    try:
        page.goto('http://127.0.0.1:8876/#/review/rollback/'+info['proposals'][0]);page.wait_for_load_state('networkidle')
        expect(page.locator('#rollback-submit')).to_be_enabled()
        page.screenshot(path=str(OUT/'reconnaissance.png'))
        assert previews[-1]['ready']
        keys.activate(summary);expect(source).to_have_attribute('open','');expect(summary).to_be_focused()
        # Exercise the actual periodic reader separately for each native control.
        page.set_viewport_size({'width':640,'height':1024})
        inverse=page.locator('#rollback-body > details').filter(has=page.get_by_text('Full inverse change',exact=True))
        focus_checks=[]
        for name,control in [('source',summary),('inverse',inverse.locator(':scope > summary')),
                             ('inverse-text',inverse.locator('pre'))]:
            keys.reach(control)
            if name=='inverse-text':
                page.keyboard.press('ArrowRight')
                page.wait_for_function('()=>document.querySelector("#rollback-body pre.review-card__diff").scrollLeft>=40')
                before_scroll=control.evaluate('el=>[el.scrollLeft,el.scrollTop]')
            page.evaluate('async()=>{await (await import("/app.js")).loadOperationHistory();}')
            focus_checks.append({'control':name,'retained':control.evaluate('el=>el===document.activeElement')})
            if name=='inverse-text':
                result['reader_scroll']={'before':before_scroll,'after':control.evaluate('el=>[el.scrollLeft,el.scrollTop]')}
        result['reader_focus']=focus_checks
        assert all(item['retained'] for item in focus_checks),focus_checks
        assert result['reader_scroll']['after']==result['reader_scroll']['before'],result['reader_scroll']
        for width in (1280,1440,640):
            page.set_viewport_size({'width':width,'height':1024})
            for theme in ('light','dark'):
                keys.theme(theme);keys.reach(summary);capture(f'{theme}-{width}')
        for width in (1280,1440):
            page.set_viewport_size({'width':width,'height':1024})
            receipt=zoom.evaluate('''async()=>{const [tab]=await chrome.tabs.query({active:true,currentWindow:true});
                await chrome.tabs.setZoom(tab.id,2);return chrome.tabs.getZoom(tab.id);}''')
            assert receipt==2
            for theme in ('light','dark'):
                keys.theme(theme);keys.reach(summary);capture(f'{theme}-{width}-native-200-percent')
            zoom.evaluate('''async()=>{const [tab]=await chrome.tabs.query({active:true,currentWindow:true});await chrome.tabs.setZoom(tab.id,1);}''')
        keys.activate(summary,'Space');expect(source).not_to_have_attribute('open','');expect(summary).to_be_focused()
        keys.activate(summary);expect(source).to_have_attribute('open','');expect(summary).to_be_focused()
        revision=previews[-1]['revision']
        keys.activate(page.locator('#rollback-refresh'));page.wait_for_load_state('networkidle')
        expect(source).to_have_attribute('open','')
        expect(page.locator('#rollback-title')).to_be_focused()
        assert previews[-1]['revision']==revision
        capture('refreshed-complete-source')
        assert not result['errors'],result['errors']
        assert all(method=='GET' and url.startswith('http://127.0.0.1:8876/') for method,url in result['requests'])
        with sqlite3.connect('file:'+info['db']+'?mode=ro',uri=True) as db:
            assert list(db.iterdump())==info['database']
        assert Path(info['target']).read_bytes()==info['current'].encode()
        assert subprocess.check_output(['git','rev-parse','HEAD'],cwd=info['snapshots'],text=True).strip()==info['head']
        visual.assert_clean();result.update(models=0,store_unchanged=True,target_unchanged=True,snapshot_unchanged=True)
        print('ROLLBACK_IDENTITIES_OK',len(result['states']),'states;',len(result['requests']),'GETs; complete identities; unchanged database/target/snapshot; zero models')
    finally:
        keys.save();(OUT/'result.json').write_text(json.dumps(result,indent=2));browser.close()
