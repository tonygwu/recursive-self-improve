"""Content-free ownership inventories from the actual instruction-surface collector.

Human-managed means unmarked text protected from automatic deletion. It does not
assert human authorship. A machine label requires an exact retained delivery.
"""
from __future__ import annotations

import base64
from collections import Counter, defaultdict
import json
from pathlib import PurePath

from .mining_history import digest, encoded
from .rule_revisions import (AvailabilityError, marked_units, read_record, retained_revisions,
                             timestamp, validate_revision)
from .scan_observations import working_copy_identity
from .instruction_context import summarize

MIGRATION = '0028_instruction_inventory'
TABLE = 'instruction_inventories'
OWNERS = ('human_managed', 'machine', 'edited', 'unknown')


class InventoryError(AvailabilityError):
    """Inventory evidence cannot support its reported ownership."""


def require_schema(store):
    if not store.query_one('SELECT name FROM schema_migrations WHERE name=?', (MIGRATION,)):
        raise InventoryError('UpgradeRequired: '+MIGRATION)
    try:
        store.query(f'SELECT id FROM {TABLE} LIMIT 0')
    except Exception as exc:
        raise InventoryError(TABLE+': applied inventory schema is damaged') from exc


def _counts():
    return {owner: {'lines': 0, 'bytes': 0} for owner in OWNERS}


def _file_inventory(item, revisions, working_copy_path):
    text = item['_text']
    lines = text.splitlines(keepends=True)
    labels = ['human_managed'] * len(lines)
    by_learning = defaultdict(list)
    for revision in revisions:
        by_learning[revision['learning_id']].append(revision)
    units = []
    markers = item['marked_units']
    duplicates = Counter(u['learning_id'] for u in markers)
    whole = [r for r in revisions if r['unit_kind'] == 'file' and r['content'] == text]
    def same_destination(revision):
        destination = revision['destination']
        paths = set(item['aliases']) | {item['real_path']}
        expected = str(PurePath(working_copy_path)/destination['relative_path']) if destination['repo_root'] else destination['target_path']
        return expected in paths
    changed_file = [r for r in revisions if r['unit_kind'] == 'file' and same_destination(r)]
    if whole:
        labels = ['machine'] * len(lines)
        units.append({'start_line': 1, 'end_line': len(lines), 'ownership': 'machine',
                      'content_hash': item['content_hash'], 'bytes': item['bytes'],
                      'learning_ids': sorted({r['learning_id'] for r in whole}),
                      'revision_ids': sorted(r['id'] for r in whole), 'cause': '', 'unit_kind': 'file'})
    elif changed_file:
        owner = 'edited' if any(duplicates[r['learning_id']] == 1 for r in changed_file) else 'unknown'
        labels = [owner] * len(lines)
        units.append({'start_line': 1, 'end_line': len(lines), 'ownership': owner,
                      'content_hash': item['content_hash'], 'bytes': item['bytes'],
                      'revision_ids': [], 'candidate_revision_ids': sorted(r['id'] for r in changed_file),
                      'learning_id': changed_file[0]['learning_id'],
                      'cause': 'delivered_file_changed' if owner == 'edited' else 'delivered_file_marker_missing_or_ambiguous', 'unit_kind': 'file'})
    else:
        for marker in markers:
            candidates = by_learning[marker['learning_id']]
            exact = []
            if duplicates[marker['learning_id']] == 1:
                for revision in candidates:
                    if revision['unit_kind'] != 'bullet': continue
                    current = [u for u in marked_units(text, line_counts={revision['learning_id']: len(revision['content'].splitlines())})
                               if u['learning_id'] == revision['learning_id']]
                    if len(current) == 1 and current[0]['recognized'] and current[0]['content'] == revision['content']:
                        exact.append((revision, current[0]))
            current = exact[0][1] if exact else marker
            owner = 'machine' if exact else 'edited' if candidates and marker['recognized'] and duplicates[marker['learning_id']] == 1 else 'unknown'
            cause = '' if exact else 'marked_content_differs' if owner == 'edited' else 'ambiguous_marker' if candidates else 'no_retained_delivery'
            unit = {k: current[k] for k in ('start_line', 'end_line', 'content_hash')}
            unit.update(ownership=owner, revision_ids=sorted(r['id'] for r,_ in exact),
                        learning_id=marker['learning_id'], cause=cause, unit_kind='bullet',
                        bytes=len(current['content'].encode('utf-8')))
            units.append(unit)
        # Overlapping inferred marker regions cannot allocate ownership twice.
        for unit in units:
            for other in units:
                if other is not unit and max(unit['start_line'],other['start_line']) <= min(unit['end_line'],other['end_line']):
                    unit.update(ownership='unknown', revision_ids=[], cause='overlapping_marked_units')
        for unit in units:
            labels[unit['start_line']-1:unit['end_line']] = [unit['ownership']]*(unit['end_line']-unit['start_line']+1)
    counts = _counts()
    for line, owner in zip(lines, labels):
        counts[owner]['lines'] += 1
        counts[owner]['bytes'] += len(line.encode('utf-8'))
    public = {k: v for k,v in item.items() if k not in {'_text', 'marked_units'}}
    return {**public, 'lines': len(lines), 'ownership': counts, 'units': units}


