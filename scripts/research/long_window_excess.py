#!/usr/bin/env python3
"""장기 구간(2019-06~2026-09) SEPA 단독 백테스트 — 포지션별 KODEX200 초과수익 (연구 전용, 2026-09-30).

질문: SEPA 계열이 **어느 시장 국면에서든** 포지션(왕복) 단위로 KODEX200 을 이기는가.
문서: docs/research/long-window-sepa-vs-kodex200-2026-09.md

- 백테스터(`scripts/backtest_strategies.py`)·유효 설정 builder·`ab_exit_policy.make_config` 를 그대로 쓴다
  (동작 변경 없음). 이 파일은 셀 실행·원자료 저장과 지표 계산만 한다.
- 지표 정의는 실거래 원장 `src/analytics/excess_return.position_row` 와 같다:
  r = 순손익 / 매수원금(가격×수량), b = Σ (청산수량/총수량) × (종가[청산일]/종가[진입일] − 1),
  초과 = r − b, 초과(원) = 순손익 − 매수원금×b, 손절 클립 = max(r, −진입손절%) − b.
  벤치 날짜가 없으면 null (0 으로 채우지 않는다).
- 국면: 진입일 **전날까지** 확정된 KOSPI 종가 vs MA50·MA200, 20일 연율 변동성(σ×√252) > 25%.

KIS 무접촉. OHLCV 는 pykrx(백테스터 UniverseManager 경로), KODEX200 은 FDR 069500, 지수는 Yahoo ^KS11.

  prep  : 유니버스 OHLCV·레짐 캐시 생성(다운로드) + 벤치마크 CSV 저장
  cell  : 셀 1개 실행(--offline, 캐시만) → <out>/<cell>/ 원자료·포지션 행 저장
  report: 저장된 셀 → summary.json + 표(markdown)
"""

import argparse
import csv
import json
import math
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

END_DATE = "2026-09-26"
MONTHS = 89                 # 백테스터 start = end − months×30일 → 2019-06-05 (요청 06-01 대비 2거래일 늦음)
VOL_HIGH = 0.25
OUT_DEFAULT = ROOT / "results" / "long_window_sepa_2026-09"
END_REASON = "백테스트 종료 청산"


# ── 지표 (순수 함수 — tests/test_long_window_excess.py) ─────────────────────────

def position_rows(trades, bench: dict) -> list:
    """백테스터 trades(BUY/SELL 이벤트) → 포지션 행. bench = {'YYYY-MM-DD': 종가}."""
    open_, out = {}, []
    for t in trades:
        if t.side == "BUY":
            open_[t.symbol] = {"symbol": t.symbol, "entry_date": t.date, "quantity": t.quantity,
                               "cost": t.price * t.quantity, "buy_fee": t.fee, "proceeds": 0.0,
                               "stop_pct": t.stop_pct, "exits": [], "reasons": []}
            continue
        p = open_.get(t.symbol)
        if p is None:
            continue
        p["exits"].append((t.date, t.quantity))
        p["proceeds"] += t.amount - t.fee
        p["reasons"].append(t.reason)
        if sum(q for _, q in p["exits"]) >= p["quantity"]:
            out.append(finish(open_.pop(t.symbol), bench))
    return out


def finish(p: dict, bench: dict) -> dict:
    net = p["proceeds"] - p["cost"] - p["buy_fee"]          # = ResultAnalyzer.positions() pnl
    r = net / p["cost"] if p["cost"] > 0 else None
    b, why = None, None
    e0 = bench.get(p["entry_date"])
    if e0 is None:
        why = f"bench_date_missing: entry {p['entry_date']}"
    else:
        total = sum(q for _, q in p["exits"])
        acc = 0.0
        for d, q in p["exits"]:
            if d not in bench:
                why = f"bench_date_missing: exit {d}"
                break
            acc += q / total * (bench[d] / e0 - 1)
        else:
            b = acc
    x = r - b if r is not None and b is not None else None
    rc = max(r, -p["stop_pct"] / 100) if r is not None and p["stop_pct"] > 0 else r
    return {"symbol": p["symbol"], "entry_date": p["entry_date"], "last_exit_date": p["exits"][-1][0],
            "entry_cost": p["cost"], "net_pnl": net, "net_return": r, "bench_return": b,
            "excess_return": x, "excess_krw": net - p["cost"] * b if b is not None else None,
            "stop_pct": p["stop_pct"], "clipped_excess_return": rc - b if b is not None else None,
            "bench_missing_reason": why, "forced_end": END_REASON in p["reasons"],
            "exit_dates": [d for d, _ in p["exits"]],
            "reasons": p["reasons"]}


