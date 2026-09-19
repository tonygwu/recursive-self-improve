"""Two real reviewed deliveries and a separately invoked temporary inverse worker."""
from contextlib import closing
from pathlib import Path
import json
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tests import conftest as boundary
from tests.test_apply import insert_proposal, make_diff, run_git
from tests.test_delivery_worker import approve
from self_improve.config import Config
from self_improve.dashboard.app import create_app
from self_improve.llm import LLMRunner
from self_improve.store import Store
from self_improve.worker import run_once

OUT = ROOT/'reports/dashboard-parity/ordinary-rollback'
OUT.mkdir(parents=True, exist_ok=True)
BASE = '# Rules\n\n- Original.\n'
A = BASE + '- Rule A.\n'
B = A + '- Rule B.\n'
CURRENT = B + '\nHuman footer.\n'
REVERSED = BASE + '- Rule B.\n\nHuman footer.\n'


def configuration(root):
    return Config(state_dir=str(root/'state'), global_claude_md=str(root/'rules.md'),
        codex_global_agents_md=str(root/'global-agents.md'), skills_dir=str(root/'skills'),
        codex_skills_dir=str(root/'codex-skills'), production_repo_path=str(root/'repo-prod'),
        claude_projects_dir=str(root/'claude-projects'), claude_history_path=str(root/'history.jsonl'),
        codex_sessions_dir=str(root/'codex-sessions'), codex_archived_dir=str(root/'codex-archive'))


def refuse_models(*args, **kwargs):
    raise AssertionError('No models are allowed in ordinary rollback acceptance')


def guard(event, args):
    boundary._audit(event, args)
    if boundary._violations:
        raise AssertionError('No private resources are allowed in ordinary rollback acceptance')


LLMRunner._execute = refuse_models
boundary._armed = True
sys.addaudithook(guard)


def seed(root, cfg, store):
    target = root/'rules.md'; target.write_text(BASE)
    proposals, commands = [], []
    for before, after in [(BASE, A), (A, B)]:
        proposal = insert_proposal(store, target=target, diff=make_diff(before, after), status='ungated')
        command = approve(store, cfg, proposal)
        delivered = run_once(store, cfg)
        assert delivered['id']==command['id'] and delivered['state']=='completed', delivered
        assert target.read_text()==after
        proposals.append(proposal['id']); commands.append(command['id'])
    target.write_text(CURRENT)
    assert run_once(store, cfg) is None
    return {'root':str(root),'db':str(store.db_path),'target':str(target),
        'snapshots':str(cfg.state_path('snapshots')), 'head':run_git(['rev-parse','HEAD'],cfg.state_path('snapshots')),
        'proposals':proposals,'commands':commands,'current':CURRENT,'reversed':REVERSED,
        'events':store.query('SELECT * FROM proposal_events'),'database':list(store.conn.iterdump())}


if sys.argv[1:]:
    assert sys.argv[1:]==['--once'], 'Only one explicit fixture-worker operation is allowed'
    manifest = json.loads((OUT/'manifest.json').read_text())
    root = Path(manifest['root']).resolve()
    assert root.is_dir() and root.name.startswith('si-ordinary-rollback-')
    cfg = configuration(root)
    assert Path(manifest['db']).resolve()==cfg.state_path('state.db').resolve()
    with closing(Store(cfg.state_path('state.db'), migrate=False)) as store:
        assert all(Path(p['target_path']).resolve().is_relative_to(root)
                   for p in store.query('SELECT target_path FROM proposals'))
        operation = run_once(store, cfg)
        assert not store.query('SELECT id FROM llm_calls')
        print('ORDINARY_ROLLBACK_WORKER',json.dumps(operation))
else:
    import uvicorn
    with tempfile.TemporaryDirectory(prefix='si-ordinary-rollback-') as folder:
        root = Path(folder).resolve(); cfg = configuration(root)
        with closing(Store(cfg.state_path('state.db'))) as store:
            manifest = seed(root, cfg, store)
        (OUT/'manifest.json').write_text(json.dumps(manifest))
        uvicorn.run(create_app(cfg),host='127.0.0.1',port=8876)
