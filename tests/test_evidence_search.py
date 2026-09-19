"""Complete retained evidence, with invented state and no private resource reads."""
import json

import pytest

from self_improve.execution_policy import proposal_dispositions
from tests.test_rejections import store, proposal, decision, init_git_repo, run_git
from tests.test_rule_availability import cfg
from self_improve.commands import submit_command


def test_retained_disposition_never_resolves_a_target_and_preserves_default_policy(cfg,store,tmp_path,monkeypatch):
    from self_improve import rejections, destinations
    clones=[init_git_repo(tmp_path/name,'AGENTS.md','# Fixture\n') for name in ('copy-a','copy-b')]
    for clone in clones:run_git(['remote','add','origin','https://example.test/owner/fixture.git'],clone)
    first=proposal(store,clones[0]/'AGENTS.md')
    submit_command(store,cfg,decision(store,cfg,'reject_target',[first]))
    same=proposal(store,clones[1]/'AGENTS.md',lesson=first['learning_id'])
    different=proposal(store,clones[1]/'CLAUDE.md',lesson=first['learning_id'])
    unrelated=proposal(store,tmp_path/'other.md')
    store.update('learnings','id',unrelated['learning_id'],{'rule_text':'A distinct, unrelated fixture rule.'});store.commit()
    normal={p['id']:p for p in proposal_dispositions(store,cfg)}
    assert normal[same['id']]['next_step']=='suppressed'
    assert normal[different['id']]['next_step']=='review'
    def forbidden(*args,**kwargs):raise AssertionError('retained reader inspected the working copy')
    monkeypatch.setattr(rejections,'target_identity',forbidden)
    monkeypatch.setattr(destinations,'resolve_destination',forbidden)
    import builtins,io,os,sqlite3,subprocess
    from pathlib import Path
    with monkeypatch.context() as guard:
        for module,attribute in [(builtins,'open'),(io,'open'),(os,'open'),(Path,'open'),(sqlite3,'connect'),(subprocess,'Popen'),(os,'stat'),(os,'listdir'),(os,'scandir')]:
            guard.setattr(module,attribute,forbidden)
        retained={p['id']:p for p in proposal_dispositions(store,cfg,retained_only=True)}
    for p in (same,different):
        assert retained[p['id']]['next_step']=='unknown'
        assert retained[p['id']]['execution']['allowed'] is False
        assert retained[p['id']]['execution']['reason']=='target_identity_unobserved'
    assert retained[unrelated['id']]==normal[unrelated['id']]


def seed(store,tmp_path):
    from self_improve.store import utc_now_iso
    stamp='2030-01-01T00:00:00Z'
    for n in range(45):
        store.insert('learnings',{'id':f'l{n:02}','title':f'Fixture rule {n:02}',
            'rule_text':'Complete rule body '+str(n),'why':'Original retained reason',
            'created_at':stamp,'status':'rejected' if n==44 else 'candidate'})
    for n in range(45):
        store.insert('sessions',{'file_path':str(tmp_path/f'aged-out-{n:02}.jsonl'),'source':'codex',
            'session_id':f'native-session-{n:02}','project_path':str(tmp_path/('clone-a' if n%2 else 'clone-b')),
            'project_key':'remote:example.test/fixture/repo','first_ts':stamp})
    for n in range(25):
        window=[{'role':'user','text':('Earlier context.\n'*500)+'Needle at full archive end.'}] if n==5 else [{'role':'user','text':f'Correction occurrence {n}.'}]
        if n==6:window=[{'session_file':str(tmp_path/'aged-out-06.jsonl'),'text':'Repeated error original text','count_in_session':3,'ts':stamp}]
        store.insert('incidents',{'id':f'i{n:02}','session_file':str(tmp_path/f'aged-out-{n:02}.jsonl'),
            'session_id':f'native-session-{n:02}','project_path':str(tmp_path/('clone-a' if n%2 else 'clone-b')),
            'project_key':'remote:example.test/fixture/repo','signal_type':'repeated_error' if n==6 else 'correction',
            'matched_text':'a'*40 if n==6 else f'Correction {n}', 'window_json':json.dumps(window),
            'status':'mined','ts':stamp,'created_at':stamp})
        store.insert('incident_learnings',{'incident_id':f'i{n:02}','learning_id':'l00'})
    for iid,status in [('unmined','new'),('dismissed','dismissed')]:
        store.insert('incidents',{'id':iid,'session_file':str(tmp_path/'aged-out-44.jsonl'),
            'session_id':'native-session-44','signal_type':'correction','matched_text':'Unlinked diagnostic needle',
            'status':status,'window_json':json.dumps([{'role':'user','text':'Please preserve complete unlinked evidence.'}]),'created_at':stamp})
    store.insert('proposals',{'id':'p00','learning_id':'l00','target_path':str(tmp_path/'rule.md'),
        'target_kind':'global_claude_md','action':'add','status':'pending','created_at':stamp,
        'diff_unified':'--- a/rule.md\n+++ b/rule.md\n@@ -0,0 +1 @@\n+Complete proposal only token.\n'})
    store.commit()


