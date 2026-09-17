"""Task 10B1: characterize the actual legacy sizing wrapper before kernel extraction."""

from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import src.core.engine as engine_module
from src.core.engine import RiskManager
from src.core.event import SignalEvent
from src.core.types import (
    HybridConfig, OrderSide, Portfolio, Position, RiskConfig, Signal,
    SignalStrength, StrategyType,
)
from src.utils.sizing import atr_position_multiplier, planned_risk
from src.utils.stop_policy import StopDecision


KST = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 9, 18, 10, 0, tzinfo=KST)
EQUITY = Decimal("10000000")
PRICE = Decimal("10000")


class _FixedDate(date):
    @classmethod
    def today(cls):
        return NOW.date()


@pytest.fixture
def external_factors(monkeypatch):
    """The three external overlays and date are explicit deterministic boundaries."""
    calls = []
    values = {"calendar": 1.0, "volatility": 1.0, "conviction": 1.0}
    monkeypatch.setattr(engine_module, "date", _FixedDate)
    monkeypatch.setattr(
        "src.utils.calendar_seasonality.calendar_multiplier",
        lambda day, market: (calls.append(("calendar", day, market)) or values["calendar"], "synthetic"),
    )
    monkeypatch.setattr(
        "src.utils.volatility_targeting.vol_targeting_multiplier",
        lambda strategy: (calls.append(("volatility", strategy)) or values["volatility"], "synthetic"),
    )
    monkeypatch.setattr(
        "src.utils.team_conviction.team_conviction_multiplier",
        lambda symbol: (calls.append(("conviction", symbol)) or values["conviction"], "synthetic"),
    )
    return calls, values


def _portfolio(*, cash=EQUITY, positions=(), daily_pnl=Decimal("0")):
    return Portfolio(
        cash=Decimal(cash), initial_capital=EQUITY, daily_pnl=Decimal(daily_pnl),
        positions={position.symbol: position for position in positions},
    )


def _config(*, mode="nominal", core_pct=30.0, hybrid=None, min_value=200000, sepa_cap=42.0):
    return RiskConfig(
        sizing_mode=mode, base_position_pct=25.0, max_position_pct=28.0,
        min_position_value=min_value, daily_max_loss_pct=5.0,
        risk_per_trade_pct=0.7, risk_max_position_pct=18.0,
        hybrid=hybrid or HybridConfig(),
        strategy_allocation={"core_holding": core_pct, "sepa_trend": sepa_cap,
                             "mean_reversion": 0.0, "momentum_breakout": 0.0},
    )


def _manager(*, config=None, portfolio=None, available=None, stop=None):
    """Actual RiskManager instance with only the I/O-free sizing dependencies supplied."""
    portfolio = portfolio or _portfolio()
    manager = object.__new__(RiskManager)
    manager.config = config or _config()
    manager.engine = SimpleNamespace(
        portfolio=portfolio,
        get_available_cash=lambda: Decimal(available if available is not None else portfolio.cash),
    )
    manager._reserved_by_order = {}
    manager._pending_strategy = {}
    manager._entry_risk_cfg_hash = "synthetic-sizing-config"
    if stop is not None:
        manager._resolve_entry_stop = stop
    return manager


def _signal(*, strategy=StrategyType.SEPA_TREND, strength=SignalStrength.NORMAL,
            price=PRICE, metadata=None):
    raw = Signal(
        symbol="005930", side=OrderSide.BUY, strength=strength, strategy=strategy,
        price=Decimal(price), metadata=dict(metadata or {}), timestamp=NOW,
    )
    event = SignalEvent.from_signal(raw, source="sizing-characterization")
    event.timestamp = NOW
    return event


def _stop_recorder(calls, *, pct="5", source="strategy", crash=False):
    def resolve(strategy):
        calls.append(("resolver", strategy))
        return StopDecision(Decimal(pct), source, crash)
    return resolve


