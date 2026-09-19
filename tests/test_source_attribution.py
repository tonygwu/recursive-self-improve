"""Canonical labels and native identities over invented retained evidence."""
from contextlib import closing
from pathlib import Path
import json
import pytest
from fastapi.testclient import TestClient
from self_improve.store import Store
from self_improve.config import Config
from self_improve.dashboard import queries,rule_data,evidence_data
from self_improve.dashboard.app import create_app

STAMP='2030-01-01T00:00:00Z'
PROJECT='remote:example.test/team/original'


def seed(store,root):
    for n in range(3):
        store.insert('learnings',{'id':f'rule-{n}','title':f'Invented rule {n}',
            'rule_text':f'Preserve invented output {n}.','source':'agentic' if n==2 else 'codex' if n==1 else 'claude','created_at':STAMP})
    for name,provider,native,key,label in [
        ('claude','claude','shared-native','remote:example.test/team/changed','team/changed'),
        ('codex','codex','shared-native',PROJECT,'team/original'),
        ('alias','codex','another-native',PROJECT,'team/original-alias'),
        ('missing-id','codex','',PROJECT,''),
        ('unknown-a','','shared-native','', ''),('unknown-b','','shared-native','', ''),
        ('unrecognized','invented-provider','same','','')]:
        file=str(root/(name+'.jsonl'))
        store.insert('sessions',{'file_path':file,'source':provider,'session_id':native,
            'project_key':key,'project_display':label})
        store.insert('incidents',{'id':name,'session_file':file,'session_id':native,
            'project_key':PROJECT if name in ('claude','codex','alias','missing-id') else '',
            'signal_type':'correction','ts':STAMP,'created_at':STAMP,'matched_text':'Invented retained correction',
            'window_json':json.dumps([{'role':'user','text':'Keep the invented output.'}])})
        store.link_incident_learning(name,'rule-0' if name in ('claude','codex') else 'rule-1')
    store.commit()


@pytest.fixture
def env(tmp_path):
    with closing(Store(tmp_path/'state.db')) as store:
        seed(store,tmp_path)
        yield store,Config(state_dir=str(tmp_path/'unused')),tmp_path


def test_repository_labels_follow_incident_key_and_preserve_aliases(env):
    store,cfg,_=env
    row=rule_data.detail(store,'rule-0')
    assert row['provenance']['repos']==['team/original']
    assert row['project_count']==1
    refs=row['provenance']['projects']
    assert len(refs)==1 and refs[0]['key']==PROJECT
    assert refs[0]['aliases']==['team/original','team/original-alias']
    assert all(i['identity']['project']['key']==PROJECT for i in row['provenance']['incidents'])
    assert 'team/changed' not in json.dumps(row['provenance']['projects'])


def test_native_session_identity_includes_provider_and_links_to_exact_source(env):
    store,cfg,_=env
    p=rule_data.detail(store,'rule-0')['provenance']
    assert p['session_ids_total']==2
    assert p['known_session_count']==2 and p['unknown_session_records']==0
    assert len({r['key'] for r in p['sessions']})==2
    for session in p['sessions']:
        result=evidence_data.detail(store,cfg,kind='session',source_id=session['key'])
        assert result['source']['provider']==session['provider']
        assert result['source']['native_session_id']=='shared-native'
        assert len(result['source']['incidents'])==1
    review=queries._provenance(store,['rule-0'])['rule-0']
    assert review['session_count']==2 and review['unknown_session_records']==0


def test_unknown_identities_remain_separate_and_not_native_sessions(env):
    store,cfg,_=env
    p=rule_data.detail(store,'rule-1')['provenance']
    assert p['known_session_count']==1 and p['unknown_session_records']==4
    assert len(p['sessions'])==5
    assert p['unknown_source_incidents']==3
    for session in p['sessions']:
        result=evidence_data.detail(store,cfg,kind='session',source_id=session['key'])
        assert result['source']['identity_kind']==session['identity_kind']
    unknown=[s for s in p['sessions'] if s['identity_kind']=='transcript']
    assert len({s['key'] for s in unknown})==4
    assert all(s['reason'] for s in unknown)
    assert rule_data.detail(store,'rule-2')['provenance']['learning_source']['value'] is None
    assert rule_data.detail(store,'rule-2')['provenance']['learning_source']['recorded']=='agentic'


