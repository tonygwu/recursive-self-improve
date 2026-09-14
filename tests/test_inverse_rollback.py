"""Rollback removes one applied contribution and survives interrupted publication."""
from dataclasses import replace
import json

import pytest

from self_improve import apply
from self_improve.worker import run_once
from tests.test_apply import cfg, store, insert_proposal, make_diff, init_git_repo, run_git
from tests.test_delivery_worker import approve

BASE='# Rules\n\n- Original.\n'
A=BASE+'- Rule A.\n'
B=A+'- Rule B.\n'


def propose(store,target,old,new,**kw):
    return insert_proposal(store,target=target,diff=make_diff(old,new),**kw)


@pytest.mark.parametrize('git', [False,True])
def test_rollback_a_after_b_preserves_b_and_later_human_content(cfg,store,tmp_path,git):
    if git:
        repo=init_git_repo(tmp_path/'project','AGENTS.md',BASE);target=repo/'AGENTS.md'
    else:
        target=tmp_path/'rules.md';target.write_text(BASE)
    one=propose(store,target,BASE,A);apply.apply_proposal(store,cfg,one)
    two=propose(store,target,A,B);apply.apply_proposal(store,cfg,two)
    if not git:target.write_text('# Human heading\n'+B+'\nHuman footer.\n')
    result=apply.rollback(store,cfg,one['id'])
    assert result['outcome']=='rolled_back'
    expected=BASE+'- Rule B.\n'
    if git:
        assert run_git(['show',cfg.project_branch_name+':AGENTS.md'],repo)==expected.strip()
        assert target.read_text()==BASE
    else:assert target.read_text()=='# Human heading\n'+expected+'\nHuman footer.\n'
    assert store.query_one('SELECT status FROM proposals WHERE id=?',(two['id'],))['status']=='applied'
    assert apply.rollback(store,cfg,one['id'])==result
    assert len(store.query("SELECT * FROM proposal_events WHERE event='rolled_back'"))==1


def test_a_changed_rule_conflicts_instead_of_restoring_the_old_file(cfg,store,tmp_path):
    target=tmp_path/'rules.md';target.write_text(BASE)
    one=propose(store,target,BASE,A);apply.apply_proposal(store,cfg,one)
    changed=BASE+'- Rule A edited by a human.\n- Rule B.\n';target.write_text(changed)
    with pytest.raises(apply.ApplyError,match='conflict|changed|locate'):apply.rollback(store,cfg,one['id'])
    assert target.read_text()==changed
    assert store.query_one('SELECT status FROM proposals WHERE id=?',(one['id'],))['status']=='applied'


def test_rollback_uses_the_delivered_branch_when_configuration_changes(cfg,store,tmp_path):
    repo=init_git_repo(tmp_path/'project','AGENTS.md',BASE);target=repo/'AGENTS.md'
    p=propose(store,target,BASE,A);apply.apply_proposal(store,cfg,p)
    result=apply.rollback(store,replace(cfg,project_branch_name='different/branch'),p['id'])
    assert result['outcome']=='rolled_back'
    assert run_git(['show',cfg.project_branch_name+':AGENTS.md'],repo)==BASE.strip()
    assert run_git(['branch','--list','different/branch'],repo)==''


@pytest.mark.parametrize('identical',[False,True])
def test_a_combined_command_rolls_back_only_the_selected_physical_contribution(cfg,store,tmp_path,identical):
    before='first\nmiddle\nlast\n';one_after='FIRST\nmiddle\nlast\n';two_after=one_after if identical else 'first\nmiddle\nLAST\n'
    target=tmp_path/'rules.md';target.write_text(before)
    p=propose(store,target,before,one_after,status='ungated')
    q=propose(store,target,before,two_after,status='ungated')
    approve(store,cfg,p,q);assert run_once(store,cfg)['state']=='completed'
    result=apply.rollback(store,cfg,p['id'])
    assert target.read_text()==(before if identical else two_after)
    expected={p['id'],q['id']} if identical else {p['id']}
    assert set(result['affected_proposal_ids'])==expected
    assert store.query_one('SELECT status FROM proposals WHERE id=?',(q['id'],))['status']==('rolled_back' if identical else 'applied')
    if not identical:
        apply.rollback(store,cfg,q['id'])
        assert target.read_text()==before


