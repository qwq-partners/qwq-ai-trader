"""비교 시도·쌍의 증거 보존 조회와 명시 복구. 공급자 호출·월 정산·시각 생성이 없다.

- inspect_attempt / inspect_pair / export_incomplete_pair 는 읽기 전용 진단이다.
- restore_pair_result 는 PairResultPersistenceError.result 같은 **원래 행**만 받아
  저장된 manifest·requested_at·각 시도 저장 원본과 모두 일치할 때만
  ``UPDATE comparison_pairs SET result=? WHERE pair_id=? AND result IS NULL`` 한다.
  현재 시각으로 어떤 시각 필드도 만들지 않고, 맞지 않으면 ValueError 로 아무것도 쓰지 않는다.
- 감사 대사로 정산된 비용은 inspect 의 ``reconciled`` 로만 보이고 원래 행·응답은 바꾸지 않는다.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import sqlite3
import time

from .model_comparison_execution import ComparisonCallGuard
from .model_comparison_runner import _json
from ..utils.llm_budget import BudgetBlocked, BudgetStateError

# 러너가 오류·시간 초과 시 원래 응답 위에 덮어쓰는 키와, 모든 시도에 붙이는 키.
_ERROR_OVERRIDES = frozenset({'provider_success', 'success', 'content', 'api_cost_usd', 'request_sent',
                              'timing_valid', 'requested_model', 'error'})
_RUNNER_KEYS = frozenset({'attempt_id', 'role', 'reservation_token', 'ledger_state', 'available_at'})
# 행 기록 뒤 감사 대사만 상태를 바꿀 수 있다. 그 외 상태 차이는 모순이다.
_RECONCILED_FROM = frozenset({'unknown', 'pending_reconciliation'})


def _check_guard(guard):
    if not isinstance(guard, ComparisonCallGuard):
        raise ValueError('ComparisonCallGuard required')


def _cost(value):
    return None if value is None else str(value)


def inspect_attempt(guard, attempt_id) -> dict:
    """원래 응답/사용량·원래 추정 비용·대사 비용·예약 토큰을 분리해 돌려준다."""
    _check_guard(guard)
    record = guard.lookup(attempt_id)
    pair_id = attempt_id.split(':', 1)[0] if ':' in attempt_id else None
    if record is None:
        return {'attempt_id': attempt_id, 'pair_id': pair_id, 'found': False}
    result = record['result']
    return {'attempt_id': attempt_id, 'pair_id': pair_id, 'found': True,
            'request_sha256': record['request_sha256'], 'state': record['state'],
            'block_reason': record['block_reason'], 'reservation_token': record['reservation_token'],
            'original_response': None if result is None else deepcopy(result.value),
            'original_measured_cost_usd': None if result is None else _cost(result.actual_cost_usd),
            'original_estimated_cost_usd': record['original_estimated_cost_usd'],
            'reconciled': record['reconciled']}


def _pair_storage(guard, pair_id):
    """(manifest, requested_at, result) 또는 None, 그리고 저장된 시도 ID 목록."""
    if not isinstance(pair_id, str) or not pair_id:
        raise ValueError('pair_id required')
    prefix = pair_id + ':'
    try:
        with guard.ledger._connect(readonly=True) as db:
            db.execute('BEGIN')
            guard._check_policy(db)
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='comparison_pairs'").fetchone() is None:
                row = None
            else:
                row = db.execute('SELECT manifest,requested_at,result FROM comparison_pairs WHERE pair_id=?',
                                 (pair_id,)).fetchone()
            ids = [r[0] for r in db.execute('SELECT attempt_id FROM comparison_requests '
                                            'WHERE substr(attempt_id,1,?)=? ORDER BY attempt_id',
                                            (len(prefix), prefix))]
            return (tuple(row) if row is not None else None), ids
    except BudgetBlocked:
        raise
    except (OSError, sqlite3.Error):
        raise BudgetStateError('comparison_pair_storage_unavailable') from None


def inspect_pair(guard, pair_id) -> dict:
    _check_guard(guard)
    row, ids = _pair_storage(guard, pair_id)
    attempts = [inspect_attempt(guard, attempt_id) for attempt_id in ids]
    if row is None:
        return {'pair_id': pair_id, 'found': False, 'complete': False, 'attempts': attempts}
    manifest, requested_at, result = row
    return {'pair_id': pair_id, 'found': True, 'manifest': json.loads(manifest),
            'manifest_sha256': hashlib.sha256(manifest.encode()).hexdigest(),
            'requested_at': requested_at, 'has_result': result is not None,
            'result': None if result is None else json.loads(result),
            'attempts': attempts, 'complete': result is not None}


def export_incomplete_pair(guard, pair_id) -> dict:
    """미완성 쌍의 진단 자료만 만든다. 결과 행이 아니며 재실행/정산 근거로 쓰지 않는다."""
    info = inspect_pair(guard, pair_id)
    if not info['found']:
        raise ValueError('pair_not_found')
    if info['complete']:
        raise ValueError('pair_complete')
    reasons = ['pair_result_missing', 'no_automatic_retry']
    reasons += [f"{a['attempt_id']}:{a['state']}" for a in info['attempts']
                if a.get('state') not in {'known', 'known_reconciled'}]
    return {**info, 'kind': 'model-comparison-incomplete-pair-v1', 'diagnostic_only': True, 'reasons': reasons}


def _row_attempts(row, pair_id):
    unique = {}
    for arm in ('current', 'candidate'):
        attempts = row.get(arm, {}).get('attempts') if isinstance(row.get(arm), dict) else None
        if type(attempts) is not list or not attempts:
            raise ValueError('result_row_attempts_missing')
        for attempt in attempts:
            attempt_id = attempt.get('attempt_id') if type(attempt) is dict else None
            if not isinstance(attempt_id, str) or not attempt_id.startswith(pair_id + ':'):
                raise ValueError('result_row_attempt_identity_mismatch')
            if attempt_id in unique and unique[attempt_id] != attempt:
                raise ValueError('result_row_shared_attempt_mismatch')
            unique[attempt_id] = attempt
    return unique


def _verify_attempt(guard, attempt):
    record = guard.lookup(attempt['attempt_id'])
    if record is None:
        raise ValueError('result_row_attempt_not_stored')
    if attempt.get('reservation_token') != record['reservation_token']:
        raise ValueError('result_row_reservation_mismatch')
    old, new = attempt.get('ledger_state'), record['state']
    if old != new and not (old in _RECONCILED_FROM and new == 'known_reconciled'):
        raise ValueError('result_row_ledger_state_mismatch')
    saved = record['result']
    if saved is None:
        if attempt.get('success') is True:
            raise ValueError('result_row_success_without_stored_response')
        return
    if type(saved.value) is not dict:
        raise ValueError('result_row_stored_response_shape')
    skip = _RUNNER_KEYS | (_ERROR_OVERRIDES if 'error' in attempt else frozenset())
    for key, value in saved.value.items():
        if key not in skip and attempt.get(key) != value:
            raise ValueError('result_row_response_mismatch')


def _write_once(guard, pair_id, manifest_json, payload):
    with guard.ledger._connect() as db:
        db.execute('BEGIN IMMEDIATE')
        guard._check_policy(db)
        row = db.execute('SELECT manifest,result FROM comparison_pairs WHERE pair_id=?', (pair_id,)).fetchone()
        if row is None or row[0] != manifest_json:
            raise ValueError('pair_manifest_changed')
        if row[1] is not None:
            if row[1] == payload:
                return False
            raise ValueError('pair_result_already_present')
        changed = db.execute('UPDATE comparison_pairs SET result=? WHERE pair_id=? AND result IS NULL',
                             (payload, pair_id)).rowcount
        if changed != 1:
            raise BudgetStateError('comparison_pair_storage_corrupt')
        db.execute('COMMIT')
        return True


def restore_pair_result(guard, pair_id, result_row, *, manifest_json) -> dict:
    _check_guard(guard)
    if type(result_row) is not dict or not isinstance(manifest_json, str):
        raise ValueError('original result row and manifest_json required')
    try:
        payload = _json(result_row)
        manifest = json.loads(manifest_json)
    except (TypeError, ValueError, RecursionError):
        raise ValueError('result_row_or_manifest_not_json') from None
    row, ids = _pair_storage(guard, pair_id)
    if row is None:
        raise ValueError('pair_not_found')
    stored_manifest, requested_at, stored_result = row
    if stored_manifest != manifest_json or type(manifest) is not dict or type(manifest.get('spec')) is not dict:
        raise ValueError('pair_manifest_mismatch')
    if stored_result is not None:
        if stored_result == payload:
            return {'restored': False, 'already_present': True, 'pair_id': pair_id,
                    'result_sha256': hashlib.sha256(payload.encode()).hexdigest()}
        raise ValueError('pair_result_already_present')
    spec = manifest['spec']
    expected = {'schema_version': manifest.get('schema_version'), 'pair_id': pair_id,
                'decision_id': spec.get('decision_id'), 'signal_id': spec.get('signal_id'),
                'symbol': spec.get('symbol'), 'pricing_basis': spec.get('pricing_basis'),
                'requested_at': requested_at, 'policy': spec}
    if any(result_row.get(key) != value for key, value in expected.items()):
        raise ValueError('result_row_identity_mismatch')
    attempts = _row_attempts(result_row, pair_id)
    if set(attempts) != set(ids):
        raise ValueError('result_row_attempt_set_mismatch')
    for attempt in attempts.values():
        _verify_attempt(guard, attempt)

    # 동일 BUSY/LOCKED 만 약1초/최대10회 재시도한다(_store_result 와 같은 경계).
    deadline = time.monotonic() + 1.0
    for attempt in range(10):
        try:
            restored = _write_once(guard, pair_id, manifest_json, payload)
            break
        except (BudgetBlocked, ValueError):
            raise
        except (OSError, sqlite3.Error) as exc:
            code = getattr(exc, 'sqlite_errorcode', None)
            busy = isinstance(code, int) and (code & 0xFF) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
            remaining = deadline - time.monotonic()
            if busy and attempt < 9 and remaining > 0:
                time.sleep(min(0.01, remaining))
                continue
            # 커밋 응답 유실만 같은 바이트 읽기로 확인한다. 다른 값은 덮어쓰지 않는다.
            try:
                if _pair_storage(guard, pair_id)[0][2] == payload:
                    restored = True
                    break
            except (BudgetBlocked, TypeError):
                pass
            raise BudgetStateError('comparison_pair_storage_unavailable') from None
    return {'restored': restored, 'already_present': not restored, 'pair_id': pair_id,
            'result_sha256': hashlib.sha256(payload.encode()).hexdigest()}
