"""Full redacted text reaches retention through real, temporary surface reads."""
import json
import sqlite3
from pathlib import Path

import pytest

from self_improve import instruction_text as text_archive
from self_improve import instruction_inventory as inventory
from self_improve import rule_availability as availability
from self_improve.mining_history import digest, encoded
from self_improve.redact import redact_text
from self_improve.rule_revisions import text_hash
from self_improve.store import Store
from tests.test_rule_availability import cfg, store, known_copy, delivery, init_git_repo, run_git

AT = '2030-01-01T00:00:00Z'


def collect(store, cfg, *, at=AT, run_id=''):
    return availability.collect_availability(store, cfg, observed_at=at, run_id=run_id)


def archives(store):
    return [text_archive.read_archive(store, row['id']) for row in store.query(
        'SELECT id FROM instruction_text_archives ORDER BY observed_at,id')]


def test_actual_collector_reuses_surface_and_retains_complete_redacted_text(cfg, store, tmp_path, monkeypatch):
    original = '# Café\n' + 'Retained fixture line.\n' * 600 + 'token = ghp_' + 'Ab1Cd2Ef3Gh4Ij5Kl6Mn7Op8Qr9St0Uv1Wx2' + '\nNeedle at end.\n'
    repo = init_git_repo(tmp_path/'project', 'AGENTS.md', original)
    key = known_copy(store, repo)
    inspect = availability.inspect_surfaces
    calls = []
    def read_once(*args, **kwargs):
        result = inspect(*args, **kwargs)
        calls.append(result)
        # The archive must use the same read, even when disk changes before save.
        (repo/'AGENTS.md').write_text('Changed after the observed read.\n')
        return result
    monkeypatch.setattr(availability, 'inspect_surfaces', read_once)
    collected = collect(store, cfg, run_id='fixture-run')
    assert len(calls) == 1
    record = archives(store)[0]
    assert record['inventory_id'] == collected['inventory_ids'][0]
    assert record['project_key'] == key and record['run_id'] == 'fixture-run'
    file = record['files'][0]
    assert file['text'] == redact_text(original) and file['text'].endswith('Needle at end.\n')
    assert 'ghp_Ab1' not in encoded(record)
    assert file['content_hash'] == text_hash(original)
    assert file['redacted_hash'] == text_hash(file['text'])
    assert file['bytes'] == len(original.encode()) and file['lines'] == len(original.splitlines())
    assert file['redacted_bytes'] == len(file['text'].encode()) < file['bytes']
    assert file['redacted_lines'] == len(file['text'].splitlines())
    assert file['loading_paths'][0]['runtime_loading_verified'] is False
    assert record['coverage']['runtime_loading_verified'] is False
    assert text_archive.current_archives(store)['records'][0]['archive']['id'] == record['id']
    assert store.query_one('SELECT COUNT(*) n FROM llm_calls')['n'] == 0


def test_scopes_aliases_limits_and_partial_coverage_follow_exact_inventory(cfg, store, tmp_path):
    repo = init_git_repo(tmp_path/'project', 'CLAUDE.md', '@shared.md\n@alias.md\n@missing.md\n')
    (repo/'shared.md').write_text('Complete shared fixture.\n')
    (repo/'alias.md').symlink_to(repo/'shared.md')
    rules = repo/'.claude/rules'; rules.mkdir(parents=True)
    (rules/'scoped.md').write_text('---\npaths: ["src/**"]\n---\nScoped fixture.\n')
    (rules/'large.md').write_text('x' * ((1 << 20) + 1))
    known_copy(store, repo); collect(store, cfg)
    record = archives(store)[0]
    inv = inventory.project_inventory(store, project_key=record['project_key'])['records'][0]
    assert record['status'] == 'partial'
    assert record['coverage']['issues'] == inv['issues']
    assert record['coverage']['limits'] == inv['limits']
    assert any(issue['cause'] == 'file_byte_limit' for issue in record['coverage']['issues'])
    assert 'session_receipts' in record['coverage']['unobserved_sources']
    shared = [f for f in record['files'] if f['real_path'] == str(repo/'shared.md')]
    assert len(shared) == 1 and len(shared[0]['aliases']) == 2
    assert {p['scope']['kind'] for f in record['files'] for p in f['loading_paths']} == {'project_always_loaded','path_scoped'}
    assert text_archive.current_archives(store)['records'][0]['status'] == 'partial'


