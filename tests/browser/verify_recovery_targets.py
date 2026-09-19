"""Read-only recovery target geometry and exact previews on temporary state."""
from pathlib import Path
import argparse
import hashlib
import json
import sqlite3
import struct
import tempfile
from urllib.parse import unquote

from playwright.sync_api import expect, sync_playwright
from keyboard_actions import KeyboardActions
from visual_checks import VisualChecks

ROOT = Path(__file__).resolve().parents[2]
BASE = 'http://127.0.0.1:8876/'
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--origins', nargs='+', choices=['review', 'rules'], default=['review', 'rules'])
parser.add_argument('--output', type=Path, default=ROOT/'reports/dashboard-parity/recovery-targets/green')
args = parser.parse_args()
OUT = args.output.resolve()
OUT.mkdir(parents=True, exist_ok=True)
manifest = json.loads((ROOT/'reports/dashboard-parity/accessibility/manifest.json').read_text())
assert Path(manifest['db']).parent.parent.name.startswith('si-accessibility-ui-')
LABELS = ['Request a hook proposal…', 'Request a corrected target…']
MODES = ['hook', 'correct_target']
SELECTORS = {'review': '.review-member a.review-link',
             'rules': '#inspector-body > section > p > a.review-link[href^="#/review/recovery/"]'}
MEASURE = r'''e=>{
 const box=r=>({left:r.left,right:r.right,top:r.top,bottom:r.bottom,width:r.width,height:r.height});
 const r=e.getBoundingClientRect(),x=r.left+r.width/2,y=r.top+r.height/2;
 const points=[[x,y],[x-11.9,y-11.9],[x+11.9,y-11.9],[x-11.9,y+11.9],[x+11.9,y+11.9],
               [x,r.top+1],[x,r.bottom-1],[r.left+2,y],[r.right-2,y]];
 return {text:e.textContent,href:e.getAttribute('href'),box:box(r),
   fragments:[...e.getClientRects()].map(box),display:getComputedStyle(e).display,radius:getComputedStyle(e).borderRadius,
   right_boundary:[.1,.5,1,2].map(inset=>{const h=document.elementFromPoint(r.right-inset,y);return {inset,matched:h===e||e.contains(h)};}),
   hits:points.map(([x,y])=>{const h=document.elementFromPoint(x,y);return {x,y,matched:h===e||e.contains(h),hit_tag:h?.tagName};})};}'''
result = {'status': 'attempted', 'states': [], 'activations': [], 'requests': [], 'errors': [],
          'source_hashes': {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                            for p in [ROOT/'src/self_improve/dashboard/static/app.css',
                                      ROOT/'src/self_improve/dashboard/static/app.js', Path(__file__).resolve()]}}


def unchanged():
    with sqlite3.connect('file:'+manifest['db']+'?mode=ro', uri=True) as db:
        assert list(db.iterdump()) == manifest['snapshot']
        assert db.execute('SELECT COUNT(*) FROM llm_calls').fetchone()[0] == 0
    assert all(Path(p).read_text() == text for p, text in manifest['targets'].items())
    for p, digest in result['source_hashes'].items():
        assert hashlib.sha256((ROOT/p).read_bytes()).hexdigest() == digest


