"""Research-preview workflow with invented histories and stubbed model/gate output.

Parsers, mining persistence, HTTP decisions, file delivery and rollback stay real.
This checks software contracts, not provider effectiveness or visual Figma parity.
"""
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

pytest.importorskip('fastapi')
from fastapi.testclient import TestClient

from self_improve import apply
from self_improve.dashboard.app import create_app
from self_improve.execution_policy import policy_snapshot, waiting_proposals
from self_improve.pipeline import run_pipeline
from self_improve.store import Store
from self_improve.worker import run_once
from tests.e2e_corpus import build_corpus, ScriptedLLM, mine_payload
from tests.test_apply import insert_proposal, make_diff
from tests.test_dashboard_commands import request as approval_request
from tests.test_dashboard_rollback import request as rollback_request
from tests.test_e2e_pipeline import _capture_gate_wiring


def test_invented_history_through_review_delivery_recovery_and_rollback(tmp_path, monkeypatch):
    corpus = build_corpus(tmp_path)
    cfg = replace(corpus.cfg, production_repo_path=str(tmp_path / 'unused-production'))
    with closing(corpus.store) as store:
        target = Path(cfg.global_claude_md)
        target.parent.mkdir(parents=True)
        baseline = '# Instructions\n'
        target.write_text(baseline)
        gate = _capture_gate_wiring(monkeypatch, verdict='ungated')
        llm = ScriptedLLM(mine_responses=[mine_payload(
            '**Keep full raw output before parsing.**', scope_guess='global')])
        stats = run_pipeline(cfg, store, review_only=True, _llm_factory=llm.factory())
        proposals = store.query('SELECT * FROM proposals WHERE run_id=?', (stats['run_id'],))
        assert len(proposals) == 1 and gate['gate'] and llm.calls
        proposal = proposals[0]
        assert target.read_text() == baseline
        assert all(not p['enabled'] for p in policy_snapshot(store)['classes'].values())

        # The same lesson has a second scratch destination. Target rejection
        # must leave the original proposal available for explicit approval.
        other = tmp_path / 'other.md'
        other.write_text(baseline)
        rejected = insert_proposal(store, target=other, status='ungated',
                                   diff=make_diff(baseline, baseline + '- Example.\n'))
        store.update('proposals', 'id', rejected['id'], {'learning_id': proposal['learning_id']})
        store.commit()

        with TestClient(create_app(cfg)) as client:
            detail = client.get(f"/api/runs/{stats['run_id']}")
            assert detail.status_code == 200 and stats['run_id'] in detail.text
            rows = client.get('/api/rules').json()['rows']
            rule = next(r for r in rows if r['id'] == proposal['learning_id'])
            assert rule['provenance']['incidents'] and rule['evidence_linked'] > 0
            assert 'Keep full raw output' in rule['rule_text']
            body = approval_request(client, [rejected['id']], 'release-reject-target')
            response = client.post('/api/commands', json={**body, 'action': 'reject_target'})
            assert response.status_code == 202, response.text
            assert response.json()['members'][0]['decision_scope'] == 'target'
            assert {p['id'] for p in waiting_proposals(store, cfg)} == {proposal['id']}

            stale = approval_request(client, [proposal['id']], 'release-stale-review')
            store.update('learnings', 'id', proposal['learning_id'], {'why': 'Updated invented explanation.'})
            store.commit()
            response = client.post('/api/commands', json=stale)
            assert response.status_code == 409 and response.json()['error'] == 'StaleRevision'
            body = approval_request(client, [proposal['id']], 'release-fresh-review')
            response = client.post('/api/commands', json=body)
            assert response.status_code == 202, response.text
            command = response.json()
            assert command['state'] == 'queued'
            assert target.read_text() == baseline and other.read_text() == baseline
            assert not cfg.state_path('snapshots').exists()

            writes = []
            original_write = apply._atomic_write
            def write(*args):
                writes.append(args[0])
                return original_write(*args)
            def crash(stage, *_):
                if stage == 'file_replaced':
                    raise KeyboardInterrupt('invented crash after durable file replacement')
            monkeypatch.setattr(apply, '_atomic_write', write)
            monkeypatch.setattr(apply, '_delivery_checkpoint', crash)
            with pytest.raises(KeyboardInterrupt):
                run_once(store, cfg)
            delivered = target.read_text()
            assert 'Keep full raw output' in delivered and len(writes) == 1
            monkeypatch.setattr(apply, '_delivery_checkpoint', lambda *_: None)
            with closing(Store(store.db_path, migrate=False)) as restarted:
                result = run_once(restarted, cfg)
            assert result['state'] == 'completed' and result['id'] == command['id']
            assert len(writes) == 1
            assert client.get(f"/api/commands/{command['id']}").json()['state'] == 'completed'
            assert client.post('/api/commands', json=body).json()['id'] == command['id']
            assert run_once(store, cfg) is None
            assert client.get('/api/review-queue').json()['families'] == []

            # A changed base invalidates the inverse preview. A fresh inverse
            # preserves unrelated human content and reverses only this edit.
            inverse = rollback_request(client, proposal['id'], 'release-stale-inverse')
            addition = '\nHuman note: use the scratch project.\n'
            target.write_text(delivered + addition)
            response = client.post('/api/commands', json=inverse)
            assert response.status_code == 409 and response.json()['error'] == 'StaleRevision'
            inverse = rollback_request(client, proposal['id'], 'release-fresh-inverse')
            queued = client.post('/api/commands', json=inverse)
            assert queued.status_code == 202, queued.text
            assert target.read_text() == delivered + addition
            assert run_once(store, cfg)['state'] == 'completed'
            assert target.read_text() == baseline + addition
            assert client.get(queued.json()['status_url']).json()['state'] == 'completed'
            assert client.post('/api/commands', json=inverse).json()['id'] == queued.json()['id']
            assert run_once(store, cfg) is None
            assert client.get('/api/review-queue').json()['families'] == []
            assert other.read_text() == baseline
            assert len(store.query("SELECT * FROM proposal_events WHERE event='applied'")) == 1
            assert len(store.query("SELECT * FROM proposal_events WHERE event='rolled_back'")) == 1
            assert all(not p['enabled'] for p in policy_snapshot(store)['classes'].values())
