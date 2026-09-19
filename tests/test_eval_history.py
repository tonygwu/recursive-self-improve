"""Complete provenance through real gate/job paths over invented temporary state."""
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import json

import pytest

from self_improve import eval_history as history, job_worker
from self_improve.commands import submit_command
from self_improve.llm import LLMRunner
from self_improve.pipeline import gate_existing_proposal, _make_sandbox_agent_runner
from self_improve.store import Store, new_id
from tests.test_model_jobs import env, cfg, store, request, OLD


def execute(env):
    cfg,store,proposal,calls=env
    queued=submit_command(store,cfg,request(env))
    result=job_worker.run_once(store,cfg)
    page=history.page(store,command_id=queued['id'])
    assert page['count']==1
    return result,history.detail(store,page['records'][0]['id'])


def test_complete_attempt_joins_exact_source_scenarios_arms_and_calls(env):
    cfg,store,p,calls=env
    result,shown=execute(env)
    assert result['state']=='completed',result
    assert shown['state']=='completed' and shown['comparison_type']=='scenario_gate'
    source=shown['source']
    assert source['run_id']==result['run_id'] and source['command_id']==result['id']
    assert source['proposal']['id']==p['id'] and source['source_revision_id']
    assert source['learning']['rule_text']
    assert len(calls)==21
    kinds=[e['kind'] for e in shown['events']]
    assert kinds.count('generation_prompt')==3
    assert kinds.count('specification')==kinds.count('scenario_result')==3
    assert kinds.count('trial_started')==kinds.count('trial_result')==18
    assert kinds.count('call_started')==kinds.count('call_result')==21
    retained={e['data']['call']['id'] for e in shown['events'] if e['kind']=='call_result'}
    assert retained=={row['id'] for row in store.query('SELECT * FROM llm_calls')}
    for scenario in shown['scenarios']:
        assert scenario['comparison']=={'computable':True,'reason':'','pass_rate_delta':1.0}
        assert scenario['arms']['without']['graded_failures']==3
        assert scenario['arms']['with']['valid_passes']==3
        assert scenario['evaluation']['id']
    assert len({s['specification']['spec_revision'] for s in shown['scenarios']})==3
    assert Path(p['target_path']).read_text()==OLD


def test_regeneration_retains_previous_rule_spec_model_and_decision(env,monkeypatch,tmp_path):
    cfg,store,p,calls=env
    _,first=execute(env)
    old=json.loads(json.dumps(first))
    store.update('learnings','id',p['learning_id'],{'rule_text':'A second invented rule revision.'})
    store.update('proposals','id',p['id'],{'status':'rolled_back'});store.commit()
    original=LLMRunner._execute
    from self_improve import pipeline
    prompts=tmp_path/'prompts';prompts.mkdir()
    (prompts/'gen_regression_eval.md').write_text(
        (pipeline.PROMPTS_DIR/'gen_regression_eval.md').read_text()+'\nA revised invented prompt instruction.\n')
    monkeypatch.setattr(pipeline,'PROMPTS_DIR',prompts)
    def revised(self,*args,**kwargs):
        result=original(self,*args,**kwargs)
        if args[1]=='eval_gen':
            spec={**result.parsed,'title':'A second invented specification'}
            return replace(result,parsed=spec,text=json.dumps(spec),model_reported='fixture-model-v2')
        return replace(result,model_reported='fixture-model-v2')
    monkeypatch.setattr(LLMRunner,'_execute',revised)
    result,second=execute(env)
    assert result['result']['status_update']=='preserved_decision'
    assert first['source']['rule_content_hash']!=second['source']['rule_content_hash']
    assert first['source']['source_revision_id']!=second['source']['source_revision_id']
    assert first['scenarios'][0]['specification']['spec_revision']!=second['scenarios'][0]['specification']['spec_revision']
    before_prompt=next(e['data'] for e in first['events'] if e['kind']=='generation_prompt')
    after_prompt=next(e['data'] for e in second['events'] if e['kind']=='generation_prompt')
    assert before_prompt['template_sha']!=after_prompt['template_sha']
    assert history.detail(store,first['source']['id'])==old
    assert all(e['data']['call']['model_reported']=='fixture-model-v2'
               for e in second['events'] if e['kind']=='call_result')
    assert history.page(store,proposal_id=p['id'])['count']==2
    assert Path(p['target_path']).read_text()==OLD


