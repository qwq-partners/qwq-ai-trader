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
from src.core.event import FillEvent, SignalEvent  # noqa: E402
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

    async def get_exchange_open_orders(self, symbol=None):
        self.calls.append(("exchange", symbol))
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
    assert ("exchange", SYM) not in broker.calls   # 첫 실패는 조회 없이 한 주기 대기(체결이면 FillEvent 가 지운다)
    assert rm._pending_fallback_count[SYM] == 1
    # 60초 뒤 재시도 — SIGNAL 마다 취소 API 를 때리지 않는다
    assert (now - rm._pending_timestamps[SYM]).total_seconds() == 600 - 60


@pytest.mark.parametrize("rows, count_after", [([{"symbol": SYM, "side": "buy", "qty": 10}], 1), (None, 6)],
                         ids=["거래소 생존 → 연속 판단불가 횟수 초기화", "조회 실패(판단 불가) → 횟수 +1"])
def test_zero_cancel_retry_keeps_while_alive_on_exchange(monkeypatch, rows, count_after):
    now = datetime(2026, 9, 21, 10, 30)
    broker = Broker(cancelled=0, tracked=[_tracked_buy()], exchange_rows=rows)
    rm = _rm(monkeypatch, broker, now)
    _stale(rm, OrderSide.BUY, now, seconds=700, reserved=Decimal("101500"))
    rm._pending_fallback_count[SYM] = 5    # 이미 여러 주기 기다렸다

    _drive(rm)

    assert SYM in rm._pending_orders and SYM in rm._reserved_by_order
    assert ("exchange", SYM) in broker.calls
    assert rm._pending_fallback_count[SYM] == count_after
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
    assert ("exchange", SYM) not in broker.calls
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
    # 생존 확인이 40회 이어진 뒤 조회가 한 번 실패해도 풀지 않는다 — 상한은 '연속' 판단 불가에만
    assert rm._pending_fallback_count[SYM] == 1
    broker._exchange_rows = None
    rm._pending_timestamps[SYM] = now - timedelta(seconds=700)
    _drive(rm)
    assert SYM in rm._pending_orders and rm._pending_fallback_count[SYM] == 2


def test_partial_fill_position_releases_so_exits_are_not_blocked(monkeypatch):
    """부분체결로 포지션이 있으면 종전대로 해제한다 — pending 은 그 종목의 손절 신호까지 막기 때문."""
    now = datetime(2026, 9, 21, 10, 30)
    broker = Broker(cancelled=0, tracked=[_tracked_buy(OrderStatus.PARTIAL)])
    rm = _rm(monkeypatch, broker, now, positions=_held())
    _stale(rm, OrderSide.BUY, now, seconds=700, reserved=Decimal("60900"))

    _drive(rm)

    assert SYM not in rm._pending_orders and SYM not in rm._reserved_by_order
    assert ("exchange", SYM) not in broker.calls


def test_keep_is_bounded_even_if_exchange_query_keeps_failing(monkeypatch):
    """조회 실패(None)가 이어져도 예약현금이 자정까지 잠기지 않는다 — 상한에서 강제 해제."""
    now = datetime(2026, 9, 21, 10, 30)
    broker = Broker(cancelled=0, tracked=[_tracked_buy()], exchange_rows=None)
    rm = _rm(monkeypatch, broker, now)
    _stale(rm, OrderSide.BUY, now, seconds=700, reserved=Decimal("101500"))
    rm._pending_fallback_count[SYM] = 15

    _drive(rm)

    assert ("exchange", SYM) in broker.calls   # 상한에서도 먼저 한 번 더 확인한다(종목 지정 조회)
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


_FIRST_PAGE = [{"pdno": OTHER, "sll_buy_dvsn_cd": "02", "rmn_qty": "3"}]
_ROWS = [{"symbol": OTHER, "side": "buy", "qty": 3}]


