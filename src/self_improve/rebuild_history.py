"""Plan the source closure required by retained execution history.

This module only reads the supplied Store. Rebuild owns transactions, backup and
all deletion. Exact scalar identity matches conservatively keep existing source
rows, including IDs inside frozen JSON. It never interprets or executes content.
"""
from collections import defaultdict, deque
import json

from .store import MIGRATIONS

SOURCE_TABLES = frozenset({
    'sessions', 'incidents', 'learnings', 'proposals', 'proposal_events',
    'incident_learnings', 'mining_history', 'eval_results',
})
EXECUTION_TABLES = frozenset({
    'commands', 'command_targets', 'command_members', 'command_control_events',
    'proposal_revisions', 'proposal_eval_history', 'instruction_operations',
    'instruction_requests', 'instruction_operation_controls', 'rejection_members',
    'model_jobs', 'job_budgets', 'job_steps', 'job_calls', 'job_evaluations',
    'proposal_resolutions', 'proposal_reapplications', 'incident_jobs',
    'recovery_jobs', 'proposal_recoveries', 'eval_attempts', 'eval_attempt_events',
    'rule_revisions', 'quality_subjects', 'quality_samples', 'quality_judgments',
    'evidence_command_results',
})
# These archives have their own retained snapshots and missing-source semantics.
# Their historical membership must not pin every current mining draft.
DETACHED_TABLES = frozenset({
    'runs', 'llm_calls', 'schema_migrations', 'execution_policies', 'execution_policy_events',
    'project_identity_cache', 'scan_manifests', 'scan_working_copies', 'scan_observations',
    'rule_availability_observations', 'rule_availability_collections',
    'instruction_inventories', 'instruction_text_archives', 'session_context_batches',
    'session_context_records', 'native_load_reports', 'rule_family_snapshots',
    'rule_family_members', 'rule_family_heads',
    'queue_coverage', 'queue_events', 'queue_processing', 'queue_snapshots',
})
PROJECTION_TABLES = frozenset({
    'scan_lines', 'scan_occurrences', 'scan_observation_occurrences', 'scan_incident_links',
    'project_stats', 'embeddings', 'contradictions', 'error_fingerprints',
})
KNOWN_TABLES = SOURCE_TABLES | EXECUTION_TABLES | DETACHED_TABLES | PROJECTION_TABLES
LEGACY_DECISIONS = frozenset({'approved_user', 'rejected_user', 'applied', 'rolled_back'})


