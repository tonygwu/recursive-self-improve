"""Monthly measurements through real, invented scanner inputs and selected Stores."""
from datetime import datetime, timezone
import dataclasses

import pytest

from self_improve.dashboard import queries
from self_improve.scan import mark_for_rescan
from tests.test_scan_observations import (
    PROJECT, SID_B, make_env, write_claude, correction_session, c_user, scan,
)

NOW = datetime(2026, 9, 15, tzinfo=timezone.utc)


def cross_month(env):
    records = correction_session(env.repo)
    for record, when in zip(records, (
        '2026-08-31T23:59:58Z', '2026-08-31T23:59:59Z',
        '2026-09-01T01:30:00+02:00', '2026-09-01T00:00:00Z',
    )):
        record['timestamp'] = when
    missing = c_user('unknown time', '2026-09-01T00:01:00Z', env.repo)
    del missing['timestamp']
    records += [missing, c_user('at exclusive end', NOW.isoformat(), env.repo)]
    write_claude(env, records)
    scan(env, 'cross-month')


def test_months_use_uncapped_occurrences_and_actual_line_timestamps(tmp_path):
    env = make_env(tmp_path)
    cross_month(env)
    env.store.conn.execute('UPDATE sessions SET lines_scanned=999999')
    env.store.conn.execute('DELETE FROM scan_incident_links')
    env.store.conn.execute('DELETE FROM incidents')
    env.store.commit()
    result = queries.incident_rate(env.store, now_utc=NOW, project_key=PROJECT)
    assert result['contract_version'] == 3
    points = {p['month']: p for p in result['series']}
    aug, sep = points['2026-08'], points['2026-09']
    assert (aug['eligible_lines'], aug['occurrences'], aug['observed_rate_per_100k']) == (3, 0, 0)
    assert (sep['eligible_lines'], sep['occurrences'], sep['observed_rate_per_100k']) == (1, 1, 100000)
    assert aug['rate_per_100k'] is sep['rate_per_100k'] is None
    assert result['reason'] == 'insufficient_sessions'
    assert aug['sessions'] == sep['sessions'] == 1
    assert aug['session_size']['median'] == 3 and sep['session_size']['median'] == 1
    assert not aug['partial'] and sep['partial']
    assert sep['signals']['correction']['count'] == 1
    assert sep['small_sample'] and not result['dropped_months']
    assert points['2026-07']['rate_per_100k'] is None
    assert result['coverage_by_project'][0]['coverage']['counts_by_cause']['unknown_time_lines'] == 1
    # Reconciliation does not create extra units or occurrences.
    mark_for_rescan(env.store, env.cfg)
    scan(env, 'rescan')
    assert queries.incident_rate(env.store, now_utc=NOW, project_key=PROJECT)['series'] == result['series']
    env.store.close()


def test_versions_are_separate_and_unknown_versions_have_no_rate(tmp_path):
    from self_improve import filter_incidents
    env = make_env(tmp_path)
    cross_month(env)
    changed = dataclasses.replace(env.cfg, correction_max_len=1500)
    mark_for_rescan(env.store, changed)
    scan(env, 'changed', cfg=changed)
    mark_for_rescan(env.store, changed)
    scan(env, 'unknown', cfg=changed, detect_fn=lambda events, cfg: filter_incidents.detect(events, cfg))
    result = queries.incident_rate(env.store, now_utc=NOW)
    assert result['reason'] == 'incompatible_versions' and result['series'] == []
    assert len(result['version_groups']) == 3
    for group in result['version_groups']:
        selected = queries.incident_rate(env.store, now_utc=NOW, compatibility_key=group['compatibility_key'])
        sep = selected['series'][-1]
        assert (sep['eligible_lines'], sep['occurrences']) == (1, 1)
        assert sep['rate_per_100k'] is None
        assert sep['observed_rate_per_100k'] == (100000 if group['identifiable'] else None)
        assert sep['reason'] == ('insufficient_sessions' if group['identifiable'] else 'unknown_version')
        assert group['months'][-1]['month'] == '2026-09'
        assert group['manifests'][0]['config']['values']['correction_max_len'] in {1500, env.cfg.correction_max_len}
    env.store.close()


