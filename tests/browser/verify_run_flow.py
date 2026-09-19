"""Actual Run accounting, exact rule links, paging, and read-only navigation."""
from pathlib import Path
import argparse
import json
import sqlite3
from playwright.sync_api import sync_playwright, expect
from keyboard_actions import KeyboardActions
from visual_checks import VisualChecks

parser=argparse.ArgumentParser()
parser.add_argument('--inspect-only',action='store_true')
args=parser.parse_args()
ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'reports/dashboard-parity/run-flow'
info=json.loads((OUT/'manifest.json').read_text())
assert 'si-run-flow-' in info['db']
BASE='http://127.0.0.1:8876/'
result={'requests':[],'errors':[],'captures':[]}
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
        labels=control.locator('option').all_text_contents()
        assert len([label for label in labels if label.startswith(name[0])]) == 1
        # A native selection commits on change and reloads the exact run.
        # Type one unique ID prefix, not the timestamp shared by every option.
        page.keyboard.press(name[0])
        page.keyboard.press('Tab')
        expect(control).to_have_value(name)
        keys.steps.append({'action':'native-select-prefix','value':name})
        keys.save()
        expect(page.locator('.run-heading__summary')).to_contain_text(name)

    try:
        page.goto(BASE+'#/overview/run/selected')
        page.wait_for_load_state('networkidle')
        page.screenshot(path=str(OUT/'initial.png'))
        (OUT/'initial.html').write_text(page.content())
        result['initial_buttons']=page.locator('button:visible').all_text_contents()
        result['initial_summaries']=page.locator('summary:visible').all_text_contents()
        if args.inspect_only:
            print(json.dumps({'buttons':result['initial_buttons'],'summaries':result['initial_summaries'],
                              'stages':page.locator('.run-stage-table').inner_text()}))
        else:
            gate=page.locator('#run-stage-gate')
            expect(gate).to_contain_text('3 attempts have no recorded outcome')
            expect(gate).not_to_contain_text('Budget refused')
            expect(page.locator('#run-stage-apply')).to_contain_text('Recorded outcomes exceed attempts by 2')
            summary=page.locator('#run-stage-cluster > td details > summary')
            keys.activate(summary)
            expect(page.locator('#run-stage-cluster')).to_contain_text('model merge calls')
            expect(page.locator('#run-stage-cluster')).to_contain_text('older pending work')
            themes('native-accounting',page.locator('#run-stage-cluster'))
            keys.activate(summary)
            mine_summary=page.locator('#run-stage-mine > td details > summary').last
            keys.activate(mine_summary)
            expect(page.locator('#run-stage-mine')).to_contain_text('1 entries in the failed bucket are recorded budget refusals')
            keys.activate(mine_summary)
            flow=page.locator('#run-rule-flow-summary')
            keys.activate(flow)
            expect(flow.locator('..')).to_contain_text('24 retained mining observations of 22 rules')
            expect(flow.locator('..')).to_contain_text('3 proposals were created by this run for 2 rules')
            expect(flow.locator('..')).to_contain_text('duplicate: 1')
            themes('relationships',flow.locator('..'))
            keys.reach(flow)
            page.evaluate('async()=>{const app=await import("/app.js");await app.loadRunRecords("calls");}')
            expect(flow).to_be_focused()
            expect(flow.locator('..')).to_have_attribute('open','')
            keys.activate(page.get_by_role('button',name='Inspect exact rule and proposal links',exact=True))
            groups=page.locator('#run-section-learnings')
            expect(page.locator('#run-status-learnings')).to_have_text('20 of 23 retained records shown')
            expect(groups.locator('.run-record')).to_have_count(20)
            expect(groups.locator('a[href="#/rules/b"]')).to_have_count(1)
            expect(groups.locator('a[href="#/review/proposal/c-one"]')).to_have_count(1)
            expect(groups.locator('a[href="#/review/proposal/c-two"]')).to_have_count(1)
            expect(groups).to_contain_text('immutable records of this run')
            expect(groups).to_contain_text('current and may reflect later work')
            themes('rule-links',groups.locator('.run-record').first)
            keys.activate(page.locator('#run-older-learnings'))
            expect(page.locator('#run-status-learnings')).to_have_text('23 of 23 retained records shown')
            expect(page.locator('#run-status-learnings')).to_be_focused()
            expect(groups.locator('.run-record')).to_have_count(23)
            keys.activate(groups.locator('a[href="#/rules/b"]'))
            page.wait_for_load_state('networkidle')
            assert page.url.endswith('#/rules/b')
            expect(page.locator('#main')).to_contain_text('Invented retained rule b')
            page.go_back();page.wait_for_load_state('networkidle')
            keys.activate(page.locator('#run-load-learnings'))
            keys.activate(page.locator('#run-section-learnings a[href="#/review/proposal/c-two"]'))
            page.wait_for_load_state('networkidle')
            assert page.url.endswith('#/review/proposal/c-two')
            expect(page.locator('#main')).to_contain_text('c-two')
            page.go_back();page.wait_for_load_state('networkidle')
            choose('neighbor')
            keys.activate(page.locator('#run-rule-flow-summary'))
            expect(page.locator('#run-rule-flow-summary').locator('..')).to_contain_text('1 retained mining observations of 1 rules')
            expect(page.locator('#run-stage-gate')).not_to_contain_text('no recorded outcome')
            page.reload();page.wait_for_load_state('networkidle')
            expect(page.locator('.run-heading__summary')).to_contain_text('neighbor')
            choose('missing-outcomes')
            expect(page.locator('#run-stage-gate')).to_contain_text('Required native outcome counters are missing')
            expect(page.locator('#run-stage-gate')).not_to_contain_text('Budget refused')
            themes('missing-outcomes',page.locator('#run-stage-gate'))
            choose('explicit-refusal')
            expect(page.locator('#run-stage-gate .run-dot')).to_have_attribute('data-state','refused')
            expect(page.locator('#run-stage-gate')).to_contain_text('refused: 3')
            choose('selected')
            delayed=[]
            pattern='**/api/runs/selected/records?kind=learnings*'
            page.route(pattern,lambda route:delayed.append(route))
            keys.activate(page.locator('#run-load-learnings'))
            expect(page.locator('#run-section-learnings')).to_contain_text('Loading records')
            assert delayed
            choose('neighbor')
            delayed[0].fulfill(status=200,json={'run_id':'selected','kind':'learnings','records':[],
                'count':999,'reason':'invented obsolete response','next_cursor':None})
            page.wait_for_load_state('networkidle')
            expect(page.locator('#run-detail')).not_to_contain_text('invented obsolete response')
            expect(page.locator('.run-heading__summary')).to_contain_text('neighbor')
            assert not result['errors'],result['errors']
            assert all(method=='GET' for method,url in result['requests'])
            with sqlite3.connect('file:'+info['db']+'?mode=ro',uri=True) as db:
                assert list(db.iterdump()) == info['sql']
                assert db.execute('SELECT COUNT(*) FROM llm_calls').fetchone()[0] == 0
            assert all(Path(path).read_text()==text for path,text in info['targets'].items())
            visual.assert_clean()
            result.update(store='unchanged',targets='unchanged',provider_executions=0)
            print('RUN_FLOW_BROWSER_OK: native accounting, 23 exact groups, paging, native focus, rule/proposal links, same-time selection/reload, delayed response excluded; unchanged Store/targets; zero provider executions')
    except BaseException:
        page.screenshot(path=str(OUT/'failure.png'))
        (OUT/'failure.html').write_text(page.content())
        raise
    finally:
        keys.save()
        browser.close()
        (OUT/'result.json').write_text(json.dumps(result,indent=2))
