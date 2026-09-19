"""Complete Overview audit with temporary data and one explicit fixture decision."""
from pathlib import Path
import json, sqlite3, time
from playwright.sync_api import sync_playwright, expect

from keyboard_actions import KeyboardActions
from visual_checks import VisualChecks

out=Path(__file__).resolve().parents[2]/'reports/dashboard-parity'
manifest=json.loads((out/'overview-manifest.json').read_text())
assert 'si-overview-ui-' in manifest['db']
base='http://127.0.0.1:8876/'
def dump():
    with sqlite3.connect('file:'+manifest['db']+'?mode=ro',uri=True) as db:
        return list(db.iterdump())
def unchanged_targets():
    assert all(Path(path).read_text()==text for path,text in manifest['targets'].items())

with sync_playwright() as p:
    browser=p.chromium.launch(headless=True)
    page=browser.new_page(viewport={'width':1440,'height':1024})
    errors=[];requests=[]
    keys=KeyboardActions(page,out/'overview-keyboard.json')
    visual=VisualChecks(page,out/'overview-contrast.json')
    page.on('pageerror',lambda error:errors.append(str(error)))
    page.on('request',lambda request:requests.append((request.method,request.url)))
    def goto(path='#/overview'):
        page.goto(base+path);page.wait_for_load_state('networkidle')
    def counts(n):
        expect(page.locator('#nav-inbox-count')).to_have_text(str(n))
        expect(page.locator('#ov-tiles .tile').nth(2).locator('.tile__value')).to_have_text(str(n))
        expect(page.locator('#ov-inbox-meta')).to_have_text(f'{n} waiting')
        assert f'{n} waiting on you' in page.locator('#ov-statusline').inner_text()
    def mining_counts(run_id, completed, failed):
        assert page.url.endswith('/run/' + run_id + '/mine'), page.url
        row = page.locator('#run-stage-mine')
        expect(row.locator('[data-run-count="input"]')).to_have_text(str(completed + failed))
        expect(row.locator('[data-run-count="completed"]')).to_have_text(str(completed))
        expect(row.locator('[data-run-count="failed"]')).to_have_text(str(failed))
    def capture(width,theme):
        page.set_viewport_size({'width':width,'height':1024})
        if page.evaluate('async()=>(await import("/app.js")).effectiveTheme()')!=theme:
            keys.activate(page.get_by_role('button',name='Dark theme' if theme=='dark' else 'Light theme',exact=True))
        assert page.evaluate('async()=>(await import("/app.js")).effectiveTheme()')==theme
        page.locator('#main').evaluate('(el)=>el.scrollTop=0')
        visual.check('overview-expanded')
        page.screenshot(path=str(out/f'overview-{theme}-{width}.png'))
        attention = page.locator('.overview-lower').bounding_box()
        review = page.locator('#ov-inbox').get_by_role('link',name='Open review queue').bounding_box()
        geometry = {'width':width, 'theme':theme, 'attention_y':attention['y'], 'review_bottom':review['y']+review['height']}
        print('OVERVIEW_COMPOSITION',json.dumps(geometry),flush=True)
        assert geometry['attention_y'] <= 650, geometry
        assert geometry['review_bottom'] <= 1000, geometry
        assert page.locator('#view-overview').evaluate('(el)=>el.scrollWidth<=el.clientWidth+1')
        page.locator('#ov-backlog').scroll_into_view_if_needed()
        page.screenshot(path=str(out/f'overview-{theme}-{width}-coverage.png'))
    try:
        goto();page.screenshot(path=str(out/'overview-before.png'))
        print('OVERVIEW_RECON',page.get_by_role('button').all_text_contents())
        start=time.monotonic()
        counts(8);assert page.locator('#ov-tiles .tile').count()==4
        assert 'all classes off' in page.locator('#ov-policy').inner_text()
        latest=page.locator('#ov-latest').inner_text()
        assert 'same-z' in latest and 'succeeded: 4' in latest and 'succeeded: 12' not in latest
        assert page.locator('#ov-inbox .overview-family').count()==3
        assert max(len(text) for text in page.locator('#ov-inbox .overview-family a').all_text_contents())<=161
        assert page.locator('#ov-inbox script').count()==0
        # Exact latest stage, keyboard focus, reload and same-time chooser.
        keys.activate(page.locator('#ov-latest').get_by_role('link',name='mine',exact=True))
        page.locator('#run-stage-mine').wait_for()
        assert page.url.endswith('/run/same-z/mine')
        mining_counts('same-z', 4, 1)
        assert page.evaluate('document.activeElement.id')=='run-stage-mine'
        visual.themes(keys,'keyboard-run-stage',out,target=page.locator('#run-stage-mine'))
        page.reload();page.locator('#run-stage-mine').wait_for()
        goto();keys.activate(page.locator('#ov-grid a[href="#/overview/night/2030-02-07/mine"]'))
        page.locator('.run-choices').wait_for()
        assert 'same-a' in page.locator('.run-choices').inner_text() and 'same-z' in page.locator('.run-choices').inner_text()
        keys.activate(page.locator('.run-choices a[href="#/overview/run/same-a/mine"]'))
        page.locator('#run-stage-mine').wait_for();mining_counts('same-a', 8, 0)
        goto()
        definitions=page.locator('.overview-grid-foot > details > summary')
        keys.activate(definitions)
        expect(page.locator('#ov-grid-foot')).to_contain_text('the budget refused it')
        expect(page.locator('#ov-grid-foot')).to_contain_text('stopped by Ctrl-C or a shutdown')
        page.evaluate('async()=>(await import("/app.js")).paintOverview()')
        expect(definitions).to_be_focused()
        expect(page.locator('.overview-grid-foot > details')).to_have_attribute('open','')
        keys.activate(definitions)
        # Full failure evidence stays open and focused during a background repaint.
        summary=page.locator('#ov-failure-history > summary');keys.activate(summary)
        assert page.locator('#ov-failure-history').evaluate('(el)=>el.open')
        awaitable='async()=>{const app=await import("/app.js");app.paintOverview();}'
        page.evaluate(awaitable)
        assert page.locator('#ov-failure-history').evaluate('(el)=>el.open')
        assert page.evaluate('document.activeElement.parentElement.id')=='ov-failure-history'
        keys.activate(page.locator('#ov-refresh'));page.wait_for_load_state('networkidle')
        expect(page.locator('#ov-refresh')).to_be_enabled()
        assert page.locator('#ov-failure-history').evaluate('(el)=>el.open')
        assert page.evaluate('document.activeElement.id')=='ov-refresh'
        keys.activate(page.locator('#ov-failures .overview-failure a'));page.locator('#run-title').wait_for()
        assert '/run/same-z' in page.url
        goto()
        text=page.locator('#ov-backlog').inner_text()
        assert '17 successful mining outcomes / 4 runs' in text and '2.4' in text
        assert '80 calls per run' in text and 'net / day' not in text
        keys.activate(page.get_by_text('Sampling policy, measurement windows and coverage',exact=True))
        assert '13 rate days' in page.locator('#ov-backlog-foot').inner_text()
        assert '2030-02-07 is excluded' in page.locator('#ov-backlog-foot').inner_text()
        assert '1 runs without mining counters' in text
        # All inspection above was read-only against the actual selected fixture.
        assert dump()==manifest['snapshot'];unchanged_targets()
        assert all(method=='GET' for method,_ in requests)
        # One explicit decision changes only recorded intent; no worker runs.
        keys.activate(page.locator('#ov-inbox .overview-family a').first)
        reject=page.get_by_role('button',name='Reject at selected targets',exact=True)
        expect(reject).to_be_enabled();keys.activate(reject)
        expect(page.locator('#nav-inbox-count')).to_have_text('7')
        keys.activate(page.get_by_role('link',name='Overview',exact=True))
        counts(7);elapsed=time.monotonic()-start;assert elapsed<300
        page.reload();page.wait_for_load_state('networkidle');counts(7)
        expect(page.get_by_role('heading',name='Recorded failures',exact=True)).to_be_visible()
        expect(page.locator('#ov-failures-meta')).to_have_text('1 recent class')
        assert 'design system' not in page.locator('#ov-latest').inner_text()
        with sqlite3.connect('file:'+manifest['db']+'?mode=ro',uri=True) as db:
            assert db.execute("SELECT COUNT(*) FROM proposals WHERE status='rejected_user'").fetchone()[0]==1
            assert db.execute('SELECT COUNT(*) FROM llm_calls').fetchone()[0]==0
        unchanged_targets()
        # Refresh failure retains data, exposes the failed request and restores focus.
        page.route('**/api/overview',lambda route:route.fulfill(status=503,json={'detail':'fixture read unavailable'}))
        keys.activate(page.locator('#ov-refresh'));expect(page.locator('#global-error')).to_be_visible()
        assert 'fixture read unavailable' in page.locator('#global-error').inner_text();counts(7)
        expect(page.locator('#ov-read-state')).to_contain_text('previous')
        page.unroute('**/api/overview');keys.activate(page.locator('#ov-refresh'))
        expect(page.locator('#global-error')).to_be_hidden();expect(page.locator('#ov-refresh')).to_be_enabled()
        assert page.evaluate('document.activeElement.id')=='ov-refresh'
        for width in (1440,1280):
            for theme in ('light','dark'):capture(width,theme)
        # No synthetic whole-page overflow; narrower grid supports keyboard scrolling.
        keys.reach(page.locator('#ov-grid'));page.keyboard.press('ArrowRight')
        assert page.evaluate('document.activeElement.id')=='ov-grid'
        # Missing records are visibly different from the earlier measured count.
        data=page.request.get(base+'api/overview').json()
        data['audit']['latest_run']=None;data['failures']={}
        data['audit']['mining'].update(per_day=None,recorded_runs=0,succeeded=0)
        page.route('**/api/overview',lambda route:route.fulfill(json=data))
        keys.activate(page.locator('#ov-refresh'));page.wait_for_load_state('networkidle')
        expect(page.locator('#ov-refresh')).to_be_enabled()
        expect(page.locator('#ov-latest')).to_contain_text('No retained run')
        expect(page.locator('#ov-failures-meta')).to_have_text('0 recent classes')
        assert 'No measured mining throughput' in page.locator('#ov-backlog').inner_text()
        keys.activate(page.locator('#ov-failure-history > summary'))
        assert 'Missing history or taxonomy coverage' in page.locator('#ov-failure-history').inner_text()
        assert 'measured zero' not in page.locator('#ov-failure-history').inner_text()
        # An explicit response fixture checks unknown rendering without altering retained state.
        column=next(c for c in data['grid']['columns'] if c['run_count'])
        column['cells']['gate'].update(state='unreadable',states={'unreadable':1,'future<outcome>':1})
        data['grid']['unreadable']=[{'run_id':column['run_ids'][0],'stage':'gate'}]
        data['grid']['unknown_run_statuses']={'future<status>':1}
        keys.activate(page.locator('#ov-refresh'));page.wait_for_load_state('networkidle')
        expect(page.locator('#ov-grid-legend')).to_contain_text('Outcome unknown (unreadable)')
        expect(page.locator('#ov-grid-legend')).to_contain_text('Outcome unknown (future<outcome>)')
        keys.activate(definitions)
        expect(page.locator('#ov-grid-foot')).to_contain_text('1 stage record(s) could not be read')
        expect(page.locator('#ov-grid-foot')).to_contain_text('never counted as success')
        expect(page.locator('#ov-grid-foot')).to_contain_text('future<status>')
        expect(page.locator('#ov-grid-foot')).to_contain_text('recorded stage outcomes could not be read')
        for selector in ('#ov-grid-legend','#ov-grid-foot'):
            assert 'unmapped' not in page.locator(selector).inner_text()
            assert 'schema' not in page.locator(selector).inner_text()
            expect(page.locator(selector+' script')).to_have_count(0)
        # Raw state, title and exact original stage link stay available.
        cell=page.locator('#ov-grid td[data-state="unreadable"]')
        expect(cell).to_have_count(1)
        assert 'unreadable' in cell.get_attribute('title')
        href=cell.locator('a').get_attribute('href')
        assert href.endswith('/gate') and ('/run/' in href or '/night/' in href)
        visual.themes(keys,'unknown-outcome-disclosure',out,target=page.locator('#ov-grid-foot'))
        assert not errors,errors
        posts=[url for method,url in requests if method=='POST']
        assert posts==[base+'api/commands'],posts
        unchanged_targets()
        result={'requests':len(requests),'explicit_decisions':len(posts),'weekly_audit_seconds':round(elapsed,3),'console_errors':errors,'read_phase_unchanged':True,'targets_unchanged':True,'model_calls':0}
        (out/'overview-browser-result.json').write_text(json.dumps(result,indent=2))
        visual.assert_clean()
        print('OVERVIEW_BROWSER_OK',json.dumps(result))
    except Exception:
        page.screenshot(path=str(out/'overview-failure.png'),full_page=True)
        (out/'overview-failure.html').write_text(page.content())
        raise
    finally:keys.save();browser.close()
