"""레짐 판단 시간 범위 분리 (2026-09-14 리뷰 후속 T9 요청 2 — F9 파생)

대상: src/core/market_regime.py
- mid_trend(5/20일) · open_expectation(장전, 09:30 만료) · intraday_risk(당일 급락) 분리
- 유효 레짐: intraday_risk 가 crash/severe 이면 강세 레짐을 bull 로 취급하지 않는다
- 결측은 0/중립이 아니라 None + missing 표식 + 사유

실행: venv/bin/python -m pytest tests/test_market_regime_horizons.py -q -p no:cacheprovider
"""

import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core.market_regime import (  # noqa: E402
    MarketRegimeAdapter,
    cap_regime_by_intraday_risk,
)


def _adapter(base_regime: str = "bull") -> MarketRegimeAdapter:
    adapter = MarketRegimeAdapter()
    adapter._current_regime = base_regime
    return adapter


# ── 유효 레짐 우선순위 ────────────────────────────────────────────────────────

def test_crash_does_not_let_stale_bull_stand(monkeypatch):
    """09-14 사례: 아침 자료 기준 bull 인데 장중 crash → 유효 레짐이 bull 이면 안 된다."""
    adapter = _adapter("bull")
    assert adapter.regime == "bull"

    import src.core.market_regime as _mr
    monkeypatch.setattr(_mr, "_now", lambda: datetime(2026, 9, 14, 12, 30))   # as_of 와 같은 날
    adapter.set_intraday_risk("crash", change_pct=-3.34, as_of=datetime(2026, 9, 14, 12, 0))

    assert adapter.regime == "sideways"
    assert adapter.mid_trend == "bull"          # 중기 관점은 보존
    assert adapter.params["max_daily_new_buys"] == 3   # sideways 파라미터가 적용


def test_severe_also_blocks_bull_and_summary_marks_cap():
    adapter = _adapter("bull")
    adapter.set_intraday_risk("severe", change_pct=-5.1)
    summary = adapter.get_summary()
    assert adapter.regime == "sideways"
    assert summary["regime"] == "sideways"
    assert summary["horizons"]["capped_by_intraday_risk"] is True
    assert summary["horizons"]["mid_trend"]["value"] == "bull"


def test_caution_and_normal_do_not_add_new_blocking():
    """새 차단을 추가하지 않는다 — caution/normal 에서는 기존 레짐 그대로."""
    for level in ("normal", "caution"):
        adapter = _adapter("bull")
        adapter.set_intraday_risk(level, change_pct=-0.9)
        assert adapter.regime == "bull", level


def test_crash_does_not_upgrade_non_bull_regimes():
    """crash 는 강세 강등만 한다 — bear/sideways 를 더 낮추지 않는다."""
    for base in ("bear", "sideways", "neutral"):
        adapter = _adapter(base)
        adapter.set_intraday_risk("crash", change_pct=-3.3)
        assert adapter.regime == base, base


def test_realtime_regime_change_is_not_masked_by_stale_view(monkeypatch):
    """실시간 update_regime 이 bear 로 바뀌면 유효 레짐·파라미터가 즉시 bear 다.

    (아침에 만들어진 강세 관점이 실시간 판단을 덮는 오버라이드가 있으면 실패한다)
    """
    import src.core.market_regime as mr

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 14, 12, 0)

    monkeypatch.setattr(mr, "datetime", _FixedDatetime)

    adapter = _adapter("bull")
    adapter._load_vix_cache_or_refresh = lambda: None
    # bear 전환 확인(30분) 이미 충족된 상태
    adapter._pending_regime = "bear"
    adapter._pending_since = datetime(2026, 9, 14, 10, 0)

    bear_data = {"change_pct": -3.3, "price": 96.0, "open": 100.0}
    adapter.update_regime(bear_data, bear_data)

    assert adapter._current_regime == "bear"
    assert adapter.regime == "bear"
    assert adapter.mid_trend == "bear"
    assert adapter.params["max_daily_new_buys"] == 1
    summary = adapter.get_summary()
    assert summary["horizons"]["mid_trend"]["value"] == "bear"
    assert summary["horizons"]["mid_trend"]["missing"] is False
    assert summary["horizons"]["mid_trend"]["as_of"] is not None


def test_no_mid_trend_override_setter_exists():
    """mid_trend 는 update_regime 결과 단일 출처 — 별도 설정 경로를 두지 않는다."""
    assert not hasattr(MarketRegimeAdapter, "set_mid_trend")


def test_intraday_risk_rejects_unknown_level():
    adapter = _adapter("bull")
    with pytest.raises(ValueError):
        adapter.set_intraday_risk("panic")


# ── 장전 개장 예상의 만료 ──────────────────────────────────────────────────────

