"""Prospective observed-flow report through the pinned file-only runner."""
from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path

import pytest

from test_pregate_readout_runner import runner, manifest_for, write_manifest
from test_observed_regular_flow_cli import collected

VERSION = "deterministic-observed-flow-readout-v1"


def manifest(mod, tmp_path, plan):
    value = manifest_for(mod, tmp_path, plan)
    value.update(version=VERSION, source_sha256=mod.source_fingerprint(VERSION))
    return value


def test_source_inventory_pins_new_cli_and_imported_study_reader(tmp_path, monkeypatch):
    mod = runner()
    root = tmp_path / "source"
    (root / "scripts/ops").mkdir(parents=True)
    (root / "src").mkdir()
    names = ("scripts/ops/run_capture_readout.py",
             "scripts/report_pregate_observed_regular_flow.py",
             "scripts/report_entry_gate_trace.py", "src/model.py")
    for name in names:
        (root / name).write_text("# fixed source\n")
    monkeypatch.setattr(mod, "ROOT", root)
    original = mod.source_fingerprint(VERSION)
    for name in names:
        path = root / name
        path.write_text("# changed source\n")
        assert mod.source_fingerprint(VERSION) != original
        path.write_text("# fixed source\n")
    assert mod.source_fingerprint("observed-flow") == original


@pytest.mark.asyncio
@pytest.mark.parametrize("empty", [False, True])
async def test_real_report_preserves_strict_and_observed_denominators(tmp_path, monkeypatch, empty):
    mod = runner()
    plan, _, as_of, _ = await collected(tmp_path, monkeypatch, empty=empty)
    value = manifest(mod, tmp_path, plan)
    path = write_manifest(tmp_path / "manifest.json", value)
    before = (plan.study_path.read_bytes(), Path(value["ledger"]).read_bytes())
    now = lambda: datetime.fromisoformat(as_of) + timedelta(minutes=2)
    result = mod.run(path, now=now)
    assert result["status"] == "diagnostics_complete", result
    assert [c["name"] for c in result["commands"]] == ["pregate-observed-regular-flow"]
    assert set(result["inputs"]) == {"study", "ledger"}
    assert result["decision_recording_finality"] == "not_applicable"
    assert result["net_pnl"] is result["account_return"] is None
    assert result["production_eligible"] is result["profitability_pass"] is False
    report = json.loads((Path(value["output_dir"]) / "pregate-observed-regular-flow.json").read_text())
    assert report["strict_report"]["denominator"] == report["denominator"]
    assert result["observed_flow_counts"] == report["counts"]
    assert report["counts"] == {"denominator": 0 if empty else 7, "eligible_pairs": 0 if empty else 5}
    assert before == (plan.study_path.read_bytes(), Path(value["ledger"]).read_bytes())
    assert mod.run(path, now=now)["status"] == "already_attempted"


@pytest.mark.asyncio
async def test_missing_prospective_policy_blocks_instead_of_using_strict_profile(tmp_path, monkeypatch):
    mod = runner()
    plan, _, as_of, _ = await collected(tmp_path, monkeypatch, enabled=False)
    value = manifest(mod, tmp_path, plan)
    result = mod.run(write_manifest(tmp_path / "manifest.json", value),
                     now=lambda: datetime.fromisoformat(as_of) + timedelta(minutes=2))
    assert result["status"] == "blocked"
    assert len(result["commands"]) == 1
    assert result["commands"][0]["returncode"] == 2
    assert result.get("observed_flow_counts") is None


