"""attach 송신 경계(GuardedKISTransport.send_prepared)의 킬스위치·감사 원장 계약.

운영 경로 KISBroker.submit_order 가 하던 두 안전장치를 attach POST 도 타는지 고정한다.
플래그 디렉터리와 감사 원장 경로는 tmp_path 로 주입한다 — 운영 캐시는 건드리지 않는다.
"""
import asyncio
import inspect
import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from src.core.types import OrderSide
from src.execution.safety import transport as transport_mod
from src.execution.safety.guards import GuardDecision
from src.execution.safety.transport import GuardedKISTransport, TransportStatus
from src.risk import kill_switch
from src.utils import audit_log
from test_execution_requests import cancel, order, submit
from test_kr_final_dispatch import Response
from test_kr_prepared_dispatch import fixture


PERMIT = GuardDecision(True, 'synthetic-owner-permit')


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    """킬스위치 플래그·감사 원장을 임시 경로로 주입한다."""
    flags, audit = tmp_path / 'flags', tmp_path / 'audit'
    flags.mkdir()

    def rows():
        found = []
        for path in sorted(audit.glob('*.jsonl')) if audit.exists() else []:
            for line in path.read_text(encoding='utf-8').splitlines():
                if line.strip():
                    found.append(json.loads(line))
        return found

    monkeypatch.setattr(kill_switch, 'CACHE_DIR', flags)
    monkeypatch.setattr(audit_log, 'AUDIT_DIR', audit)
    kill_switch.clear_cache()
    yield SimpleNamespace(flags=flags, rows=rows,
                          raw=lambda: ''.join(path.read_text(encoding='utf-8')
                                              for path in sorted(audit.glob('*.jsonl'))) if audit.exists() else '')
    kill_switch.clear_cache()


def halt(ledger, name, reason='급락장 수동 개입'):
    (ledger.flags / name).write_text(reason, encoding='utf-8')
    kill_switch.clear_cache()


def dispatch(request, broker, builder, guard=None):
    return asyncio.run(GuardedKISTransport(broker, request_builder=builder).send_prepared(
        request, guard or (lambda actual: PERMIT)))


def test_submit_records_attempt_then_acceptance(ledger):
    broker, builder, _ = fixture()
    request = submit(builder)
    result = dispatch(request, broker, builder)

    assert result.status is TransportStatus.ACKNOWLEDGED
    assert len(broker._session.posts) == 1
    rows = ledger.rows()
    assert [row['event'] for row in rows] == [audit_log.EV_SUBMIT, audit_log.EV_ACCEPT]
    for row in rows:
        assert row['market'] == 'KR' and row['symbol'] == request.symbol
        assert row['side'] == request.side.value and row['qty'] == request.quantity
        # attach 경로임을 구분할 수 있고 owner 행과 이을 수 있어야 한다.
        assert row['path'] == 'attach' and row['fingerprint'] == request.fingerprint
    # 계좌번호·hashkey·토큰은 원장에 남기지 않는다.
    raw = ledger.raw()
    for secret in (request.account.account_no, request.account.account_product_cd,
                   'synthetic-hash', 'synthetic-token'):
        assert secret not in raw


def test_cancel_attempt_is_recorded_with_the_cancel_event(ledger):
    broker, builder, _ = fixture()
    request = cancel(builder)
    result = dispatch(request, broker, builder)

    assert result.status is TransportStatus.ACKNOWLEDGED
    rows = ledger.rows()
    assert [row['event'] for row in rows] == [audit_log.EV_CANCEL, audit_log.EV_ACCEPT]
    assert rows[0]['fingerprint'] == request.fingerprint


@pytest.mark.parametrize('flag', ['KILL_SWITCH', 'KILL_SWITCH_KR'])
def test_buy_halt_blocks_buy_without_posting(ledger, flag):
    broker, builder, _ = fixture()
    halt(ledger, flag)
    request = submit(builder)
    result = dispatch(request, broker, builder)

    assert result.status is TransportStatus.NOT_SENT
    assert result.reason == 'kill_switch_blocked'
    # 상태 사유에 차단 원문을 넣지 않는다 — 원문은 감사 원장에만.
    assert '급락장' not in result.reason
    assert broker._session.posts == []
    rows = ledger.rows()
    assert [row['event'] for row in rows] == [audit_log.EV_BLOCKED]
    assert '급락장 수동 개입' in rows[0]['reason']
    assert rows[0]['path'] == 'attach' and rows[0]['fingerprint'] == request.fingerprint


def test_buy_halt_still_lets_sell_and_cancel_through(ledger):
    halt(ledger, 'KILL_SWITCH')
    broker, builder, _ = fixture()
    sell = submit(builder, order=order(side=OrderSide.SELL))
    assert dispatch(sell, broker, builder).status is TransportStatus.ACKNOWLEDGED
    assert len(broker._session.posts) == 1

    broker, builder, _ = fixture()
    assert dispatch(cancel(builder), broker, builder).status is TransportStatus.ACKNOWLEDGED
    assert len(broker._session.posts) == 1


@pytest.mark.parametrize('side', [OrderSide.BUY, OrderSide.SELL])
def test_full_halt_blocks_both_sides(ledger, side):
    broker, builder, _ = fixture()
    halt(ledger, 'KILL_SWITCH_ALL')
    result = dispatch(submit(builder, order=order(side=side)), broker, builder)

    assert result.status is TransportStatus.NOT_SENT
    assert result.reason == 'kill_switch_blocked'
    assert broker._session.posts == []


