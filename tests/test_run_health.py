"""Real budget wrappers, producer health, and retained exact-run report evidence."""
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import json

import pytest

from self_improve.config import Config
from self_improve.dashboard import run_data
from self_improve.llm import LLMRunner, _ExecResult
from self_improve.pipeline import derive_run_status, run_pipeline
from self_improve.report import generate, ReportError
from self_improve.store import Store
from tests.e2e_corpus import build_corpus, mine_payload
from tests.test_run_flow import seed_flow


def health_case(root, case, monkeypatch, *, corpus=None):
    """The real LLMRunner enforces caps; only provider execution is scripted."""
    corpus=corpus or build_corpus(root)
    cap={'refused':0,'failure':1,'success':1,'mixed':2,'all_failed':50}[case]
    calls=[]
    def execute(self, call_id, stage, *args, **kwargs):
        assert stage=='mine_agentic',stage
        calls.append(call_id)
        if case in ('failure','all_failed') or case=='mixed' and len(calls)==1:
            return _ExecResult.fail('spawn_error',error='Invented process launch failure',provider='claude')
        payload=mine_payload('Invented non-learning',is_real_learning=False)
        return _ExecResult(ok=True,text=json.dumps(payload),parsed=payload,outcome='ok',
                          provider='claude',model_reported='claude-sonnet-4-6')
    monkeypatch.setattr(LLMRunner,'_execute',execute)
    cfg=replace(corpus.cfg,mine_mode='agentic',max_cheap_calls_per_run=cap,
                codex_skills_dir=str(root/'codex-skills'),production_repo_path=str(root/'production'))
    try:
        stats=run_pipeline(cfg,corpus.store,review_only=True)
        return corpus,cfg,stats,calls
    except BaseException:
        corpus.store.close()
        raise


@pytest.mark.parametrize('case,status',[
    ('refused','ok'),('failure','degraded'),('success','ok'),('mixed','ok'),('all_failed','degraded')])
def test_actual_pipeline_budget_refusal_never_becomes_execution_failure(tmp_path,monkeypatch,case,status):
    corpus,cfg,stats,calls=health_case(tmp_path,case,monkeypatch)
    try:
        store=corpus.store
        run=store.query_one('SELECT * FROM runs WHERE id=?',(stats['run_id'],))
        mine=stats['mine'];refused=mine['taxonomy'].get('budget_refused_this_incident',0)
        execution_failed=mine['failed']-refused
        assert run['status']==status,stats['status_reasons']
        assert mine['attempted']==mine['succeeded']+mine['failed']
        if case=='refused':
            assert calls==[] and mine['attempted']==1 and refused==1
            assert stats['llm']['attempted']==0 and stats['llm']['refused']['cheap']==1
            assert store.query('SELECT * FROM llm_calls')==[]
        else: assert calls
        if status=='degraded':
            assert any(f'({execution_failed} failed)' in reason for reason in stats['status_reasons'])
        else: assert stats['status_reasons']==[]
        detail=run_data.detail(store,stats['run_id'])
        assert detail['run']['status']==status and detail['calls']['reconciled'] is True
        stage=next(s for s in detail['stages'] if s['name']=='mine')
        assert stage['accounting']['refused_in_failed']==refused
        if case=='refused':assert stage['state']=='refused'
        report=Path(stats['report_path']).read_text().split('## Appendix')[0]
        assert f'**Status**: {status}' in report
        assert f'| Mining execution failures (excluding explicit refusals) | {execution_failed} |' in report
        assert f'| Mining attempts refused at call cap | {refused} |' in report
        assert store.query('SELECT * FROM instruction_operations')==[]
    finally:corpus.store.close()


