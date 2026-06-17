#!/usr/bin/env python3
"""LLM Shadow A/B 비교 분석 (Phase 3)

~/.cache/ai_trader/llm_shadow/*.jsonl 을 읽어 primary vs shadow 모델 비교.

지표:
- 성공률 (둘 다 성공, primary만, shadow만, 둘 다 실패)
- JSON 파싱 가능률 (양 모델 모두)
- 응답 크기, 토큰 사용, 지연 분포
- 응답 동등성 (parsed JSON keys 일치)

사용:
    python scripts/analyze_shadow.py
    python scripts/analyze_shadow.py --days 7 --json out.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--log-dir", default=str(Path.home() / ".cache" / "ai_trader" / "llm_shadow"))
    p.add_argument("--json", default="")
    return p.parse_args()


def load_pairs(log_dir: Path, days: int) -> List[dict]:
    cutoff = datetime.now().date() - timedelta(days=days)
    records: List[dict] = []
    if not log_dir.exists():
        return records
    for path in sorted(log_dir.glob("*.jsonl")):
        try:
            file_date = datetime.strptime(path.stem, "%Y%m%d").date()
        except ValueError:
            continue
        if file_date < cutoff:
            continue
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return records


def try_parse_json(content: str) -> bool:
    if not content:
        return False
    # Light JSON 추출 (코드 블록 제거)
    s = content.strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", s, re.DOTALL)
    if m:
        s = m.group(1)
    # 첫 { 부터 마지막 } 까지 시도
    first = s.find("{")
    last = s.rfind("}")
    if first >= 0 and last > first:
        s = s[first:last + 1]
    try:
        json.loads(s)
        return True
    except (json.JSONDecodeError, ValueError):
        return False


def keys_overlap(a: str, b: str) -> float:
    """양 응답의 top-level JSON keys 교집합/합집합 비율."""
    try:
        oa = json.loads(a)
        ob = json.loads(b)
        if not isinstance(oa, dict) or not isinstance(ob, dict):
            return 0.0
        sa = set(oa.keys())
        sb = set(ob.keys())
        if not (sa or sb):
            return 1.0
        return len(sa & sb) / max(1, len(sa | sb))
    except Exception:
        return 0.0


def aggregate(records: List[dict]) -> Dict:
    stats = {
        "total_pairs": len(records),
        "both_success": 0,
        "primary_only": 0,
        "shadow_only": 0,
        "both_failed": 0,
        "primary_json_ok": 0,
        "shadow_json_ok": 0,
        "key_overlap_sum": 0.0,
        "key_overlap_count": 0,
        "primary_content_bytes_sum": 0,
        "shadow_content_bytes_sum": 0,
        "shadow_latency_ms_sum": 0,
        "shadow_latency_ms_max": 0,
        "shadow_in_tokens_sum": 0,
        "shadow_out_tokens_sum": 0,
        "primary_in_tokens_sum": 0,
        "primary_out_tokens_sum": 0,
    }
    by_task: Dict[str, Dict[str, int]] = defaultdict(lambda: {
        "total": 0, "both_success": 0, "shadow_failed": 0,
        "primary_json_ok": 0, "shadow_json_ok": 0,
    })
    shadow_errors: Dict[str, int] = defaultdict(int)

    for r in records:
        task = r.get("task", "?")
        p = r.get("primary", {})
        s = r.get("shadow", {})
        p_ok = bool(p.get("success"))
        s_ok = bool(s.get("success"))

        if p_ok and s_ok:
            stats["both_success"] += 1
            by_task[task]["both_success"] += 1
        elif p_ok and not s_ok:
            stats["primary_only"] += 1
            err = s.get("error", "unknown")[:80]
            shadow_errors[err] += 1
            by_task[task]["shadow_failed"] += 1
        elif s_ok and not p_ok:
            stats["shadow_only"] += 1
        else:
            stats["both_failed"] += 1

        p_content = p.get("content", "") or ""
        s_content = s.get("content", "") or ""

        if try_parse_json(p_content):
            stats["primary_json_ok"] += 1
            by_task[task]["primary_json_ok"] += 1
        if try_parse_json(s_content):
            stats["shadow_json_ok"] += 1
            by_task[task]["shadow_json_ok"] += 1

        if p_ok and s_ok and p_content and s_content:
            ov = keys_overlap(p_content, s_content)
            stats["key_overlap_sum"] += ov
            stats["key_overlap_count"] += 1

        stats["primary_content_bytes_sum"] += len(p_content)
        stats["shadow_content_bytes_sum"] += len(s_content)
        stats["shadow_latency_ms_sum"] += int(s.get("latency_ms", 0) or 0)
        stats["shadow_latency_ms_max"] = max(stats["shadow_latency_ms_max"], int(s.get("latency_ms", 0) or 0))
        stats["primary_in_tokens_sum"] += int(p.get("in_tokens", 0) or 0)
        stats["primary_out_tokens_sum"] += int(p.get("out_tokens", 0) or 0)
        stats["shadow_in_tokens_sum"] += int(s.get("in_tokens", 0) or 0)
        stats["shadow_out_tokens_sum"] += int(s.get("out_tokens", 0) or 0)
        by_task[task]["total"] += 1

    return {"stats": stats, "by_task": dict(by_task), "shadow_errors": dict(shadow_errors)}


def render(agg: Dict, days: int) -> str:
    st = agg["stats"]
    n = max(1, st["total_pairs"])
    lines = [
        f"# LLM Shadow A/B 분석 (최근 {days}일)",
        f"- 분석 시각: {datetime.now().isoformat(timespec='seconds')}",
        f"- 총 비교 쌍: {st['total_pairs']}",
        "",
        "## 성공률 분포",
        f"- 둘 다 성공: {st['both_success']} ({st['both_success']/n*100:.1f}%)",
        f"- Primary만 성공: {st['primary_only']} ({st['primary_only']/n*100:.1f}%)",
        f"- Shadow만 성공: {st['shadow_only']} ({st['shadow_only']/n*100:.1f}%)",
        f"- 둘 다 실패: {st['both_failed']} ({st['both_failed']/n*100:.1f}%)",
        "",
        "## JSON 파싱 성공률",
        f"- Primary: {st['primary_json_ok']}/{n} ({st['primary_json_ok']/n*100:.1f}%)",
        f"- Shadow:  {st['shadow_json_ok']}/{n} ({st['shadow_json_ok']/n*100:.1f}%)",
    ]
    if st["key_overlap_count"]:
        avg_ov = st["key_overlap_sum"] / st["key_overlap_count"]
        lines.append("")
        lines.append(f"## Key 동등성 (양 성공 시)\n- 평균 keys overlap: {avg_ov:.2%} ({st['key_overlap_count']}쌍)")

    lines += [
        "",
        "## 응답 크기 / 토큰",
        f"- Primary 평균 content bytes: {st['primary_content_bytes_sum']/n:.0f}",
        f"- Shadow  평균 content bytes: {st['shadow_content_bytes_sum']/n:.0f}",
        f"- Primary in_tokens 합: {st['primary_in_tokens_sum']:,}, out: {st['primary_out_tokens_sum']:,}",
        f"- Shadow  in_tokens 합: {st['shadow_in_tokens_sum']:,}, out: {st['shadow_out_tokens_sum']:,}",
        "",
        "## Shadow 지연 (ms)",
        f"- 평균: {st['shadow_latency_ms_sum']/n:.0f}",
        f"- 최대: {st['shadow_latency_ms_max']}",
    ]

    if agg["shadow_errors"]:
        lines.append("")
        lines.append("## Shadow 실패 사유 Top")
        for err, cnt in sorted(agg["shadow_errors"].items(), key=lambda kv: kv[1], reverse=True)[:5]:
            lines.append(f"- ({cnt}) {err}")

    if agg["by_task"]:
        lines.append("")
        lines.append("## Task별 비교")
        lines.append("| Task | 총 | 둘다성공 | Shadow실패 | Primary JSON OK | Shadow JSON OK |")
        lines.append("|------|----|---------|-----------|------------------|-----------------|")
        for task, st_t in agg["by_task"].items():
            t = max(1, st_t["total"])
            lines.append(
                f"| {task} | {st_t['total']} | {st_t['both_success']} | {st_t['shadow_failed']} "
                f"| {st_t['primary_json_ok']/t*100:.0f}% | {st_t['shadow_json_ok']/t*100:.0f}% |"
            )

    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    log_dir = Path(args.log_dir)
    pairs = load_pairs(log_dir, args.days)
    if not pairs:
        print(f"[WARN] Shadow 로그 없음 ({log_dir}, 최근 {args.days}일)")
        return 1
    agg = aggregate(pairs)
    md = render(agg, args.days)
    print(md)
    if args.json:
        out = {"generated_at": datetime.now().isoformat(timespec="seconds"), "days": args.days, **agg}
        Path(args.json).write_text(json.dumps(out, ensure_ascii=False, indent=2))
        print(f"\n[JSON 저장] {args.json}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
