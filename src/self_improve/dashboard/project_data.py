"""Canonical project detail. Historical delivery and present availability differ.

The caller owns the read transaction and selected Store. Only current proposal
attribution resolves local Git metadata; retained evidence never opens a transcript
or instruction file. No migration, network lookup, cache write or model call runs.
"""
import base64
import json

from .. import destinations, execution_policy, rejections, rule_revisions
from ..commands import CommandError
from ..scan_observations import content_id as digest, logical_session_key


class ProjectRequestError(ValueError):
    pass


class ProjectNotFound(ProjectRequestError):
    pass


KINDS = ('contributed', 'evidence', 'proposals', 'deliveries')


def _has(store, migration):
    return bool(store.query_one('SELECT name FROM schema_migrations WHERE name=?', (migration,)))


def _project(store, key):
    if not isinstance(key, str) or not key:
        raise ProjectRequestError('A non-empty canonical project_key is required.')
    known = store.query_one('SELECT file_path FROM sessions WHERE project_key=? LIMIT 1', (key,))
    known = known or store.query_one('SELECT id FROM incidents WHERE project_key=? LIMIT 1', (key,))
    for migration, table in (('0022_scan_observations', 'scan_working_copies'),
                             ('0024_rule_availability', 'rule_revisions'),
                             ('0028_instruction_inventory', 'instruction_inventories')):
        if not known and _has(store, migration):
            known = store.query_one(f'SELECT id FROM {table} WHERE project_key=? LIMIT 1', (key,))
    if not known:
        raise ProjectNotFound('No retained project with this canonical identity.')


def detail(store, *, project_key):
    """Compact summary without current filesystem inference or capped list totals."""
    _project(store, project_key)
    sessions = store.query('SELECT source,session_id,project_path,project_display,project_key_method,'
                           'is_subagent,headless FROM sessions WHERE project_key=?', (project_key,))
    known_ids = {logical_session_key(r['source'], r['session_id']) for r in sessions if r['session_id']}
    paths = {}
    for row in sessions:
        path = paths.setdefault(row['project_path'], {'path': row['project_path'], 'transcripts': 0,
                                                     'known_session_ids': set(), 'unknown_session_ids': 0})
        path['transcripts'] += 1
        if row['session_id']:
            path['known_session_ids'].add(logical_session_key(row['source'], row['session_id']))
        else:
            path['unknown_session_ids'] += 1
    for path in paths.values():
        path['known_session_ids'] = len(path['known_session_ids'])
    displays = sorted({r['project_display'] for r in sessions if r['project_display']})
    signals = store.query('SELECT signal_type,COUNT(*) AS count FROM incidents WHERE project_key=? '
                          'GROUP BY signal_type ORDER BY count DESC,signal_type', (project_key,))
    contributed = store.query_one('SELECT COUNT(DISTINCT il.learning_id) n FROM incident_learnings il '
                                  'JOIN incidents i ON i.id=il.incident_id WHERE i.project_key=?', (project_key,))['n']
    observed = {'known_session_ids': None, 'unknown_id_transcripts': None, 'reason': 'schema_unavailable'}
    copies = []
    if _has(store, '0022_scan_observations'):
        # Multiple versions and repeated observations of a line do not create
        # more sessions. Event-level project attribution handles cwd changes.
        rows = store.query('SELECT DISTINCT logical_session_key,transcript_id FROM scan_lines '
                           'WHERE project_key=? AND active=1', (project_key,))
        observed = {'known_session_ids': len({r['logical_session_key'] for r in rows if r['logical_session_key']}),
                    'unknown_id_transcripts': len({r['transcript_id'] for r in rows if not r['logical_session_key']}),
                    'reason': '' if rows else 'no_observed_lines'}
        if not rows:
            observed.update(known_session_ids=None, unknown_id_transcripts=None)
        copies = store.query('SELECT id,normalized_path,normalization FROM scan_working_copies '
                             'WHERE project_key=? ORDER BY normalized_path,id', (project_key,))
    delivered = None
    if _has(store, rule_revisions.MIGRATION):
        rule_revisions.require_schema(store)
        delivered = store.query_one('SELECT COUNT(*) n FROM rule_revisions WHERE project_key=?', (project_key,))['n']
    return {'project_key': project_key, 'label': displays[0] if displays else project_key,
            'displays': displays, 'identity_methods': sorted({r['project_key_method'] for r in sessions if r['project_key_method']}),
            'indexed': {'transcripts': len(sessions), 'known_session_ids': len(known_ids),
                        'unknown_session_ids': sum(not r['session_id'] for r in sessions),
                        'subagent_transcripts': sum(bool(r['is_subagent']) for r in sessions),
                        'headless_transcripts': sum(bool(r['headless']) for r in sessions)},
            'observed': observed, 'indexed_paths': sorted(paths.values(), key=lambda r: r['path']),
            'observed_working_copies': copies, 'signals': signals,
            'incidents': sum(r['count'] for r in signals), 'contributed_learnings': contributed,
            'retained_deliveries': delivered,
            'session_note': 'Known session IDs are deduplicated by provider and ID. Indexed transcripts and event-attributed observed sessions have different coverage. Neither proves that a session loaded instructions.',
            'delivery_note': 'Retained deliveries are historical application revisions, including later changed or rolled-back content. Global deliveries and historical applications without retained revisions are not counted here.'}


