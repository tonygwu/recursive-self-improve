"""Rate adequacy through real temporary scanner and immutable record readers."""
import uuid

import pytest

from self_improve.dashboard import queries
from tests.test_monthly_trends import NOW
from tests.test_scan_observations import make_env, correction_session, write_claude, scan
from tests.test_session_context import env


def monthly_cohort(env, count, *, month=9, year=2026, correction=True, extra_lines=0):
    """Invented four-line sessions, with optional extra workload and no calls."""
    from tests.test_scan_observations import c_user
    for i in range(count):
        sid = str(uuid.uuid5(uuid.NAMESPACE_URL, f'rate-{month}-{i}'))
        records = correction_session(env.repo)
        if not correction:
            records[-1]['message']['content'] = 'Looks correct'
        records += [c_user('More ordinary work', 'unused', env.repo) for _ in range(extra_lines)]
        for index, record in enumerate(records):
            record.update(sessionId=sid, timestamp=f'{year}-{month:02d}-02T01:{i:02d}:{index:02d}Z')
        write_claude(env, records, name=sid)


@pytest.mark.parametrize('count', [1, 19, 20])
@pytest.mark.parametrize('correction', [False, True])
def test_monthly_primary_rate_requires_twenty_sessions_and_preserves_arithmetic(tmp_path, count, correction):
    with_env = make_env(tmp_path)
    try:
        monthly_cohort(with_env, count, correction=correction)
        scan(with_env, 'adequacy')
        result = queries.incident_rate(with_env.store, now_utc=NOW)
        p = result['series'][-1]
        raw = 25000 if correction else 0
        assert p['sessions'] == count and p['eligible_lines'] == 4 * count
        assert p['occurrences'] == (count if correction else 0)
        assert p['rate_per_100k'] == (raw if count >= 20 else None)
        assert p['observed_rate_per_100k'] == raw
        assert p['signals']['correction']['rate_per_100k'] == p['rate_per_100k']
        assert p['signals']['correction']['observed_rate_per_100k'] == raw
        assert p['reason'] == ('' if count >= 20 else 'insufficient_sessions')
        assert result['reason'] == p['reason'] and result['computable'] == (count >= 20)
        assert p['session_size'] == {'total': count * 4, 'median': 4, 'max': 4}
        assert p['workload']['by_source'] == {'claude': count * 4}
        assert result['series'][0]['reason'] == 'zero_eligible_exposure'
        assert result['series'][0]['observed_rate_per_100k'] is None
    finally:
        with_env.store.close()


def test_mixed_months_keep_workload_and_small_zero_distinct_from_adequate_zero(tmp_path):
    e = make_env(tmp_path)
    try:
        monthly_cohort(e, 1, month=7, correction=False)
        monthly_cohort(e, 20, month=8, correction=False, extra_lines=4)
        monthly_cohort(e, 20, month=9)
        scan(e, 'mixed')
        result = queries.incident_rate(e.store, now_utc=NOW)
        jul, aug, sep = result['series'][-3:]
        assert [p['rate_per_100k'] for p in (jul, aug, sep)] == [None, 0, 25000]
        assert [p['session_size']['median'] for p in (jul, aug, sep)] == [4, 8, 4]
        assert result['computable'] and result['reason'] == ''
        stricter = queries.incident_rate(e.store, now_utc=NOW, min_sessions=21)
        assert stricter['reason'] == 'insufficient_sessions'
        assert all(p['rate_per_100k'] is None for p in stricter['series'])
    finally:
        e.store.close()


@pytest.mark.parametrize('threshold', [0, 1, 19, True, 20.0])
def test_monthly_minimum_cannot_be_lowered(tmp_path, threshold):
    from self_improve.dashboard.scan_data import ExposureRequestError
    e = make_env(tmp_path)
    try:
        with pytest.raises(ExposureRequestError, match='at least 20'):
            queries.incident_rate(e.store, now_utc=NOW, min_sessions=threshold)
    finally:
        e.store.close()


