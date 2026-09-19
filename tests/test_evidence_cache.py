"""A bounded search projection follows the selected reader's real SQLite snapshot."""
from contextlib import closing
from copy import deepcopy
import pytest

pytest.importorskip('fastapi')
from fastapi.testclient import TestClient
from self_improve.dashboard import evidence_data as e
from self_improve.dashboard.app import create_app
from self_improve.store import Store
from tests.test_evidence_search import cfg, store, seed


def counted(monkeypatch):
    calls=[]
    original=e._catalog
    def build(reader):
        calls.append(reader)
        return original(reader)
    monkeypatch.setattr(e,'_catalog',build)
    return calls


def test_real_api_reuses_complete_catalog_for_queries_and_pages_then_invalidates(store,cfg,tmp_path,monkeypatch):
    seed(store,tmp_path)
    calls=counted(monkeypatch)
    app=create_app(cfg,db_path=store.db_path)
    with TestClient(app) as client:
        first=client.get('/api/evidence',params={'kinds':'learning','limit':20}).json()
        second=client.get('/api/evidence',params={'query':'needle archive end','kinds':'incident'}).json()
        next_page=client.get('/api/evidence',params={'kinds':'learning','limit':20,'cursor':first['pagination']['next_cursor']})
        assert first['pagination']['count']==45 and next_page.status_code==200
        assert [r['source_id'] for r in second['rows']]==['i05']
        assert len(calls)==1,'Unchanged reader snapshot rebuilt the complete retained catalog'
        store.update('learnings','id','l44',{'why':'Changed retained catalog sentinel'});store.commit()
        stale=client.get('/api/evidence',params={'kinds':'learning','limit':20,'cursor':first['pagination']['next_cursor']})
        assert stale.status_code==409 and stale.json()['error']=='EvidenceChanged'
        current=client.get('/api/evidence',params={'query':'Changed retained catalog sentinel'}).json()
        assert [r['source_id'] for r in current['rows']]==['l44']
        assert len(calls)==2
        cache=app.state.evidence_search_cache
        assert cache.retained_bytes>0
    assert cache.retained_bytes==0 and app.state.evidence_search_cache is None


def test_cached_and_uncached_search_agree_for_every_source_and_redacted_tail(store,cfg,tmp_path,monkeypatch):
    from tests.test_rule_availability import known_copy, delivery, observe, init_git_repo, run_git
    seed(store,tmp_path)
    repo=init_git_repo(tmp_path/'project','AGENTS.md','# Invented instruction archive\n')
    run_git(['remote','add','origin','https://example.test/fixture/cache.git'],repo)
    known_copy(store,repo);delivery(store,cfg,repo);observe(store,cfg,1)
    store.update('learnings','id','l44',{'rule_text':'Café STRASSE abc%_x '+('tail text '*800)+'tail sentinel'})
    store.update('incidents','id','i01',{'matched_text':'Invented reader@example.invalid contact'})
    store.commit()
    options=[{'query':''},{'query':'Cafe\u0301 straße abc%_x'},{'query':'tail sentinel'},
             {'query':'reader@example.invalid'},{'query':'[REDACTED:email]'},
             {'query':'needle archive end','kinds':['incident']},
             {'query':'','project_key':'remote:example.test/fixture/repo'},
             *({'query':'','kinds':[kind],'limit':2} for kind in e.KINDS)]
    with closing(Store(store.db_path,read_only=True)) as reader:
        with reader.transaction():
            expected=[e.search(reader,cfg,**opts) for opts in options]
        assert all(expected[0]['corpus_counts'][kind]>0 for kind in e.KINDS)
        assert expected[3]['pagination']['count']==0 and expected[4]['pagination']['count']>0
        cache=e.EvidenceSearchCache(reader);calls=counted(monkeypatch)
        for opts,want in zip(options,expected):
            with reader.transaction():assert e.search(reader,cfg,cache=cache,**opts)==want
        assert len(calls)==1
        assert cache.retained_bytes<=cache.max_bytes
        assert 'source' not in cache._value['rows'][0] and 'incidents' not in cache._value
        assert e._object_bytes(cache._value)==cache.retained_bytes
        cache.clear()


def test_concurrent_commit_keeps_old_snapshot_then_refreshes_and_refuses_corruption(store,cfg,tmp_path,monkeypatch):
    seed(store,tmp_path)
    with closing(Store(store.db_path,read_only=True)) as reader:
        cache=e.EvidenceSearchCache(reader);original=e._catalog;calls=[]
        def build(current):
            value=original(current);calls.append(current)
            if len(calls)==1:
                store.update('learnings','id','l44',{'why':'Committed during catalog construction'});store.commit()
            return value
        monkeypatch.setattr(e,'_catalog',build)
        with reader.transaction():
            old=e.search(reader,cfg,query='Committed during catalog construction',cache=cache)
            assert old['pagination']['count']==0
            assert e.search(reader,cfg,query='Committed during catalog construction',cache=cache)==old
            assert reader.conn.in_transaction and len(calls)==1
        with reader.transaction():
            new=e.search(reader,cfg,query='Committed during catalog construction',cache=cache)
            assert [r['source_id'] for r in new['rows']]==['l44'] and new['revision']!=old['revision']
        assert len(calls)==2
        store.update('incidents','id','i05',{'window_json':'"invalid array"'});store.commit()
        with reader.transaction(),pytest.raises(e.EvidenceError,match='i05'):
            e.search(reader,cfg,query='unrelated filter',kinds=['learning'],cache=cache)
        assert cache.retained_bytes==0