def _strict_json(value, owner):
    def object_pairs(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = item
        return result
    def invalid(_):
        raise ValueError('nonfinite JSON value')
    try:
        return json.loads(value, object_pairs_hook=object_pairs, parse_constant=invalid)
    except (ValueError, TypeError) as exc:
        raise ValueError(owner + ': invalid dependency JSON') from exc


def _values(row, owner):
    """All complete scalar values; no substring or embedded-code matching."""
    pending = [(_strict_json(v, owner+'.'+k) if k.endswith('_json') else v)
               for k, v in row.items()]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            pending.extend(value.values())
        elif isinstance(value, (list, tuple)):
            pending.extend(value)
        elif isinstance(value, str) and value:
            yield value


def plan_history(store) -> dict:
    """Return existing source keys, complete execution rows and named reasons."""
    schema = store.schema_tables()
    unknown = set(schema) - KNOWN_TABLES
    if unknown:
        raise ValueError('Rebuild needs a retention policy for table(s): '+', '.join(sorted(unknown)))
    applied = {r['name'] for r in store.query('SELECT name FROM schema_migrations')}
    unknown_migrations = applied - {name for name, _ in MIGRATIONS}
    if unknown_migrations:
        raise ValueError('Rebuild does not recognize migration(s): '+', '.join(sorted(unknown_migrations)))
    expected = store.schema_tables(expected_migrations=applied)
    for table in sorted(set(expected) | set(schema)):
        if table == 'schema_migrations':
            continue  # Store creates its bookkeeping table before migration 0001.
        if table not in schema:
            raise ValueError('Applied schema is missing table '+table)
        if table not in expected or schema[table] != expected[table]:
            raise ValueError('Rebuild needs a retention policy for changed schema of '+table)
    store.check_foreign_keys()
    loaded = (SOURCE_TABLES | EXECUTION_TABLES) & set(schema)
    rows = {t: store.query('SELECT * FROM '+t) for t in sorted(loaded)}
    keys = {t: schema[t]['primary_key'] for t in rows}
    if any(not pk for pk in keys.values()):
        raise ValueError('Rebuild requires primary keys for source and execution records')
    keyed = {(t, tuple(r[k] for k in keys[t])): r for t in rows for r in rows[t]}
    identities = defaultdict(list)
    for (t, key), row in keyed.items():
        if t in SOURCE_TABLES and len(key) == 1 and isinstance(key[0], str) and key[0]:
            identities[key[0]].append((t, key))
    # Read reverse source edges once. Retain a complete connected evidence chain
    # only after execution pins it; ordinary orphan windows keep legacy behavior.
    children = defaultdict(list)
    reverse = [('proposals', 'id', 'proposal_events', 'proposal_id'),
               ('learnings', 'id', 'mining_history', 'learning_id'),
               ('learnings', 'id', 'incident_learnings', 'learning_id'),
               ('incidents', 'id', 'incident_learnings', 'incident_id'),
               ('sessions', 'file_path', 'incidents', 'session_file'),
               ('learnings', 'id', 'eval_results', 'subject_id'),
               ('proposals', 'id', 'eval_results', 'subject_id'),
               ('incidents', 'id', 'eval_results', 'subject_id')]
    for parent, column, child, child_column in reverse:
        if child in rows:
            for row in rows[child]:
                children[(parent, column, row[child_column])].append((child, tuple(row[k] for k in keys[child])))
    # Foreign keys may target unique non-primary or composite columns.
    indexes = {}
    for t in rows:
        for fk in schema[t]['foreign_keys']:
            parent = fk['table']
            if parent not in rows:
                continue
            columns = tuple(fk['references'])
            if any(c is None for c in columns):
                columns = tuple(keys[parent])
            index_key = (parent, columns)
            if index_key not in indexes:
                indexes[index_key] = {tuple(r[c] for c in columns): (parent, tuple(r[k] for k in keys[parent]))
                                      for r in rows[parent]}
    reasons, queue = {}, deque()
    def keep(item, reason):
        if item not in reasons:
            reasons[item] = reason
            queue.append(item)
    for item, row in keyed.items():
        table, key = item
        if table in EXECUTION_TABLES:
            keep(item, 'execution_record')
        elif table == 'proposals' and (row['status'] in LEGACY_DECISIONS or row.get('decision_scope')
                or row['applied_at'] or row['snapshot_commit_before'] or row['snapshot_commit_after']):
            keep(item, 'legacy_decision_or_application')
        elif table == 'proposal_events' and row['event'] in LEGACY_DECISIONS:
            keep(item, 'legacy_decision_or_application')
    while queue:
        item = queue.popleft(); table, key = item; row = keyed[item]
        for value in _values(row, table+'.'+repr(key)):
            for target in identities.get(value, ()):
                keep(target, 'recorded_identity')
        for fk in schema[table]['foreign_keys']:
            parent = fk['table']
            if parent not in rows:
                continue
            columns = tuple(fk['references'])
            if any(c is None for c in columns):
                columns = tuple(keys[parent])
            target = indexes[(parent, columns)].get(tuple(row[c] for c in fk['columns']))
            if target:
                keep(target, 'foreign_key')
        for column, value in row.items():
            if isinstance(value, str):
                for target in children.get((table, column, value), ()):
                    keep(target, 'source_history')
    kept = {t: [r for r in rows[t] if (t, tuple(r[k] for k in keys[t])) in reasons] for t in rows}
    # Stable row order makes preview, apply and backups comparable.
    for t in kept:
        kept[t].sort(key=lambda r: tuple(r[k] for k in keys[t]))
    measurement_scans = set()
    from .project_measurements import MIGRATION, PROFILE, measurement_detail
    if MIGRATION in applied:
        for row in store.query('SELECT id FROM project_stats WHERE record_type=?', (PROFILE,)):
            measurement = measurement_detail(store, row['id'])['measurement']
            for side in ('before', 'after'):
                if measurement[side] is not None:
                    measurement_scans.update(measurement[side]['scan_observation_ids'])
    return {'rows': rows, 'kept': kept, 'keys': keys, 'tables': sorted(schema),
            'measurement_scan_ids': measurement_scans,
            'source_keys': {t: {tuple(r[k] for k in keys[t]) for r in kept[t]} for t in SOURCE_TABLES & set(rows)},
            'counts': {t: len(kept[t]) for t in sorted(SOURCE_TABLES & set(rows))},
            'reasons': {t: {cause: sum(1 for (owner, _), reason in reasons.items() if owner == t and reason == cause)
                            for cause in sorted({reason for (owner, _), reason in reasons.items() if owner == t})}
                        for t in sorted(SOURCE_TABLES & set(rows))}}


def delete_unretained(store, plan, table, *, extra_keys=()):
    """Delete planned source rows only; caller owns the write transaction."""
    keep = plan['source_keys'].get(table, set()) | set(extra_keys)
    columns = plan['keys'][table]
    doomed = [tuple(row[k] for k in columns) for row in plan['rows'][table]
              if tuple(row[k] for k in columns) not in keep]
    store.conn.executemany('DELETE FROM '+table+' WHERE '+' AND '.join(c+'=?' for c in columns), doomed)
    return len(doomed)
