"""Explicit paid intent, durable reservations, and restart-safe gate checkpoints."""
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import json

import pytest

from self_improve import jobs,job_worker
from self_improve.commands import CommandError,submit_command,command_status,command_summary
from self_improve.llm import LLMRunner,_ExecResult
from self_improve.store import Store,new_id
from tests.test_apply import cfg,store,insert_proposal,OLD,NEW,make_diff


def synthetic_execute(self,call_id,stage,model_class,prompt,expect_json,**kwargs):
    if stage=='eval_gen':
        value={'title':'Invented formatting check','scenario_prompt':'Write the answer.',
            'workspace_files':{'input.txt':'invented input'},'success_criteria':'A good answer file exists',
            'grader':{'type':'code','check':'test -f good.txt'}}
        return _ExecResult(ok=True,text=json.dumps(value),parsed=value,outcome='ok',provider='codex',model_reported='gpt-5.6-terra',provider_attempts=1)
    sandbox=Path(kwargs['sandbox_dir'])
    if (sandbox/'CLAUDE.md').exists():(sandbox/'good.txt').write_text('good')
    return _ExecResult(ok=True,text='done',parsed=None,outcome='ok',provider='codex',model_reported='gpt-5.6-terra',provider_attempts=1)


@pytest.fixture
def env(cfg,store,tmp_path,monkeypatch):
    cfg=replace(cfg,eval_sandbox_enabled=False,global_claude_md=str(tmp_path/'global.md'),
                codex_global_agents_md=str(tmp_path/'AGENTS.md'),skills_dir=str(tmp_path/'skills'),
                codex_skills_dir=str(tmp_path/'codex-skills'),
                claude_projects_dir=str(tmp_path/'claude-projects'),
                claude_history_path=str(tmp_path/'claude-history.jsonl'),
                codex_sessions_dir=str(tmp_path/'codex-sessions'),
                codex_archived_dir=str(tmp_path/'codex-archive'),
                production_repo_path=str(tmp_path/'production'))
    target=tmp_path/'rule.md';target.write_text(OLD)
    p=insert_proposal(store,target=target,diff=make_diff(OLD,NEW),status='pending')
    calls=[]
    def execute(self,*args,**kwargs):
        calls.append((args[0],args[1]))
        return synthetic_execute(self,*args,**kwargs)
    monkeypatch.setattr(LLMRunner,'_execute',execute)
    return cfg,store,p,calls


def request(env,key=None):
    cfg,store,p,_=env;shown=jobs.preview(store,cfg,p['id'])
    return {'action':'regenerate_eval','request_key':key or new_id(),'proposal_id':p['id'],'preview_revision':shown['revision']}


def test_cost_preview_and_atomic_reservation_run_no_models(env):
    cfg,store,p,calls=env
    shown=jobs.preview(store,cfg,p['id']);assert shown['max_model_calls']==21
    assert shown['plan']['budgets']=={'cheap':0,'strong':0,'gate':21}
    body=request(env);first=submit_command(store,cfg,body)
    assert submit_command(store,cfg,body)==first and not calls
    assert len(store.query('SELECT * FROM commands'))==1
    assert first['budget']['remaining']['gate']==21
    assert store.query('SELECT * FROM runs')==[]
    assert not cfg.state_path('snapshots').exists()
    with pytest.raises(CommandError) as e:submit_command(store,cfg,{**body,'preview_revision':'f'*64})
    assert e.value.code=='IdempotencyConflict'


def test_source_or_cost_change_requires_another_preview(env):
    cfg,store,p,_=env;body=request(env)
    with pytest.raises(CommandError) as e:submit_command(store,replace(cfg,eval_trials=4),body)
    assert e.value.code=='StaleJobPreview' and store.query('SELECT * FROM commands')==[]
    store.update('learnings','id',p['learning_id'],{'why':'a changed reason'});store.commit()
    with pytest.raises(CommandError):submit_command(store,cfg,body)


def test_gate_completes_with_durable_calls_and_no_instruction_delivery(env):
    cfg,store,p,calls=env;queued=submit_command(store,cfg,request(env))
    from self_improve.worker import run_once as delivery_once
    assert delivery_once(store,cfg) is None,'instruction worker claimed a model job'
    result=job_worker.run_once(store,cfg)
    assert result['state']=='completed',(result['error_code'],result['error_detail'],result['result'])
    assert result['result']['verdict']=='gated_pass'
    assert result['budget']['consumed']=={'cheap':0,'strong':0,'gate':len(calls)}
    assert 0<len(calls)<=21 and len(result['calls'])==len(calls)
    assert len(store.query('SELECT * FROM proposal_eval_history'))==1
    assert len(store.query('SELECT * FROM job_evaluations'))==3
    assert Path(p['target_path']).read_text()==OLD and not cfg.state_path('snapshots').exists()
    assert job_worker.run_once(store,cfg) is None
    assert command_summary(command_status(store,queued['id']))['budget']==result['budget']


