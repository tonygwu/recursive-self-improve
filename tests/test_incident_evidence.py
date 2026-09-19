"""Invented heterogeneous archives through actual readers and fallback entry points."""
import json

import pytest

from self_improve.dashboard import queries

FINGERPRINT='a'*40


def incident(window):
    return {'id':'invented-incident','signal_type':'repeated_error','matched_text':FINGERPRINT,
            'window_json':json.dumps(window),'ts':'2030-01-01T00:00:00Z'}


def occurrence(**changes):
    return {'text':'Invented readable error','ts':'2030-01-01T00:00:00Z',
            'session_file':'fixture-a.jsonl','project_path':'/fixture/project','count_in_session':3,**changes}


@pytest.mark.parametrize('count',[True,-1,1.5,'3'])
def test_reader_refuses_coerced_occurrence_counts(count):
    with pytest.raises(queries.DashboardDataError,match='invented-incident'):
        queries.normalize_incident(incident([occurrence(count_in_session=count)]))


@pytest.mark.parametrize('bad',[None,'entry',3,{'text':3},{'role':False,'text':'x','ts':''}])
def test_reader_validates_every_archive_entry(bad):
    with pytest.raises(queries.DashboardDataError,match='invented-incident'):
        queries.normalize_incident(incident([occurrence(),bad]))


def test_occurrence_sessions_count_distinct_paths_and_missing_count_is_unknown():
    row=queries.normalize_incident(incident([occurrence(),occurrence()]))
    assert row['occurrences']['sessions']==1
    assert row['occurrences']['total_count']==6
    missing=occurrence();del missing['count_in_session']
    row=queries.normalize_incident(incident([missing]))
    assert row['occurrences']['total_count'] is None
    assert row['occurrences']['count_reason']


def test_mixed_archive_shape_is_not_taken_from_only_the_first_entry():
    row=queries.normalize_incident(incident([occurrence(),{'role':'user','text':'Please inspect the fixture','ts':''}]))
    assert row['window_kind']=='mixed'
    assert row['display_text']=='Invented readable error'


@pytest.mark.parametrize('raw', ['null', '{}', '"text"', '[NaN]', '[{"text":"a","text":"b"}]'])
def test_strict_archive_json(raw):
    from self_improve.incident_evidence import parse_window, IncidentEvidenceError
    with pytest.raises(IncidentEvidenceError, match='fixture-owner'):
        parse_window(raw, owner='fixture-owner')


def test_incomplete_zero_offsets_and_omissions_do_not_invent_coverage():
    from self_improve.incident_evidence import present
    unknown = present(incident([{'text':'Incomplete legacy event'}, {}]))
    assert unknown['window_kind']=='unknown' and unknown['occurrences'] is None
    empty=present(incident([]))
    assert empty['window_kind']=='empty' and empty['display_text_reason']
    entries=[occurrence(count_in_session=0, ts='', session_file='', project_path='')]
    entries += [occurrence(project_path=f'/fixture/{n}', session_file=f'{n}.jsonl') for n in range(7)]
    entries += [occurrence(ts='2030-01-01T01:00:00+02:00'), occurrence(ts='2029-12-31T23:30:00Z')]
    value=present(incident(entries),max_chars=8)
    assert value['occurrences']['first_ts']=='2030-01-01T01:00:00+02:00'
    assert value['occurrences']['last_ts']=='2030-01-01T00:00:00Z'
    assert value['occurrences']['sessions'] is None
    assert value['occurrence_coverage']['unknown_times']==1
    assert value['occurrence_coverage']['unknown_projects']==1
    assert value['occurrence_coverage']['locations_omitted']==3
    assert value['display_text_truncated']['cut_chars']==len('Invented readable error')-8
    zero=present(incident([occurrence(count_in_session=0)]))
    assert zero['occurrences']['total_count']==0
    ordinary={**incident([{'role':'user','ts':'','text':'Window context'}]),'matched_text':'Ordinary trigger'}
    assert present(ordinary)['display_text']=='Ordinary trigger'


from tests.test_rule_availability import cfg, store


