"""Evaluation presentation through real writers, copied Stores and the SPA module."""
from contextlib import closing
from dataclasses import replace
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from self_improve import eval_history
from self_improve.dashboard import eval_data
from self_improve.store import Store
from self_improve.llm import LLMRunner
from tests.test_eval_history import env, cfg, store, execute


def test_real_job_summary_keeps_all_scenarios_and_source_identity(env):
    _, store, proposal, calls = env
    job, data = execute(env)
    page = eval_data.attempts(store, proposal_id=proposal['id'])
    row = page['records'][0]
    assert row['run_id'] == job['run_id'] and row['command_id'] == job['id']
    assert (row['scenarios'], row['completed_scenarios'], row['paired_scenarios']) == (3, 3, 3)
    assert row['calls_recorded'] == row['calls_started'] == len(calls) == 21
    assert row['outcome']['code'] == 'rule_helped'
    assert 'events' not in row and 'proposal' not in row
    assert eval_data.unlinked(store)['count'] == 0
    health = eval_data.health(store)
    assert health['attempts'] == 1 and health['unlinked_results'] == 0
    assert health['by_outcome'][0]['code'] == 'rule_helped' and health['by_outcome'][0]['count'] == 1
    direct = eval_data.legacy(store, data['scenarios'][0]['evaluation']['id'])
    assert direct['attempt_links'] == [{'attempt_id': data['source']['id'], 'scenario': 0}]


