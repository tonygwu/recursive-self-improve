"""Serve invented native plugin observations from a disposable installation."""
from pathlib import Path
import argparse
import json
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
import uvicorn
from self_improve.dashboard.app import create_app
from self_improve.rule_availability import collect_availability
from tests.test_rule_availability import cfg,store,known_copy,init_git_repo
from tests.test_instruction_surfaces import write

parser=argparse.ArgumentParser()
parser.add_argument('--case',choices=['populated','failed','absent'],default='populated')
case=parser.parse_args().case
port={'populated':8876,'failed':8877,'absent':8878}[case]
out=ROOT/'reports/dashboard-parity';out.mkdir(parents=True,exist_ok=True)
with tempfile.TemporaryDirectory(prefix='si-plugin-ui-') as temp:
    root=Path(temp).resolve();config=cfg.__wrapped__(root)
    generator=store.__wrapped__(root);db=next(generator)
    repo=init_git_repo(root/'invented-project','README.md','# Invented project\n')
    key=known_copy(db,repo);targets=[];payload=root/'plugin-payload'
    registry=Path(config.global_claude_md).parent/'plugins/installed_plugins.json'
    if case=='populated':
        targets.append(write(registry,json.dumps({'version':2,'plugins':{'example@fixture':[{'scope':'user','installPath':str(payload),'version':'1.0'}]}})))
        targets.append(write(payload/'.claude-plugin/plugin.json','{"name":"example","version":"2.0"}'))
        for index in range(18):
            targets.append(write(payload/f'skills/guide-{index:02}/SKILL.md',f'---\nname: guide-{index:02}\n---\nInvented plugin guidance {index}.\nComplete retained plugin sentinel {index}.\n'))
        targets.append(write(Path(config.global_claude_md).parent/'settings.json',json.dumps({'enabledPlugins':{'example@fixture':False},'env':{'PRIVATE':'UNRELATED_SETTINGS_SENTINEL'}})))
        targets.append(write(Path(config.skills_dir)/'bundle/.claude-plugin/plugin.json','{"name":"bundle"}'))
        targets.append(write(Path(config.skills_dir)/'bundle/SKILL.md','Root bundle guidance.\n'))
    elif case=='failed':targets.append(write(registry,'{"version":999,"plugins":{}}'))
    collect_availability(db,config,observed_at='2030-01-01T00:00:00Z')
    info={'project_key':key,'db':str(db.db_path),'snapshot':list(db.conn.iterdump()),'payload':str(payload),
          'targets':{str(p):p.read_text() for p in targets}}
    (out/f'plugins-{case}-manifest.json').write_text(json.dumps(info))
    try:uvicorn.run(create_app(config),host='127.0.0.1',port=port)
    finally:generator.close()
