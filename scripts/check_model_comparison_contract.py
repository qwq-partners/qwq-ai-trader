#!/usr/bin/env python3
"""Evaluate one explicitly supplied offline contract JSON file."""
import argparse
import json
import os
from pathlib import Path
import stat
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.analytics.model_comparison_contract import evaluate_contract

LIMIT = 1024 * 1024


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _constant(_value):
    raise ValueError("invalid_json_number")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="explicit JSON file")
    args = parser.parse_args()
    try:
        # Open nonblocking so a FIFO cannot stall before the regular-file check.
        fd = os.open(args.input, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("input_not_regular_file")
            raw = stream.read(LIMIT + 1)
        if len(raw) > LIMIT:
            raise ValueError("input_too_large")
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_object, parse_constant=_constant)
        report = evaluate_contract(payload)
        print(json.dumps(report, allow_nan=False, sort_keys=True))
    except (OSError, UnicodeError, ValueError, RecursionError):
        print("invalid_input", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
