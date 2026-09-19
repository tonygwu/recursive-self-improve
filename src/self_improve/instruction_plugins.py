"""Observe native plugin instruction inputs without invoking the provider.

Registry and manifest records describe candidates, not effective runtime loading.
Only allowlisted discovery metadata leaves configuration reads.
"""
from itertools import islice
from pathlib import Path, PurePath
import json
import os
import re

import yaml

from .instruction_policy import _object, _constant, _float
from .mining_history import digest

PLUGIN_SOURCES = {'plugin_skill', 'plugin_command'}
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]*\Z')
PLUGIN_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]*(?:@[A-Za-z0-9][A-Za-z0-9._-]*)?\Z')


def unchecked():
    return {'status': 'not_checked', 'registry': {'path': '', 'status': 'not_checked', 'version': None, 'content_hash': ''},
            'skill_roots': [], 'settings': [], 'settings_complete': False, 'instances': [],
            'failures': [], 'plain_skill_exclusions': [], 'runtime_loading_verified': False}


def strict_object(text):
    value = json.loads(text.lstrip('\ufeff') if text.strip() else '{}', object_pairs_hook=_object,
                       parse_constant=_constant, parse_float=_float)
    if not isinstance(value, dict): raise ValueError('expected JSON object')
    return value


def instance_id(item):
    return digest([item['plugin_id'], item['scope'], item['project_path'], item['root']])


def enablement(plugin, settings, complete):
    matches = [s for source in settings for s in source['values'] if s['plugin_id'] == plugin['plugin_id']]
    if not complete: return {'state': 'unknown', 'value': None}
    if matches: return {'state': 'explicit', 'value': matches[-1]['value']}
    return {'state': 'manifest_default', 'value': plugin['default_enabled']}


def reason(plugin):
    if plugin['applicability'] != 'matching': return 'plugin_other_working_copy'
    if plugin['enablement']['value'] is False: return 'plugin_locally_disabled'
    if plugin['status'] == 'failed': return 'plugin_inspection_incomplete'
    return 'plugin_runtime_selection_unverified'


