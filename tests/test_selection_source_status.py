"""실제 호출 경계와 유한 상태 기록을 합성 입력으로 검증한다."""
import asyncio
from copy import deepcopy

import pytest


def api():
    from src.analytics import selection_source_status as m
    return m


def capture(events, result, source='kis_volume_surge'):
    m = api(); c = m.SourceCapture()
    async def fetch():
        for event, kwargs in events:
            m.mark_source(event, **kwargs)
        return result
    actual = asyncio.run(c.call(source, fetch))
    wrapped = c.wrap(actual)
    row = next(r for r in wrapped.selection_sources['runs'] if r['source_id'] == source)
    return actual, wrapped, row


@pytest.mark.parametrize('events,result,outcome', [
    ([], [], 'unknown_empty'), ([], [1], 'unknown_nonempty'),
    ([('payload', {'count': 0})], [], 'completed_empty'),
    ([('payload', {'count': 4})], [], 'completed_empty'),
    ([('payload', {'count': 4})], [1], 'completed_nonempty'),
    ([('error', {'reason': 'http'})], [], 'failed'),
    ([('payload', {'count': 4}), ('error', {'reason': 'parse'})], [1], 'partial'),
    ([('cache', {'age_seconds': 42})], [], 'cached_empty'),
    ([('cache', {'age_seconds': 42})], [1], 'cached_nonempty'),
])
def test_results_keep_empty_unknown_failure_partial_and_cache_distinct(events, result, outcome):
    actual, wrapped, row = capture(events, result)
    assert actual is result and list(wrapped) == result
    assert row['outcome'] == outcome and row['source_as_of'] is None
    assert row['started_at'] <= row['completed_at']
    assert len(wrapped.selection_sources['runs']) == 11
    assert sum(r['outcome'] == 'not_called' for r in wrapped.selection_sources['runs']) == 10


def test_two_market_input_requires_two_payloads_and_keeps_failure():
    _, _, one = capture([('payload', {'count': 1})], [1], 'kis_institutional_buying')
    assert one['outcome'] == 'unknown_nonempty'
    _, _, two = capture([('payload', {'count': 1}), ('payload', {'count': 0})], [1], 'kis_institutional_buying')
    assert two['outcome'] == 'completed_nonempty'
    _, _, partial = capture([('payload', {'count': 1}), ('error', {'reason': 'api'})], [1], 'kis_institutional_buying')
    assert partial['outcome'] == 'partial'


@pytest.mark.asyncio
async def test_concurrent_sources_and_scans_do_not_share_context():
    m = api(); left, right = m.SourceCapture(), m.SourceCapture()
    async def fetch(count):
        m.mark_source('payload', count=count)
        await asyncio.sleep(0)
        return list(range(count))
    results = await asyncio.gather(left.call('kis_volume_surge', fetch, 1),
        left.call('kis_new_highs', fetch, 2), right.call('kis_volume_surge', fetch, 3))
    a, b = left.wrap([]).selection_sources, right.wrap([]).selection_sources
    assert a['capture_id'] != b['capture_id']
    assert [r['raw_count'] for r in a['runs'] if r['outcome'] != 'not_called'] == [1, 2]
    assert next(r for r in b['runs'] if r['source_id'] == 'kis_volume_surge')['raw_count'] == 3
    assert results == [[0], [0, 1], [0, 1, 2]]
    m.mark_source('error', reason='http')  # outside a capture is a no-op
    assert left.wrap([]).selection_sources['runs'] == a['runs']


@pytest.mark.asyncio
async def test_raised_error_and_cancellation_propagate_and_clear_context():
    m = api(); c = m.SourceCapture()
    async def fail(): raise RuntimeError('must not serialize this detail')
    with pytest.raises(RuntimeError): await c.call('kis_volume_surge', fail)
    assert 'must not serialize' not in str(c.wrap([]).selection_sources)
    async def cancel(): raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError): await c.call('kis_new_highs', cancel)
    m.mark_source('payload', count=999)
    assert all(r['raw_count'] != 999 for r in c.wrap([]).selection_sources['runs'])


def test_snapshot_is_owned_bounded_and_validated():
    _, wrapped, row = capture([('payload', {'count': 2})], [1])
    m = api(); snap = wrapped.selection_sources
    assert m.validate_snapshot(snap, observed_at=snap['completed_at']) == snap
    for field, value in [('raw_count', -1), ('outcome', 'fresh_forever'), ('source_as_of', 'made-up')]:
        bad = deepcopy(snap); bad['runs'][0][field] = value
        with pytest.raises(ValueError): m.validate_snapshot(bad, observed_at=snap['completed_at'])
    bad = deepcopy(snap); bad['runs'].append(deepcopy(bad['runs'][0]))
    with pytest.raises(ValueError): m.validate_snapshot(bad, observed_at=snap['completed_at'])


def test_fallback_snapshot_describes_current_attempt_not_old_candidates():
    _, wrapped, _ = capture([('error', {'reason': 'http'})], [])
    m = api(); c = m.SourceCapture()
    old = [object()]
    fallback = c.wrap(old, fallback=True)
    assert fallback[0] is old[0] and fallback.selection_sources['fallback_used'] is True
    assert all(r['outcome'] == 'not_called' for r in fallback.selection_sources['runs'])


@pytest.mark.asyncio
async def test_event_overflow_preserves_business_result():
    m = api(); c = m.SourceCapture(); original = [1]
    async def noisy():
        for _ in range(17): m.mark_source('cache', age_seconds=1)
        return original
    assert await c.call('kis_volume_surge', noisy) is original
    assert c.wrap(original) is original  # unavailable, never silently truncated success


@pytest.mark.asyncio
async def test_recorder_failure_preserves_business_exception(monkeypatch):
    m = api(); c = m.SourceCapture()
    def broken(*args): raise ValueError('observer failed')
    monkeypatch.setattr(c, '_finish', broken)
    async def error(): raise RuntimeError('business failure')
    with pytest.raises(RuntimeError, match='business failure'):
        await c.call('kis_new_highs', error)


@pytest.mark.asyncio
async def test_large_aggregate_and_snapshot_validator_failure_are_unavailable(monkeypatch):
    m = api(); c = m.SourceCapture(); original = [1]
    async def fetch():
        m.mark_source('payload', count=1000000)
        m.mark_source('payload', count=1000000)
        return original
    assert await c.call('kis_institutional_buying', fetch) is original
    assert c.wrap(original) is original
    c = m.SourceCapture()
    def broken(*args, **kwargs): raise ValueError('synthetic invalid snapshot')
    monkeypatch.setattr(m, 'validate_snapshot', broken)
    assert c.wrap(original) is original
