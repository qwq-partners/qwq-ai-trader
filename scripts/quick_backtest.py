#!/usr/bin/env python3
"""
신규 전략 아이디어 1차 스크리닝 도구 (연구 전용)
================================================

정식 미러링 백테스터(backtest_strategies.py)에 올리기 **전에** 아이디어를
빠르게 기각/채택하기 위한 벡터 백테스터. 엔진 로직을 모사하지 않는다.

⚠️ 반드시 연구 venv로 실행 (운영 venv에는 vectorbt/numba가 없다 — numpy 충돌 방지):
    ./venv-research/bin/python scripts/quick_backtest.py --idea <이름>

깔때기: 아이디어 → [이 도구] → 통과 시 backtest_strategies.py 정식 구현 → BacktestGate

내장 아이디어:
  earnings_reversal  US 어닝 발표 전 낙폭과대 → 발표 후 반등 (이벤트 스터디)
  tom                turn-of-month 윈도우 수익률 검증 (SPY / KOSPI)
  lowvol             KR 저변동성 quintile → 20일 포워드 수익률 (감점 임계값 검증)
"""

import argparse
import glob
import json
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
KR_CACHE_DIR = Path.home() / ".cache" / "ai_trader" / "backtest"


def _load_env_key(name: str) -> str:
    """운영 .env에서 키 하나만 읽는다 (연구 venv에 dotenv 없음)"""
    if os.getenv(name):
        return os.environ[name]
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip()
    return ""


# ════════════════════════════════════════════════════════════════
# 아이디어 1: US 어닝 발표 리버설 (이벤트 스터디)
# ════════════════════════════════════════════════════════════════

def _fetch_earnings_calendar(months: int) -> pd.DataFrame:
    """finnhub 어닝 캘린더 (월 단위 청크, 무료 tier 60call/min 이내)"""
    import requests

    key = _load_env_key("FINNHUB_API_KEY")
    if not key:
        sys.exit("FINNHUB_API_KEY 없음 — .env 확인")

    end = date.today()
    start = end - timedelta(days=months * 30)
    rows = []
    cur = start
    while cur < end:
        chunk_end = min(cur + timedelta(days=30), end)
        r = requests.get(
            "https://finnhub.io/api/v1/calendar/earnings",
            params={"from": cur.isoformat(), "to": chunk_end.isoformat(), "token": key},
            timeout=15,
        )
        r.raise_for_status()
        for it in r.json().get("earningsCalendar", []):
            if it.get("symbol") and it.get("date"):
                rows.append({
                    "symbol": it["symbol"].strip(),
                    "date": it["date"],
                    "eps_actual": it.get("epsActual"),
                    "eps_estimate": it.get("epsEstimate"),
                    "hour": it.get("hour", ""),   # bmo(장전)/amc(장후)
                })
        print(f"  캘린더 {cur} ~ {chunk_end}: 누적 {len(rows)}건")
        cur = chunk_end + timedelta(days=1)
        time.sleep(1.1)  # rate limit 여유
    return pd.DataFrame(rows)


def _fetch_earnings_dates_yf(symbols, months: int) -> pd.DataFrame:
    """yfinance 종목별 어닝 이력 (finnhub 무료 tier는 과거 ~1개월만 제공).

    종목당 1 API 호출 — 디스크 캐시(7일)로 재실행 비용 제거.
    """
    import yfinance as yf

    cache_path = KR_CACHE_DIR.parent / "research" / "yf_earnings_dates.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache = {}
    if cache_path.exists() and time.time() - cache_path.stat().st_mtime < 7 * 86400:
        cache = json.loads(cache_path.read_text())

    cutoff = pd.Timestamp(date.today() - timedelta(days=months * 30))
    rows = []
    fetched = 0
    for i, sym in enumerate(sorted(symbols)):
        if sym in cache:
            dates = cache[sym]
        else:
            try:
                ed = yf.Ticker(sym).get_earnings_dates(limit=12)
                dates = (
                    [] if ed is None or ed.empty
                    else [str(d.date()) for d in ed.index.tz_localize(None)]
                )
            except Exception:
                dates = []
            cache[sym] = dates
            fetched += 1
            time.sleep(0.1)
            if fetched % 50 == 0:
                print(f"  yf 어닝 이력 {i+1}/{len(symbols)}종목 진행")
                cache_path.write_text(json.dumps(cache))
        for ds in dates:
            if pd.Timestamp(ds) >= cutoff and pd.Timestamp(ds) <= pd.Timestamp(date.today()):
                # yfinance는 발표 시각을 안 주므로 hour는 공란 (보수적으로 당일 반응 가정)
                rows.append({"symbol": sym, "date": ds, "hour": ""})
    cache_path.write_text(json.dumps(cache))
    return pd.DataFrame(rows)


