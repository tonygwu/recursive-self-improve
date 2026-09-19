"""Projects compare canonical repositories with explicit measurement coverage."""
from contextlib import closing
from self_improve.dashboard import queries
from self_improve.store import Store
from tests.test_dashboard_queries import _session, _learning, _proposal


def test_applied_lessons_deduplicate_copies_and_exclude_other_proposal_states(tmp_path):
    with closing(Store(tmp_path / 'state.db')) as store:
        copies = [tmp_path / 'copy-a', tmp_path / 'copy-b']
        for n, path in enumerate(copies):
            _session(store, file_path=str(tmp_path / f'session-{n}'), project_key='github:11',
                     project_path=str(path), project_display='example/renderer')
        _session(store, file_path=str(tmp_path / 'other-session'), project_key='github:12',
                 project_path=str(tmp_path / 'other'), project_display='example/other')
        lesson = _learning(store)
        for path in copies:
            _proposal(store, learning_id=lesson, status='applied', target_path=str(path / 'AGENTS.md'))
        for status in ('pending', 'approved_user', 'rolled_back', 'rejected_user', 'superseded'):
            _proposal(store, learning_id=_learning(store), status=status,
                      target_path=str(copies[0] / 'CLAUDE.md'))
        _proposal(store, learning_id=_learning(store), status='applied',
                  target_path=str(tmp_path / 'other' / 'AGENTS.md'))
        _proposal(store, learning_id=_learning(store), status='applied',
                  target_path=str(tmp_path / 'unattributed' / 'AGENTS.md'))
        store.commit()
        before = list(store.conn.iterdump())
        rows = queries.projects(store, weigh_top_n=0, isdir=lambda _: False)
        by_key = {r['project_key']: r for r in rows['rows']}
        assert by_key['github:11']['rules_applied_here'] == 1
        assert by_key['github:12']['rules_applied_here'] == 1
        assert by_key['github:11']['rules_written_here'] == 0
        assert len(rows['proposals_not_attributed']) == 1
        assert list(store.conn.iterdump()) == before


def test_full_list_selection_keeps_unknown_last_and_searches_off_page_copies(tmp_path):
    """Exercise selection independently of input order and renderer pagination."""
    import json
    import shutil
    import subprocess
    from pathlib import Path
    import pytest
    from tests.spa_assets import copy_spa_dependencies
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for the shipped JavaScript selector')
    static = Path(__file__).resolve().parents[1] / 'src/self_improve/dashboard/static'
    (tmp_path / 'app.mjs').write_bytes((static / 'app.js').read_bytes())
    copy_spa_dependencies(tmp_path)
    script = r'''
import assert from 'node:assert/strict';
import {projectListPage,projectListOptions,parseRoute,renderProjectRows} from './app.mjs';
const rows=Array.from({length:23},(_,i)=>({project_key:`key:${String(i).padStart(2,'0')}`,
 label:`example/renderer-${i}`,sessions:23-i,incidents:0,clones:1,
 clone_paths:[i===22 ? '/temp/off-page/needle-copy' : '/temp/copy-'+i],displays:['alias-'+i],
 key_method:'remote_url',rules_applied_here:0,rules_written_here:0,
 exposure:{computable:i!==0,rate_per_100k:i===1 ? 0 : i},
 context_weight:{computable:i!==0,total_bytes:i===1 ? 0 : i}}));
const before=JSON.stringify(rows);
assert.equal(projectListPage(rows).rows.length,10);
assert.equal(projectListPage(rows,'page=3').rows.length,3);
assert.equal(projectListPage(rows,'page=999').page,3);
assert.equal(projectListPage(rows,'q=NEEDLE-COPY&page=3').rows[0].project_key,'key:22');
assert.equal(projectListPage(rows,'q=alias-22').count,1);
assert.equal(projectListPage(rows,'q=key%3A22').count,1);
assert.equal(projectListPage(rows,'q=remote_url').count,23);
assert.equal(projectListPage(rows,'q=absent').count,0);
for(const sort of ['rate','context'])for(const dir of ['asc','desc']){
 const pages=[1,2,3].flatMap(page=>projectListPage(rows,`sort=${sort}&dir=${dir}&page=${page}`).rows);
 assert.equal(pages.at(-1).project_key,'key:00');
 assert.equal(pages[dir==='asc' ? 0 : 21].project_key,'key:01');
}
const ties=rows.map(row=>({...row,sessions:1}));
assert.deepEqual(projectListPage(ties.reverse()).rows.map(row=>row.project_key),
 projectListPage(ties.reverse()).rows.map(row=>row.project_key));
assert.deepEqual(projectListOptions('sort=__proto__&dir=wrong&page=-2'),{query:'',sort:'sessions',direction:'desc',page:1});
assert.equal(parseRoute('#/projects?q=needle&page=2').projectQuery,'q=needle&page=2');
assert.equal(JSON.stringify(rows),before);
const unsafe={...rows[0],label:'<img src=x onerror=alert(1)>',clone_paths:['/tmp/<script>'],
 displays:['alias "quoted"','<unsafe>']};
const html=renderProjectRows([unsafe],'',new Set([unsafe.project_key]));
assert.ok(html.includes('&lt;script&gt;'));assert.ok(!html.includes('<img src=x'));
assert.ok(html.includes('aria-expanded="true"'));assert.ok(html.includes('colspan="8"'));
assert.ok(!renderProjectRows([unsafe],'').includes('aria-controls='));
console.log(JSON.stringify({selection:'passed',rows:rows.length}));
'''
    (tmp_path / 'check.mjs').write_text(script)
    result = subprocess.run([node, str(tmp_path / 'check.mjs')], cwd=tmp_path,
                            capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == {'selection': 'passed', 'rows': 23}
