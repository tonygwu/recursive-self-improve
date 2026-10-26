"""Instruction-operation controls record intent and reconcile possible writes."""
import json

import pytest

from self_improve import apply
from self_improve.commands import CommandError, submit_command
from self_improve.operations import operation_status
from self_improve.rollback import rollback_preview
from self_improve.worker import run_once
from tests.test_apply import cfg, store
from tests.test_inverse_rollback import BASE, A, propose


def inverse(store, cfg, tmp_path):
    target = tmp_path / 'rules.md'
    target.write_text(BASE)
    p = propose(store, target, BASE, A)
    apply.apply_proposal(store, cfg, p)
    preview = rollback_preview(store, cfg, p['id'])
    queued = submit_command(store, cfg, {'action': 'rollback', 'proposal_id': p['id'],
        'request_key': 'initial-rollback-request', 'preview_revision': preview['revision']})
    return target, p, queued


def control(store, cfg, operation, action, key='operation-control-request'):
    return submit_command(store, cfg, {'action': action, 'operation_id': operation['id'], 'request_key': key})


def test_cancelling_a_queued_inverse_records_intent_and_never_prepares_a_write(cfg, store, tmp_path):
    target, p, queued = inverse(store, cfg, tmp_path)
    requested = control(store, cfg, queued, 'cancel_operation')
    assert requested['state'] == 'queued' and requested['cancel_requested'] is True
    assert requested['can_cancel'] is False
    assert target.read_text() == A
    result = run_once(store, cfg)
    assert result['state'] == 'cancelled'
    assert 'delivery' not in result['checkpoint']
    assert target.read_text() == A
    assert store.query_one('SELECT status FROM proposals WHERE id=?', (p['id'],))['status'] == 'applied'
    assert control(store, cfg, queued, 'cancel_operation')['id'] == result['id']
    assert len(result['control_history']) == 1
    with pytest.raises(CommandError, match='key'):
        control(store, cfg, queued, 'retry_operation')


@pytest.mark.parametrize('stage,expected,content', [('prepared', 'cancelled', A), ('file_replaced', 'completed', BASE)])
def test_cancellation_reconciles_prepared_and_delivered_inverse_states(cfg, store, tmp_path, monkeypatch, stage, expected, content):
    target, p, queued = inverse(store, cfg, tmp_path)
    def crash(point, *_):
        if point == stage:
            raise KeyboardInterrupt('interrupted inverse')
    monkeypatch.setattr(apply, '_operation_checkpoint', crash)
    with pytest.raises(KeyboardInterrupt):
        run_once(store, cfg)
    control(store, cfg, queued, 'cancel_operation')
    monkeypatch.setattr(apply, '_operation_checkpoint', lambda *_: None)
    result = run_once(store, cfg)
    assert result['state'] == expected and result['cancel_requested'] is True
    assert target.read_text() == content
    events = store.query("SELECT * FROM proposal_events WHERE event='rolled_back'")
    assert len(events) == (1 if expected == 'completed' else 0)
    assert run_once(store, cfg) is None


def test_retrying_a_failed_cancellation_preserves_intent_and_attempt_history(cfg, store, tmp_path, monkeypatch):
    target, _, queued = inverse(store, cfg, tmp_path)
    def crash(stage, *_):
        if stage == 'prepared':
            raise KeyboardInterrupt('prepared')
    monkeypatch.setattr(apply, '_operation_checkpoint', crash)
    with pytest.raises(KeyboardInterrupt):
        run_once(store, cfg)
    monkeypatch.setattr(apply, '_operation_checkpoint', lambda *_: None)
    target.write_text('unknown human content\n')
    control(store, cfg, queued, 'cancel_operation')
    blocked = run_once(store, cfg)
    assert blocked['state'] == 'blocked' and blocked['error_code'] == 'TargetChanged'
    assert blocked['can_retry'] is True and blocked['can_cancel'] is False
    assert run_once(store, cfg) is None
    target.write_text(A)
    requested = control(store, cfg, queued, 'retry_operation', 'retry-operation-request')
    assert requested['cancel_requested'] is True and len(requested['failures']) == 1
    result = run_once(store, cfg)
    assert result['state'] == 'cancelled' and target.read_text() == A
    assert len(result['failures']) == 1 and len(result['control_history']) == 2


