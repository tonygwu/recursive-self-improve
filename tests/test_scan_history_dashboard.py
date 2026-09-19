"""Recorded scanner provenance travels through the read API, renderer and report."""
import dataclasses
import json
from pathlib import Path

import pytest

from self_improve import report, scan_reporting
from self_improve.scan import ScanStats, ScanError, mark_for_rescan
from self_improve.dashboard import run_data, scan_data
from self_improve.store import Store
from tests.test_scan_observations import make_env, write_claude, scan, correction_session, append, jsonl, c_user, ts


def record_run(env, run_id, stats):
    env.store.insert('runs', {'id': run_id, 'started': '2030-01-01T00:00:00Z',
        'finished': '2030-01-01T00:01:00Z', 'status': 'ok',
        'stats_json': json.dumps({'run_id': run_id, 'scan': stats.as_dict()})})
    env.store.commit()


def test_same_time_runs_have_separate_scan_pages_and_owned_cursors(tmp_path, monkeypatch):
    env = make_env(tmp_path)
    for i in range(3):
        sid = f'11111111-1111-4111-8111-{i:012d}'
        records = correction_session(env.repo)
        for record in records: record['sessionId'] = sid
        write_claude(env, records, name=sid)
    record_run(env, 'first', scan(env, 'first'))
    mark_for_rescan(env.store, env.cfg)
    record_run(env, 'second', scan(env, 'second'))
    # Equal observation times still have a stable ID tie-break.
    env.store.conn.execute("UPDATE scan_observations SET observed_at='2030-01-01T00:00:30.000000Z'")
    env.store.commit()
    queries = []
    query = env.store.query
    def capture(sql, params=()):
        queries.append((sql, params))
        return query(sql, params)
    monkeypatch.setattr(env.store, 'query', capture)
    page = run_data.records(env.store, 'first', kind='scans', limit=2)
    last = run_data.records(env.store, 'first', kind='scans', limit=2, cursor=page['next_cursor'])
    assert page['count'] == last['count'] == 3
    assert last['next_cursor'] is None
    lookups = [(sql, params) for sql, params in queries if 'SELECT o.* FROM scan_observations o WHERE o.run_id' in sql]
    assert len(lookups) == 2
    for sql, params in lookups:
        plan = query('EXPLAIN QUERY PLAN ' + sql, params)
        assert any('scan_observations_run' in row['detail'] for row in plan), plan
    assert len({record['id'] for record in page['records'] + last['records']}) == 3
    assert all(record['run_id'] == 'first' for record in page['records'] + last['records'])
    with pytest.raises(scan_data.ScanHistoryRequestError, match='different selector'):
        run_data.records(env.store, 'second', kind='scans', cursor=page['next_cursor'])
    # The index is optional for readers; opening older history must not upgrade it.
    env.store.conn.execute('DROP INDEX scan_observations_run')
    env.store.conn.execute("DELETE FROM schema_migrations WHERE name='0027_scan_run_index'")
    env.store.commit()
    with env.store.transaction():
        assert run_data.records(env.store, 'first', kind='scans')['count'] == 3
    assert env.store.query_one("SELECT name FROM schema_migrations WHERE name='0027_scan_run_index'") is None
    env.store.close()


def test_incident_pages_keep_producing_and_corroborating_history_after_transcript_deletion(tmp_path):
    env = make_env(tmp_path)
    path = write_claude(env, correction_session(env.repo))
    record_run(env, 'original', scan(env, 'original'))
    incident = env.store.query_one('SELECT id FROM incidents')['id']
    for i in range(22):
        append(path, jsonl([c_user('continue', ts(i + 4), env.repo)]))
        record_run(env, f'rescan-{i}', scan(env, f'rescan-{i}'))
    path.unlink()
    first = scan_data.history_page(env.store, incident_id=incident)
    last = scan_data.history_page(env.store, incident_id=incident, cursor=first['next_cursor'])
    assert first['count'] == 23
    assert len(first['records']) == 20 and len(last['records']) == 3
    assert last['next_cursor'] is None
    producing = [row for row in first['records'] + last['records'] if row['link_kind'] == 'produced']
    assert len(producing) == 1 and producing[0]['run_id'] == 'original'
    assert producing[0]['id'] == first['incident']['produced_by_observation']
    assert all(row['manifest'] and row['occurrence_ids'] for row in first['records'] + last['records'])
    env.store.conn.execute("DELETE FROM scan_incident_links WHERE link_kind='produced'")
    env.store.commit()
    corroborated = scan_data.history_page(env.store, incident_id=incident)
    assert corroborated['incident']['provenance'] == 'legacy_unknown_corroborated'
    env.store.conn.execute('DELETE FROM scan_incident_links'); env.store.commit()
    legacy = scan_data.history_page(env.store, incident_id=incident)
    assert legacy['reason'] == 'legacy_unknown_provenance' and not legacy['computable']
    env.store.close()


