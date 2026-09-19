"""Record seven-screen keyboard/read recovery and computed text contrast evidence."""
from pathlib import Path
from urllib.parse import quote
import hashlib
import json
import sqlite3
import sys
from playwright.sync_api import sync_playwright, expect

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'reports/dashboard-parity/accessibility'
manifest=json.loads((OUT/'manifest.json').read_text())
assert Path(manifest['db']).parent.parent.name.startswith('si-accessibility-ui-')
BASE='http://127.0.0.1:8876/'
ROUTES=[('overview','#/overview','#view-overview'),('rules','#/rules/demo-rule-1','#view-rules'),
        ('review','#/review','#view-review'),('projects','#/projects','#view-projects'),
        ('project-detail','#/projects/'+quote('demo:example-evals',safe=''),'#project-detail'),
        ('evals','#/evals','#view-evals'),('run-detail','#/overview/run/demo-run-11','#run-detail')]
result={'status':'attempted','empty':manifest['empty'],'screens':[],'states':[],
        'findings':[],'errors':[],'requests':[],'source_hashes':manifest['source_hashes']}
TAG='empty' if manifest['empty'] else 'populated'
DEST=OUT/(TAG+'-result.json')
def save():DEST.write_text(json.dumps(result,indent=2)+'\n')
def finding(kind,screen,detail):result['findings'].append({'kind':kind,'screen':screen,'detail':detail});save()

from visual_checks import CONTRAST


def tab_to(page, selector, limit=350):
    target=page.locator(selector)
    for count in range(limit):
        if target.evaluate('(el)=>el===document.activeElement'):
            return count
        page.keyboard.press('Tab')
    raise AssertionError('Tab could not reach '+selector)


def capture(page,name):
    page.screenshot(path=str(OUT/(name+'.png')))
    (OUT/(name+'.html')).write_text(page.content())


def keyboard(page,name):
    # Start with real Tab traversal even if a detail view chose initial focus.
    tabs=tab_to(page,'#skip-to-main')
    expect(page.locator('#skip-to-main')).to_be_focused()
    page.keyboard.press('Enter');expect(page.locator('#main')).to_be_focused()
    page.keyboard.press('PageDown')
    if page.locator('#main').evaluate('e=>e.scrollHeight>e.clientHeight+20'):
        page.wait_for_function('document.getElementById("main").scrollTop>0')
    page.keyboard.press('Control+Home')
    page.keyboard.press('Control+k');expect(page.locator('#navigation-input')).to_be_focused()
    page.keyboard.press('Escape');expect(page.locator('#main')).to_be_focused()
    page.keyboard.press('Tab')
    focus=page.evaluate('''()=>{const e=document.activeElement,s=getComputedStyle(e),r=e.getBoundingClientRect();
      return {tag:e.tagName,id:e.id,text:e.textContent.slice(0,120),outline:s.outlineStyle,width:s.outlineWidth,
              color:s.outlineColor,visible:r.bottom>0&&r.top<innerHeight&&r.right>0&&r.left<innerWidth};}''')
    assert focus['visible'],focus
    assert focus['outline']!='none' and float(focus['width'].removesuffix('px'))>0,focus
    capture(page,name+'-keyboard')
    return {'tabs_to_skip':tabs,'after_skip':focus}


