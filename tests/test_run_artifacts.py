"""Run inspection uses temporary bundles and actual journal/retention producers."""
from contextlib import closing
from dataclasses import replace
import json
import os
from pathlib import Path

import pytest

from self_improve.config import Config
from self_improve.dashboard import run_data, run_artifacts
from self_improve.dashboard.app import create_app
from self_improve.llm import LLMRunner
from self_improve.store import Store
from tests.test_model_jobs import env, cfg, store, request


@pytest.fixture
def bundle(tmp_path):
    root=tmp_path/'selected';root.mkdir()
    with closing(Store(root/'state.db')) as store:
        for rid in ('one','two'):
            store.insert('runs',{'id':rid,'started':'2030-01-02T02:00:00Z','stats_json':'{}','report_path':str(tmp_path/'original'/'private-report.md')})
            store.insert('llm_calls',{'id':rid+'-call','run_id':rid,'stage':'grade','outcome':'ok','created_at':'2030-01-02T02:01:00Z'})
        store.commit()
        raw=root/'runs'/'one'/'raw'
        llm=LLMRunner(Config(state_dir=str(root)),store,'one',raw)
        llm._retain('one-call','pick1.json',b'{"fixture":true}')
        llm._retain_attempt('one-call',1,'codex',b'<script>fixture</script>',b'')
        llm._retain('two-call','pick1.json',b'wrong run')
        llm._retain('not-linked','pick1.json',b'no source')
        yield store,raw,tmp_path


def test_local_producer_files_and_report_are_inspectable_without_state_changes(bundle):
    store,raw,_=bundle
    (raw.parent/'report.md').write_text('# Synthetic report\n')
    before=list(store.conn.iterdump())
    page=run_artifacts.catalog(store,'one',limit=2)
    last=run_artifacts.catalog(store,'one',cursor=page['next_cursor'],limit=2)
    assert [r['key'] for r in page['records']+last['records']]==['report','one-call.a1.codex.stderr','one-call.a1.codex.stdout','one-call.pick1.json']
    assert last['next_cursor'] is None and last['count']==4
    empty=run_artifacts.read(store,'one','one-call.a1.codex.stderr')
    assert empty['bytes']==0 and empty['text']=='' and empty['omitted_bytes']==0
    assert run_artifacts.read(store,'one','report')['text']=='# Synthetic report\n'
    with pytest.raises(run_data.RunDataError,match='cursor'):
        run_artifacts.catalog(store,'two',cursor=page['next_cursor'])
    with pytest.raises(run_artifacts.ArtifactError,match='no retained call link'):
        run_artifacts.read(store,'one','two-call.pick1.json')
    assert list(store.conn.iterdump())==before


def test_api_copy_never_follows_original_report_or_configuration(bundle):
    from fastapi.testclient import TestClient
    store,raw,tmp=bundle
    original=tmp/'original';original.mkdir();(original/'private-report.md').write_text('must not read')
    (raw.parent/'report.md').write_text('original bundle report')
    copied=tmp/'copy';copied.mkdir()
    with closing(Store(copied/'state.db')) as target:store.conn.backup(target.conn)
    before=(copied/'state.db').read_bytes()
    with TestClient(create_app(Config(state_dir=str(store.db_path.parent)),db_path=copied/'state.db')) as client:
        page=client.get('/api/runs/one/artifacts').json()
        assert page['records'][0]['state']=='ArtifactMissing'
        assert page['raw_state']=='ArtifactMissing' and page['count']==1
        assert client.get('/api/runs/one/artifacts/report').status_code==404
        # Relocated bundle supplies its own file; metadata remains the original path.
        folder=copied/'runs'/'one';folder.mkdir(parents=True);(folder/'report.md').write_text('copied report')
        shown=client.get('/api/runs/one/artifacts/report').json()
        assert shown['text']=='copied report'
        response=client.get('/api/runs/one/artifacts/report/download',params={'version':shown['version']})
        assert response.content==b'copied report' and response.headers['x-content-type-options']=='nosniff'
        assert response.headers['content-disposition']=='attachment; filename="report.md"'
    assert (copied/'state.db').read_bytes()==before


