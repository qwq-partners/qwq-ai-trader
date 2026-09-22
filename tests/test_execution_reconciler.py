"""P0-2 2단계: runtime 소유 체결 대사 주기(제품 호출자 0건·live 파일 0줄).

실제 owner/store(tmp_path)·실제 요청 binding·실제 수집기(`LegacyExecutionQueries`)·실제
`engine.apply_execution_observation` 큐를 쓴다. 합성 응답 페이지는 실 KIS 취소/체결
최종성의 증명이 아니다 — 여기서 GREEN 인 것은 주기의 계약(게이트·조회 횟수·chain 처분·
저장 상태 재처리·종료)뿐이며 설치 승인이 아니다.
"""
import ast
import asyncio
import inspect
from datetime import timedelta
from decimal import Decimal as D

import pytest

from src.execution.safety import economics
from src.execution.safety import runtime as runtime_module
from src.execution.safety.application import ApplicationBlocked, FillObservation, observation_from_evidence
from src.execution.safety.evidence import evidence_pages, parse_order_evidence, parser_scope
from src.execution.safety.lifecycle import CommandStatus, OrderEvidence, OrderState
from src.execution.safety.queries import LegacyExecutionQueries
from src.execution.safety.runtime import APPLIABLE_ATTEMPT_STATES, RECONCILER_MAX_PAGES
from src.execution.safety.transport import GuardedKISTransport
from test_execution_command_owner import fixture
from test_kis_execution_queries import response


SYMBOL = '005930'


def fill_row(ref, *, quantity, filled, amount, **changes):
    """우리 주문 한 행. 신원 4필드는 ACK 가 준 `OrderRef` 에서만 만든다."""
    value = {'ord_dt': ref.order_date.replace('-', ''), 'odno': ref.order_no,
             'orgn_odno': ref.parent_order_no, 'ord_gno_brno': ref.org_no,
             'pdno': SYMBOL, 'sll_buy_dvsn_cd': '02', 'ord_qty': str(quantity),
             'tot_ccld_qty': str(filled), 'tot_ccld_amt': str(amount),
             'rmn_qty': str(quantity - filled), 'cnc_cfrm_qty': '0', 'rjct_qty': '0',
             'cncl_yn': 'N', 'excg_id_dvsn_cd': 'KRX', 'ord_dvsn_cd': '00'}
    value.update(changes)
    return value


def other_row(ref, **changes):
    """계좌 안의 무관한 주문 한 행(우리 odno 가 아니다)."""
    return fill_row(ref, quantity=1, filled=1, amount=1, odno='000999', **changes)


def child_row(ref):
    """우리 주문의 자식행(정정/취소 체인)."""
    return fill_row(ref, quantity=100, filled=0, amount=0, odno='000124',
                    orgn_odno=ref.order_no)


def collector(f, script):
    """실제 수집기를 통과시킨다 — QueryCollection/QueryPage 의 모양을 시험이 흉내 내지 않는다.

    script 는 주기별 응답 목록이며 마지막 항목이 이후 주기에 반복된다.
    """
    calls = []

    async def collect(*, start_date, end_date, exchange_scope, max_pages):
        calls.append((start_date, end_date, exchange_scope, max_pages))
        pages = script[min(len(calls), len(script)) - 1]
        fetched = []

        async def fetch(request):
            fetched.append(request)
            item = pages[len(fetched) - 1]
            if isinstance(item, BaseException):
                raise item
            return item

        queries = LegacyExecutionQueries(fetch, clock=lambda: f['clock'][0],
                                         request_timeout=1, max_pages=max_pages)
        return await queries.daily(account_scope=f['runtime'].account_scope,
                                   account_number='SYNTHETIC_ACCOUNT',
                                   product_code='SYNTHETIC_PRODUCT',
                                   start_date=start_date, end_date=end_date,
                                   exchange_scope=exchange_scope)

    return collect, calls


async def producer(tmp_path, monkeypatch, *, quantity=100, dispatch=True):
    """실제 prepare→ACK 로 order_ref·session·fill_metadata 를 가진 미해결 SUBMIT 을 만든다."""
    f = await fixture(tmp_path, monkeypatch)
    request = f['request'](quantity=quantity)
    await f['quote'](request)
    await f['commands'].prepare(request, f['entry'](request),
                                fill_metadata={'entry_signal_score': 80.0})
    f['order'] = request
    f['ref'] = None
    if dispatch:
        ack = await f['commands'].dispatch(request, f['entry'](request),
                                           GuardedKISTransport(f['broker'], request_builder=f['builder']))
        assert ack.status is CommandStatus.ACKNOWLEDGED
        f['ref'] = ack.order_ref
    f['day'] = f['clock'][0].date().isoformat()
    return f


def start_pump(engine):
    """실제 우선순위 큐/핸들러를 돌린다. emit 을 mock 하지 않는다."""
    stop = asyncio.Event()

    async def loop():
        while not stop.is_set():
            event = await engine._get_next_event()
            if event is None:
                await asyncio.sleep(0.001)
                continue
            await engine._process_event(event)

    return asyncio.create_task(loop()), stop


async def teardown(f, pump=None):
    if pump is not None:
        task, stop = pump
    try:
        await f['runtime'].shutdown()
    finally:
        if pump is not None:
            stop.set()
            await asyncio.gather(task, return_exceptions=True)
        await f['store'].close()


async def observe(runtime, f, quantity, amount, *, applied=None, accepted=True):
    """주기 **밖**에서 관측만 저장한다(생산자가 만든 상태가 아니라 준비 상태다).

    `accepted=False` 는 충돌 근거를 넣어 attempt 를 evidence_conflict 로 굳히는 용도다.
    """
    ref, now = f['ref'], f['clock'][0]
    total = f['order'].quantity
    evidence = OrderEvidence(ref, SYMBOL, 'buy', total, quantity, amount, total - quantity, 0,
                             OrderState.FINAL_FILLED if quantity == total else OrderState.PARTIAL,
                             complete=True, supported_finality=quantity == total,
                             source_contract='synthetic-reconciler-setup',
                             observed_at=now, request_started_at=now,
                             query_scope=dict(account_scope=ref.account_scope, market='KR',
                                              exchange='ALL', start_date=ref.order_date,
                                              end_date=ref.order_date, tr_id='TTTC0081R',
                                              query_kind='all', session='regular'))
    result = await runtime.lifecycle.reconcile(f['order'].attempt_id, evidence,
                                               applied_quantity=applied)
    assert result is accepted


def spy_apply(f):
    applied = []
    original = f['engine'].apply_execution_observation

    async def wrapped(observation):
        applied.append(observation)
        return await original(observation)

    f['engine'].apply_execution_observation = wrapped
    return applied


# ───────────────────────────── ① 미해결 0 / ACK 깨움 ─────────────────────────────

