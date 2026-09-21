"""자동매도 절대 금지(exit_exempt, CORE-023) — 가드 없는 SELL 발행처 고정 시험.

2026-09-21 PR #81 독립·교차 리뷰에서 확인된 잠재 공백:
  1. 코어 경로(조기경보·stale·리밸런싱·트림)는 `core_holding` + `rebalance_exclude` 로만 거른다.
  2. 전략 자체 청산(gap_and_go)은 `position.strategy` 를 보지 않고 SELL 을 낸다.

수정: 엔진 `RiskManager.on_signal` 이 면제 종목 SELL 을 발행처와 무관하게 막고(최종 방어선),
코어 경로는 면제 종목을 `rebalance_exclude` 처럼 대상에서 뺀다(오경보·리밸런싱 교착 방지).

전부 합성 입력 — 네트워크·운영 캐시 무접촉(Path.home 은 tmp_path).
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT, ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from src.core.batch_analyzer import BatchAnalyzer  # noqa: E402
from src.core.engine import StrategyManager  # noqa: E402
from src.core.event import MarketDataEvent, SignalEvent  # noqa: E402
from src.core.types import (  # noqa: E402
    OrderSide, Position, Signal, SignalStrength, StrategyType,
)
from src.strategies.exit_manager import ExitConfig, ExitManager  # noqa: E402
from src.strategies.kr.gap_and_go import GapAndGoStrategy  # noqa: E402

from test_risk_sizing import _rm, home  # noqa: E402,F401
from test_t11_entry_plan import _order_env  # noqa: E402

EXEMPT = "087010"
OTHER = "005930"


def _pos(symbol, strategy, *, avg="10000", cur="10000", qty=100):
    return Position(symbol=symbol, quantity=qty, avg_price=Decimal(avg),
                    current_price=Decimal(cur), strategy=strategy)


def _sell_rm(monkeypatch, positions, exempt):
    """on_signal SELL 경로를 구동할 엔진 RiskManager — 면제 세트는 ExitManager live set 참조."""
    rm = _rm(monkeypatch, mode="nominal")
    _order_env(monkeypatch, rm)
    rm.engine.portfolio.positions = positions
    rm._pending_exit_reasons = {}
    rm._exempt_block_logged = {}
    rm._exempt_cancel_last_try = {}
    rm._exit_exempt_ref = exempt

    async def _sell_price(_symbol, fallback):
        return fallback
    rm._get_sell_price = _sell_price

    # 브로커 대역 — 실제 KIS 계약을 따른다: 취소는 실패를 예외로 올리지 않고 건수만 돌려주며,
    # 성공한 주문만 추적(get_open_orders)에서 빠진다. cancel_ok=False 면 거절(0건·추적 유지).
    rm.cancelled, rm.submitted, rm.open_orders, rm.cancel_ok = [], [], [], True
    rm.on_cancel = lambda _symbol: None   # 취소 await 도중에 일어나는 일을 끼워 넣는 훅

    async def _cancel(symbol):
        rm.cancelled.append(symbol)
        rm.on_cancel(symbol)
        if not rm.cancel_ok:
            return 0
        _mine = [o for o in rm.open_orders if o.symbol == symbol]
        rm.open_orders[:] = [o for o in rm.open_orders if o.symbol != symbol]
        return len(_mine)

    async def _open_orders():
        return list(rm.open_orders)

    async def _submit(order):
        rm.submitted.append(order)
        return True, "ORD1"
    rm.engine.broker = SimpleNamespace(cancel_all_for_symbol=_cancel, submit_order=_submit,
                                       get_open_orders=_open_orders)
    return rm


def _stale_sell(rm, symbol, *, age_sec=120):
    """age_sec 전에 나간 지정가 SELL 이 미체결로 남아 있는 상태 (엔진 pending + 브로커 추적)."""
    from datetime import timedelta
    from test_t11_entry_plan import NOW
    rm._pending_orders.add(symbol)
    rm._pending_timestamps[symbol] = NOW - timedelta(seconds=age_sec)
    rm._pending_sides[symbol] = OrderSide.SELL
    rm._pending_quantities[symbol] = 100
    rm.open_orders.append(SimpleNamespace(symbol=symbol, is_active=True))


def _sell_event(symbol, source, strategy=StrategyType.CORE_HOLDING, **meta):
    return SignalEvent.from_signal(
        Signal(symbol=symbol, side=OrderSide.SELL, strength=SignalStrength.STRONG,
               strategy=strategy, price=Decimal("9000"), score=100, reason=source,
               metadata=dict(meta)),
        source=source,
    )


# ── 1) 엔진 중앙 가드: 발행처와 무관하게 면제 종목 SELL 은 주문이 되지 않는다 ─────

@pytest.mark.parametrize("source,meta", [
    ("core_early_alert", {"is_core": True, "early_alert": True}),
    ("core_stale_auto_sell", {"is_core": True, "stale_auto_sell": True}),
    ("core_rebalance", {"is_core": True, "rebalance": True}),
    ("core_trim", {"is_core": True, "trim": True, "quantity": 10}),
    ("gap_and_go", {}),
])
def test_engine_blocks_exempt_sell_from_any_source(home, monkeypatch, source, meta):
    rm = _sell_rm(monkeypatch, {EXEMPT: _pos(EXEMPT, "core_holding")}, {EXEMPT})
    rm._pending_exit_reasons[EXEMPT] = "코어홀딩 조기경보"

    assert asyncio.run(rm.on_signal(_sell_event(EXEMPT, source, **meta))) is None
    assert EXEMPT not in rm._pending_orders, "막힌 SELL 이 pending 을 남기면 안 된다"
    assert EXEMPT not in rm._pending_exit_reasons, "막힌 SELL 의 청산 사유가 남으면 안 된다"


def test_engine_still_sells_non_exempt_and_follows_live_set(home, monkeypatch):
    exempt = {EXEMPT}
    rm = _sell_rm(monkeypatch, {OTHER: _pos(OTHER, "core_holding"),
                                EXEMPT: _pos(EXEMPT, "manual")}, exempt)

    orders = asyncio.run(rm.on_signal(_sell_event(OTHER, "core_early_alert")))
    assert orders and orders[0].order.side == OrderSide.SELL and orders[0].order.quantity == 100

    # live set 참조 — 면제 해제(remove_exit_exempt) 직후의 SELL 도 바로 통과한다
    # (차단 경고 스로틀이 신호 쿨다운 _last_signal_time 을 오염시키면 30초간 다시 막힌다)
    assert asyncio.run(rm.on_signal(_sell_event(EXEMPT, "exit_manager"))) is None
    assert EXEMPT not in rm._last_signal_time
    exempt.discard(EXEMPT)
    assert asyncio.run(rm.on_signal(_sell_event(EXEMPT, "exit_manager")))


# ── 1-b) on_signal 을 거치지 않는 직접 제출 경로 — 면제가 런타임에 등록된 경우 ─────

def test_stale_sell_fallback_never_resubmits_exempt_symbol(home, monkeypatch):
    """면제 등록 전에 나간 지정가 SELL 이 90초 넘게 미체결 → 다른 종목 신호가 stale 루프를 돌려도
    시장가 재주문 없이 취소·pending 해제만 한다."""
    rm = _sell_rm(monkeypatch, {EXEMPT: _pos(EXEMPT, "sepa_trend"),
                                OTHER: _pos(OTHER, "sepa_trend")}, {EXEMPT})
    _stale_sell(rm, EXEMPT)

    asyncio.run(rm.on_signal(_sell_event(OTHER, "exit_manager")))

    assert rm.submitted == [], "면제 종목에 시장가 폴백이 나가면 안 된다"
    assert rm.cancelled == [EXEMPT]
    assert EXEMPT not in rm._pending_orders and EXEMPT not in rm._pending_timestamps


def test_stale_exempt_sell_keeps_pending_when_cancel_fails(home, monkeypatch):
    """취소 0건은 주문 소멸의 증거가 아니다 — 브로커 추적에 남아 있으면 pending 을 풀지 않고
    (풀면 취소 재시도 대상에서 빠진 채 지정가가 체결될 수 있다) 60초 간격으로 다시 취소한다.
    pending 시각은 되감지 않는다 — 경과가 흘러야 헬스 모니터의 300초 교착 경보가 뜬다."""
    from datetime import timedelta
    import src.core.engine as eng
    from test_t11_entry_plan import NOW, _freeze_clock
    rm = _sell_rm(monkeypatch, {EXEMPT: _pos(EXEMPT, "sepa_trend"),
                                OTHER: _pos(OTHER, "sepa_trend")}, {EXEMPT})
    _stale_sell(rm, EXEMPT)
    issued_at = rm._pending_timestamps[EXEMPT]
    rm.cancel_ok = False

    asyncio.run(rm.on_signal(_sell_event(OTHER, "exit_manager")))
    assert rm.cancelled == [EXEMPT] and rm.submitted == []
    assert EXEMPT in rm._pending_orders, "취소 실패 — 거래소 주문이 살아 있을 수 있으므로 추적 유지"
    assert rm._pending_timestamps[EXEMPT] == issued_at, "경과 시간을 숨기지 않는다"

    # 60초 안의 다음 신호는 취소 API 를 다시 때리지 않는다
    _freeze_clock(monkeypatch, eng, NOW + timedelta(seconds=59))
    asyncio.run(rm.on_signal(_sell_event(OTHER, "exit_manager")))
    assert rm.cancelled == [EXEMPT]

    # 60초가 지나면 재취소 — 이번엔 성공하므로 그때 해제한다 (재주문은 끝까지 없다)
    rm.cancel_ok = True
    _freeze_clock(monkeypatch, eng, NOW + timedelta(seconds=61))
    asyncio.run(rm.on_signal(_sell_event(OTHER, "exit_manager")))
    assert rm.cancelled == [EXEMPT, EXEMPT] and rm.submitted == []
    assert EXEMPT not in rm._pending_orders and EXEMPT not in rm._exempt_cancel_last_try


def test_stale_fallback_aborts_when_exemption_lands_during_cancel(home, monkeypatch):
    """비면제로 stale 루프에 들어온 뒤 취소 await 도중 면제가 등록되면 시장가 재주문을 멈춘다."""
    exempt = set()
    rm = _sell_rm(monkeypatch, {EXEMPT: _pos(EXEMPT, "sepa_trend"),
                                OTHER: _pos(OTHER, "sepa_trend")}, exempt)
    _stale_sell(rm, EXEMPT)
    rm.on_cancel = lambda symbol: exempt.add(symbol)   # run_manual_buy_orders 가 그 사이 등록

    asyncio.run(rm.on_signal(_sell_event(OTHER, "exit_manager")))

    assert rm.cancelled == [EXEMPT]
    assert rm.submitted == [], "면제 등록 뒤에는 시장가 폴백이 나가면 안 된다"


def test_stale_fallback_still_resubmits_non_exempt_symbol(home, monkeypatch):
    """대조군 — 비면제 종목의 시장가 폴백은 종전대로 나간다."""
    rm = _sell_rm(monkeypatch, {EXEMPT: _pos(EXEMPT, "sepa_trend"),
                                OTHER: _pos(OTHER, "sepa_trend")}, set())
    _stale_sell(rm, EXEMPT)

    asyncio.run(rm.on_signal(_sell_event(OTHER, "exit_manager")))

    assert [(o.symbol, o.side, o.quantity) for o in rm.submitted] == [(EXEMPT, OrderSide.SELL, 100)]


def test_on_order_rechecks_exemption_before_submit(home, monkeypatch):
    from src.core.event import OrderEvent
    from src.core.types import Order, OrderType
    exempt = set()
    rm = _sell_rm(monkeypatch, {EXEMPT: _pos(EXEMPT, "sepa_trend")}, exempt)

    def _order_event():
        rm._pending_orders.add(EXEMPT)
        return OrderEvent.from_order(
            Order(symbol=EXEMPT, side=OrderSide.SELL, order_type=OrderType.LIMIT, quantity=100,
                  price=Decimal("9000"), strategy="sepa_trend", reason="손절"),
            source="risk_manager")

    asyncio.run(rm.on_order(_order_event()))
    assert len(rm.submitted) == 1, "대조군: 비면제 SELL 은 제출된다"

    exempt.add(EXEMPT)   # on_signal 통과 뒤 면제 등록
    asyncio.run(rm.on_order(_order_event()))
    assert len(rm.submitted) == 1, "면제 종목 SELL 은 제출 직전에 막힌다"
    assert EXEMPT not in rm._pending_orders


def test_scheduler_exit_check_leaves_no_pending_for_exempt(home):
    """중앙 가드가 고아 pending 을 남기지 않는 근거 — 실시간 청산 체크는 등록 전에 면제를 돌려보낸다."""
    from src.schedulers.kr_scheduler import KRScheduler
    em = ExitManager(ExitConfig())
    em.add_exit_exempt(EXEMPT, reason="test")
    emitted = []

    async def _emit(event):
        emitted.append(event)
    sched = object.__new__(KRScheduler)
    sched.bot = SimpleNamespace(
        exit_manager=em, broker=object(), engine=SimpleNamespace(emit=_emit),
        _exit_pending_symbols=set(), _exit_pending_timestamps={}, _exit_reasons={},
    )

    asyncio.run(sched._check_exit_signal(EXEMPT, Decimal("1")))

    assert emitted == [] and sched.bot._exit_pending_symbols == set()
    assert sched.bot._exit_pending_timestamps == {} and sched.bot._exit_reasons == {}


def test_scheduler_cleanup_keeps_exempt_pending_while_sell_may_be_live(home, monkeypatch):
    """엔진이 취소 실패로 유지한 pending 을 스케줄러 3분 정리가 다시 풀면 안 된다 —
    취소 0건 뒤에도 브로커 추적에 SELL 이 남아 있으면 양쪽 pending 을 보존하고 60초 뒤 재취소한다."""
    from datetime import timedelta
    import src.schedulers.kr_scheduler as ks
    from test_t11_entry_plan import NOW, _freeze_clock
    _freeze_clock(monkeypatch, ks)
    em = ExitManager(ExitConfig())
    em.add_exit_exempt(EXEMPT, reason="test")
    rolled, cleared, cancelled = [], [], []
    monkeypatch.setattr(em, "rollback_stage", lambda sym: rolled.append(sym))
    open_orders = [SimpleNamespace(symbol=EXEMPT, is_active=True),
                   SimpleNamespace(symbol=OTHER, is_active=True)]
    state = {"cancel_ok": False}

    async def _cancel(symbol):
        cancelled.append(symbol)
        if not state["cancel_ok"]:
            return 0
        open_orders[:] = [o for o in open_orders if o.symbol != symbol]
        return 1

    async def _open():
        return list(open_orders)

    async def _clear(symbol):
        cleared.append(symbol)
    issued = NOW - timedelta(minutes=10)   # 장전 5분·정규장 3분 기준 모두 초과
    sched = object.__new__(ks.KRScheduler)
    sched.bot = SimpleNamespace(
        exit_manager=em,
        broker=SimpleNamespace(cancel_all_for_symbol=_cancel, get_open_orders=_open),
        engine=SimpleNamespace(risk_manager=SimpleNamespace(clear_pending=_clear,
                                                            _pending_orders={EXEMPT, OTHER})),
        _exit_pending_symbols={EXEMPT, OTHER},
        _exit_pending_timestamps={EXEMPT: issued, OTHER: issued},
    )

    asyncio.run(sched._cleanup_stale_pending())
    # 비면제는 종전대로 해제(기존 동작 불변), 면제는 양쪽 pending 보존·stage 롤백 없음
    assert cleared == [OTHER] and rolled == [OTHER]
    assert sched.bot._exit_pending_symbols == {EXEMPT}
    assert sched.bot._exit_pending_timestamps == {EXEMPT: issued}

    # 60초 안에는 취소 API 를 다시 때리지 않고, 지나면 재취소 → 성공 시 그때 해제
    _freeze_clock(monkeypatch, ks, NOW + timedelta(seconds=59))
    asyncio.run(sched._cleanup_stale_pending())
    assert cancelled.count(EXEMPT) == 1
    state["cancel_ok"] = True
    _freeze_clock(monkeypatch, ks, NOW + timedelta(seconds=61))
    asyncio.run(sched._cleanup_stale_pending())
    assert cancelled.count(EXEMPT) == 2 and cleared == [OTHER, EXEMPT]
    assert sched.bot._exit_pending_symbols == set()


# ── 2) 전략 자체 청산: gap_and_go 는 남의(manual) 포지션에도 SELL 을 낸다 → 엔진이 막는다 ──

def test_gap_strategy_sell_on_exempt_manual_position_never_becomes_order(home, monkeypatch):
    gap = GapAndGoStrategy()
    monkeypatch.setattr(gap, "get_indicators", lambda _s: {"close": 9800.0})
    monkeypatch.setattr(gap, "_calculate_indicators", lambda _s: None)
    # 같은 날 갭 후보로 등록된 뒤 MTS 수동 매수 → 동기화로 manual 포지션이 된 면제 종목
    gap._gap_date = datetime.now().date()
    gap._gap_stocks[EXEMPT] = {"gap_pct": 3.0, "open_price": Decimal("10000"),
                               "high_price": Decimal("10000"), "low_price": Decimal("9800"),
                               "detected_at": datetime.now()}
    positions = {EXEMPT: _pos(EXEMPT, "manual", cur="9800")}

    sm = object.__new__(StrategyManager)
    sm.engine = SimpleNamespace(
        update_position_price=lambda *_a: None,
        get_available_cash=lambda: Decimal("0"),
        portfolio=SimpleNamespace(positions=positions),
        stats=SimpleNamespace(signals_generated=0),
    )
    sm.strategies, sm.enabled_strategies = {"gap_and_go": gap}, ["gap_and_go"]

    tick = MarketDataEvent(symbol=EXEMPT, open=Decimal("9800"), high=Decimal("9800"),
                           low=Decimal("9800"), close=Decimal("9800"), volume=1000)
    signals = asyncio.run(sm.on_market_data(tick))
    # 특성화: 발행처(전략)는 position.strategy 를 보지 않고 SELL 을 낸다 — 가드는 엔진에 둔다
    assert [s.side for s in signals] == [OrderSide.SELL]

    rm = _sell_rm(monkeypatch, positions, {EXEMPT})
    assert asyncio.run(rm.on_signal(signals[0])) is None
    assert EXEMPT not in rm._pending_orders


# ── 3) 코어 경로: 면제 종목은 대상에서 빠진다 (오경보·리밸런싱 교착 방지) ─────────

def _core_ba(monkeypatch, tmp_path, positions, exempt, *, candidates=None):
    alerts, emitted = [], []

    async def _alert(text, **_k):
        alerts.append(text)
        return True
    monkeypatch.setattr("src.utils.telegram.send_alert", _alert)

    async def _emit(event):
        emitted.append(event)

    async def _no_daily(*_a, **_k):
        return None

    async def _scan():
        return candidates if candidates is not None else []

    em = ExitManager(ExitConfig())
    for s in exempt:
        em.add_exit_exempt(s, reason="test")
    ba = object.__new__(BatchAnalyzer)
    ba._config = {"core_holding": {"enabled": True, "max_positions": 3}}
    ba._engine = SimpleNamespace(portfolio=SimpleNamespace(positions=positions),
                                 emit=_emit, risk_manager=None)
    ba._broker = SimpleNamespace(get_daily_prices=_no_daily)
    ba._exit_manager = em
    ba._core_screener = SimpleNamespace(run_full_scan=_scan)
    ba._core_strategy = object()
    ba._core_state_path = tmp_path / "core_state.json"
    return ba, emitted, alerts


def test_core_early_alert_skips_exempt_core_position(home, monkeypatch, tmp_path):
    positions = {EXEMPT: _pos(EXEMPT, "core_holding", cur="8000"),   # -20% — 조기경보 임계 -12% 초과
                 OTHER: _pos(OTHER, "core_holding", cur="8000")}
    ba, emitted, alerts = _core_ba(monkeypatch, tmp_path, positions, {EXEMPT})

    asyncio.run(ba._monitor_core_positions())

    assert [e.symbol for e in emitted] == [OTHER], "면제 종목은 SELL 도 '즉시 매도' 알림도 없어야 한다"
    assert len(alerts) == 1 and OTHER in alerts[0] and EXEMPT not in alerts[0]


def test_core_rebalance_fallback_stop_skips_exempt(home, monkeypatch, tmp_path):
    positions = {EXEMPT: _pos(EXEMPT, "core_holding", cur="8500"),   # -15% — 폴백 손절 -10% 초과
                 OTHER: _pos(OTHER, "core_holding", cur="8500")}
    ba, emitted, _ = _core_ba(monkeypatch, tmp_path, positions, {EXEMPT})

    assert asyncio.run(ba.execute_core_rebalance()) is True
    assert [(e.symbol, e.side) for e in emitted] == [(OTHER, OrderSide.SELL)]


def test_core_rebalance_replace_skips_exempt_and_does_not_stall_buys(home, monkeypatch, tmp_path):
    """면제 코어가 교체 대상이 되면 매수가 'sold 미체결' 대기로 묶인다 — 대상에서 빼야 매수가 나간다."""
    new = SimpleNamespace(symbol="000660", name="신규", score=90.0, entry_price=Decimal("50000"),
                          reasons=["r"], indicators={})
    weak = SimpleNamespace(symbol=EXEMPT, name="면제", score=40.0, entry_price=Decimal("10000"),
                           reasons=["r"], indicators={})
    ba, emitted, _ = _core_ba(monkeypatch, tmp_path, {EXEMPT: _pos(EXEMPT, "core_holding")},
                              {EXEMPT}, candidates=[new, weak])

    assert asyncio.run(ba.execute_core_rebalance()) is True
    assert [(e.symbol, e.side) for e in emitted] == [("000660", OrderSide.BUY)]
    assert ba.get_core_state().get("sold") == []
