"""미claim 자식 명령(cancel/modify)의 종료 간선 — S4-1(결정 ⑤·계약 3).

자식 행의 `order_ref` 는 prepare 가 싣는 **부모의** 식별자라 SUBMIT 용 가드에
그대로 걸린다. 그래서 claim 이전에 실패한 취소는 영구히 prepared 로 남아 같은
부모의 재취소와 같은 종목의 새 SUBMIT 을 함께 잠근다. 이 파일은 그 종료를
고정하되, **송신 증거가 하나라도 있으면 여전히 거부한다**는 쪽을 더 많이 고정한다.

이 단계는 설치가 아니다 — 제품의 취소 호출자는 여전히 0건이고, 여기의 증거는
전부 합성 startup 허가 위의 것이다. ACK/UNKNOWN 자식의 종착역은 S4 가 풀지
않는다(취소·체결 최종성의 증거 계약 뒤).
"""
import asyncio
from copy import deepcopy
from pathlib import Path

import pytest

from src.core.types import Order, OrderSide, OrderType
from src.execution.safety.commands import CommandValidationError
from src.execution.safety.day_recovery import unresolved_reason
from src.execution.safety.lifecycle import (
    CommandKind, CommandResult, CommandStatus, OrderEvidence, OrderLifecycleCoordinator,
    OrderRef, OrderState,
)
from src.execution.safety.requests import CancelParent
from src.execution.safety.runtime import KRExecutionRuntime
from src.execution.safety.transport import GuardedKISTransport

from test_execution_abandon_candidate import CASH, SECTOR, SYMBOL, prepared, reservations
from test_execution_command_owner import fixture as command_fixture
from test_execution_decision_facts import nominal
from test_execution_runtime import NOW, setup


@pytest.fixture(autouse=True)
def synthetic_home(tmp_path, monkeypatch):
    """보호·CV 파일이 운영 캐시(~/.cache/ai_trader)를 건드리지 않게 한다."""
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)


PARENT_REF = OrderRef('scope', 'KR', '2026-09-18', 'KRX', '0009', 'branch')
FOREIGN_REF = OrderRef('scope', 'KR', '2026-09-18', 'KRX', '0010', 'branch')
# 합성 조회 범위. 실제 KIS 취소 종결 계약을 입증했다는 뜻이 아니다.
SCOPE = {'account_scope': 'scope', 'market': 'KR', 'exchange': 'KRX',
         'start_date': '2026-09-18', 'end_date': '2026-09-18', 'tr_id': 'TTTC0081R',
         'query_kind': 'all', 'session': 'regular'}


async def seed(owner, attempt_id, **changes):
    """복구된 checkpoint 처럼 한 행의 칸만 직접 채운다(예약 2항은 건드리지 않는다)."""
    def reduce(state):
        state['attempts'][attempt_id].update(changes)
        return state
    await owner.mutate('seed:' + attempt_id, reduce)


async def with_child(kind=CommandKind.CANCEL, child='C-1'):
    """ACK 된 부모 A-1 위에 미claim 자식 하나를 둔 owner."""
    owner, life = await prepared()
    assert await life.claim('A-1', 'sender-1')
    assert await life.record_result('A-1', 'sender-1', CommandResult(
        CommandStatus.ACKNOWLEDGED, 'A-1', PARENT_REF))
    assert owner.state['attempts']['A-1']['state'] == 'open'
    await life.prepare('I-1', child, 10, SYMBOL, 'buy', command=kind,
                       parent_attempt_id='A-1', order_ref=PARENT_REF)
    return owner, life


def cancelled_evidence():
    """부모를 터미널로 만드는 합성 종결 증거(전량 취소·체결 0)."""
    return OrderEvidence(PARENT_REF, SYMBOL, 'buy', 10, 0, 0, 0, 10,
                         OrderState.FINAL_CANCELLED, complete=True, supported_finality=True,
                         source_contract='synthetic-unit', observed_at=NOW,
                         request_started_at=NOW, query_scope=SCOPE)


# ── 종료가 열리는 쪽 ────────────────────────────────────────────────────

@pytest.mark.parametrize('kind', [CommandKind.CANCEL, CommandKind.MODIFY])
def test_an_unclaimed_child_command_can_be_closed(kind):
    """claim·command_status·command_ref 가 모두 비어 있으면 미송신 증거다(오늘 False)."""
    async def scenario():
        owner, life = await with_child(kind)
        assert await life.abandon_candidate('C-1', reason='dispatch_failed') is True
        child = owner.state['attempts']['C-1']
        assert child['state'] == child['status'] == 'final_rejected'
        assert child['command_status'] == 'not_sent'
        assert child['reason_code'] == 'dispatch_failed'
        assert child['claim_id'] is None
        # 자식의 order_ref 는 부모 식별자라 종료가 지우지 않는다.
        assert child['order_ref'] == PARENT_REF.to_dict()
    asyncio.run(scenario())


