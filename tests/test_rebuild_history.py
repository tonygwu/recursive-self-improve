"""Rebuild preserves execution and its source graph in temporary state only."""
import json
from contextlib import closing
from pathlib import Path

import pytest

from self_improve import worker, eval_history, job_worker
from self_improve.commands import command_status, submit_command
from self_improve.data_boundary import verify_backup
from self_improve.rebuild import rebuild_state
from self_improve.store import Store
from tests.test_delivery_worker import env, approve, propose
from tests.test_apply import cfg, store, insert_proposal
from tests.test_model_jobs import env as evaluation_env, request as evaluation_request
from tests.test_rebuild import _session, _incident


def dump(store):
    return list(store.conn.iterdump())


def unrelated(store, target):
    return insert_proposal(store, target=target, diff='', status='pending')


@pytest.mark.parametrize('completed', [False, True])
def test_saved_approval_survives_and_delivery_does_not_repeat(env, tmp_path, completed):
    cfg, store, target = env
    p = propose(store, target); command = approve(store, cfg, p)
    if completed:
        assert worker.run_once(store, cfg)['state'] == 'completed'
    other = unrelated(store, tmp_path/'unrelated.md')
    before = command_status(store, command['id']); content = target.read_bytes()
    history = store.query('SELECT * FROM proposal_events WHERE proposal_id=? ORDER BY id', (p['id'],))
    result = rebuild_state(store, export_path=tmp_path/'backup')
    assert command_status(store, command['id']) == before
    assert store.query('SELECT * FROM proposal_events WHERE proposal_id=? ORDER BY id', (p['id'],)) == history
    assert store.query_one('SELECT id FROM proposals WHERE id=?', (other['id'],)) is None
    assert store.query_one('SELECT id FROM learnings WHERE id=?', (other['learning_id'],)) is None
    assert result['deleted']['proposals'] == result['deleted']['learnings'] == 1
    assert target.read_bytes() == content
    verify_backup(tmp_path/'backup')
    payload = json.loads((tmp_path/'backup/preserved.json').read_text())
    assert payload['execution_history']['commands'][0]['id'] == command['id']
    if not completed:
        assert worker.run_once(store, cfg)['state'] == 'completed'
    assert worker.run_once(store, cfg) is None
    assert len(store.query("SELECT id FROM proposal_events WHERE event='applied'")) == 1
    assert store.query('SELECT * FROM llm_calls') == []


def test_completed_evaluation_retains_all_scenarios_and_budget(evaluation_env, tmp_path):
    cfg, store, proposal, calls = evaluation_env
    cmd = submit_command(store, cfg, evaluation_request(evaluation_env))
    assert job_worker.run_once(store, cfg)['state'] == 'completed'
    attempt = eval_history.page(store, command_id=cmd['id'])['records'][0]['id']
    before = eval_history.detail(store, attempt); saved = command_status(store, cmd['id'])
    call_count = len(calls); unrelated(store, tmp_path/'unrelated.md')
    rebuild_state(store, export_path=tmp_path/'backup')
    assert eval_history.detail(store, attempt) == before
    assert command_status(store, cmd['id']) == saved
    assert job_worker.run_once(store, cfg) is None and len(calls) == call_count
    assert store.query('PRAGMA foreign_key_check') == []


def test_history_keeps_linked_live_transcript_while_unrelated_live_rows_go(env, tmp_path):
    cfg, store, target = env
    p = propose(store, target); approve(store, cfg, p)
    kept = tmp_path/'kept.jsonl'; kept.write_text('{}\n')
    other = tmp_path/'other.jsonl'; other.write_text('{}\n')
    _session(store, str(kept), 'kept'); _session(store, str(other), 'other')
    incident = _incident(store, str(kept), 'kept', [{'text': 'invented retained evidence'}])
    _incident(store, str(other), 'other', [])
    store.link_incident_learning(incident, p['learning_id']); store.commit()
    result = rebuild_state(store, export_path=tmp_path/'backup')
    assert [r['file_path'] for r in store.query('SELECT * FROM sessions')] == [str(kept)]
    assert [r['id'] for r in store.query('SELECT * FROM incidents')] == [incident]
    assert store.query_one('SELECT * FROM incident_learnings')['learning_id'] == p['learning_id']
    assert result['preserved']['sessions'] == result['deleted']['sessions'] == 1
    assert result['retention']['execution_history']['sessions'] == 1


