"""Serve a temporary actual managed-file collection, with no provider processes."""
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
parser.add_argument('--absent', action='store_true')
args = parser.parse_args()
case, port = ('absent', 8877) if args.absent else ('observed', 8876)
out = ROOT/'reports/dashboard-parity'
out.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(prefix='si-managed-ui-') as temp:
    root = Path(temp).resolve()
    config = cfg.__wrapped__(root)
    generator = store.__wrapped__(root)
    db = next(generator)
    repo = init_git_repo(root/'invented-project', 'README.md', '# Invented project\n')
    key = known_copy(db, repo)
    files = []
    if not args.absent:
        managed = Path(config.claude_managed_dir)
        files += [write(managed/'CLAUDE.md', '@./company.txt\nManaged fixture guidance.\n'),
                  write(managed/'company.txt', 'Use temporary resources for tests.\n'),
                  write(managed/'.claude/skills/broken/SKILL.md', '---\nname: []\n---\nInvalid fixture metadata.\n')]
        for i in range(22):
            files.append(write(managed/f'.claude/skills/check-{i:02}/SKILL.md', f'Invented enterprise check {i}.\n'))
        global_file = Path(config.global_claude_md)
        global_file.parent.mkdir(parents=True)
        global_file.symlink_to(managed/'company.txt')
    collect_availability(db, config, observed_at='2030-01-01T00:00:00Z')
    info = {'project_key':key,'db':str(db.db_path),'snapshot':list(db.conn.iterdump()),
            'managed_root': config.claude_managed_dir, 'targets':{str(p):p.read_text() for p in files}}
    (out/f'managed-{case}-manifest.json').write_text(json.dumps(info))
    try:
        uvicorn.run(create_app(config), host='127.0.0.1', port=port)
    finally:
        generator.close()
