"""Invented complete Review evidence; all state and targets are temporary."""
from pathlib import Path
from datetime import datetime,timezone
import json,sys,tempfile
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
import pytest,uvicorn
from tests import conftest as boundary
from tests.test_review_detail import build_review
from self_improve.dashboard.app import create_app
OUT=ROOT/'reports/dashboard-parity/review-detail';OUT.mkdir(parents=True,exist_ok=True)
def guard(event,args):
    boundary._audit(event,args)
    if boundary._violations:raise AssertionError('Private access forbidden in Review detail')
boundary._armed=True;sys.addaudithook(guard)
with tempfile.TemporaryDirectory(prefix='si-review-detail-') as folder,pytest.MonkeyPatch.context() as patch:
    cfg,store,info=build_review(Path(folder).resolve(),patch)
    try:
        info.update(db=str(store.db_path),snapshot=list(store.conn.iterdump()),models=0,
                    targets={cfg.global_claude_md:Path(cfg.global_claude_md).read_text()})
        (OUT/'manifest.json').write_text(json.dumps(info))
        uvicorn.run(create_app(cfg,clock=lambda:datetime(2030,1,2,tzinfo=timezone.utc)),host='127.0.0.1',port=8876)
    finally:store.close()
