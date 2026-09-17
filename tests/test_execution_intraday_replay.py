"""실제 정책 reducer/SQLite 보호 재생. 생산 capture hook을 직접 거친다."""
import asyncio
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal

import pytest

from src.execution.safety import protection_recovery as recovery
from src.strategies.exit_manager import ExitManager
from test_execution_intraday_owner import owned
from test_execution_runtime import NOW, opened, observed, queued
from test_kis_index_observation import provider, raw


def assert_actual_capture_binding(writer):
    capture = getattr(recovery, 'capture_intraday_transition', None)
    assert callable(capture), 'typed intraday replay capture API missing'
    assert writer.sources._completion_reducer == writer._reduce


async def degraded(tmp_path, monkeypatch, *, clock=None, metadata=None):
    engine, exits, store, runtime, batch, writer = await owned(tmp_path, clock=clock)
    assert_actual_capture_binding(writer)
    ref = await opened(runtime, 'B1')
    observation = await observed(runtime, ref, 40, '400000', metadata=metadata)
    def fail(*args, **kwargs):
        raise ValueError('synthetic first registration failure')
    with monkeypatch.context() as patch:
        patch.setattr(ExitManager, 'register_position', fail)
        assert (await queued(engine, observation)).protection_status == 'degraded'
    return engine, exits, store, runtime, writer, ref


async def refresh(writer, monkeypatch, pct, *, at=NOW):
    output = raw()
    output['bstp_nmix_prdy_ctrt'] = str(pct)
    md, _ = await provider(monkeypatch, output)
    monkeypatch.setattr(md, '_index_clock', lambda: at)
    return await writer.refresh(md)


def invariant_roots(before, after):
    for key in before.keys() - {'protection', 'cursors', 'recovery_receipts'}:
        assert after[key] == before[key], key
    for key in before['protection'].keys() - {'states', 'entry_times', 'degraded', 'pending_owners', 'orders'}:
        assert after['protection'][key] == before['protection'][key], key


@pytest.mark.parametrize('pct,sl,ts', [(-1.5, 3.0, 2.5), (-3.0, 2.5, 2.0), (-4.0, 2.0, 1.5)])
def test_actual_degraded_fill_then_owned_policy_repair_is_supported(tmp_path, monkeypatch, pct, sl, ts):
    async def scenario():
        engine, exits, store, runtime, writer, _ = await degraded(tmp_path, monkeypatch)
        try:
            result = await refresh(writer, monkeypatch, pct)
            assert result.status == 'accepted'
            before = runtime.owner.state
            event = before['protection_replay']['005930']['events'][-1]
            assert event['kind'] == 'intraday_policy'
            assert event['source_version'] == result.committed_version
            receipt = await runtime.repair_protection('repair', '005930', expected_version=runtime.owner.version)
            assert receipt.status == 'APPLIED', receipt.reason
            state = exits.get_state('005930')
            assert state.remaining_quantity == 40
            assert (state.stop_loss_pct, state.trailing_stop_pct) == (sl, ts)
            assert engine.portfolio.positions['005930'].quantity == 40
            invariant_roots(before, runtime.owner.state)
            assert writer.snapshot().observation_status == 'missing'
            assert not runtime.trading_ready
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('quote_first', [True, False])
def test_historical_quote_uses_policy_at_that_event_not_current_policy(tmp_path, monkeypatch, quote_first):
    async def scenario():
        _, exits, store, runtime, writer, _ = await degraded(tmp_path, monkeypatch)
        try:
            if quote_first:
                await runtime.quote('005930', Decimal('9700'))
            await refresh(writer, monkeypatch, -4)
            if not quote_first:
                await runtime.quote('005930', Decimal('9700'))
            before = runtime.owner.state
            receipt = await runtime.repair_protection('ordered', '005930', expected_version=runtime.owner.version)
            assert receipt.status == ('APPLIED' if quote_first else 'BLOCKED'), receipt.reason
            if quote_first:
                assert exits.get_state('005930').stop_loss_pct == 2
            else:
                assert receipt.reason == 'unrecorded_historical_decision'
                assert runtime.owner.state['protection'] == before['protection']
            invariant_roots(before, runtime.owner.state)
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_policy_fill_quote_and_normal_recovery_match_healthy_actual_path(tmp_path, monkeypatch):
    async def scenario():
        clock = [NOW]
        engine, exits, store, runtime, writer, ref = await degraded(tmp_path / 'degraded', monkeypatch, clock=lambda: clock[0])
        control = await owned(tmp_path / 'healthy', clock=lambda: clock[0])
        control_engine, control_exits, control_store, control_runtime, _, control_writer = control
        control_ref = await opened(control_runtime, 'B1')
        await queued(control_engine, await observed(control_runtime, control_ref, 40, '400000'))
        try:
            for rt, wr, en, rf in ((runtime, writer, engine, ref),
                                    (control_runtime, control_writer, control_engine, control_ref)):
                await refresh(wr, monkeypatch, -1.5, at=clock[0])
                await queued(en, await observed(rt, rf, 100, '1000000'))
                await refresh(wr, monkeypatch, -3, at=clock[0])
                await rt.quote('005930', Decimal('10100'))
                await refresh(wr, monkeypatch, 0, at=clock[0])
            before = runtime.owner.state
            result = await runtime.repair_protection('mixed', '005930', expected_version=runtime.owner.version)
            assert result.status == 'APPLIED', result.reason
            recovered = runtime.owner.state['protection']['states']['005930']
            healthy = control_runtime.owner.state['protection']['states']['005930']
            assert recovered == healthy
            assert recovered['remaining_quantity'] == 100
            assert recovered['highest_price'] == '10100'
            assert writer.batch._intraday_recovery_until == NOW + timedelta(minutes=5)
            invariant_roots(before, runtime.owner.state)
        finally:
            await runtime.shutdown()
            await control_runtime.shutdown()
            await store.close()
            await control_store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('followup', ['same_level', 'duplicate', 'missing'])
