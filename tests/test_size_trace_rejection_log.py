"""60차(2026-10-10) — 수량 계산 단계 추적(size_trace)과 신호 뒤 거절의 원장 기록.

10/8 005490: 위험 사이징 2,910,664원 → "배율 축소로 최소 금액 미달 (142,499)" → 수량 0.
어느 배율이 얼마를 깎았는지 로그로는 알 수 없었고, 수량 0 거절은 signal_events 에 남지 않았다.
"""
import asyncio
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from src.core.engine import RiskManager
from src.core.event import SignalEvent
from src.core.types import OrderSide, RiskConfig, Signal, SignalStrength, StrategyType
from src.strategies.exit_manager import ExitConfig, ExitManager
from src.utils.stop_policy import make_entry_stop_resolver

EQ = Decimal("16170354")      # 10/8 10:25 자산
STRATEGY_EXIT_PARAMS = {"gap_and_go": {"stop_loss_pct": 3.5, "trailing_stop_pct": 1.5},
                        "sepa_trend": {"stop_loss_pct": 5.0, "trailing_stop_pct": 3.0}}


def _rm(monkeypatch, tmp_path, *, cash="4153836", daily_pnl=Decimal("0"), overlays=None):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))  # ExitManager 캐시 경로 격리
    ov = overlays or {}
    monkeypatch.setattr("src.utils.calendar_seasonality.calendar_multiplier", lambda *_a, **_k: (ov.get("calendar", 1.0), ""))
    monkeypatch.setattr("src.utils.volatility_targeting.vol_targeting_multiplier", lambda *_a, **_k: (ov.get("vol", 1.0), ""))
    monkeypatch.setattr("src.utils.team_conviction.team_conviction_multiplier", lambda *_a, **_k: (ov.get("team", 1.0), ""))
    rm = object.__new__(RiskManager)
    rm.config = RiskConfig(sizing_mode="risk", risk_per_trade_pct=0.7, risk_max_position_pct=18.0,
                           base_position_pct=25.0, max_position_pct=28.0, min_position_value=200000,
                           daily_max_loss_pct=5.0,
                           strategy_allocation={"sepa_trend": 40.0, "gap_and_go": 15.0, "core_holding": 30.0})
    rm.engine = SimpleNamespace(
        portfolio=SimpleNamespace(total_equity=EQ, effective_daily_pnl=daily_pnl,
                                  get_strategy_allocation=lambda _s: Decimal("0")),
        get_available_cash=lambda: Decimal(cash))
    rm._reserved_by_order = {}
    rm._get_core_reserve = lambda: Decimal("0")
    rm._get_core_actual_value = lambda: Decimal("0")
    rm._pending_strategy_notional = lambda _s: Decimal("0")
    em = ExitManager(ExitConfig(stop_loss_pct=5.0, min_stop_pct=4.0, max_stop_pct=8.0, atr_multiplier=2.0))
    rm._resolve_entry_stop = make_entry_stop_resolver(em, STRATEGY_EXIT_PARAMS)
    return rm


def _sig(price, *, strategy=StrategyType.GAP_AND_GO, **meta):
    s = Signal(symbol="005490", side=OrderSide.BUY, strength=SignalStrength.NORMAL, strategy=strategy,
               price=Decimal(str(price)), metadata={"atr_pct": 2.7, **meta})
    return SignalEvent.from_signal(s, source="live_screening")


def test_trace_records_each_stage_on_success(monkeypatch, tmp_path):
    rm = _rm(monkeypatch, tmp_path)
    ev = _sig(62500)
    qty = rm._calculate_position_size(ev)
    t = ev.metadata["size_trace"]
    assert qty >= 1 and "stop" not in t and t["quantity"] == qty
    for key in ("equity", "available", "max_value", "risk_stop_pct", "after_risk_sizing",
                "strategy_remaining", "daily_loss_half_applied", "pre_multiplier_value",
                "position_multiplier", "after_overlays", "quantity_raw"):
        assert key in t, key
    assert t["risk_stop_pct"] == 3.5 and t["daily_loss_half_applied"] is False
    # 위험 사이징 2.91M 이 gap 예산 15%(2.43M) 에 클램프된 사실이 trace 에 그대로 남는다
    assert Decimal(t["after_strategy_budget"]) == min(Decimal(t["after_risk_sizing"]),
                                                      Decimal(t["strategy_remaining"]))
    assert Decimal(t["pre_multiplier_value"]) == Decimal(t["after_strategy_budget"])