def idea_earnings_reversal(months: int, pre_drop_pct: float, hold_days: int,
                           source: str = "yf"):
    """
    논문: Reversal During Earnings-Announcements (재현 Sharpe 0.785)
    발표 직전 5거래일 낙폭이 큰 종목을 발표 전일 종가 매수 → 발표 후 N일 종가 매도.

    검증 설계:
      - 유니버스: S&P500 (FDR 리스트)
      - 낙폭과대군(pre5d ≤ -pre_drop_pct) vs 전체 vs 급등군(≥ +pre_drop_pct) 비교
      - 발표 시각(bmo/amc)에 따라 발표일 정렬: amc(장후)면 반응일은 D+1
      - source=yf: 종목별 어닝 이력(수년치) / finnhub: 캘린더(무료 tier는 ~1개월)
    """
    import FinanceDataReader as fdr
    import yfinance as yf

    print("=" * 60)
    print(f"어닝 리버설 이벤트 스터디 ({months}개월, 낙폭 {pre_drop_pct}%, "
          f"보유 {hold_days}일, source={source})")
    print("=" * 60)

    sp500 = set(fdr.StockListing("S&P500")["Symbol"].str.replace(".", "-", regex=False))
    print(f"S&P500 유니버스: {len(sp500)}종목")

    if source == "finnhub":
        cal = _fetch_earnings_calendar(months)
        cal["symbol"] = cal["symbol"].str.replace(".", "-", regex=False)
        cal = cal[cal["symbol"].isin(sp500)]
    else:
        cal = _fetch_earnings_dates_yf(sp500, months)
    cal = cal.drop_duplicates(["symbol", "date"])
    print(f"S&P500 어닝 이벤트: {len(cal)}건")

    start = (date.today() - timedelta(days=months * 30 + 40)).isoformat()
    px = yf.download(
        sorted(cal["symbol"].unique()), start=start, auto_adjust=True,
        progress=False, threads=True,
    )["Close"]
    print(f"가격 데이터: {px.shape[1]}종목 × {px.shape[0]}일")

    events = []
    for _, ev in cal.iterrows():
        sym = ev["symbol"]
        if sym not in px.columns:
            continue
        s = px[sym].dropna()
        if len(s) < 30:
            continue
        ann = pd.Timestamp(ev["date"])
        # 발표 반응일: 장후(amc) 발표면 다음 거래일이 반응일
        pos_arr = s.index.searchsorted(ann)
        if ev["hour"] == "amc":
            pos_arr += 1
        if pos_arr < 7 or pos_arr + hold_days >= len(s):
            continue
        # 진입 = 반응일 전일 종가, 청산 = 반응일 + (hold_days-1) 종가
        entry_pos = pos_arr - 1
        exit_pos = pos_arr + hold_days - 1
        pre5 = (s.iloc[entry_pos] / s.iloc[entry_pos - 5] - 1) * 100
        ret = (s.iloc[exit_pos] / s.iloc[entry_pos] - 1) * 100
        events.append({"symbol": sym, "date": ev["date"], "pre5": pre5, "ret": ret})

    df = pd.DataFrame(events)
    if df.empty:
        sys.exit("이벤트 없음 — 데이터 확인")

    def _bucket_stats(mask, label):
        sub = df[mask]
        if len(sub) < 5:
            print(f"  {label:<24} n={len(sub)} (표본 부족)")
            return
        t = sub["ret"].mean() / (sub["ret"].std() / np.sqrt(len(sub)))
        print(f"  {label:<24} n={len(sub):>4} | 평균 {sub['ret'].mean():+.2f}% | "
              f"중앙값 {sub['ret'].median():+.2f}% | 승률 {(sub['ret'] > 0).mean()*100:.1f}% | "
              f"t={t:.2f}")

    print(f"\n총 이벤트 {len(df)}건 — 발표 후 {hold_days}일 보유 수익률:")
    _bucket_stats(df["pre5"] <= -pre_drop_pct, f"낙폭과대 (pre5d≤-{pre_drop_pct}%)")
    _bucket_stats(df["pre5"] <= -3.0, "낙폭 (pre5d≤-3%)")
    _bucket_stats(pd.Series(True, index=df.index), "전체 (베이스라인)")
    _bucket_stats(df["pre5"] >= pre_drop_pct, f"급등 (pre5d≥+{pre_drop_pct}%)")

    out = PROJECT_ROOT / "results" / f"quickbt_earnings_reversal_{date.today():%Y%m%d}.csv"
    out.parent.mkdir(exist_ok=True)
    df.to_csv(out, index=False)
    print(f"\n이벤트 상세 저장: {out}")
    print("판정 가이드: 낙폭과대군 평균이 전체 대비 유의하게(+, t≥2) 높아야 채택")