@pytest.mark.parametrize('git,stage', [(False,'prepared'),(False,'file_replaced'),(False,'before_ack'),(True,'prepared'),(True,'ref_updated'),(True,'before_ack')])
def test_rollback_resumes_one_prepared_change_after_interruption(cfg,store,tmp_path,monkeypatch,git,stage):
    if git:
        repo=init_git_repo(tmp_path/'project','AGENTS.md',BASE);target=repo/'AGENTS.md'
    else:
        target=tmp_path/'rules.md';target.write_text(BASE)
    p=propose(store,target,BASE,A);apply.apply_proposal(store,cfg,p)
    def crash(point,*_):
        if point==stage:raise KeyboardInterrupt('rollback process stopped')
    monkeypatch.setattr(apply,'_operation_checkpoint',crash)
    with pytest.raises(KeyboardInterrupt):apply.rollback(store,cfg,p['id'])
    row=store.query_one("SELECT * FROM instruction_operations WHERE kind='rollback'")
    assert row['state']=='running'
    monkeypatch.setattr(apply,'_operation_checkpoint',lambda *_:None)
    assert run_once(store,cfg)['state']=='completed'
    result=apply.rollback(store,cfg,p['id'])
    assert result['outcome']=='rolled_back'
    assert len(store.query("SELECT * FROM proposal_events WHERE event='rolled_back'"))==1
    if git:
        assert run_git(['rev-list','--count',cfg.project_branch_name],repo)=='3'
        assert run_git(['show',cfg.project_branch_name+':AGENTS.md'],repo)==BASE.strip()
    else:assert target.read_text()==BASE


def test_created_file_rollback_preserves_new_content_and_only_removes_an_empty_result(cfg,store,tmp_path):
    target=tmp_path/'new.md'
    p=propose(store,target,'','- Rule A.\n');apply.apply_proposal(store,cfg,p)
    target.write_text('- Rule A.\n- Human addition.\n')
    result=apply.rollback(store,cfg,p['id'])
    assert target.read_text()=='- Human addition.\n' and result['restored_absent'] is False


def test_rollback_uses_frozen_delivered_text_when_the_proposal_is_later_edited(cfg,store,tmp_path):
    target=tmp_path/'rules.md';target.write_text(BASE)
    p=propose(store,target,BASE,A);apply.apply_proposal(store,cfg,p)
    store.update('proposals','id',p['id'],{'diff_unified':make_diff(BASE,BASE+'something else\n')});store.commit()
    assert apply.rollback(store,cfg,p['id'])['outcome']=='rolled_back'
    assert target.read_text()==BASE


@pytest.mark.parametrize('before,applied,current,expected', [
    ('old\r\nend', 'new\r\nend', 'heading\r\nnew\r\nend', 'heading\r\nold\r\nend'),
    ('a\nb\nc\n', 'a\nc\n', 'heading\na\nc\nfooter\n', 'heading\na\nb\nc\nfooter\n'),
    ('a\nold\nc\n', 'a\nnew\nc\n', 'a\nnew\nc\nfooter\n', 'a\nold\nc\nfooter\n'),
    ('one\nkeep\ntwo\n', 'ONE\nkeep\nTWO\n', 'head\nONE\nkeep\nTWO\nfoot\n', 'head\none\nkeep\ntwo\nfoot\n'),
])
def test_exact_inverse_preserves_unrelated_bytes(before, applied, current, expected):
    from self_improve.rollback import reverse_applied_change
    assert reverse_applied_change(before, applied, current) == expected


@pytest.mark.parametrize('before,applied,current', [
    # The original inserted copy vanished; the identical old copy is not its substitute.
    ('a\nkeep\nb\n', 'a\nkeep\nb\nkeep\n', 'a\nkeep\nb\nfooter\n'),
    ('a\nb\nc\n', 'a\nc\n', 'a\nchanged anchor\nc\n'),
    ('a\nold\nc\n', 'a\nnew\nc\n', 'a\nnew\nc\na\nnew\nc\n'),
    ('one\nkeep\ntwo\n', 'ONE\nkeep\nTWO\n', 'TWO\nkeep\nONE\n'),
])
def test_missing_or_ambiguous_changed_blocks_conflict(before, applied, current):
    from self_improve.rollback import reverse_applied_change, RollbackConflict
    with pytest.raises(RollbackConflict):
        reverse_applied_change(before, applied, current)


