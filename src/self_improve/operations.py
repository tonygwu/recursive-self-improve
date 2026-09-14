"""Read and validate durable instruction operations without importing a writer."""
import json

from .commands import COMMAND_STATES, CommandError, _hash, _parsed
from .delivery_records import DeliveryRecordError, validated_delivery, validated_cancellation
from .execution_policy import TARGET_CLASS

OPERATION_MIGRATION = '0012_instruction_operations'
OPERATION_CONTROL_MIGRATION = '0014_instruction_controls'
OPERATION_CONTROL_ACTIONS = frozenset({'retry_operation', 'cancel_operation'})
SOURCE_FIELDS = ('learning_id', 'run_id', 'status', 'action', 'target_path', 'target_kind',
                 'diff_unified', 'eval_result_id', 'created_at')


def operations_available(store):
    return store.query_one('SELECT name FROM schema_migrations WHERE name=?', (OPERATION_MIGRATION,)) is not None


def require_operations(store):
    if not operations_available(store):
        raise CommandError('UpgradeRequired', 'Upgrade the state database before instruction execution or recovery.', 503)


def operation_controls_available(store):
    return store.query_one('SELECT name FROM schema_migrations WHERE name=?', (OPERATION_CONTROL_MIGRATION,)) is not None


def _recorded_rollback_destination_matches(store, record):
    """A reviewed path alias is bound by the delivered command, not today's link."""
    from .commands import command_status, load_revision
    applied = record['rollback']['source']
    parts = applied['application_id'].split(':')
    if len(parts) != 3 or parts[0] != 'command':
        return False
    command = command_status(store, parts[1])
    target = next((t for t in command['targets'] if t['id'] == parts[2] and t['state'] == 'completed'), None)
    member = store.query_one('SELECT revision_id FROM command_members WHERE command_id=? AND target_id=? AND proposal_id=?',
                             (parts[1], parts[2], record['proposal']['id']))
    return bool(target and member and target['destination'] == record['destination']
                and load_revision(store, member['revision_id'])['snapshot']['proposal'] == record['proposal'])


