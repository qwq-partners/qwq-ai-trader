#!/usr/bin/env python3
"""V02-R — 장중 스캔 후보 원장의 사후 수익 판독(측정 전용, 오프라인).

사전 등록: docs/research/v02-candidate-ledger-2026-11-13.md 4절·7절, 판독 창·진입 점수 집계는
docs/research/v03-resume-criteria-2026-11-15.md 3·5·8절. KIS 무접촉, 운영 잡이 아니다.
  obs    : candidate_ledger/candidates_YYYYMMDD.jsonl[.gz] → 종목-일 관측(obs.jsonl, 그날 처음 반환된 스캔)
           + 건수·결측률·원천 분포, 창 경계용 건수(표준 출력 — 수익은 계산하지 않는다). --from 으로 창 시작일을 끊는다
  prices : 판독에 쓰는 종목(장 마감 뒤 첫 등장만 있는 종목 제외) + 069500 의 공개 일봉을 CSV 캐시로(V01 fetch_prices)
  report : obs + 공개 일봉 → summary.json. 최소 표본(두 집단이 모두 있는 날 15일·300종목-일)에 못 미친
           보유 기간은 '판정 불가'만 적고 수익 통계를 계산하지 않는다(중간 엿보기 금지)
"""
import argparse
import glob
import gzip
import hashlib
import json
import math
import os
import re
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from scripts.research.candidate_forward_excess import (  # noqa: E402
    BENCH, COST, adjust_bench, daily_stat, fetch_prices, forward, load_bars, slope)
from scripts.research.team_rejudge import block_bootstrap, weighted_nw_t, within_day, wmean  # noqa: E402
from src.analytics.candidate_ledger import SCHEMA  # noqa: E402

HORIZONS = (5, 20)
THRESHOLD = {5: 0.010, 20: 0.020}     # 같은 날 상위−하위 수익 차이 기준
MIN_DAYS, MIN_SYMBOL_DAYS = 15, 300   # 미달이면 '판정 불가'
ENTRY_SCORE = 75                      # V03 O3 — 엔진의 기본 진입 점수 기준(약세·변동성 상향 전)
MIN_ENTRY_N, MIN_ENTRY_DAYS = 60, 10  # O3 최소 표본(종목-일, 서로 다른 날) — 미달이면 평균을 계산하지 않는다
CLOSE = "15:30:00"                    # 이 시각 이후 처음 나온 후보는 D 종가 진입이 미래 참조라 판독에서 뺀다
NOON = "12:00:00"
FIELDS = ("score", "price", "change_pct", "volume", "volume_ratio", "rsi", "atr_pct", "per", "pbr", "foreign", "inst", "basis")
_FILE = re.compile(r"candidates_(\d{4})(\d{2})(\d{2})\.jsonl(\.gz)?")


def _num(x):
    return x if type(x) in (int, float) and math.isfinite(x) else None


def _lines(path):
    with (gzip.open if path.endswith(".gz") else open)(path, "rt", encoding="utf-8", errors="replace") as f:
        yield from f


