"""Display grouping uses invented rows/vectors and disposable Stores only."""
from contextlib import closing
from dataclasses import replace
import hashlib
import json
import math

import pytest

from self_improve.config import Config
from self_improve import rule_families as rf
from self_improve.store import Store


def learning(lid, text=None, duplicate=''):
    return {'id': lid, 'rule_text': text if text is not None else 'Instruction ' + lid,
            'duplicate_of': duplicate}


class Encoder:
    def __init__(self, version='one', encode=None):
        self.cache_key = 'model2vec-v1:' + hashlib.sha256(version.encode()).hexdigest()
        self.provenance = {'cache_key': self.cache_key, 'source': {'kind': 'synthetic'}, 'version': version}
        self.calls = []
        self._encode = encode

    def encode(self, texts):
        self.calls.append(texts)
        return self._encode(texts) if self._encode else [[1.0, 0.0] for text in texts]

    def cached_vector(self, *args):
        return self.encode([args[-1]])[0]


@pytest.fixture
def db(tmp_path):
    with closing(Store(tmp_path / 'state.sqlite')) as store:
        yield store


def seed(store, rows):
    for row in rows:
        store.insert('learnings', {**row, 'created_at': '2030-01-01T00:00:00Z'})
    store.commit()


def groups(result):
    return [row['learning_ids'] for row in result['groups']]


def current(store, cfg=None):
    with store.transaction():
        return rf.current_families(store, cfg or Config())


def test_complete_link_prevents_similarity_chains_and_is_order_independent():
    rows = [learning(lid) for lid in 'abc']
    vecs = {lid: [math.cos(angle), math.sin(angle)] for lid, angle in zip('abc', [0, .5, 1])}
    first = rf.build_families(rows, vecs, threshold=.8)
    assert groups(first) == [['a', 'b'], ['c']]
    assert rf.build_families(list(reversed(rows)), dict(reversed(list(vecs.items()))), threshold=.8) == first
    assert first['groups'][0]['minimum_inferred_cosine'] == pytest.approx(math.cos(.5))
    assert first['groups'][0]['basis'] == ['similarity']


def test_explicit_parent_transitive_links_and_equality_precede_inference():
    rows = [learning('a', 'Keep  output\nvisible'), learning('b', duplicate='a'),
            learning('c', duplicate='b'), learning('d', 'Keep output visible'),
            learning('e', 'keep output visible'), learning('f', duplicate='external rule text')]
    result = rf.build_families(rows, {}, threshold=.8)
    assert groups(result) == [['a', 'b', 'c', 'd'], ['e'], ['f']]
    assert result['groups'][0]['basis'] == ['equal', 'explicit']
    assert result['unresolved_duplicate_ids'] == ['f']
    assert result['missing_vector_ids'] == list('abcdef')


def test_inferred_join_checks_every_member_of_an_explicit_component():
    rows = [learning('a'), learning('b', duplicate='a'), learning('c')]
    result = rf.build_families(rows, {'a': [1, 0], 'b': [0, 1], 'c': [1, 0]}, threshold=.8)
    assert groups(result) == [['a', 'b'], ['c']]
    assert result['groups'][0]['minimum_inferred_cosine'] is None


def test_singletons_empty_text_and_missing_vectors_remain_distinct():
    rows = [learning('a', ''), learning('b', '  '), learning('c'), learning('d'), learning('e')]
    result = rf.build_families(rows, {'c': [1, 0], 'd': [1, 0]}, threshold=.8)
    assert groups(result) == [['a'], ['b'], ['c', 'd'], ['e']]
    assert result['empty_text_ids'] == ['a', 'b']
    assert result['missing_vector_ids'] == ['a', 'b', 'e']
    assert rf.build_families([], {}, threshold=.8)['groups'] == []


