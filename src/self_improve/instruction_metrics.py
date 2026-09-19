"""Versioned context measurements from observed text and a bounded Markdown walk.

Other Markdown is never instruction weight. No text from that walk is retained.
This module has no Store, provider or instruction-writer access.
"""
from __future__ import annotations

from itertools import islice
import os
from pathlib import Path
import stat

from .rule_revisions import text_hash

METHOD = 'utf8-unicode-codepoints/1'
MAX_ENTRIES = 10000
MAX_FILES = 2000
MAX_DEPTH = 8
MAX_FILE_BYTES = 1 << 20
MAX_TOTAL_BYTES = 16 << 20
EXCLUDED = ('.git', '.hg', '.svn', '.venv', 'venv', 'node_modules', '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache')


def _signature(value):
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _read(path, limit):
    """Read one regular file without following a changed final symlink."""
    before = path.stat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError('not_regular_file')
    flags = os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_NOFOLLOW', 0)
    with os.fdopen(os.open(path, flags), 'rb') as handle:
        raw = handle.read(limit + 1)
        after = os.fstat(handle.fileno())
    if _signature(before) != _signature(after) or _signature(after) != _signature(path.stat()):
        raise ValueError('file_changed_during_observation')
    if len(raw) > limit:
        raise ValueError('file_byte_limit' if limit == MAX_FILE_BYTES else 'total_byte_limit')
    return raw, before


def _totals(files):
    return {'files': len(files), **{k: sum(f[k] for f in files) for k in ('bytes', 'characters')}}


