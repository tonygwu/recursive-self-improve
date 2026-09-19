"""Temporary real-reader trends: complete dates, separate versions, native focus."""
from pathlib import Path
from urllib.parse import urlencode
import json
import sqlite3
from playwright.sync_api import sync_playwright, expect
from keyboard_actions import KeyboardActions
from visual_checks import VisualChecks

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'reports/dashboard-parity/trend-markers'
info = json.loads((OUT / 'manifest.json').read_text())
assert 'si-trend-markers-' in info['db']
BASE = 'http://127.0.0.1:8876/'
result = {'requests':[], 'errors':[], 'captures':[]}
with sync_playwright() as runtime:
    browser = runtime.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width':1280,'height':1024})
    keys = KeyboardActions(page, OUT/'keyboard.json')
    visual = VisualChecks(page, OUT/'contrast.json')
    page.on('pageerror',lambda e:result['errors'].append(str(e)))
    page.on('request',lambda r:result['requests'].append([r.method,r.url]))

    def capture(name):
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        # Text and controls must fit their actual cell, not just the document.
        geometry = page.locator('.trend-matrix td, .trend-matrix th').evaluate_all('''cells=>cells.flatMap(el=>{
            const r=el.getBoundingClientRect(), range=document.createRange();range.selectNodeContents(el);
            const over=[...range.getClientRects()].some(t=>t.left<r.left-1||t.right>r.right+1);
            return over?[{text:el.textContent,width:r.width,scroll:el.scrollWidth}]:[];
        })''')
        assert not geometry, geometry
        visual.check(name)
        page.screenshot(path=str(OUT/(name+'.png')))
        result['captures'].append(name)

    def visible_focus(target):
        keys.focused(target)
        assert target.evaluate('''el=>{const r=el.getBoundingClientRect();
            const hit=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);
            return hit===el||el.contains(hit);}'''), 'Focus target is occluded'

    try:
        page.goto(BASE+'#/evals');page.wait_for_load_state('networkidle')
        page.screenshot(path=str(OUT/'initial.png'))
        (OUT/'initial.html').write_text(page.content())
        result['initial_buttons']=page.locator('#view-evals button').all_text_contents()
        expect(page.locator('.trend-matrix')).to_have_count(0)
        # Daily context is usable even while incompatible scan versions hide rates.
        expect(page.locator('#trend-context-2030-09')).to_have_count(1)
        keys.activate(page.locator('#trend-context-toggle-2030-09'))
        expect(page.locator('#trend-context-2030-09')).to_contain_text('2030-09-02 UTC · 12 retained revisions')
        capture('unselected-context-light-1280')
        keys.choose(page.locator('#trend-compatibility_key'),info['version'])
        keys.activate(page.locator('#trend-submit'))
        page.wait_for_url('**/*compatibility_key=*');page.wait_for_load_state('networkidle')
        expect(page.locator('#trend-submit')).to_be_focused()
        keys.activate(page.locator('#trend-options-toggle'))
        matrix=page.locator('.trend-matrix')
        aggregate=matrix.locator('tr').filter(has=page.get_by_role('rowheader',name='all kinds',exact=True))
        correction=matrix.locator('tr').filter(has=page.get_by_role('rowheader',name='correction',exact=True))
        expect(aggregate.locator('td[data-trend-month]')).to_have_count(7)
        expect(aggregate.locator('.trend-peak')).to_have_text('25,000')
        expect(correction.locator('.trend-peak')).to_have_text('25,000')
        expect(page.locator('#trend-band-2030-09')).to_have_text('chosen +2')
        expect(page.locator('#trend-marker-2030-08')).to_have_text('▲ 08-31')
        expect(page.locator('#trend-marker-2030-09')).to_have_text('▲ 2 dates')
        for control in page.locator('[data-trend-context]').all():
            assert control.inner_text() in control.get_attribute('aria-label')
        expect(page.locator('#trend-delivery-status')).to_have_text('20 shown · 23 retained revisions in this interval')
        # A late read can change the whole body while a numeric cell is focused.
        for cell in (correction.locator('.trend-peak'), correction.locator('[data-trend-month="2030-09"] button')):
            keys.reach(cell)
            page.evaluate('''async()=>{const m=await import('/app.js');
                globalThis.trendTestHealth=m.state.evaluationHealth;
                m.state.evaluationHealth={error:'Invented delayed read failure'};m.paintTrends();}''')
            visible_focus(cell)
            page.evaluate('''async()=>{const m=await import('/app.js');
                m.state.evaluationHealth=globalThis.trendTestHealth;delete globalThis.trendTestHealth;m.paintTrends();}''')
            visible_focus(cell)
        for width in (1280,1440):
            page.set_viewport_size({'width':width,'height':1024})
            for theme in ('light','dark'):
                keys.theme(theme)
                page.locator('#main').evaluate('el=>el.scrollTop=0')
                last = matrix.locator('tbody tr:not(.trend-workload)').last.bounding_box()
                assert last['y']+last['height'] <= 1000, last
                assert page.locator('#trend-band-2030-09').bounding_box()['height'] == 24
                matrix.scroll_into_view_if_needed()
                capture(f'markers-{theme}-{width}')
                keys.activate(page.locator('#trend-marker-2030-09'),key='Space')
                summary=page.locator('#trend-context-toggle-2030-09')
                visible_focus(summary)
                detail=page.locator('#trend-context-2030-09')
                expect(detail).to_have_attribute('open','')
                expect(detail).to_contain_text('2030-09-03 UTC · 8 retained revisions')
                expect(detail).to_contain_text(info['version'])
                expect(detail).to_contain_text(info['unknown_version'])
                expect(detail).to_contain_text('unknown configuration; no rate')
                capture(f'context-{theme}-{width}')
                page.evaluate('async()=>(await import("/app.js")).paintTrends()')
                visible_focus(summary);expect(detail).to_have_attribute('open','')
                keys.activate(summary)
        # Enter on a band opens the same full context and preserves selection.
        selected_url=page.url
        keys.activate(page.locator('#trend-band-2030-08'))
        visible_focus(page.locator('#trend-context-toggle-2030-08'))
        expect(page.locator('#trend-context-2030-08')).to_contain_text('2030-08-31 UTC · 3 retained revisions')
        assert page.url == selected_url
        keys.activate(page.locator('#trend-deliveries-next'))
        page.wait_for_url('**/*delivery_cursor=*');page.wait_for_load_state('networkidle')
        expect(page.locator('#trend-delivery-status')).to_have_text('3 shown · 23 retained revisions in this interval')
        expect(page.locator('#trend-marker-2030-09')).to_have_text('▲ 2 dates')
        page.reload();page.wait_for_load_state('networkidle')
        expect(page.locator('#trend-delivery-status')).to_have_text('3 shown · 23 retained revisions in this interval')
        keys.activate(page.locator('#trend-marker-2030-09'))
        expect(page.locator('#trend-context-2030-09')).to_contain_text('2030-09-02 UTC · 12 retained revisions')
        capture('older-page-complete-context-dark-1440')
        # Unidentifiable configuration retains the observed band but has no peak.
        page.goto(BASE+'#/evals?'+urlencode({'compatibility_key':info['unknown_version']}))
        page.wait_for_load_state('networkidle')
        expect(page.locator('.trend-peak')).to_have_text(['—']*8)
        expect(page.locator('#trend-band-2030-09')).to_have_text('chosen +2')
        capture('unknown-version-dark-1440')
        # Uncovered selection still exposes dated revision context.
        page.goto(BASE+'#/evals?compatibility_key=uncovered');page.wait_for_load_state('networkidle')
        expect(page.locator('.trend-matrix')).to_have_count(0)
        keys.activate(page.locator('#trend-context-toggle-2030-09'))
        expect(page.locator('#trend-context-2030-09')).to_contain_text('2030-09-03 UTC · 8 retained revisions')
        capture('uncovered-context-dark-1440')
        page.goto(selected_url);page.wait_for_load_state('networkidle')
        page.emulate_media(forced_colors='active')
        keys.reach(page.locator('#trend-marker-2030-09'))
        visible_focus(page.locator('#trend-marker-2030-09'))
        capture('forced-colors-chart')
        keys.activate(page.locator('#trend-marker-2030-09'))
        visible_focus(page.locator('#trend-context-toggle-2030-09'))
        capture('forced-colors-context')
        page.emulate_media(forced_colors='none')
        assert not result['errors'],result['errors']
        assert all(method=='GET' for method,url in result['requests'])
        with sqlite3.connect('file:'+info['db']+'?mode=ro',uri=True) as db:
            assert list(db.iterdump()) == info['sql']
            assert db.execute('SELECT COUNT(*) FROM llm_calls').fetchone()[0] == 0
        assert all(Path(path).read_text()==text for path,text in info['targets'].items())
        visual.assert_clean()
        result.update(store='unchanged',targets='unchanged',models=0)
        print('TREND_MARKERS_OK '+json.dumps(result['captures']))
    except BaseException:
        page.screenshot(path=str(OUT/'failure.png'))
        (OUT/'failure.html').write_text(page.content())
        raise
    finally:
        keys.save();browser.close()
        (OUT/'result.json').write_text(json.dumps(result,indent=2))
