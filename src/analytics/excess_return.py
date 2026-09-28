"""실거래 KODEX200 초과수익 원장 (설계 A, 2026-09-29)

설계: docs/superpowers/specs/2026-09-28-kodex200-excess-return-ledger-design.md
계획: docs/superpowers/plans/2026-09-29-kodex200-excess-return-ledger.md (1단계 T1)

봇이 연 각 왕복 포지션을 같은 기간 KODEX200(069500) 과 비교해 비용 차감 후 초과수익을 잰다.
**측정만 한다** — 판정·승격·설정 변경·사이징 연결 없음. `status` 는 insufficient_sample/measured 뿐이다.

- 포지션 원천: DB → `scripts/export_risk_ledger.py` 의 `fetch_trade_records` → `build_ledger` (파일 경로로 로드)
- 벤치마크: 봇 브로커 `get_daily_prices("069500")` → CSV 캐시(date,close,source,fetched_at) 통째 원자 교체
- 20:30 에 매일 전체 재계산 → positions.jsonl / summary.json(원자 교체) + summary_history.jsonl(하루 한 줄)

경로는 전부 인자로 받는다(모듈 상수 없음 — 시험 격리). 금액·비율은 Decimal(str(x)).
`load_benchmark`·`position_benchmark` 는 `scripts/review_risk_canary.py` 에서 옮겨 왔고 canary 가 이것을 import 한다
(사유 문자열 `benchmark_missing`·`benchmark_date_missing: …`·`exits_missing` 유지).
"""

from __future__ import annotations

import asyncio
import csv
import importlib.util
import json
import os
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from loguru import logger

from src.analytics.gate_performance import BENCH_SYMBOL, STOP_CLIP_PCT
from src.utils.atomic_io import atomic_write_json, atomic_write_text

SCHEMA = 1
MIN_SAMPLE = 30
RECENT_DAYS = 90
LEDGER_DAYS = 730                 # DB 조회 기간(달력일)
MAX_BENCH_DAYS = 500              # 브로커 5페이지 상한
BENCH_SOURCE = f"KIS:FHKST03010100:{BENCH_SYMBOL}:adj1"
SYNC_EXIT_TYPES = ("kis_sync", "sync_reconcile", "sync_closed", "sync_partial", "sync_detected")
EXCLUSION_REASONS = (
    "exits_missing", "quantity_mismatch", "exits_aggregated", "lots_ambiguous", "manual_entry",
    "sync_entry", "recovered_at_exit", "bench_out_of_range", "record_incomplete",
)

BENCH_FILE = "kodex200_daily.csv"
SNAPSHOT_FILE = "positions.jsonl"
PREV_SNAPSHOT_FILE = "positions_prev.jsonl"
SNAPSHOT_META_FILE = "positions_meta.json"
SUMMARY_FILE = "summary.json"
HISTORY_FILE = "summary_history.jsonl"

RATIO_Q = Decimal("0.000001")
MONEY_Q = Decimal("1")


# ── 공용 파싱 (canary 의 _dec·_date·_buys 와 같은 동작) ─────────────────────────

