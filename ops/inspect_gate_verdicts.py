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

RUN_ID = re.compile(r'[A-Za-z0-9_-]+')

# A result links to a run through an eval attempt (migration 0023) or through
# proposal_eval_history. A result recorded before 0023 has neither link, so no
# run can claim it, and its start time does not establish which run produced it.
LINKED = '''
    SELECT id, verdict, attempted, succeeded, failed, error_taxonomy_json FROM eval_results
    WHERE id IN (
        SELECT json_extract(e.record_json, '$.data.eval_result_id')
          FROM eval_attempt_events e JOIN eval_attempts a ON a.id = e.attempt_id
         WHERE a.run_id = ? AND e.kind = 'scenario_result'
        UNION
        SELECT eval_result_id FROM proposal_eval_history WHERE run_id = ? AND eval_result_id <> '')
    ORDER BY started, id'''
# A result with an attempt was recorded after 0023, even when a model job owns
# that attempt. NOT IN against a NULL matches nothing, so drop null result IDs.
UNLINKED = '''
    SELECT count(*) AS n FROM eval_results
    WHERE id NOT IN (
        SELECT json_extract(record_json, '$.data.eval_result_id') FROM eval_attempt_events
         WHERE kind = 'scenario_result' AND json_extract(record_json, '$.data.eval_result_id') IS NOT NULL
        UNION
        SELECT eval_result_id FROM proposal_eval_history)'''
# julianday() reads both stored forms and gives NULL for an unreadable value or
# a missing migration row, so the order is then unknown.
ORDER = '''
    SELECT CASE
        WHEN julianday(r.started) IS NULL OR julianday(m.applied_at) IS NULL THEN 'unknown'
        WHEN julianday(r.started) < julianday(m.applied_at) THEN 'before'
        ELSE 'after' END AS ord
    FROM runs r LEFT JOIN schema_migrations m ON m.name = '0023_eval_attempts'
    WHERE r.id = ?'''


def unlinked_note(count: int, order: str) -> list[str]:
    """Say only what the queries found about results that no run claims."""
    if count == 0 or order == 'after':
        # Every result is linked, or this run started after 0023 was applied and
        # so cannot have produced a pre-0023 result.
        return []
    if order == 'before':
        return [f'  {count} result(s) in this database have no eval attempt and no run',
                '  history. Results recorded before migration 0023 can lack both, so',
                '  this run may have produced some of them.']
    if order == 'unknown':
        return [f'  {count} result(s) in this database have no eval attempt and no run',
                '  history. Results recorded before migration 0023 can lack both. This',
                "  run's start cannot be compared with the time 0023 was applied, so",
                '  whether this run produced some of them is unknown.']
    raise ValueError(f'unexpected order against migration 0023: {order!r}')


def inspect(selected: Path, requested: str | None) -> None:
    if requested is not None and not RUN_ID.fullmatch(requested):
        raise ValueError(f'invalid run id: {requested!r}; a run ID must contain only '
                         'letters, numbers, underscores or hyphens')
    # Checked before any open, so a wrong SI_STATE_DIR cannot create a database
    # and look like an empty history. The message names the path as selected.
    if not (selected / 'state.db').is_file():
        raise ValueError(f"no database file at {selected / 'state.db'}")
    state = selected.resolve()
    with closing(Store(state / 'state.db', read_only=True)) as store:
        with store.transaction():
            run = store.query_one('SELECT * FROM runs WHERE id=?', (requested,)) if requested else store.query_one(
                'SELECT * FROM runs ORDER BY started DESC, id DESC LIMIT 1')
            if run is None:
                raise ValueError('no matching run in the selected database')
            if not RUN_ID.fullmatch(run['id']):
                raise ValueError('stored run ID is not a safe artifact directory name')
            try:
                stats = json.loads(run['stats_json'])
            except json.JSONDecodeError as exc:
                raise ValueError(f'run stats_json is malformed JSON: {exc}') from exc
            if not isinstance(stats, dict):
                raise ValueError('run stats must be a JSON object')
            gate = stats.get('gate')
            if gate is not None and not isinstance(gate, dict):
                raise ValueError('the run\'s gate stats must be a JSON object')
            evaluations = store.query(LINKED, (run['id'], run['id']))
            note = [] if evaluations else unlinked_note(
                store.query_one(UNLINKED)['n'], store.query_one(ORDER, (run['id'],))['ord'])
    print('run:', run['id'])
    print(json.dumps({k: run[k] for k in ('started', 'finished', 'status')}, indent=2))
    print('\n=== gate stats as the run recorded them ===')
    print(json.dumps(gate, indent=2) if gate is not None else '  (no gate block)')
    print('\n=== per-scenario split, if the majority gate ran ===')
    splits = gate.get('scenario_splits') if gate is not None else None
    print(json.dumps(splits, indent=2) if splits is not None else '  (none)')
    print('\n=== eval_results linked to this run, with the error taxonomy that decides')
    print('    whether a verdict is about the rule or about the harness ===')
    if evaluations:
        print(json.dumps(evaluations, indent=2))
    else:
        print('\n'.join(['  no results are linked to this run', *note]))
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