def test_complete_sources_and_literal_search_beyond_first_pages(cfg,store,tmp_path):
    from self_improve.dashboard import evidence_data as e
    seed(store,tmp_path)
    first=e.search(store,cfg,query='',limit=20)
    ids=[];page=first
    while True:
        ids.extend((r['kind'],r['source_id']) for r in page['rows'])
        if page['pagination']['next_cursor'] is None:break
        page=e.search(store,cfg,query='',limit=20,cursor=page['pagination']['next_cursor'])
    assert len(ids)==len(set(ids))==45+1+27+45
    assert first['pagination']['count']==len(ids)
    assert ('learning','l44') in ids and ('incident','dismissed') in ids
    assert {r['source_id'] for r in e.search(store,cfg,query='unlinked diagnostic needle',kinds=['incident'])['rows']}=={'unmined','dismissed'}
    assert {r['source_id'] for r in e.search(store,cfg,query='needle archive end',kinds=['incident'])['rows']}=={'i05'}
    assert 'Needle at full archive end.' in e.search(store,cfg,query='Needle at full archive end.',kinds=['incident'])['rows'][0]['excerpt']
    exact=e.search(store,cfg,query='native-session-44',kinds=['session'])
    assert exact['pagination']['count']==1
    detail=e.detail(store,cfg,kind='session',source_id=exact['rows'][0]['source_id'])
    assert detail['source']['native_session_id']=='native-session-44'
    assert {i['id'] for i in detail['source']['incidents']}=={'unmined','dismissed'}
    assert e.search(store,cfg,query='Complete proposal only token',kinds=['proposal'])['pagination']['count']==1


def test_full_detail_and_all_learning_evidence_survive_deleted_transcripts(cfg,store,tmp_path):
    from self_improve.dashboard import evidence_data as e
    seed(store,tmp_path)
    full=e.detail(store,cfg,kind='incident',source_id='i05')
    assert len(full['source']['window'][0]['text'])>7000
    assert full['source']['window'][0]['text'].endswith('Needle at full archive end.')
    repeated=e.detail(store,cfg,kind='incident',source_id='i06')
    assert repeated['source']['window_kind']=='occurrence'
    assert repeated['source']['fingerprint']=='a'*40 and repeated['source']['matched_text']==''
    first=e.learning_evidence(store,learning_id='l00',limit=20)
    second=e.learning_evidence(store,learning_id='l00',limit=20,cursor=first['pagination']['next_cursor'])
    assert len(first['rows'])==20 and len(second['rows'])==5 and first['project_count']==1
    assert first['pagination']['count']==25 and second['pagination']['next_cursor'] is None
    assert len({r['source_id'] for r in first['rows']+second['rows']})==25


def test_unicode_literal_punctuation_cursors_and_bounded_summaries(cfg,store,tmp_path):
    from self_improve.dashboard import evidence_data as e
    seed(store,tmp_path)
    store.update('learnings','id','l44',{'rule_text':'Café STRASSE abc%_x '+'x'*5000+' tail-only-token'});store.commit()
    query='Cafe\u0301 straße abc%_x'
    result=e.search(store,cfg,query=query,kinds=['learning'])
    assert [r['source_id'] for r in result['rows']]==['l44']
    assert e.search(store,cfg,query='abc%_y')['pagination']['count']==0
    tail=e.search(store,cfg,query='tail-only-token')['rows'][0]
    assert len(tail['excerpt'])<=350 and tail['excerpt_cut']>4500 and 'tail-only-token' in tail['excerpt']
    first=e.search(store,cfg,query='',kinds=['learning'],limit=20)
    with pytest.raises(e.EvidenceError,match='selection'):
        e.search(store,cfg,query='',kinds=['incident'],cursor=first['pagination']['next_cursor'])
    store.update('learnings','id','l44',{'why':'New retained reason.'});store.commit()
    with pytest.raises(e.EvidenceError) as stale:e.search(store,cfg,query='',kinds=['learning'],cursor=first['pagination']['next_cursor'])
    assert stale.value.code=='EvidenceChanged' and stale.value.status==409
    for limit in (0,51,True):
        with pytest.raises(e.EvidenceError):e.search(store,cfg,query='',limit=limit)


