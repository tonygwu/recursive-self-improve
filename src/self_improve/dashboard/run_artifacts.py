"""Read retained run files only from the selected database's local bundle."""
from contextlib import contextmanager, ExitStack
import errno
import hashlib
import json
import os
import re
import stat

from .run_data import RunDataError, RunNotFound, _run, _commands

PREVIEW_BYTES = 65536
_COMPONENT = re.compile(r'[A-Za-z0-9_-]{1,160}\Z')
_RAW = re.compile(r'([A-Za-z0-9_-]{1,160})\.(?:pick[1-9][0-9]*\.json|a[1-9][0-9]*\.[a-z][a-z0-9_-]*\.(?:stdout|stderr))\Z')


class ArtifactError(RunDataError):
    def __init__(self, code, detail, status=400):
        super().__init__(detail)
        self.code, self.status = code, status


def _component(value):
    if not isinstance(value, str) or not _COMPONENT.fullmatch(value):
        raise ArtifactError('UnsafeArtifact', 'Artifact identities must be safe single path components.')
    return value


def _call_ids(store, run_id):
    """A partial journal/trace is an explicit link even before call completion."""
    ids={r['id'] for r in store.query('SELECT id FROM llm_calls WHERE run_id=?', (run_id,))}
    from .. import jobs, eval_history
    for cid in _commands(store, run_id):
        row=store.query_one('SELECT * FROM commands WHERE id=?', (cid,))
        if row is None:raise RunDataError('run '+run_id+': missing job command '+cid)
        ids.update(c['id'] for c in jobs.status(store, row)['calls'])
    if eval_history.available(store):
        for row in store.query('SELECT id FROM eval_attempts WHERE run_id=?', (run_id,)):
            attempt=eval_history.detail(store, row['id'])
            ids.update(e['data']['call_id'] for e in attempt['events'] if e['kind']=='call_started')
    # A partial trace is not permission to contradict an existing audit owner.
    ordered=sorted(ids)
    for offset in range(0,len(ordered),250):
        batch=ordered[offset:offset+250]
        rows=store.query('SELECT id,run_id FROM llm_calls WHERE id IN ('+','.join('?' for _ in batch)+')',tuple(batch))
        for row in rows:
            if row['run_id']!=run_id:
                raise RunDataError('run '+run_id+': call '+row['id']+' belongs to a different run')
    return ids


def _parts(store, run_id, key):
    _run(store, run_id);_component(run_id)
    if key=='report':return ['runs', run_id, 'report.md']
    match=_RAW.fullmatch(key)
    if not match or match[1] not in _call_ids(store,run_id):
        raise ArtifactError('ArtifactNotLinked', 'This artifact has no retained call link to the selected run.', 404)
    return ['runs',run_id,'raw',key]


def _error(exc):
    if exc.errno==errno.ENOENT:
        return ArtifactError('ArtifactMissing', 'File is not included beside the selected database.', 404)
    if exc.errno in (errno.ELOOP,errno.ENOTDIR):
        return ArtifactError('UnsafeArtifact', 'A nested artifact path is a symlink or is not a directory.', 409)
    if exc.errno in (errno.EACCES,errno.EPERM):
        return ArtifactError('ArtifactUnreadable', 'This artifact cannot be read with the current permissions.', 403)
    return ArtifactError('ArtifactReadError', 'Artifact access failed: '+str(exc), 500)


@contextmanager
def _open(store, parts, *, directory=False):
    # Only the explicitly selected database parent is trusted. Each nested
    # component uses a directory descriptor, so a rename cannot redirect a read.
    with ExitStack() as stack:
        try:
            fd=os.open(store.db_path.parent, os.O_RDONLY|os.O_DIRECTORY)
            stack.callback(os.close,fd)
            for index, part in enumerate(parts):
                flags=os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK
                if directory or index<len(parts)-1:flags|=os.O_DIRECTORY
                fd=os.open(part,flags,dir_fd=fd);stack.callback(os.close,fd)
            info=os.fstat(fd)
            if not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1):
                raise ArtifactError('UnsafeArtifact','Only regular, singly linked artifact files can be read.',409)
            yield fd
        except OSError as exc:
            raise _error(exc) from exc


