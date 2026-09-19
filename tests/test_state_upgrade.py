"""Explicit upgrades use invented temporary databases; no providers or private state."""
from contextlib import closing
import sqlite3

import pytest

from self_improve import store as store_module
from self_improve.cli import main
from self_improve.store import Store


def test_failed_migration_keeps_no_partial_schema(tmp_path, monkeypatch):
    db = tmp_path / 'state.db'
    with closing(Store(db)) as store:
        before = store.schema_tables()
    monkeypatch.setattr(store_module, 'MIGRATIONS', [*store_module.MIGRATIONS,
        ('9998_fixture_failure', 'CREATE TABLE must_roll_back (id TEXT);\nSELECT missing_column FROM missing_table;')])
    with pytest.raises(sqlite3.OperationalError, match='missing_table'):
        Store(db)
    with closing(Store(db, read_only=True)) as store:
        assert store.schema_tables() == before
        assert not store.query("SELECT * FROM schema_migrations WHERE name='9998_fixture_failure'")


def test_upgrade_cli_preview_preserves_existing_state(tmp_path, capsys):
    state = tmp_path / 'state'
    with closing(Store(state / 'state.db')) as store:
        before = store.schema_tables()
    config = tmp_path / 'config.toml'
    config.write_text(f'state_dir = "{state}"\n')
    assert main(['--config', str(config), 'upgrade-state', '--dry-run']) == 0
    assert 'pending' in capsys.readouterr().out
    with closing(Store(state / 'state.db', read_only=True)) as store:
        assert store.schema_tables() == before


@pytest.fixture
def old_state(tmp_path, monkeypatch):
    from self_improve.config import Config
    cfg = Config(state_dir=str(tmp_path / 'state'))
    migrations = store_module.MIGRATIONS
    with monkeypatch.context() as context:
        context.setattr(store_module, 'MIGRATIONS', migrations[:-1])
        with closing(Store(cfg.state_path('state.db'))) as store:
            store.insert('runs', {'id': 'invented', 'started': '2026-01-01T00:00:00Z', 'status': 'ok'})
            store.commit()
    return cfg


def _snapshot(cfg):
    with closing(Store(cfg.state_path('state.db'), read_only=True)) as store:
        return Store._logical_digest(store.conn)


def test_upgrade_copies_committed_wal_data_and_preserves_policy(old_state, tmp_path):
    import json
    from pathlib import Path
    from self_improve.data_boundary import verify_files
    from self_improve.execution_policy import set_class_policy
    from self_improve.state_upgrade import upgrade_state
    cfg = old_state
    destination = tmp_path / 'verified-backup'
    with closing(Store(cfg.state_path('state.db'), migrate=False)) as writer:
        writer.conn.execute('PRAGMA wal_autocheckpoint=0')
        set_class_policy(writer, 'global', True)
        writer.insert('runs', {'id': 'wal-only', 'started': '2026-01-02T00:00:00Z', 'status': 'ok'})
        writer.commit()
        assert Path(str(cfg.state_path('state.db')) + '-wal').stat().st_size > 0
        before = _snapshot(cfg)
        result = upgrade_state(cfg, backup=destination)
        assert result['state'] == 'upgraded'
        assert result['applied_now'] == [store_module.MIGRATIONS[-1][0]]
        manifest = verify_files(destination)
        assert manifest['logical_sha256'] == before
        assert result['backup_sha256'] == manifest['sha256']
        assert json.loads((destination / 'manifest.json').read_text()) == manifest
        with closing(Store(destination / 'state.db', read_only=True)) as backup:
            assert Store._logical_digest(backup.conn) == before
            assert backup.query_one("SELECT id FROM runs WHERE id='wal-only'")
            assert backup.migration_plan()['pending'] == result['applied_now']
        assert writer.query_one("SELECT enabled FROM execution_policies WHERE target_class='global'")['enabled'] == 1
        assert writer.migration_plan()['pending'] == []
    assert (destination.stat().st_mode & 0o777) == 0o700
    assert ((destination / 'state.db').stat().st_mode & 0o777) == 0o600
    assert ((destination / 'manifest.json').stat().st_mode & 0o777) == 0o600
    unused = tmp_path / 'unused-backup'
    assert upgrade_state(cfg, backup=unused)['state'] == 'current'
    assert not unused.exists()
    assert upgrade_state(cfg, backup=destination)['state'] == 'current'
    assert verify_files(destination) == manifest


