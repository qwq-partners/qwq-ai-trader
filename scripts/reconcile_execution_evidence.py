#!/usr/bin/env python3
"""Read explicitly supplied offline evidence and print a non-authorizing report."""

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import stat
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.execution.execution_ledger import ExecutionLedgerError, read_execution_ledger
from src.execution.recovery_evidence import reconcile_execution_evidence

MAX_JSON_BYTES = 16 * 1024 * 1024


def _pairs(items):
    value = {}
    for key, item in items:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _constant(_value):
    raise ValueError("nonstandard JSON number")


def _read_json(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode) or not 1 <= before.st_size <= MAX_JSON_BYTES:
            raise ValueError("unsupported JSON file")
        raw = handle.read(MAX_JSON_BYTES + 1)
        after = os.fstat(handle.fileno())
        if len(raw) > MAX_JSON_BYTES or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError("input changed during read")
    result = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant)
    if not isinstance(result, dict):
        raise ValueError("object envelope required")
    return result, sha256(raw).hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("ledger", "broker", "journal", "baseline"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--account-scope", required=True)
    args = parser.parse_args(argv)
    try:
        ledger = read_execution_ledger(args.ledger, args.account_scope)
        files = {name: _read_json(getattr(args, name)) for name in ("broker", "journal", "baseline")}
        report = reconcile_execution_evidence(ledger, *(files[name][0] for name in ("broker", "journal", "baseline")))
        for name, (_, fingerprint) in files.items():
            report["inputs"][name]["file_sha256"] = fingerprint
        print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (ExecutionLedgerError, OSError, ValueError, TypeError, RecursionError):
        # Never echo input payloads, file paths or account identifiers in errors.
        print("증거 파일 검증 실패: 독립 복사본·계좌 범위·입력 형식을 확인하세요.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
