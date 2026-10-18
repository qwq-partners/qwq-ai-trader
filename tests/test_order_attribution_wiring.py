"""판단 식별자는 원문 링크에서만 복사하며 실제 주문 조건을 바꾸지 않는다."""
from copy import deepcopy
import asyncio
from importlib import import_module, util
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.analytics import entry_observation as obs
from src.core.event import SignalEvent


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path))


def capture(owner, event):
    name = 'src.analytics.order_attribution_capture'
    assert util.find_spec(name) is not None, '주문 식별자 복사 경로 미구현'
    return import_module(name).capture_order_attribution(owner, event)


def observed(event, capacity=20):
    buffer = obs.EntryObservationBuffer(evaluation_epoch='synthetic', capacity=capacity)
    scan = obs.capture_scan(buffer, [SimpleNamespace(symbol=event.symbol)], 'regular')
    obs.capture_signal(buffer, scan, event)
    return SimpleNamespace(_entry_price_observer=buffer), scan


def test_real_signal_links_only_to_recorded_candidate_and_keeps_inputs():
    event = SignalEvent(symbol='005930', source='live_screening', metadata={
        'entry_plan': {'plan_id': 'plan-1', 'symbol': '005930'},
        'llm_decision': {'decision_id': 'dec-1', 'record_status': 'error'},
        'private_extra': 'must-not-be-copied'})
    owner, scan = observed(event)
    before_event, before_records = deepcopy(event), owner._entry_price_observer.export()
    value = capture(owner, event).to_dict()
    assert value == {'version': 'order-attribution-v1', 'candidate_id': scan + ':005930',
                     'plan_id': 'plan-1', 'signal_id': event.id,
                     'llm_decision_id': 'dec-1', 'missing_reasons': {}}
    assert event == before_event and owner._entry_price_observer.export() == before_records
    # ID가 있다는 사실은 LLM 판단 기록이 영속 저장됐다는 뜻이 아니다.
    event.metadata['llm_decision']['decision_id'] = 'changed'
    assert value['llm_decision_id'] == 'dec-1'


def test_no_observer_does_not_guess_candidate_from_symbol_or_metadata():
    event = SignalEvent(symbol='005930', metadata={'candidate_id': 'invented:005930'})
    value = capture(None, event).to_dict()
    assert value['signal_id'] == event.id
    assert value['candidate_id'] is None
    assert value['missing_reasons']['candidate_id'] == 'not_available'


def test_dropped_signal_cannot_be_promoted_to_candidate_link():
    event = SignalEvent(symbol='005930', source='live_screening')
    owner, _ = observed(event, capacity=1)
    assert capture(owner, event).to_dict()['candidate_id'] is None


def test_conflicting_signal_ids_are_not_joined_by_symbol():
    event = SignalEvent(symbol='005930', source='live_screening')
    owner, _ = observed(event)
    other_scan = obs.capture_scan(owner._entry_price_observer,
                                  [SimpleNamespace(symbol='005930')], 'regular')
    obs.capture_signal(owner._entry_price_observer, other_scan, event)
    value = capture(owner, event).to_dict()
    assert value['candidate_id'] is None
    assert value['missing_reasons']['candidate_id'] == 'conflict'


def test_orphan_signal_record_is_not_enough_for_candidate_link():
    event = SignalEvent(symbol='005930', source='live_screening')
    buffer = obs.EntryObservationBuffer(evaluation_epoch='synthetic', capacity=10)
    obs.capture_signal(buffer, 'orphan-scan', event)
    value = capture(SimpleNamespace(_entry_price_observer=buffer), event).to_dict()
    assert value['candidate_id'] is None


@pytest.mark.parametrize('metadata', [None, {'entry_plan': []}, {'llm_decision': []},
    {'entry_plan': {'plan_id': 'p', 'symbol': '000660'}},
    {'entry_plan': {'plan_id': True}, 'llm_decision': {'decision_id': 'x' * 161}}])
def test_optional_bad_metadata_is_missing_without_stopping_order(metadata):
    event = SignalEvent(symbol='005930', metadata=metadata)
    value = capture(None, event).to_dict()
    assert value['signal_id'] == event.id
    assert value['plan_id'] is None and value['llm_decision_id'] is None


