"""S3-3: dispatch 의 claim 이전 실패 분류 · 사유 보존 · hybrid 대조 · 재유도 시계.

전부 합성 fake HTTP · 주입 시계 · 시험용 합성 startup 허가 위의 결과다. S3 는 설치가
아니며 제품의 attach 호출자는 0건, `trading_ready` 는 제품 코드에서 계속 False 다.

실행: venv/bin/python -m pytest tests/test_execution_dispatch_reasons.py -q -p no:cacheprovider
"""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path
import re

import pytest

from src.core.types import Order, OrderSide, OrderType
from src.execution.safety.decisions import ConsumedSource
from src.execution.safety.lifecycle import CommandStatus, OrderRef
from src.execution.safety.requests import CancelParent
from src.execution.safety.transport import GuardedKISTransport

from test_execution_command_owner import fixture as command_fixture
from test_execution_decision_facts import (
    JUSTIFIED, apply_change, make_facts, nominal, paused_dispatch, publish,
)
from test_execution_regime_recheck import (
    fixture as regime_fixture, regime_digest,
)
from test_execution_risk_policy import NOW


@pytest.fixture(autouse=True)
def synthetic_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)


SECTOR = '반도체'
# 사유는 운영자 로그용 코드다 — 금액·계좌·수량이 섞이면 이 형식에서 먼저 깨진다(결정 ⑬).
CODE = re.compile(r'[a-z_]+')


def transport(f):
    return GuardedKISTransport(f['broker'], request_builder=f['builder'])


async def send(f, req):
    return await f['commands'].dispatch(req, f['entry'](req), transport(f))


def reservations(attempt):
    """예약 4항. 미송신으로 끝난 시도에는 한 항도 남으면 안 된다."""
    return (attempt['reserved_quantity'], D(attempt['reserved_cash']),
            D(attempt['reserved_exposure']), attempt['reserved_planned_risk'])


def pending_sectors(f):
    return f['runtime'].owner.state['entry_policy_effects']['pending_sectors']


def spy_abandon(f, monkeypatch):
    """실제 전이를 그대로 부르되 호출 자체를 기록한다(호출 0건도 인수 조건이다)."""
    calls = []
    original = f['runtime'].lifecycle.abandon_candidate

    async def record(attempt_id, *, reason):
        result = await original(attempt_id, reason=reason)
        calls.append((attempt_id, reason, result))
        return result

    monkeypatch.setattr(f['runtime'].lifecycle, 'abandon_candidate', record)
    return calls


async def prepared(tmp_path, monkeypatch, *, ready=True, quantity=10):
    """자동 BUY 한 건을 섹터까지 실어 prepare 해 둔 상태."""
    f = await command_fixture(tmp_path, monkeypatch, ready=ready, origin='automatic',
                              policy=nominal())
    req = f['request'](quantity=quantity)
    await f['quote'](req)
    facts = await f['facts'](req, sector=SECTOR)
    await f['commands'].prepare(req, f['entry'](req))
    return f, req, facts


# ── 미claim prepared 의 종료 (결정 ⑦) ────────────────────────────────────

def test_claim_before_failure_ends_the_attempt_and_frees_every_reservation(tmp_path, monkeypatch):
    """오늘은 prepared·예약 4항·pending 섹터가 그대로 남아 다음 판단의 자원을 갉아먹는다."""
    async def scenario():
        f, req, _ = await prepared(tmp_path, monkeypatch, ready=False, quantity=JUSTIFIED)
        try:
            attempt = f['runtime'].owner.state['attempts']['A']
            assert reservations(attempt) == (JUSTIFIED, D('507500.000'), D('500000'), None)
            assert pending_sectors(f) == {req.symbol: SECTOR}

            result = await send(f, req)
            assert result.status is CommandStatus.NOT_SENT
            assert result.reason_code == 'startup_reconciliation'
            assert f['broker']._session.posts == []
            current = f['runtime'].owner.state['attempts']['A']
            assert current['state'] == 'final_rejected'
            assert current['command_status'] == 'not_sent'
            assert current['claim_id'] is None
            assert current['reason_code'] == 'startup_reconciliation'
            assert reservations(current) == (0, D('0'), D('0'), None)
            assert pending_sectors(f) == {}

            # 같은 intent 의 다음 시도가 다시 열린다(끝나지 않은 앞 시도가 막지 않는다).
            again = f['builder'].prepare_submit(
                Order(symbol=req.symbol, side=OrderSide.BUY, quantity=JUSTIFIED,
                      price=req.valuation_price, order_type=OrderType.LIMIT,
                      strategy=req.strategy),
                intent_id=req.intent_id, attempt_id='A2', session=req.session,
                valuation_price=req.valuation_price)
            reopened = await f['commands'].prepare(again, f['entry'](again))
            assert reopened['state'] == 'prepared'
        finally: await f['store'].close()
    asyncio.run(scenario())


