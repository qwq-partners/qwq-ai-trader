"""Health retry snapshot은 producer RAM의 mutable leaf를 노출하지 않는다."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path

import pytest

from src.execution.safety.economics import encode_portfolio
from src.execution.safety.protection import encode_protection
from src.execution.safety.protection_producer import ProtectionProducer
from test_execution_signal_gateway_acceptance import NOW_KST, _CLOCK, fixture, seed_position


@pytest.fixture(autouse=True)
def synthetic_home(tmp_path, monkeypatch):
    """재사용 fixture의 모듈 범위 밖에서도 HOME·시계를 합성 값으로 고정한다."""
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    _CLOCK['kst'] = NOW_KST
    yield
    _CLOCK['kst'] = NOW_KST


def _runtime_snapshot(f, producer):
    """health 읽기가 owner·경제·보호·cooldown 상태를 writer 없이 보존하는지 대조한다."""
    runtime = f['runtime']
    return (
        runtime.owner.version,
        runtime.owner.published_version,
        f['engine']._execution_version,
        deepcopy(runtime.owner.state),
        encode_portfolio(f['engine'].portfolio),
        encode_protection(f['exits']),
        deepcopy(producer._restart_retries),
    )


def test_runtime_health_restart_retries_snapshot_copies_intent_id_leaves(tmp_path, monkeypatch):
    """`intent_ids` list 복사를 빼면 nested health 수정이 producer cooldown RAM을 바꾼다."""
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            await seed_position(f)
            runtime = f['runtime']
            producer = ProtectionProducer(runtime, clock=lambda: NOW_KST,
                                          indicator_source=lambda _symbol: {})
            runtime._protection_producer = producer

            # 빈 retry와 두 _restart_cooldowns 생성 경로의 실제 row shape를 명시한다.
            assert runtime.health()['protection_producer']['restart_retries'] == {}
            producer._restart_retries = {
                '005930': {
                    'reason': 'restart_episode_order_ambiguous',
                    'intent_ids': ['pp-i-first', 'pp-i-second', 'pp-i-first'],
                    'started_at': None,
                },
                '000660': {
                    'reason': 'restart_reemission_cooldown',
                    'intent_ids': ['pp-i-cooldown'],
                    'started_at': NOW_KST,
                },
            }
            expected = {
                '005930': {
                    'reason': 'restart_episode_order_ambiguous',
                    'intent_ids': ['pp-i-first', 'pp-i-second', 'pp-i-first'],
                    'started_at': None,
                },
                '000660': {
                    'reason': 'restart_reemission_cooldown',
                    'intent_ids': ['pp-i-cooldown'],
                    'started_at': '2026-09-18T11:00:00+09:00',
                },
            }
            before = _runtime_snapshot(f, producer)
            writer_calls = []

            def forbidden(*_args, **_kwargs):
                writer_calls.append('writer')
                raise AssertionError('read-only health reached a writer boundary')

            with monkeypatch.context() as guards:
                for obj, names in (
                    (runtime.owner, ('mutate',)),
                    (f['store'], ('commit',)),
                    (f['broker'], ('_api_get', '_api_post', 'submit_order')),
                ):
                    for name in names:
                        guards.setattr(obj, name, forbidden)

                direct = producer.health()['restart_retries']
                first = runtime.health()['protection_producer']['restart_retries']
                second = runtime.health()['protection_producer']['restart_retries']
                assert direct == first == second == expected
                assert first['005930']['intent_ids'] is not producer._restart_retries['005930']['intent_ids']
                assert first['000660']['intent_ids'] is not producer._restart_retries['000660']['intent_ids']
                assert first['005930']['intent_ids'] is not second['005930']['intent_ids']

                first['005930']['intent_ids'].append('returned-only')
                first['000660']['intent_ids'].clear()
                assert producer._restart_retries['005930']['intent_ids'] == [
                    'pp-i-first', 'pp-i-second', 'pp-i-first']
                assert producer._restart_retries['000660']['intent_ids'] == ['pp-i-cooldown']
                assert runtime.health()['protection_producer']['restart_retries'] == expected

                producer._restart_retries['005930']['intent_ids'][0] = 'internal-only'
                assert first['005930']['intent_ids'] == [
                    'pp-i-first', 'pp-i-second', 'pp-i-first', 'returned-only']
                assert runtime.health()['protection_producer']['restart_retries']['005930']['intent_ids'] == [
                    'internal-only', 'pp-i-second', 'pp-i-first']
                assert writer_calls == []

            assert _runtime_snapshot(f, producer) == (
                before[:-1] + (deepcopy(producer._restart_retries),))
            assert writer_calls == []
            assert f['posts']() == []
        finally:
            await f['teardown']()
    asyncio.run(scenario())
