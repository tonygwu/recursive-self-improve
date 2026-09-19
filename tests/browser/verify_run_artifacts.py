"""Actual file downloads, interrupted evaluations and keyboard reads on fixtures."""
from pathlib import Path
import json,sqlite3
from playwright.sync_api import sync_playwright,expect
out=Path(__file__).resolve().parents[2]/'reports/dashboard-parity'
manifest=json.loads((out/'run-artifacts-manifest.json').read_text());assert 'si-run-ui-' in manifest['db']
base='http://127.0.0.1:8877/'
def dump():
    with sqlite3.connect('file:'+manifest['db']+'?mode=ro',uri=True) as db:return list(db.iterdump())
before=dump()
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True);page=browser.new_page(viewport={'width':1440,'height':1024},accept_downloads=True)
    errors=[];requests=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    page.on('request',lambda request:requests.append((request.method,request.url)))
    try:
        page.goto(base+'#/overview/run/fixture-run');page.wait_for_load_state('networkidle')
        page.screenshot(path=str(out/'run-before.png'))
        print('RUN_RECON',page.get_by_role('button').all_text_contents())
        assert page.locator('#run-stage-gate').inner_text().find('refused: 2')>=0
        for width in (1440,1280):
            for theme in ('light','dark'):
                page.set_viewport_size({'width':width,'height':1024})
                if page.evaluate('async()=>(await import("/app.js")).effectiveTheme()')!=theme:
                    page.get_by_role('button',name='Dark theme' if theme=='dark' else 'Light theme',exact=True).click()
                page.locator('#main').evaluate('(el)=>el.scrollTop=0')
                page.screenshot(path=str(out/f'run-{theme}-{width}.png'))
                assert page.locator('#run-detail').evaluate('(el)=>el.scrollWidth<=el.clientWidth+1')
        page.get_by_text('Call budgets and caps',exact=True).click()
        assert '0 / 10' in page.locator('.run-budget').inner_text()
        page.get_by_text('Time accounting',exact=True).click()
        assert 'Unaccounted: 75 s' in page.locator('#run-detail').inner_text()
        page.get_by_role('button',name='Retained files',exact=True).click()
        assert page.evaluate('document.activeElement.id')=='run-section-artifacts'
        page.get_by_role('button',name='Load retained files',exact=True).click()
        expect(page.locator('#run-status-artifacts')).to_have_text('20 of 25 retained records shown')
        page.locator('#run-older-artifacts').click()
        expect(page.locator('#run-status-artifacts')).to_have_text('25 of 25 retained records shown')
        assert page.evaluate('document.activeElement.id')=='run-status-artifacts'
        page.locator('[data-run-artifact="fixture-call.a1.codex.stdout"]').click()
        preview=page.get_by_label('Artifact text preview',exact=True)
        expect(preview).to_contain_text('<script>fixture text, never executed</script>')
        assert page.locator('#run-section-artifacts script').count()==0
        assert 'bytes omitted from preview' in page.locator('#run-section-artifacts').inner_text()
        preview.focus();page.keyboard.press('End')
        page.wait_for_function('document.activeElement.scrollTop > 0')
        assert preview.evaluate('(el)=>el.scrollTop>0')
        with page.expect_download() as info:page.get_by_role('link',name='Download complete file',exact=True).click()
        download=info.value
        payload=Path(download.path()).read_bytes();assert len(payload)==manifest['long_bytes'] and payload.endswith(b'output\n')
        page.screenshot(path=str(out/'run-artifact-preview.png'))
        # Concurrent repaint retains the active preview instead of dropping focus.
        preview.focus()
        page.evaluate('async()=>{const app=await import("/app.js");await app.loadRunRecords("calls");}')
        assert page.evaluate('document.activeElement.id')=='artifact-text-fixture-call.a1.codex.stdout'
        # Same-time chooser and reload retain exact identity.
        page.locator('#run-selector').select_option('same-time');page.wait_for_load_state('networkidle')
        assert page.url.endswith('/run/same-time')
        page.reload();page.locator('#run-title').wait_for()
        page.get_by_text('Call budgets and caps',exact=True).click()
        assert 'Budget limits were not recorded' in page.locator('.run-budget').inner_text()
        # A real interrupted reservation stays visible without a completed result.
        page.goto(base+'#/overview/run/'+manifest['job_run']);page.wait_for_load_state('networkidle')
        page.get_by_text('Call budgets and caps',exact=True).click()
        assert '0 completed calls · 1 unresolved reservations' in page.locator('.run-budget').inner_text()
        assert page.locator('.run-budget a').get_attribute('href')=='#/review/command/'+manifest['command']
        page.get_by_role('button',name='Load evaluation attempts',exact=True).click()
        link=page.locator('#run-section-attempts a').first
        assert link.get_attribute('href')=='#/evals/attempt/'+manifest['attempt']
        page.screenshot(path=str(out/'run-interrupted-job.png'))
        link.click();page.locator('#evaluation-title').wait_for()
        assert manifest['attempt'] in page.url and 'Interrupted evaluation' in page.locator('#main').inner_text()
        # The exact frozen command is reachable independently of history pagination.
        page.goto(base+'#/overview/run/'+manifest['job_run']);page.locator('#run-title').wait_for()
        if not page.locator('.run-budget').evaluate('(el)=>el.open'):
            page.get_by_text('Call budgets and caps',exact=True).click()
        page.locator('.run-budget a').click()
        expect(page.locator('#review-execution')).to_contain_text('Selected command: '+manifest['command'])
        expect(page.locator('#delivery-command-'+manifest['command'])).to_be_visible()
        assert dump()==before
        assert all(Path(path).read_text()==text for path,text in manifest['targets'].items())
        assert all(method=='GET' for method,_ in requests) and not errors,errors
        print('RUN_ARTIFACTS_BROWSER_OK',json.dumps({'requests':len(requests),'model_calls':0,'errors':errors,'read_only':True}))
    except Exception:
        page.screenshot(path=str(out/'run-browser-failure.png'),full_page=True)
        (out/'run-browser-failure.txt').write_text(page.locator('body').inner_text())
        raise
    finally:browser.close()