@pytest.mark.parametrize('stage,payload',[
    ('mine',{'attempted':True,'succeeded':0,'failed':1}),
    ('mine',{'attempted':1,'succeeded':0,'failed':-1}),
    ('mine',{'attempted':1,'succeeded':0,'failed':1,'taxonomy':{'budget_refused_this_incident':2}}),
    ('mine',{'attempted':1,'succeeded':0,'failed':1,'taxonomy':{'budget_refused_this_incident':True}}),
    ('mine',{'attempted':1,'succeeded':0,'failed':1,'taxonomy':{'budget_refused_this_incident':'1'}}),
    ('mine',{'attempted':1,'succeeded':0,'failed':1,'taxonomy':[]} ),
    ('scan',{'files_attempted':1,'files_succeeded':False,'files_failed':1}),
    ('apply',{'attempted':-1,'applied':0,'held':0,'failed':0}),
])
def test_health_rejects_invalid_counts_instead_of_fabricating_ok(stage,payload):
    with pytest.raises(ValueError,match=stage):derive_run_status({stage:payload})


def report_fixture(tmp_path):
    store=Store(tmp_path/'state.db');seed_flow(store)
    # Same-time runs; all retained observations are OUTSIDE their wall-time window.
    store.conn.execute("UPDATE runs SET started='2020-01-01T00:00:00Z',finished='2020-01-01T00:01:00Z'")
    stats={'scan':{'files_attempted':1,'files_succeeded':1,'files_failed':0},
           'mine':{'attempted':1,'succeeded':0,'failed':1,'taxonomy':{'budget_refused_this_incident':1}},
           'apply':{'attempted':0,'applied':0,'held':0,'failed':0,'operation_ids':[]}}
    store.update('runs','id','selected',{'stats_json':json.dumps(stats)})
    store.update('proposals','id','c-two',{'status':'applied'})
    from tests.test_report import _session
    store.upsert_session(_session(str(tmp_path/'foreign.jsonl'),last_scanned_at='2020-01-01T00:00:30Z'))
    store.commit()
    return store,Config(state_dir=str(tmp_path/'unused'))


def test_report_uses_exact_mining_links_and_native_delivery_counts(tmp_path):
    store,cfg=report_fixture(tmp_path)
    with closing(store):
        before=list(store.conn.iterdump())
        generate(store,cfg,'selected',tmp_path/'report.md')
        body=(tmp_path/'report.md').read_text().split('## Appendix')[0]
        assert '| Rules with retained mining observations (this run) | 2 |' in body
        assert '| Retained mining observations (this run) | 4 |' in body
        assert '| — observations: duplicate | 1 |' in body
        assert '| — observations: amend | 1 |' in body
        assert '| Automatic edits recorded (this run) | 0 |' in body
        assert '| Current proposals: applied | 1 |' in body
        assert '| File scan passes completed (this run) | 1 |' in body
        assert 'unlinked current session rows' in body
        assert 'matching counts do not establish run ownership' in body
        assert 'Retained observations may be incomplete for older runs' in body
        assert list(store.conn.iterdump())==before


def test_report_missing_history_schema_is_unknown_not_measured_zero(tmp_path):
    store,cfg=report_fixture(tmp_path)
    with closing(store):
        store.conn.execute("DELETE FROM schema_migrations WHERE name='0021_mining_history'");store.commit()
        generate(store,cfg,'selected',tmp_path/'report.md')
        body=(tmp_path/'report.md').read_text().split('## Appendix')[0]
        assert '| Rules with retained mining observations (this run) | not recorded |' in body


def test_report_rejects_corrupt_retained_history_before_writing(tmp_path):
    store,cfg=report_fixture(tmp_path)
    with closing(store):
        record=store.query_one("SELECT id FROM mining_history WHERE run_id='selected'")
        store.update('mining_history','id',record['id'],{'record_json':'{}'});store.commit()
        with pytest.raises(ReportError,match=record['id']):generate(store,cfg,'selected',tmp_path/'report.md')
        assert not (tmp_path/'report.md').exists()


@pytest.mark.parametrize('raw',['{"scan":{"files_succeeded":true}}','{"apply":{"applied":-1}}','{"unrelated":NaN}'])
def test_report_does_not_publish_invalid_native_counts_or_nonfinite_json(tmp_path,raw):
    with closing(Store(tmp_path/'db')) as store:
        store.insert('runs',{'id':'invalid','started':'2030-01-01T00:00:00Z','stats_json':raw});store.commit()
        with pytest.raises(ReportError):generate(store,Config(state_dir=str(tmp_path/'unused')),'invalid',tmp_path/'report.md')
        assert not (tmp_path/'report.md').exists()
