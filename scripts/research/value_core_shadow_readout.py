#!/usr/bin/env python3
"""V08 — 밸류코어 섀도우 판정 (연구 전용, 2026-11-17).

질문·수익 정의·판정 규칙: docs/research/v08-value-core-shadow-verdict-2026-11-17.md (가격 조회 전 사전 등록).
KIS 무접촉. 입력은 주간 이력(읽기만), 가격은 V01 과 같은 공개 일봉과 벤치마크 보정.

  prices : 최종 픽·LLM 탈락 종목 + 069500 일봉을 CSV 캐시로 받는다
  report : 이력 + 가격 → summary.json (판정 포함)

ponytail: 계산은 V01 의 forward/load_bars/adjust_bench/fetch_prices/nw_t 를 그대로 쓴다. 통계는 주차 평균 하나다.
"""

import argparse
import csv
import glob
import hashlib
import json
import math
import os
from collections import defaultdict
from decimal import Decimal

from candidate_forward_excess import BENCH, COST, adjust_bench, fetch_prices, forward, load_bars, nw_t, slope

H = 20                       # 판정 구간(거래일)
EXTRA_H = (5, 10)            # 보조
MIN_WEEKS = 6
FLOOR = Decimal("-0.15")     # 기준 3: 하위 25% 총수익 하한


def load_history(history_dir, through):
    """(관측 행, 전체 주차 [(주차, 스캔일)], 최종 픽이 없는 주차).

    kind: final | llm_rejected(정량 통과·LLM 심사 탈락) | llm_unknown(탈락인지 심사 실패인지 기록만으로 알 수 없음).
    """
    rows, weeks, empty = [], [], []
    for path in sorted(glob.glob(os.path.join(history_dir, "*.json"))):
        with open(path) as f:
            x = json.load(f)
        if x["scan_date"] > through:
            continue
        final = list(x.get("final", []))
        if x["scan_week"] in {w for w, _d in weeks} or len(set(final)) != len(final):
            raise SystemExit(f"주간 이력 중복: {path} (같은 주차가 두 번 있거나 최종 픽에 같은 종목이 두 번 있다)")
        weeks.append((x["scan_week"], x["scan_date"]))
        if not final:
            empty.append(x["scan_week"])
        bucket = {q["symbol"]: q.get("bucket") for q in x.get("quant_passed", [])}
        base = {"week": x["scan_week"], "scan_date": x["scan_date"]}
        rows += [{**base, "symbol": s, "bucket": bucket.get(s), "kind": "final"} for s in final]
        rows += [{**base, "symbol": s, "bucket": bucket.get(s), "kind": not_passed_kind(v)}
                 for s, v in x.get("llm_verdicts", {}).items() if s not in final and v.get("passed") is False]
    return rows, weeks, empty


def not_passed_kind(verdict):
    """이력에는 오류 표식이 없다. 생산자는 호출·응답 실패를 점수 0·근거 빈 문자열로 남기는데, 정상 심사의 0점도
    같은 모양일 수 있어 '불명'으로 둔다(필드가 빠진 기록도 불명). 불명은 탈락군에 넣지 않고 건수만 센다."""
    total, line = verdict.get("total"), verdict.get("one_line")
    if total is None or line is None or (total == 0 and line == ""):
        return "llm_unknown"
    return "llm_rejected"


def observe(rows, bars_by_symbol, bench, cal, horizons=(H,) + EXTRA_H, main=H):
    """스캔일 다음 거래일 시가 진입. 반환 (관측, 제외 사유별 건수) — 포함·제외는 판정 구간(main) 기준."""
    obs, excluded = [], defaultdict(int)
    for r in rows:
        day = next((d for d in cal if d > r["scan_date"]), None)
        if day is None:
            excluded[f"{r['kind']}:no_next_trading_day"] += 1
            continue
        o = {**r, "entry_day": day}
        for h in horizons:
            got, why = forward(bars_by_symbol.get(r["symbol"], {}), cal, day, "open", h)
            b, bwhy = forward(bench, cal, day, "open", h)
            if got is None or b is None:
                if h == main:
                    excluded[f"{r['kind']}:{why or bwhy}"] += 1
                continue
            o[f"gross{h}"], o[f"bench{h}"], o[f"mae{h}"] = got[0], b[0], got[2]
            o[f"excess{h}"] = got[0] - COST - b[0]      # 픽만 왕복 비용 차감
        if f"excess{main}" in o:
            obs.append(o)
    return obs, dict(excluded)


