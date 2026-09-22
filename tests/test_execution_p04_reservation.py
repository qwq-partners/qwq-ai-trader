"""P0-4 S-B: 부분 체결 뒤 예약(약한 지점 4)의 **현 계약 고정**. 제품 코드는 한 줄도 바꾸지 않는다.

결정 문서 §4-2 항목 4 는 "`reserved_quantity` 는 terminal 전까지 줄지 않아 `held − reserved`
가 음수가 된다"고 적었는데 **코드와 어긋난다**. 여기서 고정하는 사실은 셋이다:

1. 예약 산술은 정상이다 — `reduce_economics` 가 체결마다 `max(0, reserved − delta)` 로 줄이고
   (`economics.py:467`·`:480`) 보유도 같은 commit 에서 줄어(`:437`) **차이가 0 을 밑돌지 않는다**.
   단, 이 등식은 **owner attempt 에 대사된 체결에 한해** 참이다(대사 밖 체결은 owner 가 모른다).
2. 같은 종목의 새 보호 SELL 을 막는 낱말은 **이 부분 체결 시나리오에서는** `unresolved_symbol_attempt`
   (`commands.py:393-394`)이고 `reserved_quantity_insufficient`(`:422-426`)가 아니다. 두 낱말은
   **두 겹**이다 — 잠금은 "미해결 시도가 있는 종목", 예약 검사는 "보유보다 많이 팔지 마라".
3. 진짜 영구 봉쇄는 **만료 종결 계약의 부재**다(설치 차단 사유 22) — 파서는 `FINAL_FILLED`/
   `RECONCILING` 만 만들고(`evidence.py:265-279`) `reconcile` 은 `FINAL_EXPIRED` 를 최종성으로
   읽지 않는다(`lifecycle.py:560-562`). 그래서 부분 체결 주문이 장 마감으로 소멸해도 attempt 는
   `partial` 로 남아 그 종목을 잠그고 **일자 전환을 BLOCKED** 로 만든다.

전부 합성 fake HTTP·주입 시계·시험용 합성 startup 허가 위의 결과다. 제품의 attach 호출자는
0건이고 `trading_ready` 는 제품 코드에서 계속 False 다.

실행: venv/bin/python -m pytest tests/test_execution_p04_reservation.py -q -p no:cacheprovider
"""
import asyncio
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from src.core.types import OrderSide
from src.execution.safety.application import FillObservation
from src.execution.safety.evidence import EvidencePage
from src.execution.safety.lifecycle import CommandStatus, OrderEvidence, OrderRef, OrderState
from src.execution.safety.transport import GuardedKISTransport

from test_execution_command_owner import fixture as command_fixture, held
from test_execution_risk_policy import NOW
from test_execution_runtime import queued
from test_kis_order_evidence import parse, row


@pytest.fixture(autouse=True)
def synthetic_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)


SYMBOL = '005930'
SCOPE = {'account_scope': 'test-scope', 'market': 'KR', 'exchange': 'KRX',
         'start_date': NOW.date().isoformat(), 'end_date': NOW.date().isoformat(),
         'tr_id': 'TTTC0081R', 'query_kind': 'all', 'session': 'regular'}


def attempt_row(f, attempt_id):
    return f['runtime'].owner.state['attempts'][attempt_id]


def position_quantity(f):
    position = f['engine'].portfolio.positions.get(SYMBOL)
    return 0 if position is None else position.quantity


async def fill_evidence(f, attempt_id, *, side, filled, ordered, state, amount):
    """실제 ACK 로 받은 order_ref 위에 합성 증거를 얹는다(외부 I/O 없음)."""
    ref = OrderRef(**attempt_row(f, attempt_id)['order_ref'])
    evidence = OrderEvidence(
        ref, SYMBOL, side, ordered, filled, D(amount), ordered - filled, 0, state,
        complete=True, supported_finality=state is not OrderState.RECONCILING,
        source_contract='synthetic-p04', observed_at=f['clock'][0],
        request_started_at=f['clock'][0] - timedelta(seconds=1), query_scope=dict(SCOPE))
    return ref, evidence


async def acknowledged(f, attempt_id, request, order_no):
    """실제 transport 로 한 번 POST 한다(응답 ODNO 만 시도마다 갈라 둔다)."""
    f['broker']._session.response.data = {
        'rt_cd': '0', 'output': {'ODNO': order_no, 'KRX_FWDG_ORD_ORGNO': '12345'}}
    await f['commands'].prepare(request, f['entry'](request))
    result = await f['commands'].dispatch(request, f['entry'](request),
        GuardedKISTransport(f['broker'], request_builder=f['builder']))
    assert result.status is CommandStatus.ACKNOWLEDGED
    return attempt_row(f, attempt_id)