def operation_status(store, operation_id):
    require_operations(store)
    row = store.query_one('SELECT * FROM instruction_operations WHERE id=?', (operation_id,))
    if row is None:
        raise CommandError('OperationNotFound', f'No instruction operation {operation_id!r}.', 404)
    record, checkpoint = _parsed(row, 'record_json'), _parsed(row, 'checkpoint_json')
    try:
        if row['state'] not in COMMAND_STATES or _hash(record) != row['record_hash']:
            raise ValueError('unknown state or changed record fingerprint')
        if row['kind'] not in {'auto_apply', 'rollback'} or record['kind'] != row['kind'] or record['version'] != (1 if row['kind']=='auto_apply' else 2):
            raise ValueError('unknown operation kind or version')
        source = record['proposal']
        if source['id'] != row['proposal_id']:
            raise ValueError('operation does not identify its proposal')
        if any(not isinstance(source[k], str) for k in SOURCE_FIELDS):
            raise ValueError('invalid source proposal')
        dest = record['destination']
        if any(not isinstance(dest[k], str) for k in ('mode', 'target_path', 'target_kind', 'branch_name', 'repo_root', 'git_common_dir', 'relative_path')):
            raise ValueError('invalid destination shape')
        path_matches = dest['target_path'] == source['target_path']
        if not path_matches and row['kind'] == 'rollback':
            path_matches = _recorded_rollback_destination_matches(store, record)
        if (not path_matches or dest['target_kind'] != source['target_kind']
                or dest['mode'] not in {'file', 'git_branch'}):
            raise ValueError('destination differs from the authorized proposal')
        auth = record['authorization']
        if row['kind'] == 'auto_apply':
            if (row['operation_key'] != 'auto_apply:' + source['id']
                    or auth['actor'] != 'auto' or auth['target_class'] not in {'global','project','skill'}
                    or TARGET_CLASS.get(source['target_kind']) != auth['target_class'] or source['status'] != 'gated_pass'
                    or type(auth['policy_revision']) is not int or auth['policy_revision'] < 1):
                raise ValueError('invalid automatic policy authorization')
            validated_delivery(checkpoint, source['diff_unified'])
        else:
            from .rollback import rollback_key
            preview = record['rollback']; applied = preview['source']
            if (any(not isinstance(applied[k], str) or not applied[k]
                    for k in ('application_id', 'contribution_hash', 'snapshot_before', 'snapshot_after'))
                    or any(not isinstance(applied[k], str) for k in ('before', 'applied'))
                    or type(applied['before_exists']) is not bool
                    or type(preview['base']['exists']) is not bool or type(preview['after_exists']) is not bool
                    or any(not isinstance(preview['base'][k], str) for k in ('content', 'content_hash', 'base_ref'))
                    or any(not isinstance(preview[k], str) for k in ('after_content', 'diff_unified', 'revision'))):
                raise ValueError('invalid inverse source or preview shape')
            if (auth['actor'] not in {'user','auto'} or row['operation_key'] != rollback_key(applied)+':'+row['id']
                    or applied['proposal'] != source or applied['destination'] != dest or preview['ready'] is not True
                    or preview['revision'] != _hash({k:v for k,v in preview.items() if k!='revision'})
                    or applied['contribution_hash'] != _hash({k:applied[k] for k in ('before','applied','before_exists')})):
                raise ValueError('invalid rollback authorization')
            members = applied['affected_members']
            if (not isinstance(members,list) or not members or source['id'] not in {m['proposal_id'] for m in members}
                    or len({m['proposal_id'] for m in members}) != len(members)
                    or any(not isinstance(m['applied_event_id'], str) or not m['applied_event_id'] for m in members)):
                raise ValueError('invalid rollback application membership')
            from .rollback import reverse_applied_change
            if (reverse_applied_change(applied['before'],applied['applied'],preview['base']['content']) != preview['after_content']
                    or preview['after_exists'] != (bool(preview['after_content']) or applied['before_exists'])):
                raise ValueError('rollback preview does not invert the applied contribution')
            if 'delivery' in checkpoint:
                delivery = validated_delivery(checkpoint, preview['diff_unified'], inverse_source=applied)
                if (delivery['before_content'] != preview['base']['content'] or delivery['before_exists'] != preview['base']['exists']
                        or delivery['after_content'] != preview['after_content'] or delivery['after_exists'] != preview['after_exists']):
                    raise ValueError('rollback delivery differs from the preview')
            elif row['state'] not in {'queued','running','blocked','failed','cancelled'}:
                raise ValueError('terminal rollback has no prepared delivery')
        cancelled = validated_cancellation(checkpoint, row['state'])
        if row['state'] == 'cancelled' and not cancelled:
            raise ValueError('cancellation has no reconciliation record')
        failures = json.loads(row['failures_json'])
        if not isinstance(failures, list) or type(row['attempts']) is not int or row['attempts'] < 0:
            raise ValueError('invalid failure history')
        for failure in failures:
            if (not isinstance(failure, dict) or type(failure.get('attempt')) is not int
                    or not 1 <= failure['attempt'] <= row['attempts']
                    or failure.get('state') not in {'blocked', 'failed'}
                    or any(not isinstance(failure.get(k), str) or not failure[k] for k in ('at', 'error_code'))
                    or not isinstance(failure.get('error_detail'), str)):
                raise ValueError('invalid failure history entry')
        result = _parsed(row, 'result_json')
        if row['state'] == 'completed':
            done = checkpoint['result']
            outcome = 'applied' if row['kind']=='auto_apply' else 'rolled_back'
            after_key = 'snapshot_commit_after' if row['kind']=='auto_apply' else 'snapshot_commit_rollback'
            before_sha = checkpoint['delivery']['snapshot_before'] if row['kind']=='auto_apply' else record['rollback']['source']['snapshot_before']
            if (result.get('operation_id') != row['id'] or result.get('outcome') != outcome
                    or result.get('proposal_id') != row['proposal_id']
                    or result.get(after_key) != done['snapshot_after']
                    or result.get('snapshot_commit_before') != before_sha
                    or result.get('branch_commit') != checkpoint['delivery']['branch_commit']
                    or result.get('mode') != dest['mode']):
                raise ValueError('completion differs from its recorded delivery')
    except (KeyError, TypeError, ValueError, DeliveryRecordError) as exc:
        raise CommandError('OperationDataError', f'Operation {operation_id}: {exc}', 500) from exc
    available = operation_controls_available(store)
    cancel = row.get('cancel_requested', 0)
    if type(cancel) is not int or cancel not in (0, 1):
        raise CommandError('OperationDataError', f'Operation {operation_id}: invalid cancellation flag', 500)
    history = operation_control_history(store, operation_id) if available else []
    return {k: row[k] for k in ('id','kind','proposal_id','state','attempts','error_code','error_detail','created_at','updated_at')} | {
        'record': record, 'checkpoint': checkpoint, 'failures': failures, 'result': result,
        'cancel_requested': bool(cancel), 'controls_available': available, 'control_history': history,
        'can_retry': available and row['state'] in {'blocked', 'failed'},
        'can_cancel': available and not cancel and row['state'] not in {'completed', 'cancelled'}}


