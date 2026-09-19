"""Native hook intake, using invented payloads and temporary resources only."""
from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import shlex
import subprocess
import sys
from uuid import uuid4

import pytest
from tests.test_session_context import env
from tests.test_scan_observations import PROJECT, SID, make_repo
from self_improve import scan_observations as so
from self_improve.store import Store


def event(env, kind='InstructionsLoaded', **changes):
    return dict(session_id=SID, transcript_path=str(env.tmp/'deleted.jsonl'), cwd=str(env.repo),
                hook_event_name=kind, file_path=str(Path(env.repo)/'CLAUDE.md'), memory_type='Project',
                load_reason='session_start') | changes


def native():
    from self_improve import native_loads
    return native_loads


def test_real_receiver_retains_native_path_without_reading_loaded_body(env):
    api=native();payload=event(env, prompt='never retain', content='never retain', timestamp='2020-01-01T00:00:00Z')
    got=api.record_native_event(env.store,env.cfg,payload)
    assert got['occurred_at'] is None and got['loaded_content_hash'] is None
    assert got['project_key']==PROJECT and got['logical_session_key']==so.logical_session_key('claude',SID)
    assert 'never retain' not in str(env.store.query('SELECT * FROM native_load_reports'))
    history=api.native_history(env.store,project_key=PROJECT)
    assert history['records']==[got] and history['count']==1
    assert history['in_force_instructions'] is None and history['coverage_complete'] is False


def test_transport_replay_retains_first_receipt_but_native_repeats_remain_separate(env):
    api=native();rid=str(uuid4());payload=event(env)
    first=api.record_native_event(env.store,env.cfg,payload,receipt_id=rid,received_at='2026-01-01T00:00:00Z')
    again=api.record_native_event(env.store,env.cfg,payload,receipt_id=rid,received_at='2026-01-02T00:00:00Z')
    assert first==again
    changed={**payload,'load_reason':'compact'}
    with pytest.raises(api.NativeLoadError,match='replay'):
        api.record_native_event(env.store,env.cfg,changed,receipt_id=rid)
    api.record_native_event(env.store,env.cfg,payload)
    assert api.native_history(env.store,project_key=PROJECT)['count']==2


def test_native_reader_keeps_old_schema_unknown_without_migration(tmp_path):
    api=native()
    with closing(Store(tmp_path/'old.db')) as store:
        store.conn.execute('DROP TABLE native_load_reports')
        store.conn.execute('DELETE FROM schema_migrations WHERE name=?',(api.MIGRATION,));store.commit()
    with closing(Store(tmp_path/'old.db',read_only=True)) as store:
        assert api.native_history(store,project_key=PROJECT)['count'] is None
        assert not store.query_one('SELECT name FROM schema_migrations WHERE name=?',(api.MIGRATION,))


@pytest.mark.parametrize('kind,extra',[
    ('SessionStart',{'source':'resume'}),('SessionStart',{'source':'fork'}),
    ('PostCompact',{'trigger':'auto','compact_summary':'do not retain'}),
    ('CwdChanged',{'old_cwd':'/invented/old','new_cwd':'/invented/new'}),
    ('SessionEnd',{'reason':'prompt_input_exit','last_assistant_message':'do not retain'})])
def test_native_boundaries_keep_unknown_time_and_drop_noncontract_fields(env,kind,extra):
    got=native().record_native_event(env.store,env.cfg,event(env,kind,**extra))
    assert got['event_name']==kind and got['occurred_at'] is None
    assert 'file_path' not in got['payload'] and 'do not retain' not in str(got)


@pytest.mark.parametrize('change',[
    {'hook_event_name':['InstructionsLoaded']},{'cwd':'relative'},{'session_id':'unsafe\ntext'},
    {'file_path':['bad']},{'memory_type':'made-up'},{'load_reason':'made-up'},
    {'globs':'*.py'},{'globs':[None]},{'agent_id':None}])
