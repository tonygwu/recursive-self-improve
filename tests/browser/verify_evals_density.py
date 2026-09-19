"""Read-only Evals layout and complete evidence through the real temporary API."""
from pathlib import Path
import json
import sqlite3
from playwright.sync_api import sync_playwright, expect
from keyboard_actions import KeyboardActions
from visual_checks import VisualChecks

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'reports/dashboard-parity/evals-density'
manifest = json.loads((OUT / 'manifest.json').read_text())
assert 'si-evals-density-' in manifest['db']
BASE = 'http://127.0.0.1:8876/'
result = {'captures':[], 'requests':[], 'errors':[]}
with sync_playwright() as runtime:
    browser = runtime.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width':1280, 'height':1024})
    keys = KeyboardActions(page, OUT / 'keyboard.json')
    visual = VisualChecks(page, OUT / 'contrast.json')
    page.on('pageerror', lambda error:result['errors'].append(str(error)))
    page.on('request', lambda request:result['requests'].append([request.method, request.url]))
    def capture(name, compact=False):
        page.screenshot(path=str(OUT / (name+'.png')))
        visual.check(name)
        dimensions = page.locator('.policy-table-scroll').evaluate('el=>({width:el.clientWidth,scroll:el.scrollWidth})') if page.locator('.policy-table-scroll').count() else {}
        assert page.evaluate('document.documentElement.scrollWidth===innerWidth')
        if dimensions:
            assert dimensions['width'] == dimensions['scroll'], dimensions
        if compact:
            dimensions['policy_height'] = page.locator('.policy-panel').bounding_box()['height']
            dimensions['first_signal_y'] = page.locator('.trend-matrix tbody td').first.bounding_box()['y']
            dimensions['gate_summary_y'] = page.locator('#trend-gate p').first.bounding_box()['y']
            signals = page.locator('.trend-matrix tbody tr:not(.trend-workload)')
            assert signals.count() == 8
            clipped = signals.locator('.trend-peak').evaluate_all('''cells=>cells.filter(el=>{
                const r=el.getBoundingClientRect(),text=document.createRange();text.selectNodeContents(el);
                return [...text.getClientRects()].some(t=>t.left<r.left||t.right>r.right);
            }).map(el=>el.textContent)''')
            assert not clipped, clipped
            last = signals.last.bounding_box()
            dimensions['last_signal_bottom'] = last['y'] + last['height']
            print('EVALS_COMPOSITION', json.dumps(dimensions), flush=True)
            assert dimensions['policy_height'] <= 400, dimensions
            assert dimensions['last_signal_bottom'] <= 1000, dimensions
            assert dimensions['gate_summary_y'] < 1024, dimensions
        result['captures'].append({'name':name, **dimensions})
    try:
        page.goto(BASE+'#/evals')
        page.wait_for_load_state('networkidle')
        expect(page.locator('#policy-row-global')).to_be_visible()
        (OUT / 'initial.html').write_text(page.content())
        page.screenshot(path=str(OUT / 'initial.png'))
        result['initial_buttons'] = page.locator('#view-evals button').all_text_contents()
        dimensions = page.locator('#class-evidence .panel').evaluate('el=>({height:el.getBoundingClientRect().height})')
        result['baseline'] = dimensions
        assert dimensions['height'] <= 400, dimensions
        for width in (1280, 1440):
            page.set_viewport_size({'width':width, 'height':1024})
            for theme in ('light', 'dark'):
                current = page.evaluate('async()=>(await import("/app.js")).effectiveTheme()')
                if current != theme:
                    page.get_by_role('button', name=theme.title()+' theme', exact=True).click()
                page.locator('#main').evaluate('el=>el.scrollTop=0')
                capture(f'evals-{theme}-{width}', compact=True)
        expect(page.get_by_role('switch')).to_have_count(4)
        expect(page.get_by_role('switch', name='Auto-apply hooks', exact=True)).to_be_disabled()
        expect(page.locator('#policy-row-project')).to_contain_text('0 / 0 useful')
        assert '100%' not in page.locator('.policy-table').inner_text()
        expect(page.locator('#trend-gate')).to_contain_text('3 recorded evaluation attempts')
        assert page.locator('.trend-matrix tbody td').first.get_attribute('aria-label').find('eligible physical lines') >= 0
        # The compact selection stays named and all controls remain native-keyboard reachable.
        expect(page.locator('#trend-controls details')).not_to_have_attribute('open','')
        keys.activate(page.locator('#trend-options-toggle'))
        expect(page.locator('#trend-project_key')).to_be_visible()
        project = page.locator('#trend-project_key option').nth(1).get_attribute('value')
        version = page.locator('#trend-compatibility_key option').nth(1).get_attribute('value')
        keys.choose(page.locator('#trend-project_key'), project)
        keys.choose(page.locator('#trend-compatibility_key'), version)
        keys.activate(page.locator('#trend-submit'))
        page.wait_for_url('**/*compatibility_key=*');page.wait_for_load_state('networkidle')
        expect(page.locator('#trend-submit')).to_be_focused()
        expect(page.locator('#trend-options-toggle')).to_contain_text(project)
        page.reload();page.wait_for_load_state('networkidle')
        expect(page.locator('#trend-options-toggle')).to_contain_text(project)
        keys.activate(page.locator('#trend-options-toggle'))
        expect(page.locator('#trend-project_key')).to_have_value(project)
        expect(page.locator('#trend-compatibility_key')).to_have_value(version)
        page.evaluate('async()=>(await import("/app.js")).paintTrends()')
        expect(page.locator('#trend-options-toggle')).to_be_focused()
        expect(page.locator('#trend-controls details')).to_have_attribute('open','')
        capture('evals-selection-keyboard-dark-1440')
        keys.activate(page.locator('#trend-options-toggle'))
        # Each native button reveals its own complete evidence, then restores focus.
        for cls, label in [('global','global rules'), ('project','project rules'), ('skill','skills'), ('hook','hooks')]:
            button = page.get_by_role('button', name=f'Expand {label} evidence', exact=True)
            button.focus();page.keyboard.press('Enter')
            expect(page.get_by_role('button', name=f'Collapse {label} evidence', exact=True)).to_be_focused()
            detail = page.locator('#policy-details-'+cls)
            expect(detail).to_contain_text('observed available')
            expect(detail).to_contain_text('No check time recorded')
            if cls == 'global':
                expect(detail).to_contain_text('1 uncertain · 1 unreviewed')
                expect(detail).to_contain_text('3 total attempts')
            if cls == 'hook':expect(detail).to_contain_text('Hooks never auto-apply')
            else:expect(detail).to_contain_text('These thresholds are advisory')
            page.keyboard.press('Space')
            expect(button).to_be_focused();expect(detail).to_have_count(0)
        page.locator('#policy-evidence-global').click()
        page.locator('#policy-details-global summary').click()
        capture('evals-expanded-dark-1440')
        # Read transport failure keeps current evidence and the inspected disclosure.
        page.route('**/api/class-evidence', lambda route:route.fulfill(status=503, json={'detail':'Invented read failure'}))
        page.locator('#policy-refresh').click()
        expect(page.locator('.policy-feedback')).to_contain_text('showing the previous class evidence')
        expect(page.locator('#policy-details-global details')).to_have_attribute('open','')
        expect(page.locator('#policy-refresh')).to_be_focused()
        capture('evals-refresh-error-dark-1440')
        page.unroute('**/api/class-evidence')
        # A delayed real response disables duplicate actions without removing counts.
        pending = []
        page.route('**/api/class-evidence', lambda route:pending.append(route))
        page.locator('#policy-refresh').click()
        expect(page.locator('.policy-feedback')).to_contain_text('Reading class evidence')
        expect(page.locator('#policy-refresh')).to_be_disabled()
        expect(page.get_by_role('switch', name='Auto-apply global rules', exact=True)).to_be_disabled()
        expect(page.locator('#policy-details-global')).to_contain_text('1 uncertain · 1 unreviewed')
        assert len(pending) == 1
        capture('evals-refresh-pending-dark-1440')
        pending.pop().continue_()
        expect(page.locator('#policy-refresh')).to_be_enabled()
        expect(page.locator('#policy-refresh')).to_be_focused()
        expect(page.locator('#policy-details-global details')).to_have_attribute('open','')
        page.unroute('**/api/class-evidence')
        page.locator('#policy-evidence-global').click()
        # History is collapsed initially, paginates all 21 samples, and a cursor reload opens it.
        expect(page.locator('#quality-samples > details')).not_to_have_attribute('open','')
        page.locator('#quality-history-toggle').focus();page.keyboard.press('Enter')
        expect(page.locator('#quality-history-toggle')).to_be_focused()
        expect(page.locator('#quality-history-status')).to_contain_text('20 samples shown · 21 retained')
        page.get_by_role('button', name='Older samples', exact=True).click()
        expect(page.locator('#quality-history-status')).to_contain_text('1 samples shown · 21 retained')
        assert 'quality_cursor=' in page.url
        page.reload();page.wait_for_load_state('networkidle')
        expect(page.locator('#quality-samples > details')).to_have_attribute('open','')
        expect(page.locator('#quality-history-status')).to_be_visible()
        capture('evals-history-page-dark-1440')
        page.get_by_role('button', name='Newest samples', exact=True).click()
        expect(page.locator('#quality-history-status')).to_contain_text('20 samples shown · 21 retained')
        page.get_by_role('link', name='Review project rules sample →', exact=True).click()
        expect(page.locator('#quality-detail')).to_contain_text('No verified applied revisions are eligible for this class')
        expect(page.get_by_role('button', name='Save sample for assessment', exact=True)).to_be_disabled()
        # Initial failed reads remain actionable without inventing zeros or hiding trends.
        page.route('**/api/class-evidence', lambda route:route.fulfill(status=503,json={'detail':'Invented initial failure'}))
        page.goto(BASE+'#/evals');page.reload();page.wait_for_load_state('networkidle')
        expect(page.locator('#class-evidence')).to_contain_text('Invented initial failure')
        expect(page.locator('.policy-table')).to_have_count(0)
        expect(page.locator('#trend-gate')).to_contain_text('3 recorded evaluation attempts')
        page.set_viewport_size({'width':1280,'height':1024})
        capture('evals-initial-read-error-dark-1280')
        page.unroute('**/api/class-evidence')
        page.locator('#policy-refresh').click()
        expect(page.locator('#policy-row-global')).to_be_visible()
        assert all(method == 'GET' for method, url in result['requests']), result['requests']
        assert all('quality_cursor=' not in url for method,url in result['requests'] if '/api/monthly' in url)
        assert all(Path(path).read_text() == text for path,text in manifest['targets'].items())
        with sqlite3.connect('file:'+manifest['db']+'?mode=ro', uri=True) as db:
            assert list(db.iterdump()) == manifest['sql']
            assert db.execute('SELECT COUNT(*) FROM llm_calls').fetchone()[0] == 0
        assert not result['errors'], result['errors']
        visual.assert_clean()
        result.update({'store':'unchanged','targets':'unchanged','models':0,'samples':21})
        print('EVALS_DENSITY_OK: '+json.dumps({key:result[key] for key in ('captures','store','targets','models','samples')}))
    except BaseException:
        page.screenshot(path=str(OUT / 'failure.png'))
        (OUT / 'failure.html').write_text(page.content())
        raise
    finally:
        keys.save()
        browser.close()
        (OUT / 'result.json').write_text(json.dumps(result, indent=2))
