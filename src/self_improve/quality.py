"""Frozen human assessment and explicit policy intent; no model or target writes."""
from __future__ import annotations

import json
import re
import sqlite3

from .commands import CommandError, _hash, _json, _parsed
from .execution_policy import TARGET_CLASSES, TARGET_CLASS, PolicyError, policy_snapshot, set_class_policy
from .store import new_id, utc_now_iso

MIGRATION = '0029_quality_evidence'
ACTIONS = frozenset({'create_quality_sample', 'judge_quality', 'set_class_policy'})
JUDGMENTS = ('useful', 'not_useful', 'uncertain')
METHOD = 'applied-contribution-sample/1'
TABLES = ('quality_subjects', 'quality_samples', 'quality_judgments', 'evidence_command_results')


def available(store):
    if not store.query_one('SELECT name FROM schema_migrations WHERE name=?', (MIGRATION,)):
        return False
    for table in TABLES:
        try:
            store.query(f'SELECT id FROM {table} LIMIT 0')
        except sqlite3.DatabaseError as exc:
            raise CommandError('QualityDataError', table+': installed schema is damaged.', 500) from exc
    return True


def require_schema(store):
    if not available(store):
        raise CommandError('UpgradeRequired', 'Upgrade the database before recording quality or policy commands.', 503)


def _timestamp(value):
    from .rule_revisions import timestamp, AvailabilityError
    try:
        return timestamp(value, 'quality evidence')
    except AvailabilityError as exc:
        raise CommandError('InvalidEvidenceTime', str(exc)) from exc


def _read(row, table):
    if row is None:
        raise CommandError('QualityDataError', table+': missing record.', 500)
    record = _parsed(row, 'record_json')
    if (_hash(record) != row['record_hash'] or type(record.get('version')) is not int or record.get('version') != 1
            or any(record.get(k) != row[k] for k in row if k not in {'record_json', 'record_hash', 'created_at'})
            or (table != 'quality_subjects' and record.get('created_at') != row['created_at'])):
        raise CommandError('QualityDataError', table+'.'+row['id']+': changed identity or record.', 500)
    required = {
        'quality_samples': {'command_id': str, 'preview': dict},
        'quality_judgments': {'command_id': str, 'previous_id': str, 'judgment': str, 'reviewer': str, 'note': str},
        'evidence_command_results': {'request': dict, 'result': dict},
        'quality_subjects': {},
    }[table]
    if any(type(record.get(k)) is not typ for k, typ in required.items()):
        raise CommandError('QualityDataError', table+'.'+row['id']+': invalid record shape.', 500)
    return record


def _subject(source):
    fields = ('application_id', 'contribution_hash', 'destination', 'before', 'applied',
              'before_exists', 'snapshot_before', 'snapshot_after')
    record = {k: source[k] for k in fields}
    record.update(version=1, target_class=TARGET_CLASS.get(source['destination']['target_kind']))
    record['id'] = _hash([record['application_id'], record['contribution_hash']])
    return _validate_subject(record)


def _validate_subject(record):
    try:
        if (record['target_class'] not in TARGET_CLASSES
                or TARGET_CLASS.get(record['destination']['target_kind']) != record['target_class']
                or record['id'] != _hash([record['application_id'], record['contribution_hash']])
                or record['contribution_hash'] != _hash({k: record[k] for k in ('before', 'applied', 'before_exists')})
                or type(record['before_exists']) is not bool
                or any(not isinstance(record[k], str) for k in ('before', 'applied'))
                or any(not isinstance(record[k], str) or not record[k] for k in
                       ('application_id', 'snapshot_before', 'snapshot_after'))):
            raise ValueError('invalid applied contribution')
    except (KeyError, TypeError, ValueError) as exc:
        raise CommandError('QualityDataError', 'quality subject '+str(record.get('id'))+': invalid source.', 500) from exc
    return record


