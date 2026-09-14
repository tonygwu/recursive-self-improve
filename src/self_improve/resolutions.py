"""Rollback-resolution proposals and retained origin. Instruction writes stay in apply."""
from .commands import CommandError, _hash, _json, _parsed
from .propose import make_unified_diff
from .store import new_id, utc_now_iso
import json

MIGRATION = '0017_rollback_resolutions'
ACTION = 'resolve_rollback'


def validate_selection(members):
    """One physical inverse has one chosen resolution, even for identical patches."""
    seen = set()
    for member in members:
        resolution = member['snapshot'].get('resolution')
        if resolution is None:
            continue
        source = resolution['rollback']['source']
        key = (source['application_id'], source['contribution_hash'])
        if key in seen:
            raise CommandError('ConflictingResolutions',
                'Select one resolution for this applied contribution. These proposals are alternatives.', 409)
        seen.add(key)


def completed(store,source):
    """Read a resolution receipt for this exact applied contribution."""
    pid=source['proposal']['id']
    for event in store.query("SELECT * FROM proposal_events WHERE proposal_id=? AND event='rolled_back' ORDER BY ts DESC,id DESC",(pid,)):
        if '"resolution_proposal_id"' not in event['note']:
            continue  # Historical undo events need not use this receipt format.
        try:note=json.loads(event['note'])
        except (TypeError,ValueError) as exc:
            raise CommandError('ResolutionDataError',f"Rollback event {event['id']} has unreadable metadata.",500) from exc
        if not isinstance(note,dict):
            raise CommandError('ResolutionDataError',f"Rollback event {event['id']} has invalid metadata.",500)
        if not note.get('resolution_proposal_id') or note.get('application_id')!=source['application_id'] or note.get('contribution_hash')!=source['contribution_hash']:
            continue
        from .commands import command_status,load_revision
        command=command_status(store,note['command_id'])
        target=next((t for t in command['targets'] if t['id']==note['target_id']),None)
        member=next((m for m in command['members'] if m['proposal_id']==note['resolution_proposal_id'] and m['target_id']==note['target_id']),None)
        linked=store.query_one('SELECT revision_id FROM command_members WHERE command_id=? AND target_id=? AND proposal_id=?',(note['command_id'],note['target_id'],note['resolution_proposal_id']))
        frozen=load_revision(store,linked['revision_id'])['snapshot'].get('resolution',{}).get('rollback',{}).get('source',{}) if linked else {}
        if (not target or target['state']!='completed' or not member
                or frozen.get('application_id')!=source['application_id'] or frozen.get('contribution_hash')!=source['contribution_hash']
                or target['checkpoint']['result']['snapshot_after']!=note['snapshot_rollback']):
            raise CommandError('ResolutionDataError','A recorded resolution has no matching completed delivery.',500)
        return {'proposal_id':pid,'outcome':'rolled_back','reason':'','detail':'Resolved through a reviewed conflict proposal.',
            'application_id':source['application_id'],'resolution_proposal_id':note['resolution_proposal_id'],
            'command_id':note['command_id'],'target_path':source['destination']['target_path'],
            'mode':note['mode'],'snapshot_commit_rollback':note['snapshot_rollback'],'branch_commit':note['branch_commit'],
            'affected_proposal_ids':[m['proposal_id'] for m in source['affected_members']]}
    return None


def ensure_pending(store,record):
    from .rollback import latest_application,committed_or_prepared_rollback
    source=record['rollback']['source']
    for member in source['affected_members']:
        latest=latest_application(store,member['proposal_id'])
        if not latest or latest['id']!=member['applied_event_id']:
            raise CommandError('ApplicationChanged','The original proposal has a newer application. Inspect that application before resolving its rollback.',409)
    if completed(store,source):
        raise CommandError('AlreadyRolledBack','This contribution already has a completed resolution.',409)
    existing=committed_or_prepared_rollback(store,source)
    if existing:
        raise CommandError('AlreadyRolledBack' if existing['state']=='completed' else 'ReconciliationRequired',
            'A completed or prepared rollback already owns this contribution.',409)


def acknowledge(store,record,command_id,target,checkpoint):
    """Append inverse receipts in the same transaction as reviewed delivery."""
    from .rollback import latest_application
    source=record['rollback']['source'];destination=source['destination'];result=checkpoint['result']
    for member in source['affected_members']:
        latest=latest_application(store,member['proposal_id'])
        current=store.query_one('SELECT status FROM proposals WHERE id=?',(member['proposal_id'],))
        if latest and latest['id']==member['applied_event_id'] and current['status']=='applied':
            store.update('proposals','id',member['proposal_id'],{'status':'rolled_back'})
        note={'resolution_proposal_id':record['proposal_id'],'command_id':command_id,'target_id':target['id'],
            'application_id':source['application_id'],'contribution_hash':source['contribution_hash'],
            'applied_event_id':member['applied_event_id'],'snapshot_rollback':result['snapshot_after'],
            'mode':destination['mode'],'branch':destination['branch_name'],'branch_commit':result['branch_commit']}
        store.insert('proposal_events',{'id':new_id(),'proposal_id':member['proposal_id'],'event':'rolled_back',
            'actor':'user','ts':result['completed_at'],'note':_json(note)})


def require_schema(store):
    if not store.query_one('SELECT name FROM schema_migrations WHERE name=?',(MIGRATION,)):
        raise CommandError('UpgradeRequired','Upgrade the database before requesting rollback resolutions.',503)


