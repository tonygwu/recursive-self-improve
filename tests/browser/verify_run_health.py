"""Observe stored producer health, refusal causes and caps through native navigation."""
from pathlib import Path
import argparse
import json
import sqlite3
from playwright.sync_api import sync_playwright,expect
from keyboard_actions import KeyboardActions
from visual_checks import VisualChecks

parser=argparse.ArgumentParser()
parser.add_argument('--inspect-only',action='store_true')
args=parser.parse_args()
ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'reports/dashboard-parity/run-health'
info=json.loads((OUT/'manifest.json').read_text())
assert 'si-run-health-' in info['db']
result={'requests':[],'errors':[],'captures':[],'runs':{}}
with sync_playwright() as runtime:
    browser=runtime.chromium.launch(headless=True)
    page=browser.new_page(viewport={'width':1280,'height':1024})
    keys=KeyboardActions(page,OUT/'keyboard.json')
    visual=VisualChecks(page,OUT/'contrast.json')
    page.on('pageerror',lambda error:result['errors'].append(str(error)))
    page.on('request',lambda request:result['requests'].append([request.method,request.url]))

    def capture(name,target):
        target.scroll_into_view_if_needed()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        visual.check(name)
        page.screenshot(path=str(OUT/(name+'.png')))
        result['captures'].append(name)

    def themes(name,target):
        for width in (1280,1440):
            page.set_viewport_size({'width':width,'height':1024})
            for theme in ('light','dark'):
                keys.theme(theme)
                capture(f'{name}-{theme}-{width}',target)

    def choose(name):
        control=page.locator('#run-selector')
        keys.reach(control)
        assert sum(label.startswith(name[0]) for label in control.locator('option').all_text_contents())==1
        page.keyboard.press(name[0]);page.keyboard.press('Tab')
        expect(control).to_have_value(name)
        expect(page.locator('.run-heading__summary')).to_contain_text(name)
        keys.steps.append({'action':'native-select-prefix','value':name});keys.save()

    try:
        page.goto('http://127.0.0.1:8876/#/overview/run/refused-run')
        page.wait_for_load_state('networkidle')
        page.screenshot(path=str(OUT/'initial.png'))
        (OUT/'initial.html').write_text(page.content())
        result['initial_buttons']=page.locator('button:visible').all_text_contents()
        result['initial_summaries']=page.locator('summary:visible').all_text_contents()
        if args.inspect_only:
            print(json.dumps({'buttons':result['initial_buttons'],'summaries':result['initial_summaries'],
                'stage':page.locator('#run-stage-mine').inner_text(),'heading':page.locator('.run-heading').inner_text()}))
        else:
            states={'refused-run':'refused','failure-run':'failed','success-run':'budget_exhausted',
                    'mixed-run':'partial','all-failed-run':'failed'}
            for name,record in info['runs'].items():
                if name!='refused-run':choose(name)
                expected='degraded' if name in ('failure-run','all-failed-run') else 'ok'
                assert record['status']==expected
                expect(page.locator('.run-heading__summary')).to_contain_text('Status: '+expected)
                expect(page.locator('#run-stage-mine .run-dot')).to_have_attribute('data-state',states[name])
                stats=record['stats'];mine=stats['mine'];refused=mine['taxonomy'].get('budget_refused_this_incident',0)
                execution_failed=mine['failed']-refused
                report=record['report'].split('## Appendix')[0]
                assert f'| Mining execution failures (excluding explicit refusals) | {execution_failed} |' in report
                assert f'| Mining attempts refused at call cap | {refused} |' in report
                detail=page.request.get('http://127.0.0.1:8876/api/runs/'+name).json()
                assert detail['run']['id']==name and detail['calls']['reconciled'] is True
                assert detail['calls']['recorded']==len(record['calls'])
                keys.activate(page.locator('#run-causes-mine'))
                expect(page.locator('#run-stage-mine')).to_contain_text('budget_refused_this_incident' if refused else 'call_failed:spawn_error')
                themes(name+'-causes',page.locator('#run-stage-mine'))
                keys.activate(page.locator('#run-budget-summary'))
                cheap=page.locator('.run-budget-table tr').filter(has=page.get_by_role('rowheader',name='cheap',exact=True))
                expect(cheap).to_contain_text(f"{stats['llm']['calls_made']['cheap']} / {stats['budget_limits']['cheap']}")
                expect(cheap.locator('td').last).to_have_text(str(stats['llm']['refused']['cheap']))
                themes(name+'-budget',page.locator('#run-budget-summary'))
                page.reload();page.wait_for_load_state('networkidle')
                expect(page.locator('#run-selector')).to_have_value(name)
                expect(page.locator('.run-heading__summary')).to_contain_text('Status: '+expected)
                result['runs'][name]={'status':expected,'mine_state':states[name],'execution_failed':execution_failed,
                                      'refused':refused,'calls':len(record['calls'])}
            assert not result['errors'],result['errors']
            assert all(method=='GET' for method,url in result['requests'])
            with sqlite3.connect('file:'+info['db']+'?mode=ro',uri=True) as db:
                assert list(db.iterdump())==info['sql']
                assert db.execute('SELECT COUNT(*) FROM llm_calls').fetchone()[0]==info['scripted_calls']
            assert all(Path(path).read_text()==text for path,text in info['targets'].items())
            visual.assert_clean()
            result.update(store='unchanged',targets='unchanged',paid_calls=0)
            print('RUN_HEALTH_BROWSER_OK: five actual pipeline outcomes; real budgets; native selection/reload; 40 themed captures; unchanged Store/targets; no paid calls')
    except BaseException:
        page.screenshot(path=str(OUT/'failure.png'))
        (OUT/'failure.html').write_text(page.content())
        raise
    finally:
        keys.save();browser.close()
        (OUT/'result.json').write_text(json.dumps(result,indent=2))
