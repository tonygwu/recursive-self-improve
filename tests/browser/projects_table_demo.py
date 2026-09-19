"""Invented repositories through real scanner, inventory and measurement readers."""
from datetime import datetime, timezone
from pathlib import Path
import json
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import uvicorn
from self_improve import project_measurements as pm, rule_availability
from self_improve.dashboard.app import create_app
from self_improve.dashboard.queries import projects
from self_improve.llm import LLMRunner
from tests import conftest as boundary
from tests.test_session_context import env as context_env, meta
from tests.test_project_measurements import cohort, calculate
from tests.test_scan_observations import PROJECT, make_repo, scan
from tests.test_scan_occurrences import write_codex, x_user, x_call
from tests.test_dashboard_queries import _session, _proposal, _learning

OUT = ROOT / 'reports/dashboard-parity/projects-table'
OUT.mkdir(parents=True, exist_ok=True)
NOW = datetime(2030, 2, 2, tzinfo=timezone.utc)


def refuse_models(*args, **kwargs):
    raise AssertionError('Models are forbidden in this fixture')


def guard(event, args):
    boundary._audit(event, args)
    if boundary._violations:
        raise AssertionError('Private access is forbidden in this fixture')


LLMRunner._execute = refuse_models
boundary._armed = True
sys.addaudithook(guard)
with tempfile.TemporaryDirectory(prefix='si-projects-ui-') as folder:
    root = Path(folder).resolve()
    generator = context_env.__wrapped__(root)
    fixture = next(generator)
    try:
        revision, version = cohort(fixture)
        measurement = calculate(fixture, revision, version)
        fixture.store.insert('runs', {'id':'browser-measurement', 'started':'2030-02-02T00:00:00Z'})
        fixture.store.commit()
        with fixture.store.transaction(write=True):
            pm.record_project_measurement(fixture.store, run_id='browser-measurement', measurement=measurement)
        extra_copy = make_repo(root/'copies/alpha-retained-long-working-copy-path-for-complete-inspection',
                               'https://github.com/example/alpha.git')
        write_codex(fixture, [meta(extra_copy,'2030-01-20T00:00:00Z','extra-copy'),
            x_user('Looks correct','2030-01-20T00:01:00Z')], name='rollout-extra.jsonl')
        for i in range(1,23):
            name = f'component-{i:02d}'
            if i == 1: name = 'exceptionally-long-repository-name-that-still-has-a-complete-link'
            repo = make_repo(root/'work'/name, 'https://github.com/example/'+name+'.git')
            if i % 3: (Path(repo)/'AGENTS.md').write_text('# Invented project instructions\n' * (i+1))
            records=[meta(repo,'2030-01-20T00:00:00Z',f'project-{i}'),x_call('2030-01-20T00:00:30Z',f'call-{i}'),
                     x_user('Looks correct' if i%2 else "no, that's wrong",'2030-01-20T00:01:00Z')]
            if i == 2: records.append({'type':'event_msg','payload':{'type':'token_count','info':{}}})
            write_codex(fixture,records,name=f'rollout-component-{i}.jsonl')
        scan(fixture, 'browser-projects')
        fixture.store.conn.execute('UPDATE sessions SET project_display=? WHERE project_path=?',
                                   ('example/renamed-alpha',extra_copy))
        fixture.store.commit()
        rule_availability.collect_availability(fixture.store,fixture.cfg,observed_at='2030-02-02T00:00:00Z')
        # A historical indexed path has neither a scan denominator nor an inventory.
        _session(fixture.store,file_path=str(root/'missing.jsonl'),project_key='remote:example.test/retired',
                 project_display='example/retired',project_path=str(root/'retired'),project_key_method='remote_url')
        _session(fixture.store,file_path=str(root/'excluded.jsonl'),project_key='path:invented',
                 project_display='unresolved',project_path=str(root/'unresolved'),project_key_method='unresolved')
        _proposal(fixture.store,learning_id=_learning(fixture.store),status='applied',target_path=str(root/'orphan/AGENTS.md'))
        fixture.store.commit()
        data=projects(fixture.store, now_utc=NOW)
        by_key={row['project_key']:row for row in data['rows']}
        assert by_key[PROJECT]['benefit']['computable'] and by_key[PROJECT]['rules_applied_here']==1
        assert any(r['exposure']['computable'] and r['exposure']['rate_per_100k']==0 for r in data['rows'])
        assert any(not r['exposure']['computable'] for r in data['rows'])
        manifest={'db':str(fixture.store.db_path),'snapshot':list(fixture.store.conn.iterdump()),
                  'targets':{str(p):p.read_text() for p in root.rglob('AGENTS.md')},
                  'project_key':PROJECT,'rows':data['count'],'payload':data,
                  'fixture':'24 canonical repositories; real invented scans, inventory and retained comparison'}
        (OUT/'manifest.json').write_text(json.dumps(manifest))
        uvicorn.run(create_app(fixture.cfg,clock=lambda:NOW),host='127.0.0.1',port=8876)
    finally:
        generator.close()
