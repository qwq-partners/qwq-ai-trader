"""Neutral pre-LLM evidence must survive mutation and nonlinear policy caps."""
import copy
import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from src.core.engine import RiskManager
from src.core.event import SignalEvent
from src.core.types import RiskConfig, Signal, SignalStrength, StrategyType, OrderSide
from src.utils.stop_policy import StopDecision


@pytest.fixture
def case(monkeypatch):
    for module, function in [('calendar_seasonality', 'calendar_multiplier'),
                             ('volatility_targeting', 'vol_targeting_multiplier'),
                             ('team_conviction', 'team_conviction_multiplier')]:
        monkeypatch.setattr(f'src.utils.{module}.{function}', lambda *a: (1.0, 'fixture'))
    rm = object.__new__(RiskManager)
    rm.config = RiskConfig(sizing_mode='nominal', max_position_pct=28,
                          min_position_value=200000, strategy_allocation={'sepa_trend': 40})
    rm.engine = SimpleNamespace(portfolio=SimpleNamespace(total_equity=Decimal('10000000'),
        effective_daily_pnl=Decimal('0'), get_strategy_allocation=lambda s: Decimal('0')),
        get_available_cash=lambda: Decimal('10000000'))
    rm._reserved_by_order = {}
    rm._pending_strategy = {}
    rm._LLM_REJECT_SIZE_MULT = 0.5
    rm._resolve_entry_stop = lambda s: StopDecision(Decimal('5'), 'strategy', False)
    event = SignalEvent.from_signal(Signal(symbol='TEST', side=OrderSide.BUY,
        strategy=StrategyType.SEPA_TREND, strength=SignalStrength.NORMAL,
        price=Decimal('10000'), metadata={'position_multiplier': 0.8}))
    return rm, event


def api():
    from src.analytics import model_comparison_sizing
    return model_comparison_sizing


def test_neutral_snapshot_is_detached_and_replay_uses_existing_multiplier(case, monkeypatch):
    rm, event = case
    original = copy.deepcopy(event)
    snapshot = api().capture_policy_sizing(rm, event)
    assert event == original
    encoded = json.dumps(snapshot, allow_nan=False)
    rm.engine.portfolio.total_equity = Decimal('1')
    rm._reserved_by_order['changed'] = Decimal('99999999')
    event.signal.metadata['position_multiplier'] = 0.1
    def forbidden(*a):
        raise AssertionError('replay read live overlay')
    monkeypatch.setattr('src.utils.volatility_targeting.vol_targeting_multiplier', forbidden)
    approve = api().replay_policy_sizing(snapshot, 'approve')
    reject = api().replay_policy_sizing(snapshot, 'reject')
    unavailable = api().replay_policy_sizing(snapshot, 'unavailable')
    assert (approve['quantity'], reject['quantity'], unavailable['quantity']) == (200, 100, 200)
    assert (approve['multiplier'], reject['multiplier']) == (0.8, 0.4)
    assert approve['snapshot_id'] == reject['snapshot_id'] == snapshot['snapshot_id']
    assert approve['replay_id'] != reject['replay_id']
    assert approve['admissibility_scope'] == 'sizing_only'
    assert not approve['executable'] and not approve['economic_evaluable']
    assert json.dumps(snapshot, allow_nan=False) == encoded


def test_minimum_floor_is_replayed_not_quantity_scaled(case):
    rm, event = case
    rm.engine.portfolio.total_equity = Decimal('1000000')
    event.signal.metadata = None
    snap = api().capture_policy_sizing(rm, event)
    approved = api().replay_policy_sizing(snap, 'approve')
    rejected = api().replay_policy_sizing(snap, 'reject')
    assert approved['quantity'] == 25
    assert rejected['quantity'] == 20
    assert rejected['trace']['min_clamped'] is True


def test_risk_atr_exception_and_fee_cap_use_actual_policy(case):
    rm, event = case
    rm.config.sizing_mode = 'risk'
    event.signal.metadata = {'position_multiplier': 0.825, 'atr_pct': 4}
    snap = api().capture_policy_sizing(rm, event)
    approve = api().replay_policy_sizing(snap, 'approve')
    reject = api().replay_policy_sizing(snap, 'reject')
    assert approve['quantity'] == 139  # ATR-only multiplier suppressed, buy fee final cap.
    assert reject['quantity'] == 57    # Existing ATR times soft rejection retained.
    assert approve['trace']['position_multiplier'] == 1.0
    assert reject['trace']['position_multiplier'] == 0.4125


