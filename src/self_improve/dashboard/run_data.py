"""Exact-run reads. Stored relationships are evidence; timestamps are not joins."""
from datetime import date
import json

from . import queries, scan_data
from .. import scan_reporting, availability_reporting
from ..failure_presentation import failure_copy, taxonomy_rows


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
        try:
            causes = taxonomy_rows(payload['taxonomy'], owner=f'run {run_id}.{name}.taxonomy') if payload and 'taxonomy' in payload else None
        except ValueError as exc:
            raise RunDataError(str(exc)) from exc
        try:
            accounting = queries._stage_cell(row,name,stats)
        except queries.DashboardDataError as exc:
            raise RunDataError(str(exc)) from exc
        stages.append({'name':name,'recorded':name in stats,'payload':payload, 'causes':causes,
                       'accounting':accounting, 'state':accounting['state']})
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
        'command_ids':_commands(store,run_id), 'flow':flow(store,run_id),
        'inspection':inspection(store, run_id, stats, limits),
        'missing_job_schemas':[migration for migration in JOB_SCHEMAS.values() if not _available(store,migration)],
        'scan_summary':scan_reporting.summary(stats.get('scan'), owner='run '+run_id+'.scan'),
        'availability_summary': availability_reporting.summary(
            stats.get('availability'), owner='run '+run_id+'.availability'),
        'provenance_note':'Calls and job results use explicit run links. Current proposal references are labeled separately from historical execution.'}


KINDS=('calls','attempts','evaluations','proposals','deliveries','scans','learnings')


def _flow_sources(store, run_id):
    from .. import mining_history
    recorded = _available(store, mining_history.MIGRATION)
    # Group only explicit run identities. Scan time and current proposal evals
    # never establish which execution mined a rule.
    history = []
    if recorded:
        for row in store.query('SELECT * FROM mining_history WHERE run_id=? ORDER BY created_at,id', (run_id,)):
            if row['kind'] not in mining_history.KINDS:
                raise RunDataError(f"run {run_id}: unknown mining kind {row['kind']!r} in mining_history.{row['id']}")
            try: history.append(mining_history.read(row))
            except mining_history.HistoryError as exc: raise RunDataError(str(exc)) from exc
    proposals = store.query('SELECT learning_id,COUNT(*) n FROM proposals WHERE run_id=? GROUP BY learning_id', (run_id,))
    return recorded, history, proposals


def flow(store, run_id):
    recorded, history, proposals = _flow_sources(store,run_id)
    observed = {r['learning_id'] for r in history}
    proposed = {r['learning_id'] for r in proposals}
    kinds = {}
    for row in history: kinds[row['kind']] = kinds.get(row['kind'],0)+1
    return {'mining_recorded':recorded,
        'observation_count':len(history) if recorded else None,
        'observed_learning_count':len(observed) if recorded else None,
        'observation_kinds':kinds if recorded else None,
        'created_proposal_count':sum(r['n'] for r in proposals),
        'proposal_learning_count':len(proposed),
        'observed_without_proposal_count':len(observed-proposed) if recorded else None,
        'multiple_proposal_learning_count':sum(r['n']>1 for r in proposals),
        'rule_group_count':len(observed|proposed),
        'reason':'' if recorded else 'This database has no mining-history schema; historical mining observations are unknown.',
        'note':'Mining observations and created proposals are separate cohorts. Older candidate rules can be proposed in this run. No proposal here does not establish that a rule was never routed. Retained links may not cover older historical work.'}


