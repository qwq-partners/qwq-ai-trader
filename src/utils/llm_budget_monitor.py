"""Monthly LLM budget policy notifications.

The ledger owns policy transitions and the durable notification outbox.  This
module only advances that policy observation, leases one notice, and reports
delivery.  Notification failure never changes the policy selected by the
ledger.
"""
from __future__ import annotations

import asyncio
import html
import math
import time
from typing import Any, Awaitable, Callable, Mapping, Optional

from loguru import logger


_NOTICE_LEASE_SECONDS = 120
_SEND_TIMEOUT_SECONDS = 60
_ERROR_LOG_INTERVAL_SECONDS = 300.0
_last_error_log: dict[str, float] = {}

_MODE_LABELS = {
    "normal": "기본 모드 (80% 미만)",
    "economy": "절약 모드 (80% 이상)",
    "essential": "필수 호출 모드 (95% 이상)",
    "paused": "유료 호출 중지 (100% 이상)",
    "blocked": "안전 차단 (비용 미확인 또는 원장 이상)",
}

_MODE_ACTIONS = {
    "normal": "유료 LLM 정책이 기본 모드로 운영됩니다.",
    "economy": (
        "비핵심 작업은 사전 지정된 절약 프로필을 사용합니다. "
        "위험 중요 판단 모델은 자동 하향하지 않습니다."
    ),
    "essential": "비필수 유료 LLM 호출을 중지하고 필수 호출만 허용합니다.",
    "paused": "월 한도 도달로 모든 유료 LLM 호출을 중지합니다.",
    "blocked": "미확인 비용 또는 원장 이상으로 모든 유료 LLM 호출을 안전 차단합니다.",
}


def _safe_text(value: Any, fallback: str = "미확인") -> str:
    if value is None:
        return fallback
    text = str(value).strip()
    return html.escape(text) if text else fallback


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def _money(value: Any) -> str:
    number = _number(value)
    return "미확인" if number is None else f"${number:.4f}"


def _cost_line(payload: Mapping[str, Any]) -> str:
    spent = _number(payload.get("spent_usd"))
    reserved = _number(payload.get("reserved_usd"))
    limit = _number(payload.get("monthly_limit_usd"))
    if spent is None or reserved is None or limit is None:
        return (
            f"비용: 정산 추정 {_money(payload.get('spent_usd'))} + "
            f"예약 추정 {_money(payload.get('reserved_usd'))} / "
            f"월 한도 {_money(payload.get('monthly_limit_usd'))}"
        )
    total = spent + reserved
    ratio = (
        100.0 if limit == 0 and total > 0
        else 0.0 if limit == 0
        else total / limit * 100
    )
    return (
        f"비용: 정산 추정 ${spent:.4f} + 예약 추정 ${reserved:.4f} = "
        f"관리 합계 ${total:.4f} / 월 한도 ${limit:.4f} ({ratio:.1f}%)"
    )


def format_budget_policy_notice(
    payload: Mapping[str, Any],
    *,
    event_id: Optional[str] = None,
    current_status: Optional[Mapping[str, Any]] = None,
) -> str:
    """Return a deterministic Korean notification for a durable policy event."""
    kind = payload.get("kind")
    mode = str(payload.get("policy_mode") or "")
    mode_label = _MODE_LABELS.get(mode, f"알 수 없는 정책 모드 ({_safe_text(mode)})")
    action = _MODE_ACTIONS.get(mode, "정책 원장의 현재 모드를 확인해야 합니다.")
    month = _safe_text(payload.get("month"))
    event_at = _safe_text(payload.get("event_at"))
    title = (
        "🗓️ <b>LLM 월 예산 KST 달력월 전환</b>"
        if kind == "month_reset"
        else "📊 <b>LLM 월 예산 정책 전환</b>"
    )

    lines = [
        title,
        *([f"이벤트 ID: {_safe_text(event_id)}"] if event_id is not None else []),
        f"이벤트 시각: {event_at}",
        f"기준 월: {month} (KST)",
    ]
    if kind == "month_reset":
        previous = payload.get("previous_month")
        if isinstance(previous, Mapping):
            lines.append(
                f"이전 월 {_safe_text(previous.get('month'))} "
                f"(월 경계 시점 잠정값): "
                f"정산 추정 {_money(previous.get('spent_usd'))} + "
                f"예약 추정 {_money(previous.get('reserved_usd'))}"
            )
            unresolved = previous.get("unresolved_count")
            if isinstance(unresolved, int) and not isinstance(unresolved, bool) and unresolved >= 0:
                lines.append(f"미확인 전체 건수: {unresolved}건")
        lines.append(f"새 월 {month}: {_cost_line(payload).removeprefix('비용: ')}")
    else:
        lines.append(_cost_line(payload))
    lines.append("비용 기준: 공개 단가·토큰 사용량 추정이며 공급자 청구서 확정액이 아닙니다.")

    lines.extend([
        f"기록 시점 정책: <b>{mode_label}</b>",
        "정책 단계: 절약 80% → 필수 95% → 유료 중지 100%",
        f"정책 사유: {_safe_text(payload.get('policy_reason'))}",
        f"정책 버전: {_safe_text(payload.get('policy_version'))}",
        f"기록 시점 조치: {action}",
        "기존 매매 규칙·매수 중지 설정과 보호 청산은 유지됩니다.",
    ])
    if isinstance(current_status, Mapping) and current_status.get("policy_mode"):
        current_mode = str(current_status["policy_mode"])
        current_label = _MODE_LABELS.get(
            current_mode,
            f"알 수 없는 정책 모드 ({_safe_text(current_mode)})",
        )
        lines.append(
            f"발송 시점 정책: <b>{current_label}</b> "
            f"({_safe_text(current_status.get('month'))} KST)"
        )
    if kind == "month_reset":
        if mode in {"paused", "blocked"}:
            lines.append("KST 달력월은 전환됐지만 차단 상태는 유지됩니다.")
        else:
            lines.append("KST 달력월 기준 새 월 예산으로 전환합니다.")
        lines.append("미확인 비용과 원장 이상은 월 전환으로 초기화되지 않습니다.")
    return "\n".join(lines)


