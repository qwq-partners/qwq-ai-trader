"""실제 수집·봉인·판독 경로의 합성 유량 시험. 실시장 처리율 증명은 아니다."""
import asyncio
from copy import deepcopy
from collections import Counter
from datetime import timedelta
import json
from pathlib import Path
import resource
from types import SimpleNamespace

import pytest

from src.analytics import entry_gate_trace, entry_observation
from src.analytics.entry_observation_journal import read_observation_journal
from src.analytics.entry_observation_runtime import ObservationRuntime
from src.analytics.pregate_candidate_readout import build_pregate_candidate_report
from src.data.feeds.quote_subscription import observe_screen_candidates
from test_entry_observation_runtime import load, NOW
from test_first_scan_memory_guard import guarded_study
from test_market_state_capture import advancing_capture_clock, frame, PRICE, BOOK, STATUS, receive
from test_quote_subscription import ack, make_feed


async def collect_volume(tmp_path, monkeypatch, *, fail_memory=False, plan_factory=None, cycles=1205):
    def change(study):
        guarded_study(study)
        s = study['capture']
        study['as_of'] = (NOW + timedelta(minutes=1, seconds=1208)).isoformat()
        s.update(end_at=study['as_of'], admission_end_at=(NOW + timedelta(minutes=6)).isoformat())
        s['channels']['lease_seconds'] = 1210
        s['selection_basis'].update(version='selection-basis-v4', max_candidates=100, max_modifier_stages=24)
        s['entry_gate_trace']['max_candidates'] = 100
        s['record_allocations'] = {'ws_quote': 21000, 'market_state': 19000, 'other': 512}
        s['channels']['market_state_max_records'] = 19000
        s['record_max_bytes'] = {'scan_metadata': 65536, 'ws_quote': 4096, 'market_state': 4096, 'other': 4096}
        s.update(buffer_capacity=40713, queue_capacity=40713, batch_size=100,
                 max_bytes=201 * (65536 + 2048) + 40512 * (4096 + 2048) + 2048, close_timeout_seconds=10)
    plan, study = load(tmp_path, change) if plan_factory is None else plan_factory(tmp_path)
    clock = advancing_capture_clock(monkeypatch)
    clock[0] = NOW
    monkeypatch.setattr(entry_gate_trace, '_now', lambda: clock[0].isoformat())
    feed = make_feed()
    bot = SimpleNamespace(market='kr', dry_run=False, ws_feed=feed)
    runtime = await ObservationRuntime.install(bot, plan, now=lambda: clock[0])
    coordinator = feed._quote_subscription_owner
    coordinator.interval = 0
    engine_prices = []

    async def quote(event):
        entry_observation.capture_quote(runtime.buffer, event)

    async def price(event):
        engine_prices.append(event.symbol)

    feed.on_quote(quote)
    feed.on_market_data(price)
    symbols = [f'{900001 + i:06}' for i in range(7)]
    candidates = [SimpleNamespace(symbol=s, score=90-i) for i, s in enumerate(symbols)]
    try:
        assert await feed.connect()
        await coordinator.set_operational(['800001'], PRICE, BOOK)
        clock[0] = plan.start_at + timedelta(seconds=1)
        scan = entry_observation.capture_scan(runtime.buffer, candidates, 'regular')
        trace = entry_gate_trace.begin_gate_trace(runtime.buffer, scan, candidates)
        observe_screen_candidates(feed, runtime.buffer, scan, candidates)
        trace.note('regime', 'fail', reason='weighted_or_index_floor')
        trace.finish()
        await asyncio.gather(*list(coordinator._tasks))
        for key in list(coordinator._states):
            await coordinator.handle_ack(ack(key), coordinator.generation)
        assert len(coordinator._states) == 17  # 후보15 + 기존 운영2
        if fail_memory:
            runtime.buffer.retained_memory_limit_bytes = runtime.buffer.retained_memory_bytes
        anchor = clock[0] + timedelta(seconds=1)
        cycles = 3 if fail_memory else cycles
        for tick in range(cycles):
            clock[0] = anchor + timedelta(seconds=tick)
            # 파일럿의 약3 BOOK/초·2.85 체결상태/초를 참고한 약3Hz 합성.
            # 장운영 채널은 이벤트형이므로 0/900초의 가상 상태만 별도 주입한다.
            if tick in (0, 900):
                for symbol in symbols[:5]:
                    await receive(feed, coordinator, frame(STATUS, symbol))
            for _ in range(3):
                for symbol in symbols[:5]:
                    await receive(feed, coordinator, frame(PRICE, symbol))
                    await receive(feed, coordinator, frame(BOOK, symbol))
            await receive(feed, coordinator, frame(PRICE, '800001'))
            await receive(feed, coordinator, frame(BOOK, '800001'))
            if tick % 20 == 0:
                await asyncio.sleep(0)
        assert engine_prices == ['800001'] * cycles
        assert not runtime.buffer.llm_admission and not runtime.buffer._llm_symbols
        retained = runtime.buffer.retained_memory_bytes
        counts = Counter(r['kind'] for r in runtime.buffer._records)
        clock[0] = plan.end_at
        await runtime.close()
        assert runtime.result['sealed']
        observed = read_observation_journal(plan.settings['journal_path'], max_bytes=256 * 1024 * 1024,
                                             expected_study_sha256=plan.study_sha256)
        return plan, study, observed, retained, counts, runtime.buffer, clock[0]
    finally:
        clock[0] = plan.end_at
        await runtime.close()
        await feed.disconnect()


