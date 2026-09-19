"""Complete retained evidence search through one caller-owned Store transaction.

No live semantic search, target inspection, instruction/transcript read, new
SQLite connection or model call belongs here. Search indexes the retained native
records directly; it does not make a duplicate mutable search database.
"""
from __future__ import annotations

import base64
from functools import wraps
import sqlite3
from collections import Counter, defaultdict
from copy import deepcopy
import json
import re
import sys
import unicodedata
from urllib.parse import quote

from .. import instruction_text, instruction_inventory, rule_revisions, session_context, native_loads
from .. import scan_observations as scan
from ..mining_history import digest, encoded
from ..redact import redact_text
from . import evidence_identity as identity

PROFILE = 'retained-evidence/1'
KINDS = ('learning', 'proposal', 'incident', 'session', 'instruction', 'revision')


class EvidenceError(ValueError):
    def __init__(self, detail, *, code='InvalidEvidenceRequest', status=400):
        super().__init__(detail)
        self.code, self.status = code, status


def _corrupt(owner, detail):
    return EvidenceError(owner + ': ' + str(detail), code='EvidenceDataError', status=500)


def _data_errors(reader):
    @wraps(reader)
    def wrapped(*args, **kwargs):
        try:
            return reader(*args, **kwargs)
        except EvidenceError:
            raise
        except (ValueError, KeyError, TypeError, sqlite3.DatabaseError, scan.ScanObservationError) as exc:
            raise _corrupt('retained evidence', exc) from exc
    return wrapped


def _has(store, migration):
    return bool(store.query_one('SELECT name FROM schema_migrations WHERE name=?', (migration,)))


def _object(pairs):
    result = {}
    for k,v in pairs:
        if k in result: raise ValueError('duplicate JSON key ' + k)
        result[k] = v
    return result


def _constant(value):
    raise ValueError('non-JSON constant ' + value)


def _json(value, owner, shape):
    try:
        result = json.loads(value, object_pairs_hook=_object, parse_constant=_constant)
        if not isinstance(result, shape): raise ValueError('wrong JSON shape')
        return result
    except (TypeError, ValueError) as exc:
        raise _corrupt(owner, exc) from exc


def _redacted(value):
    if isinstance(value, str): return redact_text(value)
    if isinstance(value, list): return [_redacted(v) for v in value]
    if isinstance(value, dict): return {k:_redacted(v) for k,v in value.items()}
    return value


def _incident(row):
    owner = 'incident.' + row['id']
    from ..incident_evidence import prepare
    prepared = prepare(row, owner=owner, max_chars=4000, transform_text=redact_text)
    presentation = prepared['presentation']
    source = {k:v for k,v in row.items() if k != 'window_json'}
    # Every presentation string derives from these fully redacted inputs or constants.
    source.update(matched_text=prepared['matched_text'] if not presentation['fingerprint'] else '',
                  fingerprint=presentation['fingerprint'] or None, window=prepared['window'],
                  window_kind=presentation['window_kind'], presentation=presentation,
                  time_status='known' if scan.normalize_timestamp(row['ts']) else 'unknown',
                  retention='Retained incident archive; a complete original transcript is not implied.')
    return source


def _content(row, fields, arrays=()):
    source = dict(row)
    for field in fields:
        if field in source: source[field] = redact_text(source[field])
    for field in arrays:
        if field in source: source[field.removesuffix('_json')] = _json(source.pop(field),row['id']+'.'+field,list)
    return source


def _search_source(value):
    # Exclude only reader-added annotations at their known locations. User
    # objects inside archived windows may legitimately use these field names.
    result = {k:v for k,v in value.items() if k not in {'meaning','retention','target_attribution','transcript_scope','presentation'}}
    if result.get('identity'):
        result['identity']=identity.search_values(result['identity'])
    if 'transcripts' in result:
        result['transcripts'] = [{k:v for k,v in transcript.items() if k!='session_id'} for transcript in result['transcripts']]
    if 'incidents' in result:
        result['incidents'] = [_search_source(incident) for incident in result['incidents']]
    return result


def _text(value):
    """Decoded retained strings, including metadata; no JSON escape sequences."""
    if isinstance(value, str): return value
    if isinstance(value, dict): return '\n'.join(k+'\n'+_text(v) for k,v in value.items())
    if isinstance(value, list): return '\n'.join(_text(v) for v in value)
    return '' if value is None else str(value)


def _fold(value):
    return unicodedata.normalize('NFC', value).casefold()


def _record(kind, source_id, title, source, projects=(), learning_ids=(), incident_ids=(), *, source_text=None, title_redacted=False):
    title = title if title_redacted else redact_text(title)
    source_text = _text(_search_source(source)) if source_text is None else source_text
    return {'kind':kind, 'source_id':source_id, 'key':kind+':'+source_id, 'title':title,
            'project_keys':sorted(set(projects)-{''}), 'learning_ids':sorted(set(learning_ids)),
            'incident_ids':sorted(set(incident_ids)), 'source':source, 'identity':source.get('identity'),
            '_text':unicodedata.normalize('NFC', kind+'\n'+source_id+'\n'+title+'\n'+source_text)}


