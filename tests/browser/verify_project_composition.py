"""Read-only Project composition, native navigation and evidence completeness."""
from pathlib import Path
from urllib.parse import quote, urlencode
import hashlib
import json
import sqlite3

from playwright.sync_api import sync_playwright, expect
from keyboard_actions import KeyboardActions
from visual_checks import VisualChecks

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'reports/dashboard-parity/project-composition'
manifest=json.loads((OUT/'manifest.json').read_text())
assert 'si-project-summary-ui-' in manifest['db']
BASE='http://127.0.0.1:8876/'
PROJECT='#/projects/'+quote(manifest['project_key'],safe='')
FILES=[ROOT/'src/self_improve/dashboard'/name for name in ('project_summary.py','app.py','static/app.js','static/app.css')]
def hashes():return {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in FILES}
before=hashes()
def unchanged():
    with sqlite3.connect('file:'+manifest['db']+'?mode=ro',uri=True) as db:
        assert list(db.iterdump())==manifest['snapshot']
        assert db.execute('SELECT COUNT(*) FROM llm_calls').fetchone()[0]==0
    assert all(Path(path).read_text()==text for path,text in manifest['targets'].items())
    assert hashes()==before

with sync_playwright() as runtime:
    browser=runtime.chromium.launch(headless=True)
    page=browser.new_page(viewport={'width':1440,'height':1024})
    keys=KeyboardActions(page,OUT/'keyboard.json');visual=VisualChecks(page,OUT/'contrast.json')
    requests=[];errors=[];states=[]
    page.on('request',lambda r:requests.append([r.method,r.url]))
    page.on('pageerror',lambda e:errors.append(str(e)))
    def visit(copy='alpha'):
        page.goto(BASE+PROJECT+'?'+urlencode({'working_copy_id':manifest['copies'].get(copy,copy)}))
        page.wait_for_load_state('networkidle')
    def panel(title):return page.locator('.project-primary > .panel').filter(has=page.get_by_role('heading',name=title,exact=True))
    def capture(name,compact=False):
        page.locator('#main').evaluate('e=>e.scrollTop=0')
        visual.check(name);page.screenshot(path=str(OUT/(name+'.png')))
        (OUT/(name+'.html')).write_text(page.content())
        dimensions=page.evaluate('''()=>{
          const main=document.querySelector('#main'),panels=[...document.querySelectorAll('.project-primary > .panel')];
          return {context_top:panels[1].getBoundingClientRect().top,
            rule_height:panels[0].getBoundingClientRect().height,
            overflow:main.scrollWidth>main.clientWidth+1};
        }''')
        assert not dimensions['overflow'],dimensions
        if compact:assert dimensions['context_top']<1024,dimensions
        states.append({'name':name,**dimensions})
    def themes(name,compact=False):
        for width in (1280,1440):
            page.set_viewport_size({'width':width,'height':1024})
            for theme in ('light','dark'):
                keys.theme(theme);capture(f'{name}-{theme}-{width}',compact)
    try:
        health=page.request.get(BASE+'api/health').json()
        assert health['db_path']==manifest['db'] and health['read_only'],health
        unchanged();visit()
        (OUT/'recon.html').write_text(page.content());page.screenshot(path=str(OUT/'recon.png'))
        print('PROJECT_COMPOSITION_RECON',page.get_by_role('heading').all_text_contents(),flush=True)
        rules=panel('Machine-written rules here');context=panel('Configured context and Markdown')
        expect(page.locator('#project-copy')).to_have_count(1)
        expect(page.locator('.project-summary-rule')).to_have_count(6)
        expect(page.locator('.project-tiles .tile').nth(2)).to_contain_text('2')
        expect(rules).to_contain_text('3 retained project revisions · 1 global revisions with checks here · 23 proposed edits')
        expect(rules).to_contain_text('Rolled back · project')
        expect(context).to_contain_text('docs/')
        expect(context).to_contain_text('29 B')
        expect(context.locator('tbody tr')).to_have_count(3)
        assert context.locator('tbody tr').all_text_contents()==[
            '(repository root)Instructions279 B1','docs/Other Markdown29 B1','External / globalInstructions114 B1']
        assert page.locator('.project-primary script').count()==0
        token=page.locator('.project-summary-rule__title').filter(has_text='W'*100)
        def title_width():return token.evaluate('''e=>{const r=document.createRange();r.selectNodeContents(e);return {text:r.getBoundingClientRect().width,available:e.parentElement.clientWidth}}''')
        wrapped=title_width();assert wrapped['text']<=wrapped['available']+1,wrapped
        style=page.add_style_tag(content='.project-summary-rule{overflow-wrap:normal}.project-summary-rule__title{display:inline;overflow:visible;-webkit-line-clamp:unset}')
        red=title_width();assert red['text']>red['available']+1,red
        style.evaluate('e=>e.remove()');assert title_width()==wrapped
        themes('populated',compact=True)
        # Preserve complete historical content and scope under native disclosures.
        evidence=page.locator('.project-summary-rule > details > summary').first
        keys.activate(evidence)
        expect(page.locator('.project-summary-rule').first).to_contain_text('claude')
        expect(page.locator('.project-summary-rule pre').first).to_contain_text('<!-- si:')
        record=page.locator('.project-summary-rule').first.get_by_text('Application, observations and lifecycle',exact=True)
        keys.activate(record)
        expect(page.locator('.project-summary-rule').first).to_contain_text('application_event_id')
        visual.themes(keys,'complete-revision',OUT,target=record)
        keys.activate(record);keys.activate(evidence)
        # More than one complete page remains available independently of previews.
        all_proposals=page.get_by_text('Inspect all proposed edits',exact=True)
        keys.activate(all_proposals)
        expect(page.locator('#project-status-proposals')).to_have_text('20 shown · 23 retained')
        keys.activate(page.locator('#project-older-proposals'))
        expect(page.locator('#project-status-proposals')).to_have_text('23 shown · 23 retained')
        expect(page.locator('#project-status-proposals')).to_be_focused()
        expect(panel('Proposed edits').locator('.project-record')).to_have_count(23)
        visual.check('all-proposals');keys.activate(all_proposals)
        all_deliveries=page.get_by_text('Inspect all retained deliveries',exact=True)
        keys.activate(all_deliveries)
        expect(panel('Recorded deliveries').locator('.project-record')).to_have_count(3)
        keys.activate(all_deliveries)
        # Type-ahead operates the actual select; selection survives browser history.
        keys.choose(page.locator('#project-copy'),manifest['copies']['beta'])
        page.wait_for_load_state('networkidle')
        expect(page.locator('.project-tiles .tile').nth(2).locator('.tile__value')).to_have_text('0')
        expect(rules).to_contain_text('Content not found')
        page.reload();page.wait_for_load_state('networkidle')
        expect(page.locator('#project-copy')).to_have_value(manifest['copies']['beta'])
        keys.choose(page.locator('#project-copy'),manifest['copies']['alpha']);page.wait_for_load_state('networkidle')
        page.go_back();page.wait_for_load_state('networkidle')
        expect(page.locator('#project-copy')).to_have_value(manifest['copies']['beta'])
        page.go_forward();page.wait_for_load_state('networkidle')
        expect(page.locator('#project-copy')).to_have_value(manifest['copies']['alpha'])
        expect(page.locator('.project-tiles .tile').nth(2).locator('.tile__value')).to_have_text('2')
        # A summary refresh must not detach unrelated navigation during native use.
        nav_handle=page.locator('#project-section-instructions').element_handle()
        keys.activate(page.locator('#project-rules-refresh'));page.wait_for_load_state('networkidle')
        assert nav_handle.evaluate('e=>e.isConnected'), 'Rule summary refresh detached Project navigation'
        # Delayed completion must preserve focus inside the summary as well.
        held=[]
        page.route('**/api/project-summary?*',lambda r:held.append(r))
        keys.activate(page.locator('#project-rules-refresh'))
        expect(rules.get_by_role('status')).to_contain_text('Reading retained rule summary')
        coverage=rules.get_by_text('Copy, times and coverage',exact=True)
        keys.activate(coverage)
        assert held
        held.pop().fulfill(response=page.request.get(BASE+'api/project-summary?'+urlencode({'project_key':manifest['project_key'],'working_copy_id':manifest['copies']['alpha']})))
        expect(rules.get_by_role('status')).to_have_count(0)
        expect(coverage).to_be_focused()
        keys.activate(coverage)
        for target in (page.locator('.project-summary-rule__title').first,
                       page.locator('.project-summary-rule > details > summary').first,
                       page.locator('#project-summary-observations')):
            keys.activate(page.locator('#project-rules-refresh'))
            expect(rules.get_by_role('status')).to_contain_text('Reading retained rule summary')
            keys.reach(target)
            assert held
            held.pop().fulfill(response=page.request.get(BASE+'api/project-summary?'+urlencode({'project_key':manifest['project_key'],'working_copy_id':manifest['copies']['alpha']})))
            expect(rules.get_by_role('status')).to_have_count(0)
            expect(target).to_be_focused()
        keys.activate(page.locator('#project-rules-refresh'))
        expect(rules.get_by_role('status')).to_contain_text('Reading retained rule summary')
        keys.reach(page.locator('.project-summary-rule__title').first)
        moved=page.request.get(BASE+'api/project-summary?'+urlencode({'project_key':manifest['project_key'],'working_copy_id':manifest['copies']['alpha']})).json()
        moved['deliveries']['records'].pop(0);moved['deliveries']['omitted']+=1
        held.pop().fulfill(json=moved)
        expect(rules.get_by_role('status')).to_have_count(0)
        expect(page.locator('#project-rules-refresh')).to_be_focused()
        page.unroute('**/api/project-summary?*')
        keys.activate(page.locator('#project-rules-refresh'));page.wait_for_load_state('networkidle')
        # A failed refresh keeps the last response, observation time and retry.
        page.route('**/api/project-summary?*',lambda r:r.fulfill(status=503,json={'detail':'Invented rule summary failure'}))
        keys.activate(page.locator('#project-rules-refresh'));page.wait_for_load_state('networkidle')
        expect(rules.get_by_role('alert')).to_contain_text('Last successful summary remains below')
        expect(rules).to_contain_text('2030-01-06 00:00:00 UTC')
        expect(page.locator('.project-tiles .tile').nth(2).locator('.tile__value')).to_have_text('2')
        themes('failed-refresh')
        page.unroute('**/api/project-summary?*')
        keys.activate(page.locator('#project-rules-refresh'));page.wait_for_load_state('networkidle')
        expect(rules.get_by_role('alert')).to_have_count(0)
        expect(page.locator('#project-rules-refresh')).to_be_focused()
        link=page.get_by_role('link',name='Complete configured context, measurements and history',exact=True)
        keys.activate(link);page.wait_for_load_state('networkidle')
        assert 'tab=context' in page.url and manifest['copies']['alpha'] in page.url
        expect(page.locator('#project-context-status')).to_contain_text('2 shown · 2 observation times')
        visit('f'*64)
        expect(page.locator('#project-copy')).to_have_value('f'*64)
        expect(page.locator('.project-tiles .tile').nth(2).locator('.tile__value')).to_have_text('Unknown')
        expect(rules).to_contain_text('No check for this exact revision and copy')
        expect(context).to_contain_text('No unambiguous inventory')
        themes('missing-copy')
        # Explicit renderer-only coverage; database-backed empty/conflict tests are separate.
        visit()
        data=page.request.get(BASE+'api/project-summary?'+urlencode({'project_key':manifest['project_key'],'working_copy_id':manifest['copies']['alpha']})).json()
        for empty in (True,False):
            substitute=json.loads(json.dumps(data))
            for counts in substitute['counts'].values():
                counts.update(retained=0 if empty else None,matched=0 if empty else None,known_matches=0,uncertain=0)
            substitute['deliveries'].update(records=[],count=0 if empty else None,reason='' if empty else 'schema_unavailable',omitted=0)
            substitute['proposals'].update(records=[],count=0,omitted=0)
            substitute['observation_times']=[]
            page.route('**/api/project-summary?*',lambda r:r.fulfill(json=substitute))
            keys.activate(page.locator('#project-rules-refresh'));page.wait_for_load_state('networkidle')
            expect(page.locator('.project-tiles .tile').nth(2).locator('.tile__value')).to_have_text('0' if empty else 'Unknown')
            expect(rules).to_contain_text('No delivery revisions' if empty else 'schema is unavailable')
            themes('empty' if empty else 'unavailable')
            page.unroute('**/api/project-summary?*')
        unchanged();visual.assert_clean()
        assert not errors,errors
        assert all(method=='GET' and url.startswith(BASE) for method,url in requests),requests
        result={'states':states,'requests':len(requests),'models':0,'errors':errors,'source_hashes':before,
                'data_and_targets_unchanged':True,'title_negative_control':{'wrapped':wrapped,'unwrapped':red},
                'scope':'Real populated/missing-copy reads, separate empty/unavailable renderer fixtures; no runtime receipt or production claim.'}
        (OUT/'result.json').write_text(json.dumps(result,indent=2))
        print('PROJECT_COMPOSITION_OK',json.dumps({'states':len(states),'requests':len(requests),'models':0}),flush=True)
    except BaseException:
        page.screenshot(path=str(OUT/'failure.png'));(OUT/'failure.html').write_text(page.content())
        raise
    finally:
        keys.save();browser.close()
