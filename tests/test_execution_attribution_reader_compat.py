"""임시 SQLite로 기존/귀속 의도의 읽기 호환성과 복구 경계를 검증한다."""

from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from src.execution.execution_history import ExecutionHistory
from src.execution.execution_ledger import (
    ExecutionLedger, ExecutionLedgerError, read_execution_ledger,
)


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


ROOT = Path(__file__).resolve().parents[1]
APPROVED_SOURCE_SHA256 = {
    'src/core/order_attribution.py': '4ff5db0b6d51639daca54f33fde24e959de8677d4e2e121d5c6a04c1bdd5b8fb',
    'src/execution/execution_ledger.py': '9778f0a90ab95e6cc59fedc5e4d4d72db265fb3146a7d9be71ffab11166437fc',
}


@pytest.fixture(autouse=True)
def close_test_ledger_executors(monkeypatch):
    # 제품 수명주기는 바꾸지 않고 이 파일이 만든 작업 스레드만 정리한다.
    executors = []
    original_init = ExecutionLedger.__init__

    def tracked_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        executors.append(self._executor)

    monkeypatch.setattr(ExecutionLedger, '__init__', tracked_init)
    yield
    for executor in executors:
        executor.shutdown(wait=True)


@pytest.fixture
def forward_golden(tmp_path):
    fixture = json.loads((ROOT / 'tests/fixtures/attribution_forward_v1_ledger.json').read_text())
    assert fixture['fixture_version'] == 'attribution-forward-ledger-v1'
    assert fixture['source_revision'] == 'ea5d54cd91a98322c13b781228b255da9cb5f7f8'
    assert fixture['user_version'] == 1
    for name, digest in APPROVED_SOURCE_SHA256.items():
        assert fixture['source_sha256'][name] == digest
    path = tmp_path / 'forward-golden.sqlite'
    with closing(sqlite3.connect(path)) as connection:
        connection.execute('PRAGMA user_version = 1')
        for statement in fixture['schema']:
            connection.execute(statement)
        for table, columns in (('metadata', 2), ('sessions', 2), ('orders', 2), ('events', 4)):
            placeholders = ','.join('?' for _ in range(columns))
            connection.executemany(f'INSERT INTO {table} VALUES ({placeholders})', fixture['tables'][table])
        connection.commit()
    return path, fixture


def test_reader_runtime_matches_approved_forward_source():
    # 두 단계 배포의 호환성 계약: 변경 시 새 버전 자료로 게이트를 다시 검토한다.
    for name, digest in APPROVED_SOURCE_SHA256.items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest


def test_forward_history_golden_is_readable_offline_without_mutation(forward_golden):
    path, fixture = forward_golden
    before = path.read_bytes()
    files = set(path.parent.iterdir())
    report = read_execution_ledger(path, fixture['scope'])
    assert report['event_count'] == 3
    assert report['schema_version'] == 1
    assert report['source_sha256'] == hashlib.sha256(before).hexdigest()
    assert report['orders'] == {
        key: json.loads(payload) for key, payload in fixture['tables']['orders']
    }
    facts = report['orders']['forward-golden-v1:attributed-1']['facts']
    assert facts == {**FACTS, 'local_id': 'attributed-1', 'attribution': ATTRIBUTION}
    assert path.read_bytes() == before
    assert set(path.parent.iterdir()) == files


