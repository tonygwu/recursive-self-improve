"""Frozen model-job intent and read models. No models or target writes here."""
from dataclasses import asdict
from pathlib import Path
import hashlib
import json
import re

from .commands import CommandError, _hash, _json, _parsed, load_revision, review_snapshot, save_revision
from .store import new_id, utc_now_iso
from .call_budgets import gate_calls_needed

MIGRATION = '0016_model_jobs'
ACTIONS = frozenset({'regenerate_eval','resolve_rollback','mine_incident','propose_recovery'})
CONTROLS = frozenset({'resume_job', 'cancel_job'})
POOLS = ('cheap', 'strong', 'gate')


def available(store):
    return store.query_one('SELECT name FROM schema_migrations WHERE name=?',(MIGRATION,)) is not None


def require_schema(store):
    if not available(store):
        raise CommandError('UpgradeRequired','Upgrade the database before requesting model jobs.',503)


def runtime_revision():
    """A queued request cannot silently adopt changed execution code or prompts."""
    from .resources import bundled_path
    root=Path(__file__).parent
    paths={str(path.relative_to(root)):path for path in sorted(root.rglob('*.py'))
           if '_assets' not in path.relative_to(root).parts}
    paths['prompts/gen_regression_eval.md']=bundled_path('prompts','gen_regression_eval.md')
    paths['prompts/resolve_rollback.md']=bundled_path('prompts','resolve_rollback.md')
    for name in ('mine_incident.md','mine_incident_agentic.md','propose_recovery.md'):
        paths['prompts/'+name]=bundled_path('prompts',name)
    return _hash({name:hashlib.sha256(path.read_bytes()).hexdigest() for name,path in paths.items()})


def preview(store,cfg,proposal_id,*,action="regenerate_eval"):
    require_schema(store)
    if action=='mine_incident':
        from .incident_jobs import preview as incident_preview
        return incident_preview(store,cfg,proposal_id)
    source=review_snapshot(store,proposal_id,cfg)
    if action not in ACTIONS:
        raise CommandError('InvalidJob','Choose a supported model job.')
    plan={'version':1,'action':action,'source_revision':source['revision'],
          'runtime_revision':runtime_revision(),'configuration':asdict(cfg)}
    if action=='resolve_rollback':
        from . import resolutions,rollback
        from .call_budgets import call_pool
        resolutions.require_schema(store)
        conflict=rollback.rollback_preview(store,cfg,proposal_id)
        if conflict['error_code']!='RollbackConflict':
            raise CommandError('NoRollbackConflict','This application does not need a generated conflict resolution.',409)
        if rollback.committed_or_prepared_rollback(store,conflict['source']):
            raise CommandError('ReconciliationRequired','Reconcile the prepared rollback before requesting a resolution.',409)
        pool=call_pool(cfg,action,cfg.strong_model_class)
        if pool=='gate':raise CommandError('InvalidBudget','Resolution generation must use a mining pool, not the gate pool.',500)
        plan.update(rollback=conflict,budgets={p:int(p==pool) for p in POOLS},stages=[{'stage':action,'pool':pool,'maximum':1}])
        maximum=1
    else:
        if not source['snapshot']['learning']['rule_text'].strip():
            raise CommandError('EmptyRule','There is no rule text to evaluate.',409)
        if not {'eval_gen','grade'}<=set(cfg.gate_stages):
            raise CommandError('InvalidBudget','Evaluation generation and trials must use the gate pool.',500)
        maximum=gate_calls_needed(cfg)
        if type(maximum) is not int or maximum < 1:
            raise CommandError('InvalidBudget','The evaluation cost bound must be positive.',500)
        plan.update(budgets={'cheap':0,'strong':0,'gate':maximum},
              stages=[{'stage':'eval_gen','pool':'gate','maximum':cfg.eval_scenarios},
                      {'stage':'grade','pool':'gate','maximum':cfg.eval_scenarios*2*cfg.eval_trials}])
    latest=store.query_one('SELECT c.id,c.state FROM model_jobs j JOIN commands c ON c.id=j.command_id WHERE j.plan_hash=? ORDER BY c.created_at DESC,c.id DESC LIMIT 1',(_hash(plan),))
    return {'proposal_id':proposal_id,'revision':_hash(plan),'source':source,'plan':plan,'latest_job':latest,
            'max_model_calls':maximum,'meaning':'Generate a new rollback-resolution proposal for Review.' if action=='resolve_rollback' else 'Regenerate and run the evaluation. Existing decisions and applied edits are preserved. Internal provider retries belong to one logical call.'}


