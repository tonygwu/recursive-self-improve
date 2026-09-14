"""Recovery requests generate one explicit manual-review proposal, never delivery."""
import json
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import pytest
from self_improve import recovery_jobs, job_worker
from self_improve.commands import CommandError, submit_command, review_snapshot
from self_improve.llm import LLMRunner, _ExecResult
from self_improve.store import Store,new_id
from tests.test_apply import cfg,store
from tests.test_miner_agentic import seed_learning

@pytest.fixture
def env(cfg,store,tmp_path,monkeypatch):
    cfg=replace(cfg,global_claude_md=str(tmp_path/'CLAUDE.md'),codex_global_agents_md=str(tmp_path/'AGENTS.md'),skills_dir=str(tmp_path/'skills'))
    learning=seed_learning(store,rule='Inspect the original request identifier before retrying.')
    Path(cfg.global_claude_md).write_text('# Rules\n\nHuman footer.\n')
    calls=[]
    def execute(self,call_id,stage,model_class,prompt,expect_json,**kwargs):
        assert stage=='propose_recovery';calls.append(prompt)
        source=json.loads(prompt.split('\nFROZEN INPUT\n')[1])
        if source['mode']=='hook':
            value={'supported':True,'explanation':'Block an invented unsafe command.','hook':{'event':'PreToolUse','matcher':'Bash','command':'case "$x" in forbidden) exit 2;; esac','timeout':10}}
        else:
            current=source['base']['content'];old='Human footer.' if 'Human footer.' in current else ''
            value={'supported':True,'explanation':'Put this lesson at the selected destination.','edits':[{'old':old,'new':old+'\n- Inspect the original request identifier before retrying. <!-- si:'+learning['id']+' -->\n'}]}
        return _ExecResult(ok=True,text=json.dumps(value),parsed=value,outcome='ok',provider='claude',model_reported='claude-opus-4-6')
    monkeypatch.setattr(LLMRunner,'_execute',execute)
    return cfg,store,learning,calls

def request(env,mode='correct_target',proposal_ids=()):
    cfg,store,learning,_=env;opts=recovery_jobs.options(store,cfg,learning['id'])
    target=next(t for t in opts['targets'] if t['destination']['target_kind']==('hook' if mode=='hook' else 'global_claude_md'))
    args={'learning_id':learning['id'],'mode':mode,'target_id':target['id'],'proposal_ids':list(proposal_ids)}
    shown=recovery_jobs.preview(store,cfg,**args)
    return {'action':'propose_recovery',**args,'preview_revision':shown['revision'],'request_key':new_id()}

@pytest.mark.parametrize('mode',['correct_target','hook'])
def test_one_call_generates_a_manual_proposal_and_preserves_targets(env,mode):
    cfg,store,learning,calls=env;before=Path(cfg.global_claude_md).read_text()
    body=request(env,mode);assert not calls and not store.query('SELECT * FROM commands')
    command=submit_command(store,cfg,body);assert submit_command(store,cfg,body)==command
    result=job_worker.run_once(store,cfg)
    assert result['state']=='completed',(result['error_code'],result['error_detail'])
    assert len(calls)==1 and sum(result['budget']['consumed'].values())==1
    pid=result['result']['proposal_id'];row=store.query_one('SELECT * FROM proposals WHERE id=?',(pid,))
    assert row['status']=='pending' and row['action']=='recover_rule'
    assert Path(cfg.global_claude_md).read_text()==before and not (Path(cfg.global_claude_md).parent/'settings.json').exists()
    snapshot=review_snapshot(store,pid,cfg)['snapshot'];assert snapshot['recovery']['source']['mode']==mode
    from self_improve.dashboard.queries import review_queue
    queue=review_queue(store,cfg)
    assert queue['count']==1 and queue['families'][0]['proposals'][0]['id']==pid
    from self_improve.execution_policy import automatic_permission
    assert not automatic_permission(store,cfg,{**row,'status':'gated_pass'})['allowed']


def test_changed_target_and_arbitrary_target_are_refused_before_any_call(env):
    cfg,store,learning,calls=env;body=request(env)
    with pytest.raises(CommandError):submit_command(store,cfg,{**body,'target_id':'/arbitrary/path'})
    Path(cfg.global_claude_md).write_text('changed')
    with pytest.raises(CommandError) as error:submit_command(store,cfg,body)
    assert error.value.code=='StaleJobPreview' and not calls