def test_history_api_uses_copied_store_and_never_migrates_or_starts_work(tmp_path):
    pytest.importorskip('fastapi')
    from fastapi.testclient import TestClient
    from self_improve.dashboard.app import create_app
    env = make_env(tmp_path)
    write_claude(env, correction_session(env.repo))
    record_run(env, 'original', scan(env, 'original'))
    incident = env.store.query_one('SELECT id FROM incidents')['id']
    copy_path = tmp_path / 'copy.db'
    copied = Store(copy_path); env.store.conn.backup(copied.conn); copied.close()
    env.store.conn.execute('DELETE FROM scan_incident_links'); env.store.commit()
    before = copy_path.read_bytes()
    with TestClient(create_app(env.cfg, db_path=copy_path)) as client:
        response = client.get(f'/api/incidents/{incident}/scan-history')
        assert response.status_code == 200 and response.json()['incident']['provenance'] == 'observed'
        assert client.get('/api/incidents/missing/scan-history').status_code == 404
        assert client.get('/api/runs/original/records?kind=scans').json()['count'] == 1
        assert client.get(f'/api/incidents/{incident}/scan-history?limit=0').status_code == 400
        assert client.get(f'/api/incidents/{incident}/scan-history?cursor=bad').status_code == 400
    assert copy_path.read_bytes() == before
    assert env.store.query_one('SELECT COUNT(*) AS n FROM commands')['n'] == 0
    assert env.store.query_one('SELECT COUNT(*) AS n FROM llm_calls')['n'] == 0
    env.store.close()


def test_old_schema_history_is_unknown_and_never_migrates(tmp_path):
    env = make_env(tmp_path)
    record_run(env, 'old', ScanStats())
    env.store.conn.execute("DELETE FROM schema_migrations WHERE name='0022_scan_observations'")
    env.store.commit()
    old = run_data.records(env.store, 'old', kind='scans')
    assert old['reason_code'] == 'schema_unavailable' and old['count'] is None
    assert env.store.query_one("SELECT name FROM schema_migrations WHERE name='0022_scan_observations'") is None
    env.store.close()


def test_scan_measurement_names_cover_writer_maps_and_reject_new_unlabeled_counters():
    maps = {field.name for field in dataclasses.fields(ScanStats) if field.default_factory is dict}
    assert maps == {'measurement', *scan_reporting.COUNTER_MAPS}
    with pytest.raises(ScanError, match='unrecognized measurement'):
        ScanStats(measurement={'future_counter': 1}).check_invariants()
    for broken in ([], {'lines_observed': -1}, {'lines_observed': True}, {'lines_observed': 1.5}):
        with pytest.raises(scan_reporting.ScanReportingError, match='run owned'):
            scan_reporting.summary({'measurement': broken}, owner='run owned')
    unknown = scan_reporting.summary({}, owner='run old')
    quiet = scan_reporting.summary({'measurement': {}}, owner='run new')
    assert not unknown['recorded'] and unknown['metrics'] == []
    assert quiet['recorded'] and all(metric['count'] == 0 for metric in quiet['metrics'])


def test_native_measurement_and_failure_causes_reach_exact_run_and_markdown_report(tmp_path):
    env = make_env(tmp_path)
    write_claude(env, correction_session(env.repo))
    stats = scan(env, 'observed')
    stats.error_taxonomy = {'measure:FixtureFailure': 2}
    record_run(env, 'observed', stats)
    detail = run_data.detail(env.store, 'observed')
    summary = detail['scan_summary']
    metrics = {metric['key']: metric['count'] for metric in summary['metrics']}
    assert metrics['lines_observed'] == 4 and metrics['occurrences_observed'] == 1
    assert summary['counter_maps'][0]['counts'] == {'measure:FixtureFailure': 2}
    markdown = Path(report.generate(env.store, env.cfg, 'observed', tmp_path / 'report.md')).read_text()
    assert '| Physical line observations published (includes rescans) | 4 |' in markdown
    assert '| measure:FixtureFailure | 2 |' in markdown
    assert 'do not sum run counters as unique project exposure' in markdown
    env.store.update('runs', 'id', 'observed', {'stats_json': json.dumps({'scan': {}})})
    env.store.commit()
    historical = Path(report.generate(env.store, env.cfg, 'observed', tmp_path / 'historical.md')).read_text()
    assert 'No scan measurement counters were retained' in historical
    assert 'No scan failure taxonomy was retained' in historical
    env.store.close()


