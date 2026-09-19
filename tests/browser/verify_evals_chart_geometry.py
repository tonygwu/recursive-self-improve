"""Read-only vertical rate cells and exact native inspection in temporary Evals."""
from pathlib import Path
from urllib.parse import urlencode
import argparse
import json
import sqlite3
import tempfile

from playwright.sync_api import sync_playwright, expect
from keyboard_actions import KeyboardActions
from visual_checks import VisualChecks

parser = argparse.ArgumentParser()
parser.add_argument('--fixture', choices=['density', 'markers'], default='density')
args = parser.parse_args()
ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'reports/dashboard-parity/evals-chart' / args.fixture
OUT.mkdir(parents=True, exist_ok=True)
source = 'evals-density' if args.fixture == 'density' else 'trend-markers'
info = json.loads((ROOT / 'reports/dashboard-parity' / source / 'manifest.json').read_text())
assert ('si-' + source + '-') in info['db']
BASE = 'http://127.0.0.1:8876/'
result = {'states': [], 'requests': [], 'errors': [], 'geometry': []}

with tempfile.TemporaryDirectory(prefix='si-evals-chart-browser-') as temp, sync_playwright() as runtime:
    folder = Path(temp)
    extension = folder / 'extension'
    extension.mkdir()
    (extension / 'manifest.json').write_text(json.dumps({'manifest_version': 3,
        'name': 'Temporary Evals zoom', 'version': '1.0', 'host_permissions': ['http://127.0.0.1/*'],
        'background': {'service_worker': 'worker.js'}}))
    (extension / 'worker.js').write_text('chrome.runtime.onInstalled.addListener(() => {});')
    browser = runtime.chromium.launch_persistent_context(str(folder / 'profile'), channel='chromium',
        headless=True, args=[f'--disable-extensions-except={extension}', f'--load-extension={extension}'],
        viewport={'width': 1280, 'height': 1024})
    page = browser.pages[0]
    worker = browser.service_workers[0] if browser.service_workers else browser.wait_for_event('serviceworker')
    page.on('pageerror', lambda error: result['errors'].append(str(error)))
    page.on('request', lambda request: result['requests'].append([request.method, request.url]))
    keys = KeyboardActions(page, OUT / 'keyboard.json')
    visual = VisualChecks(page, OUT / 'contrast.json')

    def capture(name, target=None):
        if target is not None:
            target.scroll_into_view_if_needed()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert not page.locator('.trend-matrix td, .trend-matrix th').evaluate_all('''cells=>cells.flatMap(el=>{
            const r=el.getBoundingClientRect(), range=document.createRange();range.selectNodeContents(el);
            return [...range.getClientRects()].some(t=>t.left<r.left-1||t.right>r.right+1)?[el.textContent]:[];
        })''')
        page.screenshot(path=str(OUT / (name + '.png')))
        (OUT / (name + '.html')).write_text(page.content())
        visual.check(name)
        result['states'].append(name)

    def focus_visible(target):
        keys.focused(target)
        assert target.evaluate('''el=>{const r=el.getBoundingClientRect(),hit=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);return hit===el||el.contains(hit)}''')

    def geometry():
        checks = page.evaluate('''async()=>{
            const m=await import('/app.js'),data=m.state.trends.data,rows=[];
            for(const signal of [null,...data.signal_types]){
                const get=p=>signal==null?p:p.signals[signal],rates=data.series.map(p=>get(p).rate_per_100k).filter(x=>x!=null),peak=rates.length?Math.max(...rates):null;
                for(const p of data.series){
                    const rate=get(p).rate_per_100k,cell=document.getElementById(`trend-value-${signal||'all'}-${p.month}`),button=cell.querySelector('button'),bar=cell.querySelector('.spark__bar'),area=cell.querySelector('.spark'),label=cell.querySelector('.trend-value');
                    rows.push({signal,month:p.month,rate,peak,tabindex:cell.tabIndex,buttonTab:button.tabIndex,area:area.getBoundingClientRect().height,
                        height:bar?.getBoundingClientRect().height??null,width:bar?.getBoundingClientRect().width??null,
                        expected:rate==null?null:rate===0?0:Math.max(1,34*rate/peak),labelVisible:label.checkVisibility(),label:label.textContent});
                }
            }return rows;
        }''')
        for item in checks:
            assert item['tabindex'] == -1 and item['buttonTab'] == 0, item
            assert item['area'] == 34, item
            if item['rate'] is None:
                assert item['height'] is None and item['labelVisible'] and item['label'] == '—', item
            else:
                assert abs(item['height'] - item['expected']) < 0.1, item
                assert item['labelVisible'] == (item['rate'] == 0), item
        result['geometry'].append(checks)

    def inspect(month, signal, key='Enter'):
        button = page.locator(f'#trend-point-{signal}-{month}')
        label = button.get_attribute('aria-label')
        url = page.url
        keys.activate(button, key=key)
        summary = page.locator('#trend-context-toggle-' + month)
        focus_visible(summary)
        detail = page.locator('#trend-context-' + month)
        expect(detail).to_have_attribute('open', '')
        expect(detail).to_contain_text('Exact monthly rates')
        expect(detail).to_contain_text('eligible physical lines')
        expect(detail).to_contain_text('known sessions')
        assert label.split('. ', 1)[1].removesuffix(' Inspect exact monthly rates.') in detail.inner_text()
        assert page.url == url
        return summary, detail

    try:
        query = '' if args.fixture == 'density' else '?' + urlencode({'compatibility_key': info['version']})
        page.goto(BASE + '#/evals' + query)
        page.wait_for_load_state('networkidle')
        capture('reconnaissance')
        (OUT / 'buttons.json').write_text(json.dumps(page.locator('#view-evals button').all_text_contents()))
        matrix = page.locator('.trend-matrix')
        for width in (1280, 1440):
            page.set_viewport_size({'width': width, 'height': 1024})
            for theme in ('light', 'dark'):
                keys.theme(theme)
                page.locator('#main').evaluate('el=>el.scrollTop=0')
                last = matrix.locator('tbody tr:not(.trend-workload)').last.bounding_box()
                assert last['y'] + last['height'] <= 1000, last
                geometry()
                capture(f'chart-{theme}-{width}')
                summary, detail = inspect('2030-09', 'correction', 'Space' if theme == 'dark' else 'Enter')
                expect(detail).to_contain_text('Partial month')
                capture(f'exact-{theme}-{width}', detail)
                page.evaluate('async()=>{const m=await import("/app.js");m.state.evaluationHealth={error:"Invented adjacent read failure"};m.paintTrends();}')
                focus_visible(summary)
                expect(detail).to_have_attribute('open', '')
                keys.activate(summary)
                button = page.locator('#trend-point-correction-2030-09')
                keys.reach(button)
                page.evaluate('async()=>{const m=await import("/app.js");m.state.evaluationHealth={loading:true};m.paintTrends();}')
                focus_visible(button)
        # Programmatic compatibility never adds a duplicate native Tab stop.
        old_cell = page.locator('#trend-value-correction-2030-09')
        old_cell.focus()
        page.evaluate('async()=>{const m=await import("/app.js");m.state.evaluationHealth={error:"Second invented state"};m.paintTrends();}')
        expect(old_cell).to_be_focused()
        if args.fixture == 'markers':
            for month, cause in [('2030-06', 'No eligible exposure'), ('2030-07', 'Insufficient sessions'), ('2030-08', '0 per 100,000 lines')]:
                summary, detail = inspect(month, 'correction')
                expect(detail).to_contain_text(cause)
                capture('exact-' + month, detail)
                keys.activate(summary)
            page.goto(BASE + '#/evals?' + urlencode({'compatibility_key': info['unknown_version']}))
            page.wait_for_load_state('networkidle')
            geometry()
            summary, detail = inspect('2030-09', 'correction')
            expect(detail).to_contain_text('Unknown detector version; no rate')
            expect(page.locator('.trend-peak')).to_have_text(['—'] * 8)
            capture('unknown-config', detail)
            page.goto(BASE + '#/evals' + query)
            page.wait_for_load_state('networkidle')
        # Deliberately synthetic in-memory display values exercise shape and
        # tiny-number precision; they never masquerade as persisted measurements.
        page.evaluate('''async()=>{const m=await import('/app.js');window.ra54Saved=m.state.trends.data;
            const data=structuredClone(window.ra54Saved),a=data.series[5],b=data.series[6];
            a.rate_per_100k=50;b.rate_per_100k=100;
            a.signals.correction.rate_per_100k=4;b.signals.correction.rate_per_100k=2;
            a.signals.standing_instruction.rate_per_100k=100;b.signals.standing_instruction.rate_per_100k=0.0001;
            m.state.trends.data=data;m.paintTrends();}''')
        geometry()
        tiny = page.locator('#trend-point-standing_instruction-2030-09 .spark__bar')
        assert tiny.bounding_box()['height'] == 1
        summary, detail = inspect('2030-09', 'standing_instruction')
        expect(detail).to_contain_text('0.0001 per 100,000 lines')
        capture('synthetic-tiny-rate', detail)
        page.evaluate('async()=>{const m=await import("/app.js");m.state.trends.data=window.ra54Saved;delete window.ra54Saved;m.paintTrends();}')
        for width in (640, 1440):
            page.set_viewport_size({'width': width, 'height': 1024})
            if width == 1440:
                worker.evaluate('async()=>{const [tab]=await chrome.tabs.query({active:true,currentWindow:true});await chrome.tabs.setZoom(tab.id,2);}')
                assert worker.evaluate('async()=>{const [tab]=await chrome.tabs.query({active:true,currentWindow:true});return chrome.tabs.getZoom(tab.id);}') == 2
            # Resizing/zooming keeps the old focused node even if it moves out
            # of view. Start fresh native traversal after changing the viewport.
            page.keyboard.press('Tab')
            summary, detail = inspect('2030-09', 'correction')
            capture('narrow-exact' if width == 640 else 'native-200-percent-exact', detail)
            keys.activate(summary)
            keys.reach(page.locator('#trend-point-correction-2030-09'))
            focus_visible(page.locator('#trend-point-correction-2030-09'))
            capture('narrow-chart' if width == 640 else 'native-200-percent-chart')
        worker.evaluate('async()=>{const [tab]=await chrome.tabs.query({active:true,currentWindow:true});await chrome.tabs.setZoom(tab.id,1);}')
        page.keyboard.press('Tab')
        page.emulate_media(forced_colors='active')
        keys.reach(page.locator('#trend-point-correction-2030-09'))
        capture('forced-colors-chart')
        summary, detail = inspect('2030-09', 'correction')
        capture('forced-colors-exact', detail)
        page.emulate_media(forced_colors='none')
        assert not result['errors'], result['errors']
        assert all(method == 'GET' for method, url in result['requests'])
        with sqlite3.connect('file:' + info['db'] + '?mode=ro', uri=True) as db:
            assert list(db.iterdump()) == info['sql']
            assert db.execute('SELECT COUNT(*) FROM llm_calls').fetchone()[0] == 0
        assert all(Path(path).read_text() == text for path, text in info['targets'].items())
        visual.assert_clean()
        result.update(store='unchanged', targets='unchanged', models=0)
        print('EVALS_CHART_OK', args.fixture, len(result['states']), 'states;', len(result['requests']), 'GETs; unchanged Store/targets; zero models')
    except BaseException:
        page.screenshot(path=str(OUT / 'failure.png'))
        (OUT / 'failure.html').write_text(page.content())
        raise
    finally:
        keys.save()
        browser.close()
        (OUT / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
