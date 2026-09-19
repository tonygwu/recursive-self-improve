"""Complete recovery destination paths and exact cost previews on temporary state."""
from pathlib import Path
import argparse
import hashlib
import json
import sqlite3
import struct
import tempfile
from urllib.parse import quote, unquote

from playwright.sync_api import expect, sync_playwright
from keyboard_actions import KeyboardActions
from visual_checks import VisualChecks

ROOT = Path(__file__).resolve().parents[2]
BASE = 'http://127.0.0.1:8876/'
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--output', type=Path, default=ROOT/'reports/dashboard-parity/recovery-paths/green')
args = parser.parse_args()
OUT = args.output.resolve()
OUT.mkdir(parents=True, exist_ok=True)
manifest = json.loads((ROOT/'reports/dashboard-parity/accessibility/manifest.json').read_text())
assert Path(manifest['db']).parent.parent.name.startswith('si-accessibility-ui-')
MEASURE = r'''e => {
 const p=e.parentElement.getBoundingClientRect(),range=document.createRange();range.selectNodeContents(e);
 const fragments=[...range.getClientRects()].map(r=>({left:r.left,right:r.right,top:r.top,bottom:r.bottom,width:r.width}));
 return {text:e.textContent,parent:{left:p.left,right:p.right,width:p.width},fragments,
   escaped:fragments.filter(r=>r.left<p.left-1||r.right>p.right+1),overflow_wrap:getComputedStyle(e).overflowWrap,
   viewport:{width:innerWidth,height:innerHeight,dpr:devicePixelRatio,document_width:document.documentElement.scrollWidth}};
}'''
result = {'status': 'attempted', 'states': [], 'activations': [], 'requests': [], 'direct_gets': [], 'errors': [],
          'source_hashes': {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                           for p in [Path(__file__).resolve(), ROOT/'src/self_improve/dashboard/static/app.js',
                                     ROOT/'src/self_improve/dashboard/static/app.css']}}


def unchanged():
    with sqlite3.connect('file:'+manifest['db']+'?mode=ro', uri=True) as db:
        assert list(db.iterdump()) == manifest['snapshot']
        assert db.execute('SELECT COUNT(*) FROM llm_calls').fetchone()[0] == 0
    assert all(Path(path).read_text() == text for path, text in manifest['targets'].items())
    for path, digest in result['source_hashes'].items():
        assert hashlib.sha256((ROOT/path).read_bytes()).hexdigest() == digest


