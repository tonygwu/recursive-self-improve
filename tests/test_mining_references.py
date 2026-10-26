"""Retained trigger recovery; every transcript, target and model result is invented."""
import json
from pathlib import Path

import pytest

from self_improve import incident_jobs, job_worker, miner
from self_improve.commands import submit_command
from self_improve.config import Config
from tests.test_apply import store
from tests.test_incident_jobs import env as incident_env, request
from tests.test_miner_agentic import seed_incident, agentic_payload, PROMPTS_DIR

NOTE = 'full transcript has no matching incident timestamp; redacted incident window only'


@pytest.fixture
def cfg(tmp_path):
    # pytest's tmp_path lies under /var/folders, which the default denylist
    # names; selected mining refuses denylisted incidents (test_mine_denylist.py).
    return Config(state_dir=str(tmp_path / 'state'), denylist_substrings=(),
        global_claude_md=str(tmp_path / 'global.md'),
        codex_global_agents_md=str(tmp_path / 'AGENTS.md'),
        skills_dir=str(tmp_path / 'skills'))


def write_session(path, project, *, ts='2026-08-02T00:30:00Z', text='An unrelated later message.'):
    path.write_text(json.dumps({'type': 'user', 'sessionId': 'sess-ag',
        'cwd': str(project), 'timestamp': ts,
        'message': {'role': 'user', 'content': text}}) + '\n')


@pytest.fixture
def stale(store, tmp_path):
    path = tmp_path / 'rewritten-session.jsonl'
    write_session(path, tmp_path)
    return seed_incident(store, str(path), project=str(tmp_path)), path


def test_agentic_mining_recovers_retained_trigger_and_publishes_once(cfg, store, stale, tmp_path):
    inc, path = stale
    before_source = path.read_bytes()
    calls = []
    def answer(prompt, sandbox):
        text = (sandbox / 'transcript.md').read_text()
        assert NOTE in text
        assert inc['ts'] in text and inc['matched_text'] in text
        assert 'An unrelated later message.' not in text
        assert '## [1]' in prompt
        calls.append(text)
        return agentic_payload()
    learning = miner.mine_incident_agentic(store, answer, inc, cfg, PROMPTS_DIR, tmp_path / 'mine')
    saved = store.query_one('SELECT * FROM incidents WHERE id=?', (inc['id'],))
    assert saved['status'] == 'mined' and saved['window_json'] == inc['window_json']
    assert saved['ts'] == inc['ts'] and path.read_bytes() == before_source
    assert len(calls) == 1
    assert len(store.query('SELECT * FROM incident_learnings WHERE incident_id=?', (inc['id'],))) == 1
    assert len(store.query('SELECT * FROM mining_history WHERE learning_id=?', (learning['id'],))) == 1
    assert store.query("SELECT * FROM incidents WHERE status='new' AND id=?", (inc['id'],)) == []


def test_selected_preview_uses_the_same_retained_source_without_writes(cfg, store, stale):
    inc, path = stale
    before = list(store.conn.iterdump())
    shown = incident_jobs.preview(store, cfg, inc['id'])
    source = shown['source']['snapshot']
    assert source['coverage']['kind'] == 'retained_window'
    assert source['coverage']['reason'] == 'full_reference_missing'
    assert NOTE in source['files']['transcript.md']
    assert shown['max_model_calls'] == 1
    assert list(store.conn.iterdump()) == before


def test_selected_job_executes_frozen_recovery_once(incident_env):
    cfg, store, inc, calls = incident_env
    path = Path(inc['session_file'])
    write_session(path, path.parent)
    command = submit_command(store, cfg, request(incident_env))
    # The worker must execute retained reviewed evidence even if the file changes again.
    path.write_text('This is no longer JSONL.\n')
    result = job_worker.run_once(store, cfg)
    assert result['id'] == command['id'] and result['state'] == 'completed'
    assert len(calls) == 1
    assert NOTE in (Path(calls[0]['kwargs']['cwd']) / 'transcript.md').read_text()
    assert job_worker.run_once(store, cfg) is None
    assert len(calls) == 1
    assert store.query_one('SELECT status FROM incidents WHERE id=?', (inc['id'],))['status'] == 'mined'
    assert store.query('SELECT * FROM instruction_operations') == []


@pytest.mark.parametrize('window', ['[]', '[1]', 'not-json', json.dumps([
    {'role': 'human', 'ts': '2026-08-03T00:30:00Z', 'text': 'A different trigger.'}])])
def test_unusable_retained_evidence_still_refuses_before_call(cfg, store, stale, tmp_path, window):
    inc, path = stale
    inc = {**inc, 'window_json': window}
    calls = []
    before = list(store.conn.iterdump())
    with pytest.raises(miner.MinerError):
        miner.mine_incident_agentic(store, lambda *args: calls.append(args), inc, cfg, PROMPTS_DIR, tmp_path / 'mine')
    assert calls == [] and list(store.conn.iterdump()) == before