def test_trace_explains_the_oct8_005490_zero_quantity(monkeypatch, tmp_path):
    """로그의 142,499원은 전략 잔여 예산 약 57만 → -27.6% 반감 ×0.5 → soft-reject ×0.5 로만 설명된다.
    20만 클램프 뒤 313,000원 1주 미달, 3주 보정(94만)도 잔여 예산 초과 → 수량 0."""
    rm = _rm(monkeypatch, tmp_path, daily_pnl=EQ * Decimal("-0.276"))
    rm.engine.portfolio.get_strategy_allocation = lambda _s: EQ * Decimal("0.15") - Decimal("570000")
    ev = _sig(313000, position_multiplier=0.5)
    assert rm._calculate_position_size(ev) == 0
    t = ev.metadata["size_trace"]
    assert t["stop"] == "quantity_below_one" and t["quantity"] == 0
    assert t["daily_loss_half_applied"] is True and t["position_multiplier"] == 0.5
    assert Decimal(t["strategy_remaining"]).quantize(Decimal("1")) == Decimal("570000")
    assert Decimal(t["pre_multiplier_value"]) == Decimal(t["after_strategy_budget"]) * Decimal("0.5")
    assert Decimal(t["after_position_multiplier"]) == Decimal(t["pre_multiplier_value"]) * Decimal("0.5")
    assert Decimal(t["after_position_multiplier"]).quantize(Decimal("1")) == Decimal("142500")
    assert t["min_clamped"] is True and Decimal(t["position_value"]) == Decimal("200000")
    assert t["quantity_raw"] == 0 and "min3_applied" not in t


def test_trace_marks_budget_exhaustion_stop(monkeypatch, tmp_path):
    rm = _rm(monkeypatch, tmp_path)
    rm.engine.portfolio.get_strategy_allocation = lambda _s: EQ  # 전략 예산 전부 점유
    ev = _sig(62500)
    assert rm._calculate_position_size(ev) == 0
    assert ev.metadata["size_trace"]["stop"] == "strategy_budget_exhausted"


def test_size_block_record_links_trace_and_cooldown_facts(monkeypatch, tmp_path):
    rm = _rm(monkeypatch, tmp_path, daily_pnl=EQ * Decimal("-0.276"))
    rm.engine.portfolio.get_strategy_allocation = lambda _s: EQ * Decimal("0.15") - Decimal("570000")
    ev = _sig(313000, position_multiplier=0.5)
    assert rm._calculate_position_size(ev) == 0
    reason, meta = RiskManager._size_block_record(rm, ev)
    assert reason == "size_zero:quantity_below_one"
    assert meta["engine_cooldown_consumed"] is True
    assert meta["screening_cooldown_consumed"] is True   # live_screening 은 큐 삽입 시 이미 소진
    assert meta["size_trace"]["stop"] == "quantity_below_one"


def test_log_sig_carries_signal_id_and_caller_metadata(monkeypatch):
    captured = {}

    class _Fake:
        async def log(self, **kw):
            captured.update(kw)

    monkeypatch.setattr("src.core.engine._SigLog.get", staticmethod(lambda: _Fake()))
    rm = object.__new__(RiskManager)
    rm.engine = SimpleNamespace(_market_regime="sideways")
    ev = _sig(313000)

    async def run():
        RiskManager._log_sig(rm, ev, event_type="blocked", block_gate="G3_size",
                             block_reason="size_zero:quantity_below_one",
                             metadata={"size_trace": {"stop": "quantity_below_one"},
                                       "engine_cooldown_consumed": True})
        await asyncio.sleep(0)

    asyncio.run(run())
    assert captured["block_gate"] == "G3_size"
    assert captured["metadata"]["signal_id"] == ev.id
    assert captured["metadata"]["size_trace"]["stop"] == "quantity_below_one"
    assert captured["metadata"]["engine_cooldown_consumed"] is True
