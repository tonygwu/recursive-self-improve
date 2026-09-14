"""Mining provenance follows the actual producing call and survives amendments."""
from dataclasses import replace
from pathlib import Path
import json

import pytest

from self_improve import miner, mining_history, pipeline
from self_improve.llm import LLMRunner, _ExecResult
from self_improve.store import Store, new_id
from tests.test_apply import cfg, store
from tests.test_miner_agentic import agentic_payload, seed_incident, seed_learning, PROMPTS_DIR


def mine(store,cfg,incident,payload,mode,run_id,tmp_path,monkeypatch):
    cfg=replace(cfg,global_claude_md=str(tmp_path/'global.md'),codex_global_agents_md=str(tmp_path/'AGENTS.md'),skills_dir=str(tmp_path/'skills'))
    window=json.loads(incident['window_json'])
    for event in window:event['ts']=incident['ts'];event['ts_utc']=incident['ts']
    incident={**incident,'window_json':json.dumps(window)}
    store.update('incidents','id',incident['id'],{'window_json':incident['window_json']});store.commit()
    stage='mine_agentic' if mode=='agentic' else 'mine'
    if mode=='fast':payload={k:v for k,v in payload.items() if k not in miner.MINE_AGENTIC_EXTRA_KEYS}
    def execute(self,*args,**kwargs):
        return _ExecResult(ok=True,text=json.dumps(payload),parsed=payload,outcome='ok',provider='claude',model_reported='claude-sonnet-4-6')
    monkeypatch.setattr(LLMRunner,'_execute',execute)
    llm=LLMRunner(cfg,store,run_id,tmp_path/'raw')
    provenance={'run_id':run_id}
    if mode=='agentic':
        return miner.mine_incident_agentic(store,lambda p,s:pipeline._llm_agentic_json(llm,stage,cfg.cheap_model_class,p,s,provenance=provenance),incident,cfg,PROMPTS_DIR,tmp_path/'sandboxes',provenance=provenance)
    return miner.mine_incident(store,lambda p:pipeline._llm_json(llm,stage,cfg.cheap_model_class,p,provenance=provenance),incident,cfg,PROMPTS_DIR,provenance=provenance)


@pytest.mark.parametrize('mode',['fast','agentic'])
def test_actual_mining_call_is_distinct_from_old_scan_and_evidence_time(cfg,store,tmp_path,monkeypatch,mode):
    incident=seed_incident(store,str(tmp_path/'aged-out.jsonl'),project=str(tmp_path),ts='2001-01-01T00:00:00Z')
    store.update('incidents','id',incident['id'],{'run_id':'old-scan'});store.commit()
    incident=store.query_one('SELECT * FROM incidents WHERE id=?',(incident['id'],))
    learning=mine(store,cfg,incident,agentic_payload(),mode,'actual-mining-run',tmp_path,monkeypatch)
    record=mining_history.page(store,learning['id'])['records'][0]
    call=store.query_one('SELECT * FROM llm_calls')
    assert record['run_id']=='actual-mining-run' and record['run_id']!=incident['run_id']
    assert record['generation']==mode and record['call']['id']==call['id']
    assert record['call']['model_reported']=='claude-sonnet-4-6'
    assert record['prompt_sha']==call['prompt_sha'] and len(record['template_sha'])==64
    assert record['incidents'][0]['ts']=='2001-01-01T00:00:00Z'
    assert record['created_at']>record['incidents'][0]['ts']
    assert record['after']['rule_text']==learning['rule_text']


