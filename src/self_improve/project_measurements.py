"""Retained before/after associations; no inference of instruction receipt or cause."""
from __future__ import annotations

import base64
from collections import Counter
from datetime import datetime, timedelta
import hashlib
import json
import statistics
import uuid

from . import scan_observations as so, session_context as sc, rule_availability as availability
from .rule_revisions import read_record, validate_revision, timestamp, require_write, application_end
from .store import utc_now_iso

MIGRATION = '0025_project_measurements'
PROFILE = 'observational_recurrence/1'
MATCH_METHOD = 'retained_incident_learning_links/1'
MIN_TIME = '1900-01-01T00:00:00.000000Z'
MAX_TIME = '9999-12-31T23:59:59.999999Z'


class MeasurementError(ValueError):
    pass


class MeasurementRequestError(MeasurementError):
    pass


def _schema(store, *, missing_ok=False):
    if not store.query_one('SELECT name FROM schema_migrations WHERE name=?', (MIGRATION,)):
        if missing_ok: return False
        raise MeasurementError('Explicit migration required: '+MIGRATION)
    columns = {r['name'] for r in store.query('PRAGMA table_info(project_stats)')}
    if not {'record_json','record_hash','project_key','rule_revision_id','compatibility_key','observed_at','record_type'} <= columns:
        raise MeasurementError('project_stats: applied measurement schema is damaged')
    return True


def _revision(store, rid, project_key):
    row = store.query_one('SELECT * FROM rule_revisions WHERE id=?', (rid,))
    if not row: raise MeasurementRequestError('No retained rule revision with that ID')
    revision = validate_revision(read_record(row, 'rule_revisions'))
    if revision['project_key'] not in {'', project_key}:
        raise MeasurementRequestError('Rule revision belongs to a different project')
    return revision


def _bounds(created, record, start, end):
    boundaries = []
    for event in record['native_context']:
        if event['kind']=='compaction' or (event['kind']=='turn' and event['working_copy_id']!=record['working_copy_id']):
            if not event['occurred_at']: return [], 'untimed_context_discontinuity'
            boundaries.append(event['occurred_at'])
    lo, hi = max(start,created), min(end,*boundaries) if boundaries else end
    return ([{'start':lo,'end':hi}] if lo<hi else []), ''


def _supported(period, source):
    return any(s['provider']==source and s['scope']['kind'] in {'global','project_always_loaded'}
               and not s['scope']['paths'] and not s['conditions']
               for s in map(json.loads,period['scopes']))


def _disjoint_intervals(intervals):
    """Union per-session/copy intervals before batching so no line crosses batches."""
    result=[]
    for current in sorted(intervals,key=lambda r:(r['sid'],r['copy'],r['start'],r['end'])):
        if result and all(result[-1][k]==current[k] for k in ('sid','copy')) and current['start']<=result[-1]['end']:
            result[-1]['end']=max(result[-1]['end'],current['end'])
        else: result.append(dict(current))
    return result


def _selection_sql(project, key, intervals):
    # Standard VALUES, with at most 128 intervals / 512 bound values per query.
    # EXISTS keeps membership distinct even within a qualification batch.
    values=','.join('(?,?,?,?)' for _ in intervals)
    params=tuple(r[k] for r in intervals for k in ('sid','copy','start','end'))
    return ("WITH chosen(sid,copy,start,end) AS (VALUES "+values+"), eligible AS ("
            "SELECT l.* FROM scan_lines l WHERE l.active=1 AND l.project_key=? AND l.compatibility_key=? "
            "AND EXISTS (SELECT 1 FROM chosen c WHERE c.sid=l.logical_session_key AND c.copy=l.working_copy_id "
            "AND l.occurred_at>=c.start AND l.occurred_at<c.end)) ", (*params,project,key))


