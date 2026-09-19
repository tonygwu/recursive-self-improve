"""Failure causes remain faithful from retained records to human-facing output."""
from contextlib import closing
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from self_improve.dashboard import queries as q, run_data
from self_improve.store import Store


FIXED = 'IntegrityError:unique:incident_learnings'


def test_shared_copy_has_no_dashboard_store_or_provider_imports():
    probe = '''
import importlib.abc, sys
class Refuse(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(('self_improve.dashboard', 'self_improve.store', 'self_improve.llm', 'fastapi')):
            raise AssertionError(fullname)
sys.meta_path.insert(0, Refuse())
from self_improve.failure_presentation import failure_copy, taxonomy_rows
assert failure_copy('call_failed:spawn_error')['name'] == 'The call never produced an answer'
assert taxonomy_rows({}, owner='isolated') == []
try:
    import self_improve.store
except AssertionError:
    pass
else:
    raise AssertionError('import guard did not fire')
'''
    result = subprocess.run([sys.executable, '-c', probe], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_missing_empty_zero_and_unknown_taxonomy_remain_distinct(tmp_path):
    from self_improve.failure_presentation import taxonomy_rows
    with closing(Store(tmp_path/'state.db')) as store:
        put_run(store, 'empty', '2030-01-01T00:00:00Z', {})
        put_run(store, 'zero', '2030-01-01T00:00:00Z', {'timeout':0})
        put_run(store, 'missing', '2030-01-01T00:00:00Z', {})
        store.update('runs', 'id', 'missing', {'stats_json':json.dumps({'mine':{}})})
        store.commit()
        stages = {name:next(s for s in run_data.detail(store, name)['stages'] if s['name']=='mine')
                  for name in ('missing', 'empty', 'zero')}
        assert stages['missing']['causes'] is None
        assert stages['empty']['causes'] == stages['zero']['causes'] == []
        assert stages['zero']['payload']['taxonomy'] == {'timeout':0}
    unknown = taxonomy_rows({'NovelCause:invented':3}, owner='invented')[0]
    assert unknown['kind'] == 'unknown' and unknown['class'] == 'NovelCause:invented'
    assert unknown['name'] == 'Unclassified outcome' and unknown['count'] == 3


def put_run(store, run_id, started, taxonomy):
    store.insert('runs', {'id':run_id, 'started':started, 'status':'degraded',
        'stats_json':json.dumps({'run_id':run_id, 'mine':{'attempted':sum(taxonomy.values()),
            'succeeded':0, 'failed':sum(taxonomy.values()), 'taxonomy':taxonomy}})})
    store.commit()


def test_generic_constraint_never_inherits_a_specific_fix(tmp_path):
    with closing(Store(tmp_path/'state.db')) as store:
        put_run(store, 'before', '2026-08-15T01:00:00Z', {'IntegrityError':1, FIXED:1})
        put_run(store, 'later', '2026-08-20T01:00:00Z', {'IntegrityError':1})
        panel = q.failure_panel(store)
        rows = {r['class']:r for key in ('open', 'quiet', 'fixed', 'regressed') for r in panel[key]}
        assert rows['IntegrityError']['status'] == 'open'
        assert 'fix_commit' not in rows['IntegrityError']
        assert rows[FIXED]['status'] == 'fixed'
        assert rows[FIXED]['fix_commit'].startswith('cfd6410')


def test_selected_run_has_readable_causes_and_never_borrows_same_time_neighbor(tmp_path):
    with closing(Store(tmp_path/'state.db')) as store:
        stamp = '2030-01-01T00:00:00Z'
        put_run(store, 'selected', stamp, {'call_failed:spawn_error':2, 'budget_exhausted':4})
        put_run(store, 'neighbor', stamp, {'MineContractViolation':99})
        store.insert('llm_calls', {'id':'spawn', 'run_id':'selected', 'stage':'mine',
            'outcome':'spawn_error', 'created_at':stamp})
        store.commit()
        snapshot = list(store.conn.iterdump())
        stages = run_data.detail(store, 'selected')['stages']
        cause = next(s for s in stages if s['name']=='mine')['causes']
        assert {r['class']:r['count'] for r in cause} == {'call_failed:spawn_error':2, 'budget_exhausted':4}
        failure = next(r for r in cause if r['kind']=='failure')
        assert failure['name'] == 'The call never produced an answer'
        assert 'failed to launch' in failure['explanation']
        assert next(r for r in cause if r['class']=='budget_exhausted')['kind'] == 'not_failure'
        calls = run_data.records(store, 'selected', kind='calls')['records']
        assert calls[0]['outcome_description']['name'] == 'The CLI would not start'
        assert list(store.conn.iterdump()) == snapshot


@pytest.mark.parametrize('taxonomy', [[], 'bad', {'timeout':True}, {'timeout':-1}, {'timeout':1.5}])
def test_corrupt_taxonomy_is_not_silently_missing_or_coerced(tmp_path, taxonomy):
    with closing(Store(tmp_path/'state.db')) as store:
        put_run(store, 'bad', '2030-01-01T00:00:00Z', {})
        store.update('runs', 'id', 'bad', {'stats_json':json.dumps({'mine':{
            'attempted':1, 'succeeded':0, 'failed':1, 'taxonomy':taxonomy}})})
        store.commit()
        with pytest.raises(q.DashboardDataError, match='bad.*mine.*taxonomy'):
            q.failure_panel(store)
        with pytest.raises(run_data.RunDataError, match='bad.*mine.*taxonomy'):
            run_data.detail(store, 'bad')


def test_report_explains_all_prd_classes_and_keeps_recorded_suffixes(tmp_path):
    from tests.test_report import _run_with_stats
    taxonomy = {'MineParseFailure':1, 'call_failed:spawn_error':2, 'empty_output':3,
                'MineContractViolation':4, 'IntegrityError:not_null:learnings.rule_text':5}
    text = _run_with_stats(tmp_path, {'mine':{'attempted':15, 'succeeded':0, 'failed':15, 'taxonomy':taxonomy}})
    for key, title in [
        ('MineParseFailure', 'No answer we could read'),
        ('call_failed:spawn_error', 'The call never produced an answer'),
        ('empty_output', 'The call produced no output'),
        ('MineContractViolation', 'Answer was missing required fields'),
        ('IntegrityError:not_null:learnings.rule_text', 'A database constraint rejected a write'),
    ]:
        assert key in text and title in text
    assert 'failed to launch' in text


def test_actual_renderer_exposes_fix_evidence_and_excludes_old_recurrence_from_current_health(tmp_path):
    from self_improve.dashboard import app
    from tests.spa_assets import copy_spa_dependencies
    (tmp_path/'app.mjs').write_bytes((Path(app.__file__).parent/'static/app.js').read_bytes())
    copy_spa_dependencies(tmp_path)
    probe=tmp_path/'probe.mjs'
    probe.write_text(r'''
import assert from 'node:assert/strict';
const m=await import('./app.mjs');
const row={class:'IntegrityError:unique:incident_learnings',name:'A database constraint rejected a write',explanation:'Constraint rejected.',has_copy:true,stages:['mine'],total:1,recent:0,recent_window_days:7,status:'regressed',fixed_at:'2026-08-16T10:56:04Z',fix_commit:'cfd6410b5469b2e2c718c16c1c042e2f1c3f123a',fix:'Incident-learning links are idempotent.'};
const data={regressed:[row],open:[],quiet:[],fixed:[],not_failures:[],latest_run_day:'2030-01-01',note:'Counts overlap.'};
const full=m.renderFailures(data);
assert.match(full,/Incident-learning links are idempotent/);
assert.match(full,/cfd6410b5469b2e2c718c16c1c042e2f1c3f123a/);
assert.match(full,/deployment/);
assert.doesNotMatch(full,/regressed after the fix/);
const overview=m.renderOverviewFailures(data).split('<details')[0];
assert.doesNotMatch(overview,/overview-failure/);
assert.match(overview,/No recent failure class/);
console.log('FAILURE_PRESENTATION_OK');
''')
    result=subprocess.run([shutil.which('node'),str(probe)],capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stderr
    assert result.stdout.strip()=='FAILURE_PRESENTATION_OK'


def test_caps_remain_visible_when_the_failure_history_is_empty(tmp_path):
    from self_improve.dashboard import app
    from tests.spa_assets import copy_spa_dependencies
    (tmp_path/'app.mjs').write_bytes((Path(app.__file__).parent/'static/app.js').read_bytes())
    copy_spa_dependencies(tmp_path)
    probe=tmp_path/'caps.mjs'
    probe.write_text('''
import assert from 'node:assert/strict';
const m=await import('./app.mjs');
const html=m.renderFailures({open:[],quiet:[],fixed:[],regressed:[],not_failures:[{class:'budget_exhausted',count:7,why:'Seven incidents were never attempted'}],note:'Entries may overlap.'});
assert.match(html,/Seven incidents were never attempted/);
assert.match(html,/Counted, but not failures/);
console.log('CAPS_RETAINED_OK');
''')
    result=subprocess.run([shutil.which('node'),str(probe)],capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stderr


@pytest.mark.parametrize('mode,expected', [
    ('spawn', 'call_failed:spawn_error'), ('empty', 'call_failed:empty_output'),
    ('parse', 'MineParseFailure'), ('shape', 'MineContractViolation'),
    ('constraint', 'IntegrityError:not_null:learnings.rule_text'),
])
def test_actual_pipeline_retains_distinct_causes_through_run_and_report(tmp_path, monkeypatch, mode, expected):
    from dataclasses import replace
    import sqlite3
    from self_improve import miner, pipeline, report
    from tests.e2e_corpus import build_corpus, ScriptedLLM, mine_payload
    corpus=build_corpus(tmp_path)
    try:
        failure = ('spawn_error', 'Invented executable absence') if mode=='spawn' else ('empty_output', 'Invented empty stdout') if mode=='empty' else None
        payload = None if mode=='parse' else {} if mode=='shape' else mine_payload('Invented rule')
        scripted=ScriptedLLM(mine_responses=[payload]*10, mine_failure=failure)
        if mode=='constraint':
            def constraint(*args, **kwargs):
                raise sqlite3.IntegrityError('NOT NULL constraint failed: learnings.rule_text')
            monkeypatch.setattr(miner, '_persist_mine_payload', constraint)
        cfg=replace(corpus.cfg, max_cheap_calls_per_run=2, max_gate_calls_per_run=0)
        stats=pipeline.run_pipeline(cfg, corpus.store, review_only=True, _llm_factory=scripted.factory())
        assert stats['mine']['taxonomy'][expected] > 0
        detail=run_data.detail(corpus.store, stats['run_id'])
        causes=next(s for s in detail['stages'] if s['name']=='mine')['causes']
        cause=next(r for r in causes if r['class']==expected)
        assert cause['count']==stats['mine']['taxonomy'][expected]
        assert cause['kind']=='failure'
        snapshot=list(corpus.store.conn.iterdump())
        destination=tmp_path/'checked-report.md'
        report.generate(corpus.store, cfg, stats['run_id'], destination)
        body=destination.read_text().split('## Raw stats',1)[0]
        assert expected in body and cause['name'] in body and cause['explanation'] in body
        assert list(corpus.store.conn.iterdump())==snapshot
        assert corpus.store.query_one('SELECT COUNT(*) n FROM learnings')['n']==0
    finally:
        corpus.store.close()
