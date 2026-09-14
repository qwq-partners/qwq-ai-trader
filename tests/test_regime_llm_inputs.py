"""LLM 레짐 분류기의 입력 신선도·정직성 테스트 (2026-09-14 리뷰 요청1·3·5)

대상: src/schedulers/kr_scheduler.py
      ::KRScheduler._run_llm_regime_classifier   — 장중 최신 지수 재조회·급락 상태 반영·as_of 표기
      ::KRScheduler._apply_regime_to_exit_manager — 충돌 방지 장치도 같은 최신 자료 사용
      ::_index_field                              — 공급자/소비자 지수 키 표기 불일치(결측을 0으로 채우지 않음)

재현하는 결함(2026-09-14 운영 관측):
  ② 12:00 장중 재분류가 08:20 스크리너 메모리(KOSPI 5일 +3.3%/20일 +1.4%)를 재사용해
     당일 -3.34% 급락·crash 상태를 보지 못한 채 trending_bull 0.85 를 그대로 적용.
  ③ 소비자는 indices["SP500"]/["SOX"]/["VIX"] 를 찾지만 공급자는 "S&P500"/"반도체(SOX)"
     를 내고 VIX 는 공급하지 않음 → 결측이 0.0 으로 프롬프트에 실림.
  요청5 저녁 훅: 모닝브리프 사후 평가 메서드가 있으면 1회 호출, 없으면 무시, 예외는 격리.

실행: venv/bin/python -m pytest tests/test_regime_llm_inputs.py -q
네트워크·KIS/LLM 자격증명 무접촉 — 공급자·LLM·캐시 경로는 전부 가짜.
"""

import asyncio
import sys
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.schedulers import kr_scheduler  # noqa: E402
from src.schedulers.kr_scheduler import KRScheduler  # noqa: E402


# ── 가짜 협력자 ────────────────────────────────────────────────────────────────

class _Screener:
    """08:20 아침 스캔이 남긴 메모리 — 장중에는 갱신되지 않는다."""

    def __init__(self, c5, c20, level=2500.0, regime="bull", closes=None):
        self._kospi = {"c5": c5, "c20": c20, "level": level}
        self._regime = regime
        self._kospi_closes = list(closes) if closes else [level] * 30

    def get_kospi_change(self):
        return dict(self._kospi)

    def get_market_regime(self):
        return self._regime


class _BatchAnalyzer:
    def __init__(self, screener, intraday_state="normal", intraday_pct=0.0):
        self._screener = screener
        self._intraday_state = intraday_state
        self._intraday_kospi_pct = intraday_pct


class _KisMarketData:
    """fetch_index_price: 코드별 응답을 미리 넣어 둔다. None 이면 조회 실패."""

    def __init__(self, responses):
        self._responses = responses
        self.calls = []

    async def fetch_index_price(self, index_code="0001"):
        self.calls.append(index_code)
        return self._responses.get(index_code)


class _ExitManager:
    def __init__(self, crash_level="normal"):
        self._intraday_crash_level = crash_level
        self.applied = []

    def apply_regime_params(self, regime):
        self.applied.append(regime)
        return 1


class _LLM:
    def __init__(self, result):
        self._result = result
        self.prompts = []

    async def complete_json(self, prompt, system=None, task=None, **kwargs):
        self.prompts.append(prompt)
        return dict(self._result)


def _make_bot(*, screener, intraday_state="normal", intraday_pct=0.0,
              kis_responses=None, crash_level="normal"):
    ba = _BatchAnalyzer(screener, intraday_state, intraday_pct)
    return SimpleNamespace(
        batch_analyzer=ba,
        kis_market_data=_KisMarketData(kis_responses or {}),
        exit_manager=_ExitManager(crash_level),
        config={"kr": {"llm_ops": {"regime_conflict_guard_enabled": True}}},
    )