def test_source_id_includes_provider_and_missing_ids_stay_separate(cfg,store,tmp_path):
    from self_improve.dashboard import evidence_data as e
    for n,source,native in [(0,'claude','shared-native'),(1,'codex','shared-native'),(2,'codex',''),(3,'codex','')]:
        store.insert('sessions',{'file_path':str(tmp_path/f'{n}.jsonl'),'source':source,'session_id':native})
    store.commit()
    rows=e.search(store,cfg,query='',kinds=['session'])['rows']
    assert len(rows)==4 and len({r['source_id'] for r in rows})==4
    assert e.search(store,cfg,query='shared-native',kinds=['session'])['pagination']['count']==2


def test_missing_and_empty_coverage_and_corruption_are_explicit(cfg,store,tmp_path):
    from self_improve.dashboard import evidence_data as e
    seed(store,tmp_path)
    result=e.search(store,cfg,query='not anywhere in fixture')
    assert result['pagination']['count']==0
    assert result['coverage']['instruction_text']['reason']=='no_inventory_observations'
    store.conn.execute("DELETE FROM schema_migrations WHERE name='0033_instruction_text'");store.commit()
    assert e.search(store,cfg,query='')['coverage']['instruction_text']['reason']=='schema_unavailable'
    store.update('incidents','id','i05',{'window_json':'"wrong shape"'});store.commit()
    with pytest.raises(e.EvidenceError,match='i05') as corrupt:e.search(store,cfg,query='')
    assert corrupt.value.status==500


def test_no_learning_review_and_reported_violation_have_typed_recovery_paths(cfg,store,tmp_path):
    from self_improve.dashboard import evidence_data as e
    seed(store,tmp_path)
    store.update('learnings','id','l00',{'violated_existing_rule':'The prior fixture rule was reportedly ignored.'});store.commit()
    new=e.detail(store,cfg,kind='incident',source_id='unmined')['diagnosis']
    assert new['facts'][0]['code']=='no_linked_learning'
    assert new['facts'][0]['destinations'][0]['href']=='#/review/mine/unmined'
    dismissed=e.detail(store,cfg,kind='incident',source_id='dismissed')['diagnosis']
    assert dismissed['facts'][0]['incident_status']=='dismissed' and dismissed['facts'][0]['destinations']==[]
    rule=e.detail(store,cfg,kind='learning',source_id='l00')['diagnosis']
    review=next(f for f in rule['facts'] if f['code']=='proposal_disposition')
    assert review['next_step']=='review' and review['destinations'][0]['href']=='#/review/proposal/p00'
    violation=next(f for f in rule['facts'] if f['code']=='reported_rule_violation')
    assert violation['certainty']=='reported'
    assert violation['destinations'][0]['mode']=='hook' and violation['destinations'][0]['href'].startswith('#/review/recovery/')
    assert len(violation['incident_ids'])==25


def test_approval_uses_exact_command_membership_without_claiming_delivery(cfg,store,tmp_path):
    from self_improve.dashboard import evidence_data as e
    from tests.test_apply import OLD
    (tmp_path/'approved.md').write_text(OLD)
    p=proposal(store,tmp_path/'approved.md')
    result=submit_command(store,cfg,decision(store,cfg,'approve',[p]))
    full=e.detail(store,cfg,kind='proposal',source_id=p['id'])
    fact=next(f for f in full['diagnosis']['facts'] if f['code']=='command_delivery')
    assert fact['command_id']==result['id'] and fact['proposal_id']==p['id']
    assert fact['target_state']!='completed'
    assert fact['destinations'][0]['source_id']==result['id']
    assert full['source']['status']=='approved_user'


