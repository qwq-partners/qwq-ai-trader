#!/usr/bin/env python3
"""명시된 유한 계좌 JSON의 순손익을 읽기 전용으로 계산한다."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
from decimal import DecimalException

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.analytics.account_net_return import build_account_report
from src.analytics.entry_evaluation_bundle import load_json_bytes

MAX_INPUT_BYTES = 16 * 1024 * 1024


def _read(path, maximum):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
            raise ValueError('읽기 한도/파일 형식 위반')
        raw = handle.read(maximum + 1)
        after = os.fstat(handle.fileno())
    if (len(raw) > maximum or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
        raise ValueError('파일 변경/한도 초과')
    return raw


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, type=Path)
    parser.add_argument('--max-input-bytes', required=True, type=int)
    args = parser.parse_args(argv)
    try:
        if not 1 <= args.max_input_bytes <= MAX_INPUT_BYTES:
            raise ValueError('읽기 상한 위반')
        raw = _read(args.input, args.max_input_bytes)
        out = build_account_report(load_json_bytes(raw))
        out['input_sha256'] = hashlib.sha256(raw).hexdigest()
        output = json.dumps(out, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
    except (OSError, ValueError, UnicodeError, KeyError, TypeError, AttributeError, RecursionError, DecimalException):
        print('[계좌 순손익] 입력 계약 오류', file=sys.stderr)
        return 2
    print(output)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
