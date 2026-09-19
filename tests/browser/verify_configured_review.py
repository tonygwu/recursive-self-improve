"""Read configured manual actions with native keyboard navigation in both themes."""
from pathlib import Path
import json
import sqlite3
from playwright.sync_api import sync_playwright, expect
from keyboard_actions import KeyboardActions
from visual_checks import VisualChecks

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'reports/dashboard-parity/configured-review'
m = json.loads((OUT / 'manifest.json').read_text())
assert Path(m['root']).name.startswith('si-configured-review-')
labels = {'add':'instruction addition', 'edit':'instruction edit', 'delete':'instruction deletion',
          'new_skill':'new skill', 'new_rule_file':'new rule file'}
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width':1280, 'height':1024})
    keys = KeyboardActions(page, OUT / 'keys.json')
    visual = VisualChecks(page, OUT / 'contrast.json')
    errors, requests, seen = [], [], []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.on('request', lambda r: requests.append((r.method, r.url)))
    try:
        page.goto('http://127.0.0.1:8876/#/review'); page.wait_for_load_state('networkidle')
        page.screenshot(path=str(OUT / 'before.png')); (OUT / 'before.html').write_text(page.content())
        expect(page.locator('#review-count')).to_have_text('5')
        for label in labels.values():
            expect(page.locator('.review-carveouts')).to_contain_text('1 ' + label + ' waiting')
        assert '(an action this dashboard has no name for)' not in page.locator('#review-body').inner_text()
        keys.activate(page.locator('#skip-to-main')); page.keyboard.press('k')
        card = page.locator('.review-card--selected')
        for index in range(5):
            page.locator('[data-decision="approve"]:not([disabled])').wait_for()
            page.wait_for_load_state('networkidle'); keys.focused(card)
            action = m['families'][card.get_attribute('data-learning-id')]; seen.append(action)
            expect(card.locator('.review-card__why')).to_contain_text('configuration requires manual review')
            assert f'review-card__why--{action}' in card.locator('.review-card__why').get_attribute('class')
            for theme in ('light', 'dark'):
                keys.theme(theme)
                # Capture preparation only: Tab traversal to the theme button
                # can leave the long page scrolled below the selected heading.
                card.locator('.review-card__why').scroll_into_view_if_needed()
                visual.check('configured-' + action)
                page.screenshot(path=str(OUT / f'{action}-{theme}.png'))
            # Theme traversal moved focus; re-enter the queue through native keys.
            keys.activate(page.locator('#skip-to-main'))
            if index < 4: page.keyboard.press('j')
        assert set(seen) == set(labels)
        assert not errors and all(method == 'GET' for method, _ in requests)
        with sqlite3.connect('file:' + m['db'] + '?mode=ro', uri=True) as db:
            assert list(db.iterdump()) == m['snapshot']
            assert db.execute('SELECT COUNT(*) FROM llm_calls').fetchone()[0] == 0
        for path, saved in m['targets'].items():
            target = Path(path); assert target.exists() == saved['exists']
            if target.exists(): assert target.read_text() == saved['content']
        visual.assert_clean()
        result = {'actions':seen, 'captures':10, 'requests':len(requests), 'errors':errors,
                  'model_calls':0, 'store':'unchanged', 'targets':'unchanged'}
        (OUT / 'result.json').write_text(json.dumps(result, indent=2))
        print('CONFIGURED_REVIEW_OK', json.dumps(result))
    except BaseException:
        page.screenshot(path=str(OUT / 'failure.png')); (OUT / 'failure.html').write_text(page.content()); raise
    finally:
        browser.close()
