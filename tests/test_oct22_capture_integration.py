"""Exact Oct. 22 high-volume study; runs the parser, writer, strict reader and observed flow."""
from datetime import datetime, timedelta, timezone
import asyncio
import json
from pathlib import Path
from time import monotonic

import pytest
from src.analytics.entry_observation_runtime import CapturePlan
from src.analytics.pregate_candidate_readout import build_pregate_candidate_report
from src.analytics.pregate_observed_regular_flow import build_observed_regular_flow_report
from test_entry_capture_prepare_20261022 import m as installer_fixture, payloads
import test_first_scan_volume as volume
import test_market_state_capture as market_frames


NOW = datetime(2026, 10, 22, 0, 25, tzinfo=timezone.utc)


def plan_factory(tmp_path):
    installer = installer_fixture.__wrapped__()
    installer.ENGINE_STATE = tmp_path / 'state'
    installer.ENGINE_STATE.mkdir()
    installer.NEW = {**installer.NEW, 'study': tmp_path / 'study.json',
                     'engine_manifest': tmp_path / 'manifest.json'}
    document, files = payloads(installer)
    # Validate the final, temporary-path artifact before the runtime consumes it.
    installer.validate_payload(document, files)
    for key in ('study', 'engine_manifest'):
        installer.NEW[key].write_bytes(files[key])
    study = installer.document(files['study'])
    return CapturePlan.load(installer.NEW['engine_manifest'], now=NOW), study


@pytest.mark.asyncio
async def test_exact_oct22_high_volume_envelope_study_preserves_strict_and_observed_flow(tmp_path, monkeypatch):
    # The generic fixture uses an earlier-date/1263-second study. Bind its raw frame
    # date and its parser clock to the new, exact 09:30--09:51 contract.
    monkeypatch.setattr(volume, 'NOW', NOW)
    original_fields = market_frames.fields
    def oct22_fields(tr, symbol='005930', width=None):
        row = original_fields(tr, symbol, width)
        if tr == market_frames.PRICE and width is None:
            row[33] = NOW.strftime('%Y%m%d')
        return row
    # `frame` resolves fields in its defining module; `price_batch` resolves
    # the separately imported reference in the volume helper.
    monkeypatch.setattr(market_frames, 'fields', oct22_fields)
    monkeypatch.setattr(volume, 'fields', oct22_fields)
    from src.analytics import entry_observation_runtime
    close_receipt = {}
    original_close = entry_observation_runtime.ObservationJournal.close
    async def measured_close(journal, *, timeout_seconds=5.0):
        close_receipt['pending_before'] = journal._queue.qsize()
        close_receipt['persisted_before'] = journal._persisted
        started = monotonic()
        result = await original_close(journal, timeout_seconds=timeout_seconds)
        close_receipt.update(result, duration_seconds=monotonic() - started,
                             timeout_seconds=timeout_seconds,
                             pending_after=journal._queue.qsize(),
                             persisted_after=journal._persisted)
        print('OCT22_CLOSE_RECEIPT=' + json.dumps(close_receipt, sort_keys=True))
        return result
    monkeypatch.setattr(entry_observation_runtime.ObservationJournal, 'close', measured_close)
    # This is a volume/contract test, not a wall-clock throughput benchmark.
    # A 21-minute fake clock must not leave ~64k queued records for the real
    # 10-second close deadline. Pace only the synthetic producer; keep the real
    # writer, fsync, raw burst, logical clock and production limits unchanged.
    pacing = {'checkpoints': 0, 'wait_seconds': 0.0, 'queue_peak': 0}
    async def producer_checkpoint(runtime, tick):
        journal = runtime.journal
        pending = journal._queue.qsize()
        pacing['queue_peak'] = max(pacing['queue_peak'], pending)
        if pending < 1000:
            return
        pacing['checkpoints'] += 1
        started = monotonic()
        while journal._queue.qsize() > 100:
            assert not journal._failed.is_set(), 'writer failed during synthetic pacing'
            assert journal._thread.is_alive(), 'writer stopped during synthetic pacing'
            elapsed = monotonic() - started
            assert elapsed < 5 and pacing['wait_seconds'] + elapsed < 30, 'synthetic writer stalled'
            await asyncio.sleep(0.01)
        elapsed = monotonic() - started
        assert not journal._failed.is_set() and journal._thread.is_alive(), 'synthetic writer failed'
        assert elapsed < 5 and pacing['wait_seconds'] + elapsed < 30, 'synthetic writer stalled'
        pacing['wait_seconds'] += elapsed
    plan, study, observed, retained, counts, buffer, now = await volume.collect_volume(
        tmp_path, monkeypatch, plan_factory=plan_factory, cycles=1257, high_volume=True,
        producer_checkpoint=producer_checkpoint)
    expected_states = 1257 * 50 + (458 - 50) + 10
    expected_books = 1257 * 25 + (114 - 25) + 1257
    assert plan.start_at == NOW + timedelta(minutes=5)
    assert plan.end_at == NOW + timedelta(minutes=26)
    assert study['capture']['journal_budget_version'] == 'bounded-envelope-v1'
    assert observed['complete'] and observed['dropped_records'] == 0
    assert counts['market_state'] == expected_states and counts['ws_quote'] == expected_books
    assert buffer._record_counts['scan_metadata'] <= 201
    assert buffer._record_counts['market_state'] == expected_states
    assert buffer._record_counts['ws_quote'] == expected_books
    assert retained <= 512 * 1024 * 1024
    assert Path(plan.settings['journal_path']).stat().st_size <= 256 * 1024 * 1024
    strict = build_pregate_candidate_report(plan.study_bytes, observed,
        study_sha256=plan.study_sha256, analysis_as_of=now.isoformat())
    observed_flow = build_observed_regular_flow_report(plan.study_bytes, observed,
        study_sha256=plan.study_sha256, analysis_as_of=now.isoformat())
    assert len(strict['candidates']) == 7
    assert observed_flow['strict_report'] == strict
    assert observed_flow['counts'] == {'denominator': 7, 'eligible_pairs': 5}
    assert close_receipt['sealed'] and close_receipt['pending_after'] == 0
    assert close_receipt['persisted_after'] == len(observed['records'])
    print('OCT22_SYNTHETIC_VOLUME_RECEIPT=' + json.dumps({
        'records': len(observed['records']), 'counts': dict(counts),
        'retained_record_bytes': retained,
        'journal_bytes': Path(plan.settings['journal_path']).stat().st_size,
        'eligible_pairs': observed_flow['counts']['eligible_pairs'],
        'live_volume_qualified': False,
        'synthetic_producer_pacing': pacing,
        'close': close_receipt,
    }, sort_keys=True))