def test_atomic_failure_and_exact_replay(cfg, store, tmp_path, monkeypatch):
    repo = init_git_repo(tmp_path/'project', 'AGENTS.md', '# Fixture\n')
    known_copy(store, repo); delivery(store, cfg, repo)
    insert = store.insert
    def fail(table, row):
        if table == text_archive.TABLE: raise RuntimeError('invented text publication failure')
        return insert(table, row)
    monkeypatch.setattr(store, 'insert', fail)
    with pytest.raises(RuntimeError, match='text publication failure'): collect(store, cfg)
    for table in ('rule_revisions','rule_availability_observations','rule_availability_collections','instruction_inventories',text_archive.TABLE):
        assert store.query_one(f'SELECT COUNT(*) n FROM {table}')['n'] == 0
    monkeypatch.setattr(store, 'insert', insert)
    first = collect(store, cfg); saved = archives(store)
    assert collect(store, cfg) == first and archives(store) == saved
    with pytest.raises(text_archive.TextArchiveError, match='transaction'):
        text_archive.record_archives(store, saved)
    with store.transaction(write=True): text_archive.record_archives(store, saved)
    assert archives(store) == saved


def test_missing_empty_metadata_only_and_changed_identity_are_distinct(cfg, store, tmp_path):
    assert text_archive.current_archives(store)['reason'] == 'no_inventory_observations'
    repo = init_git_repo(tmp_path/'project', 'README.md', 'No instructions.\n')
    known_copy(store, repo); collect(store, cfg)
    first = text_archive.current_archives(store)['records'][0]
    assert first['status'] == 'recorded' and first['archive']['files'] == []
    collect(store, cfg, at='2030-01-02T00:00:00Z')
    newer = archives(store)[-1]
    store.conn.execute('DELETE FROM instruction_text_archives WHERE id=?', (newer['id'],)); store.commit()
    latest = text_archive.current_archives(store)['records'][0]
    assert latest['status'] == 'metadata_only' and latest['archive'] is None
    assert text_archive.read_archive(store, first['archive']['id']) == first['archive']
    run_git(['remote','add','origin','https://example.test/changed/project.git'],repo)
    collect(store, cfg, at='2030-01-03T00:00:00Z')
    latest = text_archive.current_archives(store)['records'][0]
    assert latest['status'] == 'partial'
    assert latest['archive']['coverage']['issues'][0]['cause'] == 'working_copy_identity_changed'
    store.conn.execute('DELETE FROM schema_migrations WHERE name=?', (text_archive.MIGRATION,));store.commit()
    assert text_archive.current_archives(store)['reason'] == 'schema_unavailable'
    assert not store.query_one('SELECT name FROM schema_migrations WHERE name=?',(text_archive.MIGRATION,))


def test_same_project_copies_and_conflicting_simultaneous_observations(cfg, store, tmp_path):
    for name in ('a','b'):
        repo = init_git_repo(tmp_path/name, 'AGENTS.md', f'Fixture {name}.\n')
        run_git(['remote','add','origin','https://example.test/owner/project.git'],repo)
        known_copy(store, repo)
    collect(store, cfg)
    result = text_archive.current_archives(store)
    assert result['count'] == 2
    assert len({r['project_key'] for r in result['records']}) == 1
    assert len({r['working_copy_id'] for r in result['records']}) == 2
    collect(store, cfg, run_id='same-time-same-content')
    assert all(r['status'] == 'recorded' for r in text_archive.current_archives(store)['records'])
    (repo/'AGENTS.md').write_text('Conflicting content at same time.\n'); collect(store, cfg)
    conflict = [r for r in text_archive.current_archives(store)['records'] if r['status'] == 'conflicting']
    assert len(conflict) == 1 and conflict[0]['archive'] is None
    assert len(conflict[0]['inventory_ids']) == 3


