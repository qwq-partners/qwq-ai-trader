"""엔진 메모리의 제한된 후보 식별자 투영과 고정 loopback 조회. 거래 조회 없음."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
import re
import time

from src.data.providers.toss.http_body import BodyError, BodyLimits, read_json_bounded
from src.data.providers.toss.orderbook_stream import aware_time
from .toss_positions import InputUnavailable

ANCHOR_URL = 'http://127.0.0.1:8080/api/internal/entry-anchors'
SCHEMA = 'entry-anchor-projection-v2'
LEGACY_SCHEMA = 'entry-anchor-projection-v1'
SELECTION_RULE = 'first_three_in_returned_order'
MAX_SOURCE_RECORDS = 80000  # 장전 기존 호가를 포함한 v4 journal 예산. 투영 크기는 별도 제한.
FIELDS = {
    'scan': ('scan_id', 'observed_at', 'route_origin', 'population_scope', 'scan_admission_ref'),
    'signal': ('candidate_id', 'signal_id', 'observed_at'),
    'order_ready': ('signal_id', 'symbol', 'order_id', 'requested_quantity', 'observed_at'),
}


class InputInvalid(Exception):
    """로컬 endpoint가 응답했지만 제한된 payload를 신뢰할 수 없다."""
    def __init__(self):
        super().__init__('input_invalid')


def project_anchors(runtime, *, now=None):
    """호출 이벤트 루프에서 허용 필드만 복사. 관측·주문 객체를 변경하지 않는다."""
    buffer = runtime.buffer
    if getattr(buffer, 'scan_scope', None) == 'window':
        raise ValueError('anchor_requires_first_scan_capture')
    records = []
    selected, signal_ids = set(), set()
    cohort = []
    if len(buffer._records) > MAX_SOURCE_RECORDS:
        raise ValueError('anchor_source_too_large')
    for record in buffer._records:
        kind = record.get('kind')
        if kind not in FIELDS:
            continue
        if kind == 'signal':
            if record.get('candidate_id') not in selected:
                continue
            signal_ids.add(record.get('signal_id'))
        elif kind == 'order_ready' and record.get('signal_id') not in signal_ids:
            continue
        if len(records) >= 7:
            raise ValueError('anchor_population_exceeded')
        row = {k: deepcopy(record[k]) for k in FIELDS[kind] if k in record}
        row.update(kind=kind, sequence=len(records) + 1, source_sequence=record['sequence'])
        if kind == 'scan':
            if len(record['candidates']) > 100:
                raise ValueError('anchor_population_exceeded')
            row['candidates'] = [{k: c[k] for k in ('candidate_id', 'symbol')} for c in record['candidates']]
            cohort = row['candidates']
            selected = {c['candidate_id'] for c in cohort[:3]}
        records.append(row)
    status = buffer.capture_status()
    result = runtime.result or {}
    sealed = (buffer._capture_closed and result.get('sealed') is True
              and result.get('fsync_confirmed') is True and result.get('error') is None)
    reasons = list(status['incomplete_reasons'])
    if not sealed:
        reasons.append('engine_source_not_final')
    value = dict(schema_version=SCHEMA, evaluation_epoch=buffer.evaluation_epoch,
        study_sha256=runtime.plan.study_sha256, capture_id=runtime.journal._header['capture_id'],
        observed_at=aware_time(now or datetime.now(timezone.utc)).isoformat(),
        capture_closed=buffer._capture_closed, journal_sealed=sealed,
        source_record_count=len(buffer._records), complete=sealed and status['complete'],
        incomplete_reasons=sorted(set(reasons)), dropped_records=status['dropped_records'], records=records,
        selection=dict(rule=SELECTION_RULE, returned_candidate_count=len(cohort),
                       selected_candidate_ids=[c['candidate_id'] for c in cohort[:3]]))
    return validate_projection(value)


def validate_projection(value):
    keys = {'schema_version', 'evaluation_epoch', 'study_sha256', 'capture_id', 'observed_at',
            'capture_closed', 'journal_sealed', 'source_record_count', 'complete',
            'incomplete_reasons', 'dropped_records', 'records'}
    if type(value) is not dict:
        raise ValueError('anchor_schema_invalid')
    subset = value.get('schema_version') == SCHEMA
    if subset:
        keys.add('selection')
    if set(value) != keys or value['schema_version'] not in (SCHEMA, LEGACY_SCHEMA):
        raise ValueError('anchor_schema_invalid')
    for key in ('evaluation_epoch', 'capture_id'):
        if type(value[key]) is not str or not 1 <= len(value[key]) <= 120:
            raise ValueError('anchor_identity_invalid')
    if type(value['study_sha256']) is not str or not re.fullmatch('[0-9a-f]{64}', value['study_sha256']):
        raise ValueError('anchor_study_invalid')
    as_of = aware_time(value['observed_at'])
    for key in ('capture_closed', 'journal_sealed', 'complete'):
        if type(value[key]) is not bool:
            raise ValueError('anchor_status_invalid')
    for key in ('source_record_count', 'dropped_records'):
        if type(value[key]) is not int or not 0 <= value[key] <= 10000000:
            raise ValueError('anchor_count_invalid')
    reasons, records = value['incomplete_reasons'], value['records']
    if (type(reasons) is not list or len(reasons) > 20 or any(type(r) is not str or len(r) > 120 for r in reasons)
            or type(records) is not list or len(records) > 7
            or (value['complete'] and (not value['capture_closed'] or not value['journal_sealed']
                                      or reasons or value['dropped_records']))):
        raise ValueError('anchor_completeness_invalid')
    prior = 0
    scan = None
    candidates, signals, orders = {}, {}, set()
    signalled, ordered = set(), set()
    for index, row in enumerate(records, 1):
        if type(row) is not dict or row.get('kind') not in FIELDS:
            raise ValueError('anchor_record_invalid')
        allowed = set(FIELDS[row['kind']]) | {'kind','sequence','source_sequence'}
        required = allowed - {'scan_admission_ref'}
        if row['kind'] == 'scan':
            if row.get('population_scope') not in ('returned_screen_candidates', 'first_returned_scan_candidates'):
                raise ValueError('anchor_requires_first_scan_capture')
            allowed.add('candidates')
            required.add('candidates')
            cohort = row.get('candidates')
            if (type(cohort) is not list or len(cohort) > (100 if subset else 3)
                    or any(type(c) is not dict or set(c) != {'candidate_id','symbol'} for c in cohort)):
                raise ValueError('anchor_cohort_invalid')
        if (set(row) - allowed or required - set(row)
                or type(row.get('sequence')) is not int or row['sequence'] != index
                or type(row.get('source_sequence')) is not int
                or not prior < row['source_sequence'] <= value['source_record_count']):
            raise ValueError('anchor_sequence_invalid')
        prior = row['source_sequence']
        for k, v in row.items():
            if k in ('sequence', 'source_sequence', 'requested_quantity', 'candidates'):
                continue
            if type(v) is not str or not 1 <= len(v) <= 200:
                raise ValueError('anchor_scalar_invalid')
        at = aware_time(row.get('observed_at'))
        if at > as_of:
            raise ValueError('anchor_time_invalid')
        if row['kind'] == 'scan':
            if scan is not None or index != 1 or row['route_origin'] != 'live_screening':
                raise ValueError('anchor_scan_invalid')
            scan = row
            for item in row['candidates']:
                cid, symbol = item['candidate_id'], item['symbol']
                if (type(symbol) is not str or not re.fullmatch('[0-9]{6}', symbol)
                        or type(cid) is not str or cid != f"{row['scan_id']}:{symbol}"
                        or cid in candidates or symbol in candidates.values()):
                    raise ValueError('anchor_candidate_invalid')
                candidates[cid] = symbol
        else:
            if scan is None or at < aware_time(scan['observed_at']):
                raise ValueError('anchor_chain_invalid')
            sid = row['signal_id']
            if row['kind'] == 'signal':
                cid = row['candidate_id']
                if (cid not in candidates or cid in signalled or sid in signals
                        or (subset and cid not in list(candidates)[:3])):
                    raise ValueError('anchor_signal_invalid')
                signalled.add(cid)
                signals[sid] = (cid, at)
            else:
                qty, oid = row['requested_quantity'], row['order_id']
                if (sid not in signals or sid in ordered or oid in orders
                        or type(qty) is not int or not 1 <= qty <= 10**12
                        or row['symbol'] != candidates[signals[sid][0]] or at < signals[sid][1]):
                    raise ValueError('anchor_order_invalid')
                ordered.add(sid)
                orders.add(oid)
    if subset:
        selection = value['selection']
        if (type(selection) is not dict
                or set(selection) != {'rule','returned_candidate_count','selected_candidate_ids'}
                or selection['rule'] != SELECTION_RULE
                or type(selection['returned_candidate_count']) is not int
                or selection['returned_candidate_count'] != len(candidates)
                or selection['selected_candidate_ids'] != list(candidates)[:3]):
            raise ValueError('anchor_selection_invalid')
    if len(json.dumps(value, ensure_ascii=False).encode()) > 65536:
        raise ValueError('anchor_size_exceeded')
    return deepcopy(value)


class AnchorClient:
    def __init__(self, *, session_factory=None, clock=time.monotonic):
        self._factory, self._clock = session_factory, clock
        self._session = None
        self._closed = False

    async def fetch(self):
        import aiohttp
        try:
            if self._closed:
                raise InputUnavailable()
            deadline = self._clock() + 2
            async with asyncio.timeout(2):
                if self._session is None:
                    self._session = self._factory() if self._factory else aiohttp.ClientSession(
                        trust_env=False, cookie_jar=aiohttp.DummyCookieJar(), auto_decompress=False)
                    if not hasattr(self._session, '_retry_connection'):
                        raise InputUnavailable()
                    self._session._retry_connection = False
                async with self._session.get(ANCHOR_URL, allow_redirects=False,
                        headers={'Accept-Encoding':'identity', 'X-QWQ-Observation':'1'},
                        timeout=aiohttp.ClientTimeout(total=2, connect=.5, sock_connect=.5)) as response:
                    if response.status != 200:
                        raise InputUnavailable()
                    try:
                        value = await read_json_bounded(response, limits=BodyLimits(65536, 8, 4096, 1024, .2),
                                                        deadline=deadline, clock=self._clock)
                    except BodyError as exc:
                        if exc.code in ('network_error', 'timeout'):
                            raise InputUnavailable() from None
                        raise InputInvalid() from None
        except asyncio.CancelledError:
            raise
        except (InputUnavailable, InputInvalid):
            raise
        except Exception:
            raise InputUnavailable() from None
        try:
            return validate_projection(value)
        except Exception:
            raise InputInvalid() from None

    async def close(self):
        self._closed = True
        if self._session is not None:
            await self._session.close()