def _side(store, project, key, intervals, learning_id):
    intervals=_disjoint_intervals(intervals)
    groups=[];occurrences=[];observations=set();digest=hashlib.sha256()
    for offset in range(0,len(intervals),128):
        cte,params=_selection_sql(project,key,intervals[offset:offset+128])
        groups.extend(store.query(cte+"SELECT logical_session_key,source,headless,is_subagent,exclusion,category,"
            "COUNT(*) n FROM eligible GROUP BY logical_session_key,source,headless,is_subagent,exclusion,category",params))
        # Match only retained links. Many-to-many membership is not a multiplier.
        occurrences.extend(store.query(cte+"SELECT o.occurrence_id,o.signal_type,EXISTS (SELECT 1 FROM scan_incident_links sl "
            "JOIN incident_learnings il ON il.incident_id=sl.incident_id WHERE sl.occurrence_id=o.occurrence_id) classified,EXISTS (SELECT 1 FROM scan_incident_links sl "
            "JOIN incident_learnings il ON il.incident_id=sl.incident_id WHERE sl.occurrence_id=o.occurrence_id "
            "AND il.learning_id=?) matched FROM scan_occurrences o JOIN eligible l ON l.transcript_id=o.transcript_id "
            "AND l.compatibility_key=o.compatibility_key AND l.line_key=o.trigger_line_key AND l.line_no=o.trigger_line_no "
            "WHERE o.active=1 AND o.kind='signal' AND o.exclusion='' AND l.exclusion='' "
            "ORDER BY o.occurrence_id",(*params,learning_id)))
        observations.update(r['observation_id'] for r in store.query(cte+'SELECT DISTINCT observation_id FROM eligible',params))
        # Stream line revisions in canonical interval-batch/line-ID order. No
        # transcript bodies, temporary tables, or unbounded line lists are needed.
        position=''
        while True:
            page=store.query(cte+'SELECT id,revision_hash FROM eligible WHERE id>? ORDER BY id LIMIT 1000',(*params,position))
            for r in page: digest.update(so.canonical_json(r).encode()+b'\n')
            if len(page)<1000: break
            position=page[-1]['id']
    accepted=[r for r in groups if not r['exclusion']]
    sessions=Counter();counts=Counter();excluded=Counter()
    for r in accepted:
        sessions[r['logical_session_key']]+=r['n'];counts[r['source']]+=r['n']
    for r in groups:
        if r['exclusion']: excluded[r['exclusion']]+=r['n']
    matched=sorted((r for r in occurrences if r['matched']),key=lambda r:r['occurrence_id'])
    unclassified=sum(not r['classified'] for r in occurrences)
    sizes=sorted(sessions.values());lines=sum(sizes)
    return {'eligible_lines':lines,'observed_lines':sum(r['n'] for r in groups),
        'excluded_lines':dict(sorted(excluded.items())),'sessions':len(sessions),
        'session_size':{'total':lines,'median':statistics.median(sizes) if sizes else None,'max':max(sizes) if sizes else None},
        'workload':{'by_source':dict(sorted(counts.items())),'headless':sum(r['n'] for r in accepted if r['headless']),
                    'subagent':sum(r['n'] for r in accepted if r['is_subagent'])},
        'categories':dict(sorted(Counter({k:sum(r['n'] for r in accepted if r['category']==k) for k in {r['category'] for r in accepted}}).items())),
        'occurrences':len(matched),'occurrences_by_signal':dict(sorted(Counter(r['signal_type'] for r in matched).items())),
        'unmatched_occurrences':len(occurrences)-len(matched),'unclassified_occurrences':unclassified,
        'matching_complete':unclassified==0,'matched_occurrence_ids':[r['occurrence_id'] for r in matched],
        'rate_per_100k':100000*len(matched)/lines if lines and not unclassified else None,
        'scan_observation_ids':sorted(observations),
        'line_projection_hash':digest.hexdigest(),'qualified_intervals':intervals}


def _uncovered(start, end, intervals):
    position=start;gaps=[]
    for lo,hi in sorted(intervals):
        lo,hi=max(start,lo),min(end,hi)
        if lo>=hi: continue
        if lo>position: gaps.append({'start':position,'end':lo})
        position=max(position,hi)
    if position<end: gaps.append({'start':position,'end':end})
    return gaps


