"""비교 정책의 명시 승인 등록부. 기존 singleton·요청·월 원장은 변경하지 않는다.

호출자는 같은 SQLite 트랜잭션에서 정책과 소유권을 읽는다. 신규 ID는 승인된
register_policy만 추가하며, singleton은 모든 실험에 적용되는 불변 전역 상한이다.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3

from ..utils.llm_budget import BudgetBlocked, BudgetStateError, MonthlyBudgetLedger

_TABLES = {'comparison_policy_lifecycle', 'comparison_policy_registry'}


def _sha(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _approval(approved_by, approved_plan_sha256):
    if not isinstance(approved_by, str) or not approved_by.strip():
        raise ValueError('approval_missing')
    if not isinstance(approved_plan_sha256, str) or re.fullmatch('[0-9a-f]{64}', approved_plan_sha256) is None:
        raise ValueError('approved_plan_mismatch')


def _policy(payload):
    from .model_comparison_execution import ComparisonExecutionPolicy
    try:
        raw = json.loads(payload)
        values = dict(raw)
        for key in ('schema_version', 'currency', 'timezone', 'adapter_contract'):
            values.pop(key)
        values['allowed_models'] = tuple(values['allowed_models'])
        policy = ComparisonExecutionPolicy(**values)
        if policy.serialized() != payload:
            raise ValueError('unsupported_policy_contract')
        return policy
    except (TypeError, ValueError, KeyError):
        raise BudgetStateError('comparison_storage_corrupt') from None


def _columns(db, table, expected, nullable=()):
    # 월 예약의 읽기 전용 authorizer는 PRAGMA도 금지한다. schema SQL과 SELECT의
    # 열 목록만 읽어 동일한 검사 경계를 유지한다(권한 확장 없음).
    cursor = db.execute('SELECT * FROM ' + table + ' LIMIT 0')
    sql = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]
    if [r[0] for r in cursor.description] != [r[0] for r in expected]:
        raise BudgetStateError('comparison_storage_corrupt')
    for name, kind, primary in expected:
        pattern = r'(?:\(|,)\s*' + name + r'\s+' + kind + r'\b([^,]*)'
        found = re.search(pattern, sql, re.IGNORECASE)
        if found is None or bool(re.search(r'\bPRIMARY\s+KEY\b', found[1], re.IGNORECASE)) != bool(primary):
            raise BudgetStateError('comparison_storage_corrupt')
        if not primary and name not in nullable and not re.search(r'\bNOT\s+NULL\b', found[1], re.IGNORECASE):
            raise BudgetStateError('comparison_storage_corrupt')


def read_policy_state(db):
    """읽기 전용으로 지원 스키마와 모든 등록을 검사해 anchor/policies를 반환한다."""
    names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    present = names & _TABLES
    if present and present != _TABLES:
        raise BudgetStateError('comparison_storage_corrupt')
    base = {'comparison_policy', 'comparison_attempts', 'comparison_requests'}
    if not names & base:
        if present or 'comparison_pairs' in names:
            raise BudgetStateError('comparison_storage_corrupt')
        return None, {}, False
    if not base <= names:
        raise BudgetStateError('comparison_storage_corrupt')
    _columns(db, 'comparison_policy', [('id', 'INTEGER', 1), ('policy', 'TEXT', 0), ('identity_version', 'INTEGER', 0)])
    _columns(db, 'comparison_attempts', [('slot', 'TEXT', 1), ('admission_day', 'TEXT', 0),
                                        ('reservation_token', 'TEXT', 0), ('state', 'TEXT', 0)],
             nullable=('reservation_token',))
    _columns(db, 'comparison_requests', [('attempt_id', 'TEXT', 1), ('request_sha256', 'TEXT', 0),
                                        ('request_json', 'TEXT', 0), ('slot', 'TEXT', 0),
                                        ('reservation_token', 'TEXT', 0), ('result_json', 'TEXT', 0),
                                        ('block_reason', 'TEXT', 0)],
             nullable=('reservation_token', 'result_json', 'block_reason'))
    request_schema = db.execute("SELECT sql FROM sqlite_master WHERE name='comparison_requests'").fetchone()[0]
    if re.search(r'\bslot\s+TEXT\s+NOT\s+NULL\s+UNIQUE\b', request_schema, re.IGNORECASE) is None:
        raise BudgetStateError('comparison_storage_corrupt')
    rows = db.execute('SELECT id,policy,identity_version FROM comparison_policy').fetchall()
    if len(rows) != 1 or rows[0][0] != 1:
        raise BudgetStateError('comparison_policy_mismatch')
    if rows[0][2] != 1:
        raise BudgetStateError('comparison_identity_version')
    anchor = _policy(rows[0][1])
    policies = {anchor.experiment_id: rows[0][1]}
    if present:
        _columns(db, 'comparison_policy_lifecycle', [('id', 'INTEGER', 1), ('version', 'INTEGER', 0),
                                                    ('anchor_sha256', 'TEXT', 0)])
        _columns(db, 'comparison_policy_registry', [('experiment_id', 'TEXT', 1), ('policy', 'TEXT', 0),
                                                   ('policy_sha256', 'TEXT', 0), ('approved_by', 'TEXT', 0),
                                                   ('approved_plan_sha256', 'TEXT', 0)])
        meta = db.execute('SELECT id,version,anchor_sha256 FROM comparison_policy_lifecycle').fetchall()
        if len(meta) != 1 or tuple(meta[0]) != (1, 1, _sha(rows[0][1])):
            raise BudgetStateError('comparison_storage_corrupt')
        registered = db.execute('SELECT experiment_id,policy,policy_sha256,approved_by,approved_plan_sha256 '
                                'FROM comparison_policy_registry').fetchall()
        if not registered:
            raise BudgetStateError('comparison_storage_corrupt')
        for row in registered:
            policy = _policy(row[1])
            try:
                _approval(row[3], row[4])
            except ValueError:
                raise BudgetStateError('comparison_storage_corrupt') from None
            if row[0] != policy.experiment_id or row[2] != _sha(row[1]):
                raise BudgetStateError('comparison_storage_corrupt')
            if row[0] in policies and policies[row[0]] != row[1]:
                raise BudgetStateError('comparison_policy_mismatch')
            policies[row[0]] = row[1]
    return anchor, policies, bool(present)


def policy_owner(payload, policies):
    try:
        raw = json.loads(payload)
        policy_json = raw['policy']
        policy = _policy(policy_json)
        if policies.get(policy.experiment_id) != policy_json:
            raise BudgetStateError('comparison_storage_corrupt')
        return policy.experiment_id
    except (TypeError, ValueError, KeyError):
        raise BudgetStateError('comparison_storage_corrupt') from None


def _reservation_owner(db, token, owner, model=None):
    rows = db.execute("SELECT r.model,p.task,p.role FROM reservations r LEFT JOIN policy_attempts p ON p.token=r.token "
                      "WHERE r.token=?", (token,)).fetchall()
    if (len(rows) != 1 or tuple(rows[0][1:]) != ('model_comparison', owner)
            or (model is not None and rows[0][0] != model)):
        raise BudgetStateError('comparison_storage_corrupt')


def _validate_requests(db, policies):
    tokens = set()
    rows = db.execute("SELECT r.request_json,r.request_sha256,r.reservation_token,r.result_json,r.block_reason,"
                      "a.slot,a.reservation_token,a.state FROM comparison_requests r "
                      "LEFT JOIN comparison_attempts a ON a.slot=r.slot")
    for payload, digest, token, result, reason, slot, slot_token, state in rows:
        if not isinstance(payload, str) or _sha(payload) != digest:
            raise BudgetStateError('comparison_storage_corrupt')
        owner = policy_owner(payload, policies)
        request = json.loads(payload).get('request')
        if not isinstance(request, dict) or request.get('billing_channel', 'api') not in ('api', 'subscription'):
            raise BudgetStateError('comparison_storage_corrupt')
        if slot is None and (reason != 'comparison_daily_attempt_cap' or token is not None or result is not None):
            raise BudgetStateError('comparison_storage_corrupt')
        if token != slot_token:
            raise BudgetStateError('comparison_storage_corrupt')
        api = request.get('billing_channel', 'api') == 'api'
        if token is None:
            # 예약 전 admitted/blocked와 구독 시도는 합법적이다. 유료 응답이나
            # 예약 이후 상태가 있는데 양쪽 토큰이 모두 사라진 것은 손상이다.
            if api and (result is not None or state in ('reserved', 'known', 'unknown')):
                raise BudgetStateError('comparison_storage_corrupt')
        else:
            if not api or token in tokens:
                raise BudgetStateError('comparison_storage_corrupt')
            _reservation_owner(db, token, owner, request.get('model'))
            tokens.add(token)
    # 신원 도입 이전 슬롯은 요청 JSON이 없을 수 있다. 기존 anchor의 비용 연결을
    # 검사하되 행을 생성하거나 재작성하지 않고 global 슬롯으로 계속 센다.
    anchor = _policy(db.execute('SELECT policy FROM comparison_policy WHERE id=1').fetchone()[0]).experiment_id
    for token, in db.execute("SELECT a.reservation_token FROM comparison_attempts a "
                             "LEFT JOIN comparison_requests r ON r.slot=a.slot "
                             "WHERE r.slot IS NULL AND a.reservation_token IS NOT NULL"):
        if token in tokens:
            raise BudgetStateError('comparison_storage_corrupt')
        _reservation_owner(db, token, anchor)
        tokens.add(token)


def _validate_pairs(db, policies):
    from .model_comparison_execution import _kst_date
    from .model_comparison_runner import BLOCKED_VERSION, _time
    columns = [('pair_id', 'TEXT', 1), ('manifest', 'TEXT', 0), ('requested_at', 'TEXT', 0),
               ('result', 'TEXT', 0), ('admission_day', 'TEXT', 0)]
    actual = [r[0] for r in db.execute('SELECT * FROM comparison_pairs LIMIT 0').description]
    if actual == [r[0] for r in columns[:-1]]:
        _columns(db, 'comparison_pairs', columns[:-1], nullable=('result',))
        # 입장 이력이 없는 옛 빈 테이블만 runner가 열을 추가할 수 있다.
        # 행이 있으면 날짜를 추정/보정하지 않고 보수적으로 중단한다.
        if db.execute('SELECT 1 FROM comparison_pairs LIMIT 1').fetchone() is not None:
            raise BudgetStateError('comparison_storage_corrupt')
        return
    _columns(db, 'comparison_pairs', columns, nullable=('result', 'admission_day'))
    for pair_id, manifest, requested_at, result, day in db.execute('SELECT * FROM comparison_pairs'):
        policy_owner(manifest, policies)
        try:
            _time(requested_at)
            parsed = json.loads(result) if result is not None else None
            blocked = isinstance(parsed, dict) and parsed.get('schema_version') == BLOCKED_VERSION
            if day is None:
                if (not blocked or set(parsed) != {'schema_version', 'reason', 'blocked_at'}
                        or parsed['reason'] not in ('comparison_daily_pair_cap', 'comparison_input_too_large')
                        or parsed['blocked_at'] != requested_at):
                    raise ValueError('invalid_pre_admission_marker')
                prefix = pair_id + ':'
                if db.execute('SELECT 1 FROM comparison_requests WHERE substr(attempt_id,1,?)=? LIMIT 1',
                              (len(prefix), prefix)).fetchone() is not None:
                    raise ValueError('blocked_pair_has_attempts')
            else:
                _kst_date(day, 'admission_day')
                if blocked:
                    raise ValueError('blocked_pair_admitted')
        except (TypeError, ValueError, KeyError):
            raise BudgetStateError('comparison_storage_corrupt') from None


def validate_history(db, policies):
    """JSON 소유 정책·슬롯·월 예약·비용 mapping을 같은 읽기 snapshot에서 대조한다."""
    names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'comparison_requests' in names:
        _validate_requests(db, policies)
    if 'comparison_pairs' in names:
        _validate_pairs(db, policies)
    if 'policy_attempts' in names:
        for row in db.execute("SELECT DISTINCT role FROM policy_attempts WHERE task='model_comparison'"):
            if row[0] not in policies:
                raise BudgetStateError('comparison_policy_mismatch')


def check_policy(db, policy_json):
    anchor, policies, _ = read_policy_state(db)
    policy = _policy(policy_json)
    if anchor is None or policies.get(policy.experiment_id) != policy_json:
        raise BudgetStateError('comparison_policy_mismatch')
    validate_history(db, policies)
    return anchor, policies


def admission_counts(db, day, experiment_id, policies, *, pairs=False):
    if pairs:
        rows = db.execute('SELECT pair_id,manifest FROM comparison_pairs WHERE admission_day=?', (day,)).fetchall()
        local = {row[0] for row in rows if policy_owner(row[1], policies) == experiment_id}
        return len(rows), local
    rows = db.execute('SELECT a.slot,r.request_json FROM comparison_attempts a LEFT JOIN comparison_requests r '
                      'ON r.slot=a.slot WHERE a.admission_day=?', (day,)).fetchall()
    # 신원 도입 이전의 orphan 슬롯은 전역에 반드시 계수한다. 새 실험의 소유로 추정하지 않는다.
    local = sum(row[1] is not None and policy_owner(row[1], policies) == experiment_id for row in rows)
    return len(rows), local


def registration_status(db, policy):
    anchor, policies, _ = read_policy_state(db)
    validate_history(db, policies)
    saved = policies.get(policy.experiment_id)
    if saved is not None:
        if saved != policy.serialized():
            raise BudgetStateError('comparison_policy_mismatch')
        return 'matching_policy'
    return 'registration_required' if anchor is not None else 'no_existing_comparison_policy'


def register_policy(*, ledger, policy, approved_by, approved_plan_sha256):
    """동일 정책·동일 승인만 멱등. DDL과 INSERT는 하나의 원자 트랜잭션이다."""
    from .model_comparison_execution import ComparisonCallGuard, ComparisonExecutionPolicy
    if not isinstance(ledger, MonthlyBudgetLedger) or not isinstance(policy, ComparisonExecutionPolicy):
        raise ValueError('explicit_ledger_and_policy_required')
    _approval(approved_by, approved_plan_sha256)
    try:
        with ledger._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            ledger._settings(db)
            registration_status(db, policy)
            anchor, _, initialized = read_policy_state(db)
            if anchor is None:
                # 최초 정책도 동일 트랜잭션 안에서만 생성한다. 기존 원장의 월 설정은 읽기만 한다.
                ComparisonCallGuard._initialize_schema(db, policy.serialized())
                anchor, _, initialized = read_policy_state(db)
            if not initialized:
                db.execute('CREATE TABLE comparison_policy_lifecycle (id INTEGER PRIMARY KEY CHECK(id=1), '
                           'version INTEGER NOT NULL, anchor_sha256 TEXT NOT NULL)')
                db.execute('INSERT INTO comparison_policy_lifecycle VALUES (1,1,?)', (_sha(anchor.serialized()),))
                db.execute('CREATE TABLE comparison_policy_registry (experiment_id TEXT PRIMARY KEY, '
                           'policy TEXT NOT NULL, policy_sha256 TEXT NOT NULL, approved_by TEXT NOT NULL, '
                           'approved_plan_sha256 TEXT NOT NULL)')
            previous = db.execute('SELECT policy,policy_sha256,approved_by,approved_plan_sha256 '
                                  'FROM comparison_policy_registry WHERE experiment_id=?', (policy.experiment_id,)).fetchone()
            expected = (policy.serialized(), _sha(policy.serialized()), approved_by, approved_plan_sha256)
            if previous is not None:
                if tuple(previous) != expected:
                    raise BudgetStateError('comparison_policy_mismatch')
            else:
                db.execute('INSERT INTO comparison_policy_registry VALUES (?,?,?,?,?)', (policy.experiment_id, *expected))
            check_policy(db, policy.serialized())
            db.execute('COMMIT')
    except BudgetBlocked:
        raise
    except (OSError, sqlite3.Error, TypeError, ValueError):
        raise BudgetStateError('comparison_storage_unavailable') from None
