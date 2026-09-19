"""Actual read-only Project, Review, Rules and mining evidence journeys."""
from pathlib import Path
from urllib.parse import quote
import json
import sqlite3
from playwright.sync_api import sync_playwright,expect
from keyboard_actions import KeyboardActions
from visual_checks import VisualChecks

ROOT=Path(__file__).resolve().parents[2];OUT=ROOT/'reports/dashboard-parity/incident-evidence'
manifest=json.loads((OUT/'manifest.json').read_text());assert 'si-incident-ui-' in manifest['db']
requests=[];errors=[]
with sync_playwright() as runtime:
    browser=runtime.chromium.launch(headless=True)
    page=browser.new_page(viewport={'width':1440,'height':1024})
    page.on('pageerror',lambda e:errors.append(str(e)))
    page.on('request',lambda r:requests.append([r.method,r.url]))
    keys=KeyboardActions(page,OUT/'keyboard.json');visual=VisualChecks(page,OUT/'contrast.json')
    def go(route):
        page.goto('http://127.0.0.1:8876/'+route);page.wait_for_load_state('networkidle')
    def primary(scope):
        expect(scope.locator('[data-incident-readable]').filter(has_text='Invented readable repeated error').first).to_be_visible()
        expect(scope).to_contain_text('Detector fingerprint:')
    try:
        # Hold an actual read response while the operator tabs to an inactive tab.
        held=[]
        page.route('**/api/evidence/incident/i06',lambda route:held.append(route))
        page.goto('http://127.0.0.1:8876/#/rules/evidence/incident/i06?mode=evidence')
        source_tab=page.get_by_role('link',name='Source',exact=True)
        keys.reach(source_tab)
        assert len(held)==1
        response=page.request.get('http://127.0.0.1:8876/api/evidence/incident/i06')
        held.pop().fulfill(response=response)
        expect(page.locator('#inspector-body [data-evidence-detail-refresh]')).to_be_enabled()
        expect(source_tab).to_be_focused()
        expect(source_tab).not_to_have_attribute('aria-current','page')
        keys.activate(source_tab)
        expect(source_tab).to_have_attribute('aria-current','page')
        primary(page.locator('#inspector-body'))
        page.unroute('**/api/evidence/incident/i06')
        go('#/projects/'+quote(manifest['project_key'],safe=''))
        keys.activate(page.get_by_text('Evidence from this project',exact=True))
        keys.activate(page.get_by_role('button',name='Read evidence',exact=True))
        area=page.locator('#project-detail-body')
        primary(area);expect(area).to_contain_text('Recurred 5 time(s) across 2 session(s)')
        expect(area).to_contain_text('Time unknown for 1 occurrence entries')
        keys.activate(page.get_by_role('button',name='Load more evidence',exact=True))
        expect(page.locator('[id="project-status-evidence:l00"]')).to_contain_text('25 shown · 25 retained')
        expect(page.locator('[id="project-status-evidence:l00"]')).to_be_focused()
        record=page.locator('.project-record').filter(has=page.locator('[data-incident-readable]').filter(has_text='Invented readable repeated error')).last
        keys.activate(record.get_by_text('Complete retained incident and archived window',exact=True))
        expect(record).to_contain_text('COMPLETE OCCURRENCE END')
        page.screenshot(path=str(OUT/'project.png'))
        previews=[]
        page.route('**/api/review-preview?*',lambda route:previews.append(route))
        page.goto('http://127.0.0.1:8876/#/review')
        examples=page.locator('summary').filter(has_text='What actually happened').first
        keys.reach(examples)
        assert len(previews)==1
        pending=previews.pop()
        pending.fulfill(response=page.request.get(pending.request.url))
        expect(page.locator('[data-review-individual]').first).to_be_enabled()
        page.wait_for_function("async()=>{const {state}=await import('/app.js');return !state.reviewPreviews[state.selectedFamily]?.loading}")
        expect(examples).to_be_focused()
        page.unroute('**/api/review-preview?*')
        keys.activate(examples)
        expect(page.locator('.review-incident__text').first).to_have_text('Invented readable repeated error')
        expect(page.locator('.review-incidents')).to_contain_text('Recurred 5 time(s) across 2 session(s)')
        keys.activate(page.locator('[data-review-individual]').first)
        keys.activate(page.locator('summary').filter(has_text='All retained evidence').first)
        evidence=page.locator('details[data-review-key="p00:i06"]')
        keys.activate(evidence.locator('summary').first)
        primary(evidence)
        expect(evidence).to_contain_text('0 occurrences')
        expect(evidence).to_contain_text(manifest['window'][2]['session_file'])
        expect(evidence).to_contain_text('COMPLETE OCCURRENCE END')
        visual.themes(keys,'review-evidence',OUT,target=evidence.locator('summary').first)
        go('#/rules/evidence/incident/i06?mode=evidence&tab=source')
        inspector=page.locator('#inspector-body');primary(inspector)
        expect(inspector).to_contain_text('Recurred 5 time(s) across 2 session(s)')
        long=inspector.locator('.evidence-window pre.evidence-text').last
        keys.reach(long);page.keyboard.press('End')
        expect(long).to_contain_text('COMPLETE OCCURRENCE END')
        visual.themes(keys,'complete-occurrence',OUT,target=long)
        page.reload();page.wait_for_load_state('networkidle');primary(inspector)
        go('#/review/mine/unmined')
        area=page.locator('#eval-job-body');primary(area)
        expect(area).to_contain_text('Maximum: 1 logical model call')
        keys.activate(page.get_by_role('button',name='Inspect complete mining input',exact=True))
        expect(area).to_contain_text('COMPLETE OCCURRENCE END')
        expect(area).to_contain_text(manifest['window'][2]['session_file'])
        page.screenshot(path=str(OUT/'mining.png'))
        with sqlite3.connect('file:'+manifest['db']+'?mode=ro',uri=True) as conn:
            assert list(conn.iterdump())==manifest['snapshot']
            assert conn.execute('SELECT COUNT(*) FROM llm_calls').fetchone()[0]==0
        assert all(Path(p).read_bytes().hex()==v for p,v in manifest['targets'].items())
        assert all(method=='GET' for method,_ in requests) and not errors,errors
        (OUT/'requests.json').write_text(json.dumps(requests))
        print('INCIDENT_EVIDENCE_OK: Project 20+5 pages, Review examples/frozen evidence, Rules complete source, mining preview/full input; native keyboard, both themes, unchanged Store/targets, zero models')
    finally:
        (OUT/'errors.json').write_text(json.dumps(errors))
        (OUT/'last.html').write_text(page.content())
        page.screenshot(path=str(OUT/'last.png'))
        browser.close()
