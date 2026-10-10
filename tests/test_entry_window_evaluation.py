"""Fixed-window aggregation is offline-only; bundle outputs below are synthetic."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import json
import asyncio
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

KST = ZoneInfo("Asia/Seoul")


def _protocol(*, phase="diagnostic", dates=None):
    dates = dates or [f"2026-10-{day:02d}" for day in range(6, 16)]
    return {
        "version": "entry-window-protocol-v1",
        "phase": phase,
        "dataset_kind": "synthetic",
        "fixed_at": "2026-10-05T12:00:00+09:00",
        "evaluation_epoch": "epoch-1",
        "calendar": {"source_ref": "fixed-test-calendar", "source_sha256": "a" * 64,
                     "scheduled_dates": dates},
    }


def _day(day):
    study = {"dataset_kind": "synthetic", "evaluation_epoch": "epoch-1",
             "as_of": f"{day}T15:30:00+09:00",
             "markout_policy": {"fixed_at": "2026-10-01T12:00:00+09:00", "version": "m1"},
             "capital_policy": {"fixed_at": "2026-10-01T12:00:00+09:00", "version": "c1"}}
    return {"date": day, "study_utf8": json.dumps(study, sort_keys=True),
            "observations": {"complete": True, "dropped_records": 0, "incomplete_reasons": [],
                             "journal": {"capture_id": f"capture-{day}", "sealed": True,
                                            "capture_closed_at": f"{day}T15:31:00+09:00",
                                            "scope": "sealed_window", "structurally_valid": True,
                                            "durability_confirmed": True,
                                            "dropped_count_exact": True,
                                            "persistence_dropped_records": 0},
                             "records": [{"kind": "scan", "observed_at": f"{day}T09:15:00+09:00"}]}}


def _bundle(day, *, unknown=False, empty=False, policy="policy-1"):
    rows = [] if empty else [{"opportunity_id": f"{day}-{rank}", "symbol": f"S{rank}",
                              "gate_status": "unknown" if unknown and rank == 1 else "allow",
                              "a_quantity": 1, "entry_price_proxy": "100",
                              "a_net_pnl": "10", "b_net_pnl": "20", "delta_net_pnl": "10"}
                             for rank in range(1, 4)]
    tasks = [{"opportunity_id": r["opportunity_id"], "returned_rank": n}
             for n, r in enumerate(rows, 1)]
    report = {"opportunities": rows, "fees": {"buy_commission_rate": "0",
                                                   "sell_commission_rate": "0", "sell_tax_rate": "0"}}
    return {"version": "entry-evaluation-bundle-v1", "dataset_kind": "synthetic",
            "binding": {"capture_id": f"capture-{day}", "evaluation_epoch": "epoch-1"},
            "review_tasks": tasks, "original": {"report": report},
            "sensitivity": [{"slippage_bps_each": bps, "result": {"report": deepcopy(report)}}
                            for bps in (0, 10, 30)]}


def _install(monkeypatch, *, unknown=False, empty_dates=()):
    import src.analytics.entry_window_evaluation as mod

    def fake(study, observations, *, study_sha256, session_review=None, analysis_as_of=None):
        day = observations["day"]
        return _bundle(day, unknown=unknown, empty=day in empty_dates)

    monkeypatch.setattr(mod, "build_evaluation_bundle", fake)


def _days(protocol):
    result = [_day(day) for day in protocol["calendar"]["scheduled_dates"]]
    for day in result:
        day["observations"]["day"] = day["date"]
    return result


def _final_as_of():
    return datetime(2026, 11, 3, 15, 31, tzinfo=KST)


def _replace_date(value, old="2026-10-02", new="2026-10-06"):
    if isinstance(value, dict):
        return {key: _replace_date(item, old, new) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_date(item, old, new) for item in value]
    return value.replace(old, new) if isinstance(value, str) else value


def _real_day(day_text="2026-10-06"):
    """A fully real bundle path, derived from the checked-in synthetic rehearsal."""
    saved = json.loads((Path(__file__).parents[1] / "docs/research/current-engine-evaluation-rehearsal-2026-10-01.json").read_text())
    context = _replace_date(saved["synthetic_input"]["context"], new=day_text)
    observations = _replace_date(saved["synthetic_input"]["observations"], new=day_text)
    observations.update(complete=True, dropped_records=0, incomplete_reasons=[])
    for record in observations["records"]:
        if record.get("kind") == "order_ready":
            record["capital_snapshot_ref"] = hashlib.sha256(
                json.dumps(record["capital_snapshot"], sort_keys=True, allow_nan=False).encode()).hexdigest()
    raw = (json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
    sha = hashlib.sha256(raw).hexdigest()
    observations["journal"]["study_sha256"] = sha
    observations["journal"]["capture_id"] = "synthetic-capture-" + day_text
    from src.analytics.entry_evaluation_bundle import _digest
    review = {"version": "entry-session-review-v1", "dataset_kind": context["dataset_kind"],
              "binding": {"study_sha256": sha, "observation_sha256": _digest(observations),
                          "capture_id": observations["journal"]["capture_id"],
                          "evaluation_epoch": context["evaluation_epoch"]}, "quotes": []}
    for quote in observations["records"]:
        if quote["kind"] == "ws_quote":
            review["quotes"].append({"quote_id": quote["quote_id"], "symbol": quote["symbol"],
                "record_sha256": _digest(quote), "session": "KRX_REGULAR_CONTINUOUS",
                "basis": "external_event_history_review", "source_ref": "synthetic-state-events",
                "source_sha256": "a" * 64, "reviewer_ref": "synthetic-reviewer",
                "reviewed_at": f"{day_text}T10:00:00+09:00",
                "valid_from": f"{day_text}T09:00:00+09:00",
                "valid_until": f"{day_text}T09:45:00+09:00"})
    return {"date": day_text, "study_utf8": raw.decode(), "observations": observations,
            "session_review": review}, context["evaluation_epoch"]


def test_real_bundle_pipeline_binds_study_date_and_preserves_selected_pair_counts():
    from src.analytics.entry_window_evaluation import build_window_report
    day, epoch = _real_day(); protocol = _protocol(dates=["2026-10-06"])
    protocol["evaluation_epoch"] = epoch
    out = build_window_report(protocol, [day], as_of=_final_as_of())
    assert out["economic_verdict"] == "insufficient_data"
    assert out["original_total_candidates"] == 4
    assert out["selected_unknown_candidates"] == 0
    assert out["scenarios"]["0"]["price_pairs"] == 3
    assert out["scenarios"]["0"]["daily_y"] == [{"date": "2026-10-06",
        "delta": "-0.01510855146946094213476779918", "b_net": "0.01586844508435485698668853027"}]


def test_multiple_real_bundles_have_distinct_bound_studies_and_captures():
    from src.analytics.entry_window_evaluation import build_window_report
    first, epoch = _real_day("2026-10-06"); second, _ = _real_day("2026-10-07")
    protocol = _protocol(dates=["2026-10-06", "2026-10-07"]); protocol["evaluation_epoch"] = epoch
    out = build_window_report(protocol, [first, second], as_of=_final_as_of())
    assert out["economic_verdict"] == "insufficient_data"
    assert out["scenarios"]["0"]["price_pairs"] == 6
    assert [item["capture_id"] for item in out["days"]] == ["synthetic-capture-2026-10-06", "synthetic-capture-2026-10-07"]


def test_capture_policy_keeps_source_selection_and_channels_but_allows_window_instance_fields(monkeypatch):
    from src.analytics.entry_window_evaluation import build_window_report
    protocol = _protocol(dates=["2026-10-06", "2026-10-07"]); _install(monkeypatch)
    days = _days(protocol)
    for item in days:
        study = json.loads(item["study_utf8"])
        day = item["date"]
        study["capture"] = {"version": "capture-v1", "scan_admission_ref": "scan-v1",
            "source_version_ref": "source-v1", "configuration_ref": "config-v1",
            "fee_evidence_ref": "fees-v1", "capital_evidence_ref": "capital-v1",
            "capacity_evidence_ref": "capacity-v1", "buffer_capacity": 10, "queue_capacity": 10,
            "batch_size": 1, "max_bytes": 1000, "max_record_bytes": 100,
            "open_timeout_seconds": 5, "close_timeout_seconds": 5, "channels": ["quote"],
            "selection_basis": "basis-v1", "capture_id": "study-" + day,
            "start_at": f"{day}T09:00:00+09:00", "admission_end_at": f"{day}T09:05:00+09:00",
            "end_at": f"{day}T15:30:00+09:00", "journal_path": f"/tmp/{day}.jsonl", "study_ref": day}
        item["study_utf8"] = json.dumps(study, sort_keys=True)
    assert build_window_report(protocol, days, as_of=_final_as_of())["economic_verdict"] == "insufficient_data"
    changed = deepcopy(days); study = json.loads(changed[1]["study_utf8"])
    study["capture"]["channels"] = ["quote", "changed-prompt-equivalent"]
    changed[1]["study_utf8"] = json.dumps(study, sort_keys=True)
    with pytest.raises(ValueError, match="policy"):
        build_window_report(protocol, changed, as_of=_final_as_of())
    changed = deepcopy(days); study = json.loads(changed[1]["study_utf8"])
    study["capture"]["admission_end_at"] = "2026-10-07T09:06:00+09:00"
    changed[1]["study_utf8"] = json.dumps(study, sort_keys=True)
    with pytest.raises(ValueError, match="policy"):
        build_window_report(protocol, changed, as_of=_final_as_of())


def test_relabelled_pilot_or_reused_capture_cannot_fill_another_calendar_day():
    from src.analytics.entry_window_evaluation import build_window_report
    first, epoch = _real_day("2026-10-06"); protocol = _protocol(dates=["2026-10-06", "2026-10-07"])
    protocol["evaluation_epoch"] = epoch
    relabelled = deepcopy(first); relabelled["date"] = "2026-10-07"
    with pytest.raises(ValueError, match="study window"):
        build_window_report(protocol, [first, relabelled], as_of=_final_as_of())


def test_nonconstant_bootstrap_matches_independent_pcg64_circular_block_calculation():
    from src.analytics.entry_window_evaluation import _bootstrap_indices, _linear_ci
    import numpy as np
    from decimal import Decimal
    values = [Decimal("0"), Decimal("0.1"), Decimal("0.3"), Decimal("-0.2"), Decimal("0.4"), Decimal("0.2")]
    got = _linear_ci(values, _bootstrap_indices(len(values)))
    rng = np.random.Generator(np.random.PCG64(20261001))
    # Literal protocol loop is independent of production's vectorized reshape.
    samples = []
    for _ in range(10000):
        sample = []
        for start in rng.integers(0, len(values), size=2):
            sample.extend(values[(int(start) + offset) % len(values)] for offset in range(5))
        samples.append(float(sum(sample[:len(values)], Decimal(0)) / len(values)))
    expected = np.quantile(samples, [0.025, 0.975], method="linear")
    assert [float(v) for v in got] == pytest.approx(expected, abs=1e-15)


@pytest.mark.parametrize("name", ["capture", "study", "observations"])
def test_duplicate_identity_guards_reject_each_bound_identity(name):
    from src.analytics.entry_window_evaluation import _add_unique
    seen = {"same"}
    with pytest.raises(ValueError, match="duplicate " + name):
        _add_unique(seen, "same", name)


def test_complete_fixed_window_uses_all_calendar_days_and_shared_deterministic_bootstrap(monkeypatch):
    from src.analytics.entry_window_evaluation import build_window_report
    protocol = _protocol(); _install(monkeypatch)
    before = deepcopy(protocol); days = _days(protocol); before_days = deepcopy(days)
    first = build_window_report(protocol, days, as_of=_final_as_of())
    second = build_window_report(protocol, days, as_of=_final_as_of())
    assert first == second
    scenario = first["scenarios"]["0"]
    assert scenario["price_pairs"] == 30 and scenario["price_days"] == 10
    assert scenario["mean_delta"] == "0.1" and scenario["mean_b_net"] == "0.2"
    assert scenario["bootstrap"]["seed"] == 20261001
    # The main 30-pair threshold is met, but removing three tied candidates
    # empties the first day and leaves only nine price days for sensitivity.
    assert scenario["research_criteria_met"] is False
    assert first["profitability_pass"] is False and first["production_eligible"] is False
    assert protocol == before and days == before_days


def test_unknown_selected_candidate_suppresses_economic_verdict_but_reports_cohort_unknown(monkeypatch):
    from src.analytics.entry_window_evaluation import build_window_report
    protocol = _protocol(); _install(monkeypatch, unknown=True)
    out = build_window_report(protocol, _days(protocol), as_of=_final_as_of())
    assert out["economic_verdict"] == "suppressed"
    assert out["scenarios"]["0"]["mean_delta"] is None
    assert out["whole_cohort_unknown_candidates"] == 10


def test_cost_specific_unknowns_use_unique_union_and_suppress_all_economics(monkeypatch):
    import src.analytics.entry_window_evaluation as mod
    protocol = _protocol(); _install(monkeypatch)
    builder = mod.build_evaluation_bundle
    def with_cost_unknowns(*args, **kwargs):
        bundle = builder(*args, **kwargs)
        for index, bps in enumerate((0, 30)):
            report = next(s["result"]["report"] for s in bundle["sensitivity"] if s["slippage_bps_each"] == bps)
            report["opportunities"][index]["gate_status"] = "unknown"
        return bundle
    monkeypatch.setattr(mod, "build_evaluation_bundle", with_cost_unknowns)
    out = mod.build_window_report(protocol, _days(protocol), as_of=_final_as_of())
    assert out["selected_unknown_candidates"] == 20
    assert [out["scenarios"][str(bps)]["selected_unknown_candidates"] for bps in (0, 10, 30)] == [10, 0, 10]
    assert all(s["mean_delta"] is None and s["bootstrap"] is None for s in out["scenarios"].values())


@pytest.mark.parametrize("zero_field,expected", [
    (None, "conditional_research_criteria_met"),
    ("delta_net_pnl", "improvement_evidence_not_met"),
    ("b_net_pnl", "improvement_evidence_not_met"),
])
def test_sufficient_sample_requires_both_improvement_and_standalone_profit(monkeypatch, zero_field, expected):
    import src.analytics.entry_window_evaluation as mod
    protocol = _protocol(dates=[f"2026-10-{day:02d}" for day in range(6, 17)])
    _install(monkeypatch); builder = mod.build_evaluation_bundle
    def set_outcomes(*args, **kwargs):
        bundle = builder(*args, **kwargs)
        if zero_field:
            for scenario in bundle["sensitivity"]:
                for row in scenario["result"]["report"]["opportunities"]:
                    row[zero_field] = "0"
        return bundle
    monkeypatch.setattr(mod, "build_evaluation_bundle", set_outcomes)
    out = mod.build_window_report(protocol, _days(protocol), as_of=_final_as_of())
    assert out["economic_verdict"] == expected
    assert out["production_eligible"] is False and out["profitability_pass"] is False


@pytest.mark.parametrize("field", ["study", "record", "review", "close"])
def test_final_report_cannot_use_evidence_from_after_its_as_of(field):
    from src.analytics.entry_window_evaluation import build_window_report
    from src.analytics.entry_evaluation_bundle import _digest
    day, epoch = _real_day("2026-11-03")
    protocol = _protocol(dates=[day["date"]]); protocol["evaluation_epoch"] = epoch
    future = "2026-11-03T23:00:00+09:00"
    if field == "study":
        study = json.loads(day["study_utf8"]); study["as_of"] = future
        day["study_utf8"] = json.dumps(study)
    elif field == "record": day["observations"]["records"][0]["observed_at"] = future
    elif field == "review": day["session_review"]["quotes"][0]["reviewed_at"] = future
    else:
        day["observations"]["journal"]["capture_closed_at"] = future
        day["session_review"]["binding"]["observation_sha256"] = _digest(day["observations"])
        out = build_window_report(protocol, [day], as_of=_final_as_of())
        assert out["economic_verdict"] == "suppressed" and out["quality_failures"] == [day["date"]]
        return
    with pytest.raises(ValueError, match="after report as_of"):
        build_window_report(protocol, [day], as_of=_final_as_of())


def test_verified_empty_scan_is_zero_but_unsealed_or_dropped_day_suppresses(monkeypatch):
    from src.analytics.entry_window_evaluation import build_window_report
    protocol = _protocol(); _install(monkeypatch, empty_dates={"2026-10-06"})
    out = build_window_report(protocol, _days(protocol), as_of=_final_as_of())
    assert out["days"][0]["status"] == "empty_scan_unverified"
    assert out["economic_verdict"] == "suppressed"
    assert out["scenarios"]["0"]["price_pairs"] == 27
    days = _days(protocol); days[0]["observations"]["journal"]["sealed"] = False
    out = build_window_report(protocol, days, as_of=_final_as_of())
    assert out["economic_verdict"] == "suppressed"


def test_structured_completed_source_snapshot_is_required_to_verify_empty_scan():
    from src.analytics.entry_window_evaluation import _verified_empty_scan
    from src.analytics.selection_source_status import SourceCapture, snapshot_fields
    capture = SourceCapture()
    async def complete_empty():
        from src.analytics.selection_source_status import mark_source
        mark_source("payload", count=0)
        return []
    asyncio.run(capture.call("kis_volume_surge", complete_empty))
    snapshot = capture.wrap([]).selection_sources
    scan = {"observed_at": snapshot["completed_at"], "selection_sources_expected": True,
            "selection_basis_expected": True, "selection_sources_status": "observed", **snapshot_fields(snapshot)}
    assert _verified_empty_scan(scan)
    scan["selection_sources_status"] = "unavailable"
    assert not _verified_empty_scan(scan)


def test_policy_epoch_date_and_phase_mixing_are_rejected(monkeypatch):
    from src.analytics.entry_window_evaluation import build_window_report
    protocol = _protocol(); _install(monkeypatch)
    changed = _days(protocol)
    study = json.loads(changed[1]["study_utf8"]); study["markout_policy"]["version"] = "changed"
    changed[1]["study_utf8"] = json.dumps(study, sort_keys=True)
    with pytest.raises(ValueError, match="policy"):
        build_window_report(protocol, changed, as_of=_final_as_of())
    epoch_changed = _days(protocol); study = json.loads(epoch_changed[0]["study_utf8"])
    study["evaluation_epoch"] = "other-epoch"; epoch_changed[0]["study_utf8"] = json.dumps(study, sort_keys=True)
    with pytest.raises(ValueError, match="epoch"):
        build_window_report(protocol, epoch_changed, as_of=_final_as_of())
    protocol = _protocol(phase="confirmation")
    with pytest.raises(ValueError, match="outside phase"):
        build_window_report(protocol, _days(protocol), as_of=datetime(2026, 12, 1, 16, tzinfo=KST))
    protocol = _protocol(); _install(monkeypatch)
    missing = build_window_report(protocol, _days(protocol)[:-1], as_of=_final_as_of())
    assert missing["days"][-1]["status"] == "planned_missing"
    assert missing["economic_verdict"] == "suppressed"


def test_early_end_is_quality_only_and_top_three_removal_recomputes_full_day_mean(monkeypatch):
    from src.analytics.entry_window_evaluation import build_window_report
    protocol = _protocol(); _install(monkeypatch)
    early = build_window_report(protocol, _days(protocol), as_of=datetime(2026, 11, 3, 15, 29, tzinfo=KST))
    assert early["evaluation_status"] == "quality_only"
    assert early["economic_verdict"] == "suppressed"
    assert early["scenarios"]["0"]["daily_y"] == [
        {"date": day, "delta": None, "b_net": None} for day in protocol["calendar"]["scheduled_dates"]]
    assert early["scenarios"]["0"]["top3_positive_removed"] == {
        "status": "quality_only", "removed": [], "price_pairs": None, "price_days": None, "mean_delta": None}
    final = build_window_report(protocol, _days(protocol), as_of=_final_as_of())
    sensitivity = final["scenarios"]["0"]["top3_positive_removed"]
    assert sensitivity["price_pairs"] == 27 and sensitivity["price_days"] == 9
    assert sensitivity["mean_delta"] == "0.09"


@pytest.mark.parametrize("contents", ['{"protocol":NaN}', '{"protocol":1,"protocol":2}'])
def test_cli_rejects_nonfinite_or_duplicate_json_without_mutation(tmp_path, capsys, contents):
    from scripts.evaluate_entry_window import main
    source = tmp_path / "window.json"; source.write_text(contents)
    before = source.read_bytes()
    assert main(["--input", str(source), "--max-input-bytes", "1048576"]) == 2
    assert capsys.readouterr().out == ""
    assert source.read_bytes() == before


def test_cli_rejects_symlink_input(tmp_path, capsys):
    from scripts.evaluate_entry_window import main
    target = tmp_path / "target.json"; target.write_text(json.dumps({}))
    link = tmp_path / "window.json"; link.symlink_to(target)
    assert main(["--input", str(link), "--max-input-bytes", "1048576"]) == 2
    assert capsys.readouterr().out == ""


def test_cli_accepts_explicit_bounded_aggregate_larger_than_four_mib(tmp_path, capsys):
    from scripts.evaluate_entry_window import main
    day, epoch = _real_day(); protocol = _protocol(dates=["2026-10-06"])
    protocol["evaluation_epoch"] = epoch
    payload = {"protocol": protocol, "days": [day], "as_of": "2026-11-03T15:31:00+09:00"}
    source = tmp_path / "window.json"
    source.write_text(json.dumps(payload) + (" " * (4 * 1024 * 1024)))
    before = source.read_bytes()
    assert main(["--input", str(source), "--max-input-bytes", str(8 * 1024 * 1024)]) == 0
    assert json.loads(capsys.readouterr().out)["economic_verdict"] == "insufficient_data"
    assert source.read_bytes() == before
