#!/usr/bin/env python3
"""V01 — 기존 후보의 사후 KODEX200 초과수익 판독 (연구 전용, 2026-11-13).

질문·수익 정의·판정 기준: docs/research/v01-candidate-forward-excess-2026-11-13.md (가격 조회 전 사전 등록).
KIS 무접촉. 입력은 운영 서버에 이미 있는 기록(읽기만), 가격은 공개 일봉(FinanceDataReader 수정주가).

  signals: Postgres signal_events → JSONL(읽기 전용, DATABASE_URL 환경변수). 행 = {t(KST ISO), symbol, strategy, score, type, gate}
  obs    : pit_universe / signals JSONL / team_ledger → 관측(obs.jsonl)
  prices : 관측 종목 + 069500 일봉을 CSV 캐시로 내려받는다(연속 실패 5회면 중단)
  report : obs + 가격 → summary.json (판정 포함)

ponytail: 날짜별 평균 + Newey-West 만 쓴다. 종목 군집·부트스트랩은 판정이 경계선일 때 추가.
"""

import argparse
import csv
import glob
import hashlib
import json
import math
import os
import time
from collections import defaultdict
from statistics import mean

BENCH = "069500"
COST = 0.00227          # KR 왕복 수수료+거래세
HORIZONS = (1, 5, 10, 20)
STOP = 0.05             # 손절 클립(참고)
MAX_DAILY_MOVE = 0.35   # 초과 시 자료 오류로 제외
MIN_DAYS_FOR_T = 20


# ---------- 순수 계산 ----------

def forward(bars, cal, day, entry, h):
    """(수익, 손절클립 수익, 최대역행, 종료일) 또는 (None, 제외사유).

    bars: {date: (o, h, l, c)}, cal: 거래일 오름차순 목록(벤치마크 기준).
    entry='open' → D 시가 매수·D 포함 h번째 종가 매도 / 'close' → D 종가 매수·D 다음 h번째 종가 매도.
    """
    if day not in cal:
        return None, "not_trading_day"
    i = cal.index(day)
    first, last = (i, i + h - 1) if entry == "open" else (i + 1, i + h)
    if last >= len(cal):
        return None, "horizon_not_reached"
    span = cal[i:last + 1]
    if any(d not in bars or min(bars[d]) <= 0 for d in span):
        return None, "missing_bar"
    px = bars[day][0] if entry == "open" else bars[day][3]
    for a, b in zip(span, span[1:]):
        if abs(bars[b][3] / bars[a][3] - 1) > MAX_DAILY_MOVE:
            return None, "data_error"
    ret = bars[cal[last]][3] / px - 1
    stop_px, clip, clip_day = px * (1 - STOP), ret, cal[last]
    mae, stopped = 0.0, False
    for d in cal[first:last + 1]:
        o, _h, low, _c = bars[d]
        mae = min(mae, low / px - 1)
        if not stopped and low <= stop_px:
            # 시가 매수 당일의 시가는 매수가 자체라 갭 종료가 아니다
            gap = o <= stop_px and not (entry == "open" and d == day)
            clip, clip_day, stopped = (o / px - 1 if gap else -STOP), d, True
    return (ret, clip, mae, clip_day), None


def nw_t(xs, lag):
    """평균의 Newey-West t (Bartlett 가중)."""
    n = len(xs)
    if n < 2:
        return None
    m = mean(xs)
    e = [x - m for x in xs]
    var = sum(v * v for v in e) / n
    for k in range(1, min(lag, n - 1) + 1):
        var += 2 * (1 - k / (lag + 1)) * sum(e[t] * e[t - k] for t in range(k, n)) / n
    return m / math.sqrt(var / n) if var > 0 else None