def test_actual_observer_binds_inactive_copy_to_its_project_time_and_recovery(cfg,store,tmp_path):
    from self_improve.dashboard import evidence_data as e
    from tests.test_rule_availability import known_copy,delivery
    from self_improve.rule_availability import collect_availability
    clones=[init_git_repo(tmp_path/name,'AGENTS.md','# Human fixture\n') for name in ('copy-a','copy-b')]
    for clone in clones:run_git(['remote','add','origin','https://example.test/owner/observer.git'],clone)
    keys=[known_copy(store,clone) for clone in clones]
    assert keys[0]==keys[1]
    p,text=delivery(store,cfg,clones[0])
    (clones[1]/'AGENTS.md').write_text('# Human fixture\n'+text)
    store.insert('incidents',{'id':'copy-incident','session_file':str(clones[0]/'invented.jsonl'),
        'project_path':str(clones[0]),'project_key':keys[0],'signal_type':'correction','matched_text':'Existing rule did not help.',
        'window_json':'[]','status':'mined','ts':'2030-01-01T00:00:00Z','created_at':'2030-01-01T00:00:00Z'})
    store.insert('incident_learnings',{'incident_id':'copy-incident','learning_id':p['learning_id']});store.commit()
    collect_availability(store,cfg,observed_at='2030-01-02T00:00:00Z')
    facts=e.detail(store,cfg,kind='incident',source_id='copy-incident')['diagnosis']['facts']
    observed=[f for f in facts if f['code']=='observed_copy_availability']
    assert len(observed)==1
    assert observed[0]['status']=='absent' and observed[0]['working_copy']['normalized_path']==str(clones[0])
    assert observed[0]['incident_time_relations']==[{'incident_id':'copy-incident','incident_at':'2030-01-01T00:00:00.000000Z','relation':'after_incident'}]
    assert {d['kind'] for d in observed[0]['destinations']}=={'project_availability','recovery'}
    assert observed[0]['destinations'][-1]['mode']=='correct_target'
    instruction=e.search(store,cfg,query='Human fixture',kinds=['instruction'])
    assert instruction['pagination']['count']==2
    assert len({r['project_keys'][0] for r in instruction['rows']})==1
    revision=e.search(store,cfg,query='Keep the invented fixture readable',kinds=['revision'])
    assert revision['pagination']['count']==1
    # Once the incident's copy/time is unknown, no other copy is substituted.
    store.update('incidents','id','copy-incident',{'project_path':str(tmp_path/'unknown-copy'),'ts':''});store.commit()
    unknown=e.detail(store,cfg,kind='incident',source_id='copy-incident')['diagnosis']
    assert not any(f['code']=='observed_copy_availability' for f in unknown['facts'])
    assert any('Availability is unknown' in item for item in unknown['limitations'])


def test_real_pipeline_native_context_and_instruction_archives_are_searchable_without_io(tmp_path,monkeypatch):
    from tests.e2e_corpus import build_corpus
    from self_improve.pipeline import run_pipeline
    from self_improve.dashboard import evidence_data as e
    from self_improve.store import Store
    from pathlib import Path
    import builtins,io,os,sqlite3,subprocess
    corpus=build_corpus(tmp_path)
    try:
        (corpus.clone_a/'AGENTS.md').write_text('Native fixture complete instruction text.\n')
        run_pipeline(corpus.cfg,corpus.store,dry_run=True)
        saved=e.search(corpus.store,corpus.cfg,query='')
        assert saved['coverage']['session_context']['records']>0
        selected=tmp_path/'selected.db';conn=sqlite3.connect(selected);corpus.store.conn.backup(conn);conn.close()
        reader=Store(str(selected),read_only=True)
        corpus.store.insert('learnings',{'id':'outside-selected','title':'OutsideSelectedSentinel',
            'rule_text':'Unrelated mutable original database','created_at':'2030-01-01T00:00:00Z'})
        corpus.store.commit()
        try:
            def forbidden(*args,**kwargs):raise AssertionError('evidence reader escaped selected Store')
            with monkeypatch.context() as guard:
                for module,attribute in [(builtins,'open'),(io,'open'),(os,'open'),(Path,'open'),(sqlite3,'connect'),(subprocess,'Popen'),(os,'stat'),(os,'listdir'),(os,'scandir')]:
                    guard.setattr(module,attribute,forbidden)
                assert e.search(reader,corpus.cfg,query='OutsideSelectedSentinel')['pagination']['count']==0
                instruction=e.search(reader,corpus.cfg,query='Native fixture complete instruction',kinds=['instruction'])
                assert instruction['pagination']['count']==1
                e.detail(reader,corpus.cfg,kind='instruction',source_id=instruction['rows'][0]['source_id'])
                sessions=e.search(reader,corpus.cfg,query='',kinds=['session'])
                for row in sessions['rows']:e.detail(reader,corpus.cfg,kind='session',source_id=row['source_id'])
                assert reader.conn.total_changes==0
                assert reader.query_one('SELECT COUNT(*) n FROM llm_calls')['n']==0
        finally:reader.close()
    finally:corpus.store.close()