def test_amendment_retains_prior_content_and_duplicate_does_not_claim_authorship(cfg,store,tmp_path,monkeypatch):
    first=seed_incident(store,str(tmp_path/'first.jsonl'),project=str(tmp_path))
    learning=mine(store,cfg,first,agentic_payload(),'fast','run-fast',tmp_path,monkeypatch)
    second=seed_incident(store,str(tmp_path/'second.jsonl'),project=str(tmp_path),ts='2000-01-01T00:00:00Z')
    amended=agentic_payload(dedup_decision='amend',dedup_target_id=learning['id'],amended_rule_text='Inspect the reported model before accepting a result.',amended_why='Reject substitutions.')
    mine(store,cfg,second,amended,'agentic','run-amend',tmp_path,monkeypatch)
    third=seed_incident(store,str(tmp_path/'third.jsonl'),project=str(tmp_path))
    mine(store,cfg,third,agentic_payload(dedup_decision='duplicate',dedup_target_id=learning['id']),'agentic','run-evidence',tmp_path,monkeypatch)
    records=mining_history.page(store,learning['id'])['records']
    assert [r['kind'] for r in records]==['duplicate','amend','new']
    assert records[1]['before']['rule_text']==learning['rule_text']
    current=store.query_one('SELECT * FROM learnings WHERE id=?',(learning['id'],))
    summary=mining_history.summary(store,current)
    assert summary['value']=='agentic' and summary['run_id']=='run-amend'
    assert summary['history_count']==3


def test_legacy_or_unrecorded_changed_content_stays_unknown(store):
    learning=seed_learning(store)
    summary=mining_history.summary(store,learning)
    assert not summary['computable'] and 'recorded mining' in summary['reason']
    assert mining_history.page(store,learning['id'])['records']==[]


@pytest.mark.parametrize('broken',['{bad','[]','{"version":1,"value":NaN}'])
def test_corrupt_history_fails_with_its_owner_instead_of_inventing_generation(cfg,store,tmp_path,monkeypatch,broken):
    incident=seed_incident(store,str(tmp_path/'one.jsonl'),project=str(tmp_path))
    learning=mine(store,cfg,incident,agentic_payload(),'fast','run',tmp_path,monkeypatch)
    row=store.query_one('SELECT * FROM mining_history')
    store.update('mining_history','id',row['id'],{'record_json':broken});store.commit()
    with pytest.raises(mining_history.HistoryError,match=row['id']):mining_history.page(store,learning['id'])


def test_failed_history_insert_rolls_back_content_but_retains_the_paid_call(cfg,store,tmp_path,monkeypatch):
    incident=seed_incident(store,str(tmp_path/'one.jsonl'),project=str(tmp_path))
    original=mining_history.append
    def fail(*args,**kwargs):
        original(*args,**kwargs)
        raise RuntimeError('invented history failure')
    monkeypatch.setattr(mining_history,'append',fail)
    with pytest.raises(RuntimeError,match='invented history failure'):
        mine(store,cfg,incident,agentic_payload(),'fast','run',tmp_path,monkeypatch)
    assert store.query('SELECT * FROM learnings')==[]
    assert store.query('SELECT * FROM mining_history')==[]
    assert store.query_one('SELECT status FROM incidents WHERE id=?',(incident['id'],))['status']=='new'
    assert len(store.query('SELECT * FROM llm_calls'))==1


def test_a_different_call_cannot_be_claimed_as_this_content_source(cfg,store,tmp_path,monkeypatch):
    incident=seed_incident(store,str(tmp_path/'one.jsonl'),project=str(tmp_path))
    learning=mine(store,cfg,incident,agentic_payload(),'fast','run',tmp_path,monkeypatch)
    record=mining_history.page(store,learning['id'])['records'][0]
    wrong={k:record[k] for k in ('generation','stage','run_id','call_id','prompt_sha','template_sha','code_sha')}
    wrong['run_id']='different-run'
    with pytest.raises(mining_history.HistoryError,match='producing run'):
        mining_history.append(store,learning,before=learning,kind='amend',incidents=[incident],provenance=wrong)
    assert len(store.query('SELECT * FROM mining_history'))==1


