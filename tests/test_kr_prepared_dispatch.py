"""실제 builder/config/header와 단회 POST 경계. owner의 송신권 인수는 별도다."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from src.execution.broker.kis_kr import KISBroker, KISConfig
from src.execution.safety.guards import GuardDecision
from src.execution.safety.lifecycle import CommandKind
from src.execution.safety.requests import KISRequestBuilder
from src.execution.safety.transport import GuardedKISTransport, TransportStatus
from test_execution_requests import account, submit, cancel
from test_kr_final_dispatch import Response, Session


def fixture(response=None):
    identity = account()
    builder = KISRequestBuilder(identity)
    broker = object.__new__(KISBroker)
    broker.config = KISConfig(app_key='synthetic-key', app_secret='synthetic-secret',
                             account_no=identity.account_no,
                             account_product_cd=identity.account_product_cd,
                             env=identity.environment, base_url=identity.endpoint)
    broker._session = Session(response or Response(data={'rt_cd': '0'}))
    broker._token = 'synthetic-token'
    broker._token_mgr = SimpleNamespace(_access_token=None)
    calls = []
    async def connect():
        calls.append('connect')
        broker._session.closed = False
        return True
    async def token():
        calls.append('token')
        broker._token = 'synthetic-token'
        return True
    async def hashkey(body):
        calls.append(('hash', deepcopy(body)))
        return 'synthetic-hash'
    async def limiter(tr):
        calls.append(('limiter', tr))
    broker.connect, broker._ensure_token = connect, token
    broker._get_hashkey, broker._rate_limit = hashkey, limiter
    return broker, builder, calls


@pytest.mark.parametrize('command', ['submit', 'cancel'])
def test_prepared_body_reaches_actual_broker_headers_once(command):
    async def scenario():
        broker, builder, calls = fixture()
        request = submit(builder) if command == 'submit' else cancel(builder)
        inspected = []
        def guard(actual):
            inspected.append(actual)
            builder.validate(actual)
            return GuardDecision(True, 'synthetic-owner-permit')
        result = await GuardedKISTransport(broker, request_builder=builder).send_prepared(request, guard)
        assert result.status is TransportStatus.ACKNOWLEDGED
        assert len(inspected) == len(broker._session.posts) == 1
        assert inspected[0] == request and inspected[0] is not request
        url, kwargs = broker._session.posts[0]
        assert url == request.account.endpoint + request.path
        assert kwargs['json'] == request.body() == calls[0][1]
        assert kwargs['headers']['tr_id'] == request.tr_id
        assert kwargs['headers']['hashkey'] == 'synthetic-hash'
    asyncio.run(scenario())


@pytest.mark.parametrize('boundary', ['connect', 'token', 'hashkey', 'limiter'])
@pytest.mark.parametrize('changed', ['account_no', 'account_product_cd', 'env', 'base_url'])
def test_config_change_during_each_await_never_posts(boundary, changed):
    async def scenario():
        broker, builder, calls = fixture()
        request = submit(builder)
        reached, release = asyncio.Event(), asyncio.Event()
        async def wait(*args):
            reached.set()
            await release.wait()
            broker._session.closed = False
            return 'synthetic-hash' if boundary == 'hashkey' else True
        if boundary == 'connect':
            broker._session.closed = True
            broker.connect = wait
        elif boundary == 'token':
            broker._token = None
            broker._ensure_token = wait
        else:
            setattr(broker, '_get_hashkey' if boundary == 'hashkey' else '_rate_limit', wait)
        task = asyncio.create_task(GuardedKISTransport(broker, request_builder=builder).send_prepared(
            request, lambda actual: GuardDecision(True, 'synthetic-owner-permit')))
        await asyncio.wait_for(reached.wait(), 2)
        setattr(broker.config, changed, 'CHANGED')
        release.set()
        result = await task
        assert result.status is TransportStatus.NOT_SENT
        assert result.reason == 'request_broker_scope_changed'
        assert broker._session.posts == []
        if boundary in ('connect', 'token'):
            assert not any(isinstance(call, tuple) and call[0] == 'hash' for call in calls)
    asyncio.run(scenario())


def test_hash_helper_payload_mutation_is_not_signed_as_a_different_request():
    async def scenario():
        broker, builder, _ = fixture()
        async def hashkey(body):
            body['ORD_QTY'] = '99'
            return 'hash-for-wrong-body'
        broker._get_hashkey = hashkey
        result = await GuardedKISTransport(broker, request_builder=builder).send_prepared(
            submit(builder), lambda actual: GuardDecision(True, 'synthetic-owner-permit'))
        assert result.status is TransportStatus.NOT_SENT
        assert result.reason == 'hashkey_payload_changed'
        assert not broker._session.posts
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['header', 'guard_body', 'guard_config', 'modify', 'missing_builder'])
def test_meaning_changes_and_unsupported_routes_do_not_post(change):
    async def scenario():
        broker, builder, _ = fixture()
        request = submit(builder)
        if change == 'header':
            broker._get_headers = lambda tr: {'tr_id': 'TTTC0803U'}
        if change == 'modify':
            request = replace(request, command=CommandKind.MODIFY)
        def guard(actual):
            if change == 'guard_body':
                object.__setattr__(actual, 'quantity', 99)
            if change == 'guard_config':
                broker.config.account_no = 'CHANGED'
            return GuardDecision(True, 'synthetic-owner-permit')
        transport = GuardedKISTransport(broker, request_builder=None if change == 'missing_builder' else builder)
        result = await transport.send_prepared(request, guard)
        assert result.status is TransportStatus.NOT_SENT
        assert not broker._session.posts
    asyncio.run(scenario())


def test_caller_mutation_during_hash_does_not_change_captured_request():
    async def scenario():
        broker, builder, _ = fixture()
        request, expected = submit(builder), submit(builder).body()
        async def hashkey(body):
            object.__setattr__(request, 'quantity', 99)
            object.__setattr__(request, 'fingerprint', 'bad-caller-digest')
            return 'synthetic-hash'
        broker._get_hashkey = hashkey
        result = await GuardedKISTransport(broker, request_builder=builder).send_prepared(
            request, lambda actual: GuardDecision(True, 'synthetic-owner-permit'))
        assert result.status is TransportStatus.ACKNOWLEDGED
        assert broker._session.posts[0][1]['json'] == expected
    asyncio.run(scenario())


@pytest.mark.parametrize('status,data,error', [
    (401, {'rt_cd': '1', 'msg_cd': 'EXPIRED'}, None),
    (500, {}, None), (200, None, ValueError('malformed')),
    (200, None, asyncio.TimeoutError()), (200, [], None),
])
def test_prepared_post_failure_is_unknown_and_not_retried(status, data, error):
    broker, builder, _ = fixture(Response(status, data, error))
    result = asyncio.run(GuardedKISTransport(broker, request_builder=builder).send_prepared(
        submit(builder), lambda actual: GuardDecision(True, 'synthetic-owner-permit')))
    assert result.status is TransportStatus.UNKNOWN
    assert len(broker._session.posts) == 1


def test_cancelled_prepared_post_preserves_cancellation_without_retransmit():
    broker, builder, _ = fixture(Response(error=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(GuardedKISTransport(broker, request_builder=builder).send_prepared(
            submit(builder), lambda actual: GuardDecision(True, 'synthetic-owner-permit')))
    assert len(broker._session.posts) == 1


@pytest.mark.parametrize('boundary', ['connect', 'token', 'hashkey', 'limiter'])
def test_final_guard_observes_changed_permission_after_each_await(boundary):
    async def scenario():
        broker, builder, _ = fixture()
        permission = [True]
        async def change(*args):
            await asyncio.sleep(0)
            permission[0] = False
            broker._session.closed = False
            return 'synthetic-hash' if boundary == 'hashkey' else True
        if boundary == 'connect':
            broker._session.closed = True
            broker.connect = change
        elif boundary == 'token':
            broker._token = None
            broker._ensure_token = change
        else:
            setattr(broker, '_get_hashkey' if boundary == 'hashkey' else '_rate_limit', change)
        result = await GuardedKISTransport(broker, request_builder=builder).send_prepared(
            submit(builder), lambda actual: GuardDecision(permission[0], 'changed-owner-state'))
        assert result.status is TransportStatus.NOT_SENT
        assert result.reason == 'changed-owner-state'
        assert broker._session.posts == []
    asyncio.run(scenario())


def test_guard_to_post_does_not_yield_and_normal_token_rotation_is_allowed():
    async def scenario():
        broker, builder, _ = fixture()
        marker = []
        broker._token_mgr._access_token = 'synthetic-rotated-token'
        original_post = broker._session.post
        def post(*args, **kwargs):
            assert marker == ['guard']
            assert kwargs['headers']['authorization'] == 'Bearer synthetic-rotated-token'
            marker.append('post')
            return original_post(*args, **kwargs)
        broker._session.post = post
        def guard(actual):
            marker.append('guard')
            asyncio.get_running_loop().call_soon(marker.append, 'yielded')
            return GuardDecision(True, 'synthetic-owner-permit')
        result = await GuardedKISTransport(broker, request_builder=builder).send_prepared(submit(builder), guard)
        assert result.status is TransportStatus.ACKNOWLEDGED
        assert marker[:2] == ['guard', 'post']
    asyncio.run(scenario())


@pytest.mark.parametrize('allowed', ['false', 1, {'allowed': False}, [False], object(), 0, None])
def test_guard_requires_exact_boolean_allowed(allowed):
    broker, builder, _ = fixture()
    result = asyncio.run(GuardedKISTransport(broker, request_builder=builder).send_prepared(
        submit(builder), lambda actual: GuardDecision(allowed, 'malformed-result')))
    assert result.status is TransportStatus.NOT_SENT
    assert result.reason == 'invalid_guard_result'
    assert broker._session.posts == []


@pytest.mark.parametrize('reason', [None, 1, {'why': 'deny'}])
def test_guard_requires_text_reason(reason):
    broker, builder, _ = fixture()
    result = asyncio.run(GuardedKISTransport(broker, request_builder=builder).send_prepared(
        submit(builder), lambda actual: GuardDecision(True, reason)))
    assert result.status is TransportStatus.NOT_SENT
    assert result.reason == 'invalid_guard_result'
    assert broker._session.posts == []
