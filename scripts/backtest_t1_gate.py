"""G1 — T1 선행 게이트 백테스트 (비대칭 수확 전략, 2026-08-13)

질문: "D1 확인 진입(트리거+대금 gate)이 D0 종가 진입 대비 증분 edge가 있는가?"
설계: docs/strategies/asymmetric-harvest-strategy.md §6 G1. paired 비교 —
동일 D0 신호에 Arm A(D0 종가 진입) vs Arm B(D1~D3 트리거 확인 진입),
청산은 양팔 동일 (-4% 고정 손절 + 10일→20일 채널 트레일링, 분할익절 없음).

일봉 근사 한계 (문서 명시): 장중 트리거·동시각 대금은 일봉으로 근사
(D일 고가≥트리거 AND 당일 대금≥20일 중앙값 2배). 유증·실적 필터는 과거
데이터 부재로 미반영. 유니버스는 현재 상장 목록 = 생존편향 있음 → 계획상
canary 상한 5%로 상쇄. 이 결과는 G1 게이트 판정 전용이며 수익 예측이 아님.

실행: python scripts/backtest_t1_gate.py [--start 2019-01-01] [--max-universe 400]
출력: 콘솔 리포트 + ~/.cache/ai_trader/backtest/g1_result.json
"""

import argparse
import json
import pickle
import random
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

CACHE = Path.home() / ".cache" / "ai_trader" / "backtest"
RISK_PCT = 4.0          # R 정의: 손절폭 4% = 1R
FEE_RT = 0.6            # 왕복 비용 % (보수적 — 문서 §E)
TRIGGER_BUF = 1.003
GAP_MAX = 3.0           # D1 시가 갭 상한 %
VALUE_GATE_MULT = 2.0   # 대금 gate: 20일 중앙값 대비
PENDING_DAYS = 3        # D1~D3


def load_universe(max_n: int):
    import FinanceDataReader as fdr
    krx = fdr.StockListing("KRX")
    krx = krx[krx["Market"].isin(["KOSPI", "KOSDAQ"])].copy()
    # 우선주/스팩/리츠 휴리스틱 제외
    bad = krx["Name"].str.contains("우$|우B$|스팩|리츠", regex=True, na=False)
    krx = krx[~bad]
    if "Marcap" in krx.columns:
        krx = krx[(krx["Marcap"] >= 1e11) & (krx["Marcap"] <= 5e12)]
        krx = krx.sort_values("Marcap", ascending=False)
    return list(krx["Code"].astype(str).str.zfill(6))[:max_n]


def load_ohlcv(code: str, start: str):
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / f"g1_{code}_{start}.pkl"
    if f.exists():
        try:
            return pickle.load(open(f, "rb"))
        except Exception:
            pass
    try:
        import FinanceDataReader as fdr
        df = fdr.DataReader(code, start)
        if df is None or len(df) < 200:
            return None
        pickle.dump(df, open(f, "wb"))
        return df
    except Exception:
        return None


