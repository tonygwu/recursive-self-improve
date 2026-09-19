"""Native plugin collection uses invented installations and temporary Stores."""
from copy import deepcopy
from pathlib import Path
import json
import os
import shutil

import pytest
from self_improve import instruction_inventory as inventory, instruction_text
from self_improve import instruction_surfaces as surfaces, rule_availability as availability
from self_improve.mining_history import digest, encoded
from tests.test_instruction_managed import capture, validate
from tests.test_instruction_surfaces import write
from tests.test_rule_availability import cfg, store, known_copy, init_git_repo


def install(cfg, tmp_path, *, scope='user', project_path='', plugin_id='example@fixture', manifest=None):
    root = tmp_path/'payloads'/scope
    write(root/'.claude-plugin/plugin.json', json.dumps(manifest or {'name':'example','version':'2.0'}))
    registry = Path(cfg.global_claude_md).parent/'plugins/installed_plugins.json'
    entry = {'scope':scope,'installPath':str(root),'version':'1.0'}
    if project_path: entry['projectPath']=project_path
    write(registry,json.dumps({'version':2,'plugins':{plugin_id:[entry]}}))
    return root, registry


def test_installed_plugin_real_collection_retains_names_versions_and_text(cfg,tmp_path,monkeypatch):
    repo=init_git_repo(tmp_path/'project','README.md','Fixture.\n')
    root,registry=install(cfg,tmp_path)
    body=write(root/'skills/check/SKILL.md','---\nname: inspect\n---\nInvented plugin guidance.\n!`never-execute`\n')
    write(root/'commands/run.md','A command body.\n')
    write(root/'CLAUDE.md','Not a plugin source.\n')
    write(Path(cfg.global_claude_md).parent/'settings.json',json.dumps({'enabledPlugins':{'example@fixture':False},'env':{'SECRET':'DO_NOT_ARCHIVE'}}))
    monkeypatch.setattr('subprocess.run',lambda *a,**k:pytest.fail('Provider executed'))
    record=capture(cfg,repo);validate(record)
    assert record['profile']=='instruction-surfaces/6'
    discovery=record['plugin_discovery']; assert discovery['registry']['status']=='observed'
    plugin=discovery['instances'][0]
    assert (plugin['plugin_id'],plugin['registry_version'],plugin['manifest_version'])==('example@fixture','1.0','2.0')
    assert plugin['enablement']['value'] is False
    assert {p['command_name'] for f in record['files'] for p in f['loading_paths']}=={'example:inspect','example:run'}
    assert all(p['eligible_prefix_bytes']==0 for f in record['files'] for p in f['loading_paths'])
    assert 'DO_NOT_ARCHIVE' not in encoded(record)
    assert str(body) in encoded(record) and str(root/'CLAUDE.md') not in encoded(record)


def test_actual_store_retains_plugin_text_after_sources_are_removed(cfg,store,tmp_path):
    repo=init_git_repo(tmp_path/'project','README.md','Fixture.\n'); key=known_copy(store,repo)
    root,_=install(cfg,tmp_path); write(root/'skills/check/SKILL.md','Complete retained plugin body.\n')
    availability.collect_availability(store,cfg,observed_at='2030-01-01T00:00:00Z')
    shutil.rmtree(root)
    page=inventory.project_inventory(store,project_key=key)
    assert len(page['records'][0]['files'])==1
    assert page['records'][0]['files'][0]['loading_paths'][0]['plugin_id']=='example@fixture'
    retained=store.query('SELECT * FROM instruction_text_archives')
    assert 'Complete retained plugin body.' in encoded(retained)


def test_skills_directory_plugin_does_not_also_become_plain_skill(cfg,tmp_path):
    repo=init_git_repo(tmp_path/'project','README.md','Fixture.\n')
    root=Path(cfg.skills_dir)/'bundle'
    write(root/'.claude-plugin/plugin.json','{"name":"bundle"}')
    write(root/'SKILL.md','---\nname: root-skill\n---\nRoot body.\n')
    record=capture(cfg,repo);validate(record)
    paths=[p for f in record['files'] for p in f['loading_paths']]
    assert len(paths)==1 and paths[0]['source']=='plugin_skill'
    assert paths[0]['command_name']=='bundle:root-skill'
    assert record['plugin_discovery']['instances'][0]['plugin_id']=='bundle@skills-dir'


