"""T10 담당 D — 독립 재현 테스트 C (F19~F22, 발송·평가 연결 경로)

구현자(C)와 무관하게 기준 SHA(a59e29f)의 실제 코드를 기존 공개 진입점으로만
호출해 리뷰 주장을 재현한다. 아래 각 테스트는 기준 SHA에서 실패해야 하고
(결함 재현), T10 수정 통합 후에는 통과해야 한다.

대상 진입점:
  F19 — DailyReportGenerator.build_morning_brief/save_morning_brief
       + KRScheduler._send_expert_briefing_telegram (07:30 발송)
       + DailyReportGenerator.evaluate_morning_brief
  F20 — daily_report.extract_brief_claims + morning_brief_eval.evaluate
       (생산자 섹터 키 vs KIS 실측 업종명 불일치)
  F21 — DailyReportGenerator.generate_us_market_report (미국 지수만으로
       한국 방향 단정)
  F22 — daily_report.save_morning_brief (같은 날 재실행 시 원문 덮어쓰기)
       + morning_brief_eval.evaluate (원문 참조 보존 여부)

실행: /home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest tests/test_t10_repro_c.py -q
네트워크·KIS/LLM 자격증명 무접촉 — 모든 협력자는 가짜/tmp_path.
"""

import asyncio
import json
import sys
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import src.analytics.daily_report as dr_mod  # noqa: E402
from src.analytics import morning_brief_eval  # noqa: E402


# ══════════════════════════════════════════════════════════════════════════
# 공용 가짜 협력자
# ══════════════════════════════════════════════════════════════════════════

class _FakeLLMResult:
    def __init__(self, content, model="test-model", success=True):
        self.content = content
        self.model = model
        self.success = success


class _FakeLLM:
    def __init__(self, content):
        self._content = content

    async def complete(self, **kwargs):
        return _FakeLLMResult(self._content)


class _FakeKMD:
    """DailyReportGenerator._collect_brief_actuals 가 쓰는 KIS 지수/업종 조회 대역"""

    def __init__(self, kospi=None, sectors=None):
        self._kospi = kospi or {}
        self._sectors = sectors or []

    async def fetch_index_price(self, code):
        if code == "0001":
            return dict(self._kospi) if self._kospi else None
        return None

    async def fetch_sector_indices(self):
        return list(self._sectors)


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


def _stub_dr_collaborators(monkeypatch):
    """DailyReportGenerator.__init__ 이 모듈 레벨에서 부르는 협력자 4종을 대역으로 교체"""
    monkeypatch.setattr(dr_mod, "get_telegram_notifier", lambda: SimpleNamespace())
    monkeypatch.setattr(dr_mod, "get_screener", lambda: SimpleNamespace())
    monkeypatch.setattr(dr_mod, "get_theme_detector", lambda: SimpleNamespace())
    monkeypatch.setattr(dr_mod, "NewsCollector", lambda: SimpleNamespace())


def _make_dr(monkeypatch, kmd=None):
    _stub_dr_collaborators(monkeypatch)
    return dr_mod.DailyReportGenerator(kis_market_data=kmd)


# ══════════════════════════════════════════════════════════════════════════
# F19 — 07:00 생성본은 expert_consensus 가 없고, 07:30 발송이 실제 계산한
#       전문가 종합판단은 발송 문구에만 붙을 뿐 캐시에 되돌아가지 않는다
#       → 저녁 평가(evaluate_morning_brief)는 expert_direction.hit=None 고정
# ══════════════════════════════════════════════════════════════════════════

