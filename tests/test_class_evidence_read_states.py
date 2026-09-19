"""Class evidence read failures retain full diagnostic and current consent."""
import pytest
from tests.test_rule_presentation import render  # noqa: F401


@pytest.mark.parametrize("retained", [False, True])
def test_initial_and_refresh_failure_explain_cause_before_complete_diagnostic(render, retained):
    render("const retained=" + str(retained).lower() + r''';
const cause='Invented evidence source unavailable <script>sample</script> ' + 'long cause '.repeat(80);
const error='GET /api/class-evidence answered 503 '+cause;
const data={classes:[],quality_available:true,policy_available:true};
const html=m.renderClassEvidence({data:retained?data:null,error,errorDetail:cause});
const alert=html.match(/<p[^>]*role="alert"[^>]*>(.*?)<\/p>/s)[1];
assert.match(alert,retained?/showing the previous class evidence/:/Could not read class evidence/);
assert.match(alert,/Invented evidence source unavailable/);assert.match(alert,/Retry class evidence/);
assert.doesNotMatch(alert,/GET \/api/);assert.ok(alert.length<600);
assert.match(html,/>Request details<\/summary>/);
assert.match(html,/id="policy-read-error"/);
assert.ok(html.includes(error.replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;')));
assert.doesNotMatch(html,/<script>/);assert.match(html,/data-policy-refresh="true"/);
''')


def test_class_read_retains_cause_old_evidence_and_single_flight_retry(render):
    render(r'''
m.state.route='overview';
const rows=['global','project'].map((target_class,index)=>({target_class,
 applied_revisions:index?0:null,rolled_back_revisions:index?0:null,
 quality:null,evaluations:null,policy:{enabled:false},sample_advice:{applied:20}}));
const data={classes:rows,quality_available:true,policy_available:true};
m.state.classEvidence={data};
let answer;const calls=[];
globalThis.fetch=(url,options)=>{calls.push({url,method:options.method});return new Promise(resolve=>answer=resolve);};
const pending=m.loadClassEvidence();await m.loadClassEvidence();assert.equal(calls.length,1);
assert.equal(m.state.classEvidence.data,data);
answer({ok:false,status:503,statusText:'Unavailable',json:async()=>({detail:'Invented source unavailable'})});await pending;
assert.equal(m.state.classEvidence.errorDetail,'Invented source unavailable');
assert.equal(m.state.classEvidence.data,data);assert.equal(m.state.classEvidence.loading,false);
let html=m.renderClassEvidence(m.state.classEvidence);
assert.match(html,/<td>Unknown<\/td>/);assert.match(html,/<td>0<\/td>/);
assert.ok(rows.every(row=>row.policy.enabled===false));
const retry=m.loadClassEvidence();assert.equal(calls.length,2);
assert.match(m.renderClassEvidence(m.state.classEvidence),/Reading class evidence/);
answer({ok:true,json:async()=>data});await retry;
html=m.renderClassEvidence(m.state.classEvidence);assert.doesNotMatch(html,/role="alert"|Request details/);
assert.equal(m.state.classEvidence.data,data);assert.ok(calls.every(c=>c.method==='GET'&&c.url==='/api/class-evidence'));
''')
