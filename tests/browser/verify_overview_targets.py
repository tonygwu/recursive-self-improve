"""Exact-run stage targets with multiple runs per night; temporary fixtures only."""
from pathlib import Path
import argparse
import hashlib
import json
import sqlite3
import struct
from urllib.parse import quote
import tempfile

from playwright.sync_api import expect, sync_playwright
from keyboard_actions import KeyboardActions
from visual_checks import VisualChecks

ROOT = Path(__file__).resolve().parents[2]
BASE = 'http://127.0.0.1:8876/'
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--output', type=Path, default=ROOT/'reports/dashboard-parity/overview-targets')
args = parser.parse_args()
OUT = args.output.resolve()
OUT.mkdir(parents=True, exist_ok=True)
manifest = json.loads((ROOT/'reports/dashboard-parity/overview-manifest.json').read_text())
assert Path(manifest['db']).parent.parent.name.startswith('si-overview-ui-')
assert manifest['latest_run'] == 'same-z'
result = {'status': 'attempted', 'states': [], 'activations': [], 'requests': [], 'errors': [],
          'source_hashes': {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                            for p in [ROOT/'src/self_improve/dashboard/static/app.css',
                                      ROOT/'src/self_improve/dashboard/static/app.js', Path(__file__).resolve()]}}


def unchanged():
    with sqlite3.connect('file:'+manifest['db']+'?mode=ro', uri=True) as db:
        assert list(db.iterdump()) == manifest['snapshot']
        assert db.execute('SELECT COUNT(*) FROM llm_calls').fetchone()[0] == 0
    assert all(Path(p).read_text() == content for p, content in manifest['targets'].items())
    for p, digest in result['source_hashes'].items():
        assert hashlib.sha256((ROOT/p).read_bytes()).hexdigest() == digest


