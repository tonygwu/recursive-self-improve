"""Disposable actual context collection; no private access or models."""
from pathlib import Path
import json
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
import uvicorn
from self_improve.store import Store
from self_improve import rule_availability, instruction_inventory
from self_improve.dashboard.app import create_app
from self_improve.llm import LLMRunner
from tests import conftest as boundary
from tests.test_rule_availability import cfg as config_fixture, known_copy, init_git_repo, run_git

OUT=ROOT/'reports/dashboard-parity/context-history';OUT.mkdir(parents=True,exist_ok=True)
def refuse(*a,**kw):raise AssertionError('Models forbidden')
def guard(event,args):
    boundary._audit(event,args)
    if boundary._violations:raise AssertionError('Private resource access forbidden')
LLMRunner._execute=refuse
boundary._armed=True;sys.addaudithook(guard)
with tempfile.TemporaryDirectory(prefix='si-context-ui-') as folder:
    root=Path(folder).resolve();cfg=config_fixture.__wrapped__(root)
    store=Store(cfg.state_path('state.db'))
    try:
        repo=init_git_repo(root/'alpha','AGENTS.md','é\n')
        run_git(['remote','add','origin','https://example.test/fixture/context.git'],repo)
        key=known_copy(store,repo)
        (repo/'CLAUDE.md').symlink_to(repo/'AGENTS.md')
        (repo/'report.md').write_text('x'*50000)
        for day in range(1,24):
            (repo/'AGENTS.md').write_text('é'*day+'\n')
            rule_availability.collect_availability(store,cfg,observed_at=f'2030-01-{day:02d}T00:00:00Z')
        other=init_git_repo(root/'beta','README.txt','No instructions')
        run_git(['remote','add','origin','https://example.test/fixture/context.git'],other)
        assert known_copy(store,other)==key
        rule_availability.collect_availability(store,cfg,observed_at='2030-01-23T00:00:00Z')
        records=instruction_inventory.project_inventory(store,project_key=key)['records']
        copies={Path(r['working_copy']['normalized_path']).name:r['working_copy_id'] for r in records}
        manifest={'db':str(store.db_path),'snapshot':list(store.conn.iterdump()),'project_key':key,'copies':copies,
                  'targets':{str(p):p.read_bytes().hex() for p in (repo/'AGENTS.md',repo/'report.md',other/'README.txt')}}
        (OUT/'manifest.json').write_text(json.dumps(manifest))
        uvicorn.run(create_app(cfg),host='127.0.0.1',port=8876)
    finally:store.close()
