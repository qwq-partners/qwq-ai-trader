"""비교 실행기의 명시 주입형 호출·복구 경계 — 이 모듈은 공급자에 직접 연결하지 않는다.

이미 열린 MonthlyBudgetLedger와 고정 정책을 주입한 뒤, 재시도·폴백을 포함한
공급자 시도마다 ``await guard.call(request, dispatch=adapter, attempt_id=stable_id)``를 호출한다.
ID·전체 요청/정책 지문·일 슬롯을 원자적으로 고정하고 응답 JSON을 월 정산 전에 보존한다.
같은 ID의 완료 응답은 원래 시각/비용으로 복구하며 pending/blocked는 재전송하지 않는다.
정산 불확실성은 pending_reconciliation으로 남겨 수동 대사가 필요함을 알린다.
어댑터는 지정 모델·출력 상한으로 한 번만 전송하고 측정 비용(불명은 None)을
돌려줘야 한다. LLMManager는 자체 예약·다중 시도를 소유하므로 연결하면 안 된다.
실제 어댑터는 별도 검토가 필요하며 임의 콜백의 숨은 재시도를 여기서 증명하지 않는다.

명시 실험 정책과 KST 입장 슬롯만 기존 SQLite 원장에 추가한다. 월 설정·활성화
기준선은 생성하거나 재설정하지 않는다. 일 슬롯 확정 → 월 예약 → 전송 순서이며,
월 차단·충돌·프로세스 종료에도 슬롯을 환급하지 않아 초과 호출보다 과소 사용을 택한다.
이는 시도 상한이며 엔진의 일 판단 상한이나 전체 재생·보고서 구현이 아니다.

v2 입장 조건은 월 예약과 같은 BEGIN IMMEDIATE 안에서 같은 units 로 검사한다.
정책이 켜진 원장의 normal 단계만 허용하고, 이번 예약을 포함한 월 사용액이 80%를
**넘으면**(정확히 80%는 허용) 거부한다. 95% 필수 보호·100% 중지는 기존 원장 검사가
먼저 적용한다. 실험 상한은 월 집계와 별개인 실험 ID 전체의 예약/정산 합이며,
월 전환·재시작·ID 변경(저장 정책 불일치로 거부)이 상한을 되살리지 않는다.
SQLite 처리는 동기식이다. 별도 오프라인 작업 프로세스용이며 실시간 엔진의
이벤트 루프에 비차단으로 통합됐다고 해석하면 안 된다.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, localcontext
import json
import hashlib
import math
import re
import sqlite3
import time
from typing import Any, Awaitable, Callable
import uuid

from ..utils.llm_budget import BudgetBlocked, BudgetStateError, MonthlyBudgetLedger, _MAX_UNITS, _SCALE, _units
from .model_comparison_budget import _d, _integer

SCHEMA_VERSION = "model-comparison-execution-v2"
_SUPPORTED_POLICY_MODES = ("normal",)


def _kst_date(value, name):
    if not isinstance(value, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
        raise ValueError(f"{name}는 KST YYYY-MM-DD 문자열이어야 한다")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{name}는 실제 날짜여야 한다") from None


def _ceiling_units(value) -> int:
    """실험 상한(USD Decimal 문자열)을 nano-USD로 내림한다. 상한은 보수적으로 작게 잡는다."""
    if not isinstance(value, str):
        raise ValueError("experiment_usd_ceiling은 Decimal 문자열이어야 한다")
    amount = _d(value, "experiment_usd_ceiling")
    if amount >= Decimal(_MAX_UNITS) / _SCALE:
        raise ValueError("experiment_usd_ceiling이 원장 범위를 초과한다")
    units = int((amount * _SCALE).to_integral_value(rounding=ROUND_FLOOR))
    if units <= 0:
        raise ValueError("experiment_usd_ceiling은 1 nano-USD 이상이어야 한다")
    return units


@dataclass(frozen=True)
class ComparisonExecutionPolicy:
    experiment_id: str
    daily_attempt_cap: int
    allowed_models: tuple[str, ...]
    max_output_tokens: int
    experiment_usd_ceiling: str
    valid_from: str
    valid_until: str
    required_policy_mode: str = "normal"

    def __post_init__(self):
        if not isinstance(self.experiment_id, str) or not self.experiment_id.strip():
            raise ValueError("experiment_id는 비어 있지 않은 문자열이어야 한다")
        _integer(self.daily_attempt_cap, "daily_attempt_cap")
        _integer(self.max_output_tokens, "max_output_tokens", 1)
        if self.daily_attempt_cap > 2 ** 31 - 1 or self.max_output_tokens > 2 ** 31 - 1:
            raise ValueError("daily_attempt_cap·max_output_tokens는 int32 범위여야 한다")
        if (not isinstance(self.allowed_models, tuple) or not self.allowed_models
                or any(not isinstance(model, str) or not model.strip() for model in self.allowed_models)
                or len(set(self.allowed_models)) != len(self.allowed_models)):
            raise ValueError("allowed_models는 중복 없는 모델명의 비어 있지 않은 tuple이어야 한다")
        _ceiling_units(self.experiment_usd_ceiling)
        _kst_date(self.valid_from, "valid_from")
        _kst_date(self.valid_until, "valid_until")
        if self.valid_from > self.valid_until:
            raise ValueError("valid_from은 valid_until 이전이어야 한다")
        if self.required_policy_mode not in _SUPPORTED_POLICY_MODES:
            raise ValueError("required_policy_mode는 normal만 지원한다")

    def serialized(self) -> str:
        return json.dumps({**asdict(self), "schema_version": SCHEMA_VERSION,
                           "currency": "USD", "timezone": "Asia/Seoul",
                           "adapter_contract": "single-provider-attempt-v1"},
                          sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class ComparisonRequest:
    model: str
    prompt: str
    system: str
    max_output_tokens: int
    reasoning_effort: str | None = None
    temperature: float = 0.3
    seed: int | None = None
    timeout_seconds: float = 12.0

    def __post_init__(self):
        if not isinstance(self.model, str) or not self.model:
            raise ValueError("model은 비어 있지 않은 문자열이어야 한다")
        if not isinstance(self.prompt, str) or not isinstance(self.system, str):
            raise ValueError("prompt와 system은 문자열이어야 한다")
        _integer(self.max_output_tokens, "max_output_tokens", 1)
        if self.reasoning_effort is not None and (
                not isinstance(self.reasoning_effort, str) or not self.reasoning_effort.strip()):
            raise ValueError("reasoning_effort는 비어 있지 않은 문자열 또는 None이어야 한다")
        if (isinstance(self.temperature, bool) or not isinstance(self.temperature, (int, float))
                or not math.isfinite(self.temperature) or not 0 <= self.temperature <= 2):
            raise ValueError("temperature는 유한한 0~2 숫자여야 한다")
        if self.seed is not None and (isinstance(self.seed, bool) or not isinstance(self.seed, int)
                                      or not -(2 ** 63) <= self.seed < 2 ** 63):
            raise ValueError("seed는 signed64 정수 또는 None이어야 한다")
        if (isinstance(self.timeout_seconds, bool) or not isinstance(self.timeout_seconds, (int, float))
                or not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0):
            raise ValueError("timeout_seconds는 유한한 양수여야 한다")


@dataclass(frozen=True)
class ComparisonAttemptResult:
    value: Any
    actual_cost_usd: Decimal | str | int | float | None


def _ledger_cost(value) -> float | None:
    """원장의 nano-USD 올림과 일치하는 값을 반환한다. 변환 불가는 ValueError다."""
    if value is None:
        return None
    cost = _d(value, "actual_cost_usd")
    if cost >= Decimal(_MAX_UNITS) / Decimal(1_000_000_000):
        raise ValueError("actual_cost_usd가 원장 범위를 초과한다")
    with localcontext() as context:
        context.prec = max(28, len(cost.as_tuple().digits) + 10)
        # 극소 양수도 Decimal 문맥의 underflow로 0원이 되지 않게 먼저 분기한다.
        exact_units = (1 if 0 < cost < Decimal("0.000000001") else
                       int((cost * Decimal(1_000_000_000)).to_integral_value(rounding=ROUND_CEILING)))
        candidate = float(Decimal(exact_units) / Decimal(1_000_000_000))
    try:
        if _units(candidate) == exact_units:
            return candidate
    except BudgetBlocked:
        pass
    # nano-USD 단위까지 보존할 수 없으면 거절한다. 호출자가 불명 정산을 소유한다.
    raise ValueError("actual_cost_usd를 원장 정밀도로 안전하게 전달할 수 없다")


def _json_value(value):
    """JSON 복구 전후 자료형이 달라지는 tuple·비문자 키·NaN 등을 거절한다."""
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            _json_value(item)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _json_value(item)
        return
    raise ValueError("응답 value는 유한한 JSON 자료여야 한다")


def _result_json(result: ComparisonAttemptResult) -> str:
    _json_value(result.value)
    cost = result.actual_cost_usd
    kind = "decimal" if isinstance(cost, Decimal) else type(cost).__name__
    return json.dumps({"value": result.value, "cost_type": kind,
                       "cost": str(cost) if isinstance(cost, Decimal) else cost},
                      sort_keys=True, separators=(",", ":"), allow_nan=False)


def _read_result(payload: str) -> ComparisonAttemptResult:
    raw = json.loads(payload)
    cost = Decimal(raw["cost"]) if raw["cost_type"] == "decimal" else raw["cost"]
    result = ComparisonAttemptResult(raw["value"], cost)
    if _result_json(result) != payload:
        raise ValueError("응답 JSON 형식이 일치하지 않는다")
    _ledger_cost(cost)
    return result


class ComparisonCallGuard:
    """공유 월 예약 원장을 사용하는 명시 단일 시도 경계.

    정책 하나를 호출자 ID가 아닌 원장 전체에 결합해 ID·상한 변경으로 집계가
    초기화되지 않는다. 정책 이전은 미지원이며 월 원장 자체를 생성하지 않는다.
    """

    def __init__(self, *, ledger: MonthlyBudgetLedger, policy: ComparisonExecutionPolicy):
        if not isinstance(ledger, MonthlyBudgetLedger):
            raise ValueError("명시 MonthlyBudgetLedger 주입이 필요하다")
        if not isinstance(policy, ComparisonExecutionPolicy):
            raise ValueError("명시 ComparisonExecutionPolicy 주입이 필요하다")
        self.ledger = ledger
        self.policy = policy
        self._policy_json = policy.serialized()
        self._ceiling = _ceiling_units(policy.experiment_usd_ceiling)
        self._initialize()

    def _initialize(self):
        try:
            # 기존 rw 연결 경계만 사용한다. DB·월 설정·활성화 표식·기준선은 생성/교체하지 않는다.
            with self.ledger._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                self.ledger._settings(db)
                names = {row[0] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
                    "('comparison_policy','comparison_attempts')")}
                if not names:
                    db.execute("CREATE TABLE comparison_policy (id INTEGER PRIMARY KEY CHECK(id=1), policy TEXT NOT NULL)")
                    db.execute("INSERT INTO comparison_policy VALUES (1,?)", (self._policy_json,))
                    db.execute("""CREATE TABLE comparison_attempts (
                        slot TEXT PRIMARY KEY, admission_day TEXT NOT NULL,
                        reservation_token TEXT, state TEXT NOT NULL
                        CHECK(state IN ('admitted','reserved','known','unknown','blocked')))
                    """)
                    db.execute("CREATE INDEX comparison_attempt_day ON comparison_attempts(admission_day)")
                elif names != {"comparison_policy", "comparison_attempts"}:
                    raise BudgetStateError("comparison_storage_corrupt")
                self._check_policy(db)
                db.execute("SELECT slot,admission_day,reservation_token,state FROM comparison_attempts LIMIT 1")
                columns = {row[1] for row in db.execute("PRAGMA table_info(comparison_policy)")}
                if "identity_version" not in columns:
                    db.execute("ALTER TABLE comparison_policy ADD COLUMN identity_version INTEGER NOT NULL DEFAULT 1")
                    db.execute("""CREATE TABLE comparison_requests (
                        attempt_id TEXT PRIMARY KEY, request_sha256 TEXT NOT NULL,
                        request_json TEXT NOT NULL, slot TEXT NOT NULL UNIQUE,
                        reservation_token TEXT, result_json TEXT, block_reason TEXT)""")
                elif db.execute("SELECT identity_version FROM comparison_policy").fetchone()[0] != 1:
                    raise BudgetStateError("comparison_identity_version")
                db.execute("SELECT attempt_id,request_sha256,request_json,slot,reservation_token,result_json,block_reason "
                           "FROM comparison_requests LIMIT 1")
                db.execute("COMMIT")
        except BudgetBlocked:
            raise
        except (OSError, sqlite3.Error, TypeError, ValueError):
            raise BudgetStateError("comparison_storage_unavailable") from None

    def _check_policy(self, db):
        rows = db.execute("SELECT id,policy FROM comparison_policy").fetchall()
        if len(rows) != 1 or rows[0][0] != 1 or rows[0][1] != self._policy_json:
            raise BudgetStateError("comparison_policy_mismatch")
        # 메모리 정책의 우발적 변경/교체도 거부한다.
        if self.policy.serialized() != self._policy_json:
            raise BudgetStateError("comparison_policy_mismatch")

    @staticmethod
    def _attempt_id(attempt_id):
        if not isinstance(attempt_id, str) or not attempt_id.strip() or len(attempt_id) > 512:
            raise ValueError("attempt_id는 1~512자 식별자여야 한다")
        return attempt_id

    @staticmethod
    def _reconciled(db, token, reservation):
        """기존 대사 CLI의 감사 행에서 이 토큰의 증거 기반 정정 비용을 찾는다(읽기만)."""
        if token is None or reservation is None or db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='reconciliations'").fetchone() is None:
            return None
        found = []
        for audit in db.execute("SELECT id,applied_at,reviewer_ref,evidence_sha256,adjustments_json "
                                "FROM reconciliations WHERE instr(adjustments_json,?)>0 ORDER BY id", (token,)):
            found += [(audit, item) for item in json.loads(audit["adjustments_json"])
                      if isinstance(item, dict) and item.get("token") == token]
        if not found:
            return None
        if (len(found) != 1 or reservation["state"] != "settled"
                or reservation["actual_units"] != _units(found[0][1].get("actual_cost_usd"))):
            raise BudgetStateError("comparison_reconciliation_mismatch")
        audit = found[0][0]
        return {"actual_cost_usd": str(Decimal(reservation["actual_units"]) / _SCALE),
                "reconciliation_id": audit["id"], "applied_at": audit["applied_at"],
                "reviewer_ref": audit["reviewer_ref"], "evidence_sha256": audit["evidence_sha256"]}

    def _lookup_row(self, db, row):
        if hashlib.sha256(row["request_json"].encode()).hexdigest() != row["request_sha256"]:
            raise BudgetStateError("comparison_storage_corrupt")
        result = _read_result(row["result_json"]) if row["result_json"] is not None else None
        token = row["reservation_token"]
        reservation = (db.execute("SELECT state,actual_units FROM reservations WHERE token=?", (token,)).fetchone()
                       if token is not None else None)
        reconciled = self._reconciled(db, token, reservation)
        state, reason = "pending", "comparison_attempt_pending"
        if row["block_reason"]:
            state, reason = "blocked", row["block_reason"]
        if result is not None:
            state, reason = "pending_reconciliation", "comparison_pending_reconciliation"
            cost = _ledger_cost(result.actual_cost_usd)
            if reservation is not None:
                if reconciled is not None:
                    # 원래 미확정 예약이 감사 대사로 정산됐다. 응답·원래 추정 비용은 바꾸지 않는다.
                    state, reason = "known_reconciled", None
                elif cost is None and reservation["state"] == "unknown":
                    state, reason = "unknown", None
                elif cost is not None and reservation["state"] == "settled":
                    if reservation["actual_units"] != _units(cost):
                        raise BudgetStateError("comparison_settlement_mismatch")
                    state, reason = "known", None
        estimate = result.value.get("api_cost_usd") if result is not None and isinstance(result.value, dict) else None
        return {"attempt_id": row["attempt_id"], "request_sha256": row["request_sha256"],
                "state": state, "reservation_token": token,
                "result": result, "block_reason": reason,
                "original_estimated_cost_usd": estimate, "reconciled": reconciled}

    def lookup(self, attempt_id: str) -> dict | None:
        """읽기 전용 복구 조회. known/unknown/known_reconciled만 정산 확인, pending_reconciliation은 재전송 금지.

        reconciled 는 감사 대사(reconciliations)로 확인된 정정 비용·감사 참조이며,
        original_estimated_cost_usd 는 저장 응답의 api_cost_usd 다. 둘 다 원래 result 를 바꾸지 않는다.

        result는 원래 시각을 포함한 JSON value와 원래 비용 자료형을 보존한다.
        정산되지 않은 결과도 증거로 제공하지만 call은 이를 완료로 반환하지 않는다.
        """
        self._attempt_id(attempt_id)
        try:
            with self.ledger._connect(readonly=True) as db:
                db.execute("BEGIN")
                self._check_policy(db)
                row = db.execute("SELECT * FROM comparison_requests WHERE attempt_id=?", (attempt_id,)).fetchone()
                return self._lookup_row(db, row) if row is not None else None
        except BudgetBlocked:
            raise
        except (OSError, sqlite3.Error, TypeError, ValueError, ArithmeticError):
            raise BudgetStateError("comparison_storage_unavailable") from None

    def _admit(self, attempt_id: str, request_json: str):
        try:
            with self.ledger._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                self._check_policy(db)
                existing = db.execute("SELECT * FROM comparison_requests WHERE attempt_id=?", (attempt_id,)).fetchone()
                if existing is not None:
                    if existing["request_json"] != request_json:
                        raise BudgetBlocked("comparison_attempt_mismatch")
                    recovered = self._lookup_row(db, existing)
                    if recovered["state"] not in {"known", "unknown", "known_reconciled"}:
                        raise BudgetBlocked(recovered["block_reason"])
                    return None, None, recovered["result"]
                day = self.ledger.today().isoformat()
                used = db.execute("SELECT COUNT(*) FROM comparison_attempts WHERE admission_day=?", (day,)).fetchone()[0]
                slot = uuid.uuid4().hex
                fingerprint = hashlib.sha256(request_json.encode()).hexdigest()
                if used >= self.policy.daily_attempt_cap:
                    # 미전송 차단 ID도 고정한다. 날짜 변경이 같은 논리 시도의 자동 재시도가 되면 안 된다.
                    db.execute("INSERT INTO comparison_requests VALUES (?,?,?,?,NULL,NULL,?)",
                               (attempt_id, fingerprint, request_json, slot, "comparison_daily_attempt_cap"))
                    db.execute("COMMIT")
                    raise BudgetBlocked("comparison_daily_attempt_cap")
                db.execute("INSERT INTO comparison_attempts VALUES (?,?,NULL,'admitted')", (slot, day))
                db.execute("INSERT INTO comparison_requests VALUES (?,?,?,?,NULL,NULL,NULL)",
                           (attempt_id, fingerprint, request_json, slot))
                db.execute("COMMIT")
                return slot, day, None
        except BudgetBlocked:
            raise
        except (OSError, sqlite3.Error, TypeError, ValueError, ArithmeticError):
            raise BudgetStateError("comparison_storage_unavailable") from None

    def _stored_result_matches(self, attempt_id: str, token: str, payload: str) -> bool:
        try:
            with self.ledger._connect(readonly=True) as db:
                db.execute("BEGIN")
                self._check_policy(db)
                row = db.execute("SELECT reservation_token,result_json,block_reason FROM comparison_requests "
                                 "WHERE attempt_id=?", (attempt_id,)).fetchone()
                return row is not None and tuple(row) == (token, payload, None)
        except (BudgetBlocked, OSError, sqlite3.Error, TypeError, ValueError):
            return False

    def _write_result_once(self, attempt_id: str, token: str, payload: str):
        with self.ledger._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._check_policy(db)
            row = db.execute("SELECT reservation_token,result_json,block_reason FROM comparison_requests "
                             "WHERE attempt_id=?", (attempt_id,)).fetchone()
            if row is None or row["block_reason"] is not None:
                raise BudgetStateError("comparison_storage_corrupt")
            if row["result_json"] is not None:
                if tuple(row) != (token, payload, None):
                    raise BudgetStateError("comparison_result_mismatch")
                return  # 이전 COMMIT 응답 유실도 같은 토큰·바이트일 때만 성공으로 복구한다.
            changed = db.execute("UPDATE comparison_requests SET reservation_token=?,result_json=? "
                                 "WHERE attempt_id=? AND result_json IS NULL AND block_reason IS NULL",
                                 (token, payload, attempt_id)).rowcount
            if changed != 1:
                raise BudgetStateError("comparison_storage_corrupt")
            db.execute("COMMIT")

    def _store_result(self, attempt_id: str, token: str, payload: str):
        # 오프라인 작업 프로세스에서만 사용한다. 동일 응답 쓰기에 한해 BUSY/LOCKED를
        # 약 1초/최대 10회까지 재시도한다. 공급자 호출이나 월 정산은 재시도하지 않는다.
        deadline = time.monotonic() + 1.0
        for attempt in range(10):
            try:
                self._write_result_once(attempt_id, token, payload)
                return
            except BudgetBlocked:
                raise
            except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
                code = getattr(exc, "sqlite_errorcode", None)
                busy = isinstance(code, int) and (code & 0xFF) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
                remaining = deadline - time.monotonic()
                if busy and attempt < 9 and remaining > 0:
                    time.sleep(min(0.01, remaining))
                    continue
                # I/O 오류는 재쓰기하지 않는다. 이미 커밋된 동일 결과만 읽어서 확인한다.
                if self._stored_result_matches(attempt_id, token, payload):
                    return
                raise BudgetStateError("comparison_storage_unavailable") from None

    def _mark_failed(self, attempt_id: str, token: str | None, reason: str):
        # 저장된 응답은 실패 처리로 덮어쓰지 않는다. 정산 여부는 월 원장에서 판독한다.
        with self.ledger._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._check_policy(db)
            db.execute("UPDATE comparison_requests SET reservation_token=?,block_reason=? "
                       "WHERE attempt_id=? AND result_json IS NULL", (token, reason, attempt_id))
            db.execute("COMMIT")

    def _record(self, slot: str, token: str | None, state: str):
        try:
            with self.ledger._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                self._check_policy(db)
                changed = db.execute("UPDATE comparison_attempts SET reservation_token=?,state=? WHERE slot=?",
                                     (token, state, slot)).rowcount
                if changed != 1:
                    raise BudgetStateError("comparison_storage_corrupt")
                db.execute("COMMIT")
        except BudgetBlocked:
            raise
        except (OSError, sqlite3.Error, TypeError, ValueError):
            raise BudgetStateError("comparison_storage_unavailable") from None

    def _admission_check(self, db, status, committed, units, now):
        """MonthlyBudgetLedger.reserve 안(같은 잠금·같은 units)에서만 호출된다. 읽기 전용."""
        self._check_policy(db)
        if not self.ledger.policy_enabled or "policy_mode" not in status:
            raise BudgetBlocked("comparison_policy_disabled")
        if status["policy_mode"] != self.policy.required_policy_mode:
            raise BudgetBlocked("comparison_policy_mode_not_normal")
        if not self.policy.valid_from <= now.date().isoformat() <= self.policy.valid_until:
            raise BudgetBlocked("comparison_experiment_expired")
        # 이번 예약을 포함해 80%를 넘지 않아야 한다. 정확히 80%는 허용한다.
        if (committed + units) * 100 > self.ledger.limit_units * 80:
            raise BudgetBlocked("comparison_economy_threshold")
        used = db.execute("""SELECT COALESCE(SUM(CASE WHEN r.state IN ('reserved','unknown') THEN r.reserved_units
                                                     WHEN r.state='settled' THEN r.actual_units ELSE 0 END),0)
                             FROM reservations r JOIN policy_attempts p ON p.token=r.token
                             WHERE p.task='model_comparison' AND p.role=?""",
                          (self.policy.experiment_id,)).fetchone()[0]
        if not isinstance(used, int) or used < 0:
            raise BudgetStateError("comparison_storage_corrupt")
        if used + units > self._ceiling:
            raise BudgetBlocked("comparison_experiment_ceiling")

    async def call(self, request: ComparisonRequest, *,
                   dispatch: Callable[[ComparisonRequest], Awaitable[ComparisonAttemptResult]],
                   attempt_id: str | None = None) -> ComparisonAttemptResult:
        """같은 명시 ID는 한 번만 전송한다. 완료 결과만 원래 값으로 복구한다.

        입력 오류는 ValueError, 계약/저장소 오류는 BudgetBlocked다. 어댑터 예외/취소는
        원형을 유지한다. pending/blocked ID는 자동 재시도하지 않는다. ID 생략은
        호환용 새 시도이며 재시작 복구가 필요한 실행기는 반드시 안정 ID를 넘겨야 한다.
        """
        if not isinstance(request, ComparisonRequest) or not callable(dispatch):
            raise ValueError("ComparisonRequest와 단일 시도 async dispatch가 필요하다")
        if request.model not in self.policy.allowed_models:
            raise BudgetBlocked("comparison_model_not_allowed")
        if request.max_output_tokens > self.policy.max_output_tokens:
            raise BudgetBlocked("comparison_output_cap")
        attempt_id = self._attempt_id(attempt_id if attempt_id is not None else uuid.uuid4().hex)
        payload = json.dumps({"request": asdict(request), "policy": self._policy_json},
                             sort_keys=True, separators=(",", ":"), allow_nan=False)
        slot, day, recovered = self._admit(attempt_id, payload)
        if recovered is not None:
            return recovered
        token = None
        entered_dispatch = settlement_attempted = False
        outcome, cost = "unknown", None
        try:
            token = self.ledger.reserve(request.model, request.prompt, request.system, request.max_output_tokens,
                                        policy_priority="research", task="model_comparison",
                                        role=self.policy.experiment_id,
                                        admission_check=self._admission_check)
            # ID/슬롯이 이미 확정됐다. 전송 전 부가 쓰기는 하지 않는다.
            if self.ledger.today().isoformat() != day:
                raise BudgetBlocked("comparison_admission_day_changed")
            entered_dispatch = True
            result = await dispatch(request)
            if not isinstance(result, ComparisonAttemptResult):
                raise BudgetBlocked("comparison_invalid_result")
            try:
                cost = _ledger_cost(result.actual_cost_usd)
            except (ValueError, ArithmeticError):
                raise BudgetBlocked("comparison_invalid_measured_cost") from None
            outcome = "unknown" if cost is None else "known"
            try:
                result_payload = _result_json(result)
            except (ValueError, TypeError, RecursionError):
                raise BudgetBlocked("comparison_invalid_result") from None
            # 월 정산 전에 응답·토큰을 보존한다. 그 뒤 중단돼도 재전송 없이 대사한다.
            self._store_result(attempt_id, token, result_payload)
            settlement_attempted = True
            self.ledger.settle(token, outcome=outcome, actual_cost_usd=cost)
            try:
                self._record(slot, token, outcome)
            except Exception:
                pass  # 응답과 월 정산은 이미 영속화됐다. 부가 기록 실패로 응답을 잃지 않는다.
            return result
        except BaseException as exc:
            # settle은 실패해도 소유권을 잃는다. 이미 시도했다면 맹목적으로 재정산하지 않는다.
            if token is not None and not settlement_attempted:
                settlement_attempted = True
                try:
                    self.ledger.settle(token, outcome=outcome if entered_dispatch else "not_sent",
                                       actual_cost_usd=cost)
                except Exception:
                    self.ledger.abandon(token)
            reason = exc.reason if isinstance(exc, BudgetBlocked) else "comparison_attempt_interrupted"
            try:
                self._mark_failed(attempt_id, token, reason)
                self._record(slot, token, "unknown" if entered_dispatch else "blocked")
            except Exception:
                pass  # 확정된 ID/슬롯은 남는다. 결과를 복구할 수 없어도 재전송은 금지한다.
            raise