with tempfile.TemporaryDirectory(prefix='si-recovery-target-browser-') as folder, sync_playwright() as runtime:
    temporary = Path(folder)
    extension = temporary/'extension'
    extension.mkdir()
    (extension/'manifest.json').write_text(json.dumps({'manifest_version': 3, 'name': 'Temporary recovery zoom',
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

    def open_origin(theme, origin):
        page.goto('about:blank')
        page.goto(BASE+('#/review' if origin == 'review' else '#/rules/demo-rule-1'))
        page.wait_for_load_state('networkidle')
        keys.theme(theme)
        if origin == 'review':
            keys.activate(page.locator('[data-review-individual]'))
            page.wait_for_load_state('networkidle')
            expect(page.locator('.review-member')).to_have_count(1)
        expect(page.locator(SELECTORS[origin])).to_have_count(2)

    def capture(key, width):
        png = page.screenshot(path=str(OUT/(key+'.png')))
        assert struct.unpack('>II', png[16:24]) == (width, 1024)
        (OUT/(key+'.html')).write_text(page.content())
        visual.check(key)

    def check_route(href, index, origin):
        selection = json.loads(unquote(href.split('/recovery/', 1)[1]))
        assert selection == {'learning_id': 'demo-rule-1', 'mode': MODES[index],
                             'target_id': '', 'proposal_ids': ['demo-proposal-1'] if origin == 'review' else []}, selection

    try:
        page.goto(BASE+'#/review')
        page.wait_for_load_state('networkidle')
        health = page.request.get(BASE+'api/health').json()
        assert health['db_path'] == manifest['db'] and health['read_only']
        page.screenshot(path=str(OUT/'reconnaissance.png'))
        (OUT/'reconnaissance.html').write_text(page.content())
        print('RECOVERY_TARGET_RECON', page.get_by_role('heading').all_text_contents(), flush=True)
        for origin in args.origins:
            for width in (1280, 1440):
                page.set_viewport_size({'width': width, 'height': 1024})
                for factor in (1, 2):
                    for theme in ('light', 'dark'):
                        open_origin(theme, origin)
                        actual = zoom.evaluate('''async factor=>{const [tab]=await chrome.tabs.query({active:true,currentWindow:true});
                            await chrome.tabs.setZoom(tab.id,factor);return chrome.tabs.getZoom(tab.id);}''', factor)
                        assert actual == factor
                        page.wait_for_function('n=>Math.abs(devicePixelRatio-n)<.02', arg=factor)
                        key = f'{origin}-{width}-{theme}-zoom-{factor}'
                        state = {'key': key, 'zoom_receipt': actual, 'targets': [], 'viewport': page.evaluate('({width:innerWidth,height:innerHeight,dpr:devicePixelRatio})')}
                        result['states'].append(state)
                        assert abs(state['viewport']['width']*factor-width) <= 2
                        for index, label in enumerate(LABELS):
                            target = page.locator(SELECTORS[origin]).nth(index)
                            keys.reach(target)
                            expect(target).to_be_in_viewport(ratio=1)
                            measured = target.evaluate(MEASURE)
                            state['targets'].append(measured)
                            capture(key+'-'+MODES[index], width)
                            assert measured['text'] == label
                            check_route(measured['href'], index, origin)
                            # A wrapped inline union is not a solid pointer target.
                            assert len(measured['fragments']) == 1, measured
                            assert measured['box']['width'] >= 24 and measured['box']['height'] >= 24, measured
                            assert all(p['matched'] for p in measured['hits']), measured
                            for activation in ('Enter', 'pointer-edge'):
                                if activation == 'Enter':
                                    keys.activate(target)
                                else:
                                    target.scroll_into_view_if_needed()
                                    box = target.bounding_box()
                                    page.mouse.click(box['x']+box['width']/2, box['y']+box['height']-1)
                                expect(page).to_have_url(BASE+measured['href'])
                                page.wait_for_load_state('networkidle')
                                expect(page.locator('#eval-job-title')).to_have_text(label.removesuffix('…'))
                                expect(page.locator('#eval-job-title')).to_be_focused()
                                body = page.locator('#eval-job-body')
                                expect(body).to_contain_text('Choose the destination for this proposal.')
                                expect(body).not_to_contain_text('Reading recovery destinations and evidence')
                                expect(body.locator('[role=alert]')).to_have_count(0)
                                expect(body.locator('a.review-link')).not_to_have_count(0)
                                result['activations'].append({'state': key, 'mode': MODES[index], 'method': activation, 'href': measured['href'], 'options_loaded': True, 'destination_count': body.locator('a.review-link').count()})
                                capture(key+'-'+MODES[index]+'-'+activation, width)
                                open_origin(theme, origin)
                                target = page.locator(SELECTORS[origin]).nth(index)
                        print('RECOVERY_TARGET_STATE', key, 'two solid targets; exact preview routes', flush=True)
        assert not result['errors'], result['errors']
        assert all(method == 'GET' and url.startswith(BASE) for method, url in result['requests'])
        visual.assert_clean()
        result['status'] = 'succeeded'
        print('RECOVERY_TARGETS_OK', len(result['states']), 'states', len(result['activations']), 'exact activations', flush=True)
    except BaseException as error:
        result.update(status='failed', error=str(error))
        page.screenshot(path=str(OUT/'failure.png'))
        (OUT/'failure.html').write_text(page.content())
        raise
    finally:
        unchanged()
        result.update(database='unchanged', targets='unchanged', source='unchanged', model_calls=0)
        (OUT/'result.json').write_text(json.dumps(result, indent=2)+'\n')
        keys.save()
        browser.close()
