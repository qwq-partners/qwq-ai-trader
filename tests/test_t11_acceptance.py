"""T11 담당 D — 독립 인수 검증 (2026-09-15).

구현자(A/B/C/D 담당)의 테스트와 무관하게, 계획서
`docs/superpowers/plans/2026-09-15-agent-team-evidence-entryplan.md` §4 의 인수 조건
14건을 **독립 합성 입력**으로 고정한다. 기존 진입점만 사용한다(내부 헬퍼 단정 금지):
    src/agents/analysts.py, src/signals/fundamentals/stock_validator.py,
    src/agents/judgment.assess, src/agents/researchers.ResearchTeam(가짜 LLM),
    src/agents/team.TradingTeam._deliberate/deliberate_many(대역 협력자),
    src/agents/team_ledger, src/execution/entry_plan.check_entry_plan,
    src/core/batch_analyzer(PendingSignal→Signal 변환·_to_pending_signal),
    src/core/engine.RiskManager.on_signal(대역 engine/portfolio/broker),
    src/analytics/counterfactual_tracker, scripts/team_policy_ab

격리: 운영 체크아웃·캐시·.env·네트워크·주문·systemd 접근 금지. `Path.home()`/모듈
경로 상수를 tmp_path 로 monkeypatch 하고, LLM·브로커·DB 는 전부 인메모리 대역이다.
HOME 환경변수 자체는 변조하지 않는다(Path.home 클래스메서드만 패치).

실행:
    venv/bin/python -m pytest tests/test_t11_acceptance.py -q -p no:cacheprovider
"""
from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from src.agents.analysts import (  # noqa: E402
    AnalystTeam, FundamentalAnalyst, NewsAnalyst, TechnicalAnalyst,
)
from src.agents import judgment  # noqa: E402
from src.agents import team as team_mod  # noqa: E402
from src.agents import team_ledger  # noqa: E402
from src.agents import reproducibility as repro_mod  # noqa: E402
from src.agents.researchers import ResearchTeam  # noqa: E402
from src.agents.team import TradingTeam  # noqa: E402
from src.agents.types import (  # noqa: E402
    AnalystKind, AnalystReport, DebateResult, DebateTurn, EvidenceItem, Stance,
)
from src.signals.fundamentals.stock_validator import (  # noqa: E402
    SupplyDemandResult, ValidationResult,
)
from src.execution.entry_plan import PlanCheck, check_entry_plan  # noqa: E402
from src.core.batch_analyzer import BatchAnalyzer, PendingSignal  # noqa: E402
from src.core.event import SignalEvent  # noqa: E402
from src.core.types import (  # noqa: E402
    MarketSession, OrderSide, OrderType, RiskConfig, Signal, SignalStrength, StrategyType,
)
from src.core import engine as engine_mod  # noqa: E402
from src.core.engine import RiskManager  # noqa: E402
from src.analytics import counterfactual_tracker as cf_mod  # noqa: E402
from src.utils.llm import LLMProvider  # noqa: E402

import team_policy_ab as tpab  # noqa: E402


# ══════════════════════════════════════════════════════════════════════════
# 공용 격리 픽스처 — Path.home() 및 모듈 상수를 tmp_path 로 리다이렉트한다.
# (모듈 로드 시점에 Path.home()으로 이미 바인딩된 LEDGER_DIR/RESULT_DIR 류는
#  Path.home 패치만으로는 바뀌지 않으므로 모듈 속성 자체를 patch 한다.)
# ══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def iso(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(repro_mod, "LEDGER_DIR", tmp_path / "llm_ledger")
    monkeypatch.setattr(team_ledger, "LEDGER_DIR", tmp_path / "team_ledger")
    monkeypatch.setattr(team_mod, "RESULT_DIR", tmp_path / "team_verdicts")
    (tmp_path / "team_verdicts").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cf_mod, "_STATE_PATH", tmp_path / "cf_state.json")
    monkeypatch.setattr(cf_mod, "_SOURCES", {})
    monkeypatch.setattr(cf_mod, "_TEAM_VERDICT_DIR", tmp_path / "team_verdicts")
    (tmp_path / "journal").mkdir(exist_ok=True)
    monkeypatch.setattr(cf_mod, "_TRADE_JOURNAL_DIR", tmp_path / "journal")  # 빈 저널 = 그날 진입 없음
    return tmp_path


# ══════════════════════════════════════════════════════════════════════════
# 조건 #1 — 근거 없는 검증 통과가 신규 정책에서 긍정 근거로 승격되지 않음
#   entry point: analysts.FundamentalAnalyst + stock_validator.ValidationResult
#                + judgment.assess
# ══════════════════════════════════════════════════════════════════════════

class _FakeValidatorPassNoSignal:
    """검증 통과(approved=True)이지만 순매수 등 실제 긍정 신호는 없다."""

    async def validate(self, symbol, name):
        return ValidationResult(
            approved=True, validated=True, data_status="full",
            block_reason="", supply_demand_result=None,
            short_selling_result=None, trend_buzz_result=None,
        )


class _FakeValidatorPassWithSignal:
    async def validate(self, symbol, name):
        return ValidationResult(
            approved=True, validated=True, data_status="full",
            block_reason="",
            supply_demand_result=SupplyDemandResult(
                foreign_net_buying=True, institutional_net_buying=True,
            ),
            short_selling_result=None, trend_buzz_result=None,
        )


def test_cond1_validated_pass_alone_is_not_positive_basis():
    fa = FundamentalAnalyst(stock_validator=_FakeValidatorPassNoSignal(), dart_checker=None)
    report = asyncio.run(fa.analyze("035420", "NAVER"))

    # 기존 산식(돈 경로) 은 불변 — '검증 통과' 자체는 여전히 +10.
    assert report.score == 10
    assert report.risk_clear is True
    # 그러나 실제 확인된 긍정 신호(순매수 등)가 없으므로 positive_basis 는 True 가 아니다.
    assert report.positive_basis is not True

    assessment = judgment.assess("035420", [report], None)
    # risk_clear 만의 +10 은 evidence 기반 merit 계산에서 취소된다 — sufficient 로 승격 불가.
    assert assessment.merit_status != "sufficient"
    assert assessment.merit_score is not None and assessment.merit_score <= 0
    assert assessment.stance_v2 != "buy_candidate"


