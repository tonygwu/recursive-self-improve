"""Invented browser jobs with explicit, bounded workers and no external models."""
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
import json
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tests import conftest as boundary
from self_improve import job_worker, recovery_jobs
from self_improve.commands import command_status
from self_improve.config import Config
from self_improve.dashboard.app import create_app
from self_improve.llm import LLMRunner, _ExecResult
from self_improve.project_identity import resolve
from self_improve.store import Store
from tests.test_apply import insert_proposal, make_diff, OLD, NEW
from tests.test_miner_agentic import seed_incident, seed_learning, agentic_payload
from tests.test_model_jobs import synthetic_execute

OUT = ROOT / 'reports/dashboard-parity/model-jobs'
OUT.mkdir(parents=True, exist_ok=True)
RULE = 'Inspect the original request identifier before retrying.'


def configuration(root):
    return Config(state_dir=str(root / 'state'), global_claude_md=str(root / 'CLAUDE.md'),
                  codex_global_agents_md=str(root / 'AGENTS.md'),
                  skills_dir=str(root / 'skills'), codex_skills_dir=str(root / 'codex-skills'),
                  production_repo_path=str(root / 'repo-prod'),
                  claude_projects_dir=str(root / 'claude-projects'),
                  claude_history_path=str(root / 'claude-history.jsonl'),
                  codex_sessions_dir=str(root / 'codex-sessions'),
                  codex_archived_dir=str(root / 'codex-archive'), eval_sandbox_enabled=False,
                  denylist_substrings=('denied-fixture',))


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
    target = root / 'eval-rule.md'; target.write_text(OLD)
    proposal = insert_proposal(store, target=target, diff=make_diff(OLD, NEW), status='pending')
    Path(cfg.global_claude_md).write_text('# Rules\n\nHuman footer.\n')
    settings = root / 'settings.json'
    settings.write_text(json.dumps({'permissions':{'deny':['Read(.secret)']}}) + '\n')
    learning = seed_learning(store, rule=RULE)
    incident = seed_incident(store, str(root / 'deleted-session.jsonl'), project=str(root))
    identity = resolve(str(root))
    store.update('sessions', 'file_path', incident['session_file'], {
        'project_key':identity.key, 'project_display':identity.display, 'project_key_method':identity.method})
    store.update('incidents', 'id', incident['id'], {'project_key':identity.key})
    store.commit()
    targets = recovery_jobs.options(store, cfg, learning['id'])['targets']
    return {'root':str(root), 'db':str(store.db_path), 'proposal_id':proposal['id'],
            'learning_id':learning['id'], 'incident_id':incident['id'],
            'recovery_targets':{mode:next(t['id'] for t in targets if t['destination']['target_kind']==kind)
                                for mode,kind in [('hook','hook'),('correct_target','global_claude_md')]},
            'targets':{str(p):p.read_text() for p in [target, Path(cfg.global_claude_md), settings]},
            'before':list(store.conn.iterdump())}


def run_fixture_worker(mode, manifest):
    root = Path(manifest['root']).resolve()
    assert root.is_dir() and root.name.startswith('si-model-jobs-')
    cfg = configuration(root)
    calls = []

    def execute(self, call_id, stage, model_class, prompt, expect_json, **kwargs):
        calls.append({'id':call_id, 'stage':stage})
        if stage in {'eval_gen','grade'}:
            return synthetic_execute(self, call_id, stage, model_class, prompt, expect_json, **kwargs)
        if stage in {'mine','mine_agentic'}:
            value = agentic_payload()
            provider, model = 'claude', 'claude-sonnet-4-6'
        elif stage == 'propose_recovery':
            source = json.loads(prompt.split('\nFROZEN INPUT\n')[1])
            if source['mode'] == 'hook':
                value = {'supported':True, 'explanation':'Block an invented forbidden command.',
                         'hook':{'event':'PreToolUse', 'matcher':'Bash',
                                 'command':'case "$x" in forbidden) exit 2;; esac', 'timeout':10}}
            else:
                assert 'Human footer.' in source['base']['content']
                value = {'supported':True, 'explanation':'Put this lesson at the selected destination.',
                         'edits':[{'old':'Human footer.',
                                   'new':'Human footer.\n- '+RULE+' <!-- si:'+manifest['learning_id']+' -->\n'}]}
            provider, model = 'claude', 'claude-opus-4-6'
        else:
            raise AssertionError('Unrequested model stage: ' + stage)
        return _ExecResult(ok=True, text=json.dumps(value), parsed=value, outcome='ok',
                           provider=provider, model_reported=model, provider_attempts=1)

    def broken(*args, **kwargs):
        raise ValueError('Invented runner initialization failure')

    def interrupt(point):
        if point == 'step_completed':
            raise KeyboardInterrupt('Invented stop after durable completed checkpoint')

    with closing(Store(cfg.state_path('state.db'), migrate=False)) as store:
        assert all(Path(p['target_path']).is_relative_to(root) for p in store.query('SELECT target_path FROM proposals'))
        with patch.object(LLMRunner, '_execute', execute):
            if mode == '--fail':
                result = job_worker.run_once(store, cfg, _llm_factory=broken)
                assert result['state'] == 'failed' and not calls, result
            elif mode == '--interrupt':
                with patch.object(job_worker, '_checkpoint', interrupt):
                    try:
                        job_worker.run_once(store, cfg)
                    except KeyboardInterrupt:
                        pass
                    else:
                        raise AssertionError('The real job did not reach its completed checkpoint')
                running = store.query("SELECT id FROM commands WHERE state='running'")
                assert len(running) == 1 and len(calls) == 1
                result = command_status(store, running[0]['id'])
                assert result['budget']['consumed']['gate'] == 1
            else:
                result = job_worker.run_once(store, cfg)
                assert result and result['state'] == 'completed', result
                assert job_worker.run_once(store, cfg) is None
        assert all(Path(p).read_text()==text for p,text in manifest['targets'].items())
        assert not cfg.state_path('snapshots').exists()
        print('FIXTURE_MODEL_JOB', json.dumps({
            'command':{k:result[k] for k in ('id','state','run_id','budget','result','error_code','error_detail')},
            'synthetic_calls':calls}))


if sys.argv[1:]:
    assert sys.argv[1:] in [['--fail'],['--interrupt'],['--complete']], 'Unknown fixture operation'
    run_fixture_worker(sys.argv[1], json.loads((OUT / 'manifest.json').read_text()))
else:
    import uvicorn
    with tempfile.TemporaryDirectory(prefix='si-model-jobs-') as folder:
        root = Path(folder).resolve(); cfg = configuration(root)
        with closing(Store(cfg.state_path('state.db'))) as store:
            manifest = seed(root, cfg, store)
        (OUT / 'manifest.json').write_text(json.dumps(manifest))
        uvicorn.run(create_app(cfg), host='127.0.0.1', port=8876)
