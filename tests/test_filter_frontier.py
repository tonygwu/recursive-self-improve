"""Invented frontier/Ultra telemetry and independent reference-run agreement."""
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from quota_router import Selection

from self_improve.config import Config
from self_improve.data_boundary import DataBoundaryError
from self_improve.evals import filter_frontier as frontier
from self_improve.evals import filter_models as models
from tests.test_filter_eval import label, dataset


def receipt(*, model='gpt-fixture-astra', effort='ultra', answer=None):
    stream = [dict(type='thread.started', thread_id='00000000-0000-0000-0000-000000000001'),
              dict(type='item.completed', item=dict(type='agent_message', text=json.dumps(answer or label('s:1')))),
              dict(type='turn.completed', usage=dict(input_tokens=100, cached_input_tokens=0, output_tokens=40))]
    rollout = [dict(type='session_meta', payload=dict(id=stream[0]['thread_id'], source='exec',base_instructions={'text':'rubric'})),
               dict(type='turn_context', payload=dict(model=model, effort=effort))]
    return dict(returncode=0, stdout='\n'.join(map(json.dumps, stream)), stderr='', rollout=rollout)


def test_exact_model_and_ultra_are_required_from_run_evidence():
    assert frontier.parse_cli(receipt(), 'gpt-fixture-astra')['status'] == 'ok'
    assert frontier.parse_cli(receipt(model='gpt-fixture-sol'), 'gpt-fixture-astra')['status'] == 'model_mismatch'
    assert frontier.parse_cli(receipt(effort='max'), 'gpt-fixture-astra')['status'] == 'effort_mismatch'
    raw = receipt(); raw['rollout'] = []
    assert frontier.parse_cli(raw, 'gpt-fixture-astra')['status'] != 'ok'


def test_tools_missing_usage_and_multiple_turns_are_not_valid_labels():
    for extra in [dict(type='turn.completed', usage={}),
                  dict(type='item.completed', item=dict(type='command_execution', command='echo nope'))]:
        raw = receipt(); raw['stdout'] += '\n' + json.dumps(extra)
        assert frontier.parse_cli(raw, 'gpt-fixture-astra')['status'] != 'ok'


def test_three_runs_report_unanimity_pairwise_and_missing_separately():
    rows = [{a: label(a, positive=p) for a,p in zip(['a','b','c'], ps)}
            for ps in [(True,False,True), (True,False,False), (True,True,True)]]
    rows[0]['d'] = label('d')
    result = frontier.agreement(rows, ['a','b','c','d'])
    assert result['complete_cases'] == 3
    assert result['unanimous']['count'] == 1
    assert result['pairwise']['count'] == 5
    assert result['pairwise']['denominator'] == 9
    assert result['missing_ids'] == ['d']
    assert result['disagreement_ids'] == ['b','c']
    rows[2]['c']['label'] = 'insufficient_context'
    assert frontier.agreement(rows, ['a','b','c','d'])['no_majority_ids'] == ['c']


def test_frontier_uses_native_ultra_and_selected_subscription(tmp_path, monkeypatch):
    selected = Selection.from_payload({'contract_version':1,'capability':{'requested':'frontier'},
        'decision':{'account':'selected','provider':'codex','fits':True,'meets_policy':True,
                    'model':{'id':'gpt-fixture-astra','provider':'codex','listed':True,'source':'fixture'}},
        'exec':{'env':{'CODEX_HOME':'/selected'},'argv_prefix':['wrapper'],'unset_env':['DROP_ME']}})
    monkeypatch.setattr(frontier, 'eligible_accounts', lambda env,cfg: {'selected':'gpt-fixture-astra'})
    monkeypatch.setenv('OPENAI_API_KEY','never-send')
    monkeypatch.setenv('OPENAI_BASE_URL','https://unwanted.invalid')
    monkeypatch.setenv('DROP_ME','discard')
    seen=[]
    def selector(**kw):
        seen.append(kw); return selected
    def execute(argv, *, prompt,cwd,env,timeout):
        assert argv[:2] == ['wrapper','/fixture/codex']
        assert env['CODEX_HOME']=='/selected'
        assert 'OPENAI_API_KEY' not in env and 'OPENAI_BASE_URL' not in env and 'DROP_ME' not in env
        assert 'model_reasoning_effort="ultra"' in argv
        assert 'forced_login_method="chatgpt"' in argv
        assert '--ignore-user-config' in argv and '--ignore-rules' in argv
        assert 'read-only' in argv and 'web_search="disabled"' in argv
        from pathlib import Path
        assert (Path(cwd)/'rubric.txt').read_text()=='rubric'
        assert any(a.startswith('model_instructions_file=') for a in argv)
        return 0, receipt()['stdout'], ''
    monkeypatch.setattr(frontier, 'read_rollout', lambda stdout,env: receipt()['rollout'])
    result,cost=frontier.invoke({'model':'gpt-fixture-astra','reasoning_effort':'ultra','system':'rubric',
                                'messages':[{'content':'evidence'}]},tmp_path,'key',
                               replace(Config(),codex_path='/fixture/codex'), selector=selector,executor=execute)
    assert result['status']=='ok' and result['served_effort']=='ultra'
    assert seen[0]['capability']=='frontier' and 'effort' not in seen[0]
    assert seen[0]['record'] is True
    assert cost == .0015
    assert 'not vendor pricing' in result['billing_basis']
    assert 'never-send' not in (tmp_path/'key.pick.json').read_text()


