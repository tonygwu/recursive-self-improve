"""Immutable learning content and the exact mining call that produced it."""
import hashlib
import json
from pathlib import Path

from .store import new_id, utc_now_iso

MIGRATION = '0021_mining_history'
CONTENT_KINDS = ('new', 'amend', 'amend_applied', 'cluster_merge')
KINDS = CONTENT_KINDS + ('duplicate', 'duplicate_of_rejected')
CONTENT_FIELDS = ('rule_text', 'why', 'title', 'category', 'scope', 'path_globs_json',
                  'violated_existing_rule', 'incident_summary')


class HistoryError(ValueError):
    """A mining history record or its call contradicts its recorded identity."""


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def content_hash(learning):
    return digest({k:learning.get(k, '') for k in CONTENT_FIELDS})


def require_schema(store):
    if not store.query_one('SELECT name FROM schema_migrations WHERE name=?', (MIGRATION,)):
        raise HistoryError('Upgrade the database before mining: '+MIGRATION+' is required.')


def context(generation, prompt, template_sha, extra=None):
    """Capture the template and rendered input before calling a model."""
    root=Path(__file__).resolve().parent
    return {**(extra or {}), 'generation':generation,
            'stage':'mine_agentic' if generation=='agentic' else 'mine',
            'template_sha':template_sha,
            'prompt_sha':hashlib.sha256(prompt.encode()).hexdigest(),
            'code_sha':digest({name:hashlib.sha256((root/name).read_bytes()).hexdigest()
                               for name in ('miner.py','cluster.py','render.py','mining_history.py')})}


def capture(provenance, result):
    if provenance is not None:
        provenance['call_id']=getattr(result, 'call_id', '')


def append(store, learning, *, before, kind, incidents, provenance=None, source_learnings=()):
    """Append in the caller's content transaction. This function never commits."""
    require_schema(store)
    if kind not in KINDS:raise HistoryError('Unknown mining history kind: '+str(kind))
    p=dict(provenance or {})
    if p.get('generation', '') not in ('', 'fast', 'agentic'):
        raise HistoryError('Unknown miner generation: '+str(p.get('generation')))
    call=None
    if p.get('call_id'):
        call=store.query_one('SELECT * FROM llm_calls WHERE id=?', (p['call_id'],))
        if (not call or call['run_id']!=p.get('run_id', '') or call['prompt_sha']!=p.get('prompt_sha')
                or call['stage']!=p.get('stage')):
            raise HistoryError('Mining call '+p['call_id']+' differs from the producing run, stage, or prompt.')
    row={'id':new_id(), 'learning_id':learning['id'], 'kind':kind,
         'generation':p.get('generation',''), 'run_id':p.get('run_id',''),
         'command_id':p.get('command_id',''), 'call_id':p.get('call_id',''),
         'content_hash':content_hash(learning), 'created_at':utc_now_iso()}
    record={**row, 'version':1, 'before':before, 'after':{k:v for k,v in learning.items() if not k.startswith('_')},
            'incidents':incidents, 'source_learnings':list(source_learnings), 'call':call,
            'stage':p.get('stage',''), 'template_sha':p.get('template_sha',''),
            'prompt_sha':p.get('prompt_sha',''), 'code_sha':p.get('code_sha','')}
    store.insert('mining_history', {**row, 'record_json':encoded(record), 'record_hash':digest(record)})
    return record


