"""Bounded Rules summaries over retained display membership; selected-Store only."""
from __future__ import annotations

import base64
from collections import Counter
import hashlib
import json

from .. import mining_history, rule_families
from . import queries, evidence_identity as identity

TARGETS = {'all': None, 'global': {'global_claude_md','codex_global'},
           'project': {'project_agents_md','project_claude_md','rule_file'},
           'skill': {'skill'}, 'hook': {'hook'}, 'none': set()}
SORTS = {'evidence','recent','title','state'}


class RuleBrowserError(ValueError):
    def __init__(self, detail, *, code='InvalidRuleRequest', status=400):
        super().__init__(detail)
        self.code, self.status = code, status


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _options(query, target, sort, grouping):
    if not isinstance(query, str) or len(query) > 1000:
        raise RuleBrowserError('Search must contain at most 1000 characters.')
    if target not in TARGETS or sort not in SORTS or type(grouping) is not bool:
        raise RuleBrowserError('Unknown target filter, sort, or grouping mode.')
    return {'query': query.strip(), 'target': target, 'sort': sort, 'grouping': grouping}


def _page(rows, *, limit, cursor, revision, selection):
    if type(limit) is not int or not 1 <= limit <= 50:
        raise RuleBrowserError('Rule page size must be 1–50.')
    offset = 0
    if cursor:
        try:
            if len(cursor) > 2000:
                raise ValueError('oversized cursor')
            value = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
            if not isinstance(value, dict) or set(value) != {'revision','selection','after'}:
                raise ValueError('invalid cursor shape')
            if value['selection'] != selection:
                raise ValueError('cursor belongs to another selection')
            if value['revision'] != revision:
                raise RuleBrowserError('Rules changed since this page was loaded. Refresh the results.', code='RulesChanged', status=409)
            ids = [row['key'] for row in rows]
            offset = ids.index(value['after']) + 1
        except RuleBrowserError:
            raise
        except (ValueError, TypeError, KeyError, UnicodeError) as exc:
            raise RuleBrowserError('Invalid rule page cursor: ' + str(exc)) from exc
    selected = rows[offset:offset + limit]
    more = offset + len(selected) < len(rows)
    next_cursor = base64.urlsafe_b64encode(_json({'revision':revision,'selection':selection,'after':selected[-1]['key']}).encode()).decode() if more else None
    return selected, {'next_cursor':next_cursor,'offset':offset,'count':len(rows),'limit':limit}


def _matches(store, rows, query, identities):
    if not query:
        return {row['id'] for row in rows}
    tokens = query.casefold().split()
    texts = {row['id']: ' '.join(str(row[k] or '') for k in ('id','title','rule_text','why','incident_summary','source','status','scope')).casefold() for row in rows}
    # Search complete stored proposal/linked evidence, while list payloads stay
    # bounded. The separate DP-08 index/diagnosis spans additional source kinds.
    for prop in store.query('SELECT learning_id,target_path,diff_unified FROM proposals ORDER BY id'):
        if prop['learning_id'] in texts:
            texts[prop['learning_id']] += '\n' + (prop['target_path'] + '\n' + prop['diff_unified']).casefold()
    for inc in store.query('SELECT il.learning_id,i.id,i.matched_text,i.window_json,i.session_id,i.session_file,i.signal_type,i.project_key '
                           'FROM incident_learnings il JOIN incidents i ON i.id=il.incident_id ORDER BY il.learning_id,i.id'):
        if inc['learning_id'] in texts:
            # Validate archive shape; JSON escapes are not searchable text.
            window = queries._json_arr(inc['window_json'], 'learning ' + inc['learning_id'] + ' incident ' + inc['id'] + ' window_json')
            attribution=identity.identity(identities,inc)
            texts[inc['learning_id']] += '\n' + _json(identity.search_values(attribution)).casefold()
            texts[inc['learning_id']] += '\n' + (str(inc['matched_text']) + '\n' + _json(window) + '\n' + inc['session_id'] + '\n' + inc['signal_type'] + '\n' + inc['project_key']).casefold()
    return {lid for lid, text in texts.items() if all(token in text for token in tokens)}