@pytest.mark.parametrize('fault, reason', [
    ('missing_binding', 'bound_attempt_required'),
    ('startup', 'startup_reconciliation'),
    ('expired', 'decision_facts_expired'),
])
def test_claim_before_failures_keep_their_own_reason(tmp_path, monkeypatch, fault, reason):
    """오늘은 원인 셋이 전부 claim_not_available 로 뭉개져 운영자가 구분할 수 없다."""
    async def scenario():
        f, req, _ = await prepared(tmp_path, monkeypatch, ready=fault != 'startup')
        try:
            if fault == 'missing_binding':
                # prepare 하지 않은 다른 시도 — binding 자체가 없다.
                req = f['request']('B', symbol='000660')
                await f['quote'](req)
            if fault == 'expired':
                f['clock'][0] = NOW + timedelta(minutes=45)
            result = await send(f, req)
            assert result.status is CommandStatus.NOT_SENT
            assert result.reason_code == reason
            assert CODE.fullmatch(result.reason_code)
            assert f['broker']._session.posts == []
        finally: await f['store'].close()
    asyncio.run(scenario())


def test_a_foreign_claim_blocks_the_abandon_and_keeps_the_reservation(tmp_path, monkeypatch):
    """대조: 남의 claim 이 붙은 행은 이미 송신됐을 수 있다 — 거부를 삼키지 않고 예약을 지킨다."""
    async def scenario():
        f, req, _ = await prepared(tmp_path, monkeypatch, quantity=JUSTIFIED)
        calls = spy_abandon(f, monkeypatch)
        try:
            def seize(state):
                state['attempts']['A']['claim_id'] = 'synthetic-other-sender'
                return state
            await f['runtime'].owner.mutate('synthetic-foreign-claim', seize)
            before = reservations(f['runtime'].owner.state['attempts']['A'])

            result = await send(f, req)
            assert (result.status, result.reason_code) == (
                CommandStatus.NOT_SENT, 'claim_not_available')
            assert calls == [('A', 'claim_not_available', False)]
            current = f['runtime'].owner.state['attempts']['A']
            assert (current['state'], current['claim_id']) == ('prepared', 'synthetic-other-sender')
            assert reservations(current) == before
            assert f['broker']._session.posts == []
        finally: await f['store'].close()
    asyncio.run(scenario())


def test_closed_day_admission_reports_admission_closed_without_abandoning(tmp_path, monkeypatch):
    """종료·일자 전환 중에는 새 종료 전이를 시작하지 않는다 — 그 자체가 다시 던진다."""
    async def scenario():
        f, req, _ = await prepared(tmp_path, monkeypatch)
        calls = spy_abandon(f, monkeypatch)
        try:
            f['runtime']._day_closed = True
            before = f['runtime'].owner.version
            result = await send(f, req)
            assert (result.status, result.reason_code) == (
                CommandStatus.NOT_SENT, 'command_admission_closed')
            assert calls == []
            assert f['runtime'].owner.version == before
            assert f['runtime'].owner.state['attempts']['A']['state'] == 'prepared'
            assert f['broker']._session.posts == []
        finally: await f['store'].close()
    asyncio.run(scenario())


