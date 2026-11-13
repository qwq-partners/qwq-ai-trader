#!/usr/bin/env python3
"""V05 — 저장된 팀 심의 입력(분석가 보고)을 다른 모델로 다시 판정하고 사후 초과수익과 대조한다(측정 전용).

주문·설정·운영 원장과 무관하다. 사전 등록: docs/research/v05-team-rejudge-2026-11-13.md
  items  : 종목-일의 첫 심의(V01 계열 C 와 같은 계약) → 축약 입력·운영 판정·판정 순서(해시) 를 비공개 파일로
  judge  : 모델 1개로 재판정(이어 하기 가능, 보수 추정 비용이 상한에 닿으면 전송 중지). 월 예산 원장 밖 호출이다
  report : 재판정 × 사후 순초과(V01 가격 캐시·산식) → 같은 날 승인−비승인 차이, 블록 부트스트랩 구간, 판정
"""
import argparse
import asyncio
import glob
import hashlib
import json
import math
import os
import random
import sys
from collections import defaultdict
from decimal import Decimal
from statistics import mean, median

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from scripts.research.candidate_forward_excess import BENCH, COST, adjust_bench, forward, load_bars  # noqa: E402

HORIZONS = (5, 10, 20)
THRESHOLD = 0.015                 # 승인−비승인 20일 순초과 차이 기준(평가 문서 V05)
MIN_GROUP, MIN_DAYS = 30, 10      # 미달이면 '판정 불가(표본 부족)'
BLOCK, RESAMPLES, SEED = 5, 10000, 20261113
MAX_TOKENS = {"claude-fable-5-2": 3072, "claude-opus-5-5": 1024}   # Fable 은 thinking 이 max_tokens 에 포함된다
EFFORT = {"claude-fable-5-2": "medium", "claude-opus-5-5": "low"}   # 운영 보고 역할 / 운영 Bear 와 같은 설정
LIST_PRICE = {"claude-fable-5-2": (8.0, 40.0), "claude-opus-5-5": (4.0, 20.0)}   # USD/MTok, 캐시 미사용 정가
PROMPT_VERSION = "v05-rejudge-1"
SYSTEM = (
    "당신은 한국 주식 스윙 매매(보유 5~20거래일) 팀의 최종 심의자입니다.\n"
    "입력은 판단 시각에 분석가 3명(재무·기술·뉴스)이 낸 보고입니다. 보고에 없는 사실을 지어내지 말고, "
    "자료 부족·오류 표시는 불확실성으로 반영하세요.\n"
    "질문: 이 종목을 오늘 종가에 사서 20거래일 보유하면 같은 기간 KODEX200(코스피200 ETF)보다 수익률이 높겠습니까?\n"
    "buy 는 초과수익을 기대할 근거가 보고에 있을 때만 고릅니다.\n"
    '출력은 JSON 객체 하나만 씁니다: {"decision":"buy" 또는 "pass","score":0~100 정수(초과수익 가능성이 높을수록 큼, '
    '50=판단 불가),"reason":"80자 이내"}'
)
_DROP = {"symbol", "ref_id", "dedup_key", "collected_at", "valid_until", "expiry_reason", "usable", "age_minutes",
         "validation_pass_bonus", "positive_basis", "risk_clear"}


def compact(o):
    """보고에서 None·빈 값·내부 식별 필드를 뺀다. error 는 본문 대신 표시만 남긴다."""
    if isinstance(o, dict):
        out = {}
        for k, v in o.items():
            if k in _DROP:
                continue
            if k == "error":
                if v is not None:
                    out[k] = "자료 오류"
                continue
            c = compact(v)
            if c is not None and c != {} and c != [] and c != "":
                out[k] = c
        return out
    if isinstance(o, list):
        return [c for c in (compact(x) for x in o) if c is not None and c != {} and c != [] and c != ""]
    return o