@pytest.mark.parametrize('snapshot', [None, {}, {'schema_version':'wrong'}])
def test_missing_snapshot_refuses_replay(snapshot):
    with pytest.raises(ValueError):
        api().replay_policy_sizing(snapshot, 'approve')


def test_changed_snapshot_refuses_replay(case):
    snapshot = api().capture_policy_sizing(*case)
    snapshot['state']['available_cash'] = '99999999'
    with pytest.raises(ValueError):
        api().replay_policy_sizing(snapshot, 'approve')


def test_record_receives_detached_pre_await_snapshot(case, monkeypatch, tmp_path):
    import asyncio
    from test_llm_decision_record import _LLM, _cross
    from src.analytics.llm_decision_record import DecisionRecorder, set_decision_recorder
    snapshot = api().capture_policy_sizing(*case)
    original = copy.deepcopy(snapshot)
    class MutatingLLM(_LLM):
        async def complete_with(self, *a, **kw):
            snapshot['signal']['nested_multiplier'] = 0.01
            return await super().complete_with(*a, **kw)
    cross = _cross(monkeypatch, MutatingLLM('APPROVE', 'ACCEPT'))
    recorder = DecisionRecorder(tmp_path / 'decisions', enabled=True)
    set_decision_recorder(recorder)
    try:
        assert asyncio.run(cross.llm_second_check('TEST', 'sepa_trend', 90, {}, 'sideways',
            signal_id=case[1].id, neutral_sizing=snapshot)) is True
        assert recorder.flush()
        row = json.loads(next((tmp_path / 'decisions').glob('*.jsonl')).read_text())
        assert row['inputs']['neutral_sizing'] == original
    finally:
        recorder.close()
        set_decision_recorder(None)


def test_capture_disabled_never_reads_portfolio_or_overlays(case, monkeypatch, tmp_path):
    from src.analytics.llm_decision_record import DecisionRecorder, set_decision_recorder
    set_decision_recorder(DecisionRecorder(tmp_path, enabled=False))
    try:
        case[0].engine = None
        assert api().capture_policy_sizing_if_enabled(*case) is None
    finally:
        set_decision_recorder(None)


@pytest.mark.parametrize('capture_fails', [False, True])
def test_engine_freezes_before_await_and_capture_failure_keeps_rejection(case, monkeypatch, tmp_path, capture_fails):
    import asyncio
    from datetime import datetime
    from test_engine_stale_pending_fixes import _rm
    from test_llm_decision_record import _LLM, _cross
    from src.analytics.llm_decision_record import DecisionRecorder, set_decision_recorder
    manager, event = case
    rm = _rm(monkeypatch, None, datetime(2026, 10, 13, 10, 0))
    rm.config = manager.config
    rm.engine.portfolio = manager.engine.portfolio
    rm.engine.portfolio.positions = {}
    rm.engine.get_available_cash = manager.engine.get_available_cash
    rm.engine.is_trading_hours = lambda: True
    rm._LLM_CHECK_MIN, rm._LLM_BYPASS_AT, rm._LLM_REJECT_SIZE_MULT = 85, 95, 0.5
    rm._resolve_market_regime = lambda: 'sideways'
    rm._log_sig = lambda *a, **kw: None
    rm._last_cash_warn_time = None
    event.score = 90
    event.signal.reasons = ['fixture reason', 'fixture confirmation']
    class UpdatingLLM(_LLM):
        async def complete_with(self, *a, **kw):
            # Simulate a concurrent cash update while LLM requests are in flight.
            rm.engine.get_available_cash = lambda: Decimal('0')
            return await super().complete_with(*a, **kw)
    cross = _cross(monkeypatch, UpdatingLLM('REJECT', 'REJECT'))
    cross.validate = lambda **kw: (True, kw['score'], '')
    rm._cross_validator = cross
    if capture_fails:
        def fail(*a):
            raise ValueError('bad capture')
        monkeypatch.setattr(api(), 'capture_policy_sizing', fail)
    recorder = DecisionRecorder(tmp_path / 'records', enabled=True)
    set_decision_recorder(recorder)
    try:
        assert asyncio.run(rm.on_signal(event)) is None  # Cash gate stops order creation.
        assert event.signal.metadata['position_multiplier'] == 0.4
        assert event.metadata['llm_second_check'] == 'rejected_soft'
        assert recorder.flush()
        row = json.loads(next((tmp_path / 'records').glob('*.jsonl')).read_text())
        assert row['result']['approved'] is False
        snap = row['inputs']['neutral_sizing']
        if capture_fails:
            assert snap['capture_status'] == 'unavailable'
            with pytest.raises(ValueError):
                api().replay_policy_sizing(snap, 'reject')
        else:
            assert snap['state']['available_cash'] == '10000000'
            assert snap['signal']['nested_multiplier'] == 0.8
            assert api().replay_policy_sizing(snap, 'reject')['quantity'] == 100
    finally:
        recorder.close()
        set_decision_recorder(None)