def rule_recurrence(store, *, rule_revision_id, project_key, start, end,
                    compatibility_key, window_days=30, min_sessions=20):
    """Compute from retained observations only; explicit persistence is separate."""
    _schema(store)
    if not isinstance(project_key,str) or not project_key: raise MeasurementRequestError('Canonical project key required')
    if compatibility_key is not None and (not isinstance(compatibility_key,str) or not compatibility_key):
        raise MeasurementRequestError('Invalid compatibility key')
    if type(window_days) is not int or not 1<=window_days<=365 or type(min_sessions) is not int or min_sessions<20:
        raise MeasurementRequestError('Window requires 1..365 days and at least 20 sessions')
    start,end=timestamp(start,'measurement start'),timestamp(end,'measurement end')
    if start>=end: raise MeasurementRequestError('Measurement requires start < end')
    revision=_revision(store,rule_revision_id,project_key)
    result={'profile':PROFILE,'project_key':project_key,'rule_revision_id':rule_revision_id,
        'learning_id':revision['learning_id'],'application_id':revision['application_id'],
        'content_hash':revision['content_hash'],'start':start,'end':end,'compatibility_key':compatibility_key,
        'window_days':window_days,'min_sessions':min_sessions,'computable':False,'reason':'',
        'comparison':'unavailable','absolute_rate_change':None,'relative_change_pct':None,
        'coverage_complete':False,'runtime_loading_verified':False,'before':None,'after':None,
        'copies':[],'matching':{'method':MATCH_METHOD,'links':[]},'coverage':{},
        'meaning':'Observed association around first available checks; baseline does not prove absence, and neither side proves model receipt or causation.'}
    exposure=so.exposure_window(store,project_key=project_key,start=start,end=end,compatibility_key=compatibility_key)
    result['version_groups']=exposure['version_groups']
    key=exposure['compatibility_key']
    if key is None or exposure['reason'] in {'unknown_version','uncovered_version','incompatible_versions','missing_observations'}:
        result['reason']=exposure['reason'];return result
    result['compatibility_key']=key
    population=sc.session_population(store,project_key=project_key,compatibility_key=key)
    result['coverage']={'scan':exposure['coverage'],'session_context':population['coverage'],
                        'unknown_time_occurrences':exposure['diagnostics'].get('unknown_time_signal_occurrences'),
                        'unallocated_scope':'Unknown-time/session totals cover the selected retained project/version, not an invented date.'}
    if population['reason']:
        result['reason']=population['reason'];return result
    broken=store.query_one("SELECT o.occurrence_id FROM scan_occurrences o LEFT JOIN scan_lines l "
        "ON l.active=1 AND l.transcript_id=o.transcript_id AND l.compatibility_key=o.compatibility_key "
        "AND l.line_key=o.trigger_line_key AND l.line_no=o.trigger_line_no "
        "WHERE o.active=1 AND o.kind='signal' AND o.project_key=? AND o.compatibility_key=? AND "
        "(l.id IS NULL OR l.project_key<>o.project_key OR l.working_copy_id<>o.working_copy_id "
        "OR l.logical_session_key<>o.logical_session_key OR l.occurred_at<>o.occurred_at OR l.exclusion<>o.exclusion) LIMIT 1",(project_key,key))
    if broken: raise MeasurementError('occurrence '+broken['occurrence_id']+': trigger-line attribution differs or is missing')
    links=store.query("SELECT DISTINCT o.occurrence_id,o.revision_hash,sl.id scan_link_id,sl.incident_id,sl.observation_id,"
        "sl.link_kind FROM scan_occurrences o JOIN scan_incident_links sl ON sl.occurrence_id=o.occurrence_id "
        "JOIN incident_learnings il ON il.incident_id=sl.incident_id WHERE o.active=1 AND o.kind='signal' "
        "AND o.project_key=? AND o.compatibility_key=? AND il.learning_id=? ORDER BY o.occurrence_id,sl.id",
        (project_key,key,revision['learning_id']))
    result['matching']['links']=links
    result['application_end']=application_end(store,revision)
    result['related_revision_ids']=[r['id'] for r in store.query('SELECT id FROM rule_revisions WHERE learning_id=? AND id<>? ORDER BY id',(revision['learning_id'],rule_revision_id))]
    result['baseline_note']='Before the first observed availability of this revision; other revisions and unobserved prior availability can confound the comparison.'
    copy_ids={r['working_copy_id'] for r in population['records']}
    copy_ids.update(r['working_copy_id'] for r in store.query('SELECT DISTINCT working_copy_id FROM rule_availability_observations WHERE rule_revision_id=? AND project_key=?',(rule_revision_id,project_key)))
    selected={'before':[],'after':[]};exclusions={'before':Counter(),'after':Counter()}
    span=timedelta(days=window_days)
    for copy in sorted(copy_ids):
        history=availability.availability_intervals(store,rule_revision_id=rule_revision_id,working_copy_id=copy,start=MIN_TIME,end=MAX_TIME)
        if not history['periods']:
            result['copies'].append({'working_copy_id':copy,'reason':history['reason'] or 'no_available_period','availability':history})
            continue
        first=history['periods'][0];anchor=first['start'];anchor_dt=datetime.fromisoformat(anchor)
        nominal={'before':{'start':so.normalize_timestamp((anchor_dt-span).isoformat()),'end':anchor},
                 'after':{'start':anchor,'end':so.normalize_timestamp((anchor_dt+span).isoformat())}}
        windows={side:{'start':max(start,b['start']),'end':min(end,b['end'])} for side,b in nominal.items()}
        after_gaps=_uncovered(nominal['after']['start'],nominal['after']['end'],[(max(start,p['start']),min(end,p['confirmed_through'],p['end'] or end)) for p in history['periods']])
        result['copies'].append({'working_copy_id':copy,'anchor':anchor,'nominal_windows':nominal,'windows':windows,
            'partial_windows':{'before':windows['before']!=nominal['before'],'after':bool(after_gaps)},
            'uncovered_after_intervals':after_gaps,
            'availability':history,'reason':''})
        for row in population['records']:
            if row['working_copy_id']!=copy: continue
            created=row['reported_started_at']
            for side,bounds in windows.items():
                lo,hi=bounds['start'],bounds['end']
                if lo>=hi: continue
                if row['last_recorded_at'] and row['last_recorded_at']<lo: continue
                if row['first_recorded_at'] and row['first_recorded_at']>=hi: continue
                cause=row['coverage_issues'][0] if row['coverage_issues'] else row['start_reason']
                intervals=[]
                if cause or created is None: cause=cause or 'native_creation_time_unknown'
                elif not lo<=created<hi: cause='session_started_outside_window'
                elif side=='before':
                    if not _supported(first,row['source']): cause='provider_or_loading_scope_unverified'
                    else: intervals,cause=_bounds(created,row,lo,hi)
                else:
                    qualified=sc._qualify(store,row,rule_revision_id,lo,hi)
                    intervals,cause=qualified['intervals'],qualified['reason']
                if not intervals: exclusions[side][cause or 'no_qualified_interval']+=1
                for interval in intervals:
                    selected[side].append({'sid':row['logical_session_key'],'copy':copy,'start':interval['start'],'end':interval['end']})
    for side in selected:
        result[side]=_side(store,project_key,key,selected[side],revision['learning_id'])
        result[side]['excluded_session_pairs']=dict(sorted(exclusions[side].items()))
    if not any(r.get('anchor') for r in result['copies']): result['reason']='missing_observed_availability'
    elif not links: result['reason']='missing_matching_evidence'
    elif not result['before']['eligible_lines'] or not result['after']['eligible_lines']: result['reason']='zero_eligible_exposure'
    elif any(result[s]['unclassified_occurrences'] for s in ('before','after')): result['reason']='unclassified_signal_occurrences'
    elif min(result['before']['sessions'],result['after']['sessions'])<min_sessions: result['reason']='insufficient_sessions'
    else:
        before,after=result['before']['rate_per_100k'],result['after']['rate_per_100k']
        result.update(computable=True,absolute_rate_change=after-before,
                      relative_change_pct=100*(after-before)/before if before else None,
                      comparison='lower' if after<before else 'higher' if after>before else 'unchanged')
    # No mapping is an unknown numerator, not a measured zero.
    if not links:
        for side in ('before','after'):
            result[side]['occurrences']=None;result[side]['rate_per_100k']=None
    return result


