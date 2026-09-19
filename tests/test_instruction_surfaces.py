"""Configured global sources reach the actual collector without private state."""
from pathlib import Path
from dataclasses import replace

from self_improve.instruction_surfaces import inspect_surfaces
from self_improve import instruction_inventory as inventory, rule_availability as availability
from tests.test_rule_availability import cfg, store, known_copy, init_git_repo


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_untracked_global_skills_rules_and_override_only_are_collected(cfg, store, tmp_path):
    repo = init_git_repo(tmp_path/'project', 'README.md', '# Fixture\n')
    key = known_copy(store, repo)
    claude = write(Path(cfg.skills_dir)/'example/SKILL.md', '---\nname: example\ndescription: An invented skill.\n---\nSkill body.\n')
    codex = write(Path(cfg.codex_global_agents_md).parent.parent/'.agents/skills/example/SKILL.md',
                  '---\nname: example\ndescription: An invented skill.\n---\nCodex body.\n')
    rule = write(Path(cfg.global_claude_md).parent/'rules/nested/scoped.md', '---\npaths: ["src/**"]\n---\nGlobal scoped rule.\n')
    override = write(Path(cfg.codex_global_agents_md).with_name('AGENTS.override.md'), '# Global override\n')
    availability.collect_availability(store, cfg, observed_at='2030-01-01T00:00:00Z')
    record = inventory.project_inventory(store, project_key=key)['records'][0]
    assert {f['real_path'] for f in record['files']} == {str(p) for p in (claude, codex, rule, override)}
    assert record['profile'] == 'instruction-surfaces/6'
    assert all(p['origin'] == 'global' for f in record['files'] for p in f['loading_paths'])
    assert next(f for f in record['files'] if f['path'] == str(rule))['loading_paths'][0]['scope']['kind'] == 'path_scoped'
    assert record['totals']['ownership']['machine']['bytes'] == 0


def test_global_memory_import_origin_and_skill_support_files_are_not_startup_context(cfg, tmp_path):
    repo = init_git_repo(tmp_path/'project', 'README.md', '# Fixture\n')
    global_file = write(Path(cfg.global_claude_md), '@shared.md\n')
    shared = write(global_file.parent/'shared.md', 'Global imported instruction.\n')
    skill = write(Path(cfg.skills_dir)/'example/SKILL.md', '---\nname: example\n---\n@support.md\n')
    support = write(skill.parent/'support.md', 'Read only when requested.\n')
    result = inspect_surfaces(cfg, repo)
    files = {f['path']: f for f in result['files']}
    assert str(support) not in files
    assert files[str(shared)]['loading_paths'][0]['origin'] == 'global'
    assert files[str(shared)]['loading_paths'][0]['import_chain'] == [str(global_file)]
    assert files[str(skill)]['loading_paths'][0]['scope']['kind'] == 'on_demand'


def test_symlinked_rule_directories_and_skills_are_bounded_and_deduplicated(cfg, tmp_path):
    repo = init_git_repo(tmp_path/'project', 'README.md', '# Fixture\n')
    external = write(tmp_path/'shared-rules/nested/rule.md', 'Shared fixture.\n')
    (external.parent/'loop').symlink_to(external.parent.parent, target_is_directory=True)
    rules = repo/'.claude/rules'; rules.mkdir(parents=True)
    (rules/'linked').symlink_to(external.parent.parent, target_is_directory=True)
    skill = write(tmp_path/'shared-skill/SKILL.md', '---\nname: linked\n---\nSkill.\n')
    skills = Path(cfg.skills_dir); skills.mkdir(parents=True)
    (skills/'linked').symlink_to(skill.parent, target_is_directory=True)
    result = inspect_surfaces(cfg, repo)
    assert {f['real_path'] for f in result['files']} == {str(external), str(skill)}
    assert result['deduplicated']


