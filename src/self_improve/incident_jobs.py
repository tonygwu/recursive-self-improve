"""Frozen selected-incident mining. Reads and intent never invoke a model."""
from dataclasses import asdict, replace
from pathlib import Path
import json
import shlex
import sys

from .commands import CommandError, _hash, _json, _parsed
from .store import new_id, utc_now_iso, actor_for

ACTION = 'mine_incident'
MIGRATION = '0019_incident_jobs'


def require_schema(store):
    if not store.query_one('SELECT name FROM schema_migrations WHERE name=?',(MIGRATION,)):
        raise CommandError('UpgradeRequired','Upgrade the database before requesting selected-incident mining.',503)
    from .mining_history import require_schema as require_history, HistoryError
    try:require_history(store)
    except HistoryError as exc:raise CommandError('UpgradeRequired',str(exc),503) from exc


def snapshot(store,cfg,incident_id):
    from . import miner, render
    from .redact import redact_text
    from .resources import bundled_path
    from .search import _in_force_rule_units
    incident=store.query_one('SELECT * FROM incidents WHERE id=?',(incident_id,))
    if not incident:raise CommandError('NoSuchIncident','No incident with that ID.',404)
    if incident['status']!='new':
        raise CommandError('IncidentAlreadyHandled','This incident is already mined or dismissed. Inspect its existing evidence and results.',409)
    session=store.query_one('SELECT * FROM sessions WHERE file_path=?',(incident['session_file'],))
    if not session:raise CommandError('IncidentDataError','The incident has no source session.',500)
    if cfg.mine_mode not in {'agentic','fast'}:raise CommandError('InvalidMineMode','Mining mode must be agentic or fast.',500)
    from .incident_evidence import parse_window, IncidentEvidenceError
    try:window=parse_window(incident['window_json'], owner='incident.'+incident_id)
    except IncidentEvidenceError as exc:raise CommandError('IncidentDataError',str(exc),500) from exc
    files={};coverage={'kind':'retained_window'}
    if cfg.mine_mode=='agentic':
        try:
            events,start=miner._full_mine_events(store,incident,cfg);coverage['kind']='full_session'
        except miner.TranscriptAgedOut:
            events,start=miner._fallback_mine_events(store,incident)
        transcript,stats=render.session_document(events)
        if coverage['kind']=='retained_window':transcript=miner.AGED_OUT_NOTE+'\n\n'+transcript
        files={'transcript.md':transcript,
            'environment.md':redact_text(render.environment_document(store,{**incident,'start_line':start},cfg,transcript))}
        coverage.update(asdict(stats),bytes=len(transcript.encode()))
        mapping={'signal_type':incident['signal_type'],'matched_text':incident['matched_text'],
            'start_line':str(start),'project':incident['project_path'] or '(unknown)',
            'search_cli':'__FROZEN_SEARCH__','max_turns':str(cfg.mine_agent_max_turns),
            'explore_budget':str(max(1,cfg.mine_agent_max_turns-miner.TURN_RESERVE))}
        prompt,template_sha=miner.render_prompt_revision(bundled_path('prompts','mine_incident_agentic.md'),mapping)
    else:
        if not isinstance(window,list) or not window:raise CommandError('IncidentDataError','The retained window has no evidence.',409)
        prompt,template_sha=miner.render_prompt_revision(bundled_path('prompts','mine_incident.md'),{
            'signal_type':incident['signal_type'],'project':incident['project_path'] or '(unknown)',
            'window':json.dumps(window,indent=2,ensure_ascii=False),
            'in_force_instructions':miner.gather_in_force_instructions(incident['project_path'],cfg)})
    learnings=store.query('SELECT * FROM learnings ORDER BY id')
    fields=('id','status','rule_text','why','category','scope','evidence_count','project_count','duplicate_of')
    rows=[{k:redact_text(r[k]) if k in {'rule_text','why','duplicate_of'} else r[k] for k in fields} for r in learnings]
    projects=[r['project_path'] for r in store.query("SELECT DISTINCT project_path FROM sessions WHERE project_path!='' ORDER BY project_path")]
    unreadable=[]
    rules=[{'text':redact_text(text),'path':str(path)} for text,path in _in_force_rule_units(cfg,projects,unreadable=unreadable)]
    coverage['unreadable_instructions']=unreadable
    corpus={'learnings':rows,'rules':rules}
    files['learnings.jsonl']=''.join(_json(r)+'\n' for r in rows)
    incident_revision=_hash(incident)
    incident={**incident,'matched_text':redact_text(incident['matched_text']),'window_json':redact_text(incident['window_json'])}
    source={'version':1,'kind':'incident','incident':incident,'incident_revision':incident_revision,'session':session,
        'coverage':coverage,'files':files,'prompt':redact_text(prompt),'template_sha':template_sha,'corpus':corpus,
        'learning_revisions':{r['id']:_hash(r) for r in learnings}}
    return {'incident_id':incident_id,'revision':_hash(source),'snapshot':source}