@pytest.mark.parametrize("symbol, tr_cont, expected", [
    (SYM, "F", None), (SYM, "M", None),      # 첫 페이지에 없고 다음 페이지가 남음 → 판단 불가
    (SYM, "D", _ROWS), (SYM, "", _ROWS),     # 마지막 페이지 → 정말 없음(행 그대로)
    (OTHER, "F", _ROWS),                     # 찾았으면 생존 증거 — 페이지가 남아도 버리지 않는다
    (None, "F", _ROWS),                      # 종목 미지정(ExitManager 검증자)은 종전 동작 그대로
])
def test_open_order_query_three_states_only_for_the_asked_symbol(kis_broker, symbol, tr_cont, expected):
    _capture_get(kis_broker, {"rt_cd": "0", "_tr_cont": tr_cont, "output": _FIRST_PAGE})

    rows = asyncio.run(kis_broker.get_exchange_open_orders(symbol=symbol) if symbol is not None
                       else kis_broker.get_exchange_open_orders())

    assert rows == expected


# ── 유지 중이던 BUY 의 부분체결 (on_fill) ────────────────────────────────────

def _fill_ready(rm):
    rm._pending_exit_reasons = {}
    rm.config = SimpleNamespace(daily_max_loss_pct=5.0)
    rm.engine.update_position = lambda fill: None
    rm.engine.portfolio.total_equity = Decimal("1000000")
    rm.engine.portfolio.effective_daily_pnl = Decimal("0")


@pytest.mark.parametrize("keep_cnt, released", [(1, True), (0, False)],
                         ids=["취소 실패로 유지 중 → 즉시 해제", "일반 부분체결 → 잔량 추적 유지(종전)"])
def test_partial_fill_of_a_kept_stale_buy_unblocks_exits(monkeypatch, keep_cnt, released):
    """pending 은 방향 구분 없이 그 종목의 청산 신호를 막는다 — 유지 중이던 BUY 가 부분체결되면
    다음 SIGNAL(stale 루프)을 기다리지 않고 풀어야 보유분의 손절이 막히지 않는다."""
    now = datetime(2026, 9, 21, 10, 30)
    rm = _rm(monkeypatch, Broker(), now)
    _fill_ready(rm)
    _stale(rm, OrderSide.BUY, now, seconds=650, reserved=Decimal("101500"))
    if keep_cnt:
        rm._pending_fallback_count[SYM] = keep_cnt

    asyncio.run(rm.on_fill(FillEvent(symbol=SYM, side=OrderSide.BUY, quantity=4, price=PRICE)))

    assert (SYM not in rm._pending_orders) is released
    assert (SYM not in rm._reserved_by_order) is released
    if not released:
        assert rm._pending_quantities[SYM] == 6


# ── 0건 해제 직후 같은 호출의 같은 종목 BUY ─────────────────────────────────

def test_same_symbol_buy_in_the_same_call_is_blocked_after_zero_cancel_release(monkeypatch):
    """'미추적'은 방금 완전체결(FillEvent 가 큐에서 대기)일 수 있다 — 해제를 일으킨 바로 그 BUY 신호가
    포지션 반영 전에 새 주문으로 이어지면 이중 매수다."""
    now = datetime(2026, 9, 21, 10, 30)
    rm = _rm(monkeypatch, Broker(cancelled=0, tracked=[]), now)
    rm.engine.is_trading_hours = lambda: True
    rm.engine._get_current_session = lambda: MarketSession.REGULAR
    rm.engine._market_regime = "neutral"
    rm._cross_validator = SimpleNamespace(validate=lambda **kw: (True, kw["score"], ""),
                                          last_memory_adj=0, last_llm_context={})
    rm._LLM_CHECK_MIN, rm._LLM_BYPASS_AT, rm._LLM_REJECT_SIZE_MULT = 85, 95, 0.5
    rm._check_factor_budget = lambda _s: None
    rm._get_core_reserve = lambda: Decimal("0")
    rm._last_cash_warn_time = None
    rm.config = SimpleNamespace(strategy_allocation={}, factor_budgets={})
    rm.engine.get_available_cash = lambda: Decimal("5000000")   # 현금·예산 게이트는 통과 — 막는 것은 쿨다운뿐
    _stale(rm, OrderSide.BUY, now, seconds=700, reserved=Decimal("101500"))

    sig = Signal(symbol=SYM, side=OrderSide.BUY, strength=SignalStrength.NORMAL,
                 strategy=StrategyType.SEPA_TREND, price=PRICE, score=70.0, reason="20일 고가 돌파")
    orders = asyncio.run(rm.on_signal(SignalEvent.from_signal(sig, source="test")))

    assert orders is None
    assert SYM not in rm._pending_orders    # 새 pending 이 등록되지 않았다