@pytest.mark.parametrize('stage,expected', [('prepared', 'cancelled'), ('file_replaced', 'completed')])
def test_automatic_operation_cancellation_acknowledges_any_prior_write(cfg, store, tmp_path, monkeypatch, stage, expected):
    target = tmp_path / 'rules.md'
    target.write_text(BASE)
    p = propose(store, target, BASE, A)
    def crash(point, *_):
        if point == stage:
            raise KeyboardInterrupt('interrupted automatic delivery')
    monkeypatch.setattr(apply, '_operation_checkpoint', crash)
    with pytest.raises(KeyboardInterrupt):
        apply.apply_proposal(store, cfg, p)
    operation = operation_status(store, store.query_one('SELECT id FROM instruction_operations')['id'])
    control(store, cfg, operation, 'cancel_operation')
    monkeypatch.setattr(apply, '_operation_checkpoint', lambda *_: None)
    result = run_once(store, cfg)
    assert result['state'] == expected
    assert target.read_text() == (A if expected == 'completed' else BASE)
    assert store.query_one('SELECT status FROM proposals WHERE id=?', (p['id'],))['status'] == ('applied' if expected == 'completed' else 'held')


def test_operation_history_is_paginated_and_summary_omits_retained_instruction_content(cfg, store, tmp_path):
    from self_improve.operations import list_operations
    _, _, queued = inverse(store, cfg, tmp_path)
    first = list_operations(store, limit=1, summary=True)
    assert len(first['operations']) == 1 and first['next_cursor']
    second = list_operations(store, limit=1, cursor=first['next_cursor'], summary=True)
    assert len(second['operations']) == 1 and second['next_cursor'] is None
    assert {p['id'] for page in (first, second) for p in page['operations']} == {r['id'] for r in store.query('SELECT id FROM instruction_operations')}
    assert 'record' not in first['operations'][0] and 'checkpoint' not in first['operations'][0]
    assert A not in json.dumps(first)
    assert operation_status(store, queued['id'])['record']['rollback']['source']['applied'] == A
    with pytest.raises(CommandError, match='cursor'):
        list_operations(store, cursor='invalid-cursor')


def test_control_request_keys_cannot_reuse_a_rollback_intent(cfg, store, tmp_path):
    _, _, queued = inverse(store, cfg, tmp_path)
    with pytest.raises(CommandError, match='key'):
        control(store, cfg, queued, 'cancel_operation', 'initial-rollback-request')


def test_a_new_cancel_request_during_a_failed_attempt_remains_claimable(cfg, store, tmp_path, monkeypatch):
    target, _, queued = inverse(store, cfg, tmp_path)
    def fail(stage, *_):
        if stage == 'prepared':
            control(store, cfg, queued, 'cancel_operation')
            raise OSError('failure after the operator requested cancellation')
    monkeypatch.setattr(apply, '_operation_checkpoint', fail)
    result = run_once(store, cfg)
    assert result['state'] == 'queued' and result['cancel_requested']
    assert len(result['failures']) == 1
    monkeypatch.setattr(apply, '_operation_checkpoint', lambda *_: None)
    assert run_once(store, cfg)['state'] == 'cancelled'
    assert target.read_text() == A


def test_cancelling_an_unwritten_preparation_preserves_a_later_application(cfg, store, tmp_path, monkeypatch):
    from self_improve.store import new_id, utc_now_iso
    target, _, queued = inverse(store, cfg, tmp_path)
    def crash(stage, *_):
        if stage == 'prepared':
            raise KeyboardInterrupt('prepared')
    monkeypatch.setattr(apply, '_operation_checkpoint', crash)
    with pytest.raises(KeyboardInterrupt):
        run_once(store, cfg)
    old = store.query_one("SELECT * FROM proposal_events WHERE event='applied'")
    store.insert('proposal_events', {**old, 'id': new_id(), 'ts': utc_now_iso()})
    store.commit()
    control(store, cfg, queued, 'cancel_operation')
    monkeypatch.setattr(apply, '_operation_checkpoint', lambda *_: None)
    assert run_once(store, cfg)['state'] == 'cancelled'
    assert target.read_text() == A


