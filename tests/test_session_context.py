"""Native context and availability qualification over temporary, invented state."""
from dataclasses import replace
from contextlib import closing
import json
from pathlib import Path

import pytest

from self_improve.scan import mark_for_rescan
from self_improve.sources.codex import CodexSource
from self_improve import scan_observations as so
from tests.test_scan_observations import make_env, make_repo, scan, ts, PROJECT, SID, SID_B, jsonl, write_claude, c_user
from tests.test_scan_occurrences import write_codex, x_meta, x_user, x_call, BETA


@pytest.fixture
def env(tmp_path):
    value = make_env(tmp_path, claude_managed_dir=str(tmp_path/'managed'), global_claude_md=str(tmp_path/'home/CLAUDE.md'),
                     codex_global_agents_md=str(tmp_path/'codex/AGENTS.md'),
                     skills_dir=str(tmp_path/'home/skills'), codex_skills_dir=str(tmp_path/'codex/skills'))
    try:
        yield value
    finally:
        value.store.close()


def meta(cwd, when=ts(0), sid=SID, **extra):
    result=x_meta(when,cwd,sid);result['payload'].update(timestamp=when,cli_version='0.100.0',**extra)
    return result


def turn(cwd, when=ts(1), turn_id='turn-1'):
    return {'type':'turn_context','timestamp':when,'payload':{'cwd':cwd,'turn_id':turn_id}}


def test_native_turn_cwd_moves_physical_exposure_and_queue_evidence(env):
    second=make_repo(env.tmp/'other','https://github.com/example/beta.git')
    write_codex(env,[meta(env.repo),x_call(ts(1),'call-1'),turn(second,ts(2)),x_user("no, that's wrong",ts(3))])
    scan(env,'moved')
    row=env.store.query_one("SELECT project_key FROM scan_lines WHERE line_no=4 AND active=1")
    assert row['project_key']==BETA
    assert env.store.query_one("SELECT project_key FROM scan_occurrences WHERE kind='signal' AND active=1")['project_key']==BETA


def test_invalid_turn_cwd_clears_prior_attribution_instead_of_carrying_it(env):
    write_codex(env,[meta(env.repo),turn(['invalid']),x_user('later',ts(2))])
    scan(env,'invalid-cwd')
    assert env.store.query_one('SELECT project_key FROM scan_lines WHERE line_no=3 AND active=1')['project_key']==''


def test_native_metadata_does_not_carry_creation_time_to_lines(env):
    record=meta(env.repo);record['payload']['timestamp']='2026-01-01T01:00:00Z'
    path=write_codex(env,[record,turn(env.repo)])
    source=CodexSource(env.cfg);list(source.parse(str(path),record_lines=True))
    first,second=source.parse_stats.line_records
    assert first.ts_raw==ts(0) and second.ts_raw==ts(1)
    assert first.session_context['reported_started_at']=='2026-01-01T01:00:00.000000Z'
    assert second.session_context['kind']=='turn'


def test_new_incident_uses_its_trigger_copy_instead_of_file_creation_copy(env):
    second=make_repo(env.tmp/'other','https://github.com/example/beta.git')
    write_codex(env,[meta(env.repo),x_call(ts(1),'call-1'),turn(second,ts(2)),x_user("no, that's wrong",ts(3))])
    scan(env,'new-incident')
    assert env.store.query_one("SELECT project_key FROM incidents WHERE signal_type='correction'")['project_key']==BETA


def page(env, **kwargs):
    from self_improve.session_context import project_sessions
    return project_sessions(env.store,project_key=PROJECT,**kwargs)


def test_scanner_publishes_native_batch_and_keeps_first_line_time_distinct(env):
    record=meta(env.repo);record['payload']['timestamp']='2026-08-09T00:00:00Z'
    path=write_codex(env,[record,turn(env.repo),x_user('Invented private-looking body is not native metadata',ts(2))])
    assert scan(env,'first').files_succeeded==1
    got=page(env);row=got['records'][0]
    assert row['reported_started_at']=='2026-08-09T00:00:00.000000Z'
    assert row['first_recorded_at']==so.normalize_timestamp(ts(0))
    assert row['physical_lines']==3 and len(row['native_context'])==2
    assert got['in_force_sessions'] is None and not row['runtime_loading_verified']
    assert row['native_context'][0]['provider_version']=='0.100.0'
    assert row['context_batch_ids']==row['scan_observation_ids']
    exported=str(env.store.query('SELECT * FROM session_context_records'))
    assert 'private-looking' not in exported and 'base_instructions' not in exported
    path.unlink()
    assert page(env)==got  # reader needs neither transcript nor cwd reads