@pytest.mark.parametrize('change,code', [('model', 'comparison_unavailable'), ('infrastructure','harness_failed'), ('fail','rule_failed'), ('no_mistake','invalid_scenario')])
def test_outcome_distinguishes_invalid_execution_and_rule_failure(env, monkeypatch, change, code):
    original = LLMRunner._execute
    def run(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if args[1] == 'grade':
            sandbox = Path(kwargs['sandbox_dir'])
            if change == 'model': result = replace(result, model_reported='')
            elif change == 'infrastructure': result = replace(result, ok=False, outcome='subprocess_error', error='invented provider unavailable')
            elif change == 'fail': (sandbox/'good.txt').unlink(missing_ok=True)
            elif change == 'no_mistake': (sandbox/'good.txt').write_text('invented')
        return result
    monkeypatch.setattr(LLMRunner, '_execute', run)
    _, data = execute(env)
    assert eval_data.outcome(data)['code'] == code


def test_attempt_and_historical_pages_preserve_equal_times_and_unknown_sources(env):
    _, store, proposal, _ = env
    _, first = execute(env)
    learning = store.query_one('SELECT * FROM learnings WHERE id=?', (proposal['learning_id'],))
    for i in range(22):
        recorder = eval_history.begin(store, env[0], learning, proposal)
        recorder.stop(ValueError('invented stop '+str(i)))
    page = eval_data.attempts(store, learning_id=learning['id'])
    assert page['count'] == 23 and len(page['records']) == 20
    last = eval_data.attempts(store, learning_id=learning['id'], cursor=page['next_cursor'])
    assert len(last['records']) == 3 and last['next_cursor'] is None
    assert len({r['id'] for r in page['records']+last['records']}) == 23
    with pytest.raises(eval_history.EvalHistoryRequestError):
        eval_data.attempts(store, learning_id='different', cursor=page['next_cursor'])
    original = first['scenarios'][0]['evaluation']
    for i in range(23):
        store.insert('eval_results', {**original, 'id': 'legacy-'+str(i).zfill(2), 'subject_id':'no-retained-rule', 'kind':'ab' if i == 0 else 'regression'})
    store.commit()
    page = eval_data.unlinked(store)
    last = eval_data.unlinked(store,cursor=page['next_cursor'])
    assert page['count'] == 23 and len(page['records']) == 20 and len(last['records']) == 3
    assert all(r['rule_text'] is None and r['outcome']['code'] in ('historical_unknown','without_rule_only') for r in page['records']+last['records'])
    assert eval_data.legacy(store,'legacy-00')['summary']['comparison_type'] == 'without_rule_only'
    health = eval_data.health(store)
    assert health['attempts'] == 23 and health['unlinked_results'] == 23
    assert {r['code']:r['count'] for r in health['by_outcome']} == {'rule_helped':1, 'incomplete':22}
    with pytest.raises(eval_history.EvalHistoryRequestError):eval_data.unlinked(store,cursor=original['id'])


def test_api_reads_selected_copy_never_publish_or_migrate(env, tmp_path):
    pytest.importorskip('fastapi', reason='the dashboard extra is not installed')
    from fastapi.testclient import TestClient
    from self_improve.dashboard.app import create_app
    cfg, store, proposal, calls = env
    _, data = execute(env)
    target = tmp_path/'copied.db'
    with closing(Store(target)) as copy:store.conn.backup(copy.conn)
    # Mutating only the source after the backup proves the reader uses the copy.
    recorder = eval_history.begin(store, cfg, data['source']['learning'], data['source']['proposal'])
    recorder.stop(ValueError('only in the source'))
    before = target.read_bytes(); before_calls = len(calls)
    with TestClient(create_app(cfg, db_path=target)) as client:
        page = client.get('/api/eval-attempts?summary=true').json()
        assert page['count'] == 1 and page['records'][0]['outcome']['code'] == 'rule_helped'
        full = client.get('/api/eval-attempts/'+data['source']['id']).json()
        assert full['summary']['id'] == data['source']['id'] and len(full['scenarios']) == 3
        assert client.get('/api/eval-results').json()['count'] == 0
        assert client.get('/api/eval-health').json()['attempts'] == 1
        assert client.get('/api/eval-results?limit=0').status_code == 400
        assert client.get('/api/eval-results/absent').status_code == 404
    assert target.read_bytes() == before and len(calls) == before_calls


def test_empty_old_schema_and_corruption_are_distinct(env):
    _, store, _, _ = env
    assert eval_data.attempts(store)['count'] == 0
    assert eval_data.unlinked(store)['count'] == 0
    store.conn.execute('DELETE FROM schema_migrations WHERE name=?', (eval_history.MIGRATION,));store.commit()
    assert eval_data.attempts(store)['count'] is None
    assert eval_data.unlinked(store)['count'] == 0


def test_spa_renders_full_evidence_with_escaped_text_and_deep_links(env, tmp_path):
    _, store, proposal, _ = env
    _, shown = execute(env)
    data = eval_data.attempt(store, shown['source']['id'])
    data['source']['learning']['rule_text'] = '<img src=x onerror=bad()> Complete invented rule'
    static = Path(__file__).resolve().parents[1]/'src/self_improve/dashboard/static'
    module = tmp_path/'app.mjs'; module.write_bytes((static/'app.js').read_bytes())
    payload = tmp_path/'payload.json'; payload.write_text(json.dumps(data))
    script = tmp_path/'check.mjs'
    script.write_text('''import fs from 'node:fs';
import assert from 'node:assert/strict';
import * as app from './app.mjs';
const data=JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const query='eval_learning_id=abc&compatibility_key=version-a&eval_cursor=old';
const route=app.parseRoute(app.evaluationHref('attempt',data.summary.id,query));
assert.equal(route.view,'evals');assert.equal(route.trendQuery,query);
assert.equal(app.parseRoute('#/evals?').view,'evals');
assert.equal(app.evalQueryParts(query).trends.toString(),'compatibility_key=version-a');
const html=app.renderEvaluationDetail({kind:'attempt',id:data.summary.id,query,data});
assert(!html.includes('<img src=x'));assert(html.includes('&lt;img src=x'));
for (const text of ['Scenario 1','Scenario 2','Scenario 3','Fewer than 20 valid trials per arm','Every trial outcome','Complete specification','Complete frozen source','Model calls for this scenario (7)','Preview regeneration cost','Producing run']) assert(html.includes(text),text);
assert(!html.includes('+100 percentage points'));assert(html.includes('eval_cursor=old'));assert(html.includes(data.source.run_id));
const links=app.renderRunRecords('evaluations',{id:data.source.run_id,pages:{evaluations:{records:[{id:'result',verdict:'gated_pass',links:[{attempt_id:data.summary.id}]}],loaded:true,count:1}}});
assert(links.includes('/evals/attempt/'+data.summary.id));
const waiting=app.renderEvaluationHistory({params:'eval_kind=unlinked',data:{records:[],count:0,computable:true,reason:'No historical rows'}});
assert(waiting.includes('No records match'));assert(!waiting.includes('100%'));
app.state.evaluationHistory={data:{records:[],count:0,computable:true,reason:'Independent history'}};
assert(app.renderTrends({error:'Invented trend outage'}).includes('Every eval so far'));
globalThis.location={hash:'#/evals?end_month=2025-09'};
let prevented=false;
app.handleMainClick({target:{closest(selector){const values={'data-evaluation-open':data.summary.id,'data-evaluation-kind':'attempt'};const key=selector.slice(1,-1);return key in values ? {getAttribute(name){return values[name]}} : null;}},preventDefault(){prevented=true}});
assert(prevented);assert.equal(location.hash,app.evaluationHref('attempt',data.summary.id,'end_month=2025-09'));
for (const modifier of ['metaKey','ctrlKey','shiftKey','altKey']) {
  prevented=false;location.hash='#/evals?end_month=2025-09';
  app.handleMainClick({[modifier]:true,target:{closest(selector){return selector==='[data-evaluation-open]' ? {getAttribute(){return data.summary.id}} : null}},preventDefault(){prevented=true}});
  assert(!prevented,modifier+' must preserve native link behavior');
  assert.equal(location.hash,'#/evals?end_month=2025-09');
}
console.log('EVAL_RENDER_OK');
''')
    node = shutil.which('node'); assert node
    result = subprocess.run([node,str(script),str(payload)],capture_output=True,text=True,timeout=60)
    assert result.returncode == 0,result.stdout+result.stderr
    assert result.stdout.strip() == 'EVAL_RENDER_OK'