def test_custom_paths_replace_commands_extend_skills_and_deduplicate_aliases(cfg,tmp_path):
    repo=init_git_repo(tmp_path/'project','README.md','Fixture.\n')
    root,_=install(cfg,tmp_path,manifest={'name':'example','commands':['./extra/run.md'],'skills':['./extra/skill']})
    default=write(root/'skills/check/SKILL.md','Default skill.\n')
    custom=write(root/'extra/skill/SKILL.md','Custom skill.\n')
    write(root/'commands/ignored.md','Ignored default command.\n')
    alias=root/'extra/run.md';alias.symlink_to(default)
    record=capture(cfg,repo);validate(record)
    assert record['totals']['files']==2
    assert record['totals']['bytes']==len(default.read_bytes())+len(custom.read_bytes())
    assert {p['command_name'] for f in record['files'] for p in f['loading_paths']}=={'example:check','example:run','example:skill'}


@pytest.mark.parametrize('raw',['{','[]','{"version":3,"plugins":{}}','{"version":2,"plugins":[]}','{"version":2,"plugins":{"bad@fixture":[{"scope":"local","installPath":"relative"}]}}'])
def test_failed_registry_is_not_an_empty_catalog(cfg,tmp_path,raw):
    repo=init_git_repo(tmp_path/'project','README.md','Fixture.\n')
    write(Path(cfg.global_claude_md).parent/'plugins/installed_plugins.json',raw)
    record=capture(cfg,repo);validate(record)
    assert record['plugin_discovery']['registry']['status']=='failed'
    assert record['plugin_discovery']['status']=='failed'


def test_absent_registry_and_skills_are_recorded_zero(cfg,tmp_path):
    repo=init_git_repo(tmp_path/'project','README.md','Fixture.\n')
    record=capture(cfg,repo);validate(record)
    assert record['plugin_discovery']['registry']['status']=='absent'
    assert record['plugin_discovery']['instances']==[]
    assert record['plugin_discovery']['status']=='observed'


def test_project_installation_does_not_read_another_clone(cfg,tmp_path,monkeypatch):
    repo=init_git_repo(tmp_path/'project','README.md','Fixture.\n')
    other=tmp_path/'other-copy'; root,_=install(cfg,tmp_path,scope='project',project_path=str(other))
    write(root/'skills/check/SKILL.md','Other project body.\n')
    record=capture(cfg,repo);validate(record)
    assert not record['files']
    assert record['plugin_discovery']['instances'][0]['applicability']=='other_working_copy'


def test_configuration_alias_cannot_archive_raw_registry_or_manifest(cfg,tmp_path):
    repo=init_git_repo(tmp_path/'project','README.md','Fixture.\n')
    root,registry=install(cfg,tmp_path,manifest={'name':'example','mcpServers':{'private':{'env':{'SECRET':'DO_NOT_ARCHIVE'}}}})
    write(root/'skills/check/SKILL.md','Guidance.\n')
    write(Path(cfg.global_claude_md),f'@{root}/.claude-plugin/plugin.json\n@{registry}\n')
    record=capture(cfg,repo);validate(record)
    assert 'DO_NOT_ARCHIVE' not in encoded(record)
    assert {Path(f['path']).name for f in record['files']}=={'CLAUDE.md','SKILL.md'}


def test_rehashed_plugin_eligibility_corruption_is_rejected(cfg,tmp_path):
    repo=init_git_repo(tmp_path/'project','README.md','Fixture.\n')
    root,_=install(cfg,tmp_path);write(root/'skills/check/SKILL.md','Guidance.\n')
    record=capture(cfg,repo)
    path=record['files'][0]['loading_paths'][0];path['plugin_id']='different@fixture'
    record['id']=digest({k:v for k,v in record.items() if k!='id'})
    with pytest.raises(Exception,match='plugin'):
        validate(record)


def test_legacy_registry_converts_version_path_without_reading_legacy_payload(cfg,tmp_path):
    repo=init_git_repo(tmp_path/'project','README.md','Fixture.\n')
    native=Path(cfg.global_claude_md).parent/'plugins'
    write(native/'installed_plugins.json',json.dumps({'version':1,'plugins':{'example@fixture':{'version':'2.4','installPath':str(tmp_path/'wrong'),'installedAt':'2030-01-01'}}}))
    write(native/'cache/fixture/example/2.4/skills/check/SKILL.md','Converted path.\n')
    record=capture(cfg,repo);validate(record)
    assert record['plugin_discovery']['registry']['version']==1
    assert record['files'][0]['path'].endswith('/cache/fixture/example/2.4/skills/check/SKILL.md')


