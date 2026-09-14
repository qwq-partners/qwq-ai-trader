"""모닝브리프 주장 범위 + 장전 전망 사후 평가 (2026-09-14 리뷰 후속 T9 요청 3·5 — F11)

대상: src/analytics/daily_report.py, src/analytics/morning_brief_eval.py
- 미국 마감 자료뿐이면 한국장 개장 방향·갭 단정 금지 (프롬프트 규칙 + 응답 후 검사)
- 국내 자료(as_of)가 있을 때만 "개장 관찰 포인트" + 반대 근거
- 브리프 JSON 스키마 고정: inputs / claims / scope / generated_at / model
- 사후 평가: 개장 방향 · 종가 방향 · 언급 업종 상대성과 · 전문가 종합 방향

LLM·시세는 mock, 캐시 경로는 tmp_path. 운영 캐시 파일 무접촉.
실행: venv/bin/python -m pytest tests/test_morning_brief.py -q -p no:cacheprovider
"""

import asyncio
import json
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.analytics import daily_report as dr  # noqa: E402
from src.analytics import morning_brief_eval as mbe  # noqa: E402


# ── 공통 fixture ─────────────────────────────────────────────────────────────

US_QUOTES = {
    "^GSPC": {"change_pct": 0.8, "price": 6100.0},
    "^IXIC": {"change_pct": 1.2, "price": 20100.0},
    "^SOX": {"change_pct": 2.4, "price": 5800.0},
    "^VIX": {"change_pct": -3.0, "price": 14.2},
}
SECTOR_SIGNALS = {"반도체": {"boost": 5, "us_avg_pct": 2.4, "top_movers": ["NVDA", "AMD"]}}

# 09-14 실제 브리프와 같은 형태 — 미국 자료만으로 한국장 개장을 단정한 문장 포함
OPTIMISTIC_TEXT = (
    "<b>■ 시장 종합 평가</b>\n"
    "미국 증시는 반도체 주도로 강세 마감했다.\n"
    "<b>■ 한국시장 시사점</b>\n"
    "오늘 KOSPI는 반도체 중심 상승 갭 출발 가능성이 높다. 시가 매수 대응을 권고한다.\n"
    "<b>■ 섹터 흐름</b>\n"
    "반도체 강세, 방어주 약세.\n"
)


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


@pytest.fixture
def gen(monkeypatch):
    """LLM 호출을 mock 한 DailyReportGenerator"""
    g = dr.DailyReportGenerator()
    return g


def _patch_llm(monkeypatch, llm):
    monkeypatch.setattr(dr, "get_llm_manager", lambda: llm, raising=False)
    import src.utils.llm as _llm_mod
    monkeypatch.setattr(_llm_mod, "get_llm_manager", lambda: llm)


def _brief(gen, monkeypatch, text, **kwargs):
    llm = _FakeLLM(text)
    _patch_llm(monkeypatch, llm)
    record = asyncio.run(gen.build_morning_brief(
        quotes=US_QUOTES, sector_signals=SECTOR_SIGNALS, avg_pct=1.4,
        us_date_str="2026-09-12", mood="강세", **kwargs,
    ))
    return record, llm


# ── 요청 3: 주장 범위 ─────────────────────────────────────────────────────────

def test_us_only_scope_strips_korean_open_direction_claims(gen, monkeypatch):
    record, llm = _brief(gen, monkeypatch, OPTIMISTIC_TEXT)

    assert record["scope"] == "us_close_only"
    assert "상승 갭 출발" not in record["text"]
    assert "갭 출발" not in record["text"]
    assert dr.NO_KR_INPUT_NOTE in record["text"]
    assert record["removed_claims"], "제거된 단정 문장이 기록돼야 한다"
    # 미국 마감 요약이라는 범위 표시
    assert "미국시장 마감 요약" in record["text"] or "미국시장 마감 요약" in record["title"]
    # 프롬프트에도 금지 규칙이 들어간다
    assert "개장 방향" in llm.prompts[0]


