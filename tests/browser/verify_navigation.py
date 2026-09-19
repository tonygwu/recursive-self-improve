"""Real palette interaction on the disposable evidence-browser fixture."""
from pathlib import Path
from urllib.parse import quote
import json,re,sqlite3
from playwright.sync_api import sync_playwright
out=Path(__file__).resolve().parents[2]/'reports/dashboard-parity'
manifest=json.loads((out/'evidence-browser-manifest.json').read_text())
assert 'si-evidence-ui-' in manifest['db']
base='http://127.0.0.1:8876/'
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True);page=browser.new_page(viewport={'width':1440,'height':1024})
    errors=[];requests=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    page.on('request',lambda request:requests.append((request.method,request.url)))
    dialog=page.locator('#navigation-dialog');search=dialog.get_by_role('combobox')
    def open_palette():
        if not dialog.is_visible():page.get_by_role('button',name='Find or go to').click()
        else:page.keyboard.press('Control+k')
        search.wait_for();assert page.evaluate('document.activeElement.id')=='navigation-input'
    def query(value):
        open_palette();search.fill(value)
        page.wait_for_function('document.getElementById("navigation-results").getAttribute("aria-busy")==="false"')
    def choose(label):
        page.get_by_role('option').filter(has=page.locator('.navigation-result__label',has_text=re.compile('^'+re.escape(label)+'$'))).click()
    def goto(path):
        page.goto(base+path);page.wait_for_load_state('networkidle')
    try:
        goto('#/overview');page.screenshot(path=str(out/'navigation-before.png'))
        print('NAVIGATION_RECON',page.get_by_role('button').all_text_contents())
        open_palette();assert not page.get_by_role('button',name='Retry evidence search').is_visible()
        # Native modal cycles focus inside itself; Escape restores the actual opener.
        for _ in range(12):
            page.keyboard.press('Tab');assert page.evaluate('document.getElementById("navigation-dialog").contains(document.activeElement)')
        page.keyboard.press('Control+k');assert page.evaluate('document.activeElement.id')=='navigation-input'
        page.keyboard.press('Escape');assert not dialog.is_visible()
        assert page.evaluate('document.activeElement.id')=='navigation-open'
        page.keyboard.press('Control+k');search.wait_for();page.keyboard.press('ArrowDown');page.keyboard.press('Enter')
        page.wait_for_url('**/#/rules');assert not dialog.is_visible()
        assert page.evaluate('document.activeElement.id')=='main'
        # Slash belongs to the active text field and cannot move focus from an editor.
        page.locator('#rules-search-input').fill('literal');page.keyboard.type('/')
        assert page.locator('#rules-search-input').input_value()=='literal/'
        page.keyboard.press('Meta+k');search.wait_for();page.keyboard.press('Escape')
        assert page.evaluate('document.activeElement.id')=='rules-search-input'
        # Complete retained source search reaches beyond the initial Rules page.
        query('native-session-44');page.get_by_role('option').filter(has_text='native-session-44').first.wait_for()
        options=page.get_by_role('option').all_text_contents();assert any('Session' in x for x in options)
        page.get_by_role('option').filter(has_text='Session ·').click()
        page.wait_for_function('location.hash.includes("/rules/evidence/session/")')
        page.locator('#inspector-body [data-evidence-detail-refresh]:not([disabled])').wait_for()
        assert page.locator('#navigation-breadcrumb').inner_text()=='Rules / Evidence / Session'
        href=page.url
        # Closing above an existing inspector must keep that inspector open.
        open_palette();page.keyboard.press('Escape');assert page.locator('#inspector').is_visible() and page.url==href
        open_palette();page.get_by_role('button',name='Save current page',exact=True).click();page.keyboard.press('Escape')
        page.reload();page.locator('#inspector-body [data-evidence-detail-refresh]:not([disabled])').wait_for()
        open_palette();assert 'Favorites' in dialog.inner_text()
        page.get_by_role('button',name='Remove favorite',exact=True).click()
        assert not page.evaluate('JSON.parse(localStorage.getItem("self-improve.navigation.v1")).favorites.length')
        query('Toggle theme');prior=page.evaluate('document.documentElement.dataset.theme');choose('Toggle theme')
        assert page.evaluate('document.documentElement.dataset.theme')!=prior
        # Every top-level page is reachable; canonical projects are searchable too.
        for label,route in [('Overview','overview'),('Review queue','review'),('Projects','projects'),('Evals & trends','evals')]:
            query(label);choose(label);page.wait_for_url('**/#/'+route)
        query('example.test/owner/evidence-fixture');page.get_by_role('option').filter(has_text='Projects').count()
        project=page.locator('.navigation-result').filter(has=page.locator('.navigation-result__detail',has_text=re.compile('^'+re.escape(manifest['project_key'])+'$')))
        project.click();page.wait_for_url('**/#/projects/'+quote(manifest['project_key'],safe=''))
        page.locator('#project-detail-body').get_by_role('heading').first.wait_for()
        # Exact commands and operations remain readable beyond the first history page.
        for kind in ('command','operation'):
            query(kind+': '+manifest[kind]);choose('Open '+kind+': '+manifest[kind])
            page.wait_for_url('**/#/review/'+kind+'/'+manifest[kind])
            page.wait_for_function('(id)=>document.activeElement.id===id',arg=('delivery-command-' if kind=='command' else 'operation-')+manifest[kind])
            assert kind.title() in page.locator('#navigation-breadcrumb').inner_text()
        page.go_back();page.wait_for_url('**/#/review/command/'+manifest['command'])
        page.go_forward();page.wait_for_url('**/#/review/operation/'+manifest['operation'])
        query('run: absent-fixture-run');choose('Open run: absent-fixture-run')
        page.wait_for_url('**/#/overview/run/absent-fixture-run');page.locator('#run-detail [role="alert"]').wait_for()
        # Zero matches is explicitly zero, while failed reads keep an error and Retry.
        query('錯'*500);choose('Search all evidence for “'+'錯'*500+'”')
        page.wait_for_function('new URLSearchParams(location.hash.split("?")[1]).get("query")?.length===500')
        query('zz-no-fixture-match-8876');assert '0 matching evidence sources' in page.locator('#navigation-status').inner_text()
        choose('Search all evidence for “zz-no-fixture-match-8876”')
        page.wait_for_function('document.getElementById("rules-meta").textContent.startsWith("0 matching sources")')
        page.route('**/api/evidence?*',lambda route:route.fulfill(status=503,content_type='application/json',body='{"detail":"Fixture reader unavailable"}'))
        query('Needle archive end');assert 'failed' in page.locator('#navigation-status').inner_text()
        page.get_by_role('button',name='Retry evidence search').wait_for();page.unroute('**/api/evidence?*')
        page.get_by_role('button',name='Retry evidence search').click();page.wait_for_function('document.getElementById("navigation-status").textContent.startsWith("2 matching evidence sources")')
        assert not page.get_by_role('button',name='Retry evidence search').is_visible()
        page.get_by_role('option').filter(has_text='Incident ·').click();page.locator('#inspector-body [data-evidence-detail-refresh]:not([disabled])').wait_for()
        # Reverse responses through the real module callback; stale results cannot replace current input.
        page.evaluate('''() => {
          window.navigationPending={};window.navigationReadFinished={};window.navigationOriginalFetch=window.fetch;
          window.fetch=(url,opts)=>{
            const parsed=new URL(url,location.href),q=parsed.searchParams.get('query');
            if(parsed.pathname==='/api/evidence' && ['old query','new query','closed query'].includes(q))return new Promise(resolve=>{window.navigationPending[q]=resolve;});
            return window.navigationOriginalFetch(url,opts);
          };
        }''')
        open_palette();search.fill('old query');page.wait_for_function('!!window.navigationPending["old query"]')
        search.fill('new query');page.wait_for_function('!!window.navigationPending["new query"]')
        def answer(query,label):
            page.evaluate('''([query,label])=>window.navigationPending[query]({ok:true,json:async()=>{window.navigationReadFinished[query]=true;return {rows:[{kind:'learning',source_id:label,title:label,excerpt:'Fixture'}],pagination:{count:1}};}})''',[query,label])
            page.wait_for_function('(query)=>window.navigationReadFinished[query]',arg=query)
        answer('new query','New response');page.get_by_role('option').filter(has_text='New response').wait_for()
        assert 'New response' in page.locator('[role="option"][aria-selected="true"]').inner_text()
        answer('old query','Old response');assert not page.get_by_role('option').filter(has_text='Old response').count()
        search.fill('closed query');page.wait_for_function('!!window.navigationPending["closed query"]')
        page.keyboard.press('Escape');open_palette();answer('closed query','Closed response')
        assert not page.get_by_role('option').filter(has_text='Closed response').count()
        page.evaluate('window.fetch=window.navigationOriginalFetch');page.keyboard.press('Escape')
        # Malicious text stays text; no markup from source or local storage executes.
        query('Hostile');assert not dialog.locator('img,script').count();assert page.evaluate('window.privateSentinel===undefined')
        assert '<img src=x onerror=alert(1)>' in dialog.inner_text()
        page.keyboard.press('Escape')
        page.evaluate('localStorage.setItem("self-improve.navigation.v1",JSON.stringify({version:1,favorites:[{href:"javascript:alert(1)",label:"Unsafe"},{href:"#/rules",label:"<img src=x onerror=alert(1)>"}],recent:[]}))')
        page.reload();page.wait_for_load_state('networkidle');open_palette();assert not dialog.locator('img,script,a[href^="javascript:"]').count() and 'Unsafe' not in dialog.inner_text()
        query('Clear recent items');choose('Clear recent items')
        assert not page.evaluate('JSON.parse(localStorage.getItem("self-improve.navigation.v1")).recent.length')
        # Modal keys on Review cannot issue decisions, even after ready previews exist.
        page.keyboard.press('Escape');goto('#/review');open_palette()
        before=page.evaluate('import("/app.js").then(m=>m.state.selectedFamily)')
        search.fill('typing a/r j/k');page.keyboard.press('Control+j');search.dispatch_event('keydown',{'key':'j','isComposing':True})
        page.get_by_role('button',name='Save current page',exact=True).focus()
        for key in ('r','a','j','k'):
            page.get_by_role('button',name='Save current page',exact=True).dispatch_event('keydown',{'key':key,'repeat':True,'bubbles':True})
        assert page.evaluate('import("/app.js").then(m=>m.state.selectedFamily)')==before
        page.keyboard.press('Escape')
        # Storage refusal is explicit, while the modal remains usable.
        other=browser.new_context(viewport={'width':1280,'height':1024})
        other.add_init_script('Object.defineProperty(window,"localStorage",{get(){throw new Error("Fixture denied");}})')
        denied=other.new_page();denied.goto(base+'#/overview');denied.wait_for_load_state('networkidle')
        denied.get_by_role('button',name='Find or go to').click();assert 'session' in denied.locator('#navigation-saved-status').inner_text()
        denied.get_by_role('button',name='Save selected').click();assert 'unavailable' in denied.locator('#navigation-saved-status').inner_text()
        other.close()
        goto('#/rules?mode=evidence');query('Fixture rule')
        assert 'Fixture rule 00' in page.locator('[role="option"][aria-selected="true"]').inner_text()
        assert page.locator('#navigation-results').evaluate('(el)=>el.scrollTop')<100
        for width in (1280,1440):
            page.set_viewport_size({'width':width,'height':1024})
            for theme in ('light','dark'):
                page.evaluate('(theme)=>document.documentElement.dataset.theme=theme',theme)
                page.screenshot(path=str(out/f'navigation-{theme}-{width}.png'))
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                assert dialog.evaluate('(el)=>el.scrollWidth<=el.clientWidth')
        assert not errors,errors
        assert all(method=='GET' and url.startswith(base) for method,url in requests),requests
        with sqlite3.connect(manifest['db']) as db:
            assert list(db.iterdump())==manifest['snapshot'];assert db.execute('SELECT COUNT(*) FROM llm_calls').fetchone()[0]==0
        assert all(Path(path).read_text()==text for path,text in manifest['targets'].items())
        result={'requests':len(requests),'methods':sorted({m for m,_ in requests}),'console_errors':errors}
        (out/'navigation-results.json').write_text(json.dumps(result,indent=2));print('NAVIGATION_BROWSER_OK',json.dumps(result))
    except Exception:
        page.screenshot(path=str(out/'navigation-failure.png'));(out/'navigation-failure.html').write_text(page.content());raise
    finally:browser.close()
