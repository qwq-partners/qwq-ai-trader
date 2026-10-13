"""Frozen pre-LLM sizing evidence; replay never establishes order admission.

Only explicit recorder-enabled capture reads current overlays. Replay uses the real
engine calculation on an isolated object, with no live portfolio/cache references.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import inspect
import json
import math
from types import FunctionType, SimpleNamespace
from typing import Any

SCHEMA_VERSION = 'neutral-policy-sizing-v1'
_CONFIG_FIELDS = ('base_position_pct', 'max_position_pct', 'min_position_value',
                  'daily_max_loss_pct', 'sizing_mode', 'risk_per_trade_pct',
                  'risk_max_position_pct', 'strategy_allocation')
_HYBRID_FIELDS = ('enabled', 'day_trading_pct', 'day_base_position_pct', 'day_max_position_pct',
                  'short_term_pct', 'short_term_base_position_pct', 'short_term_max_position_pct',
                  'swing_pct', 'swing_base_position_pct', 'swing_max_position_pct')


def _json_copy(value):
    return json.loads(json.dumps(value, sort_keys=True, allow_nan=False))


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


def _number(value):
    if isinstance(value, bool):
        raise ValueError('boolean is not a sizing number')
    number = float(value)
    if not math.isfinite(number):
        raise ValueError('nonfinite sizing input')
    return number


def _money(value):
    _number(value)
    return str(Decimal(str(value)))


def policy_source_hash():
    """Bind replay to the exact calculation and numeric helper implementations."""
    from ..core.engine import RiskManager
    from ..utils.sizing import atr_position_multiplier, planned_risk, risk_quantity_cap
    from ..utils.fee_calculator import FeeCalculator
    return _digest([inspect.getsource(fn) for fn in (
        RiskManager._calculate_position_size, RiskManager._get_hybrid_params,
        atr_position_multiplier, planned_risk, risk_quantity_cap,
        FeeCalculator.calculate_buy_fee)])


class _OverlayIsolationError(ValueError):
    """Unsupported overlay isolation is missing evidence, not a live overlay error."""


def _observation_logger(kind):
    from loguru import logger
    def mark(record):
        record['message'] = f'[neutral-{kind}] ' + record['message']
    return logger.bind(**{f'neutral_{kind}': True}).patch(mark)


def _read_overlay(function, *args):
    """Run existing overlay code against copied mutable globals, without global swaps.

    Known overlay dependencies are module-owned plain functions; wrapped callables
    cannot safely share their hidden cache with capture, so refuse their evidence.
    """
    try:
        if not isinstance(function, FunctionType) or hasattr(function, '__wrapped__'):
            raise _OverlayIsolationError('unsupported overlay wrapper')
        original = function.__globals__
        isolated = dict(original)
        pending, visited = list(function.__code__.co_names), set()
        while pending:
            name = pending.pop()
            if name in visited:
                continue
            visited.add(name)
            dependency = original.get(name)
            if callable(dependency) and hasattr(dependency, '__wrapped__'):
                raise _OverlayIsolationError('unsupported overlay dependency wrapper')
            if isinstance(dependency, FunctionType) and dependency.__globals__ is original:
                pending.extend(dependency.__code__.co_names)
        for key, value in original.items():
            if key != '__builtins__' and isinstance(value, (dict, list, set)):
                isolated[key] = deepcopy(value)
            if isinstance(value, FunctionType) and value.__globals__ is original:
                clone = FunctionType(value.__code__, isolated, value.__name__,
                                     value.__defaults__, value.__closure__)
                clone.__kwdefaults__ = value.__kwdefaults__
                isolated[key] = clone
        if 'logger' in isolated:
            isolated['logger'] = _observation_logger('capture')
        entry = FunctionType(function.__code__, isolated, function.__name__,
                             function.__defaults__, function.__closure__)
        entry.__kwdefaults__ = function.__kwdefaults__
    except _OverlayIsolationError:
        raise
    except Exception as error:
        raise _OverlayIsolationError('overlay state could not be isolated') from error
    return entry(*args)


def capture_policy_sizing(risk_manager, event) -> dict[str, Any]:
    """Synchronously capture once before the LLM await; never mutate the signal.

    Failure raises to the observational caller, which must preserve live behavior.
    Deliberately allowlist sizing fields rather than copying a trading configuration.
    """
    from ..core.types import StrategyType
    from ..utils.calendar_seasonality import calendar_multiplier
    from ..utils.volatility_targeting import vol_targeting_multiplier
    from ..utils.team_conviction import team_conviction_multiplier
    from ..utils.fee_calculator import get_fee_calculator

    rm = risk_manager
    config = {key: getattr(rm.config, key) for key in _CONFIG_FIELDS}
    config['hybrid'] = {key: getattr(rm.config.hybrid, key) for key in _HYBRID_FIELDS}
    config = _json_copy(config)
    strategy = event.strategy.value if event.strategy else None
    nested = event.signal.metadata if event.signal is not None else None
    event_meta = event.metadata or {}
    # The engine reads the nested multiplier, even when the event has its own.
    event_mult = _number(event_meta.get('position_multiplier', 1.0))
    nested_mult = _number((nested or {}).get('position_multiplier', 1.0))
    atr = (nested or {}).get('atr_pct')
    if atr is not None:
        atr = _number(atr)
    state = {
        'equity': _money(rm.engine.portfolio.total_equity),
        'available_cash': _money(rm.engine.get_available_cash()),
        'reserved_cash': _money(rm._reserved_cash),
        'core_reserve': _money(rm._get_core_reserve()),
        'core_actual': _money(rm._get_core_actual_value()),
        'strategy_current': _money(rm.engine.portfolio.get_strategy_allocation(strategy)) if strategy else '0',
        'strategy_pending': _money(rm._pending_strategy_notional(strategy)) if strategy else '0',
        'effective_daily_pnl': _money(rm.engine.portfolio.effective_daily_pnl),
    }
    stop = {'status': 'not_required'}
    if config['sizing_mode'] == 'risk' and strategy != StrategyType.CORE_HOLDING.value:
        resolver = getattr(rm, '_resolve_entry_stop', None)
        if resolver is None:
            stop = {'status': 'missing'}
        else:
            try:
                resolved = resolver(strategy)
                stop = {'status': 'resolved', 'stop_pct': _money(resolved.stop_pct),
                        'source': resolved.source, 'crash_capped': bool(resolved.crash_capped)}
                if _number(stop['stop_pct']) <= 0:
                    raise ValueError('invalid stop')
            except Exception:
                stop = {'status': 'failed'}
    overlays = {}
    for key, call in (
        ('calendar', lambda: _read_overlay(calendar_multiplier, date.today(), 'kr')),
        ('volatility', lambda: _read_overlay(vol_targeting_multiplier, strategy or '')),
        ('team', lambda: _read_overlay(team_conviction_multiplier, event.symbol)),
    ):
        try:
            value, _ = call()
            overlays[key] = {'status': 'resolved', 'multiplier': _number(value)}
        except _OverlayIsolationError:
            raise
        except Exception:
            # Existing sizing skips an overlay on exception, not an inferred value.
            overlays[key] = {'status': 'failed', 'multiplier': None}
    snapshot = {
        'schema_version': SCHEMA_VERSION,
        'captured_at': datetime.now(timezone.utc).isoformat(),
        'capture_phase': 'pre_llm',
        'policy_source_sha256': policy_source_hash(),
        'config_sha256': _digest(config), 'config': config, 'state': state,
        'signal': {'signal_id': event.id, 'symbol': event.symbol, 'strategy': strategy,
                   'strength': event.strength.value, 'price': _money(event.price or 0),
                   'timestamp': event.timestamp.isoformat(),
                   'has_nested_signal': event.signal is not None,
                   'metadata_aliased': event.signal is not None and event.metadata is not None
                                       and event.metadata is event.signal.metadata,
                   'event_multiplier': event_mult, 'nested_multiplier': nested_mult, 'atr_pct': atr},
        'reject_multiplier': _number(rm._LLM_REJECT_SIZE_MULT),
        'stop': stop, 'overlays': overlays,
        'buy_commission_rate': _money(get_fee_calculator('KR').config.buy_commission_rate),
    }
    snapshot = _json_copy(snapshot)
    snapshot['snapshot_id'] = _digest(snapshot)
    return snapshot


def capture_policy_sizing_if_enabled(risk_manager, event):
    """Observation must never alter the live LLM verdict or sizing on failure."""
    try:
        from .llm_decision_record import get_decision_recorder
        if not get_decision_recorder().enabled:
            return None
        return capture_policy_sizing(risk_manager, event)
    except Exception:
        return {'schema_version': SCHEMA_VERSION, 'capture_status': 'unavailable',
                'reason': 'neutral_sizing_capture_failed'}


def replay_policy_sizing(snapshot, verdict: str) -> dict[str, Any]:
    """Return conditional policy quantity; invalid evidence raises ValueError.

    multiplier is a compatibility alias of input_multiplier. applied_multiplier is
    the value used after the risk-mode ATR exception, or None if sizing stopped first.
    """
    from ..core.engine import RiskManager
    from ..core.event import SignalEvent
    from ..core.types import StrategyType, SignalStrength
    from ..utils.fee_calculator import FeeCalculator, FeeConfig
    from ..utils.stop_policy import StopDecision

    try:
        data = _json_copy(snapshot)
        if not isinstance(data, dict) or data.get('schema_version') != SCHEMA_VERSION:
            raise ValueError('missing neutral sizing snapshot')
        snapshot_id = data.pop('snapshot_id')
        if snapshot_id != _digest(data) or data['capture_phase'] != 'pre_llm':
            raise ValueError('invalid neutral sizing snapshot fingerprint')
        if data['policy_source_sha256'] != policy_source_hash():
            raise ValueError('sizing policy source changed')
        if data['config_sha256'] != _digest(data['config']):
            raise ValueError('sizing configuration changed')
        if verdict not in ('approve', 'reject', 'unavailable'):
            raise ValueError('unknown verdict')
        stamp = datetime.fromisoformat(data['captured_at'])
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError('capture timestamp lacks timezone')
        config, state, sig = data['config'], data['state'], data['signal']
        for key in _CONFIG_FIELDS:
            if key not in config:
                raise ValueError('missing sizing configuration')
        if config['sizing_mode'] not in ('risk', 'nominal'):
            raise ValueError('invalid sizing mode')
        for key in _CONFIG_FIELDS:
            if key not in ('sizing_mode', 'strategy_allocation') and _number(config[key]) < 0:
                raise ValueError('negative sizing configuration')
        for value in config['strategy_allocation'].values():
            if _number(value) < 0:
                raise ValueError('negative strategy allocation')
        if type(config['hybrid']['enabled']) is not bool:
            raise ValueError('invalid hybrid flag')
        for key in _HYBRID_FIELDS:
            if key not in config['hybrid']:
                raise ValueError('missing hybrid configuration')
            if key != 'enabled' and _number(config['hybrid'][key]) < 0:
                raise ValueError('negative hybrid configuration')
        for value in state.values():
            _number(value)
        for key in ('event_multiplier', 'nested_multiplier', 'price'):
            if _number(sig[key]) < 0:
                raise ValueError('negative signal sizing input')
        if sig['atr_pct'] is not None:
            _number(sig['atr_pct'])
        if not 0 <= _number(data['reject_multiplier']) <= 1:
            raise ValueError('invalid reject multiplier')
        if type(sig['metadata_aliased']) is not bool:
            raise ValueError('invalid metadata alias flag')
        if sig['metadata_aliased'] and not sig['has_nested_signal']:
            raise ValueError('invalid metadata alias scope')
        if type(sig['has_nested_signal']) is not bool:
            raise ValueError('invalid nested signal flag')
        rm = object.__new__(RiskManager)
        rm.config = SimpleNamespace(**{**config, 'hybrid': SimpleNamespace(**config['hybrid'])})
        rm.engine = SimpleNamespace(
            portfolio=SimpleNamespace(total_equity=Decimal(state['equity']),
                effective_daily_pnl=Decimal(state['effective_daily_pnl']),
                get_strategy_allocation=lambda _: Decimal(state['strategy_current'])),
            get_available_cash=lambda: Decimal(state['available_cash']))
        rm._reserved_by_order = {'snapshot': Decimal(state['reserved_cash'])}
        rm._get_core_reserve = lambda: Decimal(state['core_reserve'])
        rm._get_core_actual_value = lambda: Decimal(state['core_actual'])
        rm._pending_strategy_notional = lambda _: Decimal(state['strategy_pending'])
        stop = data['stop']
        needs_stop = config['sizing_mode'] == 'risk' and sig['strategy'] != StrategyType.CORE_HOLDING.value
        if needs_stop == (stop['status'] == 'not_required'):
            raise ValueError('captured stop scope mismatch')
        if stop['status'] == 'resolved':
            pct = Decimal(stop['stop_pct'])
            if not pct.is_finite() or pct <= 0:
                raise ValueError('invalid captured stop')
            rm._resolve_entry_stop = lambda _: StopDecision(pct, stop['source'], stop['crash_capped'])
        elif stop['status'] == 'failed':
            def failed_stop(_):
                raise ValueError('captured stop resolution failed')
            rm._resolve_entry_stop = failed_stop
        elif stop['status'] not in ('missing', 'not_required'):
            raise ValueError('invalid captured stop status')
        scale = _number(data['reject_multiplier']) if verdict == 'reject' else 1.0
        nested_multiplier = sig['nested_multiplier'] * scale
        if sig['metadata_aliased']:
            nested_multiplier *= scale
        event = SignalEvent(id=sig['signal_id'], symbol=sig['symbol'],
            strategy=StrategyType(sig['strategy']) if sig['strategy'] else None,
            strength=SignalStrength(sig['strength']), price=Decimal(sig['price']),
            timestamp=datetime.fromisoformat(sig['timestamp']),
            metadata={'position_multiplier': sig['event_multiplier'] * scale},
            signal=(SimpleNamespace(metadata={'position_multiplier': nested_multiplier,
                                              'atr_pct': sig['atr_pct']})
                    if sig['has_nested_signal'] else None))
        overlays = {}
        for key in ('calendar', 'volatility', 'team'):
            overlay = data['overlays'][key]
            if overlay['status'] == 'resolved':
                overlays[key] = _number(overlay['multiplier'])
            elif overlay['status'] == 'failed' and overlay['multiplier'] is None:
                overlays[key] = None
            else:
                raise ValueError('invalid captured overlay')
        fee = FeeCalculator(FeeConfig(buy_commission_rate=Decimal(data['buy_commission_rate'])))
        if _number(fee.config.buy_commission_rate) < 0:
            raise ValueError('negative fee rate')
        quantity = rm._calculate_position_size(event, sizing_overlays=overlays,
                                               sizing_fee_calc=fee, record_entry_risk=False,
                                               sizing_logger=_observation_logger('replay'))
        input_multiplier = nested_multiplier if sig['has_nested_signal'] else 1.0
        result = {'snapshot_id': snapshot_id, 'verdict': verdict, 'quantity': quantity,
                  # Compatibility alias; applied_multiplier can differ in risk mode.
                  'multiplier': input_multiplier, 'input_multiplier': input_multiplier,
                  'applied_multiplier': event.metadata['size_trace'].get('position_multiplier'),
                  'trace': event.metadata['size_trace'], 'admissibility_scope': 'sizing_only',
                  'executable': False, 'economic_evaluable': False,
                  'exclusions': ['downstream_admission_not_replayed']}
        result['replay_id'] = _digest(result)
        return result
    except (KeyError, TypeError, AttributeError, ArithmeticError, OverflowError) as error:
        raise ValueError('invalid neutral sizing snapshot') from error