def build_inventory(*, surface, project_key, working_copy_path, revisions, observed_at, run_id='', measurements=None):
    """Pure classification; all input file reads have already finished."""
    at = timestamp(observed_at, 'inventory observation')
    selected = [validate_revision(r) for r in revisions if r['project_key'] in {'',project_key} and r['applied_at'] <= at]
    files = [_file_inventory(item, selected, working_copy_path) for item in surface['files']]
    totals = {'files': len(files), 'lines': sum(f['lines'] for f in files),
              'bytes': sum(f['bytes'] for f in files), 'ownership': _counts()}
    scopes = defaultdict(lambda: {'files': 0, 'observed_bytes': 0, 'eligible_prefix_bytes': 0})
    for file in files:
        for owner in OWNERS:
            for metric in ('lines','bytes'): totals['ownership'][owner][metric] += file['ownership'][owner][metric]
        grouped = defaultdict(list)
        for path in file['loading_paths']: grouped[(path['provider'], path['scope']['kind'])].append(path)
        for key, paths in grouped.items():
            scopes[key]['files'] += 1
            scopes[key]['observed_bytes'] += file['bytes']
            scopes[key]['eligible_prefix_bytes'] += max(p['eligible_prefix_bytes'] for p in paths)
    copy = working_copy_identity(project_key, working_copy_path)
    record = {'version': 1, 'project_key': project_key, 'working_copy_id': copy['id'], 'working_copy': copy,
              'observed_at': at, 'run_id': run_id, 'status': 'partial' if surface['issues'] else 'recorded',
              'files': files, 'totals': totals, 'scopes': [dict(provider=k[0],scope=k[1],**v) for k,v in sorted(scopes.items())],
              **{k:v for k,v in surface.items() if k not in {'files'}},
              'meaning': 'Unmarked text is human-managed, not verified human authorship. Machine units exactly match retained delivered revisions. Files and scopes do not prove session loading.'}
    if record['profile'] in {'instruction-surfaces/2', 'instruction-surfaces/3', 'instruction-surfaces/4', 'instruction-surfaces/5', 'instruction-surfaces/6'}:
        record['context'] = summarize(files)
    if record['profile'] in {'instruction-surfaces/3', 'instruction-surfaces/4', 'instruction-surfaces/5', 'instruction-surfaces/6'} and not files and any(
            issue['cause'] == 'working_copy_identity_changed' for issue in record['issues']):
        # Identity refusal bypasses filesystem discovery entirely. It retains
        # an explicit failed observation, never an observed empty catalog.
        record.setdefault('uninspected_commands', [])
        record.setdefault('incomplete_command_roots', [])
        if record['profile'] in {'instruction-surfaces/4', 'instruction-surfaces/5', 'instruction-surfaces/6'}:
            record.setdefault('managed_discovery', {'root': '', 'memory_status': 'not_checked', 'skills_status': 'not_checked'})
    if record['profile'] in {'instruction-surfaces/5', 'instruction-surfaces/6'} and 'policy_discovery' not in record:
        from .instruction_policy import unchecked
        record['policy_discovery'] = unchecked()
    if record['profile'] == 'instruction-surfaces/6' and 'plugin_discovery' not in record:
        from .instruction_plugins import unchecked
        record['plugin_discovery'] = unchecked()
    if measurements is not None:
        record['measurements'] = measurements
    record['id'] = digest(record)
    return record


