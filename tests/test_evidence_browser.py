"""Actual evidence read routes and browser rendering over invented local state."""
import json
import sqlite3

import pytest
pytest.importorskip('fastapi')
from fastapi.testclient import TestClient
from tests.test_evidence_search import cfg,store,seed
from self_improve.dashboard.app import create_app
from self_improve.dashboard import evidence_data as evidence


def test_actual_evidence_routes_use_one_selected_read_transaction(cfg,store,tmp_path,monkeypatch):
    seed(store,tmp_path)
    copy=tmp_path/'selected.db';conn=sqlite3.connect(copy);store.conn.backup(conn);conn.close()
    store.update('learnings','id','l00',{'rule_text':'OutsideSelectedSentinel'});store.commit()
    calls=[]
    for name in ('search','detail','learning_evidence'):
        original=getattr(evidence,name)
        def guarded(selected,*args,_name=name,_reader=original,**kwargs):
            assert str(selected.db_path)==str(copy)
            assert selected.conn.in_transaction
            assert selected.read_only is True and selected.migrate is False
            with pytest.raises(sqlite3.OperationalError, match='readonly'):
                selected.conn.execute("UPDATE learnings SET title=title WHERE id='l00'")
            before=selected.conn.total_changes
            result=_reader(selected,*args,**kwargs)
            assert selected.conn.total_changes==before
            calls.append(_name)
            return result
        monkeypatch.setattr(evidence,name,guarded)
    with TestClient(create_app(cfg,db_path=copy)) as client:
        first=client.get('/api/evidence',params={'query':'','limit':50})
        assert first.status_code==200 and first.json()['pagination']['count']==118
        assert client.get('/api/evidence',params={'query':'OutsideSelectedSentinel'}).json()['pagination']['count']==0
        matches=client.get('/api/evidence',params={'query':'Needle archive end','kinds':'incident'}).json()
        assert matches['rows'][0]['source_id']=='i05'
        detail=client.get('/api/evidence/incident/i05').json()
        assert detail['source']['window'][0]['text'].endswith('Needle at full archive end.')
        page=client.get('/api/learnings/l00/evidence').json()
        tail=client.get('/api/learnings/l00/evidence',params={'cursor':page['pagination']['next_cursor']}).json()
        assert len(page['rows'])==20 and len(tail['rows'])==5
    assert set(calls)=={'search','detail','learning_evidence'}


def test_api_errors_stale_pages_and_missing_sources_are_named(cfg,store,tmp_path):
    seed(store,tmp_path)
    with TestClient(create_app(cfg,db_path=store.db_path),raise_server_exceptions=False) as client:
        for url,code,error in [('/api/evidence?kinds=unsupported',400,'InvalidEvidenceRequest'),
            ('/api/evidence?limit=51',400,'InvalidEvidenceRequest'),
            ('/api/evidence/incident/absent',404,'EvidenceNotFound'),
            ('/api/learnings/absent/evidence',404,'EvidenceNotFound')]:
            response=client.get(url);assert response.status_code==code and response.json()['error']==error
        page=client.get('/api/evidence').json()
        store.update('incidents','id','i01',{'status':'dismissed'});store.commit()
        stale=client.get('/api/evidence',params={'cursor':page['pagination']['next_cursor']})
        assert stale.status_code==409 and stale.json()['error']=='EvidenceChanged'
        store.update('incidents','id','i05',{'window_json':'"invalid array"'});store.commit()
        broken=client.get('/api/evidence/incident/i05')
        assert broken.status_code==500 and broken.json()['error']=='EvidenceDataError' and 'i05' in broken.json()['detail']


def test_coverage_rendering_distinguishes_archived_metadata_only_and_missing_schema():
    import subprocess
    from pathlib import Path
    module=Path(__file__).resolve().parents[1]/'src/self_improve/dashboard/static/app.js'
    result=subprocess.run(['node','--input-type=module','-e',f'''
        import {{renderEvidenceCoverage}} from {json.dumps(module.as_uri())};
        const base={{instruction_text:{{count:2,reason:""}},instruction_copy_count:2,
          instruction_copy_states:{{retained:1,metadata_only:1}},session_context:{{available:true,records:0,empty_batches:1,missing_batches:2}},
          native_reports:{{available:true,records:0}},revision_archive:{{available:true,records:0}}}};
        console.log(JSON.stringify([renderEvidenceCoverage(base),renderEvidenceCoverage({{...base,instruction_text:{{count:null,reason:"schema_unavailable"}},instruction_copy_count:0}})]));
    '''],check=True,capture_output=True,text=True)
    retained,missing=json.loads(result.stdout)
    assert '2 observed copies' in retained and 'metadata_only: 1' in retained
    assert 'unavailable' not in retained
    assert '1 recorded empty batches' in retained and '2 missing batches' in retained
    assert 'schema unavailable' in missing and '0 observed copies' not in missing


def test_complete_detail_links_proposals_by_learning_identity_not_mention(cfg,store,tmp_path):
    seed(store,tmp_path)
    store.insert('proposals',{'id':'unrelated','learning_id':'l01','target_path':str(tmp_path/'unrelated.md'),
        'target_kind':'global_claude_md','action':'add','status':'pending','created_at':'2030-01-01T00:00:00Z',
        'diff_unified':'An unrelated proposal mentioning l00 in its retained text.'})
    store.commit()
    with TestClient(create_app(cfg,db_path=store.db_path)) as client:
        response=client.get('/api/evidence/learning/l00');assert response.status_code==200
        assert response.json()['proposal_ids']==['p00']
        assert client.get('/api/evidence/learning/l01').json()['proposal_ids']==['unrelated']
        listing=client.get('/api/evidence',params={'kinds':'learning'}).json()
        assert listing['rows'][0]['excerpt']=='Complete rule body 0'


def test_excluded_proposal_loaded_off_route_renders_when_revisited():
    import subprocess
    from pathlib import Path
    module=Path(__file__).resolve().parents[1]/'src/self_improve/dashboard/static/app.js'
    script=f'''
      import * as m from {json.dumps(module.as_uri())};
      const panel={{innerHTML:"",focus(){{}}}};
      globalThis.document={{getElementById:id=>panel}};
      let resolve;globalThis.fetch=()=>new Promise(r=>resolve=r);
      m.state.route="review";m.state.reviewRouteProposal="excluded";
      const pending=m.loadExcludedProposal("excluded");
      m.state.route="rules";
      resolve({{ok:true,json:async()=>({{proposal_id:"excluded",revision:"fixture",
        snapshot:{{proposal:{{id:"excluded",status:"approved_user",diff_unified:"+fixture"}}}}}})}});
      await pending;
      m.state.route="review";await m.loadExcludedProposal("excluded");
      console.log(JSON.stringify({{html:panel.innerHTML,loading:m.state.selectedProposalRead.loading}}));
    '''
    run=subprocess.run(['node','--input-type=module','-e',script],capture_output=True,text=True,check=True)
    result=json.loads(run.stdout)
    assert result['loading'] is False and 'Recorded state: approved_user' in result['html']
    assert 'Refresh selected proposal' in result['html'] and 'Reading the selected proposal' not in result['html']
