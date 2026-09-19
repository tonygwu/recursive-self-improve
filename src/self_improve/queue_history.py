"""Retained global queue observations; no transcript reads or model work."""
from datetime import datetime, timedelta, timezone
import json
import math
import re
import sqlite3

from .store import MIGRATIONS, new_id

MIGRATION = '0034_queue_history'
PROFILE = 'run-backlog/1'
PROCESSING_PROFILE = 'queue-processing/1'
TABLES = ('queue_coverage', 'queue_events', 'queue_processing', 'queue_snapshots')
OUTCOMES = {'negative': 'dismissed', 'duplicate_of_rejected': 'dismissed',
            'new': 'mined', 'duplicate': 'mined', 'amend': 'mined', 'amend_applied': 'mined'}
STATUSES = {'new', 'mined', 'dismissed'}


class QueueHistoryError(ValueError):
    """Queue coverage or retained records cannot support the claimed observation."""


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _time(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is None or result.utcoffset() != timedelta(0):
            raise ValueError('UTC required')
        return result.astimezone(timezone.utc)
    except (TypeError, AttributeError, ValueError) as exc:
        raise QueueHistoryError('Queue history contains an invalid UTC observation time') from exc


def _integer(value):
    return type(value) is int and value >= 0


def _expected_triggers():
    script = dict(MIGRATIONS)[MIGRATION]
    start = 0
    result = {}
    for offset, char in enumerate(script):
        if char == ';' and sqlite3.complete_statement(script[start:offset + 1]):
            statement = script[start:offset + 1].strip()
            match = re.search(r'CREATE TRIGGER (\w+)\b', statement)
            if match:
                result[match[1]] = statement[match.start():].rstrip(';')
            start = offset + 1
    return result


def require_schema(store):
    """Missing/changed observers are an upgrade/data error, never a fresh epoch."""
    if not store.query_one('SELECT name FROM schema_migrations WHERE name=?', (MIGRATION,)):
        raise QueueHistoryError('Upgrade the database explicitly: '+MIGRATION+' is required')
    actual = {r['name']: r['sql'] for r in store.query("SELECT name,sql FROM sqlite_master WHERE type='trigger'")}
    for name, sql in _expected_triggers().items():
        if actual.get(name, '').strip().rstrip(';') != sql:
            raise QueueHistoryError('Queue observation trigger is missing or changed: '+name)
    tables = {r['name'] for r in store.query("SELECT name FROM sqlite_master WHERE type='table'")}
    if not set(TABLES).issubset(tables):
        raise QueueHistoryError('Queue history has a missing archive table')
    coverage = store.query('SELECT * FROM queue_coverage')
    if len(coverage) != 1 or coverage[0]['id'] != 'global' or coverage[0]['profile'] != PROFILE:
        raise QueueHistoryError('Queue coverage profile is missing or incompatible')
    if not _integer(coverage[0]['opening_count']) or not _integer(coverage[0]['opening_unknown_count']):
        raise QueueHistoryError('Invalid queue opening baseline')
    _time(coverage[0]['started_at'])
    return coverage[0]


def set_processed_status(store, incident_id, status, *, outcome, provenance=None):
    """Publish a status and actual-exit receipt in the miner's transaction; no commit."""
    require_schema(store)
    if OUTCOMES.get(outcome) != status:
        raise QueueHistoryError('Processing outcome does not match its terminal status')
    p = dict(provenance or {})
    for key in ('run_id', 'command_id', 'call_id', 'stage', 'prompt_sha'):
        if not isinstance(p.get(key, ''), str):
            raise QueueHistoryError('Invalid processing '+key)
    if p.get('call_id'):
        call = store.query_one('SELECT * FROM llm_calls WHERE id=?', (p['call_id'],))
        if (not call or call['run_id'] != p.get('run_id', '') or
                call['prompt_sha'] != p.get('prompt_sha') or call['stage'] != p.get('stage')):
            raise QueueHistoryError('Queue processing call differs from its producing run, stage or prompt')
    # The conditional UPDATE obtains the writer reservation before deciding
    # whether this attempt really removed a queued member. A stale repeat earns
    # no second receipt; retain existing terminal-status behavior below.
    changed = store.conn.execute("UPDATE incidents SET status=? WHERE id=? AND status='new' RETURNING id",
                                 (status, incident_id)).fetchone()
    if changed is None:
        store.conn.execute('UPDATE incidents SET status=? WHERE id=?', (status, incident_id))
        return None
    event = store.query_one('SELECT * FROM queue_events ORDER BY seq DESC LIMIT 1')
    if (not event or event['incident_id'] != incident_id or event['operation'] != 'update' or
            event['old_status'] != 'new' or event['new_status'] != status):
        raise QueueHistoryError('Queue processing transition was not retained')
    store.insert('queue_processing', {'event_id': event['id'], 'profile': PROCESSING_PROFILE,
                 'outcome': outcome, **{k: p.get(k, '') for k in ('run_id', 'command_id', 'call_id', 'stage', 'prompt_sha')}})
    return event['id']


def _settings(settings):
    if (not isinstance(settings, dict) or set(settings) !=
            {'mine_order', 'project_filter', 'dry_run', 'cheap_call_cap', 'strong_call_cap', 'gate_call_cap'} or
            settings['mine_order'] not in ('age_out_risk', 'signal_then_recent') or
            not isinstance(settings['project_filter'], str) or type(settings['dry_run']) is not bool or
            not _integer(settings['cheap_call_cap']) or not _integer(settings['strong_call_cap']) or
            not _integer(settings['gate_call_cap'])):
        raise QueueHistoryError('Invalid frozen queue sampling settings')
    return settings


def capture(store, run_id, *, phase, settings):
    """Append a pipeline anchor in the caller transaction; no commit or rollback."""
    require_schema(store)
    if not store.conn.in_transaction:
        raise QueueHistoryError('Queue snapshot requires the caller write transaction')
    if phase not in ('start', 'finish') or not store.query_one('SELECT id FROM runs WHERE id=?', (run_id,)):
        raise QueueHistoryError('Queue snapshot needs an existing run and start/finish phase')
    encoded = _json(_settings(settings))
    existing = store.query_one('SELECT * FROM queue_snapshots WHERE run_id=? AND phase=?', (run_id, phase))
    if existing:
        if existing['settings_json'] != encoded or existing['profile'] != PROFILE:
            raise QueueHistoryError('Queue snapshot replay settings differ')
        return existing
    # SQL acquires the write lock before evaluating observation time and counts.
    store.conn.execute('''INSERT INTO queue_snapshots
        (id,run_id,phase,profile,observed_at,event_seq,queue_count,processing_count,settings_json)
        SELECT ?,?,?,?,CURRENT_TIMESTAMP || 'Z',
            (SELECT COALESCE(MAX(seq),0) FROM queue_events),
            (SELECT COUNT(*) FROM incidents WHERE status='new'),
            (SELECT COUNT(*) FROM queue_processing),?''',
        (new_id(), run_id, phase, PROFILE, encoded))
    return store.query_one('SELECT * FROM queue_snapshots WHERE run_id=? AND phase=?', (run_id, phase))


def read_run(store, run_id):
    """Read historical context only; caller owns its selected-Store transaction."""
    if not store.query_one('SELECT id FROM runs WHERE id=?', (run_id,)):
        raise KeyError(run_id)
    result = {'profile': PROFILE, 'run_id': run_id, 'scope': 'global', 'snapshot': None,
              'queue_count': None, 'window': None, 'admissions': None, 'processed_exits': None,
              'adjustments': None, 'rates': None, 'scenario': None, 'reasons': []}
    if not store.query_one('SELECT name FROM schema_migrations WHERE name=?', (MIGRATION,)):
        result['reasons'] = ['Queue history was not installed in this database.']
        return result
    coverage = require_schema(store)
    snapshot = store.query_one("SELECT * FROM queue_snapshots WHERE run_id=? ORDER BY CASE phase WHEN 'finish' THEN 0 ELSE 1 END LIMIT 1", (run_id,))
    if snapshot is None:
        result['reasons'] = ['No historical queue snapshot was retained for this run.']
        return result
    if (snapshot['profile'] != PROFILE or snapshot['phase'] not in ('start', 'finish') or
            not _integer(snapshot['event_seq']) or not _integer(snapshot['queue_count']) or
            not _integer(snapshot['processing_count'])):
        raise QueueHistoryError('Queue snapshot has an incompatible profile or invalid counts')
    try:
        settings = _settings(json.loads(snapshot['settings_json']))
    except (TypeError, ValueError) as exc:
        raise QueueHistoryError('Invalid queue snapshot settings') from exc
    observed = _time(snapshot['observed_at'])
    baseline_time = _time(coverage['started_at'])
    if observed < baseline_time:
        raise QueueHistoryError('Queue snapshot precedes its coverage baseline')
    opening = store.query_one("SELECT * FROM queue_snapshots WHERE run_id=? AND phase='start'", (run_id,))
    if opening and (opening['profile'] != PROFILE or
            not all(_integer(opening[k]) for k in ('event_seq', 'queue_count', 'processing_count')) or
            not baseline_time <= _time(opening['observed_at']) <= observed or
            opening['event_seq'] > snapshot['event_seq'] or
            opening['processing_count'] > snapshot['processing_count'] or
            opening['settings_json'] != snapshot['settings_json']):
        raise QueueHistoryError('Queue start and finish snapshots have incompatible ordering or settings')
    end = observed.replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=7)
    count, seq, last_time = coverage['opening_count'], 0, baseline_time
    admissions = processed = adjustments = tail_adjustments = receipt_count = 0
    # Stream the prefix: historical integrity does not require loading it all
    # into memory. The sequence index bounds the selected immutable snapshot.
    rows = store.conn.execute('''SELECT e.*, p.profile AS processing_profile,
        p.outcome, p.run_id AS processing_run_id, p.command_id, p.call_id, p.stage, p.prompt_sha,
        c.id AS recorded_call_id, c.run_id AS call_run_id, c.stage AS call_stage, c.prompt_sha AS call_prompt_sha,
        j.command_id AS recorded_command_id, j.run_id AS job_run_id, j.incident_id AS job_incident_id
        FROM queue_events e LEFT JOIN queue_processing p ON p.event_id=e.id
        LEFT JOIN llm_calls c ON c.id=p.call_id
        LEFT JOIN incident_jobs j ON j.command_id=p.command_id
        WHERE e.seq<=? ORDER BY e.seq''', (snapshot['event_seq'],))
    for raw in rows:
        row = dict(raw)
        seq += 1
        timestamp = _time(row['observed_at'])
        if row['seq'] != seq or timestamp < last_time or timestamp > observed:
            raise QueueHistoryError('Queue history has a sequence gap or inconsistent observation time')
        last_time = timestamp
        op, old, new = row['operation'], row['old_status'], row['new_status']
        if not ((op == 'insert' and old is None and new in STATUSES) or
                (op == 'delete' and new is None and old in STATUSES) or
                (op == 'update' and old in STATUSES and new in STATUSES and old != new)):
            raise QueueHistoryError('Queue history contains an unknown transition')
        delta = int(new == 'new') - int(old == 'new')
        if row['queue_delta'] != delta:
            raise QueueHistoryError('Queue history delta differs from its transition')
        count += delta
        if count < 0:
            raise QueueHistoryError('Queue history has negative occupancy')
        is_processed = row['processing_profile'] is not None
        if is_processed and (row['processing_profile'] != PROCESSING_PROFILE or
                op != 'update' or old != 'new' or OUTCOMES.get(row['outcome']) != new):
            raise QueueHistoryError('Queue processing receipt differs from its transition')
        if is_processed:
            receipt_count += 1
            if row['call_id']:
                if (not row['recorded_call_id'] or row['call_run_id'] != row['processing_run_id'] or
                        row['call_stage'] not in ('mine', 'mine_agentic') or row['call_stage'] != row['stage'] or
                        row['call_prompt_sha'] != row['prompt_sha']):
                    raise QueueHistoryError('Retained processing call differs from its receipt')
            if row['command_id']:
                if (not row['recorded_command_id'] or row['job_run_id'] != row['processing_run_id'] or
                        row['job_incident_id'] != row['incident_id']):
                    raise QueueHistoryError('Retained processing job differs from its receipt')
        admission = op == 'insert' and new == 'new'
        adjustment = not admission and not is_processed
        if start <= timestamp < end:
            admissions += admission
            processed += is_processed
            adjustments += adjustment
        elif end <= timestamp <= observed:
            tail_adjustments += adjustment
    if seq != snapshot['event_seq'] or count != snapshot['queue_count']:
        raise QueueHistoryError('Queue snapshot does not reconcile with its transition prefix')
    if receipt_count != snapshot['processing_count']:
        raise QueueHistoryError('Queue processing receipts changed after the retained snapshot')
    result.update(snapshot={**snapshot, 'settings': settings}, queue_count=count,
                  coverage=coverage, window={'start': start.isoformat(), 'end': end.isoformat(), 'days': 7,
                                            'complete': baseline_time <= start},
                  admissions=admissions, processed_exits=processed,
                  adjustments={'window': adjustments, 'through_snapshot': tail_adjustments})
    if coverage['opening_unknown_count']:
        result['reasons'].append('Unknown incident statuses existed when queue coverage began.')
    if baseline_time > start:
        result['reasons'].append('Seven complete UTC days of queue observation are not yet available.')
    if adjustments or tail_adjustments:
        result['reasons'].append('Queue maintenance or unattributed transitions prevent a comparable drain estimate.')
    if result['reasons']:
        return result
    arrival_rate, processing_rate = admissions / 7, processed / 7
    net = processing_rate - arrival_rate
    result['rates'] = {'admissions_per_day': arrival_rate, 'processed_per_day': processing_rate,
                       'net_drain_per_day': net}
    if count == 0:
        result['scenario'] = {'state': 'empty', 'days': None, 'assumption': 'The recorded queue is empty.'}
    elif net > 0:
        result['scenario'] = {'state': 'shrinking', 'days': math.ceil(count * 7 / (processed - admissions)),
                             'assumption': 'Only if these observed global admission and processing rates continue.'}
    else:
        result['scenario'] = {'state': 'growing' if net < 0 else 'steady', 'days': None,
                             'assumption': 'No finite drain estimate at these observed rates.'}
    return result
