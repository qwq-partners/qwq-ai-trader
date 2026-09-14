"""T11-A 근거 계약 — 신규 기대 동작 테스트 (2026-09-15).

계획서(docs/superpowers/plans/2026-09-15-agent-team-evidence-entryplan.md) §4
인수 조건 #1~#4 를 대상으로 한다. 소유 파일: src/agents/analysts.py,
src/signals/fundamentals/stock_validator.py, src/experts/news_curator.py.
전부 오프라인 — 네트워크·운영 캐시 무접촉(fake/monkeypatch로 실제 I/O를 막는다).
"""

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from src.agents.analysts import AnalystTeam, FundamentalAnalyst, NewsAnalyst, TechnicalAnalyst
from src.agents.types import AnalystKind, AnalystReport
from src.experts.types import ExpertConfig
from src.signals.fundamentals.stock_validator import (
    DartCheckResult, NewsCheckResult, ShortSellingResult, StockValidator,
    SupplyDemandResult, TrendBuzzResult,
)


def _validation_result(**overrides):
    defaults = dict(
        approved=True, confidence_adjustment=0.0, block_reason="",
        supply_demand_result=SimpleNamespace(foreign_net_buying=False, institutional_net_buying=False),
        short_selling_result=SimpleNamespace(in_top50=False),
        validated=True, data_status="full",
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class _FakeValidator:
    def __init__(self, result):
        self._result = result

    async def validate(self, symbol, name):
        return self._result


# ── 인수 조건 #1: 근거 없는 검증 통과가 긍정 근거로 승격되지 않음 ──────────

def test_fundamental_validated_pass_only_sets_risk_clear_not_positive_basis():
    """검증 통과만 있고(순매수 등 다른 신호 없음) 위험도 못 찾은 경우:
    risk_clear=True, positive_basis 는 False/None(승격 금지), evidence 는
    '위험 미발견' fact 1건뿐이어야 한다."""
    result = _validation_result(approved=True, validated=True, data_status="full")
    fa = FundamentalAnalyst(stock_validator=_FakeValidator(result))
    report = asyncio.run(fa.analyze("005930", "테스트"))

    assert report.risk_clear is True
    assert report.positive_basis in (False, None)
    assert len(report.evidence) == 1
    assert report.evidence[0].metric == "risk_clear"
    assert report.evidence[0].kind == "fact"
    assert report.evidence[0].value is True


# ── 인수 조건 #1/#2: 미연결·예외는 긍정 근거를 만들지 않음 ────────────────

@pytest.mark.parametrize("status", ["insufficient", "error"])
def test_fundamental_not_validated_yields_no_positive_evidence(status):
    """validated=False 이면 수급 데이터가 실제로는 foreign=True/inst=True 로 와도
    "확인되지 않은 값"이므로 positive_basis/risk_clear 를 True 로 승격하지 않는다
    — evidence 에도 foreign/institutional 사실이 실리지 않는다."""
    result = _validation_result(
        approved=True, validated=False, data_status=status,
        supply_demand_result=SimpleNamespace(foreign_net_buying=True, institutional_net_buying=True),
    )
    fa = FundamentalAnalyst(stock_validator=_FakeValidator(result))
    report = asyncio.run(fa.analyze("005930", "테스트"))

    assert report.data_status == status
    assert report.positive_basis is None
    assert report.risk_clear is None
    assert all(e.metric not in ("foreign_net_buying", "institutional_net_buying")
               for e in report.evidence)


def test_fundamental_no_validator_is_insufficient_with_no_positive_evidence():
    """분석가 자체가 검증기에 미연결이면(self._validator is None) 애초에
    positive_basis/risk_clear 를 판단할 근거가 없다."""
    fa = FundamentalAnalyst(stock_validator=None, dart_checker=None)
    report = asyncio.run(fa.analyze("005930", "테스트"))
    assert report.data_status == "insufficient"
    assert report.positive_basis is None
    assert report.risk_clear is None
    assert report.evidence == []


# ── 인수 조건 #2: confidence=0/만료/오류가 evidence_quality 유효 소스에서 빠짐 ──

def test_evidence_quality_excludes_zero_confidence_expired_and_error():
    now = datetime.now()
    zero_conf = AnalystReport(kind=AnalystKind.FUNDAMENTAL, symbol="005930",
                               score=0, confidence=0.0, data_as_of=now)
    errored = AnalystReport.failed(AnalystKind.NEWS, "005930", "타임아웃")
    expired = AnalystReport(kind=AnalystKind.TECHNICAL, symbol="005930",
                             score=20, confidence=0.7,
                             data_as_of=now.replace(year=now.year - 1))  # HARD_TTL 훌쩍 초과
    real = AnalystReport(kind=AnalystKind.TECHNICAL, symbol="005930",
                          score=25, confidence=0.7, data_as_of=now)

    ok, reason, _w = AnalystTeam.evidence_quality([zero_conf, errored, expired, real])
    assert ok is False                       # 유효 소스는 real 1건뿐
    assert "유효 근거 1개" in reason


# ── 인수 조건 #3: 관측 시각을 모르면 now 로 세탁하지 않는다 ────────────────

def test_technical_observed_at_none_when_caller_omits_indicators_as_of():
    ta = TechnicalAnalyst()
    report = asyncio.run(ta.analyze(
        "005930", indicators={"rsi_14": 50.0, "vol_ratio": 1.0}, indicators_as_of=None,
    ))
    assert report.observed_at is None                 # now 로 채우지 않는다
    assert report.data_status == "partial"
    assert "지표 시각 미상" in report.limitations
    assert report.data_as_of is not None               # 기존 data_as_of=now 기준선은 불변


def test_technical_observed_at_set_when_caller_provides_indicators_as_of():
    known = datetime(2026, 9, 15, 9, 5)
    ta = TechnicalAnalyst()
    report = asyncio.run(ta.analyze(
        "005930", indicators={"rsi_14": 50.0}, indicators_as_of=known,
    ))
    assert report.observed_at == known
    assert report.data_status == "full"
    assert report.limitations == []


def test_fundamental_observed_at_always_none_cache_time_unknown():
    """펀더멘털은 캐시 히트 시각을 모른다 — data_as_of(추정치)와 별도로
    observed_at 은 항상 None, limitations 에 그 이유가 남아야 한다."""
    result = _validation_result(approved=True, validated=True, data_status="full")
    fa = FundamentalAnalyst(stock_validator=_FakeValidator(result))
    report = asyncio.run(fa.analyze("005930", "테스트"))
    assert report.observed_at is None
    assert "캐시 시각 미제공" in report.limitations


# ── 인수 조건 #4: 거래량 키·단위 계약 (technical.py 생산자 ⊇ analysts.py 소비자) ──

def _synthetic_daily(n: int):
    out = []
    price = 10000.0
    for i in range(n):
        price *= 1.001  # 완만한 상승 — ma200 등 파생값이 전부 계산되도록
        out.append({
            "date": f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}",
            "open": price, "high": price * 1.01, "low": price * 0.99,
            "close": price, "volume": 1_000_000 + i * 100,
        })
    return out


