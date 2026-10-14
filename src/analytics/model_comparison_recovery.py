"""비교 시도·쌍의 증거 보존 조회와 명시 복구. 공급자 호출·월 정산·시각 생성이 없다.

- inspect_attempt / inspect_pair / export_incomplete_pair 는 읽기 전용 진단이다.
- restore_pair_result 는 PairResultPersistenceError.result 같은 **원래 행**만 받는다. 다음을 모두 만족해야
  ``UPDATE comparison_pairs SET result=? WHERE pair_id=? AND result IS NULL`` 한다.
  1) 작업자가 저장 실패 시점에 쓴 incomplete 저널(evidence_path)에 같은 행(정규화 JSON sha256)이 있다.
     완료 시각(decision_available_at·available_at)처럼 저장소에서 재계산할 수 없는 값은 이 증거로만 받는다.
  2) 저장 manifest·requested_at 과 행의 식별·정책·평가 기준시각이 같고, 행 수준 파생값(비용 합·시도 수·
     지연·경제성 false/None)이 시도들로부터 재계산한 값과 같다.
  3) 각 시도가 원장 저장 원본과 같다 — 저장 응답이 있으면 응답의 모든 키(실패면 러너의 같은 실패 함수로
     재계산), 없으면 원장의 차단 사유·상태와 행의 error·ledger_state 가 같다.
  현재 시각으로 어떤 시각 필드도 만들지 않고, 맞지 않으면 ValueError 로 아무것도 쓰지 않는다.
- 감사 대사로 정산된 비용은 inspect 의 ``reconciled`` 로만 보이고 원래 행·응답은 바꾸지 않는다.
- 남은 한계: arm 의 verdict·reasons·sizing_replay 는 재계산하지 않는다(1의 증거 일치에만 의존).
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
import sqlite3
import time

from .model_comparison_execution import ComparisonCallGuard
from .model_comparison_runner import BLOCKED_VERSION, _json, _sum_cost, failed_attempt_value, finalize_row
from ..utils.llm_budget import BudgetBlocked, BudgetStateError

# 러너가 모든 시도에 붙이는 키(저장 응답에 없는 키).
_RUNNER_KEYS = frozenset({'attempt_id', 'role', 'reservation_token', 'ledger_state', 'available_at'})
# 행 기록 뒤 감사 대사만 상태를 바꿀 수 있다. 그 외 상태 차이는 모순이다.
_RECONCILED_FROM = frozenset({'unknown', 'pending_reconciliation', 'blocked'})   # blocked: 응답 없이 끊긴 시도의 예약을 뒤에 대사
# 가드는 BudgetBlocked 가 아닌 예외(TimeoutError·ConnectionError 등)를 이 사유로만 남긴다 — 형식만 검사할 수 있다.
_INTERRUPTED = 'comparison_attempt_interrupted'
_EXCEPTION_NAME = re.compile(r'[A-Z][A-Za-z0-9_]{0,99}')
_EVIDENCE_MAX_BYTES = 64 * 1024 * 1024


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
    parsed = None if result is None else json.loads(result)
    # 입장 전 차단 표식(하루 쌍 상한·입력 크기)은 결과가 아니다.
    blocked = (parsed.get('reason') if isinstance(parsed, dict) and parsed.get('schema_version') == BLOCKED_VERSION
               else None)
    return {'pair_id': pair_id, 'found': True, 'manifest': json.loads(manifest),
            'manifest_sha256': hashlib.sha256(manifest.encode()).hexdigest(),
            'requested_at': requested_at, 'has_result': result is not None and blocked is None,
            'result': None if blocked is not None else parsed, 'blocked': blocked,
            'attempts': attempts, 'complete': result is not None and blocked is None}


def export_incomplete_pair(guard, pair_id) -> dict:
    """미완성 쌍의 진단 자료만 만든다. 결과 행이 아니며 재실행/정산 근거로 쓰지 않는다."""
    info = inspect_pair(guard, pair_id)
    if not info['found']:
        raise ValueError('pair_not_found')
    if info['complete']:
        raise ValueError('pair_complete')
    if info['blocked'] is not None:
        raise ValueError('pair_blocked_before_admission')
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


def _requested_model(spec, pair_id, attempt):
    """attempt_id(pair:role:index) 와 저장 spec 에서 그 시도가 요청했어야 할 모델을 구한다."""
    role, _, index = attempt['attempt_id'][len(pair_id) + 1:].rpartition(':')
    if attempt.get('role') != role or not index.isdigit():
        raise ValueError('result_row_attempt_identity_mismatch')
    controls = {'bear': spec.get('bear'), 'bull_current': spec.get('current'),
                'bull_candidate': spec.get('candidate')}.get(role)
    if role in ('current_fallback', 'candidate_fallback'):
        chain = spec.get('fallback_chain') or []
        controls = chain[int(index)] if int(index) < len(chain) else None
    if type(controls) is not dict:
        raise ValueError('result_row_attempt_identity_mismatch')
    return controls.get('model')


def _verify_attempt(guard, spec, pair_id, attempt):
    record = guard.lookup(attempt['attempt_id'])
    if record is None:
        raise ValueError('result_row_attempt_not_stored')
    if not _RUNNER_KEYS <= set(attempt):
        raise ValueError('result_row_attempt_identity_mismatch')
    requested_model = _requested_model(spec, pair_id, attempt)
    if attempt['reservation_token'] != record['reservation_token']:
        raise ValueError('result_row_reservation_mismatch')
    old, new = attempt['ledger_state'], record['state']
    if old != new and not (old in _RECONCILED_FROM and new == 'known_reconciled'):
        raise ValueError('result_row_ledger_state_mismatch')
    saved = record['result']
    if saved is not None and type(saved.value) is not dict:
        raise ValueError('result_row_stored_response_shape')
    body = {key: value for key, value in attempt.items() if key not in _RUNNER_KEYS}
    # error 유무로 검사를 건너뛰지 않는다. 실패 시도는 러너와 같은 함수로 저장 원본에서 다시 만든다.
    if 'error' not in attempt:
        if saved is None:
            raise ValueError('result_row_success_without_stored_response')
        expected = saved.value
    else:
        error = attempt['error']
        if not isinstance(error, str) or not error or len(error) > 200:
            raise ValueError('result_row_error_mismatch')
        if saved is None:
            # 응답이 없으면 원장의 차단 사유와 같아야 한다. 가드는 BudgetBlocked 가 아닌 예외를
            # comparison_attempt_interrupted 로만 남기므로 그때는 예외 클래스 이름 형식만 확인할 수 있다.
            reason = record['block_reason']
            if not (error == reason or (reason == _INTERRUPTED and _EXCEPTION_NAME.fullmatch(error))):
                raise ValueError('result_row_error_mismatch')
        not_sent = record['state'] == 'blocked' and record['reservation_token'] is None
        expected = failed_attempt_value(saved, not_sent=not_sent, error=error, requested_model=requested_model)
    if body != expected:
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


def restore_pair_result(guard, pair_id, result_row, *, manifest_json, evidence_path) -> dict:
    """evidence_path: 작업자 incomplete 저널(incomplete-YYYY-MM.jsonl). 같은 행 sha 가 없으면 쓰지 않는다."""
    from .model_comparison_worker import read_incomplete_journal

    _check_guard(guard)
    if type(result_row) is not dict or not isinstance(manifest_json, str):
        raise ValueError('original result row and manifest_json required')
    try:
        payload = _json(result_row)
        manifest = json.loads(manifest_json)
    except (TypeError, ValueError, RecursionError):
        raise ValueError('result_row_or_manifest_not_json') from None
    digest = hashlib.sha256(payload.encode()).hexdigest()
    row, ids = _pair_storage(guard, pair_id)
    if row is None:
        raise ValueError('pair_not_found')
    stored_manifest, requested_at, stored_result = row
    if stored_manifest != manifest_json or type(manifest) is not dict or type(manifest.get('spec')) is not dict:
        raise ValueError('pair_manifest_mismatch')
    if stored_result is not None:
        if stored_result == payload:
            return {'restored': False, 'already_present': True, 'pair_id': pair_id, 'result_sha256': digest}
        raise ValueError('pair_result_already_present')
    evidence = read_incomplete_journal(evidence_path, max_bytes=_EVIDENCE_MAX_BYTES)['records']
    if not any(r['row_sha256'] == digest and r['pair_id'] == pair_id for r in evidence):
        raise ValueError('result_row_not_in_incomplete_evidence')
    spec = manifest['spec']
    if manifest.get('schema_version') != result_row.get('schema_version'):
        raise ValueError('result_row_identity_mismatch')
    attempts = _row_attempts(result_row, pair_id)
    # 행 수준 파생값(arm·전체 비용 합, 시도 수, 지연, 경제성 false/None)·식별·평가 기준시각은
    # 저장 spec·requested_at·시도들로 다시 만든 행과 같아야 한다.
    try:
        arms = [deepcopy(result_row[name]) for name in ('current', 'candidate')]
        if any(arm.get('api_cost_usd') != _sum_cost(arm['attempts']) for arm in arms):
            raise ValueError('result_row_derived_mismatch')
        expected = finalize_row(pair_id, spec, requested_at, *arms)
    except (KeyError, TypeError, ValueError, ArithmeticError, AttributeError):
        raise ValueError('result_row_derived_mismatch') from None
    if expected != result_row:
        raise ValueError('result_row_derived_mismatch')
    if set(attempts) != set(ids):
        raise ValueError('result_row_attempt_set_mismatch')
    for attempt in attempts.values():
        _verify_attempt(guard, spec, pair_id, attempt)

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