@pytest.mark.asyncio
async def test_forward_unknown_acknowledge_preserves_attribution_and_legacy_append(forward_golden):
    path, fixture = forward_golden
    original_rows = _rows(path)
    original_order = json.loads(original_rows['orders'][0][1])
    key = 'forward-golden-v1:attributed-1'
    attribution_bytes = json.dumps(original_order['facts']['attribution'], sort_keys=True).encode()
    reader = ExecutionLedger(path, fixture['scope'])
    snapshot = await reader.open('compat-ack')
    assert snapshot['prior_unclean'] is True
    assert snapshot['orders'][key] == original_order
    _preserved(original_rows, _rows(path))
    assert await reader.close_session() is False
    await reader.acknowledge('synthetic reconciliation complete')
    after = await reader.snapshot()
    assert after['prior_unclean'] is False
    assert after['orders'][key] == {**original_order, 'acknowledged': True}
    assert json.dumps(after['orders'][key]['facts']['attribution'], sort_keys=True).encode() == attribution_bytes
    assert _rows(path)['events'][:3] == original_rows['events']
    assert await reader.close_session() is True

    # 위 파일 지문 검사가 이 reader와 승인된 forward reader의 동일성을 보장한다.
    reopened = ExecutionLedger(path, fixture['scope'])
    snapshot = await reopened.open('resumed')
    assert snapshot['prior_unclean'] is False
    assert snapshot['orders'] == after['orders']
    await reopened.intent('resumed:legacy', FACTS)
    await reopened.outcome('resumed:legacy', 'not_sent')
    assert await reopened.close_session() is True
    report = read_execution_ledger(path, fixture['scope'])
    assert report['orders'][key] == after['orders'][key]
    assert report['orders']['resumed:legacy']['facts'] == FACTS


@pytest.mark.asyncio
async def test_forward_history_intent_persists_attribution_after_compatible_baseline(forward_golden):
    path, fixture = forward_golden
    ack = ExecutionLedger(path, fixture['scope'])
    await ack.open('operator-ack')
    await ack.acknowledge('synthetic reconciliation complete')
    assert await ack.close_session() is True
    history = ExecutionHistory(path, fixture['scope'])
    await history.open()
    assert history.report()['status'] == 'ready'
    order = SimpleNamespace(
        id='baseline-writer', symbol='005930', side=SimpleNamespace(value='buy'),
        quantity=10, strategy='gap', reason='entry', partial_exit=False,
    )
    await history.intent(order, '20261018')
    await history.outcome(order.id, 'not_sent')
    assert await history.close() is True
    report = read_execution_ledger(path, fixture['scope'])
    facts = report['orders'][history.keys[order.id]]['facts']
    # The separately deployed reader baseline was verified with exactly eight
    # fields. This forward release deliberately starts writing the ninth field.
    missing = ('candidate_id', 'plan_id', 'signal_id', 'llm_decision_id')
    assert set(facts) == set(FACTS) | {'attribution'}
    assert facts == {**FACTS, 'local_id': order.id, 'attribution': {
        'version': 'order-attribution-v1', **{name: None for name in missing},
        'missing_reasons': {name: 'not_available' for name in missing},
    }}
    persisted_intent = next(
        json.loads(payload) for _, _, operation, payload in _rows(path)['events']
        if operation == 'intent' and json.loads(payload)['key'] == history.keys[order.id]
    )
    assert persisted_intent['facts'] == facts


def _rows(path):
    with closing(sqlite3.connect(path)) as connection:
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
    [],
    {},
    {**ATTRIBUTION, 'missing_reasons': {'llm_decision_id': 1}},
    {**ATTRIBUTION, 'candidate_id': 'a' * 161},
    {**ATTRIBUTION, 'candidate_id': ''},
    {**ATTRIBUTION, 'candidate_id': '-leading'},
    {**ATTRIBUTION, 'candidate_id': '.leading'},
    {**{key: value for key, value in ATTRIBUTION.items() if key != 'version'}, 'other': 'order-attribution-v1'},
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


@pytest.mark.asyncio
async def test_second_unknown_fact_key_is_rejected_without_mutation(tmp_path):
    path = tmp_path / 'extra-fact.sqlite'
    ledger = ExecutionLedger(path, 'synthetic-scope')
    await ledger.open('test')
    before = _rows(path)
    with pytest.raises(ExecutionLedgerError):
        await ledger.intent('extra:1', {**FACTS, 'attribution': ATTRIBUTION, 'future_fact': 'unsupported'})
    assert _rows(path) == before
    assert await ledger.close_session() is True