def metrics(rows: list) -> dict:
    """원장 `_metrics` 와 같은 지표 + 상위 3건(초과수익 기준) 제외 평균."""
    cov = [r for r in rows if r["excess_return"] is not None]
    xs = [r["excess_return"] for r in cov]
    n = len(xs)
    if n == 0:
        return {"n": len(rows), "bench_covered": 0}
    sd = statistics.stdev(xs) if n >= 2 else 0.0
    krw = [r["excess_krw"] for r in cov]
    rest = sorted(xs, reverse=True)[3:]
    return {
        "n": len(rows), "bench_covered": n,
        "mean_excess": sum(xs) / n, "median_excess": statistics.median(xs),
        "t_excess": (sum(xs) / n) / (sd / math.sqrt(n)) if sd > 0 else None,
        "beat_rate": sum(1 for x in xs if x > 0) / n,
        "mean_clipped_excess": sum(r["clipped_excess_return"] for r in cov) / n,
        "mean_net_return": sum(r["net_return"] for r in cov) / n,
        "mean_bench_return": sum(r["bench_return"] for r in cov) / n,
        "mean_excess_excl_top3": sum(rest) / len(rest) if rest else None,
        "excess_krw_sum": sum(krw),
        "excess_krw_excl_top3": sum(krw) - sum(sorted(krw, reverse=True)[:3]),
        "net_pnl_sum": sum(r["net_pnl"] for r in cov),
    }


def regime_labels(kospi_closes: dict) -> dict:
    """{날짜: 종가} → {날짜: 그날 종가까지 확정된 상태}. 진입 분류는 진입일 **이전** 마지막 날짜를 쓴다."""
    ds = sorted(kospi_closes)
    cs = [kospi_closes[d] for d in ds]
    out = {}
    for i, d in enumerate(ds):
        if i < 200:
            continue
        ma50, ma200 = sum(cs[i - 49:i + 1]) / 50, sum(cs[i - 199:i + 1]) / 200
        rets = [cs[j] / cs[j - 1] - 1 for j in range(i - 19, i + 1)]
        vol = statistics.stdev(rets) * math.sqrt(252)
        out[d] = {"ma200": "MA200↑" if cs[i] > ma200 else "MA200↓",
                  "ma50": "MA50↑" if cs[i] > ma50 else "MA50↓",
                  "vol": "vol>25%" if vol > VOL_HIGH else "vol≤25%", "vol20": vol}
    return out


def state_before(labels: dict, day: str):
    prev = [d for d in labels if d < day]
    return labels[max(prev)] if prev else None


# ── 입출력 ────────────────────────────────────────────────────────────────────

def read_closes(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return {r["date"]: float(r["close"]) for r in csv.DictReader(f)}


def write_closes(path: Path, series, source: str):
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "close", "source"])
        for d, c in series.items():
            if c == c and c > 0:            # NaN·0 은 쓰지 않는다 (결측 그대로)
                w.writerow([str(d)[:10], float(c), source])


def g1_universe() -> list:
    import backtest_strategies as bt
    return sorted(p.name.split("_")[1] for p in bt.CACHE_DIR.glob("g1_*_2019-01-01.pkl"))


def universe(name: str) -> list:
    import backtest_strategies as bt
    if name == "g1":
        return g1_universe()
    return bt.DEFAULT_UNIVERSE["KOSPI"][:40] + bt.DEFAULT_UNIVERSE["KOSDAQ"][:20]


def dates():
    from datetime import datetime, timedelta
    end = datetime.strptime(END_DATE, "%Y-%m-%d")
    start = end - timedelta(days=MONTHS * 30)
    warm = start - timedelta(days=400)
    return warm.strftime("%Y%m%d"), start.strftime("%Y%m%d"), end.strftime("%Y%m%d")