@pytest.mark.parametrize('mutation', ['json','duplicate_keys','hash','index','text','binding','inventory','profile'])
def test_corruption_is_named_even_after_rehash(cfg, store, tmp_path, mutation):
    repo = init_git_repo(tmp_path/'project', 'AGENTS.md', 'Retained text.\n'); known_copy(store, repo); collect(store, cfg)
    row = store.query_one('SELECT * FROM instruction_text_archives'); record = json.loads(row['record_json'])
    if mutation == 'json': row['record_json'] = '[]'
    elif mutation == 'duplicate_keys': row['record_json'] = row['record_json'][:-1] + ',"profile":"instruction-text/1"}'
    elif mutation == 'hash': row['record_hash'] = 'changed'
    elif mutation == 'index': row['working_copy_id'] = 'changed'
    elif mutation == 'inventory':
        store.conn.execute('DELETE FROM instruction_inventories');store.commit()
    else:
        if mutation == 'text': record['files'][0]['text'] += 'corrupt'
        elif mutation == 'binding': record['files'][0]['content_hash'] = 'f' * 64
        elif mutation == 'profile': record['profile'] = 'instruction-text/999'
        record['id'] = digest({k:v for k,v in record.items() if k != 'id'})
        row.update(id=record['id'], record_json=encoded(record), record_hash=digest(record))
    store.conn.execute('DELETE FROM instruction_text_archives');store.insert(text_archive.TABLE,row);store.commit()
    with pytest.raises(text_archive.TextArchiveError, match=row['id']): text_archive.read_archive(store,row['id'])


def test_selected_reader_uses_retained_archive_without_files_or_writes(cfg, store, tmp_path, monkeypatch):
    repo = init_git_repo(tmp_path/'project', 'AGENTS.md', 'Retained only in copy.\n'); known_copy(store,repo); collect(store,cfg)
    saved = archives(store)[0]
    selected = tmp_path/'selected.db'; conn=sqlite3.connect(selected);store.conn.backup(conn);conn.close()
    (repo/'AGENTS.md').unlink()
    store.conn.execute('DELETE FROM instruction_text_archives');store.commit()
    def forbidden(*args,**kwargs): raise AssertionError('unexpected filesystem or mutation')
    reader = Store(str(selected),read_only=True)
    try:
        before = reader.conn.total_changes
        monkeypatch.setattr(Path,'open',forbidden)
        monkeypatch.setattr(availability,'inspect_surfaces',forbidden)
        monkeypatch.setattr(reader,'insert',forbidden)
        import builtins
        import io
        import os
        import subprocess
        monkeypatch.setattr(builtins,'open',forbidden)
        monkeypatch.setattr(io,'open',forbidden)
        monkeypatch.setattr(os,'open',forbidden)
        monkeypatch.setattr(sqlite3,'connect',forbidden)
        monkeypatch.setattr(subprocess,'Popen',forbidden)
        assert text_archive.read_archive(reader,saved['id']) == saved
        assert text_archive.current_archives(reader)['records'][0]['archive'] == saved
        assert reader.conn.total_changes == before == 0
        assert reader.query_one('SELECT COUNT(*) n FROM llm_calls')['n'] == 0
    finally: reader.close()


def test_rebuild_preserves_and_exports_text_and_damaged_schema_fails_preview(cfg, store, tmp_path):
    from self_improve.rebuild import rebuild_state
    repo = init_git_repo(tmp_path/'project','AGENTS.md','Retained fixture.\n');known_copy(store,repo);collect(store,cfg)
    saved = store.query('SELECT * FROM instruction_text_archives ORDER BY id')
    destination = tmp_path/'private-backup';rebuild_state(store,export_path=destination)
    assert json.loads((destination/'preserved.json').read_text())['instruction_text_archives'] == saved
    assert store.query('SELECT * FROM instruction_text_archives ORDER BY id') == saved
    assert len(archives(store)) == 1
    store.conn.execute('DROP TABLE instruction_text_archives');store.commit()
    with pytest.raises(text_archive.TextArchiveError, match='damaged'):
        rebuild_state(store,dry_run=True,export_path=tmp_path/'preview-only')


