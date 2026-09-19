"""Earlier reviewed writes require frozen identity, not nearby times/current rows."""
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
import json

import pytest

from self_improve import mining_history
from self_improve.dashboard import run_context, run_data
from self_improve.store import Store
from self_improve.worker import run_once
from tests.test_delivery_worker import env, approve, propose


def observe(store, learning_id, run_id='selected'):
    learning=store.query_one('SELECT * FROM learnings WHERE id=?',(learning_id,))
    return mining_history.append(store,learning,before=None,kind='duplicate',incidents=[],
                                 provenance={'run_id':run_id})


def seed(store):
    for run_id in ('selected','neighbor','unknown'):
        store.insert('runs',{'id':run_id,'started':'2030-01-03T00:00:00Z','status':'ok',
                            'stats_json':json.dumps({'run_id':run_id,'apply':{'attempted':0,'applied':0,'held':0,'failed':0,'operation_ids':[]}})})
    store.commit()


def deliver(store,cfg,target,*,when='2030-01-02T00:00:00Z',related=True):
    target.write_text('before\n')
    proposal=propose(store,target)
    command=approve(store,cfg,proposal)
    with patch('self_improve.apply.utc_now_iso',return_value=when):
        assert run_once(store,cfg)['state']=='completed'
    if related:observe(store,proposal['learning_id'])
    store.commit()
    return proposal,command


def test_frozen_combined_target_matches_one_member_and_remains_after_current_changes(env,monkeypatch):
    cfg,store,target=env;seed(store)
    one,two=propose(store,target),propose(store,target)
    command=approve(store,cfg,one,two)
    with patch('self_improve.apply.utc_now_iso',return_value='2030-01-02T00:00:00Z'):run_once(store,cfg)
    observation=observe(store,one['learning_id'])
    observe(store,two['learning_id'],'neighbor')
    for proposal in (one,two):
        store.update('learnings','id',proposal['learning_id'],{'title':'CURRENT CHANGED'})
        store.update('proposals','id',proposal['id'],{'status':'rolled_back','learning_id':two['learning_id'],'run_id':'neighbor'})
    store.commit();target.unlink()
    before=list(store.conn.iterdump())
    with patch.object(Path,'read_text',side_effect=AssertionError('No file reads')):
        page=run_context.related_deliveries(store,'selected')
    assert page['count']==1 and page['run_id']=='selected'
    row=page['records'][0]
    assert row['command_id']==command['id'] and len(row['members'])==2
    matching=[m for m in row['members'] if m['observation_ids']]
    assert len(matching)==1 and matching[0]['observation_ids']==[observation['id']]
    assert matching[0]['snapshot']['learning']['id']==one['learning_id']
    assert matching[0]['snapshot']['learning']['title']!='CURRENT CHANGED'
    assert row['target']['diff_unified'] and row['snapshot_before'] and row['snapshot_after']
    assert list(store.conn.iterdump())==before
    assert run_data.records(store,'selected',kind='deliveries')['count']==0


@pytest.mark.parametrize('when,related,expected',[
    ('2030-01-03T00:00:00Z',True,0),('2030-01-03T01:00:00Z',True,0),
    ('2030-01-03T01:00:00+02:00',True,1),('2030-01-02T23:59:59Z',False,0)])
def test_time_only_filters_an_exact_relation(env,when,related,expected):
    cfg,store,target=env;seed(store)
    p,_=deliver(store,cfg,target,when=when,related=related)
    if not related:
        other=propose(store,target);observe(store,other['learning_id']);store.commit()
    page=run_context.related_deliveries(store,'selected')
    assert page['count']==expected


@pytest.mark.parametrize('missing',['0021_mining_history','0010_dashboard_commands','start','observations'])
def test_missing_is_not_zero(env,missing):
    cfg,store,target=env;seed(store)
    deliver(store,cfg,target)
    if missing=='start':store.update('runs','id','selected',{'started':''})
    elif missing=='observations':store.conn.execute("DELETE FROM mining_history WHERE run_id='selected'")
    else:store.conn.execute('DELETE FROM schema_migrations WHERE name=?',(missing,))
    result=run_context.related_deliveries(store,'selected')
    assert result['count'] is None and result['reason'] and result['records']==[]


