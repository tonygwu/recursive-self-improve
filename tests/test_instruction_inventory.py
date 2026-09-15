"""Invented files reach the real collector and retained ownership reader."""
import json

import pytest

from self_improve import instruction_inventory as inventory
from self_improve import rule_availability as availability
from self_improve.store import Store
from tests.test_rule_availability import cfg, store, known_copy, delivery, init_git_repo, run_git


def test_real_collection_counts_human_managed_and_complete_machine_units(cfg, store, tmp_path):
    repo = init_git_repo(tmp_path/'project', 'AGENTS.md', '# Human fixture\nUnmarked policy.\n')
    key = known_copy(store, repo)
    proposal, text = delivery(store, cfg, repo)
    run_git(['merge', '--ff-only', cfg.project_branch_name], repo)
    (repo/'CLAUDE.md').symlink_to(repo/'AGENTS.md')
    result = availability.collect_availability(store, cfg, observed_at='2030-01-01T00:00:00Z')
    page = inventory.project_inventory(store, project_key=key)
    record = page['records'][0]
    assert record['totals']['files'] == 1
    assert record['totals']['ownership']['machine']['bytes'] == len(text.encode())
    assert record['totals']['ownership']['human_managed']['lines'] == 2
    assert record['files'][0]['units'][0]['revision_ids']
    assert len(record['files'][0]['loading_paths']) == 2
    assert record['runtime_loading_verified'] is False
    assert result['inventory_observations'] == 1
    from self_improve.availability_reporting import summary
    assert next(m['count'] for m in summary(result,owner='fixture')['metrics'] if m['key']=='inventory_observations')==1
    assert text not in json.dumps(record)  # Only non-content evidence is retained.
    assert availability.collect_availability(store, cfg, observed_at='2030-01-01T00:00:00Z') == result
    assert store.query_one('SELECT COUNT(*) n FROM instruction_inventories')['n'] == 1


def test_edited_and_unknown_markers_and_copies_keep_distinct_ownership(cfg, store, tmp_path):
    a = init_git_repo(tmp_path/'copy-a', 'AGENTS.md', '# Human\n')
    b = init_git_repo(tmp_path/'copy-b', 'AGENTS.md', '# Human\n')
    for repo in (a,b): run_git(['remote','add','origin','https://example.test/owner/fixture.git'],repo)
    key = known_copy(store,a); assert known_copy(store,b) == key
    proposal,text = delivery(store,cfg,a)
    run_git(['merge','--ff-only',cfg.project_branch_name],a)
    (a/'AGENTS.md').write_text('# Human\n'+text.replace('readable','edited')+'- Unknown. <!-- si:unknown-fixture -->\n')
    availability.collect_availability(store,cfg,observed_at='2030-01-01T00:00:00Z')
    records = {r['working_copy']['normalized_path']:r for r in inventory.project_inventory(store,project_key=key)['records']}
    counts = records[str(a)]['totals']['ownership']
    assert counts['edited']['lines'] == 1
    assert counts['unknown']['lines'] == 1
    assert counts['machine']['lines'] == 0
    assert records[str(b)]['totals']['ownership']['human_managed']['lines'] == 1


def test_no_deliveries_still_collects_inventory_and_global_scoped_imports(cfg,store,tmp_path):
    repo = init_git_repo(tmp_path/'project','CLAUDE.md','@first.md\n')
    (repo/'first.md').write_text('@second.md\n')
    (repo/'second.md').write_text('Shared human-managed instruction.\n')
    scoped=repo/'.claude/rules';scoped.mkdir(parents=True)
    (scoped/'paths.md').write_text('---\npaths: ["src/**"]\n---\nScoped instruction.\n')
    from pathlib import Path
    global_file=Path(cfg.global_claude_md);global_file.parent.mkdir(parents=True);global_file.write_text('Global instruction.\n')
    key=known_copy(store,repo)
    availability.collect_availability(store,cfg,observed_at='2030-01-01T00:00:00Z')
    record=inventory.project_inventory(store,project_key=key)['records'][0]
    assert record['totals']['files'] == 5
    assert {p['scope']['kind'] for f in record['files'] for p in f['loading_paths']} == {'global','project_always_loaded','path_scoped'}
    assert any(len(p['import_chain'])==2 for f in record['files'] for p in f['loading_paths'])
    assert record['totals']['ownership']['machine']['bytes'] == 0


