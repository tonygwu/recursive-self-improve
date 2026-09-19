"""Run complete evidence-browser acceptance with delayed native-session reads."""
from pathlib import Path
import hashlib
import json
import runpy
import sqlite3

from playwright.sync_api import Browser
from keyboard_actions import KeyboardActions

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT/'reports/dashboard-parity/evidence-readiness'
OUT.mkdir(parents=True, exist_ok=True)
manifest = json.loads((ROOT/'reports/dashboard-parity/evidence-browser-manifest.json').read_text())
assert Path(manifest['db']).parent.parent.name.startswith('si-evidence-ui-')
identities = {r['logical_session_key'] for r in manifest['native_reports'].values()}
paths = ['/api/evidence/session/'+identity for identity in sorted(identities)]
result = {'status': 'attempted', 'native_checks': [], 'requests': [], 'delay_ms': 1500,
          'source_hashes': {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                           for p in [Path(__file__).resolve(), ROOT/'tests/browser/verify_evidence_browser.py',
                                     ROOT/'tests/browser/keyboard_actions.py', ROOT/'src/self_improve/dashboard/static/app.js']}}
new_page = Browser.new_page
activate = KeyboardActions.activate


def delayed_page(browser, *args, **kwargs):
    page = new_page(browser, *args, **kwargs)
    # Simulated response latency tests the readiness precondition. This is not
    # a sleep or retry in the verifier's interaction path; real responses survive.
    page.add_init_script('''(() => {
      const paths = new Set(PATHS), original = window.fetch;
      window.evidenceReadinessReceipts = [];
      window.fetch = async (...args) => {
        const response = await original(...args);
        const path = new URL(args[0] instanceof Request ? args[0].url : String(args[0]), location.href).pathname;
        if (paths.has(path)) {
          const receipt = {path, received_at: performance.now(), released_at: null};
          window.evidenceReadinessReceipts.push(receipt);
          await new Promise(resolve => setTimeout(resolve, 1500));
          receipt.released_at = performance.now();
        }
        return response;
      };
    })();'''.replace('PATHS', json.dumps(paths)))
    page.on('request', lambda request: result['requests'].append([request.method, request.url]))
    return page


def checked_activate(keys, target, key='Enter'):
    if target.text_content() == 'Source':
        observation = keys.page.evaluate('''async () => {
          const {state} = await import('/app.js');
          return {selected: state.selectedEvidence, loading: state.evidenceDetail?.loading,
                  reader_key: state.evidenceDetail?.key, has_data: !!state.evidenceDetail?.data,
                  receipts: window.evidenceReadinessReceipts, at: performance.now()};
        }''')
        selected = observation['selected']
        if selected and selected['kind'] == 'session' and selected['id'] in identities:
            result['native_checks'].append(observation)
            assert observation['loading'] is False and observation['has_data'], 'Source traversal preceded completed native-session read: '+json.dumps(observation)
            assert observation['reader_key'] == 'session:'+selected['id']
            receipts = [r for r in observation['receipts'] if r['path'] == '/api/evidence/session/'+selected['id']]
            assert receipts and receipts[-1]['released_at'] is not None
            assert receipts[-1]['released_at'] <= observation['at']
            assert receipts[-1]['released_at']-receipts[-1]['received_at'] >= result['delay_ms']-1
    return activate(keys, target, key)


try:
    Browser.new_page = delayed_page
    KeyboardActions.activate = checked_activate
    runpy.run_path(str(ROOT/'tests/browser/verify_evidence_browser.py'), run_name='__main__')
    assert {item['selected']['id'] for item in result['native_checks']} == identities
    assert all(method == 'GET' for method, _ in result['requests'])
    result['status'] = 'succeeded'
    print('EVIDENCE_READINESS_OK', len(result['native_checks']), 'completed native reads; full evidence journeys preserved')
except BaseException as error:
    result.update(status='failed', error=str(error))
    raise
finally:
    Browser.new_page = new_page
    KeyboardActions.activate = activate
    with sqlite3.connect('file:'+manifest['db']+'?mode=ro', uri=True) as db:
        assert list(db.iterdump()) == manifest['snapshot']
        assert db.execute('SELECT COUNT(*) FROM llm_calls').fetchone()[0] == 0
    assert all(Path(path).read_text() == text for path, text in manifest['targets'].items())
    for path, digest in result['source_hashes'].items():
        assert hashlib.sha256((ROOT/path).read_bytes()).hexdigest() == digest
    result.update(store='unchanged', targets='unchanged', source='unchanged', model_calls=0)
    (OUT/'result.json').write_text(json.dumps(result, indent=2)+'\n')
