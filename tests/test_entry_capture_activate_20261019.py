"""Fixed Monday candidate; synthetic readiness and temporary transaction fixtures."""
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest
import test_entry_capture_activate as historical
from test_entry_capture_activate import module, setup
from test_entry_capture_prepare_20261019 import m as installer_fixture, payloads

PROFILE = '20261019-llm-window-v1'
EPOCH = '2026-10-19-engine-llm-window-v1'
NOW = datetime(2026,10,19,0,25,tzinfo=timezone.utc)


def projection():
    return dict(scan_scope='window',capture_closed=False,llm=dict(
        version='runner-llm-window-v1',quote_admission='llm_check_started',
        llm_hook_bound=True,rest_market_state_evidence=True,market_state_evidence=True,
        market_state_max_records=50000,operational_initialized=True,
        start_at='2026-10-19T00:30:00+00:00',admission_end_at='2026-10-19T00:40:00+00:00',
        end_at='2026-10-19T01:00:00+00:00',max_scans=4,
        selection_basis_version='selection-basis-v3',selection_max_modifier_stages=24,
        selection_max_candidates=100,gate_max_candidates=100,max_candidates=5,
        registration_cap=41,external_reserved=0,operational_headroom=10,
        required_new_channels=15,buffer_capacity=110000,queue_capacity=110000,batch_size=64,
        max_record_bytes=65536,max_bytes=7434242048,dropped_records=0,
        retained_memory_limit_bytes=512*1024*1024,retained_memory_bytes=0,memory_exhausted=False,
        record_allocations=dict(ws_quote=50000,market_state=50000,other=8000,scan_metadata=804),
        incomplete_reasons=[],lease_seconds=1800,operational_demand=16,generation=0,
        connected=False,connection_pristine=True,operational_stream_ready=False))


def test_new_profile_date_and_mode_fixed():
    p = module().activation_profile(PROFILE)
    assert p.not_before == NOW and p.latest_start_at == NOW.replace(minute=27)
    assert p.engine_only and p.allow_predeployed and p.evaluation_epoch == EPOCH


def test_candidate_requires_precise_v3_readiness():
    m = module(); cfg = SimpleNamespace(engine_only=True,evaluation_epoch=EPOCH)
    assert not m.capture_prepared(cfg,{'scan_scope':'window'})
    assert m.capture_prepared(cfg,projection())


@pytest.mark.parametrize('key,value',[
    ('selection_basis_version',None),('selection_basis_version','selection-basis-v2'),
    ('selection_max_modifier_stages',None),('selection_max_modifier_stages',23),
    ('selection_max_modifier_stages',24.0),('selection_max_candidates',99),
    ('start_at','2026-10-16T00:30:00+00:00'),('start_at','2026-10-19T00:29:59+00:00'),
    ('admission_end_at','2026-10-19T00:41:00+00:00'),('end_at','2026-10-19T00:56:00+00:00'),
    ('lease_seconds',1560),('lease_seconds',True),('retained_memory_limit_bytes',512*1024*1024+1),
    ('retained_memory_bytes',-1),('retained_memory_bytes',True),('memory_exhausted',True),
    ('operational_demand',17),('llm_hook_bound',False),('incomplete_reasons',['gap']),
])
def test_candidate_rejects_wrong_readiness(key,value):
    data = projection(); data['llm'][key] = value
    assert not module().capture_prepared(SimpleNamespace(engine_only=True,evaluation_epoch=EPOCH),data)


@pytest.mark.parametrize('setup',[PROFILE],indirect=True)
@pytest.mark.parametrize('defect',[None,'deployment','time','study','old_profile','extra_input'])
def test_new_config_pins_sha_window_inputs(setup,monkeypatch,defect):
    s=setup; installer=installer_fixture.__wrapped__(); _,files=payloads(installer)
    raw=installer.document(files['activation'])
    if defect=='deployment':raw['old_head']='f'*40
    elif defect=='time':raw['latest_start_at']='2026-10-19T00:28:00+00:00'
    elif defect=='study':raw['study_sha256']='f'*64
    elif defect=='old_profile':raw['evaluation_epoch']='2026-10-16-engine-llm-window-v1'
    elif defect=='extra_input':raw['input_hashes']['/etc/qwq-toss-observer/plan.json']='f'*64
    monkeypatch.setattr(s.m.os,'geteuid',lambda:0)
    monkeypatch.setattr(s.m,'trusted_dir',lambda *a:None)
    monkeypatch.setattr(s.m,'read_file',lambda *a,**k:json.dumps(raw).encode())
    if defect:
        with pytest.raises(s.m.GuardError):s.m.load_config(PROFILE)
    else:
        cfg=s.m.load_config(PROFILE)
        assert cfg.engine_only and cfg.old_head==cfg.new_head


@pytest.mark.parametrize('setup',[PROFILE],indirect=True)
def test_candidate_rejects_deployment_before_side_effect(setup):
    assert setup.run()['status']=='rejected'
    assert not setup.events and not (setup.state/'activation.receipt').exists()


@pytest.mark.parametrize('setup',[PROFILE],indirect=True)
@pytest.mark.parametrize('instant',['2026-10-19T00:24:59+00:00','2026-10-19T00:27:00+00:00',
    '2026-10-19T09:24:59+09:00','2026-10-19T09:27:00+09:00','2026-10-16T00:25:00+00:00'])
