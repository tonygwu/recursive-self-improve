"""Exact inspection keeps retained precision and never promotes unavailable rates."""
from pathlib import Path
import shutil
import subprocess

from self_improve.dashboard import app
from tests.spa_assets import copy_spa_dependencies


def test_monthly_buttons_and_exact_context(tmp_path):
    node = shutil.which('node')
    assert node
    (tmp_path / 'app.mjs').write_bytes((Path(app.__file__).parent / 'static/app.js').read_bytes())
    copy_spa_dependencies(tmp_path)
    probe = tmp_path / 'probe.mjs'
    probe.write_text(r'''
import assert from 'node:assert/strict';
const m=await import('./app.mjs');
const point={month:'2030-09',partial:true,small_sample:false,eligible_lines:300000000,
  sessions:20,occurrences:3,rate_per_100k:0.001,session_size:{median:4,max:4},
  signals:{correction:{count:1,rate_per_100k:0.0003333333333333333}},deliveries:0};
const data={requested:{start:'2030-03-01',end:'2030-09-15'},compatibility_key:'chosen<&',
  series:[point],signal_types:['correction'],min_sessions:20,
  version_groups:[{compatibility_key:'chosen<&',identifiable:true,months:[{month:'2030-09',eligible_lines:300000000}]}],
  deliveries:{count:0,by_day:{}}};
const saved=JSON.stringify(data);
const chart=m.renderTrendMatrix(data),context=m.renderTrendContext(data);
assert.match(chart,/<button[^>]+id="trend-point-correction-2030-09"[^>]+data-trend-context="2030-09"/);
assert.match(chart,/aria-controls="trend-context-2030-09"/);
assert.match(chart,/0\.0003333333333333333 per 100,000/);
assert.match(chart,/Partial month/);
assert.match(context,/Exact monthly rates/);
assert.match(context,/0\.0003333333333333333 per 100,000/);
assert.match(context,/1 occurrences \/ 300000000 eligible physical lines/);
assert.match(context,/20 known sessions/);
assert.match(context,/chosen&lt;&amp;/);
assert.match(context,/2030-03-01 to 2030-09-15 \(exclusive\)/);
assert.equal(JSON.stringify(data),saved);
for(const [reason,text] of [['insufficient_sessions','Insufficient sessions'],['unknown_version','Unknown detector version'],['zero_eligible_exposure','No eligible exposure']]) {
  const unavailable={...data,series:[{...point,reason,rate_per_100k:null,small_sample:true,
    signals:{correction:{count:1,rate_per_100k:null,observed_rate_per_100k:999}}}]};
  const html=m.renderTrendContext(unavailable);
  assert.ok(html.includes(text));assert.match(html,/Fewer than 20 sessions/);
  assert.doesNotMatch(html,/999/);assert.doesNotMatch(m.renderTrendMatrix(unavailable),/class="spark__bar"/);
}
const zero={...data,series:[{...point,occurrences:0,rate_per_100k:0,signals:{correction:{count:0,rate_per_100k:0}}}]};
assert.match(m.renderTrendContext(zero),/0 per 100,000 lines/);
assert.match(m.renderTrendMatrix(zero),/data-zero="true"/);
// The aggregate is the supplied reader value and each row has an independent peak.
const mixed={...data,series:[{...point,month:'2030-08',rate_per_100k:50,signals:{correction:{count:1,rate_per_100k:4}}},
  {...point,rate_per_100k:100,signals:{correction:{count:1,rate_per_100k:2}}}]};
const bars=[...m.renderTrendMatrix(mixed).matchAll(/class="spark__bar"[^>]+--v:([\d.]+)/g)].map(x=>Number(x[1]));
assert.deepEqual(bars,[50,100,100,50]);
assert.match(m.renderTrendContext({...data,series:[]}),/No primary rates retained for this month/);
console.log('EVALS_CHART_RENDERER_OK');
''')
    result = subprocess.run([node, str(probe)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'EVALS_CHART_RENDERER_OK'
