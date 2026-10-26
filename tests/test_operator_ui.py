"""Operator-facing time and outcome distinctions, using invented records."""
from contextlib import closing

from self_improve.config import Config
from self_improve.store import Store
from self_improve.dashboard import overview_data
from tests.test_dashboard_queries import _run, _learning, _proposal
from tests.test_navigation import node


def test_pacific_time_is_explicit_and_handles_day_and_dst_boundaries():
    result = node('''console.log(JSON.stringify([
      app.displayTime('2026-09-19T09:30:06.594348Z'),
      app.displayTime('2026-01-01T02:00:00Z'),
      app.displayTime('2026-11-01T08:30:00Z'),
      app.displayTime('2026-11-01T09:30:00Z'),
      app.displayTime('2026-09-19T02:30:00'),
      app.displayTime(''), app.reviewWhen({first_seen:'2026-09-19T01:00:00Z',last_seen:'2026-09-19T09:30:00Z'})
     ]));''')
    assert 'Sep 19, 2026' in result[0] and '2:30 AM PDT' in result[0]
    assert 'Dec 31, 2025' in result[1] and '6:00 PM PST' in result[1]
    assert '1:30 AM PDT' in result[2] and '1:30 AM PST' in result[3]
    assert 'time zone unknown' in result[4]
    assert result[5] == 'Time not recorded'
    assert 'Sep 18, 2026' in result[6] and 'Sep 19, 2026' in result[6] and 'PT' in result[6]


def test_overview_retains_gate_outcomes_and_separates_mining_refusals(tmp_path):
    with closing(Store(tmp_path/'state.db')) as store:
        _run(store,run_id='fixture',started='2026-09-19T09:30:00Z',stats={
            'mine':{'attempted':15,'succeeded':8,'failed':7,'taxonomy':{'MinerError':6,'budget_refused_this_incident':1,'budget_exhausted':22}},
            'gate':{'attempted':9,'gated_pass':2,'gated_fail':0,'ungated':1,'inconclusive':1,'failed':0,'refused':5}})
        latest=overview_data.latest_run(store)
        mine=next(s for s in latest['stages'] if s['name']=='mine')
        gate=next(s for s in latest['stages'] if s['name']=='gate')
        assert mine['outcomes']['processing errors']==6
        assert mine['outcomes']['stopped at call limit']==1
        assert mine['outcomes']['not attempted (call limit)']==22
        assert gate['outcomes']['waiting on budget']==5
        assert gate['outcomes']['inconclusive']==1
        assert gate['outcomes']['failed evaluation']==0


def test_overview_untitled_learning_uses_retained_rule_instead_of_id(tmp_path):
    with closing(Store(tmp_path/'state.db')) as store:
        lid=_learning(store,learning_id='internal-identifier')
        store.update('learnings','id',lid,{'title':'', 'rule_text':'Read the actual output before proposing a correction.'})
        _proposal(store,learning_id=lid,status='pending')
        result=overview_data.review_preview(store,Config())
        assert result['families'][0]['title']=='Read the actual output before proposing a correction.'


def test_review_labels_distinguish_instruction_context_and_decision():
    result=node('''
      const family={learning_id:'fixture',rule_text:'Read the actual output <tag>.',incident_summary:'A retained explanation <tag>.',
        lead_reason:'No completed evaluation',proposals:[{id:'p',reason_code:'pending'}],target_rows:[]};
      console.log(JSON.stringify(app.renderReviewCard(family)));
    ''')
    assert 'Proposed instruction' in result
    assert 'Why it was proposed' in result
    assert 'Evaluation evidence' in result
    assert 'Read the actual output &lt;tag&gt;.' in result
    assert 'A retained explanation &lt;tag&gt;.' in result
    assert result.index('Proposed instruction') < result.index('Why this needs you:')


def test_overview_refreshed_review_uses_rule_text_when_title_is_empty():
    result=node("""console.log(JSON.stringify(app.renderOverviewQueue({count:1,family_count:1,
      families:[{title:'',rule_text:'**Inspect** the `<result>` first.',learning_id:'opaque-id',proposals:[{id:'p'}]}]})));""")
    assert 'Inspect the &lt;result&gt; first.' in result
    assert 'opaque-id' not in result


def test_overview_stage_guide_sits_under_the_grid_not_in_the_latest_run_column():
    """BL-57: inside the latest-run column, the stage hint and the Mine and Gate
    guide made that column taller than the stage grid, which pushed the lower
    panels past the Overview's layout limits. They render separately, into a
    container under the grid, and only when a run is retained."""
    from pathlib import Path
    result = node('''const latest={run:{id:'run-1',started:'2030-02-07T02:00:00Z',finished:'2030-02-07T03:00:00Z',status:'ok'},
      stages:[{name:'mine',recorded:true,outcomes:{processed:4},cell:{state:'ok'}}]};
      console.log(JSON.stringify([app.renderOverviewRunSummary(latest),app.renderOverviewRunGuide(latest),app.renderOverviewRunGuide(null)]));''')
    summary, guide, without_run = result
    assert 'mine' in summary and '4 processed' in summary
    assert 'What do Mine and Gate do?' not in summary and 'Open a stage' not in summary
    assert 'Open a stage for complete evidence.' in guide and '<summary>What do Mine and Gate do?</summary>' in guide
    assert without_run == ''
    html = (Path(__file__).resolve().parents[1] / 'src/self_improve/dashboard/static/index.html').read_text()
    main = html.index('class="overview-run-main"')
    assert main < html.index('id="ov-grid"') < html.index('id="ov-run-guide"') < html.index('id="ov-latest"')