def _mean(xs):
    return sum(xs) / len(xs)


def weekly_excess(obs, h=H):
    """{주차: 그 주 픽 순초과의 동일가중 평균} — 같은 주의 픽은 진입일·벤치마크 구간이 같다."""
    by = defaultdict(list)
    for o in obs:
        if f"excess{h}" in o:
            by[o["week"]].append(o[f"excess{h}"])
    return {w: _mean(v) for w, v in sorted(by.items())}


def judge(obs):
    """사전 등록 3절. obs 는 kind=final 관측만. 반환 dict 의 verdict: extend | close | extend_once | eligible."""
    weeks = weekly_excess(obs)
    n_weeks = len(weeks)
    mean_excess = _mean(list(weeks.values())) if weeks else None
    gross = sorted(o[f"gross{H}"] for o in obs)
    quartile = gross[math.ceil(Decimal("0.25") * len(gross)) - 1] if gross else None
    c2 = n_weeks >= MIN_WEEKS and mean_excess is not None and mean_excess > 0
    c3 = quartile is not None and quartile >= FLOOR
    if n_weeks < MIN_WEEKS:
        verdict = "extend"
    elif not mean_excess > 0:
        verdict = "close"
    elif not c3:
        verdict = "extend_once"
    else:
        verdict = "eligible"
    return {"weeks": n_weeks, "picks": len(obs), "mean_weekly_excess": mean_excess, "criterion2": c2,
            "lower_quartile_gross": quartile, "criterion3": c3, "verdict": verdict}


def supplementary(final_obs, rejected_obs, cal):
    """판정에 쓰지 않는 보조 지표. cal 은 거래일 달력(보조 t 의 시차를 진입일 사이의 실제 거래일 거리로 잰다)."""
    weeks = weekly_excess(final_obs)
    xs = [float(v) for v in weeks.values()]
    pos = {d: i for i, d in enumerate(cal)}
    entry = {o["week"]: o["entry_day"] for o in final_obs}
    first = {}
    for o in sorted(final_obs, key=lambda o: o["scan_date"]):
        first.setdefault(o["symbol"], o)
    rej = weekly_excess(rejected_obs)
    both = [w for w in weeks if w in rej]
    out = {
        "weeks_won": sum(1 for v in weeks.values() if v > 0),
        # 20거래일 구간은 진입일이 19거래일 이내로 떨어진 주끼리 겹친다 — 휴일이 낀 4주 간격도 포함된다
        "t_weekly_nw": nw_t(xs, H - 1, [pos[entry[w]] for w in weeks]),
        "first_pick_only": {"n": len(first), "mean_excess": _mean([o[f"excess{H}"] for o in first.values()]) if first else None},
        "by_bucket": {b: {"n": len(v), "mean_excess": _mean(v)} for b, v in sorted(_group(final_obs, "bucket").items())},
        "by_symbol_count": sorted((len(v) for v in _group(final_obs, "symbol").values()), reverse=True),
        "worst_mae": min((o[f"mae{H}"] for o in final_obs), default=None),
        "llm_rejected": {"weeks": len(rej), "picks": len(rejected_obs),
                         "mean_weekly_excess": _mean(list(rej.values())) if rej else None,
                         "final_minus_rejected": _mean([weeks[w] - rej[w] for w in both]) if both else None,
                         "paired_weeks": len(both)},
    }
    for h in EXTRA_H:
        w = weekly_excess(final_obs, h)
        out[f"mean_weekly_excess_{h}d"] = _mean(list(w.values())) if w else None
    out["post_hoc"] = post_hoc(final_obs, weeks)
    return out


