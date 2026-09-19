"""Every grouped Project view keeps its canonical route and native link semantics."""
from pathlib import Path
import json
import shutil
import subprocess

import pytest
from tests.spa_assets import copy_spa_dependencies


def test_grouped_navigation_keeps_all_views_and_escaped_project_identity(tmp_path):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required to verify the shipped navigation renderer')
    static = Path(__file__).resolve().parents[1] / 'src/self_improve/dashboard/static'
    (tmp_path / 'app.mjs').write_bytes((static / 'app.js').read_bytes())
    copy_spa_dependencies(tmp_path)
    script = r'''
import assert from 'node:assert/strict';
import {renderProjectNavigation,parseRoute} from './app.mjs';
const groups={summary:['overview',1],context:['instructions',3],inventory:['instructions',3],
 topology:['instructions',3],copies:['copies',2],availability:['copies',2],sessions:['sessions',2],
 loads:['sessions',2],rules:['signals',3],exposure:['signals',3],recurrence:['signals',3]};
const key='remote:example.test/team/project?quote="<retained>&';
const reached=new Set();
for (const [view,[group,count]] of Object.entries(groups)) {
 const html=renderProjectNavigation(key,view);
 assert.equal((html.match(/id="project-section-/g)||[]).length,5);
 assert.equal((html.match(/id="project-view-/g)||[]).length,count===1 ? 0 : count);
 assert.equal((html.match(/aria-current="page"/g)||[]).length,1);
 assert.ok(html.includes(`id="project-section-${group}"`));
 assert.ok(!html.includes('<retained>'));assert.ok(!html.includes('<button'));
 for(const [,href] of html.matchAll(/href="([^"]+)"/g)) {
  const route=parseRoute(href);assert.equal(route.view,'projects');assert.equal(route.id,key);
  const tab=new URLSearchParams(href.split('?')[1]).get('tab');assert.ok(tab in groups);reached.add(tab);
 }
 const selected=html.match(/<a[^>]+href="([^"]+)" aria-current="page"/)[1];
 assert.equal(new URLSearchParams(selected.split('?')[1]).get('tab'),view);
}
assert.deepEqual([...reached].sort(),Object.keys(groups).sort());
assert.ok(!renderProjectNavigation(key).includes('project-views'));
console.log(JSON.stringify({groups:5,views:reached.size,routes:'preserved'}));
'''
    (tmp_path / 'check.mjs').write_text(script)
    result = subprocess.run([node, str(tmp_path / 'check.mjs')], cwd=tmp_path,
                            capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == {'groups':5,'views':11,'routes':'preserved'}