def preview(store,cfg,incident_id):
    from .jobs import runtime_revision,POOLS
    from .call_budgets import call_pool
    require_schema(store);source=snapshot(store,cfg,incident_id)
    stage='mine_agentic' if cfg.mine_mode=='agentic' else 'mine'
    pool=call_pool(cfg,stage,cfg.cheap_model_class)
    if pool=='gate':raise CommandError('InvalidBudget','Selected mining must use a mining pool.',500)
    plan={'version':1,'action':ACTION,'source_revision':source['revision'],'runtime_revision':runtime_revision(),
        'configuration':asdict(cfg),'budgets':{p:int(p==pool) for p in POOLS},
        'stages':[{'stage':stage,'pool':pool,'maximum':1}]}
    revision=_hash(plan)
    latest=store.query_one('SELECT c.id,c.state FROM incident_jobs j JOIN commands c ON c.id=j.command_id WHERE j.plan_hash=? ORDER BY c.created_at DESC,c.id DESC LIMIT 1',(revision,))
    return {'incident_id':incident_id,'revision':revision,'source':source,'plan':plan,
        'latest_job':latest,'max_model_calls':1,'ready':True,
        'meaning':'Mine this incident once. A new change returns to manual Review without evaluation or instruction delivery. An agent session counts as one logical call; provider retries stay within that call.'}


def save(store,cid,shown):
    store.insert('incident_jobs',{'command_id':cid,'incident_id':shown['incident_id'],
        'source_json':_json(shown['source']),'source_hash':shown['source']['revision'],
        'plan_json':_json(shown['plan']),'plan_hash':shown['revision']})


def load(store,cid):
    require_schema(store)
    row=store.query_one('SELECT * FROM incident_jobs WHERE command_id=?',(cid,))
    if row is None:raise CommandError('JobDataError','The incident job has no frozen source.',500)
    source=_parsed({'id':cid,**row},'source_json')
    if (set(source)!={'incident_id','revision','snapshot'} or source['incident_id']!=row['incident_id']
            or source['revision']!=row['source_hash'] or _hash(source['snapshot'])!=row['source_hash']
            or source['snapshot'].get('kind')!='incident' or source['snapshot'].get('version')!=1
            or source['snapshot'].get('incident',{}).get('id')!=row['incident_id']):
        raise CommandError('JobDataError','The incident source differs from its frozen fingerprint.',500)
    return row,source


def _unchanged(store,source):
    current=store.query_one('SELECT * FROM incidents WHERE id=?',(source['incident']['id'],))
    if _hash(current)!=source['incident_revision']:
        raise CommandError('IncidentChanged','This incident changed or was handled after the preview. Existing results are preserved.',409)


def generate(store,cfg,saved,llm,journal,run_dir):
    from . import miner
    source=saved['source']['snapshot']
    from .incident_evidence import parse_window, IncidentEvidenceError
    try:parse_window(source['incident']['window_json'], owner='incident.'+source['incident']['id'])
    except IncidentEvidenceError as exc:raise CommandError('IncidentDataError',str(exc),500) from exc
    def ask():
        _unchanged(store,source)
        sandbox=run_dir/'mine'/source['incident']['id'];sandbox.mkdir(parents=True,exist_ok=True)
        for name,text in source['files'].items():
            if name not in {'transcript.md','environment.md','learnings.jsonl'}:
                raise CommandError('JobDataError','Unknown frozen mining file.',500)
            (sandbox/name).write_text(text,encoding='utf-8')
        context={'corpus':source['corpus'],'configuration':{k:getattr(cfg,k) for k in ('embedding_model','embedding_revision','embedding_model_sha256')}}
        (sandbox/'search-corpus.json').write_text(_json(context),encoding='utf-8')
        cli=shlex.quote(sys.executable)+' -m self_improve.frozen_search'
        prompt=source['prompt'].replace('__FROZEN_SEARCH__',cli)
        from . import mining_history
        history=mining_history.context(cfg.mine_mode,prompt,source['template_sha'],{'run_id':saved['run_id'],'command_id':saved['id']})
        if cfg.mine_mode=='agentic':
            llm.cfg=replace(llm.cfg,mine_search_cli=cli,mine_agent_allowed_tools=('Read','Grep','Glob',f'Bash({cli} search-learnings:*)'))
            answer=llm.call_agentic('mine_agentic',cfg.cheap_model_class,prompt,cwd=str(sandbox),expect_json=True)
        else:answer=llm.call('mine',cfg.cheap_model_class,prompt,True)
        if not answer.ok:raise CommandError('MiningCallFailed',f'{answer.outcome}: {answer.error}')
        payload,defaults=miner.normalize_mine_payload(answer.parsed,agentic=cfg.mine_mode=='agentic')
        errors=miner.validate_mine_json(payload,agentic=cfg.mine_mode=='agentic')
        if errors:raise CommandError('MiningContractViolation','; '.join(errors))
        mining_history.capture(history,answer)
        return {'payload':payload,'defaulted_keys':defaults,'coverage':source['coverage'],'provenance':history}
    return journal.step('incident:mine',{'source_revision':saved['source']['revision']},ask)