def test_an_unexpected_failure_is_named_dispatch_failed_and_abandons_nothing(tmp_path, monkeypatch):
    """저장 장애는 '보낼 수 없었다'는 판정이 아니다 — 예약을 풀지 않고 사유를 분리한다."""
    async def scenario():
        f, req, _ = await prepared(tmp_path, monkeypatch)
        calls = spy_abandon(f, monkeypatch)
        try:
            async def broken(*args, **kwargs):
                raise OSError('synthetic-claim-storage-failure')
            monkeypatch.setattr(f['runtime'].lifecycle, 'claim', broken)
            before = reservations(f['runtime'].owner.state['attempts']['A'])
            result = await send(f, req)
            assert (result.status, result.reason_code) == (
                CommandStatus.NOT_SENT, 'dispatch_failed')
            assert calls == []
            current = f['runtime'].owner.state['attempts']['A']
            assert current['state'] == 'prepared'
            assert reservations(current) == before
            assert f['broker']._session.posts == []
        finally: await f['store'].close()
    asyncio.run(scenario())


def test_a_failure_after_the_claim_is_recorded_not_abandoned(tmp_path, monkeypatch):
    """claim 이후 실패는 record_result→FINAL_REJECTED 가 맡는다 — 종료 전이를 겹쳐 걸지 않는다."""
    async def scenario():
        f, req, facts = await prepared(tmp_path, monkeypatch, quantity=JUSTIFIED)
        calls = spy_abandon(f, monkeypatch)
        try:
            task, release = await paused_dispatch(f, req, boundary='connect')
            await apply_change(f, req, facts, 'config')
            release.set()
            result = await task
            assert result.status is CommandStatus.NOT_SENT
            assert calls == []
            current = f['runtime'].owner.state['attempts']['A']
            assert current['state'] == 'final_rejected'
            assert current['claim_id'] is not None
            assert reservations(current) == (0, D('0'), D('0'), None)
            assert f['broker']._session.posts == []
        finally: await f['store'].close()
    asyncio.run(scenario())


# ── 결정 ⑨: 미claim 자식 명령도 끝난다(S4-1b) ───────────────────────────

def test_an_unclaimed_cancel_is_ended_while_the_acknowledged_submit_is_not(tmp_path, monkeypatch):
    """자식 행이 싣는 order_ref 는 부모 것이다 — lifecycle 가드가 그 한 항만 면제해 끝낸다.

    부모(ACK 된 SUBMIT)는 한 글자도 바뀌지 않고, 끝난 자식은 다음 취소를 다시 열어 준다.
    """
    async def scenario():
        f, req, _ = await prepared(tmp_path, monkeypatch)
        try:
            assert (await send(f, req)).status is CommandStatus.ACKNOWLEDGED
            parent = f['runtime'].owner.state['attempts']['A']
            cancel = f['builder'].prepare_cancel(intent_id=req.intent_id, attempt_id='C',
                session=req.session, parent=CancelParent(req.intent_id, req.attempt_id,
                    parent['version'], OrderRef.from_dict(parent['order_ref']), req.symbol,
                    req.side, req.order_type, parent['reserved_quantity'],
                    req.valuation_price, req.strategy))
            await f['commands'].prepare(cancel, f['entry'](cancel))
            before = dict(f['runtime'].owner.state['attempts']['C'])
            parent_before = dict(f['runtime'].owner.state['attempts']['A'])
            calls = spy_abandon(f, monkeypatch)

            def forge(state):
                state['attempts']['C']['request_binding']['parent_version'] = 99
                return state
            await f['runtime'].owner.mutate('synthetic-forged-cancel-binding', forge)
            posted = len(f['broker']._session.posts)
            result = await send(f, cancel)
            assert result.status is CommandStatus.NOT_SENT
            assert calls == [('C', 'request_binding_changed', True)]
            current = f['runtime'].owner.state['attempts']['C']
            assert (current['state'], current['claim_id']) == ('final_rejected', None)
            assert (current['command_status'], current['reason_code']) == (
                'not_sent', result.reason_code)
            assert reservations(current) == reservations(before)
            assert f['runtime'].owner.state['attempts']['A'] == parent_before
            assert len(f['broker']._session.posts) == posted

            # 끝난 자식은 같은 부모의 다음 취소를 막지 않는다(전에는 previous child command unresolved).
            again = f['builder'].prepare_cancel(intent_id=req.intent_id, attempt_id='C2',
                session=req.session, parent=CancelParent(req.intent_id, req.attempt_id,
                    parent['version'], OrderRef.from_dict(parent['order_ref']), req.symbol,
                    req.side, req.order_type, parent['reserved_quantity'],
                    req.valuation_price, req.strategy))
            reopened = await f['commands'].prepare(again, f['entry'](again))
            assert reopened['state'] == 'prepared'
        finally: await f['store'].close()
    asyncio.run(scenario())


