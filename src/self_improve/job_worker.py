"""Separate model-job executor. Never holds the instruction-delivery lock."""
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
import fcntl
import json

from .commands import CommandError, _hash, _json, _parsed
from .store import new_id, utc_now_iso
from . import jobs


class StoredStepFailure(Exception):
    """A known failed step can be replayed as a failure without another call."""


class JobPause(BaseException):
    """Control flow must pass through trial/gate Exception handlers without a verdict."""
    def __init__(self,code,detail):
        self.code=code;super().__init__(detail)


def _checkpoint(event):
    """Fault-injection boundary used by process-death tests."""


@contextmanager
def job_lock(cfg):
    path=cfg.state_path('model-jobs.lock');path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a+b') as handle:
        try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError as exc:raise CommandError('JobWorkerBusy','Another model-job worker holds the execution lock.',409) from exc
        try:yield
        finally:fcntl.flock(handle,fcntl.LOCK_UN)


class Journal:
    def __init__(self,store,cid):
        self.store=store;self.cid=cid;self.active=None;self.index=0

    def inspect(self):
        row=self.store.query_one('SELECT * FROM commands WHERE id=?',(self.cid,))
        saved=jobs.status(self.store,row)
        if saved['cancel_requested']:raise JobPause('JobCancelled','Cancellation was requested. No further calls will start.')
        if saved['state']!='running':raise JobPause('JobNotRunning','The job is no longer authorized to run.')
        return saved

    def step(self,key,inputs,fn):
        fingerprint=_hash(inputs);stamp=utc_now_iso()
        with self.store.transaction(write=True):
            self.inspect()
            row=self.store.query_one('SELECT * FROM job_steps WHERE command_id=? AND step_key=?',(self.cid,key))
            if row:
                if row['input_hash']!=fingerprint:raise JobPause('StepChanged','A recovery step differs from its frozen input. Request a new preview.')
                if row['state']=='completed':
                    result=_parsed(row,'result_json')
                    if _hash(result)!=row['result_hash'] or set(result)!={'value'}:raise JobPause('StepDataError','A completed step no longer matches its result fingerprint.')
                    return result['value']
                if row['state']=='failed':
                    failure=_parsed(row,'result_json')
                    raise StoredStepFailure(f"{failure['error_code']}: {failure['error_detail']}")
                raise JobPause('InterruptedStep',f"Step {key} did not record a complete result. Its calls will not be repeated; request a new attempt explicitly.")
            sid=new_id()
            self.store.insert('job_steps',{'id':sid,'command_id':self.cid,'step_key':key,'input_hash':fingerprint,
                'state':'started','created_at':stamp,'updated_at':stamp})
        self.active=sid;self.index=0
        try:value=fn()
        except Exception as exc:
            self.store.conn.rollback()
            code=getattr(exc,"code",type(exc).__name__)
            with self.store.transaction(write=True):
                self.store.update('job_steps','id',sid,{'state':'failed','updated_at':utc_now_iso(),
                    'result_json':_json({'error_code':code,'error_detail':str(exc)}),
                    'result_hash':_hash({'error_code':code,'error_detail':str(exc)})})
            raise
        finally:self.active=None
        with self.store.transaction(write=True):
            self.store.update('job_steps','id',sid,{'state':'completed','updated_at':utc_now_iso(),'result_json':_json({'value':value}),'result_hash':_hash({'value':value})})
        _checkpoint('step_completed')
        return value

    def reserve(self,pool,inputs,*,on_reserved=None):
        if self.active is None:raise JobPause('UnscopedCall','A job model call must belong to a frozen step.')
        index=self.index;self.index+=1;stamp=utc_now_iso()
        with self.store.transaction(write=True):
            saved=self.inspect()
            stages={s['stage']:s for s in saved['plan']['stages']}
            stage=stages.get(inputs['stage'])
            if stage is None or stage['pool']!=pool:raise JobPause('UnreservedStage','This stage has no authorized call reservation.')
            old=self.store.query_one('SELECT * FROM job_calls WHERE step_id=? AND call_index=?',(self.active,index))
            if old:
                if old['pool']!=pool or old['input_hash']!=_hash(inputs):raise JobPause('CallChanged','The retained call has different inputs.')
                if old['state']!='completed':raise JobPause('UnknownCallOutcome','The previous call has no recorded outcome. It will not be repeated automatically.')
                return old['id'],_parsed(old,'result_json')
            stage_used=sum(c['stage']==inputs['stage'] for c in saved['calls'])
            if saved['budget']['remaining'][pool]<=0 or stage_used>=stage['maximum']:
                from .llm import BudgetExhausted
                raise BudgetExhausted(pool,saved['budget']['maximum'][pool],1)
            cid=new_id()
            self.store.insert('job_calls',{'id':cid,'command_id':self.cid,'step_id':self.active,'call_index':index,
                'pool':pool,'input_json':_json(inputs),'input_hash':_hash(inputs),'state':'started','created_at':stamp,'updated_at':stamp})
            if on_reserved is not None:on_reserved(cid)
        _checkpoint('call_reserved')
        return cid,None

    def complete(self,cid,result):
        self.store.update('job_calls','id',cid,{'state':'completed','result_json':_json(result),'result_hash':_hash(result),'updated_at':utc_now_iso()})

    def record_eval(self,scenario,row,*,history=None):
        content={k:v for k,v in row.items() if k not in {'id','started','finished'}}
        fingerprint=_hash(content)
        with self.store.transaction(write=True):
            old=self.store.query_one('SELECT * FROM job_evaluations WHERE command_id=? AND scenario_index=?',(self.cid,scenario))
            if old:
                if old['content_hash']!=fingerprint:raise JobPause('EvaluationChanged','A replay produced different evaluation evidence.')
                if history is not None:
                    original=self.store.query_one('SELECT * FROM eval_results WHERE id=?',(old['eval_result_id'],))
                    if original is None:raise JobPause('EvaluationMissing','The recorded scenario result is missing.')
                    history.link_result(scenario,original)
                return old['eval_result_id']
            self.store.insert('eval_results',row)
            self.store.insert('job_evaluations',{'id':new_id(),'command_id':self.cid,'scenario_index':scenario,
                'eval_result_id':row['id'],'content_hash':fingerprint})
            if history is not None:history.link_result(scenario,row)
        return row['id']


