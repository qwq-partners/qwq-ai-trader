"""Fee failures must not partially apply an actual KR SELL fill."""
import asyncio
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from src.analytics import position_ledger
from src.core.event import FillEvent
from src.core.types import Fill, OrderSide
from src.utils import fee_calculator
from test_fill_reconciliation import SYM, case, process
from test_journal_commit_handoff import identified, setup
from test_sync_portfolio_characterization import _fill_check_once


def recording_case(monkeypatch, tmp_path):
    _, bot = case(monkeypatch, tmp_path)
    saves, sales = [], []
    monkeypatch.setattr(bot.engine, '_save_daily_stats', lambda: saves.append(True))
    monkeypatch.setattr(position_ledger, 'get_position_ledger', lambda market:
                        SimpleNamespace(on_sell=lambda *a, **k: sales.append((a, k))))
    bot.engine.portfolio.daily_pnl = D(50)
    bot.engine.portfolio.daily_trades = 7
    return bot.engine, saves, sales


def sell(qty=3):
    # Missing position metadata must also stay untouched on a fee failure.
    return Fill(order_id='synthetic-sell', symbol=SYM, side=OrderSide.SELL,
                quantity=qty, price=D(11000), strategy='sepa_trend',
                timestamp=datetime(2026, 10, 28, 10))


@pytest.mark.parametrize('side', ['buy', 'sell'])
@pytest.mark.parametrize('bad', [D('NaN'), D('Infinity'), D('-1'), None, 0])
def test_invalid_sell_cost_preserves_all_accounting_and_metadata(monkeypatch, tmp_path, side, bad):
    engine, saves, sales = recording_case(monkeypatch, tmp_path)
    before = asdict(engine.portfolio)
    calc = fee_calculator.FeeCalculator()
    monkeypatch.setattr(calc, f'calculate_{side}_fee', lambda amount: bad)
    monkeypatch.setattr(fee_calculator, 'get_fee_calculator', lambda: calc)

    assert engine.update_position(sell()) is False
    assert asdict(engine.portfolio) == before
    assert not saves and not sales


@pytest.mark.parametrize('failure', ['import', 'calculator', 'buy', 'sell'])
def test_sell_fee_exception_does_not_assume_zero_or_partially_apply(monkeypatch, tmp_path, failure):
    engine, saves, sales = recording_case(monkeypatch, tmp_path)
    before = asdict(engine.portfolio)

    def unavailable(*args):
        raise (ImportError if failure == 'import' else ValueError)('synthetic fee failure')

    calc = fee_calculator.FeeCalculator()
    if failure in ('import', 'calculator'):
        monkeypatch.setattr(fee_calculator, 'get_fee_calculator', unavailable)
    else:
        monkeypatch.setattr(calc, f'calculate_{failure}_fee', unavailable)
        monkeypatch.setattr(fee_calculator, 'get_fee_calculator', lambda: calc)

    assert engine.update_position(sell()) is False
    assert asdict(engine.portfolio) == before
    assert not saves and not sales


@pytest.mark.parametrize('qty,zero,cash,pnl,sell_fee', [
    (3, False, '132930', '2976', '70'),
    (10, False, '209766', '9802', '234'),
    (3, True, '133000', '3050', '0'),
    (10, True, '210000', '10050', '0'),
])
def test_known_sell_cost_preserves_partial_full_and_zero_results(
        monkeypatch, tmp_path, qty, zero, cash, pnl, sell_fee):
    engine, saves, sales = recording_case(monkeypatch, tmp_path)
    if zero:
        calc = fee_calculator.FeeCalculator(fee_calculator.US_FEE_CONFIG)
        monkeypatch.setattr(fee_calculator, 'get_fee_calculator', lambda: calc)
    fill = sell(qty)

    assert engine.update_position(fill) is True
    assert engine.portfolio.cash == D(cash)
    assert engine.portfolio.daily_pnl == D(pnl)
    assert engine.portfolio.daily_trades == 7
    if qty == 10:
        assert SYM not in engine.portfolio.positions
    else:
        pos = engine.portfolio.positions[SYM]
        assert (pos.quantity, pos.avg_price) == (7, D(10000))
        assert (pos.strategy, pos.entry_time) == (fill.strategy, fill.timestamp)
    assert saves == [True]
    assert len(sales) == 1
    assert sales[0][0] == (SYM, qty, D(11000))
    assert sales[0][1]['fee'] == D(sell_fee)


@pytest.mark.parametrize('qty', [3, 10])
@pytest.mark.parametrize('failure', ['import', 'invalid_buy'])
def test_cost_failure_retains_raw_fill_and_blocks_success_handoff_even_after_recovery(
        monkeypatch, tmp_path, qty, failure):
    sched, bot, receipts, acks, failures = setup(monkeypatch, tmp_path, holdings=10)
    before = asdict(bot.engine.portfolio)
    exit_before = asdict(bot.exit_manager.get_state(SYM))
    fill = identified(OrderSide.SELL, qty)
    original_fill = asdict(fill)
    bot.broker.fills_seq = [[fill]]

    with monkeypatch.context() as fee_patch:
        def unavailable(*args):
            raise ImportError('synthetic missing calculator')
        calc = fee_calculator.FeeCalculator()
        fee_patch.setattr(calc, 'calculate_buy_fee', lambda amount: D(-1))
        fee_patch.setattr(fee_calculator, 'get_fee_calculator',
                          unavailable if failure == 'import' else lambda: calc)
        _fill_check_once(monkeypatch, sched, [])
        handoff, = sched._pending_fill_handoffs.values()
        item = handoff['event']
        asyncio.run(process(bot.engine))

    # Restoring the calculator must not silently retry an actual execution.
    asyncio.run(bot.engine.risk_manager.on_fill(item))
    _fill_check_once(monkeypatch, sched, [])
    asyncio.run(sched._sync_portfolio())
    assert item.portfolio_applied is False
    assert bot.engine._unapplied_fills[item.id] is item
    assert item.fill is fill and handoff['fill'] is fill
    assert asdict(fill) == original_fill
    assert sched._pending_fill_handoffs[item.id] is handoff
    assert not handoff.get('postprocessing_done')
    assert not handoff.get('failed')
    assert not receipts and not acks and not failures
    assert not bot.trade_journal.entries and not bot.trade_journal.exits
    assert asdict(bot.engine.portfolio) == before
    assert asdict(bot.exit_manager.get_state(SYM)) == exit_before
    assert bot.engine.reconciliation_token() is None
    assert sched._reconciliation_token() is None
    # Duplicate execution IDs also cannot revive the failed original event.
    duplicate = FillEvent.from_fill(fill)
    asyncio.run(bot.engine.risk_manager.on_fill(duplicate))
    assert duplicate.duplicate_execution
    assert asdict(bot.engine.portfolio) == before
