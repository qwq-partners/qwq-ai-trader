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

_UNSET = object()


class _Screener:
    """08:20 아침 스캔이 남긴 메모리 — 장중에는 갱신되지 않는다."""

    def __init__(self, c5, c20, level=2500.0, regime="bull", closes=None,
                 loaded_at=None, last_bar_date=None):
        self._kospi = {"c5": c5, "c20": c20, "level": level}
        self._regime = regime
        self._kospi_closes = list(closes) if closes is not None else [level] * 30
        self._kospi_loaded_at = loaded_at or datetime(2026, 9, 14, 8, 20, 0)
        self._kospi_last_bar_date = (
            last_bar_date if last_bar_date is not None else date(2026, 9, 13)
        )

    def get_kospi_change(self):
        return dict(self._kospi)

    def get_market_regime(self):
        return self._regime


class _BatchAnalyzer:
    def __init__(self, screener, intraday_state="normal", intraday_pct=0.0,
                 updated_at=_UNSET):
        self._screener = screener
        self._intraday_state = intraday_state
        self._intraday_kospi_pct = intraday_pct
        # 기본값: 오늘 갱신된 상태 (일자 가드 통과)
        self._intraday_updated_at = (
            datetime(2026, 9, 14, 9, 5, 0) if updated_at is _UNSET else updated_at
        )


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


class _RegimeAdapter:
    """B(market_regime.MarketRegimeAdapter) 대역 — set_intraday_risk 인자만 기록."""

    def __init__(self):
        self.calls = []

    def set_intraday_risk(self, level, change_pct=None, as_of=None):
        if level not in ("normal", "caution", "crash", "severe"):
            raise ValueError(level)
        self.calls.append((level, change_pct, as_of))


def _make_bot(*, screener, intraday_state="normal", intraday_pct=0.0,
              kis_responses=None, crash_level="normal", updated_at=_UNSET):
    ba = _BatchAnalyzer(screener, intraday_state, intraday_pct, updated_at=updated_at)
    return SimpleNamespace(
        batch_analyzer=ba,
        kis_market_data=_KisMarketData(kis_responses or {}),
        exit_manager=_ExitManager(crash_level),
        engine=SimpleNamespace(_regime_adapter=_RegimeAdapter()),
        config={"kr": {"llm_ops": {"regime_conflict_guard_enabled": True}}},
    )


