"""Exact retained patches and distinct Rules states on disposable fixtures."""
from pathlib import Path
from urllib.parse import quote
import argparse
import json
import sqlite3

from playwright.sync_api import sync_playwright, expect
from keyboard_actions import KeyboardActions
from visual_checks import VisualChecks

parser = argparse.ArgumentParser()
parser.add_argument('--fixture', choices=('rules', 'review'), required=True)
args = parser.parse_args()
ROOT = Path(__file__).resolve().parents[2]
REPORT = ROOT / 'reports/dashboard-parity'
OUT = REPORT / 'rule-presentation' / args.fixture
OUT.mkdir(parents=True, exist_ok=True)
info = json.loads((REPORT / ('evidence-browser-manifest.json' if args.fixture == 'rules' else 'review-detail/manifest.json')).read_text())
assert ('si-evidence-ui-' if args.fixture == 'rules' else 'si-review-detail-') in info['db']

def snapshot():
    with sqlite3.connect('file:' + info['db'] + '?mode=ro', uri=True) as db:
        return list(db.iterdump())

before = snapshot()
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

    def inspect_patch(pre, source, name):
        expect(pre).to_be_visible()
        check(pre.text_content() == source, name + ': retained DOM text changed')
        check(pre.locator('.diff-line').count() > 0, name + ': no line presentation')
        selection = pre.evaluate('''(el,source)=>{
          const range=document.createRange(), selection=getSelection();
          const read=node=>{range.selectNodeContents(node);selection.removeAllRanges();selection.addRange(range);
            return {native:selection.toString(),range:range.toString()};};
          const decorated=read(el), control=el.cloneNode(false);
          control.textContent=source;el.after(control);const plain=read(control);control.remove();
          selection.removeAllRanges();return {decorated,plain};
        }''', source)
        # Chromium omits a final rendered newline from native selection. Compare
        # the same plain PRE as a control, and require exact raw Range/DOM text.
        check(selection['decorated']['range'] == source, name + ': complete range changed')
        check(selection['decorated']['native'] == selection['plain']['native'], name + ': native selection differs from plain patch')
        check(pre.locator('script,img').count() == 0, name + ': retained text became markup')

    try:
        route = '#/rules/l00' if args.fixture == 'rules' else '#/review/family/' + quote(info['family'])
        page.goto('http://127.0.0.1:8876/' + route)
        page.wait_for_load_state('networkidle')
        page.screenshot(path=str(OUT / 'recon.png'))
        (OUT / 'recon.html').write_text(page.content())
        if args.fixture == 'rules':
            data = page.request.get('http://127.0.0.1:8876/api/rules/l00').json()
            sources = [t['diff'] for t in data['targets'] if t.get('diff')]
            patches = page.locator('#inspector-body pre.diff')
        else:
            entry = page.evaluate('async()=>{const {state}=await import("/app.js");return state.reviewPreviews[state.reviewDetailFamily].data}')
            sources = [t['diff_unified'] for t in entry['targets'] if t['state'] == 'ready']
            patches = page.locator('.review-selected-preview .review-card__diff')
        check(len(sources) == patches.count() > 0, 'real reader patches missing')
        for width in (1280,1440):
            page.set_viewport_size({'width':width, 'height':1024})
            for theme in ('light','dark'):
                keys.theme(theme)
                label = f'{args.fixture}-{theme}-{width}'
                for i,source in enumerate(sources):
                    pre=patches.nth(i)
                    pre.scroll_into_view_if_needed()
                    inspect_patch(pre,source,label)
                visual.check(label)
                page.screenshot(path=str(OUT / (label+'.png')))
                check(page.evaluate('document.documentElement.scrollWidth<=innerWidth'), label+': overflow')
                result['states'].append(label)
        # Retain useful red evidence on the original source before invoking new helpers.
        if result['findings']:
            raise AssertionError(result['findings'])
        patch = ('\n--- a/AGENTS.md\r\n+++ b/AGENTS.md\r\n@@ -1,2 +1,2 @@\r\n'
                 ' context\r\n--- removed content, not a header\r\n+++ added <script>unsafe</script> 🧪\r\n'
                 '\\ No newline at end of file\n--- a/next\n+++ b/next\n@@ -0,0 +1,2 @@\n+\n+long ' + 'word\t ' * 80)
        # Isolated component cases are supplemental, not invented reader facts.
        page.evaluate('''async patch=>{const m=await import('/app.js'),e=document.createElement('section');
          e.id='patch-probe';e.className='panel';e.style.cssText='padding:18px;max-width:520px;margin:20px 0';
          e.innerHTML='<h2>Invented patch edge cases</h2>'+m.renderReviewDiff(patch,'edge','Complete invented patch')+
            '<div class="rules-statuses">'+['gated_pass','gated_fail','ungated','inconclusive','held','approved_user','applied','rejected_user','rolled_back','superseded','__proto__'].map(s=>m.renderRuleStatus(s)).join('')+'</div>';
          document.querySelector('#main').append(e);}''',patch)
        probe=page.locator('#patch-probe')
        summary=probe.get_by_text('Complete invented patch',exact=True)
        keys.activate(summary)
        expect(summary.locator('..')).to_have_attribute('open','')
        pre=probe.locator('pre')
        for width in (1280,1440):
            page.set_viewport_size({'width':width,'height':1024})
            for theme in ('light','dark'):
                keys.theme(theme);pre.scroll_into_view_if_needed()
                inspect_patch(pre,patch,f'component-{theme}-{width}')
                colors=pre.locator('[data-diff]').evaluate_all('els=>els.map(e=>({kind:e.dataset.diff,text:e.textContent,color:getComputedStyle(e).color,bg:getComputedStyle(e).backgroundColor}))')
                add=next(r for r in colors if r['kind']=='add');remove=next(r for r in colors if r['kind']=='remove')
                check(add['color']!=remove['color'] and add['bg']!=remove['bg'],'addition/removal styling identical')
                check(any(r['kind']=='remove' and r['text'].startswith('--- removed') for r in colors),'content classified as file header')
                visual.check(f'component-{theme}-{width}')
                page.screenshot(path=str(OUT/f'component-{theme}-{width}.png'))
                probe.locator('.rules-statuses').scroll_into_view_if_needed()
                page.screenshot(path=str(OUT/f'states-{theme}-{width}.png'))
        page.emulate_media(forced_colors='active');pre.scroll_into_view_if_needed()
        inspect_patch(pre,patch,'forced-colors')
        check(all(label in probe.inner_text() for label in ('Gate passed','Gate failed','Approved','Applied','Rolled back','Unknown status')), 'forced colors lost state distinctions')
        page.screenshot(path=str(OUT/'forced-colors.png'))
        probe.locator('.rules-statuses').scroll_into_view_if_needed()
        page.screenshot(path=str(OUT/'states-forced-colors.png'))
        result['component_states']=5
        assert not result['findings'],result['findings']
        visual.assert_clean()
        print('RULE_PRESENTATION_OK',args.fixture,len(result['states']),'reader states',flush=True)
    finally:
        result.update(store='unchanged' if snapshot()==before else 'CHANGED',targets='unchanged' if all(Path(p).read_text()==t for p,t in info['targets'].items()) else 'CHANGED',paid_calls=0)
        (OUT/'result.json').write_text(json.dumps(result,indent=2)+'\n')
        browser.close()
        assert result['store']==result['targets']=='unchanged'
        assert all(method=='GET' for method,url in result['requests'])
        assert not result['errors'],result['errors']
