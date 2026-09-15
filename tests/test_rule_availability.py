"""Actual delivered snapshots and working-copy files determine availability."""
from dataclasses import replace
from pathlib import Path
import json

import pytest

from self_improve import apply, rule_availability as availability, rule_revisions as revisions
from self_improve.config import Config
from self_improve.project_identity import resolve
from self_improve.scan_observations import working_copy_identity
from self_improve.store import Store
from tests.test_apply import store, insert_proposal, init_git_repo, make_diff, run_git


@pytest.fixture
def cfg(tmp_path):
    return Config(state_dir=str(tmp_path/'state'), global_claude_md=str(tmp_path/'home/CLAUDE.md'),
                  codex_global_agents_md=str(tmp_path/'codex/AGENTS.md'), skills_dir=str(tmp_path/'home/skills'),
                  claude_projects_dir=str(tmp_path/'transcripts/claude'), codex_sessions_dir=str(tmp_path/'transcripts/codex'),
                  codex_archived_dir=str(tmp_path/'transcripts/archived'))


def known_copy(store, repo):
    key = resolve(str(repo), use_gh=False).key
    store.insert('sessions', {'file_path': str(repo/'invented.jsonl'), 'source': 'codex',
                             'project_key': key, 'project_path': str(repo), 'first_ts': '2026-01-01T00:00:00Z'})
    store.commit()
    return key


def delivery(store, cfg, repo, *, filename='AGENTS.md', target_kind='project_agents_md'):
    target = repo/filename
    old = target.read_text() if target.exists() else ''
    p = insert_proposal(store, target=target, diff='', target_kind=target_kind)
    text = '- Keep the invented fixture readable. <!-- si:'+p['learning_id']+' -->\n'
    new = old+text
    store.update('proposals', 'id', p['id'], {'diff_unified': make_diff(old,new)})
    store.commit()
    p = store.query_one('SELECT * FROM proposals WHERE id=?',(p['id'],))
    assert apply.apply_proposal(store,cfg,p)['outcome'] == 'applied'
    return p,text


def rows(store):
    return [availability._validate_observation(revisions.read_record(row,'rule_availability_observations'))
            for row in store.query('SELECT * FROM rule_availability_observations ORDER BY observed_at,id')]


def observe(store,cfg,day,run_id=''):
    return availability.collect_availability(store,cfg,observed_at=f'2030-01-{day:02d}T00:00:00Z',run_id=run_id)


def test_branch_delivery_does_not_become_available_until_actual_checkout_integration(cfg,store,tmp_path):
    repo=init_git_repo(tmp_path/'project','AGENTS.md','# Human instructions\n')
    key=known_copy(store,repo);proposal,text=delivery(store,cfg,repo)
    assert text not in (repo/'AGENTS.md').read_text()
    first=observe(store,cfg,1)
    assert first['outcomes']=={'absent':1}
    run_git(['merge','--ff-only',cfg.project_branch_name],repo)
    second=observe(store,cfg,2)
    assert second['outcomes']=={'available':1}
    records=rows(store);available=records[-1]
    assert available['matches'][0]['loading_paths'][0]['provider']=='codex'
    revision=revisions.retained_revisions(store)[0]
    assert revision['content']==text
    interval=availability.availability_intervals(store,rule_revision_id=revision['id'],
        working_copy_id=working_copy_identity(key,str(repo))['id'],start='2029-12-01T00:00:00Z',end='2030-02-01T00:00:00Z')
    assert interval['periods'][0]['start']=='2030-01-02T00:00:00.000000Z'
    assert interval['periods'][0]['confirmed_through']=='2030-01-02T00:00:00.000000Z'
    assert interval['unknown_after_last_observation']
    assert store.query_one('SELECT COUNT(*) n FROM llm_calls')['n']==0


def test_copies_share_project_identity_but_not_availability(cfg,store,tmp_path):
    a=init_git_repo(tmp_path/'repo-a','AGENTS.md','# A\n')
    b=init_git_repo(tmp_path/'repo-b','AGENTS.md','# B\n')
    for repo in (a,b):run_git(['remote','add','origin','https://example.test/owner/invented.git'],repo)
    assert known_copy(store,a)==known_copy(store,b)
    proposal,text=delivery(store,cfg,a)
    run_git(['merge','--ff-only',cfg.project_branch_name],a)
    assert observe(store,cfg,1)['outcomes']=={'absent':1,'available':1}
    got={r['working_copy']['normalized_path']:r['status'] for r in rows(store)}
    assert got[str(a)]=='available' and got[str(b)]=='absent'
    run_git(['remote','set-url','origin','https://example.test/other/different.git'],a)
    assert observe(store,cfg,2)['outcomes']=={'absent':1,'unknown':1}
    assert any(r['cause']=='working_copy_identity_changed' for r in rows(store))


