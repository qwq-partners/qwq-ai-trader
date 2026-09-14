"""T10 담당 C — 장전 발송 스냅샷과 장후 평가 (F19~F22)

대상: src/analytics/daily_report.py, src/analytics/morning_brief_eval.py

계기(리뷰 §T10):
- F19: 07:00 생성본엔 expert_consensus 가 없고, 07:30 은 발송문에만 전문가 판단을
  붙였다. 20:30 평가는 캐시의 expert_consensus(None)를 읽어 전문가 축이
  "미기록"으로 고정됐다 — 실제 발송한 판단이 평가에 반영되지 않았다.
- F20: extract_brief_claims 는 생산자 테마명("AI/반도체")을 그대로 claims.sectors
  에 넣고, _collect_brief_actuals 는 KIS 업종명("전기전자")을 키로 쓴다 —
  exact match 가 항상 실패해 업종 평가가 전부 "미수집" 이었다.
- F21: generate_us_market_report 의 고정 문구(market_msg)가 미국 지수 평균만으로
  "한국 관련 테마주 갭업 가능성" 등 한국 개장 방향을 단정해 왔다 — LLM 브리프의
  scope 제약(sanitize_brief_claims)을 전혀 거치지 않는 별도 경로였다.
- F22: save_morning_brief 가 최신 캐시 하나만 덮어써 같은 날 재생성본이 유실되고,
  평가 원장에 원문 참조가 없었다.

이 파일은 실제 배선 경로(record_morning_brief_dispatch 호출 등)를 직접 실행해
검증한다 — 브리프 dict 에 expert_consensus 를 미리 채워 넣는 식으로 우회하지
않는다(F19 명세의 "fixture 로 통과시키지 말 것" 요구).

실행: venv/bin/python -m pytest tests/test_t10_brief_eval.py -q -p no:cacheprovider
"""

import asyncio
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.analytics import daily_report as dr  # noqa: E402
from src.analytics import morning_brief_eval as mbe  # noqa: E402


US_QUOTES_FLAT = {
    "^GSPC": {"change_pct": 0.4, "price": 6100.0},
    "^IXIC": {"change_pct": 0.3, "price": 20100.0},
    "^SOX": {"change_pct": 0.2, "price": 5800.0},
    "^DJI": {"change_pct": 0.1, "price": 42000.0},
}
US_QUOTES_STRONG_UP = {
    "^GSPC": {"change_pct": 2.0, "price": 6200.0},
    "^IXIC": {"change_pct": 2.2, "price": 20500.0},
    "^SOX": {"change_pct": 2.4, "price": 5900.0},
    "^DJI": {"change_pct": 1.8, "price": 42500.0},
}


class _FakeResp:
    def __init__(self, content: str):
        self.content = content
        self.success = True
        self.error = None
        self.model = "gpt-test"


class _FakeLLM:
    def __init__(self, content: str):
        self._content = content
        self.prompts = []

    async def complete(self, prompt, **kwargs):
        self.prompts.append(prompt)
        return _FakeResp(self._content)


class _FakeKMD:
    """KIS 시세 대역 — evaluate_morning_brief 의 _collect_brief_actuals 용"""

    def __init__(self, indices=None, sectors=None):
        self._indices = indices or {}
        self._sectors = sectors or []

    async def fetch_index_price(self, index_code="0001"):
        return self._indices.get(index_code)

    async def fetch_sector_indices(self, market="K"):
        return self._sectors


class _FakeUMD:
    """미국 시세 대역 — generate_us_market_report 용"""

    def __init__(self, quotes, sector_signals=None):
        self._quotes = quotes
        self._sector_signals = sector_signals or {}

    async def fetch_us_market_summary(self, force_refresh=False):
        return self._quotes

    async def get_sector_signals(self):
        return self._sector_signals

    async def fetch_sp500_stocks(self):
        return {}


def _patch_llm(monkeypatch, llm):
    monkeypatch.setattr(dr, "get_llm_manager", lambda: llm, raising=False)
    import src.utils.llm as _llm_mod
    monkeypatch.setattr(_llm_mod, "get_llm_manager", lambda: llm)


