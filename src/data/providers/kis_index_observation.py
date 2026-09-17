"""Index REST field provenance, not a verified market timestamp or trading permit."""
from __future__ import annotations

from datetime import datetime
import math
from uuid import uuid4


INDEX_FIELDS = {
    'price': 'bstp_nmix_prpr', 'open': 'bstp_nmix_oprc',
    'high': 'bstp_nmix_hgpr', 'low': 'bstp_nmix_lwpr',
    'change': 'bstp_nmix_prdy_vrss', 'change_pct': 'bstp_nmix_prdy_ctrt',
}


def _field(output, key):
    if key not in output or output[key] is None or output[key] == '':
        return {'status': 'missing', 'value': None}
    value = output[key]
    if type(value) not in (int, float, str):  # bool must not become 0.0/1.0
        return {'status': 'invalid', 'value': None}
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return {'status': 'invalid', 'value': None}
    if not math.isfinite(number):
        return {'status': 'invalid', 'value': None}
    # Match the existing consumer value/rounding, not a new classifier policy.
    return {'status': 'valid', 'value': round(number, 2)}


def build_index_observation(output, index_code, *, received_at):
    if type(output) is not dict or type(index_code) is not str or not index_code:
        raise ValueError('invalid_index_observation_input')
    if type(received_at) is not datetime or received_at.utcoffset() is None:
        raise ValueError('aware_index_receipt_required')
    return {'schema_version': 1, 'source': 'kis', 'source_tr': 'FHPUP02100000',
            'index_code': index_code, 'observation_id': str(uuid4()),
            'received_at': received_at.isoformat(), 'market_as_of': None,
            'fields': {name: _field(output, key) for name, key in INDEX_FIELDS.items()}}
