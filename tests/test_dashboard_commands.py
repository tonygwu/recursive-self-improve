"""Durable commands must authorize exactly the revisions the user reviewed."""
from contextlib import closing
import json

import pytest

from self_improve.config import Config
from self_improve.dashboard.app import create_app
from self_improve.store import Store
from tests.test_apply import OLD, NEW, insert_proposal, make_diff

pytest.importorskip('fastapi')
from fastapi.testclient import TestClient


@pytest.fixture
def env(tmp_path):
    cfg = Config(state_dir=str(tmp_path / 'state'))
    with closing(Store(cfg.state_path('state.db'))) as store:
        target = tmp_path / 'rules.md'
        target.write_text(OLD)
        one = insert_proposal(store, target=target, diff=make_diff(OLD, NEW), status='ungated')
        two = insert_proposal(store, target=tmp_path / 'other.md', diff=make_diff(OLD, NEW), status='pending')
        store.update('proposals', 'id', two['id'], {'learning_id': one['learning_id']})
        store.commit()
        yield cfg, store, one, two


def request(client, ids, key='test-request-001'):
    members = []
    for pid in ids:
        response = client.get(f'/api/proposals/{pid}/review')
        assert response.status_code == 200, response.text
        members.append({'proposal_id': pid, 'revision': response.json()['revision']})
    return {'request_key': key, 'action': 'approve', 'members': members, 'note': 'Reviewed all selected changes.'}


def test_atomic_approval_freezes_every_selected_revision_and_survives_restart(env):
    cfg, store, one, two = env
    with TestClient(create_app(cfg)) as client:
        body = request(client, [one['id'], two['id']])
        response = client.post('/api/commands', json=body)
        assert response.status_code == 202, response.text
        cmd = response.json()
        assert cmd['state'] == 'queued'
        assert len(cmd['members']) == len(cmd['targets']) == 2
        assert {m['revision'] for m in cmd['members']} == {m['revision'] for m in body['members']}
    with TestClient(create_app(cfg)) as client:
        saved = client.get(f"/api/commands/{cmd['id']}").json()
        assert saved == cmd
    assert {p['status'] for p in store.query('SELECT status FROM proposals')} == {'approved_user'}
    assert len(store.query('SELECT * FROM proposal_events WHERE event=?', ('approved_user',))) == 2
    frozen = store.query('SELECT snapshot_json FROM proposal_revisions')
    assert len(frozen) == 2
    assert all(json.loads(r['snapshot_json'])['proposal']['diff_unified'] == one['diff_unified'] for r in frozen)
    assert not cfg.state_path('snapshots').exists(), 'the dashboard called the file writer'


def test_one_stale_member_refuses_the_whole_family_without_partial_decisions(env):
    cfg, store, one, two = env
    with TestClient(create_app(cfg)) as client:
        body = request(client, [one['id'], two['id']])
        store.update('proposals', 'id', two['id'], {'diff_unified': make_diff(OLD, NEW + 'later\n')})
        store.commit()
        response = client.post('/api/commands', json=body)
    assert response.status_code == 409, response.text
    assert response.json()['error'] == 'StaleRevision'
    assert store.query('SELECT * FROM commands') == []
    assert store.query('SELECT * FROM proposal_revisions') == []
    assert store.query('SELECT * FROM proposal_events WHERE actor=?', ('user',)) == []
    assert {p['status'] for p in store.query('SELECT status FROM proposals')} == {'ungated', 'pending'}


def test_retry_is_idempotent_and_cannot_change_its_authorization(env):
    cfg, store, one, two = env
    with TestClient(create_app(cfg)) as client:
        body = request(client, [one['id'], two['id']])
        first = client.post('/api/commands', json=body)
        second = client.post('/api/commands', json={**body, 'members': list(reversed(body['members']))})
        assert first.status_code == second.status_code == 202
        assert first.json() == second.json()
        changed = client.post('/api/commands', json={**body, 'members': body['members'][:1]})
        assert changed.status_code == 409
        assert changed.json()['error'] == 'IdempotencyConflict'
    assert len(store.query('SELECT * FROM commands')) == 1
    assert len(store.query('SELECT * FROM command_members')) == 2


