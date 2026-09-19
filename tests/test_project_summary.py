"""Project composition from temporary real delivery/collection records."""
from pathlib import Path
from types import SimpleNamespace
import json

import pytest

from self_improve.store import Store, new_id
from self_improve import rule_availability, rule_revisions
from tests.test_rule_availability import cfg as config_fixture, known_copy, delivery, init_git_repo, run_git
from tests.test_apply import insert_proposal


def build_project(root):
    cfg=config_fixture.__wrapped__(root)
    store=Store(cfg.state_path('state.db'))
    from self_improve.execution_policy import set_class_policy
    for target_class in ('project','global','skill'):
        set_class_policy(store,target_class,True,now='2020-01-01T00:00:00Z')
    a=init_git_repo(root/'alpha','AGENTS.md','# Invented human instructions\n')
    b=init_git_repo(root/'beta','AGENTS.md','# Invented human instructions\n')
    for repo in (a,b):run_git(['remote','add','origin','https://example.test/fixture/project-summary.git'],repo)
    key=known_copy(store,a);assert known_copy(store,b)==key
    proposal,text=delivery(store,cfg,a)
    run_git(['merge','--ff-only',cfg.project_branch_name],a)
    store.conn.execute("UPDATE proposal_events SET ts='2030-01-01T00:00:00Z' WHERE event='applied'");store.commit()
    (a/'CLAUDE.md').symlink_to(a/'AGENTS.md')
    (a/'docs').mkdir();(a/'docs/guide.md').write_text('Invented on-demand reference\n')
    rule_availability.collect_availability(store,cfg,observed_at='2030-01-02T00:00:00Z')
    revision=rule_revisions.retained_revisions(store)[0]
    from self_improve.scan_observations import working_copy_identity
    copies={p.name:working_copy_identity(key,str(p))['id'] for p in (a,b)}
    return SimpleNamespace(cfg=cfg,store=store,a=a,b=b,key=key,copies=copies,proposal=proposal,text=text,revision=revision)


@pytest.fixture
def env(tmp_path):
    value=build_project(tmp_path)
    try:yield value
    finally:value.store.close()


def read(env,**kw):
    from self_improve.dashboard.project_summary import summary
    with env.store.transaction():
        return summary(env.store,env.cfg,project_key=env.key,working_copy_id=env.copies['alpha'],**kw)


def test_exact_copy_delivery_and_proposal_are_independent(env):
    from self_improve.dashboard.project_summary import summary
    pending=insert_proposal(env.store,target=env.a/'AGENTS.md',diff='',status='pending')
    env.store.commit()
    got=read(env)
    assert got['counts']['project']['matched']==1
    assert got['counts']['project']['retained']==1
    assert got['deliveries']['records'][0]['revision']['content']==env.text
    assert got['deliveries']['records'][0]['lifetime']['state']=='no_recorded_end'
    assert got['proposals']['count']==1 and got['proposals']['records'][0]['id']==pending['id']
    assert not got['runtime_loading_verified']
    other=summary(env.store,env.cfg,project_key=env.key,working_copy_id=env.copies['beta'])
    assert other['counts']['project']['matched']==0
    assert other['counts']['project']['observations']['absent']==1
    assert other['working_copy_id']==env.copies['beta']


def test_no_copy_or_missing_copy_never_falls_back(env):
    from self_improve.dashboard.project_summary import summary
    for copy in (None,'f'*64):
        got=summary(env.store,env.cfg,project_key=env.key,working_copy_id=copy)
        assert got['working_copy_id']==copy and got['counts']['project']['matched'] is None
        assert got['counts']['project']['known_matches']==0
        assert got['deliveries']['records'][0]['observation'] is None
    with pytest.raises(ValueError,match='copy'):
        summary(env.store,env.cfg,project_key=env.key,working_copy_id='invalid')


def test_retained_content_and_reads_do_not_open_target_or_mutate(env,monkeypatch):
    initial=read(env)
    env.store.update('learnings','id',env.proposal['learning_id'],{'title':'Changed current title','rule_text':'Changed current rule'})
    env.store.update('proposals','id',env.proposal['id'],{'status':'rolled_back'})
    env.store.commit()
    before=list(env.store.conn.iterdump())
    def forbidden(*a,**kw):raise AssertionError('Retained summary opened live state')
    monkeypatch.setattr('self_improve.destinations.resolve_destination',forbidden)
    monkeypatch.setattr(Path,'read_text',forbidden)
    got=read(env)
    assert got['deliveries']==initial['deliveries']
    assert got['counts']['project']['matched']==1
    assert list(env.store.conn.iterdump())==before


