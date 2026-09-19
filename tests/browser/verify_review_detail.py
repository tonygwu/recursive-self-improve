"""Native browser checks for full Review, with no decisions or private reads."""
from pathlib import Path
from urllib.parse import quote
import json,sqlite3
from playwright.sync_api import sync_playwright,expect
from visual_checks import VisualChecks
ROOT=Path(__file__).resolve().parents[2];OUT=ROOT/'reports/dashboard-parity/review-detail'
info=json.loads((OUT/'manifest.json').read_text());assert 'si-review-detail-' in info['db']
report={'requests':[],'errors':[],'captures':[],'models':0}
with sync_playwright() as runtime:
 browser=runtime.chromium.launch();page=browser.new_page(viewport={'width':1440,'height':1024})
 page.on('request',lambda r:report['requests'].append([r.method,r.url]))
 page.on('pageerror',lambda e:report['errors'].append(str(e)))
 visual=VisualChecks(page,OUT/'contrast.json')
 family='#/review/family/'+quote(info['family']);conflict='#/review/family/'+quote(info['conflict'])
 # Equal-size families have the reader's stable identity order, independent of random fixture IDs.
 toward_conflict='j' if info['family']<info['conflict'] else 'k'
 toward_family='k' if toward_conflict=='j' else 'j'
 def go(hash):
  page.goto('http://127.0.0.1:8876/'+hash);page.wait_for_load_state('networkidle')
 def capture(name,target=None):
  if target is None:page.locator('#main').evaluate('el=>el.scrollTop=0')
  else:target.scroll_into_view_if_needed()
  page.screenshot(path=str(OUT/(name+'.png')));report['captures'].append(name);visual.check(name)
 def detail():
  expect(page.locator('.review-detail')).to_have_attribute('data-learning-id',info['family'])
 try:
  go(family);detail()
  (OUT/'inspected.html').write_text(page.content());capture('first-render')
  expect(page.locator('.review-targets').first.locator('.review-card__diff')).to_be_visible()
  expect(page.locator('[data-review-panel="evaluations"]')).to_contain_text('3 / 3 passed with the rule; 0 / 3 without it')
  expect(page.locator('[data-review-panel="evaluations"]')).to_contain_text('No exact attempt link')
  expect(page.locator('.review-detail-trials li')).to_have_count(6)
  expect(page.locator('[data-decision="approve"]')).to_be_enabled()
  # The intended two-column hierarchy stays within the viewport in both themes.
  for width in (1280,1440):
   page.set_viewport_size({'width':width,'height':1024})
   for theme in ('light','dark'):
    if page.evaluate('async()=>(await import("/app.js")).effectiveTheme()')!=theme:page.get_by_role('button',name=theme.title()+' theme',exact=True).click()
    capture(f'populated-{theme}-{width}')
    assert page.locator('.review-detail-grid').evaluate('el=>el.scrollWidth<=el.clientWidth')
    assert page.locator('.review-detail-side').bounding_box()['width']==400
    assert page.get_by_role('heading',level=1).filter(visible=True).count()==1
    capture(f'decision-{theme}-{width}',page.locator('[data-review-panel="decision"]'))
    capture(f'paging-{theme}-{width}',page.locator('.review-detail-pages'))
  # Exact retained identities: a moved session cannot relabel the incident's repository.
  identities=page.locator('.review-detail-incident [data-source-identity]')
  expect(identities).to_have_count(3)
  expect(identities.nth(0)).to_contain_text('Codex · shared-native')
  expect(identities.nth(1)).to_contain_text('Claude Code · shared-native')
  expect(identities.nth(2)).to_contain_text('Agent unknown')
  for item in identities.all():
   expect(item).to_contain_text('invented/review');expect(item).not_to_contain_text('invented/moved')
  session_links=[identities.nth(i).get_by_role('link',name=('Codex' if i==0 else 'Claude Code')+' · shared-native',exact=True).get_attribute('href') for i in (0,1)]
  assert session_links[0]!=session_links[1]
  for href,label in zip(session_links,('Codex','Claude Code')):
   go(href);expect(page.locator('#inspector-body')).to_contain_text(label+' · shared-native')
  go(family);detail()
  project_link=page.locator('[id="review-identity:detail-incident-0:project:0"]')
  expect(project_link).to_have_attribute('href','#/projects/'+quote(info['project'],safe=''))
  project_link.click();page.wait_for_load_state('networkidle');expect(page.locator('#project-detail')).to_contain_text(info['project'])
  go(family);detail()
  aliases=page.locator('[id="review-identity:detail-incident-0:project:0:aliases"]');aliases.click();aliases.focus()
  page.evaluate('async()=>{(await import("/app.js")).paintReview()}');expect(aliases).to_be_focused();assert aliases.locator('..').evaluate('el=>el.open')
  capture('retained-identity-aliases',aliases.locator('..'))
  # All seven incidents are reachable, without changing selection; disabled paging retains focus.
  next_page=page.locator('#review-evidence-next');next_page.focus();page.keyboard.press('Enter')
  expect(page.locator('#review-evidence-page')).to_have_text('4–6 of 7');expect(next_page).to_be_focused()
  expect(page.locator('.review-detail-incident [data-source-identity]').nth(1)).to_contain_text('Native ID unknown')
  page.keyboard.press('Enter');expect(page.locator('#review-evidence-page')).to_have_text('7–7 of 7');expect(page.locator('#review-evidence-page')).to_be_focused()
  expect(page.locator('.review-detail-incident [data-source-identity]')).to_contain_text('Project unknown')
  page.locator('#review-evidence-prev').click();page.locator('#review-evidence-prev').click()
  # Truncated matched text absent from window_json is still complete in the exact record.
  retained=page.locator('#review-retained-detail\\:detail-incident-0');retained.click()
  complete=page.locator('#review-full-incident-detail\\:detail-incident-0');complete.click()
  expect(complete.locator('..')).to_contain_text('UNIQUE COMPLETE TAIL')
  complete.focus();page.evaluate('async()=>{(await import("/app.js")).paintReview()}');expect(complete).to_be_focused()
  assert complete.locator('..').evaluate('el=>el.open')
  capture('expanded-complete-incident',complete.locator('..'))
  # Trial evidence and exact full-attempt navigation are explicit GET routes.
  result_body=page.locator('#review-evaluation-'+info['result']+' > details')
  if not result_body.evaluate('el=>el.open'):result_body.locator(':scope > summary').click()
  trial=page.locator('.review-detail-trials summary').first;trial.click();trial.focus()
  page.evaluate('async()=>{(await import("/app.js")).paintReview()}');expect(trial).to_be_focused();assert trial.locator('..').evaluate('el=>el.open')
  expect(trial.locator('..')).to_contain_text('returncode')
  page.get_by_role('link',name='All scenarios, transcripts and provenance',exact=True).click();page.wait_for_load_state('networkidle')
  expect(page.locator('#evaluation-detail')).to_contain_text(info['attempt'])
  go(family)
  # Family and proposal identities remain distinct; unrelated navigation controls stay live.
  page.evaluate('window.reviewNavigationNode=document.querySelector("#navigation-open")')
  page.keyboard.press('Control+k');page.keyboard.type('ar');page.keyboard.press('Escape');detail()
  assert page.evaluate('window.reviewNavigationNode===document.querySelector("#navigation-open")')
  page.keyboard.press(toward_conflict);page.wait_for_load_state('networkidle')
  expect(page).to_have_url('http://127.0.0.1:8876/'+conflict)
  expect(page.locator('[data-decision="approve"]')).to_be_disabled()
  expect(page.locator('.review-targets > summary')).to_contain_text('Conflict')
  expect(page.locator('.review-selected-preview')).not_to_contain_text('Reading the selected edits')
  expect(page.locator('.review-selected-preview .review-card__diff')).to_have_count(0);capture('conflict')
  page.locator('[data-review-individual]').click()
  check=page.locator('#review-include-'+info['conflict_proposals'][1]);check.focus();page.keyboard.press('Space')
  expect(page.locator('[data-decision="approve"]')).to_be_enabled();expect(check).to_be_focused()
  page.keyboard.press('Escape');page.wait_for_load_state('networkidle')
  expect(page).to_have_url('http://127.0.0.1:8876/#/review');expect(page.locator('.review-card--selected')).to_have_attribute('data-learning-id',info['conflict'])
  page.get_by_role('link',name='Open full review',exact=True).click();expect(page).to_have_url('http://127.0.0.1:8876/'+conflict)
  expect(page.locator('.review-detail')).to_have_attribute('data-learning-id',info['conflict']);expect(page.locator('.review-detail')).to_be_focused()
  page.keyboard.press(toward_family);page.wait_for_load_state('networkidle');detail()
  go('#/review/proposal/'+info['proposals'][1]);expect(page.locator('#review-include-'+info['proposals'][1])).to_be_checked()
  expect(page.locator('#review-include-'+info['proposals'][0])).not_to_be_checked()
  go(family);expect(page.locator('[data-review-panel="evaluations"]')).not_to_contain_text('Comparable paired trials')
  page.reload();page.wait_for_load_state('networkidle');detail()  # Reload defaults to all current family members.
  # A stale identity packet never labels current evidence or changes approval authority.
  def wrong_identity(route):
   response=route.fetch();body=response.json();body['evidence_identity']['records'][0]['source_revision']='0'*64
   route.fulfill(response=response,json=body)
  page.route('**/api/review-preview?*',wrong_identity)
  page.reload();page.wait_for_load_state('networkidle')
  expect(page.locator('.review-detail')).to_contain_text('Identity labels do not match the retained incident source')
  expect(page.locator('.review-detail-incident [data-source-identity]')).to_have_count(0)
  expect(page.locator('[data-decision="approve"]')).to_be_enabled();capture('identity-mismatch')
  page.unroute('**/api/review-preview?*');page.get_by_role('button',name='Reload Review',exact=True).click()
  expect(page.locator('.review-detail-incident [data-source-identity]')).to_have_count(3)
  # Failed supplemental evidence cannot be promoted to a result; retry preserves identity/focus.
  page.route('**/api/eval-results/'+info['result'],lambda r:r.fulfill(status=503,json={'detail':'Invented exact-result read failure'}))
  page.reload();page.wait_for_load_state('networkidle');expect(page.locator('.review-detail')).to_contain_text('Invented exact-result read failure');capture('evaluation-error')
  page.unroute('**/api/eval-results/'+info['result']);retry=page.get_by_role('button',name='Retry evaluation evidence');retry.click()
  expect(page.locator('.review-detail')).to_contain_text('Comparable paired trials');detail();expect(page.locator('#review-evaluation-'+info['result'])).to_be_focused()
  # A failed selected preview disables every decision, and the normal reload recovers it.
  page.route('**/api/review-preview?*',lambda r:r.fulfill(status=503,json={'detail':'Invented selected-preview failure'}))
  page.reload();page.wait_for_load_state('networkidle')
  for name in ('approve','reject','reject_lesson'):expect(page.locator('[data-decision="'+name+'"]')).to_be_disabled()
  capture('preview-error');page.unroute('**/api/review-preview?*');page.get_by_role('button',name='Reload Review',exact=True).click()
  expect(page.locator('[data-decision="approve"]')).to_be_enabled()
  # Hold a read while navigating: late completion cannot replace another family.
  held=[]
  def hold(route):held.append((route,route.fetch()))
  page.route('**/api/eval-results/'+info['result'],hold)
  page.evaluate('async id=>{const a=await import("/app.js");a.loadReviewEvaluation(id,{refresh:true});a.paintReview()}',info['result'])
  expect(page.get_by_text('Reading exact evaluation provenance…',exact=True)).to_be_visible()
  page.locator('#review-detail-revisions').click();page.locator('#review-detail-revisions').focus()
  assert held
  held[0][0].fulfill(response=held[0][1]);expect(page.locator('.review-detail')).to_contain_text('Comparable paired trials');expect(page.locator('#review-detail-revisions')).to_be_focused()
  held.clear();page.evaluate('async id=>{const a=await import("/app.js");a.loadReviewEvaluation(id,{refresh:true});a.paintReview()}',info['result'])
  expect(page.get_by_text('Reading exact evaluation provenance…',exact=True)).to_be_visible()
  page.keyboard.press(toward_conflict);expect(page).to_have_url('http://127.0.0.1:8876/'+conflict)
  expect(page.locator('.review-detail')).to_have_attribute('data-learning-id',info['conflict'])
  individual=page.locator('[data-review-individual]')
  if individual.get_attribute('aria-expanded')!='true':individual.click()
  member_diff=page.locator('.review-member').first.get_by_text('Complete proposed edit',exact=True)
  member_diff.click();member_diff.focus()
  page.evaluate('window.reviewOtherFamilyNode=document.querySelector(".review-detail")')
  held[0][0].fulfill(response=held[0][1]);page.unroute('**/api/eval-results/'+info['result']);page.wait_for_load_state('networkidle')
  expect(page.locator('.review-detail')).to_have_attribute('data-learning-id',info['conflict']);expect(member_diff).to_be_focused()
  assert page.evaluate('window.reviewOtherFamilyNode===document.querySelector(".review-detail")')
  page.evaluate('async()=>{(await import("/app.js")).paintReview()}');expect(member_diff).to_be_focused();assert member_diff.locator('..').evaluate('el=>el.open')
  # A removed family URL never silently offers the remaining family's controls.
  go('#/review/family/missing-family');expect(page.get_by_role('heading',name='This family is no longer in Review')).to_be_visible()
  expect(page.locator('#review-body [data-decision]')).to_have_count(0);capture('missing-family')
  visual.assert_clean();assert not report['errors'],report['errors'];assert all(method=='GET' for method,url in report['requests']),report['requests']
  with sqlite3.connect('file:'+info['db']+'?mode=ro',uri=True) as db:assert list(db.iterdump())==info['snapshot']
  assert all(Path(path).read_text()==before for path,before in info['targets'].items())
  report.update(read_only_requests=len(report['requests']),store_before_decision='unchanged',targets='unchanged')
  go(family);page.reload();page.wait_for_load_state('networkidle');detail()
  approve=page.locator('[data-decision="approve"]');expect(approve).to_be_enabled();approve.focus();page.keyboard.press('Enter')
  expect(page.get_by_role('heading',name='This family is no longer in Review')).to_be_visible()
  expect(page.locator('#review-note')).to_contain_text('Approval recorded')
  expect(page.locator('#review-body [data-decision]')).to_have_count(0)
  expect(page.locator('[data-review-empty]')).to_be_focused();capture('approval-recorded')
  with sqlite3.connect('file:'+info['db']+'?mode=ro',uri=True) as db:
   db.row_factory=sqlite3.Row
   approvals=[dict(r) for r in db.execute("SELECT * FROM commands WHERE action='approve'")]
   assert len(approvals)==1 and approvals[0]['state']=='queued',approvals
   assert set(r[0] for r in db.execute("SELECT id FROM proposals WHERE status='approved_user'"))==set(info['proposals'])
  assert all(Path(path).read_text()==before for path,before in info['targets'].items())
  report.update(decision='one exact two-member approval queued; targets unchanged')
  assert sum(method=='POST' for method,url in report['requests'])==1
  assert not report['errors'],report['errors'];visual.assert_clean()
  (OUT/'report.json').write_text(json.dumps(report,indent=2))
  print('REVIEW_DETAIL_OK',json.dumps({**report,'requests':len(report['requests'])}),flush=True)
 finally:
  (OUT/'debug-state.json').write_text(json.dumps(page.evaluate('async()=>{const a=await import("/app.js");return {selected:a.state.selectedFamily,detail:a.state.reviewDetailFamily,order:a.reviewOrder(),active:document.activeElement?.outerHTML}}'),indent=2));page.screenshot(path=str(OUT/'last-state.png'));(OUT/'last-state.html').write_text(page.content());browser.close()
