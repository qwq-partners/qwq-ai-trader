#!/usr/bin/env python3
"""V01 — 기존 후보의 사후 KODEX200 초과수익 판독 (연구 전용, 2026-11-13).

질문·수익 정의·판정 기준: docs/research/v01-candidate-forward-excess-2026-11-13.md (가격 조회 전 사전 등록).
KIS 무접촉. 입력은 운영 서버에 이미 있는 기록(읽기만), 가격은 공개 일봉(FinanceDataReader 수정주가).

  signals: Postgres signal_events → JSONL(읽기 전용, DATABASE_URL 환경변수). 행 = {t(KST ISO), symbol, strategy, score, adj, type, gate}
  obs    : pit_universe / signals JSONL / team_ledger → 관측(obs.jsonl)
  prices : 관측 종목 + 069500 일봉을 CSV 캐시로 내려받는다(연속 실패 5회면 중단)
  report : obs + 가격 → summary.json (판정 포함)

ponytail: 날짜별 평균 + Newey-West 만 쓴다. 종목 군집·부트스트랩은 판정이 경계선일 때 추가.
"""

import argparse
import csv
import datetime
import glob
import hashlib
import json
import math
import os
import time
from collections import defaultdict
from decimal import Decimal
from statistics import mean

BENCH = "069500"
COST = Decimal("0.00227")          # KR 왕복 수수료+거래세
HORIZONS = (1, 5, 10, 20)
STOP = Decimal("0.05")             # 손절 클립(참고)
MAX_DAILY_MOVE = Decimal("0.35")   # 초과 시 자료 오류로 제외
MIN_DAYS_FOR_T = 20
MAE_LEVELS = ("0.035", "0.05", "0.08")


# ---------- 순수 계산 ----------