def _revision_record(row):
    """Hash each complete incident once, retaining its ordered session membership."""
    result={k:v for k,v in row.items() if k not in {'_text','identity'}}
    source=dict(result['source'])
    if row['kind']=='incident':source.pop('presentation',None)
    if row['kind']=='session':source['incidents']=[r['id'] for r in source['incidents']]
    result['source']=source
    return result


def _catalog(store):
    """One complete retained source revision; callers already own the read snapshot."""
    learnings = {r['id']:r for r in store.query('SELECT * FROM learnings ORDER BY id')}
    proposals = {r['id']:r for r in store.query('SELECT * FROM proposals ORDER BY id')}
    incidents = {r['id']:r for r in store.query('SELECT * FROM incidents ORDER BY id')}
    sessions = store.query('SELECT * FROM sessions ORDER BY source,session_id,file_path')
    identities=identity.metadata_index(store,sessions=sessions)
    links = store.query('SELECT * FROM incident_learnings ORDER BY learning_id,incident_id')
    by_learning, by_incident = defaultdict(list), defaultdict(list)
    for link in links:
        if link['learning_id'] not in learnings or link['incident_id'] not in incidents:
            raise _corrupt('incident_learnings', 'dangling learning or incident identity')
        by_learning[link['learning_id']].append(link['incident_id'])
        by_incident[link['incident_id']].append(link['learning_id'])
    revisions = rule_revisions.retained_revisions(store) if _has(store,rule_revisions.MIGRATION) else []
    revision_map = {r['id']:r for r in revisions}
    projects = {lid:{incidents[i]['project_key'] for i in by_learning[lid] if incidents[i]['project_key']} for lid in learnings}
    rows = []
    for lid,row in learnings.items():
        source = _content(row,('title','rule_text','why','incident_summary','violated_existing_rule'),('projects_json','path_globs_json'))
        source['linked_incident_ids'] = by_learning[lid]
        source['canonical_evidence_project_count'] = len(projects[lid])
        rows.append(_record('learning',lid,source['title'] or source['rule_text'],source,projects[lid],[lid],by_learning[lid]))
    for pid,row in proposals.items():
        if row['learning_id'] not in learnings: raise _corrupt('proposal.'+pid,'missing learning')
        lid = row['learning_id']
        target_projects = {r['project_key'] for r in revisions if r['proposal_id']==pid and r['project_key']}
        source = _content(row,('diff_unified',))
        source['target_attribution'] = 'Raw proposed path; canonical destinations require retained revisions or explicit target inspection.'
        rows.append(_record('proposal',pid,learnings[lid]['title'] or row['action'],source,projects[lid]|target_projects,[lid],by_learning[lid]))
    incident_sources = {iid:_incident(row) for iid,row in incidents.items()}
    incident_search = {}
    for iid,source in incident_sources.items():
        source['learning_ids'] = by_incident[iid]
        incident_search[iid] = _text(_search_source(source))
        rows.append(_record('incident',iid,source['matched_text'] or 'Retained '+redact_text(source['signal_type'])+' incident',source,[source['project_key']],by_incident[iid],[iid],source_text=incident_search[iid],title_redacted=True))

    # Native identity includes provider; missing IDs remain separate transcripts.
    session_groups = {}
    def group(source, native_id, path='', logical_key=''):
        sid = logical_key or identity.session_key(source,native_id,path,cache=identities['session_keys'])
        entry = session_groups.setdefault(sid, {'provider':source if source in identity.PRODUCTS else '', 'recorded_provider':source,
            'provider_label':identity.source_product(source)['label'],'native_session_id':native_id,
            'identity_kind':'native_session' if source in identity.PRODUCTS and (native_id or logical_key) else 'transcript',
            'transcripts':[], 'native_context':[], 'native_reports':[], 'incidents':[], 'project_keys':set()})
        if native_id and not entry['native_session_id']: entry['native_session_id']=native_id
        return entry
    transcripts = {r['file_path']:{**r,'error':redact_text(r['error'])} for r in sessions}
    for row in transcripts.values():
        entry = group(row['source'],row['session_id'],row['file_path'])
        entry['transcripts'].append(row);entry['project_keys'].add(row['project_key'])
    for source in incident_sources.values():
        transcript = transcripts.get(source['session_file'])
        provider = transcript['source'] if transcript else ''
        entry = group(provider,source['session_id'],source['session_file'])
        entry['incidents'].append(source);entry['project_keys'].add(source['project_key'])
        if transcript: entry['transcripts'].append(transcript)
    context_coverage = {'available':_has(store,session_context.MIGRATION),'records':0,
        'observations':0,'batches':0,'empty_batches':0,'missing_batches':0,'missing_observation_ids':[]}
    context_coverage['reason'] = '' if context_coverage['available'] else 'schema_unavailable'
    if context_coverage['available']:
        session_context._schema(store)
        manifests = {}
        for observed in store.query('SELECT * FROM scan_observations ORDER BY observed_at,id'):
            manifest = scan._load_manifest(store,observed['manifest_id'],'session scan '+observed['id'],manifests)
            if observed['compatibility_key'] != manifest['compatibility_key']: raise _corrupt('session scan '+observed['id'],'manifest mismatch')
            context = session_context._read_batch(store,observed)
            context_coverage['observations'] += 1
            if context is None:
                context_coverage['missing_batches'] += 1
                context_coverage['missing_observation_ids'].append(observed['id'])
            else:
                context_coverage['batches'] += 1
                context_coverage['empty_batches'] += not context
            for record in context or []:
                native_id = record['thread_id'] if record['kind']=='creation' else ''
                entry = group(record['source'],native_id,observed['session_file'],record['logical_session_key'])
                entry['native_context'].append(record);entry['project_keys'].add(record['project_key'])
                if observed['session_file'] in transcripts: entry['transcripts'].append(transcripts[observed['session_file']])
                context_coverage['records'] += 1
    context_coverage['missing_observations_revision'] = digest(context_coverage['missing_observation_ids'])
    context_coverage['missing_observations_omitted'] = max(0,len(context_coverage['missing_observation_ids'])-20)
    context_coverage['missing_observation_ids'] = context_coverage['missing_observation_ids'][:20]
    if context_coverage['missing_batches']: context_coverage['reason'] = 'missing_context_batches'
    native_coverage = {'available':_has(store,native_loads.MIGRATION),'records':0}
    if native_coverage['available']:
        native_loads._schema(store)
        for row in store.query('SELECT * FROM native_load_reports ORDER BY received_at,id'):
            report = native_loads._read(row)
            entry = group(report['source'],report['payload']['session_id'],logical_key=report['logical_session_key'])
            entry['native_reports'].append(report);entry['project_keys'].add(report['project_key'])
            native_coverage['records'] += 1
    for sid,entry in sorted(session_groups.items()):
        entry = dict(entry)
        entry['project_keys'] = sorted(entry['project_keys']-{''})
        entry['transcripts'] = list({r['file_path']:r for r in entry['transcripts']}.values())
        entry['transcript_scope'] = 'Physical file metadata can be shared by several native sessions; incident membership uses its own native identity.'
        entry['incidents'] = list({r['id']:r for r in entry['incidents']}.values())
        entry['retention'] = 'Complete associated retained records; original raw transcript text may never have been retained. Native reports are not verified instruction loading.'
        ids = [i['id'] for i in entry['incidents']]
        lids = {lid for iid in ids for lid in by_incident[iid]}
        searchable = _search_source({**entry,'incidents':[]})
        searchable['incidents'] = [incident_search[iid] for iid in ids]
        rows.append(_record('session',sid,entry['provider_label']+' session '+(entry['native_session_id'] or 'with unknown native ID'),entry,entry['project_keys'],lids,ids,source_text=_text(searchable)))

    current = instruction_text.current_archives(store)
    current_by_copy = {(r['project_key'],r['working_copy_id']):r for r in current['records']}
    if _has(store,instruction_text.MIGRATION):
        instruction_text.require_schema(store)
        for row in store.query('SELECT * FROM instruction_text_archives ORDER BY observed_at,id'):
            archive = instruction_text._read(store,row,revision_map)
            latest = current_by_copy.get((archive['project_key'],archive['working_copy_id']))
            is_latest = bool(latest and latest['archive'] and latest['archive']['id']==archive['id'])
            for file in archive['files']:
                sid = digest([archive['id'],file['file_key']])
                source = {**{k:v for k,v in archive.items() if k!='files'},'file':file,
                          'latest_selected_observation':is_latest,'latest_inventory_status':latest['status'] if latest else 'unknown',
                          'meaning':'Observed redacted instruction text at this copy and time; runtime loading and current file content remain unverified.'}
                rows.append(_record('instruction',sid,file['path'],source,[archive['project_key']]))
    for revision in revisions:
        source = deepcopy(revision);source['content']=redact_text(source['content'])
        source['application_end'] = rule_revisions.application_end(store,revision)
        source['meaning'] = 'Retained delivered revision; delivery alone does not establish working-copy availability or session loading.'
        rows.append(_record('revision',revision['id'],'Delivered rule '+revision['learning_id'],source,[revision['project_key']],[revision['learning_id']],by_learning[revision['learning_id']]))
    rows.sort(key=lambda row:(KINDS.index(row['kind']),row['source_id']))
    coverage = {'instruction_text':{k:v for k,v in current.items() if k!='records'},
                'instruction_copies':[ {k:v for k,v in row.items() if k not in {'archive','inventory_ids','archive_ids','missing_inventory_ids'}} for row in current['records'][:20]],
                'instruction_copy_count':len(current['records']), 'instruction_copies_omitted':max(0,len(current['records'])-20),
                'instruction_copy_states':dict(Counter(row['status'] for row in current['records'])),
                'session_context':context_coverage, 'native_reports':native_coverage,
                'revision_archive':{'available':_has(store,rule_revisions.MIGRATION),'records':len(revisions)},
                'note':'All retained native sources, including rejected and dismissed records. Source kinds can refer to the same event; result counts are not independent evidence counts. No original file is opened.'}
    # Fingerprint complete retained inputs before deterministic identity enrichment.
    # Every indexed session is present in the session sources, including label aliases.
    revision=digest({'rows':[_revision_record(row) for row in rows],
                     'coverage':coverage,'identity_profile':'retained-attribution/1',
                     'inventory_selection':[{k:v for k,v in r.items() if k!='archive'} for r in current['records']]})
    for row in rows:
        source=row['source']
        if row['kind']=='incident':
            attribution=identity.identity(identities,source)
        elif row['kind']=='session':
            attribution={'projects':[identity.project_ref(identities,key) for key in source['project_keys']],
                'session':{'key':row['source_id'],**{k:source[k] for k in ('provider','provider_label','recorded_provider','native_session_id','identity_kind')},
                           'reason':'' if source['identity_kind']=='native_session' else 'Native identity is incomplete; this is a retained transcript reference.'}}
        else:
            continue
        source['identity']=row['identity']=attribution
        row['_text']+='\n'+unicodedata.normalize('NFC',_text(identity.search_values(attribution)))
    return {'rows':rows, 'coverage':coverage,'learnings':learnings,'proposals':proposals,'incidents':incidents,
            'by_learning':by_learning,'by_incident':by_incident,'revisions':revisions,
            'revision':revision}


