"""Legacy command discovery through the real temporary collector and readers."""
from pathlib import Path
import os

import pytest

from self_improve import instruction_inventory as inventory, rule_availability as availability
from self_improve.instruction_surfaces import inspect_surfaces
from self_improve.mining_history import digest, encoded
from tests.test_instruction_surfaces import write
from tests.test_rule_availability import cfg, store, known_copy, init_git_repo, delivery, rows, run_git


def paths(result):
    return {p['path']: p for f in result['files'] for p in f['loading_paths']}


def test_commands_reach_real_collector_and_retained_reader(cfg, store, tmp_path, monkeypatch):
    repo = init_git_repo(tmp_path/'project', 'README.md', '# Fixture\n')
    key = known_copy(store, repo)
    command = write(repo/'.claude/commands/frontend/check.md',
                    '---\nname: ignored\npaths: ["src/**"]\n---\n@secret.md\n!`never-run`\n')
    write(command.parent/'secret.txt', 'Never discover a support file.\n')
    global_command = write(Path(cfg.global_claude_md).parent/'commands/report.md', 'Global command.\n')
    availability.collect_availability(store, cfg, observed_at='2030-01-01T00:00:00Z')
    record = inventory.project_inventory(store, project_key=key)['records'][0]
    assert record['profile'] == 'instruction-surfaces/6'
    observed = paths(record)
    assert set(observed) == {str(command), str(global_command)}
    p = observed[str(command)]
    assert p['command_name'] == 'frontend:check'
    assert p['source'] == 'command' and p['scope'] == {'kind': 'on_demand', 'paths': []}
    assert p['conditions'] == [] and p['import_chain'] == []
    assert p['eligible_prefix_bytes'] == command.stat().st_size
    assert p['shadowed_by'] == [] and p['runtime_loading_verified'] is False
    assert observed[str(global_command)]['origin'] == 'global'
    command.write_text('Changed after collection.\n')
    monkeypatch.setattr(availability, 'inspect_surfaces', lambda *a, **kw: pytest.fail('reader reopened files'))
    assert inventory.project_inventory(store, project_key=key)['records'][0] == record
    archived = store.query_one('SELECT record_json FROM instruction_text_archives')
    assert 'never-run' in archived['record_json']


def test_same_root_skill_shadows_command_using_folder_not_display_name(cfg, tmp_path):
    repo = init_git_repo(tmp_path/'project', 'README.md', '# Fixture\n')
    skill = write(repo/'.claude/skills/deploy/SKILL.md', '---\nname: Display label\n---\nSkill.\n')
    command = write(repo/'.claude/commands/deploy.md', 'Legacy.\n')
    other = write(repo/'.claude/skills/other/SKILL.md', '---\nname: Display label\n---\nOther.\n')
    result = inspect_surfaces(cfg, repo); observed = paths(result)
    assert observed[str(skill)]['command_name'] == 'deploy'
    assert observed[str(skill)]['eligible_prefix_bytes'] == skill.stat().st_size
    assert observed[str(other)]['eligible_prefix_bytes'] == other.stat().st_size
    assert observed[str(command)]['eligible_prefix_bytes'] == 0
    assert observed[str(command)]['eligibility_reason'] == 'command_shadowed_by_skill'
    assert observed[str(command)]['shadowed_by'] == [str(skill)]
    record = inventory.build_inventory(surface=result, project_key='fixture', working_copy_path=str(repo),
                                       revisions=[], observed_at='2030-01-01T00:00:00Z')
    group = next(g for g in record['context']['groups'] if g['origin'] == 'all')
    assert group['on_demand_bytes'] == skill.stat().st_size + other.stat().st_size
    assert group['unresolved_bytes'] == command.stat().st_size


def test_cross_root_command_collision_stays_unknown(cfg, tmp_path):
    repo = init_git_repo(tmp_path/'project', 'README.md', '# Fixture\n')
    a = write(repo/'.claude/commands/deploy.md', 'Project.\n')
    b = write(Path(cfg.global_claude_md).parent/'commands/deploy.md', 'Personal.\n')
    result = inspect_surfaces(cfg, repo)
    assert set(paths(result)) == {str(a), str(b)}
    assert all(p['eligible_prefix_bytes'] == 0 and p['eligibility_reason'] == 'command_name_precedence_unresolved'
               for p in paths(result).values())


def test_command_aliases_keep_names_without_counting_twice(cfg, tmp_path):
    repo = init_git_repo(tmp_path/'project', 'README.md', '# Fixture\n')
    command = write(repo/'.claude/commands/deploy.md', 'One physical file.\n')
    alias = command.with_name('ship.md'); alias.symlink_to(command)
    result = inspect_surfaces(cfg, repo)
    assert len(result['files']) == 1
    assert {p['command_name'] for p in result['files'][0]['loading_paths']} == {'deploy', 'ship'}
    assert all(p['eligible_prefix_bytes'] == command.stat().st_size for p in paths(result).values())


