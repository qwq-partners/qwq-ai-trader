"""Offline regression cases from the 2026-10-21 briefing audit."""
import asyncio
from datetime import datetime

from src.experts.orchestrator import ExpertOrchestrator
from src.experts.types import ExpertConfig, ExpertOpinion, RegimeBias


def opinion(name, bias=RegimeBias.NEUTRAL, status="ok", confidence=.8):
    return ExpertOpinion(expert=name, score=-30 if bias == RegimeBias.BEAR else 0,
                         regime_bias=bias, confidence=confidence, data_status=status)


def market_snapshot():
    return {n: opinion(n) for n in ExpertOrchestrator.MARKET_REGIME_EXPERTS}


def test_reference_bears_cannot_change_market_bias_or_consensus():
    orch = ExpertOrchestrator(ExpertConfig())
    ops = market_snapshot()
    before = (orch.aggregate_bias(ops), orch.aggregate_regime_score(ops))
    for n in ("news_curator", "earnings_expert", "sector_council"):
        ops[n] = opinion(n, RegimeBias.BEAR, confidence=1)
    assert (orch.aggregate_bias(ops), orch.aggregate_regime_score(ops)) == before
    assert orch.bear_consensus(opinions=ops) is False


def test_unknown_and_nonfinite_market_opinions_do_not_count():
    orch = ExpertOrchestrator(ExpertConfig())
    ops = market_snapshot()
    ops["macro_economist"] = opinion("macro_economist", RegimeBias.BEAR, "unknown")
    ops["kr_market_expert"] = opinion("kr_market_expert", RegimeBias.BEAR, confidence=float("nan"))
    ops["global_micro_expert"].score = float("inf")
    assert orch.data_status_summary(ops)["valid_n"] == 3
    assert orch.bear_consensus(opinions=ops) is False


def test_bear_consensus_requires_same_market_coverage():
    orch = ExpertOrchestrator(ExpertConfig())
    ops = {n: opinion(n, RegimeBias.BEAR) for n in list(orch.MARKET_REGIME_EXPERTS)[:2]}
    ops["news_curator"] = opinion("news_curator")
    ops["earnings_expert"] = opinion("earnings_expert")
    assert orch.bear_consensus(opinions=ops) is False


def test_macro_level_does_not_generate_unverifiable_price_movement(monkeypatch):
    from src.experts.macro_economist import MacroEconomist
    exp = MacroEconomist(ExpertConfig())
    async def prices():
        return {"us10y": 5.117, "dxy": 101.78, "krw_usd": 1324.53, "vix": 17.25, "wti": 98.69}
    async def empty():
        return {}
    async def text():
        return ""
    async def hallucination(*args):
        return "미 국채 금리 급등으로 한국 증시가 급락할 것입니다"
    monkeypatch.setattr(exp, "_fetch_yfinance_indicators", prices)
    monkeypatch.setattr(exp, "_fetch_semis_basket_5d", empty)
    monkeypatch.setattr(exp, "_load_manual_overrides", lambda: {})
    monkeypatch.setattr(exp, "_fetch_macro_context", text)
    monkeypatch.setattr(exp, "_llm_synthesize", hallucination)
    op = asyncio.run(exp._analyze())
    assert not any("급등" in f or "할 것입니다" in f for f in op.key_findings)
    assert any("5.12" in f for f in op.key_findings)


def test_negative_industry_text_cannot_create_positive_hbm_orders(monkeypatch):
    from src.experts.global_micro_expert import GlobalMicroExpert
    exp = GlobalMicroExpert(ExpertConfig())
    async def text(*args, **kwargs):
        return "HBM demand weakens; shipbuilding LNG orders cancelled."
    async def empty():
        return {}
    monkeypatch.setattr(exp, "_perplexity_search", text)
    monkeypatch.setattr(exp, "_fetch_sector_etf_returns", empty)
    op = asyncio.run(exp._analyze())
    assert op.score == 0
    assert not any("수요 견조" in f or "수주 모멘텀" in f for f in op.key_findings)
    assert op.data_status == "insufficient"


