"""The real Run renderer preserves native units in the revised stage/usage layout."""
import html
import json
import re

import pytest

from self_improve.dashboard import queries
from tests.test_navigation import node


def detail(stages):
    run = {'id': 'invented-run', 'started': '2030-01-02T00:00:00Z',
           'finished': '2030-01-02T00:01:30Z', 'status': 'degraded'}
    return {'run': run, 'stats': {}, 'night_runs': [run],
            'calls': {'recorded': 0, 'reported': 0, 'reason': ''},
            'inspection': {'pipeline_pools': [{'pool': 'cheap', 'used': 3, 'limit': 8, 'refused': 1}],
                           'jobs': [], 'policy_waits': 0, 'wall_clock': None},
            'stages': [{'name': name, 'recorded': payload is not None, 'payload': payload,
                        'causes': [], 'accounting': queries._stage_cell(run, name, {} if payload is None else {name: payload}),
                        'state': queries._stage_cell(run, name, {} if payload is None else {name: payload})['state']}
                       for name, payload in stages]}


def render(data, **extra):
    entry = {'id': data['run']['id'], 'kind': 'run', 'data': data,
             'pages': {'deliveries': {'loaded': True, 'records': [], 'count': 0}}, **extra}
    return node('console.log(JSON.stringify(app.renderRunDetail('+json.dumps(entry)+')));')


def column(markup, key):
    match = re.search(r'<td\b[^>]*data-run-count="'+key+r'"[^>]*>(.*?)</td>', markup, re.S)
    assert match, f'No independently aligned {key} count column'
    return html.unescape(re.sub('<[^>]+>', '', match[1])).strip()


def test_stages_precede_usage_and_usage_is_outside_compact_identity():
    markup = render(detail([('scan', {'files_attempted': 3, 'files_succeeded': 2, 'files_failed': 1})]))
    header_end = markup.index('</header>', markup.index('run-heading'))
    assert 'run-usage' not in markup[:header_end], 'Usage still stretches the identity header'
    assert markup.index('run-stage-table') < markup.index('run-usage') < markup.index('run-section-deliveries')
    for heading in ['Input units', 'Completed', 'Failures', 'Held / other', 'What happened']:
        assert heading in markup
    assert 'file scan passes' in markup and 'No automatic edits recorded' in markup


@pytest.mark.parametrize('stage,payload,expected', [
    ('scan', {'files_attempted':3,'files_succeeded':2,'files_failed':1}, ['3','2','1','—']),
    ('mine', {'attempted':5,'succeeded':2,'failed':3,'taxonomy':{'budget_refused_this_incident':2}}, ['5','2','1','refused: 2']),
    ('cluster', {'candidates':9,'mode':'agentic_passthrough'}, ['0','0','0','—']),
    ('cluster', {'candidates':9,'merge_attempted':3,'merge_succeeded':1,'merge_failed':2}, ['3','1','2','—']),
    ('gate', {'attempted':7,'gated_pass':1,'gated_fail':2,'ungated':1,'inconclusive':1,'failed':1,'refused':1}, ['7','5','1','refused: 1']),
    ('apply', {'attempted':8,'applied':2,'held':5,'failed':1}, ['8','2','1','held: 5']),
    ('apply', {'attempted':0,'applied':0,'held':0,'failed':0}, ['0','0','0','held: 0']),
])
def test_aligned_counts_preserve_each_native_unit_and_refusal_bucket(stage, payload, expected):
    markup = render(detail([(stage, payload)]))
    assert [column(markup, key) for key in ['input','completed','failed','other']] == expected
    # Complete source counters are retained, including counts not used in the compact columns.
    assert html.escape(json.dumps(payload, indent=2), quote=True).replace('&#x27;', '&#39;') in markup
    if stage == 'gate':
        assert '5 verdicts' in markup and 'pass: 1' in markup and 'fail: 2' in markup
    if stage == 'cluster':
        assert '9 candidate rules' in markup and 'model merge calls' in markup


@pytest.mark.parametrize('payload,expected', [(None,'Not recorded'), ({'attempted':3},'unknown')])
def test_missing_accounting_never_acquires_zero_counts(payload, expected):
    markup = render(detail([('mine', payload)]))
    assert all(column(markup,k)==expected for k in ['input','completed','failed','other'])
    assert 'No stage measurement' in markup if payload is None else 'Required native outcome counters are missing' in markup


