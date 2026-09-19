"""Serve a disposable inventory collected from invented command files."""
from pathlib import Path
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

out = ROOT/'reports/dashboard-parity'
out.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(prefix='si-command-ui-') as temp:
    root = Path(temp).resolve()
    config = cfg.__wrapped__(root)
    generator = store.__wrapped__(root)
    db = next(generator)
    repo = init_git_repo(root/'invented-project', 'README.md', '# Invented project\n')
    key = known_copy(db, repo)
    files = [write(repo/'.claude/commands/deploy.md', 'Legacy deployment instructions.\n'),
             write(repo/'.claude/skills/deploy/SKILL.md', '---\nname: Display label only\n---\nCurrent deployment skill.\n'),
             write(repo/'.claude/commands/frontend/check.md', '---\npaths: ["src/**"]\n---\n@support.txt\n!`never-run`\n'),
             write(Path(config.global_claude_md).parent/'commands/report.md', 'Invented global report.\n')]
    for i in range(20):
        files.append(write(repo/f'.claude/commands/check-{i:02}.md', f'Invented check {i}.\n'))
    collect_availability(db, config, observed_at='2030-01-01T00:00:00Z')
    info = {'project_key': key, 'db': str(db.db_path), 'snapshot': list(db.conn.iterdump()),
            'targets': {str(path): path.read_text() for path in files}}
    (out/'commands-demo-manifest.json').write_text(json.dumps(info))
    try:
        uvicorn.run(create_app(config), host='127.0.0.1', port=8876)
    finally:
        generator.close()