def test_incomplete_components_do_not_hide_later_complete_link_candidates():
    rows = [learning('a'), learning('b', duplicate='a'), learning('c'),
            learning('d', ''), learning('e'), learning('f', 'Same text'),
            learning('g', 'Same text'), learning('z')]
    vectors = {lid: [1, 0] for lid in ['a', 'c', 'd', 'e', 'g']}
    vectors['z'] = [0, 1]
    result = rf.build_families(rows, vectors, threshold=.8)
    assert groups(result) == [['a', 'b'], ['c', 'e'], ['d'], ['f', 'g'], ['z']]
    assert [group['basis'] for group in result['groups']] == [
        ['explicit'], ['similarity'], ['singleton'], ['equal'], ['singleton']]
    assert result['groups'][1]['minimum_inferred_cosine'] == 1
    assert result['missing_vector_ids'] == ['b', 'f']
    assert result['empty_text_ids'] == ['d']
    assert rf.build_families(list(reversed(rows)), dict(reversed(list(vectors.items()))), threshold=.8) == result


@pytest.mark.parametrize('vector', [[], [0, 0], [True, 1], [float('nan'), 1], [float('inf'), 1], '[]', {'x': 1}])
def test_invalid_vectors_fail_with_owner(vector):
    with pytest.raises(rf.FamilyError, match='learning a'):
        rf.build_families([learning('a')], {'a': vector}, threshold=.8)


@pytest.mark.parametrize('threshold', [True, float('nan'), -1.01, 1.01, '0.8'])
def test_invalid_threshold_is_not_silently_coerced(threshold):
    with pytest.raises(rf.FamilyError, match='threshold'):
        rf.build_families([], {}, threshold=threshold)


def test_duplicate_ids_unknown_vectors_and_different_dimensions_fail():
    with pytest.raises(rf.FamilyError, match='duplicate'):
        rf.build_families([learning('a'), learning('a')], {}, threshold=.8)
    with pytest.raises(rf.FamilyError, match='known learning'):
        rf.build_families([], {'a': [1]}, threshold=.8)
    with pytest.raises(rf.FamilyError, match='dimensions.*a, b'):
        rf.build_families([learning('a'), learning('b')], {'a': [1], 'b': [1, 0]}, threshold=.8)


def test_real_collection_cache_replay_and_read_only_reader(db, monkeypatch):
    seed(db, [learning('a'), learning('b')])
    encoder = Encoder()
    first = rf.collect_families(db, Config(), embedder=encoder)
    assert first['status'] == 'complete' and first['members_total'] == first['embedded_members'] == 2
    assert len(encoder.calls) == 1
    assert rf.collect_families(db, Config(), embedder=encoder) == first
    assert len(encoder.calls) == 1 and not db.conn.in_transaction
    assert db.query_one('SELECT COUNT(*) n FROM rule_family_snapshots')['n'] == 1
    assert db.query_one('SELECT COUNT(*) n FROM rule_family_members')['n'] == 2
    def forbidden(*a, **kw):
        raise AssertionError('reader or dry run tried to resolve a model')
    monkeypatch.setattr(rf, 'Embedder', forbidden)
    assert rf.collect_families(db, Config(), cache_only=True)['snapshot_id'] == first['snapshot_id']
    with closing(Store(db.db_path, read_only=True)) as ro:
        before = ro.conn.total_changes
        result = current(ro)
        assert result['snapshot_id'] == first['snapshot_id'] and groups(result) == [['a', 'b']]
        assert ro.conn.total_changes == before == 0
    assert db.query_one('SELECT COUNT(*) n FROM llm_calls')['n'] == 0


def test_source_and_config_drift_show_current_explicit_only_membership(db):
    seed(db, [learning('a'), learning('b')])
    old = rf.collect_families(db, Config(), embedder=Encoder())
    db.update('learnings', 'id', 'a', {'rule_text': 'Changed content'})
    db.commit()
    stale = current(db)
    assert stale['status'] == 'unavailable' and stale['stale_source']
    assert groups(stale) == [['a'], ['b']]
    new = rf.collect_families(db, Config(), embedder=Encoder())
    assert new['snapshot_id'] != old['snapshot_id']
    changed = replace(Config(), cluster_group_cosine=.9)
    assert current(db, changed)['stale_config']
    assert rf.family_snapshot(db, old['snapshot_id'])['sources'][0]['text_hash'] != rf.family_snapshot(db, new['snapshot_id'])['sources'][0]['text_hash']


