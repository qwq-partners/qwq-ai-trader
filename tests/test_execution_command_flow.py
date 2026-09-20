"""실제 bound 요청→fake ACK→실제 누적체결 큐→예약/R의 교차 인수.

조회 최종성/시작 허가는 명시 합성이다. 공식 KIS 계약·실 API 인수가 아니다.
"""
import asyncio
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal as D

import pytest

from src.execution.safety.application import FillObservation
from src.execution.safety.commands import CommandValidationError
from src.execution.safety import risk_policy as p
from src.execution.safety.lifecycle import CommandStatus, OrderEvidence, OrderState
from src.execution.safety.transport import GuardedKISTransport
from test_execution_command_owner import fixture
from test_execution_runtime import queued
from test_execution_risk_policy import snapshot


@pytest.mark.parametrize('origin', ['automatic', 'user'])
@pytest.mark.parametrize('sector_metadata', [{}, {'sector': ''}, {'sector': '철강'}])
def test_bound_request_partial_full_fill_and_initial_r_share_one_owner(tmp_path, monkeypatch, origin, sector_metadata):
    async def scenario():
        policy = replace(snapshot(p).policy, max_positions_per_sector=1)
        f = await fixture(tmp_path, monkeypatch, origin=origin, policy=policy)
        commands, runtime = f['commands'], f['runtime']
        try:
            request = f['request']()
            context = f['entry'](request)
            await f['quote'](request)
            if origin == 'automatic': await f['facts'](request, sector='반도체')
            prepared = await commands.prepare(request, context, sector='반도체')
            binding = deepcopy(prepared['request_binding'])
            transport = GuardedKISTransport(f['broker'], request_builder=f['builder'])
            ack = await commands.dispatch(request, context, transport)
            assert ack.status is CommandStatus.ACKNOWLEDGED
            ref = ack.order_ref
            assert ref.org_no == '12345'
            async def fill(quantity):
                total = D('10000') * quantity
                final = quantity == request.quantity
                now = f['clock'][0]
                evidence = OrderEvidence(ref, request.symbol, 'buy', request.quantity, quantity,
                    total, request.quantity - quantity, 0,
                    OrderState.FINAL_FILLED if final else OrderState.PARTIAL,
                    complete=True, supported_finality=final, source_contract='synthetic-flow-only',
                    observed_at=now, request_started_at=now,
                    query_scope=dict(account_scope=ref.account_scope, market='KR', exchange='KRX',
                        start_date=ref.order_date, end_date=ref.order_date, tr_id='TTTC0081R',
                        query_kind='all', session='regular'))
                assert await runtime.lifecycle.reconcile(request.attempt_id, evidence)
                observation = FillObservation(ref.account_scope, 'KR', ref.order_date, 'KRX',
                    ref.order_no, request.symbol, 'BUY', quantity, total,
                    org_no=ref.org_no, parent_order_no=ref.parent_order_no,
                    metadata={'registration_params': {'stop_loss_pct': 5}, **sector_metadata})
                assert (await queued(f['engine'], observation)).status == 'APPLIED'
                return observation
            async def reject_another_same_sector():
                other = f['request']('B', symbol='000660', strategy='sepa_trend')
                await f['quote'](other)
                await f['facts'](other, sector='반도체')
                automatic = f['authority'].automatic(other.symbol, 'buy', other.strategy)
                with pytest.raises(CommandValidationError, match='sector_limit'):
                    await commands.prepare(other, automatic, sector='반도체')
                assert other.attempt_id not in runtime.owner.state['attempts']
            partial = await fill(4)
            assert f['engine'].portfolio.positions[request.symbol].sector == '반도체'
            await reject_another_same_sector()
            row = runtime.owner.state['attempts'][request.attempt_id]
            assert row['reserved_quantity'] == 6
            assert D(row['reserved_cash']) == D('60900')
            assert D(row['reserved_exposure']) == D('60000')
            # (100000 + 매수fee14) × 5% = 5000.7, 잔여6/10의 원단위 CEILING.
            assert row['reserved_planned_risk'] == ('3001' if origin == 'automatic' else None)
            assert row['request_binding'] == binding
            assert f['engine'].portfolio.cash == D('1959994')
            assert (await queued(f['engine'], partial)).status == 'ALREADY_APPLIED'
            await runtime.restore()
            assert runtime.owner.state['attempts'][request.attempt_id] == row
            assert (await commands.dispatch(request, context, transport)).status is CommandStatus.NOT_SENT
            assert len(f['broker']._session.posts) == 1
            await fill(10)
            await reject_another_same_sector()
            state = runtime.owner.state
            final = state['attempts'][request.attempt_id]
            assert final['reserved_quantity'] == 0
            assert D(final['reserved_cash']) == D(final['reserved_exposure']) == 0
            assert final['reserved_planned_risk'] == ('0' if origin == 'automatic' else None)
            assert final['request_binding'] == binding
            assert not state['entry_policy_effects']['pending_sectors']
            assert f['engine'].portfolio.cash == D('1899986')
            assert f['engine'].portfolio.daily_trades == 1
            assert f['engine'].portfolio.positions[request.symbol].sector == '반도체'
            assert f['exits'].get_state(request.symbol).remaining_quantity == 10
            receipt = await runtime.finalize_initial_r('flow-initial-r', ref.key,
                expected_version=runtime.owner.version,
                initial_stop_evidence_id=state['initial_stop_evidence'][ref.key]['evidence_id'],
                finality_evidence_id=final['accepted_finality_evidence_id'])
            assert receipt.status == 'APPLIED'
            assert D(runtime.owner.state['lots'][ref.key]['initial_r']) == D('5000')
            assert runtime.owner.state['portfolio'] == state['portfolio']
            assert len(f['broker']._session.posts) == 1
        finally:
            await runtime.shutdown()
            await f['store'].close()
    asyncio.run(scenario())