def operation_control_history(store, operation_id):
    rows = store.query('SELECT c.*, r.request_json, r.request_hash, r.operation_id, r.created_at '
                       'FROM instruction_operation_controls c JOIN instruction_requests r ON r.request_key=c.request_key '
                       'WHERE r.operation_id=? ORDER BY r.created_at,r.request_key', (operation_id,))
    history = []
    for row in rows:
        try:
            request = json.loads(row['request_json'])
            before = json.loads(row['before_json'])
            if (not isinstance(request, dict) or _hash(request) != row['request_hash']
                    or request['operation_id'] != operation_id or request['action'] != row['action']
                    or request['note'] != row['note'] or row['actor'] != 'user'
                    or row['action'] not in OPERATION_CONTROL_ACTIONS
                    or not isinstance(before, dict) or before['state'] not in COMMAND_STATES
                    or type(before['cancel_requested']) is not bool
                    or any(not isinstance(before[k], str) for k in ('error_code', 'error_detail'))):
                raise ValueError('invalid control or prior result')
        except (KeyError, TypeError, ValueError) as exc:
            raise CommandError('OperationDataError', f"Operation {operation_id} control {row['request_key']}: {exc}", 500) from exc
        history.append({k: row[k] for k in ('request_key', 'action', 'actor', 'note', 'created_at')} | {'before': before})
    return history


def submit_operation_control(store, body, *, now=None):
    """Record one explicit retry/cancel request. Only the worker reconciles writes."""
    import re
    from .commands import _json
    from .store import utc_now_iso
    require_operations(store)
    if not operation_controls_available(store):
        raise CommandError('UpgradeRequired', 'Upgrade the state database before using operation controls.', 503)
    if set(body) - {'request_key', 'action', 'operation_id', 'note'}:
        raise CommandError('InvalidCommand', 'Operation controls accept an operation ID, action, request key, and optional note.')
    key, oid, action, note = (body.get('request_key'), body.get('operation_id'), body.get('action'), body.get('note', ''))
    if not isinstance(key, str) or re.fullmatch(r'[A-Za-z0-9._:-]{8,200}', key) is None:
        raise CommandError('InvalidRequestKey', 'Use a unique request key of 8–200 letters, numbers, dots, colons, underscores, or hyphens.')
    if not isinstance(oid, str) or not oid or action not in OPERATION_CONTROL_ACTIONS or not isinstance(note, str):
        raise CommandError('InvalidCommand', 'An operation control requires its operation ID and a supported action.')
    request = {'action': action, 'operation_id': oid, 'note': note}
    fingerprint = _hash(request)
    with store.transaction(write=True):
        old = store.query_one('SELECT * FROM instruction_requests WHERE request_key=?', (key,))
        if old:
            if old['request_hash'] != fingerprint:
                raise CommandError('IdempotencyConflict', 'This request key already identifies another instruction request.', 409)
            return operation_status(store, old['operation_id'])
        if (store.query_one('SELECT id FROM commands WHERE request_key=?', (key,))
                or store.query_one('SELECT id FROM command_control_events WHERE request_key=?', (key,))):
            raise CommandError('IdempotencyConflict', 'This request key already identifies another command.', 409)
        operation = operation_status(store, oid)
        if action == 'retry_operation' and not operation['can_retry']:
            raise CommandError('NotRetryable', 'This operation is not blocked or failed. Refresh its result.', 409)
        if action == 'cancel_operation' and not operation['can_cancel']:
            raise CommandError('NotCancellable', 'This operation has finished or already has a cancellation request. Refresh its result.', 409)
        stamp = now or utc_now_iso()
        store.insert('instruction_requests', {'request_key': key, 'request_hash': fingerprint, 'operation_id': oid,
                     'request_json': _json(request), 'created_at': stamp})
        store.insert('instruction_operation_controls', {'request_key': key, 'action': action, 'actor': 'user', 'note': note,
                     'before_json': _json({k: operation[k] for k in ('state', 'cancel_requested', 'error_code', 'error_detail')})})
        updates = {'updated_at': stamp, 'error_code': '', 'error_detail': ''}
        if operation['state'] != 'running':
            updates['state'] = 'queued'
        if action == 'cancel_operation':
            updates['cancel_requested'] = 1
        store.update('instruction_operations', 'id', oid, updates)
        return operation_status(store, oid)


