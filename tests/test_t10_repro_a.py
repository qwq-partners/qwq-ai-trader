"""T10 담당 D — 독립 재현 테스트 A (F13~F15, 레짐 경로)

구현자(A)와 무관하게 기준 SHA(a59e29f)의 실제 코드를 기존 공개 진입점으로만
호출해 리뷰 주장을 재현한다. 아래 각 테스트는 기준 SHA에서 실패해야 하고
(결함 재현), T10 수정 통합 후에는 통과해야 한다.

대상 진입점:
  F13 — KRScheduler._run_llm_regime_classifier (12:00 장중 재분류의 c5/c20 재계산)
  F14 — 〃 (급락 캡이 감지기 상태만 보고 이번 조회의 신선한 등락률을 무시)
       + KRScheduler._apply_regime_to_exit_manager (같은 결함이 충돌 가드에도 있음)
  F15 — ExitManager.apply_regime_params / apply_intraday_crash_params
       + BatchAnalyzer.monitor_positions (레짐 동기화가 stale_high_days를 되돌림)
       + G2: RiskManager.on_signal (engine._market_regime 2분 지연 복사본 사용)

실행: /home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest tests/test_t10_repro_a.py -q
네트워크·KIS/LLM 자격증명 무접촉 — 공급자·LLM·캐시 경로는 전부 가짜/tmp_path.
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

from src.schedulers import kr_scheduler  # noqa: E402
from src.schedulers.kr_scheduler import KRScheduler, _pct_change  # noqa: E402


# ══════════════════════════════════════════════════════════════════════════
# F13 / F14 공용 가짜 협력자 (test_regime_llm_inputs.py 패턴 참고, 파일은 독립)
# ══════════════════════════════════════════════════════════════════════════

_UNSET = object()


class _Screener:
    def __init__(self, closes, last_bar_date, loaded_at=None, regime="bull"):
        self._kospi_closes = list(closes)
        self._kospi_last_bar_date = last_bar_date
        self._kospi_loaded_at = loaded_at or datetime(2026, 9, 14, 8, 20, 0)
        self._regime = regime

    def get_kospi_change(self):
        # 사용되지 않아야 한다 (0.0 위장 방지) — 호출되면 스크리너 원본 종가열 사용 위반
        return {"c5": 0.0, "c20": 0.0, "level": 0.0}

    def get_market_regime(self):
        return self._regime


class _BatchAnalyzer:
    def __init__(self, screener, intraday_state=None, intraday_pct=None, updated_at=None):
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
        self._intraday_crash_level = "normal"
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


def _real_adapter():
    from src.core.market_regime import MarketRegimeAdapter
    return MarketRegimeAdapter()


def _make_bot(*, screener, kis_responses=None, intraday_state=None,
              intraday_pct=None, updated_at=None):
    ba = _BatchAnalyzer(screener, intraday_state, intraday_pct, updated_at)
    return SimpleNamespace(
        batch_analyzer=ba,
        kis_market_data=_KisMarketData(kis_responses or {}),
        exit_manager=_ExitManagerStub(),
        # 실제 어댑터 — 5분 감지기·12:00 분류기·30분 sync 가 공유하는 장중 위험 상태.
        # 가짜(no-op)로 두면 "분류기가 어댑터에 밀어 넣은 crash" 라는 공통 근거가 사라져
        # F14 의 두 번째 단계(input_meta 없는 캐시 + 감지기 normal)를 재현할 수 없다
        # (2026-09-15 통합 조정 — 관찰 대상은 ExitManager 가 받은 레짐 그대로).
        engine=SimpleNamespace(_regime_adapter=_real_adapter()),
        config={"kr": {"llm_ops": {"regime_conflict_guard_enabled": True}}},
    )


def _patch_env(monkeypatch, tmp_path, bot, llm, *, now):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    import src.utils.llm as llm_mod
    monkeypatch.setattr(llm_mod, "get_llm_manager", lambda: llm)

    import src.data.providers.us_market_data as umd_mod

    class _UMD:
        async def get_overnight_signal(self):
            # 미국 지수는 이번 재현 대상이 아니다 — 전부 결측으로 둔다
            return {"indices": {}}

    monkeypatch.setattr(umd_mod, "get_us_market_data", lambda: _UMD())
    monkeypatch.setattr(kr_scheduler, "_now_kst", lambda: now, raising=False)

    sched = object.__new__(KRScheduler)
    sched.bot = bot
    return sched


def _regime_file(tmp_path):
    path = tmp_path / ".cache" / "ai_trader" / "llm_regime_today.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


# ══════════════════════════════════════════════════════════════════════════
# F13 — 정오 재조회가 오늘 봉이 있으면 c5/c20 재계산을 통째로 생략한다
#       (기대: 생략이 아니라 오늘 봉을 최신값으로 "교체"해 재계산)
# ══════════════════════════════════════════════════════════════════════════

def test_f13_intraday_recompute_replaces_todays_bar_not_skips(monkeypatch, tmp_path):
    """이전 29봉=100, 오전 당일 봉=104(이미 오늘 날짜로 로드됨), 정오 재조회=97.

    현재(버그): _has_today_bar=True → c5/c20 재계산 자체를 생략 → 104 기준 +4%가 그대로 남는다.
    기대: 오늘 봉을 97로 교체해 재계산 → c5 == -3.0% (kr_as_of만 갱신하고 수치는
    구식으로 남기는 "위장 최신화"를 금지한다).
    """
    prev = [100.0] * 29
    closes_with_stale_today = prev + [104.0]
    screener = _Screener(closes_with_stale_today, last_bar_date=date(2026, 9, 14))
    bot = _make_bot(
        screener=screener,
        kis_responses={
            "0001": {"price": 97.0, "change_pct": -3.0},
            "1001": {"change_pct": -2.0},
        },
    )
    llm = _LLM({"regime": "ranging", "confidence": 0.6})
    sched = _patch_env(monkeypatch, tmp_path, bot, llm, now=datetime(2026, 9, 14, 12, 0, 0))

    asyncio.run(sched._run_llm_regime_classifier(label="12:00 (장중 업데이트)"))

    expected_c5 = _pct_change(prev + [97.0], 5)
    assert expected_c5 == pytest.approx(-3.0)

    meta = _regime_file(tmp_path).get("input_meta", {})
    assert meta.get("kospi_c5") == pytest.approx(expected_c5), (
        f"당일 봉을 최신 정오 가격(97)으로 교체하지 않고 아침 값(104, +4%)을 그대로 "
        f"프롬프트에 실었다: {meta}"
    )


# ══════════════════════════════════════════════════════════════════════════
# F14 — 급락 캡이 "이번 조회"의 신선한 등락률이 아니라 감지기의 지난 상태만 본다
# ══════════════════════════════════════════════════════════════════════════

def test_f14_cap_ignores_fresh_fetch_when_detector_lags(monkeypatch, tmp_path):
    """감지기는 아직 normal(5분 루프 미도달)인데, 이번 호출 자체의 KIS 재조회는
    이미 KOSPI -3.0%를 반환한다. LLM이 trending_bull을 줘도 급락 중이므로
    캡이 걸려야 한다(감지기가 아니라 이번 조회값 기준).
    """
    screener = _Screener([100.0] * 30, last_bar_date=date(2026, 9, 10))  # 오래된 봉 → 재계산 안 함
    bot = _make_bot(
        screener=screener,
        kis_responses={
            "0001": {"price": 97.0, "change_pct": -3.0},
            "1001": {"change_pct": -2.5},
        },
        intraday_state="normal",       # 감지기는 아직 급락을 못 봤다
        intraday_pct=0.0,
        updated_at=datetime(2026, 9, 14, 9, 5, 0),
    )
    llm = _LLM({"regime": "trending_bull", "confidence": 0.85})
    sched = _patch_env(monkeypatch, tmp_path, bot, llm, now=datetime(2026, 9, 14, 12, 0, 0))

    asyncio.run(sched._run_llm_regime_classifier(label="12:00 (장중 업데이트)"))

    saved = _regime_file(tmp_path)
    assert saved.get("regime") != "trending_bull", (
        f"감지기가 normal 이라는 이유로 이번 조회의 KOSPI -3.0% 급락을 무시하고 "
        f"trending_bull 을 그대로 저장했다: {saved}"
    )

    # 충돌 방지 장치(_apply_regime_to_exit_manager)도 같은 구멍을 공유한다 —
    # 파일에 이미 trending_bull 이 저장된 상태(구식 캐시 재현)로 별도 확인.
    cache = tmp_path / ".cache" / "ai_trader"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "llm_regime_today.json").write_text(
        json.dumps({"regime": "trending_bull", "date": date(2026, 9, 14).isoformat()}),
        encoding="utf-8",
    )
    asyncio.run(sched._apply_regime_to_exit_manager())
    assert bot.exit_manager.applied, "ExitManager 갱신이 호출되지 않았다"
    assert bot.exit_manager.applied[-1] != "trending_bull", (
        f"충돌 방지 장치도 감지기(normal)만 보고 trending_bull 을 그대로 적용했다: "
        f"{bot.exit_manager.applied}"
    )


def test_f14_cap_ignores_fresh_fetch_when_detector_none(monkeypatch, tmp_path):
    """감지기 상태가 아예 없음(None, 당일 갱신 기록 자체가 없음)이어도 동일하게
    이번 조회의 -3.0% 급락으로 캡이 걸려야 한다."""
    screener = _Screener([100.0] * 30, last_bar_date=date(2026, 9, 10))
    bot = _make_bot(
        screener=screener,
        kis_responses={
            "0001": {"price": 97.0, "change_pct": -3.0},
            "1001": {"change_pct": -2.5},
        },
        intraday_state=None,
        updated_at=None,
    )
    llm = _LLM({"regime": "trending_bull", "confidence": 0.85})
    sched = _patch_env(monkeypatch, tmp_path, bot, llm, now=datetime(2026, 9, 14, 12, 0, 0))

    asyncio.run(sched._run_llm_regime_classifier(label="12:00 (장중 업데이트)"))

    saved = _regime_file(tmp_path)
    assert saved.get("regime") != "trending_bull", (
        f"급락감지기 상태가 결측(None)이라는 이유로 이번 조회의 -3.0% 급락을 무시했다: {saved}"
    )


# ══════════════════════════════════════════════════════════════════════════
# F15 — 30분 sync(monitor_positions)가 구식 캐시 레짐을 그대로 재적용해
#       장중급락으로 조여둔 stale_high_days를 되돌린다
# ══════════════════════════════════════════════════════════════════════════

def _make_exit_manager(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    from src.strategies.exit_manager import ExitManager, ExitConfig
    return ExitManager(config=ExitConfig(), market="KR")


def _register(em, symbol="005930"):
    from src.core.types import Position, PositionSide
    pos = Position(
        symbol=symbol, name="테스트", side=PositionSide.LONG,
        quantity=10, avg_price=Decimal("10000"), current_price=Decimal("10000"),
        strategy="sepa_trend",
    )
    em.register_position(pos)
    return pos


def test_f15_monitor_positions_reverts_stale_high_days_after_crash(monkeypatch, tmp_path):
    """아침 trending_bull(stale=7) → 장중 crash(SL/TS 조임) → 30분 sync 로 neutral(stale=5)
    → 그런데 캐시 파일은 여전히 아침의 trending_bull → monitor_positions 이 이를 그대로
    재적용하면 stale_high_days 가 7로 복귀한다(원치 않음).
    """
    em = _make_exit_manager(tmp_path, monkeypatch)
    pos = _register(em)
    symbol = pos.symbol

    em.apply_regime_params("trending_bull")
    assert em._states[symbol].stale_high_days == 7

    em.apply_intraday_crash_params("crash")

    em.apply_regime_params("neutral")
    assert em._states[symbol].stale_high_days == 5
    # crash 재적용으로 SL/TS 는 이미 조여져 있어야 한다
    assert em._states[symbol].stop_loss_pct <= 2.5
    assert em._states[symbol].trailing_stop_pct <= 2.0

    # 구식 캐시(아침 trending_bull) 그대로 남아 있는 상태 재현
    cache = tmp_path / ".cache" / "ai_trader"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "llm_regime_today.json").write_text(
        json.dumps({"regime": "trending_bull", "date": date.today().isoformat()}),
        encoding="utf-8",
    )

    from src.core.batch_analyzer import BatchAnalyzer

    class _Broker:
        async def get_quote(self, symbol):
            return None  # 이번 재현은 regime 동기화 구간만 검증 — 포지션 갱신은 스킵

    ba = object.__new__(BatchAnalyzer)
    ba._engine = SimpleNamespace(portfolio=SimpleNamespace(positions={symbol: pos}))
    ba._broker = _Broker()
    ba._exit_manager = em
    ba._composite_cache_date = date.today()  # _refresh_composite_cache 조기 반환(네트워크 회피)
    ba._config = {}  # _monitor_core_positions 가 참조 (코어홀딩 조기경보, 기본값으로 스킵)

    asyncio.run(ba.monitor_positions())

    assert em._states[symbol].stale_high_days == 5, (
        f"monitor_positions 이 구식 캐시(trending_bull)를 그대로 재적용해 "
        f"stale_high_days 가 되돌아갔다 (7 복귀 금지): {em._states[symbol].stale_high_days}"
    )
    assert em._states[symbol].stop_loss_pct <= 2.5, "장중급락 SL 조임이 풀렸다"
    assert em._states[symbol].trailing_stop_pct <= 2.0, "장중급락 TS 조임이 풀렸다"


# ══════════════════════════════════════════════════════════════════════════
# G2 — RiskManager.on_signal 이 engine._regime_adapter.regime(실시간) 대신
#      2분 주기로만 갱신되는 engine._market_regime 복사본을 읽는다
# ══════════════════════════════════════════════════════════════════════════

def test_g2_on_signal_uses_live_adapter_regime_not_stale_copy(monkeypatch):
    """어댑터는 이미 sideways(crash 반영, 실시간)인데 2분 주기 복사본은 아직 bull.
    G2 크로스 검증에 전달되는 market_regime 은 어댑터의 실시간 값이어야 한다."""
    from src.core.engine import RiskManager
    from src.core.event import SignalEvent
    from src.core.types import OrderSide, StrategyType

    captured = {}

    class _CrossValidator:
        def validate(self, **kwargs):
            captured.update(kwargs)
            return False, 0.0, "테스트 차단(캡처 전용)"

    rm = object.__new__(RiskManager)
    rm._order_fail_cooldown = {}
    rm._COOLDOWN_SECONDS = 60
    rm._last_signal_time = {}
    rm._SIGNAL_COOLDOWN_SECONDS = 30
    rm._pending_timestamps = {}
    rm._pending_sides = {}
    rm._pending_fallback_count = {}
    rm._pending_quantities = {}
    rm._PENDING_TIMEOUT_SECONDS = 600
    rm._pending_lock = asyncio.Lock()
    rm._pending_orders = set()
    rm._log_sig = lambda *a, **k: None
    rm._cross_validator = _CrossValidator()

    rm.engine = SimpleNamespace(
        broker=None,
        portfolio=SimpleNamespace(positions={}),
        _market_regime="bull",  # 2분 주기 복사본 — 아직 갱신 전(구식)
        _regime_adapter=SimpleNamespace(regime="sideways", effective_regime="sideways"),
        is_trading_hours=lambda: True,
    )

    event = SignalEvent(
        symbol="005930", side=OrderSide.BUY, strategy=StrategyType.SEPA_TREND,
        score=80.0, price=Decimal("1000"), signal=None, metadata={},
    )

    asyncio.run(rm.on_signal(event))

    assert captured.get("market_regime") == "sideways", (
        f"G2 가 어댑터의 실시간 유효 레짐(sideways) 대신 2분 지연 복사본"
        f"(bull)을 크로스 검증에 넘겼다: {captured.get('market_regime')!r}"
    )
