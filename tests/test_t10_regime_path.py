"""T10 담당 A — 레짐 입력 갱신 → 유효 레짐 → 최종 소비자 경로 (F13/F14/F15/F19).

대상 경로
  F13 `KRScheduler._run_llm_regime_classifier` — 정오 재조회 시 당일 봉 교체/추가/보류
  F14 `market_regime.classify_intraday_level` + 급락 캡 병합 (이번 조회 실측 포함)
  F15 공통 유효 레짐 단일 적용 (ExitManager / G2 / 사이징)
  F19 07:30 발송 기록 배선 (C 의 `record_morning_brief_dispatch`)

실행: venv/bin/python -m pytest tests/test_t10_regime_path.py -q
네트워크·운영 캐시 무접촉 — Path.home()·공급자·LLM·시계는 전부 tmp_path/가짜 주입.
"""

import asyncio
import json
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core import market_regime as mr  # noqa: E402
from src.schedulers import kr_scheduler  # noqa: E402
from src.schedulers.kr_scheduler import KRScheduler  # noqa: E402

# 2026-09-14 = 월요일, 직전 거래일 = 2026-09-11(금). 추석(9/24~26) 과 무관.
NOON = datetime(2026, 9, 14, 12, 0, 0)
TODAY = NOON.date()
PREV_TRADING_DAY = date(2026, 9, 11)


# ── 가짜 협력자 ────────────────────────────────────────────────────────────────

class _Screener:
    def __init__(self, closes, last_bar_date, regime="neutral"):
        self._kospi_closes = list(closes)
        self._kospi_last_bar_date = last_bar_date
        self._kospi_loaded_at = datetime(2026, 9, 14, 8, 20, 0)
        self._regime = regime

    def get_kospi_change(self):
        return {"c5": 0.0, "c20": 0.0}

    def get_market_regime(self):
        return self._regime


class _BatchAnalyzer:
    def __init__(self, screener, intraday_state, intraday_pct, updated_at):
        self._screener = screener
        self._intraday_state = intraday_state
        self._intraday_kospi_pct = intraday_pct
        self._intraday_updated_at = updated_at


class _KisMarketData:
    def __init__(self, responses):
        self._responses = responses
        self.calls = []

    async def fetch_index_price(self, index_code="0001"):
        self.calls.append(index_code)
        return self._responses.get(index_code)


class _ExitManagerStub:
    def __init__(self):
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


class _AdapterStub:
    def __init__(self):
        self.calls = []

    def set_intraday_risk(self, level, change_pct=None, as_of=None):
        self.calls.append((level, change_pct, as_of))


_PROVIDER_INDICES = {
    "S&P500": {"price": 6000.0, "change": 30.0, "change_pct": 0.51},
    "NASDAQ": {"price": 20000.0, "change": 120.0, "change_pct": 0.60},
    "반도체(SOX)": {"price": 5500.0, "change": 90.0, "change_pct": 1.64},
}


def _make_bot(*, closes, last_bar_date, kis_responses,
              intraday_state="normal", intraday_pct=0.0,
              updated_at=datetime(2026, 9, 14, 9, 5, 0), screener_regime="neutral"):
    screener = _Screener(closes, last_bar_date, regime=screener_regime)
    ba = _BatchAnalyzer(screener, intraday_state, intraday_pct, updated_at)
    return SimpleNamespace(
        batch_analyzer=ba,
        kis_market_data=_KisMarketData(kis_responses),
        exit_manager=_ExitManagerStub(),
        engine=SimpleNamespace(_regime_adapter=_AdapterStub()),
        config={"kr": {"llm_ops": {"regime_conflict_guard_enabled": True}}},
    )


def _patch_env(monkeypatch, tmp_path, bot, llm, *, now=NOON):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    import src.utils.llm as llm_mod
    monkeypatch.setattr(llm_mod, "get_llm_manager", lambda: llm)

    import src.data.providers.us_market_data as umd_mod

    class _UMD:
        async def get_overnight_signal(self):
            return {"indices": dict(_PROVIDER_INDICES)}

    monkeypatch.setattr(umd_mod, "get_us_market_data", lambda: _UMD())
    monkeypatch.setattr(kr_scheduler, "_now_kst", lambda: now, raising=False)

    sched = object.__new__(KRScheduler)
    sched.bot = bot
    return sched


