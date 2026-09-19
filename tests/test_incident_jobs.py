"""Selected mining owns one frozen incident, one reservation, and no delivery."""
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import json

import pytest

from self_improve import incident_jobs, jobs, job_worker
from self_improve.commands import CommandError, submit_command
from self_improve.llm import LLMRunner, _ExecResult
from self_improve.store import Store, new_id
from tests.test_apply import cfg, store
from tests.test_miner_agentic import seed_incident, agentic_payload, seed_learning


@pytest.fixture
def env(cfg, store, tmp_path, monkeypatch):
    cfg=replace(cfg,global_claude_md=str(tmp_path/'global.md'),
        codex_global_agents_md=str(tmp_path/'AGENTS.md'),skills_dir=str(tmp_path/'skills'))
    incident=seed_incident(store,str(tmp_path/'deleted-session.jsonl'),project=str(tmp_path))
    calls=[]
    def execute(self,call_id,stage,model_class,prompt,expect_json,**kwargs):
        calls.append({'id':call_id,'stage':stage,'prompt':prompt,'kwargs':kwargs,'cfg':self.cfg})
        payload=agentic_payload()
        if self.cfg.mine_mode=='fast':
            from self_improve.miner import MINE_AGENTIC_EXTRA_KEYS
            payload={k:v for k,v in payload.items() if k not in MINE_AGENTIC_EXTRA_KEYS}
        return _ExecResult(ok=True,text=json.dumps(payload),parsed=payload,outcome='ok',provider='claude',model_reported='claude-sonnet-4-6')
    monkeypatch.setattr(LLMRunner,'_execute',execute)
    return cfg,store,incident,calls


def request(env,key=None):
    cfg,store,incident,_=env;shown=incident_jobs.preview(store,cfg,incident['id'])
    return {'action':'mine_incident','incident_id':incident['id'],'request_key':key or new_id(),'preview_revision':shown['revision']}


def test_explicit_mining_reserves_one_call_and_returns_a_manual_proposal(env):
    cfg,store,incident,calls=env
    before=list(Path(cfg.state_dir).rglob('*'))
    shown=incident_jobs.preview(store,cfg,incident['id'])
    assert shown['max_model_calls']==1 and not calls
    assert list(Path(cfg.state_dir).rglob('*'))==before
    body=request(env);command=submit_command(store,cfg,body)
    assert submit_command(store,cfg,body)==command
    assert store.query('SELECT * FROM runs')==[]
    assert store.query('SELECT * FROM proposals')==[]
    result=job_worker.run_once(store,cfg)
    assert result['state']=='completed',(result['error_code'],result['error_detail'])
    assert len(calls)==1 and sum(result['budget']['consumed'].values())==1
    assert result['budget']['maximum']['gate']==0
    assert result['result']['proposal_ids'],result['result']
    proposal=store.query_one('SELECT * FROM proposals WHERE id=?',(result['result']['proposal_ids'][0],))
    assert proposal['status']=='pending' and not Path(proposal['target_path']).exists()
    from self_improve.execution_policy import automatic_permission,waiting_proposals
    assert proposal['id'] in {p['id'] for p in waiting_proposals(store,cfg)}
    assert not automatic_permission(store,cfg,{**proposal,'status':'gated_pass'})['allowed']
    assert store.query_one('SELECT status FROM incidents WHERE id=?',(incident['id'],))['status']=='mined'
    assert job_worker.run_once(store,cfg) is None


def test_changed_evidence_requires_a_fresh_preview(env):
    cfg,store,incident,calls=env;body=request(env)
    store.update('incidents','id',incident['id'],{'matched_text':'changed evidence'});store.commit()
    with pytest.raises(CommandError) as e:submit_command(store,cfg,body)
    assert e.value.code=='StaleJobPreview' and not calls
    assert not store.query('SELECT * FROM commands')


