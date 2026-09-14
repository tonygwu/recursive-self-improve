"""Read one selected run and bounded trial evidence through the state adapter."""
from __future__ import annotations

import argparse
from contextlib import closing
import itertools
import json
import os
from pathlib import Path
import re
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from self_improve.store import Store


def inspect(state: Path, requested: str | None) -> None:
    state = state.resolve()
    if requested is not None and not re.fullmatch(r'[A-Za-z0-9_-]+', requested):
        raise ValueError('run ID must contain only letters, numbers, underscores or hyphens')
    with closing(Store(state / 'state.db', read_only=True)) as store:
        with store.transaction():
            run = store.query_one('SELECT * FROM runs WHERE id=?', (requested,)) if requested else store.query_one(
                'SELECT * FROM runs ORDER BY started DESC, id DESC LIMIT 1')
            if run is None:
                raise ValueError('no matching run in the selected database')
            if not re.fullmatch(r'[A-Za-z0-9_-]+', run['id']):
                raise ValueError('stored run ID is not a safe artifact directory name')
            stats = json.loads(run['stats_json'])
            if not isinstance(stats, dict):
                raise ValueError('run stats must be a JSON object')
            next_run = store.query_one('SELECT started FROM runs WHERE started > ? ORDER BY started LIMIT 1', (run['started'],))
            bounds = [v for v in (run['finished'], next_run['started'] if next_run else '') if v]
            end = min(bounds) if bounds else None
            evaluations = store.query(
                'SELECT id, verdict, attempted, succeeded, failed, error_taxonomy_json FROM eval_results '
                'WHERE started >= ?' + (' AND started < ?' if end else '') + ' ORDER BY started, id',
                (run['started'], end) if end else (run['started'],),
            )
    print('run:', run['id'])
    print(json.dumps({k: run[k] for k in ('started', 'finished', 'status')}, indent=2))
    print('\n=== gate stats as the run recorded them ===')
    print(json.dumps(stats.get('gate'), indent=2))
    print('\n=== evaluations in the run time window; timestamps do not prove run attribution ===')
    print(json.dumps(evaluations, indent=2))
    trials = state / 'runs' / run['id'] / 'trials'
    for path in (state / 'runs', trials.parent, trials):
        if path.is_symlink():
            raise ValueError('trial evidence must not traverse symlinks')
    print('\n=== trial evidence ===')
    if not trials.exists():
        print('No trials directory. This does not establish why trial evidence is absent.')
    else:
        paths = sorted(trials.rglob('*'))
        if any(p.is_symlink() for p in paths):
            raise ValueError('trial evidence must not traverse symlinks')
        for path in paths:
            if path.is_dir() and len(path.relative_to(trials).parts) <= 3:
                print('trials/' + str(path.relative_to(trials)))
            if path.name == 'agent_output.txt' and path.is_file():
                print('\n===', path.relative_to(trials), '===')
                with path.open() as stream:
                    lines = list(itertools.islice(stream, 41))
                print(''.join(lines[:40]), end='')
                if len(lines) > 40:
                    print('\n[Output capped at 40 lines; later lines are omitted.]')
    print('\nInspect raw trial output, error causes and served-model identity before interpreting a verdict.')
    print('An agent or grader execution error is a harness failure, not a verdict about the rule.')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run_id', nargs='?')
    args = parser.parse_args()
    state = Path(os.environ.get('SI_STATE_DIR', str(Path.home() / '.self-improve')))
    try:
        inspect(state, args.run_id)
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f'Cannot inspect selected database or evidence: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
