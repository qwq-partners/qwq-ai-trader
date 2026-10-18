"""Bounded, fresh-entry KR replay of the FIRST ExitManager decision only.

No orders, fills, inferred tick paths, production configuration or live caches.
Prices and fee rates are decimal strings; policy numbers are JSON numbers.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
from enum import Enum
import hashlib
import inspect
import json
import math
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from types import FunctionType

from ..core.types import Position
from ..strategies.exit_manager import ExitConfig, ExitManager
from ..utils.fee_calculator import FeeCalculator, FeeConfig

MAX_INPUT_BYTES = 2_000_000
MAX_EVENTS = 10_000
_KST = timezone(timedelta(hours=9))
_REGISTRATION_FIELDS = {
    'stop_loss_pct', 'trailing_stop_pct', 'first_exit_pct', 'second_exit_pct',
    'third_exit_pct', 'first_exit_ratio', 'second_exit_ratio', 'third_exit_ratio',
    'stale_high_days', 'is_core', 'max_holding_days', 'trailing_activate_pct',
    'atr_pct_hint', 'strategy_name',
}
_POSITIVE = {'stop_loss_pct', 'trailing_stop_pct', 'min_stop_pct', 'max_stop_pct',
             'atr_multiplier', 'atr_trailing_multiplier', 'atr_link_multiplier',
             'atr_link_cap_pct', 'atr_pct_hint'}
_FEE_FIELDS = {'buy_commission_rate', 'sell_commission_rate', 'sell_tax_rate'}


def _canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(',', ':'),
                          ensure_ascii=True, allow_nan=False).encode('utf-8')
    except (ValueError, TypeError, OverflowError, RecursionError) as exc:
        raise ValueError('input must be finite JSON data') from exc


def _digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _object(value, allowed, required=()):
    if type(value) is not dict or not all(type(k) is str for k in value):
        raise ValueError('expected JSON object with string keys')
    if set(value) - set(allowed) or set(required) - set(value):
        raise ValueError('unknown or missing fields')
    return value


def _text(value, label):
    if type(value) is not str or not value.strip():
        raise ValueError(f'{label} must be a nonempty string')
    return value


def _decimal(value, label, *, rate=False):
    if type(value) is not str or len(value) > 64 or not re.fullmatch(
            r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?', value):
        raise ValueError(f'{label} must be a decimal string')
    number = Decimal(value)
    if not number.is_finite():
        raise ValueError(f'{label} must be finite')
    if rate:
        valid = 0 <= number < 1
    else:
        # Keep the real Decimal/float engine within its supported arithmetic range.
        valid = Decimal('1e-12') <= number <= Decimal('1e15')
    if not valid:
        raise ValueError(f'{label} out of supported range')
    return number


def _stamp(value):
    _text(value, 'at')
    try:
        stamp = datetime.fromisoformat(value)
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError('at must include timezone')
        return stamp.astimezone(_KST)
    except (ValueError, OverflowError) as exc:
        raise ValueError('invalid timezone-aware timestamp') from exc


def _number(value, name):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f'{name} must be a finite JSON number')
    value = float(value)
    if value < 0 or (name in _POSITIVE and value <= 0):
        raise ValueError(f'{name} out of range')
    if name.endswith('_ratio') and value > 1:
        raise ValueError(f'{name} must be in [0, 1]')
    if name in {'stop_loss_pct', 'trailing_stop_pct', 'min_stop_pct', 'max_stop_pct',
                'atr_link_cap_pct', 'composite_ma5_buffer_pct'} and value >= 100:
        raise ValueError(f'{name} must be below 100')
    if value > 1_000_000:
        raise ValueError(f'{name} exceeds supported numeric range')
    return value


def _settings(raw, *, registration=False):
    defaults = asdict(ExitConfig())
    allowed = _REGISTRATION_FIELDS if registration else defaults
    _object(raw, allowed)
    result = {}
    for name, value in raw.items():
        if registration and value is None and name != 'is_core':
            result[name] = None
        elif name == 'strategy_name':
            result[name] = _text(value, name)
        elif name == 'composite_trail_min_stage':
            if value not in {'none', 'first', 'second', 'third', 'trailing'}:
                raise ValueError('invalid composite_trail_min_stage')
            result[name] = value
        elif name == 'is_core' or type(defaults.get(name)) is bool:
            if type(value) is not bool:
                raise ValueError(f'{name} must be boolean')
            if name == 'eod_close' and value:
                raise ValueError('eod_close is scheduler behavior and unsupported')
            result[name] = value
        elif name.endswith('_days'):
            if type(value) is not int or not 0 <= value <= 100_000:
                raise ValueError(f'{name} must be a nonnegative bounded integer')
            result[name] = value
        else:
            result[name] = _number(value, name)
    if not registration:
        resolved = {**defaults, **result}
        if (resolved['enable_composite_trailing']
                and resolved['composite_trail_min_stage'] == 'none'):
            raise ValueError('initial-stage composite trailing requires unsupported market_data')
        if resolved['min_stop_pct'] > resolved['max_stop_pct']:
            raise ValueError('min_stop_pct exceeds max_stop_pct')
    return result


def _validate_json(value, depth=0):
    if depth > 32:
        raise ValueError('JSON nesting exceeds supported depth')
    if type(value) is dict:
        if not all(type(key) is str for key in value):
            raise ValueError('JSON object keys must be strings')
        for item in value.values():
            _validate_json(item, depth + 1)
    elif type(value) is list:
        for item in value:
            _validate_json(item, depth + 1)
    elif type(value) not in (str, int, float, bool, type(None)):
        raise ValueError('input must contain only JSON values')


def _validate(payload):
    _validate_json(payload)
    encoded = _canonical(payload)
    if len(encoded) > MAX_INPUT_BYTES:
        raise ValueError('input exceeds MAX_INPUT_BYTES')
    _object(payload, {'schema_version', 'entry', 'events', 'policies', 'fees',
                      'expected_policy_sha256'}, {'schema_version', 'entry', 'events', 'policies', 'fees'})
    data = json.loads(encoded)
    if data['schema_version'] != 'first_exit_replay_v1':
        raise ValueError('unsupported schema_version')
    entry = _object(data['entry'], {'symbol', 'quantity', 'price', 'at', 'source_ref'},
                    {'symbol', 'quantity', 'price', 'at', 'source_ref'})
    if type(entry['symbol']) is not str or not re.fullmatch(r'[0-9]{6}', entry['symbol']):
        raise ValueError('entry.symbol must be six digits')
    if type(entry['quantity']) is not int or not 0 < entry['quantity'] <= 1_000_000_000:
        raise ValueError('entry.quantity must be a positive integer <= 1000000000')
    _decimal(entry['price'], 'entry.price')
    _text(entry['source_ref'], 'entry.source_ref')
    entry_at = _stamp(entry['at'])
    if entry_at.year < 2:
        raise ValueError('entry date outside cache calendar range')
    events = data['events']
    if type(events) is not list or len(events) > MAX_EVENTS:
        raise ValueError('events must be a list within MAX_EVENTS')
    last_seq, last_at = None, entry_at
    for event in events:
        _object(event, {'sequence', 'at', 'kind', 'price', 'source_ref'},
                {'sequence', 'at', 'kind', 'price', 'source_ref'})
        sequence = event['sequence']
        if type(sequence) is not int or (last_seq is not None and sequence <= last_seq):
            raise ValueError('event sequence must be strictly increasing integers')
        at = _stamp(event['at'])
        if at < last_at or at.date() != entry_at.date():
            raise ValueError('events must be ordered after entry on the same KST date')
        if event['kind'] != 'PRICE':
            raise ValueError('only PRICE events are supported')
        _decimal(event['price'], 'event.price')
        _text(event['source_ref'], 'event.source_ref')
        last_seq, last_at = sequence, at
    _object(data['fees'], _FEE_FIELDS, _FEE_FIELDS)
    for name, value in data['fees'].items():
        _decimal(value, name, rate=True)
    if type(data['policies']) is not list or len(data['policies']) != 2:
        raise ValueError('exactly two policies are required')
    names = set()
    for policy in data['policies']:
        _object(policy, {'name', 'config', 'registration'}, {'name'})
        name = _text(policy['name'], 'policy.name')
        if name in names:
            raise ValueError('policy names must be unique')
        names.add(name)
        policy['config'] = _settings(policy.get('config', {}))
        policy['registration'] = _settings(policy.get('registration', {}), registration=True)
    if 'expected_policy_sha256' in data and (
            type(data['expected_policy_sha256']) is not str or not
            re.fullmatch('[0-9a-f]{64}', data['expected_policy_sha256'])):
        raise ValueError('expected_policy_sha256 must be a lowercase SHA-256')
    return data, hashlib.sha256(encoded).hexdigest()


def _sources():
    from ..utils import atomic_io, fee_calculator, stop_policy
    from ..indicators import atr
    from ..strategies import exit_manager
    from ..core import types
    modules = (exit_manager, fee_calculator, stop_policy, atr, types, atomic_io)
    result = {}
    for module in modules:
        path = Path(inspect.getsourcefile(module))
        key = module.__name__.replace('.', '/') + '.py'
        result[key] = hashlib.sha256(path.read_bytes()).hexdigest()
    result['src/analytics/first_exit_replay.py'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    return result


class _QuietLogger:
    """Do not send synthetic decisions into configured process logging sinks."""
    def debug(self, *args, **kwargs):
        pass

    info = warning = error = debug


def _isolated_manager(config, fees, clock, home):
    """Clone methods, never patch the module or bypass the real constructor.

    Every method shares ONE private globals mapping. The original code objects
    still implement all decisions; only clock, home, fees and logging are local.
    """
    class ReplayDate(date):
        @classmethod
        def today(cls):
            return clock[0].date()

    class ReplayDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0] if tz is None else clock[0].replace(tzinfo=_KST).astimezone(tz)

    class ReplayPath(type(Path())):
        @classmethod
        def home(cls):
            return cls(home)

    fee = FeeCalculator(FeeConfig(**{key: Decimal(value) for key, value in fees.items()}))
    original = ExitManager.__init__.__globals__
    private = dict(original)
    for key, value in original.items():
        if key != '__builtins__' and isinstance(value, (dict, list, set)):
            private[key] = deepcopy(value)
    private.update(datetime=ReplayDatetime, date=ReplayDate, Path=ReplayPath,
                   get_fee_calculator=lambda market='KR': fee, logger=_QuietLogger())
    methods = {}
    for name, value in vars(ExitManager).items():
        if isinstance(value, FunctionType):
            if value.__closure__:
                raise ValueError('ExitManager closure is unsupported for isolated replay')
            clone = FunctionType(value.__code__, private, value.__name__, value.__defaults__)
            clone.__kwdefaults__ = deepcopy(value.__kwdefaults__)
            methods[name] = clone
        elif isinstance(value, (dict, list, set)):
            methods[name] = deepcopy(value)
        elif isinstance(value, (classmethod, staticmethod, property)):
            raise ValueError('ExitManager descriptor requires isolation review')
    isolated_type = type('FirstExitReplayManager', (ExitManager,), methods)
    private['ExitManager'] = isolated_type
    return isolated_type(config=config, market='KR')


def _json_state(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _json_state(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_state(v) for v in value]
    return value


def _arm(data, policy):
    entry = data['entry']
    clock = [_stamp(entry['at']).replace(tzinfo=None)]
    config = ExitConfig(**policy['config'])
    registration = policy['registration']
    with TemporaryDirectory(prefix='qwq-first-exit-replay-') as home:
        manager = _isolated_manager(config, data['fees'], clock, home)
        price = Decimal(entry['price'])
        manager.register_position(Position(symbol=entry['symbol'], quantity=entry['quantity'],
                                           avg_price=price, current_price=price,
                                           entry_time=clock[0]), **registration)
        state = manager._states[entry['symbol']]
        initial = _json_state(asdict(state))
        initial['effective_stop_pct'] = str(manager.resolve_stop(
            dynamic_stop_pct=state.dynamic_stop_pct, fixed_stop_pct=state.stop_loss_pct,
            is_core=state.is_core).stop_pct)
        for field in _REGISTRATION_FIELDS - {'strategy_name', 'is_core', 'atr_pct_hint'}:
            actual = getattr(state, field)
            key = ('effective_configured_trailing_stop_pct' if field == 'trailing_stop_pct'
                   else 'effective_' + field)
            initial[key] = actual if actual is not None else getattr(config, field)
        decision = None
        processed = 0
        for event in data['events']:
            clock[0] = _stamp(event['at']).replace(tzinfo=None)
            signal = manager.update_price(entry['symbol'], Decimal(event['price']))
            processed += 1
            if signal is not None:
                action, quantity, reason = signal
                decision = {'at': _stamp(event['at']).isoformat(), 'sequence': event['sequence'],
                            'source_ref': event['source_ref'], 'current_price': event['price'],
                            'action': action, 'quantity': quantity, 'reason': reason,
                            'stage': state.current_stage.value,
                            'pending_stage': state.pending_stage.value if state.pending_stage else None}
                break
        resolved = asdict(config)
        return {'name': policy['name'], 'status': 'first_decision_observed' if decision else
                'not_observed_in_supplied_path', 'first_decision': decision,
                'resolved_config': resolved, 'registration': registration,
                'effective_initial_state': initial, 'final_state': _json_state(asdict(state)),
                'events_processed': processed,
                'config_sha256': _digest({'config': resolved, 'registration': registration})}


def replay_first_exit(payload: dict) -> dict:
    """Compare exactly two policies up to each first emitted decision, or reject.

    Omitted settings use ExitConfig defaults, not live environment settings.
    Prices: [1e-12, 1e15]; quantity: [1, 1e9]. Config floats are finite JSON
    numbers <= 1e6, day counts integers <= 1e5. These are adapter bounds.
    """
    try:
        data, input_hash = _validate(payload)
        sources = _sources()
        policy_hash = _digest(sources)
        expected = data.get('expected_policy_sha256')
        if expected is not None and expected != policy_hash:
            raise ValueError('policy source hash mismatch')
        with localcontext(Context(prec=28, rounding=ROUND_HALF_EVEN)):
            arms = [_arm(data, policy) for policy in data['policies']]
        left, right = [arm['first_decision'] for arm in arms]
        signatures = [None if decision is None else
                      (decision['sequence'], decision['action'], decision['quantity'])
                      for decision in (left, right)]
        return {
            'schema_version': 'first_exit_replay_result_v1',
            'input_sha256': input_hash, 'policy_source_sha256': policy_hash,
            'source_files': sources, 'entry': data['entry'], 'fees': data['fees'],
            'decimal_context': {'precision': 28, 'rounding': 'ROUND_HALF_EVEN'},
            'arms': arms,
            'comparison': {'first_decision_changed': None if left is None and right is None
                           else signatures[0] != signatures[1]},
            'fill_status': 'not_simulated', 'net_pnl': None, 'live_exit_parity': False,
            'limitations': [
                'Fresh entry and supplied PRICE events on one KST date only.',
                'Stops at first emitted decision; no fills, stage advancement or subsequent exits.',
                'Absence of a decision covers only the supplied path, not a complete session.',
                'No order admission, liquidity, slippage, execution, portfolio or profitability simulation.',
                'No reconstructed OHLC path, price history, dynamic-stop ATR calculation or market_data.',
                'No regime/crash events, live hooks, restored state, holding-day or scheduler/EOD simulation.',
                'ATR hints affect trailing; they do not create a dynamic initial stop.',
                'Constructor baseline has no applied regime overlay, including the neutral regime table.',
                'Resolved defaults are ExitConfig defaults, not verified live environment settings.',
                'Source references and input completeness are caller evidence, not independently verified.',
            ],
        }
    except (TypeError, KeyError, AttributeError, ArithmeticError, OverflowError, RecursionError) as exc:
        raise ValueError('invalid or unsupported first exit replay input') from exc
