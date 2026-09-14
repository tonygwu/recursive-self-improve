"""Permanent lesson or target suppression, independent of proposal lifetimes.

Legacy rejected_user proposals retain lesson scope. New target decisions never
enter that global pool. Rejection records freeze the reviewed lesson and canonical
target; reads and decision recording do not write files or call models.
"""
from __future__ import annotations

import json

from .commands import CommandError, _hash, _json, load_revision, save_revision
from .store import actor_for, new_id

MIGRATION = '0015_rejection_scopes'
ACTIONS = frozenset({'reject_target', 'reject_lesson'})


def available(store):
    return store.query_one('SELECT name FROM schema_migrations WHERE name=?', (MIGRATION,)) is not None


def target_identity(store, destination):
    """Logical target identity; a delivery branch or independent clone is not a new target.

    Use the existing cached repository ID when available, otherwise the normalized
    remote URL. Preserve both so a later cache fill does not narrow an old decision.
    This resolver does not query GitHub or update the identity cache.
    """
    if destination['mode'] == 'file':
        return {'mode': 'file', 'path': destination['target_path']}
    from .project_identity import resolve
    identity = resolve(destination['repo_root'], use_gh=False)
    if identity.method not in {'remote_url', 'git_root'}:
        raise CommandError('TargetIdentityUnavailable', 'The target repository cannot be identified. Reload after restoring it.', 409)
    remote = identity.key.removeprefix('remote:') if identity.method == 'remote_url' else ''
    cached = store.query_one('SELECT project_key FROM project_identity_cache WHERE remote_norm=?', (remote,)) if remote else None
    key = cached['project_key'] if cached else identity.key
    if not isinstance(key, str) or not key:
        raise CommandError('TargetIdentityUnavailable', 'The cached project identity is empty.', 500)
    return {'mode': 'project', 'project_key': key, 'remote': remote, 'path': destination['relative_path']}


def same_target(a, b):
    if a['mode'] != b['mode'] or a['path'] != b['path']:
        return False
    return a['mode'] == 'file' or a['project_key'] == b['project_key'] or bool(a['remote'] and a['remote'] == b['remote'])


def validate_identity(identity):
    from pathlib import PurePosixPath
    if not isinstance(identity, dict) or identity.get('mode') not in {'file','project'}:
        raise ValueError('unknown canonical target mode')
    keys={'mode','path'} if identity['mode']=='file' else {'mode','path','project_key','remote'}
    if set(identity)!=keys or any(not isinstance(v,str) for v in identity.values()) or not identity['path']:
        raise ValueError('invalid canonical target fields')
    path=PurePosixPath(identity['path'])
    if identity['mode']=='file':
        if not path.is_absolute(): raise ValueError('a file target must be absolute')
    elif path.is_absolute() or '..' in path.parts or identity['path']=='.' or not identity['project_key']:
        raise ValueError('a project target must name a repository and relative file')


def _validated_member(store, row, action):
    frozen = load_revision(store, row['revision_id'])
    scope = 'lesson' if action == 'reject_lesson' else 'target'
    try:
        identity = json.loads(row['target_identity_json'])
        expected = frozen['snapshot']['target_identity'] if scope == 'target' else {}
        if scope=='target': validate_identity(identity)
        if (row['proposal_id'] != frozen['proposal_id']
                or row['learning_id'] != frozen['snapshot']['learning']['id']
                or row['decision_scope'] != scope or identity != expected):
            raise ValueError('scope, lesson, or target differs from the reviewed revision')
    except (KeyError, TypeError, ValueError) as exc:
        raise CommandError('RejectionDataError', f"Rejection member {row['id']}: {exc}", 500) from exc
    return {'proposal_id': row['proposal_id'], 'revision': frozen['revision'], 'revision_id': row['revision_id'],
            'learning_id': row['learning_id'], 'decision_scope': scope, 'target_identity': identity,
            'rule_text': frozen['snapshot']['learning']['rule_text'], 'target_id': '', 'snapshot':frozen['snapshot']}