def test_json_only_result_reference_and_legacy_decision_are_preserved(env, tmp_path):
    cfg, store, target = env
    p = propose(store, target); cmd = approve(store, cfg, p)
    generated = unrelated(store, tmp_path/'generated.md')
    legacy = insert_proposal(store, target=tmp_path/'legacy.md', diff='', status='rejected_user')
    other = unrelated(store, tmp_path/'other.md')
    store.update('commands','id',cmd['id'],{'result_json':json.dumps({'proposal_ids':[generated['id']]})});store.commit()
    rebuild_state(store, export_path=tmp_path/'backup')
    assert {r['id'] for r in store.query('SELECT * FROM proposals')} == {p['id'], generated['id'], legacy['id']}
    assert not store.query_one('SELECT id FROM proposals WHERE id=?',(other['id'],))


def test_read_only_preview_matches_apply_and_second_pass_deletes_zero(env, tmp_path):
    cfg, store, target = env
    p = propose(store, target); approve(store, cfg, p)
    unrelated(store, tmp_path/'other.md'); before = dump(store)
    with closing(Store(store.db_path, read_only=True)) as reader:
        preview = rebuild_state(reader, export_path=tmp_path/'preview', dry_run=True)
    assert dump(store) == before and not (tmp_path/'preview').exists()
    result = rebuild_state(store, export_path=tmp_path/'backup')
    assert result['deleted'] == preview['would_delete'] and result['preserved'] == preview['preserved']
    second = rebuild_state(store, export_path=tmp_path/'second')
    assert not any(second['deleted'].values())


def test_invalid_retained_json_refuses_before_backup_or_mutation(env, tmp_path):
    cfg, store, target = env
    p = propose(store, target); cmd = approve(store, cfg, p)
    store.update('commands','id',cmd['id'],{'result_json':'{invalid'});store.commit();before=dump(store)
    with pytest.raises(ValueError, match='commands.*result_json'):
        rebuild_state(store, export_path=tmp_path/'backup')
    assert dump(store) == before and not (tmp_path/'backup').exists()


def test_unknown_table_requires_retention_policy_before_deletion(env, tmp_path):
    cfg, store, target = env
    propose(store, target)
    store.conn.execute('CREATE TABLE future_history (id TEXT PRIMARY KEY)');store.commit();before=dump(store)
    with pytest.raises(ValueError, match='future_history'):
        rebuild_state(store, export_path=tmp_path/'backup')
    assert dump(store) == before and not (tmp_path/'backup').exists()

from tests.test_reapplications import undone, request as reapplication_request, REAPPLIED
from tests.test_incident_jobs import env as mining_env, request as mining_request
from tests.test_recovery_jobs import env as recovery_env, request as recovery_request
from tests.test_rollback_resolutions import conflict, request as resolution_request, RESOLVED


def test_reapplication_retains_original_inverse_and_finishes_after_rebuild(undone, tmp_path):
    cfg, store, target, original = undone
    made = submit_command(store, cfg, reapplication_request(undone))
    draft = store.query_one('SELECT * FROM proposals WHERE id=?', (made['result']['proposal_id'],))
    before = command_status(store, made['id']); content = target.read_bytes()
    rebuild_state(store, export_path=tmp_path/'backup')
    assert command_status(store, made['id']) == before and target.read_bytes() == content
    approve(store, cfg, draft)
    assert worker.run_once(store, cfg)['state'] == 'completed'
    assert target.read_text() == REAPPLIED
    assert store.query_one('SELECT status FROM proposals WHERE id=?',(original['id'],))['status']=='rolled_back'