def post_hoc(final_obs, weeks):
    """결과를 본 뒤 추가한 계산(2026-11-17) — 사전 등록 4절에 없던 것이라 해석 보조로만 쓴다."""
    bench = {o["week"]: o[f"bench{H}"] for o in final_obs}
    up = [weeks[w] for w in weeks if bench[w] > 0]
    down = [weeks[w] for w in weeks if bench[w] <= 0]
    gross = [o[f"gross{H}"] for o in final_obs]
    by_week = defaultdict(list)
    for o in final_obs:
        by_week[o["week"]].append(o[f"gross{H}"])
    top = max(_group(final_obs, "symbol").items(), key=lambda kv: len(kv[1]), default=(None, []))[0]
    rest = weekly_excess([o for o in final_obs if o["symbol"] != top])
    return {
        "bench_up_weeks": {"n": len(up), "mean_excess": _mean(up) if up else None},
        "bench_down_weeks": {"n": len(down), "mean_excess": _mean(down) if down else None},
        "slope_excess_on_bench": slope([(float(bench[w]), float(weeks[w])) for w in weeks]),
        # 두 평균 모두 주차 동일가중 — 주 판정과 같은 가중이라 (총수익 − 비용 − 벤치마크) = 주차 초과 평균이 성립한다
        "mean_weekly_gross": _mean([_mean(v) for v in by_week.values()]) if by_week else None,
        "mean_weekly_bench": _mean([bench[w] for w in weeks]) if weeks else None,
        "positive_gross_picks": sum(1 for g in gross if g > 0),
        "without_most_repeated_symbol": {"picks_removed": sum(1 for o in final_obs if o["symbol"] == top), "weeks": len(rest),
                                         "mean_weekly_excess": _mean(list(rest.values())) if rest else None},
    }


def _group(obs, key):
    by = defaultdict(list)
    for o in obs:
        by[str(o[key])].append(o[f"excess{H}"])
    return by