def test_outside_window_never_consumes_receipt(setup,instant):
    object.__setattr__(setup.cfg,'old_head',setup.cfg.new_head)
    setup.current[0]=datetime.fromisoformat(instant)
    assert setup.run()['status']=='rejected'
    assert not setup.events and not (setup.state/'activation.receipt').exists()


@pytest.mark.parametrize('setup',[PROFILE],indirect=True)
def test_same_sha_transaction_restart_once_no_checkout(setup):
    setup.anchors.update(projection())
    historical.test_predeployed_capture_restarts_once_without_checkout(setup)


@pytest.mark.parametrize('setup',[PROFILE],indirect=True)
@pytest.mark.parametrize('problem',['head','source','pending','kill','input'])
def test_preflight_guards_before_restart(setup,problem):
    setup.anchors.update(projection())
    historical.test_predeployed_checks_current_code_and_all_preflight_gates(setup,problem)


@pytest.mark.parametrize('setup',[PROFILE],indirect=True)
def test_expiry_rolls_back_only_owned_dropin(setup):
    historical.test_predeployed_expiry_removes_own_dropin_without_checkout(setup)


@pytest.mark.parametrize('setup',[PROFILE],indirect=True)
@pytest.mark.parametrize('replacement',[False,True])
def test_partial_write_rollback_respects_inode(setup,monkeypatch,replacement):
    historical.test_predeployed_partial_dropin_cleanup_respects_inode(setup,monkeypatch,replacement)


@pytest.mark.parametrize('setup',[PROFILE],indirect=True)
def test_restart_failure_never_retries_or_rolls_back(setup):
    historical.test_predeployed_restart_failure_never_rolls_back_or_retries(setup)


@pytest.mark.parametrize('argv,accepted',[(['--profile',PROFILE],True),
    (['--profile','20261017-llm-window-v1'],False),(['--profile',PROFILE,'--date','2026-10-20'],False)])
def test_explicit_cli_profile_only(monkeypatch,capsys,argv,accepted):
    m=module(); selected=[]
    monkeypatch.setattr(m.sys,'argv',[m.__file__,*argv])
    monkeypatch.setattr(m,'load_config',lambda profile:selected.append(profile) or profile)
    monkeypatch.setattr(m,'activate',lambda cfg:{'status':'complete'})
    assert m.main()==(0 if accepted else 1)
    assert json.loads(capsys.readouterr().out)['status']==('complete' if accepted else 'rejected')
    assert selected==([PROFILE] if accepted else [])


@pytest.mark.asyncio
async def test_real_runtime_projection_satisfies_candidate_without_metadata_injection(tmp_path):
    from src.analytics.entry_observation_runtime import CapturePlan, ObservationRuntime
    from src.analytics.entry_observation_startup import claim_once_manifest
    from src.dashboard.kr_api import KRAPIHandler
    from test_entry_observation_readiness import request
    from test_market_state_pilot_runtime import bot_with
    installer=installer_fixture.__wrapped__()
    installer.ENGINE_STATE=tmp_path/'state'
    installer.ENGINE_STATE.mkdir(mode=0o700)
    installer.NEW={**installer.NEW,**{key:tmp_path/name for key,name in
        (('study','study.json'),('engine_manifest','manifest.json'),('once','once.json'))}}
    manifest,files=payloads(installer)
    installer.validate_payload(manifest,files)
    for key in ('study','engine_manifest','once'):
        installer.NEW[key].write_bytes(files[key]); installer.NEW[key].chmod(0o600)
    selected=claim_once_manifest(installer.NEW['once'],now=NOW)
    assert selected==str(installer.NEW['engine_manifest'])
    assert claim_once_manifest(installer.NEW['once'],now=NOW) is None
    plan=CapturePlan.load(selected,now=NOW)
    bot=bot_with([f'{100000+i}' for i in range(8)])
    bot.engine=SimpleNamespace(risk_manager=SimpleNamespace(_entry_observation_owner=bot))
    runtime=await ObservationRuntime.install(bot,plan,now=lambda:NOW)
    try:
        response=await KRAPIHandler(SimpleNamespace(bot=bot)).get_entry_observation_readiness(request())
        assert response.status==200
        data=json.loads(response.text)
        assert data['evaluation_epoch']==EPOCH and data['study_sha256']==plan.study_sha256
        assert module().capture_prepared(SimpleNamespace(engine_only=True,evaluation_epoch=EPOCH),data)
        assert data['llm']['selection_basis_version']=='selection-basis-v3'
        assert data['llm']['selection_max_modifier_stages']==24
        assert not bot.ws_feed._quote_subscription_owner._leases
    finally:
        await runtime.close('test_finished')


@pytest.mark.parametrize('setup',[PROFILE],indirect=True)
def test_existing_deploy_lock_excludes_new_activation(setup):
    object.__setattr__(setup.cfg,'old_head',setup.cfg.new_head)
    historical.test_existing_deploy_lock_excludes_activation(setup)


@pytest.mark.parametrize('setup',[PROFILE],indirect=True)
@pytest.mark.parametrize('field',['evaluation_epoch','study_sha256'])
def test_wrong_runtime_identity_consumes_only_one_attempt(setup,field):
    setup.anchors.update(projection())
    setup.anchors[field]='wrong-identity'
    setup.source.write_bytes(b'new\n')
    result=historical.predeployed_run(setup)
    assert result['status']=='failed' and result['restart_attempted'] is True
    assert sum('restart' in event for event in setup.events)==1
    count=len(setup.events)
    assert historical.predeployed_run(setup)['status']=='rejected'
    assert len(setup.events)==count
