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
    from tests.walk_guard import forbid_live_walks
    app = create_app(cfg)
    forbid_live_walks(monkeypatch)
    with TestClient(app) as client:
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


import pytest as _pytest


@_pytest.mark.parametrize('spec', ['@~no-such-user-for-bl30/notes.md', '@./a\x00b.md'])
def test_a_bad_import_path_is_an_issue_not_a_collection_crash(cfg, tmp_path, spec):
    """BL-30: expanduser() and resolve() ran without error handling, so one
    unknown ~user or a NUL in an import aborted collection for every copy."""
    repo = init_git_repo(tmp_path/'project', 'README.md', '# Fixture\n')
    write(repo/'CLAUDE.md', f'See {spec} for details.\n')
    result = inspect_surfaces(cfg, repo)
    causes = [i['cause'] for i in result['issues']]
    assert any(c.startswith('import_path_invalid') or c.startswith('file_unreadable') for c in causes), causes
    assert str(repo/'CLAUDE.md') in {f['path'] for f in result['files']}


def test_a_plugin_registry_path_with_a_nul_is_an_issue_not_a_crash(cfg, tmp_path):
    """BL-30: an installPath with an embedded NUL raised ValueError out of the
    collector and failed the nightly before mining."""
    import json as _json
    repo = init_git_repo(tmp_path/'project', 'README.md', '# Fixture\n')
    # An empty claude_plugins_dir derives plugins/ beside the global CLAUDE.md.
    plugins = Path(cfg.claude_plugins_dir) if cfg.claude_plugins_dir else Path(cfg.global_claude_md).parent/'plugins'
    assert plugins.is_absolute()
    write(plugins/'installed_plugins.json', _json.dumps({'version': 2, 'plugins': {
        'p@mk': [{'scope': 'user', 'installPath': str(tmp_path/'pl') + '\u0000x', 'version': '1'}]}}))
    result = inspect_surfaces(cfg, repo)
    assert result['issues'], result['issues']


def test_a_dangling_codex_home_is_a_named_failure_not_an_empty_home(cfg, tmp_path):
    """BL-33: os.path.lexists returned False for a dangling or unreadable Codex
    home, so the inventory recorded no Codex files and no issue at all."""
    repo = init_git_repo(tmp_path/'project', 'README.md', '# Fixture\n')
    home = tmp_path/'codex-home'
    home.symlink_to(tmp_path/'missing-codex-home')
    result = inspect_surfaces(replace(cfg, codex_global_agents_md=str(home/'AGENTS.md')), repo)
    causes = [(i['cause'], i['path']) for i in result['issues']]
    assert any(c.startswith('source_unreadable') and p.startswith(str(home)) for c, p in causes), causes


def test_a_project_codex_config_that_changes_discovery_needs_runtime_trust(cfg, tmp_path):
    """BL-52: the project .codex/config.toml trust branch had no test. Codex
    applies a project config only to trusted projects, which the collector
    cannot observe, so discovery settings there make Codex coverage unknown."""
    repo = init_git_repo(tmp_path/'project', 'README.md', '# Fixture\n')
    write(repo/'AGENTS.md', 'Project rule.\n')
    write(repo/'.codex'/'config.toml', 'project_doc_max_bytes = 10\n')
    result = inspect_surfaces(cfg, repo)
    causes = [i['cause'] for i in result['issues']]
    assert 'codex_project_configuration_requires_runtime_trust' in causes, causes
    assert result['codex_discovery']['configuration_valid'] is False
    agents = next(f for f in result['files'] if f['path'] == str(repo/'AGENTS.md'))
    codex_paths = [p for p in agents['loading_paths'] if p['provider'] == 'codex']
    assert codex_paths and all(p['eligible_prefix_bytes'] == 0 for p in codex_paths), codex_paths


