"""Immutable delivered rule units, corroborated by retained application snapshots.

Capturing reads the supplied Store and its configured snapshot repository. It
never changes instruction files, Git refs, or the database. Persistence belongs
to the availability collector's explicit transaction.
"""
from __future__ import annotations

import hashlib
import difflib
import json
import re
from pathlib import Path

from . import project_identity
from .mining_history import digest, encoded
from .propose import line_marker_id
from .rollback import application_source, RollbackError
from .scan_observations import normalize_timestamp

MIGRATION = '0024_rule_availability'
TABLES = ('rule_revisions', 'rule_availability_observations', 'rule_availability_collections')


class AvailabilityError(ValueError):
    """A recorded revision, observation, or collection cannot be trusted."""


def timestamp(value, owner):
    normalized = normalize_timestamp(value)
    if normalized is None:
        raise AvailabilityError(f'{owner}: an explicit UTC/offset timestamp is required')
    return normalized


def text_hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def require_schema(store):
    if not store.query_one('SELECT name FROM schema_migrations WHERE name=?', (MIGRATION,)):
        raise AvailabilityError('UpgradeRequired: '+MIGRATION)
    for name in TABLES:
        try:
            store.query(f'SELECT id FROM {name} LIMIT 0')
        except Exception as exc:
            raise AvailabilityError(f'{name}: applied availability schema is damaged') from exc


def require_write(store):
    require_schema(store)
    if store.read_only or not store.conn.in_transaction:
        raise AvailabilityError('Availability persistence requires the caller\'s active write transaction')


def marked_units(text, *, line_counts=None):
    """Marked bullets, including multiline bodies; ambiguous non-bullets stay unknown.

    Marker presence alone never assigns machine ownership. Callers must match
    this exact unit to a corroborated delivered snapshot.
    """
    lines = text.splitlines(keepends=True)
    units = []
    for end, line in enumerate(lines):
        learning_id = line_marker_id(line)
        if not learning_id:
            continue
        start = end
        if line_counts and learning_id in line_counts:
            # The retained snapshot defines the complete unit length. Never
            # infer its start from the last nested bullet in a current file.
            start = max(0, end + 1 - line_counts[learning_id])
        else:
            while start > 0 and not re.match(r'^- \*\*.+?\*\*', lines[start]):
                previous = lines[start - 1]
                if line_marker_id(previous) or previous.startswith('#') or not previous.strip():
                    break
                start -= 1
            if not re.match(r'^- \*\*.+?\*\*', lines[start]) and re.match(r'^\s*[-*+]\s+', lines[end]):
                start = end
        content = ''.join(lines[start:end + 1])
        units.append({'learning_id': learning_id, 'start_line': start + 1, 'end_line': end + 1,
                      'end_byte': len(''.join(lines[:end + 1]).encode('utf-8')),
                      'content': content, 'content_hash': text_hash(content),
                      'recognized': bool(re.match(r'^\s*[-*+]\s+', lines[start]))})
    return units


def delivered_bullet(before, applied, learning_id):
    """Locate a new unit inside its actual changed block, excluding human context.

    Existing marked units use the producer's bold-lead boundary. An unformatted
    multiline edit without an unambiguous boundary is left unknown.
    """
    lines = applied.splitlines(keepends=True)
    ends = [i for i, line in enumerate(lines) if line_marker_id(line) == learning_id]
    if len(ends) != 1:
        raise AvailabilityError('No unique delivered marker')
    end = ends[0]
    if not any(line_marker_id(line) == learning_id for line in before.splitlines()):
        for tag, _, _, first, last in difflib.SequenceMatcher(
                a=before.splitlines(keepends=True), b=lines, autojunk=False).get_opcodes():
            if tag != 'equal' and first <= end < last:
                # Adjacent delivered units have their own marker boundary.
                for i in range(first, end):
                    if line_marker_id(lines[i]): first = i + 1
                while first < end and (not lines[first].strip() or lines[first].startswith('#')):
                    first += 1
                if not re.match(r'^\s*[-*+]\s+', lines[first]):
                    break
                return ''.join(lines[first:end + 1])
        raise AvailabilityError('New marker lacks an attributable changed rule block')
    unit = next(u for u in marked_units(applied) if u['learning_id'] == learning_id)
    if not unit['recognized'] or not re.match(r'^- \*\*.+?\*\*', unit['content']):
        raise AvailabilityError('Edited rule has no unambiguous retained unit boundary')
    return unit['content']


def identity_cache(store):
    return {r['remote_norm']: project_identity.ProjectIdentity(r['project_key'], r['display'], r['method'])
            for r in store.query('SELECT * FROM project_identity_cache')}


def resolve_project(path, cache):
    # Recheck the local remote on every collection; retained host IDs may come
    # from the existing cache. A miss does not perform a network lookup.
    def offline(_):
        raise LookupError('no retained repository identity; availability collection is offline')
    return project_identity.resolve(path, cache=cache, gh=offline)


