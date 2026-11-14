#!/usr/bin/env python3
"""팀 심의 원장의 분석가 보고 자료 상태 집계(읽기 전용).

원장의 값은 그대로 출력하지 않는다 — 건수와, 아래 허용 목록에 있는 분류 이름·유한한 수(점수·신뢰도)만 낸다.
허용 목록 밖의 값(요약·근거·오류·한계 문구, 종목 코드, 모르는 종류·상태·판정·슬롯·지표 이름)은 'other'/'비정상'으로 센다.

진단 문서: docs/reviews/team-input-diagnosis-2026-11-14.md
  python scripts/research/team_input_diag.py --team-dir ~/.cache/ai_trader/team_ledger --through 2026-11-13 \
      --symbol <종목코드> --out summary.json
--symbol 을 주면 그 종목의 기술 오류·토론 턴을 따로 집계하고, 그 종목이 오류 최다 종목과 같은지 표시한다(코드는 출력하지 않는다).
같은 deliberation_id 는 마지막 행이 정본 — 키는 운영 로더(src/agents/team_ledger.load_day(dedup=True))와 같은 str(id) 라
id 가 없는 행들도 하나로 묶인다. 날짜는 파일 이름(KST).
"""
import argparse
import glob
import hashlib
import json
import math
import os
import re
from collections import Counter, defaultdict
from statistics import median

# 코드가 만드는 고정 문구만 분류한다. 그 밖의 문자열은 내용을 옮기지 않고 'other' 로 센다.
ERRORS = {"지표 없음": "no_indicators"}
LIMITS = {"헤드라인 기반": "headline_based", "캐시 시각 미제공": "cache_time_missing", "지표 시각 미상": "indicator_time_unknown"}


KINDS = ("fundamental", "technical", "news")
STATUS = ("full", "partial", "insufficient", "error", "unknown")
STANCES = ("buy", "hold", "sell")
SLOTS = ("10:30", "11:30", "13:00", "14:00")
SUPPLY_METRICS = ("foreign_net_buying", "institutional_net_buying")   # 미획득 기본값이 False 로 적히는지 볼 지표


def _label(value, allowed, missing="없음"):
    """허용 목록에 있는 문자열만 그대로, 없으면 '없음', 그 밖은 'other' — 원장 값을 결과로 옮기지 않는다."""
    return missing if value is None else value if isinstance(value, str) and value in allowed else "other"


def _number(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


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
        for line in raw.decode("utf-8", "replace").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict):
                by[str(r.get("deliberation_id"))] = r
        rows += [(day, r) for r in by.values()]
    return rows, digest.hexdigest()


def summarize(rows, symbol=None):
    kinds = defaultdict(lambda: {"n": 0, "status": Counter(), "error": Counter(), "limits": Counter(), "evidence": [],
                                 "score_conf": Counter(), "score_sign": Counter(), "false_metrics": Counter()})
    slot_err, sym_rows, sym_err, calls = defaultdict(Counter), Counter(), Counter(), Counter()
    day_err = defaultdict(Counter)
    decision, suff = defaultdict(Counter), Counter()
    for day, r in rows:
        sym = r.get("symbol") if isinstance(r.get("symbol"), str) else None   # 문자열이 아닌 종목 값은 한 묶음으로
        sym_rows[sym] += 1
        mc = r.get("model_calls")
        calls[sym] += len(mc) if isinstance(mc, list) else 0   # 토론 턴(요청 자리) 수 — 응답이 없던 턴도 들어 있어 실제 전송 수가 아니다
        a = r.get("assessment")
        suff[_label(a.get("data_sufficiency") if isinstance(a, dict) else None, STATUS)] += 1
        slot = _label(r.get("slot"), SLOTS)
        tech_error = False
        for rep in r.get("reports") if isinstance(r.get("reports"), list) else []:
            if not isinstance(rep, dict):
                continue
            kind = _label(rep.get("kind"), KINDS)
            k = kinds[kind]
            k["n"] += 1
            k["status"][_label(rep.get("data_status"), STATUS)] += 1
            k["evidence"].append(len(rep["evidence"]) if isinstance(rep.get("evidence"), list) else 0)
            if rep.get("error") is not None:
                k["error"][_error_class(rep["error"])] += 1
            for x in rep["limitations"] if isinstance(rep.get("limitations"), list) else []:
                k["limits"][LIMITS.get(str(x), "other")] += 1
            s = rep.get("score")
            numeric = _number(s)   # 비교보다 먼저 — 비정상 점수는 값 없이 따로 센다
            k["score_sign"]["비정상" if not numeric else "양" if s > 0 else "음" if s < 0 else "0"] += 1
            metrics = rep["metrics"] if isinstance(rep.get("metrics"), dict) else {}
            for name in SUPPLY_METRICS:
                k["false_metrics"][name] += metrics.get(name) is False
            if kind == "fundamental":
                c = rep.get("confidence")
                k["score_conf"][f"{s}|{c}" if numeric and _number(c) else "비정상"] += 1
            if kind == "technical" and rep.get("error") is not None:
                tech_error = True
                sym_err[sym] += 1
                slot_err[slot][sym] += 1
                day_err[day][sym] += 1
        d = r.get("decision") if isinstance(r.get("decision"), dict) else {}
        approved = d.get("approved")
        decision["기술 오류" if tech_error else "기술 정상"][
            f'{_label(d.get("stance"), STANCES)}|{approved if isinstance(approved, bool) else "other"}'] += 1
    most = sym_err.most_common(1)[0][0] if sym_err else None   # 기술 오류가 가장 많은 종목(코드는 출력하지 않는다)
    top = symbol if symbol is not None else most             # 지정 종목이 있으면 그 종목을 따로 센다
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
                           "focus": "지정 종목" if symbol is not None else "오류 최다 종목",
                           "focus_is_top_error_symbol": top is not None and top == most,
                           "top_symbol": ({"errors": sym_err[top], "rows": sym_rows[top], "debate_turns": calls[top]}
                                          if top is not None else None),
                           "by_slot_top_symbol": {s: c[top] for s, c in sorted(slot_err.items())},
                           "by_slot_others": {s: sum(c.values()) - c[top] for s, c in sorted(slot_err.items())},
                           "by_day_others": {d: n for d, n in sorted((d, sum(c.values()) - c[top]) for d, c in day_err.items()) if n != 0}}}


def write_private(path, out):
    """운영 원장 집계는 0600 으로 쓴다 — 이미 있던 파일의 권한도 쓰기 전에 좁힌다."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as f:
        os.fchmod(fd, 0o600)
        json.dump(out, f, ensure_ascii=False, indent=1)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--team-dir", required=True)
    p.add_argument("--through", required=True, help="종료일 YYYY-MM-DD(파일 이름 기준, 포함)")
    p.add_argument("--out", required=True)
    p.add_argument("--symbol", help="따로 집계할 종목 코드(출력하지 않는다)")
    args = p.parse_args()
    rows, digest = load(args.team_dir, args.through)
    out = {"through": args.through, "input_sha256": digest, **summarize(rows, args.symbol)}
    write_private(args.out, out)
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
