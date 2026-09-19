"""Project graphics must use recorded byte units and preserve missing evidence."""
from pathlib import Path
import shutil
import subprocess


def test_project_graphics_keep_units_coverage_and_reasons(tmp_path):
    from tests.spa_assets import copy_spa_dependencies
    node=shutil.which('node');assert node
    static=Path(__file__).resolve().parents[1]/'src/self_improve/dashboard/static'
    (tmp_path/'app.mjs').write_bytes((static/'app.js').read_bytes());copy_spa_dependencies(tmp_path)
    (tmp_path/'check.mjs').write_text(r'''
import assert from 'node:assert/strict';
import * as m from './app.mjs';
const file={bytes:100,lines:10,ownership:{human_managed:{bytes:10,lines:7},machine:{bytes:90,lines:3},edited:{bytes:0,lines:0},unknown:{bytes:0,lines:0}}};
const bar=m.renderInstructionOwnership(file);
assert.match(bar,/data-owner="human_managed" style="width:10%"/);
assert.match(bar,/data-owner="machine" style="width:90%"/);
assert.match(bar,/10 bytes · 7 lines/);assert.match(bar,/90 bytes · 3 lines/);
assert.match(bar,/0 bytes · 0 lines/);assert.match(bar,/Left-to-right byte shares/);
assert.doesNotMatch(bar,/aria-label="[^\"]*lines/);
for(const bad of [{...file,bytes:null},{...file,bytes:99},{...file,lines:11},{...file,lines:null},{...file,ownership:{}},{...file,ownership:{...file.ownership,machine:{bytes:90}}},{...file,ownership:{...file.ownership,machine:{bytes:-1,lines:3}}}]){
 const html=m.renderInstructionOwnership(bad);assert.match(html,/Ownership shares unavailable/);assert.doesNotMatch(html,/project-ownership-bar/);
}
const zero=m.renderInstructionOwnership({bytes:0,lines:0,ownership:Object.fromEntries(Object.keys(file.ownership).map(k=>[k,{bytes:0,lines:0}]))});
assert.match(zero,/Empty source/);assert.doesNotMatch(zero,/project-ownership-bar/);
const measurement={instructions:{status:'partial',files:Array.from({length:7},(_,i)=>({real_path:'/repo/'+String.fromCharCode(97+i)+'/AGENTS.md',bytes:i===6?1000:i*10}))},other_markdown:{root:'/repo',status:'unavailable',files:[]}};
const locations=m.renderProjectLocations(measurement,'/repo');
assert.match(locations,/Largest recorded group: 1,000 bytes/);
assert.equal((locations.match(/data-location=/g)||[]).length,6);
assert.match(locations,/width:0%/);assert.match(locations,/width:5%/);
assert.match(locations,/Instructions: partial/);assert.match(locations,/Other Markdown: unavailable/);
assert.match(m.renderProjectLocations(null,'/repo'),/unavailable/);
assert.match(m.renderProjectLocations({...measurement,instructions:{files:[{real_path:'/repo/a',bytes:null}]}},'/repo'),/unavailable/);
const escaped=m.renderProjectLocations({instructions:{status:'partial',files:[{real_path:'/repo/<unsafe>/AGENTS.md',bytes:10},{real_path:'managed-text:field-a',bytes:20}]},other_markdown:{root:'/repo',status:'unavailable',files:[]}},'/repo');
assert.match(escaped,/&lt;unsafe&gt;/);assert.doesNotMatch(escaped,/<unsafe>/);assert.match(escaped,/External \/ global/);
const row={project_key:'repo:<unsafe>',benefit:{computable:false,value:'—',reason:'Unknown <cause>',meaning:'Retained <meaning>'}};
const missing=m.renderProjectBenefit(row);
assert.match(missing,/<details/);assert.match(missing,/<summary[^>]*>[^]*—[^]*Why\?/);
assert.match(missing,/Unknown &lt;cause&gt;/);assert.match(missing,/Retained &lt;meaning&gt;/);
assert.match(missing,/tab=recurrence/);assert.doesNotMatch(missing,/<unsafe>|<cause>|<meaning>/);
assert.match(m.renderProjectBenefit({...row,benefit:{computable:true,value:0,reason:'Observed zero'}}),/>0<\/span>/);
assert.match(m.renderProjectBenefit({...row,benefit:null}),/Benefit was not measured/);
console.log('PROJECT_GRAPHICS_SEMANTICS_OK');
''')
    result=subprocess.run([node,str(tmp_path/'check.mjs')],capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stderr
    assert result.stdout.strip()=='PROJECT_GRAPHICS_SEMANTICS_OK'