def test_invalid_shapes_refuse_without_mutation(env,change):
    with pytest.raises(native().NativeLoadError):native().record_native_event(env.store,env.cfg,event(env,**change))
    assert env.store.query_one('SELECT COUNT(*) n FROM native_load_reports')['n']==0


@pytest.mark.parametrize('raw',[b'[]',b'{"a":1,"a":2}',b'{"data":NaN}',b'null',b'\xff',b'x'*65537])
def test_bounded_strict_input_never_echoes_content(raw):
    with pytest.raises(native().NativeLoadError) as caught:native().parse_input(raw)
    assert 'NaN' not in str(caught.value) and len(str(caught.value))<100


def test_denylist_refuses_before_git_and_body_access(env,monkeypatch):
    api=native();cfg=replace(env.cfg,denylist_substrings=('do-not-read',))
    def forbidden(*a,**kw):raise AssertionError('identity read before denylist')
    monkeypatch.setattr(api.project_identity,'resolve',forbidden)
    with pytest.raises(api.NativeLoadError,match='path policy'):
        api.record_native_event(env.store,cfg,event(env,file_path=str(env.tmp/'do-not-read/CLAUDE.md')))
    assert env.store.query_one('SELECT COUNT(*) n FROM native_load_reports')['n']==0


def test_copy_identity_cached_project_and_parent_agent_separation(env):
    api=native();other=make_repo(env.tmp/'second','git@github.com:example/alpha.git')
    first=api.record_native_event(env.store,env.cfg,event(env))
    second=api.record_native_event(env.store,env.cfg,event(env,cwd=other,agent_id='worker-1'))
    assert first['project_key']==second['project_key']==PROJECT
    assert first['working_copy_id']!=second['working_copy_id']
    assert first['logical_session_key']==second['logical_session_key']
    assert first['agent_id']=='' and second['agent_id']=='worker-1'
    assert api.native_history(env.store,project_key=PROJECT,working_copy_id=second['working_copy_id'])['records']==[second]


def test_transaction_failure_and_pending_caller_work(env,monkeypatch):
    api=native();original=env.store.insert
    def fail(table,row):
        original(table,row)
        if table=='native_load_reports':raise RuntimeError('synthetic write failure')
    monkeypatch.setattr(env.store,'insert',fail)
    with pytest.raises(RuntimeError,match='synthetic'):api.record_native_event(env.store,env.cfg,event(env))
    assert env.store.query_one('SELECT COUNT(*) n FROM native_load_reports')['n']==0
    env.store.conn.execute('BEGIN')
    with pytest.raises(api.NativeLoadError,match='idle'):api.record_native_event(env.store,env.cfg,event(env))
    assert env.store.conn.in_transaction;env.store.conn.rollback()


def test_stable_pages_bound_every_selector_and_all_records_inspectable(env):
    api=native()
    for i in range(23):api.record_native_event(env.store,env.cfg,event(env),received_at='2026-01-01T00:00:00Z')
    first=api.native_history(env.store,project_key=PROJECT)
    second=api.native_history(env.store,project_key=PROJECT,cursor=first['next_cursor'])
    assert first['count']==second['count']==23 and len(first['records'])==20 and len(second['records'])==3
    assert len({r['id'] for r in first['records']+second['records']})==23 and second['next_cursor'] is None
    for change in ({'project_key':'another'},{'logical_session_key':'f'*64},{'working_copy_id':'f'*64}):
        with pytest.raises(api.NativeLoadRequestError):
            api.native_history(env.store,**({'project_key':PROJECT}|change),cursor=first['next_cursor'])


@pytest.mark.parametrize('column,value',[('record_json','[]'),('record_hash','wrong'),('event_name','SessionEnd')])
def test_corrupt_retained_record_fails_with_owner(env,column,value):
    api=native();row=api.record_native_event(env.store,env.cfg,event(env))
    env.store.update('native_load_reports','id',row['id'],{column:value});env.store.commit()
    with pytest.raises(api.NativeLoadError,match=row['id']):api.native_history(env.store,project_key=PROJECT)


