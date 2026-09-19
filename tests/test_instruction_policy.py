"""Embedded managed text uses invented policy files and a temporary selected Store."""
from copy import deepcopy
from pathlib import Path
import json
import os
import subprocess

import pytest

from self_improve import instruction_inventory as inventory, instruction_text
from self_improve import instruction_surfaces as surfaces, rule_availability as availability
from self_improve.mining_history import digest, encoded
from tests.test_instruction_managed import capture, validate
from tests.test_instruction_surfaces import write
from tests.test_rule_availability import cfg, store, known_copy, init_git_repo, delivery, rows


def policy(cfg, name='managed-settings.json', **values):
    return write(Path(cfg.claude_managed_dir)/name, json.dumps(values))


def test_actual_fields_select_in_order_without_retaining_other_settings(cfg, tmp_path, monkeypatch):
    repo = init_git_repo(tmp_path/'project', 'README.md', 'Invented repo.\n')
    policy(cfg, claudeMd='First café.\n', env={'PRIVATE_VALUE':'unrelated fixture secret'})
    policy(cfg, 'managed-settings.d/20-team.json', claudeMd='Second field.\n@never-read.txt\n!`never-execute`')
    policy(cfg, 'managed-settings.d/10-other.json', model='irrelevant-model')
    policy(cfg, 'managed-settings.d/.hidden.json', claudeMd='Not discovered')
    write(Path(cfg.claude_managed_dir)/'managed-settings.d/ignored.txt', 'Not JSON')
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: pytest.fail('policy executed a command'))
    surface = surfaces.inspect_surfaces(cfg, repo)
    record = inventory.build_inventory(surface=surface, project_key='github:example/fixture', working_copy_path=str(repo), revisions=[], observed_at='2030-01-01T00:00:00Z')
    validate(record)
    selected = record['policy_discovery']
    assert record['profile'] == 'instruction-surfaces/6'
    assert selected['selection'] == 'selected'
    assert selected['selected_source'].endswith('/20-team.json')
    assert selected['selected_bytes'] == len('Second field.\n@never-read.txt\n!`never-execute`'.encode())
    assert len(record['files']) == 2
    assert record['totals']['bytes'] == len('First café.\n'.encode()) + selected['selected_bytes']
    archive = instruction_text.build_archive(surface, record)
    assert 'unrelated fixture secret' not in encoded(record) + encoded(archive)
    assert 'irrelevant-model' not in encoded(record) + encoded(archive)
    assert all(p['eligible_prefix_bytes'] == 0 for f in record['files'] for p in f['loading_paths'])
    assert 'embedded_managed_text' not in record['unobserved_sources']


def test_absent_no_field_and_empty_selected_text_differ(cfg, tmp_path):
    repo = init_git_repo(tmp_path/'project', 'README.md', 'Fixture.\n')
    absent = capture(cfg, repo); validate(absent)
    assert absent['policy_discovery']['main_status'] == 'absent'
    assert absent['policy_discovery']['selection'] == 'absent'
    assert absent['policy_discovery']['selected_bytes'] == 0
    policy(cfg, env={'fixture':'not instruction text'})
    no_field = capture(cfg, repo); validate(no_field)
    assert no_field['policy_discovery']['main_status'] == 'observed'
    assert no_field['policy_discovery']['selection'] == 'absent'
    assert no_field['files'] == []
    policy(cfg, claudeMd='Previous text')
    policy(cfg, 'managed-settings.d/99-empty.json', claudeMd='')
    empty = capture(cfg, repo); validate(empty)
    assert empty['policy_discovery']['selection'] == 'selected'
    assert empty['policy_discovery']['selected_bytes'] == 0
    assert len(empty['files']) == 2


@pytest.mark.parametrize('raw', ['{', '[]', 'null', '{"claudeMd":3}', '{"claudeMd":null}',
    '{"claudeMd": "a", "claudeMd":"b"}', '{"claudeMd":"a","irrelevant":NaN}',
    '{"claudeMd":"a","irrelevant":Infinity}', '{"claudeMd":"\\ud800"}'])