def test_no_unresolved_attempt_queries_nothing_and_an_ack_wakes_one_cycle(tmp_path, monkeypatch):
    async def scenario():
        f = await producer(tmp_path, monkeypatch, dispatch=False)
        pump = start_pump(f['engine'])
        collect, calls = collector(f, [[response([])]])
        try:
            # prepared 만 있고 order_ref 가 없다 — 조회 대상이 아니다.
            f['runtime'].start_reconciler(collect, interval=100.0, cycle_timeout=5.0)
            for _ in range(20):
                await asyncio.sleep(0)
            assert calls == []
            assert f['runtime'].health()['reconciler']['targets'] == 0
            ack = await f['commands'].dispatch(f['order'], f['entry'](f['order']),
                                               GuardedKISTransport(f['broker'], request_builder=f['builder']))
            assert ack.status is CommandStatus.ACKNOWLEDGED

            async def observed():
                while not calls:
                    await asyncio.sleep(0)
            await asyncio.wait_for(observed(), 2)
            assert calls == [(f['day'], f['day'], 'ALL', RECONCILER_MAX_PAGES)]
        finally:
            await teardown(f, pump)
    asyncio.run(scenario())


# ───────────────────────── ② 부분 체결 → reconcile → apply ─────────────────────────

def test_partial_fill_is_observed_and_applied_in_one_cycle(tmp_path, monkeypatch):
    async def scenario():
        f = await producer(tmp_path, monkeypatch)
        pump = start_pump(f['engine'])
        ref, runtime = f['ref'], f['runtime']
        collect, calls = collector(f, [[response([fill_row(ref, quantity=100, filled=40, amount=400000)])]])
        try:
            assert await runtime.reconcile_once(collect) is True
            assert calls == [(f['day'], f['day'], 'ALL', RECONCILER_MAX_PAGES)]
            attempt = runtime.owner.state['attempts'][f['order'].attempt_id]
            assert attempt['observed_quantity'] == 40 and attempt['applied_quantity'] == 40
            assert attempt['reserved_quantity'] == 60 and attempt['state'] == 'partial'
            assert f['engine'].portfolio.positions[SYMBOL].quantity == 40
            assert f['exits'].get_state(SYMBOL).remaining_quantity == 40
            health = runtime.health()['reconciler']
            assert health['targets'] == 1 and health['parked'] == 0
            assert health['apply_outcomes'] == {'FillReceipt:APPLIED': 1}
            assert health['last_progress_at'] == f['clock'][0].isoformat()
        finally:
            await teardown(f, pump)
    asyncio.run(scenario())


# ───────────────────────── ③ 같은 not_found 10주기 ─────────────────────────

def test_repeated_not_found_never_advances_the_owner_but_still_queries(tmp_path, monkeypatch):
    async def scenario():
        f = await producer(tmp_path, monkeypatch)
        pump = start_pump(f['engine'])
        runtime, ref = f['runtime'], f['ref']
        collect, calls = collector(f, [[response([other_row(ref)])]])
        try:
            version = runtime.owner.version
            for _ in range(10):
                assert await runtime.reconcile_once(collect) is False
            assert len(calls) == 10
            assert runtime.owner.version == version
            assert runtime.health()['reconciler']['skipped'] == {'not_found': 10}
        finally:
            await teardown(f, pump)
    asyncio.run(scenario())


# ──────────────── ④ chain 행: reconcile 과 재구성 재접수(B2) 둘 다 0 ────────────────

def test_a_chain_row_blocks_reconcile_and_the_reconstructed_reapply(tmp_path, monkeypatch):
    """chain 이 막는 범위는 A 와 B2 뿐이다.

    B1(잔존 inbox 행)은 chain 과 무관하게 진행한다 — 그 행은 chain 판정 이전에 이미
    검증된 durable 관측이고, 자식행이 보인다는 이유로 막으면 설치 차단 사유 19가
    영구화된다. 여기서 재접수가 0인 것은 attempt 를 다시 읽어 만드는 B2 경로다.
    """
    async def scenario():
        f = await producer(tmp_path, monkeypatch)
        pump = start_pump(f['engine'])
        runtime, ref = f['runtime'], f['ref']
        # 앞선 관측으로 observed(40) > applied(0) 을 만든다 — chain 이 apply 도 막는지 본다.
        await observe(runtime, f, 40, D('400000'))
        applied = spy_apply(f)
        collect, calls = collector(f, [[response([fill_row(ref, quantity=100, filled=100, amount=1000000),
                                                  child_row(ref)])]])
        try:
            version = runtime.owner.version
            assert await runtime.reconcile_once(collect) is False
            assert len(calls) == 1 and applied == []
            assert runtime.owner.version == version
            attempt = runtime.owner.state['attempts'][f['order'].attempt_id]
            assert attempt['observed_quantity'] == 40 and attempt['applied_quantity'] == 0
            assert runtime.health()['reconciler']['skipped'] == {'chain': 1}
        finally:
            await teardown(f, pump)
    asyncio.run(scenario())


# ───────────────────────── ⑤ 무관한 행 결측은 우리 행을 지우지 않는다 ─────────────

def test_a_broken_unrelated_row_does_not_erase_our_observation(tmp_path, monkeypatch):
    async def scenario():
        f = await producer(tmp_path, monkeypatch)
        pump = start_pump(f['engine'])
        runtime, ref = f['runtime'], f['ref']
        broken = other_row(ref)
        del broken['cnc_cfrm_qty']
        collect, _ = collector(f, [[response([broken, fill_row(ref, quantity=100, filled=100,
                                                               amount=1000000)])]])
        try:
            assert await runtime.reconcile_once(collect) is True
            attempt = runtime.owner.state['attempts'][f['order'].attempt_id]
            assert attempt['observed_quantity'] == 100 and attempt['applied_quantity'] == 100
            assert attempt['state'] == 'final_filled' and attempt['reserved_quantity'] == 0
            assert f['engine'].portfolio.positions[SYMBOL].quantity == 100
        finally:
            await teardown(f, pump)
    asyncio.run(scenario())


# ───────────────────────── ⑥ 불완전 수집은 상태를 전진시키지 않는다 ─────────────────

def test_incomplete_collection_never_reaches_the_parser_or_the_owner(tmp_path, monkeypatch):
    async def scenario():
        f = await producer(tmp_path, monkeypatch)
        pump = start_pump(f['engine'])
        runtime, ref = f['runtime'], f['ref']
        collect, calls = collector(f, [[response([fill_row(ref, quantity=100, filled=100, amount=1000000)],
                                                 continuation='F', cursor=('a', 'b')),
                                        TimeoutError('synthetic-network')]])
        try:
            version = runtime.owner.version
            assert await runtime.reconcile_once(collect) is False
            assert len(calls) == 1 and runtime.owner.version == version
            attempt = runtime.owner.state['attempts'][f['order'].attempt_id]
            assert attempt['observed_quantity'] == 0
            health = runtime.health()['reconciler']
            assert health['skipped'] == {'incomplete_collection': 1}
            assert health['last_complete_at'] is None
        finally:
            await teardown(f, pump)
    asyncio.run(scenario())


# ───────────────────────── ⑦ 잔존 inbox 행의 재접수(B1) ─────────────────────────

