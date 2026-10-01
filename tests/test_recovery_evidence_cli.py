"""Explicit temporary files only: full ledger → report path and strict JSON."""

import asyncio
from copy import deepcopy
import json

import pytest

from scripts.reconcile_execution_evidence import main
from src.execution.execution_ledger import ExecutionLedger
from tests.test_recovery_evidence import evidence


def inputs(tmp_path):
    ledger, *sources = evidence()
    path = tmp_path / "ledger.sqlite"

    async def populate():
        store = ExecutionLedger(path, ledger["account_scope"])
        await store.open("s1")
        await store.intent("s1:o1", ledger["orders"]["s1:o1"]["facts"])
        await store.accepted("s1:o1", "000123", "001")
        from decimal import Decimal
        for qty, avg in ((4, "100"), (10, "106")):
            execution = await store.observe("s1:o1", qty, Decimal(avg))
            await store.receipt(execution, "portfolio_applied")
            await store.receipt(execution, "handoff_returned")
        assert await store.close_session()

    asyncio.run(populate())
    args = ["--ledger", str(path), "--account-scope", "test-scope"]
    for name, source in zip(("broker", "journal", "baseline"), sources):
        file = tmp_path / (name + ".json")
        file.write_text(json.dumps(source), encoding="utf-8")
        args += ["--" + name, str(file)]
    return args


def snapshot(tmp_path):
    return {file.name: (file.read_bytes(), file.stat().st_mtime_ns) for file in tmp_path.iterdir()}


def test_cli_reads_real_sqlite_and_json_without_mutation(tmp_path, capsys):
    args = inputs(tmp_path)
    before = snapshot(tmp_path)
    assert main(args) == 0
    result = capsys.readouterr()
    report = json.loads(result.out)
    assert not result.err
    assert report["comparison_status"] == "consistent"
    assert report["inputs"]["ledger"]["event_count"] == 10
    assert all(len(item["file_sha256"]) == 64 for key, item in report["inputs"].items() if key != "ledger")
    assert not report["runtime_release_allowed"]
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("content", ['{"duplicate": 1, "duplicate": 2}', '{"value": NaN}',
                                      '{"value": Infinity}', '[]', '{"unclosed":'])
def test_bad_json_rejected_without_echoing_input(tmp_path, capsys, content):
    args = inputs(tmp_path)
    (tmp_path / "broker.json").write_text(content)
    before = snapshot(tmp_path)
    assert main(args) == 2
    result = capsys.readouterr()
    assert not result.out
    assert "검증 실패" in result.err
    assert content not in result.err
    assert snapshot(tmp_path) == before


def test_missing_ledger_never_creates_database(tmp_path, capsys):
    args = inputs(tmp_path)
    path = tmp_path / "ledger.sqlite"
    path.unlink()
    assert main(args) == 2
    assert not path.exists()
    assert not capsys.readouterr().out


def test_mismatch_is_a_report_not_a_cli_crash(tmp_path, capsys):
    args = inputs(tmp_path)
    file = tmp_path / "journal.json"
    source = json.loads(file.read_text())
    source["rows"].append(deepcopy(source["rows"][0]))
    file.write_text(json.dumps(source))
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["comparison_status"] == "mismatch"