def _saved(tmp_path):
    path = tmp_path / ".cache" / "ai_trader" / "llm_regime_today.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


# ══════════════════════════════════════════════════════════════════════════════
# F13 — 정오 당일 봉 교체 / 추가 / 보류
# ══════════════════════════════════════════════════════════════════════════════

# 계획서 F13 필수 인수: 이전 29봉 100, 오전 당일 봉 104, 정오 가격 97
_BARS_29 = [100.0] * 29


def test_today_bar_is_replaced_not_kept(monkeypatch, tmp_path):
    """오전에 로드된 당일 봉(104)은 정오 가격(97)으로 **교체**된다 → c5/c20 = -3%."""
    bot = _make_bot(
        closes=_BARS_29 + [104.0], last_bar_date=TODAY,
        kis_responses={"0001": {"price": 97.0, "change_pct": -3.0},
                       "1001": {"change_pct": -2.5}},
    )
    llm = _LLM({"regime": "ranging", "confidence": 0.6})
    sched = _patch_env(monkeypatch, tmp_path, bot, llm)

    asyncio.run(sched._run_llm_regime_classifier(label="12:00 (장중 업데이트)"))

    meta = _saved(tmp_path).get("input_meta", {})
    assert meta.get("kospi_c5") == -3.0, f"오전 당일 봉이 그대로 남았다: {meta}"
    assert meta.get("kospi_c20") == -3.0, meta
    # 스크리너 메모리는 변형하지 않는다 (같은 날 재실행 멱등성의 근거)
    assert bot.batch_analyzer._screener._kospi_closes == _BARS_29 + [104.0]


def test_rerun_same_day_is_idempotent(monkeypatch, tmp_path):
    """같은 날 두 번 실행해도 당일 봉이 중복 누적되지 않는다."""
    bot = _make_bot(
        closes=_BARS_29 + [104.0], last_bar_date=TODAY,
        kis_responses={"0001": {"price": 97.0, "change_pct": -3.0},
                       "1001": {"change_pct": -2.5}},
    )
    llm = _LLM({"regime": "ranging", "confidence": 0.6})
    sched = _patch_env(monkeypatch, tmp_path, bot, llm)

    asyncio.run(sched._run_llm_regime_classifier(label="12:00 (장중 업데이트)"))
    first = _saved(tmp_path)["input_meta"]["kospi_c5"]
    asyncio.run(sched._run_llm_regime_classifier(label="12:00 (장중 업데이트)"))
    assert _saved(tmp_path)["input_meta"]["kospi_c5"] == first


def test_previous_trading_day_bar_is_appended(monkeypatch, tmp_path):
    """마지막 봉이 직전 거래일이면 당일 잠정 봉을 **추가**한다."""
    bot = _make_bot(
        closes=[100.0] * 30, last_bar_date=PREV_TRADING_DAY,
        kis_responses={"0001": {"price": 97.0, "change_pct": -3.0},
                       "1001": {"change_pct": -2.5}},
    )
    llm = _LLM({"regime": "ranging", "confidence": 0.6})
    sched = _patch_env(monkeypatch, tmp_path, bot, llm)

    asyncio.run(sched._run_llm_regime_classifier(label="12:00 (장중 업데이트)"))

    meta = _saved(tmp_path).get("input_meta", {})
    assert meta.get("kospi_c5") == -3.0, meta
    assert meta.get("kospi_c20") == -3.0, meta


def test_unknown_last_bar_date_is_not_disguised_as_fresh(monkeypatch, tmp_path):
    """마지막 봉 날짜 미상(KIS 폴백 로드) → 재계산 없음 + 결측 사유 기록."""
    bot = _make_bot(
        closes=[100.0] * 30, last_bar_date=None,
        kis_responses={"0001": {"price": 97.0, "change_pct": -3.0},
                       "1001": {"change_pct": -2.5}},
    )
    llm = _LLM({"regime": "ranging", "confidence": 0.6})
    sched = _patch_env(monkeypatch, tmp_path, bot, llm)

    asyncio.run(sched._run_llm_regime_classifier(label="12:00 (장중 업데이트)"))

    meta = _saved(tmp_path).get("input_meta", {})
    assert meta.get("kospi_c5") == 0.0, f"재계산이 일어났다: {meta}"   # [100]*30 → 0%
    assert any("봉" in m for m in meta.get("missing_fields", [])), meta
    assert "08:20" in (meta.get("kospi_bars_as_of") or ""), (
        f"봉 기준 as_of 가 최신으로 위장됐다: {meta.get('kospi_bars_as_of')}"
    )
    assert meta.get("kr_as_of") != meta.get("kospi_bars_as_of"), (
        "현재 지수 as_of 와 봉 기반 as_of 를 구분해야 한다"
    )