def _options(query, kinds, project_key):
    if not isinstance(query,str) or len(query)>1000: raise EvidenceError('Search must contain at most 1000 characters.')
    if not isinstance(kinds,(list,tuple)) or any(not isinstance(k,str) or k not in KINDS for k in kinds):
        raise EvidenceError('Unknown evidence source kind.')
    if project_key is not None and (not isinstance(project_key,str) or not project_key):
        raise EvidenceError('A project filter requires a nonempty canonical project key.')
    return {'query':unicodedata.normalize('NFC',query).strip(),'kinds':sorted(set(kinds)),'project_key':project_key}


def _page(rows, *, limit, cursor, revision, selector):
    if type(limit) is not int or not 1<=limit<=50: raise EvidenceError('Evidence page size must be 1–50.')
    offset = 0
    if cursor is not None:
        try:
            if not isinstance(cursor,str) or not cursor or len(cursor)>2500: raise ValueError('invalid cursor size')
            token = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
            if not isinstance(token,dict) or set(token)!={'profile','revision','selector','after'} or token['profile']!=PROFILE or token['selector']!=selector:
                raise ValueError('different evidence selection')
            if token['revision']!=revision:
                raise EvidenceError('Retained evidence changed. Refresh the results.',code='EvidenceChanged',status=409)
            offset = [r['key'] for r in rows].index(token['after'])+1
        except EvidenceError: raise
        except (ValueError,TypeError,UnicodeError,KeyError) as exc:
            raise EvidenceError('Invalid evidence cursor: '+str(exc)) from exc
    page = rows[offset:offset+limit]
    token = base64.urlsafe_b64encode(encoded({'profile':PROFILE,'revision':revision,'selector':selector,'after':page[-1]['key']}).encode()).decode() if offset+len(page)<len(rows) else None
    return page, {'offset':offset,'limit':limit,'count':len(rows),'next_cursor':token}


