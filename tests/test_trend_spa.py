"""The real trend renderer keeps gaps and late responses distinct."""
import shutil
import subprocess
from pathlib import Path
from self_improve.dashboard import app


def test_monthly_renderer_and_late_responses(tmp_path):
    node = shutil.which('node'); assert node
    (tmp_path/'app.mjs').write_bytes((Path(app.__file__).parent/'static/app.js').read_bytes())
    probe = tmp_path/'probe.mjs'
    probe.write_text(r'''
import assert from 'node:assert/strict';
const m=await import('./app.mjs');
const body={innerHTML:'',querySelectorAll:()=>[]};
globalThis.document={activeElement:null,getElementById:id=>id==='trends-body' ? body : null};
const pending=[];
globalThis.fetch=url=>new Promise(resolve=>pending.push({url,resolve}));
const answer=(i,data)=>pending[i].resolve({ok:true,json:async()=>data});
const data=label=>({requested:{end_month:'2026-09'},partial_month:'2026-09',project_options:[],version_groups:[],series:[],signal_types:['correction'],min_sessions:20,coverage_by_project:[],reason_text:label,deliveries:{records:[],count:0,note:'Recorded only.'}});
m.state.route='evals';
const first=m.loadTrends('project_key=first');
const second=m.loadTrends('project_key=second');
answer(1,data('Second scope'));await second;
const shown=body.innerHTML;
answer(0,data('First scope'));await first;
assert.equal(body.innerHTML,shown);
assert.match(shown,/Second scope/);
assert.match(m.renderTrends({data:data('<script>') }),/&lt;script&gt;/);
assert.equal(m.parseRoute('#/evals?project_key=encoded%2Fid').trendQuery,'project_key=encoded%2Fid');
assert.equal(m.parseRoute('#/evals').view,'evals');
const point={month:'2026-09',partial:true,small_sample:true,eligible_lines:4,sessions:1,session_size:{median:4},signals:{correction:{count:0,rate_per_100k:0}},deliveries:0};
const measured=m.renderTrendMatrix({...data(''),series:[point]});
assert.match(measured,/0 occurrences \/ 4 eligible physical lines/);
assert.match(measured,/class="spark__bar"/);
assert.match(measured,/data-partial="true"/);
const missing=m.renderTrendMatrix({...data(''),series:[{...point,eligible_lines:0,reason:'zero_eligible_exposure',signals:{correction:{count:0,rate_per_100k:null}}}]});
assert.match(missing,/No eligible exposure; no rate/);
assert.doesNotMatch(missing,/class="spark__bar"/);
const unknown=m.renderTrendMatrix({...data(''),series:[{...point,reason:'unknown_version',signals:{correction:{count:3,rate_per_100k:null}}}]});
assert.match(unknown,/Unknown detector version; no rate/);
assert.match(unknown,/3 occurrences \/ 4 eligible physical lines/);
assert.match(m.renderTrends({data:{...data(''),coverage_by_project:[{coverage:{coverage_complete:false}}]}}),/Incomplete scan coverage in 1 project scope/);
const tiny=m.renderTrendMatrix({...data(''),series:[{...point,eligible_lines:1000000000,signals:{correction:{count:1,rate_per_100k:0.0001}}}]});
assert.match(tiny,/&lt;0.01/);
console.log('TREND_UI_OK');
''')
    result = subprocess.run([node, str(probe)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'TREND_UI_OK'
