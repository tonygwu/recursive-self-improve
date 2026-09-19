"""A content revision alone cannot authorize a write to an unreviewed base."""
from contextlib import closing
import json

import pytest

pytest.importorskip('fastapi')
from fastapi.testclient import TestClient

from self_improve import apply
from self_improve.commands import _hash, _json, command_status, submit_command
from self_improve.config import Config
from self_improve.dashboard.app import create_app
from self_improve.review import preview_selection
from self_improve.store import Store
from self_improve.worker import run_once
from tests.test_apply import insert_proposal, make_diff

BEFORE = '# Invented rules\n' + ''.join(f'line {i}\n' for i in range(1, 13))
AFTER = BEFORE.replace('line 2\n', 'line 2\nnew invented rule\n')
FOOTER = 'unreviewed human footer\n'


@pytest.fixture
def env(tmp_path):
    target = tmp_path / 'rules.md'
    target.write_text(BEFORE)
    cfg = Config(state_dir=str(tmp_path / 'state'), global_claude_md=str(target))
    with closing(Store(cfg.state_path('state.db'))) as store:
        proposal = insert_proposal(store, target=target, diff=make_diff(BEFORE, AFTER), status='ungated')
        yield cfg, store, target, proposal


def shown_request(store, cfg, proposal, key='base-approval-001'):
    with store.transaction():
        shown = preview_selection(store, cfg, [proposal['id']])
    assert shown['ready'], shown
    return {'action': 'approve', 'request_key': key, 'preview_revision': shown['revision'],
            'members': [{'proposal_id': m['proposal_id'], 'revision': m['revision']} for m in shown['members']]}


def post_approval(client, route, body):
    if route == 'commands':
        return client.post('/api/commands', json=body)
    member = body['members'][0]
    adapted = {'decision': 'approve', 'request_key': body['request_key'], 'revision': member['revision']}
    if 'preview_revision' in body:
        adapted['preview_revision'] = body['preview_revision']
    return client.post(f"/api/proposals/{member['proposal_id']}/decision", json=adapted)


@pytest.mark.parametrize('route', ['commands', 'compatibility'])
@pytest.mark.parametrize('changed', [False, True])
def test_omitted_combined_preview_never_records_a_new_approval(env, route, changed):
    cfg, store, target, proposal = env
    body = shown_request(store, cfg, proposal)
    body.pop('preview_revision')
    if changed:
        target.write_text(BEFORE + FOOTER)
        current = shown_request(store, cfg, proposal)
        assert current['members'] == body['members'], 'member identity alone misses the changed base'
    with TestClient(create_app(cfg)) as client:
        response = post_approval(client, route, body)
    assert response.status_code == 409, response.text
    assert response.json()['error'] == 'FreshReviewRequired'
    for table in ('commands', 'command_targets', 'command_members', 'proposal_revisions', 'proposal_events', 'llm_calls'):
        assert store.query(f'SELECT * FROM {table}') == []
    assert store.query_one('SELECT status FROM proposals WHERE id=?', (proposal['id'],))['status'] == 'ungated'
    assert run_once(store, cfg) is None
    assert target.read_text() == BEFORE + (FOOTER if changed else '')
    assert not cfg.state_path('snapshots').exists()


@pytest.mark.parametrize('route', ['commands', 'compatibility'])
@pytest.mark.parametrize('timing', ['before_approval', 'before_worker', 'unchanged'])
def test_combined_preview_guards_both_boundaries_and_allows_exact_delivery(env, route, timing):
    cfg, store, target, proposal = env
    body = shown_request(store, cfg, proposal)
    if timing == 'before_approval':
        target.write_text(BEFORE + FOOTER)
    with TestClient(create_app(cfg)) as client:
        response = post_approval(client, route, body)
        if timing == 'before_approval':
            assert response.status_code == 409, response.text
            assert response.json()['error'] == 'StalePreview'
            assert store.query('SELECT * FROM commands') == []
        else:
            assert response.status_code == (202 if route == 'commands' else 200), response.text
            assert target.read_text() == BEFORE, 'the API must not write the file'
            if timing == 'before_worker':
                target.write_text(BEFORE + FOOTER)
            result = run_once(store, cfg)
            assert result['state'] == ('blocked' if timing == 'before_worker' else 'completed'), result
            if timing == 'before_worker':
                assert result['targets'][0]['error_code'] == 'TargetChanged'
            assert post_approval(client, route, body).status_code == response.status_code
    assert target.read_text() == (AFTER if timing == 'unchanged' else BEFORE + FOOTER)
    assert len(store.query("SELECT * FROM proposal_events WHERE event='applied'")) == (1 if timing == 'unchanged' else 0)
    assert store.query('SELECT * FROM llm_calls') == []


