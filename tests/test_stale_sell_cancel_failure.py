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
from src.core.engine import RiskManager, _SellKeep  # noqa: E402
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
        self.on_cancel = None

    async def cancel_all_for_symbol(self, symbol):
        if self.on_cancel is not None:
            await self.on_cancel()
        return await super().cancel_all_for_symbol(symbol)

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


def _engine(monkeypatch, broker, *, keep=None, positions=None, partial=True, undecided_for=20):
    rm = _rm(monkeypatch, broker, NOW, positions=_held_100() if positions is None else positions)
    _stale(rm, OrderSide.SELL, NOW, seconds=100)
    if partial:
        rm._pending_signal_cache[SYM] = {"sell_partial_intent": True}   # on_signal 이 수량을 정하며 남기는 의도
    if keep is not None:   # 이미 기다렸고 재시도 간격(20초)도 지났다. undecided_for = 마지막 비-판단불가 판정 뒤 경과(초)
        rm._pending_cancel_keep[SYM] = _SellKeep(keep, NOW - timedelta(seconds=20), False, False,
                                                 NOW - timedelta(seconds=undecided_for))
    return rm


def _add_kept_partial_sell(rm, symbol, *, waited, alive=False):
    rm._pending_orders.add(symbol)
    rm._pending_sides[symbol], rm._pending_quantities[symbol] = OrderSide.SELL, 10
    rm._pending_timestamps[symbol] = NOW - timedelta(seconds=300)
    rm._pending_signal_cache[symbol] = {"sell_partial_intent": True}
    at = NOW - timedelta(seconds=waited)
    rm._pending_cancel_keep[symbol] = _SellKeep(1, at, alive, alive, at)
    rm.engine.portfolio.positions[symbol] = Position(
        symbol=symbol, quantity=100, avg_price=PRICE, current_price=PRICE, strategy="sepa_trend")


def _beat(rm):
    from src.core.event import HeartbeatEvent
    return asyncio.run(rm.on_heartbeat(HeartbeatEvent(source="test")))


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
    assert rm._pending_cancel_keep[SYM] == _SellKeep(1, NOW, False, False, NOW)
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
    assert rm._pending_cancel_keep[SYM] == _SellKeep(1, NOW, False, False, NOW)


@pytest.mark.parametrize("rows, count_after", [(ALIVE, _SellKeep(1, NOW, True, True, NOW)),
                          (None, _SellKeep(6, NOW, False, False, NOW - timedelta(seconds=20)))],
                         ids=["거래소 생존 → 연속 판단불가 횟수 초기화", "조회 실패(판단 불가) → 횟수 +1"])
def test_engine_retry_keeps_while_alive_or_undecidable(monkeypatch, rows, count_after):
    broker = SellBroker(cancelled=0, tracked=[_tracked_sell()], exchange_rows=rows)
    rm = _engine(monkeypatch, broker, keep=5)

    _drive(rm)

    assert broker.calls == ["cancel", EXCHANGE]     # 취소는 매 주기 다시 시도한다(성공하면 그때 시장가 전환)
    assert broker.orders == [] and SYM in rm._pending_orders
    assert rm._pending_cancel_keep[SYM] == count_after   # (횟수, 시도 시각, 생존 로그 1회, 직전 판정이 생존, 마지막 비-판단불가 시각)


def test_engine_one_undecidable_query_after_long_alive_does_not_release(monkeypatch):
    """상한은 '연속 판단 불가'에만 적용한다 — 오래 살아 있던 주문이 조회 실패 한 번으로 풀리면 그 위에 재발행된다."""
    broker = SellBroker(cancelled=0, tracked=[_tracked_sell()], exchange_rows=ALIVE)
    rm = _engine(monkeypatch, broker, keep=9, undecided_for=36000)

    _drive(rm)                                      # 생존 확인 → 횟수·시간 예산 초기화
    broker._exchange_rows = None
    _freeze_engine(monkeypatch, NOW + timedelta(seconds=60))   # 생존 확정 뒤의 간격
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
    rm = _engine(monkeypatch, broker, keep=40, undecided_for=36000)

    _drive(rm)

    assert broker.orders == [] and SYM in rm._pending_orders


@pytest.mark.parametrize("undecided_for, released", [(179, False), (180, True)])
def test_engine_undecidable_keep_is_bounded_and_releases_without_resubmit(monkeypatch, undecided_for, released):
    """조회 실패(None)가 이어지면 pending 이 그 종목의 손절 신호까지 막는다 → 종전 최장 보유(270초)에서 해제.

    살아 있을 수 있는 주문 위라 시장가는 얹지 않는다 — 재판단은 발생원(ExitManager 검증자)이 한다.
    """
    broker = SellBroker(cancelled=0, tracked=[_tracked_sell()], exchange_rows=None)
    rm = _engine(monkeypatch, broker, keep=3, undecided_for=undecided_for)   # 횟수가 아니라 시간으로 센다

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


