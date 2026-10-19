"""Guarded first-scan readiness exposes local runtime state only."""
import json
from types import SimpleNamespace

import pytest

from src.analytics.entry_observation_runtime import ObservationRuntime
from src.dashboard.kr_api import KRAPIHandler
from test_entry_observation_readiness import request
from test_entry_observation_runtime import NOW, load, owner
from test_first_scan_memory_guard import guarded_study


async def installed(tmp_path, change=guarded_study):
    plan, _ = load(tmp_path, change)
    bot = owner()
    bot.engine = SimpleNamespace(risk_manager=SimpleNamespace(_entry_observation_owner=bot))
    return await ObservationRuntime.install(bot, plan, now=lambda: NOW), bot


@pytest.mark.asyncio
async def test_guarded_first_scan_projects_actual_local_resources(tmp_path):
    runtime, bot = await installed(tmp_path)
    try:
        response = await KRAPIHandler(SimpleNamespace(bot=bot)).get_entry_observation_readiness(request())
        assert response.status == 200
        data = json.loads(response.text)
        assert 'llm' not in data
        assert data['first_scan'] == {
            'version': 'runner-first-scan-v4', 'quote_admission': 'first_scan',
            'observer_bound': True, 'scan_scope': 'first', 'max_scans': 1,
            'rest_market_state_evidence': False, 'market_state_evidence': True,
            'market_state_max_records': 100,
            'start_at': '2026-10-01T01:00:00+00:00',
            'admission_end_at': '2026-10-01T01:01:00+00:00',
            'end_at': '2026-10-01T01:30:00+00:00', 'record_allocations': {
                'scan_metadata': 21, 'ws_quote': 100, 'market_state': 100, 'other': 100},
            'record_max_bytes': None, 'record_counts': {
                'scan_metadata': 0, 'ws_quote': 0, 'market_state': 0, 'other': 0}, 'buffer_capacity': 1000,
            'queue_capacity': 1000, 'batch_size': 10, 'max_record_bytes': 65536,
            'max_bytes': 67586048, 'retained_memory_limit_bytes': 512 * 1024 * 1024,
            'retained_memory_bytes': 0, 'memory_exhausted': False, 'dropped_records': 0,
            'incomplete_reasons': [], 'selection_basis_version': 'selection-basis-v2',
            'selection_max_candidates': 10, 'gate_max_candidates': 10,
            'max_candidates': 5, 'lease_seconds': 1800, 'registration_cap': 41,
            'external_reserved': 0, 'operational_headroom': 10,
            'required_new_channels': 15, 'operational_demand': 0,
            'operational_initialized': True, 'generation': 0, 'connected': False,
            'connection_pristine': True, 'operational_stream_ready': False,
        }
    finally:
        await runtime.close('test_finished')


@pytest.mark.asyncio
async def test_first_scan_readiness_tracks_binding_closed_and_journal_failure(tmp_path):
    runtime, bot = await installed(tmp_path)
    api = KRAPIHandler(SimpleNamespace(bot=bot))
    try:
        owner_state = bot.ws_feed._quote_subscription_owner
        owner_state.observer = object()
        assert runtime.first_scan_readiness()['observer_bound'] is False
        owner_state.observer = runtime.buffer
        runtime.journal.buffer = object()
        assert runtime.first_scan_readiness()['observer_bound'] is False
        runtime.journal.buffer = runtime.buffer
        runtime.buffer.ended = True
        data = json.loads((await api.get_entry_observation_readiness(request())).text)
        assert data['capture_closed'] is True and data['first_scan']['observer_bound'] is True
        runtime.buffer.ended = False
        runtime.journal._fail('synthetic_writer_failed')
        assert (await api.get_entry_observation_readiness(request())).status == 503
    finally:
        await runtime.close('test_finished')


@pytest.mark.asyncio
async def test_legacy_first_scan_and_llm_projections_do_not_gain_first_scan_key(tmp_path):
    from test_llm_capture_readiness import fixed_llm
    from test_pregate_capture_flow import study_change

    legacy, legacy_bot = await installed(tmp_path, study_change)
    try:
        legacy_data = json.loads((await KRAPIHandler(SimpleNamespace(bot=legacy_bot)).get_entry_observation_readiness(request())).text)
        assert 'first_scan' not in legacy_data and 'llm' not in legacy_data
    finally:
        await legacy.close('test_finished')

    llm_dir = tmp_path / 'llm'
    llm_dir.mkdir()
    llm, llm_bot = await installed(llm_dir, fixed_llm)
    try:
        llm_data = json.loads((await KRAPIHandler(SimpleNamespace(bot=llm_bot)).get_entry_observation_readiness(request())).text)
        assert 'first_scan' not in llm_data
        assert llm_data['llm'] == llm.llm_readiness()
    finally:
        await llm.close('test_finished')
