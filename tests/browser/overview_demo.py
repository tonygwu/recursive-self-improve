"""Disposable Overview audit fixture. No private resources or paid model calls."""
from pathlib import Path
from dataclasses import replace
from datetime import datetime, timezone
from contextlib import closing
import argparse,json,sys,tempfile
root_repo=Path(__file__).resolve().parents[2];sys.path.insert(0,str(root_repo))
import uvicorn
from self_improve.store import Store
from self_improve.dashboard.app import create_app
from self_improve.llm import LLMRunner
from tests.test_rule_availability import cfg as config_fixture
from tests.test_rejections import proposal,OLD
from tests.test_dashboard_queries import _run,_session,_incident

def refuse_models(*args,**kwargs):raise AssertionError('No model calls in the Overview fixture')
LLMRunner._execute=refuse_models
out=root_repo/'reports/dashboard-parity';out.mkdir(parents=True,exist_ok=True)
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--deliveries',action='store_true',help='Include completed, combined and later changed reviewed deliveries')
args=parser.parse_args()
with tempfile.TemporaryDirectory(prefix='si-overview-ui-') as temp:
    root=Path(temp).resolve();cfg=config_fixture.__wrapped__(root)
    cfg=replace(cfg,claude_history_path=str(root/'absent-history'),production_repo_path=str(root/'absent-production'))
    Path(cfg.global_claude_md).parent.mkdir(parents=True,exist_ok=True);Path(cfg.global_claude_md).write_text(OLD)
    with closing(Store(cfg.state_path('state.db'))) as store:
        projects=root/'fixture-project';projects.mkdir();session=str(root/'fixture.jsonl')
        _session(store,file_path=session,project_key='remote:example.test/fixture/overview',project_path=str(projects))
        for day in range(1,8):
            for n in range(3):
                iid=f'event-{day}-{n}'
                _incident(store,incident_id=iid,session_file=session,ts=f'2030-02-{day:02}T12:00:00Z',created_at='2030-02-08T02:00:00Z',project_key='remote:example.test/fixture/overview')
                store.update('incidents','id',iid,{'project_path':str(projects)})
        for run_id,day,ok,fail in [('first',2,2,1),('second',4,3,0),('same-a',7,8,0),('same-z',7,4,1)]:
            stats={'run_id':run_id,'review_only':True,'budget_limits':{'cheap':80,'expensive':12},
                   'scan':{'files_attempted':2,'files_succeeded':2,'files_failed':0},
                   'mine':{'attempted':ok+fail,'succeeded':ok,'failed':fail,'taxonomy':{'MineParseFailure':fail} if fail else {}},
                   'cluster':{'candidates':ok,'mode':'agentic_passthrough'},
                   'gate':{'attempted':2,'gated_pass':0,'gated_fail':0,'ungated':0,'inconclusive':0,'failed':0,'refused':2},
                   'apply':{'attempted':0,'applied':0,'held':0,'failed':0}}
            _run(store,run_id=run_id,started=f'2030-02-{day:02}T02:00:00Z',finished=f'2030-02-{day:02}T03:00:00Z',status='degraded' if fail else 'ok',stats=stats)
        _run(store,run_id='unfinished',started='2030-02-06T02:00:00Z',status='interrupted')
        pending=[]
        for index in range(8):
            target=root/f'target-{index}.md';target.write_text(OLD)
            row=proposal(store,target)
            title=('Inspect the complete fixture response before retrying ' if index==0 else 'Fixture lesson ')+str(index)
            store.update('learnings','id',row['learning_id'],{'title':title,'created_at':'2030-02-07T04:00:00Z'})
            pending.append(row['id'])
        first=store.query_one('SELECT id FROM learnings ORDER BY id LIMIT 1')
        store.update('learnings','id',first['id'],{'title':'Fixture <script>markup</script> '+('complete evidence '*30)})
        completed=[]
        if args.deliveries:
            from unittest.mock import patch
            from self_improve.worker import run_once
            from self_improve import eval_history
            from tests.test_delivery_worker import approve,propose
            for i in range(3):
                path=root/f'delivered-target-{i}.md';path.write_text('before\n')
                members=[propose(store,path)]
                if i==2:members.append(propose(store,path))
                for member in members:
                    title=('Inspect the entire invented response and its source before retrying. '*4) if i==2 else ('W'*220 if i==1 else f'Preserve the invented input {i}.')
                    store.update('learnings','id',member['learning_id'],{'title':title})
                store.commit()
                command=approve(store,cfg,*members)
                with patch('self_improve.apply.utc_now_iso',return_value=f'2030-02-07T0{i+4}:00:00Z'):
                    assert run_once(store,cfg)['state']=='completed'
                completed.append(command['id'])
                for member in members:
                    store.update('learnings','id',member['learning_id'],{'title':'Changed later; not the delivered title'})
            learning=store.query_one('SELECT * FROM learnings WHERE id=?',(members[0]['learning_id'],))
            store.commit()
            attempt=eval_history.begin(store,cfg,learning,members[0])
            attempt.stop(ValueError('Invented evaluation stopped before model execution'))
        store.commit()
        targets={str(p):p.read_text() for p in root.glob('*.md')}
        manifest={'db':str(store.db_path),'snapshot':list(store.conn.iterdump()),'targets':targets,'proposal_ids':pending,'latest_run':'same-z','completed_commands':completed}
        (out/'overview-manifest.json').write_text(json.dumps(manifest))
        uvicorn.run(create_app(cfg,db_path=store.db_path,clock=lambda:datetime(2030,2,8,tzinfo=timezone.utc)),host='127.0.0.1',port=8876)