def test_scope_unchanged_refresh_does_not_append_policy_event(tmp_path, monkeypatch, followup):
    async def scenario():
        _, _, store, runtime, writer, _ = await degraded(tmp_path, monkeypatch)
        try:
            md, _ = await provider(monkeypatch, {**raw(), 'bstp_nmix_prdy_ctrt': '-3'})
            await writer.refresh(md)
            history = runtime.owner.state['protection_replay']
            if followup == 'same_level':
                await refresh(writer, monkeypatch, -3.2)
            elif followup == 'duplicate':
                await writer.refresh(md)
            else:
                await writer.refresh(None)
            assert runtime.owner.state['protection_replay'] == history
            result = await runtime.repair_protection('no-new-scope', '005930', expected_version=runtime.owner.version)
            assert result.status == 'APPLIED', result.reason
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_healthy_policy_change_creates_no_repair_history(tmp_path, monkeypatch):
    async def scenario():
        engine, _, store, runtime, _, writer = await owned(tmp_path)
        assert_actual_capture_binding(writer)
        try:
            ref = await opened(runtime, 'B1')
            await queued(engine, await observed(runtime, ref, 40, '400000'))
            await refresh(writer, monkeypatch, -3)
            assert not runtime.owner.state.get('protection_replay')
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_source_conflict_does_not_erase_previously_applied_policy_evidence(tmp_path, monkeypatch):
    async def scenario():
        from src.execution.safety.risk_sources import _ticket
        _, exits, store, runtime, writer, _ = await degraded(tmp_path, monkeypatch)
        try:
            receipt = await refresh(writer, monkeypatch, -3)
            row = runtime.owner.state['risk_sources']['records'][receipt.operation_id]
            conflict = await writer.sources.complete(_ticket(row), 'failed')
            assert conflict.status == 'conflict'
            assert writer.snapshot().observation_status == 'conflict'
            before = runtime.owner.state
            repaired = await runtime.repair_protection('after-conflict', '005930', expected_version=runtime.owner.version)
            assert repaired.status == 'APPLIED', repaired.reason
            assert exits.get_state('005930').stop_loss_pct == 2.5
            assert writer.snapshot().observation_status == 'conflict'
            invariant_roots(before, runtime.owner.state)
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_cold_new_runtime_repair_and_retry_preserve_original_source_and_effects(tmp_path, monkeypatch):
    async def scenario():
        from src.core.engine import UnifiedEngine
        from src.core.types import TradingConfig
        from src.execution.safety.runtime import KRExecutionRuntime
        from src.execution.safety.store import ExecutionStateStore
        _, _, store, runtime, writer, _ = await degraded(tmp_path, monkeypatch)
        await refresh(writer, monkeypatch, -4)
        path, before = store.path, runtime.owner.state
        await runtime.shutdown()
        await store.close()
        reopened = ExecutionStateStore(path)
        engine = UnifiedEngine(TradingConfig(initial_capital=Decimal('2000000')))
        exits = ExitManager(persist=False, clock=lambda: NOW)
        restored = KRExecutionRuntime(reopened, engine, exits, clock=lambda: NOW, account_scope='scope')
        try:
            await restored.restore()
            version = restored.owner.version
            receipt = await restored.repair_protection('cold', '005930', expected_version=version)
            assert receipt.status == 'APPLIED', receipt.reason
            assert (await restored.repair_protection('cold', '005930', expected_version=version)).status == 'ALREADY_APPLIED'
            assert exits.get_state('005930').remaining_quantity == 40
            invariant_roots(before, restored.owner.state)
        finally:
            await restored.shutdown()
            await reopened.close()
    asyncio.run(scenario())


