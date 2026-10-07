"""Bounded, opt-in observations of actual live-screening decisions. No policy evaluation."""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import math
import re

VERSION = 'entry-gate-trace-v1'
POLICY_REF = 'kr-live-screening-gates-v1'
STAGES = tuple(('enabled session engine broker entry_time regime cash score excluded cooldown '
    'daily_count scan_change strategy batch_limit signal_limit sector quote_fetch quote_price '
    'rt_min_change rt_max_change open_price volume momentum_volume momentum_strength rsi '
    'early_supply news atr risk_reward chase signal_created').split())
STEP_FIELDS = frozenset('stage status observed_at reason value threshold'.split())
TRACE_FIELDS = frozenset(('candidate_id symbol observed_at trace_version policy_ref outcome '
                         'terminal_reason signal_id steps').split())
SCAN_FIELDS = frozenset(('entry_gate_trace_expected entry_gate_trace_version entry_gate_policy_ref '
                        'entry_gate_source_version_ref entry_gate_configuration_ref').split())


def _now():
    return datetime.now(timezone.utc).isoformat()


def validate_settings(value):
    keys = {'version','policy_ref','max_candidates','source_version_ref','configuration_ref'}
    if (type(value) is not dict or set(value) != keys or value['version'] != VERSION
            or value['policy_ref'] != POLICY_REF or type(value['max_candidates']) is not int
            or not 1 <= value['max_candidates'] <= 100):
        raise ValueError('entry gate trace settings invalid')
    for key in ('source_version_ref','configuration_ref'):
        if type(value[key]) is not str or not value[key].strip() or len(value[key]) > 200:
            raise ValueError('explicit bounded source/configuration reference required')
    return dict(value)


def scan_fields(settings):
    return dict(entry_gate_trace_expected=True, entry_gate_trace_version=VERSION,
        entry_gate_policy_ref=POLICY_REF, entry_gate_source_version_ref=settings['source_version_ref'],
        entry_gate_configuration_ref=settings['configuration_ref'])


def _code(value):
    if value is not None and (type(value) is not str or re.fullmatch(r'[a-z][a-z0-9_]{0,79}',value) is None):
        raise ValueError('fixed reason code required')
    return value


def _scalar(value):
    if value is None or type(value) is bool: return value
    if type(value) is int and value.bit_length()<=256: return value
    if type(value) is float: return value if math.isfinite(value) else None
    if isinstance(value,Decimal): return str(value) if value.is_finite() else None
    if type(value) is str and len(value) <= 200: return value
    raise ValueError('bounded scalar evidence required')


class _NullTrace:
    def check(self, stage, result, **kwargs): return result
    def note(self, *args, **kwargs): pass
    def stop(self, *args, **kwargs): pass
    def signal(self, *args, **kwargs): pass
    def finish(self, *args, **kwargs): pass


NULL_TRACE = _NullTrace()


def safe_trace_call(trace, method, *args, **kwargs):
    """Never use the return value to decide trading; an injected observer may also fail."""
    try:
        getattr(trace, method)(*args, **kwargs)
    except Exception:
        try: trace._invalidate()
        except Exception: pass
    return None


