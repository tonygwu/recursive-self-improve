"""Typed, durable user intent. This module never applies files or runs models.

A content fingerprint binds each decision to the exact proposal, explanation,
eval, and retained evidence returned by Review. Immutable revisions are saved
inside the same transaction as the command and every member decision.
"""
from __future__ import annotations

import hashlib
import json
import re

from .execution_policy import TARGET_CLASS, waiting_proposals
from .destinations import DestinationError, resolve_destination, destination_identity
from .store import PROPOSAL_ACTIONS, actor_for, new_id, utc_now_iso

COMMAND_MIGRATION = '0010_dashboard_commands'
COMMAND_STATES = frozenset({'queued', 'running', 'blocked', 'failed', 'cancelled', 'completed'})
COMMAND_ACTIONS = frozenset({'approve', 'reject_target', 'reject_lesson'})
CONTROL_ACTIONS = frozenset({'retry_delivery', 'cancel_delivery'})
CONTROL_MIGRATION = '0011_command_controls'
MAX_MEMBERS = 200


class CommandError(ValueError):
    def __init__(self, code: str, detail: str, status_code: int = 400):
        super().__init__(detail)
        self.code, self.status_code = code, status_code


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def _hash(value) -> str:
    return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


def _parsed(row, field):
    try:
        value = json.loads(row[field])
    except (TypeError, ValueError) as exc:
        raise CommandError('CommandDataError', f"{row['id']}: {field} is unreadable", 500) from exc
    if not isinstance(value, dict):
        raise CommandError('CommandDataError', f"{row['id']}: {field} must be an object", 500)
    return value


def require_schema(store):
    if store.query_one('SELECT name FROM schema_migrations WHERE name=?', (COMMAND_MIGRATION,)) is None:
        raise CommandError('UpgradeRequired', 'Upgrade the state database before submitting dashboard commands.', 503)


def review_snapshot(store, proposal_id: str, cfg) -> dict:
    """Read a complete revision; callers wrap multi-query reads in a transaction."""
    proposal = store.query_one('SELECT * FROM proposals WHERE id=?', (proposal_id,))
    if proposal is None:
        raise CommandError('NoSuchProposal', f'No proposal {proposal_id!r}.', 404)
    learning = store.query_one('SELECT * FROM learnings WHERE id=?', (proposal['learning_id'],))
    if learning is None:
        raise CommandError('CommandDataError', f'Proposal {proposal_id!r} has no learning.', 500)
    evidence = store.query(
        'SELECT i.id, i.ts, i.signal_type, i.matched_text, i.window_json, '
        'i.session_id, i.session_file, i.project_key, i.project_path '
        'FROM incidents i JOIN incident_learnings il ON il.incident_id=i.id '
        'WHERE il.learning_id=? ORDER BY i.ts, i.id', (proposal['learning_id'],),
    )
    evaluation = store.query_one('SELECT * FROM eval_results WHERE id=?', (proposal['eval_result_id'],)) if proposal['eval_result_id'] else None
    snapshot = {'version': 2, 'proposal': proposal, 'learning': learning,
                'destination': _destination(proposal, cfg),
                'evidence': evidence, 'evaluation': evaluation}
    from .resolutions import origin
    resolution=origin(store,proposal)
    if resolution:
        snapshot.update(version=3,resolution=resolution,destination=resolution['rollback']['source']['destination'])
    from .reapplications import origin as reapplication_origin
    reapplication=reapplication_origin(store,proposal)
    if reapplication:
        snapshot.update(version=4,reapplication=reapplication,destination=reapplication['source']['destination'])
    from .recovery_jobs import origin as recovery_origin
    recovery=recovery_origin(store,proposal)
    if recovery:
        snapshot.update(version=5,recovery=recovery,destination=recovery['source']['destination'])
    from .rejections import target_identity
    snapshot['target_identity'] = target_identity(store, snapshot['destination'])
    return {'proposal_id': proposal_id, 'revision': _hash(snapshot), 'snapshot': snapshot}