def test_a_later_family_member_is_not_silently_approved(env, tmp_path):
    cfg, store, one, two = env
    with TestClient(create_app(cfg)) as client:
        body = request(client, [one['id'], two['id']])
        later = insert_proposal(store, target=tmp_path / 'later.md', diff=one['diff_unified'], status='pending')
        store.update('proposals', 'id', later['id'], {'learning_id': one['learning_id']})
        store.commit()
        response = client.post('/api/commands', json=body)
        assert response.status_code == 202, response.text
        waiting = client.get('/api/review-queue').json()
    assert {p['id'] for f in waiting['families'] for p in f['proposals']} == {later['id']}


def test_changed_rule_explanation_also_invalidates_the_review(env):
    cfg, store, one, _ = env
    with TestClient(create_app(cfg)) as client:
        body = request(client, [one['id']])
        store.update('learnings', 'id', one['learning_id'], {'why': 'Different justification'})
        store.commit()
        assert client.post('/api/commands', json=body).status_code == 409
    assert store.query('SELECT * FROM commands') == []


@pytest.mark.parametrize('same_diff', [False, True])
def test_same_file_members_collapse_only_when_their_edits_agree(env, same_diff):
    cfg, store, one, two = env
    store.update('proposals', 'id', two['id'], {
        'target_path': one['target_path'],
        'diff_unified': one['diff_unified'] if same_diff else make_diff(OLD, NEW + 'conflict\n'),
    })
    store.commit()
    with TestClient(create_app(cfg)) as client:
        body = request(client, [one['id'], two['id']])
        response = client.post('/api/commands', json=body)
    if same_diff:
        assert response.status_code == 202, response.text
        assert len(response.json()['targets']) == 1
        assert len(response.json()['members']) == 2
    else:
        assert response.status_code == 409, response.text
        assert response.json()['error'] == 'ConflictingEdits'
        assert store.query('SELECT * FROM commands') == []


@pytest.mark.parametrize('extra', [{'target_path':'/arbitrary'}, {'shell':'echo unsafe'}, {'members':[]}])
def test_untyped_or_empty_requests_write_nothing(env, extra):
    cfg, store, one, _ = env
    with TestClient(create_app(cfg)) as client:
        body = request(client, [one['id']])
        response = client.post('/api/commands', json={**body, **extra})
    assert response.status_code == 400, response.text
    assert store.query('SELECT * FROM commands') == []


def test_concurrent_retries_create_one_command(env):
    from concurrent.futures import ThreadPoolExecutor
    from self_improve.commands import submit_command

    cfg, store, one, two = env
    with TestClient(create_app(cfg)) as client:
        body = request(client, [one['id'], two['id']])
    def submit(_):
        with closing(Store(cfg.state_path('state.db'), migrate=False)) as writer:
            return submit_command(writer, cfg, body)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, range(2)))
    assert results[0] == results[1]
    assert len(store.query('SELECT * FROM commands')) == 1
    assert len(store.query('SELECT * FROM proposal_events WHERE actor=?', ('user',))) == 2


def test_a_failure_after_the_first_member_rolls_back_the_whole_command(env, monkeypatch):
    cfg, store, one, two = env
    original = Store.insert
    inserted = []
    def fail_second(self, table, row):
        if table == 'command_members':
            inserted.append(row['proposal_id'])
            if len(inserted) == 2:
                raise RuntimeError('injected member write failure')
        return original(self, table, row)
    with TestClient(create_app(cfg)) as client:
        body = request(client, [one['id'], two['id']])
        monkeypatch.setattr(Store, 'insert', fail_second)
        with pytest.raises(RuntimeError, match='injected member write failure'):
            client.post('/api/commands', json=body)
    assert len(inserted) == 2
    for table in ('commands', 'command_targets', 'command_members', 'proposal_revisions'):
        assert store.query(f'SELECT * FROM {table}') == []
    assert store.query('SELECT * FROM proposal_events WHERE actor=?', ('user',)) == []
    assert {p['status'] for p in store.query('SELECT status FROM proposals')} == {'ungated', 'pending'}