def test_cond1_real_positive_signal_is_not_clipped():
    """대조군: 실제 순매수 신호가 있으면 merit 이 정상적으로 반영된다(과도한 억제가 아님을 확인)."""
    fa = FundamentalAnalyst(stock_validator=_FakeValidatorPassWithSignal(), dart_checker=None)
    report = asyncio.run(fa.analyze("035420", "NAVER"))
    assert report.positive_basis is True
    assessment = judgment.assess("035420", [report], None)
    assert assessment.merit_score is not None and assessment.merit_score > 0


# ══════════════════════════════════════════════════════════════════════════
# 조건 #2 — confidence=0·결측·오류·만료 보고서가 유효 소스를 부풀리지 않음
#   entry point: analysts.py(AnalystTeam.evidence_quality) + judgment.assess
# ══════════════════════════════════════════════════════════════════════════

def test_cond2_confidence_zero_report_excluded_from_valid_sources():
    news = asyncio.run(NewsAnalyst(orchestrator=None).analyze("005930", "삼성전자"))
    assert news.confidence == 0.0
    ok, reason, _w = AnalystTeam.evidence_quality([news])
    assert ok is False  # 단독으로는 유효 소스 0개

    assessment = judgment.assess("005930", [news], None)
    assert assessment.data_sufficiency == "insufficient"
    assert assessment.merit_status == "abstain"


def test_cond2_error_status_report_does_not_inflate_merit_or_data_sufficiency():
    """StockValidator.validate() 가 내부에서 예외를 삼켜 validated=False/data_status='error' 를
    돌려준 경우 — analysts.py 는 '기존 산식 불변' 규칙상 여전히 score=10 을 매길 수 있지만
    (approved 기본값이 True 라서) shadow 판단(merit/data_sufficiency)은 그 report 를 유효
    소스로 세면 안 된다."""

    class _FakeValidatorInternalError:
        async def validate(self, symbol, name):
            return ValidationResult(approved=True, validated=False, data_status="error")

    fa = FundamentalAnalyst(stock_validator=_FakeValidatorInternalError(), dart_checker=None)
    report = asyncio.run(fa.analyze("035420", "NAVER"))
    assert report.data_status == "error"
    # risk_clear 는 '위험 미발견'을 주장하지 않는다 — 검증 자체가 안 됐다.
    assert report.risk_clear is None

    assessment = judgment.assess("035420", [report], None)
    assert assessment.data_sufficiency == "insufficient"
    assert assessment.merit_status == "abstain"
    assert assessment.merit_score is None


def test_cond2_expired_report_excluded():
    old = AnalystReport(
        kind=AnalystKind.TECHNICAL, symbol="005930", score=40, confidence=0.9,
        data_status="full", data_as_of=datetime.now() - timedelta(minutes=90),
        evidence=[EvidenceItem(source="technical.indicators", metric="rsi_14",
                                value=70, status="full", kind="fact")],
    )
    assert AnalystTeam.is_expired(old) is True  # technical HARD_TTL=45분
    assessment = judgment.assess("005930", [old], None)
    assert assessment.data_sufficiency == "insufficient"
    assert assessment.merit_score is None


def test_cond2_mixed_batch_only_genuinely_valid_report_counts():
    """confidence=0 + 만료 + error 3건 + 진짜 유효 1건을 섞어도 data_sufficiency 는
    유효 1건 기준으로만 판정된다(4건 전체를 기준으로 'full'로 부풀지 않는다)."""
    conf0 = AnalystReport(kind=AnalystKind.NEWS, symbol="005930", score=0, confidence=0.0,
                          data_status="insufficient")
    expired = AnalystReport(kind=AnalystKind.TECHNICAL, symbol="005930", score=40,
                            confidence=0.9, data_status="full",
                            data_as_of=datetime.now() - timedelta(minutes=90))
    errored = AnalystReport(kind=AnalystKind.FUNDAMENTAL, symbol="005930", score=10,
                            confidence=0.7, data_status="error")
    valid = AnalystReport(
        kind=AnalystKind.TECHNICAL, symbol="005930", score=15, confidence=0.7,
        data_status="full", data_as_of=datetime.now() - timedelta(minutes=1),
        evidence=[EvidenceItem(source="technical.indicators", metric="rsi_14",
                                value=50, status="full", kind="fact")],
    )
    assessment = judgment.assess("005930", [conf0, expired, errored, valid], None)
    # full >= 2 여야 'full' — 유효 소스는 1건뿐이므로 최대 partial
    assert assessment.data_sufficiency in ("partial", "insufficient")
    assert assessment.data_sufficiency != "full"


# ══════════════════════════════════════════════════════════════════════════
# 조건 #3 — 실제 원자료 시각 보존, 수집시각으로 세탁 금지
#   entry point: analysts.py(TechnicalAnalyst/FundamentalAnalyst)
#                + team.TradingTeam._deliberate(indicators_as_of 보유 경로)
# ══════════════════════════════════════════════════════════════════════════

def test_cond3_technical_observed_at_none_when_timestamp_unknown():
    report = asyncio.run(
        TechnicalAnalyst().analyze("005930", "삼성전자", indicators={"rsi_14": 50.0},
                                   indicators_as_of=None)
    )
    assert report.observed_at is None
    assert report.data_status == "partial"
    assert "지표 시각 미상" in report.limitations


def test_cond3_technical_observed_at_preserved_when_known():
    real_ts = datetime(2026, 9, 10, 9, 5, 0)
    report = asyncio.run(
        TechnicalAnalyst().analyze("005930", "삼성전자", indicators={"rsi_14": 50.0},
                                   indicators_as_of=real_ts)
    )
    assert report.observed_at == real_ts
    assert report.data_status == "full"


def test_cond3_fundamental_observed_at_always_none_not_laundered():
    fa = FundamentalAnalyst(stock_validator=_FakeValidatorPassNoSignal(), dart_checker=None)
    report = asyncio.run(fa.analyze("035420", "NAVER"))
    # 캐시 히트의 실제 시각을 모르므로 observed_at 은 항상 None — data_as_of(추정치)로 채우지 않는다.
    assert report.observed_at is None
    assert "캐시 시각 미제공" in report.limitations


def test_cond3_holding_reevaluation_receives_real_indicators_as_of(iso, monkeypatch):
    monkeypatch.setattr(repro_mod, "LEDGER_DIR", iso / "llm_ledger2")
    team = TradingTeam(llm_manager=None, stock_validator=None, dart_checker=None,
                       expert_orchestrator=None)
    real_ts = datetime(2026, 9, 12, 14, 0, 0)
    verdict = asyncio.run(team.deliberate_holding(
        "005930", "삼성전자", indicators={"rsi_14": 55.0}, indicators_as_of=real_ts,
    ))
    tech_reports = [r for r in verdict.reports if r.kind == AnalystKind.TECHNICAL]
    assert tech_reports and tech_reports[0].observed_at == real_ts

    verdict_unknown = asyncio.run(team.deliberate_holding(
        "005930", "삼성전자", indicators={"rsi_14": 55.0}, indicators_as_of=None,
    ))
    tech2 = [r for r in verdict_unknown.reports if r.kind == AnalystKind.TECHNICAL]
    assert tech2 and tech2[0].observed_at is None


