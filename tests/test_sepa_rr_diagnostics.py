"""R/R 차단 근거는 실제 판정 비용과 일치해야 하며 기존 입장 정책은 유지한다."""
import asyncio
from datetime import datetime
from decimal import Decimal as D
from types import SimpleNamespace

import pytest
from loguru import logger

from src.strategies.kr.sepa_trend import SEPATrendStrategy


def test_gross_two_is_not_cost_adjusted_two():
    strategy = SEPATrendStrategy()
    details = {}
    assert not strategy.check_rr_ratio(D(100000), D(110400), D(94800), diagnostics=details)
    assert details['gross_rr'] == 2
    assert details['gate_rr'] == pytest.approx(1.87451630735)
    assert details['fee_adjusted_rr'] == details['gate_rr']
    assert details['round_trip_cost'] == 227
    assert details['cost_status'] == 'calculated'


@pytest.mark.parametrize('price,target,stop,passed,cost', [
    (1000, 1146, 930, True, 2),
    (10000, 11460, 9300, False, 22),
    (100000, 114600, 93000, False, 227),
    (100000, 114700, 93000, True, 227),
])
def test_actual_fee_rounding_and_threshold_are_preserved(price, target, stop, passed, cost):
    strategy = SEPATrendStrategy()
    details = {}
    assert strategy.check_rr_ratio(D(price), D(target), D(stop), diagnostics=details) is passed
    assert details['round_trip_cost'] == cost
    assert strategy.check_rr_ratio(D(price), D(target), D(stop)) is passed


def test_unavailable_cost_holds_entry_instead_of_assuming_zero(monkeypatch):
    def unavailable(*args):
        raise ValueError('fee model unavailable')
    monkeypatch.setattr('src.utils.fee_calculator.get_fee_calculator', unavailable)
    details = {}
    assert not SEPATrendStrategy().check_rr_ratio(D(100000), D(110400), D(94800), diagnostics=details)
    assert details['gross_rr'] == 2
    assert details['gate_rr'] is None
    assert details['fee_adjusted_rr'] is None
    assert details['round_trip_cost'] is None
    assert details['cost_status'] == 'unavailable'


@pytest.mark.parametrize('side', ['buy', 'sell'])
@pytest.mark.parametrize('value', [D('NaN'), D('Infinity'), D('-Infinity'), D('-1'), None])
def test_bad_fee_component_cannot_be_hidden_by_valid_other_side(monkeypatch, side, value):
    # A negative buy fee plus a positive sell fee can have a positive total.
    # Validating only the summed cost would incorrectly admit that result.
    fees = SimpleNamespace(calculate_buy_fee=lambda amount: D(14),
                           calculate_sell_fee=lambda amount: D(213))
    setattr(fees, f'calculate_{side}_fee', lambda amount: value)
    monkeypatch.setattr('src.utils.fee_calculator.get_fee_calculator', lambda *args: fees)
    details = {'gate_rr': 99, 'round_trip_cost': 0, 'cost_status': 'calculated'}
    assert not SEPATrendStrategy().check_rr_ratio(D(100000), D(120000), D(95000), diagnostics=details)
    assert details['gate_rr'] is None
    assert details['fee_adjusted_rr'] is None
    assert details['round_trip_cost'] is None
    assert details['cost_status'] == 'unavailable'


def test_fee_method_failure_without_diagnostics_also_holds(monkeypatch):
    def failed_sell(amount):
        raise RuntimeError('local calculation failed')
    fees = SimpleNamespace(calculate_buy_fee=lambda amount: D(14), calculate_sell_fee=failed_sell)
    monkeypatch.setattr('src.utils.fee_calculator.get_fee_calculator', lambda *args: fees)
    assert not SEPATrendStrategy().check_rr_ratio(D(100000), D(120000), D(95000))


def test_known_zero_cost_remains_distinct_from_unavailable():
    strategy = SEPATrendStrategy()
    strategy.market = 'US'
    details = {}
    assert strategy.check_rr_ratio(D(100000), D(110400), D(94800), diagnostics=details)
    assert details['fee_adjusted_rr'] == 2
    assert details['round_trip_cost'] == 0
    assert details['cost_status'] == 'calculated'


@pytest.mark.parametrize('entry,target,stop,passed', [
    (100, None, 90, True), (100, 120, None, True),
    (0, 120, 90, True), (100, 120, 100, False),
])
def test_not_evaluated_does_not_reuse_previous_diagnostics(entry, target, stop, passed):
    details = {'gate_rr': 99, 'cost_status': 'calculated'}
    assert SEPATrendStrategy().check_rr_ratio(
        D(entry), None if target is None else D(target),
        None if stop is None else D(stop), diagnostics=details,
    ) is passed
    assert details['gate_rr'] is None
    assert details['cost_status'] == 'not_evaluated'


@pytest.mark.parametrize('fee_missing', [False, True])
def test_batch_warning_reports_actual_gate_or_unavailable_cost(monkeypatch, fee_missing):
    import datetime as clock_module
    class Morning(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 28, 10, 0, tzinfo=tz)
    monkeypatch.setattr(clock_module, 'datetime', Morning)
    strategy = SEPATrendStrategy()
    if fee_missing:
        def unavailable(*args):
            raise RuntimeError('fee unavailable')
        monkeypatch.setattr('src.utils.fee_calculator.get_fee_calculator', unavailable)
    monkeypatch.setattr(strategy, '_calculate_sepa_score', lambda row: 80)
    rows = [SimpleNamespace(symbol=symbol, name='fixture', entry_price=D(100000),
                            indicators={'atr_14': atr}, reasons=[])
            for symbol, atr in [('000001', 2.6), ('000002', 4.9)]]
    messages = []
    sink = logger.add(lambda message: messages.append(str(message)), format='{message}')
    try:
        signals = asyncio.run(strategy.generate_batch_signals(rows))
    finally:
        logger.remove(sink)
    assert [signal.symbol for signal in signals] == ([] if fee_missing else ['000002'])
    warning = next(message for message in messages if '✗ 000001' in message)
    assert '비용 전 R/R=2.0000' in warning
    assert ('비용 후 R/R=미확인' if fee_missing else '비용 후 R/R=1.8745') in warning
    assert ('왕복비용=미확인' if fee_missing else '왕복비용=0.2270%') in warning
    if fee_missing:
        assert any('비용 미확인 2개' in message for message in messages)
        assert '비용상태=unavailable' in warning
    assert 'ATR=2.6000%' in warning
    assert '기준=2.00' in warning