@pytest.mark.parametrize('cash,reserved,core,strategy_used,price,want', [
    ('100000', '0', '0', '0', '10000', 0),  # Cannot reach minimum investment.
    ('2000000', '1600000', '0', '0', '100000', 3),  # Minimum-three permitted.
    ('2000000', '1800000', '0', '0', '100000', 1),  # Market cash cap cannot support three.
    ('10000000', '0', '0', '3750000', '100000', 2),  # Strategy budget caps min-three.
    ('10000000', '0', '0', '4000000', '10000', 0),  # Fully exhausted strategy budget.
    ('10000000', '0', '10000000', '0', '10000', 0),  # No non-core pool.
])
def test_cash_strategy_core_and_min_three_caps(case, cash, reserved, core, strategy_used, price, want):
    rm, event = case
    rm.engine.get_available_cash = lambda: Decimal(cash)
    rm._reserved_by_order = {'pending': Decimal(reserved)}
    rm.engine.portfolio.get_strategy_allocation = lambda s: Decimal(core if s == 'core_holding' else strategy_used)
    event.price = Decimal(price)
    event.signal.metadata = {}
    assert api().replay_policy_sizing(api().capture_policy_sizing(rm, event), 'approve')['quantity'] == want


def test_missing_nested_signal_preserves_live_event_only_multiplier_behavior(case):
    rm, event = case
    event.signal = None
    event.metadata = {'position_multiplier': 0.2}
    snap = api().capture_policy_sizing(rm, event)
    assert api().replay_policy_sizing(snap, 'approve')['quantity'] == 250
    assert api().replay_policy_sizing(snap, 'reject')['quantity'] == 250


def test_overlay_inputs_captured_once_and_fee_rate_frozen(case, monkeypatch):
    from src.utils.fee_calculator import get_fee_calculator
    rm, event = case
    rm.config.sizing_mode = 'risk'
    event.signal.metadata = {}
    calls = []
    def overlay(*a):
        calls.append(a)
        return 1.0, 'fixture'
    monkeypatch.setattr('src.utils.volatility_targeting.vol_targeting_multiplier', overlay)
    snapshot = api().capture_policy_sizing(rm, event)
    monkeypatch.setattr(get_fee_calculator('KR').config, 'buy_commission_rate', Decimal('0.5'))
    assert api().replay_policy_sizing(snapshot, 'approve')['quantity'] == 139
    assert api().replay_policy_sizing(snapshot, 'unavailable')['quantity'] == 139
    assert len(calls) == 1


def test_policy_source_change_refuses_replay(case, monkeypatch):
    snapshot = api().capture_policy_sizing(*case)
    monkeypatch.setattr(api(), 'policy_source_hash', lambda: 'changed')
    with pytest.raises(ValueError, match='source changed'):
        api().replay_policy_sizing(snapshot, 'approve')


def test_capture_cannot_warm_live_overlay_cache(case, monkeypatch, tmp_path):
    from src.utils import volatility_targeting as vt
    # Exercise the actual loader and multiplier, with a synthetic file.
    import importlib.util
    spec = importlib.util.spec_from_file_location('isolated_vol', vt.__file__)
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    fixture_file = tmp_path / 'vol.json'
    from datetime import date
    fixture_file.write_text(json.dumps({'date': date.today().isoformat(), 'realized_vol_pct': 50}))
    monkeypatch.setattr(fresh, '_CACHE_FILE', fixture_file)
    monkeypatch.setenv('VOL_TARGETING', '1')
    monkeypatch.setattr(vt, 'vol_targeting_multiplier', fresh.vol_targeting_multiplier)
    # Clone the function's defining globals, even when a wrapper/test replaces it.
    api().capture_policy_sizing(*case)
    assert fresh._mem_cache == {}