def test_real_risk_order_keeps_identifiers_and_existing_quantity(monkeypatch):
    from test_risk_sizing import _rm, _em, _sig
    from test_entry_risk_lifecycle import _order_path
    event = _sig(2.5, entry_plan={'plan_id': 'plan-real'},
                 llm_decision={'decision_id': 'dec-real'})
    event.source = 'live_screening'
    owner, scan = observed(event)
    risk = _rm(monkeypatch, mode='risk', em=_em())
    risk._entry_observation_owner = owner
    orders, _ = _order_path(monkeypatch, risk, event)
    assert len(orders) == 1 and orders[0].order.quantity == 139
    attribution = getattr(orders[0].order, 'attribution', None)
    assert attribution is not None, '실제 on_signal 주문에 판단 식별자가 없음'
    assert attribution.to_dict()['candidate_id'] == scan + ':005930'
    assert attribution.to_dict()['signal_id'] == event.id
    assert attribution.to_dict()['llm_decision_id'] == 'dec-real'


def test_risk_order_to_persisted_partial_fills_and_exact_offline_rows(tmp_path, monkeypatch):
    from test_risk_sizing import _rm, _em, _sig
    from test_entry_risk_lifecycle import _order_path
    from test_recovery_evidence import evidence
    from src.execution.execution_history import ExecutionHistory
    from src.execution.execution_ledger import read_execution_ledger
    from src.execution.recovery_evidence import reconcile_execution_evidence

    event = _sig(2.5, llm_decision={'decision_id': 'dec-integration'})
    event.source = 'live_screening'
    owner, scan = observed(event)
    risk = _rm(monkeypatch, mode='risk', em=_em())
    risk._entry_observation_owner = owner
    orders, _ = _order_path(monkeypatch, risk, event)
    order = orders[0].order
    history = ExecutionHistory(tmp_path / 'executions.sqlite', 'test-scope')

    async def persist():
        await history.intent(order, '20261018')
        await history.accepted(order.id, '000123', '001')
        first = await history.observe(order.id, 4, 100)
        assert await history.observe(order.id, 4, 100) is None
        second = await history.observe(order.id, 10, 106)
        await history.outcome(order.id, 'canceled')
        assert await history.observe(order.id, 10, 106, terminal=True) is None
        for execution_id in (first, second):
            await history.receipt(execution_id, 'portfolio_applied')
            await history.receipt(execution_id, 'handoff_returned')
        assert await history.close()
        return first, second

    ids = asyncio.run(persist())
    _, broker, journal, baseline = evidence()
    for source in (broker, journal, baseline):
        source.update(window_start='2026-10-18', window_end='2026-10-18',
                      captured_at='2026-10-18T20:00:00+09:00')
    broker['rows'][0].update(ord_dt='20261018', ord_qty=str(order.quantity),
                            cnc_cfrm_qty=str(order.quantity - 10), cncl_yn='Y')
    for row, execution_id in zip(journal['rows'], ids):
        row.update(event_date='2026-10-18', execution_id=execution_id)
    for row in baseline['rows']:
        row['captured_at'] = baseline['captured_at']
    report = reconcile_execution_evidence(
        read_execution_ledger(tmp_path / 'executions.sqlite', 'test-scope'), broker, journal, baseline)
    assert report['comparison_status'] == 'consistent'
    assert 'attribution_chains' in report, '실제 의도·증분 원장과 거래 행의 연결 미구현'
    chain, = report['attribution_chains']
    assert chain['attribution']['candidate_id'] == scan + ':005930'
    assert chain['attribution']['llm_decision_id'] == 'dec-integration'
    assert [row['execution_id'] for row in chain['executions']] == list(ids)
    assert all(row['journal_identity_status'] == 'matched_supplied_row'
               and row['journal_durability'] == 'unverified' for row in chain['executions'])


@pytest.mark.parametrize('payload', [None, {'version': 'order-attribution-v1', 'extra': 'private'}])
def test_offline_report_rejects_noncanonical_attribution(payload):
    from test_recovery_evidence import evidence, stamp
    from src.execution.recovery_evidence import reconcile_execution_evidence
    args = evidence()
    args[0]['orders']['s1:o1']['facts']['attribution'] = payload
    stamp(args[0])
    with pytest.raises(ValueError):
        reconcile_execution_evidence(*args)


def test_explicit_unknown_execution_ids_cannot_pass_legacy_aggregate():
    from test_recovery_evidence import evidence
    from src.execution.recovery_evidence import reconcile_execution_evidence
    args = evidence()
    for i, row in enumerate(args[2]['rows']):
        row['execution_id'] = f'unknown:{i}'
    assert reconcile_execution_evidence(*args)['comparison_status'] == 'mismatch'
