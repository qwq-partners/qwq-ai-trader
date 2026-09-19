"""Legacy callers must delegate arithmetic while retaining their old boundaries."""

from datetime import datetime
from decimal import Decimal

from src.core.market_regime import MarketRegimeAdapter
from src.core.types import RiskConfig
from src.risk.manager import RiskManager
import src.core.market_regime as market_regime
import src.risk.manager as risk_manager
import src.utils.regime_transition as transition


class _Clock:
    def __init__(self, *values, events=None):
        self.values = list(values)
        self.calls = 0
        self.events = events

    def now(self):
        if self.events is not None:
            self.events.append("clock")
        value = self.values[self.calls]
        self.calls += 1
        return value


class _Expert:
    def aggregate_regime_score(self):
        return 25

    def bear_consensus(self, **_kwargs):
        return False


class _BrokenExpert:
    def aggregate_regime_score(self):
        raise RuntimeError("baseline-provider-error")


class _Logger:
    def __init__(self):
        self.info_messages = []
        self.debug_messages = []

    def info(self, message):
        self.info_messages.append(message)

    def debug(self, message):
        self.debug_messages.append(message)


def _risk_config():
    return RiskConfig(
        max_position_pct=30.0,
        max_positions=8,
        daily_max_loss_pct=5.0,
        default_stop_loss_pct=5.0,
        min_cash_reserve_pct=5.0,
        min_position_value=Decimal("10000"),
    )


def test_legacy_callers_delegate_without_changing_vix_provider_clock_or_last_update_boundaries(monkeypatch, tmp_path):
    """Removing delegation or moving the loader/clock calls should break this parity check."""
    calls = []
    original_facts = transition.calculate_mid_regime_facts
    original_mid = transition.plan_mid_regime_transition
    original_expert = transition.plan_expert_transition
    original_sidecar = transition.transition_sidecar_trend
    monkeypatch.setattr(transition, "calculate_mid_regime_facts", lambda *args: (calls.append("facts"), original_facts(*args))[1])
    monkeypatch.setattr(transition, "plan_mid_regime_transition", lambda *args: (calls.append("mid"), original_mid(*args))[1])
    monkeypatch.setattr(transition, "plan_expert_transition", lambda *args, **kwargs: (calls.append("expert"), original_expert(*args, **kwargs))[1])
    monkeypatch.setattr(transition, "transition_sidecar_trend", lambda *args: (calls.append("sidecar"), original_sidecar(*args))[1])

    adapter = MarketRegimeAdapter()
    clock = _Clock(
        datetime(2026, 9, 18, 10, 0), datetime(2026, 9, 18, 10, 0, 1), datetime(2026, 9, 18, 10, 0, 2),
        events=calls,
    )
    monkeypatch.setattr(market_regime, "datetime", clock)
    monkeypatch.setattr(adapter, "_load_vix_cache_or_refresh", lambda: calls.append("vix-loader"))
    adapter._vix_state = "complacency"
    adapter._vix_value = 14.0
    bull = {"change_pct": 1.1, "open": 100.0, "price": 100.4}
    adapter.update_regime(bull, bull)
    assert calls == ["vix-loader", "facts", "clock", "mid", "clock", "clock"]
    assert clock.calls == 3
    assert adapter._current_regime == "neutral"
    assert adapter._pending_regime == "bull"
    assert adapter._pending_since == datetime(2026, 9, 18, 10, 0, 1)
    assert adapter._last_update == datetime(2026, 9, 18, 10, 0, 2)

    before_last_update = adapter._last_update
    expert_clock = _Clock(datetime(2026, 9, 18, 10, 1))
    monkeypatch.setattr(market_regime, "datetime", expert_clock)
    adapter.apply_expert_adjustment(_Expert())
    assert calls[-1] == "expert"
    assert adapter._last_update == before_last_update
    assert adapter._expert_pending == "bull"
    assert adapter._expert_pending_since == datetime(2026, 9, 18, 10, 1)
    assert expert_clock.calls == 1

    monkeypatch.setattr(risk_manager.Path, "home", lambda: tmp_path)
    manager = RiskManager(_risk_config(), Decimal("100000"))
    sidecar_clock = _Clock(datetime(2026, 9, 18, 10, 2))
    monkeypatch.setattr(risk_manager, "datetime", sidecar_clock)
    manager._sidecar_active = True
    manager.update_market_trend(
        {"price": 98.0, "open": 100.0, "high": 102.0, "low": 94.0, "change_pct": -1.0},
        {},
    )
    assert calls[-1] == "sidecar"
    assert sidecar_clock.calls == 1
    assert manager._sidecar_active is False
    assert manager._market_trend["recovering"] is True


def test_legacy_expert_provider_error_keeps_state_and_logs_before_any_transition(monkeypatch):
    """An expert-provider failure remains a caller-side no-op, not a pure default."""
    adapter = MarketRegimeAdapter()
    adapter._current_regime = "bull"
    adapter._last_update = datetime(2026, 9, 18, 10, 0)
    logger = _Logger()
    monkeypatch.setattr(market_regime, "logger", logger)
    monkeypatch.setattr(
        transition,
        "plan_expert_transition",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("pure helper must not run")),
    )

    adapter.apply_expert_adjustment(_BrokenExpert())

    assert adapter._current_regime == "bull"
    assert adapter._last_update == datetime(2026, 9, 18, 10, 0)
    assert logger.info_messages == []
    assert logger.debug_messages == ["[시장체제] 전문가 보정 실패: baseline-provider-error"]


def test_legacy_open_neutral_keeps_pending_attributes_present_or_absent(monkeypatch):
    """The 09:00–10:00 branch changes only the current regime in the baseline method."""
    index = {"change_pct": 2.0, "open": 100.0, "price": 110.0}
    pending_at = datetime(2026, 9, 18, 8, 59)

    for pending in ("bull", "bear"):
        adapter = MarketRegimeAdapter()
        adapter._current_regime = "sideways"
        adapter._pending_regime = pending
        adapter._pending_since = pending_at
        monkeypatch.setattr(adapter, "_load_vix_cache_or_refresh", lambda: None)
        monkeypatch.setattr(
            market_regime,
            "datetime",
            _Clock(datetime(2026, 9, 18, 9, 30), datetime(2026, 9, 18, 9, 30, 1)),
        )

        adapter.update_regime(index, index)

        assert adapter._current_regime == "neutral"
        assert adapter._pending_regime == pending
        assert adapter._pending_since == pending_at

    absent = MarketRegimeAdapter()
    monkeypatch.setattr(absent, "_load_vix_cache_or_refresh", lambda: None)
    monkeypatch.setattr(
        market_regime,
        "datetime",
        _Clock(datetime(2026, 9, 18, 9, 30), datetime(2026, 9, 18, 9, 30, 1)),
    )
    absent.update_regime(index, index)
    assert absent._current_regime == "neutral"
    assert not hasattr(absent, "_pending_regime")
    assert not hasattr(absent, "_pending_since")