def test_invalid_skill_does_not_grant_known_command_selection(cfg, tmp_path):
    repo = init_git_repo(tmp_path/'project', 'README.md', '# Fixture\n')
    write(repo/'.claude/skills/deploy/SKILL.md', '---\nname: [invalid\n---\nSkill.\n')
    command = write(repo/'.claude/commands/deploy.md', 'Legacy.\n')
    result = inspect_surfaces(cfg, repo)
    assert paths(result)[str(command)]['eligible_prefix_bytes'] == 0
    assert paths(result)[str(command)]['eligibility_reason'] == 'command_name_precedence_unresolved'


def test_delivered_command_cannot_claim_availability_when_shadowed(cfg, store, tmp_path):
    repo = init_git_repo(tmp_path/'project', 'README.md', '# Fixture\n')
    known_copy(store, repo)
    (repo/'.claude/commands').mkdir(parents=True)
    proposal, _ = delivery(store, cfg, repo, filename='.claude/commands/deploy.md', target_kind='project_claude_md')
    run_git(['merge', '--ff-only', cfg.project_branch_name], repo)
    availability.collect_availability(store, cfg, observed_at='2030-01-01T00:00:00Z')
    record = next(r for r in rows(store) if r['learning_id'] == proposal['learning_id'])
    assert record['status'] == 'available'
    assert all(p['scope']['kind'] == 'on_demand' for m in record['matches'] for p in m['loading_paths'])
    write(repo/'.claude/skills/deploy/SKILL.md', '---\nname: Display only\n---\nDifferent skill.\n')
    availability.collect_availability(store, cfg, observed_at='2030-01-02T00:00:00Z')
    assert rows(store)[-1]['status'] != 'available'


def test_old_profile_two_is_immutable_and_keeps_its_original_context(cfg, store, tmp_path):
    repo = init_git_repo(tmp_path/'project', 'README.md', '# Fixture\n')
    key = known_copy(store, repo)
    write(repo/'.claude/skills/check/SKILL.md', '---\nname: Old display\n---\nSkill.\n')
    old = inspect_surfaces(cfg, repo); old['profile'] = 'instruction-surfaces/2'
    for path in paths(old).values():
        for field in ('command_name', 'discovery_root', 'shadowed_by'): path.pop(field)
    record = inventory.build_inventory(surface=old, project_key=key, working_copy_path=str(repo),
                                       revisions=[], observed_at='2030-01-01T00:00:00Z')
    with store.transaction(write=True): inventory.record_inventories(store, records=[record])
    assert inventory.project_inventory(store, project_key=key)['records'][0] == record
    assert record['context']['groups'][0]['on_demand_bytes'] > 0


def test_command_discovery_caps_and_unreadable_files_are_explicit(cfg, tmp_path, monkeypatch):
    from self_improve import instruction_surfaces as surfaces
    repo = init_git_repo(tmp_path/'project', 'README.md', '# Fixture\n')
    base = repo/'.claude/commands'
    write(base/'too-big.md', 'Invented body.\n')
    (base/'broken.md').symlink_to(base/'missing.txt')
    monkeypatch.setattr(surfaces, 'MAX_FILE_BYTES', 4)
    result = inspect_surfaces(cfg, repo)
    assert not result['files']
    assert {'file_byte_limit', 'directory_entry_unresolved'} <= {i['cause'] for i in result['issues']}
    monkeypatch.setattr(surfaces, 'MAX_DISCOVERY_ENTRIES', 1)
    assert any(i['cause'] == 'discovery_entry_limit' for i in inspect_surfaces(cfg, repo)['issues'])


def test_command_directory_aliases_preserve_names_and_stop_cycles(cfg, tmp_path):
    repo = init_git_repo(tmp_path/'project', 'README.md', '# Fixture\n')
    command = write(repo/'.claude/commands/frontend/check.md', 'Body.\n')
    (command.parent/'loop').symlink_to(command.parent, target_is_directory=True)
    (command.parent.parent/'web').symlink_to(command.parent, target_is_directory=True)
    result = inspect_surfaces(cfg, repo)
    assert len(result['files']) == 1
    assert {p['command_name'] for p in result['files'][0]['loading_paths']} == {'frontend:check', 'web:check'}
    assert any(d['cause'] == 'directory_alias_or_cycle' for d in result['deduplicated'])


