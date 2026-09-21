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
    rm._exit_exempt_ref = exempt

    async def _sell_price(_symbol, fallback):
        return fallback
    rm._get_sell_price = _sell_price
    return rm


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

    # live set 참조 — 면제 해제(remove_exit_exempt) 뒤에는 같은 종목 SELL 이 통과한다
    assert asyncio.run(rm.on_signal(_sell_event(EXEMPT, "exit_manager"))) is None
    exempt.discard(EXEMPT)
    rm._last_signal_time.clear()
    assert asyncio.run(rm.on_signal(_sell_event(EXEMPT, "exit_manager")))


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
        return candidates or []

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