def test_global_session_identity_deduplicates_across_projects(tmp_path):
    from tests.test_scan_observations import make_repo
    env = make_env(tmp_path)
    second = str(make_repo(tmp_path/'other', 'beta'))
    write_claude(env, [c_user('first', '2026-09-01T00:00:00Z', env.repo),
                       c_user('second', '2026-09-02T00:00:00Z', second)])
    write_claude(env, [c_user('another session', '2026-09-03T00:00:00Z', second, sid=SID_B)], name=SID_B)
    scan(env, 'projects')
    result = queries.incident_rate(env.store, now_utc=NOW)
    point = result['series'][-1]
    assert (point['projects'], point['sessions'], point['transcripts'], point['eligible_lines']) == (2, 2, 2, 3)
    assert point['session_size']['median'] == 1.5
    assert point['workload']['by_source'] == {'claude': 3}
    env.store.close()


@pytest.mark.parametrize('kwargs', [
    {'months': 0}, {'months': 25}, {'months': True}, {'end_month': '2026-13'},
    {'end_month': '2026-10'}, {'end_month': '0000-01'}, {'project_key': ''},
    {'compatibility_key': ''}, {'delivery_cursor': 'not-json'},
])
def test_invalid_selectors_are_named_errors(tmp_path, kwargs):
    from self_improve.dashboard.scan_data import ExposureRequestError
    env = make_env(tmp_path)
    with pytest.raises(ExposureRequestError):
        queries.incident_rate(env.store, now_utc=NOW, **kwargs)
    env.store.close()


def test_older_schema_missing_version_and_damaged_manifest_are_distinct(tmp_path):
    from self_improve.scan_observations import ScanObservationError
    env = make_env(tmp_path)
    empty = queries.incident_rate(env.store, now_utc=NOW)
    assert empty['reason'] == 'missing_observations' and empty['series'] == []
    cross_month(env)
    missing = queries.incident_rate(env.store, now_utc=NOW, compatibility_key='uncovered')
    assert missing['reason'] == 'uncovered_version' and missing['series'] == []
    env.store.conn.execute("UPDATE scan_manifests SET manifest_json='{}'")
    env.store.commit()
    with pytest.raises(ScanObservationError):
        queries.incident_rate(env.store, now_utc=NOW)
    env.store.conn.execute("DELETE FROM schema_migrations WHERE name='0022_scan_observations'")
    env.store.commit()
    old = queries.incident_rate(env.store, now_utc=NOW)
    assert old['reason'] == 'schema_unavailable' and old['series'] == []
    assert not env.store.query_one("SELECT name FROM schema_migrations WHERE name='0022_scan_observations'")
    env.store.close()


def test_api_reads_only_selected_copy_and_validates_versions(tmp_path):
    import sqlite3
    pytest.importorskip('fastapi')
    from fastapi.testclient import TestClient
    from self_improve.dashboard.app import create_app
    env = make_env(tmp_path)
    cross_month(env)
    copied = tmp_path/'copied.db'
    with sqlite3.connect(copied) as target:
        env.store.conn.backup(target)
    env.store.conn.execute('UPDATE scan_lines SET active=0')
    env.store.conn.execute('UPDATE scan_occurrences SET active=0')
    env.store.commit()
    before = copied.read_bytes()
    with TestClient(create_app(env.cfg, db_path=copied, clock=lambda: NOW)) as client:
        response = client.get('/api/incident-rate')
        assert response.status_code == 200
        assert response.json()['series'][-1]['occurrences'] == 1
        assert client.get('/api/incident-rate?end_month=2027-01').status_code == 400
        assert client.get('/api/incident-rate?project_key=').status_code == 400
    assert copied.read_bytes() == before
    assert env.store.query_one('SELECT COUNT(*) n FROM commands')['n'] == 0
    assert env.store.query_one('SELECT COUNT(*) n FROM llm_calls')['n'] == 0
    env.store.close()


def test_skipped_physical_lines_remain_visible(tmp_path):
    from tests.test_scan_occurrences import write_codex, x_meta, x_tokens
    env = make_env(tmp_path)
    write_codex(env, [x_meta('2026-09-01T00:00:00Z', env.repo),
                      x_tokens('2026-09-01T00:00:01Z')])
    scan(env, 'tokens')
    point = queries.incident_rate(env.store, now_utc=NOW)['series'][-1]
    assert point['eligible_lines'] == 2 and point['observed_rate_per_100k'] == 0
    assert point['rate_per_100k'] is None
    assert point['workload']['by_source'] == {'codex': 2}
    env.store.close()


