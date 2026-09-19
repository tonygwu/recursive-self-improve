"""Supported Codex lifecycle intake with invented native stdin and temporary state."""
from contextlib import closing
import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest
from self_improve import native_loads as api, scan_observations as so
from self_improve.store import Store
from tests.test_session_context import env
from tests.test_native_loads import config_file, event as claude_event
from tests.test_scan_observations import PROJECT, SID


def event(env, kind='SessionStart', **changes):
    extra = {
        'SessionStart': {'source': 'startup'},
        'SessionEnd': {'reason': 'other'},
        'PreCompact': {'turn_id': 'turn-1', 'trigger': 'manual'},
        'PostCompact': {'turn_id': 'turn-1', 'trigger': 'auto'},
        'SubagentStart': {'turn_id': 'turn-1', 'agent_id': 'child-1', 'agent_type': 'reviewer'},
        'SubagentStop': {'turn_id': 'turn-1', 'agent_id': 'child-1', 'agent_type': 'reviewer', 'agent_transcript_path': None, 'stop_hook_active': False},
        'Interrupt': {'turn_id': 'turn-1'},
    }[kind]
    return dict(session_id=SID, transcript_path=None, cwd=str(env.repo),
                hook_event_name=kind, model='invented-reported-model', **extra) | changes


def test_codex_null_transcript_is_retained_with_explicit_provider_identity(env):
    got = api.record_native_event(env.store, env.cfg, event(env), provider='codex')
    assert got['source'] == 'codex'
    assert got['profile'] == 'codex-native-lifecycle-reports/1'
    assert got['logical_session_key'] == so.logical_session_key('codex', SID)
    assert got['payload']['transcript_path'] is None
    assert got['loaded_content_hash'] is got['occurred_at'] is got['rule_revision_id'] is None
    assert api.native_history(env.store, project_key=PROJECT)['records'] == [got]


def test_cli_generates_a_codex_fragment_without_installation_or_models(env):
    config = config_file(env)
    result = subprocess.run([sys.executable, '-m', 'self_improve.cli', '--config', str(config), 'session-hook-settings', '--provider', 'codex'], capture_output=True, text=True, cwd=env.tmp)
    assert result.returncode == 0, result.stderr
    hooks = json.loads(result.stdout)['hooks']
    assert 'InstructionsLoaded' not in hooks and 'Interrupt' in hooks
    assert hooks['SessionEnd'][0]['hooks'][0]['timeout'] <= 3