def test_empty_reports_are_not_zero_in_force_instructions(env):
    got=native().native_history(env.store,project_key=PROJECT)
    assert got['count']==0 and got['reason']=='no_retained_reports' and got['in_force_instructions'] is None


def config_file(env):
    path=env.tmp/"config with spaces 'literal'.toml"
    path.write_text('state_dir = '+json.dumps(str(env.store.db_path.parent))+'\ndenylist_substrings = []\n')
    return path


def test_generated_hook_command_runs_from_unrelated_cwd_and_emits_no_context(env):
    api=native();config=config_file(env);fragment=api.hook_settings(config_path=config,python_path=sys.executable)
    assert set(fragment['hooks'])==set(api.EVENTS)
    # Execute the exact quoted string, including a quote and spaces in config path.
    command=fragment['hooks']['InstructionsLoaded'][0]['hooks'][0]['command']
    sent=event(env,content='not retained');result=subprocess.run(command,shell=True,cwd=env.tmp,input=json.dumps(sent),text=True,capture_output=True)
    assert result.returncode==0,result.stderr
    assert result.stdout==result.stderr==''
    got=api.native_history(env.store,project_key=PROJECT)
    assert got['count']==1 and got['records'][0]['payload']==api.normalize_event(sent)
    # Re-render is read-only and changes neither an existing settings file nor DB.
    snapshot=list(env.store.conn.iterdump())
    result=subprocess.run([sys.executable,'-m','self_improve.cli','--config',str(config),'session-hook-settings'],cwd=env.tmp,capture_output=True,text=True)
    assert result.returncode==0 and json.loads(result.stdout)==fragment
    assert list(env.store.conn.iterdump())==snapshot


def test_cli_missing_config_and_schema_refuse_without_creating_state(env):
    config=config_file(env);api=native()
    result=subprocess.run([sys.executable,'-m','self_improve.cli','record-session-event'],cwd=env.tmp,input='{}',text=True,capture_output=True)
    assert result.returncode==1 and not result.stdout and 'explicit existing' in result.stderr
    env.store.conn.execute('DELETE FROM schema_migrations WHERE name=?',(api.MIGRATION,));env.store.commit()
    result=subprocess.run([sys.executable,'-m','self_improve.cli','--config',str(config),'record-session-event'],cwd=env.tmp,input=json.dumps(event(env)),text=True,capture_output=True)
    assert result.returncode==1 and not result.stdout and 'explicit migration' in result.stderr
    assert not env.store.query_one('SELECT name FROM schema_migrations WHERE name=?',(api.MIGRATION,))


def test_selected_store_api_is_read_only_and_reports_corruption(env,tmp_path):
    pytest.importorskip('fastapi');from fastapi.testclient import TestClient
    from self_improve.dashboard.app import create_app
    api=native();receipt=api.record_native_event(env.store,env.cfg,event(env))
    selected=tmp_path/'selected.db'
    with closing(Store(selected)) as other:env.store.conn.backup(other.conn)
    with TestClient(create_app(env.cfg,db_path=selected)) as client:
        before=selected.read_bytes()
        response=client.get('/api/project-native-events',params={'project_key':PROJECT})
        assert response.status_code==200 and response.json()['records']==[receipt]
        assert client.get('/api/project-native-events',params={'project_key':PROJECT,'cursor':'bad'}).status_code==400
        assert client.post('/api/project-native-events',json=event(env)).status_code==405
        assert selected.read_bytes()==before
    with closing(Store(selected)) as other:
        other.update('native_load_reports','id',receipt['id'],{'record_hash':'changed'});other.commit()
    with TestClient(create_app(env.cfg,db_path=selected)) as client:
        assert client.get('/api/project-native-events',params={'project_key':PROJECT}).status_code==409
    assert api.native_history(env.store,project_key=PROJECT)['records']==[receipt]


