"""Inspect identical retained recurrence IDs in Projects and Evals, GET-only."""
from pathlib import Path
from urllib.parse import quote
import json,sqlite3
from playwright.sync_api import sync_playwright
from keyboard_actions import KeyboardActions
from visual_checks import VisualChecks

out=Path(__file__).resolve().parents[2]/'reports/dashboard-parity'
info=json.loads((out/'recurrence-demo-manifest.json').read_text())
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True);page=browser.new_page(viewport={'width':1440,'height':1024})
    errors=[];requests=[]
    keys=KeyboardActions(page,out/'recurrence-keyboard.json')
    visual=VisualChecks(page,out/'recurrence-contrast.json')
    page.on('pageerror',lambda e:errors.append(str(e)))
    page.on('request',lambda r:requests.append((r.method,r.url)))
    try:
        url='http://127.0.0.1:8876/#/projects/'+quote(info['project_key'],safe='')+'?tab=recurrence'
        page.goto(url);page.wait_for_load_state('networkidle')
        page.screenshot(path=str(out/'recurrence-before.png'));(out/'recurrence-before.html').write_text(page.content())
        page.get_by_text('20 of 23 measurements shown',exact=True).wait_for()
        root=page.locator('#project-detail-body')
        assert 'Lower observed recurrence' in root.inner_text() and '-50%' in root.inner_text()
        keys.activate(page.get_by_role('button',name='Load more measurements',exact=True))
        page.get_by_text('23 of 23 measurements shown',exact=True).wait_for()
        assert page.evaluate('document.activeElement.id')=='recurrence-status-project'
        ids=page.locator('#project-detail-body [data-measurement-id]').evaluate_all('(els)=>els.map(e=>e.dataset.measurementId)')
        assert sorted(ids)==info['measurement_ids']
        keys.activate(page.locator('#project-detail-body [data-measurement-id]').first)
        keys.activate(page.get_by_text('Complete windows, availability, workload, exclusions and matching sources',exact=True))
        assert 'retained_incident_learning_links/1' in root.inner_text()
        assert '"coverage_complete": false' in root.inner_text()
        assert '"eligible_lines": 80' in root.inner_text()
        keys.activate(page.get_by_role('button',name='Refresh measurements',exact=True))
        page.get_by_text('20 of 23 measurements shown',exact=True).wait_for()
        assert page.evaluate('document.activeElement.id')=='recurrence-refresh-project'
        assert root.locator('details[open]').count()>=1
        for width in (1440,1280):
            page.set_viewport_size({'width':width,'height':1024})
            for theme in ('light','dark'):
                keys.theme(theme)
                page.locator('#main').evaluate('(e)=>e.scrollTop=0')
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                visual.check('recurrence-expanded')
                page.screenshot(path=str(out/f'recurrence-{theme}-{width}.png'))
        page.reload();page.wait_for_load_state('networkidle')
        page.get_by_text('20 of 23 measurements shown',exact=True).wait_for()
        page.route('**/api/project-measurements?*',lambda route:route.abort())
        keys.activate(page.get_by_role('button',name='Refresh measurements',exact=True))
        root.locator('[role="alert"]').wait_for()
        assert root.locator('[data-measurement-id]').count()==20
        page.unroute('**/api/project-measurements?*')
        keys.activate(page.get_by_role('button',name='Refresh measurements',exact=True))
        page.wait_for_load_state('networkidle');assert not root.locator('[role="alert"]').count()
        page.goto('http://127.0.0.1:8876/#/evals?project_key='+quote(info['project_key'],safe=''))
        page.wait_for_load_state('networkidle');page.locator('#recurrence-status-evals').get_by_text('20 of 23 measurements shown',exact=True).wait_for()
        keys.activate(page.locator('#recurrence-older-evals'))
        page.locator('#recurrence-status-evals').get_by_text('23 of 23 measurements shown',exact=True).wait_for()
        assert page.evaluate('document.activeElement.id')=='recurrence-status-evals'
        eval_ids=page.locator('#trends-body [data-measurement-id]').evaluate_all('(els)=>els.map(e=>e.dataset.measurementId)')
        assert sorted(eval_ids)==sorted(ids)
        page.locator('#recurrence-status-evals').scroll_into_view_if_needed();page.screenshot(path=str(out/'recurrence-evals.png'))
        page.goto('http://127.0.0.1:8876/#/projects/'+quote(info['empty_project_key'],safe='')+'?tab=recurrence')
        page.wait_for_load_state('networkidle');page.get_by_text('0 of 0 measurements shown',exact=True).wait_for()
        assert 'No recurrence measurements are retained' in root.inner_text()
        assert not errors,errors
        assert all(method=='GET' for method,url in requests if url.startswith('http://127.0.0.1:8876'))
        assert Path(info['target']).read_text()==info['target_text']
        with sqlite3.connect(info['db']) as db:
            assert list(db.iterdump())==info['snapshot']
            assert db.execute('SELECT COUNT(*) FROM llm_calls').fetchone()[0]==0
        visual.assert_clean()
        print('RECURRENCE_BROWSER_OK: same 23 IDs in Projects/Evals; 20+3, evidence, reload/focus, errors/recovery, empty state, both themes/widths, GET-only, unchanged Store/target, zero calls')
    except Exception:
        page.screenshot(path=str(out/'recurrence-failure.png'));(out/'recurrence-failure.html').write_text(page.content());raise
    finally:keys.save();browser.close()