def inspect_plugins(cfg, working_copy, *, ancestors, read_config, read_body, protect_config, present, discover,
                    issue, max_entries, policy, issues):
    """Collect through shared bounded readers; mutate only their in-memory files."""
    result = unchecked(); result['status'] = 'observed'
    native = Path(cfg.claude_plugins_dir) if cfg.claude_plugins_dir else Path(cfg.global_claude_md).parent/'plugins'
    registry = native/'installed_plugins.json'
    result['registry'].update(path=str(registry), status='absent')
    candidates = []

    def failure(path, cause, **extra):
        entry = {'path': str(path), 'cause': 'plugin_'+cause, **extra}
        result['failures'].append(entry); result['status'] = 'failed'
        issue(entry['cause'], path, **extra)

    def exists(path):
        try: return present(path)
        except (OSError, RuntimeError) as exc:
            failure(path, 'entry_unreadable', error_type=type(exc).__name__)
            return None

    def config(path, *, required=False):
        # Even absent/failed lexical paths must be protected from Markdown aliases.
        item = read_config(path, required=required)
        if item is None: return None
        try: return strict_object(item['_text']), item['content_hash']
        except (ValueError, RecursionError, UnicodeError) as exc:
            failure(path, 'configuration_invalid', error_type=type(exc).__name__)
            return None

    def entry(plugin_id, scope, project, root, version, source, order):
        if (not isinstance(plugin_id,str) or not PLUGIN_ID.fullmatch(plugin_id)
                or scope not in {'managed','user','project','local'} or not isinstance(root,str)
                or not Path(root).is_absolute() or not isinstance(project,str)
                or scope in {'project','local'} and not Path(project).is_absolute()
                or not isinstance(version,str)):
            raise ValueError('invalid plugin installation')
        normalized = str(Path(project).resolve()) if project else ''
        item = {'plugin_id':plugin_id, 'scope':scope, 'project_path':project, 'project_real_path':normalized,
                'root':str(Path(root).absolute()), 'registry_version':version, 'source':source, 'order':order,
                'applicability':'matching' if scope in {'user','managed'} or normalized == str(working_copy) else 'other_working_copy',
                'manifest_path':str(Path(root)/'.claude-plugin/plugin.json'), 'manifest_hash':'',
                'name':plugin_id.split('@')[0], 'manifest_version':'', 'default_enabled':True,
                'skills':None, 'commands':None, 'status':'not_checked', 'components':[]}
        item['id']=instance_id(item)
        return item

    registry_exists = exists(registry)
    if registry_exists is not False:
        result['registry']['status'] = 'failed'
        loaded = config(registry, required=True) if registry_exists else None
        if loaded:
            value, source_hash = loaded
            result['registry']['content_hash'] = source_hash
            try:
                version = value.get('version', 1)
                if type(version) is not int or version not in {1,2} or not isinstance(value.get('plugins'),dict):
                    raise ValueError('unsupported plugin registry')
                result['registry']['version'] = version
                for plugin_id, installs in value['plugins'].items():
                    installs = [installs] if version == 1 else installs
                    if not isinstance(installs,list): raise ValueError('invalid plugin installation array')
                    for order, install in enumerate(installs):
                        if not isinstance(install,dict): raise ValueError('invalid plugin installation')
                        if version == 1:
                            v = install['version']
                            if not isinstance(v,str): raise ValueError('invalid plugin version')
                            name, _, marketplace = plugin_id.partition('@')
                            safe = lambda s: re.sub('[^a-zA-Z0-9_-]', '-', s)
                            safe_version = re.sub('[^a-zA-Z0-9._-]', '-', v)
                            if safe_version in {'.','..'}: safe_version='-'
                            root = str(native/'cache'/safe(marketplace or 'unknown')/safe(name)/safe_version)
                            candidates.append(entry(plugin_id,'user','',root,v,'registry',order))
                        else:
                            candidates.append(entry(plugin_id,install['scope'],install.get('projectPath',''),
                                                    install['installPath'],install.get('version',''),'registry',order))
                if len(candidates)>max_entries: raise ValueError('plugin registry entry limit')
                if len({p['id'] for p in candidates})!=len(candidates): raise ValueError('duplicate plugin instance')
                result['registry']['status'] = 'observed'
            except (KeyError, TypeError, ValueError, OSError, RuntimeError) as exc:
                candidates.clear(); failure(registry,'registry_invalid',error_type=type(exc).__name__)
        else: failure(registry,'registry_unreadable')
    read_config(native/'installed_plugins_v2.json',required=False)
    if exists(native/'installed_plugins_v2.json') is not False:
        failure(native/'installed_plugins_v2.json','legacy_registry_pending')

    # Manifest children are plugins even when their root SKILL.md is present.
    roots = [(Path(cfg.skills_dir),'user'), *((directory/'.claude/skills','project') for directory in ancestors)]
    seen_roots = set()
    for base, scope in roots:
        if str(base) in seen_roots: continue
        seen_roots.add(str(base)); state = {'path':str(base),'scope':scope,'status':'absent'}
        result['skill_roots'].append(state)
        if exists(base) is False: continue
        state['status']='observed'
        try:
            with os.scandir(base) as entries: batch=list(islice(entries,max_entries+1))
            if len(batch)>max_entries:
                raise ValueError('plugin directory entry limit')
            for child in sorted(batch,key=lambda e:e.name):
                root=Path(child.path); manifest=root/'.claude-plugin/plugin.json'
                if not child.is_dir(follow_symlinks=True): continue
                if exists(manifest) is False: continue
                result['plain_skill_exclusions'].append(str(root/'SKILL.md'))
                try:
                    candidates.append(entry(child.name+'@skills-dir',scope,str(base.parent.parent) if scope=='project' else '',
                                            str(root),'','skills-dir',len(candidates)))
                except (ValueError, OSError, RuntimeError) as exc:
                    state['status']='failed';failure(root,'skills_plugin_invalid',error_type=type(exc).__name__)
        except (OSError, ValueError, RuntimeError) as exc:
            state['status']='failed';failure(base,'skills_directory_failed',error_type=type(exc).__name__)

    settings_paths = [(Path(cfg.global_claude_md).parent/'settings.json','user'),
                      (ancestors[0]/'.claude/settings.json','project'),
                      (ancestors[0]/'.claude/settings.local.json','local')]
    settings_paths += [(Path(s['path']),'managed') for s in policy['sources']]
    result['settings_complete'] = policy['selection'] not in {'unknown','not_checked'}
    for path, scope in settings_paths:
        setting={'path':str(path),'scope':scope,'status':'absent','content_hash':'','values':[]}
        result['settings'].append(setting)
        if exists(path) is False:
            read_config(path,required=False)
            continue
        loaded=config(path,required=True)
        try:
            if loaded is None: raise ValueError('unreadable plugin settings')
            values, setting['content_hash']=loaded
            mapping=values.get('enabledPlugins',{})
            if not isinstance(mapping,dict): raise ValueError('invalid enabledPlugins')
            for name,value in sorted(mapping.items()):
                if not PLUGIN_ID.fullmatch(name) or not (type(value) is bool or isinstance(value,str) and value):
                    raise ValueError('invalid plugin enablement')
                setting['values'].append({'plugin_id':name,'value':value})
            setting['status']='observed'
        except (TypeError,ValueError) as exc:
            setting['status']='failed'; setting['values']=[]; result['settings_complete']=False
            failure(path,'settings_invalid',error_type=type(exc).__name__)

    # Read every applicable manifest before any body can alias a configuration.
    for plugin in candidates:
        result['instances'].append(plugin)
        protect_config(Path(plugin['manifest_path']))
        if plugin['applicability'] != 'matching':
            plugin['enablement']=enablement(plugin,result['settings'],result['settings_complete']);continue
        manifest=Path(plugin['manifest_path']); plugin['status']='observed'
        if exists(manifest) is not False:
            loaded=config(manifest,required=True)
            try:
                if loaded is None: raise ValueError('unreadable manifest')
                values,plugin['manifest_hash']=loaded
                name=values.get('name')
                if not isinstance(name,str) or not NAME.fullmatch(name): raise ValueError('invalid manifest name')
                plugin['name']=name
                for key,out in (('version','manifest_version'),('defaultEnabled','default_enabled')):
                    if key in values:
                        if type(values[key]) is not type(plugin[out]): raise ValueError('invalid manifest field')
                        plugin[out]=values[key]
                for key in ('skills','commands'):
                    if key in values:
                        paths=values[key] if isinstance(values[key],list) else [values[key]]
                        if any(not isinstance(p,str) or not (p.startswith('./') or key=='skills' and p=='.')
                               or '..' in PurePath(p).parts or '\\' in p or '\x00' in p for p in paths):
                            raise ValueError('invalid plugin component path')
                        paths=list(dict.fromkeys(paths))
                        if len(paths)>max_entries: raise ValueError('plugin component root limit')
                        plugin[key]=paths
            except (TypeError,ValueError) as exc:
                plugin['status']='failed';failure(manifest,'manifest_invalid',error_type=type(exc).__name__)
        else:
            read_config(manifest,required=False)
            if plugin['source']=='skills-dir':
                plugin['status']='failed';failure(manifest,'manifest_missing')
        plugin['enablement']=enablement(plugin,result['settings'],result['settings_complete'])

    for plugin in candidates:
        if plugin['applicability']!='matching' or plugin['status']=='failed': continue
        root=Path(plugin['root'])
        if str(root).endswith('.zip') or not root.is_dir():
            plugin['status']='failed';failure(root,'payload_unavailable');continue
        before_issues=len(issues)
        pending=[]; default=root/'skills'
        if exists(default): pending.extend((p,'plugin_skill') for p in discover(default,skills=True))
        elif plugin['skills'] is None and exists(root/'SKILL.md'):
            pending.append((root/'SKILL.md','plugin_skill'))
        for relative in plugin['skills'] or []:
            base=root/relative
            if exists(base/'SKILL.md'): pending.append((base/'SKILL.md','plugin_skill'))
            else:
                if exists(base) is False:
                    plugin['status']='failed';failure(base,'component_missing')
                pending.extend((p,'plugin_skill') for p in discover(base,skills=True))
        for relative in plugin['commands'] if plugin['commands'] is not None else ['./commands']:
            base=root/relative
            if base.suffix=='.md': pending.append((base,'plugin_command'))
            else:
                if plugin['commands'] is not None and exists(base) is False:
                    plugin['status']='failed';failure(base,'component_missing')
                pending.extend((p,'plugin_command') for p in discover(base,preserve_alias_names=True))
        if len(issues)>before_issues:
            plugin['status']='failed';failure(root,'component_discovery_incomplete')
        seen=set()
        for path,kind in pending:
            identity=(str(path),kind)
            if identity in seen: continue
            seen.add(identity)
            file=read_body(path, uncertain=bool(result['failures']))
            if file is None:
                plugin['status']='failed';failure(path,'instruction_unreadable');continue
            name=path.parent.name if kind=='plugin_skill' else path.stem
            status='observed'
            try:
                text=file['_text'].replace('\r\n','\n')
                if text.startswith('---\n'):
                    end=text.find('\n---',4)
                    if end<0: raise ValueError('unterminated plugin frontmatter')
                    metadata=yaml.safe_load(text[4:end]) or {}
                    if not isinstance(metadata,dict): raise ValueError('invalid plugin frontmatter')
                    name=metadata.get('name',name)
                if not isinstance(name,str) or not NAME.fullmatch(name): raise ValueError('invalid invocation name')
            except (ValueError,yaml.YAMLError,RecursionError):
                name='';status='failed';plugin['status']='failed';failure(path,'frontmatter_invalid')
            component={'path':str(path),'source':kind,'name':name,'status':status,'real_path':file['real_path']}
            plugin['components'].append(component)
            alias=str(path.absolute())
            if alias not in file['aliases']:file['aliases'].append(alias)
            file['loading_paths'].append({'path':alias,'provider':'claude','origin':{'user':'global','managed':'managed','local':'project','project':'project'}[plugin['scope']],
                'source':kind,'scope':{'kind':'on_demand','paths':[]},'conditions':[],'import_chain':[],
                'skill_name':name if kind=='plugin_skill' else '', 'command_name':plugin['name']+':'+name if name else '',
                'plugin_id':plugin['plugin_id'],'plugin_instance_id':plugin['id'],
                'eligible_prefix_bytes':0,'eligibility_reason':reason(plugin),'runtime_loading_verified':False})
    for plugin in candidates:
        names={}
        for component in plugin['components']:
            if component['name']: names.setdefault(component['name'],set()).add(component['real_path'])
        if any(len(paths)>1 for paths in names.values()):
            plugin['status']='failed';failure(plugin['root'],'invocation_ambiguous')
    # A later bad component must not leave earlier paths claiming complete input.
    by_id={p['id']:p for p in candidates}
    for plugin in candidates:
        for component in plugin['components']:
            file=read_body(Path(component['path']), uncertain=False)
            if file:
                for path in file['loading_paths']:
                    if path.get('plugin_instance_id') in by_id:path['eligibility_reason']=reason(by_id[path['plugin_instance_id']])
    result['plain_skill_exclusions']=sorted(set(result['plain_skill_exclusions']))
    return result


