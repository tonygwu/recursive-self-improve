"""Serve real embedded-policy collection from a disposable invented installation."""
from pathlib import Path
import argparse
import json
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import uvicorn
from self_improve.dashboard.app import create_app
from self_improve.rule_availability import collect_availability
from tests.test_rule_availability import cfg, store, known_copy, init_git_repo
from tests.test_instruction_surfaces import write

parser = argparse.ArgumentParser()
parser.add_argument('--case', choices=['selected','failed','absent'], default='selected')
case = parser.parse_args().case
port = {'selected':8876,'failed':8877,'absent':8878}[case]
out = ROOT/'reports/dashboard-parity'; out.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(prefix='si-policy-ui-') as temp:
    root = Path(temp).resolve(); config = cfg.__wrapped__(root)
    generator = store.__wrapped__(root); db = next(generator)
    repo = init_git_repo(root/'invented-project', 'README.md', '# Invented project\n')
    key = known_copy(db, repo); targets = []
    if case != 'absent':
        managed = Path(config.claude_managed_dir)
        targets.append(write(managed/'managed-settings.json', json.dumps({'claudeMd':'Initial fixture guidance.\n', 'env':{'UNRELATED':'PRIVATE_SETTINGS_SENTINEL'}})))
        for index in range(22):
            targets.append(write(managed/f'managed-settings.d/{index:02}-team.json', json.dumps({'claudeMd':f'Invented team {index} guidance.\nRetained policy text sentinel {index}.\n'})))
        if case == 'failed': targets.append(write(managed/'managed-settings.d/99-broken.json','{"claudeMd":'))
    collect_availability(db, config, observed_at='2030-01-01T00:00:00Z')
    info = {'project_key':key,'db':str(db.db_path),'snapshot':list(db.conn.iterdump()),
            'managed_root':config.claude_managed_dir, 'targets':{str(p):p.read_text() for p in targets}}
    (out/f'policy-{case}-manifest.json').write_text(json.dumps(info))
    try: uvicorn.run(create_app(config), host='127.0.0.1', port=port)
    finally: generator.close()
