"""기존 수동/전략 미지정의 빈 문자열을 보호 DTO에서 소실하지 않는다."""
import asyncio
from decimal import Decimal

import pytest

from src.core.types import Position
from src.execution.safety.protection import encode_protection, decode_protection
from src.strategies.exit_manager import ExitManager
from test_execution_runtime import NOW, setup, opened, observed, queued


@pytest.mark.parametrize('strategy', [None, '', 'sepa_trend'])
def test_actual_manager_optional_strategy_round_trip(strategy):
    exits = ExitManager(persist=False, clock=lambda: NOW)
    exits.register_position(Position('005930', quantity=1, avg_price=Decimal('10000'),
                                     strategy=strategy, entry_time=NOW))
    dto = encode_protection(exits)
    restored = decode_protection(dto, clock=lambda: NOW)
    assert restored.get_state('005930').strategy_name == exits.get_state('005930').strategy_name
    assert encode_protection(restored) == dto


@pytest.mark.parametrize('explicit_registration', [False, True])
def test_blank_strategy_real_fill_queue_does_not_degrade_protection(tmp_path, explicit_registration):
    async def scenario():
        engine, exits, store, runtime = await setup(tmp_path)
        try:
            ref = await opened(runtime, 'B1')
            def blank(state):
                state['attempts']['B1']['strategy'] = ''
                state['intents']['B1']['strategy'] = ''
                return state
            await runtime.owner.mutate('synthetic-blank-strategy', blank)
            metadata = {'registration_params': {'strategy_name': ''}} if explicit_registration else {}
            observation = await observed(runtime, ref, 100, '1000000', metadata=metadata)
            receipt = await queued(engine, observation)
            assert (receipt.status, receipt.protection_status) == ('APPLIED', 'ready')
            assert engine.portfolio.positions['005930'].strategy == ''
            assert exits.get_state('005930').strategy_name == ''
            await runtime.restore()
            assert exits.get_state('005930').remaining_quantity == 100
            assert exits.get_state('005930').strategy_name == ''
        finally: await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('invalid', [False, 0, ' ', ' sepa_trend'])
def test_invalid_strategy_is_not_coerced(invalid):
    exits = ExitManager(persist=False, clock=lambda: NOW)
    exits.register_position(Position('005930', quantity=1, avg_price=Decimal('10000'), entry_time=NOW))
    dto = encode_protection(exits)
    dto['states']['005930']['strategy_name'] = invalid
    with pytest.raises(ValueError):
        decode_protection(dto, clock=lambda: NOW)
