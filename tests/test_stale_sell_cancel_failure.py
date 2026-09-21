"""취소 실패(0건)에 섞인 '살아 있는 SELL' 위 재발행 = 분할 매도 이중 발행 (2026-09-21, PR #81 교차 리뷰 P1-5)

`KISBroker.cancel_all_for_symbol` 은 미연결·hashkey·rt_cd≠0·예외를 전부 삼키고 0 을 돌려준다. 그래서
0건에는 "추적 주문 없음"과 "취소 실패(방금 체결됐거나 거래소에 생존)"가 섞여 있다.

재현: 100주 보유, 1차 익절 10주 지정가 SELL 활성, 취소 API 실패(0 반환).
- 엔진 `RiskManager.on_signal` 90초 폴백: 반환값을 보지 않고 시장가 10주를 추가 제출 → 합계 20주는
  보유 수량 안이라 KIS 가 거절하지 않는다(전량 매도만 APBK0400 으로 보호된다).
- 스케줄러 `_cleanup_stale_pending`(3분): 0건이면 양쪽 pending 을 풀고 stage 를 롤백 → ExitManager 가
  같은 1차 익절을 재발행. 방금 체결된 경우엔 롤백이 on_fill 의 stage 승격까지 지운다.

실행: venv/bin/python -m pytest tests/test_stale_sell_cancel_failure.py -q -p no:cacheprovider
합성 브로커만 사용 — 네트워크·운영 캐시 무접촉. 시계는 engine / kr_scheduler 모듈의 datetime 을 동결한다.
"""
import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_engine_stale_pending_fixes import (  # noqa: E402 — PR #81 하네스 재사용
    OTHER, PRICE, SYM, Broker, LegacyBroker, _drive, _rm, _stale,
)

import src.schedulers.kr_scheduler as kr  # noqa: E402
from src.core.engine import RiskManager  # noqa: E402
from src.core.types import Order, OrderSide, OrderStatus, OrderType, Position  # noqa: E402
from src.schedulers.kr_scheduler import KRScheduler  # noqa: E402
from src.strategies.exit_manager import ExitManager, ExitStage  # noqa: E402

NOW = datetime(2026, 9, 21, 10, 30)
EXCHANGE = ("exchange", SYM)   # 하네스는 거래소 조회를 (이름, 물은 종목) 으로 기록한다
ALIVE = [{"symbol": SYM, "side": "sell", "qty": 10}]
ELSEWHERE = [{"symbol": OTHER, "side": "sell", "qty": 1}, {"symbol": SYM, "side": "buy", "qty": 1}]


def _tracked_sell(status=OrderStatus.SUBMITTED):
    o = Order(symbol=SYM, side=OrderSide.SELL, order_type=OrderType.LIMIT, quantity=10, price=PRICE)
    o.status = status
    return o


def _held_100():
    return {SYM: Position(symbol=SYM, quantity=100, avg_price=PRICE, current_price=PRICE,
                          strategy="sepa_trend")}


