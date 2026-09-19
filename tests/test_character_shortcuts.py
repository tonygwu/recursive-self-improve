"""Browser-local character shortcut consent, using the shipped handlers."""
import json
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def node(script):
    module = (ROOT / "src/self_improve/dashboard/static/app.js").as_uri()
    run = subprocess.run(
        ["node", "--input-type=module", "-e", f"import * as app from {json.dumps(module)};\n" + script],
        capture_output=True, text=True, timeout=30,
    )
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


SETUP = """
const family={learning_id:'invented-shortcut-lesson',proposals:[{id:'p',revision:'r',reason_code:'pending'}],target_rows:[]};
app.state.route='review';app.state.review={families:[family]};app.state.selectedFamily=family.learning_id;
app.state.reviewPreviews[family.learning_id]={signature:JSON.stringify([['p','r']]),
 data:{ready:true,revision:'combined',targets:[],members:[{proposal_id:'p',revision:'auth'}]}};
globalThis.location={hash:'#/review'};
const requests=[];globalThis.fetch=(url,options)=>{requests.push({url,options});return new Promise(()=>{});};
const elements=new Map();
globalThis.document={getElementById(id){if(!elements.has(id))elements.set(id,{style:{},setAttribute(){},querySelectorAll(){return[];}});return elements.get(id);},querySelector(){return null;},querySelectorAll(){return[];}};
const press=(key,extra={})=>{let prevented=false;const result=app.handleReviewKeydown({key,target:{tagName:'ARTICLE'},preventDefault(){prevented=true;},...extra});return {result,prevented,hash:location.hash};};
"""


@pytest.mark.parametrize("key", ["j", "k", "o", "a", "r", "R"])
@pytest.mark.parametrize("focus", ["ARTICLE", "BUTTON", "A"])
def test_off_keys_never_consume_navigate_select_or_request(key, focus):
    result = node(SETUP + f"""
app.state.characterShortcuts={{enabled:false,notice:''}};
const answer=press({json.dumps(key)},{{shiftKey:{str(key == 'R').lower()},target:{{tagName:{json.dumps(focus)}}}}});
await new Promise(resolve=>setImmediate(resolve));
console.log(JSON.stringify({{answer,selected:app.state.selectedFamily,requests}}));
""")
    assert result == {
        "answer": {"result": "", "prevented": False, "hash": "#/review"},
        "selected": "invented-shortcut-lesson", "requests": [],
    }


def test_storage_absent_on_off_invalid_and_denied_reads():
    result = node("""
const values=[null,'on','off','garbage','true',''];
const reads=values.map(value=>app.readCharacterShortcuts({getItem(key){if(key!=='self-improve-character-shortcuts')throw Error(key);return value;}}));
reads.push(app.readCharacterShortcuts(null),app.readCharacterShortcuts({getItem(){throw Error('denied');}}));
console.log(JSON.stringify(reads));
""")
    assert [item["enabled"] for item in result] == [True, True, False, False, False, False, False, False]
    assert all(not item["notice"] for item in result[:3])
    assert all(item["notice"] for item in result[3:])


def test_toggle_reload_and_failed_write_keep_an_honest_session_choice():
    result = node("""
let saved=null;const store={getItem:()=>saved,setItem(key,value){saved=value;}};
app.loadCharacterShortcuts(store);
const initial={...app.state.characterShortcuts};app.toggleCharacterShortcuts(store);
const off={...app.state.characterShortcuts},storedOff=saved;
app.state.route='rules';app.loadCharacterShortcuts(store);
const reload={...app.state.characterShortcuts};app.toggleCharacterShortcuts({setItem(){throw Error('denied');}});
const unsavedOn={...app.state.characterShortcuts};app.toggleCharacterShortcuts(null);
const unsavedOff={...app.state.characterShortcuts};
console.log(JSON.stringify({initial,off,storedOff,reload,unsavedOn,unsavedOff}));
""")
    assert result["initial"] == {"enabled": True, "notice": ""}
    assert result["off"] == result["reload"] == {"enabled": False, "notice": ""}
    assert result["storedOff"] == "off"
    assert result["unsavedOn"]["enabled"] is True
    assert result["unsavedOff"]["enabled"] is False
    assert "could not be saved" in result["unsavedOn"]["notice"]
    assert "this page only" in result["unsavedOff"]["notice"]


def test_disabled_markup_removes_key_attributes_but_preserves_native_escape():
    result = node(SETUP + """
app.state.characterShortcuts={enabled:false,notice:''};
const html=app.renderReviewActions(family)+app.renderReviewCard(family);
app.state.reviewDetailFamily=family.learning_id;
location.hash='#/review/family/invented-shortcut-lesson';const escape=press('Escape');
console.log(JSON.stringify({html,escape,requests}));
""")
    assert 'aria-keyshortcuts=' not in result["html"]
    assert 'data-character-hint hidden style="display:none"' in result["html"]
    assert result["escape"] == {"result": "Escape", "prevented": True, "hash": "#/review"}
    assert result["requests"] == []


def test_reenabling_preserves_preview_and_input_guards_and_explicit_action():
    result = node(SETUP + """
app.state.characterShortcuts={enabled:false,notice:''};app.toggleCharacterShortcuts(null);
const ignored=[press('a',{isComposing:true}),press('r',{repeat:true}),press('o',{target:{tagName:'INPUT'}})];
app.state.reviewPreviews[family.learning_id].loading=true;ignored.push(press('a'));
app.state.reviewPreviews[family.learning_id].loading=false;
const approved=press('a');await new Promise(resolve=>setImmediate(resolve));
console.log(JSON.stringify({ignored,approved,actions:requests.map(r=>JSON.parse(r.options.body).action)}));
""")
    assert all(not item["result"] and not item["prevented"] for item in result["ignored"])
    assert result["approved"]["result"] == "a"
    assert result["actions"] == ["approve"]
