"""End-to-end nonempty readout regression using only synthetic capture evidence."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "ops" / "run_capture_readout.py"


def _runner():
    spec = importlib.util.spec_from_file_location("capture_readout_nonempty", RUNNER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
@pytest.mark.parametrize("include_market_state", [True, False])
async def test_nonempty_v3_llm_capture_survives_all_real_readout_reports(
        tmp_path, monkeypatch, include_market_state):
    """A lost cohort/decision/first-quote link must block this real four-report path."""
    from src.analytics import llm_decision_record as decisions
    from src.analytics.kis_market_state import parse_market_state
    from test_llm_capture_reachability import SYMBOL, add_quote, connected_case
    from test_market_state_capture import STATUS, fields

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    recorder = decisions.DecisionRecorder(tmp_path / "synthetic-decisions", enabled=True)
    monkeypatch.setattr(decisions, "_recorder", recorder)
    case = None
    try:
        case = await connected_case(tmp_path, monkeypatch, selection_v3=True)
        # Keep the real subscription coordinator and parser: this is received
        # market-state evidence, not an inferred regular-session assertion.
        if include_market_state:
            case.clock[0] = datetime.fromisoformat("2026-10-16T09:30:01+09:00")
            case.subscriptions.note_market_state((STATUS, SYMBOL), case.subscriptions.generation)
            assert case.subscriptions.publish_market_state(dict(
                parse_market_state(STATUS, fields(STATUS, SYMBOL)), kind="market_state", symbol=SYMBOL,
                observed_at=case.clock[0].isoformat(), received_at=case.clock[0].isoformat(),
                connection_id=case.subscriptions.connection_id, generation=case.subscriptions.generation))
        add_quote(case, "synthetic-entry", "2026-10-16T09:30:02+09:00", bid="9990")
        add_quote(case, "synthetic-too-early", "2026-10-16T09:45:01+09:00", bid="12000")
        add_quote(case, "synthetic-markout", "2026-10-16T09:45:02+09:00", bid="10040")
        case.clock[0] = case.plan.end_at
        await case.journal.close()
        await case.subscriptions.stop()
        assert recorder.close()

        decisions_path = next(recorder.directory.glob("*.jsonl"))
        original_inputs = (case.plan.study_path.read_bytes(),
                           Path(case.plan.settings["journal_path"]).read_bytes(),
                           decisions_path.read_bytes())
        runner = _runner()
        study = case.plan.study_path
        ledger = Path(case.plan.settings["journal_path"])
        manifest = {
            "version": "deterministic-capture-readout-v1",
            "study": str(study), "study_sha256": hashlib.sha256(study.read_bytes()).hexdigest(),
            "ledger": str(ledger), "decisions": str(decisions_path),
            "output_dir": str(tmp_path / "private-readout"),
            "not_before": "2026-10-16T09:57:00+09:00",
            "not_after": "2026-10-16T10:10:00+09:00",
            "source_sha256": runner.source_fingerprint(),
        }
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(json.dumps(manifest))
        result = runner.run(manifest_path, now=lambda: datetime(2026, 10, 16, 0, 58, tzinfo=timezone.utc))

        assert result["status"] == "diagnostics_complete", result
        assert [command["name"] for command in result["commands"]] == [
            "readiness", "gate-trace", "frame-diagnostics", "evaluation-unreviewed"]
        assert all(command["returncode"] == 0 and command["valid_json"] for command in result["commands"])
        output = Path(manifest["output_dir"])
        readiness = json.loads((output / "readiness.json").read_text())
        evaluation = json.loads((output / "evaluation-unreviewed.json").read_text())

        selection = evaluation["selection_basis"]
        assert selection["capture_complete"] is True
        assert selection["candidates"][0]["basis"]["modifier_trace_complete"] is True
        assert len(selection["candidates"][0]["basis"]["modifier_stages"]) == 24
        assert readiness["decisions"]["matched"] == 1
        assert readiness["decisions"]["missing_signal_ids"] == []
        quote_join = readiness["quote_join"]["rows"][0]
        assert quote_join["approved"] is True
        assert quote_join["first_quote_after"]["quote_id"] == "synthetic-entry"
        assert quote_join["first_quote_after"]["received_at"] >= quote_join["decision_available_at"]
        if include_market_state:
            assert quote_join["evaluable"] is True
            assert quote_join["market_state_at_quote"][STATUS]["state_verdict"] == "UNKNOWN"
        else:
            assert quote_join["evaluable"] is False
            assert "market_state_unknown" in quote_join["reasons"]
        task = evaluation["review_tasks"][0]
        assert task["entry_quote"]["quote_id"] == "synthetic-entry"
        assert task["markout_quote"]["quote_id"] == "synthetic-markout"
        assert evaluation["original"]["payload"]["markout_policy"]["horizon_seconds"] == 900
        assert task["entry_quote"]["record"]["provenance"]["received_at"] >= task["entry_quote"]["record"]["observed_at"]
        assert task["markout_quote"]["record"]["observed_at"] == "2026-10-16T09:45:02+09:00"
        assert task["markout_quote"]["record"]["bid"] == "10040"
        assert (datetime.fromisoformat(task["markout_quote"]["record"]["observed_at"])
                - datetime.fromisoformat(task["entry_quote"]["record"]["observed_at"])) == timedelta(seconds=900)
        assert evaluation["readiness"]["status"] == "no_comparable_pairs"
        assert set(task["reasons"]) >= {"MISSING_ENTRY_SESSION_REVIEW", "MISSING_MARKOUT_SESSION_REVIEW"}
        assert evaluation["production_eligible"] is False
        assert evaluation["account_return"] is None
        assert result["profitability_pass"] is False
        assert result["production_eligible"] is False
        assert result["account_return"] is None
        assert result["decision_recording_finality"] == "unverified"
        assert original_inputs == (case.plan.study_path.read_bytes(), ledger.read_bytes(), decisions_path.read_bytes())
    finally:
        if case is not None:
            await case.journal.close()
            await case.subscriptions.stop()
        if not recorder._closed.is_set():
            recorder.close()