def test_selected_copy_readers_share_evidence_without_changing_sources(cfg, store, tmp_path):
    import sqlite3
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from self_improve.dashboard.app import create_app
    from tests.test_evidence_search import seed
    from self_improve.dashboard import queries
    seed(store,tmp_path)
    window=[occurrence(),occurrence(count_in_session=2),occurrence(session_file='second.jsonl',project_path='/fixture/second')]
    store.update('incidents','id','i06',{'window_json':json.dumps(window)})
    store.commit()
    assert queries._provenance(store,['l00'])['l00']['examples']  # Actual Review aggregation.
    # Put the promoted incident inside the bounded examples, not just the full list.
    store.update('incidents','id','i06',{'ts':'2030-01-02T00:00:00Z'});store.commit()
    example=queries._provenance(store,['l00'])['l00']['examples'][0]
    assert example['presentation']['display_text']=='Invented readable error'
    copied=tmp_path/'selected.db'
    with sqlite3.connect(copied) as conn:store.conn.backup(conn)
    store.update('incidents','id','i06',{'matched_text':'Outside selected database'});store.commit()
    with sqlite3.connect(copied) as conn:before=list(conn.iterdump())
    with TestClient(create_app(cfg,db_path=copied)) as client:
        params={'project_key':'remote:example.test/fixture/repo','kind':'evidence','learning_id':'l00','limit':5}
        all_rows=[]
        while True:
            response=client.get('/api/project-records',params=params);assert response.status_code==200,response.text
            data=response.json();all_rows.extend(data['records'])
            if not data['next_cursor']:break
            params['cursor']=data['next_cursor']
        assert len(all_rows)==25
        project=next(r for r in all_rows if r['id']=='i06')
        full=client.get('/api/evidence/incident/i06').json()['source']
        assert project['presentation']['display_text']==full['presentation']['display_text']=='Invented readable error'
        assert project['presentation']['occurrences']['sessions']==2
        assert project['presentation']['occurrences']['total_count']==8
        assert json.loads(project['window_json'])==full['window']==window
        assert not client.get('/api/evidence',params={'query':'No readable text was retained.'}).json()['rows']
    with sqlite3.connect(copied) as conn:assert list(conn.iterdump())==before


def test_fallback_preserves_each_location_and_redacts_without_original_transcript(cfg,store,tmp_path):
    from self_improve import miner,render
    from tests.test_miner_agentic import seed_incident
    row=seed_incident(store,str(tmp_path/'deleted.jsonl'),project=str(tmp_path),ts='2030-01-01T00:00:00Z')
    window=[occurrence(session_file='/fixture/one/a.jsonl'),occurrence(session_file='/fixture/two/a.jsonl',role='tool_result',count_in_session=0,project_path='/fixture/beta')]
    from tests.test_miner_agentic import SECRET
    window[0]['text']+=' '+SECRET
    row['window_json']=json.dumps(window)
    events,_=miner._fallback_mine_events(store,row)
    text,_=render.session_document(events)
    for part in ('/fixture/one/a.jsonl','/fixture/two/a.jsonl','/fixture/beta','3x','0x','Invented readable error'):
        assert part in text
    assert SECRET not in text and '[REDACTED:' in text
    assert not (tmp_path/'deleted.jsonl').exists()


@pytest.mark.parametrize('mode',['fast','agentic'])
def test_malformed_mining_input_fails_before_calls_or_mutation(cfg,store,tmp_path,mode,monkeypatch):
    from dataclasses import replace
    from self_improve import miner,incident_jobs
    from self_improve.commands import CommandError
    from tests.test_miner_agentic import seed_incident
    row=seed_incident(store,str(tmp_path/'deleted.jsonl'),project=str(tmp_path),ts='2030-01-01T00:00:00Z')
    row['window_json']=json.dumps([occurrence(),occurrence(count_in_session=True)])
    store.update('incidents','id',row['id'],{'window_json':row['window_json']});store.commit()
    before=list(store.conn.iterdump())
    def forbidden(*args,**kwargs):raise AssertionError('No model or instruction read permitted')
    monkeypatch.setattr(miner,'gather_in_force_instructions',forbidden)
    with pytest.raises(CommandError,match='entry 2'):
        incident_jobs.view(store,replace(cfg,mine_mode=mode),row['id'])
    if mode=='fast':
        with pytest.raises(miner.MinerError,match='entry 2'):
            miner.mine_incident(store,forbidden,row,cfg,tmp_path)
    else:
        with pytest.raises(miner.MinerError,match='entry 2'):
            miner._fallback_mine_events(store,row)
    assert list(store.conn.iterdump())==before


