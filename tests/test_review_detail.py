"""Full Review uses exact selected snapshots and retained evaluation links."""
from pathlib import Path
import json, shutil, subprocess
from tests.spa_assets import copy_spa_dependencies


def build_review(root, patch):
    from dataclasses import replace
    from self_improve.config import Config
    from self_improve.store import Store
    from tests.test_model_jobs import env
    from tests.test_eval_history import execute
    from tests.test_apply import insert_proposal, make_diff, OLD, NEW
    from self_improve.llm import LLMRunner
    store=Store(root/'state/state.db')
    cfg,store,p,calls=env.__wrapped__(Config(state_dir=str(root/'state')),store,root,patch)
    cfg=replace(cfg,global_claude_md=p['target_path'])
    store.update('learnings','id',p['learning_id'],{'rule_text':'Inspect complete output before replacing a configuration file.', 'title':'Inspect output', 'why':'An invented retained lesson for review.'});store.commit()
    result,attempt=execute((cfg,store,p,calls))
    # The real job path above uses invented synchronous answers only.
    def refuse(*a,**kw):raise AssertionError('Real models forbidden in Review detail')
    patch.setattr(LLMRunner,'_execute',refuse)
    store.update('proposals','id',p['id'],{'status':'gated_fail'})
    target=Path(p['target_path'])
    b=insert_proposal(store,target=target,diff=make_diff(OLD,NEW),status='ungated')
    store.update('proposals','id',b['id'],{'learning_id':p['learning_id'],'eval_result_id':'legacy-eval'})
    store.insert('eval_results',{'id':'legacy-eval','kind':'self_eval','verdict':'ungated'})
    project='remote:example.test/invented/review'
    for i in range(7):
        session=str(root/f'invented-session-{i}.jsonl')
        native='shared-native' if i<2 else '' if i==4 else f'native-{i}'
        provider='' if i==2 else 'claude' if i%2 else 'codex'
        store.insert('sessions',{'file_path':session,'source':provider,'session_id':native,
            'project_key':'remote:example.test/invented/moved' if i==0 else project,
            'project_display':'invented/moved' if i==0 else 'invented/review-alias' if i==3 else 'invented/review'})
        store.insert('incidents',{'id':f'detail-incident-{i}','session_file':session,'session_id':native,'project_key':project if i!=6 else '', 'project_path':str(root/'invented-repo'),
            'created_at':'2030-01-01T00:00:00Z','ts':f'2030-01-01T00:00:0{i}Z' if i else '', 'signal_type':'correction',
            'matched_text':f'Inspect the invented output {i} <script>text</script>. '+('x'*450+' UNIQUE COMPLETE TAIL' if i==0 else ''),
            'window_json':json.dumps([{'role':'user','text':f'Read the invented file {i} before proposing a change.'}])})
        store.insert('incident_learnings',{'incident_id':f'detail-incident-{i}','learning_id':p['learning_id']})
    conflict=[]
    for i in range(2):
        c=insert_proposal(store,target=target,diff=make_diff(OLD,f'Conflicting alternative {i}\n'),status='pending')
        if conflict:store.update('proposals','id',c['id'],{'learning_id':conflict[0]['learning_id']})
        conflict.append(c)
    store.update('learnings','id',c['learning_id'] if len(conflict)==1 else conflict[0]['learning_id'],{'rule_text':'Choose one existing alternative before approval.'})
    store.commit()
    return cfg,store,{'project':project,'family':p['learning_id'],'proposals':[p['id'],b['id']], 'conflict':conflict[0]['learning_id'],
        'conflict_proposals':[c['id'] for c in conflict], 'attempt':attempt['source']['id'], 'synthetic_calls':len(calls),
        'result':store.query_one('SELECT eval_result_id FROM proposals WHERE id=?',(p['id'],))['eval_result_id']}


def test_full_review_selection_binding_and_complete_evidence(tmp_path):
    node=shutil.which('node');assert node
    static=Path(__file__).resolve().parents[1]/'src/self_improve/dashboard/static'
    (tmp_path/'app.mjs').write_bytes((static/'app.js').read_bytes());copy_spa_dependencies(tmp_path)
    (tmp_path/'check.mjs').write_text(r'''
import assert from 'node:assert/strict';
import {state,renderReviewDetail,reviewDetailMembers,parseRoute,loadReviewEvaluation} from './app.mjs';
import {safeNavigationLink} from './navigation.js';
const family={learning_id:'L',rule_text:'Read <script>complete text</script>',lead_reason:'Missing evaluation',proposals:[{id:'a',content_revision:'one'},{id:'b',content_revision:'two'}],target_rows:[]};
const evidence=Array.from({length:7},(_,i)=>({id:'incident-'+i,signal_type:'correction',matched_text:'x'.repeat(450)+' COMPLETE TAIL <tag>',window_json:'[]'}));
const member={proposal_id:'a',content_revision:'one',revision:'auth-a',snapshot:{evidence,evaluation:{id:'E',verdict:'ungated'}}};
const second={proposal_id:'b',content_revision:'two',revision:'auth-b',snapshot:{evidence,evaluation:null}};
state.reviewPreviews.L={signature:JSON.stringify([['a','one'],['b','two']]),data:{ready:true,revision:'exact-preview',members:[member,second],targets:[{target_key:'T',proposal_ids:['a','b'],state:'ready',destination:{mode:'direct_file',target_path:'/complete/path'},diff_unified:'+ complete edit <tag>'}]}};
assert.equal(reviewDetailMembers(family).length,2);
let html=renderReviewDetail(family);
assert.match(html,/1 selected proposal has no linked evaluation/);
assert.match(html,/7 retained incidents/);assert.match(html,/1–3 of 7/);
assert.match(html,/COMPLETE TAIL &lt;tag&gt;/);assert.doesNotMatch(html,/<script>/);
assert.match(html,/<details class="review-targets"[^>]*open/);
assert.match(html,/complete edit &lt;tag&gt;/);assert.match(html,/exact-preview/);
state.reviewEvidencePages.L=2;assert.match(renderReviewDetail(family),/7–7 of 7/);
state.reviewEvaluations.E={result:{record:{verdict:'ungated',id:'E'}},attempts:[]};
assert.match(renderReviewDetail(family),/No exact attempt link/);
state.reviewEvaluations.E.result.record.verdict='changed';assert.match(renderReviewDetail(family),/Evaluation evidence changed/);
state.reviewExcluded.b=true;assert.deepEqual(reviewDetailMembers(family),[]);
assert.doesNotMatch(renderReviewDetail(family),/7 retained incidents|COMPLETE TAIL/);
assert.equal(parseRoute('#/review/family/L%20one').id,'family/L one');
assert.ok(safeNavigationLink({href:'#/review/family/L',label:'Full review'}));
// Malformed linked-reader results do not become trial evidence.
globalThis.fetch=async()=>({ok:true,json:async()=>({record:{id:'wrong'}})});
await loadReviewEvaluation('requested');assert.match(state.reviewEvaluations.requested.error,/different result/);
let finish;globalThis.fetch=()=>new Promise(r=>finish=r);
const pending=loadReviewEvaluation('late');state.reviewEvaluations={};
finish({ok:true,json:async()=>({record:{id:'late'},attempt_links:[]})});await pending;
assert.equal(state.reviewEvaluations.late,undefined);
console.log('FULL_REVIEW_BINDING_OK');
''')
    result=subprocess.run([node,str(tmp_path/'check.mjs')],capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stderr
    assert result.stdout.strip()=='FULL_REVIEW_BINDING_OK'