def test_a_received_inbox_row_is_reapplied_without_any_query(tmp_path, monkeypatch):
    async def scenario():
        f = await producer(tmp_path, monkeypatch)
        pump = start_pump(f['engine'])
        runtime, ref = f['runtime'], f['ref']
        await observe(runtime, f, 40, D('400000'))
        observation = FillObservation(ref.account_scope, 'KR', ref.order_date, 'KRX', ref.order_no,
                                      SYMBOL, 'BUY', 40, D('400000'), org_no=ref.org_no,
                                      metadata={'entry_signal_score': 80.0})
        # 관측 저장 뒤 적용 전에 멈춘 잔해: durable 접수만 있고 경제 적용이 없다.
        receipt = await runtime.owner.receive(observation, ingress_context=runtime.ingress_context(1))
        assert receipt.status == 'RECEIVED'
        collect, calls = collector(f, [[response([other_row(ref)])]])
        try:
            assert await runtime.reconcile_once(collect) is True
            row = runtime.owner.state['inbox'][observation.observation_id]
            assert row['status'] in ('APPLIED', 'SUPERSEDED')
            assert f['engine'].portfolio.positions[SYMBOL].quantity == 40
            assert runtime.health()['reconciler']['apply_outcomes'] == {'FillReceipt:APPLIED': 1}
            # 조회는 여전히 미해결 attempt 때문에 한 번 나갔지만 재접수와는 무관하다.
            assert len(calls) == 1
        finally:
            await teardown(f, pump)
    asyncio.run(scenario())


# ───────────────────────── ⑧ 저장 상태 재구성(B2)의 동일 observation_id ─────────────

def test_stored_state_reapply_uses_the_same_observation_id_as_the_parser_path(tmp_path, monkeypatch):
    async def scenario():
        f = await producer(tmp_path, monkeypatch)
        pump = start_pump(f['engine'])
        runtime, ref = f['runtime'], f['ref']
        collect, _ = collector(f, [[response([fill_row(ref, quantity=100, filled=40, amount=400000)])]])
        # 같은 응답을 파서 경로로 한 번 읽어 기대 observation_id 를 독립적으로 만든다.
        collection = await collect(start_date=f['day'], end_date=f['day'], exchange_scope='ALL',
                                   max_pages=RECONCILER_MAX_PAGES)
        parsed = parse_order_evidence(ref, SYMBOL, 'buy', evidence_pages(collection),
                                      tr_id=collection.scope.tr_id, session='regular',
                                      query_kind='all', max_pages=RECONCILER_MAX_PAGES,
                                      observed_at=collection.completed_at,
                                      request_started_at=collection.started_at,
                                      query_scope=parser_scope(collection.scope, session='regular'),
                                      now=f['clock'][0])
        expected = observation_from_evidence(parsed, trading_day=ref.order_date,
                                             metadata={'entry_signal_score': 80.0})
        applied = spy_apply(f)
        try:
            assert await runtime.reconcile_once(collect) is True
            assert [row.observation_id for row in applied] == [expected.observation_id]
            assert applied[0].to_dict() == expected.to_dict()
            assert runtime.owner.state['inbox'][expected.observation_id]['status'] == 'APPLIED'
        finally:
            await teardown(f, pump)
    asyncio.run(scenario())


# ───────────────────────── ⑨ 일자 게이트: 조회도 재처리도 0 ─────────────────────────

@pytest.mark.parametrize('gate', ['prior_day', 'day_admission_closed'])
def test_the_day_gate_runs_before_any_query_or_reapply(tmp_path, monkeypatch, gate):
    async def scenario():
        f = await producer(tmp_path, monkeypatch)
        pump = start_pump(f['engine'])
        runtime, ref = f['runtime'], f['ref']
        await observe(runtime, f, 40, D('400000'))
        observation = FillObservation(ref.account_scope, 'KR', ref.order_date, 'KRX', ref.order_no,
                                      SYMBOL, 'BUY', 40, D('400000'), org_no=ref.org_no,
                                      metadata={'entry_signal_score': 80.0})
        await runtime.owner.receive(observation, ingress_context=runtime.ingress_context(1))
        applied = spy_apply(f)
        collect, calls = collector(f, [[response([fill_row(ref, quantity=100, filled=100, amount=1000000)])]])
        try:
            version = runtime.owner.version
            if gate == 'prior_day':
                f['clock'][0] += timedelta(days=1)
            else:
                runtime._day_closed = True
            assert await runtime.reconcile_once(collect) is False
            assert calls == [] and applied == []
            assert runtime.owner.version == version
            assert runtime.owner.state['inbox'][observation.observation_id]['status'] == 'RECEIVED'
            health = runtime.health()['reconciler']
            assert health['skipped'] == {gate: 1} and health['targets'] == 0
            assert runtime.reconciler_blocked_reason() is None
        finally:
            if gate == 'prior_day':
                f['clock'][0] -= timedelta(days=1)
            runtime._day_closed = False
            await teardown(f, pump)
    asyncio.run(scenario())


# ───────────────────────── ⑩ 멈춘 주기: cycle_timeout 과 blocked_reason ─────────────

def test_a_stalled_apply_ends_at_the_cycle_timeout_and_is_named_blocked(tmp_path, monkeypatch):
    async def scenario():
        f = await producer(tmp_path, monkeypatch)
        pump = start_pump(f['engine'])
        runtime, ref, now = f['runtime'], f['ref'], f['clock'][0]
        await observe(runtime, f, 40, D('400000'))
        held = asyncio.Event()

        async def never(observation):
            await held.wait()
            raise ApplicationBlocked('synthetic_released_after_the_test')

        f['engine'].apply_execution_observation = never
        collect, calls = collector(f, [[response([other_row(ref)])]])
        try:
            runtime.start_reconciler(collect, interval=0.01, cycle_timeout=0.05)

            async def cycles():
                # 주기 사이에는 A 의 not_found 가 먼저 기록된다. 멈춘 주기가 **끝난**
                # 순간(사유 확정 직후)을 잡아야 접미사를 고정해 단언할 수 있다.
                while len(calls) < 2 or runtime.health()['reconciler']['last_reason'] != 'cycle_timeout':
                    await asyncio.sleep(0.005)
            await asyncio.wait_for(cycles(), 5)
            assert runtime.reconciler_blocked_reason() is None  # 아직 k주기를 넘지 않았다
            f['clock'][0] += timedelta(seconds=30)
            # 주기 task 는 살아 있다 — task.done() 이 아니라 상태 전진으로 재기 때문에 잡힌다.
            assert runtime.health()['reconciler']['reconciler_running'] is True
            assert runtime.reconciler_blocked_reason() == 'reconciler_no_progress:cycle_timeout'
            assert runtime.health()['reconciler']['skipped']['cycle_timeout'] >= 1
        finally:
            held.set()
            f['clock'][0] = now
            await teardown(f, pump)
    asyncio.run(scenario())


def test_blocked_reason_names_a_producer_that_was_never_started(tmp_path, monkeypatch):
    async def scenario():
        f = await producer(tmp_path, monkeypatch)
        try:
            assert f['runtime'].reconciler_blocked_reason() == 'reconciler_not_started'
        finally:
            await teardown(f)
    asyncio.run(scenario())


