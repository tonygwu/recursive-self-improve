"""Bounded hook, corrected-target, and selected-patch proposal generation."""
from dataclasses import asdict
from pathlib import Path
import json
from .commands import CommandError,_hash,_json,_parsed,review_snapshot
from .destinations import resolve_destination,read_destination,destination_identity,DestinationError
from .store import new_id,utc_now_iso,DECIDED_STATUSES,actor_for

ACTION='propose_recovery'
MIGRATION='0020_recovery_jobs'
MODES=frozenset({'hook','correct_target','regenerate_patch'})

def require_schema(store):
    if not store.query_one('SELECT name FROM schema_migrations WHERE name=?',(MIGRATION,)):
        raise CommandError('UpgradeRequired','Upgrade the database before requesting recovery proposals.',503)

def _learning(store,lid):
    from .rejections import lesson_rejected
    row=store.query_one('SELECT * FROM learnings WHERE id=?',(lid,))
    if row is None:raise CommandError('NoSuchLearning','No learning with that ID.',404)
    if lesson_rejected(store,lid):raise CommandError('LessonRejected','This lesson remains rejected everywhere.',409)
    return row

def options(store,cfg,learning_id):
    """Resolve configured destinations and verified canonical projects; never accept paths."""
    from .routing import canonical_working_copy,skill_md_path,skill_slug,is_never_write
    from .project_identity import resolve
    from .rejections import proposal_rejection
    from .embeddings import Embedder
    require_schema(store);learning=_learning(store,learning_id)
    candidates=[(cfg.global_claude_md,'global_claude_md','Global Claude instructions'),
        (cfg.codex_global_agents_md,'codex_global','Global Codex instructions'),
        (str(Path(cfg.global_claude_md).parent/'settings.json'),'hook','Claude command hook'),
        (str(skill_md_path(cfg,learning)),'skill','Shared skill')]
    groups={};unavailable=[]
    for r in store.query("SELECT DISTINCT project_key,project_path FROM sessions WHERE project_path!='' ORDER BY project_key,project_path"):
        groups.setdefault(r['project_key'],[]).append(r['project_path'])
    for key,paths in groups.items():
        valid=[p for p in paths if Path(p).is_dir() and resolve(p).key==key]
        root=canonical_working_copy(valid,project_key=key,resolver=lambda p:resolve(p).key)
        if not root:
            unavailable.append({'project_key':key,'cause':'NoWritableProject'});continue
        for name,kind in [('CLAUDE.md','project_claude_md'),('AGENTS.md','project_agents_md'),('.claude/rules/'+skill_slug(learning)+'.md','rule_file')]:
            candidates.append((str(Path(root)/name),kind,key+' · '+name))
    # Existing proposal destinations let regeneration address its exact source.
    proposals=store.query('SELECT * FROM proposals WHERE learning_id=? ORDER BY created_at,id',(learning_id,))
    candidates.extend((p['target_path'],p['target_kind'],'Existing proposal target') for p in proposals)
    class PreviewEmbedder(Embedder):
        def cached_vector(self,owner_kind,owner_key,text):
            return self.encode([text])[0]  # Preview never populates the persistent cache.
    targets={};embedder=PreviewEmbedder(cfg,store=None)
    from .cluster import is_duplicate,rejected_vectors
    rejected,matched=is_duplicate(learning['rule_text'],rejected_vectors(store,embedder),embedder.encode,cfg.cluster_dup_cosine)
    if rejected:raise CommandError('LessonRejected','This rule matches a rejected lesson: '+matched,409)
    for path,kind,label in candidates:
        try:
            if any(is_never_write(str(p)) for p in Path(path).parents):raise DestinationError('Production checkouts are not write targets.')
            if Path(path).resolve().is_relative_to(Path(cfg.production_repo_path).expanduser().resolve()):
                raise DestinationError('The production checkout is not a write target.')
            dest=resolve_destination(cfg,path,kind)
            if kind in {'project_claude_md','project_agents_md','rule_file'} and dest['mode']!='git_branch':
                raise DestinationError('This project is missing or is no longer a Git repository.')
            if dest['mode']=='git_branch' and is_never_write(dest['repo_root']):raise DestinationError('Production checkouts are not write targets.')
            reason=proposal_rejection(store,cfg,learning,dest['target_path'],kind,embedder)
            ident=_hash(destination_identity(dest))
            if ident not in targets:targets[ident]={'id':ident,'label':label,'destination':dest,'available':not reason,'reason':reason,'modes':['regenerate_patch'] if label=='Existing proposal target' else ['hook'] if kind=='hook' else ['correct_target','regenerate_patch']}
        except (DestinationError,OSError,UnicodeError) as exc:unavailable.append({'target_path':path,'cause':type(exc).__name__,'detail':str(exc)})
    return {'learning_id':learning_id,'rule_text':learning['rule_text'],'targets':list(targets.values()),'unavailable':unavailable,
        'proposals':[{'id':p['id'],'status':p['status'],'target_path':p['target_path'],'target_kind':p['target_kind']} for p in proposals if p['status'] not in DECIDED_STATUSES]}

