"""Human judgments and class consent through real, invented delivery state."""
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import json

import pytest

from self_improve import quality
from self_improve.commands import CommandError, submit_command, command_status
from self_improve.execution_policy import policy_snapshot, set_class_policy, automatic_permission
from self_improve.store import Store, new_id
from self_improve.worker import run_once
from tests.test_delivery_worker import env as delivery_env, approve
from tests.test_apply import insert_proposal, make_diff


@pytest.fixture
def env(delivery_env, tmp_path):
    cfg, store, target = delivery_env
    cfg = replace(cfg, claude_managed_dir=str(tmp_path/'managed'), codex_global_agents_md=str(tmp_path/'codex/AGENTS.md'),
                  skills_dir=str(tmp_path/'skills'), codex_skills_dir=str(tmp_path/'codex-skills'),
                  claude_projects_dir=str(tmp_path/'claude'), claude_history_path=str(tmp_path/'claude-history.jsonl'),
                  codex_sessions_dir=str(tmp_path/'codex'), codex_archived_dir=str(tmp_path/'archive'))
    return cfg, store, target


def delivered(env, index=0, target_kind='global_claude_md', action='add'):
    cfg, store, target = env
    path = target.parent/f'invented-{index}.md'
    path.write_text('Human introduction.\n')
    after = '' if action == 'delete_human_line' else path.read_text()+f'- Verify the invented output {index}.\n'
    p = insert_proposal(store,target=path,diff=make_diff(path.read_text(),after),
                        status='pending',target_kind=target_kind,action=action)
    command = approve(store,cfg,p)
    assert run_once(store,cfg)['state'] == 'completed'
    return p, path, command


def sample_request(env, **selection):
    cfg, store, _ = env
    selection = {'target_class':'global','size':20,'seed':'invented-seed',**selection}
    shown = quality.preview(store,cfg,**selection)
    return {'action':'create_quality_sample','request_key':new_id(),**selection,'preview_revision':shown['revision']}


def create_sample(env, **selection):
    result = submit_command(env[1],env[0],sample_request(env,**selection))
    return quality.sample(env[1], result['result']['sample_id'])


def judgment_request(sample, value='useful', revision=0):
    return {'action':'judge_quality','request_key':new_id(),'sample_id':sample['id'],
            'subject_id':sample['subjects'][0]['source']['id'],'judgment':value,
            'note':'Invented human assessment.','expected_revision':revision}


def test_sample_freezes_actual_revisions_population_and_deterministic_order(env):
    cfg, store, _ = env
    proposals = [delivered(env,i)[0] for i in range(3)]
    first = quality.preview(store,cfg,target_class='global',size=2,seed='same-seed')
    assert first == quality.preview(store,cfg,target_class='global',size=2,seed='same-seed')
    created = create_sample(env,size=2,seed='same-seed')
    assert created['preview'] == first and created['counts']['eligible'] == 3
    assert len(created['subjects']) == 2 and created['counts']['reviewed'] == 0
    for p in proposals:
        store.update('learnings','id',p['learning_id'],{'rule_text':'New unrelated current text'})
    store.commit()
    delivered(env,3)
    assert quality.sample(store,created['id']) == created
    assert quality.preview(store,cfg,target_class='global')['eligible'] == 4
    assert all('New unrelated' not in r['source']['applied'] for r in created['subjects'])


def test_repeated_and_revised_judgments_keep_one_denominator_across_samples(env):
    cfg, store, _ = env
    delivered(env)
    first = create_sample(env)
    body = judgment_request(first,'uncertain')
    recorded = submit_command(store,cfg,body)
    assert submit_command(store,cfg,body) == recorded
    assert len(store.query('SELECT * FROM quality_judgments')) == 1
    assert quality.sample(store,first['id'])['counts']['precision_denominator'] == 0
    second = create_sample(env,seed='another-seed')
    assert second['subjects'][0]['judgment']['judgment'] == 'uncertain'
    submit_command(store,cfg,judgment_request(second,'useful',1))
    shown = quality.sample(store,first['id'])
    assert shown['counts']['reviewed'] == shown['counts']['precision_numerator'] == shown['counts']['precision_denominator'] == 1
    assert shown['counts']['precision'] is None and len(shown['subjects'][0]['history']) == 2
    with pytest.raises(CommandError,match='Another judgment'):
        submit_command(store,cfg,judgment_request(first,'not_useful',1))
    with closing(Store(store.db_path,read_only=True)) as reader:
        assert quality.sample(reader,first['id']) == shown
    assert store.query('SELECT * FROM llm_calls') == []