def _make_gen(monkeypatch) -> dr.DailyReportGenerator:
    """실 스크리너·텔레그램·테마감지기를 만들지 않는 DailyReportGenerator
    (운영 캐시 mkdir 회피 — conftest 격리 가드가 차단한다)."""
    monkeypatch.setattr(dr, "get_telegram_notifier", lambda: SimpleNamespace())
    monkeypatch.setattr(dr, "get_screener", lambda: SimpleNamespace())
    monkeypatch.setattr(dr, "get_theme_detector", lambda: SimpleNamespace())
    monkeypatch.setattr(dr, "NewsCollector", lambda: SimpleNamespace())
    return dr.DailyReportGenerator()


# ── F19: 발송 스냅샷이 평가에 실제로 연결되는가 ───────────────────────────────

def test_f19_sent_dispatch_snapshot_feeds_expert_axis(monkeypatch, tmp_path):
    """07:00 생성(consensus 없음) → 07:30 실제 발송(record_morning_brief_dispatch,
    status=sent) → 20:30 평가. expert_consensus 를 브리프 dict 에 직접 채우지
    않고 실제 배선 함수를 호출한다."""
    today = date.today()
    _patch_llm(monkeypatch, _FakeLLM("<b>■ 시장 종합 평가</b>\n미국 증시는 강세 마감했다.\n"))
    gen = _make_gen(monkeypatch)

    record = asyncio.run(gen.build_morning_brief(
        quotes=US_QUOTES_FLAT, sector_signals={}, avg_pct=0.3,
        us_date_str=today.isoformat(), mood="보합",
    ))
    assert record["expert_consensus"] is None, "07:00 생성 시점엔 전문가 판단이 없다"

    brief_path = tmp_path / "llm_morning_brief.json"
    archive_dir = tmp_path / "morning_brief"
    dr.save_morning_brief(record, brief_path, archive_dir=archive_dir)

    kr_date = record["kr_date"]
    dr.record_morning_brief_dispatch(
        kr_date=kr_date,
        sent_text="(결합 발송문)",
        expert_consensus={"score": 2, "bias": "neutral", "valid_n": 5},
        status="sent",
        archive_dir=archive_dir,
    )

    # 종가 -3.26% (필수 인수 사례와 동일)
    kmd = _FakeKMD(
        indices={"0001": {"price": 3095.68, "open": 3099.52,
                           "change": -104.32, "change_pct": -3.26}},
        sectors=[],
    )
    gen._kis_market_data = kmd
    ledger_path = tmp_path / "morning_brief_eval.jsonl"
    result = asyncio.run(gen.evaluate_morning_brief(
        date.fromisoformat(kr_date), brief_path=brief_path,
        ledger_path=ledger_path, archive_dir=archive_dir,
    ))

    assert result["dispatched"] is True
    assert result["brief_ref"] is not None and result["brief_ref"]["version"] == 1
    # 전문가 종합 +2(중립, ±5 밴드 내) → flat 주장, 실제 -3.26% 하락 → 미적중
    assert result["expert_direction"]["claimed"] == "flat"
    assert result["expert_direction"]["hit"] is False
    assert result["expert_direction"]["reason"] == "", "발송 스냅샷을 썼으면 '미기록' 사유가 남으면 안 된다"


def test_f19_failed_dispatch_only_skips_expert_axis_with_reason(monkeypatch, tmp_path):
    """발송 실패(status=failed)만 있는 날 → 전문가 축 평가 안 함 + 사유."""
    today = date.today()
    _patch_llm(monkeypatch, _FakeLLM("<b>■ 시장 종합 평가</b>\n미국 증시는 약세 마감했다.\n"))
    gen = _make_gen(monkeypatch)

    record = asyncio.run(gen.build_morning_brief(
        quotes=US_QUOTES_FLAT, sector_signals={}, avg_pct=-0.4,
        us_date_str=today.isoformat(), mood="보합",
    ))
    brief_path = tmp_path / "llm_morning_brief.json"
    archive_dir = tmp_path / "morning_brief"
    dr.save_morning_brief(record, brief_path, archive_dir=archive_dir)

    kr_date = record["kr_date"]
    dr.record_morning_brief_dispatch(
        kr_date=kr_date, sent_text="", expert_consensus=None,
        status="failed", archive_dir=archive_dir,
    )

    gen._kis_market_data = _FakeKMD(
        indices={"0001": {"price": 3095.68, "open": 3099.52,
                           "change": -104.32, "change_pct": -3.26}},
        sectors=[],
    )
    result = asyncio.run(gen.evaluate_morning_brief(
        date.fromisoformat(kr_date), brief_path=brief_path,
        ledger_path=tmp_path / "ledger.jsonl", archive_dir=archive_dir,
    ))

    assert result["dispatched"] is False
    assert result["expert_direction"]["hit"] is None
    assert "발송 실패" in result["expert_direction"]["reason"]