def selection(learning_id,mode,target_id,proposal_ids):
    if mode not in MODES or not isinstance(learning_id,str) or not learning_id or not isinstance(target_id,str) or not target_id:
        raise CommandError('InvalidRecovery','Select a learning, recovery action, and known destination.')
    if (not isinstance(proposal_ids,list) or len(proposal_ids)>200 or any(not isinstance(p,str) or not p for p in proposal_ids)
            or len(set(proposal_ids))!=len(proposal_ids) or (mode=='regenerate_patch' and not proposal_ids)):
        raise CommandError('InvalidRecovery','Regeneration requires exact unique proposal IDs. Other recovery actions may select the undecided proposals they replace.')
    return {'learning_id':learning_id,'mode':mode,'target_id':target_id,'proposal_ids':sorted(proposal_ids)}

def source_snapshot(store,cfg,learning_id,mode,target_id,proposal_ids):
    args=selection(learning_id,mode,target_id,proposal_ids);learning=_learning(store,learning_id)
    target=next((t for t in options(store,cfg,learning_id)['targets'] if t['id']==target_id),None)
    if not target:raise CommandError('UnknownRecoveryTarget','Choose a destination returned by the recovery options.',409)
    if mode not in target['modes']:raise CommandError('InvalidRecoveryTarget','This destination is available only for its existing proposal selection.',409)
    if not target['available']:raise CommandError(target['reason']['code'],target['reason']['detail'],409)
    dest=target['destination']
    if (mode=='hook')!=(dest['target_kind']=='hook'):
        raise CommandError('InvalidRecoveryTarget','Hook requests use Claude settings; text recovery uses an instruction destination.',409)
    members=[]
    for pid in args['proposal_ids']:
        member=review_snapshot(store,pid,cfg);p=member['snapshot']['proposal']
        if p['learning_id']!=learning_id or p['status'] in DECIDED_STATUSES:
            raise CommandError('RecoverySelectionChanged','Select only undecided proposals for this learning.',409)
        if mode=='regenerate_patch' and destination_identity(member['snapshot']['destination'])!=destination_identity(dest):
            raise CommandError('MixedRecoveryTargets','Regenerate one destination at a time. Other proposals remain unchanged.',409)
        members.append(member)
    base=read_destination(dest)
    evidence=store.query('SELECT i.* FROM incidents i JOIN incident_learnings il ON i.id=il.incident_id WHERE il.learning_id=? ORDER BY i.ts,i.id',(learning_id,))
    return {'version':1,**args,'learning':learning,'destination':dest,'base':base,'members':members,'evidence':evidence}

def preview(store,cfg,learning_id,mode,target_id,proposal_ids):
    from .jobs import runtime_revision,POOLS
    from .call_budgets import call_pool
    require_schema(store);snapshot=source_snapshot(store,cfg,learning_id,mode,target_id,proposal_ids)
    source={'learning_id':learning_id,'revision':_hash(snapshot),'snapshot':snapshot}
    pool=call_pool(cfg,ACTION,cfg.strong_model_class)
    if pool=='gate':raise CommandError('InvalidBudget','Recovery generation must use a mining pool.',500)
    plan={'version':1,'action':ACTION,'source_revision':source['revision'],'runtime_revision':runtime_revision(),'configuration':asdict(cfg),
        'budgets':{p:int(pool==p) for p in POOLS},'stages':[{'stage':ACTION,'pool':pool,'maximum':1}]}
    revision=_hash(plan)
    latest=store.query_one('SELECT c.id,c.state FROM recovery_jobs j JOIN commands c ON c.id=j.command_id WHERE j.plan_hash=? ORDER BY c.created_at DESC,c.id DESC LIMIT 1',(revision,))
    return {'learning_id':learning_id,'mode':mode,'target_id':target_id,'proposal_ids':sorted(proposal_ids),'revision':revision,'source':source,'plan':plan,
        'latest_job':latest,'max_model_calls':1,'meaning':'Generate one proposal for manual Review. No evaluation, target change, or approval occurs during this job.'}