def terciles(scores):
    """계열 전체 기준 3분위 경계 (q1, q2): s<=q1 하, s<=q2 중, 그 외 상."""
    s = sorted(scores)
    return s[len(s) // 3 - 1], s[2 * len(s) // 3 - 1]


def daily_stat(rows, key, lag):
    """rows=[(date, value dict)] → 날짜별 평균의 평균·t."""
    by = defaultdict(list)
    for d, v in rows:
        if v.get(key) is not None:
            by[d].append(v[key])
    days = sorted(by)
    xs = [mean(by[d]) for d in days]
    if not xs:
        return {"n": 0, "days": 0, "mean": None, "t": None}
    return {"n": sum(len(by[d]) for d in days), "days": len(days), "mean": mean(xs),
            "t": nw_t(xs, lag) if len(days) >= 2 else None}


def spread_stat(high, low, key, lag):
    """탐색용(판정에 쓰지 않음): 같은 날의 점수 상 − 하 평균 차이. 지수 방향이 상쇄된다."""
    by = {}
    for name, rows in (("h", high), ("l", low)):
        for d, v in rows:
            if v.get(key) is not None:
                by.setdefault(d, {"h": [], "l": []})[name].append(v[key])
    xs = [mean(g["h"]) - mean(g["l"]) for _d, g in sorted(by.items()) if g["h"] and g["l"]]
    return {"days": len(xs), "mean": mean(xs) if xs else None, "t": nw_t(xs, lag)}


def judge(rows_by_tercile, h):
    """6절 판정의 한 보유기간 분: C1~C4 와 음의 신호 여부."""
    key = f"net{h}"
    top = rows_by_tercile["high"]
    st = {k: daily_stat(v, key, h - 1) for k, v in rows_by_tercile.items()}
    hi = st["high"]
    if hi["mean"] is None:
        return {"stats": st, "c1": False, "c2": False, "c3": False, "c4": False, "negative": False}
    days = sorted({d for d, v in top if v.get(key) is not None})
    mid_day = days[len(days) // 2]
    halves = [daily_stat([r for r in top if (r[0] < mid_day) == first], key, h - 1)["mean"] for first in (True, False)]
    kept = sorted((r for r in top if r[1].get(key) is not None), key=lambda r: r[1][key])[:-3]
    ex3 = daily_stat(kept, key, h - 1)["mean"]
    enough = hi["days"] >= MIN_DAYS_FOR_T and hi["t"] is not None
    means = [st[k]["mean"] for k in ("high", "mid", "low")]
    return {
        "stats": st, "halves": halves, "ex_top3": ex3,
        "spread": spread_stat(top, rows_by_tercile["low"], key, h - 1),
        "c1": bool(enough and hi["mean"] > 0 and hi["t"] >= 2),
        "c2": bool(all(m is not None for m in means) and means[0] > means[1] > means[2]),
        "c3": bool(all(x is not None and x > 0 for x in halves)),
        "c4": bool(ex3 is not None and ex3 > 0),
        "negative": bool(enough and hi["mean"] < 0 and hi["t"] <= -2),
    }


def verdict(j5, j20):
    if j5["negative"] or j20["negative"]:
        return "음의 신호"
    if all(j[c] for j in (j5, j20) for c in ("c1", "c2", "c3", "c4")):
        return "선정 신호 있음"
    return "미입증"


# ---------- 입출력 ----------

def build_obs(pit_dir, signals, team_dir):
    obs = []
    for f in sorted(glob.glob(os.path.join(pit_dir, "*.json"))):
        j = json.load(open(f))
        for c in j["candidates"]:
            obs.append({"family": "A", "date": j["date"], "symbol": c["symbol"], "group": c.get("strategy"),
                        "score": c.get("score"), "entry": "open"})
    first, passed = {}, set()
    for line in open(signals):
        r = json.loads(line)
        k = (r["symbol"], r["t"][:10])
        if r["type"] == "passed":
            passed.add(k)
        if k not in first or r["t"] < first[k]["t"]:
            first[k] = r
    for k, r in first.items():
        gate = "passed" if k in passed else (r["gate"] if r["gate"] is not None else r["type"])
        obs.append({"family": "B", "date": k[1], "symbol": k[0], "group": gate, "strategy": r["strategy"],
                    "score": r["score"], "entry": "close"})
    first = {}
    for f in sorted(glob.glob(os.path.join(team_dir, "deliberations_*.jsonl"))):
        for line in open(f):
            r = json.loads(line)
            t = r["decided_at"] if r.get("decided_at") is not None else r["recorded_at"]
            k = (r["symbol"], t[:10])
            if k not in first or t < first[k][0]:
                d = r["decision"] if r.get("decision") is not None else {}
                label = ("buy_approved" if d.get("stance") == "buy" and d.get("approved")
                         else "hold" if d.get("stance") == "hold" and d.get("approved")
                         else "sell" if d.get("stance") == "sell" else "not_approved")
                first[k] = (t, label)
    for k, (_t, label) in first.items():
        obs.append({"family": "C", "date": k[1], "symbol": k[0], "group": label, "score": None, "entry": "close"})
    return obs


def load_bars(path):
    with open(path) as f:
        return {r["date"]: tuple(float(r[k]) for k in ("open", "high", "low", "close")) for r in csv.DictReader(f)}


def fetch_prices(symbols, out_dir, start, pause=0.4):
    import FinanceDataReader as fdr  # 지연 임포트: 계산·검사는 네트워크 라이브러리 없이 돈다
    os.makedirs(out_dir, exist_ok=True)
    fails = 0
    for s in symbols:
        p = os.path.join(out_dir, f"{s}.csv")
        if os.path.exists(p):
            continue
        try:
            df = fdr.DataReader(s, start)
            if df is None or df.empty:
                raise ValueError("empty")
            with open(p, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["date", "open", "high", "low", "close"])
                for d, r in df.iterrows():
                    w.writerow([d.strftime("%Y-%m-%d"), r["Open"], r["High"], r["Low"], r["Close"]])
            fails = 0
        except Exception as e:  # 원천 오류는 종목 단위로 건너뛰고, 연속되면 멈춘다
            fails += 1
            print(f"[실패] {s}: {type(e).__name__}")
            if fails >= 5:
                raise SystemExit("연속 실패 5회 — 중단")
        time.sleep(pause)


def adjust_bench(bench, adj_csv):
    """공개 일봉의 069500 은 분배금 미반영이다 — 초과수익 원장의 KIS 수정 종가 비율로 OHLC 를 맞춘다.

    보정 자료의 기간 [첫날, 끝날] 안에서 두 원천의 거래일이 다르면 달력이 조용히 줄어 보유 기간이 밀린다 → 실패시킨다.
    보정 기간 밖(앞·뒤)의 공개 일봉은 버린다.
    """
    with open(adj_csv) as f:
        rows = [(r["date"], float(r["close"])) for r in csv.DictReader(f)]
    adj = dict(rows)
    if len(adj) != len(rows) or any(not (math.isfinite(c) and c > 0) for c in adj.values()):
        raise SystemExit("벤치마크 보정 자료에 중복 날짜 또는 비양수·비유한 종가가 있다")
    lo, hi = min(adj), max(adj)
    inside = {d for d in bench if lo <= d <= hi}
    if inside != set(adj):
        raise SystemExit(f"벤치마크 거래일 불일치: 공개 일봉만 {sorted(inside - set(adj))[:5]}, 보정 자료만 {sorted(set(adj) - inside)[:5]}")
    return {d: tuple(x * adj[d] / bench[d][3] for x in bench[d]) for d in adj}


def top_n_per_day(rows, n=3):
    """날짜별 점수 상위 n (동점은 종목코드 순) — 사전 등록 참고 집계."""
    by = defaultdict(list)
    for o, v in rows:
        if o["score"] is not None:
            by[o["date"]].append((o, v))
    return [r for rs in by.values() for r in sorted(rs, key=lambda r: (-r[0]["score"], r[0]["symbol"]))[:n]]


def report(obs, price_dir, bench_adjust):
    bench = adjust_bench(load_bars(os.path.join(price_dir, f"{BENCH}.csv")), bench_adjust)
    cal = sorted(bench)
    cache, excluded, rows = {}, defaultdict(lambda: defaultdict(int)), []
    for o in obs:
        s = o["symbol"]
        if s not in cache:
            p = os.path.join(price_dir, f"{s}.csv")
            cache[s] = load_bars(p) if os.path.exists(p) else {}
        v = {}
        for h in HORIZONS:
            res, why = forward(cache[s], cal, o["date"], o["entry"], h)
            if res is None:
                excluded[o["family"]][f"h{h}:{why}"] += 1
                continue
            ret, clip, mae, clip_day = res
            b, _ = forward(bench, cal, o["date"], o["entry"], h)
            base = bench[o["date"]][0] if o["entry"] == "open" else bench[o["date"]][3]
            v.update({f"ret{h}": ret, f"net{h}": ret - b[0] - COST, f"mae{h}": mae,
                      f"clipnet{h}": clip - (bench[clip_day][3] / base - 1) - COST})
        rows.append((o, v))

    def table(sel, keyfn):
        groups = defaultdict(list)
        for o, v in sel:
            groups[keyfn(o)].append((o["date"], v))
        return {str(g): {f"h{h}": {**daily_stat(rs, f"net{h}", h - 1),
                                   "abs": daily_stat(rs, f"ret{h}", h - 1)["mean"],
                                   "clip": daily_stat(rs, f"clipnet{h}", h - 1)["mean"]} for h in HORIZONS}
                for g, rs in sorted(groups.items(), key=lambda kv: str(kv[0]))}

    with open(bench_adjust, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    out = {"cost": COST, "bench": {"adjusted_by": os.path.basename(bench_adjust), "sha256": digest, "rows": len(cal),
                                   "first": cal[0], "last": cal[-1]},
           "excluded": {k: dict(v) for k, v in excluded.items()}, "primary": {}, "tables": {}}
    for name, sel in (("A-sepa", [r for r in rows if r[0]["family"] == "A" and r[0]["group"] == "sepa_trend"]),
                      ("B-all", [r for r in rows if r[0]["family"] == "B"])):
        scored = [r for r in sel if r[0]["score"] is not None]
        q1, q2 = terciles([r[0]["score"] for r in scored])
        tier = lambda o: "low" if o["score"] <= q1 else "mid" if o["score"] <= q2 else "high"  # noqa: E731
        by = {k: [(o["date"], v) for o, v in scored if tier(o) == k] for k in ("high", "mid", "low")}
        j = {h: judge(by, h) for h in (5, 20)}
        spreads = {f"h{h}": spread_stat(by["high"], by["low"], f"net{h}", h - 1) for h in HORIZONS}
        out["primary"][name] = {"n": len(sel), "scored": len(scored), "q1": q1, "q2": q2,
                                "verdict": verdict(j[5], j[20]), "h5": j[5], "h20": j[20], "spread": spreads,
                                "all": table(sel, lambda o: "all")["all"],
                                "tercile": table(scored, tier),
                                "mae5_hit": {str(x): mean(1.0 if v["mae5"] <= -x else 0.0 for _o, v in sel if "mae5" in v)
                                             for x in (0.035, 0.05, 0.08)}}
    fam = lambda f: [r for r in rows if r[0]["family"] == f]  # noqa: E731
    out["tables"] = {
        "A_by_strategy": table(fam("A"), lambda o: o["group"]),
        "A_sepa_by_month": table([r for r in fam("A") if r[0]["group"] == "sepa_trend"], lambda o: o["date"][:7]),
        "B_by_gate": table(fam("B"), lambda o: o["group"] if o["group"] in ("passed", "G1_regime") else "other"),
        "B_by_strategy": table(fam("B"), lambda o: o.get("strategy")),
        "B_by_month": table(fam("B"), lambda o: o["date"][:7]),
        "C_by_decision": table(fam("C"), lambda o: o["group"]),
        "daily_top3": {**table(top_n_per_day([r for r in fam("A") if r[0]["group"] == "sepa_trend"]), lambda o: "A-sepa"),
                       **table(top_n_per_day(fam("B")), lambda o: "B-all")},
    }
    return out


def export_signals(out_path):
    """signal_events 전체를 읽기 전용으로 내보낸다. 날짜는 KST 기준."""
    import asyncio

    import asyncpg  # 지연 임포트: 계산·검사는 DB 라이브러리 없이 돈다

    async def run():
        conn = await asyncpg.connect(os.environ["DATABASE_URL"], timeout=15)
        try:
            await conn.execute("SET default_transaction_read_only = on")
            return await conn.fetch(
                "select event_time at time zone 'Asia/Seoul' as t, symbol, strategy, score, event_type, block_gate "
                "from signal_events where side = 'buy' order by id")
        finally:
            await conn.close()

    rows = asyncio.run(run())
    with open(out_path, "w") as f:
        for r in rows:
            f.write(json.dumps({"t": r["t"].isoformat(), "symbol": r["symbol"], "strategy": r["strategy"],
                                "score": r["score"], "type": r["event_type"], "gate": r["block_gate"]}) + "\n")
    print(len(rows), "행")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("signals").add_argument("--out", required=True)
    a = sub.add_parser("obs")
    a.add_argument("--pit-dir", required=True)
    a.add_argument("--signals", required=True)
    a.add_argument("--team-dir", required=True)
    a.add_argument("--out", required=True)
    b = sub.add_parser("prices")
    b.add_argument("--obs", required=True)
    b.add_argument("--out-dir", required=True)
    b.add_argument("--start", default="2026-02-01")
    c = sub.add_parser("report")
    c.add_argument("--obs", required=True)
    c.add_argument("--price-dir", required=True)
    c.add_argument("--out", required=True)
    c.add_argument("--bench-adjust", required=True, help="KIS 수정 종가 CSV(date, close) — 벤치마크 분배금 보정(필수)")
    args = ap.parse_args()
    if args.cmd == "signals":
        export_signals(args.out)
        return
    if args.cmd == "obs":
        obs = build_obs(args.pit_dir, args.signals, args.team_dir)
        with open(args.out, "w") as f:
            f.writelines(json.dumps(o, ensure_ascii=False) + "\n" for o in obs)
        print(len(obs), "관측")
        return
    obs = [json.loads(line) for line in open(args.obs)]
    if args.cmd == "prices":
        fetch_prices([BENCH] + sorted({o["symbol"] for o in obs}), args.out_dir, args.start)
    else:
        out = report(obs, args.price_dir, args.bench_adjust)
        with open(args.out, "w") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        for name, p in out["primary"].items():
            print(name, p["verdict"], {h: {k: p[h][k] for k in ("c1", "c2", "c3", "c4", "negative")} for h in ("h5", "h20")})


if __name__ == "__main__":
    main()
