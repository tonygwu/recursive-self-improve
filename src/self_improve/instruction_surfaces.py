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
from .rule_revisions import marked_units, text_hash

MAX_FILE_BYTES = 1 << 20
MAX_FILES = 2000
MAX_IMPORT_DEPTH = 5
MAX_DISCOVERY_ENTRIES = 10000
PROFILE = 'instruction-surfaces/2'


def inspect_surfaces(cfg, working_copy_path, *, global_targets=()):
    """Return one physical file plus all provider/loading paths that reach it.

    Inspect configured global roots even without retained delivered revisions.
    global_targets only makes missing configured targets explicit; it cannot
    turn an arbitrary absolute file into an instruction source.
    """
    root = Path(working_copy_path).resolve()
    result = {'files': [], 'issues': [], 'deduplicated': [], 'profile': PROFILE,
              'limits': {'max_file_bytes': MAX_FILE_BYTES, 'max_files': MAX_FILES, 'max_import_depth': MAX_IMPORT_DEPTH,
                         'max_discovery_entries_per_root': MAX_DISCOVERY_ENTRIES},
              'runtime_loading_verified': False,
              'unobserved_sources': ['managed_policy', 'runtime_plugins', 'additional_directories',
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
    cache, visited = {}, set()

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
            if len(cache) >= MAX_FILES:
                issue('file_count_limit', path, limit=MAX_FILES, omitted_at_least=1); return None
            with path.open('rb') as handle:
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
        cache[real] = item
        return item

    def seed(path, provider, mode, *, origin='project', source='memory', optional=True):
        if optional and not os.path.lexists(path):
            return
        queue.append((path, provider, {'kind': mode, 'paths': []}, 0, [], [], origin, source))

    def discover(base, *, skills=False):
        if not os.path.lexists(base): return []
        pending, seen, found, visited_entries = [base], set(), [], 0
        while pending:
            directory = pending.pop()
            try:
                real = str(directory.resolve(strict=True))
                if real in seen:
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
                            if os.path.lexists(candidate): found.append(candidate)
                        else:
                            pending.append(path)
                    elif not skills and path.suffix == '.md':
                        found.append(path)
                    if len(found) > MAX_FILES:
                        issue('discovery_file_limit', base, omitted_at_least=1, limit=MAX_FILES)
                        return sorted(found[:MAX_FILES])
            except (OSError, RuntimeError) as exc:
                issue('directory_unreadable:'+type(exc).__name__, directory)
        return sorted(found)

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
            for path in discover(base, skills=True):
                seed(path, 'claude' if base.parent.name == '.claude' else 'codex', 'on_demand', source='skill')

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
        for path in discover(base, skills=True):
            seed(path, provider, 'on_demand', origin='global', source='skill')
    for raw in sorted(selected):
        path = Path(raw)
        for base, provider in skill_roots:
            if path.is_relative_to(base.resolve()) and path.name == 'SKILL.md' and path.parent.parent == base.resolve():
                seed(path, provider, 'on_demand', origin='global', source='skill', optional=False)

    while queue:
        path, provider, scope, depth, chain, conditions, origin, source = queue.popleft()
        item = read(path)
        if item is None:
            continue
        text = item['_text']
        current_scope = dict(scope)
        current_conditions = list(conditions)
        metadata, eligibility_reason = {}, ''
        scoped_surface = source in {'rule', 'skill'}
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
                globs = metadata.get('paths') if provider == 'claude' else None
                if globs is not None:
                    if isinstance(globs, str): globs = [globs]
                    if not isinstance(globs, list) or any(not isinstance(g, str) or not g for g in globs):
                        raise ValueError('invalid paths')
                    current_scope = {'kind': 'on_demand' if current_scope['kind'] == 'on_demand' else 'path_scoped', 'paths': globs}
                    current_conditions.append({'path': str(path), 'paths': globs})
            except (yaml.YAMLError, ValueError) as exc:
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
        alias = str(path.absolute())
        if alias not in item['aliases']: item['aliases'].append(alias)
        identity = (item['real_path'], provider, origin, source, str(current_scope), str(current_conditions))
        if identity in visited:
            result['deduplicated'].append({'path': str(path), 'real_path': item['real_path'], 'provider': provider})
            continue
        visited.add(identity)
        item['loading_paths'].append({'provider': provider, 'scope': current_scope, 'conditions': current_conditions,
                                      'origin': origin, 'source': source,
                                      'skill_name': skill_name,
                                      'import_chain': chain, 'path': alias,
                                      'eligible_prefix_bytes': 0 if eligibility_reason else item['bytes'],
                                      'eligibility_reason': eligibility_reason, 'runtime_loading_verified': False})
        # Codex AGENTS.md does not implement Claude's @-import directive.
        if provider != 'claude' or source == 'skill' or eligibility_reason:
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
            queue.append((target, provider, current_scope, depth+1, chain+[item['real_path']], current_conditions, origin, 'import'))
    result['files'] = [item for item in cache.values() if item['loading_paths']]
    # Claude's runtime may shadow same-name skills at different scopes. Until
    # that precedence is observed, neither body can establish availability.
    skill_names = {}
    for item in result['files']:
        for path in item['loading_paths']:
            if path['provider'] == 'claude' and path['source'] == 'skill' and path['skill_name']:
                skill_names.setdefault(path['skill_name'], []).append((item, path))
    for paths in skill_names.values():
        if len({item['real_path'] for item, path in paths}) > 1:
            for item, path in paths:
                path.update(eligible_prefix_bytes=0, eligibility_reason='skill_name_precedence_unresolved')
                issue('skill_name_precedence_unresolved', path['path'])
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