def test_closing_a_child_leaves_the_parent_untouched():
    """계약 3 — 부모 행의 state·version·예약 4항·pending sector 가 글자 하나 안 바뀐다."""
    async def scenario():
        owner, life = await with_child()
        before = deepcopy(owner.state['attempts']['A-1'])
        sectors_before = deepcopy(owner.state['entry_policy_effects']['pending_sectors'])
        assert await life.abandon_candidate('C-1', reason='dispatch_failed') is True
        parent = owner.state['attempts']['A-1']
        assert parent == before
        assert (parent['state'], parent['version']) == (before['state'], before['version'])
        assert reservations(parent) == (10, CASH, before['reserved_exposure'],
                                        before['reserved_planned_risk'])
        assert owner.state['entry_policy_effects']['pending_sectors'] == sectors_before
    asyncio.run(scenario())


def test_closing_a_child_reopens_the_next_cancel():
    """종료 전에는 같은 부모의 두 번째 취소가 영구 차단된다."""
    async def scenario():
        owner, life = await with_child()
        with pytest.raises(ValueError, match='previous child command unresolved'):
            await life.prepare('I-1', 'C-2', 10, SYMBOL, 'buy', command=CommandKind.CANCEL,
                               parent_attempt_id='A-1', order_ref=PARENT_REF)
        assert await life.abandon_candidate('C-1', reason='dispatch_failed') is True
        await life.prepare('I-1', 'C-2', 10, SYMBOL, 'buy', command=CommandKind.CANCEL,
                           parent_attempt_id='A-1', order_ref=PARENT_REF)
        assert owner.state['attempts']['C-2']['state'] == 'prepared'
    asyncio.run(scenario())


def test_closing_a_child_clears_the_day_transition_block():
    """[일자 전환] 부모를 터미널로 만든 표본에서만 자식이 마지막 잔류다.

    부모가 비터미널이면 `unresolved_submit` 이 먼저 나와 단언이 무의미하다.
    """
    async def scenario():
        owner, life = await with_child()
        assert await life.reconcile('A-1', cancelled_evidence(), applied_quantity=0)
        assert owner.state['attempts']['A-1']['state'] == 'final_cancelled'
        assert unresolved_reason(owner.state) == 'unresolved_child_command'
        assert await life.abandon_candidate('C-1', reason='dispatch_failed') is True
        assert unresolved_reason(owner.state) == ''
    asyncio.run(scenario())


def test_a_settled_parent_keeps_its_pending_sector_after_the_child_closes():
    """[섹터] 현행 관측: `clear_settled_pending_sector` 는 kind≠submit 이면 즉시 반환한다.

    부모의 소진 시점에는 자식이 미해결이라 섹터가 남고, 자식 종료는 그 정리를
    다시 부르지 않는다 — 이 표본에서 pending sector 는 풀리지 않은 채로 남는다.
    옳은 동작의 정의가 아니라 오늘의 관측이다(unverified).
    """
    async def scenario():
        owner, life = await with_child()
        assert await life.reconcile('A-1', cancelled_evidence(), applied_quantity=0)
        sectors = owner.state['entry_policy_effects']['pending_sectors']
        assert sectors == {SYMBOL: SECTOR}
        assert await life.abandon_candidate('C-1', reason='dispatch_failed') is True
        assert owner.state['entry_policy_effects']['pending_sectors'] == {SYMBOL: SECTOR}
    asyncio.run(scenario())


# ── 종료가 계속 닫혀 있는 쪽 ────────────────────────────────────────────

@pytest.mark.parametrize('field,value', [
    ('claim_id', 'cancel-owner'),
    ('command_status', 'unknown'),
    ('command_status', 'acknowledged'),
    ('command_ref', FOREIGN_REF.to_dict()),
    ('observed_quantity', 3),
    ('applied_quantity', 3),
    ('evidence_conflict', True),
    ('state', 'reconciling'),
])
def test_child_close_rejects_any_send_evidence(field, value):
    """자식 자신의 송신 증거는 claim_id·command_status·command_ref 셋이다.

    체결 관측·증거 충돌·prepared 아닌 state 도 SUBMIT 과 같은 이유로 거부한다.
    """
    async def scenario():
        owner, life = await with_child()
        await seed(owner, 'C-1', **{field: value})
        before = deepcopy(owner.state)
        assert await life.abandon_candidate('C-1', reason='dispatch_failed') is False
        assert owner.state == before
    asyncio.run(scenario())


@pytest.mark.parametrize('forged', [FOREIGN_REF.to_dict(), None])
def test_child_close_rejects_a_forged_order_ref(forged):
    """면제는 '부모 것과 같을 때'로 한정한다 — 다른 주문번호가 실린 행은 거부한다."""
    async def scenario():
        owner, life = await with_child()
        await seed(owner, 'C-1', order_ref=forged)
        before = deepcopy(owner.state)
        assert await life.abandon_candidate('C-1', reason='dispatch_failed') is False
        assert owner.state == before
    asyncio.run(scenario())


