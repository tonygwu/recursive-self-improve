"""Native run accounting and exact retained rule/proposal relationships."""
from contextlib import closing
import json

import pytest

from self_improve import mining_history
from self_improve.dashboard import queries, run_data
from self_improve.store import Store


GATE = {'attempted':3, 'gated_pass':0, 'gated_fail':0, 'ungated':0,
        'inconclusive':0, 'failed':0, 'refused':0}


def cell(stage, payload):
    return queries._stage_cell({'id':'selected', 'status':'ok'}, stage, {stage:payload})


def test_missing_and_unaccounted_gate_outcomes_do_not_establish_refusal():
    missing = cell('gate', {'attempted':3})
    assert missing['state'] == 'unreadable'
    assert 'gated_pass' in missing['missing_fields']
    unaccounted = cell('gate', GATE)
    assert unaccounted['state'] == 'unaccounted'
    assert unaccounted['unaccounted'] == 3 and unaccounted['refused'] == 0


def test_excess_apply_outcomes_never_rewrite_the_recorded_attempt_total():
    result = cell('apply', {'attempted':0, 'applied':0, 'held':2, 'failed':0})
    assert result['state'] == 'unaccounted'
    assert result['attempted'] == 0 and result['unaccounted'] == -2
    assert 'attempted_adjusted' not in result and not result.get('idle')


@pytest.mark.parametrize('value', [True, '3', 3.2, -1])
def test_native_counts_are_not_coerced(value):
    with pytest.raises(queries.DashboardDataError, match='selected.*gate.*attempted'):
        cell('gate', {**GATE, 'attempted':value})


def test_candidate_rules_never_become_merge_successes():
    fast = cell('cluster', {'candidates':9, 'merge_attempted':0,
                           'merge_succeeded':0, 'merge_failed':0})
    agentic = cell('cluster', {'candidates':9, 'mode':'agentic_passthrough'})
    assert fast['number'] == agentic['number'] == 9
    assert fast['attempted'] == agentic['attempted'] == 0
    assert fast['succeeded'] == agentic['succeeded'] == 0
    assert fast['state'] == agentic['state'] == 'ok'


def test_complete_native_gate_buckets_keep_verdicts_and_refusals_distinct():
    assert cell('gate', {**GATE, 'refused':3})['state'] == 'refused'
    assert cell('gate', {**GATE, 'gated_fail':1, 'refused':2})['state'] == 'budget_exhausted'
    assert cell('gate', {**GATE, 'gated_fail':3})['state'] == 'ok'


def seed_flow(store):
    from unittest.mock import patch
    for run_id in ('selected','neighbor'):
        store.insert('runs', {'id':run_id, 'started':'2030-01-02T00:00:00Z',
            'status':'ok', 'stats_json':json.dumps({'run_id':run_id, 'gate':GATE})})
    for lid in ('a','b','c'):
        store.insert('learnings', {'id':lid, 'rule_text':'Invented retained rule '+lid,
                                  'created_at':'2030-01-01T00:00:00Z'})
    for index,(lid,kind,run_id) in enumerate([('a','new','selected'),('a','amend','selected'),
                           ('a','duplicate','selected'),('b','new','selected'),
                           ('b','duplicate','neighbor')]):
        learning=store.query_one('SELECT * FROM learnings WHERE id=?',(lid,))
        with patch.object(mining_history,'utc_now_iso',return_value=f'2030-01-02T00:00:0{index}Z'):
            mining_history.append(store,learning,before=None,kind=kind,incidents=[],
                                  provenance={'run_id':run_id,'generation':'agentic'})
    for pid,lid,run_id in [('a-one','a','selected'),('c-one','c','selected'),
                           ('c-two','c','selected'),('neighbor-b','b','neighbor')]:
        store.insert('proposals', {'id':pid,'learning_id':lid,'run_id':run_id,
            'target_path':'/invented/'+pid+'.md', 'target_kind':'global_claude_md',
            'action':'add', 'diff_unified':'', 'created_at':'2030-01-02T00:00:00Z'})
    store.commit()


def test_rule_flow_is_exact_paginated_and_distinguishes_observations_from_proposals(tmp_path):
    with closing(Store(tmp_path/'state.db')) as store:
        seed_flow(store)
        snapshot=list(store.conn.iterdump())
        flow=run_data.detail(store,'selected')['flow']
        assert flow['mining_recorded'] is True
        assert flow['observation_count'] == 4 and flow['observed_learning_count'] == 2
        assert flow['created_proposal_count'] == 3 and flow['proposal_learning_count'] == 2
        assert flow['observed_without_proposal_count'] == 1
        assert flow['multiple_proposal_learning_count'] == 1
        first=run_data.records(store,'selected',kind='learnings',limit=2)
        last=run_data.records(store,'selected',kind='learnings',limit=2,cursor=first['next_cursor'])
        groups=first['records']+last['records']
        assert [r['id'] for r in groups] == ['a','b','c'] and last['next_cursor'] is None
        assert [r['kind'] for r in groups[0]['observations']] == ['new','amend','duplicate']
        assert [p['id'] for p in groups[1]['proposals']] == []
        assert [p['id'] for p in groups[2]['proposals']] == ['c-one','c-two']
        with pytest.raises(run_data.RunDataError, match='cursor'):
            run_data.records(store,'neighbor',kind='learnings',cursor=first['next_cursor'])
        assert list(store.conn.iterdump()) == snapshot