def retain_legacy_command(store, command_id):
    """Represent the pre-preview persisted format without bypassing production code."""
    row = store.query_one('SELECT * FROM commands WHERE id=?', (command_id,))
    body = json.loads(row['payload_json'])
    body.pop('preview_revision')
    with store.transaction(write=True):
        store.update('commands', 'id', command_id, {'payload_json': _json(body),
                     'request_hash': _hash({k: v for k, v in body.items() if k != 'request_key'})})
        for target in store.query('SELECT * FROM command_targets WHERE command_id=?', (command_id,)):
            checkpoint = json.loads(target['checkpoint_json'])
            checkpoint.pop('review_preview')
            member = store.query_one('SELECT revision_id FROM command_members WHERE target_id=?', (target['id'],))
            revision = store.query_one('SELECT snapshot_json FROM proposal_revisions WHERE id=?', (member['revision_id'],))
            original_diff = json.loads(revision['snapshot_json'])['proposal']['diff_unified']
            store.update('command_targets', 'id', target['id'], {'checkpoint_json': _json(checkpoint), 'diff_unified': original_diff})
    return body


@pytest.mark.parametrize('stage', ['queued', 'prepared', 'file_replaced', 'before_ack'])
@pytest.mark.parametrize('cancel', [False, True])
def test_legacy_restart_preserves_reads_and_observed_writes_without_new_authority(env, monkeypatch, stage, cancel):
    cfg, store, target, proposal = env
    command = submit_command(store, cfg, shown_request(store, cfg, proposal))
    class Crash(BaseException):
        pass
    def crash(point, *_):
        if point == stage:
            raise Crash()
    if stage != 'queued':
        with monkeypatch.context() as patch:
            patch.setattr(apply, '_delivery_checkpoint', crash)
            with pytest.raises(Crash):
                run_once(store, cfg)
    body = retain_legacy_command(store, command['id'])
    saved = command_status(store, command['id'])
    assert submit_command(store, cfg, body) == saved, 'exact historical replay remains readable'
    with TestClient(create_app(cfg)) as client:
        assert client.get('/api/commands/' + command['id']).json() == saved
        assert post_approval(client, 'commands', body).json() == saved
        assert post_approval(client, 'compatibility', body).json()['command'] == saved
        changed = {**body, 'members': [{'proposal_id': proposal['id'], 'revision': 'f' * 64}]}
        for route in ('commands', 'compatibility'):
            response = post_approval(client, route, changed)
            assert response.status_code == 409 and response.json()['error'] == 'IdempotencyConflict'
        assert len(store.query('SELECT * FROM commands')) == 1
    if cancel:
        submit_command(store, cfg, {'action': 'cancel_delivery', 'request_key': 'legacy-cancel-001', 'command_id': command['id']})
    writes = []
    real_write = apply._atomic_write
    def record_write(*args):
        writes.append(args[0])
        return real_write(*args)
    monkeypatch.setattr(apply, '_atomic_write', record_write)
    result = run_once(store, cfg)
    observed = stage in {'file_replaced', 'before_ack'}
    expected = 'completed' if observed else 'cancelled' if cancel else 'blocked'
    assert result['state'] == expected, result
    if not observed and not cancel:
        assert result['targets'][0]['error_code'] == 'FreshReviewRequired'
    assert writes == [], 'legacy recovery must not initiate an unreviewed write'
    assert target.read_text() == (AFTER if observed else BEFORE)
    assert len(store.query("SELECT * FROM proposal_events WHERE event='applied'")) == int(observed)
    assert store.query('SELECT * FROM llm_calls') == []
    assert run_once(store, cfg) is None
    assert submit_command(store, cfg, body)['id'] == command['id']
    if not observed:
        if not cancel:
            submit_command(store, cfg, {'action': 'cancel_delivery', 'request_key': 'legacy-cancel-after-block', 'command_id': command['id']})
            assert run_once(store, cfg)['state'] == 'cancelled'
        target.write_text(BEFORE + FOOTER)
        fresh = shown_request(store, cfg, proposal, 'fresh-base-approval-002')
        submit_command(store, cfg, fresh)
        assert run_once(store, cfg)['state'] == 'completed'
        assert target.read_text() == AFTER + FOOTER
        assert len(writes) == 1


