"""Revised Overview uses exact retained records and compatible measurement units."""
from contextlib import closing

import pytest

from self_improve.config import Config
from self_improve.store import Store
from self_improve.dashboard import queries
from tests.test_dashboard_queries import _run, _learning, _proposal, _spread_incidents

NOW='2030-02-08T12:00:00Z'


def snapshot(store):
    from self_improve.dashboard.overview_data import snapshot
    return snapshot(store, Config(), now_utc=NOW)


def test_overview_call_slots_are_not_an_incident_net_rate(tmp_path):
    with closing(Store(tmp_path/'state.db')) as store:
        _spread_incidents(store,n=140,days=14,first_day='2030-01-25')
        data=queries.backlog(store,Config())
        assert data['net_per_day'] is None
        assert data['capacity_unit']=='model_calls_per_run'
        assert 'success' in data['capacity_note']


def test_overview_exact_latest_run_and_complete_utc_throughput(tmp_path):
    with closing(Store(tmp_path/'state.db')) as store:
        for run_id,day,counts in [('a','01',(3,2,1)),('b','01',(2,2,0)),('old','31',(10,10,0)),('current','08',(90,90,0))]:
            month='01' if run_id=='old' else '02'
            _run(store,run_id=run_id,started=f'2030-{month}-{day}T02:00:00Z',finished=f'2030-{month}-{day}T03:00:00Z',stats={'mine':dict(zip(('attempted','succeeded','failed'),counts))})
        _run(store,run_id='missing',started='2030-02-02T02:00:00Z',status='interrupted')
        data=snapshot(store)
        assert data['latest_run']['run']['id']=='current'
        assert data['latest_run']['stages'][1]['payload']['succeeded']==90
        mining=data['mining']
        assert mining['window_start']=='2030-02-01' and mining['window_end_exclusive']=='2030-02-08'
        assert mining['succeeded']==4 and mining['attempted']==5 and mining['failed']==1
        assert mining['per_day']==pytest.approx(4/7)
        assert mining['per_recorded_run']==2 and mining['days_with_runs']==2
        assert mining['run_ids']==['a','b','missing'] and mining['missing_stage_run_ids']==['missing']
        assert mining['coverage_complete'] is False


def test_overview_empty_and_recorded_zero_are_distinct(tmp_path):
    with closing(Store(tmp_path/'state.db')) as store:
        data=snapshot(store)
        assert data['latest_run'] is None and data['mining']['per_day'] is None
        _run(store,run_id='zero',started='2030-02-07T02:00:00Z',stats={'mine':{'attempted':0,'succeeded':0,'failed':0}})
        data=snapshot(store)
        assert data['mining']['per_day']==0 and data['mining']['recorded_runs']==1
        assert data['latest_run']['stages'][0]['recorded'] is False
        assert data['latest_run']['stages'][1]['recorded'] is True


def test_overview_preview_is_bounded_and_matches_the_review_resolver(tmp_path):
    with closing(Store(tmp_path/'state.db')) as store:
        for i in range(7):
            lid=_learning(store,learning_id=f'fixture-{i}')
            _proposal(store,learning_id=lid,status='pending')
            _proposal(store,learning_id=lid,status='gated_pass')
        before=list(store.conn.iterdump())
        data=snapshot(store)
        assert list(store.conn.iterdump())==before
        review=data['review']
        assert review['count']==queries.inbox(store,Config())['count']==14
        assert review['family_count']==7 and len(review['families'])==3 and review['omitted_families']==4
        assert all(f['size']==2 and f['proposal_id'] for f in review['families'])
        assert not any(x['enabled'] for x in data['policy']['classes'].values())


@pytest.mark.parametrize('counts',[{'attempted':True,'succeeded':1,'failed':0},{'attempted':1,'succeeded':2,'failed':0},{'attempted':-1,'succeeded':0,'failed':0}])
def test_overview_invalid_mining_outcomes_name_the_owner(tmp_path,counts):
    with closing(Store(tmp_path/'state.db')) as store:
        _run(store,run_id='bad-mining',started='2030-02-07T02:00:00Z',stats={'mine':counts})
        with pytest.raises(ValueError,match='bad-mining'):
            snapshot(store)


def test_overview_partial_mining_shape_cannot_hide_corrupt_counters(tmp_path):
    with closing(Store(tmp_path/'state.db')) as store:
        _run(store,run_id='bad-partial',started='2030-02-07T02:00:00Z',stats={'mine':{'attempted':-1}})
        with pytest.raises(ValueError,match='bad-partial'):
            snapshot(store)