def test_invalid_policy_never_falls_back_to_a_known_selection(cfg, tmp_path, raw):
    repo = init_git_repo(tmp_path/'project', 'README.md', 'Fixture.\n')
    policy(cfg, claudeMd='Earlier text')
    write(Path(cfg.claude_managed_dir)/'managed-settings.d/20-bad.json', raw)
    record = capture(cfg, repo); validate(record)
    assert record['policy_discovery']['selection'] == 'unknown'
    assert record['policy_discovery']['selected_bytes'] is None
    assert record['policy_discovery']['dropins_status'] == 'failed'
    assert record['files'][0]['loading_paths'][0]['eligibility_reason'] == 'managed_file_policy_incomplete'


def test_policy_aliases_share_field_bytes_and_keep_order(cfg, tmp_path):
    repo = init_git_repo(tmp_path/'project', 'README.md', 'Fixture.\n')
    first = policy(cfg, claudeMd='Shared field.\n')
    middle = policy(cfg, 'managed-settings.d/20-middle.json', claudeMd='Middle.\n')
    last = middle.with_name('30-hardlink.json'); os.link(first, last)
    alias = middle.with_name('40-symlink.json'); alias.symlink_to(first)
    record = capture(cfg, repo); validate(record)
    assert len(record['files']) == 2
    assert record['totals']['bytes'] == len('Shared field.\nMiddle.\n'.encode())
    shared = next(f for f in record['files'] if len(f['aliases']) == 3)
    assert len(shared['loading_paths']) == 3
    assert record['policy_discovery']['selected_source'] == str(alias)


@pytest.mark.parametrize('case', ['broken', 'denied', 'directory', 'bytes', 'entries'])
def test_policy_failures_keep_named_coverage(cfg, tmp_path, monkeypatch, case):
    repo = init_git_repo(tmp_path/'project', 'README.md', 'Fixture.\n')
    target = policy(cfg, claudeMd='Initial text')
    root = target.parent
    if case == 'broken': target.unlink(); target.symlink_to(root/'missing')
    elif case == 'directory': target.unlink(); target.mkdir()
    elif case == 'bytes': monkeypatch.setattr(surfaces, 'MAX_FILE_BYTES', 8)
    elif case == 'entries':
        policy(cfg, 'managed-settings.d/10-a.json', claudeMd='a')
        policy(cfg, 'managed-settings.d/20-b.json', claudeMd='b')
        monkeypatch.setattr(surfaces, 'MAX_DISCOVERY_ENTRIES', 1)
    else:
        original = Path.lstat
        def denied(self, *a, **kw):
            if self == target: raise PermissionError('fixture denial')
            return original(self, *a, **kw)
        monkeypatch.setattr(Path, 'lstat', denied)
    record = capture(cfg, repo); validate(record)
    assert record['policy_discovery']['selection'] == 'unknown'
    assert record['policy_discovery']['failures']
    assert record['status'] == 'partial'


def test_helper_presence_keeps_body_without_running_or_retaining_command(cfg, tmp_path):
    repo = init_git_repo(tmp_path/'project', 'README.md', 'Fixture.\n')
    policy(cfg, claudeMd='Local text', policyHelper={'command':'INVENTED_DO_NOT_RUN'})
    record = capture(cfg, repo); validate(record)
    assert record['policy_discovery']['helper_present'] is True
    assert record['policy_discovery']['selected_bytes'] == len('Local text')
    assert record['files'][0]['loading_paths'][0]['eligibility_reason'] == 'managed_policy_helper_unobserved'
    assert 'INVENTED_DO_NOT_RUN' not in encoded(record)