def test_real_pipeline_records_instruction_text_after_scanning_known_copies(tmp_path):
    from tests.e2e_corpus import build_corpus
    from self_improve.pipeline import run_pipeline
    corpus = build_corpus(tmp_path)
    original = 'Pipeline retained instruction.\n'
    (corpus.clone_a/'AGENTS.md').write_text(original)
    try:
        stats = run_pipeline(corpus.cfg, corpus.store, dry_run=True)
        records = archives(corpus.store)
        matched = [record for record in records if any(f['text'] == original for f in record['files'])]
        assert len(matched) == 1
        assert matched[0]['inventory_id'] in stats['availability']['inventory_ids']
        assert matched[0]['run_id'] == stats['availability']['run_id']
        assert corpus.store.query_one('SELECT id FROM runs WHERE id=?',(matched[0]['run_id'],))
        assert corpus.store.query_one('SELECT COUNT(*) n FROM llm_calls')['n'] == 0
        assert (corpus.clone_a/'AGENTS.md').read_text() == original
    finally: corpus.store.close()


def test_builder_rejects_different_surface_and_writer_does_not_commit_caller(cfg, store, tmp_path):
    from copy import deepcopy
    from self_improve.instruction_surfaces import inspect_surfaces
    from self_improve.rule_revisions import retained_revisions
    repo = init_git_repo(tmp_path/'project','AGENTS.md','Exact observed text.\n')
    key = known_copy(store,repo)
    surface = inspect_surfaces(cfg,str(repo))
    inv = inventory.build_inventory(surface=surface,project_key=key,working_copy_path=str(repo),
        revisions=retained_revisions(store),observed_at=AT)
    changed = deepcopy(surface);changed['files'][0]['_text'] = 'Different surface.\n'
    with pytest.raises(text_archive.TextArchiveError,match='differs from inventory'):
        text_archive.build_archive(changed,inv)
    record = text_archive.build_archive(surface,inv)
    with pytest.raises(RuntimeError,match='caller rolls back'):
        with store.transaction(write=True):
            inventory.record_inventories(store,records=[inv])
            text_archive.record_archives(store,[record])
            assert store.query_one('SELECT id FROM instruction_text_archives')
            raise RuntimeError('caller rolls back')
    assert store.query('SELECT id FROM instruction_inventories') == []
    assert store.query('SELECT id FROM instruction_text_archives') == []


def test_collector_requires_explicit_text_upgrade_without_mutating_old_schema(cfg, tmp_path, monkeypatch):
    from self_improve import store as storage
    path = tmp_path/'old-schema.db'
    repo = init_git_repo(tmp_path/'project','AGENTS.md','Retained after explicit upgrade.\n')
    with monkeypatch.context() as patch:
        patch.setattr(storage,'MIGRATIONS',[m for m in storage.MIGRATIONS if m[0] != text_archive.MIGRATION])
        old = storage.Store(path)
        known_copy(old,repo)
        old.close()
    writer = storage.Store(path,migrate=False)
    try:
        before = writer.query('SELECT * FROM schema_migrations ORDER BY name')
        with pytest.raises(text_archive.TextArchiveError,match='UpgradeRequired: 0033_instruction_text'):
            collect(writer,cfg)
        assert writer.query('SELECT * FROM schema_migrations ORDER BY name') == before
        assert writer.conn.total_changes == 0
        assert writer.query('SELECT id FROM instruction_inventories') == []
        assert text_archive.current_archives(writer)['reason'] == 'schema_unavailable'
    finally: writer.close()
    upgraded = storage.Store(path)
    try:
        collect(upgraded,cfg)
        assert len(archives(upgraded)) == 1
    finally: upgraded.close()


