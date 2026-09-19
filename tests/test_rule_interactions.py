"""Rules navigation uses native controls and recorded, distinguishable paths."""
from html.parser import HTMLParser
from tests.test_navigation import node

class Elements(HTMLParser):
    def __init__(self, html):
        super().__init__(); self.tags=[]; self.feed(html)
    def handle_starttag(self, tag, attrs):
        self.tags.append((tag,dict(attrs)))

def rows():
    return node('''
      app.state.ruleQuery='query=fixture&target=project&family=f&cursor=p';app.state.selectedRule='a';
      const rule=(id,path)=>({id,title:'Rule <'+id+'>',rule_text:'Invented text',status:'proposed',targets:[{kind:'project_agents_md',path}],agent_products:[]});
      const rows=[rule('a','/fixture/alpha/same/AGENTS.md'),rule('b','/fixture/beta/same/AGENTS.md'),rule('c','/fixture/other/<script>/AGENTS.md')];
      console.log(JSON.stringify({html:app.renderRuleBrowserRows({rows:rows.map(rule=>({kind:'rule',rule}))})}));
    ''')['html']

def test_rule_table_keeps_native_cells_and_exact_read_links():
    tags=Elements(rows()).tags
    assert len([t for t,a in tags if t=='tr'])==3
    assert len([t for t,a in tags if t=='td'])==12
    assert all('role' not in a and 'tabindex' not in a for t,a in tags if t=='tr')
    links=[a for t,a in tags if t=='a' and 'data-rule-id' in a]
    assert len(links)==3
    assert links[0]['aria-current']=='true'
    assert 'aria-current' not in links[1]
    assert all('query=fixture' in a['href'] and 'family=f' in a['href'] and 'cursor=p' in a['href'] for a in links)

def test_target_labels_keep_distinguishing_suffix_and_complete_escaped_paths():
    html=rows()
    labels=html.replace("<span>", "").replace("</span>", "")
    assert '…/alpha/same/AGENTS.md' in labels and '…/beta/same/AGENTS.md' in labels
    assert '<code>/fixture/alpha/same/AGENTS.md</code>' in html
    assert '<script>' not in html and '&lt;script&gt;' in html
    assert html.count('<details class="rules-target"')==3

def test_inspector_sections_are_read_links_with_one_current_location():
    result=node('''console.log(JSON.stringify(app.renderTabs([{id:'why',label:'Why'},{id:'evidence',label:'Evidence'}],'why',id=>'#/rules/fixture?tab='+id)));''')
    tags=Elements(result).tags
    assert ('nav',{'class':'tabs','aria-label':'Inspector sections'}) in tags
    links=[a for t,a in tags if t=='a']
    assert len(links)==2 and links[0]['aria-current']=='page'
    assert links[1]['href']=='#/rules/fixture?tab=evidence' and 'aria-current' not in links[1]
    assert 'aria-selected' not in result