# ════════════════════════════════════════════════════════════════
# 아이디어 1-B: US 어닝 드리프트 (EPS beat + 갭업 → 추세 지속)
# ════════════════════════════════════════════════════════════════

def _fetch_earnings_history_yf(symbols, months: int) -> pd.DataFrame:
    """yfinance 어닝 이력 v2 — 발표 시각 + EPS 서프라이즈 포함 (7일 디스크 캐시)"""
    import yfinance as yf

    cache_path = KR_CACHE_DIR.parent / "research" / "yf_earnings_history_v2.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache = {}
    if cache_path.exists() and time.time() - cache_path.stat().st_mtime < 7 * 86400:
        cache = json.loads(cache_path.read_text())

    cutoff = pd.Timestamp(date.today() - timedelta(days=months * 30))
    rows = []
    fetched = 0
    for i, sym in enumerate(sorted(symbols)):
        if sym in cache:
            recs = cache[sym]
        else:
            try:
                ed = yf.Ticker(sym).get_earnings_dates(limit=12)
                recs = []
                if ed is not None and not ed.empty:
                    for ts, r in ed.iterrows():
                        recs.append([
                            str(ts.tz_localize(None)),
                            None if pd.isna(r.get("EPS Estimate")) else float(r["EPS Estimate"]),
                            None if pd.isna(r.get("Reported EPS")) else float(r["Reported EPS"]),
                            None if pd.isna(r.get("Surprise(%)")) else float(r["Surprise(%)"]),
                        ])
            except Exception:
                recs = []
            cache[sym] = recs
            fetched += 1
            time.sleep(0.1)
            if fetched % 50 == 0:
                print(f"  yf 어닝 이력 v2 {i+1}/{len(symbols)}종목 진행")
                cache_path.write_text(json.dumps(cache))
        for ts_str, est, act, surp in recs:
            ts = pd.Timestamp(ts_str)
            if act is None or ts < cutoff or ts > pd.Timestamp.now():
                continue
            rows.append({
                "symbol": sym,
                "dt": ts,
                # 15시 이후 발표 = 장후(amc) → 반응일은 다음 거래일
                "amc": ts.hour >= 15,
                "surprise": surp,
            })
    cache_path.write_text(json.dumps(cache))
    return pd.DataFrame(rows)