def test_web_operation_controls_and_history_use_the_selected_database_copy(cfg, store, tmp_path):
    import sqlite3
    from contextlib import closing
    pytest.importorskip('fastapi')
    from fastapi.testclient import TestClient
    from self_improve.dashboard.app import create_app
    from self_improve.store import Store
    target, _, queued = inverse(store, cfg, tmp_path)
    copied = tmp_path / 'copy.db'
    with closing(sqlite3.connect(copied)) as destination:
        store.conn.backup(destination)
    with TestClient(create_app(cfg, db_path=copied)) as client:
        response = client.post('/api/commands', json={'action': 'cancel_operation', 'operation_id': queued['id'], 'request_key': 'copy-cancel-request'})
        assert response.status_code == 202, response.text
        assert response.json()['cancel_requested'] is True
        status = client.get('/api/operations/' + queued['id'] + '?summary=true')
        assert status.status_code == 200 and status.json()['cancel_requested'] is True
        history = client.get('/api/operations?summary=true&limit=1')
        assert history.status_code == 200 and history.json()['next_cursor']
    assert operation_status(store, queued['id'])['cancel_requested'] is False
    with closing(Store(copied, read_only=True)) as copy:
        assert len(operation_status(copy, queued['id'])['control_history']) == 1
    assert target.read_text() == A


def test_rollback_http_view_recovers_queued_and_completed_progress_without_changing_the_preview(cfg, store, tmp_path):
    pytest.importorskip('fastapi')
    from fastapi.testclient import TestClient
    from self_improve.dashboard.app import create_app
    target, p, queued = inverse(store, cfg, tmp_path)
    with TestClient(create_app(cfg)) as client:
        view = client.get('/api/proposals/' + p['id'] + '/rollback-preview').json()
        assert view['revision'] == queued['record']['rollback']['revision']
        assert view['active_operation']['id'] == queued['id']
        assert view['active_operation']['state'] == 'queued'
        assert run_once(store, cfg)['state'] == 'completed'
        done = client.get('/api/proposals/' + p['id'] + '/rollback-preview').json()
        assert done['error_code'] == 'AlreadyRolledBack' and done['ready'] is False
        assert done['active_operation']['id'] == queued['id']
        assert done['active_operation']['state'] == 'completed'
    assert target.read_text() == BASE


def test_a_corrupt_cancellation_flag_or_control_record_is_refused_by_name(cfg, store, tmp_path):
    """BL-51: the cancellation-flag and control-history guards had no test."""
    target, _, queued = inverse(store, cfg, tmp_path)
    control(store, cfg, queued, 'cancel_operation')
    row = store.query_one('SELECT c.request_key, c.before_json FROM instruction_operation_controls c '
                          'JOIN instruction_requests r ON r.request_key=c.request_key WHERE r.operation_id=?',
                          (queued['id'],))
    before = json.loads(row['before_json'])
    before['cancel_requested'] = 'yes'
    store.conn.execute('UPDATE instruction_operation_controls SET before_json=? WHERE request_key=?',
                       (json.dumps(before), row['request_key']))
    store.commit()
    with pytest.raises(CommandError, match='invalid control or prior result'):
        operation_status(store, queued['id'])
    # The worker pass that would reconcile the cancellation stops on the same
    # refusal: the operation stays queued and the applied content stays.
    with pytest.raises(CommandError, match='invalid control or prior result'):
        run_once(store, cfg)
    store.conn.execute('UPDATE instruction_operations SET cancel_requested=2 WHERE id=?', (queued['id'],))
    store.commit()
    with pytest.raises(CommandError, match='invalid cancellation flag'):
        operation_status(store, queued['id'])
    with pytest.raises(CommandError, match='invalid cancellation flag'):
        run_once(store, cfg)
    assert store.query_one('SELECT state FROM instruction_operations WHERE id=?', (queued['id'],))['state'] == 'queued'
    assert target.read_text() == A