def test_missing_mid_trading_day_is_not_recomputed(monkeypatch, tmp_path):
    """마지막 봉이 직전 거래일보다 오래됨(중간 거래일 누락) → 재계산 없음 + 사유."""
    bot = _make_bot(
        closes=[100.0] * 30, last_bar_date=date(2026, 9, 8),
        kis_responses={"0001": {"price": 97.0, "change_pct": -3.0},
                       "1001": {"change_pct": -2.5}},
    )
    llm = _LLM({"regime": "ranging", "confidence": 0.6})
    sched = _patch_env(monkeypatch, tmp_path, bot, llm)

    asyncio.run(sched._run_llm_regime_classifier(label="12:00 (장중 업데이트)"))

    meta = _saved(tmp_path).get("input_meta", {})
    assert meta.get("kospi_c5") == 0.0, f"누락 구간을 무시하고 재계산했다: {meta}"
    assert any("누락" in m for m in meta.get("missing_fields", [])), meta


def test_index_fetch_failure_keeps_bar_as_of_and_marks_missing(monkeypatch, tmp_path):
    """당일 지수 조회 실패 → 봉 기준 as_of 유지 + KOSPI당일 결측."""
    bot = _make_bot(
        closes=[100.0] * 30, last_bar_date=PREV_TRADING_DAY,
        kis_responses={"0001": None, "1001": None},
    )
    llm = _LLM({"regime": "ranging", "confidence": 0.6})
    sched = _patch_env(monkeypatch, tmp_path, bot, llm)

    asyncio.run(sched._run_llm_regime_classifier(label="12:00 (장중 업데이트)"))

    meta = _saved(tmp_path).get("input_meta", {})
    assert "KOSPI당일" in meta.get("missing_fields", []), meta
    assert meta.get("kospi_today_pct") is None
    assert "08:20" in (meta.get("kospi_bars_as_of") or ""), meta


def test_prompt_shows_both_as_of(monkeypatch, tmp_path):
    """프롬프트에 현재 지수 as_of 와 봉 기반 as_of 가 모두 표기된다."""
    bot = _make_bot(
        closes=_BARS_29 + [104.0], last_bar_date=TODAY,
        kis_responses={"0001": {"price": 97.0, "change_pct": -3.0},
                       "1001": {"change_pct": -2.5}},
    )
    llm = _LLM({"regime": "ranging", "confidence": 0.6})
    sched = _patch_env(monkeypatch, tmp_path, bot, llm)

    asyncio.run(sched._run_llm_regime_classifier(label="12:00 (장중 업데이트)"))

    prompt = llm.prompts[0]
    assert "봉 기준" in prompt, f"봉 기반 as_of 표기 누락:\n{prompt}"
    assert "2026-09-14T12:00:00" in prompt, f"현재 지수 as_of 표기 누락:\n{prompt}"


# ══════════════════════════════════════════════════════════════════════════════
# F14 — 최신 실측과 최종 급락 캡 일치
# ══════════════════════════════════════════════════════════════════════════════

def test_classify_intraday_level_thresholds_unchanged():
    """임계값 -1.5/-2.5/-3.5 는 그대로. 결측은 None (0·normal 로 채우지 않는다)."""
    assert mr.classify_intraday_level(None) is None
    assert mr.classify_intraday_level(0.5) == "normal"
    assert mr.classify_intraday_level(-1.49) == "normal"
    assert mr.classify_intraday_level(-1.5) == "caution"
    assert mr.classify_intraday_level(-2.49) == "caution"
    assert mr.classify_intraday_level(-2.5) == "crash"
    assert mr.classify_intraday_level(-3.49) == "crash"
    assert mr.classify_intraday_level(-3.5) == "severe"


