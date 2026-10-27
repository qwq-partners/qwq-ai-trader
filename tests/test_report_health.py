from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock
from zoneinfo import ZoneInfo
import pytest
from src.analytics.report_context import render_runtime_context
from src.analytics.audience_reports import render_engine_digest

NOW = datetime(2026, 10, 27, 21, tzinfo=ZoneInfo('Asia/Seoul'))


def setup_health(**overrides):
    sample = SimpleNamespace(engine=SimpleNamespace(get_available_cash=Mock(return_value=Decimal(0))),
                             risk_manager=SimpleNamespace(_cash_verified=True))
    health = {'session_valid': True, 'account_sync': {'last_success': NOW.timestamp()-60,
              'consecutive_failures': 0, 'idle_reason': None}}
    health.update(overrides)
    return sample, health


def test_current_cash_requires_recent_success_and_discloses_semantics():
    sample, health = setup_health()
    text = render_runtime_context(sample, now=NOW, health=health, kill_status={'halt_buy': True})
    assert '가용현금 없음' in text and '10/27 20:59' in text
    assert '시세 아님' in text and '통신 확인 아님' in text
    assert len(text.splitlines()) == 3
    sample.engine.get_available_cash.assert_called_once()
    assert '10/27 20:59' in render_engine_digest([], text, NOW)


@pytest.mark.parametrize('sync', [
    {}, {'last_success': None}, {'last_success': NOW.timestamp()-901},
    {'last_success': NOW.timestamp()+1}, {'last_success': float('nan')},
    {'last_success': NOW.timestamp()-60, 'consecutive_failures': 1},
    {'last_success': NOW.timestamp()-60, 'idle_reason': 'sync_guard'},
])
def test_unknown_stale_failed_or_idle_never_reads_cash(sync):
    sample, health = setup_health(account_sync=sync)
    text = render_runtime_context(sample, now=NOW, health=health, kill_status={})
    assert '현금 상태 미확인' in text
    sample.engine.get_available_cash.assert_not_called()


@pytest.mark.parametrize('valid', [False, None])
def test_local_session_missing_or_invalid_is_not_fresh_cash(valid):
    sample, health = setup_health(session_valid=valid)
    text = render_runtime_context(sample, now=NOW, health=health, kill_status={})
    assert '현금 상태 미확인' in text
    sample.engine.get_available_cash.assert_not_called()


def test_new_failure_preserves_last_success_but_not_raw_failure_reason():
    sample, health = setup_health()
    health['account_sync'].update(consecutive_failures=1, failure_reason='PRIVATE_RAW_ERROR')
    text = render_runtime_context(sample, now=NOW, health=health, kill_status={})
    assert '최근 실패' in text and '20:59' in text and 'PRIVATE_RAW_ERROR' not in text
    sample.engine.get_available_cash.assert_not_called()


def test_default_health_reads_real_heartbeat_contract_without_api(monkeypatch):
    from src.utils import loop_heartbeat
    monkeypatch.setattr(loop_heartbeat, '_states', {})
    monkeypatch.setattr(loop_heartbeat, '_beats', {})
    monkeypatch.setattr(loop_heartbeat.time, 'time', lambda: NOW.timestamp()-60)
    loop_heartbeat.record_success('kr_portfolio_sync')
    sample, _ = setup_health()
    sample.engine.broker = SimpleNamespace(is_connected=True)
    text = render_runtime_context(sample, now=NOW, kill_status={'halt_buy': True})
    assert '10/27 20:59' in text and '가용현금 없음' in text
    sample.engine.get_available_cash.assert_called_once()


@pytest.mark.parametrize('health', [[], 'bad', {'session_valid': True, 'account_sync': 'bad'}])
def test_malformed_health_fails_closed(health):
    sample, _ = setup_health()
    text = render_runtime_context(sample, now=NOW, health=health, kill_status={})
    assert '현금 상태 미확인' in text
    sample.engine.get_available_cash.assert_not_called()
