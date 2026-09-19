"""Old approvals need new reviewed commands; their history is never authorization."""
from contextlib import closing
from dataclasses import replace

import pytest
pytest.importorskip('fastapi')
from fastapi.testclient import TestClient

from self_improve.dashboard.app import create_app
from self_improve.dashboard import queries
from self_improve.execution_policy import automatic_permission, waiting_proposals
from self_improve.store import Store, new_id
from self_improve.worker import run_once
from tests.test_delivery_worker import env, propose


def historical(store, target, **kwargs):
    proposal = propose(store, target, **kwargs)
    store.update('proposals', 'id', proposal['id'], {'status': 'approved_user'})
    store.insert('proposal_events', {'id': new_id(), 'proposal_id': proposal['id'],
        'ts': '2026-01-01T00:00:00Z', 'event': 'approved_user', 'actor': 'user',
        'note': 'Historical decision; no retained delivery command.'})
    store.commit()
    return store.query_one('SELECT * FROM proposals WHERE id=?', (proposal['id'],))


def preview_request(client, proposal, key='fresh-approval-001'):
    response = client.get('/api/review-preview', params={'proposal_ids': proposal['id']})
    assert response.status_code == 200, response.text
    preview = response.json()
    return {'action': 'approve', 'request_key': key, 'preview_revision': preview['revision'],
        'members': [{'proposal_id': m['proposal_id'], 'revision': m['revision']} for m in preview['members']]}


def test_historical_approval_is_reviewable_without_mutating_old_decision(env):
    cfg, store, target = env
    proposal = historical(store, target)
    before = list(store.conn.iterdump())
    with TestClient(create_app(cfg)) as client:
        response = client.get('/api/review-queue')
        assert response.status_code == 200, response.text
        queue = response.json()
        assert queue['count'] == 1
        shown = queue['families'][0]['proposals'][0]
        assert shown['id'] == proposal['id'] and shown['status'] == 'approved_user'
        assert shown['reason_code'] == 'fresh_approval_required'
        assert 'fresh' in shown['why_needs_you']
        overview = client.get('/api/overview').json()
        assert queries.inbox(store, cfg)['count'] == overview['inbox']['count'] == 1
    assert list(store.conn.iterdump()) == before
    assert not automatic_permission(store, replace(cfg, auto_apply=True), proposal)['allowed']
    assert run_once(store, cfg) is None
    assert target.read_text() == 'before\n'


@pytest.mark.parametrize('adapter', [False, True])
def test_fresh_preview_is_required_by_both_approval_routes(env, adapter):
    cfg, store, target = env
    proposal = historical(store, target)
    with TestClient(create_app(cfg)) as client:
        body = preview_request(client, proposal)
        body.pop('preview_revision')
        route = '/api/commands'
        if adapter:
            route = f"/api/proposals/{proposal['id']}/decision"
            body = {'decision': 'approve', 'revision': body['members'][0]['revision'],
                    'request_key': body['request_key']}
        before = list(store.conn.iterdump())
        response = client.post(route, json=body)
        assert response.status_code == 409, response.text
        assert response.json()['error'] == 'FreshReviewRequired'
        assert list(store.conn.iterdump()) == before
    assert target.read_text() == 'before\n'


@pytest.mark.parametrize('changed', ['target', 'source', 'applied_at', 'rejected'])
def test_changed_review_source_refuses_all_decisions_atomically(env, changed):
    cfg, store, target = env
    proposal = historical(store, target)
    with TestClient(create_app(cfg)) as client:
        body = preview_request(client, proposal)
        if changed == 'target':
            target.write_text('unrelated current human content\n')
        elif changed == 'source':
            store.update('learnings', 'id', proposal['learning_id'], {'rule_text': 'Changed lesson.'})
        elif changed == 'applied_at':
            store.update('proposals', 'id', proposal['id'], {'applied_at': '2026-01-02T00:00:00Z'})
        else:
            store.update('learnings', 'id', proposal['learning_id'], {'status': 'rejected'})
        store.commit()
        before = list(store.conn.iterdump())
        response = client.post('/api/commands', json=body)
        assert response.status_code == 409, response.text
        assert response.json()['error'] in {'StalePreview', 'StaleRevision', 'NotWaiting'}
        assert list(store.conn.iterdump()) == before
    assert not store.query('SELECT id FROM commands')


def test_current_preview_delivers_once_and_retains_original_approval(env):
    cfg, store, target = env
    proposal = historical(store, target)
    old_events = store.query('SELECT * FROM proposal_events ORDER BY id')
    with TestClient(create_app(cfg)) as client:
        body = preview_request(client, proposal)
        response = client.post('/api/commands', json=body)
        assert response.status_code == 202, response.text
        command = response.json()
        assert command['state'] == 'queued'
        assert client.get('/api/review-queue').json()['count'] == 0
        assert target.read_text() == 'before\n', 'the web process applied the file'
        duplicate = client.post('/api/commands', json=body)
        assert duplicate.json() == command
        concurrent = client.post('/api/commands', json={**body, 'request_key': 'different-approval-001'})
        assert concurrent.status_code == 409 and concurrent.json()['error'] == 'NotWaiting'
        assert run_once(store, cfg)['state'] == 'completed'
        assert run_once(store, cfg) is None
        assert client.get('/api/review-queue').json()['count'] == 0
    assert target.read_text() == 'before\nafter\n'
    assert len(store.query('SELECT id FROM commands')) == 1
    events = store.query('SELECT * FROM proposal_events ORDER BY id')
    assert all(event in events for event in old_events)
    assert len([event for event in events if event['event'] == 'approved_user']) == 2
    assert len([event for event in events if event['event'] == 'applied']) == 1
    assert not store.query('SELECT id FROM llm_calls')


