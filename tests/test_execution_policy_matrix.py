"""Independent permission matrix, exercised through readers and actual writers."""
from contextlib import closing
from dataclasses import replace
from itertools import product
from pathlib import Path
import json

import pytest

from self_improve import execution_policy as policy
from self_improve.apply import apply_proposal
from self_improve.commands import CommandError, submit_command
from self_improve.config import Config
from self_improve.dashboard import queries
from self_improve.destinations import read_destination, resolve_destination
from self_improve.report import generate
from self_improve.review import preview_selection
from self_improve.store import PROPOSAL_ACTIONS, PROPOSAL_STATUSES, Store
from self_improve.worker import run_once
from tests.test_apply import init_git_repo, insert_proposal, make_diff, run_git

# Deliberately independent of production classification sets. A new vocabulary
# item needs a reviewed decision here, not automatic inheritance of permission.
STATUSES = ('pending', 'gated_pass', 'gated_fail', 'ungated', 'inconclusive',
            'held', 'approved_user', 'rejected_user', 'applied', 'rolled_back', 'superseded')
TARGETS = {'global_claude_md': 'global', 'codex_global': 'global',
           'project_agents_md': 'project', 'project_claude_md': 'project',
           'rule_file': 'project', 'skill': 'skill', 'hook': 'hook'}
ORDINARY = {'add', 'edit', 'delete', 'new_skill', 'new_rule_file'}
MANUAL = {'delete_human_line', 'convert_to_hook', 'resolve_rollback', 'reapply', 'recover_rule'}
WAITING = {'pending', 'gated_pass', 'gated_fail', 'ungated', 'inconclusive', 'held', 'approved_user'}
UNKNOWN = 'future_unsupported_value'
BEFORE, AFTER = 'existing instruction\n', 'existing instruction\nnew instruction\n'


def test_declared_vocabulary_requires_an_independent_policy_classification():
    assert PROPOSAL_STATUSES == set(STATUSES)
    assert PROPOSAL_ACTIONS == ORDINARY | MANUAL
    assert policy.TARGET_CLASS == TARGETS
    assert set(policy.TARGET_CLASSES) == {'global', 'project', 'skill', 'hook'}
    assert policy.MANDATORY_REVIEW_ACTIONS == MANUAL


@pytest.mark.parametrize('enabled', [False, True])
def test_complete_status_target_action_permission_matrix(enabled):
    snapshot = {'classes': {name: {'enabled': enabled and name != 'hook',
                                  'enabled_at': '2020-01-01T00:00:00Z'}
                            for name in ('global', 'project', 'skill', 'hook')}}
    cases = 0
    for status, kind, action in product((*STATUSES, UNKNOWN), (*TARGETS, UNKNOWN), (*sorted(ORDINARY | MANUAL), UNKNOWN)):
        proposal = {'status': status, 'target_kind': kind, 'action': action,
                    'created_at': '2030-01-01T00:00:00Z'}
        expected = enabled and status == 'gated_pass' and action in ORDINARY and kind in TARGETS and kind != 'hook'
        result = policy.automatic_eligibility(proposal, snapshot)
        assert result['allowed'] is expected, (proposal, result)
        assert bool(result['reason']) is (not expected), (proposal, result)
        assert result['target_class'] == TARGETS.get(kind)
        cases += 1
    assert cases == 1056


@pytest.fixture
def environment(tmp_path):
    cfg = Config(state_dir=str(tmp_path / 'state'), global_claude_md=str(tmp_path / 'global.md'),
                 codex_global_agents_md=str(tmp_path / 'codex.md'), skills_dir=str(tmp_path / 'skills'),
                 production_repo_path=str(tmp_path / 'absent-production'))
    with closing(Store(cfg.state_path('state.db'))) as store:
        yield cfg, store, tmp_path


def target_for(root, kind):
    if TARGETS.get(kind) == 'project':
        repo = init_git_repo(root / 'project', 'AGENTS.md', BEFORE)
        return repo / 'AGENTS.md'
    target = root / 'instruction.md'
    target.write_text(BEFORE)
    return target


def propose(store, target, *, status='gated_pass', kind='global_claude_md', action='add'):
    return insert_proposal(store, target=target, diff=make_diff(BEFORE, AFTER),
                           status=status, target_kind=kind, action=action)


