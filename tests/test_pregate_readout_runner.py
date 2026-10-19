"""Pregate-only bounded readout profile regression tests."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ops" / "run_capture_readout.py"
PREGATE_VERSION = "deterministic-pregate-readout-v1"


def runner():
    spec = importlib.util.spec_from_file_location("pregate_readout_runner", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def manifest_for(mod, tmp_path, plan, *, output="readout"):
    study = plan.study_path
    ledger = Path(plan.settings["journal_path"])
    return {
        "version": PREGATE_VERSION,
        "study": str(study),
        "study_sha256": hashlib.sha256(study.read_bytes()).hexdigest(),
        "ledger": str(ledger),
        "output_dir": str(tmp_path / output),
        "not_before": (plan.end_at + timedelta(minutes=1)).isoformat(),
        "not_after": (plan.end_at + timedelta(minutes=20)).isoformat(),
        "source_sha256": mod.source_fingerprint(version=PREGATE_VERSION),
    }


def write_manifest(path, value):
    path.write_text(json.dumps(value))
    return path


def rewrite_without_market_state(path, symbol):
    """Keep a syntactically valid fixture while removing received-state evidence for one symbol."""
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    records = [row["payload"] for row in rows if row["kind"] == "record"]
    records = [record for record in records
               if not (record["kind"] == "market_state" and record["symbol"] == symbol)]
    for sequence, record in enumerate(records, 1):
        record["sequence"] = sequence
    kinds = [("start", rows[0]["payload"]), *(("record", record) for record in records),
             ("seal", rows[-1]["payload"])]
    seal = kinds[-1][1]
    seal.update(record_count=len(records), accepted_record_count=len(records), persistence_dropped_records=0)
    previous = "0" * 64
    rebuilt = []
    for number, (kind, payload) in enumerate(kinds, 1):
        envelope = {"row": number, "previous_hash": previous, "kind": kind, "payload": payload}
        digest = hashlib.sha256(json.dumps(envelope, ensure_ascii=False, sort_keys=True,
                                           separators=(",", ":")).encode()).hexdigest()
        rebuilt.append({**envelope, "hash": digest})
        previous = digest
    path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True,
                                        separators=(",", ":")) + "\n" for row in rebuilt))


def test_pregate_fingerprint_covers_transitive_study_script(tmp_path):
    """Dropping an imported helper from the profile fingerprint must invalidate its source receipt."""
    mod = runner()
    root = tmp_path / "source"
    (root / "scripts/ops").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "scripts/ops/run_capture_readout.py").write_text("# runner\n")
    (root / "scripts/report_pregate_candidate_observation.py").write_text("# pregate cli\n")
    (root / "scripts/report_entry_gate_trace.py").write_text("VALUE = 1\n")
    (root / "src/model.py").write_text("VALUE = 1\n")
    original_root = mod.ROOT
    try:
        mod.ROOT = root
        first = mod.source_fingerprint(version=PREGATE_VERSION)
        (root / "scripts/report_entry_gate_trace.py").write_text("VALUE = 2\n")
        assert mod.source_fingerprint(version=PREGATE_VERSION) != first
    finally:
        mod.ROOT = original_root


@pytest.mark.asyncio
async def test_pregate_profile_runs_only_real_candidate_readout_and_preserves_inputs(tmp_path, monkeypatch):
    """Removing the pregate dispatch or adding a legacy decision tool must fail this contract."""
    from test_pregate_capture_flow import collect_first_scan

    mod = runner()
    plan, _, as_of, _ = await collect_first_scan(tmp_path, monkeypatch)
    manifest = manifest_for(mod, tmp_path, plan)
    before = Path(manifest["study"]).read_bytes(), Path(manifest["ledger"]).read_bytes()
    manifest_path = tmp_path / "pregate-manifest.json"
    manifest_path.write_text(json.dumps(manifest))

    result = mod.run(manifest_path, now=lambda: datetime.fromisoformat(as_of) + timedelta(minutes=2))

    assert result["status"] == "diagnostics_complete", result
    assert result["version"] == PREGATE_VERSION
    assert [command["name"] for command in result["commands"]] == ["pregate-candidate-observation"]
    assert set(result["inputs"]) == {"study", "ledger"}
    assert result["decision_recording_finality"] == "not_applicable"
    assert result["source_authenticity_verified"] is False
    assert result["account_return"] is None
    assert result["profitability_pass"] is result["production_eligible"] is False
    report = json.loads((Path(manifest["output_dir"]) / "pregate-candidate-observation.json").read_text())
    assert report["study_sha256"] == manifest["study_sha256"]
    assert report["analysis_as_of"] == result["analysis_as_of"]
    assert set(report["validation"]) == {"frame", "selection", "gates"}
    assert report["source_authenticity_verified"] is False
    assert report["account_return"] is None
    assert report["profitability_pass"] is report["production_eligible"] is False
    assert report["denominator"]["returned_candidates"] == 7
    assert len(report["candidates"]) == 7
    assert sum(row["entry_quote"] is not None for row in report["candidates"]) == 5
    assert before == (Path(manifest["study"]).read_bytes(), Path(manifest["ledger"]).read_bytes())
    assert stat.S_IMODE(Path(manifest["output_dir"]).stat().st_mode) == 0o700
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600
               for path in Path(manifest["output_dir"]).rglob("*") if path.is_file())
    before_output = {path.name: path.read_bytes() for path in Path(manifest["output_dir"]).iterdir()
                     if path.is_file()}
    assert mod.run(manifest_path, now=lambda: datetime.fromisoformat(as_of) + timedelta(minutes=2))["status"] == "already_attempted"
    assert before_output == {path.name: path.read_bytes() for path in Path(manifest["output_dir"]).iterdir()
                             if path.is_file()}


def test_pregate_check_allows_absent_future_inputs_without_writing(tmp_path):
    """Changing preflight into a consuming attempt would prevent the planned capture from running."""
    mod = runner()
    manifest = {
        "version": PREGATE_VERSION, "study": str(tmp_path / "future" / "study.json"),
        "study_sha256": "0" * 64, "ledger": str(tmp_path / "future" / "journal.jsonl"),
        "output_dir": str(tmp_path / "private-output"),
        "not_before": "2026-10-20T09:30:00+09:00", "not_after": "2026-10-20T09:45:00+09:00",
        "source_sha256": mod.source_fingerprint(version=PREGATE_VERSION),
    }
    path = write_manifest(tmp_path / "manifest.json", manifest)
    assert mod.check(path)["status"] == "preflight_ok"
    assert not Path(manifest["output_dir"]).exists()


@pytest.mark.parametrize("change", [
    lambda value: value.update(version="unknown-profile"),
    lambda value: value.update(decisions="/tmp/foreign-decisions.jsonl"),
])
def test_pregate_manifest_rejects_unknown_or_mixed_fields_without_output(tmp_path, change):
    """Accepting an unknown profile or legacy decision field can activate a different analysis path."""
    mod = runner()
    manifest = {
        "version": PREGATE_VERSION, "study": str(tmp_path / "study.json"), "study_sha256": "0" * 64,
        "ledger": str(tmp_path / "journal.jsonl"), "output_dir": str(tmp_path / "output"),
        "not_before": "2026-10-20T09:30:00+09:00", "not_after": "2026-10-20T09:45:00+09:00",
        "source_sha256": mod.source_fingerprint(version=PREGATE_VERSION),
    }
    change(manifest)
    assert mod.check(write_manifest(tmp_path / "manifest.json", manifest))["status"] == "invalid_manifest"
    assert not Path(manifest["output_dir"]).exists()


def test_source_fingerprint_cli_profiles_are_explicit_and_legacy_default_is_preserved():
    """A changed default or an accepted profile on run/check would make receipts ambiguous."""
    mod = runner()
    legacy = subprocess.run([sys.executable, str(SCRIPT), "--source-fingerprint"], text=True,
                            capture_output=True, check=False)
    pregate = subprocess.run([sys.executable, str(SCRIPT), "--source-fingerprint", "--profile", "pregate"],
                             text=True, capture_output=True, check=False)
    misplaced = subprocess.run([sys.executable, str(SCRIPT), "--profile", "pregate", "--check"],
                               text=True, capture_output=True, check=False)
    assert legacy.returncode == pregate.returncode == 0
    assert legacy.stdout.strip() == mod.source_fingerprint()
    assert pregate.stdout.strip() == mod.source_fingerprint(version=PREGATE_VERSION)
    assert legacy.stdout != pregate.stdout
    assert misplaced.returncode != 0


@pytest.mark.asyncio
async def test_pregate_empty_first_scan_is_a_completed_diagnostic(tmp_path, monkeypatch):
    """Treating zero returned candidates as an error would fabricate a replacement cohort."""
    from test_pregate_capture_flow import collect_first_scan

    mod = runner()
    plan, _, as_of, _ = await collect_first_scan(tmp_path, monkeypatch, empty_first=True)
    manifest = manifest_for(mod, tmp_path, plan)
    result = mod.run(write_manifest(tmp_path / "manifest.json", manifest),
                     now=lambda: datetime.fromisoformat(as_of) + timedelta(minutes=2))
    report = json.loads((Path(manifest["output_dir"]) / "pregate-candidate-observation.json").read_text())
    assert result["status"] == "diagnostics_complete", result
    assert report["denominator"]["returned_candidates"] == 0
    assert report["candidates"] == []


@pytest.mark.asyncio
async def test_pregate_missing_state_evidence_stays_unknown(tmp_path, monkeypatch):
    """Inferring normal state after a missing received record would overstate the observation."""
    from test_pregate_capture_flow import collect_first_scan

    mod = runner()
    plan, _, as_of, symbols = await collect_first_scan(tmp_path, monkeypatch)
    rewrite_without_market_state(Path(plan.settings["journal_path"]), symbols[0])
    manifest = manifest_for(mod, tmp_path, plan)
    result = mod.run(write_manifest(tmp_path / "manifest.json", manifest),
                     now=lambda: datetime.fromisoformat(as_of) + timedelta(minutes=2))
    report = json.loads((Path(manifest["output_dir"]) / "pregate-candidate-observation.json").read_text())
    assert result["status"] == "diagnostics_complete", result
    assert report["candidates"][0]["entry_market_state"]["verdict"] == "unknown"


@pytest.mark.asyncio
async def test_pregate_incomplete_journal_is_consumed_as_blocked_attempt(tmp_path, monkeypatch):
    """Running a report on an unsealed journal must not become a completed diagnostic or retry."""
    from test_pregate_capture_flow import collect_first_scan

    mod = runner()
    plan, _, as_of, _ = await collect_first_scan(tmp_path, monkeypatch)
    ledger = Path(plan.settings["journal_path"])
    ledger.write_bytes(b"".join(ledger.read_bytes().splitlines(keepends=True)[:-1]))
    manifest = manifest_for(mod, tmp_path, plan)
    path = write_manifest(tmp_path / "manifest.json", manifest)
    result = mod.run(path, now=lambda: datetime.fromisoformat(as_of) + timedelta(minutes=2))
    assert result["status"] == "blocked"
    assert len(result["commands"]) == 1 and result["commands"][0]["returncode"] == 2
    assert (Path(manifest["output_dir"]) / "attempted.json").exists()
    assert mod.run(path, now=lambda: datetime.fromisoformat(as_of) + timedelta(minutes=2))["status"] == "already_attempted"