with tempfile.TemporaryDirectory(prefix='si-overview-target-browser-') as folder, sync_playwright() as runtime:
    temporary = Path(folder)
    extension = temporary/'extension'
    extension.mkdir()
    (extension/'manifest.json').write_text(json.dumps({'manifest_version': 3, 'name': 'Temporary target zoom',
        'version': '1.0', 'host_permissions': ['http://127.0.0.1/*'], 'background': {'service_worker': 'worker.js'}}))
    (extension/'worker.js').write_text('chrome.runtime.onInstalled.addListener(() => {});')
    browser = runtime.chromium.launch_persistent_context(str(temporary/'profile'), channel='chromium', headless=True,
        args=[f'--disable-extensions-except={extension}', f'--load-extension={extension}'], viewport={'width': 1280, 'height': 1024})
    page = browser.pages[0]
    zoom = browser.service_workers[0] if browser.service_workers else browser.wait_for_event('serviceworker')
    keys = KeyboardActions(page, OUT/'keyboard.json')
    visual = VisualChecks(page, OUT/'contrast.json')
    page.on('request', lambda r: result['requests'].append([r.method, r.url]))
    page.on('pageerror', lambda e: result['errors'].append(str(e)))

    def goto():
        page.goto(BASE+'#/overview')
        page.wait_for_load_state('networkidle')
        expect(page.locator('#ov-latest dt a')).to_have_count(5)

    def set_zoom(factor):
        actual = zoom.evaluate('''async factor=>{const [tab]=await chrome.tabs.query({active:true,currentWindow:true});
            await chrome.tabs.setZoom(tab.id,factor);return chrome.tabs.getZoom(tab.id);}''', factor)
        assert actual == factor
        page.wait_for_function('factor=>Math.abs(devicePixelRatio-factor)<.02', arg=factor)

    try:
        goto()
        health = page.request.get(BASE+'api/health').json()
        assert health['db_path'] == manifest['db'] and health['read_only']
        page.screenshot(path=str(OUT/'reconnaissance.png'))
        (OUT/'reconnaissance.html').write_text(page.content())
        print('OVERVIEW_TARGET_RECON', page.get_by_role('heading').all_text_contents(), flush=True)
        for width in (1280, 1440):
            page.set_viewport_size({'width': width, 'height': 1024})
            for factor in (1, 2):
                set_zoom(factor)
                for theme in ('light', 'dark'):
                    goto()
                    keys.theme(theme)
                    key = f'{width}-{theme}-zoom-{factor}'
                    anchors = page.locator('#ov-latest dt a')
                    metrics = anchors.evaluate_all('''nodes=>nodes.map(e=>{const r=e.getBoundingClientRect();return {
                        stage:e.textContent,href:e.getAttribute('href'),x:r.x,y:r.y,width:r.width,height:r.height,
                        gridEquivalent:[...document.querySelectorAll('#ov-grid a')].some(a=>a.getAttribute('href')===e.getAttribute('href'))};})''')
                    result['states'].append({'key': key, 'targets': metrics, 'viewport': page.evaluate('({width:innerWidth,height:innerHeight,dpr:devicePixelRatio})')})
                    assert all(not m['gridEquivalent'] for m in metrics), metrics
                    assert [m['stage'] for m in metrics] == ['scan', 'mine', 'cluster', 'gate', 'apply']
                    assert [m['href'] for m in metrics] == [
                        '#/overview/run/' + quote(manifest['latest_run'], safe='') + '/' + stage
                        for stage in ('scan', 'mine', 'cluster', 'gate', 'apply')], metrics
                    assert all(m['width'] >= 24 and m['height'] >= 24 for m in metrics), metrics
                    keys.reach(anchors.first)
                    visual.check(key)
                    png = page.screenshot(path=str(OUT/(key+'.png')))
                    dimensions = struct.unpack('>II', png[16:24])
                    assert dimensions == (width, 1024), dimensions
                    result['states'][-1]['screenshot_pixels'] = list(dimensions)
                    (OUT/(key+'.html')).write_text(page.content())
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    # Native Tab traverses all five anchors; Enter verifies the selected run.
                    for m in metrics:
                        target = page.locator('#ov-latest dt a').filter(has_text=m['stage'])
                        keys.reach(target)
                    keys.activate(anchors.first)
                    expect(page).to_have_url(BASE+metrics[0]['href'])
                    expect(page.locator('#run-detail')).to_be_visible()
                    result['activations'].append({'state': key, 'method': 'keyboard', 'href': metrics[0]['href']})
                    # Hit the padding near the top edge, not just the text glyphs.
                    for m in metrics:
                        goto()
                        target = page.locator('#ov-latest dt a').filter(has_text=m['stage'])
                        target.scroll_into_view_if_needed()
                        point = target.evaluate('''e=>{const r=e.getBoundingClientRect(),x=r.x+r.width/2,y=r.y+1;
                            return {x,y,hit:e.contains(document.elementFromPoint(x,y))};}''')
                        assert point['hit'], point
                        page.mouse.click(point['x'], point['y'])
                        expect(page).to_have_url(BASE+m['href'])
                        expect(page.locator('#run-detail')).to_be_visible()
                        result['activations'].append({'state': key, 'method': 'pointer-edge', 'href': m['href'], 'point': point})
                    goto()
                    page.locator('#ov-deliveries').scroll_into_view_if_needed()
                    png = page.screenshot(path=str(OUT/(key+'-long-content.png')))
                    assert struct.unpack('>II', png[16:24]) == (width, 1024)
                    lower = page.locator('.overview-lower').evaluate('''e=>{
                        const box=n=>{const r=n.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height,bottom:r.bottom};};
                        return {grid:box(e),columns:[...e.children].map(box)};}''')
                    result['states'][-1]['lower'] = lower
                    left, right = lower['columns']
                    if factor == 2:
                        assert abs(left['width'] - lower['grid']['width']) <= 1, lower
                        assert abs(right['width'] - lower['grid']['width']) <= 1, lower
                        assert right['y'] >= left['bottom'], lower
                    else:
                        assert abs(left['y'] - right['y']) <= 1 and left['x'] < right['x'], lower
                    print('OVERVIEW_TARGET_STATE', key, 'five targets and exact routes pass', flush=True)
                set_zoom(1)
        assert not result['errors'], result['errors']
        assert all(method == 'GET' and url.startswith(BASE) for method, url in result['requests'])
        visual.assert_clean()
        result['status'] = 'succeeded'
        print('OVERVIEW_TARGETS_OK', len(result['states']), 'states', len(result['activations']), 'exact activations', flush=True)
    except BaseException as error:
        result.update(status='failed', error=str(error))
        page.screenshot(path=str(OUT/'failure.png'))
        (OUT/'failure.html').write_text(page.content())
        raise
    finally:
        unchanged()
        result.update(database='unchanged', targets='unchanged', source='unchanged', model_calls=0)
        (OUT/'result.json').write_text(json.dumps(result, indent=2)+'\n')
        browser.close()