def assert_delivery(cfg, target, kind):
    destination = resolve_destination(cfg, str(target), kind)
    assert read_destination(destination)['content'] == AFTER
    assert target.read_text() == (BEFORE if TARGETS.get(kind) == 'project' else AFTER)


@pytest.mark.parametrize('kind', TARGETS)
@pytest.mark.parametrize('status', (*STATUSES, UNKNOWN))
def test_actual_automatic_writer_obeys_status_and_target_matrix(environment, kind, status):
    cfg, store, root = environment
    for cls in ('global', 'project', 'skill'):
        policy.set_class_policy(store, cls, True, now='2020-01-01T00:00:00Z')
    target = target_for(root, kind)
    proposal = propose(store, target, status=status, kind=kind)
    before_head = run_git(['rev-parse', 'HEAD'], target.parent) if TARGETS[kind] == 'project' else None
    result = apply_proposal(store, cfg, proposal)
    allowed = status == 'gated_pass' and kind != 'hook'
    assert result['outcome'] == ('applied' if allowed else 'held'), result
    if allowed:
        assert_delivery(cfg, target, kind)
        assert len(store.query('SELECT * FROM instruction_operations')) == 1
        assert cfg.state_path('snapshots').exists()
    else:
        assert target.read_text() == BEFORE
        assert store.query('SELECT * FROM instruction_operations') == []
        assert not cfg.state_path('snapshots').exists()
    if before_head:
        assert run_git(['rev-parse', 'HEAD'], target.parent) == before_head
        assert run_git(['status', '--porcelain'], target.parent) == ''
    assert store.query('SELECT * FROM llm_calls') == []


@pytest.mark.parametrize('kind', TARGETS)
@pytest.mark.parametrize('status', (*STATUSES, UNKNOWN))
def test_explicit_manual_commands_are_separate_from_automatic_policy(environment, kind, status):
    cfg, store, root = environment
    target = target_for(root, kind)
    proposal = propose(store, target, status=status, kind=kind)
    assert not any(row['enabled'] for row in policy.policy_snapshot(store)['classes'].values())
    with store.transaction():
        shown = preview_selection(store, cfg, [proposal['id']])
    request = {'action': 'approve', 'request_key': 'matrix-' + proposal['id'],
               'preview_revision': shown['revision'], 'members': [
                   {'proposal_id': member['proposal_id'], 'revision': member['revision']} for member in shown['members']]}
    if status not in WAITING:
        with pytest.raises(CommandError) as exc:
            submit_command(store, cfg, request)
        assert exc.value.code == 'NotWaiting'
        assert store.query('SELECT * FROM commands') == []
        assert run_once(store, cfg) is None
        assert target.read_text() == BEFORE
        return
    command = submit_command(store, cfg, request)
    assert target.read_text() == BEFORE, 'recording approval executed its edit'
    result = run_once(store, cfg)
    assert result['id'] == command['id'] and result['state'] == 'completed', result
    assert_delivery(cfg, target, kind)
    assert run_once(store, cfg) is None
    assert len(store.query("SELECT * FROM proposal_events WHERE event='applied'")) == 1
    assert store.query('SELECT * FROM llm_calls') == []


@pytest.mark.parametrize('field', ['target_kind', 'action'])
@pytest.mark.parametrize('unknown', ['', UNKNOWN])
def test_unknown_or_missing_vocabulary_never_authorizes_either_writer(environment, field, unknown):
    cfg, store, root = environment
    policy.set_class_policy(store, 'global', True, now='2020-01-01T00:00:00Z')
    target = target_for(root, 'global_claude_md')
    proposal = propose(store, target)
    store.update('proposals', 'id', proposal['id'], {field: unknown}); store.commit()
    proposal[field] = unknown
    assert apply_proposal(store, cfg, proposal)['outcome'] == 'held'
    with store.transaction():
        with pytest.raises(CommandError) as exc:
            preview_selection(store, cfg, [proposal['id']])
        assert exc.value.code == 'UnsupportedProposal'
    with pytest.raises(CommandError) as exc:
        submit_command(store, cfg, {'action': 'approve', 'request_key': 'bad-' + proposal['id'],
                                   'members': [{'proposal_id': proposal['id'], 'revision': '0' * 64}]})
    assert exc.value.code == 'UnsupportedProposal'
    assert run_once(store, cfg) is None
    assert target.read_text() == BEFORE
    assert store.query('SELECT * FROM commands') == []
    assert store.query('SELECT * FROM instruction_operations') == []