def _request(body: dict) -> dict:
    if not isinstance(body, dict) or set(body) - {'request_key', 'action', 'members', 'note', 'preview_revision'}:
        raise CommandError('InvalidCommand', 'Use request_key, action, members, and an optional note; paths and shell commands are not accepted.')
    if not isinstance(body.get('action'), str) or body['action'] not in COMMAND_ACTIONS:
        raise CommandError('UnknownCommand', 'Use approve, reject_target, or reject_lesson for a reviewed selection.')
    key = body.get('request_key')
    if not isinstance(key, str) or re.fullmatch(r'[A-Za-z0-9._:-]{8,200}', key) is None:
        raise CommandError('InvalidRequestKey', 'request_key must be 8–200 letters, numbers, dots, colons, underscores, or hyphens.')
    note = body.get('note', '')
    if not isinstance(note, str):
        raise CommandError('InvalidCommand', 'note must be text.')
    raw = body.get('members')
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_MEMBERS:
        raise CommandError('InvalidMembers', f'Select between 1 and {MAX_MEMBERS} proposal revisions.')
    members, ids = [], set()
    for member in raw:
        if not isinstance(member, dict) or set(member) != {'proposal_id', 'revision'}:
            raise CommandError('InvalidMembers', 'Each member must contain proposal_id and revision.')
        pid, revision = member['proposal_id'], member['revision']
        if not isinstance(pid, str) or not pid or pid in ids:
            raise CommandError('InvalidMembers', 'Proposal IDs must be nonempty and unique.')
        if not isinstance(revision, str) or re.fullmatch(r'[a-f0-9]{64}', revision) is None:
            raise CommandError('InvalidRevision', f'Reload proposal {pid!r} before submitting its revision.')
        ids.add(pid)
        members.append({'proposal_id': pid, 'revision': revision})
    request = {'action': body['action'], 'request_key': key, 'note': note,
               'members': sorted(members, key=lambda m: m['proposal_id'])}
    if 'preview_revision' in body:
        if body['action'] != 'approve':
            raise CommandError('InvalidCommand', 'A rejection binds member revisions; it does not authorize a combined edit.')
        preview = body['preview_revision']
        if not isinstance(preview, str) or re.fullmatch(r'[a-f0-9]{64}', preview) is None:
            raise CommandError('InvalidRevision', 'Reload the selected edit preview before approving it.')
        request['preview_revision'] = preview
    return request


def _destination(proposal, cfg):
    if proposal['action'] not in PROPOSAL_ACTIONS or proposal['target_kind'] not in TARGET_CLASS:
        raise CommandError('UnsupportedProposal', f"Proposal {proposal['id']!r} has an unknown action or target kind.")
    try:
        return resolve_destination(cfg, proposal['target_path'], proposal['target_kind'])
    except DestinationError as exc:
        raise CommandError('InvalidTarget', f"Proposal {proposal['id']!r}: {exc}") from exc


def save_revision(store, reviewed, stamp):
    """Append the content addressed revision inside the caller's transaction."""
    if _hash(reviewed['snapshot']) != reviewed['revision']:
        raise CommandError('CommandDataError', 'The revision does not match its snapshot.', 500)
    snapshot_json = _json(reviewed['snapshot'])
    row = store.query_one('SELECT * FROM proposal_revisions WHERE proposal_id=? AND fingerprint=?',
                          (reviewed['proposal_id'], reviewed['revision']))
    if row:
        if row['snapshot_json'] != snapshot_json:
            raise CommandError('CommandDataError', f"Revision {row['id']} no longer matches its fingerprint.", 500)
        return row['id']
    rid = new_id()
    store.insert('proposal_revisions', {'id': rid, 'proposal_id': reviewed['proposal_id'],
                 'fingerprint': reviewed['revision'], 'snapshot_json': snapshot_json, 'created_at': stamp})
    return rid


