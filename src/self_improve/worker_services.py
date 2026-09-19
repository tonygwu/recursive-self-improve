"""Explicit worker service maintenance; registration is not worker health."""
from contextlib import closing
import json
import os
from pathlib import Path
import plistlib
import shutil
import sqlite3
import subprocess
import tempfile

from . import launchd
from .config import load_config
from .data_boundary import DataBoundaryError, private_storage_path
from .store import Store

SERVICES = {'delivery': ('com.self-improve.worker', 'run-worker.sh', 'worker'),
            'models': ('com.self-improve.jobs', 'run-jobs.sh', 'jobs')}


class ServiceError(ValueError):
    """A service step failed; its message includes the available evidence."""


def check_runtime(cfg):
    """Read current schema and selected identity without creating or migrating."""
    try:
        path = private_storage_path(cfg.state_path('state.db'))
        expected = os.environ.get('SI_SERVICE_STATE_DB')
        if expected and path != Path(expected).resolve():
            raise ValueError('Selected database changed; reinstall the service with its intended config')
        with closing(Store(path, read_only=True)) as store:
            plan = store.migration_plan()
        if plan['pending']:
            raise ValueError('Pending migrations: ' + ', '.join(plan['pending']))
    except (DataBoundaryError, OSError, ValueError, sqlite3.Error) as exc:
        raise ServiceError(f'Worker preflight failed: {exc}. Upgrade state explicitly with upgrade-state before starting workers.') from exc
    return {'state': 'current', 'database': str(path),
            'migration_source_sha256': plan['migration_source_sha256']}


def _run(cmd, *, env=None):
    return subprocess.run(cmd, env=env, capture_output=True, text=True)