# ══════════════════════════════════════════════════════════════════════════
# 조건 #4 — 거래량 키·단위 계약 (생산자 technical.py → 소비자 analysts.py)
# ══════════════════════════════════════════════════════════════════════════

def test_cond4_producer_key_is_vol_ratio():
    from src.indicators.technical import TechnicalIndicators
    ti = TechnicalIndicators()
    base = datetime(2026, 1, 1)
    daily = []
    for i in range(30):
        daily.append({
            "date": (base + timedelta(days=i)).strftime("%Y-%m-%d"),
            "open": 10000, "high": 10100, "low": 9900, "close": 10000,
            "volume": 100_000,
        })
    # 마지막 날 거래량 급증 (3배)
    daily[-1] = dict(daily[-1], volume=300_000)
    out = ti.calculate_all("005930", daily)
    assert "vol_ratio" in out
    assert out["vol_ratio"] > 2.0  # 거래량 급증 판정 임계값(analysts.py) 이상
    assert "volume_ratio" not in out  # 생산자는 이 키를 만들지 않는다


def test_cond4_consumer_reads_vol_ratio_and_scores_it():
    report = asyncio.run(
        TechnicalAnalyst().analyze("005930", "삼성전자",
                                   indicators={"rsi_14": 50.0, "vol_ratio": 2.5},
                                   indicators_as_of=datetime.now())
    )
    assert any(e.metric == "vol_ratio" and e.value == 2.5 for e in report.evidence)
    assert "거래량 급증" in " ".join(report.findings)


def test_cond4_consumer_ignores_legacy_volume_ratio_key():
    """계약이 정한 키가 아니면(volume_ratio) 조용히 무시된다 — 되살아나지 않는다."""
    report = asyncio.run(
        TechnicalAnalyst().analyze("005930", "삼성전자",
                                   indicators={"rsi_14": 70.0, "volume_ratio": 5.0},
                                   indicators_as_of=datetime.now())
    )
    assert not any(e.metric == "vol_ratio" for e in report.evidence)
    assert "거래량 급증" not in " ".join(report.findings)


# ══════════════════════════════════════════════════════════════════════════
# 조건 #5 — Bear 위험 허용만으로 매력·성공확률 상승 없음
#   entry point: judgment.assess
# ══════════════════════════════════════════════════════════════════════════

def _decent_reports() -> List[AnalystReport]:
    return [
        AnalystReport(
            kind=AnalystKind.FUNDAMENTAL, symbol="005930", score=35, confidence=0.8,
            data_status="full", positive_basis=True, risk_clear=True,
            data_as_of=datetime.now() - timedelta(minutes=1),
            evidence=[EvidenceItem(source="stock_validator.supply_demand",
                                   metric="foreign_net_buying", value=True,
                                   status="full", kind="fact")],
        ),
        AnalystReport(
            kind=AnalystKind.TECHNICAL, symbol="005930", score=25, confidence=0.7,
            data_status="full", data_as_of=datetime.now() - timedelta(minutes=1),
            evidence=[EvidenceItem(source="technical.indicators", metric="rsi_14",
                                   value=55, status="full", kind="fact")],
        ),
    ]


def test_cond5_bear_accept_vs_reject_does_not_move_merit_or_probability():
    reports = _decent_reports()
    debate_reject = DebateResult(symbol="005930", bull_final=True, bear_final=False,
                                 consensus=None, confidence=0.5, rounds_run=2)
    debate_accept = DebateResult(symbol="005930", bull_final=True, bear_final=True,
                                 consensus=True, confidence=1.0, rounds_run=2)

    a_reject = judgment.assess("005930", reports, debate_reject)
    a_accept = judgment.assess("005930", reports, debate_accept)

    assert a_reject.merit_score == a_accept.merit_score
    assert a_reject.merit_status == a_accept.merit_status
    assert a_reject.risk_acceptable is False
    assert a_accept.risk_acceptable is True
    # 위험 허용만으로 성공확률이 생기지 않는다 — 만장일치여도 uncalibrated.
    assert a_reject.success_probability is None
    assert a_accept.success_probability is None
    assert a_accept.calibration_status == "uncalibrated"


# ══════════════════════════════════════════════════════════════════════════
# 조건 #6 — 토론 전후 판단·변경 이유 보존
#   entry point: researchers.ResearchTeam(가짜 LLM) + judgment.assess
# ══════════════════════════════════════════════════════════════════════════

class _FakeLLMResp:
    def __init__(self, content, model="fake", provider="p"):
        self.content = content
        self.model = model
        self.provider = provider
        self.latency_ms = 5.0
        self.success = True
        self.error = None


class _ScriptedLLM:
    """provider.value 별로 라운드 순서대로 응답 텍스트를 내보내는 가짜 LLM 매니저."""

    def __init__(self, script: Dict[str, List[str]]):
        self._script = script
        self._idx: Dict[str, int] = {}

    async def complete_with(self, prompt, *, provider, weight, system, max_tokens,
                            reasoning_effort, retry_on_empty, seed, temperature):
        pv = getattr(provider, "value", str(provider))
        i = self._idx.get(pv, 0)
        self._idx[pv] = i + 1
        texts = self._script.get(pv, [])
        text = texts[i] if i < len(texts) else (texts[-1] if texts else "")
        return _FakeLLMResp(text, model=f"fake-{pv}", provider=pv)


