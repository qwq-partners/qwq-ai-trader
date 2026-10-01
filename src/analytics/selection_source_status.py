"""선정 호출의 로컬 획득 근거. 자료의 거래소 시각·독립성·수익성을 추정하지 않는다."""
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime, timezone
import inspect
import math
from uuid import uuid4

from .selection_basis import SOURCE_IDS

VERSION = 'selection-source-status-v1'
ORDER = ('premarket_gap', 'kis_volume_surge', 'kis_institutional_buying', 'kis_new_highs',
         'kis_fluctuation_rank', 'kis_foreign_buying', 'naver_volume_rank', 'naver_rise_rank',
         'naver_new_high', 'theme_news', 'llm_news')
FAILURES = frozenset(('http', 'api', 'schema', 'parse', 'exception', 'unverified'))
RUN_FIELDS = frozenset(('source_id', 'outcome', 'started_at', 'completed_at', 'returned_count',
    'payload_count', 'raw_count', 'source_cache_reads', 'input_cache_reads', 'max_cache_age_seconds',
    'last_payload_at', 'failure_count', 'failure_reason', 'source_as_of'))
SNAPSHOT_FIELDS = frozenset(('version', 'capture_id', 'started_at', 'completed_at', 'fallback_used', 'runs'))
SCAN_FIELDS = frozenset(('selection_sources_expected', 'selection_sources_status',
    'selection_sources_version', 'selection_sources_id', 'selection_sources_started_at',
    'selection_sources_completed_at', 'selection_sources_fallback_used', 'selection_source_runs'))
_ACTIVE = ContextVar('selection_source_run', default=None)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _count(value):
    if type(value) is not int or not 0 <= value <= 1000000:
        raise ValueError('원천 행수/횟수 한도')
    return value


def _time(value):
    if not isinstance(value, str):
        raise ValueError('원천 관측 시각 필요')
    at = datetime.fromisoformat(value)
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError('원천 관측 시간대 필요')
    return at


def _outcome(row):
    if row['returned_count'] is None:
        return 'not_called'
    suffix = 'nonempty' if row['returned_count'] else 'empty'
    if row['failure_count']:
        return 'partial' if row['returned_count'] else 'failed'
    required = 2 if row['source_id'] in ('kis_institutional_buying', 'kis_foreign_buying') else 1
    inputs = row['payload_count'] + row['input_cache_reads']
    if row['source_cache_reads'] or inputs >= required:
        return ('cached_' if row['source_cache_reads'] or row['input_cache_reads'] else 'completed_') + suffix
    return 'unknown_' + suffix


def mark_source(event, *, count=None, age_seconds=None, reason=None):
    """현재 호출에만 붙는 허용 값. 오류문구/원 응답을 저장하지 않으며 절대 전파하지 않는다."""
    row = _ACTIVE.get()
    if row is None:
        return
    try:
        if sum(row[k] for k in ('payload_count', 'source_cache_reads', 'input_cache_reads', 'failure_count')) >= 16:
            raise ValueError('원천 관측 이벤트 한도')
        if event == 'payload':
            row['payload_count'] += 1
            row['last_payload_at'] = _now()
            if count is None:
                row['_raw_unknown'] = True
            else:
                row['_raw_sum'] += _count(count)
        elif event in ('cache', 'input_cache'):
            row['source_cache_reads' if event == 'cache' else 'input_cache_reads'] += 1
            if age_seconds is None:
                row['_cache_unknown'] = True
            elif type(age_seconds) not in (float, int) or not math.isfinite(age_seconds) or age_seconds < 0:
                raise ValueError('캐시 경과시간 불명')
            else:
                row['_ages'].append(float(age_seconds))
        elif event == 'error' and reason in FAILURES:
            row['failure_count'] += 1
            row['failure_reason'] = reason
        else:
            raise ValueError('지원하지 않는 원천 관측')
    except Exception:
        row['_broken'] = True