def test_large_invalid_text_download_is_complete_and_version_changes_fail(bundle):
    from fastapi.testclient import TestClient
    store,raw,_=bundle;file=raw/'one-call.a1.codex.stdout'
    content=b'fixture\xff\x00'+b'x'*(run_artifacts.PREVIEW_BYTES+300)
    file.write_bytes(content)
    with TestClient(create_app(Config(state_dir=str(store.db_path.parent)))) as client:
        shown=client.get('/api/runs/one/artifacts/'+file.name).json()
        assert shown['preview_bytes']==65536 and shown['omitted_bytes']==len(content)-65536
        assert 'replacement' in shown['encoding'] and '\ufffd' in shown['text']
        result=client.get('/api/runs/one/artifacts/'+file.name+'/download',params={'version':shown['version']})
        assert result.content==content and int(result.headers['content-length'])==len(content)
        file.write_bytes(b'changed')
        stale=client.get('/api/runs/one/artifacts/'+file.name+'/download',params={'version':shown['version']})
        assert stale.status_code==409 and stale.json()['error']=='ArtifactChanged'


@pytest.mark.parametrize('attack',['file_symlink','parent_symlink','hardlink','fifo'])
def test_api_rejects_unsafe_file_topology_without_opening_private_content(bundle,attack):
    from fastapi.testclient import TestClient
    store,raw,tmp=bundle;file=raw.parent/'report.md';secret=tmp/'secret';secret.mkdir();(secret/'report.md').write_text('unread private content')
    if attack=='file_symlink':file.symlink_to(secret/'report.md')
    elif attack=='hardlink':os.link(secret/'report.md',file)
    elif attack=='fifo':os.mkfifo(file)
    else:
        raw.rename(tmp/'saved-raw');raw.parent.rmdir();raw.parent.symlink_to(secret,target_is_directory=True)
    with TestClient(create_app(Config(state_dir=str(store.db_path.parent)))) as client:
        response=client.get('/api/runs/one/artifacts/report')
        assert response.status_code==409 and response.json()['error']=='UnsafeArtifact'
        assert client.get('/api/runs/one/artifacts/report/download?version=anything').status_code==409
    assert (secret/'report.md').read_text()=='unread private content'


@pytest.mark.parametrize('key',['../report.md','one-call.pick0.json','one-call.a1.codex.html','unlinked.pick1.json'])
def test_unlinked_or_traversing_keys_have_no_file_authority(bundle,key):
    store,_,_=bundle
    with pytest.raises(run_artifacts.ArtifactError):run_artifacts.read(store,'one',key)


def test_pipeline_budgets_missing_and_zero_do_not_borrow_current_settings(bundle):
    store,_,_=bundle
    assert run_data.detail(store,'one')['inspection']['pipeline_pools']==[]
    stats={'llm':{'calls_made':{'cheap':0},'refused':{'gate':2},'policy_waits':0},'budget_limits':{'cheap':5},
           'wall_clock':{'wall_seconds':30.0,'model_seconds':10.0,'unaccounted_seconds':20.0,'largest_gap_seconds':12.0}}
    store.update('runs','id','one',{'stats_json':json.dumps(stats)});store.commit()
    shown=run_data.detail(store,'one')['inspection']
    assert shown['pipeline_pools']==[{'pool':'cheap','limit':5,'used':0,'refused':None},{'pool':'gate','limit':None,'used':None,'refused':2}]
    assert shown['policy_waits']==0 and shown['wall_clock']['unaccounted_seconds']==20.0
    for bad in ({'calls_made':[]},{'refused':{'gate':True}},{'policy_waits':-1}):
        store.update('runs','id','one',{'stats_json':json.dumps({'llm':bad})});store.commit()
        with pytest.raises(run_data.RunDataError,match='run one'):run_data.detail(store,'one')


def test_actual_interrupted_job_reservation_attempt_and_raw_call_stay_visible(env,monkeypatch):
    from self_improve import job_worker
    from self_improve.commands import submit_command
    cfg,store,proposal,calls=env
    submitted=submit_command(store,cfg,request(env))
    def stop(event):
        if event=='call_reserved':raise KeyboardInterrupt()
    monkeypatch.setattr(job_worker,'_checkpoint',stop)
    with pytest.raises(KeyboardInterrupt):job_worker.run_once(store,cfg)
    rid=store.query_one('SELECT run_id FROM model_jobs WHERE command_id=?',(submitted['id'],))['run_id']
    shown=run_data.detail(store,rid)['inspection'];job=shown['jobs'][0]
    assert job['id']==submitted['id'] and job['budget']['consumed']['gate']==1
    assert job['completed_calls']==0 and job['unresolved_calls']==1 and shown['pipeline_pools']==[]
    attempts=run_data.records(store,rid,kind='attempts')
    assert attempts['count']==1 and attempts['records'][0]['state']!='completed'
    assert run_data.records(store,rid,kind='evaluations')['records']==[]
    cid=store.query_one('SELECT id FROM job_calls')['id']
    raw=cfg.state_path('runs',rid,'raw');raw.mkdir(parents=True,exist_ok=True)
    (raw/(cid+'.pick1.json')).write_text('invented partial call artifact')
    assert cid+'.pick1.json' in {r['key'] for r in run_artifacts.catalog(store,rid)['records']}
    monkeypatch.setattr(job_worker,'_checkpoint',lambda _:None)
    job_worker.run_once(store,cfg)
    assert run_data.detail(store,rid)['inspection']['jobs'][0]['budget']['consumed']['gate']==1
    assert calls==[] and Path(proposal['target_path']).read_text().startswith('#')
    store.conn.execute("UPDATE job_budgets SET maximum=maximum+1 WHERE command_id=?",(submitted['id'],));store.commit()
    from self_improve.commands import CommandError
    with pytest.raises(CommandError,match='reservation'):run_data.detail(store,rid)


