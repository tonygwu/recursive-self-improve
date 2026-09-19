"""Disposable real reviewed deliveries; no provider, private state or service."""
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
import json
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
import uvicorn
from tests import conftest as boundary
from tests.test_rule_availability import cfg as fixture_config
from tests.test_apply import insert_proposal, make_diff
from tests.test_delivery_worker import approve
from tests.test_run_related_deliveries import seed, observe
from self_improve.dashboard.app import create_app
from self_improve.llm import LLMRunner
from self_improve.store import Store
from self_improve.worker import run_once


def guard(event,args):
    boundary._audit(event,args)
    if boundary._violations:raise AssertionError('Private resource in related-delivery fixture')


def no_models(*args,**kwargs):raise AssertionError('No model in related-delivery fixture')


boundary._armed=True
sys.addaudithook(guard)
LLMRunner._execute=no_models
out=ROOT/'reports/dashboard-parity/run-related-deliveries'
out.mkdir(parents=True,exist_ok=True)
with tempfile.TemporaryDirectory(prefix='si-run-related-') as temp:
    root=Path(temp)
    cfg=replace(fixture_config.__wrapped__(root),claude_history_path=str(root/'history.jsonl'),
                codex_skills_dir=str(root/'codex-skills'),production_repo_path=str(root/'production'))
    with closing(Store(cfg.state_path('state.db'))) as store:
        seed(store);targets={};commands=[]
        for index in range(21):
            target=root/f'invented-{index:02}.md';target.write_text('before\n')
            added=f'Complete invented contribution {index:02} '+('retained content '*35)+'END_RETAINED_TEXT\n'
            p=insert_proposal(store,target=target,diff=make_diff('before\n','before\n'+added),status='ungated')
            store.update('learnings','id',p['learning_id'],{'title':f'Invented reviewed lesson {index:02} <literal>','rule_text':added})
            store.commit();members=[p]
            if index==20:
                extra=insert_proposal(store,target=target,diff=p['diff_unified'],status='ungated')
                members.append(extra)
            command=approve(store,cfg,*members)
            with patch('self_improve.apply.utc_now_iso',return_value=f'2030-01-02T01:{index:02}:00Z'):
                assert run_once(store,cfg)['state']=='completed'
            observe(store,p['learning_id']);store.commit()
            commands.append({'id':command['id'],'proposal_id':p['id'],'target_id':command['targets'][0]['id']})
            targets[str(target)]=target.read_text()
        store.insert('runs',{'id':'empty','started':'2030-01-03T00:00:00Z','status':'ok','stats_json':'{}'})
        store.insert('learnings',{'id':'never-delivered','created_at':'2030-01-01T00:00:00Z','rule_text':'Invented undelivered rule'})
        observe(store,'never-delivered','empty');store.commit()
        (out/'manifest.json').write_text(json.dumps({'db':str(store.db_path),'snapshot':list(store.conn.iterdump()),'targets':targets,'commands':commands}))
        uvicorn.run(create_app(cfg,db_path=store.db_path),host='127.0.0.1',port=8876)