def _default_excerpt(row):
    source = row['source']
    return {'learning':source.get('rule_text'), 'proposal':source.get('diff_unified'),
            'incident':source.get('matched_text') or _text(source.get('window',[])),
            'session':source.get('native_session_id'), 'instruction':source.get('file',{}).get('text'),
            'revision':source.get('content')}.get(row['kind']) or row['title']


def _object_bytes(value):
    """Conservative retained size: count shared objects at each reference."""
    size = sys.getsizeof(value)
    if isinstance(value, dict):
        return size + sum(_object_bytes(k) + _object_bytes(v) for k,v in value.items())
    if isinstance(value, (list, tuple)):
        return size + sum(_object_bytes(v) for v in value)
    return size


def _search_projection(catalog, max_bytes):
    rows = []
    projected = {'rows':rows, 'coverage':catalog['coverage'], 'revision':catalog['revision']}
    size = _object_bytes(projected) - sys.getsizeof(rows)
    if size + sys.getsizeof(rows) > max_bytes:
        return catalog, 0
    fields = ('kind','source_id','key','title','project_keys','learning_ids','incident_ids','_text')
    for row in catalog['rows']:
        member = {key:row[key] for key in fields}
        # Retain complete metadata as one immutable value. Decode only page members.
        member['_identity_json'] = encoded(row['identity']) if row['identity'] is not None else None
        member['_default_text'] = _default_excerpt(row)
        rows.append(member)
        size += _object_bytes(member)
        if size + sys.getsizeof(rows) > max_bytes:
            # Complete uncached results, never a truncated search corpus.
            return catalog, 0
    return projected, size + sys.getsizeof(rows)


