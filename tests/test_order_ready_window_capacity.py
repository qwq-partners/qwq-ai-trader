"""Offline qualification of the order-ready-window capture path."""
from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from datetime import timedelta


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "qualify_order_ready_window_capacity.py"


def _qualifier():
    spec = importlib.util.spec_from_file_location("order_ready_window_capacity", CLI)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


def _small_profile(qualifier, *, capacity=16):
    """This is a test shape, deliberately distinct from the fixed profile."""
    return qualifier.Profile(
        scans=1,
        candidates_per_scan=1,
        buffer_capacity=capacity,
        queue_capacity=64,
        batch_size=8,
        max_bytes=2 * 1024 * 1024,
        max_record_bytes=64 * 1024,
    )


def test_small_smoke_strictly_reads_a_sealed_order_ready_window(tmp_path):
    """Breaks if the real buffer/journal path stops sealing or strict-reading."""
    qualifier = _qualifier()

    result = qualifier.run_case(
        "smoke", workspace=tmp_path, profile=_small_profile(qualifier), quote_count=4
    )

    assert result["synthetic"] is True
    assert result["profitability"] is False
    assert result["production_eligible"] is False
    assert result["capture_contract"]["version"] == "runner-order-ready-window-v1"
    assert result["capture_contract"]["quote_admission"] == "order_ready"
    assert result["capture"]["complete"] is True
    assert result["capture"]["journal"]["sealed"] is True
    assert result["readback"]["strict_reader"] is True
    assert result["readback"]["complete"] is True
    assert result["readback"]["record_count"] == result["capture"]["accepted_records"]
    assert result["quotes"]["requested"] == 4
    assert result["quotes"]["accepted"] == 4
    assert result["admissions"]["accepted"] == 1
    assert result["admissions"]["callback_count"] == 1
    assert result["timestamps"]["last_quote_at"] == qualifier._quote_at(3, 4).isoformat()
    assert result["timestamps"]["last_quote_at"] != (qualifier.END - timedelta(seconds=1)).isoformat()


def test_order_ready_boundary_uses_only_five_quote_admissions(tmp_path):
    """Breaks if the first-five admission limit permits a sixth callback."""
    qualifier = _qualifier()
    profile = qualifier.Profile(
        scans=1,
        candidates_per_scan=7,
        buffer_capacity=128,
        queue_capacity=128,
        batch_size=16,
        max_bytes=2 * 1024 * 1024,
        max_record_bytes=64 * 1024,
    )

    result = qualifier.run_case("smoke", workspace=tmp_path, profile=profile, quote_count=0)

    assert result["admissions"] == {
        "attempted": 7,
        "accepted": 5,
        "rejected": 2,
        "callback_count": 5,
        "max_candidates": 5,
    }
    assert result["capture_contract"]["channels"] == {
        "registration_cap": 41,
        "external_reserved": 0,
        "operational_headroom": 10,
        "max_candidates": 5,
        "lease_seconds": 5100,
    }
    assert result["capture"]["complete"] is True


def test_overflow_preserves_exact_drop_count_and_seals_incomplete(tmp_path):
    """Breaks if a full buffer silently reports a complete or gapless journal."""
    qualifier = _qualifier()

    result = qualifier.run_case(
        "overflow", workspace=tmp_path, profile=_small_profile(qualifier, capacity=11), quote_count=10
    )

    assert result["counts"]["attempted_records"] == 16
    assert result["capture"]["accepted_records"] == 11
    assert result["capture"]["dropped_records"] == 5
    assert result["quotes"] == {
        "requested": 10,
        "accepted": 5,
        "rejected": 5,
        "schema": "ws_quote/H0STASP0/synthetic-six-digit-symbol",
    }
    assert result["capture"]["complete"] is False
    assert result["capture"]["journal"]["sealed"] is True
    assert result["readback"]["complete"] is False
    assert result["readback"]["dropped_records"] == 5
    assert result["timestamps"]["last_quote_at"] == qualifier._quote_at(4, 10).isoformat()
    assert result["timestamps"]["requested_last_quote_at"] == qualifier._quote_at(9, 10).isoformat()


def test_window_boundaries_and_scaled_quote_cadence_are_explicit(tmp_path):
    """Breaks if half-open admission/window timestamps drift from the H1 contract."""
    qualifier = _qualifier()
    profile = qualifier.Profile(scans=14, candidates_per_scan=1, buffer_capacity=100,
        queue_capacity=64, batch_size=8, max_bytes=4 * 1024 * 1024, max_record_bytes=64 * 1024)
    plan = qualifier._plan(profile, tmp_path)
    clock = [plan.start_at - timedelta(microseconds=1)]
    buffer = qualifier._WindowBuffer(plan, lambda: clock[0])
    assert buffer.begin_scan() is None
    clock[0] = plan.start_at
    assert buffer.begin_scan() is not None
    clock[0] = buffer.admission_end - timedelta(microseconds=1)
    assert buffer.begin_scan() is not None
    clock[0] = buffer.admission_end
    assert buffer.begin_scan() is None
    clock[0] = buffer.end_at - timedelta(microseconds=1)
    assert buffer.publish({"kind": "ws_quote"}) is True
    clock[0] = buffer.end_at
    assert buffer.publish({"kind": "ws_quote"}) is False
    clock[0] = plan.start_at
    capped = qualifier._WindowBuffer(plan, lambda: clock[0])
    assert all(capped.begin_scan() is not None for _ in range(14))
    assert capped.begin_scan() is None
    assert "scan_limit_exceeded" in capped.capture_status()["incomplete_reasons"]
    assert qualifier._quote_at(0, 335_340) == qualifier.START
    assert qualifier._quote_at(335_339, 335_340) == qualifier.END - timedelta(seconds=1)
    assert qualifier._rolling_60_peak(335_340) == 4140


def test_public_cli_uses_separate_limited_capture_and_reader_processes():
    """Breaks if public success no longer contains fresh reader evidence under RLIMIT_AS."""
    completed = subprocess.run([sys.executable, str(CLI), "--case", "smoke"], cwd=ROOT,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30, check=False)
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["result"]["expectations_met"] is True
    assert result["resource"]["address_space_limit_bytes"] == 1_342_177_280
    assert result["readback"]["address_space_limit_bytes"] == 1_342_177_280
    assert result["resource"]["capture_peak_rss_bytes"] > 0
    assert result["readback"]["reader_peak_rss_bytes"] > 0
    assert result["readback"]["records_contract_validated"] is True


def test_reader_result_is_an_acceptance_requirement(tmp_path):
    """Breaks if a failed fresh reader can be overwritten by capture-side success."""
    qualifier = _qualifier()
    workspace = tmp_path / "separate-reader"
    workspace.mkdir()
    capture = asyncio.run(qualifier._capture("smoke", workspace, _small_profile(qualifier), 4))
    readback = qualifier._readback(capture)
    readback["record_count"] += 1
    assert qualifier._finish(capture, readback)["result"]["expectations_met"] is False