def _version(info):
    return hashlib.sha256(json.dumps([info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns]).encode()).hexdigest()


def _metadata(store, run_id, key, parts):
    row={'key':key,'label':'Run report' if key=='report' else key,'call_id':None if key=='report' else _RAW.fullmatch(key)[1]}
    try:
        with _open(store, parts) as fd:
            info=os.fstat(fd)
            return row|{'state':'available','bytes':info.st_size,'version':_version(info),'reason':''}
    except ArtifactError as exc:
        return row|{'state':exc.code,'bytes':None,'version':None,'reason':str(exc)}


def catalog(store, run_id, *, limit=20, cursor=None):
    row=_run(store,run_id);_component(run_id)
    if type(limit) is not int or not 1<=limit<=100:raise RunDataError('Artifact limit must be 1 through 100.')
    ids=_call_ids(store,run_id);keys=['report'];raw_state='available';reason=''
    try:
        with _open(store,['runs',run_id,'raw'],directory=True) as fd:
            with os.scandir(fd) as entries:
                keys+=sorted(e.name for e in entries if (m:=_RAW.fullmatch(e.name)) and m[1] in ids)
    except ArtifactError as exc:raw_state=exc.code;reason=str(exc)
    offset=0
    if cursor is not None:
        try:
            parsed=json.loads(cursor)
            if not isinstance(parsed,list) or len(parsed)!=2 or parsed[0]!=run_id:raise ValueError()
            offset=keys.index(parsed[1])+1
        except (ValueError,TypeError) as exc:raise RunDataError('Invalid artifact cursor for this run.') from exc
    selected=keys[offset:offset+limit]
    return {'run_id':run_id,'records':[_metadata(store,run_id,k,['runs',run_id,'report.md'] if k=='report' else ['runs',run_id,'raw',k]) for k in selected],
        'count':len(keys),'next_cursor':json.dumps([run_id,selected[-1]]) if offset+limit<len(keys) else None,
        'recorded_report_path':row['report_path'],'raw_state':raw_state,
        'reason':reason,'coverage':'Only local files with exact retained call links are listed. Raw-file retention completeness is unknown. Original recorded paths never authorize file access.'}


def read(store, run_id, key):
    with _open(store,_parts(store,run_id,key)) as fd:
        before=os.fstat(fd);version=_version(before)
        data=os.read(fd,PREVIEW_BYTES)
        if _version(os.fstat(fd))!=version:raise ArtifactError('ArtifactChanged','File changed during inspection. Inspect it again.',409)
        try:text=data.decode('utf-8');encoding='utf-8'
        except UnicodeDecodeError:text=data.decode('utf-8',errors='replace');encoding='utf-8 with replacement; download preserves exact bytes'
        return {'run_id':run_id,'key':key,'version':version,'bytes':before.st_size,'preview_bytes':len(data),
                'omitted_bytes':before.st_size-len(data),'text':text,'encoding':encoding}


def download(store, run_id, key, version):
    """Transfer descriptor ownership to the response; caller must close it."""
    held=_open(store,_parts(store,run_id,key));fd=held.__enter__()
    try:
        info=os.fstat(fd)
        if _version(info)!=version:raise ArtifactError('ArtifactChanged','File changed since inspection. Inspect it again.',409)
    except BaseException:
        held.__exit__(None,None,None);raise
    closed=False
    def close():
        nonlocal closed
        if not closed:
            closed=True;held.__exit__(None,None,None)
    def chunks():
        try:
            remaining=info.st_size
            while remaining:
                if _version(os.fstat(fd))!=version:raise ArtifactError('ArtifactChanged','File changed during download.',409)
                chunk=os.read(fd,min(65536,remaining))
                if _version(os.fstat(fd))!=version:raise ArtifactError('ArtifactChanged','File changed during download.',409)
                if not chunk:raise ArtifactError('ArtifactChanged','File shortened during download.',409)
                remaining-=len(chunk)
                yield chunk
            if _version(os.fstat(fd))!=version:raise ArtifactError('ArtifactChanged','File changed during download.',409)
        finally:close()
    return chunks(), info.st_size, close
