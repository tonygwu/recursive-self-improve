"""Evals disclosure and refresh retain the meaning of sparse class evidence."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest
from tests.spa_assets import copy_spa_dependencies


def test_sparse_evidence_disclosure_and_failed_refresh_preserve_class_consent(tmp_path):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for the shipped JavaScript renderer')
    static = Path(__file__).resolve().parents[1] / 'src/self_improve/dashboard/static'
    (tmp_path / 'app.mjs').write_bytes((static / 'app.js').read_bytes())
    copy_spa_dependencies(tmp_path)
    script = r'''
import assert from 'node:assert/strict';
import {state,renderClassEvidence,renderQualitySamples,loadClassEvidence} from './app.mjs';
const classes=['global','project','skill','hook'].map(target_class=>({target_class,
 applied_revisions:0,rolled_back_revisions:0,policy:{enabled:false,revision:0},
 sample_advice:{applied:20},availability:null,latest_observation_at:null,
 quality:{precision_numerator:1,precision_denominator:1,precision:null,uncertain:2,unreviewed:3},
 evaluations:{passed:0,comparable:0,attempts:7}}));
classes[1].quality=null;classes[1].evaluations=null;classes[1].applied_revisions=null;
classes[2].quality={...classes[0].quality,precision_numerator:19,precision_denominator:20,precision:.95};
const data={classes,quality_available:true,policy_available:true,coverage:{unknown:'<invented>'},meaning:{note:'separate counts'}};
const before=JSON.stringify(data);
let html=renderClassEvidence({data});
assert.equal((html.match(/role="switch"/g)||[]).length,4);
assert.ok(/id="policy-switch-hook"[^>]* disabled/.test(html));
assert.ok(!/id="policy-switch-global"[^>]* disabled/.test(html));
assert.ok(html.includes('1 / 1 useful'));assert.ok(!html.includes('100%'));
assert.ok(html.includes('19 / 20 useful · 95%'));
assert.ok(html.includes('Quality evidence unavailable'));assert.ok(html.includes('<td>Unknown</td>'));
assert.ok(html.includes('<td>0</td>'));assert.ok(!html.includes('id="policy-details-global"'));
assert.ok(html.includes('aria-label="Review global rules sample →"'));
state.operationDisclosures['policy-class:global']=true;
html=renderClassEvidence({data});
assert.ok(html.includes('id="policy-details-global"'));assert.ok(html.includes('2 uncertain · 3 unreviewed'));
assert.ok(html.includes('7 total attempts'));assert.ok(html.includes('Availability history unavailable'));
assert.ok(html.includes('No check time recorded'));assert.ok(html.includes('These thresholds are advisory'));
assert.ok(html.includes('&lt;invented&gt;'));assert.equal(JSON.stringify(data),before);
state.reviewDisclosures['quality-sample-history:newest']=false;
const samples={records:[],count:21,next_cursor:'older'};
assert.ok(!/<details[^>]* open/.test(renderQualitySamples({data:samples})));
assert.ok(/<details[^>]* open/.test(renderQualitySamples({data:samples,cursor:'older'})));
assert.ok(renderQualitySamples({data:samples}).includes('21 retained samples'));
// Refresh keeps previously read counts while pending and after a failed read.
globalThis.document={activeElement:{id:''},getElementById:()=>null};
state.route='overview';state.classEvidence={data};
let rejectRead,reads=0;
globalThis.fetch=()=>{reads++;return new Promise((resolve,reject)=>rejectRead=reject);};
const pending=loadClassEvidence();
assert.equal(state.classEvidence.data,data);assert.ok(state.classEvidence.loading);
assert.ok(/id="policy-switch-global"[^>]* disabled/.test(renderClassEvidence(state.classEvidence)));
await loadClassEvidence();assert.equal(reads,1);
rejectRead(new Error('Invented read failure'));await pending;
assert.equal(state.classEvidence.data,data);assert.equal(state.classEvidence.loading,false);
assert.ok(renderClassEvidence(state.classEvidence).includes('showing the previous class evidence'));
assert.ok(renderClassEvidence(state.classEvidence).includes('Invented read failure'));
assert.equal(classes[0].policy.enabled,false);assert.equal(JSON.stringify(data),before);
console.log(JSON.stringify({classes:4,sparse:'passed',refresh:'passed'}));
'''
    (tmp_path / 'check.mjs').write_text(script)
    result = subprocess.run([node, str(tmp_path / 'check.mjs')], cwd=tmp_path,
                            text=True, capture_output=True, check=True)
    assert json.loads(result.stdout) == {'classes':4, 'sparse':'passed', 'refresh':'passed'}
