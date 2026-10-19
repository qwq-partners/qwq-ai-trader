"""Offline pre-gate capture-capacity assessment contract."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).parents[1]
PYTHON = Path("/home/ubuntu/projects/qwq-ai-trader/venv/bin/python")
CLI = ROOT / "scripts" / "assess_pregate_capture.py"


def study():
    """A hand-sized first-scan-v4 contract at every resource boundary."""
    return {
        "capture": {
            "version": "runner-first-scan-v4",
            "source_version_ref": "source", "configuration_ref": "config",
            "start_at": "2026-10-19T09:25:00+09:00",
            "admission_end_at": "2026-10-19T09:30:00+09:00",
            "end_at": "2026-10-19T09:45:05+09:00",
            "buffer_capacity": 15,
            "queue_capacity": 15,
            "batch_size": 15,
            "max_record_bytes": 65536,
            "max_bytes": 15 * (65536 + 2048) + 2048,
            "selection_basis": {
                "version": "selection-basis-v4", "max_candidates": 5,
                "max_source_terms": 16, "max_modifier_stages": 24,
            },
            "entry_gate_trace": {
                "version": "entry-gate-trace-v1", "policy_ref": "kr-live-screening-gates-v1",
                "max_candidates": 5, "source_version_ref": "source", "configuration_ref": "config",
            },
            "frame_diagnostics": {"version": "kis-frame-diagnostics-v1"},
            "channels": {
                "registration_cap": 41, "external_reserved": 0,
                "operational_headroom": 0, "max_candidates": 5,
                "lease_seconds": 1205, "evidence_ref": "offline-capacity-evidence",
                "market_state_evidence": True, "market_state_max_records": 4,
            },
        }
    }


def assess(value, **kwargs):
    from src.analytics.pregate_capture_capacity import assess_pregate_capture_resources
    return assess_pregate_capture_resources(value,
                                            operational_registrations=kwargs.pop("operational_registrations", 20),
                                            external_registrations=kwargs.pop("external_registrations", 6), **kwargs)


def test_exact_capacity_boundary_reports_numeric_pass_but_never_installation_ready():
    """Breaks if exact 15+20+6 capacity or mandatory installation blockers are ignored."""
    original = study()
    report = assess(original)

    assert report["requirements"]["candidate_channels"] == 15
    assert report["requirements"]["metadata_records"] == 11
    assert report["requirements"]["minimum_buffer_records"] == 15
    assert report["checks"]["registration_capacity"] == {
        "passed": True, "candidate_channels": 15, "operational_registrations": 20,
        "external_registrations": 6, "external_reserved": 0,
        "operational_headroom": 0, "used": 41, "cap": 41,
    }
    assert report["checks"]["journal_budget"]["passed"] is True
    assert report["checks"]["readout_limit"]["passed"] is True
    assert report["numeric_configuration_passed"] is True
    assert report["installation_ready"] is False
    assert report["runtime_memory_safety_verified"] is False
    assert report["first_scan_runtime_readiness_verified"] is False
    assert {row["code"] for row in report["blockers"]} >= {
        "runtime_memory_guard_unavailable", "first_scan_runtime_readiness_unavailable",
        "consumed_v5_rotation_unimplemented", "calendar_unverified", "live_demand_not_verified",
    }
    assert original == study()


def test_one_registration_over_cap_fails_the_capacity_check():
    """Breaks if caller-supplied operational/external demand can exceed the 41-channel ceiling."""
    report = assess(study(), operational_registrations=20, external_registrations=7)
    assert report["checks"]["registration_capacity"] == {
        "passed": False, "candidate_channels": 15, "operational_registrations": 20,
        "external_registrations": 7, "external_reserved": 0,
        "operational_headroom": 0, "used": 42, "cap": 41,
    }
    assert report["numeric_configuration_passed"] is False


@pytest.mark.parametrize("field, value", [
    ("buffer_capacity", True), ("queue_capacity", float("nan")),
    ("batch_size", 1.5), ("max_record_bytes", "65536"),
    ("max_bytes", False),
])
def test_invalid_resource_numbers_are_rejected_before_reporting(field, value):
    """Breaks if bool, non-finite, or non-integer resource limits reach capacity arithmetic."""
    value_study = study()
    value_study["capture"][field] = value
    with pytest.raises(ValueError):
        assess(value_study)


def test_insufficient_tail_or_lease_is_reported_without_claiming_a_calendar_date():
    """Breaks if +900s/+5s or whole-window lease requirements are silently shortened."""
    value_study = study()
    value_study["capture"]["end_at"] = "2026-10-19T09:45:04+09:00"
    value_study["capture"]["channels"]["lease_seconds"] = 1203
    report = assess(value_study)
    assert report["checks"]["markout_tail"]["passed"] is False
    assert report["checks"]["lease_window"]["passed"] is False
    assert report["calendar_verified"] is False


def run_cli(path, *args):
    return subprocess.run([str(PYTHON), str(CLI), str(path),
                           "--operational-registrations", "20",
                           "--external-registrations", "6", *args],
                          cwd=ROOT, capture_output=True, text=True, check=False)


def test_cli_is_bounded_strict_and_does_not_modify_regular_input(tmp_path):
    """Breaks if the offline CLI accepts duplicate keys or mutates its study input."""
    path = tmp_path / "study.json"
    path.write_text(json.dumps(study()))
    original = path.read_bytes()
    result = run_cli(path)
    assert result.returncode == 0
    assert json.loads(result.stdout)["installation_ready"] is False
    assert path.read_bytes() == original

    path.write_text('{"capture": {}, "capture": {}}')
    duplicate = run_cli(path)
    assert duplicate.returncode == 2
    assert duplicate.stdout == ""


def test_cli_rejects_oversize_symlink_and_fifo_without_following_or_blocking(tmp_path):
    """Breaks if a special file, symlink, or oversized input bypasses the bounded reader."""
    oversize = tmp_path / "oversize.json"
    oversize.write_bytes(b" " * (4 * 1024 * 1024 + 1))
    assert run_cli(oversize).returncode == 2

    target = tmp_path / "target.json"
    target.write_text(json.dumps(study()))
    link = tmp_path / "study-link.json"
    link.symlink_to(target)
    assert run_cli(link).returncode == 2

    fifo = tmp_path / "study.fifo"
    os.mkfifo(fifo)
    assert run_cli(fifo).returncode == 2
