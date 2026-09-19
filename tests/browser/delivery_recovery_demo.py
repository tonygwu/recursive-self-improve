"""Actual temporary delivery outcomes and separate, explicit fixture workers."""
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
import json
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tests import conftest as boundary
from self_improve import apply, job_worker
from self_improve.commands import command_status, submit_command
from self_improve.config import Config
from self_improve.dashboard.app import create_app
from self_improve.llm import LLMRunner, _ExecResult
from self_improve.store import Store, new_id
from self_improve.worker import run_once
from tests.test_apply import insert_proposal, make_diff
from tests.test_delivery_worker import approve, propose

OUT = ROOT / 'reports/dashboard-parity/delivery-recovery'
OUT.mkdir(parents=True, exist_ok=True)
BASE = '# Rules\n\n- Original.\n'
APPLIED = BASE + '- Rule A.\n'
CURRENT = BASE + '- Rule A edited by a human.\n- Rule B.\n\nHuman footer.\n'
RESOLVED = BASE + '- Rule B.\n\nHuman footer.\n'


def configuration(root):
    return Config(state_dir=str(root / 'state'), global_claude_md=str(root / 'conflict.md'),
                  codex_global_agents_md=str(root / 'global-agents.md'),
                  skills_dir=str(root / 'skills'), codex_skills_dir=str(root / 'codex-skills'),
                  production_repo_path=str(root / 'repo-prod'),
                  claude_projects_dir=str(root / 'claude-projects'),
                  claude_history_path=str(root / 'claude-history.jsonl'),
                  codex_sessions_dir=str(root / 'codex-sessions'),
                  codex_archived_dir=str(root / 'codex-archive'))


def refuse_models(*args, **kwargs):
    raise AssertionError('External models are forbidden in this fixture')


def guard(event, args):
    boundary._audit(event, args)
    if boundary._violations:
        raise AssertionError('Private access is forbidden in this fixture')


LLMRunner._execute = refuse_models
boundary._armed = True
sys.addaudithook(guard)


def seed(root, cfg, store):
    commands, targets = {}, {}

    def one(name):
        target = root / (name + '.md'); target.write_text('before\n')
        targets[name] = str(target)
        return propose(store, target)

    target = root / 'conflict.md'; target.write_text(BASE)
    original = insert_proposal(store, target=target, diff=make_diff(BASE, APPLIED), status='ungated')
    commands['completed'] = approve(store, cfg, original)['id']
    assert run_once(store, cfg)['state'] == 'completed'
    target.write_text(CURRENT); targets['conflict'] = str(target)

    first, second = one('partial-a'), one('partial-b')
    commands['failed'] = approve(store, cfg, first, second)['id']
    real_write = apply._atomic_write

    def fail_second(path, data):
        if Path(path) == Path(targets['partial-b']):
            raise OSError('Invented temporary disk failure')
        return real_write(path, data)

    with patch.object(apply, '_atomic_write', fail_second):
        failed = run_once(store, cfg)
    assert failed['state'] == 'failed'
    assert sorted(t['state'] for t in failed['targets']) == ['completed', 'failed']

    blocked = one('blocked')
    commands['blocked'] = approve(store, cfg, blocked)['id']
    Path(targets['blocked']).write_text('Later human change.\n')
    assert run_once(store, cfg)['state'] == 'blocked'

    cancelled = one('cancelled')
    commands['cancelled'] = approve(store, cfg, cancelled)['id']
    submit_command(store, cfg, {'action':'cancel_delivery', 'command_id':commands['cancelled'], 'request_key':new_id()})
    assert run_once(store, cfg)['state'] == 'cancelled'

    running = one('running')
    commands['running'] = approve(store, cfg, running)['id']

    def interrupt(point, *_):
        if point == 'prepared':
            raise KeyboardInterrupt('Invented interruption after durable preparation')

    with patch.object(apply, '_delivery_checkpoint', interrupt):
        try:
            run_once(store, cfg)
        except KeyboardInterrupt:
            pass
        else:
            raise AssertionError('The fixture did not reach the actual preparation checkpoint')
    assert command_status(store, commands['running'])['state'] == 'running'
    queued = one('queued')
    commands['queued'] = approve(store, cfg, queued)['id']
    assert {name:command_status(store, cid)['state'] for name,cid in commands.items()} == {name:name for name in commands}
    return {'root':str(root), 'db':str(store.db_path), 'commands':commands, 'targets':targets,
            'original_proposal':original['id'], 'partial_proposals':[first['id'],second['id']],
            'initial_targets':{p:Path(p).read_text() for p in targets.values()},
            'initial_events':store.query('SELECT * FROM proposal_events'),
            'before':list(store.conn.iterdump()), 'current':CURRENT, 'resolved':RESOLVED}


if sys.argv[1:]:
    assert sys.argv[1:] in [['--delivery'], ['--models']], 'Unknown fixture operation'
    manifest = json.loads((OUT / 'manifest.json').read_text())
    root = Path(manifest['root']).resolve()
    assert root.is_dir() and root.name.startswith('si-delivery-recovery-')
    cfg = configuration(root)
    with closing(Store(cfg.state_path('state.db'), migrate=False)) as store:
        assert all(Path(p['target_path']).is_relative_to(root) for p in store.query('SELECT target_path FROM proposals'))
        if sys.argv[1] == '--delivery':
            results = []
            for _ in range(12):
                result = run_once(store, cfg)
                if result is None:
                    break
                results.append({'id':result['id'], 'state':result['state']})
            else:
                raise AssertionError('Fixture delivery did not settle')
            print('FIXTURE_DELIVERY_SETTLED', json.dumps(results))
        else:
            calls = []

            def fake_resolution(self, call_id, stage, *args, **kwargs):
                assert stage == 'resolve_rollback', stage
                calls.append(call_id)
                value = {'explanation':'Remove only edited A, preserving later B and the human footer.',
                         'edits':[{'old':'- Rule A edited by a human.\n', 'new':''}]}
                return _ExecResult(ok=True, text=json.dumps(value), parsed=value, outcome='ok',
                                   provider='codex', model_reported='gpt-5.6-terra')

            with patch.object(LLMRunner, '_execute', fake_resolution):
                result = job_worker.run_once(store, cfg)
                assert result['state'] == 'completed', result
                assert job_worker.run_once(store, cfg) is None
            assert len(calls) == 1
            assert Path(manifest['targets']['conflict']).read_text() == CURRENT
            print('FIXTURE_RESOLUTION_GENERATED', json.dumps({'command_id':result['id'], 'proposal_id':result['result']['proposal_id'], 'synthetic_calls':len(calls)}))
else:
    import uvicorn
    with tempfile.TemporaryDirectory(prefix='si-delivery-recovery-') as folder:
        root = Path(folder).resolve(); cfg = configuration(root)
        with closing(Store(cfg.state_path('state.db'))) as store:
            manifest = seed(root, cfg, store)
        (OUT / 'manifest.json').write_text(json.dumps(manifest))
        uvicorn.run(create_app(cfg), host='127.0.0.1', port=8876)