def test_changed_model_identity_uses_new_vectors_and_new_snapshot(db):
    seed(db, [learning('a'), learning('b')])
    one = rf.collect_families(db, Config(), embedder=Encoder())
    other = Encoder('two', lambda texts: [[1, 0], [0, 1]])
    two = rf.collect_families(db, Config(), embedder=other)
    assert one['snapshot_id'] != two['snapshot_id'] and len(other.calls) == 1
    assert groups(current(db)) == [['a'], ['b']]
    assert current(db)['model']['cache_key'] == other.cache_key


def test_stale_and_legacy_cache_vectors_cannot_group_new_content(db):
    seed(db, [learning('a'), learning('b')])
    rf.collect_families(db, Config(), embedder=Encoder())
    db.update('learnings', 'id', 'a', {'rule_text': 'New text'})
    db.conn.execute("UPDATE embeddings SET model=? WHERE owner_key='b'", (Config().embedding_model,))
    db.commit()
    result = rf.collect_families(db, Config(), cache_only=True)
    assert result['status'] == 'partial' and result['embedded_members'] == 0
    assert groups(current(db)) == [['a'], ['b']]
    assert current(db)['missing_vector_ids'] == ['a', 'b']


def test_empty_success_is_different_from_no_collection_and_missing_model(db, monkeypatch):
    assert current(db)['reason'] == 'not_collected'
    monkeypatch.setattr(rf, 'Embedder', lambda cfg: pytest.fail('empty population loaded model'))
    result = rf.collect_families(db, Config())
    assert result['status'] == 'complete' and result['members_total'] == 0
    seed(db, [learning('a')])
    result = rf.collect_families(db, Config(), cache_only=True)
    assert result['status'] == 'partial' and result['error'] == 'cache_only_no_recorded_model'


def test_encoding_failure_keeps_complete_earlier_batch_and_explicit_groups(db):
    seed(db, [learning(f'{i:03}') for i in range(70)] + [learning('equal-a', 'same'), learning('equal-b', 'same')])
    def encode(texts):
        if texts[0] == 'Instruction 064':
            raise rf.EmbeddingError('synthetic failed batch')
        return [[1, 0] for _ in texts]
    encoder = Encoder(encode=encode)
    result = rf.collect_families(db, Config(), embedder=encoder)
    assert [len(batch) for batch in encoder.calls] == [64, 8]
    assert result['status'] == 'partial' and result['embedded_members'] == 64
    assert 'synthetic failed batch' in result['error']
    assert ['equal-a', 'equal-b'] in groups(current(db))


def test_collector_refuses_caller_work_and_rolls_back_its_own_failed_publication(db, monkeypatch):
    seed(db, [learning('a')])
    db.update('learnings', 'id', 'a', {'why': 'pending caller work'})
    with pytest.raises(rf.FamilyError, match='idle writable'):
        rf.collect_families(db, Config(), embedder=Encoder())
    assert db.conn.in_transaction
    db.conn.rollback()
    original = db.insert
    def fail(table, row):
        if table == 'rule_family_members':
            raise RuntimeError('publication interruption')
        return original(table, row)
    monkeypatch.setattr(db, 'insert', fail)
    with pytest.raises(RuntimeError, match='publication interruption'):
        rf.collect_families(db, Config(), embedder=Encoder())
    assert not db.conn.in_transaction
    for table in (*rf.TABLES, 'embeddings'):
        assert db.query_one(f'SELECT COUNT(*) n FROM {table}')['n'] == 0


