"""Copy the shipped module dependency alongside isolated Node app probes."""
from pathlib import Path


def copy_spa_dependencies(directory):
    static=Path(__file__).resolve().parents[1]/'src/self_improve/dashboard/static'
    (directory/'navigation.js').write_bytes((static/'navigation.js').read_bytes())
    (directory/'package.json').write_text('{"type":"module"}\n')
