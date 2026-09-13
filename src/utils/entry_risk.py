"""진입 위험 스냅샷 (계획서 T3 — 결함 F4, 2026-09-14)

risk 모드 신규 매수의 **주문 시점 계획 위험**을 고정 스키마 dict 로 만들어 신호·주문 캐시·
signal_events·체결 원장(`market_context.entry_risk`)까지 같은 값으로 흘려보낸다. canary 원장
(`scripts/review_risk_canary.py` 모듈 docstring)이 이 스키마를 그대로 읽는다.

전부 순수 함수 — 네트워크·DB·상태파일 접근 없음(`applied_sha()` 의 `git rev-parse` 1회 제외).
금액은 문자열 Decimal, 시각은 ISO8601, 수량은 int.

R 분모 규약:
- `planned_risk_amount` = (price×q + 매수수수료) × net SL% — T2 사이징 상한과 같은 식.
- `initial_risk_amount` = **체결 후** 실제 체결가·누적 수량 × 실제 초기 SL (`confirm_initial_risk`).
  canary 의 `entry_cost` 정의(Σ 매수 price×quantity, 수수료 제외)와 같은 분모를 쓴다 —
  매수수수료만큼의 차이는 `planned_vs_filled_risk_delta` 에 남는다(원장 예시 -9.75).
- 확정된 초기 위험금액은 부분매도·레짐 변경·재시작으로 바꾸지 않는다(ExitManager 영속 상태).
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Tuple

from .fee_calculator import FeeCalculator, get_fee_calculator
from .sizing import planned_risk
from .stop_policy import StopDecision

__all__ = [
    "ENTRY_RISK_REQUIRED", "ENTRY_RISK_VERSION", "CONFIRMED_RISK_KEYS",
    "build_entry_risk_snapshot", "confirm_initial_risk", "merge_confirmed_risk",
    "planned_vs_filled_delta", "effective_config_hash", "applied_sha", "cohort_id",
]

ENTRY_RISK_VERSION = 1

# canary(T8) 가 필수로 요구하는 키 — 순서·이름 고정
ENTRY_RISK_REQUIRED: Tuple[str, ...] = (
    "version", "cohort_id", "sizing_mode", "strategy", "stop_basis", "stop_pct",
    "stop_source", "equity_at_decision", "risk_budget_amount", "planned_price",
    "planned_quantity", "planned_risk_amount",
)

# 유효 설정 hash 대상 (자격증명 제외 allowlist) — 최상위 섹션 이름
CONFIG_HASH_SECTIONS: Tuple[str, ...] = (
    "risk", "risk_config", "kr", "us", "exit", "exit_config", "strategies",
    "strategy_allocation", "factor_budgets",
)
# 하위 수준 방어: 이름에 이 토큰이 들어간 키는 값 자체를 제외한다
_SECRET_TOKENS: Tuple[str, ...] = (
    "key", "secret", "token", "password", "passwd", "credential", "cano", "acnt",
    "account", "chat_id", "url", "dsn", "webhook",
)

_APPLIED_SHA: Optional[str] = None


def cohort_id(strategy: Optional[str]) -> str:
    """표본 고정 식별자 — "risk-<strategy>-v1" (예: risk-sepa_trend-v1)."""
    return f"risk-{strategy or 'unknown'}-v1"


def build_entry_risk_snapshot(
    *,
    strategy: Optional[str],
    stop_decision: StopDecision,
    equity: Decimal,
    price: Decimal,
    quantity: int,
    risk_per_trade_pct: float,
    fee_calc: Optional[FeeCalculator] = None,
    applied_sha: Optional[str] = None,
    config_hash: Optional[str] = None,
    signal_ts: Optional[str] = None,
) -> dict:
    """주문 시점 계획 위험 스냅샷 (JSON 직렬화 가능한 dict).

    수량은 모든 오버레이·최소금액 보정·위험 상한(`risk_quantity_cap`)이 끝난 **최종 수량**을 넣는다.
    수량 0(주문 미생성)에는 호출하지 않는다 — 주문 없는 태그를 남기지 않기 위함.
    """
    fee = fee_calc if fee_calc is not None else get_fee_calculator("KR")
    stop_pct = stop_decision.stop_pct
    budget = equity * Decimal(str(risk_per_trade_pct)) / 100
    return {
        "version": ENTRY_RISK_VERSION,
        "cohort_id": cohort_id(strategy),
        "sizing_mode": "risk",
        "strategy": strategy or "",
        "stop_basis": "net_pnl",
        "stop_pct": str(stop_pct),
        "stop_source": stop_decision.source,
        "stop_crash_active": bool(stop_decision.crash_capped),
        "equity_at_decision": str(equity),
        "risk_budget_amount": str(budget),
        "planned_price": str(price),
        "planned_quantity": int(quantity),
        "planned_risk_amount": str(planned_risk(price, int(quantity), stop_pct, fee)),
        "signal_ts": signal_ts,
        "applied_sha": applied_sha,
        "config_hash": config_hash,
    }


def confirm_initial_risk(
    fills: Sequence[Mapping[str, Any]],
    actual_stop_pct: Any,
    fee_calc: Optional[FeeCalculator] = None,
) -> Tuple[Decimal, Decimal]:
    """체결 확정 초기 위험금액 — (initial_risk_amount, entry_cost).

    fills: 첫 매수 주문의 체결 목록 `[{price, quantity, fee}]`. 부분체결은 주문이 끝날 때까지
    누적해서 한 번에 넘긴다. entry_cost = Σ price×quantity (canary `entry_cost` 와 동일 정의),
    initial_risk_amount = entry_cost × 실제 초기 SL%.
    """
    stop = Decimal(str(actual_stop_pct)) if actual_stop_pct is not None else None
    if stop is None or not stop.is_finite() or stop <= 0:
        raise ValueError(f"유효하지 않은 실제 초기 손절폭: {actual_stop_pct!r}")
    cost = Decimal("0")
    quantity = 0
    for f in fills:
        q = int(f["quantity"])
        if q <= 0:
            continue
        cost += Decimal(str(f["price"])) * q
        quantity += q
    if quantity <= 0 or cost <= 0:
        raise ValueError("체결 수량이 없어 초기 위험금액을 확정할 수 없다")
    # fee_calc 은 호출자 호환용(수수료 기준 분모를 쓰지 않음) — 시그니처 유지
    _ = fee_calc
    return cost * stop / 100, cost


def merge_confirmed_risk(
    entry_risk: Mapping[str, Any],
    initial_risk_amount: Any,
    actual_stop_pct: Any,
    fills: Sequence[Mapping[str, Any]],
) -> dict:
    """체결 확정값을 진입 스냅샷에 병합한 **새 dict** (원본 불변).

    A 배선: 첫 매수 주문의 체결이 확정된 시점에
    `record_entry(market_context={..., "entry_risk": merge_confirmed_risk(...)})` 로 원장에 남긴다.
    거래별로 분모가 원장에 박히므로, 같은 종목을 재보유해도 옛 거래에 현재 포지션 값이
    오귀속되지 않는다(ExitManager 상태는 완전 청산 시 삭제된다).

    **이미 확정값이 있으면 그대로 돌려준다** — 복수 부분체결에서 2회차 이후 호출이
    R 분모를 바꾸거나 키를 중복 기록하지 않는다(ExitManager.set_initial_risk 와 같은 규약).
    """
    merged = dict(entry_risk) if isinstance(entry_risk, Mapping) else {}
    if merged.get("initial_risk_amount") is not None:
        return merged
    try:
        amount = Decimal(str(initial_risk_amount))
        stop = Decimal(str(actual_stop_pct))
    except (InvalidOperation, ValueError, TypeError):
        return merged
    if not amount.is_finite() or amount <= 0 or not stop.is_finite() or stop <= 0:
        return merged

    quantity = 0
    cost = Decimal("0")
    for f in fills:
        q = int(f["quantity"])
        if q <= 0:
            continue
        quantity += q
        cost += Decimal(str(f["price"])) * q
    if quantity <= 0:
        return merged

    merged["initial_risk_amount"] = str(amount)
    merged["actual_stop_pct"] = str(stop)
    merged["filled_quantity"] = quantity
    merged["entry_cost"] = str(cost)
    planned = merged.get("planned_risk_amount")
    if planned is not None:
        merged["planned_vs_filled_risk_delta"] = str(planned_vs_filled_delta(planned, amount))
    return merged


# 체결 확정 시 스냅샷에 추가되는 키 (exporter 가 closed 포지션의 분모로 우선 읽는다)
CONFIRMED_RISK_KEYS: Tuple[str, ...] = (
    "initial_risk_amount", "actual_stop_pct", "filled_quantity", "entry_cost",
    "planned_vs_filled_risk_delta",
)


def planned_vs_filled_delta(planned_risk_amount: Any, initial_risk_amount: Any) -> Decimal:
    """확정 − 계획 (음수 = 계획보다 작은 위험으로 체결)."""
    return Decimal(str(initial_risk_amount)) - Decimal(str(planned_risk_amount))


def effective_config_hash(config: Mapping[str, Any]) -> str:
    """유효 설정 sha256 (키 정렬). 자격증명은 allowlist 밖이라 해시 대상이 아니다."""
    allowed = {k: _strip_secrets(v) for k, v in config.items() if k in CONFIG_HASH_SECTIONS}
    payload = json.dumps(allowed, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _strip_secrets(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: _strip_secrets(v) for k, v in value.items() if not _is_secret_key(k)}
    if isinstance(value, (list, tuple)):
        return [_strip_secrets(v) for v in value]
    return value


def _is_secret_key(key: Any) -> bool:
    name = str(key).lower()
    return any(token in name for token in _SECRET_TOKENS)


def _detect_applied_sha() -> str:
    """`git rev-parse HEAD` — **모듈 로드 시 1회만** 실행한다."""
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(Path(__file__).resolve().parents[2]),
            stderr=subprocess.DEVNULL, text=True, timeout=5,
        )
        sha = out.strip()
        return sha if sha else "unknown"
    except Exception:
        return "unknown"


def applied_sha() -> str:
    """현재 체크아웃 SHA. **캐시만 읽는다** — 사이징 경로(이벤트 루프)에서 프로세스를 띄우지 않는다.

    값은 모듈 로드 시점(프로세스 시작)에 선계산된다. 캐시가 비어 있으면 "unknown".
    """
    return _APPLIED_SHA if _APPLIED_SHA is not None else "unknown"


# 모듈 로드 시 선계산 (import 는 프로세스 시작 시점 — asyncio 루프 밖)
_APPLIED_SHA = _detect_applied_sha()
