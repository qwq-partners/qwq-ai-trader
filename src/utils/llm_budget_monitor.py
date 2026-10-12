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
            f"비용: 확정 비용 {_money(payload.get('spent_usd'))} + "
            f"예약 {_money(payload.get('reserved_usd'))} / "
            f"월 한도 {_money(payload.get('monthly_limit_usd'))}"
        )
    total = spent + reserved
    ratio = (
        100.0 if limit == 0 and total > 0
        else 0.0 if limit == 0
        else total / limit * 100
    )
    return (
        f"비용: 확정 비용 ${spent:.4f} + 예약 ${reserved:.4f} = "
        f"관리 합계 ${total:.4f} / 월 한도 ${limit:.4f} ({ratio:.1f}%)"
    )


def format_budget_policy_notice(payload: Mapping[str, Any]) -> str:
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
        f"이벤트 시각: {event_at}",
        f"기준 월: {month} (KST)",
    ]
    if kind == "month_reset":
        previous = payload.get("previous_month")
        if isinstance(previous, Mapping):
            lines.append(
                f"이전 월 {_safe_text(previous.get('month'))}: "
                f"확정 {_money(previous.get('spent_usd'))} + "
                f"예약 {_money(previous.get('reserved_usd'))}"
            )
        lines.append(f"새 월 {month}: {_cost_line(payload).removeprefix('비용: ')}")
    else:
        lines.append(_cost_line(payload))

    lines.extend([
        f"현재 정책: <b>{mode_label}</b>",
        "정책 단계: 절약 80% → 필수 95% → 유료 중지 100%",
        f"정책 사유: {_safe_text(payload.get('policy_reason'))}",
        f"정책 버전: {_safe_text(payload.get('policy_version'))}",
        action,
        "규칙 기반 매매와 보호 청산은 계속됩니다.",
    ])
    if kind == "month_reset":
        lines.extend([
            "KST 달력월 기준 새 월 예산으로 전환합니다.",
            "미확인 비용과 원장 이상은 월 전환으로 초기화되지 않습니다.",
        ])
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
        ledger.policy_status()
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
        text = format_budget_policy_notice(notice["payload"])
    except Exception:
        _log_bounded(
            "notice_payload",
            "[LLM 예산정책] 알림 자료 형식 오류; 대기열에 보존합니다",
        )
        return False

    if send is None:
        from .telegram import send_alert
        send = send_alert

    delivered = False
    try:
        delivered = bool(await asyncio.wait_for(
            send(text), timeout=_SEND_TIMEOUT_SECONDS
        ))
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
        await budget_monitor_tick(manager, send=send)
        await asyncio.sleep(interval_seconds)