with tempfile.TemporaryDirectory(prefix='si-recovery-path-browser-') as folder, sync_playwright() as runtime:
    temporary = Path(folder)
    extension = temporary/'extension'
    extension.mkdir()
    (extension/'manifest.json').write_text(json.dumps({'manifest_version': 3, 'name': 'Temporary recovery path zoom',
        'version': '1.0', 'host_permissions': ['http://127.0.0.1/*'], 'background': {'service_worker': 'worker.js'}}))
    (extension/'worker.js').write_text('chrome.runtime.onInstalled.addListener(() => {});')
    browser = runtime.chromium.launch_persistent_context(str(temporary/'profile'), channel='chromium', headless=True,
        args=[f'--disable-extensions-except={extension}', f'--load-extension={extension}'], viewport={'width': 1280, 'height': 1024})
    page = browser.pages[0]
    zoom = browser.service_workers[0] if browser.service_workers else browser.wait_for_event('serviceworker')
    keys = KeyboardActions(page, OUT/'keyboard.json')
    visual = VisualChecks(page, OUT/'contrast.json')
    page.on('request', lambda request: result['requests'].append([request.method, request.url]))
    page.on('pageerror', lambda error: result['errors'].append(str(error)))

    def capture(key, width):
        png = page.screenshot(path=str(OUT/(key+'.png')))
        assert struct.unpack('>II', png[16:24]) == (width, 1024)
        (OUT/(key+'.html')).write_text(page.content())
        visual.check(key)

    def open_options(theme, selection):
        page.goto('about:blank')
        page.goto(BASE+'#/review/recovery/'+quote(json.dumps(selection, separators=(',', ':')), safe=''))
        page.wait_for_load_state('networkidle')
        expect(page.locator('#eval-job-body')).to_contain_text('Choose the destination for this proposal.')
        expect(page.locator('#eval-job-body [role=alert]')).to_have_count(0)
        keys.theme(theme)

    try:
        page.goto(BASE+'#/review')
        page.wait_for_load_state('networkidle')
        for path in ['api/health', 'api/learnings/demo-rule-1/recovery-options']:
            response = page.request.get(BASE+path)
            assert response.ok
            result['direct_gets'].append(['GET', BASE+path])
            if path == 'api/health':
                health = response.json()
                assert health['db_path'] == manifest['db'] and health['read_only']
            else:
                options = response.json()
        for mode in ('hook', 'correct_target'):
            selection = {'learning_id': 'demo-rule-1', 'mode': mode, 'target_id': '', 'proposal_ids': ['demo-proposal-1']}
            choices = [target for target in options['targets'] if mode in target['modes']]
            assert choices
            for width in (1280, 1440):
                page.set_viewport_size({'width': width, 'height': 1024})
                for factor in (1, 2):
                    for theme in ('light', 'dark'):
                        open_options(theme, selection)
                        actual = zoom.evaluate('''async factor=>{const [tab]=await chrome.tabs.query({active:true,currentWindow:true});
                            await chrome.tabs.setZoom(tab.id,factor);return chrome.tabs.getZoom(tab.id);}''', factor)
                        assert actual == factor
                        page.wait_for_function('n=>Math.abs(devicePixelRatio-n)<.02', arg=factor)
                        key = f'{mode}-{width}-{theme}-zoom-{factor}'
                        state = {'key': key, 'zoom_receipt': actual, 'paths': []}
                        result['states'].append(state)
                        expect(page.locator('#eval-job-body > p > code')).to_have_count(len(choices))
                        for index, choice in enumerate(choices):
                            code = page.locator('#eval-job-body > p > code').nth(index)
                            expected_path = choice['destination']['target_path']
                            expect(code).to_have_text(expected_path)
                            code.scroll_into_view_if_needed()
                            measured = code.evaluate(MEASURE)
                            state['paths'].append(measured)
                            capture(key+f'-path-{index}', width)
                            assert measured['text'] == expected_path
                            assert measured['fragments'] and not measured['escaped'], measured
                            expect(code).to_be_in_viewport(ratio=1)
                            assert abs(measured['viewport']['width']*factor-width) <= 2
                            assert measured['viewport']['document_width'] <= measured['viewport']['width']
                            if choice['available']:
                                target = page.locator('#eval-job-body').get_by_role('link', name=choice['label'], exact=True)
                                href = target.get_attribute('href')
                                selected = json.loads(unquote(href.split('/recovery/', 1)[1]))
                                assert selected == dict(selection, target_id=choice['id']), selected
                                keys.activate(target)
                                expect(page).to_have_url(BASE+href)
                                preview_path = page.locator('#eval-job-body > p.delivery-path')
                                expect(preview_path).to_have_text(expected_path)
                                expect(page.locator('#eval-job-body')).to_contain_text('Maximum: 1 logical model call.')
                                expect(page.locator('#eval-job-submit')).to_be_visible()
                                expect(page.locator('#eval-job-body [role=alert]')).to_have_count(0)
                                expect(page.locator('#eval-job-title')).to_be_focused()
                                preview_path.scroll_into_view_if_needed()
                                capture(key+f'-preview-{index}', width)
                                result['activations'].append({'state': key, 'target_id': choice['id'], 'href': href,
                                                              'path': expected_path, 'method': 'Enter', 'preview_loaded': True})
                            open_options(theme, selection)
                        print('RECOVERY_PATH_STATE', key, len(choices), 'complete paths and exact previews', flush=True)
        assert not result['errors'], result['errors']
        assert all(method == 'GET' and url.startswith(BASE) for method, url in result['requests']+result['direct_gets'])
        visual.assert_clean()
        result['status'] = 'succeeded'
        print('RECOVERY_PATHS_OK', len(result['states']), 'states', len(result['activations']), 'exact loaded cost previews', flush=True)
    except BaseException as error:
        result.update(status='failed', error=str(error))
        page.screenshot(path=str(OUT/'failure.png'))
        (OUT/'failure.html').write_text(page.content())
        raise
    finally:
        unchanged()
        result.update(store='unchanged', targets='unchanged', source='unchanged', model_calls=0)
        (OUT/'result.json').write_text(json.dumps(result, indent=2)+'\n')
        keys.save()
        browser.close()