def test_engine_release_during_query_never_widens_to_the_whole_position(monkeypatch):
    """교차 리뷰 P1: 조회 await 중 스케줄러가 취소에 성공해 pending 을 풀면 거래소엔 없고 수량 장부도 비어 있다.

    재검증 없이 '소멸 → 폴백'으로 가면 수량 결측이 보유 전량으로 대체돼 10주 익절이 100주 시장가가 된다.
    """
    broker = SellBroker(cancelled=0, tracked=[_tracked_sell()], exchange_rows=ELSEWHERE)
    rm = _engine(monkeypatch, broker, keep=1)
    broker.on_open_orders = lambda: rm.clear_pending(SYM)

    _drive(rm)

    assert broker.orders == [] and SYM not in rm._pending_timestamps


def test_engine_release_during_cancel_never_widens_to_the_whole_position(monkeypatch):
    """같은 가드가 기존 취소 await 의 경쟁도 닫는다 — 엔진 2회차 폴백과 스케줄러 3분 정리는 둘 다 ~180초에 닿는다."""
    broker = SellBroker(cancelled=1, tracked=[])
    rm = _engine(monkeypatch, broker)
    broker.on_cancel = lambda: rm.clear_pending(SYM)

    _drive(rm)

    assert broker.orders == []


def test_engine_full_quantity_sell_keeps_the_old_path_so_stops_are_not_delayed(monkeypatch):
    """전량 매도(손절·트레일링)는 원 주문이 살아 있으면 KIS 가 주문가능수량 0 으로 거절한다 — 과매도가 불가능하므로
    관문을 적용하지 않는다. 대기시키면 손절이 늦어지기만 한다."""
    broker = SellBroker(cancelled=0, tracked=[_tracked_sell()])
    rm = _engine(monkeypatch, broker, partial=False)
    rm._pending_quantities[SYM] = 100

    _drive(rm)

    assert [(o.order_type, o.quantity) for o in broker.orders] == [(OrderType.MARKET, 100)]
    assert SYM not in rm._pending_cancel_keep


def test_engine_partially_filled_full_stop_is_still_a_full_exit(monkeypatch):
    """교차 리뷰 반례: 100주 손절이 10주 체결된 뒤 잔량 90주 — 보유도 90주라 여전히 전량 청산이다(SIGNAL 하나로 제출)."""
    broker = SellBroker(cancelled=0, tracked=[_tracked_sell(OrderStatus.PARTIAL)])
    rm = _engine(monkeypatch, broker, partial=False, positions={SYM: Position(
        symbol=SYM, quantity=90, avg_price=PRICE, current_price=PRICE, strategy="sepa_trend")})
    rm._pending_quantities[SYM] = 90

    _drive(rm)

    assert [o.quantity for o in broker.orders] == [90]


def test_engine_late_balance_snapshot_does_not_turn_a_full_stop_into_a_partial(monkeypatch):
    """교차 리뷰 2회차 P1: 분할 여부를 현재 수량 비교로 추정하면, 늦게 도착한 잔고 스냅샷이 포트폴리오를 100주로
    되돌리는 순간 잔량 90주 손절이 '90 < 100 = 분할'로 읽혀 손절에 대기가 걸린다. 의도는 등록 시점에 남긴다."""
    broker = SellBroker(cancelled=0, tracked=[_tracked_sell(OrderStatus.PARTIAL)])
    rm = _engine(monkeypatch, broker, partial=False)      # 포트폴리오 100주
    rm._pending_quantities[SYM] = 90

    _drive(rm)

    assert [o.quantity for o in broker.orders] == [90] and SYM not in rm._pending_cancel_keep


