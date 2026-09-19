"""Exercise actual project JavaScript over delayed responses and full pages."""
import shutil
import subprocess
from pathlib import Path

from self_improve.dashboard import app


def test_project_pagination_focus_and_late_responses(tmp_path):
    node = shutil.which('node'); assert node
    (tmp_path/'app.mjs').write_bytes((Path(app.__file__).parent/'static/app.js').read_bytes())
    from tests.spa_assets import copy_spa_dependencies
    copy_spa_dependencies(tmp_path)
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
// Exercise the actual delegated click path, using the emitted button attributes.
const button=body.innerHTML.match(/<button[^>]*id="project-load-evidence:lesson-1"[^>]*>/)[0];
const attrs=Object.fromEntries([...button.matchAll(/([\w-]+)="([^"]*)"/g)].map(v=>[v[1],v[2]]));
const target={getAttribute:k=>attrs[k]??null,closest(selector){return selector.startsWith('[') && Object.hasOwn(attrs,selector.slice(1,-1)) ? this : null;}};
m.handleMainClick({target});
assert.match(pending[7].url,/api\/project-records\?.*kind=evidence/);
answer(7,{project_key:'b',kind:'evidence',learning_id:'lesson-1',records:[],count:0,next_cursor:null});
await new Promise(resolve=>setTimeout(resolve,0));
assert.match(body.innerHTML,/No linked records/);
console.log('PROJECT_UI_OK');
''')
    result = subprocess.run([node, str(probe)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'PROJECT_UI_OK'


def test_context_history_selection_paging_and_late_response(tmp_path):
    node=shutil.which('node');assert node
    (tmp_path/'app.mjs').write_bytes((Path(app.__file__).parent/'static/app.js').read_bytes())
    from tests.spa_assets import copy_spa_dependencies
    copy_spa_dependencies(tmp_path)
    probe=tmp_path/'context.mjs'
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
const pending=[];globalThis.fetch=url=>new Promise(resolve=>pending.push({url,resolve}));
const answer=(i,data)=>pending[i].resolve({ok:true,json:async()=>data});
const inventory=id=>({id,working_copy_id:id,working_copy:{normalized_path:'/fixture/'+id},observed_at:'2030-01-02',status:'recorded',files:[],issues:[],deduplicated:[],unobserved_sources:[],profile:'instruction-surfaces/1'});
m.state.inspectorKind='project';m.state.inspectorTab='context';m.state.selectedProject='p';
m.state.projectInventory.p={loaded:true,records:[inventory('a'),inventory('b')],count:2};
m.state.projectInventorySelection.p='a';
const first=m.loadProjectContextHistory('p');
assert.match(pending[0].url,/working_copy_id=a/);
m.state.projectInventorySelection.p='b';m.paintProjectDetail();
const second=m.loadProjectContextHistory('p');
answer(1,{project_key:'p',working_copy_id:'b',records:[],count:0,next_cursor:null,reason:'no_inventory_observations'});await second;
const selected=body.innerHTML;
answer(0,{project_key:'p',working_copy_id:'a',records:[],count:23,next_cursor:'next'});await first;
assert.equal(body.innerHTML,selected);
m.state.projectInventorySelection.p='a';m.paintProjectDetail();
assert.match(body.innerHTML,/23 observation times/);
document.getElementById('project-context-older').focus();
const more=m.loadProjectContextHistory('p',{older:true});assert.match(pending[2].url,/cursor=next/);
answer(2,{project_key:'p',working_copy_id:'a',records:[],count:23,next_cursor:null});await more;
assert.equal(document.activeElement.id,'project-context-status');
const wrong=m.loadProjectContextHistory('p',{refresh:true});
answer(3,{project_key:'other',working_copy_id:'a',records:[]});await wrong;
assert.match(body.innerHTML,/different selection/);
assert.match(body.innerHTML,/Refresh context history/);
console.log('CONTEXT_HISTORY_SPA_OK');
''')
    result=subprocess.run([node,str(probe)],capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'CONTEXT_HISTORY_SPA_OK' in result.stdout


def test_exact_copy_read_never_falls_back_or_paints_another_selection(tmp_path):
    node=shutil.which('node');assert node
    (tmp_path/'app.mjs').write_bytes((Path(app.__file__).parent/'static/app.js').read_bytes())
    from tests.spa_assets import copy_spa_dependencies
    copy_spa_dependencies(tmp_path)
    probe=tmp_path/'copy.mjs'
    probe.write_text(r'''
import assert from 'node:assert/strict';
const m=await import('./app.mjs');
const body={innerHTML:'',querySelectorAll:()=>[]};
globalThis.document={activeElement:null,getElementById:id=>id==='project-detail-body'?body:null};
const pending=[];globalThis.fetch=url=>new Promise(resolve=>pending.push({url,resolve}));
const answer=(i,data)=>pending[i].resolve({ok:true,json:async()=>data});
const inventory=id=>({id,working_copy_id:id,working_copy:{normalized_path:'/fixture/'+id},observed_at:'2030-01-02',status:'recorded',files:[],issues:[],deduplicated:[],unobserved_sources:[],profile:'instruction-surfaces/1'});
const key='remote:fixture/project?name=<escaped>&';
m.state.inspectorKind='project';m.state.inspectorTab='context';m.state.selectedProject=key;
m.state.projectInventory[key]={loaded:true,records:[inventory('a'),inventory('b')],count:3,next_cursor:'more'};
m.state.projectInventorySelection[key]='c';m.paintProjectDetail();
assert.doesNotMatch(body.innerHTML,/Observed 2030|Context history/);
assert.match(body.innerHTML,/requested working copy has not been read/);
const first=m.loadProjectInventoryCopy(key);
const query=new URLSearchParams(pending[0].url.split('?')[1]);
assert.equal(query.get('project_key'),key);assert.equal(query.get('working_copy_id'),'c');assert.equal(query.get('limit'),'1');
assert.match(body.innerHTML,/Reading requested working copy/);
m.state.projectInventorySelection[key]='b';m.paintProjectDetail();const shown=body.innerHTML;
answer(0,{project_key:key,working_copy_id:'c',records:[inventory('c')],count:1});await first;
assert.equal(body.innerHTML,shown);
m.state.projectInventorySelection[key]='c';m.paintProjectDetail();
assert.match(body.innerHTML,/value="c" selected/);assert.match(body.innerHTML,/3 of 3 copy inventories loaded/);
assert.equal(m.state.projectInventory[key].records.length,2);assert.equal(m.state.projectInventory[key].next_cursor,'more');
for(const [,href] of m.renderProjectNavigation(key,'context').matchAll(/href="([^"]+)"/g)) {
 const route=m.parseRoute(href.replaceAll('&amp;','&'));assert.equal(route.id,key);
 assert.equal(new URLSearchParams(route.projectQuery).get('working_copy_id'),'c');
}
m.state.projectInventorySelection[key]='missing';const missing=m.loadProjectInventoryCopy(key);
answer(1,{project_key:key,working_copy_id:'missing',records:[],count:0});await missing;
assert.match(body.innerHTML,/No inventory is retained for the requested working copy/);
assert.doesNotMatch(body.innerHTML,/Context history/);
const wrong=m.loadProjectInventoryCopy(key,{refresh:true});
answer(2,{project_key:key,working_copy_id:'missing',records:[inventory('b')],count:1});await wrong;
assert.match(body.innerHTML,/different working copy/);assert.doesNotMatch(body.innerHTML,/Context history/);
m.state.projectInventorySelection[key]='<invalid>';m.paintProjectDetail();
assert.ok(!body.innerHTML.includes('<invalid>'));assert.match(body.innerHTML,/&lt;invalid&gt;/);
// An explicit all-copy filter must stay unfiltered even when inventories are cached.
m.state.projectInventorySelection[key]='';
for(const tab of ['sessions','loads','exposure','context']) {
 assert.ok(!new URLSearchParams(m.projectReadHref(key,tab).split('?')[1]).has('working_copy_id'));
}
console.log('EXACT_COPY_OK');
''')
    result=subprocess.run([node,str(probe)],capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'EXACT_COPY_OK' in result.stdout


def test_project_rule_summary_keeps_exact_copy_and_failed_refresh(tmp_path):
    node=shutil.which('node');assert node
    (tmp_path/'app.mjs').write_bytes((Path(app.__file__).parent/'static/app.js').read_bytes())
    from tests.spa_assets import copy_spa_dependencies
    copy_spa_dependencies(tmp_path)
    probe=tmp_path/'summary.mjs'
    probe.write_text(r'''
import assert from 'node:assert/strict';
const m=await import('./app.mjs');
const body={innerHTML:'',querySelectorAll:()=>[]};
globalThis.document={activeElement:null,getElementById:id=>id==='project-detail-body'?body:null};
const pending=[];globalThis.fetch=url=>new Promise(resolve=>pending.push({url,resolve}));
const key='project';const a='a'.repeat(64),b='b'.repeat(64);
m.state.inspectorKind='project';m.state.inspectorTab='summary';m.state.selectedProject=key;
m.state.projectInventorySelection[key]=a;
const counts={project:{matched:1,known_matches:1,retained:1,uncertain:0},global:{matched:0,known_matches:0,retained:0,uncertain:0}};
const data=copy=>({project_key:key,working_copy_id:copy,counts,observation_times:['2030-01-02'],deliveries:{records:[],count:0,omitted:0},proposals:{records:[],count:0,omitted:0,unresolved_count:0},meaning:'Retained only'});
const answer=(index,value,status=200)=>pending[index].resolve({ok:status===200,status,json:async()=>value});
const first=m.loadProjectRuleSummary(key);assert.match(pending[0].url,new RegExp('working_copy_id='+a));
m.state.projectInventorySelection[key]=b;const second=m.loadProjectRuleSummary(key);
answer(1,data(b));await second;const shown=m.renderProjectRuleSummary(key);
assert.match(shown,/Complete project lists remain below/);
assert.match(shown,/All project and global copy observations/);
answer(0,data(a));await first;assert.equal(m.renderProjectRuleSummary(key),shown);
const failed=m.loadProjectRuleSummary(key,{refresh:true});answer(2,{detail:'Invented unavailable read'},503);await failed;
assert.match(m.renderProjectRuleSummary(key),/Invented unavailable read/);
assert.match(m.renderProjectRuleSummary(key),/2030-01-02/);
const wrong=m.loadProjectRuleSummary(key,{refresh:true});answer(3,data(a));await wrong;
assert.match(m.renderProjectRuleSummary(key),/different selection/);
assert.match(m.renderProjectRuleSummary(key),/2030-01-02/);
const unavailable=data(b);unavailable.counts={project:{...counts.project,retained:null,matched:null},global:{...counts.global,retained:null,matched:null}};
const unknown=m.loadProjectRuleSummary(key,{refresh:true});answer(4,unavailable);await unknown;
assert.match(m.renderProjectRuleSummary(key),/Global delivery history unknown/);
console.log('PROJECT_RULE_SUMMARY_SPA_OK');
''')
    result=subprocess.run([node,str(probe)],capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stderr
    assert 'PROJECT_RULE_SUMMARY_SPA_OK' in result.stdout
