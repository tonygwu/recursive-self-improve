"""Observed working-copy availability, explicit persistence, and read-only history."""
from __future__ import annotations

from collections import Counter
from datetime import datetime
import json
import os

from .instruction_surfaces import PROFILE, inspect_surfaces
from .mining_history import digest, encoded
from .rule_revisions import (AvailabilityError, MIGRATION, capture_revision, identity_cache,
    marked_units, read_record, record_revision, require_schema, require_write,
    resolve_project, retained_revisions, timestamp, validate_revision)
from .scan_observations import working_copy_identity
from .store import utc_now_iso

STATUSES = frozenset({'available', 'changed', 'absent', 'unknown'})


def _public_surface(surface):
    return {**{k: v for k, v in surface.items() if k not in {'_text', 'marked_units'}},
            'marked_units': [{k: v for k, v in unit.items() if k != 'content'} for unit in surface['marked_units']]}


def inspect_working_copy(cfg, *, project_key, working_copy_path, revisions, observed_at, _identity_failure=None, _surface=None):
    """Read actual files outside a DB transaction; return immutable observation candidates."""
    if observed_at is not None: timestamp(observed_at, 'availability observation')
    if not isinstance(project_key, str) or not project_key:
        raise AvailabilityError('A canonical project key is required')
    selected = [validate_revision(r) for r in revisions if r['project_key'] in {'', project_key}]
    copy = working_copy_identity(project_key, working_copy_path)
    surface = _surface if _surface is not None else ({'files': [], 'issues': [{'cause': 'working_copy_identity_changed'}],
                'profile': PROFILE, 'runtime_loading_verified': False}
               if _identity_failure else inspect_surfaces(cfg, working_copy_path,
                   global_targets=[r['destination']['target_path'] for r in selected if not r['project_key']]))
    # The live clock is sampled after this copy's reads. A collection that
    # takes minutes cannot backdate its last copy to the first copy's check.
    at = timestamp(observed_at or utc_now_iso(), 'availability observation')
    public = {k: v for k, v in surface.items() if k != 'files'}
    public.update(file_count=len(surface['files']), observed_bytes=sum(f['bytes'] for f in surface['files']))
    observations = []
    for revision in selected:
        matches, changed, ambiguous = [], [], []
        revision_files = []
        for item in surface['files']:
            units = [u for u in marked_units(item['_text'], line_counts={
                revision['learning_id']: len(revision['content'].splitlines())})
                if u['learning_id'] == revision['learning_id']] if revision['unit_kind'] == 'bullet' else [
                    u for u in item['marked_units'] if u['learning_id'] == revision['learning_id']]
            if not units: continue
            revision_files.append({**_public_surface(item), 'marked_units': [
                {k: v for k, v in unit.items() if k != 'content'} for unit in units]})
            entry = {'path': item['path'], 'real_path': item['real_path'], 'file_hash': item['content_hash'],
                     'loading_paths': item['loading_paths']}
            if len(units) != 1 or not units[0]['recognized']:
                ambiguous.append(entry); continue
            end_byte = item['bytes'] if revision['unit_kind'] == 'file' else units[0]['end_byte']
            entry['loading_paths'] = [p for p in item['loading_paths'] if p['eligible_prefix_bytes'] >= end_byte]
            if not entry['loading_paths']:
                ambiguous.append(entry); continue
            same = (item['_text'] == revision['content'] if revision['unit_kind'] == 'file'
                    else units[0]['content'] == revision['content'])
            (matches if same else changed).append(entry)
        status, cause = ('available', '') if matches else ('changed', 'marked_content_changed') if changed else ('absent', 'no_matching_marked_content')
        if ambiguous and not matches:
            status, cause = 'unknown', 'ambiguous_marked_content'
        elif not matches and surface['issues']:
            status, cause = 'unknown', 'incomplete_surface_inspection'
        if at < revision['applied_at']:
            status, cause = 'unknown', 'application_after_observation'
        record = {'version': 1, 'rule_revision_id': revision['id'], 'learning_id': revision['learning_id'],
                  'application_id': revision['application_id'], 'project_key': project_key,
                  'working_copy_id': copy['id'], 'working_copy': copy, 'observed_at': at,
                  'status': status, 'cause': cause, 'content_hash': revision['content_hash'],
                  'method': 'snapshot_unit_to_observed_surface/1', 'matches': matches,
                  'changed_locations': changed, 'ambiguous_locations': ambiguous,
                  'inspection': {**public, 'files': revision_files},
                  'run_id': ''}
        record['id'] = digest(record)
        observations.append(record)
    return observations