# ───────────────────────── ⑪ 종료: 진행 중 적용 대기 후 접수 거부 ─────────────────

def test_shutdown_waits_for_the_in_flight_apply_then_refuses_new_ones(tmp_path, monkeypatch):
    """진행 중 apply 는 **수집 task** 가 들고 있다 — drain 집합은 runtime 소유 task뿐이다.

    engine 의 ingress/apply task 는 engine._shutdown 의 몫이라 runtime.shutdown 이 engine 의
    사설 속성을 읽지 않는다. 수집 task 는 자연 종료할 때 자기가 시작한 apply 를 스스로
    기다리므로, 그 하나로 "진행 중 적용을 끝까지 기다린 뒤 새 접수를 거부한다"가 성립한다.
    """
    async def scenario():
        f = await producer(tmp_path, monkeypatch)
        pump = start_pump(f['engine'])
        runtime, ref = f['runtime'], f['ref']
        # 저장 상태만으로 B2 가 재구성해 접수할 관측을 만든다(observed 40 > applied 0).
        await observe(runtime, f, 40, D('400000'))
        observation = runtime._stored_observation(
            runtime.owner.state['attempts'][f['order'].attempt_id])
        reached, release = asyncio.Event(), asyncio.Event()
        original = f['store'].commit

        async def hold(expected_version, payload, commit_id, *args, **kwargs):
            # 접수(inbox)가 아니라 **경제 적용 commit** 을 잡는다 — 진행 중 apply 를 만든다.
            if commit_id.startswith('fill:'):
                reached.set()
                await release.wait()
            return await original(expected_version, payload, commit_id, *args, **kwargs)

        collect, _ = collector(f, [[response([other_row(ref)])]])
        closing = None
        try:
            monkeypatch.setattr(f['store'], 'commit', hold)
            runtime.start_reconciler(collect, interval=100.0, cycle_timeout=30.0)
            await asyncio.wait_for(reached.wait(), 3)
            closing = asyncio.create_task(runtime.shutdown())
            await asyncio.sleep(0)
            assert runtime._closing and not closing.done()
            release.set()
            await asyncio.wait_for(closing, 5)
            assert runtime.owner.state['inbox'][observation.observation_id]['status'] == 'APPLIED'
            assert runtime.health()['reconciler']['reconciler_running'] is False
            with pytest.raises(ApplicationBlocked, match='execution_application_closing'):
                await runtime.apply_observation(observation)
        finally:
            release.set()
            if closing is not None:
                await asyncio.gather(closing, return_exceptions=True)
            task, stop = pump
            stop.set()
            await asyncio.gather(task, return_exceptions=True)
            await f['store'].close()
    asyncio.run(scenario())


def test_the_loop_ends_naturally_on_closing_without_a_cancel(tmp_path, monkeypatch):
    async def scenario():
        f = await producer(tmp_path, monkeypatch, dispatch=False)
        pump = start_pump(f['engine'])
        collect, calls = collector(f, [[response([])]])
        try:
            loop = f['runtime'].start_reconciler(collect, interval=100.0, cycle_timeout=5.0)
            for _ in range(20):
                await asyncio.sleep(0)
            assert calls == [] and not loop.done()
            await asyncio.wait_for(f['runtime'].shutdown(), 3)
            assert loop.done() and not loop.cancelled() and loop.exception() is None
            assert f['runtime'].health()['reconciler']['reconciler_running'] is False
        finally:
            task, stop = pump
            stop.set()
            await asyncio.gather(task, return_exceptions=True)
            await f['store'].close()
    asyncio.run(scenario())


# ───────────────────────── ⑫ 주입 시계·중복 기동 거부 ─────────────────────────

def test_every_reconciler_stamp_comes_from_the_injected_clock(tmp_path, monkeypatch):
    async def scenario():
        f = await producer(tmp_path, monkeypatch)
        pump = start_pump(f['engine'])
        runtime, ref = f['runtime'], f['ref']
        collect, _ = collector(f, [[response([fill_row(ref, quantity=100, filled=100, amount=1000000)])]])
        try:
            frozen = f['clock'][0].isoformat()
            assert await runtime.reconcile_once(collect) is True
            health = runtime.health()['reconciler']
            assert health['last_cycle_started_at'] == frozen
            assert health['last_complete_at'] == frozen
            assert health['last_progress_at'] == frozen
        finally:
            await teardown(f, pump)
    asyncio.run(scenario())


# ─────────────────── ⑬ 적용 가능 술어: A 도 B 도 재접수하지 않는다 ───────────────────

def test_the_appliable_state_set_is_the_one_economics_enforces():
    """economics 는 이 단계의 허용 파일이 아니다 — 두 곳의 집합이 어긋나면 여기서 깨진다."""
    source = inspect.getsource(economics._matching_attempt)
    literal = source.split('attempt.get("state") not in ')[1].split(')')[0] + ')'
    assert set(ast.literal_eval(literal)) == set(APPLIABLE_ATTEMPT_STATES)


@pytest.mark.parametrize('flaw', ['evidence_conflict', 'unappliable_state'])
def test_an_attempt_economics_can_never_apply_is_not_requeried_or_readmitted(tmp_path, monkeypatch, flaw):
    """적용 불가 attempt 의 observed>applied 는 새 ingress 행을 만들지 않는다(P1).

    막지 않으면 주기마다 접수 → economics ValueError → FAILED 행이 무한히 쌓인다.
    lifecycle 은 두 표시(evidence_conflict·blocked_unknown)를 함께 세우지만 economics 는
    둘을 따로 거부하므로, 한쪽만 남기고 다른 쪽을 되돌려 술어의 두 반쪽을 각각 고정한다.
    """
    async def scenario():
        f = await producer(tmp_path, monkeypatch)
        pump = start_pump(f['engine'])
        runtime = f['runtime']
        await observe(runtime, f, 40, D('400000'))
        # 같은 수량에 더 작은 누적대금 — lifecycle 이 evidence_conflict 로 굳힌다.
        await observe(runtime, f, 40, D('300000'), accepted=False)
        attempt = runtime.owner.state['attempts'][f['order'].attempt_id]
        assert attempt['evidence_conflict'] is True and attempt['state'] == 'blocked_unknown'
        assert attempt['observed_quantity'] == 40 and attempt['applied_quantity'] == 0

        def isolate(state):
            row = state['attempts'][f['order'].attempt_id]
            if flaw == 'evidence_conflict':
                row['state'] = 'partial'   # 상태만 보면 적용 가능해 보이게 되돌린다
            else:
                row['evidence_conflict'] = False
            return state

        await runtime.owner.mutate('synthetic-isolate:' + flaw, isolate)
        applied = spy_apply(f)
        before = len(f['engine']._execution_ingress)
        collect, calls = collector(f, [[response([fill_row(f['ref'], quantity=100, filled=40,
                                                           amount=400000)])]])
        try:
            assert await runtime.reconcile_once(collect) is False
            assert calls == [] and applied == []
            assert len(f['engine']._execution_ingress) == before
            health = runtime.health()['reconciler']
            assert health['targets'] == 0
            assert health['skipped'] == {'unappliable_attempt': 1}
            assert health['apply_outcomes'] == {}
        finally:
            await teardown(f, pump)
    asyncio.run(scenario())


