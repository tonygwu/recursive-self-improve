"""Bounded Overview summaries from the selected Store; no new producer or writer."""
from datetime import date, datetime, timedelta, timezone
import json

from ..execution_policy import policy_snapshot, waiting_proposals
from . import queries, run_data


class OverviewDataError(queries.DashboardDataError, ValueError):
    pass


def _stats(row):
    # Overview corruption keeps its established server-data error contract.
    try:
        return run_data._stats(row)
    except run_data.RunDataError as exc:
        raise OverviewDataError(str(exc)) from exc


def _instant(value, owner):
    try:
        parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError) as exc:
        raise OverviewDataError(owner+': invalid or undated timestamp') from exc


def _mining(payload, owner):
    if not isinstance(payload,dict):
        raise OverviewDataError(owner+': mining stage is not an object')
    keys=('attempted','succeeded','failed')
    if any(type(payload[k]) is not int or payload[k]<0 for k in keys if k in payload):
        raise OverviewDataError(owner+': invalid mining counters')
    if not set(keys)<=payload.keys():
        return None
    if payload['attempted'] != payload['succeeded']+payload['failed']:
        raise OverviewDataError(owner+': mining outcomes do not reconcile')
    return {k:payload[k] for k in keys}


def mining_activity(store, *, now_utc):
    """Recorded successes per full UTC day, not a promise of future capacity."""
    end=date.fromisoformat(queries._utc_day(now_utc));start=end-timedelta(days=7)
    records=[];missing=[];unfinished=[];days=set();total=dict.fromkeys(('attempted','succeeded','failed'),0)
    for row in store.query('SELECT id,started,finished,status,stats_json FROM runs ORDER BY started,id'):
        day=_instant(row['started'],'run '+row['id']).date()
        if not start<=day<end:
            continue
        records.append(row['id']);days.add(day)
        if not row['finished']:
            unfinished.append(row['id'])
        stats=_stats(row)
        counters=_mining(stats['mine'],'run '+row['id']) if 'mine' in stats else None
        if counters is None:
            missing.append(row['id']);continue
        for key,value in counters.items():
            total[key]+=value
    recorded=len(records)-len(missing)
    return {'window_start':start.isoformat(),'window_end_exclusive':end.isoformat(),'window_days':7,
            'run_ids':records,'runs':len(records),'recorded_runs':recorded,'missing_stage_run_ids':missing,
            'unfinished_run_ids':unfinished,'days_with_runs':len(days),'days_without_runs':7-len(days),
            **total,'per_day':total['succeeded']/7 if recorded else None,
            'per_recorded_run':total['succeeded']/recorded if recorded else None,
            'coverage_complete':False,
            'meaning':'Successful incident-mining outcomes recorded in this execution window. Missing stages and days without records are coverage gaps. Retention and schedule coverage are not established; this is not future capacity.'}


def review_preview(store, cfg):
    """Same membership and lesson identity as Review, without building write previews."""
    waiting=waiting_proposals(store,cfg);groups={}
    for row in waiting:
        group=groups.setdefault(row['learning_id'],{'learning_id':row['learning_id'],'size':0,'proposal_id':row['id']})
        group['size']+=1
    ordered=sorted(groups.values(),key=lambda row:(-row['size'],row['learning_id']))
    shown=ordered[:3]
    for row in shown:
        learning=store.query_one('SELECT title FROM learnings WHERE id=?',(row['learning_id'],))
        if learning is None:
            raise OverviewDataError('proposal '+row['proposal_id']+': missing learning '+row['learning_id'])
        title=learning['title']
        row['title']=title[:160];row['title_cut']=len(title)>160
    box=queries.inbox(store,cfg)
    return {'auto_apply_pending':box['auto_apply_pending'],'unknown_statuses':box['unknown_statuses'],'count':len(waiting),'family_count':len(groups),'families':shown,'omitted_families':max(0,len(groups)-len(shown)),
            'meaning':'Grouped by the same learning ID as Review. Display grouping grants no approval; open Review for complete selected members.'}


def latest_run(store):
    row=store.query_one('SELECT * FROM runs ORDER BY started DESC,id DESC LIMIT 1')
    if row is None:
        return None
    stats=_stats(row)
    if 'mine' in stats:
        _mining(stats['mine'],'run '+row['id'])
    stages=[]
    for name in queries.STAGES:
        payload=stats.get(name)
        numeric=[] if payload is None else [(k,v) for k,v in payload.items() if type(v) in (int,float)]
        summary=None if payload is None else dict(numeric[:4])
        cell=queries._stage_cell(row,name,stats)
        stages.append({'name':name,'recorded':name in stats,'payload':summary,
                       'omitted_numeric_fields':max(0,len(numeric)-4),
                       'cell':{'state':cell['state']}})
    return {'run':{k:v for k,v in row.items() if k!='stats_json'},'stages':stages,
            'review_only':stats.get('review_only'),'dry_run':stats.get('dry_run'),
            'meaning':'Latest retained run by start time and ID. A same-time sibling is separate; each stage links to this exact ID.'}


def _completion(checkpoint, destination, owner):
    delivery=checkpoint.get('delivery');done=checkpoint.get('result')
    if (not isinstance(delivery,dict) or not isinstance(done,dict)
            or not isinstance(done.get('snapshot_after'),str) or not done['snapshot_after']
            or done.get('mode')!=destination['mode']
            or done.get('branch_commit')!=delivery['branch_commit']):
        raise OverviewDataError(owner+': completion does not match its delivery checkpoint')
    return delivery,done,_instant(done.get('completed_at'),owner)