def test_delivery_dates_come_from_retained_application_not_current_proposal(tmp_path):
    from self_improve import rule_availability, rule_revisions
    from self_improve.store import Store
    from tests.test_rule_availability import cfg as cfg_fixture, known_copy, delivery, init_git_repo
    cfg = cfg_fixture.__wrapped__(tmp_path)
    store = Store(cfg.state_path('state.db'))
    from self_improve.execution_policy import set_class_policy
    set_class_policy(store, 'project', True, now='2020-01-01T00:00:00Z')
    repo = init_git_repo(tmp_path/'delivery', 'AGENTS.md', '# Human\n')
    key = known_copy(store, repo)
    proposal, text = delivery(store, cfg, repo)
    store.conn.execute("UPDATE proposal_events SET ts='2026-08-31T23:59:59Z' WHERE event='applied'")
    store.commit()
    rule_availability.collect_availability(store, cfg, observed_at='2026-09-10T00:00:00Z')
    first = queries.incident_rate(store, now_utc=NOW, project_key=key)['deliveries']
    assert first['count'] == 1 and first['by_month'] == {'2026-08': 1}
    assert first['records'][0]['applied_at'] == '2026-08-31T23:59:59.000000Z'
    assert first['records'][0]['proposal_id'] == proposal['id']
    assert 'content' not in first['records'][0]
    store.update('proposals', 'id', proposal['id'], {'status': 'rolled_back', 'created_at': '2026-09-01T00:00:00Z'})
    store.commit()
    assert queries.incident_rate(store, now_utc=NOW, project_key=key)['deliveries'] == first
    assert queries.incident_rate(store, now_utc=NOW, project_key='unrelated')['deliveries']['count'] == 0
    # Invented archive expansion tests stable tied-timestamp pagination through
    # the real validated persistence boundary. These are not extra real writes.
    original = rule_revisions.retained_revisions(store)[0]
    with store.transaction():
        for i in range(22):
            record = {**original, 'application_event_id': f'invented-{i}'}
            record['id'] = rule_revisions.digest({k:v for k,v in record.items() if k not in {'id', 'captured_at'}})
            rule_revisions.record_revision(store, record)
    first = queries.incident_rate(store, now_utc=NOW, project_key=key)['deliveries']
    second = queries.incident_rate(store, now_utc=NOW, project_key=key, delivery_cursor=first['next_cursor'])['deliveries']
    assert first['count'] == second['count'] == 23
    assert len(first['records']) == 20 and len(second['records']) == 3
    assert len({r['id'] for r in first['records'] + second['records']}) == 23
    assert second['next_cursor'] is None
    from self_improve.dashboard.scan_data import ExposureRequestError
    with pytest.raises(ExposureRequestError, match='cursor'):
        queries.incident_rate(store, now_utc=NOW, project_key='other', delivery_cursor=first['next_cursor'])
    store.conn.execute("UPDATE rule_revisions SET record_json='{}'")
    store.commit()
    with pytest.raises(rule_revisions.AvailabilityError):
        queries.incident_rate(store, now_utc=NOW)
    store.close()


def test_failed_reconciliation_keeps_observed_subset_and_named_coverage(tmp_path):
    from tests.test_scan_observations import append, jsonl, c_tool
    env = make_env(tmp_path)
    path = write_claude(env, correction_session(env.repo))
    scan(env, 'baseline')
    append(path, jsonl([c_tool('2026-09-01T00:00:00Z', env.repo, tool_id='new'),
                        c_user("no, still wrong", '2026-09-01T00:00:01Z', env.repo)]))
    def refused(events, incident, cfg):
        raise RuntimeError('invented window refusal')
    failed = scan(env, 'failed', build_window_fn=refused)
    assert failed.error_taxonomy == {'window:RuntimeError': 1}
    result = queries.incident_rate(env.store, now_utc=NOW)
    assert result['series'][-2]['eligible_lines'] == 4
    assert result['series'][-1]['rate_per_100k'] is None
    assert result['coverage_by_project'][0]['coverage']['counts_by_cause']['stale_failed_reconciliation_transcripts'] == 1
    env.store.close()