def idea_earnings_drift(months: int, hold_days: int):
    """
    pending-decisions #6 검증: EPS beat + 발표 반응 갭업 → 이후 드리프트가 있는가.
    실전략(earnings_drift)의 진입 조건을 미러링: 반응일 갭업 + 종가가 시가 위(갭 유지).

    버킷: 갭 임계(3/5/7%) × EPS beat(≥10%) 조합 vs 베이스라인.
    수익률은 반응일 종가 매수 → hold_days 거래일 후 종가 매도.
    """
    import FinanceDataReader as fdr
    import yfinance as yf

    print("=" * 60)
    print(f"어닝 드리프트 검증 ({months}개월, 보유 {hold_days}일)")
    print("=" * 60)

    sp500 = set(fdr.StockListing("S&P500")["Symbol"].str.replace(".", "-", regex=False))
    ev = _fetch_earnings_history_yf(sp500, months)
    print(f"어닝 이벤트 (EPS 발표 완료): {len(ev)}건")

    start = (date.today() - timedelta(days=months * 30 + 40)).isoformat()
    px = yf.download(
        sorted(ev["symbol"].unique()), start=start, auto_adjust=True,
        progress=False, threads=True,
    )
    opens, closes = px["Open"], px["Close"]
    print(f"가격 데이터: {closes.shape[1]}종목 × {closes.shape[0]}일")

    events = []
    for _, e in ev.iterrows():
        sym = e["symbol"]
        if sym not in closes.columns:
            continue
        c = closes[sym].dropna()
        o = opens[sym].dropna()
        if len(c) < 30:
            continue
        ann_day = pd.Timestamp(e["dt"].date())
        pos = c.index.searchsorted(ann_day)
        if e["amc"]:
            pos += 1          # 장후 발표 → 다음 거래일이 반응일
        elif pos < len(c) and c.index[pos] != ann_day:
            pass              # 발표일이 휴장이면 다음 거래일이 반응일 (searchsorted가 처리)
        if pos < 1 or pos + hold_days >= len(c):
            continue
        r_date = c.index[pos]
        if r_date not in o.index:
            continue
        prev_close = c.iloc[pos - 1]
        r_open, r_close = o.loc[r_date], c.iloc[pos]
        if prev_close <= 0 or r_open <= 0:
            continue
        gap = (r_open / prev_close - 1) * 100
        held = r_close > r_open   # 갭 유지 확인 (실전략 조건 미러링)
        fwd = (c.iloc[pos + hold_days] / r_close - 1) * 100
        events.append({
            "symbol": sym, "date": str(r_date.date()), "gap": gap,
            "held": held, "surprise": e["surprise"], "fwd": fwd,
        })

    df = pd.DataFrame(events)
    if df.empty:
        sys.exit("이벤트 없음")

    def _stat(mask, label):
        sub = df[mask]
        if len(sub) < 10:
            print(f"  {label:<38} n={len(sub)} (표본 부족)")
            return
        t = sub["fwd"].mean() / (sub["fwd"].std() / np.sqrt(len(sub)))
        print(f"  {label:<38} n={len(sub):>4} | 평균 {sub['fwd'].mean():+.2f}% | "
              f"승률 {(sub['fwd'] > 0).mean()*100:.1f}% | t={t:.2f}")

    beat = df["surprise"].notna() & (df["surprise"] >= 10)
    print(f"\n총 이벤트 {len(df)}건 — 반응일 종가 매수 후 {hold_days}일 보유:")
    _stat(pd.Series(True, index=df.index), "전체 (베이스라인)")
    _stat(beat, "EPS beat ≥10%")
    _stat(df["gap"] >= 3, "갭업 ≥3%")
    for g in (3.0, 5.0, 7.0):
        _stat(beat & (df["gap"] >= g) & df["held"], f"EPS beat≥10% + 갭≥{g:.0f}% + 갭유지")
    _stat(beat & (df["gap"] >= 3) & ~df["held"], "EPS beat≥10% + 갭≥3% + 갭붕괴(반례)")
    _stat(df["surprise"].notna() & (df["surprise"] < 0), "EPS miss (<0)")

    out = PROJECT_ROOT / "results" / f"quickbt_earnings_drift_{date.today():%Y%m%d}.csv"
    out.parent.mkdir(exist_ok=True)
    df.to_csv(out, index=False)
    print(f"\n이벤트 상세 저장: {out}")
    print("판정 가이드: 'beat+갭업+갭유지' 버킷이 베이스라인 대비 유의(+, t≥2)해야 재활성화")


# ════════════════════════════════════════════════════════════════
# 아이디어 3: KR 밸류코어 (가치/성장 버킷) — value-growth-core-design.md Phase 0
# ════════════════════════════════════════════════════════════════

_DART_ACCOUNTS = {"자산총계", "부채총계", "자본총계", "매출액", "영업이익", "당기순이익"}


def _load_dart_corp_map() -> dict:
    """운영 캐시(dart_corp_code.xml)에서 종목코드→corp_code 매핑 로드"""
    import xml.etree.ElementTree as ET

    xml_path = Path.home() / ".cache" / "ai_trader" / "dart_corp_code.xml"
    if not xml_path.exists():
        sys.exit(f"corp_code 캐시 없음: {xml_path} — 운영 봇이 생성한 파일 필요")
    mapping = {}
    for el in ET.parse(xml_path).getroot().iter("list"):
        stock = (el.findtext("stock_code") or "").strip()
        corp = (el.findtext("corp_code") or "").strip()
        if len(stock) == 6 and corp:
            mapping[stock] = corp
    return mapping


