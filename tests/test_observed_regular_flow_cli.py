"""New diagnostic through the real synthetic producer, seal and file-only CLI."""
import importlib
import json
import os
from datetime import timedelta, timezone
from pathlib import Path

import pytest

import test_pregate_capture_flow as capture
from test_market_state_capture import fields, PRICE, BOOK


def cli():
    try:
        return importlib.import_module("scripts.report_pregate_observed_regular_flow")
    except ModuleNotFoundError:
        pytest.fail("observed-flow CLI is not implemented")


@pytest.mark.parametrize("defect", ["symlink", "fifo", "duplicate", "limit"])
def test_invalid_file_contract_is_rejected_without_writes(tmp_path, capsys, defect):
    command = cli()
    study = tmp_path / "study.json"
    study.write_text("{}")
    journal = tmp_path / "journal.jsonl"
    journal.write_text("not a journal\n")
    supplied = study
    if defect == "symlink":
        supplied = tmp_path / "link.json"
        supplied.symlink_to(study)
    elif defect == "fifo":
        supplied = tmp_path / "fifo"
        os.mkfifo(supplied)
    elif defect == "duplicate":
        study.write_text('{"x":1,"x":2}')
    before = study.read_bytes(), journal.read_bytes()
    assert command.main([
        "--study", str(supplied), "--journal", str(journal),
        "--as-of", "2026-10-21T02:00:00+00:00",
        "--max-journal-bytes", str(256 * 1024 * 1024 + 1 if defect == "limit" else 100),
    ]) == 2
    assert capsys.readouterr().out == ""
    assert (study.read_bytes(), journal.read_bytes()) == before


async def collected(tmp_path, monkeypatch, *, enabled=True, empty=False, contrary=False):
    original_change = capture.study_change
    original_receive = capture.receive
    original_frame = capture.frame

    def change(study):
        original_change(study)
        if enabled:
            study["observed_regular_flow_policy"] = {
                "version": "pregate-observed-regular-flow-v1",
                "fixed_at": (capture.NOW - timedelta(days=1)).isoformat(),
                "preparation_seconds": 5, "state_max_age_seconds": 5,
                "horizon_seconds": 900, "max_markout_delay_seconds": 5,
            }

    def frame(tr, symbol):
        from src.data.feeds import kis_websocket
        now = kis_websocket.datetime.now().astimezone(timezone(timedelta(hours=9)))
        raw = fields(tr, symbol)
        if tr == PRICE:
            raw[1], raw[33] = now.strftime("%H%M%S"), now.strftime("%Y%m%d")
        elif tr == BOOK:
            raw[1] = now.strftime("%H%M%S")
        else:
            raw[8] = "Y" if contrary else "N"
        return f"0|{tr}|001|" + "^".join(raw)

    async def receive(feed, coordinator, data):
        tr = data.split("|")[1]
        # No MKO data is a legitimate input to the narrower diagnostic.
        if tr == capture.STATUS and not contrary:
            return
        await original_receive(feed, coordinator, data)

    monkeypatch.setattr(capture, "study_change", change)
    monkeypatch.setattr(capture, "frame", frame)
    monkeypatch.setattr(capture, "receive", receive)
    try:
        return await capture.collect_first_scan(tmp_path, monkeypatch, empty, initial_book=True)
    finally:
        monkeypatch.setattr(capture, "study_change", original_change)
        monkeypatch.setattr(capture, "frame", original_frame)
        monkeypatch.setattr(capture, "receive", original_receive)


@pytest.mark.asyncio
@pytest.mark.parametrize("empty", [False, True])
async def test_real_sealed_journal_cli_preserves_strict_and_inputs(tmp_path, monkeypatch, capsys, empty):
    command = cli()
    from src.analytics.pregate_candidate_readout import build_pregate_candidate_report
    plan, observed, as_of, symbols = await collected(tmp_path, monkeypatch, empty=empty)
    journal = Path(plan.settings["journal_path"])
    before = plan.study_path.read_bytes(), journal.read_bytes()
    strict = build_pregate_candidate_report(
        plan.study_bytes, observed, study_sha256=plan.study_sha256, analysis_as_of=as_of)
    assert command.main([
        "--study", str(plan.study_path), "--journal", str(journal), "--as-of", as_of,
        "--max-journal-bytes", str(plan.settings["max_bytes"]),
    ]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["strict_report"] == strict
    assert report["denominator"] == strict["denominator"]
    assert len(report["candidates"]) == (0 if empty else 7)
    assert report["account_return"] is report["net_pnl"] is None
    assert report["profitability_pass"] is report["production_eligible"] is False
    if not empty:
        assert [r["symbol"] for r in report["candidates"]] == symbols
        for old, row in zip(strict["candidates"][:5], report["candidates"][:5]):
            assert old["entry_market_state"]["verdict"] == "unknown"
            assert row["original_entry_quote"] == old["entry_quote"]
            assert row["anchor_quote"]["sequence"] > old["entry_quote"]["sequence"]
            assert row["anchor_delay_from_first_book"] > 0
            assert row["anchor_state"]["flow_context"] == "observed_regular_flow"
            assert row["markout_state"]["flow_context"] == "observed_regular_flow"
            assert row["anchor_state"]["vi_state"] == "unknown"
            assert row["anchor_state"]["tradability"] == "not_evaluated"
            assert row["gross_quote_move_pct"] == pytest.approx((10000 / 10010 - 1) * 100)
        assert all(r["gross_quote_move_pct"] is None for r in report["candidates"][5:])
    assert (plan.study_path.read_bytes(), journal.read_bytes()) == before


@pytest.mark.asyncio
async def test_existing_study_does_not_implicitly_opt_in(tmp_path, monkeypatch, capsys):
    command = cli()
    plan, _, as_of, _ = await collected(tmp_path, monkeypatch, enabled=False)
    assert command.main([
        "--study", str(plan.study_path), "--journal", str(plan.settings["journal_path"]),
        "--as-of", as_of, "--max-journal-bytes", str(plan.settings["max_bytes"]),
    ]) == 2
    assert capsys.readouterr().out == ""
    # Adding a declaration after sealing cannot turn the old journal into a
    # preregistered new-policy observation, even with a backdated fixed_at.
    changed = json.loads(plan.study_bytes)
    changed["observed_regular_flow_policy"] = {
        "version": "pregate-observed-regular-flow-v1",
        "fixed_at": (capture.NOW - timedelta(days=1)).isoformat(),
        "preparation_seconds": 5, "state_max_age_seconds": 5,
        "horizon_seconds": 900, "max_markout_delay_seconds": 5,
    }
    plan.study_path.write_text(json.dumps(changed))
    assert command.main([
        "--study", str(plan.study_path), "--journal", str(plan.settings["journal_path"]),
        "--as-of", as_of, "--max-journal-bytes", str(plan.settings["max_bytes"]),
    ]) == 2
    assert capsys.readouterr().out == ""


@pytest.mark.asyncio
async def test_real_contrary_event_cannot_be_ignored(tmp_path, monkeypatch, capsys):
    command = cli()
    plan, _, as_of, _ = await collected(tmp_path, monkeypatch, contrary=True)
    assert command.main([
        "--study", str(plan.study_path), "--journal", str(plan.settings["journal_path"]),
        "--as-of", as_of, "--max-journal-bytes", str(plan.settings["max_bytes"]),
    ]) == 0
    report = json.loads(capsys.readouterr().out)
    assert all(r["gross_quote_move_pct"] is None for r in report["candidates"])
    assert all(r["anchor_state"]["flow_context"] != "observed_regular_flow"
               for r in report["candidates"][:5])