def test_concurrent_transport_retry_publishes_once(env):
    from concurrent.futures import ThreadPoolExecutor
    api=native();rid=str(uuid4());payload=event(env)
    def send(_):
        with closing(Store(env.store.db_path,migrate=False)) as store:
            return api.record_native_event(store,env.cfg,payload,receipt_id=rid)
    with ThreadPoolExecutor(max_workers=3) as pool:rows=list(pool.map(send,range(3)))
    assert all(row==rows[0] for row in rows)
    assert api.native_history(env.store,project_key=PROJECT)['count']==1


def test_reader_never_opens_reported_paths_or_resolves_identity(env,monkeypatch):
    api=native();loaded=Path(env.repo)/'CLAUDE.md';loaded.write_text('invented body')
    row=api.record_native_event(env.store,env.cfg,event(env));loaded.unlink()
    def forbidden(*a,**k):raise AssertionError('reader opened reported resources')
    monkeypatch.setattr(api.project_identity,'resolve',forbidden)
    monkeypatch.setattr(Path,'read_text',forbidden)
    assert api.native_history(env.store,project_key=PROJECT)['records']==[row]


def test_native_archive_survives_rebuild_without_projection_foreign_keys(env):
    from self_improve.rebuild import rebuild_state
    api=native();row=api.record_native_event(env.store,env.cfg,event(env))
    destination=env.tmp/'new-private-backup'
    rebuild_state(env.store,export_path=destination,dry_run=False)
    assert api.native_history(env.store,project_key=PROJECT)['records']==[row]
    files=list(destination.rglob('*.json'))
    assert any(row['id'] in path.read_text() for path in files)


@pytest.mark.parametrize('change',[{'memory_type':[]},{'load_reason':{}},{'hook_event_name':'SessionStart','source':[]}, {'hook_event_name':'PostCompact','trigger':{}}])
def test_nested_invalid_enums_raise_declared_error(env,change):
    with pytest.raises(native().NativeLoadError):native().record_native_event(env.store,env.cfg,event(env,**change))


def test_deep_native_json_is_a_declared_input_error():
    raw=b'{"ignored":'+b'['*12000+b'0'+b']'*12000+b'}'
    with pytest.raises(native().NativeLoadError):native().parse_input(raw)


def test_direct_receiver_does_not_bypass_raw_input_bound(env):
    with pytest.raises(native().NativeLoadError,match='65536'):
        native().record_native_event(env.store,env.cfg,event(env,content='x'*70000))
    assert env.store.query_one('SELECT COUNT(*) n FROM native_load_reports')['n']==0


def test_retained_identity_shape_cannot_hide_behind_a_matching_hash(env):
    api=native();row=api.record_native_event(env.store,env.cfg,event(env))
    row['identity_method']=['remote_url']
    env.store.update('native_load_reports','id',row['id'],{'record_json':so.canonical_json(row),'record_hash':so.content_id(row)});env.store.commit()
    with pytest.raises(api.NativeLoadError,match=row['id']):api.native_history(env.store,project_key=PROJECT)


def test_native_cwd_preserves_symlink_parent_semantics(env):
    api=native();second=Path(make_repo(env.tmp/'other','https://github.com/example/beta.git'))
    (second/'nested').mkdir();link=Path(env.repo)/'linked';link.symlink_to(second/'nested',target_is_directory=True)
    reported=str(link)+'/..'
    got=api.record_native_event(env.store,env.cfg,event(env,cwd=reported))
    assert got['project_key']=='remote:github.com/example/beta'
    assert got['working_copy_path']==str(second.resolve())
    assert got['payload']['cwd']==reported


def test_receiver_reuses_retained_repository_identity_without_network(env,monkeypatch):
    api=native()
    env.store.insert('project_identity_cache',{'remote_norm':'github.com/example/alpha','project_key':'github:123',
        'display':'example/alpha','method':'gh_repo_id','resolved_at':'2026-01-01T00:00:00Z'});env.store.commit()
    def forbidden(*a,**kw):raise AssertionError('network resolution')
    monkeypatch.setattr(api.project_identity,'_default_gh',forbidden)
    got=api.record_native_event(env.store,env.cfg,event(env))
    assert got['project_key']=='github:123' and got['identity_method']=='gh_repo_id'