def reseal(state):
    history = state['protection_replay']['005930']
    previous = ''
    for event in history['events']:
        event['previous'] = previous
        event['digest'] = recovery.digest({key: value for key, value in event.items() if key != 'digest'})
        previous = event['digest']
    history['tail'] = previous


@pytest.mark.parametrize('damage', ['operation', 'request', 'sequence', 'version', 'digest', 'before_policy',
                                   'after_policy', 'rule', 'rule_digest', 'bool_schema', 'bool_sequence',
                                   'extra', 'original_scope', 'event_time_utc', 'event_extra'])
def test_resealed_policy_evidence_cannot_forge_source_or_actual_transition(tmp_path, monkeypatch, damage):
    async def scenario():
        _, _, store, runtime, writer, _ = await degraded(tmp_path, monkeypatch)
        try:
            await refresh(writer, monkeypatch, -3)
            def corrupt(state):
                event = state['protection_replay']['005930']['events'][-1]
                fact = event['policy_input']
                if damage == 'operation': fact['operation_id'] = 'different'
                elif damage == 'request': fact['request_digest'] = '0' * 64
                elif damage == 'sequence': fact['attempt_sequence'] += 1
                elif damage == 'version': fact['source_version'] -= 1
                elif damage == 'digest': fact['outcome_digest'] = '0' * 64
                elif damage == 'before_policy': fact['before_policy']['kospi_pct'] = 1.0
                elif damage == 'after_policy': fact['after_policy']['kospi_pct'] = 1.0
                elif damage == 'rule': fact['rule'] = 'unsupported'
                elif damage == 'rule_digest': fact['rule_digest'] = '0' * 64
                elif damage == 'bool_schema': fact['schema'] = True
                elif damage == 'bool_sequence': fact['attempt_sequence'] = True
                elif damage == 'extra': fact['unverified'] = True
                elif damage == 'original_scope':
                    event['before']['protection']['config']['stop_loss_pct'] = 99.0
                elif damage == 'event_time_utc': event['applied_at'] = '2026-09-18T01:00:00+00:00'
                elif damage == 'event_extra': event['made_up'] = True
                reseal(state)
                return state
            await runtime.owner.mutate('synthetic-history-damage', corrupt)
            before = runtime.owner.state
            result = await runtime.repair_protection('corrupt', '005930', expected_version=runtime.owner.version)
            assert result.status == 'BLOCKED', damage
            invariant_roots(before, runtime.owner.state)
            assert runtime.owner.state['protection'] == before['protection']
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('is_core', [False, True])
def test_repaired_atr_and_core_policy_matches_actual_healthy_position(tmp_path, monkeypatch, is_core):
    async def scenario():
        params = {'is_core': is_core, 'stop_loss_pct': 8.0, 'trailing_stop_pct': 6.0,
                  'atr_pct_hint': 3.0, 'first_exit_pct': 11.0, 'strategy_name': 'core_holding' if is_core else 'sepa_trend'}
        metadata = {'registration_params': params}
        _, _, store, runtime, writer, _ = await degraded(tmp_path / 'damaged', monkeypatch, metadata=metadata)
        en, _, control_store, control, _, wr = await owned(tmp_path / 'healthy')
        try:
            ref = await opened(control, 'B1')
            await queued(en, await observed(control, ref, 40, '400000', metadata=metadata))
            await refresh(writer, monkeypatch, -4)
            await refresh(wr, monkeypatch, -4)
            before = runtime.owner.state
            result = await runtime.repair_protection('atr', '005930', expected_version=runtime.owner.version)
            assert result.status == 'APPLIED', result.reason
            assert runtime.owner.state['protection']['states']['005930'] == control.owner.state['protection']['states']['005930']
            invariant_roots(before, runtime.owner.state)
        finally:
            await runtime.shutdown()
            await control.shutdown()
            await store.close()
            await control_store.close()
    asyncio.run(scenario())


