"""Disposable 45-rule/23-family browser fixture; no private inputs or models."""
from pathlib import Path
from datetime import datetime, timezone
import json, sys, tempfile
from dataclasses import replace
root_repo=Path(__file__).resolve().parents[2];sys.path.insert(0,str(root_repo))
import uvicorn
from tests.test_rule_browser import env
from self_improve.dashboard.app import create_app
from self_improve import mining_history
out=root_repo/'reports/dashboard-parity';out.mkdir(parents=True,exist_ok=True)
with tempfile.TemporaryDirectory(prefix='si-rules-ui-') as temp:
    root=Path(temp).resolve();generator=env.__wrapped__(root);store,cfg,_=next(generator)
    try:
        cfg=replace(cfg,**{key:str(root/('unused-'+key)) for key in ('claude_projects_dir','claude_history_path','codex_sessions_dir','codex_archived_dir','global_claude_md','codex_global_agents_md','skills_dir','production_repo_path')})
        store.insert('sessions',{'file_path':'invented-session','source':'codex','session_id':'synthetic-session','project_key':'synthetic:example'})
        for i in range(7):
            store.insert('incidents',{'id':f'evidence-{i}','session_file':'invented-session','session_id':'synthetic-session','project_key':'synthetic:example','signal_type':'correction','matched_text':'Preserve the invented output before retrying.',
                'window_json':json.dumps([{'role':'user','text':'sixth_signal_sentinel' if i==5 else 'Inspect the invented error before retrying.'}]),'created_at':'2030-01-01T00:00:00Z'})
            store.link_incident_learning(f'evidence-{i}','single-21')
        learning=store.query_one("SELECT * FROM learnings WHERE id='single-21'")
        mining_history.append(store,learning,before=None,kind='new',incidents=[],provenance={'generation':'agentic','run_id':'invented-run'})
        store.commit()
        (out/'rule-browser-manifest.json').write_text(json.dumps({'db':str(store.db_path),'snapshot':list(store.conn.iterdump())}))
        uvicorn.run(create_app(cfg,clock=lambda:datetime(2030,2,2,tzinfo=timezone.utc)),host='127.0.0.1',port=8876)
    finally:generator.close()