def test_unrelated_human_edit_does_not_change_rule_but_marked_edit_does(cfg,store,tmp_path):
    repo=init_git_repo(tmp_path/'project','AGENTS.md','# Human instructions\n')
    known_copy(store,repo);proposal,text=delivery(store,cfg,repo)
    run_git(['merge','--ff-only',cfg.project_branch_name],repo)
    observe(store,cfg,1)
    target=repo/'AGENTS.md';target.write_text('# Changed human heading\n'+text+'Human footer.\n')
    assert observe(store,cfg,2)['outcomes']=={'available':1}
    target.write_text(target.read_text().replace('readable.','different.'))
    assert observe(store,cfg,3)['outcomes']=={'changed':1}
    revision=revisions.retained_revisions(store)[0]
    result=availability.availability_intervals(store,rule_revision_id=revision['id'],working_copy_id=rows(store)[0]['working_copy_id'],
        start='2030-01-01T00:00:00Z',end='2030-02-01T00:00:00Z')
    assert result['periods'][0]['end']=='2030-01-03T00:00:00.000000Z'
    assert result['periods'][0]['confirmed_through']=='2030-01-02T00:00:00.000000Z'


def test_imports_symlinks_scopes_and_codex_override_are_observed(cfg,store,tmp_path):
    repo=init_git_repo(tmp_path/'project','AGENTS.md','# Human instructions\n')
    known_copy(store,repo);proposal,text=delivery(store,cfg,repo)
    run_git(['merge','--ff-only',cfg.project_branch_name],repo)
    (repo/'AGENTS.override.md').write_text('# Only the override is loaded by Codex.\n')
    assert observe(store,cfg,1)['outcomes']=={'absent':1}
    (repo/'CLAUDE.md').write_text('@first.md\n@alias.md\n')
    (repo/'first.md').write_text('@AGENTS.md\n')
    (repo/'alias.md').symlink_to(repo/'AGENTS.md')
    assert observe(store,cfg,2)['outcomes']=={'available':1}
    record=rows(store)[-1]
    assert {p['provider'] for m in record['matches'] for p in m['loading_paths']}=={'claude'}
    assert len([f for f in record['inspection']['files'] if f['real_path']==str(repo/'AGENTS.md')])==1
    assert record['inspection']['deduplicated']
    (repo/'CLAUDE.md').unlink();(repo/'first.md').unlink();(repo/'alias.md').unlink()
    rules=repo/'.claude/rules/nested';rules.mkdir(parents=True)
    (rules/'specific.md').write_text('---\npaths: ["src/**"]\n---\n'+text)
    assert observe(store,cfg,3)['outcomes']=={'available':1}
    scopes=[p['scope'] for m in rows(store)[-1]['matches'] for p in m['loading_paths']]
    assert scopes==[{'kind':'path_scoped','paths':['src/**']}]


def test_replays_do_not_add_rows_and_writer_requires_transaction(cfg,store,tmp_path):
    repo=init_git_repo(tmp_path/'project','AGENTS.md','# Human instructions\n')
    known_copy(store,repo);delivery(store,cfg,repo);run_git(['merge','--ff-only',cfg.project_branch_name],repo)
    first=observe(store,cfg,1,run_id='scan-one')
    assert observe(store,cfg,1,run_id='scan-one')==first
    assert len(rows(store))==1
    with pytest.raises(revisions.AvailabilityError,match='active write transaction'):
        availability.record_availability(store,observations=rows(store))
    existing=rows(store)[0]
    with store.transaction(write=True):availability.record_availability(store,observations=[existing,existing])
    assert len(rows(store))==1


