"""Compare major and compact headings on existing invented temporary fixtures.

Run one matching fixture server first. --experiment injects and removes page-local
CSS; --assert-roles checks the shipped product without any style substitution.
No overall visual, human, native-runtime or production acceptance is inferred.
"""
from pathlib import Path
from urllib.parse import quote
import argparse
import hashlib
import json
import sqlite3

from playwright.sync_api import sync_playwright, expect
from visual_checks import CONTRAST

ROOT = Path(__file__).resolve().parents[2]
REPORTS = ROOT / 'reports/dashboard-parity'
MAJOR = ', '.join([
    '#view-overview .panel__title', '#view-evals .panel__title',
    '#project-detail .project-primary > .panel > .panel__head > .panel__title',
    '#run-delivery-heading', '.review-card--open .review-card__rule',
])
COMPACT = ', '.join([
    '#project-detail .project-secondary > .panel > .panel__head > .panel__title',
    '.review-next h3', '.review-carveouts h3', '.review-detail-panel h2',
])
HEADINGS = '.panel__title, .inspector__title, .view__count, .review-card__rule, .overview-status .statusline, .review-next h3, .review-carveouts h3, .review-detail-panel h2'
EXPERIMENT = (MAJOR + ' { font: 600 16px/22px var(--font-sans); letter-spacing: -.1px; }'
              + COMPACT + ' { font: 600 13px/18px var(--font-sans); letter-spacing: 0; }')
FIXTURES = {
    'overview': ('overview-manifest.json', 'si-overview-ui-', 8876),
    'review': ('review-layout/manifest.json', 'si-review-layout-', 8876),
    'review-detail': ('review-detail/manifest.json', 'si-review-detail-', 8876),
    'projects': ('projects-table/manifest.json', 'si-projects-ui-', 8876),
    'evidence': ('evidence-browser-manifest.json', 'si-evidence-ui-', 8876),
    'evals': ('evals-density/manifest.json', 'si-evals-density-', 8876),
    'run': ('run-layout-manifest.json', 'si-run-layout-', 8877),
}
# Native role values, not inferred from the product CSS under test.
TYPE_ROLES = {
    'display': ('.tile__value', ('Inter', '28px', '34px', '600', '-0.4px')),
    'label': ('.tile__label, .nav-item__count, .section__title, .overview-loop__steps span',
              ('Inter', '11px', '16px', '500', '0.4px')),
    'mono': ('code:not(:is(h1,h2,h3,h4,h5,h6) code), kbd:not(.navigation-open kbd), .mono, .kbd, .review-card__diff',
             ('Fira Mono', '12px', '19px', '400', 'normal')),
    'column': ('.data thead th, .projects-table th button, .run-stage-table thead th, .run-budget-table thead th, .policy-table thead th',
               ('Inter', '11px', '16px', '500', '0.4px')),
    'row': ('.run-stage-table tbody th', ('Inter', '13px', '20px', '500', 'normal')),
    'group': ('.rules-group-toggle', ('Inter', '13px', '20px', '500', 'normal')),
}
TYPE_MINIMUMS = {
    'overview': {'display': 4, 'label': 11},
    'review-queue': {'label': 1, 'mono': 6},
    'review': {'label': 1, 'mono': 6},
    'review-detail': {'label': 1, 'mono': 8},
    'projects': {'label': 1, 'column': 15},
    'rules': {'label': 9, 'mono': 3, 'column': 4, 'group': 1},
    'project-detail': {'display': 4, 'label': 5, 'mono': 3, 'column': 7},
    'evals': {'label': 1, 'column': 6},
    'run-detail': {'label': 1, 'mono': 1, 'column': 3, 'row': 5},
}
MEASURE = r'''({headings,major,compact,typeRoles})=>{
 const visible=e=>!!e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden';
 const rect=r=>({x:r.x,y:r.y,width:r.width,height:r.height});
 const main=document.getElementById('main');
 return {
  headings:[...document.querySelectorAll(headings)].filter(visible).map(e=>{
   const s=getComputedStyle(e),r=e.getBoundingClientRect(),range=document.createRange();range.selectNodeContents(e);
   const p=e.closest('.panel__head,.review-card__body,.inspector__head,.view__head,.overview-status')||e.parentElement;
   const pr=p.getBoundingClientRect();
   const textRects=[...range.getClientRects()].filter(r=>r.width&&r.height);
   return {id:e.id,text:e.textContent.trim(),classes:e.className,major:e.matches(major),compact:e.matches(compact),
    font:s.fontSize,line:s.lineHeight,weight:s.fontWeight,tracking:s.letterSpacing,
    rect:rect(r),parent:rect(pr),text_rects:textRects.map(rect),
    clipped:textRects.some(t=>t.left<pr.left-1||t.right>pr.right+1||t.top<pr.top-1||t.bottom>pr.bottom+1)};
  }),
  typography:Object.entries(typeRoles).flatMap(([role,selector])=>
   [...document.querySelectorAll(selector)].filter(visible).map(e=>{
    const s=getComputedStyle(e),family=s.fontFamily.split(',')[0].replaceAll('"','').trim();
    return {role,id:e.id,tag:e.tagName,classes:e.className,text:e.textContent.trim(),
     actual:[family,s.fontSize,s.lineHeight,s.fontWeight,s.letterSpacing],
     font_loaded:[...document.fonts].some(f=>f.family.replaceAll('"','')===family&&f.status==='loaded')
      &&document.fonts.check(`${s.fontWeight} ${s.fontSize} "${family}"`)};
   })),
  main:{width:main.clientWidth,scrollWidth:main.scrollWidth,height:main.clientHeight,scrollHeight:main.scrollHeight},
  content:main.textContent,
  controls:[...main.querySelectorAll('button,a,input,select,summary')].filter(visible).map(e=>({
   tag:e.tagName,id:e.id,text:e.textContent,href:e.getAttribute('href'),name:e.getAttribute('aria-label'),
   disabled:e.getAttribute('aria-disabled'),native_disabled:!!e.disabled,expanded:e.getAttribute('aria-expanded')})),
 };}'''


