"""Temporary delivered/proposed rules and exact-copy evidence; no paid models."""
from pathlib import Path
import json,sys,tempfile
from datetime import datetime,timezone
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
import uvicorn
from self_improve import rule_availability,rule_revisions
from self_improve.dashboard.app import create_app
from self_improve.llm import LLMRunner
from self_improve.store import new_id
from tests.test_project_summary import build_project
from tests.test_rule_availability import delivery,run_git
from tests.test_apply import insert_proposal
from tests import conftest as boundary
OUT=ROOT/'reports/dashboard-parity/project-composition';OUT.mkdir(parents=True,exist_ok=True)
def refuse(*a,**kw):raise AssertionError('Models forbidden in Project composition')
def guard(event,args):
    boundary._audit(event,args)
    if boundary._violations:raise AssertionError('Private access forbidden')
LLMRunner._execute=refuse;boundary._armed=True;sys.addaudithook(guard)
with tempfile.TemporaryDirectory(prefix='si-project-summary-ui-') as folder:
    env=build_project(Path(folder).resolve())
    try:
        store=env.store
        for day in (3,4):
            proposal,text=delivery(store,env.cfg,env.a)
            run_git(['merge','--ff-only',env.cfg.project_branch_name],env.a)
            store.conn.execute("UPDATE proposal_events SET ts=? WHERE proposal_id=? AND event='applied'",(f'2030-01-0{day}T00:00:00Z',proposal['id']));store.commit()
        global_target=Path(env.cfg.global_claude_md);global_target.parent.mkdir(parents=True,exist_ok=True);global_target.write_text('# Invented global instructions\n')
        global_proposal,_=delivery(store,env.cfg,env.a,filename=str(global_target),target_kind='global_claude_md')
        store.conn.execute("UPDATE proposal_events SET ts='2030-01-05T00:00:00Z' WHERE proposal_id=? AND event='applied'",(global_proposal['id'],));store.commit()
        rule_availability.collect_availability(store,env.cfg,observed_at='2030-01-06T00:00:00Z')
        # A later exact inverse does not rewrite the earlier retained file check.
        store.insert('proposal_events',{'id':new_id(),'proposal_id':proposal['id'],'event':'rolled_back','actor':'user','ts':'2030-01-07T00:00:00Z',
            'note':json.dumps({'applied_event_id':next(r for r in rule_revisions.retained_revisions(store) if r['proposal_id']==proposal['id'])['application_event_id'],
                'application_id':next(r for r in rule_revisions.retained_revisions(store) if r['proposal_id']==proposal['id'])['application_id']})})
        for index in range(23):
            p=insert_proposal(store,target=env.a/'AGENTS.md',target_kind='project_agents_md',diff='',status='pending')
            store.update('proposals','id',p['id'],{'id':f'summary-pending-{index:02}'})
            store.update('learnings','id',p['learning_id'],{'title':('W'*220 if index==0 else 'Inspect <script>invented source</script> before retrying. '*5 if index==1 else f'Invented pending rule {index:02}')})
        store.commit()
        targets={str(p):p.read_text() for p in [env.a/'AGENTS.md',env.b/'AGENTS.md',env.a/'docs/guide.md',global_target]}
        manifest={'db':str(store.db_path),'project_key':env.key,'copies':env.copies,'snapshot':list(store.conn.iterdump()),'targets':targets,'models':0}
        (OUT/'manifest.json').write_text(json.dumps(manifest))
        uvicorn.run(create_app(env.cfg,clock=lambda:datetime(2030,1,8,tzinfo=timezone.utc)),host='127.0.0.1',port=8876)
    finally:env.store.close()
