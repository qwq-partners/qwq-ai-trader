#!/usr/bin/env python3
"""Print a bounded, file-only LLM capture readiness report."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.analytics.capture_readiness import DEFAULT_MAX_BYTES, DEFAULT_MAX_STATE_AGE_SECONDS, build_capture_readiness


def main(argv=None):
    parser = argparse.ArgumentParser(description="Offline LLM capture readiness; no provider or account access.")
    parser.add_argument("--study", required=True)
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--decisions")
    parser.add_argument("--analysis-as-of")
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--max-state-age-seconds", type=float, default=DEFAULT_MAX_STATE_AGE_SECONDS)
    args = parser.parse_args(argv)
    try:
        result = build_capture_readiness(args.study, args.ledger, decisions_path=args.decisions,
                                         analysis_as_of=args.analysis_as_of, max_bytes=args.max_bytes,
                                         max_state_age_seconds=args.max_state_age_seconds)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False, sort_keys=True))
        return 0
    except (ValueError, TypeError, KeyError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