def test_f19_no_dispatch_record_reason_is_not_generic_missing(monkeypatch, tmp_path):
    """생성본은 있으나 07:30 발송 기록이 아예 없으면 dispatched=False +
    '07:30 발송 기록 없음' (기존의 무조건 '미기록'과 구분되는 사유)."""
    today = date.today()
    _patch_llm(monkeypatch, _FakeLLM("<b>■ 시장 종합 평가</b>\n보합권 마감했다.\n"))
    gen = _make_gen(monkeypatch)
    record = asyncio.run(gen.build_morning_brief(
        quotes=US_QUOTES_FLAT, sector_signals={}, avg_pct=0.1,
        us_date_str=today.isoformat(), mood="보합",
    ))
    brief_path = tmp_path / "llm_morning_brief.json"
    archive_dir = tmp_path / "morning_brief"
    dr.save_morning_brief(record, brief_path, archive_dir=archive_dir)

    gen._kis_market_data = _FakeKMD(
        indices={"0001": {"price": 3095.68, "open": 3099.52,
                           "change": -104.32, "change_pct": -3.26}},
        sectors=[],
    )
    result = asyncio.run(gen.evaluate_morning_brief(
        date.fromisoformat(record["kr_date"]), brief_path=brief_path,
        ledger_path=tmp_path / "ledger.jsonl", archive_dir=archive_dir,
    ))
    assert result["dispatched"] is False
    assert result["expert_direction"]["reason"] == "07:30 발송 기록 없음"


def test_f19_rerun_same_date_does_not_duplicate_ledger(monkeypatch, tmp_path):
    """재실행 시 원본 덮어쓰기·평가 분모 중복 증가 금지 — 같은 날짜 레코드가
    이미 있으면 재기록하지 않는다."""
    today = date.today()
    _patch_llm(monkeypatch, _FakeLLM("<b>■ 시장 종합 평가</b>\n보합권 마감했다.\n"))
    gen = _make_gen(monkeypatch)
    record = asyncio.run(gen.build_morning_brief(
        quotes=US_QUOTES_FLAT, sector_signals={}, avg_pct=0.1,
        us_date_str=today.isoformat(), mood="보합",
    ))
    brief_path = tmp_path / "llm_morning_brief.json"
    archive_dir = tmp_path / "morning_brief"
    dr.save_morning_brief(record, brief_path, archive_dir=archive_dir)
    ledger_path = tmp_path / "ledger.jsonl"

    gen._kis_market_data = _FakeKMD(
        indices={"0001": {"price": 3095.68, "open": 3099.52,
                           "change": -104.32, "change_pct": -3.26}},
        sectors=[],
    )
    r1 = asyncio.run(gen.evaluate_morning_brief(
        date.fromisoformat(record["kr_date"]), brief_path=brief_path,
        ledger_path=ledger_path, archive_dir=archive_dir,
    ))
    r2 = asyncio.run(gen.evaluate_morning_brief(
        date.fromisoformat(record["kr_date"]), brief_path=brief_path,
        ledger_path=ledger_path, archive_dir=archive_dir,
    ))
    lines = ledger_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert r1["date"] == r2["date"]
    summary = mbe.summarize(ledger_path)
    assert summary["samples"] == 1


# ── F20: 테마 식별자와 평가 대상 KIS 업종명이 연결되는가 ──────────────────────

