"""Summarize recorded loading paths without opening files or estimating tokens."""
from collections import defaultdict


def summarize(files):
    """Partition each provider's physical bytes by its broadest eligible path.

    Global/project rows may overlap through imports. Provider totals deduplicate
    them. These are candidates under the recorded profile, not session receipt.
    """
    groups = defaultdict(lambda: defaultdict(list))
    for file in files:
        for path in file['loading_paths']:
            for origin in ('all', path['origin']):
                groups[(path['provider'], origin)][file['real_path']].append((file, path))
    rows = []
    for (provider, origin), physical in sorted(groups.items()):
        row = dict(provider=provider, origin=origin, files=len(physical), observed_bytes=0,
                   startup_bytes=0, conditional_bytes=0, on_demand_bytes=0, unresolved_bytes=0)
        for paths in physical.values():
            size = paths[0][0]['bytes']
            amounts = {'startup': 0, 'conditional': 0, 'on_demand': 0}
            for _, path in paths:
                kind = path['scope']['kind']
                group = 'on_demand' if kind == 'on_demand' else 'conditional' if kind == 'path_scoped' or path['conditions'] else 'startup'
                amounts[group] = max(amounts[group], path['eligible_prefix_bytes'])
            covered = 0
            for group, prefix in amounts.items():
                row[group+'_bytes'] += max(0, prefix-covered)
                covered = max(covered, prefix)
            row['observed_bytes'] += size
            row['unresolved_bytes'] += size-covered
        rows.append(row)
    has_fields = any(path['source'] == 'embedded_memory' for file in files for path in file['loading_paths'])
    return {'version': 1, 'groups': rows, 'runtime_loading_verified': False,
            'meaning': ('Distinct file and embedded-field bytes under the recorded source profile. Embedded fields have unresolved runtime selection. ' if has_fields else 'Physical bytes under the recorded source profile. ') + 'Startup candidates, conditional rules and on-demand bodies are separate. Skill metadata budgets, runtime overrides and session receipt are not measured.'}


def project_summary(store, *, project_key, _revisions=None):
    """Latest inventory of the first copy in stable inventory-reader order."""
    from .instruction_inventory import _project_inventory
    page = _project_inventory(store, project_key=project_key, limit=1, revisions=_revisions)
    if not page['records']:
        return {'computable': False, 'reason': page['reason']}
    record = page['records'][0]
    base = {k: record[k] for k in ('working_copy_id', 'working_copy', 'observed_at', 'status')}
    if record['status'] == 'conflicting':
        return {**base, 'computable': False, 'reason': 'conflicting_inventory_observations'}
    base.update(inventory_id=record['id'], profile=record['profile'], total_bytes=record['totals']['bytes'],
                file_count=record['totals']['files'], copies=page['count'],
                selection='First copy in stable inventory order; latest observation for that copy. Inspect other copies in Project detail.')
    base['measurements'] = record.get('measurements')
    if 'context' not in record:
        return {**base, 'computable': False, 'reason': 'source_profile_upgrade_required'}
    if record['status'] == 'partial' and not record['files']:
        return {**base, 'computable': False, 'reason': 'incomplete_inventory_observation', 'issues': record['issues']}
    return {**base, **record['context'], 'computable': True, 'issues': record['issues']}


def project_summaries(store, *, project_keys):
    """Validate the shared revision archive once for a Projects read transaction."""
    from .instruction_inventory import MIGRATION
    from .rule_revisions import retained_revisions
    revisions = None
    if store.query_one('SELECT name FROM schema_migrations WHERE name=?', (MIGRATION,)):
        revisions = {r['id']:r for r in retained_revisions(store)}
    return {key: project_summary(store, project_key=key, _revisions=revisions) for key in project_keys}
