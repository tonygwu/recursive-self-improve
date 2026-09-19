"""Complete retained delivery dates and cursor-bound monthly read windows."""
import base64
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from self_improve import rule_availability, rule_revisions
from self_improve.dashboard.trend_data import monthly_exposure
from self_improve.dashboard.scan_data import ExposureRequestError
from self_improve.execution_policy import set_class_policy
from self_improve.store import Store
from tests.test_rule_availability import cfg as cfg_fixture, known_copy, delivery, init_git_repo


@pytest.fixture
def archive(tmp_path):
    cfg = cfg_fixture.__wrapped__(tmp_path)
    store = Store(cfg.state_path('state.db'))
    set_class_policy(store, 'project', True, now='2020-01-01T00:00:00Z')
    repo = init_git_repo(tmp_path/'delivery', 'AGENTS.md', '# Invented instruction\n')
    key = known_copy(store, repo)
    proposal, _ = delivery(store, cfg, repo)
    # The offset crosses into leap day in UTC. Capture time is deliberately later.
    store.conn.execute("UPDATE proposal_events SET ts='2024-03-01T00:30:00+02:00' WHERE event='applied'")
    store.commit()
    rule_availability.collect_availability(store, cfg, observed_at='2026-09-10T00:00:00Z')
    original = rule_revisions.retained_revisions(store)[0]
    with store.transaction():
        for i in range(22):
            record = {**original, 'application_event_id': f'invented-{i}',
                      'applied_at': '2024-03-01T00:00:00.000000Z' if i < 12 else original['applied_at']}
            record['id'] = rule_revisions.digest({k:v for k,v in record.items() if k not in {'id', 'captured_at'}})
            rule_revisions.record_revision(store, record)
    yield store, key, cfg, proposal
    store.close()


@pytest.mark.parametrize('initial,later,end_month', [
    ('2024-03-15', '2024-03-16', None),
    ('2024-03-31', '2024-04-01', None),
    ('2024-03-01', '2024-04-02', None),
    ('2024-04-15', '2024-05-02', '2024-03'),
])
def test_pages_keep_original_window_with_advancing_clock(archive, initial, later, end_month):
    store, key, _, _ = archive
    clock = lambda value: datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
    # The exact March boundary retains February's rows and an empty March.
    kwargs = dict(project_key=key, end_month=end_month)
    first = monthly_exposure(store, now_utc=clock(initial), **kwargs)
    cursor = first['deliveries']['next_cursor']
    if cursor is None:
        # A synthetic page position exercises a real valid interval with <20 rows.
        row = first['deliveries']['records'][0]
        cursor = base64.urlsafe_b64encode(json.dumps({'scope':{k:first['requested'][k] for k in ('project_key','start','end')}, 'after':[row['applied_at'],row['id']]}).encode()).decode()
    second = monthly_exposure(store, now_utc=clock(later), delivery_cursor=cursor, **kwargs)
    assert second['requested'] == first['requested']
    # The selector must still allow current months after paging an old interval.
    assert second['partial_month'] == later[:7]
    assert second['deliveries']['by_day'] == first['deliveries']['by_day']
    assert second['deliveries']['count'] == first['deliveries']['count']
    if first['deliveries']['next_cursor']:
        assert not ({r['id'] for r in first['deliveries']['records']} & {r['id'] for r in second['deliveries']['records']})


def test_daily_counts_are_complete_utc_and_independent_of_page(archive, tmp_path):
    store, key, cfg, proposal = archive
    now = datetime(2024,3,15,tzinfo=timezone.utc)
    before = list(store.conn.iterdump())
    first = monthly_exposure(store, now_utc=now, project_key=key)['deliveries']
    second = monthly_exposure(store, now_utc=now+timedelta(seconds=1), project_key=key, delivery_cursor=first['next_cursor'])['deliveries']
    assert (len(first['records']),len(second['records'])) == (20,3)
    assert first['by_day'] == second['by_day'] == {'2024-02-29':11,'2024-03-01':12}
    assert first['by_month'] == {'2024-02':11,'2024-03':12}
    assert sum(first['by_day'].values()) == sum(first['by_month'].values()) == first['count'] == 23
    assert len({r['id'] for r in first['records']+second['records']}) == 23
    assert second['next_cursor'] is None
    empty = monthly_exposure(store, now_utc=now, project_key='unrelated')['deliveries']
    assert empty['count'] == 0 and empty['by_day'] == {}
    # Half-open end excludes midnight March 1; explicit February includes leap day.
    feb = monthly_exposure(store, now_utc=now, months=1, end_month='2024-02')['deliveries']
    assert feb['count'] == 11 and feb['by_day'] == {'2024-02-29':11}
    assert list(store.conn.iterdump()) == before
    copied = tmp_path/'copy.db'
    with sqlite3.connect(copied) as target: store.conn.backup(target)
    store.conn.execute('DELETE FROM rule_revisions'); store.commit()
    selected = Store(copied, read_only=True)
    try:
        assert monthly_exposure(selected, now_utc=now)['deliveries']['by_day'] == first['by_day']
    finally:
        selected.close()


