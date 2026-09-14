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
    selection — 정책(A/B/C)만 비교. 진입=다음 거래일 시가, 청산=live 청산정책 미러,
                위험예산·비용 고정.
    timing    — 선정은 고정(기본 C 정책)하고 진입만 비교: 기존(다음 시가 즉시) vs
                EntryPlan 조건부(`src.execution.entry_plan.check_entry_plan` 을 그대로
                재사용 — 판정 로직을 여기서 중복 구현하지 않는다). checker 가 아직
                placeholder(CHECKER_NOT_IMPLEMENTED)면 전부 미체결이 나오는 게 정상이다.

입력 스냅샷(JSONL, 한 줄 = 후보 1건):
    {"date": "2026-08-01", "symbol": "005930", "strategy": "sepa_trend", "setup": "sepa_pullback",
     "plan": {...PendingSignal.to_dict() 형태 — score/stop_price/entry_band_low/trigger/max_entry_price...},
     "evidence": [...AnalystReport.to_dict() 형태 3건(fundamental/technical/news)...],
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
from datetime import date as _date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.utils.fee_calculator import get_fee_calculator  # noqa: E402
from src.execution.entry_plan import check_entry_plan  # noqa: E402

POLICIES = ("A", "B", "C")
EXPERIMENTS = ("selection", "timing")

# ── 청산정책 임계값 — CLAUDE.md "청산 관리(ExitManager)" 절 그대로 미러 (계획값, 사후 변경 금지) ──
DEFAULT_SL_PCT = 5.0            # ATR 동적 손절 기본값 (plan.stop_price 없을 때만 대체 사용)
TP1_PCT, TP1_FRAC = 10.0, 0.10  # 1차 익절
TP2_PCT, TP2_FRAC = 15.0, 0.50  # 2차 익절
TP3_PCT, TP3_FRAC = 25.0, 0.50  # 3차 익절
TRAIL_ARM_PCT = 5.0             # 트레일링 활성화 임계 수익률
TRAIL_DD_PCT = 3.0              # 트레일링 낙폭
MAX_HOLD_DAYS = 20               # 관찰 창 상한(연구용) — 이 안에 청산 안 되면 미완결로 제외

DEFAULT_MAX_NEW_PER_DAY = 5      # CLAUDE.md KR 리스크 "일일 신규 매수 5개" 미러

# 사전 등록(결과 전 고정) — manifest.json 에 그대로 옮겨 적는다. 여기 숫자를 결과 보고 바꾸지 않는다.
PRE_REGISTERED = {
    "primary_metric": "포지션당 비용 차감 R(risk-adjusted return) — 중앙값·평균",
    "mde": "중앙값 R +0.10 (≈ 왕복 수수료 0.227%의 SL 5% 대비 2배)",
    "holding_assumption": f"최대 관찰 {MAX_HOLD_DAYS}거래일, 이 안에 청산 트리거 없으면 미완결로 제외",
    "exit_assumption": (
        f"1차 +{TP1_PCT}%→{TP1_FRAC*100:.0f}%(원금 대비=잔여 대비, 시점상 동일) 매도, "
        f"2차 +{TP2_PCT}%→잔여의 {TP2_FRAC*100:.0f}%, 3차 +{TP3_PCT}%→잔여의 {TP3_FRAC*100:.0f}% "
        f"(1·2·3차 모두 마쳐도 잔여 {((1-TP1_FRAC)*(1-TP2_FRAC)*(1-TP3_FRAC))*100:.1f}% 남아 "
        f"트레일링·손절로만 종결 가능 — live 미러) · "
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
        "정책 B/C 는 이 SHA 시점에 src.agents.judgment.assess 가 없어 이 러너 자체 구현"
        "(r1_assess/_usable_reports/unanimous_r2)을 근사치로 쓴다 — 실제 배포된 judgment.assess 결과와 "
        "다를 수 있다. 통합 후에는 judgment.assess 로 재배선해야 한다."
    ),
    "llm_reeval_limitation": (
        "과거 판단을 재현하는 R1/R2 표결·근거는 스냅샷 시점에 실제로 LLM 이 낸 결과가 아니라면 "
        "모델의 사후 지식(hindsight)이 섞였을 수 있다 — 스냅샷이 실시간 원장(team_ledger)에서 "
        "온 것이 아니라 사후 재평가로 만들어졌다면 이 한계가 적용된다."
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
        try:
            return float(self.plan.get("score", 0) or 0)
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


# ── R1/R2 판단 (계약 2.2 규칙의 오프라인 재현 — 실제 src.agents.judgment 와는 독립) ──
def _usable_reports(evidence: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """confidence=0·오류 보고서는 제외 (인수조건 #2 — 유효 소스를 부풀리지 않는다)"""
    out = []
    for r in evidence:
        if r.get("error"):
            continue
        conf = r.get("confidence")
        if conf is not None and float(conf) <= 0:
            continue
        out.append(r)
    return out


def r1_assess(evidence: List[Dict[str, Any]]) -> Tuple[Optional[int], str, str]:
    """R1 독립 판단 — merit_score, merit_status, data_sufficiency.

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
        status, merit = "weak", sum(int(r.get("score", 0) or 0) for r in positive)
    else:
        status, merit = "sufficient", sum(int(r.get("score", 0) or 0) for r in positive)
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
    _score, status, data_suff = r1_assess(c.evidence)
    if status not in ("sufficient", "weak"):
        return False
    if risk_acceptable_r1(c.votes) is not True:
        return False
    if data_suff == "insufficient":
        return False
    return True


def gate_c(c: Candidate) -> bool:
    return gate_b(c) and unanimous_r2(c.votes)


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


# ── 청산 시뮬레이션 (live 청산정책 미러) ────────────────────────────────
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
                   stop_price: Optional[float]) -> Optional[Dict[str, Any]]:
    """분할 익절(1/2/3차) + 트레일링 + 손절 — CLAUDE.md 청산정책 그대로 미러.

    같은 봉에서 손절가와 익절 라인이 함께 걸리면 낙관적 체결을 만들지 않도록 손절을 우선한다.
    MAX_HOLD_DAYS 안에 전량 청산되지 않으면 미완결(None) — 완결 포지션만 판정에 쓴다.
    """
    if entry_price <= 0:
        return None
    remaining = 1.0
    realized = 0.0
    stage = 0
    high_wm = entry_price
    trail_armed = False
    stop_px = stop_price if (stop_price is not None and 0 < stop_price < entry_price) else None
    end = min(entry_idx + MAX_HOLD_DAYS, len(bars))
    for i in range(entry_idx, end):
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
        if trail_armed:
            trail_stop = high_wm * (1 - TRAIL_DD_PCT / 100)
            if lo <= trail_stop:
                realized += remaining * _net_pct(entry_price, trail_stop)
                return {"exit_date": bar.get("date"), "holding_days": i - entry_idx,
                        "net_pct": realized, "exit_reason": "trailing"}
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
    return None  # 관찰 창 안에 청산 안 됨 — 미완결


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
            fill = check.expected_fill_price or price
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


def _dedupe_correlated(positions: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
    """leakage_guard 이행 — 종목-주 클러스터·보유기간 겹침 표본을 독립 표본에서 제외한다.

    날짜순으로 먼저 잡힌 진입만 남긴다: 같은 종목이 같은 ISO 주에 다시 후보로 잡히거나,
    직전 포지션의 보유기간(entry~entry+holding_days)과 겹치는 후속 진입은 사실상 같은
    베팅의 반복이라 독립 표본으로 세지 않는다(§2.5 사전등록 표본 요건의 전제).
    """
    ordered = sorted(positions, key=lambda p: (p["symbol"], p["date"]))
    kept: List[Dict[str, Any]] = []
    excluded = 0
    last_by_symbol: Dict[str, Dict[str, Any]] = {}
    seen_week: set = set()
    for p in ordered:
        wk = (p["symbol"], p.get("week"))
        prev = last_by_symbol.get(p["symbol"])
        overlap = False
        if prev is not None:
            try:
                prev_entry = _date.fromisoformat(prev["date"])
                cur_entry = _date.fromisoformat(p["date"])
                prev_end = prev_entry.toordinal() + int(prev.get("holding_days") or 0)
                overlap = cur_entry.toordinal() <= prev_end
            except (ValueError, TypeError):
                overlap = False
        if wk in seen_week or overlap:
            excluded += 1
            continue
        seen_week.add(wk)
        last_by_symbol[p["symbol"]] = p
        kept.append(p)
    return kept, excluded


# ── 실험 실행 ────────────────────────────────────────────────────────────
def run_selection_experiment(rows: List[Candidate], policies: List[str], max_new: int) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for policy in policies:
        selected = select_candidates(rows, policy, max_new)
        positions: List[Dict[str, Any]] = []
        no_data = 0
        incomplete = 0
        for c in selected:
            entry = _next_open_entry(c)
            if entry is None:
                no_data += 1
                continue
            idx, entry_px = entry
            res = simulate_exit(c.prices, idx, entry_px, c.plan.get("stop_price"))
            if res is None:
                incomplete += 1  # 관찰 창 안에 미청산 — 완결 표본에서 제외(사전등록)
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
        deduped, excluded = _dedupe_correlated(positions)
        out[policy] = _summarize_positions(policy, selected, deduped, no_data, incomplete, excluded)
    return out


def run_timing_experiment(rows: List[Candidate], fixed_policy: str, max_new: int) -> Dict[str, Any]:
    selected = select_candidates(rows, fixed_policy, max_new)
    out: Dict[str, Any] = {}
    for mode in ("existing_open", "entry_plan"):
        positions: List[Dict[str, Any]] = []
        no_data = 0
        incomplete = 0
        unfilled = 0
        wait_days: List[int] = []
        opportunity_cost: List[float] = []
        for c in selected:
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
            res = simulate_exit(c.prices, idx, entry_px, c.plan.get("stop_price"))
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
        deduped, excluded = _dedupe_correlated(positions)
        summary = _summarize_positions(mode, selected, deduped, no_data, incomplete, excluded)
        summary["unfilled"] = unfilled
        summary["fill_rate"] = round(len(deduped) / len(selected), 4) if selected else None
        summary["avg_wait_days"] = round(sum(wait_days) / len(wait_days), 2) if wait_days else None
        summary["unfilled_opportunity_cost_median_r"] = (
            round(statistics.median(opportunity_cost), 4) if opportunity_cost else None
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
        "incomplete": incomplete,                  # 관찰 창 안에 청산 안 됨 / 위험값 계산 불가
        "excluded_correlated": excluded_correlated,  # leakage_guard: 종목-주 클러스터·보유기간 겹침 제외
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
        "results": results,
    }
    (out_dir / "results.json").write_text(json.dumps(results_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "report.md").write_text(build_report_md(manifest, results), encoding="utf-8")
    print(f"[team_policy_ab] {validation_status} — {out_dir}/{{manifest.json,results.json,report.md}}")


if __name__ == "__main__":
    main()