def test_operation_controls_refuse_by_name_and_record_nothing(cfg, store, tmp_path):
    """Operation-control validation had no tests: a retry of a queued
    operation, a second cancellation, a short key, an extra field and an
    empty operation ID are each refused, and none adds a control record."""
    target, p, queued = inverse(store, cfg, tmp_path)

    def refused(body, code, status):
        before = store.query('SELECT * FROM instruction_operation_controls')
        with pytest.raises(CommandError) as error:
            submit_command(store, cfg, body)
        assert (error.value.code, error.value.status_code) == (code, status), error.value
        assert store.query('SELECT * FROM instruction_operation_controls') == before

    base = {'action': 'retry_operation', 'operation_id': queued['id'], 'request_key': 'control-request-1'}
    refused(base, 'NotRetryable', 409)
    refused({**base, 'request_key': 'short'}, 'InvalidRequestKey', 400)
    refused({**base, 'extra': True}, 'InvalidCommand', 400)
    refused({**base, 'operation_id': ''}, 'InvalidCommand', 400)
    control(store, cfg, queued, 'cancel_operation')
    refused({**base, 'action': 'cancel_operation', 'request_key': 'control-request-2'}, 'NotCancellable', 409)
    assert target.read_text() == A


# ---------------------------------------------------------------------------
# BL-51: guards that stop a corrupt but internally consistent operation record
# from authorizing a write. Each case re-signs the record so the fingerprint
# check passes and the named guard is what refuses it.
# ---------------------------------------------------------------------------

def _recorded_auto_operation(tmp_path, monkeypatch):
    from contextlib import closing
    from self_improve import apply
    from self_improve.config import Config
    from self_improve.execution_policy import set_class_policy
    from self_improve.store import Store
    from tests.test_apply import OLD, NEW, insert_proposal, make_diff

    cfg = Config(state_dir=str(tmp_path / "state"))
    store = Store(tmp_path / "state" / "state.db")
    set_class_policy(store, "global", True)
    target = tmp_path / "CLAUDE.md"
    target.write_text(OLD)
    proposal = insert_proposal(store, target=target, diff=make_diff(OLD, NEW))

    def crash(point, *_):
        if point == "prepared":
            raise KeyboardInterrupt("stop after the operation is recorded")
    monkeypatch.setattr(apply, "_operation_checkpoint", crash)
    with pytest.raises(KeyboardInterrupt):
        apply.apply_proposal(store, cfg, proposal)
    monkeypatch.setattr(apply, "_operation_checkpoint", lambda *_: None)
    row = store.query_one("SELECT * FROM instruction_operations")
    assert row["state"] == "running" and "delivery" in json.loads(row["checkpoint_json"])
    return store, row, cfg, target


def _refused_by_name(err, message):
    """The named guard refused, not an earlier or later check with other words."""
    assert err.value.code == "OperationDataError", err.value
    assert str(err.value).endswith(": " + message), str(err.value)


def _resign(store, row, mutate):
    from self_improve.commands import _hash
    record = json.loads(row["record_json"])
    mutate(record)
    store.conn.execute("UPDATE instruction_operations SET record_json=?, record_hash=? WHERE id=?",
                       (json.dumps(record), _hash(record), row["id"]))
    store.commit()


@pytest.mark.parametrize("mutate,message", [
    (lambda r: r["destination"].update(target_path="/elsewhere/CLAUDE.md"), "destination differs from the authorized proposal"),
    (lambda r: r["destination"].update(mode="network"), "destination differs from the authorized proposal"),
    (lambda r: r["authorization"].update(actor="user"), "invalid automatic policy authorization"),
    (lambda r: r["authorization"].update(target_class="hook"), "invalid automatic policy authorization"),
    (lambda r: r["proposal"].update(status="ungated"), "invalid automatic policy authorization"),
    (lambda r: r["proposal"].update(id="another-proposal"), "operation does not identify its proposal"),
    (lambda r: r.update(version=99), "unknown operation kind or version"),
])
def test_a_corrupt_operation_record_is_refused_by_name(tmp_path, monkeypatch, mutate, message):
    """The reader refuses by name, and the worker pass that would publish the
    prepared write stops on the same refusal with the target unchanged."""
    from self_improve.commands import CommandError
    from self_improve.operations import operation_status
    from tests.test_apply import OLD

    store, row, cfg, target = _recorded_auto_operation(tmp_path, monkeypatch)
    try:
        operation_status(store, row["id"])  # the unmodified record validates
        _resign(store, row, mutate)
        with pytest.raises(CommandError) as err:
            operation_status(store, row["id"])
        _refused_by_name(err, message)
        with pytest.raises(CommandError) as err:
            run_once(store, cfg)
        _refused_by_name(err, message)
        assert target.read_text() == OLD
        assert store.query_one("SELECT state FROM instruction_operations WHERE id=?", (row["id"],))["state"] == "running"
        assert store.query("SELECT * FROM proposal_events WHERE event='applied'") == []
    finally:
        store.close()


