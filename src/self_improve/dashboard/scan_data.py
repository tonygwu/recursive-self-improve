"""Read-only dashboard adapter for the scanner's physical-line measurement.

The caller supplies the Store, clock and read transaction. No transcript or
working-copy inspection occurs here; options describe recorded observations.
"""
from datetime import datetime, timedelta, timezone

from .. import scan_observations as scans


class ExposureRequestError(ValueError):
    """An invalid interval or selector, distinct from damaged stored evidence."""


REASONS = {
    "": "",
    "missing_observations": "No physical-line observations are recorded for this scope.",
    "uncovered_version": "The selected version has no observations in this scope.",
    "incompatible_versions": "Several detector or parser configurations are present. Select one version; their counts cannot be pooled.",
    "unknown_version": "The detector or parser version is unknown. Raw counts are available, but a comparable rate is not.",
    "zero_eligible_exposure": "No eligible timestamped physical lines fall in this interval.",
    "schema_unavailable": "This database predates scan observations. Upgrade it explicitly and collect observations before measuring exposure.",
    "clock_unavailable": "No reference time was supplied for the 30-day measurement.",
}


def project_exposure(store, *, project_key, now_utc=None, start=None, end=None,
                     compatibility_key=None, working_copy_id=None, signal_types=None,
                     include_options=False):
    if not isinstance(project_key, str) or not project_key:
        raise ExposureRequestError("project_key must be a non-empty canonical project identity")
    if (start is None) != (end is None):
        raise ExposureRequestError("Supply both start and end, or neither for the last 30 days")
    if start is None:
        if now_utc is None:
            return _missing("clock_unavailable", project_key)
        if not isinstance(now_utc, datetime) or now_utc.tzinfo is None:
            raise ExposureRequestError("now_utc must be a timezone-aware datetime")
        end = now_utc.astimezone(timezone.utc).isoformat()
        start = (now_utc.astimezone(timezone.utc) - timedelta(days=30)).isoformat()
    start, end = scans.normalize_timestamp(start), scans.normalize_timestamp(end)
    if start is None or end is None or start >= end:
        raise ExposureRequestError("start and end must be zoned ISO timestamps with start < end")
    for name, value in (("compatibility_key", compatibility_key), ("working_copy_id", working_copy_id)):
        if value is not None and (not isinstance(value, str) or not value):
            raise ExposureRequestError(f"{name} must be non-empty when supplied")
    if signal_types is not None and (
        not isinstance(signal_types, tuple) or not signal_types
        or any(s not in scans.SIGNALS for s in signal_types)
    ):
        raise ExposureRequestError(f"signal_types must select from {', '.join(scans.SIGNALS)}")
    requested = dict(project_key=project_key, start=start, end=end,
                     compatibility_key=compatibility_key, working_copy_id=working_copy_id,
                     signal_types=list(signal_types or scans.SIGNALS))
    if not store.query_one("SELECT name FROM schema_migrations WHERE name = ?", (scans.MIGRATION,)):
        return _missing("schema_unavailable", project_key, requested)
    # Applied-but-damaged schemas and manifests propagate as named data errors.
    result = scans.exposure_window(store, project_key=project_key, start=start, end=end,
                                   compatibility_key=compatibility_key,
                                   working_copy_id=working_copy_id, signal_types=signal_types)
    result["reason_text"] = REASONS[result["reason"]]
    if include_options:
        result["options"] = {
            "signal_types": list(scans.SIGNALS),
            "working_copies": store.query(
                "SELECT id, normalized_path FROM scan_working_copies WHERE project_key = ? "
                "ORDER BY normalized_path, id", (project_key,)),
        }
    else:
        # The project table needs a compact summary. Detail keeps the verified
        # settings and module identities so a version hash is inspectable.
        for group in result["version_groups"]:
            group.pop("manifests", None)
    return result


def _missing(reason, project_key, requested=None):
    return {"computable": False, "reason": reason, "reason_text": REASONS[reason],
            "requested": requested or {"project_key": project_key},
            "occurrences": None, "eligible_lines": None, "rate_per_100k": None,
            "version_groups": [], "coverage": None}


class ScanHistoryRequestError(ValueError):
    """An invalid history selector or pagination request."""


HISTORY_REASONS = {
    '': '',
    'no_observations': 'No scan observations were retained for this selection. Older scans may have no observation history.',
    'incident_not_found': 'This incident is no longer retained.',
    'legacy_unknown_provenance': 'The producing detector observation is unknown. No scan links were retained for this incident.',
    'schema_unavailable': 'This database predates scan observation history. Upgrade it explicitly; history cannot be inferred from current detectors.',
}


def history_page(store, *, incident_id=None, run_id=None, limit=20, cursor=None):
    try:
        result = scans.scan_history(store, incident_id=incident_id, run_id=run_id, limit=limit, cursor=cursor)
    except ValueError as exc:
        raise ScanHistoryRequestError(str(exc)) from exc
    except scans.ScanObservationError:
        if store.query_one('SELECT name FROM schema_migrations WHERE name=?', (scans.MIGRATION,)):
            raise
        # Request validation runs before require_schema in the public reader.
        result = {'selector': {'incident_id': incident_id} if incident_id is not None else {'run_id': run_id},
                  'records': [], 'next_cursor': None, 'count': None, 'computable': False,
                  'reason': 'schema_unavailable'}
    result['reason_text'] = HISTORY_REASONS[result['reason']]
    return result
