#!/usr/bin/env python3
"""LLM 사용 베이스라인 수집 (Phase 1 — GPT-5 deprecation 마이그레이션)

~/.cache/ai_trader/llm_responses/*.jsonl 을 읽어 (task, model)별 통계 집계.
출력: 콘솔 markdown + JSON 파일.

사용:
    python scripts/llm_baseline.py                  # 최근 7일
    python scripts/llm_baseline.py --days 14        # 최근 14일
    python scripts/llm_baseline.py --json out.json  # JSON 저장

마이그레이션 우선순위 판단:
- 호출 빈도 높은 task/model 조합 = 교체 후 영향 큼 (Shadow A/B 우선)
- 응답 크기 평균 = 토큰 비용 비례 (proxy)
- snapshot ID 노출 시 (예: gpt-5-mini-2025-08-07) → deprecation 직접 대상

로그 필드 한계: 현재 tokens/latency 미기록 → 응답 raw 길이로 추정.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List


# Deprecation 대상 snapshot IDs (2026-12-10 셧다운)
DEPRECATED_SNAPSHOTS = {
    "gpt-5-2025-08-07",
    "gpt-5-mini-2025-08-07",
    "gpt-5-nano-2025-08-07",
    "gpt-5-pro-2025-10-06",
}

# Alias → 가능한 snapshot mapping (참고용)
KNOWN_ALIASES = {"gpt-5", "gpt-5-mini", "gpt-5-nano", "gpt-5-pro"}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="LLM 사용 베이스라인 수집")
    p.add_argument("--days", type=int, default=7, help="분석 기간 (기본 7일)")
    p.add_argument("--log-dir", default=str(Path.home() / ".cache" / "ai_trader" / "llm_responses"))
    p.add_argument("--json", help="JSON 출력 파일 경로 (선택)")
    return p.parse_args()


def load_logs(log_dir: Path, days: int) -> List[dict]:
    cutoff = datetime.now().date() - timedelta(days=days)
    records: List[dict] = []
    if not log_dir.exists():
        print(f"[ERROR] 로그 디렉토리 없음: {log_dir}", file=sys.stderr)
        return records

    for path in sorted(log_dir.glob("*.jsonl")):
        # 파일명: YYYYMMDD.jsonl
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


def aggregate(records: List[dict]) -> Dict:
    """(task, model)별 집계."""
    by_pair: Dict[tuple, Dict] = defaultdict(lambda: {
        "count": 0,
        "success": 0,
        "raw_bytes_total": 0,
        "raw_bytes_max": 0,
    })
    snapshot_seen: Dict[str, int] = defaultdict(int)
    deprecated_hits: Dict[str, int] = defaultdict(int)

    for rec in records:
        task = rec.get("task", "?")
        model = rec.get("model", "?")
        key = (task, model)
        stats = by_pair[key]
        stats["count"] += 1
        if rec.get("success"):
            stats["success"] += 1
        raw = rec.get("raw", "") or ""
        n = len(raw)
        stats["raw_bytes_total"] += n
        stats["raw_bytes_max"] = max(stats["raw_bytes_max"], n)

        if model in DEPRECATED_SNAPSHOTS:
            deprecated_hits[model] += 1
        snapshot_seen[model] += 1

    return {
        "by_pair": {f"{t}|{m}": v for (t, m), v in by_pair.items()},
        "snapshot_seen": dict(snapshot_seen),
        "deprecated_hits": dict(deprecated_hits),
    }


def render_markdown(agg: Dict, days: int, total_records: int) -> str:
    lines = [
        f"# LLM 베이스라인 (최근 {days}일)",
        f"- 분석 시각: {datetime.now().isoformat(timespec='seconds')}",
        f"- 총 호출: {total_records}건",
        "",
        "## Task × Model 호출 분포",
        "| Task | Model | 호출 | 성공률 | 평균 raw bytes | 최대 raw bytes |",
        "|------|-------|------|--------|----------------|----------------|",
    ]
    # 호출 많은 순 정렬
    pairs = sorted(
        agg["by_pair"].items(),
        key=lambda kv: kv[1]["count"],
        reverse=True,
    )
    for key, st in pairs:
        task, model = key.split("|", 1)
        count = st["count"]
        succ = st["success"]
        succ_rate = (succ / count * 100) if count else 0
        avg = (st["raw_bytes_total"] / count) if count else 0
        lines.append(
            f"| {task} | {model} | {count} | {succ_rate:.1f}% | {avg:,.0f} | {st['raw_bytes_max']:,} |"
        )

    lines.extend([
        "",
        "## 모델별 호출 합계 (마이그레이션 우선순위)",
        "| Model | 호출 | Deprecation 대상 |",
        "|-------|------|------------------|",
    ])
    for model, n in sorted(agg["snapshot_seen"].items(), key=lambda kv: kv[1], reverse=True):
        is_dep = "🔴 직접 대상" if model in DEPRECATED_SNAPSHOTS else (
            "🟡 alias (snapshot 미확인)" if model in KNOWN_ALIASES else "🟢"
        )
        lines.append(f"| {model} | {n} | {is_dep} |")

    if agg["deprecated_hits"]:
        lines.extend([
            "",
            "## ⚠️ Deprecation 직접 대상 사용 감지",
        ])
        for m, n in agg["deprecated_hits"].items():
            lines.append(f"- **{m}**: {n}회 (2026-12-10 셧다운)")
    else:
        lines.extend([
            "",
            "## Deprecation snapshot ID 직접 사용 — 없음",
            "(현재 alias 형태로 사용 중. Phase 2에서 alias → 실제 snapshot 매핑 확인 필요)",
        ])

    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    log_dir = Path(args.log_dir)
    records = load_logs(log_dir, args.days)
    if not records:
        print(f"[WARN] 분석 대상 로그 없음 ({log_dir}, 최근 {args.days}일)")
        return 1

    agg = aggregate(records)
    md = render_markdown(agg, args.days, len(records))
    print(md)

    if args.json:
        out = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "days": args.days,
            "total_records": len(records),
            **agg,
        }
        Path(args.json).write_text(json.dumps(out, ensure_ascii=False, indent=2))
        print(f"\n[JSON 저장] {args.json}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