def build_items(team_dir, through):
    """종목-일의 첫 심의(같은 deliberation_id 는 마지막 행이 정본) → 항목 목록. 순서는 내용과 무관한 해시."""
    first = {}
    for f in sorted(glob.glob(os.path.join(team_dir, "deliberations_*.jsonl"))):
        by_id = {}
        for n, line in enumerate(open(f)):
            r = json.loads(line)
            by_id[str(r["deliberation_id"]) if r.get("deliberation_id") is not None else f"row{n}"] = r
        for r in by_id.values():
            t = r["decided_at"] if r.get("decided_at") is not None else r["recorded_at"]
            k = (r["symbol"], t[:10])
            if k[1] <= through and (k not in first or t < first[k][0]):
                first[k] = (t, r)
    items = []
    for (symbol, day), (t, r) in first.items():
        d = r["decision"] if r.get("decision") is not None else {}
        approved = d.get("approved")
        label = ("invalid" if not isinstance(approved, bool)
                 else "buy_approved" if d.get("stance") == "buy" and approved is True
                 else "hold" if d.get("stance") == "hold" and approved is True
                 else "sell" if d.get("stance") == "sell" else "not_approved")
        reports = compact(r.get("reports") if r.get("reports") is not None else [])
        scores = [x["score"] for x in reports if isinstance(x.get("score"), (int, float)) and not isinstance(x.get("score"), bool)]
        items.append({"id": f"{day}:{symbol}", "date": day, "symbol": symbol,
                      "name": r["name"] if r.get("name") is not None else "", "decided_at": t,
                      "prod": label, "analyst_sum": sum(scores) if len(scores) == len(reports) and reports else None,
                      "order": hashlib.sha256(f"v05|{day}|{symbol}".encode()).hexdigest(), "reports": reports})
    return sorted(items, key=lambda x: x["order"])


def prompt_of(item):
    return (f"판단 시각: {item['decided_at'][:16].replace('T', ' ')} KST\n종목: {item['name']}({item['symbol']})\n"
            f"분석가 보고(JSON):\n{json.dumps(item['reports'], ensure_ascii=False, separators=(',', ':'))}")


def parse_judgment(text):
    """응답 본문 → (decision, score) 또는 None. 코드 펜스·앞뒤 글은 허용하되 값은 엄격히 본다."""
    if not isinstance(text, str):
        return None
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b <= a:
        return None
    try:
        o = json.loads(text[a:b + 1])
    except ValueError:
        return None
    if not isinstance(o, dict):
        return None
    d, s = o.get("decision"), o.get("score")
    if d not in ("buy", "pass") or isinstance(s, bool) or not isinstance(s, int) or not 0 <= s <= 100:
        return None
    return d, s