def test_overview_stage_summary_omits_large_taxonomies(tmp_path):
    with closing(Store(tmp_path/'state.db')) as store:
        _run(store,run_id='many-causes',started='2030-02-07T02:00:00Z',stats={'mine':{'attempted':0,'succeeded':0,'failed':0,'taxonomy':{f'fixture-{i}':1 for i in range(500)},**{f'extra-{i}':1 for i in range(500)}}})
        stage=snapshot(store)['latest_run']['stages'][1]
        assert len(stage['payload'])<=4
        assert 'taxonomy' not in stage['cell']
        assert stage['omitted_numeric_fields']==499


def test_overview_failure_calls_keep_exact_run_ownership(tmp_path):
    from tests.test_dashboard_queries import _llm_call
    with closing(Store(tmp_path/'state.db')) as store:
        _run(store,run_id='latest',started='2030-02-07T02:00:00Z')
        _llm_call(store,stage='mine',outcome='timeout',run_id='latest')
        _llm_call(store,stage='mine',outcome='timeout')
        panel=queries.failure_panel(store)
        assert panel['open'][0]['recent']==1
        assert panel['open'][0]['total']==2
        assert panel['open'][0]['last_seen']['run_id']=='latest'
        assert panel['unlinked_failed_calls']==1


def test_overview_rendered_coverage_does_not_conflate_windows_or_missing_with_zero():
    from tests.test_navigation import node
    result=node('''
      const failure={regressed:[{class:'old',name:'Old',recent:0,total:100}],open:[{class:'new',name:'New',recent:2,total:40}]};
      console.log(JSON.stringify({
        foot:app.renderBacklogFoot({arrivals:{window_days:14,rate_days:13,window_end:'2030-02-07',excluded_partial_day:{day:'2030-02-07',incidents:200}}}),
        banner:app.renderBanner({days_stale:null}).html,
        queue:app.renderOverviewQueue({count:1,family_count:1,families:[{title:'x'.repeat(1100),size:1,proposal_id:'p'}]}),
        empty:app.renderFailures({}),failures:app.renderOverviewFailures(failure)}));
    ''')
    assert '13' in result['foot'] and '200' in result['foot'] and 'excluded' in result['foot']
    assert 'no rate on this page' not in result['banner']
    assert 'x'*161 not in result['queue'] and '…' in result['queue']
    assert 'measured zero' not in result['empty']
    current, history = result['failures'].split('<details', 1)
    assert 'Old' not in current and '>100<' not in current
    assert 'New' in current and '--pct: 100' in current
    assert 'Old' in history and '>100<' in history


def test_overview_unknown_outcomes_explain_the_record_and_keep_stage_links():
    from tests.test_navigation import node
    result=node('''
      console.log(JSON.stringify({known:app.stateTitle('unreadable'),
        unknown:app.stateTitle('future-outcome'),summary:app.renderOverviewRunSummary({
          run:{id:'recorded-run',started:'2030-02-07T02:00:00Z',status:'ok'},
          stages:[{name:'gate',recorded:true,payload:{attempted:8},cell:{state:'unreadable'}}]})}));
    ''')
    assert 'design system' not in result['known']
    assert 'unmapped' not in result['known']
    assert 'unreadable' in result['known']
    assert 'future-outcome' in result['unknown'] and 'not recognized' in result['unknown']
    assert 'unreadable' in result['summary']
    assert '#/overview/run/recorded-run/gate' in result['summary']
    assert 'attempted: 8' in result['summary']


def test_overview_unknown_legend_and_disclosure_explain_uncertainty():
    from tests.test_navigation import node
    result=node('''
      const grid={runs_total:3,nights_with_runs:1,stages:['gate'],
        columns:[{run_count:3,cells:{gate:{state:'unreadable',states:{unreadable:1,unknown_status:1,'future<outcome>':1}}}}],
        unreadable:[{run_id:'retained-run',stage:'gate'}],unknown_run_statuses:{'future<status>':1}};
      console.log(JSON.stringify({compact:app.renderGridLegend(grid,{compact:true}),
        definitions:app.renderGridLegend(grid),foot:app.renderGridFoot(grid)}));
    ''')
    for html in result.values():
        assert 'unmapped' not in html and 'schema' not in html and 'shape' not in html
        assert 'future<' not in html
    for key in ('compact','definitions'):
        for state in ('unreadable','unknown_status','future&lt;outcome&gt;'):
            assert f'data-state="{state}"' in result[key]
            assert state in result[key].split('</span>',1)[1]
    assert 'Outcome unknown' in result['compact']
    assert 'recorded stage outcomes could not be read' in result['definitions']
    assert 'not recognized' in result['definitions']
    assert '3 run(s) over 1 night(s)' in result['foot']
    assert '1 stage record(s) could not be read' in result['foot']
    assert 'unknown' in result['foot'] and 'never counted as success' in result['foot']
    assert 'future&lt;status&gt;' in result['foot']


