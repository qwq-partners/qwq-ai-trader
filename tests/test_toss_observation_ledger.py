from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest


def snapshot(symbols=("005930",)):
    return {"snapshot_id": "snap-1", "selected_at": "2026-09-16T09:00:00+09:00",
            "source_success_at": "2026-09-16T08:59:00+09:00", "selection_partial": False,
            "symbols": list(symbols), "kis": {}}


def test_constructor_is_inert_and_reservation_is_durable(tmp_path):
    from src.data.providers.toss.observation_ledger import ObservationLedger

    path = tmp_path / "private" / "ledger.jsonl"
    ledger = ObservationLedger(path, plan_hash="a" * 64, max_bytes=10_000)
    assert not path.exists()
    ledger.open()
    attempts = ledger.reserve_slot("2026-09-16T09:00:00+09:00", kind="prices", snapshot=snapshot())
    assert attempts == ("2026-09-16T09:00:00+09:00:prices:000000",)
    assert path.exists()
    lines = path.read_text().splitlines()
    assert [json.loads(line)["type"] for line in lines] == ["slot"]
    assert all(json.loads(line)["hash"] for line in lines)


def test_reopen_marks_unfinished_without_replaying_or_inventing_symbols(tmp_path):
    from src.data.providers.toss.observation_ledger import ObservationLedger

    path = tmp_path / "ledger.jsonl"
    ledger = ObservationLedger(path, plan_hash="a" * 64, max_bytes=10_000)
    ledger.open()
    ledger.reserve_slot("slot", kind="prices", snapshot=snapshot(("A", "B")))
    reopened = ObservationLedger(path, plan_hash="a" * 64, max_bytes=10_000)
    reopened.open()
    assert reopened.summary()["interrupted"] == 2
    assert reopened.reserve_slot("slot", kind="prices", snapshot=snapshot(("C",))) is None
    assert reopened.summary()["duplicate_submissions"] == 1


def test_corrupt_chain_is_preserved_and_fails_closed(tmp_path):
    from src.data.providers.toss.observation_ledger import LedgerError, ObservationLedger

    path = tmp_path / "ledger.jsonl"
    path.write_text('{"not":"a ledger row"}\n')
    ledger = ObservationLedger(path, plan_hash="a" * 64, max_bytes=10_000)
    with pytest.raises(LedgerError, match="ledger_corrupt"):
        ledger.open()
    assert path.read_text() == '{"not":"a ledger row"}\n'
    assert ledger.summary()["incomplete"] is True


def test_terminal_is_idempotent_and_conflicting_second_terminal_fails(tmp_path):
    from src.data.providers.toss.observation_ledger import LedgerError, ObservationLedger

    ledger = ObservationLedger(tmp_path / "ledger", plan_hash="a" * 64, max_bytes=10_000)
    ledger.open()
    attempt, = ledger.reserve_slot("slot", kind="prices", snapshot=snapshot())
    ledger.begin(attempt)
    ledger.finish(attempt, reason="provider_failure")
    ledger.finish(attempt, reason="provider_failure")
    with pytest.raises(LedgerError, match="ledger_conflict"):
        ledger.finish(attempt, reason="success")
    assert ledger.summary()["terminal_attempts"] == 1


def test_unsafe_or_oversize_file_fails_closed(tmp_path):
    from src.data.providers.toss.observation_ledger import LedgerError, ObservationLedger

    path = tmp_path / "ledger"
    path.write_text("x" * 100)
    with pytest.raises(LedgerError, match="ledger_corrupt"):
        ObservationLedger(path, plan_hash="a" * 64, max_bytes=10).open()
    assert path.exists()


def test_review_cli_is_directly_runnable_and_read_only(tmp_path):
    from src.data.providers.toss.observation_ledger import ObservationLedger
    ledger_path = tmp_path / "ledger"
    ledger = ObservationLedger(ledger_path, plan_hash="a" * 64, max_bytes=10_000); ledger.open()
    attempt, = ledger.reserve_slot("calendar", kind="calendar", snapshot=snapshot(()))
    ledger.finish(attempt, reason="success")
    before = ledger_path.read_bytes()
    command = [sys.executable, "scripts/review_toss_observation.py", "--ledger", str(ledger_path),
               "--plan-hash", "a" * 64, "--max-bytes", "10000"]
    result = subprocess.run(command, cwd=".", capture_output=True, text=True, check=False)
    assert result.returncode == 0 and json.loads(result.stdout)["production_eligible"] is False
    assert ledger_path.read_bytes() == before
