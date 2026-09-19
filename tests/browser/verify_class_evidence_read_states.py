"""Cause-first Evals read recovery and full diagnostic inspection in temporary state."""
from pathlib import Path
import hashlib
import json
import sqlite3
import tempfile
from playwright.sync_api import sync_playwright, expect
from keyboard_actions import KeyboardActions
from visual_checks import VisualChecks

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'reports/dashboard-parity/evals-read-states'
OUT.mkdir(parents=True, exist_ok=True)
info = json.loads((OUT.parent / 'evals-density/manifest.json').read_text())
assert 'si-evals-density-' in info['db']
BASE = 'http://127.0.0.1:8876/'
cause = 'Invented evidence source is temporarily unavailable. '
detail = cause + '<script>window.fixtureExecuted=true</script> ' + 'complete diagnostic line\n' * 120 + 'FINAL DIAGNOSTIC LINE'
result = {'status': 'attempted', 'requests': [], 'errors': [], 'states': [], 'source_hashes': {
    str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
    for path in [ROOT / 'src/self_improve/dashboard/static/app.js', Path(__file__).resolve()]}}

with tempfile.TemporaryDirectory(prefix='si-evals-read-browser-') as temporary, sync_playwright() as runtime:
    folder = Path(temporary)
    extension = folder / 'extension'
    extension.mkdir()
    (extension / 'manifest.json').write_text(json.dumps({'manifest_version': 3, 'name': 'Temporary Evals zoom',
        'version': '1.0', 'host_permissions': ['http://127.0.0.1/*'], 'background': {'service_worker': 'worker.js'}}))
    (extension / 'worker.js').write_text('chrome.runtime.onInstalled.addListener(() => {});')
    browser = runtime.chromium.launch_persistent_context(str(folder / 'profile'), channel='chromium', headless=True,
        args=[f'--disable-extensions-except={extension}', f'--load-extension={extension}'], viewport={'width': 1280, 'height': 1024})
    page = browser.pages[0]
    zoom = browser.service_workers[0] if browser.service_workers else browser.wait_for_event('serviceworker')
    keys = KeyboardActions(page, OUT / 'keyboard.json')
    visual = VisualChecks(page, OUT / 'contrast.json')
    page.on('pageerror', lambda e: result['errors'].append(str(e)))
    page.on('request', lambda r: result['requests'].append([r.method, r.url]))
    panel = page.locator('#class-evidence')
    alert = panel.get_by_role('alert')
    summary = panel.get_by_text('Request details', exact=True)
    record = panel.locator('details[data-review-key="read-error:class-evidence"] pre')
    retry = page.locator('#policy-refresh')
    pattern = '**/api/class-evidence'

    def fail(route):
        route.fulfill(status=503, json={'detail': detail})

    def capture(label):
        summary.scroll_into_view_if_needed()
        page.screenshot(path=str(OUT / (label + '.png')))
        assert panel.evaluate('el=>el.scrollWidth<=el.clientWidth+1')
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        assert 'GET /api' not in alert.inner_text()
        expect(alert).to_contain_text(cause.strip())
        expect(alert).to_contain_text('Retry class evidence')
        expect(record).to_have_text('GET /api/class-evidence answered 503  ' + detail)
        assert page.evaluate('window.fixtureExecuted') is None
        visual.check(label)
        result['states'].append(label)

    try:
        page.route(pattern, fail)
        page.goto(BASE + '#/evals')
        page.wait_for_load_state('networkidle')
        page.screenshot(path=str(OUT / 'reconnaissance.png'))
        (OUT / 'reconnaissance.html').write_text(page.content())
        expect(alert).to_contain_text('Could not read class evidence')
        expect(page.locator('.policy-table')).to_have_count(0)
        expect(page.locator('#trend-gate')).to_contain_text('3 recorded evaluation attempts')
        keys.activate(summary)
        expect(record).to_be_visible()
        page.evaluate('async()=>{(await import("/app.js")).paintClassEvidence();}')
        expect(summary).to_be_focused()
        expect(record).to_be_visible()
        for width in (1280, 1440):
            page.set_viewport_size({'width': width, 'height': 1024})
            for theme in ('light', 'dark'):
                keys.theme(theme)
                keys.reach(summary)
                capture(f'initial-{theme}-{width}')
        # Chromium exposes scrollable pre elements to native Tab navigation.
        keys.reach(record)
        page.keyboard.press('End')
        page.wait_for_function('''()=>{const e=document.querySelector('#class-evidence details[data-review-key="read-error:class-evidence"] pre');return e.scrollTop+e.clientHeight>=e.scrollHeight-1;}''')
        result['diagnostic_scroll'] = record.evaluate('el=>({top:el.scrollTop,height:el.clientHeight,total:el.scrollHeight})')
        assert result['diagnostic_scroll']['top'] > 0
        page.screenshot(path=str(OUT / 'diagnostic-end.png'))
        page.unroute(pattern, fail)
        keys.activate(retry)
        expect(page.locator('#policy-row-global')).to_be_visible()
        expect(retry).to_be_focused()
        expect(alert).to_have_count(0)
        expect(summary).to_have_count(0)
        keys.activate(page.locator('#policy-evidence-global'))
        class_record = page.locator('#policy-details-global details')
        keys.activate(class_record.locator('summary'))
        before = page.locator('.policy-table').inner_text()
        policy = page.get_by_role('switch').evaluate_all('els=>els.map(e=>e.getAttribute("aria-checked"))')
        page.route(pattern, fail)
        keys.activate(retry)
        expect(alert).to_contain_text('showing the previous class evidence')
        expect(retry).to_be_focused()
        expect(class_record).to_have_attribute('open', '')
        assert page.locator('.policy-table').inner_text() == before
        # The earlier open diagnostic remains open when another read fails.
        expect(record).to_be_visible()
        keys.reach(summary)
        page.evaluate('async()=>{(await import("/app.js")).paintClassEvidence();}')
        expect(summary).to_be_focused()
        expect(record).to_be_visible()
        for width in (1280, 1440):
            page.set_viewport_size({'width': width, 'height': 1024})
            for theme in ('light', 'dark'):
                keys.theme(theme)
                capture(f'refresh-{theme}-{width}')
            receipt = zoom.evaluate('''async()=>{const [t]=await chrome.tabs.query({active:true,currentWindow:true});await chrome.tabs.setZoom(t.id,2);return chrome.tabs.getZoom(t.id);}''')
            assert receipt == 2
            for theme in ('light', 'dark'):
                keys.theme(theme)
                keys.reach(summary)
                capture(f'refresh-{theme}-{width}-zoom-200')
            zoom.evaluate('''async()=>{const [t]=await chrome.tabs.query({active:true,currentWindow:true});await chrome.tabs.setZoom(t.id,1);}''')
        page.unroute(pattern, fail)
        pending = []
        page.route(pattern, lambda route: pending.append(route))
        keys.activate(retry)
        expect(retry).to_be_disabled()
        expect(panel).to_contain_text('Reading class evidence')
        expect(page.locator('#policy-switch-global')).to_be_disabled()
        assert page.locator('.policy-table').inner_text() == before
        assert len(pending) == 1
        pending.pop().continue_()
        expect(retry).to_be_enabled()
        expect(retry).to_be_focused()
        expect(alert).to_have_count(0)
        expect(class_record).to_have_attribute('open', '')
        assert page.get_by_role('switch').evaluate_all('els=>els.map(e=>e.getAttribute("aria-checked"))') == policy
        assert all(method == 'GET' for method, _ in result['requests'])
        assert all(Path(path).read_text() == before for path, before in info['targets'].items())
        with sqlite3.connect('file:' + info['db'] + '?mode=ro', uri=True) as db:
            assert list(db.iterdump()) == info['sql']
            assert db.execute('SELECT COUNT(*) FROM llm_calls').fetchone()[0] == 0
        assert not result['errors'], result['errors']
        visual.assert_clean()
        result.update(status='succeeded', database='unchanged', targets='unchanged', external_models=0)
    except BaseException as error:
        result.update(status='failed', error=str(error))
        page.screenshot(path=str(OUT / 'failure.png'))
        (OUT / 'failure.html').write_text(page.content())
        raise
    finally:
        (OUT / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
        browser.close()

print('EVALS_READ_STATES_OK', len(result['states']), 'states; GET only; unchanged database and targets; zero models')
