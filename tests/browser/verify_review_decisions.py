"""Read-only native Review decision hierarchy, with invented retained evidence."""
from pathlib import Path
from urllib.parse import quote
import json
import sqlite3

from playwright.sync_api import sync_playwright, expect
from keyboard_actions import KeyboardActions
from visual_checks import VisualChecks

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'reports/dashboard-parity/review-decisions'
info = json.loads((OUT / 'manifest.json').read_text())
assert 'si-review-decisions-' in info['db']
report = {'requests': [], 'errors': [], 'captures': [], 'models': 0}
with sync_playwright() as runtime:
    browser = runtime.chromium.launch()
    page = browser.new_page(viewport={'width': 1280, 'height': 1024})
    page.on('request', lambda r: report['requests'].append([r.method, r.url]))
    page.on('pageerror', lambda e: report['errors'].append(str(e)))
    visual = VisualChecks(page, OUT / 'contrast.json')
    keys = KeyboardActions(page, OUT / 'keyboard.json')

    def go(fragment):
        page.goto('http://127.0.0.1:8876/' + fragment)
        page.wait_for_load_state('networkidle')

    def capture(name):
        page.screenshot(path=str(OUT / (name + '.png')))
        report['captures'].append(name)
        visual.check(name)

    def decision_geometry():
        return page.locator('.review-detail [data-decision]').evaluate_all('''els=>els.map(el=>{
          const r=el.getBoundingClientRect();return {action:el.dataset.decision,top:r.top,bottom:r.bottom,
          disabled:el.disabled,visible:el.checkVisibility()};})''')

    def result_body(result):
        return page.locator('[id="review-evaluation-' + result + '"] > details')

    def unobscured(target):
        keys.focused(target)
        assert target.evaluate('''el=>{const r=el.getBoundingClientRect();
          const hit=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);
          return hit===el || el.contains(hit);}''')

    def reset():
        go(family)
        page.reload()
        page.wait_for_load_state('networkidle')

    try:
        family = '#/review/family/' + quote(info['family'])
        go(family)
        (OUT / 'inspected.html').write_text(page.content())
        capture('initial')
        geometry = decision_geometry()
        report['initial_decisions'] = geometry
        assert len(geometry) == 3 and all(r['visible'] and 0 <= r['top'] < r['bottom'] <= 1024 for r in geometry), geometry
        # Initial ordering is the selected preview's order, never verdict or load order.
        ordered = page.evaluate('''async()=>{const a=await import('/app.js');
          return [...new Set(a.state.reviewPreviews[a.state.reviewDetailFamily].data.members.map(m=>m.snapshot.evaluation?.id).filter(Boolean))];}''')
        assert page.locator('.review-detail-evaluation').evaluate_all('els=>els.map(el=>el.id.replace("review-evaluation-",""))') == ordered
        assert result_body(ordered[0]).evaluate('el=>el.open')
        assert not result_body(ordered[1]).evaluate('el=>el.open')
        for width in (1280, 1440):
            page.set_viewport_size({'width': width, 'height': 1024})
            for theme in ('light', 'dark'):
                keys.theme(theme)
                page.locator('#main').evaluate('el=>el.scrollTop=0')
                capture(f'initial-{theme}-{width}')
                assert all(0 <= r['top'] < r['bottom'] <= 1024 for r in decision_geometry())
                assert page.locator('.review-detail-grid').evaluate('el=>el.scrollWidth<=el.clientWidth')
        # Result disclosure exposes every retained trial and identity through native keys.
        body = result_body(info['result'])
        summary = body.locator(':scope > summary')
        if not body.evaluate('el=>el.open'):
            keys.activate(summary, 'Space')
        keys.reach(summary)
        unobscured(summary)
        page.evaluate('async()=>{(await import("/app.js")).paintReview()}')
        expect(summary).to_be_focused()
        assert body.evaluate('el=>el.open')
        expect(body).to_contain_text('3 / 3 passed with the rule; 0 / 3 without it')
        expect(body.locator('.review-detail-trials li')).to_have_count(6)
        trial = body.locator('.review-detail-trials summary').first
        keys.activate(trial, 'Enter')
        expect(trial.locator('..')).to_contain_text('returncode')
        page.evaluate('async()=>{(await import("/app.js")).paintReview()}')
        unobscured(trial)
        capture('complete-trial')
        source = body.get_by_text('Producing run, specification and source', exact=True)
        keys.activate(source, 'Space')
        expect(source.locator('..')).to_contain_text(info['attempt'])
        capture('complete-source')
        keys.activate(body.get_by_role('link', name='All scenarios, transcripts and provenance', exact=True))
        page.wait_for_load_state('networkidle')
        expect(page.locator('#evaluation-detail')).to_contain_text(info['attempt'])
        reset()
        # A single selected result retains the same scope and complete evaluation.
        go('#/review/proposal/' + info['proposals'][0])
        go(family)
        expect(page.locator('.review-detail-evaluation')).to_have_count(1)
        expect(page.locator('[data-decision="approve"]')).to_contain_text('Approve selected (1)')
        capture('single-selected-result')
        reset()
        # The three decisions are adjacent in DOM and native Tab order. Never activate them here.
        approve = page.locator('[data-decision="approve"]')
        target = page.locator('[data-decision="reject"]')
        lesson = page.locator('[data-decision="reject_lesson"]')
        keys.reach(approve)
        unobscured(approve)
        page.keyboard.press('Tab')
        unobscured(target)
        page.keyboard.press('Tab')
        unobscured(lesson)
        page.keyboard.press('Tab')
        individual = page.locator('[data-review-individual]')
        unobscured(individual)
        keys.activate(individual, 'Space')
        expect(individual).to_have_attribute('aria-expanded', 'true')
        capture('decision-keyboard')
        # Read failures and stale content are visible even for a secondary result.
        page.route('**/api/eval-results/' + ordered[1], lambda r: r.fulfill(status=503, json={'detail': 'Invented secondary evidence unavailable'}))
        reset()
        expect(page.get_by_role('alert').filter(has_text='Invented secondary evidence unavailable')).to_be_visible()
        expect(approve).to_be_enabled()
        capture('secondary-read-error')
        page.unroute('**/api/eval-results/' + ordered[1])
        retry = page.get_by_role('button', name='Retry evaluation evidence', exact=True)
        keys.activate(retry)
        expect(page.locator('[id="review-evaluation-' + ordered[1] + '"]')).to_be_focused()
        expect(result_body(ordered[1])).to_be_visible()
        assert result_body(ordered[1]).evaluate('el=>el.open')
        capture('secondary-retry')
        def stale(route):
            response = route.fetch()
            data = response.json()
            data['record']['verdict'] = 'changed-read'
            route.fulfill(response=response, json=data)
        page.route('**/api/eval-results/' + ordered[1], stale)
        reset()
        expect(page.get_by_role('alert').filter(has_text='Evaluation evidence changed')).to_be_visible()
        expect(page.locator('.review-detail-evaluation')).not_to_contain_text(['changed-read', 'changed-read'])
        capture('secondary-stale')
        page.unroute('**/api/eval-results/' + ordered[1])
        # Pending reads are explicit; completion preserves unrelated focus and expansion.
        reset()
        held = []
        page.route('**/api/eval-results/' + ordered[1], lambda r: held.append((r, r.fetch())))
        page.evaluate('async id=>{const a=await import("/app.js");a.loadReviewEvaluation(id,{refresh:true});a.paintReview()}', ordered[1])
        expect(page.get_by_text('Reading exact evaluation provenance…', exact=True)).to_be_visible()
        remaining = result_body(ordered[0]).locator(':scope > summary')
        keys.reach(remaining)
        capture('secondary-pending')
        assert held
        held[0][0].fulfill(response=held[0][1])
        expect(result_body(ordered[1]).locator(':scope > summary')).to_be_visible()
        unobscured(remaining)
        page.unroute('**/api/eval-results/' + ordered[1])
        # Large selections grow naturally; every outcome and missing result stays visible.
        go('#/review/family/' + quote(info['many']))
        expect(page.locator('.review-detail-evaluation')).to_have_count(8)
        expect(page.locator('[data-review-panel="evaluations"]')).to_contain_text('1 selected proposal has no linked evaluation')
        summaries = page.locator('.review-evaluation-body > summary')
        expect(summaries).to_have_count(8)
        for verdict in ('gated_pass', 'gated_fail', 'ungated', 'unknown'):
            assert sum((' · ' + verdict + ' · ') in text for text in summaries.locator('strong').all_text_contents()) == 2
        for summary in summaries.all():
            expect(summary).to_be_visible()
        capture('many-mixed-results')
        long_result = page.locator('.review-detail-evaluation').filter(has=page.locator('[id*="-long-identity"]')).first
        long_body = long_result.locator(':scope > details')
        if not long_body.evaluate('el=>el.open'):
            keys.activate(long_body.locator(':scope > summary'))
        exact = long_body.get_by_text('1 selected proposal · exact result and members', exact=True)
        keys.activate(exact, 'Space')
        expect(exact.locator('..')).to_contain_text('-long-identity' * 18)
        page.set_viewport_size({'width': 760, 'height': 1024})
        for narrow_theme in ('light', 'dark'):
            keys.theme(narrow_theme)
            go('#/review/family/' + quote(info['many']))
            long_body = page.locator('.review-detail-evaluation').filter(has=page.locator('[id*="-long-identity"]')).first.locator(':scope > details')
            if not long_body.evaluate('el=>el.open'):
                keys.activate(long_body.locator(':scope > summary'))
            exact = long_body.get_by_text('1 selected proposal · exact result and members', exact=True)
            if not exact.locator('..').evaluate('el=>el.open'):
                keys.activate(exact, 'Space')
            keys.reach(exact)
            exact.locator('..').scroll_into_view_if_needed()
            unobscured(exact)
            capture('narrow-long-identities-' + narrow_theme)
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            assert page.locator('.review-detail-grid').evaluate('el=>el.scrollWidth<=el.clientWidth')
            keys.reach(approve)
            unobscured(approve)
            page.keyboard.press('Tab')
            unobscured(target)
            page.keyboard.press('Tab')
            unobscured(lesson)
            capture('narrow-decisions-' + narrow_theme)
            reset()
            body = result_body(info['result'])
            if not body.evaluate('el=>el.open'):
                keys.activate(body.locator(':scope > summary'), 'Space')
            trial = body.locator('.review-detail-trials summary').first
            keys.activate(trial)
            expect(trial.locator('..')).to_contain_text('returncode')
            unobscured(trial)
            capture('narrow-trial-' + narrow_theme)
            source = body.get_by_text('Producing run, specification and source', exact=True)
            keys.activate(source, 'Space')
            expect(source.locator('..')).to_contain_text(info['attempt'])
            unobscured(source)
            capture('narrow-source-' + narrow_theme)
        # Forced colors uses actual native focus, with the complete decision group visible.
        reset()
        page.set_viewport_size({'width': 1280, 'height': 1024})
        page.emulate_media(forced_colors='active')
        keys.reach(lesson)
        unobscured(lesson)
        capture('forced-colors-decisions')
        page.emulate_media(forced_colors='none')
        assert not report['errors']
        assert all(method == 'GET' for method, url in report['requests'])
        with sqlite3.connect('file:' + info['db'] + '?mode=ro', uri=True) as db:
            assert list(db.iterdump()) == info['snapshot']
        assert all(Path(path).read_text() == before for path, before in info['targets'].items())
        visual.assert_clean()
        report.update(store='unchanged', targets='unchanged')
        print('REVIEW_DECISIONS_OK', json.dumps({**report, 'requests': len(report['requests'])}), flush=True)
    finally:
        (OUT / 'report.json').write_text(json.dumps(report, indent=2))
        page.screenshot(path=str(OUT / 'last-state.png'))
        (OUT / 'last-state.html').write_text(page.content())
        browser.close()
