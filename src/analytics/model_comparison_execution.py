"""미래 비교 실행기의 명시 주입형 호출 경계 — 실행기·CLI·공급자 연결은 없다.

이미 열린 MonthlyBudgetLedger와 고정 정책을 주입한 뒤, 재시도·폴백을 포함한
공급자 시도마다 ``await guard.call(request, dispatch=adapter)``를 호출한다.
어댑터는 지정 모델·출력 상한으로 한 번만 전송하고 측정 비용(불명은 None)을
돌려줘야 한다. LLMManager는 자체 예약·다중 시도를 소유하므로 연결하면 안 된다.
실제 어댑터는 별도 검토가 필요하며 임의 콜백의 숨은 재시도를 여기서 증명하지 않는다.

명시 실험 정책과 KST 입장 슬롯만 기존 SQLite 원장에 추가한다. 월 설정·활성화
기준선은 생성하거나 재설정하지 않는다. 일 슬롯 확정 → 월 예약 → 전송 순서이며,
월 차단·충돌·프로세스 종료에도 슬롯을 환급하지 않아 초과 호출보다 과소 사용을 택한다.
이는 시도 상한이며 엔진의 일 판단 상한이나 전체 재생·보고서 구현이 아니다.
SQLite 처리는 동기식이다. 별도 오프라인 작업 프로세스용이며 실시간 엔진의
이벤트 루프에 비차단으로 통합됐다고 해석하면 안 된다.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_CEILING, localcontext
import json
import sqlite3
from typing import Any, Awaitable, Callable
import uuid

from ..utils.llm_budget import BudgetBlocked, BudgetStateError, MonthlyBudgetLedger, _MAX_UNITS, _units
from .model_comparison_budget import _d, _integer

SCHEMA_VERSION = "model-comparison-execution-v1"


@dataclass(frozen=True)
class ComparisonExecutionPolicy:
    experiment_id: str
    daily_attempt_cap: int
    allowed_models: tuple[str, ...]
    max_output_tokens: int

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

    def __post_init__(self):
        if not isinstance(self.model, str) or not self.model:
            raise ValueError("model은 비어 있지 않은 문자열이어야 한다")
        if not isinstance(self.prompt, str) or not isinstance(self.system, str):
            raise ValueError("prompt와 system은 문자열이어야 한다")
        _integer(self.max_output_tokens, "max_output_tokens", 1)


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

    def _admit(self) -> tuple[str, str]:
        try:
            with self.ledger._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                self._check_policy(db)
                day = self.ledger.today().isoformat()
                used = db.execute("SELECT COUNT(*) FROM comparison_attempts WHERE admission_day=?", (day,)).fetchone()[0]
                if used >= self.policy.daily_attempt_cap:
                    raise BudgetBlocked("comparison_daily_attempt_cap")
                slot = uuid.uuid4().hex
                db.execute("INSERT INTO comparison_attempts VALUES (?,?,NULL,'admitted')", (slot, day))
                db.execute("COMMIT")
                return slot, day
        except BudgetBlocked:
            raise
        except (OSError, sqlite3.Error, TypeError, ValueError):
            raise BudgetStateError("comparison_storage_unavailable") from None

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

    async def call(self, request: ComparisonRequest, *,
                   dispatch: Callable[[ComparisonRequest], Awaitable[ComparisonAttemptResult]]) -> ComparisonAttemptResult:
        """입력 오류는 ValueError, 응답 계약 오류는 잠재 지출 보존 뒤 BudgetBlocked로 보고한다.

        어댑터 자체 예외/취소도 잠재 지출을 보존하고 원래 예외를 유지한다.
        """
        if not isinstance(request, ComparisonRequest) or not callable(dispatch):
            raise ValueError("ComparisonRequest와 단일 시도 async dispatch가 필요하다")
        if request.model not in self.policy.allowed_models:
            raise BudgetBlocked("comparison_model_not_allowed")
        if request.max_output_tokens > self.policy.max_output_tokens:
            raise BudgetBlocked("comparison_output_cap")
        slot, day = self._admit()
        token = None
        entered_dispatch = False
        settled = False
        try:
            token = self.ledger.reserve(request.model, request.prompt, request.system, request.max_output_tokens,
                                        policy_priority="research", task="model_comparison",
                                        role=self.policy.experiment_id)
            # 슬롯과 월 예약은 이미 확정됐다. 전송 전 부가 기록은 잠금 경합 시
            # 무전송 예약까지 고아 상태로 만들 수 있어 정산 뒤에만 연결한다.
            # 동기 입장 도중 자정을 넘기면 전날 슬롯으로 전송하지 않고 새 입장을 요구한다.
            # 아직 어댑터에 진입하지 않았으므로 전송 전 실패로만 해제할 수 있다.
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
            state = "unknown" if cost is None else "known"
            self.ledger.settle(token, outcome=state, actual_cost_usd=cost)
            settled = True
            self._record(slot, token, state)
            return result
        except BaseException:
            # 취소·시간 초과·어댑터 실패·비용 오류는 0원 증거가 아니다.
            # 어댑터 진입 전 실패만 미전송으로 해제한다.
            if token is not None and not settled:
                try:
                    self.ledger.settle(token, outcome="unknown" if entered_dispatch else "not_sent")
                except Exception:
                    self.ledger.abandon(token)  # 기존 고아 예약 처리가 이후 모든 달의 호출을 차단한다.
            try:
                self._record(slot, token, "unknown" if entered_dispatch else "blocked")
            except Exception:
                pass  # 슬롯은 이미 확정됐으므로 원래 실패를 가리지 않는다.
            raise