def save(store,cid,shown):
    store.insert('recovery_jobs',{'command_id':cid,'learning_id':shown['learning_id'],'source_json':_json(shown['source']),
        'source_hash':shown['source']['revision'],'plan_json':_json(shown['plan']),'plan_hash':shown['revision']})

def load(store,cid):
    require_schema(store);row=store.query_one('SELECT * FROM recovery_jobs WHERE command_id=?',(cid,))
    if row is None:raise CommandError('JobDataError','The recovery job has no retained source.',500)
    source=_parsed({'id':cid,**row},'source_json')
    if (set(source)!={'learning_id','revision','snapshot'} or source['learning_id']!=row['learning_id'] or source['revision']!=row['source_hash']
            or _hash(source['snapshot'])!=row['source_hash'] or source['snapshot'].get('version')!=1 or source['snapshot'].get('learning_id')!=row['learning_id']):
        raise CommandError('JobDataError','The recovery source differs from its fingerprint.',500)
    return row,source

def _unchanged(store,cfg,source):
    old=source['snapshot']
    try:current=source_snapshot(store,cfg,**{k:old[k] for k in ('learning_id','mode','target_id','proposal_ids')})
    except CommandError as exc:raise CommandError('RecoverySourceChanged',str(exc),409) from exc
    if _hash(current)!=source['revision']:
        raise CommandError('RecoverySourceChanged','The learning, selected proposals, evidence, or target changed. Inspect a fresh recovery preview.',409)

def _hook_patch(current,hook,target):
    from .propose import make_unified_diff
    if (not isinstance(hook,dict) or set(hook)!={'event','matcher','command','timeout'} or hook['event'] not in {'PreToolUse','UserPromptSubmit','Stop'}
            or not isinstance(hook['matcher'],str) or not isinstance(hook['command'],str) or not hook['command'].strip()
            or type(hook['timeout']) is not int or not 1<=hook['timeout']<=60):
        raise CommandError('InvalidHook','Return one command hook with a supported blocking event, matcher, and 1–60 second timeout.')
    if hook['event']!='PreToolUse' and hook['matcher']:
        raise CommandError('InvalidHook','This hook event does not use a tool matcher.')
    try:before=json.loads(current) if current.strip() else {}
    except ValueError as exc:raise CommandError('InvalidHookSettings','The selected settings file is not JSON.') from exc
    if not isinstance(before,dict) or ('hooks' in before and not isinstance(before['hooks'],dict)):
        raise CommandError('InvalidHookSettings','The settings and hooks values must be objects.')
    after=json.loads(_json(before));hooks=after.setdefault('hooks',{});groups=hooks.setdefault(hook['event'],[])
    if not isinstance(groups,list):raise CommandError('InvalidHookSettings','Existing event hooks must be a list.')
    added={'hooks':[{'type':'command','command':hook['command'],'timeout':hook['timeout']}]}
    if hook['matcher']:added['matcher']=hook['matcher']
    if added in groups:raise CommandError('ExistingHook','This exact command hook is already configured.',409)
    groups.append(added)
    return make_unified_diff(current,json.dumps(after,indent=2,ensure_ascii=False)+'\n',target)

def generate(store,cfg,saved,llm,journal):
    from .resources import bundled_path
    from .redact import redact_text
    from .resolutions import proposed_patch
    source=saved['source'];frozen=source['snapshot']
    prompt=bundled_path('prompts','propose_recovery.md').read_text()+'\nFROZEN INPUT\n'+redact_text(_json(frozen))
    def ask():
        _unchanged(store,cfg,source)
        answer=llm.call(ACTION,cfg.strong_model_class,prompt,True)
        if not answer.ok:raise CommandError('RecoveryModelFailed',f'{answer.outcome}: {answer.error}')
        value=answer.parsed
        if (not isinstance(value,dict) or type(value.get('supported')) is not bool or not isinstance(value.get('explanation'),str) or not value['explanation'].strip()):
            raise CommandError('InvalidRecoveryResponse','The model must explain whether a concrete recovery proposal is supported.')
        if not value['supported']:
            if set(value)!={'supported','explanation'}:raise CommandError('InvalidRecoveryResponse','An unsupported result cannot contain a patch.')
            return {'supported':False,'explanation':value['explanation'],'diff_unified':''}
        field='hook' if frozen['mode']=='hook' else 'edits'
        if set(value)!={'supported','explanation',field}:raise CommandError('InvalidRecoveryResponse','The response contains unknown fields.')
        if field=='hook':diff=_hook_patch(frozen['base']['content'],value['hook'],frozen['destination']['target_path'])
        else:diff=proposed_patch(frozen['base']['content'],{k:value[k] for k in ('explanation','edits')},frozen['destination']['target_path'])
        return {'supported':True,'explanation':value['explanation'],'diff_unified':diff}
    return journal.step('recovery:generate',{'source_revision':source['revision'],'prompt_hash':_hash(prompt)},ask)

