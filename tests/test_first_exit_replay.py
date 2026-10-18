"""Offline replay contracts; all price paths are synthetic, never market evidence."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from decimal import Decimal
import importlib
import os
from pathlib import Path

import pytest


def replay(payload):
    # Lazy import lets missing implementation fail each behavioral contract clearly.
    return importlib.import_module('src.analytics.first_exit_replay').replay_first_exit(payload)


def sample(prices=('10000', '11000', '9000')):
    return {
        'schema_version': 'first_exit_replay_v1',
        'entry': {'symbol': '005930', 'quantity': 100, 'price': '10000',
                  'at': '2026-10-19T09:00:00+09:00', 'source_ref': 'synthetic:entry'},
        'events': [{'sequence': i + 1, 'at': f'2026-10-19T09:{i + 1:02d}:00+09:00',
                    'kind': 'PRICE', 'price': p, 'source_ref': f'synthetic:tick:{i + 1}'}
                   for i, p in enumerate(prices)],
        'policies': [{'name': 'A'}, {'name': 'B'}],
        'fees': {'buy_commission_rate': '0', 'sell_commission_rate': '0', 'sell_tax_rate': '0'},
    }


def test_first_partial_does_not_fill_or_advance_and_later_stop_is_not_processed():
    result = replay(sample())
    arm = result['arms'][0]
    assert arm['status'] == 'first_decision_observed'
    assert arm['first_decision']['sequence'] == 2
    assert arm['first_decision']['action'] == 'sell_partial'
    assert arm['first_decision']['quantity'] == 10
    assert arm['first_decision']['source_ref'] == 'synthetic:tick:2'
    assert arm['final_state']['current_stage'] == 'none'
    assert arm['final_state']['pending_stage'] == 'first'
    assert arm['final_state']['remaining_quantity'] == 100
    assert arm['final_state']['pending_filled_qty'] == 0
    assert len(arm['final_state']['exit_history']) == 1
    assert arm['final_state']['pending_since'] == '2026-10-19T09:02:00'
    assert result['comparison']['first_decision_changed'] is False
    assert result['fill_status'] == 'not_simulated'
    assert result['net_pnl'] is None
    assert result['live_exit_parity'] is False


def test_policy_stop_override_and_gap_current_price_are_retained():
    data = sample(('9600', '9000'))
    data['policies'][1]['registration'] = {'stop_loss_pct': 3}
    result = replay(data)
    assert [a['first_decision']['sequence'] for a in result['arms']] == [2, 1]
    assert result['arms'][0]['first_decision']['current_price'] == '9000'
    assert result['arms'][1]['effective_initial_state']['effective_stop_pct'] == '3.0'
    assert result['comparison']['first_decision_changed'] is True


def test_no_signal_is_only_absence_in_supplied_path_and_empty_path_is_valid():
    for prices in (('10000', '10100'), ()):
        result = replay(sample(prices))
        assert result['comparison']['first_decision_changed'] is None
        assert all(a['status'] == 'not_observed_in_supplied_path' for a in result['arms'])
        assert all(a['first_decision'] is None for a in result['arms'])


def test_fees_change_first_decision_without_claiming_profit():
    data = sample(('9500',))
    data['fees']['buy_commission_rate'] = '0.01'
    data['policies'][0]['config'] = {'stop_loss_pct': 5.5}
    data['policies'][1]['config'] = {'stop_loss_pct': 5.5, 'include_fees': False}
    result = replay(data)
    assert result['arms'][0]['first_decision']['action'] == 'sell_all'
    assert result['arms'][1]['first_decision'] is None


def test_atr_hint_caps_trailing_but_does_not_create_dynamic_initial_stop():
    data = sample(('12000', '11200'))
    for policy in data['policies']:
        policy['config'] = {'enable_partial_exit': False}
        policy['registration'] = {'atr_pct_hint': 10}
    result = replay(data)
    arm = result['arms'][0]
    assert arm['effective_initial_state']['dynamic_stop_pct'] is None
    assert arm['effective_initial_state']['effective_trailing_stop_pct'] == 6
    assert arm['first_decision']['sequence'] == 2
    assert 'ATR-linked' in arm['first_decision']['reason']


def test_core_breakeven_activates_before_first_exit():
    data = sample(('10600', '9700'))
    for policy in data['policies']:
        policy['registration'] = {'is_core': True, 'stop_loss_pct': 10, 'trailing_stop_pct': 20}
    arm = replay(data)['arms'][0]
    assert arm['final_state']['breakeven_activated'] is True
    assert arm['first_decision']['sequence'] == 2
    assert '본전 이탈' in arm['first_decision']['reason']


def test_same_daily_ohlc_different_intraday_order_can_change_first_action():
    up_first = replay(sample(('10000', '11000', '9000', '10000')))
    down_first = replay(sample(('10000', '9000', '11000', '10000')))
    assert up_first['arms'][0]['first_decision']['action'] == 'sell_partial'
    assert down_first['arms'][0]['first_decision']['action'] == 'sell_all'


def test_deterministic_nonmutating_concurrent_isolation():
    from src.strategies import exit_manager
    from src.utils import fee_calculator
    data = sample()
    before = deepcopy(data)
    globals_before = dict(exit_manager.ExitManager.__init__.__globals__)
    fee_before = deepcopy(vars(fee_calculator._fee_calculator.config))
    env_before = dict(os.environ)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(replay, [data] * 8))
    assert all(r == results[0] for r in results)
    assert data == before
    assert dict(os.environ) == env_before
    assert globals_before == exit_manager.ExitManager.__init__.__globals__
    assert vars(fee_calculator._fee_calculator.config) == fee_before
    assert exit_manager.Path is Path and exit_manager.datetime is datetime and exit_manager.date is date


@pytest.mark.parametrize('path,value', [
    (('schema_version',), 'v0'), (('unknown',), 1),
    (('entry', 'symbol'), '123'), (('entry', 'quantity'), True),
    (('entry', 'quantity'), 0), (('entry', 'price'), 10000),
    (('entry', 'price'), 'NaN'), (('entry', 'price'), '1e99999'),
    (('entry', 'at'), '2026-10-19T09:00:00'),
    (('entry', 'source_ref'), ''), (('events', 0, 'kind'), 'FILL'),
    (('events', 0, 'sequence'), True), (('events', 1, 'sequence'), 1),
    (('events', 0, 'at'), '2026-10-19T08:59:59+09:00'),
    (('events', 1, 'at'), '2026-10-19T09:00:01+09:00'),
    (('events', 0, 'at'), '2026-10-20T09:00:00+09:00'),
    (('events', 0, 'price'), '-1'), (('events', 0, 'price'), 'Infinity'),
    (('fees', 'sell_tax_rate'), '1'), (('fees', 'sell_tax_rate'), '-0.1'),
    (('fees', 'sell_tax_rate'), True), (('policies', 1, 'name'), 'A'),
    (('policies', 0, 'registration'), {'price_history': {}}),
    (('policies', 0, 'registration'), {'entry_time': '2026-10-19'}),
    (('policies', 0, 'registration'), {'stop_loss_pct': 0}),
    (('policies', 0, 'registration'), {'first_exit_ratio': 1.1}),
    (('policies', 0, 'registration'), {'atr_pct_hint': float('inf')}),
    (('policies', 0, 'config'), {'stop_loss_pct': True}),
    (('policies', 0, 'config'), {'include_fees': 1}),
    (('policies', 0, 'config'), {'stale_exit_days': 1.5}),
    (('policies', 0, 'config'), {'min_stop_pct': 9, 'max_stop_pct': 8}),
    (('policies', 0, 'config'), {'composite_trail_min_stage': 'invalid'}),
    (('policies', 0, 'config'), {'eod_close': True}),
    (('expected_policy_sha256',), '0' * 64),
])
def test_invalid_and_unsupported_inputs_fail_closed(path, value):
    data = sample()
    parent = data
    for part in path[:-1]:
        parent = parent[part]
    parent[path[-1]] = value
    with pytest.raises(ValueError):
        replay(data)


def test_timezone_normalization_accepts_equal_instants_and_kst_day():
    data = sample(('9500',))
    data['entry']['at'] = '2026-10-18T23:00:00Z'
    data['events'][0]['at'] = '2026-10-19T08:00:00+09:00'
    assert replay(data)['arms'][0]['first_decision']['at'] == '2026-10-19T08:00:00+09:00'


def test_source_pin_and_input_hash_bind_evidence():
    data = sample()
    result = replay(data)
    assert len(result['policy_source_sha256']) == 64
    assert {'src/strategies/exit_manager.py', 'src/utils/fee_calculator.py',
            'src/utils/stop_policy.py', 'src/indicators/atr.py'} <= set(result['source_files'])
    data['expected_policy_sha256'] = result['policy_source_sha256']
    pinned = replay(data)
    assert pinned['policy_source_sha256'] == result['policy_source_sha256']
    assert pinned['input_sha256'] != result['input_sha256']


def test_payload_and_event_limits_are_enforced():
    data = sample(())
    data['entry']['source_ref'] = 'x' * 2_000_000
    with pytest.raises(ValueError):
        replay(data)
    data = sample(('10000',))
    data['events'] *= 10_001
    with pytest.raises(ValueError):
        replay(data)


def test_same_decision_with_different_stop_reason_is_not_a_changed_decision():
    data = sample(('9000',))
    data['policies'][0]['registration'] = {'stop_loss_pct': 4}
    data['policies'][1]['registration'] = {'stop_loss_pct': 8}
    result = replay(data)
    assert result['arms'][0]['first_decision']['reason'] != result['arms'][1]['first_decision']['reason']
    assert result['comparison']['first_decision_changed'] is False


def test_missing_composite_inputs_for_initial_stage_are_rejected():
    data = sample()
    data['policies'][0]['config'] = {'composite_trail_min_stage': 'none'}
    with pytest.raises(ValueError):
        replay(data)
    data['policies'][0]['config']['enable_composite_trailing'] = False
    assert replay(data)['arms'][0]['first_decision']['action'] == 'sell_partial'


def test_invalid_last_event_is_not_hidden_by_an_earlier_first_decision():
    data = sample()
    data['events'][-1]['price'] = 'NaN'
    with pytest.raises(ValueError):
        replay(data)


@pytest.mark.parametrize('prices,registration', [
    (('10000', '11000', '9000'), {}),
    (('9600', '9400'), {'stop_loss_pct': 3}),
    (('12000', '11200'), {'atr_pct_hint': 10, 'first_exit_ratio': 0,
                          'second_exit_ratio': 0, 'third_exit_ratio': 0}),
    (('10600', '9700'), {'is_core': True, 'stop_loss_pct': 10, 'trailing_stop_pct': 20}),
])
def test_matches_direct_real_engine_with_independent_private_bindings(tmp_path, prices, registration):
    """Adapter miswiring is caught by invoking the actual class without its adapter."""
    from types import FunctionType, MethodType
    from src.core.types import Position
    from src.strategies.exit_manager import ExitConfig, ExitManager
    from src.utils.fee_calculator import FeeCalculator, FeeConfig

    data = sample(prices)
    for policy in data['policies']:
        policy['registration'] = registration
    clock = [datetime(2026, 10, 19, 9)]

    class DirectClock(datetime):
        @classmethod
        def now(cls):
            return clock[0]

    class DirectDate(date):
        @classmethod
        def today(cls):
            return clock[0].date()

    class DirectPath(type(Path())):
        @classmethod
        def home(cls):
            return cls(tmp_path)

    namespace = dict(ExitManager.__init__.__globals__)
    namespace.update(datetime=DirectClock, date=DirectDate, Path=DirectPath,
                     get_fee_calculator=lambda *args: FeeCalculator(FeeConfig(
                         buy_commission_rate=Decimal('0'), sell_commission_rate=Decimal('0'),
                         sell_tax_rate=Decimal('0'))))
    manager = object.__new__(ExitManager)
    for name, method in vars(ExitManager).items():
        if isinstance(method, FunctionType):
            copied = FunctionType(method.__code__, namespace, name, method.__defaults__)
            copied.__kwdefaults__ = method.__kwdefaults__
            setattr(manager, name, MethodType(copied, manager))
    manager.__init__(ExitConfig(), 'KR')
    manager.register_position(Position(symbol='005930', quantity=100,
                                       avg_price=Decimal('10000'), current_price=Decimal('10000'),
                                       entry_time=clock[0]), **registration)
    direct = None
    for event in data['events']:
        clock[0] = datetime.fromisoformat(event['at']).replace(tzinfo=None)
        direct = manager.update_price('005930', Decimal(event['price']))
        if direct is not None:
            break
    actual = replay(data)['arms'][0]['first_decision']
    assert (actual['action'], actual['quantity'], actual['reason']) == direct
    assert actual['sequence'] == event['sequence']


def test_concurrent_different_dates_fees_and_policies_do_not_bleed():
    inputs = []
    for day, rate in ((19, '0'), (20, '0.1'), (21, '0.01'), (22, '0.2')):
        data = sample(('10000', '11000'))
        data['entry']['at'] = f'2026-10-{day}T09:00:00+09:00'
        for event in data['events']:
            event['at'] = event['at'].replace('10-19', f'10-{day}')
        data['fees']['buy_commission_rate'] = rate
        inputs.append(data)
    expected = [replay(data) for data in inputs]
    with ThreadPoolExecutor(max_workers=4) as pool:
        actual = list(pool.map(replay, inputs))
    assert actual == expected
    assert actual[0]['arms'][0]['first_decision']['action'] == 'sell_partial'
    assert actual[1]['arms'][0]['first_decision']['action'] == 'sell_all'
    assert actual[2]['arms'][0]['first_decision'] is None
    assert actual[3]['arms'][0]['first_decision']['at'] == '2026-10-22T09:01:00+09:00'


def test_private_cache_directories_are_cleaned_after_each_arm(monkeypatch):
    from tempfile import TemporaryDirectory
    module = importlib.import_module('src.analytics.first_exit_replay')
    paths = []

    def tracking_directory(*args, **kwargs):
        directory = TemporaryDirectory(*args, **kwargs)
        paths.append(Path(directory.name))
        return directory

    monkeypatch.setattr(module, 'TemporaryDirectory', tracking_directory)
    replay(sample())
    assert len(paths) == 2 and paths[0] != paths[1]
    assert all(not path.exists() for path in paths)


def test_python_non_json_containers_are_rejected_instead_of_coerced():
    data = sample()
    data['events'] = tuple(data['events'])
    with pytest.raises(ValueError):
        replay(data)


def test_caller_decimal_context_does_not_change_replay_or_get_mutated():
    from decimal import localcontext, ROUND_UP
    data = sample(('10999.99', '11000.01'))
    data['fees']['buy_commission_rate'] = '0.000140527'
    expected = replay(data)
    with localcontext() as context:
        context.prec = 4
        context.rounding = ROUND_UP
        actual = replay(data)
        assert context.prec == 4 and context.rounding == ROUND_UP
    assert actual == expected
