"""Review error recovery uses real command/read handlers and isolated invented state."""
from pathlib import Path
import shutil
import subprocess

import pytest

from tests.spa_assets import copy_spa_dependencies

SETUP = r'''
import assert from 'node:assert/strict';
import * as m from './app.mjs';
const elements=new Map();
class Element {
 constructor(id){this.id=id;this.innerHTML='';this.textContent='';this.style={};this.attrs={};this.classList={add(){},remove(){},toggle(){}};}
 setAttribute(k,v){this.attrs[k]=String(v);} getAttribute(k){return this.attrs[k]??null;}
 removeAttribute(k){delete this.attrs[k];}
 querySelectorAll(){return [];} querySelector(){return null;} closest(){return null;}
 contains(el){return el===this;} focus(){document.activeElement=this;}
}
globalThis.document={activeElement:null,getElementById(id){if(!elements.has(id))elements.set(id,new Element(id));return elements.get(id);},querySelectorAll(){return [];},querySelector(){return null;}};
globalThis.location={hash:'#/review'};
const alert=()=>document.getElementById('global-error'), html=()=>alert().innerHTML;
const make=(id,n)=>({learning_id:id,rule_text:'Invented '+id,proposals:Array.from({length:n},(_,i)=>({id:id+i,content_revision:'content-1',revision:'auth-1',reason_code:'pending'})),target_rows:[],targets:[]});
const A=make('A',2),B=make('B',1),queue={families:[A,B],count:3};
m.state.review=queue;m.state.route='overview';m.state.selectedFamily='A';
const posts=[];let mode='refused',hold=null,failRead=false;
const response=(body,status=200)=>({ok:status<400,status,statusText:'Invented unavailable',json:async()=>body});
globalThis.fetch=async(url,options)=>{
 if(options.method==='POST') {
  const body=JSON.parse(options.body);posts.push(body);
  if(hold)await hold;
  if(mode==='lost')throw Error('Invented lost response');
  if(mode==='refused')return response({detail:'Invented <stale> command'},409);
  return response({id:'command-'+posts.length,action:body.action,state:'completed'});
 }
 if(url.startsWith(m.API.reviewPreview)) {
  const ids=new URL(url,'http://fixture').searchParams.get('proposal_ids').split(',');
  return response({ready:true,revision:'preview',targets:[],members:ids.map(id=>{const p=[...A.proposals,...B.proposals].find(p=>p.id===id);return {proposal_id:id,content_revision:p.content_revision,revision:p.revision,snapshot:{evidence:[]}};})});
 }
 if(url===m.API.review)return failRead ? response({detail:'Invented queue read failed'},503) : response(queue);
 if(url.startsWith(m.API.commands))return response({commands:[],next_cursor:null});
 if(url===m.API.overview)return response({grid:{},runs:[],learnings:[],proposals:[]});
 if(url===m.API.projects)return response({rows:[],count:0,unscoped:{}});
 return response({rules:[],learnings:[],proposals:[],pagination:{count:0}});
};
'''

