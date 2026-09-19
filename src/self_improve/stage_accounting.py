"""Pure interpretation of native pipeline counters; no inferred missing outcomes."""
from .failure_presentation import taxonomy_rows

# Independent of pipeline.STAGE_INVARIANTS; tests reconcile the declarations.
OUTCOMES = {
    'scan': ('files_attempted', ('files_succeeded', 'files_failed')),
    'mine': ('attempted', ('succeeded', 'failed')),
    'gate': ('attempted', ('gated_pass', 'gated_fail', 'ungated', 'inconclusive', 'failed', 'refused')),
    'apply': ('attempted', ('applied', 'held', 'failed')),
    'cluster': ('merge_attempted', ('merge_succeeded', 'merge_failed')),
}
MEANINGS = {
    'scan': 'Counts transcript-file scan passes, not distinct sessions. Rescanning a file is another pass. Skips and detected incidents are separate records.',
    'mine': 'Counts incident mining attempts. A processed incident can create, amend or identify a duplicate rule. The recorded failed bucket includes an incident refused at the call cap; remaining unattempted incidents are separate.',
    'cluster': 'Counts model merge calls separately from candidate rules. Candidates can include older pending work. Agentic mode passes candidates through without merge calls; absorption into pending work is separate.',
    'gate': 'Counts proposal evaluations. Pass, fail, ungated and inconclusive are recorded verdict buckets, not all successful rule trials. Exceptions and budget refusals have separate outcomes. Duplicate drops occur before evaluation.',
    'apply': 'Counts proposal delivery attempts. Applied edits, held proposals and failed attempts are separate outcomes. A hold is a recorded policy outcome, not a file write. Routing errors can occur before an apply attempt.',
}
UNITS = {'scan':'file scan passes', 'mine':'incident mining attempts',
         'cluster':'model merge calls', 'gate':'proposal evaluations', 'apply':'proposal delivery attempts'}
COUNT_FIELDS = {
    'scan': ('files_skipped_unchanged',), 'mine': (),
    'cluster': ('candidates', 'open_learnings', 'clusters', 'absorbed_into_pending',
                'open_passthrough_clusters', 'merged'),
    'gate': ('dup_dropped', 'dup_dropped_by_miner'), 'apply': (),
}


def mining_failure_counts(payload):
    """Separate explicit call-cap refusals from the native failed bucket.

    Missing failed counts remain unknown. An absent refusal counter supplies
    no evidence for subtracting anything from a known failed count.
    """
    if not isinstance(payload, dict):
        raise ValueError('mine: expected an object')
    recorded = payload.get('failed')
    if 'failed' in payload and (type(recorded) is not int or recorded < 0):
        raise ValueError('mine.failed: expected a nonnegative integer')
    taxonomy = payload.get('taxonomy', {})
    taxonomy_rows(taxonomy, owner='mine.taxonomy')
    refused = taxonomy.get('budget_refused_this_incident', 0 if recorded is not None else None)
    if recorded is not None and refused > recorded:
        raise ValueError('mine.budget_refused_this_incident: exceeds the recorded failed bucket')
    return {'recorded':recorded, 'refused':refused,
            'execution':None if recorded is None else recorded-refused}


def required(stage, payload):
    if stage not in OUTCOMES:
        raise ValueError('unknown stage '+str(stage))
    if stage == 'cluster' and payload.get('mode') == 'agentic_passthrough':
        return ('candidates',)
    attempt, outcomes = OUTCOMES[stage]
    return (attempt, *outcomes) + (('candidates',) if stage == 'cluster' else ())


def missing_fields(stage, payload):
    return [key for key in required(stage, payload) if key not in payload]


def numbers(stage, payload):
    if not isinstance(payload, dict):
        return None
    attempt_key, outcome_keys = OUTCOMES[stage]
    for key in (attempt_key, *outcome_keys, *COUNT_FIELDS[stage]):
        if key in payload and (type(payload[key]) is not int or payload[key] < 0):
            raise ValueError(f'{stage}.{key}: expected a nonnegative integer')
    if 'taxonomy' in payload:
        taxonomy_rows(payload['taxonomy'], owner=stage+'.taxonomy')
    if missing_fields(stage, payload):
        return None
    passthrough = stage == 'cluster' and payload.get('mode') == 'agentic_passthrough'
    if passthrough and any(payload.get(k, 0) for k in (attempt_key, *outcome_keys)):
        raise ValueError('cluster: agentic passthrough contradicts nonzero merge calls')
    attempted = 0 if passthrough else payload[attempt_key]
    failed_key = 'files_failed' if stage == 'scan' else 'merge_failed' if stage == 'cluster' else 'failed'
    failed = 0 if passthrough else payload[failed_key]
    refused = payload['refused'] if stage == 'gate' else 0
    succeeded = 0 if passthrough else sum(payload[k] for k in outcome_keys if k not in (failed_key, 'refused'))
    taxonomy = payload.get('taxonomy', {})
    refused_in_failed = mining_failure_counts(payload)['refused'] if stage == 'mine' else 0
    number = payload['candidates'] if stage == 'cluster' else payload['applied'] if stage == 'apply' else succeeded
    label = {'scan':"files scanned (summed over the night's runs)", 'mine':'incidents mined',
             'cluster':'candidate rules', 'gate':'recorded verdicts', 'apply':'edits applied'}[stage]
    result = {'attempted':attempted, 'succeeded':succeeded, 'failed':failed, 'refused':refused,
        'unaccounted':attempted-succeeded-failed-refused, 'number':number, 'number_label':label,
        'unit':UNITS[stage], 'meaning':MEANINGS[stage], 'refused_in_failed':refused_in_failed,
        'not_attempted':{k:taxonomy[k] for k in ('budget_exhausted','wall_deadline_reached','provider_unavailable')
                         if taxonomy.get(k,0)>0},
        'native_outcomes':{k:payload[k] for k in outcome_keys if k in payload}}
    if 'taxonomy' in payload: result['taxonomy'] = dict(taxonomy)
    if 'mode' in payload: result['mode'] = payload['mode']
    if stage == 'cluster': result['passthrough'] = passthrough
    if stage == 'scan': result['files_skipped_unchanged'] = payload.get('files_skipped_unchanged')
    if stage == 'gate': result['verdicts'] = {k:payload[k] for k in outcome_keys if k not in ('failed','refused')}
    if stage == 'apply': result.update(applied=payload['applied'],held=payload['held'])
    return result


def state_from_numbers(nums):
    if nums['unaccounted'] != 0:
        return 'unaccounted'
    failed = nums['failed'] - nums.get('refused_in_failed', 0)
    refused = nums.get('refused', 0) + nums.get('refused_in_failed', 0)
    if failed:
        return 'partial' if nums['succeeded'] else 'failed'
    if refused:
        return 'budget_exhausted' if nums['succeeded'] else 'refused'
    if nums.get('not_attempted'):
        return 'limited'
    return 'ok'