def test_valid_full_transcript_still_wins(cfg, store, stale, tmp_path):
    inc, path = stale
    write_session(path, path.parent, ts=inc['ts'], text=inc['matched_text'])
    seen = []
    def answer(prompt, sandbox):
        seen.append((sandbox / 'transcript.md').read_text())
        return agentic_payload()
    miner.mine_incident_agentic(store, answer, inc, cfg, PROMPTS_DIR, tmp_path / 'mine')
    assert len(seen) == 1 and NOTE not in seen[0] and miner.AGED_OUT_NOTE not in seen[0]


def test_empty_full_parse_is_not_reclassified_as_a_missing_reference(cfg, store, stale, tmp_path):
    inc, path = stale
    path.write_text('')
    with pytest.raises(miner.MinerError, match='parsed to zero events'):
        miner.mine_incident_agentic(store, lambda *args: pytest.fail('No call'), inc, cfg, PROMPTS_DIR, tmp_path / 'mine')


def test_recovered_window_is_redacted_at_the_final_boundary(cfg, store, stale, tmp_path):
    inc, path = stale
    secret = 'sk-' + 'A' * 48
    inc = {**inc, 'window_json': json.dumps([{'role': 'human', 'ts': inc['ts'],
        'text': inc['matched_text'] + ' ' + secret}])}
    def answer(prompt, sandbox):
        text = (sandbox / 'transcript.md').read_text()
        assert secret not in text and '[REDACTED:' in text and NOTE in text
        return agentic_payload()
    miner.mine_incident_agentic(store, answer, inc, cfg, PROMPTS_DIR, tmp_path / 'mine')


@pytest.mark.parametrize('source', ['claude', 'codex'])
@pytest.mark.parametrize('entrypoint', ['nightly', 'selected'])
def test_parser_excluded_reference_does_not_gain_mining_eligibility(cfg, store, stale, tmp_path, source, entrypoint):
    inc, path = stale
    if source == 'codex':
        store.update('sessions', 'file_path', str(path), {'source': source})
        store.commit()
        path.write_text(json.dumps({'type': 'event_msg', 'timestamp': '2026-08-02T00:30:00Z',
            'payload': {'type': 'user_message', 'message': 'Unrelated later message.'}}) + '\n')
    # An envelope with the original timestamp still exists, but its record kind
    # is deliberately excluded. Retained evidence must not override that choice.
    with path.open('a') as file:
        file.write(json.dumps({'type': 'progress' if source == 'claude' else 'turn_context',
            'timestamp': inc['ts'], 'payload': {}}) + '\n')
    before = list(store.conn.iterdump())
    with pytest.raises(miner.MinerError, match='raw record.*no parsed event'):
        if entrypoint == 'selected':
            incident_jobs.preview(store, cfg, inc['id'])
        else:
            miner.mine_incident_agentic(store, lambda *args: pytest.fail('No call'),
                inc, cfg, PROMPTS_DIR, tmp_path / 'mine')
    assert list(store.conn.iterdump()) == before


def test_malformed_raw_evidence_cannot_prove_reference_absence(cfg, store, stale, tmp_path):
    inc, path = stale
    with path.open('a') as file:
        file.write('{"timestamp":')
    with pytest.raises(miner.MinerError, match='cannot verify.*malformed raw record'):
        miner.mine_incident_agentic(store, lambda *args: pytest.fail('No call'),
            inc, cfg, PROMPTS_DIR, tmp_path / 'mine')


def test_changed_full_transcript_does_not_prove_reference_absence(cfg, store, stale, tmp_path, monkeypatch):
    from types import SimpleNamespace
    inc, path = stale
    source = miner._source_for('claude', cfg)
    def changing_parse(*args):
        yield from source.parse(*args)
        with path.open('a') as file:
            file.write('\n')
    monkeypatch.setattr(miner, '_source_for', lambda *args: SimpleNamespace(parse=changing_parse))
    with pytest.raises(miner.MinerError, match='transcript changed'):
        miner.mine_incident_agentic(store, lambda *args: pytest.fail('No call'),
            inc, cfg, PROMPTS_DIR, tmp_path / 'mine')


def test_codex_rewritten_transcript_recovers_valid_retained_occurrence(cfg, store, stale):
    inc, path = stale
    store.update('sessions', 'file_path', str(path), {'source': 'codex'})
    window = [{'ts': inc['ts'], 'text': inc['matched_text'], 'count_in_session': 3,
        'session_file': str(path), 'project_path': str(path.parent)}]
    store.update('incidents', 'id', inc['id'], {'window_json': json.dumps(window)})
    store.commit()
    path.write_text(json.dumps({'type': 'event_msg', 'timestamp': '2026-08-02T00:30:00Z',
        'payload': {'type': 'user_message', 'message': 'Unrelated later message.'}}) + '\n')
    shown = incident_jobs.preview(store, cfg, inc['id'])
    source = shown['source']['snapshot']
    assert source['coverage']['reason'] == 'full_reference_missing'
    text = source['files']['transcript.md']
    assert NOTE in text and 'recurred 3x' in text and inc['ts'] in text
