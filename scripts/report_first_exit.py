#!/usr/bin/env python3
"""Compare the first ExitManager decision on an explicit offline PRICE path."""
import argparse
import json
import os
from pathlib import Path
import stat
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.analytics.first_exit_replay import MAX_INPUT_BYTES, replay_first_exit  # noqa: E402


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("nonfinite JSON number")


def _read_payload(path):
    # Nonblocking open prevents FIFOs from hanging before the regular-file check.
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("input must be a regular file, not a symlink")
        if info.st_size > MAX_INPUT_BYTES:
            raise ValueError("input exceeds byte limit")
        raw = stream.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError("input exceeds byte limit")
    payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                         parse_constant=_reject_constant)
    if not isinstance(payload, dict):
        raise ValueError("input must be a JSON object")
    return payload


def main(argv=None):
    parser = argparse.ArgumentParser(description="동일 진입·장중 PRICE 경로의 첫 청산 판단 비교. 체결·순수익은 계산하지 않습니다.")
    parser.add_argument("--input", required=True, help="명시적 first_exit_replay_v1 JSON")
    parser.add_argument("--output", required=True, help="새 JSON 파일 (기존 파일 덮어쓰기 금지)")
    args = parser.parse_args(argv)
    try:
        if os.path.lexists(args.output):
            raise ValueError("output already exists")
        payload = _read_payload(args.input)
        report = replay_first_exit(payload)
        encoded = (json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode("utf-8")
        # O_EXCL also protects against creation between the initial check and write.
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    except (OSError, ValueError, TypeError, RecursionError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps({"status": "written", "fill_status": "not_simulated", "net_pnl": None}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
