"""Exact prospective Oct21 study through a synthetic socket, seal and readout."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest


@pytest.mark.asyncio
async def test_oct21_declared_window_and_volume_reaches_observed_flow(tmp_path, monkeypatch):
    import test_first_scan_volume as volume
    from test_entry_capture_prepare_20261021 import m as installer_fixture, payloads
    from test_market_state_capture import fields, PRICE, BOOK
    from src.analytics.entry_observation_runtime import CapturePlan
    from src.analytics.pregate_capture_capacity import assess_pregate_capture_resources
    from src.analytics.pregate_observed_regular_flow import build_observed_regular_flow_report
    from src.data.feeds import kis_websocket

    installer = installer_fixture.__wrapped__()
    monkeypatch.setattr(installer, "ENGINE_STATE", tmp_path)
    monkeypatch.setattr(installer, "NEW", {**installer.NEW, "study": tmp_path / "study.json",
                                         "engine_manifest": tmp_path / "manifest.json"})

    def plan_factory(path):
        doc, files = payloads(installer)
        installer.validate_payload(doc, files)
        for key in ("study", "engine_manifest"):
            installer.NEW[key].write_bytes(files[key])
        plan = CapturePlan.load(installer.NEW["engine_manifest"],
                                now=datetime(2026, 10, 21, 0, 25, tzinfo=timezone.utc))
        return plan, installer.document(files["study"])

    def frame(tr, symbol):
        now = kis_websocket.datetime.now().astimezone(timezone(timedelta(hours=9)))
        raw = fields(tr, symbol)
        if tr == PRICE:
            raw[1], raw[33] = now.strftime("%H%M%S"), now.strftime("%Y%m%d")
        elif tr == BOOK:
            raw[1] = now.strftime("%H%M%S")
        return f"0|{tr}|001|" + "^".join(raw)

    monkeypatch.setattr(volume, "NOW", datetime(2026, 10, 21, 0, 25, tzinfo=timezone.utc))
    monkeypatch.setattr(volume, "frame", frame)
    # Begin after scan+enrollment, then 3Hz for the remainder of the exact window.
    plan, study, observed, retained, counts, buffer, now = await volume.collect_volume(
        tmp_path, monkeypatch, plan_factory=plan_factory, cycles=1257)
    assert (plan.end_at - plan.start_at).total_seconds() == 1260
    assert observed["complete"] and observed["dropped_records"] == 0
    assert counts["ws_quote"] == 16 * 1257
    assert counts["market_state"] == 15 * 1257 + 10
    assert not buffer.memory_exhausted and retained <= 512 * 1024 * 1024
    report = build_observed_regular_flow_report(plan.study_bytes, observed,
        study_sha256=plan.study_sha256, analysis_as_of=now.isoformat())
    assert report["counts"] == {"denominator": 7, "eligible_pairs": 5}
    assert len(report["strict_report"]["candidates"]) == 7
    assert report["account_return"] is report["net_pnl"] is None
    assert report["production_eligible"] is False
    capacity = assess_pregate_capture_resources(study, operational_registrations=2,
                                                external_registrations=0)
    assert capacity["numeric_configuration_passed"] and not capacity["installation_ready"]
    print("OCT21_SYNTHETIC_VOLUME=" + json.dumps({
        "records": len(observed["records"]), "retained_record_bytes": retained,
        "journal_bytes": Path(plan.settings["journal_path"]).stat().st_size,
        "counts": dict(counts), "eligible_pairs": report["counts"]["eligible_pairs"],
        "live_volume_qualified": False}, sort_keys=True))
