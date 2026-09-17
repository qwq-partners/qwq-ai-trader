"""Loopback selection input never supplies a quote or a trading capability."""
import asyncio
import json

import pytest


class Response:
    def __init__(self, body, *, status=200, headers=None):
        self.body, self.status = body, status
        self.headers = headers or {}
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def iter_chunked(self, size):
        for offset in range(0, len(self.body), size):
            yield self.body[offset:offset + size]


class Session:
    _retry_connection = True

    def __init__(self, response):
        self.response, self.calls, self.closed = response, [], False

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response

    async def close(self):
        self.closed = True


def test_holdings_are_sorted_deduplicated_and_quotes_are_discarded():
    from src.observation.toss_positions import PositionsClient
    session = Session(Response(json.dumps([
        {'symbol': '087010', 'quantity': 2, 'current_price': 123},
        {'symbol': '005930', 'quantity': 1}, {'symbol': '087010', 'quantity': 3},
    ]).encode()))

    async def run():
        client = PositionsClient(session_factory=lambda: session)
        assert await client.fetch() == ('005930', '087010')
        await client.close()

    asyncio.run(run())
    assert session.closed and not session._retry_connection
    assert session.calls[0][0] == 'http://127.0.0.1:8080/api/positions'
    assert session.calls[0][1]['allow_redirects'] is False
    assert len(session.calls) == 1


@pytest.mark.parametrize('body', [
    b'{}', b'null', b'[{"symbol":"087010"}]',
    b'[{"symbol":"087010","quantity":null}]',
    b'[{"symbol":"087010","quantity":true}]',
    b'[{"symbol":"087010","quantity":0}]',
    b'[{"symbol":"087010","quantity":-1}]',
    b'[{"symbol":"087010","quantity":"1"}]',
    b'[{"symbol":"US:AAPL","quantity":1}]',
    b'[{"symbol":"087010","quantity":NaN}]',
    b'[{"symbol":"087010","quantity":1,"quantity":2}]',
    json.dumps([{'symbol': '087010', 'quantity': 1}] * 65).encode(),
    b'[' * 9 + b']' * 9, b'[' + b' ' * 65536 + b']',
], ids=[f'bad-{i}' for i in range(14)])
def test_bad_input_never_becomes_an_empty_success(body):
    from src.observation.toss_positions import InputUnavailable, PositionsClient
    session = Session(Response(body))
    with pytest.raises(InputUnavailable, match='input_unavailable'):
        asyncio.run(PositionsClient(session_factory=lambda: session).fetch())
    assert len(session.calls) == 1


@pytest.mark.parametrize('status,headers', [(302, {}), (500, {}), (200, {'Content-Encoding': 'gzip'})])
def test_redirect_errors_compression_are_not_followed(status, headers):
    from src.observation.toss_positions import InputUnavailable, PositionsClient
    session = Session(Response(b'[]', status=status, headers=headers))
    with pytest.raises(InputUnavailable):
        asyncio.run(PositionsClient(session_factory=lambda: session).fetch())
    assert len(session.calls) == 1


def test_only_valid_empty_array_is_empty_selection():
    from src.observation.toss_positions import PositionsClient
    assert asyncio.run(PositionsClient(session_factory=lambda: Session(Response(b'[]'))).fetch()) == ()


def test_all_valid_holdings_pass_through_for_explicit_overflow():
    from src.observation.toss_positions import PositionsClient
    body = json.dumps([{'symbol': f'{i:06}', 'quantity': 1} for i in range(25)]).encode()
    assert len(asyncio.run(PositionsClient(session_factory=lambda: Session(Response(body))).fetch())) == 25
