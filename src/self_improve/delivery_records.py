"""Validate persisted execution records without importing or invoking the writer."""
import hashlib
import json

from .propose import PatchConflict, apply_unified_diff


class DeliveryRecordError(ValueError):
    pass


def delivery_fingerprint(delivery):
    return hashlib.sha256(json.dumps(delivery, sort_keys=True, separators=(',', ':'),
                                    ensure_ascii=False, allow_nan=False).encode('utf-8')).hexdigest()


def validated_cancellation(checkpoint, state):
    """A cancelled checkpoint releases its lane only after reconciliation."""
    if 'cancellation' not in checkpoint:
        return False
    record = checkpoint['cancellation']
    if (state != 'cancelled' or not isinstance(record, dict)
            or record.get('no_delivery_observed') is not True
            or not isinstance(record.get('at'), str) or not record['at']):
        raise DeliveryRecordError('invalid cancellation checkpoint')
    return True


def validated_delivery(checkpoint, diff, *, inverse_source=None):
    delivery = checkpoint.get('delivery')
    try:
        version = 2 if inverse_source is not None else 1
        if (not isinstance(delivery, dict) or delivery.get('version') != version
                or delivery_fingerprint(delivery) != checkpoint.get('delivery_hash')):
            raise ValueError('record fingerprint changed')
        if (type(delivery['before_exists']) is not bool or type(delivery['branch_exists']) is not bool
                or not isinstance(delivery['snapshot_before'], str) or not delivery['snapshot_before']
                or not isinstance(delivery['branch_commit'], str) or not isinstance(delivery['base_ref'], str)):
            raise ValueError('invalid record shape')
        for name in ('before', 'after'):
            content = delivery[name + '_content']
            if not isinstance(content, str) or hashlib.sha256(content.encode('utf-8')).hexdigest() != delivery[name + '_hash']:
                raise ValueError(name + ' content fingerprint changed')
        if inverse_source is None:
            expected = apply_unified_diff(delivery['before_content'], diff)
        else:
            from .rollback import reverse_applied_change
            expected = reverse_applied_change(inverse_source['before'], inverse_source['applied'], delivery['before_content'])
            if (type(delivery['after_exists']) is not bool
                    or delivery['after_exists'] != (bool(expected) or inverse_source['before_exists'])):
                raise ValueError('invalid inverse file presence')
        if expected != delivery['after_content']:
            raise ValueError('record does not describe the approved edit')
        preview = checkpoint.get('review_preview')
        if preview and any(delivery[k] != preview[k] for k in ('before_hash', 'after_hash', 'before_exists')):
            raise ValueError('record differs from the approved preview')
    except (KeyError, TypeError, ValueError, PatchConflict) as exc:
        raise DeliveryRecordError(f'invalid execution checkpoint: {exc}') from exc
    return delivery