def test_frozen_transcript_survives_deletion_before_worker_execution(env,tmp_path):
    cfg,store,incident,calls=env
    fixture=Path(__file__).parent/'fixtures/claude/main_cli.jsonl'
    transcript=tmp_path/'session.jsonl';transcript.write_bytes(fixture.read_bytes())
    from self_improve.miner import _source_for
    event=next(e for e in _source_for('claude',cfg).parse(str(transcript),0) if e.text)
    other=seed_incident(store,str(transcript),project=str(tmp_path),ts=event.ts_utc)
    shown=incident_jobs.preview(store,cfg,other['id'])
    assert shown['source']['snapshot']['coverage']['kind']=='full_session'
    submit_command(store,cfg,request((cfg,store,other,calls)))
    transcript.unlink()
    result=job_worker.run_once(store,cfg)
    assert result['state']=='completed',result
    sandbox=Path(calls[0]['kwargs']['cwd'])
    assert (sandbox/'transcript.md').read_text()==shown['source']['snapshot']['files']['transcript.md']
    assert 'aged out' not in (sandbox/'transcript.md').read_text().lower()


def test_restart_after_generation_does_not_repeat_the_call(env,monkeypatch):
    cfg,store,incident,calls=env;submit_command(store,cfg,request(env))
    def stop(event):
        if event=='incident_generated':raise KeyboardInterrupt()
    monkeypatch.setattr(job_worker,'_checkpoint',stop)
    with pytest.raises(KeyboardInterrupt):job_worker.run_once(store,cfg)
    assert len(calls)==1 and not store.query('SELECT * FROM proposals')
    monkeypatch.setattr(job_worker,'_checkpoint',lambda _:None)
    with closing(Store(store.db_path,migrate=False)) as fresh:result=job_worker.run_once(fresh,cfg)
    assert result['state']=='completed' and len(calls)==1
    assert len(store.query('SELECT * FROM incident_learnings'))==1
    history=store.query('SELECT * FROM mining_history')
    assert len(history)==1 and history[0]['command_id']==result['id']
    assert history[0]['run_id']==result['run_id'] and history[0]['call_id']==calls[0]['id']


def test_an_incident_mined_elsewhere_preserves_its_new_state(env,monkeypatch):
    cfg,store,incident,calls=env;submit_command(store,cfg,request(env));execute=LLMRunner._execute
    def race(self,*args,**kwargs):
        result=execute(self,*args,**kwargs)
        with closing(Store(store.db_path,migrate=False)) as writer:
            writer.update('incidents','id',incident['id'],{'status':'dismissed'});writer.commit()
        return result
    monkeypatch.setattr(LLMRunner,'_execute',race)
    result=job_worker.run_once(store,cfg)
    assert result['state']=='failed' and result['error_code']=='IncidentChanged'
    assert not store.query('SELECT * FROM learnings') and not store.query('SELECT * FROM proposals')
    assert store.query_one('SELECT status FROM incidents WHERE id=?',(incident['id'],))['status']=='dismissed'


def test_unknown_interrupted_mining_is_not_repeated(env,monkeypatch):
    cfg,store,incident,calls=env;submit_command(store,cfg,request(env))
    def stop(event):
        if event=='call_reserved':raise KeyboardInterrupt()
    monkeypatch.setattr(job_worker,'_checkpoint',stop)
    with pytest.raises(KeyboardInterrupt):job_worker.run_once(store,cfg)
    monkeypatch.setattr(job_worker,'_checkpoint',lambda _:None)
    result=job_worker.run_once(store,cfg)
    assert result['state']=='blocked' and result['error_code']=='InterruptedStep'
    assert sum(result['budget']['consumed'].values())==1 and not calls


def test_frozen_search_uses_only_the_selected_corpus(env,tmp_path):
    cfg,store,incident,_=env
    target=seed_learning(store,rule='Always inspect the served model identity before trusting an evaluation.')
    shown=incident_jobs.preview(store,cfg,incident['id'])
    corpus=shown['source']['snapshot']['corpus']
    store.update('learnings','id',target['id'],{'rule_text':'unrelated later wording'});store.commit()
    from self_improve.search import search_corpus
    result=search_corpus(cfg,corpus,'inspect served model identity',top_k=10,with_meta=True)
    assert result['results'][0]['rule_text']==target['rule_text']
    assert result['meta']['corpus']['learnings']==1


