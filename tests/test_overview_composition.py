"""Overview populations and reviewed deliveries never infer current rule use."""
from contextlib import closing
import json

import pytest

from self_improve.dashboard import overview_data
from self_improve.store import Store
from tests.test_delivery_worker import env, approve, propose
from tests.test_dashboard_queries import _session
from tests.test_eval_history import env as evaluation_env, cfg, store, execute


def test_loop_counts_qualify_native_sessions_and_preserve_unknown_coverage(tmp_path):
    with closing(Store(tmp_path/'state.db')) as store:
        for i, (provider, native) in enumerate((('claude','same'),('claude','same'),('codex','same'),('codex',''),('future','same'))):
            path=str(tmp_path/f'{i}.jsonl')
            _session(store,file_path=path,project_key='fixture',source=provider)
            store.update('sessions','file_path',path,{'session_id':native})
        before=list(store.conn.iterdump())
        data=overview_data.loop_totals(store)
        assert data['sessions']=={'known':2,'unknown_transcripts':2,'indexed_transcripts':5}
        assert data['incidents']==data['learnings']==0
        assert data['evaluations']['attempts']==data['evaluations']['passed']==0
        assert data['delivery']['manual_targets']==data['delivery']['automatic_operations']==0
        assert data['delivery']['rollback_operations']==0
        assert list(store.conn.iterdump())==before
        store.conn.execute("DELETE FROM schema_migrations WHERE name IN ('0023_eval_attempts','0010_dashboard_commands','0012_instruction_operations')")
        missing=overview_data.loop_totals(store)
        assert missing['evaluations']['attempts'] is missing['evaluations']['passed'] is None
        assert missing['delivery']['manual_targets'] is missing['delivery']['automatic_operations'] is None
        assert missing['delivery']['rollback_operations'] is None


def test_reviewed_deliveries_are_frozen_targets_not_mutable_proposals(env, tmp_path):
    from self_improve.worker import run_once
    cfg,store,target=env
    one,two=propose(store,target),propose(store,target)
    frozen=store.query_one('SELECT title FROM learnings WHERE id=?',(one['learning_id'],))['title']
    command=approve(store,cfg,one,two)
    assert overview_data.delivery_activity(store)['manual_targets']==0
    run_once(store,cfg)
    for p in (one,two):
        store.update('learnings','id',p['learning_id'],{'title':'Mutated current title'})
        store.update('proposals','id',p['id'],{'status':'rejected_user'})
    store.commit()
    before=list(store.conn.iterdump());target.unlink()
    data=overview_data.delivery_activity(store)
    assert data['manual_targets']==1 and len(data['recent'])==1
    row=data['recent'][0]
    assert row['command_id']==command['id'] and row['member_count']==2
    assert row['title']==frozen and row['snapshot_before'] and row['snapshot_after']
    assert row['destination']['target_path']==str(target)
    assert list(store.conn.iterdump())==before


@pytest.mark.parametrize('first_time,second_time',[
    ('2030-01-02T01:00:00Z','2030-01-02T02:30:00+02:00'),
    ('2030-01-02T01:00:00.500000Z','2030-01-02T01:00:00Z'),
])
def test_completed_target_survives_partial_command_and_sorts_by_delivery_time(env,tmp_path,monkeypatch,first_time,second_time):
    from self_improve.worker import run_once
    from self_improve import apply
    cfg,store,target=env
    other=tmp_path/'other.md';other.write_text('before\n')
    one,two=propose(store,target),propose(store,other)
    first=approve(store,cfg,one,two)
    other.write_text('Changed after approval\n')
    monkeypatch.setattr(apply,'utc_now_iso',lambda:first_time)
    assert run_once(store,cfg)['state']=='blocked'
    third=tmp_path/'third.md';third.write_text('before\n')
    second=approve(store,cfg,propose(store,third))
    monkeypatch.setattr(apply,'utc_now_iso',lambda:second_time)
    run_once(store,cfg)
    data=overview_data.delivery_activity(store)
    assert data['manual_targets']==2
    assert [r['command_id'] for r in data['recent']]==[first['id'],second['id']]
    assert data['recent'][0]['completed_at']==first_time


