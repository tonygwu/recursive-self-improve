"""A paid conflict resolution returns to Review before any real scratch edit."""
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import json

import pytest

from self_improve import jobs,job_worker,rollback
from self_improve.commands import submit_command,review_snapshot,command_status
from self_improve.execution_policy import automatic_permission,waiting_proposals
from self_improve.llm import LLMRunner,_ExecResult
from self_improve.store import Store,new_id
from self_improve.worker import run_once
from tests.test_delivery_worker import env,approve
from tests.test_apply import insert_proposal,make_diff

BASE='# Rules\n\n- Original.\n'
APPLIED=BASE+'- Rule A.\n'
CURRENT=BASE+'- Rule A edited by a human.\n- Rule B.\n\nHuman footer.\n'
RESOLVED=BASE+'- Rule B.\n\nHuman footer.\n'


@pytest.fixture
def conflict(env,monkeypatch):
    cfg,store,target=env
    cfg=replace(cfg,codex_global_agents_md=str(target.parent/'global-agents.md'),skills_dir=str(target.parent/'skills'))
    target.write_text(BASE)
    p=insert_proposal(store,target=target,diff=make_diff(BASE,APPLIED),status='pending')
    approve(store,cfg,p);assert run_once(store,cfg)['state']=='completed'
    target.write_text(CURRENT);calls=[]
    def execute(self,*args,**kwargs):
        calls.append(args)
        value={'explanation':'Remove the edited A contribution while preserving B and the human footer.',
            'edits':[{'old':'- Rule A edited by a human.\n','new':''}]}
        return _ExecResult(ok=True,text=json.dumps(value),parsed=value,outcome='ok',provider='codex',model_reported='gpt-5.6-terra')
    monkeypatch.setattr(LLMRunner,'_execute',execute)
    return cfg,store,target,p,calls


def request(conflict,key=None):
    cfg,store,target,p,calls=conflict
    shown=jobs.preview(store,cfg,p['id'],action='resolve_rollback')
    return {'action':'resolve_rollback','proposal_id':p['id'],'request_key':key or new_id(),'preview_revision':shown['revision']}


def test_read_only_conflict_cost_and_idempotent_reservation(conflict):
    cfg,store,target,p,calls=conflict
    shown=jobs.preview(store,cfg,p['id'],action='resolve_rollback')
    assert shown['max_model_calls']==1 and shown['plan']['budgets']=={'cheap':0,'strong':1,'gate':0}
    assert shown['plan']['rollback']['base']['content']==CURRENT
    assert shown['plan']['rollback']['source']['applied']==APPLIED
    body=request(conflict);first=submit_command(store,cfg,body)
    assert submit_command(store,cfg,body)==first and not calls
    assert target.read_text()==CURRENT and store.query('SELECT * FROM proposal_resolutions')==[]


def test_generated_patch_is_reviewable_and_only_approval_changes_the_target(conflict):
    cfg,store,target,p,calls=conflict
    queued=submit_command(store,cfg,request(conflict));result=job_worker.run_once(store,cfg)
    assert result['state']=='completed',(result['error_code'],result['error_detail'])
    assert len(calls)==1 and result['budget']['consumed']=={'cheap':0,'strong':1,'gate':0}
    pid=result['result']['proposal_id'];draft=store.query_one('SELECT * FROM proposals WHERE id=?',(pid,))
    assert target.read_text()==CURRENT and draft['status']=='pending'
    assert store.query_one('SELECT status FROM proposals WHERE id=?',(p['id'],))['status']=='applied'
    assert pid in {x['id'] for x in waiting_proposals(store,cfg)}
    assert not automatic_permission(store,cfg,{**draft,'status':'gated_pass'})['allowed']
    source=review_snapshot(store,pid,cfg)
    assert source['snapshot']['resolution']['command_id']==queued['id']
    approved=approve(store,cfg,draft);delivered=run_once(store,cfg)
    assert delivered['state']=='completed',delivered
    assert target.read_text()==RESOLVED
    assert store.query_one('SELECT status FROM proposals WHERE id=?',(p['id'],))['status']=='rolled_back'
    assert rollback.rollback_preview(store,cfg,p['id'])['error_code']=='AlreadyRolledBack'
    assert run_once(store,cfg) is None and len(calls)==1


