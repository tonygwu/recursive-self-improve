"""Serve only the temporary larger performance fixture; no private inputs or models."""
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import uvicorn
from tests.dashboard_performance_fixture import app, build, install_guard

install_guard()
with tempfile.TemporaryDirectory(prefix='si-performance-ui-') as directory:
    with build(Path(directory).resolve()) as (env, manifest):
        uvicorn.run(app(env), host='127.0.0.1', port=8876)
