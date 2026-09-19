"""Real temporary collection supplies measurements and history, never private state."""
import json
import os
from pathlib import Path

import pytest

from self_improve import instruction_inventory as inventory, rule_availability as availability
from self_improve.mining_history import digest, encoded
from tests.test_rule_availability import cfg, store, known_copy, init_git_repo


def collect(store, cfg, day):
    return availability.collect_availability(store, cfg, observed_at=f'2030-01-{day:02d}T00:00:00Z')


def test_actual_collection_separates_unicode_aliases_and_other_markdown(cfg, store, tmp_path):
    repo = init_git_repo(tmp_path/'project', 'AGENTS.md', 'é\n')
    (repo/'CLAUDE.md').symlink_to(repo/'AGENTS.md')
    os.link(repo/'AGENTS.md', repo/'instruction-alias.md')
    (repo/'report.md').write_text('x'*50000)
    (repo/'report-alias.md').symlink_to(repo/'report.md')
    (repo/'node_modules').mkdir(); (repo/'node_modules/hidden.md').write_text('excluded')
    key = known_copy(store, repo); collect(store, cfg, 1)
    record = inventory.project_inventory(store, project_key=key)['records'][0]
    m = record['measurements']
    assert m['instructions']['totals'] == {'files': 1, 'bytes': 3, 'characters': 2}
    assert m['other_markdown']['totals'] == {'files': 1, 'bytes': 50000, 'characters': 50000}
    assert m['other_markdown']['status'] == 'measured'
    assert any(x['cause']=='excluded_directory' and x['path'].endswith('node_modules') for x in m['other_markdown']['exclusions'])
    assert len(m['other_markdown']['deduplicated']) >= 3
    assert record['totals']['bytes'] == 3
    from self_improve.instruction_context import project_summary
    assert project_summary(store, project_key=key)['measurements'] == m
    assert store.query_one('SELECT COUNT(*) n FROM llm_calls')['n'] == 0


def test_history_pages_are_copy_bound_and_compare_adjacent_observations(cfg, store, tmp_path):
    repo = init_git_repo(tmp_path/'project', 'AGENTS.md', '')
    key = known_copy(store, repo)
    for day in range(1, 24):
        (repo/'AGENTS.md').write_text('é'*day+'\n'); collect(store, cfg, day)
    copy_id = inventory.project_inventory(store, project_key=key)['records'][0]['working_copy_id']
    first = inventory.context_history(store, project_key=key, working_copy_id=copy_id)
    assert len(first['records']) == 20 and first['count'] == 23
    assert first['records'][0]['comparison']['instructions']['delta'] == {'bytes': 2, 'characters': 1, 'files': 0}
    second = inventory.context_history(store, project_key=key, working_copy_id=copy_id, cursor=first['next_cursor'])
    assert len(second['records']) == 3 and second['next_cursor'] is None
    assert second['records'][-1]['comparison']['instructions']['reason'] == 'no_prior_observation'
    with pytest.raises(inventory.InventoryError, match='Invalid inventory'):
        inventory.context_history(store, project_key=key, working_copy_id='0'*64, cursor=first['next_cursor'])
    collect(store, cfg, 24)
    with pytest.raises(inventory.InventoryError, match='Invalid inventory'):
        inventory.context_history(store, project_key=key, working_copy_id=copy_id, cursor=first['next_cursor'])


def test_optional_walk_failures_do_not_erase_available_rule(cfg, store, tmp_path, monkeypatch):
    from self_improve import instruction_metrics as metrics
    from tests.test_rule_availability import delivery, run_git, rows
    repo = init_git_repo(tmp_path/'project', 'AGENTS.md', '# Human\n')
    key = known_copy(store, repo); delivery(store, cfg, repo)
    run_git(['merge', '--ff-only', cfg.project_branch_name], repo)
    (repo/'bad.md').write_bytes(b'\xffsecret must not appear in cause')
    (repo/'locked.md').write_text('Locked report')
    (repo/'changed.md').write_text('Changing report')
    original = metrics._read
    def read(path, limit):
        if path.name == 'locked.md': raise PermissionError('fixture')
        if path.name == 'changed.md': raise ValueError('file_changed_during_observation')
        return original(path, limit)
    monkeypatch.setattr(metrics, '_read', read)
    collect(store, cfg, 1)
    record = inventory.project_inventory(store, project_key=key)['records'][0]
    assert record['status'] == 'recorded' and rows(store)[-1]['status'] == 'available'
    other = record['measurements']['other_markdown']
    assert other['status'] == 'partial' and other['totals']['bytes'] == 0
    assert {i['cause'] for i in other['issues']} == {'file_unreadable:UnicodeDecodeError', 'file_unreadable:PermissionError', 'file_changed_during_observation'}
    assert 'secret must' not in json.dumps(other)