async def judge(items, model, out_path, budget_usd, concurrency=4, limit=None, client=None, quote=None):
    """항목을 순서대로 판정해 out_path(JSONL)에 덧붙인다. 이미 있는 id 는 건너뛴다(이어 하기)."""
    done = {}
    if os.path.exists(out_path):
        for line in open(out_path):
            r = json.loads(line)
            done[r["id"]] = r
    budget_usd = Decimal(str(budget_usd))
    spent = sum((Decimal(str(r["est_usd"])) for r in done.values() if r.get("est_usd") is not None), Decimal(0))
    todo = [x for x in items if x["id"] not in done][:limit]
    if client is None:   # 운영 전송 코드를 그대로 쓴다(키 파일 검사·신원 확인 포함)
        from src.utils.claude_transport import AnthropicAPIClient
        from src.utils.llm import LLMConfig
        from src.utils.llm_pricing import rate_quote
        client = AnthropicAPIClient(LLMConfig(claude_model="claude-opus-5-5",
                                              anthropic_api_key_file="~/.config/qwq-ai-trader/anthropic-api-key"))
        quote = lambda i, o: rate_quote(model, i, o).cost_usd   # noqa: E731 — 보수 추정(입력 2배 상한)
    usd = lambda i, o: Decimal(str(quote(i, o)))   # noqa: E731 — 금액 누계는 Decimal
    worst = lambda x: usd(len(prompt_of(x).encode()) + len(SYSTEM.encode()), MAX_TOKENS[model])   # noqa: E731 — 바이트 ≥ 토큰
    state = {"spent": spent, "held": Decimal(0), "stopped": False, "attempted": 0, "recorded": 0}
    sem, cond = asyncio.Semaphore(concurrency), asyncio.Condition()
    loop = asyncio.get_running_loop()

    async def one(item):
        async with sem:
            hold = worst(item) * 2   # 재시도 1회 포함
            async with cond:
                while True:
                    # 영구 정지는 '이미 쓴 금액 + 이 요청의 최악값'이 상한을 넘을 때뿐이다.
                    if state["stopped"] or state["spent"] + hold > budget_usd:
                        state["stopped"] = True
                        cond.notify_all()
                        return
                    if state["spent"] + state["held"] + hold <= budget_usd:
                        break
                    await cond.wait()   # 다른 요청의 보류분 때문에 잠시 모자란 것 — 반환되면 다시 본다
                state["held"] += hold
                state["attempted"] += 1
            rec = {"id": item["id"], "model": model, "prompt_version": PROMPT_VERSION, "attempts": 0,
                   "in": 0, "out": 0, "est_usd": "0", "decision": None, "score": None, "status": "failed"}
            est = Decimal(0)
            for _ in range(2):
                r = await client.complete(prompt_of(item), SYSTEM, model=model, max_tokens=MAX_TOKENS[model],
                                          reasoning_effort=EFFORT[model], deadline=loop.time() + 120)
                rec["attempts"] += 1
                if r.usage_known:
                    tin = r.input_tokens if r.input_tokens is not None else 0
                    tout = r.output_tokens if r.output_tokens is not None else 0
                    rec["in"] += tin
                    rec["out"] += tout
                    est += usd(tin, tout)
                elif r.request_sent:
                    est += worst(item)   # 사용량 미확인 전송은 최악값으로 잡는다
                rec.update(resolved=r.resolved_model, identity=r.model_identity_status, finish=r.finish_reason,
                           http=r.http_status, error_kind=r.transport_error_kind)
                if r.refusal_detected:
                    rec["status"] = "refusal"   # 거절은 다시 보내지 않는다
                    break
                parsed = parse_judgment(r.content) if r.success else None
                if parsed is not None and r.model_identity_status == "verified":
                    rec.update(decision=parsed[0], score=parsed[1], status="ok")
                    break
            rec["est_usd"] = str(est)   # 금액은 Decimal 문자열로 남긴다
            async with cond:
                state["held"] -= hold
                state["spent"] += est
                state["recorded"] += 1
                with open(out_path, "a") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                cond.notify_all()

    await asyncio.gather(*(one(x) for x in todo))
    if hasattr(client, "close"):
        await client.close()
    return {"model": model, "attempted_now": state["attempted"], "recorded_now": state["recorded"],
            "remaining": len(todo) - state["recorded"], "spent_est_usd": str(state["spent"].quantize(Decimal("0.0001"))),
            "budget_stopped": state["stopped"]}


def within_day(rows, key):
    """rows=[(date, in_group: bool, value dict)] → 같은 날 (집단 − 나머지) 차이. 날짜 가중치는 n1·n0/(n1+n0)."""
    by = defaultdict(lambda: ([], []))
    for d, g, v in rows:
        if v.get(key) is not None:
            by[d][0 if g else 1].append(v[key])
    days = [d for d in sorted(by) if by[d][0] and by[d][1]]
    return [(d, mean(by[d][0]) - mean(by[d][1]), len(by[d][0]) * len(by[d][1]) / (len(by[d][0]) + len(by[d][1]))) for d in days]


def wmean(parts):
    w = sum(p[2] for p in parts)
    return sum(p[1] * p[2] for p in parts) / w if w > 0 else None


def weighted_nw_t(parts, lag, pos=None):
    """가중 평균(wmean)과 **같은 통계**의 Newey-West t. parts=[(date, diff, weight)], pos={date: 거래일 위치}.

    x_i = w_i·(d_i − m) 의 HAC 분산을 (Σw)² 로 나눈다. 가중치가 모두 같으면 단순 평균의 NW t 와 같다.
    """
    m = wmean(parts)
    if m is None or len(parts) < 2:
        return None
    at = {(pos[d] if pos is not None else i): w * (x - m) for i, (d, x, w) in enumerate(parts)}
    var = sum(v * v for v in at.values())
    for k in range(1, lag + 1):
        var += 2 * (1 - k / (lag + 1)) * sum(e * at[i - k] for i, e in at.items() if i - k in at)
    return m * sum(p[2] for p in parts) / math.sqrt(var) if var > 0 else None