class SellBroker(Broker):
    """송신된 주문까지 기록한다 — 이중 매도는 '무엇이 몇 주 나갔는가'로 판정한다."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.orders = []

    async def submit_order(self, order):
        self.orders.append(order)
        return await super().submit_order(order)


class LegacySellBroker(LegacyBroker):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.orders = []

    async def submit_order(self, order):
        self.orders.append(order)
        return await super().submit_order(order)


# ── 엔진 경로: on_signal 90초 SELL 폴백 ──────────────────────────────────────

def _freeze_engine(monkeypatch, at):
    import src.core.engine as eng

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return at
    monkeypatch.setattr(eng, "datetime", _Frozen)


def _engine(monkeypatch, broker, *, keep=None, positions=None):
    rm = _rm(monkeypatch, broker, NOW, positions=_held_100() if positions is None else positions)
    _stale(rm, OrderSide.SELL, NOW, seconds=100)
    if keep is not None:
        rm._pending_cancel_keep[SYM] = (keep, NOW - timedelta(seconds=20))   # 재시도 간격은 이미 지났다
    return rm


@pytest.mark.parametrize("status", [OrderStatus.SUBMITTED, OrderStatus.PARTIAL])
def test_engine_failed_cancel_does_not_stack_a_market_sell(monkeypatch, status):
    """재현 시나리오: 취소 0건 + 브로커가 활성 SELL 추적 중 = 취소 실패. 시장가를 얹으면 10주가 20주가 된다."""
    broker = SellBroker(cancelled=0, tracked=[_tracked_sell(status)])
    rm = _engine(monkeypatch, broker)

    _drive(rm)

    assert broker.orders == []                      # 이중 매도 없음
    assert SYM in rm._pending_orders                # 살아 있을 수 있는 주문 — pending 유지
    assert rm._pending_quantities[SYM] == 10
    assert EXCHANGE not in broker.calls           # 첫 회는 조회 없이 대기(체결이면 FillEvent 가 지운다)
    assert rm._pending_cancel_keep[SYM] == (1, NOW)
    assert SYM not in rm._pending_fallback_count    # 시장가 폴백 예산(2회)은 깎지 않는다
    # 등록 시각은 되감지 않는다 — 헬스 모니터의 5분 교착 경보가 '손절이 막힌 SELL 유지'를 볼 수 있어야 한다
    assert (NOW - rm._pending_timestamps[SYM]).total_seconds() == 100


def test_engine_failed_cancel_retry_is_throttled_to_20_seconds(monkeypatch):
    """SIGNAL 마다 취소·조회 API 를 때리지 않되(체결 확인 5초 주기 몇 회분), 손절 지연은 짧게."""
    broker = SellBroker(cancelled=0, tracked=[_tracked_sell()], exchange_rows=ALIVE)
    rm = _engine(monkeypatch, broker)

    _drive(rm)
    _freeze_engine(monkeypatch, NOW + timedelta(seconds=19))
    _drive(rm)
    assert broker.calls == ["cancel"]

    _freeze_engine(monkeypatch, NOW + timedelta(seconds=20))
    _drive(rm)
    assert broker.calls == ["cancel", "cancel", EXCHANGE] and broker.orders == []


def test_engine_zero_cancel_with_untracked_order_waits_one_cycle(monkeypatch):
    """브로커 장부는 완전 체결·취소 성공에서만 빠진다 — pending 이 남았는데 미추적이면 FillEvent 가 오는 중이다."""
    broker = SellBroker(cancelled=0, tracked=[])
    rm = _engine(monkeypatch, broker)

    _drive(rm)

    assert broker.orders == [] and SYM in rm._pending_orders
    assert rm._pending_cancel_keep[SYM] == (1, NOW)


@pytest.mark.parametrize("rows, count_after", [(ALIVE, 1), (None, 6)],
                         ids=["거래소 생존 → 연속 판단불가 횟수 초기화", "조회 실패(판단 불가) → 횟수 +1"])
def test_engine_retry_keeps_while_alive_or_undecidable(monkeypatch, rows, count_after):
    broker = SellBroker(cancelled=0, tracked=[_tracked_sell()], exchange_rows=rows)
    rm = _engine(monkeypatch, broker, keep=5)

    _drive(rm)

    assert broker.calls == ["cancel", EXCHANGE]     # 취소는 매 주기 다시 시도한다(성공하면 그때 시장가 전환)
    assert broker.orders == [] and SYM in rm._pending_orders
    assert rm._pending_cancel_keep[SYM] == (count_after, NOW)


def test_engine_one_undecidable_query_after_long_alive_does_not_release(monkeypatch):
    """상한은 '연속 판단 불가'에만 적용한다 — 오래 살아 있던 주문이 조회 실패 한 번으로 풀리면 그 위에 재발행된다."""
    broker = SellBroker(cancelled=0, tracked=[_tracked_sell()], exchange_rows=ALIVE)
    rm = _engine(monkeypatch, broker, keep=9)

    _drive(rm)                                      # 생존 확인 → 횟수 초기화
    broker._exchange_rows = None
    _freeze_engine(monkeypatch, NOW + timedelta(seconds=20))
    _drive(rm)                                      # 조회 실패 1회

    assert broker.orders == [] and SYM in rm._pending_orders
    assert rm._pending_cancel_keep[SYM][0] == 2


def test_engine_retry_resubmits_original_quantity_once_gone_from_exchange(monkeypatch):
    """한 주기 뒤에도 pending 인데(체결 아님) 거래소에 SELL 미체결이 없다 = 소멸(수동 취소 등) → 종전 시장가 폴백."""
    broker = SellBroker(cancelled=0, tracked=[_tracked_sell()], exchange_rows=ELSEWHERE)
    rm = _engine(monkeypatch, broker, keep=1)

    _drive(rm)

    assert [(o.side, o.order_type, o.quantity) for o in broker.orders] == [
        (OrderSide.SELL, OrderType.MARKET, 10)]     # 원 주문 수량 — 보유 100주 전량이 아니다
    assert rm._pending_fallback_count[SYM] == 1
    assert SYM not in rm._pending_cancel_keep       # 새 주문은 0회에서 다시 센다


def test_engine_retry_resubmits_when_broker_no_longer_tracks(monkeypatch):
    """한 주기를 기다려도 체결이 오지 않은 미추적 pending 은 종전대로 시장가로 전환한다(청산 지연 상한)."""
    broker = SellBroker(cancelled=0, tracked=[])
    rm = _engine(monkeypatch, broker, keep=1)

    _drive(rm)

    assert [o.quantity for o in broker.orders] == [10]
    assert EXCHANGE not in broker.calls


def test_engine_cancel_ack_still_converts_to_market_for_original_quantity(monkeypatch):
    """회귀 방지: 취소 ACK 면 대기 없이 곧바로 시장가 전환(손절 지연 0)."""
    broker = SellBroker(cancelled=1, tracked=[])
    rm = _engine(monkeypatch, broker)

    _drive(rm)

    assert broker.calls == ["cancel", "submit"]
    assert [o.quantity for o in broker.orders] == [10]
    assert SYM not in rm._pending_cancel_keep


def test_engine_confirmed_alive_sell_is_never_released_or_restacked(monkeypatch):
    """거래소 생존이 확인된 SELL 은 상한과 무관하게 유지한다 — 풀면 같은 분할 익절이 그 위에 재발행된다."""
    broker = SellBroker(cancelled=0, tracked=[_tracked_sell()], exchange_rows=ALIVE)
    rm = _engine(monkeypatch, broker, keep=40)

    _drive(rm)

    assert broker.orders == [] and SYM in rm._pending_orders


@pytest.mark.parametrize("keep, released", [(8, False), (9, True)])
def test_engine_undecidable_keep_is_bounded_and_releases_without_resubmit(monkeypatch, keep, released):
    """조회 실패(None)가 이어지면 pending 이 그 종목의 손절 신호까지 막는다 → 종전 최장 보유(270초)에서 해제.

    살아 있을 수 있는 주문 위라 시장가는 얹지 않는다 — 재판단은 발생원(ExitManager 검증자)이 한다.
    """
    broker = SellBroker(cancelled=0, tracked=[_tracked_sell()], exchange_rows=None)
    rm = _engine(monkeypatch, broker, keep=keep)

    _drive(rm)

    assert EXCHANGE in broker.calls               # 상한에서도 먼저 한 번 더 확인한다
    assert broker.orders == []
    assert (SYM not in rm._pending_orders) is released
    assert (SYM not in rm._pending_cancel_keep) is released


def test_engine_liveness_query_error_keeps_pending(monkeypatch):
    broker = SellBroker(cancelled=0, tracked=RuntimeError("조회 오류"))
    rm = _engine(monkeypatch, broker, keep=1)

    _drive(rm)

    assert broker.orders == [] and SYM in rm._pending_orders


def test_engine_vanished_position_still_releases_without_waiting(monkeypatch):
    """회귀 방지: 포지션이 없으면 재주문이 없으므로 이중 매도 위험도 없다 — 종전대로 즉시 해제."""
    broker = SellBroker(cancelled=0, tracked=[_tracked_sell()])
    rm = _engine(monkeypatch, broker, positions={})

    _drive(rm)

    assert broker.orders == [] and SYM not in rm._pending_orders


def test_engine_legacy_broker_falls_back_after_one_cycle(monkeypatch):
    """거래소 조회 수단이 없는 브로커는 한 주기 대기 뒤 종전 정책(시장가 전환)으로 돌아간다."""
    broker = LegacySellBroker(cancelled=0, tracked=[_tracked_sell()])
    rm = _engine(monkeypatch, broker, keep=1)

    _drive(rm)

    assert [o.quantity for o in broker.orders] == [10]


def test_engine_release_by_other_task_during_query_is_not_resurrected(monkeypatch):
    """조회 await 사이에 스케줄러 정리(clear_pending)가 끼어들면 pending·유지 장부를 되살리지 않는다."""
    broker = SellBroker(cancelled=0, tracked=[_tracked_sell()], exchange_rows=ALIVE)
    rm = _engine(monkeypatch, broker, keep=1)
    broker.on_open_orders = lambda: rm.clear_pending(SYM)

    _drive(rm)

    assert SYM not in rm._pending_timestamps and SYM not in rm._pending_cancel_keep
    assert broker.orders == []


def test_engine_full_fill_clears_the_keep_ledger(monkeypatch):
    """유지 중 체결이 도착하면 on_fill 이 유지 장부까지 지운다 — 다음 SELL 의 첫 회 대기가 건너뛰어지지 않게."""
    from src.core.event import FillEvent
    from src.core.types import Fill

    rm = _engine(monkeypatch, SellBroker(), keep=3)
    rm._pending_exit_reasons = {}
    rm.config = SimpleNamespace(daily_max_loss_pct=5.0)
    rm.engine.update_position = lambda fill: None
    rm.engine.portfolio.total_equity = Decimal("10000000")
    rm.engine.portfolio.effective_daily_pnl = Decimal("0")

    fill = Fill(order_id="ORD-1", symbol=SYM, side=OrderSide.SELL, quantity=10, price=PRICE)
    asyncio.run(rm.on_fill(FillEvent.from_fill(fill, source="test")))

    assert SYM not in rm._pending_orders and SYM not in rm._pending_cancel_keep


def test_engine_new_pending_starts_with_zero_keep_count(monkeypatch):
    """어떤 경로로든 남은 유지 횟수가 새 SELL 에 새면 첫 회 대기를 건너뛰고 곧바로 거래소 조회로 간다."""
    from src.core.event import SignalEvent
    from src.core.types import MarketSession, Signal, SignalStrength, StrategyType

    rm = _rm(monkeypatch, SellBroker(), NOW, positions=_held_100())
    rm.engine.is_trading_hours = lambda: True
    rm.engine._get_current_session = lambda: MarketSession.REGULAR
    rm._risk_validator = None
    rm._pending_cancel_keep[SYM] = (5, NOW)

    sig = Signal(symbol=SYM, side=OrderSide.SELL, strength=SignalStrength.STRONG,
                 strategy=StrategyType.SEPA_TREND, price=PRICE, reason="1차 익절",
                 metadata={"quantity": 10})
    orders = asyncio.run(rm.on_signal(SignalEvent.from_signal(sig, source="test")))

    assert orders and SYM in rm._pending_orders
    assert SYM not in rm._pending_cancel_keep


# ── 스케줄러 경로: _cleanup_stale_pending (별도 장부, 3분) ────────────────────

@pytest.fixture
def home(tmp_path, monkeypatch):
    """ExitManager 가 ~/.cache/ai_trader 대신 tmp_path 를 쓰도록 Path.home() 패치 (운영 캐시 무접촉)."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


