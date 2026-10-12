"""Offline H1 preparation contracts; no runtime installation is exercised."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from h1_multiday_fixtures import template


ROOT = Path(__file__).parents[1]
CLI = ROOT / "scripts" / "prepare_h1_observation.py"
PREPARED_AT = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)


def dates():
    current = date(2026, 10, 13)
    result = []
    while len(result) < 20:
        if current.weekday() < 5:
            result.append(current.isoformat())
        current += timedelta(days=1)
    return result


def canonical(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                       allow_nan=False) + "\n").encode()


def request(tmp_path):
    context, _ = template(tmp_path)
    # The records remain local synthetic fixtures; this is an observed-input schema fixture only.
    context["dataset_kind"] = "observed"
    context["evaluation_epoch"] = "h1-observation-preparation-test-epoch"
    first = dates()[0]
    context["capture"].update(
        start_at=first + "T09:00:00+09:00",
        admission_end_at=first + "T10:05:00+09:00",
        end_at=first + "T10:21:00+09:00",
        max_scans=14,
        channels={
            "registration_cap": 41,
            "external_reserved": 0,
            "operational_headroom": 10,
            "max_candidates": 5,
            "lease_seconds": 5100,
            "evidence_ref": "synthetic-capacity-evidence",
        },
        buffer_capacity=400000,
        queue_capacity=8192,
        batch_size=256,
        max_bytes=512 * 1024 * 1024,
        max_record_bytes=65536,
        selection_basis={"version": "selection-basis-v2", "max_candidates": 100, "max_source_terms": 16},
    )
    context["capture"]["entry_gate_trace"]["max_candidates"] = 100
    context["policy"].update(max_quote_age_seconds=1, max_decision_delay_seconds=5)
    context["received_policy"].update(max_rest_rtt_seconds=3, max_target_age_seconds=30)
    context["markout_policy"].update(horizon_seconds=900, max_delay_seconds=5)
    context["fees"] = {
        "buy_commission_rate": "0.00015",
        "sell_commission_rate": "0.00015",
        "sell_tax_rate": "0.0018",
    }
    context["regime_policy"] = {
        "source": "entry-gate-trace-v1/regime",
        "version": "h1-gate-regime-policy-v1",
        "categories": ["downside_gate_passed", "downside_gate_blocked", "unknown"],
        "missing": "unknown",
    }
    return {
        "protocol_ref": "synthetic-h1-preparation",
        "study_template": context,
        "calendar": {
            "source_ref": "synthetic-weekday-list-not-market-proof",
            "source_sha256": "c" * 64,
            "scheduled_dates": dates(),
        },
        "input_root": str(tmp_path / "inputs"),
        "state_root": str(tmp_path / "state"),
    }


def build(value, *, prepared_at=PREPARED_AT):
    try:
        from src.analytics.h1_observation_preparation import build_preparation
    except ModuleNotFoundError:
        pytest.fail("H1 observation preparation builder is not implemented")
    return build_preparation(value, prepared_at=prepared_at)


def materialize(package):
    for file in package["files"]:
        path = Path(file["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(file["utf8"], encoding="utf-8")


def test_builds_twenty_hash_bound_capture_plan_triplets(tmp_path):
    value = request(tmp_path)
    out = build(value)
    assert out["status"] == "preparation_only"
    assert out["installation_ready"] is False
    assert out["source_authenticity_verified"] is False
    assert len(out["planned_days"]) == 20
    assert len(out["files"]) == 60
    assert out["evaluation_manifest"]["days"] == []
    assert out["evaluation_manifest"]["protocol"] == out["proposed_protocol"]
    assert out["evaluation_manifest"]["as_of"] == PREPARED_AT.isoformat()
    assert out["proposed_protocol"]["fixed_at"] == PREPARED_AT.isoformat()
    assert out["proposed_protocol"]["study_policy_sha256"]
    from src.analytics.h1_multiday_evaluation import build_h1_multiday_report
    quality = build_h1_multiday_report(out["evaluation_manifest"]["protocol"],
                                       out["evaluation_manifest"]["days"],
                                       analysis_as_of=out["evaluation_manifest"]["as_of"])
    assert len(quality["days"]) == 20
    assert quality["quality_failures"] == dates()
    assert quality["economic_verdict"] == "suppressed"
    assert quality["scenarios"]["0"]["delta_net_pnl"] is None
    checks = " ".join(out["unresolved_external_checks"]).lower()
    for word in ("session", "fee", "channel", "memory", "calendar", "installer"):
        assert word in checks
    materialize(out)
    from src.analytics.entry_observation_runtime import CapturePlan
    for day in out["planned_days"]:
        manifest = Path(day["manifest_path"])
        study = Path(day["study_path"])
        once = Path(day["once_path"])
        assert manifest.parent.name == day["date"]
        assert study.parent == manifest.parent == once.parent
        assert Path(day["journal_path"]).name == "engine.jsonl"
        assert Path(day["receipt_path"]).name == "startup.receipt"
        assert not Path(day["journal_path"]).exists()
        plan = CapturePlan.load(manifest, now=PREPARED_AT)
        assert plan.context["capture"]["version"] == "runner-order-ready-window-v1"
        assert plan.context["as_of"] == day["end_at"]
        assert json.loads(once.read_text())["not_before"] == day["not_before"]
    assert len({day["journal_path"] for day in out["planned_days"]}) == 20
    assert len({day["receipt_path"] for day in out["planned_days"]}) == 20
    for row in out["files"]:
        data = row["utf8"].encode()
        assert row["utf8"].endswith("\n")
        assert hashlib.sha256(data).hexdigest() == row["sha256"]


@pytest.mark.parametrize("prepared_at", [
    datetime(2026, 10, 12, 15, tzinfo=timezone.utc),
    datetime(2026, 10, 13, 0, tzinfo=timezone.utc),
])
def test_rejects_nonprospective_preparation_time(tmp_path, prepared_at):
    with pytest.raises(ValueError):
        build(request(tmp_path), prepared_at=prepared_at)


@pytest.mark.parametrize("mutation", [
    lambda r: r["calendar"].update(scheduled_dates=dates()[:-1]),
    lambda r: r["calendar"].update(scheduled_dates=dates() + ["2026-11-09"]),
    lambda r: r["calendar"].update(scheduled_dates=dates()[:1] * 2 + dates()[2:]),
    lambda r: r["calendar"].update(scheduled_dates=sorted(dates(), reverse=True)),
    lambda r: r["calendar"].update(scheduled_dates=["2026-10-17"] + dates()[1:]),
    lambda r: r["study_template"]["capture"].update(version="runner-signal-window-v1"),
    lambda r: r["study_template"]["capture"].update(max_scans=13),
    lambda r: r["study_template"]["capture"].update(buffer_capacity=399999),
    lambda r: r["study_template"]["capture"].update(selection_basis={"version": "selection-basis-v2", "max_candidates": 5, "max_source_terms": 16}),
    lambda r: r["study_template"]["capture"]["channels"].update(operational_headroom=9),
    lambda r: r["study_template"]["capture"].update(frame_diagnostics={"version": "wrong"}),
    lambda r: r["study_template"]["capture"].update(open_timeout_seconds=0),
    lambda r: r["study_template"]["policy"].update(max_quote_age_seconds=2),
    lambda r: r["study_template"]["policy"].update(entry_slippage_bps="1"),
    lambda r: r["study_template"]["received_policy"].update(max_target_age_seconds="31"),
    lambda r: r["study_template"]["markout_policy"].update(horizon_seconds=901),
    lambda r: r["study_template"]["fees"].update(buy_commission_rate="0"),
    lambda r: r["study_template"]["regime_policy"].update(version="wrong"),
    lambda r: r["study_template"]["regime_policy"].update(categories=["bull", "bear", "unknown"]),
    lambda r: r["study_template"].pop("regime_policy"),
    lambda r: r["study_template"].update(dataset_kind="synthetic"),
])
def test_rejects_invalid_calendar_or_h1_contract(tmp_path, mutation):
    value = request(tmp_path)
    mutation(value)
    with pytest.raises(ValueError):
        build(value)


@pytest.mark.parametrize("root_key,bad", [
    ("input_root", "relative"),
    ("state_root", "/tmp/state/../escape"),
    ("state_root", None),
])
def test_rejects_unsafe_destination_roots(tmp_path, root_key, bad):
    value = request(tmp_path)
    value[root_key] = value["input_root"] if bad is None else bad
    with pytest.raises(ValueError):
        build(value)


def test_is_deterministic_preserves_input_and_hashes_arbitrary_common_metadata(tmp_path):
    value = request(tmp_path)
    value["study_template"]["unmodeled_common_metadata"] = {"review": ["retain", 2]}
    before = deepcopy(value)
    first = build(value)
    second = build(value)
    assert first == second
    assert value == before
    changed = deepcopy(value)
    changed["study_template"]["unmodeled_common_metadata"] = {"review": ["changed", 2]}
    assert build(changed)["proposed_protocol"]["study_policy_sha256"] != first["proposed_protocol"]["study_policy_sha256"]
    value["calendar"]["scheduled_dates"][0] = "2099-01-01"
    assert first["proposed_protocol"]["calendar"]["scheduled_dates"] == dates()


@pytest.mark.parametrize("mutation", [
    lambda r: r["study_template"]["capture"].update(start_at=dates()[0] + "T09:01:00+09:00"),
    lambda r: r["study_template"]["capture"].update(queue_capacity=8192.0),
    lambda r: r["study_template"].update(opportunities=[{"old": "result"}]),
    lambda r: r["study_template"].update(data_basis="market_timestamp"),
    lambda r: r["study_template"].update(capital_policy=None),
])
def test_rejects_repaired_template_contract_violations(tmp_path, mutation):
    value = request(tmp_path)
    mutation(value)
    with pytest.raises(ValueError):
        build(value)


def test_preserves_opaque_date_bearing_study_reference(tmp_path):
    value = request(tmp_path)
    value["study_template"]["capture"]["study_ref"] = "reviewed-2026-10-13-reference"
    out = build(value)
    for file in out["files"]:
        if file["path"].endswith("study.json"):
            assert json.loads(file["utf8"])["capture"]["study_ref"] == "reviewed-2026-10-13-reference"


def test_cli_rejects_invalid_regular_json_inputs_without_success_output(tmp_path):
    source = tmp_path / "request.json"
    source.write_text('{"protocol_ref":1,"protocol_ref":2}', encoding="utf-8")
    result = subprocess.run([sys.executable, str(CLI), "--input", str(source)],
                            text=True, capture_output=True, cwd=ROOT)
    assert result.returncode == 2
    assert result.stdout == ""
    link = tmp_path / "request-link.json"
    link.symlink_to(source)
    result = subprocess.run([sys.executable, str(CLI), "--input", str(link)],
                            text=True, capture_output=True, cwd=ROOT)
    assert result.returncode == 2
    assert result.stdout == ""
    source.write_bytes(b" " * (1024 * 1024 + 1))
    result = subprocess.run([sys.executable, str(CLI), "--input", str(source)],
                            text=True, capture_output=True, cwd=ROOT)
    assert result.returncode == 2
    assert result.stdout == ""
    source.write_text(json.dumps(request(tmp_path) | {"study_template": {}}), encoding="utf-8")
    result = subprocess.run([sys.executable, str(CLI), "--input", str(source)],
                            text=True, capture_output=True, cwd=ROOT)
    assert result.returncode == 2
    assert result.stdout == ""
