"""GET-only Project session inspection using the disposable native scan fixture."""
from pathlib import Path
from urllib.parse import quote
import json,sqlite3
from playwright.sync_api import sync_playwright
out=Path(__file__).resolve().parents[2]/'reports/dashboard-parity'
info=json.loads((out/'session-demo-manifest.json').read_text())
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True);page=browser.new_page(viewport={'width':1440,'height':1024})
    errors=[];requests=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    page.on('request',lambda r:requests.append((r.method,r.url)))
    try:
        url='http://127.0.0.1:8876/#/projects/'+quote(info['project_key'],safe='')+'?tab=sessions'
        page.goto(url);page.wait_for_load_state('networkidle')
        page.screenshot(path=str(out/'session-before.png'));(out/'session-before.html').write_text(page.content())
        assert page.locator('#sessions-status').inner_text()=='20 of 23 session/copy records shown'
        assert '3 with unknown creation' in page.locator('#project-detail-body').inner_text()
        page.get_by_role('button',name='Load more sessions',exact=True).focus();page.keyboard.press('Enter')
        page.get_by_text('23 of 23 session/copy records shown',exact=True).wait_for()
        assert page.locator('.session-record').count()==23
        assert page.evaluate('document.activeElement.id')=='sessions-status'
        assert page.get_by_role('button',name='Load more sessions',exact=True).count()==0
        page.get_by_label('Delivered revision',exact=True).select_option(info['revision'])
        page.get_by_role('button',name='Inspect sessions',exact=True).click()
        page.get_by_text('20 of 23 session/copy records shown',exact=True).wait_for()
        assert 'rule_revision_id='+info['revision'] in page.url and 'compatibility_key=' in page.url
        assert page.evaluate('document.activeElement.id')=='sessions-submit'
        page.get_by_role('button',name='Load more sessions',exact=True).click()
        page.get_by_text('23 of 23 session/copy records shown',exact=True).wait_for()
        candidate=page.locator('.session-record').filter(has_text='Startup candidate from file checks').first
        candidate.locator(':scope > details > summary').click()
        assert 'Continuous runtime availability is unverified.' in candidate.inner_text()
        candidate.get_by_text('Supporting availability checks and interval limits',exact=True).click()
        candidate.get_by_text('Complete native metadata and scan identities',exact=True).click()
        old=page.locator('.session-record').filter(has_text='No startup credit').first
        old.locator(':scope > details > summary').click()
        assert 'started before observed availability' in old.inner_text()
        old.locator(':scope > details > summary').click()
        # Refresh retains disclosure and keyboard focus, without mutating evidence.
        page.get_by_role('button',name='Refresh session evidence',exact=True).click()
        page.get_by_text('20 of 23 session/copy records shown',exact=True).wait_for()
        assert page.evaluate('document.activeElement.id')=='sessions-refresh'
        assert candidate.locator('details[open]').count()==3
        selected_url=page.url
        for width in (1440,1280):
            page.set_viewport_size({'width':width,'height':1024})
            for theme in ('light','dark'):
                page.evaluate('(t)=>document.documentElement.dataset.theme=t',theme)
                page.locator('#main').evaluate('(e)=>e.scrollTop=0')
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                assert page.locator('.session-filters').evaluate('(e)=>e.scrollWidth<=e.clientWidth+1')
                page.screenshot(path=str(out/f'session-{theme}-{width}.png'))
        page.reload();page.wait_for_load_state('networkidle')
        assert page.url==selected_url and page.get_by_label('Delivered revision',exact=True).input_value()==info['revision']
        assert page.get_by_label('Detector / parser version',exact=True).input_value()
        page.route('**/api/project-sessions?*',lambda route:route.abort())
        page.get_by_role('button',name='Refresh session evidence',exact=True).click()
        page.locator('#project-detail-body [role="alert"]').wait_for()
        assert page.locator('.session-record').count()==20  # prior evidence survives a failed read
        page.unroute('**/api/project-sessions?*')
        page.get_by_role('button',name='Refresh session evidence',exact=True).click()
        page.get_by_text('20 of 23 session/copy records shown',exact=True).wait_for()
        page.wait_for_load_state('networkidle')
        assert page.locator('#project-detail-body [role="alert"]').count()==0
        assert all(method=='GET' for method,url in requests if url.startswith('http://127.0.0.1:8876'))
        assert not errors,errors
        assert Path(info['target']).read_text()==info['target_text']
        with sqlite3.connect(info['db']) as db:
            assert list(db.iterdump())==info['snapshot']
            assert db.execute('SELECT COUNT(*) FROM llm_calls').fetchone()[0]==0
        print('SESSION_BROWSER_OK: 20+3; version/revision filters, candidates/exclusions, unknown native starts, keyboard/focus, reload, disclosures, both themes and widths, read failure/recovery, GET-only and unchanged Store/target; architecture data cards inspected')
    except Exception:
        page.screenshot(path=str(out/'session-failure.png'));(out/'session-failure.html').write_text(page.content());raise
    finally:browser.close()