def _resign_rollback(store, operation_id, mutate):
    from self_improve.commands import _hash
    row = store.query_one("SELECT * FROM instruction_operations WHERE id=?", (operation_id,))
    record = json.loads(row["record_json"])
    mutate(record, _hash)
    store.conn.execute("UPDATE instruction_operations SET record_json=?, record_hash=? WHERE id=?",
                       (json.dumps(record), _hash(record), operation_id))
    store.commit()


def _repreview(record, _hash, change):
    change(record["rollback"])
    record["rollback"]["revision"] = _hash({k: v for k, v in record["rollback"].items() if k != "revision"})


@pytest.mark.parametrize("mutate,message", [
    (lambda r, h: r["authorization"].update(actor="robot"), "invalid rollback authorization"),
    # Recompute the preview revision, so the ready flag is the only change and
    # the revision-hash check cannot refuse this record first.
    (lambda r, h: _repreview(r, h, lambda p: p.update(ready=False)), "invalid rollback authorization"),
    (lambda r, h: _repreview(r, h, lambda p: p["source"].update(affected_members=[])),
     "invalid rollback application membership"),
    (lambda r, h: _repreview(r, h, lambda p: p.update(after_content="tampered\n")),
     "rollback preview does not invert the applied contribution"),
    (lambda r, h: r["rollback"]["source"].update(before_exists="yes"), "invalid inverse source or preview shape"),
])
def test_a_corrupt_rollback_operation_is_refused_by_name(cfg, store, tmp_path, mutate, message):
    """BL-51: the rollback authorization, membership and inversion guards had no
    test. The worker pass that would execute the queued inverse stops on the
    same refusal and leaves the applied content in place."""
    target, _, queued = inverse(store, cfg, tmp_path)
    operation_status(store, queued["id"])  # the unmodified record validates
    _resign_rollback(store, queued["id"], mutate)
    with pytest.raises(CommandError) as err:
        operation_status(store, queued["id"])
    _refused_by_name(err, message)
    with pytest.raises(CommandError) as err:
        run_once(store, cfg)
    _refused_by_name(err, message)
    assert target.read_text() == A
    assert store.query("SELECT * FROM proposal_events WHERE event='rolled_back'") == []


def test_a_prepared_rollback_whose_delivery_differs_from_its_preview_is_refused(cfg, store, tmp_path, monkeypatch):
    """Review fix (BL-51): the prepared-delivery comparison had no test. The
    preview is re-signed with a different base, so every earlier check passes
    and only the comparison with the prepared delivery can refuse it."""
    target, _, queued = inverse(store, cfg, tmp_path)

    def crash(stage, *_):
        if stage == 'prepared':
            raise KeyboardInterrupt('stop after the inverse is prepared')
    monkeypatch.setattr(apply, '_operation_checkpoint', crash)
    with pytest.raises(KeyboardInterrupt):
        run_once(store, cfg)
    monkeypatch.setattr(apply, '_operation_checkpoint', lambda *_: None)
    prepared = operation_status(store, queued["id"])
    assert prepared["state"] == "running" and "delivery" in prepared["checkpoint"]
    _resign_rollback(store, queued["id"], lambda r, h: _repreview(
        r, h, lambda p: p["base"].update(exists=not p["base"]["exists"])))
    message = "rollback delivery differs from the preview"
    with pytest.raises(CommandError) as err:
        operation_status(store, queued["id"])
    _refused_by_name(err, message)
    with pytest.raises(CommandError) as err:
        run_once(store, cfg)
    _refused_by_name(err, message)
    assert target.read_text() == A


