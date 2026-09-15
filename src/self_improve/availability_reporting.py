"""Shared, strict labels for native availability collection results."""
from .rule_revisions import AvailabilityError
from .scan_reporting import counter_map

METRICS = {'known_working_copies': 'Known working copies', 'tracked_revisions': 'Retained delivered revisions',
           'observations': 'Rule and working-copy checks'}


def summary(value, *, owner):
    if value is None:
        return {'recorded': False, 'reason': 'No working-copy availability collection was retained for this run.'}
    try:
        if not isinstance(value, dict) or value['status'] not in {'partial', 'recorded', 'unavailable'}:
            raise ValueError('unknown collection status')
        metrics = dict(METRICS)
        if 'inventory_observations' in value:
            metrics['inventory_observations'] = 'Instruction ownership inventories'
            if not isinstance(value.get('inventory_ids'), list) or len(value['inventory_ids']) != value['inventory_observations']:
                raise ValueError('inventory IDs do not reconcile with observations')
        counts = counter_map({key: value[key] for key in metrics}, owner)
        outcomes = counter_map(value['outcomes'], owner+'.outcomes')
        causes = counter_map(value['causes'], owner+'.causes')
        if set(outcomes) - {'available', 'changed', 'absent', 'unknown'} or sum(outcomes.values()) != counts['observations']:
            raise ValueError('outcomes do not reconcile with observations')
        if not isinstance(value['errors'], list): raise ValueError('errors are not a list')
        if not isinstance(value['observed_at'], str): raise ValueError('missing observation timestamp')
    except (KeyError, TypeError, ValueError) as exc:
        raise AvailabilityError(owner+': invalid collection results: '+str(exc)) from exc
    return {'recorded': True, 'status': value['status'], 'observed_at': value['observed_at'],
            'metrics': [{'key': key, 'label': label, 'count': counts[key]} for key, label in metrics.items()],
            'outcomes': outcomes, 'causes': causes, 'errors': value['errors'],
            'meaning': 'Checks compare delivered content with actual working-copy files. They do not prove that a session loaded instructions.'}
