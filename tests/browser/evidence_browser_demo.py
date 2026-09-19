"""Disposable complete evidence browser with real local delivery and availability."""
from pathlib import Path
from dataclasses import replace
from datetime import datetime, timezone
import json, sys, tempfile
from contextlib import closing
root_repo=Path(__file__).resolve().parents[2];sys.path.insert(0,str(root_repo))
import uvicorn
from self_improve.store import Store
from self_improve.dashboard.app import create_app
from self_improve.rule_availability import collect_availability
from self_improve.commands import submit_command
from self_improve.llm import LLMRunner
from tests.test_evidence_search import seed
from tests.test_rule_availability import cfg as config_fixture,known_copy,delivery
from tests.test_rejections import proposal,decision,init_git_repo,run_git,make_diff,OLD

def refuse_models(*args,**kwargs):raise AssertionError('Browser fixture must never call a model')
LLMRunner._execute=refuse_models
out=root_repo/'reports/dashboard-parity';out.mkdir(parents=True,exist_ok=True)
with tempfile.TemporaryDirectory(prefix='si-evidence-ui-') as temp:
    root=Path(temp).resolve();cfg=config_fixture.__wrapped__(root)
    cfg=replace(cfg,claude_history_path=str(root/'absent-history'),production_repo_path=str(root/'absent-production'),
                denylist_substrings=('denied-fixture',))  # Permit this temporary fixture's native cwd.
    with closing(Store(str(cfg.state_path('state.db')))) as store:
        from self_improve.execution_policy import set_class_policy
        set_class_policy(store,"project",True,now="2020-01-01T00:00:00Z")
        seed(store,root)
        incident=store.query_one("SELECT window_json FROM incidents WHERE id='unmined'")
        window=json.loads(incident["window_json"])
        for event in window:event["ts"]="" # Unknown logical time remains explicit in the valid archive shape.
        store.update("incidents","id","unmined",{"window_json":json.dumps(window)})
        Path(cfg.global_claude_md).parent.mkdir(parents=True,exist_ok=True)
        Path(cfg.global_claude_md).write_text('# Fixture rules\n')
        store.update('learnings','id','l00',{'violated_existing_rule':'The fixture assistant reportedly ignored the existing retry rule.'})
        store.update('learnings','id','l44',{'title':'Hostile <img src=x onerror=alert(1)> rule','rule_text':'Literal hostile <script>window.privateSentinel=1</script> retained text.'})
        store.update('proposals','id','p00',{'target_path':cfg.global_claude_md,'diff_unified':make_diff('# Fixture rules\n','# Fixture rules\n- Complete proposal only token.\n')})
        store.commit()
        commands=[]
        for n in range(12):
            target=root/f'command-{n}.md';target.write_text(OLD)
            candidate=proposal(store,target)
            commands.append(submit_command(store,cfg,decision(store,cfg,'approve',[candidate])))
        clone=init_git_repo(root/'working-copy','AGENTS.md','# Human fixture instructions\n')
        run_git(['remote','add','origin','https://example.test/owner/evidence-fixture.git'],clone)
        project_key=known_copy(store,clone)
        delivered=[]
        for n in range(12):
            candidate,_=delivery(store,cfg,clone)
            delivered.append(candidate)
        focus=delivered[0]
        store.insert('incidents',{'id':'copy-incident','session_file':str(clone/'invented.jsonl'),
            'project_path':str(clone),'project_key':project_key,'signal_type':'correction',
            'matched_text':'Existing rule absent in the actual fixture working copy.',
            'window_json':json.dumps([{'role':'user','text':'Check this working copy before retrying.'}]),
            'status':'mined','ts':'2030-01-01T00:00:00Z','created_at':'2030-01-01T00:00:00Z'})
        store.insert('incident_learnings',{'incident_id':'copy-incident','learning_id':focus['learning_id']});store.commit()
        collect_availability(store,cfg,observed_at='2030-01-02T00:00:00Z')
        from self_improve.native_loads import record_native_event
        native_sid='77777777-7777-4777-8777-777777777777'
        native_reports={}
        for provider in ('claude','codex'):
            payload={'session_id':native_sid,'cwd':str(clone),'hook_event_name':'SessionStart',
                     'source':'startup','transcript_path':str(root/'missing-native.jsonl') if provider=='claude' else None}
            native_reports[provider]=record_native_event(store,cfg,payload,provider=provider)
        operation=store.query_one('SELECT id FROM instruction_operations WHERE proposal_id=?',(focus['id'],))
        assert store.query_one('SELECT COUNT(*) n FROM llm_calls')['n']==0
        manifest={'db':str(store.db_path),'snapshot':list(store.conn.iterdump()),'command':commands[0]['id'],
            'operation':operation['id'],'availability_learning':focus['learning_id'],'project_key':project_key,
            'native_sid':native_sid,'native_reports':native_reports,
            'targets':{str(p):p.read_text() for p in [Path(cfg.global_claude_md),clone/'AGENTS.md']}}
        (out/'evidence-browser-manifest.json').write_text(json.dumps(manifest))
        uvicorn.run(create_app(cfg,db_path=store.db_path,clock=lambda:datetime(2030,2,2,tzinfo=timezone.utc)),host='127.0.0.1',port=8876)
