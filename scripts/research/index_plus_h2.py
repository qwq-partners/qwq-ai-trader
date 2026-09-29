#!/usr/bin/env python3
"""H2 — 대형주 모멘텀 기울기 (사전 등록: docs/research/index-plus-program-2026-09.md §3-2).

월말 t 종가까지의 자료로 유니버스(60일 평균 거래대금 상위 U)·신호(12-1 모멘텀)를 정하고,
다음 거래일 시가에 상위 N 종목 동일 비중으로 체결, 다음 월말 신호 뒤 다음 거래일 시가에 교체한다.
비용은 교체분(새로 사는 종목·파는 종목)에만 부과. 연구 전용 — src/·운영 무변경, KIS 무접촉.

  prep  : 후보 목록(FDR 현재 상장 — 시점 목록 불가)·종목 OHLCV(Yahoo)·교차 확인(Naver)·벤치 캐시
  run   : 캐시 → 종목별 월 특징(종목 하나씩 읽고 버림) → 본 결과·강건성 → results/index_plus_h2/
  report: summary.json → tables.md 재생성

실행: nice -n 19 venv/bin/python scripts/research/index_plus_h2.py prep|run|report
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import re
import statistics
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
CACHE = Path.home() / ".cache/ai_trader/backtest/h2_ohlcv"
OUT_DEFAULT = ROOT / "results/index_plus_h2"
START, END = "2008-06-01", "2026-09-26"
FIRST_HOLD = "2010-01-01"                  # 첫 보유 월(신호는 2009-12 월말)
REF_START = "2019-06-01"

BUY_COST = 0.00014 + 0.0010               # 매수 수수료 + 슬리피지
SELL_COST = 0.0025 + 0.0010               # 매도 수수료·거래세(보수 근사) + 슬리피지
MAIN = {"top_n": 20, "universe_n": 100, "mom": "mom12"}
ROBUST = {"상위10": {"top_n": 10}, "상위30": {"top_n": 30}, "6-1 모멘텀": {"mom": "mom6"},
          "유니버스50": {"universe_n": 50}, "유니버스200": {"universe_n": 200}}
# 진단(사전 등록 밖, 보고만): 모멘텀 없이 유니버스 전체 동일 비중 — 손실이 선택 때문인지 동일 비중 때문인지 가른다
DIAG = {"진단: 유니버스100 전체 동일비중(모멘텀 무관)": {"top_n": 100}}
MIN_HIST = 250
MIN_AVAILABLE_MB = 800
DEADLINE = pd.Timestamp("2026-09-30 13:00")


# ── 제외 규칙 (사전 등록: 우선주·ETF·ETN·스팩·리츠) ─────────────────────────────

def exclusion_reason(code: str, name: str):
    """제외 사유 문자열, 포함이면 None. 우선주 = 코드 끝자리가 0 이 아님(KRX 단축코드 관례)."""
    if not code.endswith("0"):
        return "우선주(코드 끝자리≠0)"
    if re.search(r"ETF|ETN", name):
        return "ETF/ETN"
    if "스팩" in name or re.search(r"기업인수목적", name):
        return "스팩"
    if ("리츠" in name and not name.startswith("메리츠")) or "REIT" in name.upper():
        return "리츠"
    return None


# ── 월 특징 (순수 함수 — tests/test_index_plus_h2.py) ──────────────────────────

def month_ends(calendar: pd.DatetimeIndex) -> np.ndarray:
    """달의 마지막 거래일 위치. 달력의 마지막 날은 다음 날이 없으므로 월말로 치지 않는다."""
    m = calendar.month
    return np.where(m[:-1] != m[1:])[0]


def features(daily: pd.DataFrame, calendar: pd.DatetimeIndex, me: np.ndarray, turnover=None) -> pd.DataFrame:
    """종목 일봉(open/close/volume) → 월말 k 행.

    신호 열(hist·has252·turn60·traded_t·mom12·mom6)은 t=calendar[me[k]] **까지만** 본다.
    체결 열(open_exec)은 다음 거래일 시가(거래량 0·결측이면 NaN), last_close 는 t 까지 마지막 종가.
    turnover: 일별 거래대금(원) — 주면 그것으로, 없으면 종가×거래량. (수정주가×원 거래량은 분할 전 거래대금을 낮춘다)
    """
    c = daily["close"].reindex(calendar)
    v = daily["volume"].reindex(calendar).fillna(0.0)
    o = daily["open"].reindex(calendar)
    valid = c.notna() & (c > 0)
    cf = c.where(valid).ffill()
    tv = c.where(valid).fillna(0.0) * v if turnover is None else turnover.reindex(calendar).fillna(0.0)
    turn = tv.rolling(60, min_periods=60).mean()
    hist = valid.cumsum()
    traded = valid & (v > 0)
    ok_open = traded & o.notna() & (o > 0)
    cfv, n = cf.to_numpy(), len(calendar)

    def at(i):
        return cfv[i] if 0 <= i < n else np.nan

    rows = []
    for i in me:
        j = i + 1
        rows.append({
            "hist": float(hist.iloc[i]), "has252": bool(i - 252 >= 0 and not np.isnan(at(i - 252))),
            "turn60": float(turn.iloc[i]), "traded_t": bool(traded.iloc[i]),
            "mom12": at(i - 21) / at(i - 252) - 1, "mom6": at(i - 21) / at(i - 126) - 1,
            "last_close": at(i),
            "open_exec": float(o.iloc[j]) if j < n and ok_open.iloc[j] else np.nan,
        })
    return pd.DataFrame(rows, index=calendar[me])


SIGNAL_COLS = ["hist", "has252", "turn60", "traded_t", "mom12", "mom6"]


def select(sig: pd.DataFrame, *, top_n: int, universe_n: int, mom: str) -> list:
    """한 월말의 신호 열(index=종목) → 상위 종목. 동률은 종목코드 순."""
    s = sig[(sig["hist"] >= MIN_HIST) & sig["has252"] & sig["traded_t"] & (sig["turn60"] > 0)
            & sig[mom].notna() & np.isfinite(sig[mom])]
    s = s.reset_index(names="code")
    uni = s.sort_values(["turn60", "code"], ascending=[False, True]).head(universe_n)
    return list(uni.sort_values([mom, "code"], ascending=[False, True]).head(top_n)["code"])


def holdings(panel: dict, *, top_n: int, universe_n: int, mom: str) -> list:
    """월 k 마다 실제 보유(set). 새 편입은 다음 거래일 시가가 있어야 한다(거래정지·결측은 그달 제외),
    이미 보유하던 종목이 다시 뽑히면 시가 유무와 무관하게 계속 보유(거래 불필요)."""
    out, prev = [], set()
    for k in range(len(panel["open_exec"])):
        sig = pd.DataFrame({c: panel[c].iloc[k] for c in SIGNAL_COLS})
        sel = select(sig, top_n=top_n, universe_n=universe_n, mom=mom)
        oe = panel["open_exec"].iloc[k]
        held = {s for s in sel if s in prev or not math.isnan(oe[s])}
        out.append(held)
        prev = held
    return out


def price_at(panel: dict, k: int, s: str):
    """월말 k 뒤 체결가: 다음 거래일 시가, 없으면(정지) t 까지 마지막 종가. 반환 (가격, 대체 여부)."""
    p = panel["open_exec"].iloc[k][s]
    if not math.isnan(p):
        return p, False
    return panel["last_close"].iloc[k][s], True


def positions(panel: dict, held: list, bench_exec: pd.Series, first_k: int, last_k: int) -> list:
    """보유 월 k(first_k ≤ k < last_k): 체결 k → k+1. 비용은 새로 산 종목(매수)·다음 달 빠지는 종목(매도)만."""
    rows, dates = [], panel["open_exec"].index
    for k in range(first_k, last_k):
        prev = held[k - 1] if k > 0 else set()
        nxt = held[k + 1] if k + 1 < last_k else set()        # 마지막 달은 청산 매도로 친다(보수적)
        b = bench_exec.iloc[k + 1] / bench_exec.iloc[k] - 1
        for s in sorted(held[k]):
            p0, f0 = price_at(panel, k, s)
            p1, f1 = price_at(panel, k + 1, s)
            bought, sold = s not in prev, s not in nxt
            r = p1 * (1 - SELL_COST * sold) / (p0 * (1 + BUY_COST * bought)) - 1
            rows.append({"month": str(dates[k].date()), "k": k, "symbol": s, "entry_px": p0, "exit_px": p1,
                         "bought": bought, "sold": sold, "gross_return": p1 / p0 - 1, "net_return": r,
                         "bench_return": b, "excess_return": r - b, "px_fallback": f0 or f1})
    return rows


# ── 지표 ─────────────────────────────────────────────────────────────────────

def pos_metrics(xs: list) -> dict:
    n = len(xs)
    if n < 2:
        return {"n": n}
    m, sd = sum(xs) / n, statistics.stdev(xs)
    rest = sorted(xs, reverse=True)[3:]
    return {"n": n, "mean_excess": m, "t_excess": m / (sd / math.sqrt(n)) if sd > 0 else None,
            "median_excess": statistics.median(xs), "beat_rate": sum(x > 0 for x in xs) / n,
            "mean_excl_top3": sum(rest) / len(rest) if rest else None}


def account(rows: list) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    g = df.groupby("month").agg(port=("net_return", "mean"), bench=("bench_return", "first"),
                                names=("symbol", "count"), buys=("bought", "sum"))
    g["excess"] = g["port"] - g["bench"]
    return g


def acc_metrics(g: pd.DataFrame, first_exec, last_exec) -> dict:
    years = (pd.Timestamp(last_exec) - pd.Timestamp(first_exec)).days / 365.25
    pe, be = float((1 + g["port"]).prod()), float((1 + g["bench"]).prod())
    x = g["excess"]
    sd = x.std()
    return {"months": len(g), "years": years, "cagr": pe ** (1 / years) - 1, "bench_cagr": be ** (1 / years) - 1,
            "total": pe - 1, "bench_total": be - 1, "ir": float(x.mean() / sd * math.sqrt(12)) if sd > 0 else None,
            "t_monthly_excess": float(x.mean() / (sd / math.sqrt(len(x)))) if sd > 0 else None,
            "mean_names": float(g["names"].mean()), "annual_buy_turnover": float(g["buys"].sum() / g["names"].mean() / years),
            "mdd": float(((1 + g["port"]).cumprod() / (1 + g["port"]).cumprod().cummax() - 1).min()),
            "bench_mdd": float(((1 + g["bench"]).cumprod() / (1 + g["bench"]).cumprod().cummax() - 1).min())}


def evaluate(rows: list, exec_dates: pd.DatetimeIndex, first_k: int, last_k: int) -> dict:
    """사전 등록 §2/§3-2 판정 + 구간 표."""
    df = pd.DataFrame(rows)
    ks = list(range(first_k, last_k))
    thirds = [ks[i * len(ks) // 3:(i + 1) * len(ks) // 3] for i in range(3)]
    xs = list(df["excess_return"])
    half = len(ks) // 2                        # 앞·뒤 절반은 보유 월(시간) 기준
    first_half = list(df[df["k"] < ks[half]]["excess_return"])
    second_half = list(df[df["k"] >= ks[half]]["excess_return"])
    out = {"full": pos_metrics(xs), "first_half": pos_metrics(first_half), "second_half": pos_metrics(second_half),
           "thirds": [], "periods": {}}
    for part in thirds:
        sub = df[df["k"].isin(part)]
        out["thirds"].append({"from": str(exec_dates[part[0]].date()), "to": str(exec_dates[part[-1] + 1].date()),
                              **pos_metrics(list(sub["excess_return"])),
                              "account": acc_metrics(account(sub.to_dict("records")), exec_dates[part[0]],
                                                     exec_dates[part[-1] + 1])})
    out["account"] = acc_metrics(account(rows), exec_dates[first_k], exec_dates[last_k])
    ref = [k for k in ks if exec_dates[k] >= pd.Timestamp(REF_START)]
    sub = df[df["k"].isin(ref)]
    out["periods"]["참고 2019-06~"] = {**pos_metrics(list(sub["excess_return"])),
                                     "account": acc_metrics(account(sub.to_dict("records")), exec_dates[ref[0]],
                                                            exec_dates[last_k])}
    f = out["full"]
    out["verdict"] = {
        "c1_mean_pos_t_ge_2": bool(f["mean_excess"] > 0 and (f["t_excess"] or 0) >= 2),
        "c2_both_halves_pos": bool(out["first_half"]["mean_excess"] > 0 and out["second_half"]["mean_excess"] > 0),
        "c3_excl_top3_pos": bool((f["mean_excl_top3"] or 0) > 0),
        "c4_thirds_ge2_pos": int(sum(t["mean_excess"] > 0 for t in out["thirds"])) >= 2,
        "thirds_pos": int(sum(t["mean_excess"] > 0 for t in out["thirds"])),
        "account_cagr_beats": bool(out["account"]["cagr"] > out["account"]["bench_cagr"]),
    }
    v = out["verdict"]
    v["pass"] = bool(v["c1_mean_pos_t_ge_2"] and v["c2_both_halves_pos"] and v["c3_excl_top3_pos"] and v["c4_thirds_ge2_pos"])
    return out


# ── 자원 가드 ────────────────────────────────────────────────────────────────

def guard():
    with open("/proc/meminfo") as f:
        avail = next(int(line.split()[1]) // 1024 for line in f if line.startswith("MemAvailable"))
    if avail < MIN_AVAILABLE_MB:
        sys.exit(f"[중단] MemAvailable {avail}MB < {MIN_AVAILABLE_MB}MB")
    if pd.Timestamp.now() >= DEADLINE:
        sys.exit("[중단] 절대 시한 2026-09-30 13:00 KST 도달")
    return avail


# ── prep ─────────────────────────────────────────────────────────────────────

def _save(df: pd.DataFrame, path: Path, cols: dict):
    d = df.rename(columns=cols)[list(cols.values())]
    d.index = pd.to_datetime(d.index).normalize()
    d.index.name = "date"
    d.to_csv(path, compression="gzip")


def prep(args):
    import FinanceDataReader as fdr
    (CACHE / "yahoo").mkdir(parents=True, exist_ok=True)
    (CACHE / "naver").mkdir(parents=True, exist_ok=True)
    lst = fdr.StockListing("KOSPI")[["Code", "Name"]]
    lst.to_csv(CACHE / "listing_kospi_current.csv", index=False)
    rows = [(c, n, exclusion_reason(c, n)) for c, n in zip(lst["Code"], lst["Name"])]
    with (CACHE / "candidates.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["code", "name", "excluded"])
        w.writerows(rows)
    cands = [c for c, _, why in rows if why is None]
    print(f"[prep] 현재 KOSPI 상장 {len(rows)} → 후보 {len(cands)} (제외 {len(rows) - len(cands)})", flush=True)
    # 벤치: ^KS200 (Yahoo 불가 → FDR KS200), 교차 NAVER:KPI200, 069500(Yahoo)
    for key, sym in {"bench_fdr_ks200": "KS200", "bench_naver_kpi200": "NAVER:KPI200",
                     "bench_yahoo_069500": "YAHOO:069500.KS"}.items():
        p = CACHE / f"{key}.csv.gz"
        if not p.exists():
            _save(fdr.DataReader(sym, START, END), p, {"Open": "open", "Close": "close"})
    t0, fails = time.time(), {"yahoo": [], "naver": []}
    for i, code in enumerate(cands):
        if i % 50 == 0:
            print(f"[prep] {i}/{len(cands)} {time.time() - t0:.0f}s avail={guard()}MB", flush=True)
        for src, sym in (("yahoo", f"YAHOO:{code}.KS"), ("naver", code)):
            p = CACHE / src / f"{code}.csv.gz"
            if p.exists():
                continue
            try:
                d = fdr.DataReader(sym, START, END)
                if len(d) == 0:
                    raise ValueError("빈 응답")
                _save(d, p, {"Open": "open", "Close": "close", "Volume": "volume"})
            except Exception as e:           # noqa: BLE001 — 종목 하나 실패는 기록하고 계속
                fails[src].append([code, repr(e)[:120]])
            time.sleep(0.25)
    (CACHE / "prep_failures.json").write_text(json.dumps(fails, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[prep] 완료 {time.time() - t0:.0f}s 실패 yahoo={len(fails['yahoo'])} naver={len(fails['naver'])}")


# ── run ──────────────────────────────────────────────────────────────────────

def load_px(path: Path) -> pd.DataFrame:
    d = pd.read_csv(path, index_col="date", parse_dates=True)
    return d[d["close"].notna() & (d["close"] > 0)]


def build_panel(price_src: str, turn_src, codes: list, calendar, me) -> tuple:
    """가격(price_src) 일봉으로 월 특징. turn_src 가 있으면 거래대금은 그 원천의 종가×거래량(없으면 가격 원천으로 대체)."""
    cols = {c: {} for c in SIGNAL_COLS + ["last_close", "open_exec"]}
    missing, turn_fallback = [], []
    for i, code in enumerate(codes):
        if i % 100 == 0:
            guard()
        p = CACHE / price_src / f"{code}.csv.gz"
        if not p.exists():
            missing.append(code)
            continue
        tv = None
        if turn_src:
            tp = CACHE / turn_src / f"{code}.csv.gz"
            if tp.exists():
                t = load_px(tp)
                tv = t["close"] * t["volume"]
            else:
                turn_fallback.append(code)
        f = features(load_px(p), calendar, me, tv)
        for c in cols:
            cols[c][code] = f[c]
    return {c: pd.DataFrame(v) for c, v in cols.items()}, {"missing": missing, "turnover_fallback": turn_fallback}


# 원천 (§2 불가 → 대체): 가격은 Naver 수정주가(2014-07~), 거래대금은 Yahoo 종가×거래량(분할 일관).
SOURCES = {
    "본 결과": {"price": "naver", "turn": "yahoo", "first": "2015-08-01"},
    "참고: Naver 거래대금(수정가×원 거래량)": {"price": "naver", "turn": None, "first": "2015-08-01"},
    "참고: Yahoo 가격 같은 구간": {"price": "yahoo", "turn": None, "first": "2015-08-01"},
    "참고: Yahoo 가격 2010~(기업행위 미수정 오염)": {"price": "yahoo", "turn": None, "first": FIRST_HOLD},
}
ARTIFACTS = {"003060", "007460", "007610", "012170"}   # Naver 에도 남은 30% 초과 일간 변동(재상장·병합 흔적)


def study(panel, prm, bench_open, exec_dates, first_k, last_k):
    held = holdings(panel, **prm)
    rows = positions(panel, held, bench_open, first_k, last_k)
    ev = evaluate(rows, exec_dates, first_k, last_k)
    ev.update(params=prm, first_entry=str(exec_dates[first_k].date()), last_exit=str(exec_dates[last_k].date()),
              px_fallback=int(sum(r["px_fallback"] for r in rows)),
              short_months=int(sum(len(held[k]) < prm["top_n"] for k in range(first_k, last_k))),
              artifact_positions=[{k: r[k] for k in ("month", "symbol", "excess_return")} for r in rows
                                  if r["symbol"] in ARTIFACTS])
    return ev, rows, held


def run(args):
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    bench = load_px(CACHE / "bench_fdr_ks200.csv.gz")
    calendar = bench.index
    me = month_ends(calendar)
    exec_dates = calendar[me + 1]
    bench_open = bench["open"].reindex(exec_dates)
    bench_open_src = "open"
    if bench_open.isna().any() or (bench_open <= 0).any():
        bench_open, bench_open_src = bench["close"].shift(1).reindex(exec_dates), "prev_close"
    cand = pd.read_csv(CACHE / "candidates.csv", dtype=str, keep_default_na=False)
    names = dict(zip(cand["code"], cand["name"]))
    codes = list(cand[cand["excluded"] == ""]["code"])
    last_k = len(exec_dates) - 1                                   # 마지막 체결일 = 마지막 청산
    res = {"calendar": {"source": "FDR KS200 (KOSPI200 가격지수)", "first": str(calendar[0].date()),
                        "last": str(calendar[-1].date()), "rows": len(calendar)},
           "bench_exec_price": bench_open_src, "candidates": len(codes), "excluded": int((cand["excluded"] != "").sum()),
           "excluded_by_reason": cand[cand["excluded"] != ""]["excluded"].value_counts().to_dict(),
           "costs": {"buy": BUY_COST, "sell": SELL_COST}, "sources": {}, "variants": {}}
    for sname, src in SOURCES.items():
        panel, info = build_panel(src["price"], src["turn"], codes, calendar, me)
        first_k = int(np.searchsorted(exec_dates, pd.Timestamp(src["first"])))
        sig = pd.DataFrame({c: panel[c].iloc[first_k] for c in SIGNAL_COLS})
        eligible = int(((sig["hist"] >= MIN_HIST) & sig["has252"] & sig["traded_t"] & sig["mom12"].notna()).sum())
        res["sources"][sname] = {**src, **info, "eligible_at_first": eligible}
        grid = {"본 결과": {}, **ROBUST, **DIAG} if sname == "본 결과" else {sname: {}}
        for name, over in grid.items():
            guard()
            prm = {**MAIN, **over}
            ev, rows, held = study(panel, prm, bench_open, exec_dates, first_k, last_k)
            ev["source"] = sname
            res["variants"][name] = ev
            print(f"[run] {name}: {json.dumps(ev['verdict'], ensure_ascii=False)}", flush=True)
            if name != "본 결과":
                continue
            res["hold_months"] = {"first_entry": ev["first_entry"], "last_exit": ev["last_exit"], "n": last_k - first_k}
            pd.DataFrame(rows).to_csv(out / "positions.csv", index=False, float_format="%.6g")
            account(rows).to_csv(out / "account_monthly.csv", float_format="%.6g")
            with (out / "holdings_monthly.csv").open("w", encoding="utf-8", newline="") as f:
                w = csv.writer(f)
                w.writerow(["entry_date", "n", "codes", "names"])
                for k in range(first_k, last_k):
                    hs = sorted(held[k])
                    w.writerow([str(exec_dates[k].date()), len(hs), " ".join(hs), " ".join(names.get(s, s) for s in hs)])
            ever = set()                      # 유니버스에 한 번이라도 든 종목 수 (생존 편향 규모 참고)
            for k in range(first_k, last_k):
                sg = pd.DataFrame({c: panel[c].iloc[k] for c in SIGNAL_COLS})
                ever |= set(select(sg, top_n=prm["universe_n"], universe_n=prm["universe_n"], mom=prm["mom"]))
            res["universe_ever"] = len(ever)
            top = sorted(rows, key=lambda r: r["excess_return"], reverse=True)[:3]
            res["top3_positions"] = [{k: r[k] for k in ("month", "symbol", "excess_return")} | {"name": names.get(r["symbol"])}
                                     for r in top]
        del panel
    res["crosscheck"] = crosscheck(bench_open, exec_dates)
    (out / "summary.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    report(args)


def crosscheck(bench_open, exec_dates) -> dict:
    """벤치 월수익(FDR KS200 시가→시가)을 NAVER:KPI200·Yahoo 069500 시가 기준과 비교."""
    out = {}
    b = pd.Series(bench_open.to_numpy(), index=exec_dates).pct_change(fill_method=None).shift(-1).dropna()
    for key in ("bench_naver_kpi200", "bench_yahoo_069500"):
        o = load_px(CACHE / f"{key}.csv.gz")["open"].reindex(exec_dates)
        j = pd.concat([b, o.pct_change(fill_method=None).shift(-1)], axis=1).dropna()
        d = (j.iloc[:, 0] - j.iloc[:, 1]).abs()
        out[key] = {"months": len(j), "mean_abs_diff": float(d.mean()), "max_abs_diff": float(d.max()),
                    "corr": float(j.corr().iloc[0, 1]), "mean_diff": float((j.iloc[:, 0] - j.iloc[:, 1]).mean())}
    return out


# ── report ───────────────────────────────────────────────────────────────────

def _p(x, pct=True, sign=True):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    return f"{x * 100:+.2f}%" if pct and sign else (f"{x * 100:.1f}%" if pct else f"{x:.2f}")


def report(args):
    out = Path(args.out)
    res = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    m = res["variants"]["본 결과"]
    a, f = m["account"], m["full"]
    L = ["## 본 결과 (상위20·12-1·유니버스100, 비용 포함 — 가격 Naver 수정주가, 거래대금 Yahoo)", "",
         f"보유 {res['hold_months']['first_entry']} → {res['hold_months']['last_exit']} ({res['hold_months']['n']}개월), "
         f"후보 {res['candidates']}종목(제외 {res['excluded']}: {res['excluded_by_reason']}), 벤치 KOSPI200 체결가 `{res['bench_exec_price']}`, "
         f"첫 월 적격 {res['sources']['본 결과']['eligible_at_first']}종목, 유니버스에 한 번이라도 든 종목 {res['universe_ever']}", "",
         "### 계좌", "", "| 구간 | 개월 | CAGR | KOSPI200 CAGR | IR | 월 초과 t | MDD | 벤치 MDD |", "|---|---|---|---|---|---|---|---|"]
    rowsets = [("전체", a)] + [(f"{t['from']}~{t['to']}", t["account"]) for t in m["thirds"]] + \
              [("참고 2019-06~", m["periods"]["참고 2019-06~"]["account"])]
    for lab, x in rowsets:
        L.append(f"| {lab} | {x['months']} | {_p(x['cagr'])} | {_p(x['bench_cagr'])} | {_p(x['ir'], False)} | "
                 f"{_p(x['t_monthly_excess'], False)} | {_p(x['mdd'])} | {_p(x['bench_mdd'])} |")
    L += ["", "### 포지션 (종목, 보유 월) 초과수익", "", "| 구간 | n | 평균 | t | 중앙값 | 벤치 초과 비율 | 상위3 제외 평균 |",
          "|---|---|---|---|---|---|---|"]
    prs = [("전체", f), ("앞 절반", m["first_half"]), ("뒤 절반", m["second_half"])] + \
          [(f"3등분 {t['from']}~{t['to']}", t) for t in m["thirds"]] + [("참고 2019-06~", m["periods"]["참고 2019-06~"])]
    for lab, x in prs:
        L.append(f"| {lab} | {x['n']} | {_p(x['mean_excess'])} | {_p(x['t_excess'], False)} | {_p(x['median_excess'])} | "
                 f"{_p(x['beat_rate'], True, False)} | {_p(x['mean_excl_top3'])} |")
    v = m["verdict"]
    L += ["", "### 사전 등록 기준 판정", "", "| 기준 | 결과 |", "|---|---|",
          f"| 1. 평균 > 0 그리고 t ≥ 2 | {'통과' if v['c1_mean_pos_t_ge_2'] else '기각'} (평균 {_p(f['mean_excess'])}, t={_p(f['t_excess'], False)}) |",
          f"| 2. 앞·뒤 절반 각각 > 0 | {'통과' if v['c2_both_halves_pos'] else '기각'} ({_p(m['first_half']['mean_excess'])} / {_p(m['second_half']['mean_excess'])}) |",
          f"| 3. 상위 3건 제외 > 0 | {'통과' if v['c3_excl_top3_pos'] else '기각'} ({_p(f['mean_excl_top3'])}) |",
          f"| 4. 3등분 중 2 이상 > 0 | {'통과' if v['c4_thirds_ge2_pos'] else '기각'} ({v['thirds_pos']}/3) |",
          f"| **종합** | **{'통과' if v['pass'] else '기각'}** |",
          f"| (보고) 계좌 CAGR > KOSPI200 | {'예' if v['account_cagr_beats'] else '아니오'} ({_p(a['cagr'])} vs {_p(a['bench_cagr'])}) |",
          "", "## 강건성·원천 민감도·진단 (보고만 — 판정에 쓰지 않음)", "",
          "| 변형 | 보유 기간 | 포지션 n | 평균 초과 | t | 앞/뒤 절반 | 상위3 제외 | 3등분 양수 | 계좌 CAGR | 벤치 CAGR | IR | 4기준 |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for name, x in res["variants"].items():
        fx, vx, ax = x["full"], x["verdict"], x["account"]
        L.append(f"| {name} | {x['first_entry']}~{x['last_exit']} | {fx['n']} | {_p(fx['mean_excess'])} | {_p(fx['t_excess'], False)} | "
                 f"{_p(x['first_half']['mean_excess'])} / {_p(x['second_half']['mean_excess'])} | {_p(fx['mean_excl_top3'])} | "
                 f"{vx['thirds_pos']}/3 | {_p(ax['cagr'])} | {_p(ax['bench_cagr'])} | {_p(ax['ir'], False)} | "
                 f"{'통과' if vx['pass'] else '기각'} |")
    cc = res.get("crosscheck", {})
    if cc:
        L += ["", "## 교차 확인 (벤치)", ""]
        for key in ("bench_naver_kpi200", "bench_yahoo_069500"):
            b = cc[key]
            L.append(f"- 벤치 월수익 vs `{key}`: {b['months']}개월, 평균 절대차 {b['mean_abs_diff'] * 100:.3f}%p, "
                     f"최대 {b['max_abs_diff'] * 100:.2f}%p, 평균차 {b['mean_diff'] * 100:+.3f}%p, 상관 {b['corr']:.4f}")
    (out / "tables.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["prep", "run", "report"])
    ap.add_argument("--out", default=str(OUT_DEFAULT))
    args = ap.parse_args()
    {"prep": prep, "run": run, "report": report}[args.cmd](args)


if __name__ == "__main__":
    main()