def _catalog(store, cfg, options):
    families = rule_families.current_families(store, cfg)
    rows = store.query('SELECT id,title,rule_text,why,category,scope,path_globs_json,violated_existing_rule,incident_summary,status,source,evidence_count,created_at,last_seen FROM learnings ORDER BY id')
    props = store.query('SELECT id,learning_id,target_path,target_kind,status,action,eval_result_id,created_at FROM proposals ORDER BY id')
    identities=identity.metadata_index(store)
    links = store.query('SELECT il.learning_id,i.id,i.project_key,i.ts,i.session_id,i.session_file FROM incident_learnings il JOIN incidents i ON i.id=il.incident_id ORDER BY il.learning_id,i.id')
    for row in links:row['identity']=identity.identity(identities,row)
    by_prop, by_link = {}, {}
    for prop in props:
        by_prop.setdefault(prop['learning_id'], []).append(prop)
    for link in links:
        by_link.setdefault(link['learning_id'], []).append(link)
    found = _matches(store, rows, options['query'],identities)
    matched, raw = {}, {row['id']:row for row in rows}
    for row in rows:
        lid = row['id']
        proposals = by_prop.get(lid, [])
        selected = proposals if options['target'] == 'all' else [p for p in proposals if p['target_kind'] in TARGETS[options['target']]]
        if lid not in found or (options['target'] == 'none' and proposals) or (options['target'] not in {'all','none'} and not selected):
            continue
        evidence = by_link.get(lid, [])
        matched[lid] = {'learning':row,'proposals':selected,'evidence':evidence}
    groups = []
    for family in families['groups']:
        ids = [lid for lid in family['learning_ids'] if lid in matched]
        if ids:
            groups.append({**family,'matching_ids':ids})
    # Any presentation/search source change invalidates pagination. Bodies used
    # only by search affect its matched IDs; unrelated body changes need not do so.
    mining_revision = store.query_one('SELECT COUNT(*) n,MAX(created_at) latest,MAX(id) last_id FROM mining_history')
    revision = _hash({'options':options,'families':families['groups'],'family_snapshot':families['snapshot_id'],
                      'rows':rows,'proposals':props,'links':links,'matches':sorted(matched),'mining':mining_revision})
    return {'families':families,'groups':groups,'matched':matched,'raw':raw,'revision':revision,
            'counts':{'members':len(matched),'families':len(groups),
                      'proposals':sum(len(item['proposals']) for item in matched.values()),
                      'target_paths':len({(p['target_kind'],p['target_path']) for item in matched.values() for p in item['proposals']}),
                      'all_members':len(rows)}}


def _sort(item, sort):
    if sort == 'evidence':
        return (-len(item['evidence_ids']), item['key'])
    if sort == 'recent':
        return (item['latest'], item['key'])
    if sort == 'title':
        return (item['title'].casefold(), item['key'])
    return (item['state'], item['key'])


def _entry(catalog, ids, *, family=None):
    items = [catalog['matched'][lid] for lid in ids]
    first = items[0]['learning']
    states = sorted({p['status'] for item in items for p in item['proposals']} or {item['learning']['status'] for item in items})
    return {'key':family['family_id'] if family else first['id'],'ids':ids,'family':family,
            'title':first['title'] or first['rule_text'], 'state':states[0] if len(states)==1 else 'mixed',
            'latest':max((item['learning']['last_seen'] or item['learning']['created_at']) for item in items),
            'evidence_ids':sorted({e['id'] for item in items for e in item['evidence']})}


