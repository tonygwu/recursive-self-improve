"""Serve invented evaluation history from a temporary, isolated Store."""
from pathlib import Path
from datetime import datetime, timezone
import json, tempfile, sys
root_repo=Path(__file__).resolve().parents[2];sys.path.insert(0,str(root_repo))
import pytest, uvicorn
from self_improve.config import Config
from self_improve.store import Store
from self_improve import eval_history
from self_improve.dashboard.app import create_app
from tests.test_model_jobs import env
from tests.test_eval_history import execute
out=Path(__file__).resolve().parents[2]/"reports/dashboard-parity"
out.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(prefix='si-eval-ui-') as temp, pytest.MonkeyPatch.context() as patch:
    root=Path(temp).resolve();db=Store(root/'state/state.db')
    context=env.__wrapped__(Config(state_dir=str(root/'state')),db,root,patch)
    cfg,db,proposal,calls=context
    db.update('learnings','id',proposal['learning_id'],{'rule_text':'Inspect generated output before replacing a configuration file.','why':'Invented evidence for an offline dashboard fixture.'});db.commit()
    learning=db.query_one('SELECT * FROM learnings WHERE id=?',(proposal['learning_id'],))
    for i in range(22):
        record=eval_history.begin(db,cfg,learning,proposal)
        record.stop(ValueError('Invented initialization stop '+str(i)))
    result,attempt=execute(context)
    original=attempt['scenarios'][0]['evaluation']
    for i in range(23):db.insert('eval_results',{**original,'id':'historical-'+str(i).zfill(2),'subject_id':'invented-unlinked-subject','kind':'ab' if i==22 else 'regression'})
    db.commit()
    manifest={'root':str(root),'db':str(db.db_path),'attempt':attempt['source']['id'],'learning':learning['id'],'proposal':proposal['id'],'run':result['run_id'],'calls':len(calls),'historical':'historical-22'}
    db.close()
    (out/'eval-demo-manifest.json').write_text(json.dumps(manifest))
    uvicorn.run(create_app(cfg,clock=lambda:datetime(2026,9,15,tzinfo=timezone.utc)),host='127.0.0.1',port=8876)
