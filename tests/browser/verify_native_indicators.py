"""Read-only native/control graphic diagnostic using invented temporary fixtures.

Run with accessibility_demo.py, or --fixture overview with overview_demo.py.
Exit zero proves completion/data invariants, not overall accessibility acceptance.
"""
from pathlib import Path
import argparse
import base64
import copy
import hashlib
import json
import sqlite3
import xml.etree.ElementTree as ET

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[2]
BASE = 'http://127.0.0.1:8876/'
OUT = ROOT / 'reports/dashboard-parity/native-indicators'
ROUTES = {
    'rules': ('#/rules', '#rules-sort'),
    'projects': ('#/projects', '#projects-table-wrap'),
    'evals': ('#/evals', '#policy-evidence-global'),
    'quality': ('#/evals/quality/new/global', '#quality-size'),
    'overview': ('#/overview', '#ov-grid'),
}

MEASURE = r'''e=>{
 const rgba=s=>{const m=s.match(/^rgba?\(([^)]+)\)$/);if(!m)return null;const c=m[1].split(/[, /]+/).filter(Boolean).map(Number);return [c[0],c[1],c[2],c[3]??1];};
 const over=(a,b)=>a.slice(0,3).map((v,i)=>v*a[3]+b[i]*(1-a[3]));
 const bg=e=>{const chain=[];for(let p=e;p;p=p.parentElement)chain.unshift(p);let c=[255,255,255];for(const p of chain){const s=getComputedStyle(p),a=rgba(s.backgroundColor);if(!a||s.backgroundImage!=='none'||Number(s.opacity)!==1)return null;c=over(a,c);}return c;};
 const rect=e=>{const r=e.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height};};
 const style=p=>{const s=getComputedStyle(e,p);return {color:s.color,background:s.backgroundColor,background_image:s.backgroundImage,content:s.content,
   border_top:s.borderTopColor,border_left:s.borderLeftColor,border_width:s.borderWidth,
   border_top_width:s.borderTopWidth,border_right_width:s.borderRightWidth,border_left_width:s.borderLeftWidth,
   transform:s.transform,display:s.display,width:s.width,height:s.height,filter:s.filter};};
 const s=getComputedStyle(e);
 return {id:e.id,tag:e.tagName,type:e.type,role:e.getAttribute('role'),label:e.getAttribute('aria-label'),text:e.textContent.trim(),
   rect:rect(e),background:bg(e),outside_background:bg(e.parentElement),style:style(null),before:style('::before'),after:style('::after'),marker:style('::marker'),
   focused:e===document.activeElement,hovered:e.matches(':hover'),checked:e.checked,open:e.parentElement?.tagName==='DETAILS'?e.parentElement.open:undefined,
   expanded:e.getAttribute('aria-expanded'),sorted:e.closest('th')?.getAttribute('aria-sort'),value:e.value,appearance:s.appearance,color_scheme:s.colorScheme,accent:s.accentColor,
   cell_state:e.getAttribute('data-state'),hidden_runs:e.getAttribute('data-hidden-runs'),title:e.getAttribute('title'),href:e.querySelector('a')?.getAttribute('href'),
   images:[...e.querySelectorAll('img')].map(i=>({src:i.getAttribute('src'),filter:getComputedStyle(i).filter,transform:getComputedStyle(i).transform,rect:rect(i)}))};
}'''


