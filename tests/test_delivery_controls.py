"""Delivery controls preserve prior writes and the original reviewed revisions."""
from contextlib import closing
import json

import pytest
pytest.importorskip('fastapi')
from fastapi.testclient import TestClient

from self_improve import apply
from self_improve.commands import command_status
from self_improve.dashboard.app import create_app
from self_improve.store import Store
from self_improve.worker import run_once
from tests.test_delivery_worker import env, approve, propose


def control(client, command_id, action, key='control-request-001'):
    response=client.post('/api/commands',json={'request_key':key,'action':action,'command_id':command_id})
    assert response.status_code==202,response.text
    return response.json()


def test_retry_reuses_reviewed_targets_and_does_not_redeliver_successes(env,tmp_path,monkeypatch):
    cfg,store,one=env
    two=tmp_path/'second.md';two.write_text('before\n')
    p,q=propose(store,one),propose(store,two)
    cmd=approve(store,cfg,p,q)
    original=apply._atomic_write
    calls=[]
    def fail_second(path,data):
        if path==two: raise OSError('temporary disk failure')
        calls.append(path);return original(path,data)
    monkeypatch.setattr(apply,'_atomic_write',fail_second)
    assert run_once(store,cfg)['state']=='failed'
    with TestClient(create_app(cfg)) as client:
        queued=control(client,cmd['id'],'retry_delivery')
        prior=queued['control_history'][0]['before']['targets']
        failure=next(t for t in prior if t['state']=='failed')
        assert failure['error_code'] and 'temporary disk failure' in failure['error_detail']
        assert sorted(t['state'] for t in queued['targets'])==['completed','queued']
        assert {m['revision'] for m in queued['members']}=={m['revision'] for m in cmd['members']}
        assert two.read_text()=='before\n', 'the web request executed a target write'
        monkeypatch.setattr(apply,'_atomic_write',original)
        assert run_once(store,cfg)['state']=='completed'
        replay=control(client,cmd['id'],'retry_delivery')
        assert replay['state']=='completed'
    assert calls==[one]
    assert len(store.query("SELECT * FROM proposal_events WHERE event='applied'"))==2
    assert len(store.query('SELECT * FROM command_control_events'))==1
    assert run_once(store,cfg) is None


def test_cancel_queued_delivery_returns_undelivered_members_to_review(env):
    cfg,store,target=env
    p=propose(store,target);cmd=approve(store,cfg,p)
    with TestClient(create_app(cfg)) as client:
        requested=control(client,cmd['id'],'cancel_delivery')
        assert requested['cancel_requested'] is True
        assert target.read_text()=='before\n'
        result=run_once(store,cfg)
        assert result['state']=='cancelled'
        waiting=client.get('/api/review-queue').json()
        assert {p['id'] for f in waiting['families'] for p in f['proposals']}=={p['id']}
        assert control(client,cmd['id'],'cancel_delivery')['state']=='cancelled'
    assert target.read_text()=='before\n'
    assert store.query_one('SELECT status FROM proposals WHERE id=?',(p['id'],))['status']=='ungated'


@pytest.mark.parametrize('stage,expected', [('prepared','cancelled'),('file_replaced','completed'),('before_ack','completed')])
def test_cancellation_after_interruption_reconciles_a_possible_write(env,monkeypatch,stage,expected):
    cfg,store,target=env
    p=propose(store,target);cmd=approve(store,cfg,p)
    def crash(point,*_):
        if point==stage: raise KeyboardInterrupt('process stopped')
    monkeypatch.setattr(apply,'_delivery_checkpoint',crash)
    with pytest.raises(KeyboardInterrupt):run_once(store,cfg)
    with TestClient(create_app(cfg)) as client:control(client,cmd['id'],'cancel_delivery')
    monkeypatch.setattr(apply,'_delivery_checkpoint',lambda *_:None)
    result=run_once(store,cfg)
    assert result['state']==expected,result
    assert target.read_text()==('before\n' if expected=='cancelled' else 'before\nafter\n')
    assert len(store.query("SELECT * FROM proposal_events WHERE event='applied'"))==(expected=='completed')


