#!/usr/bin/env python3
"""판단 기록 ↔ 관측 원장 조인 보고 (65차). 네트워크·운영 상태 접근 없음, 입력 불변.

python scripts/report_decision_quotes.py --decisions ~/.cache/ai_trader/llm_decisions/decisions-2026-10.jsonl \
    --ledger <engine.jsonl> [--study-sha256 <sha>] [--max-delay-seconds 60] [--max-bytes 268435456]
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.analytics.decision_quote_join import (  # noqa: E402
    DEFAULT_MAX_BYTES, DEFAULT_MAX_DELAY_SECONDS, join_decisions, load_decisions, load_ledger_payloads, summarize,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--decisions", required=True)
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--study-sha256", default=None,
                        help="주면 기존 strict 리더로 해시 체인·헤더를 먼저 검증한다(실패 시 종료 2)")
    parser.add_argument("--max-delay-seconds", type=float, default=DEFAULT_MAX_DELAY_SECONDS)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    args = parser.parse_args()
    if args.study_sha256:
        from src.analytics.entry_observation_journal import read_observation_journal
        try:
            read_observation_journal(args.ledger, max_bytes=args.max_bytes, expected_study_sha256=args.study_sha256)
        except Exception as error:
            print(json.dumps({"error": f"ledger strict verification failed: {error}"}, ensure_ascii=False))
            return 2
    rows = join_decisions(load_decisions(args.decisions, max_bytes=args.max_bytes),
                          load_ledger_payloads(args.ledger, max_bytes=args.max_bytes),
                          max_delay_seconds=args.max_delay_seconds)
    print(json.dumps({"summary": summarize(rows), "rows": rows}, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
