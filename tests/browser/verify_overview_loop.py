"""Overview summary word geometry and exact navigation on temporary fixtures."""
from copy import deepcopy
from pathlib import Path
import argparse
import hashlib
import json
import sqlite3
import struct
import tempfile

from playwright.sync_api import expect, sync_playwright
from keyboard_actions import KeyboardActions
from visual_checks import VisualChecks

ROOT = Path(__file__).resolve().parents[2]
BASE = 'http://127.0.0.1:8876/'
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--output', type=Path, default=ROOT/'reports/dashboard-parity/overview-loop')
parser.add_argument('--variants', nargs='+', choices=['populated', 'empty', 'unavailable'],
                    default=['populated', 'empty', 'unavailable'])
args = parser.parse_args()
OUT = args.output.resolve()
OUT.mkdir(parents=True, exist_ok=True)
manifest = json.loads((ROOT/'reports/dashboard-parity/overview-manifest.json').read_text())
assert Path(manifest['db']).parent.parent.name.startswith('si-overview-ui-')
LABELS = ['Known session IDs', 'Retained incidents', 'Rules learned',
          'Eval attempts · pass', 'Deliveries · rollbacks', 'Comparable trend']
ROUTES = ['#/rules?kinds=session&mode=evidence', '#/rules?kinds=incident&mode=evidence',
          '#/rules', '#/evals', '#/review', '#/evals']
MEASURE = r'''nodes=>nodes.map(e=>{
    const box=n=>{const r=n.getBoundingClientRect();return {left:r.left,right:r.right,top:r.top,bottom:r.bottom,width:r.width};};
    const words=[],walker=document.createTreeWalker(e,NodeFilter.SHOW_TEXT);
    while(walker.nextNode()){const n=walker.currentNode;for(const m of n.textContent.matchAll(/\S+/g)){
        const r=document.createRange();r.setStart(n,m.index);r.setEnd(n,m.index+m[0].length);
        for(const b of r.getClientRects())words.push({text:m[0],left:b.left,right:b.right,top:b.top,bottom:b.bottom});}}
    return {...box(e),label:e.querySelector('span').textContent,value:e.querySelector('strong').textContent,
            href:e.querySelector('a').getAttribute('href'),words};})'''
TILE_MEASURE = r'''nodes=>nodes.map(e=>{
    const b=e.getBoundingClientRect(),words=[],walker=document.createTreeWalker(e,NodeFilter.SHOW_TEXT);
    while(walker.nextNode()){const n=walker.currentNode;for(const m of n.textContent.matchAll(/\S+/g)){
        const r=document.createRange();r.setStart(n,m.index);r.setEnd(n,m.index+m[0].length);
        for(const w of r.getClientRects())words.push({text:m[0],left:w.left,right:w.right,top:w.top,bottom:w.bottom});}}
    return {left:b.left,right:b.right,top:b.top,bottom:b.bottom,width:b.width,
            label:e.querySelector('.tile__label').textContent,value:e.querySelector('.tile__value').textContent,words};})'''
result = {'status': 'attempted', 'states': [], 'activations': [], 'requests': [], 'errors': [],
          'source_hashes': {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                            for p in [ROOT/'src/self_improve/dashboard/static/app.css',
                                      ROOT/'src/self_improve/dashboard/static/app.js', Path(__file__).resolve()]}}


def unchanged():
    with sqlite3.connect('file:'+manifest['db']+'?mode=ro', uri=True) as db:
        assert list(db.iterdump()) == manifest['snapshot']
        assert db.execute('SELECT COUNT(*) FROM llm_calls').fetchone()[0] == 0
    assert all(Path(p).read_text() == content for p, content in manifest['targets'].items())
    for p, digest in result['source_hashes'].items():
        assert hashlib.sha256((ROOT/p).read_bytes()).hexdigest() == digest


