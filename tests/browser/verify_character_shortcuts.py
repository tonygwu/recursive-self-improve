"""Native shortcut opt-out, persistence and explicit decisions in invented state."""
from pathlib import Path
from urllib.parse import quote
import hashlib
import json
import sqlite3
import tempfile

from playwright.sync_api import sync_playwright, expect
from keyboard_actions import KeyboardActions
from visual_checks import VisualChecks

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'reports/dashboard-parity/character-shortcuts'
OUT.mkdir(parents=True, exist_ok=True)
info = json.loads((OUT.parent / 'review-layout/manifest.json').read_text())
assert 'si-review-layout-' in info['db']
BASE = 'http://127.0.0.1:8876/'
full = BASE + '#/review/family/' + quote(info['families']['mixed'], safe='')
result = {'status': 'attempted', 'states': [], 'requests': [], 'errors': [], 'source_hashes': {
    str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
    for path in [ROOT / 'src/self_improve/dashboard/static/app.js',
                 ROOT / 'src/self_improve/dashboard/static/index.html', Path(__file__).resolve()]}}


def posts():
    return [record for record in result['requests'] if record['method'] == 'POST']


def dump():
    with sqlite3.connect('file:' + info['db'] + '?mode=ro', uri=True) as db:
        return list(db.iterdump())


