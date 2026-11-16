#!/usr/bin/env python3
"""V08 — 밸류코어 섀도우 판정 (연구 전용, 2026-11-17).

질문·수익 정의·판정 규칙: docs/research/v08-value-core-shadow-verdict-2026-11-17.md (가격 조회 전 사전 등록).
KIS 무접촉. 입력은 주간 이력(읽기만), 가격은 V01 과 같은 공개 일봉과 벤치마크 보정.

  prices : 최종 픽·LLM 탈락 종목 + 069500 일봉을 CSV 캐시로 받는다
  report : 이력 + 가격 → summary.json (판정 포함)

ponytail: 계산은 V01 의 forward/load_bars/adjust_bench/fetch_prices/nw_t 를 그대로 쓴다. 통계는 주차 평균 하나다.
"""

import argparse
import datetime
import glob
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
NW_LAG = 3                   # 주간 관측·20거래일 구간 → 앞뒤 3주와 겹친다(보조 t 전용)


def load_history(history_dir, through):
    """(관측 행, 전체 주차 수, 최종 픽이 없는 주차).

    kind: final | llm_rejected(정량 통과·LLM 심사 탈락) | llm_error(심사 호출·응답 실패 — 생산자는 이것도
    passed=False 로 남기되 total 0·한 줄 근거 없음으로 적는다. 탈락군에 넣지 않고 건수만 센다).
    """
    rows, weeks, empty = [], 0, []
    for path in sorted(glob.glob(os.path.join(history_dir, "*.json"))):
        with open(path) as f:
            x = json.load(f)
        if x["scan_date"] > through:
            continue
        weeks += 1
        final = list(x.get("final", []))
        if not final:
            empty.append(x["scan_week"])
        bucket = {q["symbol"]: q.get("bucket") for q in x.get("quant_passed", [])}
        base = {"week": x["scan_week"], "scan_date": x["scan_date"]}
        rows += [{**base, "symbol": s, "bucket": bucket.get(s), "kind": "final"} for s in final]
        rows += [{**base, "symbol": s, "bucket": bucket.get(s),
                  "kind": "llm_error" if not v.get("total") and not v.get("one_line") else "llm_rejected"}
                 for s, v in x.get("llm_verdicts", {}).items() if s not in final and v.get("passed") is False]
    return rows, weeks, empty


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


def week_index(week):
    """'2026-W35' → 연속 주 번호. 픽이 없는 주가 사이에 있어도 실제 주 간격이 유지된다(연도 경계 포함)."""
    year, wk = week.split("-W")
    return datetime.date.fromisocalendar(int(year), int(wk), 1).toordinal() // 7


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


def supplementary(final_obs, rejected_obs):
    """판정에 쓰지 않는 보조 지표."""
    weeks = weekly_excess(final_obs)
    xs = [float(v) for v in weeks.values()]
    first = {}
    for o in sorted(final_obs, key=lambda o: o["scan_date"]):
        first.setdefault(o["symbol"], o)
    rej = weekly_excess(rejected_obs)
    both = [w for w in weeks if w in rej]
    out = {
        "weeks_won": sum(1 for v in weeks.values() if v > 0),
        "t_weekly_nw": nw_t(xs, NW_LAG, [week_index(w) for w in weeks]),
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


def report(history_dir, price_dir, bench_adjust, through):
    rows, weeks_total, empty = load_history(history_dir, through)
    bench = adjust_bench(load_bars(os.path.join(price_dir, f"{BENCH}.csv")), bench_adjust, through)
    cal = sorted(bench)
    symbols = sorted({r["symbol"] for r in rows})
    bars = {s: {d: v for d, v in load_bars(os.path.join(price_dir, f"{s}.csv")).items() if d <= through} for s in symbols}
    obs, excluded = observe(rows, bars, bench, cal)
    final_obs = [o for o in obs if o["kind"] == "final"]
    rejected_obs = [o for o in obs if o["kind"] == "llm_rejected"]
    llm_errors = sum(1 for r in rows if r["kind"] == "llm_error")
    wk = weekly_excess(final_obs)
    table = [{"week": w, "scan_date": next(o["scan_date"] for o in final_obs if o["week"] == w),
              "entry_day": next(o["entry_day"] for o in final_obs if o["week"] == w),
              "picks": sum(1 for o in final_obs if o["week"] == w),
              "bench": next(o[f"bench{H}"] for o in final_obs if o["week"] == w), "excess": v} for w, v in wk.items()]
    return _plain({"through": through, "calendar": [cal[0], cal[-1], len(cal)], "weeks_total": weeks_total,
                   "weeks_without_picks": empty, "final_picks_total": sum(1 for r in rows if r["kind"] == "final"),
                   "excluded": excluded, "llm_errors": llm_errors, "judgement": judge(final_obs), "weekly": table,
                   "supplementary": supplementary(final_obs, rejected_obs),
                   "picks": [{k: o[k] for k in ("week", "symbol", "bucket", "entry_day", f"gross{H}", f"bench{H}", f"excess{H}", f"mae{H}")}
                             for o in final_obs]})


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("prices", "report"):
        p = sub.add_parser(name)
        p.add_argument("--history-dir", required=True)
        p.add_argument("--price-dir", required=True)
        p.add_argument("--through", required=True, help="고정 종료일 YYYY-MM-DD")
    sub.choices["prices"].add_argument("--start", default="2026-02-01", help="벤치마크 보정 자료의 시작일(2026-02-09)보다 앞이어야 한다")
    sub.choices["report"].add_argument("--bench-adjust", required=True, help="초과수익 원장의 kodex200_daily.csv")
    sub.choices["report"].add_argument("--out", required=True)
    a = ap.parse_args()
    if a.cmd == "prices":
        rows, _, _ = load_history(a.history_dir, a.through)
        fetch_prices(sorted({r["symbol"] for r in rows} | {BENCH}), a.price_dir, a.start, a.through)
        return
    summary = report(a.history_dir, a.price_dir, a.bench_adjust, a.through)
    with open(a.out, "w") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    print(json.dumps({k: summary[k] for k in ("through", "weeks_total", "excluded", "judgement")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