def test_recurrence_summary_preserves_workload_without_rewriting_record(env):
    from self_improve import project_measurements as pm
    from tests.test_project_measurements import cohort, calculate, publish
    from tests.test_scan_observations import PROJECT
    revision, key = cohort(env, after=19)
    original = calculate(env, revision, key)
    rid = publish(env, original)
    archive = env.store.query_one('SELECT * FROM project_stats WHERE id=?', (rid,))
    global_row = pm.measurement_history(env.store)['records'][0]
    project_row = pm.measurement_history(env.store, project_key=PROJECT)['records'][0]
    assert global_row == project_row
    assert global_row['after']['rate_per_100k'] is None
    assert global_row['before']['rate_per_100k'] == original['before']['rate_per_100k']
    assert global_row['min_sessions'] == 20
    assert global_row['after']['reason'] == 'insufficient_sessions'
    for side in ('before', 'after'):
        for name in ('session_size', 'workload'):
            assert global_row[side][name] == original[side][name]
        assert global_row[side]['observed_rate_per_100k'] == original[side]['rate_per_100k']
    assert pm.measurement_detail(env.store, rid)['measurement'] == original
    assert env.store.query_one('SELECT * FROM project_stats WHERE id=?', (rid,)) == archive
    assert env.store.query_one('SELECT COUNT(*) n FROM llm_calls')['n'] == 0


def test_unknown_sessions_do_not_make_small_samples_adequate_and_late_data_can(tmp_path):
    from tests.test_scan_observations import c_user
    from self_improve.scan import mark_for_rescan
    e = make_env(tmp_path)
    try:
        records = [c_user('Ordinary work', '2026-09-02T00:00:00Z', e.repo) for _ in range(30)]
        for r in records:
            del r['sessionId']
        unknown_time = c_user('Time not retained', '', e.repo)
        write_claude(e, records + [unknown_time], name='unknown-session')
        scan(e, 'unknown')
        first = queries.incident_rate(e.store, now_utc=NOW)
        p = first['series'][-1]
        assert (p['eligible_lines'], p['sessions'], p['unknown_session_lines']) == (30, 0, 30)
        assert p['observed_rate_per_100k'] == 0 and p['rate_per_100k'] is None
        assert p['reason'] == 'insufficient_sessions' and p['session_size']['median'] is None
        assert first['coverage_by_project'][0]['coverage']['counts_by_cause']['unknown_time_lines'] == 1
        monthly_cohort(e, 19)
        scan(e, 'late-19')
        small = queries.incident_rate(e.store, now_utc=NOW)['series'][-1]
        assert small['sessions'] == 19 and small['rate_per_100k'] is None
        monthly_cohort(e, 20)
        mark_for_rescan(e.store, e.cfg)
        scan(e, 'late-20')
        enough = queries.incident_rate(e.store, now_utc=NOW)['series'][-1]
        assert enough['sessions'] == 20 and enough['eligible_lines'] == 110
        assert enough['rate_per_100k'] == 100000 * 20 / 110
        assert enough['unknown_session_lines'] == 30 and enough['session_size']['total'] == 80
        mark_for_rescan(e.store, e.cfg)
        scan(e, 'repeat')
        assert queries.incident_rate(e.store, now_utc=NOW)['series'][-1] == enough
        for path in e.claude_dir.rglob('*.jsonl'):
            path.unlink()
        assert queries.incident_rate(e.store, now_utc=NOW)['series'][-1] == enough
    finally:
        e.store.close()


@pytest.mark.parametrize('after,missing_links', [(20, False), (0, False), (20, True)])
def test_recurrence_summary_preserves_zero_missing_matching_and_empty_exposure(env, after, missing_links):
    from self_improve import project_measurements as pm
    from tests.test_project_measurements import cohort, calculate, publish
    revision, key = cohort(env, after=after, after_matches=0)
    if missing_links:
        env.store.conn.execute('DELETE FROM incident_learnings')
        env.store.commit()
    original = calculate(env, revision, key)
    rid = publish(env, original)
    summary = pm.measurement_history(env.store)['records'][0]['after']
    reason = 'missing_matching_evidence' if missing_links else 'zero_eligible_exposure' if not after else ''
    assert summary['reason'] == reason
    assert summary['rate_per_100k'] == (None if reason else 0)
    assert summary['observed_rate_per_100k'] == original['after']['rate_per_100k']
    assert pm.measurement_detail(env.store, rid)['measurement'] == original
