"""Disposable heterogeneous incident archives through the actual dashboard."""
from pathlib import Path
from dataclasses import replace
import json
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
import uvicorn
from self_improve.store import Store
from self_improve.dashboard.app import create_app
from self_improve.llm import LLMRunner
from tests import conftest as boundary
from tests.test_rule_availability import cfg as config_fixture
from tests.test_evidence_search import seed

OUT=ROOT/'reports/dashboard-parity/incident-evidence';OUT.mkdir(parents=True,exist_ok=True)
def refuse(*a,**kw):raise AssertionError('Models forbidden')
def guard(event,args):
    boundary._audit(event,args)
    if boundary._violations:raise AssertionError('Private resource access forbidden')
LLMRunner._execute=refuse
boundary._armed=True;sys.addaudithook(guard)
with tempfile.TemporaryDirectory(prefix='si-incident-ui-') as folder:
    root=Path(folder).resolve();cfg=replace(config_fixture.__wrapped__(root),global_claude_md=str(root/'rule.md'))
    Path(cfg.global_claude_md).write_text('')
    store=Store(cfg.state_path('state.db'))
    try:
        seed(store,root)
        window=[{'text':'Invented readable repeated error','ts':'2030-01-01T00:00:00Z',
                 'session_file':str(root/'copy-a/deleted.jsonl'),'project_path':str(root/'copy-a'),'count_in_session':3},
                {'text':'Same error from another retained occurrence','ts':'',
                 'session_file':str(root/'copy-a/deleted.jsonl'),'project_path':str(root/'copy-a'),'count_in_session':2},
                {'text':'Long retained evidence '+('invented context\n'*500)+'COMPLETE OCCURRENCE END','ts':'2030-01-02T00:00:00Z',
                 'session_file':str(root/'copy-b/deleted.jsonl'),'project_path':str(root/'copy-b'),'count_in_session':0}]
        store.update('incidents','id','i06',{'ts':'2030-01-02T00:00:00Z','window_json':json.dumps(window)})
        store.update('incidents','id','unmined',{'ts':'2030-01-01T00:00:00Z','matched_text':'a'*40,
                     'signal_type':'repeated_error','window_json':json.dumps(window)})
        store.commit()
        manifest={'db':str(store.db_path),'snapshot':list(store.conn.iterdump()),
                  'project_key':'remote:example.test/fixture/repo','window':window,
                  'targets':{cfg.global_claude_md:Path(cfg.global_claude_md).read_bytes().hex()}}
        (OUT/'manifest.json').write_text(json.dumps(manifest))
        uvicorn.run(create_app(cfg),host='127.0.0.1',port=8876)
    finally:store.close()