def _scheduler(monkeypatch, broker, *, age_min=4, engine_pending=True):
    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW
    monkeypatch.setattr(kr, "datetime", _Frozen)

    exit_manager = ExitManager()
    exit_manager.register_position(Position(
        symbol=SYM, quantity=100, avg_price=PRICE, current_price=PRICE,
        entry_time=NOW - timedelta(days=3), strategy="sepa_trend"))
    state = exit_manager.get_state(SYM)
    state.pending_stage, state.pending_since = ExitStage.FIRST, NOW - timedelta(minutes=age_min)
    state.pending_target_qty = 10

    cleared = []

    async def clear_pending(symbol):
        cleared.append(symbol)
        risk_manager._pending_orders.discard(symbol)

    # 생존 판정은 엔진의 실제 함수를 쓴다(스케줄러·엔진이 같은 기준으로 가린다)
    risk_manager = object.__new__(RiskManager)
    risk_manager.engine = SimpleNamespace(broker=broker)
    risk_manager._pending_orders = {SYM} if engine_pending else set()
    risk_manager.clear_pending = clear_pending
    bot = SimpleNamespace(
        broker=broker, exit_manager=exit_manager,
        engine=SimpleNamespace(risk_manager=risk_manager),
        _exit_pending_symbols={SYM},
        _exit_pending_timestamps={SYM: NOW - timedelta(minutes=age_min)},
    )
    scheduler = object.__new__(KRScheduler)
    scheduler.bot = bot
    scheduler.alerts = []

    async def send_error_alert(error_type, message, details="", critical=False):
        scheduler.alerts.append((error_type, critical))   # 텔레그램 무접촉

    scheduler._send_error_alert = send_error_alert
    return scheduler, bot, state, cleared