def test_publication_failure_rolls_back_revision_observation_and_collection(cfg,store,tmp_path,monkeypatch):
    repo=init_git_repo(tmp_path/'project','AGENTS.md','# Human instructions\n')
    known_copy(store,repo);delivery(store,cfg,repo)
    original=store.insert
    def fail(table,row):
        if table=='rule_availability_collections':raise RuntimeError('invented publication interruption')
        return original(table,row)
    monkeypatch.setattr(store,'insert',fail)
    with pytest.raises(RuntimeError,match='publication interruption'):observe(store,cfg,1)
    for table in revisions.TABLES:assert store.query_one(f'SELECT COUNT(*) n FROM {table}')['n']==0
    monkeypatch.setattr(store,'insert',original)
    assert observe(store,cfg,1)['observations']==1


def test_missing_snapshots_unknown_marker_and_missing_copy_do_not_invent_ownership(cfg,store,tmp_path):
    repo=init_git_repo(tmp_path/'project','AGENTS.md','# Human instructions\n')
    known_copy(store,repo);proposal,text=delivery(store,cfg,repo)
    event=store.query_one("SELECT * FROM proposal_events WHERE event='applied'")
    note=json.loads(event['note']);note['snapshot_after']='unavailable-fixture-snapshot'
    store.update('proposal_events','id',event['id'],{'note':json.dumps(note)});store.commit()
    (repo/'AGENTS.md').write_text(text)
    result=observe(store,cfg,1)
    assert result['status']=='partial' and result['tracked_revisions']==0
    assert result['errors'] and not rows(store)


def test_old_and_damaged_schemas_fail_without_migration(cfg,store):
    store.conn.execute("DELETE FROM schema_migrations WHERE name='0024_rule_availability'");store.commit()
    with pytest.raises(revisions.AvailabilityError,match='UpgradeRequired'):observe(store,cfg,1)
    assert not store.query_one("SELECT name FROM schema_migrations WHERE name='0024_rule_availability'")
    store.insert('schema_migrations',{'name':'0024_rule_availability','applied_at':'2030-01-01T00:00:00Z'})
    store.conn.execute('DROP TABLE rule_availability_observations');store.commit()
    with pytest.raises(revisions.AvailabilityError,match='rule_availability_observations'):observe(store,cfg,1)


def test_whole_multiline_rule_is_checked_not_only_its_final_marked_line(cfg,store,tmp_path):
    repo=init_git_repo(tmp_path/'project','AGENTS.md','# Human instructions\n')
    known_copy(store,repo)
    p=insert_proposal(store,target=repo/'AGENTS.md',diff='',target_kind='project_agents_md')
    text='- **Fixture.** Check the complete rule.\n- First detail.\n- Last detail. <!-- si:'+p['learning_id']+' -->\n'
    before=(repo/'AGENTS.md').read_text();after=before+text
    store.update('proposals','id',p['id'],{'diff_unified':make_diff(before,after)});store.commit()
    p=store.query_one('SELECT * FROM proposals WHERE id=?',(p['id'],))
    apply.apply_proposal(store,cfg,p);run_git(['merge','--ff-only',cfg.project_branch_name],repo)
    assert observe(store,cfg,1)['outcomes']=={'available':1}
    assert revisions.retained_revisions(store)[0]['content']==text
    (repo/'AGENTS.md').write_text(after.replace('complete rule.','different rule.'))
    assert observe(store,cfg,2)['outcomes']=={'changed':1}


def test_global_skill_remains_on_demand_with_path_conditions(cfg,store,tmp_path):
    repo=init_git_repo(tmp_path/'project','AGENTS.md','# Human instructions\n');known_copy(store,repo)
    skill=Path(cfg.skills_dir)/'fixture/SKILL.md';skill.parent.mkdir(parents=True)
    p=insert_proposal(store,target=skill,diff='',target_kind='skill')
    text='---\nname: fixture\npaths: ["src/**"]\n---\n- Fixture. <!-- si:'+p['learning_id']+' -->\n'
    store.update('proposals','id',p['id'],{'diff_unified':make_diff('',text)});store.commit()
    p=store.query_one('SELECT * FROM proposals WHERE id=?',(p['id'],))
    apply.apply_proposal(store,cfg,p)
    assert observe(store,cfg,1)['outcomes']=={'available':1}
    scopes=[path['scope'] for match in rows(store)[-1]['matches'] for path in match['loading_paths']]
    assert scopes==[{'kind':'on_demand','paths':['src/**']}]
    skill.write_text(text+'Changed machine-owned skill body.\n')
    assert observe(store,cfg,2)['outcomes']=={'changed':1}