def test_old_state_preview_does_not_create_backup_or_apply_schema(old_state, tmp_path):
    from self_improve.state_upgrade import upgrade_state
    before = _snapshot(old_state)
    destination = tmp_path / 'preview-backup'
    result = upgrade_state(old_state, backup=destination, dry_run=True)
    assert result['state'] == 'pending' and result['pending'] == [store_module.MIGRATIONS[-1][0]]
    assert not destination.exists()
    assert _snapshot(old_state) == before
    with pytest.raises(ValueError, match='require --backup'):
        upgrade_state(old_state)
    assert _snapshot(old_state) == before


@pytest.mark.parametrize('history', ['unknown', 'gap'])
def test_unknown_or_gapped_history_refuses_before_backup(old_state, tmp_path, history):
    from self_improve.state_upgrade import upgrade_state
    with closing(Store(old_state.state_path('state.db'), migrate=False)) as store:
        if history == 'unknown':
            store.insert('schema_migrations', {'name': '9999_unknown', 'applied_at': '2026-01-01'})
        else:
            store.conn.execute('DELETE FROM schema_migrations WHERE name=?', (store_module.MIGRATIONS[1][0],))
        store.commit()
    before = _snapshot(old_state)
    destination = tmp_path / 'refused-backup'
    with pytest.raises(ValueError, match='unknown names|recorded subset schema|supported schema'):
        upgrade_state(old_state, backup=destination)
    assert _snapshot(old_state) == before
    assert not destination.exists()


@pytest.mark.parametrize('kind', ['existing', 'git', 'symlink'])
def test_backup_boundary_is_enforced_before_upgrade(old_state, tmp_path, kind):
    from self_improve.data_boundary import DataBoundaryError
    from self_improve.state_upgrade import upgrade_state
    root = tmp_path / 'destination'
    root.mkdir()
    if kind == 'existing':
        destination = root
    else:
        (root / '.git').mkdir()
        destination = root / 'new'
        if kind == 'symlink':
            alias = tmp_path / 'alias'
            alias.symlink_to(root, target_is_directory=True)
            destination = alias / 'new'
    before = _snapshot(old_state)
    with pytest.raises(DataBoundaryError):
        upgrade_state(old_state, backup=destination)
    assert _snapshot(old_state) == before
    assert not (root / 'new').exists()


def test_corrupt_backup_refuses_before_schema_mutation(old_state, tmp_path, monkeypatch):
    from self_improve import data_boundary
    from self_improve.state_upgrade import upgrade_state
    before = _snapshot(old_state)
    original = data_boundary.verify_files

    def corrupt_then_verify(root):
        with (root / 'state.db').open('ab') as handle:
            handle.write(b'corrupted')
        return original(root)

    monkeypatch.setattr(data_boundary, 'verify_files', corrupt_then_verify)
    with pytest.raises(data_boundary.DataBoundaryError, match='sha256/size'):
        upgrade_state(old_state, backup=tmp_path / 'bad-backup')
    assert _snapshot(old_state) == before


@pytest.mark.parametrize('failure', ['sql', 'interrupt'])
def test_upgrade_rolls_back_all_pending_work_and_keeps_verified_backup(old_state, tmp_path, monkeypatch, failure):
    from self_improve.state_upgrade import upgrade_state
    from self_improve.data_boundary import verify_files
    before = _snapshot(old_state)
    if failure == 'sql':
        monkeypatch.setattr(store_module, 'MIGRATIONS', [*store_module.MIGRATIONS,
            ('9998_bad', 'CREATE TABLE must_roll_back (id TEXT); SELECT * FROM missing_table;')])
        expected = sqlite3.OperationalError
    else:
        original = Store._apply_pending_migrations

        def interrupt(store):
            original(store)
            raise KeyboardInterrupt('fixture interrupt before commit')

        monkeypatch.setattr(Store, '_apply_pending_migrations', interrupt)
        expected = KeyboardInterrupt
    destination = tmp_path / 'kept-backup'
    with pytest.raises(expected):
        upgrade_state(old_state, backup=destination)
    assert _snapshot(old_state) == before
    assert verify_files(destination)['logical_sha256'] == before


def test_backup_reservation_excludes_concurrent_writers(old_state, tmp_path, monkeypatch):
    from self_improve.state_upgrade import upgrade_state
    monkeypatch.setattr(store_module, 'BUSY_TIMEOUT_MS', 10)
    original = Store._backup_upgrade_state
    attempts = []

    def competing_write(store, destination, plan):
        with closing(Store(store.db_path, migrate=False)) as other:
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                with other.transaction(write=True):
                    pytest.fail('competing writer entered backup/migration transaction')
        attempts.append(True)
        return original(store, destination, plan)

    monkeypatch.setattr(Store, '_backup_upgrade_state', competing_write)
    assert upgrade_state(old_state, backup=tmp_path / 'exclusive-backup')['state'] == 'upgraded'
    assert attempts == [True]