@pytest.mark.parametrize('damage',['mining','member','delivery','completion','event','duplicate_event','time','actor'])
def test_corrupt_retained_relation_fails_visibly(env,damage):
    cfg,store,target=env;seed(store)
    p,c=deliver(store,cfg,target)
    t=store.query_one('SELECT * FROM command_targets WHERE command_id=?',(c['id'],))
    cp=json.loads(t['checkpoint_json'])
    if damage=='mining':store.conn.execute("UPDATE mining_history SET record_hash='changed'")
    elif damage=='member':store.conn.execute("UPDATE proposal_revisions SET snapshot_json='{}'")
    elif damage=='delivery':cp['delivery']['after_content']='different'
    elif damage=='completion':cp['result']['snapshot_after']='different'
    elif damage=='event':store.conn.execute("DELETE FROM proposal_events WHERE event='applied'")
    elif damage=='duplicate_event':
        event=store.query_one("SELECT * FROM proposal_events WHERE event='applied'");event['id']='duplicate';store.insert('proposal_events',event)
    elif damage=='time':cp['result']['completed_at']='2030-01-02'
    elif damage=='actor':store.update('commands','id',c['id'],{'actor':'unexpected'})
    store.update('command_targets','id',t['id'],{'checkpoint_json':json.dumps(cp)})
    with pytest.raises(run_data.RunDataError):run_context.related_deliveries(store,'selected')


def test_pagination_normalizes_utc_and_rejects_foreign_or_stale_pages(env,tmp_path):
    cfg,store,target=env;seed(store)
    _,first=deliver(store,cfg,target,when='2030-01-02T02:00:00+02:00')
    _,second=deliver(store,cfg,tmp_path/'second.md',when='2030-01-02T01:00:00Z')
    page=run_context.related_deliveries(store,'selected',limit=1)
    last=run_context.related_deliveries(store,'selected',limit=1,cursor=page['next_cursor'])
    assert [r['command_id'] for r in page['records']+last['records']]==[second['id'],first['id']]
    assert last['next_cursor'] is None
    for run,cursor in [('neighbor',page['next_cursor']),('selected','invalid')]:
        with pytest.raises(run_data.RunDataError,match='cursor'):run_context.related_deliveries(store,run,cursor=cursor)
    observe(store,store.query_one('SELECT learning_id FROM mining_history')['learning_id']);store.commit()
    with pytest.raises(run_data.RunDataError,match='cursor'):run_context.related_deliveries(store,'selected',cursor=page['next_cursor'])


def test_partial_command_and_caller_transaction_are_preserved(env,tmp_path):
    cfg,store,target=env;seed(store)
    other=tmp_path/'other.md';other.write_text('before\n')
    one,two=propose(store,target),propose(store,other)
    command=approve(store,cfg,one,two);other.write_text('intervening human change\n')
    with patch('self_improve.apply.utc_now_iso',return_value='2030-01-02T00:00:00Z'):
        assert run_once(store,cfg)['state']=='blocked'
    observe(store,one['learning_id']);store.commit()
    store.conn.execute('BEGIN')
    store.update('runs','id','selected',{'status':'sentinel'})
    assert run_context.related_deliveries(store,'selected')['records'][0]['command_state']=='blocked'
    assert store.conn.in_transaction
    store.conn.rollback()
    assert store.query_one("SELECT status FROM runs WHERE id='selected'")['status']=='ok'


@pytest.mark.parametrize('limit',[0,101,True,'2',1.5])
def test_invalid_limits_and_missing_runs(env,limit):
    cfg,store,_=env;seed(store)
    with pytest.raises(run_data.RunDataError):run_context.related_deliveries(store,'selected',limit=limit)
    with pytest.raises(run_data.RunNotFound):run_context.related_deliveries(store,'absent')


