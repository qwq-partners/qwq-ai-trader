"""T10 담당 D — 독립 재현 테스트 B (F16~F18, 자료 유효성·전문가)

구현자(B)와 무관하게 기준 SHA(a59e29f)의 실제 코드를 기존 공개 진입점으로만
호출해 리뷰 주장을 재현한다. 아래 각 테스트는 기준 SHA에서 실패해야 하고
(결함 재현), T10 수정 통합 후에는 통과해야 한다.

대상 진입점:
  F16 — MacroEconomist._analyze / ExpertAgent._build_opinion (data_status 기본값)
       + ExpertOrchestrator.aggregate_regime_score / data_status_summary
  F17 — WeekendSignalExpert._analyze / KRMarketExpert._fetch_kospi200_futures
       (야간선물 as_of 없이도 점수·유효신호로 집계)
  F18 — USMarketData._fetch_via_v7 / get_overnight_signal (가격 결측 0 위장)

실행: /home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest tests/test_t10_repro_b.py -q
네트워크는 conftest 가 차단 — yfinance 실호출은 실패해 자동으로 결측(None)이 된다.
"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ══════════════════════════════════════════════════════════════════════════
# F16 — 자료가 전부 비어도 macro_economist 의 data_status 가 "ok" 로 나와
#       4번째 유효 전문가로 시장체제 집계에 가중 반영된다
# ══════════════════════════════════════════════════════════════════════════

def test_f16_macro_with_zero_data_should_not_count_as_valid_expert(monkeypatch):
    from src.experts.macro_economist import MacroEconomist
    from src.experts.types import ExpertConfig, ExpertOpinion, RegimeBias
    from src.experts.orchestrator import ExpertOrchestrator

    macro = MacroEconomist(ExpertConfig())

    async def _empty_dict():
        return {}

    def _empty_overrides():
        return {}

    async def _empty_ctx():
        return ""

    async def _empty_llm(prices, ctx):
        return None

    monkeypatch.setattr(macro, "_fetch_yfinance_indicators", _empty_dict)
    monkeypatch.setattr(macro, "_fetch_semis_basket_5d", _empty_dict)
    monkeypatch.setattr(macro, "_load_manual_overrides", _empty_overrides)
    monkeypatch.setattr(macro, "_fetch_macro_context", _empty_ctx)
    monkeypatch.setattr(macro, "_llm_synthesize", _empty_llm)

    opinion = asyncio.run(macro._analyze())

    assert opinion.raw_evidence.get("prices") == {}, "지표가 정말 전부 비어있는지 확인"
    assert opinion.data_status != "ok", (
        f"거시 지표·검색·수동오버라이드가 전부 비어 있는데도 data_status={opinion.data_status!r} "
        f"(insufficient 여야 orchestrator 집계에서 가중 0으로 제외된다)"
    )

    # 유효 3명(score 40, conf 0.8) + 완전 무자료 macro → orchestrator 집계
    orch = ExpertOrchestrator(ExpertConfig())
    others = {
        name: ExpertOpinion(
            expert=name, score=40, regime_bias=RegimeBias.BULL, confidence=0.8,
        )
        for name in ("kr_market_expert", "us_market_expert", "kr_economy_expert")
    }
    opinions = dict(others)
    opinions["macro_economist"] = opinion

    summary = orch.data_status_summary(opinions)
    assert summary["valid_n"] == 3, (
        f"자료가 전부 비어 있는 macro_economist 가 유효 시장체제 전문가로 잡혔다: {summary}"
    )
    assert summary["insufficient_coverage"] is True, (
        f"유효 3명(<MIN_VALID_EXPERTS=4)인데 커버리지 부족으로 표시되지 않았다: {summary}"
    )

    agg = orch.aggregate_regime_score(opinions)
    assert agg == 0, (
        f"커버리지 부족(유효 3명)인데도 무자료 macro_economist 가 4번째로 끼어들어 "
        f"체제 점수가 무보정(0)이 아니라 {agg} 로 나왔다"
    )

    # 대조: 정상 4번째 전문가가 있으면 보정이 걸려야 한다(게이트 자체는 살아있음을 확인)
    opinions_with_valid_4th = dict(others)
    opinions_with_valid_4th["weekend_signal_expert"] = ExpertOpinion(
        expert="weekend_signal_expert", score=40, regime_bias=RegimeBias.BULL, confidence=0.8,
    )
    agg_valid = orch.aggregate_regime_score(opinions_with_valid_4th)
    assert agg_valid != 0, f"정상 4번째 전문가가 있는데도 무보정(0)이다: {agg_valid}"


# ══════════════════════════════════════════════════════════════════════════
# F17 — 야간선물 as_of 없음(시장 시각 미제공) + 570분째 값 고정인데도
#       숫자·dict 존재만으로 점수 가산·유효신호 카운트에 들어간다
# ══════════════════════════════════════════════════════════════════════════

class _StaleNightFutures:
    """세션은 night 이지만 시장 시각(as_of)을 전혀 제공하지 않고, 9시간 넘게
    값이 그대로인(고착 의심) 야간선물 응답."""

    async def get_night_futures_quote(self):
        return {
            "session": "night",
            "change_pct": 2.0,
            "as_of": None,
            "fetched_at": "2026-09-14T20:00:00",
            "value_unchanged_minutes": 570,
            "symbol": "101W09",
            "price": 350.0,
        }


class _NoNetworkTicker:
    """conftest의 소켓 가드는 yfinance(curl_cffi 백엔드)의 실제 HTTP 호출을 잡지
    못한다(파이썬 socket 모듈을 우회) — 2026-09-15 이 테스트 작성 중 실측으로 확인.
    §0 네트워크 무접촉 경계를 지키기 위해 yfinance.Ticker 자체를 직접 가짜로 막는다."""

    def __init__(self, *a, **k):
        pass

    def history(self, *a, **k):
        raise RuntimeError("네트워크 차단(테스트 — yfinance 직접 호출 금지)")


def _block_yfinance(monkeypatch):
    import yfinance as yf
    monkeypatch.setattr(yf, "Ticker", _NoNetworkTicker)


def test_f17_weekend_expert_scores_stale_no_as_of_night_futures(monkeypatch):
    from src.experts.weekend_signal_expert import WeekendSignalExpert
    from src.experts.types import ExpertConfig

    _block_yfinance(monkeypatch)

    import src.data.providers.kis_market_data as kmd_mod
    monkeypatch.setattr(kmd_mod, "get_kis_market_data", lambda: _StaleNightFutures())

    expert = WeekendSignalExpert(ExpertConfig())
    opinion = asyncio.run(expert._analyze())

    # 나머지 yfinance 신호는 conftest 네트워크 차단으로 전부 결측(None) — kr_futures_pct 단독 신호
    assert opinion.score == 0, (
        f"as_of 없는(기준시각 미상) 야간선물 +2.0%가 그대로 점수에 반영됐다: "
        f"score={opinion.score}, findings={opinion.key_findings}"
    )
    assert not any("갭업 기대" in f for f in opinion.key_findings), (
        f"기준시각 없는 값으로 '갭업 기대' 판단 문구를 냈다: {opinion.key_findings}"
    )
    assert opinion.data_status != "ok", (
        f"결측(as_of 없음)일 뿐인데 data_status={opinion.data_status!r} 로 정상 취급됐다"
    )


def test_f17_kr_market_expert_futures_fetch_discards_no_as_of_value(monkeypatch):
    """KRMarketExpert 도 같은 KIS 공급자를 쓴다 — overnight_chg_pct 가 as_of 없이도
    그대로 반환되면 안 된다."""
    from src.experts.kr_market_expert import KRMarketExpert
    from src.experts.types import ExpertConfig

    import src.data.providers.kis_market_data as kmd_mod
    monkeypatch.setattr(kmd_mod, "get_kis_market_data", lambda: _StaleNightFutures())

    expert = KRMarketExpert(ExpertConfig())

    # 2026-09-15 통합 조정: 수정 후 구현은 fetch 결과에 값+as_of(None) 을 함께 실어 두고
    # _analyze 의 신선도 게이트에서 판단에 쓰지 않는다 — 내부 표현이 아니라 관찰 결과
    # (점수·유효 입력·data_status)로 판정한다. 다른 세 입력은 결측으로 두어 야간선물이
    # 유일한 후보가 되게 한다: 세면 partial+점수, 안 세면 insufficient+0.
    async def _empty(*a, **kw):
        return {}
    monkeypatch.setattr(expert, "_fetch_investor_flows", _empty)
    monkeypatch.setattr(expert, "_fetch_kospi_state", _empty)
    monkeypatch.setattr(expert, "_fetch_short_balance", _empty)

    op = asyncio.run(expert._analyze())

    assert op.score == 0, f"as_of 없는 야간선물 +2% 가 점수에 가산됐다: {op.score} {op.key_findings}"
    assert op.data_status == "insufficient", (
        f"as_of 없는 야간선물이 유효 입력으로 세어져 data_status={op.data_status!r}: {op.missing_inputs}"
    )
    assert any("야간선물" in m for m in op.missing_inputs), op.missing_inputs
    assert not any("갭업" in f for f in op.key_findings), op.key_findings


# ══════════════════════════════════════════════════════════════════════════
# F18 — Yahoo v7 응답에 종목·시각만 있고 가격이 없으면 price=0/missing=False 로
#       조용히 위장된다 (0 이 실측 무변동과 구분 불가)
# ══════════════════════════════════════════════════════════════════════════

class _FakeResp:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def json(self):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeSession:
    def __init__(self, payload):
        self._payload = payload

    def get(self, url, params=None, **kw):
        return _FakeResp(200, self._payload)


def _v7_payload_with_priceless_vix():
    """명세 F18 인수 입력: 지수 4종은 정상, VIX 는 종목·시각만 (가격·변동률 없음)"""
    def _q(sym, price, chg, pct):
        return {"symbol": sym, "regularMarketPrice": price, "regularMarketChange": chg,
                "regularMarketChangePercent": pct, "regularMarketTime": 1757900000}
    return {"quoteResponse": {"result": [
        _q("^GSPC", 6000.0, 30.0, 0.5), _q("^IXIC", 20000.0, 120.0, 0.6),
        _q("^SOX", 5500.0, 90.0, 1.6), _q("^DJI", 44000.0, 100.0, 0.2),
        {"symbol": "^VIX", "regularMarketTime": 1757900000},
    ]}}


def test_f18_v7_missing_price_defaults_to_zero_not_none(monkeypatch):
    from src.data.providers.us_market_data import USMarketData

    umd = USMarketData()

    async def _fake_get_session():
        return _FakeSession(_v7_payload_with_priceless_vix())

    monkeypatch.setattr(umd, "_get_session", _fake_get_session)

    result = asyncio.run(umd._fetch_via_v7())

    # 2026-09-15 통합 조정: 수정 후 파서는 가격·변동률이 둘 다 없는 심볼을 quotes 에서
    # 빼고(소비자 f-string/비교 보호) 결측 사유를 별도로 보존한다 — "price 가 0 이 아니다"
    # 라는 관찰 결과로 판정한다(심볼이 남아 있다면 price 는 None 이어야 한다).
    assert result is not None and "^GSPC" in result
    assert result.get("^VIX", {}).get("price") is None, (
        f"regularMarketPrice 가 없는 응답인데 price 가 0 으로 채워졌다(결측 위장): {result.get('^VIX')}"
    )


def test_f18_overnight_signal_vix_missing_flag_not_false(monkeypatch):
    from src.data.providers.us_market_data import USMarketData

    umd = USMarketData()

    async def _fake_get_session():
        return _FakeSession(_v7_payload_with_priceless_vix())

    monkeypatch.setattr(umd, "_get_session", _fake_get_session)

    signal = asyncio.run(umd.get_overnight_signal())
    vix = signal["indices_normalized"]["VIX"]

    assert vix["missing"] is True, (
        f"가격 없이 종목·시각만 있는 VIX 응답이 missing=False 로 표시됐다: {vix}"
    )
    assert vix["price"] is None, f"VIX price 가 0 으로 위장됐다: {vix}"
    assert vix.get("reason"), f"결측 사유가 없다: {vix}"
    assert signal["indices_normalized"]["SP500"]["missing"] is False
    assert signal["indices_normalized"]["SP500"]["change_pct"] == 0.5


def test_f18_overnight_signal_all_indices_missing_is_not_flat(monkeypatch):
    """지수 4종이 전부 결측(개별 종목만 응답)이면 summary 가 'US 시장 보합 마감' 으로
    포장되지 않고 indices_missing=True 를 낸다 (2026-09-15 T10 통합 리뷰 INT-2 advisory).
    sentiment 는 소비자 계약(테마 부스트 없음)상 neutral 유지."""
    from src.data.providers.us_market_data import USMarketData

    umd = USMarketData()
    payload = {"quoteResponse": {"result": [
        {"symbol": "AAPL", "regularMarketPrice": 230.0, "regularMarketChange": 1.0,
         "regularMarketChangePercent": 0.4, "regularMarketTime": 1757900000},
        {"symbol": "^VIX", "regularMarketTime": 1757900000},
    ]}}

    async def _fake_get_session():
        return _FakeSession(payload)

    monkeypatch.setattr(umd, "_get_session", _fake_get_session)
    signal = asyncio.run(umd.get_overnight_signal())

    assert signal["indices_missing"] is True
    assert "보합 마감" not in signal["summary"], signal["summary"]
    assert "미수집" in signal["summary"], signal["summary"]
    assert signal["sentiment"] == "neutral"
    assert all(v["missing"] for v in signal["indices_normalized"].values())