def population(store, cfg):
    """Verify real applications; saved subjects survive later snapshot loss.

    Historical applications without frozen proposal/destination provenance are
    explicit gaps. No current learning text supplies their missing revision.
    """
    from .rollback import application_source, RollbackError
    subjects = {}
    if available(store):
        for row in store.query('SELECT * FROM quality_subjects ORDER BY id'):
            source = _validate_subject(_read(row, 'quality_subjects'))
            subjects[source['id']] = {'source': source, 'links': []}
    excluded = []
    events = store.query("SELECT * FROM proposal_events WHERE event='applied' ORDER BY ts,id")
    for event in events:
        try:
            note = json.loads(event['note'])
            if not isinstance(note, dict) or not (note.get('command_id') or note.get('operation_id')):
                raise RollbackError('UnfrozenHistoricalApplication', 'No immutable applied proposal and destination are retained.', 409)
            actual = application_source(store, cfg, event['proposal_id'], event_id=event['id'])
            source = _subject(actual)
            previous = subjects.setdefault(source['id'], {'source': source, 'links': []})
            if previous['source'] != source:
                raise CommandError('QualityDataError', source['id']+': retained application source changed.', 500)
            previous['links'].append({'event_id': event['id'], 'proposal_id': event['proposal_id'],
                                      'learning_id': actual['proposal']['learning_id'], 'applied_at': event['ts']})
        except (RollbackError, ValueError, OSError) as exc:
            # Integrity failures are never downgraded to an empty denominator.
            if isinstance(exc, CommandError) and not isinstance(exc, RollbackError):
                raise
            excluded.append({'event_id': event['id'], 'proposal_id': event['proposal_id'],
                             'cause': getattr(exc, 'code', type(exc).__name__), 'detail': str(exc)})
    return {'subjects': [subjects[k] for k in sorted(subjects)], 'application_events': len(events),
            'excluded_applications': excluded, 'method': METHOD}


def _selection(target_class, size, seed):
    if target_class not in TARGET_CLASSES or type(size) is not int or not 1 <= size <= 100:
        raise CommandError('InvalidQualitySample', 'Choose a target class and a sample size from 1 to 100.')
    if not isinstance(seed, str) or not 1 <= len(seed) <= 100:
        raise CommandError('InvalidQualitySample', 'The reproducible seed must contain 1–100 characters.')


def preview(store, cfg, *, target_class, size=20, seed='quality-1'):
    require_schema(store)
    _selection(target_class, size, seed)
    data = population(store, cfg)
    rows = [r for r in data['subjects'] if r['source']['target_class'] == target_class]
    ordered = sorted(rows, key=lambda r: (_hash([METHOD, seed, r['source']['id']]), r['source']['id']))
    shown = {'method': METHOD, 'target_class': target_class, 'size': size, 'seed': seed,
             'eligible': len(rows), 'application_events': data['application_events'],
             'excluded_applications': data['excluded_applications'],
             'population': [{'id': r['source']['id'], 'source_hash': _hash(r['source'])} for r in rows],
             'selected': ordered[:size], 'max_model_calls': 0}
    return {**shown, 'revision': _hash(shown)}


def judgments(store, subject_id):
    require_schema(store)
    result = []
    for row in store.query('SELECT * FROM quality_judgments WHERE subject_id=? ORDER BY sequence', (subject_id,)):
        record = _read(row, 'quality_judgments')
        if (record['sequence'] != len(result)+1 or record.get('previous_id') != (result[-1]['id'] if result else '')
                or record.get('judgment') not in JUDGMENTS or record.get('reviewer') != 'local_operator'
                or not isinstance(record.get('note'), str)):
            raise CommandError('QualityDataError', row['id']+': invalid judgment history.', 500)
        result.append(record)
    return result


def sample(store, sample_id):
    require_schema(store)
    row = store.query_one('SELECT * FROM quality_samples WHERE id=?', (sample_id,))
    if row is None:
        raise CommandError('NoSuchQualitySample', 'No retained quality sample with this ID.', 404)
    record = _read(row, 'quality_samples')
    shown = record['preview']
    shape = {'revision':str, 'method':str, 'target_class':str, 'size':int, 'seed':str,
             'eligible':int, 'application_events':int, 'max_model_calls':int,
             'population':list, 'selected':list, 'excluded_applications':list}
    if (any(type(shown.get(k)) is not typ for k,typ in shape.items())
            or any(not isinstance(r,dict) or set(r)!={'id','source_hash'}
                   or any(not isinstance(v,str) or re.fullmatch(r'[a-f0-9]{64}',v) is None for v in r.values())
                   for r in shown.get('population',[]))
            or any(not isinstance(r,dict) or set(r)!={'source','links'}
                   or not isinstance(r['source'],dict) or not isinstance(r['links'],list)
                   for r in shown.get('selected',[]))):
        raise CommandError('QualityDataError', sample_id+': invalid sample manifest shape.', 500)
    if (_hash({k:v for k,v in shown.items() if k != 'revision'}) != shown['revision']
            or shown['method'] != METHOD or shown['target_class'] != record['target_class']):
        raise CommandError('QualityDataError', sample_id+': sample manifest changed.', 500)
    try:
        _selection(shown['target_class'], shown['size'], shown['seed'])
    except CommandError as exc:
        raise CommandError('QualityDataError',sample_id+': invalid sample selection.',500) from exc
    for item in shown['selected']:
        _validate_subject(item['source'])
    manifest = {r['id']: r['source_hash'] for r in shown['population']}
    expected = sorted(manifest, key=lambda rid: (_hash([METHOD, shown['seed'], rid]), rid))[:shown['size']]
    if (len(manifest) != len(shown['population']) or shown['eligible'] != len(manifest)
            or [r['source']['id'] for r in shown['selected']] != expected):
        raise CommandError('QualityDataError', sample_id+': sample selection differs from its population.', 500)
    selected = []
    for item in shown['selected']:
        source = _validate_subject(item['source'])
        saved = _read(store.query_one('SELECT * FROM quality_subjects WHERE id=?', (source['id'],)), 'quality_subjects')
        if saved != source or _hash(source) != manifest[source['id']]:
            raise CommandError('QualityDataError', source['id']+': sample differs from its applied revision.', 500)
        history = judgments(store, source['id'])
        selected.append({**item, 'judgment': history[-1] if history else None, 'history': history})
    return {**record, 'subjects': selected, 'counts': _counts(selected, shown['eligible'])}


