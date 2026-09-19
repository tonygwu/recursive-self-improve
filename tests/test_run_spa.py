"""Exercise the shipped JavaScript across same-time runs and delayed responses."""
import shutil
import subprocess
import json
from pathlib import Path

from self_improve.dashboard import app
from self_improve.dashboard.queries import STAGES
from self_improve.dashboard.run_data import KINDS


def test_run_routing_pagination_and_late_responses(tmp_path):
    node = shutil.which("node")
    assert node, "Node is required for dashboard behavior checks."
    source = Path(app.__file__).parent / "static" / "app.js"
    (tmp_path / "app.mjs").write_bytes(source.read_bytes())
    from tests.spa_assets import copy_spa_dependencies
    copy_spa_dependencies(tmp_path)
    (tmp_path / "vocabulary.json").write_text(json.dumps({'stages':STAGES,'kinds':KINDS}))
    probe = tmp_path / "probe.mjs"
    probe.write_text(r'''
import assert from 'node:assert/strict';
import fs from 'node:fs';
const vocabulary = JSON.parse(fs.readFileSync(new URL('./vocabulary.json',import.meta.url),'utf8'));
const m = await import('./app.mjs');
const nodes = new Map();
const body = {_html:'', get innerHTML(){return this._html;},
 set innerHTML(value){this._html=value; document.activeElement=null;}, querySelectorAll:()=>[]};
globalThis.document = {activeElement:null, getElementById(id){
 if(id==='run-detail')return body;
 if(!body.innerHTML.includes('id="'+id+'"'))return null;
 if(!nodes.has(id))nodes.set(id,{id,focus(){document.activeElement=this;}});
 return nodes.get(id);
}};
const pending = [];
let automaticDeliveryReads=true;
const deliveryReads=[];
globalThis.fetch = url => {
 if(url.endsWith('/backlog')){
  const id=decodeURIComponent(url.split('/runs/')[1].split('/')[0]);
  return Promise.resolve({ok:true,json:async()=>({run_id:id,profile:'run-backlog/1',snapshot:null,queue_count:null,reasons:['No retained snapshot']})});
 }
 if(url.includes('/related-deliveries?')){
  const id=decodeURIComponent(url.split('/runs/')[1].split('/')[0]);
  return Promise.resolve({ok:true,json:async()=>({run_id:id,kind:'related_deliveries',records:[],count:null,reason:'No retained observations',next_cursor:null})});
 }
 if(automaticDeliveryReads && url.includes('/records?kind=deliveries')){
  deliveryReads.push(url);
  const id=decodeURIComponent(url.split('/runs/')[1].split('/')[0]);
  return Promise.resolve({ok:true,json:async()=>({run_id:id,kind:'deliveries',records:[],count:0,reason:'No exact links retained',next_cursor:null})});
 }
 return new Promise(resolve=>pending.push({url,resolve}));
};
const answer = (i, data) => pending[i].resolve({ok:true,json:async()=>data});
const response = id => ({run:{id,started:'2030-01-02T02:00:00Z',finished:'',status:'running'},
 stats:{},stages:[{name:'scan',recorded:false,payload:null},{name:'gate',recorded:true,payload:{attempted:3,gated_pass:1,refused:2}}],
 night_runs:['a','b'].map(id=>({id,started:'2030-01-02T02:00:00Z',status:'running'})),
 calls:{recorded:1,reported:1,tokens_in:3,tokens_out:4,reason:''},budget_limits:null,provenance_note:'Exact links'});
const one = m.openRunRoute('run/a/gate');
const two = m.openRunRoute('run/b/gate');
answer(1,response('b')); await two;
const shown = body.innerHTML;
answer(0,response('a')); await one;
assert.equal(body.innerHTML,shown);
assert.equal(m.state.runDetail.id,'b');
assert.equal(deliveryReads.length,1);assert.match(deliveryReads[0],/\/runs\/b\/records/);
assert.match(shown,/Budget limits were not recorded/);
// This old/incomplete response has no normalized accounting. Retain its raw
// refusal count without presenting an inferred zero or native-unit total.
assert.match(shown,/&quot;refused&quot;: 2/);
assert.match(shown,/<td class="run-count" data-run-count="input"><span class="run-count-label">unknown<\/span><\/td>/);
assert.match(shown,/Not recorded/);
assert.doesNotMatch(shown,/succeeded: 1/);
const calls = m.loadRunRecords('calls');
const next = m.openRunRoute('run/a');
answer(3,response('a')); await next;
answer(2,{run_id:'b',kind:'calls',records:[{id:'b-call',stage:'grade',outcome:'ok'}],count:1,next_cursor:null});
await calls;
assert.doesNotMatch(body.innerHTML,/b-call/);
const first = m.loadRunRecords('calls');
answer(4,{run_id:'a',kind:'calls',records:[{id:'a1',stage:'grade',outcome:'ok'}],count:2,next_cursor:'page2'}); await first;
document.getElementById('run-older-calls').focus();
const more = m.loadRunRecords('calls',true);
assert.match(pending[5].url,/cursor=page2/);
answer(5,{run_id:'a',kind:'calls',records:[{id:'a2',stage:'grade',outcome:'ok'}],count:2,next_cursor:null}); await more;
assert.deepEqual(m.state.runDetail.pages.calls.records.map(r=>r.id),['a1','a2']);
assert.equal(document.activeElement.id,'run-status-calls');
assert.match(body.innerHTML,/2 of 2 retained/);
const night = m.openRunRoute('night/2030-01-02/gate');
answer(6,{count:2,records:response('a').night_runs}); await night;
assert.match(body.innerHTML,/#\/overview\/run\/a\/gate/);
assert.match(body.innerHTML,/#\/overview\/run\/b\/gate/);
assert.equal(m.state.runDetail.data,null);
const grid = m.renderGrid({stages:['gate'],columns:[{night:'2030-01-02',run_count:2,run_ids:['a','b'],cells:{gate:{state:'ok',runs:2}}}]});
assert.match(grid,/#\/overview\/night\/2030-01-02\/gate/);
assert.match(grid,/open run detail/);
// Drive every backend stage and record kind through the frontend boundary.
for(const stage of vocabulary.stages){
 const index = pending.length;
 const request = m.openRunRoute('run/a/'+stage);
 assert.equal(pending.length,index+1,'backend stage is unreachable: '+stage);
 answer(index,response('a')); await request;
 assert.equal(m.state.runDetail.error,'');
}
automaticDeliveryReads=false;
for(const kind of vocabulary.kinds){
 const index = pending.length;
 const request = m.loadRunRecords(kind);
 assert.equal(pending.length,index+1,'backend record kind is unreachable: '+kind);
 answer(index,{run_id:'a',kind,records:[],count:0,next_cursor:null}); await request;
 assert.equal(m.state.runDetail.pages[kind].error,'');
}
automaticDeliveryReads=true;
// A successful file inspection replaces stale missing-file metadata.
let idx=pending.length;
const artifactPage=m.loadRunRecords('artifacts');
answer(idx,{run_id:'a',records:[{key:'report',label:'Run report',state:'ArtifactMissing',reason:'File was missing',bytes:null}],count:1,next_cursor:null});await artifactPage;
idx=pending.length;const inspect=m.inspectRunArtifact('report');
answer(idx,{run_id:'a',key:'report',bytes:8,preview_bytes:8,omitted_bytes:0,encoding:'utf-8',version:'v1',text:'<script>'});await inspect;
assert.doesNotMatch(body.innerHTML,/File was missing/);
assert.match(body.innerHTML,/8 bytes in the selected bundle/);
assert.match(body.innerHTML,/&lt;script&gt;/);
assert.doesNotMatch(body.innerHTML,/<script>/);
assert.equal(document.activeElement.id,'artifact-result-report');
// An unrelated response preserves focused artifact text.
document.getElementById('artifact-text-report').focus();
idx=pending.length;const unrelated=m.loadRunRecords('calls');
answer(idx,{run_id:'a',kind:'calls',records:[],count:0,next_cursor:null});await unrelated;
assert.equal(document.activeElement.id,'artifact-text-report');
// An artifact response from the old run cannot overwrite the new route.
idx=pending.length;const late=m.inspectRunArtifact('report');
const changed=m.openRunRoute('run/b');answer(idx+1,response('b'));await changed;
answer(idx,{run_id:'a',key:'report',bytes:7,preview_bytes:7,omitted_bytes:0,text:'old run'});await late;
assert.equal(m.state.runDetail.id,'b');assert.doesNotMatch(body.innerHTML,/old run/);
const noCalls=response('b');noCalls.calls.recorded=0;
assert.match(m.renderRunDetail({kind:'run',id:'b',data:noCalls,pages:{}}),/Token usage not recorded/);
console.log('RUN_ROUTES_OK');
''')
    result = subprocess.run([node, str(probe)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "RUN_ROUTES_OK"
