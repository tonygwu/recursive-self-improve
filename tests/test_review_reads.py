"""Retained queue bindings never substitute for selected full authorization."""
import pytest

pytest.importorskip('fastapi')
from fastapi.testclient import TestClient

from self_improve import commands
from self_improve.dashboard.app import create_app
from tests.test_review_preview import env, preview, approval
from tests.test_apply import init_git_repo, make_diff, run_git


def test_queue_does_not_resolve_each_target_but_selected_preview_does(env, monkeypatch, tmp_path):
    cfg, store, target, before, a, b = env
    repo = init_git_repo(tmp_path / 'project', 'AGENTS.md', before)
    run_git(['remote', 'add', 'origin', 'https://example.test/fixture/review.git'], repo)
    for proposal in (a, b):
        store.update('proposals', 'id', proposal['id'], {
            'target_path': str(repo / 'AGENTS.md'), 'target_kind': 'project_agents_md'})
    store.commit()
    calls = []
    original = commands._destination
    def measured(proposal, config):
        calls.append(proposal['id'])
        return original(proposal, config)
    monkeypatch.setattr(commands, '_destination', measured)
    with TestClient(create_app(cfg)) as client:
        response = client.get('/api/review-queue')
        assert response.status_code == 200, response.text
        assert calls == [], 'Unselected queue members must not resolve delivery snapshots'
        queue = response.json()
        assert queue['count'] == 2 and queue['family_count'] == 1
        assert queue['profile'] == 'review-content/1'
        proposals = queue['families'][0]['proposals']
        assert all('revision' not in p and len(p['content_revision']) == 64 for p in proposals)
        shown = preview(client, a)
        assert calls == [a['id']]
        assert shown['members'][0]['content_revision'] == next(p['content_revision'] for p in proposals if p['id'] == a['id'])
        assert shown['ready']
    assert store.query('SELECT * FROM commands') == []
    assert (repo / 'AGENTS.md').read_text() == before


def test_content_hash_cannot_authorize_but_full_selected_preview_can(env):
    cfg, store, target, before, a, b = env
    with TestClient(create_app(cfg)) as client:
        queue = client.get('/api/review-queue').json()
        proposed = next(p for p in queue['families'][0]['proposals'] if p['id'] == a['id'])
        body = {'request_key': 'content-only-refused', 'action': 'approve',
                'members': [{'proposal_id': a['id'], 'revision': proposed['content_revision']}]}
        response = client.post('/api/commands', json=body)
        assert response.status_code == 409 and response.json()['error'] == 'StaleRevision'
        assert store.query('SELECT * FROM commands') == []
        assert store.query('SELECT * FROM proposal_events') == []
        shown = preview(client, a)
        assert shown['members'][0]['revision'] != proposed['content_revision']
        assert client.post('/api/commands', json=approval(shown)).status_code == 202
    assert target.read_text() == before


@pytest.mark.parametrize('changed', ['proposal', 'learning', 'evidence', 'evaluation'])
def test_every_retained_source_changes_the_queue_content_binding(env, changed):
    cfg, store, target, before, a, b = env
    store.insert('sessions', {'file_path': 'invented', 'source': 'claude'})
    store.insert('incidents', {'id': 'retained', 'session_file': 'invented',
                              'signal_type': 'correction', 'created_at': '2030-01-01T00:00:00Z', 'window_json': '[]'})
    store.insert('incident_learnings', {'incident_id': 'retained', 'learning_id': a['learning_id']})
    store.insert('eval_results', {'id': 'evaluation', 'kind': 'self_eval', 'verdict': 'ungated'})
    store.update('proposals', 'id', a['id'], {'eval_result_id': 'evaluation'})
    store.commit()
    with TestClient(create_app(cfg)) as client:
        def read():
            response = client.get('/api/review-queue')
            assert response.status_code == 200, response.text
            return next(p['content_revision'] for f in response.json()['families'] for p in f['proposals'] if p['id'] == a['id'])
        first = read()
        table, key, value = {
            'proposal': ('proposals', a['id'], {'diff_unified': make_diff(before, before + 'Another edit\n')}),
            'learning': ('learnings', a['learning_id'], {'why': 'New retained explanation'}),
            'evidence': ('incidents', 'retained', {'window_json': '[{"text":"New invented evidence"}]'}),
            'evaluation': ('eval_results', 'evaluation', {'metrics_json': '{"explanation":"New evaluation explanation"}'}),
        }[changed]
        store.update(table, 'id', key, value); store.commit()
        assert read() != first


def test_full_snapshot_and_authorization_fingerprint_keep_the_original_shape(env):
    from self_improve.rejections import target_identity
    cfg, store, target, before, a, b = env
    proposal = store.query_one('SELECT * FROM proposals WHERE id=?', (a['id'],))
    destination = commands._destination(proposal, cfg)
    expected = {'version': 2, 'proposal': proposal,
                'learning': store.query_one('SELECT * FROM learnings WHERE id=?', (a['learning_id'],)),
                'destination': destination, 'target_identity': target_identity(store, destination),
                'evidence': [], 'evaluation': None}
    with store.transaction():
        actual = commands.review_snapshot(store, a['id'], cfg)
    assert actual['snapshot'] == expected
    assert actual['revision'] == commands._hash(expected)
    assert actual['content_revision'] == commands.review_content_revision(expected)
