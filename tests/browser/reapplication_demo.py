"""A completed rollback served for native reapplication, plus one explicit fixture worker pass."""
from contextlib import closing
from pathlib import Path
import json
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tests import conftest as boundary
from tests.test_apply import insert_proposal, make_diff
from tests.test_delivery_worker import approve
from self_improve import apply
from self_improve.config import Config
from self_improve.dashboard.app import create_app
from self_improve.llm import LLMRunner
from self_improve.store import Store
from self_improve.worker import run_once

OUT = ROOT/'reports/dashboard-parity/reapplication'
OUT.mkdir(parents=True, exist_ok=True)
BASE = '# Rules\n\n- Original.\n'
APPLIED = BASE + '- Rule A.\n'
CURRENT = BASE + '- Rule B.\n\nHuman footer.\n'
REAPPLIED = BASE + '- Rule A.\n- Rule B.\n\nHuman footer.\n'


def configuration(root):
    return Config(state_dir=str(root/'state'), global_claude_md=str(root/'rules.md'),
        codex_global_agents_md=str(root/'global-agents.md'), skills_dir=str(root/'skills'),
        codex_skills_dir=str(root/'codex-skills'), production_repo_path=str(root/'repo-prod'),
        claude_projects_dir=str(root/'claude-projects'), claude_history_path=str(root/'history.jsonl'),
        codex_sessions_dir=str(root/'codex-sessions'), codex_archived_dir=str(root/'codex-archive'))


def refuse_models(*args, **kwargs):
    raise AssertionError('No models are allowed in reapplication acceptance')


def guard(event, args):
    boundary._audit(event, args)
    if boundary._violations:
        raise AssertionError('No private resources are allowed in reapplication acceptance')


LLMRunner._execute = refuse_models
boundary._armed = True
sys.addaudithook(guard)


def seed(root, cfg, store):
    """One reviewed delivery, a later human edit, then a completed rollback."""
    target = root/'rules.md'; target.write_text(BASE)
    proposal = insert_proposal(store, target=target, diff=make_diff(BASE, APPLIED), status='ungated')
    approve(store, cfg, proposal)
    assert run_once(store, cfg)['state'] == 'completed' and target.read_text() == APPLIED
    target.write_text(REAPPLIED)
    assert apply.rollback(store, cfg, proposal['id'])['outcome'] == 'rolled_back'
    assert target.read_text() == CURRENT
    return {'root': str(root), 'db': str(store.db_path), 'target': str(target),
        'proposal': proposal['id'], 'current': CURRENT, 'reapplied': REAPPLIED,
        'events': store.query('SELECT * FROM proposal_events'), 'database': list(store.conn.iterdump())}


if sys.argv[1:]:
    assert sys.argv[1:] == ['--once'], 'Only one explicit fixture-worker operation is allowed'
    manifest = json.loads((OUT/'manifest.json').read_text())
    root = Path(manifest['root']).resolve()
    assert root.is_dir() and root.name.startswith('si-reapplication-')
    cfg = configuration(root)
    assert Path(manifest['db']).resolve() == cfg.state_path('state.db').resolve()
    with closing(Store(cfg.state_path('state.db'), migrate=False)) as store:
        assert all(Path(p['target_path']).resolve().is_relative_to(root)
                   for p in store.query('SELECT target_path FROM proposals'))
        operation = run_once(store, cfg)
        assert not store.query('SELECT id FROM llm_calls')
        print('REAPPLICATION_WORKER', json.dumps(operation))
else:
    import uvicorn
    with tempfile.TemporaryDirectory(prefix='si-reapplication-') as folder:
        root = Path(folder).resolve(); cfg = configuration(root)
        with closing(Store(cfg.state_path('state.db'))) as store:
            manifest = seed(root, cfg, store)
        (OUT/'manifest.json').write_text(json.dumps(manifest))
        uvicorn.run(create_app(cfg), host='127.0.0.1', port=8876)