def test_cursor_rejects_changed_or_future_scope(archive):
    store, key, _, _ = archive
    now = datetime(2024,3,15,tzinfo=timezone.utc)
    cursor = monthly_exposure(store, now_utc=now, project_key=key)['deliveries']['next_cursor']
    for changes in ({'project_key':'other'}, {'months':6}, {'end_month':'2024-02'}, {'now_utc':now-timedelta(seconds=1)}):
        with pytest.raises(ExposureRequestError, match='cursor'):
            monthly_exposure(store, **(dict(now_utc=now,project_key=key,delivery_cursor=cursor)|changes))
    token = json.loads(base64.urlsafe_b64decode(cursor))
    for scope in ({'end':'2024-03-15T01:00:00+01:00'}, {'start':'2023-09-02T00:00:00.000000Z'},
                  {'end':'9999-12-31T23:59:59-01:00'}, {'end':'0001-01-01T00:00:00+01:00'}):
        changed = {**token, 'scope':token['scope'] | scope}
        with pytest.raises(ExposureRequestError, match='cursor'):
            monthly_exposure(store, now_utc=now, project_key=key, delivery_cursor=base64.urlsafe_b64encode(json.dumps(changed).encode()).decode())


def test_unavailable_history_is_not_zero(archive):
    store, _, _, _ = archive
    now = datetime(2024,3,15,tzinfo=timezone.utc)
    store.conn.execute('DELETE FROM schema_migrations WHERE name=?',(rule_revisions.MIGRATION,));store.commit()
    unavailable = monthly_exposure(store, now_utc=now)['deliveries']
    assert unavailable['count'] is None and unavailable['by_day'] == {}
    assert unavailable['reason'] == 'schema_unavailable'


def test_api_pages_across_month_boundary_and_keeps_current_selector_max(archive):
    pytest.importorskip('fastapi')
    from fastapi.testclient import TestClient
    from self_improve.dashboard.app import create_app
    store, key, cfg, _ = archive
    clock = [datetime(2024,3,31,tzinfo=timezone.utc)]
    before = list(store.conn.iterdump())
    with TestClient(create_app(cfg, clock=lambda:clock[0])) as client:
        first = client.get('/api/incident-rate',params={'project_key':key}).json()
        clock[0] = datetime(2024,4,2,tzinfo=timezone.utc)
        response = client.get('/api/incident-rate',params={'project_key':key,'delivery_cursor':first['deliveries']['next_cursor']})
        assert response.status_code == 200
        second = response.json()
        assert second['requested'] == first['requested']
        assert second['partial_month'] == '2024-04'
        assert len(second['deliveries']['records']) == 3
        assert second['deliveries']['by_day'] == first['deliveries']['by_day']
        legacy = json.loads(base64.urlsafe_b64decode(first['deliveries']['next_cursor']))
        legacy['scope'] = {k:v for k,v in legacy['scope'].items() if k in {'project_key','start','end'}}
        response = client.get('/api/incident-rate',params={'project_key':key,'delivery_cursor':base64.urlsafe_b64encode(json.dumps(legacy).encode()).decode()})
        assert response.status_code == 200 and response.json()['requested'] == first['requested']
    assert list(store.conn.iterdump()) == before


def test_global_revisions_only_in_all_projects_and_damaged_archive_fails(archive):
    store, key, _, _ = archive
    original = rule_revisions.retained_revisions(store)[0]
    with store.transaction():
        record = {**original, 'application_event_id':'invented-global',
                  'project_key':''}
        record['id'] = rule_revisions.digest({k:v for k,v in record.items() if k not in {'id','captured_at'}})
        rule_revisions.record_revision(store, record)
    now = datetime(2024,3,15,tzinfo=timezone.utc)
    assert monthly_exposure(store,now_utc=now)['deliveries']['count'] == 24
    assert monthly_exposure(store,now_utc=now,project_key=key)['deliveries']['count'] == 23
    store.conn.execute("UPDATE rule_revisions SET record_json='{}'");store.commit()
    with pytest.raises(rule_revisions.AvailabilityError):
        monthly_exposure(store,now_utc=now)
