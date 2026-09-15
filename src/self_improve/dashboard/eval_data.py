"""Read-only presentation of retained attempts and unlinked historical results."""
from __future__ import annotations

from .. import eval_history
from .queries import eval_story


def outcome(attempt):
    """Keep execution causes, gate decisions and comparable evidence separate."""
    def label(code, title, reason, tone='partial'):
        return dict(code=code, label=title, reason=reason, tone=tone)
    if attempt['comparison_type'] == 'without_rule_only':
        return label('without_rule_only', 'Without-rule rerun',
                     'Only the without-rule arm was planned. This is not a paired comparison.', 'info')
    if attempt['state'] != 'completed':
        stops = [e['data'] for e in attempt['events'] if e['kind'] == 'attempt_stopped']
        code = stops[-1].get('code', '') if stops else ''
        reason = stops[-1].get('detail', '') if stops else attempt['reason']
        if 'Budget' in code:
            return label('budget_refused', 'Budget refused', reason)
        if code in ('KeyboardInterrupt', 'SystemExit', 'InterruptedStep', 'InterruptedCall'):
            return label('interrupted', 'Interrupted evaluation', reason)
        if code == 'JobCancelled':
            return label('cancelled', 'Evaluation cancelled', reason)
        if any(e['kind'] == 'generation_failed' for e in attempt['events']):
            return label('generation_failed', 'Scenario generation failed', reason, 'error')
        return label('incomplete', 'No complete verdict', reason, 'error' if stops else 'info')
    scenarios = attempt['scenarios']
    exclusions = {cause for s in scenarios for a in s['arms'].values() for cause in a['exclusions']}
    if exclusions & {'agent_error', 'grader_error', 'asked_operator'}:
        return label('harness_failed', 'Execution failure in trials',
                     'At least one trial could not supply a graded rule outcome. Inspect every scenario.', 'error')
    if any(s['arms']['with']['skipped'] for s in scenarios):
        return label('invalid_scenario', 'Scenario did not establish a comparison',
                     'At least one with-rule arm was skipped. Its without-rule result did not justify testing the rule.')
    if not all(s['comparison']['computable'] for s in scenarios):
        return label('comparison_unavailable', 'Comparison evidence incomplete',
                     'Missing scenarios, excluded trials or differing served models prevent a complete paired comparison.')
    verdict = (attempt['result'] or {}).get('verdict')
    return {
        'gated_pass': label('rule_helped', 'The rule helped in these scenarios',
                            'The recorded gate passed with comparable evidence in every configured scenario.', 'ok'),
        'gated_fail': label('rule_failed', 'The rule failed its gate',
                            'The gate failure threshold was met. Each scenario retains its own outcome.', 'error'),
    }.get(verdict, label('inconclusive', 'No settled rule outcome',
                        'The retained gate did not establish a passing or failing majority.'))


def attempt_summary(attempt):
    source = attempt['source']
    return {
        'id': source['id'], 'kind': 'attempt', 'created_at': source['created_at'],
        'rule_text': source['learning']['rule_text'], 'learning_id': source['learning_id'],
        'proposal_id': source['proposal_id'], 'run_id': source['run_id'],
        'command_id': source['command_id'], 'source_revision_id': source['source_revision_id'],
        'state': attempt['state'], 'verdict': (attempt['result'] or {}).get('verdict'),
        'outcome': outcome(attempt), 'scenarios': len(attempt['scenarios']),
        'completed_scenarios': sum(s['evaluation'] is not None for s in attempt['scenarios']),
        'paired_scenarios': sum(s['comparison']['computable'] for s in attempt['scenarios']),
        'calls_started': sum(e['kind'] == 'call_started' for e in attempt['events']),
        'calls_recorded': sum(e['kind'] == 'call_result' for e in attempt['events']),
        'observed_arms': {arm: {field: sum(s['arms'][arm][field] for s in attempt['scenarios'])
                                for field in ('observed_passes', 'completed', 'attempted', 'requested_trials')}
                          for arm in ('without', 'with')},
    }


def attempts(store, **options):
    page = eval_history.page(store, **options)
    return {**page, 'records': [attempt_summary(eval_history.detail(store, r['id']))
                              for r in page['records']]}


def attempt(store, attempt_id):
    data = eval_history.detail(store, attempt_id)
    return {**data, 'summary': attempt_summary(data)}


