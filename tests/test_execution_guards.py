"""최신 위험 입력과 마지막 송신 경계의 보수적 계약."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from src.execution.safety.guards import (
    DispatchSnapshot, EntryAuthority, EntryContext, EntryOrigin,
    FinalDispatchGuard, FinalEntryGuard, RiskSnapshot, RiskSnapshotPublisher,
)

KST = ZoneInfo('Asia/Seoul')
NOW = datetime(2026, 9, 17, 13, 0, tzinfo=KST)


def risk(**changes):
    return replace(RiskSnapshot(1, 1, 'success', NOW, 'normal'), **changes)


def setup_guard(snapshot=None, now=NOW):
    authority = EntryAuthority()
    state = [snapshot if snapshot is not None else risk()]
    clock = [now]
    guard = FinalEntryGuard(authority, lambda: state[0], lambda: clock[0])
    return authority, state, clock, guard


@pytest.mark.parametrize('changes,reason', [
    ({'level': 'severe'}, 'intraday_severe'),
    ({'observation_status': 'failed'}, 'risk_unknown'),
    ({'as_of': None}, 'risk_unknown'),
    ({'as_of': NOW - timedelta(days=1)}, 'risk_unknown'),
    ({'as_of': NOW + timedelta(seconds=1)}, 'risk_unknown'),
    ({'as_of': NOW.replace(tzinfo=None)}, 'risk_unknown'),
    ({'level': 'invented'}, 'risk_unknown'),
    ({'recovery_until': NOW + timedelta(minutes=5)}, 'recovery_cooldown'),
    ({'recovery_until': NOW.replace(tzinfo=None)}, 'risk_unknown'),
])
def test_auto_buy_blocks_untrusted_or_adverse_current_observation(changes, reason):
    authority, _, _, guard = setup_guard(risk(**changes))
    result = guard.evaluate(authority.automatic('005930', 'buy', 'vcp_breakout'))
    assert not result.allowed
    assert result.reason == reason


def test_clock_and_snapshot_are_read_at_each_decision_not_at_construction():
    authority, snapshots, clock, guard = setup_guard()
    context = authority.automatic('005930', 'buy', 'sepa_trend')
    assert guard.evaluate(context).allowed
    snapshots[0] = risk(level='severe', version=2, attempt_sequence=2)
    assert guard.evaluate(context).reason == 'intraday_severe'
    snapshots[0] = risk()
    clock[0] = NOW.replace(hour=14, minute=29, second=59)
    assert guard.evaluate(context).allowed
    clock[0] += timedelta(seconds=1)
    assert guard.evaluate(context).reason == 'sepa_entry_cutoff'


def test_timezone_host_does_not_change_kst_cutoff():
    authority, _, _, guard = setup_guard(now=NOW.replace(hour=14, minute=30).astimezone(timezone.utc))
    assert guard.evaluate(authority.automatic('005930', 'buy', 'sepa_trend')).reason == 'sepa_entry_cutoff'


def test_explicit_routes_exempt_only_alpha_and_metadata_cannot_issue_context():
    authority, _, _, guard = setup_guard(risk(level='severe'))
    assert guard.evaluate(authority.automatic('005930', 'sell', 'sepa_trend')).allowed
    assert guard.evaluate(authority.safe_asset('458730', 'buy')).allowed
    assert guard.evaluate(authority.user_order('005930', 'buy')).allowed
    assert guard.evaluate(authority.automatic('005930', 'buy', 'manual')).reason == 'intraday_severe'
    forged = EntryContext('005930', 'buy', 'manual', EntryOrigin.USER, object())
    assert guard.evaluate(forged).reason == 'untrusted_entry_context'
    assert guard.evaluate(None).reason == 'untrusted_entry_context'
    alien = EntryAuthority().user_order('005930', 'buy')
    assert guard.evaluate(alien).reason == 'untrusted_entry_context'


def test_failure_attempt_hides_previous_normal_until_new_valid_observation():
    publisher = RiskSnapshotPublisher()
    first = publisher.publish(attempt_sequence=1, as_of=NOW, level='normal')
    authority = EntryAuthority()
    guard = FinalEntryGuard(authority, lambda: publisher.snapshot, lambda: NOW)
    context = authority.automatic('005930', 'buy', 'vcp_breakout')
    assert guard.evaluate(context).allowed
    publisher.publish(attempt_sequence=2, as_of=None, level=None, observation_status='failed')
    assert guard.evaluate(context).reason == 'risk_unknown'
    publisher.publish(attempt_sequence=1, as_of=NOW, level='normal')
    assert guard.evaluate(context).reason == 'risk_unknown'
    publisher.publish(attempt_sequence=3, as_of=NOW, level='normal')
    assert publisher.snapshot.version > first.version
    assert guard.evaluate(context).allowed


def test_conflicting_same_sequence_is_unknown_and_identical_retry_cannot_heal_it():
    publisher = RiskSnapshotPublisher()
    publisher.publish(attempt_sequence=1, as_of=NOW, level='normal')
    publisher.publish(attempt_sequence=1, as_of=NOW, level='severe')
    assert publisher.snapshot.observation_status == 'conflict'
    publisher.publish(attempt_sequence=1, as_of=NOW, level='normal')
    assert publisher.snapshot.observation_status == 'conflict'


def dispatch(**changes):
    return replace(DispatchSnapshot(
        healthy=True, version=3, published_version=3,
        startup_reconciliation=False, publication_recovery_required=False,
        attempt_id='a', owner_token='owner', claim_active=True, sent=False,
        conflicting_attempt=False, protection_degraded=False,
    ), **changes)


@pytest.mark.parametrize('changes,reason', [
    ({'healthy': False}, 'store_unhealthy'),
    ({'startup_reconciliation': True}, 'startup_reconciliation'),
    ({'published_version': 2}, 'publication_recovery_required'),
    ({'publication_recovery_required': True}, 'publication_recovery_required'),
    ({'claim_active': False}, 'sender_claim_invalid'),
    ({'owner_token': 'other'}, 'sender_claim_invalid'),
    ({'attempt_id': 'other'}, 'sender_claim_invalid'),
    ({'sent': True}, 'already_dispatched'),
    ({'conflicting_attempt': True}, 'intent_conflict'),
])
@pytest.mark.parametrize('side,command', [('buy','submit'), ('sell','submit'), ('sell','cancel')])
def test_common_safety_barrier_applies_to_all_trade_commands(changes, reason, side, command):
    authority, _, _, entry = setup_guard()
    state = [dispatch(**changes)]
    guard = FinalDispatchGuard(lambda: state[0], entry)
    context = authority.automatic('005930', side, 'sepa_trend')
    decision = guard.evaluate('a', 'owner', context, command=command)
    assert not decision.allowed
    assert decision.reason == reason


def test_degraded_protection_blocks_only_automatic_buy_and_rechecks_store():
    authority, _, _, entry = setup_guard()
    states = [dispatch(protection_degraded=True)]
    guard = FinalDispatchGuard(lambda: states[0], entry)
    buy = authority.automatic('005930', 'buy', 'vcp_breakout')
    sell = authority.automatic('005930', 'sell', 'vcp_breakout')
    assert guard.evaluate('a', 'owner', buy).reason == 'protection_degraded'
    assert guard.evaluate('a', 'owner', sell).allowed
    assert guard.evaluate('a', 'owner', buy, command='cancel').allowed
    states[0] = dispatch(healthy=False)
    assert guard.evaluate('a', 'owner', sell).reason == 'store_unhealthy'


def test_invalid_commands_and_missing_dispatch_state_fail_closed():
    authority, _, _, entry = setup_guard()
    context = authority.automatic('005930', 'buy', 'vcp_breakout')
    guard = FinalDispatchGuard(lambda: dispatch(), entry)
    assert guard.evaluate('a', 'owner', context, command='invented').reason == 'unknown_command'
    assert FinalDispatchGuard(lambda: None, entry).evaluate('a', 'owner', context).reason == 'store_unhealthy'


@pytest.mark.parametrize('changes,now,reason', [
    ({'level': 'severe'}, NOW, 'intraday_severe'),
    ({'recovery_until': NOW + timedelta(minutes=5)}, NOW, 'recovery_cooldown'),
    ({}, NOW.replace(hour=14, minute=30), 'sepa_entry_cutoff'),
    ({'observation_status': 'failed'}, NOW, 'risk_unknown'),
])
def test_automatic_buy_modification_rechecks_alpha_policy(changes, now, reason):
    authority, _, _, entry = setup_guard(risk(**changes), now=now)
    guard = FinalDispatchGuard(lambda: dispatch(), entry)
    context = authority.automatic('005930', 'buy', 'sepa_trend')
    decision = guard.evaluate('a', 'owner', context, command='modify')
    assert not decision.allowed
    assert decision.reason == reason
    assert guard.evaluate('a', 'owner', context, command='cancel').allowed


def test_degraded_protection_blocks_buy_modify_but_not_existing_sell_policy():
    authority, _, _, entry = setup_guard(risk(level='severe'))
    guard = FinalDispatchGuard(lambda: dispatch(protection_degraded=True), entry)
    buy = authority.automatic('005930', 'buy', 'sepa_trend')
    sell = authority.automatic('005930', 'sell', 'sepa_trend')
    assert guard.evaluate('a', 'owner', buy, command='modify').reason == 'protection_degraded'
    assert guard.evaluate('a', 'owner', sell, command='modify').allowed
    assert guard.evaluate('a', 'owner', buy, command='cancel').allowed