class SourceCapture:
    """한 screen_all 호출의 유한 상태. ContextVar는 gather 및 동시 scan을 분리한다."""
    def __init__(self):
        self.capture_id, self.started_at = uuid4().hex, _now()
        self.runs, self.broken = {}, False

    async def call(self, source_id, func, *args, **kwargs):
        row = token = None
        try:
            if source_id not in SOURCE_IDS or source_id in self.runs:
                raise ValueError('원천 호출 중복/범위')
            row = dict.fromkeys(RUN_FIELDS)
            row.update(source_id=source_id, started_at=_now(), payload_count=0,
                       source_cache_reads=0, input_cache_reads=0, failure_count=0,
                       _raw_unknown=False, _raw_sum=0, _cache_unknown=False, _ages=[], _broken=False)
            self.runs[source_id] = row
            token = _ACTIVE.set(row)
        except Exception:
            self.broken = True
        try:
            result = func(*args, **kwargs)
            if inspect.isawaitable(result):
                result = await result
        except BaseException:
            mark_source('error', reason='exception')
            try:
                self._finish(row, 0)
            except Exception:
                self.broken = True
            raise
        else:
            try:
                self._finish(row, len(result))
            except Exception:
                self.broken = True
            return result
        finally:
            if token is not None:
                _ACTIVE.reset(token)

    def _finish(self, row, count):
        try:
            if row is None or row['_broken']:
                raise ValueError('원천 관측 불완전')
            row.update(completed_at=_now(), returned_count=_count(count),
                       raw_count=None if row['_raw_unknown'] or not row['payload_count'] else _count(row['_raw_sum']),
                       max_cache_age_seconds=(max(row['_ages']) if row['_ages'] and not row['_cache_unknown'] else None))
            row['outcome'] = _outcome(row)
        except Exception:
            self.broken = True

    def wrap(self, result, *, fallback=False):
        try:
            return self._wrap(result, fallback=fallback)
        except Exception:
            self.broken = True
            return result

    def _wrap(self, result, *, fallback=False):
        if self.broken:
            return result
        runs = []
        for source_id in ORDER:
            if source_id in self.runs:
                row = {k: self.runs[source_id][k] for k in RUN_FIELDS}
            else:
                row = dict.fromkeys(RUN_FIELDS)
                row.update(source_id=source_id, outcome='not_called', payload_count=0,
                           source_cache_reads=0, input_cache_reads=0, failure_count=0)
            runs.append(row)
        snapshot = dict(version=VERSION, capture_id=self.capture_id, started_at=self.started_at,
                        completed_at=_now(), fallback_used=fallback, runs=runs)
        class ObservedStocks(list):
            pass
        wrapped = ObservedStocks(result)
        wrapped.selection_sources = validate_snapshot(snapshot, observed_at=snapshot['completed_at'])
        return wrapped


