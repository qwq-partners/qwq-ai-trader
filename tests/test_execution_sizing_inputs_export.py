"""S2-2: 사이징이 실제로 쓴 입력의 반출과 overlay fail-open 표식.

기존 특성화 시험(`test_execution_sizing_characterization.py`)이 못 박은 수량·provider
호출 순서를 그대로 두고 같은 표본에서 `_last_sizing_inputs` 만 검사한다. 반출은 기록일
뿐이며 주문 송신·운영 승격 근거가 아니다. 시계는 특성화 fixture 의 `_FixedDate` 주입을
그대로 쓴다(벽시계 의존 0).
"""
from decimal import Decimal
from pathlib import Path

import pytest

from src.core.types import HybridConfig, StrategyType
from src.utils.sizing import atr_position_multiplier

from test_execution_sizing_characterization import (  # noqa: F401 — pytest fixture 재사용
    PRICE, external_factors, _config, _manager, _portfolio, _signal, _stop_recorder,
)


EXPORT_KEYS = {
    'base_pct', 'strategy_allocation_pct', 'min_position_value', 'strength_multiplier',
    'position_multiplier', 'calendar_multiplier', 'volatility_multiplier',
    'conviction_multiplier', 'atr_pct', 'stop_pct', 'stop_source', 'stop_crash_capped',
    'hybrid_enabled', 'overlay_status',
}
PROVIDERS = {
    'calendar': 'src.utils.calendar_seasonality.calendar_multiplier',
    'volatility': 'src.utils.volatility_targeting.vol_targeting_multiplier',
    'conviction': 'src.utils.team_conviction.team_conviction_multiplier',
}


@pytest.fixture(autouse=True)
def synthetic_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)


def _boom(*_args, **_kwargs):
    raise RuntimeError('합성 provider 장애')


# --- R5: 특성화가 못 박은 표본의 반출값 -------------------------------------


def test_r5_nominal_export_matches_characterized_inputs(external_factors):
    manager = _manager()

    assert manager._calculate_position_size(_signal()) == 175

    inputs = manager._last_sizing_inputs
    assert set(inputs) == EXPORT_KEYS
    assert inputs['base_pct'] == 0.25
    assert inputs['strategy_allocation_pct'] == 42.0
    assert inputs['min_position_value'] == Decimal('200000')
    assert inputs['strength_multiplier'] == 1.0
    assert inputs['position_multiplier'] == 1.0
    assert inputs['atr_pct'] is None
    assert (inputs['stop_pct'], inputs['stop_source'], inputs['stop_crash_capped']) == (None, None, None)
    assert inputs['hybrid_enabled'] is False
    assert (inputs['calendar_multiplier'], inputs['volatility_multiplier'],
            inputs['conviction_multiplier']) == (1.0, 1.0, 1.0)
    assert inputs['overlay_status'] == {
        'calendar': 'applied', 'volatility': 'applied', 'conviction': 'applied',
    }


def test_r5_risk_export_keeps_pre_atr_multiplier_and_stop_triple(external_factors):
    resolver_calls = []
    manager = _manager(config=_config(mode='risk'), stop=_stop_recorder(resolver_calls))
    event = _signal(metadata={'atr_pct': 6.0, 'position_multiplier': atr_position_multiplier(6.0)})

    assert manager._calculate_position_size(event) == 139

    inputs = manager._last_sizing_inputs
    assert inputs['position_multiplier'] == atr_position_multiplier(6.0) != 1.0
    assert inputs['atr_pct'] == 6.0
    assert inputs['stop_pct'] == Decimal('5')
    assert inputs['stop_source'] == 'strategy'
    assert inputs['stop_crash_capped'] is False
    assert inputs['base_pct'] == 0.25
    assert inputs['strategy_allocation_pct'] == 42.0
    assert resolver_calls == [('resolver', 'sepa_trend')]


def test_r5_stop_triple_carries_the_resolver_crash_flag(external_factors):
    manager = _manager(config=_config(mode='risk'),
                       stop=_stop_recorder([], pct='4', source='crash_cap', crash=True))

    assert manager._calculate_position_size(_signal()) > 0

    inputs = manager._last_sizing_inputs
    assert (inputs['stop_pct'], inputs['stop_source'], inputs['stop_crash_capped']) == (
        Decimal('4'), 'crash_cap', True)


def test_r5_hybrid_export_uses_pool_base_pct_not_config_default(external_factors):
    manager = _manager(config=_config(hybrid=HybridConfig(enabled=True)))

    assert manager._calculate_position_size(_signal()) == 40

    inputs = manager._last_sizing_inputs
    assert inputs['base_pct'] == 0.20            # swing 풀 값 — config.base_position_pct(25.0) 아님
    assert inputs['hybrid_enabled'] is True


