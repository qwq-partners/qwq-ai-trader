"""Offline qualification of the actual signal-window buffer and journal."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "qualify_signal_window_capacity.py"


def _qualifier():
    spec = importlib.util.spec_from_file_location("signal_window_capacity", CLI)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


def test_smoke_qualifies_sealed_strict_readback_without_claiming_profitability(tmp_path):
    """Breaks if capture, sealing, or strict readback stops being complete."""
    qualifier = _qualifier()

    result = qualifier.run_case("smoke", workspace=tmp_path)

    assert result["synthetic"] is True
    assert result["profitability"] is False
    assert result["capture"]["complete"] is True
    assert result["capture"]["dropped_records"] == 0
    assert result["readback"]["complete"] is True
    assert result["readback"]["record_count"] == result["capture"]["accepted_records"]
    assert result["quotes"]["requested"] == 512
    assert result["quotes"]["accepted"] == 512


def test_overflow_keeps_exact_drops_and_seals_fail_closed(tmp_path):
    """Breaks if records beyond the buffer capacity are silently treated as complete."""
    qualifier = _qualifier()
    profile = qualifier.Profile(
        scans=1,
        candidates_per_scan=1,
        buffer_capacity=5,
        queue_capacity=64,
        batch_size=8,
        max_bytes=2 * 1024 * 1024,
        max_record_bytes=64 * 1024,
    )

    result = qualifier.run_case("overflow", workspace=tmp_path, profile=profile, quote_count=6)

    assert result["capture"]["complete"] is False
    assert result["capture"]["dropped_records"] == 4
    assert result["capture"]["accepted_records"] == 5
    assert result["quotes"]["requested"] == 6
    assert result["quotes"]["accepted"] == 2
    assert result["readback"]["complete"] is False
    assert result["readback"]["dropped_records"] == 4
