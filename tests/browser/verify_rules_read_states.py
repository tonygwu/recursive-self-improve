"""Native Rules certainty and read recovery; disposable fixtures and GETs only."""
from pathlib import Path
import argparse
import json
import sqlite3
import tempfile
from playwright.sync_api import sync_playwright, expect
from visual_checks import VisualChecks
from keyboard_actions import KeyboardActions

parser=argparse.ArgumentParser()
parser.add_argument('--fixture',choices=['evidence','rules'],default='evidence')
args=parser.parse_args()
ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'reports/dashboard-parity/rules-read-states'/args.fixture
OUT.mkdir(parents=True,exist_ok=True)
manifest=json.loads((OUT.parents[1]/('evidence-browser-manifest.json' if args.fixture=='evidence' else 'rule-browser-manifest.json')).read_text())
assert any(s in manifest['db'] for s in ('si-evidence-ui-','si-rules-ui-'))
BASE='http://127.0.0.1:8876/'
result={'states':[],'requests':[],'errors':[]}
CAUSE='Invented source temporarily unavailable. '
DETAIL=CAUSE+'<script>literal diagnostic</script> '+'longword'*140+' COMPLETE_DIAGNOSTIC_END'

with tempfile.TemporaryDirectory(prefix='si-rules-read-browser-') as temp, sync_playwright() as runtime:
    folder=Path(temp);extension=folder/'extension';extension.mkdir()
    (extension/'manifest.json').write_text(json.dumps({'manifest_version':3,'name':'Temporary Rules zoom','version':'1.0',
      'host_permissions':['http://127.0.0.1/*'],'background':{'service_worker':'worker.js'}}))
    (extension/'worker.js').write_text('chrome.runtime.onInstalled.addListener(() => {});')
    browser=runtime.chromium.launch_persistent_context(str(folder/'profile'),channel='chromium',headless=True,
      args=[f'--disable-extensions-except={extension}',f'--load-extension={extension}'],viewport={'width':1280,'height':1024})
    page=browser.pages[0]
    worker=browser.service_workers[0] if browser.service_workers else browser.wait_for_event('serviceworker')
    page.on('pageerror',lambda e:result['errors'].append(str(e)))
    page.on('request',lambda r:result['requests'].append([r.method,r.url]))
    visual=VisualChecks(page,OUT/'contrast.json');keys=KeyboardActions(page,OUT/'keyboard.json')

    def visit(path):
        page.goto(BASE+path);page.wait_for_load_state('networkidle')

    def capture(name, target=None):
        if target is not None:target.scroll_into_view_if_needed()
        page.screenshot(path=str(OUT/(name+'.png')))
        (OUT/(name+'.html')).write_text(page.content())
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        # Long request text must not widen its panel; complete diagnostics scroll
        # or wrap within their own region.
        for node in page.locator('#rules-notice, #inspector-body').all():
            if node.is_visible():assert node.evaluate('e=>e.scrollWidth<=e.clientWidth+1')
        visual.check(name);result['states'].append(name)

    def fail(pattern):
        page.route(pattern,lambda route:route.fulfill(status=503,json={'detail':DETAIL}))

    def failure(root, title, action):
        alert=root.locator('[role="alert"]').first
        expect(alert).to_contain_text(title);expect(alert).to_contain_text(CAUSE.strip())
        expect(alert).to_contain_text(action)
        assert len(alert.inner_text())<600
        assert 'GET /api/' not in alert.inner_text()
        disclosure=root.locator('details').filter(has=page.get_by_text('Request details',exact=True)).first
        if disclosure.get_attribute('open') is not None:keys.activate(disclosure.locator('summary'))
        expect(disclosure).not_to_have_attribute('open','')
        assert 'COMPLETE_DIAGNOSTIC_END' not in root.inner_text()
        keys.activate(disclosure.locator('summary'))
        expect(disclosure.locator('pre')).to_contain_text('GET /api/')
        expect(disclosure.locator('pre')).to_contain_text(DETAIL)
        assert page.locator('script').filter(has_text='literal diagnostic').count()==0
        return disclosure

    def hold(prefix):
        page.evaluate('''prefix=>{
          window.ra53Original=window.fetch;window.ra53Pending=false;
          window.fetch=(url,opts)=>String(url).startsWith(prefix) ? new Promise(resolve=>{
            window.ra53Pending=true;window.ra53Release=async()=>resolve(await window.ra53Original(url,opts));
          }) : window.ra53Original(url,opts);
        }''',prefix)

    def release():
        page.evaluate('''async()=>{window.fetch=window.ra53Original;await window.ra53Release();}''')
        page.wait_for_load_state('networkidle')

    try:
        if args.fixture=='rules':
            visit('#/rules');capture('family-reconnaissance')
            family=page.locator('[data-rule-family]').first
            fail('**/api/rule-families/*/members*');keys.activate(family)
            root=page.locator('.rules-member-notice').first
            failure(root,'Could not read family members','Retry members');capture('family-error',root)
            # A same-family repaint preserves the exact open native disclosure.
            page.evaluate('async()=>(await import("/app.js")).paintRuleBrowser()')
            expect(root.locator('details')).to_have_attribute('open','')
            page.unroute('**/api/rule-families/*/members*')
            keys.activate(page.get_by_role('button',name='Retry members',exact=True))
            expect(page.locator('.rules-member[data-child="true"]')).to_have_count(20)
            expect(page.locator('#rules-member-status')).to_be_focused();capture('family-recovered')
        else:
            visit('#/rules/l00?grouping=false');capture('report-reconnaissance')
            inspector=page.locator('#inspector-body')
            expect(inspector).to_contain_text('Reported violation')
            expect(inspector).to_contain_text('Rule named in the report')
            expect(inspector).to_contain_text('Agent receipt and violation are unverified.')
            expect(inspector).to_contain_text('The fixture assistant reportedly ignored the existing retry rule.')
            assert 'written but ignored' not in page.locator('body').inner_text().lower()
            visit('#/rules/l01?grouping=false')
            expect(inspector).to_contain_text('No violation report was retained')
            expect(inspector).to_contain_text('Prior rule receipt and violation are unknown.');capture('unknown-report')
            # Long escaped reports and failures in all required widths/themes.
            report='<script>literal report</script> '+'Invented reported rule. '*100+' REPORT_END'
            def long_report(route):
                response=route.fetch();data=response.json();data['enforcement_gap']['violated_existing_rule']=report
                route.fulfill(response=response,json=data)
            page.route('**/api/rules/l00',long_report)
            for width in (1280,1440):
                for theme in ('light','dark'):
                    page.set_viewport_size({'width':width,'height':1024});visit('#/rules/l00?grouping=false');keys.theme(theme)
                    expect(inspector).to_contain_text(report);capture(f'{theme}-{width}-report')
                    fail('**/api/rules/browse*');keys.activate(page.get_by_role('button',name='Refresh',exact=True))
                    failure(page.locator('#rules-notice'),'Could not read rules','Refresh')
                    expect(page.locator('#rules-notice')).to_contain_text('Showing the previous results')
                    assert page.locator('#rules-results tr').count()>0
                    capture(f'{theme}-{width}-list-error')
                    page.evaluate('async()=>(await import("/app.js")).paintRuleBrowser()')
                    expect(page.locator('#rules-notice details')).to_have_attribute('open','')
                    page.unroute('**/api/rules/browse*');keys.activate(page.get_by_role('button',name='Refresh',exact=True))
                    expect(page.locator('#rules-notice [role="alert"]')).to_have_count(0)
                    expect(page.locator('#rules-refresh')).to_be_focused()
                    page.wait_for_load_state('networkidle')
            page.unroute('**/api/rules/l00')
            # Initial shared read owns its error and exposes complete request details.
            fail('**/api/rules/browse*');page.reload();page.wait_for_load_state('networkidle')
            banner=page.locator('#global-error')
            expect(banner).to_contain_text('Could not read 1 of 4 endpoints')
            assert 'GET /api/' not in banner.inner_text()
            keys.activate(banner.get_by_text('Request details',exact=True))
            expect(banner.get_by_role('region',name='Complete request diagnostic')).to_contain_text(DETAIL)
            capture('initial-read-error',banner)
            page.unroute('**/api/rules/browse*');keys.activate(banner.get_by_role('button',name='Retry dashboard reads'))
            expect(banner).to_be_hidden();capture('initial-read-recovered')
            # Selected detail retries retain native focus; another control owns focus
            # when the user moves there before a pending read completes.
            fail('**/api/rules/l00');visit('#/rules/l00?grouping=false')
            keys.activate(page.get_by_role('button',name='Refresh rule details',exact=True))
            failure(inspector,'Could not read this rule','Retry rule details');capture('selected-rule-error')
            page.evaluate('async()=>{const m=await import("/app.js");m.openRule("l00","why");}')
            expect(inspector.locator('details').first).to_have_attribute('open','')
            page.unroute('**/api/rules/l00');keys.activate(page.get_by_role('button',name='Retry rule details'))
            expect(inspector).to_contain_text('Reported violation');expect(page.locator('#rules-read-rule-l00')).to_be_focused()
            hold('/api/rules/l00');keys.activate(page.get_by_role('button',name='Refresh rule details'))
            page.wait_for_function('window.ra53Pending');expect(inspector).to_contain_text('Reading rule details')
            search=page.get_by_role('searchbox',name='Search rules and linked evidence');keys.reach(search)
            release();expect(search).to_be_focused();capture('rule-retry-no-focus-steal')
            hold('/api/rules/l00');keys.activate(page.get_by_role('button',name='Refresh rule details'))
            page.wait_for_function('window.ra53Pending')
            # The next record need not be on the current page; navigate with a real link.
            other=page.locator('#rules-results [data-rule-id]').nth(1)
            other_id=other.get_attribute('data-rule-id');keys.activate(other);page.wait_for_load_state('networkidle')
            release();assert page.evaluate('async()=>(await import("/app.js")).state.selectedRule')==other_id
            capture('late-rule-isolation')
            # Complete evidence source, linked page, and list failures/recovery.
            for path,pattern,title in [
                ('#/rules/evidence/learning/l00?mode=evidence&tab=source','**/api/evidence/learning/l00','Could not read this evidence source'),
                ('#/rules/evidence/learning/l00?mode=evidence&tab=linked','**/api/learnings/l00/evidence*','Could not read linked incidents')]:
                fail(pattern);visit(path);failure(inspector,title,'Refresh evidence');capture('evidence-'+('linked' if 'linked' in path else 'source')+'-error')
                page.evaluate('async()=>{const m=await import("/app.js");await m.openEvidence("learning","l00");}')
                expect(inspector.get_by_text('Request details',exact=True).locator('..')).to_have_attribute('open','')
                page.unroute(pattern);keys.activate(page.get_by_role('button',name='Refresh evidence',exact=True))
                expect(inspector.locator('[role="alert"]')).to_have_count(0)
                expect(page.locator('#rules-read-evidence-refresh')).to_be_focused();capture('evidence-recovered-'+('linked' if 'linked' in path else 'source'))
            fail('**/api/evidence');visit('#/rules?mode=evidence')
            keys.activate(page.get_by_role('button',name='Refresh',exact=True))
            failure(page.locator('#rules-notice'),'Could not read retained evidence','Refresh');capture('evidence-list-error')
            page.unroute('**/api/evidence');keys.activate(page.get_by_role('button',name='Refresh',exact=True))
            expect(page.locator('#rules-notice [role="alert"]')).to_have_count(0)
            visit('#/rules?mode=evidence&kinds=learning');hold('/api/evidence?')
            keys.activate(page.get_by_role('button',name='Next results',exact=True));page.wait_for_function('window.ra53Pending')
            keys.reach(search);release();expect(search).to_be_focused();capture('evidence-paging-no-focus-steal')
            visit('#/rules?mode=evidence&query=no_fixture_source_matches_this')
            expect(page.locator('#evidence-results')).to_contain_text('No matching evidence');capture('evidence-empty')
            # Explicit history reads distinguish unknown, failed refresh and retained records.
            visit('#/rules/l00?grouping=false&tab=provenance')
            keys.activate(page.get_by_role('button',name='Load mining history',exact=True))
            expect(inspector).to_contain_text('No mining history was recorded')
            fail('**/api/learnings/l00/mining-history*');keys.activate(page.get_by_role('button',name='Refresh mining history',exact=True))
            failure(inspector,'Could not read mining history','mining history button');expect(inspector).to_contain_text('Showing the previous mining history');capture('mining-history-error')
            page.unroute('**/api/learnings/l00/mining-history*');hold('/api/learnings/l00/mining-history')
            keys.activate(page.get_by_role('button',name='Refresh mining history',exact=True));page.wait_for_function('window.ra53Pending')
            keys.reach(search);release();expect(search).to_be_focused();capture('mining-no-focus-steal')
            hold('/api/learnings/l00/mining-history')
            keys.activate(page.get_by_role('button',name='Refresh mining history',exact=True));page.wait_for_function('window.ra53Pending')
            refresh_rule=page.get_by_role('button',name='Refresh rule details',exact=True)
            keys.reach(refresh_rule);release();expect(refresh_rule).to_be_focused()
            page.evaluate('async()=>{const m=await import("/app.js");m.openRule("l00","provenance");}')
            expect(refresh_rule).to_be_focused();capture('mining-and-redock-keep-recovery-focus')
            visit('#/rules/l00?grouping=false&tab=evidence')
            scan=page.get_by_role('button',name='Load detector observations',exact=True).first
            incident=scan.get_attribute('data-scan-history');root=page.locator('[data-scan-history-root="'+incident+'"]')
            hold('/api/incidents/'+incident+'/scan-history')
            keys.activate(scan);page.wait_for_function('window.ra53Pending')
            page.wait_for_function('location.hash.includes("scan=")')
            expect(root.locator('[data-scan-history]').first).to_be_focused()
            release();expect(root).to_contain_text('0 observations shown')
            expect(root.locator('[data-scan-history]').first).to_be_focused()
            fail('**/api/incidents/'+incident+'/scan-history*');keys.activate(root.get_by_role('button',name='Refresh detector observations',exact=True))
            failure(root,'Could not read detector observations','detector observations button');capture('detector-error',root)
            page.unroute('**/api/incidents/'+incident+'/scan-history*');keys.activate(root.get_by_role('button',name='Refresh detector observations',exact=True))
            expect(root.locator('[role="alert"]')).to_have_count(0);capture('detector-recovered',root)
            fail('**/api/rules/l00');visit('#/rules/l00?grouping=false')
            keys.activate(page.get_by_role('button',name='Refresh rule details',exact=True))
            failure(inspector,'Could not read this rule','Retry rule details')
            page.set_viewport_size({'width':640,'height':1024});capture('narrow-error',inspector)
            page.keyboard.press('Tab');keys.reach(inspector.get_by_text('Request details',exact=True));capture('narrow-diagnostic')
            page.set_viewport_size({'width':1440,'height':1024})
            worker.evaluate('async()=>{const [tab]=await chrome.tabs.query({active:true,currentWindow:true});await chrome.tabs.setZoom(tab.id,2);}')
            assert worker.evaluate('async()=>{const [tab]=await chrome.tabs.query({active:true,currentWindow:true});return chrome.tabs.getZoom(tab.id);}')==2
            capture('native-200-percent',inspector)
            page.keyboard.press('Tab');keys.reach(inspector.get_by_text('Request details',exact=True));capture('native-200-percent-diagnostic')
            worker.evaluate('async()=>{const [tab]=await chrome.tabs.query({active:true,currentWindow:true});await chrome.tabs.setZoom(tab.id,1);}')
        assert not result['errors'],result['errors']
        assert all(method=='GET' for method,url in result['requests'])
        visual.assert_clean();keys.save()
        with sqlite3.connect('file:'+manifest['db']+'?mode=ro',uri=True) as conn:
            assert list(conn.iterdump())==manifest['snapshot']
            assert conn.execute('SELECT COUNT(*) FROM llm_calls').fetchone()[0]==0
        for path,text in manifest.get('targets',{}).items():assert Path(path).read_text()==text
        result.update(database_unchanged=True,targets_unchanged=True,models=0)
        (OUT/'result.json').write_text(json.dumps(result,indent=2)+'\n')
        print('RULES_READ_STATES_OK',args.fixture,len(result['states']),'states',len(result['requests']),'GETs; unchanged Store/targets, zero models')
    except Exception:
        page.screenshot(path=str(OUT/'failure.png'));(OUT/'failure.html').write_text(page.content());raise
    finally:browser.close()