def test_rescan_replaces_current_context_without_double_counting_and_can_remove_it(env):
    path=write_codex(env,[meta(env.repo),turn(env.repo),x_user('message',ts(2))])
    scan(env,'first');first=page(env)
    mark_for_rescan(env.store,env.cfg);scan(env,'second');second=page(env)
    assert first['count']==second['count']==1
    assert len(second['records'][0]['native_context'])==2
    assert first['records'][0]['context_batch_ids']!=second['records'][0]['context_batch_ids']
    assert env.store.query_one('SELECT COUNT(*) n FROM session_context_batches')['n']==2
    # Removing only creation timestamp removes candidate evidence, never revives the prior batch.
    changed=meta(env.repo);changed['payload'].pop('timestamp')
    path.write_bytes(jsonl([changed,turn(env.repo),x_user('message',ts(2))]))
    mark_for_rescan(env.store,env.cfg);scan(env,'third')
    assert page(env)['records'][0]['start_reason']=='native_creation_time_unknown'
    path.write_bytes(b'');mark_for_rescan(env.store,env.cfg);scan(env,'empty')
    assert page(env)['count']==0 and page(env)['coverage']['physical_lines']==0
    assert env.store.query_one('SELECT COUNT(*) n FROM session_context_batches')['n']==4


def test_no_native_start_for_claude_first_message_or_instruction_shaped_codex_message(env):
    write_claude(env,[c_user('Start',ts(0),env.repo),c_user('Next',ts(1),env.repo)])
    record=x_meta(ts(0),env.repo)
    write_codex(env,[record,x_user('# AGENTS.md instructions for /invented\nKeep a rule',ts(1))])
    scan(env,'unknown')
    got=page(env)
    assert got['count']==2 and got['coverage']['unknown_creation_pairs']==2
    assert {r['start_reason'] for r in got['records']}=={'native_creation_missing','native_creation_time_unknown'}


@pytest.mark.parametrize('change,reason',[
    ({'timestamp':'not-a-time'},'native_creation_time_unknown'),
    ({'timestamp':'2026-08-10T10:30:00Z'},'creation_after_retained_activity'),
    ({'forked_from_id':SID_B},'inherited_history'),
    ({'history_base':{'path':'ignored'}},'inherited_history'),
    ({'id':SID_B},'shared_root_session_identity'),
    ({'cli_version':['bad']},'invalid_native_identity'),
])
def test_ambiguous_creation_never_grants_startup_credit(env,change,reason):
    record=meta(env.repo);record['payload'].update(change)
    write_codex(env,[record,x_user('message',ts(1))]);scan(env,'ambiguous')
    row=page(env)['records'][0]
    assert row['reported_started_at'] is None and row['start_reason']==reason


def test_repeated_metadata_same_identity_is_consistent_but_changed_creation_is_unknown(env):
    path=write_codex(env,[meta(env.repo),meta(env.repo),turn(env.repo)])
    scan(env,'same');assert page(env)['records'][0]['reported_started_at']
    path.write_bytes(jsonl([meta(env.repo),meta(env.repo,ts(1)),x_user('message',ts(2))]))
    mark_for_rescan(env.store,env.cfg);scan(env,'different')
    assert page(env)['records'][0]['start_reason']=='conflicting_creation_records'


def test_parser_and_configuration_versions_do_not_mix(env):
    write_codex(env,[meta(env.repo),turn(env.repo)]);scan(env,'one')
    old=page(env)['compatibility_key']
    mark_for_rescan(env.store,env.cfg)
    scan(env,'two',cfg=replace(env.cfg,correction_max_len=101))
    result=page(env)
    assert result['count'] is None and result['reason']=='incompatible_versions'
    assert len(result['version_groups'])==2
    assert page(env,compatibility_key=old)['count']==1
    assert page(env,compatibility_key='not-covered')['count'] is None