def _proposed(store, cfg, key):
    result, unresolved, resolved = [], [], {}
    for proposal in execution_policy.proposal_dispositions(store, cfg):
        pair = (proposal['target_path'], proposal['target_kind'])
        if pair not in resolved:
            try:
                destination = destinations.resolve_destination(cfg, *pair)
                resolved[pair] = (rejections.target_identity(store, destination), '')
            except CommandError as exc:
                if exc.code != 'TargetIdentityUnavailable' or exc.status_code != 409:
                    raise
                resolved[pair] = (None, str(exc))
            except (destinations.DestinationError, OSError) as exc:
                resolved[pair] = (None, str(exc))
        identity, error = resolved[pair]
        if identity is None:
            unresolved.append({'proposal_id': proposal['id'], 'reason': error})
        elif identity.get('project_key') == key:
            learning = store.query_one('SELECT title,rule_text FROM learnings WHERE id=?', (proposal['learning_id'],))
            result.append({**proposal, 'target_identity': identity,
                           'learning': learning, 'association': 'Current canonical destination; not delivered.'})
    return result, unresolved


def records(store, cfg, *, project_key, kind, learning_id=None, limit=20, cursor=None):
    """Keyset pages bound to canonical project, list kind and optional lesson."""
    if kind not in KINDS or type(limit) is not int or not 1 <= limit <= 100:
        raise ProjectRequestError('Choose a known project record kind and a limit from 1 to 100.')
    if (kind == 'evidence') != bool(learning_id):
        raise ProjectRequestError('Only evidence pages require a non-empty learning_id.')
    _project(store, project_key)
    selector = digest(['project-records/1', project_key, kind, learning_id])
    position = ''
    if cursor is not None:
        try:
            token = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
            if set(token) != {'selector', 'position'} or token['selector'] != selector:
                raise ValueError('different selector')
            position = token['position']
            if not isinstance(position, str) or not position:
                raise ValueError('invalid position')
        except (ValueError, TypeError, KeyError, UnicodeError) as exc:
            raise ProjectRequestError('Invalid project cursor or different selection.') from exc
    reason, unresolved = '', []
    if kind == 'contributed':
        rows = store.query('SELECT l.id,l.title,l.rule_text,l.why,l.category,l.scope,l.status,'
                           'COUNT(DISTINCT i.id) AS project_incidents FROM learnings l '
                           'JOIN incident_learnings il ON il.learning_id=l.id JOIN incidents i ON i.id=il.incident_id '
                           'WHERE i.project_key=? GROUP BY l.id', (project_key,))
    elif kind == 'evidence':
        rows = store.query('SELECT i.* FROM incidents i JOIN incident_learnings il ON il.incident_id=i.id '
                           'WHERE i.project_key=? AND il.learning_id=?', (project_key, learning_id))
    elif kind == 'proposals':
        rows, unresolved = _proposed(store, cfg, project_key)
    elif not _has(store, rule_revisions.MIGRATION):
        rows, reason = [], 'schema_unavailable'
    else:
        rule_revisions.require_schema(store)
        rows = [rule_revisions.read_record(r, 'rule_revisions') for r in store.query(
            'SELECT * FROM rule_revisions WHERE project_key=?', (project_key,))]
        if not rows:
            reason = 'no_retained_deliveries'
    ordered = sorted(rows, key=lambda r: r['id'])
    remaining = [r for r in ordered if r['id'] > position]
    page = remaining[:limit]
    next_cursor = None
    if len(remaining) > limit:
        next_cursor = base64.urlsafe_b64encode(json.dumps({'selector': selector, 'position': page[-1]['id']}).encode()).decode()
    return {'project_key': project_key, 'kind': kind, 'learning_id': learning_id, 'records': page,
            'count': None if reason == 'schema_unavailable' else len(rows), 'next_cursor': next_cursor,
            'reason': reason, 'unresolved_proposals': unresolved,
            'ordering': 'Stable record ID; timestamps are not an identity or a cross-page join.'}