def test_mining_view_keeps_frozen_source_revision_and_budget(cfg,store,tmp_path):
    from self_improve import incident_jobs
    from tests.test_miner_agentic import seed_incident
    row=seed_incident(store,str(tmp_path/'deleted.jsonl'),project=str(tmp_path),ts='2030-01-01T00:00:00Z')
    store.update('incidents','id',row['id'],{'matched_text':FINGERPRINT,'window_json':json.dumps([occurrence()])});store.commit()
    before=list(store.conn.iterdump())
    frozen=incident_jobs.preview(store,cfg,row['id'])
    shown=incident_jobs.view(store,cfg,row['id'],full=True)
    assert shown['source']==frozen['source'] and shown['revision']==frozen['revision']
    assert shown['plan']==frozen['plan'] and shown['max_model_calls']==1
    assert shown['source_summary']['presentation']['display_text']=='Invented readable error'
    assert 'presentation' not in shown['source']['snapshot']['incident']
    assert list(store.conn.iterdump())==before


def test_actual_javascript_primary_examples_and_frozen_archives(tmp_path):
    import shutil,subprocess
    from pathlib import Path
    import self_improve
    from tests.spa_assets import copy_spa_dependencies
    (tmp_path/'app.mjs').write_bytes((Path(self_improve.__file__).parent/'dashboard/static/app.js').read_bytes())
    copy_spa_dependencies(tmp_path)
    (tmp_path/'probe.mjs').write_text(r'''
import assert from 'node:assert/strict';
const m=await import('./app.mjs');
const raw={id:'invented',matched_text:'a'.repeat(40),signal_type:'repeated_error',window_json:JSON.stringify([
 {text:'Readable <script>error</script>',ts:'',session_file:'/fixture/a.jsonl',project_path:'/fixture/alpha',count_in_session:0},
 {text:'Last '+ 'x'.repeat(1000)+' FULL END',ts:'',session_file:'/fixture/b.jsonl'}])};
const frozen=JSON.stringify(raw);
const primary=m.renderIncidentPrimary(raw);
assert.match(primary,/<p data-incident-readable="true">Readable &lt;script&gt;error&lt;\/script&gt;<\/p>/);
assert.match(primary,/Detector fingerprint:/);
assert.doesNotMatch(primary,/<script>/);
const retained=m.renderRetainedEvidence(raw);
for(const part of ['0 occurrences','Count unknown','Time unknown','/fixture/a.jsonl','/fixture/alpha','FULL END'])assert.ok(retained.includes(part),part);
assert.equal(JSON.stringify(raw),frozen);
assert.match(m.renderRetainedEvidence({...raw,window_json:'[{"text":false}]'}),/Retained context is unreadable/);
const annotated={...raw,presentation:{display_text:'Readable sample',fingerprint:raw.matched_text,occurrences:{sessions:1,total_count:null,first_ts:'',last_ts:'',project_paths:[],count_reason:'A count was not retained.'},occurrence_coverage:{locations:['/fixture/a'],locations_omitted:2,unknown_times:1,unknown_projects:1,other_entries:1}}};
const example=m.renderIncidentExamples({provenance:{examples:[annotated],incident_count:2,examples_held_back:1}});
for(const part of ['Readable sample','unknown number of time(s)','A count was not retained.','2 more project locations','known times only','excluded from occurrence totals','Inspect complete incident evidence','1 more incident'])assert.ok(example.includes(part),part);
assert.doesNotMatch(example,/<pre class="review-incident__text">a{40}/);
assert.match(m.renderIncidentPrimary({matched_text:'Ordinary trigger'}),/Ordinary trigger/);
console.log('INCIDENT_RENDERERS_OK');
''')
    result=subprocess.run([shutil.which('node'),str(tmp_path/'probe.mjs')],capture_output=True,text=True)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'INCIDENT_RENDERERS_OK' in result.stdout


