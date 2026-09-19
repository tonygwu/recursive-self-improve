"""Disposable native hook reports produced through the exact generated command."""
from pathlib import Path
import json,subprocess,sys,tempfile
root_repo=Path(__file__).resolve().parents[2];sys.path.insert(0,str(root_repo))
import uvicorn
from self_improve.dashboard.app import create_app
from self_improve.native_loads import hook_settings
from tests.test_session_context import env as context_env
from tests.test_native_loads import event,config_file
from tests.test_scan_observations import scan,write_claude,c_user,ts,PROJECT
out=root_repo/'reports/dashboard-parity';out.mkdir(parents=True,exist_ok=True)
with tempfile.TemporaryDirectory(prefix='si-native-ui-') as temp:
    root=Path(temp).resolve();generator=context_env.__wrapped__(root);fixture=next(generator)
    try:
        write_claude(fixture,[c_user('Invented UI evidence',ts(0),fixture.repo)])
        scan(fixture,'native-ui-fixture')
        config=config_file(fixture)
        command=hook_settings(config_path=config,python_path=sys.executable)['hooks']['InstructionsLoaded'][0]['hooks'][0]['command']
        target=Path(fixture.repo)/'CLAUDE.md';target.write_text('Invented instruction text.\n')
        for i in range(23):
            payload=event(fixture,session_id='native-session-'+str(i%3),load_reason='compact' if i%2 else 'session_start')
            if i==22:payload=event(fixture,'PostCompact',trigger='auto',compact_summary='omitted invented text')
            completed=subprocess.run(command,shell=True,cwd=root,input=json.dumps(payload),text=True,capture_output=True)
            assert completed.returncode==0,completed.stderr
            assert completed.stdout==completed.stderr==''
        manifest={'db':str(fixture.store.db_path),'project_key':PROJECT,'target':str(target),
                  'target_text':target.read_text(),'snapshot':list(fixture.store.conn.iterdump())}
        (out/'native-demo-manifest.json').write_text(json.dumps(manifest))
        uvicorn.run(create_app(fixture.cfg),host='127.0.0.1',port=8876)
    finally:generator.close()
