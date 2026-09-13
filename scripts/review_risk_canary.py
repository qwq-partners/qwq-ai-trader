#!/usr/bin/env python3
"""위험 기반 사이징 canary 오프라인 리포트 (계획서 T8, 2026-09).

검증용 포지션 원장 JSON 을 읽어 첫 5·10·30개 완결 포지션의 기술 검증과 성과 지표를
JSON 리포트로 쓴다. **주문·설정 변경 기능 없음** — 입력만 읽고 리포트만 쓴다.
네트워크·운영 상태파일(~/.cache/ai_trader)·KIS 자격증명에 접근하지 않는다.

사용:
    venv/bin/python scripts/review_risk_canary.py \\
        --input ledger.json --benchmark kodex200.csv \\
        --cohort risk-sepa-v1 --output report.json [--sha <SHA>] [--min-sample 30] [--as-of 2026-10-01]

종료 코드:
    2 — 파일 누락·JSON 파싱 실패·필수 필드 오류 (부족 항목을 stderr 에 출력, 리포트 미작성)
    0 — 데이터 정상. 성과 판정은 리포트의 ``status`` 로만 표현한다.

리포트 status (성과):
    insufficient_sample — 완결 표본 < --min-sample. 지표는 있는 만큼 계산. 판정 보류.
    hold_expansion      — 완결 ≥ min-sample 이고 평균 R≤0 또는 PF≤1 또는 (벤치마크 있을 때) 평균 초과수익≤0. 확대 보류.
    further_review      — 그 외. 자동 승격이 아니라 추가 기간·표본 검토 대상.
technical_status (기술 검증, 성과와 별도): passed | failed (+ technical_issues 목록).

입력 원장 JSON 스키마 (T3 가 이 형식으로 내보낸다. 금액은 문자열 Decimal, 시각은 ISO8601):
    {
      "version": 1,
      "generated_at": "2026-10-01T18:00:00+09:00",
      "positions": [
        {
          "position_id": "...", "symbol": "005930", "strategy": "sepa_trend",
          "cohort_id": "risk-sepa-v1", "applied_sha": "abc1234",
          "status": "closed" | "open",
          "entry_risk": {                       # T3 entry_risk 스냅샷 그대로. null 이면 legacy/unmeasured
            "version": 1, "cohort_id": "risk-sepa-v1", "sizing_mode": "risk",
            "strategy": "sepa_trend", "stop_basis": "net_pnl", "stop_pct": "5.0",
            "stop_source": "strategy", "equity_at_decision": "10000000",
            "risk_budget_amount": "70000", "planned_price": "10000",
            "planned_quantity": 139, "planned_risk_amount": "69509.75"
          },
          "initial_risk_amount": "69500" | null,   # 최초 진입 주문 완료 시 확정 (R 분모)
          "planned_vs_filled_risk_delta": "-9.75" | null,
          "fills": [ {"ts": "...", "side": "buy"|"sell", "price": "10000", "quantity": 139, "fee": "195"} ],
          "exits": [ {"ts": "...", "price": "10500", "quantity": 139, "fee": "3108", "reason": "trailing"} ],
          "net_pnl": "66197" | null,              # 수수료 포함 순손익 (open 이면 null)
          "actual_stop_pct": "5.0" | null,        # 실제 적용된 초기 SL
          "lots_ambiguous": false                 # 추가 매수로 lot 구분 불가 → 표본 제외
        }
      ]
    }
계약 메모: ``fills`` 는 매수·매도 체결 전부(순손익 재계산에 사용), ``exits`` 는 매도 체결의
사유 부착 뷰(벤치마크 청산 가중에 사용). ``fills`` 에 sell 이 없으면 ``exits`` 를 매도로 간주한다.

벤치마크 CSV: ``date,close`` (KODEX200 등). 포지션 동일기간 벤치마크는 각 부분청산 시점의
B(entry, t_i) 를 청산 수량 비중으로 가중한다. 파일이 없거나 날짜가 없으면 초과수익은 null 이다
(0 으로 채우지 않는다).

R 정의: 완결 포지션 net_pnl ÷ initial_risk_amount. 리포트의 숫자 지표는 전부 문자열 Decimal.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from statistics import median
from typing import Any, Dict, List, Optional, Tuple

POSITION_REQUIRED = (
    "position_id", "symbol", "strategy", "cohort_id", "applied_sha", "status",
    "entry_risk", "initial_risk_amount", "planned_vs_filled_risk_delta",
    "fills", "exits", "net_pnl", "actual_stop_pct", "lots_ambiguous",
)
ENTRY_RISK_REQUIRED = (
    "version", "cohort_id", "sizing_mode", "strategy", "stop_basis", "stop_pct",
    "stop_source", "equity_at_decision", "risk_budget_amount", "planned_price",
    "planned_quantity", "planned_risk_amount",
)
EXCLUSION_KEYS = ("open", "legacy_unmeasured", "missing_initial_risk", "lots_ambiguous", "cohort_mismatch")
ONE_WON = Decimal("1")
RATIO_Q = Decimal("0.000001")
MONEY_Q = Decimal("0.01")


class LedgerError(Exception):
    """입력 구조 오류 — 부족 항목 목록을 담는다 (exit 2)."""

    def __init__(self, items: List[str]):
        super().__init__("; ".join(items))
        self.items = items


# ── 로딩·구조 검증 ──────────────────────────────────────────────────────────────

def _dec(value: Any) -> Optional[Decimal]:
    """문자열/숫자 → Decimal(str(x)). None·빈값·파싱 실패는 None."""
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _strip(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _date(ts: Any) -> Optional[str]:
    """ISO8601 문자열 → 'YYYY-MM-DD' (앞 10자). 형식이 아니면 None."""
    if not isinstance(ts, str) or len(ts) < 10:
        return None
    try:
        datetime.fromisoformat(ts[:10])
    except ValueError:
        return None
    return ts[:10]


def load_ledger(path: Path) -> Dict[str, Any]:
    """원장 JSON 을 읽고 구조를 검증한다. 문제는 LedgerError 로 모아 던진다."""
    if not path.is_file():
        raise LedgerError([f"입력 원장 파일 없음: {path}"])
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise LedgerError([f"입력 원장 JSON 파싱 실패: {path}: {e}"]) from e
    problems: List[str] = []
    if not isinstance(data, dict):
        raise LedgerError(["입력 원장 최상위가 객체가 아님"])
    for key in ("version", "positions"):
        if key not in data:
            problems.append(f"최상위 필수 필드 누락: {key}")
    positions = data.get("positions")
    if not isinstance(positions, list):
        problems.append("positions 가 배열이 아님")
        raise LedgerError(problems)
    for i, pos in enumerate(positions):
        if not isinstance(pos, dict):
            problems.append(f"positions[{i}] 가 객체가 아님")
            continue
        for key in POSITION_REQUIRED:
            if key not in pos:
                problems.append(f"positions[{i}].{key} 누락")
        if pos.get("status") not in ("closed", "open"):
            problems.append(f"positions[{i}].status 값 오류: {pos.get('status')!r}")
        elif pos.get("status") == "closed" and _dec(pos.get("net_pnl")) is None:
            problems.append(f"positions[{i}].net_pnl 누락 (closed 포지션은 문자열 Decimal 필수)")
        for key in ("fills", "exits"):
            if key in pos and not isinstance(pos[key], list):
                problems.append(f"positions[{i}].{key} 가 배열이 아님")
    if problems:
        raise LedgerError(problems)
    return data


def load_benchmark(path: Optional[Path]) -> Tuple[Optional[Dict[str, Decimal]], Optional[str]]:
    """벤치마크 CSV(date,close) → {date: close}. 없으면 (None, 사유)."""
    if path is None:
        return None, "benchmark_missing"
    if not path.is_file():
        return None, f"benchmark_missing: {path}"
    closes: Dict[str, Decimal] = {}
    with path.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            d = _date(_strip(row.get("date")))
            c = _dec(_strip(row.get("close")))
            if d is not None and c is not None and c > 0:
                closes[d] = c
    if not closes:
        return None, f"benchmark_missing: 유효한 date,close 행 없음 ({path})"
    return closes, None


# ── 포지션 단위 계산 ────────────────────────────────────────────────────────────

def _buys(pos: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [f for f in pos["fills"] if isinstance(f, dict) and f.get("side") == "buy"]


def _sells(pos: Dict[str, Any]) -> List[Dict[str, Any]]:
    sells = [f for f in pos["fills"] if isinstance(f, dict) and f.get("side") == "sell"]
    return sells if sells else [e for e in pos["exits"] if isinstance(e, dict)]


def _notional(rows: List[Dict[str, Any]]) -> Optional[Decimal]:
    """Σ price×quantity. 하나라도 파싱 불가면 None."""
    total = Decimal("0")
    for r in rows:
        p, q = _dec(r.get("price")), _dec(r.get("quantity"))
        if p is None or q is None:
            return None
        total += p * q
    return total


def _fees(rows: List[Dict[str, Any]]) -> Optional[Decimal]:
    total = Decimal("0")
    for r in rows:
        fee = _dec(r.get("fee"))
        if fee is None:
            return None
        total += fee
    return total


def entry_cost(pos: Dict[str, Any]) -> Optional[Decimal]:
    buys = _buys(pos)
    return _notional(buys) if buys else None


def recompute_net_pnl(pos: Dict[str, Any]) -> Optional[Decimal]:
    """fills 기준 수수료 포함 순손익 = Σ매도 − Σ매수 − Σ수수료. 재계산 불가면 None."""
    buys, sells = _buys(pos), _sells(pos)
    if not buys or not sells:
        return None
    parts = (_notional(sells), _notional(buys), _fees(buys), _fees(sells))
    if any(p is None for p in parts):
        return None
    return parts[0] - parts[1] - parts[2] - parts[3]


def position_benchmark(bench: Optional[Dict[str, Decimal]], pos: Dict[str, Any]) -> Tuple[Optional[Decimal], Optional[str]]:
    """청산 수량 가중 동일기간 벤치마크 수익률 Σ w_i × (close[t_i]/close[entry] − 1)."""
    if bench is None:
        return None, "benchmark_missing"
    buys = _buys(pos)
    entry_d = _date(buys[0].get("ts")) if buys else None
    if entry_d is None or entry_d not in bench:
        return None, f"benchmark_date_missing: entry {entry_d}"
    exits = [e for e in pos["exits"] if isinstance(e, dict)]
    qtys = [_dec(e.get("quantity")) for e in exits]
    if not exits or any(q is None for q in qtys):
        return None, "exits_missing"
    total_q = sum(qtys, Decimal("0"))
    if total_q <= 0:
        return None, "exits_missing"
    acc = Decimal("0")
    for e in exits:
        d, q = _date(e.get("ts")), _dec(e.get("quantity"))
        if d is None or d not in bench or q is None:
            return None, f"benchmark_date_missing: exit {d}"
        acc += (q / total_q) * (bench[d] / bench[entry_d] - 1)
    return acc, None


def _last_exit_date(pos: Dict[str, Any]) -> Optional[str]:
    dates = [_date(e.get("ts")) for e in pos["exits"] if isinstance(e, dict)]
    dates = [d for d in dates if d is not None]
    return max(dates) if dates else None


def _holding_days(pos: Dict[str, Any]) -> Optional[int]:
    buys = _buys(pos)
    start = _date(buys[0].get("ts")) if buys else None
    end = _last_exit_date(pos)
    if start is None or end is None:
        return None
    return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).days


# ── 표본 분류·기술 검증 ─────────────────────────────────────────────────────────

def classify(pos: Dict[str, Any], cohort: str, sha: Optional[str], as_of: Optional[str]) -> Optional[str]:
    """제외 사유를 돌려준다. None 이면 완결 판정 표본."""
    if pos["cohort_id"] != cohort or (sha is not None and pos["applied_sha"] != sha):
        return "cohort_mismatch"
    er = pos["entry_risk"]
    if not isinstance(er, dict) or er.get("sizing_mode") != "risk":
        return "legacy_unmeasured"
    if pos["status"] == "open":
        return "open"
    last_exit = _last_exit_date(pos)
    if as_of is not None and (last_exit is None or last_exit > as_of):
        return "open"  # as-of 시점에는 아직 미완결
    if pos["lots_ambiguous"] is True:
        return "lots_ambiguous"
    ir = _dec(pos["initial_risk_amount"])
    if ir is None or ir <= 0:
        return "missing_initial_risk"
    return None


def technical_issues(positions: List[Dict[str, Any]], in_cohort: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """첫 5·10 건 기술 검증 항목 (계획서 T8 표). 한 건이라도 있으면 technical failed."""
    issues: List[Dict[str, str]] = []

    def add(pos: Dict[str, Any], kind: str, detail: str) -> None:
        issues.append({"position_id": str(pos.get("position_id")), "issue": kind, "detail": detail})

    for pid, n in Counter(str(p["position_id"]) for p in positions).items():
        if n > 1:
            issues.append({"position_id": pid, "issue": "duplicate_position_id", "detail": f"{n}건 중복"})

    for pos in in_cohort:
        er = pos["entry_risk"]
        missing = [k for k in ENTRY_RISK_REQUIRED if k not in er]
        if pos["status"] == "closed":
            if pos.get("actual_stop_pct") is None:
                missing.append("actual_stop_pct")
            if not pos["exits"]:
                missing.append("exits")
        if missing:
            add(pos, "missing_field", ", ".join(missing))
            continue
        planned, budget = _dec(er["planned_risk_amount"]), _dec(er["risk_budget_amount"])
        if planned is not None and budget is not None and planned > budget:
            add(pos, "planned_risk_exceeds_budget", f"planned {planned} > budget {budget}")
        stop_pct = _dec(er["stop_pct"])
        actual = _dec(pos.get("actual_stop_pct"))
        if stop_pct is not None and actual is not None and stop_pct != actual:
            add(pos, "stop_pct_mismatch", f"planned stop {stop_pct}% ≠ actual {actual}%")
        ir = _dec(pos["initial_risk_amount"])
        cost = entry_cost(pos)
        basis = actual if actual is not None else stop_pct  # 원장 정의(T3): 실제 초기 SL 로 확정
        if ir is not None and cost is not None and basis is not None and pos["lots_ambiguous"] is not True:
            recalc = cost * basis / Decimal("100")
            if abs(recalc - ir) > ONE_WON:
                add(pos, "initial_risk_mismatch", f"fills 재계산 {recalc:.2f} vs 원장 {ir}")
        if pos["status"] == "closed":
            recomputed = recompute_net_pnl(pos)
            net = _dec(pos["net_pnl"])
            if recomputed is None:
                add(pos, "net_pnl_unverifiable", "fills 에서 수수료 포함 순손익을 재계산할 수 없음")
            elif net is not None and abs(recomputed - net) > ONE_WON:
                add(pos, "net_pnl_mismatch", f"fills 재계산 {recomputed} vs 원장 {net}")
    return issues


# ── 성과 지표 ───────────────────────────────────────────────────────────────────

def _q(value: Optional[Decimal], quant: Decimal) -> Optional[Decimal]:
    return None if value is None else value.quantize(quant)


def _order_key(pos: Dict[str, Any]) -> Tuple[str, str]:
    last = _last_exit_date(pos)
    return (last if last is not None else "", str(pos["position_id"]))


def _total_fees(pos: Dict[str, Any]) -> Decimal:
    buy_fee, sell_fee = _fees(_buys(pos)), _fees(_sells(pos))
    return (buy_fee if buy_fee is not None else Decimal("0")) + (sell_fee if sell_fee is not None else Decimal("0"))


def _max_consecutive_losses(pnls: List[Decimal]) -> int:
    worst = run = 0
    for p in pnls:
        run = run + 1 if p < 0 else 0
        worst = max(worst, run)
    return worst


def compute_metrics(sample: List[Dict[str, Any]], bench: Optional[Dict[str, Decimal]]) -> Tuple[Dict[str, Any], List[str]]:
    """완결 표본 지표. 벤치마크 없으면 avg_excess_return 은 None."""
    notes: List[str] = []
    n = len(sample)
    if n == 0:
        return {"n": 0}, notes
    ordered = sorted(sample, key=_order_key)
    pnls = [_dec(p["net_pnl"]) for p in ordered]
    rs = [pnl / _dec(p["initial_risk_amount"]) for p, pnl in zip(ordered, pnls)]
    gross_profit = sum((p for p in pnls if p > 0), Decimal("0"))
    gross_loss = -sum((p for p in pnls if p < 0), Decimal("0"))
    pf = gross_profit / gross_loss if gross_loss > 0 else None
    if pf is None:
        notes.append("profit_factor: 손실 포지션 0건이라 미정의")
    total = sum(pnls, Decimal("0"))
    top3 = sum(sorted(pnls, reverse=True)[:3], Decimal("0"))
    fees = [_total_fees(p) for p in ordered]
    hold = [d for d in (_holding_days(p) for p in ordered) if d is not None]
    returns = []
    for p, pnl in zip(ordered, pnls):
        cost = entry_cost(p)
        if cost is not None and cost > 0:
            returns.append(pnl / cost)

    excess: List[Decimal] = []
    bench_reasons: Counter = Counter()
    for p, pnl in zip(ordered, pnls):
        b, reason = position_benchmark(bench, p)
        cost = entry_cost(p)
        if b is None or cost is None or cost <= 0:
            bench_reasons[reason if reason is not None else "entry_cost_missing"] += 1
            continue
        excess.append(pnl / cost - b)
    if bench is not None and bench_reasons:
        notes.append("benchmark 미산출 포지션: " + ", ".join(f"{k} {v}건" for k, v in sorted(bench_reasons.items())))

    deltas = []
    for p in ordered:
        planned = _dec(p["entry_risk"].get("planned_risk_amount"))
        delta = _dec(p.get("planned_vs_filled_risk_delta"))
        if delta is None and planned is not None:
            delta = _dec(p["initial_risk_amount"]) - planned
        if delta is not None and planned is not None and planned > 0:
            deltas.append(delta / planned)

    metrics = {
        "n": n,
        "avg_r": _q(sum(rs, Decimal("0")) / n, RATIO_Q),
        "median_r": _q(median(rs), RATIO_Q),
        "profit_factor": _q(pf, RATIO_Q),
        "total_net_pnl": _q(total, MONEY_Q),
        "win_rate": _q(Decimal(sum(1 for p in pnls if p > 0)) / n, RATIO_Q),
        "max_consecutive_losses": _max_consecutive_losses(pnls),
        "avg_holding_days": _q(Decimal(sum(hold)) / len(hold), RATIO_Q) if hold else None,
        "total_fees": _q(sum(fees, Decimal("0")), MONEY_Q),
        "net_pnl_excl_top3": _q(total - top3, MONEY_Q),
        "avg_return": _q(sum(returns, Decimal("0")) / len(returns), RATIO_Q) if returns else None,
        "avg_excess_return": _q(sum(excess, Decimal("0")) / len(excess), RATIO_Q) if excess else None,
        "benchmark_covered": len(excess),
        "initial_risk_deviation": {
            "avg_delta_pct": _q(sum(deltas, Decimal("0")) / len(deltas) * 100, RATIO_Q) if deltas else None,
            "max_abs_delta_pct": _q(max(abs(d) for d in deltas) * 100, RATIO_Q) if deltas else None,
            "covered": len(deltas),
        },
    }
    return metrics, notes


def decide_status(metrics: Dict[str, Any], min_sample: int) -> str:
    """보수적 확대 검토 규칙 (통계적 유의성 판정 아님). 절대 자동 승격·모드 복귀를 말하지 않는다."""
    if metrics["n"] < min_sample:
        return "insufficient_sample"
    pf = metrics["profit_factor"]
    excess = metrics["avg_excess_return"]
    if metrics["avg_r"] <= 0 or (pf is not None and pf <= 1) or (excess is not None and excess <= 0):
        return "hold_expansion"
    return "further_review"


# ── 리포트 조립 ─────────────────────────────────────────────────────────────────

def build_report(ledger: Dict[str, Any], bench: Optional[Dict[str, Decimal]], bench_reason: Optional[str],
                 bench_source: Optional[str], cohort: str, sha: Optional[str], min_sample: int,
                 as_of: Optional[str]) -> Dict[str, Any]:
    positions = ledger["positions"]
    excluded: Counter = Counter({k: 0 for k in EXCLUSION_KEYS})
    sample: List[Dict[str, Any]] = []
    in_cohort: List[Dict[str, Any]] = []
    for pos in positions:
        reason = classify(pos, cohort, sha, as_of)
        if reason not in ("cohort_mismatch", "legacy_unmeasured"):
            in_cohort.append(pos)
        if reason is None:
            sample.append(pos)
        else:
            excluded[reason] += 1

    issues = technical_issues(positions, in_cohort)
    metrics, notes = compute_metrics(sample, bench)
    status = decide_status(metrics, min_sample)
    if bench is None:
        notes.append("초과수익 미측정 (벤치마크 없음) — 0 으로 대체하지 않음")
    if status == "insufficient_sample":
        notes.append(f"표본 부족: 완결 {metrics['n']}건 < {min_sample}건 — 판정 보류. "
                     "표본 확보 목적의 거래 증가·사이징 모드 자동 전환 없음")
    elif status == "hold_expansion":
        notes.append("확대 보류: 평균 R≤0 / PF≤1 / 평균 초과수익≤0 중 하나 이상")
    else:
        notes.append("추가 검토 대상: 자동 승격 아님 — 추가 기간·표본으로 재검토")
    if issues:
        notes.append(f"기술 검증 실패 {len(issues)}건 — 성과 status 와 별개로 원장·계측 확인 필요")
    return {
        "status": status,
        "technical_status": "failed" if issues else "passed",
        "cohort": cohort,
        "applied_sha": sha,
        "as_of": as_of if as_of is not None else ledger.get("generated_at"),
        "min_sample": min_sample,
        "sample": {"closed": len(sample), "excluded": dict(excluded)},
        "technical_issues": issues,
        "metrics": metrics,
        "benchmark": {"source": bench_source, "missing_reason": bench_reason},
        "notes": notes,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="위험 기반 사이징 canary 오프라인 리포트 (주문·설정 변경 없음)")
    parser.add_argument("--input", required=True, help="검증용 포지션 원장 JSON")
    parser.add_argument("--benchmark", default=None, help="고정 벤치마크 CSV (date,close)")
    parser.add_argument("--cohort", required=True, help="표본 고정 cohort_id (예: risk-sepa-v1)")
    parser.add_argument("--output", required=True, help="리포트 JSON 경로")
    parser.add_argument("--sha", default=None, help="적용 SHA — 주면 applied_sha 불일치 포지션 제외")
    parser.add_argument("--min-sample", type=int, default=30, help="판정 최소 완결 표본 (기본 30)")
    parser.add_argument("--as-of", default=None, help="YYYY-MM-DD — 이 날짜 이후 완결은 미완결로 취급")
    args = parser.parse_args(argv)

    if args.as_of is not None and _date(args.as_of) is None:
        print(f"[canary] --as-of 형식 오류 (YYYY-MM-DD): {args.as_of}", file=sys.stderr)
        return 2
    try:
        ledger = load_ledger(Path(args.input))
    except LedgerError as e:
        print("[canary] 입력 원장 오류 — 부족 항목:", file=sys.stderr)
        for item in e.items:
            print(f"  - {item}", file=sys.stderr)
        return 2

    bench_path = Path(args.benchmark) if args.benchmark is not None else None
    bench, bench_reason = load_benchmark(bench_path)
    report = build_report(ledger, bench, bench_reason, args.benchmark, args.cohort, args.sha,
                          args.min_sample, args.as_of)
    try:
        Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n",
                                     encoding="utf-8")
    except OSError as e:
        print(f"[canary] 리포트 쓰기 실패: {args.output}: {e}", file=sys.stderr)
        return 2
    print(f"[canary] status={report['status']} technical={report['technical_status']} "
          f"closed={report['sample']['closed']} excluded={report['sample']['excluded']} → {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