def _learning_groups(store, run_id):
    recorded, history, proposals = _flow_sources(store,run_id)
    groups = {lid:{'id':lid,'mining_recorded':recorded,'observations':[],'proposals':[]}
              for lid in sorted({r['learning_id'] for r in history+proposals})}
    for observation in history:
        groups[observation['learning_id']]['observations'].append(observation)
    for row in store.query('SELECT * FROM proposals WHERE run_id=? ORDER BY created_at,id',(run_id,)):
        groups[row['learning_id']]['proposals'].append(row)
    # One selected-cohort join avoids a query for each rule without reading all
    # unrelated learning rows. Deleted current content stays absent.
    clause = 'id IN (SELECT learning_id FROM proposals WHERE run_id=?)'
    args = [run_id]
    if recorded:
        clause += ' OR id IN (SELECT learning_id FROM mining_history WHERE run_id=?)'
        args.append(run_id)
    current = {r['id']:r for r in store.query('SELECT * FROM learnings WHERE '+clause, args)}
    for lid, group in groups.items():
        group['learning'] = current.get(lid)
        group['association'] = 'Observations are immutable records of this run. Rule content and proposal content/status below are current and may reflect later work.'
    return list(groups.values())


def _records(store, run_id, kind, stats):
    if kind=='learnings':
        return _learning_groups(store,run_id),'Groups use exact mining-history and proposal run links. Empty retained links do not prove zero historical work.'
    if kind=='calls':
        rows=store.query('SELECT * FROM llm_calls WHERE run_id=? ORDER BY created_at,id',(run_id,))
        for row in rows:
            row['outcome_description']=failure_copy(row['outcome'])
            if row['outcome_description'] is None and row['outcome'] not in queries.LLM_SUCCESS_OUTCOMES:
                row['outcome_description']={'name':'Unclassified outcome',
                    'explanation':'No explanation is registered for this recorded call outcome.'}
        return rows,''
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
    from .. import eval_history
    for link in eval_history.result_links(store,run_id=run_id):
        refs.setdefault(link['eval_result_id'],[]).append(link)
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
    if kind == 'attempts':
        from . import eval_data
        page=eval_data.attempts(store,run_id=run_id,limit=limit,cursor=cursor)
        return {**page,'run_id':run_id,'kind':kind}
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


def inspection(store, run_id, stats, limits):
    """Recorded limits are independent from durable job reservations."""
    owner='run '+run_id
    llm=stats.get('llm',{})
    maps={}
    for key in ('calls_made','refused'):
        value=llm.get(key,{})
        if not isinstance(value,dict) or any(type(v) is not int or v<0 for v in value.values()):
            raise RunDataError(owner+': invalid llm.'+key)
        maps[key]=value
    waits=llm.get('policy_waits')
    if waits is not None and (type(waits) is not int or waits<0):raise RunDataError(owner+': invalid policy waits')
    pools=sorted(set(limits or {})|set(maps['calls_made'])|set(maps['refused']))
    pipeline=[{'pool':p,'limit':(limits or {}).get(p),'used':maps['calls_made'].get(p),'refused':maps['refused'].get(p)} for p in pools]
    clock=stats.get('wall_clock')
    if clock is not None:
        if not isinstance(clock,dict):raise RunDataError(owner+': invalid wall clock')
        for name in ('wall_seconds','model_seconds','unaccounted_seconds','unaccounted_pct','largest_gap_seconds'):
            if name in clock and (type(clock[name]) not in (int,float)):
                raise RunDataError(owner+': invalid wall clock '+name)
    from .. import jobs
    summaries=[]
    for cid in _commands(store,run_id):
        row=store.query_one('SELECT * FROM commands WHERE id=?',(cid,))
        if row is None:raise RunDataError(owner+': missing job command '+cid)
        data=jobs.status(store,row)
        if data['run_id']!=run_id:raise RunDataError(owner+': mismatched job '+cid)
        summaries.append({k:data[k] for k in ('id','action','state','budget','failure_taxonomy')}|{
            'completed_calls':sum(c['state']=='completed' for c in data['calls']),
            'unresolved_calls':sum(c['state']=='started' for c in data['calls'])})
    return {'pipeline_pools':pipeline,'policy_waits':waits,'jobs':summaries,'wall_clock':clock}
