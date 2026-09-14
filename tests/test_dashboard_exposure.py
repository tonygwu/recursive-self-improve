"""Dashboard measurements use invented scans, never queue/session approximations."""
from datetime import datetime, timezone

import pytest
pytest.importorskip("fastapi", reason="dashboard extra is optional")
from fastapi.testclient import TestClient

from self_improve.dashboard.app import create_app
from tests.test_scan_observations import make_env, write_claude, scan, correction_session, c_user, ts, PROJECT

NOW = datetime(2026, 8, 20, tzinfo=timezone.utc)


def client_for(env):
    return TestClient(create_app(env.cfg, clock=lambda: NOW))


def test_project_rate_uses_scan_occurrences_and_physical_lines_not_queue_totals(tmp_path):
    env = make_env(tmp_path)
    records = correction_session(env.repo)
    records += [c_user('outside the window', '2026-01-01T00:00:00Z', env.repo)]
    records += [c_user('at the exclusive end', NOW.isoformat(), env.repo)]
    write_claude(env, records)
    scan(env, 'fixture-run')
    env.store.conn.execute('UPDATE sessions SET lines_scanned = 99999999')
    env.store.conn.execute('DELETE FROM scan_incident_links')
    env.store.conn.execute('DELETE FROM incidents')
    env.store.commit()
    before = env.store.conn.total_changes
    with client_for(env) as client:
        payload = client.get('/api/projects?weigh_top_n=0').json()
        row = payload['rows'][0]
        assert row['incidents'] == 0 and row['lines_scanned'] == 99999999
        assert 'incident_rate' not in row
        measure = row['exposure']
        assert measure['occurrences'] == 1 and measure['eligible_lines'] == 4
        assert measure['rate_per_100k'] == 25000
        assert measure['requested']['end'] == '2026-08-20T00:00:00.000000Z'
        detail = client.get('/api/project-exposure', params={'project_key': PROJECT}).json()
        assert detail['options']['working_copies'][0]['normalized_path'] == env.repo
        assert detail['sessions']['logical_sessions'] == 1
        assert detail['session_size']['median'] == 4
        copy = detail['options']['working_copies'][0]['id']
        subset = client.get('/api/project-exposure', params={
            'project_key': PROJECT, 'working_copy_id': copy, 'signal_type': 'frustration',
            'start': '2026-08-01T02:00:00+02:00', 'end': '2026-08-11T00:00:00Z',
        }).json()
        assert subset['occurrences'] == 0 and subset['eligible_lines'] == 4
        assert subset['computable'] is True and subset['rate_per_100k'] == 0
        assert subset['requested']['start'] == '2026-08-01T00:00:00.000000Z'
    assert env.store.conn.total_changes == before
    assert env.store.query_one('SELECT COUNT(*) AS n FROM commands')['n'] == 0
    assert env.store.query_one('SELECT COUNT(*) AS n FROM llm_calls')['n'] == 0
    env.store.close()


@pytest.mark.parametrize('params', [
    {'start': '2026-08-01T00:00:00Z'},
    {'start': '2026-08-01', 'end': '2026-08-02'},
    {'start': '2026-08-02T00:00:00Z', 'end': '2026-08-01T00:00:00Z'},
    {'signal_type': 'invented'}, {'working_copy_id': ''}, {'compatibility_key': ''},
])
def test_bad_selectors_are_named_requests_not_internal_errors(tmp_path, params):
    env = make_env(tmp_path)
    with client_for(env) as client:
        response = client.get('/api/project-exposure', params={'project_key': PROJECT, **params})
    assert response.status_code == 400
    assert response.json()['error'] == 'ExposureRequestError'
    env.store.close()


def test_missing_and_old_schema_do_not_mean_zero_or_migrate(tmp_path):
    env = make_env(tmp_path)
    with client_for(env) as client:
        response = client.get('/api/project-exposure', params={'project_key': PROJECT}).json()
        assert response['reason'] == 'missing_observations'
        assert response['eligible_lines'] is None and response['rate_per_100k'] is None
    env.store.conn.execute("DELETE FROM schema_migrations WHERE name = '0022_scan_observations'")
    env.store.commit()
    with client_for(env) as client:
        response = client.get('/api/project-exposure', params={'project_key': PROJECT}).json()
        assert response['reason'] == 'schema_unavailable'
    assert env.store.query_one("SELECT name FROM schema_migrations WHERE name = '0022_scan_observations'") is None
    env.store.close()


def test_damaged_applied_schema_fails_loud(tmp_path):
    env = make_env(tmp_path)
    env.store.conn.execute('DROP TABLE scan_incident_links')
    env.store.commit()
    with client_for(env) as client:
        response = client.get('/api/project-exposure', params={'project_key': PROJECT})
    assert response.status_code == 409
    assert 'scan_incident_links' in response.json()['detail']
    env.store.close()