class EvidenceSearchCache:
    """One bounded projection owned by one dashboard reader, never authorization."""
    def __init__(self, store, *, max_bytes=512*1024*1024):
        if type(max_bytes) is not int or max_bytes < 0:
            raise ValueError('Search cache capacity must be a nonnegative byte count.')
        self._owner, self._connection = store, store.conn
        self.max_bytes = max_bytes
        self.clear()

    def clear(self):
        self._value, self._token, self.retained_bytes = None, None, 0

    def _version(self, store):
        return (store.query_one('PRAGMA main.data_version')['data_version'], store.conn.total_changes)

    def read(self, store):
        if store is not self._owner or store.conn is not self._connection:
            self.clear()
            raise _corrupt('search cache', 'reader Store or connection changed')
        if not store.read_only or not store.conn.in_transaction:
            self.clear()
            return _catalog(store)
        # Establish the caller's actual read snapshot before its local version.
        store.query_one('SELECT name FROM main.sqlite_schema LIMIT 1')
        token = self._version(store)
        if self._value is not None and token == self._token:
            return self._value
        self.clear()
        value, size = _search_projection(_catalog(store), self.max_bytes)
        if size and store.conn.in_transaction and self._version(store) == token:
            self._value, self._token, self.retained_bytes = value, token, size
        return value


def _summary(row, tokens):
    text = row['_text'] if tokens else (row['_default_text'] if '_default_text' in row else _default_excerpt(row))
    folded = _fold(text)
    position = next((folded.index(t) for t in tokens if t in folded),0)
    if len(folded)!=len(text):
        width = 0
        for index,char in enumerate(text):
            if width>=position: position=index;break
            width += len(char.casefold())
    start = max(0,position-80); excerpt = text[start:start+350]
    attribution = json.loads(row['_identity_json']) if row.get('_identity_json') is not None else deepcopy(row.get('identity'))
    return {k:row[k] for k in ('kind','source_id')} | {'identity':attribution,
        **{k:row[k][:3] for k in ('project_keys','learning_ids','incident_ids')},
        **{k.removesuffix('_ids').removesuffix('_keys')+'_count':len(row[k]) for k in ('project_keys','learning_ids','incident_ids')},
        'related_ids_cut':{k:max(0,len(row[k])-3) for k in ('project_keys','learning_ids','incident_ids')},
        'title':redact_text(row['title'])[:180], 'title_cut':max(0,len(row['title'])-180),
        'excerpt':excerpt, 'excerpt_start':start,'excerpt_cut':len(text)-len(excerpt),
        'href':'#/rules/evidence/'+row['kind']+'/'+quote(row['source_id'],safe='')}


@_data_errors
def search(store, cfg, *, query, kinds=(), project_key=None, limit=20, cursor=None, cache=None):
    options = _options(query,kinds,project_key)
    catalog = cache.read(store) if cache is not None else _catalog(store)
    tokens = _fold(options['query']).split()
    scoped = [r for r in catalog['rows'] if (not kinds or r['kind'] in kinds) and (project_key is None or project_key in r['project_keys'])]
    matches = [r for r in scoped if all(token in _fold(r['_text']) for token in tokens)]
    page,pagination = _page(matches,limit=limit,cursor=cursor,revision=catalog['revision'],selector=digest(options))
    return {'profile':PROFILE,'rows':[_summary(row,tokens) for row in page],'pagination':pagination,
            'source_counts':{k:sum(r['kind']==k for r in matches) for k in KINDS},
            'corpus_counts':{k:sum(r['kind']==k for r in scoped) for k in KINDS},'coverage':deepcopy(catalog['coverage']),
            'revision':catalog['revision'],'options':options}


@_data_errors
def learning_evidence(store, *, learning_id, limit=20, cursor=None):
    if not store.query_one('SELECT id FROM learnings WHERE id=?',(learning_id,)):
        raise EvidenceError('No learning has this ID.',code='EvidenceNotFound',status=404)
    sources = store.query('SELECT i.* FROM incidents i JOIN incident_learnings il ON il.incident_id=i.id WHERE il.learning_id=? ORDER BY i.ts,i.id',(learning_id,))
    identities=identity.metadata_index(store)
    rows=[]
    for r in sources:
        source={**_incident(r),'identity':identity.identity(identities,r)}
        rows.append(_record('incident',r['id'],source['matched_text'] or 'Retained '+r['signal_type']+' incident',source,[r['project_key']],[learning_id],[r['id']]))
    revision = digest([sources,[r['identity'] for r in rows]])
    page,pagination = _page(rows,limit=limit,cursor=cursor,revision=revision,selector=digest(['learning-evidence',learning_id]))
    return {'learning_id':learning_id,'rows':[_summary(r,[]) for r in page], 'pagination':pagination,'revision':revision,
            'project_count':len({r['project_key'] for r in sources if r['project_key']}),
            'unknown_project_incidents':sum(not r['project_key'] for r in sources)}


