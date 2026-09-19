"""Serve Evals from invented deliveries, judgments and native scan observations."""
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import json
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import uvicorn
from self_improve import eval_history
from self_improve.commands import submit_command
from self_improve.dashboard.app import create_app
from self_improve.llm import LLMRunner
from tests import conftest as boundary
from tests.test_delivery_worker import env as delivery_env
from tests.test_quality_policy import env as quality_env, delivered, create_sample, judgment_request
from tests.test_scan_observations import make_repo, scan
from tests.test_scan_occurrences import write_codex, x_meta, x_call, x_user

OUT = ROOT / 'reports/dashboard-parity/evals-density'
OUT.mkdir(parents=True, exist_ok=True)


def refuse_models(*args, **kwargs):
    raise AssertionError('Models are forbidden in this fixture')


def guard(event, args):
    boundary._audit(event, args)
    if boundary._violations:
        raise AssertionError('Private access is forbidden in this fixture')


LLMRunner._execute = refuse_models
boundary._armed = True
sys.addaudithook(guard)
with tempfile.TemporaryDirectory(prefix='si-evals-density-') as folder:
    root = Path(folder).resolve()
    generator = delivery_env.__wrapped__(root)
    cfg, store, target = quality_env.__wrapped__(next(generator), root)
    cfg = replace(cfg, project_identity_use_gh=False, denylist_substrings=('denied-tree',))
    fixture = (cfg, store, target)
    try:
        paths, proposals = [], []
        for i, kind in enumerate(('global_claude_md', 'global_claude_md', 'hook', 'skill')):
            proposal, path, _ = delivered(fixture, i, target_kind=kind)
            paths.append(path)
            proposals.append(proposal)
        for i in range(21):
            sample = create_sample(fixture, size=1, seed=f'invented-{i}')
            if i == 0:
                submit_command(store, cfg, judgment_request(sample, value='uncertain'))
        learning = store.query_one('SELECT * FROM learnings WHERE id=?', (proposals[0]['learning_id'],))
        for i in range(3):
            attempt = eval_history.begin(store, cfg, learning, proposals[0])
            attempt.stop(ValueError(f'Invented initialization stop {i}'))
        repo = make_repo(root / 'example')
        scanner = SimpleNamespace(cfg=cfg, store=store, repo=repo,
            codex_dir=Path(cfg.codex_sessions_dir), claude_dir=Path(cfg.claude_projects_dir))
        scanner.claude_dir.mkdir(parents=True, exist_ok=True)
        for month in range(3, 10):
            for session in range(20):
                stamp = f'2030-{month:02d}-02T00:{session:02d}:'
                sid = f'invented-{month}-{session}'
                write_codex(scanner, [x_meta(stamp+'00Z', repo, sid),
                    x_call(stamp+'01Z', 'call-'+sid), x_user("no, that's wrong", stamp+'02Z')],
                    name=f'rollout-{sid}.jsonl')
        scan(scanner, 'invented-density-scan')
        store.commit()
        manifest = {'root':str(root), 'db':str(store.db_path), 'calls':0, 'sample_count':21,
            'targets':{str(path):path.read_text() for path in paths+[target]},
            'sql':list(store.conn.iterdump())}
        (OUT / 'manifest.json').write_text(json.dumps(manifest))
        uvicorn.run(create_app(cfg, clock=lambda:datetime(2030, 9, 15, tzinfo=timezone.utc)),
                    host='127.0.0.1', port=8876)
    finally:
        generator.close()