def test_failed_measurement_writer_reaches_history_and_report_with_native_cause(tmp_path, monkeypatch):
    from self_improve import scan_observations
    env = make_env(tmp_path)
    write_claude(env, correction_session(env.repo))
    class FixtureFailure(Exception): pass
    def fail_publish(*args, **kwargs): raise FixtureFailure('invented publication failure')
    monkeypatch.setattr(scan_observations, 'record_scan', fail_publish)
    stats = scan(env, 'failed-scan')
    assert stats.files_failed == 1 and stats.files_succeeded == 0
    assert stats.error_taxonomy == {'measure:FixtureFailure': 1}
    record_run(env, 'failed-scan', stats)
    history = run_data.records(env.store, 'failed-scan', kind='scans')
    assert len(history['records']) == 1
    assert history['records'][0]['outcome'] == 'failed'
    assert 'FixtureFailure' in history['records'][0]['failure_cause']
    markdown = Path(report.generate(env.store, env.cfg, 'failed-scan', tmp_path/'failed.md')).read_text()
    assert '| measure:FixtureFailure | 1 |' in markdown
    assert '| Failed scan observations recorded | 1 |' in markdown
    assert '| Physical line observations published (includes rescans) | 0 |' in markdown
    env.store.close()


def test_applied_but_damaged_scan_schema_is_a_named_error(tmp_path):
    from self_improve.scan_observations import ScanObservationError
    env = make_env(tmp_path)
    record_run(env, 'broken', ScanStats())
    env.store.conn.execute('DROP TABLE scan_incident_links'); env.store.commit()
    with pytest.raises(ScanObservationError, match='scan_incident_links'):
        run_data.records(env.store, 'broken', kind='scans')
    env.store.close()


def test_damaged_producing_run_link_is_not_presented_as_observed(tmp_path):
    from self_improve.scan_observations import ScanObservationError
    env = make_env(tmp_path)
    write_claude(env, correction_session(env.repo))
    record_run(env, 'original', scan(env, 'original'))
    incident = env.store.query_one('SELECT id FROM incidents')['id']
    env.store.conn.execute("UPDATE scan_observations SET run_id='other-run'"); env.store.commit()
    with pytest.raises(ScanObservationError, match=incident):
        scan_data.history_page(env.store, incident_id=incident)
    env.store.close()


def test_scan_history_final_page_preserves_focus_and_rule_link(tmp_path):
    import shutil
    import subprocess
    from self_improve.dashboard import app
    module = tmp_path / 'app.mjs'
    module.write_bytes((Path(app.__file__).parent/'static'/'app.js').read_bytes())
    from tests.spa_assets import copy_spa_dependencies
    copy_spa_dependencies(tmp_path)
    runner = tmp_path / 'history.mjs'
    runner.write_text('''import assert from 'node:assert/strict';
import * as ui from './app.mjs';
const rootId='scan-history-rule-incident';
let html='<button id="'+rootId+'-older"></button>';
const root={id:rootId,querySelectorAll:()=>[],getAttribute:()=> 'incident'};
const element=id=>({id,disabled:false,closest:()=>root,focus(){document.activeElement=this}});
globalThis.document={activeElement:element(rootId+'-older'),querySelectorAll:()=>[root],
  getElementById:id=>id===rootId ? root : html.includes('id="'+id+'"') ? element(id) : null};
Object.defineProperty(root,'innerHTML',{get:()=>html,set:value=>{html=value;document.activeElement={id:'',closest:()=>null}}});
ui.state.scanHistories.incident={loaded:true,records:[],next_cursor:'older'};
globalThis.fetch=async()=>({ok:true,json:async()=>({selector:{incident_id:'incident'},records:[],count:0,next_cursor:null,computable:true,reason_text:''})});
await ui.loadIncidentScanHistory('incident',{older:true});
assert.equal(document.activeElement.id,rootId+'-load');
assert(!html.includes('Load older observations'));
const route=ui.parseRoute('#/rules/lesson%3Fid?tab=evidence&scan=incident');
assert.equal(route.id,'lesson?id');
assert.equal(route.ruleQuery,'tab=evidence&scan=incident');
const legacy=ui.renderScanHistory('legacy','rule');
assert(legacy.includes('Load detector observations'));
ui.state.scanHistories.legacy={loaded:true,records:[],incident:{provenance:'legacy_unknown_corroborated'}};
assert(ui.renderScanHistory('legacy','rule').includes('do not establish its original provenance'));
console.log('SCAN_HISTORY_FOCUS_OK');
''')
    node = shutil.which('node')
    assert node, 'Node is required for the actual dashboard interaction test'
    result = subprocess.run([node,str(runner)],capture_output=True,text=True,timeout=30)
    assert result.returncode == 0, result.stderr
    assert 'SCAN_HISTORY_FOCUS_OK' in result.stdout
