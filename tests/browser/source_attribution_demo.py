"""Temporary cross-provider identities and canonical repository names."""
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import json
import sys
import tempfile
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
import uvicorn
from self_improve.store import Store
from self_improve.llm import LLMRunner
from self_improve.dashboard.app import create_app
from tests import conftest as boundary
from tests.test_source_attribution import seed,PROJECT,STAMP
from tests.test_rule_availability import cfg as config_fixture
OUT=ROOT/'reports/dashboard-parity/source-attribution';OUT.mkdir(parents=True,exist_ok=True)


def refuse(*a,**kw):raise AssertionError('Provider calls are forbidden')
def guard(event,args):
    boundary._audit(event,args)
    if boundary._violations:raise AssertionError('Private resources are forbidden')


LLMRunner._execute=refuse
boundary._armed=True;sys.addaudithook(guard)
with tempfile.TemporaryDirectory(prefix='si-source-attribution-') as folder:
    root=Path(folder).resolve()
    cfg=replace(config_fixture.__wrapped__(root),claude_history_path=str(root/'history.jsonl'),
                codex_skills_dir=str(root/'codex-skills'),production_repo_path=str(root/'production'))
    with closing(Store(cfg.state_path('state.db'))) as store:
        seed(store,root)
        for n in range(23):
            iid=f'extra-{n:02}'
            store.insert('incidents',{'id':iid,'session_file':str(root/'codex.jsonl'),
                'session_id':'shared-native','project_key':PROJECT,'signal_type':'correction',
                'ts':STAMP,'created_at':STAMP,'matched_text':f'Invented extra correction {n:02}',
                'window_json':json.dumps([{'role':'user','text':f'Complete evidence number {n:02}.'}])})
            store.link_incident_learning(iid,'rule-0')
        target=Path(cfg.global_claude_md);target.parent.mkdir(parents=True,exist_ok=True);target.write_text('# Invented unchanged target\n')
        store.commit()
        (OUT/'manifest.json').write_text(json.dumps({'root':str(root),'db':str(store.db_path),
            'sql':list(store.conn.iterdump()),'targets':{str(target):target.read_text()},'project':PROJECT}))
        uvicorn.run(create_app(cfg),host='127.0.0.1',port=8876)