def origin(store, proposal):
    if proposal['action'] != ACTION:
        return None
    require_schema(store)
    row=store.query_one('SELECT * FROM proposal_resolutions WHERE proposal_id=?',(proposal['id'],))
    if row is None:
        raise CommandError('ResolutionDataError','This resolution has no retained origin.',500)
    record=_parsed({'id':proposal['id'],**row},'record_json')
    job=store.query_one('SELECT c.action,j.plan_json,j.plan_hash FROM commands c JOIN model_jobs j ON j.command_id=c.id WHERE c.id=?',(row['command_id'],))
    plan=_parsed({'id':row['command_id'],**job},'plan_json') if job else {}
    if (not job or job['action']!=ACTION or _hash(plan)!=job['plan_hash']
            or _hash(record)!=row['record_hash'] or record.get('version')!=1
            or record.get('proposal_id')!=proposal['id'] or record.get('command_id')!=row['command_id']
            or record.get('rollback')!=plan.get('rollback') or record.get('diff_unified')!=proposal['diff_unified']
            or record['rollback']['source']['proposal']['learning_id']!=proposal['learning_id']
            or record['rollback']['source']['destination']['target_path']!=proposal['target_path']
            or record['rollback']['source']['destination']['target_kind']!=proposal['target_kind']):
        raise CommandError('ResolutionDataError','The resolution differs from its frozen job and source.',500)
    return record


def proposed_patch(current, response, target):
    if (not isinstance(response,dict) or set(response)!={'explanation','edits'}
            or not isinstance(response['explanation'],str) or not response['explanation'].strip()
            or not isinstance(response['edits'],list) or not response['edits']):
        raise CommandError('InvalidResolution','The model did not provide an explained, local resolution proposal.')
    spans=[]
    for edit in response['edits']:
        if not isinstance(edit,dict) or set(edit)!={'old','new'} or any(not isinstance(edit[k],str) for k in ('old','new')):
            raise CommandError('InvalidResolution','Each proposed edit must contain only exact old and new text.')
        old,new=edit['old'],edit['new']
        if old==new or (not old and current) or (old and current.count(old)!=1):
            raise CommandError('InvalidResolution','A proposed span is unchanged, absent, or ambiguous in the current file.')
        if old==current and len(current.splitlines())>1:
            raise CommandError('InvalidResolution','A whole-file rewrite is not a local rollback resolution.')
        at=current.find(old) if old else 0
        spans.append((at,at+len(old),new))
    spans.sort()
    if any(a[1]>b[0] or a[0]==b[0] for a,b in zip(spans,spans[1:])):
        raise CommandError('InvalidResolution','Proposed resolution spans overlap.')
    after=current
    for start,end,new in reversed(spans):after=after[:start]+new+after[end:]
    if after==current:
        raise CommandError('InvalidResolution','The resolution does not change the conflicting content.')
    return make_unified_diff(current,after,target)


def generate(store,cfg,saved,llm,journal):
    """Return one validated model result; no proposal or target mutation here."""
    from .resources import bundled_path
    from .redact import redact_text
    frozen=saved['plan']['rollback']
    prompt=bundled_path('prompts','resolve_rollback.md').read_text()+'\n\n'+redact_text(_json(frozen))
    def ask():
        answer=llm.call(ACTION,cfg.strong_model_class,prompt,True)
        if not answer.ok:
            raise CommandError('ResolutionModelFailed',f'The resolution call failed: {answer.outcome}. {answer.error}')
        diff=proposed_patch(frozen['base']['content'],answer.parsed,frozen['source']['destination']['target_path'])
        return {'explanation':answer.parsed['explanation'],'diff_unified':diff}
    return journal.step('rollback:resolution',{'preview_revision':frozen['revision'],'prompt_hash':_hash(prompt)},ask)


def persist(store,cfg,saved,run_id,generated):
    """Publish the generated draft and its origin in the caller's transaction."""
    from .rollback import rollback_preview
    frozen=saved['plan']['rollback'];source=frozen['source']
    current=rollback_preview(store,cfg,source['proposal']['id'])
    if current['revision']!=frozen['revision']:
        raise CommandError('ResolutionSourceChanged','The application or target changed during generation. Inspect a fresh conflict.',409)
    existing=store.query_one('SELECT proposal_id FROM proposal_resolutions WHERE command_id=?',(saved['id'],))
    if existing:return existing['proposal_id']
    pid=new_id();stamp=utc_now_iso();destination=source['destination']
    store.insert('proposals',{'id':pid,'learning_id':source['proposal']['learning_id'],'run_id':run_id,
        'target_path':destination['target_path'],'target_kind':destination['target_kind'],'action':ACTION,
        'diff_unified':generated['diff_unified'],'status':'pending','created_at':stamp})
    record={'version':1,'command_id':saved['id'],'proposal_id':pid,'rollback':frozen,**generated}
    store.insert('proposal_resolutions',{'proposal_id':pid,'command_id':saved['id'],'record_json':_json(record),'record_hash':_hash(record),'created_at':stamp})
    store.insert('proposal_events',{'id':new_id(),'proposal_id':pid,'ts':stamp,'event':'created','actor':'auto',
        'note':_json({'command_id':saved['id'],'reason':'rollback_resolution','source_application_id':source['application_id']})})
    return pid
