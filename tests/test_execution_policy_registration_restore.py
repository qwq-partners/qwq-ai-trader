"""Independent B2/B4/B6 probes. Only synthetic tmp SQLite; preserve this artifact."""
import asyncio
import sqlite3

import pytest

from test_execution_risk_input_seal import fixture
from test_execution_runtime import NOW
from src.execution.safety.application import ApplicationBlocked
from src.execution.safety.runtime import KRExecutionRuntime
from src.execution.safety.store import ExecutionStateStore, encode_state
from src.core.engine import UnifiedEngine
from src.core.types import TradingConfig
from src.strategies.exit_manager import ExitManager

SELECTOR = 'protection.config'

async def rewritten_checkpoint(tmp_path, attack):
    _, _, store, runtime, _ = await fixture(tmp_path)
    await runtime.owner.register_policy_generations('original', (SELECTOR,))
    def change(state):
        state['protection']['config']['first_exit_pct'] += 1
        return state
    await runtime.owner.mutate('change-B', change)
    await runtime.owner.mutate('change-C', change)
    state = runtime.owner.state
    root = state['policy_generations']
    row = root['selectors'][SELECTOR]
    assert [x['version'] for x in row['history']] == [2, 3, 4]
    if attack == 'receipt_only':
        root['registrations']['original']['version'] = 3
    elif attack == 'consistent_prefix_truncation':
        root['registrations']['original']['version'] = 3
        row['registered_version'] = 3
        row['history'].pop(0)
    elif attack == 'legacy_schema1_without_receipts':
        del root['registrations']
    elif attack == 'full_registry_erasure':
        del state['policy_generations']
    await runtime.shutdown()
    path = store.path
    await store.close()
    with sqlite3.connect(path) as db:
        commits_before = db.execute('SELECT * FROM commits ORDER BY version').fetchall()
        db.execute('UPDATE checkpoint SET state=? WHERE id=1', (encode_state(state),))
        assert db.execute('SELECT * FROM commits ORDER BY version').fetchall() == commits_before
    reopened = ExecutionStateStore(path)
    new = KRExecutionRuntime(reopened, UnifiedEngine(TradingConfig()),
        ExitManager(persist=False, clock=lambda: NOW), clock=lambda: NOW, account_scope='scope')
    return reopened, new


@pytest.mark.parametrize('attack', [
    'receipt_only', 'legacy_schema1_without_receipts',
    'consistent_prefix_truncation', 'full_registry_erasure',
])
def test_cold_restore_rejects_checkpoint_registration_damage(tmp_path, attack):
    async def scenario():
        store, runtime = await rewritten_checkpoint(tmp_path, attack)
        before = runtime.engine.portfolio.cash
        try:
            assert await store.lookup_commit('policy-registration:original') == 2
            with pytest.raises(ApplicationBlocked):
                await runtime.restore()
            assert not runtime.owner.healthy and not runtime.trading_ready
            assert runtime.engine.portfolio.cash == before
        finally:
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('attack', ['missing_sql_receipt', 'renamed_sql_receipt'])
def test_cold_restore_requires_exact_sql_registration_id_set(tmp_path, attack):
    async def scenario():
        store, runtime = await rewritten_checkpoint(tmp_path, 'unchanged')
        with sqlite3.connect(store.path) as db:
            if attack == 'missing_sql_receipt':
                db.execute("DELETE FROM commits WHERE commit_id='policy-registration:original'")
            else:
                db.execute("UPDATE commits SET commit_id='policy-registration:renamed' WHERE commit_id='policy-registration:original'")
        try:
            with pytest.raises(ApplicationBlocked):
                await runtime.restore()
            assert not runtime.owner.healthy
        finally:
            await store.close()
    asyncio.run(scenario())


def test_genuine_legacy_without_registration_receipts_still_restores(tmp_path):
    async def scenario():
        _, _, store, runtime, _ = await fixture(tmp_path)
        try:
            before = await store.load()
            assert 'policy_generations' not in before[1]
            assert await runtime.restore() == before[0]
            assert runtime.owner.healthy
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())
