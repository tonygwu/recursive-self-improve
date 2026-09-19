"""Observe embedded managed instruction fields without executing policy.

Only decoded claudeMd text and a small provenance allowlist leave this module.
File-policy selection is not evidence that a running provider selected it.
"""
from itertools import islice
from pathlib import PurePath
from urllib.parse import quote
import json
import math
import os
import re

from .rule_revisions import marked_units, text_hash


def field_identity(path):
    return 'managed-text:' + quote(str(path), safe='/') + '#/claudeMd'


def unchecked(root=''):
    return {'root': str(root), 'main_status': 'not_checked', 'dropins_status': 'not_checked',
            'sources': [], 'failures': [], 'selection': 'not_checked', 'selected_source': '',
            'selected_bytes': None, 'helper_present': None, 'runtime_selection_verified': False}


def _selection(policy):
    if policy['main_status'] == 'not_checked':
        return 'not_checked', '', None, None
    if policy['failures']:
        return 'unknown', '', None, None
    fields = [source for source in policy['sources'] if source['field_state'] == 'text']
    helper = any(source['helper_present'] for source in policy['sources'])
    return ('selected', fields[-1]['path'], fields[-1]['field_bytes'], helper) if fields else ('absent', '', 0, helper)


def _reason(policy, path):
    if policy['selection'] == 'unknown': return 'managed_file_policy_incomplete'
    if path != policy['selected_source']: return 'managed_field_overridden'
    if policy['helper_present']: return 'managed_policy_helper_unobserved'
    return 'managed_policy_selection_unverified'


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result: raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _constant(value):
    raise ValueError('nonfinite JSON constant')


def _float(value):
    number = float(value)
    if not math.isfinite(number): raise ValueError('nonfinite JSON number')
    return number


def inspect_policy(root, *, read, present, issue, max_entries):
    """Reuse the collector's bounded reads; return field bodies and provenance."""
    policy = unchecked(root)
    policy.update(main_status='absent', dropins_status='absent')
    main, dropins = root/'managed-settings.json', root/'managed-settings.d'
    paths, files = [], {}

    def failure(path, cause, **extra):
        record = {'path': str(path), 'cause': 'managed_policy_' + cause, **extra}
        policy['failures'].append(record)
        issue(record['cause'], path, **extra)
        policy['main_status' if path == main else 'dropins_status'] = 'failed'

    try:
        if present(main):
            paths.append(main)
            policy['main_status'] = 'observed'
    except (OSError, RuntimeError) as exc:
        failure(main, 'entry_unreadable', error_type=type(exc).__name__)
    try:
        if present(dropins):
            policy['dropins_status'] = 'observed'
            with os.scandir(dropins) as entries:
                batch = list(islice(entries, max_entries + 1))
            if len(batch) > max_entries:
                # No arbitrary partial directory order can claim the last value.
                failure(dropins, 'entry_limit', limit=max_entries, omitted_at_least=1)
            else:
                paths.extend(sorted((root/'managed-settings.d'/entry.name for entry in batch
                                     if not entry.name.startswith('.') and entry.name.endswith('.json')),
                                    key=lambda p: p.name))
    except (OSError, RuntimeError) as exc:
        failure(dropins, 'directory_unreadable', error_type=type(exc).__name__)

    for path in paths:
        item = read(path)
        if item is None:
            failure(path, 'read_failed')
            continue
        try:
            values = json.loads(item['_text'] if item['_text'].strip() else '{}', object_pairs_hook=_object, parse_constant=_constant, parse_float=_float)
            if not isinstance(values, dict): raise ValueError('policy is not an object')
            text = values.get('claudeMd')
            if 'claudeMd' in values and not isinstance(text, str): raise ValueError('claudeMd is not text')
            size = len(text.encode('utf-8')) if text is not None else 0
            source = {'path': str(path), 'container_real_path': item['real_path'],
                      'container_hash': item['content_hash'],
                      'field_state': 'text' if text is not None else 'absent',
                      'field_id': field_identity(item['real_path']) if text is not None else '',
                      'field_hash': text_hash(text) if text is not None else '', 'field_bytes': size,
                      'helper_present': 'policyHelper' in values}
        except (ValueError, RecursionError, UnicodeError) as exc:
            failure(path, 'json_invalid', error_type=type(exc).__name__)
            continue
        policy['sources'].append(source)
        if text is None: continue
        alias = field_identity(path)
        file = files.setdefault(source['field_id'], {'path': alias, 'real_path': source['field_id'],
            'aliases': [], 'bytes': size, 'content_hash': source['field_hash'], 'loading_paths': [],
            '_text': text, 'marked_units': marked_units(text)})
        file['aliases'].append(alias)
        file['loading_paths'].append({'path': alias, 'provider': 'claude', 'origin': 'managed',
            'source': 'embedded_memory', 'scope': {'kind':'global','paths':[]}, 'conditions': [],
            'skill_name': '', 'import_chain': [], 'eligible_prefix_bytes': 0,
            'eligibility_reason': '', 'runtime_loading_verified': False,
            'container_path': source['path'], 'container_real_path': source['container_real_path'],
            'container_hash': source['container_hash'], 'json_pointer': '/claudeMd'})
    policy['selection'], policy['selected_source'], policy['selected_bytes'], policy['helper_present'] = _selection(policy)
    for file in files.values():
        for path in file['loading_paths']:
            path['eligibility_reason'] = _reason(policy, path['container_path'])
    return policy, list(files.values())


