"""Compact Project rule evidence, using the selected Store and caller transaction."""
from .. import instruction_inventory, rule_availability, rule_revisions
from . import project_data


def _lifetime(store, revision, applications):
    ending=rule_revisions.application_end(store,revision)
    if ending:
        return {'state':'rolled_back' if ending['cause']=='verified_rollback' else 'unknown',
                'cause':ending['cause'],'ending':ending,'successors':[]}
    origin=next((e for e in applications if e['id']==revision['application_event_id']),None)
    if origin is None:  # application_end normally diagnoses this first.
        return {'state':'unknown','cause':'application_history_missing','ending':None,'successors':[]}
    if origin['ts']!=revision['applied_at']:
        raise rule_revisions.AvailabilityError('application '+origin['id']+': timestamp differs from retained revision')
    successors=[e for e in applications if e['ts']>origin['ts']]
    ambiguous=[e for e in applications if e['ts']==origin['ts'] and e['id']!=origin['id']]
    return {'state':'superseded' if successors else 'unknown' if ambiguous else 'no_recorded_end',
            'cause':'later_application' if successors else 'simultaneous_applications' if ambiguous else '',
            'ending':None,'successors':successors or ambiguous}


def _counts(rows, *, available, copy_selected):
    observations={key:0 for key in ('available','absent','changed','unknown','unobserved')}
    lifetimes={key:0 for key in ('no_recorded_end','rolled_back','superseded','unknown')}
    known=0;uncertain=0
    for row in rows:
        state=row['lifetime']['state'];lifetimes[state]+=1
        status=row['observation']['status'] if row['observation'] else 'unobserved'
        observations[status]+=1
        if state=='no_recorded_end' and status=='available':known+=1
        if state=='unknown' or (state=='no_recorded_end' and status in ('unknown','unobserved')):uncertain+=1
    return {'retained':len(rows) if available else None,'known_matches':known,
            'matched':known if available and copy_selected and not uncertain else None,
            'uncertain':uncertain,'observations':observations,'lifetimes':lifetimes}


def summary(store, cfg, *, project_key, working_copy_id=None, limit=3):
    """Independent bounded previews; counts span complete retained populations.

    No selection means no copy inference. Only existing current-proposal
    attribution can inspect local Git; all delivery/check reads are retained-only.
    """
    if type(limit) is not int or not 1<=limit<=20:
        raise project_data.ProjectRequestError('Invalid project summary limit; use 1..20')
    if working_copy_id is not None and (not isinstance(working_copy_id,str) or len(working_copy_id)!=64
            or any(c not in '0123456789abcdef' for c in working_copy_id)):
        raise project_data.ProjectRequestError('Invalid project summary working copy')
    project_data._project(store,project_key)
    available=bool(store.query_one('SELECT name FROM schema_migrations WHERE name=?',(rule_revisions.MIGRATION,)))
    revisions={};checks={};rows=[]
    if available:
        rule_revisions.require_schema(store)
        revisions={r['id']:r for r in (rule_revisions.read_record(row,'rule_revisions') for row in
            store.query('SELECT * FROM rule_revisions WHERE project_key=?',(project_key,)))}
        cursor=None
        while True:
            page=rule_availability.project_availability(store,project_key=project_key,limit=100,cursor=cursor)
            for check in page['records']:
                if check['working_copy_id']!=working_copy_id:continue
                revision=check['revision']
                if revision['project_key'] not in ('',project_key):
                    raise rule_revisions.AvailabilityError('Project check has a foreign delivered revision')
                revisions[revision['id']]=revision;checks[revision['id']]=check
            cursor=page['next_cursor']
            if cursor is None:break
        by_proposal={}
        for revision in revisions.values():
            pid=revision['proposal_id']
            if pid not in by_proposal:
                by_proposal[pid]=[{**event,'ts':rule_revisions.timestamp(event['ts'],'application '+event['id'])}
                    for event in store.query("SELECT id,ts FROM proposal_events WHERE proposal_id=? AND event='applied'",(pid,))]
            rows.append({'revision':revision,'origin':'project' if revision['project_key'] else 'global',
                         'lifetime':_lifetime(store,revision,by_proposal[pid]),'observation':checks.get(revision['id'])})
    # A syntactically valid ID alone is not a known project copy. Checks bind
    # their copy identity; an empty population needs an independent inventory.
    copy_known=bool(checks)
    if available and working_copy_id and not copy_known:
        inventory=instruction_inventory.project_inventory(store,project_key=project_key,
            working_copy_id=working_copy_id,limit=1)
        copy_known=bool(inventory['records'])
    counts={origin:_counts([r for r in rows if r['origin']==origin],available=available,copy_selected=copy_known)
            for origin in ('project','global')}
    rows.sort(key=lambda r:(r['revision']['applied_at'],r['revision']['id']),reverse=True)
    proposals,unresolved=project_data._proposed(store,cfg,project_key)
    proposals.sort(key=lambda r:r['id'])
    observed_times=sorted({r['observation']['observed_at'] for r in rows if r['observation']})
    return {'version':1,'project_key':project_key,'working_copy_id':working_copy_id,'copy_known':copy_known,'runtime_loading_verified':False,
            'counts':counts,'observation_times':observed_times,
            'deliveries':{'records':rows[:limit],'count':len(rows) if available else None,'omitted':max(0,len(rows)-limit),
                          'reason':'' if available else 'schema_unavailable'},
            'proposals':{'records':proposals[:limit],'count':len(proposals),'omitted':max(0,len(proposals)-limit),
                         'unresolved_count':len(unresolved)},
            'meaning':'Matched revisions were observed in this exact copy at their last checks and have no known end or successor. Checks can have different times and conditional/provider scope. Between-check continuity, current presence and session receipt are unknown. Globals are separate. Counts describe retained history only.'}