def _fetch_kr_financials_dart(symbols, years) -> pd.DataFrame:
    """DART fnlttSinglAcnt 연간 재무 (CFS 우선, 30일 디스크 캐시).

    실운용 FinancialsProvider와 동일 소스 — 백테스트/실전 데이터 정합성 확보.
    """
    import requests

    key = _load_env_key("DART_API_KEY")
    if not key:
        sys.exit("DART_API_KEY 없음 — .env 확인")

    cache_path = KR_CACHE_DIR.parent / "research" / "dart_financials.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache = {}
    if cache_path.exists() and time.time() - cache_path.stat().st_mtime < 30 * 86400:
        cache = json.loads(cache_path.read_text())

    corp_map = _load_dart_corp_map()
    rows, fetched = [], 0
    total = len(symbols) * len(years)
    for sym in sorted(symbols):
        corp = corp_map.get(sym)
        if not corp:
            continue
        for yr in years:
            ck = f"{sym}_{yr}"
            if ck in cache:
                rec = cache[ck]
            else:
                rec = {}
                try:
                    r = requests.get(
                        "https://opendart.fss.or.kr/api/fnlttSinglAcnt.json",
                        params={"crtfc_key": key, "corp_code": corp,
                                "bsns_year": str(yr), "reprt_code": "11011"},
                        timeout=15,
                    )
                    data = r.json()
                    if data.get("status") == "000":
                        # CFS(연결) 우선, 없으면 OFS(별도)
                        for fs_div in ("CFS", "OFS"):
                            got = {}
                            for it in data.get("list", []):
                                if it.get("fs_div") != fs_div:
                                    continue
                                nm = it.get("account_nm", "").strip()
                                # 실측: 순이익 계정명은 "당기순이익(손실)" — 접두 정규화
                                if nm.startswith("당기순이익"):
                                    nm = "당기순이익"
                                if nm in _DART_ACCOUNTS:
                                    try:
                                        got[nm] = float(
                                            it["thstrm_amount"].replace(",", ""))
                                    except (ValueError, KeyError, AttributeError):
                                        pass
                            if "자본총계" in got:
                                rec = got
                                break
                except Exception:
                    rec = {}
                cache[ck] = rec
                fetched += 1
                time.sleep(0.06)   # DART 분당 1,000건 한도 내
                if fetched % 200 == 0:
                    print(f"  DART 재무 조회 {fetched}건 (계 {total})")
                    cache_path.write_text(json.dumps(cache, ensure_ascii=False))
            if rec.get("자본총계"):
                rows.append({
                    "symbol": sym, "year": yr,
                    "equity": rec.get("자본총계"), "liab": rec.get("부채총계"),
                    "revenue": rec.get("매출액"), "op": rec.get("영업이익"),
                    "net": rec.get("당기순이익"),
                })
    cache_path.write_text(json.dumps(cache, ensure_ascii=False))
    return pd.DataFrame(rows)


