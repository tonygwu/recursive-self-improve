"""Time populated dashboard routes without private data, models or write requests."""
import argparse
import json
import re
from pathlib import Path
import sys
import time
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from playwright.sync_api import sync_playwright, expect
from tests.dashboard_performance_fixture import OUT, install_guard, unchanged

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--samples', type=int, default=5, help='Other values are diagnostic only')
args = parser.parse_args()
if args.samples < 1: parser.error('--samples must be positive')
install_guard()
manifest = json.loads((OUT / 'manifest.json').read_text())
assert 'si-performance-ui-' in manifest['db']
BASE = 'http://127.0.0.1:8876/'
routes = [
    ('overview', '#/overview', '#ov-statusline', '100 waiting on you'),
    ('rules', '#/rules', '#rules-meta', '5,001 matching rules'),
    ('rule-detail', '#/rules/' + manifest['rule_id'], '#inspector-body', 'Inspect invented result 0'),
    ('review', '#/review', '#review-count', '100'),
    ('review-detail', '#/review/family/' + manifest['rule_id'], '.review-detail', '10 retained incidents'),
    ('projects', '#/projects', '#projects-page-status', '101'),
    ('project-detail', '#/projects/' + quote(manifest['project_key'], safe=''), '#project-detail-body', 'Observed instruction size'),
    ('evals', '#/evals', '#trends-body', 'Signals over time'),
    ('run-detail', '#/overview/run/' + manifest['run_id'], '.run-heading__summary > .mono', manifest['run_id']),
]
result = {'status':'attempted','diagnostic_only':args.samples != 5,'manifest':manifest,'routes':[], 'errors':[], 'requests':[]}
dest = OUT / f'browser-{args.samples}.json'
def save(): dest.write_text(json.dumps(result, indent=2) + '\n')
with sync_playwright() as runtime:
    browser = runtime.chromium.launch(headless=True)
    try:
        for name, route, selector, text in routes:
            context = browser.new_context(viewport={'width':1440,'height':1024})
            page = context.new_page()
            page.set_default_timeout(120000)
            page.set_default_navigation_timeout(120000)
            page.on('pageerror', lambda error: result['errors'].append(str(error)))
            page.on('request', lambda request: result['requests'].append([request.method,request.url]))
            record = {'name':name,'route':route,'reads':[]}; result['routes'].append(record); save()
            try:
                for sample in range(args.samples+1):
                    before = len(result['requests']); start = time.perf_counter()
                    if sample == 0: page.goto(BASE+route)
                    else: page.reload()
                    expect(page.locator(selector)).to_contain_text(text, timeout=120000)
                    if name == 'review-detail':
                        identities = page.locator('.review-detail-incident [data-source-identity]')
                        expect(identities).to_have_count(3, timeout=120000)
                        for identity in identities.all():
                            expect(identity).to_contain_text('Codex')
                            expect(identity.locator('a').first).to_have_attribute('href', re.compile(r'^#/projects/remote%3Aexample.test%2Fperformance%2Fproject-'))
                        expect(page.locator('[data-decision="approve"]')).to_be_enabled(timeout=120000)
                    elapsed = time.perf_counter()-start
                    page.wait_for_load_state('networkidle')
                    record['reads'].append({'kind':'first' if sample==0 else 'warm_reload','seconds':elapsed,
                                            'requests':len(result['requests'])-before})
                    save()
                    expect(page.locator('#global-error')).not_to_be_visible()
                    if name=='review':
                        expect(page.locator('#nav-inbox-count')).to_have_text('100')
                        expect(page.get_by_role('button', name='Approve', exact=True)).to_be_enabled()
                    if sample == 0:
                        page.screenshot(path=str(OUT / (name+'-initial.png')))
                        (OUT / (name+'-initial.html')).write_text(page.content())
                record['budget_pass'] = all(row['seconds']<=5 for row in record['reads'])
                for width in (1280,1440):
                    page.set_viewport_size({'width':width,'height':1024})
                    for theme in ('light','dark'):
                        current = page.evaluate('async()=>(await import("/app.js")).effectiveTheme()')
                        if current != theme:
                            page.get_by_role('button',name=theme.title()+' theme',exact=True).click()
                        page.locator('#main').evaluate('el=>el.scrollTop=0')
                        page.screenshot(path=str(OUT / f'{name}-{theme}-{width}.png'))
                print('PERF_PAGE', name, json.dumps(record), flush=True); save()
            except Exception as error:
                result.update(status='failed', error=type(error).__name__+': '+str(error))
                save()
                page.screenshot(path=str(OUT / (name+'-failure.png')),full_page=True)
                (OUT / (name+'-failure.html')).write_text(page.content())
                raise
            finally: context.close()
        assert not result['errors'],result['errors']
        assert all(method=='GET' and url.startswith(BASE) for method,url in result['requests'])
        unchanged(manifest)
        result['unchanged']=True; result['model_calls']=0
        result['budget_failures']=[r['name'] for r in result['routes'] if not r['budget_pass']]
        result['status']='failed' if result['budget_failures'] else 'succeeded';save()
        print('PERF_BROWSER_RESULT',result['status'],result['budget_failures'])
    finally: browser.close()
raise SystemExit(1 if result['budget_failures'] else 0)
