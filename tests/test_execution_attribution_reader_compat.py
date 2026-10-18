"""임시 SQLite로 기존/귀속 의도의 읽기 호환성과 복구 경계를 검증한다."""

import sqlite3

import pytest

from src.execution.execution_history import ExecutionHistory
from src.execution.execution_ledger import ExecutionLedger, ExecutionLedgerError


FACTS = {
    'local_id': 'legacy-1', 'symbol': '005930', 'side': 'buy', 'quantity': 10,
    'order_date': '20261018', 'strategy': 'gap', 'reason': 'entry',
    'partial_exit': False,
}
ATTRIBUTION = {
    'version': 'order-attribution-v1', 'candidate_id': 'candidate:1',
    'plan_id': 'plan:1', 'signal_id': 'signal:1', 'llm_decision_id': None,
    'missing_reasons': {'llm_decision_id': 'not_available'},
}


def _rows(path):
    with sqlite3.connect(path) as connection:
        return {
            table: connection.execute(f'SELECT * FROM {table} ORDER BY rowid').fetchall()
            for table in ('metadata', 'sessions', 'orders', 'events')
        }


def _preserved(before, after):
    # 새 세션 행은 추가되지만 기존 이벤트/주문/세션 payload 바이트는 불변이다.
    for table in ('sessions', 'orders', 'events'):
        assert after[table][:len(before[table])] == before[table]


@pytest.mark.asyncio
async def test_legacy_rows_and_state_survive_reader_only_rollback(tmp_path):
    path = tmp_path / 'legacy.sqlite'
    legacy = ExecutionLedger(path, 'synthetic-scope')
    await legacy.open('legacy')
    await legacy.intent('legacy:1', FACTS)
    await legacy.outcome('legacy:1', 'rejected')
    assert await legacy.close_session() is True
    expected = (await legacy.snapshot())['orders']
    before = _rows(path)

    reader = ExecutionLedger(path, 'synthetic-scope')
    snapshot = await reader.open('compat')
    assert snapshot['prior_unclean'] is False
    assert snapshot['orders'] == expected
    assert snapshot['orders']['legacy:1']['facts'] == FACTS
    _preserved(before, _rows(path))
    assert await reader.close_session() is True


@pytest.mark.asyncio
async def test_mixed_rows_accept_legacy_append_and_reopen_without_rewriting(tmp_path):
    path = tmp_path / 'mixed.sqlite'
    forward = ExecutionLedger(path, 'synthetic-scope')
    await forward.open('forward')
    await forward.intent('forward:legacy', FACTS)
    await forward.outcome('forward:legacy', 'rejected')
    attributed = {**FACTS, 'local_id': 'attributed-1', 'attribution': ATTRIBUTION}
    await forward.intent('forward:attributed', attributed)
    await forward.outcome('forward:attributed', 'not_sent')
    assert await forward.close_session() is True
    expected = (await forward.snapshot())['orders']
    before = _rows(path)

    reader = ExecutionLedger(path, 'synthetic-scope')
    snapshot = await reader.open('compat')
    assert snapshot['prior_unclean'] is False
    assert snapshot['orders'] == expected
    _preserved(before, _rows(path))
    # 귀속 행을 읽은 뒤에도 8필드 의도를 같은 원장에 기록할 수 있어야 한다.
    legacy_facts = {**FACTS, 'local_id': 'rollback-1'}
    await reader.intent('compat:1', legacy_facts)
    await reader.outcome('compat:1', 'not_sent')
    assert (await reader.snapshot())['orders']['compat:1']['facts'] == legacy_facts
    assert await reader.close_session() is True
    rollback_snapshot = (await reader.snapshot())['orders']
    rollback_rows = _rows(path)

    reopened = ExecutionLedger(path, 'synthetic-scope')
    snapshot = await reopened.open('forward-again')
    assert snapshot['orders'] == rollback_snapshot
    assert snapshot['prior_unclean'] is False
    _preserved(rollback_rows, _rows(path))
    assert await reopened.close_session() is True


@pytest.mark.asyncio
async def test_attributed_unresolved_history_keeps_recovery_hold(tmp_path):
    path = tmp_path / 'unresolved.sqlite'
    forward = ExecutionLedger(path, 'synthetic-scope')
    await forward.open('unclean')
    await forward.intent('unclean:1', {**FACTS, 'attribution': ATTRIBUTION})
    await forward.outcome('unclean:1', 'unknown')
    before = _rows(path)
    history = ExecutionHistory(path, 'synthetic-scope')
    await history.open()
    assert history.fault is None
    report = history.report()
    assert report['status'] == 'recovery_required'
    assert report['prior_unclean'] is True
    assert report['replay_allowed'] is False
    assert history.hold('005930') is not None
    assert await history.close() is False
    _preserved(before, _rows(path))
    with pytest.raises(ExecutionLedgerError):
        await history.ledger.outcome('unclean:1', 'rejected')


@pytest.mark.asyncio
@pytest.mark.parametrize('attribution', [
    None,
    {},
    {**ATTRIBUTION, 'version': 'order-attribution-v2'},
    {**ATTRIBUTION, 'candidate_id': 1},
    {**ATTRIBUTION, 'candidate_id': 'bad id'},
    {**ATTRIBUTION, 'missing_reasons': {}},
    {**ATTRIBUTION, 'missing_reasons': {'llm_decision_id': 'guessed'}},
    {**ATTRIBUTION, 'extra': 'forbidden'},
])
async def test_invalid_envelope_rejected_without_persistent_mutation(tmp_path, attribution):
    path = tmp_path / 'invalid.sqlite'
    ledger = ExecutionLedger(path, 'synthetic-scope')
    await ledger.open('test')
    before = _rows(path)
    with pytest.raises(ExecutionLedgerError):
        await ledger.intent('invalid:1', {**FACTS, 'attribution': attribution})
    assert _rows(path) == before
    assert await ledger.close_session() is True


@pytest.mark.asyncio
async def test_invalid_persisted_envelope_fails_closed_without_new_session(tmp_path, monkeypatch):
    path = tmp_path / 'corrupt-envelope.sqlite'
    forward = ExecutionLedger(path, 'synthetic-scope')
    await forward.open('forward')
    # 합성 원장의 봉인/이벤트 정합성은 유지하고 귀속 검사만 우회해 손상을 주입한다.
    with monkeypatch.context() as patch:
        patch.setattr('src.execution.execution_ledger.validate_attribution', lambda payload: payload)
        await forward.intent('forward:1', {
            **FACTS, 'attribution': {**ATTRIBUTION, 'version': 'invalid-version'},
        })
    before = _rows(path)
    history = ExecutionHistory(path, 'synthetic-scope')
    await history.open()
    assert history.fault == 'open_failed'
    assert history.report()['status'] == 'storage_fault'
    assert history.report()['replay_allowed'] is False
    assert _rows(path) == before
