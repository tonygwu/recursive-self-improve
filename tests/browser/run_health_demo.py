"""Actual temporary pipeline runs with real caps and scripted provider responses."""
from pathlib import Path
from contextlib import closing
import json
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
import pytest
import uvicorn
from tests import conftest as boundary
from tests.e2e_corpus import build_corpus
from tests.test_run_health import health_case
from self_improve import pipeline
from self_improve.llm import LLMRunner
from self_improve.dashboard.app import create_app

OUT=ROOT/'reports/dashboard-parity/run-health'
OUT.mkdir(parents=True,exist_ok=True)


def guard(event,args):
    boundary._audit(event,args)
    if boundary._violations:raise AssertionError('Private resources are forbidden')


def refuse(*args,**kwargs):
    raise AssertionError('Provider execution is forbidden after fixture preparation')


boundary._armed=True
sys.addaudithook(guard)
with tempfile.TemporaryDirectory(prefix='si-run-health-') as folder:
    root=Path(folder).resolve()
    corpus=build_corpus(root)
    with closing(corpus.store) as store:
        records={}
        for case,name in [('refused','refused-run'),('failure','failure-run'),('success','success-run'),
                          ('mixed','mixed-run'),('all_failed','all-failed-run')]:
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(pipeline,'new_id',lambda:name)
                _,cfg,stats,calls=health_case(root,case,patch,corpus=corpus)
            records[name]={'stats':stats,'calls':calls,'report':Path(stats['report_path']).read_text(),
                'status':store.query_one('SELECT status FROM runs WHERE id=?',(name,))['status']}
        LLMRunner._execute=refuse
        targets={str(p):p.read_text() for p in root.rglob('*.md') if 'runs' not in p.parts}
        info={'root':str(root),'db':str(store.db_path),'sql':list(store.conn.iterdump()),
              'targets':targets,'runs':records,'scripted_calls':sum(len(r['calls']) for r in records.values())}
        (OUT/'manifest.json').write_text(json.dumps(info))
        uvicorn.run(create_app(cfg,db_path=store.db_path),host='127.0.0.1',port=8876)