def _kr_value_growth_base(top_n: int = 300):
    """공통 데이터 준비: 유니버스·재무·가격·리밸런스 시점별 지표 테이블"""
    import FinanceDataReader as fdr
    import yfinance as yf

    listing = fdr.StockListing("KOSPI")
    listing = listing[listing["Code"].str.endswith("0")]        # 보통주만
    listing = listing.nlargest(top_n, "Marcap")
    shares = dict(zip(listing["Code"], listing["Stocks"]))
    print(f"유니버스: KOSPI 시총 상위 {len(listing)}종목 "
          f"(⚠️ 현재 시점 구성 — 생존 편향 존재, 1차 스크리닝 한정)")

    years = list(range(2019, 2026))
    fin = _fetch_kr_financials_dart(listing["Code"].tolist(), years)
    print(f"DART 재무: {fin['symbol'].nunique()}종목 × {len(years)}개년, {len(fin)}행")

    tickers = [f"{c}.KS" for c in fin["symbol"].unique()]
    px = yf.download(tickers, start="2020-06-01", auto_adjust=True,
                     progress=False, threads=True)["Close"]
    px.columns = [c.replace(".KS", "") for c in px.columns]
    px = px.dropna(axis=1, how="all")
    print(f"가격 데이터: {px.shape[1]}종목 × {px.shape[0]}일")

    f = fin.set_index(["symbol", "year"])

    def _get(sym, yr, col):
        try:
            v = f.at[(sym, yr), col]
            return None if pd.isna(v) else v
        except KeyError:
            return None

    # 리밸런스: 매년 4/1 (직전 사업연도 보고서 제출 마감 직후) → 12개월 보유
    frames = []
    for ry in range(2021, 2026):
        rb = pd.Timestamp(f"{ry}-04-01")
        pos0 = px.index.searchsorted(rb)
        pos1 = px.index.searchsorted(rb + pd.DateOffset(years=1))
        if pos1 >= len(px.index):
            pos1 = len(px.index) - 1
        d0, d1 = px.index[pos0], px.index[pos1]
        fy = ry - 1
        for sym in px.columns:
            p0, p1 = px[sym].get(d0), px[sym].get(d1)
            eq, net = _get(sym, fy, "equity"), _get(sym, fy, "net")
            if any(v is None or pd.isna(v) or v <= 0 for v in (p0, eq)) \
                    or p1 is None or pd.isna(p1):
                continue
            net_p, liab = _get(sym, fy - 1, "net"), _get(sym, fy, "liab")
            rev, rev_p = _get(sym, fy, "revenue"), _get(sym, fy - 1, "revenue")
            op, op_p = _get(sym, fy, "op"), _get(sym, fy - 1, "op")
            mcap = p0 * shares.get(sym, 0)
            if mcap <= 0:
                continue
            frames.append({
                "year": ry, "symbol": sym,
                "bm": eq / mcap,                               # B/M (PBR 역수)
                "per": (mcap / net) if net and net > 0 else None,
                "roe": (net / eq * 100) if net is not None else None,
                "debt": (liab / eq * 100) if liab is not None else None,
                "profit2y": bool(net and net > 0 and net_p and net_p > 0),
                "rev_yoy": (rev / rev_p - 1) * 100 if rev and rev_p and rev_p > 0 else None,
                "op_yoy": (op / op_p - 1) * 100 if op and op_p and op_p > 0 else None,
                "fwd": (p1 / p0 - 1) * 100,                    # 12개월 포워드
            })
    df = pd.DataFrame(frames)
    print(f"종목-연도 관측치: {len(df)}건 ({df['year'].nunique()}개 리밸런스)")
    return df


def _vg_stat(df, mask, label):
    sub = df[mask]
    if len(sub) < 20:
        print(f"  {label:<40} n={len(sub)} (표본 부족)")
        return None
    t = sub["fwd"].mean() / (sub["fwd"].std() / np.sqrt(len(sub)))
    yearly = sub.groupby("year")["fwd"].mean()
    base_yearly = df.groupby("year")["fwd"].mean()
    wins = int((yearly > base_yearly.reindex(yearly.index)).sum())
    print(f"  {label:<40} n={len(sub):>4} | 평균 {sub['fwd'].mean():+.2f}% | "
          f"승률 {(sub['fwd'] > 0).mean()*100:.1f}% | t={t:.2f} | "
          f"연도승 {wins}/{len(yearly)}")
    return sub["fwd"].mean()


def idea_kr_value(top_n: int):
    """가치 버킷: B/M 분위 × 퀄리티(2년 흑자 + 부채<200%) — 12개월 포워드"""
    df = _kr_value_growth_base(top_n)
    quality = df["profit2y"] & df["debt"].notna() & (df["debt"] < 200)

    print(f"\n[가치 버킷] B/M 5분위 (연도 내 상대 분위):")
    df["bm_q"] = df.groupby("year")["bm"].transform(
        lambda s: pd.qcut(s, 5, labels=False, duplicates="drop"))
    base = df["fwd"].mean()
    print(f"  베이스라인(전체): 평균 {base:+.2f}%")
    for q in range(4, -1, -1):
        _vg_stat(df, df["bm_q"] == q, f"B/M Q{5-q} ({'최저평가' if q == 4 else '고평가' if q == 0 else ''})")
    print(f"\n[가치+퀄리티 결합]")
    _vg_stat(df, (df["bm_q"] == 4) & quality, "B/M 최저평가 + 퀄리티")
    _vg_stat(df, (df["bm_q"] == 4) & quality & (df["roe"] >= 8), "저평가+퀄리티+ROE≥8%")
    _vg_stat(df, (df["bm_q"] == 0), "고평가 Q5 (반례)")

    out = PROJECT_ROOT / "results" / f"quickbt_kr_value_{date.today():%Y%m%d}.csv"
    df.to_csv(out, index=False)
    print(f"\n상세 저장: {out}")
    print("판정 가이드: 저평가+퀄리티 버킷이 베이스라인 대비 유의(+, t≥2) & 연도승 3/5 이상")


