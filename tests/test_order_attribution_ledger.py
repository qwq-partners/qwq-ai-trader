"""귀속 원장의 임시 SQLite 검증: 과거 구조 보존과 주문별 불변 근거."""

import copy
import importlib
import sqlite3
from dataclasses import FrozenInstanceError

import pytest

from src.core.types import Order
from src.execution.execution_history import ExecutionHistory
from src.execution.execution_ledger import ExecutionLedger, ExecutionLedgerError, read_execution_ledger


ID_FIELDS = ('candidate_id', 'plan_id', 'signal_id', 'llm_decision_id')
FACTS = {
    'local_id': 'order-1', 'symbol': '005930', 'side': 'buy', 'quantity': 10,
    'order_date': '20261018', 'strategy': 'gap', 'reason': 'entry',
    'partial_exit': False,
}
ATTRIBUTION = {
    'version': 'order-attribution-v1', 'candidate_id': 'candidate-1',
    'plan_id': 'plan-1', 'signal_id': 'signal-1', 'llm_decision_id': 'decision-1',
    'missing_reasons': {},
}


def attribution_module():
    return importlib.import_module('src.core.order_attribution')


def missing_payload(reason='not_available'):
    return {
        'version': 'order-attribution-v1', **dict.fromkeys(ID_FIELDS),
        'missing_reasons': dict.fromkeys(ID_FIELDS, reason),
    }


@pytest.mark.asyncio
async def test_unattributed_order_records_explicit_missing_ids(tmp_path):
    history = ExecutionHistory(tmp_path / 'execution.sqlite', 'synthetic')
    order = Order(id='order-1', symbol='005930', quantity=10)
    await history.intent(order, '20261018')
    record = history.report()['orders'][history.keys[order.id]]
    assert record['facts'].get('attribution') == missing_payload()
    assert history.fault is None


@pytest.mark.parametrize('value', [True, False, 12, 1.2, {}, [], b'id', '',
                                  ' space', 'space ', 'id\n', '한글', '_id',
                                  'id/path', 'a' * 161])
def test_factory_marks_invalid_ids_without_coercion(value):
    api = attribution_module()
    result = api.freeze_attribution(candidate_id=value).to_dict()
    assert result['candidate_id'] is None
    assert result['missing_reasons']['candidate_id'] == 'invalid'
    assert result['missing_reasons']['plan_id'] == 'not_available'


def test_factory_keeps_exact_valid_ids_and_freezes_missing_reasons():
    api = attribution_module()
    reasons = {'plan_id': 'conflict', 'candidate_id': 'invalid'}
    frozen = api.freeze_attribution(candidate_id='Ab0._:-', signal_id='a' * 160,
                                  missing_reasons=reasons)
    reasons['plan_id'] = 'not_available'
    result = frozen.to_dict()
    assert result == {
        'version': 'order-attribution-v1', 'candidate_id': 'Ab0._:-',
        'plan_id': None, 'signal_id': 'a' * 160, 'llm_decision_id': None,
        'missing_reasons': {'plan_id': 'conflict', 'llm_decision_id': 'not_available'},
    }
    with pytest.raises(FrozenInstanceError):
        frozen.plan_id = 'replacement'
    result['missing_reasons']['plan_id'] = 'invalid'
    assert frozen.to_dict()['missing_reasons']['plan_id'] == 'conflict'


@pytest.mark.parametrize('field,value', [
    ('version', 'future'), ('candidate_id', True), ('candidate_id', 1),
    ('candidate_id', 'a' * 161), ('candidate_id', 'id\n'),
    ('candidate_id', '한글'), ('candidate_id', ''),
    ('missing_reasons', {'candidate_id': 'not_available'}),
    ('missing_reasons', {'other': 'invalid'}), ('missing_reasons', []),
])
def test_persisted_schema_rejects_noncanonical_fields(field, value):
    payload = copy.deepcopy(ATTRIBUTION)
    payload[field] = value
    with pytest.raises(ValueError):
        attribution_module().validate_attribution(payload)