def test_overview_delivery_poll_cannot_replace_post_decision_review():
    from tests.test_navigation import node
    result=node('''
      const element={innerHTML:'',textContent:'',setAttribute(){},removeAttribute(){},querySelectorAll(){return []},classList:{toggle(){},add(){},remove(){}}};
      globalThis.document={getElementById(){return element},querySelectorAll(){return []}};
      let release,started;const reached=new Promise(r=>started=r);
      globalThis.fetch=async url=>({ok:true,json:async()=>{
        if(url.startsWith('/api/commands'))return {commands:[{id:'c',action:'approve',state:'completed',created_at:'2030-01-01',targets:[]}],next_cursor:null};
        if(url==='/api/review-queue'){started();return new Promise(r=>release=r);}
        throw Error(url);
      }});
      app.state.delivery.loaded=true;app.state.delivery.items=[{id:'c',action:'approve',state:'queued',created_at:'2030-01-01',targets:[]}];
      const poll=app.loadDeliveryHistory();await reached;
      app.state.reviewMutationGeneration++;app.state.review={count:0,families:[]};app.state.decided={p:'rejected'};
      release({count:3,families:[]});await poll;
      console.log(JSON.stringify({count:app.state.review.count,decided:app.state.decided}));
    ''')
    assert result=={'count':0,'decided':{'p':'rejected'}}


def test_overview_api_uses_selected_read_transaction_and_rejects_wrong_run(tmp_path,monkeypatch):
    from datetime import datetime, timezone
    from fastapi.testclient import TestClient
    from self_improve.dashboard.app import create_app
    from self_improve.dashboard import overview_data
    db=tmp_path/'selected.db'
    with closing(Store(db)) as store:
        _run(store,run_id='selected',started='2030-02-07T02:00:00Z')
        store.commit()
    before=db.read_bytes();original=overview_data.snapshot;seen=[]
    def inspect(store,cfg,*,now_utc):
        assert store.db_path==db and store.conn.in_transaction
        seen.append(True)
        return original(store,cfg,now_utc=now_utc)
    monkeypatch.setattr(overview_data,'snapshot',inspect)
    cfg=Config(state_dir=str(tmp_path/'unopened'))
    with TestClient(create_app(cfg,db_path=db,clock=lambda:datetime(2030,2,8,tzinfo=timezone.utc))) as client:
        result=client.get('/api/overview')
        assert result.status_code==200 and result.json()['audit']['latest_run']['run']['id']=='selected'
    assert seen and db.read_bytes()==before and not (tmp_path/'unopened').exists()
    with closing(Store(db)) as store:
        store.update('runs','id','selected',{'stats_json':'{"run_id":"another"}'})
        store.commit()
    with TestClient(create_app(cfg,db_path=db)) as client:
        result=client.get('/api/overview')
        assert result.status_code==500 and 'selected' in result.text


def test_review_reads_and_inflight_command_boundaries_discard_older_results():
    from tests.test_navigation import node
    result=node('''
      const held=[];globalThis.fetch=async()=>({ok:true,json:()=>new Promise(r=>held.push(r))});
      const first=app.refreshReviewSnapshot();await Promise.resolve();await Promise.resolve();
      const second=app.refreshReviewSnapshot();await Promise.resolve();await Promise.resolve();
      held[1]({count:2});await second;held[0]({count:4});await first;
      const latest=app.state.review.count;
      const command=app.postJSON('/api/commands',{});await Promise.resolve();await Promise.resolve();
      const during=app.refreshReviewSnapshot();await Promise.resolve();await Promise.resolve();
      held[2]({id:'fixture'});await command;held[3]({count:4});await during;
      console.log(JSON.stringify({latest,after:app.state.review.count}));
    ''')
    assert result=={'latest':2,'after':2}
