"""T10 담당 B — 자료 유효성·신선도·전문가 집계 인수 테스트 (F16/F17/F18, 2026-09-15)

docs/superpowers/plans/2026-09-13-review-remediation.md의 "## T10" 절 F16~F18 인수
사례를 고정한다. 네트워크·DB·운영 캐시(~/.cache/ai_trader) 접근 없음 — 전부
monkeypatch/tmp_path 또는 순수 함수 호출.

실행: venv/bin/python -m pytest tests/test_t10_data_validity.py -q
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.experts.global_micro_expert import GlobalMicroExpert  # noqa: E402
from src.experts.kr_economy_expert import KREconomyExpert  # noqa: E402
from src.experts.kr_market_expert import KRMarketExpert  # noqa: E402
from src.experts.macro_economist import MacroEconomist  # noqa: E402
from src.experts.orchestrator import ExpertOrchestrator  # noqa: E402
from src.experts.types import ExpertConfig, ExpertOpinion, RegimeBias  # noqa: E402
from src.experts.us_market_expert import USMarketExpert  # noqa: E402
from src.experts.weekend_signal_expert import WeekendSignalExpert  # noqa: E402


def _opinion(expert: str, score: int, confidence: float, data_status: str = "ok") -> ExpertOpinion:
    return ExpertOpinion(
        expert=expert, score=score,
        regime_bias=RegimeBias.BULL if score > 0 else RegimeBias.NEUTRAL,
        confidence=confidence, data_status=data_status,
    )


# ─────────────────────────────────────────────────────────────────
# F16 — 무자료 전문가의 coverage 충족 방지
# ─────────────────────────────────────────────────────────────────
def test_macro_economist_fully_empty_is_insufficient_and_excluded(monkeypatch):
    """실제 MacroEconomist._analyze를 완전 무자료(빈 fetch·빈 컨텍스트·빈 오버라이드)로
    실행한 의견 + 유효 3명(score 40, confidence 0.8, 동일 가중치)이면 valid_n=3·
    insufficient_coverage=True·aggregate_regime_score=0(무자료 의견의 +보정 없음).
    4번째 정상 전문가를 추가하면 valid_n=4로 커버리지를 충족하고 무보정이 풀린다."""
    macro = MacroEconomist(ExpertConfig(), llm_manager=None, perplexity_key="")

    async def _empty_prices():
        return {}

    async def _empty_semis():
        return {}

    async def _empty_ctx():
        return ""

    monkeypatch.setattr(macro, "_fetch_yfinance_indicators", _empty_prices)
    monkeypatch.setattr(macro, "_fetch_semis_basket_5d", _empty_semis)
    monkeypatch.setattr(macro, "_fetch_macro_context", _empty_ctx)
    monkeypatch.setattr(macro, "_load_manual_overrides", lambda: {})

    macro_op = asyncio.run(macro._analyze())
    macro_op.expert = "macro_economist"

    assert macro_op.data_status == "insufficient"
    assert macro_op.score == 0

    orch = ExpertOrchestrator(ExpertConfig())
    three_valid = {
        "macro_economist": macro_op,
        "kr_market_expert": _opinion("kr_market_expert", 40, 0.8),
        "us_market_expert": _opinion("us_market_expert", 40, 0.8),
        "kr_economy_expert": _opinion("kr_economy_expert", 40, 0.8),
    }
    summary = orch.data_status_summary(three_valid)
    assert summary["valid_n"] == 3
    assert summary["insufficient_coverage"] is True
    assert orch.aggregate_regime_score(three_valid) == 0

    four_valid = dict(three_valid)
    four_valid["global_micro_expert"] = _opinion("global_micro_expert", 40, 0.8)
    summary4 = orch.data_status_summary(four_valid)
    assert summary4["valid_n"] == 4
    assert summary4["insufficient_coverage"] is False
    # 커버리지 충족 후에는 무자료 macro가 빠진 4명 전부 score=40 균일 →
    # 가중평균도 40, ±30 스케일 규칙(avg*0.3, 클램프) 그대로 적용됨을 직접 계산해 검증.
    expected = int(max(-30, min(30, 40 * 0.3)))
    assert orch.aggregate_regime_score(four_valid) == expected
    assert expected != 0  # 무보정(0)에서 벗어났다는 사실 자체가 핵심


def test_us_market_expert_fully_empty_is_insufficient(monkeypatch):
    expert = USMarketExpert(ExpertConfig(), llm_manager=None, perplexity_key="")

    async def _empty_dict():
        return {}

    async def _empty_vix():
        return None

    monkeypatch.setattr(expert, "_fetch_index_states", _empty_dict)
    monkeypatch.setattr(expert, "_fetch_sector_rs", _empty_dict)
    monkeypatch.setattr(expert, "_fetch_vix", _empty_vix)
    monkeypatch.setattr(expert, "_fetch_earnings_season", _empty_dict)
    monkeypatch.setattr(expert, "_fetch_semis_state", _empty_dict)

    op = asyncio.run(expert._analyze())
    assert op.data_status == "insufficient"
    assert op.score == 0


def test_us_market_expert_core_three_present_sectors_missing_is_partial_not_ok(monkeypatch):
    """T10 B 리뷰 advisory — SPY/VIX/SOX(core 3종)가 전부 있어도 섹터 RS가 없으면
    ok가 아니라 partial이어야 한다. 섹터 RS는 XLE/XLF 강세 시 +5로 score를 실제로
    움직이는 입력이라(macro_economist의 cpi_yoy/semis_basket과 동일 성격) 판정
    기준에서 빠지면 안 된다."""
    expert = USMarketExpert(ExpertConfig(), llm_manager=None, perplexity_key="")

    async def _indices():
        return {"SPY": {"vs_ma50_pct": 3.0, "vs_ma200_pct": 5.0}}

    async def _empty_dict():
        return {}

    async def _vix():
        return 14.0

    async def _semis():
        return {"sox_5d_pct": 1.0}

    monkeypatch.setattr(expert, "_fetch_index_states", _indices)
    monkeypatch.setattr(expert, "_fetch_sector_rs", _empty_dict)  # 섹터만 결측
    monkeypatch.setattr(expert, "_fetch_vix", _vix)
    monkeypatch.setattr(expert, "_fetch_earnings_season", _empty_dict)
    monkeypatch.setattr(expert, "_fetch_semis_state", _semis)

    op = asyncio.run(expert._analyze())
    assert op.data_status == "partial"
    assert "섹터 RS" in op.missing_inputs
    assert op.score != 0  # core 3종은 정상 — score는 실제로 움직였다


def test_kr_economy_expert_fully_empty_is_insufficient(monkeypatch):
    expert = KREconomyExpert(ExpertConfig(), llm_manager=None, perplexity_key="")

    async def _empty_text(*a, **kw):
        return ""

    async def _empty_dict():
        return {}

    monkeypatch.setattr(expert, "_perplexity_search", _empty_text)
    monkeypatch.setattr(expert, "_fetch_krw_state", _empty_dict)

    op = asyncio.run(expert._analyze())
    assert op.data_status == "insufficient"
    assert op.score == 0


def test_global_micro_expert_fully_empty_is_insufficient(monkeypatch):
    expert = GlobalMicroExpert(ExpertConfig(), llm_manager=None, perplexity_key="")

    async def _empty_text(*a, **kw):
        return ""

    async def _empty_dict():
        return {}

    monkeypatch.setattr(expert, "_perplexity_search", _empty_text)
    monkeypatch.setattr(expert, "_fetch_sector_etf_returns", _empty_dict)

    op = asyncio.run(expert._analyze())
    assert op.data_status == "insufficient"
    assert op.score == 0


def test_weekend_signal_expert_fully_empty_is_insufficient(monkeypatch):
    expert = WeekendSignalExpert(ExpertConfig(), llm_manager=None, perplexity_key="")

    async def _empty_signals():
        return {}

    monkeypatch.setattr(expert, "_fetch_all_signals", _empty_signals)

    op = asyncio.run(expert._analyze())
    assert op.data_status == "insufficient"
    assert op.score == 0


def test_from_dict_missing_data_status_key_is_unknown_and_excluded():
    """T9 이전(이 필드가 없던) 저장 레코드를 from_dict로 읽으면 "ok"로 가장하지
    않고 "unknown"이 되어 orchestrator 집계에서 자연히 제외된다(F16)."""
    legacy = {
        "expert": "macro_economist", "score": 90, "regime_bias": "bull",
        "confidence": 0.9,
        # data_status 키 자체가 없음 — T9 이전 레코드 시뮬레이션
    }
    op = ExpertOpinion.from_dict(legacy)
    assert op.data_status == "unknown"

    orch = ExpertOrchestrator(ExpertConfig())
    opinions = {
        "macro_economist": op,
        "kr_market_expert": _opinion("kr_market_expert", 5, 0.6),
        "us_market_expert": _opinion("us_market_expert", 5, 0.6),
        "kr_economy_expert": _opinion("kr_economy_expert", 5, 0.6),
    }
    # unknown이 집계에 끼어들었다면 score=90 때문에 valid_n=4가 되고 보정도 커졌을 것.
    summary = orch.data_status_summary(opinions)
    assert summary["valid_n"] == 3
    assert orch.aggregate_regime_score(opinions) == 0  # valid_n=3 < MIN_VALID_EXPERTS(4)
    # T10 B 리뷰 advisory: "자료 부족 N명" 표시가 insufficient뿐 아니라 unknown도
    # 세야 valid_n(3)이 낮은데 note가 "0명"으로 보이는 표시 불일치가 안 생긴다.
    assert summary["note"] == "자료 부족 1명"
    assert summary["unknown_experts"] == ["macro_economist"]
    assert summary["insufficient_experts"] == []


# ─────────────────────────────────────────────────────────────────
# F17 — 야간선물 as_of 신선도 게이트 (weekend_signal_expert / kr_market_expert)
# ─────────────────────────────────────────────────────────────────
def test_weekend_expert_kr_futures_as_of_none_is_not_counted(monkeypatch):
    """야간선물 +2%, as_of=None, value_unchanged_minutes=570 — 점수 미가산·
    유효 신호 미포함(partial), "ok"로 잡히지 않는다."""
    _base_signals = {
        "es_pct": 0.1, "nq_pct": 0.1,
        "krw_pct": 0.1, "krw_last": 1350.0, "vix_last": 16.0,
        "btc_pct": 0.5, "zb_pct": 0.1,
    }

    expert = WeekendSignalExpert(ExpertConfig(), llm_manager=None, perplexity_key="")

    async def _signals_with_kr():
        return dict(
            _base_signals,
            kr_futures_pct=2.0, kr_futures_source="KIS:TEST",
            kr_futures_as_of=None, kr_futures_as_of_ttl_seconds=None,
            kr_futures_unchanged_minutes=570,
        )

    monkeypatch.setattr(expert, "_fetch_all_signals", _signals_with_kr)
    op = asyncio.run(expert._analyze())

    assert op.data_status == "partial"
    assert any("야간선물" in m for m in op.missing_inputs)

    # kr(+2.0%)이 반영됐다면 kr>=1.5 규칙으로 score에 +12가 더해졌을 것 — 그 기여가 없어야
    # 하므로, kr_futures 키 자체가 없는(NKD 프록시 미작동) 케이스와 점수가 같아야 한다.
    other_only = WeekendSignalExpert(ExpertConfig(), llm_manager=None, perplexity_key="")

    async def _signals_no_kr():
        return dict(_base_signals)

    monkeypatch.setattr(other_only, "_fetch_all_signals", _signals_no_kr)
    op_without_kr = asyncio.run(other_only._analyze())
    assert op.score == op_without_kr.score  # kr as_of=None → score에 기여 0, 있으나 없으나 동일


def test_weekend_expert_kr_futures_fresh_as_of_applies_existing_rule(monkeypatch):
    """세션 개장 중 관측(as_of=now)·유효기간 내 +2% → 기존 kr>=1.5 규칙(+12) 그대로."""
    expert = WeekendSignalExpert(ExpertConfig(), llm_manager=None, perplexity_key="")
    now = datetime.now()

    async def _signals():
        return {
            "kr_futures_pct": 2.0, "kr_futures_source": "KIS:TEST",
            "kr_futures_as_of": now.isoformat(),
            "kr_futures_as_of_ttl_seconds": 3600,
            "kr_futures_unchanged_minutes": 0.0,
        }

    monkeypatch.setattr(expert, "_fetch_all_signals", _signals)
    op = asyncio.run(expert._analyze())

    assert op.score == 12
    assert any("KR/JP 야간선물" in f and "갭업" in f for f in op.key_findings)


def test_weekend_expert_kr_futures_expired_as_of_is_not_counted(monkeypatch):
    """as_of는 있지만 ttl을 넘겨 만료된 자료는 유효 신호로 세지 않는다."""
    expert = WeekendSignalExpert(ExpertConfig(), llm_manager=None, perplexity_key="")
    stale_as_of = datetime.now() - timedelta(hours=10)

    async def _signals():
        return {
            "kr_futures_pct": 2.0, "kr_futures_source": "KIS:TEST",
            "kr_futures_as_of": stale_as_of.isoformat(),
            "kr_futures_as_of_ttl_seconds": 3600,  # 1시간 유효 — 10시간 전은 만료
        }

    monkeypatch.setattr(expert, "_fetch_all_signals", _signals)
    op = asyncio.run(expert._analyze())

    assert op.score == 0
    assert any("만료" in m or "as_of" in m for m in op.missing_inputs)


def test_weekend_expert_kr_futures_future_as_of_is_not_counted(monkeypatch):
    """미래 시각 as_of(시계 역전/오염 데이터)도 유효 신호로 세지 않는다."""
    expert = WeekendSignalExpert(ExpertConfig(), llm_manager=None, perplexity_key="")
    future_as_of = datetime.now() + timedelta(hours=2)

    async def _signals():
        return {
            "kr_futures_pct": 2.0, "kr_futures_source": "KIS:TEST",
            "kr_futures_as_of": future_as_of.isoformat(),
            "kr_futures_as_of_ttl_seconds": 3600,
        }

    monkeypatch.setattr(expert, "_fetch_all_signals", _signals)
    op = asyncio.run(expert._analyze())
    assert op.score == 0


def test_weekend_expert_zero_pct_is_valid_signal_not_missing(monkeypatch):
    """결측(None)과 실제 0% 변동은 구분 — 0%는 유효 신호로 카운트(가산은 0)."""
    expert = WeekendSignalExpert(ExpertConfig(), llm_manager=None, perplexity_key="")
    now = datetime.now()

    async def _signals():
        return {
            "kr_futures_pct": 0.0, "kr_futures_source": "KIS:TEST",
            "kr_futures_as_of": now.isoformat(),
            "kr_futures_as_of_ttl_seconds": 3600,
        }

    monkeypatch.setattr(expert, "_fetch_all_signals", _signals)
    op = asyncio.run(expert._analyze())
    assert not any("KR/JP" in m for m in op.missing_inputs)  # 유효 신호로 잡힘(0%는 결측이 아님)


def test_kr_market_expert_futures_stale_as_of_not_counted(monkeypatch):
    """kr_market_expert도 동일 게이트 — as_of 만료 시 nf_chg 규칙 미적용."""
    expert = KRMarketExpert(ExpertConfig(), llm_manager=None, perplexity_key="")

    async def _empty(*a, **kw):
        return {}

    async def _futures():
        stale = datetime.now() - timedelta(hours=10)
        return {
            "overnight_chg_pct": -2.5, "source": "KIS:TEST",
            "as_of": stale.isoformat(), "as_of_ttl_seconds": 3600,
        }

    monkeypatch.setattr(expert, "_fetch_investor_flows", _empty)
    monkeypatch.setattr(expert, "_fetch_short_balance", _empty)
    monkeypatch.setattr(expert, "_fetch_kospi_state", _empty)
    monkeypatch.setattr(expert, "_fetch_kospi200_futures", _futures)

    op = asyncio.run(expert._analyze())
    # 야간선물(-2.5%, 갭다운 위험 -18)이 반영됐다면 score<=-18이었을 것 — 만료라 미반영.
    assert not any(f.startswith("⚠️ KOSPI200 야간선물") for f in op.key_findings)
    assert any("야간선물" in m for m in op.missing_inputs)


def test_kr_market_expert_futures_fresh_as_of_applies_rule(monkeypatch):
    expert = KRMarketExpert(ExpertConfig(), llm_manager=None, perplexity_key="")
    now = datetime.now()

    async def _empty(*a, **kw):
        return {}

    async def _futures():
        return {
            "overnight_chg_pct": -2.5, "source": "KIS:TEST",
            "as_of": now.isoformat(), "as_of_ttl_seconds": 3600,
        }

    monkeypatch.setattr(expert, "_fetch_investor_flows", _empty)
    monkeypatch.setattr(expert, "_fetch_short_balance", _empty)
    monkeypatch.setattr(expert, "_fetch_kospi_state", _empty)
    monkeypatch.setattr(expert, "_fetch_kospi200_futures", _futures)

    op = asyncio.run(expert._analyze())
    assert any(f.startswith("⚠️ KOSPI200 야간선물") for f in op.key_findings)


# ─────────────────────────────────────────────────────────────────
# F18 — us_market_data 공급자 부분 응답의 0 변환 제거
# ─────────────────────────────────────────────────────────────────
class _FakeResp:
    def __init__(self, status: int, data: dict):
        self.status = status
        self._data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._data


class _FakeSession:
    def __init__(self, responder):
        self._responder = responder

    def get(self, url, params=None, headers=None, timeout=None):
        status, data = self._responder(url, params)
        return _FakeResp(status, data)

    async def close(self):
        pass


def test_clean_num_rejects_non_numeric_and_keeps_zero():
    from src.data.providers.us_market_data import _clean_num

    assert _clean_num(None) is None
    assert _clean_num("1.5") is None       # 문자열은 결측 취급 (정책 — 명시)
    assert _clean_num(float("nan")) is None
    assert _clean_num(float("inf")) is None
    assert _clean_num(True) is None        # bool은 int 서브클래스라 별도 차단
    assert _clean_num(0.0) == 0.0           # 정상 0.0 변동은 유지
    assert _clean_num(-1.23) == -1.23


def test_symbol_present_but_price_field_missing_is_none_not_zero():
    """F18 재현: {"symbol":"^VIX","regularMarketTime":...}만 오면 VIX price/change_pct
    가 0이 아니라 None + missing=True + missing_fields로 어느 필드가 빠졌는지 남는다."""
    from src.data.providers.us_market_data import USMarketData

    umd = USMarketData()

    def _responder(url, params):
        quotes = [
            {"symbol": "^GSPC", "regularMarketPrice": 6500.0,
             "regularMarketChange": 78.0, "regularMarketChangePercent": 1.2,
             "regularMarketTime": 1757900000},
            {"symbol": "^VIX", "regularMarketTime": 1757900000},  # 가격 필드 전부 없음
        ]
        return 200, {"quoteResponse": {"result": quotes}}

    async def fake_get_session():
        return _FakeSession(_responder)

    umd._get_session = fake_get_session  # type: ignore[assignment]

    signal = asyncio.run(umd.get_overnight_signal())  # 예외 없이 반환돼야 함(summary 포함)

    vix = signal["indices_normalized"]["VIX"]
    assert vix["price"] is None
    assert vix["change_pct"] is None
    assert vix["missing"] is True
    assert "price" in vix["missing_fields"] and "change_pct" in vix["missing_fields"]
    assert vix["reason"]

    assert signal["indices_normalized"]["SP500"]["price"] == 6500.0
    assert isinstance(signal["summary"], str) and signal["summary"]


def test_partial_field_missing_soxs_price_present_change_pct_nan():
    """일부 필드만 결측(NaN) — price는 유효, change_pct는 NaN → change_pct만 None.

    price가 유효하면 indices_normalized는 missing=False로 보존한다(kr_scheduler._norm이
    entry.get("missing")를 먼저 게이트로 보므로, 여기서 missing=True로 전부 버리면
    VIX처럼 price만 필요한 소비자까지 값을 잃는다) — missing_fields로 결측 필드만
    알린다. 단 quotes(self._cache) 자체에는 ^SOX를 넣지 않는다 — 2026-09-15 T10 F18
    2차 blocking 재현: 1차 수정은 "완전 결측"만 quotes에서 제외했는데, 이 "부분
    결측"(price만 유효) 심볼은 그대로 quotes에 None을 담아 daily_report.py의
    `pct = q["change_pct"]; pct > 0` 같은 가드 없는 소비 패턴에서 TypeError를 냈다."""
    from src.data.providers.us_market_data import USMarketData, INDEX_SYMBOLS

    umd = USMarketData()

    def _responder(url, params):
        quotes = [
            {"symbol": "^SOX", "regularMarketPrice": 5200.0,
             "regularMarketChange": float("nan"),
             "regularMarketChangePercent": float("nan"),
             "regularMarketTime": 1757900000},
        ]
        return 200, {"quoteResponse": {"result": quotes}}

    async def fake_get_session():
        return _FakeSession(_responder)

    umd._get_session = fake_get_session  # type: ignore[assignment]

    quotes = asyncio.run(umd.fetch_us_market_summary(force_refresh=True))
    # 부분 결측 심볼도 완전 결측과 동일하게 quotes(self._cache)에서 빠진다
    # (fetch_sp500_stocks의 "price is None or change_pct is None" 제외 기준과 일치).
    assert "^SOX" not in quotes

    # daily_report.generate_us_market_report(1046~1060)의 실제 패턴 그대로 재현 —
    # 가드 없이 q["change_pct"]에 접근해도 예외 없이 끝까지 돌아야 한다.
    rendered = []
    for sym in INDEX_SYMBOLS:
        q = quotes.get(sym)
        if not q:
            continue
        pct = q["change_pct"]
        rendered.append((sym, pct > 0))
    assert rendered == []  # ^SOX가 유일한 심볼이었고 quotes에서 빠졌으므로 아무것도 렌더 안 됨

    signal = asyncio.run(umd.get_overnight_signal())
    sox = signal["indices_normalized"]["SOX"]
    # quotes에서는 빠졌지만 indices_normalized는 seen_missing에 보존된 값으로
    # price를 살려낸다(missing=False + missing_fields).
    assert sox["price"] == 5200.0
    assert sox["change_pct"] is None
    assert sox["missing"] is False
    assert sox["missing_fields"] == ["change_pct"]
    # change_pct가 없으므로 idx_pcts/레거시 indices(표시명) 딕셔너리에는 빠져야 한다
    # (round() 예외 방지 + 심리 평균에 결측을 0으로 섞지 않기 위함)
    assert "반도체(SOX)" not in signal["indices"]
    assert isinstance(signal["summary"], str) and signal["summary"]


def test_normal_zero_pct_change_is_not_missing():
    """정상적인 0.0% 변동(무변동)은 결측으로 오판되면 안 된다."""
    from src.data.providers.us_market_data import USMarketData

    umd = USMarketData()

    def _responder(url, params):
        quotes = [
            {"symbol": "^DJI", "regularMarketPrice": 41000.0,
             "regularMarketChange": 0.0, "regularMarketChangePercent": 0.0,
             "regularMarketTime": 1757900000},
        ]
        return 200, {"quoteResponse": {"result": quotes}}

    async def fake_get_session():
        return _FakeSession(_responder)

    umd._get_session = fake_get_session  # type: ignore[assignment]

    signal = asyncio.run(umd.get_overnight_signal())
    dow = signal["indices_normalized"]["DOW"]
    assert dow["missing"] is False
    assert dow["change_pct"] == 0.0
    assert signal["indices"]["다우"]["change_pct"] == 0.0


def test_fully_missing_symbol_excluded_from_quotes_but_reason_preserved():
    """F16 리뷰 blocking(B 파일): 응답에 심볼은 있어도 price/change_pct가 둘 다
    결측이면 quotes(self._cache)에서 아예 빠져야 한다 — daily_report.py/
    us_market_chart.py처럼 `q = quotes.get(sym); if not q: continue` 뒤
    `q["change_pct"]`를 가드 없이 바로 쓰는 기존 소비자가 None에서 TypeError를
    내지 않게 하기 위해서다(fetch_sp500_stocks의 '조회 실패 심볼은 누락' 계약과
    동일). 그래도 indices_normalized의 reason/missing_fields는 그대로 남아야 한다
    (self._seen_missing로 별도 보존)."""
    from src.data.providers.us_market_data import USMarketData, INDEX_SYMBOLS

    umd = USMarketData()

    def _responder(url, params):
        quotes = [
            {"symbol": "^GSPC", "regularMarketPrice": 6500.0,
             "regularMarketChange": 78.0, "regularMarketChangePercent": 1.2,
             "regularMarketTime": 1757900000},
            {"symbol": "^DJI", "regularMarketTime": 1757900000},  # 가격 필드 전부 없음
        ]
        return 200, {"quoteResponse": {"result": quotes}}

    async def fake_get_session():
        return _FakeSession(_responder)

    umd._get_session = fake_get_session  # type: ignore[assignment]

    quotes = asyncio.run(umd.fetch_us_market_summary(force_refresh=True))
    assert "^DJI" not in quotes  # 완전 결측 심볼은 quotes에서 제외
    assert "^GSPC" in quotes

    # daily_report.generate_us_market_report(1046~1060)의 실제 패턴 그대로 재현 —
    # 예외 없이 끝까지 돌아야 한다.
    rendered = []
    for sym in INDEX_SYMBOLS:
        q = quotes.get(sym)
        if not q:
            continue
        pct = q["change_pct"]
        rendered.append((sym, pct > 0))
    assert rendered == [("^GSPC", True)]

    # indices_normalized는 quotes에서 빠진 ^DJI에 대해서도 "필드 결측" 사유를 유지한다
    signal = asyncio.run(umd.get_overnight_signal())
    dow = signal["indices_normalized"]["DOW"]
    assert dow["missing"] is True
    assert "필드 결측" in dow["reason"]
    assert isinstance(signal["summary"], str) and signal["summary"]


def test_mixed_full_partial_and_fully_missing_symbols_no_crash():
    """실운영에 가까운 혼합 응답(정상/부분 결측/완전 결측 3종 동시) — 리뷰 blocking
    재현 시나리오(GSPC 정상, SOX 부분 결측, DJI 완전 결측)를 그대로 섞어 quotes/
    self._seen_missing 분리와 daily_report 소비 패턴 무예외를 함께 확인한다.
    2026-09-15 (T10 F18 2차): 이 '결과가 비어있지 않은' 혼합 케이스는 1차 수정
    당시에도 통과했었다 — v7이 None을 반환하지 않았기 때문. 단일 심볼(전부
    부분/완전 결측)만 응답에 오는 극단값에서만 v8 폴백·seen_missing 초기화가
    트리거돼 조용히 정보가 사라지는 회귀가 있었다(위 v7 리턴 로직 수정으로 해결)."""
    from src.data.providers.us_market_data import USMarketData, INDEX_SYMBOLS

    umd = USMarketData()

    def _responder(url, params):
        quotes = [
            {"symbol": "^GSPC", "regularMarketPrice": 6500.0,
             "regularMarketChange": 78.0, "regularMarketChangePercent": 1.2,
             "regularMarketTime": 1757900000},
            {"symbol": "^SOX", "regularMarketPrice": 5200.0,
             "regularMarketChangePercent": float("nan"),
             "regularMarketTime": 1757900000},
            {"symbol": "^DJI", "regularMarketTime": 1757900000},
        ]
        return 200, {"quoteResponse": {"result": quotes}}

    async def fake_get_session():
        return _FakeSession(_responder)

    umd._get_session = fake_get_session  # type: ignore[assignment]

    quotes = asyncio.run(umd.fetch_us_market_summary(force_refresh=True))
    assert set(quotes.keys()) == {"^GSPC"}  # 부분·완전 결측 둘 다 quotes에서 제외

    rendered = []
    for sym in INDEX_SYMBOLS:
        q = quotes.get(sym)
        if not q:
            continue
        pct = q["change_pct"]
        rendered.append((sym, pct > 0))
    assert rendered == [("^GSPC", True)]

    signal = asyncio.run(umd.get_overnight_signal())
    norm = signal["indices_normalized"]
    assert norm["SP500"]["missing"] is False and norm["SP500"]["price"] == 6500.0
    assert norm["SOX"]["missing"] is False and norm["SOX"]["price"] == 5200.0
    assert norm["SOX"]["missing_fields"] == ["change_pct"]
    assert norm["DOW"]["missing"] is True and "필드 결측" in norm["DOW"]["reason"]
    assert isinstance(signal["summary"], str) and signal["summary"]


# ─────────────────────────────────────────────────────────────────
# F16-6 재검증 — score에 기여하지 않는 macro_context는 insufficient→partial 승격
# 근거가 아니고, score에 실제로 기여하는 cpi_yoy/semis_basket은 판정 대상이다.
# ─────────────────────────────────────────────────────────────────
def test_macro_context_alone_does_not_promote_insufficient_to_partial(monkeypatch):
    """숫자 지표(us10y/dxy/krw_usd/vix/wti/cpi_yoy/semis_basket)가 전부 결측이고
    매크로 컨텍스트(Perplexity 텍스트)만 있으면 여전히 insufficient다 —
    macro_context는 _score_indicators의 score를 전혀 바꾸지 않으므로(findings
    문자열만 추가) score=0은 "모른다"이지 "중립"이 아니다. 커버리지 계산에서도
    빠져야 한다(valid_n=3, aggregate_regime_score=0)."""
    macro = MacroEconomist(ExpertConfig(), llm_manager=None, perplexity_key="")

    async def _empty():
        return {}

    async def _ctx():
        return "연준은 금리를 동결했습니다."

    monkeypatch.setattr(macro, "_fetch_yfinance_indicators", _empty)
    monkeypatch.setattr(macro, "_fetch_semis_basket_5d", _empty)
    monkeypatch.setattr(macro, "_fetch_macro_context", _ctx)
    monkeypatch.setattr(macro, "_load_manual_overrides", lambda: {})

    op_ = asyncio.run(macro._analyze())
    op_.expert = "macro_economist"

    assert op_.data_status == "insufficient"
    assert op_.score == 0
    assert op_.confidence <= 0.2  # CONFIDENCE_CAP_INSUFFICIENT

    orch = ExpertOrchestrator(ExpertConfig())
    ops = {
        "macro_economist": op_,
        "kr_market_expert": _opinion("kr_market_expert", 40, 0.8),
        "us_market_expert": _opinion("us_market_expert", 40, 0.8),
        "kr_economy_expert": _opinion("kr_economy_expert", 40, 0.8),
    }
    summary = orch.data_status_summary(ops)
    assert summary["valid_n"] == 3
    assert summary["insufficient_coverage"] is True
    assert orch.aggregate_regime_score(ops) == 0


def test_macro_cpi_override_only_is_partial_not_insufficient(monkeypatch):
    """5개 핵심 지표가 전부 결측이어도 cpi_yoy 수동 오버라이드만 있으면 score가
    실제로 -8만큼 움직인다 — insufficient(추측)가 아니라 partial(일부 결측)이어야
    한다(리뷰 advisory A3)."""
    macro = MacroEconomist(ExpertConfig(), llm_manager=None, perplexity_key="")

    async def _empty():
        return {}

    async def _noctx():
        return ""

    monkeypatch.setattr(macro, "_fetch_yfinance_indicators", _empty)
    monkeypatch.setattr(macro, "_fetch_semis_basket_5d", _empty)
    monkeypatch.setattr(macro, "_fetch_macro_context", _noctx)
    monkeypatch.setattr(macro, "_load_manual_overrides", lambda: {"cpi_yoy": 4.2})

    op_ = asyncio.run(macro._analyze())
    assert op_.data_status == "partial"
    assert op_.score == -8


def test_macro_semis_basket_only_is_partial_not_insufficient(monkeypatch):
    """5개 핵심 지표가 전부 결측이어도 반도체 바스켓 5일 평균만 있으면 score가
    실제로 움직인다 — partial이어야 한다(리뷰 advisory A3)."""
    macro = MacroEconomist(ExpertConfig(), llm_manager=None, perplexity_key="")

    async def _empty():
        return {}

    async def _semis():
        return {"avg_5d_pct": -5.0}

    async def _noctx():
        return ""

    monkeypatch.setattr(macro, "_fetch_yfinance_indicators", _empty)
    monkeypatch.setattr(macro, "_fetch_semis_basket_5d", _semis)
    monkeypatch.setattr(macro, "_fetch_macro_context", _noctx)
    monkeypatch.setattr(macro, "_load_manual_overrides", lambda: {})

    op_ = asyncio.run(macro._analyze())
    assert op_.data_status == "partial"
    assert op_.score == -8


def test_macro_core_five_present_cpi_absent_is_ok_not_capped(monkeypatch):
    """T10 B 리뷰 advisory — 5개 핵심 yfinance 지표(+반도체 바스켓)가 전부 정상이면,
    cpi_yoy(수동 오버라이드, 평시 미설정)가 없어도 partial로 강등되면 안 된다.
    cpi_yoy는 '미입력인 선택적 수동값'이지 '수집 실패한 결측'이 아니다 — 이 상태가
    매일 반복되는 평시 상태이므로, 여기서 partial이면 raw confidence(0.72)가 매일
    CONFIDENCE_CAP_PARTIAL(0.7)에 눌린다(6개 지표 → 0.3+6*0.07=0.72 > 0.7이라
    캡이 실제로 발동하는 지점으로 골랐다)."""
    macro = MacroEconomist(ExpertConfig(), llm_manager=None, perplexity_key="")

    async def _prices():
        return {"us10y": 4.2, "dxy": 103.0, "krw_usd": 1350.0, "vix": 15.0, "wti": 70.0}

    async def _semis():
        return {"avg_5d_pct": 1.0}  # 중립 범위(±3.0 미만) — score는 안 움직이되 필드는 채움

    async def _noctx():
        return ""

    monkeypatch.setattr(macro, "_fetch_yfinance_indicators", _prices)
    monkeypatch.setattr(macro, "_fetch_semis_basket_5d", _semis)
    monkeypatch.setattr(macro, "_fetch_macro_context", _noctx)
    monkeypatch.setattr(macro, "_load_manual_overrides", lambda: {})

    op_ = asyncio.run(macro._analyze())
    assert op_.data_status == "ok"
    assert op_.confidence == 0.72  # 캡(0.7) 미적용 — raw 그대로
    # cpi_yoy는 여전히 missing_inputs에 남아 관측 가능해야 한다
    # (data_status만 ok로 승격, 결측 사실 자체는 숨기지 않는다).
    assert "CPI(수동 오버라이드)" in op_.missing_inputs


# ─────────────────────────────────────────────────────────────────
# F17-5 재검증 — KRX 야간선물(CM) 세션 판정은 요일을 봐야 한다(주말엔 세션 없음)
# ─────────────────────────────────────────────────────────────────
def test_night_session_weekend_evenings_are_not_in_session():
    """토요일 18:00~일요일 06:00, 일요일 18:00~월요일 06:00은 세션이 없는 시간대다
    (KRX 야간선물은 월~금 저녁에만 개장). 요일을 보지 않으면 이 구간이 '개장 중'
    으로 오판돼 사흘 묵은 금요일 체결가가 as_of=조회시각(실시간)으로 찍힌다
    (리뷰 F17 blocking). 실제 운영 슬롯 sunday_evening(일 22:00)이 이 구간이다."""
    from datetime import datetime as dt

    from src.utils.data_freshness import kr_night_futures_as_of

    friday_close = dt(2026, 9, 12, 6, 0)  # 금요일(09-11) 저녁 세션 종료 = 토요일 06:00 (KRX 현행, 2026-09-15 정정)

    for label, now in [
        ("일요일 22:00 (sunday_evening 슬롯)", dt(2026, 9, 13, 22, 0)),
        ("토요일 20:00", dt(2026, 9, 12, 20, 0)),
        ("월요일 03:00", dt(2026, 9, 14, 3, 0)),
    ]:
        as_of, _note, ttl = kr_night_futures_as_of(now, "night")
        assert as_of == friday_close, f"{label}: as_of={as_of} (실시간으로 오판되면 안 됨)"
        assert ttl is not None and ttl > 0


def test_night_session_weekday_evenings_and_mornings_stay_in_session():
    """월~금 저녁(18:00~), 화~토 새벽(~06:00, 전날 저녁 세션의 연장)은 정상적으로
    개장 중으로 판정되어 as_of=조회 시각(실시간 호가)이어야 한다 — 주말 수정이
    평일 정상 케이스를 깨면 안 된다."""
    from datetime import datetime as dt

    from src.utils.data_freshness import kr_night_futures_as_of

    for label, now in [
        ("금요일 20:00", dt(2026, 9, 11, 20, 0)),
        ("월요일 20:00", dt(2026, 9, 14, 20, 0)),
        ("화요일 03:00 (월요일 저녁 세션 연장)", dt(2026, 9, 15, 3, 0)),
        ("토요일 03:00 (금요일 저녁 세션 연장)", dt(2026, 9, 12, 3, 0)),
    ]:
        as_of, note, ttl = kr_night_futures_as_of(now, "night")
        assert as_of == now, f"{label}: as_of={as_of} (실시간 세션인데 과거로 밀림)"
        assert note is None
        assert ttl is not None and ttl > 0


def test_night_session_monday_morning_composes_with_is_fresh():
    """T10 B 리뷰 advisory — F17의 '주말·휴장·미국 전일 마감 자료의 정상 사용(월요일
    07:30에 토요일 06:00 종료 세션값)'을 kr_night_futures_as_of의 반환값만이 아니라
    실제 소비 경로가 쓰는 is_fresh(DataPoint, now)까지 합성해 고정한다. 전문가
    _analyze()는 now를 주입받을 인터페이스가 없어(구조적 제약) 이 조합은 여기서
    순수 함수로만 검증한다."""
    from datetime import datetime as dt

    from src.utils.data_freshness import DataPoint, is_fresh, kr_night_futures_as_of

    monday_0730 = dt(2026, 9, 14, 7, 30)  # 2026-09-14는 월요일
    as_of, note, ttl = kr_night_futures_as_of(monday_0730, "night")
    assert as_of == dt(2026, 9, 12, 6, 0)  # 금요일(09-11) 야간 세션 종료 = 토요일 06:00 (KRX 현행)
    assert note is None

    dp = DataPoint(value=2.0, as_of=as_of, source="kis_night_futures",
                    session="night", ttl_seconds=ttl)
    assert is_fresh(dp, monday_0730) is True  # 아직 다음 세션(월 18:00) 전이라 유효

    # 대조: 다음 세션 개장 이후(월요일 19:00)는 같은 값이 더 이상 유효하지 않다.
    monday_evening = dt(2026, 9, 14, 19, 0)
    assert is_fresh(dp, monday_evening) is False


# ─────────────────────────────────────────────────────────────────
# 2026-09-15 사용자 승인 — 야간 세션 규칙에 공휴일 캘린더 연동 (KRX 개시일 기준)
# ─────────────────────────────────────────────────────────────────
def test_night_session_holiday_calendar_start_day_rule(monkeypatch):
    """KRX 야간거래 안내: 휴장 여부는 야간거래 **개시일** 기준. 거래일 저녁이면 익일이
    공휴일이어도 개장(연휴 전날), 공휴일 당일 저녁은 휴장, 거래시간 18:00~익일 06:00.
    2026 내장 폴백 캘린더: 09-24·09-25 추석, 10-09 한글날(금)."""
    from datetime import datetime as dt

    import src.utils.session as session_mod
    from src.utils.data_freshness import kr_night_futures_as_of

    monkeypatch.setattr(session_mod, "_kr_market_holidays", set())   # 내장 폴백 캘린더 사용

    # 연휴 전날(09-23 수, 거래일) 20:00 — 개장 중 (as_of=now), 다음 개장은 09-28(월) 18:00
    as_of, note, ttl = kr_night_futures_as_of(dt(2026, 9, 23, 20, 0), "night")
    assert as_of == dt(2026, 9, 23, 20, 0) and note is None
    assert dt(2026, 9, 23, 20, 0) + __import__("datetime").timedelta(seconds=ttl) == dt(2026, 9, 28, 18, 0)

    # 09-24(추석 전날, 공휴일) 04:00 — 09-23 세션의 연장 → 개장 중
    as_of, _, _ = kr_night_futures_as_of(dt(2026, 9, 24, 4, 0), "night")
    assert as_of == dt(2026, 9, 24, 4, 0)

    # 09-24(공휴일) 20:00 — 개시일이 휴장일 → 세션 없음 → 직전 세션(09-23) 종료 09-24 06:00
    as_of, _, ttl = kr_night_futures_as_of(dt(2026, 9, 24, 20, 0), "night")
    assert as_of == dt(2026, 9, 24, 6, 0)
    assert as_of + __import__("datetime").timedelta(seconds=ttl) == dt(2026, 9, 28, 18, 0)

    # 09-25(추석, 금) 07:30 — 같은 값, 월요일(09-28) 07:30 까지도 그대로 유효
    as_of, _, ttl = kr_night_futures_as_of(dt(2026, 9, 25, 7, 30), "night")
    assert as_of == dt(2026, 9, 24, 6, 0)
    as_of2, _, ttl2 = kr_night_futures_as_of(dt(2026, 9, 28, 7, 30), "night")
    assert as_of2 == dt(2026, 9, 24, 6, 0) and ttl2 > 0

    # 10-09(한글날, 금) 20:00 — 휴장, 직전 세션은 10-08(목) → 종료 10-09 06:00, 다음 개장 10-12(월)
    as_of, _, ttl = kr_night_futures_as_of(dt(2026, 10, 9, 20, 0), "night")
    assert as_of == dt(2026, 10, 9, 6, 0)
    assert as_of + __import__("datetime").timedelta(seconds=ttl) == dt(2026, 10, 12, 18, 0)
    # 10-12(월) 04:00 — 전날(일) 세션 없음 → 여전히 10-09 06:00
    as_of, _, _ = kr_night_futures_as_of(dt(2026, 10, 12, 4, 0), "night")
    assert as_of == dt(2026, 10, 9, 6, 0)

    # 06:00 정정 — 05:30 은 아직 세션 중(실시간), 06:30 은 세션 종료 후(06:00 고정)
    as_of, _, _ = kr_night_futures_as_of(dt(2026, 9, 15, 5, 30), "night")
    assert as_of == dt(2026, 9, 15, 5, 30)
    as_of, _, _ = kr_night_futures_as_of(dt(2026, 9, 15, 6, 30), "night")
    assert as_of == dt(2026, 9, 15, 6, 0)