def read(row):
    owner='mining_history.'+row['id']
    try:
        r=json.loads(row['record_json'])
        fingerprint=digest(r)
    except (ValueError,TypeError) as exc:raise HistoryError(owner+': invalid record JSON') from exc
    if (not isinstance(r,dict) or r.get('version')!=1 or fingerprint!=row['record_hash']
            or not isinstance(r.get('after'),dict) or not isinstance(r.get('incidents'),list)
            or not isinstance(r.get('source_learnings'),list)
            or not (r.get('before') is None or isinstance(r['before'],dict))
            or r.get('kind') not in KINDS or r.get('generation') not in ('','fast','agentic')
            or r['after'].get('id')!=row['learning_id'] or content_hash(r['after'])!=row['content_hash']
            or any(r.get(k)!=row[k] for k in ('id','learning_id','kind','generation','run_id','command_id','call_id','content_hash','created_at'))):
        raise HistoryError(owner+': record differs from its fingerprint or shape')
    call=r.get('call')
    if r['call_id']:
        if (not isinstance(call,dict) or call.get('id')!=r['call_id'] or call.get('run_id')!=r['run_id']
                or call.get('prompt_sha')!=r.get('prompt_sha') or call.get('stage')!=r.get('stage')):
            raise HistoryError(owner+': producing call differs from its recorded identity')
    elif call is not None:raise HistoryError(owner+': call has no recorded ID')
    return r


def summary(store, learning):
    """The last content writer owns generation; later evidence does not replace it."""
    slots=','.join('?' for _ in CONTENT_KINDS)
    row=store.query_one(f'SELECT * FROM mining_history WHERE learning_id=? AND kind IN ({slots}) ORDER BY created_at DESC,id DESC LIMIT 1',
                        (learning['id'],*CONTENT_KINDS))
    count=store.query_one('SELECT COUNT(*) AS n FROM mining_history WHERE learning_id=?',(learning['id'],))['n']
    return _summary(row,learning,count)


def summaries(store, learnings):
    """Two bulk reads for the Rules table; do not query twice per learning."""
    slots=','.join('?' for _ in CONTENT_KINDS)
    rows=store.query(f'''SELECT h.* FROM mining_history h WHERE h.kind IN ({slots})
        AND NOT EXISTS (SELECT 1 FROM mining_history later WHERE later.learning_id=h.learning_id
        AND later.kind IN ({slots}) AND (later.created_at>h.created_at OR
        (later.created_at=h.created_at AND later.id>h.id)))''',(*CONTENT_KINDS,*CONTENT_KINDS))
    latest={r['learning_id']:r for r in rows}
    counts={r['learning_id']:r['n'] for r in store.query('SELECT learning_id,COUNT(*) AS n FROM mining_history GROUP BY learning_id')}
    return {l['id']:_summary(latest.get(l['id']),l,counts.get(l['id'],0)) for l in learnings}


def _summary(row, learning, count):
    unknown={'value':'—','computable':False,'reason':'No recorded mining run_id proves who produced this content. Historical scan runs are not mining provenance.', 'history_count':count}
    if not row:return unknown
    r=read(row)
    if r['content_hash']!=content_hash(learning):
        return {**unknown,'reason':'Current content differs from its last recorded mining revision. Its generation is unknown.'}
    if not r['generation']:return unknown
    return {'value':r['generation'],'computable':True,'reason':'', 'history_count':count,
            **{k:r[k] for k in ('id','run_id','command_id','call_id','created_at','kind','template_sha','prompt_sha','code_sha')},
            'model_requested':(r['call'] or {}).get('model_requested',''),
            'model_reported':(r['call'] or {}).get('model_reported','')}


def page(store, learning_id, *, limit=20, cursor=None):
    require_schema(store)
    if type(limit) is not int or not 1<=limit<=100:raise HistoryError('History limit must be 1–100.')
    clauses='learning_id=?';args=[learning_id]
    if cursor:
        anchor=store.query_one('SELECT created_at FROM mining_history WHERE learning_id=? AND id=?',(learning_id,cursor))
        if not anchor:raise HistoryError('Unknown mining history cursor for learning '+learning_id)
        clauses+=' AND (created_at<? OR (created_at=? AND id<?))';args += [anchor['created_at'],anchor['created_at'],cursor]
    rows=store.query('SELECT * FROM mining_history WHERE '+clauses+' ORDER BY created_at DESC,id DESC LIMIT ?',(*args,limit+1))
    return {'records':[read(row) for row in rows[:limit]],'next_cursor':rows[limit-1]['id'] if len(rows)>limit else None}