def block_bootstrap(parts, block=BLOCK, resamples=RESAMPLES, seed=SEED):
    """날짜 순서의 원형 블록 부트스트랩(겹치는 보유 기간의 자기상관 대응) → 가중 평균의 95% 구간."""
    n = len(parts)
    if n < 2:
        return None, None
    rng, out = random.Random(seed), []
    for _ in range(resamples):
        pick = []
        while len(pick) < n:
            s = rng.randrange(n)
            pick.extend(parts[(s + j) % n] for j in range(min(block, n - len(pick))))
        out.append(wmean(pick))
    out.sort()
    return out[int(0.025 * resamples)], out[int(0.975 * resamples) - 1]


def verdict(point, lo, hi, n_in, n_out, days):
    if n_in < MIN_GROUP or n_out < MIN_GROUP or days < MIN_DAYS or point is None or lo is None:
        return "판정 불가(표본 부족)"
    if point > THRESHOLD and lo > 0:
        return "선별력 있음"
    if point < -THRESHOLD and hi < 0:
        return "음의 선별"
    return "미입증"


def compare(rows, key, pos=None):
    """rows=[(date, in_group, value dict)] → 같은 날 차이의 점추정·구간·판정과 단순 집단 평균(참고)."""
    parts = within_day(rows, key)
    point = wmean(parts)
    lo, hi = block_bootstrap(parts)
    ins = [v[key] for _d, g, v in rows if g and v.get(key) is not None]
    outs = [v[key] for _d, g, v in rows if not g and v.get(key) is not None]
    lag = int(key[3:]) - 1 if key.startswith("net") else 0
    return {"n_in": len(ins), "n_out": len(outs), "days_both": len(parts), "diff": point, "ci95": [lo, hi],
            "t_nw": weighted_nw_t(parts, lag, pos),
            "pooled_in": mean(ins) if ins else None, "pooled_out": mean(outs) if outs else None,
            "verdict": verdict(point, lo, hi, len(ins), len(outs), len(parts))}


def split_by_day_median(rows, score_of):
    """같은 날 점수 중앙값보다 크면 True, 작으면 False, 같으면 제외 → compare 입력."""
    by = defaultdict(list)
    for item, v in rows:
        s = score_of(item)
        if s is not None:
            by[item["date"]].append((s, item, v))
    out = []
    for d, xs in by.items():
        m = median(s for s, _i, _v in xs)
        out.extend((d, s > m, v) for s, _i, v in xs if s != m)
    return out


def outcomes(items, price_dir, bench_adjust, through):
    bench = adjust_bench(load_bars(os.path.join(price_dir, f"{BENCH}.csv")), bench_adjust, through)
    cal = sorted(bench)
    cache, rows = {}, []
    for it in items:
        s = it["symbol"]
        if s not in cache:
            path = os.path.join(price_dir, f"{s}.csv")
            if not os.path.exists(path):
                raise SystemExit(f"가격 파일 없음: {s}")
            cache[s] = {d: v for d, v in load_bars(path).items() if d <= through}
        v = {}
        for h in HORIZONS:
            res, _why = forward(cache[s], cal, it["date"], "close", h)
            if res is not None:
                b, _ = forward(bench, cal, it["date"], "close", h)
                v[f"net{h}"] = float(res[0] - b[0] - COST)
        rows.append((it, v))
    return rows, {d: i for i, d in enumerate(cal)}


def check_judgments(items, model, js):
    """판정 파일이 그 모델·프롬프트의 기대 표본 안에서 항목마다 한 번씩만 담는지 본다. 어긋나면 집계하지 않는다.

    비용 상한으로 멈춘 부분 표본(기대 표본의 부분집합)은 허용한다 — 사전 등록이 그 경우의 보고를 정했다.
    """
    ids = [j.get("id") for j in js]
    want = {i["id"] for i in items}
    problems = []
    if len(ids) != len(set(ids)):
        problems.append("중복 id")
    if set(ids) - want:
        problems.append(f"표본 밖 id {len(set(ids) - want)}")
    if any(j.get("model") != model or j.get("prompt_version") != PROMPT_VERSION for j in js):
        problems.append("모델 또는 프롬프트 버전 불일치")
    if any(j.get("status") == "ok" and (j.get("identity") != "verified" or j.get("resolved") != model
                                        or parse_judgment(json.dumps({"decision": j.get("decision"), "score": j.get("score")})) is None)
           for j in js):
        problems.append("성공 행의 응답 신원·판정 값 불일치")
    if problems:
        raise SystemExit(f"판정 파일 검증 실패({model}): {', '.join(problems)}")


