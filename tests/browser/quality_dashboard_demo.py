"""Serve invented applied revisions and human samples from disposable state."""
from pathlib import Path
import json, tempfile, sys
root_repo=Path(__file__).resolve().parents[2];sys.path.insert(0,str(root_repo))
import uvicorn
from self_improve.dashboard.app import create_app
from self_improve.store import new_id
from tests.test_delivery_worker import env as delivery_env
from tests.test_quality_policy import env as quality_env, delivered, create_sample
out=root_repo/'reports/dashboard-parity';out.mkdir(parents=True,exist_ok=True)
with tempfile.TemporaryDirectory(prefix='si-quality-ui-') as temp:
    root=Path(temp).resolve();generator=delivery_env.__wrapped__(root)
    fixture=quality_env.__wrapped__(next(generator),root)
    cfg,store,target=fixture
    paths=[]
    for i,kind in enumerate(('global_claude_md','global_claude_md','hook','skill')):
        _,path,_=delivered(fixture,i,target_kind=kind);paths.append(path)
    for i in range(21):create_sample(fixture,size=1,seed='invented-'+str(i))
    # Retained historical application has no exact source and must remain excluded.
    p=store.query_one('SELECT id FROM proposals ORDER BY id LIMIT 1')
    store.insert('proposal_events',{'id':new_id(),'proposal_id':p['id'],'event':'applied','ts':'2026-01-01T00:00:00Z','actor':'pipeline','note':'{}'});store.commit()
    manifest={'root':str(root),'db':str(store.db_path),'targets':{str(p):p.read_text() for p in paths+[target]},'calls':0,'sample_count':21}
    (out/'quality-demo-manifest.json').write_text(json.dumps(manifest))
    try:uvicorn.run(create_app(cfg),host='127.0.0.1',port=8876)
    finally:generator.close()