def test_reader_uses_copied_store_and_corruption_and_missing_schema_are_explicit(cfg,store,tmp_path,monkeypatch):
    repo=init_git_repo(tmp_path/'project','AGENTS.md','# Human\n');key=known_copy(store,repo)
    availability.collect_availability(store,cfg,observed_at='2030-01-01T00:00:00Z')
    backup=tmp_path/'copy.db'
    import sqlite3
    conn=sqlite3.connect(backup);store.conn.backup(conn);conn.close()
    def forbidden(*args,**kwargs): raise AssertionError('Reader attempted filesystem collection')
    monkeypatch.setattr(availability,'inspect_surfaces',forbidden)
    reader = Store(str(backup),read_only=True)
    try:
        assert inventory.project_inventory(reader,project_key=key)['count']==1
    finally:
        reader.close()
    row=store.query_one('SELECT * FROM instruction_inventories')
    store.update('instruction_inventories','id',row['id'],{'record_json':'[]'});store.commit()
    with pytest.raises(inventory.InventoryError,match=row['id']): inventory.project_inventory(store,project_key=key)
    store.conn.execute("DELETE FROM schema_migrations WHERE name=?",(inventory.MIGRATION,));store.commit()
    assert inventory.project_inventory(store,project_key=key)['reason']=='schema_unavailable'
    assert not store.query_one('SELECT name FROM schema_migrations WHERE name=?',(inventory.MIGRATION,))


def test_inventory_publication_failure_rolls_back_whole_collection(cfg,store,tmp_path,monkeypatch):
    repo=init_git_repo(tmp_path/'project','AGENTS.md','# Human\n');known_copy(store,repo);delivery(store,cfg,repo)
    original=store.insert
    def fail(table,row):
        if table=='instruction_inventories': raise RuntimeError('invented inventory interruption')
        return original(table,row)
    monkeypatch.setattr(store,'insert',fail)
    with pytest.raises(RuntimeError,match='inventory interruption'):
        availability.collect_availability(store,cfg,observed_at='2030-01-01T00:00:00Z')
    for table in ('rule_revisions','rule_availability_observations','rule_availability_collections','instruction_inventories'):
        assert store.query_one(f'SELECT COUNT(*) n FROM {table}')['n']==0


def test_positive_empty_inventory_differs_from_missing_and_identity_change(cfg,store,tmp_path):
    repo=init_git_repo(tmp_path/'project','README.md','# Not an instruction\n');key=known_copy(store,repo)
    assert inventory.project_inventory(store,project_key=key)['reason']=='no_inventory_observations'
    availability.collect_availability(store,cfg,observed_at='2030-01-01T00:00:00Z')
    record=inventory.project_inventory(store,project_key=key)['records'][0]
    assert record['status']=='recorded' and record['totals']['bytes']==0
    run_git(['remote','add','origin','https://example.test/changed/repo.git'],repo)
    availability.collect_availability(store,cfg,observed_at='2030-01-02T00:00:00Z')
    record=inventory.project_inventory(store,project_key=key)['records'][0]
    assert record['status']=='partial' and record['totals']['bytes']==0
    assert record['issues'][0]['cause']=='working_copy_identity_changed'


