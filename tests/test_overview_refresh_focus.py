"""The real Overview refresh listener respects later focus choices."""
from pathlib import Path
import shutil
import subprocess

import pytest

from tests.spa_assets import copy_spa_dependencies

SETUP = r'''
import assert from 'node:assert/strict';
import * as app from './app.mjs';
const elements=new Map(),listeners=new Map();
class Element {
 constructor(id){this.id=id;this.innerHTML='';this.textContent='';this.style={};this.attrs={};this.listeners=new Map();this.classList={add(){},remove(){},toggle(){}};}
 setAttribute(k,v){this.attrs[k]=String(v);} getAttribute(k){return this.attrs[k]??null;}
 removeAttribute(k){delete this.attrs[k];}
 querySelectorAll(){return [];} querySelector(){return null;} closest(){return null;}
 contains(el){return el===this;}
 addEventListener(type,fn){this.listeners.set(type,fn);}
 focus(){document.activeElement=this;for(const fn of listeners.get('focusin')||[])fn({target:this});}
 set disabled(value){this._disabled=value;if(value&&document.activeElement===this)document.activeElement=document.body;}
 get disabled(){return Boolean(this._disabled);}
}
globalThis.document={activeElement:null,body:new Element('body'),getElementById(id){if(!elements.has(id))elements.set(id,new Element(id));return elements.get(id);},querySelectorAll(){return [];},querySelector(){return null;},
 addEventListener(type,fn){if(!listeners.has(type))listeners.set(type,new Set());listeners.get(type).add(fn);},
 removeEventListener(type,fn){listeners.get(type)?.delete(fn);}};
globalThis.location={hash:'#/overview'};
app.state.route='overview';app.wire();
const button=document.getElementById('ov-refresh'),other=document.getElementById('ov-grid-numbers');
const click=button.listeners.get('click');
let release,failed=false,calls=0;
const held=new Promise(r=>release=r);
const response=(body,status=200)=>({ok:status<400,status,json:async()=>body});
globalThis.fetch=async(url,options)=>{
 assert.equal(options.method,'GET');
 if(url===app.API.overview){calls++;await held;return failed?response({detail:'Invented read failure'},503):response({grid:{},runs:[],learnings:[],proposals:[]});}
 if(url===app.API.review)return response({families:[],count:0});
 if(url===app.API.projects)return response({rows:[],count:0,unscoped:{}});
 return response({rules:[],learnings:[],proposals:[],pagination:{count:0}});
};
button.focus();
'''

CASES = {
    'unmoved_success': "const task=click();assert.ok(button.disabled);release();await task;assert.equal(document.activeElement,button);",
    'unmoved_failure': "failed=true;const task=click();release();await task;assert.equal(document.activeElement,button);assert.match(document.getElementById('global-error').innerHTML,/Invented read failure/);",
    'moved_success': "const task=click();other.focus();release();await task;assert.equal(document.activeElement,other);",
    'moved_failure': "failed=true;const task=click();other.focus();release();await task;assert.equal(document.activeElement,other);",
    'moved_then_blurred': "const task=click();other.focus();document.activeElement=document.body;release();await task;assert.equal(document.activeElement,document.body,'Completion must not infer ownership after another control lost focus');",
    'route_changed': "const task=click();app.state.route='rules';release();await task;assert.equal(document.activeElement,document.body);",
    'not_initiator': "other.focus();const task=click();release();await task;assert.equal(document.activeElement,other);",
    'repeat_while_pending': "const task=click();const duplicate=click();release();await Promise.all([task,duplicate]);assert.equal(calls,1,'Disabled Refresh must not start a second load');",
}


@pytest.mark.parametrize('case', CASES)
def test_overview_refresh_focus(tmp_path, case):
    node = shutil.which('node')
    assert node
    static = Path(__file__).resolve().parents[1] / 'src/self_improve/dashboard/static'
    (tmp_path / 'app.mjs').write_bytes((static / 'app.js').read_bytes())
    copy_spa_dependencies(tmp_path)
    (tmp_path / 'check.mjs').write_text(SETUP + CASES[case] + r'''
assert.equal(button.disabled,false);
assert.equal(listeners.get('focusin')?.size || 0,0,'Refresh must remove its transient focus listener');
console.log('OVERVIEW_REFRESH_FOCUS_OK');
''')
    result = subprocess.run([node, str(tmp_path / 'check.mjs')], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == 'OVERVIEW_REFRESH_FOCUS_OK'