def _sweep(scheduler):
    asyncio.run(scheduler._cleanup_stale_pending())


@pytest.mark.parametrize("engine_pending", [True, False], ids=["stale 루프", "고아 루프"])
def test_scheduler_failed_cancel_does_not_roll_back_the_stage(home, monkeypatch, engine_pending):
    """재현 시나리오: 취소 0건 + 활성 SELL 추적 중. stage 를 롤백하면 ExitManager 가 같은 10주를 재발행한다."""
    broker = SellBroker(cancelled=0, tracked=[_tracked_sell()])
    scheduler, bot, state, cleared = _scheduler(monkeypatch, broker, engine_pending=engine_pending)

    _sweep(scheduler)

    assert state.pending_stage == ExitStage.FIRST   # 재발행 차단 유지
    assert SYM in bot._exit_pending_symbols and cleared == []
    assert EXCHANGE not in broker.calls           # 첫 회는 조회 없이 한 주기 대기


def test_scheduler_failed_cancel_retry_is_throttled_to_60_seconds(home, monkeypatch):
    """WS 틱마다 호출된다 — 유지 상태에서 매 틱 취소 API 를 때리면 KIS 한도를 스스로 소진한다."""
    broker = SellBroker(cancelled=0, tracked=[_tracked_sell()])
    scheduler, *_ = _scheduler(monkeypatch, broker)

    _sweep(scheduler)
    _sweep(scheduler)

    assert broker.calls == ["cancel"]
    assert SYM in scheduler.bot._exit_pending_symbols   # 풀려서 조용한 것이 아니라 유지한 채 스로틀된 것


