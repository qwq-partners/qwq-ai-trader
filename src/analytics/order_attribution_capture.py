"""주문에 연결할 원문 식별자만 복사한다. 판단·주문 허용·자료 영속성의 증거는 아니다."""
from .entry_observation import EntryObservationBuffer, _scalar


def _candidate_reference(owner, event):
    observer = getattr(owner, '_entry_price_observer', None)
    if not isinstance(observer, EntryObservationBuffer) or event.source != 'live_screening':
        return None, 'not_available'
    # 새 무제한 인덱스나 전체 원장 복사를 만들지 않고 기존 제한된 메모리 기록만 조회한다.
    records = observer._records
    signals = [row for row in records if row.get('kind') == 'signal'
               and row.get('signal_id') == event.id]
    if not signals:
        return None, 'not_available'
    expected = {'event_timestamp': _scalar(event.timestamp), 'strategy': event.strategy.value,
                'price': _scalar(event.price), 'signal_target_price': _scalar(event.target_price),
                'signal_stop_price': _scalar(event.stop_price)}
    if any(any(row.get(key) != value for key, value in expected.items()) for row in signals):
        return None, 'conflict'
    ids = [row.get('candidate_id') for row in signals]
    if any(type(value) is not str for value in ids):
        return None, 'invalid'
    if len(set(ids)) != 1:
        return None, 'conflict'
    candidate_id = ids[0]
    if not candidate_id.endswith(':' + event.symbol):
        return None, 'conflict'
    # 종목/시각으로 ID를 생성하지 않는다. 같은 ID의 실제 후보 원문이 있어야 한다.
    present = any(row.get('kind') == 'scan' and row.get('route_origin') == 'live_screening'
                  and any(candidate.get('candidate_id') == candidate_id
                          and candidate.get('symbol') == event.symbol
                          for candidate in row.get('candidates', [])) for row in records)
    return (candidate_id, None) if present else (None, 'not_available')


def capture_order_attribution(owner, event):
    from ..core.order_attribution import freeze_attribution

    reasons = {}
    try:
        candidate_id, reason = _candidate_reference(owner, event)
    except Exception:
        # 관측 장애로 보호 매도나 기존 주문 경로를 막지 않는다.
        candidate_id, reason = None, 'invalid'
    if reason is not None:
        reasons['candidate_id'] = reason
    meta = event.metadata if type(event.metadata) is dict else {}
    plan, decision = meta.get('entry_plan'), meta.get('llm_decision')
    plan_id = plan.get('plan_id') if type(plan) is dict else None
    decision_id = decision.get('decision_id') if type(decision) is dict else None
    if plan is not None and type(plan) is not dict:
        reasons['plan_id'] = 'invalid'
    elif type(plan) is dict and plan.get('symbol') is not None and plan['symbol'] != event.symbol:
        plan_id, reasons['plan_id'] = None, 'conflict'
    if decision is not None and type(decision) is not dict:
        reasons['llm_decision_id'] = 'invalid'
    return freeze_attribution(candidate_id=candidate_id, plan_id=plan_id,
                              signal_id=event.id, llm_decision_id=decision_id,
                              missing_reasons=reasons)
