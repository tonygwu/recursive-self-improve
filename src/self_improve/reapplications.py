"""Explicit reapplication intent and frozen origin; no models or instruction writes."""
from .commands import CommandError, _hash, _json, _parsed
from .store import new_id, utc_now_iso
import re

MIGRATION = '0018_reapplications'
ACTION = 'request_reapplication'
PROPOSAL_ACTION = 'reapply'


def require_schema(store):
    if not store.query_one('SELECT name FROM schema_migrations WHERE name=?',(MIGRATION,)):
        raise CommandError('UpgradeRequired','Upgrade the database before requesting reapplication.',503)


def source_key(source):
    return _hash({k:source[k] for k in ('application_id','contribution_hash')})


def applied_successor(store,source):
    """An older undo never starts a second application chain."""
    rows=store.query('SELECT proposal_id FROM proposal_reapplications WHERE source_key=? ORDER BY created_at,proposal_id',(source_key(source),))
    from .rollback import latest_application
    return next((r['proposal_id'] for r in rows if latest_application(store,r['proposal_id'])),None)


def receipt(store,cfg,source):
    from .rollback import committed_or_prepared_rollback, latest_application, _note, read_snapshot
    from .resolutions import completed
    resolution=completed(store,source)
    if resolution:return {'kind':'resolution','result':resolution}
    operation=committed_or_prepared_rollback(store,source)
    if operation:
        if operation['state']=='completed':
            return {'kind':'operation','id':operation['id'],'result':operation['result']}
        raise CommandError('ReconciliationRequired','Finish or cancel the prepared rollback before requesting reapplication.',409)
    # Earlier releases recorded snapshots and events without instruction operations.
    applied=latest_application(store,source['proposal']['id'])
    for event in store.query("SELECT * FROM proposal_events WHERE proposal_id=? AND event='rolled_back' ORDER BY ts DESC,id DESC",(source['proposal']['id'],)):
        note=_note(event)
        if note.get('operation_id') or note.get('resolution_proposal_id'):
            continue
        if event['ts'] < applied['ts'] or note.get('restored_from')!=source['snapshot_before']:
            continue
        snapshot=note.get('snapshot_rollback')
        if not isinstance(snapshot,str) or not snapshot:
            raise CommandError('MissingRollbackReceipt','The historical rollback has no retained after snapshot.',409)
        after=read_snapshot(cfg,snapshot,source['destination']['target_path'])
        if after!=(source['before'] if source['before_exists'] else None):
            raise CommandError('InvalidRollbackReceipt','The historical rollback snapshot differs from the recorded before state.',409)
        return {'kind':'historical','event':event}
    raise CommandError('NotRolledBack','Only a completed, recorded rollback can be reapplied.',409)


def preview(store,cfg,proposal_id):
    from .rollback import application_source, reverse_applied_change, RollbackConflict
    from .destinations import read_destination, DestinationError
    from .propose import make_unified_diff
    from .rejections import rejection_reason
    require_schema(store)
    source=application_source(store,cfg,proposal_id)
    shown={'source':source,'ready':False,'error_code':'','detail':'','max_model_calls':0,
        'base':None,'rollback_receipt':None,'after_content':'','diff_unified':'','successor_id':None}
    try:
        shown['rollback_receipt']=receipt(store,cfg,source)
        successor=applied_successor(store,source)
        if successor:
            shown['successor_id']=successor
            raise CommandError('SourceReapplied','This rollback already has a later application. Inspect that application before creating another.',409)
        candidate={**source['proposal'],'action':PROPOSAL_ACTION}
        reason=rejection_reason(store,cfg,candidate,destination=source['destination'])
        if reason:raise CommandError(reason['code'],reason['detail'],409)
        base=read_destination(source['destination']);shown['base']=base
        try:
            # A successful inverse proves the contribution is already present.
            reverse_applied_change(source['before'],source['applied'],base['content'])
        except RollbackConflict:
            pass
        else:
            raise CommandError('AlreadyPresent','The recorded change is already present in this target. No duplicate proposal was created.',409)
        try:
            after=reverse_applied_change(source['applied'],source['before'],base['content'])
        except RollbackConflict as exc:
            raise CommandError('ReapplicationConflict','The original change cannot be uniquely projected onto the current target. Inspect the current content and request a corrected proposal.',409) from exc
        diff=make_unified_diff(base['content'],after,source['destination']['target_path'])
        if not diff:raise CommandError('AlreadyPresent','Reapplication makes no content change.',409)
        shown.update(ready=True,after_content=after,diff_unified=diff)
    except CommandError as exc:
        shown.update(error_code=exc.code,detail=str(exc))
    except (DestinationError,OSError,UnicodeError) as exc:
        shown.update(error_code='DestinationUnavailable',detail=str(exc))
    return shown | {'revision':_hash(shown)}


