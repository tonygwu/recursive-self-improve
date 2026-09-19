"""Invented mixed and many-result Review families; no private or model access."""
from pathlib import Path
from datetime import datetime, timezone
import json
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import pytest
import uvicorn
from tests import conftest as boundary
from tests.test_review_detail import build_review
from tests.test_apply import insert_proposal, make_diff, OLD, NEW
from self_improve.dashboard.app import create_app

OUT = ROOT / 'reports/dashboard-parity/review-decisions'
OUT.mkdir(parents=True, exist_ok=True)


def guard(event, args):
    boundary._audit(event, args)
    if boundary._violations:
        raise AssertionError('Private access forbidden in Review decisions')


boundary._armed = True
sys.addaudithook(guard)
with tempfile.TemporaryDirectory(prefix='si-review-decisions-') as folder, pytest.MonkeyPatch.context() as patch:
    cfg, store, info = build_review(Path(folder).resolve(), patch)
    try:
        many = []
        verdicts = ['ungated', 'gated_fail', 'gated_pass', 'unknown'] * 2
        for i, verdict in enumerate(verdicts + [None]):
            proposal = insert_proposal(store, target=Path(cfg.global_claude_md), diff=make_diff(OLD, NEW), status='pending')
            if many:
                store.update('proposals', 'id', proposal['id'], {'learning_id': many[0]['learning_id']})
            result = 'invented-result-' + str(i) + ('-long-identity' * 18 if i == 3 else '')
            if verdict:
                store.insert('eval_results', {'id': result, 'kind': 'self_eval', 'verdict': verdict})
                store.update('proposals', 'id', proposal['id'], {'eval_result_id': result})
            many.append(proposal)
        store.update('learnings', 'id', many[0]['learning_id'], {'rule_text': 'Inspect every retained outcome before deciding on a large selection.'})
        store.commit()
        info.update(many=many[0]['learning_id'], db=str(store.db_path), snapshot=list(store.conn.iterdump()), models=0,
                    targets={cfg.global_claude_md: Path(cfg.global_claude_md).read_text()})
        (OUT / 'manifest.json').write_text(json.dumps(info))
        uvicorn.run(create_app(cfg, clock=lambda: datetime(2030, 1, 2, tzinfo=timezone.utc)), host='127.0.0.1', port=8876)
    finally:
        store.close()
