#!/usr/bin/env python3
"""팀 정책 A/B/C 오프라인 비교 러너 — T11 계약 §2.5 (담당 D, 2026-09-15).

"팀이 근거를 검토하고 토론하는 게 기존 규칙보다 나은가"를 같은 후보군·같은 시점에서
비교하는 **연구용** 도구다. 실주문·운영 설정과 무관(shadow 도 아님 — 오프라인 전용).

정책:
    A — 기존 규칙: 스크리너 점수 상위 K 종목을 그대로 선정 (팀 심의 없음)
    B — A + R1 독립 판단: merit_status(sufficient/weak) ∧ risk_acceptable=True ∧
        data_sufficiency≥partial 인 종목만 남긴 뒤 상위 K (토론 전, R1 표결만)
    C — B + R2(토론) 최종 투표: bull_final=bear_final=True(만장일치) 만 통과

세 정책 모두 **같은 날짜·같은 후보 풀**에서 출발한다 — 이미 BUY 로 정해진 결과를 보고
사후에 정책을 끼워 맞추지 않는다(사후 선별 금지).

실험:
    selection — 정책(A/B/C)만 비교. 진입=다음 거래일 시가, 청산=분할익절·트레일링·손절
                사다리만 미러(stale_exit/stale_high 시간 기반 청산은 미구현 — known_limitations
                참조), 위험예산·비용 고정.
    timing    — 선정은 고정(기본 C 정책)하고 진입만 비교: 기존(다음 시가 즉시) vs
                EntryPlan 조건부(`src.execution.entry_plan.check_entry_plan` 을 그대로
                재사용 — 판정 로직을 여기서 중복 구현하지 않는다). checker 판정(allow/wait/
                reject)을 그대로 따른다 — 러너가 자체적으로 체결 여부를 다시 판정하지 않는다.

입력 스냅샷(JSONL, 한 줄 = 후보 1건):
    {"date": "2026-08-01", "symbol": "005930", "strategy": "sepa_trend", "setup": "sepa_pullback",
     "plan": {...PendingSignal.to_dict() 형태 — score/stop_price/entry_band_low/trigger/max_entry_price...},
     "evidence": [...AnalystReport.to_dict() 형태 3건(fundamental/technical/news) — 정책 B/C 를
       judgment.assess 로 판정하려면 각 보고서에 data_status("full"/"partial")와 근거
       evidence(kind="fact", status, value) 를 채워야 한다. 전부 data_status="unknown"(기본값)
       이면 judgment.assess 는 "A(근거 계약) 미배선" 상태로 보고 merit_status 를 abstain 으로
       고정한다 — 이땐 B/C 선정이 0건이어도 버그가 아니다...],
     "votes": {"r1": {"bull": true, "bear": true}, "r2": {"bull": true, "bear": true}},
     "prices": [{"date": "2026-08-01", "open":.., "high":.., "low":.., "close":..}, ...],
     "synthetic": true}   # 합성 fixture 면 true — 하나라도 없으면 전체를 "unverified" 로 표시

실데이터 스냅샷이 없으면 합성 fixture(tests/fixtures)로 **도구 동작만** 검증하고,
출력은 "미검증/보류"로 표시한다. 승격 판정은 이 도구가 하지 않는다.

사용:
    python scripts/team_policy_ab.py --snapshot data.jsonl --policy all \
        --experiment selection --out results/team_policy_ab/run1
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import dataclass, field
from datetime import timedelta, date as _date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.utils.fee_calculator import get_fee_calculator  # noqa: E402
from src.execution.entry_plan import check_entry_plan  # noqa: E402
from src.agents import judgment  # noqa: E402
from src.agents.types import (  # noqa: E402
    AnalystKind, AnalystReport, ASSESSMENT_POLICY_VERSION, DebateResult, DebateTurn, EvidenceItem,
)

POLICIES = ("A", "B", "C")
EXPERIMENTS = ("selection", "timing")

# ── 청산정책 임계값 — CLAUDE.md "청산 관리(ExitManager)"의 분할익절·트레일링·손절 사다리만
#    미러한다(계획값, 사후 변경 금지). stale_exit/stale_high(시간 기반 강제청산, 실엔진
#    HOLDING_POLICIES)는 미구현이다 — known_limitations 참조, 이 절 자체가 "live 완전 미러"는
#    아니다 ──
DEFAULT_SL_PCT = 5.0            # ATR 동적 손절 기본값 (plan.stop_price 없을 때만 대체 사용)
TP1_PCT, TP1_FRAC = 10.0, 0.10  # 1차 익절
TP2_PCT, TP2_FRAC = 15.0, 0.50  # 2차 익절
TP3_PCT, TP3_FRAC = 25.0, 0.50  # 3차 익절
TRAIL_ARM_PCT = 5.0             # 트레일링 활성화 임계 수익률
TRAIL_DD_PCT = 3.0              # 트레일링 낙폭
# 관찰 창 상한(연구용, 사전등록값 — 결과 보고 바꾸지 않음). 실엔진 setup별 live max_holding
# (예: scripts/backtest_strategies.HOLDING_POLICIES['current']['sepa_max_holding_days']=10)
# 보다 느슨하다 — 이 안에 손절/익절/트레일링이 없으면 예전엔 "미완결로 제외"했으나(결과와
# 상관된 표본 탈락 — 손실은 빨리 손절돼 빠지고 승자는 미완결로 빠지는 하방 편향, 리뷰
# blocking 2026-09-15), 이제 관찰 창 마지막 봉 종가로 잔여 전량을 강제 청산해 완결로 센다
# (exit_reason="max_holding").
MAX_HOLD_DAYS = 20

DEFAULT_MAX_NEW_PER_DAY = 5      # CLAUDE.md KR 리스크 "일일 신규 매수 5개" 미러

# 사전 등록(결과 전 고정) — manifest.json 에 그대로 옮겨 적는다. 여기 숫자를 결과 보고 바꾸지 않는다.
PRE_REGISTERED = {
    "primary_metric": "포지션당 비용 차감 R(risk-adjusted return) — 중앙값·평균",
    "mde": "중앙값 R +0.10 (≈ 왕복 수수료 0.227%의 SL 5% 대비 2배)",
    "holding_assumption": (
        f"최대 관찰 {MAX_HOLD_DAYS}거래일 — 그 안에 손절/익절/트레일링이 없으면 마지막 관측 봉 "
        f"종가로 잔여 전량을 강제 청산해 완결로 센다(exit_reason=max_holding, 탈락 아님). "
        f"실엔진 setup별 live max_holding(예: sepa 10거래일)보다 느슨한 근사치다."
    ),
    "exit_assumption": (
        f"1차 +{TP1_PCT}%→{TP1_FRAC*100:.0f}%(원금 대비=잔여 대비, 시점상 동일) 매도, "
        f"2차 +{TP2_PCT}%→잔여의 {TP2_FRAC*100:.0f}%, 3차 +{TP3_PCT}%→잔여의 {TP3_FRAC*100:.0f}% "
        f"(1·2·3차 모두 마쳐도 잔여 {((1-TP1_FRAC)*(1-TP2_FRAC)*(1-TP3_FRAC))*100:.1f}% 남아 "
        f"트레일링·손절·관찰창 만기로만 종결 가능 — 사다리만 미러, stale_exit/stale_high 미구현) · "
        f"트레일링 +{TRAIL_ARM_PCT}%↑ 고점대비 -{TRAIL_DD_PCT}% · "
        f"손절 plan.stop_price(없으면 {DEFAULT_SL_PCT}%) · 같은 날 손절·익절 동시 충족 시 손절 우선(보수적)"
    ),
    "cost_assumption": "FeeCalculator 왕복(매수 0.0140527% + 매도 0.0130527%+세금0.20%) — KR 기본",
    "fill_assumption": (
        "selection 실험: 다음 거래일 시가 체결(계획 상한 무관, 실제 체결 근사). "
        "timing 실험: 기존=다음 시가 즉시, EntryPlan=check_entry_plan 재판정 — allow 나올 때까지 "
        "미체결(유리한 체결 생성 금지)"
    ),
    "sample_requirement": "완결 포지션 ≥30건/정책, 시간순 홀드아웃(마지막 1/3)에서 중앙값 R 부호 유지",
    "leakage_guard": (
        "판단 시각(date) 이전 필드만 사용 — evidence/votes/plan 은 후보일 스냅샷, prices 는 이후 봉만 읽는다. "
        "체결 시작점(existing_open·EntryPlan 공통)은 후보일 다음 첫 거래일 봉이다 — 후보일 당일 종가로 "
        "체결하지 않는다(판단 재료였던 봉으로 체결하면 미래정보가 된다). "
        "종목-주(ISO 연-주) 단위 클러스터링(같은 종목·같은 주 후보는 먼저 잡힌 것만 독립 표본으로 센다). "
        "겹치는 보유기간의 포지션을 독립 표본으로 취급하지 않는다(직전 포지션 보유기간과 겹치는 "
        "같은 종목 후속 진입은 표본에서 제외 — _dedupe_correlated, results 의 excluded_correlated 로 집계)."
    ),
    "benchmark": "KODEX200(069500) — 로컬 가격 캐시가 없으면 null. 초과수익은 계산하지 않는다(경로만 기록).",
    "policy_bc_implementation": (
        f"판단 정책 = src.agents.judgment.assess (policy_version={ASSESSMENT_POLICY_VERSION}) — "
        "스냅샷의 evidence(AnalystReport.to_dict() 형태)·votes 를 AnalystReport/DebateResult 로 "
        "복원해 호출한다(이 러너가 merit/위험/자료충분성을 자체 근사하지 않는다). "
        "B 는 votes.r1 을 최종 표로 준 DebateResult(R1 독립 판단만, 토론 전), "
        "C 는 votes.r2 를 최종 표로 준 DebateResult(R2 토론 후 만장일치)로 각각 재평가한다. "
        "스냅샷 evidence 가 data_status(full/partial)·근거(evidence, kind=fact) 를 채우지 않으면 "
        "(=A 근거계약 미배선) data_sufficiency 가 insufficient 가 되어 B/C 게이트에서 탈락한다"
        "(merit 자체는 evidence 가 있으면 계산될 수 있다) — 이때 B/C 선정이 0건이어도 버그가 아니다. "
        "보고서 신선도는 스냅샷의 age_minutes 로 복원한다(없으면 판단 시점에 신선 가정, "
        "results.reports_without_age 카운터). C 는 gate_b(R1 bear ACCEPT)를 전제로 하므로 "
        "R1 REJECT→R2 ACCEPT 전향 후보는 C 에서 제외된다(=live 팀 최종 판정보다 엄격) — "
        "results.r1_reject_r2_accept_excluded 카운터로 남긴다(정책 정의는 결과를 본 뒤 바꾸지 않는다)."
    ),
    "llm_reeval_limitation": (
        "과거 판단을 재현하는 R1/R2 표결·근거는 스냅샷 시점에 실제로 LLM 이 낸 결과가 아니라면 "
        "모델의 사후 지식(hindsight)이 섞였을 수 있다 — 스냅샷이 실시간 원장(team_ledger)에서 "
        "온 것이 아니라 사후 재평가로 만들어졌다면 이 한계가 적용된다."
    ),
    "known_limitations": (
        "(1) MAX_HOLD_DAYS 안에 손절/익절/트레일링이 없으면 마지막 관측 봉 종가로 강제 청산한다"
        "(exit_reason=max_holding) — 실제 체결이 아니라 관찰 창 마감 시점의 근사 마킹이고, "
        "실엔진 setup별 live max_holding(예: sepa 10거래일)보다 창이 넓어 강제청산 시점이 실제보다 "
        "늦을 수 있다. (2) stale_exit/stale_high(시간 기반 강제청산, 실엔진 HOLDING_POLICIES)는 "
        "미구현이다 — 이 러너의 청산은 분할익절·트레일링·손절·관찰창 만기 4가지뿐이라 "
        "live 청산정책의 '그대로 미러'가 아니다. "
        "(3) simulate_exit 은 같은 봉의 고가로 트레일링을 무장하고 같은 봉의 저가로 발동시킨다 — "
        "일봉만으로는 장중 고가·저가 선후를 알 수 없어 절대 R 수치는 근사치다(정책·팔 간 상대 "
        "비교는 동일 로직이라 편향이 작지만, 절대 수치를 승격 근거로 쓰지 말 것). "
        "(4) timing 실험의 EntryPlan 팔은 각 일봉의 자정을 판정 시각(now)으로 써서 check_entry_plan 을 "
        "부른다 — 배치 생성부의 계획 만료(expires_at=다음 영업일 15:30)는 그대로 적용되므로 후보일 "
        "다음다음 거래일부터는 PLAN_EXPIRED 로 미체결이 된다(일봉 해상도의 구조적 한계, 체결률이 "
        "낮게 나오는 방향). (5) 정책 B/C 의 성공확률·수익 개선은 이 도구가 만들어 내지 않는다."
    ),
}


# ── 스냅샷 ──────────────────────────────────────────────────────────────
@dataclass
class Candidate:
    date: str
    symbol: str
    strategy: str = ""
    setup: str = ""
    plan: Dict[str, Any] = field(default_factory=dict)
    evidence: List[Dict[str, Any]] = field(default_factory=list)
    votes: Dict[str, Any] = field(default_factory=dict)
    prices: List[Dict[str, Any]] = field(default_factory=list)
    synthetic: bool = False

    @property
    def score(self) -> float:
        raw = self.plan.get("score")
        if raw is None:
            return 0.0
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 0.0


def load_snapshot(path: Path) -> List[Candidate]:
    rows: List[Candidate] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        rows.append(Candidate(
            date=str(d.get("date") or ""), symbol=str(d.get("symbol") or ""),
            strategy=str(d.get("strategy") or ""), setup=str(d.get("setup") or ""),
            plan=d.get("plan") or {}, evidence=d.get("evidence") or [],
            votes=d.get("votes") or {}, prices=d.get("prices") or [],
            synthetic=bool(d.get("synthetic", False)),
        ))
    return rows


def _safe_int(v: Any) -> int:
    if v is None:
        return 0
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _safe_float(v: Any) -> float:
    if v is None:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


# ── judgment.assess 입력 복원 (스냅샷 dict → AnalystReport/DebateResult) ─────────────
def _parse_dt(v: Any) -> Optional[datetime]:
    """ISO 문자열 → datetime (없거나 손상되면 None — 시각을 지어내지 않는다)."""
    if isinstance(v, datetime):
        return v
    if not isinstance(v, str) or not v:
        return None
    try:
        return datetime.fromisoformat(v)
    except ValueError:
        return None


def _to_evidence_item(d: Dict[str, Any]) -> EvidenceItem:
    """AnalystReport.to_dict()['evidence'] 항목(EvidenceItem.to_dict()) 역직렬화.

    observed_at/collected_at/valid_until 을 복원해야 judgment._evidence_merit 의 근거 유효기간
    검사가 오프라인에서도 살아 있다. status 키가 없거나 비었으면 'full' 로 승격하지 않고
    'insufficient'(미상)로 둔다 — 결측을 가장 관대한 값으로 매핑하지 않는다 (R-D r5 advisory).
    """
    status = d.get("status")
    return EvidenceItem(
        source=str(d.get("source") or ""), metric=str(d.get("metric") or ""),
        value=d.get("value"), unit=d.get("unit"),
        observed_at=_parse_dt(d.get("observed_at")), collected_at=_parse_dt(d.get("collected_at")),
        period=d.get("period"),
        status=status if isinstance(status, str) and status else "insufficient",
        kind=str(d.get("kind") or "fact"),
        ref_id=d.get("ref_id"), valid_until=_parse_dt(d.get("valid_until")),
        dedup_key=d.get("dedup_key"),
    )


def _to_analyst_report(d: Dict[str, Any]) -> AnalystReport:
    """스냅샷의 AnalystReport.to_dict() 형태 dict → AnalystReport (judgment.assess 입력용).

    confidence·score 결측은 0 으로 매핑한다(0 을 "값 있음"으로 오인하지 않도록 명시적으로
    분기 — 'or 0' 패턴 금지). data_status 결측은 dataclass 기본값 "unknown" 그대로 둔다.
    """
    kind_raw = str(d.get("kind") or "technical")
    try:
        kind = AnalystKind(kind_raw)
    except ValueError:
        kind = AnalystKind.TECHNICAL
    nested = [_to_evidence_item(e) for e in (d.get("evidence") or [])]
    rep = AnalystReport(
        kind=kind, symbol=str(d.get("symbol") or ""), score=_safe_int(d.get("score")),
        confidence=_safe_float(d.get("confidence")), error=d.get("error"),
        data_status=str(d.get("data_status") or "unknown"),
        positive_basis=d.get("positive_basis"), risk_clear=d.get("risk_clear"),
        evidence=nested,
        observed_at=_parse_dt(d.get("observed_at")),
        limitations=list(d.get("limitations") or []),
    )
    # R-D r5 blocking: data_as_of 의 dataclass 기본값은 now 라 복원된 모든 보고서가 '방금 만든 것'
    # 이 된다(신선도 세탁) — judgment 의 유효성 판정(HARD_TTL·감쇠 가중치)이 전부 age_minutes 에
    # 걸려 있으므로 스냅샷이 기록한 나이를 그대로 되살린다. age_minutes 가 없는 구형·합성
    # 스냅샷은 '판단 시점에 신선' 가정으로 남고 그 사실을 results 에 카운터로 남긴다.
    age = d.get("age_minutes")
    if isinstance(age, (int, float)) and not isinstance(age, bool) and age >= 0:
        rep.data_as_of = datetime.now() - timedelta(minutes=float(age))
    return rep


def _debate_from_votes(symbol: str, votes: Dict[str, Any], upto_round: int) -> DebateResult:
    """votes({"r1": {"bull":..,"bear":..}, "r2": {...}}) → DebateResult.

    upto_round=1: R1 표를 최종으로 준다(정책 B — 토론 전 독립 판단만, judgment._risk_acceptable
    이 이 "최종" bear 값을 읽는다). upto_round=2: R2 표를 최종으로 준다(정책 C — 토론 후 만장일치).
    """
    r1 = votes.get("r1") or {}
    turns = [DebateTurn(round_no=1, side="bull", stance=r1.get("bull")),
             DebateTurn(round_no=1, side="bear", stance=r1.get("bear"))]
    if upto_round >= 2:
        r2 = votes.get("r2") or {}
        turns += [DebateTurn(round_no=2, side="bull", stance=r2.get("bull")),
                  DebateTurn(round_no=2, side="bear", stance=r2.get("bear"))]
        bull_final, bear_final = r2.get("bull"), r2.get("bear")
    else:
        bull_final, bear_final = r1.get("bull"), r1.get("bear")
    return DebateResult(symbol=symbol, turns=turns, bull_final=bull_final,
                         bear_final=bear_final, rounds_run=upto_round)


def _assess(c: Candidate, upto_round: int):
    """정책 B/C 공용 — src.agents.judgment.assess 를 그대로 호출한다(러너 자체 근사 없음)."""
    reports = [_to_analyst_report(e) for e in c.evidence]
    debate = _debate_from_votes(c.symbol, c.votes, upto_round)
    # now 를 넘겨야 EvidenceItem.valid_until 만료 검사가 동작한다(team.py 실배선과 동일)
    return judgment.assess(c.symbol, reports, debate, now=datetime.now())


# ── R1/R2 판단 레거시 유틸 — 정책 게이트에서는 더는 쓰지 않는다(judgment.assess 로 재배선,
#    §2.5). r1_assess 의 confidence 결측 처리(usable 판정)는 judgment._valid_reports 를
#    재사용해 이 함수와 실제 판단 경로의 "유효 보고서" 정의를 어긋나지 않게 유지한다. ──
def _usable_reports(evidence: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """confidence=0·결측·오류·만료 보고서는 제외 (인수조건 #2 — 유효 소스를 부풀리지 않는다).

    이전에는 confidence 가 아예 없는(None) 보고서를 유효로 세는 결함이 있었다(결측을
    '검사 통과'로 취급) — judgment._valid_reports(ok·신뢰도>0·TTL 이내)로 판정을 통일한다.
    """
    reports = [_to_analyst_report(e) for e in evidence]
    valid_ids = {id(r) for r in judgment._valid_reports(reports)}
    return [e for e, r in zip(evidence, reports) if id(r) in valid_ids]


def r1_assess(evidence: List[Dict[str, Any]]) -> Tuple[Optional[int], str, str]:
    """R1 독립 판단(레거시 유틸) — merit_score, merit_status, data_sufficiency.

    '검증 통과'·컨센서스는 가산하지 않는다 — positive_basis=True 인 보고서만 점수에 넣는다.
    """
    usable = _usable_reports(evidence)
    if not usable:
        return None, "abstain", "insufficient"
    positive = [r for r in usable if r.get("positive_basis") is True]
    if not positive:
        status = "insufficient"
        merit = 0
    elif len(positive) == 1:
        status, merit = "weak", sum(_safe_int(r.get("score")) for r in positive)
    else:
        status, merit = "sufficient", sum(_safe_int(r.get("score")) for r in positive)
    data_suff = "full" if len(usable) >= 3 else ("partial" if len(usable) >= 1 else "insufficient")
    return merit, status, data_suff


def risk_acceptable_r1(votes: Dict[str, Any]) -> Optional[bool]:
    """Bear 의 R1 독립 표결 — True(찬성/위험 수용) / False(반대) / None(기권·미기록)"""
    return (votes.get("r1") or {}).get("bear")


def unanimous_r2(votes: Dict[str, Any]) -> bool:
    r2 = votes.get("r2") or {}
    return r2.get("bull") is True and r2.get("bear") is True


# ── 정책 게이트 ──────────────────────────────────────────────────────────
def gate_a(_c: Candidate) -> bool:
    return True  # 기존 규칙: 스크리너 통과만으로 충분(팀 심의 없음)


def gate_b(c: Candidate) -> bool:
    """B — R1 독립 판단(§2.5): merit(sufficient/weak) ∧ risk_acceptable=True ∧ data_sufficiency≥partial.

    판정은 src.agents.judgment.assess 결과를 그대로 쓴다 — 이 함수가 자체적으로 점수를 다시
    계산하지 않는다.
    """
    a = _assess(c, upto_round=1)
    if a.merit_status not in ("sufficient", "weak"):
        return False
    if a.risk_acceptable is not True:
        return False
    if a.data_sufficiency == "insufficient":
        return False
    return True


def gate_c(c: Candidate) -> bool:
    """C — B + R2(토론) 최종 투표 만장일치(bull_final=bear_final=True). judgment.assess 의
    R2 DebateResult(votes.r2 최종) 결과에서 final_votes 를 그대로 읽는다."""
    if not gate_b(c):
        return False
    a2 = _assess(c, upto_round=2)
    return a2.final_votes.get("bull") is True and a2.final_votes.get("bear") is True


GATES = {"A": gate_a, "B": gate_b, "C": gate_c}


def select_candidates(rows: List[Candidate], policy: str, max_new: int) -> List[Candidate]:
    """날짜별로 정책 게이트를 통과한 후보 중 점수 상위 max_new 만 선정.

    세 정책 모두 같은 날짜의 같은 후보 풀에서 출발한다 — 게이트만 다르다(사후 선별 금지).
    """
    gate = GATES[policy]
    by_day: Dict[str, List[Candidate]] = {}
    for c in rows:
        by_day.setdefault(c.date, []).append(c)
    selected: List[Candidate] = []
    for day in sorted(by_day):
        pool = [c for c in by_day[day] if gate(c)]
        pool.sort(key=lambda c: c.score, reverse=True)
        selected.extend(pool[:max_new])
    return selected


# ── 청산 시뮬레이션 (분할익절·트레일링·손절·관찰창 만기 — stale_exit/stale_high 미구현) ──
_FEE = get_fee_calculator("KR")


def _net_pct(entry_price: float, exit_price: float) -> float:
    """수수료 반영 순수익률(%) — quantity 는 반올림 오차를 줄이기 위해 1000으로 고정(비율만 사용)."""
    from decimal import Decimal
    _pnl, pnl_pct = _FEE.calculate_net_pnl(Decimal(str(entry_price)), Decimal(str(exit_price)), 1000)
    return float(pnl_pct)


def _risk_pct(entry_price: float, stop_price: Optional[float]) -> float:
    if entry_price <= 0:
        return DEFAULT_SL_PCT
    if stop_price is not None and 0 < stop_price < entry_price:
        return (entry_price - stop_price) / entry_price * 100
    return DEFAULT_SL_PCT


def simulate_exit(bars: List[Dict[str, Any]], entry_idx: int, entry_price: float,
                   stop_price: Optional[float], skip_entry_bar: bool = False) -> Optional[Dict[str, Any]]:
    """분할 익절(1/2/3차) + 트레일링 + 손절 + 관찰창 만기 강제청산 — CLAUDE.md 청산정책의
    사다리만 미러한다(stale_exit/stale_high 시간 기반 청산은 미구현, known_limitations 참조).

    같은 봉에서 손절가와 익절 라인이 함께 걸리면 낙관적 체결을 만들지 않도록 손절을 우선한다.
    MAX_HOLD_DAYS 안에 손절/익절/트레일링이 없으면 마지막 관측 봉 종가로 잔여 전량을 강제
    청산한다(exit_reason="max_holding") — 예전엔 "미완결"로 표본에서 제외했으나, 그러면
    표본에 남는지 여부가 결과(얼마나 오래 걸렸나)와 상관돼 승자가 미완결로 빠지기 쉬운
    하방 편향이 생긴다(리뷰 blocking 2026-09-15). None 은 스캔할 봉 자체가 없을 때만.

    skip_entry_bar: 체결이 그 봉의 **종가**(예: EntryPlan)일 때 True로 둔다 — 체결 이전에
    지나간 그 봉의 저가·고가로 손절·익절·트레일링을 판정하면 아직 일어나지 않은 가격으로
    청산을 만들게 된다(리뷰 blocking, 2026-09-15). 시가 체결(existing_open)은 봉 시작이
    곧 체결 시점이라 그 봉부터 스캔해도 선행정보가 아니다 — 기본 False 로 둔다.
    holding_days 는 두 방식이 같은 의미가 되도록 항상 entry_idx 기준으로 유지한다.
    """
    if entry_price <= 0:
        return None
    remaining = 1.0
    realized = 0.0
    stage = 0
    high_wm = entry_price
    trail_armed = False
    # stop_price 결측·무효(0<stop<entry 불만족, 예: 갭하락 진입) 시 PRE_REGISTERED.exit_assumption
    # 그대로 DEFAULT_SL_PCT 대체선을 적용한다 — _risk_pct 의 분모(R)도 같은 대체값을 쓰므로
    # 결측을 "무위험"(손절 미적용)으로 새지 않고 사전등록된 가정과 일치시킨다(리뷰 blocking 2026-09-15).
    stop_px = stop_price if (stop_price is not None and 0 < stop_price < entry_price) \
        else entry_price * (1 - DEFAULT_SL_PCT / 100)
    end = min(entry_idx + MAX_HOLD_DAYS, len(bars))
    start = entry_idx + 1 if skip_entry_bar else entry_idx
    for i in range(start, end):
        bar = bars[i]
        try:
            lo, hi = float(bar["low"]), float(bar["high"])
        except (KeyError, TypeError, ValueError):
            continue
        high_wm = max(high_wm, hi)
        chg_high = (hi - entry_price) / entry_price * 100
        if chg_high >= TRAIL_ARM_PCT:
            trail_armed = True
        if stop_px is not None and lo <= stop_px:
            realized += remaining * _net_pct(entry_price, stop_px)
            return {"exit_date": bar.get("date"), "holding_days": i - entry_idx,
                    "net_pct": realized, "exit_reason": "stop_loss"}
        # 익절 사다리를 트레일링 발동 검사보다 먼저 평가한다(advisory 2026-09-15) — 순서가 반대면
        # 같은 봉에서 무장·발동이 겹칠 때 +10% 1차 익절을 건너뛰고 트레일링으로 전량 종결돼
        # live 청산 사다리(분할 익절 우선) 미러와 어긋난다.
        for stage_no, pct, frac in ((1, TP1_PCT, TP1_FRAC), (2, TP2_PCT, TP2_FRAC), (3, TP3_PCT, TP3_FRAC)):
            if stage < stage_no and chg_high >= pct and remaining > 1e-9:
                # CLAUDE.md: 1차는 "10% 매도"(remaining=1.0 시점이라 원금 대비=잔여 대비 동일),
                # 2·3차는 "잔여의 50%" — frac 은 항상 잔여 대비 비율로 적용한다(원금 대비 절대 비율 아님).
                sell_frac = min(remaining * frac, remaining)
                exit_px = entry_price * (1 + pct / 100)
                realized += sell_frac * _net_pct(entry_price, exit_px)
                remaining -= sell_frac
                stage = stage_no
        if remaining <= 1e-9:
            return {"exit_date": bar.get("date"), "holding_days": i - entry_idx,
                    "net_pct": realized, "exit_reason": f"take_profit_{stage}"}
        if trail_armed:
            trail_stop = high_wm * (1 - TRAIL_DD_PCT / 100)
            if lo <= trail_stop:
                realized += remaining * _net_pct(entry_price, trail_stop)
                return {"exit_date": bar.get("date"), "holding_days": i - entry_idx,
                        "net_pct": realized, "exit_reason": "trailing"}
    # 관찰 창 안에 손절/익절/트레일링 트리거가 없었다 — 마지막 관측 봉 종가로 잔여 전량을
    # 강제 청산해 완결로 센다(탈락이 아니라 청산, 리뷰 blocking 2026-09-15).
    last_i = end - 1
    if last_i < start:
        return None  # 스캔할 봉이 아예 없음(체결 직후 데이터 종료) — 완결 불가
    last_bar = bars[last_i]
    try:
        last_close = float(last_bar["close"])
    except (KeyError, TypeError, ValueError):
        return None
    realized += remaining * _net_pct(entry_price, last_close)
    return {"exit_date": last_bar.get("date"), "holding_days": last_i - entry_idx,
            "net_pct": realized, "exit_reason": "max_holding"}


def _find_bar_index(prices: List[Dict[str, Any]], on_or_after: str) -> Optional[int]:
    for i, bar in enumerate(prices):
        if str(bar.get("date", "")) >= on_or_after:
            return i
    return None


def _first_tradable_idx(c: Candidate) -> Optional[int]:
    """후보일 다음 거래일 봉 인덱스 — 후보일 당일 봉은 판단 재료일 뿐 체결 대상이 아니다.

    existing_open·EntryPlan 두 체결 방식이 반드시 이 같은 인덱스에서 스캔을 시작해야
    timing 실험에서 어느 쪽도 상대에게 없는 선행정보(후보일 당일 종가 등)를 얻지 않는다.
    """
    idx = _find_bar_index(c.prices, c.date)
    if idx is None:
        return None
    nxt = idx + 1 if str(c.prices[idx].get("date")) == c.date else idx
    if nxt >= len(c.prices):
        return None
    return nxt


def _next_open_entry(c: Candidate) -> Optional[Tuple[int, float]]:
    """후보일 다음 거래일 시가."""
    nxt = _first_tradable_idx(c)
    if nxt is None:
        return None
    try:
        return nxt, float(c.prices[nxt]["open"])
    except (KeyError, TypeError, ValueError):
        return None


def _entry_plan_fill(c: Candidate, start_idx: int) -> Tuple[Optional[int], Optional[float], Optional[str]]:
    """EntryPlan 조건부 진입 — 정본 검증기(check_entry_plan)를 그대로 재사용한다.

    판정 로직을 여기서 다시 만들지 않는다 — checker 가 placeholder 면 전부 wait 가 나오는 게
    정상이고(체결 0건), 담당 C 가 구현을 마치면 이 러너를 다시 돌려 실결과를 얻는다.
    """
    plan = c.plan
    for i in range(start_idx, len(c.prices)):
        bar = c.prices[i]
        try:
            price = float(bar["close"])
        except (KeyError, TypeError, ValueError):
            continue
        quote = {"price": price, "as_of": bar.get("date"), "vwap": bar.get("vwap")}
        try:
            now = datetime.fromisoformat(str(bar.get("date")))
        except ValueError:
            now = datetime.now()
        check = check_entry_plan(plan, quote, now)
        if check.status == "allow":
            efp = check.expected_fill_price
            fill = efp if (efp is not None and efp > 0) else price
            return i, float(fill), None
        if check.status == "reject":
            return None, None, "|".join(check.reasons) or "reject"
    return None, None, "no_fill_in_window"


# ── 표본 독립성(leakage_guard) ──────────────────────────────────────────
def _week_key(date_str: str) -> str:
    """ISO 연-주 키 — 같은 종목·같은 주 후보를 하나의 클러스터로 묶기 위함.

    날짜 파싱이 안 되면(빈 값 등) 원본 문자열을 그대로 키로 써서 클러스터를 나누지 않는
    (=서로 다른 표본으로 잘못 합치지 않는) 쪽으로 보수적으로 처리한다.
    """
    try:
        y, w, _ = _date.fromisoformat(date_str).isocalendar()
        return f"{y}-W{w:02d}"
    except (ValueError, TypeError):
        return date_str


def _dedupe_correlated(candidates: List[Candidate]) -> Tuple[List[Candidate], int]:
    """leakage_guard 이행 — 종목-주 클러스터·보유기간 겹침 후보를 독립 표본에서 제외한다.

    시뮬레이션 **전**(후보 단계)에 적용한다 — 실제 청산 시점(holding_days, 시뮬레이션 결과)
    으로 겹침을 판정하면 표본에 남는지 여부가 시뮬레이션 경로에 좌우돼 §2.5 사전등록 표본
    요건(사후 선별 금지)과 상충한다(advisory, 2026-09-15). 대신 관찰 창 상한(MAX_HOLD_DAYS)을
    보수적 보유기간 가정으로 쓴다 — 날짜순으로 먼저 잡힌 후보만 남긴다: 같은 종목이 같은
    ISO 주에 다시 후보로 잡히거나, 직전 후보의 가정 보유기간(entry~entry+MAX_HOLD_DAYS)과
    겹치는 후속 후보는 사실상 같은 베팅의 반복이라 독립 표본으로 세지 않는다.
    """
    ordered = sorted(candidates, key=lambda c: (c.symbol, c.date))
    kept: List[Candidate] = []
    excluded = 0
    prev_end_by_symbol: Dict[str, int] = {}
    seen_week: set = set()
    for c in ordered:
        wk = (c.symbol, _week_key(c.date))
        try:
            cur_entry_ord: Optional[int] = _date.fromisoformat(c.date).toordinal()
        except (ValueError, TypeError):
            cur_entry_ord = None
        prev_end = prev_end_by_symbol.get(c.symbol)
        overlap = prev_end is not None and cur_entry_ord is not None and cur_entry_ord <= prev_end
        if wk in seen_week or overlap:
            excluded += 1
            continue
        seen_week.add(wk)
        if cur_entry_ord is not None:
            # MAX_HOLD_DAYS 는 거래일 단위, ordinal 은 달력일 — 7/5 로 환산(R-D r5 advisory)
            prev_end_by_symbol[c.symbol] = cur_entry_ord + int(MAX_HOLD_DAYS * 7 / 5)
        kept.append(c)
    return kept, excluded


# ── 실험 실행 ────────────────────────────────────────────────────────────
def run_selection_experiment(rows: List[Candidate], policies: List[str], max_new: int) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for policy in policies:
        selected = select_candidates(rows, policy, max_new)
        deduped_candidates, excluded = _dedupe_correlated(selected)  # 시뮬레이션 전에 적용
        positions: List[Dict[str, Any]] = []
        no_data = 0
        incomplete = 0
        for c in deduped_candidates:
            entry = _next_open_entry(c)
            if entry is None:
                no_data += 1
                continue
            idx, entry_px = entry
            res = simulate_exit(c.prices, idx, entry_px, c.plan.get("stop_price"))
            if res is None:
                incomplete += 1  # 스캔할 봉 자체가 없음(체결 직후 데이터 종료) — 완결 불가
                continue
            risk = _risk_pct(entry_px, c.plan.get("stop_price"))
            r = res["net_pct"] / risk if risk > 0 else None
            if r is None:
                incomplete += 1
                continue
            positions.append({
                "symbol": c.symbol, "date": c.date, "week": _week_key(c.date),
                "entry_price": entry_px, "net_pct": res["net_pct"], "r": r,
                "holding_days": res["holding_days"], "exit_reason": res["exit_reason"],
            })
        out[policy] = _summarize_positions(policy, selected, positions, no_data, incomplete, excluded)
        out[policy]["reports_without_age"] = sum(
            1 for c in rows for e in (c.evidence or []) if e.get("age_minutes") is None
        )
        if policy == "C":
            out[policy]["r1_reject_r2_accept_excluded"] = sum(
                1 for c in rows
                if (c.votes.get("r1") or {}).get("bear") is False
                and (c.votes.get("r2") or {}).get("bull") is True
                and (c.votes.get("r2") or {}).get("bear") is True
            )
    return out


def run_timing_experiment(rows: List[Candidate], fixed_policy: str, max_new: int) -> Dict[str, Any]:
    selected = select_candidates(rows, fixed_policy, max_new)
    deduped_candidates, excluded = _dedupe_correlated(selected)  # 시뮬레이션 전에 적용, 두 팔 공용
    out: Dict[str, Any] = {}
    for mode in ("existing_open", "entry_plan"):
        positions: List[Dict[str, Any]] = []
        no_data = 0
        incomplete = 0
        unfilled = 0
        wait_days: List[int] = []
        opportunity_cost: List[float] = []
        for c in deduped_candidates:
            # 두 체결 방식이 반드시 같은 첫 관찰 봉(start_idx)에서 스캔을 시작한다 —
            # EntryPlan 쪽이 후보일 당일 봉(판단 재료)으로 체결해 선행정보 우위를 얻지 않도록.
            start_idx = _first_tradable_idx(c)
            if start_idx is None:
                no_data += 1
                continue
            if mode == "existing_open":
                entry = _next_open_entry(c)
                if entry is None:
                    no_data += 1
                    continue
                idx, entry_px = entry
            else:
                idx, entry_px, _reason = _entry_plan_fill(c, start_idx)
                if idx is None or entry_px is None:
                    unfilled += 1
                    # 미체결 기회비용 — 기존 방식(다음 시가) 이었다면 얻었을 R을 참고용으로 기록
                    base_entry = _next_open_entry(c)
                    if base_entry is not None:
                        bidx, bpx = base_entry
                        bres = simulate_exit(c.prices, bidx, bpx, c.plan.get("stop_price"))
                        if bres is not None:
                            brisk = _risk_pct(bpx, c.plan.get("stop_price"))
                            if brisk > 0:
                                opportunity_cost.append(bres["net_pct"] / brisk)
                    continue
                wait_days.append(idx - start_idx)
            res = simulate_exit(c.prices, idx, entry_px, c.plan.get("stop_price"),
                                 skip_entry_bar=(mode == "entry_plan"))
            if res is None:
                incomplete += 1
                continue
            risk = _risk_pct(entry_px, c.plan.get("stop_price"))
            if risk <= 0:
                incomplete += 1
                continue
            positions.append({
                "symbol": c.symbol, "date": c.date, "week": _week_key(c.date), "entry_price": entry_px,
                "net_pct": res["net_pct"], "r": res["net_pct"] / risk,
                "holding_days": res["holding_days"], "exit_reason": res["exit_reason"],
            })
        summary = _summarize_positions(mode, selected, positions, no_data, incomplete, excluded)
        summary["unfilled"] = unfilled
        # 체결률 = (실제 시도한 후보 - 미체결 - 데이터없음) / 실제 시도한 후보. dedup 은 시뮬레이션
        # 전에 적용되므로 분모 자체가 dedup 이후 후보 수다(deduped_candidates) — dedup 제외는
        # results 의 excluded_correlated 로 별도 집계한다(advisory, 2026-09-15).
        summary["fill_rate"] = (
            round((len(deduped_candidates) - unfilled - no_data) / len(deduped_candidates), 4)
            if deduped_candidates else None
        )
        summary["avg_wait_days"] = round(sum(wait_days) / len(wait_days), 2) if wait_days else None
        summary["unfilled_opportunity_cost_median_r"] = (
            round(statistics.median(opportunity_cost), 4) if opportunity_cost else None
        )
        summary["reports_without_age"] = sum(
            1 for c in rows for e in (c.evidence or []) if e.get("age_minutes") is None
        )
        out[mode] = summary
    return out


def _summarize_positions(label: str, selected: List[Candidate], positions: List[Dict[str, Any]],
                          no_data: int, incomplete: int, excluded_correlated: int) -> Dict[str, Any]:
    n = len(positions)
    rs = [p["r"] for p in positions]
    # 표본요건: 시간순 정렬 후 마지막 1/3을 홀드아웃으로 분리해 부호 유지 확인
    holdout_sign_match = None
    if n >= 30:
        ordered = sorted(positions, key=lambda p: p["date"])
        cut = max(1, n * 2 // 3)
        holdout = ordered[cut:]
        if holdout:
            full_med = statistics.median(rs)
            hold_med = statistics.median(p["r"] for p in holdout)
            holdout_sign_match = (full_med >= 0) == (hold_med >= 0)
    return {
        "label": label,
        "candidates_selected": len(selected),
        "completed_positions": n,
        "no_data": no_data,                       # 체결 대상 봉 자체가 없음(데이터 끝 등)
        "incomplete": incomplete,                  # 스캔할 봉 자체가 없음(체결 직후 데이터 종료) / 위험값 계산 불가
        "excluded_correlated": excluded_correlated,  # leakage_guard: 종목-주 클러스터·보유기간 겹침 제외(후보 단계)
        "median_r": round(statistics.median(rs), 4) if rs else None,
        "mean_r": round(sum(rs) / n, 4) if rs else None,
        "net_pnl_pct_sum": round(sum(p["net_pct"] for p in positions), 2) if positions else None,
        "avg_holding_days": round(sum(p["holding_days"] for p in positions) / n, 2) if n else None,
        "sample_requirement_met": n >= 30,
        "holdout_sign_match": holdout_sign_match,
    }


# ── 출력 ─────────────────────────────────────────────────────────────────
def build_manifest(policies: List[str], experiment: str, snapshot_path: str,
                    all_synthetic: bool, benchmark_path: Optional[str]) -> Dict[str, Any]:
    return {
        "tool": "scripts/team_policy_ab.py",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "snapshot": snapshot_path,
        "policies": policies,
        "experiment": experiment,
        "pre_registered": PRE_REGISTERED,
        # computed=False — 경로만 기록하고 초과수익은 계산하지 않는다(계산이 이뤄진 것처럼 읽히지 않게).
        "benchmark_kodex200": {"path": benchmark_path, "computed": False} if benchmark_path else None,
        "note": "이 도구는 승격 판정을 하지 않는다 — 사전등록 지표를 보고할 뿐이다.",
        "all_rows_synthetic": all_synthetic,
        # 정책 B/C 판단에 실제로 쓰인 judgment.assess 계약 버전 — §2.5, 재현·회귀 추적용.
        "judgment_policy_version": ASSESSMENT_POLICY_VERSION,
    }


def build_report_md(manifest: Dict[str, Any], results: Dict[str, Any]) -> str:
    lines = [f"# 팀 정책 A/B/C 비교 — {manifest['experiment']} 실험", ""]
    status = "synthetic_only" if manifest["all_rows_synthetic"] else "unverified"
    if status == "synthetic_only":
        lines.append("> **미검증/보류** — 합성 fixture 로 도구 동작만 확인했다. 실 데이터 재실행 전까지 "
                      "아래 수치는 어떤 정책·운영 결정의 근거도 될 수 없다.")
    else:
        lines.append("> **미검증** — 표본 요건·홀드아웃 검증을 통과해도 이 도구 자체는 승격 판정을 내리지 않는다. "
                      "실배분 승격은 별도 사용자 승인이 필요하다.")
    lines.append("")
    lines.append(PRE_REGISTERED["llm_reeval_limitation"])
    lines.append("")
    lines.append("**알려진 한계**: " + PRE_REGISTERED["known_limitations"])
    lines.append("")
    for key, stat in results.items():
        lines.append(f"## {key}")
        for k, v in stat.items():
            lines.append(f"- {k}: {v}")
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="팀 정책 A/B/C 오프라인 비교 (T11 §2.5)")
    ap.add_argument("--snapshot", required=True, help="후보 스냅샷 JSONL 경로")
    ap.add_argument("--policy", default="all", choices=["A", "B", "C", "all"])
    ap.add_argument("--experiment", default="selection", choices=list(EXPERIMENTS))
    ap.add_argument("--out", required=True, help="결과 출력 디렉터리")
    ap.add_argument("--max-new-per-day", type=int, default=DEFAULT_MAX_NEW_PER_DAY)
    ap.add_argument("--fixed-policy", default="C", choices=list(POLICIES),
                     help="timing 실험에서 선정을 고정할 정책")
    ap.add_argument("--benchmark", default=None, help="KODEX200 일봉 JSON(선택) — 없으면 null")
    args = ap.parse_args()

    snap_path = Path(args.snapshot)
    rows = load_snapshot(snap_path)
    policies = list(POLICIES) if args.policy == "all" else [args.policy]
    all_synthetic = bool(rows) and all(c.synthetic for c in rows)

    if args.experiment == "selection":
        results = run_selection_experiment(rows, policies, args.max_new_per_day)
    else:
        results = run_timing_experiment(rows, args.fixed_policy, args.max_new_per_day)

    validation_status = "synthetic_only" if all_synthetic else "unverified"
    manifest = build_manifest(policies, args.experiment, str(snap_path), all_synthetic, args.benchmark)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    results_payload = {
        "validation_status": validation_status,
        "n_candidates_in_snapshot": len(rows),
        # 합성/실데이터 혼합 스냅샷의 합성 행 수 — all_rows_synthetic=False 여도 내역이 보이도록
        # 기록한다(advisory, 2026-09-15: 혼합인지 전부 비합성인지 결과만 봐선 구분이 안 됐다).
        "n_synthetic_rows": sum(1 for c in rows if c.synthetic),
        "results": results,
    }
    (out_dir / "results.json").write_text(json.dumps(results_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "report.md").write_text(build_report_md(manifest, results), encoding="utf-8")
    print(f"[team_policy_ab] {validation_status} — {out_dir}/{{manifest.json,results.json,report.md}}")


if __name__ == "__main__":
    main()