def _counts(rows, eligible):
    counts = {key: sum(r.get('judgment') is not None and r['judgment']['judgment'] == key for r in rows) for key in JUDGMENTS}
    reviewed = sum(counts.values())
    denominator = counts['useful'] + counts['not_useful']
    return {**counts, 'reviewed': reviewed, 'selected': len(rows), 'eligible': eligible,
            'unreviewed': len(rows)-reviewed, 'precision_numerator': counts['useful'],
            'precision_denominator': denominator,
            'precision': counts['useful']/denominator if denominator >= 20 else None}


def samples(store, *, target_class=None, limit=20, cursor=None):
    require_schema(store)
    if target_class is not None and target_class not in TARGET_CLASSES:
        raise CommandError('InvalidQualitySample', 'Unknown target class.')
    if type(limit) is not int or not 1 <= limit <= 100:
        raise CommandError('InvalidLimit', 'Use a limit from 1 to 100.')
    where, args = (' WHERE target_class=?', [target_class]) if target_class else ('', [])
    count = store.query_one('SELECT COUNT(*) n FROM quality_samples'+where, args)['n']
    if cursor:
        row = store.query_one('SELECT * FROM quality_samples WHERE id=?', (cursor,))
        if row is None or (target_class and row['target_class'] != target_class):
            raise CommandError('InvalidCursor', 'Use a cursor from this sample selection.')
        where += (' AND ' if where else ' WHERE ')+'(created_at<? OR (created_at=? AND id<?))'
        args += [row['created_at'], row['created_at'], row['id']]
    rows = store.query('SELECT * FROM quality_samples'+where+' ORDER BY created_at DESC,id DESC LIMIT ?', (*args,limit+1))
    records = []
    for row in rows[:limit]:
        data = sample(store, row['id'])
        records.append({k:data[k] for k in ('id','target_class','created_at','counts')} | {'seed':data['preview']['seed']})
    return {'records':records, 'count':count, 'next_cursor':rows[limit-1]['id'] if len(rows)>limit else None}


def _request(body):
    action = body.get('action')
    fields = {'create_quality_sample': {'target_class','size','seed','preview_revision'},
              'judge_quality': {'sample_id','subject_id','judgment','note','expected_revision'},
              'set_class_policy': {'target_class','enabled','expected_revision'}}
    if not isinstance(action,str) or action not in fields or set(body) != fields[action] | {'action','request_key'}:
        raise CommandError('InvalidEvidenceCommand', 'Use the exact fields for this evidence command.')
    if not isinstance(body['request_key'],str) or re.fullmatch(r'[A-Za-z0-9._:-]{8,200}',body['request_key']) is None:
        raise CommandError('InvalidRequestKey','Use a unique request key of 8–200 characters.')
    if action == 'create_quality_sample':
        _selection(body['target_class'],body['size'],body['seed'])
        if not isinstance(body['preview_revision'],str) or re.fullmatch(r'[a-f0-9]{64}',body['preview_revision']) is None:
            raise CommandError('InvalidRevision','Inspect the current quality sample preview first.')
    else:
        if type(body['expected_revision']) is not int or body['expected_revision'] < 0:
            raise CommandError('InvalidRevision','Use the current nonnegative revision number.')
        if action == 'set_class_policy':
            if body['target_class'] not in TARGET_CLASSES or type(body['enabled']) is not bool:
                raise CommandError('InvalidPolicy','Choose a known class and a boolean enabled value.')
        elif (body['judgment'] not in JUDGMENTS or not isinstance(body['note'],str)
              or any(not isinstance(body[k],str) or not body[k] for k in ('sample_id','subject_id'))):
            raise CommandError('InvalidJudgment','Choose a retained sample subject and a known judgment with a text note.')
    return body


