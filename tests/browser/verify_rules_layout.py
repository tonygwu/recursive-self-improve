"""Rules columns fit beside the inspector; narrow views retain keyboard scrolling."""
from pathlib import Path
import json
import sqlite3

from playwright.sync_api import sync_playwright, expect
from self_improve.store import PROPOSAL_STATUSES
from keyboard_actions import KeyboardActions

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'reports/dashboard-parity/rules-layout'
OUT.mkdir(parents=True, exist_ok=True)
manifest = json.loads((ROOT / 'reports/dashboard-parity/evidence-browser-manifest.json').read_text())
assert 'si-evidence-ui-' in manifest['db']
result = {'captures': [], 'requests': [], 'errors': []}
with sync_playwright() as runtime:
    browser = runtime.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width': 1280, 'height': 1024})
    keys=KeyboardActions(page,OUT/'keyboard.json')
    page.on('pageerror', lambda error: result['errors'].append(str(error)))
    page.on('request', lambda request: result['requests'].append([request.method, request.url]))

    def capture(name):
        page.locator('#main').evaluate('el => el.scrollTop = 0')
        page.screenshot(path=str(OUT / (name + '.png')))
        result['captures'].append(name)
        assert page.evaluate('document.documentElement.scrollWidth === innerWidth')

    def fits():
        wrap = page.locator('#rules-table-wrap')
        # A completed read may replace rows between separate geometry calls.
        # Measure one current DOM snapshot, including every cell's visibility.
        geometry = wrap.evaluate('''el => ({
            dimensions: {available: el.clientWidth, required: el.scrollWidth},
            bounds: el.getBoundingClientRect().toJSON(),
            cells: [...document.querySelectorAll('.rules-table th, .rules-family-label')]
                .map(cell => cell.getBoundingClientRect().toJSON())
        })''')
        dimensions = geometry['dimensions']
        assert dimensions['required'] <= dimensions['available'], dimensions
        bounds = geometry['bounds']
        assert bounds['width'] > 0 and bounds['height'] > 0, bounds
        assert geometry['cells']
        for box in geometry['cells']:
            assert box['width'] > 0 and box['height'] > 0, box
            assert box['x'] >= bounds['x'] and box['x'] + box['width'] <= bounds['x'] + bounds['width'] + 1
        assert page.locator('.rules-family button').evaluate_all('els => els.every(el => el.scrollWidth <= el.clientWidth)')
        badges = page.locator('.rules-table .badge').evaluate_all(r'''els => els.map(el => {
          const box = el.getBoundingClientRect(), cell = el.closest('td').getBoundingClientRect();
          const range = document.createRange(); range.selectNodeContents(el);
          const parts = Array.from(range.getClientRects());
          const words = Array.from(el.textContent.matchAll(/[^\s·]+/g));
          const wholeWords = words.every(word => {
            range.setStart(el.firstChild, word.index); range.setEnd(el.firstChild, word.index + word[0].length);
            return range.getClientRects().length === 1;
          });
          return {text: el.textContent, available: el.clientWidth, required: el.scrollWidth,
            fits: parts.every(r => r.left >= box.left && r.right <= box.right + 1) && box.right <= cell.right,
            wholeWords, unknown: el.dataset.state === "unknown"};
        })''')
        assert badges
        assert all(b['required'] <= b['available'] and b['fits'] and (b['wholeWords'] or b['unknown']) for b in badges), badges
        expect(page.locator('#rules-inspector-host > #inspector')).to_be_visible()

    try:
        page.goto('http://127.0.0.1:8876/#/rules/l00')
        page.wait_for_load_state('networkidle')
        (OUT / 'initial.html').write_text(page.content())
        capture('initial')
        assert page.locator('.rules-member[role], .rules-member[tabindex]').count()==0
        assert page.locator('.rules-member').first.get_by_role('cell').count()==4
        assert page.locator('.rules-member').first.get_by_role('link').count()==1
        # A delayed detail response repaints the table while its target is open.
        held_detail=[];page.route('**/api/rules/l00',lambda route:held_detail.append(route))
        keys.activate(page.locator('#rules-refresh'))
        expect(page.get_by_text('Reading rule details…',exact=True)).to_be_visible()
        expect(page.locator('#rules-notice [role="status"]')).to_have_count(0)
        path=page.locator('[data-rule-row="l00"] .rules-target').first
        keys.activate(path.locator('summary'));expect(path).to_have_attribute('open','')
        assert len(held_detail)==1;held_detail.pop().continue_()
        expect(page.get_by_text('Reading rule details…',exact=True)).to_have_count(0)
        expect(path.locator('summary')).to_be_focused();expect(path).to_have_attribute('open','')
        capture('target-during-detail-read')
        keys.activate(path.locator('summary'))
        keys.activate(page.locator('#rules-refresh'))
        expect(page.get_by_text('Reading rule details…',exact=True)).to_be_visible()
        expect(page.locator('#rules-notice [role="status"]')).to_have_count(0)
        keys.activate(path.locator('summary'));expect(path).to_have_attribute('open','')
        search=page.get_by_role('searchbox',name='Search rules and linked evidence')
        keys.reach(search);assert len(held_detail)==1;held_detail.pop().continue_()
        expect(page.get_by_text('Reading rule details…',exact=True)).to_have_count(0)
        expect(search).to_be_focused();expect(path).to_have_attribute('open','')
        capture('target-read-focus-left')
        keys.activate(path.locator('summary'));page.unroute('**/api/rules/l00')
        sections=page.get_by_role('navigation',name='Inspector sections')
        expect(sections.get_by_role('link',name='Why',exact=True)).to_have_attribute('aria-current','page')
        keys.activate(sections.get_by_role('link',name='Provenance',exact=True))
        expect(page.get_by_text('Where it came from',exact=True)).to_be_visible()
        expect(sections.get_by_role('link',name='Provenance',exact=True)).to_be_focused()
        expect(sections.get_by_role('link',name='Provenance',exact=True)).to_have_attribute('aria-current','page')
        capture('provenance-keyboard')
        (OUT/'native-table-and-navigation.txt').write_text(page.locator('#view-rules').aria_snapshot())
        keys.activate(sections.get_by_role('link',name='Why',exact=True))
        keys.activate(page.get_by_role('button',name='Inspect full provenance →',exact=True))
        expect(sections.get_by_role('link',name='Provenance',exact=True)).to_be_focused()
        keys.activate(sections.get_by_role('link',name='Why',exact=True))
        fits()  # Baselines fail on table overflow, then on clipped status text.
        for width in (1280, 1440):
            page.set_viewport_size({'width': width, 'height': 1024})
            for theme in ('light', 'dark'):
                current = page.evaluate('async () => (await import("/app.js")).effectiveTheme()')
                if current != theme:
                    page.get_by_role('button', name=theme.title() + ' theme', exact=True).click()
                fits()
                capture(f'columns-{theme}-{width}')
        page.set_viewport_size({'width': 1280, 'height': 1024})
        family = page.locator('[data-rule-family]').first
        family_id = family.get_attribute('id')
        family.focus(); page.keyboard.press('Enter')
        expect(page.locator('.rules-member[data-child="true"]').first).to_be_visible()
        expect(page.locator('#' + family_id)).to_be_focused()
        fits(); capture('expanded')
        member = page.locator('.rules-member[data-child="true"] [data-rule-id]').first
        member_id = member.get_attribute('data-rule-id')
        member.focus(); page.keyboard.press('Enter')
        expect(page.locator(f'[data-rule-id="{member_id}"]')).to_be_focused()
        expect(page.locator(f'[data-rule-id="{member_id}"]')).to_have_attribute('aria-current', 'true')
        family = page.locator('#' + family_id)
        family.focus(); page.keyboard.press('Enter')
        expect(family).to_have_attribute('aria-expanded', 'false')
        expect(family).to_be_focused()
        # A long, invented API title tests natural wrapping, without changing stored data.
        title = 'Inspect the complete evidence before changing any instruction. ' * 6
        def long_title(route):
            response = route.fetch(); data = response.json()
            for row in data['rows']:
                if row['kind'] != 'rule':
                    row['representative']['title'] = title
            singles=[row['rule'] for row in data['rows'] if row['kind']=='rule']
            for row, project in zip(singles, ('alpha','beta','gamma')):
                row['targets']=[{'kind':'project_agents_md','path':'/invented/shared/prefix/'+project+'/same/AGENTS.md'}]
            route.fulfill(response=response, json=data)
        page.route('**/api/rules/browse*', long_title)
        page.locator('#rules-refresh').click()
        expect(page.locator('.rules-family strong').first).to_have_text(title.strip())
        fits(); capture('long-family')
        for project in ('alpha','beta','gamma'):
            target=page.locator('.rules-target').filter(has_text='…/'+project+'/same/AGENTS.md')
            expect(target).to_have_count(1)
            keys.activate(target.locator('summary'))
            expect(target.locator('code')).to_have_text('/invented/shared/prefix/'+project+'/same/AGENTS.md')
            assert target.locator('code').evaluate('el=>el.scrollWidth<=el.clientWidth')
            keys.activate(target.locator('summary'))
        for width in (1280,1440):
            page.set_viewport_size({'width':width,'height':1024})
            for theme in ('light','dark'):
                keys.theme(theme);fits();capture(f'distinct-paths-{theme}-{width}')
                assert page.locator('.rules-target small span').filter(has_text='AGENTS.md').evaluate_all('''els=>els.every(el=>{const r=document.createRange();r.selectNodeContents(el);return r.getClientRects().length===1;})''')
        page.set_viewport_size({'width':1280,'height':1024})
        page.unroute('**/api/rules/browse*')
        page.locator('#rules-refresh').click()
        expect(page.locator('.rules-family strong').first).not_to_have_text(title.strip())
        # Exercise every declared proposal state together and each learning state.
        learning_states = ['candidate', 'proposed', 'applied', 'rejected', 'pruned', 'superseded']
        def all_states(route):
            response = route.fetch(); data = response.json()
            rules = [row['rule'] for row in data['rows'] if row['kind'] == 'rule']
            assert len(rules) > len(learning_states)
            rules[0]['proposal_statuses'] = {status: 1 for status in sorted(PROPOSAL_STATUSES)}
            for row, status in zip(rules[1:], learning_states):
                row['proposal_statuses'] = {}; row['status'] = status
            route.fulfill(response=response, json=data)
        page.route('**/api/rules/browse*', all_states)
        page.locator('#rules-refresh').click()
        mixed = page.locator('.rules-member').first.locator('.badge')
        expect(mixed).to_have_count(len(PROPOSAL_STATUSES))
        expected = {
            'pending':('Pending','state','info'), 'gated_pass':('Gate passed','verdict','gated_pass'),
            'gated_fail':('Gate failed','verdict','gated_fail'), 'ungated':('Ungated','verdict','ungated'),
            'inconclusive':('Inconclusive','verdict','inconclusive'), 'held':('Held','state','partial'),
            'approved_user':('Approved','state','info'), 'rejected_user':('Rejected','state','abandoned'),
            'applied':('Applied','state','ok'), 'rolled_back':('Rolled back','state','abandoned'),
            'superseded':('Superseded','state','abandoned')}
        assert set(expected) == set(PROPOSAL_STATUSES)
        for status,(label,attribute,value) in expected.items():
            item=page.locator('.rules-member').first.locator('[data-rule-status="'+status+'"]')
            expect(item).to_have_text(label)
            expect(item).to_have_attribute('data-'+attribute,value)
            assert status in item.get_attribute('aria-label')
        for width in (1280,1440):
            page.set_viewport_size({'width':width,'height':1024})
            for theme in ('light','dark'):
                keys.theme(theme);fits();capture(f'all-states-{theme}-{width}')
        page.emulate_media(forced_colors='active');fits();capture('all-states-forced-colors')
        assert set(mixed.all_text_contents())=={v[0] for v in expected.values()}
        page.emulate_media(forced_colors='none')
        page.unroute('**/api/rules/browse*')
        page.locator('#rules-refresh').click()
        expect(page.locator('.rules-member .badge').first).to_have_text('Pending')
        # Future status values retain their full text even when no word break
        # exists. Only unknown tokens may wrap within a word in the State cell.
        future='future_'+'version'*10
        def unknown_status(route):
            response=route.fetch();data=response.json()
            row=next(r['rule'] for r in data['rows'] if r['kind']=='rule')
            row['proposal_statuses']={future:1};route.fulfill(response=response,json=data)
        page.route('**/api/rules/browse*',unknown_status)
        # Leave the preceding pointer-focused Refresh before native traversal.
        page.keyboard.press('Tab')
        keys.activate(page.locator('#rules-refresh'))
        unknown=page.locator('.rules-member').first.locator('.badge')
        expect(unknown).to_have_text('Unknown status: '+future)
        expect(unknown).to_have_attribute('data-state','unknown')
        for width in (1280,1440):
            page.set_viewport_size({'width':width,'height':1024})
            for theme in ('light','dark'):
                keys.theme(theme);fits();capture(f'unknown-status-{theme}-{width}')
        page.emulate_media(forced_colors='active');fits();capture('unknown-status-forced-colors')
        expect(unknown).to_have_text('Unknown status: '+future)
        page.emulate_media(forced_colors='none');page.unroute('**/api/rules/browse*')
        keys.activate(page.locator('#rules-refresh'))
        expect(page.locator('.rules-member .badge').first).to_have_text('Pending')
        # Keep horizontal keyboard scrolling where the two-column view cannot fit.
        page.set_viewport_size({'width': 1100, 'height': 1024})
        wrap = page.locator('#rules-table-wrap'); wrap.focus(); page.keyboard.press('ArrowRight')
        page.wait_for_function('document.getElementById("rules-table-wrap").scrollLeft > 0')
        expect(wrap).to_be_focused()
        capture('narrow-keyboard-scroll')
        page.set_viewport_size({'width': 1280, 'height': 1024})
        fits()
        held = []
        page.route('**/api/rules/browse*', lambda route: held.append(route))
        page.locator('#rules-refresh').click()
        expect(page.locator('#rules-notice [role="status"]')).to_have_text('Reading rules…')
        expect(page.locator('#rules-refresh')).to_be_disabled()
        fits(); capture('loading')
        assert len(held) == 1
        held[0].fulfill(status=503, json={'detail': 'Invented rules reader failure'})
        page.unroute('**/api/rules/browse*')
        expect(page.locator('#rules-notice [role="alert"]')).to_contain_text('Invented rules reader failure')
        fits(); capture('read-error')
        page.locator('#rules-refresh').click()
        expect(page.locator('#rules-notice [role="alert"]')).to_have_count(0)
        expect(page.locator('#rules-refresh')).to_be_focused()
        fits()
        page.get_by_role('button', name='Hooks', exact=True).click()
        expect(page.locator('#rules-empty')).to_contain_text('No matching rules')
        expect(page.locator('#rules-table-wrap')).not_to_be_visible()
        capture('empty-filter')
        page.get_by_role('button', name='All', exact=True).click()
        expect(page.locator('#rules-table-wrap')).to_be_visible()
        # Filter changes clear the selection; explicitly reopen a result.
        expect(page.locator('#rules-inspector-host > #inspector')).to_have_count(0)
        page.locator('[data-rule-id="l00"]').click()
        expect(page.locator('#rules-inspector-host > #inspector')).to_be_visible()
        fits()
        assert all(method == 'GET' for method, url in result['requests'])
        assert all(Path(path).read_text() == text for path, text in manifest['targets'].items())
        with sqlite3.connect('file:' + manifest['db'] + '?mode=ro', uri=True) as db:
            assert list(db.iterdump()) == manifest['snapshot']
            assert db.execute('SELECT COUNT(*) FROM llm_calls').fetchone()[0] == 0
        assert not result['errors'], result['errors']
        result.update(store='unchanged', targets='unchanged', models=0)
        print('RULES_LAYOUT_OK ' + json.dumps({'captures': len(result['captures']), 'requests': len(result['requests']), 'models': 0, 'errors': [], 'store': 'unchanged', 'targets': 'unchanged'}))
    except BaseException:
        page.screenshot(path=str(OUT / 'failure.png'))
        (OUT / 'failure.html').write_text(page.content())
        raise
    finally:
        browser.close()
        (OUT / 'result.json').write_text(json.dumps(result, indent=2))