def test_diagnosis_revisions_change_with_policy_and_history_even_when_source_search_does_not(cfg,store,tmp_path):
    from self_improve.dashboard import evidence_data as e
    from self_improve.execution_policy import set_class_policy
    p=proposal(store,tmp_path/'eligible.md',status='gated_pass')
    store.update('proposals','id',p['id'],{'created_at':'2030-01-02T00:00:00Z'});store.commit()
    set_class_policy(store,'global',False,now='2029-01-01T00:00:00Z')
    before=e.detail(store,cfg,kind='proposal',source_id=p['id'])
    set_class_policy(store,'global',True,now='2030-01-01T00:00:00Z')
    after=e.detail(store,cfg,kind='proposal',source_id=p['id'])
    assert before['source_revision']==after['source_revision'] and before['revision']!=after['revision']
    assert before['diagnosis']['facts'][0]['next_step']=='review'
    assert after['diagnosis']['facts'][0]['next_step']=='automatic_delivery'


def test_current_and_historical_instruction_text_and_corruption(cfg,store,tmp_path):
    from self_improve.dashboard import evidence_data as e
    from tests.test_rule_availability import known_copy
    from self_improve.rule_availability import collect_availability
    repo=init_git_repo(tmp_path/'observed','AGENTS.md','ArchivedBodySentinel text.\n');known_copy(store,repo)
    collect_availability(store,cfg,observed_at='2030-01-01T00:00:00Z')
    (repo/'AGENTS.md').write_text('NewestBodySentinel text.\n');collect_availability(store,cfg,observed_at='2030-01-02T00:00:00Z')
    first=e.search(store,cfg,query='ArchivedBodySentinel',kinds=['instruction'])['rows'][0]
    latest=e.search(store,cfg,query='NewestBodySentinel',kinds=['instruction'])['rows'][0]
    assert e.detail(store,cfg,kind='instruction',source_id=first['source_id'])['source']['latest_selected_observation'] is False
    assert e.detail(store,cfg,kind='instruction',source_id=latest['source_id'])['source']['latest_selected_observation'] is True
    row=store.query_one('SELECT id FROM instruction_text_archives ORDER BY observed_at LIMIT 1')
    store.update('instruction_text_archives','id',row['id'],{'record_json':'[]'});store.commit()
    with pytest.raises(e.EvidenceError,match=row['id']) as corrupt:e.search(store,cfg,query='')
    assert corrupt.value.code=='EvidenceDataError'


def test_missing_detail_invalid_inputs_redacted_sources_and_hostile_html(cfg,store,tmp_path):
    from self_improve.dashboard import evidence_data as e
    seed(store,tmp_path)
    hostile='<img src=x onerror="bad()">'
    store.update('learnings','id','l00',{'title':hostile,'rule_text':'Contact invented@example.test\n'+hostile});store.commit()
    source=e.detail(store,cfg,kind='learning',source_id='l00')
    assert hostile in source['source']['rule_text'] and 'invented@example.test' not in source['source']['rule_text']
    # Keep markup as data; P02 must escape it at the actual DOM boundary.
    assert e.search(store,cfg,query='onerror')['pagination']['count']>=1
    with pytest.raises(e.EvidenceError) as missing:e.detail(store,cfg,kind='incident',source_id='missing')
    assert missing.value.status==404
    for kwargs in [{'query':None},{'query':'x'*1001},{'query':'','kinds':['no-kind']},{'query':'','project_key':''}]:
        with pytest.raises(e.EvidenceError) as invalid:e.search(store,cfg,**kwargs)
        assert invalid.value.status==400
    page=e.learning_evidence(store,learning_id='l00')
    store.update('incidents','id','i24',{'status':'dismissed'});store.commit()
    with pytest.raises(e.EvidenceError) as changed:e.learning_evidence(store,learning_id='l00',cursor=page['pagination']['next_cursor'])
    assert changed.value.status==409


def test_generated_explanations_do_not_become_source_search_matches(cfg,store,tmp_path):
    from self_improve.dashboard import evidence_data as e
    seed(store,tmp_path)
    assert e.search(store,cfg,query='canonical destinations require',kinds=['proposal'])['pagination']['count']==0
    assert e.search(store,cfg,query='Complete proposal only token',kinds=['proposal'])['pagination']['count']==1