def health(store):
    """Count validated attempts, never promote legacy verdict rows to comparisons."""
    if not eval_history.available(store):
        return {'computable': False, 'attempts': None, 'by_outcome': [],
                'unlinked_results': store.query_one('SELECT COUNT(*) AS n FROM eval_results')['n'],
                'reason': 'Complete attempt provenance is unavailable. Historical verdicts do not establish verified comparisons.'}
    counts = {}
    rows = store.query('SELECT id FROM eval_attempts ORDER BY created_at,id')
    for row in rows:
        classification = outcome(eval_history.detail(store, row['id']))
        code = classification['code']
        counts.setdefault(code, {**classification, 'count': 0})['count'] += 1
    unlinked_count = store.query_one('SELECT COUNT(*) AS n FROM eval_results WHERE '+_unlinked_clause(store))['n']
    return {'computable': True, 'attempts': len(rows), 'by_outcome': list(counts.values()),
            'unlinked_results': unlinked_count,
            'reason': 'Counts describe all recorded attempts, including repeat evaluations. They are not a count of independent rules, a human quality judgment or a real-world improvement rate.'}


def _unlinked_clause(store):
    if not eval_history.available(store):
        return '1=1'
    # Validate the retained links before using their JSON identities in SQL.
    # One pass across scenario links avoids an N-by-N probe of every eval row.
    for row in store.query("SELECT * FROM eval_attempt_events WHERE kind='scenario_result'"):
        record = eval_history._read(row, 'eval_attempt_events')
        data = record['data']
        if not isinstance(data.get('eval_result_id'), str) or not data['eval_result_id']:
            raise eval_history.EvalHistoryError('eval_attempt_events.'+row['id']+': missing evaluation identity')
    return "id NOT IN (SELECT json_extract(record_json,'$.data.eval_result_id') FROM eval_attempt_events WHERE kind='scenario_result')"


def _legacy(row):
    story = {k:v for k,v in eval_story(row).items() if k != 'summary'}
    unpaired = row['kind'] == 'ab'
    return {
        'id': row['id'], 'kind': 'unlinked', 'created_at': row['started'],
        'subject_id': row['subject_id'], 'verdict': row['verdict'],
        'rule_text': None, 'state': 'historical', 'story': story,
        'outcome': {'code': 'without_rule_only' if unpaired else 'historical_unknown',
                    'label': 'Without-rule rerun' if unpaired else 'Historical result · '+row['verdict'],
                    'reason': 'No exact attempt source or served-model comparison is retained for this result.',
                    'tone': 'info'},
        'comparison_type': 'without_rule_only' if unpaired else 'historical_unknown',
    }


def unlinked(store, *, limit=20, cursor=None):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise eval_history.EvalHistoryRequestError('Evaluation result limit must be 1–100.')
    clause = _unlinked_clause(store)
    count = store.query_one('SELECT COUNT(*) AS n FROM eval_results WHERE '+clause)['n']
    args = []
    if cursor:
        row = store.query_one('SELECT id,started FROM eval_results WHERE '+clause+' AND id=?', (cursor,))
        if row is None:
            raise eval_history.EvalHistoryRequestError('Unknown unlinked evaluation cursor.')
        clause += ' AND (started<? OR (started=? AND id<?))'
        args += [row['started'], row['started'], row['id']]
    rows = store.query('SELECT * FROM eval_results WHERE '+clause+' ORDER BY started DESC,id DESC LIMIT ?', (*args, limit+1))
    return {'records': [_legacy(r) for r in rows[:limit]], 'count': count,
            'next_cursor': rows[limit-1]['id'] if len(rows) > limit else None,
            'computable': True,
            'reason': 'Results without an explicit attempt link. Their exact source revisions, producing calls and comparison validity remain unknown.'}


def legacy(store, result_id):
    row = store.query_one('SELECT * FROM eval_results WHERE id=?', (result_id,))
    if row is None:
        raise eval_history.EvalHistoryRequestError('Unknown evaluation result '+result_id, 404)
    links = []
    if eval_history.available(store):
        for event in store.query("SELECT * FROM eval_attempt_events WHERE kind='scenario_result' AND json_extract(record_json,'$.data.eval_result_id')=?", (result_id,)):
            recorded = eval_history._read(event, 'eval_attempt_events')
            if recorded['data'].get('evaluation') != row:
                raise eval_history.EvalHistoryError('eval_attempt_events.'+event['id']+': result differs from retained link')
            links.append({'attempt_id': event['attempt_id'], 'scenario': event['scenario_index']})
    return {'summary': _legacy(row), 'record': row, 'attempt_links': links}