def command_status(store, row):
    from .commands import _request, _parsed
    request = _request(_parsed(row, 'payload_json'))
    if (request['action'] != row['action'] or row['action'] not in ACTIONS
            or row['state'] != 'completed' or row['actor'] != 'user' or row['max_model_calls'] != 0
            or request['request_key'] != row['request_key']
            or _hash({k:v for k,v in request.items() if k!='request_key'}) != row['request_hash']):
        raise CommandError('RejectionDataError', f"Rejection {row['id']} differs from its recorded request.", 500)
    members = [_validated_member(store, m, row['action']) for m in store.query(
        'SELECT * FROM rejection_members WHERE command_id=? ORDER BY proposal_id', (row['id'],))]
    if [{k:m[k] for k in ('proposal_id','revision')} for m in members] != request['members']:
        raise CommandError('RejectionDataError', f"Rejection {row['id']} has a different member set.", 500)
    result = _parsed(row, 'result_json')
    ids = result.get('suppressed_proposal_ids')
    if (not isinstance(ids, list) or any(not isinstance(p,str) for p in ids)
            or result.get('scope') != ('lesson' if row['action']=='reject_lesson' else 'target')):
        raise CommandError('RejectionDataError', f"Rejection {row['id']} has an invalid result.", 500)
    return {k:row[k] for k in ('id','action','state','actor','created_at','updated_at','max_model_calls','error_code','error_detail')} | {
        'members': members, 'targets': [], 'result': result, 'cancel_requested': False,
        'controls_available': False, 'control_history': [], 'can_retry': False, 'can_cancel': False}


def context(store):
    """One coherent read for a queue or pipeline selection, including explicit lineage."""
    learnings = {r['id']:r for r in store.query('SELECT id,status,rule_text,duplicate_of FROM learnings')}
    rejected = {lid for lid,r in learnings.items() if r['status']=='rejected'}
    scoped = available(store)
    for p in store.query("SELECT * FROM proposals WHERE status='rejected_user'"):
        if scoped and p['decision_scope'] not in {'','target','lesson'}:
            raise CommandError('RejectionDataError',f"Proposal {p['id']} has an unknown rejection scope.",500)
        if not scoped or p.get('decision_scope','') != 'target':
            rejected.add(p['learning_id'])
    decisions = []
    if scoped:
        for row in store.query("SELECT * FROM commands WHERE action IN ('reject_target','reject_lesson')"):
            command = command_status(store, row)
            for member in command['members']:
                decisions.append({k:v for k,v in member.items() if k!='snapshot'} | {'command_id':command['id']})
                if member['decision_scope']=='lesson':
                    rejected.add(member['learning_id'])
    links = {lid:{lid} for lid in learnings}
    for lid,r in learnings.items():
        parent = r['duplicate_of']
        if parent in learnings:
            links[lid].add(parent)
            links[parent].add(lid)
    return {'learnings':learnings, 'rejected':rejected, 'decisions':decisions, 'links':links}


def _related(ctx, lid):
    seen, todo = set(), [lid]
    while todo:
        here = todo.pop()
        if here in seen:
            continue
        seen.add(here)
        todo.extend(ctx['links'].get(here, ()))
    return seen


def lesson_rejected(store, learning_id, *, ctx=None):
    ctx = ctx if ctx is not None else context(store)
    return bool(_related(ctx, learning_id) & ctx['rejected'])


def _text(value):
    return ' '.join(value.split()).casefold()


def rejection_reason(store, cfg, proposal, *, ctx=None, learning=None, destination=None):
    if proposal.get('action')=='resolve_rollback':
        from .resolutions import origin
        origin(store,proposal)  # Only a retained explicit inverse job qualifies.
        return None  # Suppression prevents new rules, not a reviewed undo of an applied edit.
    ctx = ctx if ctx is not None else context(store)
    lid = proposal['learning_id']
    if lesson_rejected(store, lid, ctx=ctx):
        return {'code':'LessonRejected', 'reason':'lesson_rejected', 'scope':'lesson',
                'detail':'This lesson is permanently rejected at every target. Existing applied edits are retained.'}
    candidates = [d for d in ctx['decisions'] if d['decision_scope']=='target']
    if not candidates:
        return None
    learning = learning or ctx['learnings'].get(lid, {})
    related = _related(ctx, lid)
    candidates = [d for d in candidates if d['learning_id'] in related
                  or (_text(learning.get('rule_text','')) and _text(learning['rule_text'])==_text(d['rule_text']))]
    if not candidates:
        return None
    from .destinations import resolve_destination
    identity = target_identity(store, destination or resolve_destination(cfg, proposal['target_path'], proposal['target_kind']))
    for d in candidates:
        if same_target(identity,d['target_identity']):
            return {'code':'TargetRejected', 'reason':'target_rejected', 'scope':'target', 'command_id':d['command_id'],
                    'detail':'This lesson is permanently rejected at this canonical target. Other targets remain available.'}
    return None