def prep(args):
    import FinanceDataReader as fdr
    import backtest_strategies as bt
    from src.utils.kospi_benchmark import load_kospi_history, proxy_outlier
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    warm, start, end = dates()
    tickers = universe(args.universe)
    um = bt.UniverseManager(size=len(tickers), use_cache=True, offline=False)
    um.tickers = tickers
    um.load_ohlcv(warm, end)
    reg = bt.MarketRegime(offline=False)
    reg.load(warm, end)
    # 거래일 기준 종목: 구간 전체 봉이 가장 많은 종목 (백테스터는 universe.tickers[0] 의 인덱스로 거래일을 만든다)
    lens = {t: len(df.loc[start:end]) for t, df in um.ohlcv.items()}
    ref = max(lens, key=lens.get)
    ordered = [ref] + [t for t in tickers if t != ref and t in um.ohlcv]
    k = fdr.DataReader("069500", "2018-01-01", END_DATE)
    write_closes(out / "kodex200_069500_fdr.csv", k["Close"], "FDR:069500")
    kc = [float(c) for c in k["Close"]]
    kd = [str(d)[:10] for d in k.index]
    ks, src = load_kospi_history("2018-01-01", END_DATE)
    write_closes(out / "kospi_index.csv", ks["Close"], src)
    meta = {"universe": args.universe, "tickers": ordered, "missing": um.missing_tickers,
            "trading_day_ref": ref, "ref_bars": lens[ref], "bars": lens,
            "kodex200_outlier": proxy_outlier(kc, kd, len(kc)),
            "kodex200_outliers_all": [f"{kd[i]} {kc[i] / kc[i - 1] - 1:+.1%}" for i in range(1, len(kc))
                                      if abs(kc[i] / kc[i - 1] - 1) > 0.12],
            "kospi_source": src, "regime_source": reg.source, "period": [warm, start, end]}
    (out / f"prep_{args.universe}.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in meta.items() if k not in ("tickers", "bars")}, ensure_ascii=False))


ATR_STOP_MIN, ATR_STOP_MAX = 4.0, 8.0   # 운영 ATR 동적 손절 범위(CLAUDE.md) — current-engine-b §1

def run_cell(args):
    import argparse as ap
    import contextlib
    import resource
    import ab_exit_policy as ab
    import backtest_strategies as bt
    from src.utils.config import effective_config_hash, load_effective_config
    out = Path(args.out)
    meta = json.loads((out / f"prep_{args.universe}.json").read_text(encoding="utf-8"))
    exit_policy, holding = args.cell.split("/")
    effective = load_effective_config(None)
    ns = ap.Namespace(entry_stop_mode="live_policy", slot_policy="live_weighted",
                      offline=True, end_date=END_DATE)
    cfg = ab.make_config(MONTHS, exit_policy, holding, "risk", "sepa", len(meta["tickers"]), ns, effective)
    engine = bt.BacktestEngine(cfg)
    if args.stop == "atr_entry":
        # 진입 전 확정 봉 ATR×2 를 4~8% 로 클램프해 진입 시 한 번 고정 (current-engine-b §1). 이후는 live_policy 와 같다.
        # 백테스터 파일은 운영 BacktestGate 가 불러 쓰므로 고치지 않고 이 인스턴스만 바꾼다.
        engine._entry_stop_pct = lambda strategy, atr_pct: (
            5.0 if atr_pct is None else max(ATR_STOP_MIN, min(ATR_STOP_MAX, atr_pct * cfg.atr_multiplier)))
    engine.universe.tickers = meta["tickers"]
    engine.universe.names = {t: t for t in meta["tickers"]}
    engine.universe.build_universe = lambda ref: None
    d = out / f"{args.universe}_{args.cell.replace('/', '_')}{'' if args.stop == 'fixed' else '_' + args.stop}"
    d.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    with open(d / "run.log", "w", encoding="utf-8") as log, contextlib.redirect_stdout(log):
        m = engine.run(save_results=False)
    elapsed = time.time() - t0
    if not m:
        raise SystemExit("백테스트 결과 없음 — run.log 확인")
    bench = read_closes(out / "kodex200_069500_fdr.csv")
    rows = position_rows(engine.trades, bench)
    dumps = lambda o: json.dumps(o, ensure_ascii=False, indent=0, default=str)  # noqa: E731
    (d / "fills.json").write_text(dumps([vars(t) for t in engine.trades]), encoding="utf-8")
    (d / "equity.json").write_text(dumps(engine.equity_curve), encoding="utf-8")
    (d / "positions.json").write_text(dumps(rows), encoding="utf-8")
    info = {"cell": args.cell, "universe": args.universe, "stop": args.stop, "elapsed_s": round(elapsed, 1),
            "max_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
            "config_hash": effective_config_hash(effective), "calc_version": bt.CALC_VERSION,
            "sizing": cfg.sizing, "exit_policy": cfg.exit_policy, "entry_stop_mode": cfg.entry_stop_mode,
            "slot_policy": cfg.slot_policy, "strategies": cfg.strategies, "allocation": cfg.allocation,
            "missing_tickers": engine.universe.missing_tickers, "regime_source": engine.regime.source,
            "metrics": {k: v for k, v in m.items() if k != "position_level"},
            "position_level": {k: v for k, v in m["position_level"].items() if k != "exit_reasons"},
            "exit_reasons": m["position_level"].get("exit_reasons")}
    (d / "cell.json").write_text(json.dumps(info, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(json.dumps({k: info[k] for k in ("cell", "elapsed_s", "max_rss_mb")}, ensure_ascii=False),
          json.dumps(metrics(rows), ensure_ascii=False, default=str))


# ── 보고 ──────────────────────────────────────────────────────────────────────

def _fmt(m: dict) -> str:
    if not m.get("bench_covered"):
        return f"| {m.get('n', 0)} | — | — | — | — | — | — | — |"
    t = m["t_excess"]
    return (f"| {m['n']} | {m['mean_excess'] * 100:+.2f}% | {m['median_excess'] * 100:+.2f}% "
            f"| {'—' if t is None else f'{t:+.2f}'} | {m['beat_rate'] * 100:.0f}% "
            f"| {m['mean_clipped_excess'] * 100:+.2f}% "
            f"| {'—' if m['mean_excess_excl_top3'] is None else f'{m['mean_excess_excl_top3'] * 100:+.2f}%'} "
            f"| {m['mean_net_return'] * 100:+.2f}% / {m['mean_bench_return'] * 100:+.2f}% |")


HDR = ("| n | 평균 초과 | 중앙 초과 | t | 이긴 비율 | 클립 평균 초과 | top3 제외 평균 | 봇 / 벤치 평균 |\n"
       "|---|---|---|---|---|---|---|---|")


def yearly_portfolio(equity: list, bench: dict) -> dict:
    """연도별 계좌 수익률 vs 같은 첫/끝 거래일의 KODEX200 수익률 (포트폴리오 단위 참고)."""
    by = {}
    prev = None
    for d, e in equity:
        y = d[:4]
        if y not in by:
            by[y] = {"start": prev or (d, e), "end": (d, e)}
        by[y]["end"] = (d, e)
        prev = (d, e)
    out = {}
    for y, v in by.items():
        (d0, e0), (d1, e1) = v["start"], v["end"]
        b = bench[d1] / bench[d0] - 1 if d0 in bench and d1 in bench else None
        out[y] = {"from": d0, "to": d1, "account": e1 / e0 - 1, "kodex200": b,
                  "diff": (e1 / e0 - 1 - b) if b is not None else None}
    return out


def report(args):
    out = Path(args.out)
    bench = read_closes(out / "kodex200_069500_fdr.csv")
    kospi = read_closes(out / "kospi_index.csv")
    labels = regime_labels(kospi)
    summary, md = {}, []
    for d in sorted(p for p in out.iterdir() if p.is_dir() and (p / "positions.json").exists()):
        info = json.loads((d / "cell.json").read_text(encoding="utf-8"))
        rows = json.loads((d / "positions.json").read_text(encoding="utf-8"))
        equity = json.loads((d / "equity.json").read_text(encoding="utf-8"))
        for r in rows:
            s = state_before(labels, r["entry_date"])
            r["state"] = s
        groups = {"전체": rows, "전체(기간종료 강제청산 제외)": [r for r in rows if not r["forced_end"]]}
        for r in rows:
            groups.setdefault(f"연도 {r['entry_date'][:4]}", []).append(r)
        for r in rows:
            s = r["state"]
            key = "국면 미상" if s is None else f"{s['ma200']} / {s['ma50']} / {s['vol']}"
            groups.setdefault(f"국면 {key}", []).append(r)
            if s is not None:
                groups.setdefault(f"축 {s['ma200']}", []).append(r)
                groups.setdefault(f"축 {s['vol']}", []).append(r)
        # 069500 오염 표시: |일수익|>12% 벤치 봉을 진입·청산 기준 종가로 쓴 포지션
        # (b 는 진입·청산일 종가만 쓰므로 그 사이를 지나기만 한 포지션은 영향 없음)
        bad = {x.split()[0] for x in json.loads((out / f"prep_{info['universe']}.json").read_text(
            encoding="utf-8"))["kodex200_outliers_all"]}
        tainted = [r for r in rows if bad & {r["entry_date"], *r["exit_dates"]}]
        groups["전체(069500 이상봉 사용 포지션 제외)"] = [r for r in rows if r not in tainted]
        # 보조 벤치: 같은 정의, KODEX200 대신 KOSPI 지수(Yahoo ^KS11) 종가
        from types import SimpleNamespace
        fills = json.loads((d / "fills.json").read_text(encoding="utf-8"))
        groups["전체(보조 벤치 = KOSPI 지수)"] = position_rows([SimpleNamespace(**f) for f in fills], kospi)
        name = d.name
        cell = {"info": info, "groups": {k: metrics(v) for k, v in sorted(groups.items())},
                "kodex200_tainted_positions": len(tainted),
                "yearly_portfolio": yearly_portfolio(equity, bench),
                "top3_excess": sorted(({"symbol": r["symbol"], "entry": r["entry_date"],
                                        "excess": r["excess_return"]} for r in rows
                                       if r["excess_return"] is not None), key=lambda z: -z["excess"])[:3]}
        e0, e1 = equity[0], equity[-1]
        # 평균 노출(근사): Σ 매수원금×보유 달력일 ÷ (평균 자산×전체 달력일) — 부분 청산은 무시해 과대 추정 쪽
        from datetime import date as _d
        span = (_d.fromisoformat(e1[0]) - _d.fromisoformat(e0[0])).days
        avg_eq = sum(e for _, e in equity) / len(equity)
        cell["avg_exposure_approx"] = sum(
            r["entry_cost"] * (_d.fromisoformat(r["last_exit_date"]) - _d.fromisoformat(r["entry_date"])).days
            for r in rows) / (avg_eq * span)
        cell["period_total"] = {"from": e0[0], "to": e1[0],
                                "account": info["metrics"]["total_return_pct"] / 100,
                                "kodex200": bench[e1[0]] / bench[e0[0]] - 1
                                if e0[0] in bench and e1[0] in bench else None}
        summary[name] = cell
        md.append(f"\n### {name} ({info['elapsed_s']}s, RSS {info['max_rss_mb']}MB)\n")
        pt = cell["period_total"]
        md.append(f"계좌 {pt['account'] * 100:+.1f}% vs KODEX200 {pt['kodex200'] * 100:+.1f}% "
                  f"({pt['from']}~{pt['to']}), MDD {info['metrics']['mdd_pct']:.1f}%, "
                  f"보유 중앙 {info['position_level']['median_holding_days']:.0f}일, "
                  f"연회전 {info['position_level']['turnover_annual']:.1f}x, "
                  f"기대값 {info['position_level']['expectancy_r']:+.2f}R, "
                  f"평균 노출(근사) {cell['avg_exposure_approx'] * 100:.0f}%, "
                  f"069500 이상봉(|일수익|>12%) 사용 포지션 {len(tainted)}\n")
        md.append("| 구분 " + HDR.replace("\n", "\n|---"))
        for k, m in cell["groups"].items():
            md.append(f"| {k} " + _fmt(m))
        md.append("\n| 연도 | 계좌 | KODEX200 | 차 |\n|---|---|---|---|")
        for y, v in cell["yearly_portfolio"].items():
            b = v["kodex200"]
            md.append(f"| {y} | {v['account'] * 100:+.1f}% | {'—' if b is None else f'{b * 100:+.1f}%'} "
                      f"| {'—' if v['diff'] is None else f'{v['diff'] * 100:+.1f}pp'} |")
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=str),
                                      encoding="utf-8")
    (out / "tables.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("phase", choices=["prep", "cell", "report"])
    ap.add_argument("--universe", choices=["g1", "default60"], default="g1")
    ap.add_argument("--cell", default="ladder/current", help="<ladder|channel>/<current|extended|none>")
    ap.add_argument("--out", default=str(OUT_DEFAULT))
    ap.add_argument("--stop", choices=["fixed", "atr_entry"], default="fixed",
                    help="fixed=운영 live_policy 고정 SL, atr_entry=진입 전 봉 ATR×2 (4~8%%) 진입 시 고정")
    args = ap.parse_args()
    {"prep": prep, "cell": run_cell, "report": report}[args.phase](args)


if __name__ == "__main__":
    main()
