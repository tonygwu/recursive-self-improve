"""The web records a reviewed inverse; only the independent worker writes it."""
from contextlib import closing
import sqlite3

import pytest

from self_improve import apply
from self_improve.dashboard.app import create_app
from self_improve.operations import operation_status
from self_improve.store import Store
from self_improve.worker import run_once
from tests.test_apply import cfg, store, run_git
from tests.test_inverse_rollback import BASE, A, propose

pytest.importorskip('fastapi')
from fastapi.testclient import TestClient


def request(client, pid, key='rollback-request-001'):
    response = client.get(f'/api/proposals/{pid}/rollback-preview')
    assert response.status_code == 200, response.text
    preview = response.json()
    assert preview['ready'], preview
    return {'action': 'rollback', 'proposal_id': pid, 'request_key': key,
            'preview_revision': preview['revision'], 'note': 'Undo this applied contribution.'}


def applied(store, cfg, tmp_path):
    target = tmp_path / 'rules.md'
    target.write_text(BASE)
    proposal = propose(store, target, BASE, A)
    apply.apply_proposal(store, cfg, proposal)
    return target, proposal


def test_api_records_intent_then_worker_delivers_and_reload_replays_it(cfg, store, tmp_path):
    target, p = applied(store, cfg, tmp_path)
    snapshot_head = run_git(['rev-parse', 'HEAD'], cfg.state_path('snapshots'))
    with TestClient(create_app(cfg)) as client:
        body = request(client, p['id'])
        response = client.post('/api/commands', json=body)
        assert response.status_code == 202, response.text
        queued = response.json()
        assert queued['state'] == 'queued' and queued['kind'] == 'rollback'
        assert target.read_text() == A
        assert run_git(['rev-parse', 'HEAD'], cfg.state_path('snapshots')) == snapshot_head
        assert store.query("SELECT * FROM proposal_events WHERE event='rolled_back'") == []
        assert run_once(store, cfg)['state'] == 'completed'
        assert target.read_text() == BASE
    with TestClient(create_app(cfg)) as client:
        status = client.get(queued['status_url'])
        assert status.status_code == 200 and status.json()['state'] == 'completed'
        replay = client.post('/api/commands', json=body)
        assert replay.status_code == 202 and replay.json()['id'] == queued['id']
        assert client.post('/api/commands', json={**body, 'note': 'Changed intent'}).status_code == 409
    assert len(store.query('SELECT * FROM instruction_requests')) == 1
    assert len(store.query("SELECT * FROM proposal_events WHERE event='rolled_back'")) == 1


def test_stale_preview_fails_before_queueing_any_operation(cfg, store, tmp_path):
    target, p = applied(store, cfg, tmp_path)
    with TestClient(create_app(cfg)) as client:
        body = request(client, p['id'])
        target.write_text(A + 'human addition\n')
        response = client.post('/api/commands', json=body)
        assert response.status_code == 409 and response.json()['error'] == 'StaleRevision'
    assert store.query("SELECT * FROM instruction_operations WHERE kind='rollback'") == []
    assert store.query('SELECT * FROM instruction_requests') == []


def test_a_new_preview_replaces_an_unwritten_stale_attempt_and_keeps_its_history(cfg, store, tmp_path):
    target, p = applied(store, cfg, tmp_path)
    with TestClient(create_app(cfg)) as client:
        first = client.post('/api/commands', json=request(client, p['id'])).json()
        target.write_text(A + 'human addition\n')
        blocked = run_once(store, cfg)
        assert blocked['state'] == 'blocked' and blocked['error_code'] == 'TargetChanged'
        assert 'delivery' not in blocked['checkpoint']
        second = client.post('/api/commands', json=request(client, p['id'], 'rollback-request-002'))
        assert second.status_code == 202, second.text
        assert second.json()['id'] != first['id']
        old = operation_status(store, first['id'])
        assert old['state'] == 'cancelled' and len(old['failures']) == 1
        assert old['result']['reason'] == 'superseded_preview'
        assert run_once(store, cfg)['state'] == 'completed'
    assert target.read_text() == BASE + 'human addition\n'


def test_a_prepared_write_cannot_be_replaced_by_another_preview(cfg, store, tmp_path, monkeypatch):
    target, p = applied(store, cfg, tmp_path)
    with TestClient(create_app(cfg)) as client:
        first = client.post('/api/commands', json=request(client, p['id'])).json()
        def crash(stage, *_):
            if stage == 'prepared':
                raise KeyboardInterrupt('process stopped after checkpoint')
        monkeypatch.setattr(apply, '_operation_checkpoint', crash)
        with pytest.raises(KeyboardInterrupt):
            run_once(store, cfg)
        target.write_text(A + 'human addition\n')
        response = client.post('/api/commands', json=request(client, p['id'], 'rollback-request-002'))
        assert response.status_code == 409 and response.json()['error'] == 'ReconciliationRequired'
        assert first['id'] in response.json()['detail']
    assert len(store.query("SELECT * FROM instruction_operations WHERE kind='rollback'")) == 1
    assert target.read_text() == A + 'human addition\n'