def test_batch_analyzer_uses_shared_classifier():
    """batch_analyzer.update_intraday_state 는 별도 구현이 아니라 공통 함수를 쓴다."""
    from src.core.batch_analyzer import BatchAnalyzer

    for pct in (0.5, -1.5, -2.5, -3.5, -2.49):
        ba = object.__new__(BatchAnalyzer)
        ba._intraday_state = "normal"
        ba._intraday_kospi_pct = 0.0
        ba._intraday_updated_at = None
        ba._exit_manager = None
        ba._intraday_recovery_until = None
        assert asyncio.run(ba.update_intraday_state(pct)) == mr.classify_intraday_level(pct)


def test_max_intraday_level_is_conservative():
    assert mr.max_intraday_level("normal", "crash") == "crash"
    assert mr.max_intraday_level("crash", "normal") == "crash"
    assert mr.max_intraday_level(None, "caution") == "caution"
    assert mr.max_intraday_level("severe", "crash") == "severe"
    assert mr.max_intraday_level(None, None) is None


def _run_crash_cap_case(monkeypatch, tmp_path, *, kospi_pct, detector_state,
                        detector_updated_at=datetime(2026, 9, 14, 9, 5, 0)):
    bot = _make_bot(
        closes=[100.0] * 30, last_bar_date=PREV_TRADING_DAY,
        kis_responses={"0001": {"price": 97.0, "change_pct": kospi_pct},
                       "1001": {"change_pct": -2.5}},
        intraday_state=detector_state, intraday_pct=None,
        updated_at=detector_updated_at,
    )
    llm = _LLM({"regime": "trending_bull", "confidence": 0.85})
    sched = _patch_env(monkeypatch, tmp_path, bot, llm)
    asyncio.run(sched._run_llm_regime_classifier(label="12:00 (장중 업데이트)"))
    return bot, _saved(tmp_path)


def test_fresh_crash_observation_caps_even_when_detector_normal(monkeypatch, tmp_path):
    """이번 조회 -3.0% + 감지기 normal(당일) + LLM trending_bull → neutral 로 제한."""
    bot, saved = _run_crash_cap_case(monkeypatch, tmp_path,
                                     kospi_pct=-3.0, detector_state="normal")
    assert saved.get("regime") == "neutral", f"최신 급락이 캡에 반영되지 않았다: {saved}"
    assert saved.get("llm_regime_raw") == "trending_bull", saved
    assert saved["input_meta"].get("intraday_level_observed") == "crash", saved["input_meta"]

    # ExitManager 에 적용되는 값도 trending_bull 이 아니다
    sched = object.__new__(KRScheduler)
    sched.bot = bot
    asyncio.run(sched._apply_regime_to_exit_manager())
    assert bot.exit_manager.applied[-1] != "trending_bull", bot.exit_manager.applied


def test_fresh_crash_observation_caps_when_detector_never_ran(monkeypatch, tmp_path):
    """감지기 초기 None(당일 갱신 없음)이어도 이번 조회 -3.0% 로 캡이 걸린다."""
    _bot, saved = _run_crash_cap_case(monkeypatch, tmp_path,
                                      kospi_pct=-3.0, detector_state=None,
                                      detector_updated_at=None)
    assert saved.get("regime") == "neutral", saved


def test_normal_observation_does_not_relax_detector_crash(monkeypatch, tmp_path):
    """분류기는 감지기를 완화 방향으로 덮어쓰지 않는다 (감지기 crash 유지)."""
    _bot, saved = _run_crash_cap_case(monkeypatch, tmp_path,
                                      kospi_pct=-0.4, detector_state="crash")
    assert saved.get("regime") == "neutral", saved
    assert saved["input_meta"].get("intraday_cap_level") == "crash", saved["input_meta"]


