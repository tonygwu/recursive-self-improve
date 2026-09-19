"""Temporary Run outcomes, including real automatic writes and incomplete history."""
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import json
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import uvicorn
from tests import conftest as boundary
from tests.test_rule_availability import cfg as fixture_config
from tests.test_apply import insert_proposal, make_diff
from self_improve import apply
from self_improve.dashboard.app import create_app
from self_improve.execution_policy import set_class_policy
from self_improve.llm import LLMRunner
from self_improve.store import Store


def refuse_private(event, args):
    boundary._audit(event, args)
    if boundary._violations:
        raise AssertionError('Run layout fixture accessed a private resource')


def refuse_models(*args, **kwargs):
    raise AssertionError('Run layout fixture cannot execute a model')


boundary._armed = True
sys.addaudithook(refuse_private)
LLMRunner._execute = refuse_models
out = ROOT / 'reports/dashboard-parity'
out.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(prefix='si-run-layout-') as temp:
    root = Path(temp).resolve()
    cfg = replace(fixture_config.__wrapped__(root),
                  claude_history_path=str(root/'history.jsonl'),
                  codex_skills_dir=str(root/'codex-skills'),
                  production_repo_path=str(root/'production'))
    with closing(Store(cfg.state_path('state.db'))) as store:
        def run(name, stats, status='degraded'):
            store.insert('runs', {'id': name, 'started':'2030-02-07T02:00:00Z',
                'finished':'2030-02-07T02:01:30Z', 'status':status,
                'stats_json':json.dumps({'run_id':name, **stats})})
            store.commit()
        stats = {'review_only':True, 'budget_limits':{'cheap':80,'strong':10,'gate':20},
            'scan':{'files_attempted':12,'files_succeeded':11,'files_failed':1,
                    'dropped_by_cap':{'frustration':7,'standing_instruction':2}},
            'mine':{'attempted':5,'succeeded':4,'failed':1,'taxonomy':{'parse_error':1}},
            'cluster':{'candidates':4,'mode':'agentic_passthrough'},
            'gate':{'attempted':2,'gated_pass':0,'gated_fail':0,'ungated':0,'inconclusive':0,'failed':0,'refused':2},
            'apply':{'attempted':0,'applied':0,'held':4,'failed':0,'operation_ids':[]},
            'llm':{'attempted':0,'calls_made':{'cheap':0,'strong':0,'gate':0},'refused':{'cheap':0,'strong':0,'gate':2},'policy_waits':0},
            'wall_clock':{'wall_seconds':90,'model_seconds':0,'unaccounted_seconds':90,'largest_gap_seconds':40}}
        run('held-run',stats)
        run('unknown-run',{'apply':{'applied':0}})
        run('mismatch-run',{'apply':{'applied':1,'operation_ids':[]}})
        run('absent-run',{})
        long_id='long-run-'+('invented-identity-'*9)
        run(long_id,stats)
        run('delivered-run',{'review_only':False},'ok')
        set_class_policy(store,'global',True,now='2020-01-01T00:00:00Z')
        target=Path(cfg.global_claude_md)
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_text('# Invented human guidance\n')
        operations=[]
        for index in range(21):
            old=target.read_text()
            text=(f'- Complete delivered fixture text {index:02}: '+('inspect this retained contribution '*12)+'\n')
            proposal=insert_proposal(store,target=target,diff=make_diff(old,old+text))
            store.update('proposals','id',proposal['id'],{'run_id':'delivered-run'})
            store.commit()
            proposal=store.query_one('SELECT * FROM proposals WHERE id=?',(proposal['id'],))
            result=apply.apply_proposal(store,cfg,proposal)
            assert result['outcome']=='applied',result
            operations.append(result['operation_id'])
        store.update('runs','id','delivered-run',{'stats_json':json.dumps({**stats,
            'run_id':'delivered-run','review_only':False,
            'apply':{'attempted':21,'applied':21,'held':0,'failed':0,'operation_ids':operations}})})
        store.commit()
        manifest={'db':str(store.db_path),'long_id':long_id,'operations':operations,
            'snapshot':list(store.conn.iterdump()),'targets':{str(target):target.read_text()}}
        (out/'run-layout-manifest.json').write_text(json.dumps(manifest))
        uvicorn.run(create_app(cfg,db_path=store.db_path),host='127.0.0.1',port=8877)
