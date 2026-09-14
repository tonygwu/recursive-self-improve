"""The shipped inspector must remain read-only even for hostile run arguments."""
from contextlib import closing
import os
from pathlib import Path
import subprocess

from self_improve.store import Store

SCRIPT = Path(__file__).resolve().parents[1] / 'ops/inspect-gate-verdicts.sh'


def inspect(state, run=None):
    return subprocess.run(
        ['bash', str(SCRIPT), *([] if run is None else [run])],
        env={**os.environ, 'SI_STATE_DIR': str(state)},
        capture_output=True, text=True, timeout=20,
    )


def seed(state):
    with closing(Store(state / 'state.db')) as store:
        store.insert('runs', {'id': 'example-run', 'started': '2026-01-01T00:00:00Z',
                             'status': 'ok', 'stats_json': '{"gate":{"attempted":1}}'})
        store.commit()


def test_missing_database_is_not_created(tmp_path):
    state = tmp_path / 'state'
    state.mkdir()
    result = inspect(state)
    assert result.returncode != 0
    assert 'database' in result.stderr.lower()
    assert list(state.iterdir()) == []


def test_run_argument_cannot_execute_sql(tmp_path):
    seed(tmp_path)
    before = (tmp_path / 'state.db').read_bytes()
    result = inspect(tmp_path, "example-run'; DELETE FROM runs; --")
    with closing(Store(tmp_path / 'state.db', read_only=True)) as store:
        assert [r['id'] for r in store.query('SELECT id FROM runs')] == ['example-run']
    assert (tmp_path / 'state.db').read_bytes() == before
    assert result.returncode != 0
    assert 'run ID' in result.stderr


def test_valid_run_displays_evidence_without_modifying_state(tmp_path):
    seed(tmp_path)
    trial = tmp_path / 'runs/example-run/trials/scenario/with'
    trial.mkdir(parents=True)
    (trial / 'agent_output.txt').write_text('Invented trial output.\n')
    before = (tmp_path / 'state.db').read_bytes()
    result = inspect(tmp_path, 'example-run')
    assert result.returncode == 0, result.stderr
    assert 'Invented trial output.' in result.stdout
    assert 'attempted' in result.stdout
    assert (tmp_path / 'state.db').read_bytes() == before


def test_trial_symlink_cannot_read_outside_selected_state(tmp_path):
    state = tmp_path / 'state'
    seed(state)
    outside = tmp_path / 'unrelated'
    outside.mkdir()
    (outside / 'agent_output.txt').write_text('OUTSIDE_STATE_SENTINEL\n')
    run = state / 'runs/example-run'
    run.mkdir(parents=True)
    (run / 'trials').symlink_to(outside, target_is_directory=True)
    result = inspect(state, 'example-run')
    assert result.returncode != 0
    assert 'symlinks' in result.stderr
    assert 'OUTSIDE_STATE_SENTINEL' not in result.stdout


def test_time_window_excludes_later_runs_without_claiming_attribution(tmp_path):
    seed(tmp_path)
    with closing(Store(tmp_path / 'state.db', migrate=False)) as store:
        store.insert('runs', {'id': 'later-run', 'started': '2026-01-02T00:00:00Z'})
        for name, started in [('selected-evaluation', '2026-01-01T01:00:00Z'),
                              ('later-evaluation', '2026-01-02T01:00:00Z')]:
            store.insert('eval_results', {'id': name, 'kind': 'regression', 'started': started})
        store.commit()
    result = inspect(tmp_path, 'example-run')
    assert result.returncode == 0, result.stderr
    assert 'selected-evaluation' in result.stdout
    assert 'later-evaluation' not in result.stdout
    assert 'timestamps do not prove run attribution' in result.stdout


def test_invalid_stats_fail_loudly_without_an_invented_gate_result(tmp_path):
    seed(tmp_path)
    with closing(Store(tmp_path / 'state.db', migrate=False)) as store:
        store.update('runs', 'id', 'example-run', {'stats_json': '[]'})
        store.commit()
    result = inspect(tmp_path, 'example-run')
    assert result.returncode != 0
    assert 'JSON object' in result.stderr
    assert 'no gate block' not in result.stdout
