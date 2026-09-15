"""Canonical project detail over invented evidence, deliveries and copies."""
import pytest

from self_improve.dashboard import project_data
from self_improve import rule_availability, rule_revisions
from self_improve.store import Store
from tests.test_rule_availability import cfg, store, known_copy, delivery, init_git_repo, run_git
from tests.test_apply import insert_proposal


def incident(store, key, index, *, learning_id=None):
    sid = 'transcript-'+str(index)
    store.insert('sessions', {'file_path': sid, 'source': 'codex', 'session_id': 'same-session',
                             'project_key': key, 'project_path': '/invented/copy'})
    iid = f'incident-{index:03}'
    store.insert('incidents', {'id': iid, 'session_file': sid, 'session_id': 'same-session',
                              'project_key': key, 'signal_type': 'user_correction', 'ts': '',
                              'matched_text': 'Invented retained correction '+str(index),
                              'window_json': '[]', 'created_at': '2030-01-01T00:00:00Z'})
    if learning_id:
        store.insert('incident_learnings', {'incident_id': iid, 'learning_id': learning_id})
    return iid


def learning(store, lid):
    store.insert('learnings', {'id': lid, 'title': 'Invented '+lid, 'rule_text': 'Keep evidence.',
                              'created_at': '2030-01-01T00:00:00Z'})


def test_summary_counts_logical_ids_and_distinct_contributions(store):
    learning(store, 'lesson')
    for i in range(3): incident(store, 'project', i, learning_id='lesson')
    store.insert('sessions', {'file_path': 'unknown', 'source': 'codex', 'project_key': 'project'})
    store.insert('sessions', {'file_path': 'other-provider', 'source': 'claude_code',
                             'session_id': 'same-session', 'project_key': 'project', 'is_subagent': 1})
    store.commit()
    row = project_data.detail(store, project_key='project')
    assert row['indexed'] == {'transcripts': 5, 'known_session_ids': 2, 'unknown_session_ids': 1,
                              'subagent_transcripts': 1, 'headless_transcripts': 0}
    assert row['contributed_learnings'] == 1 and row['incidents'] == 3
    assert row['observed']['known_session_ids'] is None
    assert row['retained_deliveries'] == 0


def test_observed_session_identity_follows_mid_transcript_projects_and_rescans(tmp_path):
    from tests.test_scan_observations import make_env, make_repo, scan, ts, PROJECT
    from tests.test_scan_occurrences import write_codex, x_meta, x_user, BETA
    from self_improve.scan import mark_for_rescan
    env = make_env(tmp_path)
    try:
        second = make_repo(tmp_path/'work'/'beta', 'https://github.com/example/beta.git')
        write_codex(env, [x_meta(ts(0), env.repo), x_user('First project', ts(1)),
                          x_meta(ts(2), second), x_user('Second project', ts(3))])
        scan(env, 'first')
        for key in (PROJECT, BETA):
            assert project_data.detail(env.store, project_key=key)['observed']['known_session_ids'] == 1
        mark_for_rescan(env.store, env.cfg); scan(env, 'again')
        for key in (PROJECT, BETA):
            assert project_data.detail(env.store, project_key=key)['observed']['known_session_ids'] == 1
    finally:
        env.store.close()


def test_complete_project_evidence_is_not_the_five_incident_rule_sample(store, cfg):
    learning(store, 'lesson')
    for i in range(23): incident(store, 'project', i, learning_id='lesson')
    incident(store, 'other-project', 100, learning_id='lesson')
    store.commit()
    first = project_data.records(store, cfg, project_key='project', kind='evidence', learning_id='lesson')
    assert first['count'] == 23 and len(first['records']) == 20
    second = project_data.records(store, cfg, project_key='project', kind='evidence', learning_id='lesson', cursor=first['next_cursor'])
    assert [r['id'] for r in second['records']] == ['incident-020', 'incident-021', 'incident-022']
    assert second['next_cursor'] is None
    for change in ({'project_key':'other-project'}, {'learning_id':'different'}, {'kind':'contributed','learning_id':None}):
        with pytest.raises(project_data.ProjectRequestError, match='cursor'):
            project_data.records(store, cfg, **({'project_key':'project','kind':'evidence','learning_id':'lesson',
                                               'cursor':first['next_cursor']} | change))
    rows = project_data.records(store, cfg, project_key='project', kind='contributed')['records']
    assert rows[0]['project_incidents'] == 23