def test_previous_day_crash_and_no_observation_is_missing(monkeypatch, tmp_path):
    """전일 crash 잔존 + 이번 조회 실패 → 캡 근거 없음 (결측 명시)."""
    bot = _make_bot(
        closes=[100.0] * 30, last_bar_date=PREV_TRADING_DAY,
        kis_responses={"0001": None, "1001": None},
        intraday_state="crash", intraday_pct=-3.26,
        updated_at=datetime(2026, 9, 13, 15, 34, 0),
    )
    llm = _LLM({"regime": "trending_bull", "confidence": 0.85})
    sched = _patch_env(monkeypatch, tmp_path, bot, llm)
    asyncio.run(sched._run_llm_regime_classifier(label="12:00 (장중 업데이트)"))

    saved = _saved(tmp_path)
    assert saved.get("regime") == "trending_bull", f"결측을 캡 근거로 썼다: {saved}"
    meta = saved["input_meta"]
    assert meta.get("intraday_cap_level") is None, meta
    assert "KOSPI당일" in meta.get("missing_fields", []), meta


def test_observation_is_pushed_to_regime_adapter(monkeypatch, tmp_path):
    """이번 관측을 어댑터에도 전달한다 (as_of = 조회 시각, 값은 병합 결과)."""
    bot, _saved_data = _run_crash_cap_case(monkeypatch, tmp_path,
                                           kospi_pct=-3.0, detector_state="normal")
    calls = bot.engine._regime_adapter.calls
    assert calls, "어댑터에 장중 위험을 전달하지 않았다"
    assert calls[-1][0] == "crash" and calls[-1][1] == -3.0 and calls[-1][2] == NOON

    # 감지기 crash + 이번 조회 normal → 어댑터에도 완화 방향으로 내려보내지 않는다
    bot2, _ = _run_crash_cap_case(monkeypatch, tmp_path,
                                  kospi_pct=-0.4, detector_state="crash")
    assert bot2.engine._regime_adapter.calls[-1][0] == "crash"


def test_no_observation_does_not_restamp_detector_state(monkeypatch, tmp_path):
    """이번 조회가 실패하면 감지기 값을 now 로 재각인하지 않는다."""
    bot = _make_bot(
        closes=[100.0] * 30, last_bar_date=PREV_TRADING_DAY,
        kis_responses={"0001": None, "1001": None},
        intraday_state="crash", intraday_pct=-3.0,
    )
    llm = _LLM({"regime": "ranging", "confidence": 0.6})
    sched = _patch_env(monkeypatch, tmp_path, bot, llm)
    asyncio.run(sched._run_llm_regime_classifier(label="12:00 (장중 업데이트)"))
    assert bot.engine._regime_adapter.calls == []


def test_set_intraday_risk_rejects_out_of_order_as_of():
    """늦게 도착한 과거 normal 이 최신 crash 를 덮지 않는다."""
    adapter = mr.MarketRegimeAdapter()
    adapter._current_regime = "bull"
    adapter.set_intraday_risk("crash", -3.0, datetime(2026, 9, 14, 12, 0))
    adapter.set_intraday_risk("normal", -0.2, datetime(2026, 9, 14, 11, 30))
    assert adapter.horizons.intraday_risk == "crash"


def test_set_intraday_risk_without_as_of_is_not_treated_as_latest():
    """as_of 없는 값은 최신으로 취급하지 않는다 (기존 관측을 덮지 않음)."""
    adapter = mr.MarketRegimeAdapter()
    adapter.set_intraday_risk("crash", -3.0, datetime(2026, 9, 14, 12, 0))
    adapter.set_intraday_risk("normal", -0.2, None)
    assert adapter.horizons.intraday_risk == "crash"