def test_cancellation_returns_unwritten_fresh_approval_to_review(env):
    cfg, store, target = env
    proposal = historical(store, target)
    with TestClient(create_app(cfg)) as client:
        command = client.post('/api/commands', json=preview_request(client, proposal)).json()
        response = client.post('/api/commands', json={'action': 'cancel_delivery',
            'command_id': command['id'], 'request_key': 'cancel-fresh-001'})
        assert response.status_code == 202, response.text
        assert run_once(store, cfg)['state'] == 'cancelled'
        queue = client.get('/api/review-queue').json()
        assert queue['count'] == 1
        assert queue['families'][0]['proposals'][0]['status'] == 'pending'
    assert target.read_text() == 'before\n'
    assert len(store.query("SELECT id FROM proposal_events WHERE event='approved_user'")) == 2


@pytest.mark.parametrize('field', ['applied_at', 'snapshot_commit_before', 'snapshot_commit_after'])
def test_application_fields_exclude_historical_candidate(env, field):
    cfg, store, target = env
    proposal = historical(store, target)
    store.update('proposals', 'id', proposal['id'], {field: 'retained-application-evidence'})
    store.commit()
    assert not waiting_proposals(store, cfg)


@pytest.mark.parametrize('event', ['applied', 'rolled_back', 'rejected_user'])
def test_application_or_rejection_event_excludes_historical_candidate(env, event):
    cfg, store, target = env
    proposal = historical(store, target)
    store.insert('proposal_events', {'id': new_id(), 'proposal_id': proposal['id'],
        'ts': '2026-01-02T00:00:00Z', 'event': event, 'actor': 'user' if event != 'applied' else 'auto'})
    store.commit()
    assert not waiting_proposals(store, cfg)


@pytest.mark.parametrize('state', ['queued', 'running', 'blocked', 'failed', 'cancelled', 'completed'])
def test_an_existing_command_keeps_its_recovery_owner(env, state):
    from tests.test_delivery_worker import approve
    cfg, store, target = env
    proposal = propose(store, target)
    command = approve(store, cfg, proposal)
    store.update('commands', 'id', command['id'], {'state': state})
    store.commit()
    assert not waiting_proposals(store, cfg)


def test_interrupted_automatic_operation_is_not_historical_approval(env, monkeypatch):
    from self_improve import apply
    from self_improve.execution_policy import set_class_policy
    cfg, store, target = env
    set_class_policy(store, 'global', True)
    proposal = propose(store, target)
    store.update('proposals', 'id', proposal['id'], {'status': 'gated_pass'})
    store.commit()
    proposal = store.query_one('SELECT * FROM proposals WHERE id=?', (proposal['id'],))
    def stop(point, *_):
        if point == 'prepared':
            raise KeyboardInterrupt('fixture interruption before file mutation')
    monkeypatch.setattr(apply, '_operation_checkpoint', stop)
    with pytest.raises(KeyboardInterrupt):
        apply.apply_proposal(store, cfg, proposal)
    assert len(store.query('SELECT id FROM instruction_operations')) == 1
    store.update('proposals', 'id', proposal['id'], {'status': 'approved_user'})
    store.commit()
    assert not waiting_proposals(store, cfg)
    assert target.read_text() == 'before\n'


def test_target_rejection_of_historical_approval_preserves_other_targets(env, tmp_path):
    cfg, store, target = env
    proposal = historical(store, target)
    other = tmp_path / 'other.md'; other.write_text('before\n')
    second = historical(store, other)
    store.update('proposals', 'id', second['id'], {'learning_id': proposal['learning_id']})
    store.commit()
    with TestClient(create_app(cfg)) as client:
        body = preview_request(client, proposal)
        body.pop('preview_revision'); body['action'] = 'reject_target'
        response = client.post('/api/commands', json=body)
        assert response.status_code == 202, response.text
        ids = {p['id'] for f in client.get('/api/review-queue').json()['families'] for p in f['proposals']}
        assert ids == {second['id']}
    assert len(store.query("SELECT id FROM proposal_events WHERE event='approved_user'")) == 2
    assert target.read_text() == other.read_text() == 'before\n'


def test_old_schema_review_membership_does_not_migrate(env, tmp_path, monkeypatch):
    from self_improve import store as module
    from self_improve.execution_policy import historical_approval_ids
    cfg, _, target = env
    migrations = module.MIGRATIONS
    with monkeypatch.context() as patch:
        patch.setattr(module, 'MIGRATIONS', migrations[:9])
        with closing(Store(tmp_path / 'older.db')) as old:
            proposal = historical(old, target)
    with closing(Store(tmp_path / 'older.db', read_only=True)) as reader:
        before = list(reader.conn.iterdump())
        assert historical_approval_ids(reader) == {proposal['id']}
        assert [p['id'] for p in waiting_proposals(reader, cfg)] == [proposal['id']]
        assert list(reader.conn.iterdump()) == before