@pytest.mark.parametrize('restart', [False, True])
def test_failed_gate_runs_every_scenario_within_its_original_reservation(env, monkeypatch, restart):
    cfg, store, p, calls = env
    submit_command(store, cfg, request(env))
    original = LLMRunner._execute

    def fail_trials(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if args[1] == 'grade':
            (Path(kwargs['sandbox_dir']) / 'good.txt').unlink(missing_ok=True)
        return result

    monkeypatch.setattr(LLMRunner, '_execute', fail_trials)
    if restart:
        def interrupt_after_two_scenarios(event):
            if event == 'step_completed' and len(calls) == 14:
                raise KeyboardInterrupt()
        monkeypatch.setattr(job_worker, '_checkpoint', interrupt_after_two_scenarios)
        with pytest.raises(KeyboardInterrupt):
            job_worker.run_once(store, cfg)
        monkeypatch.setattr(job_worker, '_checkpoint', lambda _: None)

    with closing(Store(store.db_path, migrate=False)) as fresh:
        result = job_worker.run_once(fresh, cfg)
    assert result['state'] == 'completed', result
    assert result['result']['verdict'] == 'gated_fail'
    assert len(calls) == len({c[0] for c in calls}) == 21
    assert result['budget']['consumed']['gate'] == result['budget']['maximum']['gate'] == 21
    assert len(store.query('SELECT * FROM runs')) == 1
    evaluations = store.query('SELECT * FROM job_evaluations ORDER BY scenario_index')
    assert [r['scenario_index'] for r in evaluations] == [0, 1, 2]
    assert len({r['eval_result_id'] for r in evaluations}) == 3
    assert len(store.query('SELECT * FROM proposal_eval_history')) == 1
    split = result['result']['gate']['scenario_splits'][0]
    assert split['scenarios_run'] == split['tally']['gated_fail'] == 3
    assert Path(p['target_path']).read_text() == OLD
    assert job_worker.run_once(store, cfg) is None


@pytest.mark.parametrize('initial_status',['approved_user','applied','rejected_user','rolled_back','superseded'])
def test_regeneration_preserves_every_prior_decision(env,initial_status):
    cfg,store,p,_=env;store.update('proposals','id',p['id'],{'status':initial_status});store.commit()
    submit_command(store,cfg,request(env));result=job_worker.run_once(store,cfg)
    assert result['state']=='completed',(result['error_code'],result['error_detail'],result['result'])
    assert store.query_one('SELECT status FROM proposals WHERE id=?',(p['id'],))['status']==initial_status
    assert result['result']['status_update']=='preserved_decision'


def test_restart_between_steps_reuses_results_and_does_not_renew_budget(env,monkeypatch):
    cfg,store,p,calls=env;submit_command(store,cfg,request(env));stopped=[]
    def stop(event):
        if event=='step_completed' and not stopped:stopped.append(1);raise KeyboardInterrupt()
    monkeypatch.setattr(job_worker,'_checkpoint',stop)
    with pytest.raises(KeyboardInterrupt):job_worker.run_once(store,cfg)
    assert len(calls)==1
    monkeypatch.setattr(job_worker,'_checkpoint',lambda _:None)
    with closing(Store(store.db_path,migrate=False)) as fresh:result=job_worker.run_once(fresh,cfg)
    assert result['state']=='completed',(result['error_code'],result['error_detail'],result['result'])
    assert len(calls)==21 and result['budget']['consumed']['gate']==21
    assert len({c[0] for c in calls})==21
    assert len(store.query('SELECT * FROM runs'))==1
    assert len(store.query('SELECT * FROM proposal_eval_history'))==1


def test_unknown_call_after_interruption_is_not_repeated(env,monkeypatch):
    cfg,store,p,calls=env;submit_command(store,cfg,request(env))
    def stop(event):
        if event=='call_reserved':raise KeyboardInterrupt()
    monkeypatch.setattr(job_worker,'_checkpoint',stop)
    with pytest.raises(KeyboardInterrupt):job_worker.run_once(store,cfg)
    assert not calls
    monkeypatch.setattr(job_worker,'_checkpoint',lambda _:None)
    result=job_worker.run_once(store,cfg)
    assert result['state']=='blocked' and result['error_code']=='InterruptedStep'
    assert result['budget']['consumed']['gate']==1 and not calls
    assert store.query('SELECT * FROM proposal_eval_history')==[]
    assert store.query_one('SELECT status FROM proposals WHERE id=?',(p['id'],))['status']=='pending'


def test_cancellation_during_call_preserves_result_and_stops_before_next_call(env,monkeypatch):
    cfg,store,p,calls=env;queued=submit_command(store,cfg,request(env));original=LLMRunner._execute
    def cancel(self,*args,**kwargs):
        result=original(self,*args,**kwargs)
        with closing(Store(store.db_path,migrate=False)) as writer:
            submit_command(writer,cfg,{'action':'cancel_job','request_key':new_id(),'command_id':queued['id']})
        return result
    monkeypatch.setattr(LLMRunner,'_execute',cancel)
    result=job_worker.run_once(store,cfg)
    assert result['state']=='cancelled' and len(calls)==1
    assert result['calls'][0]['state']=='completed'
    assert store.query('SELECT * FROM proposal_eval_history')==[]


def test_gate_refusal_preserves_the_decision_and_reports_no_false_verdict(env,monkeypatch):
    from self_improve import pipeline
    cfg,store,p,calls=env
    original_sandbox=pipeline._ensure_sandbox
    store.update('proposals','id',p['id'],{'status':'applied'});store.commit()
    submit_command(store,cfg,request(env))
    def refuse(*args,**kwargs):raise pipeline.BudgetExhaustedSignal('invented refusal before any trial')
    monkeypatch.setattr(pipeline,'_ensure_sandbox',refuse)
    result=job_worker.run_once(store,cfg)
    assert result['state']=='failed' and result['result']['verdict'] is None
    assert result['failure_taxonomy']=={'gate_budget_exhausted':1}
    assert store.query('SELECT * FROM proposal_eval_history')==[]
    assert store.query("SELECT * FROM proposal_events WHERE event='gated'")==[]
    assert not calls and result['budget']['consumed']['gate']==0
    assert store.query_one('SELECT status FROM proposals WHERE id=?',(p['id'],))['status']=='applied'
    monkeypatch.setattr(pipeline,'_ensure_sandbox',original_sandbox)
    submit_command(store,cfg,{'action':'resume_job','command_id':result['id'],'request_key':new_id()})
    resumed=job_worker.run_once(store,cfg)
    assert resumed['state']=='completed'
    before=resumed['control_history'][0]['before']
    assert before['failure_taxonomy']=={'gate_budget_exhausted':1}
    assert before['result']['verdict'] is None


def test_job_lock_does_not_block_delivery_lane(env):
    cfg,store,p,_=env
    from self_improve.delivery_lock import instruction_write_lock
    with instruction_write_lock(cfg):
        with job_worker.job_lock(cfg):
            with pytest.raises(CommandError) as e:
                with job_worker.job_lock(cfg):pass
            assert e.value.code=='JobWorkerBusy'


def test_cancel_queued_job_replays_without_calls(env):
    cfg,store,p,calls=env;queued=submit_command(store,cfg,request(env))
    body={'action':'cancel_job','request_key':new_id(),'command_id':queued['id']}
    result=submit_command(store,cfg,body)
    assert submit_command(store,cfg,body)==result and result['state']=='cancelled'
    assert job_worker.run_once(store,cfg) is None and not calls


def test_reservation_corruption_fails_before_model_work(env):
    cfg,store,p,calls=env;queued=submit_command(store,cfg,request(env))
    store.conn.execute("UPDATE job_budgets SET maximum=22 WHERE command_id=? AND pool='gate'",(queued['id'],));store.commit()
    with pytest.raises(CommandError) as e:job_worker.run_once(store,cfg)
    assert e.value.code=='JobDataError' and not calls

@pytest.mark.parametrize('boundary',['step_completed','call_reserved','gate_finished'])
def test_process_death_retains_budget_and_completed_evidence(env,tmp_path,boundary):
    import os,subprocess,sys
    from dataclasses import asdict
    cfg,store,p,calls=env;queued=submit_command(store,cfg,request(env))
    config=tmp_path/'job-config.json';config.write_text(json.dumps(asdict(cfg)))
    script=tmp_path/'exit_worker.py'
    script.write_text("""import json,os,sys
from contextlib import closing
from self_improve.config import Config
from self_improve.store import Store
from self_improve.llm import LLMRunner
from self_improve import job_worker
from tests.test_model_jobs import synthetic_execute
cfg=Config(**json.load(open(sys.argv[1])))
LLMRunner._execute=synthetic_execute
def stop(event):
    if event==sys.argv[2]:os._exit(77)
job_worker._checkpoint=stop
with closing(Store(cfg.state_path('state.db'),migrate=False)) as store:job_worker.run_once(store,cfg)
""")
    envvars={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1])}
    child=subprocess.run([sys.executable,str(script),str(config),boundary],cwd=tmp_path,env=envvars,capture_output=True,text=True,timeout=30)
    assert child.returncode==77,child.stderr
    before=len(store.query('SELECT * FROM job_calls'))
    result=job_worker.run_once(store,cfg)
    if boundary=='call_reserved':
        assert result['state']=='blocked' and not result['can_retry'] and not calls
        assert len(store.query('SELECT * FROM job_calls'))==before==1
    else:
        assert result['state']=='completed',(result['error_code'],result['error_detail'])
        assert len(store.query('SELECT * FROM llm_calls'))==21
        assert len(calls)==21-before
        assert len(store.query('SELECT * FROM job_evaluations'))==3
        assert len(store.query('SELECT * FROM proposal_eval_history'))==1


