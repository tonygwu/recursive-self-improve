"""Selected Review labels use retained identities without changing authority."""
from contextlib import closing
from copy import deepcopy
from pathlib import Path
import hashlib,json,shutil,subprocess
import pytest
from fastapi.testclient import TestClient
from self_improve import commands
from self_improve.dashboard import review_evidence
from self_improve.dashboard.app import create_app
from self_improve.mining_history import digest
from self_improve.review import preview_selection
from self_improve.store import Store
from tests.test_review_preview import env,preview,approval
from tests.spa_assets import copy_spa_dependencies

KEY='remote:example.test/invented/original'


def seed(store,root,learning_id):
    for name,provider,native,key,label in [
        ('claude','claude','shared-native','remote:example.test/invented/moved','invented/moved'),
        ('codex','codex','shared-native',KEY,'invented/original'),
        ('alias','codex','another-native',KEY,'invented/original-alias'),
        ('unknown','','shared-native','',''),('missing-native','codex','',KEY,'')]:
        path=str(root/(name+'.jsonl'))
        store.insert('sessions',{'file_path':path,'source':provider,'session_id':native,'project_key':key,'project_display':label})
        store.insert('incidents',{'id':name,'session_file':path,'session_id':native,'project_key':KEY if name!='unknown' else '',
            'project_path':str(root/'unrelated-path'),'signal_type':'correction','ts':'2030-01-01T00:00:00Z','created_at':'2030-01-01T00:00:00Z',
            'matched_text':'Read the “invented” output <tag>.','window_json':'[]'})
        store.link_incident_learning(name,learning_id)
    store.commit()


def test_preview_identity_is_exact_read_only_and_not_authority(env,tmp_path,monkeypatch):
    cfg,store,target,before,a,b=env;seed(store,tmp_path,a['learning_id'])
    with store.transaction():baseline=preview_selection(store,cfg,[a['id'],b['id']])
    original=deepcopy(baseline);dump=list(store.conn.iterdump())
    with store.transaction():enriched=review_evidence.enrich(store,baseline)
    assert baseline==original
    assert {k:v for k,v in enriched.items() if k!='evidence_identity'}==baseline
    assert list(store.conn.iterdump())==dump
    data=enriched['evidence_identity'];assert data['preview_revision']==baseline['revision']
    records={r['incident_id']:r for r in data['records']};assert len(records)==5
    for incident in baseline['members'][0]['snapshot']['evidence']:
        assert records[incident['id']]['source_revision']==digest(incident)
    assert records['claude']['identity']['project']['key']==KEY
    assert records['claude']['identity']['project']['aliases']==['invented/original','invented/original-alias']
    assert records['claude']['identity']['session']['provider_label']=='Claude Code'
    assert records['codex']['identity']['session']['provider_label']=='Codex'
    assert records['claude']['identity']['session']['key']!=records['codex']['identity']['session']['key']
    assert records['unknown']['identity']['session']['identity_kind']=='transcript'
    assert records['missing-native']['identity']['session']['identity_kind']=='transcript'
    assert records['unknown']['identity']['project']['label']=='Project unknown'
    with TestClient(create_app(cfg)) as client:
        shown=preview(client,a,b)
        assert shown==enriched
        rejected=client.post('/api/commands',json={**approval(shown),'evidence_identity':data})
        assert rejected.status_code==400
        accepted=client.post('/api/commands',json=approval(shown));assert accepted.status_code==202,accepted.text
    assert target.read_text()==before