def _validate(measurement):
    owner='measurement '+str(measurement.get('rule_revision_id','?')) if isinstance(measurement,dict) else 'measurement'
    try:
        if not isinstance(measurement,dict) or measurement['profile']!=PROFILE: raise ValueError()
        for name in ('project_key','rule_revision_id','learning_id','application_id','content_hash','start','end'):
            if not isinstance(measurement[name],str) or not measurement[name]: raise ValueError()
        if measurement['coverage_complete'] is not False or measurement['runtime_loading_verified'] is not False: raise ValueError()
        if type(measurement['computable']) is not bool or measurement['comparison'] not in {'unavailable','lower','higher','unchanged'}: raise ValueError()
        if measurement['min_sessions']<20 or measurement['start']>=measurement['end']: raise ValueError()
        if so.normalize_timestamp(measurement['start'])!=measurement['start'] or so.normalize_timestamp(measurement['end'])!=measurement['end']: raise ValueError()
        if not isinstance(measurement['matching']['links'],list) or measurement['matching']['method']!=MATCH_METHOD: raise ValueError()
        link_fields={'occurrence_id','revision_hash','scan_link_id','incident_id','observation_id','link_kind'}
        for link in measurement['matching']['links']:
            if not isinstance(link,dict) or set(link)!=link_fields or any(not isinstance(v,str) or not v for v in link.values()): raise ValueError()
            if link['link_kind'] not in {'produced','corroborated'}: raise ValueError()
        if not measurement['computable']:
            if measurement['comparison']!='unavailable' or not measurement['reason'] or measurement['absolute_rate_change'] is not None or measurement['relative_change_pct'] is not None: raise ValueError()
        if measurement['computable']:
            if measurement['reason'] or measurement['comparison']=='unavailable': raise ValueError()
            a,b=measurement['before'],measurement['after']
            if not measurement['matching']['links'] or min(a['sessions'],b['sessions'])<measurement['min_sessions']: raise ValueError()
            for side in (a,b):
                if side['unclassified_occurrences'] or not side['matching_complete']: raise ValueError()
                if type(side['occurrences']) is not int or side['occurrences']<0 or side['eligible_lines']<=0: raise ValueError()
                if side['rate_per_100k']!=100000*side['occurrences']/side['eligible_lines']: raise ValueError()
            change=b['rate_per_100k']-a['rate_per_100k']
            expected='lower' if change<0 else 'higher' if change>0 else 'unchanged'
            if measurement['comparison']!=expected: raise ValueError()
            if change!=measurement['absolute_rate_change'] or measurement['relative_change_pct']!=(100*change/a['rate_per_100k'] if a['rate_per_100k'] else None): raise ValueError()
        so.canonical_json(measurement)
    except (ValueError,TypeError,KeyError,OverflowError) as exc:
        raise MeasurementError(owner+': invalid retained measurement') from exc
    return measurement