def test_failed_publication_rolls_back_mining_and_reuses_the_paid_result(env,monkeypatch):
    cfg,store,incident,calls=env;queued=submit_command(store,cfg,request(env));insert=store.insert
    def fail(table,row):
        if table=='proposal_events':raise RuntimeError('invented event write failure')
        return insert(table,row)
    monkeypatch.setattr(store,'insert',fail)
    failed=job_worker.run_once(store,cfg)
    assert failed['state']=='failed' and failed['can_retry'] and len(calls)==1
    assert store.query('SELECT * FROM learnings')==[]
    assert store.query('SELECT * FROM incident_learnings')==[]
    assert store.query('SELECT * FROM proposals')==[]
    assert store.query_one('SELECT status FROM incidents WHERE id=?',(incident['id'],))['status']=='new'
    monkeypatch.setattr(store,'insert',insert)
    submit_command(store,cfg,{'action':'resume_job','command_id':queued['id'],'request_key':new_id()})
    result=job_worker.run_once(store,cfg)
    assert result['state']=='completed' and len(calls)==1
    assert len(result['result']['proposal_ids'])==1


def test_two_requests_for_one_incident_do_not_pay_for_it_twice(env):
    cfg,store,incident,calls=env;body=request(env)
    first=submit_command(store,cfg,body)
    second=submit_command(store,cfg,{**body,'request_key':new_id()})
    assert first['id']!=second['id']
    results=[job_worker.run_once(store,cfg),job_worker.run_once(store,cfg)]
    assert {r['state'] for r in results}=={'completed','failed'}
    assert next(r for r in results if r['state']=='failed')['error_code']=='IncidentChanged'
    assert len(calls)==1 and len(store.query('SELECT * FROM proposals'))==1


def test_cancel_during_mining_retains_the_answer_without_publishing_it(env,monkeypatch):
    cfg,store,incident,calls=env;queued=submit_command(store,cfg,request(env));execute=LLMRunner._execute
    def cancel(self,*args,**kwargs):
        answer=execute(self,*args,**kwargs)
        with closing(Store(store.db_path,migrate=False)) as writer:
            submit_command(writer,cfg,{'action':'cancel_job','command_id':queued['id'],'request_key':new_id()})
        return answer
    monkeypatch.setattr(LLMRunner,'_execute',cancel)
    result=job_worker.run_once(store,cfg)
    assert result['state']=='cancelled' and result['calls'][0]['state']=='completed'
    assert len(calls)==1 and not store.query('SELECT * FROM learnings')
    assert store.query_one('SELECT status FROM incidents WHERE id=?',(incident['id'],))['status']=='new'


def test_fast_mining_uses_the_selected_mode_and_one_call(env):
    cfg,store,incident,calls=env;cfg=replace(cfg,mine_mode='fast')
    submit_command(store,cfg,request((cfg,store,incident,calls)))
    result=job_worker.run_once(store,cfg)
    assert result['state']=='completed' and len(calls)==1
    assert calls[0]['stage']=='mine' and not calls[0]['kwargs']['agentic']
    assert result['result']['proposal_ids']


@pytest.mark.parametrize('shape',['turn','occurrence'])
def test_archived_evidence_and_secret_redaction_reach_the_final_job_files(env,shape):
    cfg,store,incident,calls=env
    token='sk-'+'A'*48
    entry={'ts':incident['ts'],'text':'no, that is wrong '+token}
    if shape=='turn':entry['role']='human'
    else:entry.update(session_file='old.jsonl',count_in_session=7)
    store.update('incidents','id',incident['id'],{'window_json':json.dumps([entry])});store.commit()
    shown=incident_jobs.preview(store,cfg,incident['id'])
    assert token not in json.dumps(shown['source']['snapshot']['files'])
    submit_command(store,cfg,request(env));result=job_worker.run_once(store,cfg)
    assert result['state']=='completed'
    sandbox=Path(calls[0]['kwargs']['cwd']);text=(sandbox/'transcript.md').read_text()
    assert token not in text and '[REDACTED:' in text
    if shape=='occurrence':assert '7x' in text and 'old.jsonl' in text