@pytest.mark.parametrize('mutation', ['extra', 'missing', 'missing_reason', 'bad_reason', 'null_payload'])
def test_persisted_schema_requires_exact_shape_and_complete_missing_reasons(mutation):
    payload = missing_payload()
    if mutation == 'extra':
        payload['extra'] = 1
    elif mutation == 'missing':
        del payload['signal_id']
    elif mutation == 'missing_reason':
        del payload['missing_reasons']['plan_id']
    elif mutation == 'bad_reason':
        payload['missing_reasons']['plan_id'] = 'guessed'
    else:
        payload = None
    with pytest.raises(ValueError):
        attribution_module().validate_attribution(payload)


@pytest.mark.parametrize('reason', ['not_available', 'invalid', 'conflict', 'legacy_not_recorded'])
def test_validation_copies_valid_payload_and_reasons(reason):
    payload = missing_payload(reason)
    result = attribution_module().validate_attribution(payload)
    payload['missing_reasons']['plan_id'] = 'changed'
    assert result == missing_payload(reason)


def test_factory_does_not_call_invalid_objects_string_conversion():
    class NotAnId:
        def __str__(self):
            raise AssertionError('ID 문자열 강제 변환 금지')

    result = attribution_module().freeze_attribution(plan_id=NotAnId()).to_dict()
    assert result['plan_id'] is None
    assert result['missing_reasons']['plan_id'] == 'invalid'


@pytest.mark.asyncio
async def test_legacy_facts_survive_mixed_replay_without_rewriting_old_rows(tmp_path):
    path = tmp_path / 'execution.sqlite'
    legacy = ExecutionLedger(path, 'synthetic')
    await legacy.open('legacy')
    await legacy.intent('legacy:order-1', FACTS)
    await legacy.outcome('legacy:order-1', 'rejected')
    assert await legacy.close_session()
    with sqlite3.connect(path) as db:
        original_events = db.execute('SELECT * FROM events ORDER BY sequence').fetchall()
        original_order = db.execute('SELECT payload FROM orders WHERE order_key = ?',
                                    ('legacy:order-1',)).fetchone()
    history = ExecutionHistory(path, 'synthetic')
    await history.intent(Order(id='new', symbol='005930', quantity=10), '20261018')
    await history.outcome('new', 'rejected')
    assert await history.close()
    reopened = ExecutionLedger(path, 'synthetic')
    snapshot = await reopened.open('reopened')
    assert snapshot['orders']['legacy:order-1']['facts'] == FACTS
    assert snapshot['orders'][history.keys['new']]['facts']['attribution'] == missing_payload()
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT * FROM events WHERE sequence <= ? ORDER BY sequence',
                          (len(original_events),)).fetchall() == original_events
        assert db.execute('SELECT payload FROM orders WHERE order_key = ?',
                          ('legacy:order-1',)).fetchone() == original_order


