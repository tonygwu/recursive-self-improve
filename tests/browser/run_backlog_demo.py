"""Disposable queue lifecycle and real Run API; all data are invented."""
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import json
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
import uvicorn
from tests import conftest as boundary
from tests.test_rule_availability import cfg as fixture_config
from tests.test_queue_history import fixture_store, incident, anchor, SETTINGS
from self_improve import queue_history as q
from self_improve.dashboard.app import create_app
from self_improve.llm import LLMRunner


def guard(event,args):
    boundary._audit(event,args)
    if boundary._violations:raise AssertionError('Private resource in queue-history fixture')


def no_models(*args,**kwargs):raise AssertionError('No provider in queue-history fixture')


boundary._armed=True
sys.addaudithook(guard)
LLMRunner._execute=no_models
out=ROOT/'reports/dashboard-parity/run-backlog';out.mkdir(parents=True,exist_ok=True)
with tempfile.TemporaryDirectory(prefix='si-run-backlog-') as temporary:
    root=Path(temporary)
    cfg=replace(fixture_config.__wrapped__(root),claude_history_path=str(root/'history.jsonl'),
                codex_skills_dir=str(root/'codex-skills'),production_repo_path=str(root/'production'))
    store,clock=fixture_store(root/'state.db')
    with closing(store):
        def publish(name,when,phase='finish'):
            settings={**SETTINGS,'project_filter':'invented/'+'long_scope_'*80+'END_FILTER'}
            anchor(store,clock,run_id=name,when=when,phase=phase,settings=settings)
            store.update('runs','id',name,{'started':when[:10]+'T00:00:00Z','finished':when,
              'stats_json':json.dumps({'run_id':name,'dry_run':False,'review_only':True,
                 'scan':{'files_attempted':0,'files_succeeded':0,'files_failed':0},
                 'mine':{'attempted':0,'succeeded':0,'failed':0},
                 'apply':{'attempted':0,'applied':0,'held':0,'failed':0,'operation_ids':[]},
                 'budget_limits':{'cheap':80,'strong':10,'gate':78}})})
            store.commit()
        clock[0]='2030-01-02T00:00:00.000Z';incident(store,'admitted')
        for i in range(8):q.set_processed_status(store,f'baseline-{i}','dismissed',outcome='negative')
        publish('partial','2030-01-03T12:00:00.000Z')
        publish('selected','2030-01-09T12:00:00.000Z')
        clock[0]='2030-01-10T10:00:00.000Z'
        store.conn.execute("DELETE FROM incidents WHERE id='baseline-0'")
        publish('maintenance','2030-01-10T12:00:00.000Z')
        clock[0]='2030-01-12T00:00:00.000Z'
        for i in range(5):incident(store,f'growth-{i}')
        publish('growing','2030-01-18T12:00:00.000Z')
        clock[0]='2030-01-20T00:00:00.000Z'
        for row in store.query("SELECT id FROM incidents WHERE status='new'"):
            q.set_processed_status(store,row['id'],'mined',outcome='new')
        publish('empty','2030-01-26T12:00:00.000Z')
        clock[0]='2030-01-26T13:00:00.000Z';incident(store,'next-week')
        publish('steady','2030-02-03T12:00:00.000Z')
        publish('start-only','2030-02-04T12:00:00.000Z',phase='start')
        store.insert('runs',{'id':'old','started':'2029-01-01T00:00:00Z','status':'ok','stats_json':'{}'});store.commit()
        (out/'manifest.json').write_text(json.dumps({'db':str(store.db_path),'snapshot':list(store.conn.iterdump()),
           'expected':{r:q.read_run(store,r) for r in ('selected','partial','maintenance','growing','empty','steady','start-only','old')},'models':0}))
        uvicorn.run(create_app(cfg,db_path=store.db_path),host='127.0.0.1',port=8876)