def test_actual_collector_retains_text_but_cannot_invent_availability(cfg, store, tmp_path, monkeypatch):
    pytest.importorskip('fastapi')
    from fastapi.testclient import TestClient
    from self_improve.dashboard.app import create_app
    repo = init_git_repo(tmp_path/'project', 'README.md', 'Fixture.\n')
    key = known_copy(store, repo)
    proposal, body = delivery(store, cfg, repo)
    target = policy(cfg, claudeMd=body, env={'PRIVATE_EXAMPLE':'do not retain this'})
    result = availability.collect_availability(store, cfg, observed_at='2030-01-01T00:00:00Z')
    assert result['outcomes'] == {'unknown':1}
    assert rows(store)[0]['matches'] == []
    inv = inventory.project_inventory(store, project_key=key)['records'][0]
    assert inv['totals']['ownership']['machine']['bytes'] == len(body.encode())
    archive_id = store.query_one('SELECT id FROM instruction_text_archives')['id']
    target.unlink()
    monkeypatch.setattr(availability, 'inspect_surfaces', lambda *a, **kw: pytest.fail('reader reopened policy'))
    before = list(store.conn.iterdump())
    from self_improve.redact import redact_text
    assert instruction_text.read_archive(store, archive_id)['files'][0]['text'] == redact_text(body)
    with TestClient(create_app(cfg)) as client:
        response = client.get('/api/project-inventory', params={'project_key':key})
        assert response.status_code == 200, response.text
        assert response.json()['records'][0] == inv
    assert before == list(store.conn.iterdump())
    assert store.query_one('SELECT COUNT(*) n FROM llm_calls')['n'] == 0


def test_shared_publication_transaction_rolls_back_policy(cfg, store, tmp_path, monkeypatch):
    repo = init_git_repo(tmp_path/'project', 'README.md', 'Fixture.\n'); known_copy(store, repo)
    policy(cfg, claudeMd='Fixture text')
    before = list(store.conn.iterdump())
    def fail(*a, **kw): raise RuntimeError('fixture publication failure')
    monkeypatch.setattr(instruction_text, 'record_archives', fail)
    with pytest.raises(RuntimeError, match='fixture publication failure'):
        availability.collect_availability(store, cfg, observed_at='2030-01-01T00:00:00Z')
    assert list(store.conn.iterdump()) == before


@pytest.mark.parametrize('mutation', ['selected', 'bytes', 'order', 'field_id', 'eligibility', 'root', 'helper', 'source'])
def test_rehashed_policy_contradictions_are_rejected(cfg, tmp_path, mutation):
    repo = init_git_repo(tmp_path/'project', 'README.md', 'Fixture.\n')
    policy(cfg, claudeMd='First')
    policy(cfg, 'managed-settings.d/20-second.json', claudeMd='Second')
    record = capture(cfg, repo); validate(record)
    p = record['policy_discovery']
    if mutation == 'selected': p['selected_source'] = p['sources'][0]['path']
    elif mutation == 'bytes': p['selected_bytes'] += 1
    elif mutation == 'order': p['sources'].reverse()
    elif mutation == 'field_id': p['sources'][0]['field_id'] = p['sources'][1]['field_id']
    elif mutation == 'eligibility': record['files'][0]['loading_paths'][0]['eligible_prefix_bytes'] = 1
    elif mutation == 'root': p['root'] = '/invented/other'
    elif mutation == 'helper': p['helper_present'] = True
    else: record['files'][0]['loading_paths'][0]['source'] = 'memory'
    with pytest.raises(inventory.InventoryError): validate(record)


def test_identity_refusal_retains_unchecked_policy(cfg, tmp_path):
    surface = dict(files=[], issues=[{'cause':'working_copy_identity_changed'}], profile=surfaces.PROFILE, runtime_loading_verified=False)
    record = inventory.build_inventory(surface=surface, project_key='github:example/fixture', working_copy_path=str(tmp_path), revisions=[], observed_at='2030-01-01T00:00:00Z')
    validate(record)
    assert record['policy_discovery']['selection'] == 'not_checked'
    assert record['policy_discovery']['selected_bytes'] is None


