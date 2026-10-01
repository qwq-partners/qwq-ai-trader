#!/usr/bin/env python3
"""Emit a bounded entry-window report from one explicit regular JSON input file."""
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

from src.analytics.entry_evaluation_bundle import load_json_bytes  # noqa: E402
from src.analytics.entry_window_evaluation import build_window_report  # noqa: E402

MAX_INPUT_BYTES = 256 * 1024 * 1024


def _read(path: Path, maximum: int):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise ValueError("input must be a regular file within the explicit byte limit")
        raw = handle.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError("input exceeds explicit byte limit")
    return load_json_bytes(raw)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--max-input-bytes", required=True, type=int,
                        help="positive aggregate JSON input limit, at most 256MiB")
    args = parser.parse_args(argv)
    try:
        if type(args.max_input_bytes) is not int or not 0 < args.max_input_bytes <= MAX_INPUT_BYTES:
            raise ValueError("max-input-bytes must be 1..268435456")
        payload = _read(args.input, args.max_input_bytes)
        if not isinstance(payload, dict) or set(payload) != {"protocol", "days", "as_of"}:
            raise ValueError("strict input fields required")
        result = build_window_report(payload["protocol"], payload["days"], as_of=payload["as_of"])
        output = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
    except (OSError, ValueError, UnicodeError, KeyError, TypeError, AttributeError, RecursionError) as exc:
        print(f"[고정 구간 평가] 입력 오류: {exc}", file=sys.stderr)
        return 2
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