def run_once(store,cfg,*,_llm_factory=None):
    jobs.require_schema(store)
    if store.db_path.resolve()!=cfg.state_path('state.db').resolve():raise CommandError('StoreMismatch','The model worker must use its configured database.',409)
    if store.conn.in_transaction:raise CommandError('PendingTransaction','The model worker cannot commit caller work.',409)
    with job_lock(cfg):
        with store.transaction(write=True):
            actions = sorted(jobs.ACTIONS)
            placeholders = ','.join('?' for _ in actions)
            row=store.query_one(f"SELECT * FROM commands WHERE action IN ({placeholders}) AND state IN ('queued','running') ORDER BY created_at,id LIMIT 1", tuple(actions))
            if row is None:return None
            saved=jobs.status(store,row);cid=row['id']
            if saved['cancel_requested']:
                store.update('commands','id',cid,{'state':'cancelled','updated_at':utc_now_iso()})
                return jobs.status(store,store.query_one('SELECT * FROM commands WHERE id=?',(cid,)))
            if saved['plan']['runtime_revision']!=jobs.runtime_revision():
                store.update('commands','id',cid,{'state':'blocked','error_code':'JobRuntimeChanged','error_detail':'Execution code changed. Inspect and request a fresh job.','updated_at':utc_now_iso()})
                return jobs.status(store,store.query_one('SELECT * FROM commands WHERE id=?',(cid,)))
            run_id=saved['run_id'] or new_id()
            if not saved['run_id']:
                store.insert('runs',{'id':run_id,'started':utc_now_iso(),'status':'running'})
                store.update('incident_jobs' if saved['action']=='mine_incident' else 'recovery_jobs' if saved['action']=='propose_recovery' else 'model_jobs','command_id',cid,{'run_id':run_id})
            else:
                store.update('runs','id',run_id,{'status':'running','finished':''})
            store.update('commands','id',cid,{'state':'running','updated_at':utc_now_iso()})
        saved={**saved,'run_id':run_id}
        evaluation_history = None
        try:
            from .config import Config
            from .llm import LLMRunner
            frozen=Config(**saved['plan']['configuration'])
            # State output uses the selected worker's state directory, never a copied
            # request's alternate state root. Frozen execution knobs remain unchanged.
            run_dir=cfg.state_path('runs',run_id)
            frozen=replace(frozen,state_dir=cfg.state_dir,regression_specs_dir=str(run_dir/'specs'),
                max_cheap_calls_per_run=saved['budget']['maximum']['cheap'],
                max_strong_calls_per_run=saved['budget']['maximum']['strong'],
                max_gate_calls_per_run=saved['budget']['maximum']['gate'])
            journal=Journal(store,cid)
            if saved['action']=='regenerate_eval':
                from . import eval_history
                member=saved['members'][0]
                evaluation_history=eval_history.begin(store,frozen,member['snapshot']['learning'],
                    member['snapshot']['proposal'],run_id=run_id,command_id=cid,
                    source_revision_id=member['revision_id'])
            llm=(_llm_factory or LLMRunner)(frozen,store,run_id,run_dir/'raw',call_journal=journal)
            if saved['action']=='propose_recovery':
                from . import recovery_jobs
                generated=recovery_jobs.generate(store,frozen,saved,llm,journal)
                _checkpoint('recovery_generated')
                with store.transaction(write=True):
                    journal.inspect()
                    result=recovery_jobs.persist(store,frozen,saved,run_id,generated)
                    store.update('commands','id',cid,{'state':'completed','result_json':_json(result),
                        'updated_at':utc_now_iso(),'error_code':'','error_detail':''})
                    store.update('runs','id',run_id,{'status':'ok','finished':utc_now_iso(),
                        'stats_json':_json({'review_only':True,'command_id':cid,'recovery':result})})
            elif saved['action']=='mine_incident':
                from . import incident_jobs
                generated=incident_jobs.generate(store,frozen,saved,llm,journal,run_dir)
                _checkpoint('incident_generated')
                with store.transaction(write=True):
                    journal.inspect()
                    result=incident_jobs.persist(store,frozen,saved,run_id,generated)
                    store.update('commands','id',cid,{'state':'completed','result_json':_json(result),
                        'updated_at':utc_now_iso(),'error_code':'','error_detail':''})
                    store.update('runs','id',run_id,{'status':'ok','finished':utc_now_iso(),
                        'stats_json':_json({'review_only':True,'command_id':cid,'mine':result})})
            elif saved['action']=='resolve_rollback':
                from . import resolutions
                generated=resolutions.generate(store,frozen,saved,llm,journal)
                _checkpoint('resolution_generated')
                with store.transaction(write=True):
                    journal.inspect()
                    pid=resolutions.persist(store,cfg,saved,run_id,generated)
                    store.update('commands','id',cid,{'state':'completed','result_json':_json({'run_id':run_id,'proposal_id':pid,'explanation':generated['explanation']}),
                        'updated_at':utc_now_iso(),'error_code':'','error_detail':''})
                    store.update('runs','id',run_id,{'status':'ok','finished':utc_now_iso(),'stats_json':_json({'review_only':True,'command_id':cid})})
            else:
                _run_evaluation(store,cfg,frozen,run_id,run_dir,llm,journal,saved,cid,history=evaluation_history)
        except (JobPause,Exception) as exc:
            store.conn.rollback()
            code=getattr(exc,'code',type(exc).__name__)
            if evaluation_history is not None:evaluation_history.stop(exc)
            with store.transaction(write=True):
                store.update('commands','id',cid,{'state':'cancelled' if code=='JobCancelled' else 'blocked' if isinstance(exc,JobPause) else 'failed',
                    'error_code':code,'error_detail':str(exc),'updated_at':utc_now_iso()})
                store.update('runs','id',run_id,{'status':'interrupted' if isinstance(exc,JobPause) else 'error','finished':utc_now_iso(),
                    'stats_json':_json({'review_only':True,'command_id':cid,'error_code':code,'error_detail':str(exc)})})
        return jobs.status(store,store.query_one('SELECT * FROM commands WHERE id=?',(cid,)))


