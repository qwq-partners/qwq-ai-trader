"""P1 S1′: 보호 에피소드 ID와 실제 owner의 목표 수량을 함께 검증한다."""
import asyncio
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

import pytest

from src.core.types import OrderSide
from src.execution.safety.application import FillObservation
from src.execution.safety.lifecycle import CommandStatus, OrderEvidence, OrderState
from src.execution.safety.requests import RequestValidationError

from test_cross_validator_characterization import freeze  # noqa: F401
from test_execution_command_owner import fixture as command_fixture, held
from test_execution_qualification_publishers import buy
from test_execution_runtime import queued
from test_execution_signal_gateway import install, order, synthetic_home  # noqa: F401
from test_risk_sizing import SYM


async def fixture(tmp_path, monkeypatch):
    return await install(await command_fixture(tmp_path, monkeypatch), monkeypatch)


class UnreadableIntentCache(dict):
    """보호 신호가 기존 캐시를 읽는 회귀도 잡는다."""

    def get(self, *args, **kwargs):
        raise AssertionError('보호 SELL은 자동 intent 캐시를 읽을 수 없다')


@pytest.mark.parametrize('seeded', [False, True])
def test_protective_sell_keeps_episode_identity_without_reading_or_writing_cache(
        tmp_path, monkeypatch, seeded):
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            event = buy(SYM)
            event.metadata['protection_intent_id'] = 'protect-episode-01'
            sell = order(side=OrderSide.SELL)
            previous = {(SYM, OrderSide.SELL, sell.strategy): 'older-unrelated-intent'} if seeded else {}
            f['gateway']._intents = UnreadableIntentCache(previous)
            first, _ = f['gateway']._bind(event, sell, f['clock'][0])
            again, _ = f['gateway']._bind(event, sell, f['clock'][0])
            assert first.intent_id == again.intent_id == 'protect-episode-01'
            assert first.attempt_id != again.attempt_id
            assert f['gateway']._intents == previous
            for request in (first, again):
                f['builder'].validate(request)
                with pytest.raises(RequestValidationError, match='request_fingerprint_mismatch'):
                    f['builder'].validate(replace(request, intent_id='other-episode'))
        finally:
            await f['runtime'].shutdown()
            await f['store'].close()
    asyncio.run(scenario())


@pytest.mark.parametrize('side,tag', [
    (OrderSide.BUY, 'protect-episode-01'),
    (OrderSide.SELL, None),
    (OrderSide.SELL, ''),
    (OrderSide.SELL, 42),
])
def test_buy_and_sell_without_a_nonempty_string_tag_keep_cached_identity(
        tmp_path, monkeypatch, side, tag):
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            event = buy(SYM)
            if tag is not None:
                event.metadata['protection_intent_id'] = tag
            sent = order(side=side)
            first, _ = f['gateway']._bind(event, sent, f['clock'][0])
            again, _ = f['gateway']._bind(event, sent, f['clock'][0])
            assert first.intent_id == again.intent_id
            assert first.intent_id.startswith('gw-i-')
            assert first.attempt_id != again.attempt_id
            assert f['gateway']._intents == {(SYM, side, sent.strategy): first.intent_id}
        finally:
            await f['runtime'].shutdown()
            await f['store'].close()
    asyncio.run(scenario())


@pytest.mark.parametrize('tag', [' protect-episode-01', 'protect-episode-01 ', ' '])
def test_protective_identity_is_not_normalized_before_builder_validation(tmp_path, monkeypatch, tag):
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            event = buy(SYM)
            event.metadata['protection_intent_id'] = tag
            with pytest.raises(RequestValidationError, match='invalid_request_identity'):
                f['gateway']._bind(event, order(side=OrderSide.SELL), f['clock'][0])
            assert f['gateway']._intents == {}
        finally:
            await f['runtime'].shutdown()
            await f['store'].close()
    asyncio.run(scenario())