def test_partial_coverage_and_empty_interval_remain_visible(tmp_path):
    env = make_env(tmp_path)
    records = correction_session(env.repo)
    missing = c_user('unknown time', ts(9), env.repo)
    del missing['timestamp']
    write_claude(env, records + [missing])
    scan(env, 'fixture-run')
    with client_for(env) as client:
        payload = client.get('/api/project-exposure', params={'project_key': PROJECT}).json()
        assert payload['coverage']['coverage_complete'] is False
        assert payload['coverage']['counts_by_cause']['unknown_time_lines'] == 1
        assert payload['coverage']['retention']['complete_history_known'] is False
        empty = client.get('/api/project-exposure', params={'project_key': PROJECT,
            'start': '2025-01-01T00:00:00Z', 'end': '2025-02-01T00:00:00Z'}).json()
        assert empty['eligible_lines'] == 0 and empty['occurrences'] == 0
        assert empty['rate_per_100k'] is None and empty['reason'] == 'zero_eligible_exposure'
    env.store.close()


def test_version_selection_never_pools_configs_and_unknown_keeps_raw_counts(tmp_path):
    import dataclasses
    from self_improve import filter_incidents
    from self_improve.scan import mark_for_rescan
    env = make_env(tmp_path)
    write_claude(env, correction_session(env.repo))
    scan(env, 'original')
    changed = dataclasses.replace(env.cfg, correction_max_len=1500)
    mark_for_rescan(env.store, changed)
    scan(env, 'changed', cfg=changed)
    mark_for_rescan(env.store, changed)
    scan(env, 'unknown', cfg=changed, detect_fn=lambda events, cfg: filter_incidents.detect(events, cfg))
    with client_for(env) as client:
        base = {'project_key': PROJECT}
        result = client.get('/api/project-exposure', params=base).json()
        assert result['reason'] == 'incompatible_versions' and result['occurrences'] is None
        assert len(result['version_groups']) == 3
        for group in result['version_groups']:
            selected = client.get('/api/project-exposure', params={**base, 'compatibility_key': group['compatibility_key']}).json()
            assert selected['occurrences'] == 1 and selected['eligible_lines'] == 4
            assert selected['computable'] == group['identifiable']
            manifest = group['manifests'][0]
            assert manifest['compatibility_key'] == group['compatibility_key']
            assert manifest['config']['values']['correction_max_len'] in {env.cfg.correction_max_len, 1500}
            if not group['identifiable']:
                assert selected['reason'] == 'unknown_version'
                assert selected['rate_per_100k'] is None
    env.store.close()


def test_exposure_reason_labels_cover_the_scanner_contract():
    import ast
    import inspect
    from self_improve import scan_observations
    from self_improve.dashboard.scan_data import REASONS
    tree = ast.parse(inspect.getsource(scan_observations.exposure_window))
    reasons = {
        node.value.value for node in ast.walk(tree)
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
        and any(isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant)
                and target.slice.value == 'reason' for target in node.targets)
    }
    assert reasons == set(REASONS) - {'', 'schema_unavailable', 'clock_unavailable'}


def test_exposure_renderer_keeps_missing_zero_and_version_states_distinct(tmp_path):
    import shutil
    import subprocess
    from pathlib import Path
    from self_improve.dashboard import app
    js = Path(app.__file__).parent / 'static' / 'app.js'
    module = tmp_path / 'app.mjs'
    module.write_bytes(js.read_bytes())
    runner = tmp_path / 'check.mjs'
    runner.write_text('''import assert from 'node:assert/strict';
import {renderProjectExposure, renderExposureRate, parseRoute} from './app.mjs';
const missing = renderProjectExposure({computable:false,reason_text:'No observations',requested:{}});
assert(missing.includes('No observations'));
assert(!missing.includes('Occurrences / 100k lines'));
const raw = {computable:false,reason_text:'Version unknown',occurrences:0,eligible_lines:4,requested:{},version_groups:[]};
assert(renderProjectExposure(raw).includes('Signal occurrences'));
assert(!renderExposureRate(raw).includes('>0'));
const zero = {...raw,computable:true,rate_per_100k:0,coverage:{coverage_complete:false}};
assert(renderExposureRate(zero).includes('>0.0</span>'));
assert(renderExposureRate(zero).includes('partial'));
assert(renderExposureRate({...zero,rate_per_100k:0.01,occurrences:1}).includes('&lt;0.1'));
assert(renderProjectExposure(null,{error:'Invalid interval'}).includes('Reset to last 30 days'));
assert(renderProjectExposure({...raw,reason_text:'<script>bad</script>'}).includes('&lt;script&gt;'));
const route = parseRoute('#/projects/remote%3Agithub.com%2Fexample%2Falpha?tab=exposure&signal_type=correction');
assert.equal(route.id,'remote:github.com/example/alpha');
assert.equal(route.projectQuery,'tab=exposure&signal_type=correction');
assert.equal(parseRoute('#/projects/path%3Awith%3Fquestion').id,'path:with?question');
console.log('EXPOSURE_RENDERER_OK');
''')
    node = shutil.which('node')
    assert node, 'Node is required to verify the dashboard renderer'
    result = subprocess.run([node, str(runner)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert 'EXPOSURE_RENDERER_OK' in result.stdout
