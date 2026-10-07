#!/usr/bin/env python3
"""Offline-only capacity qualification for the signal-window observation path.

This creates synthetic records only.  It never opens a feed, replays an
archive, reads a live symbol, or calculates profitability.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import gc
import json
from pathlib import Path
import resource
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.analytics.entry_gate_trace import POLICY_REF, STAGES, VERSION as GATE_TRACE_VERSION
from src.analytics.entry_observation import EntryObservationBuffer
from src.analytics.entry_observation_journal import ObservationJournal, read_observation_journal


STUDY_SHA256 = "0" * 64
# The logical 10:00–11:10 KST window is represented as UTC timestamps, just
# like received_at in the live websocket path.
KST = timezone(timedelta(hours=9))
START = datetime(2026, 10, 8, 1, 0, tzinfo=timezone.utc)
WINDOW_SECONDS = 70 * 60
RSS_LIMIT_BYTES = int(1.25 * 1024 * 1024 * 1024)
MAX_CUSTOM_QUOTES = 300_000


@dataclass(frozen=True)
class Profile:
    scans: int = 12
    candidates_per_scan: int = 100
    buffer_capacity: int = 300_000
    queue_capacity: int = 8_192
    batch_size: int = 256
    max_bytes: int = 402_653_184
    max_record_bytes: int = 65_536


TARGET_PROFILE = Profile()
CASE_QUOTES = {"smoke": 512, "base": 116_000, "stress": 232_000,
               "overflow": TARGET_PROFILE.buffer_capacity + 1}
PACING = {"batch_records": 256, "sleep_seconds": 0.002,
          "queue_low_water_fraction": 0.5, "max_drain_seconds": 2.0}


def _rss_bytes() -> int:
    # Linux reports ru_maxrss in KiB.  The target runs on Linux; retaining the
    # platform branch keeps the report correct on the BSD/macOS unit-test path.
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value * (1024 if sys.platform.startswith("linux") else 1))


def _logical_time(index: int, total: int) -> datetime:
    offset = 0 if total <= 1 else int(index * (WINDOW_SECONDS - 1) / (total - 1))
    return START + timedelta(seconds=offset)


def _symbol(index: int) -> str:
    return f"{index % 1_000_000:06d}"


def _settings(profile: Profile) -> tuple[dict, dict]:
    selection = {"version": "selection-basis-v2", "max_candidates": profile.candidates_per_scan,
                 "max_source_terms": 16}
    gate = {"version": GATE_TRACE_VERSION, "policy_ref": POLICY_REF,
            "max_candidates": profile.candidates_per_scan,
            "source_version_ref": "synthetic-signal-window-capacity-v1",
            "configuration_ref": "synthetic-signal-window-capacity-v1"}
    return selection, gate


def _scan_record(scan_id: str, scan_index: int, profile: Profile, at: datetime) -> dict:
    candidates = []
    for candidate_index in range(profile.candidates_per_scan):
        symbol = _symbol(scan_index * profile.candidates_per_scan + candidate_index)
        candidates.append({"symbol": symbol, "price": 10_000 + candidate_index, "score": 80.0,
                           "screened_at": at.isoformat(), "change_pct": 1.0, "volume": 10_000,
                           "atr_pct": 2.0, "candidate_id": f"{scan_id}:{symbol}"})
    return {"kind": "scan", "scan_id": scan_id, "observed_at": at.isoformat(),
            "session": "regular", "route_origin": "live_screening",
            "population_scope": "window_returned_scan_candidates",
            "scan_admission_ref": "synthetic-signal-window-capacity-v1", "candidates": candidates,
            "selection_basis_expected": True,
            "selection_basis_max_candidates": profile.candidates_per_scan,
            "selection_basis_max_terms": 16, "entry_gate_trace_expected": True,
            "entry_gate_trace_version": GATE_TRACE_VERSION, "entry_gate_policy_ref": POLICY_REF,
            "entry_gate_source_version_ref": "synthetic-signal-window-capacity-v1",
            "entry_gate_configuration_ref": "synthetic-signal-window-capacity-v1"}


def _selection_record(candidate_id: str, symbol: str, at: datetime) -> dict:
    return {"kind": "selection_basis", "candidate_id": candidate_id, "symbol": symbol,
            "observed_at": at.isoformat(), "basis_status": "unavailable"}


def _gate_record(candidate_id: str, symbol: str, at: datetime) -> dict:
    return {"kind": "entry_gate_trace", "candidate_id": candidate_id, "symbol": symbol,
            "observed_at": at.isoformat(), "trace_version": GATE_TRACE_VERSION,
            "policy_ref": POLICY_REF, "outcome": "unknown", "terminal_reason": "scope_finished",
            "signal_id": None,
            "steps": [{"stage": stage, "status": "unknown", "observed_at": at.isoformat(),
                       "reason": "synthetic_unavailable", "value": None, "threshold": None}
                      for stage in STAGES]}


def _quote_record(index: int, total: int) -> dict:
    at = _logical_time(index, total)
    symbol = _symbol(index)
    return {"kind": "ws_quote", "quote_id": f"synthetic-quote-{index:012d}", "symbol": symbol,
            "observed_at": at.isoformat(), "ask": 10_001, "bid": 10_000, "ask_size": 100,
            "bid_size": 100, "provenance": {"tr_id": "H0STASP0",
            "exchange_time": at.astimezone(KST).strftime("%H%M%S"), "hour_class_code": "0",
            "received_at": at.isoformat(), "message_count": 1, "source_as_of": None,
            "connection_id": "synthetic-window-capacity",
            "generation": 1}}


def _metadata_count(profile: Profile) -> int:
    return profile.scans * (1 + 2 * profile.candidates_per_scan)


def _storage_contract(profile: Profile, quote_count: int) -> dict:
    """Expose the distinction between this synthetic row size and the contract maximum."""
    metadata_conservative = _metadata_count(profile) * (profile.max_record_bytes + 2048) + 2048
    quote = _quote_record(0, max(quote_count, 1))
    quote_wire_bytes = len(json.dumps({"row": 1, "previous_hash": "0" * 64, "kind": "record",
        "payload": quote, "hash": "0" * 64}, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode("utf-8")) + 1
    quoted_rows = quote_wire_bytes * quote_count
    all_record_contract_max = (_metadata_count(profile) + quote_count) * (
        profile.max_record_bytes + 2048) + 2048
    return {"metadata_model": "selection-basis-v2 unavailable rows plus full synthetic gate steps",
            "metadata_contract_reserve_bytes": metadata_conservative,
            "quote_wire_bytes_per_row": quote_wire_bytes,
            "quoted_synthetic_bytes": quoted_rows,
            "combined_conservative_bytes": metadata_conservative + quoted_rows,
            "journal_max_bytes": profile.max_bytes,
            "metadata_reserve_plus_synthetic_quotes_fit": (
                metadata_conservative + quoted_rows <= profile.max_bytes),
            "all_record_contract_max_bytes": all_record_contract_max,
            "all_maximum_sized_records_fit": all_record_contract_max <= profile.max_bytes,
            "bounded_failure_required_if_exceeded": True}


async def _pace_writer(journal: ObservationJournal, profile: Profile) -> None:
    """Give the real writer a bounded chance to drain its finite queue.

    This is a qualification harness control, not a claim that a live callback
    has this delay budget or that the observed throughput predicts production.
    """
    low_water = int(profile.queue_capacity * PACING["queue_low_water_fraction"])
    deadline = asyncio.get_running_loop().time() + PACING["max_drain_seconds"]
    while journal._queue.qsize() > low_water:
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError("synthetic writer did not drain within bounded pacing")
        await asyncio.sleep(PACING["sleep_seconds"])
    await asyncio.sleep(PACING["sleep_seconds"])


async def _capture(case: str, *, workspace: Path, profile: Profile, quote_count: int) -> dict:
    if case not in CASE_QUOTES or type(quote_count) is not int or quote_count < 0:
        raise ValueError("bounded synthetic case required")
    if not workspace.is_absolute() or not workspace.is_dir() or workspace.is_symlink():
        raise ValueError("existing absolute synthetic workspace required")
    selection, gate = _settings(profile)
    buffer = EntryObservationBuffer(evaluation_epoch="synthetic-signal-window-capacity-v1",
                                    capacity=profile.buffer_capacity, scan_scope="window",
                                    max_scans=profile.scans,
                                    scan_admission_ref="synthetic-signal-window-capacity-v1",
                                    selection_basis_settings=selection,
                                    entry_gate_trace_settings=gate)
    journal_path = workspace / f"signal-window-capacity-{uuid4().hex}.jsonl"
    started_wall, started_cpu = time.perf_counter(), time.process_time()
    journal = await ObservationJournal.open(buffer, journal_path,
        study_ref="synthetic-signal-window-capacity-v1", study_sha256=STUDY_SHA256,
        queue_capacity=profile.queue_capacity, batch_size=profile.batch_size,
        max_bytes=profile.max_bytes, max_record_bytes=profile.max_record_bytes,
        open_timeout_seconds=10.0)
    metadata_accepted = 0
    quote_accepted = 0
    try:
        for scan_index in range(profile.scans):
            at = _logical_time(scan_index, profile.scans)
            scan_id = buffer.begin_scan()
            if scan_id is None:
                raise RuntimeError("synthetic scan admission unexpectedly rejected")
            scan = _scan_record(scan_id, scan_index, profile, at)
            metadata_accepted += int(buffer.publish(scan))
            for candidate in scan["candidates"]:
                metadata_accepted += int(buffer.publish(_selection_record(
                    candidate["candidate_id"], candidate["symbol"], at)))
                metadata_accepted += int(buffer.publish(_gate_record(
                    candidate["candidate_id"], candidate["symbol"], at)))
            await _pace_writer(journal, profile)
        for index in range(quote_count):
            quote_accepted += int(buffer.publish(_quote_record(index, quote_count)))
            if (index + 1) % PACING["batch_records"] == 0:
                await _pace_writer(journal, profile)
        close = await journal.close(timeout_seconds=120.0)
        status = buffer.capture_status()
        size = journal_path.stat().st_size if journal_path.exists() else None
        return {"case": case, "profile": asdict(profile), "pacing": dict(PACING),
                "journal_path": str(journal_path), "journal_bytes": size,
                "capture": {"complete": status["complete"],
                            "dropped_records": status["dropped_records"],
                            "accepted_records": len(buffer._records),
                            "metadata_accepted": metadata_accepted,
                            "journal": close},
                "quotes": {"requested": quote_count, "accepted": quote_accepted,
                           "rejected": quote_count - quote_accepted,
                           "schema": "ws_quote/H0STASP0/synthetic-six-digit-symbol"},
                "resource": {"capture_peak_rss_bytes": _rss_bytes(),
                             "capture_wall_seconds": round(time.perf_counter() - started_wall, 6),
                             "capture_cpu_seconds": round(time.process_time() - started_cpu, 6),
                             "rss_scope": "capture process lifetime"}}
    finally:
        # The capture process ends before an offline reader is launched by the
        # public CLI, so it never holds a full buffer while that reader builds
        # its strict result list.
        del journal
        del buffer
        gc.collect()


def _read(journal_path: Path, *, max_bytes: int) -> dict:
    started_wall, started_cpu = time.perf_counter(), time.process_time()
    observations = read_observation_journal(journal_path, max_bytes=max_bytes,
                                            expected_study_sha256=STUDY_SHA256)
    record_count = len(observations["records"])
    return {"complete": observations["complete"], "sealed": observations["journal"]["sealed"],
            "record_count": record_count, "dropped_records": observations["dropped_records"],
            "persistence_dropped_records": observations["journal"]["persistence_dropped_records"],
            "strict_reader": True, "reader_peak_rss_bytes": _rss_bytes(),
            "reader_wall_seconds": round(time.perf_counter() - started_wall, 6),
            "reader_cpu_seconds": round(time.process_time() - started_cpu, 6),
            "rss_scope": "reader process lifetime"}


def _finish(capture: dict, readback: dict) -> dict:
    profile = Profile(**capture["profile"])
    attempted = _metadata_count(profile) + capture["quotes"]["requested"]
    expected_accepted = min(profile.buffer_capacity, attempted)
    expected_dropped = attempted - expected_accepted
    expected_complete = expected_dropped == 0
    storage_contract = _storage_contract(profile, capture["quotes"]["requested"])
    journal = capture["capture"]["journal"]
    capture_summary = {**capture["capture"], "journal_bytes": capture["journal_bytes"],
                       "journal": {key: value for key, value in journal.items() if key != "path"}}
    expectations_met = (
        capture["capture"]["accepted_records"] == expected_accepted
        and capture["capture"]["dropped_records"] == expected_dropped
        and capture["quotes"]["accepted"] == max(0, expected_accepted - _metadata_count(profile))
        and capture["capture"]["complete"] is expected_complete
        and journal["sealed"] is True and journal["fsync_confirmed"] is True and journal["error"] is None
        and readback["sealed"] is True and readback["record_count"] == expected_accepted
        and readback["dropped_records"] == expected_dropped
        and readback["complete"] is expected_complete
    )
    return {"schema_version": 1, "qualifier": "signal-window-capacity-v1", "synthetic": True,
            "profitability": False, "production_eligible": False, "case": capture["case"],
            "target_profile": capture["profile"], "counts": {"metadata_attempted": _metadata_count(profile),
            "attempted_records": attempted, "expected_accepted_records": expected_accepted,
            "expected_dropped_records": expected_dropped}, "pacing": capture["pacing"],
            "quotes": capture["quotes"], "capture": capture_summary, "readback": readback,
            "resource": capture["resource"], "storage_contract": storage_contract,
            "result": {"expectations_met": expectations_met,
            "overflow_fail_closed": expected_dropped > 0 and not capture["capture"]["complete"],
            "limitations": [
                "synthetic records; no archive replay, live symbols, or network input",
                "pacing lets the writer drain and is not evidence of live callback latency",
                "the reader runs after capture in a separate process, so its RSS is reported separately",
                "selection-basis-v2 rows are synthetic unavailable evidence; this is not a claim that every "
                "deployed selection row has maximum 16-term payload size",
                "the storage contract is checked separately: if conservative metadata reserve plus quoted "
                "synthetic rows exceeds max_bytes, the journal must fail closed rather than claim capacity",
                "observed Oct 7 was 20,338 quotes and rolling-60-second peak 1,035 across five channels; "
                "2x scaling to eight channels is 55.2/s * 4,200s = 231,840, not a future guarantee",
            ]}}


def run_case(case: str, *, workspace: Path, profile: Profile = TARGET_PROFILE,
             quote_count: int | None = None) -> dict:
    """Run one local synthetic case for tests or a deliberately supplied workspace.

    The public CLI puts capture and reading in fresh limited subprocesses.  This
    helper uses the same real buffer/journal path but is intentionally for small
    test profiles, where a single-process RSS reading would not be meaningful.
    """
    if case not in CASE_QUOTES:
        raise ValueError("unsupported synthetic case")
    count = CASE_QUOTES[case] if quote_count is None else quote_count
    capture = asyncio.run(_capture(case, workspace=Path(workspace), profile=profile, quote_count=count))
    readback = _read(Path(capture["journal_path"]), max_bytes=profile.max_bytes)
    return _finish(capture, readback)


def _set_resource_limit() -> int:
    hard = resource.getrlimit(resource.RLIMIT_AS)[1]
    limit = RSS_LIMIT_BYTES if hard in (resource.RLIM_INFINITY, -1) else min(RSS_LIMIT_BYTES, hard)
    resource.setrlimit(resource.RLIMIT_AS, (limit, hard))
    return limit


def _child_capture(case: str, workspace: Path, quote_count: int) -> int:
    limit = _set_resource_limit()
    result = asyncio.run(_capture(case, workspace=workspace, profile=TARGET_PROFILE, quote_count=quote_count))
    result["resource"]["address_space_limit_bytes"] = limit
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))
    return 0


def _child_read(workspace: Path) -> int:
    limit = _set_resource_limit()
    journals = list(workspace.glob("signal-window-capacity-*.jsonl"))
    if len(journals) != 1:
        raise ValueError("exactly one synthetic journal required")
    result = _read(journals[0], max_bytes=TARGET_PROFILE.max_bytes)
    result["address_space_limit_bytes"] = limit
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))
    return 0


def _child(command: list[str], *, workspace: Path) -> dict:
    completed = subprocess.run(command, cwd=ROOT, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                               timeout=600, check=False)
    if completed.returncode != 0:
        raise RuntimeError("synthetic capacity child failed")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("synthetic capacity child produced invalid JSON") from exc


def _run_public(case: str, *, quote_count: int, output_directory: Path | None) -> dict:
    if output_directory is not None:
        output_directory = Path(output_directory)
        if (not output_directory.is_absolute() or not output_directory.is_dir()
                or output_directory.is_symlink()):
            raise ValueError("output directory must be an existing absolute non-symlink directory")
    with tempfile.TemporaryDirectory(prefix="qwq-synthetic-signal-window-capacity-",
                                     dir=output_directory) as temporary:
        workspace = Path(temporary)
        capture = _child([sys.executable, str(Path(__file__).resolve()), "--_child-capture", case,
                          "--workspace", str(workspace), "--quotes", str(quote_count)], workspace=workspace)
        readback = _child([sys.executable, str(Path(__file__).resolve()), "--_child-read",
                           "--workspace", str(workspace)], workspace=workspace)
        return _finish(capture, readback)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=tuple(CASE_QUOTES), default="smoke")
    parser.add_argument("--quotes", type=int, help=f"synthetic quote count (0..{MAX_CUSTOM_QUOTES})")
    parser.add_argument("--output-directory", type=Path,
                        help="existing absolute directory for an automatically removed synthetic workspace")
    parser.add_argument("--_child-capture", choices=tuple(CASE_QUOTES), dest="child_capture",
                        help=argparse.SUPPRESS)
    parser.add_argument("--_child-read", action="store_true", dest="child_read", help=argparse.SUPPRESS)
    parser.add_argument("--workspace", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.child_capture:
            if args.workspace is None or args.quotes is None:
                raise ValueError("child capture arguments required")
            return _child_capture(args.child_capture, args.workspace, args.quotes)
        if args.child_read:
            if args.workspace is None:
                raise ValueError("child reader workspace required")
            return _child_read(args.workspace)
        if args.quotes is not None and not 0 <= args.quotes <= MAX_CUSTOM_QUOTES:
            raise ValueError("custom quote count outside bounded range")
        quotes = CASE_QUOTES[args.case] if args.quotes is None else args.quotes
        result = _run_public(args.case, quote_count=quotes, output_directory=args.output_directory)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False))
        return 0 if result["result"]["expectations_met"] else 1
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"[synthetic signal-window capacity] {type(exc).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
