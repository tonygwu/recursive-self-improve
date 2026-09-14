"""The automatic write lane must survive a process dying between disk and DB."""
from contextlib import closing
from dataclasses import replace
import json

import pytest

from self_improve import apply
from self_improve.execution_policy import set_class_policy
from self_improve.store import Store
from self_improve.worker import run_once
from tests.test_apply import cfg, store, insert_proposal, make_diff, init_git_repo, run_git
from tests.test_delivery_worker import approve


def proposal(store,target,before='before\n',after='before\nafter\n'):
    return insert_proposal(store,target=target,diff=make_diff(before,after))


@pytest.mark.parametrize('stage', ['prepared','file_replaced','before_ack'])
def test_automatic_write_recovers_from_a_durable_checkpoint(cfg,store,tmp_path,monkeypatch,stage):
    target=tmp_path/'rules.md';target.write_text('before\n')
    p=proposal(store,target)
    writes=[];original=apply._atomic_write
    def write(path,data):writes.append(path);return original(path,data)
    monkeypatch.setattr(apply,'_atomic_write',write)
    def crash(point,*_):
        if point==stage:raise KeyboardInterrupt('automatic writer stopped')
    monkeypatch.setattr(apply,'_operation_checkpoint',crash)
    with pytest.raises(KeyboardInterrupt):apply.apply_proposal(store,cfg,p)
    with closing(Store(store.db_path,read_only=True)) as reader:
        row=reader.query_one('SELECT * FROM instruction_operations')
        assert row['state']=='running'
        assert json.loads(row['record_json'])['proposal']['diff_unified']==p['diff_unified']
        assert reader.query_one('SELECT status FROM proposals WHERE id=?',(p['id'],))['status']=='gated_pass'
    monkeypatch.setattr(apply,'_operation_checkpoint',lambda *_:None)
    with closing(Store(store.db_path,migrate=False)) as restarted:
        result=run_once(restarted,cfg)
        assert result['state']=='completed'
    assert target.read_text()=='before\nafter\n'
    assert writes==[target]
    assert len(store.query("SELECT * FROM proposal_events WHERE event='applied'"))==1
    assert apply.apply_proposal(store,cfg,p)['outcome']=='applied'
    assert writes==[target], 'a repeated API call wrote again after acknowledgement'


@pytest.mark.parametrize('stage', ['prepared','ref_updated','before_ack'])
def test_automatic_git_recovery_keeps_recorded_branch_and_current_checkout(cfg,store,tmp_path,monkeypatch,stage):
    repo=init_git_repo(tmp_path/'project','AGENTS.md','before\n');target=repo/'AGENTS.md'
    p=proposal(store,target);head=run_git(['rev-parse','HEAD'],repo)
    index=(repo/'.git/index').read_bytes()
    def crash(point,*_):
        if point==stage:raise KeyboardInterrupt('interrupted')
    monkeypatch.setattr(apply,'_operation_checkpoint',crash)
    with pytest.raises(KeyboardInterrupt):apply.apply_proposal(store,cfg,p)
    monkeypatch.setattr(apply,'_operation_checkpoint',lambda *_:None)
    changed=replace(cfg,project_branch_name='different/branch')
    assert run_once(store,changed)['state']=='completed'
    assert run_git(['rev-list','--count',cfg.project_branch_name],repo)=='2'
    assert run_git(['show',cfg.project_branch_name+':AGENTS.md'],repo)=='before\nafter'
    assert run_git(['branch','--list',changed.project_branch_name],repo)==''
    assert run_git(['rev-parse','HEAD'],repo)==head
    assert target.read_text()=='before\n' and (repo/'.git/index').read_bytes()==index


