"""Browse native receipts without opening their reported files or writing state."""
from pathlib import Path
from urllib.parse import quote
import json,sqlite3
from playwright.sync_api import sync_playwright
out=Path(__file__).resolve().parents[2]/'reports/dashboard-parity'
info=json.loads((out/'native-demo-manifest.json').read_text())
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True);page=browser.new_page(viewport={'width':1440,'height':1024})
    errors=[];requests=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    page.on('request',lambda r:requests.append((r.method,r.url)))
    try:
        url='http://127.0.0.1:8876/#/projects/'+quote(info['project_key'],safe='')+'?tab=loads'
        page.goto(url);page.wait_for_load_state('networkidle')
        page.screenshot(path=str(out/'native-before.png'));(out/'native-before.html').write_text(page.content())
        assert page.locator('#native-loads-status').inner_text()=='20 of 23 native reports shown'
        assert 'do not measure instructions in force' in page.locator('#project-detail-body').inner_text()
        page.get_by_role('button',name='Load more reports',exact=True).focus();page.keyboard.press('Enter')
        page.get_by_text('23 of 23 native reports shown',exact=True).wait_for()
        assert page.locator('.session-record').count()==23
        assert page.evaluate('document.activeElement.id')=='native-loads-status'
        card=page.locator('.session-record').filter(has_text='Instruction loaded').first
        card.locator(':scope > details > summary').click()
        card.get_by_text('Complete native report and receiver provenance',exact=True).click()
        assert '"loaded_content_hash": null' in card.inner_text()
        assert '"occurred_at": null' in card.inner_text()
        assert '"source_authentication": "local_unattested"' in card.inner_text()
        page.get_by_role('button',name='Refresh loading reports',exact=True).click()
        page.get_by_text('20 of 23 native reports shown',exact=True).wait_for()
        assert page.evaluate('document.activeElement.id')=='native-loads-refresh'
        assert card.locator('details[open]').count()==2
        for width in (1440,1280):
            page.set_viewport_size({'width':width,'height':1024})
            for theme in ('light','dark'):
                page.evaluate('(t)=>document.documentElement.dataset.theme=t',theme)
                page.locator('#main').evaluate('(e)=>e.scrollTop=0')
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                page.screenshot(path=str(out/f'native-{theme}-{width}.png'))
        card.get_by_role('link',name='All reports for this session',exact=True).click()
        page.get_by_text('8 of 8 native reports shown',exact=True).wait_for()
        assert 'logical_session_key=' in page.url
        selected=page.url;page.reload();page.wait_for_load_state('networkidle')
        assert page.url==selected and page.locator('.session-record').count()==8
        page.get_by_role('link',name='Show all project reports',exact=True).click()
        page.get_by_text('20 of 23 native reports shown',exact=True).wait_for()
        page.route('**/api/project-native-events?*',lambda route:route.abort())
        page.get_by_role('button',name='Refresh loading reports',exact=True).click()
        page.locator('#project-detail-body [role="alert"]').wait_for()
        assert page.locator('.session-record').count()==20
        page.unroute('**/api/project-native-events?*')
        page.get_by_role('button',name='Refresh loading reports',exact=True).click()
        page.get_by_text('20 of 23 native reports shown',exact=True).wait_for();page.wait_for_load_state('networkidle')
        assert not page.locator('#project-detail-body [role="alert"]').count()
        page.goto(url+'&logical_session_key='+'e'*64);page.wait_for_load_state('networkidle')
        page.get_by_text('0 of 0 native reports shown',exact=True).wait_for()
        assert 'This does not mean no instructions loaded.' in page.locator('#project-detail-body').inner_text()
        assert all(method=='GET' for method,url in requests if url.startswith('http://127.0.0.1:8876'))
        assert not errors,errors
        assert Path(info['target']).read_text()==info['target_text']
        with sqlite3.connect(info['db']) as db:
            assert list(db.iterdump())==info['snapshot']
            assert db.execute('SELECT COUNT(*) FROM llm_calls').fetchone()[0]==0
        print('NATIVE_BROWSER_OK: 20+3; native load/boundary details, session filter, unknown time/bytes, empty coverage, keyboard/focus, reload, disclosures, error recovery, both themes/widths, GET-only and unchanged Store/target')
    except Exception:
        page.screenshot(path=str(out/'native-failure.png'));(out/'native-failure.html').write_text(page.content());raise
    finally:browser.close()
