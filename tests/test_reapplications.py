"""A completed rollback can return its exact contribution to fresh manual Review."""
from contextlib import closing
from dataclasses import replace
import pytest

from self_improve import apply, reapplications
from self_improve.commands import CommandError, submit_command, review_snapshot
from self_improve.execution_policy import automatic_permission, waiting_proposals
from self_improve.store import Store, new_id
from self_improve.worker import run_once
from tests.test_apply import insert_proposal, make_diff, init_git_repo, run_git
from tests.test_delivery_worker import env, approve

BASE='# Rules\n\n- Original.\n'
APPLIED=BASE+'- Rule A.\n'
CURRENT=BASE+'- Rule B.\n\nHuman footer.\n'
REAPPLIED=BASE+'- Rule A.\n- Rule B.\n\nHuman footer.\n'


@pytest.fixture
def undone(env):
    cfg,store,target=env;target.write_text(BASE)
    proposal=insert_proposal(store,target=target,diff=make_diff(BASE,APPLIED),status='pending')
    approve(store,cfg,proposal);assert run_once(store,cfg)['state']=='completed'
    target.write_text(REAPPLIED)
    assert apply.rollback(store,cfg,proposal['id'])['outcome']=='rolled_back'
    assert target.read_text()==CURRENT
    return cfg,store,target,proposal


def request(undone,key=None):
    cfg,store,target,p=undone;shown=reapplications.preview(store,cfg,p['id'])
    return {'action':'request_reapplication','proposal_id':p['id'],
        'preview_revision':shown['revision'],'request_key':key or new_id()}


def test_reapplication_records_a_fresh_review_without_mutating_the_target(undone):
    cfg,store,target,p=undone
    shown=reapplications.preview(store,cfg,p['id'])
    assert shown['ready'] and shown['after_content']==REAPPLIED and shown['max_model_calls']==0
    body=request(undone);created=submit_command(store,cfg,body)
    assert created['state']=='completed' and created['action']=='request_reapplication'
    assert submit_command(store,cfg,body)==created
    draft=store.query_one('SELECT * FROM proposals WHERE id=?',(created['result']['proposal_id'],))
    assert draft['id']!=p['id'] and draft['status']=='pending' and draft['action']=='reapply'
    from self_improve.store import actor_for
    event=store.query_one("SELECT actor,note FROM proposal_events WHERE proposal_id=? AND event='created'",(draft['id'],))
    assert event['actor']==actor_for('created') and created['actor']=='user'
    assert target.read_text()==CURRENT and store.query('SELECT * FROM llm_calls')==[]
    assert draft['id'] in {x['id'] for x in waiting_proposals(store,cfg)}
    permission=automatic_permission(store,cfg,{**draft,'status':'gated_pass'})
    assert not permission['allowed'] and permission['reason']=='action_review_queue'
    assert review_snapshot(store,draft['id'],cfg)['snapshot']['reapplication']['source']['application_id']==shown['source']['application_id']
    approve(store,cfg,draft);assert run_once(store,cfg)['state']=='completed'
    assert target.read_text()==REAPPLIED
    assert store.query_one('SELECT status FROM proposals WHERE id=?',(p['id'],))['status']=='rolled_back'
    assert apply.rollback(store,cfg,draft['id'])['outcome']=='rolled_back'
    assert target.read_text()==CURRENT
    # Further reapplication follows the new application, not the older receipt.
    again=reapplications.preview(store,cfg,draft['id']);assert again['ready']
    assert not reapplications.preview(store,cfg,p['id'])['ready']


def test_stale_reapplication_preview_creates_no_draft(undone):
    cfg,store,target,p=undone;body=request(undone);target.write_text(CURRENT+'Later edit.\n')
    with pytest.raises(CommandError) as error:submit_command(store,cfg,body)
    assert error.value.code=='StaleReapplicationPreview'
    assert not store.query('SELECT * FROM proposal_reapplications')


