"""Complete paginated Rules data over temporary invented evidence."""
from contextlib import closing
import json

import pytest
from fastapi.testclient import TestClient

from self_improve.config import Config
from self_improve.store import Store
from self_improve import rule_families
from self_improve.dashboard import rule_data as rd
from self_improve.dashboard.app import create_app


@pytest.fixture
def env(tmp_path):
    cfg = Config(state_dir=str(tmp_path))
    store = Store(tmp_path/'state.db')
    for i in range(45):
        lid = f'family-{i:02}' if i<23 else f'single-{i-23:02}'
        store.insert('learnings', {'id':lid,'title':f'Invented rule {i:02}', 'rule_text':f'Preserve invented output {i}.',
            'why':'A controlled example lost output.', 'created_at':f'2030-01-{1+i//24:02}T{i%24:02}:00:00Z',
            'status':'proposed','source':'agentic','duplicate_of':'family-00' if 0<i<23 else ''})
        store.insert('proposals', {'id':'p-'+lid,'learning_id':lid,'target_kind':'project_agents_md' if i%2 else 'global_claude_md',
            'target_path':str(tmp_path/('AGENTS.md' if i%2 else 'GLOBAL.md')), 'action':'add','created_at':'2030-01-01T00:00:00Z',
            'diff_unified':'--- before\n+++ after\n'+('+'+str(i)+'\n')*200,'status':'pending'})
    store.commit()
    rule_families.collect_families(store,cfg,cache_only=True)
    yield store,cfg,tmp_path
    store.close()


def browse(env, **kw):
    store,cfg,_=env
    with store.transaction():return rd.browse(store,cfg,**kw)


def test_pages_cover_every_family_once_and_ungrouped_every_member(env):
    first=browse(env)
    assert first['counts']=={'members':45,'families':23,'proposals':45,'target_paths':2,'all_members':45}
    assert first['pagination']['count']==23 and len(first['rows'])==20
    last=browse(env,cursor=first['pagination']['next_cursor'])
    assert len(last['rows'])==3 and last['pagination']['next_cursor'] is None
    keys=[r['key'] for r in first['rows']+last['rows']]
    assert len(set(keys))==23
    pages=[];cursor=None
    while True:
        page=browse(env,grouping=False,cursor=cursor)
        pages.extend(page['rows']);cursor=page['pagination']['next_cursor']
        if not cursor:break
    assert len(pages)==45 and len({row['rule']['id'] for row in pages})==45
    assert all(row['kind']=='rule' for row in pages)
    assert 'diff_unified' not in json.dumps(first) and 'window_json' not in json.dumps(first)


def test_target_filters_reconcile_members_families_proposals_and_physical_paths(env):
    global_rows=browse(env,target='global')
    assert global_rows['counts']=={'members':23,'families':12,'proposals':23,'target_paths':1,'all_members':45}
    family=next(row for row in global_rows['rows'] if row['kind']=='family')
    assert family['total_members']==23 and family['matched_members']==12
    assert family['proposal_count']==12 and family['target_paths']==1
    store,cfg,_=env
    with store.transaction():
        members=rd.members(store,cfg,family['family_id'],target='global')
    assert len(members['rows'])==12 and {r['targets'][0]['kind'] for r in members['rows']}=={'global_claude_md'}
    assert browse(env,target='hook')['counts']['members']==0
    assert browse(env,target='none')['counts']['members']==0
    store.insert('learnings', {'id':'unrouted','rule_text':'No destination yet','created_at':'2030-01-01T00:00:00Z'});store.commit()
    missing=browse(env,target='none')
    assert missing['counts']['members']==1 and missing['rows'][0]['rule']['target_paths']==0


def test_complete_family_member_pages_are_bound_to_selection(env):
    store,cfg,_=env
    family=next(row for row in browse(env)['rows'] if row['kind']=='family')
    with store.transaction():
        first=rd.members(store,cfg,family['family_id'])
        last=rd.members(store,cfg,family['family_id'],cursor=first['pagination']['next_cursor'])
        with pytest.raises(rd.RuleBrowserError,match='another selection'):
            rd.members(store,cfg,family['family_id'],target='global',cursor=first['pagination']['next_cursor'])
    assert len(first['rows'])==20 and len(last['rows'])==3
    assert {row['id'] for row in first['rows']+last['rows']}=={f'family-{i:02}' for i in range(23)}


def test_stale_cursor_fails_instead_of_mixing_pages_and_sort_is_repeatable(env):
    page=browse(env,sort='title')
    assert browse(env,sort='title')==page
    store,_,_=env
    store.update('proposals','id','p-single-21',{'status':'rejected_user'});store.commit()
    with pytest.raises(rd.RuleBrowserError) as failure:browse(env,sort='title',cursor=page['pagination']['next_cursor'])
    assert failure.value.status==409 and failure.value.code=='RulesChanged'
    assert browse(env,sort='recent')['rows'][0]['kind']=='rule'