def test_native_ultra_advertisement_required_even_when_router_max_is_supported(monkeypatch):
    monkeypatch.setattr(frontier, 'load_router_config', lambda **kw: SimpleNamespace(enabled_accounts=lambda:[
        SimpleNamespace(id='supports',provider='codex'), SimpleNamespace(id='clamped',provider='codex'),
        SimpleNamespace(id='claude',provider='claude')]))
    monkeypatch.setattr(frontier, 'read_table', lambda path: ({},None))
    monkeypatch.setattr(frontier, 'lookup', lambda table,a,*args,**kw: SimpleNamespace(id='gpt-fixture-astra',listed=True))
    monkeypatch.setattr(frontier, 'fresh_listing', lambda table,a,**kw: SimpleNamespace(
        efforts=(('gpt-fixture-astra',('max','ultra') if a=='supports' else ('max',)),)))
    assert frontier.eligible_accounts({},Config()) == {'supports':'gpt-fixture-astra'}
    assert frontier.eligible_accounts({},replace(Config(),allowed_providers=('claude',))) == {}


def test_profile_cache_is_separate_and_three_runs_really_make_three_requests(tmp_path,monkeypatch):
    settings=dict(profile=frontier.PROFILE,model='gpt-fixture-astra',effort='ultra',transport=frontier.TRANSPORT)
    monkeypatch.setattr(frontier,'profile',lambda *args:settings)
    calls=[]
    def transport(payload,*args,**kwargs):
        calls.append(payload)
        return dict(status='ok',label=label('s:1'),served_model=payload['model'],served_effort='ultra',retryable=False),.01
    (tmp_path/'reference-labels.json').write_text('legacy labels')
    for order in (0,1,2,0):
        result=models.run(dataset(),{'sha256':'pin'},tmp_path,provider='reference',limit=1,order_seed=order,
                          reference_profile='frontier-ultra',reference_transport=transport,cfg=Config())
        assert result['result_file']==f'reference-frontier-ultra-order-{order}.json'
    assert len(calls)==3
    assert (tmp_path/'reference-labels.json').read_text()=='legacy labels'
    assert len(json.loads((tmp_path/'ledger.json').read_text())['calls'])==3


def test_agreement_all_missing_is_not_zero_accuracy():
    result=frontier.agreement([{}, {}, {}],['a'])
    assert result['unanimous']['rate'] is None and result['pairwise']['rate'] is None
    assert result['per_run_missing']==[1,1,1]
    with pytest.raises(DataBoundaryError,match='three independent'):
        frontier.agreement([{}],['a'])


def test_rubric_mismatch_is_rejected_even_with_correct_model_and_effort():
    assert frontier.parse_cli(receipt(),'gpt-fixture-astra','rubric')['status']=='ok'
    assert frontier.parse_cli(receipt(),'gpt-fixture-astra','different rubric')['status']=='instruction_mismatch'


def test_frontier_raw_verification_rejects_changed_cache_and_wrong_router(tmp_path):
    from self_improve.evals.filter_dataset import case_context,digest
    data=dataset(); case=data['cases'][0]
    settings=dict(profile=frontier.PROFILE,model='gpt-fixture-astra',effort='ultra',transport=frontier.TRANSPORT)
    req=frontier.request(case_context(data,case),settings)
    raw=receipt();raw['rollout'][0]['payload']['base_instructions']['text']=req['system']
    raw['rollout'].append(dict(type='response_item',payload=dict(role='user',content=[dict(text=req['messages'][0]['content'])])))
    pick={'decision':{'fits':True,'meets_policy':True,'model':{'id':settings['model']}},
          'capability':{'requested':'frontier'}}
    record={**frontier.parse_cli(raw,settings['model']), 'input_sha':digest(req),
            'raw_response':'key.response.json','response_sha':digest(raw),
            'router_pick':'key.pick.json','router_pick_sha':digest(pick)}
    path=tmp_path/'result.json'
    def save():
        models.atomic_json(tmp_path/'key.response.json',raw);models.atomic_json(tmp_path/'key.pick.json',pick)
        models.atomic_json(path,dict(dataset_sha='pin',provider='reference',reference_profile=frontier.PROFILE,
                                    reference_settings=settings,records={case['id']:record}))
    save()
    assert frontier.verified_labels(path,data,'pin')[case['id']]['label']=='worth_mining'
    record['label']={**record['label'],'label':'not_worth_mining'};save()
    with pytest.raises(DataBoundaryError,match='cache differs'):
        frontier.verified_labels(path,data,'pin')
    record['label']=frontier.parse_cli(raw,settings['model'])['label']
    pick['decision']['meets_policy']=False; record['router_pick_sha']=digest(pick);save()
    with pytest.raises(DataBoundaryError,match='router evidence'):
        frontier.verified_labels(path,data,'pin')


