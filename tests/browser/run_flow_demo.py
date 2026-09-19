"""Invented native counters and retained rule relationships; no provider calls."""
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import json
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import uvicorn
from self_improve import mining_history
from self_improve.dashboard.app import create_app
from self_improve.llm import LLMRunner
from self_improve.store import Store
from tests import conftest as boundary
from tests.test_rule_availability import cfg as fixture_config
from tests.test_run_flow import seed_flow, GATE

OUT = ROOT/'reports/dashboard-parity/run-flow'
OUT.mkdir(parents=True, exist_ok=True)


def refuse(*args, **kwargs):
    raise AssertionError('Models are forbidden in this fixture')


def guard(event, args):
    boundary._audit(event, args)
    if boundary._violations:
        raise AssertionError('Private resource access is forbidden')


LLMRunner._execute = refuse
boundary._armed = True
sys.addaudithook(guard)
with tempfile.TemporaryDirectory(prefix='si-run-flow-') as folder:
    root=Path(folder).resolve()
    cfg=replace(fixture_config.__wrapped__(root),
        claude_history_path=str(root/'history.jsonl'),
        codex_skills_dir=str(root/'codex-skills'),
        production_repo_path=str(root/'production'))
    with closing(Store(cfg.state_path('state.db'))) as store:
        seed_flow(store)
        for i in range(20):
            learning={'id':f'd-{i:02d}', 'title':f'Invented additional rule {i}',
                'rule_text':f'Inspect invented fixture {i} before parsing it.',
                'created_at':'2030-01-02T00:00:00Z'}
            store.insert('learnings',learning)
            mining_history.append(store,learning,before=None,kind='new',incidents=[],
                                  provenance={'run_id':'selected','generation':'agentic'})
        common={'scan':{'files_attempted':2,'files_succeeded':2,'files_failed':0},
                'cluster':{'candidates':29,'merge_attempted':0,'merge_succeeded':0,'merge_failed':0},
                'llm':{'attempted':0},'review_only':True}
        selected={**common,'run_id':'selected',
            'mine':{'attempted':26,'succeeded':25,'failed':1,
                    'taxonomy':{'budget_refused_this_incident':1,'budget_exhausted':4}},
            'gate':GATE,'apply':{'attempted':0,'applied':0,'held':2,'failed':0,'operation_ids':[]}}
        neighbor={**common,'run_id':'neighbor',
            'mine':{'attempted':0,'succeeded':0,'failed':0},
            'gate':{**GATE,'attempted':0},
            'apply':{'attempted':0,'applied':0,'held':0,'failed':0,'operation_ids':[]}}
        store.update('runs','id','selected',{'stats_json':json.dumps(selected)})
        store.update('runs','id','neighbor',{'stats_json':json.dumps(neighbor)})
        for name,payload in [('missing-outcomes',{'attempted':3}),('explicit-refusal',{**GATE,'refused':3})]:
            store.insert('runs',{'id':name,'started':'2030-01-02T00:00:00Z','status':'ok',
                'stats_json':json.dumps({**neighbor,'run_id':name,'gate':payload})})
        target=Path(cfg.global_claude_md)
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_text('# Invented unchanged instruction\n')
        for p in store.query('SELECT id FROM proposals'):
            store.update('proposals','id',p['id'],{'target_path':str(target),
                'diff_unified':'--- a/CLAUDE.md\n+++ b/CLAUDE.md\n@@ -1 +1,2 @@\n # Invented unchanged instruction\n+Inspect the invented result.\n'})
        store.commit()
        (OUT/'manifest.json').write_text(json.dumps({'root':str(root),'db':str(store.db_path),
            'sql':list(store.conn.iterdump()),'targets':{str(target):target.read_text()},
            'groups':23,'observations':24}))
        uvicorn.run(create_app(cfg,clock=lambda:datetime(2030,1,2,tzinfo=timezone.utc)),
                    host='127.0.0.1',port=8876)