def test_concurrent_duplicate_requests_share_one_reservation(env):
    from concurrent.futures import ThreadPoolExecutor
    cfg,store,p,calls=env;body=request(env)
    def send(_):
        with closing(Store(store.db_path,migrate=False)) as writer:return submit_command(writer,cfg,body)['id']
    with ThreadPoolExecutor(max_workers=2) as pool:ids=list(pool.map(send,range(2)))
    assert ids[0]==ids[1] and not calls
    assert len(store.query('SELECT * FROM job_budgets'))==3


def test_completed_checkpoint_tampering_cannot_invent_a_trial_result(env,monkeypatch):
    cfg,store,p,calls=env;submit_command(store,cfg,request(env));stopped=[]
    def stop(event):
        if event=='step_completed' and len(calls)==2 and not stopped:stopped.append(1);raise KeyboardInterrupt()
    monkeypatch.setattr(job_worker,'_checkpoint',stop)
    with pytest.raises(KeyboardInterrupt):job_worker.run_once(store,cfg)
    step=store.query_one("SELECT * FROM job_steps WHERE step_key LIKE '%without:trial:0'")
    bad=json.loads(step['result_json']);bad['value']['outcome']='pass'
    store.update('job_steps','id',step['id'],{'result_json':json.dumps(bad)});store.commit()
    monkeypatch.setattr(job_worker,'_checkpoint',lambda _:None)
    with pytest.raises(CommandError) as e:job_worker.run_once(store,cfg)
    assert e.value.code=='JobDataError'
    assert len(calls)==2 and store.query('SELECT * FROM proposal_eval_history')==[]