def test_cancel_does_not_guess_when_interrupted_content_matches_neither_side(env,monkeypatch):
    cfg,store,target=env
    p=propose(store,target);cmd=approve(store,cfg,p)
    def crash(point,*_):
        if point=='prepared': raise KeyboardInterrupt('prepared')
    monkeypatch.setattr(apply,'_delivery_checkpoint',crash)
    with pytest.raises(KeyboardInterrupt):run_once(store,cfg)
    target.write_text('new human content\n')
    monkeypatch.setattr(apply,'_delivery_checkpoint',lambda *_:None)
    with TestClient(create_app(cfg)) as client:
        control(client,cmd['id'],'cancel_delivery')
        assert run_once(store,cfg)['state']=='blocked'
        retried=control(client,cmd['id'],'retry_delivery','retry-cancellation-001')
        assert retried['cancel_requested'] is True, 'retry must not undo cancellation'
        assert run_once(store,cfg)['state']=='blocked'
    assert target.read_text()=='new human content\n'


def test_cancel_between_preparation_and_mutation_stops_the_write(env,monkeypatch):
    from self_improve.commands import submit_command
    cfg,store,target=env
    p=propose(store,target);cmd=approve(store,cfg,p)
    def cancel(point,*_):
        if point=='prepared':
            with closing(Store(store.db_path,migrate=False)) as other:
                submit_command(other,cfg,{'action':'cancel_delivery','command_id':cmd['id'],'request_key':'cancel-between-001'})
    monkeypatch.setattr(apply,'_delivery_checkpoint',cancel)
    assert run_once(store,cfg)['state']=='cancelled'
    assert target.read_text()=='before\n'


def test_control_key_cannot_be_reused_for_another_action(env):
    cfg,store,target=env
    p=propose(store,target);cmd=approve(store,cfg,p)
    with TestClient(create_app(cfg)) as client:
        control(client,cmd['id'],'cancel_delivery')
        response=client.post('/api/commands',json={'request_key':'control-request-001','action':'retry_delivery','command_id':cmd['id']})
        assert response.status_code==409
        assert response.json()['error']=='IdempotencyConflict'


def test_delivery_history_has_stable_pagination_and_summaries_without_file_content(env,tmp_path):
    cfg,store,_=env
    commands=[]
    for n in range(5):
        target=tmp_path/f'target-{n}.md';target.write_text('before\n')
        commands.append(approve(store,cfg,propose(store,target)))
    for cmd in commands:
        store.update('commands','id',cmd['id'],{'created_at':'2026-09-13T00:00:00Z'})
    store.commit()
    with TestClient(create_app(cfg)) as client:
        first=client.get('/api/commands',params={'limit':2,'summary':True}).json()
        assert len(first['commands'])==2 and first['next_cursor']
        assert 'checkpoint' not in first['commands'][0]['targets'][0]
        assert 'before_content' not in json.dumps(first)
        ids=[c['id'] for c in first['commands']]
        cursor=first['next_cursor']
        while cursor:
            page=client.get('/api/commands',params={'limit':2,'summary':True,'cursor':cursor}).json()
            ids.extend(c['id'] for c in page['commands']);cursor=page['next_cursor']
        assert ids==sorted([c['id'] for c in commands],reverse=True)
        detailed=client.get('/api/commands/'+ids[0]).json()
        assert 'diff_unified' in detailed['targets'][0]
        assert client.get('/api/commands',params={'cursor':'bad-cursor'}).status_code==400


def test_delivery_event_names_the_executor_and_keeps_human_authorization_separate(env):
    cfg,store,target=env
    p=propose(store,target);approve(store,cfg,p)
    assert run_once(store,cfg)['state']=='completed'
    events=store.query('SELECT event, actor FROM proposal_events WHERE proposal_id=?',(p['id'],))
    assert {'event':'approved_user','actor':'user'} in events
    assert {'event':'applied','actor':'auto'} in events


