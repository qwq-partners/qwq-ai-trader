"""팀 심의 입력 정직화(2026-11-14) — 미검증 재무 보고의 가산·가중치·수급 기본값 제거, 자동매도 금지 보유 종목 제외."""
import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from src.agents import analysts
from src.agents.analysts import AnalystTeam, FundamentalAnalyst
from src.agents.researchers import ResearchTeam
from src.agents.types import AnalystKind, AnalystReport
from src.signals.fundamentals.stock_validator import ShortSellingResult, SupplyDemandResult, ValidationResult
from test_t11_entry_plan import SYM, _collect_deliberations, _sched_bot


@pytest.fixture(autouse=True)
def boost_off(monkeypatch):
    """운영과 같은 조건 — 사이징 부스트가 꺼져 있어야 새 입력 규칙이 적용된다."""
    monkeypatch.setenv("TEAM_CONVICTION", "0")


def _fundamental(dart_items, dart_fetched=True, **result):
    """운영 검증기가 9/15 이후 실제로 돌려주는 모양(approved=True·validated=False·insufficient·수급 기본값)이 기본."""
    base = dict(approved=True, validated=False, data_status="insufficient",
                supply_demand_result=SupplyDemandResult(), short_selling_result=ShortSellingResult())

    async def validate(_symbol, _name):
        return ValidationResult(**{**base, **result})

    async def check_disclosures(_symbol, days=7):
        return SimpleNamespace(has_risk=len(dart_items) > 0, risk_disclosures=list(dart_items), fetched=dart_fetched)

    return asyncio.run(FundamentalAnalyst(SimpleNamespace(validate=validate),
                                          SimpleNamespace(check_disclosures=check_disclosures)).analyze("005930", "삼성전자"))


def test_unvalidated_pass_gets_no_bonus_no_weight_and_supply_stays_unobserved():
    r = _fundamental([])
    assert (r.score, r.confidence, r.validation_pass_bonus, r.data_status) == (0, 0.0, 0, "insufficient")
    assert r.metrics == {"foreign_net_buying": None, "institutional_net_buying": None, "dart_risk_items": 0}
    assert r.summary == "공시 위험 신호 없음 — 수급·재무 자료 미연결" and r.positive_basis is None and r.risk_clear is None
    tech = AnalystReport(kind=AnalystKind.TECHNICAL, symbol="005930", score=40, summary="지표", confidence=0.7,
                         data_as_of=datetime.now())
    assert AnalystTeam.aggregate_score([r, tech]) == 40          # 가중치 0 — 다른 관점을 희석하지 않는다
    assert "[fundamental] 점수 +0" in ResearchTeam._context("005930", "삼성전자", [r, tech])
    assert "수급·재무 자료 미연결" in ResearchTeam._context("005930", "삼성전자", [r, tech])


def test_failed_disclosure_lookup_is_not_reported_as_no_risk():
    r = _fundamental([], dart_fetched=False)   # 키 미설정·종목 미매핑·HTTP 실패의 기본값
    assert r.metrics["dart_risk_items"] is None and r.summary == "공시 자료 미획득 — 수급·재무 자료 미연결"
    assert (r.score, r.confidence) == (0, 0.0)


def test_unvalidated_result_with_disclosure_risk_scores_only_the_risk():
    r = _fundamental(["유상증자 결정"])
    assert (r.score, r.confidence, r.validation_pass_bonus, r.risk_clear) == (-30, 0.8, 0, False)
    assert r.summary == "공시 위험 신호 1건" and r.metrics["foreign_net_buying"] is None


def test_validated_result_keeps_bonus_weight_and_observed_supply():
    r = _fundamental([], validated=True, data_status="full",
                     supply_demand_result=SupplyDemandResult(foreign_net_buying=True, institutional_net_buying=False))
    assert (r.score, r.confidence, r.validation_pass_bonus) == (20, 0.7, 10)   # 외국인 +10, 검증 통과 +10
    assert r.metrics["foreign_net_buying"] is True and r.metrics["institutional_net_buying"] is False   # 관측된 False 는 보존


def test_exit_exempt_holding_is_not_deliberated(monkeypatch):
    at = datetime(2026, 11, 13, 10, 25)
    bot = _sched_bot(screened_at=at)
    bot.exit_manager = SimpleNamespace(is_exit_exempt=lambda symbol: symbol == SYM)
    calls = _collect_deliberations(monkeypatch, bot)
    assert [c for c in calls if c["holding"]] == [] and [c for c in calls if not c["holding"]]   # 후보 심의는 그대로
    bot.exit_manager = SimpleNamespace(is_exit_exempt=lambda symbol: False)
    assert [c["items"][0]["symbol"] for c in _collect_deliberations(monkeypatch, bot) if c["holding"]] == [SYM]


@pytest.mark.parametrize("value", ["1", None])
def test_legacy_rule_stays_when_the_sizing_boost_is_on(monkeypatch, value):
    """부스트가 켜진 환경(기본값 포함)에서는 종전 규칙 — 새 규칙의 판정이 신규 매수 사이징에 닿지 않는다."""
    if value is None:
        monkeypatch.delenv("TEAM_CONVICTION")
    else:
        monkeypatch.setenv("TEAM_CONVICTION", value)
    assert analysts.input_honesty_active() is False and analysts.analyst_input_version() is None
    r = _fundamental([], dart_fetched=False)
    assert (r.score, r.confidence, r.validation_pass_bonus, r.summary) == (10, 0.7, 10, "특이사항 없음")
    assert r.metrics == {"foreign_net_buying": False, "institutional_net_buying": False, "dart_risk_items": 0}
    risk = _fundamental(["유상증자 결정"])
    assert (risk.score, risk.confidence, risk.validation_pass_bonus) == (-20, 0.8, 10)


def test_new_rule_is_marked_in_the_ledger_only_when_active():
    assert analysts.input_honesty_active() is True
    assert analysts.analyst_input_version() == "analyst-input-2026-11-14"