def test_project_skills_plugin_from_ancestor_stays_inapplicable(cfg,tmp_path):
    repo=init_git_repo(tmp_path/'project','README.md','Fixture.\n');cwd=repo/'subdir';cwd.mkdir()
    write(repo/'.claude/skills/bundle/.claude-plugin/plugin.json','{"name":"bundle"}')
    write(repo/'.claude/skills/bundle/SKILL.md','Ancestor root body.\n')
    record=capture(cfg,cwd);validate(record)
    assert record['files']==[]
    assert record['plugin_discovery']['instances'][0]['applicability']=='other_working_copy'


@pytest.mark.parametrize('raw',['{','{"name":null}','{"name":"bundle","skills":"../outside"}','{"name":"bundle","commands":"/outside"}','{"name":"bundle","x":1e999}','{"name":"bundle","name":"other"}'])
def test_failed_skills_plugin_manifest_cannot_enable_plain_root(cfg,tmp_path,raw):
    repo=init_git_repo(tmp_path/'project','README.md','Fixture.\n')
    root=Path(cfg.skills_dir)/'bundle';write(root/'.claude-plugin/plugin.json',raw);write(root/'SKILL.md','Do not promote.\n')
    record=capture(cfg,repo);validate(record)
    assert record['files']==[]
    assert record['plugin_discovery']['instances'][0]['status']=='failed'


@pytest.mark.parametrize('case',['hardlink','cap','bad_setting','bad_frontmatter','missing_payload'])
def test_plugin_failures_and_aliases_do_not_claim_availability(cfg,tmp_path,monkeypatch,case):
    repo=init_git_repo(tmp_path/'project','README.md','Fixture.\n')
    root,registry=install(cfg,tmp_path)
    body=write(root/'skills/check/SKILL.md','Retained body.\n')
    if case=='hardlink':
        body.unlink();os.link(root/'.claude-plugin/plugin.json',body)
    elif case=='cap':
        write(root/'skills/second/SKILL.md','Second body.\n');monkeypatch.setattr(surfaces,'MAX_DISCOVERY_ENTRIES',1)
    elif case=='bad_setting':write(Path(cfg.global_claude_md).parent/'settings.json','{"enabledPlugins":[]}')
    elif case=='bad_frontmatter':body.write_text('---\nname: [invalid]\n---\nBody')
    elif case=='missing_payload':shutil.rmtree(root)
    record=capture(cfg,repo);validate(record)
    assert record['status']=='partial'
    assert all(p['eligible_prefix_bytes']==0 for f in record['files'] for p in f['loading_paths'])


def test_replaced_plugin_settings_refuse_publication(cfg,tmp_path,monkeypatch):
    repo=init_git_repo(tmp_path/'project','README.md','Fixture.\n')
    root,_=install(cfg,tmp_path);write(root/'skills/check/SKILL.md','Body.\n')
    settings=write(Path(cfg.global_claude_md).parent/'settings.json','{}')
    original=surfaces.inspect_plugins
    def mutate(*args,**kwargs):
        result=original(*args,**kwargs);settings.write_text('{"enabledPlugins":{"example@fixture":false}}');return result
    monkeypatch.setattr(surfaces,'inspect_plugins',mutate)
    with pytest.raises(Exception,match='Plugin source changed'):
        capture(cfg,repo)


def test_invalid_plugin_child_does_not_promote_later_plugin_to_plain_skill(cfg,tmp_path):
    repo=init_git_repo(tmp_path/'project','README.md','Fixture.\n')
    for name in ('a invalid','zvalid'):
        root=Path(cfg.skills_dir)/name
        write(root/'.claude-plugin/plugin.json','{"name":"valid"}');write(root/'SKILL.md','Plugin body.\n')
    record=capture(cfg,repo);validate(record)
    assert not any(p['source']=='skill' and p['eligible_prefix_bytes'] for f in record['files'] for p in f['loading_paths'])


def test_failed_plugin_namespace_cannot_enable_queued_plain_fallback(cfg,tmp_path,monkeypatch):
    repo=init_git_repo(tmp_path/'project','README.md','Fixture.\n')
    root=Path(cfg.skills_dir)/'bundle';write(root/'.claude-plugin/plugin.json','{"name":"bundle"}');write(root/'SKILL.md','Body.\n')
    original=os.scandir; calls=0
    def scan(path):
        nonlocal calls
        if Path(path)==Path(cfg.skills_dir):
            calls+=1
            if calls==2:raise PermissionError('fixture denied second discovery')
        return original(path)
    monkeypatch.setattr(os,'scandir',scan)
    record=capture(cfg,repo);validate(record)
    assert all(p['eligible_prefix_bytes']==0 for f in record['files'] for p in f['loading_paths'])