def test_target_changed_during_generation_never_publishes_a_stale_draft(conflict,monkeypatch):
    cfg,store,target,p,calls=conflict;original=LLMRunner._execute
    def change(self,*args,**kwargs):
        result=original(self,*args,**kwargs);target.write_text(CURRENT+'A later human change.\n');return result
    monkeypatch.setattr(LLMRunner,'_execute',change)
    submit_command(store,cfg,request(conflict));result=job_worker.run_once(store,cfg)
    assert result['state']=='failed' and result['error_code']=='ResolutionSourceChanged'
    assert len(calls)==1 and store.query('SELECT * FROM proposal_resolutions')==[]


def test_restart_after_generation_reuses_the_paid_result(conflict,monkeypatch):
    cfg,store,target,p,calls=conflict;submit_command(store,cfg,request(conflict))
    def stop(event):
        if event=='resolution_generated':raise KeyboardInterrupt()
    monkeypatch.setattr(job_worker,'_checkpoint',stop)
    with pytest.raises(KeyboardInterrupt):job_worker.run_once(store,cfg)
    assert len(calls)==1 and store.query('SELECT * FROM proposal_resolutions')==[]
    monkeypatch.setattr(job_worker,'_checkpoint',lambda _:None)
    with closing(Store(store.db_path,migrate=False)) as fresh:result=job_worker.run_once(fresh,cfg)
    assert result['state']=='completed',(result['error_code'],result['error_detail'])
    assert len(calls)==1 and len(store.query('SELECT * FROM proposal_resolutions'))==1


@pytest.mark.parametrize('stage',['prepared','file_replaced','before_ack'])
def test_interrupted_delivery_acknowledges_the_original_inverse_once(conflict,monkeypatch,stage):
    from self_improve import apply
    cfg,store,target,p,calls=conflict
    submit_command(store,cfg,request(conflict));generated=job_worker.run_once(store,cfg)
    draft=store.query_one('SELECT * FROM proposals WHERE id=?',(generated['result']['proposal_id'],))
    approve(store,cfg,draft)
    def crash(point,*args):
        if point==stage:raise KeyboardInterrupt()
    monkeypatch.setattr(apply,'_delivery_checkpoint',crash)
    with pytest.raises(KeyboardInterrupt):run_once(store,cfg)
    monkeypatch.setattr(apply,'_delivery_checkpoint',lambda *_:None)
    assert run_once(store,cfg)['state']=='completed'
    assert target.read_text()==RESOLVED
    events=store.query("SELECT * FROM proposal_events WHERE proposal_id=? AND event='rolled_back'",(p['id'],))
    assert len(events)==1 and len(calls)==1
    receipt=apply.rollback(store,cfg,p['id'])
    assert receipt['outcome']=='rolled_back' and receipt['resolution_proposal_id']==draft['id']
    assert len(store.query("SELECT * FROM proposal_events WHERE proposal_id=? AND event='rolled_back'",(p['id'],)))==1


def test_a_later_application_blocks_the_old_resolution_before_write(conflict):
    cfg,store,target,p,calls=conflict
    submit_command(store,cfg,request(conflict));generated=job_worker.run_once(store,cfg)
    draft=store.query_one('SELECT * FROM proposals WHERE id=?',(generated['result']['proposal_id'],));approve(store,cfg,draft)
    event=rollback.latest_application(store,p['id'])
    store.insert('proposal_events',{**event,'id':new_id(),'ts':'2099-01-01T00:00:00Z'});store.commit()
    result=run_once(store,cfg)
    assert result['state']=='blocked' and result['targets'][0]['error_code']=='ApplicationChanged'
    assert target.read_text()==CURRENT and len(calls)==1


def test_a_second_reviewed_resolution_cannot_undo_the_same_application_again(conflict):
    cfg,store,target,p,calls=conflict;drafts=[]
    for _ in range(2):
        submit_command(store,cfg,request(conflict));result=job_worker.run_once(store,cfg)
        draft=store.query_one('SELECT * FROM proposals WHERE id=?',(result['result']['proposal_id'],));approve(store,cfg,draft);drafts.append(draft)
    assert run_once(store,cfg)['state']=='completed'
    blocked=run_once(store,cfg)
    assert blocked['state']=='blocked' and blocked['targets'][0]['error_code']=='AlreadyRolledBack'
    assert target.read_text()==RESOLVED and len(calls)==2