@pytest.mark.parametrize('stage,expected', [('commit_prepared','cancelled'),('ref_updated','completed'),('before_ack','completed')])
def test_cancellation_reconciles_the_git_ref_without_reverting_a_delivered_commit(env,tmp_path,monkeypatch,stage,expected):
    from tests.test_apply import init_git_repo, run_git
    cfg,store,_=env
    repo=init_git_repo(tmp_path/'project','AGENTS.md','before\n')
    target=repo/'AGENTS.md'
    cmd=approve(store,cfg,propose(store,target,target_kind='project_agents_md'))
    head=run_git(['rev-parse','HEAD'],repo)
    def crash(point,*_):
        if point==stage:raise KeyboardInterrupt('interrupted Git delivery')
    monkeypatch.setattr(apply,'_delivery_checkpoint',crash)
    with pytest.raises(KeyboardInterrupt):run_once(store,cfg)
    with TestClient(create_app(cfg)) as client:control(client,cmd['id'],'cancel_delivery')
    monkeypatch.setattr(apply,'_delivery_checkpoint',lambda *_:None)
    assert run_once(store,cfg)['state']==expected
    assert run_git(['rev-parse','HEAD'],repo)==head
    assert target.read_text()=='before\n'
    if expected=='completed':
        assert run_git(['rev-list','--count',cfg.project_branch_name],repo)=='2'
        assert run_git(['show',cfg.project_branch_name+':AGENTS.md'],repo)=='before\nafter'
    else:
        assert run_git(['branch','--list',cfg.project_branch_name],repo)==''


@pytest.mark.parametrize('corrupt', [False, None, [], {'no_delivery_observed': False, 'at': '2026-09-13T00:00:00Z'}])
def test_cancelled_preparation_releases_the_lane_only_with_a_valid_checkpoint(env,monkeypatch,corrupt):
    from self_improve.commands import CommandError
    from self_improve.execution_policy import set_class_policy
    cfg,store,target=env
    cmd=approve(store,cfg,propose(store,target))
    def crash(point,*_):
        if point=='prepared':raise KeyboardInterrupt('prepared')
    monkeypatch.setattr(apply,'_delivery_checkpoint',crash)
    with pytest.raises(KeyboardInterrupt):run_once(store,cfg)
    with TestClient(create_app(cfg)) as client:control(client,cmd['id'],'cancel_delivery')
    monkeypatch.setattr(apply,'_delivery_checkpoint',lambda *_:None)
    assert run_once(store,cfg)['state']=='cancelled'
    row=store.query_one('SELECT * FROM command_targets WHERE command_id=?',(cmd['id'],))
    if corrupt is not False:
        checkpoint=json.loads(row['checkpoint_json']);checkpoint['cancellation']=corrupt
        store.update('command_targets','id',row['id'],{'checkpoint_json':json.dumps(checkpoint)})
        store.commit()
        with pytest.raises(CommandError,match='cancellation checkpoint'):command_status(store,cmd['id'])
        with pytest.raises(apply.DeliveryBlocked,match='cancellation checkpoint'):
            apply._refuse_unreconciled_delivery(store,cfg,str(target))
        assert target.read_text()=='before\n'
    else:
        set_class_policy(store,'global',True,now='2026-01-01T00:00:00Z')
        p=propose(store,target)
        store.update('proposals','id',p['id'],{'status':'gated_pass','created_at':'2026-09-13T00:00:00Z'})
        store.commit()
        p=store.query_one('SELECT * FROM proposals WHERE id=?',(p['id'],))
        assert apply.apply_proposal(store,cfg,p)['outcome']=='applied'
        assert target.read_text()=='before\nafter\n'


def test_cancellation_during_partial_delivery_does_not_strand_an_earlier_failed_target(env,tmp_path,monkeypatch):
    from self_improve.commands import submit_command
    cfg,store,one=env
    two=tmp_path/'second.md';two.write_text('before\n')
    cmd=approve(store,cfg,propose(store,one),propose(store,two))
    original=apply._deliver_command_target
    calls=[]
    def deliver(store,cfg,cid,target):
        calls.append(target['id'])
        if len(calls)==1:raise OSError('first target failed before cancellation')
        if len(calls)==2:
            with closing(Store(store.db_path,migrate=False)) as other:
                submit_command(other,cfg,{'action':'cancel_delivery','command_id':cid,'request_key':'cancel-partial-001'})
        return original(store,cfg,cid,target)
    monkeypatch.setattr(apply,'_deliver_command_target',deliver)
    first=run_once(store,cfg)
    assert first['state']=='queued', 'the cancellation requeued a failed target after this pass had visited it'
    final=run_once(store,cfg)
    assert final['state']=='cancelled'
    assert {t['state'] for t in final['targets']}=={'cancelled'}
    assert one.read_text()==two.read_text()=='before\n'
    assert run_once(store,cfg) is None
