"""미claim prepared 시도의 원자적 종료(abandon_candidate).

거부되어야 하는 경우를 먼저 고정한다. 가드가 하나라도 빠지면 이미 송신됐을 수
있는 시도의 예약을 푸는 경로가 된다.
"""
import asyncio
from copy import deepcopy
from pathlib import Path

import pytest

from src.execution.safety.lifecycle import (
    CommandResult, CommandStatus, OrderLifecycleCoordinator, OrderRef,
)
from src.execution.safety.runtime import KRExecutionRuntime
from test_execution_lifecycle import MemoryOwner
from test_execution_runtime import NOW, setup


@pytest.fixture(autouse=True)
def synthetic_home(tmp_path, monkeypatch):
    """보호·CV 파일이 운영 캐시(~/.cache/ai_trader)를 건드리지 않게 한다."""
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)


SYMBOL = '005930'
SECTOR = '반도체'
CASH = '1000200'
EXPOSURE = '1000000'
PLANNED_RISK = '5000'
SIBLING_REF = OrderRef('scope', 'KR', '2026-09-18', 'KRX', '0001', 'branch')


async def bind_reservation(owner, attempt_id, **changes):
    """commands.prepare 가 싣는 bound 예약 2항을 같은 형식으로 주입한다."""
    def reduce(state):
        attempt = state['attempts'][attempt_id]
        attempt['reserved_exposure'] = EXPOSURE
        attempt['reserved_planned_risk'] = PLANNED_RISK
        for key, value in changes.items():
            if value is None and key in ('reserved_planned_risk', 'evidence_conflict'):
                attempt.pop(key, None)
            else:
                attempt[key] = value
        return state
    await owner.mutate('seed:' + attempt_id, reduce)


async def prepared(**changes):
    """미claim prepared submit 하나만 있는 owner. 예약 4항을 모두 싣는다."""
    owner = MemoryOwner()
    owner.state = {'attempts': {}, 'intents': {},
                   'entry_policy_effects': {'pending_sectors': {SYMBOL: SECTOR}}}
    life = OrderLifecycleCoordinator(owner, clock=lambda: NOW)
    await life.prepare('I-1', 'A-1', 10, SYMBOL, 'buy', reserved_cash=CASH, strategy='sepa_trend')
    await bind_reservation(owner, 'A-1', **changes)
    return owner, life


def reservations(attempt):
    return (attempt['reserved_quantity'], attempt['reserved_cash'],
            attempt['reserved_exposure'], attempt['reserved_planned_risk'])


@pytest.mark.parametrize('state', ['submitting', 'prepared'])
def test_abandon_rejects_claimed_attempt(state):
    """claim 된 시도는 이미 POST 가 시작됐을 수 있어 예약을 풀 수 없다.

    복구된 checkpoint 에서 state 와 claim_id 가 어긋나더라도 claim_id 하나만으로
    '송신을 시작한 적 없음'을 판정한다.
    """
    async def scenario():
        owner, life = await prepared()
        assert await life.claim('A-1', 'sender-1')
        if state == 'prepared':
            await bind_reservation(owner, 'A-1', state='prepared', status='prepared')
        before = deepcopy(owner.state)
        assert await life.abandon_candidate('A-1', reason='claim_not_available') is False
        assert owner.state == before
        attempt = owner.state['attempts']['A-1']
        assert attempt['state'] == state and attempt['claim_id'] == 'sender-1'
        assert reservations(attempt) == (10, CASH, EXPOSURE, PLANNED_RISK)
    asyncio.run(scenario())


@pytest.mark.parametrize('field,value', [
    ('observed_quantity', 3), ('applied_quantity', 3), ('evidence_conflict', True)])
def test_abandon_rejects_settled_or_conflicted_attempt(field, value):
    """체결 관측 선도착·증거 충돌은 경제적 사실이라 종료가 지울 수 없다."""
    async def scenario():
        owner, life = await prepared(**{field: value})
        before = deepcopy(owner.state)
        assert await life.abandon_candidate('A-1', reason='dispatch_failed') is False
        assert owner.state == before
        assert reservations(owner.state['attempts']['A-1']) == (10, CASH, EXPOSURE, PLANNED_RISK)
    asyncio.run(scenario())


@pytest.mark.parametrize('field,value', [
    ('command_status', 'unknown'), ('order_ref', SIBLING_REF.to_dict()),
    ('state', 'final_filled')])
def test_abandon_rejects_attempt_with_send_evidence(field, value):
    """명령 결과·브로커 주문번호가 남아 있으면 미송신 증거가 아니다.

    state 만 prepared 가 아닌 복구 행(claim_id·command_status·order_ref 는 비어 있음)도
    같은 이유로 거부한다 — state 가드가 다른 절에 가려 빠져도 모르는 일이 없게 한다.
    """
    async def scenario():
        owner, life = await prepared(**{field: value})
        before = deepcopy(owner.state)
        assert await life.abandon_candidate('A-1', reason='dispatch_failed') is False
        assert owner.state == before
    asyncio.run(scenario())