def proposal_rejection(store, cfg, learning, target_path, target_kind, embedder):
    """Before generation/eval: also check near duplicates at this target only."""
    ctx = context(store)
    proposal = {'learning_id':learning['id'], 'target_path':str(target_path), 'target_kind':target_kind}
    reason = rejection_reason(store,cfg,proposal,ctx=ctx,learning=learning)
    if reason:
        return reason
    targets = [d for d in ctx['decisions'] if d['decision_scope']=='target']
    if not targets:
        return None
    from .destinations import resolve_destination
    from .cluster import is_duplicate
    identity=target_identity(store,resolve_destination(cfg,str(target_path),target_kind))
    vectors=[(d['rule_text'],embedder.cached_vector('rejected_revision',d['revision_id'],d['rule_text']))
             for d in targets if same_target(identity,d['target_identity'])]
    if not vectors:
        return None
    duplicate, matched = is_duplicate(learning['rule_text'],vectors,embedder.encode,cfg.cluster_dup_cosine)
    if duplicate:
        return {'code':'TargetRejected','reason':'target_rejected','scope':'target',
                'detail':'This rule matches a prior rejection at this canonical target. Other targets remain available.',
                'matched_rule':matched}
    return None


def record(store, cfg, request, stamp):
    """Inside submit_command's transaction; no partial family decisions."""
    if not available(store):
        raise CommandError('UpgradeRequired', 'Upgrade the state database before scoped rejection.', 503)
    from .commands import review_snapshot
    from .execution_policy import waiting_proposals
    from .store import DECIDED_STATUSES
    waiting = {p['id'] for p in waiting_proposals(store,cfg)}
    reviewed=[]
    for member in request['members']:
        current=review_snapshot(store,member['proposal_id'],cfg)
        if current['revision'] != member['revision']:
            raise CommandError('StaleRevision', f"Proposal {member['proposal_id']} changed. Reload before rejecting.",409)
        if request['action']=='reject_target' and member['proposal_id'] not in waiting:
            raise CommandError('NotWaiting',f"Proposal {member['proposal_id']} is no longer waiting.",409)
        reviewed.append(current)
    scope='lesson' if request['action']=='reject_lesson' else 'target'
    cid=new_id()
    store.insert('commands',{'id':cid,'request_key':request['request_key'],
        'request_hash':_hash({k:v for k,v in request.items() if k!='request_key'}),'action':request['action'],
        'state':'completed','actor':'user','created_at':stamp,'updated_at':stamp,'payload_json':_json(request),'max_model_calls':0})
    for current in reviewed:
        snap=current['snapshot']
        store.insert('rejection_members',{'id':new_id(),'command_id':cid,'proposal_id':current['proposal_id'],
            'revision_id':save_revision(store,current,stamp),'learning_id':snap['learning']['id'],'decision_scope':scope,
            'target_identity_json':_json(snap['target_identity'] if scope=='target' else {})})
    # The result is initialized before a coherent context read validates the command.
    store.update('commands','id',cid,{'result_json':_json({'scope':scope,'suppressed_proposal_ids':[]})})
    ctx=context(store)
    affected=[]
    for p in store.query('SELECT * FROM proposals ORDER BY id'):
        if p['status'] in DECIDED_STATUSES and p['status']!='approved_user':
            continue
        reason=rejection_reason(store,cfg,p,ctx=ctx)
        if reason is None:
            continue
        # Only this decision's scope changes rows; an unrelated old suppression
        # is still enforced by readers and writers, not reattributed to this user action.
        if scope=='target' and reason.get('command_id')!=cid:
            continue
        if scope=='lesson' and not any(m['snapshot']['learning']['id'] in _related(ctx,p['learning_id']) for m in reviewed):
            continue
        store.update('proposals','id',p['id'],{'status':'rejected_user','decision_scope':scope})
        store.insert('proposal_events',{'id':new_id(),'proposal_id':p['id'],'ts':stamp,'event':'rejected_user',
            'actor':actor_for('rejected_user'),'note':_json({'command_id':cid,'scope':scope,'note':request['note']})})
        affected.append(p['id'])
    if scope=='lesson':
        for current in reviewed:
            for lid in _related(ctx,current['snapshot']['learning']['id']):
                store.update('learnings','id',lid,{'status':'rejected'})
    store.update('commands','id',cid,{'result_json':_json({'scope':scope,'suppressed_proposal_ids':affected})})
    return command_status(store,store.query_one('SELECT * FROM commands WHERE id=?',(cid,)))
