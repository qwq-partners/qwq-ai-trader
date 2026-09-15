"""T11-A 근거 계약 — 기준선 특성화 테스트 (2026-09-15).

AnalystTeam.aggregate_score/evidence_quality 의 기존 산식을 리뷰 사례로 고정한다.
이 파일의 값은 T11-A 작업 이후에도 그대로 통과해야 한다 — 단 아래 3건(C4/C5,
DART risk_disclosures 키 정정 B1)은 근거를 명시한 버그 수정이며, 046277f(T11
도입 전)에서는 실패한다. 검증 방법(git stash 대신): `git show 046277f:<path>`로
기준 파일을 꺼내 별도로 돌리거나, 이 파일처럼 기대값을 코드가 아니라 리터럴
상수로 못박아 둔다 — 각 테스트는 score/confidence 산식을 손으로 계산한 값이다.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from src.agents.analysts import AnalystTeam, FundamentalAnalyst, NewsAnalyst, TechnicalAnalyst
from src.agents.types import AnalystKind, AnalystReport


def _report(kind: AnalystKind, score: int, confidence: float, age_minutes: float) -> AnalystReport:
    return AnalystReport(
        kind=kind, symbol="005930", score=score, confidence=confidence,
        data_as_of=datetime.now() - timedelta(minutes=age_minutes),
    )


def test_aggregate_score_review_case_plus28():
    """계획서(docs/superpowers/plans/2026-09-15-agent-team-evidence-entryplan.md) §1 A2 사례:
    펀더 +10/0.7/15분·기술 +25/0.7/4분·뉴스 +52/0.7/30분 → 감쇠 근거량≈1.75, 종합 +28.
    aggregate_score 산식은 T11-A 에서 변경하지 않았으므로 baseline 그대로 유지된다.
    """
    reports = [
        _report(AnalystKind.FUNDAMENTAL, 10, 0.7, 15),
        _report(AnalystKind.TECHNICAL, 25, 0.7, 4),
        _report(AnalystKind.NEWS, 52, 0.7, 30),
    ]
    total_w = sum(r.freshness_decayed_confidence() for r in reports)
    assert total_w == pytest.approx(1.75, abs=0.01)
    assert AnalystTeam.aggregate_score(reports) == 28


def test_technical_analyst_volume_surge_bug_fix():
    """C5 (버그 수정, 근거): 운영 경로(kr_scheduler._cached_indicators → 스크리너)가
    실제로 쓰는 생산자는 TechnicalIndicators.calculate_all이며, 거래량 지표 키로
    'vol_ratio'를 낸다 (별도 계약 테스트:
    test_technical_indicator_producer_key_set_covers_analyst_consumer_keys).
    수정 전 analysts.py의 TechnicalAnalyst는 존재하지 않는 'volume_ratio' 키를
    읽었기 때문에 vol_ratio 값과 무관하게 거래량 급증(+15) 신호가 절대 발동하지 않았다.
    이 테스트는 그 소비 키 자체를 vol_ratio 로 직접 검증한다 — 수정 전에는
    실패했다(findings에 '거래량 급증'이 없고 score==0). 모듈 함수
    compute_indicators(technical.py:144~168)는 rsi/atr_pct 이름이 달라 이
    계약 밖이며(advisory, 2026-09-15), 운영에서 이 dict가 팀에 들어오는 경로는
    현재 없다.
    """
    import asyncio

    ta = TechnicalAnalyst()
    report = asyncio.run(ta.analyze(
        "005930", indicators={"vol_ratio": 3.0}, indicators_as_of=datetime.now(),
    ))
    assert any("거래량 급증" in f for f in report.findings)
    assert report.score >= 15


def test_evidence_quality_excludes_zero_confidence_bug_fix():
    """C4 (버그 수정, 근거): confidence=0("정보 없음")인 보고서는 error가 없어 r.ok=True다.
    aggregate_score는 가중치 0이라 이런 보고서가 섞여도 점수엔 영향이 없지만,
    evidence_quality는 '유효 소스 수'를 직접 세는 게 목적이라 정보가 전혀 없는 보고서까지
    소스로 잡으면 근거량을 부풀린다. MIN_VALID_SOURCES=2인 상황에서 confidence=0 보고서
    1건 + 진짜 유효 보고서 1건을 넣으면:
      - 수정 전: len(valid)==2 → 통과(ok=True)로 오판 — 이 테스트는 수정 전 실패했다.
      - 수정 후: len(valid)==1 → 정직하게 근거 부족(ok=False)으로 판정.
    """
    zero_conf = _report(AnalystKind.FUNDAMENTAL, 0, 0.0, 1)
    real = _report(AnalystKind.TECHNICAL, 25, 0.7, 4)
    ok, reason, _total_w = AnalystTeam.evidence_quality([zero_conf, real])
    assert ok is False
    assert "유효 근거 1개" in reason


# ── (a) FundamentalAnalyst — 기준선 score/confidence/findings/metrics ────────

def _fake_validation(**overrides):
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


def test_fundamental_validated_pass_plus_foreign_inst_buying_baseline():
    """046277f(T11 도입 전)에서도 그대로 통과 — score/confidence/findings/metrics
    산식은 T11-A 에서 바뀌지 않았다. 검증 통과(+10) + 외국인·기관 동반 순매수(+30)
    만 있고 DART 는 미연결(dart_checker=None) — DART 키 정정(B1)의 영향을 받지
    않는 순수 기준선."""
    import asyncio

    result = _fake_validation(
        approved=True, validated=True, data_status="full",
        supply_demand_result=SimpleNamespace(foreign_net_buying=True, institutional_net_buying=True),
    )
    fa = FundamentalAnalyst(stock_validator=_FakeValidator(result), dart_checker=None)
    report = asyncio.run(fa.analyze("005930", "테스트"))

    assert report.score == 40          # 외국인·기관 동반 순매수 +30, 검증 통과 +10
    assert report.confidence == 0.7
    assert report.findings == ["외국인·기관 동반 순매수"]
    assert report.metrics == {"foreign_net_buying": True, "institutional_net_buying": True}


def test_fundamental_validated_pass_plus_foreign_inst_plus_dart_risk_bug_fix():
    """승인된 예외 (B1, 버그 수정, 2026-09-15): DART 소비 키가 존재하지 않는
    'risk_items'에서 실제 생산자 키 'risk_disclosures'(dart_checker.py)로
    정정됐다 — 046277f 는 항상 dart_risk_items=0/"공시 위험 신호 0건"을
    "사실"로 냈다(버그, risk_disclosures=3건이 들어와도 무시됨). 이 테스트는
    수정 후 값을 고정한다 — 046277f 에서는 실패한다(score/findings/metrics 불일치)."""
    import asyncio

    result = _fake_validation(
        approved=True, validated=True, data_status="full",
        supply_demand_result=SimpleNamespace(foreign_net_buying=True, institutional_net_buying=True),
    )
    dart_result = SimpleNamespace(
        has_risk=True,
        risk_disclosures=["유상증자 결정", "최대주주 변경", "감자 결정"],
        risk_level="warning",
    )

    class _FakeDart:
        async def check_disclosures(self, symbol, days=7):
            return dart_result

    fa = FundamentalAnalyst(stock_validator=_FakeValidator(result), dart_checker=_FakeDart())
    report = asyncio.run(fa.analyze("005930", "테스트"))

    assert report.score == 10          # +30(동반 순매수) +10(검증 통과) -30(DART 위험)
    assert report.confidence == 0.8    # DART 위험 감지 시 max(0.7, 0.8)
    assert report.findings == ["외국인·기관 동반 순매수", "공시 위험 신호 3건"]
    assert report.metrics == {
        "foreign_net_buying": True, "institutional_net_buying": True,
        "dart_risk_items": 3,
    }


# ── (b) TechnicalAnalyst — rsi/ma200/atr 조합별 score·confidence·data_as_of ──

def test_technical_analyst_rsi_ma200_combo_confidence_0_7_baseline():
    """046277f 에서도 그대로 통과. RSI 중립 구간(+10) + MA200 상단 건전 구간(+25),
    ATR/거래량은 트리거 없음 — findings 가 있으므로 confidence=0.7,
    data_as_of 는 호출자가 넘긴 indicators_as_of 그대로 쓴다."""
    import asyncio

    now = datetime(2026, 9, 15, 10, 0)
    ta = TechnicalAnalyst()
    report = asyncio.run(ta.analyze(
        "005930",
        indicators={"rsi_14": 50.0, "ma200_distance_pct": 10.0, "atr_14": 3.0},
        indicators_as_of=now,
    ))
    assert report.score == 35
    assert report.confidence == 0.7
    assert report.data_as_of == now
    assert any("RSI 중립 구간" in f for f in report.findings)
    assert any("MA200 상단 건전 구간" in f for f in report.findings)


def test_technical_analyst_no_trigger_combo_confidence_0_4_baseline():
    """046277f 에서도 그대로 통과. RSI 65~75 구간, MA200 25~40% 이격, ATR ≤7% —
    세 지표 모두 findings 를 만드는 임계값을 넘지 않아 score=0/confidence=0.4
    (findings 없음일 때의 기본값)로 떨어진다."""
    import asyncio

    now = datetime(2026, 9, 15, 10, 0)
    ta = TechnicalAnalyst()
    report = asyncio.run(ta.analyze(
        "005930",
        indicators={"rsi_14": 70.0, "ma200_distance_pct": 30.0, "atr_14": 5.0},
        indicators_as_of=now,
    ))
    assert report.score == 0
    assert report.confidence == 0.4
    assert report.data_as_of == now
    assert report.findings == []


# ── (c) NewsAnalyst — items/tags 유무별 score·confidence·data_as_of ──────────

class _FakeOrchNews:
    def __init__(self, data):
        self._data = data

    async def get_news_sentiment(self, symbol):
        return self._data


@pytest.mark.parametrize(
    "data,expected_score,expected_confidence",
    [
        ({"score": 10, "tags": ["규제"], "items": 3}, 10, 0.7),   # 기사+태그 있음
        ({"score": -5, "tags": [], "items": 2}, -5, 0.5),         # 기사 있음, 태그 없음
        ({"score": 0, "tags": [], "items": 0}, 0, 0.2),           # 기사 없음(items<=0)
    ],
)
def test_news_analyst_items_tags_combo_confidence_baseline(data, expected_score, expected_confidence):
    """046277f 에서도 그대로 통과 — score/confidence 산식은 T11-A 에서 바뀌지
    않았다(item_ids/dedup_removed 는 신규 shadow 필드라 이 산식과 무관)."""
    import asyncio

    na = NewsAnalyst(orchestrator=_FakeOrchNews(data))
    before = datetime.now()
    report = asyncio.run(na.analyze("005930"))
    after = datetime.now()

    assert report.score == expected_score
    assert report.confidence == expected_confidence
    # data_as_of = now - 30분 (호출 시점 기준, 초 단위 오차 허용)
    assert (before - timedelta(minutes=30)) <= report.data_as_of <= (after - timedelta(minutes=30))