@pytest.mark.parametrize('stage,expected', [('prepared','cancelled'),('file_replaced','completed')])
def test_policy_revocation_stops_an_unwritten_operation_but_reconciles_a_prior_write(cfg,store,tmp_path,monkeypatch,stage,expected):
    target=tmp_path/'rules.md';target.write_text('before\n');p=proposal(store,target)
    def crash(point,*_):
        if point==stage:raise KeyboardInterrupt('interrupted')
    monkeypatch.setattr(apply,'_operation_checkpoint',crash)
    with pytest.raises(KeyboardInterrupt):apply.apply_proposal(store,cfg,p)
    set_class_policy(store,'global',False)
    monkeypatch.setattr(apply,'_operation_checkpoint',lambda *_:None)
    result=run_once(store,cfg)
    assert result['state']==expected
    assert target.read_text()==('before\n' if expected=='cancelled' else 'before\nafter\n')
    assert store.query_one('SELECT status FROM proposals WHERE id=?',(p['id'],))['status']==('held' if expected=='cancelled' else 'applied')


def test_unknown_post_crash_content_blocks_all_writers_until_explicit_reconciliation(cfg,store,tmp_path,monkeypatch):
    target=tmp_path/'rules.md';target.write_text('before\n');p=proposal(store,target)
    def crash(point,*_):
        if point=='file_replaced':raise KeyboardInterrupt('interrupted')
    monkeypatch.setattr(apply,'_operation_checkpoint',crash)
    with pytest.raises(KeyboardInterrupt):apply.apply_proposal(store,cfg,p)
    target.write_text('human content after the interrupted write\n')
    monkeypatch.setattr(apply,'_operation_checkpoint',lambda *_:None)
    result=run_once(store,cfg)
    assert result['state']=='blocked' and result['error_code']=='TargetChanged'
    other=proposal(store,target,before=target.read_text(),after=target.read_text()+'next\n')
    with pytest.raises(apply.DeliveryBlocked,match='reconciliation'):apply.apply_proposal(store,cfg,other)
    # Review can record intent, but the executor must respect the same lane.
    store.update('proposals','id',other['id'],{'status':'ungated'});store.commit()
    other=store.query_one('SELECT * FROM proposals WHERE id=?',(other['id'],))
    command=approve(store,cfg,other)
    assert run_once(store,cfg)['state']=='blocked'
    assert target.read_text()=='human content after the interrupted write\n'
    target.write_text('before\nafter\n')
    assert apply.resume_write_operation(store,cfg,result['id'])['state']=='completed'
    assert len(store.query("SELECT * FROM proposal_events WHERE proposal_id=? AND event='applied'",(p['id'],)))==1


def test_automatic_preparation_does_not_commit_a_callers_unfinished_transaction(cfg,store,tmp_path):
    target=tmp_path/'rules.md';target.write_text('before\n');p=proposal(store,target)
    with store.transaction(write=True):
        store.update('learnings','id',p['learning_id'],{'rule_text':'unsaved caller edit'})
        with pytest.raises(apply.ApplyError,match='transaction'):apply.apply_proposal(store,cfg,p)
        assert store.conn.in_transaction
    assert target.read_text()=='before\n'


def test_post_write_recovery_preserves_a_later_rejection(cfg,store,tmp_path,monkeypatch):
    target=tmp_path/'rules.md';target.write_text('before\n');p=proposal(store,target)
    def crash(point,*_):
        if point=='file_replaced':raise KeyboardInterrupt('interrupted')
    monkeypatch.setattr(apply,'_operation_checkpoint',crash)
    with pytest.raises(KeyboardInterrupt):apply.apply_proposal(store,cfg,p)
    store.update('proposals','id',p['id'],{'status':'rejected_user'});store.commit()
    monkeypatch.setattr(apply,'_operation_checkpoint',lambda *_:None)
    assert run_once(store,cfg)['state']=='completed'
    assert store.query_one('SELECT status FROM proposals WHERE id=?',(p['id'],))['status']=='rejected_user'
    assert len(store.query("SELECT * FROM proposal_events WHERE event='applied'"))==1
    assert target.read_text()=='before\nafter\n'


