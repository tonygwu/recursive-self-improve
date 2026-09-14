"""An exact run never borrows calls, verdicts, or delivery from a neighboring run."""
import json

import pytest

from self_improve.dashboard import run_data
from self_improve.store import Store


@pytest.fixture
def store(tmp_path):
    held=Store(tmp_path/'runs.db')
    try:
        for key in ('first','second'):
            held.insert('runs',{'id':key,'started':'2030-01-02T02:00:00Z','status':'ok',
                'stats_json':json.dumps({'run_id':key,'gate':{'attempted':3,'gated_pass':1,'gated_fail':0,
                    'ungated':0,'inconclusive':0,'failed':0,'refused':2,'taxonomy':{'gate_budget_exhausted':2}},
                    'llm':{'attempted':2 if key=='first' else 1,'calls_made':{'gate':2},'refused':{'gate':1}}})})
        for i in range(3):
            held.insert('llm_calls',{'id':f'call-{i}','run_id':'first' if i<2 else 'second',
                'stage':'grade','created_at':'2030-01-02T02:00:05Z','outcome':'ok','model_reported':'invented-model',
                'tokens_in':i+1,'tokens_out':i+2})
        held.commit()
        yield held
    finally:
        held.close()


def test_same_timestamp_runs_keep_exact_calls_native_outcomes_and_unknown_caps(store):
    detail=run_data.detail(store,'first')
    assert [r['id'] for r in detail['night_runs']]==['first','second']
    assert detail['run']['id']=='first'
    gate=next(s for s in detail['stages'] if s['name']=='gate')
    assert gate['payload']['refused']==2 and gate['payload']['gated_pass']==1
    assert 'succeeded' not in gate['payload']
    assert not next(s for s in detail['stages'] if s['name']=='scan')['recorded']
    assert detail['calls']['recorded']==2 and detail['calls']['reconciled'] is True
    assert detail['calls']['tokens_in']==3 and detail['calls']['tokens_out']==5
    assert detail['budget_limits'] is None
    first=run_data.records(store,'first',kind='calls',limit=1)
    last=run_data.records(store,'first',kind='calls',limit=1,cursor=first['next_cursor'])
    assert [x['id'] for x in first['records']+last['records']]==['call-0','call-1']
    assert last['next_cursor'] is None
    with pytest.raises(run_data.RunDataError,match='cursor'):
        run_data.records(store,'second',kind='calls',cursor=first['next_cursor'])


def test_missing_run_and_bad_stats_never_fall_back_to_another_run(store):
    with pytest.raises(run_data.RunNotFound):run_data.detail(store,'missing')
    for broken in ('[]','{bad','{"run_id":"second"}','{"gate":[]}','{"gate":{"failed":NaN}}'):
        store.update('runs','id','first',{'stats_json':broken});store.commit()
        with pytest.raises(run_data.RunDataError,match='first'):run_data.detail(store,'first')


def test_missing_calls_expose_the_difference_from_the_recorded_total(store):
    store.conn.execute("DELETE FROM llm_calls WHERE id='call-1'");store.commit()
    result=run_data.detail(store,'first')['calls']
    assert result['recorded']==1 and result['reported']==2 and not result['reconciled']
    assert result['reason']


def test_zero_work_and_missing_stage_are_distinct(store):
    store.update('runs','id','first',{'stats_json':json.dumps({'scan':{'files_attempted':0,'files_succeeded':0,'files_failed':0}})})
    store.commit()
    stages=run_data.detail(store,'first')['stages']
    assert stages[0]['recorded'] and stages[0]['payload']['files_attempted']==0
    assert not stages[1]['recorded'] and stages[1]['payload'] is None


def test_runs_api_is_read_only_and_selected_database_is_respected(store,tmp_path):
    from fastapi.testclient import TestClient
    from self_improve.config import Config
    from self_improve.dashboard.app import create_app
    copied=Store(tmp_path/'copy.db');store.conn.backup(copied.conn);copied.close()
    store.conn.execute("DELETE FROM llm_calls WHERE run_id='first'");store.commit()
    before=(tmp_path/'copy.db').read_bytes()
    with TestClient(create_app(Config(state_dir=str(tmp_path/'unused')),db_path=tmp_path/'copy.db')) as client:
        assert client.get('/api/runs?day=2030-01-02').json()['count']==2
        assert client.get('/api/runs/first').json()['calls']['recorded']==2
        assert client.get('/api/runs/first/records?kind=calls').json()['count']==2
        assert client.get('/api/runs/missing').status_code==404
        assert client.get('/api/runs/first/records?kind=anything').status_code==400
        assert client.get('/api/runs?day=not-a-date').status_code==400
    assert (tmp_path/'copy.db').read_bytes()==before