def test_cond6_recorded_change_reason_preserved_across_rounds(iso, monkeypatch):
    monkeypatch.setattr(repro_mod, "LEDGER_DIR", iso / "llm_ledger6a")
    script = {
        LLMProvider.OPENAI.value: [
            "APPROVE 실적 서프라이즈 확인",
            "APPROVE 반론을 검토했으나 입장 유지",
        ],
        LLMProvider.GEMINI.value: [
            "REJECT 모멘텀 부족",
            "ACCEPT\n변경사유: 새근거 — 반등 거래량 확인",
        ],
    }
    team = ResearchTeam(llm_manager=_ScriptedLLM(script), rounds=2)
    reports = _decent_reports()
    result = asyncio.run(team.debate("005930", "삼성전자", reports))

    assert result.rounds_run == 2
    assert len(result.turns) == 4
    r1_bull, r1_bear, r2_bull, r2_bear = result.turns

    # R1 판단이 그대로 보존된다
    assert r1_bull.round_no == 1 and r1_bull.stance is True and r1_bull.change_reason is None
    assert r1_bear.round_no == 1 and r1_bear.stance is False and r1_bear.change_reason is None

    # R2: bull 은 유지 → 변경 이유 없음
    assert r2_bull.stance is True and r2_bull.change_reason is None
    # R2: bear 는 REJECT→ACCEPT 로 전향 — 변경 이유가 파싱되어 보존된다
    assert r2_bear.stance is True
    assert r2_bear.change_reason == {"kind": "new_evidence", "text": "반등 거래량 확인"}

    assert result.bull_final is True and result.bear_final is True
    assert result.consensus is True

    assessment = judgment.assess("005930", reports, result)
    # 토론 전(R1) 판단이 소실되지 않고 별도 필드로 남는다
    assert assessment.independent_votes == {"bull": True, "bear": False}
    assert assessment.final_votes == {"bull": True, "bear": True}
    assert any(c.get("side") == "bear" and c.get("kind") == "new_evidence"
               for c in assessment.change_reasons)


def test_cond6_unrecorded_change_when_no_reason_text(iso, monkeypatch):
    monkeypatch.setattr(repro_mod, "LEDGER_DIR", iso / "llm_ledger6b")
    script = {
        LLMProvider.OPENAI.value: ["APPROVE 근거 있음", "APPROVE 유지"],
        LLMProvider.GEMINI.value: ["REJECT 근거 부족", "ACCEPT 그냥 마음이 바뀜"],
    }
    team = ResearchTeam(llm_manager=_ScriptedLLM(script), rounds=2)
    result = asyncio.run(team.debate("005930", "삼성전자", _decent_reports()))
    r2_bear = result.turns[3]
    assert r2_bear.side == "bear" and r2_bear.round_no == 2
    assert r2_bear.stance is True
    # 판정은 바뀌었는데 '변경사유:' 줄이 없으면 지어내지 않고 unrecorded 로 남긴다
    assert r2_bear.change_reason == {"kind": "unrecorded"}


# ══════════════════════════════════════════════════════════════════════════
# 조건 #7 — EntryPlan 조건이 직렬화·이벤트·대기 통과 후 유지
#   entry point: batch_analyzer(PendingSignal, _to_pending_signal) + entry_plan.check_entry_plan
# ══════════════════════════════════════════════════════════════════════════

def _vcp_signal() -> Signal:
    return Signal(
        symbol="005930", side=OrderSide.BUY, strength=SignalStrength.STRONG,
        strategy=StrategyType.VCP_BREAKOUT, price=Decimal("41000"),
        stop_price=Decimal("39000"), target_price=Decimal("47000"),
        score=72.0, reason="VCP 변동성수축 돌파", reasons=["VCP 20일 고점 돌파 확인"],
        metadata={"entry_mode": "breakout", "breakout_trigger": 41500.0,
                  "candidate_id": "cand-77", "indicators": {"vol_ratio": 2.1}, "atr_pct": 3.2},
    )


def test_cond7_plan_generated_then_survives_json_roundtrip():
    now = datetime(2026, 9, 15, 9, 5, 0)
    expires = datetime(2026, 9, 15, 15, 30, 0)
    analyzer = object.__new__(BatchAnalyzer)  # 생성자(스크리너·브로커) 우회 — 순수 변환만 검증
    ps = analyzer._to_pending_signal(_vcp_signal(), now=now, expires=expires, slippage_pct=3.0)

    assert ps.setup == "vcp_breakout"
    assert ps.trigger == {"type": "breakout", "level": 41500.0,
                          "satisfied": None, "satisfied_at": None}
    assert ps.plan_id
    assert ps.exit_policy_ref == "exit_manager:vcp_breakout"
    assert ps.invalidation["stop_price"] == 39000.0

    roundtrip = json.loads(json.dumps(ps.to_dict()))
    ps2 = PendingSignal.from_dict(roundtrip)
    assert ps2.trigger == ps.trigger
    assert ps2.invalidation == ps.invalidation
    assert ps2.plan_id == ps.plan_id
    assert ps2.setup == ps.setup
    assert ps2.max_entry_price == pytest.approx(ps.max_entry_price)


def test_cond7_plan_survives_into_signal_event_metadata_and_checker():
    now = datetime(2026, 9, 15, 9, 5, 0)
    expires = datetime(2026, 9, 15, 15, 30, 0)
    analyzer = object.__new__(BatchAnalyzer)
    ps = analyzer._to_pending_signal(_vcp_signal(), now=now, expires=expires, slippage_pct=3.0)
    ps2 = PendingSignal.from_dict(json.loads(json.dumps(ps.to_dict())))

    # 주문 직전 단계를 미러: 현재가로 새 Signal 을 만들되 계획은 그대로 싣는다
    order_signal = Signal(
        symbol=ps2.symbol, side=OrderSide.BUY, strength=SignalStrength.STRONG,
        strategy=StrategyType.VCP_BREAKOUT, price=Decimal("41600"),
        target_price=Decimal(str(ps2.target_price)), stop_price=Decimal(str(ps2.stop_price)),
        score=ps2.score, reason=ps2.reason,
        metadata={"entry_plan": ps2.to_dict(), "quote_as_of": now.isoformat()},
    )
    event = SignalEvent.from_signal(order_signal, source="batch_analyzer")

    assert event.metadata["entry_plan"]["trigger"]["level"] == 41500.0
    assert event.metadata["entry_plan"]["plan_id"] == ps.plan_id

    # 트리거 위 → allow
    ok = check_entry_plan(event.metadata["entry_plan"],
                          {"price": 41600.0, "as_of": now.isoformat()}, now)
    assert ok.status == "allow"

    # 같은 계획, 트리거 아래 현재가 → TRIGGER_NOT_MET (wait)
    wait = check_entry_plan(event.metadata["entry_plan"],
                            {"price": 41000.0, "as_of": now.isoformat()}, now)
    assert wait.status == "wait"
    assert "TRIGGER_NOT_MET" in wait.reasons