CASES = {
    'confirmed_retry': r'''
const refused=await m.decideFamily('A','reject');
assert.equal(refused.failed.length,2);assert.match(html(),/Invented/);
const first=posts[0].request_key;mode='success';
assert.equal((await m.decideFamily('A','reject')).ok,2);
assert.equal(posts[1].request_key,first,'Explicit retry must retain its exact request identity');
assert.equal(alert().hidden,true,'Confirmed retry retained the stale command alert');
''',
    'competing_failures': r'''
await m.decideFamily('A','reject');await m.decideFamily('B','reject');
assert.match(html(),/&quot;A0&quot;/);assert.match(html(),/&quot;B0&quot;/);
mode='success';let release;hold=new Promise(r=>release=r);
const pending=m.decideFamily('A','reject');
while(posts.length<3)await new Promise(r=>setImmediate(r));
m.showError('Unrelated read failed','Invented endpoint',{owner:'dashboard-read',retry:true});
release();await pending;
assert.match(html(),/&quot;B0&quot;/);assert.match(html(),/Unrelated read failed/);assert.doesNotMatch(html(),/&quot;A0&quot;/);
''',
    'uncertain_request_identity': r'''
mode='lost';await m.decideFamily('A','reject');const old=posts[0].request_key;
mode='success';m.state.reviewExcluded.A1=true;
await m.decideFamily('A','reject');
assert.notEqual(posts[1].request_key,old);assert.match(html(),/Invented lost response/,'Different selection cannot resolve the older uncertain command');
delete m.state.reviewExcluded.A1;
await m.decideFamily('A','reject');
assert.equal(posts[2].request_key,old,'Returning to the uncertain request must recover its original key');
assert.equal(alert().hidden,true);
''',
    'refused_replacement_revision': r'''
await m.decideFamily('A','reject');const old=posts[0].request_key;
A.proposals.forEach(p=>{p.content_revision='content-2';p.revision='auth-2';});
mode='success';await m.decideFamily('A','reject');
assert.notEqual(posts[1].request_key,old);assert.ok(posts[1].members.every(m=>m.revision==='auth-2'));
assert.equal(alert().hidden,true,'Explicit replacement of a known refusal still shows the old failure');
''',
    'read_recovery_and_outcomes': r'''
await m.decideFamily('B','reject');mode='success';failRead=true;
await m.decideFamily('A','approve');
assert.equal(m.state.lastCommand.action,'approve');assert.match(html(),/recorded/i);assert.match(html(),/could not.*refresh/i);assert.match(html(),/&quot;B0&quot;/);
failRead=false;await m.refreshReviewSnapshot();
assert.match(html(),/&quot;B0&quot;/);assert.doesNotMatch(html(),/Invented queue read failed/);
m.showError('Dashboard read failed','Invented dashboard read',{owner:'dashboard-read',retry:true});
await m.load();
assert.match(html(),/&quot;B0&quot;/);assert.doesNotMatch(html(),/Invented dashboard read/,'Successful reads must clear only their own failure');
''',
    'later_error_and_focus': r'''
const old=m.showError('Earlier read','old',{owner:'dashboard-read'});
m.showError('Later read','new',{owner:'dashboard-read'});
m.clearError('dashboard-read',old);
assert.match(html(),/Later read/,'An older result erased a later failure');
alert().focus();m.clearError('dashboard-read');
assert.equal(document.activeElement.id,'main');assert.equal(alert().hidden,true);
''',
    'late_queue_read': r'''
const original=globalThis.fetch;let release,entered;
const started=new Promise(r=>entered=r);
globalThis.fetch=async(url,options)=>{if(url===m.API.review){entered();await new Promise(r=>release=r);}return original(url,options);};
const pending=m.refreshReviewSnapshot();await started;
m.showError('Newer queue failure','Keep this later failure',{owner:'review-refresh:new',kind:'review-read',retry:true});
release();assert.equal(await pending,true);assert.match(html(),/Keep this later failure/);
globalThis.fetch=original;await m.refreshReviewSnapshot();assert.equal(alert().hidden,true);
''',
    'atomic_diagnostics': r'''
await m.decideFamily('A','reject');
assert.equal((html().match(/Invented &lt;stale&gt; command/g)||[]).length,1,'One atomic refusal is repeated for each selected member');
assert.match(html(),/<details/);assert.match(html(),/&quot;A0&quot;/);assert.match(html(),/&quot;A1&quot;/);assert.match(html(),/2 of 2/);assert.doesNotMatch(html(),/<stale>/);
assert.match(html(),/retry/i);
''',
}


@pytest.mark.parametrize('case', CASES)
def test_decision_error_recovery(tmp_path, case):
    node = shutil.which('node')
    assert node
    static = Path(__file__).resolve().parents[1] / 'src/self_improve/dashboard/static'
    (tmp_path / 'app.mjs').write_bytes((static / 'app.js').read_bytes())
    copy_spa_dependencies(tmp_path)
    (tmp_path / 'check.mjs').write_text(SETUP + CASES[case] + "\nconsole.log('REVIEW_ERROR_OK');\n")
    result = subprocess.run([node, str(tmp_path / 'check.mjs')], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == 'REVIEW_ERROR_OK'
