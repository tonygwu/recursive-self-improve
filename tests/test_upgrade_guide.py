"""Execute the published upgrade instructions against invented old-schema state."""
from contextlib import closing
import json
from pathlib import Path
import subprocess
import sys

import pytest

from self_improve import store as storage

ROOT = Path(__file__).resolve().parents[1]


def instructions():
    text = (ROOT / 'docs/UPGRADING.md').read_text()
    opening = "<<'PY'\n"
    assert text.count(opening) == 1
    return text.split(opening, 1)[1].split('\nPY\n', 1)[0]


@pytest.fixture
def old_database(tmp_path, monkeypatch):
    directory = tmp_path / 'state'
    config = tmp_path / 'config.toml'
    config.write_text('state_dir = ' + json.dumps(str(directory)) + '\n')
    index = next(i for i, (name, _) in enumerate(storage.MIGRATIONS)
                 if name == '0024_rule_availability')
    with monkeypatch.context() as patch:
        patch.setattr(storage, 'MIGRATIONS', storage.MIGRATIONS[:index])
        db = storage.Store(directory / 'state.db')
    db.conn.execute('CREATE TABLE upgrade_fixture (value TEXT NOT NULL)')
    db.conn.execute('INSERT INTO upgrade_fixture VALUES (?)', ('Invented retained evidence',))
    db.commit()
    # Keep the idle connection open so the backup must include committed WAL data.
    assert db.query_one('PRAGMA journal_mode')['journal_mode'] == 'wal'
    try:
        yield config, db, tmp_path / 'backup.db'
    finally:
        db.close()


def run_guide(config, backup):
    return subprocess.run([sys.executable, '-', str(config), str(backup)],
                          input=instructions(), text=True, capture_output=True,
                          cwd=ROOT)


def test_upgrade_preserves_old_rows_and_verified_preupgrade_backup(old_database):
    config, db, backup = old_database
    result = run_guide(config, backup)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'PREVIEW_UPGRADE_OK' in result.stdout
    required = {'0024_rule_availability', '0028_instruction_inventory', '0023_eval_attempts'}
    assert required <= {r['name'] for r in db.query('SELECT name FROM schema_migrations')}
    assert db.query('SELECT * FROM upgrade_fixture') == [{'value': 'Invented retained evidence'}]
    with closing(storage.Store(backup, read_only=True)) as saved:
        assert not required.intersection(r['name'] for r in saved.query('SELECT name FROM schema_migrations'))
        assert saved.query('SELECT * FROM upgrade_fixture') == db.query('SELECT * FROM upgrade_fixture')
        assert saved.query_one('PRAGMA quick_check') == {'quick_check': 'ok'}


def test_upgrade_refuses_existing_backup_before_migration(old_database):
    config, db, backup = old_database
    backup.write_text('Keep this existing backup')
    before = list(db.conn.iterdump())
    result = run_guide(config, backup)
    assert result.returncode != 0
    assert 'Choose a new backup file' in result.stderr
    assert backup.read_text() == 'Keep this existing backup'
    assert list(db.conn.iterdump()) == before


def test_upgrade_refuses_missing_explicit_config(tmp_path):
    backup = tmp_path / 'backup.db'
    result = run_guide(tmp_path / 'missing.toml', backup)
    assert result.returncode != 0
    assert 'explicit configuration file does not exist' in result.stderr
    assert not backup.exists()


def test_upgrade_refuses_missing_database(tmp_path):
    config = tmp_path / 'config.toml'
    config.write_text('state_dir = ' + json.dumps(str(tmp_path / 'missing-state')) + '\n')
    backup = tmp_path / 'backup.db'
    result = run_guide(config, backup)
    assert result.returncode != 0
    assert 'configured database does not exist' in result.stderr
    assert not backup.exists()
    assert not (tmp_path / 'missing-state').exists()
