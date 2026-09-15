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


class _FakeDart:
    def __init__(self, result):
        self._result = result

    async def check_disclosures(self, symbol, days=7):
        return self._result


def test_fundamental_dart_risk_overrides_validator_risk_clear_and_reads_correct_key():
    """리뷰 blocking B1/B2 (2026-09-15) 재현: validator는 위험을 못 찾아
    approved=True(risk_clear 후보 True)지만, DART가 별도 공시 위험을 발견하면
    - B1: 생산자 키는 risk_disclosures 다 — metrics['dart_risk_items']가
      실제 건수(3)를 반영해야 한다(이전엔 존재하지 않는 risk_items를 읽어 항상 0).
    - B2: '검증 통과'를 risk_clear=True로 남겨두면 안 된다 — DART 위험 발견 시
      risk_clear는 False로 내려가고, 앞서 쌓인 '위험 미발견' evidence도 정정된다.
    """
    validator_result = _validation_result(approved=True, validated=True, data_status="full")
    dart_result = SimpleNamespace(
        has_risk=True,
        risk_disclosures=["유상증자 결정", "최대주주 변경", "감자 결정"],
        risk_level="warning",
    )
    fa = FundamentalAnalyst(
        stock_validator=_FakeValidator(validator_result),
        dart_checker=_FakeDart(dart_result),
    )
    report = asyncio.run(fa.analyze("005930", "테스트"))

    assert report.metrics["dart_risk_items"] == 3
    assert report.risk_clear is False
    assert not any(
        e.metric == "risk_clear" and e.value is True for e in report.evidence
    ), "DART 위험 발견 후에도 '위험 미발견' 근거가 True로 남아있으면 안 된다"
    assert report.score == -20  # validator 통과 +10, DART 위험 -30


def test_dart_checker_producer_key_set_covers_analyst_consumer_keys():
    """required_fix (B1 재발 차단): technical 계약 테스트와 같은 패턴 —
    DartCheckResult 필드 집합이 analysts.py의 소비 키(has_risk, risk_disclosures)를
    전부 포함해야 한다. risk_items 같은 존재하지 않는 키로 되돌아가면 여기서 잡힌다."""
    import dataclasses

    from src.signals.fundamentals.dart_checker import DartCheckResult

    producer_fields = {f.name for f in dataclasses.fields(DartCheckResult)}
    consumer_keys = {"has_risk", "risk_disclosures"}
    assert consumer_keys <= producer_fields


def test_fundamental_dart_risk_without_details_is_partial_not_zero_fact():
    """required_fix: has_risk=True인데 risk_disclosures가 비었으면 0건이라는
    '사실'을 만들어내지 않고 결측(status=partial, value=None)으로 남긴다."""
    dart_result = SimpleNamespace(has_risk=True, risk_disclosures=[], risk_level="warning")
    fa = FundamentalAnalyst(stock_validator=None, dart_checker=_FakeDart(dart_result))
    report = asyncio.run(fa.analyze("005930", "테스트"))

    dart_ev = [e for e in report.evidence if e.source == "dart_checker"]
    assert len(dart_ev) == 1
    assert dart_ev[0].status == "partial"
    assert dart_ev[0].value is None


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
    # 리뷰 지적(2026-09-15): 헤드라인이 비슷하면 토큰 Jaccard 유사도만으로도
    # 우연히 dedup 되어 URL 기준 dedup 이 실제로 배선됐는지 못 가린다.
    # 아래 두 헤드라인은 토큰 교집합이 1개("반도체")뿐이라 Jaccard < 0.6 —
    # 같은 URL 이라는 사실만으로 dedup 되는지를 검증한다.
    dup_items = [
        NewsItem(title="삼성전자 3분기 영업이익 급증", summary="반도체 회복",
                  url="https://n.news/1", source="naver"),
        NewsItem(title="[속보] 반도체 대장주 어닝 서프라이즈 기록", summary="시장 기대치 상회 전망 우세",
                  url="https://n.news/1", source="naver"),
    ]

    async def _fake_fetch(symbol):
        return dup_items

    curator._fetch_symbol_news = _fake_fetch
    data = asyncio.run(curator.get_symbol_sentiment("005930"))

    assert data["items"] == 1
    assert data["dedup_removed"] == 1
    assert data["item_ids"] == ["https://n.news/1"]


