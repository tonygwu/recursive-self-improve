"""Complete zero-call human assessment and class policy journey on invented state."""
from pathlib import Path
import json,sqlite3
from playwright.sync_api import sync_playwright
from keyboard_actions import KeyboardActions
from visual_checks import VisualChecks
out=Path(__file__).resolve().parents[2]/'reports/dashboard-parity'
info=json.loads((out/'quality-demo-manifest.json').read_text())
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True);page=browser.new_page(viewport={'width':1440,'height':1024})
    keys=KeyboardActions(page,out/'quality-keyboard.json')
    errors=[];requests=[]
    visual=VisualChecks(page,out/'quality-contrast.json')
    page.on('pageerror',lambda e:errors.append(str(e)))
    page.on('request',lambda r:requests.append((r.method,r.url,r.post_data)))
    try:
        page.goto('http://127.0.0.1:8876/#/evals');page.wait_for_load_state('networkidle')
        page.screenshot(path=str(out/'quality-before.png'))
        (out/'quality-before.html').write_text(page.content())
        print('RECON J4',page.get_by_role('button').all_text_contents(),flush=True)
        assert 'Auto-apply, one target class at a time' in page.locator('#class-evidence').inner_text()
        switches=page.get_by_role('switch');assert switches.count()==4
        switch=page.get_by_role('switch',name='Auto-apply global rules',exact=True)
        assert switch.get_attribute('aria-checked')=='false'
        assert page.get_by_role('switch',name='Auto-apply hooks',exact=True).is_disabled()
        keys.activate(switch,'Space')
        page.wait_for_function('document.getElementById("policy-switch-global").getAttribute("aria-checked")==="true"')
        assert page.evaluate('document.activeElement.id')=='policy-switch-global'
        assert 'enabled for new eligible proposals' in page.locator('#policy-status').inner_text()
        page.reload();page.wait_for_load_state('networkidle');assert switch.get_attribute('aria-checked')=='true'
        keys.activate(switch);page.wait_for_function('document.getElementById("policy-switch-global").getAttribute("aria-checked")==="false"')
        keys.focused(switch)
        # Stable sample pages survive reload and do not reach monthly endpoint parameters.
        keys.activate(page.locator('#quality-history-toggle'))
        keys.activate(page.get_by_role('button',name='Older samples',exact=True))
        page.get_by_text('1 samples shown · 21 retained',exact=True).wait_for();assert 'quality_cursor=' in page.url
        page.reload();page.wait_for_load_state('networkidle');page.get_by_text('1 samples shown · 21 retained',exact=True).wait_for()
        keys.activate(page.get_by_role('button',name='Newest samples',exact=True))
        keys.activate(page.get_by_role('link',name='Review global rules sample →',exact=True))
        page.get_by_role('heading',name='Preview human assessment sample',exact=True).wait_for()
        page.get_by_text('2 selected from 2 eligible',exact=False).wait_for()
        assert page.evaluate('document.activeElement.id')=='quality-title'
        assert '1 application records excluded' in page.locator('#quality-detail').inner_text()
        keys.text(page.get_by_label('Sample size',exact=True),'1');keys.text(page.get_by_label('Reproducible seed',exact=True),'browser-quality-seed')
        keys.activate(page.get_by_role('button',name='Preview selection',exact=True))
        page.get_by_text('1 selected from 2 eligible',exact=False).wait_for()
        keys.activate(page.get_by_role('button',name='Save sample for assessment',exact=True))
        page.get_by_role('heading',name='Human assessment sample',exact=True).wait_for()
        keys.focused(page.get_by_role('heading',name='Human assessment sample',exact=True))
        saved_url=page.url
        detail=page.locator('#quality-detail');detail.get_by_text('0 / 1 reviewed',exact=False).wait_for()
        select=detail.get_by_label('Judgment for revision 1',exact=True)
        keys.choose(select,'uncertain');keys.text(detail.get_by_label('Assessment note',exact=True),'Need a concrete failure example before deciding.')
        keys.activate(detail.get_by_role('button',name='Record judgment',exact=True))
        page.get_by_text('Judgment recorded. Earlier judgments remain in history.',exact=True).wait_for()
        keys.focused(detail.get_by_role('button',name='Revise judgment',exact=True))
        assert '1 / 1 reviewed · 1 uncertain · 0 / 0 useful' in detail.inner_text()
        keys.choose(select,'useful');keys.text(detail.get_by_label('Assessment note',exact=True),'The exact change prevents the demonstrated error.')
        keys.activate(detail.get_by_role('button',name='Revise judgment',exact=True))
        page.get_by_text('Complete judgment history (2)',exact=True).wait_for()
        keys.focused(detail.get_by_role('button',name='Revise judgment',exact=True))
        assert '1 / 1 useful · fewer than 20 decided' in detail.inner_text()
        keys.activate(page.get_by_text('Complete judgment history (2)',exact=True))
        keys.activate(page.get_by_text('Complete applied content and source identities',exact=True))
        keys.activate(page.get_by_role('button',name='Refresh sample',exact=True))
        page.get_by_text('Complete judgment history (2)',exact=True).wait_for()
        assert detail.locator('details[open]').count()==2
        assert page.evaluate('document.activeElement.id')=='quality-refresh'
        for width in (1440,1280):
            page.set_viewport_size({'width':width,'height':1024})
            for theme in ('light','dark'):
                keys.theme(theme)
                page.locator('#main').evaluate('(e)=>e.scrollTop=0')
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                assert detail.evaluate('(e)=>e.scrollWidth<=e.clientWidth+1')
                visual.check('quality-detail')
                page.screenshot(path=str(out/f'quality-detail-{theme}-{width}.png'))
        page.reload();page.wait_for_load_state('networkidle');assert page.url==saved_url
        page.get_by_text('Complete judgment history (2)',exact=True).wait_for()
        assert select.input_value()=='useful'
        # Network failure retains the unsent assessment and allows an explicit retry.
        page.route('**/api/commands',lambda route:route.abort())
        keys.choose(select,'not_useful');keys.text(detail.get_by_label('Assessment note',exact=True),'Invented revised assessment after another observation.')
        keys.activate(detail.get_by_role('button',name='Revise judgment',exact=True))
        page.get_by_text('Judgment not confirmed.',exact=False).wait_for()
        keys.focused(detail.get_by_role('button',name='Revise judgment',exact=True))
        assert detail.get_by_label('Assessment note',exact=True).input_value().startswith('Invented revised')
        page.screenshot(path=str(out/'quality-keyboard-retry.png'))
        page.unroute('**/api/commands')
        keys.activate(detail.get_by_role('button',name='Revise judgment',exact=True))
        page.get_by_text('Complete judgment history (3)',exact=True).wait_for()
        keys.focused(detail.get_by_role('button',name='Revise judgment',exact=True))
        assert '0 / 1 useful' in detail.inner_text()
        keys.activate(page.get_by_role('link',name='← Evals & trends',exact=True));page.wait_for_load_state('networkidle')
        assert '0 / 1 useful' in page.locator('#policy-row-global').inner_text()
        for width in (1440,1280):
            page.set_viewport_size({'width':width,'height':1024})
            for theme in ('light','dark'):
                keys.theme(theme)
                page.locator('#main').evaluate('(e)=>e.scrollTop=0')
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                visual.check('quality-policy')
                page.screenshot(path=str(out/f'quality-policy-{theme}-{width}.png'))
        keys.activate(page.get_by_role('link',name='Review project rules sample →',exact=True))
        page.get_by_text('No verified applied revisions are eligible for this class.',exact=True).wait_for()
        assert page.get_by_role('button',name='Save sample for assessment',exact=True).is_disabled()
        assert all(Path(path).read_text()==text for path,text in info['targets'].items())
        with sqlite3.connect(info['db']) as db:
            assert db.execute('SELECT COUNT(*) FROM quality_samples').fetchone()[0]==22
            assert db.execute('SELECT COUNT(*) FROM quality_judgments').fetchone()[0]==3
            assert db.execute('SELECT COUNT(*) FROM execution_policy_events').fetchone()[0]==2
            assert db.execute('SELECT COUNT(*) FROM llm_calls').fetchone()[0]==0
        posts=[json.loads(body) for method,url,body in requests if method=='POST']
        assert {r['action'] for r in posts}=={'judge_quality','create_quality_sample','set_class_policy'}
        assert posts[-2]['request_key']==posts[-1]['request_key']
        assert all('/api/commands' in url for method,url,_ in requests if method!='GET')
        assert all('quality_cursor=' not in url for _,url,_ in requests if '/api/monthly' in url)
        assert not errors,errors
        report={'keyboard_steps':len(keys.steps),'requests':len(requests),'posts':len(posts),'judgment_revisions':3,
                'class_decisions':2,'retry_key_preserved':True,'model_calls':0,'targets_unchanged':True,
                'page_errors':errors,'theme_width_captures':8}
        (out/'quality-keyboard-result.json').write_text(json.dumps(report,indent=2)+'\n')
        visual.assert_clean()
        print('QUALITY_KEYBOARD_RESULT',json.dumps(report),flush=True)
        print('QUALITY_BROWSER_OK: native keyboard sample selection, three judgment revisions, retry, two class decisions, reload, pages, focus, 8 theme/width screenshots; zero model calls and unchanged targets.')
    except BaseException:
        page.screenshot(path=str(out/'quality-failure.png'));(out/'quality-failure.html').write_text(page.content());raise
    finally:keys.save();browser.close()
