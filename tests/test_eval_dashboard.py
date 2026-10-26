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
    from self_improve.dashboard.quality_data import classes
    shown=classes(env[1],env[0])['classes'][0]
    assert shown['evaluations']['attempts']==1
    assert shown['evaluations']['comparable']==int(code=='rule_failed')
    assert shown['evaluations']['passed']==0
    assert shown['applied_revisions']==shown['quality']['reviewed']==0


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
    from tests.spa_assets import copy_spa_dependencies
    copy_spa_dependencies(tmp_path)
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


def test_class_evaluation_counts_use_frozen_class_not_current_proposal(env):
    from self_improve.dashboard.quality_data import classes
    cfg,store,p,_=env
    execute(env)
    store.update('proposals','id',p['id'],{'target_kind':'hook'});store.commit()
    rows=classes(store,cfg)['classes']
    assert rows[0]['evaluations']['comparable']==rows[0]['evaluations']['passed']==1
    assert rows[3]['evaluations']['attempts']==0
    assert rows[0]['applied_revisions']==rows[0]['quality']['reviewed']==0


def test_the_dashboard_evidence_vocabulary_matches_the_gate():
    """BL-45: the gate treats grader_error as uninformative, but the dashboard
    called it an unknown outcome and Overview said the without-rule arm "did
    not fail" for three grader errors."""
    import json as _json
    from self_improve.eval_evidence import (
        EVAL_INFRA_ERRORS, EVAL_JUDGED_ERRORS, EVAL_UNINFORMATIVE_ERRORS)
    from self_improve.evals.regression import UNINFORMATIVE_FAILURES
    from self_improve.dashboard.queries import classify_eval_result

    # Exact, not a subset: review of BL-45 found asked_operator still counted
    # as judged, which a subset check over the union cannot see.
    assert EVAL_UNINFORMATIVE_ERRORS == set(UNINFORMATIVE_FAILURES)
    assert EVAL_JUDGED_ERRORS == {"graded_fail"}
    assert "grader_error" in EVAL_INFRA_ERRORS
    row = {"id": "e1", "verdict": "error", "failed": 3, "metrics_json": _json.dumps({"without": {}, "with": None}),
           "error_taxonomy_json": _json.dumps({"grader_error": 3})}
    assert classify_eval_result(row)["class"] == "harness_broken"