@pytest.mark.parametrize('action', ['reject_target', 'reject_lesson'])
def test_rejection_scope_and_frozen_decision_remain_effective(env, tmp_path, action):
    from tests.test_rejections import decision
    cfg, store, target = env
    p = propose(store, target); body=decision(store,cfg,action,[p]);cmd = submit_command(store,cfg,body)
    before = command_status(store,cmd['id'])
    rebuild_state(store,export_path=tmp_path/'backup')
    assert command_status(store,cmd['id']) == before
    assert store.query_one('SELECT status FROM proposals WHERE id=?',(p['id'],))['status']=='rejected_user'
    assert submit_command(store,cfg,body)['state']=='completed'


@pytest.mark.parametrize('completed', [False, True])
def test_selected_incident_job_keeps_source_and_never_repeats_saved_call(mining_env, tmp_path, completed):
    from self_improve import incident_jobs
    cfg, store, incident, calls = mining_env
    cmd = submit_command(store,cfg,mining_request(mining_env))
    if completed: assert job_worker.run_once(store,cfg)['state']=='completed'
    before = command_status(store,cmd['id']); n=len(calls)
    rebuild_state(store,export_path=tmp_path/'backup')
    assert command_status(store,cmd['id'])==before
    assert incident_jobs.view(store,cfg,incident['id'])
    if not completed: assert job_worker.run_once(store,cfg)['state']=='completed'
    assert job_worker.run_once(store,cfg) is None
    assert len(calls)==n+(0 if completed else 1)
    assert store.query_one('SELECT * FROM incidents WHERE id=?',(incident['id'],))


def test_generated_recovery_keeps_frozen_source_and_manual_preview(recovery_env, tmp_path):
    from self_improve.commands import review_snapshot
    cfg,store,learning,calls=recovery_env
    cmd=submit_command(store,cfg,recovery_request(recovery_env));made=job_worker.run_once(store,cfg)
    assert made['state']=='completed';pid=made['result']['proposal_id']
    before=review_snapshot(store,pid,cfg);n=len(calls)
    rebuild_state(store,export_path=tmp_path/'backup')
    assert review_snapshot(store,pid,cfg)==before and len(calls)==n
    assert command_status(store,cmd['id'])['state']=='completed'


def test_resolution_retains_original_application_and_completes_inverse(conflict,tmp_path):
    cfg,store,target,p,calls=conflict
    submit_command(store,cfg,resolution_request(conflict));made=job_worker.run_once(store,cfg)
    assert made['state']=='completed';pid=made['result']['proposal_id'];n=len(calls)
    rebuild_state(store,export_path=tmp_path/'backup')
    draft=store.query_one('SELECT * FROM proposals WHERE id=?',(pid,));approve(store,cfg,draft)
    assert worker.run_once(store,cfg)['state']=='completed'
    assert target.read_text()==RESOLVED and len(calls)==n
    assert store.query_one('SELECT status FROM proposals WHERE id=?',(p['id'],))['status']=='rolled_back'


def test_retained_scan_transcript_keeps_denominator_and_all_observation_links(tmp_path):
    from tests.test_scan_observations import make_env,write_claude,correction_session,scan
    from self_improve import scan_observations as scans
    env=make_env(tmp_path,global_claude_md=str(tmp_path/'rule.md'))
    path=write_claude(env,correction_session(env.repo));scan(env,'scan-fixture')
    target=tmp_path/'rule.md';target.write_text('before\n')
    p=propose(env.store,target);approve(env.store,env.cfg,p)
    incident=env.store.query_one('SELECT id FROM incidents')['id']
    env.store.link_incident_learning(incident,p['learning_id']);env.store.commit()
    tables=('scan_lines','scan_occurrences','scan_observation_occurrences','scan_incident_links')
    before={t:env.store.query('SELECT * FROM '+t) for t in tables}
    rebuild_state(env.store,export_path=tmp_path/'backup')
    for t in tables:assert env.store.query('SELECT * FROM '+t)==before[t]
    assert scans.scan_history(env.store,incident_id=incident)['incident']['provenance']=='observed'
    payload=json.loads((tmp_path/'backup/preserved.json').read_text())
    assert payload['execution_scan_projections']['orphan_lines']==before['scan_lines']
    assert path.exists()


