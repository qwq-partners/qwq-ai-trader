#!/usr/bin/env python3
"""팀 심의 원장의 분석가 보고 자료 상태 집계(읽기 전용).

원장의 문자열(요약·근거·오류·한계 문구)과 종목 코드는 출력하지 않는다 — 수치와 아래 고정 분류 이름만 낸다.

진단 문서: docs/reviews/team-input-diagnosis-2026-11-14.md
  python scripts/research/team_input_diag.py --team-dir ~/.cache/ai_trader/team_ledger --through 2026-11-13 --out summary.json
같은 deliberation_id 는 마지막 행이 정본(src/agents/team_ledger.load_day(dedup=True) 와 같은 계약). 날짜는 파일 이름(KST).
"""
import argparse
import glob
import hashlib
import json
import os
import re
from collections import Counter, defaultdict
from statistics import median

# 코드가 만드는 고정 문구만 분류한다. 그 밖의 문자열은 내용을 옮기지 않고 'other' 로 센다.
ERRORS = {"지표 없음": "no_indicators"}
LIMITS = {"헤드라인 기반": "headline_based", "캐시 시각 미제공": "cache_time_missing", "지표 시각 미상": "indicator_time_unknown"}


def _error_class(text):
    text = str(text)
    return ERRORS.get(text, "timeout" if text.startswith("타임아웃") else "other")


def load(team_dir, through):
    rows, digest = [], hashlib.sha256()
    for f in sorted(glob.glob(os.path.join(team_dir, "deliberations_*.jsonl"))):
        m = re.fullmatch(r"deliberations_(\d{4})(\d{2})(\d{2})\.jsonl", os.path.basename(f))
        day = "-".join(m.groups()) if m else None
        if day is None or day > through:
            continue
        with open(f, "rb") as fh:
            raw = fh.read()
        digest.update(os.path.basename(f).encode() + hashlib.sha256(raw).digest())
        by = {}
        for n, line in enumerate(raw.decode("utf-8", "replace").splitlines()):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            by[str(r["deliberation_id"]) if r.get("deliberation_id") is not None else f"row{n}"] = r
        rows += [(day, r) for r in by.values()]
    return rows, digest.hexdigest()


def summarize(rows):
    kinds = defaultdict(lambda: {"n": 0, "status": Counter(), "error": Counter(), "limits": Counter(), "evidence": [],
                                 "score_conf": Counter(), "score_sign": Counter(), "false_metrics": Counter()})
    slot_err, sym_rows, sym_err, calls = defaultdict(Counter), Counter(), Counter(), Counter()
    day_err = defaultdict(Counter)
    decision, suff = defaultdict(Counter), Counter()
    for day, r in rows:
        sym = r.get("symbol")
        sym_rows[sym] += 1
        mc = r.get("model_calls")
        calls[sym] += len(mc) if isinstance(mc, list) else 0   # 토론 턴(요청 자리) 수 — 응답이 없던 턴도 들어 있어 실제 전송 수가 아니다
        a = r.get("assessment")
        suff[str(a.get("data_sufficiency")) if isinstance(a, dict) else "없음"] += 1
        tech_error = False
        for rep in r.get("reports") if isinstance(r.get("reports"), list) else []:
            k = kinds[str(rep.get("kind"))]
            k["n"] += 1
            k["status"][str(rep.get("data_status"))] += 1
            k["evidence"].append(len(rep["evidence"]) if isinstance(rep.get("evidence"), list) else 0)
            if rep.get("error") is not None:
                k["error"][_error_class(rep["error"])] += 1
            for x in rep["limitations"] if isinstance(rep.get("limitations"), list) else []:
                k["limits"][LIMITS.get(str(x), "other")] += 1
            s = rep.get("score")
            k["score_sign"]["양" if s > 0 else "음" if s < 0 else "0"] += isinstance(s, (int, float))
            for name, v in (rep["metrics"] if isinstance(rep.get("metrics"), dict) else {}).items():
                k["false_metrics"][name] += v is False
            if rep.get("kind") == "fundamental":
                k["score_conf"][f'{s}|{rep.get("confidence")}'] += 1
            if rep.get("kind") == "technical" and rep.get("error") is not None:
                tech_error = True
                sym_err[sym] += 1
                slot_err[str(r.get("slot"))][sym] += 1
                day_err[day][sym] += 1
        d = r.get("decision") if isinstance(r.get("decision"), dict) else {}
        decision["기술 오류" if tech_error else "기술 정상"][f'{d.get("stance")}|{d.get("approved")}'] += 1
    top = sym_err.most_common(1)[0][0] if sym_err else None   # 기술 오류가 가장 많은 종목(코드는 출력하지 않는다)
    return {"rows": len(rows), "days": len({d for d, _r in rows}), "symbols": len(sym_rows),
            "kinds": {name: {"n": k["n"], "status": dict(k["status"]), "error": dict(k["error"]), "limits": dict(k["limits"]),
                             "evidence_median": median(k["evidence"]) if k["evidence"] else None,
                             "score_sign": dict(k["score_sign"]), "always_false_metrics": sorted(
                                 m for m, c in k["false_metrics"].items() if c == k["n"]),
                             "score_conf": dict(k["score_conf"].most_common(6))}
                      for name, k in sorted(kinds.items())},
            "data_sufficiency": dict(suff), "decision": {k: dict(v) for k, v in decision.items()},
            "debate_turns": sum(calls.values()),
            "tech_error": {"total": sum(sym_err.values()), "symbols": len(sym_err),
                           "top_symbol": ({"errors": sym_err[top], "rows": sym_rows[top], "debate_turns": calls[top]}
                                          if top is not None else None),
                           "by_slot_top_symbol": {s: c[top] for s, c in sorted(slot_err.items())},
                           "by_slot_others": {s: sum(c.values()) - c[top] for s, c in sorted(slot_err.items())},
                           "by_day_others": {d: n for d, n in sorted((d, sum(c.values()) - c[top]) for d, c in day_err.items()) if n}}}


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--team-dir", required=True)
    p.add_argument("--through", required=True, help="종료일 YYYY-MM-DD(파일 이름 기준, 포함)")
    p.add_argument("--out", required=True)
    args = p.parse_args()
    rows, digest = load(args.team_dir, args.through)
    out = {"through": args.through, "input_sha256": digest, **summarize(rows)}
    with open(os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:   # 운영 원장 집계 — 비공개 권한
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