def test_rejected_lesson_cannot_be_reintroduced(undone):
    cfg,store,target,p=undone;store.update('learnings','id',p['learning_id'],{'status':'rejected'});store.commit()
    shown=reapplications.preview(store,cfg,p['id'])
    assert not shown['ready'] and shown['error_code']=='LessonRejected'
    with pytest.raises(CommandError):submit_command(store,cfg,request(undone))
    assert target.read_text()==CURRENT


def test_changed_original_block_is_a_visible_conflict(undone):
    cfg,store,target,p=undone;target.write_text('A different document.\n')
    shown=reapplications.preview(store,cfg,p['id'])
    assert not shown['ready'] and shown['error_code']=='ReapplicationConflict'
    assert target.read_text()=='A different document.\n'


@pytest.mark.parametrize('stage',['prepared','file_replaced','before_ack'])
def test_reapplication_recovery_preserves_one_new_application(undone,monkeypatch,stage):
    cfg,store,target,p=undone;created=submit_command(store,cfg,request(undone))
    draft=store.query_one('SELECT * FROM proposals WHERE id=?',(created['result']['proposal_id'],));approve(store,cfg,draft)
    def crash(point,*args):
        if point==stage:raise KeyboardInterrupt()
    monkeypatch.setattr(apply,'_delivery_checkpoint',crash)
    with pytest.raises(KeyboardInterrupt):run_once(store,cfg)
    monkeypatch.setattr(apply,'_delivery_checkpoint',lambda *_:None)
    with closing(Store(store.db_path,migrate=False)) as fresh:assert run_once(fresh,cfg)['state']=='completed'
    assert target.read_text()==REAPPLIED
    assert len(store.query("SELECT * FROM proposal_events WHERE proposal_id=? AND event='applied'",(draft['id'],)))==1


def test_two_reapplication_alternatives_cannot_deliver_twice(undone):
    cfg,store,target,p=undone;drafts=[]
    for _ in range(2):
        created=submit_command(store,cfg,request(undone))
        drafts.append(store.query_one('SELECT * FROM proposals WHERE id=?',(created['result']['proposal_id'],)))
    with pytest.raises(CommandError,match='Select one reapplication'):approve(store,cfg,*drafts)
    for draft in drafts:approve(store,cfg,draft)
    assert run_once(store,cfg)['state']=='completed'
    blocked=run_once(store,cfg)
    assert blocked['state']=='blocked' and blocked['targets'][0]['error_code']=='SourceReapplied'
    assert target.read_text()==REAPPLIED


def test_reapplication_uses_the_recorded_branch(undone,tmp_path):
    cfg,store,unused,p=undone;repo=init_git_repo(tmp_path/'project','AGENTS.md',BASE);target=repo/'AGENTS.md'
    original=insert_proposal(store,target=target,target_kind='project_agents_md',diff=make_diff(BASE,APPLIED),status='pending')
    approve(store,cfg,original);assert run_once(store,cfg)['state']=='completed'
    assert apply.rollback(store,cfg,original['id'])['outcome']=='rolled_back'
    cfg=replace(cfg,project_branch_name='changed/branch')
    created=submit_command(store,cfg,request((cfg,store,target,original)))
    draft=store.query_one('SELECT * FROM proposals WHERE id=?',(created['result']['proposal_id'],))
    head=run_git(['rev-parse','HEAD'],repo);index=(repo/'.git/index').read_bytes()
    approved=approve(store,cfg,draft);assert approved['targets'][0]['destination']['branch_name']=='self-improve/rules'
    assert run_once(store,cfg)['state']=='completed'
    assert run_git(['show','self-improve/rules:AGENTS.md'],repo)==APPLIED.strip()
    assert target.read_text()==BASE and (repo/'.git/index').read_bytes()==index and run_git(['rev-parse','HEAD'],repo)==head


def test_reapplication_restores_an_absent_file_after_its_rollback(env):
    cfg,store,target=env;target.unlink()
    original=insert_proposal(store,target=target,diff=make_diff('',APPLIED),status='pending')
    approve(store,cfg,original);assert run_once(store,cfg)['state']=='completed'
    apply.rollback(store,cfg,original['id']);assert not target.exists()
    created=submit_command(store,cfg,request((cfg,store,target,original)))
    draft=store.query_one('SELECT * FROM proposals WHERE id=?',(created['result']['proposal_id'],));approve(store,cfg,draft)
    assert run_once(store,cfg)['state']=='completed' and target.read_text()==APPLIED


