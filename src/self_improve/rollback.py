"""Read-only rollback sources and exact inverse previews. No instruction writes."""
from difflib import SequenceMatcher
from pathlib import Path
import json
import subprocess

from .commands import CommandError, _hash, command_status, load_revision
from .destinations import _git, resolve_destination, read_destination, DestinationError
from .propose import PatchConflict, apply_unified_diff, make_unified_diff


class RollbackError(CommandError):
    pass


class RollbackConflict(RollbackError):
    def __init__(self, detail):
        super().__init__('RollbackConflict', detail, 409)


def reverse_applied_change(before, applied, current):
    """Reverse exact changed blocks; ambiguity or edits to a block are conflicts.

    Unrelated current lines retain their bytes. Repeated blocks require matching
    context even when only one copy remains, so another copy cannot become the
    accidental target after a person removes the original one.
    """
    if current == applied:
        return before
    original, old, now = (s.splitlines(keepends=True) for s in (before, applied, current))
    edits = []
    for tag, a, b, c, d in SequenceMatcher(None, original, old, autojunk=False).get_opcodes():
        if tag == 'equal':
            continue
        needle, replacement = old[c:d], original[a:b]
        candidates = [i for i in range(len(now)-len(needle)+1) if now[i:i+len(needle)] == needle]
        source_copies = sum(old[i:i+len(needle)] == needle for i in range(len(old)-len(needle)+1))
        if not needle or len(candidates) != 1 or source_copies != 1:
            left, right = old[max(0,c-3):c], old[d:d+3]
            if not left and not right:
                candidates = [0] if not now and not needle else []
            else:
                candidates = [i for i in candidates
                    if (not left or (i >= len(left) and now[i-len(left):i] == left))
                    and (not right or now[i+len(needle):i+len(needle)+len(right)] == right)]
        if len(candidates) != 1:
            raise RollbackConflict('Cannot uniquely locate the applied change in the current content; the rule changed or its location is ambiguous.')
        at = candidates[0]
        if edits and at < edits[-1][1]:
            raise RollbackConflict('The current change blocks overlap or moved out of order.')
        edits.append((at, at+len(needle), replacement))
    result, position = [], 0
    for start, end, replacement in edits:
        result.extend(now[position:start]);result.extend(replacement);position=end
    result.extend(now[position:])
    return ''.join(result)


def read_snapshot(cfg, sha, target):
    repo = cfg.state_path('snapshots')
    if not sha or _git(repo, 'cat-file', '-t', sha) != 'commit':
        raise RollbackError('MissingSnapshot', f'The applied snapshot {sha!r} is unavailable.', 409)
    path = str(Path(target)).lstrip('/')
    entry = _git(repo, 'ls-tree', '-z', sha, '--', ':(literal)' + path)
    if not entry:
        return None
    metadata, name = entry.rstrip('\0').split('\t', 1)
    mode, kind, oid = metadata.split()
    if kind != 'blob' or mode not in {'100644', '100755'} or name != path:
        raise RollbackError('InvalidSnapshot', 'The applied snapshot does not contain the named instruction file.', 409)
    result = subprocess.run(['git','--no-optional-locks','-C',str(repo),'cat-file','blob',oid],capture_output=True)
    if result.returncode:
        raise RollbackError('MissingSnapshot', result.stderr.decode(errors='replace'), 409)
    return result.stdout.decode('utf-8')


def latest_application(store, proposal_id):
    return store.query_one("SELECT * FROM proposal_events WHERE proposal_id=? AND event='applied' ORDER BY ts DESC,id DESC LIMIT 1", (proposal_id,))


def _note(event):
    try:
        result=json.loads(event['note'])
        if not isinstance(result,dict):raise ValueError('not an object')
        return result
    except (TypeError,ValueError) as exc:
        raise RollbackError('InvalidApplication', f"Application event {event['id']} has an unreadable record.",409) from exc