def build_obs(ledger_dir, through, start=None):
    """원장 → (종목-일 관측, 통계). 읽히지 않는 줄·모르는 스키마·짝 없는 게이트 행은 버리고 센다.
    start(포함)를 주면 그 전 날짜 파일은 읽지 않는다 — 겹치지 않는 판독 창(V03 5절)."""
    files = {}
    for p in sorted(glob.glob(os.path.join(ledger_dir, "candidates_*.jsonl*"))):
        m = _FILE.fullmatch(os.path.basename(p))
        day = "-".join(m.groups()[:3]) if m else None
        if day is None or day > through or (start is not None and day < start):
            continue
        if day in files:
            raise SystemExit(f"같은 날짜 원장이 둘이다(.jsonl 과 .jsonl.gz): {day}")
        files[day] = p
    c = defaultdict(int)
    nulls, sources, sessions, per_day, obs = defaultdict(int), defaultdict(int), defaultdict(int), {}, []
    gate_n = {"regime": defaultdict(int), "cash": defaultdict(int), "entry_filter": defaultdict(int)}
    for day, path in files.items():
        scans, gates = {}, defaultdict(dict)
        for line in _lines(path):
            try:
                r = json.loads(line)
            except ValueError:
                c["skipped_lines"] += 1   # 쓰다가 죽어 잘린 줄(기록 계약) — 지우지 않고 건너뛴다
                continue
            kind = r.get("kind") if isinstance(r, dict) else None
            sid = r.get("scan_id") if kind in ("scan", "gate") else None
            if kind == "error":
                c["error_rows"] += 1
            elif not isinstance(sid, str):
                c["unknown_rows"] += 1
            elif kind == "gate":
                if isinstance(r.get("gate"), str):
                    gates[sid][r["gate"]] = r
                else:
                    c["unknown_rows"] += 1
            elif (r.get("schema") != SCHEMA or not isinstance(r.get("at"), str) or r["at"][:10] != day
                  or not isinstance(r.get("candidates"), list) or sid in scans):
                c["bad_scans"] += 1       # 다른 스키마·날짜 불일치·중복 scan_id 는 쓰지 않는다
            else:
                scans[sid] = r
        c["orphan_gates"] += sum(len(g) for sid, g in gates.items() if sid not in scans)
        seen = set()
        for sid, r in sorted(scans.items(), key=lambda kv: kv[1]["at"]):
            g = gates.get(sid, {})
            c["scans"] += 1
            c["candidate_rows"] += len(r["candidates"])
            c["basis_rows"] += sum(isinstance(x, dict) and isinstance(x.get("basis"), dict) for x in r["candidates"])
            sessions[str(r.get("session"))] += 1
            for name in gate_n:
                row = g.get(name)
                gate_n[name]["없음" if row is None else "기록" if name == "entry_filter"
                             else "통과" if row.get("ok") is True else "차단" if row.get("ok") is False else "불명"] += 1
            c["regime_unobserved"] += "regime" in g and g["regime"].get("observed") is not True   # 지수 조회 실패(등락률 null)
            regime = g.get("regime", {}).get("ok")
            for x in r["candidates"]:
                sym = x.get("symbol") if isinstance(x, dict) else None
                if not isinstance(sym, str) or sym in seen:
                    continue
                seen.add(sym)
                basis = x.get("basis") if isinstance(x.get("basis"), dict) else None
                terms = basis.get("terms") if basis is not None and isinstance(basis.get("terms"), list) else []
                src = sorted({t[0] for t in terms if isinstance(t, list) and len(t) == 2 and isinstance(t[0], str)
                              and _num(t[1]) is not None}) if basis is not None else None
                o = {"date": day, "symbol": sym, "t": r["at"], "scan_id": sid, "after_close": r["at"][11:] >= CLOSE,
                     "score": _num(x.get("score")), "sources": src,
                     "bonus": _num(basis.get("bonus")) if basis is not None else None,
                     "foreign": x.get("foreign") if isinstance(x.get("foreign"), bool) else None,
                     "inst": x.get("inst") if isinstance(x.get("inst"), bool) else None,
                     "regime_ok": regime if isinstance(regime, bool) else None}
                obs.append(o)
                for k in FIELDS:
                    nulls[k] += x.get(k) is None
                for s in src if src is not None else ():
                    sources[s] += 1
        per_day[day] = {"scans": len(scans), "symbol_days": len(seen)}
    n = len(obs)
    return obs, {"from": start, "through": through, "window": window_counts(obs), "days": len(files), "symbol_days": n,
                 "after_close": sum(o["after_close"] for o in obs), **{k: c[k] for k in (
                     "scans", "candidate_rows", "basis_rows", "skipped_lines", "bad_scans", "orphan_gates",
                     "error_rows", "unknown_rows", "regime_unobserved")},
                 "null_rate": {k: round(nulls[k] / n, 4) if n else None for k in FIELDS},
                 "sources": dict(sorted(sources.items())), "sessions": dict(sorted(sessions.items())),
                 "gates": {k: dict(sorted(v.items())) for k, v in gate_n.items()}, "per_day": per_day}


def window_counts(obs):
    """창 경계(V03 5절): 판독에 쓰이는 관측 수·3분위가 서는 날 수와, 둘 다 최소 표본을 처음 넘긴 날(end — 그 창의 마지막 날).
    점수·시각만으로 세며 가격·수익과 무관하다. end 뒤 날짜가 obs 에 있으면 --through 를 end 로 다시 만든다."""
    by = defaultdict(list)
    for o in obs:
        if not o["after_close"] and o["score"] is not None:
            by[o["date"]].append((o, {}))
    n = days = 0
    end = None
    for d in sorted(by):
        n, days = n + len(by[d]), days + bool(day_terciles(by[d]))
        if end is None and n >= MIN_SYMBOL_DAYS and days >= MIN_DAYS:
            end = d
    return {"usable_symbol_days": n, "tercile_days": days, "end": end}


