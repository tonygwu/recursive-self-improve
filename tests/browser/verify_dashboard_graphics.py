"""Read-only graphics geometry and visible labels on invented Evals/Run fixtures."""
from pathlib import Path
import argparse
import json
import sqlite3

from playwright.sync_api import sync_playwright
from keyboard_actions import KeyboardActions
from visual_checks import VisualChecks

parser = argparse.ArgumentParser()
parser.add_argument('--fixture', choices=('evals', 'run'), required=True)
args = parser.parse_args()
ROOT = Path(__file__).resolve().parents[2]
REPORT = ROOT / 'reports/dashboard-parity'
OUT = REPORT / 'graphics-acceptance' / args.fixture
OUT.mkdir(parents=True, exist_ok=True)
info = json.loads((REPORT / ('eval-demo-manifest.json' if args.fixture == 'evals' else 'run-health/manifest.json')).read_text())
assert ('si-eval-ui-' if args.fixture == 'evals' else 'si-run-health-') in info['db']

def snapshot():
    with sqlite3.connect('file:' + info['db'] + '?mode=ro', uri=True) as db:
        return list(db.iterdump())

before = snapshot()
targets = {str(p): p.read_bytes() for p in Path(info['root']).rglob('*.md')}
result = {'requests': [], 'errors': [], 'findings': [], 'states': []}
with sync_playwright() as runtime:
    browser = runtime.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width':1280, 'height':1024})
    keys = KeyboardActions(page, OUT / 'keyboard.json')
    visual = VisualChecks(page, OUT / 'contrast.json')
    page.on('request', lambda r: result['requests'].append([r.method, r.url]))
    page.on('pageerror', lambda e: result['errors'].append(str(e)))

    def check(condition, message):
        if not condition:
            result['findings'].append(message)

    names = ['evals'] if args.fixture == 'evals' else list(info['runs'])
    try:
        for name in names:
            page.goto('http://127.0.0.1:8876/#/' + ('evals' if name == 'evals' else 'overview/run/' + name))
            page.wait_for_load_state('networkidle')
            page.screenshot(path=str(OUT / (name + '-recon.png')))
            (OUT / (name + '-recon.html')).write_text(page.content())
            data = page.request.get('http://127.0.0.1:8876/api/' + ('eval-health' if name == 'evals' else 'runs/' + name)).json()
            for width in (1280, 1440):
                page.set_viewport_size({'width':width, 'height':1024})
                for theme in ('light', 'dark'):
                    keys.theme(theme)
                    label = f'{name}-{theme}-{width}'
                    if name == 'evals':
                        panel = page.locator('#trend-gate')
                        panel.scroll_into_view_if_needed()
                        bars = panel.locator('.outcome-strip > span').evaluate_all('els=>els.map(e=>({code:e.dataset.outcome,width:e.getBoundingClientRect().width}))')
                        total = sum(b['width'] for b in bars)
                        check(len(bars) == len(data['by_outcome']), label + ': missing outcome segments')
                        for row in data['by_outcome']:
                            bar = next((b for b in bars if b['code'] == row['code']), None)
                            check(bool(bar and total and abs(bar['width']/total-row['count']/data['attempts']) < .002), label + ': wrong share for ' + row['code'])
                            check(row['label'] in panel.inner_text(), label + ': missing outcome label')
                        check('historical result rows without an attempt link' in panel.inner_text(), label + ': historical distinction missing')
                        result['states'].append({'name':label,'denominator':data['attempts'],'bars':bars})
                    else:
                        page.locator('#main').evaluate('e=>e.scrollTop=0')
                        usage = page.locator('.run-usage')
                        check(usage.count() == 1, label + ': missing usage graphic')
                        for pool in data['inspection']['pipeline_pools']:
                            row = usage.locator('[data-pool="' + pool['pool'] + '"]')
                            if not row.count():
                                check(False, label + ': missing pool ' + pool['pool'])
                                continue
                            check(f"{pool['used']:,} / {pool['limit']:,}" in row.inner_text(), label + ': incorrect exact usage')
                            bar = row.locator('.meter__fill')
                            if pool['limit'] > 0:
                                geometry = bar.evaluate('(e)=>({bar:e.getBoundingClientRect().width,track:e.parentElement.clientWidth})')
                                check(abs(geometry['bar']/geometry['track']-min(1,pool['used']/pool['limit'])) < .01, label + ': incorrect pool geometry')
                            else:
                                check(bar.count() == 0 and 'No calls permitted' in row.inner_text(), label + ': zero cap misrepresented')
                        stages = []
                        for stage in data['stages']:
                            cell = page.locator('#run-stage-' + stage['name'] + ' .run-stage-state')
                            check(cell.count() == 1 and bool(cell.inner_text().strip()), label + ': stage label missing for ' + stage['name'])
                            stages.append({'name':stage['name'],'state':stage['state'],'label':cell.inner_text() if cell.count() else None})
                        result['states'].append({'name':label,'stages':stages})
                    check(page.evaluate('document.documentElement.scrollWidth <= innerWidth'), label + ': horizontal overflow')
                    visual.check(label)
                    page.screenshot(path=str(OUT / (label + '.png')))
            page.emulate_media(forced_colors='active')
            if name == 'evals':
                check(all(row['label'] in page.locator('#trend-gate').inner_text() for row in data['by_outcome']), 'forced colors: missing outcome label')
            else:
                check(page.locator('.run-stage-state').count() == len(data['stages']), 'forced colors: missing stage labels')
                check('may be incomplete' in page.locator('.run-usage').inner_text(), 'missing visible token caveat')
            page.screenshot(path=str(OUT / (name + '-forced-colors.png')))
            page.emulate_media(forced_colors='none')
        # Isolated rendering cases supplement the real reader fixtures. They
        # change only disposable browser DOM, never reader data or retained state.
        page.evaluate('''async fixture => {
          const m=await import('/app.js'), probe=document.createElement('section');
          probe.id='graphics-probe'; probe.className='panel';
          probe.style.cssText='padding:18px;max-width:440px;margin:20px 0';
          if(fixture==='run') probe.innerHTML=m.renderRunUsage({calls:{recorded:1,tokens_in:3,tokens_out:1},
            inspection:{pipeline_pools:[{pool:'cheap',used:null,limit:0},{pool:'gate',used:5,limit:4}]}});
          else probe.innerHTML=m.renderEvaluationHealth({data:{computable:true,attempts:21,unlinked_results:99,reason:'Invented component probe.',
            by_outcome:['budget_refused','interrupted','cancelled','invalid_scenario','comparison_unavailable','future_outcome'].map((code,i)=>({code,label:code,count:i+1,tone:'partial'}))}});
          document.querySelector('#main').append(probe);
        }''', args.fixture)
        probe = page.locator('#graphics-probe')
        for width in (1280, 1440):
            page.set_viewport_size({'width':width, 'height':1024})
            for theme in ('light', 'dark'):
                keys.theme(theme)
                probe.scroll_into_view_if_needed()
                if args.fixture == 'run':
                    check('No calls permitted · usage unknown' in probe.inner_text(), 'mixed unknown/zero cap')
                    check('1 over limit' in probe.inner_text(), 'over-limit counter hidden')
                    sizes = probe.locator('.token-share > span').evaluate_all('els=>els.map(e=>e.getBoundingClientRect().width)')
                    check(len(sizes) == 2 and abs(sizes[0]/sum(sizes)-.75) < .002, 'incorrect input/output share geometry')
                    check(probe.locator('[data-pool="cheap"] .meter__fill').count() == 0, 'zero cap acquired a meter')
                else:
                    bars = probe.locator('.outcome-strip > span').evaluate_all('els=>els.map(e=>({key:e.dataset.key,width:e.getBoundingClientRect().width}))')
                    keys_drawn = [b['key'] for b in bars]
                    check(keys_drawn == [str(i) for i in range(1,7)], 'outcome keys not unique and ordered')
                    check(probe.locator('.outcome-legend > li').evaluate_all('els=>els.map(e=>e.dataset.key)') == keys_drawn, 'legend keys differ from strip')
                    total = sum(b['width'] for b in bars)
                    check(all(abs(b['width']/total-(i+1)/21)<.002 for i,b in enumerate(bars)), 'many-outcome geometry wrong')
                    check('left-to-right order' in probe.inner_text() and 'future_outcome' in probe.inner_text(), 'ordered/future outcome missing')
                visual.check(f'component-{theme}-{width}')
                page.screenshot(path=str(OUT / f'component-{theme}-{width}.png'))
        page.emulate_media(forced_colors='active')
        probe.scroll_into_view_if_needed()
        page.screenshot(path=str(OUT / 'component-forced-colors.png'))
        if args.fixture == 'run':
            colors = probe.locator('[data-pool="gate"] .meter__fill').evaluate('(e)=>({fill:getComputedStyle(e).backgroundColor,track:getComputedStyle(e.parentElement).backgroundColor})')
            check(colors['fill'] != colors['track'] and colors['fill'] != 'rgba(0, 0, 0, 0)', 'forced colors: positive usage appears empty')
            result['forced_meter_colors'] = colors
        result['component_states'] = 4
        assert snapshot() == before
        assert all(Path(p).read_bytes() == text for p, text in targets.items())
        assert all(method == 'GET' for method, url in result['requests'])
        assert not result['errors'], result['errors']
        visual.assert_clean()
        result.update(store='unchanged', targets='unchanged', paid_calls=0)
        assert not result['findings'], result['findings']
        print('GRAPHICS_BROWSER_OK', args.fixture, len(result['states']), 'states; forced colors; unchanged Store/targets; GET-only; no paid calls')
    finally:
        keys.save()
        (OUT / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
        browser.close()
