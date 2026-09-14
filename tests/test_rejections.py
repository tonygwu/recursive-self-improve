"""A rejection follows its lesson and canonical target, not a proposal ID."""
from contextlib import closing

import pytest

from self_improve import apply
from self_improve.commands import CommandError, command_status, review_snapshot, submit_command
from self_improve.execution_policy import waiting_proposals
from self_improve.store import Store, new_id
from self_improve.worker import run_once
from tests.test_apply import cfg, store, OLD, NEW, insert_proposal, init_git_repo, make_diff, run_git


def proposal(store, target, *, lesson=None, status='pending', diff=None):
    p = insert_proposal(store, target=target, diff=diff or make_diff(OLD, NEW), status=status)
    if lesson:
        store.update('proposals', 'id', p['id'], {'learning_id': lesson})
        store.commit()
    return store.query_one('SELECT * FROM proposals WHERE id=?', (p['id'],))


def decision(store, cfg, action, proposals, key=None):
    return {'action': action, 'request_key': key or new_id(), 'members': [
        {k: review_snapshot(store, p['id'], cfg)[k] for k in ('proposal_id', 'revision')}
        for p in proposals]}


def waiting(store, cfg):
    return {p['id'] for p in waiting_proposals(store, cfg)}


def test_target_rejection_keeps_other_targets_and_the_global_pool_available(store, cfg, tmp_path):
    from self_improve.cluster import rejected_vectors
    a = proposal(store, tmp_path/'a.md')
    b = proposal(store, tmp_path/'b.md', lesson=a['learning_id'])
    body = decision(store, cfg, 'reject_target', [a])
    result = submit_command(store, cfg, body)
    assert result['state'] == 'completed' and result['targets'] == []
    assert result['members'][0]['decision_scope'] == 'target'
    assert waiting(store, cfg) == {b['id']}
    assert store.query_one('SELECT status FROM learnings WHERE id=?', (a['learning_id'],))['status'] != 'rejected'
    class NoEmbedding:
        def cached_vector(self, *_):
            pytest.fail('target rejection entered the global rejected-rule pool')
    assert rejected_vectors(store, NoEmbedding()) == []
    assert submit_command(store, cfg, body) == result
    assert command_status(store, result['id']) == result
    later = proposal(store, tmp_path/'a.md', lesson=a['learning_id'])
    assert later['id'] not in waiting(store, cfg)
    with pytest.raises(CommandError, match='key'):
        submit_command(store, cfg, {**body, 'action': 'reject_lesson'})
    assert not cfg.state_path('snapshots').exists()


def test_rejection_is_atomic_and_can_reject_conflicting_alternatives(store, cfg, tmp_path):
    a = proposal(store, tmp_path/'a.md')
    b = proposal(store, tmp_path/'a.md', lesson=a['learning_id'], diff=make_diff(OLD, NEW+'different\n'))
    body = decision(store, cfg, 'reject_target', [a,b])
    store.update('proposals', 'id', b['id'], {'diff_unified': make_diff(OLD, NEW+'changed\n')})
    store.commit()
    with pytest.raises(CommandError) as error:
        submit_command(store, cfg, body)
    assert error.value.code == 'StaleRevision'
    assert store.query('SELECT * FROM commands') == []
    assert waiting(store, cfg) == {a['id'], b['id']}
    result = submit_command(store, cfg, decision(store, cfg, 'reject_target', [a,b]))
    assert len(result['members']) == 2 and waiting(store, cfg) == set()