@pytest.mark.asyncio
async def test_report_validation_rejects_rebinding_counts_or_economic_promotion(tmp_path, monkeypatch):
    from src.analytics.pregate_observed_regular_flow import build_observed_regular_flow_report

    mod = runner()
    plan, observations, as_of, _ = await collected(tmp_path, monkeypatch)
    report = build_observed_regular_flow_report(
        plan.study_bytes, observations, study_sha256=plan.study_sha256, analysis_as_of=as_of)
    study = json.loads(plan.study_bytes)
    mod._observed_flow_report(report, study=study, study_sha256=plan.study_sha256, analysis_as_of=as_of)
    changes = [
        lambda r: r.update(study_sha256="f" * 64),
        lambda r: r.update(analysis_as_of="2026-10-21T01:00:00+00:00"),
        lambda r: r.update(evaluation_epoch="foreign"),
        lambda r: r["observed_regular_flow_policy"].update(preparation_seconds=True),
        lambda r: r["observed_regular_flow_policy"].update(fixed_at="placeholder"),
        lambda r: r.pop("observed_regular_flow_policy"),
        lambda r: r["strict_report"].update(study_sha256="f" * 64),
        lambda r: r.update(production_eligible=True),
        lambda r: r.update(net_pnl=0),
        lambda r: r.update(source_authenticity_verified=True),
        lambda r: r["counts"].update(eligible_pairs=True),
        lambda r: r["counts"].update(eligible_pairs=4),
        lambda r: r["counts"].update(denominator=6),
        lambda r: r["candidates"].reverse(),
        lambda r: r["candidates"][0].update(gross_quote_move_pct=True),
        lambda r: r["candidates"][0].update(gross_quote_move_pct=float("inf")),
        lambda r: r["candidates"][0].update(gross_quote_move_pct=10**4000),
        lambda r: r["candidates"][0].update(gross_quote_move_pct=2**1024-1),
        lambda r: r["observed_regular_flow_policy"].update(
            fixed_at=(datetime.fromisoformat(as_of) - timedelta(seconds=1)).isoformat()),
        lambda r: (r.update(evaluation_epoch="foreign"),
                   r["strict_report"].update(evaluation_epoch="foreign")),
        lambda r: r["candidates"][0].update(gross_quote_move_pct=12345.0),
        lambda r: r["candidates"][0].update(anchor_quote={}),
        lambda r: r["candidates"][0].update(original_entry_quote={"changed": True}),
        lambda r: r["candidates"][0]["anchor_state"].update(vi_state="normal"),
        lambda r: r["candidates"][0]["markout_state"].update(fillability="confirmed"),
    ]
    for change in changes:
        invalid = deepcopy(report)
        change(invalid)
        with pytest.raises(ValueError):
            mod._observed_flow_report(invalid, study=study, study_sha256=plan.study_sha256, analysis_as_of=as_of)


@pytest.mark.asyncio
@pytest.mark.parametrize("number", [10**4000, 2**1024-1], ids=["oversized", "float-overflow-boundary"])
async def test_malformed_child_number_writes_blocked_summary(tmp_path, monkeypatch, number):
    mod = runner()
    plan, _, as_of, _ = await collected(tmp_path, monkeypatch)
    value = manifest(mod, tmp_path, plan)
    child = mod._child

    def corrupted_child(name, argv, output, fd):
        result = child(name, argv, output, fd)
        path = output / (name + ".json")
        report = json.loads(path.read_text())
        report["candidates"][0]["gross_quote_move_pct"] = number
        path.write_text(json.dumps(report))
        return result

    monkeypatch.setattr(mod, "_child", corrupted_child)
    result = mod.run(write_manifest(tmp_path / "manifest.json", value),
                     now=lambda: datetime.fromisoformat(as_of) + timedelta(minutes=2))
    assert result["status"] == "blocked"
    summary = json.loads((Path(value["output_dir"]) / "summary.json").read_text())
    assert summary["status"] == "blocked"
    assert summary.get("observed_flow_counts") is None


def test_check_does_not_consume_future_observed_flow_attempt(tmp_path):
    mod = runner()
    value = {
        "version": VERSION, "study": str(tmp_path / "future-study.json"),
        "study_sha256": "0" * 64, "ledger": str(tmp_path / "future-ledger.jsonl"),
        "output_dir": str(tmp_path / "output"),
        "not_before": "2026-10-21T09:55:00+09:00", "not_after": "2026-10-21T10:30:00+09:00",
        "source_sha256": mod.source_fingerprint(VERSION),
    }
    path = write_manifest(tmp_path / "manifest.json", value)
    assert mod.check(path)["status"] == "preflight_ok"
    assert not Path(value["output_dir"]).exists()
    value["decisions"] = str(tmp_path / "decisions.jsonl")
    write_manifest(path, value)
    assert mod.check(path)["status"] == "invalid_manifest"