def to_decimal(value: Any) -> Optional[Decimal]:
    """문자열/숫자 → Decimal(str(x)). None·빈값·파싱 실패는 None."""
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _strip(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def parse_date(ts: Any) -> Optional[str]:
    """ISO8601 문자열 → 'YYYY-MM-DD' (앞 10자). 형식이 아니면 None."""
    if not isinstance(ts, str) or len(ts) < 10:
        return None
    try:
        datetime.fromisoformat(ts[:10])
    except ValueError:
        return None
    return ts[:10]


def buy_fills(pos: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [f for f in pos["fills"] if isinstance(f, dict) and f.get("side") == "buy"]


# ── 벤치마크 (canary 에서 이동 — 동작 동일) ───────────────────────────────────

def load_benchmark(path: Optional[Path]) -> Tuple[Optional[Dict[str, Decimal]], Optional[str]]:
    """벤치마크 CSV(date,close) → {date: close}. 없으면 (None, 사유)."""
    if path is None:
        return None, "benchmark_missing"
    if not path.is_file():
        return None, f"benchmark_missing: {path}"
    closes: Dict[str, Decimal] = {}
    with path.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            d = parse_date(_strip(row.get("date")))
            c = to_decimal(_strip(row.get("close")))
            if d is not None and c is not None and c > 0:
                closes[d] = c
    if not closes:
        return None, f"benchmark_missing: 유효한 date,close 행 없음 ({path})"
    return closes, None


def position_benchmark(bench: Optional[Dict[str, Decimal]], pos: Dict[str, Any]) -> Tuple[Optional[Decimal], Optional[str]]:
    """청산 수량 가중 동일기간 벤치마크 수익률 Σ w_i × (close[t_i]/close[entry] − 1)."""
    if bench is None:
        return None, "benchmark_missing"
    buys = buy_fills(pos)
    entry_d = parse_date(buys[0].get("ts")) if buys else None
    if entry_d is None or entry_d not in bench:
        return None, f"benchmark_date_missing: entry {entry_d}"
    exits = [e for e in pos["exits"] if isinstance(e, dict)]
    qtys = [to_decimal(e.get("quantity")) for e in exits]
    if not exits or any(q is None for q in qtys):
        return None, "exits_missing"
    total_q = sum(qtys, Decimal("0"))
    if total_q <= 0:
        return None, "exits_missing"
    acc = Decimal("0")
    for e in exits:
        d, q = parse_date(e.get("ts")), to_decimal(e.get("quantity"))
        if d is None or d not in bench or q is None:
            return None, f"benchmark_date_missing: exit {d}"
        acc += (q / total_q) * (bench[d] / bench[entry_d] - 1)
    return acc, None


def kis_rows_to_bench(rows: Iterable[Dict[str, Any]]) -> List[Tuple[str, Decimal]]:
    """브로커 일봉 {"date":"YYYYMMDD","close":float} → [(YYYY-MM-DD, Decimal)] 오래된 순. 8자리·종가>0 만."""
    out: Dict[str, Decimal] = {}
    for r in rows or []:
        d = str(r.get("date", ""))
        c = to_decimal(r.get("close"))
        if len(d) != 8 or not d.isdigit() or c is None or not c.is_finite() or c <= 0:
            continue
        out[f"{d[:4]}-{d[4:6]}-{d[6:]}"] = c
    return sorted(out.items())


# ── 동기화 판정 (대시보드와 공용 — TradeRecord.is_sync 는 별개로 유지) ──────────

def is_sync_entry(trade_id: Any, entry_reason: Any) -> bool:
    """동기화가 만든 진입. KR 실효는 `KIS_SYNC_` 접두, `SYNC_`·`sync_detected` 진입은 US 경로."""
    return (isinstance(trade_id, str) and trade_id.startswith(("KIS_SYNC_", "SYNC_"))) \
        or entry_reason == "sync_detected"


def is_sync_exit(exit_type: Any) -> bool:
    """동기화 청산 유형(추정 가격). `sync_detected` 는 KR 의 청산 유형이다."""
    return exit_type in SYNC_EXIT_TYPES


# ── 포지션 분류·행 ────────────────────────────────────────────────────────────

def _exits(pos: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [e for e in pos.get("exits") or [] if isinstance(e, dict)]


def _qty_sum(rows: List[Dict[str, Any]]) -> Optional[Decimal]:
    total = Decimal("0")
    for r in rows:
        q = to_decimal(r.get("quantity"))
        if q is None:
            return None
        total += q
    return total


def _span(pos: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    buys = buy_fills(pos)
    entry = parse_date(buys[0].get("ts")) if buys else None
    dates = [d for d in (parse_date(e.get("ts")) for e in _exits(pos)) if d is not None]
    return entry, (max(dates) if dates else None)


def _norm_status(day_status: Optional[Dict[Any, str]]) -> Dict[str, str]:
    return {str(k): v for k, v in (day_status or {}).items()}


def classify(pos: Dict[str, Any], bench: Optional[Dict[str, Decimal]],
             day_status: Optional[Dict[Any, str]]) -> Optional[str]:
    """설계 §5 판정 순서 ①~⑩. None = 지표 포함. 'open'·'awaiting_close' 는 행을 쓰지 않는다."""
    if pos.get("status") != "closed":
        return "open"
    exits = _exits(pos)
    if not exits or pos.get("net_pnl") is None or pos.get("pnl_missing") is True:
        return "exits_missing"                                            # ①
    sold, bought = _qty_sum(exits), _qty_sum(buy_fills(pos))
    if sold is None or bought is None:
        return "exits_missing"
    if sold < bought:
        return "awaiting_close"                                           # ②
    if sold > bought:
        return "quantity_mismatch"                                        # ③
    if pos.get("exits_aggregated") is True:
        return "exits_aggregated"                                         # ④ (lots_ambiguous 도 켜져 있다)
    if pos.get("lots_ambiguous") is True:
        return "lots_ambiguous"                                           # ⑤
    if pos.get("strategy") == "manual":
        return "manual_entry"                                             # ⑥
    if is_sync_entry(pos.get("position_id"), pos.get("entry_reason")):
        return "sync_entry"                                               # ⑦
    if pos.get("entry_reason") == "recovered_at_exit":
        return "recovered_at_exit"                                        # ⑧
    entry, last_exit = _span(pos)
    if bench and entry is not None and entry < min(bench):
        return "bench_out_of_range"                                       # ⑨
    if entry is not None and last_exit is not None:
        for d, s in _norm_status(day_status).items():
            if s == "incomplete" and entry <= d <= last_exit:
                return "record_incomplete"                                # ⑩
    return None


def has_day_status_gap(pos: Dict[str, Any], bench: Optional[Dict[str, Decimal]],
                       day_status: Optional[Dict[Any, str]]) -> bool:
    """보유 구간의 거래일 중 상태 행이 없는 날이 있는가 (요약 `day_status_missing`).

    거래일 = 벤치마크 캐시 날짜. 캐시가 없으면 진입일·청산일만 본다.
    """
    entry, last_exit = _span(pos)
    if entry is None or last_exit is None:
        return False
    if bench:
        days = {d for d in bench if entry <= d <= last_exit}
    else:
        days = {entry} | {d for d in (parse_date(e.get("ts")) for e in _exits(pos)) if d is not None}
    known = _norm_status(day_status)
    return any(d not in known for d in days)


def _q(value: Optional[Decimal], quant: Decimal) -> Optional[str]:
    return None if value is None else str(value.quantize(quant, rounding=ROUND_HALF_UP))


def _fees_total(pos: Dict[str, Any]) -> Optional[Decimal]:
    total = Decimal("0")
    for f in pos.get("fills") or []:
        fee = to_decimal(f.get("fee")) if isinstance(f, dict) else None
        if fee is None:
            return None
        total += fee
    return total


def _clip(pos: Dict[str, Any]) -> Tuple[Decimal, str]:
    """clip_pct(양수 %) 와 기준. 실제 초기 SL → 진입 스냅샷 SL → 공통 5%(비교용)."""
    er = pos.get("entry_risk") if isinstance(pos.get("entry_risk"), dict) else {}
    for cand in (pos.get("actual_stop_pct"), er.get("stop_pct")):
        s = to_decimal(cand)
        if s is not None and s > 0:
            return s, "entry_stop"
    return Decimal(str(-STOP_CLIP_PCT)), "common_5"


def position_row(pos: Dict[str, Any], bench: Optional[Dict[str, Decimal]],
                 day_status: Optional[Dict[Any, str]], *, computed_at: str,
                 code_sha: str) -> Optional[Dict[str, Any]]:
    """설계 §5 schema 1 행. 미종결·청산 진행 중이면 None. 결측은 null (0 으로 채우지 않는다)."""
    exclusion = classify(pos, bench, day_status)
    if exclusion in ("open", "awaiting_close"):
        return None
    buys = buy_fills(pos)
    cost = None
    if buys:
        cost = Decimal("0")
        for b in buys:
            p, q = to_decimal(b.get("price")), to_decimal(b.get("quantity"))
            if p is None or q is None:
                cost = None
                break
            cost += p * q
    net = None if pos.get("pnl_missing") is True else to_decimal(pos.get("net_pnl"))
    r = net / cost if net is not None and cost is not None and cost > 0 else None
    b, bench_reason = position_benchmark(bench, pos)
    x = r - b if r is not None and b is not None else None
    x_krw = net - cost * b if net is not None and cost is not None and b is not None else None
    s, basis = _clip(pos)
    rc = max(r, -s / 100) if r is not None else None
    xc = rc - b if rc is not None and b is not None else None
    overshoot = rc - r if basis == "entry_stop" and rc is not None else None
    entry, last_exit = _span(pos)
    holding = ((date.fromisoformat(last_exit) - date.fromisoformat(entry)).days
               if entry is not None and last_exit is not None else None)
    exit_types: List[str] = []
    for e in _exits(pos):
        reason = e.get("reason") or ""
        if reason not in exit_types:
            exit_types.append(reason)
    sync_exit = any(is_sync_exit(t) for t in exit_types) or is_sync_exit(pos.get("exit_type"))
    return {
        "schema": SCHEMA,
        "position_id": pos.get("position_id"), "symbol": pos.get("symbol"),
        "strategy": pos.get("strategy"), "cohort_id": pos.get("cohort_id"),
        "applied_sha": pos.get("applied_sha"),
        "entry_date": entry, "last_exit_date": last_exit, "holding_days": holding,
        "entry_cost": _q(cost, MONEY_Q), "net_pnl": _q(net, MONEY_Q),
        "fees_total_est": _q(_fees_total(pos), MONEY_Q),
        "net_return": _q(r, RATIO_Q), "bench_return": _q(b, RATIO_Q),
        "excess_return": _q(x, RATIO_Q), "excess_krw": _q(x_krw, MONEY_Q),
        "clip_pct": str(s), "clip_basis": basis,
        "clipped_return": _q(rc, RATIO_Q), "clipped_excess_return": _q(xc, RATIO_Q),
        "stop_overshoot": _q(overshoot, RATIO_Q),
        "exit_types": exit_types,
        "entry_quality": "sync_estimated" if is_sync_entry(pos.get("position_id"), pos.get("entry_reason")) else "fill",
        "exit_quality": "sync_estimated" if sync_exit else "fill",
        "exclusion": exclusion, "bench_missing_reason": bench_reason,
        "bench_source": BENCH_SOURCE if bench is not None else None,
        "computed_at": computed_at, "code_sha": code_sha,
    }


# ── 요약 ─────────────────────────────────────────────────────────────────────

def _d(v: Any) -> Optional[Decimal]:
    return to_decimal(v)


def _mean(xs: List[Decimal]) -> Optional[Decimal]:
    return sum(xs, Decimal("0")) / len(xs) if xs else None


def _metrics(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    included = [r for r in rows if r.get("exclusion") is None]
    excluded: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        reason = r.get("exclusion")
        if reason is None:
            continue
        agg = excluded.setdefault(reason, {"n": 0, "net_pnl_sum": None, "net_pnl_missing": 0})
        agg["n"] += 1
        net = _d(r.get("net_pnl"))
        if net is None:
            agg["net_pnl_missing"] += 1          # 원천 손익 결측 — 0원으로 세지 않는다
        else:
            agg["net_pnl_sum"] = net if agg["net_pnl_sum"] is None else agg["net_pnl_sum"] + net
    for agg in excluded.values():
        agg["net_pnl_sum"] = _q(agg["net_pnl_sum"], MONEY_Q)   # 전부 결측이면 null

    covered = [r for r in included if _d(r.get("excess_return")) is not None]
    xs = [_d(r["excess_return"]) for r in covered]
    krws = [_d(r["excess_krw"]) for r in covered if _d(r.get("excess_krw")) is not None]
    krw_sum = sum(krws, Decimal("0")) if krws else None
    krw_excl = (krw_sum - sum(sorted(krws, reverse=True)[:3], Decimal("0"))) if krws else None
    t = None
    if len(xs) >= 2:
        sd = statistics.stdev(xs)
        if sd > 0:
            t = _mean(xs) / (sd / Decimal(len(xs)).sqrt())
    nets = [_d(r["net_return"]) for r in included if _d(r.get("net_return")) is not None]
    benches = [_d(r["bench_return"]) for r in covered]
    clipped = [_d(r["clipped_excess_return"]) for r in covered if _d(r.get("clipped_excess_return")) is not None]
    over = []
    for r in included:
        o, cost = _d(r.get("stop_overshoot")), _d(r.get("entry_cost"))
        if r.get("clip_basis") == "entry_stop" and o is not None and o > 0 and cost is not None:
            over.append(o * cost)
    fees = [_d(r["fees_total_est"]) for r in included if _d(r.get("fees_total_est")) is not None]
    return {
        "n": len(included),
        "bench_covered": len(covered),
        "excluded": excluded,
        "mean_excess": _q(_mean(xs), RATIO_Q),
        "median_excess": _q(statistics.median(xs), RATIO_Q) if xs else None,
        "excess_krw_sum": _q(krw_sum, MONEY_Q),
        "excess_krw_excl_top3": _q(krw_excl, MONEY_Q),
        "t_excess": _q(t, Decimal("0.0001")),
        "beat_rate": _q(Decimal(sum(1 for x in xs if x > 0)) / len(xs), RATIO_Q) if xs else None,
        "mean_net_return": _q(_mean(nets), RATIO_Q),
        "mean_bench_return": _q(_mean(benches), RATIO_Q),
        "mean_clipped_excess": _q(_mean(clipped), RATIO_Q),
        "overshoot_n": len(over),
        "overshoot_krw_sum": _q(sum(over, Decimal("0")), MONEY_Q),
        "fees_total_est": _q(sum(fees, Decimal("0")), MONEY_Q),
        # 판정이 아니다 — 지표가 실제로 쓰는 표본(벤치마크 산출분)이 30 미만이면 보류
        "status": "measured" if len(covered) >= MIN_SAMPLE else "insufficient_sample",
    }


def _groups(rows: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        month = (r.get("last_exit_date") or "unknown")[:7]
        for key in ("all", f"strategy:{r.get('strategy')}", f"month:{month}",
                    f"exit_quality:{r.get('exit_quality')}", f"cohort:{r.get('cohort_id')}"):
            out[key].append(r)
    out.setdefault("all", [])
    return out


def _drift(rows: List[Dict[str, Any]], previous: List[Dict[str, Any]]) -> int:
    """직전 계산일 스냅샷 대비 net_pnl·bench_return 이 바뀌었거나 사라진 포지션 수."""
    now = {r.get("position_id"): r for r in rows}
    changed = 0
    for p in previous:
        cur = now.get(p.get("position_id"))
        if cur is None or (cur.get("net_pnl"), cur.get("bench_return")) != (p.get("net_pnl"), p.get("bench_return")):
            changed += 1
    return changed


def summarize(rows: List[Dict[str, Any]], previous_rows: List[Dict[str, Any]], *, today: date,
              awaiting_close: int, day_status_missing: int) -> Dict[str, Any]:
    """설계 §7 — 창(전체·최근 90일, 마지막 청산일 기준) × 묶음(전체·전략·월·청산 품질·cohort)."""
    cutoff = (today - timedelta(days=RECENT_DAYS)).isoformat()
    recent = [r for r in rows if r.get("last_exit_date") is not None and r["last_exit_date"] >= cutoff]
    return {
        "schema": SCHEMA,
        "computed_date": today.isoformat(),
        "rows": len(rows),
        "awaiting_close": awaiting_close,
        "drift": _drift(rows, previous_rows),
        "day_status_missing": day_status_missing,
        "windows": {
            "all": {k: _metrics(v) for k, v in _groups(rows).items()},
            "recent_90d": {k: _metrics(v) for k, v in _groups(recent).items()},
        },
    }


def _pct(v: Any) -> str:
    d = _d(v)
    return "-" if d is None else f"{d * 100:+.2f}%"


def _krw(v: Any) -> str:
    d = _d(v)
    return "-" if d is None else f"{d:+,.0f}원"


def format_weekly_line(summary: Dict[str, Any]) -> str:
    """토요일 텔레그램 한 줄(계산일 포함). HTML 이스케이프는 호출부가 한다."""
    groups = summary.get("windows", {}).get("all", {})
    m = groups.get("all", {})
    fill = groups.get("exit_quality:fill", {})
    ex = m.get("excluded", {}) or {}
    ex_n = sum(v.get("n", 0) for v in ex.values())
    known = [_d(v.get("net_pnl_sum")) for v in ex.values()]
    known = [k for k in known if k is not None]
    ex_sum = sum(known, Decimal("0")) if known else None
    ex_missing = sum(v.get("net_pnl_missing", 0) for v in ex.values())
    t = _d(m.get("t_excess"))
    day = str(summary.get("computed_date") or "????-??-??")[5:]
    verdict = "표본 판정 보류" if m.get("status") != "measured" else "측정값 — 자동 판정 없음"
    return (
        f"📏 실거래 초과수익(KODEX200·비용 차감, {day} 계산) n={m.get('n', 0)} "
        f"평균 {_pct(m.get('mean_excess'))} 합계 {_krw(m.get('excess_krw_sum'))} "
        f"t={'-' if t is None else f'{t:.1f}'} · 클립 {_pct(m.get('mean_clipped_excess'))} · "
        f"동기화 제외 n={fill.get('n', 0)} {_pct(fill.get('mean_excess'))} · "
        f"제외 {ex_n}건 {_krw(ex_sum)}{f'(손익 결측 {ex_missing}건)' if ex_missing else ''} ({verdict})"
    )


# ── exporter 로드·일일 갱신 ──────────────────────────────────────────────────

_EXPORTER = None
_EXPORTER_NAME = "_export_risk_ledger_for_excess_return"


def load_exporter(root: Path):
    """scripts/export_risk_ledger.py 를 파일 경로로 로드한다(backtest_gate 와 같은 방식, 프로세스 수명 캐시)."""
    global _EXPORTER
    if _EXPORTER is not None:
        return _EXPORTER
    path = Path(root) / "scripts" / "export_risk_ledger.py"
    spec = importlib.util.spec_from_file_location(_EXPORTER_NAME, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"exporter 로드 실패: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_EXPORTER_NAME] = module
    spec.loader.exec_module(module)
    _EXPORTER = module
    return module


def _weekdays(start: date, end: date) -> int:
    n, d = 0, start
    while d <= end:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            # append 도중 중단된 꼬리 줄 — 영구 실패로 번지지 않게 건너뛴다
            logger.warning(f"[초과수익] 손상된 줄 건너뜀: {path.name}")
    return rows


def _jsonl(rows: List[Dict[str, Any]]) -> str:
    return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)


async def _refresh_benchmark(broker: Any, path: Path, oldest_entry: Optional[str], today: date,
                             fetched_at: str) -> None:
    """일봉 조회 → 첫 날짜 ≤ 가장 오래된 진입일일 때만 CSV 통째 교체. 실패·부분 응답은 기존 캐시 유지."""
    if oldest_entry is None:
        logger.info("[초과수익] 종결 포지션 없음 — 벤치마크 조회 생략")
        return
    days = min(_weekdays(date.fromisoformat(oldest_entry), today) + 5, MAX_BENCH_DAYS)
    try:
        rows = kis_rows_to_bench(await broker.get_daily_prices(BENCH_SYMBOL, days=days))
    except Exception as e:
        logger.warning(f"[초과수익] 벤치마크 조회 실패 — 기존 캐시 사용: {type(e).__name__}: {e}")
        return
    if not rows:
        logger.warning("[초과수익] 벤치마크 응답 없음 — 기존 캐시 사용")
        return
    # 상한(500거래일)만큼 요청하고 실제로 그만큼 받았는데도 진입일에 못 미치면 브로커 한계다 — 받은 범위를 쓴다
    # (그 앞의 포지션은 bench_out_of_range 로 드러난다). 요청이 상한이어도 받은 행이 모자라면(2페이지 실패 시 1페이지만
    # 돌아온다 — kis_kr.py get_daily_prices) 부분 응답으로 보고 기존 캐시를 쓴다(교차 공급자 리뷰 P1).
    at_cap = days >= MAX_BENCH_DAYS and len(rows) >= MAX_BENCH_DAYS   # 상한만큼 **실제로 받았을 때만** 브로커 한계로 본다
    if rows[0][0] > oldest_entry and not at_cap:
        logger.warning(
            f"[초과수익] 벤치마크 부분 응답(첫 날짜 {rows[0][0]} > 진입 {oldest_entry}) — 기존 캐시 사용")
        return
    body = "date,close,source,fetched_at\n" + "".join(
        f"{d},{c},{BENCH_SOURCE},{fetched_at}\n" for d, c in rows)
    atomic_write_text(path, body)


def _append_history(path: Path, line: Dict[str, Any]) -> None:
    """같은 계산일 줄이 없을 때만 append + fsync."""
    for row in _read_jsonl(path):
        if row.get("computed_date") == line["computed_date"]:
            return
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        raw = path.read_bytes()
        if raw and not raw.endswith(b"\n"):
            # append 도중 중단된 꼬리 — 마지막 줄바꿈까지로 되돌려 다음 기록이 같은 물리 줄에 붙지 않게 한다
            keep = raw[: raw.rfind(b"\n") + 1]
            logger.warning(f"[초과수익] 이력 손상 꼬리 {len(raw) - len(keep)}바이트 제거: {path.name}")
            atomic_write_text(path, keep.decode("utf-8", errors="replace"))
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


# ── 거래일 기록 대사 (설계 §5-1, 계획 T8·T9) ──────────────────────────────────
# complete = "20:30 대사가 불일치를 찾지 못했다" — 완전성의 증명이 아니다(엔진 기록에 주문번호가 없다).
# 날짜는 호출부 now(호스트 로컬 naive)의 date — event_time 과 같은 규약이라 KST 로 바꾸지 않는다.

WRITE_QUEUE_WAIT_SEC = 20
MAX_REASONS = 20
DAY_STATUS_SOURCE = "kr_excess_20_30"

_SQL_DAY_SUMS = (
    "SELECT e.symbol, e.event_type, SUM(e.quantity) AS qty FROM trade_events e "
    "JOIN trades t ON t.id = e.trade_id WHERE t.market = 'KR' AND e.event_time::date = $1 "
    "GROUP BY e.symbol, e.event_type")
_SQL_TRADE_ROWS = (
    "SELECT t.id, t.exit_quantity, SUM(e.quantity) AS sold FROM trades t "
    "JOIN trade_events e ON e.trade_id = t.id AND e.event_type = 'SELL' "
    "WHERE t.market = 'KR' AND t.id IN (SELECT DISTINCT trade_id FROM trade_events WHERE event_type = 'SELL' "
    "AND event_time::date = $1) GROUP BY t.id, t.exit_quantity")
_SQL_SAVE_STATUS = (
    "INSERT INTO execution_day_status (trade_date, status, reasons, source, checked_at, updated_at) "
    f"VALUES ($1,$2,$3,'{DAY_STATUS_SOURCE}',$4,$4) ON CONFLICT (trade_date) DO UPDATE SET "
    "status=EXCLUDED.status, reasons=EXCLUDED.reasons, source=EXCLUDED.source, "
    "checked_at=EXCLUDED.checked_at, updated_at=EXCLUDED.updated_at")
_SQL_LOAD_STATUS = "SELECT trade_date, status FROM execution_day_status WHERE trade_date >= $1"


def _qty(v: Any) -> Decimal:
    q = to_decimal(v)
    return q if q is not None else Decimal("0")


async def verify_day_records(*, broker: Any, fetch: Any, write_queue: Any, day: date) -> Dict[str, Any]:
    """KIS 당일 체결(완결 확인) ↔ DB trade_events·trades 대조. 저장하지 않고 결과만 돌려준다."""
    reasons = set()
    checked: Dict[str, Any] = {}

    if write_queue is not None:                                              # ①
        try:
            await asyncio.wait_for(write_queue.join(), WRITE_QUEUE_WAIT_SEC)
        except asyncio.TimeoutError:
            reasons.add("write_queue_pending")

    fills = None                                                             # ②
    query = getattr(broker, "get_fills_for_date_checked", None)
    if query is None:
        reasons.add("fill_query_unavailable")
    else:
        try:
            rows, complete, why = await query(day)
        except Exception as e:
            logger.warning(f"[초과수익] 체결 조회 예외: {type(e).__name__}: {e}")
            rows, complete, why = [], False, "exception"
        if complete:
            fills = rows
        else:
            reasons.add(f"fill_query_incomplete:{why}")

    try:
        if fills is not None:                                                # ③ ④ — 미완 조회는 부분 목록이라 비교하지 않는다
            kis_sell: Dict[str, Decimal] = defaultdict(Decimal)
            kis_buy = set()
            for f in fills:
                if f.get("sll_buy_dvsn_cd") == "01":
                    kis_sell[f["symbol"]] += _qty(f.get("tot_ccld_qty"))
                elif f.get("sll_buy_dvsn_cd") == "02":
                    kis_buy.add(f["symbol"])
            db_sell: Dict[str, Decimal] = defaultdict(Decimal)
            db_buy = set()
            sums = await fetch(_SQL_DAY_SUMS, day)
            for r in sums:
                if r["event_type"] == "SELL":
                    db_sell[r["symbol"]] += _qty(r["qty"])
                elif r["event_type"] == "BUY":
                    db_buy.add(r["symbol"])
            for sym in set(kis_sell) | set(db_sell):
                if kis_sell.get(sym, Decimal("0")) != db_sell.get(sym, Decimal("0")):
                    reasons.add(f"sell_qty:{sym}")
            for sym in kis_buy ^ db_buy:                                     # BUY 수량은 비교하지 않는다
                reasons.add(f"buy:{sym}")
            checked.update(kis_fills=len(fills), db_groups=len(sums))
        trade_rows = await fetch(_SQL_TRADE_ROWS, day)                       # ⑤ 손익은 비교하지 않는다
        for r in trade_rows:
            if _qty(r["exit_quantity"]) != _qty(r["sold"]):
                reasons.add(f"trade_row:{r['id']}")
        checked["trades_checked"] = len(trade_rows)
    except Exception as e:                                                   # ⑦
        logger.warning(f"[초과수익] 기록 대사 DB 조회 실패: {type(e).__name__}: {e}")
        reasons.add("db_query_failed")

    out = sorted(reasons)
    if len(out) > MAX_REASONS:
        out = out[:MAX_REASONS] + [f"…(+{len(out) - MAX_REASONS})"]
    return {"status": "incomplete" if out else "complete", "reasons": out, "checked": checked}   # ⑥


async def save_day_status(execute: Any, day: date, result: Dict[str, Any], now: datetime) -> bool:
    """execution_day_status upsert. 실패는 로그만 남기고 False."""
    try:
        await execute(_SQL_SAVE_STATUS, day, result["status"],
                      json.dumps(result["reasons"], ensure_ascii=False), now)
        return True
    except Exception as e:
        logger.warning(f"[초과수익] 거래일 상태 저장 실패: {type(e).__name__}: {e}")
        return False


async def load_day_status(fetch: Any, since: date) -> Dict[Any, str]:
    """상태 표 조회. 표 없음(도입 전)만 {} — 그 밖의 실패는 올려 이번 갱신을 중단한다(기존 파일 보존)."""
    try:
        rows = await fetch(_SQL_LOAD_STATUS, since)
    except Exception as e:
        # 표 없음만 — 열 없음(UndefinedColumnError) 등 다른 'does not exist' 는 갱신 중단 쪽으로 올린다(coordinator 보완)
        if type(e).__name__ == "UndefinedTableError" or 'relation "execution_day_status" does not exist' in str(e):
            logger.info("[초과수익] execution_day_status 표 없음 — 상태 없음으로 진행")
            return {}
        raise
    return {r["trade_date"]: r["status"] for r in rows}


async def run_daily_update(*, broker: Any, fetch: Any, out_dir: Path, root: Path, now: datetime,
                           code_sha: str, day_status: Optional[Dict[Any, str]] = None,
                           execute: Any = None, write_queue: Any = None) -> Dict[str, Any]:
    """설계 §4 ①~④ — 포지션 원장 → 벤치마크 캐시 → 스냅샷 → 요약. 예외는 호출부(스케줄러)가 삼킨다.

    fetch = 봇 TradeStorage `pool.fetch` (connect/disconnect 하지 않는다).
    execute 가 있으면(운영) 오늘 기록 대사 → 상태 저장 → 상태 표 읽기(오늘은 이번 결과로 덮음)로 day_status 를 만든다.
    상태 표 조회 실패(표 없음 제외)는 파일을 하나도 쓰기 전에 예외로 올린다.
    execute 가 없으면 day_status 인자를 그대로 쓴다(1단계 경로).
    """
    out_dir = Path(out_dir)
    today = now.date()
    computed_at = now.isoformat()
    exporter = load_exporter(root)

    verdict: Optional[Dict[str, Any]] = None
    saved: Optional[bool] = None
    if execute is not None:
        verdict = await verify_day_records(broker=broker, fetch=fetch, write_queue=write_queue, day=today)
        saved = await save_day_status(execute, today, verdict, now)

    # ② 먼저 — 가장 오래된 진입일을 알아야 ① 의 조회 범위를 정한다(결과는 설계 순서와 같다)
    trades = await exporter.fetch_trade_records(fetch, LEDGER_DAYS)
    positions = exporter.build_ledger(trades, {})["positions"]
    entries = [e for e in (_span(p)[0] for p in positions if p.get("status") == "closed") if e is not None]
    oldest_entry = min(entries) if entries else None

    if verdict is not None:
        since = date.fromisoformat(oldest_entry) if oldest_entry is not None else today
        day_status = _norm_status(await load_day_status(fetch, since))
        day_status[today.isoformat()] = verdict["status"]      # 저장 성공 여부와 무관하게 이번 결과

    # ① 벤치마크 캐시
    bench_path = out_dir / BENCH_FILE
    await _refresh_benchmark(broker, bench_path, oldest_entry, today, computed_at)
    bench, bench_reason = load_benchmark(bench_path)
    if bench is None:
        logger.warning(f"[초과수익] 벤치마크 없음 — 초과수익 null 로 기록: {bench_reason}")

    # ③ 스냅샷 (전체 재계산)
    rows: List[Dict[str, Any]] = []
    awaiting = gaps = 0
    for pos in positions:
        row = position_row(pos, bench, day_status, computed_at=computed_at, code_sha=code_sha)
        if row is None:
            if classify(pos, bench, day_status) == "awaiting_close":
                awaiting += 1
            continue
        if row["exclusion"] is None and has_day_status_gap(pos, bench, day_status):
            gaps += 1
        rows.append(row)

    snap_path, prev_path = out_dir / SNAPSHOT_FILE, out_dir / PREV_SNAPSHOT_FILE
    meta_path = out_dir / SNAPSHOT_META_FILE
    if snap_path.is_file():
        snap_day = None
        try:
            snap_day = json.loads(meta_path.read_text(encoding="utf-8")).get("computed_date") if meta_path.is_file() else None
        except (OSError, json.JSONDecodeError):
            snap_day = None
        if snap_day is None:                     # 메타가 없던 옛 스냅샷 — 첫 행 계산 시각으로 대신 본다
            current_rows = _read_jsonl(snap_path)
            snap_day = str(current_rows[0].get("computed_at", ""))[:10] if current_rows else None
        if snap_day != today.isoformat():
            # 직전 계산일 스냅샷 보관 — 같은 날 재실행은 그날 첫 실행 전 기준을 그대로 쓴다(빈 스냅샷도 날짜로 판정)
            atomic_write_text(prev_path, snap_path.read_text(encoding="utf-8"))
    previous = _read_jsonl(prev_path)
    atomic_write_text(snap_path, _jsonl(rows))
    atomic_write_json(meta_path, {"computed_date": today.isoformat(), "computed_at": computed_at})

    # ④ 요약
    summary = summarize(rows, previous, today=today, awaiting_close=awaiting, day_status_missing=gaps)
    summary.update({
        "computed_at": computed_at, "code_sha": code_sha,
        "bench_source": BENCH_SOURCE if bench is not None else None,
        "bench_missing_reason": bench_reason,
        "bench_first_date": min(bench) if bench else None,
        "bench_last_date": max(bench) if bench else None,
        "day_status_today": verdict["status"] if verdict else None,
        "day_status_reasons": verdict["reasons"] if verdict else None,
        "day_status_saved": saved,
    })
    atomic_write_json(out_dir / SUMMARY_FILE, summary, indent=2)
    head = summary["windows"]["all"]["all"]
    _append_history(out_dir / HISTORY_FILE, {
        "computed_date": summary["computed_date"], "computed_at": computed_at, "code_sha": code_sha,
        "rows": summary["rows"], "awaiting_close": awaiting, "drift": summary["drift"],
        "day_status_missing": gaps, **{k: head[k] for k in (
            "n", "bench_covered", "mean_excess", "median_excess", "excess_krw_sum",
            "excess_krw_excl_top3", "t_excess", "mean_clipped_excess", "status")},
    })
    logger.info(
        f"[초과수익] 원장 갱신: 행 {len(rows)} (포함 {head['n']}) 대기 {awaiting} drift {summary['drift']} "
        f"평균 초과 {head['mean_excess']} ({head['status']})")
    return summary
