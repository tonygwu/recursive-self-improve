"""Actual identity labels, canonical links and complete native session navigation."""
from pathlib import Path
from urllib.parse import quote
import argparse,json,sqlite3
from playwright.sync_api import sync_playwright,expect
from keyboard_actions import KeyboardActions
from visual_checks import VisualChecks
parser=argparse.ArgumentParser();parser.add_argument('--inspect-only',action='store_true');args=parser.parse_args()
ROOT=Path(__file__).resolve().parents[2];OUT=ROOT/'reports/dashboard-parity/source-attribution'
info=json.loads((OUT/'manifest.json').read_text());assert 'si-source-attribution-' in info['db']
result={'requests':[],'errors':[],'captures':[]}
with sync_playwright() as runtime:
    browser=runtime.chromium.launch(headless=True);page=browser.new_page(viewport={'width':1280,'height':1024})
    keys=KeyboardActions(page,OUT/'keyboard.json');visual=VisualChecks(page,OUT/'contrast.json')
    page.on('pageerror',lambda e:result['errors'].append(str(e)))
    page.on('request',lambda r:result['requests'].append([r.method,r.url]))
    def go(route):
        page.goto('http://127.0.0.1:8876/'+route);page.wait_for_load_state('networkidle')
    def capture(name,target):
        for width in (1280,1440):
            page.set_viewport_size({'width':width,'height':1024})
            for theme in ('light','dark'):
                keys.theme(theme);target.scroll_into_view_if_needed()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                label=f'{name}-{theme}-{width}';visual.check(label)
                page.screenshot(path=str(OUT/(label+'.png')));result['captures'].append(label)
    try:
        go('#/rules/rule-0?tab=provenance')
        panel=page.locator('#inspector-body')
        page.screenshot(path=str(OUT/'initial.png'));(OUT/'initial.html').write_text(page.content())
        result['initial_buttons']=page.locator('button:visible').all_text_contents()
        result['initial_links']=panel.locator('a').evaluate_all('els=>els.map(e=>({text:e.textContent,href:e.getAttribute("href")}))')
        if args.inspect_only:print(json.dumps({'buttons':result['initial_buttons'],'links':result['initial_links'],'panel':panel.inner_text()}))
        else:
            expect(panel).to_contain_text('2 known native sessions')
            expect(panel).not_to_contain_text('team/changed')
            expect(panel).to_contain_text('does not measure relative agent quality')
            aliases=panel.get_by_text('2 retained names for this repository',exact=True)
            keys.activate(aliases);expect(panel).to_contain_text('team/original-alias')
            capture('rule-provenance',panel)
            links=panel.locator('a[href*="/evidence/session/"]')
            expect(links).to_have_count(2)
            destinations=[]
            for label in ('Claude Code · shared-native','Codex · shared-native'):
                link=panel.get_by_role('link',name=label,exact=True)
                href=link.get_attribute('href');destinations.append(href)
                keys.activate(link);page.wait_for_load_state('networkidle')
                expect(panel).to_contain_text(label.split(' · ')[0])
                expect(panel.locator('[data-source-identity]').first).to_contain_text('shared-native')
                page.reload();page.wait_for_load_state('networkidle');assert page.url.endswith(href)
                capture('session-'+label.split(' · ')[0].split()[0].lower(),panel)
                page.go_back();page.wait_for_load_state('networkidle')
            assert len(set(destinations))==2
            keys.activate(panel.get_by_role('link',name='team/original',exact=True))
            page.wait_for_load_state('networkidle')
            assert page.url.endswith('#/projects/'+quote(info['project'],safe=''))
            expect(page.locator('#main')).to_contain_text('team/original')
            go('#/rules/evidence/learning/rule-0?mode=evidence&tab=linked')
            expect(panel).to_contain_text('1–20 of 25 linked incidents')
            expect(panel.locator('[data-source-identity]')).to_have_count(20)
            keys.activate(panel.get_by_role('button',name='Next linked',exact=True))
            expect(panel).to_contain_text('21–25 of 25 linked incidents')
            expect(panel.locator('[data-source-identity]')).to_have_count(5)
            expect(panel).to_contain_text('team/original');expect(panel).to_contain_text('Codex')
            capture('linked-evidence',panel)
            go('#/rules/evidence/incident/claude?mode=evidence&tab=source')
            expect(panel).to_contain_text('Claude Code · shared-native')
            expect(panel.locator('[data-source-identity]')).not_to_contain_text('team/changed')
            capture('incident-source',panel)
            go('#/rules/rule-1?tab=provenance')
            expect(panel).to_contain_text('1 known native sessions · 4 transcript references with incomplete identity')
            expect(panel).to_contain_text('Unknown source')
            unknown=panel.get_by_role('link',name='Agent unknown · shared-native',exact=True)
            expect(unknown).to_have_count(2)
            assert len(set(unknown.evaluate_all('els=>els.map(e=>e.getAttribute("href"))')))==2
            capture('unknown-identity',panel)
            keys.activate(unknown.first);page.wait_for_load_state('networkidle')
            expect(panel).to_contain_text('Agent unknown')
            expect(panel).to_contain_text('native identity incomplete')
            go('#/rules/rule-2?tab=provenance')
            expect(panel).to_contain_text('Agent unknown');expect(panel).not_to_contain_text('Agent product')
            assert not result['errors'],result['errors']
            assert all(method=='GET' for method,url in result['requests'])
            with sqlite3.connect('file:'+info['db']+'?mode=ro',uri=True) as db:
                assert list(db.iterdump())==info['sql']
                assert db.execute('SELECT COUNT(*) FROM llm_calls').fetchone()[0]==0
            assert all(Path(p).read_text()==value for p,value in info['targets'].items())
            visual.assert_clean();result.update(sql='unchanged',targets='unchanged',paid_calls=0,native_session_links=destinations)
            print('SOURCE_ATTRIBUTION_BROWSER_OK: canonical labels, aliases, two provider-qualified sessions, unknown references, 20+5 incident pages, exact links/reload, both themes/widths; unchanged SQL/targets; zero model calls')
    except BaseException:
        page.screenshot(path=str(OUT/'failure.png'));(OUT/'failure.html').write_text(page.content());raise
    finally:
        keys.save();browser.close();(OUT/'result.json').write_text(json.dumps(result,indent=2))