def test_apply_regime_to_exit_manager_merges_file_observation(monkeypatch, tmp_path):
    """30분 sync 도 같은 병합 규칙 — 파일의 kospi_today_pct 가 캡 근거가 된다."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(kr_scheduler, "_now_kst", lambda: NOON, raising=False)
    cache = tmp_path / ".cache" / "ai_trader"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "llm_regime_today.json").write_text(json.dumps({
        "regime": "trending_bull",
        "date": TODAY.isoformat(),
        "input_meta": {"kospi_today_pct": -3.0},
    }), encoding="utf-8")

    bot = _make_bot(closes=[100.0] * 30, last_bar_date=PREV_TRADING_DAY,
                    kis_responses={}, intraday_state="normal")
    sched = object.__new__(KRScheduler)
    sched.bot = bot
    asyncio.run(sched._apply_regime_to_exit_manager())

    assert bot.exit_manager.applied == ["neutral"], bot.exit_manager.applied


def test_apply_regime_uses_now_kst_for_date_gate(monkeypatch, tmp_path):
    """파일 날짜 비교도 _now_kst 기준 (date.today() 와 이중 시계 금지)."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(kr_scheduler, "_now_kst", lambda: NOON, raising=False)
    cache = tmp_path / ".cache" / "ai_trader"
    cache.mkdir(parents=True, exist_ok=True)
    if date.today() == TODAY:
        pytest.skip("실제 오늘이 고정 시계와 같으면 두 시계를 구분할 수 없다")
    (cache / "llm_regime_today.json").write_text(json.dumps({
        "regime": "trending_bull", "date": date.today().isoformat(),
    }), encoding="utf-8")

    bot = _make_bot(closes=[100.0] * 30, last_bar_date=PREV_TRADING_DAY,
                    kis_responses={}, intraday_state="normal")
    sched = object.__new__(KRScheduler)
    sched.bot = bot
    asyncio.run(sched._apply_regime_to_exit_manager())
    # 실제 오늘 ≠ 2026-09-14 이므로 적용되지 않아야 한다
    assert bot.exit_manager.applied == [], bot.exit_manager.applied


# ══════════════════════════════════════════════════════════════════════════════
# F15 — 모든 최종 소비자에 유효 레짐 적용
# ══════════════════════════════════════════════════════════════════════════════