def collect(surface, working_copy_path):
    """Filesystem work only; caller publishes the returned content-free record."""
    files = [{'real_path': f['real_path'], 'content_hash': f['content_hash'],
              'bytes': f['bytes'], 'characters': len(f['_text'])} for f in surface['files']]
    result = {'version': 1, 'method': METHOD,
              'meaning': 'UTF-8 bytes and Unicode code points, including whitespace. No token count or runtime receipt is inferred.',
              'instructions': {'status': 'partial' if surface['issues'] else 'measured',
                               'files': files, 'totals': _totals(files)},
              'other_markdown': {'status': 'measured', 'files': [], 'totals': _totals([]), 'issues': [],
                                 'exclusions': [], 'deduplicated': [], 'entries_examined': 0,
                                 'root': '',
                                 'scope': 'Repository Markdown files outside observed instruction sources; directory symlinks and external file links excluded.',
                                 'limits': {'entries': MAX_ENTRIES, 'files': MAX_FILES, 'depth': MAX_DEPTH,
                                            'file_bytes': MAX_FILE_BYTES, 'total_bytes': MAX_TOTAL_BYTES,
                                            'excluded_directories': list(EXCLUDED)}}}
    other = result['other_markdown']
    def issue(cause, path):
        other['issues'].append({'cause': cause, 'path': str(path)})
    def exclude(cause, path):
        other['exclusions'].append({'cause': cause, 'path': str(path)})
    if any(i['cause'] in {'working_copy_identity_changed', 'working_copy_missing', 'working_copy_not_git'} for i in surface['issues']):
        other.update(status='unavailable', totals=None)
        issue('working_copy_not_observed', working_copy_path)
        return result
    root = Path(working_copy_path).resolve()
    while root.parent != root and not os.path.lexists(root/'.git'):
        root = root.parent
    if not os.path.lexists(root/'.git'):
        other.update(status='unavailable', totals=None)
        issue('repository_root_unavailable', working_copy_path)
        return result
    other['root'] = str(root)
    if surface['issues']:
        issue('instruction_classification_incomplete', root)
    instruction_paths = {f['real_path'] for f in files}
    # Named failed instruction reads cannot become ordinary report text.
    instruction_paths.update(str(Path(i['path']).absolute()) for i in surface['issues'] if i.get('path'))
    physical = {}
    for f in surface['files']:
        if f['real_path'].startswith('managed-text:'):
            continue
        try:
            st = Path(f['real_path']).stat()
            physical[(st.st_dev, st.st_ino)] = f['real_path']
        except OSError:
            issue('instruction_identity_unavailable', f['real_path'])
    pending = [(root, 0)]
    used_bytes = 0
    while pending:
        directory, depth = pending.pop()
        try:
            if directory.resolve() != directory or not directory.is_relative_to(root):
                exclude('directory_symlink', directory); continue
            before = directory.stat()
            remaining = MAX_ENTRIES - other['entries_examined']
            with os.scandir(directory) as handle:
                entries = list(islice(handle, remaining + 1))
            if len(entries) > remaining:
                issue('entry_limit', directory)
                entries = entries[:remaining]
            if _signature(before) != _signature(directory.stat()):
                issue('directory_changed_during_observation', directory)
            for entry in sorted(entries, key=lambda e: e.name):
                other['entries_examined'] += 1
                path = Path(entry.path)
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name in EXCLUDED:
                            exclude('excluded_directory', path)
                        elif os.path.lexists(path/'.git'):
                            exclude('nested_repository', path)
                        elif depth >= MAX_DEPTH:
                            issue('depth_limit', path)
                        else:
                            pending.append((path, depth + 1))
                        continue
                    if entry.is_symlink() and entry.is_dir():
                        exclude('directory_symlink', path); continue
                    if path.suffix.lower() not in {'.md', '.markdown'}:
                        continue
                    real = path.resolve(strict=True)
                    if not real.is_relative_to(root):
                        exclude('external_file_symlink', path); continue
                    st = real.stat()
                    identity = (st.st_dev, st.st_ino)
                    if str(real) in instruction_paths or str(path) in instruction_paths or identity in physical:
                        other['deduplicated'].append({'path': str(path), 'retained_path': physical.get(identity, str(real))})
                        continue
                    if len(other['files']) >= MAX_FILES:
                        issue('file_count_limit', path); continue
                    if used_bytes >= MAX_TOTAL_BYTES:
                        issue('total_byte_limit', path); continue
                    limit = min(MAX_FILE_BYTES, MAX_TOTAL_BYTES-used_bytes)
                    used_bytes += min(st.st_size, limit)
                    raw, observed = _read(real, limit)
                    if (path.resolve(strict=True) != real or _signature(st) != _signature(observed)
                            or _signature(observed) != _signature(path.stat())):
                        raise ValueError('file_changed_during_observation')
                    text = raw.decode('utf-8')
                    other['files'].append({'path': str(path), 'real_path': str(real),
                                           'bytes': len(raw), 'characters': len(text), 'content_hash': text_hash(text)})
                    physical[identity] = str(real)
                except UnicodeError as exc:
                    issue('file_unreadable:'+type(exc).__name__, path)
                except ValueError as exc:
                    issue(str(exc), path)
                except (OSError, UnicodeError, RuntimeError) as exc:
                    issue('file_unreadable:'+type(exc).__name__, path)
            if _signature(before) != _signature(directory.stat()):
                issue('directory_changed_during_observation', directory)
            if other['entries_examined'] >= MAX_ENTRIES and pending:
                issue('entry_limit', pending[-1][0]); break
        except OSError as exc:
            issue('directory_unreadable:'+type(exc).__name__, directory)
    other['files'].sort(key=lambda f: f['real_path'])
    other['totals'] = _totals(other['files'])
    other['status'] = 'partial' if other['issues'] else 'measured'
    return result