def test_us_only_scope_keeps_us_sentences(gen, monkeypatch):
    record, _ = _brief(gen, monkeypatch, OPTIMISTIC_TEXT)
    assert "미국 증시는 반도체 주도로 강세 마감했다." in record["text"]
    assert "반도체 강세, 방어주 약세." in record["text"]


def test_kr_inputs_allow_open_watchpoints_with_counter_evidence(gen, monkeypatch):
    kr_inputs = [
        {"name": "야간선물", "value": "+0.97%", "as_of": "2026-09-12T05:00:00", "source": "kis"},
        {"name": "원달러", "value": "1385.0", "as_of": "2026-09-12T06:30:00", "source": "kis"},
    ]
    record, llm = _brief(
        gen, monkeypatch,
        "<b>■ 개장 관찰 포인트</b>\n야간선물 +0.97%로 상승 출발 가능성. 다만 원화 약세는 반대 근거다.\n",
        kr_inputs=kr_inputs,
    )
    assert record["scope"] == "with_kr_inputs"
    assert "상승 출발" in record["text"]          # 국내 자료가 있으면 제거하지 않는다
    assert "반대 근거" in llm.prompts[0]
    assert [i["name"] for i in record["inputs"] if i.get("kind") == "kr"] == ["야간선물", "원달러"]
    assert all(i["as_of"] for i in record["inputs"] if i.get("kind") == "kr")


def test_kr_inputs_without_as_of_fall_back_to_us_only(gen, monkeypatch):
    """as_of 없는 국내 자료는 '유효한 자료'로 인정하지 않는다."""
    record, _ = _brief(
        gen, monkeypatch, OPTIMISTIC_TEXT,
        kr_inputs=[{"name": "야간선물", "value": "+0.97%", "as_of": None, "source": "kis"}],
    )
    assert record["scope"] == "us_close_only"
    assert "갭 출발" not in record["text"]


def test_expert_conflict_is_flagged(gen, monkeypatch):
    record, _ = _brief(
        gen, monkeypatch, OPTIMISTIC_TEXT,
        expert_consensus={"score": 2, "bias": "neutral"},
    )
    assert record["expert_conflict"] is not None
    assert "+2" in record["expert_conflict"]
    assert "중립" in record["expert_conflict"]
    assert record["expert_conflict"] in record["text"]


def test_no_expert_conflict_when_tone_matches(gen, monkeypatch):
    record, _ = _brief(
        gen, monkeypatch, OPTIMISTIC_TEXT,
        expert_consensus={"score": 20, "bias": "bull"},
    )
    assert record["expert_conflict"] is None


def test_brief_json_schema_is_fixed(gen, monkeypatch, tmp_path):
    record, _ = _brief(gen, monkeypatch, OPTIMISTIC_TEXT)
    path = tmp_path / "llm_morning_brief.json"
    dr.save_morning_brief(record, path)

    data = json.loads(path.read_text(encoding="utf-8"))
    for key in ("us_date", "text", "generated_at", "model", "scope", "inputs", "claims"):
        assert key in data, key
    assert isinstance(data["inputs"], list) and data["inputs"]
    assert set(data["claims"]) >= {"open_direction", "close_direction", "sectors", "basis"}
    # 미국 자료만이면 개장 방향 주장은 남지 않는다
    assert data["claims"]["open_direction"] is None
    assert data["claims"]["sectors"]


def test_llm_failure_returns_none(gen, monkeypatch):
    class _Fail(_FakeLLM):
        async def complete(self, prompt, **kwargs):
            resp = _FakeResp("")
            resp.success = False
            resp.error = "timeout"
            return resp

    _patch_llm(monkeypatch, _Fail(""))
    record = asyncio.run(gen.build_morning_brief(
        quotes=US_QUOTES, sector_signals=SECTOR_SIGNALS, avg_pct=1.4,
        us_date_str="2026-09-12", mood="강세",
    ))
    assert record is None


# ── 요청 3 보조: 순수 함수 ────────────────────────────────────────────────────