def _request(body):
    recovery=isinstance(body,dict) and body.get('action')=='propose_recovery'
    subject='learning_id' if recovery else 'incident_id' if isinstance(body,dict) and body.get('action')=='mine_incident' else 'proposal_id'
    if not isinstance(body,dict) or set(body)!=({'action','request_key',subject,'preview_revision'} | ({'mode','target_id','proposal_ids'} if recovery else set())) or body['action'] not in ACTIONS:
        raise CommandError('InvalidJob',f'Use action, request_key, {subject}, and preview_revision.')
    if not isinstance(body['request_key'],str) or re.fullmatch(r'[A-Za-z0-9._:-]{8,200}',body['request_key']) is None:
        raise CommandError('InvalidRequestKey','Use a unique request key of 8–200 characters.')
    if not isinstance(body[subject],str) or not body[subject]:
        raise CommandError('InvalidJob','Select a proposal.')
    if not isinstance(body['preview_revision'],str) or re.fullmatch(r'[a-f0-9]{64}',body['preview_revision']) is None:
        raise CommandError('InvalidRevision','Inspect the job cost preview before requesting work.')
    if recovery:
        from .recovery_jobs import selection
        return body | selection(**{k:body[k] for k in ('learning_id','mode','target_id','proposal_ids')})
    return body


def _unused_key(store,key):
    from .rollback import instruction_request_exists
    if instruction_request_exists(store,key) or store.query_one('SELECT id FROM command_control_events WHERE request_key=?',(key,)):
        raise CommandError('IdempotencyConflict','This key identifies another operation.',409)


def submit(store,cfg,body,*,now=None):
    require_schema(store);request=_request(body);stamp=now or utc_now_iso()
    request_hash=_hash({k:v for k,v in request.items() if k!='request_key'})
    with store.transaction(write=True):
        _unused_key(store,request['request_key'])
        old=store.query_one('SELECT * FROM commands WHERE request_key=?',(request['request_key'],))
        if old:
            if old['action'] not in ACTIONS or old['request_hash']!=request_hash:
                raise CommandError('IdempotencyConflict','This key identifies another command.',409)
            return status(store,old)
        if request['action']=='propose_recovery':
            from .recovery_jobs import preview as recovery_preview
            shown=recovery_preview(store,cfg,**{k:request[k] for k in ('learning_id','mode','target_id','proposal_ids')})
        else:
            shown=preview(store,cfg,request.get('incident_id') or request.get('proposal_id'),action=request['action'])
        if shown['revision']!=request['preview_revision']:
            raise CommandError('StaleJobPreview','The source, execution settings, or cost changed. Inspect a fresh preview.',409)
        cid=new_id()
        store.insert('commands',{'id':cid,'request_key':request['request_key'],'request_hash':request_hash,
            'action':request['action'],'actor':'user','state':'queued','created_at':stamp,'updated_at':stamp,
            'payload_json':_json(request),'max_model_calls':shown['max_model_calls']})
        if request['action'] in {'mine_incident','propose_recovery'}:
            if request['action']=='mine_incident':
                from .incident_jobs import save
            else:
                from .recovery_jobs import save
            save(store,cid,shown)
        else:
            store.insert('model_jobs',{'command_id':cid,'source_revision_id':save_revision(store,shown['source'],stamp),
                'plan_json':_json(shown['plan']),'plan_hash':shown['revision']})
        for pool,maximum in shown['plan']['budgets'].items():
            store.insert('job_budgets',{'id':new_id(),'command_id':cid,'pool':pool,'maximum':maximum})
        return status(store,store.query_one('SELECT * FROM commands WHERE id=?',(cid,)))