def report(items, judgments, price_dir, bench_adjust, through):
    for model, js in judgments.items():
        check_judgments(items, model, js)
    rows, pos = outcomes(items, price_dir, bench_adjust, through)
    out = {"prompt_version": PROMPT_VERSION, "through": through, "threshold": THRESHOLD, "items": len(items),
           "with_net": {f"h{h}": sum(f"net{h}" in v for _i, v in rows) for h in HORIZONS}, "models": {}, "reference": {}}
    ref = {"prod_buy_approved": [(i["date"], i["prod"] == "buy_approved", v) for i, v in rows if i["prod"] != "invalid"],
           "analyst_sum_high": split_by_day_median(rows, lambda i: i["analyst_sum"])}
    out["reference"] = {name: {f"h{h}": compare(rs, f"net{h}", pos) for h in HORIZONS} for name, rs in ref.items()}
    for model, js in judgments.items():
        ok = {j["id"]: j for j in js if j["status"] == "ok"}
        sel = [(i, v) for i, v in rows if i["id"] in ok]
        m = {"judged": len(js), "ok": len(ok), "complete": len(js) == len(items),   # False = 비용 상한 등으로 멈춘 부분 표본
             "judged_ids_sha256": hashlib.sha256("\n".join(sorted(j["id"] for j in js)).encode()).hexdigest(),
             "buy_share": mean(ok[i["id"]]["decision"] == "buy" for i, _v in sel) if sel else None,
             "tokens_in": sum(j["in"] for j in js), "tokens_out": sum(j["out"] for j in js),
             "est_usd_conservative": str(sum((Decimal(str(j["est_usd"])) for j in js), Decimal(0)).quantize(Decimal("0.0001"))),
             "list_usd": str((sum((j["in"] * Decimal(str(LIST_PRICE[model][0])) + j["out"] * Decimal(str(LIST_PRICE[model][1]))
                                   for j in js), Decimal(0)) / 1000000).quantize(Decimal("0.0001")))}
        status = defaultdict(int)
        for j in js:
            status[j["status"]] += 1
        m["status"] = dict(sorted(status.items()))
        m["decision"] = {f"h{h}": compare([(i["date"], ok[i["id"]]["decision"] == "buy", v) for i, v in sel], f"net{h}", pos)
                         for h in HORIZONS}
        m["score_high"] = {f"h{h}": compare(split_by_day_median(sel, lambda i: ok[i["id"]]["score"]), f"net{h}", pos)
                           for h in HORIZONS}
        m["agree_prod_buy"] = mean((ok[i["id"]]["decision"] == "buy") == (i["prod"] == "buy_approved") for i, _v in sel) if sel else None
        failed = len(js) - len(ok)
        m["primary"] = "판정 불가(실패율)" if js and failed / len(js) > 0.10 else m["decision"]["h20"]["verdict"]
        out["models"][model] = m
    names = list(judgments)
    if len(names) == 2:
        a = {j["id"]: j["decision"] for j in judgments[names[0]] if j["status"] == "ok"}
        b = {j["id"]: j["decision"] for j in judgments[names[1]] if j["status"] == "ok"}
        both = set(a) & set(b)
        out["model_agreement"] = {"n": len(both), "same": mean(a[k] == b[k] for k in both) if both else None}
    return out


def _pct(x):
    return "—" if x is None else f"{x * 100:+.2f}"