def _patch_env(monkeypatch, tmp_path, bot, llm, *, indices, now, normalized=None):
    """Path.home()·LLM·US 공급자·시계를 전부 가짜로 교체."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    import src.utils.llm as llm_mod
    monkeypatch.setattr(llm_mod, "get_llm_manager", lambda: llm)

    import src.data.providers.us_market_data as umd_mod

    class _UMD:
        async def get_overnight_signal(self):
            payload = {"indices": dict(indices)}
            if normalized is not None:
                payload["indices_normalized"] = dict(normalized)
            return payload

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
    # 고정 문자열이 아니라 스크리너가 벤치마크를 실제로 로드한 시각이 실린다
    assert "2026-09-14 08:20" in prompt, f"스크리너 로드 시각 표기 누락:\n{prompt}"


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
    # 감지기 갱신 시각(고정 2026-09-14)과 같은 날로 시계를 고정 — 실제 날짜가 넘어가면
    # 당일 게이트가 "전일 상태" 로 판정해 테스트가 날짜에 따라 깨진다 (2026-09-15 격리 점검)
    monkeypatch.setattr(kr_scheduler, "_now_kst", lambda: datetime(2026, 9, 14, 10, 0, 0))
    cache = tmp_path / ".cache" / "ai_trader"
    cache.mkdir(parents=True, exist_ok=True)
    # 파일 기준일도 고정 시계와 같은 날 — 소비자의 당일 게이트가 _now_kst 로 통일됐다
    # (2026-09-15 T10 F14: date.today() 와 _now_kst() 이중 시계 제거)
    (cache / "llm_regime_today.json").write_text(
        json.dumps({"regime": "trending_bull", "date": "2026-09-14"}),
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

def test_evening_brief_eval_hook_calls_once_when_available(monkeypatch):
    monkeypatch.setattr(KRScheduler, "_morning_brief_eval_date", None)
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


def test_evening_brief_eval_hook_skips_when_method_absent(monkeypatch):
    # 클래스 속성 누수로 날짜 가드에 걸려 테스트가 무효화되지 않게 매번 초기화
    monkeypatch.setattr(KRScheduler, "_morning_brief_eval_date", None)
    bot = SimpleNamespace(report_generator=SimpleNamespace())
    asyncio.run(KRScheduler._run_morning_brief_evaluation(bot, date(2026, 9, 14)))
    monkeypatch.setattr(KRScheduler, "_morning_brief_eval_date", None)
    bot2 = SimpleNamespace(report_generator=None)
    asyncio.run(KRScheduler._run_morning_brief_evaluation(bot2, date(2026, 9, 14)))


def test_evening_brief_eval_hook_isolates_exception(monkeypatch):
    monkeypatch.setattr(KRScheduler, "_morning_brief_eval_date", None)
    called = []

    class _RG:
        async def evaluate_morning_brief(self, day):
            called.append(day)
            raise RuntimeError("평가 실패")

    bot = SimpleNamespace(report_generator=_RG())
    asyncio.run(KRScheduler._run_morning_brief_evaluation(bot, date(2026, 9, 14)))
    assert called, "날짜 가드에 걸려 실제 경로에 도달하지 못했다"


def test_evening_brief_eval_hook_times_out(monkeypatch):
    """평가가 지연되면 저녁 잡을 막지 않고 타임아웃으로 빠져나온다."""
    monkeypatch.setattr(KRScheduler, "_morning_brief_eval_date", None)
    monkeypatch.setattr(kr_scheduler, "_MORNING_BRIEF_EVAL_TIMEOUT", 0.05, raising=False)

    class _RG:
        async def evaluate_morning_brief(self, day):
            await asyncio.sleep(5)

    bot = SimpleNamespace(report_generator=_RG())
    import time as _t
    _t0 = _t.monotonic()
    result = asyncio.run(KRScheduler._run_morning_brief_evaluation(bot, date(2026, 9, 14)))
    assert result is None                       # 타임아웃 경로: note 없음
    assert _t.monotonic() - _t0 < 1.0            # 5초 sleep 을 기다리지 않았다


# ── blocking #1: 스크리너 종가열이 비면 c5/c20 은 0 이 아니라 결측 ──────────────

def test_empty_screener_cache_reports_missing_not_zero(monkeypatch, tmp_path):
    """재시작 직후 08:10 — 종가열이 비면 '+0.0%' 가 아니라 결측으로 실린다."""
    screener = _Screener(c5=0.0, c20=0.0, level=0.0, closes=[])
    bot = _make_bot(screener=screener)
    llm = _LLM({"regime": "ranging", "confidence": 0.6})
    sched = _patch_env(monkeypatch, tmp_path, bot, llm,
                       indices=_PROVIDER_INDICES,
                       now=datetime(2026, 9, 14, 8, 10, 0))

    asyncio.run(sched._run_llm_regime_classifier(label="08:10"))

    prompt = llm.prompts[0]
    assert "+0.0%" not in prompt, f"빈 캐시를 0.0% 로 포장했다:\n{prompt}"
    assert "스크리너 캐시 없음" in prompt, f"캐시 없음 표기 누락:\n{prompt}"
    missing = _regime_file(tmp_path).get("input_meta", {}).get("missing_fields") or []
    assert "KOSPI_c5" in missing and "KOSPI_c20" in missing, f"결측 집계 누락: {missing}"


def test_short_close_series_reports_c20_missing(monkeypatch, tmp_path):
    """종가열이 6개 이상 21개 미만이면 c5 만 유효, c20 은 결측 (0 금지)."""
    screener = _Screener(c5=1.0, c20=0.0, level=2500.0,
                         closes=[2400.0 + i for i in range(10)])
    # 09-14 월요일의 직전 거래일은 일요일(09-13)이 아니라 금요일이다.
    screener._kospi_last_bar_date = datetime(2026, 9, 11).date()
    bot = _make_bot(screener=screener)
    llm = _LLM({"regime": "ranging", "confidence": 0.6})
    sched = _patch_env(monkeypatch, tmp_path, bot, llm,
                       indices=_PROVIDER_INDICES,
                       now=datetime(2026, 9, 14, 8, 10, 0))

    asyncio.run(sched._run_llm_regime_classifier(label="08:10"))

    missing = _regime_file(tmp_path).get("input_meta", {}).get("missing_fields") or []
    assert "KOSPI_c20" in missing, f"20일 결측 집계 누락: {missing}"
    assert "KOSPI_c5" not in missing, f"5일은 계산 가능한데 결측 처리됨: {missing}"


# ── blocking #2: 전일 급락 상태가 다음 날 최신 사실로 실리면 안 된다 ────────────

def test_stale_crash_state_from_previous_day_is_ignored(monkeypatch, tmp_path):
    """전일 15:34 crash 잔존 → 다음 날 08:10 에는 결측 처리·캡 미발동."""
    screener = _Screener(c5=3.3, c20=1.4)
    bot = _make_bot(screener=screener, intraday_state="crash", intraday_pct=-3.26,
                    updated_at=datetime(2026, 9, 13, 15, 34, 0))
    llm = _LLM({"regime": "trending_bull", "confidence": 0.8})
    sched = _patch_env(monkeypatch, tmp_path, bot, llm,
                       indices=_PROVIDER_INDICES,
                       now=datetime(2026, 9, 14, 8, 10, 0))

    asyncio.run(sched._run_llm_regime_classifier(label="08:10"))

    prompt = llm.prompts[0]
    assert "상태: crash" not in prompt, f"전일 급락 상태가 당일 사실로 실렸다:\n{prompt}"
    saved = _regime_file(tmp_path)
    assert saved.get("regime") == "trending_bull", f"전일 상태로 캡이 걸렸다: {saved}"
    meta = saved.get("input_meta", {})
    assert meta.get("intraday_crash_level") is None
    assert any("급락감지기" in m for m in (meta.get("missing_fields") or [])), meta


def test_stale_crash_state_does_not_cap_conflict_guard(monkeypatch, tmp_path):
    """충돌 방지 장치도 전일 급락 상태로는 캡을 걸지 않는다."""
    import json

    screener = _Screener(c5=0.2, c20=0.1, regime="neutral")
    bot = _make_bot(screener=screener, intraday_state="crash", intraday_pct=-3.26,
                    updated_at=datetime(2026, 9, 13, 15, 34, 0))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(kr_scheduler, "_now_kst",
                        lambda: datetime(2026, 9, 14, 8, 10, 0), raising=False)
    cache = tmp_path / ".cache" / "ai_trader"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "llm_regime_today.json").write_text(
        json.dumps({"regime": "trending_bull", "date": "2026-09-14"}),
        encoding="utf-8",
    )

    sched = object.__new__(KRScheduler)
    sched.bot = bot
    asyncio.run(sched._apply_regime_to_exit_manager())

    assert bot.exit_manager.applied[-1] == "trending_bull", (
        f"전일 급락 상태로 캡이 걸렸다: {bot.exit_manager.applied}"
    )


def test_update_intraday_state_records_updated_at():
    """batch_analyzer 가 감지기 갱신 시각을 기록한다 (스냅샷 일자 가드의 근거)."""
    from src.core.batch_analyzer import BatchAnalyzer

    ba = object.__new__(BatchAnalyzer)
    ba._intraday_state = "normal"
    ba._intraday_kospi_pct = 0.0
    ba._intraday_updated_at = None
    ba._exit_manager = None
    ba._intraday_recovery_until = None

    before = datetime.now()
    state = asyncio.run(ba.update_intraday_state(-2.6))
    assert state == "crash"
    assert ba._intraday_updated_at is not None
    assert ba._intraday_updated_at >= before


# ── advisory (d): 당일 봉이 이미 있으면 장중 지수를 덧붙이지 않는다 ─────────────

def test_intraday_recompute_replaces_today_bar(monkeypatch, tmp_path):
    """catch-up 스캔이 장중에 로드한 당일 봉은 **교체**한다 (이중 계상도, 오전 값 유지도 아님).

    2026-09-15 정정: 이 테스트는 원래 `skips_when_last_bar_is_today` 라는 이름으로
    "오전 봉을 그대로 두고 c5/c20 재계산을 생략" 하는 동작을 정답으로 고정했다.
    그 경로는 `kr_as_of` 만 now 로 갱신해 오전 값을 최신 값처럼 보이게 했고
    (T10 F13 재현 입력: 오전 봉 104 → 정오 97 인데 c5 가 +4% 로 실림),
    12:00 재분류가 급락을 놓친 원인 중 하나였다. 올바른 동작은 교체다.
    """
    from src.schedulers.kr_scheduler import _pct_change

    closes = [2400.0 + i for i in range(30)]
    screener = _Screener(c5=0.0, c20=0.0, level=closes[-1], closes=closes,
                         loaded_at=datetime(2026, 9, 14, 10, 5, 0),
                         last_bar_date=date(2026, 9, 14))
    bot = _make_bot(
        screener=screener,
        kis_responses={"0001": {"price": 2100.0, "change_pct": -3.34},
                       "1001": {"change_pct": -2.78}},
    )
    llm = _LLM({"regime": "ranging", "confidence": 0.6})
    sched = _patch_env(monkeypatch, tmp_path, bot, llm,
                       indices=_PROVIDER_INDICES,
                       now=datetime(2026, 9, 14, 12, 0, 0))

    asyncio.run(sched._run_llm_regime_classifier(label="12:00 (장중 업데이트)"))

    meta = _regime_file(tmp_path).get("input_meta", {})
    assert meta.get("kospi_c5") == _pct_change(closes[:-1] + [2100.0], 5), meta
    assert meta.get("kospi_today_pct") == -3.34, meta
    # 당일 봉이 중복 누적되지 않는다 (스크리너 메모리 무변형)
    assert screener._kospi_closes == closes


def test_intraday_recompute_appends_when_last_bar_is_previous_day(monkeypatch, tmp_path):
    """마지막 봉이 전 거래일이면 당일 지수를 덧붙여 5일 변화율을 갱신한다."""
    from src.schedulers.kr_scheduler import _pct_change

    closes = [2400.0 + i for i in range(30)]
    screener = _Screener(c5=0.0, c20=0.0, level=closes[-1], closes=closes,
                         last_bar_date=date(2026, 9, 11))
    bot = _make_bot(
        screener=screener,
        kis_responses={"0001": {"price": 2100.0, "change_pct": -3.34},
                       "1001": {"change_pct": -2.78}},
    )
    llm = _LLM({"regime": "ranging", "confidence": 0.6})
    sched = _patch_env(monkeypatch, tmp_path, bot, llm,
                       indices=_PROVIDER_INDICES,
                       now=datetime(2026, 9, 14, 12, 0, 0))

    asyncio.run(sched._run_llm_regime_classifier(label="12:00 (장중 업데이트)"))

    meta = _regime_file(tmp_path).get("input_meta", {})
    assert meta.get("kospi_c5") == _pct_change(closes + [2100.0], 5), meta


# ── 배선 1: 급락 감지기 갱신 → 레짐 어댑터 set_intraday_risk ────────────────────

def test_intraday_risk_is_pushed_to_regime_adapter():
    """crash 전이 시 어댑터에 (level, change_pct, as_of) 를 전달한다."""
    bot = _make_bot(screener=_Screener(c5=0.0, c20=0.0))
    sched = object.__new__(KRScheduler)
    sched.bot = bot
    as_of = datetime(2026, 9, 14, 9, 5, 0)

    sched._push_intraday_risk("crash", -3.34, as_of)
    assert bot.engine._regime_adapter.calls == [("crash", -3.34, as_of)]

    # 결측 등락률은 0 이 아니라 None 으로 전달
    sched._push_intraday_risk("caution")
    assert bot.engine._regime_adapter.calls[-1] == ("caution", None, None)


def test_push_intraday_risk_is_silent_without_adapter():
    """어댑터·메서드가 없으면 조용히 건너뛴다 (급락 감지 루프를 막지 않는다)."""
    bot = _make_bot(screener=_Screener(c5=0.0, c20=0.0))
    bot.engine = SimpleNamespace()
    sched = object.__new__(KRScheduler)
    sched.bot = bot
    sched._push_intraday_risk("crash", -3.34)  # 예외 없이 통과

    bot.engine = SimpleNamespace(_regime_adapter=SimpleNamespace())
    sched._push_intraday_risk("crash", -3.34)


def test_cap_uses_shared_market_regime_function():
    """스케줄러 자체 캡 표 제거 — B 의 cap_regime_by_intraday_risk 를 쓴다."""
    from src.core.market_regime import cap_regime_by_intraday_risk

    assert not hasattr(KRScheduler, "_cap_by_intraday_crash")
    assert not hasattr(KRScheduler, "_CRASH_CAP")
    sched = object.__new__(KRScheduler)
    # 기존 의미 보존: crash/severe 에서만 trending_bull → neutral
    assert cap_regime_by_intraday_risk("trending_bull", "crash") == "neutral"
    assert cap_regime_by_intraday_risk("turning_point", "crash") == "turning_point"
    assert cap_regime_by_intraday_risk("trending_bull", "caution") == "trending_bull"
    assert sched._resolve_regime_conflict("neutral", "trending_bull", "crash") == "neutral"
    assert sched._resolve_regime_conflict("neutral", "trending_bull", None) == "trending_bull"
    assert sched._resolve_regime_conflict("bear", "trending_bull", None) == "neutral"


# ── 배선 3: US 지수는 C 의 indices_normalized 우선 ─────────────────────────────

_NORMALIZED = {
    "SP500": {"price": 6000.0, "change": -60.0, "change_pct": -1.0,
              "as_of": "2026-09-12T20:00:00+00:00", "fetched_at": "2026-09-14T07:01:00",
              "source": "yahoo_finance", "missing": False},
    "NASDAQ": {"price": 20000.0, "change": -250.0, "change_pct": -1.25,
               "as_of": "2026-09-12T20:00:00+00:00", "fetched_at": "2026-09-14T07:01:00",
               "source": "yahoo_finance", "missing": False},
    "SOX": {"price": 5500.0, "change": -180.0, "change_pct": -3.2,
            "as_of": "2026-09-12T20:00:00+00:00", "fetched_at": "2026-09-14T07:01:00",
            "source": "yahoo_finance", "missing": False},
    "VIX": {"price": 22.5, "change": 2.0, "change_pct": 9.8,
            "as_of": "2026-09-12T20:00:00+00:00", "fetched_at": "2026-09-14T07:01:00",
            "source": "yahoo_finance", "missing": False},
    "DOW": {"price": None, "change": None, "change_pct": None,
            "as_of": None, "fetched_at": None, "source": "yahoo_finance",
            "missing": True, "reason": "^DJI 조회 실패"},
}


def test_us_indices_read_from_normalized_keys(monkeypatch, tmp_path):
    """indices_normalized 의 SP500/SOX/VIX 가 실제 값으로 실리고 as_of 는 마감·조회 구분."""
    screener = _Screener(c5=0.5, c20=0.2)
    bot = _make_bot(screener=screener)
    llm = _LLM({"regime": "ranging", "confidence": 0.6})
    sched = _patch_env(monkeypatch, tmp_path, bot, llm,
                       indices=_PROVIDER_INDICES, normalized=_NORMALIZED,
                       now=datetime(2026, 9, 14, 8, 10, 0))

    asyncio.run(sched._run_llm_regime_classifier(label="08:10"))

    prompt = llm.prompts[0]
    assert "-1.00" in prompt, f"SP500 정규화 값 누락:\n{prompt}"
    assert "-3.20" in prompt, f"SOX 정규화 값 누락:\n{prompt}"
    assert "22.5" in prompt, f"VIX 레벨(price) 누락:\n{prompt}"
    saved = _regime_file(tmp_path)
    us_as_of = saved.get("input_meta", {}).get("us_as_of") or ""
    assert "마감" in us_as_of and "조회" in us_as_of, f"as_of/fetched_at 구분 표기 누락: {us_as_of}"
    assert "VIX" not in (saved.get("input_meta", {}).get("missing_fields") or [])


def test_normalized_missing_entry_is_not_zero(monkeypatch, tmp_path):
    """missing=True 항목은 0 이 아니라 결측으로 집계된다."""
    screener = _Screener(c5=0.5, c20=0.2)
    bot = _make_bot(screener=screener)
    llm = _LLM({"regime": "ranging", "confidence": 0.6})
    norm = dict(_NORMALIZED)
    norm["SOX"] = {"price": None, "change_pct": None, "as_of": None,
                   "fetched_at": None, "missing": True, "reason": "^SOX 조회 실패"}
    sched = _patch_env(monkeypatch, tmp_path, bot, llm,
                       indices={}, normalized=norm,
                       now=datetime(2026, 9, 14, 8, 10, 0))

    asyncio.run(sched._run_llm_regime_classifier(label="08:10"))

    missing = _regime_file(tmp_path).get("input_meta", {}).get("missing_fields") or []
    assert "SOX" in missing, f"결측 집계 누락: {missing}"
    assert "SOX: 0" not in llm.prompts[0]


# ── advisory (g): 캡 적용 시 원본 confidence 를 보존한다 ───────────────────────

def test_capped_regime_records_raw_confidence_and_flag(monkeypatch, tmp_path):
    screener = _Screener(c5=3.3, c20=1.4)
    bot = _make_bot(screener=screener, intraday_state="crash", intraday_pct=-3.34,
                    crash_level="crash")
    llm = _LLM({"regime": "trending_bull", "confidence": 0.85})
    sched = _patch_env(monkeypatch, tmp_path, bot, llm,
                       indices=_PROVIDER_INDICES,
                       now=datetime(2026, 9, 14, 12, 0, 0))

    asyncio.run(sched._run_llm_regime_classifier(label="12:00 (장중 업데이트)"))

    saved = _regime_file(tmp_path)
    assert saved.get("regime_capped") is True, saved
    assert saved.get("confidence_raw") == 0.85, saved
    assert saved.get("confidence") == 0.85, "적용 레짐 confidence 는 그대로 둔다"


# ── 배선 4: 07:30 전문가 브리핑 — 상충 문구 + 자료 부족 표시 ───────────────────

class _Orchestrator:
    def __init__(self, summary):
        self._summary = summary

    def data_status_summary(self, opinions=None):
        return dict(self._summary)


class _Notifier:
    def __init__(self):
        self.sent = []

    async def send_report(self, msg):
        self.sent.append(msg)
        return True

    async def send_message(self, msg):
        self.sent.append(msg)
        return True


def _run_briefing(monkeypatch, tmp_path, *, brief, summary, agg):
    import json as _json
    import src.utils.telegram as tg_mod

    import src.data.providers.disclosure_feed as disc_mod

    notifier = _Notifier()
    monkeypatch.setattr(tg_mod, "get_telegram_notifier", lambda: notifier)

    async def _no_disclosure(top_n=5, days=3):
        return ""

    monkeypatch.setattr(disc_mod, "fetch_disclosure_summary", _no_disclosure)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    cache = tmp_path / ".cache" / "ai_trader"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "llm_morning_brief.json").write_text(
        _json.dumps(brief, ensure_ascii=False), encoding="utf-8")

    sched = object.__new__(KRScheduler)
    sched.bot = SimpleNamespace(expert_orchestrator=_Orchestrator(summary))
    asyncio.run(sched._send_expert_briefing_telegram(
        "🌅 장전", {}, agg, "neutral", False, use_report_channel=True))
    assert notifier.sent, "브리핑이 전송되지 않았다"
    return notifier.sent[0]


def test_briefing_appends_expert_conflict_note(monkeypatch, tmp_path):
    """낙관 브리프 + 전문가 종합 +2(중립) → 상충 문구가 브리프 끝에 붙는다."""
    brief = {
        "text": "미국 증시 강세 마감. 반도체 랠리가 이어졌다.",
        "tone": "bull",
        "generated_at": datetime.now().isoformat(),
    }
    msg = _run_briefing(monkeypatch, tmp_path, brief=brief,
                        summary={"counts": {"ok": 6, "partial": 0, "insufficient": 0},
                                 "insufficient_experts": [], "note": None,
                                 "valid_n": 6, "insufficient_coverage": False},
                        agg=2)
    assert "상충" in msg, f"전문가 상충 문구 누락:\n{msg}"


def test_briefing_shows_data_status_note(monkeypatch, tmp_path):
    """자료 부족 N명·커버리지 부족이 전문가 종합 줄에 표시된다."""
    brief = {"text": "미국 증시 혼조 마감.", "tone": "neutral",
             "generated_at": datetime.now().isoformat()}
    msg = _run_briefing(monkeypatch, tmp_path, brief=brief,
                        summary={"counts": {"ok": 2, "partial": 1, "insufficient": 3},
                                 "insufficient_experts": ["macro_economist"],
                                 "note": "자료 부족 3명",
                                 "valid_n": 2, "insufficient_coverage": True},
                        agg=0)
    assert "자료 부족 3명" in msg, f"자료 부족 표시 누락:\n{msg}"
    assert "무보정" in msg, f"커버리지 부족 표시 누락:\n{msg}"


def test_intraday_recompute_skipped_when_last_bar_date_unknown(monkeypatch, tmp_path):
    """KIS 폴백 로드처럼 마지막 봉 날짜를 모르면(None) 당일 지수를 덧붙이지 않는다.

    2026-09-15 정정: 이전 판은 분기 조건식을 테스트 안에서 재현했을 뿐 실제 함수를
    부르지 않아, 구현이 바뀌어도 통과했다. 이제 분류기를 실행해 결과를 검증한다.
    """
    from src.schedulers.kr_scheduler import _pct_change

    closes = [2400.0 + i for i in range(30)]
    screener = _Screener(c5=0.0, c20=0.0, level=closes[-1], closes=closes)
    screener._kospi_last_bar_date = None   # _Screener 기본값(전 거래일) 대신 "날짜 미상"
    bot = _make_bot(
        screener=screener,
        kis_responses={"0001": {"price": 2100.0, "change_pct": -3.34},
                       "1001": {"change_pct": -2.78}},
    )
    llm = _LLM({"regime": "ranging", "confidence": 0.6})
    sched = _patch_env(monkeypatch, tmp_path, bot, llm,
                       indices=_PROVIDER_INDICES,
                       now=datetime(2026, 9, 14, 12, 0, 0))

    asyncio.run(sched._run_llm_regime_classifier(label="12:00 (장중 업데이트)"))

    meta = _regime_file(tmp_path).get("input_meta", {})
    assert meta.get("kospi_c5") is None, meta  # 로드 시각으로 봉 날짜를 대체하지 않는다.
    assert meta.get("kospi_c20") is None, meta
    assert any("미상" in m for m in (meta.get("missing_fields") or [])), meta
