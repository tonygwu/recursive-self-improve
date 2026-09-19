"""Graphics must preserve missing values and exact retained measurement units."""
import shutil
import subprocess
from pathlib import Path

from self_improve.dashboard import app
from tests.spa_assets import copy_spa_dependencies


def test_graphics_keep_attempts_pools_and_unknown_states_separate(tmp_path):
    node = shutil.which('node')
    assert node
    (tmp_path / 'app.mjs').write_bytes((Path(app.__file__).parent / 'static/app.js').read_bytes())
    copy_spa_dependencies(tmp_path)
    probe = tmp_path / 'probe.mjs'
    probe.write_text(r'''
import assert from 'node:assert/strict';
const m = await import('./app.mjs');
const health = {computable:true, attempts:4, unlinked_results:17, reason:'Recorded attempts only.',
  by_outcome:[{code:'rule_helped',label:'Rule helped',count:1,tone:'ok'},
    {code:'interrupted',label:'Interrupted',count:3,tone:'partial'}]};
let html = m.renderEvaluationHealth({data:health});
assert.match(html, /class="outcome-strip"/);
assert.match(html, /data-outcome="rule_helped"[^>]+--share:25/);
assert.match(html, /data-outcome="interrupted"[^>]+--share:75/);
assert.match(html, /17 historical result rows/);
assert.match(html, /aria-hidden="true"/);
for (const value of [{...health,computable:false,attempts:null,by_outcome:[]},
  {...health,attempts:0,by_outcome:[]}, {...health,attempts:5},
  {...health,by_outcome:[...health.by_outcome,{...health.by_outcome[0],count:0}]}]) {
  assert.doesNotMatch(m.renderEvaluationHealth({data:value}), /class="outcome-strip"/);
}
assert.match(m.renderEvaluationHealth({data:{...health,attempts:0,by_outcome:[]}}),/No recorded attempts to distribute/);
assert.match(m.renderEvaluationHealth({data:{...health,attempts:5}}),/Distribution unavailable/);
assert.match(m.renderEvaluationHealth({data:{...health,attempts:1,by_outcome:[{code:'<script>',label:'<script>',count:1,tone:'alien'}]}}),/&lt;script&gt;/);
const many = ['budget_refused','interrupted','cancelled','invalid_scenario','comparison_unavailable','future_outcome'].map(code=>({code,label:code,count:1,tone:'partial'}));
const manyHTML=m.renderEvaluationHealth({data:{...health,attempts:6,by_outcome:many}});
assert.match(manyHTML,/left-to-right order/);
for(let i=1;i<=6;i++) assert.equal((manyHTML.match(new RegExp('data-key="'+i+'"','g'))||[]).length,2);
const usage = (pools,tokens={recorded:1,tokens_in:3,tokens_out:1}) =>
  m.renderRunUsage({inspection:{pipeline_pools:pools,jobs:[{budget:{maximum:{cheap:999}}}]},calls:tokens});
html = usage([{pool:'cheap',used:1,limit:4,refused:0},{pool:'gate',used:4,limit:4,refused:2}]);
assert.match(html,/data-pool="cheap"/);assert.match(html,/--pct:25/);
assert.match(html,/--pct:100/);assert.match(html,/2 refused/);assert.match(html,/Budget exhausted/);
assert.doesNotMatch(html,/999/);
assert.match(html,/Stored token totals: 3 in \/ 1 out/);assert.match(html,/--share:75/);
assert.match(html,/may be incomplete/);assert.match(html,/no token cap/);
for (const pool of [{pool:'cheap',used:null,limit:4},{pool:'gate',used:4,limit:null}]) {
  assert.doesNotMatch(usage([pool]),/class="meter__fill"/);
  assert.match(usage([pool]),/Usage ratio unknown/);
}
assert.match(usage([{pool:'cheap',used:0,limit:0}]),/No calls permitted/);
assert.doesNotMatch(usage([{pool:'cheap',used:0,limit:0}]),/class="meter__fill"/);
assert.match(usage([{pool:'cheap',used:null,limit:0}]),/No calls permitted · usage unknown/);
assert.match(usage([{pool:'cheap',used:5,limit:4}]),/1 over limit/);
assert.match(usage([{pool:'cheap',used:5,limit:0}]),/5 over limit/);
assert.match(usage([{pool:'cheap',used:0,limit:4}]),/--pct:0/);
for (const calls of [{recorded:0,tokens_in:0,tokens_out:0},
 {recorded:1,tokens_in:0,tokens_out:0},{recorded:1,tokens_in:null,tokens_out:1}]) {
 assert.doesNotMatch(usage([],calls),/class="token-share"/);
 assert.match(usage([],calls),/share unavailable/);
}
assert.match(m.renderRunUsage({calls:{recorded:0}}),/Pipeline budget not recorded/);
assert.match(usage([{pool:'<script>',used:1,limit:2}]),/&lt;script&gt;/);
for (const [state,label] of Object.entries({refused:'Budget refused',unaccounted:'Accounting gap',
 interrupted:'Interrupted',limited:'Work omitted',budget_exhausted:'Budget exhausted',
 unreadable:'Record unreadable',unknown_status:'Outcome unknown',ok:'Succeeded'})) {
 const labelHTML=m.renderRunStageState(state);
 assert.match(labelHTML,/class="run-stage-state"/);assert.ok(labelHTML.includes(label));
}
assert.match(m.renderRunStageState('<script>'),/Outcome unknown/);
assert.match(m.renderRunStageState('<script>'),/&lt;script&gt;/);
assert.doesNotMatch(m.renderRunStageState('<script>'),/<script>/);
assert.match(m.renderRunStageState('__proto__'),/Outcome unknown/);
assert.doesNotMatch(m.renderRunStageState('__proto__'),/\[object Object\]/);
console.log('DASHBOARD_GRAPHICS_OK');
''')
    result = subprocess.run([node, str(probe)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'DASHBOARD_GRAPHICS_OK'