def load_revision(store, revision_id: str) -> dict:
    """Load frozen authorization with integrity checks, never current content."""
    row = store.query_one('SELECT * FROM proposal_revisions WHERE id=?', (revision_id,))
    if row is None:
        raise CommandError('CommandDataError', f'Revision {revision_id!r} is missing.', 500)
    snapshot = _parsed(row, 'snapshot_json')
    expected = {'version', 'proposal', 'learning', 'destination', 'evidence', 'evaluation'}
    if snapshot.get('version') in (2,3,4,5):
        expected.add('target_identity')
    if snapshot.get('version')==3:
        expected.add('resolution')
    if snapshot.get('version')==4:
        expected.add('reapplication')
    if snapshot.get('version')==5:
        expected.add('recovery')
    if (set(snapshot) != expected
            or snapshot['version'] not in (1,2,3,4,5)
            or not all(isinstance(snapshot[k], dict) for k in ('proposal', 'learning', 'destination'))
            or not isinstance(snapshot['evidence'], list)
            or not (snapshot['evaluation'] is None or isinstance(snapshot['evaluation'], dict))
            or snapshot['proposal'].get('id') != row['proposal_id']
            or _hash(snapshot) != row['fingerprint']):
        raise CommandError('CommandDataError', f'Revision {revision_id!r} no longer matches its fingerprint or shape.', 500)
    if snapshot['version'] in (2,3,4,5):
        from .rejections import validate_identity
        try:
            validate_identity(snapshot['target_identity'])
        except ValueError as exc:
            raise CommandError('CommandDataError',f'Revision {revision_id!r}: {exc}',500) from exc
    return {'proposal_id': row['proposal_id'], 'revision': row['fingerprint'], 'snapshot': snapshot}