def test_r5_min_position_value_is_the_floor_sizing_actually_used(external_factors):
    manager = _manager(config=_config(min_value=500000))

    assert manager._calculate_position_size(_signal(metadata={'position_multiplier': 0.1})) == 50

    inputs = manager._last_sizing_inputs
    assert inputs['min_position_value'] == Decimal('500000')
    assert inputs['position_multiplier'] == 0.1


def test_r5_strategy_allocation_pct_is_none_without_a_budget_cap(external_factors):
    manager = _manager(config=_config(sepa_cap=0.0))

    assert manager._calculate_position_size(_signal()) == 175
    assert manager._last_sizing_inputs['strategy_allocation_pct'] is None


def test_r5_weak_strength_multiplier_is_the_applied_value(external_factors):
    from src.core.types import SignalStrength

    manager = _manager()

    assert manager._calculate_position_size(_signal(strength=SignalStrength.WEAK)) == 87
    assert manager._last_sizing_inputs['strength_multiplier'] == 0.5


def test_r5_money_fields_are_exported_as_decimal_not_float(external_factors):
    """금액 필드는 Decimal 로 반출한다 — Decimal == float 이 True 라 값 비교로는 못 박히지 않는다."""
    nominal = _manager()

    assert nominal._calculate_position_size(_signal()) == 175

    nominal_inputs = nominal._last_sizing_inputs
    assert isinstance(nominal_inputs['min_position_value'], Decimal)
    assert nominal_inputs['stop_pct'] is None

    risk = _manager(config=_config(mode='risk'), stop=_stop_recorder([]))

    assert risk._calculate_position_size(_signal()) == 139

    risk_inputs = risk._last_sizing_inputs
    assert isinstance(risk_inputs['min_position_value'], Decimal)
    assert isinstance(risk_inputs['stop_pct'], Decimal)


# --- R6: overlay fail-open 표식 ----------------------------------------------


@pytest.mark.parametrize('kind', sorted(PROVIDERS))
def test_r6_provider_failure_is_marked_unavailable_with_unit_multiplier(
        external_factors, monkeypatch, kind):
    healthy = _manager()
    assert healthy._calculate_position_size(_signal()) == 175
    assert healthy._last_sizing_inputs['overlay_status'][kind] == 'applied'

    monkeypatch.setattr(PROVIDERS[kind], _boom)
    failing = _manager()

    # 수량은 provider 가 1.0 을 준 경우와 같다 — 표식만 다르다.
    assert failing._calculate_position_size(_signal()) == 175

    inputs = failing._last_sizing_inputs
    assert inputs['overlay_status'] == {
        name: ('unavailable' if name == kind else 'applied') for name in PROVIDERS
    }
    assert inputs[f'{kind}_multiplier'] == 1.0


@pytest.mark.parametrize('kind', sorted(PROVIDERS))
def test_r6_kernel_rejected_multiplier_is_recorded_only_after_apply_overlay(
        external_factors, kind):
    """provider 는 정상 반환했지만 kernel apply_overlay 가 그 배율을 처리하지 못한 표본.

    문자열 배율은 calendar 에서 Decimal 변환 실패, volatility/conviction 에서 비교 TypeError 를
    낸다(src/utils/position_sizing_kernel.py apply_overlay). 배율이 실제로 적용되지 않았으므로
    기록은 apply_overlay **성공 뒤**에만 남아야 한다 — 호출 앞에서 기록하면 미적용 배율이
    'applied' 로 새어 나간다.
    """
    _calls, values = external_factors
    values[kind] = 'unset'
    manager = _manager()

    # 수량은 배율 1.0 표본과 같다 — 적용이 없었다는 뜻.
    assert manager._calculate_position_size(_signal()) == 175

    inputs = manager._last_sizing_inputs
    assert inputs['overlay_status'] == {
        name: ('unavailable' if name == kind else 'applied') for name in PROVIDERS
    }
    assert inputs[f'{kind}_multiplier'] == 1.0


@pytest.mark.parametrize(('kind', 'expected_quantity'), [
    ('calendar', 20),       # 0 배율 → 최소금액 바닥(200,000원)으로 클램프
    ('volatility', 20),
    ('conviction', 175),    # conviction 은 1.0 초과만 적용 → 수량 불변
])
def test_r6_zero_multiplier_is_applied_not_unavailable(
        external_factors, kind, expected_quantity):
    """배율 0.0 은 provider 가 실제로 준 값이다 — falsy 라는 이유로 'unavailable' 이 아니다."""
    _calls, values = external_factors
    values[kind] = 0.0
    manager = _manager()

    assert manager._calculate_position_size(_signal()) == expected_quantity

    inputs = manager._last_sizing_inputs
    assert inputs[f'{kind}_multiplier'] == 0.0
    assert inputs['overlay_status'] == {name: 'applied' for name in PROVIDERS}