def test_download_detects_changes_inside_the_read_before_yielding(bundle,monkeypatch):
    store,raw,_=bundle;file=raw.parent/'report.md';file.write_bytes(b'original')
    shown=run_artifacts.read(store,'one','report')
    chunks,_,close=run_artifacts.download(store,'one','report',shown['version'])
    original_read=os.read
    def changed(fd,n):
        file.write_bytes(b'MUTATED!')
        return original_read(fd,n)
    monkeypatch.setattr(run_artifacts.os,'read',changed)
    try:
        with pytest.raises(run_artifacts.ArtifactError,match='changed during download'):next(chunks)
    finally:close()


@pytest.mark.parametrize('phase',['http.response.start','http.response.body'])
def test_actual_download_response_closes_descriptors_on_disconnect(bundle,monkeypatch,phase):
    import asyncio
    from fastapi.testclient import TestClient
    from starlette.requests import ClientDisconnect
    store,raw,_=bundle;(raw.parent/'report.md').write_text('synthetic download')
    app=create_app(Config(state_dir=str(store.db_path.parent)))
    with TestClient(app) as client:
        version=client.get('/api/runs/one/artifacts/report').json()['version']
        endpoint=next(route.endpoint for route in app.routes if getattr(route,'path',None)=='/api/runs/{run_id}/artifacts/{key}/download')
        original_open=os.open;fds=[]
        def track(*args,**kwargs):
            fd=original_open(*args,**kwargs);fds.append(fd);return fd
        with monkeypatch.context() as patch:
            patch.setattr(run_artifacts.os,'open',track)
            response=client.portal.call(endpoint,'one','report',version)
        assert len(fds)==4
        async def receive():return {'type':'http.disconnect'}
        async def send(message):
            if message['type']==phase:raise OSError('fixture disconnect')
        with pytest.raises(ClientDisconnect):
            asyncio.run(response({'type':'http','asgi':{'spec_version':'2.4'}},receive,send))
        for fd in fds:
            with pytest.raises(OSError):os.fstat(fd)


@pytest.mark.parametrize('source',['attempt','job'])
def test_partial_call_cannot_contradict_existing_audit_owner(env,monkeypatch,source):
    from self_improve import eval_history,job_worker
    from self_improve.commands import submit_command
    cfg,store,proposal,calls=env
    store.insert('runs',{'id':'other','started':'2030-01-01T00:00:00Z'});store.commit()
    if source=='attempt':
        rid='selected';cid='conflicting-call'
        store.insert('runs',{'id':rid,'started':'2030-01-01T00:00:00Z'});store.commit()
        lesson=store.query_one('SELECT * FROM learnings WHERE id=?',(proposal['learning_id'],))
        recorder=eval_history.begin(store,cfg,lesson,proposal,run_id=rid)
        recorder.call_trace(scenario=0).start(store,cid,{'run_id':rid,'stage':'eval_gen'})
    else:
        submit_command(store,cfg,request(env))
        def stop(event):
            if event=='call_reserved':raise KeyboardInterrupt()
        monkeypatch.setattr(job_worker,'_checkpoint',stop)
        with pytest.raises(KeyboardInterrupt):job_worker.run_once(store,cfg)
        rid=store.query_one('SELECT run_id FROM model_jobs')['run_id'];cid=store.query_one('SELECT id FROM job_calls')['id']
    store.insert('llm_calls',{'id':cid,'run_id':'other','stage':'eval_gen','created_at':'2030-01-01T00:01:00Z'});store.commit()
    raw=cfg.state_path('runs',rid,'raw');raw.mkdir(parents=True,exist_ok=True)
    (raw/(cid+'.pick1.json')).write_text('conflicting evidence cannot authorize this read')
    with pytest.raises(run_data.RunDataError,match='different run'):run_artifacts.catalog(store,rid)
    with pytest.raises(run_data.RunDataError,match='different run'):run_artifacts.read(store,rid,cid+'.pick1.json')
    assert calls==[]