def record_project_measurement(store, *, run_id, measurement):
    _schema(store);require_write(store);_validate(measurement)
    if not store.query_one('SELECT id FROM runs WHERE id=?',(run_id,)): raise MeasurementError('Measurement run is missing')
    selection=[PROFILE,run_id,*[measurement[k] for k in ('project_key','rule_revision_id','compatibility_key','start','end','window_days','min_sessions')]]
    rid=str(uuid.uuid5(uuid.NAMESPACE_URL,so.canonical_json(selection)))
    record={'id':rid,'run_id':run_id,'record_type':PROFILE,'observed_at':timestamp(utc_now_iso(),'measurement publication'),
            **{k:measurement[k] for k in ('project_key','rule_revision_id','compatibility_key')},'measurement':measurement}
    record['compatibility_key']=record['compatibility_key'] or ''
    existing=store.query_one('SELECT * FROM project_stats WHERE id=?',(rid,))
    if existing:
        record['observed_at']=_read(existing)['observed_at']
        if _read(existing)!=record: raise MeasurementError('measurement '+rid+': replay differs')
        return rid
    # The public writer cannot accept invented links or stale projections. This
    # runs under the caller's write snapshot; retained readers never recompute.
    fresh=rule_recurrence(store,**{k:measurement[k] for k in ('rule_revision_id','project_key','start','end','compatibility_key','window_days','min_sessions')})
    if fresh!=measurement: raise MeasurementError('measurement '+rid+': source evidence differs from calculation')
    store.insert('project_stats', {k:record[k] for k in ('id','run_id','record_type','observed_at','project_key','rule_revision_id','compatibility_key')} |
        {'project_path':'','period_start':measurement['start'],'period_end':measurement['end'],
         'record_json':so.canonical_json(record),'record_hash':so.content_id(record)})
    return rid