@pytest.mark.parametrize('cap,value,cause', [('MAX_ENTRIES',1,'entry_limit'),('MAX_FILES',0,'file_count_limit'),('MAX_DEPTH',0,'depth_limit'),('MAX_FILE_BYTES',1,'file_byte_limit'),('MAX_TOTAL_BYTES',1,'total_byte_limit')])
def test_walk_caps_remain_partial_and_name_omissions(cfg, store, tmp_path, monkeypatch, cap, value, cause):
    from self_improve import instruction_metrics as metrics
    repo = init_git_repo(tmp_path/'project', 'AGENTS.md', 'é\n'); key=known_copy(store,repo)
    (repo/'report.md').write_text('large')
    (repo/'deep').mkdir(); (repo/'deep/report.md').write_text('nested')
    monkeypatch.setattr(metrics, cap, value)
    collect(store,cfg,1)
    m=inventory.project_inventory(store,project_key=key)['records'][0]['measurements']
    assert m['instructions']['totals']['characters']==2
    assert m['other_markdown']['status']=='partial'
    assert cause in {i['cause'] for i in m['other_markdown']['issues']}


def test_empty_external_links_and_identity_refusal_are_distinct(cfg, store, tmp_path):
    from tests.test_rule_availability import run_git
    repo=init_git_repo(tmp_path/'project','README.txt','not markdown');key=known_copy(store,repo)
    outside=tmp_path/'external.md';outside.write_text('excluded')
    (repo/'external.md').symlink_to(outside)
    (repo/'external-dir').symlink_to(tmp_path, target_is_directory=True)
    collect(store,cfg,1)
    m=inventory.project_inventory(store,project_key=key)['records'][0]['measurements']
    assert m['instructions']['totals']==m['other_markdown']['totals']=={'files':0,'bytes':0,'characters':0}
    assert m['other_markdown']['status']=='measured'
    assert {'external_file_symlink','directory_symlink'} <= {i['cause'] for i in m['other_markdown']['exclusions']}
    run_git(['remote','add','origin','https://example.test/changed/project.git'],repo)
    collect(store,cfg,2)
    m=inventory.project_inventory(store,project_key=key)['records'][0]['measurements']
    assert m['other_markdown']['status']=='unavailable' and m['other_markdown']['totals'] is None


def replace_record(store, record):
    old=record['id']; record['id']=digest({k:v for k,v in record.items() if k!='id'})
    store.conn.execute('DELETE FROM instruction_inventories WHERE id=?',(old,))
    store.insert('instruction_inventories',{**{k:record[k] for k in ('id','project_key','working_copy_id','observed_at','run_id','status')},'record_json':encoded(record),'record_hash':digest(record)})
    store.commit()


def test_history_legacy_conflicts_partial_and_incompatible_are_not_zero(cfg,store,tmp_path,monkeypatch):
    from self_improve import instruction_metrics as metrics
    repo=init_git_repo(tmp_path/'project','AGENTS.md','a\n');key=known_copy(store,repo)
    collect(store,cfg,1)
    record=inventory.project_inventory(store,project_key=key)['records'][0];copy_id=record['working_copy_id']
    del record['measurements'];replace_record(store,record)
    collect(store,cfg,2)
    history=lambda:inventory.context_history(store,project_key=key,working_copy_id=copy_id)
    assert history()['records'][0]['comparison']['instructions']['reason']=='measurement_not_recorded'
    (repo/'AGENTS.md').write_text('b\n');collect(store,cfg,2)
    collect(store,cfg,3)
    page=history(); assert page['records'][1]['status']=='conflicting'
    assert len(page['records'][1]['observations'])==2
    assert page['records'][0]['comparison']['instructions']['reason']=='conflicting_observations'
    collect(store,cfg,4)
    assert history()['records'][0]['comparison']['instructions']['delta']['bytes']==0
    monkeypatch.setattr(metrics,'MAX_DEPTH',2);collect(store,cfg,5)
    comparison=history()['records'][0]['comparison']
    assert comparison['instructions']['computable']
    assert comparison['other_markdown']['reason']=='incompatible_measurement_scope'
    (repo/'bad.md').write_bytes(b'\xff');collect(store,cfg,6)
    comparison=history()['records'][0]['comparison']
    assert comparison['instructions']['computable']
    assert comparison['other_markdown']['reason']=='incomplete_measurement'
    record=inventory.project_inventory(store,project_key=key)['records'][0]
    record['limits']['max_import_depth']+=1;replace_record(store,record)
    assert history()['records'][0]['comparison']['instructions']['reason']=='incompatible_measurement_scope'