# ── hybrid 축 대조 (S3-2 가 실은 축의 첫 소비자) ─────────────────────────

@pytest.mark.parametrize('configured, declared, reason', [
    (True, False, 'decision_hybrid_mismatch'),
    (True, True, 'unsupported_hybrid_sizing'),
    (False, False, None),
])
def test_declared_hybrid_must_match_the_published_policy(tmp_path, monkeypatch,
                                                         configured, declared, reason):
    """게시자 자기신고만 믿으면 '설정은 hybrid on 인데 신고는 off' 가 그대로 통과한다."""
    async def scenario():
        f = await command_fixture(tmp_path, monkeypatch, origin='automatic',
                                  policy=replace(nominal(), hybrid_enabled=configured))
        try:
            req = f['request'](quantity=10)
            await f['quote'](req)
            await publish(f, make_facts(req, hybrid_enabled=declared))
            if reason is None:
                attempt = await f['commands'].prepare(req, f['entry'](req))
                assert attempt['state'] == 'prepared'
                return
            with pytest.raises(ValueError, match=reason):
                await f['commands'].prepare(req, f['entry'](req))
            assert f['runtime'].owner.state['attempts'] == {}
            assert f['broker']._session.posts == []
        finally: await f['store'].close()
    asyncio.run(scenario())


# ── config 축이 처음으로 실효를 갖는다 (결정 ⑨) ─────────────────────────

def test_a_new_config_version_after_prepare_blocks_the_post_and_frees_the_reservation(
        tmp_path, monkeypatch):
    async def scenario():
        f, req, facts = await prepared(tmp_path, monkeypatch, quantity=JUSTIFIED)
        try:
            await apply_change(f, req, facts, 'config')
            result = await send(f, req)
            assert (result.status, result.reason_code) == (
                CommandStatus.NOT_SENT, 'stale_decision_config_version')
            assert f['broker']._session.posts == []
            current = f['runtime'].owner.state['attempts']['A']
            assert current['state'] == 'final_rejected'
            assert reservations(current) == (0, D('0'), D('0'), None)
            assert pending_sectors(f) == {}
        finally: await f['store'].close()
    asyncio.run(scenario())


# ── 재유도는 송신 시각으로 한다 (schema1 의 당일 게이트) ─────────────────

@pytest.mark.parametrize('crash', [True, False])
def test_regime_rederivation_uses_the_dispatch_clock_not_the_decision_time(
        tmp_path, monkeypatch, crash):
    """판단 시각으로 재유도하면 판단~송신 사이의 급락이 당일 게이트 밖으로 밀려 가려진다."""
    async def scenario():
        f = await regime_fixture(tmp_path, monkeypatch)
        try:
            assert f['runtime'].owner.state['regime_policy'].get('schema', 1) == 1
            # 같은 순간을 UTC 표기로 담으면 naive `.date()` 만 전일이 된다.
            decided = f['clock'][0].replace(hour=8).astimezone(timezone.utc)
            assert decided.date() != f['clock'][0].date()
            req = f['request']()
            await f['market_quote'](req)
            await f['commands'].publish_qualification_source('regime', as_of=decided,
                digest=regime_digest('bull'), expected_version=f['runtime'].owner.version)
            row = f['commands'].read_qualification_source('regime')
            source = ConsumedSource('regime', row['version'],
                                    datetime.fromisoformat(row['as_of']), row['digest'])
            await publish(f, make_facts(req, sources=(source,), decided_at=decided), sources=False)
            await f['commands'].prepare(req, f['entry'](req))
            if crash:
                await f['intraday']('crash', f['clock'][0])
            result = await f['send'](req)
            if not crash:
                assert result.status is CommandStatus.ACKNOWLEDGED
                assert len(f['broker']._session.posts) == 1
                return
            assert (result.status, result.reason_code) == (
                CommandStatus.NOT_SENT, 'stale_regime_decision')
            assert f['broker']._session.posts == []
        finally:
            await f['runtime'].shutdown(); await f['store'].close()
    asyncio.run(scenario())


