"""on_signal 진입부 stale 루프 결함 수정 (2026-09-21)

1. 동시호가(15:20~15:30): 90초 넘은 미체결 SELL 을 **취소하지 않는다** — 시장가 재주문이 불가한
   시간대라 취소를 먼저 보내면 청산 주문이 재주문 없이 거래소에서 사라졌다.
5. 10분 BUY 정리: 취소 0건에는 "추적 주문 없음(소멸)"과 "취소 실패(방금 체결됐거나 거래소에 생존)"가
   섞여 있다(브로커 cancel 은 실패를 예외로 올리지 않고 0 을 돌려준다). 브로커가 아직 활성 주문을
   추적하면 pending·예약현금을 유지하고, 한 주기 뒤에도 그대로면 거래소 실 미체결로 가린다.

실행: venv/bin/python -m pytest tests/test_engine_stale_pending_fixes.py -q -p no:cacheprovider
합성 브로커만 사용 — 네트워크·운영 캐시 무접촉. 시계는 engine 모듈의 datetime 을 동결한다.
"""
import asyncio
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import src.core.engine as eng  # noqa: E402
from src.core.engine import RiskManager  # noqa: E402
from src.core.event import SignalEvent  # noqa: E402
from src.core.types import (  # noqa: E402
    MarketSession, Order, OrderSide, OrderStatus, OrderType, Position, Signal,
    SignalStrength, StrategyType,
)

SYM = "005930"
OTHER = "000660"   # 루프를 구동하는 다른 종목의 SIGNAL
PRICE = Decimal("10000")


class LegacyBroker:
    """취소·송신·인메모리 조회만 있는 합성 브로커 (거래소 실 미체결 조회 수단 없음)."""

    def __init__(self, *, cancelled=0, tracked=(), exchange_rows=()):
        self.calls = []
        self._cancelled = cancelled
        self._tracked = tracked if isinstance(tracked, Exception) else list(tracked)
        self._exchange_rows = exchange_rows
        self.on_open_orders = None

    async def cancel_all_for_symbol(self, symbol):
        self.calls.append("cancel")
        return self._cancelled

    async def submit_order(self, order):
        self.calls.append("submit")
        return True, "ORD-1"

    async def get_open_orders(self):
        if isinstance(self._tracked, Exception):
            raise self._tracked
        if self.on_open_orders is not None:
            await self.on_open_orders()
        return list(self._tracked)



class Broker(LegacyBroker):
    """KISKRBroker 처럼 거래소 실 미체결 조회(TTTC8036R 대응)까지 있는 합성 브로커."""

    async def get_exchange_open_orders(self):
        self.calls.append("exchange")
        return None if self._exchange_rows is None else list(self._exchange_rows)


def _tracked_buy(status=OrderStatus.SUBMITTED):
    o = Order(symbol=SYM, side=OrderSide.BUY, order_type=OrderType.LIMIT,
              quantity=10, price=PRICE)
    o.status = status
    return o


def _rm(monkeypatch, broker, now, *, positions=None):
    """on_signal 을 stale 루프까지만 구동한다 — 직후 '거래 시간 외 차단'으로 반환."""
    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return now
    monkeypatch.setattr(eng, "datetime", _Frozen)

    rm = object.__new__(RiskManager)
    rm._order_fail_cooldown, rm._COOLDOWN_SECONDS = {}, 300
    rm._last_signal_time, rm._SIGNAL_COOLDOWN_SECONDS = {}, 30
    rm._PENDING_TIMEOUT_SECONDS = 600
    rm._pending_orders, rm._pending_quantities = set(), {}
    rm._pending_timestamps, rm._pending_sides = {}, {}
    rm._pending_fallback_count, rm._reserved_by_order = {}, {}
    rm._pending_strategy, rm._pending_signal_cache = {}, {}
    rm._pending_lock = asyncio.Lock()
    rm.engine = SimpleNamespace(
        broker=broker,
        portfolio=SimpleNamespace(positions=positions if positions is not None else {}),
        is_trading_hours=lambda: False,
        _get_current_session=lambda: MarketSession.CLOSED,
        _pending_sector_map={},
    )
    return rm


def _stale(rm, side, now, *, seconds, reserved=None):
    rm._pending_orders.add(SYM)
    rm._pending_sides[SYM] = side
    rm._pending_quantities[SYM] = 10
    rm._pending_timestamps[SYM] = now - timedelta(seconds=seconds)
    if reserved is not None:
        rm._reserved_by_order[SYM] = reserved


