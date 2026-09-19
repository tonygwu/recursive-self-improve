"""Pure validation and presentation of retained incident archives.

This module reads no external resources and never changes source records.
"""
from datetime import datetime, timezone
import json
import re


class IncidentEvidenceError(ValueError):
    pass


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key ' + key)
        result[key] = value
    return result


def _constant(value):
    raise ValueError('non-JSON constant ' + value)


def parse_window(raw, *, owner):
    """Validate every archived entry; incomplete legacy fields remain unknown."""
    try:
        window = json.loads(raw, object_pairs_hook=_object, parse_constant=_constant)
        if not isinstance(window, list):
            raise ValueError('window_json must be an array')
        for index, entry in enumerate(window, 1):
            label = f'window_json entry {index}'
            if not isinstance(entry, dict):
                raise ValueError(label + ' must be an object')
            for field in ('role', 'text', 'ts', 'session_file', 'project_path'):
                if field in entry and not isinstance(entry[field], str):
                    raise ValueError(f'{label}.{field} must be a string')
            if 'count_in_session' in entry and (type(entry['count_in_session']) is not int or entry['count_in_session'] < 0):
                raise ValueError(label + '.count_in_session must be a nonnegative integer')
        return window
    except json.JSONDecodeError as exc:
        raise IncidentEvidenceError(f'{owner}: window_json is not strict JSON: {exc}') from exc
    except (TypeError, ValueError) as exc:
        raise IncidentEvidenceError(f'{owner}: {exc}') from exc


def entry_kind(entry):
    turn = bool(entry.get('role'))
    occurrence = 'count_in_session' in entry or 'session_file' in entry
    return 'mixed' if turn and occurrence else 'turn' if turn else 'occurrence' if occurrence else 'unknown'


def _time(value):
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return stamp.astimezone(timezone.utc) if stamp.tzinfo else None
    except (ValueError, TypeError, AttributeError):
        return None


def present(row, *, owner=None, max_chars=None):
    """Derive readable, bounded summaries without editing frozen source fields."""
    return prepare(row, owner=owner, max_chars=max_chars)['presentation']


def _transform_strings(value, transform, owner):
    if isinstance(value, str):
        result = transform(value)
        if not isinstance(result, str):
            raise IncidentEvidenceError(owner + ': transform_text must return a string')
        return result
    if isinstance(value, list):
        return [_transform_strings(v, transform, owner) for v in value]
    if isinstance(value, dict):
        return {k:_transform_strings(v, transform, owner) for k,v in value.items()}
    return value


def prepare(row, *, owner=None, max_chars=None, transform_text=None):
    """Validate once; transform complete strings before deriving a bounded view."""
    owner = owner or f"incidents.{row.get('id')}"
    window = parse_window(row.get('window_json'), owner=owner)
    matched = row.get('matched_text', '')
    if not isinstance(matched, str):
        raise IncidentEvidenceError(owner + ': matched_text must be a string')
    if transform_text is not None:
        matched = _transform_strings(matched, transform_text, owner)
        window = _transform_strings(window, transform_text, owner)
    return {'window':window, 'matched_text':matched,
            'presentation':_present_window(window, matched, max_chars=max_chars)}


def _present_window(window, matched, *, max_chars):
    fingerprint = matched if re.fullmatch(r'[a-fA-F0-9]{40}', matched) else ''
    kinds = {entry_kind(e) for e in window}
    kind = 'empty' if not kinds else next(iter(kinds)) if len(kinds) == 1 else 'mixed'
    text = matched if not fingerprint else ''
    if not text:
        text = next((e['text'] for e in window if e.get('text')), '')
    reason = '' if text else 'No readable text was retained.'
    if not text:
        text = f'(no readable text retained; error fingerprint {fingerprint[:8]}…)' if fingerprint else '(no readable text retained)'
    cut = None
    if max_chars is not None and len(text) > max_chars:
        cut = {'cut_chars': len(text) - max_chars, 'original_chars': len(text)}
        text = text[:max_chars]
    entries = [e for e in window if entry_kind(e) in {'occurrence', 'mixed'}]
    occurrences, coverage = None, None
    if entries:
        paths = sorted({e['project_path'] for e in entries if e.get('project_path')})
        sessions = {e['session_file'] for e in entries if e.get('session_file')}
        missing_count = sum('count_in_session' not in e for e in entries)
        missing_session = sum(not e.get('session_file') for e in entries)
        times = [(t, e['ts']) for e in entries if (t := _time(e.get('ts')))]
        occurrences = {
            'sessions': None if missing_session else len(sessions),
            'total_count': None if missing_count else sum(e['count_in_session'] for e in entries),
            'first_ts': min(times)[1] if times else '',
            'last_ts': max(times)[1] if times else '',
            'project_paths': paths,
        }
        if missing_count:
            occurrences['count_reason'] = f'{missing_count} occurrence entries have no retained count.'
        if missing_session:
            occurrences['session_reason'] = f'{missing_session} occurrence entries have no retained session path.'
        coverage = {
            'entries': len(entries), 'other_entries': len(window) - len(entries),
            'known_sessions': len(sessions),
            'unknown_times': len(entries) - len(times),
            'unknown_projects': sum(not e.get('project_path') for e in entries),
            'locations': paths[:5], 'locations_omitted': max(0, len(paths) - 5),
        }
    return {'window_kind': kind, 'window_len': len(window), 'display_text': text,
            'display_text_reason': reason, 'display_text_truncated': cut,
            'fingerprint': fingerprint, 'matched_text_is_fingerprint': bool(fingerprint),
            'occurrences': occurrences, 'occurrence_coverage': coverage}