def _summary(store, item):
    row, proposals, evidence = item['learning'], item['proposals'], item['evidence']
    targets = sorted({(p['target_kind'],p['target_path']) for p in proposals})
    return {'id':row['id'],'title':row['title'][:180],'title_cut':max(0,len(row['title'])-180),'rule_text':row['rule_text'][:350],
            'preview_cut':max(0,len(row['rule_text'])-350),'status':row['status'],
            'proposal_statuses':dict(sorted(Counter(p['status'] for p in proposals).items())),
            'targets':[{'kind':kind,'path':path} for kind,path in targets[:3]],'target_paths':len(targets),
            'proposal_count':len(proposals),'evidence_linked':len({e['id'] for e in evidence}),
            'project_count':len({e['project_key'] for e in evidence if e['project_key']}),
            'unknown_project_incidents':sum(not e['project_key'] for e in evidence),
            'agent_products':sorted({e['identity']['session']['provider'] for e in evidence}-{''}),
            'unknown_source_incidents':sum(not e['identity']['session']['provider'] for e in evidence),
            'miner_generation':mining_history.summary(store,row),
            'enforcement_gap':bool(row['violated_existing_rule'])}


def _public_entry(store, catalog, entry):
    family = entry['family']
    first = _summary(store, catalog['matched'][entry['ids'][0]])
    if family is None or family['size'] == 1:
        return {'kind':'rule','key':entry['key'],'rule':first}
    items = [catalog['matched'][lid] for lid in entry['ids']]
    return {'kind':'family','key':entry['key'],'family_id':family['family_id'],'representative':first,
            'total_members':family['size'],'matched_members':len(entry['ids']),
            'evidence_linked':len(entry['evidence_ids']),
            'target_paths':len({(p['target_kind'],p['target_path']) for item in items for p in item['proposals']}),
            'proposal_count':sum(len(item['proposals']) for item in items),
            'state':entry['state'],'basis':family['basis'],'minimum_inferred_cosine':family['minimum_inferred_cosine']}


def browse(store, cfg, *, query='', target='all', sort='evidence', grouping=True, limit=20, cursor=None):
    options = _options(query,target,sort,grouping)
    catalog = _catalog(store,cfg,options)
    entries = [_entry(catalog,g['matching_ids'],family=g) for g in catalog['groups']] if grouping else [_entry(catalog,[lid]) for lid in catalog['matched']]
    entries.sort(key=lambda item:_sort(item,sort), reverse=sort=='recent')
    page, pagination = _page(entries,limit=limit,cursor=cursor,revision=catalog['revision'],selection=_hash(options))
    coverage = {k:v for k,v in catalog['families'].items() if k not in {'groups','sources','vector_hashes','config','model'}}
    for key in ('missing_vector_ids','empty_text_ids','unresolved_duplicate_ids'):
        coverage[key.replace('_ids','_count')] = len(coverage.get(key, []))
        coverage.pop(key, None)
    model = catalog['families']['model']
    coverage['model'] = {'cache_key':model['cache_key'], 'source':model['provenance'].get('source')} if model else None
    return {'mode':'paged','rows':[_public_entry(store,catalog,row) for row in page],
            'counts':catalog['counts'],'pagination':pagination,'revision':catalog['revision'],
            'options':options,'coverage':coverage,
            'search_coverage':'Rules, proposals and linked retained incidents/sessions. Unmined incidents and other source kinds are not included here.'}


def members(store, cfg, family_id, *, query='',target='all',sort='evidence',grouping=True,limit=20,cursor=None):
    options = _options(query,target,sort,grouping)
    catalog = _catalog(store,cfg,options)
    family = next((g for g in catalog['groups'] if g['family_id']==family_id),None)
    if family is None:
        raise RuleBrowserError('This family has no matching current members. Refresh the results.',code='RuleFamilyNotFound',status=404)
    entries = [_entry(catalog,[lid]) for lid in family['matching_ids']]
    entries.sort(key=lambda item:_sort(item,sort),reverse=sort=='recent')
    page,pagination = _page(entries,limit=limit,cursor=cursor,revision=catalog['revision'],selection=_hash({**options,'family_id':family_id}))
    return {'family_id':family_id,'rows':[_summary(store,catalog['matched'][entry['ids'][0]]) for entry in page],
            'total_members':family['size'],'matched_members':len(entries),'pagination':pagination,'revision':catalog['revision']}


def detail(store, learning_id):
    result = queries.rules(store, learning_ids=[learning_id])
    if not result['rows']:
        raise RuleBrowserError('No learning has this ID.',code='RuleNotFound',status=404)
    return result['rows'][0]
