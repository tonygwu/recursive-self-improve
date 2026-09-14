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
globalThis.fetch = url => new Promise(resolve=>pending.push({url,resolve}));
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
assert.match(shown,/Budget limits were not recorded/);
assert.match(shown,/refused: 2/);
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
for(const kind of vocabulary.kinds){
 const index = pending.length;
 const request = m.loadRunRecords(kind);
 assert.equal(pending.length,index+1,'backend record kind is unreachable: '+kind);
 answer(index,{run_id:'a',kind,records:[],count:0,next_cursor:null}); await request;
 assert.equal(m.state.runDetail.pages[kind].error,'');
}
console.log('RUN_ROUTES_OK');
''')
    result = subprocess.run([node, str(probe)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "RUN_ROUTES_OK"