@pytest.mark.parametrize('reason', ['', None])
def test_abandon_requires_reason(reason):
    """사유 없는 종료는 '보냈는데 거부됨'과 구분할 수 없어 거부한다."""
    async def scenario():
        owner, life = await prepared()
        before = deepcopy(owner.state)
        with pytest.raises(ValueError):
            await life.abandon_candidate('A-1', reason=reason)
        assert owner.state == before
    asyncio.run(scenario())


def test_abandon_reducer_failure_leaves_state_unchanged():
    """가드 통과 뒤 reducer 안에서 던져도 단일 mutate 라 무변경으로 남는다."""
    async def scenario():
        owner, life = await prepared(reserved_planned_risk=None)
        before = deepcopy(owner.state)
        with pytest.raises(ValueError, match='incomplete_reservation_pair'):
            await life.abandon_candidate('A-1', reason='dispatch_failed')
        assert owner.state == before
        assert owner.state['entry_policy_effects']['pending_sectors'] == {SYMBOL: SECTOR}
    asyncio.run(scenario())


def test_record_result_cannot_finish_unclaimed_attempt():
    """기존 경로 재사용 불가: claim_id 불일치로 무변경이다."""
    async def scenario():
        owner, life = await prepared()
        before = deepcopy(owner.state)
        assert await life.record_result('A-1', 'sender-x', CommandResult(
            CommandStatus.NOT_SENT, 'A-1')) is False
        assert owner.state == before
        assert reservations(owner.state['attempts']['A-1']) == (10, CASH, EXPOSURE, PLANNED_RISK)
    asyncio.run(scenario())


def test_abandon_unblocks_intent_retry():
    """종료 전에는 같은 intent 의 재시도가 영구 차단된다."""
    async def scenario():
        owner, life = await prepared()
        assert life.replacement_quantity('I-1') == 0
        with pytest.raises(ValueError, match='previous attempt unresolved or target exceeded'):
            await life.prepare('I-1', 'A-2', 10, SYMBOL, 'buy', reserved_cash=CASH)
        before_version = owner.state['attempts']['A-1']['version']
        assert await life.abandon_candidate('A-1', reason='claim_not_available') is True
        attempt = owner.state['attempts']['A-1']
        # 다른 전이와 같이 상태 변경과 version 증가가 한 쌍이다
        assert attempt['version'] == before_version + 1
        assert attempt['state'] == attempt['status'] == 'final_rejected'
        assert attempt['command_status'] == 'not_sent'
        assert attempt['reason_code'] == 'claim_not_available'
        assert attempt['claim_id'] is None
        assert reservations(attempt) == (0, '0', '0', '0')
        assert life.replacement_quantity('I-1') == 10
        await life.prepare('I-1', 'A-2', 10, SYMBOL, 'buy', reserved_cash=CASH)
        assert owner.state['attempts']['A-2']['state'] == 'prepared'
    asyncio.run(scenario())


@pytest.mark.parametrize('sibling,remains', [(False, False), (True, True)])
def test_abandon_clears_only_settled_pending_sector(sibling, remains):
    """같은 종목의 미해결 submit 이 남아 있으면 pending 섹터를 지우지 않는다."""
    async def scenario():
        owner, life = await prepared()
        if sibling:
            await life.prepare('I-2', 'B-1', 5, SYMBOL, 'buy', reserved_cash='500100')
            assert await life.claim('B-1', 'sender-2')
            await life.record_result('B-1', 'sender-2', CommandResult(
                CommandStatus.ACKNOWLEDGED, 'B-1', SIBLING_REF))
            assert owner.state['attempts']['B-1']['state'] == 'open'
        assert await life.abandon_candidate('A-1', reason='claim_not_available') is True
        sectors = owner.state['entry_policy_effects']['pending_sectors']
        assert (SYMBOL in sectors) is remains
    asyncio.run(scenario())


def test_abandon_after_restore_releases_bound_reservations(tmp_path):
    """checkpoint 복구 뒤에도 같은 가드로 예약 4항이 0 이 된다."""
    async def scenario():
        engine, exits, store, runtime = await setup(tmp_path)
        await runtime.lifecycle.prepare('I-1', 'A-1', 10, SYMBOL, 'buy',
                                        reserved_cash=CASH, strategy='sepa_trend')
        await bind_reservation(runtime.owner, 'A-1')
        second = KRExecutionRuntime(store, engine, exits, clock=lambda: NOW)
        await second.restore()
        assert second.owner.state['attempts']['A-1']['claim_id'] is None
        assert await second.lifecycle.abandon_candidate('A-1', reason='dispatch_failed') is True
        attempt = second.owner.state['attempts']['A-1']
        assert attempt['state'] == 'final_rejected' and attempt['reason_code'] == 'dispatch_failed'
        assert reservations(attempt) == (0, '0', '0', '0')
        assert second.lifecycle.replacement_quantity('I-1') == 10
    asyncio.run(scenario())