def _application_source(store,cfg,proposal_id,*,event_id=None):
    """Use the requested application event, or the latest when no ID is given."""
    current=store.query_one('SELECT * FROM proposals WHERE id=?',(proposal_id,))
    if current is None:raise RollbackError('ProposalNotFound', f'no proposal with id {proposal_id!r}',404)
    event=(store.query_one("SELECT * FROM proposal_events WHERE id=? AND proposal_id=? AND event='applied'", (event_id,proposal_id))
           if event_id is not None else latest_application(store,proposal_id))
    if event is None:
        if current['status']=='applied' and not current['snapshot_commit_before']:
            raise RollbackError('MissingSnapshot', f'proposal {proposal_id} has no snapshot_commit_before',409)
        raise RollbackError('NotApplied', f"proposal {proposal_id} has no recorded application; only 'applied' changes can be rolled back",409)
    note=_note(event)
    if note.get('mode') not in {'file','git_branch'} or any(not isinstance(note.get(k),str) or not note[k] for k in ('snapshot_before','snapshot_after')):
        raise RollbackError('InvalidApplication','The application lacks its delivery mode or snapshot references.',409)
    cohort=[{'proposal_id':proposal_id,'applied_event_id':event['id']}]
    if note.get('operation_id'):
        from .operations import operation_status
        operation=operation_status(store,note['operation_id'])
        if operation['kind']!='auto_apply' or operation['state']!='completed' or operation['proposal_id']!=proposal_id:
            raise RollbackError('InvalidApplication','The application operation does not match this proposal.',409)
        source=operation['record']['proposal'];dest=operation['record']['destination'];delivery=operation['checkpoint']['delivery']
        before,applied=delivery['before_content'],delivery['after_content']
        before_exists=delivery['before_exists'];application_id='operation:'+operation['id']
        if note.get('snapshot_before')!=delivery['snapshot_before'] or note.get('snapshot_after')!=operation['result']['snapshot_commit_after']:
            raise RollbackError('InvalidApplication','The application event differs from its operation snapshots.',409)
        full_after=applied
    elif note.get('command_id'):
        command=command_status(store,note['command_id'])
        targets=[t for t in command['targets'] if t['id']==note.get('target_id') and t['state']=='completed']
        if len(targets)!=1:raise RollbackError('InvalidApplication','The applied command target is unavailable.',409)
        target=targets[0];dest=target['destination'];delivery=target['checkpoint']['delivery']
        before=delivery['before_content'];before_exists=delivery['before_exists'];full_after=delivery['after_content']
        selected=store.query_one('SELECT * FROM command_members WHERE target_id=? AND proposal_id=?',(target['id'],proposal_id))
        if selected is None or selected['revision_id']!=note.get('revision_id'):
            raise RollbackError('InvalidApplication','The application does not identify a frozen member revision.',409)
        source=load_revision(store,selected['revision_id'])['snapshot']['proposal']
        applied=apply_unified_diff(before,source['diff_unified'])
        application_id='command:'+command['id']+':'+target['id']
        cohort=[]
        for member in store.query('SELECT * FROM command_members WHERE target_id=? ORDER BY proposal_id',(target['id'],)):
            frozen=load_revision(store,member['revision_id'])['snapshot']['proposal']
            if apply_unified_diff(before,frozen['diff_unified'])==applied:
                member_event=latest_application(store,member['proposal_id'])
                if member_event:
                    member_note = _note(member_event)
                    if member_note.get('target_id') == target['id']:
                        if (member_note.get('command_id') != command['id']
                                or member_note.get('revision_id') != member['revision_id']
                                or member_note.get('mode') != dest['mode']
                                or member_note.get('branch', '') != dest['branch_name']):
                            raise RollbackError('InvalidApplication', 'An equivalent application has a different recorded revision or destination.', 409)
                        cohort.append({'proposal_id':member['proposal_id'],'applied_event_id':member_event['id']})
        if note.get('snapshot_before')!=delivery['snapshot_before'] or note.get('snapshot_after')!=target['checkpoint']['result']['snapshot_after']:
            raise RollbackError('InvalidApplication','The application event differs from its command snapshots.',409)
    else:
        # Historical events have no immutable proposal revision. Their retained
        # snapshots prove the entire one-proposal file change, not today's text.
        source=current;dest=resolve_destination(cfg,current['target_path'],current['target_kind'])
        if note.get('mode')=='git_branch':
            from dataclasses import replace
            if not note.get('branch'):raise RollbackError('MissingDestination','Historical application has no recorded branch.',409)
            dest=resolve_destination(replace(cfg,project_branch_name=note['branch']),current['target_path'],current['target_kind'])
        if dest['mode']!=note.get('mode'):
            raise RollbackError('DestinationChanged','The historical application destination changed.',409)
        prior=read_snapshot(cfg,note.get('snapshot_before'),dest['target_path'])
        before=prior or '';before_exists=prior is not None
        applied=read_snapshot(cfg,note.get('snapshot_after'),dest['target_path'])
        if applied is None:raise RollbackError('MissingSnapshot','The applied file is absent from its after snapshot.',409)
        full_after=applied;application_id='event:'+event['id']
    if note['mode'] != dest['mode'] or note.get('branch', '') != dest['branch_name']:
        raise RollbackError('InvalidApplication', 'The application event differs from its recorded destination.', 409)
    prior=read_snapshot(cfg,note.get('snapshot_before'),dest['target_path'])
    after=read_snapshot(cfg,note.get('snapshot_after'),dest['target_path'])
    if prior!=(before if before_exists else None) or after!=full_after:
        raise RollbackError('InvalidSnapshot','The retained snapshots do not match the recorded application.',409)
    contribution=_hash({'before':before,'applied':applied,'before_exists':before_exists})
    return {'proposal':source,'destination':dest,'application_id':application_id,'contribution_hash':contribution,
            'before':before,'applied':applied,'before_exists':before_exists,'affected_members':cohort,
            'snapshot_before':note['snapshot_before'],'snapshot_after':note['snapshot_after']}


