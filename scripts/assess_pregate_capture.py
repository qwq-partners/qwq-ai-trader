#!/usr/bin/env python3
"""Print an offline pre-gate capacity assessment for one strict study JSON."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.analytics.pregate_capture_capacity import (READOUT_DEFAULT_MAX_BYTES,
                                                     assess_pregate_capture_resources)
from scripts.report_entry_gate_trace import _study


MAX_STUDY_BYTES = 4 * 1024 * 1024


def read_study(path: str) -> dict:
    # Reuse the established nofollow, regular-file, bounded, stable read.
    _, value = _study(Path(path))
    if type(value) is not dict:
        raise ValueError("study input must be a JSON object")
    return value


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("study", help="bounded regular study JSON file")
    result.add_argument("--operational-registrations", type=int, required=True)
    result.add_argument("--external-registrations", type=int, required=True)
    result.add_argument("--readout-max-bytes", type=int, default=READOUT_DEFAULT_MAX_BYTES)
    return result


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        report = assess_pregate_capture_resources(
            read_study(args.study), operational_registrations=args.operational_registrations,
            external_registrations=args.external_registrations,
            readout_max_bytes=args.readout_max_bytes)
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
        parser().error(str(error))
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
