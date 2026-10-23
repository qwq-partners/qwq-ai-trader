"""보고 전용 JSONL 계약. I/O·자격·authority를 소유하지 않는다."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from zoneinfo import ZoneInfo

from .market_types import Quote

REQUEST_MAX_BYTES = 16384
RESPONSE_MAX_BYTES = 65536
MAX_SYMBOLS = 20
MAX_AGE_SECONDS = 60
REQUEST_TIMEOUT_SECONDS = 45.0
PURPOSE = 'daily_report_current_price'
KST = ZoneInfo('Asia/Seoul')


class ProtocolError(ValueError):
    def __init__(self):
        super().__init__('invalid_report_protocol')


def _keys(value, names):
    if type(value) is not dict or set(value) != set(names.split()):
        raise ProtocolError()


def _aware(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ProtocolError()
    return value.astimezone(KST)


def _stamp(value):
    if type(value) is not str or not 1 <= len(value) <= 40:
        raise ProtocolError()
    try:
        return _aware(datetime.fromisoformat(value))
    except (ValueError, OverflowError):
        raise ProtocolError() from None


def canonical_bytes(document):
    try:
        return json.dumps(document, sort_keys=True, separators=(',', ':'),
                          ensure_ascii=True, allow_nan=False).encode('ascii')
    except (ValueError, TypeError, RecursionError):
        raise ProtocolError() from None


def strict_loads(raw, *, maximum):
    if type(raw) is not bytes or not 0 < len(raw) <= maximum:
        raise ProtocolError()
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ProtocolError()
            result[key] = value
        return result
    def invalid(_):
        raise ProtocolError()
    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ProtocolError() from None
    if type(value) is not dict:
        raise ProtocolError()
    return value


@dataclass(frozen=True)
class ReportCohort:
    report_date: date
    generated_at: datetime
    members: tuple[tuple[int, str], ...]
    cohort_id: str

    @classmethod
    def create(cls, report_date, generated_at, members):
        stamp = _aware(generated_at)
        if (type(report_date) is not date or stamp.date() != report_date
                or type(members) not in (tuple, list) or not 1 <= len(members) <= MAX_SYMBOLS):
            raise ProtocolError()
        normalized, seen, last = [], set(), 0
        for item in members:
            if type(item) not in (tuple, list) or len(item) != 2:
                raise ProtocolError()
            rank, symbol = item
            if (type(rank) is not int or not last < rank <= 10000 or type(symbol) is not str
                    or re.fullmatch('[0-9]{6}', symbol) is None or symbol in seen):
                raise ProtocolError()
            normalized.append((rank, symbol))
            seen.add(symbol)
            last = rank
        content = dict(report_date=report_date.isoformat(), generated_at=stamp.isoformat(),
                       members=[dict(rank=r, symbol=s) for r, s in normalized])
        return cls(report_date, stamp, tuple(normalized), hashlib.sha256(canonical_bytes(content)).hexdigest())

    def to_document(self):
        return dict(report_date=self.report_date.isoformat(), generated_at=self.generated_at.isoformat(),
                    members=[dict(rank=r, symbol=s) for r, s in self.members], cohort_id=self.cohort_id)

    @classmethod
    def from_document(cls, document):
        _keys(document, 'report_date generated_at members cohort_id')
        try:
            day = document['report_date']
            if type(day) is not str or re.fullmatch(r'\d{4}-\d{2}-\d{2}', day) is None:
                raise ProtocolError()
            members = document['members']
            if type(members) is not list or not 1 <= len(members) <= MAX_SYMBOLS:
                raise ProtocolError()
            for member in members:
                _keys(member, 'rank symbol')
            result = cls.create(date.fromisoformat(day), _stamp(document['generated_at']),
                                tuple((m['rank'], m['symbol']) for m in members))
            if document != result.to_document():
                raise ProtocolError()
            return result
        except (ValueError, TypeError, KeyError, OverflowError):
            raise ProtocolError() from None


def make_request(cohort, *, request_id, now):
    if type(cohort) is not ReportCohort:
        raise ProtocolError()
    document = dict(schema_version=1, purpose=PURPOSE, request_id=request_id,
                    requested_at=_aware(now).isoformat(), cohort=cohort.to_document())
    validate_request(document, now=now)
    return document


def validate_request(document, *, now, policy=None):
    _keys(document, 'schema_version purpose request_id requested_at cohort')
    if (type(document['schema_version']) is not int or document['schema_version'] != 1
            or document['purpose'] != PURPOSE or type(document['request_id']) is not str
            or re.fullmatch('[0-9a-f]{32}', document['request_id']) is None):
        raise ProtocolError()
    local, requested = _aware(now), _stamp(document['requested_at'])
    cohort = ReportCohort.from_document(document['cohort'])
    if (local.date() != cohort.report_date or requested.date() != cohort.report_date
            or not cohort.generated_at <= requested <= local
            or (local - requested).total_seconds() > REQUEST_TIMEOUT_SECONDS):
        raise ProtocolError()
    if policy is not None:
        try:
            if (policy['schema_version'] != 3 or policy['report_display']['purpose'] != PURPOSE
                    or cohort.report_date.isoformat() not in policy['dates']
                    or not '16:00' <= requested.strftime('%H:%M') <= local.strftime('%H:%M') < '16:05'
                    or not any(s['start'] <= requested.strftime('%H:%M') <= local.strftime('%H:%M') < s['end']
                               for s in policy['sessions']) or len(cohort.members) > policy['selection']['max_symbols']):
                raise ProtocolError()
        except (KeyError, TypeError):
            raise ProtocolError() from None
    return cohort


def report_snapshot(cohort, *, now, policy):
    from .observation import _cohort
    verified = ReportCohort.from_document(cohort.to_document())
    stamp = _aware(now)
    if stamp.date() != verified.report_date or stamp < verified.generated_at:
        raise ProtocolError()
    return dict(snapshot_id=verified.cohort_id, selected_at=stamp.isoformat(),
                source_success_at=verified.generated_at.isoformat(), selection_partial=False,
                symbols=tuple(s for _, s in verified.members), kis={}, cohort=_cohort(policy, stamp))


def make_result(request, *, quotes, valid_until, status='ok'):
    if status not in {'ok', 'unavailable'}:
        raise ProtocolError()
    values = []
    for quote in quotes:
        if type(quote) is not Quote:
            raise ProtocolError()
        values.append(dict(symbol=quote.symbol, price=str(quote.price),
            observed_at=_aware(quote.observed_at).isoformat(), fetched_at=_aware(quote.fetched_at).isoformat(),
            market_basis=quote.market_basis, currency=quote.currency))
    if (status == 'unavailable' and (values or valid_until is not None)) or (status == 'ok' and not values):
        raise ProtocolError()
    return dict(schema_version=1, request_id=request['request_id'], cohort_id=request['cohort']['cohort_id'],
                status=status, valid_until=None if valid_until is None else _aware(valid_until).isoformat(), quotes=values)


def validate_result(document, *, request, now):
    _keys(document, 'schema_version request_id cohort_id status valid_until quotes')
    cohort = validate_request(request, now=now)
    local = _aware(now)
    if (type(document['schema_version']) is not int or document['schema_version'] != 1
            or document['request_id'] != request['request_id'] or document['cohort_id'] != cohort.cohort_id
            or document['status'] not in ('ok', 'unavailable') or type(document['quotes']) is not list):
        raise ProtocolError()
    if document['status'] == 'unavailable':
        if document['quotes'] or document['valid_until'] is not None:
            raise ProtocolError()
        return {}, None
    if not 1 <= len(document['quotes']) <= len(cohort.members):
        raise ProtocolError()
    end = _stamp(document['valid_until'])
    if not local < end <= local.replace(hour=16, minute=5, second=0, microsecond=0):
        raise ProtocolError()
    symbols, values = {s for _, s in cohort.members}, {}
    for item in document['quotes']:
        _keys(item, 'symbol price observed_at fetched_at market_basis currency')
        symbol = item['symbol']
        if (type(symbol) is not str or symbol not in symbols or symbol in values
                or type(item['price']) is not str or len(item['price']) > 128
                or item['market_basis'] not in ('unknown', 'krx', 'krx_nxt') or item['currency'] != 'KRW'):
            raise ProtocolError()
        try:
            price = Decimal(item['price'])
            if (not price.is_finite() or price <= 0 or abs(price.adjusted()) > 30
                    or abs(price.as_tuple().exponent) > 30):
                raise ProtocolError()
        except InvalidOperation:
            raise ProtocolError() from None
        observed, fetched = _stamp(item['observed_at']), _stamp(item['fetched_at'])
        if (not observed <= fetched <= local or (local - observed).total_seconds() > MAX_AGE_SECONDS
                or end > observed + timedelta(seconds=MAX_AGE_SECONDS)):
            raise ProtocolError()
        values[symbol] = Quote(symbol, price, observed, fetched, 'ok',
            frozenset({'market_basis'}) if item['market_basis'] == 'unknown' else frozenset(),
            item['market_basis'], 'KRW')
    return values, end
