"""Full Review preserves each outcome while keeping decisions together."""
from pathlib import Path
import shutil
import subprocess

from tests.spa_assets import copy_spa_dependencies


def test_review_outcome_inventory_and_scoped_actions(tmp_path):
    node = shutil.which('node')
    assert node
    static = Path(__file__).resolve().parents[1] / 'src/self_improve/dashboard/static'
    (tmp_path / 'app.mjs').write_bytes((static / 'app.js').read_bytes())
    copy_spa_dependencies(tmp_path)
    (tmp_path / 'check.mjs').write_text(r'''
import assert from 'node:assert/strict';
import {state,renderReviewDetail,renderReviewActions} from './app.mjs';
const records=[{id:'historical',verdict:'ungated'},{id:'paired',verdict:'gated_fail'},
 {id:'unavailable',verdict:'gated_pass'}, {id:'stale',verdict:'gated_fail'},
 {id:'pending',verdict:'unknown'}, null];
const proposals=records.map((evaluation,i)=>({id:'p'+i,content_revision:'r'+i}));
const members=proposals.map((p,i)=>({proposal_id:p.id,content_revision:p.content_revision,
 revision:'auth'+i,snapshot:{evaluation:records[i],evidence:[]}}));
const family={learning_id:'L',rule_text:'Inspect the complete output.',lead_reason:'Review retained evidence',proposals,target_rows:[]};
state.reviewPreviews.L={signature:JSON.stringify(proposals.map(p=>[p.id,p.content_revision])),
 data:{ready:true,members,targets:[]}};
state.reviewEvaluations.historical={result:{record:records[0]},attempts:[]};
state.reviewEvaluations.paired={result:{record:records[1]},attempts:[]};
state.reviewEvaluations.unavailable={error:'Invented unavailable <source>'};
state.reviewEvaluations.stale={result:{record:{id:'stale',verdict:'changed'}},attempts:[]};
state.reviewEvaluations.pending={loading:true};
let html=renderReviewDetail(family);
assert.match(html,/5 evaluation results · selected proposal order/);
assert.match(html,/data-review-key="review-evaluation:historical" open/);
assert.doesNotMatch(html,/data-review-key="review-evaluation:paired" open/);
assert.match(html,/Result 1 of 5 · ungated · 1 selected proposal/);
assert.match(html,/Result 2 of 5 · gated_fail · 1 selected proposal/);
assert.match(html,/Result 3 of 5 · gated_pass · 1 selected proposal/);
assert.match(html,/Invented unavailable &lt;source&gt;/);
assert.match(html,/Evaluation evidence changed since the selected preview/);
assert.match(html,/Reading exact evaluation provenance/);
assert.match(html,/1 selected proposal has no linked evaluation/);
assert.match(html,/Historical reported trials/);
assert.match(html,/Paired comparison unknown/);
// An unfavorable linked result remains explicit even while its body is closed.
const arm={observed_passes:null,completed:null,valid_trials:null,requested_trials:3,served_models:[]};
state.reviewEvaluations.paired.attempts=[{data:{source:{id:'A'},scenarios:[{}],events:[]},
 scenario:{scenario:0,arms:{with:arm,without:arm},comparison:{computable:false,reason:'Missing <model> telemetry'}}}];
html=renderReviewDetail(family);
assert.match(html,/Scenario 1: paired change unavailable · Missing &lt;model&gt; telemetry/);
assert.match(html,/Missing results are not zero/);
assert.doesNotMatch(html,/0 \/ 3 valid planned trials/);
assert.doesNotMatch(html,/data-review-key="review-evaluation:paired" open/);
const actions=renderReviewActions(family,{full:true});
assert.ok(actions.indexOf('data-decision="approve"') < actions.indexOf('data-decision="reject"'));
assert.ok(actions.indexOf('data-decision="reject_lesson"') < actions.indexOf('data-review-individual'));
assert.match(actions,/Approve selected \(6\)/);
assert.match(actions,/unselected and future targets/);
// The compact queue keeps its existing action ordering and labels.
const queue=renderReviewActions(family);
assert.ok(queue.indexOf('data-review-individual') < queue.indexOf('data-decision="reject"'));
state.reviewDisclosures['review-evaluation:historical']=false;
state.reviewDisclosures['review-evaluation:paired']=true;
html=renderReviewDetail(family);
assert.doesNotMatch(html,/data-review-key="review-evaluation:historical" open/);
assert.match(html,/data-review-key="review-evaluation:paired" open/);
// Preview failure remains the authority for all three decisions.
state.reviewPreviews.L={error:'Cannot inspect selected edits'};
assert.equal((renderReviewActions(family,{full:true}).match(/ disabled/g)||[]).length,3);
console.log('REVIEW_DECISIONS_OK');
''')
    result = subprocess.run([node, str(tmp_path / 'check.mjs')], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'REVIEW_DECISIONS_OK'