@pytest.mark.parametrize('mode',['unknown_model','different_models','agent_error','retried','unknown_attempt_count'])
def test_unverified_or_failed_arms_cannot_be_reported_as_valid_comparisons(env,monkeypatch,mode):
    original=LLMRunner._execute
    def changed(self,*args,**kwargs):
        result=original(self,*args,**kwargs)
        if args[1]=='grade':
            if mode=='unknown_model':return replace(result,model_reported='')
            if mode=='agent_error':return replace(result,ok=False,outcome='timeout',model_reported='')
            if mode=='retried':return replace(result,provider_attempts=2)
            if mode=='unknown_attempt_count':return replace(result,provider_attempts=None)
            if (Path(kwargs['sandbox_dir'])/'CLAUDE.md').exists():return replace(result,model_reported='fixture-other-model')
        return result
    monkeypatch.setattr(LLMRunner,'_execute',changed)
    _,shown=execute(env)
    for scenario in shown['scenarios']:
        assert not scenario['comparison']['computable']
        assert scenario['comparison']['pass_rate_delta'] is None
        if mode=='unknown_model':
            assert scenario['arms']['with']['observed_passes']==3
            assert scenario['arms']['with']['exclusions']=={'unverified_model':3}
        if mode=='agent_error':
            assert scenario['arms']['without']['graded_failures']==0
            assert scenario['arms']['without']['exclusions']=={'agent_error':3}
            assert scenario['arms']['with']['skipped']['reason']=='without_arm_uninformative'


def test_manual_budget_refusal_retains_attempt_and_actual_run_without_calls(env):
    cfg,store,p,calls=env
    stats=gate_existing_proposal(replace(cfg,max_gate_calls_per_run=0),store,p['id'])
    rows=history.page(store,proposal_id=p['id'])['records'];assert len(rows)==1
    shown=history.detail(store,rows[0]['id'])
    assert shown['source']['run_id'] and shown['source']['run_id']!=p.get('run_id')
    assert shown['source']['command_id']==''
    assert shown['state']=='stopped' and shown['result'] is None
    assert [e['data']['code'] for e in shown['events']]==['BudgetExhaustedSignal']
    assert stats['gate']['refused']==1 and not calls


def test_invalid_generation_preserves_each_call_and_failure(env,monkeypatch):
    original=LLMRunner._execute
    monkeypatch.setattr(LLMRunner,'_execute',lambda self,*a,**kw:replace(original(self,*a,**kw),parsed={'invalid':'fixture'}))
    result,shown=execute(env)
    assert result['state']=='failed'
    assert len([e for e in shown['events'] if e['kind']=='generation_failed'])==3
    assert len([e for e in shown['events'] if e['kind']=='call_result'])==3
    assert shown['state']=='stopped' and shown['result'] is None
    assert all(s['specification'] is None for s in shown['scenarios'])


def test_restart_after_complete_gate_preserves_original_events_and_call_ids(env,monkeypatch):
    cfg,store,p,calls=env;queued=submit_command(store,cfg,request(env))
    def die(event):
        if event=='gate_finished':raise KeyboardInterrupt()
    monkeypatch.setattr(job_worker,'_checkpoint',die)
    with pytest.raises(KeyboardInterrupt):job_worker.run_once(store,cfg)
    attempt=history.page(store,command_id=queued['id'])['records'][0]
    before=history.detail(store,attempt['id'])
    assert before['state']=='completed' and len(calls)==21
    monkeypatch.setattr(job_worker,'_checkpoint',lambda _:None)
    with closing(Store(store.db_path,migrate=False)) as fresh:result=job_worker.run_once(fresh,cfg)
    assert result['state']=='completed',result
    assert history.detail(store,attempt['id'])==before
    assert len(calls)==21