def test_store_metadata_groups_composite_references(tmp_path):
    with closing(Store(tmp_path/'meta.db')) as store:
        store.conn.execute('CREATE TABLE fixture_parent (a TEXT, b TEXT, PRIMARY KEY(a,b))')
        store.conn.execute('CREATE TABLE fixture_child (id TEXT PRIMARY KEY, a TEXT,b TEXT,FOREIGN KEY(a,b) REFERENCES fixture_parent(a,b))')
        store.commit();before=dump(store)
        schema=store.schema_tables()
        assert schema['fixture_parent']['primary_key']==['a','b']
        assert schema['fixture_child']['foreign_keys']==[{'table':'fixture_parent','columns':['a','b'],'references':['a','b']}]
        assert dump(store)==before


def test_old_evaluations_of_retained_subjects_stay_readable(env, tmp_path):
    cfg,store,target=env;p=propose(store,target);approve(store,cfg,p)
    for eid in ('older-eval','current-eval'):
        store.insert('eval_results',{'id':eid,'kind':'ab','subject_id':p['learning_id']})
    store.update('proposals','id',p['id'],{'eval_result_id':'current-eval'});store.commit()
    before=store.query('SELECT * FROM eval_results ORDER BY id')
    rebuild_state(store,export_path=tmp_path/'backup')
    assert store.query('SELECT * FROM eval_results ORDER BY id')==before


@pytest.mark.parametrize('alter', [
    'ALTER TABLE learnings ADD COLUMN future_audit TEXT',
    'ALTER TABLE runs ADD COLUMN future_proposal_id TEXT REFERENCES proposals(id) ON DELETE CASCADE',
])
def test_known_table_changes_refuse_before_export(env,tmp_path,alter):
    cfg,store,target=env;p=propose(store,target)
    store.conn.execute(alter)
    if 'runs' in alter:store.insert('runs',{'id':'retained-run','started':'2030-01-01T00:00:00Z','future_proposal_id':p['id']})
    store.commit();before=dump(store)
    with pytest.raises(ValueError,match='schema.*(learnings|runs)'):
        rebuild_state(store,export_path=tmp_path/'backup')
    assert dump(store)==before and not (tmp_path/'backup').exists()


def test_ignored_deletion_is_not_reported_as_success(env,tmp_path):
    cfg,store,target=env;path=tmp_path/'live.jsonl';path.write_text('{}\n')
    _session(store,str(path),'live');_incident(store,str(path),'live',[])
    store.conn.execute("CREATE TRIGGER ignored BEFORE DELETE ON sessions BEGIN SELECT RAISE(IGNORE); END")
    store.commit();before=dump(store)
    with pytest.raises(ValueError,match='count.*sessions'):
        rebuild_state(store,export_path=tmp_path/'backup')
    assert dump(store)==before


def test_directory_in_place_of_transcript_preserves_only_surviving_window(env,tmp_path):
    cfg,store,target=env;path=tmp_path/'former.jsonl';path.mkdir()
    _session(store,str(path),'lost');iid=_incident(store,str(path),'lost',[{'text':'irreplaceable fixture'}]);store.commit()
    rebuild_state(store,export_path=tmp_path/'backup')
    assert store.query_one('SELECT id FROM incidents WHERE id=?',(iid,))
    assert json.loads((tmp_path/'backup/preserved.json').read_text())['incidents'][0]['id']==iid