def test_existing_healthy_stage_pending_and_initial_r_survive_policy_replay(tmp_path, monkeypatch):
    async def scenario():
        engine, _, store, runtime, _, writer = await owned(tmp_path)
        assert_actual_capture_binding(writer)
        try:
            ref = await opened(runtime, 'B1')
            await queued(engine, await observed(runtime, ref, 40, '400000'))
            await opened(runtime, 'S1', 'sell', quantity=10)
            def anchor(state):
                row = state['protection']['states']['005930']
                row.update(current_stage='second', highest_price='12000', breakeven_activated=True,
                           initial_risk_amount='20000', actual_stop_pct='5', pending_stage='third',
                           pending_since=NOW.isoformat(), pending_target_qty=10, pending_filled_qty=0)
                state['protection']['pending_owners']['005930'] = 'S1'
                state['protection']['exit_exempt'].append('OTHER')
                return state
            await runtime.owner.mutate('synthetic-existing-anchor', anchor)
            second = await observed(runtime, ref, 100, '1000000')
            def fail(*args): raise ValueError('synthetic add-on registration failure')
            with monkeypatch.context() as patch:
                patch.setattr('src.execution.safety.protection._registration', fail)
                assert (await queued(engine, second)).protection_status == 'degraded'
            await refresh(writer, monkeypatch, -3)
            before = runtime.owner.state
            result = await runtime.repair_protection('stage', '005930', expected_version=runtime.owner.version)
            assert result.status == 'APPLIED', result.reason
            row = runtime.owner.state['protection']['states']['005930']
            for key, value in {'current_stage': 'second', 'highest_price': '12000',
                'breakeven_activated': True, 'initial_risk_amount': '20000', 'actual_stop_pct': '5',
                'pending_stage': 'third', 'pending_target_qty': 10, 'pending_filled_qty': 0,
                'remaining_quantity': 100}.items():
                assert row[key] == value
            invariant_roots(before, runtime.owner.state)
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_repair_does_not_redeliver_committed_preemptive_effect_or_mutate_other_symbol(tmp_path, monkeypatch):
    async def scenario():
        from src.core.types import Position
        from src.execution.safety.economics import decode_portfolio, encode_portfolio
        from src.execution.safety.protection import decode_protection, encode_protection
        engine, _, store, runtime, writer, _ = await degraded(tmp_path, monkeypatch)
        try:
            def other(state):
                portfolio = decode_portfolio(state['portfolio'])
                position = Position(symbol='000660', quantity=10, avg_price=Decimal('10000'),
                    current_price=Decimal('10000'), strategy='sepa_trend', entry_time=NOW - timedelta(days=10))
                portfolio.positions[position.symbol] = position
                exits = decode_protection(state['protection'], clock=lambda: NOW)
                exits.register_position(position)
                state['portfolio'], state['protection'] = encode_portfolio(portfolio), encode_protection(exits)
                return state
            await runtime.owner.mutate('synthetic-other-stale-holding', other)
            await refresh(writer, monkeypatch, -3)
            before = runtime.owner.state
            effects = [v for v in before['outbox'].values() if v.get('effect_source') == 'intraday_preemptive']
            assert len(effects) == 1 and effects[0]['symbol'] == '000660'
            result = await runtime.repair_protection('with-effect', '005930', expected_version=runtime.owner.version)
            assert result.status == 'APPLIED', result.reason
            invariant_roots(before, runtime.owner.state)
            assert before['protection']['states']['000660'] == runtime.owner.state['protection']['states']['000660']
            assert not engine._event_queue
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('boundary', ['precommit', 'postcommit'])
def test_policy_history_shares_source_commit_and_recovers_after_sql_failure(tmp_path, monkeypatch, boundary):
    async def scenario():
        _, _, store, runtime, writer, _ = await degraded(tmp_path, monkeypatch)
        original = store.commit
        async def reject(expected, state, command):
            if command.startswith('command:risk-complete:'):
                if boundary == 'postcommit':
                    await original(expected, state, command)
                raise OSError('synthetic completion SQL failure')
            return await original(expected, state, command)
        try:
            with monkeypatch.context() as patch:
                patch.setattr(store, 'commit', reject)
                with pytest.raises(OSError):
                    await refresh(writer, monkeypatch, -3)
            await runtime.restore()
            events = runtime.owner.state['protection_replay']['005930']['events']
            assert [event['kind'] for event in events] == (
                ['fill', 'intraday_policy'] if boundary == 'postcommit' else ['fill'])
            before = runtime.owner.state
            result = await runtime.repair_protection('after-failure', '005930', expected_version=runtime.owner.version)
            assert result.status == 'APPLIED', result.reason
            invariant_roots(before, runtime.owner.state)
        finally:
            await store.close()  # source 저장 오류가 있었으므로 정상 shutdown 성공을 주장하지 않는다.
    asyncio.run(scenario())