def prep(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d["value"] = d["Close"] * d["Volume"]
    d["high120"] = d["High"].rolling(120).max().shift(1)
    d["high20"] = d["High"].rolling(20).max().shift(1)
    d["low10"] = d["Low"].rolling(10).min().shift(1)
    d["low20"] = d["Low"].rolling(20).min().shift(1)
    d["ma20"] = d["Close"].rolling(20).mean()
    d["ma60"] = d["Close"].rolling(60).mean()
    d["val_med20"] = d["value"].rolling(20).median().shift(1)
    d["val_avg20"] = d["value"].rolling(20).mean().shift(1)
    d["ret20"] = d["Close"].pct_change(20)
    tr = np.maximum(d["High"] - d["Low"],
                    np.maximum((d["High"] - d["Close"].shift(1)).abs(),
                               (d["Low"] - d["Close"].shift(1)).abs()))
    d["atr14"] = tr.rolling(14).mean()
    d["atr14_prev5"] = d["atr14"].shift(5)
    d["range20"] = (d["High"].rolling(20).max() - d["Low"].rolling(20).min()) / d["Close"]
    return d


def find_d0_signals(d: pd.DataFrame, relaxed: bool = False) -> list:
    """D0: 120일 신고가 돌파 마감 + 대금 3배 + 셋업 필터 (문서 §2 깔때기)

    relaxed=True (1차 실행 표본 20건 — 조합 과협착 판명):
    베이스 고저폭·ATR 수축 제거, 평균 대금 100억→30억. 핵심 정체성
    (신고가 돌파·대금 폭증·복권필터·정배열)은 유지.
    """
    sig = []
    c = d["Close"]
    cond = (
        (c > d["high120"])
        & (d["value"] >= 3 * d["val_avg20"])
        & (d["val_avg20"] >= (3e9 if relaxed else 1e10))
        & (c >= 3000)
        & (d["ret20"] <= 0.60)                          # 복권 필터
        & (c > d["ma20"]) & (d["ma20"] > d["ma60"])     # 정배열
    )
    if not relaxed:
        cond = cond & (d["range20"] <= 0.25) & (d["atr14"] <= d["atr14_prev5"])
    idx = np.where(cond.fillna(False).to_numpy())[0]
    last_i = -10**9
    for i in idx:
        if i - last_i < 10:   # 동일 종목 10일 내 중복 신호 억제
            continue
        if i + PENDING_DAYS + 2 >= len(d):
            continue
        sig.append(i)
        last_i = i
    return sig


def simulate_exit(d: pd.DataFrame, ei: int, entry: float) -> float:
    """공통 청산: -4% 고정 → 채널 트레일링(10d, +30% 후 20d). 반환: 순수익% (비용 차감)"""
    stop = entry * (1 - RISK_PCT / 100)
    runner = False
    n = len(d)
    lows, highs, closes = d["Low"].to_numpy(), d["High"].to_numpy(), d["Close"].to_numpy()
    low10, low20 = d["low10"].to_numpy(), d["low20"].to_numpy()
    opens = d["Open"].to_numpy()
    exit_px = closes[n - 1]
    for j in range(ei + 1, n):
        # 갭 관통: 시가가 손절선 아래면 시가 체결
        if opens[j] <= stop:
            exit_px = opens[j]
            break
        if lows[j] <= stop:
            exit_px = stop
            break
        if highs[j] >= entry * 1.30:
            runner = True
        ch = low20[j] if runner else low10[j]
        if not np.isnan(ch):
            ch = max(ch, stop)  # 채널이 손절선 아래면 손절선 유지
            if closes[j] < ch:
                exit_px = closes[j]
                break
            stop = max(stop, ch * 0.999)  # 채널을 사실상의 스탑으로 승급
    return (exit_px - entry) / entry * 100 - FEE_RT


def simulate_exit_engine(d: pd.DataFrame, ei: int, entry: float) -> float:
    """현행 엔진 청산 근사 (G2 비교군): 분할익절 +10%/10%·+15%/잔여50%·+25%/잔여50%
    + 고점 -4.5% 트레일링(+5% 활성) + 보유 20영업일 상한. 손절 -4% (채널군과 동일 R).
    반환: 가중평균 순수익% (비용 차감)"""
    stop = entry * 0.96
    remaining = 1.0
    realized = 0.0  # sum(비중 × 수익%)
    stage = 0
    highest = entry
    n = len(d)
    end = min(ei + 20, n - 1)  # 보유 상한 20영업일
    opens, highs, lows, closes = (d["Open"].to_numpy(), d["High"].to_numpy(),
                                  d["Low"].to_numpy(), d["Close"].to_numpy())
    for j in range(ei + 1, end + 1):
        # 1) 손절 (갭 관통 포함)
        px = opens[j] if opens[j] <= stop else (stop if lows[j] <= stop else None)
        if px is not None:
            realized += remaining * (px - entry) / entry * 100
            remaining = 0.0
            break
        highest = max(highest, highs[j])
        # 2) 분할 익절 (지정가 체결 근사)
        for tgt, ratio, st in ((1.10, 0.10, 1), (1.15, 0.50, 2), (1.25, 0.50, 3)):
            if stage < st and highs[j] >= entry * tgt and remaining > 0:
                sell = remaining * ratio if st > 1 else 0.10  # 1차는 총량의 10%
                sell = min(sell, remaining)
                realized += sell * (entry * tgt - entry) / entry * 100
                remaining -= sell
                stage = st
        # 3) 트레일링 (+5% 활성, 고점 -4.5%)
        if highest >= entry * 1.05 and remaining > 0:
            trail = highest * 0.955
            if lows[j] <= trail:
                realized += remaining * (max(trail, stop) - entry) / entry * 100
                remaining = 0.0
                break
    if remaining > 0:
        realized += remaining * (closes[end] - entry) / entry * 100
    return realized - FEE_RT


def _regime_ok_dates(start: str) -> set:
    """체제 게이트 (전략 §2A): KOSPI 지수 종가 > 20일선인 날짜 집합 (D0 판정용)"""
    import FinanceDataReader as fdr
    ks = fdr.DataReader("KS11", start)
    ma20 = ks["Close"].rolling(20).mean()
    ok = ks.index[ks["Close"] > ma20]
    return {str(d)[:10] for d in ok}


def run_g2(start: str, max_universe: int, holdout_months: int = 18,
           regime_gate: bool = True):
    """G2: 진입 B 고정, 청산 A/B (채널 vs 현행 엔진) + 홀드아웃/MDD/연패
    regime_gate: 전략 §2A 체제 게이트 — D0가 지수 20일선 위인 날만 신호 채택"""
    codes = load_universe(max_universe)
    ok_dates = _regime_ok_dates(start) if regime_gate else None
    print(f"[G2] 유니버스 {len(codes)}종목, 진입 B 고정, "
          f"홀드아웃 {holdout_months}개월, 체제게이트={regime_gate}")
    rows = []
    for k, code in enumerate(codes):
        df = load_ohlcv(code, start)
        if df is None:
            continue
        d = prep(df)
        for i in find_d0_signals(d, relaxed=True):
            if ok_dates is not None and str(d.index[i])[:10] not in ok_dates:
                continue  # 체제 게이트: 지수 20일선 아래 D0는 미채택
            d0_close = d["Close"].iloc[i]
            trigger = max(d["High"].iloc[i], d["high20"].iloc[i]) * TRIGGER_BUF
            for j in range(i + 1, min(i + 1 + PENDING_DAYS, len(d))):
                gap = (d["Open"].iloc[j] - d0_close) / d0_close * 100
                if gap > GAP_MAX:
                    continue
                if (d["High"].iloc[j] >= trigger
                        and d["value"].iloc[j] >= VALUE_GATE_MULT * d["val_med20"].iloc[j]):
                    entry = max(d["Open"].iloc[j], trigger)
                    rows.append({
                        "date": str(d.index[j])[:10],
                        "ch": simulate_exit(d, j, entry) / RISK_PCT,
                        "en": simulate_exit_engine(d, j, entry) / RISK_PCT,
                    })
                    break
        if (k + 1) % 100 == 0:
            print(f"  {k + 1}/{len(codes)}, 체결 {len(rows)}건")

    t = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
    cutoff = (pd.Timestamp.now() - pd.DateOffset(months=holdout_months)).strftime("%Y-%m-%d")

    def seq_stats(s: pd.Series) -> dict:
        s = s.astype(float)
        cum = s.cumsum()
        mdd = float((cum.cummax() - cum).max())  # R 단위 MDD
        streak = best = 0
        for v in s:
            streak = streak + 1 if v < 0 else 0
            best = max(best, streak)
        top10_n = max(1, int(len(s) * 0.1))
        total = s.sum()
        return {
            "n": len(s), "expectancy_R": round(s.mean(), 3),
            "win_rate": round((s > 0).mean() * 100, 1),
            "total_R": round(total, 1), "mdd_R": round(mdd, 1),
            "max_losing_streak": best,
            "top10pct_contrib": round(s.nlargest(top10_n).sum() / total * 100, 1)
            if total > 0 else None,
        }

    ins, oos = t[t["date"] < cutoff], t[t["date"] >= cutoff]
    result = {
        "run_at": datetime.now().isoformat(), "entry_policy": "B(trigger+value gate)",
        "holdout_cutoff": cutoff, "n_total": len(t),
        "in_sample": {"channel": seq_stats(ins["ch"]), "engine": seq_stats(ins["en"])},
        "holdout": {"channel": seq_stats(oos["ch"]), "engine": seq_stats(oos["en"])},
        "gate_criteria": {"oos_min_R": 0.15, "mdd_note": "R단위 — 자본% 환산은 리스크 1%/건 곱"},
    }
    ch_oos = result["holdout"]["channel"]["expectancy_R"]
    en_oos = result["holdout"]["engine"]["expectancy_R"]
    result["verdict"] = {
        "oos_channel_ok": bool(ch_oos >= 0.15),
        "oos_engine_ok": bool(en_oos >= 0.15),
        "winner_exit": "channel" if ch_oos >= en_oos else "engine",
    }
    out = CACHE / "g2_result.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=1))
    print(f"\n저장: {out}")


def run(start: str, max_universe: int, relaxed: bool = False):
    codes = load_universe(max_universe)
    print(f"유니버스 {len(codes)}종목, 기간 {start}~ (relaxed={relaxed})")
    trades = []
    n_signals = 0
    for k, code in enumerate(codes):
        df = load_ohlcv(code, start)
        if df is None:
            continue
        d = prep(df)
        for i in find_d0_signals(d, relaxed=relaxed):
            n_signals += 1
            d0_close = d["Close"].iloc[i]
            date = str(d.index[i])[:10]
            # Arm A: D0 종가 진입
            ra = simulate_exit(d, i, d0_close) / RISK_PCT
            # Arm B: D1~D3 트리거 확인 진입
            trigger = max(d["High"].iloc[i], d["high20"].iloc[i]) * TRIGGER_BUF
            rb = None
            for j in range(i + 1, min(i + 1 + PENDING_DAYS, len(d))):
                gap = (d["Open"].iloc[j] - d0_close) / d0_close * 100
                if gap > GAP_MAX:
                    continue  # 과갭 — 당일 금지, pending 유지
                if (d["High"].iloc[j] >= trigger
                        and d["value"].iloc[j] >= VALUE_GATE_MULT * d["val_med20"].iloc[j]):
                    entry_b = max(d["Open"].iloc[j], trigger)
                    rb = simulate_exit(d, j, entry_b) / RISK_PCT
                    break
            # Arm C (look-ahead 없는 실행 가능 변형): D1 시가가 -2%~+3% 범위면
            # D1 시가 진입 — "확인"을 기다리지 않고 과갭만 회피
            rc = None
            if i + 1 < len(d):
                gap1 = (d["Open"].iloc[i + 1] - d0_close) / d0_close * 100
                if -2.0 <= gap1 <= GAP_MAX:
                    rc = simulate_exit(d, i + 1, d["Open"].iloc[i + 1]) / RISK_PCT
            trades.append({"code": code, "date": date, "year": date[:4],
                           "a": round(ra, 3),
                           "b": round(rb, 3) if rb is not None else None,
                           "c": round(rc, 3) if rc is not None else None})
        if (k + 1) % 50 == 0:
            print(f"  {k + 1}/{len(codes)} 종목, 신호 {n_signals}건")

    df_t = pd.DataFrame(trades)
    if df_t.empty:
        print("신호 없음")
        return
    filled = df_t.dropna(subset=["b"])
    fill_rate = len(filled) / len(df_t) * 100 if len(df_t) else 0.0

    def stats(s):
        s = s.astype(float)
        top10_n = max(1, int(len(s) * 0.1))
        top = s.nlargest(top10_n).sum()
        total = s.sum()
        return {
            "n": len(s), "expectancy_R": round(s.mean(), 3),
            "median_R": round(s.median(), 3),
            "win_rate": round((s > 0).mean() * 100, 1),
            "top10pct_contrib": round(top / total * 100, 1) if total > 0 else None,
            "total_R": round(total, 1),
        }

    diffs = (filled["b"] - filled["a"]).astype(float).to_numpy()
    rng = random.Random(42)
    boots = []
    for _ in range(5000):
        sample = [diffs[rng.randrange(len(diffs))] for _ in range(len(diffs))]
        boots.append(np.mean(sample))
    ci = (float(np.percentile(boots, 5)), float(np.percentile(boots, 95)))

    yearly = {}
    for y, g in filled.groupby("year"):
        yearly[y] = {"n": len(g), "paired_diff": round(float((g["b"] - g["a"]).mean()), 3)}

    result = {
        "run_at": datetime.now().isoformat(), "start": start, "relaxed": relaxed,
        "universe": len(codes), "n_signals": len(df_t),
        "fill_rate_pct": round(fill_rate, 1),
        "arm_a_all": stats(df_t["a"]),
        "arm_a_on_filled": stats(filled["a"]),
        "arm_b": stats(filled["b"]),
        "arm_c_d1_open": stats(df_t.dropna(subset=["c"])["c"]),
        "arm_c_fill_rate_pct": round(df_t["c"].notna().mean() * 100, 1),
        "paired_diff_mean_R": round(float(np.mean(diffs)), 3),
        "paired_diff_ci90": [round(ci[0], 3), round(ci[1], 3)],
        "yearly": yearly,
        "gate_criteria": {
            "min_fills": 150, "min_paired_improvement_R": 0.10,
            "ci_lower_gt": 0.0, "fill_rate_range": [20, 70],
        },
        "verdict": {
            "fills_ok": bool(len(filled) >= 150),
            "improvement_ok": bool(float(np.mean(diffs)) >= 0.10),
            "ci_ok": bool(ci[0] > 0.0),
            "fill_rate_ok": bool(20 <= fill_rate <= 70),
        },
    }
    result["verdict"]["PASS"] = all(result["verdict"].values())

    out = CACHE / ("g1_result_relaxed.json" if relaxed else "g1_result.json")
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=1))
    print(f"\n저장: {out}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--start", default="2019-01-01")
    p.add_argument("--max-universe", type=int, default=400)
    p.add_argument("--relaxed", action="store_true")
    p.add_argument("--g2", action="store_true", help="G2: 진입 B 고정 + 청산 A/B")
    a = p.parse_args()
    if a.g2:
        sys.exit(run_g2(a.start, a.max_universe))
    sys.exit(run(a.start, a.max_universe, relaxed=a.relaxed))