def test_target_rejection_survives_independent_clones_and_new_proposal_ids(store, cfg, tmp_path):
    from self_improve.rejections import rejection_reason
    clones = [init_git_repo(tmp_path/name, 'AGENTS.md', OLD) for name in ('repo-0','repo-1')]
    for root in clones:
        run_git(['remote','add','origin','https://example.invalid/team/product.git'], root)
    a = proposal(store, clones[0]/'AGENTS.md')
    submit_command(store, cfg, decision(store, cfg, 'reject_target', [a]))
    clone = proposal(store, clones[1]/'AGENTS.md', lesson=a['learning_id'], status='gated_pass')
    different = proposal(store, clones[1]/'CLAUDE.md', lesson=a['learning_id'])
    assert rejection_reason(store, cfg, clone)['code'] == 'TargetRejected'
    assert rejection_reason(store, cfg, different) is None
    outcome = apply.apply_proposal(store, cfg, clone)
    assert outcome['outcome'] == 'held' and outcome['reason'] == 'target_rejected'
    assert run_git(['branch','--list',cfg.project_branch_name], clones[1]) == ''
    assert not cfg.state_path('snapshots').exists()


def test_a_changed_canonical_target_requires_fresh_review(store, cfg, tmp_path):
    root = init_git_repo(tmp_path/'repo', 'AGENTS.md', OLD)
    run_git(['remote','add','origin','https://example.invalid/team/one.git'], root)
    a = proposal(store, root/'AGENTS.md')
    body = decision(store, cfg, 'reject_target', [a])
    run_git(['remote','set-url','origin','https://example.invalid/team/two.git'], root)
    with pytest.raises(CommandError) as error:
        submit_command(store, cfg, body)
    assert error.value.code == 'StaleRevision'
    assert store.query('SELECT * FROM commands') == []


@pytest.mark.parametrize('dedup', ['duplicate','amend'])
def test_reject_lesson_cannot_be_resurrected_by_mining(store, cfg, tmp_path, dedup):
    from self_improve.miner import _persist_dedup_decision
    a = proposal(store, tmp_path/'a.md')
    b = proposal(store, tmp_path/'b.md', lesson=a['learning_id'])
    submit_command(store, cfg, decision(store, cfg, 'reject_lesson', [a]))
    assert waiting(store, cfg) == set()
    session, incident = new_id(), new_id()
    path=str(tmp_path/'invented.jsonl')
    store.insert('sessions', {'session_id':session, 'source':'codex', 'file_path':path})
    store.insert('incidents', {'id':incident, 'session_file':path, 'session_id':session, 'signal_type':'correction',
                             'ts':'2026-09-01T00:00:00Z', 'created_at':'2026-09-01T00:00:00Z'})
    store.commit()
    before = store.query_one('SELECT * FROM learnings WHERE id=?', (a['learning_id'],))
    _persist_dedup_decision(store, {'id':incident,'ts':'2026-09-01T00:00:00Z'},
        {'amended_rule_text':'Resurrected rule','amended_why':'More evidence'}, before, dedup, '')
    after = store.query_one('SELECT * FROM learnings WHERE id=?', (a['learning_id'],))
    assert after == before
    assert store.query_one('SELECT status FROM incidents WHERE id=?', (incident,))['status'] == 'dismissed'
    assert b['id'] not in waiting(store, cfg)


@pytest.mark.parametrize('stage', ['queued','prepared','file_replaced'])
def test_lesson_rejection_revokes_unwritten_delivery_and_acknowledges_observed_writes(store, cfg, tmp_path, monkeypatch, stage):
    target=tmp_path/'a.md'; target.write_text(OLD)
    a=proposal(store,target)
    b=proposal(store,tmp_path/'b.md',lesson=a['learning_id'])
    approved=submit_command(store,cfg,decision(store,cfg,'approve',[a]))
    if stage!='queued':
        def crash(point,*_):
            if point==stage: raise KeyboardInterrupt('interrupted approval')
        monkeypatch.setattr(apply,'_delivery_checkpoint',crash)
        with pytest.raises(KeyboardInterrupt): run_once(store,cfg)
        monkeypatch.setattr(apply,'_delivery_checkpoint',lambda *_:None)
    submit_command(store,cfg,decision(store,cfg,'reject_lesson',[b]))
    result=run_once(store,cfg)
    assert result['id']==approved['id']
    assert result['state']==('completed' if stage=='file_replaced' else 'cancelled')
    assert target.read_text()==(NEW if stage=='file_replaced' else OLD)
    assert store.query_one('SELECT status FROM proposals WHERE id=?',(a['id'],))['status']=='rejected_user'
    assert len(store.query("SELECT * FROM proposal_events WHERE event='applied'"))==(1 if stage=='file_replaced' else 0)
    assert run_once(store,cfg) is None