def test_f19_expert_direction_axis_never_recorded(monkeypatch, tmp_path):
    # build_morning_brief 는 함수 내부에서 `from ..utils.llm import get_llm_manager` 로
    # 다시 import 한다 — 실제 소스 모듈(src.utils.llm)을 패치해야 한다.
    import src.utils.llm as llm_mod
    monkeypatch.setattr(llm_mod, "get_llm_manager", lambda: _FakeLLM(
        "미국 증시는 상승 마감했다. 반도체 랠리가 이어졌다."
    ))

    kmd = _FakeKMD(kospi={"price": 2400.0, "change": -80.0, "open": 2450.0,
                           "change_pct": -3.26})
    dr = _make_dr(monkeypatch, kmd=kmd)

    # 1) 07:00 생성 — 전문가 데이터가 아직 없다(expert_consensus=None)
    brief = asyncio.run(dr.build_morning_brief(
        quotes={}, sector_signals={}, avg_pct=1.0, us_date_str="2026.09.13",
        mood="상승", kr_inputs=None, expert_consensus=None,
    ))
    assert brief is not None, "브리프 생성 실패 — LLM 대역 확인 필요"

    brief_path = tmp_path / ".cache" / "ai_trader" / "llm_morning_brief.json"
    dr_mod.save_morning_brief(brief, path=brief_path)

    # 2) 07:30 발송 — 이 시점에는 실제 전문가 종합점수(+2, 중립)가 계산된다
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    import src.utils.telegram as tg_mod
    notifier = _CapturingNotifier()
    monkeypatch.setattr(tg_mod, "get_telegram_notifier", lambda: notifier)

    import src.data.providers.disclosure_feed as disc_mod
    async def _no_disclosure(top_n=5, days=3):
        return ""
    monkeypatch.setattr(disc_mod, "fetch_disclosure_summary", _no_disclosure)

    from src.schedulers.kr_scheduler import KRScheduler

    class _Orch:
        def data_status_summary(self, opinions=None):
            return {"counts": {"ok": 6, "partial": 0, "insufficient": 0},
                    "insufficient_experts": [], "note": None,
                    "valid_n": 6, "insufficient_coverage": False}

    sched = object.__new__(KRScheduler)
    sched.bot = SimpleNamespace(expert_orchestrator=_Orch())
    asyncio.run(sched._send_expert_briefing_telegram(
        "🌅 장전", {}, 2, "neutral", False, use_report_channel=True,
    ))
    assert notifier.sent, "07:30 발송이 이뤄지지 않았다"

    # 3) 저녁 평가 — 07:30 이 실제로 판단·발송한 +2(flat)를 반영해야 한다
    # build_morning_brief 가 kr_date 를 date.today() 로 고정 기록하므로(모킹 안 함)
    # 평가일도 동일하게 맞춘다 — 그렇지 않으면 무관한 "기준일 불일치" 사유로 스킵된다.
    record = asyncio.run(dr.evaluate_morning_brief(
        report_date=date.today(), brief_path=brief_path,
        ledger_path=tmp_path / "morning_brief_eval.jsonl",
    ))
    assert record is not None
    assert record["expert_direction"]["hit"] is not None, (
        f"07:30 이 실제로 계산·발송한 전문가 종합판단(+2)이 브리프 캐시로 되돌아가지 "
        f"않아 저녁 평가가 '미기록(hit=None)'으로 고정됐다: {record['expert_direction']}"
    )


# ══════════════════════════════════════════════════════════════════════════
# F20 — 브리프 생성 시 사용한 섹터 표시명(AI/반도체 등)과 KIS 업종 실측명
#       (전기전자 등)이 달라 outperformed 가 항상 결측 처리된다
# ══════════════════════════════════════════════════════════════════════════

def test_f20_sector_claim_vs_actual_name_mismatch_leaves_all_unscored():
    text = (
        "AI/반도체 관련주 강세가 예상된다. 바이오 업종도 관심을 받을 전망이다. "
        "다만 반대로 조정 가능성도 있다."
    )
    sector_signals = {"AI/반도체": {"boost": 5}, "바이오": {"boost": 3}}
    claims = dr_mod.extract_brief_claims(
        text, sector_signals, scope=dr_mod.SCOPE_US_ONLY, basis=["미국 지수"],
    )
    assert set(claims["sectors"]) == {"AI/반도체", "바이오"}, claims

    brief = {
        "kr_date": "2026-09-14", "generated_at": "2026-09-14T07:00:00",
        "claims": claims,
    }
    actual = {
        "date": "2026-09-14",
        "kospi": {"close_change_pct": -1.0},
        "sectors": {"전기전자": -4.2, "의약품": -2.0},   # KIS 실측 업종명
    }
    record = morning_brief_eval.evaluate(brief, actual)

    assert record["sectors"], "언급 섹터가 평가 레코드에 없다"
    assert any(s["outperformed"] is not None for s in record["sectors"]), (
        f"생산자 섹터명(AI/반도체·바이오)과 실측 업종명(전기전자·의약품) 표기가 달라 "
        f"언급 섹터 전부 '업종 수익률 미수집'으로 결측 처리됐다: {record['sectors']}"
    )


# ══════════════════════════════════════════════════════════════════════════
# F21 — 미국 지수 평균만으로 "한국 관련 테마주 갭업 가능성" 같은 한국시장
#       방향 단정 문구가 그대로 발송된다 (국내 자료 전무)
# ══════════════════════════════════════════════════════════════════════════

