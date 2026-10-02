"""명시된 계좌 평가·현금흐름의 조건부 산술. 원천 인증/매매 기능은 없다."""
from datetime import datetime, timezone
from decimal import Decimal, DecimalException, localcontext
import re

SCHEMA = 'account-net-return-v1'
BENCHMARKS = {'KODEX200', 'KODEX200_EX_SEMICONDUCTORS'}
PORTFOLIO_FIELDS = set(('source_ref source_sha256 declared_complete start end income_in_equity '
                        'trading_costs_in_equity cashflows external_operating_costs').split())
FLAGS = ('declared_complete', 'income_in_equity', 'trading_costs_in_equity')
FLOW_SIGNS = {'deposit': 1, 'transfer_in': 1, 'withdrawal': -1, 'transfer_out': -1}


def _fields(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        raise ValueError('입력 필드 불일치')
    return value


def _text(value):
    if type(value) is not str or not value.strip() or len(value) > 200:
        raise ValueError('유한 참조/ID 필요')
    return value


def _at(value):
    if type(value) is not str or len(value) > 64:
        raise ValueError('시각 문자열 필요')
    at = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError('시간대 필요')
    try:
        return at.astimezone(timezone.utc)
    except OverflowError as exc:
        raise ValueError('지원 시각 범위 초과') from exc


def _money(value, *, positive=False):
    if type(value) is not str or re.fullmatch(r'[+-]?[0-9]{1,24}(?:\.[0-9]{1,8})?', value) is None:
        raise ValueError('유한 십진 금액 문자열 필요')
    amount = Decimal(value)
    if positive and amount <= 0:
        raise ValueError('양수 금액 필요')
    return amount


def _rows(value):
    if type(value) is not list or len(value) > 10000:
        raise ValueError('행 한도 위반')
    return value


def _portfolio(value, as_of):
    p = _fields(value, PORTFOLIO_FIELDS)
    _text(p['source_ref'])
    if type(p['source_sha256']) is not str or re.fullmatch('[0-9a-f]{64}', p['source_sha256']) is None:
        raise ValueError('원천 지문 필요')
    if any(type(p[k]) is not bool for k in FLAGS):
        raise ValueError('완전성/포함 선언 필요')
    endpoints = []
    for name in ('start', 'end'):
        row = _fields(p[name], {'at', 'equity'})
        endpoints.append((_at(row['at']), _money(row['equity'])))
    (start_at, start), (end_at, end) = endpoints
    if not start_at < end_at <= as_of:
        raise ValueError('평가/보고 시각 역전')
    ids = set(); flows = []; costs = []; previous = start_at
    for row in _rows(p['cashflows']):
        _fields(row, {'id', 'at', 'kind', 'amount', 'pre_equity', 'post_equity'})
        identity = _text(row['id']); at = _at(row['at'])
        if identity in ids or not previous < at < end_at:
            raise ValueError('현금흐름 중복/시간순/범위 불일치')
        ids.add(identity); previous = at
        if type(row['kind']) is not str or row['kind'] not in FLOW_SIGNS:
            raise ValueError('외부 현금흐름 종류 불일치')
        signed = _money(row['amount'], positive=True) * FLOW_SIGNS[row['kind']]
        pre, post = row['pre_equity'], row['post_equity']
        if (pre is None) != (post is None):
            raise ValueError('입출금 전후 평가 쌍 필요')
        if pre is not None:
            pre, post = _money(pre), _money(post)
            if post != pre + signed:
                raise ValueError('입출금 전후 평가 모순')
        flows.append((at, row['kind'], signed, pre, post))
    for row in _rows(p['external_operating_costs']):
        _fields(row, {'id', 'at', 'amount', 'allocation_ref'})
        identity = _text(row['id']); at = _at(row['at']); _text(row['allocation_ref'])
        if identity in ids or not start_at <= at <= end_at:
            raise ValueError('운영비 중복/기간 불일치')
        ids.add(identity); costs.append(_money(row['amount'], positive=True))
    return dict(start_at=start_at, end_at=end_at, start=start, end=end, flows=flows,
                costs=costs, missing=[k for k in FLAGS if not p[k]],
                source_ref=p['source_ref'], source_sha256=p['source_sha256'])


def _num(value):
    return None if value is None else str(value.normalize())


def _calculate(p):
    missing = p['missing']
    net_flow = sum((r[2] for r in p['flows']), Decimal(0))
    cost = sum(p['costs'], Decimal(0))
    gain = p['end'] - p['start'] - net_flow
    twr = None; twr_reasons = []
    if missing:
        twr_reasons.append('incomplete_portfolio_evidence')
    else:
        if any(r[3] is None for r in p['flows']):
            twr_reasons.append('missing_flow_valuation')
        if (p['start'] <= 0 or p['end'] < 0
                or any(r[3] is not None and (r[3] < 0 or r[4] <= 0) for r in p['flows'])):
            twr_reasons.append('nonpositive_denominator_or_negative_nav')
        if not twr_reasons:
            try:
                factor = Decimal(1); base = p['start']
                for _, _, _, pre, post in p['flows']:
                    factor *= pre / base
                    base = post
                twr = factor * (p['end'] / base) - 1
            except DecimalException:
                twr_reasons.append('numeric_range_exceeded')
    return dict(source_ref=p['source_ref'], source_sha256=p['source_sha256'],
        calculation_basis='provisional_declared_evidence',
        net_external_flow=_num(net_flow) if not missing else None,
        gain_before_external_operating_costs=_num(gain) if not missing else None,
        external_operating_costs=_num(cost) if not missing else None,
        net_gain=_num(gain - cost) if not missing else None,
        twr_embedded_costs=_num(twr), all_costs_twr=_num(twr) if cost == 0 else None,
        unavailable_reasons=list(missing), twr_unavailable_reasons=twr_reasons,
        twr_basis='income_and_trading_costs_embedded_in_nav',
        external_costs_in_twr=False, decimal_precision=50)


def _difference(a, b):
    return None if a is None or b is None else _num(Decimal(a) - Decimal(b))


def build_account_report(payload):
    """입출금 전후 평가를 연결한다. 서로 다른 벤치마크 ID/평가액은 대조하지 않는다."""
    with localcontext() as context:
        context.prec = 50
        p = _fields(payload, {'schema_version', 'dataset_kind', 'as_of', 'currency',
                              'tax_basis_ref', 'account', 'benchmarks'})
        if (p['schema_version'] != SCHEMA or p['dataset_kind'] not in ('synthetic', 'account_export')
                or p['currency'] != 'KRW'):
            raise ValueError('계좌 자료 계약 불일치')
        _text(p['tax_basis_ref']); as_of = _at(p['as_of'])
        account = _portfolio(p['account'], as_of); result = _calculate(account)
        if type(p['benchmarks']) is not list or len(p['benchmarks']) > 2:
            raise ValueError('벤치마크 한도 위반')
        comparisons = []; names = set()
        for item in p['benchmarks']:
            _fields(item, {'name', 'methodology_ref', 'portfolio'})
            name = _text(item['name']); _text(item['methodology_ref'])
            if name not in BENCHMARKS or name in names:
                raise ValueError('벤치마크 종류/중복 불일치')
            names.add(name); benchmark = _portfolio(item['portfolio'], as_of)
            if (any(benchmark[k] != account[k] for k in ('start_at', 'end_at', 'start'))
                    or [r[:3] for r in benchmark['flows']] != [r[:3] for r in account['flows']]):
                raise ValueError('벤치마크 기간/초기자본/현금흐름 불일치')
            metrics = _calculate(benchmark)
            comparisons.append(dict(name=name, methodology_ref=item['methodology_ref'], portfolio=metrics,
                net_gain_difference=_difference(result['net_gain'], metrics['net_gain']),
                twr_difference=_difference(result['twr_embedded_costs'], metrics['twr_embedded_costs']),
                twr_difference_basis='embedded_costs_only'))
        return dict(schema_version='account-net-return-report-v1', dataset_kind=p['dataset_kind'],
            as_of=as_of.isoformat(), currency='KRW', tax_basis_ref=p['tax_basis_ref'],
            start_at=account['start_at'].isoformat(), end_at=account['end_at'].isoformat(),
            account=result, benchmarks=comparisons, benchmark_basis='matched_supplied_portfolios',
            source_authenticity_verified=False, engine_attribution_verified=False, production_eligible=False)
