"""Explicit release maintenance. No transcript reads, services, targets or models."""
from contextlib import closing
from pathlib import Path
import os
import tempfile

from .data_boundary import private_destination, private_storage_path
from .store import Store


def upgrade_state(cfg, *, database=None, backup=None, initialize=False, dry_run=False):
    selected_path = Path(database).expanduser() if database is not None else cfg.state_path('state.db')
    if initialize and (selected_path.exists() or selected_path.is_symlink()):
        raise ValueError('State already exists; use an upgrade with --backup instead')
    path = private_storage_path(selected_path)
    if backup is not None and initialize:
        raise ValueError('--backup and --initialize are mutually exclusive')
    backup = Path(backup).expanduser() if backup is not None else None
    if backup is not None:
        private_storage_path(backup)
    if initialize:
        if path.exists():
            raise ValueError('State already exists; use an upgrade with --backup instead')
        if dry_run:
            return {'state': 'missing', 'database': str(path), 'action': 'initialize', 'dry_run': True}
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, name = tempfile.mkstemp(prefix='.state-init-', suffix='.db', dir=path.parent)
        os.close(descriptor)
        staged = Path(name)
        try:
            with closing(Store(staged)) as store:
                plan = store.migration_plan()
            # link publishes atomically and refuses to replace an intervening file.
            os.link(staged, path)
        finally:
            for suffix in ('', '-wal', '-shm'):
                Path(str(staged) + suffix).unlink(missing_ok=True)
        return {'state': 'initialized', 'database': str(path), **plan, 'backup': None}
    with closing(Store(path, read_only=True)) as store:
        plan = store.migration_plan()
    if dry_run:
        if plan['pending'] and backup is not None:
            private_destination(backup)
        return {'state': 'pending' if plan['pending'] else 'current',
                'database': str(path), **plan, 'dry_run': True}
    with closing(Store(path, migrate=False)) as store:
        return {'database': str(path), **store.upgrade_with_backup(backup)}
