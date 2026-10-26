"""Navigation routes and real client keyboard boundaries, using invented state."""
import json
from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parents[1]

def node(script):
    module=(ROOT/'src/self_improve/dashboard/static/navigation.js').as_uri()
    app=(ROOT/'src/self_improve/dashboard/static/app.js').as_uri()
    run=subprocess.run(['node','--input-type=module','-e',f'import * as n from {json.dumps(module)}; import * as app from {json.dumps(app)};\n'+script],capture_output=True,text=True)
    assert run.returncode==0,run.stderr
    return json.loads(run.stdout)

def test_navigation_saves_read_routes_and_refuses_executable_destinations():
    result=node('''
      const links=['#/overview/run/fixture%2Fid','#/rules/evidence/incident/i?mode=evidence&query=fixture','#/projects/remote%3Afixture?tab=ownership','#/review/command/c',
      'https://example.test/','#/review/mine/i','#/review/recovery/x','#/review/rollback/p','javascript:alert(1)','#/rules/%zz'];
      console.log(JSON.stringify(links.map(href=>n.safeNavigationLink({href,label:'Fixture'}))));
    ''')
    assert all(result[:4]) and result[4:]==[None]*6

def test_navigation_storage_is_bounded_validated_and_optional():
    result=node('''
      const rows=Array.from({length:15},(_,i)=>({href:'#/rules/r'+i,label:'Fixture '+i}));
      const raw=JSON.stringify({version:1,favorites:[{href:'javascript:alert(1)',label:'Bad'},...rows],recent:[...rows,rows[0]]});
      console.log(JSON.stringify([n.readSavedNavigation({getItem:()=>raw}),n.readSavedNavigation({getItem:()=>'{broken'}),n.readSavedNavigation({getItem(){throw Error('denied');}})]));
    ''')
    assert len(result[0]['favorites'])==len(result[0]['recent'])==10
    assert result[0]['favorites'][0]['href']=='#/rules/r0'
    assert result[1]['notice'] and result[2]['notice']

def test_navigation_evidence_and_exact_record_identity():
    result=node('''
      const evidence=n.evidenceNavigation({rows:[{kind:'session',source_id:'native/a b',title:'<img src=x>',excerpt:'retained'}],pagination:{count:1}});
      let bad=false;try{n.evidenceNavigation({rows:[{kind:'worker',source_id:'i',title:'t'}],pagination:{count:1}});}catch{bad=true;}
      console.log(JSON.stringify({evidence,bad,direct:n.directNavigation('run: same-time-id')}));
    ''')
    assert result['evidence'][0]['href']=='#/rules/evidence/session/native%2Fa%20b?mode=evidence'
    assert result['direct'][0]['href']=='#/overview/run/same-time-id' and result['bad']

def test_review_shortcuts_do_not_handle_modified_composing_or_editable_input():
    result=node('''
      app.state.route='review';app.state.review={families:[{learning_id:'first'},{learning_id:'second'}]};app.state.selectedFamily='first';
      const ignored=[{ctrlKey:true},{metaKey:true},{altKey:true},{isComposing:true},{repeat:true},{target:{isContentEditable:true}}];
      const answers=ignored.map(extra=>app.handleReviewKeydown({key:'j',target:{tagName:'DIV'},...extra}));
      console.log(JSON.stringify({answers,selected:app.state.selectedFamily}));
    ''')
    assert result['selected']=='first' and result['answers']==['']*6

def test_open_modal_consumes_repeated_keys_and_all_supported_unicode_queries():
    result=node('''
      const elements=new Map();const d={getElementById(id){if(!elements.has(id))elements.set(id,{addEventListener(){}});return elements.get(id);}};
      Object.assign(d.getElementById('navigation-dialog'),{open:true,showModal(){}});
      const nav=n.initNavigation({document:d,storage:{getItem:()=>null},navigate(){},search(){},projects:()=>({rows:[]}),current:()=>({href:'#/overview',label:'Overview'}),toggleTheme(){}});
      console.log(JSON.stringify({repeat:nav.handleShortcut({key:'r',repeat:true}),unicode:n.safeNavigationLink({href:'#/rules?mode=evidence&query='+encodeURIComponent('錯'.repeat(500)),label:'Unicode search'})}));
    ''')
    assert result['repeat'] is True
    assert result['unicode'] is not None


def test_evidence_response_rejects_negative_counts_and_inherited_source_kinds():
    result=node('''
      const answers=[];
      for(const [kind,count] of [['learning',-1],['constructor',1],['__proto__',1],['learning',0]]){
        try{n.evidenceNavigation({rows:[{kind,source_id:'i',title:'Fixture'}],pagination:{count}});answers.push('accepted');}catch{answers.push('refused');}
      }
      console.log(JSON.stringify(answers));
    ''')
    assert result==['refused']*4


