"""Versioned display-only families. No mining dedupe or execution authorization."""
from __future__ import annotations

import hashlib
import json
import math
import unicodedata
import uuid

from .embeddings import Embedder, EmbeddingError, cosine, parse_cached_vector
from .store import utc_now_iso

MIGRATION = '0032_rule_families'
PROFILE = 'display_families/1'
TABLES = ('rule_family_snapshots', 'rule_family_members', 'rule_family_heads')


class FamilyError(ValueError):
    """A display family input or retained record cannot be trusted."""


class FamilyRequestError(FamilyError):
    pass


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _text_hash(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _id(value):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, PROFILE + ':' + _hash(value)))


def _threshold(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not -1 <= value <= 1:
        raise FamilyError('display families: threshold must be a finite number between -1 and 1')
    return float(value)


def _config(cfg):
    return {'model': cfg.embedding_model, 'revision': cfg.embedding_revision,
            'expected_sha256': cfg.embedding_model_sha256,
            'threshold': _threshold(cfg.cluster_group_cosine)}


def _source(learnings):
    out = []
    seen = set()
    for row in learnings:
        lid = row.get('id')
        if not isinstance(lid, str) or not lid or lid in seen:
            raise FamilyError(f'learning {lid!r}: missing or duplicate identity')
        seen.add(lid)
        text, duplicate = row.get('rule_text'), row.get('duplicate_of')
        if not isinstance(text, str) or not isinstance(duplicate, str):
            raise FamilyError(f'learning {lid}: rule_text and duplicate_of must be strings')
        out.append({'id': lid, 'rule_text': text, 'duplicate_of': duplicate})
    return sorted(out, key=lambda row: row['id'])


def _inputs(store):
    return _source(store.query('SELECT id,rule_text,duplicate_of FROM learnings ORDER BY id'))


def _vector(raw, owner):
    try:
        vec = parse_cached_vector(_json(raw), owner)
        if not math.isfinite(cosine(vec, vec)):
            raise ValueError("vector norm overflows cosine arithmetic")
    except (EmbeddingError, ValueError, TypeError, OverflowError) as exc:
        raise FamilyError(f'learning {owner}: invalid family vector: {exc}') from exc
    return vec


def build_families(learnings, vectors, *, threshold):
    """Explicit/equality components, then deterministic complete-link joins."""
    rows = _source(learnings)
    threshold = _threshold(threshold)
    by_id = {row['id']: row for row in rows}
    if not isinstance(vectors, dict) or set(vectors) - set(by_id):
        raise FamilyError('display families: vectors must map known learning IDs')
    vecs = {lid: _vector(vec, lid) for lid, vec in vectors.items()}
    if len({len(vec) for vec in vecs.values()}) > 1:
        raise FamilyError('display families: incompatible vector dimensions for ' + ', '.join(sorted(vecs)))
    parent = {lid: lid for lid in by_id}
    edges, unresolved, empty = [], [], []
    equal = {}

    def root(lid):
        while parent[lid] != lid:
            parent[lid] = parent[parent[lid]]
            lid = parent[lid]
        return lid

    def union(a, b, basis):
        ra, rb = root(a), root(b)
        parent[max(ra, rb)] = min(ra, rb)
        edges.append((a, b, basis))

    for row in rows:
        lid, duplicate = row['id'], row['duplicate_of']
        if duplicate and duplicate in by_id and duplicate != lid:
            union(lid, duplicate, 'explicit')
        elif duplicate and duplicate not in by_id:
            unresolved.append(lid)
        normalized = ' '.join(unicodedata.normalize('NFC', row['rule_text']).split())
        if not normalized:
            empty.append(lid)
        elif normalized in equal:
            union(lid, equal[normalized], 'equal')
        else:
            equal[normalized] = lid
    components = {}
    for lid in by_id:
        components.setdefault(root(lid), []).append(lid)
    families, similarity_families = [], []
    ineligible = (set(by_id) - set(vecs)) | set(empty)
    for members in sorted(components.values(), key=lambda ids: ids[0]):
        basis = {basis for a, b, basis in edges if a in members and b in members}
        can_infer = ineligible.isdisjoint(members)
        # Explicit/equal groups remain visible even when inference is impossible.
        # In particular, the reader's no-vector fallback needs no pairwise scan.
        for family in similarity_families if can_infer else ():
            scores = [max(-1.0, min(1.0, cosine(vecs[a], vecs[b]))) for a in members for b in family['learning_ids']]
            if min(scores) < threshold:
                continue
            family['learning_ids'].extend(members)
            family['basis'].update(basis | {'similarity'})
            previous = family['minimum_inferred_cosine']
            family['minimum_inferred_cosine'] = min(scores + ([previous] if previous is not None else []))
            break
        else:
            family = {'learning_ids': list(members), 'basis': basis, 'minimum_inferred_cosine': None}
            families.append(family)
            if can_infer:
                similarity_families.append(family)
    for family in families:
        family['learning_ids'].sort()
        family['family_id'] = _id(family['learning_ids'])
        family['size'] = len(family['learning_ids'])
        family['basis'] = sorted(family['basis']) or ['singleton']
    return {'groups': families, 'members_total': len(rows), 'embedded_members': len(vecs),
            'missing_vector_ids': sorted(set(by_id) - set(vecs)),
            'empty_text_ids': empty, 'unresolved_duplicate_ids': unresolved}


def _schema(store, *, missing_ok=False):
    if not store.query_one('SELECT name FROM schema_migrations WHERE name=?', (MIGRATION,)):
        if missing_ok:
            return False
        raise FamilyError('Explicit migration required: ' + MIGRATION)
    columns = ('id,profile,generation,created_at,source_hash,config_hash,record_json,record_hash',
               'snapshot_id,learning_id,family_id,text_hash', 'profile,snapshot_id,selected_at')
    for table, selected in zip(TABLES, columns):
        try:
            store.query(f'SELECT {selected} FROM {table} LIMIT 0')
        except Exception as exc:
            raise FamilyError(f'{table}: applied family schema is damaged') from exc
    return True


def _members(record):
    hashes = {row['id']: row['text_hash'] for row in record['sources']}
    return sorted([{'learning_id': lid, 'family_id': group['family_id'], 'text_hash': hashes[lid]}
                   for group in record['groups'] for lid in group['learning_ids']], key=lambda row: row['learning_id'])


def family_snapshot(store, snapshot_id):
    """Validate the complete retained record and its query index; never write."""
    _schema(store)
    row = store.query_one('SELECT * FROM rule_family_snapshots WHERE id=?', (snapshot_id,))
    if row is None:
        raise FamilyRequestError(f'No retained family snapshot: {snapshot_id}')
    try:
        record = json.loads(row['record_json'])
        if not isinstance(record, dict) or _hash(record) != row['record_hash'] or _id(record) != row['id']:
            raise ValueError('content hash or snapshot identity mismatch')
        if record['profile'] != PROFILE or row['profile'] != PROFILE:
            raise ValueError('unsupported family profile')
        if record['source_hash'] != row['source_hash'] or _hash(record['config']) != row['config_hash']:
            raise ValueError('source/config binding mismatch')
        _threshold(record['config']['threshold'])
        sources = record['sources']
        lids = [source['id'] for source in sources]
        if lids != sorted(set(lids)) or record['members_total'] != len(lids):
            raise ValueError('source population mismatch')
        assigned = []
        for group in record['groups']:
            ids = group['learning_ids']
            if not ids or ids != sorted(set(ids)) or group['size'] != len(ids) or group['family_id'] != _id(ids):
                raise ValueError('family identity/member mismatch')
            basis = group['basis']
            if not basis or basis != sorted(set(basis)) or set(basis) - {'explicit','equal','similarity','singleton'}:
                raise ValueError('invalid family basis')
            score = group['minimum_inferred_cosine']
            if 'similarity' in basis:
                if _threshold(score) < record['config']['threshold']:
                    raise ValueError('inferred family below threshold')
            elif score is not None:
                raise ValueError('non-inferred family has a similarity score')
            assigned.extend(ids)
        if sorted(assigned) != lids:
            raise ValueError('families do not partition source population')
        if set(record['vector_hashes']) | set(record['missing_vector_ids']) != set(lids) or set(record['vector_hashes']) & set(record['missing_vector_ids']):
            raise ValueError('vector coverage does not partition sources')
        if record['embedded_members'] != len(record['vector_hashes']):
            raise ValueError('embedding count mismatch')
        for key in ('missing_vector_ids', 'empty_text_ids', 'unresolved_duplicate_ids'):
            if record[key] != sorted(set(record[key])) or set(record[key]) - set(lids):
                raise ValueError(f'invalid {key}')
        expected_status = 'partial' if record['missing_vector_ids'] or record['empty_text_ids'] or record['error'] else 'complete'
        if record['status'] != expected_status:
            raise ValueError('coverage status mismatch')
        got = store.query('SELECT learning_id,family_id,text_hash FROM rule_family_members WHERE snapshot_id=? ORDER BY learning_id', (snapshot_id,))
        if got != _members(record):
            raise ValueError('membership index differs from snapshot')
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise FamilyError(f'family snapshot {snapshot_id}: {exc}') from exc
    return {**record, 'snapshot_id': row['id'], 'created_at': row['created_at'], 'generation': row['generation']}


def _latest(store, config_hash=None, source_hash=None):
    head = store.query_one('SELECT * FROM rule_family_heads WHERE profile=?', (PROFILE,))
    if head is None:
        if store.query_one('SELECT id FROM rule_family_snapshots LIMIT 1'):
            raise FamilyError('display families: retained snapshots have no current selection')
        return None
    record = family_snapshot(store, head['snapshot_id'])
    if config_hash is not None and _hash(record['config']) != config_hash:
        return None
    if source_hash is not None and record['source_hash'] != source_hash:
        return None
    return record


def _cache_vectors(store, rows, model_key):
    vectors = {}
    for start in range(0, len(rows), 128):
        batch = {row['id']: row for row in rows[start:start + 128]}
        marks = ','.join('?' for _ in batch)
        cached = store.query("SELECT owner_key,text_sha,vector_json FROM embeddings WHERE owner_kind='learning' AND model=? AND owner_key IN (" + marks + ')', (model_key, *batch))
        for row in cached:
            text = batch[row['owner_key']]['rule_text']
            if not text.strip() or row['text_sha'] != hashlib.sha1(text.encode()).hexdigest():
                continue
            try:
                vectors[row['owner_key']] = _vector(json.loads(row['vector_json']), row['owner_key'])
            except (ValueError, TypeError) as exc:
                raise FamilyError(f'learning {row["owner_key"]}: invalid cached family vector: {exc}') from exc
    return vectors


def collect_families(store, cfg, *, embedder=None, cache_only=False):
    """Publish exact local vectors and membership; never commit another writer."""
    if store.read_only or store.conn.in_transaction:
        raise FamilyError('Family collection requires an idle writable Store')
    _schema(store)
    config = _config(cfg)
    rows = _inputs(store)
    source_hash, config_hash = _hash(rows), _hash(config)
    vectors, model, error = {}, None, ''
    cache_rows = []
    if any(row['rule_text'].strip() for row in rows):
        if cache_only:
            prior = _latest(store, config_hash=config_hash)
            model = prior['model'] if prior else None
            if model is None:
                error = 'cache_only_no_recorded_model'
        else:
            try:
                encoder = embedder if embedder is not None else Embedder(cfg, store=None)
                model = {'cache_key': encoder.cache_key, 'provenance': encoder.provenance}
                if not isinstance(model['cache_key'], str) or not model['cache_key'].startswith('model2vec-v1:') or model['provenance'].get('cache_key') != model['cache_key']:
                    raise EmbeddingError('embedding identity is not an exact model2vec cache key')
            except (EmbeddingError, ValueError, OSError, AttributeError) as exc:
                model = None
                error = f'model_unavailable: {type(exc).__name__}: {exc}'
        if model is not None:
            vectors = _cache_vectors(store, rows, model['cache_key'])
            missing = [row for row in rows if row['rule_text'].strip() and row['id'] not in vectors]
            if cache_only and missing:
                error = 'cache_only_missing_vectors'
            elif not cache_only:
                for start in range(0, len(missing), 64):
                    batch = missing[start:start + 64]
                    try:
                        encoded = encoder.encode([row['rule_text'] for row in batch])
                    except (EmbeddingError, ValueError, OSError) as exc:
                        error = f'encoding_failed: {type(exc).__name__}: {exc}'
                        break
                    if not isinstance(encoded, list) or len(encoded) != len(batch):
                        raise FamilyError('display families: encoder returned the wrong vector population')
                    for row, raw in zip(batch, encoded):
                        vec = _vector(raw, row['id'])
                        vectors[row['id']] = vec
                        cache_rows.append((row, vec))
    built = build_families(rows, vectors, threshold=config['threshold'])
    record = {**built, 'profile': PROFILE, 'source_hash': source_hash, 'config': config,
              'sources': [{'id': row['id'], 'text_hash': _text_hash(row['rule_text']), 'duplicate_of': row['duplicate_of']} for row in rows],
              'model': model, 'vector_hashes': {lid: _hash(vec) for lid, vec in vectors.items()},
              'error': error, 'status': 'partial' if built['missing_vector_ids'] or built['empty_text_ids'] or error else 'complete'}
    sid = _id(record)
    with store.transaction(write=True):
        if _hash(_inputs(store)) != source_hash or _config(cfg) != config:
            raise FamilyError('Learning source or grouping config changed during family collection; retry')
        for row, vec in cache_rows:
            store.conn.execute("INSERT INTO embeddings (owner_kind,owner_key,model,text_sha,vector_json,created_at) VALUES ('learning',?,?,?,?,?) ON CONFLICT(owner_kind,owner_key,model) DO UPDATE SET text_sha=excluded.text_sha,vector_json=excluded.vector_json,created_at=excluded.created_at",
                               (row['id'], model['cache_key'], hashlib.sha1(row['rule_text'].encode()).hexdigest(), _json(vec), utc_now_iso()))
        existing = store.query_one('SELECT id FROM rule_family_snapshots WHERE id=?', (sid,))
        if existing is None:
            generation = store.query_one('SELECT COALESCE(MAX(generation),0)+1 n FROM rule_family_snapshots')['n']
            store.insert('rule_family_snapshots', {'id': sid, 'profile': PROFILE, 'generation': generation,
                         'created_at': utc_now_iso(), 'source_hash': source_hash, 'config_hash': config_hash,
                         'record_json': _json(record), 'record_hash': _hash(record)})
            for row in _members(record):
                store.insert('rule_family_members', {'snapshot_id': sid, **row})
        result = family_snapshot(store, sid)
        store.conn.execute('INSERT INTO rule_family_heads (profile,snapshot_id,selected_at) VALUES (?,?,?) '
                           'ON CONFLICT(profile) DO UPDATE SET snapshot_id=excluded.snapshot_id,selected_at=excluded.selected_at',
                           (PROFILE, sid, utc_now_iso()))
    attempted = int(not cache_only and any(row['rule_text'].strip() for row in rows))
    return {**{key: result[key] for key in ('snapshot_id','created_at','status','error','members_total','embedded_members')},
            'inference_attempted': attempted, 'inference_succeeded': int(bool(attempted and not error)),
            'inference_failed': int(bool(attempted and error)), 'cache_only': cache_only}


def current_families(store, cfg):
    """Read current compatible membership, or explicit-only groups with a reason."""
    rows, config = _inputs(store), _config(cfg)
    if not _schema(store, missing_ok=True):
        reason, latest = 'migration_required', None
    else:
        current = _latest(store, config_hash=_hash(config), source_hash=_hash(rows))
        if current is not None:
            return {**current, 'reason': current['error'], 'stale_source': False, 'stale_config': False,
                    'model_freshness': 'recorded; model assets are not rechecked by readers'}
        latest = _latest(store)
        reason = 'stale_snapshot' if latest else 'not_collected'
    return {**build_families(rows, {}, threshold=config['threshold']), 'profile': PROFILE,
            'status': 'unavailable', 'reason': reason, 'snapshot_id': None, 'model': None,
            'config': config, 'source_hash': _hash(rows), 'stale_source': bool(latest and latest['source_hash'] != _hash(rows)),
            'stale_config': bool(latest and latest['config'] != config),
            'model_freshness': 'unknown; no current retained model binding'}