def test_failed_plugin_settings_completeness_cannot_be_rehashed(cfg,tmp_path):
    from self_improve.instruction_plugins import enablement,reason
    repo=init_git_repo(tmp_path/'project','README.md','Fixture.\n')
    root,_=install(cfg,tmp_path);write(root/'skills/check/SKILL.md','Body.\n')
    write(Path(cfg.global_claude_md).parent/'settings.json','{"enabledPlugins":[]}')
    record=capture(cfg,repo);plugins=record['plugin_discovery'];plugins['settings_complete']=True
    for plugin in plugins['instances']:plugin['enablement']=enablement(plugin,plugins['settings'],True)
    for file in record['files']:
        for path in file['loading_paths']:path['eligibility_reason']=reason(plugins['instances'][0])
    with pytest.raises(Exception,match='plugin.*complet'):
        validate(record)


@pytest.mark.parametrize('alias',['symlink','hardlink'])
def test_other_copy_manifest_alias_does_not_enter_archive(cfg,tmp_path,alias):
    repo=init_git_repo(tmp_path/'project','README.md','Fixture.\n')
    root,_=install(cfg,tmp_path,scope='project',project_path=str(tmp_path/'other-copy'),manifest={'name':'example','env':{'secret':'UNRELATED_MANIFEST_SENTINEL'}})
    if alias=='symlink':(repo/'CLAUDE.md').symlink_to(root/'.claude-plugin/plugin.json')
    else:os.link(root/'.claude-plugin/plugin.json',repo/'CLAUDE.md')
    surface=surfaces.inspect_surfaces(cfg,repo)
    record=inventory.build_inventory(surface=surface,project_key='github:example/fixture',working_copy_path=str(repo),revisions=[],observed_at='2030-01-01T00:00:00Z')
    validate(record)
    assert 'UNRELATED_MANIFEST_SENTINEL' not in encoded(instruction_text.build_archive(surface,record))


def test_duplicate_plugin_invocation_names_are_named_coverage(cfg,tmp_path):
    repo=init_git_repo(tmp_path/'project','README.md','Fixture.\n')
    root,_=install(cfg,tmp_path)
    for folder in ('one','two'):write(root/f'skills/{folder}/SKILL.md','---\nname: same\n---\nDistinct body '+folder)
    record=capture(cfg,repo);validate(record)
    assert record['plugin_discovery']['instances'][0]['status']=='failed'
    assert any(i['cause']=='plugin_invocation_ambiguous' for i in record['issues'])


def test_denied_legacy_registry_keeps_named_failure(cfg,tmp_path,monkeypatch):
    repo=init_git_repo(tmp_path/'project','README.md','Fixture.\n')
    old=Path(cfg.global_claude_md).parent/'plugins/installed_plugins_v2.json'
    original=Path.lstat
    def deny(path,*a,**kw):
        if path==old:raise PermissionError('fixture denied')
        return original(path,*a,**kw)
    monkeypatch.setattr(Path,'lstat',deny)
    record=capture(cfg,repo);validate(record)
    assert record['plugin_discovery']['status']=='failed'


def test_repeated_plugin_roots_do_not_multiply_traversal(cfg,tmp_path,monkeypatch):
    repo=init_git_repo(tmp_path/'project','README.md','Fixture.\n')
    root,_=install(cfg,tmp_path,manifest={'name':'example','commands':['./commands']*30})
    write(root/'commands/run.md','Body.\n')
    original=os.scandir;calls=0
    def scan(path):
        nonlocal calls
        if Path(path)==root/'commands':calls+=1
        return original(path)
    monkeypatch.setattr(os,'scandir',scan)
    record=capture(cfg,repo);validate(record)
    assert calls==1


def test_unchecked_plugins_cannot_bypass_loading_path_validation(cfg,tmp_path):
    from self_improve.instruction_plugins import unchecked
    repo=init_git_repo(tmp_path/'project','README.md','Fixture.\n')
    root,_=install(cfg,tmp_path);write(root/'skills/check/SKILL.md','Body.\n')
    surface=surfaces.inspect_surfaces(cfg,repo);surface['plugin_discovery']=unchecked()
    surface['files'][0]['loading_paths'][0]['eligible_prefix_bytes']=surface['files'][0]['bytes']
    record=inventory.build_inventory(surface=surface,project_key='github:example/fixture',working_copy_path=str(repo),revisions=[],observed_at='2030-01-01T00:00:00Z')
    with pytest.raises(Exception,match='unchecked plugin'):
        validate(record)


