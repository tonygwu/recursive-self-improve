"""Real scratch file/ref writes driven by frozen dashboard commands."""
from contextlib import closing, contextmanager
from dataclasses import replace
import json

import pytest

from self_improve import apply
from self_improve.commands import command_status, submit_command
from self_improve.config import Config
from self_improve.review import preview_selection
from self_improve.store import Store
from tests.test_apply import insert_proposal, make_diff, init_git_repo, run_git


@pytest.fixture
def env(tmp_path):
    cfg = Config(state_dir=str(tmp_path / 'state'), global_claude_md=str(tmp_path / 'rules.md'), production_repo_path=str(tmp_path / 'repo-prod'))
    target = tmp_path / 'rules.md'
    target.write_text('before\n')
    with closing(Store(cfg.state_path('state.db'))) as store:
        yield cfg, store, target


def approve(store, cfg, *proposals):
    with store.transaction():
        shown = preview_selection(store, cfg, [p['id'] for p in proposals])
    return submit_command(store, cfg, {'action': 'approve', 'request_key': 'request-' + proposals[0]['id'],
        'preview_revision': shown['revision'], 'members': [
            {'proposal_id': m['proposal_id'], 'revision': m['revision']} for m in shown['members']]})


def propose(store, target, **kwargs):
    return insert_proposal(store, target=target, diff=make_diff('before\n', 'before\nafter\n'), status='ungated', **kwargs)


@pytest.mark.parametrize('action,kind', [('add','global_claude_md'), ('convert_to_hook','hook'), ('delete_human_line','project_agents_md')])
def test_explicit_approval_delivers_with_every_automatic_class_off(env, action, kind):
    from self_improve.worker import run_once
    cfg, store, target = env
    p = propose(store, target, action=action, target_kind=kind)
    cmd = approve(store, cfg, p)
    result = run_once(store, cfg)
    assert result['id'] == cmd['id'] and result['state'] == 'completed', result
    assert target.read_text() == 'before\nafter\n'
    assert store.query_one('SELECT status FROM proposals WHERE id=?', (p['id'],))['status'] == 'applied'
    assert run_once(store, cfg) is None
    assert len(store.query("SELECT * FROM proposal_events WHERE event='applied'")) == 1
    assert command_status(store, cmd['id'])['targets'][0]['checkpoint']['delivery']['snapshot_before']


def test_branch_delivery_uses_recorded_branch_and_preserves_checkout_index_and_head(env, tmp_path):
    from self_improve.worker import run_once
    cfg, store, _ = env
    repo = init_git_repo(tmp_path / 'project', 'AGENTS.md', 'before\n')
    target = repo / 'AGENTS.md'
    p = propose(store, target, target_kind='project_agents_md')
    cmd = approve(store, cfg, p)
    target.write_text('uncommitted human work\n')
    run_git(['add', 'AGENTS.md'], repo)
    head, index = run_git(['rev-parse', 'HEAD'], repo), (repo / '.git/index').read_bytes()
    cfg = replace(cfg, project_branch_name='different/branch')
    result = run_once(store, cfg)
    assert result['state'] == 'completed', result
    assert run_git(['show', cmd['targets'][0]['destination']['branch_name'] + ':AGENTS.md'], repo) == 'before\nafter'
    assert target.read_text() == 'uncommitted human work\n'
    assert (repo / '.git/index').read_bytes() == index
    assert run_git(['rev-parse', 'HEAD'], repo) == head


@pytest.mark.parametrize('stage', ['prepared', 'file_replaced', 'before_ack'])
def test_restart_after_file_crash_reconciles_without_duplicate_write(env, monkeypatch, stage):
    from self_improve.worker import run_once
    cfg, store, target = env
    p = propose(store, target)
    cmd = approve(store, cfg, p)
    calls=[]
    real_write=apply._atomic_write
    def write(*args):
        calls.append(args[0]); return real_write(*args)
    monkeypatch.setattr(apply, '_atomic_write', write)
    def crash(point, *_):
        if point == stage:
            raise KeyboardInterrupt('simulated process death')
    monkeypatch.setattr(apply, '_delivery_checkpoint', crash)
    with pytest.raises(KeyboardInterrupt):
        run_once(store, cfg)
    monkeypatch.setattr(apply, '_delivery_checkpoint', lambda *_: None)
    with closing(Store(store.db_path, migrate=False)) as reopened:
        result=run_once(reopened, cfg)
    assert result['state'] == 'completed', result
    assert target.read_text() == 'before\nafter\n'
    assert len(calls) == 1
    assert len(store.query("SELECT * FROM proposal_events WHERE event='applied'")) == 1
    assert command_status(store, cmd['id'])['state'] == 'completed'


@pytest.mark.parametrize('stage', ['commit_prepared', 'ref_updated', 'before_ack'])
def test_restart_after_ref_crash_creates_exactly_one_delivery_commit(env, tmp_path, monkeypatch, stage):
    from self_improve.worker import run_once
    cfg, store, _ = env
    repo = init_git_repo(tmp_path / 'project', 'AGENTS.md', 'before\n')
    p = propose(store, repo / 'AGENTS.md', target_kind='project_agents_md')
    cmd = approve(store, cfg, p)
    def crash(point, *_):
        if point == stage: raise KeyboardInterrupt('simulated process death')
    monkeypatch.setattr(apply, '_delivery_checkpoint', crash)
    with pytest.raises(KeyboardInterrupt): run_once(store, cfg)
    monkeypatch.setattr(apply, '_delivery_checkpoint', lambda *_: None)
    assert run_once(store, cfg)['state'] == 'completed'
    assert run_git(['rev-list', '--count', cfg.project_branch_name], repo) == '2'
    assert run_git(['show', cfg.project_branch_name + ':AGENTS.md'], repo) == 'before\nafter'
    assert command_status(store, cmd['id'])['targets'][0]['checkpoint']['delivery']['branch_commit']


def test_target_changed_after_approval_is_blocked_without_erasing_human_work(env):
    from self_improve.worker import run_once
    cfg, store, target = env
    p = propose(store, target)
    approve(store, cfg, p)
    target.write_text('human edit\n')
    result = run_once(store, cfg)
    assert result['state'] == 'blocked'
    assert result['targets'][0]['error_code'] == 'TargetChanged'
    assert target.read_text() == 'human edit\n'
    assert run_once(store, cfg) is None, 'blocked work must not spin forever'


def test_partial_family_records_the_success_and_the_blocked_target(env, tmp_path):
    from self_improve.worker import run_once
    cfg, store, target = env
    other=tmp_path / 'second.md'; other.write_text('before\n')
    one, two=propose(store,target), propose(store,other)
    cmd=approve(store,cfg,one,two)
    other.write_text('changed\n')
    result=run_once(store,cfg)
    assert result['state'] == 'blocked'
    assert sorted(t['state'] for t in result['targets']) == ['blocked','completed']
    assert target.read_text() == 'before\nafter\n'
    assert other.read_text() == 'changed\n'


