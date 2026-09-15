"""Verify the local evaluation dashboard; run against eval_dashboard_demo.py."""
from pathlib import Path
import hashlib,json
from playwright.sync_api import sync_playwright
out=Path(__file__).resolve().parents[2]/"reports/dashboard-parity"
out.mkdir(parents=True, exist_ok=True)
info=json.loads((out/'eval-demo-manifest.json').read_text())
before=hashlib.sha256(Path(info['db']).read_bytes()).hexdigest()
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True)
    page=browser.new_page(viewport={'width':1440,'height':1024})
    errors=[];requests=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    page.on('request',lambda r:requests.append((r.method,r.url)))
    try:
        page.goto('http://127.0.0.1:8876/#/evals');page.wait_for_load_state('networkidle')
        page.get_by_text('20 shown · 23 recorded attempts',exact=True).wait_for()
        page.get_by_text('23 recorded evaluation attempts.',exact=True).wait_for()
        assert '23 historical result rows without an attempt link' in page.locator('#trend-gate').inner_text()
        history=page.locator('#evaluation-history-body')
        assert '3 of 3 scenario results' in history.inner_text()
        assert '9 / 9 passed' in history.inner_text() and '0 / 9 passed' in history.inner_text()
        region=page.get_by_role('region',name='Evaluation history records');region.focus();page.keyboard.press('End')
        page.wait_for_function('document.querySelector(".eval-cards").scrollTop > 0')
        older=page.get_by_role('button',name='Older records',exact=True);older.focus();page.keyboard.press('Enter')
        page.get_by_text('3 shown · 23 recorded attempts',exact=True).wait_for()
        assert page.evaluate('document.activeElement.id')=='evaluation-page-status'
        assert 'eval_cursor=' in page.url
        page.reload();page.wait_for_load_state('networkidle');page.get_by_text('3 shown · 23 recorded attempts',exact=True).wait_for()
        page.get_by_role('button',name='Newest records',exact=True).click()
        page.get_by_text('20 shown · 23 recorded attempts',exact=True).wait_for()
        # Existing trend filters and history selection retain independent state.
        page.get_by_label('Last month (UTC)').fill('2025-09');page.get_by_role('button',name='Show seven months').click()
        page.wait_for_url('**/*end_month=2025-09*');page.wait_for_load_state('networkidle')
        page.get_by_text('20 shown · 23 recorded attempts',exact=True).wait_for()
        first=page.locator('#evaluation-link-'+info['attempt'])
        first.focus();page.keyboard.press('Enter')
        title=page.get_by_role('heading',name='Evaluation attempt',exact=True);title.wait_for()
        assert page.evaluate('document.activeElement.id')=='evaluation-title'
        assert 'end_month=2025-09' in page.url
        detail=page.locator('#evaluation-detail')
        assert detail.get_by_role('heading',name='Scenario 1 · Invented formatting check',exact=True).count()==1
        assert detail.get_by_role('heading',name='Scenario 3 · Invented formatting check',exact=True).count()==1
        assert 'Fewer than 20 valid trials per arm' in detail.inner_text()
        assert '+100 percentage points' not in detail.inner_text()
        detail.get_by_text('Complete frozen source, settings and revision identities',exact=True).click()
        detail.get_by_text('Model calls for this scenario (7)',exact=True).first.click()
        detail.get_by_text('Complete specification and content hash',exact=True).first.click()
        assert 'codex / gpt-5.6-terra' in detail.inner_text()
        with page.expect_response(lambda r:r.url.endswith('/api/eval-attempts/'+info['attempt'])):
            page.get_by_role('button',name='Refresh evidence',exact=True).click()
        page.wait_for_function('document.querySelectorAll("#evaluation-detail details[open]").length >= 3')
        assert detail.locator('details[open]').count()>=3
        assert page.evaluate('document.activeElement.id')=='evaluation-detail-refresh'
        for width in (1440,1280):
            page.set_viewport_size({'width':width,'height':1024})
            for theme in ('light','dark'):
                page.evaluate('(theme)=>document.documentElement.dataset.theme=theme',theme)
                page.locator('#main').evaluate('(e)=>e.scrollTop=0')
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                assert detail.evaluate('(e)=>e.scrollWidth<=e.clientWidth+1')
                assert page.locator('.eval-notice').evaluate('(e)=>getComputedStyle(e).backgroundColor')!='rgba(0, 0, 0, 0)'
                page.screenshot(path=str(out/f'eval-detail-{theme}-{width}.png'))
        page.reload();page.wait_for_load_state('networkidle');title.wait_for()
        assert info['attempt'] in page.url and 'end_month=2025-09' in page.url
        detail.get_by_role('link',name='Producing run',exact=True).click()
        page.locator('#run-title').wait_for();page.get_by_role('button',name='Load linked evaluations',exact=True).click()
        page.get_by_text('3 of 3 retained records shown',exact=True).wait_for()
        page.get_by_role('link',name='Inspect complete evaluation',exact=True).first.click();title.wait_for()
        detail.get_by_role('link',name='Preview regeneration cost…',exact=True).click()
        page.get_by_role('heading',name='Regenerate evaluation',exact=True).wait_for()
        page.get_by_text('At most 21 model calls.',exact=True).wait_for()
        assert all(method=='GET' for method,_ in requests),requests
        page.goto('http://127.0.0.1:8876/#/evals?eval_learning_id='+info['learning']+'&end_month=2025-09');page.wait_for_load_state('networkidle')
        page.get_by_text('20 shown · 23 recorded attempts',exact=True).wait_for()
        history.get_by_text('Filter attempts by exact source ID',exact=True).click()
        history.get_by_label('Exact ID').fill('missing-source');history.get_by_role('button',name='Show history',exact=True).click()
        page.get_by_text('0 shown · 0 recorded attempts',exact=True).wait_for()
        assert 'end_month=2025-09' in page.url
        history.locator('#evaluation-kind').select_option('unlinked');history.get_by_role('button',name='Show history',exact=True).click()
        page.get_by_text('20 shown · 23 unlinked results',exact=True).wait_for()
        assert 'eval_learning_id=' not in page.url
        history.get_by_role('button',name='Older records',exact=True).click()
        page.get_by_text('3 shown · 23 unlinked results',exact=True).wait_for()
        page.reload();page.wait_for_load_state('networkidle');page.get_by_text('3 shown · 23 unlinked results',exact=True).wait_for()
        history.get_by_role('button',name='Newest records',exact=True).click();page.get_by_text('20 shown · 23 unlinked results',exact=True).wait_for()
        history.locator('#evaluation-link-'+info['historical']).click()
        page.get_by_role('heading',name='Historical evaluation',exact=True).wait_for()
        assert 'Without-rule rerun' in detail.inner_text()
        assert 'No paired change is calculated' in detail.inner_text()
        page.screenshot(path=str(out/'eval-historical.png'))
        page.goto('http://127.0.0.1:8876/#/evals/attempt/absent');page.wait_for_load_state('networkidle')
        detail.get_by_role('alert').wait_for()
        assert 'Unknown evaluation attempt' in detail.inner_text()
        # A trend error cannot hide evaluation history from its independent reader.
        page.route('**/api/incident-rate*',lambda route:route.fulfill(status=503,content_type='application/json',body='{"detail":"Invented trend outage"}'))
        page.goto('http://127.0.0.1:8876/#/evals');page.wait_for_load_state('networkidle')
        page.get_by_text('20 shown · 23 recorded attempts',exact=True).wait_for()
        assert 'Invented trend outage' in page.locator('#trends-body').inner_text()
        page.unroute('**/api/incident-rate*');page.get_by_role('button',name='Retry monthly observations',exact=True).click();page.wait_for_load_state('networkidle')
        for width in (1440,1280):
            page.set_viewport_size({'width':width,'height':1024})
            for theme in ('light','dark'):
                page.evaluate('(theme)=>document.documentElement.dataset.theme=theme',theme)
                page.locator('#main').evaluate('(e)=>e.scrollTop=0')
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                page.screenshot(path=str(out/f'eval-history-{theme}-{width}.png'))
        assert not errors,errors
        assert all(method=='GET' for method,_ in requests),requests
    except BaseException:
        page.screenshot(path=str(out/'eval-browser-failure.png'))
        print('FAILURE_URL',page.url,flush=True)
        print(page.locator('#main').inner_text()[-12000:],flush=True)
        print('PAGE_ERRORS',errors,flush=True)
        raise
    browser.close()
assert hashlib.sha256(Path(info['db']).read_bytes()).hexdigest()==before
print('EVAL_BROWSER_OK: 23 attempts, 23 historical rows; pagination, source filters, exact run links, reload, focus, disclosures, both themes, cost preview, independent error, GET-only and unchanged Store')
