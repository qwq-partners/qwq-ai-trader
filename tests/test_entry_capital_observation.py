"""Synthetic pre-pending capital evidence; no broker/state-file access."""
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.analytics import entry_observation as obs
from src.core.types import Order, OrderSide, StrategyType
from test_risk_sizing import _rm, _em, _sig, STRATEGY_EXIT_PARAMS
from test_entry_risk_lifecycle import _order_path


def setup_capital(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path))
    em = _em()
    rm = _rm(monkeypatch, mode='risk', em=em, cash='4000000')
    rm.engine.portfolio.cash = Decimal('4500000')
    rm.engine.portfolio.get_strategy_allocation = lambda strategy: Decimal('400000')
    rm._get_core_reserve = lambda: Decimal('500000')
    rm._reserved_by_order = {'OTHER': Decimal('300000')}
    rm._pending_strategy_notional = lambda strategy: Decimal('200000')
    buffer = obs.EntryObservationBuffer(evaluation_epoch='synthetic', capacity=20)
    owner = SimpleNamespace(_entry_price_observer=buffer, exit_manager=em,
                            _strategy_exit_params=STRATEGY_EXIT_PARAMS)
    rm._entry_observation_owner = owner
    event = _sig(2.5, strategy=StrategyType.GAP_AND_GO)
    event.source = 'live_screening'
    order = Order(symbol=event.symbol, side=OrderSide.BUY, quantity=50,
                  price=Decimal('10000'), strategy='gap_and_go')
    return rm, owner, event, order, buffer


def freeze(owner, rm, event, order):
    assert hasattr(obs, 'freeze_pre_pending_capital'), 'pre-pending evidence is missing'
    return obs.freeze_pre_pending_capital(owner, rm, event, order)


def test_snapshot_separates_cash_strategy_and_prior_reservations(monkeypatch, tmp_path):
    rm, owner, event, order, buffer = setup_capital(monkeypatch, tmp_path)
    before = deepcopy(rm._reserved_by_order)
    snap = freeze(owner, rm, event, order)
    assert snap['basis'] == 'engine_memory_not_broker_balance'
    assert snap['stage'] == 'before_current_pending_registration'
    assert snap['cash_after_reserve'] == '4000000'
    assert snap['prior_pending_cash_reserved'] == '300000'
    assert snap['core_cash_reserved'] == '500000'
    assert snap['cash_capacity_before'] == '3200000'
    assert Decimal(snap['strategy_cap_notional']) == Decimal('1500000')
    assert Decimal(snap['strategy_remaining_notional']) == Decimal('900000')
    assert snap['current_order_reservation_included'] is False
    assert snap['order_id'] == order.id and snap['signal_id'] == event.id
    assert rm._reserved_by_order == before and buffer.export()['records'] == []
    rm._reserved_by_order[order.symbol] = Decimal('507500')
    rm.engine.portfolio.cash = Decimal('0')
    assert snap['cash'] == '4500000' and snap['prior_pending_cash_reserved'] == '300000'


@pytest.mark.parametrize('mode', ['disabled', 'closed', 'outside_cohort', 'sell'])
def test_inactive_or_out_of_scope_capture_does_not_read_capital(monkeypatch, tmp_path, mode):
    rm, owner, event, order, buffer = setup_capital(monkeypatch, tmp_path)
    calls = []
    def fail():
        calls.append('read')
        raise AssertionError('capital getter must not run')
    rm.engine.get_available_cash = fail
    if mode == 'disabled': owner._entry_price_observer = None
    elif mode == 'closed': buffer._capture_closed = True
    elif mode == 'outside_cohort':
        owner._entry_price_observer = obs.EntryObservationBuffer(
            evaluation_epoch='synthetic', capacity=5, scan_scope='first', scan_admission_ref='synthetic')
    else: order.side = OrderSide.SELL
    assert freeze(owner, rm, event, order) is None
    assert calls == []
    assert buffer.export()['complete'] is True


@pytest.mark.parametrize('defect', ['raises', 'nan', 'missing'])
def test_unavailable_snapshot_does_not_escape_into_order_flow(monkeypatch, tmp_path, defect):
    rm, owner, event, order, buffer = setup_capital(monkeypatch, tmp_path)
    if defect == 'raises':
        rm.engine.get_available_cash = lambda: (_ for _ in ()).throw(RuntimeError('synthetic'))
    elif defect == 'nan': rm.engine.portfolio.cash = Decimal('NaN')
    else: del rm.engine.portfolio.cash
    assert freeze(owner, rm, event, order) is None
    assert buffer.export()['records'] == []