def test_an_inbox_row_whose_owning_attempt_is_unappliable_is_not_readmitted(tmp_path, monkeypatch):
    """B1 도 같은 술어를 쓴다 — 행의 order_key 로 소유 attempt 를 찾아서(P1).

    chain 이 아니라 **적용 가능성**만이 B1 을 막는다. 여기서 막지 않으면 잔존 행이
    주기마다 재접수돼 FAILED ingress 행만 늘어난다.
    """
    async def scenario():
        f = await producer(tmp_path, monkeypatch)
        pump = start_pump(f['engine'])
        runtime, ref = f['runtime'], f['ref']
        await observe(runtime, f, 40, D('400000'))
        observation = FillObservation(ref.account_scope, 'KR', ref.order_date, 'KRX', ref.order_no,
                                      SYMBOL, 'BUY', 40, D('400000'), org_no=ref.org_no,
                                      metadata={'entry_signal_score': 80.0})
        assert (await runtime.owner.receive(
            observation, ingress_context=runtime.ingress_context(1))).status == 'RECEIVED'
        # 행이 남은 뒤에 소유 attempt 가 conflict 로 굳는다.
        await observe(runtime, f, 40, D('300000'), accepted=False)
        applied = spy_apply(f)
        before = len(f['engine']._execution_ingress)
        collect, calls = collector(f, [[response([other_row(ref)])]])
        try:
            assert await runtime.reconcile_once(collect) is False
            assert calls == [] and applied == []
            assert len(f['engine']._execution_ingress) == before
            assert runtime.owner.state['inbox'][observation.observation_id]['status'] == 'RECEIVED'
            # B1(잔존 행)과 B2(재구성)가 같은 attempt 를 각각 한 번씩 거른다.
            assert runtime.health()['reconciler']['skipped'] == {'unappliable_attempt': 2}
        finally:
            await teardown(f, pump)
    asyncio.run(scenario())


# ────────────── ⑭ 미적용 inbox 행 하나로도 막힌 생산자를 이름 붙인다(P2-a) ──────────────

def test_an_unapplied_inbox_row_alone_names_the_stalled_producer(tmp_path, monkeypatch):
    async def scenario():
        f = await producer(tmp_path, monkeypatch)
        pump = start_pump(f['engine'])
        runtime, ref, now = f['runtime'], f['ref'], f['clock'][0]
        # 전량 관측 + 적용까지 반영해 조회 대상(A)을 0으로 만든다.
        await observe(runtime, f, 100, D('1000000'), applied=100)
        observation = FillObservation(ref.account_scope, 'KR', ref.order_date, 'KRX', ref.order_no,
                                      SYMBOL, 'BUY', 40, D('400000'), org_no=ref.org_no)
        assert (await runtime.owner.receive(
            observation, ingress_context=runtime.ingress_context(1))).status == 'RECEIVED'
        tried = []

        async def refuse(row):
            tried.append(row)
            raise ApplicationBlocked('synthetic_apply_refused')

        f['engine'].apply_execution_observation = refuse
        collect, calls = collector(f, [[response([])]])
        try:
            runtime.start_reconciler(collect, interval=1.0, cycle_timeout=5.0)

            async def one_cycle():
                # 재접수는 runtime 소유 task 안에서 돈다 — 호출 사실이 아니라 주기가
                # 사유를 확정한 순간을 기다려야 접미사를 고정해 단언할 수 있다.
                while runtime.health()['reconciler']['last_reason'] != 'apply_failed':
                    await asyncio.sleep(0)
            await asyncio.wait_for(one_cycle(), 3)
            assert tried
            assert calls == []  # 조회 대상 0 — 원장 TR 은 나가지 않았다
            health = runtime.health()['reconciler']
            assert health['targets'] == 0 and health['last_reason'] == 'apply_failed'
            assert runtime.reconciler_blocked_reason() is None  # 아직 k주기 전
            f['clock'][0] += timedelta(seconds=30)
            assert runtime.reconciler_blocked_reason() == 'reconciler_no_progress:apply_failed'
        finally:
            f['clock'][0] = now
            await teardown(f, pump)
    asyncio.run(scenario())


# ────────────── ⑮ 주기 도중 일자 park: parked 1, 그 주기는 진전이 아니다(P-9) ──────────

def test_a_day_park_during_the_cycle_is_parked_and_not_progress(tmp_path, monkeypatch):
    async def scenario():
        f = await producer(tmp_path, monkeypatch)
        pump = start_pump(f['engine'])
        runtime = f['runtime']
        await observe(runtime, f, 40, D('400000'))
        original = f['engine'].apply_execution_observation

        async def park(observation):
            # 일자 게이트를 지난 뒤 fence 가 닫히는 순간을 만든다 — B 는 InboxReceipt 를 받는다.
            runtime._day_closed = True
            return await original(observation)

        f['engine'].apply_execution_observation = park
        collect, _ = collector(f, [[response([other_row(f['ref'])])]])
        try:
            assert await runtime.reconcile_once(collect) is False
            health = runtime.health()['reconciler']
            assert health['parked'] == 1
            assert health['apply_outcomes'] == {'InboxReceipt:RECEIVED': 1}
            assert health['last_progress_at'] is None
            attempt = runtime.owner.state['attempts'][f['order'].attempt_id]
            assert attempt['applied_quantity'] == 0
        finally:
            runtime._day_closed = False
            await teardown(f, pump)
    asyncio.run(scenario())


def test_a_second_reconciler_is_refused_and_a_closed_runtime_starts_none(tmp_path, monkeypatch):
    async def scenario():
        f = await producer(tmp_path, monkeypatch, dispatch=False)
        pump = start_pump(f['engine'])
        collect, _ = collector(f, [[response([])]])
        try:
            f['runtime'].start_reconciler(collect, interval=100.0, cycle_timeout=5.0)
            with pytest.raises(ApplicationBlocked, match='reconciler_already_running'):
                f['runtime'].start_reconciler(collect)
            for bad in (0, -1, float('nan')):
                with pytest.raises(ValueError):
                    f['runtime'].start_reconciler(collect, interval=bad)
            await asyncio.wait_for(f['runtime'].shutdown(), 3)
            with pytest.raises(ApplicationBlocked, match='reconciler_admission_closed'):
                f['runtime'].start_reconciler(collect)
        finally:
            task, stop = pump
            stop.set()
            await asyncio.gather(task, return_exceptions=True)
            await f['store'].close()
    asyncio.run(scenario())


# ────────────── ⑯ 저장된 ACK 의 깨움은 호출자 취소와 분리된다(Codex 11차 P1) ──────────────

