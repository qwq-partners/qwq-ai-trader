"""Published-token ceiling for one narrowly certified Gemini request.

The caller attests text-only Standard generateContent, one candidate, no tools,
explicit cache or continuation. maxOutputTokens includes thinking. References:
https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-lite
https://ai.google.dev/gemini-api/docs/generate-content/thinking
https://ai.google.dev/gemini-api/docs/pricing

Certificates preserve admission-time rates; later pricing updates never reduce
an unresolved liability. The digest detects accidental certificate corruption,
not a malicious actor capable of rewriting the financial database.
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation, ROUND_CEILING

GEMINI_TIMEOUT_CONTRACT = 'gemini-flash-lite-standard-text-v1'
GEMINI_TIMEOUT_MODEL = 'gemini-3.1-flash-lite'
GEMINI_INPUT_LIMIT = 1_048_576
GEMINI_OUTPUT_LIMIT = 65_536
_SCALE = 1_000_000_000
_FIELDS = frozenset({'contract', 'token', 'model', 'month', 'created_at', 'input_tokens',
                     'max_output_tokens', 'input_rate', 'output_rate', 'basis', 'ceiling_units'})


def valid_request(model, max_output_tokens, contract):
    return (contract == GEMINI_TIMEOUT_CONTRACT and model == GEMINI_TIMEOUT_MODEL
            and type(max_output_tokens) is int and 1 <= max_output_tokens <= GEMINI_OUTPUT_LIMIT)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _ceiling(input_rate, output_rate, cap):
    try:
        rates = [Decimal(value) for value in (input_rate, output_rate)]
        if any(not rate.is_finite() or rate <= 0 for rate in rates):
            raise ValueError('invalid_timeout_certificate')
        units = int(((GEMINI_INPUT_LIMIT * rates[0] + cap * rates[1]) *
                     (_SCALE // 1_000_000)).to_integral_value(rounding=ROUND_CEILING))
        if not 0 < units < 2 ** 62:
            raise ValueError('invalid_timeout_certificate')
        return units
    except (InvalidOperation, OverflowError):
        raise ValueError('invalid_timeout_certificate') from None


def make_certificate(token, model, cap, now, quote):
    """Build only from a rate_quote obtained inside the admission transaction."""
    input_rate, output_rate = str(quote.input_usd_per_million), str(quote.output_usd_per_million)
    units = _ceiling(input_rate, output_rate, cap)
    data = {'contract': GEMINI_TIMEOUT_CONTRACT, 'token': token, 'model': model,
            'month': now.strftime('%Y-%m'), 'created_at': now.isoformat(),
            'input_tokens': GEMINI_INPUT_LIMIT, 'max_output_tokens': cap,
            'input_rate': input_rate, 'output_rate': output_rate, 'basis': quote.basis,
            'ceiling_units': units}
    digest = hashlib.sha256(_canonical(data).encode()).hexdigest()
    return units, _canonical({'data': data, 'sha256': digest})


def validate_certificate(bound, reservation):
    """Return the certified ceiling, raising ValueError on damaged evidence."""
    try:
        if (type(bound['held']) is not int or bound['held'] not in (0, 1)
                or type(bound['ceiling_units']) is not int
                or not isinstance(bound['certificate'], str)):
            raise ValueError
        envelope = json.loads(bound['certificate'])
        if (not isinstance(envelope, dict) or set(envelope) != {'data', 'sha256'}
                or _canonical(envelope) != bound['certificate']):
            raise ValueError
        data = envelope['data']
        if not isinstance(data, dict) or set(data) != _FIELDS:
            raise ValueError
        if hashlib.sha256(_canonical(data).encode()).hexdigest() != envelope['sha256']:
            raise ValueError
        if not valid_request(data['model'], data['max_output_tokens'], data['contract']):
            raise ValueError
        if type(data['input_tokens']) is not int or data['input_tokens'] != GEMINI_INPUT_LIMIT:
            raise ValueError
        for field in ('token', 'model', 'month', 'created_at'):
            if data[field] != reservation[field]:
                raise ValueError
        if bound['token'] != reservation['token']:
            raise ValueError
        if any(not isinstance(data[key], str) or not data[key] for key in ('input_rate', 'output_rate', 'basis')):
            raise ValueError
        units = _ceiling(data['input_rate'], data['output_rate'], data['max_output_tokens'])
        if (type(data['ceiling_units']) is not int or type(reservation['reserved_units']) is not int
                or units != data['ceiling_units'] or units != bound['ceiling_units']
                or units != reservation['reserved_units']):
            raise ValueError
        return units
    except (KeyError, TypeError, ValueError, OverflowError, RecursionError):
        raise ValueError('invalid_timeout_certificate') from None


def initialize_bounds(db):
    """Add only an auxiliary table; legacy eight-column inserts stay valid."""
    db.execute('''CREATE TABLE IF NOT EXISTS timeout_bounds (
        token TEXT PRIMARY KEY, certificate TEXT NOT NULL,
        ceiling_units INTEGER NOT NULL CHECK(ceiling_units>0),
        held INTEGER NOT NULL CHECK(held IN (0,1)))''')
    validate_schema(db)


def validate_schema(db):
    columns = db.execute('PRAGMA table_info(timeout_bounds)').fetchall()
    expected = [('token', 'TEXT', 0, 1), ('certificate', 'TEXT', 1, 0),
                ('ceiling_units', 'INTEGER', 1, 0), ('held', 'INTEGER', 1, 0)]
    if [(row['name'], row['type'], row['notnull'], row['pk']) for row in columns] != expected:
        raise ValueError('invalid_timeout_bounds_schema')
