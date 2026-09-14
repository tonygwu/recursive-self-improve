"""Exact-run reads. Stored relationships are evidence; timestamps are not joins."""
from datetime import date
import json

from . import queries, scan_data
from .. import scan_reporting


class RunDataError(ValueError):
    """A selected run or pagination request cannot be read faithfully."""


class RunNotFound(RunDataError):
    pass


def object_json(raw, owner):
    try:
        value=json.loads(raw)
        json.dumps(value,allow_nan=False)
    except (ValueError,TypeError) as exc:
        raise RunDataError(owner+': invalid JSON') from exc
    if not isinstance(value,dict):raise RunDataError(owner+': expected a JSON object')
    return value


def _run(store, run_id):
    row=store.query_one('SELECT * FROM runs WHERE id=?',(run_id,))
    if row is None:raise RunNotFound('No run with ID '+run_id)
    return row


def list_runs(store, day):
    try:
        if date.fromisoformat(day).isoformat()!=day:raise ValueError()
    except (ValueError,TypeError) as exc:raise RunDataError('Run day must be YYYY-MM-DD in UTC.') from exc
    rows=store.query('SELECT id,started,finished,status FROM runs WHERE substr(started,1,10)=? ORDER BY started,id',(day,))
    return {'day':day,'timezone':'UTC','records':rows,'count':len(rows)}


def _stats(row):
    stats=object_json(row['stats_json'],'run '+row['id'])
    if 'run_id' in stats and stats['run_id']!=row['id']:
        raise RunDataError('run '+row['id']+': stats identify a different run')
    for name in (*queries.STAGES, 'llm'):
        if name in stats and not isinstance(stats[name], dict):
            raise RunDataError(f"run {row['id']}: {name} is not an object")
    return stats


JOB_SCHEMAS = {'model_jobs': '0016_model_jobs', 'incident_jobs': '0019_incident_jobs',
               'recovery_jobs': '0020_recovery_jobs'}


def _available(store, migration):
    return bool(store.query_one('SELECT name FROM schema_migrations WHERE name=?', (migration,)))


def _commands(store, run_id):
    # These are actual job execution links, not the run that created the source proposal.
    ids=set()
    for table, migration in JOB_SCHEMAS.items():
        if _available(store, migration):
            ids.update(r['command_id'] for r in store.query(f'SELECT command_id FROM {table} WHERE run_id=?',(run_id,)))
    return sorted(ids)


def detail(store, run_id):
    row=_run(store,run_id);stats=_stats(row)
    stages=[]
    for name in queries.STAGES:
        payload=stats.get(name)
        if name in stats and not isinstance(payload,dict):
            raise RunDataError(f'run {run_id}: {name} is not an object')
        stages.append({'name':name,'recorded':name in stats,'payload':payload})
    calls=store.query_one('SELECT COUNT(*) AS n,COALESCE(SUM(tokens_in),0) AS tokens_in,COALESCE(SUM(tokens_out),0) AS tokens_out FROM llm_calls WHERE run_id=?',(run_id,))
    llm=stats.get('llm')
    if llm is not None and not isinstance(llm,dict):raise RunDataError('run '+run_id+': llm is not an object')
    reported=(llm or {}).get('attempted')
    if reported is not None and (type(reported) is not int or reported<0):
        raise RunDataError('run '+run_id+': llm attempted is not a nonnegative integer')
    reconciled=None if reported is None else reported==calls['n']
    limits=stats.get('budget_limits')
    if limits is not None and (not isinstance(limits,dict) or any(type(v) is not int or v<0 for v in limits.values())):
        raise RunDataError('run '+run_id+': invalid recorded budget limits')
    return {'run':{k:v for k,v in row.items() if k!='stats_json'},'stats':stats,'stages':stages,
        'night_runs':list_runs(store,row['started'][:10])['records'],'budget_limits':limits,
        'calls':{'recorded':calls['n'],'reported':reported,'reconciled':reconciled,
            'tokens_in':calls['tokens_in'],'tokens_out':calls['tokens_out'],
            'reason':'No call total was recorded in run statistics.' if reconciled is None else
                     'Recorded call rows differ from the run total; retained history may be incomplete.' if not reconciled else ''},
        'command_ids':_commands(store,run_id),
        'missing_job_schemas':[migration for migration in JOB_SCHEMAS.values() if not _available(store,migration)],
        'scan_summary':scan_reporting.summary(stats.get('scan'), owner='run '+run_id+'.scan'),
        'provenance_note':'Calls and job results use explicit run links. Current proposal references are labeled separately from historical execution.'}


KINDS=('calls','evaluations','proposals','deliveries','scans')