def test_multiple_native_sessions_keep_their_own_incidents_and_learning_links(tmp_path):
    from tests.test_scan_observations import make_env, scan, ts, SID, SID_B
    from tests.test_scan_occurrences import write_codex, x_call, x_user
    from tests.test_session_context import meta
    from self_improve.dashboard import evidence_data as e
    env=make_env(tmp_path)
    try:
        path=write_codex(env,[meta(env.repo,ts(0),SID),x_call(ts(1),'a'),x_user("no, that's wrong",ts(2)),
            meta(env.repo,ts(3),SID_B),x_call(ts(4),'b'),x_user('no, use the other option',ts(5))])
        scan(env,'two-native')
        incidents=env.store.query('SELECT * FROM incidents')
        assert {i['session_id'] for i in incidents}=={SID,SID_B}
        for i in incidents:
            env.store.insert('learnings',{'id':i['session_id'],'rule_text':'Separate learning','created_at':ts(6)})
            env.store.insert('incident_learnings',{'incident_id':i['id'],'learning_id':i['session_id']})
        env.store.commit()
        for sid in (SID,SID_B):
            result=e.search(env.store,env.cfg,query=sid,kinds=['session'])
            assert result['pagination']['count']==1
            details=[e.detail(env.store,env.cfg,kind='session',source_id=r['source_id']) for r in result['rows']]
            selected=next(d for d in details if d['source']['native_session_id']==sid)
            assert {i['session_id'] for i in selected['source']['incidents']}=={sid}
            assert selected['learning_ids']==[sid]
            assert [t['file_path'] for t in selected['source']['transcripts']]==[str(path)]
        assert e.search(env.store,env.cfg,query='complete original transcript',kinds=['session'])['pagination']['count']==0
    finally:env.store.close()


def test_retained_session_errors_are_redacted_without_changing_identity(cfg,store,tmp_path):
    from self_improve.dashboard import evidence_data as e
    from self_improve.redact import redact_text
    secret='sk-'+'inventedSecret1234567890'*3
    error='FixtureParserError '+secret
    assert secret not in redact_text(error)
    path=str(tmp_path/'retained.jsonl')
    store.insert('sessions',{'file_path':path,'source':'codex','session_id':'error-native','error':error});store.commit()
    result=e.search(store,cfg,query='FixtureParserError',kinds=['session'])
    assert len(result['rows'])==1 and secret not in json.dumps(result)
    detail=e.detail(store,cfg,kind='session',source_id=result['rows'][0]['source_id'])
    transcript=detail['source']['transcripts'][0]
    assert transcript['error']==redact_text(error) and transcript['file_path']==path
    assert transcript['session_id']=='error-native'


def test_missing_context_batch_differs_from_valid_empty_batch_and_missing_schema(tmp_path):
    from tests.test_scan_observations import make_env,scan
    from tests.test_scan_occurrences import write_codex
    from self_improve.dashboard import evidence_data as e
    env=make_env(tmp_path)
    try:
        write_codex(env,[]);scan(env,'empty-context')
        observed=env.store.query_one('SELECT id FROM scan_observations')['id']
        valid=e.search(env.store,env.cfg,query='')['coverage']['session_context']
        assert valid['observations']==valid['batches']==valid['empty_batches']==1
        assert valid['missing_batches']==0 and valid['records']==0
        env.store.conn.execute('DELETE FROM session_context_batches');env.store.commit()
        missing=e.search(env.store,env.cfg,query='')['coverage']['session_context']
        assert missing['observations']==missing['missing_batches']==1 and missing['batches']==0
        assert missing['missing_observation_ids']==[observed]
        row=e.search(env.store,env.cfg,query='',kinds=['session'])['rows'][0]
        assert e.detail(env.store,env.cfg,kind='session',source_id=row['source_id'])['coverage']['session_context']==missing
        env.store.conn.execute("DELETE FROM schema_migrations WHERE name='0030_session_context'");env.store.commit()
        absent=e.search(env.store,env.cfg,query='')['coverage']['session_context']
        assert absent['available'] is False and absent['reason']=='schema_unavailable'
    finally:env.store.close()