def persist(store,cfg,saved,run_id,generated):
    """Save one mine result and any Review proposal in the caller's transaction."""
    from . import miner
    from .rejections import lesson_rejected
    source=saved['source']['snapshot'];incident=source['incident'];_unchanged(store,source)
    payload=generated['payload'];target_id=payload.get('dedup_target_id')
    if target_id:
        current=store.query_one('SELECT * FROM learnings WHERE id=?',(target_id,))
        if current is None or (not lesson_rejected(store,target_id) and _hash(current)!=source['learning_revisions'].get(target_id)):
            raise CommandError('LearningChanged','The referenced learning changed after the preview. Its existing revision is preserved.',409)
    learning=miner._persist_mine_payload(store,incident,payload,agentic=cfg.mine_mode=='agentic',commit=False,provenance=generated['provenance'])
    result={'incident_id':incident['id'],'run_id':run_id,'proposal_ids':[],
        'learning_id':learning['id'] if learning else None,'outcome':'dismissed' if learning is None else learning['_dedup'],
        'summary':payload['incident_summary'],'coverage':generated['coverage'],'defaulted_keys':generated['defaulted_keys']}
    if learning is None or learning['_dedup']=='duplicate_of_rejected':return result
    if learning['_dedup']=='duplicate':
        from .execution_policy import waiting_proposals
        result['proposal_ids']=[p['id'] for p in waiting_proposals(store,cfg) if p['learning_id']==learning['id']]
        return result
    from .propose import ProposalError
    from .routing import RoutingError
    from .destinations import DestinationError
    from .embeddings import EmbeddingError
    try:draft=_build_proposal(store,cfg,learning,source)
    except (CommandError,ProposalError,RoutingError,DestinationError,OSError,UnicodeError,EmbeddingError) as exc:
        code=getattr(exc,'code',type(exc).__name__)
        result.update(outcome='mined_without_proposal',proposal_error={'code':code,'detail':str(exc)},taxonomy={'propose_'+code:1})
        return result
    pid=new_id();stamp=utc_now_iso()
    store.insert('proposals',{'id':pid,'learning_id':learning['id'],'run_id':run_id,
        'target_path':draft['target_path'],'target_kind':draft['target_kind'],'action':draft['action'],
        'diff_unified':draft['diff_unified'],'status':'pending','created_at':stamp})
    store.insert('proposal_events',{'id':new_id(),'proposal_id':pid,'ts':stamp,'event':'created','actor':actor_for('created'),
        'note':_json({'reason':'selected_incident_mining','run_id':run_id,'incident_id':source['incident']['id'],'demotion_reason':draft.get('demotion_reason','')})})
    store.update('learnings','id',learning['id'],{'status':'proposed'})
    result['proposal_ids']=[pid]
    return result