def test_old_schema_does_not_turn_missing_mining_observations_into_zero(tmp_path):
    with closing(Store(tmp_path/'state.db')) as store:
        seed_flow(store)
        store.conn.execute('DELETE FROM schema_migrations WHERE name=?',(mining_history.MIGRATION,))
        store.commit()
        flow=run_data.detail(store,'selected')['flow']
        assert flow['mining_recorded'] is False and flow['observation_count'] is None
        assert flow['observed_without_proposal_count'] is None
        assert flow['created_proposal_count'] == 3


def test_corrupt_retained_mining_identity_is_not_a_valid_rule_group(tmp_path):
    with closing(Store(tmp_path/'state.db')) as store:
        seed_flow(store)
        row=store.query_one('SELECT id FROM mining_history WHERE run_id=?',('selected',))
        store.update('mining_history','id',row['id'],{'record_json':'{}'})
        store.commit()
        with pytest.raises(run_data.RunDataError, match=row['id']):
            run_data.detail(store,'selected')
        with pytest.raises(run_data.RunDataError, match=row['id']):
            run_data.records(store,'selected',kind='learnings')


def test_actual_rule_group_renderer_exposes_each_link_and_complete_record():
    from tests.test_navigation import node
    result=node('''
      const group={id:'rule-a',learning:{rule_text:'Invented rule '.repeat(30)},
        mining_recorded:true,observations:[{kind:'amend',id:'observation-a'}],
        proposals:[{id:'proposal-a',target_path:'AGENTS.md',status:'pending'},
                   {id:'proposal-b',target_path:'CLAUDE.md',status:'held'}]};
      const html=app.renderRunRecords('learnings',{id:'selected',pages:{learnings:{
        loaded:true,records:[group],count:1}}});
      console.log(JSON.stringify({html}));
    ''')
    assert '#/rules/rule-a' in result['html']
    assert '#/review/proposal/proposal-a' in result['html']
    assert '#/review/proposal/proposal-b' in result['html']
    assert 'Complete rule observations and proposals' in result['html']
    assert 'observation-a' in result['html']


def test_summary_rejects_unknown_mining_kinds(tmp_path):
    with closing(Store(tmp_path/'state.db')) as store:
        seed_flow(store)
        store.conn.execute("UPDATE mining_history SET kind='invented-unknown' WHERE run_id='selected'")
        store.commit()
        with pytest.raises(run_data.RunDataError, match='selected.*unknown mining kind'):
            run_data.detail(store,'selected')


def test_reader_declarations_reconcile_with_independent_producer():
    from self_improve import pipeline, stage_accounting
    assert {k:stage_accounting.OUTCOMES[k] for k in pipeline.STAGE_INVARIANTS} == pipeline.STAGE_INVARIANTS