def command_status(store, command_id: str) -> dict:
    require_schema(store)
    row = store.query_one('SELECT * FROM commands WHERE id=?', (command_id,))
    if row is None:
        raise CommandError('NoSuchCommand', f'No command {command_id!r}.', 404)
    from . import reapplications
    if row['action']==reapplications.ACTION:
        return reapplications.status(store,row)
    from . import jobs
    if row['action'] in jobs.ACTIONS:
        return jobs.status(store,row)
    from . import rejections
    if row['action'] in rejections.ACTIONS:
        return rejections.command_status(store, row)
    if row['state'] not in COMMAND_STATES:
        raise CommandError('CommandDataError', f"Command {command_id!r} has unknown state {row['state']!r}.", 500)
    try:
        request = _request(_parsed(row, 'payload_json'))
    except CommandError as exc:
        raise CommandError('CommandDataError', f'Command {command_id!r} has an invalid saved request: {exc}', 500) from exc
    if (request['action'] != row['action'] or request['request_key'] != row['request_key']
            or _hash({k: v for k, v in request.items() if k != 'request_key'}) != row['request_hash']):
        raise CommandError('CommandDataError', f'Command {command_id!r} does not match its saved request.', 500)
    members, revisions = [], {}
    for member in store.query('SELECT * FROM command_members WHERE command_id=? ORDER BY proposal_id', (command_id,)):
        reviewed = load_revision(store, member['revision_id'])
        if reviewed['proposal_id'] != member['proposal_id'] or member['decision_scope'] != 'target':
            raise CommandError('CommandDataError', f"Command member {member['id']} has inconsistent authorization.", 500)
        revisions[member['proposal_id']] = reviewed['snapshot']
        members.append({k: member[k] for k in ('proposal_id', 'target_id', 'decision_scope')} | {'revision': reviewed['revision']})
    if [{'proposal_id': m['proposal_id'], 'revision': m['revision']} for m in members] != request['members']:
        raise CommandError('CommandDataError', f'Command {command_id!r} has a different member set than its saved request.', 500)
    targets, previews = [], []
    for target in store.query('SELECT * FROM command_targets WHERE command_id=? ORDER BY target_key', (command_id,)):
        if target['state'] not in COMMAND_STATES:
            raise CommandError('CommandDataError', f"Target {target['id']} has an unknown state.", 500)
        destination = _parsed(target, 'destination_json')
        checkpoint = _parsed(target, 'checkpoint_json')
        from .delivery_records import DeliveryRecordError, validated_cancellation
        try:
            validated_cancellation(checkpoint, target['state'])
        except DeliveryRecordError as exc:
            raise CommandError('CommandDataError', f"Target {target['id']}: {exc}", 500) from exc
        if 'delivery' in checkpoint:
            from .delivery_records import DeliveryRecordError, validated_delivery
            try:
                validated_delivery(checkpoint, target['diff_unified'])
            except DeliveryRecordError as exc:
                raise CommandError('CommandDataError', f"Target {target['id']} has an invalid checkpoint: {exc}", 500) from exc
        if 'preview_revision' in request:
            preview = checkpoint.get('review_preview')
            if (not isinstance(preview, dict) or preview.get('state') != 'ready'
                    or preview.get('destination') != destination
                    or preview.get('diff_unified') != target['diff_unified']
                    or preview.get('target_key') != target['target_key']):
                raise CommandError('CommandDataError', f"Target {target['id']} differs from its authorized preview.", 500)
            previews.append(preview)
        target_members = [m for m in members if m['target_id'] == target['id']]
        if not target_members:
            raise CommandError('CommandDataError', f"Target {target['id']} has no authorization.", 500)
        if not any(destination == revisions[m['proposal_id']]['destination'] for m in target_members):
            raise CommandError('CommandDataError', f"Target {target['id']} has no matching reviewed destination.", 500)
        for member in target_members:
            frozen = revisions[member['proposal_id']]
            try:
                identity = destination_identity(destination)
                agrees = (identity == destination_identity(frozen['destination'])
                          and _hash(identity) == target['target_key']
                          and destination['target_kind'] == frozen['destination']['target_kind']
                          and ('preview_revision' in request or target['diff_unified'] == frozen['proposal']['diff_unified']))
            except (KeyError, TypeError) as exc:
                raise CommandError('CommandDataError', f"Target {target['id']} has an invalid destination.", 500) from exc
            if not agrees:
                raise CommandError('CommandDataError', f"Target {target['id']} differs from its reviewed revision.", 500)
        targets.append({'id': target['id'], 'state': target['state'],
                        'destination': destination, 'diff_unified': target['diff_unified'],
                        'checkpoint': checkpoint,
                        'error_code': target['error_code'], 'error_detail': target['error_detail']})
    if {m['target_id'] for m in members} != {t['id'] for t in targets}:
        raise CommandError('CommandDataError', f'Command {command_id!r} has missing targets.', 500)
    if 'preview_revision' in request:
        from .review import preview_revision
        if preview_revision(members, previews) != request['preview_revision']:
            raise CommandError('CommandDataError', f'Command {command_id!r} no longer matches the reviewed preview.', 500)
    if row.get('cancel_requested', 0) not in (0, 1):
        raise CommandError('CommandDataError', f"Command {command_id!r} has invalid cancellation state.", 500)
    controls = controls_available(store)
    history = []
    if controls:
        for event in store.query('SELECT * FROM command_control_events WHERE command_id=? ORDER BY created_at, id', (command_id,)):
            history.append({k: event[k] for k in ('id', 'action', 'actor', 'note', 'created_at')} | {'before': _parsed(event, 'before_json')})
    return {k: row[k] for k in ('id', 'action', 'state', 'actor', 'created_at', 'updated_at',
                                'max_model_calls', 'error_code', 'error_detail')} | {
        'members': members, 'targets': targets, 'result': _parsed(row, 'result_json'),
        'cancel_requested': bool(row.get('cancel_requested', 0)), 'controls_available': controls, 'control_history': history,
        'can_retry': controls and row['state'] in {'blocked', 'failed'},
        'can_cancel': controls and row['state'] in {'queued', 'running', 'blocked', 'failed'}
                      and not row.get('cancel_requested', 0) and any(t['state'] not in {'completed', 'cancelled'} for t in targets)}


