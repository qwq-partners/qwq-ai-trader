"""실제 체결 큐의 cash/exposure/계획위험 해제. 송신 허가 인수와 구분한다."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal

import pytest

from src.execution.safety.economics import reduce_economics
from src.execution.safety.application import FillDelta
from test_execution_runtime import setup, opened, observed, queued


async def reserved(runtime, *, measured=True):
    ref = await opened(runtime, 'B1')
    def bind(state):
        # 명시 합성 예약 fixture. 실제 prepared request/claim 배선은 별도 시험이다.
        row = state['attempts']['B1']
        row['sector'] = '반도체'
        row['request_binding'] = {'fixture': 'resource-release-only', 'sector': '반도체'}
        row['reserved_exposure'] = '1000000'
        row['reserved_planned_risk'] = '50007.05' if measured else None
        state['entry_policy_effects'] = {'pending_sectors': {'005930': '반도체', 'OTHER': '철강'},
                                         'sidecar_active': True}
        return state
    await runtime.owner.mutate('synthetic-resources', bind)
    return ref


@pytest.mark.parametrize('measured', [True, False])
def test_real_queue_partial_full_duplicate_and_reopen_resource_conservation(tmp_path, measured):
    async def scenario():
        engine, exits, store, runtime = await setup(tmp_path)
        try:
            ref = await reserved(runtime, measured=measured)
            first = await observed(runtime, ref, 40, '400000')
            receipt = await queued(engine, first)
            assert receipt.status == 'APPLIED'
            row = runtime.owner.state['attempts']['B1']
            assert (row['reserved_quantity'], row['reserved_cash'], row['reserved_exposure']) == (60, '600120', '600000')
            assert row['reserved_planned_risk'] == ('30005' if measured else None)
            assert runtime.owner.state['entry_policy_effects']['pending_sectors']['005930'] == '반도체'
            duplicate = await queued(engine, first)
            assert duplicate.status == 'ALREADY_APPLIED'
            assert runtime.owner.state['attempts']['B1'] == row
            await runtime.restore()
            assert runtime.owner.state['attempts']['B1'] == row
            last = await observed(runtime, ref, 100, '1000000')
            assert (await queued(engine, last)).status == 'APPLIED'
            final = runtime.owner.state['attempts']['B1']
            assert final['reserved_quantity'] == 0
            assert Decimal(final['reserved_exposure']) == Decimal(final['reserved_cash']) == 0
            assert final['reserved_planned_risk'] == ('0' if measured else None)
            assert runtime.owner.state['entry_policy_effects'] == {
                'pending_sectors': {'OTHER': '철강'}, 'sidecar_active': True}
            assert engine.portfolio.positions['005930'].quantity == 100
        finally:
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['other_sector', 'sidecar'])
def test_full_fill_cannot_clear_foreign_sector_or_change_sidecar(tmp_path, change):
    async def scenario():
        engine, exits, store, runtime = await setup(tmp_path)
        try:
            ref = await reserved(runtime)
            observation = await observed(runtime, ref, 100, '1000000')
            original = runtime.owner.reducer
            def tamper(state, fact, delta):
                result = original(state, fact, delta)
                candidate = deepcopy(result.state)
                effects = candidate['entry_policy_effects']
                if change == 'other_sector': effects['pending_sectors'].pop('OTHER')
                else: effects['sidecar_active'] = False
                return replace(result, state=candidate)
            runtime.owner.reducer = tamper
            before = runtime.owner.state
            assert (await queued(engine, observation)).status == 'FAILED'
            assert runtime.owner.state['entry_policy_effects'] == before['entry_policy_effects']
            assert runtime.owner.state['portfolio'] == before['portfolio']
        finally: await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('corrupt', ['exposure', 'risk', 'unmeasured_to_zero', 'missing_pair'])
def test_fill_write_set_rejects_excessive_or_unmeasured_resource_release(tmp_path, corrupt):
    async def scenario():
        engine, exits, store, runtime = await setup(tmp_path)
        try:
            ref = await reserved(runtime, measured=corrupt != 'unmeasured_to_zero')
            observation = await observed(runtime, ref, 40, '400000')
            original = runtime.owner.reducer
            def tamper(state, fact, delta):
                result = original(state, fact, delta)
                candidate = deepcopy(result.state)
                row = candidate['attempts']['B1']
                if corrupt == 'missing_pair':
                    del row['reserved_planned_risk']
                else:
                    row['reserved_exposure' if corrupt == 'exposure' else 'reserved_planned_risk'] = '0'
                return replace(result, state=candidate)
            runtime.owner.reducer = tamper
            before = runtime.owner.state
            result = await queued(engine, observation)
            assert result.status == 'FAILED'
            assert runtime.owner.state['attempts'] == before['attempts']
            assert runtime.owner.state['portfolio'] == before['portfolio']
        finally:
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('invalid', [None, 1, 'NaN', '-1'])
def test_invalid_exposure_reservation_does_not_apply_economics(tmp_path, invalid):
    async def scenario():
        engine, exits, store, runtime = await setup(tmp_path)
        try:
            ref = await reserved(runtime)
            observation = await observed(runtime, ref, 40, '400000')
            state = runtime.owner.state
            state['attempts']['B1']['reserved_exposure'] = invalid
            before = deepcopy(state)
            with pytest.raises(ValueError):
                reduce_economics(state, observation, FillDelta(40, Decimal('400000'), Decimal('0')),
                                 now=runtime._now())
            assert state == before
        finally:
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('changed', [
    {'reserved_exposure': '1', 'reserved_planned_risk': None},
    {'reserved_exposure': '0', 'reserved_planned_risk': '1'},
    {'reserved_exposure': '0'},
    {'reserved_exposure': '0', 'reserved_planned_risk': 'NaN'},
])
def test_remaining_resource_cannot_pass_day_quiescence_or_initial_r(tmp_path, changed):
    from src.execution.safety.day_recovery import unresolved_reason
    from test_execution_initial_r import candidate, finalize
    async def scenario():
        _, _, store, _, ref, state = await candidate(tmp_path)
        try:
            assert unresolved_reason(state) == ''
            state['attempts']['B1'].update(changed)
            before = deepcopy(state)
            assert unresolved_reason(state) in {'remaining_reservation', 'invalid_reservation'}
            result = finalize(state, ref)
            assert result['recovery_receipts']['r1']['status'] == 'BLOCKED'
            result.pop('recovery_receipts')
            assert result == before
        finally: await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('risk', [None, '0'])
def test_exhausted_new_resources_allow_day_quiescence_and_initial_r(tmp_path, risk):
    from src.execution.safety.day_recovery import unresolved_reason
    from test_execution_initial_r import candidate, finalize
    async def scenario():
        _, _, store, _, ref, state = await candidate(tmp_path)
        try:
            state['attempts']['B1'].update(reserved_exposure='0', reserved_planned_risk=risk)
            before = deepcopy(state['attempts'])
            assert unresolved_reason(state) == ''
            result = finalize(state, ref)
            assert result['recovery_receipts']['r1']['status'] == 'APPLIED'
            assert result['attempts'] == before
        finally: await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('risk', ['1', 'NaN'])
def test_initial_r_cannot_ignore_sibling_risk_reservation(tmp_path, risk):
    from test_execution_initial_r import candidate, finalize
    async def scenario():
        _, _, store, _, ref, state = await candidate(tmp_path)
        try:
            sibling = {**deepcopy(state['attempts']['B1']), 'attempt_id': 'B2',
                       'reserved_exposure': '0', 'reserved_planned_risk': risk}
            state['attempts']['B2'] = sibling
            state['intents']['B1']['attempt_ids'].append('B2')
            before = deepcopy(state)
            result = finalize(state, ref)
            assert result['recovery_receipts']['r1']['status'] == 'BLOCKED'
            result.pop('recovery_receipts')
            assert result == before
        finally: await store.close()
    asyncio.run(scenario())