def test_interrupted_call_is_explicit_and_never_repeated(env,monkeypatch):
    cfg,store,p,calls=env;queued=submit_command(store,cfg,request(env))
    def die(*args,**kwargs):raise KeyboardInterrupt()
    monkeypatch.setattr(LLMRunner,'_execute',die)
    with pytest.raises(KeyboardInterrupt):job_worker.run_once(store,cfg)
    attempt=history.page(store,command_id=queued['id'])['records'][0]
    shown=history.detail(store,attempt['id'])
    assert [e['kind'] for e in shown['events']].count('call_started')==1
    assert not any(e['kind']=='call_result' for e in shown['events'])
    assert shown['result'] is None
    result=job_worker.run_once(store,cfg)
    assert result['state']=='blocked' and result['error_code']=='InterruptedStep'
    assert not calls and store.query('SELECT * FROM llm_calls')==[]


def test_call_reservation_and_its_history_link_survive_the_same_interrupt(env,monkeypatch):
    cfg,store,p,calls=env;queued=submit_command(store,cfg,request(env))
    def die(event):
        if event=='call_reserved':raise KeyboardInterrupt()
    monkeypatch.setattr(job_worker,'_checkpoint',die)
    with pytest.raises(KeyboardInterrupt):job_worker.run_once(store,cfg)
    attempt=history.page(store,command_id=queued['id'])['records'][0]
    shown=history.detail(store,attempt['id'])
    reserved=store.query('SELECT * FROM job_calls');assert len(reserved)==1
    starts=[e for e in shown['events'] if e['kind']=='call_started']
    assert len(starts)==1 and starts[0]['data']['call_id']==reserved[0]['id']
    assert not calls and store.query('SELECT * FROM llm_calls')==[]
    monkeypatch.setattr(job_worker,'_checkpoint',lambda _:None)
    result=job_worker.run_once(store,cfg)
    assert result['state']=='blocked' and result['error_code']=='InterruptedStep'
    assert not calls


def test_result_and_history_link_publish_atomically_then_resume_without_duplicate_calls(env,monkeypatch):
    cfg,store,p,calls=env;queued=submit_command(store,cfg,request(env))
    original=history.Recorder.link_result
    def fail(self,*args):
        original(self,*args)
        raise ValueError('invented failure after the history link')
    monkeypatch.setattr(history.Recorder,'link_result',fail)
    failed=job_worker.run_once(store,cfg)
    assert failed['state']=='failed' and len(calls)==7
    assert store.query('SELECT * FROM eval_results')==[]
    assert store.query('SELECT * FROM job_evaluations')==[]
    assert store.query("SELECT * FROM eval_attempt_events WHERE kind='scenario_result'")==[]
    monkeypatch.setattr(history.Recorder,'link_result',original)
    submit_command(store,cfg,{'action':'resume_job','command_id':queued['id'],'request_key':new_id()})
    done=job_worker.run_once(store,cfg)
    assert done['state']=='completed',done
    assert len(calls)==len({c[0] for c in calls})==21
    assert len(store.query('SELECT * FROM eval_attempts'))==1
    assert len(store.query('SELECT * FROM eval_results'))==3


def test_pagination_is_complete_and_bound_to_its_selector(env):
    cfg,store,p,calls=env
    learning=store.query_one('SELECT * FROM learnings WHERE id=?',(p['learning_id'],))
    for _ in range(23):history.begin(store,cfg,learning,p)
    first=history.page(store,proposal_id=p['id']);assert len(first['records'])==20
    second=history.page(store,proposal_id=p['id'],cursor=first['next_cursor'])
    assert len(second['records'])==3 and second['next_cursor'] is None
    assert len({r['id'] for r in first['records']+second['records']})==23
    with pytest.raises(history.EvalHistoryRequestError):history.page(store,proposal_id='other',cursor=first['next_cursor'])
    with pytest.raises(history.EvalHistoryRequestError):history.page(store,limit=0)