def _validate(record, revisions):
    owner = 'inventory.'+str(record.get('id','?')) if isinstance(record,dict) else 'inventory'
    try:
        if record['profile'] not in {'instruction-surfaces/1', 'instruction-surfaces/2', 'instruction-surfaces/3', 'instruction-surfaces/4', 'instruction-surfaces/5', 'instruction-surfaces/6'}:
            raise ValueError('unsupported instruction source profile')
        if (record['version'] != 1 or record['id'] != digest({k:v for k,v in record.items() if k!='id'})
                or record['status'] not in {'recorded','partial'} or record['runtime_loading_verified'] is not False
                or not record['project_key'] or not isinstance(record['files'],list)
                or record['observed_at'] != timestamp(record['observed_at'],owner)
                or record['working_copy_id'] != digest([record['project_key'],record['working_copy']['normalized_path']])
                or record['working_copy']['id'] != record['working_copy_id']):
            raise ValueError('invalid identity or shape')
        if (not isinstance(record['issues'],list) or any(not isinstance(issue,dict) or not isinstance(issue.get('cause'),str) for issue in record['issues'])
                or record['status'] != ('partial' if record['issues'] else 'recorded')):
            raise ValueError('coverage and status disagree')
        totals = {'files': len(record['files']), 'bytes': 0, 'lines': 0, 'ownership': _counts()}
        scopes = defaultdict(lambda: {'files':0,'observed_bytes':0,'eligible_prefix_bytes':0})
        real_paths = set()
        for file in record['files']:
            if file['real_path'] in real_paths: raise ValueError('duplicate physical file')
            real_paths.add(file['real_path'])
            grouped = defaultdict(list)
            for path in file['loading_paths']:
                prefix = path['eligible_prefix_bytes']
                if type(prefix) is not int or not 0 <= prefix <= file['bytes'] or path['runtime_loading_verified'] is not False:
                    raise ValueError('invalid source eligibility')
                sources = {'memory', 'rule', 'skill', 'import'} | ({'command'} if record['profile'] in {'instruction-surfaces/3', 'instruction-surfaces/4', 'instruction-surfaces/5', 'instruction-surfaces/6'} else set())
                if record['profile'] in {'instruction-surfaces/5', 'instruction-surfaces/6'}: sources.add('embedded_memory')
                if record['profile'] == 'instruction-surfaces/6': sources.update({'plugin_skill','plugin_command'})
                if record['profile'] in {'instruction-surfaces/2', 'instruction-surfaces/3', 'instruction-surfaces/4', 'instruction-surfaces/5', 'instruction-surfaces/6'} and (
                        path['origin'] not in ({'global', 'project', 'managed'} if record['profile'] in {'instruction-surfaces/4', 'instruction-surfaces/5', 'instruction-surfaces/6'} else {'global', 'project'}) or path['provider'] not in {'claude', 'codex'}
                        or path['source'] not in sources
                        or (path['source'] == 'command' and path['provider'] != 'claude')
                        or path['scope']['kind'] not in {'global', 'project_always_loaded', 'path_scoped', 'on_demand'}
                        or not isinstance(path['conditions'], list) or not isinstance(path['eligibility_reason'], str)):
                    raise ValueError('invalid source-profile loading path')
                grouped[(path['provider'],path['scope']['kind'])].append(prefix)
            if not grouped: raise ValueError('file has no loading path')
            for key, values in grouped.items():
                scopes[key]['files'] += 1
                scopes[key]['observed_bytes'] += file['bytes']
                scopes[key]['eligible_prefix_bytes'] += max(values)
            for metric in ('lines','bytes'):
                if type(file[metric]) is not int or file[metric] < 0: raise ValueError('invalid file count')
                totals[metric] += file[metric]
                amounts = [file['ownership'][o][metric] for o in OWNERS]
                if any(type(v) is not int or v < 0 for v in amounts) or sum(amounts) != file[metric]:
                    raise ValueError('ownership does not reconcile')
                for o in OWNERS: totals['ownership'][o][metric] += file['ownership'][o][metric]
            machine_lines = machine_bytes = 0
            for unit in file['units']:
                empty_file = file['lines'] == 0 and unit['unit_kind'] == 'file' and unit['start_line'] == 1 and unit['end_line'] == 0
                if unit['ownership'] not in OWNERS or not (empty_file or 1 <= unit['start_line'] <= unit['end_line'] <= file['lines']):
                    raise ValueError('invalid unit range or ownership')
                for revision_id in unit.get('candidate_revision_ids',[]):
                    if revisions[revision_id]['project_key'] not in {'',record['project_key']}:
                        raise ValueError('candidate revision belongs to another project')
                if unit['ownership'] != 'machine':
                    if unit['revision_ids']: raise ValueError('unverified unit claims exact revisions')
                    continue
                if not unit['revision_ids']: raise ValueError('machine unit lacks a retained revision')
                if unit['unit_kind']=='file' and (unit['content_hash']!=file['content_hash'] or unit['bytes']!=file['bytes']):
                    raise ValueError('whole unit differs from file')
                expected_learnings = sorted({revisions[rid]['learning_id'] for rid in unit['revision_ids']})
                if (unit['learning_ids'] if unit['unit_kind']=='file' else [unit['learning_id']]) != expected_learnings:
                    raise ValueError('unit learning differs from retained revision')
                machine_lines += unit['end_line']-unit['start_line']+1
                machine_bytes += unit['bytes']
                for revision_id in unit['revision_ids']:
                    revision = revisions[revision_id]
                    if (revision['project_key'] not in {'',record['project_key']}
                            or revision['applied_at'] > record['observed_at']
                            or revision['content_hash'] != unit['content_hash']
                            or revision['unit_kind'] != unit['unit_kind']
                            or len(revision['content'].encode('utf-8')) != unit['bytes']
                            or len(revision['content'].splitlines()) != unit['end_line']-unit['start_line']+1):
                        raise ValueError('unit differs from retained revision')
            if file['ownership']['machine'] != {'lines':machine_lines,'bytes':machine_bytes}:
                raise ValueError('machine allocation differs from exact units')
        if totals != record['totals']: raise ValueError('file totals do not reconcile')
        if 'measurements' in record:
            from .instruction_metrics import validate
            validate(record['measurements'], record)
        if record['profile'] in {'instruction-surfaces/3', 'instruction-surfaces/4', 'instruction-surfaces/5', 'instruction-surfaces/6'}:
            from .instruction_commands import validate_commands
            validate_commands(record['files'], uninspected=record['uninspected_commands'], incomplete_roots=record['incomplete_command_roots'])
        if record['profile'] in {'instruction-surfaces/2', 'instruction-surfaces/3', 'instruction-surfaces/4', 'instruction-surfaces/5', 'instruction-surfaces/6'} and record['context'] != summarize(record['files']):
            raise ValueError('context totals do not reconcile')
        if record['profile'] in {'instruction-surfaces/4', 'instruction-surfaces/5', 'instruction-surfaces/6'}:
            from .instruction_managed import validate_managed
            validate_managed(record)
        if record['profile'] in {'instruction-surfaces/5', 'instruction-surfaces/6'}:
            from .instruction_policy import validate_policy
            validate_policy(record)
        if record['profile'] == 'instruction-surfaces/6':
            from .instruction_plugins import validate_plugins
            validate_plugins(record)
        if record['scopes'] != [dict(provider=k[0],scope=k[1],**v) for k,v in sorted(scopes.items())]:
            raise ValueError('source scope totals do not reconcile')
    except (KeyError,TypeError,ValueError) as exc:
        raise InventoryError(owner+': invalid retained inventory: '+str(exc)) from exc
    return record