def test_independence_rejects_reusing_one_thread_as_three_runs():
    rows=[{'a':{**label('a'),'reference_thread_id':'same'}} for _ in range(3)]
    with pytest.raises(DataBoundaryError,match='reuse'):
        frontier.agreement(rows,['a'])
    rows[1]['a']['reference_thread_id']='second';rows[2]['a']['reference_thread_id']='third'
    result=frontier.agreement(rows,['a'])
    assert result['unanimous']['rate']==1
    assert result['fleiss_kappa'] is None  # no class variation; chance agreement is already 1


def test_new_cli_default_is_frontier_and_agreement_requires_three_distinct_runs(tmp_path,monkeypatch):
    from self_improve.cli import main
    from self_improve import config
    from self_improve.evals import filter_cli
    from tests.test_filter_eval import frozen
    root,data,manifest=frozen(tmp_path)
    monkeypatch.setattr(config,'load_config',lambda path:Config())
    def scripted(*args,**kw):
        assert kw['reference_profile']=='frontier-ultra'
        return dict(records={},charged_or_reserved_usd=0)
    monkeypatch.setattr(filter_cli,'run',scripted)
    out=tmp_path/'calls';out.mkdir()
    assert main(['eval-filter','reference','--dataset',str(root),'--output',str(out)])==0
    assert (out/'reference-frontier-ultra-labels-order-0.json').exists()
    assert not (out/'reference-labels.json').exists()
    raw=tmp_path/'same.json';models.atomic_json(raw,dict(dataset_sha=manifest['sha256'],order_seed=0))
    assert main(['eval-filter','agreement','--dataset',str(root),'--output',str(tmp_path/'agreement.json'),
                 '--reference-results',str(raw),str(raw),str(raw)])==2
    assert not (tmp_path/'agreement.json').exists()


def test_reviewed_invalid_case_continues_without_retry_relabel_or_cache_reset(tmp_path):
    calls=[]
    def transport(payload,*args,**kw):
        anchor=json.loads(payload['messages'][0]['content'])['anchor']
        calls.append(anchor)
        answer=label(anchor);answer['signals']=[]
        return dict(status='ok',label=answer,retryable=False,served_model=models.REFERENCE),.01
    first=models.run(dataset(),{'sha256':'pin'},tmp_path,provider='reference',limit=3,
                     reference_transport=transport,cfg=Config())
    stopped=next(iter(first['records']))
    assert len(calls)==1 and first['records'][stopped]['status']=='invalid_label'
    second=models.run(dataset(),{'sha256':'pin'},tmp_path,provider='reference',limit=3,
                      reference_transport=transport,cfg=Config(),continue_after_invalid_label=[stopped])
    assert len(calls)==2 and calls.count(stopped)==1  # next unreviewed invalid case still stops
    assert second['records'][stopped]['status']=='invalid_label'
    assert second['continued_invalid_label_ids']==[stopped]
    assert len(json.loads((tmp_path/'ledger.json').read_text())['calls'])==2


def test_signal_incomplete_vote_only_enters_category_agreement(tmp_path):
    from self_improve.evals.filter_dataset import case_context,digest
    data=dataset();case=data['cases'][0]
    settings=dict(profile=frontier.PROFILE,model='gpt-fixture-astra',effort='ultra',transport=frontier.TRANSPORT)
    req=frontier.request(case_context(data,case),settings)
    answer=label(case['id']);answer['signals']=[]
    raw=receipt(answer=answer);raw['rollout'][0]['payload']['base_instructions']['text']=req['system']
    raw['rollout'].append(dict(type='response_item',payload=dict(role='user',content=[dict(text=req['messages'][0]['content'])])))
    pick={'decision':{'fits':True,'meets_policy':True,'model':{'id':settings['model']}},'capability':{'requested':'frontier'}}
    record={**frontier.parse_cli(raw,settings['model']), 'status':'invalid_label','input_sha':digest(req),
            'raw_response':'key.response.json','response_sha':digest(raw),'router_pick':'key.pick.json','router_pick_sha':digest(pick)}
    models.atomic_json(tmp_path/'key.response.json',raw);models.atomic_json(tmp_path/'key.pick.json',pick)
    path=tmp_path/'result.json';models.atomic_json(path,dict(dataset_sha='pin',provider='reference',reference_profile=frontier.PROFILE,
                                                          reference_settings=settings,records={case['id']:record}))
    assert frontier.verified_labels(path,data,'pin')=={}
    decisions=frontier.verified_labels(path,data,'pin',category_only=True)
    assert decisions[case['id']]['label']=='worth_mining' and decisions[case['id']]['full_schema_valid'] is False
    runs=[{a:{**r,'reference_thread_id':str(i)} for a,r in decisions.items()} for i in range(3)]
    result=frontier.agreement(runs,[case['id']])
    assert result['unanimous']['count']==1 and result['signals_unanimous']['denominator']==0