def test_reapplication_accepts_a_verified_historical_rollback(env):
    import json
    cfg,store,target=env;target.write_text(BASE)
    original=insert_proposal(store,target=target,diff=make_diff(BASE,APPLIED),status='pending')
    approve(store,cfg,original);run_once(store,cfg);apply.rollback(store,cfg,original['id'])
    event=store.query_one("SELECT * FROM proposal_events WHERE proposal_id=? AND event='rolled_back'",(original['id'],))
    note=json.loads(event['note']);operation=note.pop('operation_id')
    store.update('proposal_events','id',event['id'],{'note':json.dumps(note)})
    store.conn.execute('DELETE FROM instruction_operations WHERE id=?',(operation,));store.commit()
    shown=reapplications.preview(store,cfg,original['id'])
    assert shown['ready'] and shown['rollback_receipt']['kind']=='historical'
    note['snapshot_rollback']=note['restored_from']+'not-a-commit'
    store.update('proposal_events','id',event['id'],{'note':json.dumps(note)});store.commit()
    assert not reapplications.preview(store,cfg,original['id'])['ready']


def test_reapplication_http_uses_the_selected_store(undone,tmp_path):
    from fastapi.testclient import TestClient
    from self_improve.dashboard.app import create_app
    cfg,store,target,p=undone;copy=tmp_path/'copy.db'
    with closing(Store(copy)) as copied:store.conn.backup(copied.conn)
    before=store.query('SELECT * FROM commands')
    with TestClient(create_app(cfg,db_path=copy)) as client:
        shown=client.get(f"/api/proposals/{p['id']}/reapplication-preview");assert shown.status_code==200
        body={'action':'request_reapplication','proposal_id':p['id'],'preview_revision':shown.json()['revision'],'request_key':new_id()}
        created=client.post('/api/commands',json=body);assert created.status_code==202,created.text
        assert client.post('/api/commands',json=body).json()==created.json()
        assert client.get(f"/api/proposals/{p['id']}/reapplication-preview").json()['latest_request']['id']==created.json()['id']
        assert client.post('/api/commands',json={**body,'path':'/unchecked'}).status_code==400
    assert store.query('SELECT * FROM commands')==before and target.read_text()==CURRENT


def test_reapplication_does_not_claim_a_newer_original_application(undone):
    from self_improve.rollback import latest_application
    cfg,store,target,p=undone;created=submit_command(store,cfg,request(undone))
    draft=store.query_one('SELECT * FROM proposals WHERE id=?',(created['result']['proposal_id'],));approve(store,cfg,draft)
    event=latest_application(store,p['id']);store.insert('proposal_events',{**event,'id':new_id(),'ts':'2099-01-01T00:00:00Z'});store.commit()
    blocked=run_once(store,cfg)
    assert blocked['state']=='blocked' and blocked['targets'][0]['error_code']=='ApplicationChanged'
    assert target.read_text()==CURRENT


def test_process_exit_after_reapplication_write_is_reconciled_once(undone,tmp_path):
    import os,subprocess,sys,json
    from dataclasses import asdict
    from pathlib import Path
    cfg,store,target,p=undone;created=submit_command(store,cfg,request(undone))
    draft=store.query_one('SELECT * FROM proposals WHERE id=?',(created['result']['proposal_id'],));approve(store,cfg,draft)
    config=tmp_path/'config.json';config.write_text(json.dumps(asdict(cfg)))
    script=tmp_path/'exit_reapplication.py';script.write_text("""import json,os,sys
from self_improve import apply
from self_improve.config import Config
from self_improve.store import Store
from self_improve.worker import run_once
cfg=Config(**json.load(open(sys.argv[1])))
def stop(event,*args):
    if event=='file_replaced':os._exit(77)
apply._delivery_checkpoint=stop
run_once(Store(cfg.state_path('state.db'),migrate=False),cfg)
""")
    child=subprocess.run([sys.executable,str(script),str(config)],cwd=tmp_path,
        env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1])},capture_output=True,text=True,timeout=30)
    assert child.returncode==77,child.stderr
    assert target.read_text()==REAPPLIED
    assert not store.query("SELECT * FROM proposal_events WHERE proposal_id=? AND event='applied'",(draft['id'],))
    assert run_once(store,cfg)['state']=='completed'
    assert len(store.query("SELECT * FROM proposal_events WHERE proposal_id=? AND event='applied'",(draft['id'],)))==1