def _plain(x):
    if isinstance(x, Decimal):
        return round(float(x), 6)
    if isinstance(x, dict):
        return {k: _plain(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_plain(v) for v in x]
    return x


def report(history_dir, price_dir, bench_adjust, through, allow_partial_prices=False):
    rows, weeks_total, empty = load_history(history_dir, through)
    bench = adjust_bench(load_bars(os.path.join(price_dir, f"{BENCH}.csv")), bench_adjust, through)
    cal = sorted(bench)
    symbols = sorted({r["symbol"] for r in rows})
    missing = [s for s in symbols if not os.path.exists(os.path.join(price_dir, f"{s}.csv"))]
    if missing:
        raise SystemExit(f"가격 파일 없는 종목 {len(missing)}개: {missing[:10]} — prices 를 다시 실행한다")
    # adjust_bench 는 보정 자료의 [첫날, 끝날] 밖을 버린다. 달력이 줄면 진입일이 뒤로 밀리거나(앞이 없을 때)
    # 만기가 지난 픽이 '미도래'로 빠진다(뒤가 없을 때) → 달력이 [가장 이른 스캔일, 종료일] 을 덮는지 먼저 본다.
    # 종료일은 달력의 마지막 거래일이어야 한다 — 가격·보정 자료가 모두 함께 오래된 경우도 여기서 걸린다.
    if cal[-1] != through:
        raise SystemExit(f"달력의 마지막 거래일({cal[-1]})이 종료일({through})과 다르다 — 종료일은 마지막 거래일로 지정하고, "
                         "벤치마크 일봉·보정 자료(kodex200_daily.csv)가 그날까지 있는지 확인한다")
    first_scan_all = min((r["scan_date"] for r in rows), default=None)
    if first_scan_all is not None and cal[0] > first_scan_all:
        raise SystemExit(f"달력 시작({cal[0]})이 가장 이른 스캔일({first_scan_all})보다 늦다 — 진입일이 뒤로 밀린다. "
                         "벤치마크 일봉·보정 자료의 시작일을 확인한다")
    fingerprint = hashlib.sha256()
    for s in [BENCH] + symbols:
        with open(os.path.join(price_dir, f"{s}.csv"), "rb") as f:
            fingerprint.update(s.encode() + f.read())
    bars = {s: {d: v for d, v in load_bars(os.path.join(price_dir, f"{s}.csv")).items() if d <= through} for s in symbols}
    # V01 과 같은 검사: 캐시가 [그 종목의 첫 스캔일, 마지막 거래일]을 덮지 못하면 missing_bar 로 조용히 빠져
    # 남은 픽만으로 판정이 나온다 → 기본은 거부, 거래정지·상장폐지가 확인된 경우만 명시적으로 허용한다.
    first_scan = {}
    for r in rows:
        first_scan[r["symbol"]] = min(first_scan.get(r["symbol"], r["scan_date"]), r["scan_date"])
    partial = sorted(s for s in symbols if not bars[s] or max(bars[s]) < cal[-1] or min(bars[s]) > first_scan[s])
    if partial and not allow_partial_prices:
        raise SystemExit(f"가격 캐시가 분석 기간을 덮지 못하는 종목 {len(partial)}개: {partial[:10]} — "
                         "prices 를 새 디렉터리로 다시 받거나, 거래정지·상장폐지가 맞으면 --allow-partial-prices 로 명시한다")
    obs, excluded = observe(rows, bars, bench, cal)
    final_obs = [o for o in obs if o["kind"] == "final"]
    rejected_obs = [o for o in obs if o["kind"] == "llm_rejected"]
    llm_unknown = sum(1 for r in rows if r["kind"] == "llm_unknown")
    with open(bench_adjust) as f:   # 보정 자료는 날마다 행·조회 시각이 늘어난다 → 종료일까지의 (날짜, 종가)만 지문에 넣는다
        adj_rows = sorted((r["date"], r["close"]) for r in csv.DictReader(f) if r["date"] <= through)
    digest = lambda x: hashlib.sha256(json.dumps(x, sort_keys=True, ensure_ascii=False).encode()).hexdigest()  # noqa: E731
    # 픽·심사 기록이 없는 주차도 관측 기간의 일부다 → 모든 주차를 지문에 넣는다
    inputs = {"history_sha256": digest({"weeks": weeks_total,
                                        "rows": sorted(rows, key=lambda r: (r["scan_date"], r["kind"], r["symbol"]))}),
              "bench_adjust_sha256": digest(adj_rows)}
    wk = weekly_excess(final_obs)
    table = [{"week": w, "scan_date": next(o["scan_date"] for o in final_obs if o["week"] == w),
              "entry_day": next(o["entry_day"] for o in final_obs if o["week"] == w),
              "picks": sum(1 for o in final_obs if o["week"] == w),
              "bench": next(o[f"bench{H}"] for o in final_obs if o["week"] == w), "excess": v} for w, v in wk.items()]
    return _plain({"through": through, "calendar": [cal[0], cal[-1], len(cal)], "weeks_total": len(weeks_total),
                   "weeks_without_picks": empty, "final_picks_total": sum(1 for r in rows if r["kind"] == "final"),
                   "excluded": excluded, "llm_unknown": llm_unknown, "inputs": inputs,
                   "prices": {"symbols": len(symbols), "sha256": fingerprint.hexdigest(), "partial_coverage": len(partial),
                              "allow_partial_prices": allow_partial_prices},
                   "judgement": judge(final_obs), "weekly": table,
                   "supplementary": supplementary(final_obs, rejected_obs, cal),
                   "picks": [{k: o[k] for k in ("week", "symbol", "bucket", "entry_day", f"gross{H}", f"bench{H}", f"excess{H}", f"mae{H}")}
                             for o in final_obs]})


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("prices", "report"):
        p = sub.add_parser(name)
        p.add_argument("--history-dir", required=True)
        p.add_argument("--price-dir", required=True)
        p.add_argument("--through", required=True, help="고정 종료일 YYYY-MM-DD — 마지막 거래일로 지정한다")
    sub.choices["prices"].add_argument("--start", default="2026-02-01", help="벤치마크 보정 자료의 시작일(2026-02-09)보다 앞이어야 한다")
    sub.choices["report"].add_argument("--bench-adjust", required=True, help="초과수익 원장의 kodex200_daily.csv")
    sub.choices["report"].add_argument("--out", required=True)
    sub.choices["report"].add_argument("--allow-partial-prices", action="store_true",
                                       help="분석 기간을 덮지 못하는 가격 캐시를 허용(거래정지·상장폐지 확인 시에만)")
    a = ap.parse_args()
    if a.cmd == "prices":
        rows, _, _ = load_history(a.history_dir, a.through)
        fetch_prices(sorted({r["symbol"] for r in rows} | {BENCH}), a.price_dir, a.start, a.through)
        return
    summary = report(a.history_dir, a.price_dir, a.bench_adjust, a.through, a.allow_partial_prices)
    with open(a.out, "w") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    print(json.dumps({k: summary[k] for k in ("through", "calendar", "weeks_total", "excluded", "llm_unknown", "inputs", "prices", "judgement")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