def test_collapsed_rollback_is_visible_for_every_affected_member(cfg,tmp_path):
    from self_improve.store import Store
    from tests.test_inverse_rollback import propose
    from tests.test_delivery_worker import approve
    from self_improve.worker import run_once
    from self_improve import apply
    from self_improve.dashboard import evidence_data as e
    s=Store(cfg.state_path('state.db'))
    try:
        target=tmp_path/'rules.md';before='first\nmiddle\nlast\n';after='FIRST\nmiddle\nlast\n';target.write_text(before)
        p,q=[propose(s,target,before,after,status='ungated') for _ in range(2)]
        approve(s,cfg,p,q);run_once(s,cfg)
        rolled=apply.rollback(s,cfg,p['id'])
        assert set(rolled['affected_proposal_ids'])=={p['id'],q['id']}
        for selected in (p,q):
            detail=e.detail(s,cfg,kind='proposal',source_id=selected['id'])
            ops=[f for f in detail['diagnosis']['facts'] if f['code']=='instruction_operation']
            assert len(ops)==1 and ops[0]['operation_kind']=='rollback' and ops[0]['state']=='completed'
    finally:s.close()


def test_diagnosis_uses_retained_copy_identity_for_symlink_cwd(tmp_path,monkeypatch):
    from dataclasses import replace
    from tests.test_scan_observations import make_env,scan,ts
    from tests.test_scan_occurrences import write_codex,x_user,x_call
    from tests.test_session_context import meta
    from tests.test_rule_availability import delivery,cfg as availability_cfg
    from self_improve import rule_availability
    from self_improve.execution_policy import set_class_policy
    from self_improve.dashboard import evidence_data as e
    import builtins,io,os,sqlite3,subprocess
    from pathlib import Path
    root=tmp_path.resolve();env=make_env(root)
    env.cfg=replace(availability_cfg.__wrapped__(root),claude_projects_dir=str(env.claude_dir),
        codex_sessions_dir=str(env.codex_dir),project_identity_use_gh=False,denylist_substrings=('denied-tree',))
    try:
        set_class_policy(env.store,'project',True,now='2020-01-01T00:00:00Z')
        repo=init_git_repo(root/'actual','AGENTS.md','# Temporary fixture\n')
        run_git(['remote','add','origin','https://example.test/fixture/alias.git'],repo)
        alias=root/'alias';alias.symlink_to(repo,target_is_directory=True)
        p,text=delivery(env.store,env.cfg,repo)
        write_codex(env,[meta(str(alias)),x_call(ts(1),'call-a'),x_user("no, that's wrong",ts(2))]);scan(env,'aliased')
        incident=env.store.query_one('SELECT * FROM incidents')
        env.store.insert('incident_learnings',{'incident_id':incident['id'],'learning_id':p['learning_id']})
        env.store.update('incidents','id',incident['id'],{'status':'mined'});env.store.commit()
        rule_availability.collect_availability(env.store,env.cfg,observed_at='2030-01-01T00:00:00Z')
        copy_id=env.store.query_one('SELECT working_copy_id FROM scan_occurrences')['working_copy_id']
        assert incident['project_path']==str(alias)
        def forbidden(*args,**kwargs):raise AssertionError('reader inspected filesystem')
        with monkeypatch.context() as guard:
            for module,attribute in [(builtins,'open'),(io,'open'),(os,'open'),(Path,'open'),(sqlite3,'connect'),(subprocess,'Popen'),(os,'stat'),(os,'listdir'),(os,'scandir')]:
                guard.setattr(module,attribute,forbidden)
            result=e.detail(env.store,env.cfg,kind='incident',source_id=incident['id'])
        observed=[f for f in result['diagnosis']['facts'] if f['code']=='observed_copy_availability']
        assert len(observed)==1 and observed[0]['working_copy_id']==copy_id and observed[0]['status']=='absent'
        assert any(d['kind']=='recovery' and d['mode']=='correct_target' for d in observed[0]['destinations'])
        # Indexed copy changes must fail hash validation, never borrow a copy.
        occurrence=env.store.query_one('SELECT * FROM scan_occurrences')
        env.store.update('scan_occurrences','id',occurrence['id'],{'working_copy_id':'changed-copy'});env.store.commit()
        with pytest.raises(e.EvidenceError,match='occurrence') as corrupt:
            e.detail(env.store,env.cfg,kind='incident',source_id=incident['id'])
        assert corrupt.value.code=='EvidenceDataError'
        env.store.update('scan_occurrences','id',occurrence['id'],{'working_copy_id':copy_id});env.store.commit()
        # A later rescan of identical trigger lines can retain a different copy
        # under the same occurrence ID. The link does not bind a revision ID.
        other=init_git_repo(root/'other','AGENTS.md','# Temporary fixture\n')
        run_git(['remote','add','origin','https://example.test/fixture/alias.git'],other)
        alias.unlink();alias.symlink_to(other,target_is_directory=True)
        from self_improve.scan import mark_for_rescan
        mark_for_rescan(env.store,env.cfg);scan(env,'aliased-again')
        versions=env.store.query('SELECT occurrence_id,working_copy_id FROM scan_occurrences')
        assert len({r['occurrence_id'] for r in versions})==1
        assert len({r['working_copy_id'] for r in versions})==2
        unknown=e.detail(env.store,env.cfg,kind='incident',source_id=incident['id'])['diagnosis']
        assert not any(f['code']=='observed_copy_availability' for f in unknown['facts'])
        assert any('conflicting scan bindings' in line for line in unknown['limitations'])
    finally:env.store.close()