def persist(store,cfg,saved,run_id,generated):
    _unchanged(store,cfg,saved['source']);source=saved['source']['snapshot']
    result={'run_id':run_id,'learning_id':source['learning_id'],'mode':source['mode'],'explanation':generated['explanation'],
        'proposal_id':None,'superseded_proposal_ids':[],'supported':generated['supported']}
    if not generated['supported']:return result
    pid=new_id();stamp=utc_now_iso();dest=source['destination']
    store.insert('proposals',{'id':pid,'learning_id':source['learning_id'],'run_id':run_id,'target_path':dest['target_path'],
        'target_kind':dest['target_kind'],'action':'recover_rule','diff_unified':generated['diff_unified'],'status':'pending','created_at':stamp})
    record={'version':1,'proposal_id':pid,'command_id':saved['id'],'source':source,**generated}
    store.insert('proposal_recoveries',{'proposal_id':pid,'command_id':saved['id'],'record_json':_json(record),'record_hash':_hash(record),'created_at':stamp})
    store.insert('proposal_events',{'id':new_id(),'proposal_id':pid,'ts':stamp,'event':'created','actor':actor_for('created'),
        'note':_json({'command_id':saved['id'],'reason':source['mode']})})
    for old in source['proposal_ids']:
        store.update('proposals','id',old,{'status':'superseded'})
        store.insert('proposal_events',{'id':new_id(),'proposal_id':old,'ts':stamp,'event':'superseded','actor':actor_for('superseded'),
            'note':_json({'replacement_proposal_id':pid,'command_id':saved['id']})})
    result.update(proposal_id=pid,superseded_proposal_ids=source['proposal_ids'])
    return result

def origin(store,proposal):
    if proposal['action']!='recover_rule':return None
    require_schema(store);row=store.query_one('SELECT * FROM proposal_recoveries WHERE proposal_id=?',(proposal['id'],))
    if row is None:raise CommandError('RecoveryDataError','This recovery proposal has no retained origin.',500)
    from .jobs import status
    command=store.query_one('SELECT * FROM commands WHERE id=?',(row['command_id'],))
    if command is None or command['action']!=ACTION:raise CommandError('RecoveryDataError','The origin has no recovery command.',500)
    source=status(store,command)['source']
    record=_parsed({'id':proposal['id'],**row},'record_json')
    dest=source['snapshot']['destination']
    if (_hash(record)!=row['record_hash'] or record.get('version')!=1 or record.get('source')!=source['snapshot']
            or record.get('proposal_id')!=proposal['id'] or record.get('command_id')!=row['command_id']
            or record.get('diff_unified')!=proposal['diff_unified'] or proposal['learning_id']!=source['learning_id']
            or proposal['target_path']!=dest['target_path'] or proposal['target_kind']!=dest['target_kind']):
        raise CommandError('RecoveryDataError','The recovery proposal differs from its retained source.',500)
    return record


def view(store,cfg,learning_id,mode,target_id,proposal_ids,*,fresh=False):
    args=selection(learning_id,mode,target_id,proposal_ids);require_schema(store)
    if not fresh:
        from .jobs import status
        for row in store.query('SELECT c.* FROM commands c JOIN recovery_jobs j ON j.command_id=c.id WHERE j.learning_id=? ORDER BY c.created_at DESC,c.id DESC',(learning_id,)):
            saved=status(store,row)
            if saved['selection']==args:
                return {**args,'revision':_hash(saved['plan']),'source':saved['source'],'plan':saved['plan'],
                    'latest_job':{'id':row['id'],'state':row['state']},'max_model_calls':saved['max_model_calls'],
                    'meaning':'Retained recovery request. Inspect its result and original source in history.'}
    return preview(store,cfg,**args)
