"""State distinctions and diff decoration must preserve retained facts and text."""
from html.parser import HTMLParser
from pathlib import Path
import json
import shutil
import subprocess

import pytest

from tests.spa_assets import copy_spa_dependencies


@pytest.fixture
def render(tmp_path):
    node = shutil.which('node')
    assert node
    static = Path(__file__).resolve().parents[1] / 'src/self_improve/dashboard/static'
    (tmp_path / 'app.mjs').write_bytes((static / 'app.js').read_bytes())
    copy_spa_dependencies(tmp_path)

    def run(code):
        (tmp_path / 'probe.mjs').write_text("import assert from 'node:assert/strict';\nimport * as m from './app.mjs';\n" + code)
        result = subprocess.run([node, str(tmp_path / 'probe.mjs')], capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        return result.stdout
    return run


class RetainedText(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.text = ''
        self.lines = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if 'data-diff' in attrs:
            self.lines.append(attrs['data-diff'])

    def handle_data(self, text):
        self.text += text


def test_rule_statuses_separate_gate_decision_and_delivery(render):
    render(r'''
const expected = {
 pending:['Pending','data-state="info"'], gated_pass:['Gate passed','data-verdict="gated_pass"'],
 gated_fail:['Gate failed','data-verdict="gated_fail"'], ungated:['Ungated','data-verdict="ungated"'],
 inconclusive:['Inconclusive','data-verdict="inconclusive"'], held:['Held','data-state="partial"'],
 approved_user:['Approved','data-state="info"'], rejected_user:['Rejected','data-state="abandoned"'],
 applied:['Applied','data-state="ok"'], rolled_back:['Rolled back','data-state="abandoned"'],
 superseded:['Superseded','data-state="abandoned"']};
for(const [status,[label,meaning]] of Object.entries(expected)) {
 const html=m.renderRuleStatus(status);
 assert.ok(html.includes('>'+label+'</span>'),html);
 assert.ok(html.includes(meaning),html);
 assert.ok(html.includes('data-rule-status="'+status+'"'),html);
 assert.ok(html.includes('aria-label='),html);
}
assert.match(m.renderRuleStatus('gated_pass'),/does not authorize delivery/);
assert.match(m.renderRuleStatus('approved_user'),/approval alone does not establish completed delivery/);
assert.doesNotMatch(m.renderRuleStatus('approved_user'),/not yet applied/);
assert.match(m.renderRuleStatus('superseded'),/no longer current/);
for(const s of ['candidate','proposed','applied','rejected','pruned','superseded']) {
 assert.match(m.renderRuleStatus(s,'learning'),new RegExp('>'+s[0].toUpperCase()+s.slice(1)+'</span>'));
}
for(const s of ['__proto__','constructor','toString','future_state','<script>']) {
 const h=m.renderRuleStatus(s);assert.match(h,/data-state="unknown"/);assert.match(h,/Unknown status/);
 assert.doesNotMatch(h,/<script>/);
 assert.match(m.renderRuleStatus(s,'learning'),/data-state="unknown"/);
}
const data={rows:[{kind:'rule',rule:{id:'L',status:'candidate',targets:[],proposal_statuses:{gated_pass:1,approved_user:1,applied:1}}}]};
const html=m.renderRuleBrowserRows(data);
for(const status of ['gated_pass','approved_user','applied'])assert.ok(html.includes(m.renderRuleStatus(status)));
''')


def test_diff_text_and_line_roles_are_exact(render):
    patch = ('\n--- a/AGENTS.md\r\n+++ b/AGENTS.md\r\n@@ -1,3 +1,3 @@\r\n'
             ' context\r\n--- content removed, not a header\r\n+++ content added, not a header\r\n'
             '-<script>bad & worse</script>\r\n+\tUnicode café 🧪  \r\n'
             '\\ No newline at end of file\n'
             '--- a/second\n+++ b/second\n@@ -0,0 +1,2 @@\n+\n+last line')
    cases = [patch, '', '\n', '\r\n', 'plain retained text <tag>', '+ bare addition\n- bare removal\n', '+ long ' + '🧪\t x ' * 300 + '\n\n']
    output = json.loads(render('console.log(JSON.stringify(' + json.dumps(cases) + '.map(m.renderUnifiedDiff)));'))
    for source, html in zip(cases, output, strict=True):
        assert RetainedText(html).text == source
        assert '<script>' not in html
    assert RetainedText(output[0]).lines == ['context','header','header','hunk','context','remove','add','remove','add','note','header','header','hunk','add','add']
    assert RetainedText(output[5]).lines == ['add','remove']
    # Omitted counts mean one line; exhausted hunks let the next file header
    # regain its header role. Empty sides and adjacent hunks must not drift.
    more = ('@@ -1 +1 @@\n--- old\n+++ new\n--- a/next\n+++ b/next\n'
            '@@ -1,0 +2 @@\n+added\n@@ -2 +3,0 @@\n-removed\nraw\rtext')
    html = json.loads(render('console.log(JSON.stringify(m.renderUnifiedDiff(' + json.dumps(more) + ')));'))
    assert RetainedText(html).text == more
    assert RetainedText(html).lines == ['hunk','remove','add','header','header','hunk','add','hunk','remove','context']
    render(r'''
const patch='--- a/file\n+++ b/file\n@@ -1 +1 @@\n-old\n+new <tag>\n';
const decoration=m.renderUnifiedDiff(patch);
assert.ok(m.renderRuleDiffs({targets:[{target_path:'file',diff:patch}]}).includes(decoration));
assert.ok(m.renderReviewDiff(patch).includes(decoration));
assert.match(m.renderRuleDiffs({targets:[{target_path:'file',diff:'',diff_reason:'Not retained'}]}),/Not retained/);
assert.ok(m.renderEvidenceSource({kind:'proposal',source:{diff_unified:patch}}).includes(decoration));
assert.ok(!m.renderEvidenceSource({kind:'revision',source:{content:patch}}).includes('data-diff'));
''')