def idea_kr_growth(top_n: int):
    """성장 버킷: 매출/영업이익 YoY 성장 + 가격 체크(PER) — 12개월 포워드"""
    df = _kr_value_growth_base(top_n)
    base = df["fwd"].mean()
    print(f"\n[성장 버킷] 베이스라인(전체): 평균 {base:+.2f}%")
    grow = (df["rev_yoy"].notna() & (df["rev_yoy"] >= 15)
            & df["op_yoy"].notna() & (df["op_yoy"] >= 25))
    _vg_stat(df, df["rev_yoy"].notna() & (df["rev_yoy"] >= 15), "매출 YoY ≥15%")
    _vg_stat(df, df["op_yoy"].notna() & (df["op_yoy"] >= 25), "영업이익 YoY ≥25%")
    _vg_stat(df, grow, "매출≥15% + 영업이익≥25% (설계 조건)")
    _vg_stat(df, grow & df["profit2y"], "설계 조건 + 2년 흑자")
    _vg_stat(df, grow & df["per"].notna() & (df["per"] < 25), "설계 조건 + PER<25 (가격 체크)")
    _vg_stat(df, df["rev_yoy"].notna() & (df["rev_yoy"] < 0)
             & df["op_yoy"].notna() & (df["op_yoy"] < 0), "역성장 (반례)")

    out = PROJECT_ROOT / "results" / f"quickbt_kr_growth_{date.today():%Y%m%d}.csv"
    df.to_csv(out, index=False)
    print(f"\n상세 저장: {out}")
    print("판정 가이드: 설계 조건 버킷이 베이스라인 대비 유의(+, t≥2) & 연도승 3/5 이상")


# ════════════════════════════════════════════════════════════════
# 아이디어 2: turn-of-month (시즈널리티 오버레이 검증)
# ════════════════════════════════════════════════════════════════

def idea_tom(symbol: str, months: int):
    """월말 2거래일 + 월초 3거래일 수익률 vs 그 외 — 오버레이 방향성 검증"""
    if symbol.upper() == "KOSPI":
        import FinanceDataReader as fdr
        px = fdr.DataReader("KS11", (date.today() - timedelta(days=months * 30)).isoformat())["Close"]
    else:
        import yfinance as yf
        px = yf.download(symbol, period=f"{months}mo", auto_adjust=True, progress=False)["Close"]
        if isinstance(px, pd.DataFrame):
            px = px.iloc[:, 0]

    rets = px.pct_change().dropna()
    # 실제 거래일 기준 월말/월초 판정
    by_month = pd.Series(rets.index, index=rets.index).groupby(rets.index.to_period("M"))
    in_window = pd.Series(False, index=rets.index)
    for _, days in by_month:
        days = days.sort_values()
        window = list(days[-2:]) + list(days[:3])
        in_window.loc[window] = True

    tom, rest = rets[in_window], rets[~in_window]
    ann = 252
    print("=" * 60)
    print(f"turn-of-month 검증: {symbol} ({months}개월, 거래일 {len(rets)}일)")
    print("=" * 60)
    for label, r in (("ToM 윈도우 (월말2+월초3)", tom), ("그 외 거래일", rest)):
        if len(r) == 0:
            continue
        sharpe = r.mean() / r.std() * np.sqrt(ann) if r.std() > 0 else 0
        print(f"  {label:<22} n={len(r):>4} | 일평균 {r.mean()*100:+.3f}% | "
              f"연환산 Sharpe {sharpe:.2f} | 승률 {(r > 0).mean()*100:.1f}%")
    print("판정 가이드: ToM 일평균이 그 외 대비 뚜렷이 높으면 오버레이 유지")