def test_an_acknowledged_child_is_never_closed():
    """실제 ACK 경로(RECONCILING·command_ref)를 거친 자식은 잔류한다 — S4 가 풀지 않는다."""
    async def scenario():
        owner, life = await with_child()
        assert await life.claim('C-1', 'cancel-owner')
        assert await life.record_result('C-1', 'cancel-owner', CommandResult(
            CommandStatus.ACKNOWLEDGED, 'C-1', FOREIGN_REF))
        child = owner.state['attempts']['C-1']
        assert (child['state'], child['command_ref']) == ('reconciling', FOREIGN_REF.to_dict())
        before = deepcopy(owner.state)
        assert await life.abandon_candidate('C-1', reason='dispatch_failed') is False
        assert owner.state == before
    asyncio.run(scenario())


def test_an_acknowledged_submit_is_still_protected():
    """[SUBMIT 보호 유지] order_ref 가 채워진 SUBMIT 은 여전히 종료되지 않는다."""
    async def scenario():
        owner, life = await with_child()
        await seed(owner, 'A-1', state='prepared', status='prepared', claim_id=None,
                   command_status=None)
        before = deepcopy(owner.state)
        assert await life.abandon_candidate('A-1', reason='dispatch_failed') is False
        assert owner.state == before
        assert reservations(owner.state['attempts']['A-1'])[0] == 10
    asyncio.run(scenario())


# ── 소비자 쪽 해금 ──────────────────────────────────────────────────────

def test_closing_a_child_unblocks_a_new_submit_for_the_symbol(tmp_path, monkeypatch):
    """[종목 해금] 미해결 자식이 하나라도 있으면 같은 종목의 새 SUBMIT 이 거부된다."""
    async def scenario():
        f = await command_fixture(tmp_path, monkeypatch, ready=True, origin='automatic',
                                  policy=nominal())
        try:
            req = f['request']()
            await f['quote'](req)
            await f['facts'](req, sector=SECTOR)
            await f['commands'].prepare(req, f['entry'](req))
            result = await f['commands'].dispatch(req, f['entry'](req), GuardedKISTransport(
                f['broker'], request_builder=f['builder']))
            assert result.status is CommandStatus.ACKNOWLEDGED
            parent = f['runtime'].owner.state['attempts']['A']
            cancel = f['builder'].prepare_cancel(
                intent_id=req.intent_id, attempt_id='C', session=req.session,
                parent=CancelParent(req.intent_id, req.attempt_id, parent['version'],
                                    OrderRef.from_dict(parent['order_ref']), req.symbol,
                                    req.side, req.order_type, parent['reserved_quantity'],
                                    req.valuation_price, req.strategy))
            await f['commands'].prepare(cancel, f['entry'](cancel))

            # 부모만 터미널로 만든다(복구된 checkpoint 형태) — 남는 잔류는 자식 하나다.
            def settle(state):
                row = state['attempts']['A']
                row.update(state=OrderState.FINAL_CANCELLED.value, status='final_cancelled',
                           reserved_quantity=0, reserved_cash='0', reserved_exposure='0',
                           reserved_planned_risk=None)
                return state
            await f['runtime'].owner.mutate('synthetic-settled-parent', settle)

            again = f['request']('A2')
            await f['quote'](again)
            await f['facts'](again, sector=SECTOR)
            with pytest.raises(CommandValidationError, match='unresolved_child_attempt'):
                await f['commands'].prepare(again, f['entry'](again))
            assert await f['runtime'].lifecycle.abandon_candidate(
                'C', reason='dispatch_failed') is True
            reopened = await f['commands'].prepare(again, f['entry'](again))
            assert reopened['state'] == 'prepared'
            assert len(f['broker']._session.posts) == 1
        finally:
            await f['store'].close()
    asyncio.run(scenario())


def test_child_close_after_restore_keeps_the_same_verdict(tmp_path):
    """checkpoint 복구 뒤에도 같은 가드로 판정이 같다."""
    async def scenario():
        engine, exits, store, runtime = await setup(tmp_path)
        try:
            await runtime.lifecycle.prepare('I-1', 'A-1', 10, SYMBOL, 'buy',
                                            reserved_cash=CASH, strategy='sepa_trend')
            assert await runtime.lifecycle.claim('A-1', 'sender-1')
            assert await runtime.lifecycle.record_result('A-1', 'sender-1', CommandResult(
                CommandStatus.ACKNOWLEDGED, 'A-1', PARENT_REF))
            await runtime.lifecycle.prepare('I-1', 'C-1', 10, SYMBOL, 'buy',
                                            command=CommandKind.CANCEL,
                                            parent_attempt_id='A-1', order_ref=PARENT_REF)
            second = KRExecutionRuntime(store, engine, exits, clock=lambda: NOW)
            await second.restore()
            assert second.owner.state['attempts']['C-1']['claim_id'] is None
            assert await second.lifecycle.abandon_candidate('C-1', reason='dispatch_failed') is True
            child = second.owner.state['attempts']['C-1']
            assert child['state'] == 'final_rejected' and child['command_status'] == 'not_sent'
            assert second.owner.state['attempts']['A-1']['state'] == 'open'
        finally:
            # 열린 SQLite fd 를 남기면 같은 프로세스의 fd 계수 시험(account_lease)이 틀어진다
            await store.close()
    asyncio.run(scenario())
