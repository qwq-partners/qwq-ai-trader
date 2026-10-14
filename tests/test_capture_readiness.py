import asyncio
import hashlib
import json
from datetime import timedelta
from pathlib import Path

import pytest


def _decision(signal_id, symbol, strategy, when, decision_id):
    return {
        "schema_version": "llm-decision-v1", "decision_id": decision_id,
        "signal_id": signal_id, "symbol": symbol, "strategy": strategy,
        "stage": "adversarial", "decision_available_at": when,
        "result": {"approved": True, "failed": False, "verdict": "approved"},
    }


def _capture(tmp_path):
    """Create a sealed real LLM-window journal, without sockets or providers."""
    from src.analytics import entry_observation as obs
    import src.analytics.entry_observation_journal as journal
    from src.analytics.entry_observation_runtime import ObservationRuntime
    from test_entry_observation_runtime import load, owner
    from test_llm_window_observation import enter, llm_plan
    from test_signal_window_observation import stock

    async def run():
        plan, _ = load(tmp_path, llm_plan)
        clock = [plan.start_at - timedelta(seconds=1)]
        bot = owner()
        original, observation_clock = journal._timestamp, obs._now
        journal._timestamp = lambda: clock[0].isoformat()
        obs._now = lambda: clock[0].isoformat()
        try:
            runtime = await ObservationRuntime.install(bot, plan, now=lambda: clock[0])
            clock[0] = plan.start_at
            scan = obs.capture_scan(runtime.buffer, [stock("900001"), stock("900002")], "regular")
            enter(runtime.buffer, scan, "sig-a", "900001")
            enter(runtime.buffer, scan, "sig-b", "900002")
            await asyncio.gather(*bot.ws_feed._quote_subscription_owner._tasks)
            clock[0] = plan.end_at - timedelta(microseconds=1)
            await runtime.close()
        finally:
            journal._timestamp = original
            obs._now = observation_clock
        return plan

    plan = asyncio.run(run())
    return plan.study_path, plan.settings["journal_path"], plan.start_at, plan.end_at


def test_report_selects_exact_signal_cohort_and_excludes_unrelated_monthly_rows(tmp_path):
    from src.analytics.capture_readiness import build_capture_readiness

    study, ledger, start, _ = _capture(tmp_path)
    decisions = tmp_path / "decisions.jsonl"
    decisions.write_text("\n".join(json.dumps(row) for row in [
        _decision("sig-a", "900001", "gap_and_go", (start.replace(microsecond=1)).isoformat(), "right"),
        _decision("foreign", "900001", "gap_and_go", (start.replace(microsecond=1)).isoformat(), "wrong"),
    ]) + "\n")

    report = build_capture_readiness(study, ledger, decisions_path=decisions)

    assert report["llm_started_count"] == 2
    assert report["decisions"]["matched"] == 1
    assert report["decisions"]["excluded"] == 1
    assert [row["decision_id"] for row in report["quote_join"]["rows"]] == ["right"]
    assert report["economics"] == {"profitability_pass": False, "account_return": None,
                                   "production_eligible": False}


def test_missing_decision_input_is_not_no_llm_checks(tmp_path):
    from src.analytics.capture_readiness import build_capture_readiness

    study, ledger, _, _ = _capture(tmp_path)
    report = build_capture_readiness(study, ledger)

    assert report["decision_input_status"] == "decision_input_missing"
    assert report["llm_started_count"] == 2
    assert report["status"] != "no_llm_checks"
    assert report["decision_recording_finality"] == "unverified"
    assert report["decisions"]["missing_signal_ids"] == ["sig-a", "sig-b"]
    assert report["decision_snapshot_unsettled"] is True


def test_decision_before_started_and_duplicate_id_are_diagnostic_not_selected(tmp_path):
    from src.analytics.capture_readiness import build_capture_readiness

    study, ledger, start, _ = _capture(tmp_path)
    decisions = tmp_path / "decisions.jsonl"
    decisions.write_text("\n".join(json.dumps(row) for row in [
        _decision("sig-a", "900001", "gap_and_go", (start.replace(microsecond=1)).isoformat(), "same"),
        _decision("sig-a", "900001", "gap_and_go", (start.replace(microsecond=2)).isoformat(), "same"),
        _decision("sig-b", "900002", "gap_and_go", (start - timedelta(microseconds=1)).isoformat(), "early"),
    ]) + "\n")

    report = build_capture_readiness(study, ledger, decisions_path=decisions)

    assert report["decisions"]["matched"] == 0
    assert report["decisions"]["ambiguous_decision_ids"] == ["same"]
    assert report["decisions"]["invalid"]["decision_before_started"] == 1


def test_late_and_identity_mismatched_decisions_remain_unsettled(tmp_path):
    from src.analytics.capture_readiness import build_capture_readiness

    study, ledger, start, end = _capture(tmp_path)
    decisions = tmp_path / "decisions.jsonl"
    decisions.write_text("\n".join(json.dumps(row) for row in [
        _decision("sig-a", "900001", "gap_and_go", end.isoformat(), "late"),
        _decision("sig-b", "900999", "gap_and_go", (start.replace(microsecond=1)).isoformat(), "mismatch"),
    ]) + "\n")

    report = build_capture_readiness(study, ledger, decisions_path=decisions)

    assert report["decisions"]["matched"] == 0
    assert report["decisions"]["late_decision_ids"] == ["late"]
    assert report["decisions"]["invalid"]["decision_identity_mismatch"] == 1
    assert report["decision_snapshot_unsettled"] is True
    assert report["quote_join"]["summary"]["evaluable"] == 0


def test_partial_or_wrong_study_inputs_fail_closed_and_unsealed_is_not_technical_ready(tmp_path):
    from src.analytics.capture_readiness import build_capture_readiness

    study, ledger, _, _ = _capture(tmp_path)
    partial = tmp_path / "partial.jsonl"
    partial.write_text('{"schema_version":"llm-decision-v1"}')
    with pytest.raises(ValueError, match="partial"):
        build_capture_readiness(study, ledger, decisions_path=partial)

    changed = tmp_path / "changed-study.json"
    changed.write_bytes(study.read_bytes() + b" ")
    with pytest.raises(ValueError):
        build_capture_readiness(changed, ledger)

    unsealed = tmp_path / "unsealed.jsonl"
    lines = Path(ledger).read_bytes().splitlines(keepends=True)
    unsealed.write_bytes(b"".join(lines[:-1]))
    report = build_capture_readiness(study, unsealed)
    assert report["journal"]["complete"] is False
    assert report["technical"]["technical_ready"] is False
