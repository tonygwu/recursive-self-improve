"""Guarded populated or empty temporary Store for cross-screen read-only checks."""
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import argparse
import hashlib
import json
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import uvicorn
from examples.dashboard_demo import NOW, seed
from self_improve.dashboard.app import create_app
from self_improve.llm import LLMRunner
from self_improve.store import Store
from tests import conftest as boundary
from tests.test_rule_availability import cfg as fixture_config


def refuse_private(event, args):
    boundary._audit(event, args)
    if boundary._violations:
        raise AssertionError('Accessibility fixture attempted a private resource')


def refuse_models(*args, **kwargs):
    raise AssertionError('No models in the accessibility fixture')


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--empty', action='store_true')
args = parser.parse_args()
boundary._armed = True
sys.addaudithook(refuse_private)
LLMRunner._execute = refuse_models
out = ROOT / 'reports/dashboard-parity/accessibility'
out.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(prefix='si-accessibility-ui-') as folder:
    root = Path(folder).resolve()
    cfg = fixture_config.__wrapped__(root) if args.empty else seed(root)
    cfg = replace(cfg, claude_managed_dir=str(root/'managed'), codex_skills_dir=str(root/'codex-skills'),
                  claude_history_path=str(root/'history.jsonl'), production_repo_path=str(root/'production'))
    with closing(Store(cfg.state_path('state.db'))) as store:
        manifest = {'db':str(store.db_path), 'snapshot':list(store.conn.iterdump()),
                    'targets':{str(p):p.read_text() for p in root.rglob('AGENTS.md')},
                    'empty':args.empty,
                    'source_hashes':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
                                     for p in (ROOT/'src/self_improve').rglob('*')
                                     if p.suffix in {'.py','.js','.css','.html','.svg'}}}
    (out/'manifest.json').write_text(json.dumps(manifest))
    uvicorn.run(create_app(cfg, clock=lambda:NOW), host='127.0.0.1', port=8876)
