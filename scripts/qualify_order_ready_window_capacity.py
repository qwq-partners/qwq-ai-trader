#!/usr/bin/env python3
"""Offline synthetic capacity qualification for H1 order-ready observation.

It creates no market connection, order, account read, or profitability result.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import gc
import hashlib
import json
from pathlib import Path
import resource
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.analytics.entry_gate_trace import POLICY_REF, STAGES, VERSION as GATE_VERSION, scan_fields
from src.analytics.entry_price_shadow import KST
from src.analytics.entry_observation_journal import ObservationJournal, read_observation_journal
from src.analytics.entry_observation_runtime import CapturePlan, _WindowBuffer, validate_window_contract


STUDY_SHA256 = "0" * 64
UTC = timezone.utc
START = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)  # 09:00 KST Thursday
ADMISSION_END = START + timedelta(minutes=65)
END = START + timedelta(minutes=81)  # 10:21 KST, 4,860 seconds total
RSS_LIMIT_BYTES = int(1.25 * 1024 * 1024 * 1024)
MAX_CUSTOM_QUOTES = 400_001


@dataclass(frozen=True)
class Profile:
    scans: int = 14
    candidates_per_scan: int = 100
    buffer_capacity: int = 400_000
    queue_capacity: int = 8_192
    batch_size: int = 256
    max_bytes: int = 512 * 1024 * 1024
    max_record_bytes: int = 64 * 1024


TARGET_PROFILE = Profile()
CASE_QUOTES = {"smoke": 512, "base": 167_670, "stress": 335_340, "overflow": 400_001}
CHANNELS = {"registration_cap": 41, "external_reserved": 0, "operational_headroom": 10,
            "max_candidates": 5, "lease_seconds": 5100, "evidence_ref": "synthetic-h1-capacity"}
PACING = {"batch_records": 256, "sleep_seconds": 0.001, "queue_low_water_fraction": 0.5,
          "max_drain_seconds": 2.0, "meaning": "paced drain; not live callback latency"}


def _rss_bytes() -> int:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value * (1024 if sys.platform.startswith("linux") else 1))


def _symbol(index: int) -> str:
    return f"{index % 1_000_000:06d}"


def _at(index: int, total: int, *, end: datetime) -> datetime:
    seconds = max(0, int((end - START).total_seconds()) - 1)
    return START + timedelta(seconds=0 if total <= 1 else index * seconds // (total - 1))


def _quote_at(index: int, total: int) -> datetime:
    """Uniform half-open 4,860-second placement; full stress ends at 10:20:59."""
    return START + timedelta(seconds=0 if total <= 0 else index * 4860 // total)


def _rolling_60_peak(total: int) -> int:
    buckets = [0] * 4860
    for index in range(total):
        buckets[index * 4860 // total] += 1
    return max(sum(buckets[start:start + 60]) for start in range(4801)) if total else 0


def _study_context(profile: Profile, workspace: Path) -> dict:
    capture = {"version": "runner-order-ready-window-v1", "study_ref": "synthetic-h1-capacity",
        "start_at": START.isoformat(), "admission_end_at": ADMISSION_END.isoformat(), "end_at": END.isoformat(),
        "scan_admission_ref": "synthetic-h1-order-ready-window", "source_version_ref": "synthetic-source",
        "configuration_ref": "synthetic-config", "fee_evidence_ref": "synthetic-fees",
        "capital_evidence_ref": "synthetic-capital", "capacity_evidence_ref": "synthetic-capacity",
        "journal_path": str(workspace / "order-ready-window-capacity.jsonl"),
        "buffer_capacity": profile.buffer_capacity, "queue_capacity": profile.queue_capacity,
        "batch_size": profile.batch_size, "max_bytes": profile.max_bytes,
        "max_record_bytes": profile.max_record_bytes, "open_timeout_seconds": 10.0,
        "close_timeout_seconds": 10.0, "channels": dict(CHANNELS), "max_scans": profile.scans,
        "quote_admission": "order_ready", "selection_basis": {"version": "selection-basis-v2",
        "max_candidates": profile.candidates_per_scan, "max_source_terms": 16}, "entry_gate_trace": {"version": GATE_VERSION,
        "policy_ref": POLICY_REF, "max_candidates": profile.candidates_per_scan, "source_version_ref": "synthetic-source",
        "configuration_ref": "synthetic-config"}, "frame_diagnostics": {"version": "kis-frame-diagnostics-v1"}}
    return {"schema_version": 1, "dataset_kind": "synthetic", "evaluation_epoch": "synthetic-h1-capacity-v1",
        "as_of": END.isoformat(), "fee_model_ref": "synthetic-explicit-zero-fees",
        "fees": {"buy_commission_rate": "0", "sell_commission_rate": "0", "sell_tax_rate": "0"},
        "policy": {"max_quote_age_seconds": 5, "max_decision_delay_seconds": 5,
                   "entry_slippage_bps": "0", "exit_slippage_bps": "0"}, "data_basis": "received_snapshot",
        "received_policy": {"version": "received-v1", "max_rest_rtt_seconds": 3, "max_target_age_seconds": 30},
        "outcome_basis": "fixed_horizon_bid_markout", "markout_policy": {"version": "fixed-horizon-bid-v1",
        "policy_ref": "synthetic-h1", "fixed_at": "2026-09-30T00:00:00+00:00", "horizon_seconds": 900,
        "max_delay_seconds": 5}, "capital_policy": {"version": "pre-pending-guards-v1",
        "policy_ref": "synthetic-h1", "fixed_at": "2026-09-30T00:00:00+00:00",
        "source_version_ref": "synthetic-source", "configuration_ref": "synthetic-config",
        "max_snapshot_age_seconds": "1", "risk_per_trade_pct": "0.7", "max_position_pct": "18"},
        "opportunities": [], "capture": capture}


def _plan(profile: Profile, workspace: Path) -> CapturePlan:
    context = _study_context(profile, workspace)
    study = workspace / "study.json"
    study.write_text(json.dumps(context, sort_keys=True), encoding="utf-8")
    digest = hashlib.sha256(study.read_bytes()).hexdigest()
    manifest = workspace / "manifest.json"
    manifest.write_text(json.dumps({"study_path": str(study), "study_sha256": digest}), encoding="utf-8")
    return CapturePlan.load(manifest, now=START - timedelta(seconds=1))


def _scan(scan_id: str, scan_index: int, profile: Profile, at: datetime) -> dict:
    candidates = [{"symbol": _symbol(scan_index * profile.candidates_per_scan + index), "price": 10_000,
                   "score": 80.0, "screened_at": at.isoformat(), "change_pct": 1.0, "volume": 10_000,
                   "atr_pct": 2.0, "candidate_id": f"{scan_id}:{_symbol(scan_index * profile.candidates_per_scan + index)}"}
                  for index in range(profile.candidates_per_scan)]
    return {"kind": "scan", "scan_id": scan_id, "observed_at": at.isoformat(), "session": "regular",
            "route_origin": "live_screening", "population_scope": "window_returned_scan_candidates",
            "scan_admission_ref": "synthetic-h1-order-ready-window", "candidates": candidates,
            "selection_basis_expected": True, "selection_basis_max_candidates": profile.candidates_per_scan,
            "selection_basis_max_terms": 16, **scan_fields({"version": GATE_VERSION, "policy_ref": POLICY_REF,
            "max_candidates": profile.candidates_per_scan, "source_version_ref": "synthetic-source",
            "configuration_ref": "synthetic-config"})}


def _selection(candidate_id: str, symbol: str, at: datetime) -> dict:
    return {"kind": "selection_basis", "candidate_id": candidate_id, "symbol": symbol,
            "observed_at": at.isoformat(), "basis_status": "unavailable"}


def _gate(candidate_id: str, symbol: str, at: datetime) -> dict:
    return {"kind": "entry_gate_trace", "candidate_id": candidate_id, "symbol": symbol,
            "observed_at": at.isoformat(), "trace_version": GATE_VERSION, "policy_ref": POLICY_REF,
            "outcome": "unknown", "terminal_reason": "synthetic_unavailable", "signal_id": None,
            "steps": [{"stage": stage, "status": "unknown", "observed_at": at.isoformat(),
                       "reason": "synthetic_unavailable", "value": None, "threshold": None} for stage in STAGES]}


def _signal(candidate_id: str, symbol: str, at: datetime) -> dict:
    return {"kind": "signal", "candidate_id": candidate_id, "signal_id": f"signal-{candidate_id}",
            "observed_at": at.isoformat(), "event_timestamp": at.isoformat(), "strategy": "gap_and_go",
            "price": 10_000, "signal_target_price": 10_500, "signal_stop_price": 9_500}


def _ready(candidate_id: str, symbol: str, at: datetime) -> dict:
    signal_id = f"signal-{candidate_id}"
    return {"kind": "order_ready", "signal_id": signal_id, "symbol": symbol, "order_id": f"order-{signal_id}",
            "observed_at": at.isoformat(), "requested_quantity": 1, "strategy": "gap_and_go"}


def _quote(index: int, at: datetime) -> dict:
    return {"kind": "ws_quote", "quote_id": f"synthetic-quote-{index:012d}", "symbol": _symbol(index % 10),
            "observed_at": at.isoformat(), "ask": 10_001, "bid": 10_000, "ask_size": 100, "bid_size": 100,
            "provenance": {"tr_id": "H0STASP0", "exchange_time": at.astimezone(KST).strftime("%H%M%S"), "hour_class_code": "0",
            "received_at": at.isoformat(), "message_count": 1, "source_as_of": None,
            "connection_id": "synthetic-order-ready-window", "generation": 1}}


async def _pace(journal: ObservationJournal, profile: Profile) -> None:
    deadline = asyncio.get_running_loop().time() + PACING["max_drain_seconds"]
    low = int(profile.queue_capacity * PACING["queue_low_water_fraction"])
    while journal._queue.qsize() > low:
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError("synthetic writer did not drain within bounded pacing")
        await asyncio.sleep(PACING["sleep_seconds"])


async def _capture(case: str, workspace: Path, profile: Profile, quote_count: int) -> dict:
    if case not in CASE_QUOTES or type(quote_count) is not int or not 0 <= quote_count <= MAX_CUSTOM_QUOTES:
        raise ValueError("bounded synthetic case required")
    if not workspace.is_absolute() or not workspace.is_dir() or workspace.is_symlink():
        raise ValueError("existing absolute synthetic workspace required")
    plan = _plan(profile, workspace)  # invokes the actual CapturePlan.validate path
    clock = [START]
    buffer = _WindowBuffer(plan, lambda: clock[0])
    callbacks: list[dict] = []
    buffer.order_ready_quote_submit = callbacks.append
    started_wall, started_cpu = time.perf_counter(), time.process_time()
    journal = await ObservationJournal.open(buffer, plan.settings["journal_path"], study_ref="synthetic-h1-capacity",
        study_sha256=plan.study_sha256, queue_capacity=profile.queue_capacity, batch_size=profile.batch_size,
        max_bytes=profile.max_bytes, max_record_bytes=profile.max_record_bytes, open_timeout_seconds=10.0)
    accepted = {"metadata": 0, "controls": 0, "quotes": 0}
    quote_index = 0
    emitted = 0
    try:
        for scan_index in range(profile.scans):
            scan_at = _at(scan_index, profile.scans, end=ADMISSION_END)
            while quote_index < quote_count and _quote_at(quote_index, quote_count) < scan_at:
                clock[0] = _quote_at(quote_index, quote_count)
                accepted["quotes"] += int(buffer.publish(_quote(quote_index, clock[0])))
                quote_index += 1; emitted += 1
                if emitted % PACING["batch_records"] == 0:
                    await _pace(journal, profile)
            clock[0] = scan_at
            scan_id = buffer.begin_scan()
            if scan_id is None:
                raise RuntimeError(f"synthetic scan admission unexpectedly rejected: {scan_index}")
            accepted["metadata"] += int(buffer.publish(_scan(scan_id, scan_index, profile, clock[0]))); emitted += 1
            for candidate_index in range(profile.candidates_per_scan):
                symbol = _symbol(scan_index * profile.candidates_per_scan + candidate_index)
                candidate_id = f"{scan_id}:{symbol}"
                accepted["metadata"] += int(buffer.publish(_selection(candidate_id, symbol, clock[0]))); emitted += 1
                accepted["metadata"] += int(buffer.publish(_gate(candidate_id, symbol, clock[0]))); emitted += 1
                if scan_index == 0 and candidate_index < min(7, profile.candidates_per_scan):
                    accepted["controls"] += int(buffer.publish(_signal(candidate_id, symbol, clock[0]))); emitted += 1
                    before = len(buffer._records); buffer.publish(_ready(candidate_id, symbol, clock[0])); emitted += 1
                    accepted["controls"] += int(len(buffer._records) > before)
                if emitted % PACING["batch_records"] == 0:
                    await _pace(journal, profile)
        while quote_index < quote_count:
            clock[0] = _quote_at(quote_index, quote_count)
            accepted["quotes"] += int(buffer.publish(_quote(quote_index, clock[0])))
            quote_index += 1
            emitted += 1
            if emitted % PACING["batch_records"] == 0:
                await _pace(journal, profile)
        clock[0] = END
        close = await journal.close(timeout_seconds=10.0)
        status = buffer.capture_status()
        records = buffer._records
        peak = _rolling_60_peak(quote_count)
        if quote_count == CASE_QUOTES["stress"] and peak != 4140:
            raise RuntimeError("synthetic stress cadence no longer matches the stated 4,140/60s basis")
        return {"case": case, "profile": asdict(profile), "journal_path": plan.settings["journal_path"],
                "study_path": str(plan.study_path), "study_sha256": plan.study_sha256,
                "journal_bytes": Path(plan.settings["journal_path"]).stat().st_size, "capture": {"complete": status["complete"],
                "dropped_records": status["dropped_records"], "accepted_records": len(buffer._records), "journal": close},
                "accepted": accepted, "quotes": {"requested": quote_count, "accepted": accepted["quotes"],
                "rejected": quote_count - accepted["quotes"], "schema": "ws_quote/H0STASP0/synthetic-six-digit-symbol"},
                "admissions": {"attempted": min(7, profile.candidates_per_scan),
                "accepted": len(callbacks), "rejected": min(7, profile.candidates_per_scan) - len(callbacks),
                "callback_count": len(callbacks), "max_candidates": 5},
                "timestamps": {"first_quote_at": next((r["observed_at"] for r in records if r.get("kind") == "ws_quote"), None),
                "last_quote_at": next((r["observed_at"] for r in reversed(records) if r.get("kind") == "ws_quote"), None),
                "requested_last_quote_at": _quote_at(quote_count - 1, quote_count).isoformat() if quote_count else None,
                "rolling_60_quote_peak": peak, "rolling_60_scope": "requested synthetic schedule, including dropped quotes",
                "record_timestamps_monotonic": all(
                records[index]["observed_at"] <= records[index + 1]["observed_at"] for index in range(len(records)-1)),
                "scan_records": sum(r.get("kind") == "scan" for r in records)},
                "resource": {"capture_peak_rss_bytes": _rss_bytes(), "capture_wall_seconds": round(time.perf_counter()-started_wall, 6),
                "capture_cpu_seconds": round(time.process_time()-started_cpu, 6), "rss_scope": "capture process lifetime"}}
    finally:
        del journal, buffer
        gc.collect()


def _readback(capture: dict) -> dict:
    started_wall, started_cpu = time.perf_counter(), time.process_time()
    study = Path(capture["study_path"])
    context = json.loads(study.read_text(encoding="utf-8"))
    data = read_observation_journal(capture["journal_path"], max_bytes=context["capture"]["max_bytes"],
        expected_study_sha256=capture["study_sha256"])
    records_contract_validated = data["complete"]
    validate_window_contract(context, data if records_contract_validated else None)
    return {"complete": data["complete"], "sealed": data["journal"]["sealed"],
            "record_count": len(data["records"]), "dropped_records": data["dropped_records"],
            "persistence_dropped_records": data["journal"]["persistence_dropped_records"],
            "strict_reader": True, "reader_peak_rss_bytes": _rss_bytes(),
            "records_contract_validated": records_contract_validated,
            "reader_wall_seconds": round(time.perf_counter()-started_wall, 6),
            "reader_cpu_seconds": round(time.process_time()-started_cpu, 6)}


def _finish(capture: dict, readback: dict) -> dict:
    p = Profile(**capture["profile"])
    metadata = p.scans * (1 + 2 * p.candidates_per_scan)
    controls = min(7, p.candidates_per_scan) * 3
    attempted = metadata + controls + capture["quotes"]["requested"]
    accepted = min(p.buffer_capacity, attempted)
    dropped = attempted - accepted
    journal = capture["capture"]["journal"]
    ok = (capture["capture"]["accepted_records"] == accepted and capture["capture"]["dropped_records"] == dropped
          and readback["record_count"] == accepted and readback["dropped_records"] == dropped
          and readback["persistence_dropped_records"] == 0
          and capture["capture"]["complete"] is (dropped == 0) and readback["complete"] is (dropped == 0)
          and journal["sealed"] and journal["fsync_confirmed"] and journal["error"] is None
          and journal["persisted_records"] == accepted and readback["sealed"]
          and capture["timestamps"]["record_timestamps_monotonic"])
    capture_summary = {**capture["capture"], "journal": {k: v for k, v in journal.items() if k != "path"}}
    return {"schema_version": 1, "qualifier": "order-ready-window-capacity-v1", "synthetic": True,
            "profitability": False, "production_eligible": False, "case": capture["case"],
            "target_profile": capture["profile"], "capture_contract": {"version": "runner-order-ready-window-v1",
            "quote_admission": "order_ready", "channels": {k: CHANNELS[k] for k in CHANNELS if k != "evidence_ref"},
            "window": {"start_at": START.isoformat(), "admission_end_at": ADMISSION_END.isoformat(), "end_at": END.isoformat(),
            "seconds": 4860}}, "counts": {"metadata_attempted": metadata, "control_attempted": controls,
            "attempted_records": attempted, "expected_accepted_records": accepted, "expected_dropped_records": dropped},
            "pacing": dict(PACING), "quotes": capture["quotes"], "admissions": capture["admissions"],
            "timestamps": capture["timestamps"],
            "channel_allocation_exercised": False,
            "capture": capture_summary, "readback": readback, "resource": capture["resource"],
            "disk": {"journal_bytes": capture["journal_bytes"], "journal_max_bytes": p.max_bytes,
                     "remaining_bytes": p.max_bytes - capture["journal_bytes"]},
            "result": {"expectations_met": ok, "overflow_fail_closed": dropped > 0 and not capture["capture"]["complete"],
            "limitations": ["synthetic records only; no feed, broker, account, or network input",
            "the callback count proves buffer admission only; it does not claim channel registration or lease success",
            "generated synthetic shapes are bounded by actual rows, not a guarantee that every production row is max_record_bytes",
            "paced drain is explicit and is not a live callback latency or throughput claim",
            "historical basis: 1,035 quotes/60 seconds on five channels; ten channels scaled 2x for 4,860 seconds = 335,340 quotes; this is an assumption, not a future guarantee"]}}


def run_case(case: str, *, workspace: Path, profile: Profile = TARGET_PROFILE, quote_count: int | None = None) -> dict:
    count = CASE_QUOTES[case] if quote_count is None else quote_count
    capture = asyncio.run(_capture(case, Path(workspace), profile, count))
    return _finish(capture, _readback(capture))


def _set_limit() -> int:
    hard = resource.getrlimit(resource.RLIMIT_AS)[1]
    limit = RSS_LIMIT_BYTES if hard in (resource.RLIM_INFINITY, -1) else min(RSS_LIMIT_BYTES, hard)
    resource.setrlimit(resource.RLIMIT_AS, (limit, hard))
    return limit


def _child(command: list[str]) -> dict:
    done = subprocess.run(command, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, timeout=600, check=False)
    if done.returncode: raise RuntimeError("synthetic capacity child failed")
    return json.loads(done.stdout)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=tuple(CASE_QUOTES), default="smoke")
    parser.add_argument("--quotes", type=int)
    parser.add_argument("--_child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--_child-reader", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--workspace", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args._child:
            if args.workspace is None: raise ValueError("child workspace required")
            limit = _set_limit()
            profile = TARGET_PROFILE
            value = asyncio.run(_capture(args.case, args.workspace, profile, args.quotes))
            value["resource"]["address_space_limit_bytes"] = limit
            print(json.dumps(value, sort_keys=True, allow_nan=False))
            return 0
        if args._child_reader:
            if args.workspace is None: raise ValueError("reader workspace required")
            limit = _set_limit()
            capture = {"study_path": str(args.workspace / "study.json"),
                       "study_sha256": hashlib.sha256((args.workspace / "study.json").read_bytes()).hexdigest(),
                       "journal_path": str(args.workspace / "order-ready-window-capacity.jsonl")}
            value = _readback(capture)
            value["address_space_limit_bytes"] = limit
            print(json.dumps(value, sort_keys=True, allow_nan=False))
            return 0
        count = CASE_QUOTES[args.case] if args.quotes is None else args.quotes
        if not 0 <= count <= MAX_CUSTOM_QUOTES: raise ValueError("custom quote count outside bounded range")
        with tempfile.TemporaryDirectory(prefix="qwq-synthetic-order-ready-window-", dir=None) as name:
            capture = _child([sys.executable, str(Path(__file__).resolve()), "--_child", "--case", args.case,
                             "--quotes", str(count), "--workspace", name])
            readback = _child([sys.executable, str(Path(__file__).resolve()), "--_child-reader",
                "--workspace", name])
            result = _finish(capture, readback)
        print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
        return 0 if result["result"]["expectations_met"] else 1
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"[synthetic order-ready capacity] {type(exc).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