def test_source_change_during_encoding_is_rechecked_before_any_cache_write(db):
    seed(db, [learning('a')])
    def encode(texts):
        with closing(Store(db.db_path, migrate=False)) as second:
            second.update('learnings', 'id', 'a', {'rule_text': 'concurrent correction'})
            second.commit()
        return [[1, 0]]
    with pytest.raises(rf.FamilyError, match='changed during'):
        rf.collect_families(db, Config(), embedder=Encoder(encode=encode))
    assert db.query_one('SELECT COUNT(*) n FROM embeddings')['n'] == 0
    assert db.query_one('SELECT COUNT(*) n FROM rule_family_snapshots')['n'] == 0
    assert db.query_one("SELECT rule_text FROM learnings WHERE id='a'")['rule_text'] == 'concurrent correction'


def test_corrupt_cached_vector_fails_without_replacing_it(db):
    seed(db, [learning('a')])
    encoder = Encoder()
    rf.collect_families(db, Config(), embedder=encoder)
    db.conn.execute("UPDATE embeddings SET vector_json='[0,0]'")
    db.commit()
    with pytest.raises(rf.FamilyError, match='learning a'):
        rf.collect_families(db, Config(), embedder=encoder)
    assert len(encoder.calls) == 1


def test_member_index_and_record_corruption_are_errors_not_empty_groups(db):
    seed(db, [learning('a')])
    sid = rf.collect_families(db, Config(), embedder=Encoder())['snapshot_id']
    db.conn.execute("UPDATE rule_family_members SET family_id='wrong'")
    db.commit()
    with pytest.raises(rf.FamilyError, match='membership index'):
        current(db)
    with pytest.raises(rf.FamilyError, match='membership index'):
        rf.collect_families(db, Config(), embedder=Encoder())
    db.conn.execute("UPDATE rule_family_snapshots SET record_json='[]'")
    db.commit()
    with pytest.raises(rf.FamilyError, match='content hash'):
        rf.family_snapshot(db, sid)


def test_missing_migration_is_distinct_from_damaged_applied_schema(db):
    db.conn.execute('DROP TABLE rule_family_members')
    db.commit()
    with pytest.raises(rf.FamilyError, match='applied family schema'):
        current(db)
    db.conn.execute('DELETE FROM schema_migrations WHERE name=?', (rf.MIGRATION,))
    db.commit()
    assert current(db)['reason'] == 'migration_required'
    with pytest.raises(rf.FamilyError, match='Explicit migration'):
        rf.collect_families(db, Config(), cache_only=True)


def test_rebuild_exports_and_preserves_family_archive_without_learning_fk(db, tmp_path):
    from self_improve.rebuild import rebuild_state
    seed(db, [learning('a'), learning('b')])
    sid = rf.collect_families(db, Config(), embedder=Encoder())['snapshot_id']
    before = rf.family_snapshot(db, sid)
    rebuild_state(db, export_path=tmp_path / 'private-backup')
    assert db.query_one('SELECT COUNT(*) n FROM learnings')['n'] == 0
    assert rf.family_snapshot(db, sid) == before
    assert current(db)['stale_source']
    backup = json.loads((tmp_path / 'private-backup' / 'preserved.json').read_text())
    assert backup['rule_families']['rule_family_snapshots'][0]['id'] == sid
    assert len(backup['rule_families']['rule_family_members']) == 2


def test_pipeline_publishes_post_mining_membership_and_dry_run_never_resolves_model(tmp_path, monkeypatch):
    from tests.e2e_corpus import build_corpus, ScriptedLLM, mine_payload
    from self_improve.pipeline import run_pipeline
    corpus = build_corpus(tmp_path)
    llm = ScriptedLLM(mine_responses=[mine_payload('Preserve a failing command output before retrying.') for _ in range(8)])
    encoder = Encoder()
    try:
        stats = run_pipeline(corpus.cfg, corpus.store, review_only=True,
                             _llm_factory=llm.factory(), _embedder_factory=lambda cfg, store: encoder)
        record = current(corpus.store, corpus.cfg)
        assert llm.sandboxes and record['members_total'] > 0
        assert stats['rule_families']['snapshot_id'] == record['snapshot_id']
        assert {row['id'] for row in record['sources']} == {row['id'] for row in corpus.store.query('SELECT id FROM learnings')}
        assert record['status'] == 'complete'
        run = corpus.store.query_one('SELECT stats_json FROM runs ORDER BY started DESC LIMIT 1')
        assert json.loads(run['stats_json'])['rule_families']['snapshot_id'] == record['snapshot_id']
        monkeypatch.setattr(rf, 'Embedder', lambda cfg: pytest.fail('dry-run resolved model'))
        dry = run_pipeline(corpus.cfg, corpus.store, dry_run=True)
        assert dry['rule_families']['snapshot_id'] == record['snapshot_id']
    finally:
        corpus.store.close()