def test_serving_a_copy_queues_rollback_only_in_that_copy(cfg, store, tmp_path):
    target, p = applied(store, cfg, tmp_path)
    copy = tmp_path / 'copy.db'
    with closing(sqlite3.connect(copy)) as destination:
        store.conn.backup(destination)
    with TestClient(create_app(cfg, db_path=copy)) as client:
        response = client.post('/api/commands', json=request(client, p['id']))
        assert response.status_code == 202, response.text
        assert client.get(response.json()['status_url']).json()['state'] == 'queued'
    with closing(Store(copy, read_only=True)) as copied:
        assert len(copied.query('SELECT * FROM instruction_requests')) == 1
    assert store.query('SELECT * FROM instruction_requests') == []
    assert store.query("SELECT * FROM instruction_operations WHERE kind='rollback'") == []
    assert target.read_text() == A


@pytest.mark.parametrize('first', ['approve', 'rollback', 'cancel_delivery'])
def test_rollback_keys_share_the_approval_and_control_namespace(cfg, store, tmp_path, first):
    target, p = applied(store, cfg, tmp_path)
    other = propose(store, tmp_path / 'other.md', '', '- Other.\n', status='ungated')
    with TestClient(create_app(cfg)) as client:
        from tests.test_dashboard_commands import request as approval_request
        approval = approval_request(client, [other['id']], 'shared-request-001')
        inverse = request(client, p['id'], 'shared-request-001')
        if first == 'approve':
            assert client.post('/api/commands', json=approval).status_code == 202
            second = client.post('/api/commands', json=inverse)
        elif first == 'rollback':
            assert client.post('/api/commands', json=inverse).status_code == 202
            second = client.post('/api/commands', json=approval)
            assert client.post('/api/commands', json={'action': 'cancel_delivery', 'command_id': 'unused',
                'request_key': 'shared-request-001'}).status_code == 409
        else:
            command = client.post('/api/commands', json={**approval, 'request_key': 'separate-request-001'}).json()
            control = {'action': 'cancel_delivery', 'command_id': command['id'], 'request_key': 'shared-request-001'}
            assert client.post('/api/commands', json=control).status_code == 202
            second = client.post('/api/commands', json=inverse)
        assert second.status_code == 409 and second.json()['error'] == 'IdempotencyConflict'


def test_a_newer_application_with_identical_content_blocks_before_preparation(cfg, store, tmp_path):
    from self_improve.store import new_id, utc_now_iso
    target, p = applied(store, cfg, tmp_path)
    with TestClient(create_app(cfg)) as client:
        assert client.post('/api/commands', json=request(client, p['id'])).status_code == 202
    # Seed a subsequent application of the same bytes. Content alone cannot
    # distinguish the delivered revision that the operator selected.
    old = store.query_one("SELECT * FROM proposal_events WHERE event='applied'")
    store.insert('proposal_events', {**old, 'id': new_id(), 'ts': utc_now_iso()})
    store.commit()
    result = run_once(store, cfg)
    assert result['state'] == 'blocked' and result['error_code'] == 'ApplicationChanged'
    assert 'delivery' not in result['checkpoint']
    assert target.read_text() == A


@pytest.mark.parametrize('change', ['source_shape', 'after_content', 'presence'])
def test_malformed_inverse_records_have_the_same_named_error_in_api_and_worker(cfg, store, tmp_path, change):
    import json
    from self_improve.commands import CommandError, _hash
    target, p = applied(store, cfg, tmp_path)
    with TestClient(create_app(cfg)) as client:
        queued = client.post('/api/commands', json=request(client, p['id'])).json()
        record = queued['record']
        preview = record['rollback']
        if change == 'source_shape':
            preview['source']['before'] = ['not text']
            preview['source']['contribution_hash'] = _hash({k: preview['source'][k] for k in ('before', 'applied', 'before_exists')})
        elif change == 'presence':
            preview['after_exists'] = 1  # JSON number is not a boolean.
        else:
            preview['after_content'] = 'unapproved replacement\n'
        preview['revision'] = _hash({k: v for k, v in preview.items() if k != 'revision'})
        store.update('instruction_operations', 'id', queued['id'], {'record_json': json.dumps(record), 'record_hash': _hash(record)})
        store.commit()
        response = client.get(queued['status_url'])
        assert response.status_code == 500 and response.json()['error'] == 'OperationDataError'
        with pytest.raises(CommandError, match='Operation'):
            run_once(store, cfg)
    assert target.read_text() == A