def test_a_recorded_direct_file_cannot_silently_become_a_working_tree_write(cfg,store,tmp_path,monkeypatch):
    folder=tmp_path/'new-repo';folder.mkdir()
    target=folder/'AGENTS.md';target.write_text('before\n');p=proposal(store,target)
    def crash(point,*_):
        if point=='prepared':raise KeyboardInterrupt('prepared')
    monkeypatch.setattr(apply,'_operation_checkpoint',crash)
    with pytest.raises(KeyboardInterrupt):apply.apply_proposal(store,cfg,p)
    init_git_repo(folder,'AGENTS.md','before\n')
    monkeypatch.setattr(apply,'_operation_checkpoint',lambda *_:None)
    result=run_once(store,cfg)
    assert result['state']=='blocked' and result['error_code']=='DestinationChanged'
    assert target.read_text()=='before\n'
    assert run_git(['status','--porcelain'],folder)==''


def test_a_real_process_exit_is_recovered_without_repeating_the_write(cfg,store,tmp_path):
    import subprocess,sys
    from dataclasses import asdict
    target=tmp_path/'rules.md';target.write_text('before\n');p=proposal(store,target)
    config=tmp_path/'config.json';config.write_text(json.dumps(asdict(cfg)))
    script='''
import json,os,sys
from self_improve.config import Config
from self_improve.store import Store
from self_improve import apply
cfg=Config(**json.load(open(sys.argv[1])))
store=Store(cfg.state_path('state.db'),migrate=False)
proposal=store.query_one('SELECT * FROM proposals WHERE id=?',(sys.argv[2],))
apply._operation_checkpoint=lambda stage, oid: os._exit(77) if stage=='file_replaced' else None
apply.apply_proposal(store,cfg,proposal)
'''
    child=subprocess.run([sys.executable,'-c',script,str(config),p['id']],capture_output=True,text=True,timeout=20)
    assert child.returncode==77,child.stderr
    assert target.read_text()=='before\nafter\n'
    assert run_once(store,cfg)['state']=='completed'
    assert len(store.query("SELECT * FROM proposal_events WHERE event='applied'"))==1


def test_failed_operation_requires_an_explicit_retry_and_retains_failure_history(cfg,store,tmp_path,monkeypatch,capsys):
    from self_improve import cli
    from self_improve.operations import operation_status
    target=tmp_path/'rules.md';target.write_text('before\n');p=proposal(store,target)
    def fail(point,*_):
        if point=='file_replaced':raise OSError('invented failure after rename')
    monkeypatch.setattr(apply,'_operation_checkpoint',fail)
    with pytest.raises(apply.DeliveryBlocked,match='invented failure'):apply.apply_proposal(store,cfg,p)
    row=store.query_one('SELECT * FROM instruction_operations')
    assert row['state']=='failed' and run_once(store,cfg) is None
    monkeypatch.setattr(apply,'_operation_checkpoint',lambda *_:None)
    monkeypatch.setattr(cli,'load_config',lambda *_:cfg)
    assert cli.main(['worker','--operation',row['id']])==0
    output=json.loads(capsys.readouterr().out)
    assert output['id']==row['id'] and output['state']=='completed'
    assert 'record' not in output and 'before_content' not in json.dumps(output)
    status=operation_status(store,row['id'])
    assert status['attempts']==2 and status['failures'][0]['error_code']=='OSError'
    assert 'invented failure' in status['failures'][0]['error_detail']
    assert target.read_text()=='before\nafter\n'


@pytest.mark.parametrize('field', ['record_json','checkpoint_json','result_json','failures_json'])
def test_corrupt_operation_records_never_report_valid_completion(cfg,store,tmp_path,field):
    from self_improve.operations import operation_status
    from self_improve.commands import CommandError
    target=tmp_path/'rules.md';target.write_text('before\n');p=proposal(store,target)
    assert apply.apply_proposal(store,cfg,p)['outcome']=='applied'
    row=store.query_one('SELECT * FROM instruction_operations')
    store.update('instruction_operations','id',row['id'],{field:'{}'});store.commit()
    with pytest.raises(CommandError):operation_status(store,row['id'])
    assert target.read_text()=='before\nafter\n'