def test_http_preview_and_job_intent_use_the_selected_copy(env,tmp_path):
    pytest.importorskip('fastapi')
    from fastapi.testclient import TestClient
    from self_improve.dashboard.app import create_app
    cfg,store,p,calls=env
    copy=tmp_path/'copy.db'
    with closing(Store(copy)) as copied:store.conn.backup(copied.conn)
    with TestClient(create_app(cfg,db_path=copy)) as client:
        shown=client.get(f"/api/proposals/{p['id']}/eval-preview")
        assert shown.status_code==200,shown.text
        body={'action':'regenerate_eval','proposal_id':p['id'],'request_key':new_id(),'preview_revision':shown.json()['revision']}
        queued=client.post('/api/commands',json=body)
        assert queued.status_code==202,queued.text
        assert client.post('/api/commands',json=body).json()==queued.json()
        assert client.get('/api/commands?summary=true').json()['commands'][0]['budget']['maximum']['gate']==21
    assert not calls and store.query('SELECT * FROM commands')==[]
    with closing(Store(copy,migrate=False)) as copied:
        assert len(copied.query('SELECT * FROM commands'))==1
        with pytest.raises(CommandError) as e:job_worker.run_once(copied,cfg)
        assert e.value.code=='StoreMismatch'

def test_constructor_failure_is_a_recorded_failed_job(env):
    cfg,store,p,calls=env;submit_command(store,cfg,request(env))
    def broken(*args,**kwargs):raise ValueError('invented runner initialization failure')
    result=job_worker.run_once(store,cfg,_llm_factory=broken)
    assert result['state']=='failed' and result['error_code']=='ValueError'
    assert 'initialization' in result['error_detail'] and not calls
    shown=command_summary(result)
    assert shown['error_code']=='ValueError' and shown['error_detail']==result['error_detail']