def _real_exit_manager(tmp_path, monkeypatch):
    """실제 ExitManager — stage 파일은 tmp_path 아래로만 쓴다."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    from src.core.types import Position, StrategyType
    from src.strategies.exit_manager import ExitManager

    em = ExitManager()
    pos = Position(
        symbol="005930", quantity=10,
        avg_price=Decimal("70000"), current_price=Decimal("70000"),
        strategy=StrategyType.SEPA_TREND,
    )
    em.register_position(pos)
    return em


def test_monitor_positions_does_not_reapply_raw_cached_regime(monkeypatch, tmp_path):
    """아침 trending_bull 캐시가 crash 보정 뒤(neutral) monitor 에서 되살아나지 않는다."""
    from src.core.batch_analyzer import BatchAnalyzer

    em = _real_exit_manager(tmp_path, monkeypatch)
    cache = tmp_path / ".cache" / "ai_trader"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "llm_regime_today.json").write_text(json.dumps({
        "regime": "trending_bull", "date": date.today().isoformat(),
    }), encoding="utf-8")

    # 08:10 아침 캐시 적용(trending_bull, stale 7d) → 장중 crash 로 SL/TS 조임 →
    # 30분 sync 가 캡을 적용해 neutral(stale 5d) 로 낮춘 상태
    em.apply_regime_params("trending_bull")
    assert em._states["005930"].stale_high_days == 7
    em.apply_intraday_crash_params("crash")
    em.apply_regime_params("neutral")
    state = em._states["005930"]
    assert state.stale_high_days == 5 and state.stop_loss_pct == 2.5

    ba = object.__new__(BatchAnalyzer)
    ba._exit_manager = em
    ba._engine = SimpleNamespace(portfolio=SimpleNamespace(positions={"005930": object()}))
    ba._broker = SimpleNamespace(get_quote=_none_quote)
    ba._composite_cache_date = date.today()
    ba._ma5_cache = {}
    ba._prev_low_cache = {}
    ba._config = {}                 # 코어홀딩 조기경보 비활성

    asyncio.run(ba.monitor_positions())

    assert state.stale_high_days == 5, "캐시 원본 trending_bull 이 재적용됐다 (stale 7d 복귀)"
    assert state.first_exit_pct == 10.0
    assert state.stop_loss_pct == 2.5, "급락 SL 조임이 풀렸다"
    assert state.trailing_stop_pct == 2.0, "급락 TS 조임이 풀렸다"


async def _none_quote(symbol):
    return None


def test_g2_gate_reads_effective_regime_from_adapter():
    """어댑터가 crash 로 sideways 인데 2분 복사본이 bull 이면 게이트는 sideways 를 본다."""
    from src.core.engine import RiskManager

    adapter = mr.MarketRegimeAdapter()
    adapter._current_regime = "bull"
    adapter.set_intraday_risk("crash", -3.0, datetime.now())
    assert adapter.regime == "sideways"

    captured = {}

    class _CV:
        last_memory_adj = 0

        def validate(self, **kwargs):
            captured.update(kwargs)
            return False, kwargs["score"], "테스트 차단"

    rm = object.__new__(RiskManager)
    rm.engine = SimpleNamespace(_regime_adapter=adapter, _market_regime="bull")
    assert RiskManager._resolve_market_regime(rm) == "sideways"

    # 복사본만 있으면 폴백
    rm2 = object.__new__(RiskManager)
    rm2.engine = SimpleNamespace(_market_regime="bull")
    assert RiskManager._resolve_market_regime(rm2) == "bull"


def test_sizing_params_follow_effective_regime():
    """사이징(adapter.get_params)은 이미 유효 레짐 기준이다."""
    adapter = mr.MarketRegimeAdapter()
    adapter._current_regime = "bull"
    adapter.set_intraday_risk("crash", -3.0, datetime.now())
    assert adapter.get_params()["base_position_pct"] == \
        mr.MarketRegimeAdapter.REGIME_PARAMS["sideways"]["base_position_pct"]


# ══════════════════════════════════════════════════════════════════════════════
# F19 — 07:30 발송 기록 배선 (C→A 계약)
# ══════════════════════════════════════════════════════════════════════════════

class _Notifier:
    def __init__(self, ok=True):
        self.ok = ok
        self.sent = []

    async def send_report(self, msg):
        self.sent.append(msg)
        return self.ok

    async def send_message(self, msg):
        self.sent.append(msg)
        return self.ok


def _run_morning_briefing(monkeypatch, tmp_path, *, recorder=None, ok=True):
    import src.utils.telegram as tg_mod
    import src.data.providers.disclosure_feed as disc_mod
    import src.analytics.daily_report as dr_mod

    notifier = _Notifier(ok=ok)
    monkeypatch.setattr(tg_mod, "get_telegram_notifier", lambda: notifier)

    async def _no_disclosure(top_n=5, days=3):
        return ""

    monkeypatch.setattr(disc_mod, "fetch_disclosure_summary", _no_disclosure)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(kr_scheduler, "_now_kst",
                        lambda: datetime(2026, 9, 14, 7, 30), raising=False)
    if recorder is None:
        monkeypatch.delattr(dr_mod, "record_morning_brief_dispatch", raising=False)
    else:
        monkeypatch.setattr(dr_mod, "record_morning_brief_dispatch", recorder,
                            raising=False)

    sched = object.__new__(KRScheduler)
    sched.bot = SimpleNamespace(expert_orchestrator=None)
    asyncio.run(sched._send_expert_briefing_telegram(
        "🌅 장전", {}, 7, "neutral", False, use_report_channel=True))
    return notifier


def test_dispatch_is_recorded_with_expert_consensus(monkeypatch, tmp_path):
    calls = []
    notifier = _run_morning_briefing(
        monkeypatch, tmp_path, recorder=lambda **kw: calls.append(kw))

    assert notifier.sent, "브리핑이 전송되지 않았다"
    assert len(calls) == 1, calls
    kw = calls[0]
    assert kw["kr_date"] == "2026-09-14"
    assert kw["sent_text"] == notifier.sent[0]
    assert kw["expert_consensus"] == {"score": 7, "bias": "neutral", "valid_n": 0}
    assert kw["status"] == "sent"
    assert "conflict_note" in kw


def test_dispatch_record_marks_failed_send(monkeypatch, tmp_path):
    calls = []
    _run_morning_briefing(monkeypatch, tmp_path,
                          recorder=lambda **kw: calls.append(kw), ok=False)
    assert calls and calls[0]["status"] == "failed", calls


def test_dispatch_record_absent_is_skipped(monkeypatch, tmp_path):
    notifier = _run_morning_briefing(monkeypatch, tmp_path, recorder=None)
    assert notifier.sent, "기록 함수가 없다고 발송이 막히면 안 된다"


def test_dispatch_record_exception_is_isolated(monkeypatch, tmp_path):
    def _boom(**kw):
        raise RuntimeError("원장 쓰기 실패")

    notifier = _run_morning_briefing(monkeypatch, tmp_path, recorder=_boom)
    assert notifier.sent, "기록 실패가 발송 경로를 깨뜨렸다"