def test_labels_change_only_with_supplied_store_snapshot_and_preserve_revisions(env,tmp_path):
    cfg,store,target,before,a,b=env;seed(store,tmp_path,a['learning_id'])
    with store.transaction():original=review_evidence.enrich(store,preview_selection(store,cfg,[a['id']]))
    copy_path=tmp_path/'copy.db'
    with closing(Store(copy_path)) as copied:
        store.conn.backup(copied.conn)
        copied.conn.execute("UPDATE sessions SET project_display='copied-name' WHERE project_key=?",(KEY,));copied.commit()
    with TestClient(create_app(cfg,db_path=copy_path)) as client:
        copied=preview(client,a)
    assert copied['revision']==original['revision'] and copied['members']==original['members']
    assert all(r['identity']['project']['label']=='copied-name' for r in copied['evidence_identity']['records'] if r['incident_id']!='unknown')
    with store.transaction():again=review_evidence.enrich(store,preview_selection(store,cfg,[a['id']]))
    assert again==original
    store.update('incidents','id','claude',{'session_id':'a-changed-native-id'});store.commit()
    with store.transaction():changed=review_evidence.enrich(store,preview_selection(store,cfg,[a['id']]))
    assert changed['revision']!=original['revision']
    old=next(r for r in original['evidence_identity']['records'] if r['incident_id']=='claude')
    new=next(r for r in changed['evidence_identity']['records'] if r['incident_id']=='claude')
    assert old['source_revision']!=new['source_revision'] and old['identity']['session']['key']!=new['identity']['session']['key']
    assert target.read_text()==before


def test_ambiguous_duplicate_sources_are_refused_without_mutating_preview(env,tmp_path):
    cfg,store,_,_,a,b=env;seed(store,tmp_path,a['learning_id'])
    with store.transaction():shown=preview_selection(store,cfg,[a['id'],b['id']])
    shown['members'][1]['snapshot']['evidence'][0]['matched_text']='A different retained row'
    before=deepcopy(shown)
    with store.transaction(),pytest.raises(commands.CommandError,match='disagree'):
        review_evidence.enrich(store,shown)
    assert shown==before


def test_browser_validates_exact_identity_bindings_and_keeps_labels_escaped(env,tmp_path):
    cfg,store,_,_,a,b=env;seed(store,tmp_path,a['learning_id'])
    with store.transaction():shown=review_evidence.enrich(store,preview_selection(store,cfg,[a['id']]))
    node=shutil.which('node');assert node
    static=Path(__file__).resolve().parents[1]/'src/self_improve/dashboard/static'
    (tmp_path/'app.mjs').write_bytes((static/'app.js').read_bytes());copy_spa_dependencies(tmp_path)
    (tmp_path/'preview.json').write_text(json.dumps(shown))
    (tmp_path/'check.mjs').write_text(r'''
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {validateReviewEvidenceIdentity,renderSourceIdentity,renderIncidentExamples,state} from './app.mjs';
const shown=JSON.parse(fs.readFileSync(new URL('./preview.json',import.meta.url)));
const labels=await validateReviewEvidenceIdentity(shown);
assert.equal(labels.claude.session.provider_label,'Claude Code');
assert.equal(labels.claude.project.label,'invented/original');
const original=JSON.stringify(shown);
for (const change of [p=>p.evidence_identity.profile='wrong',p=>p.evidence_identity.preview_revision='wrong',
 p=>p.members[0].snapshot.evidence[0].matched_text='changed',p=>p.evidence_identity.records[0].incident_id='another',
 p=>p.evidence_identity.records.push(p.evidence_identity.records[0]),p=>p.evidence_identity.records.pop()]) {
 const p=structuredClone(shown);change(p);await assert.rejects(validateReviewEvidenceIdentity(p));
}
assert.equal(JSON.stringify(shown),original);
assert.equal(await validateReviewEvidenceIdentity({}),null);
const id=structuredClone(labels.claude);id.project.label='<script>invented</script>';
const html=renderSourceIdentity(id,{prefix:'review-identity:claude'});
assert.match(html,/&lt;script&gt;invented&lt;\/script&gt;/);assert.doesNotMatch(html,/<script>/);
assert.match(html,/id="review-identity:claude:session"/);assert.match(html,/data-review-key="review-identity:claude:project:0:aliases"/);
const examples=renderIncidentExamples({learning_id:'L',provenance:{examples:[{id:'claude',ts:'2030',project_path:'/unrelated',matched_text:'Invented',presentation:{identity:labels.claude}}]}});
assert.match(examples,/Claude Code/);assert.match(examples,/invented\/original/);assert.doesNotMatch(examples,/invented\/moved/);
console.log('REVIEW_IDENTITY_BINDING_OK');
''')
    result=subprocess.run([node,str(tmp_path/'check.mjs')],capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stderr
    assert result.stdout.strip()=='REVIEW_IDENTITY_BINDING_OK'
