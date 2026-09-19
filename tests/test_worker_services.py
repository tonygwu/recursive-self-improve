"""Service acceptance uses invented state and fake launchctl; no real workers."""
from contextlib import closing
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys

import pytest

from self_improve.config import Config
from self_improve.store import Store

REPO = Path(__file__).resolve().parents[1]


def fixture_config(tmp_path):
    project = tmp_path / 'relocated production'
    (project / 'ops').mkdir(parents=True)
    for name in ('run-worker.sh', 'run-jobs.sh'):
        source = REPO / 'ops' / name
        if source.exists():
            shutil.copyfile(source, project / 'ops' / name)
            (project / 'ops' / name).chmod(0o755)
    cfg = Config(state_dir=str(tmp_path / 'private state'), production_repo_path=str(project))
    config = tmp_path / 'selected config.toml'
    config.write_text(f'state_dir = {json.dumps(cfg.state_dir)}\nproduction_repo_path = {json.dumps(str(project))}\n')
    with closing(Store(cfg.state_path('state.db'))):
        pass
    return cfg, config


@pytest.mark.parametrize('worker', ['worker', 'jobs'])
def test_actual_cli_read_only_preflight(tmp_path, worker):
    cfg, config = fixture_config(tmp_path)
    before = cfg.state_path('state.db').read_bytes()
    result = subprocess.run([sys.executable, '-m', 'self_improve.cli', '--config', str(config), worker, '--check'], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report['state'] == 'current'
    assert report['database'] == str(cfg.state_path('state.db').resolve())
    assert report['worker'] == ('delivery' if worker == 'worker' else 'models')
    assert cfg.state_path('state.db').read_bytes() == before


def test_missing_state_preflight_never_creates_it(tmp_path):
    from self_improve import worker_services
    cfg = Config(state_dir=str(tmp_path / 'missing'))
    with pytest.raises(worker_services.ServiceError, match='upgrade-state'):
        worker_services.check_runtime(cfg)
    assert not cfg.state_path().exists()


class FakeServiceCommands:
    """Run the real temporary script/CLI preflight, but never native launchctl."""
    def __init__(self):
        self.registered = set()
        self.calls = []
        self.failures = {}
        self.preflight_reply = None
        self.on_preflight = None

    def __call__(self, cmd, *, env=None):
        self.calls.append(cmd)
        if cmd[0] == '/bin/bash':
            if self.preflight_reply is not None:
                return subprocess.CompletedProcess(cmd, *self.preflight_reply)
            result = subprocess.run(cmd, env=env, capture_output=True, text=True)
            if self.on_preflight:
                self.on_preflight()
            return result
        if cmd[0] == '/usr/bin/plutil':
            plistlib.loads(Path(cmd[-1]).read_bytes())
        elif cmd[0] == '/bin/launchctl':
            op = cmd[1]
            if self.failures.get(op):
                self.failures[op] -= 1
                return subprocess.CompletedProcess(cmd, 5, '', 'invented ' + op + ' failure')
            if op == 'print':
                return subprocess.CompletedProcess(cmd, 0 if cmd[2] in self.registered else 113, 'invented registration', '')
            if op == 'bootstrap':
                data = plistlib.loads(Path(cmd[-1]).read_bytes())
                self.registered.add(cmd[2] + '/' + data['Label'])
            elif op == 'bootout':
                self.registered.remove(cmd[2])
            else:
                pytest.fail('unexpected launchctl command')
        else:
            pytest.fail('unexpected native command')
        return subprocess.CompletedProcess(cmd, 0, 'invented success', '')


@pytest.fixture
def service_env(tmp_path, monkeypatch):
    import shlex
    from self_improve import worker_services as services
    cfg, config = fixture_config(tmp_path)
    bindir = tmp_path / 'fake bins'
    bindir.mkdir()
    # The uv stand-in has a bare interpreter dependency. The complete service
    # PATH must find it under a minimal launchd-like environment.
    interpreter = bindir / 'service-python'
    interpreter.write_text('#!/bin/bash\nexec ' + shlex.quote(sys.executable) + ' "$@"\n')
    interpreter.chmod(0o755)
    uv = bindir / 'uv'
    uv.write_text('#!/usr/bin/env service-python\nimport os,sys\nassert sys.argv[1:5] == ["run", "--no-sync", "--frozen", "selfimprove"]\nos.execv(sys.executable, [sys.executable,"-m","self_improve.cli"]+sys.argv[5:])\n')
    uv.chmod(0o755)
    monkeypatch.setenv('SI_UV', str(uv))
    monkeypatch.setenv('SI_PATH_EXTRA', str(bindir))
    monkeypatch.setenv('PYTHONPATH', str(REPO / 'src'))
    monkeypatch.delenv('SI_SERVICE_STATE_DB', raising=False)
    monkeypatch.delenv('SI_CONFIG', raising=False)
    return services, cfg, config, FakeServiceCommands(), tmp_path / 'agents'


def operate(env, service, action):
    services, cfg, config, runner, agents = env
    dst = agents / (service + '.plist')
    return services.manage(cfg, service, action, config_path=config, run_cmd=runner, agent_plist_path=dst)


@pytest.mark.parametrize('service', ['delivery', 'models'])
def test_install_restart_status_and_uninstall_use_selected_config(service_env, service):
    services, cfg, config, runner, agents = service_env
    before = cfg.state_path('state.db').read_bytes()
    assert 'registration verified' in operate(service_env, service, 'install')
    dst = agents / (service + '.plist')
    data = plistlib.loads(dst.read_bytes())
    assert data['EnvironmentVariables']['SI_CONFIG'] == str(config)
    assert data['EnvironmentVariables']['SI_SERVICE_STATE_DB'] == str(cfg.state_path('state.db'))
    assert cfg.state_path('state.db').read_bytes() == before
    assert len(runner.registered) == 1
    assert 'config matches; database matches' in operate(service_env, service, 'status')
    assert 'registration verified' in operate(service_env, service, 'install')
    assert len(runner.registered) == 1
    # Execute the installed chain twice against an empty invented queue. It does
    # no target or model work and uses the existing selected database each time.
    for _ in range(2):
        result = subprocess.run(data['ProgramArguments'] + ['--once'], env=dict(os.environ, **data['EnvironmentVariables']), capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    assert 'removed' in operate(service_env, service, 'uninstall')
    assert not runner.registered and not dst.exists()
    assert 'already absent' in operate(service_env, service, 'uninstall')


def test_services_do_not_stop_or_remove_each_other(service_env):
    _, _, _, runner, agents = service_env
    for service in ('delivery', 'models'):
        operate(service_env, service, 'install')
    model_before = (agents / 'models.plist').read_bytes()
    operate(service_env, 'delivery', 'uninstall')
    assert len(runner.registered) == 1
    assert next(iter(runner.registered)).endswith('/com.self-improve.jobs')
    assert (agents / 'models.plist').read_bytes() == model_before


@pytest.mark.parametrize('operation,count,message', [('bootout', 1, 'prior plist retained'), ('bootstrap', 1, 'prior state restored'), ('bootstrap', 2, 'restoration incomplete')])
def test_failed_replacement_preserves_prior_service_evidence(service_env, operation, count, message):
    services, _, _, runner, agents = service_env
    operate(service_env, 'delivery', 'install')
    dst = agents / 'delivery.plist'
    before = dst.read_bytes()
    runner.failures[operation] = count
    with pytest.raises(services.ServiceError, match=message):
        operate(service_env, 'delivery', 'install')
    assert dst.read_bytes() == before
    assert bool(runner.registered) == (count == 1)


@pytest.mark.parametrize('reply', [(2, '', 'invented preflight failed'), (0, '{}', ''), (0, 'not json', ''), (0, '[]', '')])
def test_failed_or_wrong_preflight_does_not_touch_existing_service(service_env, reply):
    services, _, _, runner, agents = service_env
    operate(service_env, 'delivery', 'install')
    before = (agents / 'delivery.plist').read_bytes()
    registered = set(runner.registered)
    runner.calls.clear()
    runner.preflight_reply = reply
    with pytest.raises(services.ServiceError, match='preflight'):
        operate(service_env, 'delivery', 'install')
    assert (agents / 'delivery.plist').read_bytes() == before
    assert runner.registered == registered
    assert all(cmd[0] != '/bin/launchctl' for cmd in runner.calls)


@pytest.mark.parametrize('worker', ['worker', 'jobs'])
@pytest.mark.parametrize('mode', ['check', 'once'])
def test_old_schema_refuses_cli_without_migration_or_work(tmp_path, monkeypatch, worker, mode):
    import self_improve.store as store_module
    from self_improve.cli import main
    cfg, config = fixture_config(tmp_path)
    cfg.state_path('state.db').unlink()
    with monkeypatch.context() as patch:
        patch.setattr(store_module, 'MIGRATIONS', store_module.MIGRATIONS[:-1])
        with closing(Store(cfg.state_path('state.db'))):
            pass
    before = cfg.state_path('state.db').read_bytes()
    assert main(['--config', str(config), worker, '--' + mode]) == 2
    assert cfg.state_path('state.db').read_bytes() == before


@pytest.mark.parametrize('service', ['delivery', 'models'])
def test_config_removed_or_retargeted_refuses_installed_startup(service_env, tmp_path, service):
    _, cfg, config, _, agents = service_env
    operate(service_env, service, 'install')
    data = plistlib.loads((agents / (service + '.plist')).read_bytes())
    env = dict(os.environ, **data['EnvironmentVariables'])
    other = tmp_path / 'other state'
    with closing(Store(other / 'state.db')):
        pass
    before = (other / 'state.db').read_bytes()
    config.write_text(f'state_dir = {json.dumps(str(other))}\n')
    result = subprocess.run(data['ProgramArguments'] + ['--once'], env=env, capture_output=True, text=True)
    assert result.returncode == 2 and 'Selected database changed' in result.stderr
    assert (other / 'state.db').read_bytes() == before
    config.unlink()
    result = subprocess.run(data['ProgramArguments'] + ['--once'], env=env, capture_output=True, text=True)
    assert result.returncode == 2 and 'existing absolute config' in result.stderr
    # Status/removal do not need the deleted config or a readable database.
    cfg.state_path('state.db').unlink()
    assert 'registered' in operate(service_env, service, 'status')
    assert 'removed' in operate(service_env, service, 'uninstall')


def test_other_config_cannot_replace_or_remove_service(service_env, tmp_path):
    services, cfg, _, runner, agents = service_env
    operate(service_env, 'delivery', 'install')
    other = tmp_path / 'other.toml'
    other.write_text('')
    before = (agents / 'delivery.plist').read_bytes()
    for action in ('install', 'uninstall'):
        with pytest.raises(services.ServiceError, match='another config'):
            services.manage(cfg, 'delivery', action, config_path=other, run_cmd=runner, agent_plist_path=agents / 'delivery.plist')
    assert (agents / 'delivery.plist').read_bytes() == before


def test_config_change_during_preflight_refuses_registration(service_env):
    services, _, config, runner, agents = service_env
    runner.on_preflight = lambda: config.write_text('')
    with pytest.raises(services.ServiceError, match='changed during preflight'):
        operate(service_env, 'delivery', 'install')
    assert not agents.exists() and not runner.registered


def test_actual_service_cli_dispatch_bypasses_default_store(service_env, monkeypatch, capsys):
    from self_improve import cli
    services, _, config, runner, agents = service_env
    manage = services.manage
    def selected(*args, **kwargs):
        return manage(*args, **kwargs, run_cmd=runner, agent_plist_path=agents / 'delivery.plist')
    monkeypatch.setattr(services, 'manage', selected)
    def forbidden(*args, **kwargs):
        pytest.fail('service command opened shared migrating Store')
    monkeypatch.setattr(cli, 'Store', forbidden)
    for action in ('install', 'status', 'uninstall'):
        assert cli.main(['--config', str(config), 'service', 'delivery', action]) == 0
    assert 'registration verified' in capsys.readouterr().out


@pytest.mark.parametrize('worker', ['worker', 'jobs'])
def test_missing_explicit_config_never_falls_back(tmp_path, worker, capsys):
    from self_improve.cli import main
    assert main(['--config', str(tmp_path / 'missing.toml'), worker, '--once']) == 2
    assert 'config does not exist' in capsys.readouterr().err


def test_installed_delivery_chain_restart_does_not_duplicate_write(service_env, tmp_path):
    from self_improve.config import load_config
    from tests.test_delivery_worker import approve, propose
    services, _, config, runner, agents = service_env
    target = tmp_path / 'rules.md'
    target.write_text('before\n')
    with config.open('a') as handle:
        handle.write(f'global_claude_md = {json.dumps(str(target))}\n')
    cfg = load_config(config)
    env = services, cfg, config, runner, agents
    with closing(Store(cfg.state_path('state.db'))) as store:
        proposal = propose(store, target)
        approved = approve(store, cfg, proposal)
    operate(env, 'delivery', 'install')
    assert target.read_text() == 'before\n'  # preflight must not execute intent
    data = plistlib.loads((agents / 'delivery.plist').read_bytes())
    for _ in range(2):
        result = subprocess.run(data['ProgramArguments'] + ['--once'], env=dict(os.environ, **data['EnvironmentVariables']), capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    assert target.read_text() == 'before\nafter\n'
    with closing(Store(cfg.state_path('state.db'), read_only=True)) as store:
        assert store.query_one('SELECT state FROM commands WHERE id=?', (approved['id'],))['state'] == 'completed'
        assert len(store.query("SELECT * FROM proposal_events WHERE event='applied'")) == 1


def test_unknown_registration_probe_preserves_service(service_env):
    services, _, _, runner, agents = service_env
    operate(service_env, 'delivery', 'install')
    before = (agents / 'delivery.plist').read_bytes()
    for action in ('install', 'uninstall'):
        runner.failures['print'] = 1
        with pytest.raises(services.ServiceError, match='cannot be determined'):
            operate(service_env, 'delivery', action)
        assert (agents / 'delivery.plist').read_bytes() == before
        assert runner.registered


def test_status_does_not_report_probe_error_as_absence(service_env):
    _, _, _, runner, _ = service_env
    runner.failures['print'] = 1
    assert 'registration unknown' in operate(service_env, 'delivery', 'status')


@pytest.mark.parametrize('failure', ['missing_script', 'non_executable', 'missing_config', 'old_runtime', 'wrong_state', 'lint', 'symlink'])
def test_install_preconditions_refuse_before_registration(service_env, tmp_path, failure):
    services, cfg, config, runner, agents = service_env
    if failure == 'missing_script':
        (Path(cfg.production_repo_path) / 'ops' / 'run-worker.sh').unlink()
    elif failure == 'non_executable':
        (Path(cfg.production_repo_path) / 'ops' / 'run-worker.sh').chmod(0o600)
    elif failure == 'missing_config':
        config.unlink()
    elif failure in {'old_runtime', 'wrong_state'}:
        value = dict(services.check_runtime(cfg), worker='delivery')
        value['migration_source_sha256' if failure == 'old_runtime' else 'database'] = 'wrong'
        runner.preflight_reply = (0, json.dumps(value), '')
    elif failure == 'lint':
        original = runner
        def runner(cmd, *, env=None):
            if cmd[0] == '/usr/bin/plutil':
                return subprocess.CompletedProcess(cmd, 1, '', 'bad plist')
            return original(cmd, env=env)
        service_env = services, cfg, config, runner, agents
    else:
        agents.mkdir()
        (agents / 'delivery.plist').symlink_to(tmp_path / 'missing')
    with pytest.raises(services.ServiceError):
        operate(service_env, 'delivery', 'install')
    if hasattr(runner, 'calls'):
        assert all(cmd[0] != '/bin/launchctl' for cmd in runner.calls)


def test_first_bootstrap_failure_removes_only_new_owned_plist(service_env):
    services, _, _, runner, agents = service_env
    runner.failures['bootstrap'] = 1
    with pytest.raises(services.ServiceError, match='prior state restored'):
        operate(service_env, 'delivery', 'install')
    assert not (agents / 'delivery.plist').exists() and not runner.registered


def test_failed_uninstall_retains_plist(service_env):
    services, _, _, runner, agents = service_env
    operate(service_env, 'delivery', 'install')
    before = (agents / 'delivery.plist').read_bytes()
    runner.failures['bootout'] = 1
    with pytest.raises(services.ServiceError, match='plist retained'):
        operate(service_env, 'delivery', 'uninstall')
    assert (agents / 'delivery.plist').read_bytes() == before and runner.registered


def test_unmanaged_registration_cannot_be_replaced_or_removed(service_env):
    services, _, _, runner, _ = service_env
    runner.registered.add(services.launchd._gui_domain() + '/com.self-improve.worker')
    for action in ('install', 'uninstall'):
        with pytest.raises(services.ServiceError, match='ownership cannot be verified'):
            operate(service_env, 'delivery', action)
    assert len(runner.registered) == 1


def test_installed_model_chain_retains_budget_across_process_restart(service_env, tmp_path, monkeypatch):
    from self_improve.config import load_config
    from self_improve.commands import submit_command, command_status
    from self_improve import jobs
    from tests.test_apply import insert_proposal, make_diff, OLD, NEW
    services, _, config, runner, agents = service_env
    paths = {'global_claude_md': 'global.md', 'codex_global_agents_md': 'AGENTS.md',
             'skills_dir': 'skills', 'codex_skills_dir': 'codex-skills',
             'claude_projects_dir': 'claude-projects', 'claude_history_path': 'claude-history.jsonl',
             'codex_sessions_dir': 'codex-sessions', 'codex_archived_dir': 'codex-archive'}
    with config.open('a') as handle:
        handle.write('eval_sandbox_enabled = false\n')
        for key, name in paths.items():
            handle.write(f'{key} = {json.dumps(str(tmp_path / name))}\n')
    cfg = load_config(config)
    env = services, cfg, config, runner, agents
    target = tmp_path / 'rule.md'
    target.write_text(OLD)
    with closing(Store(cfg.state_path('state.db'))) as store:
        proposal = insert_proposal(store, target=target, diff=make_diff(OLD, NEW), status='pending')
        shown = jobs.preview(store, cfg, proposal['id'])
        command = submit_command(store, cfg, {'action': 'regenerate_eval', 'request_key': 'synthetic-job', 'proposal_id': proposal['id'], 'preview_revision': shown['revision']})
    # Install preflight must leave a queued paid job entirely untouched.
    operate(env, 'models', 'install')
    with closing(Store(cfg.state_path('state.db'), read_only=True)) as store:
        assert store.query('SELECT * FROM job_calls') == []
        assert command_status(store, command['id'])['state'] == 'queued'
    data = plistlib.loads((agents / 'models.plist').read_bytes())
    uv = Path(data['EnvironmentVariables']['SI_UV'])
    uv.write_text('''#!/usr/bin/env service-python
import os,sys
sys.path.insert(0, REPO_LITERAL)
from self_improve.llm import LLMRunner
from self_improve import cli,job_worker
from tests.test_model_jobs import synthetic_execute
LLMRunner._execute = synthetic_execute
def checkpoint(event):
    if event == 'step_completed' and os.environ.get('FIXTURE_CRASH'):
        os._exit(77)
job_worker._checkpoint = checkpoint
assert sys.argv[1:5] == ['run','--no-sync','--frozen','selfimprove']
raise SystemExit(cli.main(sys.argv[5:]))
'''.replace('REPO_LITERAL', repr(str(REPO))))
    runtime_env = dict(os.environ, **data['EnvironmentVariables'], PYTHONPATH=os.pathsep.join([str(REPO / 'src'), str(REPO)]))
    crashed = subprocess.run(data['ProgramArguments'] + ['--once'], env=dict(runtime_env, FIXTURE_CRASH='1'), capture_output=True, text=True, timeout=30)
    assert crashed.returncode == 77, crashed.stderr
    with closing(Store(cfg.state_path('state.db'), read_only=True)) as store:
        before = command_status(store, command['id'])['budget']
        assert len(store.query('SELECT * FROM job_calls')) == 1
    for _ in range(2):
        result = subprocess.run(data['ProgramArguments'] + ['--once'], env=runtime_env, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
    with closing(Store(cfg.state_path('state.db'), read_only=True)) as store:
        completed = command_status(store, command['id'])
        assert completed['state'] == 'completed', completed
        assert completed['budget']['maximum'] == before['maximum']
        assert len(store.query('SELECT * FROM job_calls')) == 21
        assert len(store.query('SELECT * FROM llm_calls')) == 21
        assert len(store.query('SELECT * FROM proposal_eval_history')) == 1
    assert target.read_text() == OLD


@pytest.mark.parametrize('script_name', ['run-worker.sh', 'run-jobs.sh'])
def test_service_script_cannot_inherit_another_python_or_uv_project(tmp_path, script_name):
    cfg, config = fixture_config(tmp_path)
    fake = tmp_path / 'uv'
    fake.write_text('''#!/bin/bash
for name in PYTHONPATH PYTHONHOME VIRTUAL_ENV UV_PROJECT UV_WORKING_DIR UV_PROJECT_ENVIRONMENT UV_ENV_FILE UV_CONFIG_FILE UV_ISOLATED; do
    if /usr/bin/printenv "$name" >/dev/null; then
        echo "inherited runtime redirect: $name" >&2
        exit 7
    fi
done
printf '%s\\n' "$PWD" "$@"
''')
    fake.chmod(0o755)
    redirects = dict.fromkeys(('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV', 'UV_PROJECT', 'UV_WORKING_DIR', 'UV_PROJECT_ENVIRONMENT', 'UV_ENV_FILE', 'UV_CONFIG_FILE', 'UV_ISOLATED'), str(tmp_path / 'wrong-runtime'))
    result = subprocess.run(['/bin/bash', str(Path(cfg.production_repo_path) / 'ops' / script_name), '--check'], env=dict(redirects, PATH='/usr/bin:/bin', SI_UV=str(fake), SI_CONFIG=str(config), SI_PATH_EXTRA=str(tmp_path)), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [cfg.production_repo_path, 'run', '--no-sync', '--frozen', 'selfimprove', '--config', str(config), 'worker' if script_name == 'run-worker.sh' else 'jobs', '--check']
