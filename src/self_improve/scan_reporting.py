"""Native scan measurement and cause labels shared by reports and the dashboard."""

MEASUREMENT_COUNTERS = {
    'observations_recorded': 'Scan observations recorded',
    'projections_unavailable': 'Observations without a measurable projection',
    'observation_failures_recorded': 'Failed scan observations recorded',
    'full_reparses': 'Full transcript parse passes',
    'full_reparse_bytes': 'Bytes read by full transcript passes',
    'lines_observed': 'Physical line observations published (includes rescans)',
    'occurrences_observed': 'Occurrence observations published (includes rescans and fingerprints)',
    'incident_links': 'Incident links recorded',
    'incident_links_ambiguous': 'Ambiguous incident links left unassigned',
    'unchanged_unobserved': 'Unchanged files skipped without observations',
    'unchanged_uncovered_version': 'Unchanged files skipped without the current version',
}

COUNTER_MAPS = {
    'error_taxonomy': 'Scan failures by phase and cause',
    'malformed_by_cause': 'Malformed lines by cause',
    'unknown_line_types': 'Unknown line types',
    'skipped_line_types': 'Intentionally skipped line types',
    'detector_taxonomy': 'Detector omissions by cause',
    'dropped_by_cap': 'Mining candidates dropped by signal cap',
    'denylisted_by_substring': 'Denylist matches',
    'project_key_methods': 'Project identity methods',
    'incidents_by_signal': 'Mining incidents recorded by signal',
}


class ScanReportingError(ValueError):
    """Stored scan counters cannot be rendered without guessing."""


def counter_map(value, owner):
    if not isinstance(value, dict) or any(
        not isinstance(k, str) or not k or type(v) is not int or v < 0
        for k, v in value.items()
    ):
        raise ScanReportingError(f'{owner}: expected named nonnegative integer counters')
    return value


def validate_measurement(value, owner):
    value = counter_map(value, owner)
    unknown = set(value) - set(MEASUREMENT_COUNTERS)
    if unknown:
        raise ScanReportingError(f'{owner}: unrecognized measurement counters {sorted(unknown)}')
    return value


def summary(scan_stats, *, owner):
    if scan_stats is not None and not isinstance(scan_stats, dict):
        raise ScanReportingError(f'{owner}: scan statistics are not an object')
    stats = scan_stats or {}
    recorded = 'measurement' in stats
    counts = validate_measurement(stats['measurement'], owner + '.measurement') if recorded else {}
    return {
        'recorded': recorded,
        'reason': '' if recorded else 'No scan measurement counters were retained. This is unknown history, not zero activity.',
        'metrics': [{'key': key, 'label': label, 'count': counts.get(key, 0)}
                    for key, label in MEASUREMENT_COUNTERS.items()] if recorded else [],
        'counter_maps': [{
            'key': key, 'label': label, 'recorded': key in stats,
            'counts': counter_map(stats[key], owner + '.' + key) if key in stats else None,
        } for key, label in COUNTER_MAPS.items()],
        'meaning': 'These counters describe work in this scan run. Rescans can observe the same line or occurrence again; do not sum run counters as unique project exposure.',
    }