def test_unknown_coverage_and_conflicting_same_time_checks_interrupt_periods(cfg,store,tmp_path,monkeypatch):
    from self_improve import instruction_surfaces
    repo=init_git_repo(tmp_path/'project','AGENTS.md','# Human instructions\n')
    key=known_copy(store,repo);p,text=delivery(store,cfg,repo)
    run_git(['merge','--ff-only',cfg.project_branch_name],repo)
    observe(store,cfg,1);observe(store,cfg,2)
    target=repo/'AGENTS.md';original=target.read_text();target.write_text(original.replace('readable.','edited.'))
    observe(store,cfg,2)
    page=availability.project_availability(store,project_key=key)
    assert page['records'][0]['conflicting_observations'] and page['records'][0]['status']=='unknown'
    interval=availability.availability_intervals(store,rule_revision_id=revisions.retained_revisions(store)[0]['id'],
        working_copy_id=rows(store)[0]['working_copy_id'],start='2030-01-01T00:00:00Z',end='2030-02-01T00:00:00Z')
    assert interval['periods'][0]['end']=='2030-01-02T00:00:00.000000Z'
    target.write_text(original)
    monkeypatch.setattr(instruction_surfaces,'MAX_FILE_BYTES',4)
    result=observe(store,cfg,3)
    assert result['outcomes']=={'unknown':1}
    assert rows(store)[-1]['inspection']['issues'][0]['cause']=='file_byte_limit'


def test_wrong_matched_content_is_rejected_even_with_recomputed_record_id(cfg,store,tmp_path):
    from copy import deepcopy
    from self_improve.mining_history import digest
    repo=init_git_repo(tmp_path/'project','AGENTS.md','# Human instructions\n')
    known_copy(store,repo);delivery(store,cfg,repo);run_git(['merge','--ff-only',cfg.project_branch_name],repo)
    observe(store,cfg,1)
    broken=deepcopy(rows(store)[0]);broken['matches'][0]['file_hash']='0'*64
    broken['id']=digest({k:v for k,v in broken.items() if k!='id'})
    with pytest.raises(revisions.AvailabilityError,match='content or loading path'):
        with store.transaction(write=True):availability.record_availability(store,observations=[broken])
    assert len(rows(store))==1


def test_cli_and_pipeline_reach_real_collection_and_reads_use_only_the_copy(cfg,store,tmp_path,monkeypatch,capsys):
    from self_improve import cli,pipeline
    from self_improve.dashboard.app import create_app
    from fastapi.testclient import TestClient
    repo=init_git_repo(tmp_path/'project','AGENTS.md','# Human instructions\n')
    key=known_copy(store,repo);delivery(store,cfg,repo);run_git(['merge','--ff-only',cfg.project_branch_name],repo)
    monkeypatch.setattr(cli,'load_config',lambda *_:cfg)
    assert cli.main(['observe-availability'])==0
    assert json.loads(capsys.readouterr().out)['outcomes']=={'available':1}
    Path(cfg.claude_projects_dir).mkdir(parents=True)
    Path(cfg.codex_sessions_dir).mkdir(parents=True)
    stats=pipeline.run_pipeline(cfg,store,dry_run=True)
    assert stats['availability']['outcomes']=={'available':1}
    copy_path=tmp_path/'copy.db';copy=Store(copy_path);store.conn.backup(copy.conn);copy.close()
    store.conn.execute('DELETE FROM rule_availability_observations');store.commit()
    before=copy_path.read_bytes()
    with TestClient(create_app(cfg,db_path=copy_path)) as client:
        response=client.get('/api/project-availability',params={'project_key':key})
        assert response.status_code==200 and response.json()['count']==1
        assert response.json()['records'][0]['status']=='available'
        assert client.get('/api/project-availability',params={'project_key':key,'limit':0}).status_code==400
    assert copy_path.read_bytes()==before
    assert store.query_one('SELECT COUNT(*) n FROM llm_calls')['n']==0


