"""Read-only navigation through every Project view and shared breadcrumbs."""
from pathlib import Path
from urllib.parse import quote
import json
import sqlite3

from playwright.sync_api import sync_playwright, expect

from keyboard_actions import KeyboardActions
from visual_checks import VisualChecks

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'reports/dashboard-parity/navigation-layout'
OUT.mkdir(parents=True, exist_ok=True)
manifest = json.loads((ROOT / 'reports/dashboard-parity/evidence-browser-manifest.json').read_text())
assert 'si-evidence-ui-' in manifest['db']
BASE = 'http://127.0.0.1:8876/'
PROJECT = '#/projects/'+quote(manifest['project_key'], safe='')
result = {'captures':[], 'requests':[], 'errors':[]}
with sync_playwright() as runtime:
    browser = runtime.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width':1280, 'height':1024})
    keys=KeyboardActions(page,OUT/'navigation-keyboard.json')
    visual=VisualChecks(page,OUT/'navigation-contrast.json')
    page.on('pageerror', lambda error:result['errors'].append(str(error)))
    page.on('request', lambda request:result['requests'].append([request.method,request.url]))
    def visit(route):
        page.goto(BASE+route);page.wait_for_load_state('networkidle')
    def capture(name):
        page.locator('#main').evaluate('el=>el.scrollTop=0')
        visual.check(name)
        page.screenshot(path=str(OUT / (name+'.png')))
        assert page.evaluate('document.documentElement.scrollWidth===innerWidth')
        dimensions = {}
        if page.locator('#project-detail').is_visible():
            for selector in ('.project-sections','.project-views'):
                links = page.locator(selector+' a')
                if links.count():
                    positions = links.evaluate_all('els=>els.map(el=>el.getBoundingClientRect().y)')
                    assert len(set(positions)) == 1, (selector,positions)
                    assert page.locator(selector).evaluate('el=>el.scrollWidth<=el.clientWidth'),selector
                    dimensions[selector] = {'links':len(positions),'rows':len(set(positions))}
        result['captures'].append({'name':name,**dimensions})
    def themes(name):
        for width in (1280,1440):
            page.set_viewport_size({'width':width,'height':1024})
            for theme in ('light','dark'):
                keys.theme(theme)
                capture(f'{name}-{theme}-{width}')
    try:
        page.goto(BASE+'#/rules/l00');page.wait_for_load_state('networkidle')
        expect(page.locator('#navigation-breadcrumb nav')).to_be_visible()
        (OUT / 'rules-initial.html').write_text(page.content())
        page.screenshot(path=str(OUT / 'rules-initial.png'))
        breadcrumb = page.locator('#navigation-breadcrumb nav').bounding_box()
        heading = page.locator('#view-rules h1').bounding_box()
        result['breadcrumb'] = {'x':breadcrumb['x'], 'y':breadcrumb['y'], 'heading_x':heading['x']}
        visit('#/projects');themes('project-collection')
        page.goto(BASE+PROJECT);page.wait_for_load_state('networkidle')
        expect(page.locator('#project-title')).to_be_visible()
        (OUT / 'project-initial.html').write_text(page.content())
        page.screenshot(path=str(OUT / 'project-initial.png'))
        positions = page.locator('#project-detail-body > .tabs .tab').evaluate_all('els=>els.map(el=>el.getBoundingClientRect().y)')
        result['flat_tab_rows'] = len(set(positions))
        assert breadcrumb['x'] == heading['x'] and breadcrumb['y'] >= 24, result
        assert not positions, result
        expect(page.get_by_role('navigation',name='Project sections').get_by_role('link')).to_have_count(5)
        expect(page.locator('.project-views')).to_have_count(0)
        themes('project-overview')
        # Each legacy query value selects its group, page and the same reader.
        groups = [
            ('instructions',[('context','Instruction context'),('inventory','Instruction ownership'),('topology','Topology')]),
            ('copies',[('copies','Working copies'),('availability','Availability')]),
            ('sessions',[('sessions','Session evidence'),('loads','Loading reports')]),
            ('signals',[('rules','Rules'),('exposure','Exposure'),('recurrence','Recurrence')]),
        ]
        visited = ['summary']
        for group, views in groups:
            link=page.locator('#project-section-'+group)
            keys.activate(link);page.wait_for_load_state('networkidle')
            expect(link).to_be_focused();expect(link).to_have_attribute('aria-current','true')
            expect(page.locator('.project-views a')).to_have_count(len(views))
            for view,label in views:
                link=page.locator('.project-views').get_by_role('link',name=label,exact=True)
                keys.activate(link);page.wait_for_load_state('networkidle')
                expect(link).to_be_focused();expect(link).to_have_attribute('aria-current','page')
                assert '?tab='+view in page.url,page.url
                expect(page.locator('.project-navigation [aria-current="page"]')).to_have_count(1)
                expect(page.locator('#project-title')).to_be_visible()
                assert page.locator('#project-detail-body .panel').count()>0
                visual.themes(keys,'keyboard-project-'+view,OUT,target=page.locator('#project-title'))
                visited.append(view)
        assert len(set(visited))==11
        result['views']=visited
        visit(PROJECT+'?tab=inventory')
        expect(page.locator('#project-view-inventory')).to_have_attribute('aria-current','page')
        expect(page.locator('#inventory-status')).to_contain_text('1 shown')
        themes('project-instructions')
        keys.activate(page.locator('#project-view-topology'));page.wait_for_load_state('networkidle')
        expect(page.locator('#project-section-instructions')).to_have_attribute('aria-current','true')
        page.go_back();page.wait_for_load_state('networkidle')
        expect(page.locator('#project-view-inventory')).to_have_attribute('aria-current','page')
        page.go_forward();page.reload();page.wait_for_load_state('networkidle')
        expect(page.locator('#project-view-topology')).to_have_attribute('aria-current','page')
        expect(page.get_by_role('heading',name='Observed instruction wiring',exact=True)).to_be_visible()
        # A real reader failure remains actionable within its selected group.
        keys.activate(page.locator('#project-view-inventory'));page.wait_for_load_state('networkidle')
        page.route('**/api/project-inventory?*',lambda route:route.fulfill(status=503,json={'detail':'Invented inventory read failure'}))
        keys.activate(page.locator('#project-refresh'))
        expect(page.locator('#project-detail-body [role="alert"]')).to_contain_text('Invented inventory read failure')
        expect(page.locator('#project-view-inventory')).to_have_attribute('aria-current','page')
        capture('project-reader-error-dark-1440')
        page.unroute('**/api/project-inventory?*')
        keys.activate(page.locator('#project-refresh'))
        expect(page.locator('#project-detail-body [role="alert"]')).to_have_count(0)
        expect(page.locator('#inventory-status')).to_contain_text('1 shown')
        expect(page.locator('#project-refresh')).to_be_focused()
        # The two shared breadcrumb routes use the same inset and remain real links.
        for name,route,label,view in [
            ('rules','#/rules/l00','Rules','#view-rules'),
            ('review','#/review/command/'+manifest['command'],'Review queue','#view-review'),
        ]:
            visit(route)
            expect(page.locator('#navigation-breadcrumb')).to_contain_text(label)
            themes(name+'-breadcrumb')
            bread=page.locator('#navigation-breadcrumb nav').bounding_box()
            content=page.locator(view).bounding_box()
            assert bread['x']==content['x']+28 and bread['y']==24,(bread,content)
            result[name+'_breadcrumb']={'x':bread['x'],'y':bread['y']}
            link=page.locator('#navigation-breadcrumb').get_by_role('link',name=label,exact=True)
            keys.activate(link);page.wait_for_load_state('networkidle')
            expect(page.locator('#navigation-breadcrumb')).to_be_empty()
            assert page.locator(view+' h1').bounding_box()['y']==24
        # Direct entry on a session-specific query keeps its filter and chosen view.
        visit(PROJECT+'?tab=loads&logical_session_key='+'e'*64)
        expect(page.locator('#project-view-loads')).to_have_attribute('aria-current','page')
        expect(page.locator('#project-section-sessions')).to_have_attribute('aria-current','true')
        page.reload();page.wait_for_load_state('networkidle')
        assert 'logical_session_key=' in page.url
        expect(page.locator('#native-loads-status')).to_contain_text('0 of 0')
        expect(page.locator('#project-detail-body')).to_contain_text('This does not mean no instructions loaded')
        assert all(method=='GET' for method,url in result['requests'])
        assert all(Path(path).read_text()==text for path,text in manifest['targets'].items())
        with sqlite3.connect('file:'+manifest['db']+'?mode=ro',uri=True) as db:
            assert list(db.iterdump())==manifest['snapshot']
            assert db.execute('SELECT COUNT(*) FROM llm_calls').fetchone()[0]==0
        assert not result['errors'],result['errors']
        result.update({'store':'unchanged','targets':'unchanged','models':0})
        visual.assert_clean()
        print('NAVIGATION_LAYOUT_OK: '+json.dumps({key:result[key] for key in ['views','breadcrumb','store','targets','models']}))
    except BaseException:
        page.screenshot(path=str(OUT / 'failure.png'))
        (OUT / 'failure.html').write_text(page.content())
        raise
    finally:
        keys.save()
        browser.close()
        (OUT / 'result.json').write_text(json.dumps(result, indent=2))