def test_partial_tail_and_unknown_timestamps_are_explicit(env):
    record=meta(env.repo);record.pop('timestamp')
    path=write_codex(env,[record,x_user('message',ts(1))])
    with path.open('ab') as stream: stream.write(b'{"type":"turn_context"')
    scan(env,'partial');got=page(env)
    assert got['coverage']['unknown_time_lines']==1 and got['coverage']['pending_tails'][0]['bytes']>0
    assert got['records'][0]['start_reason']=='creation_line_time_unknown'


def test_context_publication_failure_rolls_back_lines_incidents_and_offset(env,monkeypatch):
    path=write_codex(env,[meta(env.repo),turn(env.repo)])
    scan(env,'before');old=page(env);lines=env.store.query('SELECT * FROM scan_lines')
    batches=env.store.query('SELECT * FROM session_context_batches')
    path.write_bytes(jsonl([meta(env.repo),turn(env.repo),x_user('new',ts(2))]))
    original=env.store.insert
    def fail(table,record):
        if table=='session_context_records': raise OSError('invented context disk failure')
        return original(table,record)
    monkeypatch.setattr(env.store,'insert',fail)
    assert scan(env,'failed').files_failed==1
    assert env.store.query('SELECT * FROM scan_lines')==lines
    assert env.store.query('SELECT * FROM session_context_batches')==batches
    current=page(env)
    assert current['records'][0]['context_batch_ids']==old['records'][0]['context_batch_ids']
    assert current['coverage']['failed_scan_observations']


def test_replay_requires_transaction_and_changed_payload_fails(env):
    from self_improve import session_context as sc
    write_codex(env,[meta(env.repo),turn(env.repo)]);scan(env,'first')
    record=env.store.query_one('SELECT * FROM scan_observations')
    manifest=json.loads(env.store.query_one('SELECT manifest_json FROM scan_manifests')['manifest_json'])
    observation={**record,'manifest':manifest}
    contexts=[json.loads(r['record_json']) for r in env.store.query('SELECT record_json FROM session_context_records ORDER BY line_no')]
    native=[(r['line_no'],{k:r[k] for k in sc.NATIVE_FIELDS}) for r in contexts]
    lines=[]
    for r in env.store.query('SELECT * FROM scan_lines ORDER BY line_no'):
        wc=env.store.query_one('SELECT * FROM scan_working_copies WHERE id=?',(r['working_copy_id'],))
        lines.append({**r,'working_copy':wc})
    with pytest.raises(sc.SessionContextError,match='active write transaction'):
        sc.record_scan_context(env.store,observation=observation,lines=lines,contexts=native)
    with env.store.transaction():
        env.store.conn.execute('UPDATE sessions SET mtime=mtime')
        assert sc.record_scan_context(env.store,observation=observation,lines=lines,contexts=native)==record['id']
    with env.store.transaction():
        env.store.conn.execute('UPDATE sessions SET mtime=mtime')
        native[0][1]['provider_version']='different'
        with pytest.raises(sc.SessionContextError,match='replay differs'):
            sc.record_scan_context(env.store,observation=observation,lines=lines,contexts=native)


@pytest.mark.parametrize('mutation',[
    "UPDATE session_context_records SET record_json='[]'",
    "UPDATE session_context_records SET working_copy_id='other'",
    "UPDATE session_context_batches SET record_count=100",
    "UPDATE scan_manifests SET compatibility_key='different'",
])
def test_corrupt_retained_context_is_named_instead_of_becoming_unknown(env,mutation):
    from self_improve.session_context import SessionContextError
    write_codex(env,[meta(env.repo),turn(env.repo)]);scan(env,'first')
    env.store.conn.execute(mutation);env.store.commit()
    with pytest.raises((SessionContextError,so.ScanObservationError),match='context|manifest'):
        page(env)


def test_denied_metadata_does_not_enter_context_archive(env):
    denied=make_repo(env.tmp/'denied-tree','https://github.com/example/denied.git')
    write_codex(env,[meta(denied),turn(denied),x_user('denied body',ts(2))]);scan(env,'denied')
    assert env.store.query('SELECT * FROM session_context_records')==[]
    assert env.store.query_one('SELECT record_count FROM session_context_batches')['record_count']==0


def at(day,hour=0): return f'2030-01-{day:02d}T{hour:02d}:00:00Z'