def test_source_removed_while_backup_is_written_refuses_deletion(env,tmp_path,monkeypatch):
    from self_improve import rebuild
    cfg,store,target=env;path=tmp_path/'live.jsonl';path.write_text('{}\n')
    _session(store,str(path),'live');_incident(store,str(path),'live',[{'text':'fixture'}]);store.commit()
    before=dump(store);backup=rebuild.backup_rebuild_rows
    def remove(payload,destination):
        result=backup(payload,destination);path.unlink();return result
    monkeypatch.setattr(rebuild,'backup_rebuild_rows',remove)
    with pytest.raises(ValueError,match='Transcript changed'):
        rebuild_state(store,export_path=tmp_path/'backup')
    assert dump(store)==before


def test_unreadable_regular_transcript_is_preserved(env,tmp_path,monkeypatch):
    import os
    cfg,store,target=env;path=tmp_path/'unreadable.jsonl';path.write_text('{}\n')
    _session(store,str(path),'lost');iid=_incident(store,str(path),'lost',[]);store.commit()
    real=os.open
    def deny(file,*a,**kw):
        if str(file)==str(path):raise PermissionError('fixture denial')
        return real(file,*a,**kw)
    monkeypatch.setattr(os,'open',deny)
    rebuild_state(store,export_path=tmp_path/'backup')
    assert store.query_one('SELECT id FROM incidents WHERE id=?',(iid,))


def test_copied_store_rebuild_leaves_original_rows_and_targets_unchanged(env,tmp_path):
    cfg,store,target=env;p=propose(store,target);approve(store,cfg,p)
    unrelated(store,tmp_path/'other.md');before=dump(store);content=target.read_bytes()
    with closing(Store(tmp_path/'copy.db')) as copied:
        store.conn.backup(copied.conn)
        result=rebuild_state(copied,export_path=tmp_path/'copy-backup')
        assert result['deleted']['proposals']==1
        assert copied.query_one('SELECT id FROM proposals WHERE id=?',(p['id'],))
    assert dump(store)==before and target.read_bytes()==content


def test_learning_vectors_follow_retained_owners_and_audit_is_backed_up(env,tmp_path):
    cfg,store,target=env;p=propose(store,target);approve(store,cfg,p)
    other=unrelated(store,tmp_path/'other.md')
    for kind,key in [('learning',p['learning_id']),('learning',other['learning_id']),('rule','fixture-rule')]:
        store.insert('embeddings',{'owner_kind':kind,'owner_key':key,'model':'fixture','text_sha':'fixture',
                                  'vector_json':'[1,0]','created_at':'2030-01-01T00:00:00Z'})
    store.insert('runs',{'id':'audit-run','started':'2030-01-01T00:00:00Z'})
    store.commit();before=store.query('SELECT * FROM runs')
    result=rebuild_state(store,export_path=tmp_path/'backup')
    assert {r['owner_key'] for r in store.query('SELECT * FROM embeddings')}=={p['learning_id'],'fixture-rule'}
    assert result['deleted']['embeddings']==1 and result['preserved']['embeddings']==2
    payload=json.loads((tmp_path/'backup/preserved.json').read_text())
    assert payload['execution_audit']['runs']==before==store.query('SELECT * FROM runs')
    assert [r['owner_key'] for r in payload['learning_embeddings']]==[p['learning_id']]


def test_cli_preview_and_failure_use_selected_store_without_migration(env,tmp_path,monkeypatch,capsys):
    from self_improve import cli
    cfg,store,target=env;p=propose(store,target);approve(store,cfg,p)
    monkeypatch.setattr(cli,'load_config',lambda *_:cfg)
    before=dump(store)
    assert cli.main(['rebuild-state','--export',str(tmp_path/'preview'),'--dry-run'])==0
    shown=json.loads(capsys.readouterr().out)
    assert shown['preserved']['proposals']==1 and dump(store)==before
    assert not (tmp_path/'preview').exists()
    store.conn.execute('ALTER TABLE learnings ADD COLUMN future TEXT');store.commit();before=dump(store)
    assert cli.main(['rebuild-state','--export',str(tmp_path/'refused')])==2
    assert 'changed schema of learnings' in capsys.readouterr().err
    assert dump(store)==before and not (tmp_path/'refused').exists()