def _drive(rm):
    sig = Signal(symbol=OTHER, side=OrderSide.BUY, strength=SignalStrength.NORMAL,
                 strategy=StrategyType.SEPA_TREND, price=PRICE, reason="시험 구동")
    return asyncio.run(rm.on_signal(SignalEvent.from_signal(sig, source="test")))


def _held():
    return {SYM: Position(symbol=SYM, quantity=10, avg_price=PRICE, current_price=PRICE,
                          strategy="sepa_trend")}


# ── 1. 동시호가 ──────────────────────────────────────────────────────────────

def test_closing_auction_keeps_limit_sell_on_exchange(monkeypatch):
    now = datetime(2026, 9, 21, 15, 25)
    broker = Broker(cancelled=1)
    rm = _rm(monkeypatch, broker, now, positions=_held())
    _stale(rm, OrderSide.SELL, now, seconds=200)

    _drive(rm)

    assert broker.calls == []          # 취소도 재주문도 보내지 않는다 — 원 지정가 유지
    assert SYM in rm._pending_orders   # 살아 있는 주문이므로 pending 도 유지


def test_closing_auction_still_releases_pending_of_vanished_position(monkeypatch):
    """회귀 방지: 포지션이 없는 pending 은 동시호가에도 종전대로 취소 → 해제(15:30 뒤엔 정리 주체가 없다)."""
    now = datetime(2026, 9, 21, 15, 25)
    broker = Broker(cancelled=0)
    rm = _rm(monkeypatch, broker, now, positions={})
    _stale(rm, OrderSide.SELL, now, seconds=200)

    _drive(rm)

    assert broker.calls == ["cancel"]
    assert SYM not in rm._pending_orders


def test_closing_auction_still_releases_after_max_fallback(monkeypatch):
    now = datetime(2026, 9, 21, 15, 25)
    broker = Broker(cancelled=1)
    rm = _rm(monkeypatch, broker, now, positions=_held())
    _stale(rm, OrderSide.SELL, now, seconds=200)
    rm._pending_fallback_count[SYM] = 2

    _drive(rm)

    assert broker.calls == [] and SYM not in rm._pending_orders


def test_regular_hours_fallback_unchanged(monkeypatch):
    """회귀 방지: 정규장(15:20 전)은 종전대로 취소 → 시장가 재주문."""
    now = datetime(2026, 9, 21, 15, 19)
    broker = Broker(cancelled=1)
    rm = _rm(monkeypatch, broker, now, positions=_held())
    _stale(rm, OrderSide.SELL, now, seconds=200)

    _drive(rm)

    assert broker.calls == ["cancel", "submit"]
    assert rm._pending_fallback_count[SYM] == 1


# ── 5. 10분 BUY 정리의 취소 0건 ──────────────────────────────────────────────

@pytest.mark.parametrize("status", [OrderStatus.SUBMITTED, OrderStatus.PARTIAL])
def test_zero_cancel_with_tracked_order_keeps_reservation(monkeypatch, status):
    """취소 0건 + 브로커가 활성 주문 추적 중 = 취소 실패. 예약을 풀면 즉시 재매수(이중 매수)."""
    now = datetime(2026, 9, 21, 10, 30)
    broker = Broker(cancelled=0, tracked=[_tracked_buy(status)])
    rm = _rm(monkeypatch, broker, now)
    _stale(rm, OrderSide.BUY, now, seconds=700, reserved=Decimal("101500"))

    _drive(rm)

    assert SYM in rm._pending_orders
    assert rm._reserved_by_order[SYM] == Decimal("101500")
    assert "exchange" not in broker.calls   # 첫 실패는 조회 없이 한 주기 대기(체결이면 FillEvent 가 지운다)
    assert rm._pending_fallback_count[SYM] == 1
    # 60초 뒤 재시도 — SIGNAL 마다 취소 API 를 때리지 않는다
    assert (now - rm._pending_timestamps[SYM]).total_seconds() == 600 - 60


@pytest.mark.parametrize("rows", [[{"symbol": SYM, "side": "buy", "qty": 10}], None],
                         ids=["거래소 생존", "조회 실패(판단 불가)"])
def test_zero_cancel_retry_keeps_while_alive_on_exchange(monkeypatch, rows):
    now = datetime(2026, 9, 21, 10, 30)
    broker = Broker(cancelled=0, tracked=[_tracked_buy()], exchange_rows=rows)
    rm = _rm(monkeypatch, broker, now)
    _stale(rm, OrderSide.BUY, now, seconds=700, reserved=Decimal("101500"))
    rm._pending_fallback_count[SYM] = 1    # 이미 한 주기 기다렸다

    _drive(rm)

    assert SYM in rm._pending_orders and SYM in rm._reserved_by_order
    assert "exchange" in broker.calls
    assert rm._pending_fallback_count[SYM] == 2
    assert (now - rm._pending_timestamps[SYM]).total_seconds() == 600 - 60