def status(store,row):
    require_schema(store)
    incident=row['action']=='mine_incident';recovery=row['action']=='propose_recovery';detached=incident or recovery
    if detached:
        if incident:
            from .incident_jobs import load
        else:
            from .recovery_jobs import load
        job,source=load(store,row['id'])
    else:
        job=store.query_one('SELECT * FROM model_jobs WHERE command_id=?',(row['id'],))
    if job is None:
        raise CommandError('JobDataError',f"Job {row['id']} has no frozen plan.",500)
    plan=_parsed({'id':row['id'],**job},'plan_json')
    request=_request(_parsed(row,'payload_json'))
    if not detached:source=load_revision(store,job['source_revision_id'])
    subject='learning_id' if recovery else 'incident_id' if incident else 'proposal_id'
    budgets={b['pool']:b['maximum'] for b in store.query('SELECT * FROM job_budgets WHERE command_id=?',(row['id'],))}
    if recovery and any(request[k]!=source['snapshot'].get(k) for k in ('learning_id','mode','target_id','proposal_ids')):
        raise CommandError('JobDataError','Recovery selection differs from its saved source.',500)
    if (row['action']!=request['action'] or row['actor']!='user' or row['request_key']!=request['request_key']
            or row['request_hash']!=_hash({k:v for k,v in request.items() if k!='request_key'})
            or _hash(plan)!=job['plan_hash'] or job['plan_hash']!=request['preview_revision']
            or plan.get('source_revision')!=source['revision'] or request[subject]!=source[subject]
            or plan.get('action')!=row['action'] or plan.get('version')!=1
            or set(budgets)!=set(POOLS) or any(type(v) is not int or v<0 for v in budgets.values())
            or budgets!=plan.get('budgets') or sum(budgets.values())!=row['max_model_calls']):
        raise CommandError('JobDataError',f"Job {row['id']} no longer matches its authorization or reservation.",500)
    from .commands import COMMAND_STATES
    if row['state'] not in COMMAND_STATES or row['cancel_requested'] not in (0,1):
        raise CommandError('JobDataError',f"Job {row['id']} has an invalid state.",500)
    result=_parsed(row,'result_json')
    taxonomy=result.get('taxonomy',{})
    if not isinstance(taxonomy,dict) or any(not isinstance(k,str) or type(v) is not int or v<0 for k,v in taxonomy.items()):
        raise CommandError('JobDataError',f"Job {row['id']} has invalid outcome causes.",500)
    taxonomy=taxonomy.copy()
    calls=[];used={p:0 for p in POOLS}
    for c in store.query('SELECT * FROM job_calls WHERE command_id=? ORDER BY created_at,id',(row['id'],)):
        inputs=_parsed(c,'input_json');result=_parsed(c,'result_json')
        if c['pool'] not in budgets or c['state'] not in {'started','completed'} or _hash(inputs)!=c['input_hash']:
            raise CommandError('JobDataError',f"Call {c['id']} has invalid reservation data.",500)
        used[c['pool']]+=1
        step=store.query_one('SELECT command_id FROM job_steps WHERE id=?',(c['step_id'],))
        if not step or step['command_id']!=row['id']:raise CommandError('JobDataError',f"Call {c['id']} has no matching step.",500)
        if c['state']=='completed':
            if _hash(result)!=c['result_hash']:raise CommandError('JobDataError',f"Call {c['id']} has a changed result.",500)
            from .store import LLM_SUCCESS_OUTCOMES
            outcome=result.get('outcome')
            if not isinstance(outcome,str) or type(result.get('ok')) is not bool or result['ok']!=(outcome in LLM_SUCCESS_OUTCOMES):
                raise CommandError('JobDataError',f"Call {c['id']} has an invalid result.",500)
            audit=store.query_one('SELECT id,outcome,run_id,stage,prompt_sha FROM llm_calls WHERE id=?',(c['id'],))
            if (not audit or audit['outcome']!=outcome or audit['run_id']!=job['run_id']
                    or audit['stage']!=inputs.get('stage') or audit['prompt_sha']!=inputs.get('prompt_sha')):
                raise CommandError('JobDataError',f"Call {c['id']} has no matching audit record.",500)
            if not result['ok']:taxonomy[outcome]=taxonomy.get(outcome,0)+1
        else:taxonomy['interrupted_or_running']=taxonomy.get('interrupted_or_running',0)+1
        calls.append({'id':c['id'],'pool':c['pool'],'state':c['state'],'stage':inputs['stage'],
                      'model_class':inputs['model_class'],'result':result})
    if any(used[p]>budgets[p] for p in POOLS):
        raise CommandError('JobDataError',f"Job {row['id']} exceeded its reservation.",500)
    steps=[]
    for step in store.query('SELECT * FROM job_steps WHERE command_id=? ORDER BY created_at,id',(row['id'],)):
        result=_parsed(step,'result_json')
        if step['state'] not in {'started','completed','failed'} or (step['state']!='started' and _hash(result)!=step['result_hash']):
            raise CommandError('JobDataError',f"Step {step['id']} has an invalid result or state.",500)
        if step['state']=='failed':
            code=result.get('error_code')
            if not isinstance(code,str) or not isinstance(result.get('error_detail'),str):
                raise CommandError('JobDataError',f"Step {step['id']} has no failure cause.",500)
            taxonomy['step_'+code]=taxonomy.get('step_'+code,0)+1
        steps.append({'id':step['id'],'key':step['step_key'],'state':step['state'],'result':result})
    unfinished=store.query_one("SELECT id FROM job_steps WHERE command_id=? AND state!='completed' LIMIT 1",(row['id'],))
    history=[{k:e[k] for k in ('id','action','actor','note','created_at')}|{'before':_parsed(e,'before_json')}
             for e in store.query('SELECT * FROM command_control_events WHERE command_id=? ORDER BY created_at,id',(row['id'],))]
    return {k:row[k] for k in ('id','action','state','actor','created_at','updated_at','max_model_calls','error_code','error_detail')}|{
        'members':[] if detached else [source|{'revision_id':job['source_revision_id']}],'targets':[],
        **({'source':source,subject:source[subject]} if detached else {}),
        **({'selection':{k:request[k] for k in ('learning_id','mode','target_id','proposal_ids')}} if recovery else {}),
        'result':_parsed(row,'result_json'),'plan':plan,'run_id':job['run_id'],
        'budget':{'maximum':budgets,'consumed':used,'remaining':{p:budgets[p]-used[p] for p in POOLS}},
        'calls':calls,'steps':steps,'failure_taxonomy':taxonomy,'cancel_requested':bool(row['cancel_requested']),
        'controls_available':True,'control_history':history,
        'can_retry':row['state'] in {'failed','blocked'} and not row['cancel_requested'] and not unfinished and row['error_code']!='JobRuntimeChanged',
        'can_cancel':row['state'] in {'queued','running','failed','blocked'} and not row['cancel_requested']}