# ════════════════════════════════════════════════════════════════
# 아이디어 3: KR 저변동성 quintile (감점 임계값 검증)
# ════════════════════════════════════════════════════════════════

def idea_lowvol(months: int):
    """
    백테스트 OHLCV 캐시(정식 백테스터가 만든 pickle) 재활용.
    형성기 60일 일수익률 σ로 5분위 → 20일 포워드 수익률 비교.
    """
    files = sorted(glob.glob(str(KR_CACHE_DIR / "ohlcv_*_20241231_20260803.pkl")))
    if not files:
        # 최신 범위 파일 자동 탐색
        all_files = glob.glob(str(KR_CACHE_DIR / "ohlcv_*.pkl"))
        if not all_files:
            sys.exit(f"KR 캐시 없음: {KR_CACHE_DIR} — backtest_strategies.py를 먼저 실행")
        suffix = max(f.rsplit("_", 2)[-1] for f in all_files)
        files = sorted(f for f in all_files if f.endswith(suffix))

    rows = []
    max_bars = months * 21 + 60   # 형성기 60일 포함
    for f in files:
        sym = Path(f).stem.split("_")[1]
        df = pd.read_pickle(f)
        closes = df["종가"].astype(float).iloc[-max_bars:]
        if len(closes) < 120:
            continue
        rets = closes.pct_change().dropna()
        # 워킹 포워드: 60일 σ 형성 → 이후 20일 수익률 (겹치지 않게 20일 스텝)
        for t in range(60, len(closes) - 20, 20):
            sigma = rets.iloc[t - 60:t].std() * 100
            fwd = (closes.iloc[t + 20] / closes.iloc[t] - 1) * 100
            rows.append({"symbol": sym, "sigma": sigma, "fwd20": fwd})

    df = pd.DataFrame(rows)
    if df.empty:
        sys.exit("표본 없음")
    df["q"] = pd.qcut(df["sigma"], 5, labels=["Q1(저변동)", "Q2", "Q3", "Q4", "Q5(고변동)"])
    print("=" * 60)
    print(f"KR 저변동성 quintile 검증 (종목 {df['symbol'].nunique()}개, 관측 {len(df)}건)")
    print("=" * 60)
    g = df.groupby("q", observed=True)["fwd20"]
    for q, s in g:
        print(f"  {q:<10} n={len(s):>5} | σ범위 {df[df['q']==q]['sigma'].min():.1f}~"
              f"{df[df['q']==q]['sigma'].max():.1f}% | 20일 포워드 평균 {s.mean():+.2f}% | "
              f"승률 {(s > 0).mean()*100:.1f}%")
    print("판정 가이드: Q5(고변동)의 포워드 수익률/승률이 Q1~Q3보다 낮으면 감점 유지")


# ════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(description="신규 전략 아이디어 1차 스크리닝")
    ap.add_argument("--idea", required=True,
                    choices=["earnings_reversal", "earnings_drift", "tom", "lowvol",
                             "kr_value", "kr_growth"])
    ap.add_argument("--months", type=int, default=12)
    ap.add_argument("--symbol", default="SPY", help="tom 전용 (SPY | QQQ | KOSPI)")
    ap.add_argument("--pre-drop", type=float, default=5.0,
                    help="earnings_reversal: 낙폭과대 기준 %%")
    ap.add_argument("--hold-days", type=int, default=3,
                    help="earnings_reversal: 발표 후 보유일")
    ap.add_argument("--source", default="yf", choices=["yf", "finnhub"],
                    help="earnings_reversal: 어닝 일자 소스")
    args = ap.parse_args()

    if args.idea == "earnings_reversal":
        idea_earnings_reversal(args.months, args.pre_drop, args.hold_days, args.source)
    elif args.idea == "earnings_drift":
        idea_earnings_drift(args.months, args.hold_days)
    elif args.idea == "kr_value":
        idea_kr_value(300)
    elif args.idea == "kr_growth":
        idea_kr_growth(300)
    elif args.idea == "tom":
        idea_tom(args.symbol, args.months)
    elif args.idea == "lowvol":
        idea_lowvol(args.months)


if __name__ == "__main__":
    main()
