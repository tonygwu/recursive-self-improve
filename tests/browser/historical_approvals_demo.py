"""Disposable historical approval UI and explicit one-pass fixture worker."""
from contextlib import closing
from pathlib import Path
import json
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tests import conftest as boundary
from self_improve.config import Config
from self_improve.dashboard.app import create_app
from self_improve.llm import LLMRunner
from self_improve.store import Store
from self_improve.worker import run_once
from tests.test_historical_approvals import historical

OUT = ROOT / 'reports/dashboard-parity/historical-approvals'
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
if sys.argv[1:] == ['--worker']:
    manifest = json.loads((OUT / 'manifest.json').read_text())
    root = Path(manifest['root']).resolve()
    assert root.is_dir() and root.name.startswith('si-historical-ui-')
    cfg = Config(state_dir=str(root / 'state'), global_claude_md=str(root / 'rules.md'),
                 production_repo_path=str(root / 'repo-prod'))
    with closing(Store(cfg.state_path('state.db'), migrate=False)) as store:
        assert {p['target_path'] for p in store.query('SELECT target_path FROM proposals')} == {str(root / 'rules.md')}
        result = run_once(store, cfg)
        assert result['state'] == 'completed', result
        assert run_once(store, cfg) is None
        assert not store.query('SELECT id FROM llm_calls')
    print('FIXTURE_WORKER_COMPLETED_ONCE')
else:
    import uvicorn
    with tempfile.TemporaryDirectory(prefix='si-historical-ui-') as folder:
        root = Path(folder).resolve()
        target = root / 'rules.md'; target.write_text('before\n')
        cfg = Config(state_dir=str(root / 'state'), global_claude_md=str(target),
                     production_repo_path=str(root / 'repo-prod'))
        with closing(Store(cfg.state_path('state.db'))) as store:
            proposal = historical(store, target)
            store.update('learnings', 'id', proposal['learning_id'], {
                'title': 'Review an old approval', 'rule_text': 'Inspect the complete change before writing.',
                'why': 'The earlier decision did not retain a delivery command.'})
            store.commit()
            manifest = {'root': str(root), 'db': str(store.db_path), 'proposal': proposal['id'],
                        'learning': proposal['learning_id'], 'before': list(store.conn.iterdump()),
                        'events': store.query('SELECT * FROM proposal_events')}
        (OUT / 'manifest.json').write_text(json.dumps(manifest))
        uvicorn.run(create_app(cfg), host='127.0.0.1', port=8876)