@pytest.mark.parametrize("rows", [ALIVE, None], ids=["거래소 생존", "조회 실패(판단 불가)"])
def test_scheduler_retry_keeps_while_alive_or_undecidable(home, monkeypatch, rows):
    broker = SellBroker(cancelled=0, tracked=[_tracked_sell()], exchange_rows=rows)
    scheduler, bot, state, cleared = _scheduler(monkeypatch, broker)
    scheduler._stale_cancel_miss = {SYM: (bot._exit_pending_timestamps[SYM], 0)}   # 이미 한 주기 기다렸다

    _sweep(scheduler)

    assert EXCHANGE in broker.calls
    assert state.pending_stage == ExitStage.FIRST and SYM in bot._exit_pending_symbols and cleared == []


def test_scheduler_retry_releases_once_gone_from_exchange(home, monkeypatch):
    """거래소에 SELL 미체결이 없으면 소멸 — 종전대로 양쪽 pending 해제 + stage 롤백(재판단 허용)."""
    broker = SellBroker(cancelled=0, tracked=[_tracked_sell()], exchange_rows=ELSEWHERE)
    scheduler, bot, state, cleared = _scheduler(monkeypatch, broker)
    scheduler._stale_cancel_miss = {SYM: (bot._exit_pending_timestamps[SYM], 0)}

    _sweep(scheduler)

    assert state.pending_stage is None and SYM not in bot._exit_pending_symbols and cleared == [SYM]
    assert SYM not in scheduler._stale_cancel_miss