@pytest.mark.parametrize("metadata, partial", [
    ({"quantity": 10, "exit_action": "sell_partial"}, True),
    ({"quantity": 10}, True),                                   # batch·core 트림 등 exit_action 없는 발행처
    ({"quantity": 100, "exit_action": "sell_all"}, False),
    ({"quantity": 100}, False),                                 # 수량이 보유 전량이면 과매도 여지가 없다
    ({"quantity": 100, "exit_action": "sell_partial"}, False),
    ({"quantity": 10, "exit_action": "sell_all"}, False),       # 의도가 전량이면 수량과 무관하게 전량
    ({"quantity": 90, "exit_action": "replacement_exit"}, False),   # 교체 축출은 전량 의도(발행 뒤 보유가 달라져도)
    ({}, False),                                                # 수량 미지정 = 보유 전량
])
def test_engine_records_the_partial_intent_when_the_sell_is_registered(monkeypatch, metadata, partial):
    from src.core.event import SignalEvent
    from src.core.types import MarketSession, Signal, SignalStrength, StrategyType

    rm = _rm(monkeypatch, SellBroker(), NOW, positions=_held_100())
    rm.engine.is_trading_hours = lambda: True
    rm.engine._get_current_session = lambda: MarketSession.REGULAR
    rm._risk_validator = None
    rm._pending_signal_cache[SYM] = {"score": 80.0}             # 어떤 경로로든 남은 옛 캐시

    sig = Signal(symbol=SYM, side=OrderSide.SELL, strength=SignalStrength.STRONG,
                 strategy=StrategyType.SEPA_TREND, price=PRICE, reason="청산", metadata=metadata)
    orders = asyncio.run(rm.on_signal(SignalEvent.from_signal(sig, source="test")))

    assert orders and SYM in rm._pending_orders
    assert rm._pending_signal_cache.get(SYM) == ({"sell_partial_intent": True} if partial else None)


def test_engine_cancel_count_from_another_order_does_not_bypass_the_gate(monkeypatch):
    """취소 건수는 종목 단위다 — 해제된 BUY 잔량이 취소된 1건을 SELL 취소로 읽으면 살아 있는 SELL 위에 시장가가 얹힌다."""
    broker = SellBroker(cancelled=1, tracked=[_tracked_sell()])
    rm = _engine(monkeypatch, broker)

    _drive(rm)

    assert broker.orders == [] and rm._pending_cancel_keep[SYM][0] == 1


def test_engine_opposite_side_order_is_not_evidence_that_the_sell_is_alive(monkeypatch):
    from test_engine_stale_pending_fixes import _tracked_buy

    broker = SellBroker(cancelled=1, tracked=[_tracked_buy()])
    rm = _engine(monkeypatch, broker)

    _drive(rm)

    assert [o.quantity for o in broker.orders] == [10]      # SELL 은 장부에서 빠졌다 = 취소 성공, 지연 없음


def test_engine_kept_sell_does_not_spend_the_market_fallback_budget(monkeypatch):
    """시장가 폴백 1회 뒤의 주문이 다시 stale 이 되고 취소가 실패해도 폴백 횟수는 그대로다."""
    broker = SellBroker(cancelled=0, tracked=[_tracked_sell()])
    rm = _engine(monkeypatch, broker)
    rm._pending_fallback_count[SYM] = 1

    _drive(rm)

    assert broker.orders == [] and rm._pending_fallback_count[SYM] == 1


def test_engine_heartbeat_drives_the_retry_without_another_signal(monkeypatch):
    """교차 리뷰 P1: 유지는 '다음에 다시 본다'는 약속인데 stale 루프는 SIGNAL 이 와야 돈다.

    부분체결만 돼도 스케줄러 장부는 지워지므로 받쳐 줄 주체가 없다 → 엔진 하트비트(직렬 큐, 10초)가 구동한다.
    """
    broker = SellBroker(cancelled=0, tracked=[_tracked_sell()], exchange_rows=ELSEWHERE)
    rm = _engine(monkeypatch, broker)

    _drive(rm)                                              # SIGNAL 하나 — 첫 회 대기
    _freeze_engine(monkeypatch, NOW + timedelta(seconds=10))
    _beat(rm)
    assert broker.calls == ["cancel"]                       # 재시도 간격 전

    _freeze_engine(monkeypatch, NOW + timedelta(seconds=20))
    _beat(rm)
    assert broker.calls == ["cancel", "cancel", EXCHANGE, "submit"]
    assert [o.quantity for o in broker.orders] == [10]


def test_engine_confirmed_alive_sell_backs_off_to_60_seconds(monkeypatch):
    """독립 리뷰 2회차 P2: 생존 확정 분기는 상한이 없다 — 20초 고정이면 한 종목 고착에 하루 천 건 넘는 취소 POST·원장
    조회가 나간다. 복구는 사람 손(MTS 취소)이라 20초가 사는 것도 없다. 판단 불가·첫 회 대기는 20초 그대로."""
    broker = SellBroker(cancelled=0, tracked=[_tracked_sell()], exchange_rows=ALIVE)
    rm = _engine(monkeypatch, broker, keep=1)

    _drive(rm)                                              # 생존 확인
    assert rm._pending_cancel_keep[SYM] == _SellKeep(1, NOW, True, True, NOW)
    _freeze_engine(monkeypatch, NOW + timedelta(seconds=59))
    _beat(rm)
    assert broker.calls == ["cancel", EXCHANGE]

    _freeze_engine(monkeypatch, NOW + timedelta(seconds=60))
    _beat(rm)
    assert broker.calls == ["cancel", EXCHANGE, "cancel", EXCHANGE]


