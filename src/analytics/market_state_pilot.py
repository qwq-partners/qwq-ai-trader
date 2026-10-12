"""한 종목·유한 구간의 기술 수집 계약. 경제성 코호트/판정을 생성하지 않는다."""
from datetime import time
import math
from pathlib import Path

from .entry_observation import EntryObservationBuffer
from .entry_observation_journal import MAX_LINE_BYTES
from .entry_price_shadow import KST, _timestamp

PILOT_VERSION = 'runner-market-state-pilot-v1'


def validate_pilot_context(context, *, study_path, now):
    if (type(context) is not dict or set(context) != {'schema_version', 'evaluation_epoch', 'as_of', 'capture'}
            or context['schema_version'] != 'market-state-pilot-study-v1'):
        raise ValueError('기술 파일럿 전용 연구 계약 필요')
    epoch = context['evaluation_epoch']
    if not isinstance(epoch, str) or not epoch.strip() or len(epoch) > 200:
        raise ValueError('bounded evaluation_epoch required')
    s = context['capture']
    fields = set(('version study_ref start_at end_at pilot_symbol source_version_ref configuration_ref '
        'capacity_evidence_ref journal_path buffer_capacity queue_capacity batch_size max_bytes '
        'max_record_bytes open_timeout_seconds close_timeout_seconds channels').split())
    if type(s) is not dict or set(s) != fields or s['version'] != PILOT_VERSION:
        raise ValueError('기술 파일럿 capture 계약/필드 불일치')
    for key in ('study_ref', 'source_version_ref', 'configuration_ref', 'capacity_evidence_ref'):
        if not isinstance(s[key], str) or not s[key].strip() or len(s[key]) > 200:
            raise ValueError('제한된 명시 근거 참조 필요')
    symbol = s['pilot_symbol']
    if not isinstance(symbol, str) or len(symbol) != 6 or not symbol.isascii() or not symbol.isdigit():
        raise ValueError('한 종목 ASCII 6자리 코드 필요')
    start, end = (_timestamp(s[k], k) for k in ('start_at', 'end_at'))
    if (now.tzinfo is None or now.utcoffset() is None or not now < start < end
            or (end-start).total_seconds() > 300 or end != _timestamp(context['as_of'], 'as_of')):
        raise ValueError('미래 최대300초/as_of 구간 필요')
    a, b = start.astimezone(KST), end.astimezone(KST)
    if (a.date() != b.date() or a.weekday() >= 5 or not time(9) <= a.time() < b.time() < time(15,20)):
        raise ValueError('같은 평일 KRX 시간 형식 필요; 개장 증거는 별도')
    p = Path(s['journal_path'])
    if not p.is_absolute() or p == study_path:
        raise ValueError('별도 절대 신규 원장 경로 필요')
    if p.exists() or p.is_symlink():
        raise FileExistsError('관측 원장 경로 재사용 금지')
    for key in ('buffer_capacity','queue_capacity','batch_size','max_bytes','max_record_bytes'):
        if type(s[key]) is not int or s[key] <= 0:
            raise ValueError('자원 한도는 양의 정수 필요')
    if (s['batch_size'] > s['queue_capacity'] or not 4096 <= s['max_record_bytes'] <= MAX_LINE_BYTES-2048):
        raise ValueError('파일럿 저장 batch/레코드 한도 위반')
    for key in ('open_timeout_seconds','close_timeout_seconds'):
        if type(s[key]) not in (int,float) or not math.isfinite(s[key]) or not 0 < s[key] <= 10:
            raise ValueError('I/O 대기 한도는0~10초')
    ch = s['channels']
    keys = set(('registration_cap external_reserved operational_headroom max_candidates lease_seconds '
                'evidence_ref market_state_evidence market_state_max_records').split())
    if (type(ch) is not dict or set(ch) != keys or ch['market_state_evidence'] is not True
            or type(ch['max_candidates']) is not int or ch['max_candidates'] != 1):
        raise ValueError('한 종목 장 상태 채널 계약 필요')
    from src.data.feeds.quote_subscription import QuoteSubscriptionCoordinator
    QuoteSubscriptionCoordinator(EntryObservationBuffer(evaluation_epoch=epoch, capacity=1), **ch)
    if ch['registration_cap']-ch['external_reserved']-ch['operational_headroom'] < 3:
        raise ValueError('파일럿3채널 예산 부족')
    if ch['lease_seconds'] < (end-now).total_seconds():
        raise ValueError('준비부터 종료까지 lease 필요')
    if (s['buffer_capacity'] < 100+ch['market_state_max_records']
            or s['queue_capacity'] < s['buffer_capacity']
            or s['max_bytes'] < s['buffer_capacity']*(s['max_record_bytes']+2048)+2048):
        raise ValueError('파일럿 유한 메모리/큐/저장 예산 부족')


class PilotBuffer(EntryObservationBuffer):
    """수신 기술 자료만 복사한다. 일상 스캐너/주문 훅은 수집하지 않는다."""
    def __init__(self, plan, now):
        s = plan.settings
        super().__init__(evaluation_epoch=plan.context['evaluation_epoch'], capacity=s['buffer_capacity'])
        self.scan_scope = 'pilot'
        self.order_ready_admission = False
        self.static_pilot = True
        self.pilot_symbol = s['pilot_symbol']
        self.start_at, self.end_at = plan.start_at, plan.end_at
        self._now, self._last_clock = now, now()
        self.ended = False

    def current_time(self):
        now = self._now()
        if now < self._last_clock:
            self.mark_incomplete('capture_clock_regressed')
            self.ended = True
        self._last_clock = now
        if now >= self.end_at:
            self.ended = True
        return now

    def begin_scan(self):
        return None

    def accepts_order_signal(self, signal_id):
        return False

    def publish(self, record):
        now = self.current_time()
        if self.ended or record.get('kind') not in ('quote_subscription','ws_quote','market_state'):
            return False
        if record['kind'] != 'quote_subscription':
            if now < self.start_at or record.get('symbol') != self.pilot_symbol:
                return False
            try:
                value = (record.get('provenance', {}).get('received_at')
                         if record['kind'] == 'ws_quote' else record.get('received_at'))
                received = _timestamp(value, 'received_at')
            except (ValueError, TypeError, AttributeError):
                self.mark_incomplete('pilot_receipt_time_invalid')
                return False
            if not self.start_at <= received < self.end_at:
                return False
        return super().publish(record)