def test_news_curator_market_analyze_does_not_share_symbol_url_dedup():
    """R-A r3 blocking #1 (2026-09-15) 수정 확인: URL 선행 dedup을 종목별 경로
    (get_symbol_sentiment) 전용으로 옮겼으므로, 시장 뉴스 경로(_analyze)는
    046277f(도입 전) 그대로 Jaccard만 적용한다 — 같은 URL이라도 헤드라인 토큰
    교집합이 낮으면(Jaccard < 0.6) 1건으로 뭉개지지 않고 2건 그대로 집계돼야 한다.
    이전 버전(95afccc)은 공유 _deduplicate에 URL dedup이 섞여 있어 1건으로
    잘못 집계됐다(그 값을 '특성화'로 잘못 고정한 것이 이 테스트가 대체하는 이전 버전)."""
    from src.experts.news_curator import NewsCurator, NewsItem

    curator = NewsCurator(ExpertConfig())
    dup_items = [
        NewsItem(title="삼성전자 3분기 영업이익 급증", summary="반도체 회복",
                  url="https://n.news/1", source="naver"),
        NewsItem(title="[속보] 반도체 대장주 어닝 서프라이즈 기록", summary="시장 기대치 상회",
                  url="https://n.news/1", source="naver"),
    ]

    async def _kr(self=None):
        return dup_items

    async def _global(self=None):
        return []

    curator._fetch_kr_market_news = _kr
    curator._fetch_global_speculative = _global

    opinion = asyncio.run(curator._analyze())
    assert opinion.raw_evidence["item_count"] == 2


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

def _wire_offline_checks(sv, *, mcp_manager=None, news_raises=False,
                          sd_ok=True, ss_ok=True):
    """StockValidator의 5개 _safe_check_* 를 네트워크 없는 순수 함수로 교체한다.

    R-A r3 blocking #2 (2026-09-15): _safe_check_supply_demand/_safe_check_short_selling
    은 이제 (결과, ok) 튜플을 낸다 — 이 fake 도 실제 시그니처를 따른다.
    sd_ok/ss_ok 로 "실제로 조회에 성공했는가"를 개별 제어할 수 있다
    (기본값 True=이상적 성공 시나리오, 집계 로직 자체를 검증하는 3개 테스트용).
    """
    sv._mcp_manager = mcp_manager

    async def _news(symbol, name):
        if news_raises:
            raise RuntimeError("네트워크 차단(테스트)")
        return NewsCheckResult()

    async def _dart(symbol):
        return DartCheckResult()

    async def _sd(symbol):
        return SupplyDemandResult(), sd_ok

    async def _ss(symbol):
        return ShortSellingResult(), ss_ok

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


# ── stock_validator: 하위 검증 실제 획득 여부에서 validated/data_status 유도 ──
# R-A r3 blocking #2 (2026-09-15): mcp_ok(서버 연결 여부)만으로 판단하던 이전 버전은
# "연결은 됐지만 조회는 실패"를 "검증 통과"로 잘못 보고했다. 아래 두 테스트는
# _safe_check_supply_demand/_safe_check_short_selling 을 목으로 대체하지 않고
# 실제 구현을 그대로 태워 그 구분을 확인한다.

def test_stock_validator_partial_when_short_selling_tool_structurally_absent():
    """MCP 연결 + 수급 조회 성공, 그러나 공매도는 pykrx-mcp에 도구가 없어
    (_safe_check_short_selling 이 항상 ok=False) — 이건 '실패'가 아니라
    구조적 영구 공백이므로 insufficient 가 아니라 partial 이어야 한다."""
    sv = StockValidator()
    sv._mcp_manager = SimpleNamespace(is_server_available=lambda name: True)
    sv._safe_check_news = lambda symbol, name: _async_return(NewsCheckResult())
    sv._safe_check_dart = lambda symbol: _async_return(DartCheckResult())
    sv._safe_check_trend_buzz = lambda name: _async_return(TrendBuzzResult())
    # 실제 _fetch_supply_demand 는 호출하지 않고 캐시 경로로 성공을 모사한다.
    sv._get_cache = lambda cache, key, ttl: SupplyDemandResult(foreign_net_buying=True)
    # _safe_check_short_selling 은 실제 구현 그대로(패치 안 함) — 항상 (default, False).

    result = asyncio.run(sv.validate("005930", "삼성전자"))
    assert result.validated is True
    assert result.data_status == "partial"
    assert result.supply_demand_result.foreign_net_buying is True


def test_stock_validator_insufficient_when_supply_demand_fetch_raises():
    """MCP는 연결됐지만 _fetch_supply_demand 내부에서 예외가 나면
    _safe_check_supply_demand 는 그 예외를 삼키고 기본값을 돌려준다(실제 구현) —
    이 경우는 '실패'이므로 partial 이 아니라 insufficient 여야 한다."""
    sv = StockValidator()
    sv._mcp_manager = SimpleNamespace(is_server_available=lambda name: True)
    sv._safe_check_news = lambda symbol, name: _async_return(NewsCheckResult())
    sv._safe_check_dart = lambda symbol: _async_return(DartCheckResult())
    sv._safe_check_trend_buzz = lambda name: _async_return(TrendBuzzResult())
    sv._get_cache = lambda cache, key, ttl: None

    async def _raise(symbol):
        raise RuntimeError("MCP 응답 파싱 실패(테스트)")

    sv._fetch_supply_demand = _raise
    # _safe_check_short_selling 은 실제 구현 그대로 — 항상 (default, False).

    result = asyncio.run(sv.validate("005930", "삼성전자"))
    assert result.approved is True
    assert result.validated is False
    assert result.data_status == "insufficient"


async def _async_return(value):
    return value