def application_source(store,cfg,proposal_id,*,event_id=None):
    try:
        return _application_source(store,cfg,proposal_id,event_id=event_id)
    except (DestinationError,OSError,UnicodeError,PatchConflict) as exc:
        raise RollbackError('ApplicationUnavailable',f'The recorded application cannot be verified: {exc}',409) from exc


def rollback_key(source):
    return 'rollback:'+source['application_id']+':'+source['contribution_hash']


def rollback_view(store, cfg, proposal_id):
    """Attach current progress to the HTTP view without changing authorization."""
    from .operations import operation_summary, operations_available
    preview = rollback_preview(store, cfg, proposal_id)
    active = None
    if operations_available(store):
        for operation in rollback_operations(store, preview['source']):
            if operation['state'] == 'cancelled':
                continue
            if (operation['id'] == preview.get('existing_operation_id')
                    or 'delivery' in operation['checkpoint']
                    or operation['record']['rollback']['revision'] == preview['revision']):
                active = operation_summary(operation)
                break
    return preview | {'active_operation': active}


def rollback_preview(store,cfg,proposal_id):
    source=application_source(store,cfg,proposal_id)
    from .resolutions import completed
    receipt=completed(store,source)
    if receipt:
        result={'source':source,'ready':False,'error_code':'AlreadyRolledBack','detail':'This contribution was undone through a reviewed resolution.','resolution_result':receipt}
        return result | {'revision':_hash(result)}
    from .operations import operations_available
    existing = committed_or_prepared_rollback(store, source) if operations_available(store) else None
    if existing and existing['state'] == 'completed':
        completed = {'source': source, 'ready': False, 'error_code': 'AlreadyRolledBack',
                     'detail': 'This applied contribution has already been rolled back.', 'existing_operation_id': existing['id']}
        return completed | {'revision': _hash(completed)}
    try:
        base=read_destination(source['destination'])
    except (DestinationError, OSError, UnicodeError) as exc:
        raise RollbackError('DestinationUnavailable', f'The rollback target cannot be read: {exc}', 409) from exc
    preview={'source':source,'base':base,'ready':False,'error_code':'','detail':'','after_content':'','after_exists':True,'diff_unified':''}
    try:
        if not base['exists']:raise RollbackConflict('The applied file is now missing; there is no current content to reverse.')
        after=reverse_applied_change(source['before'],source['applied'],base['content'])
        preview.update(ready=True,after_content=after,after_exists=bool(after) or source['before_exists'],
                       diff_unified=make_unified_diff(base['content'],after,source['destination']['target_path']))
    except RollbackConflict as exc:
        preview.update(error_code=exc.code,detail=str(exc))
    preview['revision']=_hash(preview)
    return preview


INTENT_MIGRATION='0013_instruction_requests'


def instruction_request_exists(store,key):
    available=store.query_one('SELECT name FROM schema_migrations WHERE name=?',(INTENT_MIGRATION,))
    return bool(available and store.query_one('SELECT request_key FROM instruction_requests WHERE request_key=?',(key,)))


def rollback_operations(store,source):
    from .operations import operation_status
    rows=store.query('SELECT id FROM instruction_operations WHERE operation_key LIKE ? ORDER BY created_at DESC,id DESC', (rollback_key(source)+':%',))
    return [operation_status(store,row['id']) for row in rows]


def committed_or_prepared_rollback(store,source):
    operations=rollback_operations(store,source)
    return next((op for op in operations if op['state']=='completed'),None) or next((op for op in operations if 'delivery' in op['checkpoint'] and op['state']!='cancelled'),None)