with sync_playwright() as runtime:
    browser=runtime.chromium.launch(headless=True)
    try:
        for name,route,selector in ROUTES:
            context=browser.new_context(viewport={'width':1440,'height':1024})
            page=context.new_page()
            page.on('pageerror',lambda error:result['errors'].append(str(error)))
            page.on('request',lambda req:result['requests'].append([req.method,req.url]))
            try:
                page.goto(BASE+route);page.wait_for_load_state('networkidle')
                expect(page.locator(selector)).to_be_visible()
                capture(page,TAG+'-'+name+'-recon')
                print('RECON',TAG,name,page.get_by_role('button').all_text_contents()[:12],flush=True)
                row={'screen':name}
                try:row['keyboard']=keyboard(page,TAG+'-'+name)
                except AssertionError as error:finding('keyboard',name,str(error));capture(page,TAG+'-'+name+'-keyboard-failure')
                result['screens'].append(row)
                for width in [1280,1440]:
                    page.set_viewport_size({'width':width,'height':1024})
                    for theme in ['light','dark']:
                        if page.evaluate('async()=>(await import("/app.js")).effectiveTheme()')!=theme:
                            tab_to(page,'#theme-toggle');page.keyboard.press('Enter')
                        assert page.evaluate('async()=>(await import("/app.js")).effectiveTheme()')==theme
                        page.locator('#main').evaluate('e=>e.scrollTop=0')
                        check=page.evaluate(CONTRAST)
                        row.setdefault('contrast',[]).append({'width':width,'theme':theme,**check})
                        capture(page,TAG+'-'+name+'-'+theme+'-'+str(width))
                        if check['failed']:finding('contrast',name,{'width':width,'theme':theme,'failures':check['failed']})
                save()
            finally:context.close()
        if not manifest['empty']:
            # A held real response is loading, never a made-up successful payload.
            context=browser.new_context();page=context.new_page();held=[]
            page.on('pageerror',lambda error:result['errors'].append(str(error)))
            page.on('request',lambda req:result['requests'].append([req.method,req.url]))
            page.route('**/api/overview',lambda route:held.append(route))
            page.goto(BASE+'#/overview',wait_until='domcontentloaded')
            expect(page.locator('#ov-statusline')).to_contain_text('Loading')
            capture(page,'overview-initial-loading')
            assert held
            held.pop().continue_();page.wait_for_load_state('networkidle')
            expect(page.locator('#ov-statusline')).not_to_contain_text('Loading')
            result['states'].append({'screen':'overview','state':'held initial response','status':'succeeded'})
            context.close()
            # One shared read can fail while other current data remains visible.
            for name,route,selector in ROUTES:
                context=browser.new_context();page=context.new_page()
                page.on('pageerror',lambda error:result['errors'].append(str(error)))
                page.on('request',lambda req:result['requests'].append([req.method,req.url]))
                page.route('**/api/overview',lambda route:route.fulfill(status=503,json={'detail':'Invented initial read unavailable'}))
                page.goto(BASE+route);page.wait_for_load_state('networkidle')
                expect(page.locator('#global-error')).to_contain_text('Invented initial read unavailable')
                expect(page.locator('#nav-freshness')).not_to_contain_text('Loading')
                if name=='overview':
                    expect(page.locator('#ov-statusline')).not_to_contain_text('Loading')
                    assert page.locator('#ov-grid .loading').count()==0
                    for width in (1280,1440):
                        page.set_viewport_size({'width':width,'height':1024})
                        for theme in ('light','dark'):
                            if page.evaluate('async()=>(await import("/app.js")).effectiveTheme()')!=theme:
                                tab_to(page,'#theme-toggle');page.keyboard.press('Enter')
                            capture(page,f'overview-initial-failure-{theme}-{width}')
                            check=page.evaluate(CONTRAST)
                            result['states'].append({'screen':'overview','state':'initial failure',
                                'width':width,'theme':theme,'contrast':check,'status':'succeeded'})
                            if check['failed']:finding('contrast','overview initial failure',check['failed'])
                capture(page,name+'-initial-read-failure')
                retry=page.locator('#global-error button')
                state={'screen':name,'state':'initial shared read recovery','status':'failed'}
                if retry.count()!=1:
                    finding('read-recovery',name,'Shared failure offers no in-page retry; banner only asks to reload the dashboard.')
                else:
                    tab_to(page,'#global-error button');page.keyboard.press('Enter')
                    page.wait_for_load_state('networkidle')
                    expect(page.locator('#global-error')).to_contain_text('Invented initial read unavailable')
                    expect(retry).to_be_focused();expect(retry).to_have_attribute('aria-disabled','false')
                    page.unroute('**/api/overview')
                    held_retry=[]
                    if name=='overview':
                        page.route('**/api/overview',lambda route:held_retry.append(route))
                    page.keyboard.press('Enter')
                    if name=='overview':
                        expect(page.locator('#ov-statusline')).to_contain_text('Loading')
                        assert page.locator('#ov-grid .loading').count()==1
                        capture(page,'overview-held-initial-retry')
                        assert len(held_retry)==1
                        held_retry.pop().continue_()
                    expect(page.locator('#global-error')).to_be_hidden()
                    expect(page.locator('#main')).to_be_focused()
                    assert page.url==BASE+route
                    state['status']='succeeded'
                result['states'].append(state);save();context.close()
            # Failed refresh retains old values and explains their age. Moving
            # focus elsewhere while a successful retry waits must be respected.
            context=browser.new_context();page=context.new_page()
            page.on('pageerror',lambda error:result['errors'].append(str(error)))
            page.on('request',lambda req:result['requests'].append([req.method,req.url]))
            page.goto(BASE+'#/overview');page.wait_for_load_state('networkidle')
            before=page.locator('#ov-statusline').inner_text()
            page.route('**/api/overview',lambda route:route.fulfill(status=503,json={'detail':'Invented refresh unavailable'}))
            tab_to(page,'#ov-refresh');page.keyboard.press('Enter')
            expect(page.locator('#global-error')).to_contain_text('Previously shown data may be out of date')
            expect(page.locator('#ov-statusline')).to_have_text(before)
            expect(page.locator('#ov-read-state')).to_contain_text('previous')
            page.unroute('**/api/overview');held=[]
            page.route('**/api/overview',lambda route:held.append(route))
            tab_to(page,'#global-error button');page.keyboard.press('Enter')
            expect(page.locator('#global-error button')).to_have_attribute('aria-disabled','true')
            assert held
            tab_to(page,'#theme-toggle')
            held.pop().continue_();page.wait_for_load_state('networkidle')
            expect(page.locator('#global-error')).to_be_hidden()
            expect(page.locator('#theme-toggle')).to_be_focused()
            capture(page,'refresh-recovered-without-focus-theft')
            result['states'].append({'screen':'overview','state':'refresh failure, delayed retry, user-moved focus','status':'succeeded'})
            context.close()
        assert not result['errors'],result['errors']
        assert all(method=='GET' and url.startswith(BASE) for method,url in result['requests'])
        with sqlite3.connect('file:'+manifest['db']+'?mode=ro',uri=True) as db:
            assert list(db.iterdump())==manifest['snapshot']
            assert db.execute('SELECT COUNT(*) FROM llm_calls').fetchone()[0]==0
        assert all(Path(p).read_text()==text for p,text in manifest['targets'].items())
        for p,want in manifest['source_hashes'].items():assert hashlib.sha256((ROOT/p).read_bytes()).hexdigest()==want,p
        result.update(unchanged=True,model_calls=0,status='failed' if result['findings'] else 'succeeded');save()
        print('ACCESSIBILITY_AUDIT',TAG,result['status'],'findings',len(result['findings']),flush=True)
    except Exception as error:
        result.update(status='failed',error=type(error).__name__+': '+str(error));save();raise
    finally:browser.close()
sys.exit(1 if result['findings'] else 0)