@pytest.mark.parametrize('kind', api.CODEX_EVENTS)
def test_exact_generated_command_records_every_supported_codex_event(env, kind):
    config = config_file(env)
    hooks = api.hook_settings(config_path=config, python_path=sys.executable, provider='codex')['hooks']
    handler = hooks[kind][0]['hooks'][0]
    sent = event(env, kind, prompt='never retain this body', compact_summary='never retain this body', last_assistant_message='never retain this body', tool_input={'secret': 'never retain this body'})
    result = subprocess.run(handler['command'], shell=True, cwd=env.tmp, input=json.dumps(sent), text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == ''
    got = api.native_history(env.store, project_key=PROJECT)['records'][0]
    assert got['payload'] == api.normalize_event(sent, provider='codex')
    assert got['source'] == 'codex' and 'never retain' not in str(got)
    assert got['occurred_at'] is got['loaded_content_hash'] is None
    assert handler['timeout'] == (3 if kind in {'SessionEnd','Interrupt'} else 20)


@pytest.mark.parametrize('kind,change', [
    ('SessionStart', {'source': 'fork'}), ('SessionStart', {'source': ['resume']}),
    ('SessionStart', {'transcript_path': 12}), ('SessionStart', {'transcript_path': 'relative'}),
    ('SessionStart', {'model': None}), ('SessionStart', {'permission_mode': 'unknown'}),
    ('SessionStart', {'session_id': 'newline\n'}), ('SessionStart', {'cwd': 'relative'}),
    ('PreCompact', {'trigger': 'unknown'}), ('PreCompact', {'turn_id': None}),
    ('SubagentStart', {'agent_id': ''}), ('SubagentStart', {'agent_type': []}),
    ('SubagentStop', {'stop_hook_active': 0}), ('SubagentStop', {'agent_transcript_path': 'relative'}),
    ('SessionEnd', {'reason': 'not documented'}), ('Interrupt', {'turn_id': ['turn']})])
def test_invalid_codex_metadata_never_publishes(env, kind, change):
    with pytest.raises(api.NativeLoadError):
        api.record_native_event(env.store, env.cfg, event(env, kind, **change), provider='codex')
    assert env.store.query('SELECT * FROM native_load_reports') == []


@pytest.mark.parametrize('path', [None, '/invented/gone.jsonl'])
def test_transcript_is_metadata_and_is_never_opened(env, monkeypatch, path):
    import builtins
    real_open = builtins.open
    def guarded(file, *args, **kwargs):
        if str(file) == '/invented/gone.jsonl':
            pytest.fail('native receipt read transcript content')
        return real_open(file, *args, **kwargs)
    monkeypatch.setattr(builtins, 'open', guarded)
    row = api.record_native_event(env.store, env.cfg, event(env, transcript_path=path), provider='codex')
    assert row['payload']['transcript_path'] == path
    assert api.native_history(env.store, project_key=PROJECT)['records'] == [row]


def test_subagent_boundaries_keep_parent_session_and_child_identity(env):
    parent = api.record_native_event(env.store, env.cfg, event(env), provider='codex')
    child = api.record_native_event(env.store, env.cfg, event(env, 'SubagentStart'), provider='codex')
    stopped = api.record_native_event(env.store, env.cfg, event(env, 'SubagentStop'), provider='codex')
    assert parent['logical_session_key'] == child['logical_session_key'] == stopped['logical_session_key']
    assert child['agent_id'] == stopped['agent_id'] == 'child-1'
    assert parent['agent_id'] == ''


def test_same_payload_and_receipt_cannot_change_provider_on_replay(env, monkeypatch):
    payload = event(env, transcript_path='/invented/gone.jsonl')
    rid = str(uuid4())
    first = api.record_native_event(env.store, env.cfg, payload, receipt_id=rid, provider='claude')
    # Use the common normalized fields so the provider itself is the only change.
    payload.pop('model')
    def forbidden(*a, **k):
        pytest.fail('replay resolved current identity')
    monkeypatch.setattr(api.project_identity, 'resolve', forbidden)
    with pytest.raises(api.NativeLoadError, match='replay differs'):
        api.record_native_event(env.store, env.cfg, payload, receipt_id=rid, provider='codex')
    assert api.native_history(env.store, project_key=PROJECT)['records'] == [first]


def test_codex_replay_keeps_first_receive_time_without_current_identity(env, monkeypatch):
    rid = str(uuid4())
    first = api.record_native_event(env.store, env.cfg, event(env), provider='codex', receipt_id=rid, received_at='2026-01-01T00:00:00Z')
    def forbidden(*a, **k):
        pytest.fail('replay resolved current identity')
    monkeypatch.setattr(api.project_identity, 'resolve', forbidden)
    assert api.record_native_event(env.store, env.cfg, event(env), provider='codex', receipt_id=rid, received_at='2026-02-01T00:00:00Z') == first


def test_same_native_id_in_two_providers_keeps_distinct_session_keys(env):
    a = api.record_native_event(env.store, env.cfg, claude_event(env))
    b = api.record_native_event(env.store, env.cfg, event(env), provider='codex')
    assert a['payload']['session_id'] == b['payload']['session_id']
    assert a['project_key'] == b['project_key'] and a['working_copy_id'] == b['working_copy_id']
    assert a['logical_session_key'] != b['logical_session_key']
    for row in (a,b):
        assert api.native_history(env.store, project_key=PROJECT, logical_session_key=row['logical_session_key'])['records'] == [row]


def test_mixed_profile_pages_and_rebuild_preserve_every_receipt(env):
    from self_improve.rebuild import rebuild_state
    expected = []
    for n in range(23):
        provider = 'claude' if n % 2 else 'codex'
        payload = claude_event(env) if provider == 'claude' else event(env)
        expected.append(api.record_native_event(env.store, env.cfg, payload, provider=provider))
    first = api.native_history(env.store, project_key=PROJECT)
    second = api.native_history(env.store, project_key=PROJECT, cursor=first['next_cursor'])
    assert len(first['records']) == 20 and len(second['records']) == 3
    assert {r['id'] for r in first['records'] + second['records']} == {r['id'] for r in expected}
    assert first['in_force_instructions'] is None and first['coverage_complete'] is False
    destination = env.tmp / 'new-private-backup'
    rebuild_state(env.store, export_path=destination, dry_run=False)
    after = api.native_history(env.store, project_key=PROJECT, limit=100)
    assert {r['id'] for r in after['records']} == {r['id'] for r in expected}
    archived = [p.read_text() for p in destination.rglob('*.json')]
    assert all(any(row['id'] in text for text in archived) for row in expected)


@pytest.mark.parametrize('field,value', [('source','claude'),('profile',api.PROFILE),('logical_session_key','0'*64),('occurred_at','2026-01-01T00:00:00Z')])
def test_hash_consistent_corrupt_provider_binding_is_refused(env, field, value):
    row = api.record_native_event(env.store, env.cfg, event(env), provider='codex')
    row[field] = value
    changes = {'record_json':so.canonical_json(row), 'record_hash':so.content_id(row)}
    if field in api.COLUMNS:
        changes[field] = value
    env.store.update('native_load_reports', 'id', row['id'], changes)
    env.store.commit()
    with pytest.raises(api.NativeLoadError, match=row['id']):
        api.native_history(env.store, project_key=PROJECT)


def test_codex_denylist_includes_optional_child_transcript_before_identity(env, monkeypatch):
    from dataclasses import replace
    cfg = replace(env.cfg, denylist_substrings=('do-not-read',))
    def forbidden(*a, **k):
        pytest.fail('identity access before path policy')
    monkeypatch.setattr(api.project_identity, 'resolve', forbidden)
    with pytest.raises(api.NativeLoadError, match='path policy'):
        api.record_native_event(env.store, cfg, event(env, 'SubagentStop', agent_transcript_path='/do-not-read/session.jsonl'), provider='codex')
    assert env.store.query('SELECT * FROM native_load_reports') == []


def test_codex_unknown_load_event_cannot_forge_file_receipt(env):
    with pytest.raises(api.NativeLoadError, match='Unsupported Codex'):
        api.record_native_event(env.store, env.cfg, claude_event(env), provider='codex')


@pytest.mark.parametrize('raw', [b'[]',b'{"a":1,"a":2}',b'{"a":NaN}',b'null',b'\xff',b'x'*65537])
def test_codex_input_uses_same_bounded_strict_parser(raw):
    with pytest.raises(api.NativeLoadError):
        api.parse_input(raw, provider='codex')


def test_codex_http_reads_selected_copy_and_never_writes(env, tmp_path):
    pytest.importorskip('fastapi')
    from fastapi.testclient import TestClient
    from self_improve.dashboard.app import create_app
    original = list(env.store.conn.iterdump())
    selected = tmp_path / 'selected.db'
    with closing(Store(selected)) as other:
        env.store.conn.backup(other.conn)
        row = api.record_native_event(other, env.cfg, event(env), provider='codex')
    with TestClient(create_app(env.cfg, db_path=selected)) as client:
        response = client.get('/api/project-native-events', params={'project_key':PROJECT})
        assert response.status_code == 200
        assert response.json()['records'] == [row]
        assert client.post('/api/project-native-events', json=event(env)).status_code == 405
    assert list(env.store.conn.iterdump()) == original
