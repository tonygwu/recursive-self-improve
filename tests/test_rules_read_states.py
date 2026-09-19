"""Reported evidence and recoverable reads, using invented temporary state."""
from self_improve.dashboard import queries
from tests.test_dashboard_queries import _learning, _writable
from tests.test_rule_presentation import render  # noqa: F401


def test_api_report_is_not_verified_receipt_or_violation(tmp_path):
    store, _ = _writable(tmp_path)
    try:
        report = "Retained <script>report</script> & exact text."
        _learning(store, learning_id="reported", violated_existing_rule=report)
        _learning(store, learning_id="unknown")
        rows = {r["id"]: r["enforcement_gap"] for r in queries.rules(store)["rows"]}
        assert rows["reported"]["flagged"] is True
        assert rows["reported"]["violated_existing_rule"] == report
        assert "reported" in rows["reported"]["label"]
        assert "unverified" in rows["reported"]["label"]
        assert rows["unknown"]["flagged"] is False
        assert "unknown" in rows["unknown"]["label"]
    finally:
        store.close()


def test_both_lists_and_inspector_qualify_the_report(render):
    render(r'''
const row={id:'reported',rule_text:'Invented rule',status:'candidate',targets:[],
 enforcement_gap:{flagged:true,violated_existing_rule:'<script>report</script> & complete',label:'Receipt is unverified'}};
const inspector=m.renderRuleInspector(row,'why');
assert.match(inspector,/Reported violation/);assert.match(inspector,/Rule named in the report/);
assert.match(inspector,/&lt;script&gt;report&lt;\/script&gt; &amp; complete/);
assert.doesNotMatch(inspector,/<script>|written but ignored|The rule it violated/i);
assert.match(m.renderRuleRows([row],''),/Reported violation/);
assert.match(m.renderRuleBrowserRows({rows:[{kind:'rule',rule:{...row,enforcement_gap:true}}]}),/Reported violation/);
const unknown=m.renderRuleInspector({...row,enforcement_gap:{flagged:false,label:'Receipt is unknown'}},'why');
assert.match(unknown,/No violation report was retained/);assert.doesNotMatch(unknown,/written but ignored/i);
''')


def test_history_failures_keep_full_diagnostics_and_previous_records(render):
    render(r'''
const error='GET /api/private-looking-but-invented answered 503 <script>diagnostic</script> '+ 'tail '.repeat(200);
const entry={records:[],loaded:true,loading:false,error,errorDetail:'Invented unavailable source'};
m.state.scanHistories.incident=entry;m.state.miningHistories.learning=entry;
for(const html of [m.renderScanHistory('incident','rule'),m.renderMiningHistory({id:'learning'})]){
 const alert=html.match(/<p[^>]*role="alert"[^>]*>(.*?)<\/p>/s)?.[1];
 assert.ok(alert,html);assert.match(alert,/Invented unavailable source/);
 assert.match(alert,/previous/i);assert.doesNotMatch(alert,/GET \/api/);
 assert.match(html,/>Request details<\/summary>/);assert.match(html,/tail tail tail/);
 assert.match(html,/&lt;script&gt;diagnostic/);assert.doesNotMatch(html,/<script>/);
}
''')


def test_family_failure_leads_with_recovery_and_keeps_request(render):
    render(r'''
m.state.ruleQuery='family=f';m.state.ruleMembers={key:'/api/rule-families/f/members',
 error:'GET /api/rule-families/f/members answered 503 <unavailable>',errorDetail:'Invented unavailable family'};
const html=m.renderRuleBrowserRows({rows:[{kind:'family',family_id:'f',representative:{title:'Family'}}]});
const alert=html.match(/<p[^>]*role="alert"[^>]*>(.*?)<\/p>/s)?.[1];
assert.match(alert,/Could not read family members/);assert.match(alert,/Retry members/);
assert.doesNotMatch(alert,/GET \/api/);assert.match(html,/>Request details<\/summary>/);
assert.match(html,/GET \/api\/rule-families\/f\/members answered 503 &lt;unavailable&gt;/);
''')


def test_new_family_revision_supersedes_pending_old_members(render):
    render(r'''
globalThis.document={getElementById(){return null;},activeElement:null};
m.state.ruleQuery='family=f';m.state.route='rules';m.state.rules={mode:'paged'};
m.state.rulePage={data:{revision:'old'}};
const resolvers=[];globalThis.fetch=()=>new Promise(resolve=>resolvers.push(resolve));
const old=m.loadRuleMembers();m.state.rulePage={data:{revision:'new'}};
const fresh=m.loadRuleMembers({refresh:true});assert.equal(resolvers.length,2);
function response(revision,title){return {ok:true,json:async()=>({revision,
 rows:[{id:title,title,targets:[]}],pagination:{count:1,offset:0}})};}
resolvers[1](response('new','NEW MEMBER'));await fresh;
resolvers[0](response('old','STALE MEMBER'));await old;
assert.equal(m.state.ruleMembers.revision,'new');
const html=m.renderRuleBrowserRows({rows:[{kind:'family',family_id:'f',representative:{title:'Family'}}]});
assert.match(html,/NEW MEMBER/);assert.doesNotMatch(html,/STALE MEMBER/);
''')


def test_late_evidence_page_keeps_the_users_new_focus_on_success_or_failure(render):
    render(r'''
for(const ok of [true,false]) {
 const search={id:'rules-search-input'},pager={id:'evidence-next'};
 const meta={id:'rules-meta',focus(){document.activeElement=this;}};
 globalThis.document={getElementById(id){return id==='rules-meta'?meta:null;},activeElement:pager};
 m.state.ruleQuery='mode=evidence&cursor='+ok;m.state.route='rules';
 let resolve;globalThis.fetch=()=>new Promise(r=>resolve=r);
 const pending=m.loadEvidencePage({focus:true});document.activeElement=search;
 resolve({ok,status:503,statusText:'Unavailable',json:async()=>ok ? {rows:[]} : {detail:'Invented read failure'}});
 await pending;assert.equal(document.activeElement,search);
}
''')
