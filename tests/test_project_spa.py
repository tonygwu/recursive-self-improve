"""Exercise actual project JavaScript over delayed responses and full pages."""
import shutil
import subprocess
from pathlib import Path

from self_improve.dashboard import app


def test_project_pagination_focus_and_late_responses(tmp_path):
    node = shutil.which('node'); assert node
    (tmp_path/'app.mjs').write_bytes((Path(app.__file__).parent/'static/app.js').read_bytes())
    probe = tmp_path/'probe.mjs'
    probe.write_text(r'''
import assert from 'node:assert/strict';
const m=await import('./app.mjs');
const nodes=new Map();
const body={_html:'',get innerHTML(){return this._html;},set innerHTML(value){this._html=value;document.activeElement=null;},querySelectorAll:()=>[]};
globalThis.document={activeElement:null,getElementById(id){
 if(id==='project-detail-body')return body;
 if(!body.innerHTML.includes('id="'+id+'"'))return null;
 if(!nodes.has(id))nodes.set(id,{id,focus(){document.activeElement=this;}});
 return nodes.get(id);
}};
const pending=[];
globalThis.fetch=url=>new Promise(resolve=>pending.push({url,resolve}));
const answer=(i,data)=>pending[i].resolve({ok:true,json:async()=>data});
const summary=key=>({project_key:key,label:key,displays:[],indexed:{known_session_ids:2,transcripts:4,unknown_session_ids:1},observed:{},indexed_paths:[],incidents:3,contributed_learnings:23,retained_deliveries:1});
m.state.inspectorKind='project';m.state.inspectorTab='summary';m.state.selectedProject='a';
const first=m.loadProjectDetail('a');
m.state.selectedProject='b';
const second=m.loadProjectDetail('b');
answer(1,summary('b'));await second;
const shown=body.innerHTML;
answer(0,summary('a'));await first;
assert.equal(body.innerHTML,shown);
assert.match(shown,/Known indexed sessions/);
assert.match(shown,/Historical revisions; availability is separate/);
const page=m.loadProjectRecords('contributed');
answer(2,{project_key:'b',kind:'contributed',records:[{id:'lesson-1',title:'<script>Fixture</script>',rule_text:'Retained text',project_incidents:23}],count:2,next_cursor:'next'});await page;
assert.match(body.innerHTML,/&lt;script&gt;/);
assert.doesNotMatch(body.innerHTML,/<script>/);
document.getElementById('project-older-contributed').focus();
const more=m.loadProjectRecords('contributed',{older:true});
assert.match(pending[3].url,/cursor=next/);
answer(3,{project_key:'b',kind:'contributed',records:[{id:'lesson-2',title:'Second',rule_text:'More text',project_incidents:1}],count:2,next_cursor:null});await more;
assert.equal(document.activeElement.id,'project-status-contributed');
assert.match(body.innerHTML,/2 shown · 2 retained/);
const evidence=m.loadProjectRecords('evidence',{learningId:'lesson-1'});
assert.match(pending[4].url,/learning_id=lesson-1/);
answer(4,{project_key:'b',kind:'evidence',learning_id:'lesson-1',records:[{id:'i6',matched_text:'Sixth retained incident',ts:'',session_id:'session-6',signal_type:'user_correction'}],count:1,next_cursor:null});await evidence;
assert.match(body.innerHTML,/Sixth retained incident/);
assert.match(body.innerHTML,/Time unknown/);
assert.match(body.innerHTML,/data-scan-history-root="i6"/);
const stale=m.loadProjectRecords('deliveries');
m.state.selectedProject='a';m.paintProjectDetail();
const different=body.innerHTML;
answer(5,{project_key:'b',kind:'deliveries',records:[],count:null,reason:'schema_unavailable'});await stale;
assert.equal(body.innerHTML,different);
m.state.selectedProject='b';m.paintProjectDetail();
assert.match(body.innerHTML,/predates retained delivery revisions/);
const wrong=m.loadProjectRecords('proposals');
answer(6,{project_key:'a',kind:'proposals',records:[],count:0});await wrong;
assert.match(body.innerHTML,/different selection/);
console.log('PROJECT_UI_OK');
''')
    result = subprocess.run([node, str(probe)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'PROJECT_UI_OK'