def test_legacy_rejected_proposals_stay_lesson_wide(store, cfg, tmp_path):
    from self_improve.rejections import rejection_reason
    a=proposal(store,tmp_path/'a.md',status='rejected_user')
    b=proposal(store,tmp_path/'b.md',lesson=a['learning_id'])
    assert rejection_reason(store,cfg,b)['code']=='LessonRejected'
    assert waiting(store,cfg)==set()


def test_rejection_rolls_back_every_member_when_event_recording_fails(store, cfg, tmp_path, monkeypatch):
    a=proposal(store,tmp_path/'a.md'); b=proposal(store,tmp_path/'b.md',lesson=a['learning_id'])
    body=decision(store,cfg,'reject_target',[a,b])
    original=store.insert
    count=0
    def fail(table,row):
        nonlocal count
        if table=='proposal_events':
            count+=1
            if count==2: raise RuntimeError('event persistence failed')
        return original(table,row)
    monkeypatch.setattr(store,'insert',fail)
    with pytest.raises(RuntimeError,match='event persistence'): submit_command(store,cfg,body)
    assert waiting(store,cfg)=={a['id'],b['id']}
    assert store.query('SELECT * FROM commands')==[]
    assert store.query('SELECT * FROM rejection_members')==[]


def test_scope_corruption_is_a_named_error_before_any_writer_runs(store,cfg,tmp_path):
    a=proposal(store,tmp_path/'a.md')
    command=submit_command(store,cfg,decision(store,cfg,'reject_target',[a]))
    row=store.query_one('SELECT * FROM rejection_members')
    store.update('rejection_members','id',row['id'],{'target_identity_json':'{}'})
    store.commit()
    with pytest.raises(CommandError) as error: command_status(store,command['id'])
    assert error.value.code=='RejectionDataError'
    assert not cfg.state_path('snapshots').exists()


def test_target_scope_follows_explicit_learning_merges_and_exact_new_lessons(store,cfg,tmp_path):
    from self_improve.rejections import rejection_reason
    a=proposal(store,tmp_path/'a.md')
    submit_command(store,cfg,decision(store,cfg,'reject_target',[a]))
    equivalent=proposal(store,tmp_path/'a.md')
    assert rejection_reason(store,cfg,equivalent)['code']=='TargetRejected'
    store.update('learnings','id',equivalent['learning_id'],{'rule_text':'An amended version'})
    assert rejection_reason(store,cfg,equivalent) is None
    store.update('learnings','id',a['learning_id'],{'status':'superseded','duplicate_of':equivalent['learning_id']})
    store.commit()
    assert rejection_reason(store,cfg,equivalent)['code']=='TargetRejected'


def test_later_project_identity_cache_resolution_does_not_narrow_a_rejection(store,cfg,tmp_path):
    from self_improve.rejections import rejection_reason
    root=init_git_repo(tmp_path/'repo','AGENTS.md',OLD)
    remote='example.invalid/team/product'
    run_git(['remote','add','origin',f'https://{remote}.git'],root)
    a=proposal(store,root/'AGENTS.md')
    submit_command(store,cfg,decision(store,cfg,'reject_target',[a]))
    store.insert('project_identity_cache',{'remote_norm':remote,'project_key':'github:1000000001','display':'demo',
                 'method':'gh_repo_id','resolved_at':'2026-09-13T00:00:00Z'})
    store.commit()
    b=proposal(store,root/'AGENTS.md',lesson=a['learning_id'])
    assert rejection_reason(store,cfg,b)['code']=='TargetRejected'