def test_redirecting_a_symlink_requires_reviewing_the_new_destination(env, tmp_path):
    cfg, store, one, _ = env
    a, b, link = tmp_path / 'a.md', tmp_path / 'b.md', tmp_path / 'alias.md'
    a.write_text(OLD)
    b.write_text(OLD)
    link.symlink_to(a)
    store.update('proposals', 'id', one['id'], {'target_path': str(link)})
    store.commit()
    with TestClient(create_app(cfg)) as client:
        body = request(client, [one['id']])
        link.unlink()
        link.symlink_to(b)
        response = client.post('/api/commands', json=body)
    assert response.status_code == 409
    assert store.query('SELECT * FROM commands') == []


def test_reading_review_does_not_save_a_revision_or_migrate(env, monkeypatch):
    from self_improve import store as store_module
    cfg, store, one, _ = env
    before = store.query('SELECT name FROM schema_migrations ORDER BY name')
    monkeypatch.setattr(store_module, 'MIGRATIONS', [*store_module.MIGRATIONS,
                        ('future_probe', 'CREATE TABLE probe (id TEXT PRIMARY KEY);')])
    with TestClient(create_app(cfg)) as client:
        assert client.get('/api/review-queue').status_code == 200
        assert client.get(f"/api/proposals/{one['id']}/review").status_code == 200
    assert store.query('SELECT * FROM proposal_revisions') == []
    assert store.query('SELECT name FROM schema_migrations ORDER BY name') == before


def test_destination_matches_the_real_writer_even_for_a_global_file_inside_git(env, tmp_path):
    from tests.test_apply import init_git_repo
    cfg, store, one, _ = env
    repo = init_git_repo(tmp_path / 'tracked', 'CLAUDE.md', OLD)
    store.update('proposals', 'id', one['id'], {'target_path': str(repo / 'CLAUDE.md')})
    store.commit()
    with TestClient(create_app(cfg)) as client:
        body = request(client, [one['id']])
        response = client.post('/api/commands', json=body)
    assert response.status_code == 202, response.text
    destination = response.json()['targets'][0]['destination']
    assert destination['mode'] == 'git_branch'
    assert destination['repo_root'] == str(repo)
    assert destination['relative_path'] == 'CLAUDE.md'
    assert destination['branch_name'] == cfg.project_branch_name
    assert not cfg.state_path('snapshots').exists()


@pytest.mark.parametrize('corruption', ['invalid_json', 'wrong_shape', 'changed_snapshot', 'changed_target'])
def test_corrupted_authorization_is_an_explicit_error(env, corruption):
    cfg, store, one, _ = env
    with TestClient(create_app(cfg)) as client:
        command = client.post('/api/commands', json=request(client, [one['id']])).json()
        revision = store.query_one('SELECT * FROM proposal_revisions')
        if corruption == 'changed_target':
            store.update('command_targets', 'id', command['targets'][0]['id'], {'diff_unified': 'different authorized edit'})
        else:
            value = {'invalid_json': '{', 'wrong_shape': '[]', 'changed_snapshot': '{}'}[corruption]
            store.update('proposal_revisions', 'id', revision['id'], {'snapshot_json': value})
        store.commit()
        response = client.get(f"/api/commands/{command['id']}")
    assert response.status_code == 500, response.text
    assert response.json()['error'] == 'CommandDataError'


def test_compatibility_approval_requires_the_reviewed_revision(env):
    cfg, store, one, _ = env
    with TestClient(create_app(cfg)) as client:
        response = client.post(f"/api/proposals/{one['id']}/decision", json={'decision': 'approve'})
    assert response.status_code == 409
    assert response.json()['error'] == 'ReviewRequired'
    assert store.query('SELECT * FROM commands') == []
    assert store.query_one('SELECT status FROM proposals WHERE id=?', (one['id'],))['status'] == 'ungated'


def test_compatibility_approval_is_an_idempotent_command_adapter(env):
    cfg, store, one, _ = env
    with TestClient(create_app(cfg)) as client:
        reviewed = request(client, [one['id']])
        body = {'decision': 'approve', 'request_key': reviewed['request_key'], 'revision': reviewed['members'][0]['revision']}
        first = client.post(f"/api/proposals/{one['id']}/decision", json=body)
        second = client.post(f"/api/proposals/{one['id']}/decision", json=body)
    assert first.status_code == second.status_code == 200
    assert first.json()['command']['id'] == second.json()['command']['id']
    assert len(store.query('SELECT * FROM commands')) == 1
    assert len(store.query('SELECT * FROM command_members')) == 1
