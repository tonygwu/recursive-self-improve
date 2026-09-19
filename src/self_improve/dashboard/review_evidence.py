"""Read-only identity labels for exact selected Review evidence, outside authority."""
from ..commands import CommandError
from ..mining_history import digest
from . import evidence_identity

PROFILE = 'review-evidence/1'


def enrich(store, preview):
    """Use the caller's selected-preview read transaction; never mutate its members."""
    sources = {}
    for member in preview['members']:
        for incident in member['snapshot']['evidence']:
            incident_id = incident['id']
            if incident_id in sources and sources[incident_id] != incident:
                raise CommandError('CommandDataError', 'Selected incident snapshots disagree.', 500)
            sources[incident_id] = incident
    index = evidence_identity.metadata_index(store) if sources else None
    records = [{'incident_id': incident_id, 'source_revision': digest(incident),
                'identity': evidence_identity.identity(index, incident)}
               for incident_id, incident in sorted(sources.items())]
    return {**preview, 'evidence_identity': {'profile': PROFILE,
            'preview_revision': preview['revision'], 'records': records}}