def applied(env, *, last_check=5):
    from tests.test_apply import init_git_repo,run_git
    from tests.test_rule_availability import delivery
    from self_improve.execution_policy import set_class_policy
    from self_improve import rule_availability,rule_revisions
    repo=init_git_repo(Path(env.repo),'AGENTS.md','# Invented instructions\n')
    set_class_policy(env.store,'project',True,now='2020-01-01T00:00:00Z')
    proposal,text=delivery(env.store,env.cfg,repo)
    run_git(['merge','--ff-only',env.cfg.project_branch_name],repo)
    rule_availability.collect_availability(env.store,env.cfg,observed_at=at(2))
    if last_check: rule_availability.collect_availability(env.store,env.cfg,observed_at=at(last_check))
    revision=rule_revisions.retained_revisions(env.store)[0]
    return revision,repo,text


def qualify(env,revision,**kwargs):
    from self_improve.session_context import session_eligibility
    return session_eligibility(env.store,rule_revision_id=revision['id'],
        working_copy_id=so.working_copy_identity(PROJECT,env.repo)['id'],
        compatibility_key=page(env)['compatibility_key'],start=kwargs.pop('start',at(1)),end=kwargs.pop('end',at(10)),**kwargs)


def test_actual_file_checks_and_native_start_qualify_only_observation_supported_intervals(env):
    write_codex(env,[meta(env.repo,at(3)),turn(env.repo,at(3,1)),x_user('message',at(4))]);scan(env,'new')
    revision,repo,text=applied(env)
    result=qualify(env,revision);row=result['records'][0]['qualification']
    assert row['status']=='candidate'
    assert [(p['start'],p['end']) for p in row['intervals']]==[(so.normalize_timestamp(at(3)),so.normalize_timestamp(at(5)))]
    assert row['in_force'] is None and not row['continuity_verified']
    # The UI's all-history qualification uses the same reader.
    ui=page(env,rule_revision_id=revision['id'])['records'][0]['qualification']
    assert ui['intervals']==row['intervals'] and ui['status']==row['status']


def test_session_already_active_before_available_check_has_no_startup_credit(env):
    write_codex(env,[meta(env.repo,at(1)),turn(env.repo,at(3)),x_user('later',at(4))]);scan(env,'old')
    revision,_,_=applied(env)
    result=qualify(env,revision)['records'][0]['qualification']
    assert result['status']=='excluded' and result['reason']=='started_before_observed_availability'
    assert result['intervals']==[]


def test_single_check_cannot_claim_continuity(env):
    write_codex(env,[meta(env.repo,at(2)),x_user('message',at(3))]);scan(env,'one')
    revision,_,_=applied(env,last_check=None)
    row=qualify(env,revision)['records'][0]['qualification']
    assert row['reason']=='no_confirmed_continuity' and row['intervals']==[]


def test_compaction_and_copy_moves_bound_candidates_without_inventing_receipt(env):
    second=make_repo(env.tmp/'second','https://github.com/example/alpha.git')
    write_codex(env,[meta(env.repo,at(3)),turn(env.repo,at(3,1)),
                     {'type':'compacted','timestamp':at(4),'payload':{'message':'unretained'}},
                     turn(second,at(6)),x_user('moved',at(7))]);scan(env,'moved')
    revision,_,_=applied(env,last_check=8)
    row=qualify(env,revision)['records'][0]['qualification']
    assert row['intervals'][0]['end']==so.normalize_timestamp(at(4))
    other=[r for r in page(env)['records'] if r['working_copy_path']==second][0]
    assert other['reported_started_at'] is None and other['start_reason']=='creation_in_different_copy'


def test_scope_must_match_provider_and_unconditional_startup(env):
    from self_improve import rule_availability
    write_codex(env,[meta(env.repo,at(3)),x_user('message',at(4))]);scan(env,'scope')
    revision,repo,text=applied(env)
    # A new on-demand-only observation cannot give startup credit to a new session.
    (repo/'AGENTS.md').write_text('# Human\n')
    skill=repo/'.agents/skills/invented';skill.mkdir(parents=True)
    (skill/'SKILL.md').write_text('---\nname: invented\ndescription: invented fixture\n---\n'+text)
    rule_availability.collect_availability(env.store,env.cfg,observed_at=at(6))
    rule_availability.collect_availability(env.store,env.cfg,observed_at=at(9))
    write_codex(env,[meta(env.repo,at(7),SID_B),x_user('later',at(8))],name='rollout-other.jsonl');scan(env,'skill')
    rows=qualify(env,revision)['records']
    later=[r for r in rows if r['reported_started_at']==so.normalize_timestamp(at(7))][0]
    assert later['qualification']['status']=='unknown'
    assert later['qualification']['reason']=='provider_or_loading_scope_unverified'


