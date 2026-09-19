"""Invented mixed and conflicting Review families; no live state or model calls."""
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import json
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import uvicorn
from tests import conftest as boundary
from tests.test_rule_availability import cfg as fixture_config
from tests.test_apply import insert_proposal, make_diff, init_git_repo
from self_improve.dashboard.app import create_app
from self_improve.llm import LLMRunner
from self_improve.store import Store


def refuse_private(event, args):
    boundary._audit(event, args)
    if boundary._violations:
        raise AssertionError('Review fixture attempted private access')


def refuse_models(*args, **kwargs):
    raise AssertionError('Review fixture cannot execute models')


boundary._armed = True
sys.addaudithook(refuse_private)
LLMRunner._execute = refuse_models
out = ROOT/'reports/dashboard-parity/review-layout'
out.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(prefix='si-review-layout-') as temp:
    root = Path(temp).resolve()
    cfg = replace(fixture_config.__wrapped__(root),
                  claude_history_path=str(root/'history.jsonl'),
                  codex_skills_dir=str(root/'codex-skills'),
                  production_repo_path=str(root/'production'),
                  global_claude_md_line_budget=3)
    target = Path(cfg.global_claude_md)
    target.parent.mkdir(parents=True, exist_ok=True)
    before = 'one\ntwo\nthree\n'
    target.write_text(before)
    targets = [target]
    for index in range(3):
        repo = init_git_repo(root/('invented-project-'+str(index)+'-with-a-complete-long-name'), 'AGENTS.md', before)
        targets.append(repo/'AGENTS.md')
    with closing(Store(cfg.state_path('state.db'))) as store:
        families = {}
        ids = []
        for index, path in enumerate(targets):
            for n in range([13,3,2,2][index]):
                p = insert_proposal(store, target=path,
                    target_kind='global_claude_md' if index == 0 else 'project_agents_md',
                    diff=make_diff(before,before+'Keep the complete invented evidence visible.\n'),
                    status=['pending','ungated','gated_fail'][n%3])
                if not ids: families['mixed']=p['learning_id']
                store.update('proposals','id',p['id'],{'learning_id':families['mixed']})
                ids.append(p['id'])
        store.update('learnings','id',families['mixed'],{'title':'Inspect complete evidence',
            'rule_text':'Read the complete retained evidence before approving an instruction change.',
            'why':'Twenty proposals repeat one invented lesson across four destinations.'})
        conflict=[]
        for n in range(2):
            p=insert_proposal(store,target=target,diff=make_diff(before,before.replace('two',f'ALTERNATIVE {n}')),status='pending')
            if not conflict: families['conflict']=p['learning_id']
            store.update('proposals','id',p['id'],{'learning_id':families['conflict']})
            conflict.append(p['id'])
        store.update('learnings','id',families['conflict'],{'title':'Choose one alternative',
            'rule_text':'Select one of the two conflicting invented corrections.'})
        store.commit()
        (out/'manifest.json').write_text(json.dumps({'db':str(store.db_path),'families':families,
            'mixed':ids,'conflict':conflict,'snapshot':list(store.conn.iterdump()),
            'targets':{str(p):p.read_text() for p in targets}}))
        uvicorn.run(create_app(cfg,db_path=store.db_path),host='127.0.0.1',port=8876)