from tests.test_session_context import env as native_env
from tests.test_incident_jobs import env as mining_env


def test_actual_native_report_is_complete_searchable_and_keeps_unknown_receipt_time(native_env):
    from tests.test_native_loads import event
    from self_improve.native_loads import record_native_event
    from self_improve.dashboard import evidence_data as e
    got=record_native_event(native_env.store,native_env.cfg,event(native_env))
    found=e.search(native_env.store,native_env.cfg,query='InstructionsLoaded',kinds=['session'])
    assert found['coverage']['native_reports']['records']==1
    assert len(found['rows'])==1
    detail=e.detail(native_env.store,native_env.cfg,kind='session',source_id=found['rows'][0]['source_id'])
    assert detail['source']['native_reports']==[got]
    assert got['occurred_at'] is None and got['loaded_content_hash'] is None


@pytest.mark.parametrize('indexed', [False, True])
def test_native_report_search_and_api_preserve_provider_without_a_transcript(native_env, indexed):
    TestClient = pytest.importorskip('fastapi.testclient').TestClient
    from self_improve.dashboard.app import create_app
    from self_improve.native_loads import record_native_event
    from tests.test_native_loads import event as claude_event
    from tests.test_codex_session_reports import event as codex_event

    store, cfg = native_env.store, native_env.cfg
    reports = {
        provider: record_native_event(store, cfg, make_event(native_env), provider=provider)
        for provider, make_event in [('claude', claude_event), ('codex', codex_event)]
    }
    sid = reports['codex']['payload']['session_id']
    assert reports['claude']['payload']['session_id'] == sid
    if indexed:
        for provider in reports:
            store.insert('sessions', {'file_path': str(native_env.tmp / (provider + '-missing.jsonl')),
                         'source': provider, 'session_id': sid})
        store.commit()
    before = list(store.conn.iterdump())
    with TestClient(create_app(cfg, db_path=store.db_path)) as client:
        found = client.get('/api/evidence', params={'query': sid, 'kinds': 'session'}).json()
        assert found['pagination']['count'] == found['coverage']['native_reports']['records'] == 2
        assert {row['title'] for row in found['rows']} == {'Claude Code session '+sid, 'Codex session '+sid}
        for provider, report in reports.items():
            selected = client.get('/api/evidence', params={'query': provider + ' ' + sid, 'kinds': 'session'}).json()
            assert [row['source_id'] for row in selected['rows']] == [report['logical_session_key']]
            response = client.get('/api/evidence/session/' + report['logical_session_key'])
            assert response.status_code == 200
            source = response.json()['source']
            assert source['provider'] == provider and source['native_session_id'] == sid
            assert source['native_reports'] == [report]
            assert len(source['transcripts']) == int(indexed)
            assert not source['incidents'] and not source['native_context']
            assert report['occurred_at'] is report['loaded_content_hash'] is None
    assert list(store.conn.iterdump()) == before
    assert store.query_one('SELECT COUNT(*) n FROM llm_calls')['n'] == 0


def test_actual_incident_job_is_linked_without_executing_it(mining_env):
    from tests.test_incident_jobs import request
    from self_improve.dashboard import evidence_data as e
    cfg,store,incident,calls=mining_env
    command=submit_command(store,cfg,request(mining_env))
    detail=e.detail(store,cfg,kind='incident',source_id=incident['id'])
    fact=next(f for f in detail['diagnosis']['facts'] if f['code']=='no_linked_learning')
    assert fact['jobs']==[{'id':command['id'],'state':command['state'],'action':'mine_incident'}]
    assert any(d['kind']=='command' and d['source_id']==command['id'] for d in fact['destinations'])
    assert calls==[]