def doc_rows(summary):
    """커밋된 집계 → 결과 문서의 표 행(문서와 집계가 어긋나지 않게 검사에서 대조한다)."""
    rows = []
    groups = [(f"{m} `buy`−`pass`", v["decision"]) for m, v in summary["models"].items()]
    groups += [(f"{m} 점수 상−하(보조)", v["score_high"]) for m, v in summary["models"].items()]
    groups += [("운영 승인 BUY−나머지(기준선)", summary["reference"]["prod_buy_approved"]),
               ("분석가 점수 합 상−하(기준선)", summary["reference"]["analyst_sum_high"])]
    for name, by_h in groups:
        for h in HORIZONS:
            c = by_h[f"h{h}"]
            rows.append(f"| {name} | {h}일 | {c['n_in']} / {c['n_out']} | {c['days_both']} | {_pct(c['diff'])} | "
                        f"{_pct(c['ci95'][0])} ~ {_pct(c['ci95'][1])} | {'—' if c['t_nw'] is None else format(c['t_nw'], '+.2f')} | "
                        f"{_pct(c['pooled_in'])} / {_pct(c['pooled_out'])} | {c['verdict']} |")
    return rows


def _sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def input_hashes(items_path, judgment_paths, price_dir, bench_adjust, symbols):
    """집계가 어떤 입력에서 나왔는지 — 비공개 파일은 내용 대신 해시만 커밋한다."""
    prices = hashlib.sha256()
    for s in [BENCH, *sorted(symbols)]:
        prices.update(s.encode() + bytes.fromhex(_sha(os.path.join(price_dir, f"{s}.csv"))))
    return {"items_sha256": _sha(items_path), "judgments_sha256": {m: _sha(p) for m, p in judgment_paths.items()},
            "prices_sha256": prices.hexdigest(), "bench_adjust_sha256": _sha(bench_adjust)}


def _load(path):
    return [json.loads(line) for line in open(path)]


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("items")
    a.add_argument("--team-dir", required=True)
    a.add_argument("--through", required=True)
    a.add_argument("--out", required=True)
    a = sub.add_parser("judge")
    a.add_argument("--items", required=True)
    a.add_argument("--model", required=True, choices=sorted(MAX_TOKENS))
    a.add_argument("--out", required=True)
    a.add_argument("--budget-usd", required=True, type=Decimal)
    a.add_argument("--max-date", help="이 날짜 이하 항목만(20일 결과가 있는 주 표본)")
    a.add_argument("--limit", type=int)
    a.add_argument("--concurrency", type=int, default=4)
    a = sub.add_parser("report")
    a.add_argument("--items", required=True)
    a.add_argument("--judgments", required=True, nargs="+", help="model=path")
    a.add_argument("--price-dir", required=True)
    a.add_argument("--bench-adjust", required=True)
    a.add_argument("--through", required=True)
    a.add_argument("--max-date", help="이 날짜 이하 항목만(주 표본) — 기준선도 같은 표본으로 계산한다")
    a.add_argument("--out", required=True)
    args = p.parse_args()
    if args.cmd == "items":
        items = build_items(args.team_dir, args.through)
        fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)   # 분석가 보고 원문이 들어 있다 — 비공개
        with os.fdopen(fd, "w") as f:
            for it in items:
                f.write(json.dumps(it, ensure_ascii=False) + "\n")
        print(json.dumps({"items": len(items), "dates": len({i["date"] for i in items}),
                          "sha256": hashlib.sha256(open(args.out, "rb").read()).hexdigest()}))
    elif args.cmd == "judge":
        items = [i for i in _load(args.items) if args.max_date is None or i["date"] <= args.max_date]
        print(json.dumps(asyncio.run(judge(items, args.model, args.out, args.budget_usd, concurrency=args.concurrency, limit=args.limit)), ensure_ascii=False))
    else:
        paths = dict(x.split("=", 1) for x in args.judgments)
        judgments = {m: _load(path) for m, path in paths.items()}
        items = [i for i in _load(args.items) if args.max_date is None or i["date"] <= args.max_date]
        out = report(items, judgments, args.price_dir, args.bench_adjust, args.through)
        out["max_date"] = args.max_date
        out["inputs"] = input_hashes(args.items, paths, args.price_dir, args.bench_adjust, {i["symbol"] for i in items})
        with open(args.out, "w") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
            f.write("\n")
        print("\n".join(doc_rows(out)))


if __name__ == "__main__":
    main()