def test_stale_sample_and_changed_retry_are_atomic(env):
    cfg, store, _ = env
    delivered(env)
    body = sample_request(env)
    delivered(env,1)
    with pytest.raises(CommandError,match='eligible population changed'):
        submit_command(store,cfg,body)
    assert store.query('SELECT * FROM quality_samples') == []
    body = sample_request(env)
    created = submit_command(store,cfg,body)
    assert submit_command(store,cfg,body) == created
    with pytest.raises(CommandError,match='another command'):
        submit_command(store,cfg,{**body,'seed':'changed-seed'})


def test_approvals_and_unfrozen_history_are_not_quality_labels(env):
    cfg, store, target = env
    p = insert_proposal(store,target=target,diff=make_diff('before\n','after\n'),status='approved_user')
    store.insert('proposal_events',{'id':'invented-legacy','proposal_id':p['id'],'event':'applied',
                                   'ts':'2026-01-01T00:00:00Z','actor':'pipeline','note':'{}'})
    store.commit()
    shown = quality.preview(store,cfg,target_class='global')
    assert shown['eligible'] == 0 and shown['application_events'] == 1
    assert shown['excluded_applications'][0]['cause'] == 'UnfrozenHistoricalApplication'
    with pytest.raises(CommandError,match='No verified applied'):
        submit_command(store,cfg,sample_request(env))


def test_hooks_and_human_deletions_have_inspectable_delivered_quality_sources(env):
    for i,kind in enumerate(('hook','project_agents_md')):
        p,path,_ = delivered(env,i,target_kind=kind,action='convert_to_hook' if kind=='hook' else 'delete_human_line')
        sample = create_sample(env,target_class='hook' if kind=='hook' else 'project')
        assert sample['subjects'][0]['source']['applied'] == path.read_text()
        assert sample['subjects'][0]['links'][0]['proposal_id'] == p['id']


def test_policy_command_persists_consent_but_cannot_authorize_backlog(env):
    cfg, store, target = env
    old = insert_proposal(store,target=target,diff=make_diff('before\n','after\n'))
    body = {'action':'set_class_policy','request_key':new_id(),'target_class':'global','enabled':True,'expected_revision':0}
    result = submit_command(store,cfg,body)
    assert result['state'] == 'completed' and result['max_model_calls'] == 0
    assert submit_command(store,cfg,body) == result
    assert not automatic_permission(store,cfg,old)['allowed']
    new = insert_proposal(store,target=target,diff=make_diff('before\n','after\n'))
    assert automatic_permission(store,cfg,new)['allowed']
    assert target.read_text() == 'before\n' and run_once(store,cfg) is None
    with closing(Store(store.db_path,read_only=True)) as reader:
        assert policy_snapshot(reader)['classes']['global']['enabled']
        assert command_status(reader,result['id']) == result
    assert len(store.query('SELECT * FROM execution_policy_events')) == 1
    with pytest.raises(CommandError,match='changed in another request'):
        submit_command(store,cfg,{**body,'request_key':new_id(),'enabled':False})
    with pytest.raises(CommandError,match='hooks always require'):
        submit_command(store,cfg,{**body,'request_key':new_id(),'target_class':'hook'})


def test_command_record_failure_rolls_back_policy_and_quality(env,monkeypatch):
    cfg, store, _ = env
    delivered(env)
    original = store.insert
    def fail(table,row):
        if table == 'evidence_command_results':raise RuntimeError('invented final-record failure')
        return original(table,row)
    monkeypatch.setattr(store,'insert',fail)
    for body in (sample_request(env),{'action':'set_class_policy','request_key':new_id(),
                                    'target_class':'global','enabled':True,'expected_revision':0}):
        with pytest.raises(RuntimeError,match='invented final-record'):
            submit_command(store,cfg,body)
    assert store.query('SELECT * FROM quality_samples') == store.query('SELECT * FROM quality_subjects') == []
    assert not policy_snapshot(store)['classes']['global']['enabled']
    assert store.query('SELECT * FROM execution_policy_events') == []


def test_saved_sample_survives_snapshot_loss_and_rejects_corruption(env,monkeypatch):
    cfg,store,_ = env
    delivered(env)
    saved = create_sample(env)
    from self_improve import rollback
    monkeypatch.setattr(rollback,'read_snapshot',lambda *_:None)
    assert quality.sample(store,saved['id']) == saved
    # The frozen subject remains eligible; missing original snapshots remain a gap.
    shown = quality.preview(store,cfg,target_class='global')
    assert shown['eligible'] == 1 and len(shown['excluded_applications']) == 1
    store.conn.execute("UPDATE quality_samples SET record_hash='changed' WHERE id=?",(saved['id'],));store.commit()
    with pytest.raises(CommandError,match='changed identity'):
        quality.sample(store,saved['id'])


