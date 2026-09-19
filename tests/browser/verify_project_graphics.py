"""Recorded byte graphics and native-keyboard reasons on disposable real readers."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
from urllib.parse import quote, urlencode

from playwright.sync_api import expect, sync_playwright
from keyboard_actions import KeyboardActions
from visual_checks import VisualChecks

parser = argparse.ArgumentParser()
parser.add_argument('--fixture', choices=('project', 'projects'), required=True)
kind = parser.parse_args().fixture
ROOT = Path(__file__).resolve().parents[2]
fixture = 'project-composition' if kind == 'project' else 'projects-table'
manifest = json.loads((ROOT / 'reports/dashboard-parity' / fixture / 'manifest.json').read_text())
assert ('si-project-summary-ui-' if kind == 'project' else 'si-projects-ui-') in manifest['db']
OUT = ROOT / 'reports/dashboard-parity/project-graphics' / kind
OUT.mkdir(parents=True, exist_ok=True)
BASE = 'http://127.0.0.1:8876/'
files = [ROOT / 'src/self_improve/dashboard/static' / name for name in ('app.js', 'app.css')]
hashes = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}


def unchanged():
    with sqlite3.connect('file:' + manifest['db'] + '?mode=ro', uri=True) as db:
        assert list(db.iterdump()) == manifest['snapshot']
        assert db.execute('SELECT COUNT(*) FROM llm_calls').fetchone()[0] == 0
    assert all(Path(p).read_text() == text for p, text in manifest['targets'].items())
    assert hashes == {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}


with sync_playwright() as runtime:
    browser = runtime.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width': 1280, 'height': 1024})
    keys = KeyboardActions(page, OUT / 'keyboard.json')
    visual = VisualChecks(page, OUT / 'contrast.json')
    requests, errors, states = [], [], []
    page.on('request', lambda r: requests.append([r.method, r.url]))
    page.on('pageerror', lambda e: errors.append(str(e)))

    def capture(name, target):
        target.scroll_into_view_if_needed()
        visual.check(name)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert page.locator('#main').evaluate('e=>e.scrollWidth<=e.clientWidth+1')
        page.screenshot(path=str(OUT / (name + '.png')))
        target.screenshot(path=str(OUT / (name + '-detail.png')))
        (OUT / (name + '.html')).write_text(page.content())
        states.append(name)

    def matrix(name, target, disclosure=None):
        for width in (1280, 1440):
            page.set_viewport_size({'width': width, 'height': 1024})
            for theme in ('light', 'dark'):
                keys.theme(theme)
                if disclosure is not None and not disclosure.evaluate('e=>e.open'):
                    keys.activate(disclosure.locator('summary'))
                capture(f'{name}-{theme}-{width}', target)

    def benefit_geometry(details):
        return details.evaluate('''e=>{
          const p=e.querySelector('.project-benefit-content').getBoundingClientRect(),
            s=e.querySelector('summary').getBoundingClientRect(),
            m=document.querySelector('#main').getBoundingClientRect(),
            row=e.closest('tr').getBoundingClientRect();
          return {width:p.width,left:p.left,right:p.right,mainLeft:m.left,mainRight:m.right,
            height:row.height,above:p.bottom<s.top,below:p.top>s.bottom};
        }''')

    try:
        health = page.request.get(BASE + 'api/health').json()
        assert health['db_path'] == manifest['db'] and health['read_only']
        unchanged()
        url = BASE + '#/projects'
        if kind == 'project':
            url += '/' + quote(manifest['project_key'], safe='') + '?' + urlencode({'working_copy_id': manifest['copies']['alpha']})
        page.goto(url)
        page.wait_for_load_state('networkidle')
        page.screenshot(path=str(OUT / 'recon.png'))
        (OUT / 'recon.html').write_text(page.content())
        print('PROJECT_GRAPHICS_RECON', kind, page.get_by_role('heading').all_text_contents(), flush=True)
        if kind == 'project':
            panel = page.locator('.project-primary > .panel').filter(has=page.get_by_role('heading', name='Configured context and Markdown', exact=True))
            expect(panel.locator('.project-location-bar')).to_have_count(3)
            expect(panel).to_contain_text('Largest recorded group: 279 bytes')
            fills = panel.locator('.project-location-bar > span').evaluate_all('els=>els.map(e=>parseFloat(e.style.width))')
            assert all(abs(actual - expected / 279 * 100) < 0.001 for actual, expected in zip(fills, (279, 29, 114)))
            matrix('locations', panel)
            keys.activate(panel.get_by_text('Instruction ownership and complete inventory', exact=True))
            inventory = page.request.get(BASE + 'api/project-inventory?' + urlencode({'project_key': manifest['project_key'], 'working_copy_id': manifest['copies']['alpha'], 'limit': 1})).json()['records'][0]
            owners = ('human_managed', 'machine', 'edited', 'unknown')
            for i, file in enumerate(inventory['files'][:8]):
                source = panel.locator('.project-file').nth(i)
                expect(source.locator('.project-ownership-legend li')).to_have_count(4)
                for owner in owners:
                    amount = file['ownership'][owner]
                    legend = source.locator(f'.project-ownership-legend [data-owner="{owner}"]')
                    expect(legend).to_contain_text(f"{amount['bytes']:,} bytes · {amount['lines']:,} lines")
                    if file['bytes']:
                        fill = source.locator(f'.project-ownership-bar [data-owner="{owner}"]')
                        assert abs(float(fill.evaluate('e=>e.style.width').removesuffix('%')) - amount['bytes'] / file['bytes'] * 100) < 0.001
                expect(source.locator('.project-ownership-bar')).to_have_attribute('aria-hidden', 'true')
            expect(panel).to_contain_text('protected from automatic deletion')
            matrix('ownership', panel)
            page.emulate_media(forced_colors='active')
            capture('ownership-forced', panel)
            assert panel.locator('.project-location-bar > span').first.evaluate('e=>getComputedStyle(e).backgroundColor') != panel.locator('.project-location-bar').first.evaluate('e=>getComputedStyle(e).backgroundColor')
            page.emulate_media(forced_colors='none')
            keys.activate(panel.get_by_role('link', name='Complete configured context, measurements and history', exact=True))
            expect(page.locator('#project-context-status')).to_contain_text('2 shown · 2 observation times')
        else:
            rows = {r['project_key']: r for r in manifest['payload']['rows']}
            visible = page.locator('#projects-tbody > tr[data-project-key]')
            expect(visible.first.locator('.project-benefit-detail > summary')).to_have_count(1)
            checked = []
            for computable in (False, True):
                row = next(r for r in visible.all() if rows[r.get_attribute('data-project-key')]['benefit']['computable'] is computable)
                benefit = rows[row.get_attribute('data-project-key')]['benefit']
                details = row.locator('.project-benefit-detail')
                summary = details.locator('summary')
                expect(summary).to_contain_text(str(benefit['value']))
                keys.activate(summary)
                expect(page.locator('#view-projects')).to_be_visible()
                expect(details).to_have_attribute('open', '')
                expect(details.locator('.project-benefit-reason')).to_have_text(benefit['reason'])
                expect(details.locator('.project-benefit-meaning')).to_have_text(benefit['meaning'])
                geometry = benefit_geometry(details)
                assert 320 <= geometry['width'] <= 352 and geometry['height'] < 110, geometry
                assert geometry['left'] >= geometry['mainLeft'] and geometry['right'] <= geometry['mainRight'], geometry
                checked.append({'computable': computable, 'reason': benefit['reason']})
                matrix('known' if computable else 'unavailable', page.locator('#projects-table-wrap'), details)
                keys.activate(summary, 'Space')
                expect(details).not_to_have_attribute('open', '')
            # The last row opens upward; opening it closes the previous native group.
            first = visible.first.locator('.project-benefit-detail')
            last = visible.last.locator('.project-benefit-detail')
            keys.activate(first.locator('summary'))
            next_context = visible.nth(1).locator('.project-context a')
            keys.reach(next_context)
            assert next_context.evaluate('e=>{const b=e.getBoundingClientRect();return e.contains(document.elementFromPoint(b.left+b.width/2,b.top+b.height/2))}'), 'Open benefit panel obscures focus in another row'
            if not first.evaluate('e=>e.open'):
                keys.activate(first.locator('summary'))
            keys.activate(last.locator('summary'))
            expect(first).not_to_have_attribute('open', '')
            assert benefit_geometry(last)['above']
            link = last.get_by_role('link', name='Inspect recurrence evidence', exact=True)
            keys.reach(link)
            assert link.evaluate('e=>{const b=e.getBoundingClientRect();return e.contains(document.elementFromPoint(b.left+b.width/2,b.top+b.height/2))}')
            capture('last-row', page.locator('#projects-table-wrap'))
            keys.activate(last.locator('summary'), 'Space')
            # One filtered row has room below, with no clipping or horizontal spill.
            keys.text(page.locator('#projects-search'), 'alpha-retained-long-working-copy-path')
            keys.activate(page.get_by_role('button', name='Search', exact=True))
            expect(visible).to_have_count(1)
            details = visible.first.locator('.project-benefit-detail')
            keys.activate(details.locator('summary'))
            assert benefit_geometry(details)['below']
            keys.reach(details.get_by_role('link', name='Inspect recurrence evidence', exact=True))
            capture('one-row', details)
            keys.activate(page.get_by_role('button', name='Clear', exact=True))
            expect(visible).to_have_count(10)
            details = visible.first.locator('.project-benefit-detail')
            summary = details.locator('summary')
            if not details.evaluate('e=>e.open'):
                keys.activate(summary)
            held = []
            page.route('**/api/projects', lambda r: held.append(r))
            keys.activate(page.get_by_role('button', name='Refresh', exact=True))
            expect(page.locator('#projects-read-state')).to_contain_text('Reading repositories')
            keys.reach(summary)
            assert len(held) == 1
            held.pop().fulfill(response=page.request.get(BASE + 'api/projects'))
            expect(page.locator('#projects-read-state')).to_be_empty()
            expect(summary).to_be_focused()
            expect(details).to_have_attribute('open', '')
            page.unroute('**/api/projects')
            page.emulate_media(forced_colors='active')
            capture('reason-forced', page.locator('#projects-table-wrap'))
            keys.activate(details.get_by_role('link', name='Inspect recurrence evidence', exact=True))
            page.wait_for_load_state('networkidle')
            assert 'tab=recurrence' in page.url
            expect(page.locator('#project-detail')).to_be_visible()
            page.go_back()
            expect(page.locator('#view-projects')).to_be_visible()
        visual.assert_clean()
        assert not errors, errors
        assert all(method == 'GET' and url.startswith(BASE) for method, url in requests), requests
        unchanged()
        result = {'fixture': kind, 'states': states, 'requests': len(requests), 'model_calls': 0, 'data_and_targets_unchanged': True, 'errors': errors, 'source_hashes': hashes}
        (OUT / 'result.json').write_text(json.dumps(result, indent=2))
        print('PROJECT_GRAPHICS_OK', json.dumps(result), flush=True)
    except BaseException:
        page.screenshot(path=str(OUT / 'failure.png'))
        (OUT / 'failure.html').write_text(page.content())
        raise
    finally:
        unchanged()
        keys.save()
        browser.close()