def test_eval_history_uses_exact_links_and_retains_full_metrics(store):
    store.insert('learnings', {'id':'lesson','title':'Synthetic lesson','rule_text':'Check the fixture.','created_at':'2030-01-02T02:00:00Z'})
    store.insert('proposals', {'id':'proposal','learning_id':'lesson','run_id':'second','target_path':'fixture.md','target_kind':'project_agents_md','action':'add','created_at':'2030-01-02T02:00:00Z'})
    store.insert('proposal_revisions', {'id':'revision','proposal_id':'proposal','fingerprint':'test','snapshot_json':'{}','created_at':'2030-01-02T02:00:00Z'})
    for key in ('linked','unlinked'):
        store.insert('eval_results', {'id':key,'kind':'regression','started':'2030-01-02T02:00:05Z','verdict':'inconclusive','metrics_json':json.dumps({'scenarios':list(range(101))})})
    store.insert('proposal_eval_history', {'id':'history','proposal_id':'proposal','source_revision_id':'revision','run_id':'first','eval_result_id':'linked','verdict':'inconclusive','created_at':'2030-01-02T02:00:05Z'})
    store.update('proposals','id','proposal',{'eval_result_id':'unlinked'})
    store.commit()
    result=run_data.records(store,'first',kind='evaluations')
    assert [r['id'] for r in result['records']]==['linked']
    assert result['records'][0]['metrics']['scenarios']==list(range(101))
    assert run_data.records(store,'second',kind='evaluations')['records']==[]
    assert run_data.records(store,'first',kind='proposals')['records']==[]
    assert 'current' in run_data.records(store,'second',kind='proposals')['records'][0]['association']
    store.update('eval_results','id','linked',{'metrics_json':'[]'});store.commit()
    with pytest.raises(run_data.RunDataError,match='linked.metrics'):
        run_data.records(store,'first',kind='evaluations')


def test_missing_delivery_links_are_unknown_and_bad_shapes_fail(store):
    assert run_data.records(store,'first',kind='deliveries')['reason']
    for payload in ([],{'operation_ids':'anything'}, {'operation_ids':['same','same']}):
        store.update('runs','id','first',{'stats_json':json.dumps({'apply':payload})});store.commit()
        with pytest.raises(run_data.RunDataError,match='first'):
            run_data.records(store,'first',kind='deliveries')
    store.update('runs','id','first',{'stats_json':json.dumps({'apply':{'applied':0,'operation_ids':[]}})});store.commit()
    result=run_data.records(store,'first',kind='deliveries')
    assert result['records']==[] and result['reason']==''


def test_older_schema_reads_never_migrate_or_invent_history(tmp_path,monkeypatch):
    from self_improve import store as store_module
    with monkeypatch.context() as patch:
        patch.setattr(store_module,'MIGRATIONS',[(name,sql) for name,sql in store_module.MIGRATIONS if name<'0010'])
        old=Store(tmp_path/'old.db')
        old.insert('runs',{'id':'old-run','started':'2030-01-02T02:00:00Z','stats_json':'{}'})
        old.commit();old.close()
    before=(tmp_path/'old.db').read_bytes()
    reader=Store(tmp_path/'old.db',read_only=True)
    try:
        detail=run_data.detail(reader,'old-run')
        assert detail['missing_job_schemas'] and detail['command_ids']==[]
        assert run_data.records(reader,'old-run',kind='evaluations')['reason']
        assert run_data.records(reader,'old-run',kind='deliveries')['reason']
        assert not reader.query_one("SELECT name FROM schema_migrations WHERE name='0010_dashboard_commands'")
    finally:reader.close()
    assert (tmp_path/'old.db').read_bytes()==before