def test_resuming_a_failed_initialization_reports_the_same_run_as_running(env):
    cfg,store,p,calls=env;queued=submit_command(store,cfg,request(env))
    def broken(*args,**kwargs):raise ValueError('invented initialization failure')
    failed=job_worker.run_once(store,cfg,_llm_factory=broken)
    assert failed['can_retry'] and not calls
    submit_command(store,cfg,{'action':'resume_job','command_id':queued['id'],'request_key':new_id()})
    def inspect(*args,**kwargs):
        run=store.query_one('SELECT * FROM runs WHERE id=?',(failed['run_id'],))
        assert run['status']=='running' and not run['finished'],run
        return LLMRunner(*args,**kwargs)
    completed=job_worker.run_once(store,cfg,_llm_factory=inspect)
    assert completed['state']=='completed',completed['error_detail']
    assert completed['run_id']==failed['run_id'] and len(calls)==21
    assert len(store.query('SELECT * FROM runs'))==1


def test_restart_reuses_a_known_invalid_scenario_without_repaying_it(env,monkeypatch):
    from dataclasses import replace
    cfg,store,p,calls=env;submit_command(store,cfg,request(env));original=LLMRunner._execute;stopped=[]
    def invalid_first(self,*args,**kwargs):
        result=original(self,*args,**kwargs)
        return replace(result,parsed={'invalid':'invented invalid spec'}) if len(calls)==1 else result
    def stop(event):
        if event=='step_completed' and len(calls)==2 and not stopped:stopped.append(1);raise KeyboardInterrupt()
    monkeypatch.setattr(LLMRunner,'_execute',invalid_first);monkeypatch.setattr(job_worker,'_checkpoint',stop)
    with pytest.raises(KeyboardInterrupt):job_worker.run_once(store,cfg)
    monkeypatch.setattr(job_worker,'_checkpoint',lambda _:None)
    result=job_worker.run_once(store,cfg)
    assert result['state']=='completed',(result['error_code'],result['error_detail'])
    assert len(calls)==15 and result['budget']['consumed']['gate']==15
    assert result['failure_taxonomy']['step_SpecError']==1


def test_reload_preview_identifies_the_recorded_job_without_authorizing_another(env):
    cfg,store,p,calls=env;shown=jobs.preview(store,cfg,p['id']);queued=submit_command(store,cfg,request(env))
    reloaded=jobs.preview(store,cfg,p['id'])
    assert reloaded['latest_job']['id']==queued['id']
    assert shown['revision']==reloaded['revision'] and not calls
    assert len(store.query('SELECT * FROM commands'))==1

def test_new_runner_instances_cannot_replenish_the_job_reservation(env,tmp_path):
    from dataclasses import asdict
    from self_improve.llm import BudgetExhausted
    cfg,store,p,calls=env;queued=submit_command(store,cfg,request(env))
    run_id=new_id();store.insert('runs',{'id':run_id,'started':'2026-09-12T00:00:00Z'})
    store.update('model_jobs','command_id',queued['id'],{'run_id':run_id})
    store.update('commands','id',queued['id'],{'state':'running'});store.commit()
    sandbox=tmp_path/'trial';sandbox.mkdir()
    def call(index,stage):
        journal=job_worker.Journal(store,queued['id'])
        runner=LLMRunner(cfg,store,run_id,cfg.state_path('raw'),call_journal=journal)
        return journal.step(str(index),{'stage':stage},lambda:asdict(runner.call(stage,cfg.cheap_model_class,'invented task',False,sandbox_dir=str(sandbox))))
    for i,stage in enumerate(['eval_gen']*3+['grade']*18):call(i,stage)
    with pytest.raises(BudgetExhausted):call(21,'grade')
    assert len(calls)==21 and len(store.query('SELECT * FROM job_calls'))==21

def test_runtime_fingerprint_is_independent_of_source_or_wheel_asset_location(tmp_path,monkeypatch):
    from self_improve import resources
    original=jobs.runtime_revision();root=Path(jobs.__file__).parent;installed=tmp_path/'package'
    import shutil
    shutil.copytree(root,installed,ignore=shutil.ignore_patterns('__pycache__'))
    prompts=installed/'_assets'/'prompts'
    shutil.copytree(resources.bundled_path('prompts'),prompts,dirs_exist_ok=True)
    monkeypatch.setattr(jobs,'__file__',str(installed/'jobs.py'))
    monkeypatch.setattr(resources,'bundled_path',lambda *parts:installed/'_assets'/Path(*parts))
    assert jobs.runtime_revision()==original