def test_disabled_and_invalid_codex_skills_retain_bytes_without_eligibility(cfg, tmp_path):
    repo = init_git_repo(tmp_path/'project', 'README.md', '# Fixture\n')
    skills = Path(cfg.codex_global_agents_md).parent.parent/'.agents/skills'
    disabled = write(skills/'disabled/SKILL.md', '---\nname: disabled\ndescription: Invented.\n---\nBody.\n')
    invalid = write(skills/'invalid/SKILL.md', '---\nname: invalid\n---\nMissing required description.\n')
    write(Path(cfg.codex_global_agents_md).parent/'config.toml',
          f'[[skills.config]]\npath = "{disabled}"\nenabled = false\n')
    result = inspect_surfaces(cfg, repo)
    files = {f['real_path']: f for f in result['files']}
    assert files[str(disabled)]['loading_paths'][0]['eligible_prefix_bytes'] == 0
    assert files[str(disabled)]['loading_paths'][0]['eligibility_reason'] == 'skill_disabled'
    assert files[str(invalid)]['loading_paths'][0]['eligible_prefix_bytes'] == 0
    assert any(i['cause'] == 'skill_metadata_invalid' for i in result['issues'])


def test_context_partition_and_aliases_reach_retained_api_without_rereading(cfg, store, tmp_path, monkeypatch):
    import pytest
    pytest.importorskip('fastapi')
    from fastapi.testclient import TestClient
    from self_improve.dashboard.app import create_app
    repo = init_git_repo(tmp_path/'project', 'README.md', '# Fixture\n')
    key = known_copy(store, repo)
    skill = write(Path(cfg.skills_dir)/'example/SKILL.md', '---\nname: example\n---\nAn invented café skill.\n')
    memory = write(repo/'CLAUDE.md', '@'+str(skill)+'\n')
    scoped = write(repo/'.claude/rules/conditional.md', '---\npaths: ["src/**"]\n---\nConditional.\n')
    (repo/'AGENTS.md').symlink_to(memory)
    availability.collect_availability(store, cfg, observed_at='2030-01-01T00:00:00Z')
    record = inventory.project_inventory(store, project_key=key)['records'][0]
    groups = {(g['provider'],g['origin']):g for g in record['context']['groups']}
    claude = groups['claude','all']
    assert claude['startup_bytes'] == len(memory.read_bytes())+len(skill.read_bytes())
    assert claude['conditional_bytes'] == len(scoped.read_bytes())
    assert claude['on_demand_bytes'] == 0  # Imported skill body already counted.
    assert groups['claude','global']['on_demand_bytes'] == len(skill.read_bytes())
    assert claude['observed_bytes'] == record['totals']['bytes']
    assert groups['codex','all']['startup_bytes'] == len(memory.read_bytes())
    memory.write_text('Changed after observation.\n')
    before = store.query_one('SELECT COUNT(*) n FROM instruction_inventories')['n']
    def forbidden(*args, **kwargs): raise AssertionError('Reader attempted collection')
    monkeypatch.setattr(availability, 'inspect_surfaces', forbidden)
    from self_improve.dashboard import app as dashboard_app
    monkeypatch.setattr(dashboard_app, '_context_weigher', forbidden)
    with TestClient(create_app(cfg)) as client:
        rows = client.get('/api/projects?other_md=1').json()['rows']
        weight = next(row for row in rows if row['project_key'] == key)['context_weight']
        assert weight['computable'] and weight['total_bytes'] == record['totals']['bytes']
        assert weight['groups'] == record['context']['groups']
        assert weight['inventory_id'] == record['id']
    assert store.query_one('SELECT COUNT(*) n FROM instruction_inventories')['n'] == before


def test_context_tampering_is_named_even_with_recomputed_outer_hash(cfg, store, tmp_path):
    import pytest
    from self_improve.mining_history import digest, encoded
    repo = init_git_repo(tmp_path/'project', 'AGENTS.md', '# Fixture\n')
    key = known_copy(store, repo)
    availability.collect_availability(store, cfg, observed_at='2030-01-01T00:00:00Z')
    record = inventory.project_inventory(store, project_key=key)['records'][0]
    old = record['id']; record['context']['groups'][0]['startup_bytes'] += 1
    record['id'] = digest({k:v for k,v in record.items() if k != 'id'})
    store.update('instruction_inventories', 'id', old, {'id':record['id'], 'record_json':encoded(record), 'record_hash':digest(record)})
    store.commit()
    with pytest.raises(inventory.InventoryError, match=record['id']+'.*context totals'):
        inventory.project_inventory(store, project_key=key)