@pytest.mark.parametrize('profile', ['instruction-surfaces/1','instruction-surfaces/2','instruction-surfaces/3','instruction-surfaces/4'])
def test_older_profiles_remain_readable(cfg, tmp_path, profile):
    repo = init_git_repo(tmp_path/'project', 'CLAUDE.md', 'Fixture.\n')
    surface = surfaces.inspect_surfaces(cfg, repo)
    surface['profile'] = profile; surface.pop('policy_discovery', None)
    record = inventory.build_inventory(surface=surface, project_key='github:example/fixture', working_copy_path=str(repo), revisions=[], observed_at='2030-01-01T00:00:00Z')
    assert validate(deepcopy(record)) == record


def test_legacy_context_wording_keeps_immutable_records_valid():
    from self_improve.instruction_context import summarize
    assert summarize([])['meaning'] == ('Physical bytes under the recorded source profile. '
        'Startup candidates, conditional rules and on-demand bodies are separate. '
        'Skill metadata budgets, runtime overrides and session receipt are not measured.')


@pytest.mark.parametrize('route', ['import', 'hardlink'])
def test_raw_policy_cannot_enter_archive_through_an_instruction_alias(cfg, tmp_path, route):
    repo = init_git_repo(tmp_path/'project', 'README.md', 'Fixture.\n')
    target = policy(cfg, claudeMd='Only embedded text', env={'fixture':'RAW_SETTINGS_SENTINEL'}, policyHelper={'command':'HELPER_COMMAND_SENTINEL'})
    if route == 'import': write(target.parent/'CLAUDE.md', '@./managed-settings.json\n')
    else: os.link(target, repo/'CLAUDE.md')
    surface = surfaces.inspect_surfaces(cfg, repo)
    inv = inventory.build_inventory(surface=surface, project_key='github:example/fixture',working_copy_path=str(repo), revisions=[], observed_at='2030-01-01T00:00:00Z')
    validate(inv)
    archive = instruction_text.build_archive(surface,inv)
    assert 'RAW_SETTINGS_SENTINEL' not in encoded(archive)
    assert 'HELPER_COMMAND_SENTINEL' not in encoded(archive)
    assert any(i['cause']=='policy_container_not_instruction' for i in inv['issues'])


def test_overflow_float_is_not_strict_json(cfg,tmp_path):
    repo=init_git_repo(tmp_path/'project','README.md','Fixture.\n')
    write(Path(cfg.claude_managed_dir)/'managed-settings.json','{"claudeMd":"Text","nested":{"x":1e999}}')
    inv=capture(cfg,repo); validate(inv)
    assert inv['policy_discovery']['selection']=='unknown'


@pytest.mark.parametrize('field', ['container_hash','helper_present'])
def test_shared_container_metadata_must_agree_across_aliases(cfg,tmp_path,field):
    repo=init_git_repo(tmp_path/'project','README.md','Fixture.\n')
    target=policy(cfg,claudeMd='Text')
    alias=target.parent/'managed-settings.d/20-alias.json'; alias.parent.mkdir(); alias.symlink_to(target)
    inv=capture(cfg,repo); validate(inv)
    p=inv['policy_discovery']; s=p['sources'][-1]; path=inv['files'][0]['loading_paths'][-1]
    if field=='container_hash': s[field]='0'*64;path[field]='0'*64
    else: s[field]=True;p[field]=True;path['eligibility_reason']='managed_policy_helper_unobserved'
    with pytest.raises(inventory.InventoryError): validate(inv)