def test_scheduler_miss_of_a_previous_pending_does_not_skip_the_first_wait(home, monkeypatch):
    """유지 표식은 pending 등록 시각에 묶인다 — 이전 pending 의 표식이 새 pending 의 첫 회 대기를 건너뛰게 하지 않는다."""
    broker = SellBroker(cancelled=0, tracked=[_tracked_sell()], exchange_rows=ELSEWHERE)
    scheduler, bot, state, cleared = _scheduler(monkeypatch, broker)
    scheduler._stale_cancel_miss = {SYM: (NOW - timedelta(hours=1), 3)}

    _sweep(scheduler)

    assert EXCHANGE not in broker.calls
    assert state.pending_stage == ExitStage.FIRST and cleared == []


def test_scheduler_untracked_zero_cancel_still_releases(home, monkeypatch):
    """회귀 방지: 추적 주문이 없으면(엔진이 SELL 신호를 거부해 주문 자체가 없던 고아 등) 종전대로 즉시 해제.

    여기서 기다리면 그 종목의 청산 판정(손절 포함)이 60초 더 막힌다.
    """
    broker = SellBroker(cancelled=0, tracked=[])
    scheduler, bot, state, cleared = _scheduler(monkeypatch, broker)

    _sweep(scheduler)

    assert state.pending_stage is None and SYM not in bot._exit_pending_symbols and cleared == [SYM]


def test_scheduler_cancel_ack_still_releases(home, monkeypatch):
    broker = SellBroker(cancelled=1, tracked=[])
    scheduler, bot, state, cleared = _scheduler(monkeypatch, broker)

    _sweep(scheduler)

    assert state.pending_stage is None and SYM not in bot._exit_pending_symbols and cleared == [SYM]


@pytest.mark.parametrize("streak, released", [(13, False), (14, True)])
def test_scheduler_undecidable_keep_is_bounded_at_15_consecutive_failures(home, monkeypatch, streak, released):
    """조회 실패(None)가 연속 15회(60초 스로틀 → 15분) 이어지면 강제 해제 — 별도 장부가 청산 판정을 종일 막지 않게."""
    broker = SellBroker(cancelled=0, tracked=[_tracked_sell()], exchange_rows=None)
    scheduler, bot, state, cleared = _scheduler(monkeypatch, broker)
    scheduler._stale_cancel_miss = {SYM: (bot._exit_pending_timestamps[SYM], streak)}

    _sweep(scheduler)

    assert (SYM not in bot._exit_pending_symbols) is released and cleared == ([SYM] if released else [])


def test_scheduler_confirmed_alive_sell_is_kept_beyond_15_minutes(home, monkeypatch):
    """거래소 생존이 확인된 SELL 은 15분이 지나도 풀지 않는다 — 풀면 그 위에 같은 분할 익절이 재발행된다."""
    broker = SellBroker(cancelled=0, tracked=[_tracked_sell()], exchange_rows=ALIVE)
    scheduler, bot, state, cleared = _scheduler(monkeypatch, broker, age_min=40)
    scheduler._stale_cancel_miss = {SYM: (bot._exit_pending_timestamps[SYM], 14)}

    _sweep(scheduler)
    scheduler._stale_cancel_last_try.clear()    # 60초 경과를 대신한다
    _sweep(scheduler)

    assert state.pending_stage == ExitStage.FIRST and SYM in bot._exit_pending_symbols and cleared == []
    # 청산 판정이 보류되는 상태라 사람이 알아야 한다 — 텔레그램 경보는 pending 당 1회
    assert scheduler.alerts == [("청산 주문 취소 불가", True)]
    # 생존 확인은 연속 판단불가 횟수를 되돌린다 — 이후 조회 실패 한 번으로 풀리지 않는다
    broker._exchange_rows = None
    scheduler._stale_cancel_last_try.clear()
    _sweep(scheduler)
    assert SYM in bot._exit_pending_symbols and cleared == []


def test_scheduler_liveness_query_error_keeps_pending(home, monkeypatch):
    broker = SellBroker(cancelled=0, tracked=RuntimeError("조회 오류"))
    scheduler, bot, state, cleared = _scheduler(monkeypatch, broker)

    _sweep(scheduler)

    assert state.pending_stage == ExitStage.FIRST and SYM in bot._exit_pending_symbols and cleared == []