def test_pages_conflicting_times_and_api_keep_copy_ownership(cfg,store,tmp_path):
    from fastapi.testclient import TestClient
    from self_improve.dashboard.app import create_app
    for index in range(3):
        repo=init_git_repo(tmp_path/f'copy-{index}','AGENTS.md','# Human\n')
        run_git(['remote','add','origin','https://example.test/owner/fixture.git'],repo)
        key=known_copy(store,repo)
    availability.collect_availability(store,cfg,observed_at='2030-01-01T00:00:00Z')
    first=inventory.project_inventory(store,project_key=key,limit=2)
    second=inventory.project_inventory(store,project_key=key,limit=2,cursor=first['next_cursor'])
    assert first['count']==3 and len(first['records'])==2 and len(second['records'])==1 and second['next_cursor'] is None
    assert len({r['working_copy_id'] for r in first['records']+second['records']})==3
    with pytest.raises(inventory.InventoryError,match='different project'):
        inventory.project_inventory(store,project_key='different',cursor=first['next_cursor'])
    (repo/'AGENTS.md').write_text('# Changed at same timestamp\n')
    availability.collect_availability(store,cfg,observed_at='2030-01-01T00:00:00Z')
    assert len([r for r in inventory.project_inventory(store,project_key=key)['records'] if r['status']=='conflicting'])==1
    copy=tmp_path/'reader.db';reader=Store(copy);store.conn.backup(reader.conn);reader.close()
    store.conn.execute('DELETE FROM instruction_inventories');store.commit()
    before=copy.read_bytes()
    with TestClient(create_app(cfg,db_path=copy)) as client:
        assert client.get('/api/project-inventory',params={'project_key':key}).json()['count']==3
        assert client.get('/api/project-inventory',params={'project_key':key,'limit':0}).status_code==400
    assert copy.read_bytes()==before
    assert store.query_one('SELECT COUNT(*) n FROM llm_calls')['n']==0


def test_rebuild_exports_and_keeps_inventory_without_reading_source_again(cfg,store,tmp_path):
    from self_improve.rebuild import rebuild_state
    repo=init_git_repo(tmp_path/'project','AGENTS.md','# Human\n');key=known_copy(store,repo)
    availability.collect_availability(store,cfg,observed_at='2030-01-01T00:00:00Z')
    before=store.query('SELECT * FROM instruction_inventories ORDER BY id')
    destination=tmp_path/'private-backup'
    rebuild_state(store,export_path=destination)
    assert json.loads((destination/'preserved.json').read_text())['instruction_inventories']==before
    assert store.query('SELECT * FROM instruction_inventories ORDER BY id')==before
    assert inventory.project_inventory(store,project_key=key)['count']==1


def test_complete_multiline_unit_and_utf8_bytes_and_claimed_revision_corruption(cfg,store,tmp_path):
    from self_improve import apply
    from self_improve.mining_history import digest,encoded
    from tests.test_apply import insert_proposal,make_diff
    repo=init_git_repo(tmp_path/'project','AGENTS.md','# Human café\n');key=known_copy(store,repo)
    proposal=insert_proposal(store,target=repo/'AGENTS.md',diff='',target_kind='project_agents_md')
    text='- **Whole rule.** Keep café.\n- Include every line.\n- End. <!-- si:'+proposal['learning_id']+' -->\n'
    before=(repo/'AGENTS.md').read_text()
    store.update('proposals','id',proposal['id'],{'diff_unified':make_diff(before,before+text)});store.commit()
    apply.apply_proposal(store,cfg,store.query_one('SELECT * FROM proposals WHERE id=?',(proposal['id'],)))
    run_git(['merge','--ff-only',cfg.project_branch_name],repo)
    availability.collect_availability(store,cfg,observed_at='2030-01-01T00:00:00Z')
    record=inventory.project_inventory(store,project_key=key)['records'][0]
    assert record['totals']['ownership']['machine']=={'lines':3,'bytes':len(text.encode())}
    assert record['files'][0]['units'][0]['start_line']==2
    record['files'][0]['units'][0]['content_hash']='0'*64
    old=record['id'];record['id']=digest({k:v for k,v in record.items() if k!='id'})
    store.conn.execute('DELETE FROM instruction_inventories WHERE id=?',(old,))
    store.insert('instruction_inventories',{**{k:record[k] for k in ('id','project_key','working_copy_id','observed_at','status','run_id')},'record_json':encoded(record),'record_hash':digest(record)});store.commit()
    with pytest.raises(inventory.InventoryError,match='retained revision'):
        inventory.project_inventory(store,project_key=key)


def test_inventory_writer_requires_transaction_and_damaged_schema_is_not_empty(cfg,store,tmp_path):
    with pytest.raises(inventory.InventoryError,match='active write transaction'):
        inventory.record_inventories(store,records=[])
    store.conn.execute('DROP TABLE instruction_inventories');store.commit()
    with pytest.raises(inventory.InventoryError,match='applied inventory schema is damaged'):
        inventory.project_inventory(store,project_key='fixture')