def delivery_activity(store):
    """Count recorded actions, not current rules or distinct contributions.

    Reads only retained records. A combined target can contain several edits;
    an inverse operation can reverse just one of them. Never subtract the counts.
    """
    from .. import commands, operations
    manual_available=bool(store.query_one('SELECT name FROM schema_migrations WHERE name=?',(commands.COMMAND_MIGRATION,)))
    automatic_available=operations.operations_available(store)
    result={'manual_targets':0 if manual_available else None,
            'automatic_operations':0 if automatic_available else None,
            'rollback_operations':0 if automatic_available else None,
            'legacy_application_events':0,'recent':[]}
    events=[];by_member={};by_operation={}
    for row in store.query("SELECT * FROM proposal_events WHERE event='applied'"):
        try: note=json.loads(row['note'])
        except (ValueError,TypeError): note=None
        note=note if isinstance(note,dict) else {}
        events.append((row,note))
        if isinstance(note.get('command_id'),str):
            by_member.setdefault((note['command_id'],row['proposal_id']),[]).append((row,note))
        if isinstance(note.get('operation_id'),str):
            by_operation.setdefault(note['operation_id'],[]).append((row,note))
    linked=set();recent=[]
    if manual_available:
        ids=store.query("SELECT DISTINCT c.id FROM commands c JOIN command_targets t ON t.command_id=c.id "
                        "WHERE c.action='approve' AND t.state='completed'")
        for row in ids:
            command=commands.command_status(store,row['id'])
            if command['actor']!='user':
                raise OverviewDataError('command '+row['id']+': reviewed delivery has no user authorization')
            members=store.query('SELECT * FROM command_members WHERE command_id=? ORDER BY proposal_id',(row['id'],))
            for target in command['targets']:
                if target['state']!='completed':continue
                owner='command target '+target['id'];checkpoint=target['checkpoint']
                delivery,done,when=_completion(checkpoint,target['destination'],owner)
                selected=[m for m in members if m['target_id']==target['id']]
                for member in selected:
                    expected={'command_id':command['id'],'target_id':target['id'],'revision_id':member['revision_id'],
                              'mode':target['destination']['mode'],'branch':target['destination']['branch_name'],
                              'snapshot_before':delivery['snapshot_before'],'snapshot_after':done['snapshot_after']}
                    candidates=by_member.get((command['id'],member['proposal_id']),[])
                    matches=[e for e,note in candidates if all(note.get(k)==v for k,v in expected.items())]
                    if len(matches)!=1 or len(candidates)!=1:
                        raise OverviewDataError(owner+': member application evidence is missing or ambiguous')
                    linked.add(matches[0]['id'])
                frozen=commands.load_revision(store,selected[0]['revision_id'])['snapshot']
                title=frozen['learning']['title']
                recent.append({'command_id':command['id'],'target_id':target['id'],
                               'completed_at':when.isoformat().replace('+00:00','Z'),
                               'title':title[:140],'title_cut':len(title)>140,'member_count':len(selected),
                               'destination':target['destination'],'snapshot_before':delivery['snapshot_before'],
                               'snapshot_after':done['snapshot_after']})
                result['manual_targets']+=1
    if automatic_available:
        for row in store.query("SELECT id FROM instruction_operations WHERE state='completed'"):
            operation=operations.operation_status(store,row['id'])
            destination=operation['record']['destination']
            delivery,done,_=_completion(operation['checkpoint'],destination,'operation '+row['id'])
            field='automatic_operations' if operation['kind']=='auto_apply' else 'rollback_operations'
            result[field]+=1
            if operation['kind']=='auto_apply':
                expected={'operation_id':row['id'],'mode':destination['mode'],'branch':destination['branch_name'],
                          'snapshot_before':delivery['snapshot_before'],'snapshot_after':done['snapshot_after']}
                candidates=by_operation.get(row['id'],[])
                matches=[e for e,note in candidates if e['proposal_id']==operation['proposal_id']
                         and all(note.get(k)==v for k,v in expected.items())]
                if len(matches)!=1 or len(candidates)!=1:
                    raise OverviewDataError('operation '+row['id']+': application evidence is missing or ambiguous')
                linked.add(matches[0]['id'])
    result['legacy_application_events']=sum(e['id'] not in linked for e,_ in events)
    result['recent']=sorted(recent,key=lambda r:(_instant(r['completed_at'],'reviewed delivery'),r['command_id'],r['target_id']),reverse=True)[:2]
    return result


def loop_totals(store):
    """Independent retained populations; neither a run funnel nor success rates."""
    from . import evidence_identity, eval_data
    rows=store.query('SELECT source,session_id,file_path FROM sessions')
    known=[r for r in rows if r['source'] in evidence_identity.PRODUCTS and r['session_id']]
    sessions={evidence_identity.session_key(r['source'],r['session_id'],r['file_path']) for r in known}
    health=eval_data.health(store)
    return {'sessions':{'known':len(sessions),'unknown_transcripts':len(rows)-len(known),'indexed_transcripts':len(rows)},
            'incidents':store.query_one('SELECT COUNT(*) n FROM incidents')['n'],
            'learnings':store.query_one('SELECT COUNT(*) n FROM learnings')['n'],
            'evaluations':{'attempts':health['attempts'],
                           'passed':sum(r['count'] for r in health['by_outcome'] if r['code']=='rule_helped') if health['computable'] else None,
                           'unlinked_results':health['unlinked_results']},
            'delivery':delivery_activity(store)}


def snapshot(store, cfg, *, now_utc):
    return {'version':1,'policy':policy_snapshot(store),'latest_run':latest_run(store),
            'review':review_preview(store,cfg),'mining':mining_activity(store,now_utc=now_utc),
            'loop':loop_totals(store)}
