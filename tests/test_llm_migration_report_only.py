"""The legacy nightly monitor must never promote models or restart trading."""
import ast
import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


SOURCE = Path(__file__).resolve().parents[1] / "scripts/llm_migration_monitor.py"


@pytest.fixture
def monitor(tmp_path, monkeypatch):
    # Legacy RED runs must not execute subprocesses even on an unexpected branch.
    def refuse_process(*args, **kwargs):
        raise AssertionError("process launch forbidden in monitor test")
    monkeypatch.setattr(subprocess, "run", refuse_process)
    spec = importlib.util.spec_from_file_location("migration_report_test", SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(module, "SHADOW_LOG_DIR", tmp_path / "shadow")
    monkeypatch.setattr(module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(module, "EVOLVED_OVERRIDES_PATH", tmp_path / "overrides.yml", raising=False)
    return module


def passing_aggregate():
    return {
        "pairs": 50,
        "stats": {"both_success": 50, "primary_only": 0, "shadow_only": 0,
                  "both_failed": 0, "key_overlap_count": 50,
                  "key_overlap_sum": 50, "shadow_latency_ms_sum": 5000},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["shadow", "transitioned", "completed", "unknown"])
@pytest.mark.parametrize("passing", [True, False])
async def test_all_legacy_phases_are_read_only(monitor, monkeypatch, phase, passing):
    state = {"phase": phase, "transitioned_at": "2026-01-01T00:00:00",
             "failed_evals": [{"date": "2026-01-01"}]}
    monitor.STATE_PATH.write_text(json.dumps(state))
    monitor.EVOLVED_OVERRIDES_PATH.write_text("llm:\n  openai_model_light: preserved\n")
    before = (monitor.STATE_PATH.read_bytes(), monitor.EVOLVED_OVERRIDES_PATH.read_bytes())
    aggregate = passing_aggregate() if passing else {"error": "shadow 로그 없음", "pairs": 0}
    monkeypatch.setattr(monitor, "run_shadow_analysis", lambda **_: aggregate)
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr(monitor, "store_report", sent)
    commands = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: commands.append(a) or SimpleNamespace(returncode=0))

    assert await monitor.main_async() == 0

    assert (monitor.STATE_PATH.read_bytes(), monitor.EVOLVED_OVERRIDES_PATH.read_bytes()) == before
    assert commands == []
    sent.assert_awaited_once()
    body = sent.call_args.args[0]
    assert "보고 전용" in body
    assert "순수익" in body
    assert "자동 전환 완료" not in body
    assert "추가 수집" not in body


def test_import_does_not_load_environment():
    tree = ast.parse(SOURCE.read_text())
    assert not any(isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
                   and isinstance(n.value.func, ast.Name) and n.value.func.id == "_load_env"
                   for n in tree.body)


@pytest.mark.asyncio
async def test_dry_run_never_sends(monitor, monkeypatch):
    monkeypatch.setattr(monitor, "run_shadow_analysis", lambda **_: passing_aggregate())
    sent = AsyncMock()
    monkeypatch.setattr(monitor, "store_report", sent)
    assert await monitor.main_async(dry_run=True) == 0
    sent.assert_not_awaited()
    assert not monitor.STATE_PATH.exists()


@pytest.mark.parametrize("content", ["not json", "[]", "null"])
def test_unreadable_or_non_object_state_is_unknown(monitor, content):
    monitor.STATE_PATH.write_text(content)
    assert monitor.load_state()["phase"] == "unknown"


def test_empty_state_is_not_assumed_shadow(monitor):
    assert monitor.load_state()["phase"] == "unknown"


def test_cli_dry_run_does_not_load_env(monitor, tmp_path, monkeypatch):
    env_file = tmp_path / "test.env"
    env_file.write_text("TG_CHAT_ID=synthetic-fixture\n")
    monkeypatch.delenv("TG_CHAT_ID", raising=False)
    monkeypatch.setattr(monitor, "run_shadow_analysis", lambda **_: {"error": "shadow 로그 없음", "pairs": 0})
    assert monitor.main(["--dry-run", "--env-file", str(env_file),
                         "--state-file", str(monitor.STATE_PATH),
                         "--log-dir", str(monitor.SHADOW_LOG_DIR)]) == 0
    import os
    assert "TG_CHAT_ID" not in os.environ


def test_env_loader_only_uses_notification_keys(monitor, tmp_path, monkeypatch):
    env_file = tmp_path / "test.env"
    env_file.write_text("TG_CHAT_ID=fixture\nOPENAI_API_KEY=not-a-key\nQWQ_TEST_UNUSED=ignored\n")
    monkeypatch.delenv("TG_CHAT_ID", raising=False)
    monkeypatch.delenv("QWQ_TEST_UNUSED", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "existing-fixture")
    monitor._load_env(env_file)
    import os
    assert os.environ["TG_CHAT_ID"] == "fixture"
    assert os.environ["OPENAI_API_KEY"] == "existing-fixture"
    assert "QWQ_TEST_UNUSED" not in os.environ


@pytest.mark.asyncio
async def test_send_failure_is_not_reported_as_success(monitor, monkeypatch):
    monkeypatch.setattr(monitor, "run_shadow_analysis", lambda **_: passing_aggregate())
    monkeypatch.setattr(monitor, "send_telegram", AsyncMock(return_value=False))
    assert await monitor.main_async(telegram=True) == 1


@pytest.mark.asyncio
async def test_real_shadow_records_stay_descriptive(monitor, monkeypatch):
    from datetime import date
    monkeypatch.setattr(monitor, "PROJECT_ROOT", SOURCE.parents[1])
    monitor.SHADOW_LOG_DIR.mkdir()
    record = {"task": "fixture", "primary": {"success": True, "content": '{"score": 1}'},
              "shadow": {"success": True, "content": '{"score": -1}', "latency_ms": 10}}
    (monitor.SHADOW_LOG_DIR / f"{date.today():%Y%m%d}.jsonl").write_text(
        (json.dumps(record) + "\n") * 50)
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr(monitor, "store_report", sent)
    assert await monitor.main_async() == 0
    text = sent.call_args.args[0]
    assert "<b>50</b>건" in text
    assert "과정 품질 기준 충족" in text
    assert "판단 정확도·순수익의 증거가 아닙니다" in text
    assert not monitor.STATE_PATH.exists()


@pytest.mark.asyncio
async def test_notification_result_and_session_cleanup(monitor, monkeypatch):
    import src.utils.telegram as telegram
    session = SimpleNamespace(closed=False, close=AsyncMock())
    notifier = SimpleNamespace(is_configured=True, _session=session,
                               send_message=AsyncMock(return_value=False))
    monkeypatch.setattr(telegram, "TelegramNotifier", lambda: notifier)
    assert await monitor.send_telegram("fixture") is False
    session.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_malformed_observation_reports_unavailable(monitor, monkeypatch):
    def fail(**kwargs):
        raise TypeError("fixture contains invalid field")
    monkeypatch.setattr(monitor, "run_shadow_analysis", fail)
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr(monitor, "store_report", sent)
    assert await monitor.main_async() == 0
    assert "자료 분석 불가" in sent.call_args.args[0]
    assert not monitor.STATE_PATH.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("record", [None, {"primary": None}, {"shadow": {"latency_ms": float("inf")}}])
async def test_real_invalid_jsonl_keeps_nightly_report(monitor, monkeypatch, record):
    from datetime import date
    monkeypatch.setattr(monitor, "PROJECT_ROOT", SOURCE.parents[1])
    monitor.SHADOW_LOG_DIR.mkdir()
    (monitor.SHADOW_LOG_DIR / f"{date.today():%Y%m%d}.jsonl").write_text(json.dumps(record) + "\n")
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr(monitor, "store_report", sent)
    assert await monitor.main_async() == 0
    assert "자료 분석 불가" in sent.call_args.args[0]
    assert not monitor.STATE_PATH.exists()
