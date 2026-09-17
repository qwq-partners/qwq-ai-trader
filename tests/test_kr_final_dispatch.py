"""인증/limiter 대기 뒤 마지막 검사와 거래 POST 1회 경계."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from src.execution.safety.guards import GuardDecision
from src.execution.safety.transport import GuardedKISTransport, TransportStatus


class Response:
    def __init__(self, status=200, data=None, error=None):
        self.status, self.data, self.error = status, data, error

    async def __aenter__(self):
        if isinstance(self.error, asyncio.CancelledError):
            raise self.error
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self):
        if self.error:
            raise self.error
        return self.data


class Session:
    closed = False

    def __init__(self, response):
        self.response, self.posts = response, []

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return self.response


def broker(response=None):
    session = Session(response or Response(data={'rt_cd': '0', 'output': {'ODNO': '123'}}))

    async def ok(*args):
        return True

    async def hashkey(payload):
        return 'fake-hash'

    return SimpleNamespace(_session=session, _token='fake', connect=ok,
        _ensure_token=ok, _get_hashkey=hashkey, _rate_limit=ok,
        _get_headers=lambda tr: {'tr_id': tr},
        config=SimpleNamespace(base_url='https://offline.invalid'))


def run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize('await_boundary', ['connect', 'token', 'hashkey', 'limiter'])
def test_market_changes_during_each_await_prevent_http(await_boundary):
    async def exercise():
        b = broker()
        reached, release = asyncio.Event(), asyncio.Event()
        allowed = [True]

        async def hold(*args):
            reached.set()
            await release.wait()
            return 'fake-hash' if await_boundary == 'hashkey' else True

        if await_boundary == 'connect':
            b._session.closed = True
            async def connect():
                await hold()
                b._session.closed = False
                return True
            b.connect = connect
        elif await_boundary == 'token':
            b._token = None
            b._ensure_token = hold
        elif await_boundary == 'hashkey':
            b._get_hashkey = hold
        else:
            b._rate_limit = hold
        transport = GuardedKISTransport(b)
        task = asyncio.create_task(transport.send('submit', 'TTTC0802U', {'PDNO': '005930'},
            lambda: GuardDecision(allowed[0], 'intraday_severe')))
        await reached.wait()
        allowed[0] = False
        release.set()
        result = await task
        assert result.status == TransportStatus.NOT_SENT
        assert result.reason == 'intraday_severe'
        assert b._session.posts == []
    run(exercise())


@pytest.mark.parametrize('status,data,error', [
    (401, {'rt_cd': '1', 'msg_cd': 'EGW00123'}, None),
    (500, {'rt_cd': '1', 'msg_cd': 'EGW00123'}, None),
    (200, None, ValueError('bad JSON')),
    (200, None, asyncio.TimeoutError()),
    (200, ['not an object'], None),
    (429, {}, None),
])
def test_post_never_repeats_after_auth_network_or_malformed_reply(status, data, error):
    b = broker(Response(status, data, error))
    result = run(GuardedKISTransport(b).send('submit', 'TTTC0802U', {},
                                          lambda: GuardDecision(True, 'allowed')))
    assert result.status == TransportStatus.UNKNOWN
    assert len(b._session.posts) == 1


@pytest.mark.parametrize('command,tr,path', [
    ('submit', 'TTTC0802U', '/order-cash'),
    ('cancel', 'TTTC0803U', '/order-rvsecncl'),
    ('modify', 'TTTC0803U', '/order-rvsecncl'),
])
def test_explicit_success_at_transport_is_only_ack(command,tr,path):
    b = broker()
    result = run(GuardedKISTransport(b).send(command, tr, {'PDNO':'005930'},
                                          lambda: GuardDecision(True, 'allowed')))
    assert result.status == TransportStatus.ACKNOWLEDGED
    assert len(b._session.posts) == 1
    assert b._session.posts[0][0].endswith(path)
    assert b._session.posts[0][1]['headers']['hashkey'] == 'fake-hash'


def test_explicit_reject_is_distinct_from_unknown_and_guard_failure():
    b = broker(Response(data={'rt_cd':'1', 'msg_cd':'ORDER_REJECTED'}))
    result = run(GuardedKISTransport(b).send('submit','TTTC0802U',{},
                                         lambda: GuardDecision(True,'allowed')))
    assert result.status == TransportStatus.REJECTED


def test_unclassified_request_cannot_reach_http_and_no_secrets_in_reason():
    b = broker()
    result = run(GuardedKISTransport(b).send('invented','UNKNOWN',{},
                                         lambda: GuardDecision(True,'allowed')))
    assert result.status == TransportStatus.NOT_SENT
    assert b._session.posts == []


def test_cancellation_propagates_but_never_repeats_trade_post():
    b = broker(Response(error=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        run(GuardedKISTransport(b).send('submit', 'TTTC0802U', {},
                                      lambda: GuardDecision(True,'allowed')))
    assert len(b._session.posts) == 1


@pytest.mark.parametrize('await_boundary', ['connect', 'token', 'hashkey', 'limiter'])
def test_payload_is_frozen_before_preparation_awaits(await_boundary):
    async def exercise():
        b = broker()
        reached, release = asyncio.Event(), asyncio.Event()
        payload = {'PDNO': '005930', 'ORD_QTY': '10', 'metadata': {'lots': [10]}}
        hashed = []
        async def hold():
            reached.set()
            await release.wait()
        async def hashkey(prepared):
            if await_boundary == 'hashkey':
                await hold()
            hashed.append(deepcopy(prepared))
            return 'fake-hash'
        b._get_hashkey = hashkey
        if await_boundary == 'connect':
            b._session.closed = True
            async def connect():
                await hold()
                b._session.closed = False
                return True
            b.connect = connect
        elif await_boundary == 'token':
            b._token = None
            async def token():
                await hold()
                return True
            b._ensure_token = token
        elif await_boundary == 'limiter':
            async def rate_limit(tr):
                await hold()
            b._rate_limit = rate_limit
        task = asyncio.create_task(GuardedKISTransport(b).send('submit', 'TTTC0802U', payload,
            lambda: GuardDecision(True, 'allowed')))
        await reached.wait()
        payload['ORD_QTY'] = '100'
        payload['metadata']['lots'].append(90)
        release.set()
        result = await task
        assert result.status == TransportStatus.ACKNOWLEDGED
        expected = {'PDNO': '005930', 'ORD_QTY': '10', 'metadata': {'lots': [10]}}
        assert hashed == [expected]
        assert b._session.posts[0][1]['json'] == expected
        payload['PDNO'] = '000000'
        assert b._session.posts[0][1]['json'] == expected
    run(exercise())


@pytest.mark.parametrize('payload', [
    {'ORD_QTY': float('nan')}, {'ORD_QTY': float('inf')},
    {'nested': {'value': float('-inf')}}, {'value': object()},
    {'value': (1, 2)}, {1: 'not a string key'}, ['not a payload object'],
])
def test_invalid_payload_is_rejected_before_any_preparation_io(payload):
    b = broker()
    preparation = []
    async def hashkey(params):
        preparation.append('hashkey')
        return 'fake-hash'
    b._get_hashkey = hashkey
    result = run(GuardedKISTransport(b).send('submit', 'TTTC0802U', payload,
                                          lambda: GuardDecision(True, 'allowed')))
    assert result.status == TransportStatus.NOT_SENT
    assert preparation == []
    assert b._session.posts == []


def test_headers_are_copied_without_mutating_broker_defaults():
    b = broker()
    shared = {'tr_id': 'TTTC0802U', 'authorization': 'fake-token'}
    b._get_headers = lambda tr: shared
    result = run(GuardedKISTransport(b).send('submit', 'TTTC0802U', {'ORD_QTY': '10'},
                                          lambda: GuardDecision(True, 'allowed')))
    assert result.status == TransportStatus.ACKNOWLEDGED
    assert shared == {'tr_id': 'TTTC0802U', 'authorization': 'fake-token'}
    shared['authorization'] = 'changed'
    assert b._session.posts[0][1]['headers'] == {
        'tr_id': 'TTTC0802U', 'authorization': 'fake-token', 'hashkey': 'fake-hash',
    }