def forward(bars, cal, day, entry, h):
    """(수익, 손절클립 수익, 최대역행, 종료일, 갭 종료 여부) 또는 (None, 제외사유).

    bars: {date: (o, h, l, c)} 가격은 Decimal, cal: 거래일 오름차순 목록(벤치마크 기준). 반환 수익도 Decimal 이며
    통계 계층(report 의 관측값)에 들어갈 때만 float 로 바꾼다.
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
    mae, stopped, gapped = Decimal(0), False, False
    for d in cal[first:last + 1]:
        o, _h, low, _c = bars[d]
        mae = min(mae, low / px - 1)
        if not stopped and low <= stop_px:
            # 시가 매수 당일의 시가는 매수가 자체라 갭 종료가 아니다
            gapped = o <= stop_px and not (entry == "open" and d == day)
            clip, clip_day, stopped = (o / px - 1 if gapped else -STOP), d, True
    return (ret, clip, mae, clip_day, gapped), None


def nw_t(xs, lag, idx=None):
    """평균의 Newey-West t (Bartlett 가중).

    idx 는 각 값의 거래일 위치(달력 인덱스). 주면 시차를 **실제 거래일 거리**로 잰다 — 후보가 없는 날이 사이에 끼면
    두 관측은 인접 시차가 아니다. 없으면 연속 위치로 본다.
    """
    n = len(xs)
    if n < 2:
        return None
    m = mean(xs)
    at = dict(zip(idx if idx is not None else range(n), (x - m for x in xs)))
    var = sum(v * v for v in at.values()) / n
    for k in range(1, lag + 1):
        var += 2 * (1 - k / (lag + 1)) * sum(e * at[i - k] for i, e in at.items() if i - k in at) / n
    return m / math.sqrt(var / n) if var > 0 else None


def terciles(scores):
    """계열 전체 기준 3분위 경계 (q1, q2): s<=q1 하, s<=q2 중, 그 외 상."""
    s = sorted(scores)
    return s[len(s) // 3 - 1], s[2 * len(s) // 3 - 1]


def daily_stat(rows, key, lag, pos=None):
    """rows=[(date, value dict)] → 날짜별 평균의 평균·t. pos={date: 거래일 위치} 를 주면 t 의 시차를 거래일 거리로 잰다."""
    by = defaultdict(list)
    for d, v in rows:
        if v.get(key) is not None:
            by[d].append(v[key])
    days = sorted(by)
    xs = [mean(by[d]) for d in days]
    if not xs:
        return {"n": 0, "days": 0, "mean": None, "t": None}
    return {"n": sum(len(by[d]) for d in days), "days": len(days), "mean": mean(xs),
            "t": nw_t(xs, lag, [pos[d] for d in days] if pos is not None else None) if len(days) >= 2 else None}


def spread_stat(high, low, key, lag, pos=None):
    """탐색용(판정에 쓰지 않음): 같은 날의 점수 상 − 하 평균 차이. 지수 방향이 상쇄된다."""
    by = {}
    for name, rows in (("h", high), ("l", low)):
        for d, v in rows:
            if v.get(key) is not None:
                by.setdefault(d, {"h": [], "l": []})[name].append(v[key])
    days = [d for d, g in sorted(by.items()) if g["h"] and g["l"]]
    xs = [mean(by[d]["h"]) - mean(by[d]["l"]) for d in days]
    return {"days": len(xs), "mean": mean(xs) if xs else None,
            "t": nw_t(xs, lag, [pos[d] for d in days] if pos is not None else None)}


def drop_top_contributors(rows, key, n=3):
    """날짜별 평균의 평균을 가장 많이 끌어올리는 관측을 하나씩 n번 뺀다(실제 제거 영향 기준).

    관측을 빼면 그날 평균의 분모가 바뀌고, 그날의 유일한 관측이면 날짜 자체가 사라진다.
    """
    by = defaultdict(list)
    for d, v in rows:
        if v.get(key) is not None:
            by[d].append(v[key])
    for _ in range(n):
        if not by:
            break
        total = sum(mean(xs) for xs in by.values())
        cur, best = total / len(by), None
        for d, xs in by.items():
            x = max(xs)   # 같은 날에서는 가장 큰 값을 빼는 것이 평균을 가장 많이 낮춘다
            if len(xs) > 1:
                after = (total - mean(xs) + (sum(xs) - x) / (len(xs) - 1)) / len(by)
            elif len(by) > 1:
                after = (total - x) / (len(by) - 1)
            else:
                after = None
            drop = cur - after if after is not None else float("inf")
            if best is None or drop > best[0]:
                best = (drop, d, x)
        if best[0] <= 0:
            break   # 더 빼도 평균이 내려가지 않는다
        by[best[1]].remove(best[2])
        if not by[best[1]]:
            del by[best[1]]
    return mean(mean(xs) for xs in by.values()) if by else None


def slope(pairs):
    """탐색용: 후보 수익을 같은 구간 벤치마크 수익에 회귀한 기울기(절편 포함 OLS)."""
    if len(pairs) < 3:
        return None
    mx, my = mean(p[0] for p in pairs), mean(p[1] for p in pairs)
    sxx = sum((x - mx) ** 2 for x, _y in pairs)
    return sum((x - mx) * (y - my) for x, y in pairs) / sxx if sxx > 0 else None


def judge(rows_by_tercile, h, pos=None):
    """6절 판정의 한 보유기간 분: C1~C4 와 음의 신호 여부."""
    key = f"net{h}"
    top = rows_by_tercile["high"]
    st = {k: daily_stat(v, key, h - 1, pos) for k, v in rows_by_tercile.items()}
    hi = st["high"]
    if hi["mean"] is None:
        return {"stats": st, "c1": False, "c2": False, "c3": False, "c4": False, "negative": False}
    days = sorted({d for d, v in top if v.get(key) is not None})
    mid_day = days[len(days) // 2]
    halves = [daily_stat([r for r in top if (r[0] < mid_day) == first], key, h - 1)["mean"] for first in (True, False)]
    ex3 = drop_top_contributors(top, key)
    enough = hi["days"] >= MIN_DAYS_FOR_T and hi["t"] is not None
    means = [st[k]["mean"] for k in ("high", "mid", "low")]
    return {
        "stats": st, "halves": halves, "ex_top3": ex3,
        "spread": spread_stat(top, rows_by_tercile["low"], key, h - 1, pos),
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


def combine(verdicts):
    """사전 등록 6절: 주 판정 계열 중 하나만 통과하면 그 판정은 '잠정'이다(V02 신규 자료에서 재확인)."""
    passed = [k for k, v in verdicts.items() if v == "선정 신호 있음"]
    if len(passed) == 1 and len(verdicts) > 1:
        return {**verdicts, passed[0]: "선정 신호 있음(잠정)"}
    return dict(verdicts)


def doc_rows(summary):
    """연구 문서 7.1(판정)·7.2(순초과 표)의 행을 요약에서 그대로 만든다 — 문서와 산출물의 일치 검사용."""
    pct = lambda x: f"{x * 100:+.2f}%".replace("-", "−")   # noqa: E731
    tt = lambda x: f"{x:+.2f}".replace("-", "−")            # noqa: E731
    mark = lambda j: " / ".join("✓" if j[c] else "✗" for c in ("c1", "c2", "c3", "c4"))  # noqa: E731
    verdicts, table = [], []
    for name, p in summary["primary"].items():
        verdicts.append(f"| {name} | **{p['verdict']}** | {mark(p['h5'])} | {mark(p['h20'])} |")
        q1, q2 = f"{round(p['q1'], 1):g}", f"{round(p['q2'], 1):g}"
        for label, g in ((f"{name} 전체", p["all"]), (f"{name} 점수 상(>{q2})", p["tercile"]["high"]),
                         (f"{name} 점수 중({q1}~{q2})", p["tercile"]["mid"]), (f"{name} 점수 하(≤{q1})", p["tercile"]["low"])):
            h5, h20 = g["h5"], g["h20"]
            table.append(f"| {label} | {pct(h5['mean'])} | {tt(h5['t'])} | {pct(h20['mean'])} | {tt(h20['t'])} "
                         f"| {h5['n']:,} / {h5['days']} |")
    return verdicts, table


# ---------- 입출력 ----------

def build_obs(pit_dir, signals, team_dir, through):
    """through(YYYY-MM-DD, KST) 이후 날짜의 입력은 쓰지 않는다 — 나중에 다시 돌려도 같은 코호트가 나오게 한다."""
    obs = []
    for f in sorted(glob.glob(os.path.join(pit_dir, "*.json"))):
        j = json.load(open(f))
        if j["date"] > through:
            continue
        for c in j["candidates"]:
            obs.append({"family": "A", "date": j["date"], "symbol": c["symbol"], "group": c.get("strategy"),
                        "score": c.get("score"), "entry": "open"})
    first, passed = {}, {}
    for line in open(signals):
        r = json.loads(line)
        k = (r["symbol"], r["t"][:10])
        if k[1] > through:
            continue
        if r["type"] == "passed" and (k not in passed or r["t"] < passed[k]["t"]):
            passed[k] = r
        if k not in first or r["t"] < first[k]["t"]:
            first[k] = r
    for k, r in first.items():
        # 주 판정의 점수는 사전 등록대로 그날 첫 이벤트. 게이트·전략 참고 집계는 대표 이벤트(첫 통과, 없으면 첫 이벤트)
        rep = passed.get(k, r)
        gate = "passed" if k in passed else (r["gate"] if r["gate"] is not None else r["type"])
        adj = rep.get("adj")
        obs.append({"family": "B", "date": k[1], "symbol": k[0], "group": gate, "strategy": rep["strategy"],
                    "score": r["score"], "rep_score": adj if adj is not None else rep["score"], "entry": "close"})
    first = {}
    for f in sorted(glob.glob(os.path.join(team_dir, "deliberations_*.jsonl"))):
        by_id = {}   # 같은 deliberation_id 는 마지막 행이 정본 — src/agents/team_ledger.load_day(dedup=True) 와 같은 계약
        for n, line in enumerate(open(f)):
            r = json.loads(line)
            by_id[str(r["deliberation_id"]) if r.get("deliberation_id") is not None else f"row{n}"] = r
        for r in by_id.values():
            t = r["decided_at"] if r.get("decided_at") is not None else r["recorded_at"]
            k = (r["symbol"], t[:10])
            if k[1] > through:
                continue
            if k not in first or t < first[k][0]:
                d = r["decision"] if r.get("decision") is not None else {}
                approved = d.get("approved")   # 정확히 True/False 만 인정 — 1·"yes"·누락은 invalid 로 따로 센다
                label = ("invalid" if not isinstance(approved, bool)   # 결정 없음·키 누락·비 bool 모두 invalid
                         else "buy_approved" if d.get("stance") == "buy" and approved is True
                         else "hold" if d.get("stance") == "hold" and approved is True
                         else "sell" if d.get("stance") == "sell" else "not_approved")
                first[k] = (t, label)
    for k, (_t, label) in first.items():
        obs.append({"family": "C", "date": k[1], "symbol": k[0], "group": label, "score": None, "entry": "close"})
    return obs


def load_bars(path):
    """비유한·비양수·고저 역전 OHLC 행은 버려 결측 봉으로 만든다(그 봉을 걸치는 관측은 missing_bar 로 제외·집계). 중복 날짜는 실패."""
    bars = {}
    with open(path) as f:
        for r in csv.DictReader(f):
            v = tuple(Decimal(r[k]) for k in ("open", "high", "low", "close"))
            if r["date"] in bars:
                raise SystemExit(f"가격 캐시 중복 날짜: {path} {r['date']}")
            # 유한·양수이고 저가 ≤ 시가·종가 ≤ 고가인 봉만 쓴다(구조적으로 불가능한 봉은 MAE·손절 도달률을 왜곡한다)
            if all(x.is_finite() and x > 0 for x in v) and v[2] <= min(v[0], v[3]) and max(v[0], v[3]) <= v[1]:
                bars[r["date"]] = v
            else:
                bars[r["date"]] = None
    return {d: v for d, v in bars.items() if v is not None}


def fetch_prices(symbols, out_dir, start, through, pause=0.4):
    import FinanceDataReader as fdr  # 지연 임포트: 계산·검사는 네트워크 라이브러리 없이 돈다
    os.makedirs(out_dir, exist_ok=True)
    fails, failed = 0, []
    for s in symbols:
        p = os.path.join(out_dir, f"{s}.csv")
        if os.path.exists(p):
            continue
        try:
            df = fdr.DataReader(s, start, through)
            if df is None or df.empty:
                raise ValueError("empty")
            with open(p + ".tmp", "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["date", "open", "high", "low", "close"])
                for d, r in df.iterrows():
                    w.writerow([d.strftime("%Y-%m-%d"), r["Open"], r["High"], r["Low"], r["Close"]])
            os.replace(p + ".tmp", p)   # 중단된 부분 파일이 캐시로 남지 않게 한다
            fails = 0
        except Exception as e:  # 원천 오류는 종목 단위로 건너뛰고, 연속되면 멈춘다
            fails += 1
            failed.append(s)
            print(f"[실패] {s}: {type(e).__name__}")
            if fails >= 5:
                raise SystemExit("연속 실패 5회 — 중단")
        time.sleep(pause)
    if failed:
        raise SystemExit(f"미수집 {len(failed)}종목: {failed[:10]} — 다시 실행해 채운다(report 는 가격 파일이 없으면 실패)")


def adjust_bench(bench, adj_csv, through=None):
    """공개 일봉의 069500 은 분배금 미반영이다 — 초과수익 원장의 KIS 수정 종가 비율로 OHLC 를 맞춘다.

    보정 자료의 기간 [첫날, 끝날] 안에서 두 원천의 거래일이 다르면 달력이 조용히 줄어 보유 기간이 밀린다 → 실패시킨다.
    보정 기간 밖(앞·뒤)의 공개 일봉은 버린다.
    """
    with open(adj_csv) as f:
        rows = [(r["date"], Decimal(r["close"])) for r in csv.DictReader(f)]
    if through is not None:   # 보정 자료·공개 일봉 모두 고정 종료일까지만 쓴다
        rows = [r for r in rows if r[0] <= through]
        bench = {d: v for d, v in bench.items() if d <= through}
    adj = dict(rows)
    if len(adj) != len(rows) or any(not (c.is_finite() and c > 0) for c in adj.values()):
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


def report(obs, price_dir, bench_adjust, through, allow_partial_prices=False):
    late = sorted({o["date"] for o in obs if o["date"] > through})
    if late:
        raise SystemExit(f"고정 종료일 {through} 이후 관측이 있다: {late[:5]}")
    bench = adjust_bench(load_bars(os.path.join(price_dir, f"{BENCH}.csv")), bench_adjust, through)
    cal = sorted(bench)
    pos = {d: i for i, d in enumerate(cal)}
    symbols = sorted({o["symbol"] for o in obs})
    missing = [s for s in symbols if not os.path.exists(os.path.join(price_dir, f"{s}.csv"))]
    if missing:
        raise SystemExit(f"가격 파일 없는 종목 {len(missing)}개: {missing[:10]} — prices 를 다시 실행한다")
    fingerprint = hashlib.sha256()
    with open(os.path.join(price_dir, f"{BENCH}.csv"), "rb") as f:   # 벤치마크 원본 OHLC 도 지문에 넣는다
        fingerprint.update(BENCH.encode() + hashlib.sha256(f.read()).digest())
    cache, excluded, rows = {}, defaultdict(lambda: defaultdict(int)), []
    for s in symbols:
        with open(os.path.join(price_dir, f"{s}.csv"), "rb") as f:
            fingerprint.update(s.encode() + hashlib.sha256(f.read()).digest())
        cache[s] = {d: v for d, v in load_bars(os.path.join(price_dir, f"{s}.csv")).items() if d <= through}
    # 캐시가 분석 기간을 덮는지 확인한다: 첫 봉이 첫 관측일보다 늦거나 마지막 봉이 벤치마크 끝날보다 이르면
    # 짧은 캐시(다른 기간으로 받은 파일)이거나 신규 상장·거래정지다. 조용히 missing_bar 로 흡수하지 않는다.
    first_obs = {}
    for o in obs:
        first_obs[o["symbol"]] = min(first_obs.get(o["symbol"], o["date"]), o["date"])
    partial = sorted(s for s in symbols if not cache[s] or max(cache[s]) < cal[-1] or min(cache[s]) > first_obs[s])
    if partial and not allow_partial_prices:
        raise SystemExit(f"가격 캐시가 분석 기간을 덮지 못하는 종목 {len(partial)}개: {partial[:10]} — "
                         "prices 를 새 디렉터리로 다시 받거나, 거래정지·상장폐지가 맞으면 --allow-partial-prices 로 명시한다")
    stale = len(partial)
    for o in obs:
        s = o["symbol"]
        v = {}
        for h in HORIZONS:
            res, why = forward(cache[s], cal, o["date"], o["entry"], h)
            if res is None:
                excluded[o["family"]][f"h{h}:{why}"] += 1
                continue
            ret, clip, mae, clip_day, gapped = res
            b, _ = forward(bench, cal, o["date"], o["entry"], h)
            base = bench[o["date"]][0] if o["entry"] == "open" else bench[o["date"]][3]
            # 가격·비용 산식은 Decimal 로 끝내고 여기서 통계용 float 로 바꾼다
            v.update({f"ret{h}": float(ret), f"bench{h}": float(b[0]), f"net{h}": float(ret - b[0] - COST),
                      f"mae{h}": float(mae), f"up{h}": ret > 0,
                      f"hit{h}": {x: mae <= -Decimal(x) for x in MAE_LEVELS},   # 경계 비교는 float 로 바꾸기 전에
                      # 갭 종료면 벤치마크도 그날 시가, 장중 손절·만기 종료는 그날 종가(일봉 근사)
                      f"clipnet{h}": float(clip - (bench[clip_day][0 if gapped else 3] / base - 1) - COST)})
        rows.append((o, v))

    def table(sel, keyfn):
        groups = defaultdict(list)
        for o, v in sel:
            groups[keyfn(o)].append((o["date"], v))
        return {str(g): {f"h{h}": {**daily_stat(rs, f"net{h}", h - 1, pos),
                                   "abs": daily_stat(rs, f"ret{h}", h - 1)["mean"],
                                   "clip": daily_stat(rs, f"clipnet{h}", h - 1)["mean"]} for h in HORIZONS}
                for g, rs in sorted(groups.items(), key=lambda kv: str(kv[0]))}

    with open(bench_adjust, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    out = {"cost": float(COST), "through": through, "bench": {"adjusted_by": os.path.basename(bench_adjust), "sha256": digest, "rows": len(cal),
                                   "first": cal[0], "last": cal[-1]},
           # 마지막 봉이 벤치마크 끝날보다 이른 종목 수(거래정지·상장폐지 또는 오래된 캐시)를 함께 남긴다
           "obs": {"n": len(obs), "sha256": hashlib.sha256(
               json.dumps(sorted(obs, key=lambda o: json.dumps(o, sort_keys=True)), sort_keys=True).encode()).hexdigest()},
           "prices": {"symbols": len(symbols), "sha256": fingerprint.hexdigest(), "partial_coverage": stale,
                      "allow_partial_prices": allow_partial_prices},
           "excluded": {k: dict(v) for k, v in excluded.items()}, "primary": {}, "tables": {}}
    for name, sel in (("A-sepa", [r for r in rows if r[0]["family"] == "A" and r[0]["group"] == "sepa_trend"]),
                      ("B-all", [r for r in rows if r[0]["family"] == "B"])):
        scored = [r for r in sel if r[0]["score"] is not None]
        q1, q2 = terciles([r[0]["score"] for r in scored])
        tier = lambda o: "low" if o["score"] <= q1 else "mid" if o["score"] <= q2 else "high"  # noqa: E731
        by = {k: [(o["date"], v) for o, v in scored if tier(o) == k] for k in ("high", "mid", "low")}
        j = {h: judge(by, h, pos) for h in (5, 20)}
        spreads = {f"h{h}": spread_stat(by["high"], by["low"], f"net{h}", h - 1, pos) for h in HORIZONS}
        out["primary"][name] = {"n": len(sel), "scored": len(scored), "q1": q1, "q2": q2,
                                "verdict": verdict(j[5], j[20]), "h5": j[5], "h20": j[20], "spread": spreads,
                                "all": table(sel, lambda o: "all")["all"],
                                "tercile": table(scored, tier),
                                "mae5_hit": {x: (lambda xs: mean(xs) if xs else None)(
                                    [1.0 if v["hit5"][x] else 0.0 for _o, v in sel if "hit5" in v]) for x in MAE_LEVELS},
                                # 탐색용: 20일 뒤 결과(상승/하락)별 5일 안 −5% 도달률, 벤치마크 수익에 대한 기울기
                                "mae5_hit_5pct_by_ret20": {
                                    name: (lambda xs: {"n": len(xs), "rate": mean(xs) if xs else None})(
                                        [1.0 if v["hit5"]["0.05"] else 0.0 for _o, v in sel
                                         if "hit5" in v and "up20" in v and v["up20"] == want])
                                    for name, want in (("up", True), ("down", False))},
                                "slope_vs_bench": {f"h{h}": slope([(v[f"bench{h}"], v[f"ret{h}"]) for _o, v in sel
                                                                  if f"ret{h}" in v]) for h in (5, 20)}}
    for name, v in combine({k: p["verdict"] for k, p in out["primary"].items()}).items():
        out["primary"][name]["verdict"] = v
    fam = lambda f: [r for r in rows if r[0]["family"] == f]  # noqa: E731
    out["tables"] = {
        "A_by_strategy": table(fam("A"), lambda o: o["group"]),
        "A_sepa_by_month": table([r for r in fam("A") if r[0]["group"] == "sepa_trend"], lambda o: o["date"][:7]),
        "B_by_gate": table(fam("B"), lambda o: o["group"] if o["group"] in ("passed", "G1_regime") else "other"),
        "B_by_strategy": table(fam("B"), lambda o: o.get("strategy")),
        "B_by_month": table(fam("B"), lambda o: o["date"][:7]),
        "C_by_decision": table(fam("C"), lambda o: o["group"]),
        "B_tercile_by_rep_score": (lambda q: table(
            [r for r in fam("B") if r[0]["rep_score"] is not None],
            lambda o: "low" if o["rep_score"] <= q[0] else "mid" if o["rep_score"] <= q[1] else "high"))(
                terciles([r[0]["rep_score"] for r in fam("B") if r[0]["rep_score"] is not None])),
        "daily_top3": {**table(top_n_per_day([r for r in fam("A") if r[0]["group"] == "sepa_trend"]), lambda o: "A-sepa"),
                       **table(top_n_per_day(fam("B")), lambda o: "B-all")},
    }
    return out


def export_signals(out_path, through):
    """signal_events 를 읽기 전용으로 내보낸다. 날짜는 KST 기준이며 through(포함)까지만."""
    import asyncio

    import asyncpg  # 지연 임포트: 계산·검사는 DB 라이브러리 없이 돈다

    async def run():
        conn = await asyncpg.connect(os.environ["DATABASE_URL"], timeout=15)
        try:
            await conn.execute("SET default_transaction_read_only = on")
            return await conn.fetch(
                "select event_time at time zone 'Asia/Seoul' as t, symbol, strategy, score, adjusted_score, event_type, block_gate "
                "from signal_events where side = 'buy' "
                "and (event_time at time zone 'Asia/Seoul')::date <= $1::date order by id", through)
        finally:
            await conn.close()

    rows = asyncio.run(run())
    with open(out_path, "w") as f:
        for r in rows:
            f.write(json.dumps({"t": r["t"].isoformat(), "symbol": r["symbol"], "strategy": r["strategy"],
                                "score": r["score"], "adj": r["adjusted_score"], "type": r["event_type"],
                                "gate": r["block_gate"]}) + "\n")
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
    c.add_argument("--allow-partial-prices", action="store_true", help="분석 기간을 다 덮지 못하는 가격 파일을 명시적으로 허용")
    for name in ("signals", "obs", "prices", "report"):   # 모든 단계가 같은 고정 종료일을 받는다
        sub.choices[name].add_argument("--through", required=True, help="분석 종료일 YYYY-MM-DD(KST, 포함)")
    args = ap.parse_args()
    if args.cmd == "signals":
        export_signals(args.out, datetime.date.fromisoformat(args.through))
        return
    if args.cmd == "obs":
        obs = build_obs(args.pit_dir, args.signals, args.team_dir, args.through)
        with open(args.out, "w") as f:
            f.writelines(json.dumps(o, ensure_ascii=False) + "\n" for o in obs)
        print(len(obs), "관측")
        return
    obs = [json.loads(line) for line in open(args.obs)]
    if args.cmd == "prices":
        fetch_prices([BENCH] + sorted({o["symbol"] for o in obs}), args.out_dir, args.start, args.through)
    else:
        out = report(obs, args.price_dir, args.bench_adjust, args.through, args.allow_partial_prices)
        with open(args.out, "w") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        for name, p in out["primary"].items():
            print(name, p["verdict"], {h: {k: p[h][k] for k in ("c1", "c2", "c3", "c4", "negative")} for h in ("h5", "h20")})


if __name__ == "__main__":
    main()