# ── 결정 ⑬: 사유는 코드다 ───────────────────────────────────────────────

def test_reason_codes_never_carry_money_account_or_quantity(tmp_path, monkeypatch):
    """금액 gate 가 막아도 사유에 금액·계좌·종목·수량이 실리면 안 된다."""
    async def scenario():
        f, req, facts = await prepared(tmp_path, monkeypatch, quantity=JUSTIFIED)
        try:
            await apply_change(f, req, facts, 'cash')
            result = await send(f, req)
            assert result.status is CommandStatus.NOT_SENT
            assert CODE.fullmatch(result.reason_code)
            assert result.reason_code == 'decision_quantity_unjustified'
            secrets = (req.symbol, req.account.account_scope, str(req.quantity),
                       str(req.valuation_price))
            assert not any(secret in result.reason_code for secret in secrets)
            assert f['broker']._session.posts == []
        finally: await f['store'].close()
    asyncio.run(scenario())


def test_a_closing_runtime_racing_the_abandon_is_reported_as_admission_closed(tmp_path, monkeypatch):
    """종료 전이 도중 admission 이 닫히면 원래 사유가 아니라 '종료 중'으로 보고한다.

    dispatch 진입 전에 닫힌 경우와 달리, claim 이전 실패를 끝내려는 abandon 이 던지는 경합이다.
    """
    from src.execution.safety.application import ApplicationBlocked

    async def scenario():
        f, req, _ = await prepared(tmp_path, monkeypatch, ready=False)

        async def blocked(attempt_id, *, reason):
            raise ApplicationBlocked('day_transition_admission_closed')

        monkeypatch.setattr(f['runtime'].lifecycle, 'abandon_candidate', blocked)
        try:
            result = await send(f, req)
            assert (result.status, result.reason_code) == (
                CommandStatus.NOT_SENT, 'command_admission_closed')
            assert f['runtime'].owner.state['attempts']['A']['state'] == 'prepared'
            assert f['broker']._session.posts == []
        finally: await f['store'].close()
    asyncio.run(scenario())


def test_a_session_that_closes_after_prepare_ends_the_attempt(tmp_path, monkeypatch):
    """prepare 뒤 장 경계가 닫혀도 '보내지 못한 요청은 예약을 남기지 않는다'(계약 5).

    세션 재검사가 claim 이전 분류 바깥에서 터지면 예외가 그대로 빠져나가 예약이 남는다.
    """
    from src.execution.safety.guards import GuardDecision

    async def scenario():
        f, req, _ = await prepared(tmp_path, monkeypatch)
        calls = spy_abandon(f, monkeypatch)
        try:
            f['session'][0] = GuardDecision(False, 'market_closed')
            result = await send(f, req)
            assert (result.status, result.reason_code) == (CommandStatus.NOT_SENT, 'market_closed')
            assert calls == [('A', 'market_closed', True)]
            attempt = f['runtime'].owner.state['attempts']['A']
            assert attempt['state'] == 'final_rejected' and attempt['claim_id'] is None
            assert reservations(attempt) == (0, D('0'), D('0'), None)
            assert pending_sectors(f) == {}
            assert f['broker']._session.posts == []
        finally: await f['store'].close()
    asyncio.run(scenario())


# ── 대조: claim 이후 실패한 자식은 S4-1b 도 풀지 않는다 (계획 §1 사실 3) ──

