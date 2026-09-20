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
    JUSTIFIED, apply_change, make_facts, nominal, publish,
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


# ── 결정 ⑧: 자식 명령은 S4 로 이월한 현행을 고정한다 ─────────────────────

def test_an_unclaimed_cancel_is_left_for_s4_and_never_abandoned(tmp_path, monkeypatch):
    """자식 행은 부모 order_ref 를 싣고 있어 구조적으로 abandon 될 수 없다 — 부르지도 않는다."""
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
            assert calls == []
            current = f['runtime'].owner.state['attempts']['C']
            assert (current['state'], current['claim_id']) == ('prepared', None)
            assert reservations(current) == reservations(before)
            assert reservations(f['runtime'].owner.state['attempts']['A']) == reservations(parent_before)
            assert len(f['broker']._session.posts) == posted
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
