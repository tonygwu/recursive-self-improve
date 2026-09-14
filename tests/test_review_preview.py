"""A preview describes the exact selected edits, including their combined effect."""
from contextlib import closing
import json

import pytest

from self_improve.config import Config
from self_improve.store import Store
from tests.test_apply import insert_proposal, make_diff, init_git_repo

pytest.importorskip('fastapi')
from fastapi.testclient import TestClient
from self_improve.dashboard.app import create_app


@pytest.fixture
def env(tmp_path):
    target = tmp_path / 'CLAUDE.md'
    before = 'one\ntwo\nthree\nfour\nfive\nsix\n'
    target.write_text(before)
    cfg = Config(state_dir=str(tmp_path / 'state'), global_claude_md=str(target), global_claude_md_line_budget=6)
    with closing(Store(cfg.state_path('state.db'))) as store:
        a = insert_proposal(store, target=target, diff=make_diff(before, before.replace('two', 'TWO')), status='pending')
        b = insert_proposal(store, target=target, diff=make_diff(before, before.replace('five', 'FIVE')), status='ungated')
        store.update('proposals', 'id', b['id'], {'learning_id': a['learning_id']})
        store.commit()
        yield cfg, store, target, before, a, b


def preview(client, *proposals):
    response = client.get('/api/review-preview', params={'proposal_ids': ','.join(p['id'] for p in proposals)})
    assert response.status_code == 200, response.text
    return response.json()


def approval(shown, key='preview-approval-001'):
    return {'request_key': key, 'action': 'approve', 'preview_revision': shown['revision'],
            'members': [{'proposal_id': m['proposal_id'], 'revision': m['revision']} for m in shown['members']]}


def test_overlapping_context_with_disjoint_edits_has_one_complete_preview(env):
    from self_improve.propose import apply_unified_diff
    cfg, store, target, before, a, b = env
    with TestClient(create_app(cfg)) as client:
        shown = preview(client, a, b)
        assert len(shown['targets']) == 1
        combined = shown['targets'][0]
        assert combined['state'] == 'ready', combined
        assert apply_unified_diff(before, combined['diff_unified']) == before.replace('two', 'TWO').replace('five', 'FIVE')
        assert {m['proposal_id'] for m in shown['members']} == {a['id'], b['id']}
        response = client.post('/api/commands', json=approval(shown))
        assert response.status_code == 202, response.text
        command = response.json()
        assert command['targets'][0]['diff_unified'] == combined['diff_unified']
        assert client.get('/api/commands/' + command['id']).status_code == 200
    assert target.read_text() == before
    assert not cfg.state_path('snapshots').exists()


def test_conflicting_alternatives_are_visible_and_selecting_one_is_enough(env):
    cfg, store, target, before, a, b = env
    store.update('proposals', 'id', b['id'], {'diff_unified': make_diff(before, before.replace('two', 'other'))})
    store.commit()
    with TestClient(create_app(cfg)) as client:
        shown = preview(client, a, b)
        assert shown['targets'][0]['state'] == 'conflict'
        assert len(shown['members']) == 2, 'conflicts must not hide either member'
        assert client.post('/api/commands', json=approval(shown)).status_code == 409
        selected = preview(client, a)
        assert client.post('/api/commands', json=approval(selected, 'one-alternative')).status_code == 202
    assert store.query_one('SELECT status FROM proposals WHERE id=?', (b['id'],))['status'] == 'ungated'


def test_a_changed_target_invalidates_the_preview_without_any_decision(env):
    cfg, store, target, before, a, b = env
    with TestClient(create_app(cfg)) as client:
        shown = preview(client, a, b)
        target.write_text(before + 'human addition\n')
        response = client.post('/api/commands', json=approval(shown))
    assert response.status_code == 409, response.text
    assert response.json()['error'] == 'StalePreview'
    assert store.query('SELECT * FROM commands') == []
    assert {r['status'] for r in store.query('SELECT status FROM proposals')} == {'pending', 'ungated'}


def test_line_budget_counts_combined_lines_once_and_is_advisory(env):
    cfg, store, target, before, a, b = env
    diff = make_diff(before, before + 'one shared addition\n')
    for p in (a, b):
        store.update('proposals', 'id', p['id'], {'diff_unified': diff})
    store.commit()
    with TestClient(create_app(cfg)) as client:
        shown = preview(client, a, b)
        budget = shown['targets'][0]['budget']
        assert budget == {'before': 6, 'after': 7, 'limit': 6, 'over': True}
        response = client.post('/api/commands', json=approval(shown))
        assert response.status_code == 202, response.text


def test_queue_and_preview_count_blank_lines_in_the_generation_budget(env):
    from self_improve.dashboard.queries import _line_budget
    cfg, store, target, before, a, b = env
    content = before + '\n\n'
    target.write_text(content)
    store.update('proposals', 'id', a['id'], {'diff_unified': make_diff(content, content + 'new rule\n')})
    store.commit()
    with TestClient(create_app(cfg)) as client:
        shown = preview(client, a)
    assert shown['targets'][0]['budget']['before'] == 8
    assert _line_budget(cfg, str(target), 1)['used'] == 8


def test_preview_reads_the_delivery_branch_instead_of_the_active_checkout(env, tmp_path):
    import subprocess
    cfg, store, target, before, a, b = env
    repo = init_git_repo(tmp_path / 'project', 'AGENTS.md', before)
    subprocess.run(['git', '-C', str(repo), 'checkout', '-b', cfg.project_branch_name], check=True, capture_output=True)
    (repo / 'AGENTS.md').write_text('branch content\n')
    subprocess.run(['git', '-C', str(repo), 'commit', '-am', 'Branch rule'], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(repo), 'checkout', '-'], check=True, capture_output=True)
    store.update('proposals', 'id', a['id'], {'target_path': str(repo / 'AGENTS.md'), 'target_kind': 'project_agents_md',
                 'diff_unified': make_diff('branch content\n', 'branch content\nnew rule\n')})
    store.commit()
    with TestClient(create_app(cfg)) as client:
        shown = preview(client, a)
    assert shown['targets'][0]['state'] == 'ready', shown
    assert shown['targets'][0]['before_content'] == 'branch content\n'
    assert (repo / 'AGENTS.md').read_text() == before


def test_full_member_evidence_and_eval_are_returned_without_truncation(env):
    cfg, store, target, before, a, b = env
    for n in range(7):
        iid = f'evidence-{n}'
        store.insert('sessions', {'file_path': f'/invented/session-{n}', 'source': 'codex', 'session_id': f'session-{n}'})
        store.insert('incidents', {'id': iid, 'session_id': f'session-{n}', 'session_file': f'/invented/session-{n}',
                     'created_at': '2026-09-01T00:00:00Z', 'ts': f'2026-09-01T00:00:0{n}Z',
                     'signal_type': 'correction', 'matched_text': 'full evidence ' + str(n),
                     'window_json': json.dumps([{'role': 'user', 'text': 'x' * 15000 + f' tail-{n}'}])})
        store.insert('incident_learnings', {'incident_id': iid, 'learning_id': a['learning_id']})
    store.commit()
    with TestClient(create_app(cfg)) as client:
        shown = preview(client, a, b)
    for member in shown['members']:
        evidence = member['snapshot']['evidence']
        assert len(evidence) == 7
        assert json.loads(evidence[-1]['window_json'])[0]['text'].endswith('tail-6')
    assert store.query('SELECT * FROM proposal_revisions') == [], 'preview writes must remain empty'
