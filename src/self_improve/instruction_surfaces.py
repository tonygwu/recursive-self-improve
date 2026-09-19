"""Observe supported instruction surfaces with explicit loading paths and limits.

This is a filesystem observation, never proof that an agent loaded an instruction.
No writes, database access, model calls, or provider processes occur here.
"""
from __future__ import annotations

from collections import deque
from itertools import islice
from pathlib import Path
import os
import stat
import tomllib

import yaml

from .dashboard.context_weight import find_imports
from .rule_revisions import AvailabilityError, marked_units, text_hash
from .instruction_commands import command_metadata, resolve_commands
from .instruction_policy import inspect_policy, unchecked
from .instruction_plugins import inspect_plugins, unchecked as unchecked_plugins

MAX_FILE_BYTES = 1 << 20
MAX_FILES = 2000
MAX_IMPORT_DEPTH = 5
MAX_DISCOVERY_ENTRIES = 10000
PROFILE = 'instruction-surfaces/6'


def inspect_surfaces(cfg, working_copy_path, *, global_targets=()):
    """Return one physical file plus all provider/loading paths that reach it.

    Inspect configured global roots even without retained delivered revisions.
    global_targets only makes missing configured targets explicit; it cannot
    turn an arbitrary absolute file into an instruction source.
    """
    root = Path(working_copy_path).resolve()
    managed_root = Path(cfg.claude_managed_dir)
    result = {'plugin_discovery': unchecked_plugins(), 'policy_discovery': unchecked(managed_root), 'managed_discovery': {'root': str(managed_root), 'memory_status': 'not_checked', 'skills_status': 'not_checked'},
              'files': [], 'issues': [], 'deduplicated': [], 'profile': PROFILE,
              'uninspected_commands': [], 'incomplete_command_roots': [],
              'limits': {'max_file_bytes': MAX_FILE_BYTES, 'max_files': MAX_FILES, 'max_import_depth': MAX_IMPORT_DEPTH,
                         'max_discovery_entries_per_root': MAX_DISCOVERY_ENTRIES},
              'runtime_loading_verified': False,
              'unobserved_sources': ['embedded_policy_imports', 'effective_managed_policy', 'remote_os_policy', 'codex_managed_sources', 'runtime_plugins', 'additional_directories',
                                     'session_receipts', 'skill_catalog_metadata_budget', 'runtime_exclusions'],
              'scope_note': 'Observed configured instruction surfaces; per-session loading, runtime flags and policy exclusions are not inferred.'}
    if not root.is_dir():
        result['issues'].append({'cause': 'working_copy_missing', 'path': str(root)})
        return result
    git_root = root
    while git_root.parent != git_root and not os.path.lexists(git_root / '.git'):
        git_root = git_root.parent
    if not os.path.lexists(git_root / '.git'):
        result['issues'].append({'cause': 'working_copy_not_git', 'path': str(root)})
        return result
    ancestors = list(reversed([root, *root.parents[:len(root.parents) - len(git_root.parents)]]))
    queue = deque()
    cache, physical, visited = {}, {}, set()
    multiply_linked = set()
    physical_ids = {}

    def issue(cause, path, **extra):
        result['issues'].append({'cause': cause, 'path': str(path), **extra})

    def read(path):
        lexical = str(path.absolute())
        try:
            real = str(path.resolve(strict=True))
            if real in cache:
                return cache[real]
            before = path.stat()
            if not stat.S_ISREG(before.st_mode):
                issue('not_regular_file', path); return None
            physical_key = (before.st_dev, before.st_ino)
            if physical_key in physical:
                return physical[physical_key]
            if len(cache) >= MAX_FILES:
                issue('file_count_limit', path, limit=MAX_FILES, omitted_at_least=1); return None
            flags = os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_NOFOLLOW', 0)
            with os.fdopen(os.open(real, flags), 'rb') as handle:
                opened = os.fstat(handle.fileno())
                if not stat.S_ISREG(opened.st_mode):
                    issue('not_regular_file', path); return None
                raw = handle.read(MAX_FILE_BYTES + 1)
                after = os.fstat(handle.fileno())
            current = path.stat()
            signature = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
            if signature(before) != signature(after) or signature(after) != signature(current):
                issue('file_changed_during_observation', path); return None
            if len(raw) > MAX_FILE_BYTES:
                issue('file_byte_limit', path, bytes=after.st_size, omitted_bytes=max(0, after.st_size-MAX_FILE_BYTES)); return None
            text = raw.decode('utf-8')
        except FileNotFoundError:
            issue('file_missing', path); return None
        except (OSError, UnicodeError, RuntimeError) as exc:
            issue('file_unreadable:'+type(exc).__name__, path); return None
        item = {'path': lexical, 'real_path': real, 'aliases': [lexical], 'bytes': len(raw),
                'content_hash': text_hash(text), 'loading_paths': [], '_text': text,
                'marked_units': marked_units(text)}
        if before.st_nlink > 1: multiply_linked.add(real)
        physical_ids[real] = physical_key
        cache[real] = item
        physical[physical_key] = item
        return item

    def seed(path, provider, mode, *, origin='project', source='memory', optional=True, discovery_root=None):
        if optional and not os.path.lexists(path):
            return
        queue.append((path, provider, {'kind': mode, 'paths': []}, 0, [], [], origin, source, discovery_root))

    def present(path):
        try:
            path.lstat()
            return True
        except FileNotFoundError:
            # A missing child below a dangling directory link is a failed read,
            # not a known absent source. Stop at the nearest existing ancestor.
            for parent in path.parents:
                try:
                    info = parent.lstat()
                except FileNotFoundError:
                    continue
                if stat.S_ISLNK(info.st_mode):
                    parent.stat()  # expose broken/denied link resolution
                break
            return False

    def discover(base, *, skills=False, preserve_alias_names=False):
        try:
            if not present(base): return []
        except (OSError, RuntimeError) as exc:
            issue('directory_unreadable:'+type(exc).__name__, base)
            return []
        pending, seen, found, visited_entries = [(base, ())], set(), [], 0
        while pending:
            directory, parents = pending.pop()
            try:
                real = str(directory.resolve(strict=True))
                if real in parents or (not preserve_alias_names and real in seen):
                    result['deduplicated'].append({'path': str(directory), 'real_path': real, 'cause': 'directory_alias_or_cycle'})
                    continue
                seen.add(real)
                with os.scandir(directory) as entries:
                    batch = list(islice(entries, MAX_DISCOVERY_ENTRIES - visited_entries + 1))
                visited_entries += len(batch)
                if visited_entries > MAX_DISCOVERY_ENTRIES:
                    issue('discovery_entry_limit', base, omitted_at_least=1, limit=MAX_DISCOVERY_ENTRIES)
                    break
                for entry in sorted(batch, key=lambda e: e.name):
                    path = Path(entry.path)
                    if entry.is_symlink() and not path.exists():
                        issue('directory_entry_unresolved', path)
                        continue
                    if entry.is_dir(follow_symlinks=True):
                        if skills:
                            candidate = path/'SKILL.md'
                            if present(candidate): found.append(candidate)
                        else:
                            pending.append((path, (*parents, real)))
                    elif not skills and path.suffix == '.md':
                        found.append(path)
                    if len(found) > MAX_FILES:
                        issue('discovery_file_limit', base, omitted_at_least=1, limit=MAX_FILES)
                        return sorted(found[:MAX_FILES])
            except (OSError, RuntimeError) as exc:
                issue('directory_unreadable:'+type(exc).__name__, directory)
        return sorted(found)

    def discover_commands(base, *, skills=False):
        before = len(result['issues'])
        found = discover(base, skills=skills, preserve_alias_names=not skills)
        if len(result['issues']) > before:
            result['incomplete_command_roots'] = sorted(set(result['incomplete_command_roots']) | {str(base.absolute())})
        return found

    # Retain only discovery settings and the source hash, never unrelated
    # provider configuration. Runtime overrides are still unverified.
    codex_config = Path(cfg.codex_global_agents_md).parent/'config.toml'
    codex = {'max_bytes': 32768, 'fallback_filenames': [], 'configuration_valid': True,
             'skill_configuration_valid': True, 'skill_overrides': [],
             'runtime_overrides_verified': False, 'sources': []}
    config_paths = [codex_config, *(directory/'.codex/config.toml' for directory in ancestors)]
    for config_path in config_paths:
        if not os.path.lexists(config_path): continue
        config_file = read(config_path)
        if config_file is None:
            codex['configuration_valid'] = codex['skill_configuration_valid'] = False; continue
        codex['sources'].append({'path': str(config_path), 'content_hash': config_file['content_hash']})
        try:
            values = tomllib.loads(config_file['_text'])
            if config_path != codex_config and 'skills' in values:
                issue('codex_project_skill_configuration_requires_runtime_trust', config_path)
                codex['skill_configuration_valid'] = False
            elif 'skills' in values:
                try:
                    settings = values['skills']
                    if not isinstance(settings, dict) or not isinstance(settings.get('config', []), list):
                        raise ValueError('invalid skills configuration')
                    for setting in settings.get('config', []):
                        if (not isinstance(setting, dict) or not isinstance(setting.get('path'), str)
                                or not Path(setting['path']).is_absolute() or type(setting.get('enabled')) is not bool):
                            raise ValueError('invalid skill override')
                        codex['skill_overrides'].append({'path': str(Path(setting['path']).resolve()), 'enabled': setting['enabled']})
                except (ValueError, OSError, RuntimeError):
                    issue('codex_skill_configuration_invalid', config_path)
                    codex['skill_configuration_valid'] = False
            relevant = {'project_doc_max_bytes', 'project_doc_fallback_filenames'} & values.keys()
            if config_path != codex_config and relevant:
                issue('codex_project_configuration_requires_runtime_trust', config_path)
                codex['configuration_valid'] = False
                continue
            maximum = values.get('project_doc_max_bytes', codex['max_bytes'])
            fallbacks = values.get('project_doc_fallback_filenames', codex['fallback_filenames'])
            if (type(maximum) is not int or maximum < 0 or not isinstance(fallbacks, list)
                    or any(not isinstance(v, str) or not v or Path(v).name != v or v in {'.', '..'} for v in fallbacks)):
                raise ValueError('invalid discovery settings')
            codex.update(max_bytes=maximum, fallback_filenames=fallbacks)
        except (tomllib.TOMLDecodeError, ValueError):
            issue('codex_discovery_configuration_invalid', config_path)
            codex['configuration_valid'] = codex['skill_configuration_valid'] = False
    result['codex_discovery'] = codex

    for directory in ancestors:
        for path in (directory/'CLAUDE.md', directory/'.claude/CLAUDE.md', directory/'CLAUDE.local.md'):
            seed(path, 'claude', 'project_always_loaded')
        # The override wins only when it is nonempty. A read error must not
        # silently select the lower-priority file and claim normal loading.
        for name in ('AGENTS.override.md', 'AGENTS.md', *codex['fallback_filenames']):
            path = directory/name
            if os.path.lexists(path):
                item = read(path)
                if item is None or item['_text'].strip():
                    if item is not None: seed(path, 'codex', 'project_always_loaded')
                    break
        rules = directory/'.claude/rules'
        for path in discover(rules):
            seed(path, 'claude', 'project_always_loaded', source='rule')
        for base in (directory/'.claude/skills', directory/'.agents/skills'):
            for path in (discover_commands(base, skills=True) if base.parent.name == '.claude' else discover(base, skills=True)):
                seed(path, 'claude' if base.parent.name == '.claude' else 'codex', 'on_demand', source='skill', discovery_root=base)
        commands = directory/'.claude/commands'
        for path in discover_commands(commands):
            seed(path, 'claude', 'on_demand', source='command', discovery_root=commands)

    # Only configured global targets can produce global availability. A random
    # absolute file with a matching marker is not automatically an instruction.
    selected = {str(Path(p).resolve()) for p in global_targets}
    for raw, provider in ((cfg.global_claude_md, 'claude'), (cfg.codex_global_agents_md, 'codex')):
        path = Path(raw)
        if provider == 'codex' and os.path.lexists(path.with_name('AGENTS.override.md')):
            override = read(path.with_name('AGENTS.override.md'))
            if override is None: continue
            if override['_text'].strip(): path = path.with_name('AGENTS.override.md')
        seed(path, provider, 'global', origin='global', optional=str(path.resolve()) not in selected)
    for path in discover(Path(cfg.global_claude_md).parent/'rules'):
        seed(path, 'claude', 'global', origin='global', source='rule')
    codex_skills = Path(cfg.codex_skills_dir) if cfg.codex_skills_dir else Path(cfg.codex_global_agents_md).parent.parent/'.agents/skills'
    skill_roots = [(Path(cfg.skills_dir), 'claude'), (codex_skills, 'codex')]
    result['configured_skill_roots'] = [{'path': str(base.absolute()), 'provider': provider} for base, provider in skill_roots]
    for base, provider in skill_roots:
        for path in (discover_commands(base, skills=True) if provider == 'claude' else discover(base, skills=True)):
            seed(path, provider, 'on_demand', origin='global', source='skill', discovery_root=base)
    commands = Path(cfg.global_claude_md).parent/'commands'
    for path in discover_commands(commands):
        seed(path, 'claude', 'on_demand', origin='global', source='command', discovery_root=commands)
    for raw in sorted(selected):
        path = Path(raw)
        for base, provider in skill_roots:
            if path.is_relative_to(base.resolve()) and path.name == 'SKILL.md' and path.parent.parent == base.resolve():
                seed(path, provider, 'on_demand', origin='global', source='skill', optional=False, discovery_root=base.resolve())

    # Native managed file surfaces are observations, never writer targets.
    # lstat distinguishes an absent entry from denied or broken discovery.
    managed = result['managed_discovery']
    def managed_entry(path, field):
        try:
            if not present(path):
                managed[field] = 'absent'
                return False
        except (OSError, RuntimeError) as exc:
            issue('managed_entry_unreadable:'+type(exc).__name__, path)
            managed[field] = 'failed'
            return False
        managed[field] = 'observed'
        return True

    memory = managed_root/'CLAUDE.md'
    if managed_entry(memory, 'memory_status'):
        seed(memory, 'claude', 'global', origin='managed', optional=False)
    enterprise = managed_root/'.claude/skills'
    result['configured_skill_roots'].append({'path': str(enterprise), 'provider': 'claude'})
    if managed_entry(enterprise, 'skills_status'):
        before = len(result['issues'])
        for path in discover_commands(enterprise, skills=True):
            seed(path, 'claude', 'on_demand', origin='managed', source='skill', discovery_root=enterprise)
        if len(result['issues']) > before:
            managed['skills_status'] = 'failed'
    elif managed['skills_status'] == 'failed':
        result['incomplete_command_roots'] = sorted(set(result['incomplete_command_roots']) | {str(enterprise)})

    # Identify configuration containers before any Markdown import expansion.
    # A whole-container alias is still configuration, not archived instructions.
    policy_containers = set()
    policy_main_paths, policy_directories = set(), set()
    for path, destinations in ((managed_root/'managed-settings.json', policy_main_paths),
                               (managed_root/'managed-settings.d', policy_directories)):
        destinations.add(os.path.abspath(path))
        try: destinations.add(str(path.resolve()))
        except (OSError, RuntimeError): pass  # Native entry checks retain the failure.
    def native_policy_path(path):
        candidate = Path(os.path.abspath(path))
        return str(candidate) in policy_main_paths or (str(candidate.parent) in policy_directories
                and not candidate.name.startswith('.') and candidate.name.endswith('.json'))
    def policy_signature(path):
        try:
            signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)
            return (signature(path.lstat()), signature(path.stat()))
        except (OSError, RuntimeError) as exc:
            return type(exc).__name__
    policy_signatures = {path: policy_signature(path) for path in
                         (managed_root/'managed-settings.json', managed_root/'managed-settings.d')}
    def read_policy(path):
        policy_signatures.setdefault(path, policy_signature(path))
        item = read(path)
        if item is not None: policy_containers.add(item['real_path'])
        return item
    result['policy_discovery'], embedded = inspect_policy(managed_root, read=read_policy, present=present,
                                                         issue=issue, max_entries=MAX_DISCOVERY_ENTRIES)

    plugin_containers, plugin_container_paths, plugin_signatures = set(), set(), {}
    plugin_container_inodes = set()
    def plugin_present(path):
        plugin_signatures.setdefault(path, policy_signature(path))
        return present(path)
    def protect_plugin_config(path):
        plugin_signatures.setdefault(path, policy_signature(path))
        plugin_container_paths.add(os.path.abspath(path))
        try:
            plugin_container_paths.add(str(path.resolve()))
            info = path.stat()
            plugin_container_inodes.add((info.st_dev, info.st_ino))
        except (OSError, RuntimeError): pass
    def read_plugin_config(path, *, required=False):
        protect_plugin_config(path)
        try:
            if not required and not plugin_present(path): return None
        except (OSError, RuntimeError) as exc:
            issue('plugin_configuration_unreadable', path, error_type=type(exc).__name__)
            return None
        item = read(path)
        if item is not None: plugin_containers.add(item['real_path'])
        return item
    def plugin_container(item, path):
        return (physical_ids.get(item['real_path']) in plugin_container_inodes
                or item['real_path'] in plugin_containers or str(path.absolute()) in plugin_container_paths
                or item['real_path'] in plugin_container_paths
                or path.name == 'plugin.json' and path.parent.name == '.claude-plugin'
                or Path(item['real_path']).name == 'plugin.json' and Path(item['real_path']).parent.name == '.claude-plugin')
    def read_plugin_body(path, *, uncertain=False):
        item = read(path)
        if item is not None and uncertain and item['real_path'] in multiply_linked:
            issue('plugin_alias_identity_unresolved', path); return None
        if item is not None and (plugin_container(item, path) or item['real_path'] in policy_containers
                                or native_policy_path(path) or native_policy_path(item['real_path'])):
            issue('plugin_configuration_not_instruction', path); return None
        return item
    result['plugin_discovery'] = inspect_plugins(cfg, root, ancestors=ancestors, read_config=read_plugin_config,
        read_body=read_plugin_body, protect_config=protect_plugin_config, present=plugin_present, discover=discover, issue=issue,
        max_entries=MAX_DISCOVERY_ENTRIES, policy=result['policy_discovery'], issues=result['issues'])
    plugin_exclusions = set(result['plugin_discovery']['plain_skill_exclusions'])
    failed_plugin_roots = {entry['path'] for entry in result['plugin_discovery']['skill_roots'] if entry['status']=='failed'}

    while queue:
        path, provider, scope, depth, chain, conditions, origin, source, discovery_root = queue.popleft()
        command = command_metadata(path.absolute(), source=source, discovery_root=Path(discovery_root).absolute()) if provider == 'claude' and source in {'skill', 'command'} else {}
        if source == 'skill' and provider == 'claude' and str(path.absolute()) in plugin_exclusions:
            continue
        item = read(path)
        if item is None:
            if origin == 'managed' and source in {'memory', 'skill'}:
                managed['memory_status' if source == 'memory' else 'skills_status'] = 'failed'
                if source == 'memory':
                    issue('managed_memory_read_failed', path)
            if command:
                result['uninspected_commands'].append({**command, 'path': str(path.absolute()), 'source': source, 'provider': provider})
            continue
        is_plugin_container = plugin_container(item, path)
        is_container = is_plugin_container or item['real_path'] in policy_containers or native_policy_path(path) or native_policy_path(item['real_path'])
        unresolved_alias = (result['policy_discovery']['selection'] == 'unknown' or result['plugin_discovery']['status'] == 'failed') and item['real_path'] in multiply_linked
        if is_container or unresolved_alias:
            issue('policy_container_not_instruction' if item['real_path'] in policy_containers or native_policy_path(path) or native_policy_path(item['real_path']) else 'plugin_configuration_not_instruction' if is_plugin_container else 'policy_alias_identity_unresolved',
                  path, container_real_path=item['real_path'])
            if origin == 'managed' and source == 'memory':
                managed['memory_status'] = 'failed'
                issue('managed_memory_read_failed', path)
            if command:
                result['uninspected_commands'].append({**command, 'path': str(path.absolute()), 'source': source, 'provider': provider})
                if origin == 'managed': managed['skills_status'] = 'failed'
            continue
        text = item['_text']
        current_scope = dict(scope)
        current_conditions = list(conditions)
        metadata, eligibility_reason = {}, ''
        scoped_surface = source in {'rule', 'skill', 'command'}
        metadata_text = text.replace('\r\n', '\n')
        if scoped_surface and metadata_text.startswith('---\n'):
            end = metadata_text.find('\n---', 4)
            if end < 0:
                issue('frontmatter_unterminated', path)
                eligibility_reason = 'frontmatter_unterminated'
            try:
                if end < 0: raise ValueError('unterminated frontmatter')
                metadata = yaml.safe_load(metadata_text[4:end]) or {}
                if not isinstance(metadata, dict): raise ValueError('frontmatter is not an object')
                globs = metadata.get('paths') if provider == 'claude' and source != 'command' else None
                if globs is not None:
                    if isinstance(globs, str): globs = [globs]
                    if not isinstance(globs, list) or any(not isinstance(g, str) or not g for g in globs):
                        raise ValueError('invalid paths')
                    current_scope = {'kind': 'on_demand' if current_scope['kind'] == 'on_demand' else 'path_scoped', 'paths': globs}
                    current_conditions.append({'path': str(path), 'paths': globs})
            except (yaml.YAMLError, ValueError, RecursionError) as exc:
                issue('frontmatter_invalid', path, error_type=type(exc).__name__)
                eligibility_reason = 'frontmatter_invalid'
                metadata = {}
        skill_name = metadata.get('name', path.parent.name) if source == 'skill' else ''
        if source == 'skill' and (not isinstance(skill_name, str) or not skill_name.strip()):
            issue('skill_metadata_invalid', path)
            eligibility_reason = 'skill_metadata_invalid'
            skill_name = ''
        if source == 'skill' and provider == 'codex':
            if any(not isinstance(metadata.get(k), str) or not metadata[k].strip() for k in ('name', 'description')):
                issue('skill_metadata_invalid', path)
                eligibility_reason = 'skill_metadata_invalid'
            if not codex['skill_configuration_valid']:
                eligibility_reason = 'codex_skill_configuration_unresolved'
            settings = [s['enabled'] for s in codex['skill_overrides'] if s['path'] == item['real_path']]
            if settings and not all(settings):
                eligibility_reason = 'skill_disabled'
        if origin == 'managed' and source == 'skill':
            if eligibility_reason:
                managed['skills_status'] = 'failed'
            elif path.parent.name.casefold() == 'synced':
                eligibility_reason = 'reserved_skill_directory'
        if source == 'skill' and provider == 'claude' and str(path.parent.parent.absolute()) in failed_plugin_roots:
            eligibility_reason = 'plugin_namespace_unresolved'
        alias = str(path.absolute())
        if alias not in item['aliases']: item['aliases'].append(alias)
        identity = (item['real_path'], provider, origin, source, str(current_scope), str(current_conditions), alias if command else '')
        if identity in visited:
            result['deduplicated'].append({'path': str(path), 'real_path': item['real_path'], 'provider': provider})
            continue
        visited.add(identity)
        item['loading_paths'].append({'provider': provider, 'scope': current_scope, 'conditions': current_conditions,
                                      'origin': origin, 'source': source,
                                      'skill_name': skill_name,
                                      **command,
                                      'import_chain': chain, 'path': alias,
                                      'eligible_prefix_bytes': 0 if eligibility_reason else item['bytes'],
                                      'eligibility_reason': eligibility_reason, 'runtime_loading_verified': False})
        # Codex AGENTS.md does not implement Claude's @-import directive.
        if provider != 'claude' or source in {'skill', 'command'} or eligibility_reason:
            continue
        imports = find_imports(text)
        if imports and depth >= MAX_IMPORT_DEPTH:
            issue('import_depth_limit', path, omitted_imports=len(imports)); continue
        for spec in imports:
            target = Path(spec).expanduser()
            if not target.is_absolute(): target = path.parent/target
            if str(target.resolve()) in chain + [item['real_path']]:
                result['deduplicated'].append({'path': str(target), 'cause': 'import_cycle'})
                continue
            queue.append((target, provider, current_scope, depth+1, chain+[item['real_path']], current_conditions, origin, 'import', None))
    for path, signature in policy_signatures.items():
        if policy_signature(path) != signature:
            raise AvailabilityError('Managed policy source changed during observation: ' + str(path))
    for path, signature in plugin_signatures.items():
        if policy_signature(path) != signature:
            raise AvailabilityError('Plugin source changed during observation: ' + str(path))
    result['files'] = [item for item in cache.values() if item['loading_paths']] + embedded
    resolve_commands(result['files'], issue, uninspected=result['uninspected_commands'], incomplete_roots=result['incomplete_command_roots'])
    codex_paths = [(item, path) for item in result['files'] for path in item['loading_paths']
                   if path['provider'] == 'codex' and path['scope']['kind'] != 'on_demand']
    codex_paths.sort(key=lambda pair: (pair[1]['scope']['kind'] != 'global', len(Path(pair[1]['path']).parts)))
    remaining = codex['max_bytes'] if codex['configuration_valid'] else 0
    for index, (item, path) in enumerate(codex_paths):
        if index: remaining = max(0, remaining - 2)
        path['eligible_prefix_bytes'] = min(item['bytes'], remaining)
        remaining = max(0, remaining - item['bytes'])
        if path['eligible_prefix_bytes'] < item['bytes']:
            issue('codex_instruction_coverage_limit' if codex['configuration_valid'] else 'codex_configuration_unresolved',
                  path['path'], omitted_bytes=item['bytes']-path['eligible_prefix_bytes'])
    return result
