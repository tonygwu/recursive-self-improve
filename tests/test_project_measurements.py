"""Observed recurrence through real temporary scanner and availability producers."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import uuid

import pytest

from tests.test_session_context import env, meta, applied
from tests.test_scan_observations import PROJECT, scan
from tests.test_scan_occurrences import write_codex, x_call, x_user, x_tokens

START='2029-12-01T00:00:00Z'
END='2030-02-02T00:00:00Z'


def cohort(env, *, before=20, after=20, after_matches=10):
    for side,n,day in [('before',before,'2029-12-20'),('after',after,'2030-01-03')]:
        for i in range(n):
            sid=str(uuid.uuid5(uuid.NAMESPACE_URL,side+str(i)))
            when=lambda sec:f'{day}T{1+i//60:02d}:{i%60:02d}:{sec:02d}Z'
            write_codex(env,[meta(env.repo,when(0),sid),x_call(when(1),'call'),
                x_user("no, that's wrong" if side=='before' or i<after_matches else 'Looks correct',when(2)),
                x_tokens(when(3))],name=f'rollout-{side}-{i}.jsonl')
    scan(env,'cohort')
    revision,_,_=applied(env,last_check=31)
    for inc in env.store.query('SELECT id FROM incidents'):
        env.store.insert('incident_learnings',{'incident_id':inc['id'],'learning_id':revision['learning_id']})
        env.store.update('incidents','id',inc['id'],{'status':'mined'})
    env.store.commit()
    key=env.store.query_one('SELECT compatibility_key FROM scan_lines LIMIT 1')['compatibility_key']
    return revision,key


def calculate(env,revision,key,**options):
    from self_improve.project_measurements import rule_recurrence
    return rule_recurrence(env.store,rule_revision_id=revision['id'],project_key=PROJECT,
                           start=START,end=END,compatibility_key=key,**options)


def test_real_scanner_and_actual_file_checks_compare_equal_physical_denominators(env):
    revision,key=cohort(env)
    got=calculate(env,revision,key)
    assert got['before']['eligible_lines']==got['after']['eligible_lines']==80
    assert got['before']['sessions']==got['after']['sessions']==20
    assert (got['before']['occurrences'],got['after']['occurrences'])==(20,10)
    assert got['relative_change_pct']==-50 and got['comparison']=='lower'
    assert got['before']['rate_per_100k']==25000
    assert got['after']['rate_per_100k']==12500
    assert got['computable'] and not got['coverage_complete']
    assert not got['runtime_loading_verified'] and got['matching']['method']=='retained_incident_learning_links/1'
    assert env.store.query_one('SELECT COUNT(*) n FROM project_stats')['n']==0
    assert env.store.query_one('SELECT COUNT(*) n FROM llm_calls')['n']==0


def test_missing_links_is_unknown_while_positive_exposure_can_have_zero_matches(env):
    revision,key=cohort(env,after_matches=0)
    got=calculate(env,revision,key)
    assert got['after']['occurrences']==0 and got['relative_change_pct']==-100
    env.store.conn.execute('DELETE FROM incident_learnings');env.store.commit()
    unknown=calculate(env,revision,key)
    assert unknown['reason']=='missing_matching_evidence' and unknown['after']['occurrences'] is None
    assert unknown['after']['eligible_lines']==80 and unknown['after']['rate_per_100k'] is None


def test_twenty_known_sessions_are_required_and_cannot_be_lowered(env):
    from self_improve.project_measurements import MeasurementRequestError
    revision,key=cohort(env,after=19)
    got=calculate(env,revision,key)
    assert got['reason']=='insufficient_sessions' and got['after']['sessions']==19
    with pytest.raises(MeasurementRequestError,match='at least 20'):
        calculate(env,revision,key,min_sessions=1)


def test_publication_is_explicit_atomic_and_replay_preserves_first_receiver_time(env,monkeypatch):
    from self_improve import project_measurements as pm
    revision,key=cohort(env);m=calculate(env,revision,key)
    env.store.insert('runs',{'id':'measurement-run','started':START});env.store.commit()
    with pytest.raises(ValueError,match='write transaction'):pm.record_project_measurement(env.store,run_id='measurement-run',measurement=m)
    monkeypatch.setattr(pm,'utc_now_iso',lambda:'2030-03-01T00:00:00.000000Z')
    with env.store.transaction(write=True):rid=pm.record_project_measurement(env.store,run_id='measurement-run',measurement=m)
    record=pm.measurement_detail(env.store,rid)
    assert record['observed_at']=='2030-03-01T00:00:00.000000Z'
    monkeypatch.setattr(pm,'utc_now_iso',lambda:'2030-03-02T00:00:00.000000Z')
    with env.store.transaction(write=True):assert pm.record_project_measurement(env.store,run_id='measurement-run',measurement=m)==rid
    assert pm.measurement_detail(env.store,rid)==record
    changed={**m,'baseline_note':'changed'}
    with pytest.raises(pm.MeasurementError,match='replay differs'):
        with env.store.transaction(write=True):pm.record_project_measurement(env.store,run_id='measurement-run',measurement=changed)
    env.store.insert('runs',{'id':'fail-run','started':START});env.store.commit()
    with pytest.raises(RuntimeError,match='invented'):
        with env.store.transaction(write=True):
            pm.record_project_measurement(env.store,run_id='fail-run',measurement=m)
            raise RuntimeError('invented interrupted transaction')
    assert env.store.query_one('SELECT COUNT(*) n FROM project_stats')['n']==1


def test_collector_keeps_original_windows_for_old_revisions(env):
    from self_improve import project_measurements as pm
    revision,key=cohort(env)
    env.store.insert('runs',{'id':'much-later','started':'2031-01-01T00:00:00Z'});env.store.commit()
    got=pm.collect_project_measurements(env.store,run_id='much-later',observed_at='2031-01-01T00:00:00Z')
    retained=pm.measurement_detail(env.store,got['measurement_ids'][0])['measurement']
    assert retained['before']['sessions']==retained['after']['sessions']==20
    assert retained['relative_change_pct']==-50


def test_cross_activation_session_never_receives_post_startup_credit(env):
    revision,key=cohort(env)
    write_codex(env,[meta(env.repo,'2029-12-25T00:00:00Z','cross-session'),x_call('2029-12-25T00:01:00Z','cross'),
                     x_user("no, that's wrong",'2030-01-04T00:00:00Z')],name='rollout-cross.jsonl')
    scan(env,'cross');got=calculate(env,revision,key)
    assert got['before']['eligible_lines']==82 and got['after']['eligible_lines']==80
    assert got['after']['excluded_session_pairs']['session_started_outside_window']==1


def test_deleted_transcripts_and_identical_rescans_retain_counts(env):
    from self_improve.scan import mark_for_rescan
    revision,key=cohort(env);a=calculate(env,revision,key)
    mark_for_rescan(env.store,env.cfg);scan(env,'repeat');b=calculate(env,revision,key)
    for side in ('before','after'):
        assert a[side]['occurrences']==b[side]['occurrences']
        assert a[side]['eligible_lines']==b[side]['eligible_lines']
        assert a[side]['matched_occurrence_ids']==b[side]['matched_occurrence_ids']
    for path in env.codex_dir.rglob('*.jsonl'):path.unlink()
    assert calculate(env,revision,key)==b


def test_corrupt_trigger_binding_fails_instead_of_changing_the_numerator(env):
    from self_improve.project_measurements import MeasurementError
    revision,key=cohort(env)
    env.store.conn.execute("UPDATE scan_occurrences SET occurred_at='2030-01-25T00:00:00.000000Z' WHERE active=1");env.store.commit()
    with pytest.raises(MeasurementError,match='trigger-line attribution'):
        calculate(env,revision,key)


def test_actual_pipeline_publishes_the_measurement_used_by_both_readers(env,monkeypatch):
    from self_improve import pipeline, project_measurements as pm, rule_availability
    revision,key=cohort(env)
    monkeypatch.setattr(pm,'utc_now_iso',lambda:'2030-02-02T00:00:00.000000Z')
    monkeypatch.setattr(rule_availability,'utc_now_iso',lambda:'2030-02-02T00:00:00.000000Z')
    result=pipeline.run_pipeline(env.cfg,env.store,dry_run=True)
    ids=result['project_measurements']['measurement_ids'];assert len(ids)==1
    assert pm.project_benefit(env.store,project_key=PROJECT)['measurement_ids']==ids
    assert pm.measurement_history(env.store)['records'][0]['id']==ids[0]
    assert pm.measurement_history(env.store,project_key=PROJECT)['records'][0]['id']==ids[0]
    assert pm.measurement_detail(env.store,ids[0])['measurement']['relative_change_pct']==-50
    assert env.store.query_one('SELECT COUNT(*) n FROM llm_calls')['n']==0


def publish(env,m,run='published'):
    from self_improve import project_measurements as pm
    env.store.insert('runs',{'id':run,'started':START});env.store.commit()
    with env.store.transaction(write=True):return pm.record_project_measurement(env.store,run_id=run,measurement=m)


def test_pipeline_mines_new_links_before_both_dashboard_readers(env,monkeypatch):
    from self_improve import pipeline, project_measurements as pm, rule_availability
    from self_improve.dashboard.app import create_app
    from fastapi.testclient import TestClient
    from tests.e2e_corpus import ScriptedLLM, mine_payload
    revision,key=cohort(env)
    sid=str(uuid.uuid4())
    path=write_codex(env,[meta(env.repo,'2030-01-05T01:00:00Z',sid),
        x_call('2030-01-05T01:00:01Z','new-call'),
        x_user("no, that's wrong",'2030-01-05T01:00:02Z'),
        x_tokens('2030-01-05T01:00:03Z')],name='rollout-new-mining.jsonl')
    assert not env.store.query_one('SELECT id FROM incidents WHERE session_file=?',(str(path),))
    llm=ScriptedLLM(mine_responses=[mine_payload('Check the invented condition.',
        dedup_decision='duplicate',dedup_target_id=revision['learning_id'])])
    monkeypatch.setattr(pm,'utc_now_iso',lambda:'2030-02-02T00:00:00.000000Z')
    monkeypatch.setattr(rule_availability,'utc_now_iso',lambda:'2030-02-02T00:00:00.000000Z')
    result=pipeline.run_pipeline(env.cfg,env.store,review_only=True,_llm_factory=llm.factory())
    incident=env.store.query_one('SELECT * FROM incidents WHERE session_file=?',(str(path),))
    assert incident['status']=='mined'
    ids=result['project_measurements']['measurement_ids'];assert len(ids)==1
    measurement=pm.measurement_detail(env.store,ids[0])['measurement']
    assert incident['id'] in {link['incident_id'] for link in measurement['matching']['links']}
    assert measurement['after']['occurrences']==11 and measurement['after']['eligible_lines']==84
    assert measurement['after']['sessions']==21 and measurement['computable']
    assert [call['stage'] for call in llm.calls].count('mine_agentic')==1
    assert {r['provider'] for r in env.store.query('SELECT provider FROM llm_calls')}=={'fake'}
    before=list(env.store.conn.iterdump())
    with TestClient(create_app(env.cfg,db_path=env.store.db_path)) as client:
        projects=client.get('/api/projects').json()['rows']
        project=next(p for p in projects if p['project_key']==PROJECT)
        assert project['benefit']['measurement_ids']==ids
        assert client.get('/api/project-measurements').json()['records'][0]['id']==ids[0]
        assert client.get('/api/project-measurements',params={'project_key':PROJECT}).json()['records'][0]['id']==ids[0]
    assert list(env.store.conn.iterdump())==before


@pytest.mark.parametrize('mutation',["record_json='[]'","project_key='wrong'","record_hash='wrong'"])
def test_retained_corruption_fails_with_owner(env,mutation):
    from self_improve import project_measurements as pm
    revision,key=cohort(env);rid=publish(env,calculate(env,revision,key))
    env.store.conn.execute('UPDATE project_stats SET '+mutation);env.store.commit()
    with pytest.raises(pm.MeasurementError,match=rid):pm.measurement_history(env.store)


def test_pagination_copied_store_and_old_schema_reads_are_read_only(env):
    from self_improve import project_measurements as pm
    from self_improve.store import Store
    from self_improve.dashboard.app import create_app
    from fastapi.testclient import TestClient
    revision,key=cohort(env);m=calculate(env,revision,key)
    for i in range(23):publish(env,m,run='page-'+str(i))
    first=pm.measurement_history(env.store,project_key=PROJECT)
    second=pm.measurement_history(env.store,project_key=PROJECT,cursor=first['next_cursor'])
    assert first['count']==23 and len(first['records'])==20 and len(second['records'])==3
    assert len({r['id'] for r in first['records']+second['records']})==23
    with pytest.raises(pm.MeasurementRequestError):pm.measurement_history(env.store,cursor=first['next_cursor'])
    snapshot=env.tmp/'copy.db'
    with closing(Store(snapshot)) as copy:env.store.conn.backup(copy.conn)
    before=list(env.store.conn.iterdump())
    app=create_app(env.cfg,db_path=snapshot)
    with TestClient(app) as client:
        payload=client.get('/api/project-measurements',params={'project_key':PROJECT}).json()
        assert payload==first
        assert client.get('/api/project-measurements/'+first['records'][0]['id']).json()['id']==first['records'][0]['id']
        assert client.post('/api/project-measurements',json={}).status_code==405
    assert list(env.store.conn.iterdump())==before
    env.store.conn.execute('DELETE FROM schema_migrations WHERE name=?',(pm.MIGRATION,));env.store.commit()
    assert pm.measurement_history(env.store)['reason']=='schema_unavailable'
    assert not env.store.query_one('SELECT name FROM schema_migrations WHERE name=?',(pm.MIGRATION,))


def test_verified_rollback_ends_the_original_application_even_if_a_copy_still_has_text(env,monkeypatch):
    from self_improve import apply
    revision,key=cohort(env)
    monkeypatch.setattr(apply,'utc_now_iso',lambda:'2030-01-02T23:00:00.000000Z')
    outcome=apply.rollback(env.store,env.cfg,revision['proposal_id'])
    assert outcome['outcome']=='rolled_back'
    # Branch delivery is rolled back; this working copy deliberately still has
    # the prior text. It cannot grant the completed application a new lifetime.
    got=calculate(env,revision,key)
    assert got['after']['eligible_lines']==0
    assert got['application_end']['cause']=='verified_rollback'


def test_hash_consistent_wrong_comparison_is_rejected(env):
    from self_improve import project_measurements as pm
    from self_improve import scan_observations as so
    revision,key=cohort(env);rid=publish(env,calculate(env,revision,key))
    row=env.store.query_one('SELECT * FROM project_stats WHERE id=?',(rid,))
    record=json.loads(row['record_json']);record['measurement']['comparison']='higher'
    env.store.update('project_stats','id',rid,{'record_json':so.canonical_json(record),'record_hash':so.content_id(record)});env.store.commit()
    with pytest.raises(pm.MeasurementError,match=rid):pm.project_benefit(env.store,project_key=PROJECT)


def test_unclassified_after_signals_cannot_be_claimed_as_reduced_recurrence(env):
    revision,key=cohort(env,after_matches=20)
    env.store.conn.execute("DELETE FROM incident_learnings WHERE incident_id IN (SELECT id FROM incidents WHERE ts>='2030-01-01')");env.store.commit()
    got=calculate(env,revision,key)
    assert not got['computable'] and got['reason']=='unclassified_signal_occurrences'
    assert got['after']['unclassified_occurrences']==20 and got['after']['rate_per_100k'] is None
    assert got['relative_change_pct'] is None


def test_absent_late_check_does_not_claim_a_complete_post_window(env):
    from self_improve import rule_availability
    revision,key=cohort(env);before=calculate(env,revision,key)
    (Path(env.repo)/'AGENTS.md').write_text('# Rule no longer present\n')
    rule_availability.collect_availability(env.store,env.cfg,observed_at=END)
    after=calculate(env,revision,key)
    assert before['copies'][0]['partial_windows']['after']
    assert after['copies'][0]['partial_windows']['after']
    assert after['copies'][0]['uncovered_after_intervals']==before['copies'][0]['uncovered_after_intervals']


def test_zero_baseline_retains_absolute_change_without_a_relative_percent(env):
    revision,key=cohort(env)
    env.store.insert('learnings',{'id':'other-learning','title':'Different invented lesson',
        'rule_text':'Check a different invented condition.','created_at':START})
    ids=[r['id'] for r in env.store.query("SELECT id FROM incidents WHERE ts<'2030-01-01'")]
    for iid in ids:
        env.store.conn.execute('DELETE FROM incident_learnings WHERE incident_id=?',(iid,))
        env.store.insert('incident_learnings',{'incident_id':iid,'learning_id':'other-learning'})
    env.store.commit();got=calculate(env,revision,key)
    assert got['computable'] and got['before']['rate_per_100k']==0
    assert got['relative_change_pct'] is None and got['absolute_rate_change']==12500
    assert got['comparison']=='higher'


def test_unavailable_label_and_invented_match_are_refused_by_the_public_writer(env):
    from self_improve import project_measurements as pm
    revision,key=cohort(env);m=calculate(env,revision,key)
    env.store.insert('runs',{'id':'invalid-source','started':START});env.store.commit()
    bad={**m,'computable':False}
    with pytest.raises(pm.MeasurementError):
        with env.store.transaction(write=True):pm.record_project_measurement(env.store,run_id='invalid-source',measurement=bad)
    changed=json.loads(json.dumps(m));changed['matching']['links'][0]['incident_id']='invented'
    with pytest.raises(pm.MeasurementError,match='source evidence differs'):
        with env.store.transaction(write=True):pm.record_project_measurement(env.store,run_id='invalid-source',measurement=changed)
    assert env.store.query('SELECT * FROM project_stats')==[]


def test_supported_reapplication_never_reopens_predecessor_availability(env,monkeypatch):
    from self_improve import apply,rule_availability,reapplications,rule_revisions
    from self_improve.commands import submit_command
    from self_improve.worker import run_once
    from tests.test_delivery_worker import approve
    from tests.test_apply import run_git
    revision,key=cohort(env)
    monkeypatch.setattr(apply,'utc_now_iso',lambda:'2030-01-02T23:00:00.000000Z')
    assert apply.rollback(env.store,env.cfg,revision['proposal_id'])['outcome']=='rolled_back'
    shown=reapplications.preview(env.store,env.cfg,revision['proposal_id']);assert shown['ready']
    request=submit_command(env.store,env.cfg,{'action':'request_reapplication','proposal_id':revision['proposal_id'],
        'preview_revision':shown['revision'],'request_key':'reapply-test'})
    draft=env.store.query_one('SELECT * FROM proposals WHERE id=?',(request['result']['proposal_id'],))
    monkeypatch.setattr(apply,'utc_now_iso',lambda:'2030-01-04T00:00:00.000000Z')
    approve(env.store,env.cfg,draft);assert run_once(env.store,env.cfg)['state']=='completed'
    run_git(['merge','--ff-only',env.cfg.project_branch_name],Path(env.repo))
    result=rule_availability.collect_availability(env.store,env.cfg,observed_at='2030-02-03T00:00:00Z')
    assert result['outcomes']=={'available':1,'unknown':1}
    history=rule_availability.project_availability(env.store,project_key=PROJECT)['records']
    old=next(r for r in history if r['revision']['id']==revision['id'])
    assert old['status']=='unknown' and old['observations'][0]['cause']=='verified_rollback'
    records=rule_revisions.retained_revisions(env.store);assert len(records)==2
    successor=next(r for r in records if r['id']!=revision['id'])
    assert successor['proposal_id']!=revision['proposal_id'] and successor['application_id']!=revision['application_id']
    assert calculate(env,revision,key)['after']['eligible_lines']==0


def test_new_version_remains_separate_and_missing_version_is_not_zero(env):
    from dataclasses import replace
    from self_improve.scan import mark_for_rescan
    revision,key=cohort(env)
    other=replace(env.cfg,max_incidents_per_signal_per_session=2)
    mark_for_rescan(env.store,other);scan(env,'changed-version',cfg=other)
    got=calculate(env,revision,None)
    assert got['reason']=='incompatible_versions' and not got['computable']
    assert calculate(env,revision,key)['before']['eligible_lines']==80
    assert calculate(env,revision,'missing-version')['reason']=='uncovered_version'


@pytest.mark.parametrize('side',['before','after'])
@pytest.mark.parametrize('boundary',['compaction','copy_move','unknown_start'])
def test_real_recurrence_selection_honors_native_context_boundaries(env,side,boundary):
    from tests.test_session_context import turn
    from tests.test_scan_observations import make_repo
    revision,key=cohort(env)
    day='2029-12-21' if side=='before' else '2030-01-04'
    at=lambda sec:f'{day}T01:00:{sec:02d}Z'
    first=meta(env.repo,at(0),str(uuid.uuid4()))
    if boundary=='unknown_start':first['payload'].pop('timestamp')
    stop={'type':'compacted','timestamp':at(2),'payload':{}}
    if boundary=='copy_move':stop=turn(make_repo(env.tmp/'another-copy'),at(2))
    write_codex(env,[first,x_call(at(1),'boundary-call'),stop,
        x_user("no, that's wrong",at(3))],name='rollout-boundary.jsonl')
    scan(env,'boundary');got=calculate(env,revision,key)
    assert got[side]['eligible_lines']==(80 if boundary=='unknown_start' else 82)
    assert got['before']['occurrences']==20 and got['after']['occurrences']==10
    assert got[side]['sessions']==(20 if boundary=='unknown_start' else 21)


def test_late_data_changes_a_later_measurement_without_rewriting_the_archive(env):
    from self_improve import project_measurements as pm
    revision,key=cohort(env);old=publish(env,calculate(env,revision,key))
    frozen=pm.measurement_detail(env.store,old)
    path=write_codex(env,[meta(env.repo,'2030-01-08T00:00:00Z',str(uuid.uuid4())),
        x_call('2030-01-08T00:00:01Z','late'),x_user("no, that's wrong",'2030-01-08T00:00:02Z')],name='rollout-late.jsonl')
    scan(env,'late')
    incident=env.store.query_one('SELECT id FROM incidents WHERE session_file=?',(str(path),))
    env.store.link_incident_learning(incident['id'],revision['learning_id']);env.store.commit()
    new=publish(env,calculate(env,revision,key),run='later')
    assert new!=old and pm.measurement_detail(env.store,old)==frozen
    latest=pm.measurement_detail(env.store,new)['measurement']
    assert latest['after']['eligible_lines']==83 and latest['after']['occurrences']==11


def test_rebuild_preserves_measurements_and_required_source_history(env):
    from self_improve import project_measurements as pm
    from self_improve.rebuild import rebuild_state
    revision,key=cohort(env);rid=publish(env,calculate(env,revision,key))
    frozen=pm.measurement_detail(env.store,rid)
    before=calculate(env,revision,key)
    destination=env.tmp/'history-backup'
    rebuild_state(env.store,export_path=destination)
    assert pm.measurement_detail(env.store,rid)==frozen
    backup=json.loads((destination/'preserved.json').read_text())
    assert [r['id'] for r in backup['project_measurements']]==[rid]
    assert calculate(env,revision,key)==before


def test_two_signals_on_one_line_increase_only_the_occurrence_numerator(env):
    revision,key=cohort(env)
    path=write_codex(env,[meta(env.repo,'2030-01-07T00:00:00Z',str(uuid.uuid4())),
        x_call('2030-01-07T00:00:01Z','two-signals'),
        x_user("no, that's wrong??",'2030-01-07T00:00:02Z')],name='rollout-two-signals.jsonl')
    scan(env,'two-signals')
    incidents=env.store.query('SELECT * FROM incidents WHERE session_file=?',(str(path),))
    assert {r['signal_type'] for r in incidents}=={'correction','frustration'}
    for row in incidents:env.store.link_incident_learning(row['id'],revision['learning_id'])
    env.store.commit();got=calculate(env,revision,key)
    assert got['after']['eligible_lines']==83 and got['after']['occurrences']==12
    assert got['after']['occurrences_by_signal']=={'correction':11,'frustration':1}


def test_interval_batches_preserve_counts_and_union_overlapping_qualifications(env,monkeypatch):
    import sqlite3
    from self_improve import session_context as sc
    revision,key=cohort(env,before=140,after=140,after_matches=70)
    env.store.conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER,999)
    baseline=calculate(env,revision,key)
    original=sc._qualify
    def duplicate(*args,**kwargs):
        result=original(*args,**kwargs)
        return {**result,'intervals':result['intervals']*2}
    monkeypatch.setattr(sc,'_qualify',duplicate)
    got=calculate(env,revision,key)
    assert got==baseline
    assert got['before']['eligible_lines']==got['after']['eligible_lines']==560
    assert got['before']['sessions']==got['after']['sessions']==140
    assert got['after']['occurrences']==70 and got['relative_change_pct']==-50