def test_a_completed_rollback_without_a_prepared_delivery_is_refused_by_name(cfg, store, tmp_path):
    """Review fix (BL-51): a terminal inverse that never prepared a delivery
    must be refused by name. Without that guard the reader fails later on a
    missing result key, with no named cause."""
    _, _, queued = inverse(store, cfg, tmp_path)
    store.conn.execute("UPDATE instruction_operations SET state='completed' WHERE id=?", (queued["id"],))
    store.commit()
    with pytest.raises(CommandError) as err:
        operation_status(store, queued["id"])
    _refused_by_name(err, "terminal rollback has no prepared delivery")


VALID_FAILURE = {"attempt": 1, "state": "blocked", "at": "2026-10-17T00:00:00Z",
                 "error_code": "TargetChanged", "error_detail": ""}


@pytest.mark.parametrize("failures,attempts,message", [
    ({"not": "a list"}, 1, "invalid failure history"),
    ([VALID_FAILURE], 0, "invalid failure history entry"),  # attempt above the count
    ([{**VALID_FAILURE, "attempt": 0}], 1, "invalid failure history entry"),
    ([{**VALID_FAILURE, "state": "completed"}], 1, "invalid failure history entry"),
    ([{**VALID_FAILURE, "at": ""}], 1, "invalid failure history entry"),
    ([{**VALID_FAILURE, "error_detail": None}], 1, "invalid failure history entry"),
    (["not an entry"], 1, "invalid failure history entry"),
])
def test_a_corrupt_failure_history_is_refused_by_name(cfg, store, tmp_path, failures, attempts, message):
    """Review fix (BL-51): the entry guard had no test, and the old match
    'failure history|failures' accepted any refusal that mentioned failures."""
    target, _, queued = inverse(store, cfg, tmp_path)
    update = "UPDATE instruction_operations SET failures_json=?, attempts=? WHERE id=?"
    store.conn.execute(update, (json.dumps([VALID_FAILURE]), 1, queued["id"]))
    store.commit()
    assert operation_status(store, queued["id"])["failures"] == [VALID_FAILURE]
    store.conn.execute(update, (json.dumps(failures), attempts, queued["id"]))
    store.commit()
    with pytest.raises(CommandError) as err:
        operation_status(store, queued["id"])
    _refused_by_name(err, message)
    with pytest.raises(CommandError) as err:
        run_once(store, cfg)
    _refused_by_name(err, message)
    assert target.read_text() == A


@pytest.mark.parametrize("mutate,message", [
    # operations.py about :58. The worker compares these fields with the
    # current proposal, so a non-text value must be refused before that.
    (lambda r: r["proposal"].update(learning_id=None), "invalid source proposal"),
    (lambda r: r["proposal"].update(eval_result_id=7), "invalid source proposal"),
    # operations.py about :61. A file-mode delivery never reads these fields,
    # so nothing after the guard notices a missing branch or repository root.
    (lambda r: r["destination"].update(branch_name=None), "invalid destination shape"),
    (lambda r: r["destination"].update(repo_root=0), "invalid destination shape"),
])
def test_a_non_text_source_or_destination_field_is_refused_by_name(tmp_path, monkeypatch, mutate, message):
    """Review of BL-51: with either shape guard replaced by `pass`, the
    operation-control and rollback tests still passed. The reader refuses by
    name, and the worker pass that would publish the prepared write stops on
    the same refusal with the target unchanged and the operation running."""
    from tests.test_apply import OLD

    store, row, cfg, target = _recorded_auto_operation(tmp_path, monkeypatch)
    try:
        operation_status(store, row["id"])  # the unmodified record validates
        _resign(store, row, mutate)
        with pytest.raises(CommandError) as err:
            operation_status(store, row["id"])
        _refused_by_name(err, message)
        with pytest.raises(CommandError) as err:
            run_once(store, cfg)
        _refused_by_name(err, message)
        assert target.read_text() == OLD
        assert store.query_one("SELECT state FROM instruction_operations WHERE id=?", (row["id"],))["state"] == "running"
        assert store.query("SELECT * FROM proposal_events WHERE event='applied'") == []
    finally:
        store.close()
