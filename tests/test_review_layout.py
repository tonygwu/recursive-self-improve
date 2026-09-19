"""Review density must retain exact destinations, conflicts and budget warnings."""
from pathlib import Path
import shutil
import subprocess
from tests.spa_assets import copy_spa_dependencies


def test_compact_review_preserves_complete_decision_evidence(tmp_path):
    node=shutil.which('node')
    assert node
    static=Path(__file__).resolve().parents[1]/'src/self_improve/dashboard/static'
    (tmp_path/'app.mjs').write_bytes((static/'app.js').read_bytes())
    copy_spa_dependencies(tmp_path)
    (tmp_path/'check.mjs').write_text(r'''
import assert from 'node:assert/strict';
import {state,renderSelectedPreview,renderReviewActions,renderProposalBreakdown} from './app.mjs';
const family={learning_id:'L',proposals:[{id:'a',revision:'one',why_needs_you:'Reason one',target_path:'/complete/path'},
 {id:'b',revision:'two',why_needs_you:'Reason two',target_path:'/complete/path'}],target_rows:[{}]};
state.reviewPreviews.L={signature:JSON.stringify([['a','one'],['b','two']]),data:{ready:true,members:[{},{}],targets:[{
 target_key:'target',proposal_ids:['a','b'],state:'ready',destination:{mode:'direct_file',target_path:'/complete/path'},
 budget:{before:3,after:4,limit:3,over:true},diff_unified:'+ complete patch <tag>'}]}};
assert.match(renderReviewActions(family),/btn--primary/);
function checkHints() {
 const actions=renderReviewActions(family);
 for (const [decision,key] of [['approve','a'],['reject','r']]) {
   const button=actions.match(new RegExp('<button[^>]*data-decision="'+decision+'"[^>]*>[\\s\\S]*?</button>'))[0];
   assert.match(button,new RegExp('aria-keyshortcuts="'+key+'"'));
   assert.match(button,new RegExp('<kbd class="kbd" aria-hidden="true">'+key+'</kbd>'));
 }
 const everywhere=actions.match(/<button[^>]*data-decision="reject_lesson"[^>]*>[\s\S]*?<\/button>/)[0];
 assert.match(everywhere,/aria-keyshortcuts="Shift\+r"/);
 assert.match(everywhere,/<kbd class="kbd" aria-hidden="true">⇧r<\/kbd>/);
 assert.match(actions,/selected reviewed targets/);
 assert.match(actions,/rejects the lesson everywhere/);
 return actions;
}
checkHints();
const html=renderSelectedPreview(family);
assert.match(html,/\/complete\/path/);assert.match(html,/Above the budget; approval remains available/);
assert.match(html,/Complete combined edit/);assert.match(html,/complete patch &lt;tag&gt;/);
assert.doesNotMatch(html,/<details class="review-targets"[^>]* open/);
assert.match(html,/<summary[^>]*>[\s\S]*Ready[\s\S]*<\/summary>/);
const reasons=renderProposalBreakdown(family);
assert.match(reasons,/<details class="review-breakdown"/);
assert.match(reasons,/Reason one/);assert.match(reasons,/Reason two/);
state.reviewPreviews.L.data.ready=false;
Object.assign(state.reviewPreviews.L.data.targets[0],{state:'conflict',detail:'Two incompatible alternatives'});
assert.match(renderSelectedPreview(family),/<summary[^>]*>[\s\S]*Conflict[\s\S]*<\/summary>/);
assert.match(renderSelectedPreview(family),/Two incompatible alternatives/);
assert.match(renderReviewActions(family),/data-decision="approve"[^>]*disabled/);
for (const entry of [{loading:true},{error:'Invented failure'},undefined]) {
 state.reviewPreviews.L=entry;
 const actions=checkHints();
 for (const decision of ['approve','reject','reject_lesson']) {
   assert.match(actions,new RegExp('data-decision="'+decision+'"[^>]*disabled'));
 }
}
console.log('REVIEW_LAYOUT_EVIDENCE_OK');
''')
    result=subprocess.run([node,str(tmp_path/'check.mjs')],capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stderr
    assert result.stdout.strip()=='REVIEW_LAYOUT_EVIDENCE_OK'
