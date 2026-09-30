#!/usr/bin/env python3
"""H3 — 가치·퀄리티 기울기 (사전 등록: docs/research/index-plus-program-2026-09.md §3-3).

월말 t 마다 근사 시총(수정종가(t) × 현재 상장주식수) 상위 200 → 접수일 ≤ t 인 최근 사업보고서로 BM·ROE →
순위 백분위 평균 상위 100 을 시총가중(H3), 점수 가능 유니버스 전체 시총가중(B). 다음 거래일 시가 체결, 월 1회.
비용은 교체분·비중 변화분(드리프트 뒤 비중 → 새 목표 비중의 차이)에만. 연구 전용 — src/·운영 무변경, KIS·DART 무접촉.

  prep  : H2 가격 캐시(Naver 수정주가) → 종목별 월말 특징(hist·last_close·open_exec) → ~/.cache/ai_trader/backtest/h3_panel/
  run   : 패널 + DART 주요계정 → 본 결과·강건성 → results/index_plus_h3/
  report: summary.json → tables.md 재생성

실행: nice -n 19 <venv>/bin/python scripts/research/index_plus_h3.py prep|run|report
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import pickle
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("index_plus_h2", ROOT / "scripts/research/index_plus_h2.py")
h2 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(h2)

PX_CACHE = h2.CACHE / "naver"
PANEL = Path.home() / ".cache/ai_trader/backtest/h3_panel/panel.pkl"
DATA = ROOT / "results/index_plus_h3"
OUT_DEFAULT = DATA
BUY_COST, SELL_COST = h2.BUY_COST, h2.SELL_COST
UNIVERSE_N, MIN_HIST = 200, 250
MAIN = {"top_n": 100, "score": "combo", "weight": "cap"}
BASE = {"top_n": None, "score": "combo", "weight": "cap"}          # B: 점수 가능 유니버스 전체
ROBUST = {"상위 1/4(50)": {"top_n": 50}, "BM 단독": {"score": "bm"}, "ROE 단독": {"score": "roe"},
          "동일 비중 H3": {"weight": "equal"}}
DEADLINE = pd.Timestamp("2026-09-30 15:30")
PANEL_COLS = ["hist", "last_close", "open_exec"]


def guard():
    h2.DEADLINE = DEADLINE
    return h2.guard()


# ── 재무 (시점 정합) ─────────────────────────────────────────────────────────

def pick_statement(fund: pd.DataFrame) -> pd.DataFrame:
    """(fy, corp_code) 당 한 행: 연결(CFS) 행에 자본총계·순이익이 둘 다 있으면 CFS, 아니면 OFS."""
    f = fund.copy()
    f["ok"] = f["equity"].notna() & f["net_income"].notna()
    f["pri"] = np.where(f["fs_div"] == "CFS", 0, 1) + np.where(f["ok"], 0, 2)
    return f.sort_values(["fy", "corp_code", "pri"]).drop_duplicates(["fy", "corp_code"]).drop(columns=["ok", "pri"])


def fundamentals_asof(fund: pd.DataFrame, t: pd.Timestamp) -> pd.DataFrame:
    """접수일 ≤ t 인 사업보고서 중 사업연도가 가장 최근인 것(index=code). 접수일이 t 뒤인 행은 보지 않는다."""
    f = fund[fund["rcept_date"] <= t]
    return f.sort_values(["code", "fy"]).drop_duplicates("code", keep="last").set_index("code")


# ── 신호·목표 비중 (t 까지 자료만) ───────────────────────────────────────────

def scored_universe(sig: pd.DataFrame, shares: pd.Series, fin: pd.DataFrame) -> pd.DataFrame:
    """sig(index=code: hist, last_close) → 근사 시총 상위 200 → 재무 결합.
    반환 열: cap, bm, roe, has_fin(자본총계 > 0 이고 순이익 있음). 동률은 종목코드 순."""
    s = sig[(sig["hist"] >= MIN_HIST) & sig["last_close"].notna()].copy()
    s["cap"] = s["last_close"] * shares.reindex(s.index)
    s = s[s["cap"] > 0].reset_index(names="code").sort_values(["cap", "code"], ascending=[False, True])
    u = s.head(UNIVERSE_N).set_index("code")
    fin = fin.reindex(u.index)
    u["equity"], u["net_income"] = fin["equity"], fin["net_income"]
    u["has_fin"] = (u["equity"] > 0) & u["net_income"].notna()
    u["bm"] = u["equity"] / u["cap"]
    u["roe"] = u["net_income"] / u["equity"]
    return u[["cap", "equity", "net_income", "has_fin", "bm", "roe"]]


def universe_at(panel: dict, k: int, t: pd.Timestamp, shares: pd.Series, fund: pd.DataFrame) -> pd.DataFrame:
    """월말 k(날짜 t)의 점수 유니버스 — 가격은 패널 k 행(t 까지), 재무는 접수일 ≤ t."""
    sig = pd.DataFrame({c: panel[c].iloc[k] for c in ("hist", "last_close")})
    return scored_universe(sig, shares, fundamentals_asof(fund, t))


def target_weights(u: pd.DataFrame, *, top_n, score: str, weight: str) -> pd.Series:
    """점수 가능 종목 안 순위 백분위(높을수록 좋음) → 상위 top_n(None = 전체) → 시총가중 또는 동일 비중."""
    s = u[u["has_fin"]].copy()
    if s.empty:
        return pd.Series(dtype=float)
    pb, pr = s["bm"].rank(pct=True), s["roe"].rank(pct=True)
    s["score"] = {"combo": (pb + pr) / 2, "bm": pb, "roe": pr}[score]
    s = s.reset_index(names="code").sort_values(["score", "code"], ascending=[False, True])
    if top_n is not None:
        s = s.head(top_n)
    w = s.set_index("code")["cap"] if weight == "cap" else pd.Series(1.0, index=s["code"])
    return w / w.sum()


# ── 체결·비용·포지션 ─────────────────────────────────────────────────────────

def executable(targets: list, panel: dict) -> list:
    """새 편입(직전 달 미보유)은 다음 거래일 시가가 있어야 한다(정지·결측은 그달 제외 후 재정규화). 계속 보유는 유지."""
    out, prev = [], set()
    for k, w in enumerate(targets):
        oe = panel["open_exec"].iloc[k]
        keep = [s for s in w.index if s in prev or not math.isnan(oe.get(s, np.nan))]
        w = w[keep]
        w = w / w.sum() if len(w) else w
        out.append(w)
        prev = set(w.index)
    return out


def positions(panel: dict, weights: list, bench_exec: pd.Series, first_k: int, last_k: int) -> list:
    """보유 월 k(first_k ≤ k < last_k), 체결 k → k+1.

    비용 = 비중 변화분만: 매수분 f_buy = max(w_k − 드리프트 비중, 0)/w_k (새 편입은 1),
    매도분 f_sell = max(드리프트 비중 − w_{k+1}, 0)/드리프트 비중 (편출·마지막 달 청산은 1).
    순수익 = (1+g)(1 − SELL·f_sell)/(1 + BUY·f_buy) − 1. 계좌 월수익 = Σ w_k · 순수익."""
    dates = panel["open_exec"].index
    gross = []
    for k in range(first_k, last_k):
        gross.append({s: h2.price_at(panel, k + 1, s)[0] / h2.price_at(panel, k, s)[0] - 1 for s in weights[k].index})

    def drifted(k):                      # 보유 월 k 끝(= 체결일 k+1 직전)의 비중
        g, w = gross[k - first_k], weights[k]
        v = {s: w[s] * (1 + g[s]) for s in w.index}
        tot = sum(v.values())
        return {s: x / tot for s, x in v.items()}

    rows = []
    for k in range(first_k, last_k):
        w, g = weights[k], gross[k - first_k]
        pd_prev = drifted(k - 1) if k > first_k else {}
        cur = drifted(k)
        nxt = weights[k + 1] if k + 1 < last_k else pd.Series(dtype=float)
        b = bench_exec.iloc[k + 1] / bench_exec.iloc[k] - 1
        for s in w.index:
            f_buy = max(w[s] - pd_prev.get(s, 0.0), 0.0) / w[s]
            f_sell = max(cur[s] - float(nxt.get(s, 0.0)), 0.0) / cur[s] if cur[s] > 0 else 0.0
            r = (1 + g[s]) * (1 - SELL_COST * f_sell) / (1 + BUY_COST * f_buy) - 1
            fb0, fb1 = h2.price_at(panel, k, s)[1], h2.price_at(panel, k + 1, s)[1]
            rows.append({"month": str(dates[k].date()), "k": k, "symbol": s, "weight": float(w[s]),
                         "gross_return": g[s], "f_buy": f_buy, "f_sell": f_sell, "net_return": r,
                         "bought": s not in pd_prev, "bench_return": b, "excess_return": r - b,
                         "px_fallback": fb0 or fb1})
    return rows


def account(rows: list) -> pd.DataFrame:
    """계좌 월수익 = 비중 가중 순수익 (h2.evaluate 가 이 함수를 쓴다 — h2 의 동일 비중 account 를 대체)."""
    df = pd.DataFrame(rows)
    df["wr"] = df["weight"] * df["net_return"]
    g = df.groupby("month").agg(port=("wr", "sum"), bench=("bench_return", "first"),
                                names=("symbol", "count"), buys=("bought", "sum"))
    g["excess"] = g["port"] - g["bench"]
    return g


h2.account = account                     # ponytail: h2.evaluate 재사용을 위해 모듈 전역 교체 — 판정 코드 한 벌 유지


# ── prep ─────────────────────────────────────────────────────────────────────

def calendar():
    bench = h2.load_px(h2.CACHE / "bench_fdr_ks200.csv.gz")
    cal = bench.index
    me = h2.month_ends(cal)
    return bench, cal, me


def prep(args):
    _, cal, me = calendar()
    cand = pd.read_csv(DATA / "universe_candidates.csv", dtype=str)
    cols, missing = {c: {} for c in PANEL_COLS}, []
    for i, code in enumerate(cand["code"]):
        if i % 100 == 0:
            print(f"[prep] {i}/{len(cand)} avail={guard()}MB", flush=True)
        p = PX_CACHE / f"{code}.csv.gz"
        if not p.exists():
            missing.append(code)
            continue
        f = h2.features(h2.load_px(p), cal, me)
        for c in cols:
            cols[c][code] = f[c]
    PANEL.parent.mkdir(parents=True, exist_ok=True)
    with PANEL.open("wb") as fh:
        pickle.dump({"panel": {c: pd.DataFrame(v) for c, v in cols.items()}, "missing": missing}, fh)
    print(f"[prep] 완료 — 누락 {len(missing)} → {PANEL}")


# ── run ──────────────────────────────────────────────────────────────────────

def load_fund() -> pd.DataFrame:
    f = pd.read_csv(DATA / "dart_fundamentals.csv", dtype={"corp_code": str, "stock_code": str, "rcept_no": str,
                                                          "rcept_date": str})
    cand = pd.read_csv(DATA / "universe_candidates.csv", dtype=str)
    f["code"] = f["corp_code"].map(dict(zip(cand["corp_code"], cand["code"])))
    f["rcept_date"] = pd.to_datetime(f["rcept_date"], format="%Y%m%d")
    return pick_statement(f[f["code"].notna()])


def run(args):
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    print(f"[run] avail={guard()}MB", flush=True)
    bench, cal, me = calendar()
    exec_dates = cal[me + 1]
    bench_open = bench["open"].reindex(exec_dates)
    assert bench_open.notna().all() and (bench_open > 0).all()
    with PANEL.open("rb") as fh:
        pk = pickle.load(fh)
    panel = pk["panel"]
    cand = pd.read_csv(DATA / "universe_candidates.csv", dtype=str)
    names = dict(zip(cand["code"], cand["name"]))
    shares = pd.Series(cand["stocks"].astype(float).to_numpy(), index=cand["code"])
    fund = load_fund()
    n_me = len(exec_dates)
    unis = []
    for k in range(n_me):
        unis.append(universe_at(panel, k, cal[me[k]], shares, fund))
    # 실제 시작: 사전 등록 구간(2015-08~) 안에서 점수 가능 종목이 생기는 첫 월말. 그 전 월은 B·H3 모두 결과 없음
    n_scored = [int(u["has_fin"].sum()) for u in unis]
    first_k = next(k for k in range(n_me) if n_scored[k] > 0 and cal[me[k]] >= pd.Timestamp("2015-07-31"))
    last_k = n_me - 1
    uni_tab = [{"signal_month_end": str(cal[me[k]].date()), "exec": str(exec_dates[k].date()), "universe": len(unis[k]),
                "scored": n_scored[k], "no_fin": int((~unis[k]["has_fin"]).sum()),
                "missing_fin": int(unis[k]["equity"].isna().sum()),
                "equity_le0": int((unis[k]["equity"] <= 0).sum()),
                "ni_missing": int((unis[k]["equity"].notna() & unis[k]["net_income"].isna()).sum())}
               for k in range(n_me) if cal[me[k]] >= pd.Timestamp("2015-07-31")]
    pd.DataFrame(uni_tab).to_csv(out / "universe_monthly.csv", index=False)
    res = {"calendar": {"source": "FDR KS200", "first": str(cal[0].date()), "last": str(cal[-1].date())},
           "panel_missing": pk["missing"], "candidates": len(cand), "costs": {"buy": BUY_COST, "sell": SELL_COST},
           "first_signal": str(cal[me[first_k]].date()), "hold": {"first_entry": str(exec_dates[first_k].date()),
                                                                   "last_exit": str(exec_dates[last_k].date()),
                                                                   "n": last_k - first_k},
           "universe_stats": {k2: float(np.mean([r[k2] for r in uni_tab if r["exec"] >= str(exec_dates[first_k].date())
                                                 and r["exec"] < str(exec_dates[last_k].date())]))
                              for k2 in ("universe", "scored", "no_fin", "missing_fin", "equity_le0", "ni_missing")},
           "variants": {}}
    grid = {"H3 본 결과": MAIN, "B 기준선(점수 가능 유니버스 시총가중)": BASE,
            **{k2: {**MAIN, **v} for k2, v in ROBUST.items()}}
    accts = {}
    for name, prm in grid.items():
        guard()
        tw = [target_weights(unis[k], **prm) if k >= first_k else pd.Series(dtype=float) for k in range(n_me)]
        w = executable(tw, panel)
        rows = positions(panel, w, bench_open, first_k, last_k)
        ev = h2.evaluate(rows, exec_dates, first_k, last_k)
        g = account(rows)
        accts[name] = g
        buy_w = pd.DataFrame(rows).assign(bw=lambda d: d["weight"] * d["f_buy"]).groupby("month")["bw"].sum()
        ev.update(params={k2: v for k2, v in prm.items()}, first_entry=str(exec_dates[first_k].date()),
                  last_exit=str(exec_dates[last_k].date()), px_fallback=int(sum(r["px_fallback"] for r in rows)),
                  mean_names=float(g["names"].mean()), annual_buy_weight_turnover=float(buy_w.iloc[1:].mean() * 12),
                  top3=[{"month": r["month"], "symbol": r["symbol"], "name": names.get(r["symbol"]),
                         "excess_return": r["excess_return"]}
                        for r in sorted(rows, key=lambda r: r["excess_return"], reverse=True)[:3]])
        res["variants"][name] = ev
        print(f"[run] {name}: {json.dumps(ev['verdict'], ensure_ascii=False)} cagr={ev['account']['cagr']:.4f}", flush=True)
        if name == "H3 본 결과":
            accts_rows = rows
            pd.DataFrame(rows).to_csv(out / "positions.csv", index=False, float_format="%.6g")
            with (out / "holdings_monthly.csv").open("w", encoding="utf-8") as fh:
                fh.write("entry_date,n,codes\n")
                for k in range(first_k, last_k):
                    fh.write(f"{exec_dates[k].date()},{len(w[k])},{' '.join(sorted(w[k].index))}\n")
    # 2차: H3 vs B 월 초과 IR
    h, bb = accts["H3 본 결과"], accts["B 기준선(점수 가능 유니버스 시총가중)"]
    h.join(bb[["port", "names"]].add_prefix("b_")).to_csv(out / "account_monthly.csv", float_format="%.6g")
    x = h["port"] - bb["port"]
    res["h3_vs_b"] = {"mean_monthly": float(x.mean()), "ir": float(x.mean() / x.std() * math.sqrt(12)),
                      "t": float(x.mean() / (x.std() / math.sqrt(len(x)))),
                      "thirds": [{"from": p.index[0], "to": p.index[-1], "mean_monthly": float(p.mean()),
                                  "t": float(p.mean() / (p.std() / math.sqrt(len(p))))}
                                 for p in (x.iloc[i * len(x) // 3:(i + 1) * len(x) // 3] for i in range(3))]}
    # 사후 진단(보고만): DART 자료가 FY2023 부터만 있는 종목(주로 금융업)은 2024-03 접수 전까지 유니버스 밖이다.
    # 그 시점 앞·뒤로 H3 − B 를 나눠 2차 우위가 어디서 왔는지 본다.
    late = set(fund.groupby("code")["fy"].min().loc[lambda s: s >= 2023].index)
    cut = "2024-03-29"
    hp = pd.DataFrame(accts_rows)
    late_w = hp[hp["symbol"].isin(late)].groupby("month")["weight"].sum().reindex(h.index).fillna(0.0)
    res["diag_late_fin"] = {"n_late_codes": len(late), "cut_signal_month": cut,
                            "h3_weight_late_after": float(late_w[late_w.index >= cut].mean())}
    for lab, sel in (("before", x.index < cut), ("after", x.index >= cut)):
        p = x[sel]
        res["diag_late_fin"][lab] = {"months": int(len(p)), "mean_monthly": float(p.mean()),
                                     "t": float(p.mean() / (p.std() / math.sqrt(len(p))))}
    bx = bb["port"] - bb["bench"]
    res["b_vs_ks200"] = {"mean_monthly": float(bx.mean()), "ir": float(bx.mean() / bx.std() * math.sqrt(12)),
                         "t": float(bx.mean() / (bx.std() / math.sqrt(len(bx))))}
    (out / "summary.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    report(args)


# ── report ───────────────────────────────────────────────────────────────────

def report(args):
    out = Path(args.out)
    res = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    P = h2._p
    m = res["variants"]["H3 본 결과"]
    b = res["variants"]["B 기준선(점수 가능 유니버스 시총가중)"]
    a, f, ba = m["account"], m["full"], b["account"]
    us = res["universe_stats"]
    L = ["## H3 본 결과 (점수 상위 100 시총가중, 비용 포함 — 가격 Naver 수정주가)", "",
         f"첫 신호 {res['first_signal']}, 보유 {res['hold']['first_entry']} → {res['hold']['last_exit']} ({res['hold']['n']}개월), "
         f"후보 {res['candidates']}, 월평균 유니버스 {us['universe']:.1f} · 점수 가능 {us['scored']:.1f} · 재무 미접수 {us['missing_fin']:.1f} · "
         f"자본≤0 {us['equity_le0']:.2f} · 순이익 결측 {us['ni_missing']:.2f}", "",
         "### 계좌 (2차)", "", "| 포트폴리오 | 구간 | 개월 | CAGR | KOSPI200 CAGR | 월 초과 IR(vs KS200) | 월 초과 t | MDD | 벤치 MDD | 평균 종목 | 연 매수 회전(비중) |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    for name, x in res["variants"].items():
        ax = x["account"]
        L.append(f"| {name} | 전체 | {ax['months']} | {P(ax['cagr'])} | {P(ax['bench_cagr'])} | {P(ax['ir'], False)} | "
                 f"{P(ax['t_monthly_excess'], False)} | {P(ax['mdd'])} | {P(ax['bench_mdd'])} | {x['mean_names']:.1f} | "
                 f"{x['annual_buy_weight_turnover'] * 100:.0f}% |")
    L += ["", "| 구간(H3 / B) | 개월 | H3 CAGR | B CAGR | KOSPI200 CAGR | H3 IR | B IR |", "|---|---|---|---|---|---|---|"]
    for t, tb in zip(m["thirds"], b["thirds"]):
        L.append(f"| {t['from']}~{t['to']} | {t['account']['months']} | {P(t['account']['cagr'])} | {P(tb['account']['cagr'])} | "
                 f"{P(t['account']['bench_cagr'])} | {P(t['account']['ir'], False)} | {P(tb['account']['ir'], False)} |")
    r, rb = m["periods"]["참고 2019-06~"]["account"], b["periods"]["참고 2019-06~"]["account"]
    L.append(f"| 참고 2019-06~ | {r['months']} | {P(r['cagr'])} | {P(rb['cagr'])} | {P(r['bench_cagr'])} | {P(r['ir'], False)} | {P(rb['ir'], False)} |")
    hb, bk = res["h3_vs_b"], res["b_vs_ks200"]
    L += ["", f"- H3 − B 월 초과: 평균 {P(hb['mean_monthly'])}, IR {hb['ir']:.2f}, t {hb['t']:.2f} (3등분: "
          + ", ".join(f"{p['from']}~{p['to']} {P(p['mean_monthly'])} t={p['t']:.2f}" for p in hb["thirds"]) + ", 신호 월말 기준)",
          f"- B − KOSPI200 월 초과: 평균 {P(bk['mean_monthly'])}, IR {bk['ir']:.2f}, t {bk['t']:.2f}",
          f"- (사후 진단) H3 − B, 신호 월말 {res['diag_late_fin']['cut_signal_month']} 앞: {P(res['diag_late_fin']['before']['mean_monthly'])} "
          f"t={res['diag_late_fin']['before']['t']:.2f} ({res['diag_late_fin']['before']['months']}개월) / 뒤: "
          f"{P(res['diag_late_fin']['after']['mean_monthly'])} t={res['diag_late_fin']['after']['t']:.2f} "
          f"({res['diag_late_fin']['after']['months']}개월) — 뒤 구간 H3 비중 중 FY2023~ 자료 종목(주로 금융, "
          f"{res['diag_late_fin']['n_late_codes']}종목) {res['diag_late_fin']['h3_weight_late_after'] * 100:.1f}%",
          "", "### 포지션 (종목, 보유 월) KOSPI200 초과 — 동일 가중 (1차)", "",
          "| 구간 | n | 평균 | t | 중앙값 | 벤치 초과 비율 | 상위3 제외 평균 |", "|---|---|---|---|---|---|---|"]
    prs = [("전체", f), ("앞 절반", m["first_half"]), ("뒤 절반", m["second_half"])] + \
          [(f"3등분 {t['from']}~{t['to']}", t) for t in m["thirds"]] + [("참고 2019-06~", m["periods"]["참고 2019-06~"])]
    for lab, x in prs:
        L.append(f"| {lab} | {x['n']} | {P(x['mean_excess'])} | {P(x['t_excess'], False)} | {P(x['median_excess'])} | "
                 f"{P(x['beat_rate'], True, False)} | {P(x['mean_excl_top3'])} |")
    v = m["verdict"]
    c2a = a["cagr"] > ba["cagr"]
    c2b = a["cagr"] > a["bench_cagr"]
    L += ["", "### 사전 등록 기준 판정", "", "| 기준 | 값 | 판정 |", "|---|---|---|",
          f"| 1차-1. 포지션 초과 평균 > 0 그리고 t ≥ 2 | 평균 {P(f['mean_excess'])}, t = {P(f['t_excess'], False)} | {'통과' if v['c1_mean_pos_t_ge_2'] else '기각'} |",
          f"| 1차-2. 앞·뒤 절반 각각 > 0 | {P(m['first_half']['mean_excess'])} / {P(m['second_half']['mean_excess'])} | {'통과' if v['c2_both_halves_pos'] else '기각'} |",
          f"| 1차-3. 상위 3건 제외 > 0 | {P(f['mean_excl_top3'])} | {'통과' if v['c3_excl_top3_pos'] else '기각'} |",
          f"| 1차-4. 3등분 중 2 이상 > 0 | {v['thirds_pos']}/3 | {'통과' if v['c4_thirds_ge2_pos'] else '기각'} |",
          f"| **1차 종합** | | **{'통과' if v['pass'] else '기각'}** |",
          f"| 2차-a. H3 CAGR > B CAGR | {P(a['cagr'])} vs {P(ba['cagr'])} | {'통과' if c2a else '기각'} |",
          f"| 2차-b. H3 CAGR > KS200 CAGR | {P(a['cagr'])} vs {P(a['bench_cagr'])} | {'통과' if c2b else '기각'} |",
          f"| **2차 종합** | | **{'통과' if c2a and c2b else '기각'}** |",
          f"| (보고) 월 초과 IR vs KS200 | {P(a['ir'], False)} | — |",
          "", "## 강건성 (보고만 — 판정에 쓰지 않음)", "",
          "| 변형 | 포지션 n | 평균 초과 | t | 앞/뒤 절반 | 상위3 제외 | 3등분 양수 | CAGR | KS200 CAGR | IR | 1차 4기준 |",
          "|---|---|---|---|---|---|---|---|---|---|---|"]
    for name, x in res["variants"].items():
        fx, vx, ax = x["full"], x["verdict"], x["account"]
        L.append(f"| {name} | {fx['n']} | {P(fx['mean_excess'])} | {P(fx['t_excess'], False)} | "
                 f"{P(x['first_half']['mean_excess'])} / {P(x['second_half']['mean_excess'])} | {P(fx['mean_excl_top3'])} | "
                 f"{vx['thirds_pos']}/3 | {P(ax['cagr'])} | {P(ax['bench_cagr'])} | {P(ax['ir'], False)} | {'통과' if vx['pass'] else '기각'} |")
    L += ["", "상위 3 포지션(H3): " + ", ".join(f"{t['name']}({t['symbol']}) {t['month']} {P(t['excess_return'])}" for t in m["top3"])]
    (out / "tables.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["prep", "run", "report"])
    ap.add_argument("--out", default=str(OUT_DEFAULT))
    args = ap.parse_args()
    t0 = pd.Timestamp.now()
    {"prep": prep, "run": run, "report": report}[args.cmd](args)
    import resource
    print(f"[{args.cmd}] {(pd.Timestamp.now() - t0).total_seconds():.0f}s, 최대 RSS "
          f"{resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024}MB", flush=True)


if __name__ == "__main__":
    main()