def used_symbols(obs):
    """판독에 쓰는 종목 — 장 마감 뒤 첫 등장뿐인 종목은 가격도 받지 않는다(그 종목의 수집 실패가 판독을 막지 않게)."""
    return sorted({o["symbol"] for o in obs if not o["after_close"]})


def _digest(obj):
    return hashlib.sha256(json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def outcomes(obs, price_dir, bench_adjust, through, allow_partial=False):
    """D 종가 진입·h거래일 뒤 종가(V01 산식·가격 원천). 반환 [(obs, {ret5, bench5, ret20, bench20})], 거래일 위치, 제외 사유,
    짧은 캐시 종목 수, 가격 지문(종료일까지의 보정 벤치마크·종목 봉 — 종료일 뒤에 붙은 행은 지문을 바꾸지 않는다)."""
    late = sorted({o["date"] for o in obs if o["date"] > through})
    if late:
        raise SystemExit(f"고정 종료일 {through} 이후 관측이 있다: {late[:5]}")
    bench = adjust_bench(load_bars(os.path.join(price_dir, f"{BENCH}.csv")), bench_adjust, through)
    cal = sorted(bench)
    use = [o for o in obs if not o["after_close"]]
    symbols = used_symbols(obs)
    missing = [s for s in symbols if not os.path.exists(os.path.join(price_dir, f"{s}.csv"))]
    if missing:
        raise SystemExit(f"가격 파일 없는 종목 {len(missing)}개: {missing[:10]} — prices 를 다시 실행한다")
    cache = {s: {d: v for d, v in load_bars(os.path.join(price_dir, f"{s}.csv")).items() if d <= through} for s in symbols}
    first = {}
    for o in use:
        first[o["symbol"]] = min(first.get(o["symbol"], o["date"]), o["date"])
    partial = sorted(s for s in symbols if not cache[s] or max(cache[s]) < cal[-1] or min(cache[s]) > first[s])
    if partial and not allow_partial:   # 짧은 캐시를 조용히 missing_bar 로 흡수하지 않는다(V01 과 같은 계약)
        raise SystemExit(f"가격 캐시가 분석 기간을 덮지 못하는 종목 {len(partial)}개: {partial[:10]} — "
                         "prices 를 새 디렉터리로 다시 받거나, 거래정지·상장폐지가 맞으면 --allow-partial-prices 로 명시한다")
    excluded, rows = defaultdict(int), []
    for o in use:
        v = {}
        for h in HORIZONS:
            res, why = forward(cache[o["symbol"]], cal, o["date"], "close", h)
            if res is None:
                excluded[f"h{h}:{why}"] += 1
                continue
            b, _ = forward(bench, cal, o["date"], "close", h)
            v[f"ret{h}"], v[f"bench{h}"] = float(res[0]), float(b[0])
        rows.append((o, v))
    return (rows, {d: i for i, d in enumerate(cal)}, dict(sorted(excluded.items())), len(partial),
            _digest({BENCH: sorted(bench.items()), **{s: sorted(cache[s].items()) for s in symbols}}))


def day_terciles(rows):
    """rows=[(obs, v)] → [(date, True=상위/False=하위, v)]. 날짜별 점수 순위 3분위(k=n//3) — 하위는 k번째로 낮은 점수 이하,
    상위는 k번째로 높은 점수 이상(경계 동점은 함께 넣는다). 점수 있는 후보가 3개 미만이거나 두 경계가 만나는 날은 뺀다."""
    by = defaultdict(list)
    for o, v in rows:
        if o["score"] is not None:
            by[o["date"]].append((o["score"], v))
    out = []
    for d, xs in by.items():
        s, k = sorted(x[0] for x in xs), len(xs) // 3
        if k < 1 or s[k - 1] >= s[-k]:
            continue
        out.extend((d, x >= s[-k], v) for x, v in xs if x <= s[k - 1] or x >= s[-k])
    return out


def judge(rows, h, pos=None):
    key = f"ret{h}"
    parts = within_day(day_terciles(rows), key)
    out = {"symbol_days": sum(o["score"] is not None and key in v for o, v in rows), "days_both": len(parts)}
    if len(parts) < MIN_DAYS or out["symbol_days"] < MIN_SYMBOL_DAYS:
        return {**out, "verdict": "판정 불가"}   # 여기서 멈춘다 — 표본이 차기 전에는 차이를 계산하지 않는다
    point, (lo, hi) = wmean(parts), block_bootstrap(parts)
    verdict = ("선정 신호 있음" if point > THRESHOLD[h] and lo > 0
               else "음의 신호" if point < -THRESHOLD[h] and hi < 0 else "미입증")
    return {**out, "diff": point, "ci95": [lo, hi], "t_nw": weighted_nw_t(parts, h - 1, pos), "verdict": verdict}


def tradability(rows, h, pos=None):
    """보조 판정: 상위 3분위의 비용 차감 순초과(수익 − β×벤치마크 − 왕복 비용). β 는 이 표본의 후보 수익을 벤치마크 수익에 회귀한 기울기."""
    r, b = f"ret{h}", f"bench{h}"
    beta = slope([(v[b], v[r]) for o, v in rows if o["score"] is not None and r in v])
    if beta is None:
        return None
    top = [(d, {"x": v[r] - beta * v[b] - float(COST)}) for d, g, v in day_terciles(rows) if g and r in v]
    st = daily_stat(top, "x", h - 1, pos)
    return {"beta": beta, **st, "net_positive": st["mean"] is not None and st["mean"] > 0}


def entry_score(rows, h, beta, pos=None, primary_ready=True):
    """V03 O3: 첫 스캔 점수가 진입 기준 이상인 후보의 비용 차감 순초과. β 는 tradability 와 같은 값(창 전체 표본),
    날짜별 평균의 평균(daily_stat). 실제 주문 집단이 아니라 그 앞 단계다(점수 상향·필터·엔진 게이트 이전).
    계산하지 않는 경우에도 건수와 사유를 남긴다 — 빠진 항목과 판정 불가를 구분하기 위해서다."""
    r, b = f"ret{h}", f"bench{h}"
    sel = [(o, v) for o, v in rows if o["score"] is not None and o["score"] >= ENTRY_SCORE and r in v]
    n, days = len(sel), len({o["date"] for o, _v in sel})
    why = ("주 판정 표본 미달" if not primary_ready else "진입 점수 표본 미달" if n < MIN_ENTRY_N or days < MIN_ENTRY_DAYS
           else "β 추정 불가" if beta is None else None)
    if why is not None:
        return {"n": n, "days": days, "verdict": "판정 불가", "reason": why}
    st = daily_stat([(o["date"], {"x": v[r] - beta * v[b] - float(COST)}) for o, v in sel], "x", h - 1, pos)
    return {**st, "net_positive": st["mean"] > 0}


def explore(rows, h):
    """탐색(판정에 쓰지 않음): 표시가 있는 후보 − 없는 후보의 같은 날 차이. 표시를 모르는 후보는 뺀다."""
    def either(o):
        return True if o["foreign"] is True or o["inst"] is True else False if o["foreign"] is False and o["inst"] is False else None
    flags = {"수급 표시": either, "출현 보너스": lambda o: None if o["bonus"] is None else o["bonus"] > 0,
             "오후 첫 등장": lambda o: o["t"][11:] >= NOON, "약세장 차단 스캔": lambda o: None if o["regime_ok"] is None else not o["regime_ok"]}
    for s in sorted({s for o, _v in rows if o["sources"] is not None for s in o["sources"]}):
        flags[f"원천 {s}"] = lambda o, s=s: None if o["sources"] is None else s in o["sources"]
    out = {}
    for name, fn in flags.items():
        sel = [(o["date"], fn(o), v) for o, v in rows if fn(o) is not None and f"ret{h}" in v]
        parts = within_day(sel, f"ret{h}")
        out[name] = {"n_in": sum(g for _d, g, _v in sel), "n_out": sum(not g for _d, g, _v in sel), "days_both": len(parts),
                     "diff": wmean(parts), "ci95": list(block_bootstrap(parts))}
    return out


def combine(verdicts):
    """두 보유 기간의 판정 → 종합. 한 기간만 통과하면 잠정, 방향이 엇갈리면 미입증."""
    for name, other in (("선정 신호 있음", "음의 신호"), ("음의 신호", "선정 신호 있음")):
        if name in verdicts and other not in verdicts:
            return name if verdicts.count(name) == len(verdicts) else f"{name}(잠정 — 한 보유 기간만)"
    return "판정 불가" if all(v == "판정 불가" for v in verdicts) else "미입증"


def report(obs, price_dir, bench_adjust, through, allow_partial=False):
    dates = sorted({o["date"] for o in obs})
    end = window_counts(obs)["end"]
    if end is not None and dates[-1] > end:   # 창이 찬 뒤의 관측이 섞이면 사전 등록한 창이 아니다 — 계산하지 않는다
        raise SystemExit(f"판독 창은 {end} 에 찼는데 관측이 {dates[-1]} 까지 있다 — obs 를 --through {end} 로 다시 만든다")
    rows, pos, excluded, partial, prices = outcomes(obs, price_dir, bench_adjust, through, allow_partial)
    out = {"schema": "v02r-2", "window": {"first_obs": dates[0] if dates else None, "last_obs": dates[-1] if dates else None},
           "through": through, "inputs": {"obs_sha256": _digest(obs), "prices_sha256": prices},
           "symbol_days": len(obs), "after_close_excluded": len(obs) - len(rows),
           "unscored": sum(o["score"] is None for o, _v in rows), "partial_price_symbols": partial, "excluded": excluded,
           "primary": {}, "tradability": {}, "entry_score": {}, "exploratory": {}}
    for h in HORIZONS:
        j = out["primary"][f"h{h}"] = judge(rows, h, pos)
        if j["verdict"] == "판정 불가":
            out["entry_score"][f"h{h}"] = entry_score(rows, h, None, pos, primary_ready=False)
            continue
        t = out["tradability"][f"h{h}"] = tradability(rows, h, pos)
        out["entry_score"][f"h{h}"] = entry_score(rows, h, None if t is None else t["beta"], pos)
        if j["verdict"] == "선정 신호 있음" and (t is None or not t["net_positive"]):
            j["note"] = "거래 가능성 판정 불가(β 추정 불가)" if t is None else "선별은 되지만 비용을 못 넘음"
        out["exploratory"][f"h{h}"] = explore(rows, h)
    out["verdict"] = combine([out["primary"][f"h{h}"]["verdict"] for h in HORIZONS])
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("obs")
    a.add_argument("--ledger-dir", required=True)
    a.add_argument("--from", dest="start", help="창 시작일 YYYY-MM-DD(포함) — 이전 창과 겹치지 않게 끊는다")
    b = sub.add_parser("report")
    c = sub.add_parser("prices")
    c.add_argument("--out-dir", required=True)
    c.add_argument("--start", required=True, help="가격 시작일 YYYY-MM-DD — 첫 관측일 이전")
    for x in (b, c):
        x.add_argument("--obs", required=True)
    b.add_argument("--price-dir", required=True)
    b.add_argument("--bench-adjust", required=True, help="KIS 수정 종가 CSV(date, close) — 벤치마크 분배금 보정")
    b.add_argument("--allow-partial-prices", action="store_true")
    for x in (a, b, c):
        x.add_argument("--through", required=True, help="분석 종료일 YYYY-MM-DD(KST, 포함)")
    for x in (a, b):
        x.add_argument("--out", required=True)
    args = p.parse_args()
    if args.cmd == "obs":
        if args.start is not None and args.start > args.through:
            raise SystemExit(f"--from {args.start} 이 --through {args.through} 보다 늦다")
        obs, stats = build_obs(args.ledger_dir, args.through, args.start)
        with open(args.out, "w") as f:
            f.writelines(json.dumps(o, ensure_ascii=False) + "\n" for o in obs)
        print(json.dumps(stats, ensure_ascii=False, indent=1))
        end = stats["window"]["end"]
        if end is not None and end < max(o["date"] for o in obs):
            print(f"창이 {end} 에 찼다 — 판독용 obs 는 --through {end} 로 다시 만든다", file=sys.stderr)
        return
    obs = [json.loads(line) for line in open(args.obs)]
    if args.cmd == "prices":
        fetch_prices([BENCH, *used_symbols(obs)], args.out_dir, args.start, args.through)
        return
    out = report(obs, args.price_dir, args.bench_adjust, args.through, args.allow_partial_prices)
    with open(args.out, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(out["verdict"], {k: {x: v[x] for x in ("symbol_days", "days_both", "verdict")} for k, v in out["primary"].items()})


if __name__ == "__main__":
    main()