def test_engine_undecidable_after_alive_returns_to_the_20_second_budget(monkeypatch):
    """교차 리뷰 3회차 P1: 간격을 '한 번이라도 생존을 봤는가'로 읽으면 이후 판단 불가 9회가 60초 간격이 돼 해제가
    9분 뒤다. 간격은 **직전 판정**을 따른다 — 생존 뒤 첫 재시도만 60초, 판단 불가로 바뀌면 20초씩."""
    broker = SellBroker(cancelled=0, tracked=[_tracked_sell()], exchange_rows=ALIVE)
    rm = _engine(monkeypatch, broker, keep=1)
    _drive(rm)                                               # T: 생존 확인
    broker._exchange_rows = None

    released_at = None
    for sec in range(10, 600, 10):                           # 10초 하트비트
        _freeze_engine(monkeypatch, NOW + timedelta(seconds=sec))
        _beat(rm)
        if SYM not in rm._pending_orders:
            released_at = sec
            break

    assert released_at == 180                                # T+60 에 첫 판단 불가, 이후 20초마다, 시간 예산(180초)에서 해제
    assert broker.calls.count("cancel") == 1 + 7             # T, 그리고 T+60·80·…·180 — 60초 간격이 고정되면 60·120·180 뿐
    assert broker.orders == []


def test_engine_many_kept_sells_release_on_the_same_time_budget(monkeypatch):
    """교차 리뷰 4회차 P1: 하트비트당 한 종목 + 횟수 상한이면 8종목의 재시도 간격이 80초가 돼 마지막 해제가 등록 후
    820초다. SIGNAL 도 스케줄러 장부도 없이(부분체결로 지워진 상태) 하트비트만으로 8종목이 같은 예산 안에 끝나야 한다."""
    symbols = [SYM] + [f"{i:06d}" for i in range(100001, 100008)]
    tracked = []
    for sym in symbols:
        order = _tracked_sell()
        order.symbol = sym
        tracked.append(order)
    broker = SellBroker(cancelled=0, tracked=tracked, exchange_rows=None)
    rm = _engine(monkeypatch, broker)
    for sym in symbols[1:]:
        _add_kept_partial_sell(rm, sym, waited=0)
        del rm._pending_cancel_keep[sym]                     # 아직 유지 전 — 같은 SIGNAL 에서 첫 회 대기에 들어간다
        rm._pending_timestamps[sym] = NOW - timedelta(seconds=100)

    _drive(rm)                                               # 등록 후 90초대의 SIGNAL 하나
    assert set(rm._pending_cancel_keep) == set(symbols)

    released_at = {}
    for sec in range(10, 900, 10):
        _freeze_engine(monkeypatch, NOW + timedelta(seconds=sec))
        _beat(rm)
        for sym in symbols:
            if sym not in rm._pending_orders:
                released_at.setdefault(sym, sec)
        if len(released_at) == len(symbols):
            break

    assert released_at == {sym: 180 for sym in symbols} and broker.orders == []


def test_engine_heartbeat_retries_every_due_symbol_and_skips_the_rest(monkeypatch):
    """때가 된 유지분은 한 하트비트에 모두 처리한다(오래 기다린 순) — 생존 확정(60초 간격)이라 아직 때가 아닌 종목은 건너뛴다."""
    broker = SellBroker(cancelled=0, tracked=[_tracked_sell()], exchange_rows=ALIVE)
    rm = _engine(monkeypatch, broker, keep=1)
    third = "035720"
    _add_kept_partial_sell(rm, OTHER, waited=40, alive=True)     # 60초 간격 — 아직
    _add_kept_partial_sell(rm, third, waited=30)                 # 20초 간격 — 때가 됐다

    _beat(rm)

    assert broker.calls.count("cancel") == 2
    assert [o.symbol for o in broker.orders] == [third]          # 장부에 없는 종목은 곧바로 종전 폴백
    assert rm._pending_cancel_keep[SYM].tried_at == NOW          # SYM 도 같은 하트비트에서 재시도됐다
    assert rm._pending_cancel_keep[OTHER].tried_at == NOW - timedelta(seconds=40)


