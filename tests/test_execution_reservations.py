"""잔여 예약 공통 판정. None 계획위험은 측정0과 구분한다."""
from copy import deepcopy

import pytest


def predicate(row):
    from src.execution.safety.reservations import has_remaining_reservation
    return has_remaining_reservation(row)


@pytest.mark.parametrize('extra, expected', [
    ({}, False),
    ({'reserved_exposure': '0', 'reserved_planned_risk': None}, False),
    ({'reserved_exposure': '0', 'reserved_planned_risk': '0'}, False),
    ({'reserved_exposure': '1', 'reserved_planned_risk': None}, True),
    ({'reserved_exposure': '0', 'reserved_planned_risk': '0.01'}, True),
    ({'reserved_cash': '1'}, True), ({'reserved_quantity': 1}, True),
])
def test_remaining_resources_and_legacy_compatibility(extra, expected):
    row = {'reserved_quantity': 0, 'reserved_cash': '0', **extra}
    before = deepcopy(row)
    assert predicate(row) is expected
    assert row == before


@pytest.mark.parametrize('extra', [
    {'reserved_quantity': True}, {'reserved_quantity': -1}, {'reserved_quantity': '0'},
    {'reserved_cash': None}, {'reserved_cash': 0}, {'reserved_cash': 'NaN'},
    {'reserved_cash': '-1'}, {'reserved_cash': 'Infinity'},
    {'reserved_exposure': '0'}, {'reserved_planned_risk': None},
    *[{'reserved_exposure': bad, 'reserved_planned_risk': None}
      for bad in [None, True, 0, 'NaN', '-1', 'Infinity', 'garbage']],
    *[{'reserved_exposure': '0', 'reserved_planned_risk': bad}
      for bad in [True, 0, 'NaN', '-1', 'Infinity', 'garbage']],
])
def test_invalid_reservation_is_not_interpreted_as_finished(extra):
    row = {'reserved_quantity': 0, 'reserved_cash': '0', **extra}
    with pytest.raises(ValueError):
        predicate(row)


@pytest.mark.parametrize('row', [{}, {'reserved_quantity': 0}, {'reserved_cash': '0'}, None, []])
def test_missing_base_reservation_is_not_fabricated_as_zero(row):
    with pytest.raises(ValueError):
        predicate(row)