def test_default_embedder_constructor_uses_encode_only_mode(db, monkeypatch):
    from types import SimpleNamespace
    from self_improve import embeddings
    seed(db, [learning('a')])
    encoder = Encoder()
    monkeypatch.setattr(embeddings, 'resolve_model', lambda *a: SimpleNamespace(cache_key=encoder.cache_key, provenance=encoder.provenance))
    modes = []
    def load(instance):
        modes.append(instance.store)
        return encoder
    monkeypatch.setattr(embeddings.Embedder, '_load', load)
    assert rf.collect_families(db, Config())['status'] == 'complete'
    assert modes == [None]


def test_replay_recovers_current_selection_after_failure_and_model_rollback(db):
    seed(db, [learning('a'), learning('b')])
    encoder = Encoder()
    first = rf.collect_families(db, Config(), embedder=encoder)
    class Broken:
        @property
        def cache_key(self):
            raise rf.EmbeddingError('temporary model failure')
    failed = rf.collect_families(db, Config(), embedder=Broken())
    assert current(db)['snapshot_id'] == failed['snapshot_id']
    assert rf.collect_families(db, Config(), embedder=encoder) == first
    assert current(db)['snapshot_id'] == first['snapshot_id']
    other = rf.collect_families(db, Config(), embedder=Encoder('two', lambda texts: [[1,0],[0,1]]))
    assert groups(current(db)) == [['a'], ['b']]
    assert other['snapshot_id'] != first['snapshot_id']
    assert rf.collect_families(db, Config(), embedder=encoder) == first
    assert groups(current(db)) == [['a','b']]
    assert current(db)['snapshot_id'] == first['snapshot_id']


def test_computed_roundoff_is_clamped_and_overflow_is_refused(db):
    a = [.7316242066268128,.5764279589751974,.7812095675548225,.6615809723574216,.839298017459614,.43939451090100756,.1550494207938402,.149835057407401]
    b = [x * 1.01 for x in a]
    seed(db, [learning('a'), learning('b')])
    assert rf.collect_families(db, Config(), embedder=Encoder(encode=lambda texts: [a,b]))['status'] == 'complete'
    assert current(db)['groups'][0]['minimum_inferred_cosine'] == 1
    with pytest.raises(rf.FamilyError, match='learning a'):
        rf.build_families([learning('a'),learning('b')], {'a':[1e200,0],'b':[-1e200,0]}, threshold=.8)


def test_inference_failure_degrades_health_but_cache_only_gap_does_not(db, tmp_path):
    from self_improve.pipeline import derive_run_status
    from self_improve.report import generate
    seed(db, [learning('a')])
    def fail(texts):
        raise rf.EmbeddingError('controlled inference failure')
    failed = rf.collect_families(db, Config(), embedder=Encoder(encode=fail))
    status, reasons = derive_run_status({'rule_families':failed})
    assert status == 'degraded' and reasons == ['rule_families: 0 of 1 attempts succeeded (1 failed)']
    dry = rf.collect_families(db, Config(), cache_only=True)
    assert derive_run_status({'rule_families':dry}) == ('ok', [])
    assert dry['error'] == 'cache_only_missing_vectors'
    db.insert('runs', {'id':'family-failure-run', 'started':'2030-01-01T00:00:00Z', 'status':status, 'stats_json':json.dumps({'rule_families':failed})})
    db.commit()
    report = tmp_path / 'report.md'
    generate(db, Config(), 'family-failure-run', report)
    text = report.read_text()
    assert '## Display family coverage' in text
    assert '0 of 1 learning members' in text and 'controlled inference failure' in text