def test_a_completed_operation_does_not_report_a_new_application_after_rollback(cfg,store,tmp_path):
    target=tmp_path/'rules.md';target.write_text('before\n');p=proposal(store,target)
    apply.apply_proposal(store,cfg,p)
    apply.rollback(store,cfg,p['id'])
    current=store.query_one('SELECT * FROM proposals WHERE id=?',(p['id'],))
    assert apply.apply_proposal(store,cfg,current)['outcome']=='held'
    with pytest.raises(apply.ApplyError,match='changed'):apply.apply_proposal(store,cfg,p)
    assert target.read_text()=='before\n'
    assert len(store.query("SELECT * FROM proposal_events WHERE event='applied'"))==1


def test_status_names_unfinished_operations_and_recovery_without_exposing_file_contents(cfg,store,tmp_path,monkeypatch,capsys):
    from self_improve import cli
    target=tmp_path/'rules.md';target.write_text('before\n');p=proposal(store,target)
    def fail(point,*_):
        if point=='file_replaced':raise OSError('retained failure cause')
    monkeypatch.setattr(apply,'_operation_checkpoint',fail)
    with pytest.raises(apply.DeliveryBlocked):apply.apply_proposal(store,cfg,p)
    monkeypatch.setattr(cli,'load_config',lambda *_:cfg)
    assert cli.main(['status'])==0
    report=json.loads(capsys.readouterr().out)['instruction_operations']
    assert report['counts']['failed']==1 and report['shown']==1 and report['remaining']==0
    item=report['unfinished'][0]
    assert item['error_code']=='OSError' and 'retained failure cause' in item['error_detail']
    assert item['reconcile_with']=='selfimprove worker --operation '+item['id']
    assert 'before_content' not in json.dumps(report) and 'record_json' not in json.dumps(report)
    assert store.query_one('SELECT state FROM instruction_operations')['state']=='failed'


def test_cancelled_automatic_preparation_releases_the_lane_for_a_human_approval(cfg,store,tmp_path,monkeypatch):
    target=tmp_path/'rules.md';target.write_text('before\n');p=proposal(store,target)
    def crash(point,*_):
        if point=='prepared':raise KeyboardInterrupt('prepared')
    monkeypatch.setattr(apply,'_operation_checkpoint',crash)
    with pytest.raises(KeyboardInterrupt):apply.apply_proposal(store,cfg,p)
    set_class_policy(store,'global',False)
    monkeypatch.setattr(apply,'_operation_checkpoint',lambda *_:None)
    assert run_once(store,cfg)['state']=='cancelled'
    current=store.query_one('SELECT * FROM proposals WHERE id=?',(p['id'],))
    cmd=approve(store,cfg,current)
    result=run_once(store,cfg)
    assert result['id']==cmd['id'] and result['state']=='completed'
    assert target.read_text()=='before\nafter\n'


def test_worker_refuses_the_previous_schema_without_migrating_it(cfg,tmp_path,monkeypatch):
    from self_improve import store as storage
    from self_improve.commands import CommandError
    monkeypatch.setattr(storage,'MIGRATIONS',[m for m in storage.MIGRATIONS if m[0]<'0012_instruction_operations'])
    with closing(Store(cfg.state_path('state.db'))) as old:
        before=old.query('SELECT name FROM schema_migrations ORDER BY name')
        with pytest.raises(CommandError,match='Upgrade'):run_once(old,cfg)
        assert old.query('SELECT name FROM schema_migrations ORDER BY name')==before
        assert not old.query("SELECT name FROM sqlite_master WHERE name='instruction_operations'")
