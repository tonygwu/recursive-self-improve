"""Read-only real history UI, paging, copy selection and native keyboard recovery."""
from pathlib import Path
from urllib.parse import quote, urlparse, parse_qs, urlencode, urlunparse
import json
import sqlite3
from playwright.sync_api import sync_playwright, expect
from keyboard_actions import KeyboardActions
from visual_checks import VisualChecks

ROOT=Path(__file__).resolve().parents[2];OUT=ROOT/'reports/dashboard-parity/context-history'
manifest=json.loads((OUT/'manifest.json').read_text());assert 'si-context-ui-' in manifest['db']
route='http://127.0.0.1:8876/#/projects/'+quote(manifest['project_key'],safe='')+'?tab=context'
requests=[];errors=[]
with sync_playwright() as runtime:
    browser=runtime.chromium.launch(headless=True)
    page=browser.new_page(viewport={'width':1440,'height':1024})
    page.on('pageerror',lambda e:errors.append(str(e)))
    page.on('request',lambda r:requests.append([r.method,r.url]))
    keys=KeyboardActions(page,OUT/'keyboard.json');visual=VisualChecks(page,OUT/'contrast.json')
    def choose_copy(value):
        keys.choose(page.locator('#project-copy'),value)
        expect(page.locator('#project-detail-body summary').first).to_be_focused()
    try:
        page.goto(route);page.wait_for_load_state('networkidle')
        page.screenshot(path=str(OUT/'initial.png'));(OUT/'initial.html').write_text(page.content())
        expect(page.get_by_role('heading',name='Observed instructions',exact=True).first).to_be_visible()
        expect(page.locator('#project-copy')).to_be_visible()
        default_copy=page.locator('#project-copy').input_value()
        other_copy=next(value for value in manifest['copies'].values() if value!=default_copy)
        choose_copy(other_copy)
        page.locator('#project-copy').click();page.keyboard.press('Escape')
        expect(page.locator('#project-view-context')).to_have_attribute('aria-current','page')
        expect(page.locator('#project-copy')).to_have_value(other_copy)
        keys.reach(page.locator('#project-copy'))
        page.go_back();page.wait_for_load_state('networkidle')
        expect(page.locator('#project-copy')).to_have_value(default_copy)
        expect(page.locator('#project-copy')).to_be_focused()
        page.go_forward();page.wait_for_load_state('networkidle')
        expect(page.locator('#project-copy')).to_have_value(other_copy)
        expect(page.locator('#project-copy')).to_be_focused()
        page.reload();page.wait_for_load_state('networkidle')
        expect(page.locator('#project-copy')).to_have_value(other_copy)
        if page.locator('#project-copy').input_value()!=manifest['copies']['alpha']:
            choose_copy(manifest['copies']['alpha'])
        expect(page.locator('#project-context-status')).to_contain_text('20 shown · 23 observation times')
        expect(page.locator('#project-detail-body')).to_contain_text('47 UTF-8 bytes · 24 characters')
        expect(page.locator('#project-detail-body')).to_contain_text('50,000 UTF-8 bytes · 50,000 characters')
        keys.activate(page.locator('#project-context-older'))
        expect(page.locator('#project-context-status')).to_contain_text('23 shown · 23 observation times')
        expect(page.locator('#project-context-status')).to_be_focused()
        expect(page.locator('#project-context-older')).to_have_count(0)
        summaries=page.locator('#project-context-history > details > summary')
        expect(summaries).to_have_count(23)
        keys.activate(summaries.first)
        expect(page.locator('#project-context-history > details').first).to_contain_text('+2 bytes · +1 characters')
        visual.themes(keys,'history-expanded',OUT,target=summaries.first)
        keys.activate(summaries.last)
        expect(page.locator('#project-context-history > details').last).to_contain_text('no prior observation')
        choose_copy(manifest['copies']['beta'])
        expect(page.locator('#project-context-status')).to_contain_text('1 shown · 1 observation times')
        expect(page.locator('#project-detail-body')).to_contain_text('0 UTF-8 bytes · 0 characters')
        choose_copy(manifest['copies']['alpha'])
        expect(page.locator('#project-context-status')).to_contain_text('23 shown · 23 observation times')
        page.go_back();page.wait_for_load_state('networkidle')
        expect(page.locator('#project-copy')).to_have_value(manifest['copies']['beta'])
        expect(page.locator('#project-context-status')).to_contain_text('1 shown · 1 observation times')
        page.go_forward();page.wait_for_load_state('networkidle')
        expect(page.locator('#project-copy')).to_have_value(manifest['copies']['alpha'])
        expect(page.locator('#project-context-status')).to_contain_text('23 shown · 23 observation times')
        page.route('**/api/project-context-history?*',lambda r:r.fulfill(status=503,json={'detail':'Invented history read failure'}))
        keys.activate(page.locator('#project-context-refresh'))
        expect(page.locator('#project-context-history [role=alert]')).to_contain_text('Invented history read failure')
        expect(page.locator('#project-context-refresh')).to_be_focused()
        page.unroute('**/api/project-context-history?*')
        keys.activate(page.locator('#project-context-refresh'))
        expect(page.locator('#project-context-history [role=alert]')).to_have_count(0)
        expect(page.locator('#project-context-status')).to_contain_text('20 shown · 23 observation times')
        page.reload();page.wait_for_load_state('networkidle')
        expect(page.locator('#project-copy')).to_have_value(manifest['copies']['alpha'])
        expect(page.locator('#project-context-status')).to_contain_text('20 shown · 23 observation times')
        keys.activate(page.locator('#project-context-refresh'))
        expect(page.locator('#project-context-refresh')).to_be_focused()
        visual.themes(keys,'history-reloaded',OUT,target=page.get_by_role('heading',name='Instruction context',exact=True))
        # Exact copy follows native section links, including ownership and overview links.
        for selector in ['#project-view-topology','#project-view-inventory','#project-section-overview','#project-section-instructions']:
            keys.activate(page.locator(selector));page.wait_for_load_state('networkidle')
            expect(page.locator('#project-copy')).to_have_value(manifest['copies']['alpha'])
            assert parse_qs(page.url.split('?',1)[1])['working_copy_id']==[manifest['copies']['alpha']]
        expect(page.locator('#project-context-status')).to_contain_text('20 shown · 23 observation times')
        selected_route=route+'&working_copy_id='+manifest['copies']['alpha']
        missing='f'*64
        page.goto(route+'&working_copy_id='+missing);page.wait_for_load_state('networkidle')
        expect(page.locator('#project-copy')).to_have_value(missing)
        expect(page.locator('#project-copy-status')).to_contain_text('No inventory is retained for the requested working copy')
        expect(page.locator('#project-context-history')).to_have_count(0)
        visual.themes(keys,'missing-copy',OUT,target=page.locator('#project-copy-status'))
        assert page.evaluate('document.documentElement.scrollWidth===innerWidth')
        # Truncate only the invented first page. The exact-copy request uses the real API.
        held=[];list_data=[]
        def paged_inventory(request):
            params=parse_qs(urlparse(request.request.url).query)
            if params.get('working_copy_id')==[manifest['copies']['alpha']]:
                held.append(request);return
            upstream=urlparse(request.request.url)
            upstream_params={name:values for name,values in params.items() if name!='cursor'}
            response=request.fetch(url=urlunparse(upstream._replace(query=urlencode(upstream_params,doseq=True))));data=response.json()
            if 'working_copy_id' not in params:
                if not list_data:list_data.extend(data['records'])
                data['records']=[r for r in list_data if (r['working_copy_id']==manifest['copies']['alpha'])==bool(params.get('cursor'))]
                data['next_cursor']=None if params.get('cursor') else 'invented-next-copy'
            request.fulfill(response=response,json=data)
        page.route('**/api/project-inventory?*',paged_inventory)
        page.goto(selected_route);page.reload(wait_until='domcontentloaded')
        expect(page.locator('#project-copy-status')).to_contain_text('Reading requested working copy')
        expect(page.locator('#project-context-history')).to_have_count(0)
        assert len(held)==1
        # Move selection before the old exact read completes: its reply cannot replace beta.
        keys.choose(page.locator('#project-copy'),manifest['copies']['beta'])
        expect(page.locator('#project-context-status')).to_contain_text('1 shown · 1 observation times')
        held.pop().continue_();page.wait_for_load_state('networkidle')
        expect(page.locator('#project-copy')).to_have_value(manifest['copies']['beta'])
        expect(page.locator('#project-context-status')).to_contain_text('1 shown · 1 observation times')
        # The cached exact result remains reachable without changing the list cursor.
        page.goto(selected_route);page.wait_for_load_state('networkidle')
        expect(page.locator('#project-copy')).to_have_value(manifest['copies']['alpha'])
        expect(page.locator('#project-context-status')).to_contain_text('20 shown · 23 observation times')
        expect(page.locator('#inventory-older')).to_be_visible()
        keys.activate(page.locator('#inventory-older'));page.wait_for_load_state('networkidle')
        expect(page.locator('#project-copy option')).to_have_count(2)
        expect(page.locator('#inventory-older')).to_have_count(0)
        expect(page.locator('#inventory-status')).to_be_focused()
        page.unroute('**/api/project-inventory?*')
        # A fresh deep link outside the first page also resolves without any prior selection.
        fail_exact=[True]
        def hide_first_page_copy(request):
            if 'working_copy_id' in parse_qs(urlparse(request.request.url).query):
                if fail_exact[0]:request.fulfill(status=503,json={'detail':'Invented exact-copy read failure'})
                else:request.continue_()
                return
            response=request.fetch();data=response.json()
            data['records']=[row for row in data['records'] if row['working_copy_id']!=manifest['copies']['alpha']]
            data['next_cursor']='invented-next-copy'
            request.fulfill(response=response,json=data)
        page.route('**/api/project-inventory?*',hide_first_page_copy)
        page.reload();page.wait_for_load_state('networkidle')
        expect(page.locator('#project-copy')).to_have_value(manifest['copies']['alpha'])
        expect(page.locator('#project-copy-status')).to_contain_text('Invented exact-copy read failure')
        expect(page.locator('#project-context-history')).to_have_count(0)
        fail_exact[0]=False
        keys.activate(page.get_by_role('button',name='Retry requested copy',exact=True));page.wait_for_load_state('networkidle')
        expect(page.locator('#inventory-status')).to_be_focused()
        expect(page.locator('#project-copy')).to_have_value(manifest['copies']['alpha'])
        expect(page.locator('#project-context-status')).to_contain_text('20 shown · 23 observation times')
        visual.themes(keys,'exact-copy',OUT,target=page.get_by_role('heading',name='Instruction context',exact=True))
        page.unroute('**/api/project-inventory?*')
        with sqlite3.connect('file:'+manifest['db']+'?mode=ro',uri=True) as conn:
            assert list(conn.iterdump())==manifest['snapshot']
            assert conn.execute('SELECT COUNT(*) FROM llm_calls').fetchone()[0]==0
        assert all(Path(p).read_bytes().hex()==v for p,v in manifest['targets'].items())
        assert all(method=='GET' for method,_ in requests) and not errors,errors
        print('CONTEXT_HISTORY_OK: 23 observations, two copies, 20+3 pages, Unicode/report separation, keyboard focus, error recovery, reload/back/forward/section links, missing copy, delayed exact read, copy beyond first page; unchanged Store/targets; zero models')
    finally:
        browser.close()