def test_changed_source_during_generation_preserves_existing_decisions(env,monkeypatch):
    cfg,store,learning,calls=env;submit_command(store,cfg,request(env));original=LLMRunner._execute
    def race(self,*args,**kwargs):
        answer=original(self,*args,**kwargs)
        with closing(Store(store.db_path,migrate=False)) as other:
            other.update('learnings','id',learning['id'],{'rule_text':'A newer lesson'});other.commit()
        return answer
    monkeypatch.setattr(LLMRunner,'_execute',race)
    result=job_worker.run_once(store,cfg)
    assert result['error_code']=='RecoverySourceChanged' and not store.query('SELECT * FROM proposals')
    assert len(calls)==1


def test_regeneration_binds_exact_undecided_alternatives(env):
    cfg,store,learning,calls=env;ids=[]
    for i in range(2):
        pid=new_id();ids.append(pid)
        store.insert('proposals',{'id':pid,'learning_id':learning['id'],'target_path':cfg.global_claude_md,'target_kind':'global_claude_md','action':'add','diff_unified':'old alternative '+str(i),'status':'pending','created_at':'2026-01-01T00:00:00Z'})
    store.commit();submit_command(store,cfg,request(env,'regenerate_patch',ids));result=job_worker.run_once(store,cfg)
    assert result['state']=='completed',result
    assert all(store.query_one('SELECT status FROM proposals WHERE id=?',(pid,))['status']=='superseded' for pid in ids)
    assert result['result']['superseded_proposal_ids']==sorted(ids)
    assert len(calls)==1


def test_interruption_after_generation_reuses_the_answer(env,monkeypatch):
    cfg,store,learning,calls=env;submit_command(store,cfg,request(env))
    def stop(event):
        if event=='recovery_generated':raise KeyboardInterrupt()
    monkeypatch.setattr(job_worker,'_checkpoint',stop)
    with pytest.raises(KeyboardInterrupt):job_worker.run_once(store,cfg)
    assert not store.query('SELECT * FROM proposals') and len(calls)==1
    monkeypatch.setattr(job_worker,'_checkpoint',lambda _:None)
    result=job_worker.run_once(store,cfg)
    assert result['state']=='completed' and len(calls)==1


def test_rejected_learning_cannot_be_recovered_under_a_new_target(env):
    cfg,store,learning,calls=env;store.update('learnings','id',learning['id'],{'status':'rejected'});store.commit()
    with pytest.raises(CommandError) as error:request(env)
    assert error.value.code=='LessonRejected' and not calls


def test_generated_proposal_delivers_only_after_manual_approval(env):
    from self_improve.review import preview_selection
    from self_improve import worker
    cfg,store,learning,calls=env;submit_command(store,cfg,request(env));result=job_worker.run_once(store,cfg)
    pid=result['result']['proposal_id'];shown=preview_selection(store,cfg,[pid])
    body={'action':'approve','request_key':new_id(),'members':[{'proposal_id':pid,'revision':shown['members'][0]['revision']}],'preview_revision':shown['revision']}
    submit_command(store,cfg,body);delivered=worker.run_once(store,cfg)
    assert delivered['state']=='completed',delivered
    assert 'Human footer.' in Path(cfg.global_claude_md).read_text() and 'original request identifier' in Path(cfg.global_claude_md).read_text()


def test_preview_with_target_rejections_is_read_only_and_target_scoped(env):
    from tests.test_rejections import proposal,decision
    cfg,store,learning,calls=env
    old=proposal(store,Path(cfg.global_claude_md))
    submit_command(store,cfg,decision(store,cfg,'reject_target',[old]))
    before=store.conn.total_changes
    with closing(Store(store.db_path,read_only=True)) as reader:
        options=recovery_jobs.options(reader,cfg,learning['id'])
        target=next(t for t in options['targets'] if t['destination']['target_kind']=='codex_global')
        recovery_jobs.preview(reader,cfg,learning['id'],'correct_target',target['id'],[])
    assert store.conn.total_changes==before and not calls