@pytest.mark.asyncio
async def test_guarded_pilot_scaled_synthetic_stream_seals_and_reads(tmp_path, monkeypatch):
    plan, study, observed, retained, counts, buffer, now = await collect_volume(tmp_path, monkeypatch)
    assert observed['complete'] and observed['dropped_records'] == 0
    assert counts['ws_quote'] == 16 * 1205 and counts['market_state'] == 15 * 1205 + 10
    assert retained <= 512 * 1024 * 1024 and not buffer.memory_exhausted
    report = build_pregate_candidate_report(plan.study_bytes, observed,
        study_sha256=plan.study_sha256, analysis_as_of=now.isoformat())
    assert len(report['candidates']) == 7
    assert all(r['entry_quote'] and r['markout_quote'] for r in report['candidates'][:5])
    assert all(r['entry_market_state']['verdict'] == r['markout_market_state']['verdict'] == 'normal_continuous'
               for r in report['candidates'][:5])
    assert report['account_return'] is None and not report['production_eligible']
    from src.analytics.pregate_capture_capacity import assess_pregate_capture_resources
    capacity = assess_pregate_capture_resources(study, operational_registrations=2, external_registrations=0)
    assert capacity['checks']['readout_limit']['passed']
    assert capacity['numeric_configuration_passed']
    uniform = deepcopy(study)
    uniform['capture'].pop('record_max_bytes')
    uniform['capture']['max_bytes'] = 40713 * (65536 + 2048) + 2048
    assert not assess_pregate_capture_resources(uniform, operational_registrations=2,
        external_registrations=0)['checks']['readout_limit']['passed']
    assert not capacity['installation_ready']
    # 종류별 직렬화 한도까지 강제해야 판독 최대 크기 계약을 통과한다.
    print('SYNTHETIC_VOLUME_RECEIPT=' + json.dumps({
        'cycles': 1205, 'candidate_symbols': 5, 'candidate_channels': 15, 'operational_channels': 2,
        'frames': 32 * 1205 + 10, 'records': len(observed['records']), 'counts': dict(counts),
        'retained_record_bytes': retained, 'journal_bytes': Path(plan.settings['journal_path']).stat().st_size,
        'configured_journal_max_bytes': plan.settings['max_bytes'], 'reader_limit_bytes': 256 * 1024 * 1024,
        'process_peak_rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        'live_volume_qualified': False, 'installation_ready': False}, sort_keys=True))


@pytest.mark.asyncio
async def test_exhausted_observer_preserves_operational_prices_and_seals_incomplete(tmp_path, monkeypatch):
    plan, _, observed, _, counts, buffer, now = await collect_volume(tmp_path, monkeypatch, fail_memory=True)
    assert buffer.memory_exhausted
    assert not observed['complete'] and observed['dropped_records'] > 0
    assert 'retained_memory_limit' in observed['incomplete_reasons']
    assert counts['ws_quote'] == counts['market_state'] == 0
    with pytest.raises(ValueError, match='sealed complete undropped'):
        build_pregate_candidate_report(plan.study_bytes, observed,
            study_sha256=plan.study_sha256, analysis_as_of=now.isoformat())