def test_cond7_plan_survives_pending_queue_persistence_cycle():
    """대기 큐 저장/로드(_save_json/_load_json 과 동일한 to_dict→JSON→from_dict)를 통과해도
    계획 필드가 그대로다."""
    now = datetime(2026, 9, 15, 9, 5, 0)
    expires = datetime(2026, 9, 15, 15, 30, 0)
    analyzer = object.__new__(BatchAnalyzer)
    ps = analyzer._to_pending_signal(_vcp_signal(), now=now, expires=expires, slippage_pct=3.0)

    persisted = json.dumps([ps.to_dict()], ensure_ascii=False)
    loaded = [PendingSignal.from_dict(d) for d in json.loads(persisted)]
    assert len(loaded) == 1
    assert loaded[0].plan_id == ps.plan_id
    assert loaded[0].trigger == ps.trigger
    assert loaded[0].required_inputs == ps.required_inputs
    assert loaded[0].assumptions["fee_side"] == "buy_only"


# ══════════════════════════════════════════════════════════════════════════
# 조건 #8 — 상한 초과·만료·필수 결측은 shadow 에서 wait/reject
#   entry point: entry_plan.check_entry_plan
# ══════════════════════════════════════════════════════════════════════════

NOW8 = datetime(2026, 9, 15, 10, 0, 0)


def _base_plan(**kw) -> Dict[str, Any]:
    base = dict(
        plan_id="p1", setup="sepa_pullback", strategy="sepa_trend",
        entry_price=10000.0, max_entry_price=10300.0, stop_price=9500.0,
        target_price=11000.0, entry_mode="close", breakout_trigger=0.0,
        expires_at=(NOW8 + timedelta(hours=3)).isoformat(),
        invalidation={"stop_price": 9500.0, "below_price": 0.0,
                      "intraday_levels": ["severe"],
                      "expires_at": (NOW8 + timedelta(hours=3)).isoformat()},
        trigger={"type": "none"},
        required_inputs=[], missing_inputs=[],
        assumptions={"fee_bps": 1.4, "slippage_bps": None},
    )
    base.update(kw)
    return base


def _quote(price, as_of=NOW8, **kw):
    q = {"price": price, "as_of": as_of.isoformat() if isinstance(as_of, datetime) else as_of}
    q.update(kw)
    return q


def test_cond8_expired_plan_rejects():
    plan = _base_plan(expires_at=(NOW8 - timedelta(minutes=1)).isoformat())
    r = check_entry_plan(plan, _quote(10000.0), NOW8)
    assert r.status == "reject"
    assert "PLAN_EXPIRED" in r.reasons


def test_cond8_price_above_cap_waits_not_rejects():
    plan = _base_plan()
    r = check_entry_plan(plan, _quote(10500.0), NOW8)  # cap=10300
    assert r.status == "wait"
    assert "PRICE_ABOVE_CAP" in r.reasons


def test_cond8_required_input_missing_waits():
    plan = _base_plan(required_inputs=["custom_liquidity_score"])
    r = check_entry_plan(plan, _quote(10000.0), NOW8)
    assert r.status == "wait"
    assert "INPUT_MISSING:custom_liquidity_score" in r.reasons
    assert "custom_liquidity_score" in r.missing_inputs


def test_cond8_gap_vwap_setup_requires_vwap_in_quote():
    plan = _base_plan(setup="gap_vwap", strategy="gap_and_go")
    r = check_entry_plan(plan, _quote(10000.0), NOW8)  # vwap 없음
    assert r.status == "wait"
    assert "INPUT_MISSING:vwap" in r.reasons

    r2 = check_entry_plan(plan, _quote(10000.0, vwap=9900.0), NOW8)
    assert "INPUT_MISSING:vwap" not in r2.reasons


def test_cond8_severe_intraday_rejects():
    plan = _base_plan()
    r = check_entry_plan(plan, _quote(10000.0), NOW8, intraday_level="severe")
    assert r.status == "reject"
    assert "INTRADAY_BLOCK:severe" in r.reasons


def test_cond8_invalidation_stop_breach_rejects():
    plan = _base_plan()
    r = check_entry_plan(plan, _quote(9400.0), NOW8)  # <= stop 9500
    assert r.status == "reject"
    assert "INVALIDATED:stop_price" in r.reasons


def test_cond8_missing_quote_waits_not_reject():
    plan = _base_plan()
    r = check_entry_plan(plan, None, NOW8)
    assert r.status == "wait"
    assert "QUOTE_MISSING" in r.reasons


def test_cond8_stale_quote_waits():
    plan = _base_plan()
    stale_as_of = NOW8 - timedelta(seconds=600)  # > QUOTE_MAX_AGE_SEC(300)
    r = check_entry_plan(plan, _quote(10000.0, as_of=stale_as_of), NOW8)
    assert r.status == "wait"
    assert "QUOTE_STALE" in r.reasons


# ══════════════════════════════════════════════════════════════════════════
# 조건 #9/#10 — RiskManager.on_signal (대역 engine/portfolio/broker)
# ══════════════════════════════════════════════════════════════════════════

SYM9 = "005380"