def test_hook_preserves_existing_settings_and_commands(env):
    cfg,store,learning,calls=env
    path=Path(cfg.global_claude_md).parent/'settings.json'
    original={'permissions':{'deny':['Read(.secret)']},'hooks':{'PreToolUse':[{'matcher':'Edit','hooks':[{'type':'command','command':'existing-check'}]}]}}
    path.write_text(json.dumps(original)+'\n')
    submit_command(store,cfg,request(env,'hook'));result=job_worker.run_once(store,cfg)
    assert result['state']=='completed',result
    from self_improve.propose import apply_unified_diff
    proposal=store.query_one('SELECT * FROM proposals WHERE id=?',(result['result']['proposal_id'],))
    after=json.loads(apply_unified_diff(path.read_text(),proposal['diff_unified']))
    assert after['permissions']==original['permissions']
    assert after['hooks']['PreToolUse'][:-1]==original['hooks']['PreToolUse']
    assert path.read_text()==json.dumps(original)+'\n'


def test_unsupported_result_retains_reason_and_no_false_proposal(env,monkeypatch):
    cfg,store,learning,calls=env;original=LLMRunner._execute
    def unsupported(self,*args,**kwargs):
        answer=original(self,*args,**kwargs);value={'supported':False,'explanation':'This rule needs human judgment.'}
        return replace(answer,parsed=value,text=json.dumps(value))
    monkeypatch.setattr(LLMRunner,'_execute',unsupported)
    body=request(env,'hook');submit_command(store,cfg,body);result=job_worker.run_once(store,cfg)
    assert result['state']=='completed' and result['result']['supported'] is False and not store.query('SELECT * FROM proposals')
    shown=recovery_jobs.view(store,cfg,**{k:body[k] for k in ('learning_id','mode','target_id','proposal_ids')})
    assert shown['latest_job']['id']==result['id'] and len(calls)==1


def test_cancellation_during_a_call_keeps_the_answer_without_publishing(env,monkeypatch):
    from self_improve.jobs import control
    cfg,store,learning,calls=env;command=submit_command(store,cfg,request(env));original=LLMRunner._execute
    def cancel(self,*args,**kwargs):
        answer=original(self,*args,**kwargs)
        with closing(Store(store.db_path,migrate=False)) as writer:control(writer,{'action':'cancel_job','command_id':command['id'],'request_key':new_id()})
        return answer
    monkeypatch.setattr(LLMRunner,'_execute',cancel)
    result=job_worker.run_once(store,cfg)
    assert result['state']=='cancelled' and len(calls)==1 and not store.query('SELECT * FROM proposals')
    assert result['steps'][0]['state']=='completed'


def test_publication_failure_rolls_back_proposal_and_supersession(env,monkeypatch):
    cfg,store,learning,calls=env;submit_command(store,cfg,request(env));insert=store.insert
    def fail(table,row):
        if table=='proposal_recoveries':raise RuntimeError('publication failed')
        return insert(table,row)
    monkeypatch.setattr(store,'insert',fail);result=job_worker.run_once(store,cfg)
    assert result['state']=='failed' and not store.query('SELECT * FROM proposals') and len(calls)==1
    monkeypatch.setattr(store,'insert',insert)
    from self_improve.jobs import control
    control(store,{'action':'resume_job','command_id':result['id'],'request_key':new_id()})
    result=job_worker.run_once(store,cfg);assert result['state']=='completed' and len(calls)==1


def test_api_preview_and_intent_use_the_selected_database(env,tmp_path):
    from fastapi.testclient import TestClient
    from self_improve.dashboard.app import create_app
    cfg,store,learning,calls=env;copy=tmp_path/'copy.db'
    with closing(Store(copy)) as copied:store.conn.backup(copied.conn)
    with TestClient(create_app(cfg,db_path=copy)) as client:
        opts=client.get(f"/api/learnings/{learning['id']}/recovery-options").json()
        target=next(t for t in opts['targets'] if t['destination']['target_kind']=='global_claude_md')
        args={'mode':'correct_target','target_id':target['id'],'proposal_ids':'[]'}
        shown=client.get(f"/api/learnings/{learning['id']}/recovery-preview",params=args).json()
        body={'action':'propose_recovery','learning_id':learning['id'],**args,'proposal_ids':[], 'request_key':new_id(),'preview_revision':shown['revision']}
        response=client.post('/api/commands',json=body);assert response.status_code==202,response.text
        assert client.post('/api/commands',json=body).json()['id']==response.json()['id']
        assert client.post('/api/commands',json={**body,'target_path':'/arbitrary/path'}).status_code==400
        assert client.get('/api/commands?summary=true').json()['commands'][0]['selection']['learning_id']==learning['id']
    assert not store.query('SELECT * FROM commands') and not calls


