"""Native session evidence and conservative startup candidates, never loading receipts.

The scanner owns publication. Readers open no transcript, instruction file, model
or default Store. See docs/dashboard-parity/SESSION_ELIGIBILITY.md.
"""
from __future__ import annotations

import base64
from collections import defaultdict
import json
import re

from . import scan_observations as so
from . import rule_availability as availability
from .rule_revisions import read_record, validate_revision, timestamp

MIGRATION = '0030_session_context'
PROFILE = 'session-context/1'
TABLES = ('session_context_batches', 'session_context_records')
KINDS = {'creation', 'turn', 'compaction'}
SCAN_COLUMNS = 'id,transcript_id,compatibility_key,manifest_id,observed_at,outcome,projection,line_end,pending_bytes'
NATIVE_FIELDS = {'kind', 'reported_started_at', 'start_time_status', 'provider_version',
                 'thread_id', 'root_session_id', 'turn_id', 'inherited_history', 'metadata_issue'}
BINDINGS = ('observation_id', 'line_key', 'line_no', 'source', 'project_key',
            'working_copy_id', 'logical_session_key', 'occurred_at')
RECORD_FIELDS = NATIVE_FIELDS | set(BINDINGS) | {'id', 'profile'}
BATCH_FIELDS = {'observation_id', 'profile', 'transcript_id', 'compatibility_key', 'record_ids'}


class SessionContextError(ValueError):
    """Corrupt evidence or a required explicit upgrade."""


class SessionContextRequestError(SessionContextError):
    """Invalid selection or cursor."""


def _identifier(value):
    return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,200}', value) else ''


def native_context(kind, payload):
    """Extract a content-free native allowlist; do not interpret message bodies."""
    if kind not in KINDS or not isinstance(payload, dict):
        raise SessionContextError('Invalid native context shape')
    raw = payload.get('timestamp') if kind == 'creation' else None
    started = so.normalize_timestamp(raw)
    issue = ''
    for field in ('id', 'session_id', 'turn_id', 'cli_version'):
        if payload.get(field) is not None and not _identifier(payload[field]):
            issue = 'invalid_native_identity'
    if kind == 'creation' and not _identifier(payload.get('id')):
        issue = 'invalid_native_identity'
    return {'kind': kind, 'reported_started_at': started or '',
            'start_time_status': 'reported' if started else 'missing' if raw in (None, '') else 'invalid',
            'thread_id': _identifier(payload.get('id')) if kind == 'creation' else '',
            'root_session_id': _identifier(payload.get('session_id')) if kind == 'creation' else '',
            'turn_id': _identifier(payload.get('turn_id')) if kind == 'turn' else '',
            'provider_version': _identifier(payload.get('cli_version')) if kind == 'creation' else '',
            'inherited_history': any(payload.get(k) is not None for k in (
                'forked_from_id', 'parent_thread_id', 'history_base', 'subagent_history_start_ordinal')),
            'metadata_issue': issue}