def test_upgrade_never_commits_pending_caller_work(old_state, tmp_path):
    with closing(Store(old_state.state_path('state.db'), migrate=False)) as store:
        store.insert('runs', {'id': 'pending', 'started': '2026-01-03'})
        with pytest.raises(ValueError, match='pending caller work'):
            store.upgrade_with_backup(tmp_path / 'never-created')
        assert store.conn.in_transaction
        store.conn.rollback()
    assert not (tmp_path / 'never-created').exists()


def test_sqlite_statement_boundaries_preserve_strings_comments_and_triggers(tmp_path, monkeypatch):
    monkeypatch.setattr(store_module, 'MIGRATIONS', [('0001_fixture', '''
        CREATE TABLE inputs (value TEXT);
        CREATE TABLE outputs (value TEXT);
        -- a comment with a semicolon ; remains SQL whitespace
        CREATE TRIGGER record_input AFTER INSERT ON inputs BEGIN
          INSERT INTO outputs VALUES ('first;item');
          INSERT INTO outputs VALUES (NEW.value);
        END;
        INSERT INTO inputs VALUES ('second;item');
    ''')])
    with closing(Store(tmp_path / 'statement.db')) as store:
        assert store.query('SELECT value FROM outputs') == [{'value': 'first;item'}, {'value': 'second;item'}]
        assert store.migration_plan()['pending'] == []


def test_initialization_is_explicit_atomic_and_starts_all_classes_off(tmp_path):
    from self_improve.config import Config
    from self_improve.state_upgrade import upgrade_state
    cfg = Config(state_dir=str(tmp_path / 'fresh-state'))
    with pytest.raises(FileNotFoundError):
        upgrade_state(cfg, dry_run=True)
    assert not cfg.state_path('state.db').parent.exists()
    assert upgrade_state(cfg, initialize=True, dry_run=True)['state'] == 'missing'
    assert not cfg.state_path('state.db').parent.exists()
    assert upgrade_state(cfg, initialize=True)['state'] == 'initialized'
    with closing(Store(cfg.state_path('state.db'), read_only=True)) as store:
        policies = store.query('SELECT enabled FROM execution_policies')
        assert len(policies) == 4 and all(row['enabled'] == 0 for row in policies)
    before = _snapshot(cfg)
    with pytest.raises(ValueError, match='already exists'):
        upgrade_state(cfg, initialize=True)
    assert _snapshot(cfg) == before
    assert not list(cfg.state_path('state.db').parent.glob('.state-init-*'))


def test_failed_initialization_never_publishes_partial_state(tmp_path, monkeypatch):
    from self_improve.config import Config
    from self_improve.state_upgrade import upgrade_state
    cfg = Config(state_dir=str(tmp_path / 'fresh-state'))
    monkeypatch.setattr(store_module, 'MIGRATIONS', [('0001_bad', 'CREATE TABLE t(x); INVALID SQL;')])
    with pytest.raises(sqlite3.Error):
        upgrade_state(cfg, initialize=True)
    assert list(cfg.state_path('state.db').parent.iterdir()) == []


def test_initialization_does_not_overwrite_an_intervening_file(tmp_path, monkeypatch):
    from self_improve.config import Config
    from self_improve import state_upgrade
    cfg = Config(state_dir=str(tmp_path / 'fresh-state'))
    original = state_upgrade.os.link

    def race(source, target):
        target.write_text('intervening owner')
        original(source, target)

    monkeypatch.setattr(state_upgrade.os, 'link', race)
    with pytest.raises(FileExistsError):
        state_upgrade.upgrade_state(cfg, initialize=True)
    assert cfg.state_path('state.db').read_text() == 'intervening owner'
    assert not list(cfg.state_path('state.db').parent.glob('.state-init-*'))