def test_unavailable_verdict_still_persists_neutral_evidence(case, tmp_path):
    import asyncio
    from src.core.cross_validator import CrossStrategyValidator
    from src.analytics.llm_decision_record import DecisionRecorder, set_decision_recorder
    snapshot = api().capture_policy_sizing(*case)
    recorder = DecisionRecorder(tmp_path / 'unavailable', enabled=True)
    set_decision_recorder(recorder)
    try:
        cross = CrossStrategyValidator()
        assert asyncio.run(cross.llm_second_check('TEST', 'sepa_trend', 90, {}, 'sideways',
            signal_id=case[1].id, neutral_sizing=snapshot)) is True
        assert recorder.flush()
        files = list((tmp_path / 'unavailable').glob('*.jsonl'))
        assert files, 'unavailable decision silently lost'
        row = json.loads(files[0].read_text())
        assert row['inputs']['neutral_sizing'] == snapshot
        assert row['result']['failed'] is True
        assert row['stage'] == 'unavailable'
    finally:
        recorder.close()
        set_decision_recorder(None)


@pytest.mark.parametrize('path,value', [('sizing_mode','invalid'), ('max_position_pct', -5),
                                        ('risk_per_trade_pct', True)])
def test_invalid_config_even_with_valid_hash_refuses_replay(case, path, value):
    snapshot = api().capture_policy_sizing(*case)
    snapshot['config'][path] = value
    snapshot['config_sha256'] = api()._digest(snapshot['config'])
    snapshot.pop('snapshot_id')
    snapshot['snapshot_id'] = api()._digest(snapshot)
    with pytest.raises(ValueError):
        api().replay_policy_sizing(snapshot, 'approve')


def test_hybrid_and_daily_loss_inputs_are_frozen(case):
    rm, event = case
    rm.config.hybrid.enabled = True
    rm.config.hybrid.swing_pct = 50
    rm.config.hybrid.swing_base_position_pct = 20
    rm.config.hybrid.swing_max_position_pct = 30
    rm.engine.portfolio.effective_daily_pnl = Decimal('-300000')
    event.signal.metadata = {}
    snapshot = api().capture_policy_sizing(rm, event)
    rm.config.hybrid.swing_pct = 1
    rm.engine.portfolio.effective_daily_pnl = Decimal('0')
    approved = api().replay_policy_sizing(snapshot, 'approve')
    rejected = api().replay_policy_sizing(snapshot, 'reject')
    assert approved['quantity'] == 50
    assert rejected['quantity'] == 25
    assert approved['trace']['daily_loss_half_applied'] is True


@pytest.mark.parametrize('status,want', [('missing','entry_stop_resolver_missing'),
                                        ('failed','entry_stop_resolve_failed')])
def test_unresolved_risk_stop_never_falls_back_to_nominal(case, status, want):
    rm, event = case
    rm.config.sizing_mode = 'risk'
    if status == 'missing':
        rm._resolve_entry_stop = None
    else:
        def fail(_):
            raise ValueError('no stop')
        rm._resolve_entry_stop = fail
    snapshot = api().capture_policy_sizing(rm, event)
    for verdict in ('approve','reject','unavailable'):
        result = api().replay_policy_sizing(snapshot, verdict)
        assert result['quantity'] == 0 and result['trace']['stop'] == want


def test_shared_event_signal_metadata_replays_existing_double_reduction(case):
    rm, event = case
    event.metadata = event.signal.metadata
    snapshot = api().capture_policy_sizing(rm, event)
    # Existing engine writes both metadata containers; aliases observe both writes.
    assert api().replay_policy_sizing(snapshot, 'approve')['quantity'] == 200
    assert api().replay_policy_sizing(snapshot, 'reject')['quantity'] == 50


def test_absent_event_and_nested_metadata_use_live_defaults(case):
    rm, event = case
    event.metadata = None
    event.signal.metadata = None
    snapshot = api().capture_policy_sizing(rm, event)
    assert event.metadata is None and event.signal.metadata is None
    assert api().replay_policy_sizing(snapshot, 'approve')['quantity'] == 250
    assert api().replay_policy_sizing(snapshot, 'reject')['quantity'] == 125