def _validate_observation(record):
    owner = 'availability.'+str(record.get('id', '?')) if isinstance(record, dict) else 'availability'
    try:
        if (record['version'] != 1 or record['status'] not in STATUSES
                or record['id'] != digest({k: v for k, v in record.items() if k != 'id'})
                or record['observed_at'] != timestamp(record['observed_at'], owner)
                or any(not isinstance(record[k], str) or not record[k] for k in
                       ('rule_revision_id', 'learning_id', 'application_id', 'project_key', 'working_copy_id', 'content_hash'))
                or not isinstance(record['run_id'], str)
                or record['working_copy_id'] != digest([record['project_key'], record['working_copy']['normalized_path']])
                or record['working_copy']['id'] != record['working_copy_id']
                or record['method'] != 'snapshot_unit_to_observed_surface/1'
                or any(not isinstance(record[k], list) for k in ('matches', 'changed_locations', 'ambiguous_locations'))
                or not isinstance(record['inspection'], dict)):
            raise ValueError('invalid identity or shape')
        if record['status'] == 'available' and (not record['matches'] or record['cause']):
            raise ValueError('available observation lacks an exact content match')
    except (KeyError, TypeError, ValueError) as exc:
        raise AvailabilityError(owner+': invalid observation: '+str(exc)) from exc
    return record


def _bind_revision_impl(record, revision):
    if (any(record[k] != revision[k] for k in ('learning_id', 'application_id', 'content_hash'))
            or revision['project_key'] not in {'', record['project_key']}):
        raise AvailabilityError('availability '+record['id']+': observation differs from its delivered revision')
    if record['status'] == 'available':
        if record['observed_at'] < revision['applied_at']:
            raise AvailabilityError('availability '+record['id']+': availability predates application')
        for match in record['matches']:
            files = [f for f in record['inspection']['files'] if f['real_path'] == match['real_path']]
            if len(files) != 1:
                raise AvailabilityError('availability '+record['id']+': matching file is not uniquely observed')
            file = files[0]
            units = [u for u in file['marked_units'] if u['learning_id'] == revision['learning_id']]
            expected = file['content_hash'] if revision['unit_kind'] == 'file' else units[0]['content_hash'] if len(units) == 1 else None
            end_byte = file['bytes'] if revision['unit_kind'] == 'file' else units[0]['end_byte']
            if (expected != revision['content_hash'] or file['content_hash'] != match['file_hash']
                    or not match['loading_paths'] or any(p not in file['loading_paths']
                    or p['eligible_prefix_bytes'] < end_byte for p in match['loading_paths'])):
                raise AvailabilityError('availability '+record['id']+': content or loading path lacks observation evidence')
    return record


def _bind_revision(record, revision):
    try:
        return _bind_revision_impl(record, revision)
    except AvailabilityError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise AvailabilityError('availability '+record['id']+': invalid match evidence') from exc


def _loading_scopes(record):
    return sorted({encoded({k: path[k] for k in ('provider', 'scope', 'conditions')})
                   for match in record['matches'] for path in match['loading_paths']})


def _check_signature(record):
    return digest([record['status'], _loading_scopes(record)])


def record_availability(store, *, observations):
    """Validate and write in the caller's active write transaction; never commit."""
    require_write(store)
    prepared = {}
    for record in observations:
        _validate_observation(record)
        row = store.query_one('SELECT * FROM rule_revisions WHERE id=?', (record['rule_revision_id'],))
        if row is None:
            raise AvailabilityError('availability '+record['id']+': missing delivered revision')
        revision = read_record(row, 'rule_revisions')
        _bind_revision(record, revision)
        previous = store.query_one('SELECT * FROM rule_availability_observations WHERE id=?', (record['id'],))
        if previous:
            if read_record(previous, 'rule_availability_observations') != record:
                raise AvailabilityError('availability '+record['id']+': replay differs from retained observation')
        else:
            prepared[record['id']] = record
    for record in prepared.values():
        keys = ('id', 'rule_revision_id', 'project_key', 'working_copy_id', 'observed_at', 'status', 'run_id')
        store.insert('rule_availability_observations', {**{k: record[k] for k in keys},
                     'record_json': encoded(record), 'record_hash': digest(record)})
    return [r['id'] for r in observations]