def submit_command(store, cfg, body: dict, *, now: str | None = None) -> dict:
    """Persist one all-or-nothing authorization. Replays return the same command."""
    from . import reapplications
    if isinstance(body,dict) and body.get('action')==reapplications.ACTION:
        return reapplications.submit(store,cfg,body,now=now)
    from . import jobs
    if isinstance(body,dict) and isinstance(body.get('action'),str):
        if body['action'] in jobs.ACTIONS:return jobs.submit(store,cfg,body,now=now)
        if body['action'] in jobs.CONTROLS:return jobs.control(store,body,now=now)
    from .operations import OPERATION_CONTROL_ACTIONS, submit_operation_control
    if isinstance(body, dict) and isinstance(body.get('action'), str) and body['action'] in OPERATION_CONTROL_ACTIONS:
        return submit_operation_control(store, body, now=now)
    if isinstance(body, dict) and body.get('action') == 'rollback':
        from .rollback import submit_rollback
        return submit_rollback(store,cfg,body,now=now)
    if isinstance(body, dict) and isinstance(body.get('action'), str) and body['action'] in CONTROL_ACTIONS:
        return submit_control(store, body, now=now)
    request = _request(body)
    require_schema(store)
    request_hash = _hash({k: v for k, v in request.items() if k != 'request_key'})
    stamp = now or utc_now_iso()
    with store.transaction(write=True):
        from .rollback import instruction_request_exists
        if instruction_request_exists(store, request['request_key']):
            raise CommandError('IdempotencyConflict', 'This request key already identifies an instruction operation.', 409)
        if controls_available(store) and store.query_one('SELECT id FROM command_control_events WHERE request_key=?', (request['request_key'],)):
            raise CommandError('IdempotencyConflict', 'This request key already identifies a delivery control.', 409)
        existing = store.query_one('SELECT id, request_hash FROM commands WHERE request_key=?', (request['request_key'],))
        if existing:
            if existing['request_hash'] != request_hash:
                raise CommandError('IdempotencyConflict', 'This request key already identifies a different command.', 409)
            return command_status(store, existing['id'])
        if request['action'] in {'reject_target', 'reject_lesson'}:
            from .rejections import record
            return record(store, cfg, request, stamp)
        waiting = {p['id'] for p in waiting_proposals(store, cfg)}
        reviewed, targets = [], {}
        for member in request['members']:
            current = review_snapshot(store, member['proposal_id'], cfg)
            if current['revision'] != member['revision']:
                raise CommandError('StaleRevision', f"Proposal {member['proposal_id']!r} changed. Reload and review it before deciding.", 409)
            if member['proposal_id'] not in waiting:
                raise CommandError('NotWaiting', f"Proposal {member['proposal_id']!r} is no longer waiting for a decision.", 409)
            proposal = current['snapshot']['proposal']
            destination = current['snapshot']['destination']
            key = _hash(destination_identity(destination))
            if key in targets:
                target = targets[key]
                if target['destination']['target_kind'] != destination['target_kind'] or ('preview_revision' not in request and target['diff_unified'] != proposal['diff_unified']):
                    raise CommandError('ConflictingEdits', 'Selected proposals contain different edits for the same target. Select one existing proposal or request regeneration.', 409)
            else:
                targets[key] = {'id': new_id(), 'destination': destination, 'diff_unified': proposal['diff_unified']}
            reviewed.append((current, targets[key]['id']))
        from .resolutions import validate_selection
        validate_selection([current for current, _ in reviewed])
        from .reapplications import validate_selection as validate_reapplications
        validate_reapplications([current for current, _ in reviewed])
        if 'preview_revision' in request:
            from .review import prepare_targets, preview_revision
            members = [current for current, _ in reviewed]
            previews = prepare_targets(cfg, members)
            if preview_revision(members, previews) != request['preview_revision']:
                raise CommandError('StalePreview', 'The target or selected edit changed. Reload the preview before approving.', 409)
            for preview in previews:
                if preview['state'] != 'ready':
                    raise CommandError(preview['error_code'], preview['detail'], 409)
                target = targets[preview['target_key']]
                target['diff_unified'] = preview['diff_unified']
                target['checkpoint'] = {'review_preview': preview}
        cid = new_id()
        store.insert('commands', {'id': cid, 'request_key': request['request_key'], 'request_hash': request_hash,
                     'action': request['action'], 'state': 'queued', 'actor': 'user', 'created_at': stamp,
                     'updated_at': stamp, 'payload_json': _json(request), 'max_model_calls': 0})
        for key, target in targets.items():
            store.insert('command_targets', {'id': target['id'], 'command_id': cid, 'target_key': key,
                         'destination_json': _json(target['destination']), 'diff_unified': target['diff_unified'],
                         'checkpoint_json': _json(target.get('checkpoint', {}))})
        for current, target_id in reviewed:
            rid = save_revision(store, current, stamp)
            pid = current['proposal_id']
            store.insert('command_members', {'id': new_id(), 'command_id': cid, 'proposal_id': pid,
                         'revision_id': rid, 'target_id': target_id, 'decision_scope': 'target'})
            store.update('proposals', 'id', pid, {'status': 'approved_user'})
            store.insert('proposal_events', {'id': new_id(), 'proposal_id': pid, 'ts': stamp,
                         'event': 'approved_user', 'actor': actor_for('approved_user'), 'note': request['note']})
        return command_status(store, cid)