@pytest.mark.parametrize('mutation', ['name', 'source', 'profile', 'shadow', 'provider', 'invalid', 'duplicate'])
def test_recomputed_hash_cannot_hide_invalid_command_metadata(cfg, store, tmp_path, mutation):
    repo = init_git_repo(tmp_path/'project', 'README.md', '# Fixture\n')
    key = known_copy(store, repo)
    write(repo/'.claude/commands/deploy.md', '---\nname: [broken\n---\nLegacy.\n' if mutation == 'invalid' else 'Legacy.\n')
    if mutation in {'shadow', 'duplicate'}: write(repo/'.claude/skills/deploy/SKILL.md', 'Skill.\n')
    availability.collect_availability(store, cfg, observed_at='2030-01-01T00:00:00Z')
    record = inventory.project_inventory(store, project_key=key)['records'][0]
    old = record['id']
    if mutation == 'name': record['files'][0]['loading_paths'][0]['command_name'] = 'wrong'
    elif mutation == 'source': record['files'][0]['loading_paths'][0]['source'] = 'unexpected'
    elif mutation == 'profile': record['profile'] = 'instruction-surfaces/999'
    elif mutation == 'provider': record['files'][0]['loading_paths'][0]['provider'] = 'codex'
    else:
        from self_improve.instruction_context import summarize
        command = next(f for f in record['files'] if f['path'].endswith('deploy.md'))
        if mutation == 'duplicate':
            command['loading_paths'].insert(0, {**command['loading_paths'][0], 'command_name': 'wrong',
                                               'eligible_prefix_bytes': command['bytes'], 'eligibility_reason': '', 'shadowed_by': []})
        elif mutation == 'invalid':
            assert command['loading_paths'][0]['eligibility_reason'] == 'frontmatter_invalid'
            command['loading_paths'][0]['eligible_prefix_bytes'] = command['bytes']
        else:
            command['loading_paths'][0].update(eligible_prefix_bytes=command['bytes'], eligibility_reason='', shadowed_by=[])
        record['context'] = summarize(record['files'])
        next(s for s in record['scopes'] if s['provider'] == 'claude')['eligible_prefix_bytes'] = record['totals']['bytes']
    record['id'] = digest({k:v for k,v in record.items() if k != 'id'})
    store.update('instruction_inventories', 'id', old, {'id':record['id'], 'record_json':encoded(record), 'record_hash':digest(record)})
    store.commit()
    with pytest.raises(inventory.InventoryError, match=record['id']):
        inventory.project_inventory(store, project_key=key)


def test_unreadable_competitor_cannot_grant_command_candidate(cfg, tmp_path, monkeypatch):
    from self_improve import instruction_surfaces as surfaces
    repo = init_git_repo(tmp_path/'project', 'README.md', '# Fixture\n')
    command = write(repo/'.claude/commands/deploy.md', 'Legacy.\n')
    write(repo/'.claude/skills/deploy/SKILL.md', 'x'*200)
    monkeypatch.setattr(surfaces, 'MAX_FILE_BYTES', 100)
    result = inspect_surfaces(cfg, repo)
    assert paths(result)[str(command)]['eligible_prefix_bytes'] == 0
    assert paths(result)[str(command)]['eligibility_reason'] == 'command_name_precedence_unresolved'


def test_deep_yaml_is_named_per_file_failure(cfg, tmp_path):
    repo = init_git_repo(tmp_path/'project', 'README.md', '# Fixture\n')
    command = write(repo/'.claude/commands/deploy.md', '---\nx: '+ '['*1200+'0'+']'*1200+'\n---\nBody.\n')
    result = inspect_surfaces(cfg, repo)
    assert paths(result)[str(command)]['eligible_prefix_bytes'] == 0
    assert any(i['cause'] == 'frontmatter_invalid' for i in result['issues'])


def test_hardlink_command_names_count_one_physical_file(cfg, tmp_path):
    repo = init_git_repo(tmp_path/'project', 'README.md', '# Fixture\n')
    command = write(repo/'.claude/commands/deploy.md', 'Body.\n')
    os.link(command, command.with_name('ship.md'))
    result = inspect_surfaces(cfg, repo)
    assert len(result['files']) == 1
    assert {p['command_name'] for p in result['files'][0]['loading_paths']} == {'deploy', 'ship'}


def test_replaced_fifo_is_rejected_before_a_read(cfg, tmp_path, monkeypatch):
    repo = init_git_repo(tmp_path/'project', 'README.md', '# Fixture\n')
    command = write(repo/'.claude/commands/deploy.md', 'Body.\n')
    original = os.open
    reached = []
    def replaced(path, flags, *args, **kwargs):
        if str(path) == str(command):
            reached.append(path)
            # Fail immediately if a regression would open this FIFO blocking.
            assert flags & os.O_NONBLOCK
            command.unlink(); os.mkfifo(command)
        return original(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, 'open', replaced)
    result = inspect_surfaces(cfg, repo)
    assert reached
    assert not result['files']
    assert any(i['cause'] == 'not_regular_file' for i in result['issues'])
    assert result['uninspected_commands'][0]['command_name'] == 'deploy'


def test_incomplete_directory_cannot_establish_command_selection(cfg, tmp_path, monkeypatch):
    repo = init_git_repo(tmp_path/'project', 'README.md', '# Fixture\n')
    command = write(repo/'.claude/commands/deploy.md', 'Body.\n')
    root = repo/'.claude/skills'; root.mkdir()
    original = os.scandir
    def unreadable(path):
        if Path(path) == root: raise PermissionError('Invented unreadable catalog')
        return original(path)
    monkeypatch.setattr(os, 'scandir', unreadable)
    result = inspect_surfaces(cfg, repo)
    assert paths(result)[str(command)]['eligible_prefix_bytes'] == 0
    assert paths(result)[str(command)]['eligibility_reason'] == 'command_discovery_incomplete'
    assert result['incomplete_command_roots'] == [str(root)]