def test_later_exact_inverse_does_not_revive_stale_available_text(env):
    event={'id':new_id(),'proposal_id':env.proposal['id'],'event':'rolled_back','actor':'user','ts':'2030-01-03T00:00:00Z',
           'note':json.dumps({'applied_event_id':env.revision['application_event_id'],'application_id':env.revision['application_id']})}
    env.store.insert('proposal_events',event);env.store.commit()
    got=read(env);row=got['deliveries']['records'][0]
    assert row['observation']['status']=='available'
    assert row['lifetime']['state']=='rolled_back'
    assert row['lifetime']['ending']['event_id']==event['id']
    assert got['counts']['project']['known_matches']==0
    assert got['counts']['project']['lifetimes']['rolled_back']==1


def test_new_application_after_last_check_supersedes_old_revision(env):
    event=env.store.query_one('SELECT * FROM proposal_events WHERE id=?',(env.revision['application_event_id'],))
    env.store.insert('proposal_events',{**event,'id':new_id(),'ts':'2030-01-03T00:00:00Z'});env.store.commit()
    got=read(env)
    assert got['deliveries']['records'][0]['lifetime']['state']=='superseded'
    assert got['counts']['project']['known_matches']==0


@pytest.mark.parametrize('damage',['origin','unbound_rollback'])
def test_uncertain_lifetime_keeps_matched_subtotal_separate(env,damage):
    if damage=='origin':env.store.conn.execute('DELETE FROM proposal_events WHERE id=?',(env.revision['application_event_id'],))
    else:env.store.insert('proposal_events',{'id':new_id(),'proposal_id':env.proposal['id'],'event':'rolled_back','actor':'user','ts':'2030-01-03T00:00:00Z','note':'{}'})
    env.store.commit();got=read(env)
    assert got['counts']['project']['matched'] is None
    assert got['counts']['project']['known_matches']==0
    assert got['counts']['project']['lifetimes']['unknown']==1


def test_global_provider_scope_stays_separate(env):
    target=Path(env.cfg.global_claude_md);target.parent.mkdir(parents=True,exist_ok=True);target.write_text('# Global invented instructions\n')
    p,text=delivery(env.store,env.cfg,env.a,filename=str(target),target_kind='global_claude_md')
    rule_availability.collect_availability(env.store,env.cfg,observed_at='2030-01-04T00:00:00Z')
    got=read(env)
    assert got['counts']['project']['retained']==1 and got['counts']['global']['retained']==1
    global_row=next(r for r in got['deliveries']['records'] if r['origin']=='global')
    assert global_row['revision']['content']==text
    assert {p['provider'] for o in global_row['observation']['observations'] for m in o['matches'] for p in m['loading_paths']}=={'claude'}


def test_preview_limit_does_not_cap_counts_and_availability_reads_all_pages(env,monkeypatch):
    original=rule_availability.project_availability;calls=[]
    def small(store,**kw):
        calls.append(kw.get('cursor'));return original(store,**(kw|{'limit':1}))
    monkeypatch.setattr(rule_availability,'project_availability',small)
    for i in range(4):insert_proposal(env.store,target=env.a/'AGENTS.md',diff='',status='pending')
    env.store.commit();got=read(env,limit=1)
    assert got['counts']['project']['matched']==1 and len(calls)==2
    assert got['proposals']['count']==4 and len(got['proposals']['records'])==1
    assert got['proposals']['omitted']==3


def test_unknown_schema_differs_from_available_empty_population(env):
    env.store.conn.execute("DELETE FROM schema_migrations WHERE name='0024_rule_availability'");env.store.commit()
    got=read(env)
    assert got['counts']['project']['retained'] is None
    assert got['counts']['project']['matched'] is None
    assert got['deliveries']['reason']=='schema_unavailable'


def test_corrupt_revision_is_loud(env):
    env.store.conn.execute("UPDATE rule_revisions SET record_json='{}'");env.store.commit()
    with pytest.raises(rule_revisions.AvailabilityError,match='invalid stored record'):read(env)


