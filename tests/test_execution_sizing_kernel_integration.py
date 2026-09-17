"""Actual sizing wrapper must consume the pure phases without changing I/O order."""
from decimal import InvalidOperation
from decimal import Decimal
from types import SimpleNamespace

import pytest

from test_execution_sizing_characterization import (
    _manager, _config, _signal, _stop_recorder, external_factors,
)


@pytest.mark.parametrize('mode,expected', [('nominal', 175), ('risk', 139)])
def test_actual_wrapper_uses_kernel_initial_and_final_phases(monkeypatch, external_factors, mode, expected):
    from src.utils import position_sizing_kernel as kernel
    trace = []
    for name in ('nominal_initial', 'override_risk_initial', 'pre_fee_quantity', 'apply_fee_risk_cap'):
        original = getattr(kernel, name)

        def spy(*args, _name=name, _original=original, **kwargs):
            trace.append(_name)
            return _original(*args, **kwargs)

        monkeypatch.setattr(kernel, name, spy)
    manager = _manager(config=_config(mode=mode), stop=_stop_recorder([]))
    assert manager._calculate_position_size(_signal()) == expected
    assert trace == (['nominal_initial', 'override_risk_initial', 'pre_fee_quantity', 'apply_fee_risk_cap']
                     if mode == 'risk' else ['nominal_initial', 'pre_fee_quantity'])


@pytest.mark.parametrize('broken,expected', [('calendar', 105), ('volatility', 231), ('conviction', 96)])
def test_each_bad_overlay_keeps_prior_amount_and_still_calls_later_factors(external_factors, broken, expected):
    calls, values = external_factors
    values.update(calendar=1.1, volatility=0.5, conviction=1.2)
    values[broken] = 'invalid-multiplier'
    manager = _manager()
    event = _signal()
    assert manager._calculate_position_size(event) == expected
    assert [name for name, *_ in calls] == ['calendar', 'volatility', 'conviction']
    assert 'entry_risk' not in event.metadata
    assert 'entry_risk' not in event.signal.metadata


def test_unguarded_position_overlay_error_still_precedes_external_factors(external_factors):
    calls, _ = external_factors
    manager = _manager()
    with pytest.raises(InvalidOperation):
        manager._calculate_position_size(_signal(metadata={'position_multiplier': 'invalid'}))
    assert calls == []


def test_nominal_wrapper_does_not_require_unused_risk_configuration(external_factors):
    manager = _manager()
    manager.config = SimpleNamespace(**{
        key: value for key, value in vars(manager.config).items()
        if key not in {'sizing_mode', 'risk_per_trade_pct', 'risk_max_position_pct'}
    })
    assert manager._calculate_position_size(_signal()) == 175


@pytest.mark.parametrize('minimum,price', [(2000000, 10000), (0, 5000000)])
def test_risk_early_zero_does_not_read_fee_settings(monkeypatch, external_factors, minimum, price):
    """Independent review F1: pre-fee rejections must stay dependency-free."""
    def unavailable(*_args, **_kwargs):
        pytest.fail('fee settings must not be read for an already rejected size')

    monkeypatch.setattr('src.utils.fee_calculator.get_fee_calculator', unavailable)
    manager = _manager(config=_config(mode='risk'), stop=_stop_recorder([]))
    manager.config.min_position_value = minimum
    event = _signal()
    event.price = Decimal(price)
    assert manager._calculate_position_size(event) == 0
    assert 'entry_risk' not in event.metadata
    assert 'entry_risk' not in event.signal.metadata