def test_palette_contains_every_routed_top_level_view():
    result=node('''console.log(JSON.stringify({views:app.VIEWS,pages:n.NAVIGATION_PAGES.filter(x=>!x.href.includes('?')).map(x=>x.href.slice(2))}));''')
    assert set(result['views'])==set(result['pages'])


def test_an_open_palette_keeps_plain_review_keys_from_deciding():
    """BL-49: wire() must ask the palette first. The SPA page model had no
    showModal, so the palette never initialized under test, and the existing
    check sent only repeated keys, which Review ignores anyway. Removing the
    guard in app.js passed every test while plain `a` approved a lesson."""
    result=node(r'''
const listeners = {};
const stub = (id) => ({ id, style: {}, dataset: {}, classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
  addEventListener(type, fn) { (this._l ||= {})[type] = fn; }, setAttribute() {}, getAttribute() { return null; }, removeAttribute() {},
  querySelector() { return null; }, querySelectorAll() { return []; }, focus() {}, scrollIntoView() {}, closest() { return null; }, contains() { return false; },
  innerHTML: '', textContent: '', hidden: false, value: '' });
const elements = new Map();
const d = {
  documentElement: stub('html'), body: stub('body'), activeElement: null, visibilityState: 'visible',
  getElementById(id) { if (!elements.has(id)) elements.set(id, stub(id)); return elements.get(id); },
  querySelector() { return null; }, querySelectorAll() { return []; },
  addEventListener(type, fn) { (listeners[type] ||= []).push(fn); }, removeEventListener() {},
};
const dialog = d.getElementById('navigation-dialog');
Object.assign(dialog, { open: false, showModal() { this.open = true; }, close() { this.open = false; } });
globalThis.document = d;
globalThis.window = { addEventListener() {}, localStorage: { getItem: () => null, setItem() {} } };
globalThis.location = { hash: '#/review' };
const requests = [];
globalThis.fetch = (url, options) => { requests.push({ url, options }); return new Promise(() => {}); };
app.wire();
const family = { learning_id: 'invented-palette-lesson', proposals: [{ id: 'p', revision: 'r', reason_code: 'pending' }], target_rows: [] };
app.state.route = 'review'; app.state.review = { families: [family] }; app.state.selectedFamily = family.learning_id;
app.state.characterShortcuts = { enabled: true, notice: '' };
app.state.reviewPreviews[family.learning_id] = { signature: JSON.stringify([['p', 'r']]),
  data: { ready: true, revision: 'combined', targets: [], members: [{ proposal_id: 'p', revision: 'auth' }] } };
const key = (k, extra = {}) => { const ev = { key: k, target: { tagName: 'BUTTON' }, defaultPrevented: false,
  preventDefault() { this.defaultPrevented = true; }, ...extra }; (listeners.keydown || []).forEach(fn => fn(ev)); return ev; };
const posts = () => requests.filter(r => r.options && r.options.method === 'POST').length;
key('k', { ctrlKey: true });
const opened = dialog.open;
for (const k of ['a', 'r', 'j', 'k', 'o']) key(k);
key('R', { shiftKey: true });
await new Promise(r => setImmediate(r));
const whileOpen = posts();
const selectedWhileOpen = app.state.selectedFamily;
dialog.close();
key('a');
await new Promise(r => setImmediate(r));
console.log(JSON.stringify({ keydownListeners: (listeners.keydown || []).length, opened, whileOpen, selectedWhileOpen, afterClose: posts() }));
''')
    assert result["keydownListeners"] == 1 and result["opened"] is True
    assert result["whileOpen"] == 0, "a Review key decided while the palette was open"
    assert result["selectedWhileOpen"] == "invented-palette-lesson"
    assert result["afterClose"] == 1, "control: the same key must decide once the palette closes"


def test_a_recovery_link_must_name_a_lesson_and_a_mode():
    """BL-46: #/review/recovery/null parsed as JSON null, openRecoveryJob threw
    inside an async function, and the previous job's panel stayed on screen."""
    result=node(r'''
      const answers=[];
      for(const text of ['null','123','[]','{"mode":"hook"}','{"learning_id":"L1","mode":""}','{"learning_id":"L1","mode":"bogus"}',
                         '{"learning_id":"L1","mode":"hook"}','{"learning_id":"L2","mode":"correct_target"}','{"learning_id":"L3","mode":"regenerate_patch"}']){
        try{answers.push(app.recoverySelectionFromLink(text).learning_id);}catch(error){answers.push('refused');}
      }
      console.log(JSON.stringify(answers));
    ''')
    assert result==['refused']*6+['L1','L2','L3']


def test_the_spa_recovery_modes_are_the_server_modes():
    """BL-46 review: a bogus mode opened an empty destination list with no
    error. The SPA checks modes against its own list, so it must equal the
    set the server accepts."""
    from self_improve.recovery_jobs import MODES
    result=node('console.log(JSON.stringify(app.RECOVERY_MODES));')
    assert sorted(result)==sorted(MODES) and len(result)==len(MODES)