def test_api_selected_store_and_read_only(env,tmp_path):
    from fastapi.testclient import TestClient
    from self_improve.dashboard.app import create_app
    cfg,store,target=env;seed(store);deliver(store,cfg,target)
    copy=tmp_path/'copy.db'
    with closing(Store(copy)) as copied:store.conn.backup(copied.conn)
    before=copy.read_bytes();target_before=target.read_bytes()
    store.conn.execute('DELETE FROM mining_history');store.commit()
    with TestClient(create_app(cfg,db_path=copy)) as client:
        url='/api/runs/selected/related-deliveries'
        result=client.get(url);assert result.status_code==200,result.text
        assert result.json()['count']==1 and result.json()['profile']=='run-related-reviewed/1'
        assert client.get(url+'?cursor=invalid').status_code==400
        assert client.get('/api/runs/missing/related-deliveries').status_code==404
        assert client.post(url).status_code==405
    assert copy.read_bytes()==before and target.read_bytes()==target_before


def test_spa_full_historical_evidence_escapes_content_and_preserves_attribution(env):
    from tests.test_navigation import node
    from tests.test_run_composition_v2 import render,detail
    cfg,store,target=env;seed(store);proposal,_=deliver(store,cfg,target)
    page=run_context.related_deliveries(store,'selected')
    record=page['records'][0];record['members'][0]['snapshot']['learning']['title']='<script>invented title</script>'
    record['target']['diff_unified']='--- a\n+++ b\n@@ -1 +1 @@\n-old\n+'+'long invented contribution '*200+'END_FULL_DIFF\n'
    markup=render(detail([]),pages={'related_deliveries':{**page,'loaded':True}})
    assert markup.index('run-section-deliveries')<markup.index('run-section-related_deliveries')
    assert 'END_FULL_DIFF' in markup and '&lt;script&gt;invented title&lt;/script&gt;' in markup
    assert '<script>invented title' not in markup
    assert '#/review/command/'+record['command_id'] in markup
    assert '#/review/rollback/'+proposal['id'] in markup
    assert 'Same learning observed by this run' in markup
    assert "separate from this run's automatic edits" in markup
    assert 'does not establish receipt, violation or benefit' in markup
    assert 'run-refresh-related-deliveries' in markup


def test_spa_related_loading_failure_refresh_and_late_response_are_isolated():
    from tests.test_navigation import node
    data=node('''
      const pending=[],urls=[];
      const data={run:{id:"one",started:"2030-01-03T00:00:00Z",status:"ok"},stats:{},night_runs:[],stages:[],calls:{}};
      app.state.runDetail={id:"one",data,pages:{}};
      const body={innerHTML:"",querySelectorAll:()=>[]};
      globalThis.document={activeElement:null,getElementById:()=>body};
      globalThis.fetch=async url=>{urls.push(url);return await new Promise((resolve,reject)=>pending.push({resolve:data=>resolve({ok:true,json:async()=>data}),reject}));};
      const first=app.loadRunRecords("related_deliveries");
      const loading=app.renderRunRecords("related_deliveries",app.state.runDetail);
      pending[0].reject(new Error("invented read failed"));await first;
      const failed=app.renderRunRecords("related_deliveries",app.state.runDetail);
      const second=app.loadRunRecords("related_deliveries");
      pending[1].resolve({run_id:"one",kind:"related_deliveries",records:[],count:0,reason:"",next_cursor:"cursor"});await second;
      const old=app.state.runDetail;
      const later=app.loadRunRecords("related_deliveries",true);
      app.state.runDetail={id:"two",data:{...data,run:{id:"two"}},pages:{}};
      pending[2].resolve({run_id:"one",kind:"related_deliveries",records:[{id:"late"}],count:1});await later;
      console.log(JSON.stringify({urls,loading,failed,old:old.pages.related_deliveries.records,current:app.state.runDetail.pages}));
    ''')
    assert 'Loading records' in data['loading'] and 'Could not read' in data['failed']
    assert data['urls'][0]=='/api/runs/one/related-deliveries?limit=20'
    assert data['urls'][2].endswith('&cursor=cursor')
    assert data['old']==[] and data['current']=={}