def _request(body):
    if not isinstance(body,dict) or set(body)!={'action','request_key','proposal_id','preview_revision'} or body['action']!=ACTION:
        raise CommandError('InvalidReapplication','Use action, request_key, proposal_id, and preview_revision.')
    if not isinstance(body['request_key'],str) or re.fullmatch(r'[A-Za-z0-9._:-]{8,200}',body['request_key']) is None:
        raise CommandError('InvalidRequestKey','Use a unique request key of 8–200 characters.')
    if not isinstance(body['proposal_id'],str) or not body['proposal_id']:
        raise CommandError('InvalidReapplication','Choose an original proposal.')
    if not isinstance(body['preview_revision'],str) or re.fullmatch(r'[a-f0-9]{64}',body['preview_revision']) is None:
        raise CommandError('InvalidRevision','Inspect the reapplication preview first.')
    return body


def view(store,cfg,proposal_id):
    """Progress is outside the authorization fingerprint."""
    shown=preview(store,cfg,proposal_id)
    latest=None
    for row in store.query('SELECT command_id AS id,proposal_id FROM proposal_reapplications WHERE source_key=? ORDER BY created_at DESC,proposal_id DESC',(source_key(shown['source']),)):
        _,record,_=_record(store,command_id=row['id'])
        if record['preview']['revision']==shown['revision']:
            latest=row
            break
    return shown | {'latest_request':latest}


def _record(store,*,proposal_id=None,command_id=None):
    require_schema(store)
    field,value=('proposal_id',proposal_id) if proposal_id is not None else ('command_id',command_id)
    row=store.query_one(f'SELECT * FROM proposal_reapplications WHERE {field}=?',(value,))
    if not row:raise CommandError('ReapplicationDataError','The reapplication origin is missing.',500)
    record=_parsed({'id':value,**row},'record_json')
    command=store.query_one('SELECT * FROM commands WHERE id=?',(row['command_id'],))
    request=_request(_parsed(command,'payload_json')) if command else None
    if (not command or command['action']!=ACTION or command['actor']!='user' or command['state']!='completed'
            or _hash(record)!=row['record_hash'] or record.get('version')!=1
            or record.get('proposal_id')!=row['proposal_id'] or record.get('command_id')!=row['command_id']
            or record.get('preview',{}).get('revision')!=request['preview_revision']
            or record['preview']['source']['proposal']['id']!=request['proposal_id']
            or source_key(record['preview']['source'])!=row['source_key']
            or _hash({k:v for k,v in record['preview'].items() if k!='revision'})!=request['preview_revision']
            or command['request_key']!=request['request_key']
            or command['request_hash']!=_hash({k:v for k,v in request.items() if k!='request_key'})):
        raise CommandError('ReapplicationDataError','The reapplication differs from its recorded request and preview.',500)
    return row,record,command


def origin(store,proposal):
    if proposal['action']!=PROPOSAL_ACTION:return None
    _,record,_=_record(store,proposal_id=proposal['id']);shown=record['preview'];source=shown['source']
    if (proposal['diff_unified']!=shown['diff_unified'] or proposal['learning_id']!=source['proposal']['learning_id']
            or any(proposal[k]!=source['destination'][k] for k in ('target_path','target_kind'))):
        raise CommandError('ReapplicationDataError','The draft differs from its frozen reapplication.',500)
    return {'version':1,'proposal_id':proposal['id'],'command_id':record['command_id'],
            'source':source,'rollback_receipt':shown['rollback_receipt'],'preview_revision':shown['revision']}


def ensure_pending(store,cfg,record):
    from .rollback import latest_application
    for member in record['source']['affected_members']:
        latest=latest_application(store,member['proposal_id'])
        if not latest or latest['id']!=member['applied_event_id']:
            raise CommandError('ApplicationChanged','The original proposal has a newer application. Inspect it before reapplying.',409)
    if applied_successor(store,record['source']):
        raise CommandError('SourceReapplied','This rollback already has a later application.',409)
    if receipt(store,cfg,record['source'])!=record['rollback_receipt']:
        raise CommandError('RollbackChanged','The recorded rollback changed after review.',409)


