"""Startup advice targets invented selected state without starting runtime work."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shlex
import sqlite3

import pytest

from self_improve import cli, store as store_module
from self_improve.config import Config
from self_improve.dashboard import app as dashboard
from self_improve.execution_policy import policy_snapshot
from self_improve.store import Store


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    cfg = Config(state_dir=str(tmp_path / 'unrelated-configured-state'))
    with closing(Store(cfg.state_path('state.db'))):
        pass
    before = digest(cfg.state_path('state.db'))
    monkeypatch.setattr(cli, 'load_config', lambda path: cfg)
    monkeypatch.setattr(cli, '_dispatch', lambda *args: pytest.fail('maintenance started runtime dispatch'))
    yield cfg
    assert digest(cfg.state_path('state.db')) == before


def startup_preview(path):
    with pytest.raises(dashboard.DashboardStartupError) as error:
        dashboard.open_read_only_store(path)
    message = str(error.value)
    assert str(path) in message
    assert 'run --dry-run' not in message
    commands = [shlex.split(line.strip()) for line in message.splitlines()
                if line.strip().startswith('uv run selfimprove upgrade-state ')]
    assert len(commands) == 1, message
    args = commands[0][3:]
    assert args[args.index('--database') + 1] == str(path)
    assert '--dry-run' in args
    return message, args


def test_missing_state_preview_and_explicit_initialization_target_the_selected_copy(isolated, tmp_path, capsys):
    selected = tmp_path / "selected 'quoted' folder" / 'custom state.db'
    message, args = startup_preview(selected)
    assert '--initialize' in args and 'without --dry-run' in message
    assert cli.main(args) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview['database'] == str(selected) and preview['action'] == 'initialize'
    assert not selected.parent.exists()
    assert cli.main([a for a in args if a != '--dry-run']) == 0
    initialized = json.loads(capsys.readouterr().out)
    assert initialized['database'] == str(selected) and initialized['state'] == 'initialized'
    with closing(Store(selected, read_only=True)) as reader:
        assert not reader.migration_plan()['pending']
        assert all(not row['enabled'] for row in policy_snapshot(reader)['classes'].values())


@pytest.mark.parametrize('unknown_table', [False, True])
def test_existing_state_preview_never_initializes_or_guesses_unknown_history(isolated, tmp_path, capsys, unknown_table):
    selected = tmp_path / "existing 'quoted' copy.db"
    with closing(sqlite3.connect(selected)) as db:
        if unknown_table:
            db.execute('CREATE TABLE invented_unrelated (value TEXT)')
            db.execute("INSERT INTO invented_unrelated VALUES ('preserve me')")
            db.commit()
    before = digest(selected)
    message, args = startup_preview(selected)
    assert '--initialize' not in args and '--backup' in message
    assert 'no migration history' in message
    assert cli.main(args) == (2 if unknown_table else 0)
    output = capsys.readouterr()
    if unknown_table:
        assert 'tables but no migration history' in output.err and not output.out
    else:
        preview = json.loads(output.out)
        assert preview['database'] == str(selected) and preview['state'] == 'pending'
    assert digest(selected) == before


def test_explicit_copy_upgrade_preserves_configured_state_and_verifies_its_own_backup(isolated, tmp_path, monkeypatch, capsys):
    selected = tmp_path / "upgrade 'quoted' copy.db"
    with monkeypatch.context() as patch:
        patch.setattr(store_module, 'MIGRATIONS', store_module.MIGRATIONS[:-1])
        with closing(Store(selected)):
            pass
    before = digest(selected)
    assert cli.main(['upgrade-state', '--database', str(selected), '--dry-run']) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview['database'] == str(selected) and preview['pending'] == [store_module.MIGRATIONS[-1][0]]
    assert digest(selected) == before
    backup = tmp_path / 'new-private-backup'
    assert cli.main(['upgrade-state', '--database', str(selected), '--backup', str(backup)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['database'] == str(selected) and result['state'] == 'upgraded'
    with closing(Store(selected, read_only=True)) as reader:
        assert not reader.migration_plan()['pending']
    with closing(Store(backup / 'state.db', read_only=True)) as reader:
        assert reader.migration_plan()['pending'] == preview['pending']


def test_wal_failure_does_not_suggest_unquoted_or_implicit_database_mutation(tmp_path):
    selected = tmp_path / "quoted ' database.db"
    with closing(Store(selected)):
        pass
    message = dashboard._open_failure_message(selected, sqlite3.OperationalError('unable to open database file'))
    assert str(selected) in message and 'WAL' in message and '-shm' in message
    assert 'will NOT retry with a writable handle' in message
    assert 'journal_mode=DELETE' not in message
    assert 'consistent SQLite backup' in message


def test_explicit_selector_still_refuses_checkout_storage(isolated, tmp_path, capsys):
    checkout = tmp_path / 'invented-checkout'
    (checkout / '.git').mkdir(parents=True)
    selected = checkout / 'state.db'
    assert cli.main(['upgrade-state', '--database', str(selected), '--initialize', '--dry-run']) == 2
    output = capsys.readouterr()
    assert 'Git checkout' in output.err and not output.out
    assert not selected.exists()


def test_preview_pins_a_relative_selection_before_the_operator_changes_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = Path('selected state') / 'state.db'
    args = shlex.split(dashboard._upgrade_preview(path, initialize=True))
    assert args[args.index('--database') + 1] == str(tmp_path / path)


def test_database_disappearing_during_open_keeps_the_original_startup_error(tmp_path, monkeypatch):
    selected = tmp_path / 'vanishing.db'
    selected.touch()
    attempts = []
    def failed_open(path, *, read_only):
        attempts.append(read_only)
        selected.unlink()
        raise sqlite3.OperationalError('controlled open failure')
    monkeypatch.setattr(dashboard, 'Store', failed_open)
    with pytest.raises(dashboard.DashboardStartupError) as error:
        dashboard.open_read_only_store(selected)
    assert 'controlled open failure' in str(error.value)
    assert 'size unavailable' in str(error.value)
    assert attempts == [True]


def test_unreadable_sidecar_metadata_is_unknown_not_absent(tmp_path, monkeypatch):
    selected = tmp_path / 'state.db'
    wal = selected.with_name('state.db-wal')
    wal.touch()
    stat = Path.stat
    def denied(path, *args, **kwargs):
        if path == wal:
            raise PermissionError('invented metadata refusal')
        return stat(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'stat', denied)
    message = dashboard._sidecar_report(selected)
    assert str(wal) + ': unavailable (PermissionError)' in message
    assert str(selected.with_name('state.db-shm')) + ': absent' in message
