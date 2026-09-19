"""Disposable Run inspection with actual interrupted-job and raw-file producers."""
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import json,sys,tempfile
root_repo=Path(__file__).resolve().parents[2];sys.path.insert(0,str(root_repo))
import pytest,uvicorn
from self_improve import job_worker
from self_improve.commands import submit_command
from self_improve.config import Config
from self_improve.dashboard.app import create_app
from self_improve.llm import LLMRunner
from self_improve.store import Store
from tests.test_model_jobs import env as model_env,request,OLD
from tests.test_dashboard_queries import _run

out=root_repo/'reports/dashboard-parity';out.mkdir(parents=True,exist_ok=True)
with tempfile.TemporaryDirectory(prefix='si-run-ui-') as temp, pytest.MonkeyPatch.context() as patch:
    root=Path(temp);config=Config(state_dir=str(root/'state'))
    with closing(Store(config.state_path('state.db'))) as store:
        fixture=model_env.__wrapped__(config,store,root,patch);cfg,_,proposal,calls=fixture
        # Refuse every model; the journal checkpoint stops before any execution.
        def refuse(*args,**kwargs):raise AssertionError('No model execution in Run browser fixture')
        patch.setattr(LLMRunner,'_execute',refuse)
        command=submit_command(store,cfg,request(fixture))
        def stop(event):
            if event=='call_reserved':raise KeyboardInterrupt()
        patch.setattr(job_worker,'_checkpoint',stop)
        try:job_worker.run_once(store,cfg)
        except KeyboardInterrupt:pass
        job_run=store.query_one('SELECT run_id FROM model_jobs')['run_id']
        attempt=store.query_one('SELECT id FROM eval_attempts')['id']
        stats={'run_id':'fixture-run','review_only':True,'budget_limits':{'cheap':80,'strong':10,'gate':20},
            'scan':{'files_attempted':12,'files_succeeded':11,'files_failed':1,'dropped_by_cap':{'frustration':7}},
            'mine':{'attempted':5,'succeeded':4,'failed':1,'taxonomy':{'parse_error':1}},
            'cluster':{'candidates':4,'mode':'agentic_passthrough'},
            'gate':{'attempted':2,'gated_pass':0,'gated_fail':0,'ungated':0,'inconclusive':0,'failed':0,'refused':2},
            'apply':{'attempted':0,'applied':0,'held':4,'failed':0,'operation_ids':[]},
            'llm':{'attempted':1,'calls_made':{'cheap':1,'strong':0,'gate':0},'refused':{'cheap':0,'strong':0,'gate':2},'policy_waits':0},
            'wall_clock':{'wall_seconds':90,'model_seconds':15,'unaccounted_seconds':75,'largest_gap_seconds':40}}
        _run(store,run_id='fixture-run',started='2030-02-07T02:00:00Z',finished='2030-02-07T02:01:30Z',status='degraded',stats=stats)
        _run(store,run_id='same-time',started='2030-02-07T02:00:00Z',status='ok',stats={})
        store.insert('llm_calls',{'id':'fixture-call','run_id':'fixture-run','stage':'mine','outcome':'parse_error','created_at':'2030-02-07T02:01:00Z','model_reported':'invented-model'})
        raw=cfg.state_path('runs','fixture-run','raw');llm=LLMRunner(cfg,store,'fixture-run',raw)
        long=b'<script>fixture text, never executed</script>\n'+b'Complete retained provider output\n'*2200
        llm._retain_attempt('fixture-call',1,'codex',long,b'')
        for n in range(1,23):llm._retain('fixture-call',f'pick{n}.json',json.dumps({'fixture_pick':n}).encode())
        (raw.parent/'report.md').write_text('# Invented run report\nOnly temporary fixture data.\n')
        store.commit()
        manifest={'db':str(store.db_path),'job_run':job_run,'command':command['id'],'attempt':attempt,'long_bytes':len(long),'targets':{proposal['target_path']:OLD}}
        (out/'run-artifacts-manifest.json').write_text(json.dumps(manifest))
        uvicorn.run(create_app(cfg,db_path=store.db_path),host='127.0.0.1',port=8877)