def test_engine_heartbeat_only_touches_kept_sells_whose_retry_is_due(monkeypatch):
    """하트비트가 구동하는 것은 '간격이 지난 유지분의 재시도'뿐이다 — 폴백 상한 해제 같은 다른 분기를 앞당기지 않는다."""
    broker = SellBroker(cancelled=0, tracked=[_tracked_sell()], exchange_rows=ALIVE)
    rm = _engine(monkeypatch, broker)
    rm._pending_cancel_keep[SYM] = _SellKeep(1, NOW - timedelta(seconds=5), False, False, NOW - timedelta(seconds=5))
    rm._pending_fallback_count[SYM] = 2                     # 간격 검사보다 앞에 있는 상한 분기

    _beat(rm)
    assert SYM in rm._pending_orders and broker.calls == []

    _freeze_engine(monkeypatch, NOW + timedelta(seconds=15))
    _beat(rm)
    assert SYM not in rm._pending_orders                    # 때가 되면 종전 상한 분기대로 해제


def test_engine_heartbeat_does_nothing_without_a_kept_sell(monkeypatch):
    """하트비트는 유지분의 재시도만 구동한다 — 90초 폴백·10분 BUY 정리의 SIGNAL 의존은 종전 그대로."""
    broker = SellBroker(cancelled=1, tracked=[])
    rm = _engine(monkeypatch, broker)

    _beat(rm)

    assert broker.calls == []


def test_engine_heartbeat_is_silent_outside_regular_hours(monkeypatch):
    broker = SellBroker(cancelled=0, tracked=[_tracked_sell()], exchange_rows=ELSEWHERE)
    rm = _engine(monkeypatch, broker, keep=1)
    _freeze_engine(monkeypatch, datetime(2026, 9, 21, 15, 31))

    _beat(rm)

    assert broker.calls == []


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
    rm._pending_cancel_keep[SYM] = _SellKeep(5, NOW, False, False, NOW)

    sig = Signal(symbol=SYM, side=OrderSide.SELL, strength=SignalStrength.STRONG,
                 strategy=StrategyType.SEPA_TREND, price=PRICE, reason="1차 익절",
                 metadata={"quantity": 10})
    orders = asyncio.run(rm.on_signal(SignalEvent.from_signal(sig, source="test")))

    assert orders and SYM in rm._pending_orders
    assert SYM not in rm._pending_cancel_keep


def test_engine_intent_is_decided_with_the_quantity_not_after_the_quote_await(monkeypatch):
    """교차 리뷰 3회차 P1: 수량(보유 90주 전량)을 정한 뒤 매수1호가 조회를 기다리는 동안 늦은 잔고 동기화가 보유를
    100주로 되돌리면, 등록 시점의 보유량과 다시 비교하는 방식은 `90 < 100` 으로 전량 청산을 분할로 굳힌다."""
    from src.core.event import SignalEvent
    from src.core.types import MarketSession, Signal, SignalStrength, StrategyType

    position = Position(symbol=SYM, quantity=90, avg_price=PRICE, current_price=PRICE, strategy="core_holding")
    broker = SellBroker()

    async def get_best_bid(symbol):
        position.quantity = 100                              # 호가 조회 await 중 잔고 스냅샷이 덮어쓴다
        return 9990
    broker.get_best_bid = get_best_bid

    rm = _rm(monkeypatch, broker, NOW, positions={SYM: position})
    rm.engine.is_trading_hours = lambda: True
    rm.engine._get_current_session = lambda: MarketSession.REGULAR
    rm._risk_validator = None

    sig = Signal(symbol=SYM, side=OrderSide.SELL, strength=SignalStrength.STRONG,
                 strategy=StrategyType.SEPA_TREND, price=PRICE, reason="코어 손절")   # 수량·exit_action 없음
    orders = asyncio.run(rm.on_signal(SignalEvent.from_signal(sig, source="test")))

    assert orders and orders[0].order.quantity == 90
    assert SYM not in rm._pending_signal_cache               # 전량 의도 — 취소 실패 관문에 들어가지 않는다


# ── 스케줄러 경로: _cleanup_stale_pending (별도 장부, 3분) ────────────────────

