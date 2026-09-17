"""Regression for independent review: policy deletion/retyping cannot heal degraded protection."""
import asyncio
from copy import deepcopy
from decimal import Decimal

import pytest

from test_execution_intraday_replay import degraded, refresh, reseal, invariant_roots


@pytest.mark.parametrize('damage', ['erase_bridge', 'retype_quote'])
@pytest.mark.parametrize('cycle', [False, True])
@pytest.mark.parametrize('historical_stop', [False, True])
def test_original_accepted_policy_must_survive_in_replay_history(
        tmp_path, monkeypatch, damage, cycle, historical_stop):
    async def scenario():
        engine, _, store, runtime, writer, _ = await degraded(tmp_path, monkeypatch)
        try:
            await refresh(writer, monkeypatch, -3)
            if historical_stop:
                await runtime.quote('005930', Decimal('9700'))
            if cycle:
                await refresh(writer, monkeypatch, 0)
            original = runtime.owner.state

            def corrupt(state):
                history = state['protection_replay']['005930']
                previous = None
                events = []
                for event in history['events']:
                    if event['kind'] == 'intraday_policy':
                        if damage == 'erase_bridge':
                            # Give the preceding compact quote a full equivalent
                            # before scope, then hide the policy change in after.
                            if 'scope_digest' in previous:
                                previous.pop('scope_digest')
                                previous['before'] = deepcopy(event['before'])
                            previous['after'] = deepcopy(event['after'])
                            continue
                        event.pop('policy_input')
                        event.update(kind='quote', price='10000', market_data=None,
                            intent_id=None, command_id='synthetic-retyped-policy',
                            decision=None, provenance=None)
                    events.append(event)
                    previous = event
                history['events'] = events
                reseal(state)
                return state

            await runtime.owner.mutate('synthetic-policy-erasure', corrupt)
            await runtime.restore()
            before = runtime.owner.state
            for key in original.keys() - {'protection_replay'}:
                assert before[key] == original[key], key
            receipt = await runtime.repair_protection('erasure-repair', '005930',
                expected_version=runtime.owner.version)
            assert receipt.status == 'BLOCKED', receipt.reason
            assert runtime.owner.state['protection'] == before['protection']
            invariant_roots(before, runtime.owner.state)
            assert not engine._event_queue and not runtime.trading_ready
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())