def test_missing_recovery_origin_blocks_approved_delivery(env):
    from self_improve.review import preview_selection
    from self_improve import worker
    cfg,store,learning,calls=env;submit_command(store,cfg,request(env));result=job_worker.run_once(store,cfg)
    pid=result['result']['proposal_id'];shown=preview_selection(store,cfg,[pid]);before=Path(cfg.global_claude_md).read_text()
    submit_command(store,cfg,{'action':'approve','request_key':new_id(),'members':[{'proposal_id':pid,'revision':shown['members'][0]['revision']}],'preview_revision':shown['revision']})
    store.conn.execute('DELETE FROM proposal_recoveries WHERE proposal_id=?',(pid,));store.commit()
    delivered=worker.run_once(store,cfg)
    assert delivered['state']=='blocked',delivered
    assert Path(cfg.global_claude_md).read_text()==before


def test_canonical_project_options_use_repo_zero_and_branch_delivery(env,tmp_path):
    from tests.test_apply import init_git_repo,run_git
    from tests.test_miner_agentic import seed_incident
    from self_improve.project_identity import resolve
    cfg,store,learning,calls=env
    base=tmp_path/'project';a=init_git_repo(base/'repo-0','AGENTS.md','original\n');b=init_git_repo(base/'repo-1','AGENTS.md','original\n')
    for root in (a,b):run_git(['remote','add','origin','https://example.invalid/recovery/project.git'],root)
    incident=seed_incident(store,str(tmp_path/'absent.jsonl'),project=str(b))
    key=resolve(str(b)).key;store.conn.execute('UPDATE sessions SET project_key=? WHERE file_path=?',(key,incident['session_file']));store.commit()
    targets=recovery_jobs.options(store,cfg,learning['id'])['targets']
    projects=[t for t in targets if t['destination']['target_kind']=='project_agents_md']
    assert len(projects)==1
    assert projects[0]['destination']['repo_root']==str(a.resolve()) and projects[0]['destination']['mode']=='git_branch'
    assert not calls


def test_actual_process_death_after_generation_reuses_the_answer(env,tmp_path):
    import subprocess,sys
    from dataclasses import asdict
    cfg,store,learning,calls=env;submit_command(store,cfg,request(env))
    config=tmp_path/'config.json';config.write_text(json.dumps(asdict(cfg)));script=tmp_path/'child.py'
    script.write_text('''import json,os,sys
from pathlib import Path
from self_improve.config import Config
from self_improve.store import Store
from self_improve import job_worker
from self_improve.llm import LLMRunner,_ExecResult
cfg=Config(**json.loads(Path(sys.argv[1]).read_text()))
def execute(self,*args,**kwargs):
    value={'supported':True,'explanation':'Use the selected target.','edits':[{'old':'Human footer.','new':'Human footer.\\n- Check the request.'}]}
    return _ExecResult(ok=True,text=json.dumps(value),parsed=value,outcome='ok',provider='claude',model_reported='claude-opus-4-6')
def stop(event):
    if event=='recovery_generated':os._exit(77)
LLMRunner._execute=execute;job_worker._checkpoint=stop
job_worker.run_once(Store(cfg.state_path('state.db'),migrate=False),cfg)
''')
    run=subprocess.run([sys.executable,str(script),str(config)],cwd=tmp_path,capture_output=True,text=True,timeout=30)
    assert run.returncode==77,run.stderr
    assert not store.query('SELECT * FROM proposals') and len(store.query('SELECT * FROM job_calls'))==1
    result=job_worker.run_once(store,cfg)
    assert result['state']=='completed' and not calls and len(store.query('SELECT * FROM job_calls'))==1


@pytest.mark.parametrize('mode',['hook','correct_target'])
def test_recovery_from_review_supersedes_only_its_selected_old_proposal(env,mode):
    cfg,store,learning,calls=env;ids=[]
    for status in ['pending','pending','approved_user','applied']:
        pid=new_id();ids.append(pid)
        store.insert('proposals',{'id':pid,'learning_id':learning['id'],'target_path':cfg.global_claude_md,'target_kind':'global_claude_md','action':'add','diff_unified':'original proposal','status':status,'created_at':'2026-01-01T00:00:00Z'})
    store.commit()
    body=request(env,mode,[ids[0]]);submit_command(store,cfg,body);result=job_worker.run_once(store,cfg)
    assert result['state']=='completed',result
    assert [store.query_one('SELECT status FROM proposals WHERE id=?',(pid,))['status'] for pid in ids]==['superseded','pending','approved_user','applied']
    assert result['result']['superseded_proposal_ids']==[ids[0]]