def create_rollback_operation(store,preview,*,actor='user',now=None):
    """Record intent, keeping abandoned previews and every prepared operation."""
    from .commands import _json
    from .store import new_id,utc_now_iso
    source=preview['source'];stamp=now or utc_now_iso()
    operations=rollback_operations(store,source)
    for op in operations:
        if op['state']=='completed':return op['id']
        if 'delivery' in op['checkpoint'] and op['state']!='cancelled':
            if op['record']['rollback']['revision']!=preview['revision']:
                raise RollbackError('ReconciliationRequired',f"Operation {op['id']} has a prepared write. Reconcile it before replacing its preview.",409)
            return op['id']
    if not preview['ready']:raise RollbackConflict(preview['detail'])
    for op in operations:
        if op['state']=='cancelled':continue
        if op['record']['rollback']['revision']==preview['revision']:
            store.update('instruction_operations','id',op['id'],{'state':'queued','error_code':'','error_detail':'','updated_at':stamp})
            return op['id']
        # No checkpoint means the previous intent never reached publication.
        store.update('instruction_operations','id',op['id'],{'state':'cancelled',
            'checkpoint_json':_json({'cancellation':{'no_delivery_observed':True,'at':stamp}}),
            'result_json':_json({'proposal_id':op['proposal_id'],'outcome':'held','reason':'superseded_preview','detail':'A new reviewed rollback preview replaced this unwritten intent.'}),
            'updated_at':stamp})
    oid=new_id()
    record={'version':2,'kind':'rollback','proposal':source['proposal'],'destination':source['destination'],
            'authorization':{'actor':actor},'rollback':preview}
    store.insert('instruction_operations',{'id':oid,'operation_key':rollback_key(source)+':'+oid,'kind':'rollback','proposal_id':source['proposal']['id'],
        'record_json':_json(record),'record_hash':_hash(record),'checkpoint_json':'{}','state':'queued','created_at':stamp,'updated_at':stamp})
    return oid


def submit_rollback(store,cfg,body,*,now=None):
    import re
    from .commands import _json
    from .store import utc_now_iso
    from .operations import require_operations,operation_status
    require_operations(store)
    if not store.query_one('SELECT name FROM schema_migrations WHERE name=?',(INTENT_MIGRATION,)):
        raise RollbackError('UpgradeRequired','Upgrade the state database before submitting rollback requests.',503)
    if set(body)-{'action','request_key','proposal_id','preview_revision','note'}:
        raise RollbackError('InvalidCommand','Rollback accepts a proposal ID, reviewed preview revision, request key, and note.')
    key,pid,revision,note=(body.get(k) for k in ('request_key','proposal_id','preview_revision','note'))
    note='' if note is None else note
    if not isinstance(key,str) or re.fullmatch(r'[A-Za-z0-9._:-]{8,200}',key) is None:
        raise RollbackError('InvalidRequestKey','Use a unique request key of 8–200 letters, numbers, dots, colons, underscores, or hyphens.')
    if not isinstance(pid,str) or not pid or not isinstance(revision,str) or re.fullmatch('[a-f0-9]{64}',revision) is None or not isinstance(note,str):
        raise RollbackError('InvalidCommand','Rollback requires a proposal ID and its exact reviewed preview revision.')
    request={'action':'rollback','proposal_id':pid,'preview_revision':revision,'note':note};fingerprint=_hash(request)
    with store.transaction(write=True):
        old=store.query_one('SELECT * FROM instruction_requests WHERE request_key=?',(key,))
        if old:
            if old['request_hash']!=fingerprint:raise RollbackError('IdempotencyConflict','This request key identifies a different rollback.',409)
            result=operation_status(store,old['operation_id'])
        else:
            if store.query_one('SELECT id FROM commands WHERE request_key=?',(key,)) or store.query_one('SELECT id FROM command_control_events WHERE request_key=?',(key,)):
                raise RollbackError('IdempotencyConflict','This request key identifies another command.',409)
            preview=rollback_preview(store,cfg,pid)
            if preview['revision']!=revision:raise RollbackError('StaleRevision','The application or target changed. Read a fresh rollback preview.',409)
            oid=create_rollback_operation(store,preview,now=now)
            store.insert('instruction_requests',{'request_key':key,'request_hash':fingerprint,'operation_id':oid,
                'request_json':_json(request),'created_at':now or utc_now_iso()})
            result=operation_status(store,oid)
    return result | {'status_url':'/api/operations/'+result['id']}