# BACKLOG BL-35: each archive is validated against the redaction rules version
# it records. Invented Figma-shaped URL path: the current rules redact its
# 22-character mixed-case ID as high entropy; the pre-versioning rules kept
# every token with two or more slashes.
FIGMA_SHAPED = 'Open https://www.figma.com/design/Qx7Lm2Rt9Vb4Kp8Wn3Zd5H/MyDesignSystem first.\n'
_UNVERSIONED = object()


def _rewritten(record, raw, redact_with, version):
    """The archive that a writer with other redaction rules would have stored."""
    from copy import deepcopy
    changed = {k: deepcopy(v) for k, v in record.items() if k not in {'id', text_archive.RULES_FIELD}}
    if version is not _UNVERSIONED: changed[text_archive.RULES_FIELD] = version
    for file in changed['files']:
        text = redact_with(raw)
        file.update(text=text, redacted_hash=text_hash(text), redacted_bytes=len(text.encode()),
                    redacted_lines=len(text.splitlines()))
    changed['id'] = digest(changed)
    return changed


def _store_directly(store, record):
    """Replace the stored row byte-for-byte, as an earlier writer left it."""
    store.conn.execute('DELETE FROM instruction_text_archives WHERE inventory_id=?', (record['inventory_id'],))
    store.insert(text_archive.TABLE, {**{k: record[k] for k in text_archive.COLUMNS},
                                      'record_json': encoded(record), 'record_hash': digest(record)})
    store.commit()


def _one_file_archive(cfg, store, tmp_path, body):
    repo = init_git_repo(tmp_path/'project', 'AGENTS.md', body)
    known_copy(store, repo); collect(store, cfg)
    saved = archives(store)
    assert len(saved) == 1 and len(saved[0]['files']) == 1
    return saved[0]


def test_an_unversioned_archive_validates_with_the_frozen_pre_versioning_rules(cfg, store, tmp_path):
    from self_improve import redact, redact_legacy
    # Premise: the current rules change this text and the frozen rules keep it.
    assert redact.redact_text(FIGMA_SHAPED) != FIGMA_SHAPED
    assert redact_legacy.redact_text(FIGMA_SHAPED) == FIGMA_SHAPED
    current = _one_file_archive(cfg, store, tmp_path, FIGMA_SHAPED)
    legacy = _rewritten(current, FIGMA_SHAPED, redact_legacy.redact_text, _UNVERSIONED)
    assert text_archive.RULES_FIELD not in legacy and legacy['files'][0]['text'] == FIGMA_SHAPED
    _store_directly(store, legacy)
    assert text_archive.read_archive(store, legacy['id']) == legacy
    assert text_archive.current_archives(store)['records'][0]['archive'] == legacy
    # An exact replay of the retained row stays a no-op.
    with store.transaction(write=True): text_archive.record_archives(store, [legacy])
    assert archives(store) == [legacy]


def test_a_new_archive_records_the_current_rules_version(cfg, store, tmp_path):
    from self_improve import redact
    record = _one_file_archive(cfg, store, tmp_path, FIGMA_SHAPED)
    assert record[text_archive.RULES_FIELD] == redact.RULES_VERSION
    assert record['files'][0]['text'] == redact.redact_text(FIGMA_SHAPED) != FIGMA_SHAPED
    stored = json.loads(store.query_one('SELECT record_json FROM instruction_text_archives')['record_json'])
    assert stored[text_archive.RULES_FIELD] == redact.RULES_VERSION
    assert text_archive.current_archives(store)['records'][0]['archive'] == record


@pytest.mark.parametrize('version', ['redaction-rules/999', '', None, 2])
def test_an_unknown_rules_version_is_refused_by_archive_name(cfg, store, tmp_path, version):
    from self_improve import redact
    current = _one_file_archive(cfg, store, tmp_path, 'Retained text.\n')
    unknown = _rewritten(current, 'Retained text.\n', redact.redact_text, version)
    _store_directly(store, unknown)
    expected = 'instruction_text_archives.' + unknown['id'] + '.*unknown redaction rules version'
    with pytest.raises(text_archive.TextArchiveError, match=expected):
        text_archive.read_archive(store, unknown['id'])
    with pytest.raises(text_archive.TextArchiveError, match=expected):
        text_archive.current_archives(store)