def test_configured_codex_root_crlf_metadata_and_broken_skill_link(cfg, tmp_path):
    repo = init_git_repo(tmp_path/'project', 'README.md', '# Fixture\n')
    cfg = replace(cfg, codex_skills_dir=str(tmp_path/'explicit-skills'))
    skill = write(Path(cfg.codex_skills_dir)/'fixture/SKILL.md', '---\nname: fixture\ndescription: Invented.\n---\nBody.\n')
    skill.write_bytes(skill.read_bytes().replace(b'\n', b'\r\n'))
    (Path(cfg.codex_skills_dir)/'broken').symlink_to(tmp_path/'missing', target_is_directory=True)
    result = inspect_surfaces(cfg, repo)
    assert result['files'][0]['loading_paths'][0]['eligible_prefix_bytes'] == len(skill.read_bytes())
    assert any(i['cause'] == 'directory_entry_unresolved' for i in result['issues'])


def test_discovery_limit_is_named_and_old_profiles_remain_readable(cfg, store, tmp_path, monkeypatch):
    from self_improve import instruction_surfaces as surfaces
    from self_improve.instruction_context import project_summary
    repo = init_git_repo(tmp_path/'project', 'README.md', '# Fixture\n')
    key = known_copy(store, repo)
    for i in range(4): write(Path(cfg.skills_dir)/str(i)/'SKILL.md', '---\nname: fixture\n---\nBody.\n')
    monkeypatch.setattr(surfaces, 'MAX_DISCOVERY_ENTRIES', 2)
    limited = inspect_surfaces(cfg, repo)
    assert any(i['cause'] == 'discovery_entry_limit' for i in limited['issues'])
    write(repo/'AGENTS.md', '# Retained old profile\n')
    old = inspect_surfaces(cfg, repo); old['profile'] = 'instruction-surfaces/1'
    for file in old['files']:
        for path in file['loading_paths']:
            for field in ('origin','source','eligibility_reason'): path.pop(field)
    record = inventory.build_inventory(surface=old, project_key=key, working_copy_path=str(repo),
                                       revisions=[], observed_at='2030-01-01T00:00:00Z')
    with store.transaction(write=True): inventory.record_inventories(store, records=[record])
    assert inventory.project_inventory(store, project_key=key)['records'][0] == record
    assert project_summary(store, project_key=key)['reason'] == 'source_profile_upgrade_required'


def test_claude_skill_name_collision_does_not_invent_active_body(cfg, tmp_path):
    repo = init_git_repo(tmp_path/'project', 'README.md', '# Fixture\n')
    text = '---\nname: same-name\n---\nInvented body.\n'
    write(Path(cfg.skills_dir)/'same-command/SKILL.md', text)
    write(repo/'.claude/skills/same-command/SKILL.md', text)
    result = inspect_surfaces(cfg, repo)
    assert len(result['files']) == 2
    assert all(p['eligible_prefix_bytes'] == 0 and p['eligibility_reason'] == 'command_name_precedence_unresolved'
               for file in result['files'] for p in file['loading_paths'])


def test_multiple_projects_validate_shared_revision_archive_once(cfg, store, tmp_path, monkeypatch):
    from self_improve.dashboard.queries import projects
    from self_improve import rule_revisions
    for index in range(3):
        repo = init_git_repo(tmp_path/str(index), 'AGENTS.md', '# Fixture\n')
        known_copy(store, repo)
    availability.collect_availability(store, cfg, observed_at='2030-01-01T00:00:00Z')
    calls = []
    original = rule_revisions.retained_revisions
    def counted(reader):
        calls.append(reader)
        return original(reader)
    monkeypatch.setattr(rule_revisions, 'retained_revisions', counted)
    def forbidden(*args): raise AssertionError('Per-project archive validation')
    monkeypatch.setattr(inventory, 'retained_revisions', forbidden)
    with store.transaction(): result = projects(store)
    assert len(result['rows']) == 3
    assert all(row['context_weight']['computable'] for row in result['rows'])
    assert calls == [store]


def test_failed_empty_inspection_is_unknown_but_observed_empty_is_zero(cfg, store, tmp_path):
    from self_improve.instruction_context import project_summary
    repo = init_git_repo(tmp_path/'project', 'README.md', '# Fixture\n')
    key = known_copy(store, repo)
    availability.collect_availability(store, cfg, observed_at='2030-01-01T00:00:00Z')
    first = project_summary(store, project_key=key)
    assert first['computable'] is True and first['total_bytes'] == 0
    (repo/'AGENTS.md').symlink_to(repo/'missing.md')
    availability.collect_availability(store, cfg, observed_at='2030-01-02T00:00:00Z')
    failed = project_summary(store, project_key=key)
    assert failed['computable'] is False and failed['reason'] == 'incomplete_inventory_observation'