def test_pagination_is_complete_and_cursor_binds_project_version_revision_and_copy(env):
    from self_improve.session_context import SessionContextRequestError
    for i in range(23):
        write_codex(env,[meta(env.repo,sid=f'session-{i:02}'),turn(env.repo)],name=f'rollout-{i}.jsonl')
    scan(env,'pages');first=page(env);second=page(env,cursor=first['next_cursor'])
    assert first['count']==23 and len(first['records'])==20 and len(second['records'])==3
    assert second['next_cursor'] is None
    assert len({r['id'] for r in first['records']+second['records']})==23
    for selector in ({'compatibility_key':'other'},{'rule_revision_id':'other'},{'working_copy_id':'other'}):
        with pytest.raises(SessionContextRequestError,match='cursor'):
            page(env,cursor=first['next_cursor'],**selector)


def test_rebuild_missing_projection_is_unknown_and_context_is_backed_up(env):
    from self_improve.rebuild import rebuild_state
    write_codex(env,[meta(env.repo),turn(env.repo)]);scan(env,'before')
    backup=env.tmp/'private-export'
    rebuild_state(env.store,export_path=backup)
    env.store.commit()
    assert env.store.query_one('SELECT COUNT(*) n FROM session_context_batches')['n']==1
    assert any('session_context' in p.read_text() for p in backup.glob('*.json'))
    result=page(env)
    assert result['count'] is None and result['reason']=='incomplete_physical_projection'
    assert result['coverage']['incomplete_physical_projections']


def test_unknown_schema_read_does_not_migrate_and_writer_refuses(env):
    from self_improve.store import Store
    env.store.conn.execute('DROP TABLE session_context_records');env.store.conn.execute('DROP TABLE session_context_batches')
    env.store.conn.execute('DELETE FROM schema_migrations WHERE name=?',('0030_session_context',));env.store.commit()
    with closing(Store(env.store.db_path,read_only=True)) as reader:
        from self_improve.session_context import project_sessions
        assert project_sessions(reader,project_key=PROJECT)['reason']=='schema_unavailable'
        assert not reader.query_one('SELECT name FROM schema_migrations WHERE name=?',('0030_session_context',))
    write_codex(env,[meta(env.repo),turn(env.repo)])
    result=scan(env,'upgrade-required')
    assert result.files_failed==1 and env.store.query('SELECT * FROM scan_lines')==[]


def test_half_open_qualification_window_excludes_boundary_activity(env):
    write_codex(env,[meta(env.repo,at(3)),x_user('message',at(4))]);scan(env,'window')
    revision,_,_=applied(env)
    assert qualify(env,revision,end=at(3))['records']==[]
    row=qualify(env,revision,start=at(4),end=at(5))['records'][0]
    assert row['qualification']['intervals'][0]['start']==so.normalize_timestamp(at(4))
    assert row['qualification']['intervals'][0]['end']==so.normalize_timestamp(at(5))


def test_changed_then_restored_rule_does_not_reactivate_an_already_running_session(env):
    from self_improve import rule_availability
    write_codex(env,[meta(env.repo,at(3)),x_user('message',at(8))]);scan(env,'window')
    revision,repo,text=applied(env)
    (repo/'AGENTS.md').write_text('# Changed\n'+text.replace('readable.','edited.'))
    rule_availability.collect_availability(env.store,env.cfg,observed_at=at(6))
    (repo/'AGENTS.md').write_text('# Restored\n'+text)
    for day in (7,9): rule_availability.collect_availability(env.store,env.cfg,observed_at=at(day))
    row=qualify(env,revision)['records'][0]['qualification']
    assert len(row['intervals'])==1 and row['intervals'][0]['end']==so.normalize_timestamp(at(5))