def test_rollback_and_reapplication_keep_a_recorded_destination_after_alias_moves(env,tmp_path):
    cfg,store,target=env;target.write_text(BASE)
    alias=tmp_path/'alias';alias.symlink_to(target.parent,target_is_directory=True)
    named=alias/target.name
    p=insert_proposal(store,target=named,diff=make_diff(BASE,APPLIED),status='pending')
    approve(store,cfg,p);assert run_once(store,cfg)['state']=='completed'
    other=tmp_path/'other';other.mkdir();untouched=other/target.name;untouched.write_text('Unrelated target.\n')
    alias.unlink();alias.symlink_to(other,target_is_directory=True)
    apply.rollback(store,cfg,p['id']);assert target.read_text()==BASE
    created=submit_command(store,cfg,request((cfg,store,target,p)))
    draft=store.query_one('SELECT * FROM proposals WHERE id=?',(created['result']['proposal_id'],))
    approve(store,cfg,draft);assert run_once(store,cfg)['state']=='completed'
    assert target.read_text()==APPLIED and untouched.read_text()=='Unrelated target.\n'


from tests.test_rollback_resolutions import conflict as resolution_conflict


def test_reapplication_after_a_reviewed_resolution_uses_no_additional_models(resolution_conflict):
    from self_improve import jobs,job_worker
    cfg,store,target,p,calls=resolution_conflict
    shown=jobs.preview(store,cfg,p['id'],action='resolve_rollback')
    submit_command(store,cfg,{'action':'resolve_rollback','proposal_id':p['id'],'request_key':new_id(),'preview_revision':shown['revision']})
    generated=job_worker.run_once(store,cfg)
    resolved=store.query_one('SELECT * FROM proposals WHERE id=?',(generated['result']['proposal_id'],))
    approve(store,cfg,resolved);assert run_once(store,cfg)['state']=='completed'
    assert target.read_text()==CURRENT
    shown=reapplications.preview(store,cfg,p['id']);assert shown['ready'] and shown['rollback_receipt']['kind']=='resolution'
    created=submit_command(store,cfg,request((cfg,store,target,p)))
    draft=store.query_one('SELECT * FROM proposals WHERE id=?',(created['result']['proposal_id'],))
    approve(store,cfg,draft);assert run_once(store,cfg)['state']=='completed'
    assert target.read_text()==REAPPLIED and len(calls)==1


@pytest.mark.parametrize('approved',[False,True])
def test_a_fresh_target_preview_replaces_only_undecided_older_drafts(undone,approved):
    cfg,store,target,p=undone
    first=submit_command(store,cfg,request(undone));old=first['result']['proposal_id']
    if approved:approve(store,cfg,store.query_one('SELECT * FROM proposals WHERE id=?',(old,)))
    target.write_text(CURRENT+'A later unrelated note.\n')
    shown=reapplications.view(store,cfg,p['id'])
    assert shown['ready'] and shown['latest_request'] is None
    newer=submit_command(store,cfg,request(undone))['result']['proposal_id']
    assert store.query_one('SELECT status FROM proposals WHERE id=?',(old,))['status']==('approved_user' if approved else 'superseded')
    assert newer in {x['id'] for x in waiting_proposals(store,cfg)}
    assert store.query_one('SELECT status FROM learnings WHERE id=?',(p['learning_id'],))['status']!='rejected'