def test_rejected_lesson_can_be_undone_by_an_explicit_reviewed_resolution(conflict):
    cfg,store,target,p,calls=conflict
    store.update('learnings','id',p['learning_id'],{'status':'rejected'});store.commit()
    submit_command(store,cfg,request(conflict));result=job_worker.run_once(store,cfg)
    draft=store.query_one('SELECT * FROM proposals WHERE id=?',(result['result']['proposal_id'],))
    assert draft['id'] in {x['id'] for x in waiting_proposals(store,cfg)}
    approve(store,cfg,draft);assert run_once(store,cfg)['state']=='completed'
    assert target.read_text()==RESOLVED
    assert store.query_one('SELECT status FROM learnings WHERE id=?',(p['learning_id'],))['status']=='rejected'


@pytest.mark.parametrize('value',[
    {'explanation':'no patch','edits':[]},
    {'explanation':'rewrite','edits':[{'old':CURRENT,'new':BASE}]},
    {'explanation':'bad shape','edits':[{'old':'Rule A','new':'','path':'/elsewhere'}]},
    {'explanation':'missing span','edits':[{'old':'not in current content','new':''}]},
    {'explanation':'overlap','edits':[{'old':'Rule A edited','new':''},{'old':'Rule A edited by a human.','new':''}]},
])
def test_invalid_model_patch_never_creates_a_draft(conflict,monkeypatch,value):
    cfg,store,target,p,calls=conflict
    def invalid(*args,**kwargs):return _ExecResult(ok=True,text=json.dumps(value),parsed=value,outcome='ok',provider='codex',model_reported='gpt-5.6-terra')
    monkeypatch.setattr(LLMRunner,'_execute',invalid)
    submit_command(store,cfg,request(conflict));result=job_worker.run_once(store,cfg)
    assert result['state']=='failed' and result['error_code']=='InvalidResolution',result['error_detail']
    assert store.query('SELECT * FROM proposal_resolutions')==[] and target.read_text()==CURRENT


def test_cancellation_during_generation_retains_the_call_but_creates_no_draft(conflict,monkeypatch):
    cfg,store,target,p,calls=conflict;original=LLMRunner._execute
    queued=submit_command(store,cfg,request(conflict))
    def cancel(self,*args,**kwargs):
        result=original(self,*args,**kwargs)
        with closing(Store(store.db_path,migrate=False)) as writer:
            submit_command(writer,cfg,{'action':'cancel_job','command_id':queued['id'],'request_key':new_id()})
        return result
    monkeypatch.setattr(LLMRunner,'_execute',cancel)
    result=job_worker.run_once(store,cfg)
    assert result['state']=='cancelled' and len(calls)==1
    assert result['calls'][0]['state']=='completed'
    assert store.query('SELECT * FROM proposal_resolutions')==[] and target.read_text()==CURRENT


def test_resolution_http_intent_uses_the_selected_copy(conflict,tmp_path):
    from fastapi.testclient import TestClient
    from self_improve.dashboard.app import create_app
    cfg,store,target,p,calls=conflict;copy=tmp_path/'copy.db'
    with closing(Store(copy)) as copied:store.conn.backup(copied.conn)
    before=store.query('SELECT * FROM commands')
    with TestClient(create_app(cfg,db_path=copy)) as client:
        shown=client.get(f"/api/proposals/{p['id']}/resolution-preview")
        assert shown.status_code==200,shown.text
        body={'action':'resolve_rollback','proposal_id':p['id'],'preview_revision':shown.json()['revision'],'request_key':new_id()}
        queued=client.post('/api/commands',json=body)
        assert queued.status_code==202,queued.text
        assert client.post('/api/commands',json=body).json()==queued.json()
        assert client.post('/api/commands',json={**body,'request_key':new_id(),'path':'/unchecked'}).status_code==400
    assert store.query('SELECT * FROM commands')==before and not calls and target.read_text()==CURRENT