def _rm_harness(monkeypatch):
    """RiskManager.__init__ 을 우회(무거운 실제 협력자 생성 방지)하고 on_signal 이 실제로
    쓰는 속성만 최소로 채운다 — 순수 합성 값, 운영 캐시·네트워크 무접촉."""
    logged: List[Dict[str, Any]] = []

    class _FakeSigLog:
        async def log(self, **kw):
            logged.append(kw)

    monkeypatch.setattr(engine_mod._SigLog, "get", staticmethod(lambda: _FakeSigLog()))
    # 사이징 오버레이(캘린더·변동성·팀 conviction)는 각자 운영 캐시 파일을 읽는다 —
    # 격리를 위해 중립값(1.0)으로 고정한다(합성 입력, 실 파일 접촉 0).
    monkeypatch.setattr("src.utils.calendar_seasonality.calendar_multiplier",
                        lambda *a, **k: (1.0, ""))
    monkeypatch.setattr("src.utils.volatility_targeting.vol_targeting_multiplier",
                        lambda *a, **k: (1.0, ""))
    monkeypatch.setattr("src.utils.team_conviction.team_conviction_multiplier",
                        lambda *a, **k: (1.0, ""))

    rm = object.__new__(RiskManager)
    rm.config = RiskConfig(
        base_position_pct=25.0, max_position_pct=28.0, min_position_value=100_000,
        strategy_allocation={"sepa_trend": 42.0, "core_holding": 30.0},
    )
    rm.engine = SimpleNamespace(
        portfolio=SimpleNamespace(
            positions={}, total_equity=Decimal("15000000"),
            effective_daily_pnl=Decimal("0"),
            get_strategy_allocation=lambda _s: Decimal("0"),
        ),
        broker=None,
        get_available_cash=lambda: Decimal("15000000"),
        is_trading_hours=lambda: True,
        _get_current_session=lambda: MarketSession.REGULAR,
        can_open_position=lambda *a, **k: (True, ""),
        _pending_sector_map={},
        _market_regime="neutral",
        _regime_adapter=None,
        expert_orchestrator=None,
    )
    rm._cross_validator = SimpleNamespace(
        validate=lambda **kw: (True, 96.0, ""), last_memory_adj=0, last_llm_context={},
    )
    rm._risk_validator = None
    rm._sector_lookup = None
    rm._resolve_entry_stop = None
    rm._order_fail_cooldown = {}
    rm._COOLDOWN_SECONDS = 300
    rm._last_signal_time = {}
    rm._SIGNAL_COOLDOWN_SECONDS = 30
    rm._pending_orders = set()
    rm._pending_quantities = {}
    rm._pending_timestamps = {}
    rm._pending_sides = {}
    rm._pending_fallback_count = {}
    rm._pending_strategy = {}
    rm._pending_signal_cache = {}
    rm._reserved_by_order = {}
    rm._pending_lock = asyncio.Lock()
    rm._last_cash_warn_time = None
    rm._LLM_CHECK_MIN, rm._LLM_BYPASS_AT, rm._LLM_REJECT_SIZE_MULT = 85, 95, 0.5
    rm._REPLACEMENT_MIN_SCORE = 85
    rm._check_factor_budget = lambda _s: None
    return rm, logged


def _buy_event(entry_plan: Optional[Dict[str, Any]] = None) -> SignalEvent:
    meta: Dict[str, Any] = {}
    if entry_plan is not None:
        meta["entry_plan"] = entry_plan
        meta["quote_as_of"] = datetime.now().isoformat()
    sig = Signal(
        symbol=SYM9, side=OrderSide.BUY, strength=SignalStrength.NORMAL,
        strategy=StrategyType.SEPA_TREND, price=Decimal("52000"),
        stop_price=Decimal("49500"), target_price=Decimal("58000"), score=96.0,
        reason="sepa 추세 정렬", reasons=["sepa 추세 정렬 확인"], metadata=meta,
    )
    return SignalEvent.from_signal(sig, source="test-independent")


def _fingerprint(order):
    return (order.symbol, order.side, order.order_type, order.quantity,
            order.price, order.strategy, order.reason, order.signal_score)


def test_cond9_flag_off_order_identical_to_flag_on(monkeypatch):
    """§4 #9: ENTRY_PLAN_SHADOW off 일 때 결과(Order)가 on 일 때와 동일해야 한다."""
    plan = _base_plan(setup="sepa_pullback", strategy="sepa_trend")

    monkeypatch.setenv("ENTRY_PLAN_SHADOW", "1")
    rm_on, _ = _rm_harness(monkeypatch)
    events_on = asyncio.run(rm_on.on_signal(_buy_event(entry_plan=plan)))
    assert events_on
    order_on = events_on[0].order

    monkeypatch.setenv("ENTRY_PLAN_SHADOW", "0")
    rm_off, _ = _rm_harness(monkeypatch)
    events_off = asyncio.run(rm_off.on_signal(_buy_event(entry_plan=plan)))
    assert events_off
    order_off = events_off[0].order

    assert _fingerprint(order_on) == _fingerprint(order_off)
    assert order_off.order_type is OrderType.MARKET  # 운영 주문 방식 불변


def test_cond9_flag_off_never_calls_checker(monkeypatch):
    calls = []
    real_check = engine_mod.check_entry_plan

    def _spy(*a, **k):
        calls.append(1)
        return real_check(*a, **k)

    monkeypatch.setattr(engine_mod, "check_entry_plan", _spy)
    plan = _base_plan(setup="sepa_pullback", strategy="sepa_trend")

    monkeypatch.setenv("ENTRY_PLAN_SHADOW", "0")
    rm_off, _ = _rm_harness(monkeypatch)
    asyncio.run(rm_off.on_signal(_buy_event(entry_plan=plan)))
    assert calls == []

    monkeypatch.setenv("ENTRY_PLAN_SHADOW", "1")
    rm_on, _ = _rm_harness(monkeypatch)
    asyncio.run(rm_on.on_signal(_buy_event(entry_plan=plan)))
    assert calls == [1]


def test_cond9_no_plan_present_order_still_produced(monkeypatch):
    """계획이 아예 없어도(신규 매수 전량) 돈 경로는 그대로 동작한다."""
    monkeypatch.setenv("ENTRY_PLAN_SHADOW", "1")
    rm, _ = _rm_harness(monkeypatch)
    events = asyncio.run(rm.on_signal(_buy_event(entry_plan=None)))
    assert events and events[0].order.quantity > 0