def validate_snapshot(snapshot, *, observed_at):
    if (type(snapshot) is not dict or set(snapshot) != SNAPSHOT_FIELDS or snapshot['version'] != VERSION
            or type(snapshot['fallback_used']) is not bool or not isinstance(snapshot['capture_id'], str)
            or len(snapshot['capture_id']) != 32 or any(c not in '0123456789abcdef' for c in snapshot['capture_id'])):
        raise ValueError('원천 관측 형식/버전 불일치')
    start, end = _time(snapshot['started_at']), _time(snapshot['completed_at'])
    if not start <= end <= _time(observed_at):
        raise ValueError('원천 관측 시각 순서 불일치')
    runs = snapshot['runs']
    if type(runs) is not list or len(runs) != len(ORDER):
        raise ValueError('원천 관측 전체 분모 필요')
    for source_id, row in zip(ORDER, runs):
        if type(row) is not dict or set(row) != RUN_FIELDS or row['source_id'] != source_id or row['source_as_of'] is not None:
            raise ValueError('원천 관측 ID/필드/원천시각 주장 불일치')
        for key in ('payload_count', 'source_cache_reads', 'input_cache_reads', 'failure_count'):
            _count(row[key])
        if sum(row[k] for k in ('payload_count', 'source_cache_reads', 'input_cache_reads', 'failure_count')) > 16:
            raise ValueError('원천 관측 이벤트 한도 초과')
        if row['returned_count'] is not None:
            _count(row['returned_count'])
            if not start <= _time(row['started_at']) <= _time(row['completed_at']) <= end:
                raise ValueError('원천 호출 시각 불일치')
        elif any(row[k] is not None for k in ('started_at', 'completed_at', 'last_payload_at', 'raw_count',
                                             'max_cache_age_seconds', 'failure_reason')) or any(
                row[k] != 0 for k in ('payload_count', 'source_cache_reads', 'input_cache_reads', 'failure_count')):
            raise ValueError('미호출 원천에 관측값 주장')
        if row['raw_count'] is not None:
            _count(row['raw_count'])
            if not row['payload_count']:
                raise ValueError('응답 없는 원천의 행수')
        if row['payload_count']:
            if not _time(row['started_at']) <= _time(row['last_payload_at']) <= _time(row['completed_at']):
                raise ValueError('원천 응답 시각 불일치')
        elif row['last_payload_at'] is not None:
            raise ValueError('응답 없는 원천의 응답 시각')
        age = row['max_cache_age_seconds']
        if age is not None and (type(age) not in (int, float) or not math.isfinite(age) or age < 0
                or not row['source_cache_reads'] + row['input_cache_reads']):
            raise ValueError('캐시 경과시간 불일치')
        if ((row['failure_count'] == 0 and row['failure_reason'] is not None)
                or (row['failure_count'] > 0 and row['failure_reason'] not in FAILURES)
                or row['outcome'] != _outcome(row)):
            raise ValueError('원천 상태/실패 근거 모순')
    return deepcopy(snapshot)


def snapshot_fields(snapshot):
    return dict(selection_sources_version=snapshot['version'], selection_sources_id=snapshot['capture_id'],
                selection_sources_started_at=snapshot['started_at'], selection_sources_completed_at=snapshot['completed_at'],
                selection_sources_fallback_used=snapshot['fallback_used'], selection_source_runs=snapshot['runs'])


def read_scan_sources(scan):
    extra = SCAN_FIELDS & set(scan)
    if not extra:
        return {'status': 'disabled', 'snapshot': None}
    if scan.get('selection_sources_expected') is not True or scan.get('selection_basis_expected') is not True:
        raise ValueError('명시 활성화 없는 원천 관측')
    common = {'selection_sources_expected', 'selection_sources_status'}
    if scan.get('selection_sources_status') == 'unavailable' and extra == common:
        return {'status': 'unavailable', 'snapshot': None}
    if scan.get('selection_sources_status') != 'observed' or extra != SCAN_FIELDS:
        raise ValueError('원천 관측 누락/상태 모순')
    snapshot = dict(version=scan['selection_sources_version'], capture_id=scan['selection_sources_id'],
        started_at=scan['selection_sources_started_at'], completed_at=scan['selection_sources_completed_at'],
        fallback_used=scan['selection_sources_fallback_used'], runs=scan['selection_source_runs'])
    return {'status': 'observed', 'snapshot': validate_snapshot(snapshot, observed_at=scan['observed_at'])}


def lineage_diagnostic(basis):
    """알려진 동일 입력 계열만 묶는다. 서로 다른 계열의 통계적 독립은 주장하지 않는다."""
    groups = {'premarket_gap': 'kis_fluctuation', 'kis_fluctuation_rank': 'kis_fluctuation',
              'naver_rise_rank': 'naver_rise', 'naver_new_high': 'naver_rise'}
    by_family = {}
    for term in basis['source_terms']:
        source = term['source_id']; family = groups.get(source, source)
        by_family.setdefault(family, []).append(source)
    return {'occurrences': len(basis['source_terms']), 'known_input_families': len(by_family),
            'overlapping_families': {k:v for k,v in by_family.items() if len(v) > 1},
            'independence_verified': False, 'profit_effect_verified': False}
