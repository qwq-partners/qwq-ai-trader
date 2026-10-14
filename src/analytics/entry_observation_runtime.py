"""명시 연구 파일로만 설치하는 유한 Runner 관측. 주문 없는 실행 모드는 아니다."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, time, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import stat

from .entry_observation import EntryObservationBuffer
from .entry_observation_journal import ObservationJournal, MAX_LINE_BYTES, _unique_keys, _reject_constant
from .entry_price_shadow import build_report, _timestamp, KST


READY_VERSION = 'runner-order-ready-window-v1'
LLM_VERSION = 'runner-llm-window-v1'
LLM_RETAINED_MEMORY_LIMIT_BYTES = 384 * 1024 * 1024
WINDOW_VERSIONS = ('runner-signal-window-v1', READY_VERSION, LLM_VERSION)


def _now():
    return datetime.now(timezone.utc)


def _read(path):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('명시 절대 경로 필요')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('일반 파일 필요')
        raw = stream.read(262145)
    if len(raw) > 262144:
        raise ValueError('연구/manifest 크기 한도 초과')
    return raw


def _decode(raw):
    value = json.loads(raw, object_pairs_hook=_unique_keys, parse_constant=_reject_constant)
    if type(value) is not dict:
        raise ValueError('JSON 객체 필요')
    return value


def validate_window_contract(context, observations=None):
    """Offline as well as install-time binding; never reopen or amend a study."""
    s = context.get('capture', {})
    rest_opt_in = 'rest_market_state_evidence' in s
    if rest_opt_in and (s.get('version') != LLM_VERSION or s['rest_market_state_evidence'] is not True):
        raise ValueError('REST 상태 근거는 명시 LLM 창의 true 계약만 허용')
    if observations is not None:
        for record in observations['records']:
            if 'market_state_snapshot' in record or 'rest_market_state_status' in record:
                if not rest_opt_in or record.get('kind') != 'rest_quote':
                    raise ValueError('REST 상태 레코드는 명시 관측 계약 필요')
    if observations is not None and any(r.get('kind') == 'market_state' for r in observations['records']):
        channels = s.get('channels', {})
        if type(channels) is not dict or channels.get('market_state_evidence') is not True:
            raise ValueError('market-state records require explicit capture contract')
    scans = ([r for r in observations['records'] if r.get('kind') == 'scan']
             if observations is not None else [])
    if observations is not None and s.get('version') != LLM_VERSION:
        if any(r.get('kind') in ('llm_check_started', 'llm_check_admission')
                or 'allocation_signal_id' in r or str(r.get('reason', '')).startswith('llm_check_')
                for r in observations['records']):
            raise ValueError('LLM admission requires explicit study version')
    if observations is not None and s.get('version') not in (READY_VERSION, LLM_VERSION):
        if any(r.get('kind') == 'order_ready_admission' or (r.get('kind') == 'quote_subscription'
                and ('signal_id' in r or 'order_id' in r
                or str(r.get('reason', '')).startswith('order_ready_'))) for r in observations['records']):
            raise ValueError('order-ready identity requires explicit study version')
    if s.get('version') not in WINDOW_VERSIONS:
        if any(r.get('population_scope') == 'window_returned_scan_candidates' for r in scans):
            raise ValueError('window population requires explicit window study')
        return
    if (type(s.get('max_scans')) is not int or not 1 <= s['max_scans'] <= 100
            or s.get('quote_admission') != ({READY_VERSION: 'order_ready', LLM_VERSION: 'llm_check_started'}.get(s.get('version'), 'signal_created'))
            or context.get('outcome_basis') != 'fixed_horizon_bid_markout'):
        raise ValueError('bounded signal-window contract required')
    if s.get('version') in (READY_VERSION, LLM_VERSION):
        channels = s.get('channels', {})
        channel_fields = set(('registration_cap external_reserved operational_headroom '
                              'max_candidates lease_seconds evidence_ref').split())
        if type(channels) is not dict or set(channels) - {'market_state_evidence', 'market_state_max_records'} != channel_fields:
            raise ValueError('H1 bounded channel contract required')
        from src.data.feeds.quote_subscription import QuoteSubscriptionCoordinator
        QuoteSubscriptionCoordinator(EntryObservationBuffer(evaluation_epoch=context.get('evaluation_epoch'), capacity=1),
                                     **channels)
        if (channels.get('max_candidates') != 5 or type(channels.get('max_candidates')) is not int
                or channels.get('registration_cap') != 41 or channels.get('operational_headroom') != 10):
            raise ValueError('H1 order-ready requires first five / 41 channels / 10 headroom')
    from .entry_capital_limits import CapitalPolicy
    from .entry_markout import MarkoutPolicy
    from .entry_gate_trace import validate_settings as gate_settings, scan_fields
    from .selection_basis import validate_settings as selection_settings
    capital = CapitalPolicy.from_dict(context.get('capital_policy'))
    markout = MarkoutPolicy.from_dict(context.get('markout_policy'))
    gate = gate_settings(s.get('entry_gate_trace'))
    selection = selection_settings(s.get('selection_basis'))
    start, admission, end = (_timestamp(s.get(k), k) for k in ('start_at', 'admission_end_at', 'end_at'))
    if (not max(capital.fixed_at, markout.fixed_at) <= start < admission < end
            or end != _timestamp(context.get('as_of'), 'as_of')
            or (end - admission).total_seconds() <= markout.horizon_seconds
            or capital.source_version_ref != s.get('source_version_ref')
            or capital.configuration_ref != s.get('configuration_ref')):
        raise ValueError('window time/capital policy binding mismatch')
    if s.get('version') in (READY_VERSION, LLM_VERSION) and s['channels']['lease_seconds'] < (end - start).total_seconds():
        raise ValueError('order-ready lease must span capture window')
    if s.get('version') == LLM_VERSION:
        _validate_llm_capacity(s)
    if observations is not None:
        # Seal/audit time may be later; it never extends the captured market window.
        # Prestart subscription ACKs remain valid evidence of an existing connection.
        for record in observations['records']:
            observed = _timestamp(record.get('observed_at'), 'window record observed_at')
            if s.get('version') in (READY_VERSION, LLM_VERSION) and (observed >= end or (observed < start
                    and record.get('kind') != 'quote_subscription')):
                raise ValueError('order-ready record outside half-open capture window')
            if observed > end:
                raise ValueError('window record observed_at after capture.end_at')
        if len(scans) > s['max_scans']:
            raise ValueError('window scan count exceeds study bound')
        for r in scans:
            if (r.get('population_scope') != 'window_returned_scan_candidates'
                    or r.get('scan_admission_ref') != s.get('scan_admission_ref')
                    or not start <= _timestamp(r.get('observed_at'), 'scan') < admission
                    or any(r.get(k) != v for k, v in scan_fields(gate).items())
                    or r.get('selection_basis_expected') is not True
                    or r.get('selection_basis_max_candidates') != selection['max_candidates']
                    or r.get('selection_basis_max_terms') != selection['max_source_terms']):
                raise ValueError('window population/time binding mismatch')
        if s.get('version') == READY_VERSION:
            _validate_ready_records(observations['records'], start, admission, end)
        elif s.get('version') == LLM_VERSION:
            limits = {**s['record_allocations'], 'scan_metadata': _scan_records(s)}
            counts = dict.fromkeys(limits, 0)
            for record in observations['records']:
                category = _record_category(record)
                counts[category] += 1
                if counts[category] > limits[category]:
                    raise ValueError('LLM replay exceeds declared record allocation')
            _validate_llm_records(observations['records'], start, admission, end)


def _scan_records(s):
    return s['max_scans'] * (1 + s['selection_basis']['max_candidates']
                             + s['entry_gate_trace']['max_candidates'])


def _record_category(record):
    kind = record.get('kind')
    return ('scan_metadata' if kind in ('scan', 'selection_basis', 'entry_gate_trace')
            else kind if kind in ('ws_quote', 'market_state') else 'other')


def _validate_llm_capacity(s):
    allocations = s.get('record_allocations')
    if (type(allocations) is not dict or set(allocations) != {'ws_quote', 'market_state', 'other'}
            or any(type(v) is not int or v <= 0 for v in allocations.values())):
        raise ValueError('LLM explicit positive integer record allocations required')
    if (s['channels'].get('market_state_evidence') is not True
            or s['channels'].get('market_state_max_records') != allocations['market_state']):
        raise ValueError('LLM market-state allocation binding mismatch')
    total = _scan_records(s) + sum(allocations.values())
    if (any(type(s.get(k)) is not int or s[k] <= 0 for k in
            ('buffer_capacity', 'queue_capacity', 'max_bytes', 'max_record_bytes'))
            or s['buffer_capacity'] < total or s['queue_capacity'] < s['buffer_capacity']
            or s['max_bytes'] < s['buffer_capacity'] * (s['max_record_bytes'] + 2048) + 2048):
        raise ValueError('LLM quote/state/other/scan capacity sum insufficient')


def _llm_admission_record(record, signal, candidates, seen, symbols, start, admission, end):
    sid, symbol = record.get('signal_id'), record.get('symbol')
    if (signal is None or not isinstance(sid, str) or not sid
            or not isinstance(symbol, str) or len(symbol) != 6 or not symbol.isascii() or not symbol.isdigit()
            or not isinstance(signal.get('candidate_id'), str)
            or not signal['candidate_id'].endswith(':' + symbol)
            or _timestamp(signal.get('observed_at'), 'signal') > _timestamp(record.get('observed_at'), 'LLM')):
        raise ValueError('LLM missing prior signal identity')
    if sid in seen:
        reason = 'llm_check_duplicate'
    elif (record.get('source') != 'live_screening'
            or candidates.get(signal['candidate_id']) != 'live_screening'
            or record.get('strategy') != signal.get('strategy')):
        reason = 'llm_check_outside_scope'
    elif not start <= _timestamp(record['observed_at'], 'LLM') < admission:
        reason = 'llm_check_admission_closed'
    elif symbol in symbols:
        reason = 'llm_check_reused'
    elif len(symbols) >= 5:
        reason = 'llm_check_limit'
    else:
        reason = 'llm_check_admitted'
    allocated = reason in ('llm_check_admitted', 'llm_check_reused')
    return dict(kind='llm_check_admission', observed_at=record['observed_at'],
        signal_id=sid, candidate_id=signal['candidate_id'], symbol=symbol, tr_id='H0STASP0',
        reason=reason, status='admitted' if allocated else 'unallocated',
        allocation_signal_id=(symbols.get(symbol, sid) if allocated else None),
        expires_at=end.isoformat())


def _validate_llm_records(records, start, admission, end):
    candidates, signals, symbols, admitted, seen = {}, {}, {}, {}, set()
    pending = None
    for record in records:
        kind = record.get('kind')
        if pending is not None:
            if any(record.get(k) != v for k, v in pending.items()):
                raise ValueError('LLM admission identity/order mismatch')
            seen.add(record['signal_id'])
            if record['reason'] == 'llm_check_admitted':
                symbols[record['symbol']] = record['signal_id']
                admitted[record['signal_id']] = record
            pending = None
            continue
        if kind == 'scan':
            candidates.update({c['candidate_id']: record.get('route_origin') for c in record.get('candidates', [])})
        elif kind == 'signal':
            sid = record.get('signal_id')
            if not isinstance(sid, str) or not sid or record.get('candidate_id') not in candidates:
                raise ValueError('LLM signal identity missing')
            if sid in signals and any(signals[sid].get(k) != record.get(k) for k in
                    ('candidate_id', 'strategy', 'price', 'event_timestamp', 'signal_target_price', 'signal_stop_price')):
                raise ValueError('LLM conflicting signal identity')
            signals.setdefault(sid, record)
        elif kind == 'llm_check_started':
            pending = _llm_admission_record(record, signals.get(record.get('signal_id')), candidates,
                                            seen, symbols, start, admission, end)
        elif kind in ('llm_check_admission', 'order_ready_admission'):
            raise ValueError('LLM unexpected admission record')
        elif kind == 'quote_subscription':
            if record.get('status') == 'admitted' or 'order_id' in record:
                raise ValueError('LLM unexpected lease identity')
            if record.get('candidate_id') is not None or 'signal_id' in record or 'allocation_signal_id' in record:
                identity = admitted.get(record.get('allocation_signal_id'))
                if (identity is None or any(record.get(k) != identity.get(k) for k in
                        ('candidate_id', 'signal_id', 'allocation_signal_id', 'symbol', 'tr_id'))
                        or _timestamp(record.get('observed_at'), 'lease') < _timestamp(identity['observed_at'], 'LLM')
                        or (record.get('expires_at') is not None and _timestamp(record['expires_at'], 'expiry') != end)):
                    raise ValueError('LLM lease identity/time mismatch')
    if pending is not None:
        raise ValueError('LLM admission record missing')


def _validate_ready_records(records, start, admission, end):
    """배정 순서를 원장으로 다시 계산하며 호가 가격은 사용하지 않는다."""
    candidates, signals, admitted, seen = {}, {}, {}, set()
    pending = None
    for record in records:
        kind = record.get('kind')
        if pending is not None:
            expected, reason, signal = pending
            if (kind != 'order_ready_admission' or record.get('reason') != reason
                    or record.get('status') != ('admitted' if reason == 'order_ready_admitted' else 'unallocated')
                    or any(record.get(k) != expected.get(k) for k in ('signal_id', 'order_id', 'symbol', 'observed_at'))
                    or record.get('candidate_id') != signal.get('candidate_id')
                    or record.get('tr_id') != 'H0STASP0'
                    or _timestamp(record.get('expires_at'), 'admission expiry') != end):
                raise ValueError('order-ready admission identity/order mismatch')
            if reason == 'order_ready_admitted':
                admitted[record['signal_id']] = record
            pending = None
            continue
        if kind == 'scan':
            for candidate in record.get('candidates', []):
                candidates[candidate['candidate_id']] = record.get('route_origin')
        elif kind == 'signal':
            sid = record.get('signal_id')
            if not isinstance(sid, str) or not sid or record.get('candidate_id') not in candidates:
                raise ValueError('order-ready signal identity missing')
            if sid in signals and any(signals[sid].get(k) != record.get(k) for k in (
                    'candidate_id', 'strategy', 'price', 'signal_target_price', 'signal_stop_price', 'event_timestamp')):
                raise ValueError('order-ready conflicting signal identity')
            signals.setdefault(sid, {k: v for k, v in record.items() if k != 'sequence'})
        elif kind == 'order_ready':
            signal = signals.get(record.get('signal_id'))
            if (signal is None or not isinstance(record.get('order_id'), str) or not record['order_id']
                    or _timestamp(signal.get('observed_at'), 'signal') > _timestamp(record.get('observed_at'), 'ready')):
                raise ValueError('order-ready missing prior signal/order identity')
            reason = _ready_reason(record, signal, candidates, seen, len(admitted), start, admission)
            seen.add(record['signal_id'])
            seen.add(('order', record.get('order_id')))
            pending = record, reason, signal
        elif kind == 'order_ready_admission':
            raise ValueError('order-ready admission without preceding order')
        elif kind == 'quote_subscription':
            if record.get('status') == 'admitted':
                raise ValueError('order-ready admission without preceding order')
            if record.get('candidate_id') is not None or 'signal_id' in record or 'order_id' in record:
                identity = admitted.get(record.get('signal_id'))
                if (identity is None or any(record.get(k) != identity.get(k)
                        for k in ('candidate_id', 'signal_id', 'order_id', 'symbol', 'tr_id'))
                        or _timestamp(record.get('observed_at'), 'lease') < _timestamp(identity['observed_at'], 'admitted')
                        or (record.get('expires_at') is not None
                            and _timestamp(record['expires_at'], 'lease expiry') != end)):
                    raise ValueError('order-ready lease identity/time mismatch')
    if pending is not None:
        raise ValueError('order-ready admission record missing')


def _ready_reason(record, signal, candidates, seen, admitted, start, admission):
    if record.get('signal_id') in seen or ('order', record.get('order_id')) in seen:
        return 'order_ready_duplicate'
    if (not isinstance(signal.get('candidate_id'), str)
            or signal.get('strategy') != 'gap_and_go' or record.get('strategy') != 'gap_and_go'
            or candidates.get(signal.get('candidate_id')) != 'live_screening'
            or not signal.get('candidate_id', '').endswith(':' + str(record.get('symbol')))):
        return 'order_ready_outside_scope'
    if type(record.get('requested_quantity')) is not int or record['requested_quantity'] <= 0:
        return 'order_ready_invalid_quantity'
    if not start <= _timestamp(record.get('observed_at'), 'order_ready') < admission:
        return 'order_ready_admission_closed'
    if admitted >= 5:
        return 'order_ready_limit'
    return 'order_ready_admitted'


@dataclass(frozen=True)
class CapturePlan:
    study_path: Path
    study_sha256: str
    study_bytes: bytes

    @property
    def context(self):
        return _decode(self.study_bytes)

    @property
    def settings(self):
        return self.context['capture']

    @property
    def start_at(self):
        return _timestamp(self.settings['start_at'], 'start_at')

    @property
    def end_at(self):
        return _timestamp(self.settings['end_at'], 'end_at')

    @classmethod
    def load(cls, path, *, now=None):
        envelope = _decode(_read(path))
        if set(envelope) != {'study_path', 'study_sha256'}:
            raise ValueError('manifest에는 study_path/study_sha256만 허용')
        raw = _read(envelope['study_path'])
        digest = hashlib.sha256(raw).hexdigest()
        if digest != envelope['study_sha256']:
            raise ValueError('연구 파일 지문 불일치')
        plan = cls(Path(envelope['study_path']), digest, raw)
        plan.validate(now=now or _now())
        return plan

    def validate(self, *, now):
        if (hashlib.sha256(self.study_bytes).hexdigest() != self.study_sha256
                or _read(self.study_path) != self.study_bytes):
            raise ValueError('설치 전 연구 파일 변경')
        context = self.context
        from .market_state_pilot import PILOT_VERSION, validate_pilot_context
        if isinstance(context.get('capture'), dict) and context['capture'].get('version') == PILOT_VERSION:
            validate_pilot_context(context, study_path=self.study_path, now=now)
            return
        if 'opportunities' in context and (type(context['opportunities']) is not list or context['opportunities']):
            raise ValueError('미래 수집 연구에는 기존 결과를 넣지 않는다')
        build_report({**context, 'opportunities': []})
        if 'capital_policy' in context:
            from .entry_capital_limits import CapitalPolicy
            if CapitalPolicy.from_dict(context['capital_policy']).fixed_at > now:
                raise ValueError('자본 정책은 수집 설치 전에 고정해야 한다')
        if context.get('data_basis') != 'received_snapshot':
            raise ValueError('Runner 수집은 received_snapshot 전용')
        s = self.settings
        expected = set(('version study_ref start_at admission_end_at end_at scan_admission_ref '
            'source_version_ref configuration_ref fee_evidence_ref capital_evidence_ref capacity_evidence_ref '
            'journal_path buffer_capacity queue_capacity batch_size max_bytes max_record_bytes '
            'open_timeout_seconds close_timeout_seconds channels').split())
        if type(s) is dict and s.get('version') in ('runner-first-scan-v2','runner-first-scan-v3','runner-first-scan-v4', 'runner-signal-window-v1', 'runner-order-ready-window-v1', LLM_VERSION):
            expected.add('selection_basis')
        if type(s) is dict and s.get('version') in ('runner-first-scan-v3', 'runner-first-scan-v4', 'runner-signal-window-v1', 'runner-order-ready-window-v1', LLM_VERSION):
            expected.add('entry_gate_trace')
        if type(s) is dict and s.get('version') in ('runner-first-scan-v4', 'runner-signal-window-v1', 'runner-order-ready-window-v1', LLM_VERSION):
            expected.add('frame_diagnostics')
        if type(s) is dict and s.get('version') in WINDOW_VERSIONS:
            expected.update(('max_scans', 'quote_admission'))
        if type(s) is dict and s.get('version') == LLM_VERSION:
            expected.add('record_allocations')
            if 'rest_market_state_evidence' in s:
                expected.add('rest_market_state_evidence')
        if (type(s) is not dict or set(s) != expected
                or s['version'] not in ('runner-first-scan-v1', 'runner-first-scan-v2', 'runner-first-scan-v3', 'runner-first-scan-v4', 'runner-signal-window-v1', 'runner-order-ready-window-v1', LLM_VERSION)):
            raise ValueError('지원하지 않는 capture 계약/필드')
        if s['version'] in ('runner-first-scan-v4', 'runner-signal-window-v1', 'runner-order-ready-window-v1', LLM_VERSION):
            from .kis_frame_diagnostics import validate_settings as validate_frame_settings
            validate_frame_settings(s['frame_diagnostics'])
        if s['version'] in WINDOW_VERSIONS:
            validate_window_contract(context)
        for key in ('study_ref', 'scan_admission_ref', 'source_version_ref', 'configuration_ref',
                    'fee_evidence_ref', 'capital_evidence_ref', 'capacity_evidence_ref'):
            if not isinstance(s[key], str) or not s[key].strip() or len(s[key]) > 200:
                raise ValueError('제한된 명시 근거 참조 필요')
        start, end = self.start_at, self.end_at
        admission_end = _timestamp(s['admission_end_at'], 'admission_end_at')
        if not now < start < admission_end < end or end != _timestamp(context['as_of'], 'as_of'):
            raise ValueError('설치/첫 스캔/종료/as_of 시각 불일치')
        local_start, local_end = start.astimezone(KST), end.astimezone(KST)
        if (local_start.date() != local_end.date() or local_start.weekday() >= 5
                or not time(9) <= local_start.time() < local_end.time() < time(15, 20)):
            raise ValueError('동일 평일 KRX 정규장 범위 필요; 실제 개장 증거는 별도')
        if context.get('outcome_basis') == 'fixed_horizon_bid_markout':
            from .entry_markout import MarkoutPolicy
            m = MarkoutPolicy.from_dict(context['markout_policy'])
            if m.fixed_at > now or (end - admission_end).total_seconds() <= m.horizon_seconds:
                raise ValueError('사전 고정/보유시간 범위 위반')
        p = Path(s['journal_path'])
        if not p.is_absolute() or p == self.study_path:
            raise ValueError('별도 절대 신규 원장 경로 필요')
        if p.exists() or p.is_symlink():
            raise FileExistsError('관측 원장 경로 재사용 금지')
        for key in ('buffer_capacity', 'queue_capacity', 'batch_size', 'max_bytes', 'max_record_bytes'):
            if type(s[key]) is not int or s[key] <= 0:
                raise ValueError('자원 한도는 양의 정수 필요')
        if s['batch_size'] > s['queue_capacity'] or not 1 <= s['max_record_bytes'] <= MAX_LINE_BYTES-2048:
            raise ValueError('저장 batch/레코드 한도 위반')
        metadata_records = 1
        if s['version'] in ('runner-first-scan-v2', 'runner-first-scan-v3', 'runner-first-scan-v4', 'runner-signal-window-v1', 'runner-order-ready-window-v1', LLM_VERSION):
            from .selection_basis import validate_settings
            selection = validate_settings(s['selection_basis'])
            # 스캔과 후보별 근거의 즉시 발생량을 최소 예산으로 예약한다.
            # 호가/주문 등 전체 구간 유량은 기존 capacity_evidence_ref로 별도 확인한다.
            burst = 1 + selection['max_candidates']
            record_minimum = 65536 if selection['version'] == 'selection-basis-v2' else 32768
            if s['version'] in ('runner-first-scan-v3', 'runner-first-scan-v4', 'runner-signal-window-v1', 'runner-order-ready-window-v1', LLM_VERSION):
                from .entry_gate_trace import validate_settings as validate_gate_settings
                gate = validate_gate_settings(s['entry_gate_trace'])
                if (gate['source_version_ref'] != s['source_version_ref']
                        or gate['configuration_ref'] != s['configuration_ref']
                        or gate['max_candidates'] < selection['max_candidates']):
                    raise ValueError('진입 관측 소스/설정/후보 범위 불일치')
                burst += gate['max_candidates']
                record_minimum = 65536
            metadata_records = burst * (s['max_scans'] if s['version'] in WINDOW_VERSIONS else 1)
            if (s['buffer_capacity'] < metadata_records or s['queue_capacity'] < burst
                    or s['max_record_bytes'] < record_minimum
                    or s['max_bytes'] < metadata_records * (s['max_record_bytes'] + 2048) + 2048):
                raise ValueError('선정 근거의 최소 저장 예산 부족')
        for key in ('open_timeout_seconds', 'close_timeout_seconds'):
            if type(s[key]) not in (int, float) or not math.isfinite(s[key]) or not 0 < s[key] <= 10:
                raise ValueError('I/O 대기 한도는 0~10초')
        contract = s['channels']
        if type(contract) is not dict or set(contract) - {'market_state_evidence', 'market_state_max_records'} != set(('registration_cap external_reserved '
                'operational_headroom max_candidates lease_seconds evidence_ref').split()):
            raise ValueError('명시 채널 계약 필요; 해제 ACK 승격은 지원하지 않음')
        from src.data.feeds.quote_subscription import QuoteSubscriptionCoordinator
        QuoteSubscriptionCoordinator(EntryObservationBuffer(evaluation_epoch=context['evaluation_epoch'], capacity=1),
                                     **contract)
        if contract.get('market_state_evidence'):
            # 전체 유한 버퍼가 한 번에 큐에 들어가도 상태 추가 때문에 writer가
            # 끊기지 않도록 보수적으로 예약한다. 실제 유량 충족은 파일럿 검증이다.
            if (s['buffer_capacity'] < metadata_records + contract['market_state_max_records']
                    or s['queue_capacity'] < s['buffer_capacity']
                    or s['max_bytes'] < s['buffer_capacity'] * (s['max_record_bytes'] + 2048) + 2048):
                raise ValueError('market-state 유한 메모리/큐/저장 예산 부족')
        if contract['lease_seconds'] < (end-start).total_seconds():
            raise ValueError('관측 종료까지 충분한 lease 필요')


class _WindowBuffer(EntryObservationBuffer):
    def __init__(self, plan, now):
        s = plan.settings
        super().__init__(evaluation_epoch=plan.context['evaluation_epoch'], capacity=s['buffer_capacity'],
                         scan_scope='window' if s['version'] in WINDOW_VERSIONS else 'first',
                         max_scans=s.get('max_scans'), scan_admission_ref=s['scan_admission_ref'],
                         selection_basis_settings=s.get('selection_basis'),
                         entry_gate_trace_settings=s.get('entry_gate_trace'),
                         frame_diagnostics_settings=s.get('frame_diagnostics'),
                         retained_memory_limit_bytes=(LLM_RETAINED_MEMORY_LIMIT_BYTES
                             if s['version'] == LLM_VERSION else None),
                         max_record_bytes=s['max_record_bytes'] if s['version'] == LLM_VERSION else None)
        self.rest_market_state_evidence = s.get('rest_market_state_evidence') is True
        self.llm_admission = s['version'] == LLM_VERSION
        self.llm_quote_submit = None
        self._llm_symbols = {}
        self._llm_seen = set()
        self._record_counts = {}
        self._record_limits = ({**s['record_allocations'], 'scan_metadata': _scan_records(s)}
                               if self.llm_admission else {})
        self.order_ready_admission = s['version'] == READY_VERSION
        self.order_ready_quote_submit = None
        self._ready_signals = {}
        self._ready_candidates = {}
        self._ready_seen = set()
        self._ready_admitted = 0
        self._now = now
        self.start_at, self.end_at = plan.start_at, plan.end_at
        self.admission_end = _timestamp(s['admission_end_at'], 'admission_end_at')
        self.ended = False
        self._last_clock = now()

    def current_time(self):
        now = self._now()
        if now < self._last_clock:
            self.mark_incomplete('capture_clock_regressed')
            self.ended = True
        self._last_clock = now
        if now >= self.end_at:
            self.ended = True
        return now

    @property
    def selection_capture_enabled(self):
        return (not self.ended and self.current_time() < self.admission_end
                and super().selection_capture_enabled)

    def begin_scan(self):
        now = self.current_time()
        if self.ended or not self.start_at <= now < self.admission_end:
            return None
        return super().begin_scan()

    def publish(self, record):
        if self._capture_closed:
            return False
        now = self.current_time()
        if self.ended:
            return False
        if not self._retention_input_allowed(record):
            return False
        # Window quotes start at the declared boundary; subscription ACKs and
        # gaps before it remain necessary evidence for the existing session.
        if self.scan_scope == 'window' and now < self.start_at and record.get('kind') in ('ws_quote', 'market_state'):
            return False
        # Original first-scan contracts retain their preparation-stage quotes.
        if self.llm_admission and record.get('kind') == 'llm_check_started':
            return self._publish_llm_check(record)
        category = _record_category(record)
        if self.llm_admission and self._record_counts.get(category, 0) >= self._record_limits[category]:
            self.mark_incomplete(category + '_record_limit')
            return False
        recorded = super().publish(record)
        if recorded and self.llm_admission:
            self._record_counts[category] = self._record_counts.get(category, 0) + 1
            try:
                if record.get('kind') == 'scan':
                    for candidate in record.get('candidates', []):
                        self._ready_candidates[candidate['candidate_id']] = record.get('route_origin')
                elif record.get('kind') == 'signal':
                    self._ready_signals.setdefault(record['signal_id'], dict(record))
            except Exception:
                self.mark_incomplete('llm_check_admission_failed')
        if recorded and self.order_ready_admission:
            try:
                if record.get('kind') == 'scan':
                    for candidate in record.get('candidates', []):
                        self._ready_candidates[candidate['candidate_id']] = record.get('route_origin')
                elif record.get('kind') == 'signal':
                    self._ready_signals.setdefault(record['signal_id'], dict(record))
                elif record.get('kind') == 'order_ready':
                    self._admit_order_ready(record)
            except Exception:
                self.mark_incomplete('order_ready_admission_failed')
        return recorded

    def _publish_llm_check(self, record):
        """시작/배정은 단일 event-loop 턴에서 검증하고 함께 기록한다. await 없음."""
        if self._capture_closed:
            return False
        try:
            signal = self._ready_signals.get(record.get('signal_id'))
            admission = _llm_admission_record(record, signal, self._ready_candidates,
                self._llm_seen, self._llm_symbols, self.start_at, self.admission_end, self.end_at)
            if not self.start_at <= _timestamp(record.get('observed_at'), 'LLM') < self.end_at:
                raise ValueError('LLM boundary timestamp outside capture')
            # 두 번째 publish에서 시계/한도를 다시 검사하면 고립된 started가 남는다.
            if self._record_counts.get('other', 0) + 2 > self._record_limits['other']:
                self.mark_incomplete('other_record_limit')
                return False
            if len(self._records) + 2 > self.capacity:
                self.mark_incomplete('llm_check_pair_buffer_limit')
                return False
            prepared = self._prepare_retained_records([record, admission])
            if prepared is None:
                return False
            pair, charge = prepared
            if self._journal_sink is not None and not self._journal_sink.offer_pair(pair):
                self.mark_incomplete('llm_check_pair_not_recorded')
                return False
            self._records.extend(pair)
            self.retained_memory_bytes += charge
            self._record_counts['other'] = self._record_counts.get('other', 0) + 2
        except Exception:
            self.mark_incomplete('llm_check_admission_failed')
            return False
        self._llm_seen.add(record['signal_id'])
        if admission['reason'] == 'llm_check_admitted':
            self._llm_symbols[record['symbol']] = record['signal_id']
            try:
                if self.llm_quote_submit is None:
                    self.mark_incomplete('llm_check_enrollment_unavailable')
                else:
                    self.llm_quote_submit(admission)
            except Exception:
                self.mark_incomplete('llm_check_enrollment_failed')
        return True

    def _admit_order_ready(self, record):
        # 저장된 준비 이벤트의 순서로만 예약한다. 호가/미래 가격은 읽지 않는다.
        signal = self._ready_signals.get(record.get('signal_id'), {})
        reason = _ready_reason(record, signal, self._ready_candidates, self._ready_seen,
                               self._ready_admitted, self.start_at, self.admission_end)
        self._ready_seen.add(record.get('signal_id'))
        self._ready_seen.add(('order', record.get('order_id')))
        admission = dict(kind='order_ready_admission', observed_at=record['observed_at'],
            status='admitted' if reason == 'order_ready_admitted' else 'unallocated',
            reason=reason, signal_id=record.get('signal_id'), order_id=record.get('order_id'),
            candidate_id=signal.get('candidate_id'), symbol=record.get('symbol'), tr_id='H0STASP0',
            expires_at=self.end_at.isoformat())
        if not self.publish(admission):
            self.mark_incomplete('order_ready_admission_not_recorded')
            return
        if reason != 'order_ready_admitted':
            return
        self._ready_admitted += 1
        try:
            if self.order_ready_quote_submit is None:
                self.mark_incomplete('order_ready_enrollment_unavailable')
            else:
                self.order_ready_quote_submit(admission)
        except Exception:
            self.mark_incomplete('order_ready_enrollment_failed')


class ObservationRuntime:
    @classmethod
    async def install(cls, bot, plan, *, now=_now):
        if bot.market not in ('kr', 'both') or bot.dry_run or bot.ws_feed is None:
            raise ValueError('KR 실제 피드가 준비된 Runner에서만 설치; 주문 없는 모드가 아님')
        feed = bot.ws_feed
        if (getattr(bot, '_entry_price_observer', None) is not None
                or getattr(bot, '_entry_observation_runtime', None) is not None
                or getattr(feed, '_quote_subscription_owner', None) is not None
                or getattr(feed, '_running', False) or getattr(feed, '_connected', False)):
            raise RuntimeError('기존 관측/실행 피드 덮어쓰기 금지')
        plan.validate(now=now())
        self = cls()
        self.bot, self.plan, self._now = bot, plan, now
        from .market_state_pilot import PILOT_VERSION, PilotBuffer
        pilot = plan.settings['version'] == PILOT_VERSION
        self.buffer = PilotBuffer(plan, now) if pilot else _WindowBuffer(plan, now)
        self.task = self._close_task = None
        self.result = None
        s = plan.settings
        self.journal = await ObservationJournal.open(self.buffer, s['journal_path'],
            study_ref=s['study_ref'], study_sha256=plan.study_sha256,
            **{k:s[k] for k in ('queue_capacity', 'batch_size', 'max_bytes', 'max_record_bytes', 'open_timeout_seconds')})
        sentinel = object()
        names = ('_quote_subscription_owner', '_quote_lifecycle_lock', '_quote_maintenance_task',
                 '_managed_data_count', '_subscribed_symbols', '_pending_subscriptions')
        before = {name: getattr(feed, name, sentinel) for name in names}
        sets = {name: set(value) for name, value in before.items() if isinstance(value, set)}
        try:
            # open 완료 경계에 이미 요청된 취소를 설치 공개 전에 전달한다.
            await asyncio.sleep(0)
            if now() >= plan.start_at or _read(plan.study_path) != plan.study_bytes:
                raise ValueError('원장 준비 중 시작 시각/연구 지문 변경')
            feed.enable_quote_observation(self.buffer, **s['channels'])
            if getattr(self.buffer, 'llm_admission', False) and self.buffer.rest_market_state_evidence:
                # Stopped feed only: measure existing demand before reporting readiness.
                # No candidate is selected and no socket/API is started here.
                await feed._apply_subscriptions()
            if pilot:
                # Feed is stopped: this updates local demand only, with no socket/API.
                await feed._apply_subscriptions()
                if not feed._quote_subscription_owner.install_static_lease(
                        s['pilot_symbol'], expires_at=plan.end_at):
                    # Optional technical capture cannot stop holding management.
                    for name, value in before.items():
                        if value is sentinel:
                            if hasattr(feed, name):
                                delattr(feed, name)
                        else:
                            if name in sets:
                                value.clear()
                                value.update(sets[name])
                            setattr(feed, name, value)
                    self.buffer.mark_incomplete('pilot_channel_capacity')
                    self.buffer.ended = True
                    self.result = await self.journal.close(timeout_seconds=s['close_timeout_seconds'])
                    bot._entry_observation_close_result = self.result
                    return None
            if getattr(self.buffer, 'llm_admission', False):
                self.buffer.llm_quote_submit = feed._quote_subscription_owner.submit_llm_check
            elif self.buffer.order_ready_admission:
                self.buffer.order_ready_quote_submit = feed._quote_subscription_owner.submit_order_ready
            elif self.buffer.scan_scope == 'window':
                self.buffer.signal_quote_submit = feed._quote_subscription_owner.submit
            bot._entry_price_observer = self.buffer
            bot._entry_observation_runtime = self
        except BaseException:
            # 정지 feed의 동기 설치만 되돌린다. 소켓/토큰/태스크는 여기서 시작하지 않는다.
            for name, value in before.items():
                if value is sentinel:
                    if hasattr(feed, name):
                        delattr(feed, name)
                else:
                    if name in sets:
                        value.clear()
                        value.update(sets[name])
                    setattr(feed, name, value)
            self.buffer.mark_incomplete('installation_failed')
            self.buffer.ended = True
            await self.journal.close(timeout_seconds=s['close_timeout_seconds'])
            raise
        return self

    def llm_readiness(self):
        """Actual process bindings and bounded counters; no symbols/account values."""
        b, j = self.buffer, self.journal
        feed = self.bot.ws_feed
        owner = feed._quote_subscription_owner
        snapshot = owner.snapshot()
        socket = getattr(feed, '_ws', None)
        connected = (owner._send is not None and getattr(feed, '_connected', False) is True
                     and getattr(feed, '_should_connect', False) is True
                     and socket is not None and socket.closed is False)
        pristine = (owner._send is None and owner.generation == 0 and socket is None
                    and getattr(feed, '_connected', False) is False)
        callback = getattr(b, 'llm_quote_submit', None)
        risk = getattr(getattr(self.bot, 'engine', None), 'risk_manager', None)
        bound = (b.llm_admission and callback == owner.submit_llm_check
                 and owner.observer is b and b._journal_sink is j and j.buffer is b
                 and getattr(self.bot, '_entry_price_observer', None) is b
                 and getattr(risk, '_entry_observation_owner', None) is self.bot)
        return dict(version=LLM_VERSION if b.llm_admission else None,
            quote_admission='llm_check_started' if b.llm_admission else None,
            llm_hook_bound=bool(bound), rest_market_state_evidence=b.rest_market_state_evidence,
            market_state_evidence=owner.market_state_evidence,
            market_state_max_records=owner.market_state_max_records,
            start_at=b.start_at.isoformat(), admission_end_at=b.admission_end.isoformat(),
            end_at=b.end_at.isoformat(), max_scans=b.max_scans,
            selection_max_candidates=b.selection_basis_settings['max_candidates'],
            gate_max_candidates=b.entry_gate_trace_settings['max_candidates'],
            max_candidates=owner.max_candidates, lease_seconds=owner.lease_seconds,
            registration_cap=owner.cap, external_reserved=owner.reserved,
            operational_headroom=owner.headroom,
            operational_initialized=owner._operational_initialized,
            operational_demand=snapshot['operational_demand'],
            required_new_channels=owner.max_candidates * (3 if owner.market_state_evidence else 1),
            connected=connected, connection_pristine=pristine, generation=owner.generation,
            operational_stream_ready=bool(owner._operational_initialized
                and connected and snapshot['operational_missing'] == 0),
            record_allocations=dict(b._record_limits), buffer_capacity=b.capacity,
            queue_capacity=j._queue.maxsize, batch_size=j._batch_size,
            max_record_bytes=j._max_record_bytes, max_bytes=j._max_bytes,
            retained_memory_limit_bytes=b.retained_memory_limit_bytes,
            retained_memory_bytes=b.retained_memory_bytes, memory_exhausted=b.memory_exhausted,
            dropped_records=b._dropped, incomplete_reasons=sorted(b._incomplete_reasons))

    def watch_tasks(self, tasks):
        # 보조/일회성 작업의 정상 반환은 관측 경로의 종료가 아니다.
        required = {'engine', 'kr_ws_feed', 'kr_screener', 'kr_rest_price_feed'}
        for task in tasks:
            if task.get_name() in required:
                task.add_done_callback(self._task_finished)

    def _task_finished(self, task):
        if not self.buffer.ended:
            self.interrupt('runner_task_ended')

    def start(self):
        if self.task is None:
            self.task = asyncio.create_task(self._until_deadline(), name='entry_observation_deadline')

    def interrupt(self, reason):
        if not self.buffer.ended:
            if self.buffer.current_time() < self.plan.end_at:
                self.buffer.mark_incomplete(reason)
            self.buffer.ended = True
        owner = self.bot.ws_feed._quote_subscription_owner
        owner.end_observation()

    async def _until_deadline(self):
        try:
            while not self.buffer.ended:
                remaining = (self.plan.end_at-self.buffer.current_time()).total_seconds()
                if remaining <= 0 or self.buffer.ended:
                    break
                await asyncio.sleep(min(remaining, 0.25))
            return await self.close()
        except asyncio.CancelledError:
            self.interrupt('deadline_task_cancelled')
            await self.close()
            raise

    async def close(self, reason=None):
        if self._close_task is None:
            if reason:
                self.interrupt(reason)
            elif self.buffer.current_time() < self.plan.end_at and not self.buffer.ended:
                self.interrupt('capture_closed_early')
            else:
                self.interrupt('capture_deadline')
            self._close_task = asyncio.create_task(self._finish())
        return await asyncio.shield(self._close_task)

    async def _finish(self):
        self.result = await self.journal.close(timeout_seconds=self.plan.settings['close_timeout_seconds'])
        self.bot._entry_observation_close_result = self.result
        return self.result