def control(store,body,*,now=None):
    require_schema(store)
    if not isinstance(body,dict) or set(body)-{'action','request_key','command_id','note'} or body.get('action') not in CONTROLS:
        raise CommandError('InvalidJobControl','Use resume_job or cancel_job with command_id and request_key.')
    key=body.get('request_key');cid=body.get('command_id');note=body.get('note','')
    if not isinstance(key,str) or re.fullmatch(r'[A-Za-z0-9._:-]{8,200}',key) is None or not isinstance(cid,str) or not cid or not isinstance(note,str):
        raise CommandError('InvalidJobControl','Use a valid key, command ID, and text note.')
    req={'action':body['action'],'command_id':cid,'note':note};fingerprint=_hash(req);stamp=now or utc_now_iso()
    with store.transaction(write=True):
        old=store.query_one('SELECT * FROM command_control_events WHERE request_key=?',(key,))
        if old:
            if old['request_hash']!=fingerprint:raise CommandError('IdempotencyConflict','This key identifies another control.',409)
            return status(store,store.query_one('SELECT * FROM commands WHERE id=?',(old['command_id'],)))
        from .rollback import instruction_request_exists
        if instruction_request_exists(store,key) or store.query_one('SELECT id FROM commands WHERE request_key=?',(key,)):
            raise CommandError('IdempotencyConflict','This key identifies another operation.',409)
        row=store.query_one('SELECT * FROM commands WHERE id=?',(cid,))
        if row is None or row['action'] not in ACTIONS:raise CommandError('NoSuchJob','No model job with that ID.',404)
        before=status(store,row)
        allowed=before['can_cancel'] if body['action']=='cancel_job' else before['can_retry']
        if not allowed:raise CommandError('JobControlUnavailable','This job cannot accept that control.',409)
        store.insert('command_control_events',{'id':new_id(),'command_id':cid,'request_key':key,'request_hash':fingerprint,
            'action':body['action'],'actor':'user','note':note,'created_at':stamp,'before_json':_json({
                'state':row['state'],'error_code':row['error_code'],'error_detail':row['error_detail'],
                'budget':before['budget'],'result':before['result'],'failure_taxonomy':before['failure_taxonomy']})})
        changes={'updated_at':stamp,'error_code':'','error_detail':''}
        if body['action']=='cancel_job':changes.update(cancel_requested=1,state='cancelled' if row['state']!='running' else 'running')
        else:changes['state']='queued'
        store.update('commands','id',cid,changes)
        return status(store,store.query_one('SELECT * FROM commands WHERE id=?',(cid,)))
