"""Run the populated evidence fixture with private-resource access forbidden."""
from pathlib import Path
import runpy
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tests import conftest as boundary


def refuse_private_access(event, args):
    boundary._audit(event, args)
    if boundary._violations:
        raise AssertionError('Navigation fixture attempted private-resource access')


boundary._armed = True
sys.addaudithook(refuse_private_access)
# This existing fixture refuses model calls and retains its complete Store/targets.
runpy.run_path(str(ROOT / 'tests/browser/evidence_browser_demo.py'), run_name='__main__')