def test_a_revoked_member_cancels_unwritten_delivery_and_keeps_its_decision(env):
    from self_improve.worker import run_once
    cfg, store, target = env
    p = propose(store, target)
    approve(store, cfg, p)
    store.update('learnings', 'id', p['learning_id'], {'status':'rejected'})
    store.update('proposals', 'id', p['id'], {'status':'rejected_user'})
    store.commit()
    result=run_once(store,cfg)
    assert result['state']=='cancelled'
    assert result['targets'][0]['error_code']=='LessonRejected'
    assert result['targets'][0]['checkpoint']['cancellation']['no_delivery_observed'] is True
    assert target.read_text()=='before\n'
    assert store.query_one('SELECT status FROM proposals WHERE id=?',(p['id'],))['status']=='rejected_user'
    assert run_once(store,cfg) is None


def test_worker_refuses_a_target_in_a_production_checkout(env, tmp_path):
    from self_improve.worker import run_once
    cfg, store, _=env
    root=init_git_repo(tmp_path/'repo-prod','AGENTS.md','before\n')
    p=propose(store,root/'AGENTS.md')
    approve(store,cfg,p)
    result=run_once(store,cfg)
    assert result['state']=='blocked'
    assert result['targets'][0]['error_code']=='DestinationChanged'
    assert run_git(['branch','--list',cfg.project_branch_name],root)==''


def test_a_real_process_crash_releases_the_lock_and_recovers_the_rename(env):
    import subprocess, sys
    from self_improve.worker import run_once
    cfg, store, target=env
    p=propose(store,target); approve(store,cfg,p)
    script="""
import os, sys
from self_improve.config import Config
from self_improve.store import Store
from self_improve import apply
from self_improve.worker import run_once
cfg=Config(state_dir=sys.argv[1],global_claude_md=sys.argv[2],production_repo_path=sys.argv[1]+'/unused-production')
apply._delivery_checkpoint=lambda stage, target: os._exit(77) if stage=='file_replaced' else None
run_once(Store(cfg.state_path('state.db'),migrate=False),cfg)
"""
    child=subprocess.run([sys.executable,'-c',script,cfg.state_dir,str(target)],capture_output=True,text=True)
    assert child.returncode==77,child.stderr
    assert target.read_text()=='before\nafter\n'
    assert run_once(store,cfg)['state']=='completed'
    assert len(store.query("SELECT * FROM proposal_events WHERE event='applied'"))==1


