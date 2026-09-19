"""Invented monthly cohorts and an immutable small recurrence comparison."""
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import json
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import uvicorn
from self_improve.dashboard.app import create_app
from self_improve.llm import LLMRunner
from self_improve.scan import mark_for_rescan
from tests import conftest as boundary
from tests.test_session_context import env as context_env
from tests.test_project_measurements import cohort, calculate, publish
from tests.test_rate_coverage import monthly_cohort
from tests.test_scan_observations import PROJECT, scan

OUT = ROOT / 'reports/dashboard-parity/rate-coverage'
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
with tempfile.TemporaryDirectory(prefix='si-rate-coverage-') as folder:
    root = Path(folder).resolve()
    generator = context_env.__wrapped__(root)
    fixture = next(generator)
    try:
        revision, version = cohort(fixture, after=19)
        measurement = calculate(fixture, revision, version)
        mid = publish(fixture, measurement)
        monthly_cohort(fixture, 1, month=7, year=2030, correction=False)
        monthly_cohort(fixture, 20, month=8, year=2030, correction=False, extra_lines=4)
        monthly_cohort(fixture, 20, month=9, year=2030)
        scan(fixture, 'monthly-coverage')
        changed = replace(fixture.cfg, correction_max_len=1500)
        mark_for_rescan(fixture.store, changed)
        scan(fixture, 'second-version', cfg=changed)
        fixture.store.commit()
        target = Path(fixture.repo) / 'AGENTS.md'
        manifest = {'root':str(root), 'db':str(fixture.store.db_path),
            'project_key':PROJECT, 'version':version, 'measurement_id':mid,
            'measurement':measurement, 'sql':list(fixture.store.conn.iterdump()),
            'targets':{str(target):target.read_text()}}
        (OUT / 'manifest.json').write_text(json.dumps(manifest))
        uvicorn.run(create_app(fixture.cfg, clock=lambda:datetime(2030, 9, 15, tzinfo=timezone.utc)),
                    host='127.0.0.1', port=8876)
    finally:
        generator.close()