def test_accounting_import_does_not_load_pipeline_store_dashboard_or_provider():
    import subprocess, sys
    script = '''
import importlib.abc, sys
blocked = ('self_improve.pipeline','self_improve.store','self_improve.dashboard',
           'self_improve.llm','fastapi')
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name+'.') for name in blocked):
            raise RuntimeError('forbidden import '+fullname)
sys.meta_path.insert(0, Guard())
from self_improve import stage_accounting
assert stage_accounting.numbers('mine', {'attempted':0,'succeeded':0,'failed':0})['number'] == 0
try:
    import self_improve.pipeline
except RuntimeError:
    pass
else:
    raise AssertionError('import guard did not run')
print('ACCOUNTING_IMPORT_ISOLATED')
'''
    result = subprocess.run([sys.executable,'-c',script],capture_output=True,text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'ACCOUNTING_IMPORT_ISOLATED'


@pytest.mark.parametrize('stage,payload,state', [
    ('scan', {'files_attempted':0,'files_succeeded':0,'files_failed':0}, 'ok'),
    ('scan', {'files_attempted':3,'files_succeeded':2,'files_failed':1}, 'partial'),
    ('mine', {'attempted':2,'succeeded':0,'failed':2}, 'failed'),
    ('mine', {'attempted':2,'succeeded':1,'failed':1,'taxonomy':{'budget_refused_this_incident':1}}, 'budget_exhausted'),
    ('mine', {'attempted':1,'succeeded':0,'failed':1,'taxonomy':{'budget_refused_this_incident':1}}, 'refused'),
    ('mine', {'attempted':0,'succeeded':0,'failed':0,'taxonomy':{'provider_unavailable':4}}, 'limited'),
    ('mine', {'attempted':0,'succeeded':0,'failed':0,'taxonomy':{'wall_deadline_reached':4}}, 'limited'),
    ('mine', {'attempted':2,'succeeded':2,'failed':0,'taxonomy':{'budget_exhausted':4}}, 'limited'),
    ('cluster', {'candidates':9,'merge_attempted':2,'merge_succeeded':1,'merge_failed':1}, 'partial'),
    ('apply', {'attempted':3,'applied':1,'held':2,'failed':0}, 'ok'),
])
def test_native_units_cover_zero_failures_refusals_and_omissions(stage,payload,state):
    actual=cell(stage,payload)
    assert actual['state'] == state and actual['unaccounted'] == 0
    assert actual['meaning'] and actual['unit']
    assert actual['attempted'] == payload.get('attempted',payload.get('files_attempted',payload.get('merge_attempted')))


def test_contradictory_passthrough_and_mining_budget_buckets_fail():
    for stage,payload in [('cluster',{'mode':'agentic_passthrough','candidates':9,'merge_attempted':1}),
                          ('mine',{'attempted':1,'succeeded':1,'failed':0,'taxonomy':{'budget_refused_this_incident':1}})]:
        with pytest.raises(queries.DashboardDataError, match='selected'):
            cell(stage,payload)


def test_same_night_aggregation_labels_partial_counter_coverage(tmp_path):
    from datetime import datetime, timezone
    with closing(Store(tmp_path/'state.db')) as store:
        seed_flow(store)
        store.update('runs','id','selected',{'stats_json':json.dumps({'gate':{**GATE,'gated_pass':3}})})
        store.update('runs','id','neighbor',{'stats_json':json.dumps({'gate':{'attempted':4}})})
        store.commit()
        grid=queries.run_stage_grid(store,now_utc=datetime(2030,1,2,tzinfo=timezone.utc))
        gate=grid['columns'][0]['cells']['gate']
        assert gate['state'] == 'unreadable'
        assert gate['runs'] == 2 and gate['accounted_runs'] == 1 and gate['unreadable_runs'] == 1
        assert gate['attempted'] == 3 and gate['number'] == 3
        assert {c['run_id'] for c in gate['per_run']} == {'selected','neighbor'}


def test_copied_api_retains_observations_beside_current_proposals(tmp_path):
    from fastapi.testclient import TestClient
    from self_improve.config import Config
    from self_improve.dashboard.app import create_app
    with closing(Store(tmp_path/'original.db')) as store:
        seed_flow(store)
        store.update('learnings','id','a',{'rule_text':'Later current wording'})
        store.update('proposals','id','a-one',{'status':'rejected'})
        store.commit()
        copied=tmp_path/'copy.db'
        with closing(Store(copied)) as copy: store.conn.backup(copy.conn)
        store.conn.execute("DELETE FROM mining_history WHERE run_id='selected'")
        store.commit()
        before=copied.read_bytes()
        with TestClient(create_app(Config(state_dir=str(tmp_path/'unused')),db_path=copied)) as client:
            response=client.get('/api/runs/selected/records',params={'kind':'learnings','limit':1})
            assert response.status_code == 200,response.text
            a=response.json()['records'][0]
            assert a['learning']['rule_text'] == 'Later current wording'
            assert all(o['after']['rule_text']=='Invented retained rule a' for o in a['observations'])
            assert a['proposals'][0]['status'] == 'rejected'
            assert 'current' in a['association']
            assert client.get('/api/runs/neighbor/records',params={'kind':'learnings','cursor':response.json()['next_cursor']}).status_code == 400
        assert copied.read_bytes() == before


@pytest.mark.parametrize('mode',['agentic','fast'])
def test_real_temporary_pipeline_keeps_native_units_and_rule_links(tmp_path,mode):
    from dataclasses import replace
    from tests.e2e_corpus import build_corpus, ScriptedLLM, mine_payload
    from self_improve.pipeline import run_pipeline
    corpus=build_corpus(tmp_path)
    try:
        from self_improve.miner import MINE_AGENTIC_EXTRA_KEYS
        fast={k:v for k,v in mine_payload('Invented fast rule').items() if k not in MINE_AGENTIC_EXTRA_KEYS}
        llm=ScriptedLLM(mine_responses=[mine_payload('Invented rule '+str(i)) for i in range(20)],
                        other_responses={'mine':fast})
        stats=run_pipeline(replace(corpus.cfg,mine_mode=mode),corpus.store,
                           review_only=True,_llm_factory=llm.factory())
        result=run_data.detail(corpus.store,stats['run_id'])
        stages={s['name']:s for s in result['stages']}
        for name in ('scan','mine','cluster','gate','apply'):
            accounting=stages[name]['accounting']
            assert accounting['state'] not in ('unreadable','unaccounted'),(name,stats[name])
            assert accounting['unaccounted'] == 0
        assert stages['mine']['accounting']['attempted'] > 0
        if mode=='agentic': assert stages['cluster']['accounting']['attempted'] == 0
        else: assert stages['cluster']['accounting']['attempted'] == stats['cluster']['merge_attempted']
        groups=run_data.records(corpus.store,stats['run_id'],kind='learnings')['records']
        assert groups and result['flow']['observation_count'] > 0
        assert all(o['run_id']==stats['run_id'] for g in groups for o in g['observations'])
        assert all(p['run_id']==stats['run_id'] for g in groups for p in g['proposals'])
    finally: corpus.store.close()
