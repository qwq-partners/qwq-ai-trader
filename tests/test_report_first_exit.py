"""Offline CLI boundaries; synthetic inputs only."""
import importlib.util
import json
import os
from pathlib import Path
import stat

import pytest


def _module():
    path = Path(__file__).resolve().parents[1] / "scripts/report_first_exit.py"
    spec = importlib.util.spec_from_file_location("report_first_exit_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def cli(monkeypatch):
    module = _module()
    calls = []
    def replay(payload):
        calls.append(payload)
        return {"net_pnl": None, "fill_status": "not_simulated"}
    monkeypatch.setattr(module, "replay_first_exit", replay)
    return module, calls


def test_cli_writes_new_private_report_and_preserves_input(cli, tmp_path):
    module, calls = cli
    source = tmp_path / "input.json"
    source.write_text('{"synthetic": true}', encoding="utf-8")
    before = source.read_bytes()
    output = tmp_path / "report.json"
    assert module.main(["--input", str(source), "--output", str(output)]) == 0
    assert calls == [{"synthetic": True}]
    assert json.loads(output.read_text())["net_pnl"] is None
    assert source.read_bytes() == before
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


@pytest.mark.parametrize("raw", [b'{"x":1,"x":2}', b'{"x":{"y":1,"y":2}}', b'{"x":NaN}', b'{"x":Infinity}', b'[1]', b'{invalid', b'\xff'])
def test_cli_rejects_invalid_json_without_replay(cli, tmp_path, raw):
    module, calls = cli
    source, output = tmp_path / "in.json", tmp_path / "out.json"
    source.write_bytes(raw)
    assert module.main(["--input", str(source), "--output", str(output)]) == 2
    assert calls == []
    assert not output.exists()


def test_cli_bounds_input_bytes_before_replay(cli, tmp_path):
    module, calls = cli
    source, output = tmp_path / "in.json", tmp_path / "out.json"
    source.write_bytes(b" " * (module.MAX_INPUT_BYTES + 1))
    assert module.main(["--input", str(source), "--output", str(output)]) == 2
    assert calls == []
    assert not output.exists()


@pytest.mark.parametrize("kind", ["regular", "symlink", "same"])
def test_cli_preserves_existing_output_before_replay(cli, tmp_path, kind):
    module, calls = cli
    source, output = tmp_path / "in.json", tmp_path / "out.json"
    source.write_text("{}")
    if kind == "regular":
        output.write_text("preserve")
    elif kind == "symlink":
        output.symlink_to(source)
    else:
        output = source
    before = output.read_bytes()
    assert module.main(["--input", str(source), "--output", str(output)]) == 2
    assert output.read_bytes() == before
    assert calls == []


@pytest.mark.parametrize("kind", ["fifo", "directory", "symlink"])
def test_cli_rejects_nonregular_or_indirect_input(cli, tmp_path, kind):
    module, calls = cli
    source, output = tmp_path / "in.json", tmp_path / "out.json"
    if kind == "fifo":
        os.mkfifo(source)
    elif kind == "directory":
        source.mkdir()
    else:
        target = tmp_path / "target.json"
        target.write_text("{}")
        source.symlink_to(target)
    assert module.main(["--input", str(source), "--output", str(output)]) == 2
    assert calls == []
    assert not output.exists()


def test_cli_invalid_contract_creates_no_report(cli, monkeypatch, tmp_path):
    module, _ = cli
    def invalid(payload):
        raise ValueError("unsupported input contract")
    monkeypatch.setattr(module, "replay_first_exit", invalid)
    source, output = tmp_path / "in.json", tmp_path / "out.json"
    source.write_text("{}")
    assert module.main(["--input", str(source), "--output", str(output)]) == 2
    assert not output.exists()


def test_cli_real_replay_reports_first_signal_without_profit(tmp_path):
    module = _module()
    source, output = tmp_path / "in.json", tmp_path / "out.json"
    payload = {
        "schema_version": "first_exit_replay_v1",
        "entry": {"symbol": "000001", "quantity": 100, "price": "10000",
                  "at": "2026-10-19T09:30:00+09:00", "source_ref": "synthetic:entry"},
        "events": [{"sequence": 1, "at": "2026-10-19T09:31:00+09:00",
                    "kind": "PRICE", "price": "9500", "source_ref": "synthetic:1"}],
        "policies": [{"name": "A", "config": {"stop_loss_pct": 4}},
                     {"name": "B", "config": {"stop_loss_pct": 8}}],
        "fees": {"buy_commission_rate": "0", "sell_commission_rate": "0", "sell_tax_rate": "0"},
    }
    source.write_text(json.dumps(payload))
    assert module.main(["--input", str(source), "--output", str(output)]) == 0
    report = json.loads(output.read_text())
    assert report["arms"][0]["first_decision"]["action"] == "sell_all"
    assert report["arms"][1]["status"] == "not_observed_in_supplied_path"
    assert report["comparison"]["first_decision_changed"] is True
    assert report["net_pnl"] is None
    assert report["live_exit_parity"] is False