def record_inventories(store, *, records):
    """Validate and persist inside the collector's transaction; never commit."""
    require_schema(store)
    if store.read_only or not store.conn.in_transaction:
        raise InventoryError('Inventory persistence requires the caller\'s active write transaction')
    revisions = {r['id']:r for r in retained_revisions(store)}
    for record in records:
        _validate(record,revisions)
        previous = store.query_one(f'SELECT * FROM {TABLE} WHERE id=?',(record['id'],))
        if previous:
            if read_record(previous,TABLE) != record: raise InventoryError('inventory '+record['id']+': replay changed')
            continue
        store.insert(TABLE,{**{k:record[k] for k in ('id','project_key','working_copy_id','observed_at','run_id','status')},
                            'record_json':encoded(record),'record_hash':digest(record)})
    return [r['id'] for r in records]


def _read(row, revisions):
    try:
        return _validate(read_record(row,TABLE),revisions)
    except AvailabilityError as exc:
        raise InventoryError(str(exc)) from exc


def project_inventory(store, *, project_key, limit=20, cursor=None, working_copy_id=None):
    """Latest checks per copy. No filesystem reads, migrations, or writes."""
    return _project_inventory(store, project_key=project_key, limit=limit, cursor=cursor, working_copy_id=working_copy_id)