@pytest.mark.parametrize(
    ("strength", "expected"),
    [(SignalStrength.WEAK, 87), (SignalStrength.NORMAL, 175),
     (SignalStrength.STRONG, 196), (SignalStrength.VERY_STRONG, 196)],
)
def test_nominal_strengths_use_noncore_pool_and_call_all_overlay_boundaries(external_factors, strength, expected):
    calls, _ = external_factors
    manager = _manager()

    assert manager._calculate_position_size(_signal(strength=strength)) == expected
    assert calls == [("calendar", NOW.date(), "kr"), ("volatility", "sepa_trend"), ("conviction", "005930")]


def test_core_bypasses_risk_resolver_but_keeps_nominal_size(external_factors):
    calls, _ = external_factors
    resolver = []
    manager = _manager(config=_config(mode="risk"), stop=_stop_recorder(resolver))
    event = _signal(strategy=StrategyType.CORE_HOLDING)

    assert manager._calculate_position_size(event) == 100
    assert resolver == []
    assert "sizing_mode" not in event.signal.metadata
    assert [name for name, *_ in calls] == ["calendar", "volatility", "conviction"]


def test_hybrid_uses_horizon_pool_and_keeps_core_reserve_behavior(external_factors):
    hybrid = HybridConfig(enabled=True)
    noncore = _manager(config=_config(hybrid=hybrid))
    core = _manager(config=_config(hybrid=hybrid))

    assert noncore._calculate_position_size(_signal()) == 40
    assert core._calculate_position_size(_signal(strategy=StrategyType.CORE_HOLDING)) == 100


def test_risk_mode_final_fee_cap_is_139_not_140_and_snapshots_are_independent(external_factors):
    calls, _ = external_factors
    resolver_calls = []
    manager = _manager(config=_config(mode="risk"), stop=_stop_recorder(resolver_calls))
    event = _signal(metadata={"atr_pct": 6.0, "position_multiplier": atr_position_multiplier(6.0)})

    quantity = manager._calculate_position_size(event)

    assert quantity == 139
    assert planned_risk(PRICE, 139, Decimal("5")) <= Decimal("70000")
    assert planned_risk(PRICE, 140, Decimal("5")) > Decimal("70000")
    assert resolver_calls == [("resolver", "sepa_trend")]
    assert [name for name, *_ in calls] == ["calendar", "volatility", "conviction"]
    assert event.signal.metadata["sizing_mode"] == "risk"
    assert event.metadata["entry_risk"] == event.signal.metadata["entry_risk"]
    assert event.metadata["entry_risk"] is not event.signal.metadata["entry_risk"]
    event.metadata["entry_risk"]["planned_quantity"] = -1
    assert event.signal.metadata["entry_risk"]["planned_quantity"] == 139


def test_loss_reduction_and_downscale_overlay_are_not_reinflated(external_factors):
    calls, values = external_factors
    portfolio = _portfolio(daily_pnl=Decimal("-250000"))
    manager = _manager(portfolio=portfolio)
    values["volatility"] = 0.5

    assert manager._calculate_position_size(_signal()) == 43
    assert [name for name, *_ in calls] == ["calendar", "volatility", "conviction"]


def test_boost_overlays_reclamp_to_risk_cap_after_calendar_and_conviction(external_factors):
    calls, values = external_factors
    resolver_calls = []
    values.update(calendar=1.1, conviction=1.2)
    manager = _manager(config=_config(mode="risk"), stop=_stop_recorder(resolver_calls))

    assert manager._calculate_position_size(_signal(metadata={"position_multiplier": 1.3})) == 139
    assert resolver_calls == [("resolver", "sepa_trend")]
    assert [name for name, *_ in calls] == ["calendar", "volatility", "conviction"]


def test_minimum_value_floor_and_three_share_correction_are_actual_wrapper_contracts(external_factors):
    calls, values = external_factors
    values["conviction"] = 1.0
    floor = _manager(config=_config(min_value=500000))
    floor_event = _signal(metadata={"position_multiplier": 0.1})
    hybrid = HybridConfig(enabled=True)
    three = _manager(config=_config(core_pct=0.0, hybrid=hybrid, min_value=100000))

    assert floor._calculate_position_size(floor_event) == 50
    assert three._calculate_position_size(_signal(strategy=StrategyType.MEAN_REVERSION, price=Decimal("100000"))) == 3
    assert [name for name, *_ in calls] == ["calendar", "volatility", "conviction"] * 2


