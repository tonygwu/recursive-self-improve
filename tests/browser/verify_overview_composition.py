"""Read-only Overview parity on overview_demo.py --deliveries, with no live state."""
from pathlib import Path
import hashlib
import json
import sqlite3

from playwright.sync_api import sync_playwright, expect
from keyboard_actions import KeyboardActions
from visual_checks import VisualChecks

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'reports/dashboard-parity/overview-composition';OUT.mkdir(parents=True,exist_ok=True)
manifest=json.loads((OUT.parent/'overview-manifest.json').read_text())
assert 'si-overview-ui-' in manifest['db'] and len(manifest['completed_commands'])==3
BASE='http://127.0.0.1:8876/'
FILES=[ROOT/'src/self_improve/dashboard'/name for name in ('overview_data.py','static/app.js','static/app.css','static/index.html')]
def hashes():return {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in FILES}
before=hashes()
def unchanged():
    with sqlite3.connect('file:'+manifest['db']+'?mode=ro',uri=True) as db:
        assert list(db.iterdump())==manifest['snapshot']
        assert db.execute('SELECT COUNT(*) FROM llm_calls').fetchone()[0]==0
    assert all(Path(path).read_text()==content for path,content in manifest['targets'].items())
    assert hashes()==before

with sync_playwright() as p:
    browser=p.chromium.launch(headless=True)
    page=browser.new_page(viewport={'width':1440,'height':1024})
    requests=[];errors=[];states=[]
    page.on('request',lambda r:requests.append([r.method,r.url]))
    page.on('pageerror',lambda e:errors.append(str(e)))
    keys=KeyboardActions(page,OUT/'keyboard.json')
    visual=VisualChecks(page,OUT/'contrast.json')
    def goto():
        page.goto(BASE+'#/overview');page.wait_for_load_state('networkidle')
        expect(page.locator('#ov-loop li')).to_have_count(6)
    def capture(name):
        page.locator('#main').evaluate('e=>e.scrollTop=0')
        visual.check(name)
        page.screenshot(path=str(OUT/(name+'.png')))
        (OUT/(name+'.html')).write_text(page.content())
        metrics=page.evaluate('''()=>{
          const box=s=>{const r=document.querySelector(s).getBoundingClientRect();return {top:r.top,bottom:r.bottom,height:r.height}};
          return {loop:box('#ov-loop'),attention:box('.overview-lower'),review:box('#ov-inbox .btn'),failures:box('#ov-failures'),
            overflow:document.querySelector('#view-overview').scrollWidth>document.querySelector('#view-overview').clientWidth+1};
        }''')
        states.append({'name':name,**metrics})
        assert not metrics['overflow'],metrics
        assert metrics['attention']['top']<=650,metrics
        assert metrics['review']['bottom']<=1000,metrics
        return metrics
    try:
        health=page.request.get(BASE+'api/health').json()
        assert health['db_path']==manifest['db'] and health['read_only'],health
        unchanged()
        goto();page.screenshot(path=str(OUT/'recon.png'))
        print('OVERVIEW_COMPOSITION_RECON',page.get_by_role('heading').all_text_contents(),flush=True)
        data=page.request.get(BASE+'api/overview').json()
        delivery=data['audit']['loop']['delivery']
        assert delivery['manual_targets']==3 and len(delivery['recent'])==2
        assert data['audit']['loop']['evaluations']['attempts']==1
        assert data['audit']['loop']['evaluations']['passed']==0
        expect(page.locator('#ov-deliveries .overview-delivery')).to_have_count(2)
        assert 'Changed later' not in page.locator('#ov-deliveries').inner_text()
        assert '2 proposals' in page.locator('#ov-deliveries').inner_text()
        assert '…' in page.locator('#ov-deliveries').inner_text()
        token=page.locator('.overview-delivery__title').filter(has_text='W'*100)
        def title_width():return token.evaluate('''e=>{const r=document.createRange();r.selectNodeContents(e);return {text:r.getBoundingClientRect().width,available:e.parentElement.clientWidth}}''')
        wrapped=title_width();assert wrapped['text']<=wrapped['available']+1,wrapped
        style=page.add_style_tag(content='.overview-delivery{overflow-wrap:normal}.overview-delivery__title{display:inline;overflow:visible;-webkit-line-clamp:unset}')
        red=title_width();assert red['text']>red['available']+1,red
        style.evaluate('e=>e.remove()');assert title_width()==wrapped
        for width in (1280,1440):
            page.set_viewport_size({'width':width,'height':1024})
            for theme in ('light','dark'):
                if page.evaluate('async()=>(await import("/app.js")).effectiveTheme()')!=theme:
                    keys.activate(page.get_by_role('button',name=theme.title()+' theme',exact=True))
                metrics=capture(f'populated-{theme}-{width}')
                assert metrics['failures']['top']<1000,metrics
                coverage=page.locator('.overview-status summary');keys.activate(coverage)
                expect(page.locator('#ov-loop-coverage')).to_contain_text('not one run or conversion rates')
                keys.activate(page.locator('#ov-refresh'));page.wait_for_load_state('networkidle')
                expect(coverage.locator('..')).to_have_attribute('open','')
                visual.check(f'coverage-{theme}-{width}')
                keys.activate(coverage)
        # Exact historical command navigation uses a GET reader and survives reload.
        link=page.locator('#ov-deliveries .overview-delivery__title').first
        cid=delivery['recent'][0]['command_id'];keys.activate(link)
        page.locator('#delivery-command-'+cid).wait_for()
        assert page.url.endswith('/review/command/'+cid)
        page.reload();page.locator('#delivery-command-'+cid).wait_for()
        goto();keys.activate(page.locator('#ov-loop').get_by_role('link',name='Comparable trend Inspect by version →'))
        keys.activate(page.locator('#trend-options-toggle'))
        expect(page.locator('#trends-filter')).to_be_visible()
        # Explicit response states preserve empty versus unavailable populations.
        goto()
        for empty in (True,False):
            substitute=json.loads(json.dumps(data));loop=substitute['audit']['loop']
            loop.update(sessions={'known':0,'unknown_transcripts':0,'indexed_transcripts':0},incidents=0,learnings=0)
            loop['evaluations'].update(attempts=0 if empty else None,passed=0 if empty else None)
            loop['delivery'].update(manual_targets=0 if empty else None,automatic_operations=0 if empty else None,rollback_operations=0 if empty else None,recent=[])
            page.route('**/api/overview',lambda route:route.fulfill(json=substitute))
            for width in (1280,1440):
                page.set_viewport_size({'width':width,'height':1024})
                for theme in ('light','dark'):
                    if page.evaluate('async()=>(await import("/app.js")).effectiveTheme()')!=theme:
                        keys.activate(page.get_by_role('button',name=theme.title()+' theme',exact=True))
                    keys.activate(page.locator('#ov-refresh'));page.wait_for_load_state('networkidle')
                    expect(page.locator('#ov-refresh')).to_be_enabled()
                    expect(page.locator('#ov-deliveries')).to_contain_text('No completed' if empty else 'unavailable')
                    if not empty:
                        expect(page.locator('#ov-loop')).to_contain_text('Unknown')
                        expect(page.locator('#ov-tiles .tile').nth(1)).to_contain_text('Unknown')
                        expect(page.locator('#ov-statusline')).to_contain_text('delivery history unknown')
                    capture(f'{"empty" if empty else "unavailable"}-{theme}-{width}')
            page.unroute('**/api/overview')
        assert not errors,errors
        assert all(method=='GET' and url.startswith(BASE) for method,url in requests),requests
        unchanged();visual.assert_clean()
        result={'states':states,'requests':len(requests),'models':0,'errors':errors,'source_hashes':before,
                'data_and_targets_unchanged':True,'title_negative_control':{'wrapped':wrapped,'unwrapped':red,'restored':True},
                'scope':'Real populated selected Store; empty/unavailable response fixtures are separate renderer checks.'}
        (OUT/'result.json').write_text(json.dumps(result,indent=2))
        print('OVERVIEW_COMPOSITION_OK',json.dumps({'states':len(states),'requests':len(requests),'models':0}),flush=True)
    except Exception:
        page.screenshot(path=str(OUT/'failure.png'),full_page=True)
        (OUT/'failure.html').write_text(page.content())
        raise
    finally:
        keys.save();browser.close()
