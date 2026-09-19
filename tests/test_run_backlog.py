"""Selected-Store GET and Run backlog presentation, using invented history."""
from contextlib import closing
import json

from fastapi.testclient import TestClient
from self_improve.dashboard.app import create_app
from self_improve.store import Store
from self_improve import queue_history as q
from tests.test_queue_history import history, anchor, incident, SETTINGS
from tests.test_rule_availability import cfg
from tests.test_run_composition_v2 import detail, render
from tests.test_navigation import node


def test_api_uses_selected_copy_and_reports_missing_and_corrupt_history(history,cfg,tmp_path):
    store,clock=history;anchor(store,clock)
    copy=tmp_path/'copied.db'
    with closing(Store(copy)) as other:store.conn.backup(other.conn)
    before=copy.read_bytes()
    incident(store,'later-live');store.commit()
    with TestClient(create_app(cfg,db_path=copy)) as client:
        response=client.get('/api/runs/selected/backlog')
        assert response.status_code==200,response.text
        assert response.json()['queue_count']==10 and response.json()['profile']==q.PROFILE
        assert client.get('/api/runs/missing/backlog').status_code==404
        assert client.post('/api/runs/selected/backlog').status_code==405
    assert copy.read_bytes()==before
    with closing(Store(copy,migrate=False)) as writer:writer.conn.execute('DROP TRIGGER queue_observe_delete')
    with TestClient(create_app(cfg,db_path=copy)) as client:
        bad=client.get('/api/runs/selected/backlog')
        assert bad.status_code==400 and 'trigger' in bad.text


def test_render_preserves_units_windows_unknown_and_escaped_settings(history):
    store,clock=history;clock[0]='2030-01-03T00:00:00.000Z'
    for i in range(8):q.set_processed_status(store,f'baseline-{i}','mined',outcome='new')
    anchor(store,clock,settings={**SETTINGS,'project_filter':'<script> '+'long filter '*200+'END_FILTER'})
    data=q.read_run(store,'selected')
    markup=render(detail([]),backlog={'data':data})
    assert markup.index('run-usage')<markup.index('run-section-backlog')<markup.index('run-section-deliveries')
    assert '<strong>2 unmined</strong>' in markup
    assert 'only if these observed rates continue' in markup and '<strong>2 days</strong>' in markup
    assert 'cheap 80, strong 10, gate 78' in markup
    assert 'END_FILTER' in markup and '&lt;script&gt;' in markup and '<script>' not in markup
    assert '2030-01-02T00:00:00+00:00' in markup and '2030-01-09T00:00:00+00:00' in markup
    assert 'Call slots are not incident throughput' in markup
    old={**data,'snapshot':None,'queue_count':None,'rates':None,'scenario':None,'reasons':['No retained snapshot']}
    unknown=render(detail([]),backlog={'data':old})
    assert 'Historical queue size unavailable' in unknown and '0 unmined' not in unknown


def test_loading_failed_refresh_retry_and_foreign_late_response(history):
    store,clock=history;anchor(store,clock);payload=q.read_run(store,'selected')
    script=r'''
      import assert from 'node:assert/strict';
      const body={innerHTML:'',querySelectorAll:()=>[]};
      globalThis.document={activeElement:null,getElementById:id=>id==='run-detail'?body:null,querySelectorAll:()=>[]};
      const payload=PAYLOAD;
      const record=DETAIL;record.run.id='selected';
      const selected={id:'selected',kind:'run',data:record,pages:{}};
      app.state.runDetail=selected;
      let resolve;
      globalThis.fetch=()=>new Promise(r=>{resolve=r;});
      let pending=app.loadRunBacklog();
      assert.match(body.innerHTML,/Loading queue history/);
      resolve({ok:true,json:async()=>payload});await pending;
      assert.equal(selected.backlog.data.queue_count,10);
      document.activeElement=null;
      globalThis.fetch=async()=>{throw new Error('invented read failure');};
      await app.loadRunBacklog();
      assert.match(body.innerHTML,/previously read snapshot/);
      assert.match(body.innerHTML,/10 unmined/);
      assert.match(body.innerHTML,/>Retry<\/button>/);
      globalThis.fetch=async()=>({ok:true,json:async()=>payload});
      await app.loadRunBacklog();assert.equal(selected.backlog.error,'');
      globalThis.fetch=()=>new Promise(r=>{resolve=r;});
      pending=app.loadRunBacklog();
      const next={id:'other',kind:'run',data:{...record,run:{...record.run,id:'other'}},pages:{}};
      app.state.runDetail=next;
      const previous=body.innerHTML;
      resolve({ok:true,json:async()=>payload});await pending;
      assert.equal(body.innerHTML,previous);assert.equal(next.backlog,undefined);
      app.state.runDetail=selected;
      globalThis.fetch=async()=>({ok:true,json:async()=>({...payload,run_id:'foreign'})});
      await app.loadRunBacklog();assert.match(selected.backlog.error,/different run/);
      console.log(JSON.stringify('BACKLOG_SPA_OK'));
    '''.replace('PAYLOAD',json.dumps(payload)).replace('DETAIL',json.dumps(detail([])))
    assert node(script)=='BACKLOG_SPA_OK'