def test_f20_mapped_theme_evaluates_unmapped_theme_excluded_with_reason():
    """생산자 키(AI/반도체·바이오) vs provider 키(전기전자·의약품 + 무관 업종)."""
    claims = dr.extract_brief_claims(
        "AI/반도체 테마와 바이오 테마, K-뷰티 테마가 모두 강세다.",
        {"AI/반도체": 1, "바이오": 1, "K-뷰티": 1}, "with_kr_inputs", basis=["미국 지수"],
    )
    by_theme = {s["theme"]: s for s in claims["sectors"]}
    assert by_theme["AI/반도체"]["eval_targets"] == ["전기전자"]
    assert by_theme["AI/반도체"]["supported"] is True
    assert by_theme["바이오"]["eval_targets"] == ["의약품"]
    assert by_theme["K-뷰티"]["supported"] is False
    assert by_theme["K-뷰티"]["eval_targets"] == []
    assert by_theme["K-뷰티"]["reason"] == "평가 대상 미합의"

    brief = {
        "kr_date": "2026-09-14", "generated_at": "2026-09-14T07:01:00",
        "scope": "with_kr_inputs", "claims": claims, "expert_consensus": None,
    }
    actual = {
        "date": "2026-09-14",
        "kospi": {"close_change_pct": -3.26},
        # "반도체"는 claims 가 요구하지 않는 무관 업종 — 매핑 안 됐으면 안 쓰인다
        "sectors": {"전기전자": -5.0, "의약품": 1.0, "반도체": 99.0},
    }
    result = mbe.evaluate(brief, actual)
    by_name = {s["name"]: s for s in result["sectors"]}

    assert by_name["AI/반도체"]["return_pct"] == pytest.approx(-5.0)
    assert by_name["AI/반도체"]["relative_pct"] == pytest.approx(-5.0 - (-3.26))
    assert by_name["AI/반도체"]["outperformed"] is False
    assert by_name["바이오"]["outperformed"] is True
    assert by_name["K-뷰티"]["outperformed"] is None
    assert by_name["K-뷰티"]["reason"] == "평가 대상 미합의"

    # summarize: 유효 표본만 세고 제외 사유별 건수를 함께 낸다
    ledger_path_holder = {}
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "ledger.jsonl"
        mbe.append_ledger(p, result)
        summary = mbe.summarize(p)
        assert summary["sectors"]["n"] == 2       # AI/반도체 + 바이오만 유효
        assert summary["sectors"]["hits"] == 1    # 바이오만 적중
        assert summary["sectors"]["excluded"] == {"평가 대상 미합의": 1}


# ── F21: kr_inputs 없이 한국 방향을 단정하지 않는가 ───────────────────────────

def test_f21_strong_us_up_without_kr_inputs_defers_kr_direction(monkeypatch):
    """avg_pct >= 1.5, kr_inputs 없음 → 발송 텍스트에 한국 방향 단정이 없어야
    한다(정상/차트동반/폴백 세 경로가 전부 같은 market_msg 변수를 참조하므로
    — daily_report.py generate_us_market_report — 반환된 report 텍스트 검증으로
    세 경로 모두를 대표한다)."""
    monkeypatch.setattr(dr, "get_telegram_notifier", lambda: SimpleNamespace())
    monkeypatch.setattr(dr, "get_screener", lambda: SimpleNamespace())
    monkeypatch.setattr(dr, "get_theme_detector", lambda: SimpleNamespace())
    monkeypatch.setattr(dr, "NewsCollector", lambda: SimpleNamespace())
    gen = dr.DailyReportGenerator()
    gen._us_market_data = _FakeUMD(US_QUOTES_STRONG_UP)

    report = asyncio.run(gen.generate_us_market_report(send_telegram=False))

    for banned in ("갭업 가능성", "갭상승", "갭 상승", "하방 압력"):
        assert banned not in report, banned
    assert "미국 지수 평균" in report and "국내 자료 없음" in report
    assert "판단 보류" in report


def test_f21_strong_us_up_with_kr_inputs_keeps_existing_wording(monkeypatch):
    """kr_inputs(as_of 포함)가 있으면 기존 관찰 포인트 문구를 허용한다 — 근거가
    있는 주장은 막지 않는다."""
    monkeypatch.setattr(dr, "get_telegram_notifier", lambda: SimpleNamespace())
    monkeypatch.setattr(dr, "get_screener", lambda: SimpleNamespace())
    monkeypatch.setattr(dr, "get_theme_detector", lambda: SimpleNamespace())
    monkeypatch.setattr(dr, "NewsCollector", lambda: SimpleNamespace())
    gen = dr.DailyReportGenerator()
    gen._us_market_data = _FakeUMD(US_QUOTES_STRONG_UP)

    kr_inputs = [{"name": "야간선물", "value": "+1.0%",
                  "as_of": datetime.now().isoformat(), "source": "kis"}]
    report = asyncio.run(gen.generate_us_market_report(
        send_telegram=False, kr_inputs=kr_inputs,
    ))
    assert "갭업 가능성" in report