def test_resolution_keeps_the_recorded_branch_when_config_changes(conflict,tmp_path):
    from tests.test_apply import init_git_repo,run_git
    cfg,store,unused,p,calls=conflict
    repo=init_git_repo(tmp_path/'project','AGENTS.md',BASE);target=repo/'AGENTS.md'
    original_branch=run_git(['branch','--show-current'],repo)
    p=insert_proposal(store,target=target,diff=make_diff(BASE,APPLIED),status='pending',target_kind='project_agents_md')
    approve(store,cfg,p);assert run_once(store,cfg)['state']=='completed'
    run_git(['checkout',cfg.project_branch_name],repo);target.write_text(CURRENT)
    run_git(['add','AGENTS.md'],repo);run_git(['commit','-m','A later human edit'],repo);run_git(['checkout',original_branch],repo)
    head=run_git(['rev-parse','HEAD'],repo);index=(repo/'.git/index').read_bytes()
    submit_command(store,cfg,request((cfg,store,target,p,calls)));generated=job_worker.run_once(store,cfg)
    assert generated['state']=='completed',generated['error_detail']
    draft=store.query_one('SELECT * FROM proposals WHERE id=?',(generated['result']['proposal_id'],))
    changed=replace(cfg,project_branch_name='different/branch')
    approve(store,changed,draft);assert run_once(store,changed)['state']=='completed'
    assert run_git(['show',cfg.project_branch_name+':AGENTS.md'],repo)==RESOLVED.strip()
    assert target.read_text()==BASE and (repo/'.git/index').read_bytes()==index and run_git(['rev-parse','HEAD'],repo)==head


def test_process_exit_after_resolution_write_recovers_one_inverse(conflict,tmp_path):
    import os,subprocess,sys
    from dataclasses import asdict
    cfg,store,target,p,calls=conflict
    submit_command(store,cfg,request(conflict));generated=job_worker.run_once(store,cfg)
    draft=store.query_one('SELECT * FROM proposals WHERE id=?',(generated['result']['proposal_id'],));approve(store,cfg,draft)
    config=tmp_path/'resolution-config.json';config.write_text(json.dumps(asdict(cfg)))
    script=tmp_path/'exit_resolution.py'
    script.write_text("""import json,os,sys
from contextlib import closing
from self_improve.config import Config
from self_improve.store import Store
from self_improve import apply
from self_improve.worker import run_once
cfg=Config(**json.load(open(sys.argv[1])))
def stop(event,*args):
    if event=='file_replaced':os._exit(77)
apply._delivery_checkpoint=stop
with closing(Store(cfg.state_path('state.db'),migrate=False)) as store:run_once(store,cfg)
""")
    child=subprocess.run([sys.executable,str(script),str(config)],cwd=tmp_path,
        env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1])},capture_output=True,text=True,timeout=30)
    assert child.returncode==77,child.stderr
    assert target.read_text()==RESOLVED
    assert store.query("SELECT * FROM proposal_events WHERE event='rolled_back'")==[]
    assert run_once(store,cfg)['state']=='completed'
    assert len(store.query("SELECT * FROM proposal_events WHERE event='rolled_back'"))==1 and len(calls)==1


@pytest.mark.parametrize('with_preview',[False,True])
def test_two_alternatives_for_the_same_inverse_require_selecting_one(conflict,with_preview):
    from self_improve.commands import CommandError
    from self_improve.review import preview_selection
    cfg,store,target,p,calls=conflict;drafts=[]
    for _ in range(2):
        submit_command(store,cfg,request(conflict));generated=job_worker.run_once(store,cfg)
        drafts.append(generated['result']['proposal_id'])
    shown=preview_selection(store,cfg,drafts)
    body={'action':'approve','request_key':new_id(),'members':[
        {'proposal_id':m['proposal_id'],'revision':m['revision']} for m in shown['members']]}
    if with_preview:body['preview_revision']=shown['revision']
    with pytest.raises(CommandError,match='Select one resolution') as error:
        submit_command(store,cfg,body)
    assert error.value.code=='ConflictingResolutions'
    assert not shown['ready'] and shown['targets'][0]['error_code']=='ConflictingResolutions'
    assert target.read_text()==CURRENT
    assert all(store.query_one('SELECT status FROM proposals WHERE id=?',(pid,))['status']=='pending' for pid in drafts)