def test_class_evidence_counts_exact_rollback_without_inventing_quality(env):
    from self_improve import apply
    from self_improve.dashboard.quality_data import classes
    cfg,store,_ = env
    proposal,_,_ = delivered(env)
    before = classes(store,cfg)['classes'][0]
    assert before['applied_revisions'] == 1 and before['rolled_back_revisions'] == 0
    assert before['quality']['reviewed'] == 0 and before['quality']['precision'] is None
    assert before['availability']['not_observed'] == 1
    assert apply.rollback(store,cfg,proposal['id'])['outcome'] == 'rolled_back'
    after = classes(store,cfg)
    assert after['classes'][0]['rolled_back_revisions'] == 1
    assert after['coverage']['unlinked_rollback_events'] == []
    assert after['classes'][0]['quality']['reviewed'] == 0


def test_sample_corrupt_shape_fails_with_record_identity(env):
    from self_improve.commands import _json, _hash
    delivered(env)
    saved=create_sample(env);row=env[1].query_one('SELECT * FROM quality_samples WHERE id=?',(saved['id'],))
    record=json.loads(row['record_json']);record['preview']=[]
    env[1].update('quality_samples','id',saved['id'],{'record_json':_json(record),'record_hash':_hash(record)});env[1].commit()
    with pytest.raises(CommandError,match=saved['id']):quality.sample(env[1],saved['id'])


def test_equivalent_delivered_members_share_one_quality_subject(env):
    cfg,store,target=env
    diff=make_diff('before\n','before\nshared contribution\n')
    a=insert_proposal(store,target=target,diff=diff,status='pending')
    b=insert_proposal(store,target=target,diff=diff,status='pending')
    approve(store,cfg,a,b);assert run_once(store,cfg)['state']=='completed'
    shown=quality.preview(store,cfg,target_class='global')
    assert shown['application_events']==2 and shown['eligible']==1
    assert len(shown['selected'][0]['links'])==2
    assert create_sample(env)['counts']['selected']==1


def test_quality_api_uses_selected_copy_and_reads_are_read_only(env,tmp_path):
    import sqlite3
    pytest.importorskip('fastapi',reason='the dashboard extra is not installed')
    from fastapi.testclient import TestClient
    from self_improve.dashboard.app import create_app
    cfg,store,target=env;delivered(env);saved=create_sample(env)
    copy=tmp_path/'copy.db'
    with sqlite3.connect(copy) as dest:store.conn.backup(dest)
    original_counts={t:len(store.query('SELECT * FROM '+t)) for t in quality.TABLES+('commands','execution_policy_events')}
    before=copy.read_bytes()
    with TestClient(create_app(cfg,db_path=copy)) as client:
        for url in ('/api/class-evidence','/api/quality-preview?target_class=global','/api/quality-samples','/api/quality-samples/'+saved['id']):
            response=client.get(url);assert response.status_code==200,response.text
        assert copy.read_bytes()==before
        response=client.post('/api/commands',json=judgment_request(saved,'not_useful'))
        assert response.status_code==202,response.text
        cid=response.json()['id'];assert client.get('/api/commands/'+cid).json()['result']['judgment']=='not_useful'
        assert client.get('/api/quality-samples/'+saved['id']).json()['counts']['not_useful']==1
        body={'action':'set_class_policy','request_key':new_id(),'target_class':'skill','enabled':True,'expected_revision':0}
        assert client.post('/api/commands',json=body).status_code==202
        assert client.post('/api/commands',json={**body,'request_key':new_id(),'enabled':'true'}).status_code==400
    for t,n in original_counts.items():assert len(store.query('SELECT * FROM '+t))==n
    assert not policy_snapshot(store)['classes']['skill']['enabled']
    assert target.read_text()=='before\n' and store.query('SELECT * FROM llm_calls')==[]


def test_judgment_failure_and_policy_transactions_do_not_commit_caller_work(env,monkeypatch):
    cfg,store,_=env;delivered(env);saved=create_sample(env)
    original=store.insert
    def fail(table,row):
        if table=='evidence_command_results':raise RuntimeError('invented publication failure')
        return original(table,row)
    monkeypatch.setattr(store,'insert',fail)
    with pytest.raises(RuntimeError,match='publication failure'):submit_command(store,cfg,judgment_request(saved))
    assert quality.judgments(store,saved['subjects'][0]['source']['id'])==[]
    with pytest.raises(RuntimeError,match='caller abort'):
        with store.transaction(write=True):
            set_class_policy(store,'project',True,commit=False,expected_revision=0)
            raise RuntimeError('caller abort')
    assert not policy_snapshot(store)['classes']['project']['enabled']
    store.conn.execute("INSERT INTO learnings(id,rule_text,created_at) VALUES('pending-quality-work','invented','2026-01-01T00:00:00Z')")
    with pytest.raises(RuntimeError,match='pending'):set_class_policy(store,'project',True)
    assert store.conn.in_transaction
    store.conn.rollback()