def test_codex_fallback_and_byte_coverage_are_observed_without_runtime_claims(cfg,store,tmp_path):
    repo=init_git_repo(tmp_path/'project','AGENTS.md','# Human instructions\n')
    known_copy(store,repo);p,text=delivery(store,cfg,repo)
    run_git(['merge','--ff-only',cfg.project_branch_name],repo)
    (repo/'AGENTS.md').rename(repo/'TEAM.md')
    config=Path(cfg.codex_global_agents_md).parent/'config.toml';config.parent.mkdir(parents=True)
    config.write_text('project_doc_fallback_filenames = ["TEAM.md"]\nproject_doc_max_bytes = 10000\n')
    assert observe(store,cfg,1)['outcomes']=={'available':1}
    check=rows(store)[-1]
    assert check['matches'][0]['path'].endswith('TEAM.md')
    assert not check['inspection']['runtime_loading_verified']
    config.write_text('project_doc_fallback_filenames = ["TEAM.md"]\nproject_doc_max_bytes = 5\n')
    assert observe(store,cfg,2)['outcomes']=={'unknown':1}
    assert any(i['cause']=='codex_instruction_coverage_limit' for i in rows(store)[-1]['inspection']['issues'])
    config.write_text('project_doc_max_bytes = "broken"\n')
    assert observe(store,cfg,3)['outcomes']=={'unknown':1}
    assert any(i['cause']=='codex_discovery_configuration_invalid' for i in rows(store)[-1]['inspection']['issues'])


def test_foreign_copy_is_not_read_and_unrelated_human_bullet_is_not_owned(cfg,store,tmp_path,monkeypatch):
    repo=init_git_repo(tmp_path/'project','AGENTS.md','- **Human.** Keep this separate.\n')
    run_git(['remote','add','origin','https://example.test/one/repo.git'],repo)
    known_copy(store,repo);p,text=delivery(store,cfg,repo)
    run_git(['merge','--ff-only',cfg.project_branch_name],repo)
    assert observe(store,cfg,1)['outcomes']=={'available':1}
    assert revisions.retained_revisions(store)[0]['content']==text
    run_git(['remote','set-url','origin','https://example.test/two/repo.git'],repo)
    def forbidden(*args,**kwargs):raise AssertionError('foreign working copy inspected')
    monkeypatch.setattr(availability,'inspect_surfaces',forbidden)
    assert observe(store,cfg,2)['outcomes']=={'unknown':1}
    assert rows(store)[-1]['inspection']['file_count']==0


def test_rebuild_exports_and_preserves_availability_after_proposal_deletion(cfg,store,tmp_path):
    from self_improve.rebuild import rebuild_state
    repo=init_git_repo(tmp_path/'project','AGENTS.md','# Human instructions\n')
    key=known_copy(store,repo);delivery(store,cfg,repo);run_git(['merge','--ff-only',cfg.project_branch_name],repo)
    observe(store,cfg,1)
    before={table:store.query(f'SELECT * FROM {table} ORDER BY id') for table in revisions.TABLES}
    destination=tmp_path/'private-backup'
    with pytest.raises(ValueError,match='retained execution history'):
        rebuild_state(store,export_path=destination)
    assert not destination.exists()
    for table,records in before.items():assert store.query(f'SELECT * FROM {table} ORDER BY id')==records
    # A legacy database can retain delivered snapshots without operation rows.
    # Exercise that supported rebuild shape separately from the refusal above.
    store.conn.execute('DELETE FROM instruction_operations');store.commit()
    rebuild_state(store,export_path=destination)
    backup=json.loads((destination/'preserved.json').read_text())
    assert backup['rule_availability']==before
    for table,records in before.items():assert store.query(f'SELECT * FROM {table} ORDER BY id')==records
    assert store.query('SELECT * FROM proposals')==[]
    assert availability.project_availability(store,project_key=key)['records'][0]['status']=='available'
    assert observe(store,cfg,2)['outcomes']=={'available':1}


def test_old_exact_application_remains_distinct_after_reapplication(cfg,store,tmp_path):
    repo=init_git_repo(tmp_path/'project','AGENTS.md','# Human instructions\n')
    known_copy(store,repo);proposal,text=delivery(store,cfg,repo)
    observe(store,cfg,1)
    old=revisions.retained_revisions(store)[0]
    apply.rollback(store,cfg,proposal['id'])
    store.update('proposals','id',proposal['id'],{'status':'ungated'})
    store.commit()
    p=store.query_one('SELECT * FROM proposals WHERE id=?',(proposal['id'],))
    from tests.test_delivery_worker import approve
    from self_improve.worker import run_once
    approve(store,cfg,p)
    result=run_once(store,cfg)
    assert result['state']=='completed',result
    observe(store,cfg,2)
    got=revisions.retained_revisions(store)
    assert len(got)==2 and got[0]==old
    assert len({r['application_id'] for r in got})==2
    old_checks=[r for r in rows(store) if r['rule_revision_id']==old['id']]
    assert old_checks[-1]['status']=='unknown' and old_checks[-1]['cause']=='application_superseded'
    event=store.query_one('SELECT * FROM proposal_events WHERE id=?',(old['application_event_id'],))
    assert revisions.capture_revision(store,cfg,event,captured_at='2030-01-03T00:00:00Z',cache={})['id']==old['id']