def test_cond10_shadow_checker_exception_does_not_block_order(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("합성 장애 — shadow 검증기 고장")

    monkeypatch.setattr(engine_mod, "check_entry_plan", _boom)
    monkeypatch.setenv("ENTRY_PLAN_SHADOW", "1")
    rm, _ = _rm_harness(monkeypatch)
    plan = _base_plan(setup="sepa_pullback", strategy="sepa_trend")
    events = asyncio.run(rm.on_signal(_buy_event(entry_plan=plan)))
    assert events, "shadow 검증기 예외가 나도 주문은 생성돼야 한다"
    assert events[0].order.quantity > 0


def test_cond10_shadow_sig_log_exception_does_not_block_order(monkeypatch):
    class _BoomSigLog:
        @staticmethod
        def get():
            raise RuntimeError("합성 장애 — 로깅 계층 고장")

    monkeypatch.setattr(engine_mod, "_SigLog", _BoomSigLog)
    monkeypatch.setenv("ENTRY_PLAN_SHADOW", "1")
    rm, _ = _rm_harness(monkeypatch)
    plan = _base_plan(setup="sepa_pullback", strategy="sepa_trend")
    events = asyncio.run(rm.on_signal(_buy_event(entry_plan=plan)))
    assert events, "시그널 로깅 계층 예외가 나도 주문은 생성돼야 한다"


def test_cond10_check_entry_plan_itself_never_raises_on_malformed_plan():
    class _BoomToDict:
        def to_dict(self):
            raise ValueError("합성 손상 계획")

    r = check_entry_plan(_BoomToDict(), _quote(10000.0), NOW8)
    assert r.status == "wait"
    assert any(reason.startswith("CHECKER_ERROR") for reason in r.reasons)


def test_cond10_team_assessment_failure_does_not_touch_decision(iso, monkeypatch):
    monkeypatch.setattr(repro_mod, "LEDGER_DIR", iso / "llm_ledger10")

    def _boom_assess(*a, **k):
        raise RuntimeError("합성 장애 — TeamAssessment 계산 고장")

    monkeypatch.setattr(team_mod, "_assess_team", _boom_assess)
    team = TradingTeam(llm_manager=None, stock_validator=None, dart_checker=None,
                       expert_orchestrator=None)
    verdict = asyncio.run(team.deliberate_candidate(
        "005930", "삼성전자", indicators={"rsi_14": 55.0}, gate_checker=None,
    ))
    assert verdict.error is None
    assert verdict.decision is not None  # 돈 경로 결정은 그대로 확정됐다
    assert verdict.assessment is None    # shadow 만 조용히 실패


def test_cond10_team_ledger_write_failure_returns_none_not_raises(tmp_path):
    bad_dir = tmp_path / "not_a_dir"
    bad_dir.write_text("this is a file, not a directory", encoding="utf-8")
    did = team_ledger.append_deliberation(
        {"symbol": "005930", "decided_at": "2026-09-15T10:00:00", "slot": "10:30",
         "input_snapshot_hash": "abc"},
        ledger_dir=bad_dir,
    )
    assert did is None  # 예외 없이 실패만 보고


# ══════════════════════════════════════════════════════════════════════════
# 조건 #11/#12 — 같은 날 복수 판단 추적 + 중복 재시도가 표본을 부풀리지 않음
#   entry point: team_ledger + counterfactual_tracker
# ══════════════════════════════════════════════════════════════════════════

def test_cond11_multiple_slots_same_symbol_same_day_are_both_tracked(iso):
    ledger_dir = iso / "team_ledger_11"
    row_morning = {
        "symbol": "005930", "slot": "10:30", "decided_at": "2026-09-15T10:30:00",
        "input_snapshot_hash": "hashA",
        "decision": {"stance": "hold", "approved": False},
    }
    row_afternoon = {
        "symbol": "005930", "slot": "14:00", "decided_at": "2026-09-15T14:00:00",
        "input_snapshot_hash": "hashB",
        "decision": {"stance": "buy", "approved": True},
    }
    id1 = team_ledger.append_deliberation(row_morning, ledger_dir=ledger_dir)
    id2 = team_ledger.append_deliberation(row_afternoon, ledger_dir=ledger_dir)
    assert id1 and id2 and id1 != id2  # 슬롯이 다르면 다른 판단으로 남는다

    rows = team_ledger.history_for_symbol("005930", "2026-09-15", ledger_dir=ledger_dir)
    assert len(rows) == 2
    counts = team_ledger.count_samples(rows)
    assert counts.get("buy_approved") == 1
    assert counts.get("hold") == 1


def test_cond11_approved_buy_without_fill_evidence_tracked_as_unfilled(iso):
    day8 = "20260115"
    verdict_dir = iso / "team_verdicts"
    verdict_dir.mkdir(parents=True, exist_ok=True)
    rows = [{
        "symbol": "005930", "wiki_context_used": False,
        "decision": {"stance": "buy", "approved": True},
    }]
    (verdict_dir / f"verdicts_{day8}.json").write_text(
        json.dumps(rows, ensure_ascii=False), encoding="utf-8",
    )

    class _NoOpBroker:
        async def get_daily_prices(self, symbol, days=45):
            return []

    tracker = cf_mod.CounterfactualTracker(fill_evidence_check=None)
    result = asyncio.run(tracker.update(_NoOpBroker()))
    assert result["added"] >= 1
    key = "team_buy_unfilled|005930|2026-01-15"
    assert key in tracker._state


def test_cond12_duplicate_deliberation_retry_dedups_on_read_but_stays_appended(iso):
    ledger_dir = iso / "team_ledger_12"
    row = {
        "symbol": "000660", "slot": "10:30", "decided_at": "2026-09-15T10:30:05",
        "input_snapshot_hash": "same-hash",
        "decision": {"stance": "hold", "approved": False},
    }
    id1 = team_ledger.append_deliberation(row, ledger_dir=ledger_dir)
    id2 = team_ledger.append_deliberation(dict(row), ledger_dir=ledger_dir)  # 재시도(동일 입력)
    assert id1 == id2  # 같은 입력 → 같은 deliberation_id (멱등)

    raw_lines = team_ledger.load_day("2026-09-15", ledger_dir=ledger_dir, dedup=False)
    assert len(raw_lines) == 2  # append-only — 두 줄 다 남는다

    deduped = team_ledger.load_day("2026-09-15", ledger_dir=ledger_dir, dedup=True)
    assert len(deduped) == 1  # 그러나 읽기 쪽은 1건으로 접힌다 — 표본이 부풀지 않는다


def test_cond12_counterfactual_ingest_is_idempotent_on_retry(iso):
    day8 = "20260116"
    verdict_dir = iso / "team_verdicts"
    verdict_dir.mkdir(parents=True, exist_ok=True)
    rows = [{
        "symbol": "005930", "wiki_context_used": False,
        "decision": {"stance": "buy", "approved": True},
    }]
    (verdict_dir / f"verdicts_{day8}.json").write_text(
        json.dumps(rows, ensure_ascii=False), encoding="utf-8",
    )

    class _NoOpBroker:
        async def get_daily_prices(self, symbol, days=45):
            return []

    tracker = cf_mod.CounterfactualTracker(fill_evidence_check=None)
    first = asyncio.run(tracker.update(_NoOpBroker()))
    second = asyncio.run(tracker.update(_NoOpBroker()))  # 같은 파일 재실행(재시도 시뮬레이션)
    assert first["added"] >= 1
    assert second["added"] == 0  # 이미 등록된 키는 다시 세지 않는다


# ══════════════════════════════════════════════════════════════════════════
# 조건 #13 — A/B/C 동일 후보·시점·위험·청산·비용 비교
#   entry point: scripts/team_policy_ab.py
# ══════════════════════════════════════════════════════════════════════════

def _ab_bars(start: str, n: int = 3):
    base = datetime.fromisoformat(start)
    out = []
    for i in range(n):
        d = (base + timedelta(days=i)).strftime("%Y-%m-%d")
        if i == 0:
            out.append({"date": d, "open": 9900, "high": 9950, "low": 9800, "close": 9900,
                       "volume": 100000})
        elif i == 1:
            # 진입봉(다음 거래일) — 시가 10000, 저가가 5% 손절선(9500) 아래로 이탈
            out.append({"date": d, "open": 10000, "high": 10050, "low": 9400, "close": 9450,
                       "volume": 120000})
        else:
            out.append({"date": d, "open": 9450, "high": 9500, "low": 9300, "close": 9350,
                       "volume": 90000})
    return out


def _ab_candidate_with_evidence(symbol: str, score: float, synthetic=True) -> tpab.Candidate:
    rep = AnalystReport(
        kind=AnalystKind.FUNDAMENTAL, symbol=symbol, score=30, confidence=0.8,
        data_as_of=datetime(2026, 1, 5),  # 후보 판단 시각의 자료, 테스트 실행 시각 사용 금지
        data_status="full", positive_basis=True, risk_clear=True,
        evidence=[EvidenceItem(source="stock_validator.supply_demand",
                               metric="foreign_net_buying", value=True,
                               status="full", kind="fact")],
    )
    return tpab.Candidate(
        date="2026-01-05", symbol=symbol, strategy="sepa_trend", setup="sepa_pullback",
        plan={"score": score, "stop_price": 9500.0},
        evidence=[rep.to_dict()],
        votes={"r1": {"bull": True, "bear": True}, "r2": {"bull": True, "bear": True}},
        prices=_ab_bars("2026-01-05"),
        synthetic=synthetic,
    )


def _ab_candidate_no_evidence(symbol: str, score: float, synthetic=True) -> tpab.Candidate:
    return tpab.Candidate(
        date="2026-01-05", symbol=symbol, strategy="sepa_trend", setup="sepa_pullback",
        plan={"score": score, "stop_price": 9500.0},
        evidence=[],  # A 근거계약 미배선 상태 — B/C 는 탈락해야 한다
        votes={"r1": {"bull": None, "bear": None}, "r2": {"bull": None, "bear": None}},
        prices=_ab_bars("2026-01-05"),
        synthetic=synthetic,
    )


def test_cond13_same_candidate_pool_across_policies_only_gate_differs():
    strong = _ab_candidate_with_evidence("005930", score=80)
    weak = _ab_candidate_no_evidence("000660", score=50)
    rows = [strong, weak]

    sel_a = tpab.select_candidates(rows, "A", 5)
    sel_b = tpab.select_candidates(rows, "B", 5)
    sel_c = tpab.select_candidates(rows, "C", 5)

    # A(기존 규칙): 게이트 없음 — 둘 다 선정
    assert {c.symbol for c in sel_a} == {"005930", "000660"}
    # B/C: 근거·토론 게이트 통과한 후보만 — 같은 rows 에서 출발했지만 부분집합이다
    assert {c.symbol for c in sel_b} <= {c.symbol for c in sel_a}
    assert {c.symbol for c in sel_c} <= {c.symbol for c in sel_b}
    assert "005930" in {c.symbol for c in sel_b}
    assert "000660" not in {c.symbol for c in sel_b}


def test_cond13_cost_and_exit_assumptions_are_singular_not_per_policy():
    """청산 사다리·비용 가정은 정책과 무관한 모듈 상수 하나로 고정돼 있다(정책별로
    다른 파라미터를 받지 않는다) — 실행 시그니처 자체가 이를 강제한다."""
    import inspect
    sig = inspect.signature(tpab.simulate_exit)
    assert "policy" not in sig.parameters
    assert isinstance(tpab.PRE_REGISTERED["cost_assumption"], str)
    assert isinstance(tpab.PRE_REGISTERED["exit_assumption"], str)
    assert isinstance(tpab.PRE_REGISTERED["fill_assumption"], str)


def test_cond13_selection_experiment_uses_same_rows_object_for_all_policies():
    rows = [_ab_candidate_with_evidence("005930", score=80),
           _ab_candidate_no_evidence("000660", score=50)]
    results = tpab.run_selection_experiment(rows, ["A", "B", "C"], max_new=5)
    assert set(results) == {"A", "B", "C"}
    # A 는 게이트가 없어 항상 B/C 이상(또는 동일)으로 선정된다 — 같은 표본에서 필터만 강해진다
    assert results["A"]["candidates_selected"] >= results["B"]["candidates_selected"]
    assert results["B"]["candidates_selected"] >= results["C"]["candidates_selected"]


# ══════════════════════════════════════════════════════════════════════════
# 조건 #14 — 검증 자료 없을 때 성공확률·수익 개선 수치 생성 금지
#   entry point: judgment.assess + scripts/team_policy_ab.py
# ══════════════════════════════════════════════════════════════════════════

def test_cond14_no_evidence_no_debate_yields_no_success_probability():
    assessment = judgment.assess("005930", [], None)
    assert assessment.success_probability is None
    assert assessment.calibration_status == "uncalibrated"
    assert assessment.merit_status == "abstain"
    assert assessment.stance_v2 == "abstain"


def test_cond14_runner_report_marks_synthetic_only_and_never_promotes():
    rows = [_ab_candidate_with_evidence("005930", score=80, synthetic=True)]
    all_synthetic = all(c.synthetic for c in rows)
    assert all_synthetic is True
    manifest = tpab.build_manifest(["A", "B", "C"], "selection", "synthetic.jsonl",
                                   all_synthetic, None)
    assert manifest["all_rows_synthetic"] is True
    assert "success_probability" not in manifest

    results = tpab.run_selection_experiment(rows, ["A", "B", "C"], max_new=5)
    for policy_stats in results.values():
        assert "success_probability" not in policy_stats
        assert "promotion" not in policy_stats

    report_md = tpab.build_report_md(manifest, results)
    assert "미검증/보류" in report_md
    assert "success_probability" not in report_md


# ══════════════════════════════════════════════════════════════════════════
# 전체 스위트 자체 점검 — 실데이터/네트워크 미사용 선언
# ══════════════════════════════════════════════════════════════════════════

def test_isolation_no_real_home_cache_touched(iso):
    """iso 픽스처가 활성화된 상태에서 team_ledger/reproducibility 상수가 tmp_path 를
    가리키는지 재확인 — 운영 캐시 경로가 아님을 구조적으로 보장한다."""
    assert str(team_ledger.LEDGER_DIR).startswith(str(iso))
    assert str(repro_mod.LEDGER_DIR).startswith(str(iso))
    assert str(team_mod.RESULT_DIR).startswith(str(iso))
    assert str(cf_mod._STATE_PATH).startswith(str(iso))