@pytest.mark.parametrize('enabled', [False, True])
def test_review_counts_and_report_match_the_independent_permission_set(environment, enabled):
    cfg, store, root = environment
    # Exercise a configured extra review action separately from mandatory actions.
    cfg = replace(cfg, review_queue_actions=('edit',))
    if enabled:
        for cls in ('global', 'project', 'skill'):
            policy.set_class_policy(store, cls, True, now='2020-01-01T00:00:00Z')
    for run, veto in [('ordinary-run', False), ('review-only-run', True)]:
        store.insert('runs', {'id': run, 'started': '2030-01-01T00:00:00Z',
                             'finished': '2030-01-01T01:00:00Z', 'status': 'ok',
                             'stats_json': json.dumps({'review_only': veto})})
    expected, automatic, cases = set(), set(), []
    for status, kind in product(STATUSES, TARGETS):
        cases.append((status, kind, 'add', 'ordinary-run', False))
    for kind in TARGETS:
        # Generated actions require their real retained origins, exercised below.
        for action in ('delete_human_line', 'convert_to_hook', 'edit'):
            cases.append(('gated_pass', kind, action, 'ordinary-run', False))
        cases.append(('gated_pass', kind, 'add', 'review-only-run', False))
        cases.append(('gated_pass', kind, 'add', 'ordinary-run', True))
    for index, (status, kind, action, run, rollback) in enumerate(cases):
        target = root / f'case-{index}.md'; target.write_text(BEFORE)
        row = propose(store, target, status=status, kind=kind, action=action)
        store.update('proposals', 'id', row['id'], {'run_id': run})
        if rollback:
            old = propose(store, root / f'old-{index}.md', status='rolled_back')
            store.update('proposals', 'id', old['id'], {'learning_id': row['learning_id']})
        if status in WAITING:
            eligible = enabled and status == 'gated_pass' and action == 'add' and kind != 'hook' and run == 'ordinary-run' and not rollback
            (automatic if eligible else expected).add(row['id'])
    unknown = propose(store, root / 'unknown.md', status=UNKNOWN)
    store.commit()
    queue = queries.review_queue(store, cfg)
    listed = {row['id'] for family in queue['families'] for row in family['proposals']}
    assert listed == expected
    assert set(queries.waiting_proposal_ids(store, cfg)) == expected
    inbox = queries.inbox(store, cfg)
    assert inbox['count'] == queue['count'] == len(expected)
    assert inbox['auto_apply_pending'] == queue['auto_apply_pending'] == len(automatic)
    assert inbox['unknown_statuses'] == queue['unknown_statuses'] == {UNKNOWN: 1}
    assert unknown['id'] not in listed
    for run in ('ordinary-run', 'review-only-run'):
        report = Path(generate(store, cfg, run, root / f'{run}.md')).read_text()
        section = report.split('## Held / review queue\n', 1)[1].split('\n## Budget', 1)[0]
        rows = store.query('SELECT id,target_path FROM proposals WHERE run_id=?', (run,))
        assert {row['id'] for row in rows if '`' + row['target_path'] + '`' in section} == expected & {row['id'] for row in rows}
    assert all((root / f'case-{index}.md').read_text() == BEFORE for index in range(len(cases)))
    assert store.query('SELECT * FROM llm_calls') == []
    assert store.query('SELECT * FROM commands') == []


# These fixtures create genuine retained origin chains with invented responses.
# They never launch a provider; direct fabricated recovery rows are not valid.
from tests.test_apply import cfg, store  # noqa: E402,F401
from tests.test_delivery_worker import env, approve  # noqa: E402,F401
from tests.test_rollback_resolutions import conflict  # noqa: E402,F401
from tests.test_reapplications import undone  # noqa: E402,F401
from tests.test_recovery_jobs import env as recovery_env  # noqa: E402,F401