def test_caller_pending_work_is_not_committed_by_history(env):
    cfg,store,p,calls=env
    learning=store.query_one('SELECT * FROM learnings WHERE id=?',(p['learning_id'],))
    store.update('learnings','id',learning['id'],{'title':'uncommitted caller work'})
    with pytest.raises(RuntimeError,match='pending transaction'):history.begin(store,cfg,learning,p)
    with closing(Store(store.db_path,read_only=True)) as reader:
        assert reader.query_one('SELECT title FROM learnings WHERE id=?',(learning['id'],))['title']!='uncommitted caller work'
        assert reader.query('SELECT * FROM eval_attempts')==[]
    store.conn.rollback()


def test_readers_use_selected_store_and_report_corruption(env,tmp_path):
    from fastapi.testclient import TestClient
    from self_improve.dashboard.app import create_app
    cfg,store,p,calls=env;_,shown=execute(env)
    copy=tmp_path/'copied.db'
    with closing(Store(copy)) as copied:store.conn.backup(copied.conn)
    prior_calls=len(calls)
    with TestClient(create_app(cfg,db_path=copy)) as client:
        page=client.get('/api/eval-attempts',params={'proposal_id':p['id']})
        assert page.status_code==200 and page.json()['count']==1
        response=client.get('/api/eval-attempts/'+shown['source']['id'])
        assert response.status_code==200
        assert {k:v for k,v in response.json().items() if k != "summary"}==shown
        assert response.json()["summary"]["id"]==shown["source"]["id"]
        assert client.get('/api/eval-attempts',params={'limit':0}).status_code==400
        assert client.get('/api/eval-attempts/missing').status_code==404
    with closing(Store(copy,migrate=False)) as copied:
        copied.update('eval_attempts','id',shown['source']['id'],{'record_json':'[]'});copied.commit()
    with TestClient(create_app(cfg,db_path=copy)) as client:
        response=client.get('/api/eval-attempts/'+shown['source']['id'])
        assert response.status_code==500 and shown['source']['id'] in response.json()['detail']
    assert history.detail(store,shown['source']['id'])==shown and len(calls)==prior_calls


def test_old_schema_is_unknown_and_missing_installed_table_is_an_error(env):
    _,store,_,_=env
    store.conn.execute('DROP TABLE eval_attempt_events');store.commit()
    with pytest.raises(history.EvalHistoryError,match='missing table'):history.page(store)
    store.conn.execute('DELETE FROM schema_migrations WHERE name=?',(history.MIGRATION,));store.commit()
    with closing(Store(store.db_path,read_only=True)) as reader:
        assert history.page(reader)['count'] is None
        assert not history.page(reader)['computable']
        assert not reader.query_one('SELECT name FROM schema_migrations WHERE name=?',(history.MIGRATION,))


def test_without_rule_rerun_retains_spec_and_is_never_a_paired_experiment(env,tmp_path):
    from self_improve.evals import ab
    import yaml
    cfg,store,p,calls=env
    store.update('learnings','id',p['learning_id'],{'status':'applied'})
    store.update('proposals','id',p['id'],{'status':'applied','applied_at':'2026-09-01T00:00:00Z'})
    rid=new_id();store.insert('runs',{'id':rid,'started':'2026-09-02T00:00:00Z'});store.commit()
    specs=tmp_path/'specs';specs.mkdir()
    spec={'id':p['learning_id'],'title':'Invented rerun','scenario_prompt':'Write the answer.',
          'workspace_files':{'input.txt':'invented'},'success_criteria':'good.txt exists',
          'grader':{'type':'code','check':'test -f good.txt'}}
    (specs/(p['learning_id']+'.yaml')).write_text(yaml.safe_dump(spec))
    llm=LLMRunner(cfg,store,rid,tmp_path/'raw')
    result=ab.rerun_applied(store,cfg,_make_sandbox_agent_runner(llm,cfg),specs,
                            work_dir=tmp_path/'trials',run_id=rid)
    assert result['still_needed']==1
    attempt=history.page(store,run_id=rid)['records'][0]
    shown=history.detail(store,attempt['id'])
    assert shown['comparison_type']=='without_rule_only'
    assert shown['scenarios'][0]['comparison']['reason']=='without_rule_only_rerun'
    assert shown['scenarios'][0]['arms']['with']['attempted']==0
    assert shown['scenarios'][0]['arms']['without']['valid_trials']==3
    (specs/(p['learning_id']+'.yaml')).unlink()
    assert history.detail(store,attempt['id'])==shown