def operation_summary(operation):
    keys = ('id', 'kind', 'proposal_id', 'state', 'attempts', 'error_code', 'error_detail', 'created_at', 'updated_at',
            'cancel_requested', 'controls_available', 'can_retry', 'can_cancel', 'result')
    return {k: operation[k] for k in keys} | {
        'destination': operation['record']['destination'],
        'prepared': 'delivery' in operation['checkpoint'],
        'failure_count': len(operation['failures']), 'control_count': len(operation['control_history']),
    }


def list_operations(store, *, limit=50, cursor=None, summary=False):
    import base64
    from datetime import datetime
    from .commands import _json
    require_operations(store)
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
            raise CommandError('InvalidCursor', 'Use the next_cursor returned by operation history.') from exc
        where = ' WHERE created_at<? OR (created_at=? AND id<?)'
        params = (decoded[0], decoded[0], decoded[1])
    rows = store.query('SELECT id,created_at FROM instruction_operations' + where + ' ORDER BY created_at DESC,id DESC LIMIT ?', (*params, limit + 1))
    page = rows[:limit]
    operations = [operation_status(store, row['id']) for row in page]
    more = len(rows) > limit
    next_cursor = base64.urlsafe_b64encode(_json([page[-1]['created_at'], page[-1]['id']]).encode()).decode().rstrip('=') if more else None
    return {'operations': [operation_summary(o) for o in operations] if summary else operations,
            'limit': limit, 'more_available': more, 'next_cursor': next_cursor}


def operation_inventory(store, limit=20):
    """Bounded status output with explicit coverage and no instruction content."""
    if not operations_available(store):
        return {'available': False, 'detail': 'Upgrade the state database to record instruction recovery.'}
    counts = {s: 0 for s in sorted(COMMAND_STATES)}
    for row in store.query('SELECT state, COUNT(*) AS n FROM instruction_operations GROUP BY state'):
        if row['state'] not in counts:
            raise CommandError('OperationDataError', f"Unknown instruction operation state {row['state']!r}.", 500)
        counts[row['state']] = row['n']
    rows = store.query("SELECT id FROM instruction_operations WHERE state NOT IN ('completed','cancelled') ORDER BY created_at,id LIMIT ?", (limit,))
    unfinished = []
    for row in rows:
        operation = operation_status(store, row['id'])
        unfinished.append({k: operation[k] for k in ('id','kind','proposal_id','state','attempts','error_code','error_detail')} | {
            'destination': operation['record']['destination'], 'reconcile_with': 'selfimprove worker --operation ' + operation['id']})
    total = sum(n for s, n in counts.items() if s not in {'completed','cancelled'})
    return {'available': True, 'counts': counts, 'unfinished': unfinished, 'shown': len(unfinished), 'remaining': total - len(unfinished)}
