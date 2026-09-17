"""KR 보호 상태의 명시 checkpoint와 파일 I/O 없는 ExitManager 후보 계산.

경제 상태의 commit/멱등성은 application 소유다. 이 모듈은 주문을 송신하거나
legacy 파일을 읽지 않으며, 후보의 결과만 돌려준다. degraded는 자동 복구하지 않는다.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
import math
from pathlib import Path
from zoneinfo import ZoneInfo

from ...core.types import Position
from ...strategies.exit_manager import (
    ExitConfig, ExitManager, ExitStage, PositionExitState, REGIME_EXIT_PARAMS,
)
from .application import FillDelta, FillObservation
from .store import encode_state


_KST = ZoneInfo("Asia/Seoul")
_CONFIG_FIELDS = (
    "enable_partial_exit", "first_exit_pct", "first_exit_ratio", "second_exit_pct",
    "second_exit_ratio", "third_exit_pct", "third_exit_ratio", "stop_loss_pct",
    "enable_dynamic_stop", "atr_multiplier", "min_stop_pct", "max_stop_pct",
    "trailing_stop_pct", "trailing_activate_pct", "atr_trailing_multiplier",
    "enable_atr_linked_trailing", "atr_link_multiplier", "atr_link_cap_pct", "include_fees",
    "max_holding_days", "stale_exit_days", "stale_exit_pnl_pct", "stale_high_days",
    "stale_high_min_pnl_pct", "post_exit_stale_days", "post_exit_stale_pnl_pct",
    "enable_composite_trailing", "composite_trail_min_stage", "composite_ma5_buffer_pct",
    "composite_prev_low_enabled", "eod_close",
)
_STATE_FIELDS = (
    "symbol", "entry_price", "original_quantity", "remaining_quantity", "current_stage",
    "highest_price", "total_realized_pnl", "exit_history", "stop_loss_pct", "trailing_stop_pct",
    "first_exit_pct", "second_exit_pct", "third_exit_pct", "first_exit_ratio", "second_exit_ratio",
    "third_exit_ratio", "atr_pct", "dynamic_stop_pct", "effective_trailing_stop_pct",
    "breakeven_activated", "last_new_high_date", "stale_high_days", "pending_stage",
    "pending_since", "pending_target_qty", "pending_filled_qty", "initial_quantity", "is_core",
    "max_holding_days", "trailing_activate_pct", "strategy_name", "initial_risk_amount", "actual_stop_pct",
)
_ROOTS = {"schema", "market", "config", "states", "entry_times", "exit_exempt", "max_holding_days",
          "current_regime", "intraday_crash_level", "integrity_reset_symbols", "degraded", "orders", "pending_owners"}
_DECIMALS = {"entry_price", "highest_price", "total_realized_pnl", "initial_risk_amount", "actual_stop_pct"}
_QUANTITIES = {"original_quantity", "remaining_quantity", "pending_target_qty", "pending_filled_qty", "initial_quantity"}
_OPTIONAL_NUMBERS = {"stop_loss_pct", "trailing_stop_pct", "first_exit_pct", "second_exit_pct", "third_exit_pct",
                     "first_exit_ratio", "second_exit_ratio", "third_exit_ratio", "atr_pct", "dynamic_stop_pct",
                     "effective_trailing_stop_pct", "trailing_activate_pct"}
_PENDING = ("pending_stage", "pending_since", "pending_target_qty", "pending_filled_qty")
_ORDER_FIELDS = {"symbol", "side", "intent_id", "kind", "base_quantity", "cumulative_quantity", "reset_applied"}
_KINDS = {"initial_entry", "same_entry_fill", "distinct_add_on", "exit"}
_REGISTER = (_OPTIONAL_NUMBERS - {"atr_pct", "dynamic_stop_pct", "effective_trailing_stop_pct"}) | {
    "atr_pct_hint", "stale_high_days", "is_core", "max_holding_days", "strategy_name",
}


def _text(value):
    if type(value) is not str or not value or value != value.strip():
        raise ValueError("보호 식별자 오류")
    return value


def _strategy(value):
    # 기존 Position/ExitManager의 전략 미지정 ''/None은 보존한다.
    # 종목/intent 같은 필수 식별자의 _text 제약은 완화하지 않는다.
    if value is not None and value != "":
        _text(value)
    elif value is not None and type(value) is not str:
        raise ValueError("보호 전략 형식 오류")
    return value


def _quantity(value):
    if type(value) is not int or value < 0:
        raise ValueError("보호 수량 오류")
    return value


def _number(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("보호 정책 숫자 오류")
    return value


def _aware(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("보호 시각은 aware datetime이어야 합니다")
    return value


def _time(value):
    if type(value) is not str:
        raise ValueError("보호 시각 형식 오류")
    return _aware(datetime.fromisoformat(value))


def _encode_time(value):
    if not isinstance(value, datetime):
        raise ValueError("보호 시각 형식 오류")
    # 기존 ExitManager가 만든 naive 시각의 전환 규칙은 KR 로컬 시각으로 명시한다.
    if value.tzinfo is None:
        value = value.replace(tzinfo=_KST)
    return _aware(value).isoformat()


def _keys(value, expected):
    if type(value) is not dict or set(value) != set(expected):
        raise ValueError("보호 checkpoint 필드 누락/미지원")


def _state_from_dict(symbol, row):
    _keys(row, _STATE_FIELDS)
    values = deepcopy(row)
    if _text(values["symbol"]) != symbol:
        raise ValueError("보호 종목 불일치")
    for name in _DECIMALS:
        value = values[name]
        if name in {"initial_risk_amount", "actual_stop_pct"} and value is None:
            continue
        if type(value) is not str:
            raise ValueError("보호 금액은 Decimal 문자열이어야 합니다")
        try:
            amount = Decimal(value)
        except Exception:
            raise ValueError("보호 금액 오류") from None
        if not amount.is_finite() or (name != "total_realized_pnl" and amount < 0):
            raise ValueError("보호 금액 오류")
        values[name] = amount
    for name in _QUANTITIES:
        _quantity(values[name])
    for name in _OPTIONAL_NUMBERS:
        if values[name] is not None:
            _number(values[name])
    for name in ("stale_high_days", "max_holding_days"):
        if values[name] is not None:
            _quantity(values[name])
    for name in ("breakeven_activated", "is_core"):
        if type(values[name]) is not bool:
            raise ValueError("보호 bool 필드 오류")
    values["current_stage"] = ExitStage(values["current_stage"])
    if values["pending_stage"] is not None:
        values["pending_stage"] = ExitStage(values["pending_stage"])
    if values["pending_since"] is not None:
        values["pending_since"] = _time(values["pending_since"])
    if values["last_new_high_date"] is not None:
        raw_date = values["last_new_high_date"]
        parsed = date.fromisoformat(raw_date)
        if parsed.isoformat() != raw_date:
            raise ValueError("보호 날짜 형식 오류")
        values["last_new_high_date"] = parsed
    _strategy(values["strategy_name"])
    if type(values["exit_history"]) is not list:
        raise ValueError("보호 이력 형식 오류")
    for event in values["exit_history"]:
        _keys(event, {"timestamp", "action", "quantity", "reason", "remaining_before"})
        _time(event["timestamp"])
        if event["action"] not in {"sell_all", "sell_partial"}:
            raise ValueError("보호 이력 명령 오류")
        _quantity(event["quantity"])
        _quantity(event["remaining_before"])
        _text(event["reason"])
    if values["remaining_quantity"] > values["original_quantity"]:
        raise ValueError("보호 잔량이 원수량 초과")
    if values["remaining_quantity"] and values["entry_price"] <= 0:
        raise ValueError("보호 진입가 오류")
    return PositionExitState(**values)


def _validate(dto):
    _keys(dto, _ROOTS)
    encode_state(dto)
    if type(dto["schema"]) is not int or dto["schema"] != 1 or dto["market"] != "KR":
        raise ValueError("미지원 보호 checkpoint")
    _keys(dto["config"], _CONFIG_FIELDS)
    defaults = ExitConfig()
    for name, value in dto["config"].items():
        default = getattr(defaults, name)
        if type(default) is bool:
            if type(value) is not bool:
                raise ValueError("보호 설정 bool 오류")
        elif type(default) is int:
            _quantity(value)
        elif type(default) is str:
            if value not in ExitManager._COMPOSITE_STAGE_MAP:
                raise ValueError("보호 설정 stage 오류")
        else:
            _number(value)
    _quantity(dto["max_holding_days"])
    if dto["current_regime"] not in REGIME_EXIT_PARAMS or dto["intraday_crash_level"] not in {"normal", "caution", "crash", "severe"}:
        raise ValueError("보호 레짐 오류")
    for name in ("states", "entry_times", "degraded", "orders", "pending_owners"):
        if type(dto[name]) is not dict:
            raise ValueError("보호 map 형식 오류")
        for key in dto[name]:
            _text(key)
    states = {symbol: _state_from_dict(symbol, row) for symbol, row in dto["states"].items()}
    entry_times = {symbol: _time(value) for symbol, value in dto["entry_times"].items()}
    for name in ("exit_exempt", "integrity_reset_symbols"):
        if type(dto[name]) is not list or any(type(value) is not str for value in dto[name]):
            raise ValueError("보호 집합 형식 오류")
        if len(set(dto[name])) != len(dto[name]):
            raise ValueError("보호 집합 중복")
        for value in dto[name]:
            _text(value)
    for row in dto["degraded"].values():
        _keys(row, {"quantity", "reason"})
        _quantity(row["quantity"])
        _text(row["reason"])
    if set(states) - set(dto["degraded"]) - set(entry_times):
        raise ValueError("정상 보호 상태의 진입시각 누락")
    for row in dto["orders"].values():
        _keys(row, _ORDER_FIELDS)
        _text(row["symbol"])
        _text(row["intent_id"])
        if row["side"] not in {"BUY", "SELL"} or row["kind"] not in _KINDS:
            raise ValueError("보호 주문 분류 오류")
        if (row["side"] == "SELL") != (row["kind"] == "exit"):
            raise ValueError("보호 주문 방향/분류 불일치")
        _quantity(row["base_quantity"])
        _quantity(row["cumulative_quantity"])
        if type(row["reset_applied"]) is not bool:
            raise ValueError("보호 주문 reset 오류")
    for owner in dto["pending_owners"].values():
        _text(owner)
    return states, entry_times


def encode_protection(manager: ExitManager) -> dict:
    """모든 보호 실행 필드를 독립 JSON 값으로 내보낸다. legacy 시각은 KST다."""
    if not isinstance(manager, ExitManager):
        raise ValueError("ExitManager가 필요합니다")
    states = {}
    for symbol, state in manager._states.items():
        row = {name: deepcopy(getattr(state, name)) for name in _STATE_FIELDS}
        for name in _DECIMALS:
            if row[name] is not None:
                row[name] = str(row[name])
        for name in ("current_stage", "pending_stage"):
            if row[name] is not None:
                row[name] = row[name].value
        if row["pending_since"] is not None:
            row["pending_since"] = _encode_time(row["pending_since"])
        if row["last_new_high_date"] is not None:
            row["last_new_high_date"] = row["last_new_high_date"].isoformat()
        for event in row["exit_history"]:
            event["timestamp"] = _encode_time(datetime.fromisoformat(event["timestamp"]))
        states[symbol] = row
    extras = deepcopy(getattr(manager, "_execution_protection", {"degraded": {}, "orders": {}, "pending_owners": {}}))
    dto = {"schema": 1, "market": manager.market,
           "config": {name: deepcopy(getattr(manager.config, name)) for name in _CONFIG_FIELDS},
           "states": states, "entry_times": {key: _encode_time(value) for key, value in manager._entry_times.items()},
           "exit_exempt": sorted(manager._exit_exempt), "max_holding_days": manager._max_holding_days,
           "current_regime": manager._current_regime, "intraday_crash_level": manager._intraday_crash_level,
           "integrity_reset_symbols": sorted(manager._integrity_reset_symbols), **extras}
    _validate(dto)
    return dto


async def _unknown_pending(_symbol):
    """파일/네트워크 없는 보수적 표식. 후보에서 비동기 verifier를 호출하지 않는다."""
    return None


def decode_protection(dto: dict, *, clock) -> ExitManager:
    """엄격히 검증한 checkpoint를 독립 메모리 전용 계산기로 복원한다."""
    states, entry_times = _validate(dto)
    _aware(clock())
    manager = ExitManager(ExitConfig(**deepcopy(dto["config"])), market=dto["market"],
                          persist=False, state_dir=Path("/execution-protection-no-io"),
                          clock=lambda: _aware(clock()).astimezone(_KST))
    manager._states, manager._entry_times = states, entry_times
    manager._exit_exempt = set(dto["exit_exempt"])
    manager._max_holding_days = dto["max_holding_days"]
    manager._current_regime = dto["current_regime"]
    manager._intraday_crash_level = dto["intraday_crash_level"]
    manager._integrity_reset_symbols = set(dto["integrity_reset_symbols"])
    manager._pending_verifier = _unknown_pending
    manager._execution_protection = {key: deepcopy(dto[key]) for key in ("degraded", "orders", "pending_owners")}
    return manager


def publish_protection(live: ExitManager, dto: dict, *, clock) -> None:
    """모든 검증 뒤 소유 필드만 게시한다. live I/O 의존성은 건드리지 않는다."""
    candidate = decode_protection(dto, clock=clock)
    if not isinstance(live, ExitManager) or live.market != candidate.market:
        raise ValueError("보호 게시 시장 불일치")
    for name in ("config", "_states", "_entry_times", "_max_holding_days",
                 "_current_regime", "_intraday_crash_level", "_integrity_reset_symbols", "_execution_protection"):
        setattr(live, name, deepcopy(getattr(candidate, name)))
    # RiskManager가 참조하는 동일 set 객체를 유지한다. 이 게시 구간에는 await가 없다.
    live._exit_exempt.clear()
    live._exit_exempt.update(candidate._exit_exempt)


def _registration(observation):
    params = dict(observation.metadata.get("registration_params", {}))
    if not params.keys() <= _REGISTER:
        raise ValueError("미지원 보호 등록 인자")
    for name, value in params.items():
        if name == "strategy_name":
            _strategy(value)
        elif name == "is_core":
            if type(value) is not bool:
                raise ValueError("보호 등록 bool 오류")
        elif value is not None:
            if name in {"stale_high_days", "max_holding_days"}:
                _quantity(value)
            else:
                _number(value)
    return params


def _degraded(dto, symbol, quantity):
    candidate = deepcopy(dto)
    candidate["degraded"][symbol] = {"quantity": quantity, "reason": "protection_calculation_failed"}
    return candidate, "degraded"


def _closed(dto, symbol):
    candidate = deepcopy(dto)
    for name in ("states", "entry_times", "degraded", "pending_owners"):
        candidate[name].pop(symbol, None)
    return candidate, "exempt" if symbol in dto["exit_exempt"] else "ready"


def reduce_protection(dto: dict, *, before: Position | None, after: Position | None,
                      observation: FillObservation, delta: FillDelta, fill_kind: str,
                      intent_id: str, now: datetime) -> tuple[dict, str]:
    """경제 증분이 확정된 뒤 보호만 계산한다. 실패는 수량을 담은 degraded다."""
    _validate(dto)
    _aware(now)
    if not isinstance(observation, FillObservation) or not isinstance(delta, FillDelta):
        raise ValueError("정규화된 체결이 필요합니다")
    symbol = observation.symbol
    quantity = after.quantity if after is not None else 0
    _quantity(quantity)
    closed = False
    try:
        _text(intent_id)
        if fill_kind not in _KINDS or observation.market != "KR":
            raise ValueError("보호 체결 범위 오류")
        if (observation.side == "SELL") != (fill_kind == "exit"):
            raise ValueError("보호 체결 방향/분류 불일치")
        if type(delta.quantity) is not int or delta.quantity <= 0 or delta.amount <= 0:
            raise ValueError("보호 체결 증분 오류")
        for position in (before, after):
            if position is not None and (position.symbol != symbol or type(position.quantity) is not int or position.quantity <= 0):
                raise ValueError("보호 포지션 범위 오류")
        previous_quantity = before.quantity if before is not None else 0
        expected = previous_quantity + delta.quantity if observation.side == "BUY" else previous_quantity - delta.quantity
        if expected != quantity or expected < 0:
            raise ValueError("보호 경제 수량 불일치")
        closed = quantity == 0
        # 경제 보유0은 보호할 잔량이 없다. 정상 상태는 아래 실제 on_fill을 거치고,
        # 이미 degraded/누락인 상태도 존재하지 않는 보유의 BUY 장벽으로 남기지 않는다.
        if symbol in dto["degraded"]:
            return _closed(dto, symbol) if closed else _degraded(dto, symbol, quantity)
        manager = decode_protection(dto, clock=lambda: now)
        extras = manager._execution_protection
        state = manager.get_state(symbol)
        if before is not None and (state is None or state.remaining_quantity != previous_quantity):
            raise ValueError("보호 잔량 불일치")
        order_key = observation.order_key
        order = extras["orders"].get(order_key)
        if order is None:
            if observation.cumulative_quantity != delta.quantity:
                raise ValueError("보호 주문의 과거 누적 누락")
            if observation.side == "BUY" and fill_kind == "same_entry_fill":
                raise ValueError("보호 진입 주문 식별 불명")
            order = {"symbol": symbol, "side": observation.side, "intent_id": intent_id, "kind": fill_kind,
                     "base_quantity": previous_quantity, "cumulative_quantity": 0, "reset_applied": False}
            extras["orders"][order_key] = order
        if (order["symbol"] != symbol or order["side"] != observation.side or order["intent_id"] != intent_id
                or order["cumulative_quantity"] + delta.quantity != observation.cumulative_quantity):
            raise ValueError("보호 주문 누적/소유권 불일치")
        if (observation.side == "BUY" and fill_kind != "same_entry_fill"
                and order["kind"] != fill_kind):
            raise ValueError("보호 주문 분류 변경 금지")
        order["cumulative_quantity"] = observation.cumulative_quantity
        if observation.side == "BUY":
            params = _registration(observation)
            if before is None:
                if state is not None or fill_kind != "initial_entry":
                    raise ValueError("보호 신규 진입 불일치")
                manager.register_position(deepcopy(after), **params)
            else:
                if fill_kind == "initial_entry":
                    raise ValueError("보호 기존 진입 재등록 금지")
                state.entry_price = after.avg_price
                state.remaining_quantity = quantity
                if order["kind"] == "initial_entry":
                    state.original_quantity += delta.quantity
                    state.initial_quantity += delta.quantity
                elif order["kind"] == "distinct_add_on":
                    # 별도 추가매수의 원수량과 고점은 기존 register_position 정책을 유지.
                    state.original_quantity = quantity
                    new_high = (after.current_price if after.current_price is not None
                                and after.current_price > 0 else after.avg_price)
                    state.highest_price = max(state.highest_price, new_high)
                    # 주문 전 보유를 기준으로 누적10%를 최초 통과할 때 한 번만 리셋.
                    if (not order["reset_applied"]
                            and order["cumulative_quantity"] * 10 >= max(order["base_quantity"], 1)):
                        state.current_stage = ExitStage.NONE
                        state.breakeven_activated = False
                        state.initial_quantity = quantity
                        order["reset_applied"] = True
                else:
                    raise ValueError("보호 진입 분류 불명")
        else:
            if state is None:
                raise ValueError("보호 매도 대상 누락")
            saved_pending = {name: getattr(state, name) for name in _PENDING}
            owns_pending = extras["pending_owners"].get(symbol) == intent_id
            if not owns_pending:
                state.pending_stage = None
            manager.on_fill(symbol, delta.quantity, delta.price)
            state = manager.get_state(symbol)
            if state is not None and not owns_pending:
                for name, value in saved_pending.items():
                    setattr(state, name, value)
            if state is None or state.pending_stage is None:
                extras["pending_owners"].pop(symbol, None)
        candidate = encode_protection(manager)
        return candidate, "exempt" if symbol in candidate["exit_exempt"] else "ready"
    except Exception:
        return _closed(dto, symbol) if closed else _degraded(dto, symbol, quantity)


def quote_protection(dto: dict, *, symbol: str, price: Decimal, now: datetime,
                     market_data: dict | None = None, intent_id: str | None = None) -> tuple[dict, object]:
    """가격별 보호 전이를 순서대로 계산한다. 반환 결정은 주문 송신이 아니다."""
    _validate(dto)
    _aware(now)
    _text(symbol)
    if type(price) is not Decimal or not price.is_finite() or price <= 0:
        raise ValueError("보호 가격 오류")
    if intent_id is not None:
        _text(intent_id)
    if symbol in dto["degraded"]:
        return deepcopy(dto), None
    manager = decode_protection(dto, clock=lambda: now)
    state = manager.get_state(symbol)
    pending = {name: getattr(state, name) for name in _PENDING} if state is not None and state.pending_stage is not None else None
    history = deepcopy(state.exit_history) if pending is not None else None
    if pending is not None:
        # verifier 존재만으로는1800초 hard expiry를 막지 못한다. 후보에서만 시각을
        # 현재로 고정하고 원 필드 복원; 기존 pending 중 새 stop/익절 결정도 내보내지 않는다.
        state.pending_since = now
    decision = manager.update_price(symbol, price, deepcopy(market_data))
    if pending is not None:
        for name, value in pending.items():
            setattr(state, name, value)
        state.exit_history = history
        decision = None
    elif state is not None and state.pending_stage is not None:
        if intent_id is None:
            raise ValueError("신규 pending 보호 목표에는 intent_id가 필요합니다")
        manager._execution_protection["pending_owners"][symbol] = intent_id
    return encode_protection(manager), decision