def _build_proposal(store,cfg,learning,source):
    from . import routing,propose
    from .destinations import resolve_destination,read_destination
    from .rejections import lesson_rejected,proposal_rejection
    from .embeddings import Embedder
    from .cluster import is_duplicate,rejected_vectors
    if lesson_rejected(store,learning['id']):raise CommandError('LessonRejected','The lesson remains rejected.',409)
    if learning.get('duplicate_of'):
        raise CommandError('ExistingInstruction','The miner identified an instruction already covering this lesson.',409)
    prior=store.query_one("SELECT * FROM proposals WHERE learning_id=? AND status='applied' ORDER BY created_at DESC,id DESC LIMIT 1",(learning['id'],))
    embedder=Embedder(cfg,store=store)
    duplicate,matched=is_duplicate(learning['rule_text'],rejected_vectors(store,embedder),embedder.encode,cfg.cluster_dup_cosine)
    if duplicate:raise CommandError('LessonRejected','This rule matches a rejected lesson: '+matched,409)
    if not prior:
        from .search import search_corpus
        matches=search_corpus(cfg,{'learnings':[],'rules':source['corpus']['rules']},learning['rule_text'],top_k=1)
        if matches and matches[0]['cosine']>=cfg.cluster_dup_cosine:
            raise CommandError('ExistingInstruction','An instruction already covers this lesson: '+matches[0]['rule_text'],409)

    route=routing.route(learning,cfg) if not prior else None
    candidate={'target_path':prior['target_path'] if prior else route.target_path,'target_kind':prior['target_kind'] if prior else route.target_kind}
    dest=resolve_destination(cfg,candidate['target_path'],candidate['target_kind']);current=read_destination(dest)['content']
    reason=proposal_rejection(store,cfg,learning,dest['target_path'],dest['target_kind'],embedder)
    if reason:raise CommandError(reason['code'],reason['detail'],409)
    draft=propose.build_edit_proposal(learning,dest['target_path'],current,cfg,target_kind=dest['target_kind']) if prior else propose.build_proposal(learning,route,current,cfg)
    if not draft['diff_unified']:
        raise CommandError('HookProposalRequired' if draft['action']=='convert_to_hook' else 'EmptyProposal','Mining produced no executable patch. Request a reviewed correction or hook proposal.',409)
    return draft


def view(store,cfg,incident_id,*,full=False):
    """Small previews keep full retained inputs available on explicit inspection."""
    from .incident_evidence import present, IncidentEvidenceError
    def presentation(incident):
        try:return present(incident,max_chars=4000)
        except IncidentEvidenceError as exc:raise CommandError('IncidentDataError',str(exc),500) from exc
    try:shown=preview(store,cfg,incident_id)
    except CommandError as exc:
        if exc.code!='IncidentAlreadyHandled':raise
        require_schema(store)
        row=store.query_one('SELECT c.* FROM incident_jobs j JOIN commands c ON c.id=j.command_id WHERE j.incident_id=? ORDER BY c.created_at DESC,c.id DESC LIMIT 1',(incident_id,))
        if row:
            from .jobs import status
            saved=status(store,row)
            shown={'incident_id':incident_id,'source':saved['source'],'plan':saved['plan'],
                'revision':_hash(saved['plan']),'max_model_calls':saved['max_model_calls'],
                'latest_job':{'id':row['id'],'state':row['state']},'ready':False,
                'meaning':str(exc),'result':saved['result']}
        else:
            incident=store.query_one('SELECT * FROM incidents WHERE id=?',(incident_id,))
            return {'incident_id':incident_id,'ready':False,'max_model_calls':0,'latest_job':None,
                'revision':_hash(incident),'plan':{'stages':[]},'meaning':str(exc),
                'source_summary':{'incident':incident,'presentation':presentation(incident),'coverage':{},'files':[],'learning_count':0,'instruction_count':0}}
    source=shown['source']['snapshot']
    summary={'incident':source['incident'],'presentation':presentation(source['incident']),'coverage':source['coverage'],
        'files':[{'name':k,'bytes':len(v.encode())} for k,v in source['files'].items()],
        'learning_count':len(source['corpus']['learnings']),'instruction_count':len(source['corpus']['rules'])}
    return shown | {'source_summary':summary} if full else {k:v for k,v in shown.items() if k not in {'source','plan'}} | {
        'source_summary':summary,'stages':shown['plan']['stages']}


def page(store,*,limit=25,cursor=None):
    import base64
    from .dashboard.queries import normalize_incident
    if type(limit) is not int or not 1<=limit<=100:raise CommandError('InvalidLimit','Use a limit from 1 to 100.')
    where="status='new'";params=()
    if cursor:
        try:
            key=json.loads(base64.b64decode(cursor+'='*(-len(cursor)%4),altchars=b'-_',validate=True))
            if not isinstance(key,list) or len(key)!=2 or not all(isinstance(v,str) for v in key):raise ValueError()
        except (ValueError,TypeError) as exc:raise CommandError('InvalidCursor','Use the returned incident cursor.') from exc
        where+=' AND (created_at<? OR (created_at=? AND id<?))';params=(key[0],key[0],key[1])
    rows=store.query('SELECT * FROM incidents WHERE '+where+' ORDER BY created_at DESC,id DESC LIMIT ?',(*params,limit+1))
    shown=rows[:limit]
    more=base64.urlsafe_b64encode(_json([shown[-1]['created_at'],shown[-1]['id']]).encode()).decode().rstrip('=') if len(rows)>limit else None
    return {'items':[normalize_incident(r) for r in shown],'count':store.query_one("SELECT COUNT(*) AS n FROM incidents WHERE status='new'")['n'],'next_cursor':more}