def test_zero_cancel_retry_releases_when_gone_from_exchange(monkeypatch):
    """한 주기 뒤에도 pending 인데(체결 아님) 거래소에 미체결이 없다 = 소멸. 영구 잠김 방지."""
    now = datetime(2026, 9, 21, 10, 30)
    broker = Broker(cancelled=0, tracked=[_tracked_buy()],
                    exchange_rows=[{"symbol": OTHER, "side": "buy", "qty": 1},
                                   {"symbol": SYM, "side": "sell", "qty": 1}])
    rm = _rm(monkeypatch, broker, now)
    _stale(rm, OrderSide.BUY, now, seconds=700, reserved=Decimal("101500"))
    rm._pending_fallback_count[SYM] = 1

    _drive(rm)

    assert SYM not in rm._pending_orders and SYM not in rm._reserved_by_order
    assert SYM in rm._order_fail_cooldown


def test_zero_cancel_without_tracked_order_still_releases(monkeypatch):
    """2026-08-04 P1 보존: 추적 주문이 없으면(이미 소멸) 0건도 해제 — 예약현금 잠김 방지."""
    now = datetime(2026, 9, 21, 10, 30)
    broker = Broker(cancelled=0, tracked=[])
    rm = _rm(monkeypatch, broker, now)
    _stale(rm, OrderSide.BUY, now, seconds=700, reserved=Decimal("101500"))

    _drive(rm)

    assert SYM not in rm._pending_orders and SYM not in rm._reserved_by_order
    assert "exchange" not in broker.calls
    # '미추적'은 방금 체결(FillEvent 대기 중)일 수도 있다 — 포지션 반영 전 같은 종목 재매수 차단
    assert SYM in rm._order_fail_cooldown


def test_cancel_ack_releases(monkeypatch):
    now = datetime(2026, 9, 21, 10, 30)
    broker = Broker(cancelled=1, tracked=[])
    rm = _rm(monkeypatch, broker, now)
    _stale(rm, OrderSide.BUY, now, seconds=700, reserved=Decimal("101500"))

    _drive(rm)

    assert SYM not in rm._pending_orders and SYM not in rm._reserved_by_order
    assert SYM not in rm._order_fail_cooldown   # 취소 ACK 뒤 재진입은 종전대로 허용


def test_confirmed_alive_order_is_never_force_released(monkeypatch):
    """거래소 생존이 확인된 주문은 유지 상한과 무관하게 pending·예약을 지킨다 — 살아 있는 주문을 잊지 않는다."""
    now = datetime(2026, 9, 21, 10, 30)
    broker = Broker(cancelled=0, tracked=[_tracked_buy()],
                    exchange_rows=[{"symbol": SYM, "side": "buy", "qty": 10}])
    rm = _rm(monkeypatch, broker, now)
    _stale(rm, OrderSide.BUY, now, seconds=700, reserved=Decimal("101500"))
    rm._pending_fallback_count[SYM] = 40

    _drive(rm)

    assert SYM in rm._pending_orders and rm._reserved_by_order[SYM] == Decimal("101500")
    assert SYM not in rm._order_fail_cooldown


def test_partial_fill_position_releases_so_exits_are_not_blocked(monkeypatch):
    """부분체결로 포지션이 있으면 종전대로 해제한다 — pending 은 그 종목의 손절 신호까지 막기 때문."""
    now = datetime(2026, 9, 21, 10, 30)
    broker = Broker(cancelled=0, tracked=[_tracked_buy(OrderStatus.PARTIAL)])
    rm = _rm(monkeypatch, broker, now, positions=_held())
    _stale(rm, OrderSide.BUY, now, seconds=700, reserved=Decimal("60900"))

    _drive(rm)

    assert SYM not in rm._pending_orders and SYM not in rm._reserved_by_order
    assert "exchange" not in broker.calls


def test_keep_is_bounded_even_if_exchange_query_keeps_failing(monkeypatch):
    """조회 실패(None)가 이어져도 예약현금이 자정까지 잠기지 않는다 — 상한에서 강제 해제."""
    now = datetime(2026, 9, 21, 10, 30)
    broker = Broker(cancelled=0, tracked=[_tracked_buy()], exchange_rows=None)
    rm = _rm(monkeypatch, broker, now)
    _stale(rm, OrderSide.BUY, now, seconds=700, reserved=Decimal("101500"))
    rm._pending_fallback_count[SYM] = 15

    _drive(rm)

    assert "exchange" in broker.calls   # 상한에서도 먼저 한 번 더 확인한다
    assert SYM not in rm._pending_orders and SYM not in rm._reserved_by_order
    assert SYM in rm._order_fail_cooldown   # 살아 있을 수 있는 원 주문 위 재매수 차단(BUY 전용 쿨다운)