@pytest.mark.parametrize('core', [False, True])
def test_uncapped_strategy_and_core_reserve_have_explicit_meanings(monkeypatch, tmp_path, core):
    rm, owner, event, order, _ = setup_capital(monkeypatch, tmp_path)
    if core:
        event.strategy = StrategyType.CORE_HOLDING
        order.strategy = 'core_holding'
    rm.config.strategy_allocation[order.strategy] = 0
    snap = freeze(owner, rm, event, order)
    assert snap['strategy_cap_notional'] is None
    assert snap['strategy_remaining_notional'] is None
    assert snap['core_cash_reserved'] == ('0' if core else '500000')


def test_negative_remaining_cash_is_recorded_not_invented_as_zero(monkeypatch, tmp_path):
    rm, owner, event, order, _ = setup_capital(monkeypatch, tmp_path)
    rm._reserved_by_order = {'OTHER': Decimal('5000000')}
    snap = freeze(owner, rm, event, order)
    assert snap['cash_capacity_before'] == '-1500000'


@pytest.mark.parametrize('missing_capital', [False, True])
def test_real_order_snapshots_before_own_reservation_without_changing_order(
        monkeypatch, tmp_path, missing_capital):
    fingerprints = []
    snapshots = []
    template = _sig(2.5); template.source = 'live_screening'
    for enabled in (False, True):
        rm, owner, _, _, buffer = setup_capital(monkeypatch, tmp_path)
        if missing_capital: del rm.engine.portfolio.cash
        if not enabled: rm._entry_observation_owner = None
        # Helper resets pending containers; existing reservation remains explicit.
        events, _ = _order_path(monkeypatch, rm, deepcopy(template))
        order = events[0].order
        fingerprints.append((order.symbol, order.quantity, order.price, rm._reserved_by_order,
                             rm._pending_quantities, rm._pending_signal_cache))
        if enabled:
            row = buffer.export()['records'][0]
            assert row['capital_budget'] is None
            snap = row['capital_snapshot']
            if missing_capital:
                assert snap is None and row['capital_snapshot_status'] == 'unavailable'
                continue
            assert snap['prior_pending_cash_reserved'] == '300000'
            assert snap['requested_quantity'] == order.quantity
            assert snap['order_id'] == row['order_id'] == order.id
            assert snap['captured_at'] <= row['observed_at']
            assert rm._reserved_cash > Decimal(snap['prior_pending_cash_reserved'])
            snapshots.append(snap)
    assert fingerprints[0] == fingerprints[1]
    assert len(snapshots) == int(not missing_capital)


@pytest.mark.asyncio
async def test_capital_snapshot_survives_journal_and_does_not_replace_budget(monkeypatch, tmp_path):
    from test_entry_observation_journal import opened, read
    rm, owner, event, order, _ = setup_capital(monkeypatch, tmp_path)
    buffer, journal, path = await opened(tmp_path)
    owner._entry_price_observer = buffer
    snap = freeze(owner, rm, event, order)
    obs.capture_order_ready(owner, event, order, capital_snapshot=snap)
    result = await journal.close()
    assert result['fsync_confirmed']
    row = read(path)['records'][0]
    assert row['capital_snapshot'] == snap and row['capital_budget'] is None
    assert row['capital_snapshot_ref']


@pytest.mark.asyncio
async def test_capital_allowlist_rejects_unrelated_private_data(monkeypatch, tmp_path):
    from test_entry_observation_journal import opened, read
    rm, owner, event, order, _ = setup_capital(monkeypatch, tmp_path)
    buffer, journal, path = await opened(tmp_path)
    owner._entry_price_observer = buffer
    snap = freeze(owner, rm, event, order)
    snap['account_number'] = 'synthetic-sensitive-field'
    obs.capture_order_ready(owner, event, order, capital_snapshot=snap)
    await journal.close()
    loaded = read(path)
    assert not loaded['complete'] and loaded['records'] == []
    assert b'synthetic-sensitive-field' not in path.read_bytes()