@pytest.mark.parametrize('rules', ['unversioned', 'current'])
def test_text_that_fails_its_own_rules_version_is_still_refused(cfg, store, tmp_path, rules):
    from self_improve import redact
    raw = 'token = ghp_' + 'Ab1Cd2Ef3Gh4Ij5Kl6Mn7Op8Qr9St0Uv1Wx2' + '\n'
    current = _one_file_archive(cfg, store, tmp_path, raw)
    version = _UNVERSIONED if rules == 'unversioned' else redact.RULES_VERSION
    # Both rule sets redact this token, so storing it raw must fail either way.
    unredacted = _rewritten(current, raw, lambda text: text, version)
    _store_directly(store, unredacted)
    with pytest.raises(text_archive.TextArchiveError, match=unredacted['id'] + '.*invalid redacted text'):
        text_archive.read_archive(store, unredacted['id'])


def test_a_versioned_archive_holding_text_only_the_legacy_rules_keep_is_refused(cfg, store, tmp_path):
    """A versioned archive is validated with the rules of its version, never
    with the legacy rules. The ghp_ token above cannot show this, because both
    rule sets redact it. FIGMA_SHAPED tells them apart."""
    from self_improve import redact, redact_legacy
    assert redact_legacy.redact_text(FIGMA_SHAPED) == FIGMA_SHAPED
    assert redact.redact_text(FIGMA_SHAPED) != FIGMA_SHAPED
    current = _one_file_archive(cfg, store, tmp_path, FIGMA_SHAPED)
    forged = _rewritten(current, FIGMA_SHAPED, redact_legacy.redact_text, redact.RULES_VERSION)
    assert forged[text_archive.RULES_FIELD] == redact.RULES_VERSION
    assert forged['files'][0]['text'] == FIGMA_SHAPED
    _store_directly(store, forged)
    expected = 'instruction_text_archives.' + forged['id'] + '.*invalid redacted text'
    with pytest.raises(text_archive.TextArchiveError, match=expected):
        text_archive.read_archive(store, forged['id'])
    with pytest.raises(text_archive.TextArchiveError, match=expected):
        text_archive.current_archives(store)


def test_a_new_archive_without_the_current_rules_version_is_not_written(cfg, store, tmp_path):
    from self_improve import redact_legacy
    from self_improve.instruction_surfaces import inspect_surfaces
    from self_improve.rule_revisions import retained_revisions
    repo = init_git_repo(tmp_path/'project', 'AGENTS.md', FIGMA_SHAPED)
    key = known_copy(store, repo)
    surface = inspect_surfaces(cfg, str(repo))
    inv = inventory.build_inventory(surface=surface, project_key=key, working_copy_path=str(repo),
        revisions=retained_revisions(store), observed_at=AT)
    legacy = _rewritten(text_archive.build_archive(surface, inv), FIGMA_SHAPED,
                        redact_legacy.redact_text, _UNVERSIONED)
    with pytest.raises(text_archive.TextArchiveError, match='must record the current redaction rules'):
        with store.transaction(write=True):
            inventory.record_inventories(store, records=[inv])
            text_archive.record_archives(store, [legacy])
    assert store.query('SELECT id FROM instruction_text_archives') == []


def test_glued_google_keys_no_longer_abort_collection(cfg, store, tmp_path):
    """BL-35: one redaction pass left the second of two glued keys in place,
    so the archive failed its own validation and the collection aborted."""
    key = 'AIza' + 'A' * 35  # invented
    record = _one_file_archive(cfg, store, tmp_path, 'keys ' + key + key + '\n')
    assert record['files'][0]['text'] == 'keys [REDACTED:google_api_key][REDACTED:google_api_key]\n'