@pytest.fixture
def home(tmp_path, monkeypatch):
    """ExitManager 가 ~/.cache/ai_trader 대신 tmp_path 를 쓰도록 Path.home() 패치 (운영 캐시 무접촉)."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


def _scheduler(monkeypatch, broker, *, age_min=4, engine_pending=True, partial=True):
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
    if partial:   # _check_exit_signal 이 sell_partial 로 등록하며 남기는 표식(등록 시각에 묶인다)
        scheduler._partial_exit_pending = {SYM: bot._exit_pending_timestamps[SYM]}

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
    scheduler._stale_cancel_miss = {SYM: (bot._exit_pending_timestamps[SYM], 0, False)}   # 이미 한 주기 기다렸다

    _sweep(scheduler)

    assert EXCHANGE in broker.calls
    assert state.pending_stage == ExitStage.FIRST and SYM in bot._exit_pending_symbols and cleared == []


def test_scheduler_retry_releases_once_gone_from_exchange(home, monkeypatch):
    """거래소에 SELL 미체결이 없으면 소멸 — 종전대로 양쪽 pending 해제 + stage 롤백(재판단 허용)."""
    broker = SellBroker(cancelled=0, tracked=[_tracked_sell()], exchange_rows=ELSEWHERE)
    scheduler, bot, state, cleared = _scheduler(monkeypatch, broker)
    scheduler._stale_cancel_miss = {SYM: (bot._exit_pending_timestamps[SYM], 0, False)}

    _sweep(scheduler)

    assert state.pending_stage is None and SYM not in bot._exit_pending_symbols and cleared == [SYM]
    assert SYM not in scheduler._stale_cancel_miss


def test_scheduler_miss_of_a_previous_pending_does_not_skip_the_first_wait(home, monkeypatch):
    """유지 표식은 pending 등록 시각에 묶인다 — 이전 pending 의 표식이 새 pending 의 첫 회 대기를 건너뛰게 하지 않는다."""
    broker = SellBroker(cancelled=0, tracked=[_tracked_sell()], exchange_rows=ELSEWHERE)
    scheduler, bot, state, cleared = _scheduler(monkeypatch, broker)
    scheduler._stale_cancel_miss = {SYM: (NOW - timedelta(hours=1), 1, True)}

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


@pytest.mark.parametrize("streak, released", [(0, False), (1, True)])
def test_scheduler_undecidable_keep_releases_at_5_minutes_without_rolling_back(home, monkeypatch, streak, released):
    """독립 리뷰 P1: 조회 실패가 이어질 때 이 장부가 엔진(270초)보다 오래 남으면 그동안 손절 신호 자체가 생성되지 않는다.

    연속 2회(등록 후 약 5분)에서 이 장부와 엔진 pending 을 푼다. stage 는 롤백하지 않는다 — 손절 판정은 다시
    열리고, 같은 분할 익절의 재발행은 ExitManager 검증자(거래소 확인 후에만 만료)가 계속 막는다.
    """
    broker = SellBroker(cancelled=0, tracked=[_tracked_sell()], exchange_rows=None)
    scheduler, bot, state, cleared = _scheduler(monkeypatch, broker)
    scheduler._stale_cancel_miss = {SYM: (bot._exit_pending_timestamps[SYM], streak, False)}

    _sweep(scheduler)

    assert (SYM not in bot._exit_pending_symbols) is released and cleared == ([SYM] if released else [])
    assert state.pending_stage == ExitStage.FIRST
    assert (SYM not in scheduler._stale_cancel_miss) is released


def test_scheduler_confirmed_alive_sell_is_kept_beyond_15_minutes(home, monkeypatch):
    """거래소 생존이 확인된 SELL 은 15분이 지나도 풀지 않는다 — 풀면 그 위에 같은 분할 익절이 재발행된다."""
    broker = SellBroker(cancelled=0, tracked=[_tracked_sell()], exchange_rows=ALIVE)
    scheduler, bot, state, cleared = _scheduler(monkeypatch, broker, age_min=40)
    scheduler._stale_cancel_miss = {SYM: (bot._exit_pending_timestamps[SYM], 1, False)}

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


def test_scheduler_alert_is_per_pending_not_per_symbol(home, monkeypatch):
    """경보 표식은 pending 등록 시각에 묶인다 — 체결로 끝난 이전 pending 의 표식이 같은 종목의 다음 경보를 막지 않는다."""
    broker = SellBroker(cancelled=0, tracked=[_tracked_sell()], exchange_rows=ALIVE)
    scheduler, bot, state, cleared = _scheduler(monkeypatch, broker)
    scheduler._stale_cancel_miss = {SYM: (NOW - timedelta(hours=2), 0, True)}   # 이전 pending 에서 이미 경보함

    _sweep(scheduler)                            # 새 pending 의 첫 회 대기
    scheduler._stale_cancel_last_try.clear()
    _sweep(scheduler)                            # 생존 확인

    assert scheduler.alerts == [("청산 주문 취소 불가", True)]


def test_scheduler_alert_failure_does_not_release_anything(home, monkeypatch):
    """경보 예외가 새어 나가면 호출측의 '취소 예외' 분기가 받아 15분 넘은 pending 을 강제 해제·롤백한다."""
    broker = SellBroker(cancelled=0, tracked=[_tracked_sell()], exchange_rows=ALIVE)
    scheduler, bot, state, cleared = _scheduler(monkeypatch, broker, age_min=40)
    scheduler._stale_cancel_miss = {SYM: (bot._exit_pending_timestamps[SYM], 0, False)}

    async def boom(*a, **kw):
        raise RuntimeError("텔레그램 오류")
    scheduler._send_error_alert = boom

    _sweep(scheduler)

    assert state.pending_stage == ExitStage.FIRST and SYM in bot._exit_pending_symbols and cleared == []


def test_scheduler_full_exit_keeps_the_old_path_so_stops_are_not_delayed(home, monkeypatch):
    """전량 청산(손절·트레일링·EOD)은 과매도 여지가 없다 — 종전대로 즉시 해제해 손절 재판단을 늦추지 않는다.

    교차 리뷰 2회차 P1: `pending_stage` 로 추정하면 안 된다 — '롤백 없는 해제' 뒤에는 옛 익절 stage 가 남은 채
    새 손절 pending 이 등록된다. 이 시험은 stage 가 남아 있는(FIRST) 상태에서 표식 없는 pending 을 푼다.
    """
    broker = SellBroker(cancelled=0, tracked=[_tracked_sell()])
    scheduler, bot, state, cleared = _scheduler(monkeypatch, broker, partial=False)
    assert state.pending_stage == ExitStage.FIRST

    _sweep(scheduler)

    assert SYM not in bot._exit_pending_symbols and cleared == [SYM]


def test_scheduler_partial_mark_of_an_older_pending_does_not_gate_a_new_stop(home, monkeypatch):
    broker = SellBroker(cancelled=0, tracked=[_tracked_sell()])
    scheduler, bot, state, cleared = _scheduler(monkeypatch, broker, partial=False)
    scheduler._partial_exit_pending = {SYM: NOW - timedelta(hours=1)}     # 이전 분할 익절의 표식

    _sweep(scheduler)

    assert SYM not in bot._exit_pending_symbols and cleared == [SYM]


@pytest.mark.parametrize("hook, rows, waited", [
    ("on_cancel", (), None), ("on_open_orders", ELSEWHERE, 0), ("on_open_orders", None, 1)],
    ids=["취소 await 중 교체", "생존 조회 await 중 교체(소멸 판정)", "생존 조회 await 중 교체(판단 불가 상한)"])
def test_scheduler_never_releases_a_pending_that_replaced_the_one_it_was_checking(home, monkeypatch, hook, rows, waited):
    """교차 리뷰 2회차 P1: await 사이에 옛 익절 A 가 체결로 끝나고 같은 종목의 다음 익절 B 가 등록되면,
    A 에 대한 '소멸' 판정으로 B 의 양쪽 pending·stage 를 풀어 B 가 중복 발행된다."""
    broker = SellBroker(cancelled=0, tracked=[_tracked_sell()], exchange_rows=rows)
    scheduler, bot, state, cleared = _scheduler(monkeypatch, broker)
    if waited is not None:
        scheduler._stale_cancel_miss = {SYM: (bot._exit_pending_timestamps[SYM], waited, False)}

    async def replace():
        bot._exit_pending_timestamps[SYM] = NOW          # B 의 등록 시각
    setattr(broker, hook, replace)

    _sweep(scheduler)

    assert SYM in bot._exit_pending_symbols and bot._exit_pending_timestamps[SYM] == NOW
    assert cleared == [] and state.pending_stage == ExitStage.FIRST


def test_scheduler_replacement_during_cancel_is_caught_for_full_exits_too(home, monkeypatch):
    """전량 청산은 관문을 타지 않고 곧바로 해제로 간다 — 해제 직전의 세대 재검증이 유일한 방어선이다."""
    broker = SellBroker(cancelled=0, tracked=[_tracked_sell()])
    scheduler, bot, state, cleared = _scheduler(monkeypatch, broker, partial=False)

    async def replace():
        bot._exit_pending_timestamps[SYM] = NOW
    broker.on_cancel = replace

    _sweep(scheduler)

    assert SYM in bot._exit_pending_symbols and cleared == [] and state.pending_stage == ExitStage.FIRST


def test_stop_loss_reopens_while_the_partial_stage_stays_gated(home, monkeypatch):
    """'롤백 없는 해제'의 전제: ExitManager 의 손절 판정은 pending_stage 관문보다 앞이고, 같은 분할 익절은 계속 막힌다.

    ExitManager 가 재배열돼 이 순서가 바뀌면 판단 불가 해제 뒤 손절이 조용히 막힌다 — 그때 이 시험이 깨져야 한다.
    """
    scheduler, bot, state, cleared = _scheduler(monkeypatch, SellBroker())
    monkeypatch.setattr(ExitManager, "_count_business_days", lambda self, s, e: 1)

    async def undecidable(symbol):
        return None
    bot.exit_manager.set_pending_verifier(undecidable)   # 운영과 같은 배선(하드 만료 30분)
    state.pending_since = datetime.now()                 # ExitManager 는 실제 시계를 쓴다
    assert state.pending_stage == ExitStage.FIRST

    assert bot.exit_manager.update_price(SYM, PRICE * Decimal("1.12")) is None       # 1차 익절 조건이지만 보류
    action, qty, reason = bot.exit_manager.update_price(SYM, PRICE * Decimal("0.90"))

    assert (action, qty) == ("sell_all", 100) and "손절" in reason
    assert state.pending_stage == ExitStage.FIRST


@pytest.mark.parametrize("action, marked", [("sell_partial", True), ("sell_all", False)])
def test_scheduler_marks_partial_exits_when_it_registers_them(home, monkeypatch, action, marked):
    """분할 표식은 발행 시점의 exit_action 으로 남고 등록 시각에 묶인다."""
    scheduler, bot, state, cleared = _scheduler(monkeypatch, SellBroker(), partial=False)
    bot._exit_pending_symbols.clear()
    bot._exit_pending_timestamps.clear()
    scheduler._partial_exit_pending = {SYM: NOW - timedelta(hours=1)}
    events = []

    async def emit(event):
        events.append(event)

    async def no_expire(symbol):
        return None

    position = Position(symbol=SYM, quantity=100, avg_price=PRICE, current_price=PRICE, strategy="sepa_trend")
    bot.engine.portfolio = SimpleNamespace(positions={SYM: position})
    bot.engine.emit = emit
    bot.engine.risk_manager._pending_sides, bot.engine.risk_manager._pending_fallback_count = {}, {}
    bot.engine.risk_manager._pending_orders = set()
    bot._pause_resume_at, bot._sell_blocked_symbols, bot._exit_reasons = None, {}, {}
    bot._strategy_exit_params = {}
    bot.exit_manager = SimpleNamespace(
        is_exit_exempt=lambda sym: False, maybe_expire_pending=no_expire,
        update_price=lambda sym, price, market_data=None: (action, 10 if marked else 100, "시험 청산"))

    asyncio.run(scheduler._check_exit_signal(SYM, PRICE))

    assert len(events) == 1 and SYM in bot._exit_pending_symbols
    expected = {SYM: bot._exit_pending_timestamps[SYM]} if marked else {}
    assert scheduler._partial_exit_pending == expected


def test_scheduler_cancel_count_from_another_order_does_not_bypass_the_gate(home, monkeypatch):
    broker = SellBroker(cancelled=1, tracked=[_tracked_sell()])
    scheduler, bot, state, cleared = _scheduler(monkeypatch, broker)

    _sweep(scheduler)

    assert state.pending_stage == ExitStage.FIRST and SYM in bot._exit_pending_symbols and cleared == []


def test_scheduler_overlapping_sweeps_do_not_repeat_the_cancel(home, monkeypatch):
    """교차 리뷰 P2: WS 틱·REST 폴링·독립 정리 루프가 같은 함수를 부른다 — 생존 판정 await 중에도 스로틀 표식이 남아야 한다."""
    broker = SellBroker(cancelled=0, tracked=[_tracked_sell()])
    scheduler, bot, state, cleared = _scheduler(monkeypatch, broker)
    broker.on_open_orders = lambda: scheduler._cleanup_stale_pending()   # 판정 도중 다른 호출이 끼어든다

    _sweep(scheduler)

    assert broker.calls == ["cancel"]


def test_scheduler_liveness_query_error_keeps_pending(home, monkeypatch):
    broker = SellBroker(cancelled=0, tracked=RuntimeError("조회 오류"))
    scheduler, bot, state, cleared = _scheduler(monkeypatch, broker)

    _sweep(scheduler)

    assert state.pending_stage == ExitStage.FIRST and SYM in bot._exit_pending_symbols and cleared == []