def _records(store, run_id, kind, stats):
    if kind=='calls':
        return store.query('SELECT * FROM llm_calls WHERE run_id=? ORDER BY created_at,id',(run_id,)),''
    if kind=='proposals':
        rows=store.query('SELECT * FROM proposals WHERE run_id=? ORDER BY created_at,id',(run_id,))
        for row in rows:row['association']='Created by this run; content, status, eval reference, and snapshots are current and may reflect later work.'
        return rows,'These are current proposal references, not an immutable record of the run\'s applied edits.'
    if kind=='deliveries':
        from ..operations import operation_status
        ids=stats.get('apply',{}).get('operation_ids')
        if ids is None:return [],'This run did not record exact delivery operation IDs. Proposal references cannot prove which edits this execution wrote.'
        if not isinstance(ids,list) or any(not isinstance(x,str) for x in ids) or len(ids)!=len(set(ids)):
            raise RunDataError('run '+run_id+': invalid delivery operation IDs')
        if ids and not _available(store, '0012_instruction_operations'):
            raise RunDataError('run '+run_id+': delivery links exist without the operation schema')
        result=[]
        for oid in ids:
            operation=operation_status(store,oid)
            source=operation['record'].get('proposal',{})
            if source.get('run_id')!=run_id:raise RunDataError('run '+run_id+': delivery '+oid+' has a different source run')
            result.append(operation)
        applied = stats['apply'].get('applied')
        reason = '' if type(applied) is int and applied == len(result) else 'Linked delivery count does not reconcile with the recorded applied total.'
        if any(r['state'] != 'completed' or r['kind'] != 'auto_apply' for r in result):
            raise RunDataError('run '+run_id+': applied delivery is not a completed automatic operation')
        return result,reason
    refs={}
    history = store.query('SELECT * FROM proposal_eval_history WHERE run_id=? ORDER BY created_at,id',(run_id,)) if _available(store, '0010_dashboard_commands') else []
    for row in history:
        if row['eval_result_id']:
            refs.setdefault(row['eval_result_id'],[]).append({'kind':'proposal_eval_history','id':row['id'],
                'proposal_id':row['proposal_id'],'source_revision_id':row['source_revision_id'],'run_id':run_id})
    for cid in _commands(store,run_id):
        for row in store.query('SELECT * FROM job_evaluations WHERE command_id=? ORDER BY scenario_index,id',(cid,)):
            refs.setdefault(row['eval_result_id'],[]).append({'kind':'job_evaluations','id':row['id'],
                'command_id':cid,'scenario_index':row['scenario_index'],'run_id':run_id})
    results=[]
    for eid,links in refs.items():
        row=store.query_one('SELECT * FROM eval_results WHERE id=?',(eid,))
        if row is None:raise RunDataError('run '+run_id+': linked evaluation '+eid+' is missing')
        row['metrics']=object_json(row.pop('metrics_json'),'eval_results.'+eid+'.metrics')
        row['error_taxonomy']=object_json(row.pop('error_taxonomy_json'),'eval_results.'+eid+'.error_taxonomy')
        row['links']=links;results.append(row)
    results.sort(key=lambda r:(r['started'],r['id']))
    return results,'Only evaluations with retained run or command links appear here. Historical scenario totals remain in the stage record; unlinked evals are not assigned by time.'


def records(store, run_id, *, kind, limit=50, cursor=None):
    if kind not in KINDS:raise RunDataError('Unknown run record kind: '+str(kind))
    if type(limit) is not int or not 1<=limit<=100:raise RunDataError('Record limit must be 1 through 100.')
    stats=_stats(_run(store,run_id))
    if kind == 'scans':
        page = scan_data.history_page(store, run_id=run_id, limit=limit, cursor=cursor)
        return {**page, 'run_id': run_id, 'kind': kind, 'reason_code': page['reason'], 'reason': page['reason_text']}
    rows,reason=_records(store,run_id,kind,stats)
    offset=0
    if cursor is not None:
        try:
            parsed=json.loads(cursor)
            if not isinstance(parsed,list) or len(parsed)!=3 or parsed[:2]!=[run_id,kind]:raise ValueError()
            offset=next(i+1 for i,r in enumerate(rows) if r['id']==parsed[2])
        except (ValueError,TypeError,StopIteration) as exc:raise RunDataError('Invalid cursor for this run and record kind.') from exc
    page=rows[offset:offset+limit]
    return {'run_id':run_id,'kind':kind,'records':page,'count':len(rows),'reason':reason,
        'next_cursor':json.dumps([run_id,kind,page[-1]['id']],separators=(',',':')) if offset+limit<len(rows) else None}
