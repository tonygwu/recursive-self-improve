"""Actual complete-source browsing and four recovery journeys on temporary state."""
from pathlib import Path
from urllib.parse import quote
import json, sqlite3, time
from playwright.sync_api import sync_playwright, expect
from keyboard_actions import KeyboardActions
from visual_checks import VisualChecks

out=Path(__file__).resolve().parents[2]/'reports/dashboard-parity'
manifest=json.loads((out/'evidence-browser-manifest.json').read_text())
base='http://127.0.0.1:8876/'
assert 'si-evidence-ui-' in manifest['db']
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True);page=browser.new_page(viewport={'width':1440,'height':1024})
    errors=[];requests=[];journeys={}
    keys=KeyboardActions(page,out/'evidence-keyboard.json')
    visual=VisualChecks(page,out/'evidence-contrast.json')
    page.on('pageerror',lambda error:errors.append(str(error)))
    page.on('request',lambda request:requests.append((request.method,request.url)))
    def goto(path):
        page.goto(base+path);page.wait_for_load_state('networkidle')
    def wait_for_source(kind,id,tab):
        page.wait_for_function("async ([kind,id,tab])=>{const {state}=await import('/app.js');return state.inspectorKind==='evidence' && state.selectedEvidence?.kind===kind && state.selectedEvidence?.id===id && state.inspectorTab===tab && !state.evidenceDetail?.loading && !!state.evidenceDetail?.data}",arg=[kind,id,tab])
        page.locator('#inspector-body [role="status"]').wait_for(state='detached')
        page.locator('#inspector-body [data-evidence-detail-refresh]:not([disabled])').wait_for()
        assert not page.locator('#inspector-body [role="alert"]').count(),page.locator('#inspector-body').inner_text()
    def source(kind,id,tab='diagnosis'):
        goto('#/rules/evidence/'+kind+'/'+quote(id,safe='')+'?mode=evidence&tab='+tab)
        wait_for_source(kind,id,tab)
    try:
        goto('#/rules?mode=evidence&kinds=learning')
        page.locator('#evidence-results a').nth(19).wait_for()
        page.screenshot(path=str(out/'evidence-before.png'));(out/'evidence-before.html').write_text(page.content())
        print('RECON_BUTTONS',page.locator('#view-rules button').all_text_contents())
        assert page.locator('#evidence-results .evidence-result').count()==20
        first=page.locator('[data-evidence-key]').evaluate_all('(els)=>els.map(e=>e.dataset.evidenceKey)')
        keys.activate(page.get_by_role('button',name='Next results',exact=True))
        page.wait_for_function('document.getElementById("rules-foot").textContent.includes("21–40")')
        assert page.evaluate('document.activeElement.id')=='rules-meta'
        assert not set(first)&set(page.locator('[data-evidence-key]').evaluate_all('(els)=>els.map(e=>e.dataset.evidenceKey)'))
        second=page.url;page.reload();page.wait_for_function('document.getElementById("rules-foot").textContent.includes("21–40")')
        page.go_back();page.wait_for_function('document.getElementById("rules-foot").textContent.includes("1–20")')
        page.go_forward();page.wait_for_url(second);page.wait_for_function('document.getElementById("rules-foot").textContent.includes("21–40")')
        keys.choose(page.get_by_label('Source',exact=True),'incident')
        page.wait_for_function('document.getElementById("evidence-kind").value==="incident" && document.querySelector(".evidence-kind")?.textContent==="Incident"')
        search=page.get_by_role('searchbox',name='Search rules and linked evidence')
        keys.text(search,'Needle archive end');page.wait_for_function("document.getElementById('rules-meta').textContent.startsWith('1 matching sources') && new URLSearchParams(location.hash.split('?')[1]).get('query')==='Needle archive end'")
        page.locator('[data-evidence-key="incident:i05"]').wait_for()
        assert page.locator('#evidence-results .evidence-result').count()==1
        before=page.url;keys.activate(page.locator('[data-evidence-key="incident:i05"]'))
        page.get_by_role('link',name='Source',exact=True).wait_for();keys.activate(page.get_by_role('link',name='Source',exact=True))
        page.get_by_text('Complete retained window',exact=True).wait_for()
        assert page.locator('.evidence-window pre.evidence-text').inner_text().endswith('Needle at full archive end.')
        assert page.evaluate('document.activeElement.dataset.tab')=='source'
        assert page.locator('#rules-inspector-host > #inspector').is_visible()
        long=page.locator('.evidence-window pre.evidence-text');keys.reach(long);page.keyboard.press('End')
        visual.themes(keys,'keyboard-long-evidence',out)
        keys.reach(long)
        page.keyboard.press('Escape');page.wait_for_url(before)
        assert page.evaluate('document.activeElement.dataset.evidenceKey')=='incident:i05'
        # Delayed linked reads retain the activated section; never reclaim focus
        # after the operator moves into the search field.
        source('learning','l00')
        held=[];page.route('**/api/learnings/l00/evidence*',lambda route:held.append(route))
        linked=page.get_by_role('navigation',name='Inspector sections').get_by_role('link',name='Linked evidence',exact=True)
        keys.activate(linked);expect(page.get_by_text('Reading linked incidents…',exact=True)).to_be_visible()
        expect(linked).to_be_focused();assert len(held)==1
        held.pop().continue_();page.get_by_text('1–20 of 25 linked incidents',exact=True).wait_for()
        expect(linked).to_be_focused();page.screenshot(path=str(out/'evidence-linked-focused.png'))
        keys.activate(page.get_by_role('button',name='Next linked',exact=True))
        expect(page.get_by_text('Reading linked incidents…',exact=True)).to_be_visible()
        assert len(held)==1;held.pop().continue_()
        page.get_by_text('21–25 of 25 linked incidents',exact=True).wait_for()
        expect(page.locator('#evidence-linked-status')).to_be_focused()
        keys.activate(page.get_by_role('button',name='First linked',exact=True))
        expect(page.get_by_text('Reading linked incidents…',exact=True)).to_be_visible()
        keys.reach(page.get_by_role('searchbox',name='Search rules and linked evidence'))
        assert len(held)==1;held.pop().continue_()
        page.get_by_text('1–20 of 25 linked incidents',exact=True).wait_for()
        expect(page.get_by_role('searchbox',name='Search rules and linked evidence')).to_be_focused()
        page.screenshot(path=str(out/'evidence-linked-focus-left.png'))
        page.unroute('**/api/learnings/l00/evidence*')
        source('learning','l00','linked');page.get_by_text('1–20 of 25 linked incidents',exact=True).wait_for()
        assert page.locator('#inspector-body [data-evidence-key]').count()==20
        keys.activate(page.get_by_role('button',name='Next linked',exact=True));page.get_by_text('21–25 of 25 linked incidents',exact=True).wait_for()
        linked_url=page.url;page.reload();page.get_by_text('21–25 of 25 linked incidents',exact=True).wait_for()
        assert page.locator('#inspector-body [data-evidence-key]').count()==5
        assert page.locator('#inspector-body a').filter(has_text='p00').count()==1
        keys.activate(page.locator('#inspector-body [data-evidence-key]').first);page.locator('#inspector-body [data-evidence-detail-refresh]:not([disabled])').wait_for()
        page.go_back();page.wait_for_url(linked_url);page.get_by_text('21–25 of 25 linked incidents',exact=True).wait_for()
        source('incident','i06','source');page.get_by_text('Complete retained window',exact=True).wait_for()
        assert '3 recorded in session' in page.locator('#inspector-body').inner_text()
        assert 'Repeated error original text' in page.locator('#inspector-body').inner_text()
        source('learning','l44','source');page.get_by_text('Complete rule',exact=True).wait_for()
        assert '<script>window.privateSentinel=1</script>' in page.locator('#inspector-body').inner_text()
        visual.themes(keys,'keyboard-complete-rule',out)
        assert page.evaluate('window.privateSentinel') is None and page.locator('#inspector-body script,#inspector img').count()==0
        for kind in ('session','instruction','revision'):
            listing=page.request.get(base+'api/evidence?kinds='+kind).json();assert listing['rows']
            source(kind,listing['rows'][0]['source_id'],'source')
            assert page.locator('#inspector-body').get_by_text('Complete source fields and identity',exact=True).count()==1
        # Report-only native sessions keep provider identity without a scanned transcript.
        native_sid=manifest['native_sid']
        goto('#/rules?mode=evidence&kinds=session&query='+native_sid)
        provider_labels={'claude':'Claude Code','codex':'Codex'}
        for provider,label in provider_labels.items():
            page.get_by_text(label+' session '+native_sid,exact=True).wait_for()
        assert page.locator('#evidence-results .evidence-result').count()==2
        for provider,report in manifest['native_reports'].items():
            keys.activate(page.locator('[data-evidence-key="session:'+report['logical_session_key']+'"]'))
            wait_for_source('session',report['logical_session_key'],'diagnosis')
            keys.activate(page.get_by_role('link',name='Source',exact=True))
            page.locator('#inspector-body [data-source-identity]').get_by_role('link',name=provider_labels[provider]+' · '+native_sid,exact=True).wait_for()
            keys.activate(page.get_by_text('Native reports 1',exact=True))
            body=page.locator('#inspector-body').inner_text()
            assert '"source": "'+provider+'"' in body
            assert '"occurred_at": null' in body and '"loaded_content_hash": null' in body
            assert report['id'] in body and 'No records retained.' in body
            visual.themes(keys,'report-only-'+provider,out)
            page.reload();page.wait_for_load_state('networkidle')
            page.locator('#inspector-body [data-source-identity]').get_by_role('link',name=provider_labels[provider]+' · '+native_sid,exact=True).wait_for()
            page.keyboard.press('Escape')
            page.locator('#evidence-results .evidence-result').nth(1).wait_for()
        goto('#/rules?mode=evidence')
        keys.choose(page.get_by_label('Project',exact=True),manifest['project_key'])
        page.wait_for_function('(key)=>new URLSearchParams(location.hash.split("?")[1]).get("project_key")===key',arg=manifest['project_key'])
        page.locator('#rules-notice [role="status"]').wait_for(state='detached')
        assert page.locator('#evidence-results a').count()>0
        # Four real read-to-preview journeys. No request is submitted and no model is invoked.
        start=time.monotonic();source('incident','unmined')
        keys.activate(page.get_by_role('link',name='Preview mining this incident',exact=True))
        page.get_by_role('button',name='Mine this incident · up to 1 call',exact=True).wait_for()
        assert 'retained incident window' in page.locator('#eval-job-body').inner_text();journeys['unmined_to_mining_preview']=time.monotonic()-start
        start=time.monotonic();source('learning','l00')
        keys.activate(page.get_by_role('link',name='Review this proposal',exact=True))
        keys.activate(page.locator('.review-selected-preview .review-targets > summary'))
        page.get_by_text('Complete combined edit',exact=True).wait_for();keys.activate(page.get_by_text('Complete combined edit',exact=True))
        assert 'Complete proposal only token.' in page.locator('#review-body').inner_text()
        assert page.evaluate('(async()=>{const m=await import("/app.js");return m.state.selectedFamily})()')=='l00';journeys['pending_to_selected_review']=time.monotonic()-start
        start=time.monotonic();source('learning','l00')
        keys.activate(page.get_by_role('link',name='Preview hook recovery',exact=True))
        page.get_by_role('link',name='Claude command hook',exact=True).wait_for();keys.activate(page.get_by_role('link',name='Claude command hook',exact=True))
        page.get_by_role('button',name='Generate recovery proposal · up to 1 call',exact=True).wait_for()
        visual.check('hook-recovery-preview')
        assert 'This proposes a Claude command hook' in page.locator('#eval-job-body').inner_text();journeys['reported_violation_to_hook_preview']=time.monotonic()-start
        start=time.monotonic();source('incident','copy-incident')
        page.get_by_text('Recorded copy availability: absent',exact=True).wait_for()
        assert 'observation after incident' in page.locator('#inspector-body').inner_text()
        keys.activate(page.get_by_role('link',name='Preview a corrected target',exact=True))
        target=page.get_by_role('link',name=manifest['project_key']+' · AGENTS.md',exact=True);target.wait_for();keys.activate(target)
        page.get_by_role('button',name='Generate recovery proposal · up to 1 call',exact=True).wait_for()
        assert 'working-copy/AGENTS.md' in page.locator('#eval-job-body').inner_text();journeys['absent_copy_to_correct_target_preview']=time.monotonic()-start
        assert all(seconds<60 for seconds in journeys.values()),journeys
        # Exact execution records outside the first history pages, including reload.
        commands=page.request.get(base+'api/commands?summary=true&limit=10').json()['commands']
        ops=page.request.get(base+'api/operations?summary=true&limit=10').json()['operations']
        assert manifest['command'] not in {c['id'] for c in commands} and manifest['operation'] not in {o['id'] for o in ops}
        for kind in ('command','operation'):
            goto('#/review/'+kind+'/'+manifest[kind]);card=page.locator('#'+('delivery-command-' if kind=='command' else 'operation-')+manifest[kind]);card.wait_for()
            page.wait_for_function('(id)=>document.activeElement.id===id',arg=card.get_attribute('id'))
            assert card.locator('details').first.get_attribute('open') is not None
            page.reload();card.wait_for();page.wait_for_function('(id)=>document.activeElement.id===id',arg=card.get_attribute('id'))
        selected=page.request.get(base+'api/commands/'+manifest['command']).json()['members'][0]['proposal_id']
        goto('#/review/proposal/'+selected)
        page.get_by_text('Selected proposal',exact=True).wait_for()
        assert selected in page.locator('#review-selected-proposal').inner_text()
        assert 'approved_user' in page.locator('#review-selected-proposal').inner_text()
        assert not page.locator('#review-body [data-decision]').count()
        goto('#/review/command/nonexistent-fixture-command');page.locator('#review-execution [role="alert"]').wait_for()
        assert 'nonexistent-fixture-command' in page.locator('#review-execution').inner_text()
        # Actual source errors remain visible; explicit refresh recovers.
        source('incident','i05')
        page.route('**/api/evidence/incident/i05',lambda route:route.abort())
        keys.activate(page.get_by_role('button',name='Refresh evidence',exact=True));page.locator('#inspector-body [role="alert"]').wait_for()
        page.unroute('**/api/evidence/incident/i05');keys.activate(page.get_by_role('button',name='Refresh evidence',exact=True))
        page.locator('#inspector-body [role="alert"]').wait_for(state='detached');page.locator('#inspector-body [data-evidence-detail-refresh]:not([disabled])').wait_for()
        # An actual retained-source edit invalidates a cursor. Restore the fixture afterward.
        goto('#/rules?mode=evidence&kinds=incident');page.locator('#rules-notice [role="status"]').wait_for(state='detached')
        keys.activate(page.get_by_role('button',name='Next results',exact=True));page.wait_for_function('document.getElementById("rules-foot").textContent.includes("21–28")')
        # Status changes now leave immutable queue observations. Change retained
        # text instead, so restoring the fixture also preserves that audit log.
        with sqlite3.connect(manifest['db']) as db:
            original_text=db.execute("SELECT matched_text FROM incidents WHERE id='i00'").fetchone()[0]
            db.execute("UPDATE incidents SET matched_text=? WHERE id='i00'",(original_text+' Invented cursor invalidation.',))
        page.reload();page.locator('#rules-notice [role="alert"]').wait_for()
        assert 'EvidenceChanged' in page.locator('#rules-notice').inner_text()
        keys.activate(page.get_by_role('button',name='Refresh',exact=True));page.locator('#rules-notice [role="alert"]').wait_for(state='detached')
        with sqlite3.connect(manifest['db']) as db:db.execute("UPDATE incidents SET matched_text=? WHERE id='i00'",(original_text,))
        # Real exported request handlers with deliberately reversed completion order.
        source('incident','i05')
        race=page.evaluate('''async()=>{
          const m=await import('/app.js'),original=window.fetch;
          const d5=await (await original('/api/evidence/incident/i05')).json(),d6=await (await original('/api/evidence/incident/i06')).json();
          const pending={};window.fetch=(url)=>new Promise(resolve=>pending[url]=resolve);
          try{
            const first=m.openEvidence('incident','i05',{refresh:true}),second=m.openEvidence('incident','i06');
            pending['/api/evidence/incident/i06']({ok:true,json:async()=>d6});await second;
            pending['/api/evidence/incident/i05']({ok:true,json:async()=>d5});await first;
            return {id:m.state.selectedEvidence.id,source:m.state.evidenceDetail.data.source_id};
          }finally{window.fetch=original;}
        }''');assert race=={'id':'i06','source':'i06'},race
        goto('#/review/command/'+manifest['command'])
        page.locator('#delivery-command-'+manifest['command']).wait_for()
        page.wait_for_function('(id)=>document.activeElement.id===id',arg='delivery-command-'+manifest['command'])
        refreshed=page.evaluate('''async(id)=>{
          const m=await import('/app.js'),original=window.fetch;clearInterval(m.state.deliveryTimer);
          if(m.state.delivery.promise)await m.state.delivery.promise;
          const before=await (await original('/api/commands/'+id)).json();
          const after={...before,state:'completed',updated_at:'2031-01-01T00:00:00Z',targets:before.targets.map(t=>({...t,state:'completed'}))};
          m.state.deliveryDetails[id]={data:before};m.state.delivery.items=[before];m.state.delivery.loaded=true;
          const calls=[];
          window.fetch=async(url,opts)=>{calls.push(url);if(url==='/api/commands?summary=true&limit=10')return {ok:true,json:async()=>({commands:[after],next_cursor:null})};if(url==='/api/commands/'+id)return {ok:true,json:async()=>after};return original(url,opts);};
          try{await m.loadDeliveryHistory();return {summary:m.state.delivery.items[0].state,detail:m.state.deliveryDetails[id].data.state,target:m.state.deliveryDetails[id].data.targets[0].state,fullRead:calls.includes('/api/commands/'+id),rollback:document.getElementById('delivery-command-'+id).textContent.includes('Review rollback')};}
          finally{window.fetch=original;}
        }''',manifest['command'])
        assert refreshed=={'summary':'completed','detail':'completed','target':'completed','fullRead':True,'rollback':True},refreshed
        page.reload();page.wait_for_load_state('networkidle')
        # Final screenshots: source selection and meaningful diagnosis at both design widths/themes.
        source('incident','copy-incident');page.get_by_text('Recorded copy availability: absent',exact=True).wait_for()
        for width in (1440,1280,1100):
            page.set_viewport_size({'width':width,'height':1024})
            for theme in ('light','dark'):
                keys.theme(theme)
                page.locator('#main').evaluate('(el)=>el.scrollTop=0')
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                assert page.locator('#rules-inspector-host > #inspector').is_visible()
                visual.check('evidence-diagnosis')
                page.screenshot(path=str(out/f'evidence-{theme}-{width}.png'))
        assert not errors,errors
        assert all(method=='GET' for method,_ in requests),requests
        with sqlite3.connect(manifest['db']) as db:
            assert list(db.iterdump())==manifest['snapshot']
            assert db.execute('SELECT COUNT(*) FROM llm_calls').fetchone()[0]==0
        assert all(Path(path).read_text()==text for path,text in manifest['targets'].items())
        (out/'evidence-browser-results.json').write_text(json.dumps({'journeys_seconds':journeys,'request_count':len(requests),'methods':sorted({m for m,_ in requests}),'console_errors':errors},indent=2))
        visual.assert_clean()
        print('EVIDENCE_BROWSER_OK',json.dumps(journeys))
    except Exception:
        page.screenshot(path=str(out/'evidence-failure.png'));(out/'evidence-failure.html').write_text(page.content());raise
    finally:keys.save();browser.close()
