"""Retained identity metadata; canonical keys never inherit a different copy's label."""
from ..mining_history import digest
from ..scan_observations import logical_session_key

PRODUCTS={'claude':'Claude Code','codex':'Codex'}


def source_product(value):
    return {'value':value if value in PRODUCTS else None,'recorded':value,
            'label':PRODUCTS.get(value,'Agent unknown'),
            'reason':'' if value in PRODUCTS else 'No supported source product is recorded.'}


def metadata_index(store, *, sessions=None):
    rows=sessions if sessions is not None else store.query(
        'SELECT file_path,source,session_id,project_key,project_display FROM sessions ORDER BY file_path')
    projects={}
    for row in rows:
        if row['project_key'] and row['project_display']:
            projects.setdefault(row['project_key'],set()).add(row['project_display'])
    return {'transcripts':{row['file_path']:row for row in rows},
            'projects':{key:sorted(labels) for key,labels in projects.items()},'session_keys':{}}


def project_ref(index, project_key):
    labels=index['projects'].get(project_key,[]) if project_key else []
    return {'key':project_key,'label':labels[0] if labels else project_key or 'Project unknown',
            'aliases':list(labels),'label_status':'known' if labels else 'unavailable',
            'reason':'' if labels else 'No repository display name is retained for this canonical key.' if project_key
                else 'No canonical project identity is retained.'}


def session_key(provider, native_id, path, *, cache=None):
    # Unknown providers cannot turn equal native IDs into a proven shared session.
    known=provider in PRODUCTS and bool(native_id)
    key=(provider,native_id,'') if known else (provider,'',path)
    if cache is not None and key in cache:return cache[key]
    value=logical_session_key(provider,native_id) if known else digest(['transcript',provider,path])
    if cache is not None:cache[key]=value
    return value


def session_ref(index, incident):
    path=incident.get('session_file','')
    transcript=index['transcripts'].get(path)
    product=source_product(transcript['source'] if transcript else '')
    native_id=incident.get('session_id','')
    known=bool(product['value'] and native_id)
    return {'key':session_key(product['recorded'],native_id,path,cache=index['session_keys']),
            'provider':product['value'] or '', 'provider_label':product['label'],
            'recorded_provider':product['recorded'],'native_session_id':native_id,
            'identity_kind':'native_session' if known else 'transcript','session_file':path,
            'reason':'' if known else 'Native session identity is incomplete; this is a separate retained transcript reference.',
            'basis':'Source product comes from retained transcript-index metadata. This is not model telemetry or a runtime loading receipt.'}


def identity(index, incident):
    return {'project':project_ref(index,incident.get('project_key','')),
            'session':session_ref(index,incident)}


def session_summary(index, incidents):
    sessions={}
    unknown_sources=0
    for row in incidents:
        ref=session_ref(index,row)
        sessions.setdefault(ref['key'],ref)
        unknown_sources += not ref['provider']
    refs=sorted(sessions.values(),key=lambda r:(r['provider'],r['native_session_id'],r['key']))
    return {'sessions':refs,'known_session_count':sum(r['identity_kind']=='native_session' for r in refs),
            'unknown_session_records':sum(r['identity_kind']=='transcript' for r in refs),
            'unknown_source_incidents':unknown_sources}


def search_values(value):
    """Search identity values without treating explanatory copy as evidence."""
    projects=value.get('projects', [value['project']] if value.get('project') else [])
    session=value.get('session', {})
    values=[v for p in projects for v in [p['key'],*p['aliases']]]
    values.extend(session.get(k,'') for k in ('provider','recorded_provider','native_session_id','session_file'))
    values.append(PRODUCTS.get(session.get('provider'),''))
    return sorted(set(values)-{''})