def assert_generated_action_requires_manual_delivery(cfg, store, pid):
    for cls in ('global', 'project', 'skill'):
        policy.set_class_policy(store, cls, True, now='2020-01-01T00:00:00Z')
    store.update('proposals', 'id', pid, {'status': 'gated_pass'}); store.commit()
    draft = store.query_one('SELECT * FROM proposals WHERE id=?', (pid,))
    target = Path(draft['target_path']); before = target.read_bytes()
    outcome = apply_proposal(store, cfg, draft)
    assert outcome['outcome'] == 'held' and outcome['reason'] == 'action_review_queue'
    assert target.read_bytes() == before
    queue = queries.review_queue(store, cfg)
    assert {p['id'] for f in queue['families'] for p in f['proposals']} == {pid}
    assert queue['count'] == queries.inbox(store, cfg)['count'] == 1
    assert queue['auto_apply_pending'] == queries.inbox(store, cfg)['auto_apply_pending'] == 0
    if draft['run_id']:
        text = Path(generate(store, cfg, draft['run_id'], target.parent / 'origin-report.md')).read_text()
        held = text.split('## Held / review queue\n', 1)[1].split('\n## Budget', 1)[0]
        assert '`' + str(target) + '`' in held
    for cls in ('global', 'project', 'skill'):
        policy.set_class_policy(store, cls, False)
    command = approve(store, cfg, draft)
    result = run_once(store, cfg)
    assert result['id'] == command['id'] and result['state'] == 'completed', result
    assert target.read_bytes() != before
    assert run_once(store, cfg) is None


def test_resolution_origin_obeys_automatic_and_manual_policy(conflict):
    from self_improve import job_worker
    from tests.test_rollback_resolutions import request, RESOLVED
    cfg, store, target, _, calls = conflict
    submit_command(store, cfg, request(conflict))
    result = job_worker.run_once(store, cfg)
    assert result['state'] == 'completed' and len(calls) == 1
    assert_generated_action_requires_manual_delivery(cfg, store, result['result']['proposal_id'])
    assert target.read_text() == RESOLVED and len(calls) == 1


def test_reapplication_origin_obeys_automatic_and_manual_policy(undone):
    from tests.test_reapplications import request, REAPPLIED
    cfg, store, target, _ = undone
    result = submit_command(store, cfg, request(undone))
    assert_generated_action_requires_manual_delivery(cfg, store, result['result']['proposal_id'])
    assert target.read_text() == REAPPLIED
    assert store.query('SELECT * FROM llm_calls') == []


def test_recovery_origin_obeys_automatic_and_manual_policy(recovery_env):
    from self_improve import job_worker
    from tests.test_recovery_jobs import request
    cfg, store, _, calls = recovery_env
    submit_command(store, cfg, request(recovery_env))
    result = job_worker.run_once(store, cfg)
    assert result['state'] == 'completed' and len(calls) == 1
    assert_generated_action_requires_manual_delivery(cfg, store, result['result']['proposal_id'])
    assert 'original request identifier' in Path(cfg.global_claude_md).read_text() and len(calls) == 1


@pytest.mark.parametrize('action', sorted(ORDINARY))
def test_configured_ordinary_review_action_has_a_working_api_explanation(environment, action):
    from fastapi.testclient import TestClient
    from self_improve.dashboard.app import create_app
    cfg, store, root = environment
    cfg = replace(cfg, review_queue_actions=(action,))
    policy.set_class_policy(store, 'global', True, now='2020-01-01T00:00:00Z')
    target = target_for(root, 'global_claude_md')
    proposal = propose(store, target, action=action)
    before = list(store.conn.iterdump())
    with TestClient(create_app(cfg)) as client:
        response = client.get('/api/review-queue')
        assert response.status_code == 200, response.text
        queue = response.json()
        assert queue['count'] == 1 and queue['auto_apply_pending'] == 0
        member = queue['families'][0]['proposals'][0]
        assert member['id'] == proposal['id'] and member['reason_code'] == action
        assert 'configuration requires manual review' in member['why_needs_you']
        assert queue['carve_out_summary'][action] == 1
    assert target.read_text() == BEFORE
    assert list(store.conn.iterdump()) == before
