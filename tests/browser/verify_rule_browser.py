"""Complete Rules browsing, navigation and request-order checks on invented data."""
from pathlib import Path
import json, sqlite3
from playwright.sync_api import sync_playwright
out=Path(__file__).resolve().parents[2]/'reports/dashboard-parity'
manifest=json.loads((out/'rule-browser-manifest.json').read_text())
base='http://127.0.0.1:8876/'
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True);page=browser.new_page(viewport={'width':1440,'height':1024})
    errors=[];requests=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    page.on('request',lambda request:requests.append((request.method,request.url)))
    try:
        page.goto(base+'#/rules');page.wait_for_load_state('networkidle')
        page.screenshot(path=str(out/'rules-before.png'));(out/'rules-before.html').write_text(page.content())
        assert '45 matching rules · 23 families · 45 proposals · 2 recorded target paths' in page.locator('#rules-meta').inner_text()
        assert page.locator('#rules-results tr').count()==20
        assert 'agentic' in page.locator('[data-rule-row="single-21"]').inner_text()
        family=page.locator('[data-rule-family]').first;fid=family.get_attribute('data-rule-family')
        family.focus();page.keyboard.press('Enter')
        page.locator('.rules-member[data-child="true"]').nth(19).wait_for()
        assert page.evaluate('document.activeElement.id')=='family-'+fid
        first=set(page.locator('.rules-member[data-child="true"]').evaluate_all('(els)=>els.map(e=>e.dataset.ruleRow)'))
        page.get_by_role('button',name='Next members',exact=True).focus();page.keyboard.press('Enter')
        page.get_by_text('21–23 of 23 matching members',exact=True).wait_for()
        assert page.evaluate('document.activeElement.id')=='rules-member-status'
        last=set(page.locator('.rules-member[data-child="true"]').evaluate_all('(els)=>els.map(e=>e.dataset.ruleRow)'))
        assert first|last=={f'family-{i:02}' for i in range(23)} and not first&last
        family_url=page.url
        page.reload();page.wait_for_load_state('networkidle');page.get_by_text('21–23 of 23 matching members',exact=True).wait_for()
        child=page.locator('.rules-member[data-child="true"] [data-rule-id]').first;selected=child.get_attribute('data-rule-id')
        child.focus();page.keyboard.press('Enter')
        page.locator('#inspector-body').get_by_text('Why the machine believes it',exact=True).wait_for()
        assert page.evaluate('document.activeElement.dataset.ruleId')==selected
        assert page.locator('#rules-inspector-host > #inspector').count()==1
        assert 'member_cursor=' in page.url
        page.screenshot(path=str(out/'rules-expanded.png'))
        page.keyboard.press('Escape');page.wait_for_url(family_url)
        assert page.evaluate('document.activeElement.dataset.ruleId')==selected
        page.goto(base+'#/rules?sort=title');page.wait_for_load_state('networkidle')
        page.get_by_role('button',name='Next page',exact=True).click()
        page.get_by_text('21–23 of 23 families',exact=True).wait_for()
        assert page.locator('#rules-results tr').count()==3
        page.reload();page.wait_for_load_state('networkidle');page.get_by_text('21–23 of 23 families',exact=True).wait_for()
        page.go_back();page.wait_for_load_state('networkidle');page.get_by_text('1–20 of 23 families',exact=True).wait_for()
        page.get_by_role('button',name='Global instructions',exact=True).click()
        page.get_by_text('23 matching rules · 12 families · 23 proposals · 1 recorded target paths',exact=True).wait_for()
        page.get_by_label('Group similar rules',exact=True).uncheck()
        page.get_by_text('1–20 of 23 rules',exact=True).wait_for()
        page.get_by_role('button',name='Hooks',exact=True).click();page.get_by_text('No matching rules',exact=True).wait_for()
        page.get_by_role('button',name='All',exact=True).click();page.get_by_text('1–20 of 45 rules',exact=True).wait_for()
        search=page.get_by_role('searchbox',name='Search rules and linked evidence')
        search.fill('sixth_signal_sentinel');page.get_by_text('1–1 of 1 rules',exact=True).wait_for()
        assert page.locator('[data-rule-id]').get_attribute('data-rule-id')=='single-21'
        # A pending debounce must not overwrite browser history navigation.
        search.fill('should_not_replace_back');page.go_back();page.wait_for_load_state('networkidle')
        page.wait_for_timeout(300) # Deliberately cross the 220ms debounce deadline to disprove a late navigation.
        assert 'should_not_replace_back' not in page.url
        assert search.input_value()!='should_not_replace_back'
        # Selected details do not depend on the visible page, and tab URLs survive reload.
        page.goto(base+'#/rules/single-21?sort=title&tab=provenance');page.wait_for_load_state('networkidle')
        page.locator('#inspector-body').get_by_text('Where it came from',exact=True).wait_for()
        assert '7 incident(s), 1 project(s)' in page.locator('#inspector-body').inner_text()
        page.get_by_role('button',name='Load mining history',exact=True).click()
        page.get_by_text('Complete content, source evidence, and call record',exact=True).wait_for()
        page.get_by_role('link',name='Why',exact=True).click()
        for width in (1440,1100):
            page.set_viewport_size({'width':width,'height':1024})
            for theme in ('light','dark'):
                page.evaluate('(theme)=>document.documentElement.dataset.theme=theme',theme)
                page.locator('#main').evaluate('(el)=>el.scrollTop=0')
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                assert page.locator('#rules-inspector-host > #inspector').is_visible()
                page.screenshot(path=str(out/f'rules-{theme}-{width}.png'))
            wrap=page.locator('#rules-table-wrap');wrap.focus();before=wrap.evaluate('(el)=>el.scrollLeft');page.keyboard.press('ArrowRight')
            if width==1100:
                page.wait_for_function('document.getElementById("rules-table-wrap").scrollLeft>0')
                assert wrap.evaluate('(el)=>el.scrollLeft')>before
        page.set_viewport_size({'width':1440,'height':1024})
        page.route('**/api/rules/browse*',lambda route:route.abort())
        page.get_by_role('button',name='Refresh',exact=True).click();page.locator('#rules-notice [role="alert"]').wait_for()
        page.unroute('**/api/rules/browse*');page.get_by_role('button',name='Refresh',exact=True).click()
        page.locator('#rules-notice [role="alert"]').wait_for(state='detached');page.locator('#inspector-body').get_by_text('Why the machine believes it',exact=True).wait_for()
        # Run actual exported loaders with deliberately reversed GET completion order.
        result=page.evaluate('''async () => {
          const app=await import('/app.js'), original=window.fetch, sample=structuredClone(app.state.rulePage.data);
          let release, delayed=new Promise(resolve=>release=resolve);
          try {
            window.fetch=async (url,opts)=>{
              if(String(url).startsWith('/api/rules/browse')) {
                const older=!String(url).includes('hook'); if(older)await delayed;
                const data={...sample,revision:older?'old':'new'};
                return {ok:true,json:async()=>data};
              }
              return original(url,opts);
            };
            app.state.ruleQuery='';app.state.selectedRule='';
            const old=app.load();
            app.state.ruleQuery='target=hook';await app.loadRulePage();release();await old;
            if(app.state.rulePage.data.revision!=='new')throw Error('Old shared load replaced newer query');
            return 'REVERSED_GET_OK';
          } finally {release();window.fetch=original;}
        }''')
        assert result=='REVERSED_GET_OK'
        # Closing, refreshing, then revisiting an unchanged summary still refetches full detail.
        page.goto(base+'#/rules/single-21');page.wait_for_load_state('networkidle')
        page.locator('#inspector-body').get_by_text('Why the machine believes it',exact=True).wait_for()
        page.keyboard.press('Escape');page.wait_for_load_state('networkidle')
        def changed_detail(route):
            response=route.fetch();data=response.json();data['why']='Fresh detail after unchanged summary';route.fulfill(response=response,json=data)
        page.route('**/api/rules/single-21',changed_detail)
        page.get_by_role('button',name='Refresh',exact=True).click();page.wait_for_load_state('networkidle')
        page.locator('[data-rule-id="single-21"]').click()
        page.get_by_text('Fresh detail after unchanged summary',exact=True).wait_for()
        page.unroute('**/api/rules/single-21')
        assert not errors,errors
        assert all(method=='GET' for method,url in requests if url.startswith(base))
        assert not any(url==base+'api/rules' for _,url in requests)
        with sqlite3.connect(manifest['db']) as db:
            assert list(db.iterdump())==manifest['snapshot']
            assert db.execute('SELECT COUNT(*) FROM llm_calls').fetchone()[0]==0
        print('RULE_BROWSER_OK: 20+3 family and member pages, filters/counts, known miner, selected details, provenance, reload/back/focus, both themes at 1440/1100, scroll, GET failure/recovery, reversed responses, cache refresh, unchanged Store, zero model calls')
    except Exception:
        page.screenshot(path=str(out/'rules-failure.png'));(out/'rules-failure.html').write_text(page.content());raise
    finally:browser.close()