def _destination(kind, source_id, *, mode=None):
    result = {'kind':kind,'source_id':source_id}
    escaped = quote(source_id,safe='')
    if kind=='mine_incident': result['href']='#/review/mine/'+escaped
    elif kind=='review_proposal': result['href']='#/review/proposal/'+escaped
    elif kind in {'command','operation'}: result['href']='#/review/'+kind+'/'+escaped
    elif kind=='project_availability': result['href']='#/projects/'+escaped+'?tab=availability'
    elif kind=='learning': result['href']='#/rules/'+escaped
    elif kind=='recovery':
        selection={'learning_id':source_id,'mode':mode,'target_id':'','proposal_ids':[]}
        result.update(mode=mode,href='#/review/recovery/'+quote(encoded(selection),safe=''))
    return result


def _incident_copy_binding(store, incident):
    """Use retained exact links; disagreeing historical revisions stay unknown.

    A link archives an occurrence ID, not its projection revision ID. Therefore
    a changed identity across its retained revisions cannot select a copy safely.
    Legacy incidents without links can still match an exact recorded path.
    """
    if not _has(store,scan.LINK_MIGRATION): return None
    history = scan.scan_history(store,incident_id=incident['id'],limit=100)
    records = list(history['records'])
    while history['next_cursor']:
        history = scan.scan_history(store,incident_id=incident['id'],limit=100,cursor=history['next_cursor'])
        records.extend(history['records'])
    if not records: return None
    bindings, missing = set(), False
    cache = {}
    for observed in records:
        owner = 'incident '+incident['id']+' scan '+observed['id']
        for oid in observed['occurrence_ids']:
            membership = store.query_one('SELECT 1 FROM scan_observation_occurrences WHERE observation_id=? AND occurrence_id=?',(observed['id'],oid))
            link = store.query_one('SELECT * FROM scan_incident_links WHERE observation_id=? AND occurrence_id=? AND incident_id=?',(observed['id'],oid,incident['id']))
            if not membership or link['id']!=scan.content_id([oid,incident['id'],observed['id']]):
                raise _corrupt(owner,'invalid occurrence membership')
            rows = store.query('SELECT * FROM scan_occurrences WHERE occurrence_id=? ORDER BY id',(oid,))
            if not rows: missing = True
            for row in rows:
                row_owner = owner+' occurrence '+row['id']
                origin = store.query_one('SELECT * FROM scan_observations WHERE id=?',(row['observation_id'],))
                if origin is None: raise _corrupt(row_owner,'missing projection observation')
                manifest = scan._load_manifest(store,origin['manifest_id'],row_owner,cache)
                identity = {k:row[k] for k in scan._OCCURRENCE_FIELDS if k not in {'incident_links','supporting_line_keys','evidence'}}
                identity['supporting_line_keys'] = _json(row['supporting_lines_json'],row_owner+'.support',list)
                identity['evidence'] = _json(row['evidence_json'],row_owner+'.evidence',dict)
                attributes = {k:row[k] for k in ('source','project_key','working_copy_id','logical_session_key','headless','is_subagent','exclusion')}
                expected = scan.occurrence_identity(transcript_id=row['transcript_id'],
                    **{k:identity[k] for k in ('compatibility_key','kind','signal_type','supporting_line_keys','trigger_line_key','discriminator')})
                revision = scan.content_id(['scan-occurrence-revision/1',identity,attributes])
                if (row['occurrence_id']!=expected or row['revision_hash']!=revision
                        or row['id']!=scan.content_id([revision,row['observation_id']])
                        or row['transcript_id']!=observed['transcript_id'] or row['transcript_id']!=origin['transcript_id']
                        or row['compatibility_key']!=observed['compatibility_key']
                        or row['compatibility_key']!=origin['compatibility_key'] or row['compatibility_key']!=manifest['compatibility_key']
                        or row['source']!=observed['source'] or row['signal_type']!=incident['signal_type']):
                    raise _corrupt(row_owner,'changed occurrence identity or projection binding')
                if not row['working_copy_id']:
                    missing = True
                    continue
                copy = store.query_one('SELECT * FROM scan_working_copies WHERE id=?',(row['working_copy_id'],))
                if (copy is None or copy['project_key']!=row['project_key']
                        or copy['id']!=scan.content_id([copy['project_key'],copy['normalized_path']])
                        or copy['normalization'] not in {'realpath','lexical_missing'}):
                    raise _corrupt(row_owner,'invalid retained working copy')
                bindings.add((row['project_key'],row['working_copy_id']))
    return {'status':'observed' if not missing and len(bindings)==1 else 'unknown',
            'pairs':bindings, 'observation_ids':[r['id'] for r in records]}


