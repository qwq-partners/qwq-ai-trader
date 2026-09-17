"""같은 원관측의 보호·진입 가격 완료 증거. 거래 권한이나 전 관측 원장이 아니다."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from decimal import Decimal, InvalidOperation

from .protection_recovery import digest


def observation_market_data(observation, value):
    """별도 기술 지표가 같은 원관측의 OHLC를 바꾸지 못하게 한다."""
    if value is None:
        value = {}
    if type(value) is not dict:
        raise ValueError('market_data_observation_mismatch')
    result = deepcopy(value)
    originals = {'price': observation.price, 'close': observation.price,
                 **{key: getattr(observation, key) for key in
                    ('open', 'high', 'low', 'volume', 'value', 'change', 'change_pct')}}
    for key, original in originals.items():
        if key not in result:
            continue
        supplied = result[key]
        try:
            if type(supplied) not in (str, int, float, Decimal):
                raise ValueError('market_data_observation_mismatch')
            parsed = Decimal(str(supplied))
            if not parsed.is_finite() or parsed != original:
                raise ValueError('market_data_observation_mismatch')
        except (InvalidOperation, TypeError) as exc:
            raise ValueError('market_data_observation_mismatch') from exc
        result[key] = str(original)
    if 'symbol' in result and result['symbol'] != observation.symbol:
        raise ValueError('market_data_observation_mismatch')
    # MA5/prev_low는 다른 지표다. 실제 보호가 읽는 당일 low는 원 DTO가 소유한다.
    result['low'] = str(observation.low)
    return result


def quote_price_view(request, version):
    return {'price': request['price'], 'source_version': version,
            'received_at': request['observed_at'], 'market_as_of': request['market_as_of'],
            'source': request['source'], 'source_event_id': request['source_event_id']}


def validate_price_views(state, version):
    rows = state.get('quote_price_views', {})
    if type(rows) is not dict:
        raise ValueError('invalid_quote_price_views')
    keys = {'price', 'source_version', 'received_at', 'market_as_of', 'source', 'source_event_id'}
    for symbol, row in rows.items():
        if (type(symbol) is not str or not symbol or symbol != symbol.strip()
                or type(row) is not dict or row.keys() != keys
                or type(row['price']) is not str or type(row['source_version']) is not int
                or not 0 < row['source_version'] <= version):
            raise ValueError('invalid_quote_price_view')
        price = Decimal(row['price'])
        received = datetime.fromisoformat(row['received_at'])
        if not price.is_finite() or price <= 0 or received.utcoffset() is None:
            raise ValueError('invalid_quote_price_view')
        explicit = state.get('latest_explicit_quote', {}).get(symbol)
        if row['market_as_of'] is None:
            if (row['source'] is not None or row['source_event_id'] is not None
                    or (explicit and explicit['admission_version'] >= row['source_version'])):
                raise ValueError('quote_price_view_source_conflict')
        else:
            observed = datetime.fromisoformat(row['market_as_of'])
            if observed.utcoffset() is None or observed > received:
                raise ValueError('invalid_quote_price_view_time')
            expected = {'price': row['price'], 'as_of': row['market_as_of'],
                        'received_at': row['received_at'], 'source': row['source'],
                        'source_event_id': row['source_event_id'],
                        'admission_version': row['source_version']}
            if not explicit or any(explicit.get(key) != value for key, value in expected.items()):
                raise ValueError('quote_price_view_source_conflict')


def observation_from_event(event):
    from ...core.event import MarketDataEvent
    from ...core.market_observation import MarketObservation
    if type(event) is not MarketDataEvent or type(event.observation) is not MarketObservation:
        raise ValueError('market_observation_required')
    observation = MarketObservation.from_dict(event.observation.to_dict())
    expected = {'symbol': observation.symbol, 'close': observation.price,
                **{name: getattr(observation, name) for name in
                   ('open', 'high', 'low', 'volume', 'value', 'change')},
                'change_pct': float(observation.change_pct), 'source': 'kis_websocket'}
    if any(type(getattr(event, key)) is not type(value) or getattr(event, key) != value
           for key, value in expected.items()):
        raise ValueError('market_event_observation_mismatch')
    return observation


def canonical_observation(value, *, symbol, price, as_of, source, event_id, now):
    from ...core.market_observation import MarketObservation
    if type(value) is not MarketObservation:
        raise ValueError('market_observation_required')
    if (type(now) is not datetime or now.utcoffset() is None
            or type(as_of) is not datetime or as_of.utcoffset() is None):
        raise ValueError('market_observation_day_or_time_mismatch')
    value = MarketObservation.from_dict(value.to_dict())
    if (value.symbol != symbol or value.price != price or value.market_as_of != as_of
            or value.source != source or value.source_event_id != event_id):
        raise ValueError('market_observation_binding_mismatch')
    if (value.market_as_of.astimezone(now.tzinfo).date() != now.date()
            or not value.market_as_of <= value.received_at <= now):
        raise ValueError('market_observation_day_or_time_mismatch')
    return value


def entry_quote(observation):
    return {'symbol': observation.symbol, 'price': str(observation.price),
            'as_of': observation.market_as_of.isoformat(), 'source': observation.source,
            'event_id': observation.source_event_id}


def complete_source(state, *, command_id, request, admission_version, completed_version, decision):
    from ...core.market_observation import MarketObservation
    observation = MarketObservation.from_dict(request['entry_observation'])
    state.setdefault('entry_quotes', {})[observation.symbol] = entry_quote(observation)
    state.setdefault('market_sources', {})[observation.symbol] = {
        'admission_id': command_id, 'request': deepcopy(request), 'request_digest': digest(request),
        'observation': observation.to_dict(), 'admission_version': admission_version,
        'completed_version': completed_version, 'invalidated_at_version': 0,
        'decision': None if decision is None else list(decision),
    }


def validate_sources(state, version):
    rows = state.get('market_sources', {})
    if type(rows) is not dict:
        raise ValueError('invalid_market_sources')
    fields = {'admission_id', 'request', 'request_digest', 'observation', 'admission_version',
              'completed_version', 'invalidated_at_version', 'decision'}
    request_fields = {'symbol', 'price', 'market_data', 'intent_id', 'observed_at', 'market_as_of',
                      'source', 'source_event_id', 'entry_observation'}
    for symbol, row in rows.items():
        if type(row) is not dict or row.keys() != fields:
            raise ValueError('invalid_market_source_proof')
        request = row['request']
        if type(request) is not dict or request.keys() != request_fields:
            raise ValueError('invalid_market_source_request')
        from ...core.market_observation import MarketObservation
        observation = MarketObservation.from_dict(row['observation'])
        if row['observation'] != request['entry_observation'] or symbol != observation.symbol:
            raise ValueError('market_source_observation_conflict')
        if observation_market_data(observation, request['market_data']) != request['market_data']:
            raise ValueError('market_source_data_conflict')
        view = state.get('quote_price_views', {}).get(symbol)
        if not view or view['source_version'] < row['admission_version']:
            raise ValueError('market_source_price_view_required')
        if (type(row['admission_id']) is not str or not row['admission_id'].startswith('quote:')
                or row['request_digest'] != digest(request)):
            raise ValueError('market_source_digest_conflict')
        canonical_observation(observation, symbol=request['symbol'], price=observation.price,
            as_of=datetime.fromisoformat(request['market_as_of']), source=request['source'],
            event_id=request['source_event_id'], now=datetime.fromisoformat(request['observed_at']))
        if request['price'] != str(observation.price):
            raise ValueError('market_source_price_conflict')
        a, c, invalid = (row[key] for key in ('admission_version', 'completed_version', 'invalidated_at_version'))
        if (any(type(v) is not int for v in (a, c, invalid)) or not 0 < a < c <= version
                or (invalid != 0 and not c < invalid <= version)):
            raise ValueError('invalid_market_source_version')
        if view['source_version'] == a:
            if view != quote_price_view(request, a) or invalid:
                raise ValueError('market_source_price_view_conflict')
        else:
            # 새 가격은 보호 완료 전에도 영속된다. 정상 pending 복원은 허용하되
            # 완료 뒤에는 그 가격 세대보다 새로운 무효화 증거가 필요하다.
            pending = state.get('protection_quote_admissions', {})
            matching = [item for item in pending.values()
                        if item.get('symbol') == symbol
                        and item.get('status') == 'RECEIVED'
                        and item.get('source_version') == view['source_version']
                        and quote_price_view(item, item['source_version']) == view]
            if view['source_version'] <= c or (not matching and invalid <= view['source_version']):
                raise ValueError('market_source_generation_conflict')
        decision = row['decision']
        outbox = state.get('outbox', {}).get(row['admission_id'])
        if decision is None:
            if outbox is not None:
                raise ValueError('market_source_decision_conflict')
        else:
            if (type(decision) is not list or len(decision) != 3
                    or decision[0] not in ('sell_all', 'sell_partial')
                    or type(decision[1]) is not int or decision[1] <= 0
                    or type(decision[2]) is not str or not decision[2]):
                raise ValueError('invalid_market_source_decision')
            expected = {'kind': 'protection_decision', 'symbol': symbol, 'decision': decision,
                        'intent_id': request['intent_id'], 'observed_at': request['observed_at'],
                        'provenance': {'market_as_of': request['market_as_of'],
                            'source': request['source'], 'source_event_id': request['source_event_id'],
                            'received_at': request['observed_at']}}
            if type(outbox) is not dict or any(outbox.get(k) != v for k, v in expected.items()):
                raise ValueError('market_source_decision_conflict')
        if state.get('entry_quotes', {}).get(symbol) != entry_quote(observation):
            raise ValueError('market_source_entry_conflict')


def source_is_current(state, symbol):
    """무관한 후속 commit은 허용하되 다른 입력을 이전 완료 증거로 승인하지 않는다."""
    row = state.get('market_sources', {}).get(symbol)
    if row is None:
        # legacy module fixtures only. A3 factory must require actual source for every BUY.
        return True
    if row['invalidated_at_version']:
        return False
    quote = state.get('latest_explicit_quote', {}).get(symbol)
    request = row['request']
    if state.get('quote_price_views', {}).get(symbol) != quote_price_view(request, row['admission_version']):
        return False
    expected = {'price': request['price'], 'as_of': request['market_as_of'],
                'source': request['source'], 'source_event_id': request['source_event_id'],
                'market_data': request['market_data']}
    return (type(quote) is dict and all(quote.get(key) == value for key, value in expected.items())
            and quote.get('admission_version') == row['admission_version'])


def completed_duplicate(state, request):
    """현재 보존 행의 중복만 판정한다. 모든 과거 ID의 exactly-once 주장이 아니다."""
    row = state.get('market_sources', {}).get(request['symbol'])
    if row is None:
        return False, None
    previous = row['request']
    if (previous['source'], previous['source_event_id']) != (request['source'], request['source_event_id']):
        return False, None
    original = {k: v for k, v in previous.items() if k != 'observed_at'}
    current = {k: v for k, v in request.items() if k != 'observed_at'}
    if digest(original) != digest(current):
        raise ValueError('market_source_event_conflict')
    if not source_is_current(state, request['symbol']):
        raise ValueError('stale_market_source_completion')
    return True, None if row['decision'] is None else tuple(row['decision'])