async def cancel_child(tmp_path, monkeypatch):
    """ACK 된 부모 SUBMIT 한 건과 그 밑에 prepare 만 해 둔 취소 자식."""
    f, req, _ = await prepared(tmp_path, monkeypatch)
    assert (await send(f, req)).status is CommandStatus.ACKNOWLEDGED
    parent = f['runtime'].owner.state['attempts']['A']
    cancel = f['builder'].prepare_cancel(intent_id=req.intent_id, attempt_id='C',
        session=req.session, parent=CancelParent(req.intent_id, req.attempt_id,
            parent['version'], OrderRef.from_dict(parent['order_ref']), req.symbol,
            req.side, req.order_type, parent['reserved_quantity'],
            req.valuation_price, req.strategy))
    await f['commands'].prepare(cancel, f['entry'](cancel))
    return f, cancel


@pytest.mark.parametrize('failure, status, command_status, reason, posted', [
    ('transport', CommandStatus.UNKNOWN, 'unknown', 'dispatch_unconfirmed', 1),
    ('guard', CommandStatus.NOT_SENT, 'not_sent', 'current_request_guard_rejected', 0),
])
def test_a_child_that_failed_after_the_claim_is_not_abandoned(tmp_path, monkeypatch, failure,
                                                              status, command_status, reason, posted):
    """claim 이 붙은 자식은 '보냈을 수 있는' 행이다 — 잔류를 상태값까지 고정한다.

    이 잔류를 푸는 것은 취소·체결 최종성의 증거 계약(10A2/10C)이지 S4 가 아니다.
    """
    from test_kr_final_dispatch import Response

    async def scenario():
        f, cancel = await cancel_child(tmp_path, monkeypatch)
        try:
            parent_before = dict(f['runtime'].owner.state['attempts']['A'])
            calls = spy_abandon(f, monkeypatch)
            before = len(f['broker']._session.posts)
            if failure == 'transport':
                # POST 는 나갔고 응답만 읽지 못한 경우 — 접수 여부를 모른다.
                f['broker']._session.response = Response(
                    data={'rt_cd': '0'}, error=OSError('synthetic-cancel-transport-failure'))
                result = await send(f, cancel)
            else:
                task, release = await paused_dispatch(f, cancel, boundary='hashkey')
                f['commands']._permits.clear()
                release.set()
                result = await task
            assert (result.status, result.reason_code) == (status, reason)
            assert calls == []
            current = f['runtime'].owner.state['attempts']['C']
            assert (current['state'], current['command_status']) == ('reconciling', command_status)
            assert current['claim_id'] is not None
            assert len(f['broker']._session.posts) == before + posted

            # 어떤 abandon 가드로도 끝낼 수 없다(직접 불러도 거부).
            child_before = dict(current)
            assert await f['runtime'].lifecycle.abandon_candidate(
                'C', reason='synthetic-direct-abandon') is False
            assert calls == [('C', 'synthetic-direct-abandon', False)]
            assert f['runtime'].owner.state['attempts']['C'] == child_before
            assert f['runtime'].owner.state['attempts']['A'] == parent_before
            assert reservations(f['runtime'].owner.state['attempts']['A']) == reservations(parent_before)
        finally: await f['store'].close()
    asyncio.run(scenario())


# ── P0-4 S-C: 세션 경계의 조용한 소멸 (약한 지점 7) ─────────────────────
#
# attach 는 **전선 본문을 prepare 시점에 굽는다** — 세션이 `ORD_DVSN`·`AFHR_FLPR_YN` 과
# fingerprint 와 증거 provenance 에 통째로 들어간다(`requests.py:251-260`·`199-202`·`231`,
# `runtime.py:883-896`). 그래서 dispatch 시점에 세션이 바뀌면 그 요청은 **그 세션에 대해
# 틀린 주문**이고, owner 는 보내지 않고 시도를 끝낸다. 아래 시험은 그 계약을 고정한다.
# `session_guard` 의 제품 구현은 0건이므로 guard 는 허용으로 주입해 세션 경계만 격리한다.

