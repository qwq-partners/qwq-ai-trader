"""Synthetic records only: capture memory failure never enrolls or reaches disk."""
import sys
from types import SimpleNamespace

import pytest

from src.analytics import entry_observation as obs
from test_llm_window_observation import setup
from test_signal_window_observation import stock, event


def guarded(limit=100000, maximum=1024):
    return obs.EntryObservationBuffer(evaluation_epoch='synthetic-memory', capacity=100,
        retained_memory_limit_bytes=limit, max_record_bytes=maximum)


def row(**fields):
    return dict(kind='ws_quote', observed_at='2026-10-15T00:00:01+00:00', **fields)


@pytest.mark.parametrize('field', ['exchange_time', 'hour_class_code', 'market_class_code'])
def test_oversized_book_metadata_rejected_before_copy_or_journal(monkeypatch, field):
    b = guarded()
    offered = []
    b._journal_sink = SimpleNamespace(offer=offered.append, failure_reason=None)
    monkeypatch.setattr(obs, 'deepcopy', lambda _: pytest.fail('copied rejected input'))
    assert b.publish(row(**{field: 'x' * 100000})) is False
    assert b._records == [] and offered == [] and b.retained_memory_bytes == 0
    assert b._dropped == 1 and not b.capture_status()['complete']


@pytest.mark.parametrize('defect', ['unknown', 'cycle', 'nested', 'nan', 'huge_keys'])
def test_non_json_or_unbounded_shape_never_deepcopies(monkeypatch, defect):
    r = row()
    if defect == 'unknown': r['exchange_time'] = object()
    elif defect == 'cycle': r['exchange_time'] = r
    elif defect == 'nested': r['exchange_time'] = [[[[[[['x']]]]]]]
    elif defect == 'nan': r['ask'] = float('nan')
    else: r.update({str(i): i for i in range(10000)})
    b = guarded()
    monkeypatch.setattr(obs, 'deepcopy', lambda _: pytest.fail('copied rejected input'))
    assert b.publish(r) is False
    assert b._records == [] and b._dropped == 1


def deep_size(value):
    result = sys.getsizeof(value)
    if type(value) is dict:
        result += sum(deep_size(k) + deep_size(v) for k, v in value.items())
    elif type(value) is list:
        result += sum(deep_size(v) for v in value)
    return result


def test_cumulative_budget_is_conservative_and_sticky(monkeypatch):
    b = guarded(2000)
    assert b.publish(row())
    assert b.retained_memory_bytes >= deep_size(b._records[0])
    b.retained_memory_limit_bytes = b.retained_memory_bytes
    before = list(b._records)
    monkeypatch.setattr(obs, 'deepcopy', lambda _: pytest.fail('copied exhausted input'))
    for _ in range(5): assert b.publish(row()) is False
    assert b.memory_exhausted and b._records == before and b._dropped == 5
    assert 'retained_memory_limit' in b.capture_status()['incomplete_reasons']


def test_actual_copy_budget_rechecked_before_retention(monkeypatch):
    b = guarded(2000)
    def inflated(record):
        return {**record, 'exchange_time': 'x' * 3000}
    monkeypatch.setattr(obs, 'deepcopy', inflated)
    assert b.publish(row()) is False
    assert b._records == [] and b.retained_memory_bytes == 0 and b.memory_exhausted


def test_disabled_base_guard_preserves_existing_contract():
    b = obs.EntryObservationBuffer(evaluation_epoch='legacy', capacity=2)
    assert b.publish({'kind': 'legacy_unrestricted', 'data': 'x' * 100000})
    assert b.retained_memory_limit_bytes is None and b.retained_memory_bytes == 0


def test_llm_pair_memory_failure_is_atomic(tmp_path, monkeypatch):
    _, _, b, _, _ = setup(tmp_path, monkeypatch)
    assert b.retained_memory_limit_bytes == 384 * 1024 * 1024
    scan = obs.capture_scan(b, [stock()], 'regular')
    obs.capture_signal(b, scan, event('synthetic', '900001'))
    before = (list(b._records), dict(b._record_counts), set(b._cohort_signals),
              set(b._llm_seen), dict(b._llm_symbols), b.retained_memory_bytes)
    b.retained_memory_limit_bytes = b.retained_memory_bytes + 1
    offered, enrolled = [], []
    b._journal_sink = SimpleNamespace(offer_pair=offered.append, failure_reason=None)
    b.llm_quote_submit = enrolled.append
    monkeypatch.setattr(obs, 'deepcopy', lambda _: pytest.fail('copied exhausted pair'))
    e = event('synthetic', '900001'); e.source = 'live_screening'
    obs.capture_llm_check_started(SimpleNamespace(_entry_price_observer=b), e)
    assert before == (b._records, b._record_counts, b._cohort_signals,
                      b._llm_seen, b._llm_symbols, b.retained_memory_bytes)
    assert not offered and not enrolled and b.memory_exhausted


def test_pair_journal_rejection_does_not_charge_memory(tmp_path, monkeypatch):
    _, _, b, _, _ = setup(tmp_path, monkeypatch)
    scan = obs.capture_scan(b, [stock()], 'regular')
    obs.capture_signal(b, scan, event('synthetic', '900001'))
    before = b.retained_memory_bytes
    b._journal_sink = SimpleNamespace(offer_pair=lambda pair: False, failure_reason=None)
    e = event('synthetic', '900001'); e.source = 'live_screening'
    obs.capture_llm_check_started(SimpleNamespace(_entry_price_observer=b), e)
    assert b.retained_memory_bytes == before and not b._llm_seen


def test_llm_malformed_record_preserves_quota_cohort_and_journal(tmp_path, monkeypatch):
    _, _, b, _, _ = setup(tmp_path, monkeypatch)
    offered = []
    b._journal_sink = SimpleNamespace(offer=offered.append, failure_reason=None)
    assert not b.publish(row(exchange_time='x' * (b._retained_max_record_bytes + 1)))
    assert not b._records and not b._record_counts and not b._cohort_candidates
    assert not offered and b.retained_memory_bytes == 0
    assert not b.capture_status()['complete']


def test_nested_container_and_keys_are_charged():
    b = guarded(maximum=4096)
    assert b.publish(dict(kind='rest_quote', quote={'price': 100},
                          observed_at='2026-10-15T00:00:01+00:00'))
    assert b.retained_memory_bytes >= deep_size(b._records[0])
