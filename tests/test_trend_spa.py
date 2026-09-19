"""The real trend renderer keeps gaps and late responses distinct."""
import shutil
import subprocess
from pathlib import Path
from self_improve.dashboard import app


def test_monthly_renderer_and_late_responses(tmp_path):
    node = shutil.which('node'); assert node
    (tmp_path/'app.mjs').write_bytes((Path(app.__file__).parent/'static/app.js').read_bytes())
    from tests.spa_assets import copy_spa_dependencies
    copy_spa_dependencies(tmp_path)
    probe = tmp_path/'probe.mjs'
    probe.write_text(r'''
import assert from 'node:assert/strict';
const m=await import('./app.mjs');
const body={innerHTML:'',querySelectorAll:()=>[]};
const controls={innerHTML:'',querySelectorAll:()=>[]};
globalThis.document={activeElement:null,getElementById:id=>id==='trends-body' ? body : id==='trend-controls' ? controls : null};
const pending=[];
globalThis.fetch=url=>new Promise(resolve=>pending.push({url,resolve}));
const answer=(i,data)=>pending[i].resolve({ok:true,json:async()=>data});
const data=label=>({requested:{end_month:'2026-09'},partial_month:'2026-09',project_options:[],version_groups:[],series:[],signal_types:['correction'],min_sessions:20,coverage_by_project:[],reason_text:label,deliveries:{records:[],count:0,note:'Recorded only.'}});
m.state.route='evals';
const first=m.loadTrends('project_key=first');
const second=m.loadTrends('project_key=second');
answer(1,data('Second scope'));await second;
const shown=body.innerHTML;
const selected=controls.innerHTML;
answer(0,data('First scope'));await first;
assert.equal(body.innerHTML,shown);
assert.equal(controls.innerHTML,selected);
assert.match(shown,/Second scope/);
assert.match(m.renderTrends({data:data('<script>') }),/&lt;script&gt;/);
assert.equal(m.parseRoute('#/evals?project_key=encoded%2Fid').trendQuery,'project_key=encoded%2Fid');
assert.equal(m.parseRoute('#/evals').view,'evals');
const versions=[{compatibility_key:'first',identifiable:true},{compatibility_key:'second',identifiable:true}];
const choose=m.renderTrendControls({data:{...data(''),reason:'incompatible_versions',version_groups:versions}});
assert.match(choose,/<details[^>]+open/);
assert.match(choose,/Choose detector configuration/);
assert.match(choose,/value="first"/);
assert.match(choose,/value="second"/);
assert.match(m.renderTrendControls({data:data('')}),/Detector coverage unavailable/);
const scoped=m.renderTrendControls({data:{...data(''),requested:{project_key:'<script>',end_month:'2026-09',compatibility_key:'first'},version_groups:versions,compatibility_key:'first'}});
assert.match(scoped,/&lt;script&gt;/);
assert.match(scoped,/value="first" selected/);
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
const small=m.renderTrendMatrix({...data(''),series:[{...point,reason:'insufficient_sessions',signals:{correction:{count:0,rate_per_100k:null,observed_rate_per_100k:0}}}]});
assert.match(small,/Insufficient sessions; trend rate unavailable/);
assert.doesNotMatch(small,/class="spark__bar"/);
assert.match(small,/Unknown-session lines/);
const raw=m.renderTrends({data:{...data(''),series:[{...point,reason:'insufficient_sessions',rate_per_100k:null,observed_rate_per_100k:0,signals:{correction:{count:0,rate_per_100k:null,observed_rate_per_100k:0}}}]}});
assert.match(raw,/Inspect observed arithmetic/);
assert.match(raw,/0 observed per 100,000 lines/);
assert.match(raw,/does not make a small sample adequate/);
const side={eligible_lines:76,occurrences:10,sessions:19,rate_per_100k:null,reason:'insufficient_sessions',session_size:{median:4,max:4},workload:{by_source:{codex:76},headless:4,subagent:0}};
m.state.measurementHistory['/api/project-measurements?limit=20']={loaded:true,count:1,records:[{id:'r',learning_id:'l',project_key:'p',rule_revision_id:'revision',compatibility_key:'v',observed_at:'2026-09-01',computable:false,reason:'insufficient_sessions',min_sessions:20,before:side,after:side}]};
const recurrence=m.renderRecurrence('', 'evals');
assert.match(recurrence,/Rate unavailable · insufficient sessions/);
assert.match(recurrence,/median 4 · largest 4 lines/);
assert.match(recurrence,/codex: 76 lines/);
assert.match(recurrence,/4 headless · 0 subagent lines/);
// Explicit peaks use only available primary rates. Aggregate is supplied by
// the reader, never the sum of individually rounded signal rates.
const marked={...data(''),compatibility_key:'selected',version_groups:[
  {compatibility_key:'selected',identifiable:true,months:[{month:'2026-09',eligible_lines:80}],manifests:[]},
  {compatibility_key:'unknown',identifiable:false,months:[{month:'2026-09',eligible_lines:80}],manifests:[]},
  {compatibility_key:'uncovered',identifiable:true,months:[],manifests:[]}],
  deliveries:{records:[],count:23,by_day:{'2026-09-02':12,'2026-09-03':11}},
  series:[{...point,small_sample:false,occurrences:2,rate_per_100k:2500,
    signals:{correction:{count:1,rate_per_100k:1250}}}]};
const matrix=m.renderTrendMatrix(marked);
assert.match(matrix,/all kinds/);
assert.match(matrix,/>Peak<\/th>/);
assert.match(matrix,/data-trend-month="2026-09"/);
assert.match(matrix,/data-trend-context="2026-09"/);
assert.match(matrix,/2,500/);
assert.match(matrix,/chosen \+1/);
assert.match(matrix,/2 dates/);
const context=m.renderTrendContext(marked);
assert.match(context,/2026-09-02/);assert.match(context,/2026-09-03/);
assert.match(context,/12 retained revisions/);assert.match(context,/11 retained revisions/);
assert.match(context,/unknown/);assert.doesNotMatch(context,/uncovered<\/code>/);
const peakOnly=html=>Array.from(html.matchAll(/class="trend-peak"[^>]*>(.*?)<\/td>/g),m=>m[1]);
assert.deepEqual(peakOnly(m.renderTrendMatrix({...marked,series:[{...point,signals:{correction:{count:0,rate_per_100k:null,observed_rate_per_100k:999}}}]})),['—','—']);
assert.deepEqual(peakOnly(m.renderTrendMatrix({...marked,series:[{...point,rate_per_100k:0,signals:{correction:{count:0,rate_per_100k:0}}}]})),['0','0']);
assert.deepEqual(peakOnly(m.renderTrendMatrix({...marked,series:[{...point,rate_per_100k:0.0001,signals:{correction:{count:1,rate_per_100k:0.0001}}}]})),['&lt;0.01','&lt;0.01']);
assert.match(m.renderTrendContext({...marked,series:[]}),/2026-09-02/);
console.log('TREND_UI_OK');
''')
    result = subprocess.run([node, str(probe)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'TREND_UI_OK'
