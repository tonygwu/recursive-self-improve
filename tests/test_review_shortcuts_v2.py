"""V2 Review navigation and lesson-rejection guards use the shipped handlers."""
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def node(script):
    module = (ROOT / 'src/self_improve/dashboard/static/app.js').as_uri()
    code = f"import * as app from {json.dumps(module)};\n" + script
    run = subprocess.run(['node', '--input-type=module', '-e', code], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


SETUP = '''
const family={learning_id:'lesson / full',proposals:[{id:'p',revision:'r',reason_code:'pending'}],target_rows:[]};
app.state.route='review';app.state.review={families:[family]};app.state.selectedFamily=family.learning_id;
globalThis.location={hash:'#/review'};
const press=(key,extra={})=>{let prevented=false;const value=app.handleReviewKeydown({key,target:{tagName:'ARTICLE'},preventDefault(){prevented=true;},...extra});return {value,prevented,hash:location.hash};};
'''


def test_o_opens_only_the_selected_full_review_without_requests():
    result = node(SETUP + '''
let requests=0;globalThis.fetch=()=>{requests++;throw Error('Navigation must not request a command');};
const answer=press('o');console.log(JSON.stringify({answer,requests}));
''')
    assert result == {'answer': {'value': 'o', 'prevented': True, 'hash': '#/review/family/lesson%20%2F%20full'}, 'requests': 0}


def test_v2_review_shortcut_labels_preserve_scopes_and_accessible_names():
    result = node(SETUP + '''
const actions=app.renderReviewActions(family);
const lesson=actions.match(/<button[^>]*data-decision="reject_lesson"[^>]*>[\\s\\S]*?<\\/button>/)[0];
const card=app.renderReviewCard(family),link=card.match(/<a id="review-open-[\\s\\S]*?<\\/a>/)[0];
console.log(JSON.stringify({lesson,link,actions}));
''')
    assert 'aria-keyshortcuts="Shift+r"' in result['lesson']
    assert '<kbd class="kbd" aria-hidden="true">⇧r</kbd>' in result['lesson']
    assert 'aria-keyshortcuts="o"' in result['link']
    assert '<kbd class="kbd" aria-hidden="true">o</kbd>' in result['link']
    assert 'has no shortcut' not in result['actions']
    assert 'unselected and future targets' in result['actions']


def test_new_keys_ignore_unreviewed_members_modified_input_and_other_panels():
    result = node(SETUP + '''
const answers=[];
const ignored=[{defaultPrevented:true},{ctrlKey:true},{metaKey:true},{altKey:true},{isComposing:true},{repeat:true},
 {target:{tagName:'INPUT'}},{target:{tagName:'TEXTAREA'}},{target:{tagName:'SELECT'}},{target:{isContentEditable:true}}];
for(const key of ['o','R'])for(const extra of ignored)answers.push(press(key,{shiftKey:key==='R',...extra}));
for(const panel of ['#review-delivery','#review-eval','#review-incidents','#review-operations','#review-rollback'])
 for(const key of ['o','R'])answers.push(press(key,{shiftKey:key==='R',target:{closest:s=>s===panel}}));
app.state.route='overview';for(const key of ['o','R'])answers.push(press(key,{shiftKey:key==='R'}));app.state.route='review';
for(const selection of ['','missing-family']){app.state.selectedFamily=selection;answers.push(press('o'));answers.push(press('R',{shiftKey:true}));}
app.state.selectedFamily=family.learning_id;
const signature=JSON.stringify([['p','r']]);
for(const entry of [undefined,{loading:true},{error:'Read failed'},{signature:'stale',data:{members:[{}]}},
 {signature,data:{members:[]}}, {signature,error:'Stale',data:{members:[{}]}}, {signature,loading:true,data:{members:[{}]}}]) {
 app.state.reviewPreviews[family.learning_id]=entry;answers.push(press('R',{shiftKey:true}));
}
app.state.reviewPreviews[family.learning_id]={signature,data:{members:[{proposal_id:'p',revision:'auth'}]}};
answers.push(press('R')); // Caps Lock alone is not lesson rejection.
app.state.deciding[family.learning_id]=true;answers.push(press('R',{shiftKey:true}));
app.state.deciding={};app.state.reviewExcluded.p=true;answers.push(press('R',{shiftKey:true}));
console.log(JSON.stringify(answers));
''')
    assert all(not a['value'] and not a['prevented'] and a['hash'] == '#/review' for a in result)