def collect_availability(store, cfg, *, run_id='', observed_at=None):
    """Reach real delivery snapshots and actual known copies, then publish atomically.

    The pipeline calls this after scanning. CLI collection is also available
    without a mining/model run. No observation is invented for an untracked copy.
    """
    require_schema(store)
    from . import instruction_inventory as inventory
    inventory.require_schema(store)
    from . import instruction_text
    instruction_text.require_schema(store)
    if store.conn.in_transaction:
        raise AvailabilityError('Collect availability outside a database transaction')
    at = timestamp(observed_at or utc_now_iso(), 'availability collection')
    cache = identity_cache(store)
    revisions = retained_revisions(store)
    known = {r['application_event_id'] for r in revisions}
    errors = []
    applications = store.query("SELECT * FROM proposal_events WHERE event='applied' ORDER BY ts,id")
    latest_applications = {event['proposal_id']: event['id'] for event in applications}
    for event in applications:
        if event['id'] in known: continue
        try:
            revisions.append(capture_revision(store, cfg, event, captured_at=at, cache=cache))
        except (AvailabilityError, ValueError, OSError) as exc:
            errors.append({'subject': 'application:'+event['id'], 'cause': getattr(exc, 'code', type(exc).__name__), 'detail': str(exc)})
    copies = {}
    for row in store.query("SELECT DISTINCT project_key,project_path FROM sessions WHERE project_key<>'' AND project_path<>''"):
        copy = working_copy_identity(row['project_key'], row['project_path'])
        copies[(row['project_key'], copy['id'])] = (row['project_key'], row['project_path'])
    if store.query_one("SELECT name FROM schema_migrations WHERE name='0022_scan_observations'"):
        for row in store.query('SELECT project_key,normalized_path FROM scan_working_copies'):
            copy = working_copy_identity(row['project_key'], row['normalized_path'])
            copies[(row['project_key'], copy['id'])] = (row['project_key'], row['normalized_path'])
    for revision in revisions:
        if revision['project_key'] and revision['destination']['repo_root']:
            path = revision['destination']['repo_root']
            copy = working_copy_identity(revision['project_key'], path)
            copies[(revision['project_key'], copy['id'])] = (revision['project_key'], path)
    observations, inventories, text_archives = [], [], []
    for (project_key, copy_id), (_, path) in sorted(copies.items()):
        relevant = [r for r in revisions if r['project_key'] in {'', project_key}]
        actual = resolve_project(path, cache)
        surface = ({'files': [], 'issues': [{'cause': 'working_copy_identity_changed'}],
                    'profile': PROFILE, 'runtime_loading_verified': False}
                   if actual.key != project_key else inspect_surfaces(cfg, path,
                       global_targets=[*(p for p in (cfg.global_claude_md, cfg.codex_global_agents_md) if os.path.lexists(p)),
                           *(r['destination']['target_path'] for r in relevant if not r['project_key'])]))
        copy_at = timestamp(observed_at or utc_now_iso(), 'inventory observation')
        from .instruction_metrics import collect as collect_metrics
        measurements = collect_metrics(surface, path)
        inventories.append(inventory.build_inventory(surface=surface, project_key=project_key,
            working_copy_path=path, revisions=relevant, observed_at=copy_at, run_id=run_id, measurements=measurements))
        text_archives.append(instruction_text.build_archive(surface, inventories[-1]))
        batch = inspect_working_copy(cfg, project_key=project_key, working_copy_path=path,
                                     revisions=relevant, observed_at=copy_at, _surface=surface)
        for record in batch:
            revision = next(r for r in relevant if r['id'] == record['rule_revision_id'])
            latest = latest_applications.get(revision['proposal_id'])
            from .rule_revisions import application_end
            ending = application_end(store, revision)
            if ending and (ending['at'] is None or copy_at >= ending['at']):
                record.update(status='unknown', cause=ending['cause'], matches=[])
            if latest and latest != revision['application_event_id']:
                record.update(status='unknown', cause='application_superseded', matches=[])
            if actual.key != project_key:
                record.update(status='unknown', cause='working_copy_identity_changed', matches=[])
                record['observed_project_identity'] = {'key': actual.key, 'method': actual.method}
            record['run_id'] = run_id
            record['id'] = digest({k: v for k, v in record.items() if k != 'id'})
        observations.extend(batch)
    counts = dict(sorted(Counter(r['status'] for r in observations).items()))
    causes = Counter(r['cause'] for r in observations if r['cause'])
    causes.update(e['cause'] for e in errors)
    unique_issues = {(r['working_copy_id'], encoded(issue)) for r in inventories for issue in r['issues']}
    coverage_causes = Counter(json.loads(issue)['cause'] for _, issue in unique_issues)
    causes.update(coverage_causes)
    record = {'version': 1, 'run_id': run_id, 'observed_at': timestamp(observed_at or utc_now_iso(), 'collection completion'),
              'status': 'partial' if errors or counts.get('unknown') or unique_issues else 'recorded' if observations or inventories else 'unavailable',
              'reason': '' if observations else 'No observable delivered revisions and known working copies overlap.',
              'known_working_copies': len(copies), 'tracked_revisions': len(revisions),
              'observations': len(observations), 'outcomes': counts, 'causes': dict(sorted(causes.items())),
              'surface_issues_by_cause': dict(sorted(coverage_causes.items())),
              'errors': errors, 'observation_ids': [r['id'] for r in observations],
              'inventory_observations': len(inventories), 'inventory_ids': [r['id'] for r in inventories],
              'scope': 'Retained delivered rule units in known working copies; no inference of session loading.'}
    record['id'] = digest(record)
    # Every filesystem read has finished. An interruption rolls back this entire
    # collection, its new revision records, and its observations together.
    with store.transaction(write=True):
        for revision in revisions: record_revision(store, revision)
        record_availability(store, observations=observations)
        inventory.record_inventories(store, records=inventories)
        instruction_text.record_archives(store, text_archives)
        previous = store.query_one('SELECT * FROM rule_availability_collections WHERE id=?', (record['id'],))
        if previous:
            if read_record(previous, 'rule_availability_collections') != record:
                raise AvailabilityError('availability collection '+record['id']+': replay changed')
        else:
            store.insert('rule_availability_collections', {**{k: record[k] for k in ('id', 'run_id', 'observed_at', 'status')},
                         'record_json': encoded(record), 'record_hash': digest(record)})
    return record