def test_full_halt_does_not_block_cancel(ledger):
    """취소는 위험을 줄이는 명령이다 — 현행 cancel_order 와 같은 이유로 검사하지 않는다."""
    broker, builder, _ = fixture()
    halt(ledger, 'KILL_SWITCH_ALL')
    result = dispatch(cancel(builder), broker, builder)

    assert result.status is TransportStatus.ACKNOWLEDGED
    assert len(broker._session.posts) == 1
    assert [row['event'] for row in ledger.rows()] == [audit_log.EV_CANCEL, audit_log.EV_ACCEPT]


@pytest.mark.parametrize('target', ['check', 'record'])
def test_pre_post_safety_failure_never_sends(ledger, monkeypatch, target):
    """ⓐ 킬스위치 검사·EV_SUBMIT 기록은 송신 앞이다 — 거기서 터지면 송신 0."""
    broker, builder, _ = fixture()
    def boom(*args, **kwargs):
        raise RuntimeError('synthetic-safety-failure')
    monkeypatch.setattr(kill_switch if target == 'check' else audit_log, target, boom)
    result = dispatch(submit(builder), broker, builder)

    assert result.status is TransportStatus.NOT_SENT
    assert result.reason == 'preparation_failed'
    assert broker._session.posts == []


def test_recording_failure_after_the_response_keeps_the_ack(ledger, monkeypatch):
    """ⓑ 응답 뒤 기록 실패가 ACK를 UNKNOWN으로 바꾸지 않는다."""
    broker, builder, _ = fixture()
    original = audit_log.record

    def record(event, **fields):
        if event != audit_log.EV_SUBMIT:
            raise RuntimeError('synthetic-ledger-failure')
        original(event, **fields)

    monkeypatch.setattr(audit_log, 'record', record)
    result = dispatch(submit(builder), broker, builder)

    assert result.status is TransportStatus.ACKNOWLEDGED
    assert result.reason == 'command_acknowledged'
    assert [row['event'] for row in ledger.rows()] == [audit_log.EV_SUBMIT]


def test_no_application_await_between_guard_and_post():
    """ⓒ guard와 POST 사이에 새 application await를 두지 않는다."""
    assert not asyncio.iscoroutinefunction(kill_switch.check)
    assert not asyncio.iscoroutinefunction(audit_log.record)
    assert not asyncio.iscoroutinefunction(audit_log.record_blocked)
    body = inspect.getsource(transport_mod.GuardedKISTransport.send_prepared)
    between = body.split('decision = guard(prepared)', 1)[1].split('broker._session.post(', 1)[0]
    code = [line.split('#', 1)[0] for line in between.splitlines()]
    assert not any('await' in line for line in code)


def test_unconfirmed_response_is_recorded_as_a_rejection_marked_unconfirmed(ledger):
    broker, builder, _ = fixture(Response(status=500))
    result = dispatch(submit(builder), broker, builder)

    assert result.status is TransportStatus.UNKNOWN
    rows = ledger.rows()
    assert [row['event'] for row in rows] == [audit_log.EV_SUBMIT, audit_log.EV_REJECT]
    assert rows[1]['unconfirmed'] is True
    assert rows[1]['reason'] == 'http_response_unconfirmed'


def test_broker_rejection_is_recorded_as_a_confirmed_rejection(ledger):
    broker, builder, _ = fixture(Response(data={'rt_cd': '1', 'msg_cd': 'APBK0919'}))
    result = dispatch(submit(builder), broker, builder)

    assert result.status is TransportStatus.REJECTED
    rows = ledger.rows()
    assert [row['event'] for row in rows] == [audit_log.EV_SUBMIT, audit_log.EV_REJECT]
    assert 'unconfirmed' not in rows[1]


def test_exception_after_dispatch_keeps_the_submit_row(ledger):
    """송신 뒤 예외에도 시도 기록은 남고 결과는 미확인으로 남는다."""
    broker, builder, _ = fixture(Response(error=RuntimeError('synthetic-post-failure')))
    result = dispatch(submit(builder), broker, builder)

    assert result.status is TransportStatus.UNKNOWN
    assert result.reason == 'dispatch_unconfirmed'
    rows = ledger.rows()
    assert [row['event'] for row in rows] == [audit_log.EV_SUBMIT, audit_log.EV_REJECT]
    assert rows[1]['unconfirmed'] is True


def test_guard_rejection_is_not_recorded_as_an_attempt(ledger):
    """guard가 막으면 송신 시도 자체가 없었던 것이다 — 원장에 시도 행을 만들지 않는다."""
    broker, builder, _ = fixture()
    result = dispatch(submit(builder), broker, builder,
                      guard=lambda actual: GuardDecision(False, 'synthetic-guard-block'))

    assert result.status is TransportStatus.NOT_SENT
    assert result.reason == 'synthetic-guard-block'
    assert ledger.rows() == []


def test_transport_price_and_quantity_reach_the_ledger(ledger):
    broker, builder, _ = fixture()
    request = submit(builder, order=order(quantity=7, price=Decimal('10025')))
    dispatch(request, broker, builder)

    row = ledger.rows()[0]
    assert row['qty'] == 7 and row['price'] == format(request.wire_price, 'f')
