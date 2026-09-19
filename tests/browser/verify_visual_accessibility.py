"""Read-only rendered-accessibility diagnostic; candidates require visual review.

Run through with_server.py against accessibility_demo.py (also --empty).
Only a temporary Chromium profile loads the generated loopback-only zoom helper.
Exit 0 means the diagnostic completed, not that candidates or WCAG checks passed.
"""
from pathlib import Path
from urllib.parse import quote
import argparse
import base64
import hashlib
import json
import sqlite3
import tempfile

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[2]
BASE = 'http://127.0.0.1:8876/'
OUT = ROOT / 'reports/dashboard-parity/visual-accessibility'
ROUTES = {
    'overview': ('#/overview', '#view-overview', '#ov-refresh'),
    'rules': ('#/rules/demo-rule-1', '#view-rules', '#rules-search-input'),
    'review': ('#/review', '#view-review', '[data-decision="approve"]'),
    'projects': ('#/projects', '#view-projects', '#projects-search'),
    'project-detail': ('#/projects/' + quote('demo:example-evals', safe=''), '#project-detail', '.project-sections a'),
    'evals': ('#/evals', '#view-evals', '.policy-evidence'),
    'run-detail': ('#/overview/run/demo-run-11', '#run-detail', '[data-run-refresh]'),
}
MODES = ['baseline', 'zoom-125', 'zoom-150', 'zoom-175', 'zoom-200', 'text-spacing', 'forced-colors']
SPACING = '''* {line-height:1.5 !important;letter-spacing:.12em !important;word-spacing:.16em !important;}
 p {margin-bottom:2em !important;}'''