def _patch_env(monkeypatch, tmp_path, bot, llm, *, indices, now):
    """Path.home()·LLM·US 공급자·시계를 전부 가짜로 교체."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    import src.utils.llm as llm_mod
    monkeypatch.setattr(llm_mod, "get_llm_manager", lambda: llm)

    import src.data.providers.us_market_data as umd_mod

    class _UMD:
        async def get_overnight_signal(self):
            return {"indices": dict(indices)}

    monkeypatch.setattr(umd_mod, "get_us_market_data", lambda: _UMD())
    monkeypatch.setattr(kr_scheduler, "_now_kst", lambda: now, raising=False)

    sched = object.__new__(KRScheduler)
    sched.bot = bot
    return sched


def _regime_file(tmp_path):
    import json
    path = tmp_path / ".cache" / "ai_trader" / "llm_regime_today.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


_PROVIDER_INDICES = {
    # 실제 us_market_data.get_overnight_signal() 이 내는 표기 (VIX 없음)
    "S&P500": {"price": 6000.0, "change": 30.0, "change_pct": 0.51},
    "NASDAQ": {"price": 20000.0, "change": 120.0, "change_pct": 0.60},
    "반도체(SOX)": {"price": 5500.0, "change": 90.0, "change_pct": 1.64},
    "다우": {"price": 44000.0, "change": 100.0, "change_pct": 0.23},
}


# ── ② 장중 재분류가 아침 메모리를 재사용 ───────────────────────────────────────

def test_noon_reclassification_uses_fresh_index_and_crash_state(monkeypatch, tmp_path):
    """12:00 재분류는 최신 지수를 재조회하고 급락 상태를 LLM 입력에 넣는다."""
    screener = _Screener(c5=3.3, c20=1.4, level=2500.0,
                         closes=[2400.0 + i for i in range(30)])
    bot = _make_bot(
        screener=screener,
        intraday_state="crash",
        intraday_pct=-3.34,
        kis_responses={
            "0001": {"price": 2416.0, "change": -83.5, "change_pct": -3.34, "label": "KOSPI"},
            "1001": {"price": 700.0, "change": -20.0, "change_pct": -2.78, "label": "KOSDAQ"},
        },
        crash_level="crash",
    )
    llm = _LLM({"regime": "trending_bull", "lead_strategy": "sepa", "confidence": 0.85,
                "reasoning": "미장 강세"})
    sched = _patch_env(monkeypatch, tmp_path, bot, llm,
                       indices=_PROVIDER_INDICES,
                       now=datetime(2026, 9, 14, 12, 0, 0))

    asyncio.run(sched._run_llm_regime_classifier(label="12:00 (장중 업데이트)"))

    assert llm.prompts, "LLM 호출이 없었다"
    prompt = llm.prompts[0]
    # 최신 당일 등락률이 프롬프트에 있어야 한다
    assert "-3.34" in prompt, f"최신 KOSPI 등락률 누락:\n{prompt}"
    # 아침 메모리의 낙관 수치가 최신 자료 없이 단독으로 실리면 안 된다
    assert "crash" in prompt, f"급락 감지기 상태 누락:\n{prompt}"
    assert "as_of" in prompt, f"기준 시각(as_of) 표기 누락:\n{prompt}"
    assert bot.kis_market_data.calls, "장중 실행인데 최신 지수를 재조회하지 않았다"

    # crash 상태에서는 LLM 이 trending_bull 을 줘도 그대로 적용하지 않는다
    saved = _regime_file(tmp_path)
    assert saved.get("regime") != "trending_bull", f"급락 중 trending_bull 적용됨: {saved}"
    assert saved.get("llm_regime_raw") == "trending_bull", "원본 LLM 응답을 보존해야 한다"


def test_noon_index_fetch_failure_falls_back_with_stale_label(monkeypatch, tmp_path):
    """최신 조회 실패 시 아침 값을 쓰되 '장중 갱신 실패'를 프롬프트에 명시한다."""
    screener = _Screener(c5=3.3, c20=1.4)
    bot = _make_bot(screener=screener, kis_responses={"0001": None, "1001": None})
    llm = _LLM({"regime": "ranging", "confidence": 0.6})
    sched = _patch_env(monkeypatch, tmp_path, bot, llm,
                       indices=_PROVIDER_INDICES,
                       now=datetime(2026, 9, 14, 12, 0, 0))

    asyncio.run(sched._run_llm_regime_classifier(label="12:00 (장중 업데이트)"))

    prompt = llm.prompts[0]
    assert "장중 갱신 실패" in prompt, f"폴백 라벨 누락:\n{prompt}"
    assert "08:20" in prompt, f"아침 캐시 기준 시각 표기 누락:\n{prompt}"


def test_morning_run_does_not_fetch_intraday_index(monkeypatch, tmp_path):
    """08:10 실행은 장 시작 전이므로 장중 재조회를 하지 않는다(기존 동작 보존)."""
    screener = _Screener(c5=3.3, c20=1.4)
    bot = _make_bot(screener=screener,
                    kis_responses={"0001": {"change_pct": -3.34}})
    llm = _LLM({"regime": "ranging", "confidence": 0.6})
    sched = _patch_env(monkeypatch, tmp_path, bot, llm,
                       indices=_PROVIDER_INDICES,
                       now=datetime(2026, 9, 14, 8, 10, 0))

    asyncio.run(sched._run_llm_regime_classifier(label="08:10"))

    assert bot.kis_market_data.calls == [], "장 시작 전인데 지수를 재조회했다"


# ── ③ 지수 필드명 불일치 → 결측을 0 으로 채움 ──────────────────────────────────

def test_index_field_accepts_both_spellings_and_returns_none_when_missing():
    """_index_field: 공급자/소비자 표기를 모두 받고, 없으면 None (0 금지)."""
    f = kr_scheduler._index_field
    assert f(_PROVIDER_INDICES, "SP500", "S&P500") == pytest.approx(0.51)
    assert f(_PROVIDER_INDICES, "SOX", "반도체(SOX)") == pytest.approx(1.64)
    # VIX 는 공급되지 않는다 → 0 이 아니라 None
    assert f(_PROVIDER_INDICES, "VIX", "VIX(공포지수)",
             fields=("value", "price")) is None
    assert f({}, "SP500") is None
    assert f(None, "SP500") is None


def test_missing_index_is_reported_as_missing_not_zero(monkeypatch, tmp_path):
    """공급자 형태 dict 에서 S&P500/SOX 는 값으로, VIX 는 결측으로 프롬프트에 실린다."""
    screener = _Screener(c5=0.5, c20=0.2)
    bot = _make_bot(screener=screener)
    llm = _LLM({"regime": "ranging", "confidence": 0.6})
    sched = _patch_env(monkeypatch, tmp_path, bot, llm,
                       indices=_PROVIDER_INDICES,
                       now=datetime(2026, 9, 14, 8, 10, 0))

    asyncio.run(sched._run_llm_regime_classifier(label="08:10"))

    prompt = llm.prompts[0]
    assert "+0.51" in prompt, f"S&P500 값이 실리지 않았다(0 으로 대체됨):\n{prompt}"
    assert "+1.64" in prompt, f"SOX 값이 실리지 않았다(0 으로 대체됨):\n{prompt}"
    assert "VIX" in prompt and "결측" in prompt, f"VIX 결측 표식 누락:\n{prompt}"
    assert "VIX: 0.0" not in prompt, f"결측을 0 으로 채웠다:\n{prompt}"

    saved = _regime_file(tmp_path)
    assert "VIX" in (saved.get("input_meta", {}).get("missing_fields") or []), (
        f"missing_fields 메타 누락: {saved.get('input_meta')}"
    )


# ── 충돌 방지 장치도 최신 급락 상태를 본다 ────────────────────────────────────

def test_conflict_guard_uses_intraday_crash_state(monkeypatch, tmp_path):
    """08:20 스크리너가 bull 이어도 장중 crash 면 trending_bull 을 적용하지 않는다."""
    import json

    screener = _Screener(c5=3.3, c20=1.4, regime="bull")
    bot = _make_bot(screener=screener, intraday_state="crash",
                    intraday_pct=-3.34, crash_level="crash")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    cache = tmp_path / ".cache" / "ai_trader"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "llm_regime_today.json").write_text(
        json.dumps({"regime": "trending_bull", "date": date.today().isoformat()}),
        encoding="utf-8",
    )

    sched = object.__new__(KRScheduler)
    sched.bot = bot
    asyncio.run(sched._apply_regime_to_exit_manager())

    assert bot.exit_manager.applied, "ExitManager 갱신이 호출되지 않았다"
    assert bot.exit_manager.applied[-1] != "trending_bull", (
        f"장중 crash 인데 trending_bull 적용됨: {bot.exit_manager.applied}"
    )


# ── 요청5: 저녁 모닝브리프 사후 평가 훅 ───────────────────────────────────────

def test_evening_brief_eval_hook_calls_once_when_available():
    calls = []

    class _RG:
        async def evaluate_morning_brief(self, day):
            calls.append(day)
            return {"verdict": "miss"}

    bot = SimpleNamespace(report_generator=_RG())
    today = date(2026, 9, 14)
    asyncio.run(KRScheduler._run_morning_brief_evaluation(bot, today))
    asyncio.run(KRScheduler._run_morning_brief_evaluation(bot, today))
    assert calls == [today], f"1회만 호출해야 한다: {calls}"


def test_evening_brief_eval_hook_skips_when_method_absent():
    bot = SimpleNamespace(report_generator=SimpleNamespace())
    asyncio.run(KRScheduler._run_morning_brief_evaluation(bot, date(2026, 9, 14)))
    bot2 = SimpleNamespace(report_generator=None)
    asyncio.run(KRScheduler._run_morning_brief_evaluation(bot2, date(2026, 9, 14)))


def test_evening_brief_eval_hook_isolates_exception():
    class _RG:
        async def evaluate_morning_brief(self, day):
            raise RuntimeError("B 구현 미완")

    bot = SimpleNamespace(report_generator=_RG())
    asyncio.run(KRScheduler._run_morning_brief_evaluation(bot, date(2026, 9, 14)))