@pytest.mark.parametrize("sentence", [
    "오늘 KOSPI는 상승 갭 출발이 예상된다.",
    "코스피는 하락 출발할 것이다.",
    "국내 증시는 갭상승 출발이 유력하다.",
    "개장 직후 상승 출발 가능성이 높다.",
])
def test_sanitize_removes_open_direction_sentences(sentence):
    text, removed = dr.sanitize_brief_claims(sentence, scope="us_close_only")
    assert removed == [sentence]
    assert dr.NO_KR_INPUT_NOTE in text


@pytest.mark.parametrize("sentence", [
    "S&P500은 상승 마감했다.",
    "반도체 섹터가 강세를 보였다.",
    "미국 시장은 갭상승 출발 후 상승 마감했다.",  # 미국 문장은 제거하지 않는다
])
def test_sanitize_keeps_us_sentences(sentence):
    text, removed = dr.sanitize_brief_claims(sentence, scope="us_close_only")
    assert removed == []
    assert text == sentence


def test_sanitize_is_noop_with_kr_inputs():
    s = "오늘 KOSPI는 상승 갭 출발이 예상된다."
    text, removed = dr.sanitize_brief_claims(s, scope="with_kr_inputs")
    assert removed == []
    assert text == s


# ── 요청 5: 사후 평가 ─────────────────────────────────────────────────────────

BRIEF_0914 = {
    "us_date": "2026-09-12",
    "kr_date": "2026-09-14",
    "generated_at": "2026-09-14T07:01:00",
    "scope": "with_kr_inputs",
    "claims": {
        "open_direction": "up",
        "close_direction": "up",
        "sectors": ["반도체"],
        "basis": ["미국 지수", "빅테크"],
    },
    "expert_consensus": {"score": 2, "bias": "neutral"},
}

ACTUAL_0914 = {
    "date": "2026-09-14",
    "kospi": {"open_change_pct": -3.14, "close_change_pct": -3.26},
    "kosdaq": {"open_change_pct": -2.80, "close_change_pct": -3.01},
    "sectors": {"반도체": -4.20, "은행": -1.10},
}


def test_evaluate_0914_case_is_miss_on_open_and_close():
    result = mbe.evaluate(BRIEF_0914, ACTUAL_0914)
    assert result["evaluated"] is True
    assert result["open_direction"]["claimed"] == "up"
    assert result["open_direction"]["actual"] == "down"
    assert result["open_direction"]["hit"] is False
    assert result["open_direction"]["actual_pct"] == pytest.approx(-3.14)
    assert result["close_direction"]["hit"] is False
    assert result["close_direction"]["actual_pct"] == pytest.approx(-3.26)
    # 전문가 종합 +2 중립 → 실제 -3.26% 하락 → 방향 미적중
    assert result["expert_direction"]["claimed"] == "flat"
    assert result["expert_direction"]["hit"] is False
    # 언급 업종 상대성과: 반도체 -4.20% vs 시장 -3.26% → 언더퍼폼
    sector = result["sectors"][0]
    assert sector["name"] == "반도체"
    assert sector["relative_pct"] == pytest.approx(-0.94)
    assert sector["outperformed"] is False


def test_evaluate_hit_case():
    brief = json.loads(json.dumps(BRIEF_0914))
    brief["kr_date"] = "2026-09-15"
    actual = {
        "date": "2026-09-15",
        "kospi": {"open_change_pct": 1.2, "close_change_pct": 1.8},
        "sectors": {"반도체": 3.5},
    }
    result = mbe.evaluate(brief, actual)
    assert result["open_direction"]["hit"] is True
    assert result["close_direction"]["hit"] is True
    assert result["sectors"][0]["outperformed"] is True


def test_evaluate_missing_claim_is_none_not_false():
    brief = json.loads(json.dumps(BRIEF_0914))
    brief["claims"]["open_direction"] = None
    result = mbe.evaluate(brief, ACTUAL_0914)
    assert result["open_direction"]["claimed"] is None
    assert result["open_direction"]["hit"] is None
    assert result["open_direction"]["reason"]


