"""Validate retained managed file observations without filesystem access."""
from pathlib import PurePath


def validate_managed(record):
    managed = record['managed_discovery']
    if not isinstance(managed, dict) or set(managed) != {'root', 'memory_status', 'skills_status'}:
        raise ValueError('invalid managed discovery shape')
    root = managed['root']
    if not isinstance(root, str):
        raise ValueError('invalid managed root type')
    statuses = [managed['memory_status'], managed['skills_status']]
    if any(status not in {'observed', 'absent', 'failed', 'not_checked'} for status in statuses):
        raise ValueError('invalid managed discovery outcome')
    refused = not record['files'] and any(i['cause'] in {
        'working_copy_identity_changed', 'working_copy_missing', 'working_copy_not_git'} for i in record['issues'])
    if 'not_checked' in statuses:
        if not refused or statuses != ['not_checked', 'not_checked']:
            raise ValueError('managed discovery was not checked without identity refusal')
        if root and (not isinstance(root, str) or not PurePath(root).is_absolute()):
            raise ValueError('invalid unchecked managed root')
        return
    if refused or not isinstance(root, str) or not PurePath(root).is_absolute():
        raise ValueError('invalid managed discovery root')
    if any('path' in issue and not isinstance(issue['path'], str) for issue in record['issues']):
        raise ValueError('invalid managed discovery issue path')
    root = PurePath(root)
    memory, skills = str(root/'CLAUDE.md'), root/'.claude/skills'
    paths = [(file, path) for file in record['files'] for path in file['loading_paths']
             if path['origin'] == 'managed' and path['source'] not in {'embedded_memory','plugin_skill','plugin_command'}]
    memory_paths = [(f, p) for f, p in paths if p['source'] == 'memory']
    if len(memory_paths) != (1 if managed['memory_status'] == 'observed' else 0):
        raise ValueError('managed memory outcome differs from retained paths')
    for file, path in paths:
        if path['provider'] != 'claude' or path['source'] not in {'memory', 'skill', 'import'}:
            raise ValueError('unsupported managed provider or source')
        if path['source'] == 'skill':
            if (path['discovery_root'] != str(skills) or path['import_chain']
                    or path['scope']['kind'] != 'on_demand'
                    or managed['skills_status'] not in {'observed', 'failed'}):
                raise ValueError('managed skill differs from its discovery root')
            if PurePath(path['path']).parent.name.casefold() == 'synced' and (
                    not path['eligibility_reason'] or path['eligible_prefix_bytes'] != 0):
                raise ValueError('reserved managed skill claims eligibility')
        elif path['scope'] != {'kind': 'global', 'paths': []} or path['conditions']:
            raise ValueError('managed memory scope differs')
        elif path['source'] == 'memory':
            if path['path'] != memory or path['import_chain']:
                raise ValueError('managed memory differs from its native file')
        else:
            chain = path['import_chain']
            if (not isinstance(chain, list) or not chain or not memory_paths
                    or chain[0] != memory_paths[0][0]['real_path']):
                raise ValueError('managed import lacks its memory origin')
            # Each retained predecessor must belong to this same import chain.
            for index, physical in enumerate(chain):
                if not any(f['real_path'] == physical and p['source'] in {'memory', 'import'}
                           and p['import_chain'] == chain[:index] for f, p in paths):
                    raise ValueError('managed import chain is incomplete')
    if managed['skills_status'] == 'observed' and any(
            p['eligibility_reason'] in {'frontmatter_invalid', 'frontmatter_unterminated', 'skill_metadata_invalid'}
            for f, p in paths if p['source'] == 'skill'):
        raise ValueError('managed skill failure claims a complete read')
    # Discovery failures must carry their producer identity. A memory import
    # can point below the skill directory without being skill discovery.
    memory_failed = any(i.get('path') == memory and (i['cause'] == 'managed_memory_read_failed'
                        or i['cause'].startswith('managed_entry_unreadable:')) for i in record['issues'])
    skill_failed = bool(str(skills) in record['incomplete_command_roots'] or any(
        p['discovery_root'] == str(skills) for p in record['uninspected_commands']) or any(
        p['eligibility_reason'] in {'frontmatter_invalid', 'frontmatter_unterminated', 'skill_metadata_invalid'}
        for f, p in paths if p['source'] == 'skill'))
    if ((managed['memory_status'] == 'failed') != memory_failed
            or (managed['skills_status'] == 'failed') != skill_failed):
        raise ValueError('managed discovery outcome contradicts retained failure evidence')