@pytest.mark.parametrize('damage', ['quote_pending', 'missing_quote', 'missing_source', 'pending_source', 'failed_source'])
def test_policy_input_does_not_heal_unresolved_or_missing_evidence(tmp_path, monkeypatch, damage):
    async def scenario():
        _, _, store, runtime, writer, _ = await degraded(tmp_path, monkeypatch)
        try:
            result = await refresh(writer, monkeypatch, -3)
            await runtime.quote('005930', Decimal('10100'))
            state = runtime.owner.state
            if damage == 'quote_pending':
                state.setdefault('protection_quote_admissions', {})['unresolved'] = {'symbol': '005930'}
            elif damage == 'missing_quote':
                state['protection_replay']['005930']['events'].pop()
            elif damage == 'missing_source':
                state['risk_sources']['records'].pop(result.operation_id)
            elif damage == 'pending_source':
                state['risk_sources']['records'][result.operation_id]['terminal'] = None
            else:
                state['risk_sources']['records'][result.operation_id]['terminal']['receipt']['status'] = 'failed'
            before = deepcopy(state)
            # 실제 repair reducer의 손상된 입력 검사. 손상 source를 live 게시하지 않는다.
            repaired = recovery.reduce_repair(state, 'missing-proof', '005930',
                expected_version=runtime.owner.version, state_version=runtime.owner.version)
            assert repaired['recovery_receipts']['missing-proof']['status'] == 'BLOCKED'
            invariant_roots(before, repaired)
            assert repaired['protection'] == before['protection']
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('damage', ['forged_actual_output', 'candidate_global_gap'])
def test_resealed_continuous_scopes_must_match_actual_policy_not_just_hashes(tmp_path, monkeypatch, damage):
    async def scenario():
        _, _, store, runtime, writer, _ = await degraded(tmp_path, monkeypatch)
        try:
            await refresh(writer, monkeypatch, -3)
            def corrupt(state):
                fill, policy = state['protection_replay']['005930']['events']
                if damage == 'forged_actual_output':
                    policy['after']['protection']['config']['first_exit_pct'] = 12.0
                    state['protection']['config']['first_exit_pct'] = 12.0
                else:
                    fill['after']['protection']['config']['stop_loss_pct'] = 99.0
                    policy['before']['protection']['config']['stop_loss_pct'] = 99.0
                reseal(state)
                return state
            await runtime.owner.mutate('synthetic-coherent-policy-forgery', corrupt)
            before = runtime.owner.state
            result = await runtime.repair_protection('coherent-forgery', '005930', expected_version=runtime.owner.version)
            assert result.status == 'BLOCKED', damage
            invariant_roots(before, runtime.owner.state)
            assert runtime.owner.state['protection'] == before['protection']
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_capture_refuses_earlier_history_erasure_before_appending(tmp_path, monkeypatch):
    async def scenario():
        _, _, store, runtime, writer, _ = await degraded(tmp_path, monkeypatch)
        actual = writer._reduce
        def erase(state, ticket, envelope, version):
            before = deepcopy(state)
            after = actual(state, ticket, envelope, version)
            after['protection_replay'] = {}
            recovery.capture_intraday_transition(before, after, ticket=ticket, envelope=envelope,
                version=version, now=runtime._now())
            return after
        monkeypatch.setattr(writer.sources, '_completion_reducer', erase)
        try:
            before = runtime.owner.state
            with pytest.raises(ValueError, match='history'):
                await refresh(writer, monkeypatch, -3)
            assert runtime.owner.state['protection_replay'] == before['protection_replay']
            assert runtime.owner.state['protection'] == before['protection']
        finally:
            await store.close()
    asyncio.run(scenario())