def test_empty_retained_population_is_zero_not_missing_schema(env):
    from self_improve.dashboard.project_summary import summary
    key,copy=empty_project(env)
    got=summary(env.store,env.cfg,project_key=key,working_copy_id=copy)
    assert got['counts']['project']['retained']==0
    assert got['counts']['project']['matched']==0
    assert got['deliveries']['reason']=='' and got['deliveries']['records']==[]


def empty_project(env):
    repo=init_git_repo(env.a.parent/'empty','AGENTS.md','# Empty delivery population\n')
    key=known_copy(env.store,repo)
    rule_availability.collect_availability(env.store,env.cfg,observed_at='2030-01-04T00:00:00Z')
    from self_improve.scan_observations import working_copy_identity
    return key,working_copy_identity(key,str(repo))['id']


@pytest.mark.parametrize('population',['empty','ended'])
def test_missing_or_foreign_copy_stays_unknown_without_open_revisions(env,population):
    from self_improve.dashboard.project_summary import summary
    if population=='empty':key,known=empty_project(env)
    else:
        key,known=env.key,env.copies['alpha']
        env.store.insert('proposal_events',{'id':new_id(),'proposal_id':env.proposal['id'],'event':'rolled_back','actor':'user','ts':'2030-01-03T00:00:00Z',
            'note':json.dumps({'applied_event_id':env.revision['application_event_id'],'application_id':env.revision['application_id']})})
    env.store.commit()
    foreign=init_git_repo(env.a.parent/'foreign','AGENTS.md','# Unrelated fixture\n')
    foreign_key=known_copy(env.store,foreign)
    from self_improve.scan_observations import working_copy_identity
    foreign_id=working_copy_identity(foreign_key,str(foreign))['id']
    for copy in ('f'*64,foreign_id):
        got=summary(env.store,env.cfg,project_key=key,working_copy_id=copy)
        assert got['counts']['project']['matched'] is None
    assert summary(env.store,env.cfg,project_key=key,working_copy_id=known)['counts']['project']['matched']==0


def test_simultaneous_conflicting_checks_keep_all_evidence_and_unknown_total(env):
    target=env.a/'AGENTS.md';target.write_text(target.read_text().replace('readable.','changed.'))
    rule_availability.collect_availability(env.store,env.cfg,observed_at='2030-01-02T00:00:00Z')
    got=read(env);check=got['deliveries']['records'][0]['observation']
    assert check['conflicting_observations'] and check['status']=='unknown'
    assert len(check['observations'])==2
    assert got['counts']['project']['matched'] is None
    assert got['counts']['project']['uncertain']==1


def test_equivalent_timestamp_and_simultaneous_application_identities(env):
    event=env.store.query_one('SELECT * FROM proposal_events WHERE id=?',(env.revision['application_event_id'],))
    env.store.update('proposal_events','id',event['id'],{'ts':'2029-12-31T16:00:00-08:00'});env.store.commit()
    assert read(env)['counts']['project']['matched']==1
    env.store.insert('proposal_events',{**event,'id':new_id(),'ts':'2030-01-01T01:00:00+01:00'});env.store.commit()
    got=read(env)
    assert got['deliveries']['records'][0]['lifetime']['cause']=='simultaneous_applications'
    assert got['counts']['project']['matched'] is None


def test_api_uses_selected_store_without_writes(env,tmp_path):
    pytest.importorskip('fastapi')
    from self_improve.dashboard.app import create_app
    from fastapi.testclient import TestClient
    selected=tmp_path/'selected.db';copy=Store(selected)
    env.store.conn.backup(copy.conn);copy.close()
    env.store.conn.execute('DELETE FROM proposal_events WHERE id=?',(env.revision['application_event_id'],));env.store.commit()
    assert read(env)['counts']['project']['matched'] is None
    before=selected.read_bytes()
    with TestClient(create_app(env.cfg,db_path=selected)) as client:
        result=client.get('/api/project-summary',params={'project_key':env.key,'working_copy_id':env.copies['alpha']})
        assert result.status_code==200,result.text
        assert result.json()['counts']['project']['matched']==1
        assert client.get('/api/project-summary',params={'project_key':'missing'}).status_code==404
    assert selected.read_bytes()==before