def test_a_real_process_exit_after_file_removal_reconciles_absence(cfg, store, tmp_path):
    import subprocess
    import sys
    from dataclasses import asdict
    target = tmp_path / 'new.md'
    p = propose(store, target, '', '- Rule A.\n')
    apply.apply_proposal(store, cfg, p)
    config = tmp_path / 'config.json'
    config.write_text(json.dumps(asdict(cfg)))
    script = '''
import json,os,sys
from self_improve.config import Config
from self_improve.store import Store
from self_improve import apply
cfg=Config(**json.load(open(sys.argv[1])))
store=Store(cfg.state_path('state.db'),migrate=False)
apply._operation_checkpoint=lambda stage, oid: os._exit(77) if stage=='file_removed' else None
apply.rollback(store,cfg,sys.argv[2])
'''
    child = subprocess.run([sys.executable, '-c', script, str(config), p['id']],
                           capture_output=True, text=True, timeout=20)
    assert child.returncode == 77, child.stderr
    assert not target.exists()
    assert store.query("SELECT * FROM proposal_events WHERE event='rolled_back'") == []
    result = run_once(store, cfg)
    assert result['state'] == 'completed' and result['result']['restored_absent'] is True
    assert apply.rollback(store, cfg, p['id']) == result['result']
    assert len(store.query("SELECT * FROM proposal_events WHERE event='rolled_back'")) == 1


def test_a_new_git_file_is_removed_from_the_recorded_branch_only(cfg, store, tmp_path):
    repo = init_git_repo(tmp_path / 'project', 'README.md', 'project\n')
    target = repo / 'AGENTS.md'
    p = propose(store, target, '', '- Rule A.\n')
    apply.apply_proposal(store, cfg, p)
    # Uncommitted operator content in the checkout is not the delivery branch.
    target.write_text('operator work\n')
    result = apply.rollback(store, cfg, p['id'])
    assert result['restored_absent'] is True
    assert run_git(['ls-tree', cfg.project_branch_name, '--', 'AGENTS.md'], repo) == ''
    assert target.read_text() == 'operator work\n'
    assert run_git(['rev-list', '--count', cfg.project_branch_name], repo) == '3'


def test_a_later_reviewed_application_gets_its_own_inverse_identity(cfg, store, tmp_path):
    target = tmp_path / 'rules.md'
    target.write_text(BASE)
    p = propose(store, target, BASE, A)
    apply.apply_proposal(store, cfg, p)
    first = apply.rollback(store, cfg, p['id'])
    # Seed a new reviewable revision. The product reapplication control is separate.
    store.update('proposals', 'id', p['id'], {'status': 'ungated'})
    store.commit()
    approve(store, cfg, p)
    assert run_once(store, cfg)['state'] == 'completed'
    second = apply.rollback(store, cfg, p['id'])
    assert second['operation_id'] != first['operation_id']
    assert second['application_id'] != first['application_id']
    assert target.read_text() == BASE
    assert len(store.query("SELECT * FROM proposal_events WHERE event='rolled_back'")) == 2


def test_historical_snapshot_application_ignores_a_later_proposal_edit(cfg, store, tmp_path):
    target = tmp_path / 'rules.md'
    target.write_text(BASE)
    p = propose(store, target, BASE, A)
    apply.apply_proposal(store, cfg, p)
    event = store.query_one("SELECT * FROM proposal_events WHERE event='applied'")
    note = json.loads(event['note'])
    del note['operation_id']  # Old published events have snapshots but no immutable record.
    store.update('proposal_events', 'id', event['id'], {'note': json.dumps(note)})
    store.update('proposals', 'id', p['id'], {'diff_unified': make_diff(BASE, BASE + 'new proposal\n')})
    store.commit()
    target.write_text(A + 'human addition\n')
    result = apply.rollback(store, cfg, p['id'])
    assert result['application_id'] == 'event:' + event['id']
    assert target.read_text() == BASE + 'human addition\n'


@pytest.mark.parametrize('change', [{'mode': 'git_branch'}, {'snapshot_after': 'missing-commit'}, {'branch': 'changed/branch'}])
def test_corrupt_application_destination_or_snapshot_is_named_before_writing(cfg, store, tmp_path, change):
    target = tmp_path / 'rules.md'
    target.write_text(BASE)
    p = propose(store, target, BASE, A)
    apply.apply_proposal(store, cfg, p)
    event = store.query_one("SELECT * FROM proposal_events WHERE event='applied'")
    note = json.loads(event['note'])
    store.update('proposal_events', 'id', event['id'], {'note': json.dumps({**note, **change})})
    store.commit()
    with pytest.raises(apply.DeliveryBlocked, match='application|Application|snapshot'):
        apply.rollback(store, cfg, p['id'])
    assert target.read_text() == A
