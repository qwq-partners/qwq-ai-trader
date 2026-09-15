"""합성 Toss/KIS 가격쌍을 위한 순수 오프라인 shadow 통계.

실행 경로는 입력 JSON만 소비한다. 이 모듈은 인증, HTTP, 환경변수, 캐시를 사용하지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
from typing import Any, Callable, Mapping, Sequence


SPEC_VERSION = "1.2.17"
SPEC_SHA256 = "791082da4cb379117ed9fdc29a45bd42746f7a1aec368da1e9f4e1f3bfbff5b4"
HASH_INPUT_NORMALIZATION = (
    "documented fields only; sorted-key compact JSON; timestamps normalized to UTC"
)
_MANIFEST_FIELDS = frozenset({
    "schema_version",
    "mode",
    "dataset_kind",
    "spec_version",
    "spec_sha256",
    "max_age_seconds",
    "max_pair_skew_seconds",
    "min_valid_pairs",
    "min_coverage",
    "p95_limit_pct",
    "outlier_threshold_pct",
    "max_outlier_fraction",
    "p95_method",
})
_SOURCE_FIELDS = ("price", "observed_at", "fetched_at", "status", "latency_ms")


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _parse_aware(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("timestamp must be a non-empty ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return parsed


def _utc_text(value: Any) -> str | None:
    try:
        return _parse_aware(value).astimezone(timezone.utc).isoformat()
    except ValueError:
        return None


def _normalized_hash_rows(rows: Sequence[Any]) -> list[dict[str, Any]]:
    """Hash only documented, non-secret input fields in a deterministic form."""
    normalized: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            normalized.append({"row": "invalid"})
            continue
        item: dict[str, Any] = {
            "pair_id": row.get("pair_id") if isinstance(row.get("pair_id"), str) else None,
            "symbol": row.get("symbol", "").strip().upper() if isinstance(row.get("symbol"), str) else None,
            "now": _utc_text(row.get("now")),
        }
        for provider in ("kis", "toss"):
            source = row.get(provider)
            if not isinstance(source, Mapping):
                item[provider] = None
                continue
            fields: dict[str, Any] = {}
            for field in _SOURCE_FIELDS:
                value = source.get(field)
                if field.endswith("_at"):
                    fields[field] = _utc_text(value)
                elif field in {"price", "latency_ms"}:
                    fields[field] = value if _is_number(value) else None
                else:
                    fields[field] = value if isinstance(value, str) else None
            item[provider] = fields
        normalized.append(item)
    return normalized


@dataclass(frozen=True)
class ShadowManifest:
    """유효한 synthetic/offline shadow 실행에 필요한 고정 계약."""

    schema_version: int
    mode: str
    dataset_kind: str
    spec_version: str
    spec_sha256: str
    max_age_seconds: float
    max_pair_skew_seconds: float
    min_valid_pairs: int
    min_coverage: float
    p95_limit_pct: float
    outlier_threshold_pct: float
    max_outlier_fraction: float
    p95_method: str

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ShadowManifest":
        if not isinstance(data, Mapping):
            raise ValueError("manifest must be an object")
        if set(data) != _MANIFEST_FIELDS:
            raise ValueError("manifest fields do not match the offline contract")
        if data["schema_version"] != 1 or isinstance(data["schema_version"], bool):
            raise ValueError("schema_version must be 1")
        if data["mode"] != "offline" or data["dataset_kind"] != "synthetic":
            raise ValueError("only offline synthetic datasets are supported")
        if data["spec_version"] != SPEC_VERSION or data["spec_sha256"] != SPEC_SHA256:
            raise ValueError("manifest does not match the pinned public spec")
        if data["p95_method"] != "nearest_rank":
            raise ValueError("p95_method must be nearest_rank")

        integer_fields = ("min_valid_pairs",)
        for field in integer_fields:
            value = data[field]
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{field} must be a non-negative integer")
        nonnegative_fields = (
            "max_age_seconds",
            "max_pair_skew_seconds",
            "p95_limit_pct",
            "outlier_threshold_pct",
        )
        for field in nonnegative_fields:
            if not _is_number(data[field]) or data[field] < 0:
                raise ValueError(f"{field} must be a finite non-negative number")
        unit_interval_fields = ("min_coverage", "max_outlier_fraction")
        for field in unit_interval_fields:
            if not _is_number(data[field]) or not 0 <= data[field] <= 1:
                raise ValueError(f"{field} must be a finite number in [0, 1]")
        return cls(**dict(data))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "mode": self.mode,
            "dataset_kind": self.dataset_kind,
            "spec_version": self.spec_version,
            "spec_sha256": self.spec_sha256,
            "max_age_seconds": self.max_age_seconds,
            "max_pair_skew_seconds": self.max_pair_skew_seconds,
            "min_valid_pairs": self.min_valid_pairs,
            "min_coverage": self.min_coverage,
            "p95_limit_pct": self.p95_limit_pct,
            "outlier_threshold_pct": self.outlier_threshold_pct,
            "max_outlier_fraction": self.max_outlier_fraction,
            "p95_method": self.p95_method,
        }


def build_shadow(*, enabled: bool = False, factory: Callable[[], Any]) -> Any | None:
    """명시적인 bool True일 때만 주입받은 factory를 호출한다."""
    if not isinstance(enabled, bool):
        raise TypeError("enabled must be a bool")
    if not enabled:
        return None
    return factory()


def _source_values(row: Mapping[str, Any], name: str) -> tuple[float, datetime, datetime, float]:
    source = row.get(name)
    if not isinstance(source, Mapping):
        raise KeyError("source")
    price = source.get("price")
    latency = source.get("latency_ms")
    if not _is_number(price) or price <= 0:
        raise ArithmeticError("price")
    if not _is_number(latency) or latency < 0:
        raise ArithmeticError("latency")
    return float(price), _parse_aware(source.get("observed_at")), _parse_aware(source.get("fetched_at")), float(latency)


def _excluded_reason(
    row: Any, manifest: ShadowManifest, seen: set[tuple[str, str, str]]
) -> tuple[str | None, float | None]:
    if not isinstance(row, Mapping):
        return "invalid_row", None
    if not isinstance(row.get("pair_id"), str) or not isinstance(row.get("symbol"), str):
        return "invalid_row", None
    symbol = row["symbol"].strip().upper()
    if not symbol:
        return "invalid_row", None
    kis = row.get("kis")
    toss = row.get("toss")
    if not isinstance(kis, Mapping) or not isinstance(toss, Mapping):
        return "invalid_row", None
    if kis.get("status") != "ok" or toss.get("status") != "ok":
        return "provider_status", None
    try:
        now = _parse_aware(row.get("now"))
        kis_price, kis_observed, kis_fetched, _ = _source_values(row, "kis")
        toss_price, toss_observed, toss_fetched, _ = _source_values(row, "toss")
    except ArithmeticError as exc:
        return ("invalid_price" if str(exc) == "price" else "invalid_latency"), None
    except (KeyError, ValueError):
        return "invalid_time", None
    if kis_observed > kis_fetched or toss_observed > toss_fetched:
        return "invalid_time", None
    if kis_fetched > now or toss_fetched > now:
        return "invalid_time", None
    key = (
        symbol,
        kis_observed.astimezone(timezone.utc).isoformat(),
        toss_observed.astimezone(timezone.utc).isoformat(),
    )
    if key in seen:
        return "duplicate_symbol_observed_at", None
    seen.add(key)
    if abs((kis_observed - toss_observed).total_seconds()) > manifest.max_pair_skew_seconds:
        return "observation_skew", None
    if (now - kis_fetched).total_seconds() > manifest.max_age_seconds or (
        now - toss_fetched
    ).total_seconds() > manifest.max_age_seconds:
        return "stale", None
    return None, abs(toss_price - kis_price) / kis_price * 100


def summarize_pairs(rows: Sequence[Any], manifest: ShadowManifest) -> dict[str, Any]:
    """입력 행을 절대 외부 I/O 없이 집계하고 JSON 호환 보고서를 반환한다."""
    if not isinstance(manifest, ShadowManifest):
        raise TypeError("manifest must be a ShadowManifest")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)):
        raise ValueError("rows must be a JSON array")

    excluded: dict[str, int] = {}
    differences: list[float] = []
    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        reason, difference = _excluded_reason(row, manifest, seen)
        if reason is not None:
            excluded[reason] = excluded.get(reason, 0) + 1
        elif difference is not None:
            differences.append(difference)

    attempted = len(rows)
    valid = len(differences)
    coverage = valid / attempted if attempted else 0.0
    differences.sort()
    p95 = differences[math.ceil(0.95 * valid) - 1] if valid else None
    outliers = sum(value > manifest.outlier_threshold_pct for value in differences)
    outlier_fraction = outliers / valid if valid else None
    sufficient = valid >= manifest.min_valid_pairs and coverage >= manifest.min_coverage
    within_limits = (
        sufficient
        and p95 is not None
        and p95 <= manifest.p95_limit_pct
        and outlier_fraction is not None
        and outlier_fraction <= manifest.max_outlier_fraction
    )
    status = "insufficient" if not sufficient else ("within_limits" if within_limits else "threshold_exceeded")
    time_excluded = sum(excluded.get(reason, 0) for reason in ("invalid_time", "observation_skew", "stale"))
    return {
        "attempted_pairs": attempted,
        "valid_pairs": valid,
        "failed_pairs": excluded.get("provider_status", 0),
        "duplicate_pairs": excluded.get("duplicate_symbol_observed_at", 0),
        "time_excluded_pairs": time_excluded,
        "excluded_reasons": excluded,
        "coverage": coverage,
        "p95_difference_pct": p95,
        "outlier_fraction": outlier_fraction,
        "meets_thresholds": within_limits,
        "status": status,
        "p95_method": manifest.p95_method,
        "manifest_sha256": _canonical_hash(manifest.to_dict()),
        "dataset_sha256": _canonical_hash(_normalized_hash_rows(rows)),
        "hash_input_normalization": HASH_INPUT_NORMALIZATION,
        "synthetic_only": True,
        "production_eligible": False,
    }