def test_pagination_keeps_unknown_and_changed_content_honest_in_a_copied_api(cfg,store,tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from self_improve.dashboard.app import create_app
    incident=seed_incident(store,str(tmp_path/'one.jsonl'),project=str(tmp_path))
    learning=mine(store,cfg,incident,agentic_payload(),'fast','run',tmp_path,monkeypatch)
    old=mining_history.page(store,learning['id'])['records'][0]
    for _ in range(3):mining_history.append(store,learning,before=learning,kind='duplicate',incidents=[incident])
    store.commit()
    copy=tmp_path/'copy.db'
    copied=Store(copy);store.conn.backup(copied.conn);copied.close()
    store.update('learnings','id',learning['id'],{'rule_text':'unrecorded edit'});store.commit()
    current=store.query_one('SELECT * FROM learnings WHERE id=?',(learning['id'],))
    assert not mining_history.summary(store,current)['computable']
    with TestClient(create_app(cfg,db_path=copy)) as client:
        first=client.get(f"/api/learnings/{learning['id']}/mining-history?limit=2").json()
        second=client.get(f"/api/learnings/{learning['id']}/mining-history",params={'limit':2,'cursor':first['next_cursor']}).json()
        assert len({r['id'] for r in first['records']+second['records']})==4 and second['next_cursor'] is None
        assert second['records'][-1]['id']==old['id']
        assert client.get('/api/learnings/missing/mining-history').status_code==404
    assert len(store.query('SELECT * FROM llm_calls'))==1 and not store.query('SELECT * FROM commands')


def test_pipeline_merges_name_the_second_producing_call(tmp_path,monkeypatch):
    from tests.e2e_corpus import build_corpus
    corpus=build_corpus(tmp_path)
    cfg=replace(corpus.cfg,mine_mode='fast',cluster_group_cosine=-1.0)
    payload={k:v for k,v in agentic_payload().items() if k not in miner.MINE_AGENTIC_EXTRA_KEYS}
    def execute(self,call_id,stage,*args,**kwargs):
        value=payload if stage=='mine' else {'decision':'merge','generalized_rule':'Inspect the actual model and preserve its call identifier.','why':'Keep the producing evidence.'}
        if stage not in {'mine','cluster'}:return _ExecResult.fail('parse_failure')
        return _ExecResult(ok=True,text=json.dumps(value),parsed=value,outcome='ok',provider='claude',model_reported='claude-sonnet-4-6' if stage=='mine' else 'claude-opus-4-6')
    monkeypatch.setattr(LLMRunner,'_execute',execute)
    stats=pipeline.run_pipeline(cfg,corpus.store,review_only=True)
    assert stats['cluster']['merge_succeeded']>=1,stats
    row=corpus.store.query_one("SELECT * FROM mining_history WHERE kind='cluster_merge'")
    record=mining_history.read(row)
    assert record['call']['stage']=='cluster' and record['call']['model_reported']=='claude-opus-4-6'
    assert len(record['source_learnings'])>=2 and record['incidents']
    current=corpus.store.query_one('SELECT * FROM learnings WHERE id=?',(row['learning_id'],))
    assert mining_history.summary(corpus.store,current)['call_id']==record['call_id']


def test_template_revision_is_the_text_used_to_render_the_call(cfg,store,tmp_path,monkeypatch):
    import hashlib,sys
    templates=tmp_path/'prompts';templates.mkdir()
    text=(PROMPTS_DIR/'mine_incident.md').read_text()
    (templates/'mine_incident.md').write_text(text)
    monkeypatch.setattr(sys.modules[__name__],'PROMPTS_DIR',templates)
    original=miner.render_prompt
    def render_then_edit(path,mapping,**kwargs):
        result=original(path,mapping,**kwargs)
        Path(path).write_text(text+'\nChanged after rendering.\n')
        return result
    monkeypatch.setattr(miner,'render_prompt',render_then_edit)
    incident=seed_incident(store,str(tmp_path/'one.jsonl'),project=str(tmp_path))
    learning=mine(store,cfg,incident,agentic_payload(),'fast','run',tmp_path,monkeypatch)
    record=mining_history.page(store,learning['id'])['records'][0]
    assert record['template_sha']==hashlib.sha256(text.encode()).hexdigest()


def test_rebuild_exports_mining_history_before_removing_derived_rows(cfg,store,tmp_path,monkeypatch):
    from self_improve.rebuild import rebuild_state
    from self_improve.data_boundary import verify_backup
    incident=seed_incident(store,str(tmp_path/'one.jsonl'),project=str(tmp_path))
    mine(store,cfg,incident,agentic_payload(),'fast','run',tmp_path,monkeypatch)
    retained=store.query('SELECT * FROM mining_history')
    destination=tmp_path/'private-backup'
    rebuild_state(store,export_path=destination)
    verify_backup(destination)
    assert json.loads((destination/'preserved.json').read_text())['mining_history']==retained
    assert store.query('SELECT * FROM mining_history')==[]
