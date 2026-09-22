"""P0-2 2단계: runtime 소유 체결 대사 주기(제품 호출자 0건·live 파일 0줄).

실제 owner/store(tmp_path)·실제 요청 binding·실제 수집기(`LegacyExecutionQueries`)·실제
`engine.apply_execution_observation` 큐를 쓴다. 합성 응답 페이지는 실 KIS 취소/체결
최종성의 증명이 아니다 — 여기서 GREEN 인 것은 주기의 계약(게이트·조회 횟수·chain 처분·
저장 상태 재처리·종료)뿐이며 설치 승인이 아니다.
"""
import asyncio
from datetime import timedelta
from decimal import Decimal as D

import pytest

from src.execution.safety.application import ApplicationBlocked, FillObservation, observation_from_evidence
from src.execution.safety.evidence import evidence_pages, parse_order_evidence, parser_scope
from src.execution.safety.lifecycle import CommandStatus, OrderEvidence, OrderState
from src.execution.safety.queries import LegacyExecutionQueries
from src.execution.safety.runtime import RECONCILER_MAX_PAGES
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

    async def collect(*, start_date, end_date, exchange_scope):
        calls.append((start_date, end_date, exchange_scope))
        pages = script[min(len(calls), len(script)) - 1]
        fetched = []

        async def fetch(request):
            fetched.append(request)
            item = pages[len(fetched) - 1]
            if isinstance(item, BaseException):
                raise item
            return item

        queries = LegacyExecutionQueries(fetch, clock=lambda: f['clock'][0],
                                         request_timeout=1, max_pages=RECONCILER_MAX_PAGES)
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


async def observe(runtime, f, quantity, amount):
    """주기 **밖**에서 관측만 저장한다(생산자가 만든 상태가 아니라 준비 상태다)."""
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
    assert await runtime.lifecycle.reconcile(f['order'].attempt_id, evidence)


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
            assert calls == [(f['day'], f['day'], 'ALL')]
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
            assert calls == [(f['day'], f['day'], 'ALL')]
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


# ───────────────────────── ④ chain 행: reconcile·apply 둘 다 0 ─────────────────────

def test_a_chain_row_blocks_both_reconcile_and_the_stored_reapply(tmp_path, monkeypatch):
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
        collection = await collect(start_date=f['day'], end_date=f['day'], exchange_scope='ALL')
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
                while len(calls) < 2:
                    await asyncio.sleep(0.01)
            await asyncio.wait_for(cycles(), 3)
            assert runtime.reconciler_blocked_reason() is None  # 아직 k주기를 넘지 않았다
            f['clock'][0] += timedelta(seconds=30)
            # 주기 task 는 살아 있다 — task.done() 이 아니라 상태 전진으로 재기 때문에 잡힌다.
            assert runtime.health()['reconciler']['reconciler_running'] is True
            assert runtime.reconciler_blocked_reason().startswith('reconciler_no_progress:')
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
    async def scenario():
        f = await producer(tmp_path, monkeypatch)
        pump = start_pump(f['engine'])
        runtime, ref = f['runtime'], f['ref']
        await observe(runtime, f, 40, D('400000'))
        observation = FillObservation(ref.account_scope, 'KR', ref.order_date, 'KRX', ref.order_no,
                                      SYMBOL, 'BUY', 40, D('400000'), org_no=ref.org_no)
        reached, release = asyncio.Event(), asyncio.Event()
        original = f['store'].commit

        async def hold(expected_version, payload, commit_id, *args, **kwargs):
            # 접수(inbox)가 아니라 **경제 적용 commit** 을 잡는다 — 진행 중 apply 를 만든다.
            if commit_id.startswith('fill:'):
                reached.set()
                await release.wait()
            return await original(expected_version, payload, commit_id, *args, **kwargs)

        tasks = []
        try:
            monkeypatch.setattr(f['store'], 'commit', hold)
            ingress = asyncio.create_task(f['engine'].apply_execution_observation(observation))
            tasks.append(ingress)
            await asyncio.wait_for(reached.wait(), 2)
            closing = asyncio.create_task(runtime.shutdown())
            tasks.append(closing)
            await asyncio.sleep(0)
            assert runtime._closing and not closing.done()
            release.set()
            await asyncio.wait_for(asyncio.gather(*tasks), 3)
            assert runtime.owner.state['inbox'][observation.observation_id]['status'] == 'APPLIED'
            with pytest.raises(ApplicationBlocked, match='execution_application_closing'):
                await runtime.apply_observation(observation)
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
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
