"""주문 제출 시점에 고정하는 관측 ID. 주문 판단이나 복구 권한으로 쓰지 않는다."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping


_VERSION = 'order-attribution-v1'
_ID_FIELDS = ('candidate_id', 'plan_id', 'signal_id', 'llm_decision_id')
_ID_PATTERN = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,159}')
_REASONS = frozenset({'not_available', 'invalid', 'conflict', 'legacy_not_recorded'})
_DEFAULT_REASONS = tuple((name, 'not_available') for name in _ID_FIELDS)


def _valid_id(value: Any) -> bool:
    return type(value) is str and _ID_PATTERN.fullmatch(value) is not None


def validate_attribution(payload: Any) -> dict:
    """영속 형식은 엄격히 검사하고 복사한다. 과거 행에 기본값을 삽입하지 않는다."""
    if type(payload) is not dict or set(payload) != {'version', *_ID_FIELDS, 'missing_reasons'}:
        raise ValueError('귀속 필드가 일치하지 않습니다')
    if type(payload['version']) is not str or payload['version'] != _VERSION:
        raise ValueError('지원하지 않는 귀속 버전')
    missing = set()
    for name in _ID_FIELDS:
        value = payload[name]
        if value is None:
            missing.add(name)
        elif not _valid_id(value):
            raise ValueError('유효하지 않은 귀속 ID')
    reasons = payload['missing_reasons']
    if type(reasons) is not dict or set(reasons) != missing:
        raise ValueError('귀속 결측 사유의 필드가 일치하지 않습니다')
    if any(type(reason) is not str or reason not in _REASONS for reason in reasons.values()):
        raise ValueError('지원하지 않는 귀속 결측 사유')
    return {
        'version': _VERSION, **{name: payload[name] for name in _ID_FIELDS},
        'missing_reasons': dict(reasons),
    }


@dataclass(frozen=True)
class OrderAttribution:
    """원본 ID와 결측 사유의 불변 스냅샷. 일반 입력은 freeze_attribution으로 만든다."""

    candidate_id: str | None = None
    plan_id: str | None = None
    signal_id: str | None = None
    llm_decision_id: str | None = None
    missing_reasons: tuple[tuple[str, str], ...] = _DEFAULT_REASONS

    def __post_init__(self):
        self.to_dict()

    def to_dict(self) -> dict:
        # dict 변환 전에 중복·가변 쌍을 거부해 비정규 객체를 정상으로 승격하지 않는다.
        pairs = self.missing_reasons
        if type(pairs) is not tuple or any(
            type(pair) is not tuple or len(pair) != 2
            or type(pair[0]) is not str or type(pair[1]) is not str
            for pair in pairs
        ):
            raise ValueError('귀속 결측 사유는 불변 쌍이어야 합니다')
        reasons = dict(pairs)
        if len(reasons) != len(pairs):
            raise ValueError('중복된 귀속 결측 사유')
        return validate_attribution({
            'version': _VERSION,
            **{name: getattr(self, name) for name in _ID_FIELDS},
            'missing_reasons': reasons,
        })


def freeze_attribution(
    *, candidate_id=None, plan_id=None, signal_id=None, llm_decision_id=None,
    missing_reasons: Mapping[str, str] | None = None,
) -> OrderAttribution:
    """관측 입력은 문자열로 강제 변환하지 않고 잘못된 값만 결측으로 격리한다."""
    values = {
        'candidate_id': candidate_id, 'plan_id': plan_id,
        'signal_id': signal_id, 'llm_decision_id': llm_decision_id,
    }
    reasons = []
    supplied = missing_reasons if isinstance(missing_reasons, Mapping) else {}
    for name, value in values.items():
        if _valid_id(value):
            continue
        reason = supplied.get(name, 'not_available') if value is None else 'invalid'
        if type(reason) is not str or reason not in _REASONS:
            reason = 'invalid'
        values[name] = None
        reasons.append((name, reason))
    return OrderAttribution(**values, missing_reasons=tuple(reasons))