@pytest.mark.parametrize('raw', ['', ' \n\t'])
def test_empty_native_policy_file_preserves_the_previous_field(cfg,tmp_path,raw):
    repo=init_git_repo(tmp_path/'project','README.md','Fixture.\n')
    main=policy(cfg,claudeMd='Earlier text')
    write(main.parent/'managed-settings.d/20-empty.json',raw)
    inv=capture(cfg,repo); validate(inv)
    assert inv['policy_discovery']['selection']=='selected'
    assert inv['policy_discovery']['selected_source']==str(main)
    assert inv['policy_discovery']['dropins_status']=='observed'
    main.write_text(raw)
    inv=capture(cfg,repo); validate(inv)
    assert inv['policy_discovery']['selection']=='absent'
    assert inv['policy_discovery']['main_status']=='observed'


@pytest.mark.parametrize('route', ['import','hardlink','symlink'])
def test_incomplete_policy_discovery_cannot_expose_whole_container(cfg,tmp_path,monkeypatch,route):
    repo=init_git_repo(tmp_path/'project','README.md','Fixture.\n')
    target=policy(cfg,'managed-settings.d/20-target.json',claudeMd='Text',env={'private':'CAPPED_SETTINGS_SENTINEL'})
    policy(cfg,'managed-settings.d/10-other.json',claudeMd='Other')
    if route=='import': write(repo/'CLAUDE.md','@'+str(target)+'\n')
    elif route=='symlink': (repo/'CLAUDE.md').symlink_to(target)
    else: os.link(target,repo/'CLAUDE.md')
    monkeypatch.setattr(surfaces,'MAX_DISCOVERY_ENTRIES',1)
    surface=surfaces.inspect_surfaces(cfg,repo)
    inv=inventory.build_inventory(surface=surface,project_key='github:example/fixture',working_copy_path=str(repo),revisions=[],observed_at='2030-01-01T00:00:00Z')
    validate(inv)
    assert inv['policy_discovery']['selection']=='unknown'
    assert 'CAPPED_SETTINGS_SENTINEL' not in encoded(instruction_text.build_archive(surface,inv))


def test_capped_symlinked_dropin_directory_cannot_expose_its_target(cfg,tmp_path,monkeypatch):
    repo=init_git_repo(tmp_path/'project','README.md','Fixture.\n')
    root=Path(cfg.claude_managed_dir);root.mkdir()
    external=tmp_path/'policy-files';external.mkdir()
    (root/'managed-settings.d').symlink_to(external,target_is_directory=True)
    write(external/'10-first.json','{}')
    target=write(external/'20-second.json',json.dumps({'claudeMd':'Text','env':{'x':'DIRECTORY_ALIAS_SECRET'}}))
    (repo/'CLAUDE.md').symlink_to(target)
    monkeypatch.setattr(surfaces,'MAX_DISCOVERY_ENTRIES',1)
    surface=surfaces.inspect_surfaces(cfg,repo)
    inv=inventory.build_inventory(surface=surface,project_key='github:example/fixture',working_copy_path=str(repo),revisions=[],observed_at='2030-01-01T00:00:00Z')
    validate(inv)
    assert 'DIRECTORY_ALIAS_SECRET' not in encoded(instruction_text.build_archive(surface,inv))


def test_policy_identity_change_refuses_collection_before_publication(cfg,store,tmp_path,monkeypatch):
    repo=init_git_repo(tmp_path/'project','README.md','Fixture.\n');known_copy(store,repo)
    target=policy(cfg,claudeMd='Old text')
    alias=write(repo/'CLAUDE.md','Regular Markdown before replacement.\n')
    original=Path.stat; replaced=False
    def race(self,*a,**kw):
        nonlocal replaced
        if self==alias and not replaced:
            replaced=True
            target.unlink();target.write_text(json.dumps({'claudeMd':'New text','env':{'x':'RACING_RAW_SETTING_SENTINEL'}}))
            alias.unlink();os.link(target,alias)
        return original(self,*a,**kw)
    monkeypatch.setattr(Path,'stat',race)
    before=list(store.conn.iterdump())
    with pytest.raises(availability.AvailabilityError,match='policy source changed'):
        availability.collect_availability(store,cfg,observed_at='2030-01-01T00:00:00Z')
    assert before==list(store.conn.iterdump())