def _log_bounded(key: str, message: str) -> None:
    now = time.monotonic()
    previous = _last_error_log.get(key)
    if previous is None or now - previous >= _ERROR_LOG_INTERVAL_SECONDS:
        _last_error_log[key] = now
        logger.warning(message)


async def budget_monitor_tick(
    manager: Any,
    *,
    send: Optional[Callable[[str], Awaitable[bool]]] = None,
) -> Optional[bool]:
    """Observe policy and deliver at most one durable notice.

    ``None`` means no delivery was attempted, ``True`` means the notice was
    delivered and acknowledged, and ``False`` means it remains retryable.
    """
    config = getattr(manager, "config", None)
    ledger = getattr(manager, "monthly_budget", None)
    if not getattr(config, "budget_policy_enabled", False) or ledger is None:
        return None

    try:
        current_status = ledger.policy_status()
    except Exception:
        _log_bounded(
            "policy_status",
            "[LLM 예산정책] 예산 정책 상태 확인 실패; 다음 주기에 재시도합니다",
        )
        return None

    try:
        notice = ledger.claim_policy_notice(lease_seconds=_NOTICE_LEASE_SECONDS)
    except Exception:
        _log_bounded(
            "claim_notice",
            "[LLM 예산정책] 알림 대기열 확인 실패; 다음 주기에 재시도합니다",
        )
        return None
    if notice is None:
        return None

    try:
        event_id = notice["event_id"]
        claim_token = notice["claim_token"]
        text = format_budget_policy_notice(
            notice["payload"],
            event_id=event_id,
            current_status=current_status,
        )
    except Exception:
        _log_bounded(
            "notice_payload",
            "[LLM 예산정책] 알림 자료 형식 오류; 대기열에 보존합니다",
        )
        return False

    delivered = False
    try:
        if send is None:
            from .telegram import send_alert
            send = send_alert
        delivered = (await asyncio.wait_for(
            send(text), timeout=_SEND_TIMEOUT_SECONDS
        )) is True
    except asyncio.CancelledError:
        # Do not finish the claim. Its ledger lease makes it recoverable.
        raise
    except asyncio.TimeoutError:
        _log_bounded(
            "send_timeout",
            "[LLM 예산정책] 알림 전송 시간 초과; 대기열에 보존합니다",
        )
    except Exception:
        _log_bounded(
            "send_failure",
            "[LLM 예산정책] 알림 전송 실패; 대기열에 보존합니다",
        )

    try:
        ledger.finish_policy_notice(event_id, claim_token, delivered=delivered)
    except Exception:
        _log_bounded(
            "finish_notice",
            "[LLM 예산정책] 알림 결과 저장 실패; 임대 만료 후 재시도합니다",
        )
        return False
    return delivered


async def run_budget_monitor(
    manager: Any,
    *,
    send: Optional[Callable[[str], Awaitable[bool]]] = None,
    interval_seconds: float = 30,
) -> None:
    """Run budget policy observation until the caller cancels the task."""
    if (
        isinstance(interval_seconds, bool)
        or not isinstance(interval_seconds, (int, float))
        or not math.isfinite(interval_seconds)
        or interval_seconds <= 0
    ):
        raise ValueError("interval_seconds must be a finite positive number")
    while True:
        try:
            await budget_monitor_tick(manager, send=send)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log_bounded('monitor_tick', '[LLM 예산정책] 알림 모니터 일시 오류; 다음 주기에 재시도합니다')
        await asyncio.sleep(interval_seconds)