with tempfile.TemporaryDirectory(prefix='si-overview-loop-browser-') as folder, sync_playwright() as runtime:
    temporary = Path(folder)
    extension = temporary/'extension'
    extension.mkdir()
    (extension/'manifest.json').write_text(json.dumps({'manifest_version': 3, 'name': 'Temporary loop zoom',
        'version': '1.0', 'host_permissions': ['http://127.0.0.1/*'], 'background': {'service_worker': 'worker.js'}}))
    (extension/'worker.js').write_text('chrome.runtime.onInstalled.addListener(() => {});')
    browser = runtime.chromium.launch_persistent_context(str(temporary/'profile'), channel='chromium', headless=True,
        args=[f'--disable-extensions-except={extension}', f'--load-extension={extension}'], viewport={'width': 1280, 'height': 1024})
    page = browser.pages[0]
    zoom = browser.service_workers[0] if browser.service_workers else browser.wait_for_event('serviceworker')
    keys = KeyboardActions(page, OUT/'keyboard.json')
    visual = VisualChecks(page, OUT/'contrast.json')
    page.on('request', lambda r: result['requests'].append([r.method, r.url]))
    page.on('pageerror', lambda e: result['errors'].append(str(e)))

    def goto():
        # Hash-only navigation can retain the previous variant's Overview cache.
        page.goto('about:blank')
        page.goto(BASE+'#/overview')
        page.wait_for_load_state('networkidle')
        expect(page.locator('#ov-loop li')).to_have_count(6)

    def set_zoom(factor):
        actual = zoom.evaluate('''async factor=>{const [tab]=await chrome.tabs.query({active:true,currentWindow:true});
            await chrome.tabs.setZoom(tab.id,factor);return chrome.tabs.getZoom(tab.id);}''', factor)
        assert actual == factor
        page.wait_for_function('factor=>Math.abs(devicePixelRatio-factor)<.02', arg=factor)

    try:
        goto()
        health = page.request.get(BASE+'api/health').json()
        assert health['db_path'] == manifest['db'] and health['read_only']
        page.screenshot(path=str(OUT/'reconnaissance.png'))
        (OUT/'reconnaissance.html').write_text(page.content())
        print('OVERVIEW_LOOP_RECON', page.get_by_role('heading').all_text_contents(), flush=True)
        data = page.request.get(BASE+'api/overview').json()
        for variant in args.variants:
            response = deepcopy(data)
            loop = response['audit']['loop']
            if variant != 'populated':
                count = 0 if variant == 'empty' else None
                loop['sessions']['known'] = count
                loop.update(incidents=count, learnings=count)
                loop['evaluations'].update(attempts=count, passed=count)
                loop['delivery'].update(manual_targets=count, automatic_operations=count, rollback_operations=count)
                page.route('**/api/overview', lambda route: route.fulfill(json=response))
            value = lambda x: 'Unknown' if x is None else f'{x:,}'
            delivery = loop['delivery']
            delivered = None if delivery['manual_targets'] is None or delivery['automatic_operations'] is None else delivery['manual_targets'] + delivery['automatic_operations']
            expected = [value(loop['sessions']['known']), value(loop['incidents']), value(loop['learnings']),
                        value(loop['evaluations']['attempts'])+' · '+value(loop['evaluations']['passed']),
                        value(delivered)+' · '+value(delivery['rollback_operations']), 'Inspect by version →']
            for width in (1280, 1440):
                page.set_viewport_size({'width': width, 'height': 1024})
                for factor in (1, 2):
                    set_zoom(factor)
                    for theme in ('light', 'dark'):
                        goto()
                        keys.theme(theme)
                        page.locator('#main').evaluate('e=>e.scrollTop=0')
                        key = f'{variant}-{width}-{theme}-zoom-{factor}'
                        items = page.locator('#ov-loop li').evaluate_all(MEASURE)
                        state = {'key': key, 'items': items, 'viewport': page.evaluate('({width:innerWidth,height:innerHeight,dpr:devicePixelRatio})')}
                        result['states'].append(state)
                        png = page.screenshot(path=str(OUT/(key+'.png')))
                        assert struct.unpack('>II', png[16:24]) == (width, 1024)
                        (OUT/(key+'.html')).write_text(page.content())
                        assert [i['label'] for i in items] == LABELS
                        assert [i['href'] for i in items] == ROUTES
                        assert [i['value'] for i in items] == expected, {'actual': [i['value'] for i in items], 'expected': expected}
                        escaped = [{'label':i['label'], 'word':w} for i in items for w in i['words']
                                   if w['left'] < i['left']-1 or w['right'] > i['right']+1
                                   or w['top'] < i['top']-1 or w['bottom'] > i['bottom']+1]
                        collisions = [[x['text'],y['text']] for n,a in enumerate(items) for b in items[n+1:]
                                      for x in a['words'] for y in b['words']
                                      if min(x['right'],y['right']) > max(x['left'],y['left'])
                                      and min(x['bottom'],y['bottom']) > max(x['top'],y['top'])]
                        state.update(escaped_words=escaped, collisions=collisions)
                        assert not escaped and not collisions, state
                        if factor == 1:
                            assert max(i['top'] for i in items)-min(i['top'] for i in items) <= 1
                        else:
                            assert len({round(i['top']) for i in items}) == 2
                            assert items[3]['top'] >= max(i['bottom'] for i in items[:3])
                        columns = 6 if factor == 1 else 3
                        for offset in range(0, 6, columns):
                            row = items[offset:offset+columns]
                            assert max(i['top'] for i in row)-min(i['top'] for i in row) <= 1
                            assert all(a['right'] <= b['left'] for a, b in zip(row, row[1:]))
                        tiles = page.locator('#ov-tiles .tile').evaluate_all(TILE_MEASURE)
                        assert len(tiles) == 4
                        state['tiles'] = tiles
                        tile_escape = [{'label':i['label'], 'word':w} for i in tiles for w in i['words']
                                       if w['left'] < i['left']-1 or w['right'] > i['right']+1
                                       or w['top'] < i['top']-1 or w['bottom'] > i['bottom']+1]
                        state['tile_escaped_words'] = tile_escape
                        assert not tile_escape, {'key':key, 'tiles':tiles, 'escaped':tile_escape}
                        tile_columns = 4 if factor == 1 else 2
                        for offset in range(0, 4, tile_columns):
                            row = tiles[offset:offset+tile_columns]
                            assert max(i['top'] for i in row)-min(i['top'] for i in row) <= 1
                            assert all(a['right'] <= b['left'] for a, b in zip(row, row[1:]))
                        if factor == 2:
                            assert tiles[2]['top'] >= max(i['bottom'] for i in tiles[:2])
                        visual.check(key)
                        page.locator('#ov-tiles').scroll_into_view_if_needed()
                        for target in page.locator('#ov-tiles .tile__value').all():
                            expect(target).to_be_in_viewport(ratio=1)
                        png = page.screenshot(path=str(OUT/(key+'-tiles.png')))
                        assert struct.unpack('>II', png[16:24]) == (width, 1024)
                        visual.check(key+'-tiles')
                        for index, route in enumerate(ROUTES):
                            target = page.locator('#ov-loop a').nth(index)
                            keys.reach(target)
                            if variant == 'populated':
                                keys.activate(target)
                                expect(page).to_have_url(BASE+route)
                                page.wait_for_load_state('networkidle')
                                result['activations'].append({'state': key, 'index': index, 'href': route})
                                goto()
                        print('OVERVIEW_LOOP_STATE', key, 'six labels and values retained; no word collision', flush=True)
                    set_zoom(1)
            if variant != 'populated':
                page.unroute('**/api/overview')
        assert not result['errors'], result['errors']
        assert all(method == 'GET' and url.startswith(BASE) for method, url in result['requests'])
        visual.assert_clean()
        result['status'] = 'succeeded'
        print('OVERVIEW_LOOP_OK', len(result['states']), 'states', len(result['activations']), 'native activations', flush=True)
    except BaseException as error:
        result.update(status='failed', error=str(error))
        page.screenshot(path=str(OUT/'failure.png'))
        (OUT/'failure.html').write_text(page.content())
        raise
    finally:
        unchanged()
        result.update(database='unchanged', targets='unchanged', source='unchanged', model_calls=0)
        (OUT/'result.json').write_text(json.dumps(result, indent=2)+'\n')
        keys.save()
        browser.close()