def controls_available(store):
    return store.query_one('SELECT name FROM schema_migrations WHERE name=?', (CONTROL_MIGRATION,)) is not None


def require_control_schema(store):
    require_schema(store)
    if not controls_available(store):
        raise CommandError('UpgradeRequired', 'Upgrade the state database before running the delivery worker or using delivery controls.', 503)


def submit_control(store, body, *, now=None):
    """Record retry or cancellation intent; this function never executes a file."""
    require_control_schema(store)
    if set(body) - {'request_key', 'action', 'command_id', 'note'}:
        raise CommandError('InvalidCommand', 'Delivery controls accept a command ID, request key, action, and optional note.')
    key, cid, note = body.get('request_key'), body.get('command_id'), body.get('note', '')
    if not isinstance(key, str) or re.fullmatch(r'[A-Za-z0-9._:-]{8,200}', key) is None:
        raise CommandError('InvalidRequestKey', 'Use a unique request key of 8–200 letters, numbers, dots, colons, underscores, or hyphens.')
    if not isinstance(cid, str) or not cid or not isinstance(note, str):
        raise CommandError('InvalidCommand', 'command_id must be nonempty text and note must be text.')
    request_hash = _hash({'action': body['action'], 'command_id': cid, 'note': note})
    with store.transaction(write=True):
        from .rollback import instruction_request_exists
        if instruction_request_exists(store, key):
            raise CommandError('IdempotencyConflict', 'This request key already identifies an instruction operation.', 409)
        old = store.query_one('SELECT * FROM command_control_events WHERE request_key=?', (key,))
        if old:
            if old['request_hash'] != request_hash:
                raise CommandError('IdempotencyConflict', 'This request key already identifies a different delivery control.', 409)
            return command_status(store, old['command_id'])
        if store.query_one('SELECT id FROM commands WHERE request_key=?', (key,)):
            raise CommandError('IdempotencyConflict', 'This request key already identifies a command.', 409)
        command = command_status(store, cid)
        if body['action'] == 'retry_delivery':
            if not command['can_retry']:
                raise CommandError('NotRetryable', 'This delivery is not blocked or failed. Refresh its current result.', 409)
        elif body['action'] == 'cancel_delivery':
            if not command['can_cancel']:
                raise CommandError('NotCancellable', 'This delivery has finished or already has a cancellation request. Refresh its current result.', 409)
        else:
            raise CommandError('UnknownCommand', 'Unknown delivery control.')
        stamp = now or utc_now_iso()
        store.insert('command_control_events', {'id': new_id(), 'request_key': key, 'request_hash': request_hash,
                     'command_id': cid, 'action': body['action'], 'actor': 'user', 'note': note, 'created_at': stamp,
                     'before_json': _json({'state': command['state'], 'cancel_requested': command['cancel_requested'],
                         'targets': [{k: t[k] for k in ('id', 'state', 'error_code', 'error_detail')} for t in command['targets']]})})
        updates = {'updated_at': stamp, 'error_code': '', 'error_detail': '', 'result_json': '{}'}
        if command['state'] != 'running':
            updates['state'] = 'queued'
        if body['action'] == 'cancel_delivery':
            updates['cancel_requested'] = 1
        store.update('commands', 'id', cid, updates)
        for target in command['targets']:
            if target['state'] in {'blocked', 'failed'}:
                store.update('command_targets', 'id', target['id'], {'state': 'queued', 'error_code': '', 'error_detail': ''})
        return command_status(store, cid)