def validate_selection(members):
    seen=set()
    for member in members:
        record=member['snapshot'].get('reapplication')
        if record is None:continue
        key=source_key(record['source'])
        if key in seen:raise CommandError('ConflictingReapplications','Select one reapplication for this recorded rollback.',409)
        seen.add(key)


def submit(store,cfg,body,*,now=None):
    from .jobs import _unused_key
    require_schema(store);request=_request(body);stamp=now or utc_now_iso()
    request_hash=_hash({k:v for k,v in request.items() if k!='request_key'})
    with store.transaction(write=True):
        _unused_key(store,request['request_key'])
        previous=store.query_one('SELECT * FROM commands WHERE request_key=?',(request['request_key'],))
        if previous:
            if previous['action']!=ACTION or previous['request_hash']!=request_hash:
                raise CommandError('IdempotencyConflict','This request key identifies another command.',409)
            return status(store,previous)
        shown=preview(store,cfg,request['proposal_id'])
        if shown['revision']!=request['preview_revision']:
            raise CommandError('StaleReapplicationPreview','The target or original application changed. Inspect a fresh preview.',409)
        if not shown['ready']:raise CommandError(shown['error_code'],shown['detail'],409)
        cid,pid=new_id(),new_id();source=shown['source'];destination=source['destination']
        store.insert('commands',{'id':cid,'request_key':request['request_key'],'request_hash':request_hash,
            'action':ACTION,'actor':'user','state':'completed','created_at':stamp,'updated_at':stamp,
            'payload_json':_json(request),'max_model_calls':0,'result_json':_json({'proposal_id':pid,'original_proposal_id':request['proposal_id']})})
        store.insert('proposals',{'id':pid,'learning_id':source['proposal']['learning_id'],
            'target_path':destination['target_path'],'target_kind':destination['target_kind'],'action':PROPOSAL_ACTION,
            'diff_unified':shown['diff_unified'],'status':'pending','created_at':stamp})
        record={'version':1,'proposal_id':pid,'command_id':cid,'preview':shown}
        store.insert('proposal_reapplications',{'proposal_id':pid,'command_id':cid,'source_key':source_key(source),
            'record_json':_json(record),'record_hash':_hash(record),'created_at':stamp})
        from .store import DECIDED_STATUSES, PROPOSAL_STATUSES, actor_for
        for older in store.query('SELECT p.id,p.status FROM proposal_reapplications r JOIN proposals p ON p.id=r.proposal_id WHERE r.source_key=? AND p.id!=?',(source_key(source),pid)):
            _,old_record,_=_record(store,proposal_id=older['id'])
            if (older['status'] in PROPOSAL_STATUSES and older['status'] not in DECIDED_STATUSES
                    and old_record['preview']['revision']!=shown['revision']):
                store.update('proposals','id',older['id'],{'status':'superseded'})
                store.insert('proposal_events',{'id':new_id(),'proposal_id':older['id'],'ts':stamp,'event':'superseded',
                    'actor':actor_for('superseded'),'note':_json({'command_id':cid,'replacement_proposal_id':pid,'reason':'fresh_reapplication_preview'})})
        store.insert('proposal_events',{'id':new_id(),'proposal_id':pid,'ts':stamp,'event':'created','actor':actor_for('created'),
            'note':_json({'command_id':cid,'reason':'explicit_reapplication','source_application_id':source['application_id']})})
        return status(store,store.query_one('SELECT * FROM commands WHERE id=?',(cid,)))


def status(store,row):
    linked,record,command=_record(store,command_id=row['id'])
    result=_parsed(command,'result_json')
    if result!={'proposal_id':linked['proposal_id'],'original_proposal_id':record['preview']['source']['proposal']['id']} or command['max_model_calls']!=0:
        raise CommandError('ReapplicationDataError','The reapplication result or call bound changed.',500)
    return {k:command[k] for k in ('id','action','state','actor','created_at','updated_at','max_model_calls','error_code','error_detail')} | {
        'members':[],'targets':[],'result':result,'record':record,'cancel_requested':False,
        'controls_available':True,'control_history':[],'can_retry':False,'can_cancel':False}
