"""Future summary guidance never rewrites an approved record or instruction."""
from dataclasses import replace
from pathlib import Path
import hashlib
import json

import pytest

from tests.test_dashboard_commands import env, request
from tests.test_miner_agentic import agentic_payload, seed_incident
from self_improve import miner, mining_history
from self_improve.dashboard.app import create_app
from fastapi.testclient import TestClient

PROMPTS = Path(__file__).resolve().parents[1] / 'prompts'


@pytest.mark.parametrize('mode', ['fast', 'agentic', 'duplicate'])
def test_future_summary_guidance_preserves_approved_content(env, tmp_path, mode):
    cfg, store, one, two = env
    cfg = replace(cfg, global_claude_md=one['target_path'],
                  codex_global_agents_md=str(tmp_path / 'AGENTS.md'),
                  skills_dir=str(tmp_path / 'skills'))
    old_summary = 'Original retained wording: preserve <literal> text and the full explanation. ' * 8
    store.update('learnings', 'id', one['learning_id'], {'incident_summary': old_summary})
    store.commit()
    with TestClient(create_app(cfg)) as client:
        response = client.post('/api/commands', json=request(client, [one['id']]))
        assert response.status_code == 202, response.text
    protected_tables = ('proposals', 'proposal_revisions', 'commands', 'command_members', 'proposal_events')
    before = {table: store.query('SELECT * FROM ' + table + ' ORDER BY rowid') for table in protected_tables}
    learning_before = store.query_one('SELECT * FROM learnings WHERE id=?', (one['learning_id'],))
    target = Path(one['target_path'])
    target_before = target.read_bytes()
    summary = ('The agent replaced the configuration file and removed an existing setting. '
               'The retained window does not show why it chose that replacement.')
    payload = agentic_payload(incident_summary=summary,
                              generalized_rule='**Read the current configuration** — preserve unrelated settings when changing a file.')
    if mode == 'duplicate':
        payload.update(dedup_decision='duplicate', dedup_target_id=one['learning_id'])
    incident = seed_incident(store, str(tmp_path / 'aged-out.jsonl'), project=str(tmp_path))
    store.update('incidents', 'id', incident['id'], {
        'matched_text': 'That replacement removed the existing setting.',
        'window_json': json.dumps([
            {'role': 'assistant', 'ts': incident['ts'], 'text': 'I replaced the configuration file.'},
            {'role': 'human', 'ts': incident['ts'], 'text': 'That replacement removed the existing setting.'},
        ]),
    })
    store.commit()
    incident = store.query_one('SELECT * FROM incidents WHERE id=?', (incident['id'],))
    calls = []

    def response(prompt, *_):
        calls.append(prompt)
        assert '## Summary style guide' in prompt
        assert 'observed consequence' in prompt
        assert 'cause is unknown' in prompt
        assert 'Do not amend an existing rule only to change its wording.' in prompt
        if mode == 'fast':
            return {k: v for k, v in payload.items() if k not in miner.MINE_AGENTIC_EXTRA_KEYS}
        return payload

    if mode == 'fast':
        result = miner.mine_incident(store, response, incident, cfg, PROMPTS)
        template = PROMPTS / 'mine_incident.md'
    else:
        result = miner.mine_incident_agentic(store, response, incident, cfg, PROMPTS, tmp_path / 'sandbox')
        template = PROMPTS / 'mine_incident_agentic.md'
    assert len(calls) == 1
    assert result['incident_summary'] == (old_summary if mode == 'duplicate' else summary)
    current = store.query_one('SELECT * FROM learnings WHERE id=?', (one['learning_id'],))
    for field in mining_history.CONTENT_FIELDS:
        assert current[field] == learning_before[field], field
    for table, rows in before.items():
        assert store.query('SELECT * FROM ' + table + ' ORDER BY rowid') == rows, table
    assert target.read_bytes() == target_before
    record = mining_history.page(store, result['id'])['records'][0]
    assert record['template_sha'] == hashlib.sha256(template.read_bytes()).hexdigest()
    assert record['prompt_sha'] == hashlib.sha256(calls[0].encode()).hexdigest()
    assert record['after']['incident_summary'] == result['incident_summary']
