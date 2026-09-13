"""손절 해석 공통 함수 (2026-09-14 리뷰 후속 T2 — F1 실제 손절 기준 위험 사이징)

대상: src/utils/stop_policy.py resolve_effective_stop
우선순위 dynamic(min 하한 클램프) > strategy(미클램프) > global, 비코어만 급락 cap.
실행: venv/bin/python -m pytest tests/test_stop_policy.py -q -p no:cacheprovider
"""
import sys
from decimal import Decimal as D
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.utils.stop_policy import StopDecision, resolve_effective_stop, stop_pct_or_none  # noqa: E402


def test_fixed_stop_is_not_dynamic_clamped():
    result = resolve_effective_stop(
        dynamic_stop_pct=None, fixed_stop_pct=D("3"), global_stop_pct=D("5"),
        min_dynamic_stop_pct=D("4"), crash_stop_pct=None, is_core=False)
    assert result.stop_pct == D("3")
    assert result.source == "strategy"


def test_dynamic_stop_then_crash_cap():
    result = resolve_effective_stop(
        dynamic_stop_pct=D("2"), fixed_stop_pct=D("5"), global_stop_pct=D("5"),
        min_dynamic_stop_pct=D("4"), crash_stop_pct=D("2.5"), is_core=False)
    assert result.stop_pct == D("2.5")
    assert result.crash_capped is True


@pytest.mark.parametrize("dynamic,fixed,crash,is_core,expected", [
    # global fallback: dynamic·strategy 없음 → global 5
    (None, None, None, False, StopDecision(D("5"), "global", False)),
    # global 도 급락 cap 대상 (비코어)
    (None, None, D("2.5"), False, StopDecision(D("2.5"), "global", True)),
    # dynamic 은 min 하한 클램프 (2 → 4), cap 없음
    (D("2"), D("5"), None, False, StopDecision(D("4"), "dynamic", False)),
    # dynamic 이 min 보다 크면 그대로
    (D("6"), D("5"), None, False, StopDecision(D("6"), "dynamic", False)),
    # strategy 가 crash 보다 이미 타이트하면 cap 미발동
    (None, D("2"), D("2.5"), False, StopDecision(D("2"), "strategy", False)),
    # core 예외: 급락 cap 미적용 (코어 SL 10 유지)
    (None, D("10"), D("2.5"), True, StopDecision(D("10"), "strategy", False)),
    # core 도 dynamic 이 있으면 dynamic 우선 (호출자가 코어엔 dynamic 을 만들지 않는 것이 규약)
    (D("3"), D("10"), D("2.5"), True, StopDecision(D("4"), "dynamic", False)),
])
def test_priority_table(dynamic, fixed, crash, is_core, expected):
    result = resolve_effective_stop(
        dynamic_stop_pct=dynamic, fixed_stop_pct=fixed, global_stop_pct=D("5"),
        min_dynamic_stop_pct=D("4"), crash_stop_pct=crash, is_core=is_core)
    assert result == expected


@pytest.mark.parametrize("kw", [
    dict(fixed_stop_pct=D("0")),
    dict(fixed_stop_pct=D("-1")),
    dict(dynamic_stop_pct=D("NaN")),
    dict(global_stop_pct=D("0")),
    dict(min_dynamic_stop_pct=D("Infinity")),
    dict(crash_stop_pct=D("0")),
])
def test_invalid_settings_raise(kw):
    base = dict(dynamic_stop_pct=None, fixed_stop_pct=D("5"), global_stop_pct=D("5"),
                min_dynamic_stop_pct=D("4"), crash_stop_pct=None, is_core=False)
    base.update(kw)
    with pytest.raises(ValueError):
        resolve_effective_stop(**base)


def test_stop_pct_or_none_boundary_conversion():
    assert stop_pct_or_none(None) is None
    assert stop_pct_or_none(5.0) == D("5.0")
    assert stop_pct_or_none("3.5") == D("3.5")
    with pytest.raises(ValueError):
        stop_pct_or_none("abc")
