"""보고 대상/원시각과 IPC의 불일치를 실제 parser에서 거부한다."""
from copy import deepcopy
from datetime import date, datetime, timedelta
from decimal import Decimal
import importlib.util
from zoneinfo import ZoneInfo

import pytest

KST = ZoneInfo('Asia/Seoul')
DAY = date(2026, 10, 26)
AT = datetime(2026, 10, 26, 16, 0, 3, tzinfo=KST)


def test_report_protocol_is_available():
    assert importlib.util.find_spec('src.data.providers.toss.report_protocol') is not None


def cohort():
    from src.data.providers.toss.report_protocol import ReportCohort
    return ReportCohort.create(DAY, AT.replace(hour=8, minute=25, second=0),
                               ((1, '000660'), (2, '005930')))


def request():
    from src.data.providers.toss.report_protocol import make_request
    return make_request(cohort(), request_id='a' * 32, now=AT)


def response():
    from src.data.providers.toss.market_types import Quote
    from src.data.providers.toss.report_protocol import make_result
    stamp = AT + timedelta(seconds=1)
    quote = Quote('000660', Decimal('123000'), stamp, stamp, 'ok',
                  frozenset({'market_basis'}), 'unknown', 'KRW')
    return make_result(request(), quotes=(quote,), valid_until=stamp + timedelta(seconds=60))


def test_cohort_round_trip_preserves_original_time_and_order():
    from src.data.providers.toss.report_protocol import ReportCohort
    original = cohort()
    restored = ReportCohort.from_document(original.to_document())
    assert restored == original
    assert restored.generated_at.hour == 8
    assert restored.members == ((1, '000660'), (2, '005930'))


@pytest.mark.parametrize('members', [(), ((1, '000660'), (2, '000660')),
    ((True, '000660'),), ((2, '000660'), (1, '005930')), ((1, '../etc'),),
    tuple((n + 1, f'{n:06d}') for n in range(21))])
def test_bad_or_oversized_cohort_is_not_silently_reselected(members):
    from src.data.providers.toss.report_protocol import ReportCohort, ProtocolError
    with pytest.raises(ProtocolError):
        ReportCohort.create(DAY, AT, members)


def test_cohort_hash_does_not_allow_replacing_a_member():
    from src.data.providers.toss.report_protocol import ReportCohort, ProtocolError
    doc = cohort().to_document()
    doc['members'][0]['symbol'] = '035420'
    with pytest.raises(ProtocolError):
        ReportCohort.from_document(doc)


@pytest.mark.parametrize('raw', [b'{"a":1,"a":2}', b'{"a":NaN}', b'[]', b'{', b' ' * 65])
def test_json_rejects_ambiguous_truncated_or_oversized_input(raw):
    from src.data.providers.toss.report_protocol import strict_loads, ProtocolError
    with pytest.raises(ProtocolError):
        strict_loads(raw, maximum=64)


def test_report_snapshot_does_not_replace_old_morning_cohort_with_holdings():
    from src.data.providers.toss.report_protocol import report_snapshot
    policy = {'dates': [DAY.isoformat()], 'sessions': [{'name': 'after', 'start': '16:00', 'end': '16:05'}],
              'selection': {'max_snapshot_age_seconds': 300, 'max_symbols': 20}, 'dataset_kind': 'live'}
    result = report_snapshot(cohort(), now=AT, policy=policy)
    assert result['symbols'] == ('000660', '005930')
    assert result['source_success_at'] == cohort().generated_at.isoformat()
    assert result['snapshot_id'] == cohort().cohort_id
    assert result['kis'] == {} and not result['selection_partial']
    assert 'selection_metadata' not in result


def test_valid_response_keeps_unknown_basis_and_original_timestamps():
    from src.data.providers.toss.report_protocol import validate_result
    values, end = validate_result(response(), request=request(), now=AT + timedelta(seconds=2))
    assert tuple(values) == ('000660',)
    assert values['000660'].market_basis == 'unknown'
    assert values['000660'].observed_at == AT + timedelta(seconds=1)
    assert end == AT + timedelta(seconds=61)


@pytest.mark.parametrize('mutation', ['request_id', 'cohort_id', 'symbol', 'duplicate',
    'nan', 'future', 'stale', 'currency', 'extended_lease', 'unknown_key'])
def test_mismatched_or_invalid_response_is_rejected(mutation):
    from src.data.providers.toss.report_protocol import validate_result, ProtocolError
    doc = deepcopy(response())
    q = doc['quotes'][0]
    if mutation in {'request_id', 'cohort_id'}: doc[mutation] = 'b' * len(doc[mutation])
    elif mutation == 'symbol': q['symbol'] = '035420'
    elif mutation == 'duplicate': doc['quotes'].append(deepcopy(q))
    elif mutation == 'nan': q['price'] = 'NaN'
    elif mutation == 'future': q['observed_at'] = (AT + timedelta(seconds=3)).isoformat()
    elif mutation == 'stale': q['observed_at'] = (AT - timedelta(seconds=61)).isoformat()
    elif mutation == 'currency': q['currency'] = 'USD'
    elif mutation == 'extended_lease': doc['valid_until'] = (AT + timedelta(seconds=120)).isoformat()
    else: doc['command'] = 'arbitrary'
    with pytest.raises(ProtocolError):
        validate_result(doc, request=request(), now=AT + timedelta(seconds=2))


@pytest.mark.parametrize('now', [AT - timedelta(seconds=1), AT + timedelta(seconds=46), AT + timedelta(days=1)])
def test_original_request_time_is_not_refreshed(now):
    from src.data.providers.toss.report_protocol import validate_request, ProtocolError
    with pytest.raises(ProtocolError):
        validate_request(request(), now=now)