def command_summary(command):
    """Progress for polling; full content remains in the explicit detail read."""
    keys = ('id', 'action', 'state', 'actor', 'created_at', 'updated_at', 'max_model_calls',
            'cancel_requested', 'controls_available', 'can_retry', 'can_cancel')
    summary = {k: command[k] for k in keys} | {
        'result': command['result'],
        'member_count': len(command['members']),
        'counts': {s: sum(t['state'] == s for t in command['targets']) for s in sorted(COMMAND_STATES)},
        'control_count': len(command['control_history']),
        'targets': [{k: t[k] for k in ('id', 'state', 'destination', 'error_code', 'error_detail')} | {
            'delivery_result': t['checkpoint'].get('result', {}),
            'prepared': 'delivery' in t['checkpoint'],
        } for t in command['targets']],
    }
    from .jobs import ACTIONS as JOB_ACTIONS
    if command['action'] in JOB_ACTIONS:
        summary.update({k:command[k] for k in ('budget','run_id','failure_taxonomy','error_code','error_detail')})
        summary['stages']=command['plan']['stages']
        if command['action']=='propose_recovery':summary.update(learning_id=command['learning_id'],selection=command['selection'])
        elif command['action']=='mine_incident':summary['incident_id']=command['incident_id']
        else:summary['proposal_id']=command['members'][0]['proposal_id']
    return summary


def list_commands(store, *, limit=50, cursor=None, summary=False):
    import base64
    from datetime import datetime
    require_schema(store)
    if not 1 <= limit <= 100:
        raise CommandError('InvalidLimit', 'limit must be between 1 and 100')
    where, params = '', ()
    if cursor is not None:
        try:
            decoded = json.loads(base64.b64decode(cursor + '=' * (-len(cursor) % 4), altchars=b'-_', validate=True))
            if not isinstance(decoded, list) or len(decoded) != 2 or not all(isinstance(v, str) and v for v in decoded):
                raise ValueError('invalid cursor shape')
            if datetime.fromisoformat(decoded[0].replace('Z', '+00:00')).tzinfo is None:
                raise ValueError('cursor time has no timezone')
        except (ValueError, TypeError) as exc:
            raise CommandError('InvalidCursor', 'Use the next_cursor returned by the command history.') from exc
        where = ' WHERE created_at<? OR (created_at=? AND id<?)'
        params = (decoded[0], decoded[0], decoded[1])
    rows = store.query('SELECT id, created_at FROM commands' + where + ' ORDER BY created_at DESC, id DESC LIMIT ?', (*params, limit + 1))
    page = rows[:limit]
    commands = [command_status(store, r['id']) for r in page]
    more = len(rows) > limit
    next_cursor = base64.urlsafe_b64encode(_json([page[-1]['created_at'], page[-1]['id']]).encode()).decode().rstrip('=') if more else None
    return {'commands': [command_summary(c) for c in commands] if summary else commands,
            'limit': limit, 'more_available': more, 'next_cursor': next_cursor}
