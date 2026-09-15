"""Immutable evaluation sources and append-only execution evidence.

Low-level append owns no transaction. Recorder owns short publication transactions;
model execution and filesystem work always happen outside those transactions.
"""
from __future__ import annotations

import hashlib
import json

from .store import new_id, utc_now_iso

MIGRATION = '0023_eval_attempts'
TABLES = ('eval_attempts', 'eval_attempt_events')
KINDS = ('generation_started', 'generation_prompt', 'specification', 'generation_failed',
         'trial_started', 'trial_result', 'trial_failed', 'arm_skipped',
         'call_started', 'call_result', 'scenario_result', 'attempt_result', 'attempt_stopped')
CONFIG_FIELDS = ('eval_scenarios', 'eval_trials', 'gate_without_min_failures',
                 'gate_with_min_passes', 'cheap_model_class', 'strong_model_class',
                 'allowed_providers', 'sandbox_incompatible_providers',
                 'llm_timeout_seconds', 'quota_wait_max_seconds', 'eval_sandbox_enabled',
                 'eval_sandbox_npx_package', 'eval_sandbox_allowed_domains',
                 'max_gate_calls_per_run')


class EvalHistoryError(ValueError):
    """Missing schema or inconsistent retained execution evidence."""
    status_code = 500


class EvalHistoryRequestError(EvalHistoryError):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code=status_code


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def available(store):
    installed = bool(store.query_one('SELECT name FROM schema_migrations WHERE name=?', (MIGRATION,)))
    if installed:
        for table in TABLES:
            if not store.query_one("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)):
                raise EvalHistoryError(f'{MIGRATION}: missing table {table}')
    return installed


def require_schema(store):
    if not available(store):
        raise EvalHistoryError('Upgrade the database before evaluation: '+MIGRATION+' is required.')


def _read(row, table):
    owner = table+'.'+row['id']
    try:
        record = json.loads(row['record_json'])
        fingerprint = digest(record)
    except (ValueError, TypeError) as exc:
        raise EvalHistoryError(owner+': invalid JSON') from exc
    if (not isinstance(record, dict) or record.get('version') != 1
            or fingerprint != row['record_hash']
            or any(record.get(k) != v for k, v in row.items() if k not in ('record_json', 'record_hash'))):
        raise EvalHistoryError(owner+': record differs from its shape, index or fingerprint')
    if table == 'eval_attempts':
        if (record.get('kind') not in ('gate','without_rule_rerun')
                or not all(isinstance(record.get(k),dict) for k in ('learning','proposal','settings','retry_policy'))
                or record['learning'].get('id') != record['learning_id']
                or record['proposal'].get('id','') != record['proposal_id']
                or not isinstance(record['learning'].get('rule_text'),str)
                or record.get('source_hash') != digest({'learning':record['learning'],'proposal':record['proposal']})
                or record.get('rule_content_hash') != hashlib.sha256(record['learning']['rule_text'].encode()).hexdigest()
                or set(record['settings']) != set(CONFIG_FIELDS)
                or any(type(record['settings'].get(k)) is not int or record['settings'][k]<1
                       for k in ('eval_scenarios','eval_trials'))):
            raise EvalHistoryError(owner+': invalid frozen source or settings')
    elif (record.get('kind') not in KINDS or not isinstance(record.get('data'),dict)
          or record.get('arm') not in ('','without','with')
          or any(type(record.get(k)) is not int or record[k]<-1 for k in ('scenario_index','trial_index'))):
        raise EvalHistoryError(owner+': invalid event shape')
    return record


def append(store, attempt_id, *, event_key, kind, data, scenario=-1, arm='', trial=-1):
    """Append once in the caller's transaction; replay must carry identical data."""
    require_schema(store)
    if not store.conn.in_transaction or store.read_only:
        raise EvalHistoryError('Evaluation event publication requires a caller-owned write transaction.')
    if (kind not in KINDS or arm not in ('', 'without', 'with')
            or type(scenario) is not int or scenario < -1 or type(trial) is not int or trial < -1
            or not isinstance(data, dict) or not isinstance(event_key, str) or not event_key):
        raise EvalHistoryError('eval_attempts.'+attempt_id+': invalid event fields')
    owner = store.query_one('SELECT * FROM eval_attempts WHERE id=?', (attempt_id,))
    if owner is None:
        raise EvalHistoryError('Unknown evaluation attempt '+attempt_id)
    source = _read(owner, 'eval_attempts')
    if scenario >= source['settings']['eval_scenarios'] or trial >= source['settings']['eval_trials']:
        raise EvalHistoryError('eval_attempts.'+attempt_id+': event exceeds the frozen scenario/trial plan')
    if kind=='call_started' and (data.get('run_id')!=source['run_id']
                                or data.get('stage')!=('grade' if arm else 'eval_gen')):
        raise EvalHistoryError('eval_attempts.'+attempt_id+': call differs from its producing run or stage')
    if kind=='generation_prompt':
        if any(not isinstance(data.get(k),str) or hashlib.sha256(data[k].encode()).hexdigest()!=data.get(k+'_sha')
               for k in ('template','prompt')):
            raise EvalHistoryError('eval_attempts.'+attempt_id+': generation prompt differs from its revision')
    if kind=='call_result':
        call=data.get('call')
        if (not isinstance(call,dict) or call.get('run_id')!=source['run_id']
                or store.query_one('SELECT * FROM llm_calls WHERE id=?',(call.get('id'),))!=call):
            raise EvalHistoryError('eval_attempts.'+attempt_id+': call result differs from its atomic audit row')
    if kind=='specification':
        from .evals.harness import spec_from_dict
        spec_from_dict(data.get('spec'),origin='eval_attempts.'+attempt_id)
        if digest(data['spec'])!=data.get('spec_revision'):
            raise EvalHistoryError('eval_attempts.'+attempt_id+': specification hash differs')
    if kind=='trial_started':
        spec=store.query_one("SELECT * FROM eval_attempt_events WHERE attempt_id=? AND kind='specification' AND scenario_index=?",(attempt_id,scenario))
        expected_rule=source['rule_content_hash'] if arm=='with' else None
        if (arm not in ('without','with') or trial<0 or not spec
                or _read(spec,'eval_attempt_events')['data']['spec_revision']!=data.get('spec_revision')
                or data.get('rule_content_hash')!=expected_rule):
            raise EvalHistoryError('eval_attempts.'+attempt_id+': trial differs from its frozen rule or specification')
    content = {'attempt_id':attempt_id, 'event_key':event_key, 'kind':kind,
               'scenario_index':scenario, 'arm':arm, 'trial_index':trial, 'data':data}
    old = store.query_one('SELECT * FROM eval_attempt_events WHERE attempt_id=? AND event_key=?', (attempt_id,event_key))
    if old:
        saved = _read(old, 'eval_attempt_events')
        if any(saved.get(k) != v for k,v in content.items()):
            raise EvalHistoryError('eval_attempt_events.'+old['id']+': replay changed its evidence')
        return old['id']
    if kind!='attempt_stopped' and store.query_one("SELECT id FROM eval_attempt_events WHERE attempt_id=? AND kind='attempt_result'",(attempt_id,)):
        raise EvalHistoryError('eval_attempts.'+attempt_id+': completed attempts cannot execute new work')
    row = {k:v for k,v in content.items() if k != 'data'}
    row.update(id=new_id(), created_at=utc_now_iso())
    record = {**row, 'version':1, 'data':data}
    store.insert('eval_attempt_events', {**row, 'record_json':encoded(record), 'record_hash':digest(record)})
    return row['id']


def begin(store, cfg, learning, proposal, *, run_id='', command_id='', source_revision_id='', kind='gate'):
    """Freeze source before execution; a command replay reuses its one attempt."""
    require_schema(store)
    if kind not in ('gate', 'without_rule_rerun'):
        raise EvalHistoryError('Unknown evaluation kind '+str(kind))
    from .jobs import runtime_revision
    content = {'kind':kind, 'proposal_id':proposal.get('id',''), 'learning_id':learning['id'],
               'run_id':run_id, 'command_id':command_id, 'source_revision_id':source_revision_id,
               'learning':{k:v for k,v in learning.items() if not k.startswith('_')},
               'proposal':{k:v for k,v in proposal.items() if not k.startswith('_')},
               'settings':{k:getattr(cfg,k) for k in CONFIG_FIELDS},
               'runtime_revision':runtime_revision(),
               'retry_policy':{'oauth_retries_per_call':1, 'quota_repicks':1,
                               'maximum_provider_attempts':3},
               'grader_timeout_seconds':600.0}
    if kind == 'without_rule_rerun':
        content['settings']['eval_scenarios'] = 1
    # Normalize tuple-valued settings before comparing a later JSON replay.
    content = json.loads(encoded(content))
    content['source_hash']=digest({'learning':content['learning'],'proposal':content['proposal']})
    content['rule_content_hash']=hashlib.sha256(learning['rule_text'].encode()).hexdigest()
    if (any(not isinstance(content[k],str) for k in ('proposal_id','learning_id','run_id','command_id','source_revision_id'))
            or not content['learning_id'] or not content['proposal_id']
            or any(type(content['settings'][k]) is not int or content['settings'][k]<1 for k in ('eval_scenarios','eval_trials'))):
        raise EvalHistoryError('Evaluation source identifiers and trial/scenario counts must be valid.')
    if proposal.get('learning_id', learning['id']) != learning['id']:
        raise EvalHistoryError('Evaluation proposal and learning identities disagree.')
    with store.transaction(write=True):
        if run_id and not store.query_one('SELECT id FROM runs WHERE id=?', (run_id,)):
            raise EvalHistoryError('Unknown evaluation run '+run_id)
        if source_revision_id:
            from .commands import load_revision
            reviewed = load_revision(store, source_revision_id)
            if (reviewed['proposal_id'] != proposal['id'] or reviewed['snapshot']['learning']!=content['learning']
                    or reviewed['snapshot']['proposal']!=content['proposal']):
                raise EvalHistoryError('Evaluation source revision differs from the frozen proposal or learning.')
        old = store.query_one('SELECT * FROM eval_attempts WHERE command_id=?', (command_id,)) if command_id else None
        if old:
            saved = _read(old, 'eval_attempts')
            if any(saved.get(k) != v for k,v in content.items()):
                raise EvalHistoryError('eval_attempts.'+old['id']+': command replay changed its source')
            return Recorder(store, old['id'])
        row = {k:content[k] for k in ('kind','proposal_id','learning_id','run_id','command_id','source_revision_id')}
        row.update(id=new_id(), created_at=utc_now_iso())
        record = {**content, **row, 'version':1}
        store.insert('eval_attempts', {**row, 'record_json':encoded(record), 'record_hash':digest(record)})
    return Recorder(store, row['id'])


class Recorder:
    def __init__(self, store, attempt_id):
        self.store, self.id = store, attempt_id

    def event(self, event_key, kind, data, *, scenario=-1, arm='', trial=-1):
        with self.store.transaction(write=True):
            return append(self.store,self.id,event_key=event_key,kind=kind,data=data,
                          scenario=scenario,arm=arm,trial=trial)

    def stop(self, exc):
        code = getattr(exc, 'code', type(exc).__name__)
        data={'code':code, 'detail':str(exc)}
        return self.event('stop:'+digest(data), 'attempt_stopped', data)

    def call_trace(self, *, scenario, arm='', trial=-1):
        return CallTrace(self, scenario, arm, trial)

    def arm(self, scenario, arm):
        return ArmHistory(self, scenario, arm)

    def link_result(self, scenario, row):
        """The result writer owns the transaction; original row times survive replay."""
        return append(self.store,self.id,event_key=f'scenario:{scenario}:result',kind='scenario_result',
                      data={'eval_result_id':row['id'],'evaluation':row},scenario=scenario)


class ArmHistory:
    def __init__(self, recorder, scenario, arm):
        self.recorder, self.scenario, self.arm = recorder, scenario, arm
        self.current = None

    def _event(self, trial, suffix, kind, data):
        return self.recorder.event(f'scenario:{self.scenario}:{self.arm}:trial:{trial}:{suffix}',
                                   kind,data,scenario=self.scenario,arm=self.arm,trial=trial)

    def started(self, trial, artifact_dir, *, spec, rule_text, grader_timeout):
        self.current = trial
        self._event(trial,'start','trial_started',{'artifact_dir':artifact_dir,
                    'spec_revision':digest(spec), 'grader_timeout_seconds':grader_timeout,
                    'rule_content_hash':hashlib.sha256(rule_text.encode()).hexdigest() if rule_text is not None else None})

    def failed(self, trial, exc):
        code = getattr(exc,'code',type(exc).__name__)
        data={'code':code,'detail':str(exc)}
        self._event(trial,'stop:'+digest(data),'trial_failed',data)

    def finished(self, trial, record):
        self._event(trial,'result','trial_result',record)

    def call_trace(self):
        if self.current is None:
            raise EvalHistoryError('A model call must belong to a started trial.')
        return self.recorder.call_trace(scenario=self.scenario,arm=self.arm,trial=self.current)


class CallTrace:
    """LLM hook: start commits before execution; result shares the call transaction."""
    def __init__(self, recorder, scenario, arm, trial):
        self.recorder, self.scenario, self.arm, self.trial = recorder, scenario, arm, trial

    def start(self, store, call_id, data):
        with store.transaction(write=True):
            self.reserve(store,call_id,data)

    def reserve(self, store, call_id, data):
        """Called inside the journal's call-reservation transaction, before its crash boundary."""
        if store is not self.recorder.store:
            raise EvalHistoryError('Evaluation call and history must use the same Store.')
        append(store,self.recorder.id,event_key='call:'+call_id+':start',kind='call_started',
               data={'call_id':call_id,**data},scenario=self.scenario,arm=self.arm,trial=self.trial)

    def finish(self, store, row, *, provider_attempts):
        if store is not self.recorder.store:
            raise EvalHistoryError('Evaluation call and history must use the same Store.')
        append(store,self.recorder.id,event_key='call:'+row['id']+':result',kind='call_result',
               data={'call':row, 'provider_attempts':provider_attempts},
               scenario=self.scenario,arm=self.arm,trial=self.trial)


def detail(store, attempt_id):
    require_schema(store)
    row = store.query_one('SELECT * FROM eval_attempts WHERE id=?', (attempt_id,))
    if row is None:
        raise EvalHistoryRequestError('Unknown evaluation attempt '+attempt_id,404)
    source = _read(row, 'eval_attempts')
    events = [_read(r,'eval_attempt_events') for r in store.query(
        'SELECT * FROM eval_attempt_events WHERE attempt_id=? ORDER BY created_at,id',(attempt_id,))]
    scenarios = _scenarios(store,source,events)
    completed = [e for e in events if e['kind']=='attempt_result']
    if len(completed)>1:
        raise EvalHistoryError('eval_attempts.'+attempt_id+': multiple completion records')
    stops = [e for e in events if e['kind']=='attempt_stopped']
    return {'source':source, 'events':events, 'scenarios':scenarios,
            'comparison_type':'without_rule_only' if source['kind']=='without_rule_rerun' else 'scenario_gate',
            'state':'completed' if completed else 'stopped' if stops else 'incomplete',
            'result':completed[-1]['data'] if completed else None,
            'reason':'' if completed else 'No complete verdict was recorded; inspect stops, calls and the triggering run or job.'}


def _scenarios(store, source, events):
    """Describe observed outcomes; incomplete or confounded arms have no rate delta."""
    from .evals.harness import spec_from_dict, SpecError
    owner='eval_attempts.'+source['id']
    specs={}; starts={}; results={}; calls={}; call_starts={}; skips={}; verdicts={}
    def invalid(message):
        raise EvalHistoryError(owner+': '+message)
    for event in events:
        if event['kind']=='call_started':
            data=event['data']; cid=data.get('call_id')
            if not isinstance(cid,str) or not cid or data.get('run_id')!=source['run_id']:
                invalid('call start differs from the producing run')
            if cid in call_starts:invalid('duplicate call start '+cid)
            call_starts[cid]=((event['scenario_index'],event['arm'],event['trial_index']),data)
    for event in events:
        data=event['data']; kind=event['kind']; index=event['scenario_index']
        key=(index,event['arm'],event['trial_index'])
        if index>=source['settings']['eval_scenarios'] or event['trial_index']>=source['settings']['eval_trials']:
            invalid('event exceeds the frozen plan')
        if kind=='specification':
            if not isinstance(data.get('spec'),dict) or digest(data['spec'])!=data.get('spec_revision'):
                invalid('specification differs from its revision')
            try:spec_from_dict(data['spec'],origin=owner)
            except SpecError as exc:invalid(str(exc))
            if index in specs:invalid('duplicate scenario specification')
            specs[index]=data
        elif kind=='trial_started':
            if key in starts:invalid('duplicate trial start')
            starts[key]=data
        elif kind=='trial_result':
            if data.get('outcome') not in ('pass','graded_fail','agent_error','grader_error','asked_operator'):
                invalid('unknown trial outcome')
            if key in results:invalid('duplicate trial result')
            results[key]=data
        elif kind=='call_result':
            call=data.get('call')
            if not isinstance(call,dict) or not isinstance(call.get('id'),str):invalid('invalid call result')
            cid=call['id']; start=call_starts.get(cid)
            actual=store.query_one('SELECT * FROM llm_calls WHERE id=?',(cid,))
            if (actual!=call or not start or start[0]!=key or call.get('run_id')!=source['run_id']
                    or any(start[1].get(k)!=call.get(k) for k in ('stage','prompt_sha'))):
                invalid('call '+cid+' differs from its recorded execution')
            if data.get('provider_attempts') is not None and (type(data['provider_attempts']) is not int or data['provider_attempts']<0):
                invalid('invalid provider attempt count for '+cid)
            calls.setdefault(key,[]).append({'call':call,'provider_attempts':data.get('provider_attempts')})
        elif kind=='arm_skipped':skips[(index,event['arm'])]=data
        elif kind=='scenario_result':
            evaluation=data.get('evaluation')
            if (not isinstance(evaluation,dict) or evaluation.get('id')!=data.get('eval_result_id')
                    or store.query_one('SELECT * FROM eval_results WHERE id=?',(data['eval_result_id'],))!=evaluation):
                invalid('scenario result differs from its retained evaluation')
            verdicts[index]=evaluation
    scenarios=[]
    for index in range(source['settings']['eval_scenarios']):
        spec=specs.get(index); arms={}
        for arm in ('without','with'):
            keys=[key for key in starts if key[:2]==(index,arm)]
            summary={'requested_trials':source['settings']['eval_trials'],'attempted':len(keys),
                     'completed':0,'observed_passes':0,'graded_failures':0,'valid_trials':0,
                     'valid_passes':0,'exclusions':{},'served_models':[],
                     'skipped':skips.get((index,arm)), 'pass_rate':None}
            models=set()
            for key in keys:
                started=starts[key]; result=results.get(key); linked=calls.get(key,[])
                call=linked[0]['call'] if len(linked)==1 else None
                provider_attempts=linked[0]['provider_attempts'] if len(linked)==1 else None
                for item in linked:
                    observed=item['call']
                    if observed.get('provider') and observed.get('model_reported'):
                        models.add((observed['provider'],observed['model_reported']))
                expected_rule=source['rule_content_hash'] if arm=='with' else None
                if (not spec or started.get('spec_revision')!=spec['spec_revision']
                        or started.get('rule_content_hash')!=expected_rule):invalid('trial input differs from its frozen rule or spec')
                cause=None
                if result is None:cause='incomplete_trial'
                else:
                    summary['completed']+=1
                    summary['observed_passes']+=int(result['outcome']=='pass')
                    summary['graded_failures']+=int(result['outcome']=='graded_fail')
                    if result['outcome'] not in ('pass','graded_fail'):cause=result['outcome']
                    elif call is None or not call.get('model_reported') or not call.get('provider'):
                        cause='unverified_model'
                    elif call['outcome'] not in ('ok','parse_recovered','oauth_transient_retried'):
                        cause='invalid_call_outcome'
                    elif provider_attempts is None:cause='unverified_attempt_count'
                    elif provider_attempts!=1 or 'repicked_from=' in call.get('error',''):
                        cause='provider_retry_or_repick'
                    elif spec['spec']['grader']['type']!='code':cause='unverified_model_grader'
                    else:
                        summary['valid_trials']+=1
                        summary['valid_passes']+=int(result['outcome']=='pass')
                if cause:summary['exclusions'][cause]=summary['exclusions'].get(cause,0)+1
            summary['served_models']=[{'provider':p,'model':m} for p,m in sorted(models)]
            if keys and not summary['exclusions'] and len(models)==1:
                summary['pass_rate']=summary['valid_passes']/summary['valid_trials']
            arms[arm]=summary
        without,with_=arms['without'],arms['with']
        reason=('without_rule_only_rerun' if source['kind']=='without_rule_rerun' else
                'missing_or_excluded_trials' if any(a['valid_trials']!=source['settings']['eval_trials'] or a['exclusions'] for a in arms.values()) else
                'different_or_unknown_served_models' if without['served_models']!=with_['served_models'] or len(without['served_models'])!=1 else '')
        scenarios.append({'scenario':index,'specification':spec,'evaluation':verdicts.get(index),'arms':arms,
                          'comparison':{'computable':not reason,'reason':reason,
                                        'pass_rate_delta':with_['pass_rate']-without['pass_rate'] if not reason else None}})
    if any(key not in starts for key in results):invalid('trial result has no start')
    return scenarios


def page(store, *, proposal_id=None, learning_id=None, run_id=None, command_id=None, limit=20, cursor=None):
    if type(limit) is not int or not 1<=limit<=100:
        raise EvalHistoryRequestError('Evaluation history limit must be 1–100.')
    selectors = {k:v for k,v in locals().copy().items()
                 if k in ('proposal_id','learning_id','run_id','command_id') and v is not None}
    if len(selectors)>1 or any(not isinstance(v,str) or not v for v in selectors.values()):
        raise EvalHistoryRequestError('Choose at most one nonempty evaluation history selector.')
    if not available(store):
        return {'records':[], 'count':None, 'next_cursor':None, 'computable':False,
                'reason':'Complete evaluation provenance was not installed for this database.'}
    clause = ' AND '.join(k+'=?' for k in selectors) or '1=1'
    args = list(selectors.values())
    count = store.query_one('SELECT COUNT(*) AS n FROM eval_attempts WHERE '+clause,tuple(args))['n']
    if cursor:
        anchor = store.query_one('SELECT * FROM eval_attempts WHERE '+clause+' AND id=?',(*args,cursor))
        if anchor is None:
            raise EvalHistoryRequestError('Unknown evaluation history cursor for this selector.')
        clause += ' AND (created_at<? OR (created_at=? AND id<?))'
        args += [anchor['created_at'],anchor['created_at'],cursor]
    rows = store.query('SELECT * FROM eval_attempts WHERE '+clause+' ORDER BY created_at DESC,id DESC LIMIT ?',(*args,limit+1))
    return {'records':[_read(r,'eval_attempts') for r in rows[:limit]], 'count':count,
            'next_cursor':rows[limit-1]['id'] if len(rows)>limit else None, 'computable':True,
            'reason':'Historical evals without explicit attempt links remain unlinked.'}


def result_links(store, *, run_id):
    """Explicit scenario links for existing Run readers, without time inference."""
    if not available(store):return []
    attempts={r['id']:_read(r,'eval_attempts') for r in store.query('SELECT * FROM eval_attempts WHERE run_id=?',(run_id,))}
    rows=store.query("""SELECT e.* FROM eval_attempt_events e JOIN eval_attempts a ON a.id=e.attempt_id
                        WHERE a.run_id=? AND e.kind='scenario_result' ORDER BY e.created_at,e.id""",(run_id,))
    links=[]
    for row in rows:
        event=_read(row,'eval_attempt_events');source=attempts[event['attempt_id']];data=event['data']
        evaluation=data.get('evaluation')
        if (source['run_id']!=run_id or not isinstance(evaluation,dict)
                or evaluation.get('id')!=data.get('eval_result_id')
                or store.query_one('SELECT * FROM eval_results WHERE id=?',(data['eval_result_id'],))!=evaluation):
            raise EvalHistoryError('eval_attempt_events.'+event['id']+': invalid scenario result link')
        links.append({'kind':'eval_attempts','id':event['id'],'attempt_id':source['id'],
                      'run_id':run_id,'command_id':source['command_id'],'proposal_id':source['proposal_id'],
                      'source_revision_id':source['source_revision_id'],'scenario_index':event['scenario_index'],
                      'eval_result_id':data['eval_result_id']})
    return links