def test_process_exit_before_upgrade_commit_retains_before_state(old_state, tmp_path):
    import subprocess
    import sys
    from self_improve.data_boundary import verify_files
    before = _snapshot(old_state)
    destination = tmp_path / 'crash-backup'
    code = '''
import os,sys
from self_improve.config import Config
from self_improve.store import Store
from self_improve.state_upgrade import upgrade_state
original=Store._apply_pending_migrations
def crash(store):
    original(store)
    os._exit(73)
Store._apply_pending_migrations=crash
upgrade_state(Config(state_dir=sys.argv[1]), backup=sys.argv[2])
'''
    result = subprocess.run([sys.executable, '-c', code, old_state.state_dir, str(destination)],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 73, result.stdout + result.stderr
    assert _snapshot(old_state) == before
    assert verify_files(destination)['logical_sha256'] == before


def test_cli_upgrade_backs_up_before_any_implicit_migration(old_state, tmp_path, capsys):
    import json
    from self_improve.data_boundary import verify_files
    config = tmp_path / 'selected.toml'
    config.write_text(f'state_dir = "{old_state.state_dir}"\n')
    before = _snapshot(old_state)
    destination = tmp_path / 'cli-backup'
    assert main(['--config', str(config), 'upgrade-state', '--backup', str(destination)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['state'] == 'upgraded'
    assert verify_files(destination)['logical_sha256'] == before
    with closing(Store(destination / 'state.db', read_only=True)) as backup:
        assert backup.migration_plan()['pending'] == [store_module.MIGRATIONS[-1][0]]


def test_cli_upgrade_error_is_nonzero_and_keeps_existing_state(old_state, tmp_path, capsys):
    config = tmp_path / 'selected.toml'
    config.write_text(f'state_dir = "{old_state.state_dir}"\n')
    before = _snapshot(old_state)
    assert main(['--config', str(config), 'upgrade-state']) == 2
    result = capsys.readouterr()
    assert 'require --backup' in result.err and not result.out
    assert _snapshot(old_state) == before


def test_initialization_refuses_an_existing_dangling_symlink(tmp_path):
    from self_improve.config import Config
    from self_improve.state_upgrade import upgrade_state
    cfg = Config(state_dir=str(tmp_path / 'state'))
    cfg.state_path('state.db').parent.mkdir()
    target = tmp_path / 'absent.db'
    cfg.state_path('state.db').symlink_to(target)
    with pytest.raises(ValueError, match='already exists'):
        upgrade_state(cfg, initialize=True)
    assert not target.exists()
    assert cfg.state_path('state.db').is_symlink()


def test_explicit_upgrade_refuses_state_in_a_git_checkout(tmp_path):
    from self_improve.config import Config
    from self_improve.data_boundary import DataBoundaryError
    from self_improve.state_upgrade import upgrade_state
    checkout = tmp_path / 'checkout'
    checkout.mkdir()
    (checkout / '.git').mkdir()
    cfg = Config(state_dir=str(checkout / 'state'))
    with pytest.raises(DataBoundaryError, match='Git checkout'):
        upgrade_state(cfg, initialize=True)
    assert not cfg.state_path('state.db').parent.exists()


@pytest.mark.parametrize('missing', ['0009_execution_policy', '0026_scan_incident_links'])
def test_explicit_upgrade_accepts_verified_legacy_subsets(tmp_path, monkeypatch, missing):
    from self_improve.config import Config
    from self_improve.state_upgrade import upgrade_state
    from self_improve.data_boundary import verify_files
    cfg = Config(state_dir=str(tmp_path / 'legacy'))
    with monkeypatch.context() as patch:
        patch.setattr(store_module, 'MIGRATIONS', [m for m in store_module.MIGRATIONS if m[0] != missing])
        Store(cfg.state_path('state.db')).close()
    before = _snapshot(cfg)
    assert upgrade_state(cfg, dry_run=True)['pending'] == [missing]
    destination = tmp_path / 'legacy-backup'
    result = upgrade_state(cfg, backup=destination)
    assert result['applied_now'] == [missing]
    assert verify_files(destination)['logical_sha256'] == before
    with closing(Store(cfg.state_path('state.db'), read_only=True)) as store:
        assert store.migration_plan()['pending'] == []


@pytest.mark.parametrize('value', [None, b'invalid-receipt'])
def test_invalid_migration_receipt_type_fails_with_a_named_error(old_state, value):
    from self_improve.state_upgrade import upgrade_state
    with closing(Store(old_state.state_path('state.db'), migrate=False)) as store:
        store.insert('schema_migrations', {'name': value, 'applied_at': '2026-01-01'})
        store.commit()
    with pytest.raises(ValueError, match='invalid migration name'):
        upgrade_state(old_state, dry_run=True)


@pytest.mark.parametrize('change', [
    'DROP INDEX idx_sessions_project',
    'CREATE INDEX unrecorded_fixture_index ON runs(started)',
])
def test_subset_validation_covers_indexes_before_backup(tmp_path, monkeypatch, change):
    from self_improve.config import Config
    from self_improve.state_upgrade import upgrade_state
    cfg = Config(state_dir=str(tmp_path / 'subset'))
    with monkeypatch.context() as patch:
        patch.setattr(store_module, 'MIGRATIONS', [m for m in store_module.MIGRATIONS if m[0] != '0009_execution_policy'])
        with closing(Store(cfg.state_path('state.db'))) as store:
            store.conn.execute(change)
            store.commit()
    before = _snapshot(cfg)
    destination = tmp_path / 'never-backed-up'
    with pytest.raises(ValueError, match='recorded subset schema'):
        upgrade_state(cfg, backup=destination)
    assert _snapshot(cfg) == before
    assert not destination.exists()