def _diagnosis(store, cfg, catalog, selected):
    from .. import commands, execution_policy, operations, rule_availability
    facts, limitations = [], []
    incident_ids = selected['incident_ids']
    learning_ids = selected['learning_ids']
    if not incident_ids: limitations.append('No incident is explicitly linked to this source; text similarity is not an evidence link.')
    for iid in incident_ids:
        incident = catalog['incidents'][iid]
        if catalog['by_incident'][iid]: continue
        jobs = []
        if _has(store,'0019_incident_jobs'):
            for row in store.query('SELECT command_id FROM incident_jobs WHERE incident_id=? ORDER BY command_id',(iid,)):
                job = commands.command_status(store,row['command_id'])
                jobs.append({k:job[k] for k in ('id','state','action')})
        facts.append({'code':'no_linked_learning','certainty':'recorded','incident_id':iid,
            'title':'No linked learning retained','incident_status':incident['status'],'jobs':jobs,
            'detail':'This incident has no retained learning link. '+('It was deliberately dismissed.' if incident['status']=='dismissed' else 'Its mining status is '+incident['status']+'.'),
            'destinations':([_destination('mine_incident',iid)] if incident['status']=='new' else [])+[_destination('command',job['id']) for job in jobs]})

    known_learnings = {lid:catalog['learnings'][lid] for lid in learning_ids if lid in catalog['learnings']}
    related_proposals = [p for p in catalog['proposals'].values() if p['learning_id'] in known_learnings]
    if selected['kind']=='proposal': related_proposals=[catalog['proposals'][selected['source_id']]]
    dispositions = {p['id']:p for p in execution_policy.proposal_dispositions(store,cfg,retained_only=True)} if related_proposals else {}
    for lid,learning in known_learnings.items():
        if learning['violated_existing_rule']:
            facts.append({'code':'reported_rule_violation','certainty':'reported','learning_id':lid,
                'title':'An existing-rule violation was reported','report':redact_text(learning['violated_existing_rule']),
                'incident_ids':catalog['by_learning'][lid],
                'detail':'This is the retained mining report. It does not prove instruction receipt or causal effect.',
                'destinations':[_destination('recovery',lid,mode='hook')]})
        else: limitations.append('Learning '+lid+' has no retained existing-rule violation report; enforcement remains unknown.')
    command_cache = {}
    operations_by_proposal = defaultdict(list)
    if related_proposals and _has(store,operations.OPERATION_MIGRATION):
        for row in store.query('SELECT id FROM instruction_operations ORDER BY created_at,id'):
            operation = operations.operation_status(store,row['id'])
            members = {operation['proposal_id']}
            if operation['kind']=='rollback':
                members.update(m['proposal_id'] for m in operation['record']['rollback']['source']['affected_members'])
            for pid in members: operations_by_proposal[pid].append(operation)
    for proposal in related_proposals:
        pid = proposal['id']
        if pid in dispositions:
            disposition = dispositions[pid]
            step = disposition['next_step']
            facts.append({'code':'proposal_disposition','certainty':'unknown' if step=='unknown' else 'recorded',
                'proposal_id':pid,'title':{'review':'Awaiting human review','automatic_delivery':'Eligible for automatic delivery',
                    'suppressed':'Suppressed by rejection','unknown':'Current review status needs target inspection'}[step],
                'next_step':step,'execution':disposition['execution'],
                'detail':disposition['execution']['detail'],
                'destinations':[_destination('review_proposal',pid)]})
        memberships = store.query('SELECT command_id,target_id FROM command_members WHERE proposal_id=? ORDER BY command_id,target_id',(pid,)) if _has(store,'0010_dashboard_commands') else []
        for member in memberships:
            cid=member['command_id']
            if cid not in command_cache: command_cache[cid]=commands.command_status(store,cid)
            command=command_cache[cid]
            target=next(t for t in command['targets'] if t['id']==member['target_id'])
            facts.append({'code':'command_delivery','certainty':'recorded','proposal_id':pid,'command_id':cid,
                'target_id':target['id'],'command_state':command['state'],'target_state':target['state'],
                'title':'Approval command target: '+target['state'],
                'detail':'Approval and delivery are distinct. This is the recorded state of this exact target and member.',
                'destination':target['destination'],'destinations':[_destination('command',cid)]})
        ops = operations_by_proposal[pid]
        for operation in ops:
            facts.append({'code':'instruction_operation','certainty':'recorded','proposal_id':pid,
                'operation_id':operation['id'],'operation_kind':operation['kind'],'state':operation['state'],
                'title':'Instruction operation: '+operation['state'], 'detail':operation['error_detail'] or 'Recorded execution state, not inferred from approval.',
                'destinations':[_destination('operation',operation['id'])]})
        if proposal['status']=='approved_user' and not memberships and not ops:
            facts.append({'code':'approval_without_execution_record','certainty':'unknown','proposal_id':pid,
                'title':'Approval is recorded; delivery evidence is missing',
                'detail':'No command member or instruction operation is retained for this approval. Do not treat it as a completed write.',
                'destinations':[_destination('review_proposal',pid)]})

    # Associate only the retained scan identity, or exact legacy path. Never canonicalize
    # a path against today's filesystem or borrow an unrelated copy's observation.
    relevant = [r for r in catalog['revisions'] if r['learning_id'] in learning_ids]
    if selected['kind']=='revision': relevant=[r for r in relevant if r['id']==selected['source_id']]
    incident_context = [catalog['incidents'][iid] for iid in incident_ids]
    copy_bindings = {i['id']:_incident_copy_binding(store,i) for i in incident_context} if relevant else {}
    observed_pairs = 0
    for revision in relevant:
        rows=store.query('SELECT * FROM rule_availability_observations WHERE rule_revision_id=? ORDER BY observed_at,id',(revision['id'],))
        grouped=defaultdict(list)
        for row in rows:
            record=rule_availability._validate_observation(rule_revisions.read_record(row,'rule_availability_observations'))
            rule_availability._bind_revision(record,revision)
            grouped[(record['project_key'],record['working_copy_id'])].append(record)
        for (project_key,copy_id),records in grouped.items():
            last_at=records[-1]['observed_at'];same=[r for r in records if r['observed_at']==last_at]
            related_incidents=[]
            for incident in incident_context:
                binding=copy_bindings[incident['id']]
                if binding is not None:
                    matches=binding['status']=='observed' and (project_key,copy_id) in binding['pairs']
                else:
                    matches=incident['project_key']==project_key and incident['project_path']==same[0]['working_copy']['normalized_path']
                if matches: related_incidents.append(incident)
            if incident_context and not related_incidents:continue
            observed_pairs+=1
            conflicting=len({rule_availability._check_signature(r) for r in same})>1
            record=same[-1];status='unknown' if conflicting else record['status']
            relations=[]
            for incident in related_incidents:
                at=scan.normalize_timestamp(incident['ts'])
                relations.append({'incident_id':incident['id'],'incident_at':at,
                    'relation':'unknown_time' if at is None else 'same_timestamp' if at==last_at else 'before_incident' if last_at<at else 'after_incident'})
            facts.append({'code':'observed_copy_availability','certainty':'unknown' if status=='unknown' else 'observed',
                'learning_id':revision['learning_id'],'rule_revision_id':revision['id'],'project_key':project_key,
                'working_copy_id':copy_id,'working_copy':record['working_copy'],'observed_at':last_at,
                'observation_ids':[r['id'] for r in same],'status':status,
                'cause':'conflicting_simultaneous_observations' if conflicting else record['cause'],
                'title':'Recorded copy availability: '+status,
                'scope':[json.loads(value) for value in rule_availability._loading_scopes(record)] if not conflicting else [],
                'incident_time_relations':relations,'destination':revision['destination'],
                'detail':'The actual observer checked this copy at the stated time. This does not establish availability or instruction loading at a different incident time.',
                'destinations':[_destination('project_availability',project_key)]+([_destination('recovery',revision['learning_id'],mode='correct_target')] if revision['learning_id'] in known_learnings and status in {'absent','changed'} else [])})
    if relevant and not observed_pairs:
        limitations.append('No retained availability observation matches this source’s retained project and working-copy identity. Missing or conflicting scan bindings remain unknown. Availability is unknown; no current path resolution or cross-copy substitution was made.')
    if not relevant:limitations.append('No delivered revision is explicitly linked to this source; proposed text does not prove delivery or availability.')
    return {'facts':facts,'limitations':limitations,'meaning':'Multiple facts may apply. Missing evidence remains unknown; these observations do not prove why an incident occurred.'}


@_data_errors
def detail(store, cfg, *, kind, source_id):
    if kind not in KINDS or not isinstance(source_id,str) or not source_id:
        raise EvidenceError('A known source kind and nonempty source ID are required.')
    catalog=_catalog(store)
    row=next((r for r in catalog['rows'] if r['kind']==kind and r['source_id']==source_id),None)
    if row is None:raise EvidenceError('No retained source has this identity.',code='EvidenceNotFound',status=404)
    from .. import mining_history
    provenance={lid:mining_history.summary(store,catalog['learnings'][lid]) for lid in row['learning_ids'] if lid in catalog['learnings']}
    diagnosis = _diagnosis(store,cfg,catalog,row)
    return {k:v for k,v in row.items() if k not in {'key','_text'}} | {
        'source_revision':catalog['revision'],'revision':digest([catalog['revision'],provenance,diagnosis]),
        'provenance':provenance,'diagnosis':diagnosis,
        'proposal_ids':sorted(p['id'] for p in catalog['proposals'].values() if p['learning_id'] in row['learning_ids']),
        'coverage':catalog['coverage']}