def test_competing_worker_processes_do_not_duplicate_the_target_write(env):
    import subprocess, sys
    cfg,store,target=env
    p=propose(store,target); approve(store,cfg,p)
    script="""
import json, sys
from contextlib import closing
from self_improve.config import Config
from self_improve.store import Store
from self_improve.worker import run_once
cfg=Config(state_dir=sys.argv[1],global_claude_md=sys.argv[2],production_repo_path=sys.argv[1]+'/unused-production')
with closing(Store(cfg.state_path('state.db'),migrate=False)) as store:
    print(json.dumps(run_once(store,cfg)))
"""
    jobs=[subprocess.Popen([sys.executable,'-c',script,cfg.state_dir,str(target)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for _ in range(2)]
    results=[]
    for job in jobs:
        out,err=job.communicate(timeout=20)
        assert job.returncode==0,err
        results.append(json.loads(out))
    assert sum(bool(r and r.get('state')=='completed') for r in results)==1,results
    assert target.read_text()=='before\nafter\n'
    assert len(store.query("SELECT * FROM proposal_events WHERE event='applied'"))==1


def test_nightly_and_rollback_cannot_enter_a_worker_write_lane(env):
    from self_improve.delivery_lock import instruction_write_lock,WriteBusy
    cfg,store,target=env
    p=propose(store,target)
    with instruction_write_lock(cfg):
        with pytest.raises(WriteBusy): apply.apply_proposal(store,cfg,p)
        with pytest.raises(WriteBusy): apply.rollback(store,cfg,p['id'])
    assert target.read_text()=='before\n'


def test_failed_post_write_checkpoint_blocks_competing_automatic_target(env, monkeypatch):
    from self_improve.worker import run_once
    from self_improve.execution_policy import set_class_policy
    cfg,store,target=env
    p=propose(store,target); approve(store,cfg,p)
    def fail(stage,*_):
        if stage=='file_replaced': raise OSError('simulated storage failure after rename')
    monkeypatch.setattr(apply,'_delivery_checkpoint',fail)
    result=run_once(store,cfg)
    assert result['state']=='failed'
    set_class_policy(store,'global',True,now='2020-01-01T00:00:00Z')
    automatic=insert_proposal(store,target=target,diff=make_diff('before\nafter\n','before\nafter\nautomatic\n'))
    with pytest.raises(apply.ApplyError,match='reconciliation'):
        apply.apply_proposal(store,cfg,automatic)
    assert target.read_text()=='before\nafter\n'


@pytest.mark.parametrize('writer', ['worker','automatic'])
def test_a_checked_out_delivery_branch_is_refused_before_head_changes(env, tmp_path, writer):
    from self_improve.worker import run_once
    from self_improve.execution_policy import set_class_policy
    cfg,store,_=env
    repo=init_git_repo(tmp_path/'project','AGENTS.md','before\n')
    run_git(['checkout','-b',cfg.project_branch_name],repo)
    before=run_git(['rev-parse','HEAD'],repo)
    target=repo/'AGENTS.md'
    if writer=='worker':
        p=propose(store,target,target_kind='project_agents_md');approve(store,cfg,p)
        try:
            result=run_once(store,cfg)
        except apply.ApplyError:
            result=None
        assert run_git(['rev-parse','HEAD'],repo)==before, 'the guard ran only after changing HEAD'
        assert result['targets'][0]['error_code']=='BranchCheckedOut'
    else:
        set_class_policy(store,'project',True,now='2020-01-01T00:00:00Z')
        p=insert_proposal(store,target=target,target_kind='project_agents_md',diff=make_diff('before\n','before\nafter\n'))
        with pytest.raises(apply.ApplyError): apply.apply_proposal(store,cfg,p)
        assert run_git(['rev-parse','HEAD'],repo)==before, 'the guard ran only after changing HEAD'
    assert target.read_text()=='before\n'


def test_branch_delivery_preserves_an_executable_file_mode(env,tmp_path):
    from self_improve.worker import run_once
    cfg,store,_=env
    repo=init_git_repo(tmp_path/'project','hook.sh','before\n')
    run_git(['update-index','--chmod=+x','hook.sh'],repo)
    run_git(['commit','-m','Executable hook'],repo)
    p=propose(store,repo/'hook.sh',target_kind='hook',action='convert_to_hook');approve(store,cfg,p)
    assert run_once(store,cfg)['state']=='completed'
    assert run_git(['ls-tree',cfg.project_branch_name,'hook.sh'],repo).startswith('100755 ')


def test_command_reads_report_a_corrupt_execution_checkpoint(env,monkeypatch):
    from self_improve.worker import run_once
    from self_improve.commands import CommandError
    cfg,store,target=env
    p=propose(store,target);cmd=approve(store,cfg,p)
    def crash(stage,*_):
        if stage=='prepared': raise KeyboardInterrupt('prepared')
    monkeypatch.setattr(apply,'_delivery_checkpoint',crash)
    with pytest.raises(KeyboardInterrupt): run_once(store,cfg)
    row=store.query_one('SELECT * FROM command_targets WHERE command_id=?',(cmd['id'],))
    checkpoint=json.loads(row['checkpoint_json'])
    checkpoint['delivery']['after_content']='unreviewed change\n'
    store.update('command_targets','id',row['id'],{'checkpoint_json':json.dumps(checkpoint)});store.commit()
    with pytest.raises(CommandError,match='checkpoint'):
        command_status(store,cmd['id'])
    assert target.read_text()=='before\n'


@pytest.mark.parametrize('change, state, code', [
    (lambda store, p: store.update('proposals', 'id', p['id'],
        {'diff_unified': make_diff('before\n', 'before\nunreviewed\n')}), 'blocked', 'RevisionChanged'),
    (lambda store, p: store.update('proposals', 'id', p['id'], {'status': 'held'}), 'blocked', 'AuthorizationRevoked'),
    # A rejection cancels the unwritten edit instead (AGENTS.md, rejection scopes).
    (lambda store, p: store.update('learnings', 'id', p['learning_id'], {'status': 'rejected'}), 'cancelled', None),
])
def test_a_change_after_approval_stops_delivery_and_writes_nothing(env, change, state, code):
    """The executor re-checks each approved member before writing: changed
    content, a status that is no longer approved, and a rejected lesson all
    stop the command. None of these refusals had a test."""
    from self_improve.worker import run_once
    cfg, store, target = env
    p = propose(store, target)
    approve(store, cfg, p)
    change(store, p); store.commit()
    result = run_once(store, cfg)
    assert result['state'] == state, result
    if code:
        assert result['targets'][0]['error_code'] == code, result['targets'][0]
    assert target.read_text() == 'before\n'
    assert run_once(store, cfg) is None, 'stopped work must not spin forever'


def test_an_unpublished_branch_delivery_survives_head_moving(env, tmp_path, monkeypatch):
    """BL-14: a crash after commit_prepared, before the delivery branch was
    created, recorded base_ref=HEAD. Once the operator committed, the tip no
    longer matched, the target was classified TargetChanged, and retry, cancel
    and every later write to the repository were refused, although the branch
    is absent and HEAD lacks the prepared edit, so nothing was published."""
    from self_improve.commands import command_status
    from self_improve.worker import run_once
    cfg, store, _ = env
    repo = init_git_repo(tmp_path / 'project', 'AGENTS.md', 'before\n')
    p = propose(store, repo / 'AGENTS.md', target_kind='project_agents_md')
    cmd = approve(store, cfg, p)

    def crash(point, *_):
        if point == 'commit_prepared':
            raise KeyboardInterrupt('process died after durable preparation')
    monkeypatch.setattr(apply, '_delivery_checkpoint', crash)
    with pytest.raises(KeyboardInterrupt):
        run_once(store, cfg)
    monkeypatch.setattr(apply, '_delivery_checkpoint', lambda *_: None)
    (repo / 'other.txt').write_text('x\n')
    run_git(['add', 'other.txt'], repo)
    run_git(['commit', '-q', '-m', 'human work'], repo)
    with closing(Store(cfg.state_path('state.db'), migrate=False)) as fresh:
        result = run_once(fresh, cfg)
        assert result['state'] == 'completed', result
        assert command_status(fresh, cmd['id'])['state'] == 'completed'
    branch = cmd['targets'][0]['destination']['branch_name']
    assert run_git(['show', branch + ':AGENTS.md'], repo) == 'before\nafter'
    # The published commit keeps its recorded base, the HEAD at preparation.
    # That is safe here because AGENTS.md at the new HEAD is still 'before\n'.
    delivery = command_status(store, cmd['id'])['targets'][0]['checkpoint']['delivery']
    assert run_git(['rev-parse', branch + '^'], repo) == delivery['base_ref']


def _crash_before_the_branch_exists(store, cfg, tmp_path, monkeypatch):
    """Approve an AGENTS.md edit in a repository that also holds CLAUDE.md,
    then crash after its branch commit is prepared and before the branch is
    created (BL-14). Returns the repository, proposal and command."""
    from self_improve.worker import run_once
    repo = init_git_repo(tmp_path / 'project', 'AGENTS.md', 'before\n')
    _operator_commits(repo, 'CLAUDE.md', 'before\n')
    p = propose(store, repo / 'AGENTS.md', target_kind='project_agents_md')
    cmd = approve(store, cfg, p)

    def crash(point, *_):
        if point == 'commit_prepared':
            raise KeyboardInterrupt('process died after durable preparation')
    with monkeypatch.context() as patch:
        patch.setattr(apply, '_delivery_checkpoint', crash)
        with pytest.raises(KeyboardInterrupt):
            run_once(store, cfg)
    return repo, p, cmd


def _operator_commits(repo, name, content):
    (repo / name).write_text(content)
    run_git(['add', name], repo)
    run_git(['commit', '-q', '-m', 'human work on ' + name], repo)
    return run_git(['rev-parse', 'HEAD'], repo)


def _second_target_delivers(store, cfg, repo):
    """Another target in the same repository is no longer refused."""
    from self_improve.worker import run_once
    q = propose(store, repo / 'CLAUDE.md', target_kind='project_claude_md')
    approve(store, cfg, q)
    result = run_once(store, cfg)
    assert result['state'] == 'completed', result
    assert run_git(['show', cfg.project_branch_name + ':CLAUDE.md'], repo) == 'before\nafter'


def test_after_an_unpublished_retry_another_target_in_the_repository_delivers(env, tmp_path, monkeypatch):
    """Review of BL-14: before the fix, the stuck target made every later write
    to the repository fail with ReconciliationRequired."""
    from self_improve.worker import run_once
    cfg, store, _ = env
    repo, p, cmd = _crash_before_the_branch_exists(store, cfg, tmp_path, monkeypatch)
    _operator_commits(repo, 'other.txt', 'x\n')
    assert run_once(store, cfg)['state'] == 'completed'
    _second_target_delivers(store, cfg, repo)


@pytest.mark.parametrize('edited', ['other.txt', 'AGENTS.md'])
def test_cancel_after_head_moved_releases_an_unpublished_branch_delivery(env, tmp_path, monkeypatch, edited):
    """Review of BL-14: with the fix reverted, cancel ended 'blocked'. It must
    reach 'cancelled' when the operator's commit leaves AGENTS.md different
    from the prepared after-state: the branch is absent and HEAD lacks the
    edit, so nothing was published. The lane is then free. (When HEAD holds
    the after-state, see test_a_published_branch_merged_without_its_commit_is_acknowledged.)"""
    from self_improve.worker import run_once
    cfg, store, _ = env
    repo, p, cmd = _crash_before_the_branch_exists(store, cfg, tmp_path, monkeypatch)
    head = _operator_commits(repo, edited, 'operator edit\n')
    submit_command(store, cfg, {'action': 'cancel_delivery', 'request_key': 'cancel-moved-head', 'command_id': cmd['id']})
    result = run_once(store, cfg)
    assert result['state'] == 'cancelled', result
    assert [t['state'] for t in result['targets']] == ['cancelled']
    assert run_git(['branch', '--list', cfg.project_branch_name], repo) == ''
    assert run_git(['rev-parse', 'HEAD'], repo) == head
    assert store.query_one('SELECT status FROM proposals WHERE id=?', (p['id'],))['status'] == 'ungated'
    assert store.query("SELECT * FROM proposal_events WHERE event='applied'") == []
    _second_target_delivers(store, cfg, repo)


def test_cancel_after_head_moved_releases_a_target_that_is_not_utf8_text(env, tmp_path, monkeypatch):
    """Review of the second BL-14 fix: observation now reads the target at
    HEAD when the branch is absent. Through read_destination, a non-UTF-8
    AGENTS.md made cancel fail with UnicodeDecodeError; at 812e176 it
    cancelled. The prepared after-state is UTF-8 text, so bytes that do not
    decode cannot be that state, and cancel must still release the target.
    Nothing was published."""
    from self_improve.worker import run_once
    cfg, store, _ = env
    repo, p, cmd = _crash_before_the_branch_exists(store, cfg, tmp_path, monkeypatch)
    (repo / 'AGENTS.md').write_bytes(b'operator \xff edit\n')
    run_git(['add', 'AGENTS.md'], repo)
    run_git(['commit', '-q', '-m', 'non-UTF-8 operator edit'], repo)
    head = run_git(['rev-parse', 'HEAD'], repo)
    submit_command(store, cfg, {'action': 'cancel_delivery', 'request_key': 'cancel-odd-target', 'command_id': cmd['id']})
    result = run_once(store, cfg)
    assert result['state'] == 'cancelled', result
    assert [t['state'] for t in result['targets']] == ['cancelled'], result['targets']
    assert run_git(['branch', '--list', cfg.project_branch_name], repo) == ''
    assert run_git(['rev-parse', 'HEAD'], repo) == head
    assert store.query_one('SELECT status FROM proposals WHERE id=?', (p['id'],))['status'] == 'ungated'


def test_a_retry_after_head_moved_blocks_when_head_changed_the_target(env, tmp_path, monkeypatch):
    """Review of BL-14: after the crash, the operator committed an edit to
    AGENTS.md itself. The retry published the prepared commit on the old HEAD,
    and the branch lacked the operator's edit ('BRANCH CONTAINS HEAD: False').
    Without a crash the same change is blocked as TargetChanged. The approval
    reviewed bytes that HEAD no longer has, so the retry must block too."""
    from self_improve.worker import run_once
    cfg, store, _ = env
    repo, p, cmd = _crash_before_the_branch_exists(store, cfg, tmp_path, monkeypatch)
    head = _operator_commits(repo, 'AGENTS.md', 'operator edit\n')
    result = run_once(store, cfg)
    assert result['state'] == 'blocked', result
    assert result['targets'][0]['error_code'] == 'TargetChanged', result['targets'][0]
    assert run_git(['branch', '--list', cfg.project_branch_name], repo) == ''
    assert run_git(['rev-parse', 'HEAD'], repo) == head
    assert (repo / 'AGENTS.md').read_text() == 'operator edit\n'
    assert store.query("SELECT * FROM proposal_events WHERE event='applied'") == []
    assert run_once(store, cfg) is None, 'blocked work must not spin forever'
    # A fresh review of the new content delivers it on the new HEAD.
    submit_command(store, cfg, {'action': 'cancel_delivery', 'request_key': 'cancel-after-block', 'command_id': cmd['id']})
    assert run_once(store, cfg)['state'] == 'cancelled'
    fresh = insert_proposal(store, target=repo / 'AGENTS.md', target_kind='project_agents_md', status='ungated',
                            diff=make_diff('operator edit\n', 'operator edit\nafter\n'))
    approve(store, cfg, fresh)
    assert run_once(store, cfg)['state'] == 'completed'
    assert run_git(['show', cfg.project_branch_name + ':AGENTS.md'], repo) == 'operator edit\nafter'
    assert run_git(['rev-parse', cfg.project_branch_name + '^'], repo) == head


def test_an_automatic_retry_after_head_moved_blocks_when_head_changed_the_target(env, tmp_path, monkeypatch):
    """Review of BL-14: the automatic writer shares the publication step, so
    the same moved-HEAD check applies to a prepared automatic operation."""
    from self_improve.execution_policy import set_class_policy
    cfg, store, _ = env
    set_class_policy(store, 'project', True, now='2020-01-01T00:00:00Z')
    repo = init_git_repo(tmp_path / 'project', 'AGENTS.md', 'before\n')
    p = insert_proposal(store, target=repo / 'AGENTS.md', target_kind='project_agents_md',
                        diff=make_diff('before\n', 'before\nafter\n'))

    def crash(point, *_):
        if point == 'prepared':
            raise KeyboardInterrupt('process died after durable preparation')
    with monkeypatch.context() as patch:
        patch.setattr(apply, '_operation_checkpoint', crash)
        with pytest.raises(KeyboardInterrupt):
            apply.apply_proposal(store, cfg, p)
    _operator_commits(repo, 'AGENTS.md', 'operator edit\n')
    operation = store.query_one('SELECT id FROM instruction_operations')
    result = apply.resume_write_operation(store, cfg, operation['id'])
    assert result['state'] == 'blocked', result
    assert result['error_code'] == 'TargetChanged', result
    assert run_git(['branch', '--list', cfg.project_branch_name], repo) == ''
    assert store.query("SELECT * FROM proposal_events WHERE event='applied'") == []


def test_cancelling_an_automatic_operation_after_head_moved_releases_its_unpublished_branch(env, tmp_path, monkeypatch):
    """Review of BL-14: the cancellation path of an automatic operation had no
    biting test. Its branch was absent at preparation, the process died after
    the branch commit was prepared, and the operator then committed on HEAD.
    With the fix reverted, cancel ended 'blocked' with TargetChanged. The
    operator commit touches another file, so the branch is still absent and
    AGENTS.md at the new HEAD still equals the prepared before-state, not the
    after-state. Nothing was published, so cancel must reach 'cancelled'. (A
    HEAD that holds the after-state is acknowledged instead; see
    test_a_published_branch_merged_without_its_commit_is_acknowledged.)"""
    from self_improve.commands import submit_command
    from self_improve.execution_policy import set_class_policy
    from self_improve.worker import run_once
    cfg, store, _ = env
    set_class_policy(store, 'project', True, now='2020-01-01T00:00:00Z')
    repo = init_git_repo(tmp_path / 'project', 'AGENTS.md', 'before\n')
    _operator_commits(repo, 'CLAUDE.md', 'before\n')  # the second target below
    p = insert_proposal(store, target=repo / 'AGENTS.md', target_kind='project_agents_md',
                        diff=make_diff('before\n', 'before\nafter\n'))

    # The automatic writer has no 'commit_prepared' point. 'prepared' is the
    # same moment: the operation is durable and its branch commit exists.
    def crash(point, *_):
        if point == 'prepared':
            raise KeyboardInterrupt('process died after durable preparation')
    with monkeypatch.context() as patch:
        patch.setattr(apply, '_operation_checkpoint', crash)
        with pytest.raises(KeyboardInterrupt):
            apply.apply_proposal(store, cfg, p)
    operation = store.query_one('SELECT id, checkpoint_json FROM instruction_operations')
    delivery = json.loads(operation['checkpoint_json'])['delivery']
    assert delivery['branch_exists'] is False and delivery['branch_commit'], delivery
    assert run_git(['branch', '--list', cfg.project_branch_name], repo) == ''
    head = _operator_commits(repo, 'other.txt', 'operator edit\n')
    assert head != delivery['base_ref']
    submit_command(store, cfg, {'action': 'cancel_operation', 'operation_id': operation['id'],
                                'request_key': 'cancel-automatic-moved-head'})
    result = run_once(store, cfg)
    assert result['id'] == operation['id'] and result['state'] == 'cancelled', result
    assert result['checkpoint']['cancellation']['no_delivery_observed'] is True
    assert run_git(['branch', '--list', cfg.project_branch_name], repo) == ''
    assert run_git(['rev-parse', 'HEAD'], repo) == head
    assert (repo / 'AGENTS.md').read_text() == 'before\n'
    assert store.query_one('SELECT status FROM proposals WHERE id=?', (p['id'],))['status'] == 'held'
    assert store.query("SELECT * FROM proposal_events WHERE event='applied'") == []
    assert run_once(store, cfg) is None, 'cancelled work must not spin forever'
    # The lane is free: an approved edit to another file in the repository delivers.
    _second_target_delivers(store, cfg, repo)


MERGE_STYLES = ('squash', 'cherry_pick', 'rebase')


def _merge_without_its_commit_and_delete(repo, branch, style):
    """The operator merges the delivery branch so that HEAD holds its content
    but not its commit, then deletes the branch. A fast-forward merge keeps
    the commit in HEAD's history; these three do not."""
    if style == 'squash':
        run_git(['merge', '-q', '--squash', branch], repo)
        run_git(['commit', '-q', '-m', 'squash the delivery'], repo)
    elif style == 'cherry_pick':
        _operator_commits(repo, 'other.txt', 'x\n')
        # Every delivery commit on the branch, oldest first: one, or two when
        # an earlier delivery created the branch.
        run_git(['cherry-pick', 'HEAD..' + branch], repo)
    else:
        main = run_git(['symbolic-ref', '--short', 'HEAD'], repo)
        _operator_commits(repo, 'other.txt', 'x\n')
        run_git(['rebase', '-q', main, branch], repo)
        run_git(['switch', '-q', main], repo)
        run_git(['merge', '-q', '--ff-only', branch], repo)
    run_git(['branch', '-D', branch], repo)
    return run_git(['rev-parse', 'HEAD'], repo)


@pytest.mark.parametrize('control', ['cancel', 'retry'])
@pytest.mark.parametrize('style', MERGE_STYLES)
def test_a_published_branch_merged_without_its_commit_is_acknowledged(env, tmp_path, monkeypatch, style, control):
    """Second review of BL-14: the first delivery to a repository published
    the branch, and the worker died before the completion commit. The operator
    squash-merged the branch and deleted it. The retry blocked as
    TargetChanged, and the cancel recorded no_delivery_observed and returned
    the proposal to Review while its edit was live at HEAD. A second approval
    could then add the same rule twice. HEAD holding the prepared after-state
    is an observed write, so both controls must acknowledge it."""
    from self_improve.worker import run_once
    cfg, store, _ = env
    repo = init_git_repo(tmp_path / 'project', 'AGENTS.md', 'before\n')
    _operator_commits(repo, 'CLAUDE.md', 'before\n')
    p = propose(store, repo / 'AGENTS.md', target_kind='project_agents_md')
    cmd = approve(store, cfg, p)

    def crash(point, *_):
        if point == 'ref_updated':
            raise KeyboardInterrupt('process died after update-ref, before the completion commit')
    with monkeypatch.context() as patch:
        patch.setattr(apply, '_delivery_checkpoint', crash)
        with pytest.raises(KeyboardInterrupt):
            run_once(store, cfg)
    assert run_git(['show', cfg.project_branch_name + ':AGENTS.md'], repo) == 'before\nafter'
    head = _merge_without_its_commit_and_delete(repo, cfg.project_branch_name, style)
    assert run_git(['show', 'HEAD:AGENTS.md'], repo) == 'before\nafter'
    if control == 'cancel':
        submit_command(store, cfg, {'action': 'cancel_delivery', 'request_key': 'cancel-after-merge', 'command_id': cmd['id']})
    result = run_once(store, cfg)
    assert result['state'] == 'completed', result
    target = result['targets'][0]
    assert target['state'] == 'completed', target
    assert 'cancellation' not in target['checkpoint'], target['checkpoint']
    assert store.query_one('SELECT status FROM proposals WHERE id=?', (p['id'],))['status'] == 'applied'
    assert len(store.query("SELECT * FROM proposal_events WHERE event='applied'")) == 1
    assert store.query("SELECT * FROM proposal_events WHERE event='approval_cancelled'") == []
    # Acknowledgement writes no ref and leaves the operator's merge alone.
    assert run_git(['branch', '--list', cfg.project_branch_name], repo) == ''
    assert run_git(['rev-parse', 'HEAD'], repo) == head
    _second_target_delivers(store, cfg, repo)


def _second_delivery_published_on_an_existing_branch(store, cfg, tmp_path, monkeypatch):
    """A first delivery creates the branch and completes. A second delivery
    to the same file is prepared on that branch, publishes, and the worker
    dies before its completion commit. Returns the repository, both
    proposals and the second command."""
    from self_improve.worker import run_once
    repo = init_git_repo(tmp_path / 'project', 'AGENTS.md', 'before\n')
    _operator_commits(repo, 'CLAUDE.md', 'before\n')  # for _second_target_delivers
    first = propose(store, repo / 'AGENTS.md', target_kind='project_agents_md')
    approve(store, cfg, first)
    assert run_once(store, cfg)['state'] == 'completed'
    second = insert_proposal(store, target=repo / 'AGENTS.md', target_kind='project_agents_md', status='ungated',
                             diff=make_diff('before\nafter\n', 'before\nafter\nsecond\n'))
    cmd = approve(store, cfg, second)

    def crash(point, *_):
        if point == 'ref_updated':
            raise KeyboardInterrupt('process died after update-ref, before the completion commit')
    with monkeypatch.context() as patch:
        patch.setattr(apply, '_delivery_checkpoint', crash)
        with pytest.raises(KeyboardInterrupt):
            run_once(store, cfg)
    delivery = command_status(store, cmd['id'])['targets'][0]['checkpoint']['delivery']
    assert delivery['branch_exists'] is True, delivery
    assert run_git(['show', cfg.project_branch_name + ':AGENTS.md'], repo) == 'before\nafter\nsecond'
    return repo, first, second, cmd


@pytest.mark.parametrize('control', ['cancel', 'retry'])
@pytest.mark.parametrize('style', MERGE_STYLES)
def test_a_second_delivery_on_an_existing_branch_merged_without_its_commit_is_acknowledged(env, tmp_path, monkeypatch, style, control):
    """Third review of BL-14: observation checked HEAD for the prepared
    after-state only when the branch was absent at preparation. Here the
    branch existed, because a first delivery created it. The second delivery
    published, the worker died before its completion commit, and the operator
    merged the branch without its commits and deleted it. Retry and cancel
    then blocked as TargetChanged, and every later delivery to the repository
    was refused with ReconciliationRequired, although the edit is live at
    HEAD. An absent branch now and a HEAD that holds the after-state is an
    observed write, whatever the branch was at preparation."""
    from self_improve.worker import run_once
    cfg, store, _ = env
    repo, first, second, cmd = _second_delivery_published_on_an_existing_branch(store, cfg, tmp_path, monkeypatch)
    head = _merge_without_its_commit_and_delete(repo, cfg.project_branch_name, style)
    assert run_git(['show', 'HEAD:AGENTS.md'], repo) == 'before\nafter\nsecond'
    if control == 'cancel':
        submit_command(store, cfg, {'action': 'cancel_delivery', 'request_key': 'cancel-second-merged', 'command_id': cmd['id']})
    result = run_once(store, cfg)
    assert result['id'] == cmd['id'] and result['state'] == 'completed', result
    target = result['targets'][0]
    assert target['state'] == 'completed', target
    assert 'cancellation' not in target['checkpoint'], target['checkpoint']
    for p in (first, second):
        assert store.query_one('SELECT status FROM proposals WHERE id=?', (p['id'],))['status'] == 'applied'
    assert len(store.query("SELECT * FROM proposal_events WHERE event='applied'")) == 2
    assert store.query("SELECT * FROM proposal_events WHERE event='approval_cancelled'") == []
    # Acknowledgement writes no ref and leaves the operator's merge alone.
    assert run_git(['branch', '--list', cfg.project_branch_name], repo) == ''
    assert run_git(['rev-parse', 'HEAD'], repo) == head
    _second_target_delivers(store, cfg, repo)


def test_a_second_delivery_whose_branch_is_gone_without_its_edit_at_head_stays_blocked(env, tmp_path, monkeypatch):
    """Third review of BL-14: the branch existed at preparation, so an absent
    branch now and a HEAD without the exact after-state do not show that
    nothing was published. Here the edit did publish: the operator squashed
    it into HEAD, deleted the branch and edited the file again. Cancel must
    not record 'no delivery observed' and return a live edit to Review. The
    target stays blocked as TargetChanged and nothing is written."""
    from self_improve.worker import run_once
    cfg, store, _ = env
    repo, first, second, cmd = _second_delivery_published_on_an_existing_branch(store, cfg, tmp_path, monkeypatch)
    _merge_without_its_commit_and_delete(repo, cfg.project_branch_name, 'squash')
    head = _operator_commits(repo, 'AGENTS.md', 'before\nafter\nsecond\noperator edit\n')
    submit_command(store, cfg, {'action': 'cancel_delivery', 'request_key': 'cancel-second-edited', 'command_id': cmd['id']})
    result = run_once(store, cfg)
    assert result['state'] == 'blocked', result
    target = result['targets'][0]
    assert (target['state'], target['error_code']) == ('blocked', 'TargetChanged'), target
    assert 'cancellation' not in target['checkpoint'], target['checkpoint']
    assert store.query_one('SELECT status FROM proposals WHERE id=?', (second['id'],))['status'] == 'approved_user'
    assert store.query("SELECT * FROM proposal_events WHERE event='approval_cancelled'") == []
    assert run_git(['branch', '--list', cfg.project_branch_name], repo) == ''
    assert run_git(['rev-parse', 'HEAD'], repo) == head
    assert (repo / 'AGENTS.md').read_text() == 'before\nafter\nsecond\noperator edit\n'


def test_a_squash_merged_automatic_delivery_is_acknowledged_on_cancel(env, tmp_path, monkeypatch):
    """Second review of BL-14: the automatic writer shares the observation
    step, so a squash-merged automatic write is acknowledged, not cancelled."""
    from self_improve.execution_policy import set_class_policy
    from self_improve.worker import run_once
    cfg, store, _ = env
    set_class_policy(store, 'project', True, now='2020-01-01T00:00:00Z')
    repo = init_git_repo(tmp_path / 'project', 'AGENTS.md', 'before\n')
    p = insert_proposal(store, target=repo / 'AGENTS.md', target_kind='project_agents_md',
                        diff=make_diff('before\n', 'before\nafter\n'))

    def crash(point, *_):
        if point == 'ref_updated':
            raise KeyboardInterrupt('process died after update-ref, before the completion commit')
    with monkeypatch.context() as patch:
        patch.setattr(apply, '_operation_checkpoint', crash)
        with pytest.raises(KeyboardInterrupt):
            apply.apply_proposal(store, cfg, p)
    _merge_without_its_commit_and_delete(repo, cfg.project_branch_name, 'squash')
    oid = store.query_one('SELECT id FROM instruction_operations')['id']
    submit_command(store, cfg, {'action': 'cancel_operation', 'operation_id': oid, 'request_key': 'cancel-auto-squash'})
    result = run_once(store, cfg)
    assert result['state'] == 'completed', result
    assert 'cancellation' not in result['checkpoint'], result['checkpoint']
    assert result['result']['outcome'] == 'applied', result['result']
    assert store.query_one('SELECT status FROM proposals WHERE id=?', (p['id'],))['status'] == 'applied'
    assert len(store.query("SELECT * FROM proposal_events WHERE event='applied'")) == 1


def test_a_squash_merged_rollback_is_acknowledged_on_cancel(env, tmp_path, monkeypatch):
    """Second review of BL-14: rollback shares the observation step too. A
    squash-merged inverse is live at HEAD, so cancel must record the rollback,
    not leave the application looking applied."""
    from self_improve.worker import run_once
    cfg, store, _ = env
    repo = init_git_repo(tmp_path / 'project', 'AGENTS.md', 'before\n')
    p = propose(store, repo / 'AGENTS.md', target_kind='project_agents_md')
    approve(store, cfg, p)
    assert run_once(store, cfg)['state'] == 'completed'
    branch = cfg.project_branch_name
    run_git(['merge', '-q', '--ff-only', branch], repo)
    run_git(['branch', '-d', branch], repo)

    def crash(point, *_):
        if point == 'ref_updated':
            raise KeyboardInterrupt('process died after update-ref, before the completion commit')
    with monkeypatch.context() as patch:
        patch.setattr(apply, '_operation_checkpoint', crash)
        with pytest.raises(KeyboardInterrupt):
            apply.rollback(store, cfg, p['id'])
    assert run_git(['show', branch + ':AGENTS.md'], repo) == 'before'
    _merge_without_its_commit_and_delete(repo, branch, 'squash')
    oid = store.query_one("SELECT id FROM instruction_operations WHERE kind='rollback'")['id']
    submit_command(store, cfg, {'action': 'cancel_operation', 'operation_id': oid, 'request_key': 'cancel-rollback-squash'})
    result = run_once(store, cfg)
    assert result['state'] == 'completed', result
    assert 'cancellation' not in result['checkpoint'], result['checkpoint']
    assert result['result']['outcome'] == 'rolled_back', result['result']
    assert store.query_one('SELECT status FROM proposals WHERE id=?', (p['id'],))['status'] == 'rolled_back'
    assert len(store.query("SELECT * FROM proposal_events WHERE event='rolled_back'")) == 1


# Targets whose line boundaries the '\n'-joined applier would rewrite (BL-15).
# The last one lacks a final newline beyond the reach of the edit's hunk.
REWRITTEN_TARGETS = {'crlf': b'before\r\n', 'form_feed': b'before\x0c\n',
                     'no_final_newline': b'before\none\ntwo\nthree\nfour'}


def _propose_against(store, target, raw, **kwargs):
    """A proposal built the way the pipeline builds one: from splitlines().
    It inserts one line after the first; returns it and the LF-joined result."""
    lines = raw.decode('utf-8').splitlines()
    lf = '\n'.join(lines) + '\n'
    edited = '\n'.join([lines[0], 'after', *lines[1:]]) + '\n'
    fields = {'status': 'ungated', **kwargs}
    return insert_proposal(store, target=target, diff=make_diff(lf, edited), **fields), edited


@contextmanager
def _without_line_ending_refusal(monkeypatch):
    """Run the enclosed steps as the code before BL-15 did: no refusal anywhere."""
    from self_improve import propose
    with monkeypatch.context() as patch:
        patch.setattr(propose, '_refuse_rewritten_boundaries', lambda content: None)
        yield


@pytest.mark.parametrize('name', sorted(REWRITTEN_TARGETS))
def test_an_approval_recorded_before_the_refusal_still_cannot_rewrite_line_endings(env, monkeypatch, name):
    """Review of BL-15: a command approved before the refusal existed holds a
    reviewed base whose hash still matches the target. Preparation is then the
    last refusal before the worker writes the LF-rewritten bytes."""
    from self_improve.worker import run_once
    cfg, store, target = env
    raw = REWRITTEN_TARGETS[name]
    target.write_bytes(raw)
    p, _ = _propose_against(store, target, raw)
    with _without_line_ending_refusal(monkeypatch):
        cmd = approve(store, cfg, p)
    result = run_once(store, cfg)
    assert result['state'] == 'blocked', result
    assert result['targets'][0]['error_code'] == 'UnsupportedLineEndings', result['targets'][0]
    assert target.read_bytes() == raw
    assert store.query("SELECT * FROM proposal_events WHERE event='applied'") == []
    assert command_status(store, cmd['id'])['targets'][0]['checkpoint'].get('delivery') is None


@pytest.mark.parametrize('name', sorted(REWRITTEN_TARGETS))
def test_a_delivery_recorded_before_the_refusal_stays_readable_and_reversible(env, monkeypatch, name):
    """Review of BL-15: the record validator and the rollback source recompute
    a retained delivery from its retained before_content. They opt out of the
    refusal, so a delivery made before it existed still reads and rolls back
    to its exact original bytes."""
    from self_improve.worker import run_once
    cfg, store, target = env
    raw = REWRITTEN_TARGETS[name]
    target.write_bytes(raw)
    p, written = _propose_against(store, target, raw)
    with _without_line_ending_refusal(monkeypatch):
        cmd = approve(store, cfg, p)
        assert run_once(store, cfg)['state'] == 'completed'
    assert target.read_bytes() == written.encode('utf-8'), 'the earlier applier rewrote the boundaries'
    assert command_status(store, cmd['id'])['state'] == 'completed'
    result = apply.rollback(store, cfg, p['id'])
    assert result['outcome'] == 'rolled_back', result
    assert target.read_bytes() == raw


def _target_in(mode, tmp_path, file_target, raw):
    """A target holding exactly these bytes: the env file, or AGENTS.md
    committed in a new repository. Returns (target, target_kind, repo)."""
    if mode == 'file':
        file_target.write_bytes(raw)
        return file_target, 'global_claude_md', None
    repo = init_git_repo(tmp_path / 'project', 'README.md', 'x\n')
    run_git(['config', 'core.autocrlf', 'false'], repo)
    (repo / 'AGENTS.md').write_bytes(raw)
    run_git(['add', 'AGENTS.md'], repo)
    run_git(['commit', '-q', '-m', 'operator file'], repo)
    assert _blob(repo, 'HEAD', 'AGENTS.md') == raw
    return repo / 'AGENTS.md', 'project_agents_md', repo


def _blob(repo, rev, name):
    import subprocess
    return subprocess.run(['git', 'cat-file', 'blob', f'{rev}:{name}'], cwd=repo,
                          capture_output=True, check=True).stdout


def _delivered_bytes(cfg, target, repo):
    """What a reader of the destination sees: the file, or the branch blob."""
    if repo is None:
        return target.read_bytes()
    if run_git(['branch', '--list', cfg.project_branch_name], repo) == '':
        return None
    return _blob(repo, cfg.project_branch_name, 'AGENTS.md')


def _assert_unwritten(cfg, target, repo, raw):
    if repo is None:
        assert target.read_bytes() == raw
    else:
        assert _delivered_bytes(cfg, target, repo) is None, 'no delivery branch may be published'
        assert _blob(repo, 'HEAD', 'AGENTS.md') == raw
        assert target.read_bytes() == raw


def _crash_at(stage):
    def crash(point, *_):
        if point == stage:
            raise KeyboardInterrupt('process died under the code before BL-15')
    return crash


@pytest.mark.parametrize('mode', ['file', 'git_branch'])
@pytest.mark.parametrize('name', sorted(REWRITTEN_TARGETS))
def test_a_delivery_prepared_before_the_refusal_is_refused_at_publication(env, tmp_path, monkeypatch, name, mode):
    """BL-60: the code before BL-15 prepared this delivery and died before
    publishing it. Validation of the retained record opts out of the refusal,
    so the upgraded worker published the LF-rewritten bytes. Reviewer probe:
    'POST-UPGRADE PUBLISH: completed BYTES: b'before\\nkeep\\nafter\\n'' for a
    CRLF target and for one without a final newline. Publication must refuse
    it by name, write nothing, and leave cancel and a fresh review open."""
    from self_improve.worker import run_once
    cfg, store, file_target = env
    raw = REWRITTEN_TARGETS[name]
    target, kind, repo = _target_in(mode, tmp_path, file_target, raw)
    p, _ = _propose_against(store, target, raw, target_kind=kind)
    with _without_line_ending_refusal(monkeypatch):
        cmd = approve(store, cfg, p)
        with monkeypatch.context() as patch:
            patch.setattr(apply, '_delivery_checkpoint', _crash_at('prepared'))
            with pytest.raises(KeyboardInterrupt):
                run_once(store, cfg)
    assert command_status(store, cmd['id'])['targets'][0]['checkpoint']['delivery']['before_content'] == raw.decode('utf-8')
    result = run_once(store, cfg)
    assert result['state'] == 'blocked', result
    refused = result['targets'][0]
    assert refused['error_code'] == 'UnsupportedLineEndings', refused
    assert 'prepared before' in refused['error_detail'], refused['error_detail']
    _assert_unwritten(cfg, target, repo, raw)
    assert store.query("SELECT * FROM proposal_events WHERE event='applied'") == []
    assert run_once(store, cfg) is None, 'blocked work must not spin forever'
    submit_command(store, cfg, {'action': 'cancel_delivery', 'request_key': 'cancel-old-record', 'command_id': cmd['id']})
    assert run_once(store, cfg)['state'] == 'cancelled'
    _assert_unwritten(cfg, target, repo, raw)
    assert store.query_one('SELECT status FROM proposals WHERE id=?', (p['id'],))['status'] == 'ungated'


def _refused_at_publication(store, cfg, tmp_path, monkeypatch, file_target, name, mode):
    """The code before BL-15 prepares this delivery and dies before it
    publishes. The upgraded worker then refuses it at publication.
    Returns the target, its repository, the proposal, the command and the
    refused target."""
    from self_improve.worker import run_once
    raw = REWRITTEN_TARGETS[name]
    target, kind, repo = _target_in(mode, tmp_path, file_target, raw)
    p, _ = _propose_against(store, target, raw, target_kind=kind)
    with _without_line_ending_refusal(monkeypatch):
        cmd = approve(store, cfg, p)
        with monkeypatch.context() as patch:
            patch.setattr(apply, '_delivery_checkpoint', _crash_at('prepared'))
            with pytest.raises(KeyboardInterrupt):
                run_once(store, cfg)
    refused = run_once(store, cfg)['targets'][0]
    assert refused['error_code'] == 'UnsupportedLineEndings', refused
    return target, repo, p, cmd, refused


def _fix_the_file(target, repo, name):
    """What the operator does to fix the file: convert its boundaries to
    '\\n' and end it with one, which is the LF content the proposal used."""
    fixed = '\n'.join(REWRITTEN_TARGETS[name].decode('utf-8').splitlines()) + '\n'
    if repo is None:
        target.write_text(fixed)
    else:
        _operator_commits(repo, 'AGENTS.md', fixed)


@pytest.mark.parametrize('mode', ['file', 'git_branch'])
@pytest.mark.parametrize('name', sorted(REWRITTEN_TARGETS))
def test_a_delivery_refused_at_publication_says_to_cancel_before_fixing_the_file(env, tmp_path, monkeypatch, name, mode):
    """Review of BL-60: the refusal detail said "convert the file to LF line
    endings first" and then "Cancel it, fix the file". In file mode, fixing
    the file first makes the cancel block as TargetChanged, because the file
    then matches neither recorded state. The detail must say: cancel first,
    then fix the file, then review a new preview. That order must work."""
    from self_improve.worker import run_once
    cfg, store, file_target = env
    target, repo, p, cmd, refused = _refused_at_publication(store, cfg, tmp_path, monkeypatch, file_target, name, mode)
    detail = refused['error_detail']
    steps = ['Cancel this delivery first.', 'Then fix the file:', 'Then review a new preview.']
    assert all(step in detail for step in steps), detail
    assert [detail.index(step) for step in steps] == sorted(detail.index(step) for step in steps), detail
    before_cancel = detail[:detail.index(steps[0])]
    assert ' first' not in before_cancel and 'fix' not in before_cancel, detail
    # Cancel first.
    submit_command(store, cfg, {'action': 'cancel_delivery', 'request_key': 'cancel-before-fix', 'command_id': cmd['id']})
    assert run_once(store, cfg)['state'] == 'cancelled'
    # Then fix the file.
    _fix_the_file(target, repo, name)
    # Then review a new preview; it delivers.
    with store.transaction():
        shown = preview_selection(store, cfg, [p['id']])
    assert [t['state'] for t in shown['targets']] == ['ready'], shown['targets']
    submit_command(store, cfg, {'action': 'approve', 'request_key': 'approve-after-fix', 'preview_revision': shown['revision'],
                                'members': [{'proposal_id': m['proposal_id'], 'revision': m['revision']} for m in shown['members']]})
    assert run_once(store, cfg)['state'] == 'completed'
    lines = REWRITTEN_TARGETS[name].decode('utf-8').splitlines()
    edited = '\n'.join([lines[0], 'after', *lines[1:]]) + '\n'
    assert _delivered_bytes(cfg, target, repo) == edited.encode('utf-8')
    assert store.query_one('SELECT status FROM proposals WHERE id=?', (p['id'],))['status'] == 'applied'


@pytest.mark.parametrize('name', sorted(REWRITTEN_TARGETS))
def test_a_file_fixed_before_the_cancel_blocks_that_cancel(env, tmp_path, monkeypatch, name):
    """Review of BL-60: the reason the refusal detail says to cancel first.
    In file mode, a file fixed before the cancel matches neither the
    recorded before-state nor the after-state, so the cancel blocks as
    TargetChanged and leaves the file alone."""
    from self_improve.worker import run_once
    cfg, store, file_target = env
    target, repo, p, cmd, _ = _refused_at_publication(store, cfg, tmp_path, monkeypatch, file_target, name, 'file')
    _fix_the_file(target, repo, name)
    fixed = target.read_bytes()
    submit_command(store, cfg, {'action': 'cancel_delivery', 'request_key': 'cancel-after-fix', 'command_id': cmd['id']})
    result = run_once(store, cfg)
    assert result['state'] == 'blocked', result
    assert result['targets'][0]['error_code'] == 'TargetChanged', result['targets'][0]
    assert target.read_bytes() == fixed


@pytest.mark.parametrize('mode', ['file', 'git_branch'])
@pytest.mark.parametrize('name', sorted(REWRITTEN_TARGETS))
def test_an_automatic_write_prepared_before_the_refusal_is_refused_at_publication(env, tmp_path, monkeypatch, name, mode):
    """BL-60: the automatic writer publishes a prepared record through the
    same step, so it refuses the same retained record by name."""
    from self_improve.execution_policy import set_class_policy
    from self_improve.worker import run_once
    cfg, store, file_target = env
    set_class_policy(store, 'global' if mode == 'file' else 'project', True, now='2020-01-01T00:00:00Z')
    raw = REWRITTEN_TARGETS[name]
    target, kind, repo = _target_in(mode, tmp_path, file_target, raw)
    p, _ = _propose_against(store, target, raw, target_kind=kind, status='gated_pass')
    with _without_line_ending_refusal(monkeypatch):
        with monkeypatch.context() as patch:
            patch.setattr(apply, '_operation_checkpoint', _crash_at('prepared'))
            with pytest.raises(KeyboardInterrupt):
                apply.apply_proposal(store, cfg, p)
    oid = store.query_one('SELECT id FROM instruction_operations')['id']
    result = apply.resume_write_operation(store, cfg, oid)
    assert result['state'] == 'blocked', result
    assert result['error_code'] == 'UnsupportedLineEndings', result
    assert 'prepared before' in result['error_detail'], result['error_detail']
    _assert_unwritten(cfg, target, repo, raw)
    assert store.query("SELECT * FROM proposal_events WHERE event='applied'") == []
    submit_command(store, cfg, {'action': 'cancel_operation', 'operation_id': oid, 'request_key': 'cancel-old-operation'})
    assert run_once(store, cfg)['state'] == 'cancelled'
    _assert_unwritten(cfg, target, repo, raw)
    assert store.query_one('SELECT status FROM proposals WHERE id=?', (p['id'],))['status'] == 'held'


@pytest.mark.parametrize('mode', ['file', 'git_branch'])
@pytest.mark.parametrize('name', sorted(REWRITTEN_TARGETS))
def test_a_delivery_published_before_the_refusal_is_acknowledged_and_reversible(env, tmp_path, monkeypatch, name, mode):
    """BL-60: the refusal applies only to a write that has not happened. The
    code before BL-15 published this delivery and died before its completion
    commit. The upgraded worker must acknowledge the observed write, and
    rollback must restore the exact original bytes."""
    from self_improve.worker import run_once
    cfg, store, file_target = env
    raw = REWRITTEN_TARGETS[name]
    target, kind, repo = _target_in(mode, tmp_path, file_target, raw)
    p, written = _propose_against(store, target, raw, target_kind=kind)
    with _without_line_ending_refusal(monkeypatch):
        cmd = approve(store, cfg, p)
        with monkeypatch.context() as patch:
            patch.setattr(apply, '_delivery_checkpoint', _crash_at('file_replaced' if repo is None else 'ref_updated'))
            with pytest.raises(KeyboardInterrupt):
                run_once(store, cfg)
    assert _delivered_bytes(cfg, target, repo) == written.encode('utf-8'), 'the earlier applier rewrote the boundaries'
    result = run_once(store, cfg)
    assert result['state'] == 'completed', result
    assert command_status(store, cmd['id'])['state'] == 'completed'
    assert len(store.query("SELECT * FROM proposal_events WHERE event='applied'")) == 1
    rolled = apply.rollback(store, cfg, p['id'])
    assert rolled['outcome'] == 'rolled_back', rolled
    assert _delivered_bytes(cfg, target, repo) == raw
