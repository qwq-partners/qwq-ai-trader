#!/usr/bin/env python3
"""Offline-only denominator report for a Toss observation ledger.

It imports neither runtime, authentication nor HTTP modules and never repairs
or appends to its input file.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.data.providers.toss.observation_ledger import ObservationLedger


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Read a Toss observation ledger without side effects")
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--plan-hash", required=True)
    parser.add_argument("--max-bytes", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        summary = ObservationLedger.read_only_summary(args.ledger, plan_hash=args.plan_hash, max_bytes=args.max_bytes)
    except (ValueError, TypeError):
        summary = {"incomplete": True, "error_code": "invalid_report_input", "production_eligible": False}
    summary["production_eligible"] = False
    print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
    return 0 if not summary["incomplete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
