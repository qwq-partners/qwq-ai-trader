"""Offline report-slot readout is bound to one saved morning cohort."""
from __future__ import annotations

import json
import subprocess
import sys
import asyncio
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo


KST = ZoneInfo("Asia/Seoul")
PLAN_HASH = "a" * 64


def cohort(day, members=((1, "005930"), (2, "000660"))):
    from src.data.providers.toss.report_protocol import ReportCohort

    return ReportCohort.create(day, datetime(day.year, day.month, day.day, 8, 25, tzinfo=KST), members)


def policy(day):
    return {"dataset_kind": "synthetic", "sessions": [{"name": "after", "start": "16:00", "end": "16:05"}],
            "selection": {"max_symbols": 20}, "limits": {"job_timeout_seconds": 10, "max_retries": 0, "max_pages": 1},
            "comparison": {"min_valid_pairs": 1, "outlier_pct": 5, "max_age_seconds": 120,
                           "max_skew_seconds": 30, "expected_market_basis": "krx"}}


def snapshot(value, selected_at):
    from src.data.providers.toss.report_protocol import report_snapshot

    return report_snapshot(value, now=selected_at, policy=policy(value.report_date))


def reserve(ledger, value, slot_id, *, finish=True):
    selected_at = datetime.fromisoformat(slot_id) + timedelta(seconds=2)
    snap = snapshot(value, selected_at)
    if not finish:
        ledger.reserve_slot(slot_id, kind="prices", snapshot=snap)
        return

    from src.data.providers.toss.observation import ObservationRunner

    class Client:
        async def get(self, path, *, params, budget):
            assert path == "/api/v1/prices"
            return {"result": [{"symbol": symbol, "lastPrice": "101", "currency": "KRW"}
                                for symbol in params["symbols"].split(",")]}

    result = asyncio.run(ObservationRunner(client=Client(), ledger=ledger, policy=policy(value.report_date), now=lambda: selected_at)
                         .prices(slot_id=slot_id, snapshot=snap))
    assert result.outcome == "failure" and result.excluded_pairs == len(value.members)


def test_readout_uses_only_exact_cohort_slot_and_preserves_rank_order(tmp_path):
    from src.data.providers.toss.observation_ledger import ObservationLedger

    target = cohort(date(2026, 10, 24))
    foreign = cohort(date(2026, 10, 23), ((1, "035420"),))
    ledger = ObservationLedger(tmp_path / "ledger", plan_hash=PLAN_HASH, max_bytes=100_000)
    ledger.open()
    reserve(ledger, foreign, "2026-10-23T16:00:00+09:00")
    reserve(ledger, target, "2026-10-24T16:00:00+09:00")
    reserve(ledger, target, "2026-10-24T16:04:00+09:00")
    ledger.close()

    report = ObservationLedger.read_only_report_slot(
        ledger.path, plan_hash=PLAN_HASH, max_bytes=100_000, cohort=target,
        slot_id="2026-10-24T16:00:00+09:00")

    assert report["error_code"] is None and not report["incomplete"]
    assert [(row["rank"], row["symbol"]) for row in report["members"]] == list(target.members)
    assert [row["reason"] for row in report["members"]] == ["excluded", "excluded"]
    assert report["members"][0]["quote"] == {
        "price": "101", "observed_at": None,
        "fetched_at": "2026-10-24T16:00:02+09:00", "status": "partial",
        "market_basis": "unknown", "currency": "KRW",
    }