def _write_plist(path, content):
    """Replace one owned service file atomically, with private permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.self-improve-', suffix='.plist', dir=path.parent)
    staged = Path(name)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(content)
        os.replace(staged, path)
    finally:
        staged.unlink(missing_ok=True)


def _existing(path, service, config):
    if path.is_symlink():
        raise ServiceError(f'Refusing a symlink service plist: {path}')
    if not path.exists():
        return None
    raw = path.read_bytes()
    try:
        data = plistlib.loads(raw)
        managed = data['Label'] == SERVICES[service][0] and data['EnvironmentVariables']['SI_CONFIG'] == str(config)
    except (KeyError, TypeError, ValueError, plistlib.InvalidFileException):
        managed = False
    if not managed:
        raise ServiceError(f'Service plist belongs to another config or lacks managed identity: {path}')
    return raw


def service_plist(cfg, service, config, *, uv_path):
    label, script, stem = SERVICES[service]
    data = launchd.worker_plist_content(cfg)
    data.update(Label=label,
                ProgramArguments=['/bin/bash', str(Path(cfg.production_repo_path) / 'ops' / script)],
                StandardOutPath=str(cfg.state_path('logs', stem + '.stdout.log')),
                StandardErrorPath=str(cfg.state_path('logs', stem + '.stderr.log')),
                EnvironmentVariables={
                    'SI_CONFIG': str(config),
                    'SI_SERVICE_STATE_DB': str(cfg.state_path('state.db').resolve()),
                    'SI_UV': str(uv_path),
                    'PATH': '/usr/bin:/bin:/usr/sbin:/sbin',
                    'SI_PATH_EXTRA': os.environ.get('SI_PATH_EXTRA', f'{Path.home()}/.local/bin:/opt/homebrew/bin:/usr/local/bin'),
                })
    return data


def manage(cfg, service, action, *, config_path=None, run_cmd=None, agent_plist_path=None):
    """Install, remove or inspect one worker. Tests supply a fake command runner."""
    if service not in SERVICES or action not in {'install', 'uninstall', 'status'}:
        raise ServiceError('Unknown worker service or action')
    run_cmd = run_cmd or _run
    config = Path(config_path or Path.home() / '.self-improve' / 'config.toml').expanduser().resolve()
    label = SERVICES[service][0]
    dst = Path(agent_plist_path) if agent_plist_path is not None else Path.home() / 'Library' / 'LaunchAgents' / f'{label}.plist'
    target = f'{launchd._gui_domain()}/{label}'
    steps = []

    def command(args, *, env=None):
        try:
            result = run_cmd(args, env=env)
        except OSError as exc:
            raise ServiceError('Service command failed: ' + str(exc) + '\n' + '\n'.join(steps)) from exc
        steps.append(launchd._evidence(result))
        return result

    def failed(message):
        return ServiceError(message + '\n' + '\n'.join(steps))

    def registration():
        probe = command([launchd._LAUNCHCTL, 'print', target])
        if probe.returncode not in (0, 113):
            raise failed('Registration cannot be determined; service files retained')
        return probe.returncode == 0

    if action == 'status':
        # No state or config read is needed to report registration.
        probe = command([launchd._LAUNCHCTL, 'print', target])
        try:
            raw = _existing(dst, service, config)
            identity = 'config matches' if raw is not None else 'plist missing'
            if raw is not None:
                actual = plistlib.loads(raw)['EnvironmentVariables'].get('SI_SERVICE_STATE_DB')
                identity += '; database matches' if actual == str(cfg.state_path('state.db').resolve()) else '; database differs'
        except (ServiceError, OSError) as exc:
            identity = str(exc)
        registered = {0: 'registered', 113: 'NOT registered'}.get(probe.returncode, 'registration unknown')
        return f'{label}: {registered}; {identity}. Registration does not prove worker health.\n' + '\n'.join(steps)

    old = _existing(dst, service, config)
    if action == 'uninstall':
        if registration():
            if old is None:
                raise failed('Registered service has no managed plist; ownership cannot be verified')
            if command([launchd._LAUNCHCTL, 'bootout', target]).returncode:
                raise failed('launchctl bootout failed; plist retained')
            if registration():
                raise failed('Service still registered after bootout; plist retained')
        if old is not None:
            dst.unlink()
            steps.append(f'removed {dst}')
        else:
            steps.append('service already absent')
        return '\n'.join(steps)

    if not config.is_file():
        raise ServiceError(f'Service installation requires an existing config: {config}')
    if load_config(config) != cfg:
        raise ServiceError('Selected config changed during installation; retry with the intended config')
    for name in ('state_dir', 'production_repo_path'):
        if not Path(getattr(cfg, name)).is_absolute():
            raise ServiceError(f'{name} must be an absolute path for service installation')
    expected = check_runtime(cfg)
    if old is not None and plistlib.loads(old)['EnvironmentVariables'].get('SI_SERVICE_STATE_DB') != expected['database']:
        raise ServiceError('Existing service selects another database; uninstall it before selecting a different state')
    uv_path = Path(os.environ.get('SI_UV') or shutil.which('uv') or '')
    if not uv_path.is_absolute() or not uv_path.is_file() or not os.access(uv_path, os.X_OK):
        raise ServiceError('Install uv or set SI_UV to its absolute executable path before service installation')
    data = service_plist(cfg, service, config, uv_path=uv_path)
    script = Path(data['ProgramArguments'][1])
    if not script.is_file() or not os.access(script, os.X_OK):
        raise ServiceError(f'Service script missing or not executable: {script}')
    env = dict(os.environ, **data['EnvironmentVariables'])
    preflight = command(data['ProgramArguments'] + ['--check'], env=env)
    if preflight.returncode:
        raise failed('Production worker preflight failed; no registration changed')
    try:
        actual = json.loads(preflight.stdout)
    except (ValueError, TypeError):
        actual = None
    if actual != dict(expected, worker=service):
        raise failed('Production worker preflight identity/schema mismatch; no registration changed')
    if load_config(config) != cfg:
        raise failed('Selected config changed during preflight; no registration changed')
    rendered = plistlib.dumps(data)
    with tempfile.TemporaryDirectory(prefix='self-improve-service-') as temp:
        staged = Path(temp) / 'service.plist'
        staged.write_bytes(rendered)
        if command([launchd._PLUTIL, '-lint', str(staged)]).returncode:
            raise failed('plutil -lint failed; no registration changed')
    registered = registration()
    if registered and old is None:
        raise failed('Registered service has no managed plist; ownership cannot be verified')
    cfg.state_path('logs').mkdir(parents=True, exist_ok=True, mode=0o700)
    if registered and command([launchd._LAUNCHCTL, 'bootout', target]).returncode:
        raise failed('launchctl bootout failed; prior plist retained')
    if registered and registration():
        raise failed('Service still registered after bootout; prior plist retained')
    attempted_bootstrap = False
    try:
        _write_plist(dst, rendered)
        attempted_bootstrap = True
        if command([launchd._LAUNCHCTL, 'bootstrap', launchd._gui_domain(), str(dst)]).returncode:
            raise failed('launchctl bootstrap failed')
        if command([launchd._LAUNCHCTL, 'print', target]).returncode:
            raise failed('Registration NOT verified after bootstrap')
    except (ServiceError, OSError) as exc:
        steps.append(str(exc))
        # Do not hide uncertain registration after a failed bootstrap/verification.
        try:
            if attempted_bootstrap and registration():
                if command([launchd._LAUNCHCTL, 'bootout', target]).returncode:
                    raise failed('Replacement cleanup failed; current plist retained for inspection')
            if old is None:
                dst.unlink(missing_ok=True)
            else:
                _write_plist(dst, old)
                steps.append('restored prior plist')
            if registered:
                restored = command([launchd._LAUNCHCTL, 'bootstrap', launchd._gui_domain(), str(dst)])
                checked = command([launchd._LAUNCHCTL, 'print', target])
                if restored.returncode or checked.returncode:
                    raise failed('Prior registration restoration failed; prior plist retained')
                steps.append('prior registration restored')
        except (ServiceError, OSError) as restore_exc:
            raise failed(f'Installation failed; restoration incomplete: {restore_exc}') from exc
        raise failed('Installation failed; prior state restored') from exc
    steps.append(f'{label}: registration verified; config={config}; database={expected["database"]}. Worker health requires runtime evidence.')
    return '\n'.join(steps)
