"""v5 installation bytes and the real runtime must share one selection contract."""
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.analytics.entry_observation_runtime import CapturePlan, ObservationRuntime
from src.analytics.entry_observation_startup import claim_once_manifest
from src.dashboard.kr_api import KRAPIHandler
from test_entry_capture_activate import module as activation_module
from test_entry_capture_prepare_20261019_v5 import m as installer_fixture, payloads
from test_entry_observation_readiness import request
from test_market_state_pilot_runtime import bot_with


@pytest.mark.asyncio
async def test_v5_actual_study_once_runtime_and_activation_contract(tmp_path, monkeypatch):
    installer = installer_fixture.__wrapped__()
    core = installer.core
    monkeypatch.setattr(core, 'ENGINE_STATE', tmp_path/'state')
    core.ENGINE_STATE.mkdir(mode=0o700)
    monkeypatch.setattr(core, 'NEW', {**core.NEW, **{
        key: tmp_path/(key+'.json') for key in ('study', 'engine_manifest', 'once')}})
    doc, files = payloads(core)
    core.validate_payload(doc, files)
    for key in ('study', 'engine_manifest', 'once'):
        core.NEW[key].write_bytes(files[key])
        core.NEW[key].chmod(0o600)
    now = datetime(2026, 10, 19, 0, 25, tzinfo=timezone.utc)
    manifest = claim_once_manifest(core.NEW['once'], now=now)
    plan = CapturePlan.load(manifest, now=now)
    assert plan.context['evaluation_epoch'] == core.EPOCH
    assert plan.settings['selection_basis']['version'] == 'selection-basis-v4'
    assert core.NEW['study'].read_bytes() == files['study']
    bot = bot_with([f'{100000+i}' for i in range(8)])
    bot.engine = SimpleNamespace(risk_manager=SimpleNamespace(_entry_observation_owner=bot))
    runtime = await ObservationRuntime.install(bot, plan, now=lambda: now)
    try:
        response = await KRAPIHandler(SimpleNamespace(bot=bot)).get_entry_observation_readiness(request())
        projection = json.loads(response.text)
        activation = activation_module()
        selected = activation.activation_profile(core.PROFILE)
        cfg = SimpleNamespace(engine_only=True, evaluation_epoch=selected.evaluation_epoch)
        assert activation.capture_prepared(cfg, projection)
        assert projection['llm']['operational_demand'] == 16
        assert projection['llm']['selection_basis_version'] == 'selection-basis-v4'
        assert runtime.buffer.selection_supply_policy_capture_enabled
        assert claim_once_manifest(core.NEW['once'], now=now) is None
        assert core.NEW['study'].read_bytes() == files['study']
    finally:
        await runtime.close('synthetic_finished')


@pytest.mark.asyncio
@pytest.mark.parametrize('include_market_state', [True, False])
async def test_new_selection_schema_survives_nonempty_real_readout(tmp_path, monkeypatch, include_market_state):
    """Exercise the existing four real report CLIs with new policy payloads."""
    import test_llm_capture_reachability as reach
    from test_capture_readout_nonempty import test_nonempty_v3_llm_capture_survives_all_real_readout_reports
    original_study = reach.study
    def new_study(context, **kwargs):
        original_study(context, **kwargs)
        context['capture']['selection_basis'].update(version='selection-basis-v4', max_modifier_stages=24)
    monkeypatch.setattr(reach, 'study', new_study)
    await test_nonempty_v3_llm_capture_survives_all_real_readout_reports(tmp_path, monkeypatch, include_market_state)
    evaluation = json.loads((tmp_path/'private-readout'/'evaluation-unreviewed.json').read_text())
    selection = evaluation['selection_basis']
    assert selection['candidates'][0]['basis']['version'] == 'kr-selection-basis-v3'
    assert selection['supply_policy_evidence']['comparable_spdi_shadows'] == 1