def test_project_codex_files_that_exactly_fill_the_budget_are_fully_eligible(cfg, tmp_path):
    """BL-32: the collector charged 2 bytes per separator. Codex charges each
    project document's own bytes to project_doc_max_bytes and adds the
    separator only when it renders the text, so two project files that sum
    to exactly the allowance load in full. One more byte is omitted."""
    repo = init_git_repo(tmp_path/'project', 'README.md', '# Fixture\n')
    write(Path(cfg.codex_global_agents_md).parent/'config.toml', 'project_doc_max_bytes = 100\n')
    root_doc = write(repo/'AGENTS.md', 'r' * 59 + '\n')
    nested_doc = write(repo/'sub/AGENTS.md', 'n' * 39 + '\n')

    def codex_prefixes():
        result = inspect_surfaces(cfg, repo/'sub')
        files = {f['path']: f for f in result['files']}
        prefixes = [p['eligible_prefix_bytes'] for doc in (root_doc, nested_doc)
                    for p in files[str(doc.resolve())]['loading_paths'] if p['provider'] == 'codex']
        limits = [(Path(i['path']).parent.name, i['omitted_bytes']) for i in result['issues']
                  if i['cause'] == 'codex_instruction_coverage_limit']
        return prefixes, limits

    assert codex_prefixes() == ([60, 40], [])
    nested_doc.write_text('n' * 40 + '\n')
    assert codex_prefixes() == ([60, 40], [('sub', 1)])


def test_a_project_codex_skills_table_needs_runtime_trust(cfg, tmp_path):
    """BL-52: the second trust cause had no test. A [skills] table in a
    project .codex/config.toml applies only in a trusted project, so the
    collector cannot resolve skill settings. It names the cause, leaves the
    project skill unresolved, and keeps document discovery valid."""
    repo = init_git_repo(tmp_path/'project', 'README.md', '# Fixture\n')
    write(repo/'AGENTS.md', 'Project rule.\n')
    write(repo/'.codex'/'config.toml', '[skills]\nconfig = []\n')
    skill = write(repo/'.agents/skills/example/SKILL.md', '---\nname: example\ndescription: An invented skill.\n---\nBody.\n')
    result = inspect_surfaces(cfg, repo)
    causes = [(i['cause'], i['path']) for i in result['issues']]
    assert causes == [('codex_project_skill_configuration_requires_runtime_trust', str(repo.resolve()/'.codex/config.toml'))], causes
    assert result['codex_discovery']['skill_configuration_valid'] is False
    assert result['codex_discovery']['configuration_valid'] is True
    files = {f['path']: f for f in result['files']}
    [skill_path] = files[str(skill.resolve())]['loading_paths']
    assert (skill_path['eligibility_reason'], skill_path['eligible_prefix_bytes']) == ('codex_skill_configuration_unresolved', 0)
    [agents_path] = files[str(repo.resolve()/'AGENTS.md')]['loading_paths']
    assert agents_path['eligible_prefix_bytes'] == len('Project rule.\n')


def test_an_import_chain_past_the_depth_cap_is_named_with_its_omissions(cfg, tmp_path):
    """BL-52: no test reached import_depth_limit; raising the cap passed.

    The chain is literal, not built from MAX_IMPORT_DEPTH, so a changed cap
    or one extra loaded hop fails here. Claude Code follows imports for five
    hops: CLAUDE.md -> h1 ... h5 load, and h5's import of h6 is omitted."""
    repo = init_git_repo(tmp_path/'project', 'README.md', '# Fixture\n')
    write(repo/'CLAUDE.md', '@h1.md\n')
    for level in range(1, 6):
        write(repo/f'h{level}.md', f'level {level}\n@h{level + 1}.md\n')
    write(repo/'h6.md', 'level 6\n')
    result = inspect_surfaces(cfg, repo)
    limits = [i for i in result['issues'] if i['cause'] == 'import_depth_limit']
    assert limits == [{'cause': 'import_depth_limit', 'path': str(repo.resolve()/'h5.md'), 'omitted_imports': 1}], result['issues']
    loaded = {Path(f['path']).name for f in result['files']}
    assert {'CLAUDE.md', 'h1.md', 'h2.md', 'h3.md', 'h4.md', 'h5.md'} <= loaded, loaded
    assert 'h6.md' not in loaded, loaded


