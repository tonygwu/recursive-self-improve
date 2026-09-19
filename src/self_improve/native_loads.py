"""Local native loading reports; no loaded-byte, event-time or continuity inference."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import re
import shlex
import uuid

from . import project_identity
from . import scan_observations as so
from .rule_revisions import identity_cache
from .store import utc_now_iso

MIGRATION = '0031_native_load_reports'
PROFILE = 'claude-native-load-reports/1'
CODEX_PROFILE = 'codex-native-lifecycle-reports/1'
PROFILES = {'claude': PROFILE, 'codex': CODEX_PROFILE}
CODEX_EVENTS = ('SessionStart', 'PreCompact', 'PostCompact', 'SessionEnd', 'SubagentStart', 'SubagentStop', 'Interrupt')
CODEX_COMMON = ('session_id', 'transcript_path', 'cwd', 'hook_event_name', 'model', 'permission_mode')
CODEX_FIELDS = {
    'SessionStart': ('source',), 'SessionEnd': ('reason',),
    'PreCompact': ('turn_id', 'trigger'), 'PostCompact': ('turn_id', 'trigger'),
    'SubagentStart': ('turn_id', 'agent_id', 'agent_type'),
    'SubagentStop': ('turn_id', 'agent_id', 'agent_type', 'agent_transcript_path', 'stop_hook_active'),
    'Interrupt': ('turn_id',),
}
MAX_INPUT_BYTES = 65536
EVENTS = ('InstructionsLoaded', 'SessionStart', 'PostCompact', 'CwdChanged', 'SessionEnd')
COMMON = ('session_id', 'transcript_path', 'cwd', 'hook_event_name', 'agent_id')
EVENT_FIELDS = {
    'InstructionsLoaded': ('file_path', 'memory_type', 'load_reason', 'globs', 'trigger_file_path', 'parent_file_path'),
    'SessionStart': ('source',), 'PostCompact': ('trigger',),
    'CwdChanged': ('old_cwd', 'new_cwd'), 'SessionEnd': ('reason',),
}
PATH_FIELDS = {'cwd', 'transcript_path', 'file_path', 'trigger_file_path', 'parent_file_path', 'old_cwd', 'new_cwd', 'agent_transcript_path'}
COLUMNS = ('id', 'profile', 'received_at', 'project_key', 'logical_session_key', 'working_copy_id', 'event_name')
FIELDS = set(COLUMNS) | {'payload', 'payload_hash', 'working_copy_path', 'identity_method',
    'source', 'agent_id', 'occurred_at', 'loaded_content_hash', 'rule_revision_id', 'source_authentication'}


class NativeLoadError(ValueError):
    """Input or retained evidence failed the native report contract."""


class NativeLoadRequestError(NativeLoadError):
    """Invalid reader selection."""


def _text(value, field, *, maximum=4096):
    if (not isinstance(value, str) or not value or len(value)>maximum
            or any(ord(c)<32 for c in value)):
        raise NativeLoadError('Invalid native field: '+field)
    return value


def normalize_event(payload, *, provider='claude'):
    if not isinstance(payload, dict):
        raise NativeLoadError('Native input must be a JSON object')
    try:
        if len(so.canonical_json(payload).encode('utf-8'))>MAX_INPUT_BYTES:
            raise NativeLoadError('Native input exceeds 65536 bytes')
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, NativeLoadError): raise
        raise NativeLoadError('Native input must be bounded strict JSON') from exc
    if provider == 'codex':
        return _normalize_codex_event(payload)
    if provider != 'claude':
        raise NativeLoadError('Unsupported native provider')
    kind=payload.get('hook_event_name')
    if not isinstance(kind,str) or kind not in EVENTS:
        raise NativeLoadError('Unsupported native event')
    keep={k:payload[k] for k in (*COMMON,*EVENT_FIELDS[kind]) if k in payload}
    for name in ('session_id','transcript_path','cwd','hook_event_name'):
        _text(keep.get(name),name)
    for name,value in keep.items():
        if name=='globs':
            if not isinstance(value,list) or len(value)>100:
                raise NativeLoadError('Invalid native globs')
            for item in value: _text(item,'glob',maximum=1000)
        else: _text(value,name)
        if name in PATH_FIELDS:
            if not os.path.isabs(value): raise NativeLoadError('Native path must be absolute: '+name)
            # Preserve reported spelling: collapsing '..' before resolving a
            # symlink can change which repository the native cwd identifies.
    for name in ('session_id','agent_id'):
        if name in keep and not re.fullmatch(r'[A-Za-z0-9_.:-]{1,200}',keep[name]):
            raise NativeLoadError('Invalid native identity: '+name)
    if kind=='InstructionsLoaded':
        if not keep.get('file_path') or keep.get('memory_type') not in {'User','Project','Local','Managed'}:
            raise NativeLoadError('Invalid native load path or scope')
        if keep.get('load_reason') not in {'session_start','nested_traversal','path_glob_match','include','compact'}:
            raise NativeLoadError('Invalid native load reason')
    elif kind=='SessionStart' and keep.get('source') not in {'startup','resume','clear','compact','fork'}:
        raise NativeLoadError('Invalid native session source')
    elif kind=='PostCompact' and keep.get('trigger') not in {'auto','manual'}:
        raise NativeLoadError('Invalid native compaction trigger')
    elif kind=='CwdChanged' and not all(keep.get(k) for k in ('old_cwd','new_cwd')):
        raise NativeLoadError('Native cwd change requires both paths')
    elif kind=='SessionEnd':
        _text(keep.get('reason'),'reason',maximum=100)
    if len(so.canonical_json(keep).encode())>MAX_INPUT_BYTES:
        raise NativeLoadError('Native input exceeds 65536 bytes')
    return keep


def _normalize_codex_event(payload):
    kind = payload.get('hook_event_name')
    if not isinstance(kind, str) or kind not in CODEX_EVENTS:
        raise NativeLoadError('Unsupported Codex lifecycle event')
    keep = {k: payload[k] for k in (*CODEX_COMMON, *CODEX_FIELDS[kind]) if k in payload}
    required = {'session_id', 'cwd', 'hook_event_name'} | set(CODEX_FIELDS[kind]) - {'agent_transcript_path'}
    if not required <= keep.keys():
        raise NativeLoadError('Missing Codex lifecycle identity or boundary field')
    for name, value in keep.items():
        if name == 'stop_hook_active':
            if type(value) is not bool:
                raise NativeLoadError('Invalid native field: stop_hook_active')
            continue
        if name in {'transcript_path', 'agent_transcript_path'} and value is None:
            continue
        _text(value, name)
        if name in PATH_FIELDS and not os.path.isabs(value):
            raise NativeLoadError('Native path must be absolute: ' + name)
        if name in {'session_id', 'agent_id', 'turn_id'} and not re.fullmatch(r'[A-Za-z0-9_.:-]{1,200}', value):
            raise NativeLoadError('Invalid native identity: ' + name)
    if kind == 'SessionStart' and keep['source'] not in {'startup', 'resume', 'clear', 'compact'}:
        raise NativeLoadError('Invalid Codex session source')
    if kind in {'PreCompact', 'PostCompact'} and keep['trigger'] not in {'auto', 'manual'}:
        raise NativeLoadError('Invalid Codex compaction trigger')
    if kind == 'SessionEnd' and keep['reason'] != 'other':
        raise NativeLoadError('Invalid Codex session end reason')
    if 'permission_mode' in keep and keep['permission_mode'] not in {'default', 'acceptEdits', 'plan', 'dontAsk', 'bypassPermissions'}:
        raise NativeLoadError('Invalid Codex permission mode')
    return keep


def parse_input(raw, *, provider='claude'):
    if len(raw)>MAX_INPUT_BYTES: raise NativeLoadError('Native input exceeds 65536 bytes')
    def pairs(values):
        result={}
        for k,v in values:
            if k in result: raise NativeLoadError('Native input has duplicate JSON keys')
            result[k]=v
        return result
    def invalid(_): raise NativeLoadError('Native input must be strict JSON')
    try:
        return normalize_event(json.loads(raw,object_pairs_hook=pairs,parse_constant=invalid), provider=provider)
    except (ValueError,TypeError,UnicodeError,RecursionError) as exc:
        if isinstance(exc,NativeLoadError): raise
        raise NativeLoadError('Native input must be strict JSON') from exc


def _schema(store, *, missing_ok=False):
    present=store.query_one('SELECT name FROM schema_migrations WHERE name=?',(MIGRATION,))
    if not present:
        if missing_ok: return False
        raise NativeLoadError('Native reports require explicit migration '+MIGRATION)
    if not store.query_one("SELECT name FROM sqlite_master WHERE type='table' AND name='native_load_reports'"):
        raise NativeLoadError('Native report schema is damaged')
    return True


def _uuid(value):
    try:
        if not isinstance(value,str) or str(uuid.UUID(value))!=value: raise ValueError()
        return value
    except (ValueError,TypeError,AttributeError) as exc:
        raise NativeLoadError('Receipt ID must be a canonical UUID') from exc


def _read(row):
    owner='native receipt '+str(row['id'])
    try:
        record=json.loads(row['record_json'])
        if not isinstance(record,dict) or set(record)!=FIELDS: raise ValueError()
        _uuid(record['id'])
        if record['profile'] != PROFILES.get(record['source']) or any(record[k]!=row[k] for k in COLUMNS): raise ValueError()
        if so.content_id(record)!=row['record_hash']: raise ValueError()
        if record['payload']!=normalize_event(record['payload'], provider=record['source']): raise ValueError()
        if record['payload_hash']!=so.content_id(record['payload']): raise ValueError()
        if record['event_name']!=record['payload']['hook_event_name']: raise ValueError()
        if record['logical_session_key']!=so.logical_session_key(record['source'],record['payload']['session_id']): raise ValueError()
        if record['agent_id']!=record['payload'].get('agent_id',''): raise ValueError()
        if not record['received_at'] or so.normalize_timestamp(record['received_at'])!=record['received_at']: raise ValueError()
        if record['source_authentication']!='local_unattested': raise ValueError()
        _text(record['project_key'],'project identity')
        _text(record['working_copy_path'],'working copy')
        if not os.path.isabs(record['working_copy_path']): raise ValueError()
        if not isinstance(record['identity_method'],str) or record['identity_method'] not in project_identity.METHODS: raise ValueError()
        if any(record[k] is not None for k in ('occurred_at','loaded_content_hash','rule_revision_id')): raise ValueError()
        if record['working_copy_id']!=so.content_id([record['project_key'],record['working_copy_path']]): raise ValueError()
        return record
    except (ValueError,TypeError,KeyError) as exc:
        raise NativeLoadError(owner+': invalid retained content or indexed binding') from exc


def record_native_event(store,cfg,payload,*,receipt_id=None,received_at=None,provider='claude'):
    """Record one local hook report atomically; no body reads or provider calls."""
    if store.read_only or store.conn.in_transaction:
        raise NativeLoadError('Native receiver requires an idle writable Store')
    _schema(store)
    received=so.normalize_timestamp(received_at if received_at is not None else utc_now_iso())
    if received is None: raise NativeLoadError('Invalid receiver timestamp')
    rid=_uuid(receipt_id) if receipt_id is not None else str(uuid.uuid4())
    clean=normalize_event(payload, provider=provider)
    for k,v in clean.items():
        if k in PATH_FIELDS and v is not None and any(part in v for part in cfg.denylist_substrings):
            raise NativeLoadError('Native event excluded by configured path policy')
    # Check resolved cwd before any Git subprocess. Loaded/transcript paths
    # remain lexical reports: never open them to guess previously loaded bytes.
    resolved=os.path.realpath(clean['cwd'])
    if any(part in resolved for part in cfg.denylist_substrings):
        raise NativeLoadError('Native event excluded by configured path policy')
    # Replay binds the native payload, not today's cwd identity or receive time.
    prior=store.query_one('SELECT * FROM native_load_reports WHERE id=?',(rid,))
    if prior:
        record=_read(prior)
        if record['source']!=provider or record['payload']!=clean: raise NativeLoadError('Native receipt replay differs')
        return record
    identity=project_identity.resolve(resolved,use_gh=False)
    if identity.method==project_identity.METHOD_REMOTE_URL:
        identity=identity_cache(store).get(identity.key.removeprefix('remote:'),identity)
    copy=so.working_copy_identity(identity.key,clean['cwd'])
    record={'id':rid,'profile':PROFILES[provider],'received_at':received,'source':provider,
        'project_key':identity.key,'identity_method':identity.method,
        'logical_session_key':so.logical_session_key(provider,clean['session_id']),
        'agent_id':clean.get('agent_id',''),'working_copy_id':copy['id'],
        'working_copy_path':copy['normalized_path'],'event_name':clean['hook_event_name'],
        'payload':clean,'payload_hash':so.content_id(clean),'occurred_at':None,
        'loaded_content_hash':None,'rule_revision_id':None,'source_authentication':'local_unattested'}
    with store.transaction(write=True):
        prior=store.query_one('SELECT * FROM native_load_reports WHERE id=?',(rid,))
        if prior:
            old=_read(prior)
            if old['source']!=provider or old['payload']!=clean: raise NativeLoadError('Native receipt replay differs')
            return old
        store.insert('native_load_reports',{k:record[k] for k in COLUMNS} |
            {'record_json':so.canonical_json(record),'record_hash':so.content_id(record)})
    return record


def native_history(store,*,project_key,logical_session_key=None,working_copy_id=None,limit=20,cursor=None):
    if not isinstance(project_key,str) or not project_key.strip(): raise NativeLoadRequestError('Project key is required')
    if type(limit) is not int or not 1<=limit<=100: raise NativeLoadRequestError('Page limit must be from 1 to 100')
    for value in (logical_session_key,working_copy_id):
        if value is not None and (not isinstance(value,str) or not re.fullmatch('[a-f0-9]{64}',value)):
            raise NativeLoadRequestError('Invalid native history selector')
    selector=so.content_id([PROFILE,project_key,logical_session_key,working_copy_id])
    position=None
    if cursor is not None:
        try:
            pos=json.loads(base64.urlsafe_b64decode(cursor).decode())
            if set(pos)!={'selector','received_at','id'} or pos['selector']!=selector: raise ValueError()
            _uuid(pos['id'])
            if not pos['received_at'] or so.normalize_timestamp(pos['received_at'])!=pos['received_at']: raise ValueError()
            position=pos
        except (ValueError,TypeError,KeyError,UnicodeError) as exc:
            raise NativeLoadRequestError('Invalid native history cursor or changed selection') from exc
    result={'project_key':project_key,'logical_session_key':logical_session_key,'working_copy_id':working_copy_id,
            'records':[],'count':None,'next_cursor':None,'coverage_complete':False,
            'in_force_instructions':None,'rule_revision_attribution':'unknown',
            'event_time':'unknown','order':'receiver_time_descending','reason':'schema_unavailable'}
    if not _schema(store,missing_ok=True): return result
    clauses=['project_key=?'];args=[project_key]
    for key,value in (('logical_session_key',logical_session_key),('working_copy_id',working_copy_id)):
        if value is not None:clauses.append(key+'=?');args.append(value)
    where=' AND '.join(clauses)
    count=store.query_one('SELECT COUNT(*) n FROM native_load_reports WHERE '+where,args)['n']
    if position:
        where+=' AND (received_at<? OR (received_at=? AND id<?))'
        args.extend([position['received_at'],position['received_at'],position['id']])
    rows=store.query('SELECT * FROM native_load_reports WHERE '+where+' ORDER BY received_at DESC,id DESC LIMIT ?',(*args,limit+1))
    records=[_read(row) for row in rows[:limit]]
    following=None
    if len(rows)>limit:
        last=records[-1];following=base64.urlsafe_b64encode(so.canonical_json({'selector':selector,'received_at':last['received_at'],'id':last['id']}).encode()).decode()
    return {**result,'records':records,'count':count,'next_cursor':following,
            'reason':'no_retained_reports' if not count else 'native_time_content_and_continuity_unverified'}


def hook_settings(*,config_path,python_path,provider='claude'):
    config=Path(config_path);python=Path(python_path)
    if not config.is_absolute() or not python.is_absolute():
        raise NativeLoadError('Hook settings require absolute executable and config paths')
    if provider not in PROFILES:
        raise NativeLoadError('Unsupported native provider')
    command=shlex.join([str(python),'-m','self_improve.cli','--config',str(config),'record-session-event','--provider',provider])
    events = CODEX_EVENTS if provider == 'codex' else EVENTS
    return {'hooks':{event:[{'hooks':[{'type':'command','command':command,
        'timeout':3 if provider == 'codex' and event in {'SessionEnd','Interrupt'} else 20}]}] for event in events}}


def native_cli(args):
    """Keep hook output empty so observability never injects context or consent."""
    from contextlib import closing
    import sqlite3
    import sys
    from .config import load_config
    from .store import Store
    try:
        if not args.config or not Path(args.config).is_file():
            raise NativeLoadError('Native commands require an explicit existing --config file')
        config=Path(args.config).resolve()
        if args.command=='session-hook-settings':
            print(json.dumps(hook_settings(config_path=config,python_path=sys.executable,provider=args.provider),indent=2))
            return 0
        # Bound the read before JSON parsing; never echo input or exception payloads.
        raw=sys.stdin.buffer.read(MAX_INPUT_BYTES+1)
        payload=parse_input(raw,provider=args.provider)
        cfg=load_config(config)
        with closing(Store(cfg.state_path('state.db'),migrate=False)) as store:
            record_native_event(store,cfg,payload,receipt_id=args.receipt_id,provider=args.provider)
        return 0
    except NativeLoadError as exc:
        print(str(exc),file=sys.stderr)
        return 1
    except (OSError,sqlite3.Error,ValueError):
        print('Native receiver could not read configuration or publish to the existing state database',file=sys.stderr)
        return 1