def test_evaluate_missing_actual_is_not_evaluated():
    result = mbe.evaluate(BRIEF_0914, {"date": "2026-09-14"})
    assert result["evaluated"] is False
    assert result["reason"]
    assert result["open_direction"]["hit"] is None


def test_evaluate_flat_band():
    brief = json.loads(json.dumps(BRIEF_0914))
    brief["kr_date"] = "2026-09-15"
    brief["claims"]["open_direction"] = "flat"
    brief["claims"]["close_direction"] = "flat"
    actual = {"date": "2026-09-15", "kospi": {"open_change_pct": 0.1, "close_change_pct": -0.2}}
    result = mbe.evaluate(brief, actual)
    assert result["open_direction"]["hit"] is True
    assert result["close_direction"]["hit"] is True


# ── 요청 5: 원장 ─────────────────────────────────────────────────────────────

def test_append_ledger_and_summarize(tmp_path):
    path = tmp_path / "sub" / "morning_brief_eval.jsonl"
    mbe.append_ledger(path, mbe.evaluate(BRIEF_0914, ACTUAL_0914))
    hit_brief = json.loads(json.dumps(BRIEF_0914))
    hit_brief["kr_date"] = "2026-09-15"
    hit_actual = {"date": "2026-09-15",
                  "kospi": {"open_change_pct": 1.2, "close_change_pct": 1.8},
                  "sectors": {"반도체": 3.5}}
    mbe.append_ledger(path, mbe.evaluate(hit_brief, hit_actual))

    assert len(path.read_text(encoding="utf-8").strip().splitlines()) == 2

    summary = mbe.summarize(path)
    assert summary["samples"] == 2
    assert summary["open_direction"]["n"] == 2
    assert summary["open_direction"]["hits"] == 1
    assert summary["open_direction"]["hit_rate"] == pytest.approx(0.5)
    assert summary["close_direction"]["hits"] == 1
    assert "하루" in summary["note"]


def test_summarize_last_n(tmp_path):
    path = tmp_path / "morning_brief_eval.jsonl"
    mbe.append_ledger(path, mbe.evaluate(BRIEF_0914, ACTUAL_0914))
    next_day = json.loads(json.dumps(BRIEF_0914))
    next_day["kr_date"] = "2026-09-15"
    mbe.append_ledger(path, mbe.evaluate(
        next_day,
        {"date": "2026-09-15", "kospi": {"open_change_pct": 1.2, "close_change_pct": 1.8}},
    ))
    summary = mbe.summarize(path, last_n=1)
    assert summary["samples"] == 1
    assert summary["open_direction"]["hits"] == 1


def test_summarize_missing_file(tmp_path):
    summary = mbe.summarize(tmp_path / "none.jsonl")
    assert summary["samples"] == 0
    assert summary["open_direction"]["hit_rate"] is None


# ── 요청 5: DailyReportGenerator 훅 ───────────────────────────────────────────

class _FakeKMD:
    def __init__(self, indices, sectors):
        self._indices = indices
        self._sectors = sectors
        self.calls = []

    async def fetch_index_price(self, index_code="0001"):
        self.calls.append(index_code)
        return self._indices.get(index_code)

    async def fetch_sector_indices(self, market="K"):
        return self._sectors


