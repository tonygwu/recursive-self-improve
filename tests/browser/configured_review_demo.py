"""Five valid ordinary actions configured for Review; invented temporary data."""
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import json
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import uvicorn
from self_improve.dashboard.app import create_app
from self_improve.execution_policy import set_class_policy
from self_improve.llm import LLMRunner
from self_improve.store import Store
from tests import conftest as boundary
from tests.test_rule_availability import cfg as fixture_config
from tests.test_apply import insert_proposal, make_diff, init_git_repo


def refuse_private(event, args):
    boundary._audit(event, args)
    if boundary._violations:
        raise AssertionError('Configured Review fixture attempted private access')


def refuse_models(*args, **kwargs):
    raise AssertionError('Configured Review cannot execute models')


boundary._armed = True
sys.addaudithook(refuse_private)
LLMRunner._execute = refuse_models
OUT = ROOT / 'reports/dashboard-parity/configured-review'
OUT.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(prefix='si-configured-review-') as temp:
    root = Path(temp).resolve()
    actions = ('add', 'edit', 'delete', 'new_skill', 'new_rule_file')
    cfg = replace(fixture_config.__wrapped__(root), review_queue_actions=actions,
                  claude_history_path=str(root / 'no-history'), production_repo_path=str(root / 'no-production'))
    project = init_git_repo(root / 'project', 'AGENTS.md', '# Project instructions\n')
    with closing(Store(cfg.state_path('state.db'))) as store:
        for cls in ('global', 'project', 'skill'):
            set_class_policy(store, cls, True, now='2020-01-01T00:00:00Z')
        families, targets = {}, {}
        for action in actions:
            kind = 'skill' if action == 'new_skill' else 'rule_file' if action == 'new_rule_file' else 'global_claude_md'
            target = project / 'new-rule.md' if action == 'new_rule_file' else root / (action + '.md')
            before = '' if action.startswith('new_') else 'Keep the original invented instruction.\n'
            after = '' if action == 'delete' else 'Read the invented fixture before editing.\n'
            if before:
                target.write_text(before)
            row = insert_proposal(store, target=target, diff=make_diff(before, after), target_kind=kind, action=action)
            store.update('learnings', 'id', row['learning_id'], {'rule_text': f'Review the configured {action.replace("_", " ")} action.'})
            families[row['learning_id']] = action
            targets[str(target)] = {'exists': target.exists(), 'content': before}
        store.commit()
        (OUT / 'manifest.json').write_text(json.dumps({'root': str(root), 'db': str(store.db_path),
            'snapshot': list(store.conn.iterdump()), 'families': families, 'targets': targets}))
        uvicorn.run(create_app(cfg), host='127.0.0.1', port=8876)