def test_manual_gate_has_all_explicit_run_links_without_a_job(env):
    from self_improve.dashboard import run_data
    cfg,store,p,calls=env
    gate_existing_proposal(cfg,store,p['id'])
    attempt=history.page(store,proposal_id=p['id'])['records'][0]
    assert attempt['command_id']=='' and attempt['run_id']
    shown=history.detail(store,attempt['id']);assert shown['state']=='completed'
    links=history.result_links(store,run_id=attempt['run_id'])
    assert {r['scenario_index'] for r in links}=={0,1,2}
    records=run_data.records(store,attempt['run_id'],kind='evaluations')
    assert len(records['records'])==3
    assert all(any(link['kind']=='eval_attempts' for link in r['links']) for r in records['records'])
    assert len(calls)==21


@pytest.mark.parametrize('mode',['manual','job'])
def test_initialization_failure_has_frozen_attempt_and_no_model_call(env,mode):
    cfg,store,p,calls=env
    def broken(*args,**kwargs):raise ValueError('invented initialization failure')
    if mode=='manual':
        with pytest.raises(ValueError,match='initialization'):
            gate_existing_proposal(cfg,store,p['id'],_llm_factory=broken)
    else:
        submit_command(store,cfg,request(env))
        assert job_worker.run_once(store,cfg,_llm_factory=broken)['state']=='failed'
    attempt=history.page(store,proposal_id=p['id'])['records'][0]
    shown=history.detail(store,attempt['id'])
    assert shown['state']=='stopped' and shown['result'] is None
    assert shown['events'][0]['data']=={'code':'ValueError','detail':'invented initialization failure'}
    assert not calls


def test_call_result_and_history_are_one_transaction(env,monkeypatch):
    cfg,store,p,calls=env
    original=history.CallTrace.finish
    def broken(self,*args,**kwargs):
        original(self,*args,**kwargs)
        raise ValueError('invented call publication failure')
    monkeypatch.setattr(history.CallTrace,'finish',broken)
    result,shown=execute(env)
    assert result['state']=='failed' and len(calls)==3
    assert store.query('SELECT * FROM llm_calls')==[]
    assert store.query("SELECT * FROM eval_attempt_events WHERE kind='call_result'")==[]
    assert len([e for e in shown['events'] if e['kind']=='call_started'])==3
    assert all(c['state']=='started' for c in store.query('SELECT * FROM job_calls'))


def test_same_timestamp_events_use_explicit_links_not_uuid_order(env,monkeypatch):
    monkeypatch.setattr(history,'utc_now_iso',lambda:'2026-09-02T00:00:00Z')
    result,shown=execute(env)
    assert result['state']=='completed' and shown['state']=='completed'
    assert all(s['comparison']['computable'] for s in shown['scenarios'])


def test_mutated_call_or_spec_cannot_relabel_retained_history(env):
    _,store,_,_=env;_,shown=execute(env)
    event=next(e for e in shown['events'] if e['kind']=='call_result')
    store.update('llm_calls','id',event['data']['call']['id'],{'model_reported':'changed model'});store.commit()
    with pytest.raises(history.EvalHistoryError,match='differs from its recorded execution'):
        history.detail(store,shown['source']['id'])


def test_rebuild_preserves_attempt_and_current_source(env,tmp_path):
    from self_improve.rebuild import rebuild_state
    cfg,store,p,_=env
    learning=store.query_one('SELECT * FROM learnings WHERE id=?',(p['learning_id'],))
    attempt=history.begin(store,cfg,learning,p)
    before=history.detail(store,attempt.id)
    backup=tmp_path/'rebuild-backup'
    rebuild_state(store,export_path=backup)
    assert history.detail(store,attempt.id)==before and backup.exists()
    assert store.query_one('SELECT id FROM proposals WHERE id=?',(p['id'],))
    assert json.loads((backup/'preserved.json').read_text())['execution_history']['eval_attempts']