def _read(row):
    owner='measurement '+row['id']
    try:
        record=json.loads(row['record_json'])
        if set(record)!={'id','run_id','record_type','observed_at','project_key','rule_revision_id','compatibility_key','measurement'}: raise ValueError()
        if so.content_id(record)!=row['record_hash']: raise ValueError()
        for k in record.keys()-{'measurement'}:
            if record[k]!=row[k]: raise ValueError()
        m=_validate(record['measurement'])
        if record['record_type']!=PROFILE or so.normalize_timestamp(record['observed_at'])!=record['observed_at']: raise ValueError()
        if any(record[k]!=(m[k] or '') for k in ('project_key','rule_revision_id','compatibility_key')): raise ValueError()
        if row['period_start']!=m['start'] or row['period_end']!=m['end']: raise ValueError()
        selection=[PROFILE,record['run_id'],*[m[k] for k in ('project_key','rule_revision_id','compatibility_key','start','end','window_days','min_sessions')]]
        if record['id']!=str(uuid.uuid5(uuid.NAMESPACE_URL,so.canonical_json(selection))): raise ValueError()
        return record
    except (ValueError,TypeError,KeyError) as exc:
        raise MeasurementError(owner+': invalid record or indexed binding') from exc


def collect_project_measurements(store, *, run_id, observed_at=None):
    _schema(store)
    if store.read_only or store.conn.in_transaction: raise MeasurementError('Measurement collection requires an idle writable Store')
    end=timestamp(observed_at or utc_now_iso(),'measurement collection')
    ids=[];causes=Counter();comparisons=Counter()
    with store.transaction(write=True):
        projects=store.query("SELECT DISTINCT project_key FROM scan_working_copies WHERE project_key<>'' ORDER BY project_key")
        revisions=[read_record(r,'rule_revisions') for r in store.query('SELECT * FROM rule_revisions ORDER BY id')]
        for project in projects:
            key=project['project_key']
            versions=[r['compatibility_key'] for r in store.query('SELECT DISTINCT compatibility_key FROM scan_lines WHERE active=1 AND project_key=? ORDER BY compatibility_key',(key,))] or [None]
            for revision in revisions:
                if revision['project_key'] not in {'',key}: continue
                first=store.query_one("SELECT MIN(observed_at) first FROM rule_availability_observations WHERE rule_revision_id=? AND project_key=? AND status='available'",(revision['id'],key))['first']
                anchor=min(end,first) if first else end
                start=so.normalize_timestamp((datetime.fromisoformat(anchor)-timedelta(days=30)).isoformat())
                for version in versions:
                    m=rule_recurrence(store,rule_revision_id=revision['id'],project_key=key,start=start,end=end,compatibility_key=version)
                    ids.append(record_project_measurement(store,run_id=run_id,measurement=m))
                    comparisons[m['comparison']]+=1
                    if m['reason']: causes[m['reason']]+=1
    return {'recorded':len(ids),'measurement_ids':ids,'comparisons':dict(sorted(comparisons.items())),
            'causes':dict(sorted(causes.items())),'observed_at':end,'coverage_complete':False}


def _summary(record):
    m=record['measurement']
    sides={}
    for side in ('before','after'):
        value=m[side]
        if value is None:
            sides[side]=None
            continue
        observed=value['rate_per_100k']
        reason=('missing_matching_evidence' if value['occurrences'] is None
                else 'zero_eligible_exposure' if not value['eligible_lines']
                else 'unclassified_signal_occurrences' if value['unclassified_occurrences']
                else 'insufficient_sessions' if value['sessions']<m['min_sessions'] else '')
        sides[side]={k:value[k] for k in ('eligible_lines','occurrences','sessions','session_size','workload')}
        sides[side].update(observed_rate_per_100k=observed,
                           rate_per_100k=None if reason else observed,reason=reason)
    return {k:record[k] for k in record if k!='measurement'} | {k:m[k] for k in
        ('learning_id','application_id','computable','comparison','reason','absolute_rate_change','relative_change_pct','coverage_complete','start','end','min_sessions')} | sides