def test_rollback_preserves_snapshot_but_schema_coverage_commit_invalidates(store,cfg,tmp_path,monkeypatch):
    seed(store,tmp_path)
    with closing(Store(store.db_path,read_only=True)) as reader:
        cache=e.EvidenceSearchCache(reader);calls=counted(monkeypatch)
        with reader.transaction():before=e.search(reader,cfg,query='',cache=cache)
        store.update('learnings','id','l44',{'why':'Rolled back change'});store.conn.rollback()
        with reader.transaction():assert e.search(reader,cfg,query='',cache=cache)==before
        assert len(calls)==1
        store.conn.execute("DELETE FROM schema_migrations WHERE name='0033_instruction_text'");store.commit()
        with reader.transaction():after=e.search(reader,cfg,query='',cache=cache)
        assert len(calls)==2 and after['revision']!=before['revision']
        assert after['coverage']['instruction_text']['reason']=='schema_unavailable'


def test_mutating_public_result_cannot_poison_a_later_search(store,cfg,tmp_path):
    seed(store,tmp_path)
    with closing(Store(store.db_path,read_only=True)) as reader:
        cache=e.EvidenceSearchCache(reader)
        with reader.transaction():result=e.search(reader,cfg,query='',cache=cache)
        expected=deepcopy(result)
        result['coverage']['instruction_text']['reason']='Poisoned result'
        result['rows'][0]['project_keys'].append('Poisoned project')
        result['rows'][0]['title']='Poisoned title'
        result['options']['kinds'].append('incident')
        with reader.transaction():assert e.search(reader,cfg,query='',cache=cache)==expected


@pytest.mark.parametrize('populated',[False,True])
def test_capacity_fallback_keeps_complete_answers_without_retaining_them(store,cfg,tmp_path,monkeypatch,populated):
    if populated:seed(store,tmp_path)
    with closing(Store(store.db_path,read_only=True)) as reader:
        with reader.transaction():expected=e.search(reader,cfg,query='')
        cache=e.EvidenceSearchCache(reader,max_bytes=1);calls=counted(monkeypatch)
        for _ in range(2):
            with reader.transaction():assert e.search(reader,cfg,query='',cache=cache)==expected
            assert cache.retained_bytes==0
        assert len(calls)==2


def test_cache_does_not_take_transaction_ownership_or_reuse_another_handle(store,cfg,tmp_path):
    seed(store,tmp_path)
    with closing(Store(store.db_path,read_only=True)) as reader, closing(Store(store.db_path,read_only=True)) as other:
        cache=e.EvidenceSearchCache(reader)
        e.search(reader,cfg,query='',cache=cache)
        assert not reader.conn.in_transaction and cache.retained_bytes==0
        with reader.transaction():
            e.search(reader,cfg,query='',cache=cache)
            assert reader.conn.in_transaction and cache.retained_bytes>0
        with other.transaction(),pytest.raises(e.EvidenceError,match='Store or connection changed'):
            e.search(other,cfg,query='',cache=cache)
        assert cache.retained_bytes==0
        connection=reader.conn
        try:
            reader.conn=other.conn
            with other.transaction(),pytest.raises(e.EvidenceError,match='Store or connection changed'):
                e.search(reader,cfg,query='',cache=cache)
        finally:reader.conn=connection
    cache=e.EvidenceSearchCache(store)
    store.update('learnings','id','l44',{'why':'Uncommitted caller content'})
    assert e.search(store,cfg,query='Uncommitted caller content',cache=cache)['pagination']['count']==1
    assert store.conn.in_transaction and cache.retained_bytes==0
    store.conn.rollback()
    assert e.search(store,cfg,query='Uncommitted caller content',cache=cache)['pagination']['count']==0


def test_cached_and_uncached_projection_builds_stay_inside_selected_store(store,cfg,tmp_path,monkeypatch):
    import builtins,io,os,sqlite3,subprocess
    from pathlib import Path
    seed(store,tmp_path)
    with closing(Store(store.db_path,read_only=True)) as reader:
        with reader.transaction():expected=e.search(reader,cfg,query='needle archive end')
        cache=e.EvidenceSearchCache(reader)
        def forbidden(*args,**kwargs):raise AssertionError('search cache escaped selected Store')
        with monkeypatch.context() as guard:
            for module,attribute in [(builtins,'open'),(io,'open'),(os,'open'),(Path,'open'),(sqlite3,'connect'),(subprocess,'Popen'),(os,'stat'),(os,'listdir'),(os,'scandir')]:
                guard.setattr(module,attribute,forbidden)
            for _ in range(2):
                with reader.transaction():assert e.search(reader,cfg,query='needle archive end',cache=cache)==expected
        assert reader.conn.total_changes==0
        assert reader.query_one('SELECT COUNT(*) n FROM llm_calls')['n']==0
