#!/usr/bin/env python3
"""Read a sealed, explicitly opted-in study's supplementary quote movement offline."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.report_entry_gate_trace import _study
from src.analytics.entry_observation_journal import read_observation_journal
from src.analytics.pregate_observed_regular_flow import build_observed_regular_flow_report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--study", type=Path, required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--max-journal-bytes", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        if not 1 <= args.max_journal_bytes <= 256 * 1024 * 1024:
            raise ValueError("journal limit outside 1..256MiB")
        raw, _ = _study(args.study)
        sha = hashlib.sha256(raw).hexdigest()
        observations = read_observation_journal(
            args.journal, max_bytes=args.max_journal_bytes, expected_study_sha256=sha)
        report = build_observed_regular_flow_report(
            raw, observations, study_sha256=sha, analysis_as_of=args.as_of)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False))
        return 0
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        print("[체결 관측 후 호가 진단] 입력 계약 오류", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
