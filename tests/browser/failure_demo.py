"""Invented current failures and exact historical source-fix evidence."""
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import argparse
import json
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import uvicorn
from self_improve.dashboard.app import create_app
from self_improve.llm import LLMRunner
from self_improve.store import Store
from tests import conftest as boundary
from tests.test_rule_availability import cfg as fixture_config
from tests.test_failure_acceptance import put_run, FIXED

parser = argparse.ArgumentParser()
parser.add_argument('--later-occurrence', action='store_true')
args = parser.parse_args()
mode = 'later' if args.later_occurrence else 'fixed'
OUT = ROOT / 'reports/dashboard-parity/failures' / mode
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
with tempfile.TemporaryDirectory(prefix='si-failure-ui-') as folder:
    root = Path(folder).resolve()
    cfg = replace(fixture_config.__wrapped__(root),
        claude_history_path=str(root/'history.jsonl'),
        codex_skills_dir=str(root/'codex-skills'),
        production_repo_path=str(root/'production'))
    with closing(Store(cfg.state_path('state.db'))) as store:
        put_run(store, 'historical-fixed', '2026-08-15T00:00:00Z', {FIXED:1})
        if args.later_occurrence:
            put_run(store, 'old-later-occurrence', '2029-12-15T00:00:00Z', {FIXED:2})
        stamp = '2030-01-15T00:00:00Z'
        taxonomy = {'call_failed:spawn_error':2, 'empty_output':1, 'MineParseFailure':1,
            'MineContractViolation':1, 'IntegrityError':1, 'budget_exhausted':4,
            'NovelCause:invented':1}
        put_run(store, 'active-errors', stamp, taxonomy)
        put_run(store, 'same-time-neighbor', stamp, {'timeout':99})
        for i, outcome in enumerate(('spawn_error', 'empty_output', 'parse_error', 'ok')):
            store.insert('llm_calls', {'id':f'invented-call-{i}', 'run_id':'active-errors',
                'stage':'mine', 'outcome':outcome, 'created_at':stamp})
        store.commit()
        target = Path(cfg.global_claude_md)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('# Invented unchanged instruction\n')
        manifest = {'root':str(root), 'db':str(store.db_path), 'mode':mode,
            'sql':list(store.conn.iterdump()), 'synthetic_calls':4,
            'targets':{str(target):target.read_text()}}
        (OUT/'manifest.json').write_text(json.dumps(manifest))
        uvicorn.run(create_app(cfg, clock=lambda:datetime(2030, 1, 15, tzinfo=timezone.utc)),
                    host='127.0.0.1', port=8876)
