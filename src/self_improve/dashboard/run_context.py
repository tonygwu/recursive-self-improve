"""Historical reviewed deliveries related by immutable learning identity only.

The caller owns the selected Store and its read transaction. No target reads,
current proposal joins, model calls or transaction control occur here.
"""
import base64
import json

from .. import commands, mining_history
from . import run_data
from .overview_data import _completion, _instant

PROFILE = 'run-related-reviewed/1'
COVERAGE = ('Only retained mining observations and frozen reviewed deliveries with the same learning ID are linked. '
            'Unlinked historical writes are not included. This association does not establish receipt, violation or benefit.')


def _members(store, command_id, observations):
    result=[]
    for member in store.query('SELECT * FROM command_members WHERE command_id=? ORDER BY proposal_id',(command_id,)):
        revision=commands.load_revision(store,member['revision_id'])
        snapshot=revision['snapshot'];learning_id=snapshot['learning'].get('id')
        if not isinstance(learning_id,str) or not learning_id or snapshot['proposal'].get('learning_id')!=learning_id:
            raise run_data.RunDataError('Reviewed member '+member['id']+': frozen learning identities disagree.')
        result.append({**member,**revision,'observation_ids':observations.get(learning_id,[])})
    return result


def _records(store, run, observations):
    started=_instant(run['started'],'run '+run['id'])
    records=[]
    # Validate authorization before using a frozen identity. Reading current proposals
    # here would lose old links when a producer amends a proposal or learning.
    ids=store.query("SELECT DISTINCT c.id FROM commands c JOIN command_targets t ON t.command_id=c.id "
                    "WHERE c.action='approve' AND t.state='completed' ORDER BY c.id")
    for row in ids:
        members=_members(store,row['id'],observations)
        matched_targets={m['target_id'] for m in members if m['observation_ids']}
        if not matched_targets:continue
        command=commands.command_status(store,row['id'])
        if command['actor']!='user':raise run_data.RunDataError('Reviewed delivery has no user authorization: '+row['id'])
        for target in command['targets']:
            if target['state']!='completed' or target['id'] not in matched_targets:continue
            owner='command target '+target['id']
            delivery,done,when=_completion(target['checkpoint'],target['destination'],owner)
            selected=[m for m in members if m['target_id']==target['id']]
            for member in selected:
                expected={'command_id':command['id'],'target_id':target['id'],'revision_id':member['revision_id'],
                          'mode':target['destination']['mode'],'branch':target['destination']['branch_name'],
                          'snapshot_before':delivery['snapshot_before'],'snapshot_after':done['snapshot_after']}
                candidates=[]
                for event in store.query("SELECT * FROM proposal_events WHERE proposal_id=? AND event='applied'",(member['proposal_id'],)):
                    try:note=json.loads(event['note'])
                    except (ValueError,TypeError):continue
                    if isinstance(note,dict) and note.get('command_id')==command['id']:candidates.append((event,note))
                if len(candidates)!=1 or any(candidates[0][1].get(k)!=v for k,v in expected.items()):
                    raise run_data.RunDataError(owner+': member application evidence is missing or ambiguous.')
            if when>=started:continue
            records.append({'id':target['id'],'command_id':command['id'],'command_state':command['state'],
                            'completed_at':when.isoformat().replace('+00:00','Z'),
                            'snapshot_before':delivery['snapshot_before'],'snapshot_after':done['snapshot_after'],
                            'target':target,'members':selected})
    return sorted(records,key=lambda r:(_instant(r['completed_at'],'completion'),r['command_id'],r['id']),reverse=True)


def related_deliveries(store, run_id, *, limit=20, cursor=None):
    """Return a revision-bound page; absent retention never becomes measured zero."""
    if type(limit) is not int or not 1<=limit<=100:raise run_data.RunDataError('Related delivery limit must be 1–100.')
    run=run_data._run(store,run_id)
    reason='';records=[];observations={}
    try:
        missing=[name for name in (mining_history.MIGRATION,commands.COMMAND_MIGRATION)
                 if not run_data._available(store,name)]
        if missing:reason='Related delivery coverage is unknown: missing '+', '.join(missing)+'.'
        elif not run['started']:reason='Related delivery coverage is unknown: run start time was not retained.'
        else:
            _instant(run['started'],'run '+run_id)
            for row in store.query('SELECT * FROM mining_history WHERE run_id=? ORDER BY id',(run_id,)):
                record=mining_history.read(row)
                observations.setdefault(record['learning_id'],[]).append(record['id'])
            if not observations:reason='No retained mining observations establish a relationship for this run. Earlier delivery coverage is unknown.'
            else:records=_records(store,run,observations)
    except (commands.CommandError,mining_history.HistoryError,ValueError,KeyError,TypeError) as exc:
        raise run_data.RunDataError(str(exc)) from exc
    revision=mining_history.digest({'profile':PROFILE,'run_id':run_id,'started':run['started'],
                                     'observations':observations,'records':records,'reason':reason})
    start=0
    if cursor is not None:
        try:
            decoded=json.loads(base64.b64decode(cursor,altchars=b'-_',validate=True))
            if (not isinstance(decoded,list) or len(decoded)!=4
                    or decoded[:3]!=[PROFILE,run_id,revision]):raise ValueError
            start=[r['id'] for r in records].index(decoded[3])+1
        except (ValueError,TypeError,UnicodeError) as exc:
            raise run_data.RunDataError('Invalid or stale related delivery cursor. Refresh earlier deliveries to start a new page.') from exc
    page=records[start:start+limit]
    next_cursor=None
    if start+limit<len(records):
        next_cursor=base64.urlsafe_b64encode(mining_history.encoded([PROFILE,run_id,revision,page[-1]['id']]).encode()).decode()
    return {'profile':PROFILE,'run_id':run_id,'kind':'related_deliveries','records':page,
            'count':None if reason else len(records),'reason':reason,'coverage':COVERAGE,
            'revision':revision,'next_cursor':next_cursor}
