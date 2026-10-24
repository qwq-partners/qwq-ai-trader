#!/usr/bin/env python3
"""Offline-only denominator report for a Toss observation ledger.

It imports neither runtime, authentication nor HTTP modules and never repairs
or appends to its input file.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import sys
from datetime import datetime, timedelta


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.data.providers.toss.observation_ledger import ObservationLedger
from src.data.providers.toss.report_protocol import ReportCohort, strict_loads


def _read_private_json(path, *, maximum=65536):
    """Read a bounded regular file through stable, non-symlink descriptors."""
    value = Path(path)
    if not value.is_absolute():
        raise ValueError("absolute_path_required")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    dirs, fd = [], os.open("/", flags)
    try:
        dirs.append(fd)
        for name in value.parts[1:-1]:
            child = os.open(name, flags, dir_fd=fd)
            dirs.append(child)
            info = os.fstat(child)
            linked = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if (not stat.S_ISDIR(linked.st_mode) or (info.st_dev, info.st_ino) != (linked.st_dev, linked.st_ino)
                    or (info.st_mode & 0o022 and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX))):
                raise ValueError("unsafe_parent")
            fd = child
        file_fd = os.open(value.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=fd)
        try:
            before = os.fstat(file_fd)
            linked = os.stat(value.name, dir_fd=fd, follow_symlinks=False)
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                    or (before.st_dev, before.st_ino) != (linked.st_dev, linked.st_ino)
                    or before.st_size > maximum):
                raise ValueError("unsafe_file")
            raw = b""
            while len(raw) <= maximum:
                chunk = os.read(file_fd, min(65536, maximum + 1 - len(raw)))
                if not chunk:
                    break
                raw += chunk
            after = os.fstat(file_fd)
            stable = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
            if len(raw) != before.st_size or stable(before) != stable(after) or len(raw) > maximum:
                raise ValueError("unstable_file")
            return strict_loads(raw, maximum=maximum)
        finally:
            os.close(file_fd)
    finally:
        for directory in reversed(dirs):
            os.close(directory)


def _report_input(path, slot_id):
    document = _read_private_json(path)
    if (set(document) not in ({"date", "stocks"}, {"date", "stocks", "report_cohort"})
            or type(document["date"]) is not str or type(document["stocks"]) is not list):
        raise ValueError("morning_document")
    slot = datetime.fromisoformat(slot_id)
    if (slot.tzinfo is None or slot.utcoffset() != timedelta(hours=9) or slot.isoformat() != slot_id
            or (slot.hour, slot.minute, slot.second, slot.microsecond) != (16, 0, 0, 0)
            or document["date"] != slot.date().isoformat()):
        raise ValueError("report_slot")
    if not document["stocks"] and document.get("report_cohort") is None:
        return None
    if set(document) != {"date", "stocks", "report_cohort"}:
        raise ValueError("missing_cohort")
    cohort = ReportCohort.from_document(document["report_cohort"])
    stocks = []
    for stock in document["stocks"]:
        if type(stock) is not dict or type(stock.get("rank")) is not int or type(stock.get("symbol")) is not str:
            raise ValueError("stocks")
        stocks.append((stock["rank"], stock["symbol"]))
    if (cohort.report_date != slot.date() or cohort.members != tuple(stocks)):
        raise ValueError("cohort_members")
    return cohort


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Read a Toss observation ledger without side effects")
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--plan-hash", required=True)
    parser.add_argument("--max-bytes", type=int, required=True)
    parser.add_argument("--report-cohort")
    parser.add_argument("--slot-id")
    args = parser.parse_args(argv)
    try:
        if bool(args.report_cohort) != bool(args.slot_id):
            raise ValueError("report_arguments")
        if args.report_cohort:
            cohort = _report_input(args.report_cohort, args.slot_id)
            if cohort is None:
                summary = {"slot_id": args.slot_id, "cohort_id": None, "members": [], "member_count": 0,
                           "terminal_attempts": 0, "successful_attempts": 0, "incomplete": False,
                           "error_code": "no_candidates", "production_eligible": False}
            else:
                summary = ObservationLedger.read_only_report_slot(
                    args.ledger, plan_hash=args.plan_hash, max_bytes=args.max_bytes,
                    cohort=cohort, slot_id=args.slot_id)
        else:
            summary = ObservationLedger.read_only_summary(args.ledger, plan_hash=args.plan_hash, max_bytes=args.max_bytes)
    except (ValueError, TypeError, OSError, OverflowError):
        summary = {"incomplete": True, "error_code": "invalid_report_input", "production_eligible": False}
    summary["production_eligible"] = False
    print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
    return 0 if not summary["incomplete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