def test_missing_project_name_is_explicit_and_unknown_key_cannot_inherit_name(env):
    store,cfg,_=env
    store.conn.execute("UPDATE sessions SET project_display='' WHERE project_key=?",(PROJECT,))
    store.commit()
    project=rule_data.detail(store,'rule-0')['provenance']['projects'][0]
    assert project['label']==PROJECT and project['label_status']=='unavailable'
    store.update('incidents','id','claude',{'project_key':''});store.commit()
    row=rule_data.detail(store,'rule-0')
    unknown=next(r for r in row['provenance']['incidents'] if r['id']=='claude')['identity']
    assert unknown['project']['key']=='' and unknown['project']['label']=='Project unknown'
    assert row['unknown_project_incidents']==1


def test_incident_identity_is_visible_in_all_read_paths_and_search(env):
    store,cfg,_=env
    for rows in (evidence_data.search(store,cfg,query='',kinds=['incident'])['rows'],
                 evidence_data.learning_evidence(store,learning_id='rule-0')['rows']):
        row=next(r for r in rows if r['source_id']=='claude')
        assert row['identity']['project']['label']=='team/original'
        assert row['identity']['session']['provider_label']=='Claude Code'
    detail=evidence_data.detail(store,cfg,kind='incident',source_id='claude')
    assert detail['source']['identity']['project']['key']==PROJECT
    assert detail['source']['identity']['session']['native_session_id']=='shared-native'
    assert rule_data.browse(store,cfg,query='team/original Claude Code',grouping=False)['counts']['members']==1
    assert {r['source_id'] for r in evidence_data.search(store,cfg,query='team/original Claude Code',kinds=['incident'])['rows']}=={'claude'}


def test_identity_metadata_changes_invalidate_both_paged_readers(env):
    store,cfg,_=env
    rules=rule_data.browse(store,cfg,grouping=False,limit=1)
    evidence=evidence_data.learning_evidence(store,learning_id='rule-0',limit=1)
    store.conn.execute("UPDATE sessions SET project_display='team/renamed' WHERE project_key=?",(PROJECT,));store.commit()
    with pytest.raises(rule_data.RuleBrowserError,match='changed'):
        rule_data.browse(store,cfg,grouping=False,limit=1,cursor=rules['pagination']['next_cursor'])
    with pytest.raises(evidence_data.EvidenceError,match='changed'):
        evidence_data.learning_evidence(store,learning_id='rule-0',limit=1,cursor=evidence['pagination']['next_cursor'])


def test_actual_api_copied_store_and_metadata_reads_never_mutate(env,tmp_path,monkeypatch):
    store,cfg,_=env
    copy=tmp_path/'copy.db'
    from self_improve import rule_families
    monkeypatch.setattr(rule_families,'Embedder',lambda *a,**kw:pytest.fail('model during GET'))
    import sqlite3
    with closing(sqlite3.connect(copy)) as destination:store.conn.backup(destination)
    # Selected copy differs from both cfg's empty path and the original Store.
    store.conn.execute("UPDATE sessions SET project_display='wrong-original' WHERE project_key=?",(PROJECT,));store.commit()
    with closing(Store(copy,migrate=False)) as selected:
        before=list(selected.conn.iterdump())
        with TestClient(create_app(cfg,db_path=copy)) as client:
            rule=client.get('/api/rules/rule-0');assert rule.status_code==200
            assert rule.json()['provenance']['repos']==['team/original']
            sid=rule.json()['provenance']['sessions'][0]['key']
            response=client.get('/api/evidence/session/'+sid)
            assert response.status_code==200,response.text
        assert list(selected.conn.iterdump())==before


def test_identity_explanations_are_not_searchable_source_evidence(env):
    store,cfg,_=env
    assert rule_data.browse(store,cfg,query='runtime loading receipt')['counts']['members']==0
    assert evidence_data.search(store,cfg,query='runtime loading receipt')['pagination']['count']==0


