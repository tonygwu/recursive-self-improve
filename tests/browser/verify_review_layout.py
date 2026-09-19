"""Complete Review targets and member selection through read-only browser actions."""
from pathlib import Path
import json
import sqlite3
from playwright.sync_api import sync_playwright, expect
from visual_checks import VisualChecks

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'reports/dashboard-parity/review-layout'
info=json.loads((OUT/'manifest.json').read_text())
assert 'si-review-layout-' in info['db']
result={'requests':[],'errors':[],'captures':[]}
with sync_playwright() as runtime:
    browser=runtime.chromium.launch(headless=True)
    page=browser.new_page(viewport={'width':1280,'height':1024})
    visual=VisualChecks(page,OUT/'contrast.json')
    page.on('pageerror',lambda error:result['errors'].append(str(error)))
    page.on('request',lambda request:result['requests'].append([request.method,request.url]))
    def capture(name):
        page.locator('#main').evaluate('el=>el.scrollTop=0')
        page.screenshot(path=str(OUT/(name+'.png')))
        result['captures'].append(name)
    def select(name):
        family=info['families'][name]
        page.evaluate('async id=>{const app=await import("/app.js");app.state.selectedFamily=id;app.paintReview();}',family)
        page.wait_for_load_state('networkidle')
        expect(page.locator('.review-card--open')).to_have_attribute('data-learning-id',family)
    def hints():
        for decision,key in (('approve','a'),('reject','r')):
            button=page.locator(f'[data-decision="{decision}"]')
            expect(button).to_have_attribute('aria-keyshortcuts',key)
            expect(button.locator('kbd')).to_be_visible()
            expect(button.locator('kbd')).to_have_text(key)
            expect(button.locator('kbd')).to_have_attribute('aria-hidden','true')
        # Hints preserve the native controls' accessible names and global scope distinction.
        expect(page.get_by_role('button',name='Reject at selected targets',exact=True)).to_be_visible()
        everywhere=page.get_by_role('button',name='Reject lesson everywhere',exact=True)
        expect(everywhere).to_have_attribute('aria-keyshortcuts','Shift+r')
        expect(everywhere.locator('kbd')).to_have_text('⇧r')
        expect(everywhere.locator('kbd')).to_have_attribute('aria-hidden','true')
        expect(page.locator('.review-card--open')).to_contain_text('selected reviewed targets')
        expect(page.locator('.review-card--open')).to_contain_text('rejects the lesson everywhere')
    try:
        page.goto('http://127.0.0.1:8876/#/review')
        page.wait_for_load_state('networkidle')
        select('mixed')
        capture('initial')
        (OUT/'initial.html').write_text(page.content())
        approve=page.locator('[data-decision="approve"]')
        expect(approve).to_be_enabled()
        hints()
        # A modal search must receive a/r text without deciding the selected family.
        page.keyboard.press('Control+k')
        expect(page.locator('#navigation-input')).to_be_focused()
        page.keyboard.type('ar')
        expect(page.locator('#navigation-input')).to_have_value('ar')
        page.keyboard.press('Escape')
        expect(page.locator('.review-card--open')).to_have_attribute('data-learning-id',info['families']['mixed'])
        result['initial_actions']=page.locator('.review-card__actions').bounding_box()
        assert 'btn--primary' in approve.get_attribute('class')
        assert result['initial_actions']['y']+result['initial_actions']['height']<1024,result['initial_actions']
        for width in (1280,1440):
            page.set_viewport_size({'width':width,'height':1024})
            for theme in ('light','dark'):
                current=page.evaluate('async()=>(await import("/app.js")).effectiveTheme()')
                if current!=theme:page.get_by_role('button',name=theme.title()+' theme',exact=True).click()
                capture(f'mixed-{theme}-{width}')
                hints();visual.check('mixed-shortcuts')
                assert page.locator('#review-body').evaluate('el=>el.scrollWidth<=el.clientWidth')
                assert approve.evaluate('el=>getComputedStyle(el).backgroundColor')!=page.locator('[data-decision="reject"]').evaluate('el=>getComputedStyle(el).backgroundColor')
                box=page.locator('.review-card__actions').bounding_box()
                assert box['y']+box['height']<1024,box
        summaries=page.locator('.review-selected-preview .review-targets > summary')
        expect(summaries).to_have_count(4)
        for path in info['targets']:
            expect(summaries.filter(has_text=path)).to_be_visible()
        expect(page.locator('.review-selected-preview')).to_contain_text('Above the budget; approval remains available.')
        assert all(not item.evaluate('el=>el.open') for item in page.locator('.review-selected-preview .review-targets').all())
        global_summary=summaries.filter(has_text=next(p for p in info['targets'] if p.endswith('/home/CLAUDE.md')))
        global_summary.focus();page.keyboard.press('Enter')
        global_target=global_summary.locator('..')
        global_target.get_by_text('Complete combined edit',exact=True).click()
        expect(global_target.locator('.review-card__diff')).to_be_visible()
        expect(global_target.locator('.review-card__diff')).to_contain_text('Keep the complete invented evidence visible.')
        expect(global_target.get_by_text('Direct file delivery.',exact=True)).to_be_visible()
        global_summary.focus()
        page.evaluate('async()=>{const app=await import("/app.js");app.paintReview();}')
        expect(global_summary).to_be_focused()
        assert global_target.evaluate('el=>el.open')
        expect(global_target.locator('.review-card__diff')).to_be_visible()
        combined=global_target.get_by_text('Complete combined edit',exact=True)
        combined.focus()
        page.evaluate('async()=>{const app=await import("/app.js");app.paintReview();}')
        expect(combined).to_be_focused()
        expect(global_target.locator('.review-card__diff')).to_be_visible()
        project_summary=summaries.filter(has_text='invented-project-0')
        project_summary.click()
        expect(project_summary.locator('..')).to_contain_text('Integration into your working copy is a separate step.')
        page.locator('.review-breakdown > summary').click()
        expect(page.locator('.review-proposal')).to_have_count(20)
        capture('expanded-evidence')
        page.locator('[data-review-individual]').click()
        expect(page.locator('.review-member')).to_have_count(20)
        check=page.locator('#review-include-'+info['mixed'][0]);check.focus();page.keyboard.press('Space')
        expect(approve).to_contain_text('Approve selected (19)')
        hints()
        expect(check).to_be_focused()
        page.keyboard.press('Space');expect(approve).to_contain_text('write once per file')
        select('conflict')
        expect(approve).to_be_disabled()
        expect(page.locator('.review-targets > summary')).to_contain_text('Conflict')
        capture('conflict')
        page.locator('[data-review-individual]').click()
        check=page.locator('#review-include-'+info['conflict'][1]);check.focus();page.keyboard.press('Space')
        expect(approve).to_be_enabled();expect(approve).to_contain_text('Approve selected (1)')
        expect(check).to_be_focused()
        check.check();expect(approve).to_be_disabled()
        # A failed exact preview cannot permit approval; reload recovers it.
        page.route('**/api/review-preview?*',lambda route:route.fulfill(status=503,json={'detail':'Invented preview read failure'}))
        page.reload();page.wait_for_load_state('networkidle')
        expect(approve).to_be_disabled()
        expect(page.locator('#review-body')).to_contain_text('Invented preview read failure')
        hints()
        expect(page.locator('[data-decision="reject"]')).to_be_disabled()
        capture('preview-error')
        assert page.locator('#review-body .error-state').evaluate('el=>el.scrollWidth<=el.clientWidth'), 'Preview error text is clipped'
        page.unroute('**/api/review-preview?*')
        page.get_by_role('button',name='Reload Review',exact=True).click()
        page.wait_for_load_state('networkidle')
        select('mixed');expect(approve).to_be_enabled()
        select('conflict');select('mixed')
        delayed=[]
        pattern='**/api/review-preview?*'
        page.route(pattern,lambda route:delayed.append(route))
        pending=page.locator('[data-review-individual]')
        if pending.get_attribute('aria-expanded')=='false':pending.click()
        page.locator('#review-include-'+info['mixed'][0]).uncheck()
        expect(approve).to_be_disabled()
        expect(page.locator('#review-body')).to_contain_text('Reading the selected edits and destinations')
        hints()
        expect(page.locator('[data-decision="reject"]')).to_be_disabled()
        assert len(delayed)==1
        capture('preview-loading')
        select('conflict')
        # The current conflict is already cached. Finish only the old family's request.
        delayed[0].continue_()
        page.unroute(pattern)
        page.wait_for_load_state('networkidle')
        expect(page.locator('.review-card--open')).to_have_attribute('data-learning-id',info['families']['conflict'])
        expect(approve).to_be_disabled()
        page.locator('.review-card--open').focus();page.keyboard.press('j');page.wait_for_load_state('networkidle')
        expect(page.locator('.review-card--open')).to_be_focused()
        page.keyboard.press('k');page.wait_for_load_state('networkidle')
        expect(page.locator('.review-card--open')).to_be_focused()
        with sqlite3.connect('file:'+info['db']+'?mode=ro',uri=True) as db:
            assert list(db.iterdump())==info['snapshot']
            assert db.execute('SELECT COUNT(*) FROM llm_calls').fetchone()[0]==0
        assert all(Path(p).read_text()==text for p,text in info['targets'].items())
        assert all(method=='GET' for method,url in result['requests'])
        assert not result['errors'],result['errors']
        visual.assert_clean()
        print('REVIEW_LAYOUT_OK',json.dumps({'captures':len(result['captures']),'requests':len(result['requests']),'models':0,'errors':[],'store':'unchanged','targets':'unchanged'}))
    except BaseException:
        page.screenshot(path=str(OUT/'failure.png'))
        (OUT/'failure.html').write_text(page.content())
        raise
    finally:
        browser.close();(OUT/'result.json').write_text(json.dumps(result,indent=2))