def test_r6_applied_status_carries_the_actual_provider_value(external_factors):
    _calls, values = external_factors
    values['volatility'] = 0.5
    manager = _manager(portfolio=_portfolio(daily_pnl=Decimal('-250000')))

    assert manager._calculate_position_size(_signal()) == 43

    inputs = manager._last_sizing_inputs
    assert inputs['volatility_multiplier'] == 0.5
    assert inputs['overlay_status']['volatility'] == 'applied'


# --- R7: 조기 return 은 직전 값을 남기지 않는다 ------------------------------


@pytest.mark.parametrize('case', ['zero_price', 'disabled', 'no_cash', 'risk_no_resolver',
                                  'strategy_exhausted'])
def test_r7_early_returns_clear_the_export(external_factors, case):
    manager = _manager(config=_config(mode='risk'), stop=_stop_recorder([]))
    assert manager._calculate_position_size(_signal(metadata={'atr_pct': 3.0})) > 0
    assert manager._last_sizing_inputs is not None

    event = _signal()
    if case == 'zero_price':
        event.price = Decimal('0')
    elif case == 'disabled':
        event.strategy = StrategyType.MOMENTUM_BREAKOUT
    elif case == 'no_cash':
        manager.engine.get_available_cash = lambda: Decimal('0')
    elif case == 'risk_no_resolver':
        del manager._resolve_entry_stop
    elif case == 'strategy_exhausted':
        manager.config.strategy_allocation['sepa_trend'] = 10.0
        from src.core.types import Position
        held = Position('000660', quantity=100, avg_price=PRICE, current_price=PRICE,
                        strategy='sepa_trend')
        manager.engine.portfolio = _portfolio(cash=Decimal('9000000'), positions=(held,))

    assert manager._calculate_position_size(event) == 0
    assert manager._last_sizing_inputs is None


def test_r7_post_overlay_rejection_also_clears_the_export(external_factors):
    calls, _ = external_factors
    manager = _manager(config=_config(core_pct=0.0, min_value=500000))
    assert manager._calculate_position_size(_signal()) == 250
    assert manager._last_sizing_inputs is not None

    manager.engine.get_available_cash = lambda: Decimal('300000')

    assert manager._calculate_position_size(_signal()) == 0
    assert [name for name, *_ in calls] == ['calendar', 'volatility', 'conviction'] * 2
    assert manager._last_sizing_inputs is None


# --- R8~R10 ------------------------------------------------------------------


def test_r8_nominal_mode_never_exports_atr_pct(external_factors):
    manager = _manager()
    event = _signal(metadata={'atr_pct': 6.0, 'position_multiplier': atr_position_multiplier(6.0)})

    assert manager._calculate_position_size(event) > 0

    inputs = manager._last_sizing_inputs
    assert inputs['atr_pct'] is None
    assert inputs['position_multiplier'] == atr_position_multiplier(6.0)


def test_r9_missing_signal_object_exports_unit_position_multiplier(external_factors):
    manager = _manager()
    event = _signal()
    event.signal = None
    event.metadata['position_multiplier'] = 2.0   # event 쪽에서 읽으면 잡힌다

    assert manager._calculate_position_size(event) > 0
    assert manager._last_sizing_inputs['position_multiplier'] == 1.0


def test_r10_metadata_key_sets_stay_on_the_legacy_contract(external_factors):
    nominal = _manager()
    event = _signal(metadata={'position_multiplier': 1.0})
    before_event, before_signal = set(event.metadata), set(event.signal.metadata)

    assert nominal._calculate_position_size(event) > 0
    assert set(event.metadata) == before_event
    assert set(event.signal.metadata) == before_signal

    risk = _manager(config=_config(mode='risk'), stop=_stop_recorder([]))
    risk_event = _signal(metadata={'atr_pct': 6.0,
                                   'position_multiplier': atr_position_multiplier(6.0)})

    assert risk._calculate_position_size(risk_event) == 139
    assert set(risk_event.signal.metadata) == {
        'atr_pct', 'position_multiplier', 'sizing_mode', 'risk_stop_pct', 'stop_source',
        'stop_crash_active', 'entry_risk',
    }
    assert set(risk_event.metadata) == {'atr_pct', 'position_multiplier', 'entry_risk'}
