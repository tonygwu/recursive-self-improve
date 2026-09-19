"""Drive the shipped Projects table against the temporary real-reader fixture."""
from pathlib import Path
import hashlib
import json
import sqlite3
from urllib.parse import quote
from playwright.sync_api import sync_playwright, expect

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'reports/dashboard-parity/projects-table'
manifest=json.loads((OUT/'manifest.json').read_text())
assert 'si-projects-ui-' in manifest['db']
BASE='http://127.0.0.1:8876/'
result={'captures':[],'requests':[],'errors':[]}
with sync_playwright() as runtime:
    browser=runtime.chromium.launch(headless=True)
    page=browser.new_page(viewport={'width':1280,'height':1024})
    page.on('pageerror',lambda error:result['errors'].append(str(error)))
    page.on('request',lambda request:result['requests'].append([request.method,request.url]))
    def capture(name):
        page.screenshot(path=str(OUT/(name+'.png')))
        dimensions=page.locator('#projects-table-wrap').evaluate('el=>({width:el.clientWidth,scroll:el.scrollWidth,table:el.querySelector("table").getBoundingClientRect().width})')
        assert dimensions['width']==dimensions['scroll'],dimensions
        assert page.evaluate('document.documentElement.scrollWidth===innerWidth')
        result['captures'].append({'name':name,**dimensions})
    def search(text):
        page.get_by_role('searchbox',name='Find a repository').fill(text)
        page.get_by_role('button',name='Search',exact=True).click()
        expect(page.locator('#projects-page-status')).to_contain_text('matching' if text else 'repositories')
    try:
        page.goto(BASE+'#/projects');page.wait_for_load_state('networkidle')
        expect(page.locator('#projects-tbody > tr[data-project-key]')).to_have_count(10)
        (OUT/'initial.html').write_text(page.content())
        assert len(page.locator('#view-projects button').all())>10
        # Inspect both widths/themes after the recorded initial reconnaissance.
        for width in (1280,1440):
            page.set_viewport_size({'width':width,'height':1024})
            for theme in ('light','dark'):
                current=page.evaluate('async()=>(await import("/app.js")).effectiveTheme()')
                if current!=theme:page.get_by_role('button',name=theme.title()+' theme',exact=True).click()
                page.locator('#main').evaluate('el=>el.scrollTop=0')
                capture(f'projects-{theme}-{width}')
        # Native keyboard disclosure must toggle only once and preserve focus.
        expand=page.get_by_role('button',name='Expand indexed copies of alpha',exact=True)
        expand.focus();page.keyboard.press('Enter')
        collapse=page.get_by_role('button',name='Collapse indexed copies of alpha',exact=True)
        expect(collapse).to_be_focused();expect(collapse).to_have_attribute('aria-expanded','true')
        assert page.locator('.project-copies li').count()==2
        assert 'alpha-retained-long-working-copy-path' in page.locator('.project-copies').inner_text()
        capture('projects-expanded-dark-1440')
        page.keyboard.press('Space')
        expect(page.get_by_role('button',name='Expand indexed copies of alpha',exact=True)).to_be_focused()
        expect(page.locator('.project-copies')).to_have_count(0)
        # Three pages; page changes survive browser Back and Reload.
        page.get_by_role('button',name='Next',exact=True).click()
        expect(page.locator('#projects-page-status')).to_contain_text('11–20 of 24')
        page.get_by_role('button',name='Next',exact=True).click()
        expect(page.locator('#projects-tbody > tr[data-project-key]')).to_have_count(4)
        page.reload();page.wait_for_load_state('networkidle')
        expect(page.locator('#projects-page-status')).to_contain_text('21–24 of 24')
        # Hidden copy-path match, then long repository display without truncating its name.
        search('alpha-retained-long-working-copy-path')
        expect(page.locator('#projects-tbody > tr[data-project-key]')).to_have_count(1)
        expect(page.locator('#projects-page-status')).to_contain_text('1 matching repositories · 24 total')
        link=page.locator('.project-name');link.focus();page.keyboard.press('Enter')
        expect(page.locator('#project-detail')).to_be_visible()
        page.go_back();expect(page.locator('#view-projects')).to_be_visible()
        expect(page.locator('#projects-search')).to_have_value('alpha-retained-long-working-copy-path')
        search('exceptionally-long')
        expect(page.locator('.project-name')).to_contain_text('exceptionally-long')
        full_name=page.locator('.project-name').inner_text()
        assert len(full_name)>50
        page.set_viewport_size({'width':1280,'height':1024})
        page.get_by_role('button',name='Expand indexed copies of '+full_name,exact=True).click()
        expect(page.locator('.project-copies')).to_contain_text(full_name)
        capture('projects-long-name-dark-1280')
        # Known zero sorts ahead of unknown in ascending order, unknown stays last in both.
        page.get_by_role('button',name='Clear',exact=True).click()
        page.locator('#project-sort-rate button').click()
        expect(page.locator('#project-sort-rate')).to_have_attribute('aria-sort','descending')
        page.locator('#project-sort-rate button').focus();page.keyboard.press('Enter')
        expect(page.locator('#project-sort-rate')).to_have_attribute('aria-sort','ascending')
        expect(page.locator('#project-sort-rate button')).to_be_focused()
        first=page.locator('#projects-tbody > tr[data-project-key]').first
        expect(first.locator('td').nth(3)).to_contain_text('0.0')
        for _ in range(2):page.get_by_role('button',name='Next',exact=True).click()
        expect(page.locator('#projects-tbody > tr[data-project-key]').last).to_contain_text('example/retired')
        search('no-repository-matches')
        expect(page.locator('#projects-tbody')).to_contain_text('No repositories match')
        capture('projects-no-match-dark-1280')
        # Fault injection changes only the read transport; it does not fabricate measurements.
        page.route('**/api/projects',lambda route:route.fulfill(status=503,json={'detail':'Invented read failure'}))
        page.reload();page.wait_for_load_state('networkidle')
        expect(page.locator('#projects-read-state')).to_contain_text('Could not read repositories')
        expect(page.locator('#projects-tbody')).to_contain_text('Repository data is unavailable')
        capture('projects-read-error-dark-1280')
        page.unroute('**/api/projects')
        page.get_by_role('button',name='Retry repositories',exact=True).click()
        expect(page.locator('#projects-read-state')).to_be_empty()
        expect(page.get_by_role('button',name='Refresh',exact=True)).to_be_focused()
        expect(page.locator('#projects-tbody')).to_contain_text('No repositories match')
        expect(page.locator('#global-error')).not_to_be_visible()
        page.get_by_role('button',name='Clear',exact=True).click()
        # A delayed real response shows progress and disables duplicate refresh clicks.
        pending=[]
        page.route('**/api/projects',lambda route:pending.append(route))
        page.get_by_role('button',name='Refresh',exact=True).click()
        expect(page.locator('#projects-read-state')).to_contain_text('Reading repositories')
        expect(page.get_by_role('button',name='Refresh',exact=True)).to_be_disabled()
        assert len(pending)==1
        pending.pop().continue_();page.unroute('**/api/projects')
        expect(page.locator('#projects-read-state')).to_be_empty()
        # Empty indexed dataset, delivered as an explicit disposable test response.
        empty={**manifest['payload'],'rows':[],'count':0,'clone_paths_total':0,'sessions_total':0,'incidents_total':0}
        page.route('**/api/projects',lambda route:route.fulfill(json=empty))
        page.get_by_role('button',name='Refresh',exact=True).click()
        expect(page.locator('#projects-tbody')).to_contain_text('No indexed repositories')
        expect(page.locator('#projects-benefit-note')).to_contain_text('No repository comparisons are available yet')
        capture('projects-empty-dark-1280')
        page.unroute('**/api/projects');page.get_by_role('button',name='Refresh',exact=True).click()
        expect(page.locator('#projects-tbody > tr[data-project-key]')).to_have_count(10)
        page.get_by_text('Coverage and exclusions',exact=False).click()
        expect(page.locator('#projects-notes')).to_be_visible()
        expect(page.locator('#projects-notes').get_by_role('heading',name='Proposals that belong to no repo',exact=True)).to_be_visible()
        page.get_by_role('button',name='Refresh',exact=True).focus()
        page.keyboard.press('Tab')
        expect(page.locator('#projects-table-wrap')).to_be_focused()
        assert page.locator('#projects-table-wrap').evaluate('el=>getComputedStyle(el).outlineStyle')=='solid'
        page.keyboard.press('End')
        with sqlite3.connect('file:'+manifest['db']+'?mode=ro',uri=True) as db:
            assert list(db.iterdump())==manifest['snapshot']
            assert db.execute('SELECT COUNT(*) FROM llm_calls').fetchone()[0]==0
        assert all(Path(path).read_text()==text for path,text in manifest['targets'].items())
        assert not result['errors'],result['errors']
        assert all(method=='GET' and url.startswith(BASE) for method,url in result['requests'])
        result.update(read_only=True,model_calls=0,rows=manifest['rows'])
        result['source_hashes']={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
          for p in (ROOT/'src/self_improve/dashboard/static/app.js',ROOT/'src/self_improve/dashboard/static/app.css',ROOT/'src/self_improve/dashboard/static/index.html',ROOT/'src/self_improve/dashboard/queries.py')}
        (OUT/'result.json').write_text(json.dumps(result,indent=2))
        print(f'PROJECTS_TABLE_OK: {len(result["captures"])} captures; 24 repositories; {len(result["requests"])} GETs; unchanged Store/targets; zero page errors/model calls')
    except Exception:
        page.screenshot(path=str(OUT/'failure.png'),full_page=True)
        (OUT/'failure.html').write_text(page.content())
        raise
    finally:browser.close()