def availability_intervals(store, *, rule_revision_id, working_copy_id, start, end):
    """Observation-supported half-open periods; never extrapolate past the last check."""
    require_schema(store)
    start, end = timestamp(start, 'interval start'), timestamp(end, 'interval end')
    if start >= end: raise AvailabilityError('Availability interval requires start < end')
    revision_row = store.query_one('SELECT * FROM rule_revisions WHERE id=?', (rule_revision_id,))
    if revision_row is None:
        raise AvailabilityError('availability '+rule_revision_id+': delivered revision missing')
    revision = read_record(revision_row, 'rule_revisions')
    records = [_validate_observation(read_record(row, 'rule_availability_observations')) for row in store.query(
        'SELECT * FROM rule_availability_observations WHERE rule_revision_id=? AND working_copy_id=? AND observed_at<? ORDER BY observed_at,id',
        (rule_revision_id, working_copy_id, end))]
    for record in records: _bind_revision(record, revision)
    grouped = {}
    for record in records:
        grouped.setdefault(record['observed_at'], []).append(record)
    checks = []
    for same_time in grouped.values():
        if len({_check_signature(r) for r in same_time}) > 1:
            checks.append({**same_time[0], 'status': 'unknown', 'cause': 'conflicting_simultaneous_observations', 'matches': []})
        else:
            checks.append(same_time[0])
    periods, active = [], None
    for record in checks:
        when = record['observed_at']
        if record['status'] == 'available':
            scopes = _loading_scopes(record)
            if active and active['scopes'] != scopes:
                active['end'] = when; periods.append(active); active = None
            if active is None:
                active = {'start': when, 'end': None, 'confirmed_through': when, 'scopes': scopes, 'observation_ids': []}
            active['confirmed_through'] = when
            active['observation_ids'].append(record['id'])
        elif active:
            active['end'] = when; periods.append(active); active = None
    if active: periods.append(active)
    from .rule_revisions import application_end
    ending = application_end(store, revision)
    if ending and ending['at'] is None:
        periods = []
    elif ending:
        periods = [{**p, 'end':min(p['end'] or ending['at'], ending['at']),
                    'confirmed_through':min(p['confirmed_through'],ending['at'])}
                   for p in periods if p['start'] < ending['at']]
    gaps = [(datetime.fromisoformat(b['observed_at'])-datetime.fromisoformat(a['observed_at'])).total_seconds()
            for a,b in zip(records,records[1:])]
    visible = [p for p in periods if p['start'] < end and (p['end'] or end) > start]
    return {'rule_revision_id': rule_revision_id, 'working_copy_id': working_copy_id, 'start': start, 'end': end,
            'periods': visible, 'observation_count': len(records), 'computable': bool(records),
            'reason': ending['cause'] if ending and not periods else '' if records else 'no_availability_observations',
            'application_end': ending,
            'first_observed_at': records[0]['observed_at'] if records else None,
            'last_observed_at': records[-1]['observed_at'] if records else None,
            'largest_observation_gap_seconds': max(gaps, default=None),
            'unknown_before_first_observation': True, 'unknown_after_last_observation': True,
            'session_eligibility_computable': False,
            'meaning': 'Availability is supported by discrete observations. Changes between them and per-session loading remain unverified; qualify sessions by their own start and loading scope.'}