def test_a_newly_named_copy_of_a_rejected_lesson_stays_suppressed(env):
    cfg,store,incident,calls=env
    seed_learning(store,status='rejected',rule=agentic_payload()['generalized_rule'])
    submit_command(store,cfg,request(env));result=job_worker.run_once(store,cfg)
    assert result['state']=='completed' and not result['result']['proposal_ids']
    assert result['result']['proposal_error']['code']=='LessonRejected'
    assert result['failure_taxonomy']=={'propose_LessonRejected':1}


def test_frozen_search_cli_does_not_open_the_default_database(env,tmp_path):
    cfg,store,incident,calls=env
    seed_learning(store,rule='Check the returned model identity.')
    submit_command(store,cfg,request(env));job_worker.run_once(store,cfg)
    import subprocess,sys
    sandbox=Path(calls[0]['kwargs']['cwd'])
    result=subprocess.run([sys.executable,'-m','self_improve.frozen_search','search-learnings','model identity','--top','1'],cwd=sandbox,capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    result=json.loads(result.stdout)
    assert result['meta']['corpus']['learnings']==1
    assert result['results'][0]['rule_text']=='Check the returned model identity.'


def test_copied_store_preview_pagination_and_intent_never_reopen_live_state(env,tmp_path):
    from fastapi.testclient import TestClient
    from self_improve.dashboard.app import create_app
    cfg,store,incident,calls=env
    for i in range(3):seed_incident(store,str(tmp_path/f'absent-{i}.jsonl'),project=str(tmp_path))
    copy=tmp_path/'copy.db'
    with closing(Store(copy)) as copied:store.conn.backup(copied.conn)
    with TestClient(create_app(cfg,db_path=copy)) as client:
        first=client.get('/api/incidents?limit=2').json();assert first['count']==4 and len(first['items'])==2
        second=client.get('/api/incidents',params={'limit':2,'cursor':first['next_cursor']}).json()
        assert len({r['id'] for r in first['items']+second['items']})==4 and second['next_cursor'] is None
        shown=client.get(f"/api/incidents/{incident['id']}/mining-preview").json()
        assert shown['max_model_calls']==1 and 'source' not in shown
        full=client.get(f"/api/incidents/{incident['id']}/mining-preview?full=true").json()
        assert full['revision']==shown['revision'] and full['source']['snapshot']['files']['transcript.md']
        body={'action':'mine_incident','incident_id':incident['id'],'request_key':new_id(),'preview_revision':shown['revision']}
        queued=client.post('/api/commands',json=body)
        assert queued.status_code==202,queued.text
        assert client.post('/api/commands',json=body).json()==queued.json()
        bad=client.post('/api/commands',json={**body,'target_path':'/arbitrary/path'})
        assert bad.status_code==400
        assert client.get('/api/commands?summary=true').json()['commands'][0]['incident_id']==incident['id']
    assert not store.query('SELECT * FROM commands') and not calls
    copied_cfg=replace(cfg,state_dir=str(tmp_path/'copy-state'))
    # Exercise worker refusal for this copied store and separate state directory.
    with closing(Store(copy,migrate=False)) as copied:
        with pytest.raises(CommandError):job_worker.run_once(copied,copied_cfg)


def test_actual_process_death_after_generation_reuses_the_paid_answer(env,tmp_path):
    import subprocess,sys,os
    from dataclasses import asdict
    cfg,store,incident,calls=env;submit_command(store,cfg,request(env))
    config=tmp_path/'job-config.json';config.write_text(json.dumps(asdict(cfg)))
    script=tmp_path/'mine-worker.py'
    script.write_text('''import json,sys,os
from pathlib import Path
from self_improve.config import Config
from self_improve.store import Store
from self_improve import job_worker
from self_improve.llm import LLMRunner,_ExecResult
cfg=Config(**json.loads(Path(sys.argv[1]).read_text()))
payload=json.loads(sys.argv[2])
def execute(self,*args,**kwargs):
    return _ExecResult(ok=True,text=json.dumps(payload),parsed=payload,outcome='ok',provider='claude',model_reported='claude-sonnet-4-6')
def stop(event):
    if event=='incident_generated':os._exit(77)
LLMRunner._execute=execute
job_worker._checkpoint=stop
job_worker.run_once(Store(cfg.state_path('state.db'),migrate=False),cfg)
''')
    result=subprocess.run([sys.executable,str(script),str(config),json.dumps(agentic_payload())],cwd=tmp_path,capture_output=True,text=True,timeout=30)
    assert result.returncode==77,result.stderr
    assert not store.query('SELECT * FROM learnings') and len(store.query('SELECT * FROM job_calls'))==1
    completed=job_worker.run_once(store,cfg)
    assert completed['state']=='completed' and completed['result']['proposal_ids']
    assert not calls and len(store.query('SELECT * FROM job_calls'))==1


def test_negative_verdict_is_retained_without_a_false_proposal(env,monkeypatch):
    cfg,store,incident,calls=env;original=LLMRunner._execute
    def negative(self,*args,**kwargs):
        answer=original(self,*args,**kwargs);payload=agentic_payload(is_real_learning=False,incident_summary='No durable lesson in this invented event.')
        return replace(answer,parsed=payload,text=json.dumps(payload))
    monkeypatch.setattr(LLMRunner,'_execute',negative)
    submit_command(store,cfg,request(env));result=job_worker.run_once(store,cfg)
    assert result['state']=='completed' and result['result']['outcome']=='dismissed'
    assert result['result']['summary']=='No durable lesson in this invented event.'
    assert not store.query('SELECT * FROM learnings') and not store.query('SELECT * FROM proposals')
    view=incident_jobs.view(store,cfg,incident['id'])
    assert not view['ready'] and view['latest_job']['id']==result['id']


def test_amendment_supersedes_every_undecided_revision_and_preserves_decisions(env,monkeypatch):
    cfg,store,incident,calls=env
    learning=seed_learning(store,status='applied')
    from self_improve.propose import format_bullet
    Path(cfg.global_claude_md).write_text('# Rules\n'+format_bullet(learning)+'\n')
    states=['pending','held','gated_pass','gated_fail','inconclusive','ungated','approved_user','applied']
    ids={}
    for status in states:
        pid=new_id();ids[status]=pid
        store.insert('proposals',{'id':pid,'learning_id':learning['id'],'target_path':cfg.global_claude_md,
            'target_kind':'global_claude','action':'add','diff_unified':'original reviewed bytes','status':status,'created_at':'2026-01-01T00:00:00Z'})
    store.commit();execute=LLMRunner._execute
    def amend(self,*args,**kwargs):
        answer=execute(self,*args,**kwargs)
        payload=agentic_payload(dedup_decision='amend',dedup_target_id=learning['id'],amended_rule_text='Inspect the complete served model identity before accepting a result.',amended_why='Avoid an incorrectly labelled result.')
        return replace(answer,parsed=payload,text=json.dumps(payload))
    monkeypatch.setattr(LLMRunner,'_execute',amend)
    submit_command(store,cfg,request(env));result=job_worker.run_once(store,cfg)
    assert result['state']=='completed' and result['result']['proposal_ids'],result
    for status,pid in ids.items():
        row=store.query_one('SELECT * FROM proposals WHERE id=?',(pid,))
        assert row['status']==(status if status in {'approved_user','applied'} else 'superseded'),(status,row['status'])
        assert row['diff_unified']=='original reviewed bytes'
    assert 'original reviewed bytes' not in store.query_one('SELECT diff_unified FROM proposals WHERE id=?',(result['result']['proposal_ids'][0],))['diff_unified']


def test_selected_job_publishes_one_atomic_queue_processing_receipt(env):
    cfg,store,incident,calls=env
    command=submit_command(store,cfg,request(env))
    result=job_worker.run_once(store,cfg)
    assert result['state']=='completed'
    receipt=store.query_one('SELECT * FROM queue_processing')
    job=store.query_one('SELECT * FROM incident_jobs WHERE command_id=?',(command['id'],))
    assert receipt['command_id']==command['id'] and receipt['run_id']==job['run_id']
    call=store.query_one('SELECT * FROM llm_calls WHERE id=?',(receipt['call_id'],))
    assert call['run_id']==receipt['run_id'] and call['prompt_sha']==receipt['prompt_sha']
    assert job_worker.run_once(store,cfg) is None
    assert len(store.query('SELECT * FROM queue_processing'))==1