def test_rendered_identity_escapes_names_and_keeps_exact_native_links(tmp_path):
    import subprocess
    from tests.spa_assets import copy_spa_dependencies
    copy_spa_dependencies(tmp_path)
    module=tmp_path/'app.js'
    module.write_bytes((Path(__file__).resolve().parents[1]/'src/self_improve/dashboard/static/app.js').read_bytes())
    value={'project':{'key':'repo:/one?two','label':'<img onerror="x">','aliases':['<bad>','safe'],'label_status':'known'},
           'session':{'key':'native/key','provider_label':'Claude Code','native_session_id':'<script>','identity_kind':'native_session'}}
    script=f'import {{renderSourceIdentity}} from {json.dumps(module.as_uri())}; console.log(renderSourceIdentity({json.dumps(value)}));'
    result=subprocess.run(['node','--input-type=module','-e',script],capture_output=True,text=True,check=True).stdout
    assert '<img' not in result and '<script>' not in result and '&lt;img' in result
    assert '#/projects/repo%3A%2Fone%3Ftwo' in result
    assert '#/rules/evidence/session/native%2Fkey?' in result
    assert 'Claude Code' in result and '2 retained names' in result


def test_returned_identity_cannot_poison_cached_source_results(env):
    store,cfg,_=env
    with closing(Store(store.db_path,read_only=True)) as reader:
        cache=evidence_data.EvidenceSearchCache(reader)
        with reader.transaction():
            result=evidence_data.search(reader,cfg,query='',kinds=['incident'],cache=cache)
        assert cache.retained_bytes>0
        row=next(r for r in result['rows'] if r['source_id']=='claude')
        row['identity']['project']['aliases'].append('invented-cache-poison')
        row['identity']['session']['provider_label']='invented-cache-poison'
        with reader.transaction():
            again=evidence_data.search(reader,cfg,query='',kinds=['incident'],cache=cache)
        assert 'invented-cache-poison' not in json.dumps(again)


@pytest.mark.parametrize('field,value',[('project_display','team/renamed'),('source','claude'),('session_id','changed-native')])
def test_search_revision_covers_identity_inputs_even_in_unlinked_transcripts(env,field,value):
    store,cfg,root=env
    store.insert('sessions',{'file_path':str(root/'unlinked.jsonl'),'source':'codex',
        'session_id':'unlinked','project_key':PROJECT,'project_display':'team/unlinked'})
    store.commit()
    before=evidence_data.search(store,cfg,query='',limit=1)
    store.update('sessions','file_path',str(root/'unlinked.jsonl'),{field:value});store.commit()
    after=evidence_data.search(store,cfg,query='',limit=1)
    assert before['revision']!=after['revision']
    with pytest.raises(evidence_data.EvidenceError,match='changed'):
        evidence_data.search(store,cfg,query='',limit=1,cursor=before['pagination']['next_cursor'])


@pytest.mark.parametrize('fingerprint',[False,True])
def test_full_window_and_derived_presentation_keep_complete_redaction(fingerprint):
    from tests.test_miner_agentic import SECRET
    row={'id':'invented','matched_text':'a'*40 if fingerprint else 'Retained '+SECRET,
         'ts':STAMP,'window_json':json.dumps([{'text':'x'*3989+' '+SECRET,
             'session_file':'/invented/'+SECRET,'project_path':'/invented/'+SECRET,
             'count_in_session':4,'ts':STAMP,'metadata':{'reason':SECRET}}])}
    source=evidence_data._incident(row)
    assert SECRET not in json.dumps(source)
    assert SECRET[:10] not in source['presentation']['display_text']
    assert '[REDACTED:' in json.dumps(source)
    assert source['presentation']['occurrences']['total_count']==4
    assert source['presentation']['occurrences']['sessions']==1


@pytest.mark.parametrize('change',['archive_tail','session_membership','learning_membership'])
def test_catalog_revision_binds_complete_incident_source_and_relationships(env,change):
    store,cfg,_=env
    window=[{'role':'user','text':'visible text '+('invented context '*400)}]
    store.update('incidents','id','claude',{'window_json':json.dumps(window)});store.commit()
    before=evidence_data.search(store,cfg,query='',limit=1)
    if change=='archive_tail':
        window[0]['text']+=' retained tail change'
        store.update('incidents','id','claude',{'window_json':json.dumps(window)})
    elif change=='session_membership':
        store.update('incidents','id','claude',{'session_id':'another-session'})
    else:store.link_incident_learning('claude','rule-2')
    store.commit()
    after=evidence_data.search(store,cfg,query='',limit=1)
    assert before['revision']!=after['revision']
    with pytest.raises(evidence_data.EvidenceError,match='changed'):
        evidence_data.search(store,cfg,query='',limit=1,cursor=before['pagination']['next_cursor'])
