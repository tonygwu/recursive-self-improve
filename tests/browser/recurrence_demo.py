"""Temporary recurrence data from real scans, file checks and one actual pipeline."""
from datetime import datetime,timezone
from pathlib import Path
import json,sys,tempfile
root_repo=Path(__file__).resolve().parents[2];sys.path.insert(0,str(root_repo))
import pytest,uvicorn
from self_improve import project_measurements as pm, rule_availability, pipeline
from self_improve.dashboard.app import create_app
from tests.test_session_context import env as context_env,meta
from tests.test_project_measurements import cohort
from tests.test_scan_observations import PROJECT,make_repo
from tests.test_scan_occurrences import write_codex,x_user,BETA
out=root_repo/'reports/dashboard-parity';out.mkdir(parents=True,exist_ok=True)
with tempfile.TemporaryDirectory(prefix='si-recurrence-ui-') as temp:
    root=Path(temp).resolve();generator=context_env.__wrapped__(root);fixture=next(generator)
    try:
        revision,key=cohort(fixture)
        beta=make_repo(root/'work/beta','https://github.com/example/beta.git')
        write_codex(fixture,[meta(beta,'2030-01-04T00:00:00Z','beta-session'),x_user('Invented clean work','2030-01-04T00:01:00Z')],name='rollout-beta.jsonl')
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(pm,'utc_now_iso',lambda:'2030-02-02T00:00:00.000000Z')
            patch.setattr(rule_availability,'utc_now_iso',lambda:'2030-02-02T00:00:00.000000Z')
            first=pipeline.run_pipeline(fixture.cfg,fixture.store,dry_run=True)
            assert len(first['project_measurements']['measurement_ids'])==1
            for i in range(22):
                rid='browser-observation-'+str(i)
                fixture.store.insert('runs',{'id':rid,'started':'2030-02-02T00:00:00Z'});fixture.store.commit()
                pm.collect_project_measurements(fixture.store,run_id=rid)
        target=Path(fixture.repo)/'AGENTS.md'
        manifest={'db':str(fixture.store.db_path),'project_key':PROJECT,'empty_project_key':BETA,'target':str(target),
            'target_text':target.read_text(),'snapshot':list(fixture.store.conn.iterdump()),
            'measurement_ids':{r['id'] for r in fixture.store.query('SELECT id FROM project_stats')}}
        manifest['measurement_ids']=sorted(manifest['measurement_ids'])
        (out/'recurrence-demo-manifest.json').write_text(json.dumps(manifest))
        uvicorn.run(create_app(fixture.cfg,clock=lambda:datetime(2030,2,2,tzinfo=timezone.utc)),host='127.0.0.1',port=8876)
    finally:generator.close()