T1519 = NOW.replace(hour=15, minute=19)   # regular 의 마지막 1분
T1521 = NOW.replace(hour=15, minute=21)   # closing(15:20~15:30)
T1531 = NOW.replace(hour=15, minute=31)   # break(15:30~15:40)
T1541 = NOW.replace(hour=15, minute=41)   # next_market(15:40~20:00)


PROTECTED = 60


def session_request(f, moment, *, aid='A', order_type=OrderType.LIMIT, quantity=PROTECTED,
                    intent_id=None):
    """그 순간의 세션으로 구운 보호 SELL. 세션은 DTO 생성 시점에 고정된다."""
    from src.execution.safety.requests import RequestSession, _session_at
    return f['builder'].prepare_submit(
        Order(symbol='005930', side=OrderSide.SELL, quantity=quantity, price=D('10000'),
              order_type=order_type, strategy='sepa_trend'),
        intent_id='I-A' if intent_id is None else intent_id, attempt_id=aid,
        session=RequestSession(moment.date().isoformat(), moment, _session_at(moment)),
        valuation_price=D('10000'))


async def prepared_at(tmp_path, monkeypatch, moment, **changes):
    """주입 시계를 `moment` 로 옮긴 뒤 그 세션의 보호 SELL 한 건을 prepare 해 둔다.

    `ready=True` 는 기존 dispatch 시험과 같은 합성 startup 허가다 — 그래야 dispatch 가
    `startup_reconciliation` 이 아니라 세션 사유로 끝난다. 보호 SELL 을 쓰는 이유는 그것이
    약한 지점 7 의 실제 피해자이고, 알파 게이트가 SELL 에는 적용되지 않아 세션 경계만
    격리되기 때문이다(`guards.py:116`).
    """
    from test_execution_command_owner import held
    f = await command_fixture(tmp_path, monkeypatch, ready=True, origin='automatic',
                              policy=nominal())
    await held(f, 100)
    f['clock'][0] = moment
    req = session_request(f, moment, **changes)
    await f['commands'].prepare(req, f['entry'](req))
    return f, req


def test_a_session_boundary_crossed_between_prepare_and_dispatch_ends_the_attempt(
        tmp_path, monkeypatch):
    """C1 — 15:19 에 구운 `regular` 요청을 15:21 에 보내면 `request_session_changed` 다.

    `_dispatch` 는 `session=False` 로 들어가지만 세션을 건너뛰지 않는다 — `_bound`→
    `_evaluate`→`_session`(`commands.py:364`)이 **dispatch 시점 시계**로 다시 대조한다.
    끝나는 모양은 계약 5 그대로: `final_rejected`/`not_sent`/그 `reason_code` 와 예약 4항 0.
    `reserved_planned_risk` 는 nominal 사이징이라 처음부터 None 이고 None 으로 남는다.
    """
    async def scenario():
        f, req = await prepared_at(tmp_path, monkeypatch, T1519)
        calls = spy_abandon(f, monkeypatch)
        try:
            attempt = f['runtime'].owner.state['attempts']['A']
            assert (attempt['state'], attempt['reserved_quantity']) == ('prepared', PROTECTED)
            assert attempt['reserved_planned_risk'] is None

            f['clock'][0] = T1521
            result = await send(f, req)
            assert (result.status, result.reason_code) == (
                CommandStatus.NOT_SENT, 'request_session_changed')
            assert CODE.fullmatch(result.reason_code)
            assert calls == [('A', 'request_session_changed', True)]
            current = f['runtime'].owner.state['attempts']['A']
            assert current['state'] == 'final_rejected'
            assert current['command_status'] == 'not_sent'
            assert current['claim_id'] is None
            assert current['reason_code'] == 'request_session_changed'
            assert reservations(current) == (0, D('0'), D('0'), None)
            assert pending_sectors(f) == {}
            assert f['broker']._session.posts == []
        finally: await f['store'].close()
    asyncio.run(scenario())