def test_modified_whole_file_is_not_relabelled_as_human_authorship(cfg,store,tmp_path):
    from self_improve import apply
    from tests.test_apply import insert_proposal,make_diff
    repo=init_git_repo(tmp_path/'project','AGENTS.md','# Human\n');key=known_copy(store,repo)
    target=repo/'.claude/rules/fixture.md';target.parent.mkdir(parents=True)
    proposal=insert_proposal(store,target=target,diff='',target_kind='rule_file')
    text='# Generated fixture\n- Rule. <!-- si:'+proposal['learning_id']+' -->\n'
    store.update('proposals','id',proposal['id'],{'diff_unified':make_diff('',text)});store.commit()
    apply.apply_proposal(store,cfg,store.query_one('SELECT * FROM proposals WHERE id=?',(proposal['id'],)))
    run_git(['merge','--ff-only',cfg.project_branch_name],repo)
    target.write_text(text+'Edited footer.\n')
    availability.collect_availability(store,cfg,observed_at='2030-01-01T00:00:00Z')
    file=next(f for f in inventory.project_inventory(store,project_key=key)['records'][0]['files'] if f['path']==str(target))
    assert file['ownership']['edited']=={'lines':3,'bytes':len(target.read_bytes())}
    assert file['ownership']['human_managed']['lines']==0
    target.write_text('Replacement with unknown ownership.\n')
    availability.collect_availability(store,cfg,observed_at='2030-01-02T00:00:00Z')
    file=next(f for f in inventory.project_inventory(store,project_key=key)['records'][0]['files'] if f['path']==str(target))
    assert file['ownership']['unknown']['lines']==1
    assert file['ownership']['human_managed']['lines']==0


def test_inventory_ui_pagination_focus_late_response_and_unknown_counts(tmp_path):
    import shutil,subprocess
    from pathlib import Path
    from self_improve.dashboard import app
    node=shutil.which('node');assert node
    (tmp_path/'app.mjs').write_bytes((Path(app.__file__).parent/'static/app.js').read_bytes())
    probe=tmp_path/'probe.mjs'
    probe.write_text(r'''
import assert from 'node:assert/strict';
const m=await import('./app.mjs');
const nodes=new Map();
const body={_html:'',get innerHTML(){return this._html;},set innerHTML(value){this._html=value;document.activeElement=null;},querySelectorAll:()=>[]};
globalThis.document={activeElement:null,getElementById(id){
 if(id==='project-detail-body')return body;
 if(!body.innerHTML.includes('id="'+id+'"'))return null;
 if(!nodes.has(id))nodes.set(id,{id,focus(){document.activeElement=this;}});
 return nodes.get(id);
}};
const pending=[];
globalThis.fetch=url=>new Promise(resolve=>pending.push({url,resolve}));
const answer=(i,data)=>pending[i].resolve({ok:true,json:async()=>data});
m.state.inspectorKind='project';m.state.inspectorTab='inventory';m.state.selectedProject='project-a';
m.state.projects={rows:[{project_key:'project-a'},{project_key:'project-b'}]};
const first=m.loadProjectInventory('project-a');
answer(0,{project_key:'project-a',records:[],count:2,next_cursor:'next'});await first;
document.getElementById('inventory-older').focus();
const more=m.loadProjectInventory('project-a',{older:true});
assert.match(pending[1].url,/cursor=next/);
answer(1,{project_key:'project-a',records:[],count:2,next_cursor:null});await more;
assert.equal(document.activeElement?.id,'inventory-status');
const stale=m.loadProjectInventory('project-a',{refresh:true});
m.state.selectedProject='project-b';
const current=m.loadProjectInventory('project-b');
answer(3,{project_key:'project-b',records:[],count:0,next_cursor:null,reason:'no_inventory_observations'});await current;
const shown=body.innerHTML;
answer(2,{project_key:'project-a',records:[],count:2,next_cursor:null});await stale;
assert.equal(body.innerHTML,shown);
assert.match(shown,/ownership is unknown/);
assert.match(m.renderProjectInventory({loaded:true,count:null,reason:'schema_unavailable',records:[]}),/count unknown/);
console.log('INVENTORY_UI_OK');
''')
    result=subprocess.run([node,str(probe)],capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stderr
    assert result.stdout.strip()=='INVENTORY_UI_OK'