def test_retry_without_exchange_query_support_releases(monkeypatch):
    """거래소 조회 수단이 없는 브로커는 한 주기 대기 뒤 종전 정책(해제)으로 돌아간다."""
    now = datetime(2026, 9, 21, 10, 30)
    broker = LegacyBroker(cancelled=0, tracked=[_tracked_buy()])
    rm = _rm(monkeypatch, broker, now)
    _stale(rm, OrderSide.BUY, now, seconds=700, reserved=Decimal("101500"))
    rm._pending_fallback_count[SYM] = 1

    _drive(rm)

    assert SYM not in rm._pending_orders and SYM not in rm._reserved_by_order


def test_open_orders_error_keeps_pending(monkeypatch):
    now = datetime(2026, 9, 21, 10, 30)
    broker = Broker(cancelled=0, tracked=RuntimeError("조회 오류"))
    rm = _rm(monkeypatch, broker, now)
    _stale(rm, OrderSide.BUY, now, seconds=700, reserved=Decimal("101500"))

    _drive(rm)

    assert SYM in rm._pending_orders and SYM in rm._reserved_by_order


def test_release_by_other_task_during_query_is_not_resurrected(monkeypatch):
    """조회 await 사이에 다른 태스크(스케줄러 정리의 clear_pending)가 pending 을 지웠다면 되살리지 않는다.

    엔진 이벤트는 직렬이라 on_fill 은 끼어들지 못한다 — 끼어드는 것은 별도 태스크다.
    """
    now = datetime(2026, 9, 21, 10, 30)
    broker = Broker(cancelled=0, tracked=[_tracked_buy()])
    rm = _rm(monkeypatch, broker, now)
    _stale(rm, OrderSide.BUY, now, seconds=700, reserved=Decimal("101500"))
    broker.on_open_orders = lambda: rm.clear_pending(SYM)

    _drive(rm)

    assert SYM not in rm._pending_timestamps
    assert SYM not in rm._pending_fallback_count   # 남으면 이후 SELL 폴백 예산(2회)을 깎는다


def test_new_pending_starts_with_zero_fallback_count(monkeypatch):
    """한 장부를 SELL 폴백(상한 2)·BUY 유지(상한 15)가 같이 쓴다 — 잔존 값이 새 pending 에 새면
    그 SELL 은 첫 90초에 상한 분기로 빠져 시장가 폴백을 한 번도 못 쓴다."""
    now = datetime(2026, 9, 21, 10, 30)
    rm = _rm(monkeypatch, Broker(), now, positions=_held())
    rm.engine.is_trading_hours = lambda: True
    rm.engine._get_current_session = lambda: MarketSession.REGULAR
    rm._risk_validator = None
    rm._pending_fallback_count[SYM] = 2     # 어떤 경로로든 남은 값

    sig = Signal(symbol=SYM, side=OrderSide.SELL, strength=SignalStrength.STRONG,
                 strategy=StrategyType.SEPA_TREND, price=PRICE, reason="손절",
                 metadata={"quantity": 10})
    orders = asyncio.run(rm.on_signal(SignalEvent.from_signal(sig, source="test")))

    assert orders and orders[0].order.side == OrderSide.SELL
    assert SYM in rm._pending_orders
    assert SYM not in rm._pending_fallback_count


# ── 거래소 실 미체결 조회의 다중 페이지 ──────────────────────────────────────

from test_kis_tr_switch import _capture_get, broker as kis_broker  # noqa: E402,F401


@pytest.mark.parametrize("tr_cont, undecidable", [("F", True), ("M", True), ("D", False), ("", False)])
def test_open_order_query_is_undecidable_when_more_pages_remain(kis_broker, tr_cont, undecidable):
    """첫 페이지만 읽는 조회가 '미체결 없음'을 말하면 살아 있는 주문의 pending 이 풀린다 → 판단 불가(None)."""
    _capture_get(kis_broker, {"rt_cd": "0", "_tr_cont": tr_cont, "output": [
        {"pdno": OTHER, "sll_buy_dvsn_cd": "02", "rmn_qty": "3"}]})

    rows = asyncio.run(kis_broker.get_exchange_open_orders())

    assert (rows is None) if undecidable else (rows == [{"symbol": OTHER, "side": "buy", "qty": 3}])