def contrast(a, b):
    def lum(c):
        v=[n/255/12.92 if n/255<=.04045 else ((n/255+.055)/1.055)**2.4 for n in c]
        return sum(n*w for n,w in zip(v,[.2126,.7152,.0722]))
    lo,hi=sorted([lum(a),lum(b)])
    return (hi+.05)/(lo+.05)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture', choices=['accessibility','overview'], default='accessibility')
    parser.add_argument('--screens', nargs='+', choices=list(ROUTES))
    parser.add_argument('--widths', nargs='+', type=int, choices=[1280,1440], default=[1280,1440])
    parser.add_argument('--themes', nargs='+', choices=['light','dark'], default=['light','dark'])
    parser.add_argument('--modes', nargs='+', choices=['ordinary','forced'], default=['ordinary','forced'])
    parser.add_argument('--assert-fixes', action='store_true', help='Require measured Project expansion arrows to remain visible at 3:1 or better.')
    args=parser.parse_args()
    screens=args.screens or (['overview'] if args.fixture=='overview' else ['rules','projects','evals','quality'])
    if args.fixture=='overview':
        assert screens==['overview']
        manifest=json.loads((ROOT/'reports/dashboard-parity/overview-manifest.json').read_text())
        assert '/si-overview-ui-' in str(Path(manifest['db']).resolve())
        tag='overview'
    else:
        assert 'overview' not in screens
        manifest=json.loads((ROOT/'reports/dashboard-parity/accessibility/manifest.json').read_text())
        assert Path(manifest['db']).parent.parent.name.startswith('si-accessibility-ui-')
        tag='empty' if manifest['empty'] else 'populated'
    source_hashes={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
                   for p in (ROOT/'src/self_improve').rglob('*') if p.suffix in {'.py','.js','.css','.html','.svg'}}
    svg=ET.fromstring((ROOT/'src/self_improve/dashboard/static/icons/project-expand.svg').read_text())
    paths=svg.findall('{http://www.w3.org/2000/svg}path');assert len(paths)==1
    fill=paths[0].attrib['fill'];assert fill.startswith('#') and len(fill)==7
    image_foreground=[int(fill[i:i+2],16) for i in [1,3,5]]
    out=OUT/tag;out.mkdir(parents=True,exist_ok=True)
    result={'status':'attempted','fixture':tag,'scope':vars(args),'source_hashes':source_hashes,
            'states':[],'requests':[],'errors':[],'candidates':[],'regressions':[],
            'limits':['Chromium emulation is not native OS or assistive acceptance.',
                      'Native popup internals and antialiased colors are not normative source-color measurements.',
                      'Corner source-pixel presence does not establish sufficient recognizable marker geometry; textured contrast remains unmeasured.',
                      'Mixed Overview responses are renderer fixtures, not retained backend measurements.'],
            'exclusions':['Policy switch image/filter repeats On/Off/Manual text.',
                          'Project identity dots, numeric bars and notice dots have visible text equivalents.']}

    def save():
        (out/'result.json').write_text(json.dumps(result,indent=2)+'\n')

    with sync_playwright() as runtime:
        browser=runtime.chromium.launch(headless=True)
        context=browser.new_context(viewport={'width':1280,'height':1024})
        page=context.new_page();page.set_default_timeout(10000)
        page.on('request',lambda req:result['requests'].append([req.method,req.url]))
        page.on('pageerror',lambda error:result['errors'].append(str(error)))
        try:
            if args.fixture=='overview':
                response=context.request.get(BASE+'api/overview');assert response.ok
                result['requests'].append(['GET',BASE+'api/overview'])
                original=response.json();mixed=copy.deepcopy(original)
                column=next(c for c in mixed['grid']['columns'] if c['run_count']==2)
                column['cells']['scan'].update(state='ok',states={'ok':1,'abandoned':1},runs=2,runs_without_record=1)
                column['cells']['mine'].update(state='partial',states={'partial':1,'running':1},runs=2,runs_without_record=1)
                (out/'retained-overview-response.json').write_text(json.dumps(original,indent=2)+'\n')
                (out/'mixed-rendering-response.json').write_text(json.dumps(mixed,indent=2)+'\n')
                result['mixed_case']={'night':column['night'],'run_ids':column['run_ids'],'scope':'Response-only invented rendering, preserves original link identities.'}
                page.route('**/api/overview',lambda route:route.fulfill(json=mixed))

            def capture(key):
                page.screenshot(path=str(out/(key+'.png')))
                (out/(key+'.html')).write_text(page.content())

            def reach(target):
                expect(target).to_be_visible();expect(target).to_be_enabled()
                for _ in range(250):
                    if target.evaluate('e=>e===document.activeElement'):return
                    page.keyboard.press('Tab')
                raise AssertionError('Native Tab did not reach '+str(target))

            def activate(target, key='Enter'):
                reach(target);page.keyboard.press(key);page.wait_for_load_state('networkidle')

            def sample(target,key,kind='native',glyph=None):
                target.evaluate('e=>e.scrollIntoView({block:"center",inline:"nearest",behavior:"instant"})')
                crop=glyph or target
                crop.scroll_into_view_if_needed()
                observed=target.evaluate(MEASURE)
                crop_rect=crop.bounding_box()
                png=page.screenshot()
                suppressed=restored=None
                if kind=='hidden-marker':
                    style=page.add_style_tag(content='.cell[data-hidden-runs]::after { content: none !important; }')
                    try:
                        assert target.evaluate(MEASURE)['rect']==observed['rect']
                        suppressed=page.screenshot()
                        (out/(key+'-marker-suppressed.png')).write_bytes(suppressed)
                    finally:
                        style.evaluate('e=>e.remove()')
                    assert target.evaluate(MEASURE)['rect']==observed['rect']
                    restored=page.screenshot()
                region=None
                if kind=='sort-image':
                    assert observed['after']['width']=='7px' and observed['after']['height']=='6px'
                    region={'x':observed['rect']['width']-7,'y':observed['rect']['height']/2-3,'width':7,'height':6}
                elif kind=='hidden-marker':
                    number=lambda value:float(value.removesuffix('px'))
                    width=number(observed['after']['border_left_width'])
                    height=number(observed['after']['border_top_width'])
                    assert width==6 and height==6
                    region={'x':observed['rect']['width']-number(observed['style']['border_right_width'])-width,
                            'y':number(observed['style']['border_top_width']),'width':width,'height':height}
                observed['pixel_region']=region
                observed['pixels']=page.evaluate('''async ({raw,region,rect,crop,suppressed,restored})=>{
                    const image=new Image();image.src='data:image/png;base64,'+raw;await image.decode();
                    const c=document.createElement('canvas');c.width=image.width;c.height=image.height;
                    const ctx=c.getContext('2d');ctx.drawImage(image,0,0);const bytes=ctx.getImageData(0,0,c.width,c.height).data,counts=new Map();
                    const scale=devicePixelRatio;
                    if(c.width!==Math.round(innerWidth*scale)||c.height!==Math.round(innerHeight*scale))throw Error('Unexpected viewport PNG scale');
                    const bounds=r=>({x:Math.floor(r.x*scale),y:Math.floor(r.y*scale),right:Math.ceil((r.x+r.width)*scale),bottom:Math.ceil((r.y+r.height)*scale)});
                    const box=bounds(crop);
                    if(box.x<0||box.y<0||box.right>c.width||box.bottom>c.height)throw Error('Control crop outside viewport');
                    const cut=b=>{const d=document.createElement('canvas');d.width=b.right-b.x;d.height=b.bottom-b.y;
                        d.getContext('2d').drawImage(c,b.x,b.y,d.width,d.height,0,0,d.width,d.height);return d.toDataURL('image/png').split(',')[1];};
                    const decode=async raw=>{if(!raw)return null;const i=new Image();i.src='data:image/png;base64,'+raw;await i.decode();
                        if(i.width!==c.width||i.height!==c.height)throw Error('Changed PNG dimensions');
                        const d=document.createElement('canvas');d.width=c.width;d.height=c.height;const dc=d.getContext('2d');dc.drawImage(i,0,0);return dc.getImageData(0,0,d.width,d.height).data;};
                    const without=await decode(suppressed),again=await decode(restored),changedCounts=new Map();
                    let inspected=0,changed=0,restoreMismatch=0;
                    for(let y=box.y;y<box.bottom;y++)for(let x=box.x;x<box.right;x++){
                        if(region){const u=(x+.5)/scale-rect.x-region.x,v=(y+.5)/scale-rect.y-region.y;
                            if(u<0||v<0||u>=region.width||v>=region.height)continue;
                        }
                        const i=(y*c.width+x)*4,key=Array.from(bytes.slice(i,i+4)).join(',');counts.set(key,(counts.get(key)||0)+1);inspected++;
                        if(without&&bytes.slice(i,i+4).some((v,j)=>v!==without[i+j])){changed++;changedCounts.set(key,(changedCounts.get(key)||0)+1);}
                        if(again&&bytes.slice(i,i+4).some((v,j)=>v!==again[i+j]))restoreMismatch++;
                    }
                    const indicator=region?cut(bounds({...region,x:rect.x+region.x,y:rect.y+region.y})):null;
                    return {width:box.right-box.x,height:box.bottom-box.y,device_pixel_ratio:scale,physical_crop:box,
                        inspected_pixels:inspected,control_png:cut(box),indicator_png:indicator,
                        changed_pixels:suppressed?changed:null,restored_mismatch_pixels:restored?restoreMismatch:null,
                        changed_colors:[...changedCounts].map(([s,count])=>({rgba:s.split(',').map(Number),count})),
                        sampling:suppressed?'corner; marker-only suppression comparison':region?'indicator rectangle only':'complete control image',
                        colors:[...counts].sort((a,b)=>b[1]-a[1]).slice(0,24).map(([s,count])=>({rgba:s.split(',').map(Number),count}))};
                }''',{'raw':base64.b64encode(png).decode(),'region':region,
                      'rect':observed['rect'],'crop':crop_rect,
                      'suppressed':base64.b64encode(suppressed).decode() if suppressed else None,
                      'restored':base64.b64encode(restored).decode() if restored else None})
                (out/(key+'-control.png')).write_bytes(base64.b64decode(observed['pixels'].pop('control_png')))
                indicator=observed['pixels'].pop('indicator_png')
                if indicator:(out/(key+'-indicator.png')).write_bytes(base64.b64decode(indicator))
                assert observed['pixels']['inspected_pixels']>0
                if kind=='hidden-marker':assert observed['pixels']['restored_mismatch_pixels']==0
                observed.update(key=key,kind=kind)
                if kind in ['project-image','sort-image','policy-image']:
                    assert all(i['filter']=='none' for i in observed['images'])
                    if kind=='sort-image':assert '/icons/project-expand.svg' in observed['after']['background_image']
                    else:assert len(observed['images'])==1 and observed['images'][0]['src']=='/icons/project-expand.svg'
                    # This single-path SVG's declared fill; actual solid pixels must agree.
                    observed['foreground']=image_foreground
                elif kind=='hidden-marker':
                    observed['foreground']=target.evaluate('''e=>{
                        const c=document.createElement('canvas').getContext('2d');c.fillStyle=getComputedStyle(e,'::after').borderTopColor;c.fillRect(0,0,1,1);
                        return [...c.getImageData(0,0,1,1).data].slice(0,3);
                    }''')
                if 'foreground' in observed:
                    pixels=observed['pixels']['changed_colors' if kind=='hidden-marker' else 'colors']
                    observed['solid_pixel_present']=any(p['rgba'][:3]==observed['foreground'] for p in pixels)
                    observed['contrast']=contrast(observed['foreground'],observed['background']) if observed['background'] is not None else None
                    if observed['contrast'] is None or observed['contrast']<3 or not observed['solid_pixel_present']:
                        result['candidates'].append({'key':key,'contrast':observed['contrast'],'solid_pixel_present':observed['solid_pixel_present'],'kind':kind})
                    if kind=='project-image':
                        result['regressions'].append({'id':'VA-11','key':key,'contrast':observed['contrast'],
                            'pass':observed['contrast'] is not None and observed['contrast']>=3 and observed['solid_pixel_present']})
                capture(key)
                return observed

            def native_states(target,key):
                page.mouse.move(5,5)
                reach(target)
                for _ in range(12):
                    page.keyboard.press('Tab')
                    if not target.evaluate('e=>e===document.activeElement'):break
                assert not target.evaluate('e=>e===document.activeElement'), 'Native Tab did not leave '+str(target)
                values=[sample(target,key+'-unfocused')]
                assert not values[-1]['focused']
                reach(target);values.append(sample(target,key+'-focused'))
                assert values[-1]['focused']
                target.hover();values.append(sample(target,key+'-hovered'))
                return values

            for screen in screens:
                route,ready=ROUTES[screen]
                for width in args.widths:
                    page.set_viewport_size({'width':width,'height':1024})
                    for theme in args.themes:
                        for mode in args.modes:
                            key=f'{screen}-{theme}-{width}-{mode}'
                            page.emulate_media(color_scheme=theme,forced_colors='none')
                            page.goto('about:blank');page.goto(BASE+route);page.wait_for_load_state('networkidle')
                            expect(page.locator(ready)).to_be_visible()
                            if page.evaluate('async()=>(await import("/app.js")).effectiveTheme()')!=theme:
                                activate(page.locator('#theme-toggle'))
                            if mode=='forced':page.emulate_media(color_scheme=theme,forced_colors='active')
                            capture(key+'-recon')
                            state={'key':key,'observations':[]};items=state['observations']
                            if screen=='rules':
                                items.extend(native_states(page.locator('#rules-sort'),key+'-select'))
                                checkbox=page.locator('#rules-group-toggle')
                                for checked in [False,True]:
                                    if checkbox.is_checked()!=checked:activate(checkbox,'Space')
                                    expect(checkbox).to_be_checked(checked=checked)
                                    items.extend(native_states(checkbox,key+'-checkbox-'+str(checked)))
                            elif screen=='projects':
                                if not manifest.get('empty'):
                                    expand=page.locator('.project-expand').first
                                    for value in ['false','true']:
                                        if expand.get_attribute('aria-expanded')!=value:activate(expand)
                                        expect(expand).to_have_attribute('aria-expanded',value)
                                        reach(expand);page.mouse.move(5,5)
                                        items.append(sample(expand,key+'-expand-'+value,'project-image',expand.locator('img')))
                                        expand.hover();items.append(sample(expand,key+'-expand-'+value+'-hover','project-image',expand.locator('img')))
                                sort=page.locator('#project-sort-sessions button')
                                for value in ['ascending','descending']:
                                    if sort.locator('..').get_attribute('aria-sort')!=value:activate(sort)
                                    expect(sort.locator('..')).to_have_attribute('aria-sort',value)
                                    page.mouse.move(5,5);items.append(sample(sort,key+'-sort-'+value,'sort-image'))
                            elif screen=='evals':
                                evidence=page.locator('#policy-evidence-global')
                                for value in ['false','true']:
                                    if evidence.get_attribute('aria-expanded')!=value:activate(evidence)
                                    expect(evidence).to_have_attribute('aria-expanded',value)
                                    reach(evidence);page.mouse.move(5,5)
                                    items.append(sample(evidence,key+'-policy-'+value,'policy-image',evidence.locator('img')))
                                summary=page.locator('#trend-options-toggle')
                                for opened in [False,True]:
                                    if summary.evaluate('e=>e.parentElement.open')!=opened:activate(summary)
                                    items.append(sample(summary,key+'-summary-'+str(opened)))
                                for selector in ['#trend-project_key','#trend-compatibility_key','#trend-end_month']:
                                    items.extend(native_states(page.locator(selector),key+'-'+selector[1:]))
                                month=page.locator('#trend-end_month');reach(month)
                                state['unsaved_month_before']=month.input_value()
                                page.keyboard.press('ArrowUp');state['unsaved_month_changed']=month.input_value()
                                assert state['unsaved_month_changed']!=state['unsaved_month_before']
                                page.keyboard.press('ArrowDown');assert month.input_value()==state['unsaved_month_before']
                            elif screen=='quality':
                                size=page.locator('#quality-size');items.extend(native_states(size,key+'-number'))
                                reach(size);initial=size.input_value();page.keyboard.press('ArrowUp')
                                assert int(size.input_value())==int(initial)+1
                                state['unsaved_size_changed']=size.input_value();page.keyboard.press('ArrowDown');assert size.input_value()==initial
                            else:
                                markers=page.locator('.cell[data-hidden-runs]')
                                assert markers.count()==2,markers.count()
                                for i,cell in enumerate(markers.all()):
                                    observed=sample(cell,key+f'-hidden-{i}','hidden-marker')
                                    assert observed['hidden_runs']=='1' and observed['href']
                                    assert 'which this cell' in observed['title']
                                    items.append(observed)
                            result['states'].append(state);save()
                            print('NATIVE_INDICATOR_STATE',tag,key,'observations',len(items),'candidates',len(result['candidates']),flush=True)
            assert not result['errors'],result['errors']
            assert all(method=='GET' and url.startswith(BASE) for method,url in result['requests']),result['requests']
            with sqlite3.connect('file:'+manifest['db']+'?mode=ro',uri=True) as db:
                assert list(db.iterdump())==manifest['snapshot']
                assert db.execute('SELECT COUNT(*) FROM llm_calls').fetchone()[0]==0
            assert all(Path(p).read_text()==text for p,text in manifest['targets'].items())
            assert all(hashlib.sha256((ROOT/p).read_bytes()).hexdigest()==h for p,h in source_hashes.items())
            result.update(status='diagnostic_complete',unchanged=True,models=0);save()
            if args.assert_fixes:
                assert all(r['pass'] for r in result['regressions']), [r for r in result['regressions'] if not r['pass']]
            print('NATIVE_INDICATORS_COMPLETE',tag,'states',len(result['states']),'GETs',len(result['requests']),
                  'candidates',len(result['candidates']),'unchanged Store/targets/source; models0',flush=True)
        except BaseException as error:
            result.update(status='failed',error=type(error).__name__+': '+str(error));save()
            capture('failure');raise
        finally:
            context.close();browser.close()


if __name__=='__main__':
    main()
