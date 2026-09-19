"""Serve native session evidence from a disposable, invented scan and delivery."""
from pathlib import Path
import json, tempfile, sys
root_repo=Path(__file__).resolve().parents[2];sys.path.insert(0,str(root_repo))
import uvicorn
from self_improve.dashboard.app import create_app
from tests.test_session_context import env as context_env,meta,turn,at,applied
from tests.test_scan_observations import scan,write_claude,c_user,PROJECT
from tests.test_scan_occurrences import write_codex,x_user
out=root_repo/'reports/dashboard-parity';out.mkdir(parents=True,exist_ok=True)
with tempfile.TemporaryDirectory(prefix='si-session-ui-') as temp:
    root=Path(temp).resolve();generator=context_env.__wrapped__(root);fixture=next(generator)
    for i in range(22):
        when=at(1) if i==0 else at(3)
        record=meta(fixture.repo,when,sid=f'invented-session-{i:02}')
        if i==1: record['payload'].pop('timestamp')
        if i==2: record['payload']['forked_from_id']='invented-parent'
        write_codex(fixture,[record,turn(fixture.repo,at(4)),x_user('Invented activity',at(7))],name=f'rollout-{i}.jsonl')
    write_claude(fixture,[c_user('Invented Claude activity',at(4),fixture.repo)])
    assert scan(fixture,'browser-fixture').files_succeeded==23
    revision,repo,text=applied(fixture,last_check=8)
    manifest={'root':str(root),'db':str(fixture.store.db_path),'project_key':PROJECT,'revision':revision['id'],
              'target':str(repo/'AGENTS.md'),'target_text':(repo/'AGENTS.md').read_text(),
              'snapshot':list(fixture.store.conn.iterdump())}
    (out/'session-demo-manifest.json').write_text(json.dumps(manifest))
    try:uvicorn.run(create_app(fixture.cfg),host='127.0.0.1',port=8876)
    finally:generator.close()