def test_measurements_api_copy_readonly_replay_and_corruption(cfg,store,tmp_path,monkeypatch):
    import sqlite3
    from fastapi.testclient import TestClient
    from self_improve.dashboard.app import create_app
    from self_improve import instruction_metrics as metrics
    repo=init_git_repo(tmp_path/'project','AGENTS.md','é\n');key=known_copy(store,repo)
    collect(store,cfg,1);collect(store,cfg,1)
    assert store.query_one('SELECT COUNT(*) n FROM instruction_inventories')['n']==1
    record=inventory.project_inventory(store,project_key=key)['records'][0];copy_id=record['working_copy_id']
    backup=tmp_path/'copy.db';connection=sqlite3.connect(backup);store.conn.backup(connection);connection.close()
    before=backup.read_bytes()
    def forbidden(*a,**kw):raise AssertionError('reader collected files')
    monkeypatch.setattr(metrics,'collect',forbidden)
    with TestClient(create_app(cfg,db_path=backup)) as client:
        params={'project_key':key,'working_copy_id':copy_id}
        response=client.get('/api/project-context-history',params=params);assert response.status_code==200,response.text
        assert response.json()['records'][0]['measurements']['instructions']['totals']['characters']==2
        assert client.get('/api/project-context-history',params={**params,'cursor':'invalid'}).status_code==400
        assert client.get('/api/project-context-history',params={**params,'limit':0}).status_code==400
        assert client.get('/api/project-context-history',params={**params,'working_copy_id':'0'*64}).json()['count']==0
    assert backup.read_bytes()==before
    record['measurements']['instructions']['totals']['characters']=999;replace_record(store,record)
    with pytest.raises(inventory.InventoryError,match='measurement totals'):
        inventory.context_history(store,project_key=key,working_copy_id=copy_id)


def test_new_measurements_rollback_with_later_archive_failure(cfg,store,tmp_path,monkeypatch):
    from self_improve import instruction_text
    repo=init_git_repo(tmp_path/'project','AGENTS.md','é\n');known_copy(store,repo)
    before=list(store.conn.iterdump())
    def fail(*a,**kw):raise RuntimeError('fixture archive failure')
    monkeypatch.setattr(instruction_text,'record_archives',fail)
    with pytest.raises(RuntimeError,match='fixture archive failure'):collect(store,cfg,1)
    assert list(store.conn.iterdump())==before


def test_actual_read_detects_file_change_and_incomplete_instruction_classification(cfg,store,tmp_path,monkeypatch):
    from self_improve import instruction_metrics as metrics
    repo=init_git_repo(tmp_path/'project','AGENTS.md','@missing.md\n');key=known_copy(store,repo)
    (repo/'CLAUDE.md').symlink_to(repo/'AGENTS.md')
    report=repo/'report.md';report.write_text('before')
    original=metrics.os.fdopen
    class ChangingRead:
        def __init__(self,handle):self.handle=handle
        def __enter__(self):self.handle.__enter__();return self
        def __exit__(self,*args):return self.handle.__exit__(*args)
        def fileno(self):return self.handle.fileno()
        def read(self,*args):
            raw=self.handle.read(*args)
            if raw==b'before':report.write_text('changed during this read')
            return raw
    monkeypatch.setattr(metrics.os,'fdopen',lambda *a,**kw:ChangingRead(original(*a,**kw)))
    collect(store,cfg,1)
    record=inventory.project_inventory(store,project_key=key)['records'][0]
    m=record['measurements']
    assert m['instructions']['status']=='partial'
    assert {'file_changed_during_observation','instruction_classification_incomplete'} <= {i['cause'] for i in m['other_markdown']['issues']}
    assert m['other_markdown']['totals']['bytes']==0


def test_nested_cwd_walks_repository_root_and_excludes_nested_repositories(cfg,store,tmp_path):
    repo=init_git_repo(tmp_path/'project','AGENTS.md','é\n')
    (repo/'report.md').write_text('parent report')
    cwd=repo/'src';cwd.mkdir()
    init_git_repo(repo/'vendor/other','report.md','belongs to another project')
    key=known_copy(store,cwd);collect(store,cfg,1)
    record=inventory.project_inventory(store,project_key=key)['records'][0]
    other=record['measurements']['other_markdown']
    assert other['totals']['bytes']==len('parent report')
    assert other['root']==str(repo)
    assert any(x['cause']=='nested_repository' for x in other['exclusions'])