def source_hashes():
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (ROOT / 'src/self_improve').rglob('*')
            if p.suffix in {'.py', '.js', '.css', '.html', '.svg'}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture', choices=FIXTURES, required=True)
    parser.add_argument('--widths', nargs='+', type=int, choices=[1280, 1440], default=[1280, 1440])
    parser.add_argument('--themes', nargs='+', choices=['light', 'dark'], default=['light', 'dark'])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--experiment', action='store_true')
    mode.add_argument('--assert-roles', action='store_true')
    parser.add_argument('--assert-type-scale', action='store_true',
                        help='Also check native display, label, mono, table and control roles.')
    args = parser.parse_args()
    manifest_path, prefix, port = FIXTURES[args.fixture]
    manifest = json.loads((REPORTS / manifest_path).read_text())
    db_path = Path(manifest['db']).resolve()
    assert any(p.name.startswith(prefix) for p in db_path.parents), db_path
    routes = {
        'overview': [('overview', '#/overview', '#view-overview')],
        'review': [('review-queue', '#/review', '#view-review'),
                   ('review', '#/review/proposal/' + manifest.get('mixed', [''])[0], '#view-review')],
        'review-detail': [('review-detail', '#/review/family/' + quote(manifest.get('family', ''), safe=''), '.review-detail-panel')],
        'projects': [('projects', '#/projects', '#view-projects')],
        'evidence': [('rules', '#/rules/l00', '#view-rules'),
                     ('project-detail', '#/projects/' + quote(manifest.get('project_key', ''), safe=''), '#project-detail')],
        'evals': [('evals', '#/evals', '#view-evals')],
        'run': [('run-detail', '#/overview/run/delivered-run', '#run-detail')],
    }[args.fixture]
    base = f'http://127.0.0.1:{port}/'
    tag = 'experiment' if args.experiment else 'type' if args.assert_type_scale else 'assert' if args.assert_roles else 'baseline'
    out = REPORTS / 'heading-roles' / tag / args.fixture
    out.mkdir(parents=True, exist_ok=True)
    result = {'status': 'attempted', 'fixture': args.fixture, 'db': str(db_path), 'scope': vars(args),
              'source_hashes': source_hashes(), 'states': [], 'requests': [], 'errors': [], 'findings': []}

    def save():
        (out / 'result.json').write_text(json.dumps(result, indent=2) + '\n')

    def measure(page, name):
        page.locator('#main').evaluate('e=>e.scrollTop=0')
        page.screenshot(path=str(out / (name + '.png')))
        (out / (name + '.html')).write_text(page.content())
        data = page.evaluate(MEASURE, {'headings': HEADINGS, 'major': MAJOR, 'compact': COMPACT,
                                      'typeRoles': {k: v[0] for k, v in TYPE_ROLES.items()} if args.assert_type_scale else {}})
        data['content_sha256'] = hashlib.sha256(data.pop('content').encode()).hexdigest()
        data['contrast'] = page.evaluate(CONTRAST)
        data['screenshot'] = str((out / (name + '.png')).relative_to(ROOT))
        return data

    def check_heading_code(page, screen, name):
        # Exercise the real renderer with invented content, then restore the DOM.
        # Heading code has an inherited role, distinct from body-sized code.
        selector = '.review-detail-head h1' if screen == 'review-detail' else '.review-card--open .review-card__rule'
        heading = page.locator(selector).first
        if not heading.count():
            heading = page.locator('.review-card__rule').first
        expect(heading).to_be_visible()
        original = heading.inner_html()
        try:
            data = heading.evaluate('''async e=>{
                const {mdLite}=await import('/app.js');
                e.innerHTML=mdLite('Validate `fixture.cfg` before saving.');
                await document.fonts.ready;
                const read=n=>{const s=getComputedStyle(n);return [
                    s.fontFamily.split(',')[0].replaceAll('"','').trim(),
                    s.fontSize,s.lineHeight,s.fontWeight,s.letterSpacing]};
                return {heading:read(e),code:read(e.querySelector('code'))};
            }''')
            page.screenshot(path=str(out / (name + '-heading-code.png')))
        finally:
            heading.evaluate('(e,html)=>{e.innerHTML=html}', original)
        assert heading.inner_html() == original, 'Heading probe did not restore the DOM'
        data['restored'] = True
        return data

    save()
    with sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        context = browser.new_context(viewport={'width': 1280, 'height': 1024}, reduced_motion='reduce')
        page = context.new_page()
        page.on('request', lambda request: result['requests'].append([request.method, request.url]))
        page.on('pageerror', lambda error: result['errors'].append(str(error)))
        try:
            for screen, route, selector in routes:
                page.goto(base + route)
                page.wait_for_load_state('networkidle')
                expect(page.locator(selector).first).to_be_visible()
                if args.fixture == 'review-detail':
                    expect(page.locator('[data-decision="approve"]')).to_be_enabled()
                page.evaluate('document.fonts.ready')
                # Inspect rendered DOM and controls before changing the theme or style.
                measure(page, screen + '-recon')
                print('RECON', screen, page.get_by_role('button').all_text_contents()[:10], flush=True)
                # A deep-linked Review card receives focus. Its rounded outline
                # changed one antialias pixel on repaint despite identical DOM
                # geometry. Normalize focus outside the compared headings;
                # keyboard/focus acceptance belongs to the existing journeys.
                page.locator('#main').evaluate('e=>e.focus({preventScroll:true})')
                for width in args.widths:
                    page.set_viewport_size({'width': width, 'height': 1024})
                    for theme in args.themes:
                        if page.evaluate('async()=>(await import("/app.js")).effectiveTheme()') != theme:
                            page.get_by_role('button', name=theme.title() + ' theme', exact=True).click()
                        assert page.evaluate('async()=>(await import("/app.js")).effectiveTheme()') == theme
                        name = f'{screen}-{theme}-{width}'
                        before = measure(page, name + '-shipped')
                        row = {'screen': screen, 'width': width, 'theme': theme, 'shipped': before}
                        result['states'].append(row)
                        if args.experiment:
                            style = page.add_style_tag(content=EXPERIMENT)
                            try:
                                after = measure(page, name + '-experiment')
                                row['experiment'] = after
                                assert after['content_sha256'] == before['content_sha256']
                                assert after['controls'] == before['controls']
                                compact_before = [h for h in before['headings'] if not h['major'] and not h['compact']]
                                compact_after = [h for h in after['headings'] if not h['major'] and not h['compact']]
                                fields = ('text', 'font', 'line', 'weight', 'tracking')
                                assert [[h[f] for f in fields] for h in compact_before] == [[h[f] for f in fields] for h in compact_after]
                            finally:
                                style.evaluate('e=>e.remove()')
                            restored = measure(page, name + '-restored')
                            assert {k: v for k, v in restored.items() if k != 'screenshot'} == {k: v for k, v in before.items() if k != 'screenshot'}, 'Page-local experiment did not restore exact measured state'
                            # Native rounded edges can repaint with a few changed
                            # antialias pixels (observed: eight select-border pixels,
                            # all one-channel differences of one). Retain both PNGs
                            # and record equality without confusing byte identity
                            # with exact restored styles, text and layout above.
                            row['restored_png_identical'] = (out / (name + '-restored.png')).read_bytes() == (out / (name + '-shipped.png')).read_bytes()
                            row['restored'] = True
                        checked = row.get('experiment', before)
                        if args.assert_type_scale:
                            for role, minimum in TYPE_MINIMUMS[screen].items():
                                assert sum(t['role'] == role for t in checked['typography']) >= minimum, (screen, role, 'Missing native role')
                            for t in checked['typography']:
                                if tuple(t['actual']) != TYPE_ROLES[t['role']][1] or not t['font_loaded']:
                                    result['findings'].append({'kind': 'native_type_role', 'state': name, 'element': t})
                        minimum = {'overview': 4, 'review-queue': 1, 'review': 1,
                                   'project-detail': 4, 'evals': 7, 'run-detail': 1}.get(screen, 0)
                        assert sum(h['major'] for h in checked['headings']) >= minimum, 'Expected headings are missing'
                        compact_minimum = {'project-detail': 3, 'review-queue': 2, 'review': 2, 'review-detail': 4}.get(screen, 0)
                        assert sum(h['compact'] for h in checked['headings']) >= compact_minimum, 'Expected compact headings are missing'
                        for h in checked['headings']:
                            if h['clipped']:
                                result['findings'].append({'kind': 'heading_outside_parent', 'state': name, 'heading': h})
                            if (args.assert_roles or args.experiment) and (h['major'] or h['compact']):
                                expected = ('16px', '22px', '600', '-0.1px') if h['major'] else ('13px', '18px', '600', 'normal')
                                actual = (h['font'], h['line'], h['weight'], h['tracking'])
                                if actual != expected:
                                    result['findings'].append({'kind': 'heading_scale', 'state': name, 'heading': h})
                            elif args.assert_roles:
                                # These roles deliberately retain their prior scale.
                                expected = ('13px', '20px', '600', 'normal')
                                if h['id'] == 'ov-statusline':
                                    expected = ('16px', '22px', '600', '-0.1px')
                                elif h['id'] == 'review-count':
                                    expected = ('13px', '20px', '600', '-0.2px')
                                actual = (h['font'], h['line'], h['weight'], h['tracking'])
                                if actual != expected:
                                    result['findings'].append({'kind': 'unchanged_role_scale', 'state': name, 'heading': h})
                        if checked['main']['scrollWidth'] > checked['main']['width']:
                            result['findings'].append({'kind': 'main_horizontal_overflow', 'state': name})
                        if checked['contrast']['failed']:
                            result['findings'].append({'kind': 'text_contrast', 'state': name, 'detail': checked['contrast']['failed']})
                        if args.assert_type_scale and screen in {'review-queue', 'review', 'review-detail'}:
                            probe = check_heading_code(page, screen, name)
                            row['inline_heading'] = probe
                            if probe['code'] != ['Fira Mono', *probe['heading'][1:]]:
                                result['findings'].append({'kind': 'inline_heading_scale', 'state': name, 'detail': probe})
                        save()
            assert not result['errors'], result['errors']
            assert all(method == 'GET' and url.startswith(base) for method, url in result['requests'])
            with sqlite3.connect('file:' + str(db_path) + '?mode=ro', uri=True) as db:
                assert list(db.iterdump()) == manifest.get('snapshot', manifest.get('sql'))
                # Full Review seeds evaluation history with invented synchronous
                # responses before serving. The exact SQL snapshot above proves
                # this read-only journey added or changed no call records.
                seeded_calls = manifest['synthetic_calls'] if args.fixture == 'review-detail' else 0
                if args.fixture == 'review-detail':
                    assert manifest['models'] == 0
                assert db.execute('SELECT COUNT(*) FROM llm_calls').fetchone()[0] == seeded_calls
            assert all(Path(p).read_text() == text for p, text in manifest['targets'].items())
            assert source_hashes() == result['source_hashes']
            result.update(status='failed' if result['findings'] else 'succeeded',
                          store='unchanged', targets='unchanged', source='unchanged', models=0)
            save()
            print('HEADING_ROLES', json.dumps({k: result[k] for k in ('status', 'fixture', 'store', 'targets', 'source', 'models')}),
                  'states', len(result['states']), 'GETs', len(result['requests']), 'findings', len(result['findings']), flush=True)
            assert not result['findings'], result['findings']
        except Exception:
            result['status'] = 'failed'
            save()
            page.screenshot(path=str(out / 'failure.png'))
            raise
        finally:
            context.close()
            browser.close()


if __name__ == '__main__':
    main()
