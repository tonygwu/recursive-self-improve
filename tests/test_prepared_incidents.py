"""One validated archive supplies complete retained content and safe presentation."""
import json
from copy import deepcopy
import pytest
from self_improve import incident_evidence as evidence
from self_improve.dashboard import evidence_data
from self_improve.redact import redact_text
from tests.test_miner_agentic import SECRET


def row(window,matched='a'*40):
    return {'id':'invented','matched_text':matched,'window_json':json.dumps(window),
            'ts':'2030-01-01T00:00:00Z','signal_type':'repeated_error'}


@pytest.mark.parametrize('window',[
    [],[{'role':'user','text':'Keep the invented result.'}],
    [{'text':'Invented error','session_file':'/invented/session','project_path':'/invented/repo',
      'count_in_session':3,'ts':'2030-01-01T00:00:00Z'}],
])
def test_real_reader_decodes_each_archive_once(monkeypatch,window):
    calls=[];original=evidence.parse_window
    def traced(raw,**kwargs):
        calls.append(raw);return original(raw,**kwargs)
    monkeypatch.setattr(evidence,'parse_window',traced)
    incident=row(window);before=deepcopy(incident)
    result=evidence_data._incident(incident)
    assert result['window']==window and incident==before
    assert len(calls)==1,'Complete incident preparation must not serialize and decode the same archive again'


@pytest.mark.parametrize('window,kind',[
    ([], 'empty'),([{'role':'assistant','text':'Invented output'}],'turn'),
    ([{'session_file':'/invented/a','text':'Invented error','count_in_session':2}],'occurrence'),
    ([{'role':'user','text':'Invented correction'},{'session_file':'/invented/a','count_in_session':2}],'mixed'),
    ([{'unrecognized':{'nested':['Invented metadata',None,False,2]}}],'unknown'),
])
def test_complete_preparation_preserves_shapes_and_public_presentation(window,kind):
    incident=row(window);before=deepcopy(incident)
    prepared=evidence.prepare(incident)
    assert prepared['window']==window and incident==before
    assert prepared['matched_text']=='a'*40
    assert prepared['presentation']['window_kind']==kind
    assert prepared['presentation']['window_len']==len(window)
    assert prepared['presentation']['fingerprint']=='a'*40
    assert evidence.present(incident)==prepared['presentation']
    prepared['window'].append({'text':'caller mutation'})
    assert evidence.prepare(incident)['window']==window


@pytest.mark.parametrize('field',['role','text','ts','session_file','project_path'])
@pytest.mark.parametrize('invalid',[None,True,12,[],{}])
def test_invalid_known_fields_fail_before_any_transformation(field,invalid):
    calls=[]
    with pytest.raises(evidence.IncidentEvidenceError,match=field):
        evidence.prepare(row([{field:invalid}]),transform_text=lambda v:calls.append(v) or v)
    assert calls==[]


@pytest.mark.parametrize('raw',['{}','[1]','[null]','[{"text":"a","text":"b"}]','[{"other":NaN}]','[{"count_in_session":true}]','[{"count_in_session":-1}]','[{"count_in_session":1.5}]'])
def test_strict_json_and_counts_remain_required(raw):
    incident=row([]);incident['window_json']=raw
    with pytest.raises(evidence.IncidentEvidenceError):
        evidence.prepare(incident,transform_text=redact_text)


@pytest.mark.parametrize('matched',[None,False,4,[],{}])
def test_invalid_matched_text_is_not_transformed(matched):
    calls=[]
    with pytest.raises(evidence.IncidentEvidenceError,match='matched_text must be a string'):
        evidence.prepare(row([],matched),transform_text=lambda v:calls.append(v) or v)
    assert calls==[]


def test_redaction_precedes_cutoff_and_preserves_complete_window():
    text='x'*3989+' '+SECRET+' Ω 🐈'
    incident=row([{'text':text,'role':'user','session_file':'/invented/'+SECRET,
        'project_path':'/invented/'+SECRET,'count_in_session':3,'ts':'2030-01-01T00:00:00Z',
        'unknown':{'metadata':[SECRET,{'more':SECRET},42,True,None]}}])
    before=deepcopy(incident)
    prepared=evidence.prepare(incident,transform_text=redact_text,max_chars=4000)
    assert incident==before and SECRET not in json.dumps(prepared)
    assert prepared['window'][0]['text']==redact_text(text)
    shown=prepared['presentation']
    assert shown['display_text']==redact_text(text)[:4000]
    assert SECRET[:10] not in shown['display_text']
    assert shown['display_text_truncated']=={'original_chars':len(redact_text(text)),'cut_chars':len(redact_text(text))-4000}
    assert shown['occurrences']['total_count']==3
    assert shown['occurrences']['first_ts']=='2030-01-01T00:00:00Z'
    assert shown['occurrence_coverage']['unknown_times']==0


def test_unknown_occurrence_coverage_is_not_replaced_with_zero():
    result=evidence.prepare(row([{'session_file':'','text':'é é 🐈','ts':'unknown'},
        {'count_in_session':0,'project_path':'/invented/repo'}]),transform_text=redact_text)
    view=result['presentation']
    assert view['display_text']=='é é 🐈'
    assert view['occurrences']['total_count'] is None
    assert view['occurrences']['sessions'] is None
    assert view['occurrences']['first_ts']==''
    assert view['occurrence_coverage']['unknown_times']==2


def test_transform_cannot_break_validated_string_shape():
    with pytest.raises(evidence.IncidentEvidenceError,match='transform_text must return a string'):
        evidence.prepare(row([]),transform_text=lambda value:None)


def test_fallback_title_and_matched_text_keep_existing_privacy():
    result=evidence_data._incident(row([{'text':'Invented archive'}],matched='Retained '+SECRET))
    assert result['matched_text']==redact_text('Retained '+SECRET)
    assert result['presentation']['display_text']==result['matched_text']
    assert SECRET not in json.dumps(result)
