"""Monthly active scan projections. The caller owns the Store and read transaction.

Validate contributing manifests through the scanner's public exposure reader,
then aggregate physical lines by their own UTC timestamps. SQL grouping keeps
large transcripts out of Python and deduplicates session identities globally.
No queue rows, filesystem reads, model calls, or current detector code infer data.
"""
import base64
import json
import re
import statistics
from datetime import datetime, timezone

from .. import rule_revisions, scan_observations as scans
from .scan_data import ExposureRequestError, REASONS


def _month(index):
    return f'{index // 12:04d}-{index % 12 + 1:02d}'


def _index(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{4}-(0[1-9]|1[0-2])', value):
        raise ExposureRequestError('end_month must use YYYY-MM')
    return int(value[:4]) * 12 + int(value[5:]) - 1


def _delivery_cursor(cursor):
    if cursor is None:
        return None
    try:
        token = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        scope, after = token['scope'], token['after']
        if (set(scope) not in ({'project_key', 'start', 'end'},
                              {'project_key', 'start', 'end', 'months', 'end_month'})
                or not isinstance(after, list) or len(after) != 2
                or not all(isinstance(v, str) and v for v in after)
                or scans.normalize_timestamp(after[0]) != after[0]
                or not re.fullmatch(r'[a-f0-9]{64}', after[1])
                or any(not isinstance(scope[k], str) or scans.normalize_timestamp(scope[k]) != scope[k]
                       for k in ('start', 'end'))
                or not scope['start'] <= after[0] < scope['end']
                or scope['start'][7:] != '-01T00:00:00.000000Z'):
            raise ValueError('invalid scope or position')
        return token
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
        raise ExposureRequestError('Invalid delivery cursor for this trend selection') from exc


def _deliveries(store, requested, token):
    scope = {k: requested[k] for k in ('project_key', 'start', 'end', 'months', 'end_month')}
    after = token['after'] if token else None
    if token and any(scope.get(k) != v for k, v in token['scope'].items()):
        raise ExposureRequestError('Invalid delivery cursor for this trend selection')
    out = {'records': [], 'count': None, 'next_cursor': None, 'by_month': {}, 'by_day': {}, 'reason': '',
           'note': 'Retained rule revisions use the application event timestamp. Older applications without a retained revision are unknown. Delivery does not prove instruction availability, session receipt, or a causal effect.'}
    if not store.query_one('SELECT name FROM schema_migrations WHERE name=?', (rule_revisions.MIGRATION,)):
        out['reason'] = 'schema_unavailable'
        return out
    records = [r for r in rule_revisions.retained_revisions(store)
               if requested['start'] <= r['applied_at'] < requested['end']
               and (requested['project_key'] is None or r['project_key'] == requested['project_key'])]
    records.sort(key=lambda r: (r['applied_at'], r['id']), reverse=True)
    out['count'] = len(records)
    for r in records:
        month = r['applied_at'][:7]
        out['by_month'][month] = out['by_month'].get(month, 0) + 1
        day = r['applied_at'][:10]
        out['by_day'][day] = out['by_day'].get(day, 0) + 1
    page = [r for r in records if after is None or [r['applied_at'], r['id']] < after]
    out['records'] = [{k: r[k] for k in ('id', 'learning_id', 'proposal_id', 'application_id',
                                        'application_event_id', 'applied_at', 'project_key', 'content_hash')}
                      for r in page[:20]]
    if len(page) > 20:
        last = page[19]
        out['next_cursor'] = base64.urlsafe_b64encode(json.dumps(
            {'scope': scope, 'after': [last['applied_at'], last['id']]}, sort_keys=True).encode()).decode()
    return out


def monthly_exposure(store, *, now_utc, project_key=None, compatibility_key=None,
                     months=7, end_month=None, min_sessions=20, delivery_cursor=None):
    """One version at a time, across one canonical project or all known projects.

    Missing periods remain visible. Small samples retain observed arithmetic but
    have no primary trend rate. Unknown-time and failed-reconciliation coverage is unallocated,
    reported once per project, and never copied into each month's denominator.
    """
    if not isinstance(now_utc, datetime) or now_utc.tzinfo is None or now_utc.utcoffset() is None:
        raise ExposureRequestError('now_utc must be a timezone-aware datetime')
    for name, value in (('project_key', project_key), ('compatibility_key', compatibility_key)):
        if value is not None and (not isinstance(value, str) or not value):
            raise ExposureRequestError(f'{name} must be non-empty when supplied')
    if type(months) is not int or not 1 <= months <= 24:
        raise ExposureRequestError('months must be an integer from 1 to 24')
    if type(min_sessions) is not int or min_sessions < 20:
        raise ExposureRequestError('min_sessions must be an integer of at least 20')
    clock = now_utc.astimezone(timezone.utc)
    latest_month = f'{clock.year:04d}-{clock.month:02d}'
    token = _delivery_cursor(delivery_cursor)
    cursor_anchor = None
    if token:
        frozen_end = token['scope']['end']
        if frozen_end > scans.normalize_timestamp(clock.isoformat()):
            raise ExposureRequestError('Invalid future delivery cursor')
        clock = datetime.fromisoformat(frozen_end.replace('Z', '+00:00'))
        # Derive from start, not end minus an instant: a current-month request
        # made exactly at midnight on day one still includes that empty month.
        cursor_anchor = _month(_index(token['scope']['start'][:7]) + months - 1)
    current = f'{clock.year:04d}-{clock.month:02d}'
    anchor = _index(end_month or cursor_anchor or current)
    if anchor > _index(current) or anchor - months + 1 < 12:
        raise ExposureRequestError('The requested months must be in years 0001–9999 and not in the future')
    start = _month(anchor - months + 1) + '-01T00:00:00.000000Z'
    # Avoid constructing year 10000 when the explicit clock is at year 9999.
    end = scans.normalize_timestamp(clock.isoformat()) if anchor == _index(current) else _month(anchor + 1) + '-01T00:00:00.000000Z'
    if start == end:
        raise ExposureRequestError('The requested interval has no elapsed time; select an earlier month or more months')
    requested = dict(project_key=project_key, compatibility_key=compatibility_key,
                     months=months, end_month=_month(anchor), start=start, end=end)
    result = {'contract_version': 3, 'requested': requested, 'partial_month': latest_month,
              'primary_series': 'signal_occurrences_per_100k_physical_lines', 'min_sessions': min_sessions,
              'series': [], 'dropped_months': [], 'version_groups': [], 'compatibility_key': None,
              'project_options': [], 'coverage_by_project': [], 'computable': False,
              'reason': '', 'reason_text': '', 'signal_types': list(scans.SIGNALS),
              'denominator_note': 'Counts use uncapped signal occurrences and eligible physical JSONL lines at their own timestamps. One version is selected; incompatible versions are never pooled. Different signals can describe the same underlying mistake.',
              'retention_note': 'Only retained scan observations are represented. An unavailable month has no primary rate; inspect its named cause and raw observations. Earlier deleted transcripts are unknown. Current-month totals stop at the reference time.',
              'deliveries': _deliveries(store, requested, token)}

    def unavailable(reason):
        text = (f'No month has at least {min_sessions} known sessions. Raw counts and observed arithmetic remain inspectable; trend rates are unavailable.'
                if reason == 'insufficient_sessions' else REASONS[reason])
        result.update(reason=reason, reason_text=text)
        return result

    if not store.query_one('SELECT name FROM schema_migrations WHERE name=?', (scans.MIGRATION,)):
        return unavailable('schema_unavailable')
    scans.require_schema(store)
    projects = [r['project_key'] for r in store.query(
        "SELECT project_key FROM scan_lines WHERE active=1 AND project_key<>'' UNION "
        "SELECT project_key FROM scan_occurrences WHERE active=1 AND project_key<>'' ORDER BY project_key")]
    result['project_options'] = projects
    result['unassigned_lines'] = store.query(
        "SELECT compatibility_key,exclusion,COUNT(*) count FROM scan_lines WHERE active=1 "
        "AND project_key='' GROUP BY compatibility_key,exclusion") if project_key is None else []
    selected_projects = [project_key] if project_key is not None else projects
    windows, versions = [], {}
    for key in selected_projects:
        window = scans.exposure_window(store, project_key=key, start=start, end=end,
                                       compatibility_key=compatibility_key)
        windows.append(window)
        for group in window['version_groups']:
            version = versions.setdefault(group['compatibility_key'], {
                'compatibility_key': group['compatibility_key'], 'identifiable': group['identifiable'],
                'manifests': {}, 'months': []})
            for manifest in group['manifests']:
                version['manifests'][scans.content_id(manifest)] = manifest
    # The public reader validates both line and occurrence projections against
    # their saved observation and manifest before any grouped count becomes a rate.
    scope = "active=1 AND project_key<>''"
    params = []
    if project_key is not None:
        scope += ' AND project_key=?'
        params.append(project_key)
    dated = scope + ' AND occurred_at>=? AND occurred_at<?'
    dated_params = [*params, start, end]
    bands = store.query('SELECT compatibility_key,substr(occurred_at,1,7) month,COUNT(*) n '
                        f"FROM scan_lines WHERE {dated} AND exclusion='' GROUP BY compatibility_key,month",
                        dated_params)
    for band in bands:
        versions[band['compatibility_key']]['months'].append({'month': band['month'], 'eligible_lines': band['n']})
    result['version_groups'] = [{**v, 'manifests': list(v['manifests'].values())}
                                for _, v in sorted(versions.items())]
    if not versions:
        return unavailable('missing_observations')
    if compatibility_key is not None and compatibility_key not in versions:
        return unavailable('uncovered_version')
    if compatibility_key is None and len(versions) > 1:
        return unavailable('incompatible_versions')
    chosen = compatibility_key or next(iter(versions))
    result['compatibility_key'] = chosen
    identifiable = versions[chosen]['identifiable']
    result['coverage_by_project'] = [
        {'project_key': w['requested']['project_key'], 'coverage': w['coverage'],
         'diagnostics': w['diagnostics'], 'reason': w['reason']}
        for w in windows if w['compatibility_key'] == chosen]
    points = {}
    for index in range(anchor - months + 1, anchor + 1):
        month = _month(index)
        points[month] = {'month': month, 'partial': month == current,
            'eligible_lines': 0, 'observed_lines': 0, 'excluded_lines': {},
            'occurrences': 0, 'signals': {s: {'count': 0, 'rate_per_100k': None} for s in scans.SIGNALS},
            'sessions': 0, 'transcripts': 0, 'projects': 0, 'unknown_session_lines': 0,
            'workload': {'by_source': {}, 'headless': 0, 'subagent': 0, 'partial_detector_lines': 0},
            'deliveries': result['deliveries']['by_month'].get(month) if result['deliveries']['count'] is None
                          else result['deliveries']['by_month'].get(month, 0)}
    rows = store.query('SELECT substr(occurred_at,1,7) month,exclusion,logical_session_key,transcript_id,'
                       'project_key,source,headless,is_subagent,detector_coverage,COUNT(*) n '
                       f'FROM scan_lines WHERE {dated} AND compatibility_key=? '
                       'GROUP BY month,exclusion,logical_session_key,transcript_id,project_key,source,headless,is_subagent,detector_coverage',
                       [*dated_params, chosen])
    sizes, transcripts, project_sets = {}, {}, {}
    for row in rows:
        month, n = row['month'], row['n']
        p = points[month]
        p['observed_lines'] += n
        if row['exclusion']:
            cause = row['exclusion']
            p['excluded_lines'][cause] = p['excluded_lines'].get(cause, 0) + n
            continue
        p['eligible_lines'] += n
        workload = p['workload']
        workload['by_source'][row['source']] = workload['by_source'].get(row['source'], 0) + n
        workload['headless'] += n if row['headless'] else 0
        workload['subagent'] += n if row['is_subagent'] else 0
        workload['partial_detector_lines'] += n if row['detector_coverage'] == 'partial' else 0
        transcripts.setdefault(month, set()).add(row['transcript_id'])
        project_sets.setdefault(month, set()).add(row['project_key'])
        if row['logical_session_key']:
            by_session = sizes.setdefault(month, {})
            key = row['logical_session_key']
            by_session[key] = by_session.get(key, 0) + n
        else:
            p['unknown_session_lines'] += n
    for row in store.query('SELECT substr(occurred_at,1,7) month,signal_type,COUNT(*) n '
                           f"FROM scan_occurrences WHERE {dated} AND compatibility_key=? AND kind='signal' AND exclusion='' "
                           'GROUP BY month,signal_type', [*dated_params, chosen]):
        if row['signal_type'] not in scans.SIGNALS:
            raise scans.ScanObservationError('Unknown primary signal type: '+row['signal_type'])
        p = points[row['month']]
        p['signals'][row['signal_type']]['count'] += row['n']
        p['occurrences'] += row['n']
    for month, p in points.items():
        values = list(sizes.get(month, {}).values())
        p.update(sessions=len(values), transcripts=len(transcripts.get(month, set())),
                 projects=len(project_sets.get(month, set())),
                 session_size={'total': sum(values), 'median': statistics.median(values) if values else None,
                               'max': max(values) if values else None}, small_sample=len(values) < min_sessions)
        p['observed_rate_per_100k'] = 100000 * p['occurrences'] / p['eligible_lines'] if identifiable and p['eligible_lines'] else None
        p['reason'] = ('unknown_version' if not identifiable else 'zero_eligible_exposure'
                       if not p['eligible_lines'] else 'insufficient_sessions' if p['small_sample'] else '')
        p['rate_per_100k'] = None if p['reason'] else p['observed_rate_per_100k']
        for signal in p['signals'].values():
            signal['observed_rate_per_100k'] = 100000 * signal['count'] / p['eligible_lines'] if identifiable and p['eligible_lines'] else None
            signal['rate_per_100k'] = None if p['reason'] else signal['observed_rate_per_100k']
    result['series'] = list(points.values())
    result['computable'] = any(p['rate_per_100k'] is not None for p in result['series'])
    if result['computable']:
        return result
    return unavailable('unknown_version' if not identifiable else 'insufficient_sessions'
                       if any(p['eligible_lines'] for p in result['series']) else 'zero_eligible_exposure')