def test_pages_are_project_bound_and_interval_reader_rejects_corruption(cfg,store,tmp_path):
    from self_improve.mining_history import digest,encoded
    repo=init_git_repo(tmp_path/'project','AGENTS.md','# Human instructions\n')
    key=known_copy(store,repo)
    for _ in range(3):delivery(store,cfg,repo)
    run_git(['merge','--ff-only',cfg.project_branch_name],repo)
    observe(store,cfg,1)
    first=availability.project_availability(store,project_key=key,limit=2)
    second=availability.project_availability(store,project_key=key,limit=2,cursor=first['next_cursor'])
    assert first['count']==3 and len(second['records'])==1 and second['next_cursor'] is None
    assert len({r['revision']['id'] for r in first['records']+second['records']})==3
    with pytest.raises(revisions.AvailabilityError,match='different project'):
        availability.project_availability(store,project_key='another',cursor=first['next_cursor'])
    record=rows(store)[0];record['matches'][0]['file_hash']='broken'
    record['id']=digest({k:v for k,v in record.items() if k!='id'})
    store.conn.execute('DELETE FROM rule_availability_observations');store.commit()
    keys=('id','rule_revision_id','project_key','working_copy_id','observed_at','status','run_id')
    store.insert('rule_availability_observations',{**{k:record[k] for k in keys},'record_json':encoded(record),'record_hash':digest(record)})
    store.commit()
    with pytest.raises(revisions.AvailabilityError,match='content or loading path'):
        availability.availability_intervals(store,rule_revision_id=record['rule_revision_id'],working_copy_id=record['working_copy_id'],
            start='2030-01-01T00:00:00Z',end='2030-02-01T00:00:00Z')


def test_native_run_and_report_preserve_partial_collection_causes(cfg,store,tmp_path):
    from self_improve import pipeline
    from self_improve.dashboard.run_data import detail
    from self_improve.availability_reporting import summary
    repo=init_git_repo(tmp_path/'project','AGENTS.md','# Human instructions\n')
    known_copy(store,repo);delivery(store,cfg,repo)
    event=store.query_one("SELECT * FROM proposal_events WHERE event='applied'")
    note=json.loads(event['note']);note['snapshot_after']='missing-fixture-object'
    store.update('proposal_events','id',event['id'],{'note':json.dumps(note)});store.commit()
    Path(cfg.claude_projects_dir).mkdir(parents=True);Path(cfg.codex_sessions_dir).mkdir(parents=True)
    stats=pipeline.run_pipeline(cfg,store,dry_run=True)
    result=detail(store,stats['run_id'])['availability_summary']
    assert result['status']=='partial' and result['causes']
    report=Path(stats['report_path']).read_text()
    assert 'Working-copy rule availability' in report and 'Collection: **partial**' in report
    for cause in result['causes']:assert cause in report
    assert not summary(None,owner='old')['recorded']
    bad={**stats['availability'],'observations':1}
    with pytest.raises(revisions.AvailabilityError,match='reconcile'):summary(bad,owner='broken')


def test_live_observation_clock_is_sampled_after_file_reads(cfg,store,tmp_path,monkeypatch):
    repo=init_git_repo(tmp_path/'project','AGENTS.md','# Human instructions\n')
    key=known_copy(store,repo);delivery(store,cfg,repo);run_git(['merge','--ff-only',cfg.project_branch_name],repo)
    observe(store,cfg,1)
    calls=[];real=availability.inspect_surfaces
    def inspect(*args,**kwargs):
        value=real(*args,**kwargs);calls.append('read');return value
    def clock():
        assert calls==['read'];calls.append('clock');return '2030-01-03T00:00:00Z'
    monkeypatch.setattr(availability,'inspect_surfaces',inspect);monkeypatch.setattr(availability,'utc_now_iso',clock)
    check=availability.inspect_working_copy(cfg,project_key=key,working_copy_path=str(repo),
        revisions=revisions.retained_revisions(store),observed_at=None)[0]
    assert check['observed_at']=='2030-01-03T00:00:00.000000Z'