def test_run_and_retained_record_errors_keep_cause_first_and_complete_diagnostic():
    data = detail([])
    diagnostic = 'GET /api/runs/invented-run/records?cursor=' + 'x'*500 + ' answered 503: <invented>'
    for extra in [
        {'error': diagnostic, 'errorDetail': 'Invented run reader unavailable'},
        {'pages': {'deliveries': {'error':diagnostic,'errorDetail':'Invented delivery reader unavailable',
                                 'records':[],'loaded':False,'loading':False,'count':0}}},
        {'pages': {'calls': {'error':diagnostic,'errorDetail':'Invented call reader unavailable',
                            'records':[{'id':'retained-call','stage':'mine','outcome':'ok'}],
                            'loaded':True,'loading':False,'count':1}}},
    ]:
        markup = render(data, **extra)
        alert = re.search(r'<p[^>]*role="alert"[^>]*>(.*?)</p>', markup, re.S)[1]
        assert 'Could not read' in alert and 'Invented' in alert
        assert 'GET /api' not in alert
        assert '<summary' in markup and 'Request details' in markup
        assert html.escape(diagnostic, quote=True) in markup
        assert '<invented>' not in markup
        if 'calls' in extra.get('pages',{}):
            assert 'retained-call' in markup and '1 of 1 retained records shown' in markup


def test_read_detail_is_retained_and_retired_by_actual_run_loader():
    result = node('''
      const body={innerHTML:'',querySelectorAll:()=>[]};
      globalThis.document={activeElement:null,getElementById:id=>id==='run-detail'?body:null};
      const failures=[];
      globalThis.fetch=async()=>({ok:false,status:503,statusText:'Unavailable',json:async()=>({error:'Invented cause',detail:'Complete source stayed unread'})});
      await app.openRunRoute('run/invented-run');
      failures.push([app.state.runDetail.errorDetail,body.innerHTML]);
      app.state.runDetail={id:'invented-run',kind:'run',data:'''+json.dumps(detail([]))+''',pages:{}};
      await app.loadRunRecords('calls');failures.push([app.state.runDetail.pages.calls.errorDetail,body.innerHTML]);
      globalThis.fetch=async()=>({ok:true,json:async()=>({run_id:'invented-run',kind:'calls',records:[],count:0})});
      await app.loadRunRecords('calls');
      console.log(JSON.stringify({failures,retired:app.state.runDetail.pages.calls.errorDetail,html:body.innerHTML}));
    ''')
    assert all(x[0]=='Invented cause · Complete source stayed unread' for x in result['failures'])
    assert result['retired']=='' and 'Invented cause' not in result['html']


def test_artifact_inspection_preserves_diagnostic_and_retires_it_after_retry():
    result = node('''
      const body={innerHTML:'',querySelectorAll:()=>[]};
      globalThis.document={activeElement:null,getElementById:id=>id==='run-detail'?body:null};
      app.state.runDetail={id:'invented-run',kind:'run',data:'''+json.dumps(detail([]))+''',pages:{
        artifacts:{loaded:true,count:1,records:[{key:'report',label:'Run report',state:'available',bytes:8}]}}};
      globalThis.fetch=async()=>({ok:false,status:503,statusText:'Unavailable',json:async()=>({error:'Invented file unavailable',detail:'Preserve <source>'})});
      await app.inspectRunArtifact('report');
      const failed={entry:app.state.runDetail.artifactDetails.report,html:body.innerHTML};
      globalThis.fetch=async()=>({ok:true,json:async()=>({run_id:'invented-run',key:'report',bytes:8,preview_bytes:8,
        omitted_bytes:0,encoding:'utf-8',version:'v1',text:'<source>'})});
      await app.inspectRunArtifact('report');
      console.log(JSON.stringify({failed,entry:app.state.runDetail.artifactDetails.report,html:body.innerHTML}));
    ''')
    assert result['failed']['entry']['errorDetail']=='Invented file unavailable · Preserve <source>'
    alert = re.search(r'<p[^>]*role="alert"[^>]*>(.*?)</p>', result['failed']['html'], re.S)[1]
    assert 'Could not read this file.' in alert and 'GET /api' not in alert
    assert 'GET /api/runs/invented-run/artifacts/report answered 503' in result['failed']['html']
    assert result['entry']['error']=='' and not result['entry'].get('errorDetail')
    assert 'Invented file unavailable' not in result['html']
    assert '&lt;source&gt;' in result['html'] and 'Download complete file' in result['html']