def test_readout_rejects_missing_mismatched_unfinished_and_corrupt_targets_without_mutation(tmp_path):
    from src.data.providers.toss.observation_ledger import ObservationLedger

    target = cohort(date(2026, 10, 24))
    ledger = ObservationLedger(tmp_path / "ledger", plan_hash=PLAN_HASH, max_bytes=100_000)
    ledger.open()
    reserve(ledger, target, "2026-10-24T16:00:00+09:00", finish=False)
    ledger.close()
    before = ledger.path.read_bytes()

    unfinished = ObservationLedger.read_only_report_slot(
        ledger.path, plan_hash=PLAN_HASH, max_bytes=100_000, cohort=target,
        slot_id="2026-10-24T16:00:00+09:00")
    empty = ObservationLedger(tmp_path / "empty", plan_hash=PLAN_HASH, max_bytes=100_000)
    empty.open(); empty.close()
    absent = ObservationLedger.read_only_report_slot(
        empty.path, plan_hash=PLAN_HASH, max_bytes=100_000, cohort=target,
        slot_id="2026-10-24T16:00:00+09:00")
    assert (unfinished["incomplete"], unfinished["error_code"], unfinished["successful_attempts"]) == (True, "ledger_incomplete", 0)
    assert (absent["incomplete"], absent["error_code"], absent["successful_attempts"]) == (True, "report_slot_unavailable", 0)
    assert ledger.path.read_bytes() == before

    wrong = cohort(date(2026, 10, 24), ((1, "035420"),))
    other = ObservationLedger(tmp_path / "other", plan_hash=PLAN_HASH, max_bytes=100_000)
    other.open()
    reserve(other, wrong, "2026-10-24T16:00:00+09:00")
    other.close()
    mismatched = ObservationLedger.read_only_report_slot(
        other.path, plan_hash=PLAN_HASH, max_bytes=100_000, cohort=target,
        slot_id="2026-10-24T16:00:00+09:00")
    missing_file = ObservationLedger.read_only_report_slot(
        tmp_path / "missing", plan_hash=PLAN_HASH, max_bytes=100_000, cohort=target,
        slot_id="2026-10-24T16:00:00+09:00")
    assert (mismatched["incomplete"], mismatched["error_code"], mismatched["successful_attempts"]) == (True, "report_slot_mismatch", 0)
    assert (missing_file["incomplete"], missing_file["error_code"]) == (True, "ledger_unavailable")

    complete = ObservationLedger(tmp_path / "complete", plan_hash=PLAN_HASH, max_bytes=100_000)
    complete.open()
    reserve(complete, target, "2026-10-24T16:00:00+09:00")
    reserve(complete, wrong, "2026-10-24T16:04:00+09:00", finish=False)
    complete.close()
    foreign_unfinished = ObservationLedger.read_only_report_slot(
        complete.path, plan_hash=PLAN_HASH, max_bytes=100_000, cohort=target,
        slot_id="2026-10-24T16:00:00+09:00")
    assert (foreign_unfinished["incomplete"], foreign_unfinished["error_code"], foreign_unfinished["successful_attempts"]) == (True, "ledger_incomplete", 0)

    ledger.path.write_bytes(before + b"not-json\n")
    corrupt = ObservationLedger.read_only_report_slot(
        ledger.path, plan_hash=PLAN_HASH, max_bytes=100_000, cohort=target,
        slot_id="2026-10-24T16:00:00+09:00")
    assert corrupt["incomplete"] and corrupt["error_code"] == "ledger_corrupt"


def test_readout_rejects_excluded_quote_for_the_wrong_symbol(tmp_path):
    from src.data.providers.toss.observation_ledger import ObservationLedger

    target = cohort(date(2026, 10, 24), ((1, "005930"),))
    slot_id = "2026-10-24T16:00:00+09:00"
    ledger = ObservationLedger(tmp_path / "ledger", plan_hash=PLAN_HASH, max_bytes=100_000)
    ledger.open()
    attempt, = ledger.reserve_slot(slot_id, kind="prices", snapshot=snapshot(target, datetime.fromisoformat(slot_id) + timedelta(seconds=2)))
    ledger.begin(attempt)
    ledger.finish(attempt, reason="excluded", observation={
        "valid_pairs": 0, "excluded_pairs": 1, "kis": None,
        "toss": {"symbol": "035420", "price": "101", "observed_at": None,
                 "fetched_at": slot_id, "status": "partial", "missing_fields": ["observed_at"],
                 "market_basis": "unknown", "currency": "KRW"},
        "comparison": {"valid": False, "reason": "missing_quote", "pair_id": None,
                       "delta_pct": None, "outlier": None}, "evaluated_at": slot_id,
    })
    ledger.close()

    result = ObservationLedger.read_only_report_slot(
        ledger.path, plan_hash=PLAN_HASH, max_bytes=100_000, cohort=target, slot_id=slot_id)
    assert (result["incomplete"], result["error_code"], result["successful_attempts"]) == (True, "report_slot_mismatch", 0)


def test_cli_requires_matching_morning_document_and_report_slot(tmp_path):
    from src.data.providers.toss.observation_ledger import ObservationLedger

    target = cohort(date(2026, 10, 24))
    ledger = ObservationLedger(tmp_path / "ledger", plan_hash=PLAN_HASH, max_bytes=100_000)
    ledger.open()
    reserve(ledger, target, "2026-10-24T16:00:00+09:00")
    ledger.close()
    morning = tmp_path / "morning.json"
    morning.write_text(json.dumps({"date": "2026-10-24", "stocks": [
        {"rank": rank, "symbol": symbol} for rank, symbol in target.members],
        "report_cohort": target.to_document()}))
    before = ledger.path.read_bytes()
    command = [sys.executable, "scripts/review_toss_observation.py", "--ledger", str(ledger.path),
               "--plan-hash", PLAN_HASH, "--max-bytes", "100000", "--report-cohort", str(morning),
               "--slot-id", "2026-10-24T16:00:00+09:00"]
    result = subprocess.run(command, cwd=".", capture_output=True, text=True)
    assert result.returncode == 0
    assert json.loads(result.stdout)["cohort_id"] == target.cohort_id
    assert ledger.path.read_bytes() == before

    malformed = dict(json.loads(morning.read_text()))
    malformed["stocks"].reverse()
    morning.write_text(json.dumps(malformed))
    rejected = subprocess.run(command, cwd=".", capture_output=True, text=True)
    assert rejected.returncode == 2
    assert json.loads(rejected.stdout)["error_code"] == "invalid_report_input"

    morning.write_text(json.dumps({"date": "2026-10-24", "stocks": []}))
    no_candidates = subprocess.run(command, cwd=".", capture_output=True, text=True)
    assert no_candidates.returncode == 0
    assert json.loads(no_candidates.stdout)["error_code"] == "no_candidates"
