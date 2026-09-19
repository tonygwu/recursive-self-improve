"""Serve the public demo for read-only, seven-screen baseline inspection."""
from contextlib import closing
from dataclasses import replace
from pathlib import Path
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


def refuse_models(*args, **kwargs):
    raise AssertionError("No model calls in the acceptance fixture")


def refuse_private_access(event, args):
    boundary._audit(event, args)
    if boundary._violations:
        raise AssertionError("Acceptance fixture attempted a private resource")


LLMRunner._execute = refuse_models
boundary._armed = True
sys.addaudithook(refuse_private_access)
out = ROOT / "reports/dashboard-parity/acceptance"
out.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(prefix="si-acceptance-ui-") as folder:
    root = Path(folder).resolve()
    cfg = replace(seed(root), claude_managed_dir=str(root / "managed"))
    with closing(Store(cfg.state_path("state.db"), read_only=True)) as store:
        manifest = {
            "db": str(store.db_path),
            "snapshot": list(store.conn.iterdump()),
            "targets": {str(p): p.read_text() for p in root.glob("projects/*/AGENTS.md")},
            "fixture": "public demo; sparse history, no recorded measurements",
        }
    (out / "manifest.json").write_text(json.dumps(manifest))
    uvicorn.run(create_app(cfg, clock=lambda: NOW), host="127.0.0.1", port=8876)
