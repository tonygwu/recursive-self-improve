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