def test_disclosure_count_is_not_positive_earnings_evidence(monkeypatch):
    from src.experts.earnings_expert import EarningsExpert
    exp = EarningsExpert(ExpertConfig())
    async def empty(**kwargs):
        return {}
    async def disclosures(**kwargs):
        return {"count": 30}
    monkeypatch.setattr(exp, "_fetch_us_upcoming_earnings", empty)
    monkeypatch.setattr(exp, "_fetch_us_recent_surprises", empty)
    monkeypatch.setattr(exp, "_fetch_kr_recent_disclosures", disclosures)
    op = asyncio.run(exp._analyze())
    assert op.score == 0
    assert op.raw_evidence["kr_disclosures"]["count"] == 30
    assert op.data_status == "insufficient"


def test_briefing_shows_rebound_and_cumulative_weakness_with_evidence():
    from src.experts.briefing import render_expert_briefing
    orch = ExpertOrchestrator(ExpertConfig())
    ops = market_snapshot()
    ops["macro_economist"].key_findings = ["US10Y 5.12% <b>untrusted</b> & source"]
    sector = opinion("sector_council")
    sector.raw_evidence = {"sector_scores": {"반도체": {
        "r1": 3.34, "r5": -10.36, "r20": -10.46, "r5_sessions": 5,
        "r20_sessions": 20, "score": -80, "quant_only": True,
        "score_saturated": True, "source": "KIS:091160", "as_of": "2026-10-21",
    }}}
    ops["sector_council"] = sector
    text = render_expert_briefing("장후", ops, 0, "neutral", False,
                                 orch.data_status_summary(ops), now=datetime(2026, 10, 21, 16, 30))
    assert "당일 +3.34%" in text and "5세션 -10.36%" in text
    assert "KIS:091160" in text and "가격 기준 2026-10-21" in text
    assert "정량점수 한도 도달" in text
    assert "&lt;b&gt;untrusted&lt;/b&gt; &amp; source" in text
    assert "신뢰도" not in text and "정상매매" not in text and "BUY 차단" not in text
    assert "반영 6/6" in text


def test_legacy_sector_evidence_cannot_claim_today_or_exact_period():
    from src.experts.briefing import render_expert_briefing
    sector = opinion("sector_council")
    sector.raw_evidence = {"sector_scores": {"반도체": {"r1": 3, "r5": -5, "r20": -8, "score": -50}}}
    text = render_expert_briefing("장후", {"sector_council": sector}, 0, "neutral", False, {})
    assert "최근 1세션 +3.00%" in text and "기간 미확인" in text
    assert "가격 기준 미확인" in text
    assert "당일 +3.00%" not in text
    assert "종합 방향 판단 보류" in text
    assert "자료 상태별 수: 미확인" in text
    assert "확보 0 / 부분 0 / 부족 0" not in text


def test_new_briefing_regime_score_is_not_scored_as_a_return_prediction():
    from src.analytics.morning_brief_eval import _expert_abstain_reason, _expert_claim
    expert = {"score": 0, "valid_n": 6, "interpretation": "regime_adjustment_not_forecast"}
    assert _expert_claim(expert) is None
    assert "체제 보정" in _expert_abstain_reason(expert)
    # Historic records retain their historic evaluation contract.
    assert _expert_claim({"score": 0, "valid_n": 6}) == "flat"


def test_kr_negative_search_mentions_cannot_generate_positive_score(monkeypatch):
    from src.experts.kr_economy_expert import KREconomyExpert
    exp = KREconomyExpert(ExpertConfig())
    async def text(*args, **kwargs):
        return "수출 증가 없음. 금리 인하 불가."
    async def empty():
        return {}
    monkeypatch.setattr(exp, "_perplexity_search", text)
    monkeypatch.setattr(exp, "_fetch_krw_state", empty)
    op = asyncio.run(exp._analyze())
    assert op.score == 0 and op.data_status == "insufficient"