def test_a_stored_ack_wakes_the_cycle_even_when_the_caller_was_cancelled(tmp_path, monkeypatch):
    """결과 저장 중 호출자가 취소돼도 durable 결과가 commit 되면 주기가 깨어난다.

    저장 task 는 호출자 취소와 무관하게 살아남아 성공하므로 attempt 는 OPEN 이 된다.
    깨움이 호출자 뒤에만 있으면 수집기는 Event 에서 무기한 자고, 그 주문의 첫 체결은
    다음 ACK 가 올 때까지 보이지 않는다.
    """
    async def scenario():
        f = await producer(tmp_path, monkeypatch, dispatch=False)
        pump = start_pump(f['engine'])
        runtime = f['runtime']
        reached, release = asyncio.Event(), asyncio.Event()
        original = runtime.lifecycle.record_result

        async def record(*args, **kwargs):
            reached.set()
            await release.wait()
            return await original(*args, **kwargs)

        monkeypatch.setattr(runtime.lifecycle, 'record_result', record)
        collect, calls = collector(f, [[response([])]])
        caller = None
        try:
            runtime.start_reconciler(collect, interval=100.0, cycle_timeout=5.0)
            for _ in range(20):
                await asyncio.sleep(0)
            assert calls == []  # 미해결 시도 0 — Event 에서 자고 있다
            caller = asyncio.create_task(f['commands'].dispatch(
                f['order'], f['entry'](f['order']),
                GuardedKISTransport(f['broker'], request_builder=f['builder'])))
            await asyncio.wait_for(reached.wait(), 3)
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller
            assert calls == []  # 아직 저장 전이라 깨울 근거가 없다
            release.set()

            async def observed():
                while not calls:
                    await asyncio.sleep(0)
            await asyncio.wait_for(observed(), 3)
            assert calls == [(f['day'], f['day'], 'ALL', RECONCILER_MAX_PAGES)]
            assert runtime.owner.state['attempts'][f['order'].attempt_id]['state'] == 'open'
        finally:
            release.set()
            if caller is not None:
                await asyncio.gather(caller, return_exceptions=True)
            await teardown(f, pump)
    asyncio.run(scenario())


# ────────────── ⑰ 조회 예산은 주기 예산과 별개이고 B1 은 조회 앞에 있다 ──────────────

async def received_inbox_row(runtime, f):
    """B1 이 재접수할 잔존 RECEIVED 행 하나 — 관측 저장 뒤 적용 전에 멈춘 잔해다."""
    ref = f['ref']
    await observe(runtime, f, 40, D('400000'))
    observation = FillObservation(ref.account_scope, 'KR', ref.order_date, 'KRX', ref.order_no,
                                  SYMBOL, 'BUY', 40, D('400000'), org_no=ref.org_no,
                                  metadata={'entry_signal_score': 80.0})
    assert (await runtime.owner.receive(
        observation, ingress_context=runtime.ingress_context(1))).status == 'RECEIVED'
    return observation


def stuck_query(seen, started, release):
    async def stuck(*, start_date, end_date, exchange_scope, max_pages):
        seen.append(max_pages)
        started.set()
        await release.wait()
        raise AssertionError('조회는 자기 예산에서 끊겨야 한다')
    return stuck


def test_the_inbox_reapply_has_already_run_when_the_query_starts(tmp_path, monkeypatch):
    """(B1) 은 조회 **앞**에서 돈다(Codex 11차 P1).

    A 가 앞이면 조회가 예산을 소진한 주기에서 B 가 한 번도 돌지 않아, 이미 저장된
    RECEIVED 관측이 조회 장애와 같은 수명을 갖는다. 시각이 아니라 순서로 잰다 —
    조회에 들어선 순간 재접수가 끝나 있어야 한다.
    """
    async def scenario():
        f = await producer(tmp_path, monkeypatch)
        pump = start_pump(f['engine'])
        runtime = f['runtime']
        observation = await received_inbox_row(runtime, f)
        started, release, seen = asyncio.Event(), asyncio.Event(), []
        try:
            runtime.start_reconciler(stuck_query(seen, started, release),
                                     interval=100.0, cycle_timeout=30.0)
            await asyncio.wait_for(started.wait(), 5)
            assert runtime.owner.state['inbox'][observation.observation_id]['status'] in (
                'APPLIED', 'SUPERSEDED')
            assert f['engine'].portfolio.positions[SYMBOL].quantity == 40
        finally:
            release.set()
            await teardown(f, pump)
    asyncio.run(scenario())


def test_a_stuck_query_is_cut_by_its_own_budget_not_the_cycle_budget(tmp_path, monkeypatch):
    """조회에는 주기와 **별도**의 예산이 있고 `max_pages` 는 수집기까지 전달된다.

    조회 예산이 없으면 조회 한 번이 주기 예산 전체를 삼켜 B2 까지 굶는다. 파서에만
    상한을 걸고 수집기에 안 걸면 수집기는 기본 10페이지를 돌아 그 예산을 넘긴다.
    """
    async def scenario():
        f = await producer(tmp_path, monkeypatch)
        pump = start_pump(f['engine'])
        runtime = f['runtime']
        observation = await received_inbox_row(runtime, f)
        started, release, seen = asyncio.Event(), asyncio.Event(), []
        monkeypatch.setattr(runtime_module, 'RECONCILER_QUERY_TIMEOUT', 0.05)
        try:
            # 주기 예산(기본 60초)은 그대로 남는다 — 끊긴 것은 조회뿐이다.
            assert await runtime.reconcile_once(stuck_query(seen, started, release)) is True
            assert seen == [RECONCILER_MAX_PAGES] and started.is_set()
            assert runtime.owner.state['inbox'][observation.observation_id]['status'] in (
                'APPLIED', 'SUPERSEDED')
            assert runtime.health()['reconciler']['skipped'] == {'query_timeout': 1}
        finally:
            release.set()
            await teardown(f, pump)
    asyncio.run(scenario())


# ────────────── ⑱ 주기 타임아웃은 진행 중 owner 변경을 끊지 않는다 ──────────────

def test_a_cycle_timeout_never_cancels_an_owner_change_it_started(tmp_path, monkeypatch):
    """주기 예산은 '새 작업 시작'만 멈춘다(Codex 11차 P1).

    진행 중 SQL commit 을 취소하면 `_commit_publish` 가 owner 를 `_block()` 한 채 남겨
    다음 명령까지 전부 막힌다. 수락된 저장 작업은 runtime 소유 task 로 shield 하고
    종료 drain 이 그 완료를 기다린다.
    """
    async def scenario():
        f = await producer(tmp_path, monkeypatch)
        pump = start_pump(f['engine'])
        runtime, ref = f['runtime'], f['ref']
        reached, release = asyncio.Event(), asyncio.Event()
        original = f['store'].commit

        async def hold(expected_version, payload, commit_id, *args, **kwargs):
            # 주기가 시작한 관측 저장 commit 을 붙잡아 주기 타임아웃을 만든다.
            if commit_id.startswith('command:reconcile:'):
                reached.set()
                await release.wait()
            return await original(expected_version, payload, commit_id, *args, **kwargs)

        collect, _ = collector(f, [[response([fill_row(ref, quantity=100, filled=40,
                                                       amount=400000)])]])
        try:
            monkeypatch.setattr(f['store'], 'commit', hold)
            runtime.start_reconciler(collect, interval=100.0, cycle_timeout=0.05)
            await asyncio.wait_for(reached.wait(), 3)

            async def timed_out():
                while runtime.health()['reconciler']['last_reason'] != 'cycle_timeout':
                    await asyncio.sleep(0.005)
            await asyncio.wait_for(timed_out(), 5)
            # 주기는 끝났지만 그 주기가 시작한 owner 변경은 살아 있다(주기 task + 저장 task).
            assert len([task for task in runtime._reconciler_tasks if not task.done()]) == 2
            release.set()
            await asyncio.wait_for(runtime.shutdown(), 5)   # ③ drain 이 끝까지 기다린다
            assert runtime.owner.healthy is True            # ① _block 이 남지 않는다
            assert runtime.owner.publication_recovery_required is False
            attempt = runtime.owner.state['attempts'][f['order'].attempt_id]
            assert attempt['observed_quantity'] == 40       # ② 변경이 끝까지 게시된다
            assert attempt['state'] == 'partial'
        finally:
            release.set()
            task, stop = pump
            stop.set()
            await asyncio.gather(task, return_exceptions=True)
            await f['store'].close()
    asyncio.run(scenario())