def submit(store, cfg, body, *, now=None):
    from .jobs import _unused_key
    require_schema(store)
    request = _request(body)
    stamp = _timestamp(now or utc_now_iso())
    request_hash = _hash({k:v for k,v in request.items() if k != 'request_key'})
    with store.transaction(write=True):
        _unused_key(store,request['request_key'])
        existing = store.query_one('SELECT * FROM commands WHERE request_key=?',(request['request_key'],))
        if existing:
            if existing['action'] != request['action'] or existing['request_hash'] != request_hash:
                raise CommandError('IdempotencyConflict','This request key already identifies another command.',409)
            return command_status(store, existing)
        cid = new_id()
        if request['action'] == 'set_class_policy':
            before = policy_snapshot(store)['classes'][request['target_class']]
            try:
                after = set_class_policy(store,request['target_class'],request['enabled'],
                    expected_revision=request['expected_revision'],now=stamp,commit=False)
            except PolicyError as exc:
                raise CommandError('PolicyConflict',str(exc),409) from exc
            result = {'target_class':request['target_class'],'before':before,'after':after}
        elif request['action'] == 'create_quality_sample':
            shown = preview(store,cfg,**{k:request[k] for k in ('target_class','size','seed')})
            if shown['revision'] != request['preview_revision']:
                raise CommandError('StaleQualityPreview','The eligible population changed. Inspect a fresh sample preview.',409)
            if not shown['selected']:
                raise CommandError('EmptyQualitySample','No verified applied revisions are eligible for this class.',409)
            for item in shown['selected']:
                source = item['source']
                if not store.query_one('SELECT id FROM quality_subjects WHERE id=?',(source['id'],)):
                    _insert(store,'quality_subjects',source,stamp)
            sid = new_id()
            record = {'version':1,'id':sid,'target_class':request['target_class'],'created_at':stamp,
                      'command_id':cid,'preview':shown}
            _insert(store,'quality_samples',record,stamp)
            result = {'sample_id':sid,'selected':len(shown['selected']),'eligible':shown['eligible']}
        else:
            data = sample(store,request['sample_id'])
            item = next((r for r in data['subjects'] if r['source']['id'] == request['subject_id']),None)
            if item is None:
                raise CommandError('InvalidSampleSubject','This revision is not in the frozen sample.',409)
            current = item['judgment']
            sequence = current['sequence'] if current else 0
            if sequence != request['expected_revision']:
                raise CommandError('StaleJudgment','Another judgment was recorded. Read its history before revising it.',409)
            record = {'version':1,'id':new_id(),'subject_id':request['subject_id'],'sample_id':request['sample_id'],
                      'sequence':sequence+1,'previous_id':current['id'] if current else '',
                      'created_at':stamp,'reviewer':'local_operator','judgment':request['judgment'],
                      'note':request['note'],'command_id':cid}
            _insert(store,'quality_judgments',record,stamp)
            result = {'sample_id':request['sample_id'],'subject_id':request['subject_id'],
                      'judgment_id':record['id'],'revision':record['sequence'],'judgment':record['judgment']}
        store.insert('commands',{'id':cid,'action':request['action'],'request_key':request['request_key'],
            'request_hash':request_hash,'state':'completed','actor':'user','created_at':stamp,'updated_at':stamp,
            'payload_json':_json(request),'result_json':_json(result),'max_model_calls':0})
        record = {'version':1,'id':cid,'created_at':stamp,'request':request,'result':result}
        _insert(store,'evidence_command_results',record,stamp)
        return command_status(store,store.query_one('SELECT * FROM commands WHERE id=?',(cid,)))


def _insert(store, table, record, stamp):
    fields = {'quality_subjects':('target_class',),'quality_samples':('target_class',),
              'quality_judgments':('subject_id','sample_id','sequence'),'evidence_command_results':()}[table]
    store.insert(table,{'id':record['id'],'created_at':stamp,**{k:record[k] for k in fields},
                        'record_json':_json(record),'record_hash':_hash(record)})


def command_status(store,row):
    require_schema(store)
    record = _read(store.query_one('SELECT * FROM evidence_command_results WHERE id=?',(row['id'],)), 'evidence_command_results')
    request = _request(_parsed(row,'payload_json'))
    result = _parsed(row,'result_json')
    if (record['request'] != request or record['result'] != result or row['state'] != 'completed'
            or row['action'] != request['action'] or row['request_key'] != request['request_key']
            or row['request_hash'] != _hash({k:v for k,v in request.items() if k!='request_key'})
            or row['max_model_calls'] != 0 or row['actor'] != 'user'
            or row['created_at'] != record['created_at']):
        raise CommandError('QualityDataError',row['id']+': evidence command changed.',500)
    return {k:row[k] for k in ('id','action','state','actor','created_at','updated_at','max_model_calls','error_code','error_detail')} | {
        'members':[],'targets':[],'result':result,'record':record,'cancel_requested':False,
        'controls_available':True,'control_history':[],'can_retry':False,'can_cancel':False}