def project_availability(store, *, project_key, limit=20, cursor=None):
    """Latest retained checks per revision/copy, with stable selector-owned pages."""
    import base64
    if not isinstance(project_key, str) or not project_key or type(limit) is not int or not 1 <= limit <= 100:
        raise AvailabilityError('Invalid availability project or page limit')
    selector = digest(['project-availability/1', project_key])
    position = None
    if cursor is not None:
        try:
            token = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
            if (not isinstance(token, dict) or token.get('selector') != selector
                    or not isinstance(token.get('position'), list) or len(token['position']) != 2
                    or any(not isinstance(v, str) or len(v) != 64 for v in token['position'])):
                raise ValueError('wrong selector or position')
            position = token['position']
        except (TypeError, ValueError, UnicodeError) as exc:
            raise AvailabilityError('Invalid availability cursor or different project') from exc
    if not store.query_one('SELECT name FROM schema_migrations WHERE name=?', (MIGRATION,)):
        return {'project_key': project_key, 'records': [], 'count': None, 'next_cursor': None,
                'computable': False, 'reason': 'schema_unavailable'}
    require_schema(store)
    params = [project_key]
    after = ''
    if position:
        after = ' AND (rule_revision_id>? OR (rule_revision_id=? AND working_copy_id>?))'
        params.extend([position[0], position[0], position[1]])
    pairs = store.query('SELECT rule_revision_id,working_copy_id,MAX(observed_at) AS observed_at '
                        'FROM rule_availability_observations WHERE project_key=?'+after+
                        ' GROUP BY rule_revision_id,working_copy_id ORDER BY rule_revision_id,working_copy_id LIMIT ?',
                        (*params, limit+1))
    count = store.query_one('SELECT COUNT(*) AS n FROM (SELECT rule_revision_id,working_copy_id '
                            'FROM rule_availability_observations WHERE project_key=? GROUP BY rule_revision_id,working_copy_id) counted',
                            (project_key,))['n']
    records = []
    for pair in pairs[:limit]:
        revision_row = store.query_one('SELECT * FROM rule_revisions WHERE id=?', (pair['rule_revision_id'],))
        if not revision_row:
            raise AvailabilityError('availability '+pair['rule_revision_id']+': delivered revision missing')
        revision = read_record(revision_row, 'rule_revisions')
        latest = [_validate_observation(read_record(row, 'rule_availability_observations')) for row in store.query(
            'SELECT * FROM rule_availability_observations WHERE rule_revision_id=? AND working_copy_id=? AND observed_at=? ORDER BY id',
            (pair['rule_revision_id'], pair['working_copy_id'], pair['observed_at']))]
        for observation in latest:
            _bind_revision(observation, revision)
        signatures = {_check_signature(r) for r in latest}
        status = latest[0]['status'] if len(signatures) == 1 else 'unknown'
        records.append({'revision': revision, 'working_copy_id': pair['working_copy_id'], 'observed_at': pair['observed_at'],
                        'status': status, 'conflicting_observations': len(signatures) > 1, 'observations': latest})
    next_cursor = None
    if len(pairs) > limit:
        last = pairs[limit-1]
        next_cursor = base64.urlsafe_b64encode(encoded({'selector': selector,
            'position': [last['rule_revision_id'], last['working_copy_id']]}).encode()).decode()
    collection_row = store.query_one('SELECT * FROM rule_availability_collections ORDER BY observed_at DESC,id DESC LIMIT 1')
    collection = read_record(collection_row, 'rule_availability_collections') if collection_row else None
    return {'project_key': project_key, 'records': records, 'count': count, 'next_cursor': next_cursor,
            'last_collection': collection,
            'computable': bool(records), 'reason': '' if records else 'no_retained_observations',
            'meaning': 'These are recorded working-copy checks. Branch delivery, markers alone, and an already-running session do not prove that a rule was loaded.'}