@pytest.mark.parametrize('stage',['prepared','file_replaced'])
def test_target_rejection_reconciles_automatic_delivery(store,cfg,tmp_path,monkeypatch,stage):
    target=tmp_path/'a.md'; target.write_text(OLD)
    a=proposal(store,target,status='gated_pass')
    b=proposal(store,target,lesson=a['learning_id'])
    def crash(point,*_):
        if point==stage: raise KeyboardInterrupt('automatic interruption')
    monkeypatch.setattr(apply,'_operation_checkpoint',crash)
    with pytest.raises(KeyboardInterrupt): apply.apply_proposal(store,cfg,a)
    monkeypatch.setattr(apply,'_operation_checkpoint',lambda *_:None)
    submit_command(store,cfg,decision(store,cfg,'reject_target',[b]))
    result=run_once(store,cfg)
    assert result['state']==('completed' if stage=='file_replaced' else 'cancelled')
    assert target.read_text()==(NEW if stage=='file_replaced' else OLD)
    assert store.query_one('SELECT status FROM proposals WHERE id=?',(a['id'],))['status']=='rejected_user'


def test_rejecting_one_member_cancels_a_combined_write_and_returns_its_unrejected_peer(store,cfg,tmp_path):
    from self_improve.review import preview_selection
    target=tmp_path/'a.md'; target.write_text('first\nlast\n')
    a=proposal(store,target,diff=make_diff('first\nlast\n','FIRST\nlast\n'))
    b=proposal(store,target,diff=make_diff('first\nlast\n','first\nLAST\n'))
    store.update('learnings','id',b['learning_id'],{'rule_text':'A distinct lesson'})
    store.commit()
    preview=preview_selection(store,cfg,[a['id'],b['id']])
    body=decision(store,cfg,'approve',[a,b]); body['preview_revision']=preview['revision']
    submit_command(store,cfg,body)
    newer=proposal(store,target,lesson=a['learning_id'])
    submit_command(store,cfg,decision(store,cfg,'reject_target',[newer]))
    result=run_once(store,cfg)
    assert result['state']=='cancelled'
    assert target.read_text()=='first\nlast\n'
    assert waiting(store,cfg)=={b['id']}


def test_scoped_rejection_api_uses_the_selected_database_and_survives_reload(store,cfg,tmp_path):
    from fastapi.testclient import TestClient
    from self_improve.dashboard.app import create_app
    a=proposal(store,tmp_path/'a.md')
    copy=tmp_path/'copy.db'
    with closing(Store(copy)) as copied: store.conn.backup(copied.conn)
    with TestClient(create_app(cfg,db_path=copy)) as client:
        revision=client.get(f"/api/proposals/{a['id']}/review").json()['revision']
        body={'action':'reject_target','request_key':'copy-rejection-request','members':[{'proposal_id':a['id'],'revision':revision}]}
        response=client.post('/api/commands',json=body)
        assert response.status_code==202,response.text
        result=response.json()
        assert client.get('/api/review-queue').json()['families']==[]
    with TestClient(create_app(cfg,db_path=copy)) as client:
        assert client.get(f"/api/commands/{result['id']}").json()==result
        assert client.post('/api/commands',json=body).json()==result
    assert store.query('SELECT * FROM commands')==[]
    assert waiting(store,cfg)=={a['id']}


@pytest.mark.parametrize('amend',[False,True])
def test_pipeline_respects_target_rejection_before_generation_and_eval(tmp_path,amend):
    from pathlib import Path
    from tests.e2e_corpus import build_corpus, ScriptedLLM, mine_payload
    from self_improve.pipeline import run_pipeline
    corpus=build_corpus(tmp_path)
    with closing(corpus.store) as s:
        target=Path(corpus.cfg.global_claude_md)
        a=proposal(s,target)
        s.update('learnings','id',a['learning_id'],{'scope':'global'})
        if amend:
            s.update('proposals','id',a['id'],{'status':'applied'})
            a=proposal(s,target,lesson=a['learning_id'])
        s.commit()
        submit_command(s,corpus.cfg,decision(s,corpus.cfg,'reject_target',[a]))
        llm=ScriptedLLM(mine_responses=[mine_payload('No new lesson',is_real_learning=False) for _ in range(30)])
        stats=run_pipeline(corpus.cfg,s,review_only=True,_llm_factory=llm.factory())
        assert stats['apply']['taxonomy'].get('propose_TargetRejected')==1,stats['apply']
        assert stats['gate']['attempted']==0
        assert len(s.query('SELECT * FROM proposals'))==(2 if amend else 1)