def capture_revision(store, cfg, event, *, captured_at, cache):
    """Read one exact application, even after this proposal was applied again."""
    source = application_source(store, cfg, event['proposal_id'], event_id=event['id'])
    proposal, destination = source['proposal'], source['destination']
    learning_id = proposal['learning_id']
    units = [u for u in marked_units(source['applied']) if u['learning_id'] == learning_id]
    if len(units) != 1 or not units[0]['recognized']:
        raise AvailabilityError(f"application {event['id']}: no unique retained marked rule unit")
    kind = 'file' if proposal['target_kind'] in {'rule_file', 'skill'} else 'bullet'
    content = source['applied'] if kind == 'file' else delivered_bullet(source['before'], source['applied'], learning_id)
    global_target = proposal['target_kind'] in {'global_claude_md', 'codex_global'}
    if proposal['target_kind'] == 'skill':
        global_target = Path(destination['target_path']).is_relative_to(Path(cfg.skills_dir).resolve())
    project = None if global_target else resolve_project(destination['repo_root'] or str(Path(destination['target_path']).parent), cache)
    if project is not None and project.method not in {'gh_repo_id', 'remote_url', 'git_root'}:
        raise AvailabilityError(f"application {event['id']}: project identity is not a verified repository")
    record = {'version': 1, 'learning_id': learning_id, 'proposal_id': event['proposal_id'],
              'application_id': source['application_id'], 'application_event_id': event['id'],
              'applied_at': timestamp(event['ts'], 'application '+event['id']),
              'project_key': project.key if project else '', 'destination': destination,
              'unit_kind': kind, 'content': content, 'content_hash': text_hash(content),
              'snapshot_before': source['snapshot_before'], 'snapshot_after': source['snapshot_after'],
              'before_hash': text_hash(source['before']), 'applied_hash': text_hash(source['applied']),
              'contribution_hash': source['contribution_hash'], 'method': 'retained_application_snapshots',
              'captured_at': timestamp(captured_at, 'revision capture')}
    record['id'] = digest({k: v for k, v in record.items() if k != 'captured_at'})
    return record


def validate_revision(record):
    owner = 'rule_revision.'+str(record.get('id', '?')) if isinstance(record, dict) else 'rule_revision'
    try:
        if (record['version'] != 1 or record['unit_kind'] not in {'file', 'bullet'}
                or not record['content'] or text_hash(record['content']) != record['content_hash']
                or record['id'] != digest({k: v for k, v in record.items() if k not in {'id', 'captured_at'}})
                or record['method'] != 'retained_application_snapshots'
                or any(not isinstance(record[k], str) or not record[k] for k in
                       ('learning_id', 'proposal_id', 'application_id', 'application_event_id', 'snapshot_before', 'snapshot_after'))):
            raise ValueError('changed identity, shape, or content')
        for key in ('applied_at', 'captured_at'):
            if timestamp(record[key], owner) != record[key]:
                raise ValueError('noncanonical timestamp')
        if not isinstance(record['project_key'], str) or not isinstance(record['destination'], dict):
            raise ValueError('invalid destination or project')
    except (KeyError, TypeError, ValueError) as exc:
        raise AvailabilityError(f'{owner}: invalid delivered revision: {exc}') from exc
    return record


def read_record(row, table):
    owner = table+'.'+row['id']
    try:
        record = json.loads(row['record_json'])
        if not isinstance(record, dict) or digest(record) != row['record_hash']:
            raise ValueError('changed fingerprint or non-object record')
        if any(record.get(k) != row[k] for k in row if k not in {'record_json', 'record_hash', 'created_at'}):
            raise ValueError('indexed values differ from the record')
    except (TypeError, ValueError) as exc:
        raise AvailabilityError(owner+': invalid stored record') from exc
    if table == 'rule_revisions':
        if row['created_at'] != record.get('captured_at'):
            raise AvailabilityError(owner+': capture timestamp differs from the indexed value')
        validate_revision(record)
    return record


def record_revision(store, record):
    require_write(store)
    validate_revision(record)
    previous = store.query_one('SELECT * FROM rule_revisions WHERE application_event_id=? AND learning_id=?',
                               (record['application_event_id'], record['learning_id']))
    if previous:
        saved = read_record(previous, 'rule_revisions')
        if saved['id'] != record['id']:
            raise AvailabilityError('application '+record['application_event_id']+': delivered revision changed')
        return saved['id']
    fields = ('id', 'learning_id', 'proposal_id', 'application_id', 'application_event_id', 'project_key', 'content_hash')
    store.insert('rule_revisions', {**{k: record[k] for k in fields}, 'created_at': record['captured_at'],
                                   'record_json': encoded(record), 'record_hash': digest(record)})
    return record['id']


def retained_revisions(store):
    require_schema(store)
    return [read_record(row, 'rule_revisions') for row in store.query('SELECT * FROM rule_revisions ORDER BY created_at,id')]


def application_end(store, revision):
    """Read the exact application's closed lifetime, without current-file inference.

    A successor proposal does not share its predecessor's application identity.
    Missing origin/lifecycle evidence is unknown and cannot establish an open life.
    """
    origin=store.query_one("SELECT id FROM proposal_events WHERE id=? AND proposal_id=? AND event='applied'",
                           (revision['application_event_id'],revision['proposal_id']))
    if not origin:
        return {'at':None,'cause':'application_history_missing','event_id':None}
    endings=[]
    for event in store.query("SELECT id,ts,note FROM proposal_events WHERE proposal_id=? AND event='rolled_back' ORDER BY ts,id",(revision['proposal_id'],)):
        when=timestamp(event['ts'],'rollback event '+event['id'])
        if when<revision['applied_at']: continue
        try: note=json.loads(event['note'])
        except (ValueError,TypeError): note=None
        if not isinstance(note,dict) or not note.get('applied_event_id'):
            endings.append({'at':when,'cause':'rollback_application_unbound','event_id':event['id']})
        elif note['applied_event_id']==revision['application_event_id']:
            if note.get('application_id')!=revision['application_id']:
                raise AvailabilityError('rollback event '+event['id']+': application binding differs')
            endings.append({'at':when,'cause':'verified_rollback','event_id':event['id']})
    return min(endings,key=lambda r:(r['at'],r['event_id'])) if endings else None