def _schema(store):
    if not store.query_one('SELECT name FROM schema_migrations WHERE name=?', (MIGRATION,)):
        raise SessionContextError(f'Session context requires explicit migration {MIGRATION}')
    for table in TABLES:
        if not store.query_one("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)):
            raise SessionContextError('Session context schema missing table '+table)


def _object(raw, owner):
    try:
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError('not an object')
        so.canonical_json(value)
        return value
    except (TypeError, ValueError) as exc:
        raise SessionContextError(owner+': invalid strict JSON object') from exc


def _validate_native(record, owner):
    if set(record) != NATIVE_FIELDS or record['kind'] not in KINDS:
        raise SessionContextError(owner+': invalid native context fields')
    if (type(record['inherited_history']) is not bool
            or record['metadata_issue'] not in {'', 'invalid_native_identity'}
            or record['start_time_status'] not in {'reported', 'missing', 'invalid'}):
        raise SessionContextError(owner+': invalid native context status')
    for field in ('provider_version', 'thread_id', 'root_session_id', 'turn_id'):
        if not isinstance(record[field], str) or (record[field] and _identifier(record[field]) != record[field]):
            raise SessionContextError(owner+': invalid '+field)
    when = record['reported_started_at']
    if record['start_time_status'] == 'reported':
        if record['kind'] != 'creation' or not when or so.normalize_timestamp(when) != when:
            raise SessionContextError(owner+': invalid reported creation time')
    elif when != '':
        raise SessionContextError(owner+': unknown creation time must be empty')


def _validate_record(record, owner):
    if set(record) != RECORD_FIELDS or record['profile'] != PROFILE:
        raise SessionContextError(owner+': invalid context record/profile')
    _validate_native({k: record[k] for k in NATIVE_FIELDS}, owner)
    if record['source'] != 'codex' or type(record['line_no']) is not int or record['line_no'] < 1:
        raise SessionContextError(owner+': invalid context source/line')
    for field in set(BINDINGS)-{'line_no'}:
        if not isinstance(record[field], str):
            raise SessionContextError(owner+': invalid binding '+field)
    if record['occurred_at'] and so.normalize_timestamp(record['occurred_at']) != record['occurred_at']:
        raise SessionContextError(owner+': invalid line timestamp')
    if record['id'] != so.content_id({k:v for k,v in record.items() if k != 'id'}):
        raise SessionContextError(owner+': context content differs from identity')


def record_scan_context(store, *, observation, lines, contexts):
    """Publish after record_scan, within its caller-owned per-file transaction."""
    owner = 'session context '+str(observation.get('id'))
    _schema(store)
    if store.read_only or not store.conn.in_transaction:
        raise SessionContextError(owner+': requires caller active write transaction')
    scan = store.query_one('SELECT * FROM scan_observations WHERE id=?', (observation['id'],))
    if (not scan or scan['outcome'] != 'succeeded' or scan['projection'] != 'complete'
            or scan['transcript_id'] != observation['transcript_id']
            or scan['compatibility_key'] != observation['manifest']['compatibility_key']):
        raise SessionContextError(owner+': missing or mismatched complete scan observation')
    by_line = {line['line_no']:line for line in lines}
    persisted = {row['line_no']:row for row in store.query(
        'SELECT line_no,line_key,source,project_key,working_copy_id,logical_session_key,occurred_at '
        'FROM scan_lines WHERE transcript_id=? AND compatibility_key=? AND active=1',
        (scan['transcript_id'],scan['compatibility_key']))}
    records = []
    for line_no, metadata in contexts:
        _validate_native(metadata, owner)
        line = by_line.get(line_no)
        if not line or line['exclusion'] == 'denied' or line['source'] != 'codex':
            raise SessionContextError(owner+': context has no allowed physical line')
        record = {**metadata, 'profile':PROFILE, 'observation_id':scan['id'],
                  'line_no':line_no, 'line_key':line['line_key'], 'source':line['source'],
                  'project_key':line['project_key'],
                  'working_copy_id':line['working_copy']['id'] if line['working_copy'] else '',
                  'logical_session_key':line['logical_session_key'], 'occurred_at':line['occurred_at']}
        stored_line=persisted.get(line_no)
        if not stored_line or any(record[k]!=stored_line[k] for k in BINDINGS if k!='observation_id'):
            raise SessionContextError(owner+': context differs from persisted physical line')
        record['id']=so.content_id(record)
        _validate_record(record, owner)
        records.append(record)
    records.sort(key=lambda r:r['line_no'])
    if len({r['line_no'] for r in records}) != len(records):
        raise SessionContextError(owner+': duplicate native context line')
    batch = {'observation_id':scan['id'], 'profile':PROFILE, 'transcript_id':scan['transcript_id'],
             'compatibility_key':scan['compatibility_key'], 'record_ids':[r['id'] for r in records]}
    encoded = so.canonical_json(batch)
    prior = store.query_one('SELECT * FROM session_context_batches WHERE observation_id=?', (scan['id'],))
    if prior:
        loaded = _read_batch(store, scan)
        if prior['record_json'] != encoded or loaded != records:
            raise SessionContextError(owner+': replay differs from retained context')
        return scan['id']
    store.insert('session_context_batches', {'observation_id':scan['id'], 'profile':PROFILE,
                 'content_hash':so.content_id(batch), 'record_count':len(records), 'record_json':encoded})
    for record in records:
        store.insert('session_context_records', {k:record[k] for k in ('id', *BINDINGS, 'kind')} |
                     {'record_json':so.canonical_json(record)})
    return scan['id']


def _read_batch(store, scan):
    owner = 'session context '+scan['id']
    row = store.query_one('SELECT * FROM session_context_batches WHERE observation_id=?', (scan['id'],))
    if row is None:
        return None
    batch = _object(row['record_json'], owner)
    if (set(batch) != BATCH_FIELDS or batch['profile'] != PROFILE or row['profile'] != PROFILE
            or batch['observation_id'] != scan['id'] or batch['transcript_id'] != scan['transcript_id']
            or batch['compatibility_key'] != scan['compatibility_key']
            or row['content_hash'] != so.content_id(batch)
            or not isinstance(batch['record_ids'], list)):
        raise SessionContextError(owner+': invalid context batch or scan binding')
    records = []
    for child in store.query('SELECT * FROM session_context_records WHERE observation_id=? ORDER BY line_no', (scan['id'],)):
        record = _object(child['record_json'], owner+' record '+child['id'])
        _validate_record(record, owner+' record '+child['id'])
        if any(child[k] != record[k] for k in ('id', *BINDINGS, 'kind')):
            raise SessionContextError(owner+': indexed context columns differ from content')
        records.append(record)
    if row['record_count'] != len(records) or batch['record_ids'] != [r['id'] for r in records]:
        raise SessionContextError(owner+': incomplete or changed context children')
    return records


def _selection(store, project_key, compatibility_key):
    """Read one version; verify manifests before trusting their indexed keys."""
    if not isinstance(project_key, str) or not project_key.strip():
        raise SessionContextRequestError('A canonical project key is required')
    if compatibility_key is not None and (not isinstance(compatibility_key, str) or not compatibility_key):
        raise SessionContextRequestError('Invalid session compatibility key')
    # Keep wide scan provenance JSON out of this reader. Context batches bind
    # only these headers; the existing scan-history reader supplies full detail.
    scans=store.query(f'SELECT {SCAN_COLUMNS} FROM scan_observations WHERE transcript_id IN ('
        'SELECT transcript_id FROM scan_lines WHERE project_key=? UNION '
        'SELECT s.transcript_id FROM session_context_records r JOIN scan_observations s '
        'ON s.id=r.observation_id WHERE r.project_key=?) ORDER BY observed_at,id',
        (project_key,project_key))
    cache, versions = {}, {}
    for scan in scans:
        manifest = so._load_manifest(store, scan['manifest_id'], 'session scan '+scan['id'], cache)
        if scan['compatibility_key'] != manifest['compatibility_key']:
            raise SessionContextError('session scan '+scan['id']+': manifest compatibility differs')
        item=versions.setdefault(scan['compatibility_key'], {'compatibility_key':scan['compatibility_key'],
                                  'identifiable':manifest['identifiable'], 'observations':0})
        item['observations'] += 1
    choices=sorted(versions.values(),key=lambda r:r['compatibility_key'])
    if compatibility_key is None and len(choices)>1:
        return None, choices, [], 'incompatible_versions'
    selected=compatibility_key or (choices[0]['compatibility_key'] if choices else None)
    if not selected or selected not in versions:
        return selected, choices, [], 'uncovered_version' if selected else 'missing_observations'
    if not versions[selected]['identifiable']:
        return selected, choices, [], 'unidentifiable_version'
    return selected, choices, [s for s in scans if s['compatibility_key']==selected], ''


def _population(store, project_key, compatibility_key):
    selected, versions, scans, reason = _selection(store, project_key, compatibility_key)
    base={'project_key':project_key,'compatibility_key':selected,'version_groups':versions,'reason':reason,
          'records':[],'count':None,'coverage':None,'runtime_loading_verified':False,'in_force_sessions':None}
    if reason:
        return base
    latest, complete = {}, {}
    for scan in scans:
        tid=scan['transcript_id'];latest[tid]=scan
        if scan['outcome']=='succeeded' and scan['projection']=='complete':
            complete[tid]=scan
    records, missing_batches, failed, pending = [], [], [], []
    for tid, scan in complete.items():
        children=_read_batch(store,scan)
        if children is None: missing_batches.append(scan['id'])
        else: records.extend(children)
        if latest[tid]['outcome']=='failed': failed.append(latest[tid]['id'])
        if scan['pending_bytes']: pending.append({'observation_id':scan['id'],'bytes':scan['pending_bytes']})
    # Retained scan lines are the population. Metadata is not a second denominator.
    lines=store.query('SELECT project_key,logical_session_key,working_copy_id,transcript_id,source,'
        "COUNT(*) physical_lines,SUM(occurred_at='') unknown_time_lines,"
        "MIN(NULLIF(occurred_at,'')) first_at,MAX(NULLIF(occurred_at,'')) last_at "
        'FROM scan_lines WHERE compatibility_key=? AND active=1 '
        'GROUP BY project_key,logical_session_key,working_copy_id,transcript_id,source', (selected,))
    by_transcript=defaultdict(int)
    all_by_session=defaultdict(list)
    for row in lines:
        by_transcript[row['transcript_id']]+=row['physical_lines']
        all_by_session[row['logical_session_key']].append(row)
    incomplete_projections = [s['id'] for tid,s in complete.items()
                              if by_transcript[tid] != s['line_end']]
    selected_lines=[r for r in lines if r['project_key']==project_key]
    wanted={r['logical_session_key'] for r in selected_lines if r['logical_session_key']}
    # Include context from other copies/projects of the same logical session;
    # otherwise a move or conflicting creation could grant false startup credit.
    extra_tids={r['transcript_id'] for r in lines if r['logical_session_key'] in wanted} - set(complete)
    for tid in extra_tids:
        extra=store.query_one(f"SELECT {SCAN_COLUMNS} FROM scan_observations WHERE transcript_id=? AND compatibility_key=? "
                              "AND outcome='succeeded' AND projection='complete' ORDER BY observed_at DESC,id DESC LIMIT 1", (tid,selected))
        if extra:
            manifest=so._load_manifest(store,extra['manifest_id'],'session scan '+extra['id'],{})
            if manifest['compatibility_key']!=selected:
                raise SessionContextError('session scan '+extra['id']+': incompatible extra context')
            children=_read_batch(store,extra)
            if children is None: missing_batches.append(extra['id'])
            else: records.extend(children)
            scans.append(extra)
            if by_transcript[tid] != extra['line_end']:
                incomplete_projections.append(extra['id'])
            latest_extra=store.query_one(f'SELECT {SCAN_COLUMNS} FROM scan_observations WHERE transcript_id=? AND compatibility_key=? '
                                         'ORDER BY observed_at DESC,id DESC LIMIT 1',(tid,selected))
            if latest_extra['outcome']=='failed':
                failed.append(latest_extra['id']);scans.append(latest_extra)
            if extra['pending_bytes']:
                pending.append({'observation_id':extra['id'],'bytes':extra['pending_bytes']})
    by_session={}
    for row in selected_lines:
        if not row['logical_session_key']: continue
        key=(row['logical_session_key'],row['working_copy_id'])
        by_session.setdefault(key,[]).append(row)
    context_by_session=defaultdict(list)
    for record in records: context_by_session[record['logical_session_key']].append(record)
    scans_by_transcript=defaultdict(list)
    for record in scans: scans_by_transcript[record['transcript_id']].append(record)
    grouped=[]
    for (sid, copy_id), members in sorted(by_session.items()):
        context=context_by_session[sid]
        creations=[r for r in context if r['kind']=='creation']
        all_lines=all_by_session[sid]
        known=sorted(t for r in members for t in (r['first_at'],r['last_at']) if t)
        tids={r['transcript_id'] for r in all_lines}
        relevant_scans=[s for tid in tids for s in scans_by_transcript[tid]]
        relevant_missing=[s for s in missing_batches if any(r['id']==s for r in relevant_scans)]
        start, cause = None, ''
        if relevant_missing: cause='context_profile_missing'
        elif not creations: cause='native_creation_missing'
        elif any(r['metadata_issue'] for r in creations): cause='invalid_native_identity'
        elif any(r['inherited_history'] for r in creations): cause='inherited_history'
        elif any(r['root_session_id'] and r['thread_id'] and r['root_session_id']!=r['thread_id'] for r in creations):
            cause='shared_root_session_identity'
        elif any(r['start_time_status']!='reported' for r in creations): cause='native_creation_time_unknown'
        elif len({(r['reported_started_at'],r['working_copy_id']) for r in creations}) != 1:
            cause='conflicting_creation_records'
        elif any(r['working_copy_id']!=copy_id for r in creations): cause='creation_in_different_copy'
        elif not copy_id: cause='working_copy_unknown'
        elif any(not r['occurred_at'] for r in creations): cause='creation_line_time_unknown'
        else:
            start=creations[0]['reported_started_at']
            times=[r['first_at'] for r in all_lines if r['first_at']]
            if times and start>min(times): start,cause=None,'creation_after_retained_activity'
        copy=store.query_one('SELECT normalized_path FROM scan_working_copies WHERE id=?',(copy_id,)) if copy_id else None
        grouped.append({'id':so.content_id(['session-copy/1',sid,copy_id]), 'logical_session_key':sid,
            'working_copy_id':copy_id,'working_copy_path':copy['normalized_path'] if copy else '',
            'source':members[0]['source'], 'first_recorded_at':known[0] if known else None,
            'last_recorded_at':known[-1] if known else None,'physical_lines':sum(r['physical_lines'] for r in members),
            'unknown_time_lines':sum(r['unknown_time_lines'] for r in members),
            'reported_started_at':start, 'start_reason':cause,'native_context':context,
            'scan_observation_ids':sorted({r['id'] for r in relevant_scans}),
            'context_batch_ids':sorted({r['observation_id'] for r in context}),
            'coverage_issues':(['context_profile_missing'] if relevant_missing else []) +
                (['latest_scan_failed'] if any(s['id'] in failed for s in relevant_scans) else []) +
                (['incomplete_physical_projection'] if any(s['id'] in incomplete_projections for s in relevant_scans) else []),
            'runtime_loading_verified':False,'continuity_verified':False})
    base.update(records=grouped,count=len(grouped),coverage={
        'logical_sessions':len({r['logical_session_key'] for r in selected_lines if r['logical_session_key']}),
        'session_copy_pairs':len(grouped),'physical_lines':sum(r['physical_lines'] for r in selected_lines),
        'unknown_session_lines':sum(r['physical_lines'] for r in selected_lines if not r['logical_session_key']),
        'unknown_time_lines':sum(r['unknown_time_lines'] for r in selected_lines),
        'missing_context_batches':sorted(set(missing_batches)), 'failed_scan_observations':failed,
        'incomplete_physical_projections':incomplete_projections,
        'pending_tails':pending,'reported_creation_pairs':sum(r['reported_started_at'] is not None for r in grouped),
        'unknown_creation_pairs':sum(r['reported_started_at'] is None for r in grouped)})
    if not complete:
        base.update(count=None,reason='no_complete_projection')
    elif incomplete_projections:
        base.update(count=None,reason='incomplete_physical_projection')
    return base


def _revision(store, revision_id, project_key=None):
    row=store.query_one('SELECT * FROM rule_revisions WHERE id=?',(revision_id,))
    if not row:
        raise SessionContextRequestError('No retained rule revision with that ID')
    revision=validate_revision(read_record(row,'rule_revisions'))
    if project_key and revision['project_key'] not in {'',project_key}:
        raise SessionContextRequestError('Rule revision belongs to a different project')
    return revision


def _qualify(store, record, revision_id, start, end):
    result={'status':'unknown','reason':record['start_reason'],'intervals':[],
            'runtime_loading_verified':False,'continuity_verified':False,'in_force':None}
    if record['coverage_issues']:
        result['reason']=record['coverage_issues'][0];return result
    if record['reported_started_at'] is None:
        return result
    periods=availability.availability_intervals(store,rule_revision_id=revision_id,
        working_copy_id=record['working_copy_id'],start='1900-01-01T00:00:00.000000Z',
        end='9999-12-31T23:59:59.999999Z')
    # A confirming check at the exclusive window end can support the preceding
    # interval. Read retained check history first, then clip candidate time;
    # the observation timestamp is not an occurrence added to the numerator.
    result['availability']=periods
    created=record['reported_started_at']
    if not periods['periods']:
        result['reason']=periods['reason'] or 'no_available_period';return result
    if created<periods['periods'][0]['start']:
        result.update(status='excluded',reason='started_before_observed_availability');return result
    boundary_times=[]
    for event in record['native_context']:
        if event['kind']=='compaction' or (event['kind']=='turn' and event['working_copy_id']!=record['working_copy_id']):
            if not event['occurred_at']:
                result['reason']='untimed_context_discontinuity';return result
            boundary_times.append(event['occurred_at'])
    supported_scope=temporal_match=False
    for period in periods['periods']:
        if not period['start']<=created<=period['confirmed_through']: continue
        temporal_match=True
        scopes=[json.loads(s) for s in period['scopes']]
        if not any(s['provider']==record['source'] and s['scope']['kind'] in {'global','project_always_loaded'}
                   and not s['scope']['paths'] and not s['conditions'] for s in scopes): continue
        supported_scope=True
        lower=max(start,created,period['start'])
        upper=min(end,period['confirmed_through'],period['end'] or end,*boundary_times)
        if lower<upper:
            result['intervals'].append({'start':lower,'end':upper,'observation_ids':period['observation_ids'],
                                        'meaning':'startup_candidate_between_discrete_file_checks'})
    result.update(status='candidate' if result['intervals'] else 'unknown',
                  reason='' if result['intervals'] else 'no_confirmed_continuity' if supported_scope else
                  'provider_or_loading_scope_unverified' if temporal_match else 'no_observation_supported_start')
    return result


def session_population(store, *, project_key, compatibility_key=None):
    """Validated retained native starts and coverage; no availability or receipt inference."""
    _schema(store)
    return _population(store, project_key, compatibility_key)


def session_eligibility(store, *, rule_revision_id, working_copy_id, compatibility_key, start, end):
    """Return candidate intervals for one copy and version; never claim receipt."""
    _schema(store)
    start,end=timestamp(start,'session window start'),timestamp(end,'session window end')
    if start>=end: raise SessionContextRequestError('Session window requires start < end')
    copy=store.query_one('SELECT project_key FROM scan_working_copies WHERE id=?',(working_copy_id,))
    if not copy: raise SessionContextRequestError('Unknown working copy')
    _revision(store,rule_revision_id,copy['project_key'])
    result=_population(store,copy['project_key'],compatibility_key)
    records=[]
    for row in result['records']:
        if row['working_copy_id']!=working_copy_id: continue
        if row['first_recorded_at'] and row['first_recorded_at']>=end: continue
        if row['last_recorded_at'] and row['last_recorded_at']<start: continue
        records.append({**row,'qualification':_qualify(store,row,rule_revision_id,start,end)})
    return {**result,'records':records,'count':len(records) if result['count'] is not None else None,
            'rule_revision_id':rule_revision_id,'working_copy_id':working_copy_id,'start':start,'end':end}


def project_sessions(store, *, project_key, compatibility_key=None, rule_revision_id=None,
                     working_copy_id=None, limit=20, cursor=None):
    """Stable pages of one version. Every selector is bound to the cursor."""
    if type(limit) is not int or not 1<=limit<=100:
        raise SessionContextRequestError('Session page limit must be from 1 to 100')
    for name,value in (('project',project_key),('version',compatibility_key),('revision',rule_revision_id),('copy',working_copy_id)):
        if (name=='project' or value is not None) and (not isinstance(value,str) or not value.strip()):
            raise SessionContextRequestError('Invalid session '+name+' selector')
    selector=so.content_id(['project-sessions/1',project_key,compatibility_key,rule_revision_id,working_copy_id])
    position=''
    if cursor is not None:
        try:
            value=json.loads(base64.urlsafe_b64decode(cursor).decode())
            if set(value)!={'selector','position'} or value['selector']!=selector or not re.fullmatch('[a-f0-9]{64}',value['position']):
                raise ValueError('wrong selector')
            position=value['position']
        except (TypeError, ValueError, KeyError, UnicodeError) as exc:
            raise SessionContextRequestError('Invalid session cursor or different selection') from exc
    if not store.query_one('SELECT name FROM schema_migrations WHERE name=?',(MIGRATION,)):
        return {'project_key':project_key,'compatibility_key':compatibility_key,'version_groups':[],
                'records':[],'count':None,'next_cursor':None,'coverage':None,'reason':'schema_unavailable',
                'runtime_loading_verified':False,'in_force_sessions':None,'rule_revisions':[]}
    _schema(store)
    if working_copy_id is not None:
        copy=store.query_one('SELECT project_key FROM scan_working_copies WHERE id=?',(working_copy_id,))
        if not copy or copy['project_key']!=project_key:
            raise SessionContextRequestError('Working copy is unknown or belongs to another project')
    result=_population(store,project_key,compatibility_key)
    if rule_revision_id: _revision(store,rule_revision_id,project_key)
    revisions=[_revision(store,r['id'],project_key) for r in store.query(
        "SELECT id FROM rule_revisions WHERE project_key=? OR project_key='' ORDER BY id",(project_key,))]
    rows=[r for r in result['records'] if working_copy_id is None or r['working_copy_id']==working_copy_id]
    if working_copy_id is not None and not rows and not result['reason']:
        result.update(count=None,reason='uncovered_working_copy')
    remaining=sorted((r for r in rows if r['id']>position),key=lambda r:r['id'])
    page=remaining[:limit]
    # Inspection covers retained history. The recurrence interface takes explicit
    # half-open windows, so changing a UI filter cannot redefine a comparison.
    if rule_revision_id:
        page=[{**r,'qualification':_qualify(store,r,rule_revision_id,'1900-01-01T00:00:00.000000Z','9999-12-31T23:59:59.999999Z')} for r in page]
    next_cursor=base64.urlsafe_b64encode(so.canonical_json({'selector':selector,'position':page[-1]['id']}).encode()).decode() if len(remaining)>limit else None
    return {**result,'records':page,'count':len(rows) if result['count'] is not None else None,
            'next_cursor':next_cursor,'rule_revision_id':rule_revision_id,'working_copy_id':working_copy_id,
            'rule_revisions':[{'id':r['id'],'learning_id':r['learning_id'],'applied_at':r['applied_at'],
                               'content_hash':r['content_hash']} for r in revisions]}