def _project_inventory(store, *, project_key, limit=20, cursor=None, revisions=None, working_copy_id=None):
    """Share already validated revisions within one multi-project read transaction."""
    if not isinstance(project_key,str) or not project_key or type(limit) is not int or not 1 <= limit <= 100:
        raise InventoryError('Invalid inventory project or page limit')
    if working_copy_id is not None and (not isinstance(working_copy_id,str) or len(working_copy_id)!=64
            or any(c not in '0123456789abcdef' for c in working_copy_id)):
        raise InventoryError('Invalid inventory working copy')
    selector = digest(['project-inventory/1',project_key] + ([working_copy_id] if working_copy_id is not None else []))
    position = ''
    if cursor is not None:
        try:
            token = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
            position = token['position']
            if token['selector'] != selector or not isinstance(position,str) or len(position)!=64: raise ValueError('wrong selector')
        except (ValueError,KeyError,TypeError,UnicodeError) as exc:
            raise InventoryError('Invalid inventory cursor or different project') from exc
    response = {'project_key':project_key,'records':[],'count':None,'next_cursor':None,'computable':False,'reason':'schema_unavailable'}
    if working_copy_id is not None: response['working_copy_id'] = working_copy_id
    if not store.query_one('SELECT name FROM schema_migrations WHERE name=?',(MIGRATION,)): return response
    require_schema(store)
    clause = 'project_key=?' + (' AND working_copy_id=?' if working_copy_id is not None else '')
    values = (project_key,working_copy_id) if working_copy_id is not None else (project_key,)
    copies = store.query(f'SELECT working_copy_id,MAX(observed_at) AS observed_at FROM {TABLE} '
                         f'WHERE {clause} AND working_copy_id>? GROUP BY working_copy_id ORDER BY working_copy_id LIMIT ?',
                         (*values,position,limit+1))
    count = store.query_one(f'SELECT COUNT(DISTINCT working_copy_id) n FROM {TABLE} WHERE {clause}',values)['n']
    if revisions is None:
        revisions = {r['id']:r for r in retained_revisions(store)}
    records = []
    for copy in copies[:limit]:
        same = [_read(row,revisions) for row in store.query(
            f'SELECT * FROM {TABLE} WHERE project_key=? AND working_copy_id=? AND observed_at=? ORDER BY id',
            (project_key,copy['working_copy_id'],copy['observed_at']))]
        signatures = {digest({k:v for k,v in r.items() if k not in {'id','run_id'}}) for r in same}
        if len(signatures)>1:
            records.append({'working_copy_id':copy['working_copy_id'],'working_copy':same[0]['working_copy'],
                            'observed_at':copy['observed_at'],'status':'conflicting','observations':same})
        else: records.append(same[0])
    response.update(records=records,count=count,computable=bool(count),reason='' if count else 'no_inventory_observations')
    if len(copies)>limit:
        response['next_cursor']=base64.urlsafe_b64encode(encoded({'selector':selector,'position':copies[limit-1]['working_copy_id']}).encode()).decode()
    return response