def validate_plugins(record):
    """Reconcile immutable plugin metadata and file references without I/O."""
    data=record['plugin_discovery']
    if data['runtime_loading_verified'] is not False or data['status'] not in {'not_checked','observed','failed'}:
        raise ValueError('invalid plugin observation')
    if data['status']=='not_checked':
        if data != unchecked() or any(p['source'] in PLUGIN_SOURCES for f in record['files'] for p in f['loading_paths']):
            raise ValueError('invalid unchecked plugins')
        return
    if data['status']!=('failed' if data['failures'] else 'observed'):
        raise ValueError('plugin failure coverage differs')
    if any(f not in record['issues'] for f in data['failures']):raise ValueError('plugin failures missing from coverage')
    if data['registry']['status'] not in {'absent','observed','failed'}:raise ValueError('invalid plugin registry status')
    complete = record['policy_discovery']['selection'] not in {'unknown','not_checked'} and all(s['status'] != 'failed' for s in data['settings'])
    if data['settings_complete'] is not complete:raise ValueError('plugin settings completeness differs')
    for settings in data['settings']:
        if settings['status'] not in {'absent','observed','failed'}:raise ValueError('invalid plugin settings status')
        if settings['status']!='observed' and settings['values']:raise ValueError('unobserved plugin settings values')
        for value in settings['values']:
            if not PLUGIN_ID.fullmatch(value['plugin_id']) or not (type(value['value']) is bool or isinstance(value['value'],str) and value['value']):
                raise ValueError('invalid plugin enablement input')
    instances={}
    expected=[]
    for plugin in data['instances']:
        if (plugin['id']!=instance_id(plugin) or plugin['id'] in instances or not PLUGIN_ID.fullmatch(plugin['plugin_id'])
                or plugin['scope'] not in {'user','managed','project','local'} or not NAME.fullmatch(plugin['name'])
                or plugin['manifest_path']!=str(PurePath(plugin['root'])/'.claude-plugin/plugin.json')
                or plugin['enablement']!=enablement(plugin,data['settings'],data['settings_complete'])):
            raise ValueError('invalid plugin instance identity or enablement')
        if plugin['applicability'] != ('matching' if plugin['scope'] in {'user','managed'} or plugin['project_real_path']==record['working_copy']['normalized_path'] else 'other_working_copy'):
            raise ValueError('plugin working-copy applicability differs')
        if plugin['applicability']!='matching' and plugin['components']:raise ValueError('plugin belongs to another working copy')
        instances[plugin['id']]=plugin
        for component in plugin['components']:
            if component['source'] not in PLUGIN_SOURCES or not PurePath(component['path']).is_relative_to(plugin['root']) or '..' in PurePath(component['path']).parts:
                raise ValueError('plugin component path escapes root')
            expected.append((plugin['id'],component['path'],component['source'],component['real_path']))
    actual=[]
    for file in record['files']:
        for path in file['loading_paths']:
            if path['source'] not in PLUGIN_SOURCES:continue
            plugin=instances.get(path['plugin_instance_id'])
            if not plugin:raise ValueError('missing plugin instance')
            component=next((c for c in plugin['components'] if c['path']==path['path'] and c['source']==path['source']),None)
            if (component is None or path['plugin_id']!=plugin['plugin_id'] or path['provider']!='claude'
                    or path['origin']!={'user':'global','managed':'managed','local':'project','project':'project'}[plugin['scope']]
                    or path['eligible_prefix_bytes']!=0 or path['eligibility_reason']!=reason(plugin)
                    or path['scope']!={'kind':'on_demand','paths':[]} or path['conditions'] or path['import_chain']
                    or path['command_name']!=(plugin['name']+':'+component['name'] if component['name'] else '')):
                raise ValueError('invalid plugin loading path')
            actual.append((plugin['id'],path['path'],path['source'],file['real_path']))
    if sorted(actual)!=sorted(expected) or len(set(actual))!=len(actual):raise ValueError('plugin components differ from inventory')