@pytest.mark.parametrize('stage', ['commit_prepared', 'ref_updated', 'before_ack'])
@pytest.mark.parametrize('cancel', [False, True])
def test_legacy_branch_restart_cannot_publish_an_unreviewed_ref(env, tmp_path, monkeypatch, stage, cancel):
    from tests.test_apply import init_git_repo, run_git
    cfg, store, _, proposal = env
    repo = init_git_repo(tmp_path / 'project', 'AGENTS.md', BEFORE)
    target = repo / 'AGENTS.md'
    store.update('proposals', 'id', proposal['id'], {'target_path': str(target), 'target_kind': 'project_agents_md'})
    store.commit()
    body = shown_request(store, cfg, proposal)
    command = submit_command(store, cfg, body)
    target.write_text('unrelated working-copy edits\n')
    run_git(['add', 'AGENTS.md'], repo)
    head = run_git(['rev-parse', 'HEAD'], repo)
    index = (repo / '.git/index').read_bytes()
    class Crash(BaseException):
        pass
    def crash(point, *_):
        if point == stage:
            raise Crash()
    with monkeypatch.context() as patch:
        patch.setattr(apply, '_delivery_checkpoint', crash)
        with pytest.raises(Crash):
            run_once(store, cfg)
    legacy = retain_legacy_command(store, command['id'])
    if cancel:
        submit_command(store, cfg, {'action': 'cancel_delivery', 'request_key': 'branch-legacy-cancel', 'command_id': command['id']})
    refs = run_git(['show-ref'], repo)
    publish = apply._publish_prepared_write
    def refuse_new_write(*args):
        pytest.fail('Legacy restart attempted a new reference update')
    monkeypatch.setattr(apply, '_publish_prepared_write', refuse_new_write)
    result = run_once(store, cfg)
    observed = stage != 'commit_prepared'
    assert result['state'] == ('completed' if observed else 'cancelled' if cancel else 'blocked'), result
    if not observed and not cancel:
        assert result['targets'][0]['error_code'] == 'FreshReviewRequired'
    assert run_git(['show-ref'], repo) == refs
    assert run_git(['rev-parse', 'HEAD'], repo) == head
    assert (repo / '.git/index').read_bytes() == index
    assert target.read_text() == 'unrelated working-copy edits\n'
    branch = command['targets'][0]['destination']['branch_name']
    if observed:
        assert run_git(['show', branch + ':AGENTS.md'], repo) == AFTER.rstrip('\n')
    else:
        assert 'refs/heads/' + branch not in refs
    assert len(store.query("SELECT * FROM proposal_events WHERE event='applied'")) == int(observed)
    assert store.query('SELECT * FROM llm_calls') == []
    assert run_once(store, cfg) is None
    assert submit_command(store, cfg, legacy)['id'] == command['id']
    if not observed:
        if not cancel:
            submit_command(store, cfg, {'action': 'cancel_delivery', 'request_key': 'branch-cancel-after-block', 'command_id': command['id']})
            assert run_once(store, cfg)['state'] == 'cancelled'
        monkeypatch.setattr(apply, '_publish_prepared_write', publish)
        submit_command(store, cfg, shown_request(store, cfg, proposal, 'fresh-branch-base-002'))
        assert run_once(store, cfg)['state'] == 'completed'
        assert run_git(['show', branch + ':AGENTS.md'], repo) == AFTER.rstrip('\n')
        assert run_git(['rev-parse', 'HEAD'], repo) == head
        assert (repo / '.git/index').read_bytes() == index
        assert target.read_text() == 'unrelated working-copy edits\n'