def test_open_expectation_expires_at_0930():
    adapter = _adapter("neutral")
    as_of = datetime(2026, 9, 14, 8, 50)
    adapter.set_open_expectation("상승 갭 출발 가능성", as_of=as_of)

    assert adapter.open_expectation(now=datetime(2026, 9, 14, 9, 0)) == "상승 갭 출발 가능성"
    assert adapter.open_expectation(now=datetime(2026, 9, 14, 9, 31)) is None

    expired = adapter.get_summary(now=datetime(2026, 9, 14, 9, 31))["horizons"]["open_expectation"]
    assert expired["value"] is None
    assert expired["missing"] is True
    assert "09:30" in expired["reason"]
    assert expired["as_of"] == as_of.isoformat()


def test_open_expectation_expires_on_next_day():
    adapter = _adapter("neutral")
    adapter.set_open_expectation("상승", as_of=datetime(2026, 9, 14, 8, 50))
    assert adapter.open_expectation(now=datetime(2026, 9, 15, 8, 55)) is None


def test_open_expectation_never_moves_effective_regime():
    adapter = _adapter("sideways")
    adapter.set_open_expectation("강한 상승 갭", as_of=datetime(2026, 9, 14, 8, 50))
    assert adapter.regime == "sideways"


# ── 결측 표기 ────────────────────────────────────────────────────────────────

def test_missing_horizons_are_none_with_reason_not_zero():
    adapter = _adapter("neutral")
    horizons = adapter.get_summary()["horizons"]
    for key in ("open_expectation", "intraday_risk"):
        assert horizons[key]["value"] is None, key
        assert horizons[key]["missing"] is True, key
        assert horizons[key]["reason"], key
    # intraday 등락률도 0 으로 채우지 않는다
    assert horizons["intraday_risk"]["change_pct"] is None
    # update_regime 이 한 번도 안 돌았으면 mid_trend 도 결측이다 (기본 neutral 로 포장 금지)
    assert horizons["mid_trend"]["missing"] is True
    assert horizons["mid_trend"]["reason"]
    assert horizons["mid_trend"]["as_of"] is None
    assert horizons["mid_trend"]["source"] == "update_regime"


# ── LLM 레짐 문자열 캡 (kr_scheduler 가 사용하는 순수 함수) ─────────────────────

@pytest.mark.parametrize("regime,risk,expected", [
    ("trending_bull", "crash", "neutral"),
    ("trending_bull", "severe", "neutral"),
    ("trending_bull", "caution", "trending_bull"),
    ("trending_bull", "normal", "trending_bull"),
    ("trending_bull", None, "trending_bull"),
    ("bull", "crash", "sideways"),
    ("ranging", "severe", "ranging"),
    ("turning_point", "crash", "turning_point"),
    ("trending_bear", "crash", "trending_bear"),
])
def test_cap_regime_by_intraday_risk(regime, risk, expected):
    assert cap_regime_by_intraday_risk(regime, risk) == expected


# ── 장전 LLM 진단이 급락을 덮지 않는다 ─────────────────────────────────────────

def test_llm_attack_diagnosis_does_not_upgrade_during_crash(monkeypatch):
    """오래된 [공격] 장전 진단이 장중 급락 중 bear → sideways 상향을 하면 안 된다."""
    import asyncio
    monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)

    class _Resp:
        success = True
        content = "[공격] 반도체 수급 강세"
        error = None

    class _LLM:
        async def complete(self, *a, **kw):
            return _Resp()

    adapter = _adapter("bear")
    adapter.set_intraday_risk("crash", change_pct=-3.3)
    asyncio.run(adapter.llm_morning_diagnosis(_LLM()))

    assert adapter._current_regime == "bear"
    assert adapter.regime == "bear"
    # 진단 자체는 장전 예상으로 기록된다 (읽을 때 09:30 만료가 적용될 뿐)
    assert "[공격]" in (adapter.horizons.open_expectation or "")


def test_llm_defense_diagnosis_still_demotes_bull(monkeypatch):
    """기존 의미 보존 — [방어] 진단의 bull → sideways 강등은 그대로."""
    import asyncio
    monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)

    class _Resp:
        success = True
        content = "[방어] 관세 리스크"
        error = None

    class _LLM:
        async def complete(self, *a, **kw):
            return _Resp()

    adapter = _adapter("bull")
    asyncio.run(adapter.llm_morning_diagnosis(_LLM()))
    assert adapter._current_regime == "sideways"


def test_previous_day_intraday_risk_does_not_cap_next_morning(monkeypatch):
    """전일 15:3x crash 가 어댑터에 남아도 다음 날 장전 유효 레짐을 강등하지 않는다 (2026-09-14 재리뷰)."""
    import src.core.market_regime as _mr
    adapter = _adapter("bull")
    adapter.set_intraday_risk("crash", change_pct=-3.26, as_of=datetime(2026, 9, 14, 15, 34))
    monkeypatch.setattr(_mr, "_now", lambda: datetime(2026, 9, 15, 8, 20))
    assert adapter.regime == "bull"                       # 전일 위험은 결측 취급
    monkeypatch.setattr(_mr, "_now", lambda: datetime(2026, 9, 14, 15, 40))
    assert adapter.regime == "sideways"                   # 당일이면 강등