def validate_policy(record):
    """Validate retained projection and field identities using no filesystem reads."""
    policy = record['policy_discovery']
    if not isinstance(policy, dict) or set(policy) != set(unchecked()):
        raise ValueError('invalid managed policy shape')
    root = policy['root']
    if not isinstance(root, str) or root != record['managed_discovery']['root']:
        raise ValueError('managed policy root differs')
    if (policy['runtime_selection_verified'] is not False or not isinstance(policy['sources'], list)
            or not isinstance(policy['failures'], list)):
        raise ValueError('invalid managed policy evidence')
    statuses = (policy['main_status'], policy['dropins_status'])
    if any(status not in {'not_checked','absent','observed','failed'} for status in statuses):
        raise ValueError('invalid managed policy outcome')
    if 'not_checked' in statuses:
        if (statuses != ('not_checked','not_checked') or record['files'] or policy['sources'] or policy['failures']
                or record['managed_discovery']['memory_status'] != 'not_checked'):
            raise ValueError('managed policy unchecked without refused collection')
    elif not PurePath(root).is_absolute() or record['managed_discovery']['memory_status'] == 'not_checked':
        raise ValueError('managed policy checked without a root')
    main, dropins = str(PurePath(root)/'managed-settings.json'), PurePath(root)/'managed-settings.d'

    def native(path):
        if not isinstance(path, str): return False
        p = PurePath(path)
        return path == main or (p.parent == dropins and not p.name.startswith('.') and p.name.endswith('.json'))

    def hashed(value):
        return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None

    failures = policy['failures']
    for failure in failures:
        if (not isinstance(failure, dict) or not isinstance(failure.get('path'), str)
                or not (native(failure['path']) or failure['path'] == str(dropins))
                or failure.get('cause') not in {'managed_policy_entry_unreadable','managed_policy_directory_unreadable',
                    'managed_policy_entry_limit','managed_policy_read_failed','managed_policy_json_invalid'}
                or failure not in record['issues']):
            raise ValueError('invalid managed policy failure evidence')
    if [i for i in record['issues'] if i['cause'].startswith('managed_policy_')] != failures:
        raise ValueError('managed policy failures differ from inspection issues')
    if ('not_checked' not in statuses and
            ((policy['main_status'] == 'failed') != any(f['path'] == main for f in failures)
             or (policy['dropins_status'] == 'failed') != any(f['path'] != main for f in failures))):
        raise ValueError('managed policy status contradicts failure evidence')
    sources = policy['sources']
    paths, containers = [], {}
    for source in sources:
        if (not isinstance(source, dict) or set(source) != {'path','container_real_path','container_hash',
                'field_state','field_id','field_hash','field_bytes','helper_present'}
                or not native(source['path']) or not isinstance(source['container_real_path'], str)
                or not PurePath(source['container_real_path']).is_absolute() or not hashed(source['container_hash'])
                or source['field_state'] not in {'absent','text'} or type(source['helper_present']) is not bool
                or type(source['field_bytes']) is not int or source['field_bytes'] < 0):
            raise ValueError('invalid managed policy source')
        if source['field_state'] == 'text':
            if source['field_id'] != field_identity(source['container_real_path']) or not hashed(source['field_hash']):
                raise ValueError('managed field identity differs')
        elif (source['field_id'], source['field_hash'], source['field_bytes']) != ('','',0):
            raise ValueError('absent managed field claims text')
        snapshot = {k:v for k,v in source.items() if k != 'path'}
        previous = containers.setdefault(source['container_real_path'], snapshot)
        if previous != snapshot:
            raise ValueError('managed container aliases have contradictory snapshots')
        paths.append(source['path'])
    if paths != sorted(set(paths), key=lambda path: (path != main, PurePath(path).name)):
        raise ValueError('managed policy order or aliases differ')
    if ((main in paths) != (policy['main_status'] == 'observed')
            or any(path != main for path in paths) and policy['dropins_status'] not in {'observed','failed'}
            or any(path in paths for path in (f['path'] for f in failures))):
        raise ValueError('managed policy outcomes differ from sources')
    expected = _selection(policy)
    if (policy['selection'], policy['selected_source'], policy['selected_bytes'], policy['helper_present']) != expected:
        raise ValueError('managed policy selection differs from ordered fields')
    if policy['selected_bytes'] is not None and type(policy['selected_bytes']) is not int:
        raise ValueError('invalid selected field bytes')
    if policy['helper_present'] is not None and type(policy['helper_present']) is not bool:
        raise ValueError('invalid selected policy helper')
    field_sources = {s['path']:s for s in sources if s['field_state'] == 'text'}
    seen = []
    for file in record['files']:
        embedded = [p for p in file['loading_paths'] if p['source'] == 'embedded_memory']
        if not embedded and not file['real_path'].startswith('managed-text:'): continue
        if not embedded or len(embedded) != len(file['loading_paths']):
            raise ValueError('managed text field confused with a physical file')
        aliases = []
        for path in embedded:
            source = field_sources[path['container_path']]
            alias = field_identity(source['path'])
            if (file['real_path'] != source['field_id'] or file['content_hash'] != source['field_hash']
                    or file['bytes'] != source['field_bytes'] or path['path'] != alias
                    or path['container_real_path'] != source['container_real_path']
                    or path['container_hash'] != source['container_hash'] or path['json_pointer'] != '/claudeMd'
                    or path['provider'] != 'claude' or path['origin'] != 'managed'
                    or path['scope'] != {'kind':'global','paths':[]} or path['conditions'] or path['import_chain']
                    or path['skill_name'] != '' or path['eligible_prefix_bytes'] != 0
                    or path['eligibility_reason'] != _reason(policy, source['path'])):
                raise ValueError('managed text field differs from source evidence')
            seen.append(source['path']); aliases.append(alias)
        if file['aliases'] != aliases or file['path'] != aliases[0]:
            raise ValueError('managed text field aliases differ')
    if sorted(seen) != sorted(field_sources):
        raise ValueError('managed text field coverage differs from source evidence')