def _run_evaluation(store,cfg,frozen,run_id,run_dir,llm,journal,saved,cid,*,history=None):
    from .pipeline import gate_one_proposal,new_gate_stats,_evidence_excerpt
    source=saved['members'][0];proposal=dict(source['snapshot']['proposal']);learning=source['snapshot']['learning']
    proposal['eval_result_id']='';gate=new_gate_stats();taxonomy={}
    def bump(key):taxonomy[key]=taxonomy.get(key,0)+1
    gate_one_proposal(store,frozen,llm,learning,proposal,run_dir=run_dir,
        sandbox_state={'ok':None,'error':'','observed':{}},gate_stats=gate,bump_taxonomy=bump,
        evidence_override=_evidence_excerpt(next((i['window_json'] for i in source['snapshot']['evidence'] if i['window_json']),'')),
        checkpoint=journal,run_id=run_id,source_revision_id=source['revision_id'],history=history)
    store.commit()
    _checkpoint('gate_finished')
    with store.transaction(write=True):
        from .commands import review_snapshot
        from .store import DECIDED_STATUSES,actor_for
        current=review_snapshot(store,source['proposal_id'],cfg)
        status_update='preserved_decision' if current['snapshot']['proposal']['status'] in DECIDED_STATUSES else 'preserved_changed_revision'
        if current['revision']==source['revision'] and current['snapshot']['proposal']['status'] not in DECIDED_STATUSES and proposal['eval_result_id']:
            store.update('proposals','id',proposal['id'],{'status':proposal['status'],'eval_result_id':proposal['eval_result_id']})
            status_update='updated'
        success=bool(proposal['eval_result_id'])
        if success:
            store.insert('proposal_eval_history',{'id':new_id(),'proposal_id':proposal['id'],
                'source_revision_id':source['revision_id'],'run_id':run_id,'eval_result_id':proposal['eval_result_id'],
                'verdict':proposal['status'],'created_at':utc_now_iso()})
            store.insert('proposal_events',{'id':new_id(),'proposal_id':proposal['id'],'ts':utc_now_iso(),
                'event':'gated','actor':actor_for('gated'),'note':_json({'command_id':cid,'run_id':run_id,'status_update':status_update})})
        cancelled=store.query_one('SELECT cancel_requested FROM commands WHERE id=?',(cid,))['cancel_requested']
        state='completed' if success else 'cancelled' if cancelled else 'failed'
        result={'run_id':run_id,'gate':gate,'taxonomy':taxonomy,'status_update':status_update,
            'eval_result_id':proposal['eval_result_id'],'verdict':proposal['status'] if success else None}
        store.update('commands','id',cid,{'state':state,'result_json':_json(result),'updated_at':utc_now_iso(),
            'error_code':'' if success else 'GateNotRun','error_detail':'' if success else 'No evaluation verdict was produced. See the recorded causes.'})
        store.update('runs','id',run_id,{'status':'ok' if success else 'degraded','finished':utc_now_iso(),
            'stats_json':_json({'review_only':True,'command_id':cid,'gate':gate,'taxonomy':taxonomy})})


def serve(cfg,*,once=False):
    from contextlib import closing
    from .store import Store
    import time
    with closing(Store(cfg.state_path('state.db'),migrate=False)) as store:
        while True:
            try:result=run_once(store,cfg)
            except CommandError as exc:
                if exc.code!='JobWorkerBusy':raise
                result={'state':'busy','error_code':exc.code,'error_detail':str(exc)}
            if result:print(_json({k:result[k] for k in ('id','state','budget','failure_taxonomy','error_code','error_detail') if k in result}),flush=True)
            if once:return 1 if result and result['state'] in {'failed','blocked','busy'} else 0
            if result is None or result['state']=='busy':time.sleep(1)