def _write_brief(path: Path, brief: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")


def test_evaluate_morning_brief_writes_ledger(tmp_path):
    brief_path = tmp_path / "llm_morning_brief.json"
    ledger_path = tmp_path / "morning_brief_eval.jsonl"
    _write_brief(brief_path, BRIEF_0914)

    # KOSPI 전일종가 3200 → 시가 3099.5(-3.14%), 종가 3095.7(-3.26%)
    kmd = _FakeKMD(
        indices={
            "0001": {"label": "KOSPI", "price": 3095.68, "open": 3099.52,
                     "change": -104.32, "change_pct": -3.26},
            "1001": {"label": "KOSDAQ", "price": 780.0, "open": 782.0,
                     "change": -24.2, "change_pct": -3.01},
        },
        sectors=[{"name": "반도체", "change_pct": -4.20}, {"name": "은행", "change_pct": -1.10}],
    )
    g = dr.DailyReportGenerator(kis_market_data=kmd)
    record = asyncio.run(g.evaluate_morning_brief(
        date(2026, 9, 14), brief_path=brief_path, ledger_path=ledger_path))

    assert record["evaluated"] is True
    assert record["open_direction"]["hit"] is False
    assert record["close_direction"]["hit"] is False
    assert record["open_direction"]["actual_pct"] == pytest.approx(-3.14, abs=0.02)
    assert ledger_path.exists()
    assert json.loads(ledger_path.read_text(encoding="utf-8").strip())["date"] == "2026-09-14"


def test_evaluate_morning_brief_records_failure_when_index_missing(tmp_path):
    brief_path = tmp_path / "llm_morning_brief.json"
    ledger_path = tmp_path / "morning_brief_eval.jsonl"
    _write_brief(brief_path, BRIEF_0914)

    kmd = _FakeKMD(indices={}, sectors=[])
    g = dr.DailyReportGenerator(kis_market_data=kmd)
    record = asyncio.run(g.evaluate_morning_brief(
        date(2026, 9, 14), brief_path=brief_path, ledger_path=ledger_path))

    assert record["evaluated"] is False
    assert record["reason"]
    assert ledger_path.exists()


def test_evaluate_morning_brief_without_brief_returns_none(tmp_path):
    g = dr.DailyReportGenerator(kis_market_data=_FakeKMD({}, []))
    record = asyncio.run(g.evaluate_morning_brief(
        date(2026, 9, 14),
        brief_path=tmp_path / "absent.json",
        ledger_path=tmp_path / "ledger.jsonl",
    ))
    assert record is None
    assert not (tmp_path / "ledger.jsonl").exists()


def test_evaluate_summary_line(tmp_path):
    ledger_path = tmp_path / "morning_brief_eval.jsonl"
    mbe.append_ledger(ledger_path, mbe.evaluate(BRIEF_0914, ACTUAL_0914))
    line = mbe.summary_line(ledger_path)
    assert "개장" in line and "종가" in line
    assert "1건" in line or "n=1" in line


# ── B 리뷰 반영: KR 주어 가드 · 소수점 문장 분리 · 기준일 대조 ──────────────────

@pytest.mark.parametrize("sentence", [
    "미국 반도체 랠리를 반영해 오늘 KOSPI는 갭상승 출발이 예상된다.",
    "미국 증시 호조로 코스피 상승 출발 가능성이 높다.",
    "S&P500 강세를 이어받아 국내 증시는 갭상승 출발할 전망이다.",
])
def test_sanitize_removes_kr_claims_even_with_us_evidence(sentence):
    """미국 근거 + 국내 결론이 한 문장에 있어도 국내 단정은 제거한다."""
    text, removed = dr.sanitize_brief_claims(sentence, scope="us_close_only")
    assert removed == [sentence]
    assert dr.NO_KR_INPUT_NOTE in text


def test_sanitize_does_not_split_decimals():
    """소수점에서 문장이 쪼개져 본문이 훼손되면 안 된다."""
    text = "S&P500 +0.8% 로 마감했다."
    out, removed = dr.sanitize_brief_claims(text, scope="us_close_only")
    assert removed == []
    assert out == text


def test_sanitize_decimal_does_not_detach_claim_from_kr_subject():
    """소수점 분리 때문에 KR 주어와 단정이 서로 다른 '문장'으로 쪼개지면 안 된다."""
    src = "코스피는 S&P500 +0.8% 를 반영해 갭상승 출발이 예상된다."
    out, removed = dr.sanitize_brief_claims(src, scope="us_close_only")
    assert removed == [src]
    assert "+0" not in out and "S&P500" not in out
    assert out.strip() == dr.NO_KR_INPUT_NOTE


def test_sanitize_keeps_us_sentence_intact_while_removing_kr_claim():
    src = (
        "S&P500 +0.8% 로 마감했다. "
        "이를 반영해 오늘 KOSPI는 갭상승 출발이 예상된다. "
        "나스닥은 +1.25% 상승했다."
    )
    out, removed = dr.sanitize_brief_claims(src, scope="us_close_only")
    assert len(removed) == 1
    assert "오늘 KOSPI는 갭상승 출발" in removed[0]
    assert "S&P500 +0.8% 로 마감했다." in out
    assert "나스닥은 +1.25% 상승했다." in out
    assert dr.NO_KR_INPUT_NOTE in out


def test_extract_claims_ignores_us_close_sentences():
    """미국 마감 서술을 KOSPI 주장으로 원장에 기록하지 않는다."""
    claims = dr.extract_brief_claims(
        "S&P500은 상승 마감했다. 미국 증시는 갭상승 출발 후 상승 마감했다.",
        {"반도체": 1}, "with_kr_inputs", basis=["미국 지수"],
    )
    assert claims["open_direction"] is None
    assert claims["close_direction"] is None


def test_extract_claims_reads_kr_subject_sentences():
    claims = dr.extract_brief_claims(
        "S&P500은 상승 마감했다. 오늘 코스피는 상승 출발 후 상승 마감할 전망이다.",
        {"반도체": 1}, "with_kr_inputs", basis=["야간선물"],
    )
    assert claims["open_direction"] == "up"
    assert claims["close_direction"] == "up"


def test_brief_record_has_kr_date(gen, monkeypatch):
    record, _ = _brief(gen, monkeypatch, OPTIMISTIC_TEXT)
    assert record["kr_date"] == date.today().isoformat()


def test_evaluate_skips_when_brief_is_from_another_day():
    """전날 브리프를 오늘 실측과 대조하지 않는다."""
    brief = json.loads(json.dumps(BRIEF_0914))
    brief["kr_date"] = "2026-09-11"
    result = mbe.evaluate(brief, ACTUAL_0914)
    assert result["evaluated"] is False
    assert "2026-09-11" in result["reason"]
    for axis in ("open_direction", "close_direction", "expert_direction"):
        assert result[axis]["hit"] is None


def test_evaluate_morning_brief_stale_cache_is_not_scored(tmp_path):
    brief_path = tmp_path / "llm_morning_brief.json"
    ledger_path = tmp_path / "morning_brief_eval.jsonl"
    stale = json.loads(json.dumps(BRIEF_0914))
    stale["kr_date"] = "2026-09-11"
    stale["generated_at"] = "2026-09-11T07:01:00"
    _write_brief(brief_path, stale)

    kmd = _FakeKMD(
        indices={"0001": {"label": "KOSPI", "price": 3095.68, "open": 3099.52,
                          "change": -104.32, "change_pct": -3.26}},
        sectors=[],
    )
    g = dr.DailyReportGenerator(kis_market_data=kmd)
    record = asyncio.run(g.evaluate_morning_brief(
        date(2026, 9, 14), brief_path=brief_path, ledger_path=ledger_path))

    assert record["evaluated"] is False
    assert record["close_direction"]["hit"] is None
    assert "2026-09-11" in record["reason"]


def test_expert_conflict_note_accepts_brief_text():
    """07:30 결합부가 레코드 tone 또는 본문 어느 쪽을 넘겨도 동작한다."""
    note_from_tone = dr.build_expert_conflict_note("bull", {"score": 2})
    note_from_text = dr.build_expert_conflict_note(OPTIMISTIC_TEXT, {"score": 2})
    assert note_from_tone and note_from_text
    assert note_from_text == dr.build_expert_conflict_note(
        dr.brief_tone(OPTIMISTIC_TEXT), {"score": 2})
    assert dr.build_expert_conflict_note("bull", {"bias": "bear"}) is not None
    assert dr.build_expert_conflict_note("bull", None) is None