def test_sample_pages_and_manual_precision_keep_explicit_denominators(env):
    delivered(env)
    saved=[create_sample(env,seed='seed-'+str(i)) for i in range(3)]
    first=quality.samples(env[1],limit=2)
    second=quality.samples(env[1],limit=2,cursor=first['next_cursor'])
    assert first['count']==second['count']==3 and len(first['records'])==2 and len(second['records'])==1
    assert {r['id'] for r in first['records']+second['records']}=={r['id'] for r in saved}
    with pytest.raises(CommandError,match='cursor'):quality.samples(env[1],target_class='skill',cursor=first['next_cursor'])
    rows=[{'judgment':{'judgment':'useful'}} for _ in range(19)]+[{'judgment':{'judgment':'uncertain'}}]
    assert quality._counts(rows,20)['precision'] is None
    rows.append({'judgment':{'judgment':'not_useful'}})
    counts=quality._counts(rows,21)
    assert counts['precision']==19/20 and counts['uncertain']==1 and counts['reviewed']==21


def test_class_availability_comes_from_real_collector_after_checkout_integration(env,tmp_path):
    from self_improve import rule_availability
    from self_improve.dashboard.quality_data import classes
    from tests.test_rule_availability import known_copy
    from tests.test_apply import init_git_repo, run_git
    cfg,store,_=env
    repo=init_git_repo(tmp_path/'project','AGENTS.md','Human introduction.\n');known_copy(store,repo)
    target=repo/'AGENTS.md'
    p=insert_proposal(store,target=target,diff='',status='pending',target_kind='project_agents_md')
    text='Human introduction.\n- Verify output. <!-- si:'+p['learning_id']+' -->\n'
    store.update('proposals','id',p['id'],{'diff_unified':make_diff(target.read_text(),text)});store.commit()
    p=store.query_one('SELECT * FROM proposals WHERE id=?',(p['id'],))
    approve(store,cfg,p);assert run_once(store,cfg)['state']=='completed'
    rule_availability.collect_availability(store,cfg,observed_at='2030-01-01T00:00:00Z')
    assert classes(store,cfg)['classes'][1]['availability']['not_available']==1
    run_git(['merge','--ff-only',cfg.project_branch_name],repo)
    rule_availability.collect_availability(store,cfg,observed_at='2030-01-02T00:00:00Z')
    row=classes(store,cfg)['classes'][1]
    assert row['availability']['available']==1 and row['quality']['reviewed']==0
    assert row['latest_observation_at']=='2030-01-02T00:00:00.000000Z'


def test_typed_class_consent_reaches_final_automatic_writer(env):
    from self_improve import apply
    cfg,store,target=env
    old=insert_proposal(store,target=target,diff=make_diff('before\n','before\nold backlog\n'))
    submit_command(store,cfg,{'action':'set_class_policy','target_class':'global','enabled':True,'expected_revision':0,'request_key':new_id()})
    assert apply.apply_proposal(store,cfg,old)['outcome']=='held'
    assert target.read_text()=='before\n'
    new=insert_proposal(store,target=target,diff=make_diff('before\n','before\nnew eligible change\n'))
    assert apply.apply_proposal(store,cfg,new)['outcome']=='applied'
    assert target.read_text()=='before\nnew eligible change\n'
    assert store.query('SELECT * FROM llm_calls')==[]


def test_missing_quality_schema_is_unknown_and_damaged_schema_is_loud(env):
    from self_improve.dashboard.quality_data import classes
    _,store,_=env
    store.conn.execute('DELETE FROM schema_migrations WHERE name=?',(quality.MIGRATION,));store.commit()
    assert all(row['quality'] is None for row in classes(store,env[0])['classes'])
    with pytest.raises(CommandError,match='Upgrade'):quality.samples(store)
    store.conn.execute('INSERT INTO schema_migrations(name,applied_at) VALUES(?,?)',(quality.MIGRATION,'2026-01-01T00:00:00Z'))
    store.conn.execute('DROP TABLE quality_judgments');store.commit()
    with pytest.raises(CommandError,match='quality_judgments'):classes(store,env[0])