def test_maximal_briefing_preserves_html_across_telegram_chunks():
    from html.parser import HTMLParser
    from src.experts.briefing import render_expert_briefing
    from src.utils.telegram import TelegramNotifier
    class BalancedHTML(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.stack = []
        def handle_starttag(self, tag, attrs):
            assert tag in {"b", "i"} and not attrs
            self.stack.append(tag)
        def handle_endtag(self, tag):
            assert self.stack.pop() == tag
    orch = ExpertOrchestrator(ExpertConfig())
    ops = market_snapshot()
    for name in ("news_curator", "earnings_expert"):
        ops[name] = opinion(name)
    for op in ops.values():
        op.key_findings = ["<>&" * 500]
        op.missing_inputs = ["<>&" * 500]
    sector = opinion("sector_council")
    sector.raw_evidence = {"sector_scores": {
        f"sector-{i}": {"r1": i, "r5": -i, "r20": -2 * i,
                         "score": -i, "source": "<>&" * 500}
        for i in range(13)
    }}
    ops["sector_council"] = sector
    message = render_expert_briefing("검증", ops, 0, "neutral", False, orch.data_status_summary(ops))
    notifier = object.__new__(TelegramNotifier)  # no configuration or network
    chunks = notifier._split_message(message)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk.encode("utf-16-le")) // 2 <= 4096
        parser = BalancedHTML()
        parser.feed(chunk)
        parser.close()
        assert not parser.stack


def test_news_only_sector_is_never_presented_as_price_trend():
    from src.experts.briefing import render_expert_briefing
    sector = opinion("sector_council")
    sector.raw_evidence = {"sector_scores": {"반도체": {"score": -48, "qual_only": True,
                                                       "reason": "공급망 뉴스"}}}
    text = render_expert_briefing("장후", {"sector_council": sector}, 0, "neutral", False, {})
    assert "뉴스 단독 점수 -48" in text and "가격 추세 미확인" in text
    assert "누적 추세점수 -48" not in text


def test_representative_news_source_and_collection_time_are_displayed():
    from src.experts.briefing import render_expert_briefing
    news = opinion("news_curator")
    news.key_findings = ["대표 기사 (-70)"]
    news.raw_evidence = {"item_count": 2, "classified_count": 2, "neutral_count": 1,
                         "representative_articles": [{"source": "예시신문", "title": "대표 기사",
                           "url": "https://news.example/driver", "fetched_at": "2026-10-21T09:30:00"}]}
    text = render_expert_briefing("장후", {"news_curator": news}, 0, "neutral", False, {})
    assert "예시신문" in text and "수집 10-21 09:30" in text
    assert "https://news.example/driver" in text and "발행시각 미확인" in text


def test_briefing_leads_with_rebound_vs_trend_and_a_check_not_buy_advice():
    from src.experts.briefing import render_expert_briefing
    orch = ExpertOrchestrator(ExpertConfig())
    ops = market_snapshot()
    sector = opinion('sector_council')
    sector.raw_evidence = {'sector_scores': {'반도체': {
        'r1': 3.34, 'r5': -10.36, 'r20': -10.46, 'r5_sessions': 5,
        'as_of': '2026-10-21', 'score': -80, 'quant_only': True,
    }}}
    ops['sector_council'] = sector
    text = render_expert_briefing('장후', ops, 0, 'neutral', False,
                                 orch.data_status_summary(ops), now=datetime(2026, 10, 21, 16, 30))
    assert text.index('먼저 볼 판단') < text.index('시장 입력과 참고 자료')
    assert '반등과 누적 약세가 공존' in text
    assert '거래대금·개별 수급' in text and '추세 전환 확정' not in text
