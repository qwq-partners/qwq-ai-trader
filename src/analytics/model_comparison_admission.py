"""LLM 이후 진입 게이트 재생과 KIS 장 상태 분류 (순수 함수, 주문·네트워크 없음).

replay_admission 은 판단 전 중립 사이징 스냅샷(model_comparison_sizing)과 그 재생
결과만으로 다시 계산할 수 있는 게이트를 passed/failed 로, 엔진 바깥 상태(보유·pending·
쿨다운·매수 중지·증권사 한도 등)는 unknown 으로 둔다. 엔진 주문 경로를 호출하지 않으며
엔진 규칙을 복사하지 않는다(재생 수량은 replay_policy_sizing 이 실제 엔진 함수로 계산).
외부 게이트가 항상 unknown 이므로 현재 코드에서 admissible 은 True 가 되지 않는다 — 실측
근거 생산자가 생기기 전까지 경제성 계산을 막는 의도된 결과다.

classify_market_state 는 kis_market_state 가 보존한 raw 필드만 읽는다. 코드 의미는 저장소가
지문을 고정한 KIS 공개 가이드 원문(아래 출처)에서 확인한 값만 쓰고, 나머지는 unknown 이다.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal, DecimalException
from typing import Any, Mapping

from .entry_price_shadow import _timestamp
from .kis_market_state import _GUIDE_SCHEMA, _SCHEMA_VERSION as _STATE_SCHEMA, _TR_SPECS

ADMISSION_SCHEMA = 'model-comparison-admission-v1'
MARKET_STATE_SCHEMA = 'market-state-classification-v1'

# ── 진입 게이트 목록 (근거: src/core/engine.py · src/risk/manager.py @ c4e48e7) ──────────
# 재생 가능 = 판단 전 스냅샷 state/config 와 재생 수량만으로 같은 비교를 다시 할 수 있다.
# as_of 는 스냅샷 captured_at(판단 전)이다. LLM await 동안 장부가 바뀔 수 있으므로
# '판단 전 장부 기준 통과'이지 주문 시점 통과의 증명이 아니다.
REPLAYABLE_GATES = {
    'cash_precheck': 'engine.py:2130-2146 G5_cash 가용현금-예약-코어예약<=0',
    'strategy_budget_precheck': 'engine.py:2147-2166 G5_budget 전략 보유+pending>=한도',
    'sizing_available_cash': 'engine.py:3096-3106 _calculate_position_size no_available_cash',
    'sizing_strategy_budget': 'engine.py:3166-3184 strategy_budget_exhausted',
    'sizing_min_position_value': 'engine.py:3271-3287 below_min_position_value',
    'sizing_risk_cap': 'engine.py:3311-3333 위험 0.7% 재클램프·risk_cap_below_min_position_value',
    'sizing_quantity': 'engine.py:2211,2226-2238 수량0 → G3_size size_zero:<stop>',
    'engine_daily_hard_cap': 'engine.py:822-829 effective_daily_pnl/equity<=-daily_max_loss_pct*2.5 '
                             '(engine.config.risk 와 RiskManager.config 가 같은 객체라는 배선 가정: '
                             'scripts/run_trader.py:848-849)',
    'engine_position_size': 'engine.py:885-889 price*q>equity*max_position_pct',
    'engine_cash': 'engine.py:891-896 price*q*1.001>가용현금-(예약+코어예약)',
}
# 외부 상태 = 스냅샷에 없거나 판단 뒤 바뀌는 엔진/증권사 상태. 재생으로 통과를 만들지 않는다.
EXTERNAL_GATES = {
    'factor_budget': 'engine.py:2168-2179 factor_budgets 설정·노출이 스냅샷에 없음(enforce 여부 포함)',
    'order_fail_cooldown': 'engine.py:2181-2189 _order_fail_cooldown 런타임 상태',
    'unresolved_cancel': 'engine.py:2222-2224 has_unresolved_cancel 런타임 상태',
    'validator_cash_verified': 'manager.py:287-288 _cash_verified',
    'validator_macro_entry_limit': 'manager.py:293-306 거시 국면 당일 진입 수',
    'validator_stop_loss_reentry': 'manager.py:320-328 당일 손절 재진입',
    'validator_exit_reentry': 'manager.py:339-343 당일 청산 재진입',
    'validator_sync_health': 'manager.py:348-386 잔고 동기화 건강성',
    'validator_daily_loss_limit': 'manager.py:391-411 스마트 일일 손실 한도(전략 귀속 포함)',
    'validator_daily_new_buys': 'manager.py:416-427 당일 신규 매수 수',
    'validator_max_positions': 'manager.py:432-452 보유 수(잔여 비율 가중)',
    'validator_min_cash_reserve': 'manager.py:457-460 portfolio.cash 예비현금',
    'validator_position_size': 'manager.py:462-466 검증자 자체 설정',
    'validator_cash': 'manager.py:468-473 검증자 현금 기준',
    'validator_exit_cooldown': 'manager.py:484-523 당일 손실청산 누적 쿨다운',
    'sector_limit': 'engine.py:2286-2293,858-883 섹터 조회·pending 섹터',
    'engine_daily_trades': 'engine.py:831-849 체결+pending BUY 횟수',
    'pending_race': 'engine.py:2353-2356 pending 등록 직전 재검증',
    'kill_switch': '브로커 계층 KILL_SWITCH/KILL_SWITCH_KR 파일 (주문 제출 시)',
    'unknown_buy_hold': 'engine.py:2011-2015 + 브로커 접수 불명 BUY 보류',
    'broker_buying_power': '증권사 매수가능금액·주문 한도(KIS 응답)',
}

# trace['stop'] → 그 단계에서 실패한 사이징 게이트
_STOP_GATE = {
    'no_available_cash': 'sizing_available_cash',
    'strategy_budget_exhausted': 'sizing_strategy_budget',
    'below_min_position_value': 'sizing_min_position_value',
    'risk_cap_below_min_position_value': 'sizing_risk_cap',
}
# _calculate_position_size 안의 평가 순서 (앞 단계에서 멈추면 뒤 단계는 평가되지 않음)
_SIZING_ORDER = ('sizing_available_cash', 'sizing_strategy_budget',
                 'sizing_min_position_value', 'sizing_risk_cap')


def _gate(status, as_of, reason, replayable):
    return {'status': status, 'as_of': as_of, 'reason': reason, 'replayable': replayable}


def _all_unknown(reason, anchor=None):
    gates = {name: _gate('unknown', None, reason, True) for name in REPLAYABLE_GATES}
    gates.update({name: _gate('unknown', None, 'external_state_not_replayable', False)
                  for name in EXTERNAL_GATES})
    return {'schema_version': ADMISSION_SCHEMA, 'gates': gates, 'admissible': None,
            'anchor_at': anchor, 'snapshot_id': None, 'replay_id': None, 'verdict': None,
            'quantity': None, 'reasons': [reason]}


def _admissible(gates):
    statuses = {g['status'] for g in gates.values()}
    if 'failed' in statuses:
        return False
    return None if 'unknown' in statuses else True


def replay_admission(envelope_or_snapshot: Mapping, verdict: str, replay: Mapping, *,
                     anchor_at: str | None = None) -> dict:
    """판단 전 스냅샷 기준의 진입 게이트 재생. 예외 없이 사유를 담아 반환한다.

    envelope(I2: neutral_sizing·input_available_at)를 받으면 anchor=input_available_at,
    스냅샷만 받으면 anchor_at 이 필수다. 스냅샷 captured_at 이 anchor 보다 늦으면(나중
    장부의 누설) 모든 재생 게이트를 unknown 으로 둔다.
    """
    from .model_comparison_sizing import _digest
    try:
        source = envelope_or_snapshot
        if not isinstance(source, Mapping):
            return _all_unknown('snapshot_missing')
        if 'neutral_sizing' in source:
            snapshot, anchor_raw = source.get('neutral_sizing'), source.get('input_available_at')
        else:
            snapshot, anchor_raw = source, anchor_at
        if not isinstance(snapshot, Mapping):
            return _all_unknown('snapshot_missing')
        if anchor_raw is None:
            return _all_unknown('anchor_missing')
        anchor = _timestamp(anchor_raw, 'anchor')
        body = {k: v for k, v in snapshot.items() if k != 'snapshot_id'}
        if snapshot.get('snapshot_id') != _digest(body):
            return _all_unknown('snapshot_fingerprint_mismatch', anchor_raw)
        captured = _timestamp(snapshot.get('captured_at'), 'captured_at')
        if captured > anchor:
            return _all_unknown('snapshot_after_anchor', anchor_raw)
        if (not isinstance(replay, Mapping) or replay.get('snapshot_id') != snapshot['snapshot_id']
                or replay.get('verdict') != verdict):
            return _all_unknown('replay_snapshot_or_verdict_mismatch', anchor_raw)
        quantity, trace = replay.get('quantity'), replay.get('trace')
        if type(quantity) is not int or quantity < 0 or not isinstance(trace, Mapping):
            return _all_unknown('replay_quantity_invalid', anchor_raw)
        as_of = snapshot['captured_at']
        state, config, signal = snapshot['state'], snapshot['config'], snapshot['signal']
        d = {k: Decimal(str(v)) for k, v in state.items()}
        price = Decimal(str(signal['price']))
        is_core = signal.get('strategy') == 'core_holding'
        gates = {}

        available = d['available_cash'] - d['reserved_cash'] - (0 if is_core else d['core_reserve'])
        gates['cash_precheck'] = _gate('passed' if available > 0 else 'failed', as_of,
                                       f'available={available}', True)
        cap_pct = Decimal(str((config.get('strategy_allocation') or {}).get(signal.get('strategy'), 0)))
        if cap_pct > 0:
            cap = d['equity'] * cap_pct / 100
            used = d['strategy_current'] + d['strategy_pending']
            gates['strategy_budget_precheck'] = _gate('passed' if used < cap else 'failed', as_of,
                                                      f'used={used} cap={cap}', True)
        else:
            gates['strategy_budget_precheck'] = _gate('passed', as_of, 'no_strategy_cap', True)

        stop = trace.get('stop')
        if quantity == 0:
            gates['sizing_quantity'] = _gate('failed', as_of, f"size_zero:{stop or 'quantity_zero'}", True)
        else:
            gates['sizing_quantity'] = _gate('passed', as_of, f'quantity={quantity}', True)
        failed_gate = _STOP_GATE.get(stop)
        reached = True
        for name in _SIZING_ORDER:
            if name == failed_gate:
                gates[name] = _gate('failed', as_of, f'size_zero:{stop}', True)
                reached = False
            elif not reached or (stop is not None and failed_gate is None):
                # 다른 사유로 먼저 멈췄으면 이 단계는 평가되지 않았다
                gates[name] = _gate('unknown', as_of, f'not_reached:{stop}', True)
            elif name == 'sizing_risk_cap' and 'risk_quantity_cap' in trace:
                gates[name] = _gate('passed', as_of, f"clamped_to={trace['risk_quantity_cap']}", True)
            else:
                gates[name] = _gate('passed', as_of, 'not_triggered', True)

        hard_cap = Decimal(str(config['daily_max_loss_pct'])) * Decimal('2.5')
        pnl_pct = d['effective_daily_pnl'] / d['equity'] * 100 if d['equity'] > 0 else Decimal('0')
        gates['engine_daily_hard_cap'] = _gate('failed' if pnl_pct <= -hard_cap else 'passed', as_of,
                                               f'pnl_pct={pnl_pct} cap=-{hard_cap}', True)
        value = price * quantity
        if quantity == 0:
            for name in ('engine_position_size', 'engine_cash'):
                gates[name] = _gate('unknown', as_of, 'not_reached:size_zero', True)
        else:
            max_value = d['equity'] * Decimal(str(config['max_position_pct'])) / 100
            gates['engine_position_size'] = _gate('failed' if value > max_value else 'passed', as_of,
                                                  f'value={value} max={max_value}', True)
            reserved = d['reserved_cash'] + (0 if is_core else d['core_reserve'])
            required = value * Decimal('1.001')
            room = d['available_cash'] - reserved
            gates['engine_cash'] = _gate('failed' if required > room else 'passed', as_of,
                                         f'required={required} available={room}', True)
        gates.update({name: _gate('unknown', None, 'external_state_not_replayable', False)
                      for name in EXTERNAL_GATES})
        return {'schema_version': ADMISSION_SCHEMA, 'gates': gates, 'admissible': _admissible(gates),
                'anchor_at': anchor_raw, 'snapshot_id': snapshot['snapshot_id'],
                'replay_id': replay.get('replay_id'), 'verdict': verdict, 'quantity': quantity,
                'reasons': []}
    except (ValueError, KeyError, TypeError, AttributeError, DecimalException, ArithmeticError):
        return _all_unknown('snapshot_invalid')


# ── 장 상태 코드 의미 ───────────────────────────────────────────────────────────
# 출처(공식 1차): KIS 개발자 포털 공개 상세 API, 인증 없이 조회.
#   H0STCNT0 https://apiportal.koreainvestment.com/api/apis/public/detail?accessUrl=%2Ftryitout%2FH0STCNT0
#     저장 원문 sha256 833b105418e98807d3e24753f97ed008e0f5f6972a0937a1a0f40d252543624d (2026-10-12 조회,
#     필드 수정일 2026-09-11T16:31:27+09:00) — kis_market_state._TR_SPECS 와 같은 지문.
#   H0STMKO0 https://apiportal.koreainvestment.com/api/apis/public/detail?accessUrl=%2Ftryitout%2FH0STMKO0
#     저장 원문 sha256 4c76c54995ff112312ecf9e98f4f194d6edfe2f0749300b8639c10ff0d40c3f5 (2026-10-12 조회,
#     필드 수정일 2025-04-30T13:11:13+09:00).
#   2026-10-13 재조회(WebFetch)로 아래 설명 문구가 같음을 확인했다(변환 본문이라 해시 비교 불가).
# 정상 연속매매로 쓰는 값 (H0STCNT0, 체결이 일어난 순간의 값):
#   NEW_MKOP_CLS_CODE '20' = 첫 자리 2 장중 + 둘째 자리 0 보통
#   TRHT_YN 'N' = 정상거래, HOUR_CLS_CODE '0' = 장중, MARKET_CLS_CODE '2' = 정규
_CNT_NORMAL = {'new_mkop_cls_code': '20', 'trht_yn': 'N', 'hour_cls_code': '0', 'market_cls_code': '2'}
# 문서화된 다른 값 = 정상 연속매매가 아님 (첫/둘째 자리 의미가 문서에 있는 값만)
_CNT_NOT_NORMAL = {
    'new_mkop_cls_code': {a + b for a in '123478' for b in '012378'} - {'20'},
    'trht_yn': {'Y'},
    'hour_cls_code': {'A', 'B', 'C', 'D'},       # C = 9시 이후 예상가·VI 발동
    'market_cls_code': {'1', '3', '5'},          # 프리·애프터·종가
}
# H0STMKO0 MKOP_CLS_CODE (사건 코드). 112 장개시만 연속매매와 양립한다.
_MKO_CONSISTENT = {'112'}
_MKO_NOT_NORMAL = {'110', '121', '129', '130', '139', '140', '146', '149', '150', '156', '159',
                   '164', '174', '182', '184'}  # 동시호가·장마감·시간외·시장임시정지·서킷브레이크
# 175/185 서킷 해제, 387~398 사이드카, F**/P** 예고, '???' 는 연속매매 여부를 문서가 정하지 않아 unknown.
# ISCD_STAT_CLS_CODE: 58 거래정지 → not_normal, 00/55/57 은 연속매매와 무관한 분류, 그 외(51~54, 59) unknown.
_ISCD_NEUTRAL = {'00', '55', '57'}
# 확정 못 한 의미: H0STMKO0 TRHT_YN·EXCH_CLS_CODE 설명 공란, MRKT_TRTM/ANTC/DIVI 는 동시호가 사건용,
# H0STCNT0 MRKT_TRTM_CLS_CODE 설명 공란. 이 필드들은 정상 근거로 쓰지 않는다(Y 만 보수적으로 차단).


def classify_market_state(raw_state_record_or_list, *, now, max_age_seconds, expected_connection_id,
                          expected_generation) -> dict:
    """정상 연속매매 판정. 근거가 하나라도 부족하면 unknown, 반대 근거면 not_normal.

    normal_continuous 조건: (1) 같은 연결·세대의 H0STCNT0 최신 기록이 now 이전·max_age 이내이고
    20/N/0/2, (2) 같은 연결·세대의 H0STMKO0 최신 기록이 112 장개시·VI N·종목상태 중립.
    장운영 알림은 사건식이라 없으면 unknown 이다(침묵은 정상 근거가 아님).
    채널별 최신(now 이전) 기록을 먼저 고른다. 그 기록이 미자격·다른 연결/세대이거나 채널에 시각
    불명 기록이 있으면 그 채널은 unknown(latest_record_unqualified:<tr>)이며 이전 정상 기록으로
    대체하지 않는다.
    """
    basis, refs = [], []

    def out(verdict):
        return {'schema_version': MARKET_STATE_SCHEMA, 'verdict': verdict,
                'basis': basis, 'source_refs': refs,
                'evaluated_at': now if isinstance(now, str) else None}

    try:
        at = _timestamp(now, 'now')
        if (isinstance(max_age_seconds, bool) or not isinstance(max_age_seconds, (int, float))
                or not max_age_seconds > 0):
            basis.append('invalid_max_age')
            return out('unknown')
    except ValueError:
        basis.append('invalid_now')
        return out('unknown')
    records = raw_state_record_or_list
    if isinstance(records, Mapping):
        records = [records]
    if not isinstance(records, (list, tuple)) or not records:
        basis.append('missing_events')
        return out('unknown')
    # 채널별 최신 기록을 먼저 고르고, 그 최신 기록이 미자격이면 채널 전체를 unknown 으로 둔다.
    # 이전 정상 기록으로 대체하지 않는다(정상 뒤 무효/세대 전환이 오면 지금 상태는 불명).
    latest, untimed, conflicted = {}, set(), set()
    symbols = set()
    for raw in records:
        if not isinstance(raw, Mapping) or raw.get('kind') != 'market_state':
            basis.append('not_market_state_record')
            continue
        tr = raw.get('tr_id')
        spec = _TR_SPECS.get(tr)
        if spec is None:
            basis.append(f'unqualified_record:{tr}')
            continue
        try:
            received = _timestamp(raw.get('received_at'), 'received_at')
        except ValueError:
            basis.append(f'invalid_received_at:{tr}')
            untimed.add(tr)   # 순서를 알 수 없는 기록이 최신일 수 있다
            continue
        if received > at:
            continue  # now 이후 기록은 판정에 쓰지 않는다
        symbols.add(raw.get('symbol'))
        problem = None
        if (raw.get('schema_version') != _STATE_SCHEMA or raw.get('market_state_schema') != _GUIDE_SCHEMA
                or raw.get('market_state_source_sha256') != spec['source_sha256']
                or raw.get('market_state_status') != 'raw_only'):
            problem = f'unqualified_record:{tr}'
        elif raw.get('connection_id') != expected_connection_id or raw.get('generation') != expected_generation:
            problem = f'other_connection_or_generation:{tr}'
        key = (received, *(v if type(v) is int else -1
                           for v in (raw.get('local_frame_index'), raw.get('record_index'))))
        if tr in latest and key == latest[tr][0] and (dict(raw) != dict(latest[tr][1]) or problem != latest[tr][2]):
            conflicted.add(tr)       # 같은 수신 시각·번호에 내용이 다른 기록 — 입력 순서로 고르지 않고 불명으로 둔다(5회차 P2)
        if tr not in latest or key > latest[tr][0]:
            latest[tr] = (key, raw, problem)
    disqualified = set(untimed)
    for tr in conflicted:
        basis.append(f'conflicting_records:{tr}')
        disqualified.add(tr)
    for tr in list(latest):
        if latest[tr][2] is not None:
            basis.append(latest[tr][2])
            disqualified.add(tr)
    for tr in sorted(disqualified):
        basis.append(f'latest_record_unqualified:{tr}')
        latest.pop(tr, None)
    if len(symbols) > 1:
        basis.append('mixed_symbols')
        return out('unknown')
    for tr in ('H0STCNT0', 'H0STMKO0'):
        if tr in latest:
            raw = latest[tr][1]
            refs.append({'tr_id': tr, 'received_at': raw.get('received_at'),
                         'connection_id': raw.get('connection_id'), 'generation': raw.get('generation'),
                         'local_frame_index': raw.get('local_frame_index'),
                         'record_index': raw.get('record_index'),
                         'source_sha256': raw.get('market_state_source_sha256')})

    cnt_state = 'unknown'
    if 'H0STCNT0' in disqualified:
        pass  # 최신 기록 미자격 사유는 위에 기록했다
    elif 'H0STCNT0' not in latest:
        basis.append('missing_events:H0STCNT0')
    else:
        (received, _, _), cnt, _ = latest['H0STCNT0']
        values = {k: cnt.get(k) for k in _CNT_NORMAL}
        if at - received > timedelta(seconds=max_age_seconds):
            basis.append('stale:H0STCNT0')
        elif any(not isinstance(v, str) or not v.strip() for v in values.values()):
            basis.append('empty_value:H0STCNT0')
        elif any(values[k] in _CNT_NOT_NORMAL[k] for k in values):
            cnt_state = 'not_normal'
            basis.extend(f'not_normal:H0STCNT0.{k}={values[k]}' for k in values
                         if values[k] in _CNT_NOT_NORMAL[k])
        elif values == _CNT_NORMAL:
            cnt_state = 'normal'
            basis.append('normal:H0STCNT0=20/N/0/2')
        else:
            basis.extend(f'unknown_code:H0STCNT0.{k}={values[k]}' for k in values
                         if values[k] != _CNT_NORMAL[k])

    mko_state = 'unknown'
    if 'H0STMKO0' in disqualified:
        pass
    elif 'H0STMKO0' not in latest:
        basis.append('missing_events:H0STMKO0')
    else:
        mko = latest['H0STMKO0'][1]
        code, vi, iscd, halt = (mko.get('mkop_cls_code'), mko.get('vi_cls_code'),
                                mko.get('iscd_stat_cls_code'), mko.get('trht_yn'))
        if any(not isinstance(v, str) or not v.strip() for v in (code, vi, iscd)):
            basis.append('empty_value:H0STMKO0')
        else:
            code, vi, iscd = code.strip(), vi.strip(), iscd.strip()
            bad = []
            if code in _MKO_NOT_NORMAL:
                bad.append(f'mkop_cls_code={code}')
            if vi == 'Y':
                bad.append('vi_cls_code=Y')
            if iscd == '58':
                bad.append('iscd_stat_cls_code=58')
            if isinstance(halt, str) and halt.strip() == 'Y':
                bad.append('trht_yn=Y')
            if bad:
                mko_state = 'not_normal'
                basis.extend('not_normal:H0STMKO0.' + b for b in bad)
            elif code in _MKO_CONSISTENT and vi == 'N' and iscd in _ISCD_NEUTRAL:
                mko_state = 'normal'
                basis.append(f'normal:H0STMKO0={code}/vi=N/iscd={iscd}')
            else:
                basis.append(f'unknown_code:H0STMKO0={code}/vi={vi}/iscd={iscd}')

    if cnt_state == 'not_normal':
        return out('not_normal')
    if mko_state == 'not_normal':
        if cnt_state == 'normal':
            basis.append('channel_conflict')
            return out('unknown')
        return out('not_normal')
    if cnt_state == 'normal' and mko_state == 'normal':
        return out('normal_continuous')
    return out('unknown')
