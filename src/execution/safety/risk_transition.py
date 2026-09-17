"""Pure candidate calculation for the existing intraday risk policy.

This module has no owner, clock, I/O, emit, or live-object publication path.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
import math
from typing import Any, Optional

from ...core.market_regime import classify_intraday_level
from .protection import decode_protection, encode_protection


_LEVELS = {"normal", "caution", "crash", "severe"}
_COOLDOWN = timedelta(minutes=5)


@dataclass(frozen=True)
class IntradayPolicyState:
    """Immutable batch-policy facts; ``updated_at=None`` is an unobserved baseline."""

    level: str
    kospi_pct: float
    updated_at: Optional[datetime]
    recovery_until: Optional[datetime]

    def __post_init__(self) -> None:
        if self.level not in _LEVELS:
            raise ValueError("invalid intraday policy level")
        if isinstance(self.kospi_pct, bool):
            raise ValueError("invalid intraday policy percent")
        try:
            value = float(self.kospi_pct)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("invalid intraday policy percent") from exc
        if not math.isfinite(value):
            raise ValueError("invalid intraday policy percent")
        object.__setattr__(self, "kospi_pct", value)
        _optional_aware(self.updated_at, "updated_at")
        _optional_aware(self.recovery_until, "recovery_until")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "level": self.level,
            "kospi_pct": self.kospi_pct,
            "updated_at": _encode_optional_time(self.updated_at),
            "recovery_until": _encode_optional_time(self.recovery_until),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "IntradayPolicyState":
        expected = {"schema_version", "level", "kospi_pct", "updated_at", "recovery_until"}
        if not isinstance(payload, dict) or set(payload) != expected:
            raise ValueError("invalid intraday policy payload")
        if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
            raise ValueError("unsupported intraday policy payload")
        return cls(
            level=payload["level"],
            kospi_pct=payload["kospi_pct"],
            updated_at=_decode_optional_time(payload["updated_at"], "updated_at"),
            recovery_until=_decode_optional_time(payload["recovery_until"], "recovery_until"),
        )


@dataclass(frozen=True)
class IntradayTransitionResult:
    """A candidate only; its protection DTO has not been published or emitted."""

    status: str
    state: IntradayPolicyState
    protection_dto: dict[str, Any]
    preemptive_stale_required: bool

    def __post_init__(self) -> None:
        if self.status not in {"applied", "missing"}:
            raise ValueError("invalid intraday transition status")
        if not isinstance(self.state, IntradayPolicyState):
            raise ValueError("invalid intraday transition state")
        if not isinstance(self.protection_dto, dict):
            raise ValueError("invalid intraday protection dto")
        if type(self.preemptive_stale_required) is not bool:
            raise ValueError("invalid preemptive stale flag")


def transition_intraday(
    previous: IntradayPolicyState,
    protection_dto: dict[str, Any],
    *,
    change_pct: Any,
    classified_at: datetime,
) -> IntradayTransitionResult:
    """Calculate one existing BatchAnalyzer/ExitManager transition without effects.

    ``classified_at`` is the classifier observation time, never a fabricated
    market ``as_of``.  Missing input deliberately preserves the prior baseline
    (including an unobserved normal/0.0 baseline) and protection DTO.
    """
    if not isinstance(previous, IntradayPolicyState):
        raise ValueError("intraday policy state is required")
    _aware(classified_at, "classified_at")
    if not isinstance(protection_dto, dict):
        raise ValueError("intraday protection dto is required")

    percent = _finite_percent(change_pct)
    if percent is None:
        return IntradayTransitionResult(
            status="missing",
            state=previous,
            protection_dto=deepcopy(protection_dto),
            preemptive_stale_required=False,
        )
    level = classify_intraday_level(percent)
    if level is None:  # Defensive parity for future shared-classifier changes.
        return IntradayTransitionResult(
            status="missing",
            state=previous,
            protection_dto=deepcopy(protection_dto),
            preemptive_stale_required=False,
        )

    recovery_until = previous.recovery_until
    candidate_protection = deepcopy(protection_dto)
    if level != previous.level:
        # decode_protection creates the real persist=False ExitManager clone and
        # validates the checkpoint before its existing mutation methods run.
        manager = decode_protection(candidate_protection, clock=lambda: classified_at)
        if level == "normal":
            manager.recover_from_intraday_crash()
            if previous.level in {"caution", "crash", "severe"}:
                recovery_until = classified_at + _COOLDOWN
        else:
            manager.apply_intraday_crash_params(level)
        candidate_protection = encode_protection(manager)

    state = IntradayPolicyState(
        level=level,
        kospi_pct=percent,
        updated_at=classified_at,
        recovery_until=recovery_until,
    )
    return IntradayTransitionResult(
        status="applied",
        state=state,
        protection_dto=candidate_protection,
        preemptive_stale_required=(previous.level == "normal" and level in {"crash", "severe"}),
    )


def _finite_percent(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        percent = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return percent if math.isfinite(percent) else None


def _aware(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be an aware datetime")
    return value


def _optional_aware(value: Optional[datetime], name: str) -> None:
    if value is not None:
        _aware(value, name)


def _encode_optional_time(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return _aware(value, "intraday policy time").isoformat()


def _decode_optional_time(value: Any, name: str) -> Optional[datetime]:
    if value is None:
        return None
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"invalid {name}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"invalid {name}") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"invalid {name}")
    return _aware(parsed, name)