def test_search_uses_sixth_linked_incident_and_literal_tokens_with_shape_validation(env):
    store,_,_=env
    for i in range(7):
        store.insert('sessions',{'file_path':f'missing-{i}','source':'codex','session_id':f'session-{i}'})
        store.insert('incidents',{'id':f'inc-{i}','session_file':f'missing-{i}','session_id':f'session-{i}',
            'created_at':'2030-01-01T00:00:00Z','project_key':'repo:one','signal_type':'correction','matched_text':'synthetic note',
            'window_json':json.dumps([{'role':'user','text':'SIXTH_SENTINEL café 100%_%' if i==5 else 'ordinary evidence'}])})
        store.link_incident_learning(f'inc-{i}','single-21')
    store.commit()
    result=browse(env,query='SIXTH_SENTINEL café 100%_% session-5')
    assert result['counts']['members']==1 and result['rows'][0]['rule']['id']=='single-21'
    assert result['rows'][0]['rule']['project_count']==1
    assert result['rows'][0]['rule']['evidence_linked']==7
    store.update('learnings','id','single-21',{'project_count':7});store.commit()
    detail=rd.detail(store,'single-21')
    assert detail['project_count']==1 and detail['recorded_project_count']==7
    store.update('incidents','id','inc-5',{'window_json':'"wrong shape"'});store.commit()
    with pytest.raises(rd.queries.DashboardDataError,match='window_json'):
        browse(env,query='sentinel')


def test_selected_detail_reads_only_its_evidence_and_keeps_complete_diff(env,monkeypatch):
    store,_,_=env
    # An unrelated corrupt archive must not be read by a selected inspector.
    store.insert('sessions',{'file_path':'missing','source':'codex'})
    store.insert('incidents',{'id':'bad','created_at':'2030-01-01T00:00:00Z','session_file':'missing','signal_type':'correction','window_json':'"bad"'})
    store.link_incident_learning('bad','family-00');store.commit()
    statements=[];original=store.query
    def query(sql,*args,**kw):
        statements.append(sql);return original(sql,*args,**kw)
    monkeypatch.setattr(store,'query',query)
    row=rd.detail(store,'single-21')
    assert row['id']=='single-21' and len(row['targets'][0]['diff'].splitlines())==202
    evidence_queries=[sql for sql in statements if 'window_json' in sql]
    assert evidence_queries and all('WHERE il.learning_id IN' in sql for sql in evidence_queries)
    assert not any('SELECT h.* FROM mining_history' in sql for sql in statements)


@pytest.mark.parametrize('kw',[{'target':'bogus'},{'sort':'bogus'},{'limit':0},{'limit':51},{'cursor':'bad'},{'query':'x'*1001}])
def test_invalid_requests_fail_with_named_error(env,kw):
    with pytest.raises(rd.RuleBrowserError):browse(env,**kw)


def test_actual_api_uses_selected_store_without_models_or_mutation(env,monkeypatch):
    store,cfg,tmp=env
    monkeypatch.setattr(rule_families,'Embedder',lambda *a,**kw:pytest.fail('GET loaded a model'))
    app=create_app(cfg,db_path=store.db_path)
    before={table:store.query(f'SELECT * FROM {table}') for table in ('learnings','proposals','llm_calls',*rule_families.TABLES)}
    with TestClient(app) as client:
        first=client.get('/api/rules/browse').json()
        assert first['mode']=='paged' and first['counts']['members']==45
        second=client.get('/api/rules/browse',params={'cursor':first['pagination']['next_cursor']})
        assert second.status_code==200 and len(second.json()['rows'])==3
        family=next(row for row in first['rows'] if row['kind']=='family')
        assert client.get('/api/rule-families/'+family['family_id']+'/members').json()['matched_members']==23
        detail=client.get('/api/rules/single-21')
        assert detail.status_code==200 and len(detail.json()['targets'][0]['diff'].splitlines())==202
        assert client.get('/api/rules/nonexistent').status_code==404
        assert client.get('/api/rules/browse?target=bogus').status_code==400
        assert client.get('/api/rules/browse?limit=900').status_code==400
    after={table:store.query(f'SELECT * FROM {table}') for table in before}
    assert before==after

def test_known_generation_and_corrupt_history_have_named_api_results(env):
    from self_improve import mining_history
    store,cfg,_=env
    learning=store.query_one("SELECT * FROM learnings WHERE id='single-21'")
    history=mining_history.append(store,learning,before=None,kind='new',incidents=[],provenance={'generation':'agentic','run_id':'synthetic-run'})
    store.commit()
    row=next(r['rule'] for r in browse(env,query='single-21')['rows'])
    assert row['miner_generation']['value']=='agentic' and row['miner_generation']['computable']
    store.update('mining_history','id',history['id'],{'record_json':'{}'});store.commit()
    with TestClient(create_app(cfg,db_path=store.db_path),raise_server_exceptions=False) as client:
        for path in ('/api/rules/browse?query=single-21','/api/rules/single-21'):
            response=client.get(path)
            assert response.status_code==500
            assert response.json()['error']=='MiningHistoryError'
            assert history['id'] in response.json()['detail']