# The browser supplies these computed values. Every heuristic remains a candidate,
# with its exact element/context retained for inspection; no guessed CSS pass.
OBSERVE = r'''() => {
 const modal=document.querySelector('dialog[open]');
 const visible=e=>e.checkVisibility({checkVisibilityCSS:true,checkOpacity:true}) && e.getClientRects().length && !e.closest('.visually-hidden') && (!modal||modal.contains(e));
 const sel=e=>e.id?'#'+CSS.escape(e.id):e.parentElement?sel(e.parentElement)+' > '+e.tagName.toLowerCase()+':nth-child('+([...e.parentElement.children].indexOf(e)+1)+')':e.tagName.toLowerCase();
 const rect=e=>{const r=e.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height,right:r.right,bottom:r.bottom};};
 const describe=e=>({selector:sel(e),tag:e.tagName,text:(e.getAttribute('aria-label')||e.innerText||e.getAttribute('placeholder')||'').trim().slice(0,240),rect:rect(e)});
 const rgba=s=>{const m=s.match(/^rgba?\(([^)]+)\)$/);if(!m)return null;const v=m[1].split(/[, /]+/).filter(Boolean).map(Number);return [v[0],v[1],v[2],v[3]??1];};
 const over=(a,b)=>a.slice(0,3).map((v,i)=>v*a[3]+b[i]*(1-a[3]));
 const lum=c=>c.slice(0,3).map(v=>{v/=255;return v<=.04045?v/12.92:((v+.055)/1.055)**2.4;}).reduce((a,v,i)=>a+v*[.2126,.7152,.0722][i],0);
 const ratio=(a,b)=>(Math.max(lum(a),lum(b))+.05)/(Math.min(lum(a),lum(b))+.05);
 const bg=e=>{const chain=[];for(let p=e;p;p=p.parentElement)chain.unshift(p);let c=[255,255,255];for(const p of chain){const s=getComputedStyle(p),a=rgba(s.backgroundColor);if(!a||s.backgroundImage!=='none'||Number(s.opacity)!==1)return null;c=over(a,c);}return c;};
 const contrast=(color,background)=>{const c=rgba(color);return c&&background?ratio(over(c,background),background):null;};
 if(ratio([0,0,0],[255,255,255])!==21)throw Error('Non-text contrast control failed');
 const controls=[...document.querySelectorAll('button,a[href],input,select,textarea,summary,[role="switch"],[role="tab"]')].filter(visible);
 const targets=controls.map(e=>{const d=describe(e),s=getComputedStyle(e),r=d.rect;
   const label=e.matches('input')&&e.labels?.length?[...e.labels].map(describe):[];
   const textParent=e.parentElement?.closest('p,li,dd');
   return {...d,disabled:e.matches(':disabled,[aria-disabled="true"]'),inline_candidate:!!(textParent&&getComputedStyle(e).display==='inline'&&textParent.textContent.trim()!==e.textContent.trim()),
     native_candidate:e.matches('input,select,textarea'),label_targets:label,small:r.width<24||r.height<24,
     outline:s.outline,background:s.backgroundColor,color:s.color,border:s.borderColor,shadow:s.boxShadow,font:s.fontSize,line_height:s.lineHeight};});
 const circleRect=(a,b)=>{const x=a.x+a.width/2,y=a.y+a.height/2;const dx=Math.max(b.x-x,0,x-b.right),dy=Math.max(b.y-y,0,y-b.bottom);return dx*dx+dy*dy<144-0.01;};
 const tooClose=(a,b)=>{const dx=a.x+a.width/2-b.x-b.width/2,dy=a.y+a.height/2-b.y-b.height/2;return dx*dx+dy*dy<576-0.01;};
 targets.forEach((t,i)=>{if(!t.small||t.disabled)return;t.neighbors=targets.filter((u,j)=>i!==j&&!u.disabled&&!controls[i].contains(controls[j])&&!controls[j].contains(controls[i]) && (circleRect(t.rect,u.rect)||(u.small&&tooClose(t.rect,u.rect)))).map(u=>({selector:u.selector,text:u.text}));});
 const clips=[];
 for(const e of document.querySelectorAll('body *')){if(!visible(e)||e.matches('script,style,svg,path,option'))continue;const s=getComputedStyle(e),x=e.scrollWidth>e.clientWidth+2,y=e.scrollHeight>e.clientHeight+2;
   if((x&&['hidden','clip'].includes(s.overflowX))||(y&&['hidden','clip'].includes(s.overflowY))){
     const r=rect(e),children=[...e.querySelectorAll('button,input,select,a[href],summary,h1,h2,h3,p,pre,span,code,td,label')].filter(visible).filter(c=>{
       const q=rect(c);if(q.right<=r.right+2&&q.x>=r.x-2)return false;
       for(let p=c.parentElement;p&&p!==e;p=p.parentElement)if(['auto','scroll'].includes(getComputedStyle(p).overflowX))return false;
       return true;}).map(describe);
     clips.push({...describe(e),overflow:[s.overflowX,s.overflowY],scroll:[e.scrollWidth,e.scrollHeight],client:[e.clientWidth,e.clientHeight],ellipsis:s.textOverflow==='ellipsis',line_clamp:s.webkitLineClamp,overflow_children:children});}}
 const indicators=[];
 for(const e of document.querySelectorAll('input:not([type="checkbox"]),select,textarea,.search,.cell,.swatch,[aria-current="page"],[aria-pressed="true"],[data-selected="true"],.project-expand,.rules-family button,.policy-evidence,.projects-table th[aria-sort] button,#inspector-close,.infotip__btn')){
   if(!visible(e))continue;const s=getComputedStyle(e),before=getComputedStyle(e,'::before'),after=getComputedStyle(e,'::after');
   indicators.push({...describe(e),kind:e.matches('.cell,.swatch')?'state-symbol':e.matches('input,select,textarea,.search')?'input-boundary':e.matches('.project-expand,.rules-family button,.policy-evidence,.projects-table th[aria-sort] button,#inspector-close,.infotip__btn')?'control-glyph':'selected-state',
     disabled:e.matches(':disabled'),background:s.backgroundColor,color:s.color,border:s.borderColor,border_width:s.borderWidth,border_style:s.borderStyle,
     background_rgb:bg(e),outside_background_rgb:bg(e.parentElement),focused:e===document.activeElement,
     outline_style:s.outlineStyle,outline_width:s.outlineWidth,outline_offset:s.outlineOffset,outline_contrast:contrast(s.outlineColor,bg(e)),
     border_vs_inside:contrast(s.borderTopColor,bg(e)),border_vs_outside:contrast(s.borderTopColor,bg(e.parentElement)),
     symbol:before.content,symbol_display:before.display,symbol_color:before.color,
     symbol_contrast:!['none','normal','""'].includes(before.content)&&before.display!=='none'?contrast(before.color,bg(e)):null,
     after:{content:after.content,display:after.display,border:after.borderColor,border_width:after.borderWidth,background_image:after.backgroundImage,border_top_vs_fill:contrast(after.borderTopColor,bg(e))},text_contrast:contrast(s.color,bg(e)),
     images:[...e.querySelectorAll('img')].map(img=>({src:img.getAttribute('src'),filter:getComputedStyle(img).filter,rect:rect(img)}))});
 }
 const e=document.activeElement,s=getComputedStyle(e),parent=e.closest('.search');
 const focus={...describe(e),outline_style:s.outlineStyle,outline_width:s.outlineWidth,outline_color:s.outlineColor,outline_contrast:contrast(s.outlineColor,bg(e.parentElement)),
   parent:parent?{...describe(parent),border:getComputedStyle(parent).borderColor,shadow:getComputedStyle(parent).boxShadow,
     outline_style:getComputedStyle(parent).outlineStyle,outline_width:getComputedStyle(parent).outlineWidth,
     outline_contrast:contrast(getComputedStyle(parent).outlineColor,bg(parent.parentElement))}:null};
 return {viewport:{width:innerWidth,height:innerHeight,dpr:devicePixelRatio,visual_scale:visualViewport.scale,document_width:document.documentElement.scrollWidth},
   title:[...document.querySelectorAll('h1')].filter(visible).map(e=>({...describe(e),font:getComputedStyle(e).fontSize})),
   controls:targets,clipping_candidates:clips,indicators,focus,forced:matchMedia('(forced-colors:active)').matches};
}'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--screens', nargs='+', choices=list(ROUTES), default=list(ROUTES))
    parser.add_argument('--widths', nargs='+', type=int, choices=[1280, 1440], default=[1280, 1440])
    parser.add_argument('--themes', nargs='+', choices=['light', 'dark'], default=['light', 'dark'])
    parser.add_argument('--modes', nargs='+', choices=MODES, default=MODES)
    parser.add_argument('--assert-fixes', action='store_true', help='Require the bounded regressions exercised by this run to pass; candidates remain diagnostic.')
    parser.add_argument('--inspect-controls', action='store_true', help='Capture unfocused inputs, below-fold Evals coverage and actual indicator pixels.')
    parser.add_argument('--fixture', choices=['accessibility','evidence'], default='accessibility')
    args = parser.parse_args()
    routes = dict(ROUTES)
    if args.fixture=='evidence':
        assert args.screens==['rules'], 'The evidence fixture adapter is Rules-only'
        manifest=json.loads((ROOT/'reports/dashboard-parity/evidence-browser-manifest.json').read_text())
        assert 'si-evidence-ui-' in str(Path(manifest['db']).resolve())
        manifest['empty']=False
        manifest['source_hashes']={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (ROOT/'src/self_improve').rglob('*') if p.suffix in {'.py','.js','.css','.html','.svg'}}
        routes['rules']=('#/rules/l00','#view-rules','#rules-search-input')
        tag='evidence'
    else:
        manifest = json.loads((ROOT / 'reports/dashboard-parity/accessibility/manifest.json').read_text())
        assert Path(manifest['db']).parent.parent.name.startswith('si-accessibility-ui-')
        tag = 'empty' if manifest['empty'] else 'populated'
    out = OUT / tag
    out.mkdir(parents=True, exist_ok=True)
    result = {'status':'attempted', 'scope':vars(args), 'empty':manifest['empty'], 'states':[],
              'requests':[], 'errors':[], 'candidates':[], 'regressions':[], 'source_hashes':manifest['source_hashes'],
              'limitations':['Candidates require visual/semantic review; this is not a conformance result.',
                'Forced colors are Chromium emulation, not an OS or assistive-technology session.',
                'SVG/filter, native indicator and complex background contrast need separate inspection.']}

    def save():
        (out / 'result.json').write_text(json.dumps(result, indent=2) + '\n')

    with tempfile.TemporaryDirectory(prefix='si-a11y-browser-') as folder, sync_playwright() as runtime:
        temporary = Path(folder)
        extension = temporary / 'extension'
        extension.mkdir()
        (extension / 'manifest.json').write_text(json.dumps({'manifest_version':3, 'name':'Temporary fixture zoom',
            'version':'1.0', 'host_permissions':['http://127.0.0.1/*'], 'background':{'service_worker':'worker.js'}}))
        (extension / 'worker.js').write_text('chrome.runtime.onInstalled.addListener(() => {});')
        context = runtime.chromium.launch_persistent_context(str(temporary / 'profile'), channel='chromium', headless=True,
            args=[f'--disable-extensions-except={extension}', f'--load-extension={extension}'], viewport={'width':1280,'height':1024})
        try:
            worker = context.service_workers[0] if context.service_workers else context.wait_for_event('serviceworker')
            page = context.pages[0]
            page.set_default_timeout(10000)
            page.on('pageerror', lambda error:result['errors'].append(str(error)))
            page.on('request', lambda req:result['requests'].append([req.method,req.url]))

            def zoom(factor):
                receipt = worker.evaluate('''async factor=>{
                    const tabs=await chrome.tabs.query({url:'http://127.0.0.1:8876/*'});
                    if(tabs.length!==1)throw Error('Expected exactly one fixture tab');
                    await chrome.tabs.setZoom(tabs[0].id,factor);
                    return {factor:await chrome.tabs.getZoom(tabs[0].id),url:tabs[0].url};
                }''', factor)
                assert abs(receipt['factor']-factor)<0.001, receipt
                return receipt

            def reach(target):
                expect(target).to_be_visible()
                expect(target).to_be_enabled()
                for count in range(250):
                    if target.evaluate('e=>e===document.activeElement'):
                        return count
                    page.keyboard.press('Tab')
                raise AssertionError('Native Tab could not reach '+str(target))

            def activate(target):
                reach(target)
                page.keyboard.press('Enter')
                page.wait_for_load_state('networkidle')

            def capture(key):
                page.screenshot(path=str(out / (key+'.png')))
                (out / (key+'.html')).write_text(page.content())

            def pixels(locator, key, crop=None):
                """Measure an actual browser PNG, retaining colors rather than guessing a glyph."""
                inspection_scroll=None
                if crop=='family':
                    # A wide focused row can leave its leading icon outside the table
                    # scrollport. Keep that observation separate from graphic contrast.
                    inspection_scroll={'before':locator.evaluate('e=>({x:e.getBoundingClientRect().x,scrollLeft:e.closest("#rules-table-wrap").scrollLeft})')}
                    locator.evaluate('e=>e.scrollIntoView({block:"center",inline:"start",behavior:"instant"})')
                    inspection_scroll['after']=locator.evaluate('e=>({x:e.getBoundingClientRect().x,scrollLeft:e.closest("#rules-table-wrap").scrollLeft})')
                locator.scroll_into_view_if_needed()
                box=locator.bounding_box()
                if crop=='sort':
                    box={'x':box['x']+box['width']-7,'y':box['y']+box['height']/2-3,'width':7,'height':6}
                elif crop=='family':
                    box={'x':box['x']+14,'y':box['y']+box['height']/2-4,'width':8,'height':8}
                    assert locator.evaluate('(e,r)=>e.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2))',box),box
                # Tab zoom changes CSS coordinates; screenshot clips do not apply that
                # transform. Crop the actual full viewport PNG in physical pixels.
                measured=page.evaluate('''async ({encoded,box})=>{
                    const image=new Image();image.src='data:image/png;base64,'+encoded;await image.decode();
                    const dpr=devicePixelRatio;
                    if(Math.abs(image.width-innerWidth*dpr)>2)throw Error('Unexpected screenshot scale');
                    const x=Math.max(0,Math.floor(box.x*dpr)),y=Math.max(0,Math.floor(box.y*dpr));
                    const right=Math.min(image.width,Math.ceil((box.x+box.width)*dpr)),bottom=Math.min(image.height,Math.ceil((box.y+box.height)*dpr));
                    if(right<=x||bottom<=y)throw Error('Indicator is outside the viewport');
                    const c=document.createElement('canvas');c.width=right-x;c.height=bottom-y;
                    const ctx=c.getContext('2d');ctx.drawImage(image,x,y,c.width,c.height,0,0,c.width,c.height);
                    const data=ctx.getImageData(0,0,c.width,c.height).data;
                    const counts=new Map();for(let i=0;i<data.length;i+=4){const k=Array.from(data.slice(i,i+4)).join(',');counts.set(k,(counts.get(k)||0)+1);}
                    return {width:c.width,height:c.height,dpr,css_rect:box,pixel_rect:{x,y,right,bottom},viewport_png:{width:image.width,height:image.height},
                        png:c.toDataURL('image/png').split(',')[1],
                        colors:[...counts].sort((a,b)=>b[1]-a[1]).slice(0,12).map(([color,count])=>({rgba:color.split(',').map(Number),count}))};
                }''',{'encoded':base64.b64encode(page.screenshot()).decode(),'box':box})
                (out/(key+'.png')).write_bytes(base64.b64decode(measured.pop('png')))
                if inspection_scroll is not None:measured['inspection_scroll']=inspection_scroll
                return measured

            for name in args.screens:
                route, view, focus_selector = routes[name]
                for width in args.widths:
                    page.set_viewport_size({'width':width,'height':1024})
                    for theme in args.themes:
                        for mode in args.modes:
                            key=f'{name}-{theme}-{width}-{mode}'
                            page.emulate_media(forced_colors='none', color_scheme=theme)
                            # Force a fresh document, even when only a hash changed. Each
                            # mode must start with the same disclosure/selection state.
                            page.goto('about:blank')
                            page.goto(BASE+route)
                            page.wait_for_load_state('networkidle')
                            zoom(1)
                            expect(page.locator(view)).to_be_visible()
                            if page.evaluate('async()=>(await import("/app.js")).effectiveTheme()')!=theme:
                                activate(page.locator('#theme-toggle'))
                            capture(key+'-recon')
                            # Read-only disclosures expose content; no policy or decision activation.
                            if not manifest['empty']:
                                if name=='rules':
                                    activate(page.locator('#rules-inspector-host').get_by_role('link',name='Evidence',exact=True))
                                elif name=='review':
                                    activate(page.locator('[data-review-individual]'))
                                    disclosure=page.locator('.review-targets > summary').first
                                    if disclosure.count():activate(disclosure)
                                elif name=='project-detail':
                                    activate(page.locator('.project-sections a').nth(1))
                                elif name=='evals':
                                    activate(page.locator('.policy-evidence').first)
                            if name=='overview':activate(page.locator('.overview-grid-foot details > summary'))
                            before = page.evaluate(OBSERVE)
                            receipt = zoom(int(mode.split('-')[1])/100 if mode.startswith('zoom-') else 1)
                            if mode=='text-spacing':page.add_style_tag(content=SPACING)
                            if mode=='forced-colors':page.emulate_media(forced_colors='active', color_scheme=theme)
                            # Wait for measured browser layout, never use a timing sleep as evidence.
                            factor=receipt['factor']
                            page.wait_for_function('factor=>Math.abs(devicePixelRatio-factor)<.02',arg=factor)
                            measured = page.evaluate(OBSERVE)
                            assert abs(measured['viewport']['width']*factor-width)<=2, (key,measured['viewport'],receipt)
                            assert measured['forced']==(mode=='forced-colors')
                            page.locator('#main').evaluate('e=>e.scrollTop=0')
                            capture(key+'-top')
                            target=page.locator(focus_selector).first
                            if not target.count() or not target.is_enabled():target=page.locator('#theme-toggle')
                            before_focus=target.evaluate('''e=>{const s=getComputedStyle(e),p=e.closest('.search');return {
                                outline:s.outline,border:s.borderColor,shadow:s.boxShadow,
                                parent:p?{border:getComputedStyle(p).borderColor,shadow:getComputedStyle(p).boxShadow}:null};}''')
                            tabs=reach(target)
                            observed=page.evaluate(OBSERVE)
                            capture(key)
                            state={'key':key,'zoom_receipt':receipt,'baseline_viewport':before['viewport'],
                                   'focus_tabs':tabs,'before_focus':before_focus,'initial_indicators':measured['indicators'],**observed}
                            if name=='projects':
                                bounds=page.locator('#main').bounding_box()
                                toolbar=[t for t in observed['controls'] if t['selector'] in ['#projects-search','#projects-clear','#projects-refresh'] or t['selector'].startswith('#projects-search-form > button')]
                                assert len(toolbar)==4, toolbar
                                result['regressions'].append({'id':'VA-01','state':key,'passed':all(t['rect']['x']>=bounds['x'] and t['rect']['right']<=bounds['x']+bounds['width'] for t in toolbar)})
                            if name=='rules' and mode=='forced-colors':
                                f=observed['focus']; parent=f['parent']
                                result['regressions'].append({'id':'VA-02','state':key,'passed':bool(parent and parent['outline_style']!='none' and float(parent['outline_width'].removesuffix('px'))>=2 and parent['outline_contrast']>=3)})
                                categories={}
                                for category,selector,expected in [('navigation','.nav-item[aria-current="page"]',1),('filters','.rules-chip[aria-pressed="true"]',2),('rows','.rules-member[data-selected="true"]',0 if manifest['empty'] else 1)]:
                                    selected=page.locator(selector).evaluate_all('''nodes=>nodes.filter(e=>e.checkVisibility()).map(e=>{const s=getComputedStyle(e);return {style:s.outlineStyle,width:parseFloat(s.outlineWidth),offset:parseFloat(s.outlineOffset)};})''')
                                    categories[category]={'expected':expected,'observed':selected}
                                result['regressions'].append({'id':'VA-03','state':key,'passed':all(len(c['observed'])==c['expected'] and all(i['style']!='none' and i['width']>=1 and i['offset']<0 for i in c['observed']) for c in categories.values()),'categories':categories})
                            if name=='review' and mode=='zoom-200' and width==1280 and not manifest['empty']:
                                page.locator('.review-row__meta').first.scroll_into_view_if_needed()
                                capture(key+'-next-up')
                                target.scroll_into_view_if_needed()
                            if name=='review' and not manifest['empty']:
                                paths=page.locator('.review-row__meta > code').evaluate_all('''nodes=>nodes.map(e=>{const r=e.getBoundingClientRect(),p=e.parentElement.getBoundingClientRect();return {left:r.left,right:r.right,parent_left:p.left,parent_right:p.right};})''')
                                assert paths, paths
                                result['regressions'].append({'id':'VA-04','state':key,'passed':all(p['left']>=p['parent_left'] and p['right']<=p['parent_right']+1 for p in paths),'paths':paths})
                                summaries=page.locator('.review-member > .review-disclose > summary').evaluate_all('nodes=>nodes.map(e=>e.getBoundingClientRect().height)')
                                assert summaries, summaries
                                result['regressions'].append({'id':'VA-05','state':key,'passed':all(h>=24 for h in summaries),'heights':summaries})
                            if name=='rules' and not manifest['empty']:
                                diagnosis=page.locator('#inspector-body > nav + p > a').bounding_box()
                                assert diagnosis, diagnosis
                                result['regressions'].append({'id':'VA-06','state':key,'passed':diagnosis['height']>=24,'height':diagnosis['height']})
                            for candidate in observed['clipping_candidates']:
                                result['candidates'].append({'state':key,'kind':'clipping','detail':candidate})
                            for target_info in observed['controls']:
                                if target_info.get('neighbors') and not target_info['inline_candidate']:
                                    result['candidates'].append({'state':key,'kind':'target-spacing','detail':target_info})
                            for indicator in observed['indicators']:
                                value=indicator['symbol_contrast'] if indicator['kind']=='state-symbol' else indicator['border_vs_outside'] if indicator['kind']=='input-boundary' else None
                                if value is not None and value<3 and not indicator['disabled']:
                                    result['candidates'].append({'state':key,'kind':'non-text-contrast','detail':indicator})
                            f=observed['focus']
                            if (f['outline_style']=='none' or float(f['outline_width'].removesuffix('px'))==0) and not (f['parent'] and (f['parent']['shadow']!='none' or f['parent']['outline_style']!='none')):
                                result['candidates'].append({'state':key,'kind':'focus-indicator','detail':f})
                            if name=='rules' and mode=='forced-colors':
                                state['current_control_focus']=[]
                                for i,current in enumerate(page.locator('.nav-item[aria-current="page"],.rules-chip[aria-pressed="true"]').all()):
                                    reach(current)
                                    focused=page.evaluate(OBSERVE)['focus']
                                    state['current_control_focus'].append(focused)
                                    capture(key+f'-current-focused-{i}')
                                result['regressions'].append({'id':'VA-03-focus','state':key,'passed':all(f['outline_style']=='dashed' and float(f['outline_width'].removesuffix('px'))>=2 and f['outline_contrast']>=3 for f in state['current_control_focus'])})
                                reach(target)
                            if args.inspect_controls:
                                # Move away through Tab to inspect the text field without its focus cue.
                                page.keyboard.press('Tab')
                                state['unfocused']=page.evaluate(OBSERVE)
                                capture(key+'-unfocused')
                                if name=='evals':
                                    coverage=page.locator('#run-disclosure-quality-class-coverage')
                                    reach(coverage)
                                    state['coverage_focus']=page.evaluate(OBSERVE)
                                    capture(key+'-coverage')
                                    summary=next(c for c in state['coverage_focus']['controls'] if c['selector']=='#run-disclosure-quality-class-coverage')
                                    result['regressions'].append({'id':'VA-07','state':key,'passed':not summary['small'] or not summary.get('neighbors'),'target':summary})
                                    words=page.locator('.eval-tools summary').evaluate_all(r'''nodes=>nodes.flatMap(e=>{
                                        const walker=document.createTreeWalker(e,NodeFilter.SHOW_TEXT),words=[];
                                        while(walker.nextNode()){const n=walker.currentNode;
                                            for(const m of n.textContent.matchAll(/\b(?:All|observed|projects|through|Detector|Human|assessment|history|retained|samples)\b/g)){
                                                const r=document.createRange();r.setStart(n,m.index);r.setEnd(n,m.index+m[0].length);
                                                words.push({word:m[0],rects:[...r.getClientRects()].map(r=>({x:r.x,y:r.y,width:r.width,height:r.height}))});
                                            }}return words;})''')
                                    assert words, words
                                    result['regressions'].append({'id':'VA-08','state':key,'passed':all(len(w['rects'])==1 for w in words),'words':words})
                                if name=='rules' and args.fixture=='evidence':
                                    family=page.locator('.rules-family button').first
                                    initial=family.get_attribute('aria-expanded')
                                    state['family_indicators']=[]
                                    state['family_native_focus']=[]
                                    for expanded in ['false','true']:
                                        if family.get_attribute('aria-expanded')!=expanded:activate(family)
                                        expect(family).to_have_attribute('aria-expanded',expanded)
                                        reach(family)
                                        native=family.evaluate('''e=>{
                                            const r=e.getBoundingClientRect(),label=e.querySelector('strong').getBoundingClientRect(),wrap=e.closest('#rules-table-wrap'),clip=wrap.getBoundingClientRect();
                                            return {focused:e===document.activeElement,expanded:e.getAttribute('aria-expanded'),scrollLeft:wrap.scrollLeft,
                                                marker:{left:r.left+14,right:r.left+22},label:{left:label.left,right:label.right},clip:{left:clip.left,right:clip.right}};
                                        }''')
                                        state['family_native_focus'].append(native)
                                        capture(key+'-family-'+expanded+'-native-focus')
                                        measured=family.evaluate(r'''async e=>{
                                            const s=getComputedStyle(e,'::before'),img=e.querySelector('img');
                                            let color,kind,transform;
                                            if(s.content!=='none'&&s.maskImage!=='none'){
                                                color=s.backgroundColor;kind='mask';transform=s.transform;
                                            }else{
                                                if(!img.checkVisibility()||getComputedStyle(img).filter!=='none')throw Error('Unsupported family image');
                                                const source=await (await fetch(img.src)).text(),doc=new DOMParser().parseFromString(source,'image/svg+xml');
                                                const paths=[...doc.querySelectorAll('path')];
                                                if(paths.length!==1||!/^#[0-9a-f]{6}$/i.test(paths[0].getAttribute('fill')))throw Error('Unsupported family SVG');
                                                color=paths[0].getAttribute('fill');kind='svg';transform=getComputedStyle(img).transform;
                                            }
                                            const c=document.createElement('canvas').getContext('2d');c.fillStyle=color;c.fillRect(0,0,1,1);
                                            return {kind,foreground:[...c.getImageData(0,0,1,1).data].slice(0,3),transform};
                                        }''')
                                        observation=next(i for i in page.evaluate(OBSERVE)['indicators'] if i['selector']=='#'+family.get_attribute('id'))
                                        measured['background']=observation['background_rgb']
                                        measured['expanded']=expanded
                                        def luminance(rgb):
                                            channels=[v/255/12.92 if v/255<=.04045 else ((v/255+.055)/1.055)**2.4 for v in rgb]
                                            return sum(c*w for c,w in zip(channels,[.2126,.7152,.0722]))
                                        a,b=sorted([luminance(measured['foreground']),luminance(measured['background'])])
                                        measured['contrast']=(b+.05)/(a+.05)
                                        # The first eight-pixel glyph occupies the existing leading icon slot.
                                        measured['pixels']=pixels(family,key+'-family-'+expanded,crop='family')
                                        state['family_indicators'].append(measured)
                                        capture(key+'-family-'+expanded+'-context')
                                    if family.get_attribute('aria-expanded')!=initial:activate(family)
                                    result['regressions'].append({'id':'VA-10','state':key,'passed':all(i['focused'] and all(i['clip']['left']-1<=i[k]['left'] and i[k]['right']<=i['clip']['right']+1 for k in ['marker','label']) for i in state['family_native_focus']),'focus':state['family_native_focus']})
                                    result['regressions'].append({'id':'VA-09','state':key,'passed':all(i['contrast']>=3 and any(c['rgba'][:3]==i['foreground'] for c in i['pixels']['colors']) for i in state['family_indicators']) and state['family_indicators'][0]['transform']!=state['family_indicators'][1]['transform'],'indicators':state['family_indicators']})
                                state['indicator_pixels']=[]
                                image_selectors=['.project-expand img','.rules-family button img','.policy-evidence img','#inspector-close','.infotip__btn','select','.projects-table th[aria-sort] button','.cell[data-hidden-runs]']
                                for selector in image_selectors:
                                    for i,element in enumerate(page.locator(selector).all()):
                                        if not element.is_visible():continue
                                        state['indicator_pixels'].append({'selector':selector,'index':i,'rendered':pixels(element,key+f'-indicator-{len(state["indicator_pixels"])}','sort' if selector.endswith('th[aria-sort] button') else None)})
                                reach(target)
                            # The native modal remains usable under the same zoom/spacing/color mode.
                            page.keyboard.press('Control+k')
                            expect(page.locator('#navigation-input')).to_be_focused()
                            expect(page.locator('#navigation-dialog')).to_be_visible()
                            page.keyboard.type('Rules')
                            expect(page.locator('#navigation-input')).to_have_value('Rules')
                            state['palette']=page.evaluate(OBSERVE)
                            for indicator in state['palette']['indicators']:
                                if indicator['kind']=='input-boundary' and indicator['border_vs_outside'] is not None and indicator['border_vs_outside']<3:
                                    result['candidates'].append({'state':key+'-palette','kind':'non-text-contrast','detail':indicator})
                            for candidate in state['palette']['clipping_candidates']:
                                result['candidates'].append({'state':key+'-palette','kind':'clipping','detail':candidate})
                            capture(key+'-palette')
                            if args.inspect_controls:
                                page.keyboard.press('Tab')
                                assert not page.locator('#navigation-input').evaluate('e=>e===document.activeElement')
                                state['palette_unfocused']=page.evaluate(OBSERVE)
                                capture(key+'-palette-unfocused')
                            page.keyboard.press('Escape')
                            expect(target).to_be_focused()
                            result['states'].append(state)
                            save()
                            print('VISUAL_ACCESSIBILITY_STATE',tag,key,'viewport',observed['viewport'],'candidates',len(result['candidates']),flush=True)
            assert not result['errors'], result['errors']
            assert all(method=='GET' and url.startswith(BASE) for method,url in result['requests'])
            with sqlite3.connect('file:'+manifest['db']+'?mode=ro',uri=True) as db:
                assert list(db.iterdump())==manifest['snapshot']
                assert db.execute('SELECT COUNT(*) FROM llm_calls').fetchone()[0]==0
            assert all(Path(p).read_text()==text for p,text in manifest['targets'].items())
            for path,expected in manifest['source_hashes'].items():
                assert hashlib.sha256((ROOT/path).read_bytes()).hexdigest()==expected,path
            result.update(status='diagnostic_complete',unchanged=True,model_calls=0)
            save()
            failures=[r for r in result['regressions'] if not r['passed']]
            print('BOUNDED_ACCESSIBILITY_REGRESSIONS',json.dumps({'checks':len(result['regressions']),'failures':failures}),flush=True)
            if args.assert_fixes:
                assert not failures, failures
            print('VISUAL_ACCESSIBILITY_DIAGNOSTIC_COMPLETE',tag,'states',len(result['states']),
                  'candidates',len(result['candidates']),'GETs',len(result['requests']),'models 0; unchanged Store/targets/source',flush=True)
        except BaseException as error:
            result.update(status='failed',error=type(error).__name__+': '+str(error))
            if 'page' in locals():
                page.screenshot(path=str(out/'failure.png'))
                (out/'failure.html').write_text(page.content())
            save()
            raise
        finally:
            context.close()


if __name__=='__main__':
    main()
