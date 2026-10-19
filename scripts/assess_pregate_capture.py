#!/usr/bin/env python3
"""Print an offline pre-gate capacity assessment for one strict study JSON."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.analytics.pregate_capture_capacity import (READOUT_DEFAULT_MAX_BYTES,
                                                     assess_pregate_capture_resources)


MAX_STUDY_BYTES = 4 * 1024 * 1024


def _unique_keys(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON object key")
        value[key] = item
    return value


def _reject_constant(value):
    raise ValueError(f"non-finite JSON constant: {value}")


def read_study(path: str) -> dict:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("study input must be a regular file")
        if info.st_size > MAX_STUDY_BYTES:
            raise ValueError("study input exceeds byte limit")
        raw = os.read(fd, MAX_STUDY_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) > MAX_STUDY_BYTES:
        raise ValueError("study input exceeds byte limit")
    value = json.loads(raw, object_pairs_hook=_unique_keys, parse_constant=_reject_constant)
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