@pytest.mark.parametrize('damage',['delivery','result','time','snapshot','branch','event'])
def test_incomplete_or_changed_completion_evidence_fails_loudly(env,damage):
    from self_improve.worker import run_once
    cfg,store,target=env
    command=approve(store,cfg,propose(store,target));run_once(store,cfg)
    row=store.query_one('SELECT * FROM command_targets WHERE command_id=?',(command['id'],))
    checkpoint=json.loads(row['checkpoint_json'])
    if damage in ('delivery','result'):checkpoint.pop(damage)
    elif damage=='time':checkpoint['result']['completed_at']='2030-01-02'
    elif damage=='snapshot':checkpoint['result']['snapshot_after']='changed-snapshot'
    elif damage=='branch':checkpoint['result']['branch_commit']='changed-branch'
    else:store.conn.execute("DELETE FROM proposal_events WHERE event='applied'")
    store.update('command_targets','id',row['id'],{'checkpoint_json':json.dumps(checkpoint)})
    with pytest.raises(ValueError):overview_data.delivery_activity(store)


def test_overview_new_sections_are_read_links_and_escape_retained_text():
    from tests.test_navigation import node
    result=node('''
      console.log(JSON.stringify({loop:app.renderOverviewLoop({sessions:{known:2,unknown_transcripts:1,indexed_transcripts:4},incidents:3,learnings:1,evaluations:{attempts:null,passed:null,unlinked_results:2},delivery:{manual_targets:null,automatic_operations:null,rollback_operations:null,legacy_application_events:2}}),
        deliveries:app.renderOverviewDeliveries({manual_targets:3,recent:[{command_id:'cmd/one',target_id:'t',completed_at:'2030-01-01T00:00:00Z',title:'<script>unsafe</script>',title_cut:true,member_count:2,destination:{target_path:'/invented/<target>',mode:'file'},snapshot_before:'before',snapshot_after:'after'}]})}));
    ''')
    assert 'Recorded populations' in result['loop'] and 'Unknown' in result['loop']
    assert '#/evals' in result['loop']
    assert '#/review/command/cmd%2Fone' in result['deliveries']
    assert '<script>' not in result['deliveries'] and '&lt;script&gt;' in result['deliveries']
    assert '2 proposals' in result['deliveries'] and '…' in result['deliveries']
    assert '<button' not in result['loop']+result['deliveries']


def test_loop_passes_require_complete_actual_attempt_evidence(evaluation_env):
    from self_improve import eval_history
    _,store,proposal,calls=evaluation_env
    _,attempt=execute(evaluation_env)
    count=len(calls)
    assert overview_data.loop_totals(store)['evaluations']=={'attempts':1,'passed':1,'unlinked_results':0}
    learning=attempt['source']['learning']
    recorder=eval_history.begin(store,evaluation_env[0],learning,proposal)
    recorder.stop(ValueError('Invented interrupted evaluation'))
    store.update('proposals','id',proposal['id'],{'status':'gated_fail'});store.commit()
    assert overview_data.loop_totals(store)['evaluations']=={'attempts':2,'passed':1,'unlinked_results':0}
    assert len(calls)==count


@pytest.mark.parametrize('kind',['auto_apply','rollback'])
@pytest.mark.parametrize('damage',['none','mode','branch','snapshot','time','event'])
def test_operation_counts_validate_completion_and_preserve_independent_units(cfg,store,tmp_path,kind,damage):
    from self_improve import apply
    from tests.test_apply import insert_proposal,make_diff
    path=tmp_path/'automatic.md';path.write_text('before\n')
    proposal=insert_proposal(store,target=path,diff=make_diff('before\n','before\nafter\n'))
    apply.apply_proposal(store,cfg,proposal)
    if kind=='rollback':apply.rollback(store,cfg,proposal['id'])
    row=store.query_one('SELECT * FROM instruction_operations WHERE kind=?',(kind,))
    checkpoint=json.loads(row['checkpoint_json']);result=json.loads(row['result_json'])
    if damage=='mode':checkpoint['result'].pop('mode')
    elif damage=='branch':checkpoint['result']['branch_commit']='different'
    elif damage=='snapshot':
        checkpoint['result']['snapshot_after']=''
        result['snapshot_commit_after' if kind=='auto_apply' else 'snapshot_commit_rollback']=''
    elif damage=='time':checkpoint['result']['completed_at']='undated'
    elif damage=='event':
        store.conn.execute("DELETE FROM proposal_events WHERE event='applied'")
    store.update('instruction_operations','id',row['id'],{'checkpoint_json':json.dumps(checkpoint),'result_json':json.dumps(result)})
    if damage!='none':
        with pytest.raises(ValueError):overview_data.delivery_activity(store)
    else:
        data=overview_data.delivery_activity(store)
        assert data['automatic_operations']==1 and data['manual_targets']==0
        assert data['rollback_operations']==int(kind=='rollback')
        assert data['legacy_application_events']==0