async def apply_take_profit_fill(f, result):
    """합성 브로커 관측만 주입하고 최종성·체결·보호 적용은 실제 owner로 수행한다."""
    ref, now = result.order_ref, f['clock'][0]
    evidence = OrderEvidence(
        ref, SYM, 'sell', 10, 10, D('112000'), 0, 0, OrderState.FINAL_FILLED,
        complete=True, supported_finality=True, source_contract='synthetic-test',
        observed_at=now, request_started_at=now - timedelta(seconds=1),
        query_scope={'account_scope': 'test-scope', 'market': 'KR', 'exchange': 'KRX',
                     'start_date': now.date().isoformat(), 'end_date': now.date().isoformat(),
                     'tr_id': 'TTTC0081R', 'query_kind': 'all', 'session': 'regular'})
    assert await f['runtime'].lifecycle.reconcile(result.attempt_id, evidence)
    observation = FillObservation(
        'test-scope', 'KR', ref.order_date, 'KRX', ref.order_no, SYM, 'SELL', 10,
        D('112000'), org_no=ref.org_no)
    receipt = await queued(f['engine'], observation)
    assert (receipt.status, receipt.protection_status) == ('APPLIED', 'ready')


@pytest.mark.parametrize('next_episode,reason,price', [
    ('protect-stop-02', '손절', D('9000')),
    ('protect-eod-02', 'EOD 전량 청산', D('11000')),
])
def test_filled_ten_share_profit_does_not_cap_next_ninety_share_episode(
        tmp_path, monkeypatch, freeze, next_episode, reason, price):
    async def scenario():
        freeze(11, 0, day=18)
        f = await fixture(tmp_path, monkeypatch)
        try:
            await held(f, quantity=100)
            # 공통 보유 fixture는 주문 예약용이다. 체결 적용에는 측정 원가도 필요하다.
            def measured_basis(state):
                state['risk']['cost_basis_remaining'][SYM] = '1000000'
                state['risk']['buy_fee_remaining'][SYM] = '141'
                return state
            await f['runtime'].owner.mutate('synthetic-measured-buy-basis', measured_basis)
            decision = await f['runtime'].quote(SYM, D('11200'), intent_id='protect-profit-01')
            assert decision[:2] == ('sell_partial', 10)
            event = buy(SYM)
            event.metadata['protection_intent_id'] = 'protect-profit-01'
            first = await f['gateway'].submit(
                event, replace(order(side=OrderSide.SELL, price=D('11200')), reason='익절'), None)
            assert first.status is CommandStatus.ACKNOWLEDGED
            state = f['runtime'].owner.state
            first_attempt = state['attempts'][first.attempt_id]
            assert first_attempt['intent_id'] == state['protection']['pending_owners'][SYM] == 'protect-profit-01'
            assert state['intents']['protect-profit-01']['target_quantity'] == 10
            await apply_take_profit_fill(f, first)
            state = f['runtime'].owner.state
            assert state['attempts'][first.attempt_id]['applied_quantity'] == 10
            assert state['portfolio']['positions'][SYM]['quantity'] == 90
            assert state['protection']['states'][SYM]['pending_stage'] is None
            assert SYM not in state['protection']['pending_owners']
            if next_episode == 'protect-stop-02':
                decision = await f['runtime'].quote(SYM, price, intent_id=next_episode)
                assert decision[:2] == ('sell_all', 90)
            # EOD의 정책 결정/발행은 S2 소유다. 이 시험은 게이트웨이부터 검증한다.
            event.metadata['protection_intent_id'] = next_episode
            f['broker']._session.response.data['output']['ODNO'] = '1234567891'
            second = await f['gateway'].submit(
                event, replace(order(side=OrderSide.SELL, quantity=90, price=price), reason=reason), None)
            assert second.status is CommandStatus.ACKNOWLEDGED
            state = f['runtime'].owner.state
            second_attempt = state['attempts'][second.attempt_id]
            assert second_attempt['intent_id'] == next_episode
            assert second_attempt['quantity'] == second_attempt['reserved_quantity'] == 90
            assert state['intents'][next_episode]['target_quantity'] == 90
            assert state['intents']['protect-profit-01']['target_quantity'] == 10
            assert first.attempt_id != second.attempt_id
        finally:
            await f['runtime'].shutdown()
            await f['store'].close()
    asyncio.run(scenario())
