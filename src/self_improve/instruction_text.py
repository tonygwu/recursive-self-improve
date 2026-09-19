"""Immutable redacted text from the inventory collector's exact observed surface.

This archive does not assert runtime loading. Readers use only their supplied
Store; neither missing historical text nor a stale observation triggers a file read.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import json

from . import instruction_inventory as inventory
from .mining_history import digest, encoded
from .redact import redact_text
from .rule_revisions import AvailabilityError, read_record, retained_revisions, text_hash

MIGRATION = '0033_instruction_text'
TABLE = 'instruction_text_archives'
PROFILE = 'instruction-text/1'
BINDINGS = ('project_key', 'working_copy_id', 'working_copy', 'observed_at', 'run_id', 'status')
FILE_METADATA = ('path', 'real_path', 'aliases', 'bytes', 'lines', 'content_hash', 'loading_paths')
COVERAGE = ('profile', 'issues', 'limits', 'deduplicated', 'unobserved_sources',
            'scope_note', 'runtime_loading_verified')
COLUMNS = ('id', 'inventory_id', 'project_key', 'working_copy_id', 'observed_at', 'run_id')


class TextArchiveError(AvailabilityError):
    """Retained instruction text or its inventory binding cannot be trusted."""


def require_schema(store):
    if not store.query_one('SELECT name FROM schema_migrations WHERE name=?', (MIGRATION,)):
        raise TextArchiveError('UpgradeRequired: ' + MIGRATION)
    try:
        store.query(f'SELECT {",".join(COLUMNS)},record_json,record_hash FROM {TABLE} LIMIT 0')
    except Exception as exc:
        raise TextArchiveError(TABLE + ': applied text archive schema is damaged') from exc
    inventory.require_schema(store)


def _base(inv):
    return {'profile': PROFILE, 'inventory_id': inv['id'],
            **{key: deepcopy(inv[key]) for key in BINDINGS},
            'coverage': {key: deepcopy(inv[key]) for key in COVERAGE if key in inv}}


def build_archive(surface, inventory):
    """Pure: redact the already-read files, retaining their exact inventory identity."""
    owner = 'instruction_text.inventory.' + str(inventory.get('id', '?'))
    try:
        if inventory['id'] != digest({k:v for k,v in inventory.items() if k != 'id'}):
            raise ValueError('invalid inventory identity')
        if any(inventory.get(k) != v for k,v in surface.items() if k != 'files'):
            raise ValueError('surface coverage differs from inventory')
        source = {f['real_path']: f for f in surface['files']}
        if len(source) != len(surface['files']) or set(source) != {f['real_path'] for f in inventory['files']}:
            raise ValueError('surface file identities differ from inventory')
        files = []
        for meta in inventory['files']:
            observed = source[meta['real_path']]
            raw = observed['_text']
            if (any(observed[key] != meta[key] for key in FILE_METADATA if key != 'lines')
                    or text_hash(raw) != meta['content_hash'] or len(raw.encode()) != meta['bytes']
                    or len(raw.splitlines()) != meta['lines']):
                raise ValueError('surface content differs from inventory: ' + meta['real_path'])
            text = redact_text(raw)
            files.append({**{key: deepcopy(meta[key]) for key in FILE_METADATA},
                          'file_key': digest(meta['real_path']), 'text': text,
                          'redacted_hash': text_hash(text), 'redacted_bytes': len(text.encode()),
                          'redacted_lines': len(text.splitlines())})
        record = {**_base(inventory), 'files': files}
        record['id'] = digest(record)
        return record
    except (KeyError, TypeError, ValueError) as exc:
        raise TextArchiveError(owner + ': cannot bind observed text: ' + str(exc)) from exc


def _validate(record, inv):
    owner = TABLE + '.' + str(record.get('id', '?')) if isinstance(record, dict) else TABLE
    try:
        base = _base(inv)
        if (set(record) != set(base) | {'id', 'files'} or any(record[k] != v for k,v in base.items())
                or record['id'] != digest({k:v for k,v in record.items() if k != 'id'})
                or not isinstance(record['files'], list) or len(record['files']) != len(inv['files'])):
            raise ValueError('invalid identity, profile or inventory binding')
        for file, meta in zip(record['files'], inv['files']):
            if (set(file) != set(FILE_METADATA) | {'file_key','text','redacted_hash','redacted_bytes','redacted_lines'}
                    or any(file[key] != meta[key] for key in FILE_METADATA)
                    or file['file_key'] != digest(meta['real_path']) or not isinstance(file['text'], str)
                    or file['redacted_hash'] != text_hash(file['text'])
                    or type(file['redacted_bytes']) is not int or file['redacted_bytes'] != len(file['text'].encode())
                    or type(file['redacted_lines']) is not int or file['redacted_lines'] != len(file['text'].splitlines())
                    or redact_text(file['text']) != file['text']):
                raise ValueError('invalid redacted text or file binding: ' + str(meta['real_path']))
    except (KeyError, TypeError, ValueError) as exc:
        raise TextArchiveError(owner + ': invalid retained text: ' + str(exc)) from exc
    return record


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result: raise ValueError('duplicate JSON key: ' + key)
        result[key] = value
    return result


def _non_json_constant(value):
    raise ValueError('non-JSON constant: ' + value)


def _inventory(store, inventory_id, revisions):
    row = store.query_one(f'SELECT * FROM {inventory.TABLE} WHERE id=?', (inventory_id,))
    if row is None: raise TextArchiveError('retained inventory missing: ' + inventory_id)
    return inventory._read(row, revisions)


def _read(store, row, revisions, inv=None):
    try:
        # read_record checks indexed fields/hash; reject duplicate keys and NaN too.
        json.loads(row['record_json'], object_pairs_hook=_strict_object, parse_constant=_non_json_constant)
        record = read_record(row, TABLE)
        return _validate(record, inv if inv is not None else _inventory(store, row['inventory_id'], revisions))
    except (AvailabilityError, TypeError, ValueError) as exc:
        raise TextArchiveError(TABLE + '.' + row['id'] + ': ' + str(exc)) from exc


def record_archives(store, records):
    """Publish inside the collector's transaction; never commit or replace history."""
    require_schema(store)
    if store.read_only or not store.conn.in_transaction:
        raise TextArchiveError('Text archive persistence requires the caller\'s active write transaction')
    revisions = {r['id']:r for r in retained_revisions(store)}
    for record in records:
        try:
            inv = _inventory(store, record['inventory_id'], revisions)
            _validate(record, inv)
            previous = store.query_one(f'SELECT * FROM {TABLE} WHERE inventory_id=?', (record['inventory_id'],))
            if previous:
                if _read(store, previous, revisions, inv) != record:
                    raise TextArchiveError('immutable text archive replay changed')
            else:
                store.insert(TABLE, {**{k:record[k] for k in COLUMNS},
                                     'record_json': encoded(record), 'record_hash': digest(record)})
        except (AvailabilityError, KeyError, TypeError, ValueError) as exc:
            owner = record.get('id', '?') if isinstance(record, dict) else '?'
            raise TextArchiveError(TABLE + '.' + str(owner) + ': ' + str(exc)) from exc