def test_technical_indicator_producer_key_set_covers_analyst_consumer_keys():
    from src.indicators.technical import TechnicalIndicators

    ti = TechnicalIndicators()
    out = ti.calculate_all("TESTSYM", _synthetic_daily(260))
    consumer_keys = {"rsi_14", "ma200_distance_pct", "atr_14", "vol_ratio"}
    assert consumer_keys <= out.keys()
    # 단위: atr_14는 생산자 주석·소비자(threshold>7 → "%") 양쪽 모두 % 스케일
    assert 0 <= out["atr_14"] < 100
    # ma200_distance_pct 도 % 스케일 (소비자 threshold 40/25/0 은 %)
    assert isinstance(out["ma200_distance_pct"], float)


# ── 뉴스: dedup + 헤드라인 한계 표기 ───────────────────────────────────

def test_news_curator_symbol_sentiment_dedups_same_url_article():
    from src.experts.news_curator import NewsCurator, NewsItem

    curator = NewsCurator(ExpertConfig())
    dup_items = [
        NewsItem(title="삼성전자 실적 서프라이즈", summary="영업이익 급증",
                  url="https://n.news/1", source="naver"),
        NewsItem(title="삼성전자 실적 서프라이즈 단신", summary="영업이익 급증 요약",
                  url="https://n.news/1", source="naver"),
    ]

    async def _fake_fetch(symbol):
        return dup_items

    curator._fetch_symbol_news = _fake_fetch
    data = asyncio.run(curator.get_symbol_sentiment("005930"))

    assert data["items"] == 1
    assert data["dedup_removed"] == 1
    assert data["item_ids"] == ["https://n.news/1"]


def test_news_analyst_evidence_and_limitations_reflect_dedup():
    class _FakeOrch:
        async def get_news_sentiment(self, symbol):
            return {"score": 10, "tags": [], "items": 1,
                     "item_ids": ["https://n.news/1"], "dedup_removed": 1}

    na = NewsAnalyst(orchestrator=_FakeOrch())
    report = asyncio.run(na.analyze("005930"))

    assert len(report.evidence) == 1
    assert report.evidence[0].dedup_key == "https://n.news/1"
    assert report.evidence[0].ref_id == "https://n.news/1"
    assert report.metrics["dedup_removed"] == 1
    assert "헤드라인 기반" in report.limitations
    assert report.observed_at is None


# ── stock_validator: validated/data_status 가 실제 검증 수행 여부를 반영 ──

def _wire_offline_checks(sv, *, mcp_manager=None, news_raises=False):
    """StockValidator의 5개 _safe_check_* 를 네트워크 없는 순수 함수로 교체한다."""
    sv._mcp_manager = mcp_manager

    async def _news(symbol, name):
        if news_raises:
            raise RuntimeError("네트워크 차단(테스트)")
        return NewsCheckResult()

    async def _dart(symbol):
        return DartCheckResult()

    async def _sd(symbol):
        return SupplyDemandResult()

    async def _ss(symbol):
        return ShortSellingResult()

    async def _tb(name):
        return TrendBuzzResult()

    sv._safe_check_news = _news
    sv._safe_check_dart = _dart
    sv._safe_check_supply_demand = _sd
    sv._safe_check_short_selling = _ss
    sv._safe_check_trend_buzz = _tb


def test_stock_validator_insufficient_when_mcp_not_connected():
    sv = StockValidator()
    _wire_offline_checks(sv, mcp_manager=None)
    result = asyncio.run(sv.validate("005930", "삼성전자"))
    assert result.approved is True          # 기존 approved 의미·값 불변
    assert result.validated is False
    assert result.data_status == "insufficient"


def test_stock_validator_full_when_mcp_connected():
    sv = StockValidator()
    _wire_offline_checks(sv, mcp_manager=SimpleNamespace(is_server_available=lambda name: True))
    result = asyncio.run(sv.validate("005930", "삼성전자"))
    assert result.approved is True
    assert result.validated is True
    assert result.data_status == "full"


def test_stock_validator_exception_marks_error_but_keeps_approved_true():
    sv = StockValidator()
    _wire_offline_checks(sv, mcp_manager=None, news_raises=True)
    result = asyncio.run(sv.validate("005930", "삼성전자"))
    assert result.approved is True          # 기존 소비자(batch 검증 흐름) 동작 그대로
    assert result.validated is False
    assert result.data_status == "error"