def test_the_producer_can_reprepare_the_same_intent_in_the_new_session(tmp_path, monkeypatch):
    """C2 — 재준비의 소유자는 **생산자**다(dispatch 안 재시도가 아니라).

    NOT_SENT 로 닫힌 시도는 `final_rejected`(=TERMINAL_STATES)라 같은 종목의 새 SUBMIT 을
    막지 않고, 새 세션에서 새로 구운 요청은 본문·fingerprint·증거 라벨이 **함께** 그 세션의
    것이 된다. 같은 intent 를 유지한 채 attempt 만 새로 발급하는 gateway 모양 그대로다.
    """
    async def scenario():
        f, req = await prepared_at(tmp_path, monkeypatch, T1519)
        try:
            f['clock'][0] = T1521
            assert (await send(f, req)).status is CommandStatus.NOT_SENT

            again = session_request(f, T1521, aid='A2', intent_id=req.intent_id)
            assert again.session.session == 'closing' and again.fingerprint != req.fingerprint
            reopened = await f['commands'].prepare(again, f['entry'](again))
            assert reopened['state'] == 'prepared'
            assert (await send(f, again)).status is CommandStatus.ACKNOWLEDGED
            assert len(f['broker']._session.posts) == 1
            assert f['runtime'].owner.state['attempts']['A2']['intent_id'] == req.intent_id
        finally: await f['store'].close()
    asyncio.run(scenario())


def test_a_session_label_cannot_be_forged_onto_a_different_moment():
    """C4 — DTO 생성 시점에 라벨과 시각이 어긋나면 `request_session_mismatch` 다.

    "세션만 갈아끼워 그대로 보낸다"는 완화가 요청 객체 단계에서 이미 불가능하다는 뜻이다.
    """
    from src.execution.safety.requests import RequestSession, RequestValidationError, _session_at

    assert _session_at(T1519) == 'regular' and _session_at(T1531) == 'break'
    with pytest.raises(RequestValidationError) as caught:
        RequestSession(T1531.date().isoformat(), T1531, 'regular')
    assert str(caught.value) == 'request_session_mismatch'
    # 같은 순간의 올바른 라벨은 통과한다(거부가 시각 자체 때문이 아니라는 대조).
    assert RequestSession(T1531.date().isoformat(), T1531, 'break').session == 'break'


def test_the_closing_and_next_market_sessions_bake_different_wire_bodies():
    """C6 — `closing` 은 시장가를 build 에서 거부하고 `next_market` 은 둘을 같은 본문으로 굽는다.

    15:40~20:00 에서는 지정가·시장가가 **같은 `ORD_DVSN='05'`·`AFHR_FLPR_YN='Y'`** 이고
    시장가만 `wire_price` 가 0 이다 — 즉 그 구간에서 "시장가 에스컬레이션"은 전선상 지정가와
    구분되지 않는다(①M8). `closing` 에서의 지정가 강등이냐 15:40 대기냐는 **P1 입력**이다.
    """
    from src.execution.safety.requests import (
        KISRequestBuilder, RequestSession, RequestValidationError, _session_at)
    from test_execution_requests import account

    builder = KISRequestBuilder(account())

    def build(moment, order_type):
        return builder.prepare_submit(
            Order(symbol='005930', side=OrderSide.SELL, quantity=10, price=D('10000'),
                  order_type=order_type, strategy='sepa_trend'),
            intent_id='I-S', attempt_id='S-' + order_type.value,
            session=RequestSession(moment.date().isoformat(), moment, _session_at(moment)),
            valuation_price=D('10000'))

    assert _session_at(T1521) == 'closing' and _session_at(T1541) == 'next_market'
    with pytest.raises(RequestValidationError) as caught:
        build(T1521, OrderType.MARKET)
    assert str(caught.value) == 'unsupported_submit_session'
    assert build(T1521, OrderType.LIMIT).body()['ORD_DVSN'] == '00'

    limit, market = build(T1541, OrderType.LIMIT), build(T1541, OrderType.MARKET)
    for request in (limit, market):
        assert request.body()['ORD_DVSN'] == '05'
        assert request.body()['AFHR_FLPR_YN'] == 'Y'
    assert limit.wire_price == D('10000') and market.wire_price == D('0')
    assert limit.body()['ORD_UNPR'] == '10000' and market.body()['ORD_UNPR'] == '0'
    assert limit.fingerprint != market.fingerprint
