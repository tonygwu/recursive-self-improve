"""Pure configured command identity; never invoke providers or command bodies."""
from collections import defaultdict
from copy import deepcopy
from pathlib import Path


def command_metadata(path, *, source, discovery_root):
    """Use lexical discovery paths so symlink aliases retain their own names."""
    path, root = Path(path), Path(discovery_root)
    if not path.is_absolute() or not root.is_absolute():
        raise ValueError('command discovery paths must be absolute')
    relative = path.relative_to(root)
    if '..' in relative.parts or source not in {'skill', 'command'}:
        raise ValueError('invalid command discovery path')
    if source == 'skill':
        if len(relative.parts) != 2 or relative.name != 'SKILL.md':
            raise ValueError('invalid skill discovery path')
        name = relative.parent.name
    else:
        if relative.suffix != '.md':
            raise ValueError('invalid legacy command path')
        name = ':'.join(relative.with_suffix('').parts)
    if not name:
        raise ValueError('empty command name')
    return {'discovery_root': str(root), 'command_name': name, 'shadowed_by': []}


DERIVED_REASONS = {'command_shadowed_by_skill', 'command_name_precedence_unresolved', 'command_discovery_incomplete'}


def resolve_commands(files, issue, *, uninspected=(), incomplete_roots=()):
    """Resolve same-location shadowing; retain cross-location ambiguity.

    Eligibility is only a candidate under this profile, never runtime selection.
    Do not fall back to a command when a same-name skill could not be parsed.
    """
    names = defaultdict(list)
    for file in files:
        for path in file['loading_paths']:
            if path['provider'] == 'claude' and path['source'] in {'skill', 'command'}:
                names[path['command_name']].append((file, path))
    for missing in uninspected:
        names[missing['command_name']].append(({'real_path': missing['path']},
            {**missing, 'eligibility_reason': 'command_source_unreadable'}))
    for entries in names.values():
        locations = defaultdict(list)
        for file, path in entries:
            locations[str(Path(path['discovery_root']).parent)].append((file, path))
        for local in locations.values():
            skills = [(f, p) for f, p in local if p['source'] == 'skill']
            if len({f['real_path'] for f, p in skills}) != 1 or any(p['eligibility_reason'] for f, p in skills):
                continue
            for file, path in local:
                if path['source'] == 'command' and not path['eligibility_reason']:
                    path.update(eligible_prefix_bytes=0, eligibility_reason='command_shadowed_by_skill',
                                shadowed_by=sorted({p['path'] for f, p in skills}))
        remaining = [(f, p) for f, p in entries if p['eligibility_reason'] != 'command_shadowed_by_skill']
        if len({f['real_path'] for f, p in remaining}) > 1:
            for file, path in remaining:
                if not path['eligibility_reason']:
                    path.update(eligible_prefix_bytes=0, eligibility_reason='command_name_precedence_unresolved')
                    issue('command_name_precedence_unresolved', path['path'])
        if incomplete_roots:
            # An incomplete namespace can hide a same-name competitor anywhere
            # in the selected catalog. Do not infer a fallback from its absence.
            for file, path in entries:
                if not path['eligibility_reason']:
                    path.update(eligible_prefix_bytes=0, eligibility_reason='command_discovery_incomplete')


def validate_commands(files, *, uninspected=(), incomplete_roots=()):
    """Validate retained invocation identity and shadow references without I/O."""
    if (not isinstance(uninspected, list) or not isinstance(incomplete_roots, list)
            or any(not isinstance(p, str) or not Path(p).is_absolute() for p in incomplete_roots)
            or incomplete_roots != sorted(set(incomplete_roots))):
        raise ValueError('invalid command discovery coverage')
    for missing in uninspected:
        expected = command_metadata(missing['path'], source=missing['source'], discovery_root=missing['discovery_root'])
        if missing != {**expected, 'path': missing['path'], 'source': missing['source'], 'provider': 'claude'}:
            raise ValueError('invalid uninspected command identity')
    entries = [p for f in files for p in f['loading_paths']
               if p['provider'] == 'claude' and p['source'] in {'skill', 'command'}]
    paths = {p['path']: p for p in entries}
    for path in entries:
        expected = command_metadata(path['path'], source=path['source'], discovery_root=path['discovery_root'])
        if path['command_name'] != expected['command_name']:
            raise ValueError('command name differs from discovery path')
        if path['eligibility_reason'] and path['eligible_prefix_bytes'] != 0:
            raise ValueError('ineligible command path claims candidate bytes')
        shadows = path['shadowed_by']
        if not isinstance(shadows, list) or any(not isinstance(p, str) for p in shadows) or shadows != sorted(set(shadows)):
            raise ValueError('invalid command shadow references')
        if bool(shadows) != (path['eligibility_reason'] == 'command_shadowed_by_skill'):
            raise ValueError('command shadow reason disagrees')
        if shadows and (path['source'] != 'command' or path['eligible_prefix_bytes'] != 0):
            raise ValueError('shadowed command claims eligibility')
        for target in shadows:
            skill = paths.get(target)
            if (not skill or skill['source'] != 'skill' or skill['command_name'] != path['command_name']
                    or Path(skill['discovery_root']).parent != Path(path['discovery_root']).parent):
                raise ValueError('command shadow target differs from its namespace')
        if path['scope'] != {'kind': 'on_demand', 'paths': []} and path['source'] == 'command':
            raise ValueError('command body is not on demand')
    expected_files = deepcopy(files)
    for file in expected_files:
        for path in file['loading_paths']:
            if path['provider'] == 'claude' and path['source'] in {'skill', 'command'}:
                if path['eligibility_reason'] in DERIVED_REASONS:
                    path.update(eligible_prefix_bytes=file['bytes'], eligibility_reason='', shadowed_by=[])
    resolve_commands(expected_files, lambda *a, **k: None, uninspected=uninspected, incomplete_roots=incomplete_roots)
    if expected_files != files:
        raise ValueError('command precedence does not reconcile')