def validate(measurements, record):
    """Check retained measurements without rereading the observed files."""
    m = measurements
    if m['version'] != 1 or type(m['version']) is not int or m['method'] != METHOD:
        raise ValueError('unsupported context measurement method')
    for name in ('instructions', 'other_markdown'):
        section = m[name]
        if section['status'] not in {'measured', 'partial', 'unavailable'} or not isinstance(section['files'], list):
            raise ValueError('invalid measurement coverage')
        paths = set()
        for f in section['files']:
            if not isinstance(f['real_path'], str) or f['real_path'] in paths:
                raise ValueError('duplicate measurement file')
            paths.add(f['real_path'])
            if any(type(f[k]) is not int or f[k] < 0 for k in ('bytes', 'characters')) or f['characters'] > f['bytes']:
                raise ValueError('invalid character or byte count')
            if not isinstance(f['content_hash'], str) or len(f['content_hash']) != 64:
                raise ValueError('invalid measurement content hash')
        expected = None if section['status'] == 'unavailable' else _totals(section['files'])
        if expected is not None and (not isinstance(section['totals'], dict) or any(type(v) is not int for v in section['totals'].values())):
            raise ValueError('invalid measurement totals')
        if section['totals'] != expected or (expected is None and section['files']):
            raise ValueError('measurement totals do not reconcile')
    observed = {f['real_path']: f for f in record['files']}
    if set(observed) != {f['real_path'] for f in m['instructions']['files']}:
        raise ValueError('instruction measurements differ from inventory')
    for f in m['instructions']['files']:
        if any(f[k] != observed[f['real_path']][k] for k in ('bytes', 'content_hash')):
            raise ValueError('instruction measurement content differs')
    if m['instructions']['status'] != ('partial' if record['issues'] else 'measured'):
        raise ValueError('instruction measurement coverage differs')
    other = m['other_markdown']
    if not isinstance(other['root'], str):
        raise ValueError('invalid Markdown repository root')
    if not isinstance(other['scope'], str) or not other['scope'] or set(observed) & {f['real_path'] for f in other['files']}:
        raise ValueError('invalid or overlapping Markdown scope')
    for k in ('issues', 'exclusions', 'deduplicated'):
        if not isinstance(other[k], list) or any(not isinstance(i, dict) or not isinstance(i.get('path'), str) for i in other[k]):
            raise ValueError('invalid Markdown coverage details')
    if any(not isinstance(i.get('cause'), str) or not i['cause'] for k in ('issues', 'exclusions') for i in other[k]):
        raise ValueError('invalid Markdown omission cause')
    if type(other['entries_examined']) is not int or other['entries_examined'] < 0:
        raise ValueError('invalid Markdown entry count')
    limits = other['limits']
    if any(type(limits[k]) is not int or limits[k] < 0 for k in ('entries', 'files', 'depth', 'file_bytes', 'total_bytes')):
        raise ValueError('invalid Markdown limits')
    if not isinstance(limits['excluded_directories'], list) or any(not isinstance(v,str) for v in limits['excluded_directories']):
        raise ValueError('invalid excluded directories')
    if (other['status'] == 'measured') != (not other['issues']):
        raise ValueError('Markdown coverage and status disagree')
    if (other['entries_examined'] > limits['entries'] or len(other['files']) > limits['files']
            or any(f['bytes'] > limits['file_bytes'] for f in other['files'])
            or (other['totals'] and other['totals']['bytes'] > limits['total_bytes'])):
        raise ValueError('Markdown counts exceed limits')


def compare(current, previous):
    """Adjacent observations, not a causal benefit or native context claim."""
    result = {}
    for name in ('instructions', 'other_markdown'):
        reason = ''
        a, b = current.get('measurements'), previous.get('measurements') if previous else None
        if previous is None: reason = 'no_prior_observation'
        elif current['status'] == 'conflicting' or previous['status'] == 'conflicting': reason = 'conflicting_observations'
        elif not a or not b: reason = 'measurement_not_recorded'
        elif current['profile'] != previous['profile'] or a['method'] != b['method'] or a['version'] != b['version'] or current.get('limits') != previous.get('limits'): reason = 'incompatible_measurement_scope'
        elif name == 'other_markdown' and any(a[name][k] != b[name][k] for k in ('root', 'scope', 'limits')): reason = 'incompatible_measurement_scope'
        elif a[name]['status'] != 'measured' or b[name]['status'] != 'measured': reason = 'incomplete_measurement'
        result[name] = {'computable': not reason, 'reason': reason, 'delta': None if reason else {
            k: a[name]['totals'][k]-b[name]['totals'][k] for k in ('files','bytes','characters')}}
    return result