def test_handled_mining_preview_reports_corruption_without_changing_state(cfg,store,tmp_path):
    from self_improve import incident_jobs
    from self_improve.commands import CommandError
    from tests.test_miner_agentic import seed_incident
    row=seed_incident(store,str(tmp_path/'deleted.jsonl'),project=str(tmp_path))
    store.update('incidents','id',row['id'],{'status':'mined','window_json':'[{"text":false}]'});store.commit()
    before=list(store.conn.iterdump())
    with pytest.raises(CommandError,match='entry 1') as exc:incident_jobs.view(store,cfg,row['id'])
    assert exc.value.code=='IncidentDataError' and row['id'] in str(exc.value)
    assert list(store.conn.iterdump())==before


@pytest.mark.parametrize("leave_inspector", [False, True])
def test_evidence_reply_preserves_the_focused_tab_before_activation(tmp_path, leave_inspector):
    import shutil,subprocess
    from pathlib import Path
    import self_improve
    from tests.spa_assets import copy_spa_dependencies
    (tmp_path/'app.mjs').write_bytes((Path(self_improve.__file__).parent/'dashboard/static/app.js').read_bytes())
    copy_spa_dependencies(tmp_path)
    (tmp_path/'focus.mjs').write_text(r'''
import assert from 'node:assert/strict';
const m=await import('./app.mjs');
const nodes=new Map();
const tabs=['diagnosis','source','linked'].map(tab=>({getAttribute:k=>k==='data-tab'?tab:null,focus(){document.activeElement=this;}}));
globalThis.document={activeElement:null,getElementById(id){
 if(!nodes.has(id))nodes.set(id,{style:{},classList:{add(){},remove(){}},setAttribute(){},focus(){document.activeElement=this;},querySelectorAll:()=>id==='inspector-body'?tabs:[],contains:el=>id==='inspector-body'&&tabs.includes(el),_html:'',set innerHTML(v){this._html=v;if(this.contains(document.activeElement))document.activeElement=null;},get innerHTML(){return this._html;}});
 return nodes.get(id);
}};
let answer;globalThis.fetch=()=>new Promise(resolve=>answer=resolve);
m.state.route='rules';m.state.ruleQuery='mode=evidence';
const read=m.openEvidence('incident','fixture');
tabs[1].focus(); // The operator tabs to Source while the read is pending.
const outside=document.getElementById('search');
if(LEAVE_INSPECTOR)outside.focus();
answer({ok:false,status:503,json:async()=>({detail:'Invented read failure'})});
await read;
assert.equal(document.activeElement,LEAVE_INSPECTOR?outside:tabs[1]);
assert.equal(m.state.inspectorTab,'diagnosis'); // Focus is not activation.
assert.match(nodes.get('inspector-body').innerHTML,/Invented read failure/);
console.log('EVIDENCE_PENDING_FOCUS_OK');
'''.replace('LEAVE_INSPECTOR', 'true' if leave_inspector else 'false'))
    result=subprocess.run([shutil.which('node'),str(tmp_path/'focus.mjs')],capture_output=True,text=True)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'EVIDENCE_PENDING_FOCUS_OK' in result.stdout


def test_complete_evidence_redacts_before_capping_legacy_primary_text():
    from self_improve.dashboard.evidence_data import _incident
    from tests.test_miner_agentic import SECRET
    text='x'*3989+' '+SECRET
    source=_incident(incident([occurrence(text=text)]))
    assert SECRET not in source['window'][0]['text']
    assert SECRET[:10] not in source['presentation']['display_text']
    assert '[REDACTED:' in source['presentation']['display_text']