class GateTrace:
    def __init__(self, observer, scan):
        self.observer = observer
        self.rows = {c['symbol']: dict(candidate_id=c['candidate_id'],symbol=c['symbol'],
            trace_version=VERSION,policy_ref=POLICY_REF,outcome=None,terminal_reason=None,
            signal_id=None,steps=[]) for c in scan['candidates']}
        self.finished = False

    def _invalidate(self):
        try: self.observer.mark_incomplete('entry_gate_trace_invalid')
        except Exception: pass

    def _targets(self, symbol):
        if symbol is None: return [r for r in self.rows.values() if r['outcome'] is None]
        if symbol not in self.rows: raise ValueError('unlinked gate candidate')
        return [self.rows[symbol]] if self.rows[symbol]['outcome'] is None else []

    def _append(self, row, stage, status, *, value=None, threshold=None, reason=None):
        if (stage not in STAGES or status not in ('pass','fail','unknown','not_applicable')
                or (row['steps'] and STAGES.index(stage) <= STAGES.index(row['steps'][-1]['stage']))):
            raise ValueError('gate stage/status/order invalid')
        row['steps'].append(dict(stage=stage,status=status,observed_at=_now(),reason=_code(reason),
                                 value=_scalar(value),threshold=_scalar(threshold)))
        if status == 'fail':
            row.update(outcome='blocked',terminal_reason=reason or stage)

    def check(self, stage, result, **kwargs):
        if type(result) is bool: self.note(stage,'pass' if result else 'fail',**kwargs)
        else: self.note(stage,'unknown',**kwargs)
        return result

    def note(self, stage, status, *, symbol=None, value=None, threshold=None, reason=None):
        if self.finished: return
        try:
            for row in self._targets(symbol):
                self._append(row,stage,status,value=value,threshold=threshold,reason=reason)
        except Exception: self._invalidate()

    def stop(self, symbol, reason, *, stage=None, outcome='not_reached'):
        if self.finished: return
        try:
            if outcome not in ('not_reached','unknown'): raise ValueError('invalid stop outcome')
            reason=_code(reason)
            if reason is None: raise ValueError('stop reason required')
            for row in self._targets(symbol):
                # Reaching a stop is not evaluating the skipped policy predicate.
                if stage is not None:
                    self._append(row,stage,'unknown',reason=reason)
                row.update(outcome=outcome,terminal_reason=reason)
        except Exception: self._invalidate()

    def signal(self, symbol, signal_id):
        if self.finished: return
        try:
            if type(signal_id) is not str or not signal_id.strip() or len(signal_id)>200:
                raise ValueError('signal event identity required')
            for row in self._targets(symbol):
                self._append(row,'signal_created','pass')
                row.update(outcome='signal_created',terminal_reason='signal_created',signal_id=signal_id)
        except Exception: self._invalidate()

    def finish(self, reason='scope_finished'):
        if self.finished: return
        self.finished=True
        try:
            reason=_code(reason)
            if reason is None: raise ValueError('finish reason required')
            for row in self.rows.values():
                if row['outcome'] is None: row.update(outcome='unknown',terminal_reason=reason)
                record={'kind':'entry_gate_trace',**deepcopy(row),'observed_at':_now()}
                try:
                    if self.observer.publish(record) is not True:
                        self.observer.mark_incomplete('entry_gate_trace_publish_failed')
                except Exception: self._invalidate()
        except Exception: self._invalidate()


def begin_gate_trace(observer, scan_id, stocks):
    """Bind to the already captured cohort, never rebuild it from mutable screener results."""
    from .entry_observation import EntryObservationBuffer
    try:
        if (not isinstance(observer,EntryObservationBuffer) or observer.entry_gate_trace_settings is None
                or observer._capture_closed or scan_id is None
                or (observer.scan_scope == 'first' and observer._entry_gate_trace_started)
                or scan_id in observer._window_traces_started):
            return NULL_TRACE
        scans=[r for r in observer._records if r.get('kind')=='scan' and r.get('scan_id')==scan_id]
        if len(scans)!=1: return NULL_TRACE
        observer._entry_gate_trace_started=True
        if observer.scan_scope == 'window': observer._window_traces_started.add(scan_id)
        scan=scans[0];candidates=scan['candidates']
        if len(candidates)>observer.entry_gate_trace_settings['max_candidates']:
            raise ValueError('gate candidate bound exceeded')
        symbols=[c['symbol'] for c in candidates]
        if (len(set(symbols))!=len(symbols) or any(type(s) is not str or len(s)>200 for s in symbols)
                or any(c['candidate_id']!=f'{scan_id}:{c["symbol"]}' for c in candidates)):
            raise ValueError('gate candidate identities invalid')
        return GateTrace(observer,scan)
    except Exception:
        try: observer.mark_incomplete('entry_gate_trace_invalid')
        except Exception: pass
        return NULL_TRACE
