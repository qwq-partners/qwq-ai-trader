"""KR 경제 후보와 명시 DTO. 파일/원장/네트워크 부작용을 갖지 않는다.

비용은 기존 요율의 주문 누적 추정치이며 실제 징수액이 아니다. now는 관측을
적용하는 시각이지 브로커 실행 시각의 증명이 아니다. 날짜 인계/초기 원가 불명은
복구 담당의 영역으로 남기고 여기에서 초기화하거나 추정하지 않는다.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import math
from zoneinfo import ZoneInfo

from ...core.types import Portfolio, Position, PositionSide, Market, TimeHorizon, RiskMetrics
from ...risk.manager import DailyStats
from ...utils.fee_calculator import FeeCalculator
from .application import FillObservation, FillDelta
from .lifecycle import OrderRef, clear_settled_pending_sector
from .resources import remaining_resource_amount


_KST = ZoneInfo("Asia/Seoul")
_PORTFOLIO_FIELDS = {"schema", "cash", "positions", "initial_capital", "market", "currency",
                     "daily_pnl", "daily_trades", "daily_start_unrealized_pnl"}
_POSITION_FIELDS = {"symbol", "name", "side", "quantity", "avg_price", "current_price", "market",
                    "currency", "stop_loss", "take_profit", "trailing_stop_pct", "highest_price",
                    "strategy", "entry_time", "sector", "time_horizon", "trade_id", "entry_signal_score"}
_DAILY_FIELDS = {"date", "trades", "wins", "losses", "total_pnl", "max_drawdown", "consecutive_losses", "peak_equity"}
_RISK_FIELDS = {"schema", "day", "daily_stats", "consecutive_losses", "stop_loss_today",
                "stop_loss_rebound_used", "exited_today", "daily_exit_count", "daily_exit_count_date",
                "counted_buy_orders", "count_loss_intents", "buy_fee_remaining", "cost_basis_remaining"}


def _fields(value, expected):
    if type(value) is not dict or value.keys() != expected:
        raise ValueError("checkpoint 필드 불일치")


def _decimal(value, *, nonnegative=False) -> Decimal:
    if type(value) is not str:
        raise ValueError("금액은 Decimal 문자열이어야 합니다")
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise ValueError("잘못된 금액") from None
    if not result.is_finite() or (nonnegative and result < 0):
        raise ValueError("비유한 또는 음수 금액")
    return result


def _integer(value):
    if type(value) is not int or value < 0:
        raise ValueError("수량/카운트는 0 이상 정수여야 합니다")
    return value


def _text(value, *, optional=False, nonempty=False):
    if value is None and optional:
        return value
    if type(value) is not str or (nonempty and (not value or value != value.strip())):
        raise ValueError("잘못된 문자열")
    return value


def _day(value):
    try:
        if type(value) is not str or date.fromisoformat(value).isoformat() != value:
            raise ValueError
    except ValueError:
        raise ValueError("ISO 거래일이 필요합니다") from None
    return value


def _time(value, *, legacy=False):
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value) if type(value) is str else value
        if not isinstance(parsed, datetime):
            raise ValueError
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            if not legacy:
                raise ValueError
            parsed = parsed.replace(tzinfo=_KST)
        return parsed
    except (ValueError, TypeError):
        raise ValueError("aware 시각이 필요합니다") from None


def _number(value):
    if value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
        raise ValueError("유한한 수치가 필요합니다")
    return value


def _position_dto(position: Position) -> dict:
    timestamp = _time(position.entry_time, legacy=True)
    return {"symbol": position.symbol, "name": position.name, "side": position.side.value,
            "quantity": position.quantity, "avg_price": str(position.avg_price),
            "current_price": str(position.current_price), "market": position.market.value,
            "currency": position.currency, "stop_loss": None if position.stop_loss is None else str(position.stop_loss),
            "take_profit": None if position.take_profit is None else str(position.take_profit),
            "trailing_stop_pct": position.trailing_stop_pct,
            "highest_price": None if position.highest_price is None else str(position.highest_price),
            "strategy": position.strategy, "entry_time": timestamp.isoformat() if timestamp else None,
            "sector": position.sector, "time_horizon": position.time_horizon.value if position.time_horizon else None,
            "trade_id": position.trade_id, "entry_signal_score": getattr(position, "entry_signal_score", None)}


def _decode_position(row: dict) -> Position:
    _fields(row, _POSITION_FIELDS)
    if row["market"] not in ("KRX", "KRX_EXT") or row["currency"] != "KRW":
        raise ValueError("KR/KRW만 지원합니다")
    if row["side"] not in ("flat", "long"):
        raise ValueError("KR long 포지션만 지원합니다")
    if row["entry_time"] is not None and type(row["entry_time"]) is not str:
        raise ValueError("DTO 시각은 aware ISO 문자열이어야 합니다")
    values = {key: row[key] for key in ("symbol", "name", "strategy", "sector", "trade_id")}
    _text(values["symbol"], nonempty=True)
    _text(values["name"])
    for key in ("strategy", "sector", "trade_id"):
        _text(values[key], optional=True)
    for key in ("avg_price", "current_price"):
        values[key] = _decimal(row[key], nonnegative=True)
    for key in ("stop_loss", "take_profit", "highest_price"):
        values[key] = None if row[key] is None else _decimal(row[key], nonnegative=True)
    values.update(quantity=_integer(row["quantity"]), side=PositionSide(row["side"]),
                  market=Market(row["market"]), currency="KRW", entry_time=_time(row["entry_time"]),
                  time_horizon=None if row["time_horizon"] is None else TimeHorizon(row["time_horizon"]),
                  trailing_stop_pct=_number(row["trailing_stop_pct"]))
    position = Position(**values)
    position.entry_signal_score = _number(row["entry_signal_score"])
    return position


def encode_portfolio(portfolio: Portfolio) -> dict:
    dto = {"schema": 1, "cash": str(portfolio.cash), "initial_capital": str(portfolio.initial_capital),
           "positions": {key: _position_dto(value) for key, value in portfolio.positions.items()},
           "market": portfolio.market.value, "currency": portfolio.currency,
           "daily_pnl": str(portfolio.daily_pnl), "daily_trades": portfolio.daily_trades,
           "daily_start_unrealized_pnl": str(portfolio.daily_start_unrealized_pnl)}
    decode_portfolio(dto)
    return dto


def decode_portfolio(dto: dict) -> Portfolio:
    _fields(dto, _PORTFOLIO_FIELDS)
    if type(dto["schema"]) is not int or dto["schema"] != 1:
        raise ValueError("미지원 Portfolio schema")
    if dto["market"] not in ("KRX", "KRX_EXT") or dto["currency"] != "KRW" or type(dto["positions"]) is not dict:
        raise ValueError("KR/KRW Portfolio가 필요합니다")
    positions = {}
    for key, row in dto["positions"].items():
        position = _decode_position(row)
        if key != position.symbol:
            raise ValueError("포지션 종목 키 불일치")
        positions[key] = position
    return Portfolio(cash=_decimal(dto["cash"]), positions=positions,
                     initial_capital=_decimal(dto["initial_capital"], nonnegative=True),
                     market=Market(dto["market"]), currency="KRW", daily_pnl=_decimal(dto["daily_pnl"]),
                     daily_trades=_integer(dto["daily_trades"]),
                     daily_start_unrealized_pnl=_decimal(dto["daily_start_unrealized_pnl"]))


def new_risk_state(day: str) -> dict:
    _day(day)
    return {"schema": 1, "day": day,
            "daily_stats": {"date": day, "trades": 0, "wins": 0, "losses": 0, "total_pnl": "0",
                            "max_drawdown": "0", "consecutive_losses": 0, "peak_equity": "0"},
            "consecutive_losses": 0, "stop_loss_today": [], "stop_loss_rebound_used": [],
            "exited_today": {}, "daily_exit_count": 0, "daily_exit_count_date": day,
            "counted_buy_orders": [], "count_loss_intents": [],
            "buy_fee_remaining": {}, "cost_basis_remaining": {}}


def validate_risk(dto: dict) -> dict:
    _fields(dto, _RISK_FIELDS)
    if type(dto["schema"]) is not int or dto["schema"] != 1:
        raise ValueError("미지원 risk schema")
    day = _day(dto["day"])
    stats = dto["daily_stats"]
    _fields(stats, _DAILY_FIELDS)
    if stats["date"] != day or dto["daily_exit_count_date"] != day:
        raise ValueError("위험 상태 거래일 불일치")
    for key in ("trades", "wins", "losses", "consecutive_losses"):
        _integer(stats[key])
    for key in ("total_pnl", "max_drawdown", "peak_equity"):
        _decimal(stats[key], nonnegative=key != "total_pnl")
    _integer(dto["consecutive_losses"])
    _integer(dto["daily_exit_count"])
    for key in ("stop_loss_today", "stop_loss_rebound_used", "counted_buy_orders", "count_loss_intents"):
        items = dto[key]
        if type(items) is not list:
            raise ValueError("위험 키 목록이 필요합니다")
        for item in items:
            _text(item, nonempty=True)
        if len(set(items)) != len(items):
            raise ValueError("중복 위험 키")
    for key in ("buy_fee_remaining", "cost_basis_remaining"):
        if type(dto[key]) is not dict:
            raise ValueError("원가/비용 잔액 mapping이 필요합니다")
        for symbol, amount in dto[key].items():
            _text(symbol, nonempty=True)
            _decimal(amount, nonnegative=True)
    if type(dto["exited_today"]) is not dict:
        raise ValueError("청산 정보 mapping이 필요합니다")
    for symbol, row in dto["exited_today"].items():
        _text(symbol, nonempty=True)
        _fields(row, {"price", "time", "sector"})
        if _decimal(row["price"], nonnegative=True) <= 0:
            raise ValueError("청산 가격이 필요합니다")
        if type(row["time"]) is not str:
            raise ValueError("DTO 시각은 aware ISO 문자열이어야 합니다")
        when = _time(row["time"])
        if when is None or when.astimezone(_KST).date().isoformat() != day:
            raise ValueError("청산 거래일 불일치")
        _text(row["sector"])
    return deepcopy(dto)


def encode_risk(manager, *, day: str) -> dict:
    if manager.market != "KR":
        raise ValueError("KR 위험 상태만 지원합니다")
    dto = new_risk_state(day)
    stats = manager.daily_stats
    dto["daily_stats"] = {"date": stats.date.isoformat(), "trades": stats.trades, "wins": stats.wins,
                          "losses": stats.losses, "total_pnl": str(stats.total_pnl),
                          "max_drawdown": str(stats.max_drawdown), "consecutive_losses": stats.consecutive_losses,
                          "peak_equity": str(stats.peak_equity)}
    dto.update(consecutive_losses=manager._consecutive_losses, stop_loss_today=sorted(manager._stop_loss_today),
               stop_loss_rebound_used=sorted(manager._stop_loss_rebound_used), daily_exit_count=manager._daily_exit_count,
               daily_exit_count_date=manager._daily_exit_count_date.isoformat())
    dto["exited_today"] = {symbol: {"price": str(row["price"]), "time": _time(row["time"], legacy=True).isoformat(),
                                      "sector": row["sector"]} for symbol, row in manager._exited_today.items()}
    for key in ("counted_buy_orders", "count_loss_intents"):
        dto[key] = sorted(getattr(manager, "_execution_" + key, set()))
    for key in ("buy_fee_remaining", "cost_basis_remaining"):
        dto[key] = {symbol: str(value) for symbol, value in getattr(manager, "_execution_" + key, {}).items()}
    return validate_risk(dto)


def publish_risk(manager, dto: dict) -> None:
    value = validate_risk(dto)
    stats = dict(value["daily_stats"])
    stats["date"] = date.fromisoformat(stats["date"])
    for key in ("total_pnl", "max_drawdown", "peak_equity"):
        stats[key] = Decimal(stats[key])
    daily_stats = DailyStats(**stats)
    if manager.daily_stats.date != daily_stats.date:
        manager.metrics = RiskMetrics()
        manager._last_exit_cooldown_log.clear()
    # 기존 risk 메서드는 naive datetime.now()를 사용한다. live projection만 명시적
    # KST naive로 내보내며, durable checkpoint의 aware 시각은 바꾸지 않는다.
    exits = {symbol: {"price": _number(float(Decimal(row["price"]))),
                      "time": _time(row["time"]).astimezone(_KST).replace(tzinfo=None).isoformat(), "sector": row["sector"]}
             for symbol, row in value["exited_today"].items()}
    manager.daily_stats = daily_stats
    manager._consecutive_losses = value["consecutive_losses"]
    manager._stop_loss_today = set(value["stop_loss_today"])
    manager._stop_loss_rebound_used = set(value["stop_loss_rebound_used"])
    manager._exited_today = exits
    manager._daily_exit_count = value["daily_exit_count"]
    manager._daily_exit_count_date = date.fromisoformat(value["day"])
    for key in ("counted_buy_orders", "count_loss_intents"):
        setattr(manager, "_execution_" + key, set(value[key]))
    for key in ("buy_fee_remaining", "cost_basis_remaining"):
        setattr(manager, "_execution_" + key, {symbol: Decimal(amount) for symbol, amount in value[key].items()})
    manager.metrics.daily_trades = daily_stats.trades
    manager.metrics.consecutive_losses = daily_stats.consecutive_losses


@dataclass(frozen=True)
class EconomicReduction:
    state: dict
    before_position: Position | None
    after_position: Position | None
    fill_kind: str
    attempt_id: str
    intent_id: str


def _matching_attempt(state, observation, delta):
    matches = []
    for key, attempt in state.get("attempts", {}).items():
        if attempt.get("kind") != "submit" or not attempt.get("order_ref"):
            continue
        ref = OrderRef.from_dict(attempt["order_ref"])
        if ref.key == observation.order_key:
            matches.append((key, attempt))
    if len(matches) != 1:
        raise ValueError("단일 submit 소유권이 필요합니다")
    key, attempt = matches[0]
    if (attempt.get("attempt_id") != key or attempt.get("symbol") != observation.symbol
            or attempt.get("side") != observation.side.lower()
            or attempt.get("evidence_conflict")
            or attempt.get("state") not in ("open", "partial", "final_filled", "final_cancelled", "final_expired")):
        raise ValueError("주문 소유권/관측 상태 불일치")
    intent_id = _text(attempt.get("intent_id"), nonempty=True)
    intent = state.get("intents", {}).get(intent_id, {})
    if (intent.get("symbol") != observation.symbol or intent.get("side") != observation.side.lower()
            or key not in intent.get("attempt_ids", [])):
        raise ValueError("intent 소유권 불일치")
    applied, observed, ordered = (_integer(attempt[name]) for name in ("applied_quantity", "observed_quantity", "quantity"))
    cursor = state.get("cursors", {}).get(observation.order_key)
    old_qty = 0 if cursor is None else _integer(cursor["quantity"])
    old_amount = Decimal("0") if cursor is None else _decimal(cursor["amount"], nonnegative=True)
    if cursor is not None and cursor["identity"] != observation.identity:
        raise ValueError("체결 identity 불일치")
    if observation.side == "BUY" and ((observation.order_key in state["risk"]["counted_buy_orders"]) != (old_qty > 0)):
        raise ValueError("주문 카운트/cursor 불일치")
    observed_amount = _decimal(attempt["observed_amount"], nonnegative=True)
    if (applied != old_qty or not old_qty < observation.cumulative_quantity <= observed <= ordered
            or delta.quantity != observation.cumulative_quantity - old_qty
            or delta.amount != observation.cumulative_amount - old_amount
            or delta.amount <= 0 or delta.fee != 0
            or observed_amount < observation.cumulative_amount
            or (observed == observation.cumulative_quantity and observed_amount != observation.cumulative_amount)):
        raise ValueError("체결 cursor/누적 관측 불일치")
    if any(row.get("order_key") == observation.order_key and row.get("status") == "NEEDS_RECONCILIATION"
           for row in state.get("inbox", {}).values()):
        raise ValueError("미해결 주문 충돌")
    return key, intent_id, old_amount


def _entry_sector(attempt, metadata):
    """주문이 소유한 분류를 체결로 이관한다. 관측 metadata는 legacy 전용이다."""
    if 'request_binding' not in attempt:
        return metadata.get('sector')
    binding = attempt['request_binding']
    if type(binding) is not dict or 'sector' not in binding or 'sector' not in attempt:
        raise ValueError('invalid_bound_sector')
    sector = binding['sector']
    if sector is not None and (type(sector) is not str or not sector or sector.strip() != sector):
        raise ValueError('invalid_bound_sector')
    if type(attempt['sector']) is not type(sector) or attempt['sector'] != sector:
        raise ValueError('conflicting_bound_sector')
    return sector  # 명시 None도 관측으로 추측·대체하지 않는다.


def reduce_economics(state: dict, observation: FillObservation, delta: FillDelta, *, now: datetime) -> EconomicReduction:
    now = _time(now)
    if now is None or observation.market != "KR" or observation.exchange != "KRX" or observation.cumulative_fee != 0:
        raise ValueError("지원하지 않는 시장/실측 비용")
    if type(delta.quantity) is not int or delta.quantity <= 0:
        raise ValueError("양수 체결 증분이 필요합니다")
    if any(type(value) is not Decimal or not value.is_finite() for value in (delta.amount, delta.fee)):
        raise ValueError("증분 대금/비용은 유한한 Decimal이어야 합니다")
    metadata = observation.metadata
    if not metadata.keys() <= {"name", "sector", "entry_signal_score", "registration_params", "exit_type"}:
        raise ValueError("알 수 없는 체결 metadata")
    for key in ("name", "sector", "exit_type"):
        if key in metadata: _text(metadata[key])
    if "entry_signal_score" in metadata: _number(metadata["entry_signal_score"])
    portfolio = decode_portfolio(state["portfolio"])
    risk = validate_risk(state["risk"])
    if risk["daily_stats"]["trades"] != portfolio.daily_trades:
        raise ValueError("Portfolio/risk 거래 카운트 불일치")
    for key in ("buy_fee_remaining", "cost_basis_remaining"):
        if not risk[key].keys() <= portfolio.positions.keys():
            raise ValueError("미보유 종목의 원가/비용 잔액")
    if not risk["day"] == now.astimezone(_KST).date().isoformat() == observation.trading_day:
        raise ValueError("거래일 인계/대사가 필요합니다")
    attempt_id, intent_id, old_amount = _matching_attempt(state, observation, delta)
    candidate = deepcopy(state)
    candidate["risk"] = risk
    attempt = candidate["attempts"][attempt_id]
    before = portfolio.positions.get(observation.symbol)
    before_snapshot = None if before is None else _decode_position(_position_dto(before))
    price = delta.amount / delta.quantity
    fees = FeeCalculator()
    fee_fn = fees.calculate_buy_fee if observation.side == "BUY" else fees.calculate_sell_fee
    estimated_fee = fee_fn(observation.cumulative_amount) - fee_fn(old_amount)
    symbol, key = observation.symbol, observation.order_key
    realized = Decimal("0")
    lots = candidate.setdefault("lots", {})
    if observation.side == "BUY":
        entry_sector = _entry_sector(attempt, metadata)
        existing_lot = lots.get(key)
        old_quantity = observation.cumulative_quantity - delta.quantity
        if (existing_lot is None) != (old_quantity == 0):
            raise ValueError("기존 applied 체결과 진입 lot 존재 여부 불일치")
        if existing_lot is not None and (existing_lot["closed"] or before is None):
            raise ValueError("종결된 진입의 늦은 체결은 대사가 필요합니다")
        is_first = existing_lot is None
        if not is_first:
            original_kind = existing_lot["kind"]
            base_quantity = _integer(existing_lot["pre_buy_quantity"])
            if (OrderRef.from_dict(existing_lot["order_ref"]).key != key
                    or existing_lot["symbol"] != symbol
                    or existing_lot["attempt_id"] != attempt_id or existing_lot["intent_id"] != intent_id
                    or original_kind not in ("initial_entry", "distinct_add_on")
                    or (original_kind == "initial_entry") != (base_quantity == 0)
                    or (original_kind == "initial_entry" and existing_lot["lifecycle_id"] != key)
                    or _integer(existing_lot["quantity"]) != old_quantity
                    or _decimal(existing_lot["amount"], nonnegative=True) != old_amount):
                raise ValueError("진입 lot 소유권/분류/누적 cursor 불일치")
            fill_kind = "same_entry_fill" if existing_lot["kind"] == "initial_entry" else "distinct_add_on"
        else:
            fill_kind = "initial_entry" if before is None else "distinct_add_on"
            lots[key] = {"symbol": symbol, "order_ref": deepcopy(attempt["order_ref"]), "intent_id": intent_id,
                         "attempt_id": attempt_id, "lifecycle_id": key if before is None else None,
                         "kind": fill_kind, "pre_buy_quantity": 0 if before is None else before.quantity,
                         "quantity": 0, "amount": "0", "initial_r_status": "pending", "initial_r": None,
                         "fee_basis": "estimated_order_cumulative", "closed": False}
        if before is None:
            position = Position(symbol, name=metadata.get("name", ""), side=PositionSide.LONG,
                                strategy=attempt.get("strategy"), entry_time=now, sector=entry_sector)
            position.entry_signal_score = metadata.get("entry_signal_score")
            portfolio.positions[symbol] = position
            risk["cost_basis_remaining"][symbol] = "0"
            risk["buy_fee_remaining"][symbol] = "0"
        else:
            position = before
            if symbol not in risk["cost_basis_remaining"]:
                raise ValueError("기존 진입 원가가 미측정입니다")
        cost = Decimal(risk["cost_basis_remaining"][symbol]) + delta.amount
        position.quantity += delta.quantity
        position.avg_price = cost / position.quantity
        position.current_price = price
        if position.highest_price is None or price > position.highest_price:
            position.highest_price = price
        portfolio.cash -= delta.amount + estimated_fee
        risk["cost_basis_remaining"][symbol] = str(cost)
        if symbol in risk["buy_fee_remaining"]:
            risk["buy_fee_remaining"][symbol] = str(Decimal(risk["buy_fee_remaining"][symbol]) + estimated_fee)
        lots[key].update(quantity=observation.cumulative_quantity, amount=str(observation.cumulative_amount))
        if key not in risk["counted_buy_orders"]:
            risk["counted_buy_orders"].append(key)
            portfolio.daily_trades += 1
        if is_first and symbol in risk["stop_loss_today"] and symbol not in risk["stop_loss_rebound_used"]:
            risk["stop_loss_rebound_used"].append(symbol)
    else:
        fill_kind = "exit"
        if before is None or delta.quantity > before.quantity:
            raise ValueError("미보유/초과 매도는 대사가 필요합니다")
        if symbol not in risk["buy_fee_remaining"] or symbol not in risk["cost_basis_remaining"]:
            raise ValueError("기존 매수 원가/비용이 미측정입니다")
        basis, buy_fee = Decimal(risk["cost_basis_remaining"][symbol]), Decimal(risk["buy_fee_remaining"][symbol])
        full = delta.quantity == before.quantity
        allocated_cost = basis if full else basis * delta.quantity / before.quantity
        allocated_fee = buy_fee if full else buy_fee * delta.quantity / before.quantity
        realized = delta.amount - allocated_cost - allocated_fee - estimated_fee
        portfolio.cash += delta.amount - estimated_fee
        portfolio.daily_pnl += realized
        before.quantity -= delta.quantity
        risk["cost_basis_remaining"][symbol] = str(basis - allocated_cost)
        risk["buy_fee_remaining"][symbol] = str(buy_fee - allocated_fee)
        exit_type = metadata.get("exit_type", "")
        if (exit_type in {"stop_loss", "breakeven", "emergency_stop"}
                and intent_id not in risk["count_loss_intents"]):
            risk["count_loss_intents"].append(intent_id)
            if symbol not in risk["exited_today"]:
                risk["daily_exit_count"] += 1
        if exit_type in {"stop_loss", "emergency_stop"} and symbol not in risk["stop_loss_today"]:
            risk["stop_loss_today"].append(symbol)
        if full:
            risk["exited_today"].setdefault(symbol, {"price": str(price), "time": now.isoformat(), "sector": before.sector or ""})
            del portfolio.positions[symbol]
            del risk["cost_basis_remaining"][symbol]
            del risk["buy_fee_remaining"][symbol]
            for lot in lots.values():
                if lot["symbol"] == symbol: lot["closed"] = True
    reserved = _integer(attempt["reserved_quantity"])
    reserved_cash = _decimal(attempt["reserved_cash"], nonnegative=True)
    remaining = max(0, reserved - delta.quantity)
    next_cash = (min(reserved_cash, (reserved_cash * remaining / reserved).quantize(Decimal("1"), rounding=ROUND_CEILING))
                 if reserved else reserved_cash)
    resource_fields = {'reserved_exposure', 'reserved_planned_risk'}
    if resource_fields & attempt.keys():
        if not resource_fields <= attempt.keys():
            raise ValueError('incomplete_request_resource_reservation')
        for field in resource_fields:
            original = attempt[field]
            if original is None and field == 'reserved_planned_risk':
                continue  # 미측정은 0으로 바꾸지 않는다.
            amount = _decimal(original, nonnegative=True)
            attempt[field] = str(remaining_resource_amount(amount, reserved, remaining))
    attempt.update(applied_quantity=observation.cumulative_quantity, reserved_quantity=remaining, reserved_cash=str(next_cash))
    clear_settled_pending_sector(candidate, attempt_id)
    risk["daily_stats"]["trades"] = portfolio.daily_trades
    candidate["portfolio"] = encode_portfolio(portfolio)
    validate_risk(risk)
    after = portfolio.positions.get(symbol)
    candidate.setdefault("outbox", {})[observation.observation_id] = {
        "status": "pending", "order_key": key, "attempt_id": attempt_id, "intent_id": intent_id,
        "observation": observation.to_dict(), "quantity": delta.quantity, "amount": str(delta.amount),
        "estimated_fee": str(estimated_fee), "fee_basis": "estimated_order_cumulative", "realized_pnl": str(realized),
        "before_position": None if before_snapshot is None else _position_dto(before_snapshot),
        "after_position": None if after is None else _position_dto(after),
        "portfolio": deepcopy(candidate["portfolio"]), "risk": deepcopy(risk)}
    return EconomicReduction(candidate, before_snapshot, after, fill_kind, attempt_id, intent_id)