class _FakeUMD:
    def __init__(self, avg_pct):
        self._avg = avg_pct

    async def fetch_us_market_summary(self, force_refresh=False):
        from src.data.providers.us_market_data import INDEX_SYMBOLS
        return {
            sym: {"change_pct": self._avg, "price": 100.0, "name": sym}
            for sym in INDEX_SYMBOLS
        }

    async def get_sector_signals(self):
        return {}

    async def fetch_sp500_stocks(self):
        return {}


def test_f21_us_report_asserts_kr_direction_from_us_indices_only(monkeypatch, tmp_path):
    notifier = _CapturingNotifier()
    monkeypatch.setattr(dr_mod, "get_telegram_notifier", lambda: notifier)
    monkeypatch.setattr(dr_mod, "get_screener", lambda: SimpleNamespace())
    monkeypatch.setattr(dr_mod, "get_theme_detector", lambda: SimpleNamespace())
    monkeypatch.setattr(dr_mod, "NewsCollector", lambda: SimpleNamespace())

    import src.analytics.us_market_chart as chart_mod
    monkeypatch.setattr(chart_mod, "generate_combined_chart", lambda **kw: None)

    import src.utils.llm as llm_mod
    monkeypatch.setattr(llm_mod, "get_llm_manager", lambda: _FakeLLM("미국 증시 상승 마감"))
    # generate_us_market_report 는 본문 발송 후 내부적으로 build_morning_brief +
    # save_morning_brief(기본 경로=운영 홈)도 호출한다 — 이 테스트의 대상이 아니므로
    # 운영 캐시에 손대지 않도록 저장만 무해화한다 (§0 격리 경계).
    monkeypatch.setattr(dr_mod, "save_morning_brief", lambda record, path=None: tmp_path / "unused.json")

    dr = dr_mod.DailyReportGenerator()
    dr._us_market_data = _FakeUMD(avg_pct=2.0)   # avg_pct >= 1.5 → "강한 상승" 분기

    result = asyncio.run(dr.generate_us_market_report(send_telegram=True))

    assert notifier.sent, "리포트가 발송되지 않았다"
    all_text = "\n".join(notifier.sent) + "\n" + (result or "")
    assert "갭업 가능성" not in all_text, (
        f"국내 자료 없이 미국 지수 평균(+2.0%)만으로 '한국 관련 테마주 갭업 가능성'을 "
        f"단정해 발송했다:\n{all_text}"
    )


# ══════════════════════════════════════════════════════════════════════════
# F22 — 같은 날 재생성 시 최신 캐시가 이전 원문을 덮어쓰고, 저녁 평가 레코드에도
#       어떤 원문을 평가했는지 참조가 남지 않는다
# ══════════════════════════════════════════════════════════════════════════

def test_f22_repeated_save_loses_original_without_eval_reference(tmp_path):
    path = tmp_path / "llm_morning_brief.json"
    rec1 = {"kr_date": "2026-09-14", "generated_at": "2026-09-14T07:00:00",
            "text": "07:00 최초 생성본", "claims": {}}
    rec2 = {"kr_date": "2026-09-14", "generated_at": "2026-09-14T07:05:00",
            "text": "07:05 재실행본(장애 재시도)", "claims": {}}

    dr_mod.save_morning_brief(rec1, path=path)
    dr_mod.save_morning_brief(rec2, path=path)

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["text"] == rec2["text"], "캐시가 최신본을 담고 있는지 우선 확인"
    assert rec1["text"] != saved["text"]  # rec1 원문은 더 이상 이 경로로 복구 불가능(사전 조건 확인)

    # 저녁 평가 레코드가 "어느 원문을 평가했는지" 복원할 참조를 갖는지 확인
    brief = json.loads(path.read_text(encoding="utf-8"))
    actual = {"date": "2026-09-14", "kospi": {"close_change_pct": -1.0}}
    record = morning_brief_eval.evaluate(brief, actual)

    ref_keys = {"brief_ref", "brief_text_sha", "text_sha", "brief_id", "brief_version"}
    present = ref_keys & set(record.keys())
    assert present, (
        f"같은 날 재실행으로 이전 원문(07:00)이 영구 덮어써지는데, 평가 레코드에도 "
        f"어떤 원문을 평가했는지 복원할 참조가 없다 (레코드 키: {sorted(record.keys())})"
    )
