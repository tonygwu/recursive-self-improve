"""Exercise actual plugin observation and retained Project evidence, GET-only."""
from pathlib import Path
from urllib.parse import quote
import json
import sqlite3
import sys
from playwright.sync_api import sync_playwright, expect

ROOT=Path(__file__).resolve().parents[2];out=ROOT/'reports/dashboard-parity'
inspect_only='--inspect' in sys.argv
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True)
    page=browser.new_page(viewport={'width':1440,'height':1024})
    errors,requests=[],[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    page.on('request',lambda r:requests.append((r.method,r.url)))
    try:
        for case,port in [('populated',8876),('failed',8877),('absent',8878)]:
            info=json.loads((out/f'plugins-{case}-manifest.json').read_text())
            url=f'http://127.0.0.1:{port}/#/projects/'+quote(info['project_key'],safe='')+'?tab=topology'
            page.goto(url);page.wait_for_load_state('networkidle')
            page.screenshot(path=str(out/f'plugins-{case}-before.png'))
            (out/f'plugins-{case}-before.html').write_text(page.content())
            content=page.locator('#project-detail-body')
            if inspect_only:
                print(case,content.inner_text()[-3000:]);continue
            expected={'populated':'2 installation records.','failed':'Plugin coverage is incomplete','absent':'No plugins were found in the inspected registry'}[case]
            assert expected in content.inner_text()
            assert 'Plugin files do not establish rule availability' in content.inner_text()
            assert 'UNRELATED_SETTINGS_SENTINEL' not in page.content()
            if case=='populated':
                summary=page.locator('summary').filter(has=page.get_by_text('example@fixture',exact=True)).first
                summary.focus();page.keyboard.press('Enter')
                plugin=summary.locator('..')
                assert 'Installed version: 1.0 · manifest version: 2.0' in plugin.inner_text()
                assert 'Explicit setting: disabled' in plugin.inner_text()
                summary.focus();page.keyboard.press('End')
                assert page.evaluate('document.activeElement.tagName')=='SUMMARY'
                page.wait_for_function('document.querySelector("#main").scrollTop > 0')
                for width in (1440,1280):
                    page.set_viewport_size({'width':width,'height':1024})
                    for theme in ('light','dark'):
                        page.evaluate('(theme)=>document.documentElement.dataset.theme=theme',theme)
                        plugin.scroll_into_view_if_needed()
                        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                        page.screenshot(path=str(out/f'plugins-{theme}-{width}.png'))
                name=info['payload']+'/skills/guide-17/SKILL.md'
                file=page.locator('details').filter(has=page.locator('summary').get_by_text(name,exact=True)).first
                file.locator(':scope > summary').click()
                assert '/example:guide-17' in file.inner_text()
                file.get_by_role('link',name='Browse retained plugin instruction text',exact=True).click()
                page.locator('#evidence-results a[data-evidence-key]').wait_for()
                assert page.locator('#evidence-results a[data-evidence-key]').count()==1
                page.locator('#evidence-results a[data-evidence-key]').click()
                page.get_by_role('link',name='Source',exact=True).click()
                page.get_by_text('Complete retained redacted text',exact=True).wait_for()
                assert 'Complete retained plugin sentinel 17.' in page.locator('#inspector-body').inner_text()
                page.screenshot(path=str(out/'plugins-retained-text.png'))
                page.goto(url);page.wait_for_load_state('networkidle')
            page.reload();page.wait_for_load_state('networkidle')
            assert expected in content.inner_text()
            page.get_by_role('link',name='Instruction context',exact=True).click()
            expect(page.locator("#project-view-context")).to_have_attribute("aria-current", "page")
            assert expected in content.inner_text()
            page.screenshot(path=str(out/f'plugins-{case}-context.png'))
            assert all(Path(path).read_text()==text for path,text in info['targets'].items())
            with sqlite3.connect(info['db']) as db:
                assert list(db.iterdump())==info['snapshot']
                assert db.execute('SELECT COUNT(*) FROM llm_calls').fetchone()[0]==0
        if not inspect_only:
            assert not errors,errors
            assert all(m=='GET' for m,u in requests if u.startswith('http://127.0.0.1:'))
            print(f'PLUGIN_INSTRUCTIONS_BROWSER_OK: {len(requests)} GET-only requests; versions, scopes, configuration, namespaced bodies, populated/failed/absent, both themes/widths, focus/scroll/reload, unchanged Stores/targets, zero models/errors.')
    except Exception:
        page.screenshot(path=str(out/'plugins-failure.png'))
        (out/'plugins-failure.html').write_text(page.content());raise
    finally:browser.close()