def test_the_surface_file_count_cap_is_named(cfg, tmp_path, monkeypatch):
    from self_improve import instruction_surfaces as surfaces
    repo = init_git_repo(tmp_path/'project', 'README.md', '# Fixture\n')
    write(repo/'CLAUDE.md', '@a.md\n@b.md\n@c.md\n')
    for name in 'abc':
        write(repo/f'{name}.md', f'{name}\n')
    monkeypatch.setattr(surfaces, 'MAX_FILES', 2)
    result = inspect_surfaces(cfg, repo)
    assert any(i['cause'] == 'file_count_limit' for i in result['issues']), result['issues']


def test_the_surface_file_count_cap_holds_exactly_2000_files_and_names_the_next(cfg, tmp_path):
    """Review of BL-52: changing the read cap's `>=` to `>` passed every
    instruction test, because the cap test above omits two files at a
    patched cap of 2. The counts here are literal. One rule directory stays
    inside its own discovery cap of 2,000, and CLAUDE.md adds one more file.

    At exactly 2,000 read files nothing is omitted. At 2,001 exactly one is,
    with the cap and the omission named, and every other file still loads.
    CLAUDE.md is read before the rule directory, whose names are sorted, so
    the last rule name is the one omitted."""
    repo = init_git_repo(tmp_path/'project', 'README.md', '# Fixture\n')
    write(repo/'CLAUDE.md', 'Project rule.\n')
    rules = repo/'.claude/rules'
    for index in range(1999):
        write(rules/f'r{index:04}.md', f'Invented rule {index}.\n')
    exact = inspect_surfaces(cfg, repo)
    assert not [i for i in exact['issues'] if i['cause'] in {'file_count_limit', 'discovery_file_limit'}], exact['issues'][:3]
    assert len(exact['files']) == 2000

    write(rules/'r1999.md', 'Invented rule 1999.\n')
    over = inspect_surfaces(cfg, repo)
    assert not [i for i in over['issues'] if i['cause'] == 'discovery_file_limit'], over['issues'][:3]
    limits = [i for i in over['issues'] if i['cause'] == 'file_count_limit']
    assert limits == [{'cause': 'file_count_limit', 'path': str(repo.resolve()/'.claude/rules/r1999.md'),
                       'limit': 2000, 'omitted_at_least': 1}], limits
    loaded = {Path(f['path']).name for f in over['files']}
    assert len(loaded) == 2000 and {'CLAUDE.md', 'r1998.md'} <= loaded and 'r1999.md' not in loaded, sorted(loaded)[-3:]


def test_a_rule_directory_past_the_discovery_file_cap_is_named_with_its_omissions(cfg, tmp_path):
    """BL-52: nothing reached discovery_file_limit; replacing its check with
    `if False:` passed every instruction test. The 2,001 files are literal,
    so a raised MAX_FILES fails here as well as a removed check. Discovery
    sorts one directory by name, so the last name is the one omitted."""
    repo = init_git_repo(tmp_path/'project', 'README.md', '# Fixture\n')
    rules = repo/'.claude/rules'
    for index in range(2001):
        write(rules/f'r{index:04}.md', f'Invented rule {index}.\n')
    result = inspect_surfaces(cfg, repo)
    limits = [i for i in result['issues'] if i['cause'] == 'discovery_file_limit']
    assert limits == [{'cause': 'discovery_file_limit', 'path': str(repo.resolve()/'.claude/rules'),
                       'omitted_at_least': 1, 'limit': 2000}], limits
    loaded = {Path(f['path']).name for f in result['files']}
    assert len(loaded) == 2000 and 'r1999.md' in loaded and 'r2000.md' not in loaded, sorted(loaded)[-3:]
    # The omission is discovery's, not the separate read cap's.
    assert not [i for i in result['issues'] if i['cause'] == 'file_count_limit'], result['issues'][:3]
    # At exactly the cap nothing is omitted.
    (rules/'r2000.md').unlink()
    exact = inspect_surfaces(cfg, repo)
    assert not [i for i in exact['issues'] if i['cause'] == 'discovery_file_limit'], exact['issues'][:3]
    assert len(exact['files']) == 2000
