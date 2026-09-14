"""Real scratch file/ref writes driven by frozen dashboard commands."""
from contextlib import closing
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