with tempfile.TemporaryDirectory(prefix='si-character-shortcut-browser-') as temporary, sync_playwright() as runtime:
    folder = Path(temporary)
    extension = folder / 'extension'
    extension.mkdir()
    (extension / 'manifest.json').write_text(json.dumps({
        'manifest_version': 3, 'name': 'Temporary shortcut zoom', 'version': '1.0',
        'host_permissions': ['http://127.0.0.1/*'], 'background': {'service_worker': 'worker.js'}}))
    (extension / 'worker.js').write_text('chrome.runtime.onInstalled.addListener(() => {});')
    browser = runtime.chromium.launch_persistent_context(
        str(folder / 'profile'), channel='chromium', headless=True,
        args=[f'--disable-extensions-except={extension}', f'--load-extension={extension}'],
        viewport={'width': 1280, 'height': 1024})
    page = browser.pages[0]
    zoom = browser.service_workers[0] if browser.service_workers else browser.wait_for_event('serviceworker')

    def observe(current):
        current.on('pageerror', lambda e: result['errors'].append(str(e)))
        current.on('request', lambda r: result['requests'].append({
            'method': r.method, 'url': r.url, 'body': r.post_data_json if r.method == 'POST' else None}))

    observe(page)
    keys = KeyboardActions(page, OUT / 'keyboard.json')
    visual = VisualChecks(page, OUT / 'contrast.json')
    toggle = page.locator('#character-shortcuts-toggle')
    approve = page.locator('[data-decision="approve"]')

    def hints(enabled):
        expect(toggle).to_have_text('Character shortcuts: ' + ('on' if enabled else 'off'))
        expect(toggle).to_have_attribute('aria-pressed', str(enabled).lower())
        assert page.locator('[data-character-key]').evaluate_all(
            '(els,on)=>els.every(e=>on ? e.getAttribute("aria-keyshortcuts")===e.dataset.characterKey : !e.hasAttribute("aria-keyshortcuts"))', enabled)
        assert page.locator('[data-character-hint]').evaluate_all(
            '(els,on)=>els.every(e=>on ? !e.hidden && e.style.display!=="none" : e.hidden && e.style.display==="none")', enabled)

    def capture(name):
        keys.reach(toggle)
        geometry = toggle.evaluate('''el=>{
            const r=el.getBoundingClientRect(),f=el.closest('.nav__footer').getBoundingClientRect();
            const text=document.createRange();text.selectNodeContents(el);
            return {width:r.width,height:r.height,scrollWidth:el.scrollWidth,clientWidth:el.clientWidth,
                outside:[...text.getClientRects()].some(t=>t.left<r.left-.5||t.right>r.right+.5),
                insideFooter:r.left>=f.left-.5&&r.right<=f.right+.5,
                viewport:{top:r.top,bottom:r.bottom,height:innerHeight}};}''')
        assert geometry['width'] > 0 and geometry['height'] >= 24
        assert not geometry['outside'] and geometry['insideFooter'], geometry
        assert geometry['scrollWidth'] <= geometry['clientWidth'] + 1, geometry
        assert geometry['viewport']['top'] >= 0 and geometry['viewport']['bottom'] <= geometry['viewport']['height'], geometry
        visual.check(name)
        page.screenshot(path=str(OUT / (name + '.png')))
        result['states'].append({'name': name, 'geometry': geometry})

    def no_action(focus):
        keys.reach(page.locator(focus))
        before = page.evaluate('''async()=>{const a=await import('/app.js');return {url:location.href,selected:a.state.selectedFamily};}''')
        page.evaluate('''()=>{window.shortcutEvents=[];window.shortcutProbe=e=>window.shortcutEvents.push({key:e.key,prevented:e.defaultPrevented});document.addEventListener('keydown',window.shortcutProbe);}''')
        before_posts = len(posts())
        for key in ('j', 'k', 'o', 'a', 'r', 'Shift+R'):
            page.keyboard.press(key)
        events = page.evaluate('''()=>{document.removeEventListener('keydown',window.shortcutProbe);return window.shortcutEvents;}''')
        after = page.evaluate('''async()=>{const a=await import('/app.js');return {url:location.href,selected:a.state.selectedFamily};}''')
        assert before == after and len(posts()) == before_posts
        assert all(not event['prevented'] for event in events), events
        result.setdefault('off_key_checks', []).append({'focus': focus, 'events': events})

    try:
        page.goto(full)
        page.wait_for_load_state('networkidle')
        expect(approve).to_be_enabled()
        page.screenshot(path=str(OUT / 'reconnaissance.png'))
        (OUT / 'reconnaissance.html').write_text(page.content())
        assert page.request.get(BASE + 'api/health').json()['db_path'] == info['db']
        hints(True)
        # The native Space action changes only the preference and current hints.
        preview = page.evaluate('async id=>(await import("/app.js")).state.reviewPreviews[id].data.revision', info['families']['mixed'])
        keys.activate(toggle, 'Space')
        hints(False)
        expect(toggle).to_be_focused()
        assert page.evaluate('async id=>(await import("/app.js")).state.reviewPreviews[id].data.revision', info['families']['mixed']) == preview
        for focus in ('#character-shortcuts-toggle', '#theme-toggle', '#nav-projects', '[data-decision="approve"]'):
            no_action(focus)
        page.keyboard.press('Escape')
        expect(page).to_have_url(BASE + '#/review')
        expect(page.locator('.review-detail')).to_have_count(0)
        hints(False)
        page.reload()
        page.wait_for_load_state('networkidle')
        hints(False)
        keys.activate(page.locator('#nav-rules'))
        expect(page.locator('#rules-search-input')).to_be_visible()
        page.keyboard.press('/')
        expect(page.locator('#nav-rules')).to_be_focused()
        hints(False)
        # Modified command palette and its native text field remain available.
        page.keyboard.press('Control+k')
        expect(page.locator('#navigation-input')).to_be_focused()
        page.keyboard.type('ar')
        expect(page.locator('#navigation-input')).to_have_value('ar')
        page.keyboard.press('Escape')
        keys.activate(toggle, 'Space')
        hints(True)
        page.keyboard.press('/')
        expect(page.locator('#rules-search-input')).to_be_focused()
        keys.activate(toggle, 'Space')
        page.goto(full)
        page.wait_for_load_state('networkidle')
        hints(False)
        for width in (1280, 1440, 640):
            page.set_viewport_size({'width': width, 'height': 1024})
            for theme in ('light', 'dark'):
                keys.theme(theme)
                capture(f'off-{theme}-{width}')
                keys.activate(toggle, 'Space')
                hints(True)
                capture(f'on-{theme}-{width}')
                keys.activate(toggle, 'Space')
        for width in (1280, 1440):
            page.set_viewport_size({'width': width, 'height': 1024})
            receipt = zoom.evaluate('''async()=>{const [tab]=await chrome.tabs.query({active:true,currentWindow:true});await chrome.tabs.setZoom(tab.id,2);return chrome.tabs.getZoom(tab.id);}''')
            assert receipt == 2
            for theme in ('light', 'dark'):
                keys.theme(theme)
                capture(f'off-{theme}-{width}-zoom-200')
            zoom.evaluate('''async()=>{const [tab]=await chrome.tabs.query({active:true,currentWindow:true});await chrome.tabs.setZoom(tab.id,1);}''')
        # A denied read is off before listeners attach; a denied save is honest.
        denied = browser.new_page()
        observe(denied)
        denied.add_init_script('''const get=Storage.prototype.getItem,set=Storage.prototype.setItem;
            Storage.prototype.getItem=function(k){if(k==='self-improve-character-shortcuts')throw Error('Invented denied read');return get.call(this,k);};
            Storage.prototype.setItem=function(k,v){if(k==='self-improve-character-shortcuts')throw Error('Invented denied save');return set.call(this,k,v);};''')
        denied.goto(full)
        denied.wait_for_load_state('networkidle')
        denied_keys = KeyboardActions(denied, OUT / 'denied-keyboard.json')
        denied_toggle = denied.locator('#character-shortcuts-toggle')
        expect(denied_toggle).to_have_attribute('aria-pressed', 'false')
        expect(denied.locator('#character-shortcuts-notice')).to_contain_text('Could not read saved shortcuts')
        denied_keys.reach(denied.locator('#theme-toggle'))
        for key in ('a', 'r', 'Shift+R'):
            denied.keyboard.press(key)
        denied_keys.activate(denied_toggle, 'Space')
        expect(denied_toggle).to_have_attribute('aria-pressed', 'true')
        expect(denied.locator('#character-shortcuts-notice')).to_contain_text('this page only; it could not be saved')
        denied_keys.activate(denied_toggle, 'Space')
        expect(denied_toggle).to_have_attribute('aria-pressed', 'false')
        denied_visual = VisualChecks(denied, OUT / 'denied-contrast.json')
        denied_visual.check('storage-unavailable')
        denied_visual.assert_clean()
        denied.screenshot(path=str(OUT / 'storage-unavailable.png'))
        denied.reload()
        denied.wait_for_load_state('networkidle')
        expect(denied_toggle).to_have_attribute('aria-pressed', 'false')
        denied.close()
        assert not posts() and dump() == info['snapshot']
        # Explicit native re-enable restores the accepted reviewed action.
        page.set_viewport_size({'width': 1440, 'height': 1024})
        keys.activate(toggle, 'Space')
        hints(True)
        expect(approve).to_be_enabled()
        keys.reach(approve)
        with page.expect_response(lambda r: r.url.endswith('/api/commands') and r.request.method == 'POST') as response:
            page.keyboard.press('a')
        assert response.value.status == 202
        expect(page.get_by_role('heading', name='This family is no longer in Review')).to_be_visible()
        keys.activate(page.get_by_role('link', name='Return to queue', exact=True))
        expect(page.locator('[data-decision="reject"]')).to_be_enabled()
        assert len(posts()) == 1 and posts()[0]['body']['action'] == 'approve'
        assert posts()[0]['body']['preview_revision'] == preview
        # The remaining conflicting family still requires its current read.
        expect(page.locator('.review-card--selected')).to_have_attribute('data-learning-id', info['families']['conflict'])
        keys.reach(page.locator('[data-decision="reject"]'))
        with page.expect_response(lambda r: r.url.endswith('/api/commands') and r.request.method == 'POST') as response:
            page.keyboard.press('r')
        assert response.value.status == 202
        expect(page.locator('#nav-inbox-count')).to_have_text('0')
        assert [r['body']['action'] for r in posts()] == ['approve', 'reject_target']
        with sqlite3.connect('file:' + info['db'] + '?mode=ro', uri=True) as db:
            assert db.execute('SELECT COUNT(*) FROM commands').fetchone()[0] == 2
            assert db.execute('SELECT COUNT(*) FROM llm_calls').fetchone()[0] == 0
        assert all(Path(path).read_text() == before for path, before in info['targets'].items())
        assert not result['errors'], result['errors']
        visual.assert_clean()
        result.update(status='succeeded', external_models=0, unchanged_targets=True, unintended_posts=0)
    except BaseException as error:
        result.update(status='failed', error=str(error))
        page.screenshot(path=str(OUT / 'failure.png'))
        (OUT / 'failure.html').write_text(page.content())
        raise
    finally:
        (OUT / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
        browser.close()

print('CHARACTER_SHORTCUTS_OK', len(result['states']), 'states, two explicit commands, zero unintended commands/models')