# ────────────── ⑲ 퇴행 응답은 version 을 전진시키지 않는다(Codex 11차 P2) ──────────────

def test_a_regressed_response_never_advances_the_owner(tmp_path, monkeypatch):
    """저장 40주/400000 인데 응답이 20주/200000 이면 호출 자체를 건너뛴다.

    `lifecycle.reconcile` 은 `qty < old_qty` 를 무시하고 그대로 반환하지만 `owner.mutate`
    는 그 상태를 그대로 commit 해 version 만 전진시킨다(게시·전파 비용이 매 주기 발생).
    """
    async def scenario():
        f = await producer(tmp_path, monkeypatch)
        pump = start_pump(f['engine'])
        runtime, ref = f['runtime'], f['ref']
        await observe(runtime, f, 40, D('400000'), applied=40)
        collect, calls = collector(f, [[response([fill_row(ref, quantity=100, filled=20,
                                                           amount=200000)])]])
        try:
            version = runtime.owner.version
            for _ in range(10):
                assert await runtime.reconcile_once(collect) is False
            assert len(calls) == 10 and runtime.owner.version == version
            attempt = runtime.owner.state['attempts'][f['order'].attempt_id]
            assert attempt['observed_quantity'] == 40 and attempt['observed_amount'] == '400000'
            assert runtime.health()['reconciler']['skipped'] == {'regressed_observation': 10}
        finally:
            await teardown(f, pump)
    asyncio.run(scenario())


# ────────────── ⑳ 주기 예산·깨움·기록(Codex 12차) ──────────────

def hold_applies(f, reached, release):
    """재접수를 전부 `release` 까지 붙잡는다 — 그동안 어떤 주기도 B1 을 넘어가지 못한다.

    돌려주는 목록은 주기마다 하나씩 늘어난다(주기가 B1 에서 끊겨도 다음 주기가 다시 온다).
    """
    original = f['engine'].apply_execution_observation
    seen = []

    async def apply(observation):
        seen.append(observation)
        reached.set()
        await release.wait()
        return await original(observation)

    f['engine'].apply_execution_observation = apply
    return seen


def hold_commit(f, monkeypatch, prefix, reached, release):
    """첫 `prefix` commit 하나만 붙잡는다 — 주기보다 오래 사는 owner 작업을 만든다."""
    original = f['store'].commit

    async def hold(expected_version, payload, commit_id, *args, **kwargs):
        if commit_id.startswith(prefix) and not reached.is_set():
            reached.set()
            await release.wait()
        return await original(expected_version, payload, commit_id, *args, **kwargs)

    monkeypatch.setattr(f['store'], 'commit', hold)


async def wait_for_reason(runtime, reason, timeout=5):
    async def named():
        while runtime.health()['reconciler']['last_reason'] != reason:
            await asyncio.sleep(0.005)
    await asyncio.wait_for(named(), timeout)


async def wait_until(predicate, timeout=5):
    async def satisfied():
        while not predicate():
            await asyncio.sleep(0.005)
    await asyncio.wait_for(satisfied(), timeout)


def steady_clock(runtime, monkeypatch, start=1000.0):
    """주기 예산의 단조 시각을 시험이 쥔다 — 예산 경계를 실시간으로 기다리지 않는다."""
    value = [start]
    monkeypatch.setattr(runtime, '_steady', lambda: value[0])
    return value


def test_a_b1_timeout_does_not_leave_the_loop_asleep(tmp_path, monkeypatch):
    """B1 이 첫 주기 예산을 넘겨도 다음 주기는 스스로 돈다(Codex 12차 P1).

    `_reconciler_target_count` 는 B1 **뒤**에야 갱신된다 — B1 에서 주기가 끊기면 초기값 0 이
    남아 루프가 '대상 없음' 으로 보고 Event 에서 무기한 잔다. 잘지/돌지는 지난 주기의
    캐시가 아니라 호출 시점의 상태(`_stalled_targets`)로 정해야 한다.
    """
    async def scenario():
        f = await producer(tmp_path, monkeypatch)
        pump = start_pump(f['engine'])
        runtime = f['runtime']
        await received_inbox_row(runtime, f)
        reached, release = asyncio.Event(), asyncio.Event()
        seen = hold_applies(f, reached, release)
        collect, calls = collector(f, [[response([other_row(f['ref'])])]])
        try:
            runtime.start_reconciler(collect, interval=0.01, cycle_timeout=0.05)
            await asyncio.wait_for(reached.wait(), 5)
            await wait_for_reason(runtime, 'cycle_timeout')
            # 붙잡힌 채로 다음 주기가 온다 — 완료된 작업이 하나도 없으니 깨움이 아니라
            # 루프의 판정만이 근거다.
            await wait_until(lambda: len(seen) >= 2)
            assert calls == []   # 어떤 주기도 B1 에서 막혀 조회까지 가지 못한다
            release.set()
            await wait_until(lambda: bool(calls))   # 풀리면 그 주기가 조회까지 간다
        finally:
            release.set()
            await teardown(f, pump)
    asyncio.run(scenario())


def test_a_surviving_owner_task_wakes_a_loop_that_went_to_sleep(tmp_path, monkeypatch):
    """주기보다 오래 산 작업의 완료가 잠든 루프를 깨운다(Codex 12차 P1).

    루프가 '기다릴 것 없음' 으로 판정해 Event 에서 잠든 뒤 그 작업이 commit 하면, 깨울
    근거는 그 완료뿐이다 — 없으면 다음 ACK 까지 그 주문의 체결이 보이지 않는다.
    """
    async def scenario():
        f = await producer(tmp_path, monkeypatch)
        pump = start_pump(f['engine'])
        runtime = f['runtime']
        await received_inbox_row(runtime, f)
        reached, release = asyncio.Event(), asyncio.Event()
        hold_applies(f, reached, release)
        # 루프가 잠들도록 '기다릴 것 없음' 을 고정한다 — 깨움 말고는 다음 주기가 없다.
        monkeypatch.setattr(runtime, '_stalled_targets', lambda state, business_day: False)
        collect, calls = collector(f, [[response([other_row(f['ref'])])]])
        try:
            runtime.start_reconciler(collect, interval=0.01, cycle_timeout=0.05)
            await asyncio.wait_for(reached.wait(), 5)
            await wait_for_reason(runtime, 'cycle_timeout')
            for _ in range(50):
                await asyncio.sleep(0)
            assert calls == []   # 잠들었다 — interval 로는 깨어나지 않는다
            release.set()
            await wait_until(lambda: bool(calls))
        finally:
            release.set()
            await teardown(f, pump)
    asyncio.run(scenario())