def read_archive(store, archive_id):
    """Read the full retained redacted text; never reopen its original files."""
    require_schema(store)
    row = store.query_one(f'SELECT * FROM {TABLE} WHERE id=?', (archive_id,))
    if row is None: raise TextArchiveError(TABLE + '.' + str(archive_id) + ': archive not found')
    revisions = {r['id']:r for r in retained_revisions(store)}
    return _read(store, row, revisions)


def current_archives(store):
    """Latest inventory per copy, with explicit missing/conflicting text coverage.

    Current means latest recorded observation only. It never means that files
    still match, or that an instruction was loaded by a specific session.
    """
    response = {'profile': PROFILE, 'records': [], 'count': None, 'reason': 'schema_unavailable',
                'meaning': 'Latest recorded inventory per copy; current files and runtime loading remain unverified.'}
    for migration in (MIGRATION, inventory.MIGRATION):
        if not store.query_one('SELECT name FROM schema_migrations WHERE name=?', (migration,)):
            return response
    require_schema(store)
    rows = store.query(f'SELECT i.* FROM {inventory.TABLE} i JOIN '
                       f'(SELECT project_key,working_copy_id,MAX(observed_at) observed_at FROM {inventory.TABLE} '
                       'GROUP BY project_key,working_copy_id) latest '
                       'ON i.project_key=latest.project_key AND i.working_copy_id=latest.working_copy_id '
                       'AND i.observed_at=latest.observed_at ORDER BY i.project_key,i.working_copy_id,i.id')
    revisions = {r['id']:r for r in retained_revisions(store)}
    grouped = defaultdict(list)
    for row in rows:
        inv = inventory._read(row, revisions)
        grouped[(inv['project_key'], inv['working_copy_id'])].append(inv)
    for (project_key, copy_id), same in grouped.items():
        available, missing = [], []
        for inv in same:
            row = store.query_one(f'SELECT * FROM {TABLE} WHERE inventory_id=?', (inv['id'],))
            if row is None: missing.append(inv['id'])
            else: available.append(_read(store, row, revisions, inv))
        signatures = {digest({k:v for k,v in inv.items() if k not in {'id','run_id'}}) for inv in same}
        conflicting = len(signatures) > 1
        archive = None if conflicting or not available else available[-1]
        response['records'].append({
            'project_key': project_key, 'working_copy_id': copy_id, 'working_copy': same[0]['working_copy'],
            'observed_at': same[0]['observed_at'], 'inventory_ids': [inv['id'] for inv in same],
            'archive_ids': [a['id'] for a in available], 'missing_inventory_ids': missing,
            'status': 'conflicting' if conflicting else archive['status'] if archive else 'metadata_only',
            'archive': archive,
        })
    response.update(count=len(grouped), reason='' if grouped else 'no_inventory_observations')
    return response