def _steer_scenarios(monkeypatch, plan):
    """Give each scenario of a real job its own invented outcome.

    ``pass`` keeps the synthetic default (the without-rule arm fails, the
    with-rule arm passes). ``error_with`` and ``error_without`` time out every
    grade call in that arm, ``fail`` makes the with-rule arm fail graded, and
    ``ungated`` lets the without-rule arm pass so the mistake never reproduces.
    """
    original = LLMRunner._execute
    def run(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if args[1] != 'grade':
            return result
        sandbox = Path(kwargs['sandbox_dir'])
        scenario = next(int(p.split('-')[1]) for p in sandbox.parts if p.startswith('scenario-'))
        mode, with_rule = plan[scenario], (sandbox/'CLAUDE.md').exists()
        if mode == ('error_with' if with_rule else 'error_without'):
            return replace(result, ok=False, outcome='timeout', model_reported='')
        if mode == 'fail' and with_rule:
            (sandbox/'good.txt').unlink(missing_ok=True)
        if mode == 'ungated':
            (sandbox/'good.txt').write_text('invented')
        return result
    monkeypatch.setattr(LLMRunner, '_execute', run)


@pytest.mark.parametrize('plan,tally,case,why', [
    (('error_with',)*3, {'gated_pass': 0, 'gated_fail': 0, 'ungated': 0, 'error': 3}, 'untested',
     'no scenario tested the rule: all 3 ended in an error that gave no evidence for or against it.'),
    (('error_without',)*3, {'gated_pass': 0, 'gated_fail': 0, 'ungated': 0, 'error': 3}, 'untested',
     'no scenario tested the rule: all 3 ended in an error that gave no evidence for or against it.'),
    (('pass', 'error_with', 'error_with'), {'gated_pass': 1, 'gated_fail': 0, 'ungated': 0, 'error': 2},
     'too_few_passes', '1 of 3 scenarios showed the rule helping, too few for the gate to pass it, '
     'and none showed it failing. The other 2 did not test the rule.'),
    (('fail', 'error_with', 'error_with'), {'gated_pass': 0, 'gated_fail': 1, 'ungated': 0, 'error': 2},
     'too_few_failures', '1 of 3 scenarios showed the rule failing, too few for the gate to reject it, '
     'and none showed it helping. The other 2 did not test the rule.'),
    (('pass', 'ungated', 'error_with'), {'gated_pass': 1, 'gated_fail': 0, 'ungated': 1, 'error': 1},
     'too_few_passes', '1 of 3 scenarios showed the rule helping, too few for the gate to pass it, '
     'and none showed it failing. Of the other 2, 1 did not reproduce the mistake and 1 did not test the rule.'),
    (('pass', 'fail', 'pass'), {'gated_pass': 2, 'gated_fail': 1, 'ungated': 0, 'error': 0}, 'disagreed',
     "the gate's scenarios disagreed: 2 of 3 showed the rule helping and 1 showed it failing."),
])
def test_review_says_what_an_inconclusive_tally_shows(env, monkeypatch, plan, tally, case, why):
    """BL-22: three scenarios whose arms all broke tally {'error': 3}. The gate
    holds that as inconclusive, and Review said "the gate ran several scenarios
    and they disagreed" and "Contrary evidence exists". No scenario had tested
    the rule. One pass or one failure beside errors is not a disagreement
    either. Only a tally with both a pass and a failure may say so."""
    from self_improve.dashboard.queries import review_queue
    cfg, store, proposal, _ = env
    _steer_scenarios(monkeypatch, dict(enumerate(plan)))
    result, shown = execute(env)
    assert result['state'] == 'completed', result
    assert shown['result']['scenario_tally'] == tally
    assert shown['result']['verdict'] == 'inconclusive'
    family = next(f for f in review_queue(store, cfg)['families'] if f['learning_id'] == proposal['learning_id'])
    card = family['proposals'][0]
    assert (card['status'], card['reason_code'], card['reason_detail']) == ('inconclusive', 'inconclusive', case)
    assert card['why_needs_you'] == family['lead_reason'] == why
    assert ('disagreed' in why) == (case == 'disagreed')
    assert ('Contrary evidence' in family['means']) == (case == 'disagreed')
    if case == 'untested':
        assert 'without eval evidence' in family['means']


def test_an_inconclusive_proposal_whose_attempt_reached_another_verdict_is_refused(env):
    """The tally is read through the exact attempt that set the proposal's
    status. A link to an attempt with a different verdict is a broken record,
    so Review raises and names it rather than choosing copy."""
    from self_improve.dashboard.queries import DashboardDataError, review_queue
    cfg, store, proposal, _ = env
    result, _ = execute(env)
    assert result['result']['verdict'] == 'gated_pass'
    store.update('proposals', 'id', proposal['id'], {'status': 'inconclusive'}); store.commit()
    with pytest.raises(DashboardDataError, match=r"eval_attempt_events\..*recorded 'gated_pass'"):
        review_queue(store, cfg)


def test_a_regated_proposal_reads_the_tally_of_its_latest_gate(env, monkeypatch):
    """Review fix (BL-22): a proposal gated twice keeps both attempts. The
    first ended gated_pass; the re-gate ended inconclusive and set the
    proposal's eval result. Review must skip the older attempt rather than
    refuse the whole queue because that attempt recorded another verdict."""
    from self_improve.dashboard.queries import review_queue
    cfg, store, proposal, _ = env
    first, _ = execute(env)
    assert first['result']['verdict'] == 'gated_pass'
    _steer_scenarios(monkeypatch, dict(enumerate(('error_with',) * 3)))
    second, shown = execute(env)
    assert second['state'] == 'completed', second
    assert shown['result']['verdict'] == 'inconclusive'
    row = store.query_one('SELECT status, eval_result_id FROM proposals WHERE id=?', (proposal['id'],))
    assert row['status'] == 'inconclusive'
    assert row['eval_result_id'] == shown['result']['eval_result_id']
    results = store.query(
        "SELECT * FROM eval_attempt_events WHERE kind='attempt_result' ORDER BY created_at, id")
    assert len(results) == 2
    family = next(f for f in review_queue(store, cfg)['families']
                  if f['learning_id'] == proposal['learning_id'])
    assert family['proposals'][0]['reason_detail'] == 'untested'


def _second_attempt_result(store, cfg, proposal, data):
    """Publish one more attempt result for ``proposal`` through the real
    writer, so the record carries a valid shape and fingerprint."""
    learning = store.query_one('SELECT * FROM learnings WHERE id=?', (proposal['learning_id'],))
    current = store.query_one('SELECT * FROM proposals WHERE id=?', (proposal['id'],))
    recorder = eval_history.begin(store, cfg, learning, current)
    return recorder.event('result', 'attempt_result', data)


def test_two_attempt_results_naming_one_eval_result_are_refused(env, monkeypatch):
    """Two attempts cannot both have produced one eval result. Review names
    the second record rather than choosing which tally to show."""
    from self_improve.dashboard.queries import DashboardDataError, review_queue
    cfg, store, proposal, _ = env
    _steer_scenarios(monkeypatch, dict(enumerate(('error_with',) * 3)))
    _, shown = execute(env)
    assert shown['result']['verdict'] == 'inconclusive'
    second = _second_attempt_result(store, cfg, proposal, shown['result'])
    with pytest.raises(DashboardDataError,
                       match=rf"eval_attempt_events\.{second}: a second attempt result names eval result"):
        review_queue(store, cfg)


def test_an_attempt_result_without_a_scenario_tally_is_refused(env):
    """The attempt that set an inconclusive status must carry its tally. A
    linked result without one is a broken record, not an unknown tally."""
    from self_improve.dashboard.queries import DashboardDataError, review_queue
    cfg, store, proposal, _ = env
    _, shown = execute(env)
    assert shown['result']['verdict'] == 'gated_pass'
    data = {k: v for k, v in shown['result'].items() if k != 'scenario_tally'}
    data.update(verdict='inconclusive', eval_result_id='invented-regate-result')
    store.update('proposals', 'id', proposal['id'],
                 {'status': 'inconclusive', 'eval_result_id': 'invented-regate-result'})
    store.commit()
    event = _second_attempt_result(store, cfg, proposal, data)
    with pytest.raises(DashboardDataError,
                       match=rf"eval_attempt_events\.{event}: the gate attempt result has no scenario_tally"):
        review_queue(store, cfg)


@pytest.mark.parametrize('tally', [
    {'error': 3},
    {'gated_pass': 0, 'gated_fail': 0, 'ungated': 1, 'error': 2},
    {'gated_pass': 0, 'gated_fail': 0, 'ungated': 0, 'error': 0},
    {'gated_pass': '1', 'gated_fail': 0, 'ungated': 0, 'error': 2},
])
def test_a_tally_the_gate_cannot_write_for_inconclusive_raises(tally):
    from self_improve.dashboard.queries import DashboardDataError, inconclusive_reason
    with pytest.raises(DashboardDataError, match='invented-owner'):
        inconclusive_reason(tally, owner='invented-owner')


def test_inconclusive_copy_does_not_rerun_the_gate_vote():
    """The docstring promises shape checks only, not a second vote. The
    current thresholds call {'gated_fail': 2, 'error': 1} gated_fail, but a
    stored tally must keep its copy if those thresholds change later."""
    from self_improve.dashboard.queries import inconclusive_reason
    from self_improve.evals.regression import majority_verdict
    tally = {'gated_pass': 0, 'gated_fail': 2, 'ungated': 0, 'error': 1}
    assert majority_verdict(tally) == 'gated_fail'
    case, _, _ = inconclusive_reason(tally, owner='invented-owner')
    assert case == 'too_few_failures'