@pytest.mark.parametrize('remaining,reason,queries', [(0.2, 'query_timeout', 1),
                                                      (-5.0, 'query_budget_exhausted', 0)])
def test_the_query_budget_is_the_cycle_deadline_minus_the_reapply_reserve(
        tmp_path, monkeypatch, remaining, reason, queries):
    """조회 예산은 고정 45초가 아니라 `(마감 − 재접수 유보)` 까지다(Codex 12차 P1).

    고정이면 B1 이 쓴 시간이 차감되지 않아 주기 예산이 조회 도중 먼저 끝나고 B2 가 통째로
    굶는다. 남은 예산이 0 이하면 조회는 시작조차 하지 않는다.
    """
    async def scenario():
        f = await producer(tmp_path, monkeypatch)
        pump = start_pump(f['engine'])
        runtime = f['runtime']
        await received_inbox_row(runtime, f)            # B1 이 재접수할 행(40주)
        await observe(runtime, f, 100, D('1000000'))    # B2 가 재구성할 잔여(100주)
        steady = steady_clock(runtime, monkeypatch)
        original = f['engine'].apply_execution_observation
        spent = []

        async def spend(observation):
            if not spent:   # B1 이 주기 예산의 대부분을 쓴다
                spent.append(observation)
                steady[0] += (runtime._reconciler_cycle_timeout
                              - runtime_module.RECONCILER_REAPPLY_RESERVE - remaining)
            return await original(observation)

        f['engine'].apply_execution_observation = spend
        started, release, seen = asyncio.Event(), asyncio.Event(), []
        try:
            assert await asyncio.wait_for(
                runtime.reconcile_once(stuck_query(seen, started, release)), 5) is True
            assert len(seen) == queries
            assert runtime.health()['reconciler']['skipped'] == {reason: 1}
            # 유보가 남아 B2 가 같은 주기에 돌았다.
            assert f['engine'].portfolio.positions[SYMBOL].quantity == 100
        finally:
            release.set()
            await teardown(f, pump)
    asyncio.run(scenario())


def test_a_query_that_ate_the_budget_starts_no_new_reconcile(tmp_path, monkeypatch):
    """A 의 대상 루프도 유보를 지킨다 — 남은 시간은 B2 의 것이다(Codex 12차 P1)."""
    async def scenario():
        f = await producer(tmp_path, monkeypatch)
        pump = start_pump(f['engine'])
        runtime, ref = f['runtime'], f['ref']
        await observe(runtime, f, 40, D('400000'))      # B2 가 재구성할 관측(40주)
        steady = steady_clock(runtime, monkeypatch)
        collect, calls = collector(f, [[response([fill_row(ref, quantity=100, filled=100,
                                                           amount=1000000)])]])

        async def slow(**kwargs):
            result = await collect(**kwargs)
            steady[0] += runtime._reconciler_cycle_timeout   # 조회가 유보까지 먹었다
            return result

        try:
            assert await runtime.reconcile_once(slow) is True
            assert len(calls) == 1
            attempt = runtime.owner.state['attempts'][f['order'].attempt_id]
            assert attempt['observed_quantity'] == 40    # reconcile 은 시작하지 않았다
            assert runtime.health()['reconciler']['skipped'] == {'reconcile_budget_exhausted': 1}
            assert f['engine'].portfolio.positions[SYMBOL].quantity == 40   # B2 는 돌았다
        finally:
            await teardown(f, pump)
    asyncio.run(scenario())


def test_a_cycle_timeout_does_not_lose_the_apply_outcome(tmp_path, monkeypatch):
    """대기자가 취소돼도 receipt 분류·진전 시각은 남는다(Codex 12차 P2).

    기록이 대기자 쪽에 있으면 주기 타임아웃이 그 줄에 도달하지 못해, 성공한 적용이
    관측창에서 통째로 사라진다.
    """
    async def scenario():
        f = await producer(tmp_path, monkeypatch)
        pump = start_pump(f['engine'])
        runtime = f['runtime']
        await received_inbox_row(runtime, f)
        reached, release = asyncio.Event(), asyncio.Event()
        hold_commit(f, monkeypatch, 'fill:', reached, release)
        collect, _ = collector(f, [[response([other_row(f['ref'])])]])
        try:
            runtime.start_reconciler(collect, interval=100.0, cycle_timeout=0.05)
            await asyncio.wait_for(reached.wait(), 5)
            await wait_for_reason(runtime, 'cycle_timeout')
            assert runtime.health()['reconciler']['apply_outcomes'] == {}
            release.set()
            await wait_until(lambda: runtime.health()['reconciler']['apply_outcomes'])
            health = runtime.health()['reconciler']
            assert health['apply_outcomes'] == {'FillReceipt:APPLIED': 1}
            assert health['last_progress_at'] == f['clock'][0].isoformat()
        finally:
            release.set()
            await teardown(f, pump)
    asyncio.run(scenario())


def test_a_cycle_timeout_does_not_lose_the_reconcile_progress(tmp_path, monkeypatch):
    """저장 뒤 observed 증가 판정도 task 본문에 있다(Codex 12차 P2).

    재접수는 실패시켜 둔다 — 여기서 갱신되는 진전 시각의 출처는 살아남은 reconcile 뿐이다.
    """
    async def scenario():
        f = await producer(tmp_path, monkeypatch)
        pump = start_pump(f['engine'])
        runtime, ref = f['runtime'], f['ref']
        reached, release = asyncio.Event(), asyncio.Event()
        hold_commit(f, monkeypatch, 'command:reconcile:', reached, release)

        async def refuse(observation):
            raise ApplicationBlocked('synthetic_apply_refused')

        f['engine'].apply_execution_observation = refuse
        collect, _ = collector(f, [[response([fill_row(ref, quantity=100, filled=40,
                                                       amount=400000)])]])
        try:
            runtime.start_reconciler(collect, interval=100.0, cycle_timeout=0.05)
            await asyncio.wait_for(reached.wait(), 5)
            await wait_for_reason(runtime, 'cycle_timeout')
            assert runtime.health()['reconciler']['last_progress_at'] is None
            release.set()
            await wait_until(
                lambda: runtime.health()['reconciler']['last_progress_at'] is not None)
            attempt = runtime.owner.state['attempts'][f['order'].attempt_id]
            assert attempt['observed_quantity'] == 40
        finally:
            release.set()
            await teardown(f, pump)
    asyncio.run(scenario())