# ── F22: 날짜별 원문·발송문 보존, 과거 재평가는 실측 재라벨링 금지 ────────────

def test_f22_two_generations_same_day_both_preserved_latest_cache_is_second(monkeypatch, tmp_path):
    gen = _make_gen(monkeypatch)
    brief_path = tmp_path / "llm_morning_brief.json"
    archive_dir = tmp_path / "morning_brief"

    _patch_llm(monkeypatch, _FakeLLM("<b>■ 시장 종합 평가</b>\n첫 번째 생성 버전이다.\n"))
    rec1 = asyncio.run(gen.build_morning_brief(
        quotes=US_QUOTES_FLAT, sector_signals={}, avg_pct=0.2,
        us_date_str=date.today().isoformat(), mood="보합",
    ))
    dr.save_morning_brief(rec1, brief_path, archive_dir=archive_dir)

    _patch_llm(monkeypatch, _FakeLLM("<b>■ 시장 종합 평가</b>\n두 번째(재생성) 버전이다.\n"))
    rec2 = asyncio.run(gen.build_morning_brief(
        quotes=US_QUOTES_FLAT, sector_signals={}, avg_pct=0.2,
        us_date_str=date.today().isoformat(), mood="보합",
    ))
    dr.save_morning_brief(rec2, brief_path, archive_dir=archive_dir)

    archive = json.loads((archive_dir / f"{rec2['kr_date']}.json").read_text(encoding="utf-8"))
    assert len(archive["generated"]) == 2
    assert "첫 번째" in archive["generated"][0]["text"]
    assert "두 번째" in archive["generated"][1]["text"]
    assert archive["generated"][0]["version"] == 1
    assert archive["generated"][1]["version"] == 2

    cache = json.loads(brief_path.read_text(encoding="utf-8"))
    assert "두 번째" in cache["text"], "최신 캐시는 2번째 생성본이어야 한다"


def test_f22_past_report_date_actuals_not_collected(monkeypatch, tmp_path):
    """report_date 가 과거이면(현재가 API라 그 시점을 재현 못하므로) 오늘 실측을
    그 날짜로 재라벨링하지 않는다 — 미수집 + 사유."""
    gen = _make_gen(monkeypatch)
    past = date.today() - timedelta(days=3)

    # 만에 하나 코드가 조회를 시도하면 여기서 걸린다 — 과거 날짜 가드는
    # _kis_market_data 를 건드리기 전에 반환해야 한다.
    class _ExplodingKMD:
        async def fetch_index_price(self, index_code="0001"):
            raise AssertionError("과거 날짜인데 실측 조회를 시도했다")

        async def fetch_sector_indices(self, market="K"):
            raise AssertionError("과거 날짜인데 업종 조회를 시도했다")

    gen._kis_market_data = _ExplodingKMD()
    actual = asyncio.run(gen._collect_brief_actuals(past))
    assert "kospi" not in actual
    assert "sectors" not in actual
    assert actual.get("note")


def test_f22_evaluate_morning_brief_past_date_reason_mentions_no_relabel(monkeypatch, tmp_path):
    gen = _make_gen(monkeypatch)
    past = date.today() - timedelta(days=3)
    brief = {
        "kr_date": past.isoformat(), "generated_at": f"{past.isoformat()}T07:01:00",
        "scope": "us_close_only", "text": "...", "model": "gpt-test",
        "claims": {"open_direction": None, "close_direction": None, "sectors": [], "basis": []},
        "expert_consensus": None,
    }
    brief_path = tmp_path / "llm_morning_brief.json"
    archive_dir = tmp_path / "morning_brief"
    dr.save_morning_brief(brief, brief_path, archive_dir=archive_dir)

    result = asyncio.run(gen.evaluate_morning_brief(
        past, brief_path=brief_path, ledger_path=tmp_path / "ledger.jsonl",
        archive_dir=archive_dir,
    ))
    assert result["evaluated"] is False
    assert "과거 날짜" in result["reason"]
