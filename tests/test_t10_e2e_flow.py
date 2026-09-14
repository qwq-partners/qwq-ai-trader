"""T10 담당 D — 통합 E2E 흐름 (07:00 → 07:30 → 12:00 → 20:30)

통합 SHA a6d81d0 의 실제 공개 진입점만으로 하루 사이클을 고정 시계·메모리
공급자로 연결한다. 내부 헬퍼 단정 없이 각 슬롯의 실제 함수를 순서대로 호출해
"각 단계는 고쳤으나 단계 사이 연결이 끊긴" 결함(F13~F22)이 통합 후 실제로
이어지는지 확인한다.

진입점:
  07:00 — DailyReportGenerator.generate_us_market_report
  07:30 — KRScheduler._send_expert_briefing_telegram(record_dispatch=True)
  12:00 — KRScheduler._run_llm_regime_classifier + _apply_regime_to_exit_manager
         + BatchAnalyzer.monitor_positions (30분 sync)
  20:30 — KRScheduler._run_morning_brief_evaluation / evaluate_morning_brief

실행: /home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest tests/test_t10_e2e_flow.py -q
네트워크·KIS/LLM 자격증명 무접촉 — 모든 협력자는 가짜/tmp_path (conftest 가 강제).
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

import src.analytics.daily_report as dr_mod  # noqa: E402
from src.analytics import morning_brief_eval  # noqa: E402
from src.schedulers import kr_scheduler  # noqa: E402
from src.schedulers.kr_scheduler import KRScheduler  # noqa: E402
from src.core.market_regime import (  # noqa: E402
    MarketRegimeAdapter, classify_intraday_level, max_intraday_level,
)

_KR_DATE = date(2026, 9, 14)


# ══════════════════════════════════════════════════════════════════════════
# 공용 가짜 협력자 (D 재현 파일들과 동일 패턴)
# ══════════════════════════════════════════════════════════════════════════

class _FakeLLMResult:
    def __init__(self, content, model="test-model", success=True):
        self.content = content
        self.model = model
        self.success = success


class _FakeLLM:
    """complete()(모닝브리프) + complete_json()(레짐 분류기) 둘 다 대역"""

    def __init__(self, complete_content=None, json_result=None):
        self._content = complete_content
        self._json_result = json_result
        self.json_prompts = []

    async def complete(self, **kwargs):
        return _FakeLLMResult(self._content)

    async def complete_json(self, prompt, system=None, task=None, **kwargs):
        self.json_prompts.append(prompt)
        return dict(self._json_result)


class _FakeKMD:
    """DailyReportGenerator._collect_brief_actuals 의 KIS 지수/업종 조회 대역"""

    def __init__(self, kospi=None, sectors=None):
        self._kospi = kospi or {}
        self._sectors = sectors or []

    async def fetch_index_price(self, code):
        if code == "0001":
            return dict(self._kospi) if self._kospi else None
        return None

    async def fetch_sector_indices(self):
        return list(self._sectors)


class _FakeUMD:
    """generate_us_market_report 의 US 데이터 대역"""

    def __init__(self, avg_pct, sector_signals=None):
        self._avg = avg_pct
        self._sector_signals = sector_signals or {}

    async def fetch_us_market_summary(self, force_refresh=False):
        from src.data.providers.us_market_data import INDEX_SYMBOLS
        return {
            sym: {"change_pct": self._avg, "price": 100.0, "name": sym}
            for sym in INDEX_SYMBOLS
        }

    async def get_sector_signals(self):
        return dict(self._sector_signals)

    async def fetch_sp500_stocks(self):
        return {}


class _CapturingNotifier:
    def __init__(self):
        self.sent = []
        self.report_chat_id = "test-chat"

    async def send_report(self, msg):
        self.sent.append(msg)
        return True

    async def send_message(self, msg):
        self.sent.append(msg)
        return True


class _RegimeScreener:
    """12:00 재분류가 읽는 08:20 스크리너 메모리 대역"""

    def __init__(self, closes, last_bar_date, loaded_at=None, kospi_regime="bull"):
        self._kospi_closes = list(closes)
        self._kospi_last_bar_date = last_bar_date
        self._kospi_loaded_at = loaded_at or datetime(2026, 9, 14, 8, 20, 0)
        self._kospi_regime = kospi_regime

    def get_kospi_change(self):
        return {"c5": 0.0, "c20": 0.0, "level": 0.0}  # 사용되면 위장 — 호출 금지

    def get_market_regime(self):
        return self._kospi_regime


class _RegimeBatchAnalyzer:
    def __init__(self, screener, intraday_state=None, intraday_pct=None, updated_at=None):
        self._screener = screener
        self._intraday_state = intraday_state
        self._intraday_kospi_pct = intraday_pct
        self._intraday_updated_at = updated_at


class _KisMarketData:
    def __init__(self, responses):
        self._responses = dict(responses)
        self.calls = []

    async def fetch_index_price(self, index_code="0001"):
        self.calls.append(index_code)
        return self._responses.get(index_code)


class _Orch:
    """07:30 브리핑의 data_status_summary 대역"""

    def data_status_summary(self, opinions=None):
        return {"counts": {"ok": 6, "partial": 0, "insufficient": 0},
                "insufficient_experts": [], "note": None,
                "valid_n": 6, "insufficient_coverage": False}


def _stub_dr_collaborators(monkeypatch, notifier=None):
    monkeypatch.setattr(dr_mod, "get_telegram_notifier", lambda: notifier or SimpleNamespace())
    monkeypatch.setattr(dr_mod, "get_screener", lambda: SimpleNamespace())
    monkeypatch.setattr(dr_mod, "get_theme_detector", lambda: SimpleNamespace())
    monkeypatch.setattr(dr_mod, "NewsCollector", lambda: SimpleNamespace())


def _patch_common_paths(monkeypatch, tmp_path):
    """전 슬롯 공용 — Path.home()/tmp 고정 + 모듈 상수 정합화"""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    brief_path = tmp_path / ".cache" / "ai_trader" / "llm_morning_brief.json"
    ledger_path = tmp_path / ".cache" / "ai_trader" / "morning_brief_eval.jsonl"
    monkeypatch.setattr(dr_mod, "MORNING_BRIEF_PATH", brief_path)
    monkeypatch.setattr(dr_mod, "MORNING_BRIEF_LEDGER_PATH", ledger_path)
    monkeypatch.setattr(dr_mod, "_today", lambda: _KR_DATE)
    return brief_path, ledger_path


def _regime_file(tmp_path):
    path = tmp_path / ".cache" / "ai_trader" / "llm_regime_today.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _register_position(em, symbol="005930"):
    from src.core.types import Position, PositionSide
    pos = Position(
        symbol=symbol, name="테스트", side=PositionSide.LONG,
        quantity=10, avg_price=Decimal("10000"), current_price=Decimal("10000"),
        strategy="sepa_trend",
    )
    em.register_position(pos)
    return pos


# ══════════════════════════════════════════════════════════════════════════
# 07:00 → 07:30 → 12:00 공용 파이프라인 (main flow + case f 가 공유)
# ══════════════════════════════════════════════════════════════════════════

def _run_morning_to_noon(monkeypatch, tmp_path):
    """07:00 생성 → 07:30 발송(기록) → 12:00 재분류+ExitManager 적용까지 실행.

    Returns dict: dr, bot, sched, brief_path, ledger_path, exit_manager, position
    """
    brief_path, ledger_path = _patch_common_paths(monkeypatch, tmp_path)
    KRScheduler._morning_brief_eval_date = None  # 클래스 상태 오염 방지

    # ── 07:00: 미국 마감 리포트 + LLM 모닝브리프 생성 ──
    notifier_0700 = _CapturingNotifier()
    _stub_dr_collaborators(monkeypatch, notifier=notifier_0700)

    import src.utils.llm as llm_mod
    llm_0700 = _FakeLLM(complete_content=(
        "미국 증시는 반도체 강세에 힘입어 상승 마감했다. "
        "AI/반도체 관련 국내 테마는 주목받을 수 있는 재료다. "
        "2차전지 업종도 관심권으로 거론된다. "
        "국내 장전 자료가 없어 개장 방향은 판단하지 않는다."
    ))
    monkeypatch.setattr(llm_mod, "get_llm_manager", lambda: llm_0700)

    import src.analytics.us_market_chart as chart_mod
    monkeypatch.setattr(chart_mod, "generate_combined_chart", lambda **kw: None)

    kmd = _FakeKMD(
        kospi={"price": 2400.0, "change": -80.0, "open": 2450.0, "change_pct": -3.26},
        sectors=[{"name": "전기전자", "change_pct": -4.2}, {"name": "의약품", "change_pct": -2.0}],
    )
    dr = dr_mod.DailyReportGenerator(kis_market_data=kmd)
    dr._us_market_data = _FakeUMD(
        avg_pct=0.6,
        sector_signals={
            "AI/반도체": {"boost": 5, "us_avg_pct": 1.2, "top_movers": ["NVDA", "AMD"]},
            "2차전지": {"boost": 3, "us_avg_pct": 0.5, "top_movers": ["LIT"]},
        },
    )

    us_report = asyncio.run(dr.generate_us_market_report(send_telegram=True))

    assert notifier_0700.sent, "07:00 리포트가 발송되지 않았다"
    all_0700_text = "\n".join(notifier_0700.sent) + "\n" + (us_report or "")
    assert "갭업 가능성" not in all_0700_text and "갭상승" not in all_0700_text, (
        f"F21: 국내 자료 없이 미국 지수 평균만으로 한국 개장 방향을 단정했다:\n{all_0700_text}"
    )

    archive = json.loads(
        (tmp_path / ".cache" / "ai_trader" / "morning_brief" / f"{_KR_DATE.isoformat()}.json")
        .read_text(encoding="utf-8")
    )
    assert len(archive["generated"]) == 1, f"07:00 생성 아카이브가 없다: {archive}"
    assert archive["generated"][0]["kr_date"] == _KR_DATE.isoformat()

    # ── 07:30: 전문가 브리핑 결합 발송 (record_dispatch=True, morning 슬롯) ──
    notifier_0730 = _CapturingNotifier()
    import src.utils.telegram as tg_mod
    monkeypatch.setattr(tg_mod, "get_telegram_notifier", lambda: notifier_0730)

    import src.data.providers.disclosure_feed as disc_mod
    async def _no_disclosure(top_n=5, days=3):
        return ""
    monkeypatch.setattr(disc_mod, "fetch_disclosure_summary", _no_disclosure)

    monkeypatch.setattr(kr_scheduler, "_now_kst",
                         lambda: datetime(2026, 9, 14, 7, 30, 0), raising=False)

    sched = object.__new__(KRScheduler)
    bot = SimpleNamespace(expert_orchestrator=_Orch(), report_generator=dr)
    sched.bot = bot

    asyncio.run(sched._send_expert_briefing_telegram(
        "🌅 장전", {}, 2, "neutral", False,
        use_report_channel=True, record_dispatch=True,
    ))
    assert notifier_0730.sent, "07:30 결합 발송이 이뤄지지 않았다"
    combined = "\n".join(notifier_0730.sent)
    assert "미국시장 마감" in combined or "LLM 모닝브리프" in combined or "AI/반도체" in combined, (
        f"07:30 발송문에 07:00 브리프 본문이 결합되지 않았다:\n{combined[:400]}"
    )

    archive = json.loads(
        (tmp_path / ".cache" / "ai_trader" / "morning_brief" / f"{_KR_DATE.isoformat()}.json")
        .read_text(encoding="utf-8")
    )
    assert len(archive["dispatch"]) == 1
    assert archive["dispatch"][0]["status"] == "sent"
    assert archive["dispatch"][0]["expert_consensus"] == {
        "score": 2, "bias": "neutral", "valid_n": 0,
    }

    # ── 12:00: 장중 재분류 → ExitManager 실적용 ──
    prev = [100.0] * 29
    closes_with_stale_today = prev + [104.0]  # 오전에 이미 로드된 당일 봉(위장 최신값)
    screener = _RegimeScreener(closes_with_stale_today, last_bar_date=_KR_DATE, kospi_regime="bull")
    ba = _RegimeBatchAnalyzer(
        screener, intraday_state="normal", intraday_pct=0.0,
        updated_at=datetime(2026, 9, 14, 9, 5, 0),  # 감지기는 아직 급락을 못 봤다(당일)
    )
    kis_md = _KisMarketData({
        "0001": {"price": 97.0, "change_pct": -3.0},
        "1001": {"change_pct": -2.0},
    })
    exit_mgr = _make_exit_manager(tmp_path, monkeypatch)
    pos = _register_position(exit_mgr)

    regime_adapter = MarketRegimeAdapter()
    engine = SimpleNamespace(_regime_adapter=regime_adapter)
    bot.batch_analyzer = ba
    bot.kis_market_data = kis_md
    bot.exit_manager = exit_mgr
    bot.engine = engine
    bot.config = {"kr": {"llm_ops": {"regime_conflict_guard_enabled": True}}}

    llm_1200 = _FakeLLM(json_result={"regime": "trending_bull", "confidence": 0.85})
    monkeypatch.setattr(llm_mod, "get_llm_manager", lambda: llm_1200)

    import src.data.providers.us_market_data as umd_mod

    class _EmptyUMD:
        async def get_overnight_signal(self):
            return {"indices": {}, "indices_normalized": {}}
    monkeypatch.setattr(umd_mod, "get_us_market_data", lambda: _EmptyUMD())

    monkeypatch.setattr(kr_scheduler, "_now_kst",
                         lambda: datetime(2026, 9, 14, 12, 0, 0), raising=False)

    # 아침 trending_bull 을 먼저 적용해두면(현실적인 08:10 기준) F15 확인이 의미가 생긴다
    exit_mgr.apply_regime_params("trending_bull")
    assert exit_mgr._states[pos.symbol].stale_high_days == 7

    asyncio.run(sched._run_llm_regime_classifier(label="12:00 (장중 업데이트)"))

    saved = _regime_file(tmp_path)
    meta = saved.get("input_meta", {})
    assert saved.get("regime") == "neutral", (
        f"F14: 정오 재조회의 KOSPI -3.0% 급락으로 trending_bull → neutral 로 강등돼야 한다: {saved}"
    )
    assert saved.get("regime_capped") is True
    assert meta.get("kospi_c5") == pytest.approx(-3.0, abs=0.05), (
        f"F13: 오전 위장값(104, +4%)이 아니라 정오 재조회(97)로 교체된 c5 여야 한다: {meta}"
    )
    assert "당일 잠정봉 교체" in (meta.get("kospi_bars_as_of") or "")

    asyncio.run(sched._apply_regime_to_exit_manager())
    assert exit_mgr._states[pos.symbol].stale_high_days == 5, (
        f"충돌 방지 장치 적용 후 neutral(5) 이어야 하는데 "
        f"{exit_mgr._states[pos.symbol].stale_high_days} — 아침 trending_bull(7) 이 되살아났다"
    )

    return {
        "dr": dr, "bot": bot, "sched": sched, "exit_manager": exit_mgr, "position": pos,
        "brief_path": brief_path, "ledger_path": ledger_path,
        "kmd_eval": kmd,
    }


def _make_exit_manager(tmp_path, monkeypatch):
    # Path.home 은 이미 _patch_common_paths 에서 tmp_path 로 고정돼 있다
    from src.strategies.exit_manager import ExitManager, ExitConfig
    return ExitManager(config=ExitConfig(), market="KR")


# ══════════════════════════════════════════════════════════════════════════
# 메인 흐름: 07:00 → 07:30 → 12:00 → (30분 sync) → 20:30
# ══════════════════════════════════════════════════════════════════════════

def test_full_day_flow_07_00_to_20_30(monkeypatch, tmp_path):
    ctx = _run_morning_to_noon(monkeypatch, tmp_path)
    exit_mgr, pos = ctx["exit_manager"], ctx["position"]

    # ── 30분 sync(monitor_positions) — 캐시 원본(trending_bull) 을 재적용하지 않는다 (F15) ──
    from src.core.batch_analyzer import BatchAnalyzer

    class _Broker:
        async def get_quote(self, symbol):
            return None  # 이번 검증은 regime 동기화 구간만 — 시세 갱신은 스킵

    ba2 = object.__new__(BatchAnalyzer)
    ba2._engine = SimpleNamespace(portfolio=SimpleNamespace(positions={pos.symbol: pos}))
    ba2._broker = _Broker()
    ba2._exit_manager = exit_mgr
    ba2._composite_cache_date = date.today()  # _refresh_composite_cache 조기 반환
    ba2._config = {}

    asyncio.run(ba2.monitor_positions())

    assert exit_mgr._states[pos.symbol].stale_high_days == 5, (
        "30분 sync(monitor_positions) 가 아침 trending_bull 캐시를 재적용해 "
        f"stale_high_days 가 되돌아갔다: {exit_mgr._states[pos.symbol].stale_high_days}"
    )

    # ── 20:30: 사후 평가 ──
    monkeypatch.setattr(kr_scheduler, "_now_kst",
                         lambda: datetime(2026, 9, 14, 20, 30, 0), raising=False)

    record = asyncio.run(
        KRScheduler._run_morning_brief_evaluation(ctx["bot"], _KR_DATE)
    )
    assert record is not None, "20:30 평가 훅이 아무것도 반환하지 않았다"

    lines = ctx["ledger_path"].read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1, f"원장에 정확히 1건이 기록돼야 한다: {lines}"
    rec = json.loads(lines[0])

    assert rec["dispatched"] is True, "07:30 발송 성공(status=sent) 스냅샷을 못 찾았다 (F19)"
    assert rec["brief_ref"] is not None

    # 개장/종가 축 — us_close_only 라 주장 자체가 없다(정상 기권, hit=None + 사유)
    assert rec["open_direction"]["hit"] is None
    assert rec["open_direction"]["reason"], "기권 사유가 비어 있다"
    assert rec["close_direction"]["hit"] is None
    assert rec["close_direction"]["reason"]

    # 전문가 축 — 07:30 이 실제로 계산·발송한 +2(flat) 가 반영돼야 한다 (F19)
    assert rec["expert_direction"]["claimed"] == "flat", rec["expert_direction"]
    assert rec["expert_direction"]["hit"] is False, (
        "전문가 종합판단(flat)과 KOSPI 종가(-3.26%, down) 가 불일치하는데 "
        f"hit 이 False 가 아니다(미기록 None 으로 남으면 F19 미해결): {rec['expert_direction']}"
    )

    # 섹터 축 — 매핑된 테마(AI/반도체→전기전자) 는 outperformed 판정, 미매핑(2차전지) 은 사유
    sectors_by_name = {s["name"]: s for s in rec["sectors"]}
    assert sectors_by_name["AI/반도체"]["outperformed"] is not None, (
        f"F20: 생산자 섹터명(AI/반도체)과 실측 업종명(전기전자) 매핑이 평가에 반영되지 않았다: "
        f"{sectors_by_name['AI/반도체']}"
    )
    assert sectors_by_name["2차전지"]["outperformed"] is None
    assert sectors_by_name["2차전지"]["reason"] == "평가 대상 미합의"

    # 재실행 방지 — 같은 훅을 다시 불러도(리부트 없이) 원장 중복 없음
    record2 = asyncio.run(
        KRScheduler._run_morning_brief_evaluation(ctx["bot"], _KR_DATE)
    )
    assert record2 is None, "같은 프로세스 내 재호출은 클래스 가드로 완전히 스킵돼야 한다"
    lines2 = ctx["ledger_path"].read_text(encoding="utf-8").splitlines()
    assert len(lines2) == 1


# ══════════════════════════════════════════════════════════════════════════
# (f) 재시작 재현 — 프로세스 재기동(클래스 상태 리셋)을 시뮬레이션해도 원장 1건
# ══════════════════════════════════════════════════════════════════════════

def test_case_f_restart_dedup_keeps_single_ledger_record(monkeypatch, tmp_path):
    ctx = _run_morning_to_noon(monkeypatch, tmp_path)

    monkeypatch.setattr(kr_scheduler, "_now_kst",
                         lambda: datetime(2026, 9, 14, 20, 30, 0), raising=False)

    record1 = asyncio.run(
        KRScheduler._run_morning_brief_evaluation(ctx["bot"], _KR_DATE)
    )
    assert record1 is not None

    # 프로세스 재시작 시뮬레이션 — classmethod 의 인메모리 가드(_morning_brief_eval_date)는
    # 재기동하면 클래스 정의값(None)으로 되돌아간다. 진짜 중복 방지는
    # evaluate_morning_brief -> find_ledger_record (원장 파일 기반) 이어야 한다.
    KRScheduler._morning_brief_eval_date = None

    record2 = asyncio.run(
        KRScheduler._run_morning_brief_evaluation(ctx["bot"], _KR_DATE)
    )
    assert record2 is not None, "재시작 후 재평가 호출은 (원장에서) 기존 레코드를 반환해야 한다"
    assert record2 == record1, "재시작 후 재평가가 다른 레코드를 만들어냈다 (원장 중복 위험)"

    lines = ctx["ledger_path"].read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1, (
        f"F19/F22: 재시작 시뮬레이션 후에도 원장 레코드는 1건이어야 한다: {len(lines)}건"
    )


# ══════════════════════════════════════════════════════════════════════════
# (a) 전일 crash 감지기 상태는 오늘 12:00 캡의 근거가 아니다
# ══════════════════════════════════════════════════════════════════════════

def test_case_a_stale_detector_from_yesterday_not_used_as_cap(monkeypatch, tmp_path):
    _patch_common_paths(monkeypatch, tmp_path)

    screener = _RegimeScreener([100.0] * 30, last_bar_date=date(2026, 9, 10))  # 오래된 봉 → 재계산 없음
    ba = _RegimeBatchAnalyzer(
        screener,
        intraday_state="crash", intraday_pct=-3.3,
        updated_at=datetime(2026, 9, 13, 15, 34, 0),  # 어제 갱신 — 당일 아님
    )
    kis_md = _KisMarketData({
        "0001": {"price": 2500.0, "change_pct": 0.3},   # 오늘은 평온(정상)
        "1001": {"change_pct": 0.2},
    })
    bot = SimpleNamespace(
        batch_analyzer=ba, kis_market_data=kis_md,
        engine=SimpleNamespace(_regime_adapter=MarketRegimeAdapter()),
        config={"kr": {"llm_ops": {"regime_conflict_guard_enabled": True}}},
    )
    sched = object.__new__(KRScheduler)
    sched.bot = bot

    import src.utils.llm as llm_mod
    llm = _FakeLLM(json_result={"regime": "trending_bull", "confidence": 0.8})
    monkeypatch.setattr(llm_mod, "get_llm_manager", lambda: llm)
    import src.data.providers.us_market_data as umd_mod
    class _EmptyUMD:
        async def get_overnight_signal(self):
            return {"indices": {}, "indices_normalized": {}}
    monkeypatch.setattr(umd_mod, "get_us_market_data", lambda: _EmptyUMD())
    monkeypatch.setattr(kr_scheduler, "_now_kst",
                         lambda: datetime(2026, 9, 14, 12, 0, 0), raising=False)

    asyncio.run(sched._run_llm_regime_classifier(label="12:00 (장중 업데이트)"))

    saved = _regime_file(tmp_path)
    meta = saved.get("input_meta", {})
    assert saved.get("regime") == "trending_bull", (
        f"전일 crash(당일 아님) 가 오늘 캡의 근거로 잘못 쓰였다: {saved}"
    )
    assert meta.get("intraday_crash_level") is None, (
        f"어제 갱신된 감지기 상태를 당일 사실처럼 실었다: {meta}"
    )
    assert "급락감지기(당일 갱신 없음)" in (meta.get("missing_fields") or [])


# ══════════════════════════════════════════════════════════════════════════
# (b) 어댑터에 늦게 도착한(as_of 더 이른) 과거 normal 이 최신 crash 를 덮지 않는다
# ══════════════════════════════════════════════════════════════════════════

def test_case_b_late_arriving_stale_normal_does_not_override_fresh_crash(monkeypatch, tmp_path):
    _patch_common_paths(monkeypatch, tmp_path)

    screener = _RegimeScreener([100.0] * 30, last_bar_date=date(2026, 9, 10))
    ba = _RegimeBatchAnalyzer(screener, intraday_state=None, updated_at=None)
    adapter = MarketRegimeAdapter()
    engine = SimpleNamespace(_regime_adapter=adapter)
    bot = SimpleNamespace(
        batch_analyzer=ba,
        engine=engine,
        config={"kr": {"llm_ops": {"regime_conflict_guard_enabled": True}}},
    )
    sched = object.__new__(KRScheduler)
    sched.bot = bot

    import src.utils.llm as llm_mod
    import src.data.providers.us_market_data as umd_mod
    class _EmptyUMD:
        async def get_overnight_signal(self):
            return {"indices": {}, "indices_normalized": {}}
    monkeypatch.setattr(umd_mod, "get_us_market_data", lambda: _EmptyUMD())

    # ① 12:00 — KOSPI -3.0% 급락 관측 → 어댑터에 crash 가 as_of=12:00 로 기록된다
    kis_md_1200 = _KisMarketData({
        "0001": {"price": 97.0, "change_pct": -3.0}, "1001": {"change_pct": -2.5},
    })
    bot.kis_market_data = kis_md_1200
    monkeypatch.setattr(llm_mod, "get_llm_manager",
                         lambda: _FakeLLM(json_result={"regime": "ranging", "confidence": 0.6}))
    monkeypatch.setattr(kr_scheduler, "_now_kst",
                         lambda: datetime(2026, 9, 14, 12, 0, 0), raising=False)
    asyncio.run(sched._run_llm_regime_classifier(label="12:00 (장중 업데이트)"))

    assert adapter.horizons.intraday_risk == "crash"
    assert adapter.horizons.intraday_risk_as_of == datetime(2026, 9, 14, 12, 0, 0)

    # ② 지연된 재시도(예: 타임아웃 재시도 루프)가 09:05 시각 기준 "정상" 관측을
    #    이제서야 밀어 넣으려 한다 — as_of 가 기존(12:00)보다 이르므로 무시돼야 한다.
    kis_md_delayed = _KisMarketData({
        "0001": {"price": 2500.0, "change_pct": 0.2}, "1001": {"change_pct": 0.1},
    })
    bot.kis_market_data = kis_md_delayed
    monkeypatch.setattr(llm_mod, "get_llm_manager",
                         lambda: _FakeLLM(json_result={"regime": "trending_bull", "confidence": 0.7}))
    monkeypatch.setattr(kr_scheduler, "_now_kst",
                         lambda: datetime(2026, 9, 14, 9, 5, 0), raising=False)
    asyncio.run(sched._run_llm_regime_classifier(label="09:05 (지연 재시도)"))

    assert adapter.horizons.intraday_risk == "crash", (
        f"늦게 도착한 과거(09:05) normal 관측이 최신(12:00) crash 를 덮어썼다: "
        f"{adapter.horizons.intraday_risk}@{adapter.horizons.intraday_risk_as_of}"
    )
    assert adapter.horizons.intraday_risk_as_of == datetime(2026, 9, 14, 12, 0, 0)


# ══════════════════════════════════════════════════════════════════════════
# (c) 12:00 지수 조회 실패 → missing_fields KOSPI당일, 캡은 감지기 당일 상태만
# ══════════════════════════════════════════════════════════════════════════

def test_case_c_index_fetch_failure_falls_back_to_detector_only_cap(monkeypatch, tmp_path):
    _patch_common_paths(monkeypatch, tmp_path)

    screener = _RegimeScreener([100.0] * 30, last_bar_date=date(2026, 9, 10))
    ba = _RegimeBatchAnalyzer(
        screener, intraday_state="crash", intraday_pct=-2.8,
        updated_at=datetime(2026, 9, 14, 11, 55, 0),  # 당일 갱신
    )
    kis_md = _KisMarketData({})  # 0001/1001 모두 결측 → fetch_index_price 가 None 반환
    bot = SimpleNamespace(
        batch_analyzer=ba, kis_market_data=kis_md,
        engine=SimpleNamespace(_regime_adapter=MarketRegimeAdapter()),
        config={"kr": {"llm_ops": {"regime_conflict_guard_enabled": True}}},
    )
    sched = object.__new__(KRScheduler)
    sched.bot = bot

    import src.utils.llm as llm_mod
    monkeypatch.setattr(llm_mod, "get_llm_manager",
                         lambda: _FakeLLM(json_result={"regime": "trending_bull", "confidence": 0.8}))
    import src.data.providers.us_market_data as umd_mod
    class _EmptyUMD:
        async def get_overnight_signal(self):
            return {"indices": {}, "indices_normalized": {}}
    monkeypatch.setattr(umd_mod, "get_us_market_data", lambda: _EmptyUMD())
    monkeypatch.setattr(kr_scheduler, "_now_kst",
                         lambda: datetime(2026, 9, 14, 12, 0, 0), raising=False)

    asyncio.run(sched._run_llm_regime_classifier(label="12:00 (장중 업데이트)"))

    saved = _regime_file(tmp_path)
    meta = saved.get("input_meta", {})
    assert "KOSPI당일" in (meta.get("missing_fields") or []), meta
    assert meta.get("kospi_today_pct") is None
    assert meta.get("intraday_level_observed") is None, (
        "지수 조회가 실패했는데 이번 조회 실측이 있는 것처럼 분류됐다"
    )
    assert meta.get("intraday_cap_level") == "crash", (
        f"지수 조회 실패 시에는 감지기 당일 상태(crash)만으로 캡이 걸려야 한다: {meta}"
    )
    assert saved.get("regime") != "trending_bull", (
        f"감지기 당일 crash 가 있는데도 trending_bull 이 그대로 저장됐다: {saved}"
    )


# ══════════════════════════════════════════════════════════════════════════
# (d) 07:30 발송 실패(status=failed) 만 있는 날 — 20:30 은 전문가 축을 평가하지
#     않고 사유를 남긴다 (생성본 자체는 있음)
# ══════════════════════════════════════════════════════════════════════════

def test_case_d_dispatch_failed_only_leaves_expert_axis_unscored(monkeypatch, tmp_path):
    brief_path, ledger_path = _patch_common_paths(monkeypatch, tmp_path)

    rec = {
        "kr_date": _KR_DATE.isoformat(), "generated_at": "2026-09-14T07:00:00",
        "text": "07:00 생성본", "claims": {"open_direction": None, "close_direction": None,
                                          "sectors": [], "basis": []},
        "scope": dr_mod.SCOPE_US_ONLY,
    }
    dr_mod.save_morning_brief(rec, path=brief_path)
    dr_mod.record_morning_brief_dispatch(
        kr_date=_KR_DATE.isoformat(), sent_text="발송 시도(실패)",
        expert_consensus=None, status="failed",
    )

    kmd = _FakeKMD(kospi={"price": 2400.0, "change": -80.0, "open": 2450.0, "change_pct": -1.0})
    _stub_dr_collaborators(monkeypatch)
    dr = dr_mod.DailyReportGenerator(kis_market_data=kmd)

    record = asyncio.run(dr.evaluate_morning_brief(report_date=_KR_DATE))

    assert record is not None
    assert record["dispatched"] is False
    assert record["expert_direction"]["hit"] is None
    assert record["expert_direction"]["claimed"] is None
    assert record["expert_direction"]["reason"] == "07:30 발송 실패 — 전문가 판단 미평가", (
        record["expert_direction"]
    )


# ══════════════════════════════════════════════════════════════════════════
# (e) Yahoo 부분 응답(VIX 가격 없음)이 12:00 input_meta 에 VIX 결측으로 반영된다
# ══════════════════════════════════════════════════════════════════════════

class _FakeYahooResp:
    def __init__(self, payload):
        self.status = 200
        self._payload = payload

    async def json(self):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeYahooSession:
    def __init__(self, payload):
        self._payload = payload

    def get(self, url, params=None, **kw):
        return _FakeYahooResp(self._payload)


def test_case_e_yahoo_partial_vix_missing_reflected_in_regime_input(monkeypatch, tmp_path):
    _patch_common_paths(monkeypatch, tmp_path)

    screener = _RegimeScreener([100.0] * 30, last_bar_date=date(2026, 9, 10))
    ba = _RegimeBatchAnalyzer(screener)
    bot = SimpleNamespace(
        batch_analyzer=ba, kis_market_data=_KisMarketData({}),
        engine=SimpleNamespace(_regime_adapter=MarketRegimeAdapter()),
        config={"kr": {"llm_ops": {"regime_conflict_guard_enabled": False}}},
    )
    sched = object.__new__(KRScheduler)
    sched.bot = bot

    import src.utils.llm as llm_mod
    monkeypatch.setattr(llm_mod, "get_llm_manager",
                         lambda: _FakeLLM(json_result={"regime": "ranging", "confidence": 0.6}))

    from src.data.providers.us_market_data import USMarketData

    def _q(sym, price, chg, pct):
        return {"symbol": sym, "regularMarketPrice": price, "regularMarketChange": chg,
                "regularMarketChangePercent": pct, "regularMarketTime": 1757900000}

    payload = {"quoteResponse": {"result": [
        _q("^GSPC", 6000.0, 30.0, 0.5), _q("^IXIC", 20000.0, 120.0, 0.6),
        _q("^SOX", 5500.0, 90.0, 1.6), _q("^DJI", 44000.0, 100.0, 0.2),
        {"symbol": "^VIX", "regularMarketTime": 1757900000},  # 가격·변동률 없음
    ]}}

    umd = USMarketData()

    async def _fake_get_session():
        return _FakeYahooSession(payload)
    monkeypatch.setattr(umd, "_get_session", _fake_get_session)

    import src.data.providers.us_market_data as umd_mod
    monkeypatch.setattr(umd_mod, "get_us_market_data", lambda: umd)

    monkeypatch.setattr(kr_scheduler, "_now_kst",
                         lambda: datetime(2026, 9, 14, 8, 10, 0), raising=False)

    asyncio.run(sched._run_llm_regime_classifier(label="08:10"))

    saved = _regime_file(tmp_path)
    meta = saved.get("input_meta", {})
    assert "VIX" in (meta.get("missing_fields") or []), (
        f"VIX 가격이 없는데 프롬프트 입력에 결측으로 반영되지 않았다: {meta}"
    )
    # 다른 지수는 정상 수집돼야 한다 (VIX 만 결측이지 전체 실패가 아니다)
    assert "SP500" not in (meta.get("missing_fields") or []), meta


# ══════════════════════════════════════════════════════════════════════════
# 격리 재확인 — curl_cffi/yfinance 가 실제로 막히는지 (conftest 검증)
# ══════════════════════════════════════════════════════════════════════════

def test_isolation_blocks_yfinance_curl_cffi():
    import tests.conftest as ct

    before = len(ct.VIOLATIONS)
    try:
        import yfinance as yf
    except Exception:
        pytest.skip("yfinance 미설치 환경 — 격리 대상 자체가 없음")

    ticker = yf.Ticker("AAPL")
    raised = False
    try:
        hist = ticker.history(period="1d")
    except Exception:
        raised = True
        hist = None

    after = len(ct.VIOLATIONS)
    assert raised or (hist is None or getattr(hist, "empty", True)), (
        "yfinance 호출이 예외도 없이 빈 결과도 아닌 실데이터를 반환했다 — 네트워크 차단이 "
        "우회됐을 수 있다"
    )
    assert after > before, (
        f"yfinance.Ticker.history() 호출이 conftest 위반 카운트를 증가시키지 않았다 "
        f"(차단이 감지되지 않음): before={before} after={after}"
    )
