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