def test_near_duplicate_suppression_is_checked_after_routing_without_entering_global_pool(store,cfg,tmp_path):
    from self_improve.rejections import proposal_rejection
    a=proposal(store,tmp_path/'a.md')
    submit_command(store,cfg,decision(store,cfg,'reject_target',[a]))
    learning={'id':'new-lesson','rule_text':'A restated version of the rejected rule'}
    class Embedding:
        def cached_vector(self,*_): return [1.0,0.0]
        def encode(self,texts): return [[1.0,0.0] for _ in texts]
    assert proposal_rejection(store,cfg,learning,tmp_path/'a.md','global_claude_md',Embedding())['code']=='TargetRejected'
    assert proposal_rejection(store,cfg,learning,tmp_path/'b.md','global_claude_md',Embedding()) is None


def test_lesson_rejection_retains_its_reviewed_text_even_after_later_mutable_rows_change(store,cfg,tmp_path):
    from self_improve.cluster import rejected_vectors
    a=proposal(store,tmp_path/'a.md',status='applied')
    submit_command(store,cfg,decision(store,cfg,'reject_lesson',[a]))
    # An earlier in-flight producer can retain an obsolete learning dictionary.
    store.update('learnings','id',a['learning_id'],{'status':'candidate','rule_text':'Later rewritten text'})
    store.commit()
    class Embedding:
        def cached_vector(self,*_): return [1.0,0.0]
    assert {text for text,_ in rejected_vectors(store,Embedding())}=={'test rule','Later rewritten text'}
    b=proposal(store,tmp_path/'b.md',lesson=a['learning_id'])
    assert b['id'] not in waiting(store,cfg)


def test_concurrent_rejection_replays_record_one_atomic_decision(store,cfg,tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    a=proposal(store,tmp_path/'a.md'); b=proposal(store,tmp_path/'b.md',lesson=a['learning_id'])
    body=decision(store,cfg,'reject_target',[a,b])
    def submit(_):
        with closing(Store(store.db_path,migrate=False)) as writer:
            return submit_command(writer,cfg,body)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(submit,range(2)))
    assert results[0]==results[1]
    assert len(store.query('SELECT * FROM commands'))==1
    assert len(store.query("SELECT * FROM proposal_events WHERE event='rejected_user'"))==2


def test_scope_upgrade_is_explicit_and_legacy_reads_remain_available(cfg,tmp_path,monkeypatch):
    from self_improve import store as module
    monkeypatch.setattr(module,'MIGRATIONS',[m for m in module.MIGRATIONS if m[0]!='0015_rejection_scopes'])
    with closing(Store(tmp_path/'legacy.db')) as legacy:
        a=proposal(legacy,tmp_path/'a.md')
        body=decision(legacy,cfg,'reject_target',[a])
        with pytest.raises(CommandError) as error: submit_command(legacy,cfg,body)
        assert error.value.code=='UpgradeRequired'
        assert legacy.query('SELECT * FROM commands')==[]
        legacy.update('proposals','id',a['id'],{'status':'rejected_user'})
        legacy.commit()
        b=proposal(legacy,tmp_path/'b.md',lesson=a['learning_id'])
        assert b['id'] not in waiting(legacy,cfg)


def test_unknown_scope_is_reported_instead_of_guessing_a_suppression(store,cfg,tmp_path):
    from self_improve.rejections import context
    a=proposal(store,tmp_path/'a.md',status='rejected_user')
    store.update('proposals','id',a['id'],{'decision_scope':'unknown'})
    store.commit()
    with pytest.raises(CommandError) as error: context(store)
    assert error.value.code=='RejectionDataError'
