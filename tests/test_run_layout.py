"""The Run summary must distinguish completed writes from missing evidence."""
from pathlib import Path
import shutil
import subprocess

from tests.spa_assets import copy_spa_dependencies


def test_delivery_summary_preserves_zero_unknown_and_partial_evidence(tmp_path):
    node=shutil.which('node')
    assert node, 'Node is required for the shipped Run renderer'
    static=Path(__file__).resolve().parents[1]/'src/self_improve/dashboard/static'
    (tmp_path/'app.mjs').write_bytes((static/'app.js').read_bytes())
    copy_spa_dependencies(tmp_path)
    (tmp_path/'check.mjs').write_text(r'''
import assert from 'node:assert/strict';
import {renderRunDetail} from './app.mjs';
const data={run:{id:'fixture',started:'2030-01-01T00:00:00Z',finished:'',status:'ok'},
 stats:{scan:{dropped_by_cap:{frustration:7}},apply:{applied:0}},stages:[],night_runs:[],
 calls:{recorded:0,reason:''},provenance_note:'Only exact links'};
const render=page=>renderRunDetail({kind:'run',id:'fixture',data,pages:page?{deliveries:page}:{}});
const ready={loaded:true,records:[],count:0,reason:'',error:'',loading:false};
const zero=render(ready);
assert.match(zero,/No automatic edits recorded/);
assert.ok(zero.indexOf('run-section-deliveries')<zero.indexOf('Call budgets and caps'));
assert.ok(zero.indexOf('Scan selection was capped')<zero.indexOf('run-section-deliveries'));
assert.match(zero,/frustration: 7/);
for(const page of [undefined,{...ready,loaded:false,loading:true},{...ready,reason:'No exact links retained'},
 {...ready,error:'Reader unavailable',loaded:false},{...ready,reason:'Totals do not reconcile'}]){
 const html=render(page);assert.doesNotMatch(html,/No automatic edits recorded/);
}
const failed=render({...ready,loaded:false,error:'Read failed'});
assert.match(failed,/Records not loaded/);
assert.doesNotMatch(failed,/Loading records/);
const loading=render({...ready,loaded:false,loading:true});
assert.equal((loading.match(/Loading records…/g)||[]).length,1);
const older=render({...ready,count:2,loading:true});
assert.match(older,/0 of 2 retained records shown/);
assert.equal((older.match(/Loading records…/g)||[]).length,1);
const delivery={id:'operation',state:'completed',proposal_id:'proposal',
 record:{destination:{target_path:'<private-looking fixture>'},proposal:{diff_unified:'<retained patch>'}},
 result:{snapshot_commit_before:'before',snapshot_commit_after:'after'}};
const partial=render({...ready,count:2,records:[delivery],reason:'Totals do not reconcile',next_cursor:'cursor'});
assert.match(partial,/2 automatic edits recorded/);
assert.match(partial,/Totals do not reconcile/);
assert.match(partial,/1 of 2 retained records shown/);
assert.match(partial,/&lt;retained patch&gt;/);assert.doesNotMatch(partial,/<retained patch>/);
assert.match(partial,/Snapshot before/);assert.match(partial,/Snapshot after/);
assert.match(partial,/#\/review\/rollback\/proposal/);assert.match(partial,/run-older-deliveries/);
assert.match(partial,/<details id="run-delivery-records"/);
assert.doesNotMatch(partial,/<details id="run-delivery-records"[^>]* open/);
console.log('RUN_DELIVERY_MEANINGS_OK');
''')
    result=subprocess.run([node,str(tmp_path/'check.mjs')],capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stderr
    assert result.stdout.strip()=='RUN_DELIVERY_MEANINGS_OK'