# ── FillEvent 없이 잔고 동기화로 포지션이 먼저 반영되는 경로 ────────────────

def _scheduler_with(rm, emitted):
    """_check_exit_signal 이 만지는 속성만 가진 최소 bot + KRScheduler(초기화 생략)."""
    from src.schedulers.kr_scheduler import KRScheduler

    async def _no_expire(symbol):
        return None

    async def _emit(event):
        emitted.append(event)

    rm.engine.emit = _emit
    rm.engine.risk_manager = rm
    bot = SimpleNamespace(
        engine=rm.engine, broker=rm.engine.broker,
        exit_manager=SimpleNamespace(
            is_exit_exempt=lambda symbol: False, maybe_expire_pending=_no_expire,
            update_price=lambda symbol, price, market_data=None: ("stop_loss", 4, "손절 -5.2%")),
        _pause_resume_at=None, _exit_pending_symbols=set(), _exit_pending_timestamps={},
        _sell_blocked_symbols={}, _exit_reasons={}, _strategy_exit_params={},
    )
    sched = object.__new__(KRScheduler)
    sched.bot = bot
    return sched


@pytest.mark.parametrize("keep_cnt, exit_emitted", [(1, True), (0, False)],
                         ids=["유지 중이던 BUY → 해제 후 손절 발행", "일반 BUY pending → 종전대로 청산 검사 보류"])
def test_synced_position_of_a_kept_stale_buy_can_exit_without_any_signal(monkeypatch, keep_cnt, exit_emitted):
    """체결 조회가 비어 FillEvent 가 없고 잔고 동기화로만 4주가 반영됐다. 다른 SIGNAL 이 없어도
    (stale 루프가 돌지 않아도) 가격 틱의 청산 검사가 손절을 낼 수 있어야 한다."""
    now = datetime(2026, 9, 21, 10, 40)
    held = {SYM: Position(symbol=SYM, quantity=4, avg_price=PRICE, current_price=PRICE, strategy="sepa_trend")}
    rm = _rm(monkeypatch, Broker(), now, positions=held)
    _stale(rm, OrderSide.BUY, now, seconds=650, reserved=Decimal("101500"))
    if keep_cnt:
        rm._pending_fallback_count[SYM] = keep_cnt
    emitted = []

    asyncio.run(_scheduler_with(rm, emitted)._check_exit_signal(SYM, Decimal("9480")))

    assert bool(emitted) is exit_emitted
    assert (SYM not in rm._pending_orders) is exit_emitted
    if exit_emitted:
        assert emitted[0].side == OrderSide.SELL and emitted[0].symbol == SYM


def test_release_kept_stale_buy_leaves_other_pendings_alone(monkeypatch):
    """SELL pending(횟수 장부를 같이 쓴다)·포지션 없는 유지 BUY 는 건드리지 않는다."""
    now = datetime(2026, 9, 21, 10, 40)
    rm = _rm(monkeypatch, Broker(), now, positions=_held())
    _stale(rm, OrderSide.SELL, now, seconds=30)
    rm._pending_fallback_count[SYM] = 1
    assert asyncio.run(rm.release_kept_stale_buy(SYM)) is False and SYM in rm._pending_orders

    rm2 = _rm(monkeypatch, Broker(), now, positions={})
    _stale(rm2, OrderSide.BUY, now, seconds=650, reserved=Decimal("101500"))
    rm2._pending_fallback_count[SYM] = 3
    assert asyncio.run(rm2.release_kept_stale_buy(SYM)) is False and SYM in rm2._reserved_by_order
