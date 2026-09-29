#!/usr/bin/env python3
"""H1 — 지수 보유 + 하락 방어 오버레이 (사전 등록: docs/research/index-plus-program-2026-09.md §3).

t 일 종가까지로 비중을 정해 t+1 수익에 적용한다. 연구 전용 — src/·운영 무변경, KIS 무접촉(Yahoo ^KS11).
실행: venv/bin/python scripts/research/index_overlay_h1.py [--out results/index_plus_h1]
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

VOL_THRESHOLD, VOL_TARGET, MIN_MULT = 25.0, 25.0, 0.4   # src/utils/volatility_targeting.py 와 같은 값
COST = 0.0005                                             # |Δw| 1 당
PERIODS = {
    "전체 2001~2026-09": ("2001-01-01", "2026-12-31"),
    "2001~2008": ("2001-01-01", "2008-12-31"),
    "2009~2016": ("2009-01-01", "2016-12-31"),
    "2017~2026-09": ("2017-01-01", "2026-12-31"),
    "참고 2019-06~2026-09": ("2019-06-01", "2026-12-31"),
}


def weights(close: pd.Series, *, ma: int = 200, vol_threshold: float = VOL_THRESHOLD) -> pd.DataFrame:
    """t 일 종가까지만 쓰는 비중(t+1 수익에 적용). 롤링 창이 차기 전은 NaN."""
    r = close.pct_change()
    trend = (close > close.rolling(ma).mean()).astype(float).where(close.rolling(ma).count() == ma)
    vol = r.rolling(20).std() * math.sqrt(252) * 100
    volw = np.where(vol > vol_threshold, np.maximum(MIN_MULT, VOL_TARGET / vol), 1.0)
    volw = pd.Series(volw, index=close.index).where(vol.notna())
    return pd.DataFrame({"B0": 1.0, "H1a": trend, "H1b": volw, "H1c": trend * volw}, index=close.index)


def simulate(close: pd.Series, w: pd.Series, *, cash_annual: float = 0.0) -> pd.Series:
    """일별 전략 수익: w_{t-1}·r_t + (1−w_{t-1})·현금 − 비용·|w_{t-1} − w_{t-2}|."""
    r = close.pct_change()
    held = w.shift(1)
    cash = (1 + cash_annual) ** (1 / 252) - 1
    cost = COST * held.diff().abs().fillna(0.0)
    return (held * r + (1 - held) * cash - cost).dropna()


def stats(ret: pd.Series) -> dict:
    eq = (1 + ret).cumprod()
    years = len(ret) / 252
    cagr = eq.iloc[-1] ** (1 / years) - 1 if years > 0 else float("nan")
    sd = ret.std() * math.sqrt(252)
    return {"days": len(ret), "cagr": cagr, "vol": sd, "sharpe": (ret.mean() * 252) / sd if sd > 0 else None,
            "mdd": float((eq / eq.cummax() - 1).min())}


def load_close(start: str = "2000-01-01", end: str = "2026-09-28") -> pd.Series:
    import FinanceDataReader as fdr
    df = fdr.DataReader("YAHOO:^KS11", start, end)
    c = pd.to_numeric(df["Close"], errors="coerce")
    c = c[c > 0].dropna()          # 사전 등록 §3: NaN·0 이하 행 제외
    c.index = pd.to_datetime(c.index).normalize()
    return c


def run(close: pd.Series) -> dict:
    out = {"source": "YAHOO:^KS11", "first": str(close.index[0].date()), "last": str(close.index[-1].date()),
           "rows": len(close), "main": {}, "cash_2.5": {}, "robust": {}, "turnover": {}}
    w = weights(close)
    variants = {"main": 0.0, "cash_2.5": 0.025}
    for key, cash in variants.items():
        for name in w.columns:
            ret = simulate(close, w[name], cash_annual=cash)
            out[key][name] = {p: stats(ret[a:b]) for p, (a, b) in PERIODS.items()}
    for name in w.columns:
        held = w[name].shift(1).dropna()
        out["turnover"][name] = {"mean_weight": float(held.mean()),
                                 "annual_turnover": float(held.diff().abs().sum() / (len(held) / 252))}
    for label, kw in {"MA150": {"ma": 150}, "MA250": {"ma": 250},
                      "vol20": {"vol_threshold": 20.0}, "vol30": {"vol_threshold": 30.0}}.items():
        wr = weights(close, **kw)
        out["robust"][label] = {n: stats(simulate(close, wr[n])["2001-01-01":]) for n in ("H1a", "H1b", "H1c")}
    return out


def verdict(res: dict) -> dict:
    """사전 등록 §3 판정 (비용 포함·현금 0)."""
    m = res["main"]
    full, thirds = "전체 2001~2026-09", ("2001~2008", "2009~2016", "2017~2026-09")
    out = {}
    for n in ("H1a", "H1b", "H1c"):
        wins = sum(m[n][p]["cagr"] > m["B0"][p]["cagr"] for p in thirds)
        out[n] = {"return_beat": bool(m[n][full]["cagr"] > m["B0"][full]["cagr"] and wins >= 2),
                  "third_wins": int(wins),
                  "risk_beat": bool((m[n][full]["sharpe"] or 0) > (m["B0"][full]["sharpe"] or 0)
                                    and m[n][full]["mdd"] > m["B0"][full]["mdd"])}
    return out


def table(res: dict, key: str = "main") -> str:
    lines = ["| 구간 | 전략 | CAGR | 변동성 | Sharpe | MDD |", "|---|---|---|---|---|---|"]
    for p in PERIODS:
        for n, v in res[key].items():
            s = v[p]
            lines.append(f"| {p} | {n} | {s['cagr']*100:+.2f}% | {s['vol']*100:.1f}% | "
                         f"{(s['sharpe'] or 0):.2f} | {s['mdd']*100:.1f}% |")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="results/index_plus_h1")
    args = ap.parse_args()
    res = run(load_close())
    res["verdict"] = verdict(res)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    (out / "tables.md").write_text("## 본 결과(비용 포함·현금 0)\n\n" + table(res) + "\n\n## 현금 연 2.5%\n\n"
                                   + table(res, "cash_2.5") + "\n", encoding="utf-8")
    print(json.dumps(res["verdict"], ensure_ascii=False))
    print(json.dumps(res["turnover"], ensure_ascii=False))


if __name__ == "__main__":
    main()