def test_missing_turn_cwd_and_unknown_compaction_time_block_continuity(env):
    write_codex(env,[meta(env.repo,at(3)),{'type':'compacted','payload':{}},turn(None,at(4)),x_user('unknown scope',at(5))])
    scan(env,'unknown-scope');revision,_,_=applied(env,last_check=8)
    row=qualify(env,revision)['records'][0]['qualification']
    assert row['intervals']==[] and row['reason']=='untimed_context_discontinuity'


def test_session_api_uses_selected_copy_get_only_and_reports_bad_selectors(env,tmp_path):
    pytest.importorskip('fastapi')
    from fastapi.testclient import TestClient
    from self_improve.dashboard.app import create_app
    from self_improve.store import Store
    write_codex(env,[meta(env.repo),turn(env.repo)]);scan(env,'copy')
    private_copy=tmp_path/'selected';private_copy.mkdir()
    with closing(Store(private_copy/'state.db')) as target: env.store.conn.backup(target.conn)
    # Remove the source archive only in the original Store. The API must still
    # read the selected backup, and must never migrate either Store.
    env.store.conn.execute('DELETE FROM session_context_records');env.store.conn.execute('DELETE FROM session_context_batches');env.store.commit()
    config=replace(env.cfg,state_dir=str(private_copy))
    with TestClient(create_app(config)) as client:
        got=client.get('/api/project-sessions',params={'project_key':PROJECT})
        assert got.status_code==200 and got.json()['records'][0]['reported_started_at']
        assert client.get('/api/project-sessions',params={'project_key':PROJECT,'cursor':'bad'}).status_code==400
        assert client.get('/api/project-sessions',params={'project_key':PROJECT,'rule_revision_id':'missing'}).status_code==400
        assert client.post('/api/project-sessions',json={}).status_code==405
    with closing(Store(private_copy/'state.db',read_only=True)) as reader:
        assert reader.query_one('SELECT COUNT(*) n FROM commands')['n']==0
        assert reader.query_one('SELECT COUNT(*) n FROM llm_calls')['n']==0
        assert reader.query_one('SELECT COUNT(*) n FROM session_context_batches')['n']==1


def test_session_reader_transfers_groups_instead_of_whole_physical_line_history(env,monkeypatch):
    write_codex(env,[meta(env.repo),turn(env.repo)]+[x_user('Invented repeated activity',ts(2)) for _ in range(400)])
    scan(env,'large');original=env.store.query
    observed=[]
    def bounded(sql,params=()):
        rows=original(sql,params)
        if 'FROM scan_lines' in sql and not 'scan_observations' in sql:
            observed.append(len(rows))
            assert len(rows)<10, 'Session inspection transferred every physical line instead of its grouped counts'
        return rows
    monkeypatch.setattr(env.store,'query',bounded)
    got=page(env)
    assert observed and got['records'][0]['physical_lines']==402
    assert got['records'][0]['first_recorded_at']==so.normalize_timestamp(ts(0))
    assert got['records'][0]['last_recorded_at']==so.normalize_timestamp(ts(2))


def test_context_writer_binds_to_persisted_physical_line_not_just_caller_input(env):
    from self_improve import session_context as sc
    write_codex(env,[meta(env.repo),turn(env.repo)]);scan(env,'first')
    stored=env.store.query_one('SELECT * FROM scan_observations')
    manifest=json.loads(env.store.query_one('SELECT manifest_json FROM scan_manifests')['manifest_json'])
    observation={**stored,'manifest':manifest}
    records=[json.loads(r['record_json']) for r in env.store.query('SELECT record_json FROM session_context_records ORDER BY line_no')]
    native=[(r['line_no'],{k:r[k] for k in sc.NATIVE_FIELDS}) for r in records]
    lines=[]
    for row in env.store.query('SELECT * FROM scan_lines ORDER BY line_no'):
        wc=env.store.query_one('SELECT * FROM scan_working_copies WHERE id=?',(row['working_copy_id'],))
        lines.append({**row,'working_copy':wc,'project_key':'forged-project'})
    with pytest.raises(sc.SessionContextError,match='physical line'):
        with env.store.transaction():
            env.store.conn.execute('DELETE FROM session_context_records')
            env.store.conn.execute('DELETE FROM session_context_batches')
            sc.record_scan_context(env.store,observation=observation,lines=lines,contexts=native)
    assert env.store.query_one('SELECT COUNT(*) n FROM session_context_batches')['n']==1