def measurement_history(store, *, project_key=None, limit=20, cursor=None):
    if project_key is not None and (not isinstance(project_key,str) or not project_key): raise MeasurementRequestError('Invalid project selector')
    if type(limit) is not int or not 1<=limit<=100: raise MeasurementRequestError('Page limit must be 1..100')
    selector=so.content_id([PROFILE,project_key]);position=None
    if cursor is not None:
        try:
            position=json.loads(base64.urlsafe_b64decode(cursor).decode())
            if set(position)!={'selector','observed_at','id'} or position['selector']!=selector: raise ValueError()
            if so.normalize_timestamp(position['observed_at'])!=position['observed_at'] or str(uuid.UUID(position['id']))!=position['id']: raise ValueError()
        except (ValueError,TypeError,KeyError,UnicodeError,AttributeError) as exc: raise MeasurementRequestError('Invalid measurement cursor or changed project') from exc
    base={'records':[],'count':None,'next_cursor':None,'project_key':project_key,'reason':'schema_unavailable'}
    if not _schema(store,missing_ok=True): return base
    where='record_type=?';args=[PROFILE]
    if project_key is not None: where+=' AND project_key=?';args.append(project_key)
    count=store.query_one('SELECT COUNT(*) n FROM project_stats WHERE '+where,args)['n']
    if position:
        where+=' AND (observed_at<? OR (observed_at=? AND id<?))';args.extend([position['observed_at'],position['observed_at'],position['id']])
    rows=store.query('SELECT * FROM project_stats WHERE '+where+' ORDER BY observed_at DESC,id DESC LIMIT ?',(*args,limit+1))
    records=[_summary(_read(r)) for r in rows[:limit]]
    following=None
    if len(rows)>limit:
        last=records[-1];following=base64.urlsafe_b64encode(so.canonical_json({'selector':selector,'observed_at':last['observed_at'],'id':last['id']}).encode()).decode()
    return {**base,'records':records,'count':count,'next_cursor':following,'reason':'' if count else 'no_recorded_measurements'}


def measurement_detail(store, measurement_id):
    _schema(store)
    row=store.query_one('SELECT * FROM project_stats WHERE id=? AND record_type=?',(measurement_id,PROFILE))
    if row is None: raise MeasurementRequestError('No retained project measurement with that ID')
    return _read(row)


def project_benefit(store, *, project_key):
    result={'computable':False,'value':'—','reason':'No retained recurrence measurements for this project',
            'lower':0,'higher':0,'unchanged':0,'unavailable':0,'measurement_ids':[],
            'meaning':'Latest observed association per revision and detector; no pooling across versions or overlapping rules.'}
    if not _schema(store,missing_ok=True): return {**result,'reason':'Measurement schema unavailable'}
    rows=store.query('SELECT p.* FROM project_stats p WHERE p.record_type=? AND p.project_key=? AND NOT EXISTS ('
        'SELECT 1 FROM project_stats n WHERE n.record_type=p.record_type AND n.project_key=p.project_key '
        'AND n.rule_revision_id=p.rule_revision_id AND n.compatibility_key=p.compatibility_key '
        'AND (n.observed_at>p.observed_at OR (n.observed_at=p.observed_at AND n.id>p.id))) ORDER BY p.rule_revision_id,p.compatibility_key',(PROFILE,project_key))
    for row in rows:
        r=_read(row);result['measurement_ids'].append(r['id']);result[r['measurement']['comparison']]+=1
    total=result['lower']+result['higher']+result['unchanged']
    if total: result.update(computable=True,value=f"{result['lower']} of {total} lower",reason='Observed subsets with incomplete coverage; associations are not causal benefit.')
    elif rows: result['reason']='Recorded comparisons lack sufficient matched, qualified exposure; inspect the measurement causes.'
    return result