def test_market_order_affordability_uses_130_percent_and_strategy_cap_counts_held_plus_pending(external_factors):
    affordable = _manager(config=_config(core_pct=0.0), available=Decimal("1300000"))
    assert affordable._calculate_position_size(_signal()) == 100

    held = Position("000660", quantity=100, avg_price=PRICE, current_price=PRICE, strategy="sepa_trend")
    capped = _manager(config=_config(sepa_cap=30.0), portfolio=_portfolio(cash=Decimal("9000000"), positions=(held,)),
                      available=EQUITY)
    capped._reserved_by_order = {"PENDING": Decimal("1000000")}
    capped._pending_strategy = {"PENDING": "sepa_trend"}
    assert capped._calculate_position_size(_signal()) == 100


@pytest.mark.parametrize("case", ["zero_price", "disabled", "no_cash", "risk_no_resolver", "strategy_exhausted"])
def test_early_returns_do_not_call_resolver_or_overlay_factors(external_factors, case):
    calls, _ = external_factors
    resolver_calls = []
    manager = _manager(config=_config(mode="risk"), stop=_stop_recorder(resolver_calls))
    event = _signal()
    if case == "zero_price":
        event.price = Decimal("0")
    elif case == "disabled":
        event.strategy = StrategyType.MOMENTUM_BREAKOUT
    elif case == "no_cash":
        manager.engine.get_available_cash = lambda: Decimal("0")
    elif case == "risk_no_resolver":
        del manager._resolve_entry_stop
    elif case == "strategy_exhausted":
        manager.config.strategy_allocation["sepa_trend"] = 10.0
        position = Position("000660", quantity=100, avg_price=PRICE, current_price=PRICE, strategy="sepa_trend")
        manager.engine.portfolio = _portfolio(cash=Decimal("9000000"), positions=(position,))

    assert manager._calculate_position_size(event) == 0
    assert resolver_calls == ([] if case != "strategy_exhausted" else [("resolver", "sepa_trend")])
    assert calls == []


def test_factor_budget_is_first_bucket_held_only_and_fail_open_without_sizing_calls(external_factors):
    held = Position("000660", quantity=100, avg_price=PRICE, current_price=PRICE, strategy="sepa_trend")
    manager = _manager(
        config=_config(core_pct=0.0), portfolio=_portfolio(cash=Decimal("9000000"), positions=(held,)),
    )
    manager.config.factor_budgets = {
        "enforce": True,
        "buckets": {
            "first": {"strategies": ["sepa_trend"], "max_pct": 10.0},
            "later": {"strategies": ["sepa_trend"], "max_pct": 5.0},
        },
    }
    manager._reserved_by_order = {"PENDING": Decimal("9000000")}
    manager._pending_strategy = {"PENDING": "sepa_trend"}
    direct = manager._check_factor_budget("sepa_trend")
    manager.config.factor_budgets["buckets"]["first"]["max_pct"] = 20.0
    first_only = manager._check_factor_budget("sepa_trend")
    manager.config.factor_budgets["buckets"]["first"]["max_pct"] = 10.0
    manager.config.factor_budgets["enforce"] = False
    shadow = manager._check_factor_budget("sepa_trend")
    manager.config.factor_budgets = {"buckets": {"broken": {"strategies": object(), "max_pct": 1}}}

    assert "팩터 'first'" in direct
    assert first_only is None
    assert "팩터 'first'" in shadow
    assert manager._check_factor_budget("sepa_trend") is None
    manager._reserved_by_order = {}
    manager._pending_strategy = {}
    manager._check_factor_budget = lambda _strategy: pytest.fail("sizing must not call factor budget")
    assert manager._calculate_position_size(_signal()) == 250
