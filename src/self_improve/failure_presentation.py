"""Pure descriptions of retained outcomes, shared by reports and dashboard readers.

No database, provider, dashboard import or I/O. Counts remain source entries;
these descriptions never infer delivery, queue state or successful recovery.
"""

NOT_FAILURE_TAXONOMY = {'propose_TargetRejected': 'a prior target rejection suppressed proposal generation at that '
                           'target; no eval was attempted',
 'propose_LessonRejected': 'a prior lesson-wide rejection suppressed proposal generation '
                           'everywhere; no eval was attempted',
 'budget_exhausted': "incidents the run's cap never reached — not attempted, not failed",
 'budget_refused_this_incident': 'one incident refused because the cap was reached mid-run',
 'gate_budget_exhausted': "a proposal held because the gate's budget was gone before it was "
                          'asked',
 'wall_deadline_reached': 'incidents left unmined because the run reached its wall-clock '
                          'deadline; not attempted'}

FAILURE_COPY = {'MineParseFailure': {'name': 'No answer we could read',
                      'explanation': 'The mining output could not be parsed as the required '
                                     'JSON. This differs from a call that produced no '
                                     'output.'},
 'call_failed': {'name': 'The call never produced an answer',
                 'explanation': 'The provider call did not yield an accepted answer. The '
                                'retained suffix identifies its recorded outcome.'},
 'gate_sandbox_unverified': {'name': 'Sandbox containment was not verified',
                             'explanation': 'The sandbox verification did not establish '
                                            'containment. This evaluation trial was not run; '
                                            'the event supplies no verdict about the rule.'},
 'provider_unavailable': {'name': 'No provider could be started',
                          'explanation': 'Preflight found no usable provider, so this work '
                                         'was refused before a model attempt. The count '
                                         'describes the work refused.'},
 'MineContractViolation': {'name': 'Answer was missing required fields',
                           'explanation': 'The answer did not satisfy the required fields, '
                                          'types, values or relationships. Valid JSON alone '
                                          'is not a valid mining result.'},
 'IntegrityError': {'name': 'A database constraint rejected a write',
                    'explanation': 'SQLite refused a write because a database constraint was '
                                   'violated. The retained suffix names the recorded '
                                   'constraint. Without a suffix, the constraint and its '
                                   'cause are unknown.'},
 'parse_error': {'name': 'No answer we could read',
                 'explanation': 'Output arrived but could not be read in the required '
                                'response format. Empty stdout has the separate empty_output '
                                'outcome.'},
 'empty_output': {'name': 'The call produced no output',
                  'explanation': 'The provider exited cleanly and wrote nothing to stdout, so '
                                 'there was no answer to parse.'},
 'timeout': {'name': 'The call exceeded its deadline',
             'explanation': 'The call passed its deadline and was stopped. This does not '
                            'establish a rule verdict.'},
 'max_turns': {'name': 'The agent ran out of turns',
               'explanation': 'The provider reported its turn limit before returning an '
                              'accepted answer.'},
 'model_mismatch': {'name': 'A different model answered',
                    'explanation': 'The response envelope named a model other than the one '
                                   'requested, so the answer was refused.'},
 'quota_exhausted': {'name': 'Out of quota',
                     'explanation': "The account's usage window was spent."},
 'spawn_error': {'name': 'The CLI would not start',
                 'explanation': 'The agent process failed to launch.'},
 'auth_failed': {'name': 'Authentication failed',
                 'explanation': 'The provider rejected authentication. The record does not '
                                'establish whether a later login or retry succeeded.'},
 'sandbox_denied': {'name': 'The sandbox refused the agent',
                    'explanation': 'The provider reported a sandbox or permission denial. '
                                   'Inspect the trial record before drawing a conclusion '
                                   'about the rule; this outcome alone does not establish a '
                                   'valid evaluation.'},
 'sandbox_incapable_fleet': {'name': 'No account could run a sandboxed trial',
                             'explanation': 'The router refused this trial because the '
                                            'available providers could not satisfy its '
                                            'sandbox requirements. This is a preflight '
                                            'refusal, not a failed rule evaluation.'},
 'other': {'name': 'Unclassified failure',
           'explanation': 'A failure was recorded without a more specific cause.'}}

FAILURE_PREFIX_COPY = (('gate_',
  {'name': 'The gate stage raised',
   'explanation': 'The gate stage raised an exception. The suffix records its class; inspect '
                  'the retained attempt for how far evaluation progressed.'}),
 ('propose_',
  {'name': 'The propose stage raised',
   'explanation': 'The proposal stage raised an exception. Its class does not establish a '
                  'more specific cause or a later delivery outcome.'}),
 ('prune_',
  {'name': 'The prune stage raised',
   'explanation': 'The prune stage raised an exception. Inspect the retained operation before '
                  'inferring whether any edit occurred.'}),
 ('not_appliable_',
  {'name': 'Not in a state the apply stage can act on',
   'explanation': 'The apply stage reached a proposal whose status it does not act on. The '
                  'suffix is that status. Nothing was written.'}))


def failure_copy(cls: str) -> dict | None:
    """Exact names first; a known suffix can explain a failed call more precisely."""
    if cls in NOT_FAILURE_TAXONOMY:
        return None
    if cls in FAILURE_COPY:
        return dict(FAILURE_COPY[cls])
    family, sep, detail = cls.partition(':')
    if sep and family in FAILURE_COPY:
        result = dict(FAILURE_COPY[family])
        if family == 'call_failed':
            specific = FAILURE_COPY.get(detail)
            if specific:
                result['explanation'] += ' ' + specific['explanation']
        return result
    for prefix, copy in FAILURE_PREFIX_COPY:
        if cls.startswith(prefix):
            return dict(copy)
    return None


def taxonomy_rows(taxonomy, *, owner: str) -> list[dict]:
    """Retain identifiers and counts; reject corrupt shapes without silent zeros."""
    if not isinstance(taxonomy, dict):
        raise ValueError(owner + ': expected an object of outcome counts')
    rows = []
    for cls, count in taxonomy.items():
        if not isinstance(cls, str) or not cls or type(count) is not int or count < 0:
            raise ValueError(f'{owner}: invalid count or identifier for {cls!r}')
        if count == 0:
            continue
        copy = failure_copy(cls)
        kind = 'not_failure' if cls in NOT_FAILURE_TAXONOMY else 'failure' if copy else 'unknown'
        rows.append({'class':cls, 'count':count, 'kind':kind,
            'name':copy['name'] if copy else 'Work not performed' if kind == 'not_failure' else 'Unclassified outcome',
            'explanation':copy['explanation'] if copy else NOT_FAILURE_TAXONOMY[cls]
                if kind == 'not_failure' else 'No explanation is registered for this recorded identifier.'})
    return rows
