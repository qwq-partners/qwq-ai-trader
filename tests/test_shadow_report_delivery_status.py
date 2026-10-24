"""A failed report delivery must not look successful to cron or its operator."""

import runpy
import sys
from pathlib import Path

import dotenv
import pytest

from src.utils import telegram


SOURCE = Path(__file__).resolve().parents[1] / "scripts/shadow_report.py"


def run_cli(monkeypatch, tmp_path, *args):
    # Keep the real report builder and CLI, with empty private input directories.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(sys, "argv", [str(SOURCE), *args])
    try:
        runpy.run_path(str(SOURCE), run_name="__main__")
    except SystemExit as exc:
        return exc.code
    return 0


@pytest.mark.parametrize(
    "outcome, expected_rc, expected_label",
    [
        (True, 0, "[전송 완료]"),
        (False, 1, "[전송 실패]"),
        (None, 1, "[전송 실패]"),
        (RuntimeError("synthetic-sensitive-detail"), 1, "[전송 실패]"),
    ],
    ids=["accepted", "rejected", "unconfirmed", "exception"],
)
def test_cli_delivery_status_matches_notifier_result(
    monkeypatch, tmp_path, capsys, outcome, expected_rc, expected_label
):
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **kw: False)
    delivered = []

    async def send_alert(text):
        delivered.append(text)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(telegram, "send_alert", send_alert)

    rc = run_cli(monkeypatch, tmp_path, "--days", "7", "--telegram")
    output = capsys.readouterr().out

    assert rc == expected_rc
    assert expected_label in output
    assert len(delivered) == 1  # This change must not add retry sends.
    assert "최근 7일" in delivered[0]
    assert "아직 심의 기록이 없습니다." in delivered[0]
    if expected_rc:
        assert "[전송 완료]" not in output
    assert "synthetic-sensitive-detail" not in output


def test_print_only_does_not_load_credentials_or_send(monkeypatch, tmp_path, capsys):
    def reject_env(*args, **kwargs):
        pytest.fail("print-only mode loaded credentials")

    async def reject_send(*args, **kwargs):
        pytest.fail("print-only mode sent a report")

    monkeypatch.setattr(dotenv, "load_dotenv", reject_env)
    monkeypatch.setattr(telegram, "send_alert", reject_send)

    assert run_cli(monkeypatch, tmp_path) == 0
    output = capsys.readouterr().out
    assert "아직 심의 기록이 없습니다." in output
    assert "[전송 완료]" not in output
    assert "[전송 실패]" not in output