def context_history(store, *, project_key, working_copy_id, limit=20, cursor=None):
    """Complete timestamp groups for one copy with adjacent measurement changes.

    The caller owns the read transaction. Cursors bind the retained-history
    revision so inserts cannot silently skip or duplicate historical groups.
    """
    from .instruction_metrics import compare
    if (not isinstance(project_key,str) or not project_key or not isinstance(working_copy_id,str)
            or len(working_copy_id) != 64 or any(c not in '0123456789abcdef' for c in working_copy_id)
            or type(limit) is not int or not 1 <= limit <= 100):
        raise InventoryError('Invalid inventory history selection or page limit')
    response = {'project_key':project_key, 'working_copy_id':working_copy_id, 'records':[],
                'count':None, 'count_unit':'observation times', 'next_cursor':None,
                'computable':False, 'reason':'schema_unavailable'}
    if not store.query_one('SELECT name FROM schema_migrations WHERE name=?',(MIGRATION,)):
        return response
    require_schema(store)
    metadata = store.query(f'SELECT id,record_hash,observed_at FROM {TABLE} WHERE project_key=? AND working_copy_id=? ORDER BY observed_at DESC,id', (project_key,working_copy_id))
    revision = digest(metadata)
    selector = digest(['context-history/1', project_key, working_copy_id, revision])
    position = None
    if cursor is not None:
        try:
            token = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
            position = token['position']
            if token['selector'] != selector or position != timestamp(position,'cursor'):
                raise ValueError('selection or history changed')
        except (ValueError, KeyError, TypeError, AttributeError, UnicodeError, AvailabilityError) as exc:
            raise InventoryError('Invalid inventory history cursor: selection or retained history changed; refresh history') from exc
    times = sorted({r['observed_at'] for r in metadata}, reverse=True)
    if position is not None:
        times = [at for at in times if at < position]
    revisions = {r['id']:r for r in retained_revisions(store)}
    groups = []
    for at in times[:limit+1]:
        same = [_read(row,revisions) for row in store.query(f'SELECT * FROM {TABLE} WHERE project_key=? AND working_copy_id=? AND observed_at=? ORDER BY id', (project_key,working_copy_id,at))]
        signatures = {digest({k:v for k,v in r.items() if k not in {'id','run_id'}}) for r in same}
        if len(signatures)>1:
            record = {'working_copy_id':working_copy_id,'working_copy':same[0]['working_copy'],
                      'observed_at':at,'status':'conflicting','observations':same}
        else:
            record = {**same[0], 'observations':same}
        groups.append(record)
    for i,record in enumerate(groups[:limit]):
        previous = groups[i+1] if i+1<len(groups) else None
        record['comparison'] = compare(record,previous)
        record['previous_observed_at'] = previous['observed_at'] if previous else None
    count = len({r['observed_at'] for r in metadata})
    response.update(records=groups[:limit],count=count,revision=revision,computable=bool(count),reason='' if count else 'no_inventory_observations')
    if len(groups)>limit:
        response['next_cursor'] = base64.urlsafe_b64encode(encoded({'selector':selector,'position':groups[limit-1]['observed_at']}).encode()).decode()
    return response