async def applied(f, attempt_id, *, side, filled, ordered, state, amount):
    """대사 → 실큐 한 바퀴. 체결 사실이 owner 경제에 실제로 반영되는 경로다."""
    ref, evidence = await fill_evidence(f, attempt_id, side=side, filled=filled,
                                        ordered=ordered, state=state, amount=amount)
    assert await f['runtime'].lifecycle.reconcile(attempt_id, evidence)
    observation = FillObservation(ref.account_scope, ref.market, ref.order_date, ref.exchange,
                                  ref.order_no, SYMBOL, side.upper(), filled, D(amount),
                                  org_no=ref.org_no, parent_order_no=ref.parent_order_no)
    return await queued(f['engine'], observation)


async def partially_sold(tmp_path, monkeypatch, *, ordered=100, filled=40):
    """실제 BUY 100 전량 체결로 보유를 만든 뒤 SELL 100 중 40 주만 대사·적용한 상태."""
    f = await command_fixture(tmp_path, monkeypatch, origin='user')
    buy = f['request']('B1', quantity=ordered, price=D('10000'))
    await f['quote'](buy)
    await acknowledged(f, 'B1', buy, 'ODNO-BUY-1')
    assert (await applied(f, 'B1', side='buy', filled=ordered, ordered=ordered,
                          state=OrderState.FINAL_FILLED,
                          amount=str(ordered * 10000))).status == 'APPLIED'

    request = f['request']('S1', quantity=ordered, price=D('11000'), side=OrderSide.SELL)
    await acknowledged(f, 'S1', request, 'ODNO-SELL-1')
    assert (await applied(f, 'S1', side='sell', filled=filled, ordered=ordered,
                          state=OrderState.RECONCILING,
                          amount=str(filled * 11000))).status == 'APPLIED'
    return f, request


# ── B1: 예약 산술은 정상이고, 막는 낱말은 종목 잠금이다 ──────────────────

def test_a_partial_sell_leaves_reserved_equal_to_held_and_the_symbol_lock_speaks_first(
        tmp_path, monkeypatch):
    """SELL 100/40 뒤 `reserved == held == 60` 이고 차이는 정확히 0 이다.

    이중 매도를 막는 것은 **두 겹**이다 — (a) 종목 단위 잠금(`commands.py:393-394`)과
    (b) 예약 검사(`:422-426`). 이 부분 체결 시나리오에서 먼저 말하는 것은 (a) 이고,
    (a) 를 열어도 (b) 가 `0 >= 60` 거짓으로 같은 결론을 낸다 — 원 주문이 거래소에 살아
    있는 동안에는 그것이 옳다. 잔여 60 의 구제는 취소·에스컬레이션(P1/D2)이다.

    `held − reserved >= 0` 은 **owner attempt 에 대사된 체결에 한해** 참이다.
    """
    async def scenario():
        f, _ = await partially_sold(tmp_path, monkeypatch)
        try:
            attempt = attempt_row(f, 'S1')
            assert (attempt['state'], attempt['applied_quantity']) == ('partial', 40)
            assert attempt['reserved_quantity'] == 60
            assert position_quantity(f) == 60
            assert position_quantity(f) - attempt['reserved_quantity'] == 0

            retry = f['request']('S2', quantity=60, side=OrderSide.SELL)
            with pytest.raises(ValueError) as caught:
                await f['commands'].prepare(retry, f['entry'](retry))
            assert str(caught.value) == 'unresolved_symbol_attempt'
            assert 'S2' not in f['runtime'].owner.state['attempts']
        finally:
            await f['store'].close()
    asyncio.run(scenario())


# ── B2: 두 낱말의 순서(394 가 426 보다 앞) ───────────────────────────────

def test_the_symbol_lock_is_evaluated_before_the_reservation_check(tmp_path, monkeypatch):
    """두 낱말이 서로 다른 상태를 이름 붙인다 — 잠금을 예약 검사 뒤로 옮기면 (a) 가 (b) 로 바뀐다.

    (a) 미해결 SELL 이 남은 종목: `unresolved_symbol_attempt`(예약 검사에 **도달하지 않는다**).
    (b) 미해결 시도가 하나도 없고 보유보다 많이 파는 요청: `reserved_quantity_insufficient`.
    """
    async def scenario():
        f = await command_fixture(tmp_path, monkeypatch, origin='user')
        try:
            await held(f, 100)

            # (b) 미해결 시도 0 건 — 예약 합계가 0 이라 보유 초과만 남는다.
            oversell = f['request']('O1', quantity=101, side=OrderSide.SELL)
            with pytest.raises(ValueError) as oversold:
                await f['commands'].prepare(oversell, f['entry'](oversell))
            assert str(oversold.value) == 'reserved_quantity_insufficient'

            # (a) 미해결 SELL 을 하나 만들면 같은 요청이 더 앞의 낱말로 막힌다.
            first = f['request']('S1', quantity=100, side=OrderSide.SELL)
            await f['commands'].prepare(first, f['entry'](first))
            with pytest.raises(ValueError) as locked:
                await f['commands'].prepare(oversell, f['entry'](oversell))
            assert str(locked.value) == 'unresolved_symbol_attempt'

            # 같은 잠금이 보유 범위 안의 요청에도 걸린다(예약 검사라면 통과했을 수량이다).
            within = f['request']('O2', quantity=1, side=OrderSide.SELL)
            with pytest.raises(ValueError) as still_locked:
                await f['commands'].prepare(within, f['entry'](within))
            assert str(still_locked.value) == 'unresolved_symbol_attempt'
        finally:
            await f['store'].close()
    asyncio.run(scenario())