def test_same_time_plugin_metadata_conflict_is_not_arbitrarily_selected(cfg,store,tmp_path):
    repo=init_git_repo(tmp_path/'project','README.md','Fixture.\n')
    root,_=install(cfg,tmp_path);write(root/'skills/check/SKILL.md','Body.\n')
    first=capture(cfg,repo)
    write(root/'.claude-plugin/plugin.json','{"name":"example","version":"3.0"}')
    second=capture(cfg,repo)
    with store.transaction(write=True):inventory.record_inventories(store,records=[first,second])
    page=inventory.project_inventory(store,project_key=first['project_key'])
    assert page['records'][0]['status']=='conflicting'
    assert len(page['records'][0]['observations'])==2


def test_actual_plugin_api_retains_owned_text_without_inventing_availability(cfg,store,tmp_path,monkeypatch):
    pytest.importorskip('fastapi')
    from fastapi.testclient import TestClient
    from self_improve.dashboard.app import create_app
    from self_improve.redact import redact_text
    from tests.test_rule_availability import delivery,rows
    repo=init_git_repo(tmp_path/'project','README.md','Fixture.\n');key=known_copy(store,repo)
    proposal,body=delivery(store,cfg,repo)
    root,_=install(cfg,tmp_path);write(root/'skills/check/SKILL.md',body)
    result=availability.collect_availability(store,cfg,observed_at='2030-01-01T00:00:00Z')
    assert result['outcomes']=={'unknown':1} and rows(store)[0]['matches']==[]
    inv=inventory.project_inventory(store,project_key=key)['records'][0]
    assert inv['totals']['ownership']['machine']['bytes']==len(body.encode())
    archive=store.query_one('SELECT id FROM instruction_text_archives')['id'];shutil.rmtree(root)
    monkeypatch.setattr(availability,'inspect_surfaces',lambda *a,**kw:pytest.fail('reader reopened plugin'))
    before=list(store.conn.iterdump())
    assert instruction_text.read_archive(store,archive)['files'][0]['text']==redact_text(body)
    with TestClient(create_app(cfg)) as client:
        response=client.get('/api/project-inventory',params={'project_key':key})
        assert response.status_code==200,response.text
        assert response.json()['records'][0]==inv
    assert list(store.conn.iterdump())==before
    assert store.query_one('SELECT COUNT(*) n FROM llm_calls')['n']==0


def test_plugin_publication_failure_rolls_back_inventory_and_text(cfg,store,tmp_path,monkeypatch):
    repo=init_git_repo(tmp_path/'project','README.md','Fixture.\n');known_copy(store,repo)
    root,_=install(cfg,tmp_path);write(root/'skills/check/SKILL.md','Fixture body.\n')
    before=list(store.conn.iterdump())
    def fail(*a,**kw):raise RuntimeError('fixture publication failure')
    monkeypatch.setattr(instruction_text,'record_archives',fail)
    with pytest.raises(RuntimeError,match='fixture publication failure'):
        availability.collect_availability(store,cfg,observed_at='2030-01-01T00:00:00Z')
    assert list(store.conn.iterdump())==before


@pytest.mark.parametrize('value',[None,12,'relative'])
def test_plugin_directory_override_requires_an_absolute_path(cfg,value):
    from dataclasses import replace
    from self_improve.config import ConfigError
    with pytest.raises(ConfigError,match='claude_plugins_dir'):replace(cfg,claude_plugins_dir=value)


def test_plugin_directory_override_reaches_actual_registry_read(cfg,tmp_path):
    from dataclasses import replace
    repo=init_git_repo(tmp_path/'project','README.md','Fixture.\n')
    root,registry=install(cfg,tmp_path);write(root/'skills/check/SKILL.md','Body.\n')
    relocated=tmp_path/'relocated-plugins';relocated.mkdir();registry.rename(relocated/'installed_plugins.json')
    record=capture(replace(cfg,claude_plugins_dir=str(relocated)),repo);validate(record)
    assert record['plugin_discovery']['registry']['path']==str(relocated/'installed_plugins.json')
    assert len(record['files'])==1


def test_profile_five_remains_readable_without_plugin_metadata(cfg,tmp_path):
    repo=init_git_repo(tmp_path/'project','README.md','Fixture.\n')
    record=capture(cfg,repo);record['profile']='instruction-surfaces/5';record.pop('plugin_discovery')
    validate(record)