def test_real_rollback_does_not_erase_completed_delivery(env):
    from self_improve.apply import rollback
    cfg,store,target=env;seed(store)
    p,c=deliver(store,cfg,target)
    before=run_context.related_deliveries(store,'selected')['records']
    result=rollback(store,cfg,p['id'])
    assert result.get('outcome')=='rolled_back',result
    assert target.read_text()=='before\n'
    assert run_context.related_deliveries(store,'selected')['records']==before


def test_every_matched_observation_keeps_one_combined_target(env):
    cfg,store,target=env;seed(store)
    one,two=propose(store,target),propose(store,target)
    approve(store,cfg,one,two)
    with patch('self_improve.apply.utc_now_iso',return_value='2030-01-02T00:00:00Z'):run_once(store,cfg)
    observe(store,one['learning_id']);observe(store,one['learning_id']);observe(store,two['learning_id']);store.commit()
    page=run_context.related_deliveries(store,'selected')
    assert page['count']==1
    assert sorted(len(m['observation_ids']) for m in page['records'][0]['members'])==[1,2]


def test_stale_page_failure_preserves_current_page_and_refresh_resets_cursor():
    from tests.test_navigation import node
    result=node('''
      const data={run:{id:"one",started:"2030-01-03T00:00:00Z",status:"ok"},stats:{},night_runs:[],stages:[],calls:{}};
      const body={innerHTML:"",querySelectorAll:()=>[]};globalThis.document={activeElement:null,getElementById:()=>body};
      app.state.runDetail={id:"one",data,pages:{related_deliveries:{records:[],loaded:true,count:0,reason:"retained sentinel",next_cursor:"stale",error:""}}};
      const urls=[];globalThis.fetch=async url=>{urls.push(url);return {ok:false,status:400,json:async()=>({detail:"Invalid or stale related delivery cursor. Refresh earlier deliveries."})};};
      await app.loadRunRecords("related_deliveries",true);
      const failed=app.renderRunRecords("related_deliveries",app.state.runDetail);
      globalThis.fetch=async url=>{urls.push(url);return {ok:true,json:async()=>({run_id:"one",kind:"related_deliveries",records:[],count:0,reason:"",next_cursor:null})};};
      await app.loadRunRecords("related_deliveries");
      console.log(JSON.stringify({urls,failed,page:app.state.runDetail.pages.related_deliveries}));
    ''')
    assert '&cursor=stale' in result['urls'][0] and 'cursor=' not in result['urls'][1]
    assert 'retained sentinel' in result['failed'] and 'Refresh earlier deliveries' in result['failed']
    assert not result['page']['error'] and result['page']['next_cursor'] is None


def test_failed_refresh_retries_refresh_instead_of_appending_duplicate_targets():
    from tests.test_navigation import node
    result=node('''
      const data={run:{id:"one",started:"2030-01-03T00:00:00Z",status:"ok"},stats:{},night_runs:[],stages:[],calls:{}};
      const body={innerHTML:"",querySelectorAll:()=>[]};globalThis.document={activeElement:null,getElementById:()=>body};
      const record={id:"target-1",command_id:"command",command_state:"completed",target:{destination:{target_path:"invented.md"},diff_unified:""},members:[]};
      const page={records:[record],loaded:true,count:1,reason:"",next_cursor:null,error:""};
      app.state.runDetail={id:"one",data,pages:{related_deliveries:page}};
      globalThis.fetch=async()=>({ok:false,status:503,json:async()=>({detail:"Invented refresh failure"})});
      await app.loadRunRecords("related_deliveries");
      const markup=app.renderRunRecords("related_deliveries",app.state.runDetail);
      const retryOlder=markup.match(/id="run-older-related_deliveries"[^>]*data-run-older="(true|false)"/)[1]==="true";
      globalThis.fetch=async()=>({ok:true,json:async()=>({run_id:"one",kind:"related_deliveries",records:[record],count:1,next_cursor:null})});
      await app.loadRunRecords("related_deliveries",retryOlder);
      console.log(JSON.stringify({retryOlder,ids:page.records.map(r=>r.id)}));
    ''')
    assert result=={'retryOlder':False,'ids':['target-1']}