# ── B3: 영구 `partial` 은 일자 전환을 BLOCKED 로 만든다(차단 사유 22) ─────

def test_a_surviving_partial_blocks_the_day_rollover_even_after_the_reservation_is_gone(
        tmp_path, monkeypatch):
    """`unresolved_submit` 은 예약과 **별개 사유**다 — 예약을 0 으로 지워도 일자는 넘어가지 않는다.

    그리고 BLOCKED 로 끝난 `prepare_day_rollover` 도 `day_transition` 행은 commit 한다 —
    그 fence 위의 `rollover_day` 가 같은 사유로 다시 BLOCKED 가 된다(②M1 의 모양).
    """
    async def scenario():
        f, _ = await partially_sold(tmp_path, monkeypatch)
        runtime = f['runtime']
        try:
            f['clock'][0] = NOW + timedelta(days=1)
            fence = await runtime.prepare_day_rollover(
                'p04-prepare', expected_version=runtime.owner.version,
                from_day=NOW.date().isoformat(), to_day=f['clock'][0].date().isoformat(),
                valuation_boundary=f['clock'][0])
            assert (fence.status, fence.reason) == ('BLOCKED', 'unresolved_submit')

            def erase(state):
                attempt = state['attempts']['S1']
                attempt.update(reserved_quantity=0, reserved_cash='0', reserved_exposure='0',
                               reserved_planned_risk=None)
                return state
            await runtime.owner.mutate('p04-erase-reservation', erase)
            from src.execution.safety.reservations import has_remaining_reservation
            assert not has_remaining_reservation(runtime.owner.state['attempts']['S1'])

            receipt = await runtime.rollover_day(
                'p04-rollover', expected_version=runtime.owner.version,
                fence_id=fence.fence_id, valuation_evidence_id='p04-none')
            assert (receipt.status, receipt.reason) == ('BLOCKED', 'unresolved_submit')
            assert runtime.owner.state['risk']['day'] == NOW.date().isoformat()
        finally:
            await f['store'].close()
    asyncio.run(scenario())


# ── B4: 만료 종결 계약이 없다(파서 + reconcile 양쪽) ─────────────────────

def test_a_partially_filled_row_is_never_parsed_as_final_finality():
    """`remaining > 0` 행은 `FINAL_FILLED` 가 되지 않는다 — `RECONCILING` + `unsupported_finality`.

    파서에는 `FINAL_CANCELLED`/`FINAL_EXPIRED` 생산 경로 자체가 없다(`evidence.py:279`).
    """
    evidence = parse([EvidencePage([row(tot_ccld_qty='4', tot_ccld_amt='400', rmn_qty='6')], 'D')])
    assert evidence.state is OrderState.RECONCILING
    assert evidence.complete and not evidence.supported_finality
    assert evidence.reason == 'unsupported_finality'
    assert (evidence.cumulative_quantity, evidence.remaining_quantity) == (4, 6)


def test_reconcile_refuses_to_read_a_synthetic_expiry_as_finality(tmp_path, monkeypatch):
    """파서가 만들지 않는 `FINAL_EXPIRED` 를 **손으로 지어 넣어도** 종결로 읽지 않는다.

    세션 경계 소멸에는 고정된 만료 계약이 없다(`lifecycle.py:560-562`) — 그래서 attempt 는
    `partial` 로 남고 예약 60 도 남는다. 이것이 설치 차단 사유 22 의 실물이다.
    """
    async def scenario():
        f, _ = await partially_sold(tmp_path, monkeypatch)
        try:
            before = dict(attempt_row(f, 'S1'))
            _, expired = await fill_evidence(f, "S1", side="sell", filled=40, ordered=100,
                                             state=OrderState.FINAL_EXPIRED, amount='440000')
            assert expired.state.value in {'final_filled', 'final_cancelled', 'final_rejected',
                                           'final_expired'}
            assert await f['runtime'].lifecycle.reconcile('S1', expired)
            after = attempt_row(f, 'S1')
            assert after['state'] == 'partial'
            assert after['reserved_quantity'] == before['reserved_quantity'] == 60
            assert D(after['reserved_cash']) == D(before['reserved_cash'])
        finally:
            await f['store'].close()
    asyncio.run(scenario())