def test_delivery_is_historical_and_not_pending_or_available(cfg, store, tmp_path):
    repo = init_git_repo(tmp_path/'copy', 'AGENTS.md', '# Human\n')
    key = known_copy(store, repo)
    proposal, text = delivery(store, cfg, repo)
    pending = insert_proposal(store, target=repo/'AGENTS.md', diff='', target_kind='project_agents_md', status='pending')
    store.commit()
    rule_availability.collect_availability(store, cfg, observed_at='2030-01-01T00:00:00Z')
    assert text not in (repo/'AGENTS.md').read_text()
    assert project_data.detail(store, project_key=key)['retained_deliveries'] == 1
    row = project_data.records(store, cfg, project_key=key, kind='deliveries')['records'][0]
    assert row['proposal_id'] == proposal['id'] and row['content'] == text
    proposals = project_data.records(store, cfg, project_key=key, kind='proposals')['records']
    assert [p['id'] for p in proposals] == [pending['id']]
    assert proposals[0]['next_step'] == 'review'
    assert rule_availability.project_availability(store, project_key=key)['records'][0]['status'] == 'absent'
    # A later mutable status cannot erase retained historical application content.
    store.update('proposals', 'id', proposal['id'], {'status':'rolled_back'})
    store.commit()
    assert project_data.records(store, cfg, project_key=key, kind='deliveries')['records'][0] == row


def test_current_targets_resolve_canonical_copies_and_nested_repositories(cfg, store, tmp_path):
    parent = init_git_repo(tmp_path/'parent', 'AGENTS.md', '# Parent\n')
    sibling = init_git_repo(tmp_path/'sibling', 'AGENTS.md', '# Sibling\n')
    nested = init_git_repo(parent/'nested', 'AGENTS.md', '# Nested\n')
    for repo in (parent, sibling):
        run_git(['remote','add','origin','https://example.test/invented/same.git'], repo)
    key = known_copy(store, parent)
    # This sibling is not indexed: no path-prefix attribution can identify it.
    proposal = insert_proposal(store, target=sibling/'AGENTS.md', diff='', status='pending')
    nested_proposal = insert_proposal(store, target=nested/'AGENTS.md', diff='', status='pending')
    bad = insert_proposal(store, target='relative.md', diff='', status='pending')
    store.commit()
    got = project_data.records(store, cfg, project_key=key, kind='proposals')
    assert [r['id'] for r in got['records']] == [proposal['id']]
    assert nested_proposal['id'] not in str(got['records'])
    assert got['unresolved_proposals'][0]['proposal_id'] == bad['id']


def test_older_schema_unknown_delivery_and_corruption_fail_loud(cfg, store, tmp_path):
    incident(store, 'project', 1)
    store.conn.execute('DELETE FROM schema_migrations WHERE name=?', (rule_revisions.MIGRATION,))
    store.commit()
    assert project_data.detail(store, project_key='project')['retained_deliveries'] is None
    page = project_data.records(store, cfg, project_key='project', kind='deliveries')
    assert page['count'] is None and page['reason'] == 'schema_unavailable'


def test_retained_delivery_damage_is_not_an_empty_result(cfg, store, tmp_path):
    repo = init_git_repo(tmp_path/'project', 'AGENTS.md', '# Human\n')
    key = known_copy(store, repo)
    delivery(store, cfg, repo)
    rule_availability.collect_availability(store, cfg, observed_at='2030-01-01T00:00:00Z')
    store.conn.execute("UPDATE rule_revisions SET record_json='{}'")
    store.commit()
    with pytest.raises(rule_revisions.AvailabilityError, match='invalid stored record'):
        project_data.records(store, cfg, project_key=key, kind='deliveries')


def test_api_reads_selected_copy_without_mutating_or_opening_sources(cfg, store, tmp_path, monkeypatch):
    pytest.importorskip('fastapi')
    from self_improve.dashboard.app import create_app
    from fastapi.testclient import TestClient
    learning(store, 'lesson'); incident(store, 'project', 1, learning_id='lesson'); store.commit()
    selected = tmp_path/'selected.db'
    copy = Store(selected)
    try:
        store.conn.backup(copy.conn)
    finally:
        copy.close()
    before = selected.read_bytes()
    def forbidden(*args, **kwargs): raise AssertionError('Retained project reader opened a source')
    monkeypatch.setattr('self_improve.destinations.resolve_destination', forbidden)
    with TestClient(create_app(cfg, db_path=selected)) as client:
        assert client.get('/api/project-detail', params={'project_key':'project'}).json()['incidents'] == 1
        for kind in ('contributed','evidence','deliveries'):
            response = client.get('/api/project-records', params={'project_key':'project','kind':kind,
                                      **({'learning_id':'lesson'} if kind=='evidence' else {})})
            assert response.status_code == 200, response.text
        assert client.get('/api/project-detail', params={'project_key':'missing'}).status_code == 404
        assert client.get('/api/project-records', params={'project_key':'project','kind':'invalid'}).status_code == 400
        assert client.get('/api/project-records', params={'project_key':'project','kind':'contributed','cursor':'wrong'}).status_code == 400
    assert selected.read_bytes() == before
    assert store.query_one('SELECT COUNT(*) n FROM llm_calls')['n'] == 0