@pytest.mark.asyncio
async def test_attribution_survives_two_partial_fills_cancel_receipts_and_reopen(tmp_path):
    path = tmp_path / 'execution.sqlite'
    history = ExecutionHistory(path, 'synthetic')
    frozen = attribution_module().freeze_attribution(**{
        field: ATTRIBUTION[field] for field in ID_FIELDS
    })
    order = Order(id='order-1', symbol='005930', quantity=10, attribution=frozen)
    await history.intent(order, '20261018')
    await history.accepted(order.id, '000123', '001')
    first = await history.observe(order.id, 3, 100)
    assert await history.observe(order.id, 3, 100) is None
    second = await history.observe(order.id, 7, 104)
    assert await history.observe(order.id, 7, 104) is None
    await history.outcome(order.id, 'canceled')
    assert await history.observe(order.id, 7, 104, terminal=True) is None
    for execution_id in (first, second):
        await history.receipt(execution_id, 'portfolio_applied')
        await history.receipt(execution_id, 'handoff_returned')
    assert await history.close()
    reopened = ExecutionHistory(path, 'synthetic')
    await reopened.open()
    record = reopened.report()['orders'][history.keys[order.id]]
    assert record['facts']['attribution'] == ATTRIBUTION
    assert record['status'] == 'canceled'
    assert record['terminal_quantity'] == 7
    assert record['executions'] == [
        {'execution_id': first, 'from_quantity': 0, 'to_quantity': 3, 'price': '100',
         'portfolio_applied': True, 'handoff_returned': True},
        {'execution_id': second, 'from_quantity': 3, 'to_quantity': 7, 'price': '107',
         'portfolio_applied': True, 'handoff_returned': True},
    ]
    assert reopened.report()['status'] == 'ready'
    assert await reopened.close()
    exported = read_execution_ledger(path, 'synthetic')
    assert exported['orders'][history.keys[order.id]]['facts']['attribution'] == ATTRIBUTION


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['unknown', 'not_sent', 'rejected'])
async def test_nonaccepted_outcomes_preserve_attribution(tmp_path, status):
    path = tmp_path / 'execution.sqlite'
    history = ExecutionHistory(path, 'synthetic')
    frozen = attribution_module().freeze_attribution(signal_id='signal-1')
    order = Order(id='order-1', symbol='005930', quantity=10, attribution=frozen)
    await history.intent(order, '20261018')
    await history.outcome(order.id, status)
    assert await history.close() is (status != 'unknown')
    reopened = ExecutionHistory(path, 'synthetic')
    await reopened.open()
    record = reopened.report()['orders'][history.keys[order.id]]
    assert record['facts']['attribution']['signal_id'] == 'signal-1'
    assert record['status'] == status
    assert record['executions'] == []


@pytest.mark.asyncio
@pytest.mark.parametrize('malformed', [{}, ATTRIBUTION, True, 'candidate-1'])
async def test_malformed_order_attribution_degrades_without_history_fault(tmp_path, malformed):
    history = ExecutionHistory(tmp_path / 'execution.sqlite', 'synthetic')
    order = Order(id='order-1', symbol='005930', quantity=10)
    order.attribution = malformed
    await history.intent(order, '20261018')
    assert history.report()['orders'][history.keys[order.id]]['facts']['attribution'] == missing_payload('invalid')
    assert history.fault is None
    assert history.hold() is None


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [
    ('candidate_id', True), ('missing_reasons', []),
    ('missing_reasons', (('plan_id', 'invalid'), ('plan_id', 'conflict'))),
])
async def test_noncanonical_frozen_object_degrades_without_history_fault(tmp_path, field, value):
    frozen = attribution_module().freeze_attribution(candidate_id='candidate-1')
    object.__setattr__(frozen, field, value)
    history = ExecutionHistory(tmp_path / 'execution.sqlite', 'synthetic')
    order = Order(id='order-1', symbol='005930', quantity=10, attribution=frozen)
    await history.intent(order, '20261018')
    assert history.report()['orders'][history.keys[order.id]]['facts']['attribution'] == missing_payload('invalid')
    assert history.fault is None


@pytest.mark.asyncio
async def test_mutations_after_intent_do_not_change_persisted_attribution(tmp_path):
    api = attribution_module()
    history = ExecutionHistory(tmp_path / 'execution.sqlite', 'synthetic')
    frozen = api.freeze_attribution(candidate_id='candidate-1')
    order = Order(id='order-1', symbol='005930', quantity=10, attribution=frozen)
    await history.intent(order, '20261018')
    order.attribution = api.freeze_attribution(candidate_id='candidate-2')
    result = history.report()
    result['orders'][history.keys[order.id]]['facts']['attribution']['candidate_id'] = 'candidate-3'
    durable = await history.ledger.snapshot()
    assert durable['orders'][history.keys[order.id]]['facts']['attribution']['candidate_id'] == 'candidate-1'


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [('attribution', None), ('unexpected', 1),
                                        ('attribution', {**ATTRIBUTION, 'candidate_id': True})])
async def test_ledger_rejects_invalid_payload_and_unknown_fact_shapes(tmp_path, field, value):
    ledger = ExecutionLedger(tmp_path / 'execution.sqlite', 'synthetic')
    await ledger.open('session')
    with pytest.raises(ExecutionLedgerError):
        await ledger.intent('session:order-1', {**FACTS, field: value})
    assert (await ledger.snapshot())['orders'] == {}
