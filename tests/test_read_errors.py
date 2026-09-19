"""Failed reads retain complete diagnostics without putting URLs in the alert."""
from pathlib import Path
import shutil
import subprocess

import pytest

from tests.spa_assets import copy_spa_dependencies


@pytest.mark.parametrize("surface", ["review", "project"])
def test_failed_read_explanation_and_complete_diagnostics(tmp_path, surface):
    node = shutil.which("node")
    assert node
    static = Path(__file__).resolve().parents[1] / "src/self_improve/dashboard/static"
    (tmp_path / "app.mjs").write_bytes((static / "app.js").read_bytes())
    copy_spa_dependencies(tmp_path)
    (tmp_path / "check.mjs").write_text(r'''
import assert from 'node:assert/strict';
import * as m from './app.mjs';
const surface=process.argv[2], cause='Invented <unavailable> read', requests=[];
globalThis.fetch=async url=>{requests.push(url);return {ok:false,status:503,statusText:'Unavailable',json:async()=>({detail:cause})};};
const family={learning_id:'lesson',proposals:Array.from({length:20},(_,i)=>({id:String(i).padStart(64,'a'),revision:'rev'})),target_rows:[]};
const key='remote:example.test/project',copy='b'.repeat(64);
m.state.projectInventorySelection[key]=copy;
const cacheKey=JSON.stringify([key,copy]);
let render,entry;
if(surface==='review') {
 await m.loadReviewPreview(family);entry=m.state.reviewPreviews.lesson;
 render=()=>m.renderSelectedPreview(family);
 for(const action of ['approve','reject','reject_lesson'])
   assert.match(m.renderReviewActions(family),new RegExp('data-decision="'+action+'"[^>]*disabled'));
} else {
 await m.loadProjectRuleSummary(key);entry=m.state.projectRuleSummaries[cacheKey];
 render=()=>m.renderProjectRuleSummary(key);
}
const html=render();
const alert=html.match(/<p[^>]*class="error-state"[^>]*>([\s\S]*?)<\/p>/)?.[1];
assert.ok(alert,'A failed read needs an actionable alert');
assert.doesNotMatch(alert,/GET \/api\//,'The initial alert must not expose the complete transport URL');
assert.match(alert,/Invented &lt;unavailable&gt; read/);
assert.match(html,/<p[^>]*role="alert"/);
const details=html.match(/<details[^>]*>[\s\S]*?<\/details>/)?.[0];
assert.ok(details);assert.doesNotMatch(details.split('>')[0],/\bopen\b/);
assert.match(details,/Request details/);assert.match(details,/GET \/api\//);
assert.match(details,/answered 503/);assert.match(details,/Invented &lt;unavailable&gt; read/);
assert.doesNotMatch(html,/<unavailable>/);
assert.equal(entry.error.includes(requests[0]),true,'Retain the original complete transport message');
if(surface==='review') {
 for(const p of family.proposals)assert.ok(details.includes(p.id));
 assert.match(html,/Reload Review/);
 entry.error='A proposal changed. Reload Review before deciding.';entry.errorDetail='';
 assert.match(render(),/A proposal changed/);
} else {
 const counts={matched:0,known_matches:0,retained:0,uncertain:0};
 entry.data={project_key:key,working_copy_id:copy,counts:{project:counts,global:counts},
   observation_times:['2030-01-02'],deliveries:{records:[],count:0},
   proposals:{records:[],count:0,unresolved_count:0},meaning:'Retained only'};
 assert.match(render(),/Last successful summary remains below/);
 assert.match(render(),/2030-01-02/);
 await m.loadProjectRuleSummary(key,{refresh:true});
 assert.match(render(),/2030-01-02/);
 globalThis.fetch=async()=>({ok:true,json:async()=>entry.data});
 await m.loadProjectRuleSummary(key,{refresh:true});
 assert.doesNotMatch(render(),/role="alert"|Request details|Invented/);
}
// Other consumers retain the original message and rejection semantics.
globalThis.fetch=async()=>({ok:false,status:502,statusText:'Bad gateway',json:async()=>{throw Error('not JSON')}});
await assert.rejects(m.getJSON('/api/other?exact=1'),{message:'GET /api/other?exact=1 answered 502  Bad gateway'});
console.log('READ_ERROR_OK '+surface);
''')
    result = subprocess.run([node, str(tmp_path / "check.mjs"), surface], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "READ_ERROR_OK " + surface
