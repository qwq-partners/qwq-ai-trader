"""KIS index quote를 위험 분류용의 작은, 증명 가능한 입력으로 축소한다.

이 경계는 조회, 시계 읽기, owner 게시, 정책 적용을 하지 않는다. REST receipt는
시장 시각이 아니므로 ``market_as_of``는 검증된 ``None``만 표현한다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import json
import math
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from ...core.market_regime import classify_intraday_level


_KST = ZoneInfo("Asia/Seoul")
_FIELDS = frozenset({"price", "open", "high", "low", "change", "change_pct"})
_OBSERVATION_KEYS = frozenset({
    "schema_version", "source", "source_tr", "index_code", "observation_id",
    "received_at", "market_as_of", "fields",
})
_TREND_FIELDS = ("price", "open", "high", "low", "change_pct")


@dataclass(frozen=True)
class IndexRiskInput:
    """위험 분류 전의 immutable index provenance 입력이다."""

    outcome: str
    change_pct: float | None
    level: str | None
    source: str
    source_event_id: str
    received_at: datetime | None
    market_as_of: None
    observation_json: str

    def __post_init__(self) -> None:
        if self.outcome not in {"success", "missing"}:
            raise ValueError("invalid index risk outcome")
        if type(self.source) is not str or type(self.source_event_id) is not str:
            raise ValueError("invalid index risk provenance")
        if self.received_at is not None:
            _aware(self.received_at)
        if self.market_as_of is not None or type(self.observation_json) is not str:
            raise ValueError("invalid index risk timestamp")
        if self.outcome == "missing":
            if self.change_pct is not None or self.level is not None:
                raise ValueError("missing index risk must not classify")
            return
        if type(self.change_pct) is not float or not math.isfinite(self.change_pct):
            raise ValueError("invalid index risk percent")
        if self.level != classify_intraday_level(self.change_pct):
            raise ValueError("invalid index risk level")


def normalize_index_risk(
    quote: Any,
    *,
    now: datetime,
    business_day: str,
) -> IndexRiskInput:
    """Validate producer provenance and return a success or fail-closed missing input.

    A legacy outer quote may contain numeric zero after a raw missing/invalid
    field. Only a matching producer metadata field marked ``valid`` can yield a
    normal level.
    """
    _aware(now)
    expected_day = _iso_day(business_day)
    if type(quote) is not dict:
        return _missing()

    observation = quote.get("_observation")
    parsed = _observation(observation)
    if parsed is None:
        return _missing()
    received_at, change_pct, observation_json = parsed
    if received_at > now:
        return _missing()
    try:
        received_day = received_at.astimezone(_KST).date()
    except (OverflowError, ValueError):
        return _missing()
    if received_day != expected_day:
        return _missing()

    outer_pct = _finite_number(quote.get("change_pct"))
    if change_pct is None or outer_pct is None or outer_pct != change_pct:
        return _missing()
    level = classify_intraday_level(change_pct)
    if level is None:
        return _missing()
    return IndexRiskInput(
        outcome="success",
        change_pct=change_pct,
        level=level,
        source="kis:FHPUP02100000:index0001",
        source_event_id=observation["observation_id"],
        received_at=received_at,
        market_as_of=None,
        observation_json=observation_json,
    )


def _missing() -> IndexRiskInput:
    return IndexRiskInput(
        outcome="missing", change_pct=None, level=None, source="missing",
        source_event_id="", received_at=None, market_as_of=None, observation_json="{}",
    )


@dataclass(frozen=True)
class IndexTrendInput:
    """추세 계산용 detached 값. 생성 자체가 source 성공/시장 신선도 권한은 아니다.

    legacy 추세의 한쪽 지수 누락→0 fallback은 이 인계 경계에서 사용하지 않는다.
    두 지수의 결합/순서/현재 source 검사는 실제 owner caller의 별도 책임이다.
    """

    index_code: str
    outcome: str
    values: tuple[tuple[str, float], ...]
    source: str
    source_event_id: str
    received_at: datetime | None
    market_as_of: None
    observation_json: str

    def __post_init__(self) -> None:
        if (type(self.index_code) is not str or self.index_code not in {"0001", "1001"}
                or type(self.outcome) is not str or self.outcome not in {"success", "missing"}
                or type(self.values) is not tuple or self.market_as_of is not None
                or type(self.observation_json) is not str):
            raise ValueError("invalid index trend DTO")
        if self.outcome == "missing":
            if (self.values != () or self.source != "missing" or self.source_event_id != ""
                    or self.received_at is not None or self.observation_json != "{}"):
                raise ValueError("missing index trend must not carry facts")
            return
        if (len(self.values) != len(_TREND_FIELDS) or any(
                type(pair) is not tuple or len(pair) != 2 or pair[0] != name
                or type(pair[1]) is not float or not math.isfinite(pair[1])
                or (name != "change_pct" and pair[1] <= 0)
                for name, pair in zip(_TREND_FIELDS, self.values))):
            raise ValueError("invalid index trend values")
        try:
            observation = json.loads(self.observation_json)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid index trend provenance") from exc
        parsed = _observation(observation, index_code=self.index_code)
        if (parsed is None or parsed[2] != self.observation_json
                or self.received_at != parsed[0]
                or self.source != "kis:FHPUP02100000:index" + self.index_code
                or self.source_event_id != observation["observation_id"]
                or any(observation["fields"][name]["status"] != "valid"
                       or observation["fields"][name]["value"] != value
                       for name, value in self.values)):
            raise ValueError("conflicting index trend provenance")


def normalize_index_trend(
    quote: Any, *, index_code: str, now: datetime, business_day: str,
) -> IndexTrendInput:
    """기존 두 지수 응답만 검증한다. 조회/시계/추가 TTL/레짐 판정은 하지 않는다.

    OHLC는 양수여야 산식의 fallback 분기로 결측이 숨지 않는다. 등락률 0은
    원 필드가 valid인 경우 그대로 유효하다. 사용하지 않는 전일 대비 금액의
    결측은 허용한다. 모든 required 값은 producer metadata와 정확히 일치해야 한다.
    """
    if type(index_code) is not str or index_code not in {"0001", "1001"}:
        raise ValueError("unsupported index trend contract")
    _aware(now)
    expected_day = _iso_day(business_day)
    missing = IndexTrendInput(index_code, "missing", (), "missing", "", None, None, "{}")
    if type(quote) is not dict:
        return missing
    observation = quote.get("_observation")
    parsed = _observation(observation, index_code=index_code)
    if parsed is None:
        return missing
    received_at, _, encoded = parsed
    if received_at > now:
        return missing
    try:
        if received_at.astimezone(_KST).date() != expected_day:
            return missing
    except (OverflowError, ValueError):
        return missing
    values = []
    for name in _TREND_FIELDS:
        field = observation["fields"][name]
        value = _finite_number(quote.get(name))
        if (field["status"] != "valid" or value is None or value != field["value"]
                or (name != "change_pct" and value <= 0)):
            return missing
        values.append((name, value))
    return IndexTrendInput(index_code, "success", tuple(values),
        "kis:FHPUP02100000:index" + index_code, observation["observation_id"],
        received_at, None, encoded)


def _observation(value: Any, *, index_code: str = "0001") -> tuple[datetime, float | None, str] | None:
    if type(value) is not dict or set(value) != _OBSERVATION_KEYS:
        return None
    if (type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["source"] != "kis" or value["source_tr"] != "FHPUP02100000"
            or value["index_code"] != index_code or value["market_as_of"] is not None):
        return None
    if not _uuid(value["observation_id"]):
        return None
    received_at = _parse_receipt(value["received_at"])
    if received_at is None:
        return None
    fields = value["fields"]
    if type(fields) is not dict or set(fields) != _FIELDS:
        return None
    values = {}
    for name, field in fields.items():
        parsed = _field(field)
        if parsed is _INVALID:
            return None
        values[name] = parsed
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        return None
    return received_at, values["change_pct"], encoded


_INVALID = object()


def _field(value: Any) -> float | None | object:
    if type(value) is not dict or set(value) != {"status", "value"}:
        return _INVALID
    status, raw = value["status"], value["value"]
    if type(status) is not str:
        return _INVALID
    if status in {"missing", "invalid"}:
        return None if raw is None else _INVALID
    if status != "valid":
        return _INVALID
    parsed = _finite_number(raw)
    return parsed if parsed is not None else _INVALID


def _finite_number(value: Any) -> float | None:
    if type(value) not in (int, float):
        return None
    try:
        parsed = float(value)
    except OverflowError:
        return None
    return parsed if math.isfinite(parsed) else None


def _parse_receipt(value: Any) -> datetime | None:
    if type(value) is not str or not value or value.strip() != value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.isoformat() != value:
        return None
    try:
        return _aware(parsed)
    except ValueError:
        return None


def _uuid(value: Any) -> bool:
    if type(value) is not str:
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def _iso_day(value: Any) -> date:
    if type(value) is not str:
        raise ValueError("business_day must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("business_day must be an ISO date") from exc
    if parsed.isoformat() != value:
        raise ValueError("business_day must be an ISO date")
    return parsed


def _aware(value: Any) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be an aware datetime")
    return value
