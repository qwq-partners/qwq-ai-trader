"""RR 근거의 실제 배치→대기 JSON→실행→signal_events 연결과 매매 불변성."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import json
from types import SimpleNamespace

import pytest

from src.core import engine as eng
from src.core.event import SignalEvent
from src.strategies.kr.sepa_trend import SEPATrendStrategy
from test_batch_indicator_roundtrip import analyzer  # noqa: F401
from test_t11_entry_plan import NOW, SYM
from test_risk_sizing import home  # noqa: F401


def _signals(monkeypatch):
    import datetime as clock
    class Morning(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 15, 10, 30, tzinfo=tz)
    monkeypatch.setattr(clock, 'datetime', Morning)
    strategy = SEPATrendStrategy()
    monkeypatch.setattr(strategy, '_calculate_sepa_score', lambda row: 80)
    candidates = [SimpleNamespace(symbol=sym, name='합성', entry_price=D('10000'),
                                 indicators={'atr_14': atr}, reasons=[])
                  for sym, atr in [('000001', 2.6), (SYM, 4.9)]]
    return asyncio.run(strategy.generate_batch_signals(candidates))


def _logged_execution(ba, monkeypatch):
    logged, events = [], []
    rm = object.__new__(eng.RiskManager)
    rm.engine = SimpleNamespace(_market_regime='caution')
    class Sink:
        async def log(self, **kw):
            logged.append(kw)
    monkeypatch.setattr(eng._SigLog, 'get', staticmethod(lambda: Sink()))
    async def emit(event):
        events.append(event)
        rm._log_sig(event, event_type='passed')
    async def quote(_symbol):
        return {'price': 10100}
    async def positions():
        return {}
    async def nxt():
        return set()
    async def no_sleep(_seconds):
        pass
    ba._engine = SimpleNamespace(portfolio=SimpleNamespace(positions={}), emit=emit,
                                 _stock_name_cache={})
    ba._broker = SimpleNamespace(get_quote=quote, get_positions=positions,
                                 get_nxt_symbols=nxt)
    ba._sector_momentum = None
    ba._config = {'batch': {'signal_interval_sec': 0}}
    ba._market_regime = 'caution'
    monkeypatch.setattr('src.core.batch_analyzer.asyncio.sleep', no_sleep)
    asyncio.run(ba.execute_pending_signals())
    assert len(events) == len(logged) == 1
    return events[0], logged[0]['metadata']


def test_source_gate_survives_pending_json_execution_and_real_log(analyzer, monkeypatch):
    signals = _signals(monkeypatch)
    assert [s.symbol for s in signals] == [SYM]
    signal = signals[0]
    pending = analyzer._to_pending_signal(signal, now=NOW,
                                          expires=NOW + timedelta(hours=5), slippage_pct=3)
    analyzer._pending = [pending]
    analyzer._save_json()
    analyzer._pending = []  # 실제 JSON 재로드
    event, logged = _logged_execution(analyzer, monkeypatch)
    assert 'rr_context' in logged
    context = logged['rr_context']
    selection = context['selection']
    assert selection['gate'] == {'basis': 'entry_roundtrip_per_share',
                                  'threshold': '2.0', 'passed': True}
    assert selection['prices'] == {'entry': '10000', 'target': '11470.000', 'stop': '9299.9999999999990000'}
    assert selection['diagnostics']['round_trip_cost'] == 22
    assert selection['fee_snapshot']['buy_commission_rate'] == '0.000140527'
    assert context['signal']['prices'] == {'entry': '10100.0', 'target': '11470.0', 'stop': '9847.5'}
    assert context['signal']['signal_id'] == event.id
    assert context['signal']['source'] == 'batch_analyzer'
    assert context['plan']['plan_id'] == pending.plan_id
    assert context['signal']['quote_as_of'] == NOW.isoformat()
    assert context['quantities'] == {'requested_quantity': None,
                                     'entry_risk': {'planned_quantity': None, 'planned_price': None},
                                     'size_trace_quantity': None}
    assert 'rr_selection' not in event.metadata  # entry_plan 경로만 사용
    assert event.score == signal.score == 80
    assert event.price == D('10100') and event.stop_price == D('9847.5')


def _selection(monkeypatch):
    return _signals(monkeypatch)[0].metadata['rr_selection']


def test_snapshot_ownership_through_every_boundary(analyzer, monkeypatch):
    signal = _signals(monkeypatch)[0]
    before = deepcopy(signal.metadata['rr_selection'])
    pending = analyzer._to_pending_signal(signal, now=NOW,
                                          expires=NOW + timedelta(hours=5), slippage_pct=3)
    signal.metadata['rr_selection']['prices']['entry'] = '1'
    signal.metadata['rr_selection']['fee_snapshot']['sell_tax_rate'] = '1'
    assert pending.inputs_ref['rr_selection'] == before
    analyzer._pending = [pending]
    analyzer._save_json()
    event, logged = _logged_execution(analyzer, monkeypatch)
    frozen = deepcopy(logged['rr_context'])
    event.metadata['entry_plan']['inputs_ref']['rr_selection']['prices']['stop'] = '2'
    event.price = D('1')
    assert logged['rr_context'] == frozen
    assert frozen['selection'] == before


@pytest.mark.parametrize('bad', [None, {}, {'version': 99}, True, [], float('nan')])
def test_missing_or_malformed_selection_never_invents_gate(bad):
    from src.utils.rr_context import copy_rr_selection
    result = copy_rr_selection(bad, symbol=SYM, strategy='sepa_trend')
    assert result['status'] == 'unavailable'
    assert 'gate' not in result
    if bad is None:
        assert result['reason'] == 'not_captured'
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize('field,bad', [
    ('version', True), ('version', 2), ('source_ref', 'wrong'),
    ('symbol', '000000'), ('strategy', 'vcp_breakout'),
    ('captured_at', 'x' * 257), ('prices', {'entry': 'NaN', 'target': '2', 'stop': '1'}),
    ('prices', {'entry': '1' * 129, 'target': '2', 'stop': '1'}),
    ('prices', {'entry': '1e999999999', 'target': '2', 'stop': '1'}),
    ('gate', {'basis': 'gross', 'threshold': '2.0', 'passed': True}),
    ('diagnostics', {'gross_rr': float('inf')}),
])
def test_invalid_captured_schema_is_not_accepted(monkeypatch, field, bad):
    from src.utils.rr_context import copy_rr_selection
    value = _selection(monkeypatch)
    value[field] = bad
    result = copy_rr_selection(value, symbol=SYM, strategy='sepa_trend')
    assert result['status'] in {'unavailable', 'conflict'}
    assert 'gate' not in result
    json.dumps(result, allow_nan=False)


def test_cycles_and_arbitrary_objects_are_not_serialized(monkeypatch):
    from src.utils.rr_context import copy_rr_selection
    value = _selection(monkeypatch)
    value['prices'] = value
    assert copy_rr_selection(value, symbol=SYM, strategy='sepa_trend')['status'] == 'unavailable'
    class Hostile:
        def __str__(self):
            raise AssertionError('must not stringify arbitrary objects')
    value['prices'] = Hostile()
    assert copy_rr_selection(value, symbol=SYM, strategy='sepa_trend')['status'] == 'unavailable'


def test_direct_and_plan_conflict_has_no_arbitrary_winner(monkeypatch):
    from src.utils.rr_context import build_rr_log_context
    signal = _signals(monkeypatch)[0]
    event = SignalEvent.from_signal(signal)
    planned = deepcopy(signal.metadata['rr_selection'])
    planned['prices']['entry'] = '9999'
    event.metadata['entry_plan'] = {'symbol': SYM, 'strategy': 'sepa_trend',
                                   'inputs_ref': {'rr_selection': planned}}
    result = build_rr_log_context(event)
    assert result['selection']['status'] == 'conflict'
    assert 'gate' not in result['selection']
    event.metadata['entry_plan']['inputs_ref']['rr_selection'] = deepcopy(signal.metadata['rr_selection'])
    assert build_rr_log_context(event)['selection']['status'] == 'captured'
    event.metadata['entry_plan']['symbol'] = '000000'
    assert build_rr_log_context(event)['selection']['status'] == 'conflict'


@pytest.mark.parametrize('quantity', [None, 0, 12, True, -1, 10**13, 1.5, float('inf')])
def test_distinct_quantity_evidence_and_no_submission_claim(monkeypatch, quantity):
    from src.utils.rr_context import build_rr_log_context
    event = SignalEvent.from_signal(_signals(monkeypatch)[0])
    event.metadata.update(entry_risk={'planned_quantity': quantity, 'planned_price': '10000'},
                          size_trace={'quantity': 0})
    result = build_rr_log_context(event, {'requested_quantity': quantity})
    expected = quantity if type(quantity) is int and 0 <= quantity <= 10**12 else None
    assert result['quantities'] == {'requested_quantity': expected,
                                    'entry_risk': {'planned_quantity': expected, 'planned_price': '10000'},
                                    'size_trace_quantity': 0}
    frozen = deepcopy(result)
    event.metadata['entry_risk']['planned_quantity'] = 999
    assert result == frozen
    assert 'submitted' not in json.dumps(result)


def test_log_freezes_before_scheduled_writer_and_caller_cannot_override(monkeypatch):
    event = SignalEvent.from_signal(_signals(monkeypatch)[0], source='fixture')
    event.metadata.update(entry_risk={'planned_quantity': 7, 'planned_price': '10000'},
                          size_trace={'quantity': 7}, llm_decision={'decision_id': 'fixture'})
    rm = object.__new__(eng.RiskManager)
    rm.engine = SimpleNamespace(_market_regime='neutral')
    logged = []
    class Sink:
        async def log(self, **kw):
            logged.append(kw['metadata'])
    monkeypatch.setattr(eng._SigLog, 'get', staticmethod(lambda: Sink()))
    async def run():
        rm._log_sig(event, event_type='passed', metadata={'rr_context': {'spoof': True},
                                                       'requested_quantity': 0})
        event.metadata['rr_selection']['prices']['entry'] = '1'
        event.metadata['entry_risk']['planned_quantity'] = 99
        event.price = D('2')
        await asyncio.sleep(0)
    asyncio.run(run())
    result = logged[0]['rr_context']
    assert result['selection']['prices']['entry'] == '10000'
    assert result['signal']['prices']['entry'] == '10000'
    assert result['quantities']['entry_risk']['planned_quantity'] == 7
    assert result['quantities']['requested_quantity'] == 0
    assert logged[0]['llm_decision'] == {'decision_id': 'fixture'}


@pytest.mark.parametrize('fail_at', ['fee', 'selection', 'pending', 'log'])
def test_capture_exception_does_not_change_signal_or_execution(analyzer, monkeypatch, fail_at):
    baseline = _signals(monkeypatch)[0]
    def broken(*args, **kw):
        raise RuntimeError('diagnostic capture broken')
    paths = {'fee': 'src.strategies.base.capture_fee_snapshot',
             'selection': 'src.strategies.kr.sepa_trend.capture_sepa_rr_selection',
             'pending': 'src.core.batch_analyzer.copy_rr_selection',
             'log': 'src.core.engine.build_rr_log_context'}
    monkeypatch.setattr(paths[fail_at], broken)
    signal = _signals(monkeypatch)[0]
    for field in ('symbol', 'side', 'strength', 'price', 'target_price', 'stop_price', 'score', 'confidence'):
        assert getattr(signal, field) == getattr(baseline, field)
    assert {k: v for k, v in signal.metadata.items() if k != 'rr_selection'} == {
        k: v for k, v in baseline.metadata.items() if k != 'rr_selection'}
    pending = analyzer._to_pending_signal(signal, now=NOW,
                                          expires=NOW + timedelta(hours=5), slippage_pct=3)
    analyzer._pending = [pending]
    analyzer._save_json()
    event, logged = _logged_execution(analyzer, monkeypatch)
    assert event.price == D('10100') and event.stop_price == D('9847.5')
    if fail_at == 'fee':
        assert logged['rr_context']['selection']['fee_snapshot']['status'] == 'unavailable'
    elif fail_at == 'log':
        assert logged['rr_context']['status'] == 'unavailable'
    else:
        assert logged['rr_context']['selection']['status'] == 'unavailable'


@pytest.mark.parametrize('rates', [None, D('0'), D('NaN'), D('-1'), True])
def test_fee_snapshot_preserves_zero_and_never_changes_boolean(monkeypatch, rates):
    from src.utils.fee_calculator import FeeConfig
    config = FeeConfig(buy_commission_rate=rates)
    fees = SimpleNamespace(config=config, calculate_buy_fee=lambda p: D(0),
                           calculate_sell_fee=lambda p: D(0))
    monkeypatch.setattr('src.utils.fee_calculator.get_fee_calculator', lambda *a: fees)
    diagnostics = {}
    assert SEPATrendStrategy().check_rr_ratio(D(100), D(120), D(90), diagnostics=diagnostics)
    snapshot = diagnostics['fee_snapshot']
    assert snapshot['status'] == ('captured' if rates == D(0) and type(rates) is D else 'unavailable')
    if snapshot['status'] == 'captured':
        assert snapshot['buy_commission_rate'] == '0'
        config.buy_commission_rate = D('0.5')
        assert snapshot['buy_commission_rate'] == '0'


def test_missing_fee_config_and_missing_diagnostics_are_honest(monkeypatch):
    from src.utils.rr_context import capture_sepa_rr_selection
    fees = SimpleNamespace(calculate_buy_fee=lambda p: D(0), calculate_sell_fee=lambda p: D(0))
    monkeypatch.setattr('src.utils.fee_calculator.get_fee_calculator', lambda *a: fees)
    signal = _signals(monkeypatch)[0]
    assert signal.metadata['rr_selection']['fee_snapshot']['status'] == 'unavailable'
    assert capture_sepa_rr_selection(signal, {})['status'] == 'unavailable'


def test_timestamp_timezone_is_preserved(monkeypatch):
    from src.utils.rr_context import capture_sepa_rr_selection
    signal = _signals(monkeypatch)[0]
    signal.timestamp = datetime(2026, 9, 15, 10, 30, tzinfo=timezone(timedelta(hours=9)))
    diagnostics = signal.metadata['rr_selection']['diagnostics'].copy()
    diagnostics['fee_snapshot'] = signal.metadata['rr_selection']['fee_snapshot']
    assert capture_sepa_rr_selection(signal, diagnostics)['captured_at'] == '2026-09-15T10:30:00+09:00'


def test_present_null_direct_snapshot_conflicts_with_captured_plan(monkeypatch):
    from src.utils.rr_context import build_rr_log_context
    event = SignalEvent.from_signal(_signals(monkeypatch)[0])
    event.metadata['entry_plan'] = {'symbol': SYM, 'strategy': 'sepa_trend',
                                   'inputs_ref': {'rr_selection': event.metadata['rr_selection']}}
    event.metadata['rr_selection'] = None
    assert build_rr_log_context(event)['selection']['status'] == 'conflict'


@pytest.mark.parametrize('mode', ['risk', 'nominal'])
@pytest.mark.parametrize('path', ['positive', 'zero', 'cash', 'g3', 'pending_race'])
def test_real_order_path_is_identical_when_log_capture_raises(home, monkeypatch, mode, path):
    from test_risk_sizing import _em, _rm, _sig
    from test_t11_entry_plan import _order_env
    from test_t11_money_path_baseline import _fingerprint
    selection = _selection(monkeypatch)
    original_capture = eng.build_rr_log_context
    def broken(*args, **kwargs):
        raise RuntimeError('optional metadata failure')
    def run(fail):
        monkeypatch.setattr(eng, 'build_rr_log_context', broken if fail else original_capture)
        rm = _rm(monkeypatch, mode=mode, em=_em(),
                 cash='0' if path == 'cash' else '5000000',
                 min_value=9000000 if path == 'zero' else 200000)
        logged = _order_env(monkeypatch, rm)
        if path == 'g3':
            rm.engine.can_open_position = lambda *a, **k: (False, 'fixture G3')
        if path == 'pending_race':
            def race(*args, **kwargs):
                rm._pending_orders.add(SYM)
                return True, ''
            rm.engine.can_open_position = race
        event = _sig(2.5, rr_selection=deepcopy(selection), llm_decision={'decision_id': 'fixture'})
        event.timestamp = event.signal.timestamp = NOW
        orders = asyncio.run(rm.on_signal(event))
        assert len(logged) == 1
        old_meta = {key: deepcopy(logged[0]['metadata'][key])
                    for key in ('entry_risk', 'size_trace', 'llm_decision')}
        return ([_fingerprint(o.order) for o in (orders or []) ],
                set(rm._pending_orders), dict(rm._pending_quantities),
                dict(rm._last_signal_time), old_meta,
                logged[0]['event_type'], logged[0]['block_gate'],
                logged[0]['metadata']['rr_context'])
    baseline = run(False)
    failed = run(True)
    assert baseline[:-1] == failed[:-1]
    assert failed[-1]['status'] == 'unavailable'
    quantities = baseline[-1]['quantities']
    if path == 'cash':
        assert quantities['requested_quantity'] is None
        assert quantities['entry_risk']['planned_quantity'] is None
        assert quantities['size_trace_quantity'] is None
    elif path == 'zero':
        assert quantities['requested_quantity'] == 0
        assert quantities['size_trace_quantity'] == 0
    else:
        expected = 139 if mode == 'risk' else 239
        assert quantities['size_trace_quantity'] == expected
        assert quantities['entry_risk']['planned_quantity'] == (expected if mode == 'risk' else None)
        assert bool(baseline[0]) is (path == 'positive')


@pytest.mark.parametrize('strategy', ['sepa_trend', 'vcp_breakout'])
def test_legacy_signal_does_not_acquire_known_gate(analyzer, monkeypatch, strategy):
    from src.core.types import StrategyType
    from src.utils.rr_context import build_rr_log_context
    signal = _signals(monkeypatch)[0]
    signal.metadata.pop('rr_selection')
    signal.strategy = StrategyType(strategy)
    pending = analyzer._to_pending_signal(signal, now=NOW,
                                          expires=NOW + timedelta(hours=5), slippage_pct=3)
    assert pending.inputs_ref['rr_selection']['reason'] == 'not_captured'
    event = SignalEvent.from_signal(signal)
    event.metadata['entry_plan'] = pending.to_dict()
    result = build_rr_log_context(event)
    assert result['selection']['reason'] == 'not_captured'
    assert 'gate' not in result['selection']