def _invented_target_paths(store):
    """Proposal target paths are searched. The fixture puts them below
    tmp_path, which names the operating-system user (pytest-of-<user>), so a
    zero-match search for a common word would depend on the machine."""
    for row in store.query('SELECT id,target_path FROM proposals'):
        store.update('proposals','id',row['id'],{'target_path':'/invented/'+row['target_path'].rsplit('/',1)[1]})
    assert {row['target_path'] for row in store.query('SELECT target_path FROM proposals')}=={'/invented/AGENTS.md','/invented/GLOBAL.md'}


def test_rules_search_matches_decoded_evidence_text_not_json(env):
    """BL-46: Rules search indexed the JSON-encoded window, so a literal quote
    or backslash never matched and JSON keys such as "role" matched. BL-46
    review: indexing every string value made each role value match instead,
    so "user" or "assistant" found every rule with a trajectory window."""
    store,_,_=env
    _invented_target_paths(store)
    store.insert('sessions',{'file_path':'missing-q','source':'codex','session_id':'session-q'})
    store.insert('incidents',{'id':'inc-q','session_file':'missing-q','session_id':'session-q',
        'created_at':'2030-01-01T00:00:00Z','project_key':'repo:one','signal_type':'correction','matched_text':'synthetic note',
        'window_json':json.dumps([{'role':'user','text':'ran "pytest-quoted" in C:\\work\\winpath'},
                                  {'role':'assistant','text':'invented reply'},
                                  {'role':'tool_result','text':'invented output'}])})
    store.link_incident_learning('inc-q','single-21')
    store.commit()
    assert browse(env,query='"pytest-quoted"')['counts']['members']==1
    assert browse(env,query='C:\\work\\winpath')['counts']['members']==1
    assert browse(env,query='invented reply')['counts']['members']==1
    assert browse(env,query='{"role":')['counts']['members']==0
    # A key name alone is not evidence text, and neither is a role value.
    for query in ('role','user','assistant','tool_result'):
        assert browse(env,query=query)['counts']['members']==0,query


def test_rules_search_matches_every_decoded_window_value_but_no_key(env):
    """BL-46 review: searching only event['text'] lost recall. A repeated-error
    window keeps its locations in other fields, so a search for the clone or
    the session that saw the error matched nothing. Every decoded string value
    is searchable, at any depth; key names and non-string values are not.
    The role and ts values of an entry label the archive and are not
    searchable; a nested object has no defined fields, so its values are."""
    store,_,_=env
    _invented_target_paths(store)
    store.insert('sessions',{'file_path':'missing-r','source':'claude','session_id':'session-r'})
    store.insert('incidents',{'id':'inc-r','session_file':'missing-r','session_id':'session-r',
        'created_at':'2030-01-01T00:00:00Z','project_key':'repo:two','signal_type':'repeated_error','matched_text':'fingerprint-r',
        'window_json':json.dumps([{'session_file':'/transcripts/other-clone-session.jsonl','project_path':'/Code/widget/repo-7',
                                   'count_in_session':31337,'ts':'2031-07-19T13:57:46Z','text':'invented error',
                                   'tool':{'name':'deeply-nested-tool','role':'nested-role-value'}}])})
    store.link_incident_learning('inc-r','single-20')
    store.commit()
    for query in ('repo-7','other-clone-session','deeply-nested-tool','/Code/widget/repo-7','nested-role-value'):
        assert browse(env,query=query)['counts']['members']==1,query
    for query in ('project_path','count_in_session','session_file','31337','2031','2031-07-19','13:57:46'):
        assert browse(env,query=query)['counts']['members']==0,query


def test_rules_search_for_user_matches_a_users_home_path(env):
    """BL-46 review, documented and not fixed: Rules search is a casefolded
    substring match over target paths and session paths too. Every macOS home
    path starts with /Users/, so a search for "user" matches each rule whose
    target or session lives in a home directory, though no evidence text holds
    the word. The tests above use /invented/ paths, which hide this. This test
    pins the current behavior, so a change to it is a decision someone makes."""
    store,_,_=env
    _invented_target_paths(store)
    store.commit()
    assert browse(env,query='user')['counts']['members']==0
    store.update('proposals','id','p-single-01',{'target_path':'/Users/invented-person/Code/widget/AGENTS.md'})
    session='/Users/invented-person/.claude/projects/widget/session-u.jsonl'
    store.insert('sessions',{'file_path':session,'source':'claude','session_id':'session-u'})
    store.insert('incidents',{'id':'inc-u','session_file':session,'session_id':'session-u',
        'created_at':'2030-01-01T00:00:00Z','project_key':'repo:one','signal_type':'correction','matched_text':'synthetic note',
        'window_json':json.dumps([{'role':'assistant','text':'invented reply'}])})
    store.link_incident_learning('inc-u','single-02')
    store.commit()
    for query in ('user','USERS','/users/invented-person'):
        rows=browse(env,query=query,grouping=False)['rows']
        assert {row['rule']['id'] for row in rows}=={'single-01','single-02'},query
