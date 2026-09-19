"""Actual managed-source collection; all policy and state live in temporary trees."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import json
import os

import pytest

from self_improve import config, instruction_inventory as inventory, instruction_text
from self_improve import instruction_surfaces as surfaces, rule_availability as availability
from self_improve.mining_history import digest
from tests.test_instruction_surfaces import write
from tests.test_rule_availability import cfg, store, known_copy, init_git_repo, delivery, rows


def capture(cfg, repo):
    return inventory.build_inventory(surface=surfaces.inspect_surfaces(cfg, repo),
        project_key='github:example/fixture', working_copy_path=str(repo), revisions=[], observed_at='2030-01-01T00:00:00Z')


def validate(record):
    record['id'] = digest({k:v for k,v in record.items() if k != 'id'})
    return inventory._validate(record, {})


@pytest.mark.parametrize('platform,path', [('darwin','/Library/Application Support/ClaudeCode'),
    ('linux','/etc/claude-code'), ('win32','C:/Program Files/ClaudeCode')])
def test_native_default_without_resource_access(monkeypatch, platform, path):
    monkeypatch.setattr(config.sys, 'platform', platform)
    assert config.default_claude_managed_dir() == path


@pytest.mark.parametrize('value', ['', 'relative', None, 17])
def test_invalid_override_fails_before_collection(cfg, value):
    with pytest.raises(config.ConfigError, match='claude_managed_dir'):
        replace(cfg, claude_managed_dir=value)


def test_managed_memory_skills_imports_and_no_speculative_execution(cfg, tmp_path):
    repo = init_git_repo(tmp_path/'project', 'README.md', '# Fixture\n')
    root = Path(cfg.claude_managed_dir)
    memory = write(root/'CLAUDE.md', '@./company.txt\nManaged policy body.\n')
    imported = write(root/'company.txt', 'Imported café guidance.\n@CLAUDE.md\n')
    skill = write(root/'.claude/skills/check/SKILL.md', '---\nname: Display name\n---\n@secret.txt\n!`never-run`\n')
    write(skill.parent/'secret.txt', 'Not discovered.\n')
    for unsupported in ('rules/speculative.md', '.claude/rules/speculative.md', 'commands/speculative.md', '.claude/commands/speculative.md'):
        write(root/unsupported, 'Not a documented managed file source.\n')
    write(root/'managed-settings.json', json.dumps({'apiKeyHelper':'never-run'}))
    record = capture(cfg, repo)
    validate(record)
    assert record['profile'] == 'instruction-surfaces/6'
    assert record['managed_discovery'] == dict(root=str(root), memory_status='observed', skills_status='observed')
    assert {f['path'] for f in record['files']} == {str(memory), str(imported), str(skill)}
    for file in record['files']:
        assert file['loading_paths'][0]['origin'] == 'managed'
        assert file['loading_paths'][0]['runtime_loading_verified'] is False
    assert {g['origin'] for g in record['context']['groups']} == {'managed', 'all'}
    assert record['totals']['bytes'] == sum(p.stat().st_size for p in (memory, imported, skill))
    assert 'effective_managed_policy' in record['unobserved_sources']
    assert any(d.get('cause') == 'import_cycle' for d in record['deduplicated'])


def test_missing_and_empty_managed_sources_are_distinct(cfg, tmp_path):
    repo = init_git_repo(tmp_path/'project', 'README.md', '# Fixture\n')
    absent = capture(cfg, repo); validate(absent)
    assert absent['managed_discovery']['memory_status'] == absent['managed_discovery']['skills_status'] == 'absent'
    write(Path(cfg.claude_managed_dir)/'CLAUDE.md', '')
    (Path(cfg.claude_managed_dir)/'.claude/skills').mkdir(parents=True)
    empty = capture(cfg, repo); validate(empty)
    assert empty['managed_discovery']['memory_status'] == empty['managed_discovery']['skills_status'] == 'observed'
    assert empty['totals']['bytes'] == 0 and empty['totals']['files'] == 1


@pytest.mark.parametrize('target,field', [('CLAUDE.md','memory_status'), ('.claude/skills','skills_status')])
def test_denied_managed_entry_is_failed_not_absent(cfg, tmp_path, monkeypatch, target, field):
    repo = init_git_repo(tmp_path/'project', 'README.md', '# Fixture\n')
    path = Path(cfg.claude_managed_dir)/target
    original = Path.lstat
    def denied(self, *a, **kw):
        if self == path: raise PermissionError('invented denial')
        return original(self, *a, **kw)
    monkeypatch.setattr(Path, 'lstat', denied)
    record = capture(cfg, repo); validate(record)
    assert record['managed_discovery'][field] == 'failed' and record['status'] == 'partial'
    assert any(i['cause'] == 'managed_entry_unreadable:PermissionError' for i in record['issues'])


def test_unreadable_managed_namespace_cannot_enable_local_command(cfg, tmp_path, monkeypatch):
    repo = init_git_repo(tmp_path/'project', 'README.md', '# Fixture\n')
    write(repo/'.claude/commands/check.md', 'Local body.\n')
    root = Path(cfg.claude_managed_dir)/'.claude/skills'; root.mkdir(parents=True)
    original = os.scandir
    def denied(path):
        if Path(path) == root: raise PermissionError('invented denial')
        return original(path)
    monkeypatch.setattr(os, 'scandir', denied)
    record = capture(cfg, repo); validate(record)
    assert record['managed_discovery']['skills_status'] == 'failed'
    path = record['files'][0]['loading_paths'][0]
    assert path['eligible_prefix_bytes'] == 0 and path['eligibility_reason'] == 'command_discovery_incomplete'


def test_managed_broken_alias_and_byte_limit_are_failed(cfg, tmp_path, monkeypatch):
    repo = init_git_repo(tmp_path/'project', 'README.md', '# Fixture\n')
    root = Path(cfg.claude_managed_dir); root.mkdir()
    memory = root/'CLAUDE.md'; memory.symlink_to(root/'missing.md')
    first = capture(cfg, repo); validate(first)
    assert first['managed_discovery']['memory_status'] == 'failed'
    memory.unlink(); memory.write_text('x'*65)
    monkeypatch.setattr(surfaces, 'MAX_FILE_BYTES', 64)
    second = capture(cfg, repo); validate(second)
    assert second['managed_discovery']['memory_status'] == 'failed'
    assert any(i['cause'] == 'file_byte_limit' for i in second['issues'])


def test_managed_skill_metadata_failure_is_retained(cfg, tmp_path):
    repo = init_git_repo(tmp_path/'project', 'README.md', '# Fixture\n')
    write(Path(cfg.claude_managed_dir)/'.claude/skills/bad/SKILL.md', '---\nname: []\n---\nBad metadata.\n')
    record = capture(cfg, repo); validate(record)
    assert record['managed_discovery']['skills_status'] == 'failed'
    assert record['files'][0]['loading_paths'][0]['eligible_prefix_bytes'] == 0


def test_managed_and_personal_aliases_share_physical_bytes(cfg, tmp_path):
    repo = init_git_repo(tmp_path/'project', 'README.md', '# Fixture\n')
    body = write(Path(cfg.global_claude_md), 'Shared instructions.\n')
    root = Path(cfg.claude_managed_dir); root.mkdir()
    os.link(body, root/'CLAUDE.md')
    record = capture(cfg, repo); validate(record)
    assert record['totals']['files'] == 1
    groups = {g['origin']: g for g in record['context']['groups']}
    assert set(groups) == {'all', 'global', 'managed'}
    assert all(g['startup_bytes'] == body.stat().st_size for g in groups.values())


def test_managed_name_collision_remains_unresolved(cfg, tmp_path):
    repo = init_git_repo(tmp_path/'project', 'README.md', '# Fixture\n')
    write(repo/'.claude/skills/check/SKILL.md', 'Project skill.\n')
    write(Path(cfg.claude_managed_dir)/'.claude/skills/check/SKILL.md', 'Managed skill.\n')
    record = capture(cfg, repo); validate(record)
    paths = [p for f in record['files'] for p in f['loading_paths']]
    assert all(p['eligible_prefix_bytes'] == 0 and p['eligibility_reason'] == 'command_name_precedence_unresolved' for p in paths)


def test_managed_read_reaches_availability_inventory_archive_and_api(cfg, store, tmp_path, monkeypatch):
    pytest.importorskip('fastapi')
    from fastapi.testclient import TestClient
    from self_improve.dashboard.app import create_app
    repo = init_git_repo(tmp_path/'project', 'README.md', '# Fixture\n')
    key = known_copy(store, repo)
    proposal, text = delivery(store, cfg, repo)
    target = write(Path(cfg.claude_managed_dir)/'CLAUDE.md', text)
    result = availability.collect_availability(store, cfg, observed_at='2030-01-01T00:00:00Z')
    assert result['outcomes'] == {'available': 1}
    assert rows(store)[0]['matches'][0]['loading_paths'][0]['origin'] == 'managed'
    inv = inventory.project_inventory(store, project_key=key)['records'][0]
    assert inv['totals']['ownership']['machine']['bytes'] == len(text.encode())
    archive_id = store.query_one('SELECT id FROM instruction_text_archives')['id']
    target.unlink()
    monkeypatch.setattr(availability, 'inspect_surfaces', lambda *a, **kw: pytest.fail('reader reopened targets'))
    before = list(store.conn.iterdump())
    from self_improve.redact import redact_text
    assert instruction_text.read_archive(store, archive_id)['files'][0]['text'] == redact_text(text)
    with TestClient(create_app(cfg)) as client:
        response = client.get('/api/project-inventory', params={'project_key': key})
        assert response.status_code == 200, response.text
        assert response.json()['records'][0] == inv
    assert list(store.conn.iterdump()) == before


def test_publication_failure_rolls_back_managed_inventory_and_text(cfg, store, tmp_path, monkeypatch):
    repo = init_git_repo(tmp_path/'project', 'README.md', '# Fixture\n'); known_copy(store, repo)
    write(Path(cfg.claude_managed_dir)/'CLAUDE.md', 'Managed fixture.\n')
    before = list(store.conn.iterdump())
    def fail(*a, **kw): raise RuntimeError('invented archive failure')
    monkeypatch.setattr(instruction_text, 'record_archives', fail)
    with pytest.raises(RuntimeError, match='invented archive failure'):
        availability.collect_availability(store, cfg, observed_at='2030-01-01T00:00:00Z')
    assert list(store.conn.iterdump()) == before


@pytest.mark.parametrize('mutation', ['root','memory_status','provider','source','path','origin','chain'])
def test_retained_managed_semantics_reject_rehashed_mutations(cfg, tmp_path, mutation):
    repo = init_git_repo(tmp_path/'project', 'README.md', '# Fixture\n')
    root = Path(cfg.claude_managed_dir)
    write(root/'CLAUDE.md', '@./included.txt\n'); write(root/'included.txt', 'Imported.\n')
    record = capture(cfg, repo)
    managed_path = next(p for f in record['files'] for p in f['loading_paths'] if p['source'] == 'memory')
    if mutation == 'root': record['managed_discovery']['root'] = str(root/'different')
    elif mutation == 'memory_status': record['managed_discovery']['memory_status'] = 'absent'
    elif mutation == 'chain':
        next(p for f in record['files'] for p in f['loading_paths'] if p['source'] == 'import')['import_chain'] = []
    else: managed_path[mutation] = {'provider':'codex','source':'rule','path':str(root/'different.md'),'origin':'global'}[mutation]
    # Recompute derived metrics as well: semantic checks must carry the guard.
    from self_improve.instruction_context import summarize
    record['context'] = summarize(record['files'])
    with pytest.raises(inventory.InventoryError): validate(record)


@pytest.mark.parametrize('profile', ['instruction-surfaces/1', 'instruction-surfaces/2', 'instruction-surfaces/3'])
def test_older_profiles_stay_readable(cfg, tmp_path, profile):
    repo = init_git_repo(tmp_path/'project', 'CLAUDE.md', 'Older user instructions.\n')
    surface = surfaces.inspect_surfaces(cfg, repo)
    surface['profile'] = profile; surface.pop('managed_discovery')
    record = inventory.build_inventory(surface=surface, project_key='github:example/fixture', working_copy_path=str(repo), revisions=[], observed_at='2030-01-01T00:00:00Z')
    assert validate(deepcopy(record)) == record


def test_identity_refusal_does_not_invent_a_managed_check(cfg, tmp_path):
    surface = dict(files=[], issues=[{'cause':'working_copy_identity_changed'}], profile=surfaces.PROFILE, runtime_loading_verified=False)
    record = inventory.build_inventory(surface=surface, project_key='github:example/fixture', working_copy_path=str(tmp_path), revisions=[], observed_at='2030-01-01T00:00:00Z')
    validate(record)
    assert record['managed_discovery'] == dict(root='', memory_status='not_checked', skills_status='not_checked')
    record['managed_discovery']['memory_status'] = 'absent'
    with pytest.raises(inventory.InventoryError): validate(record)


def test_broken_managed_root_is_failure_not_absence(cfg, tmp_path):
    repo = init_git_repo(tmp_path/'project', 'README.md', '# Fixture\n')
    write(repo/'.claude/commands/check.md', 'Local command.\n')
    root = Path(cfg.claude_managed_dir); root.symlink_to(tmp_path/'missing-root', target_is_directory=True)
    record = capture(cfg, repo); validate(record)
    assert record['managed_discovery']['memory_status'] == record['managed_discovery']['skills_status'] == 'failed'
    assert record['files'][0]['loading_paths'][0]['eligible_prefix_bytes'] == 0


@pytest.mark.parametrize('source', ['memory','skills'])
@pytest.mark.parametrize('status', ['absent','observed'])
def test_rehashed_failure_cannot_be_relabelled(cfg, tmp_path, monkeypatch, source, status):
    repo = init_git_repo(tmp_path/'project', 'README.md', '# Fixture\n')
    relative = 'CLAUDE.md' if source == 'memory' else '.claude/skills/check/SKILL.md'
    write(Path(cfg.claude_managed_dir)/relative, 'x'*65)
    monkeypatch.setattr(surfaces, 'MAX_FILE_BYTES', 64)
    record = capture(cfg, repo); validate(record)
    record['managed_discovery'][source+'_status'] = status
    with pytest.raises(inventory.InventoryError): validate(record)


def test_managed_discovery_disappearance_is_failed(cfg, tmp_path, monkeypatch):
    repo = init_git_repo(tmp_path/'project', 'README.md', '# Fixture\n')
    write(repo/'.claude/commands/check.md', 'Local command.\n')
    root = Path(cfg.claude_managed_dir)/'.claude/skills'; root.mkdir(parents=True)
    original = os.lstat; seen = 0
    def disappears(path, *a, **kw):
        nonlocal seen
        if Path(path) == root:
            seen += 1
            if seen > 1: raise PermissionError('invented discovery race')
        return original(path, *a, **kw)
    monkeypatch.setattr(os, 'lstat', disappears)
    record = capture(cfg, repo); validate(record)
    assert record['managed_discovery']['skills_status'] == 'failed'
    assert record['files'][0]['loading_paths'][0]['eligible_prefix_bytes'] == 0


def test_reserved_managed_skill_cannot_supply_eligibility(cfg, tmp_path):
    repo = init_git_repo(tmp_path/'project', 'README.md', '# Fixture\n')
    write(Path(cfg.claude_managed_dir)/'.claude/skills/SYNCED/SKILL.md', 'Reserved authored folder.\n')
    record = capture(cfg, repo); validate(record)
    path = record['files'][0]['loading_paths'][0]
    assert path['eligible_prefix_bytes'] == 0
    assert path['eligibility_reason'] == 'reserved_skill_directory'
    path.update(eligible_prefix_bytes=record['files'][0]['bytes'],eligibility_reason='')
    from self_improve.instruction_context import summarize
    record['context'] = summarize(record['files'])
    with pytest.raises(inventory.InventoryError): validate(record)


def test_invalid_managed_issue_path_has_named_corruption_error(cfg, tmp_path, monkeypatch):
    repo = init_git_repo(tmp_path/'project', 'README.md', '# Fixture\n')
    write(Path(cfg.claude_managed_dir)/'.claude/skills/check/SKILL.md', 'x'*65)
    monkeypatch.setattr(surfaces, 'MAX_FILE_BYTES', 64)
    record = capture(cfg, repo); record['issues'][0]['path'] = 17
    with pytest.raises(inventory.InventoryError): validate(record)


@pytest.mark.parametrize('case', ['skill_support_import', 'missing_memory_import'])
def test_import_failure_is_not_managed_discovery_failure(cfg, store, tmp_path, case):
    repo = init_git_repo(tmp_path/'project', 'README.md', '# Fixture\n'); key = known_copy(store, repo)
    root = Path(cfg.claude_managed_dir)
    if case == 'skill_support_import':
        (root/'.claude/skills/check').mkdir(parents=True)
        write(root/'CLAUDE.md', '@./.claude/skills/check/support.txt\n')
    else:
        write(repo/'CLAUDE.md', '@'+str(root/'CLAUDE.md')+'\n')
    availability.collect_availability(store, cfg, observed_at='2030-01-01T00:00:00Z')
    record = inventory.project_inventory(store, project_key=key)['records'][0]
    assert record['status'] == 'partial' and any(i['cause'] == 'file_missing' for i in record['issues'])
    expected = 'observed' if case == 'skill_support_import' else 'absent'
    assert record['managed_discovery']['memory_status'] == record['managed_discovery']['skills_status'] == expected
