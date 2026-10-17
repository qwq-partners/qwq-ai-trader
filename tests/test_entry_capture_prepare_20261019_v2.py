"""Replacement Monday capture: synthetic inputs only, never host operations."""
import importlib.util
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_entry_capture_prepare_20261019 as v1
from test_entry_capture_activate import module as activation_module
from test_entry_capture_activate_20261019 import projection

SCRIPT = Path(__file__).parents[1] / 'scripts/ops/entry_capture_prepare_20261019_v2.py'

@pytest.fixture
def m():
    assert SCRIPT.exists(), 'separate v2 installer required'
    spec = importlib.util.spec_from_file_location('prepare_oct19_v2', SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_v2_paths_and_payload_do_not_reuse_v1(m):
    c = m.core
    old = v1.m.__wrapped__()
    assert c.PROFILE == '20261019-llm-window-v2'
    assert c.EPOCH == '2026-10-19-engine-llm-window-v2'
    assert c.NOT_BEFORE == old.NOT_BEFORE
    assert not set(c.NEW.values()) & set(old.NEW.values())
    assert all(getattr(c,k) != getattr(old,k) for k in ('INSTALL','BACKUP','INPUT','ENGINE_STATE','STATE','ACT_SERVICE','ACT_TIMER'))
    assert set(c.OLD) == set(m.OLD_OWNERS)
    assert not set(m.OLD_ABSENT) & set(c.OLD)
    doc, files = v1.payloads(c)
    c.validate_manifest(doc)
    c.validate_payload(doc,files)
    assert b'activate-20261019-v2.py --profile 20261019-llm-window-v2' in files['activation_service']
    assert b'OnCalendar=2026-10-19 09:25:00 Asia/Seoul' in files['activation_timer']
    assert m.INSTALL_DEADLINE == datetime(2026,10,19,0,20,tzinfo=timezone.utc)


@pytest.mark.parametrize('field,value', [('engine_head','main'), ('engine_head','a'*39),
    ('profile','20261019-llm-window-v1'), ('protected_hashes',{}),('old_files',{})])
def test_v2_manifest_requires_fixed_sha_and_exact_inventory(m,field,value):
    doc,_ = v1.payloads(m.core)
    doc[field] = value
    with pytest.raises(m.core.GuardError): m.core.validate_manifest(doc)


@pytest.mark.parametrize('key', ['study','executor','once','activation','engine_manifest'])
def test_v2_corrupt_payload_pin_rejected(m,key):
    c=m.core; doc,files=v1.payloads(c); files[key]+=b' '
    with pytest.raises(c.GuardError,match='payload_pin_mismatch'): c.validate_payload(doc,files)


@pytest.mark.parametrize('artifact,changes', [('once',{'receipt_path':'/home/ubuntu/.local/state/qwq-entry-observation/20261019-llm-window-v1/startup.receipt'}),
    ('activation',{'new_head':'b'*40}), ('study',{'evaluation_epoch':'2026-10-19-engine-llm-window-v1'})])
def test_v2_rehashed_old_identity_or_changed_head_rejected(m,artifact,changes):
    c=m.core; doc,files=v1.payloads(c)
    value=c.document(files[artifact]); value.update(changes); files[artifact]=c.raw(value)
    doc['new_files'][artifact]=c.sha(files[artifact])
    with pytest.raises(c.GuardError): c.validate_payload(doc,files)


@pytest.mark.parametrize('timer,enabled,service', [('active','disabled','inactive'),
    ('inactive','enabled','inactive'),('inactive','disabled','activating'),
    ('inactive','disabled','active'),('inactive','disabled','failed'),('','disabled','inactive')])
def test_retired_v1_must_be_disabled_and_inactive(m,monkeypatch,timer,enabled,service):
    monkeypatch.setattr(m,'require_absent',lambda *a:None)
    class Host:
        def show(self,unit,prop):
            if unit==m.OLD_TIMER:return enabled if prop=='UnitFileState' else timer
            return service
    with pytest.raises(m.core.GuardError,match='retired_v1_not_quiescent'):m.retirement_guards(Host())


def test_retirement_checks_exact_old_attempts_and_shared_dropin(m,monkeypatch):
    seen=[]
    monkeypatch.setattr(m,'require_absent',lambda p,owner:seen.append((p,owner)))
    class Host:
        def show(self,unit,prop): return 'disabled' if prop=='UnitFileState' else 'inactive'
    m.retirement_guards(Host())
    assert dict(seen)==m.OLD_ABSENT
    assert {p.name for p in m.OLD_ABSENT}=={'activation.receipt','status.json','startup.receipt','engine.jsonl','entry-capture.conf'}
    assert all('20261019-llm-window-v1' in str(p) for p in m.OLD_ABSENT if p!=m.core.DROPIN)


@pytest.mark.parametrize('kind',['file','symlink','dangling'])
def test_absence_rejects_existing_and_symlink_attempts(m,tmp_path,monkeypatch,kind):
    import os
    p=tmp_path/'receipt'
    if kind=='file':p.write_bytes(b'consumed')
    else:p.symlink_to(tmp_path/('receipt' if kind=='symlink' else 'missing'))
    monkeypatch.setattr(m.core,'open_dir',lambda path,owner:os.open(path,os.O_RDONLY))
    with pytest.raises(m.core.GuardError,match='retired_v1_attempt_or_dropin_exists'):m.require_absent(p,0)


def test_v2_profile_and_strict_readiness():
    a=activation_module(); p=a.activation_profile('20261019-llm-window-v2')
    assert p.engine_only and p.allow_predeployed
    assert p.evaluation_epoch=='2026-10-19-engine-llm-window-v2'
    cfg=SimpleNamespace(engine_only=True,evaluation_epoch=p.evaluation_epoch)
    assert a.capture_prepared(cfg,projection())
    value=projection();value['llm']['selection_max_modifier_stages']=23
    assert not a.capture_prepared(cfg,value)
    with pytest.raises(a.GuardError):a.activation_profile('20261019-llm-window-v4')


def test_private_core_is_hash_pinned_and_does_not_mutate_v1(m,monkeypatch):
    before=v1.m.__wrapped__()
    assert before.PROFILE=='20261019-llm-window-v1'
    assert m.core.PROFILE=='20261019-llm-window-v2'
    monkeypatch.setattr(m,'CORE_SHA256','0'*64)
    with pytest.raises(RuntimeError,match='preparation_core_pin_mismatch'):m._load_core()
    assert v1.m.__wrapped__().PROFILE==before.PROFILE


@pytest.mark.parametrize('instant',['2026-10-19T00:20:00+00:00','2026-10-19T09:25:00+09:00'])
def test_v2_deadline_precedes_all_host_and_file_reads(m,monkeypatch,instant):
    class Clock:
        @classmethod
        def now(cls,tz):return datetime.fromisoformat(instant)
    monkeypatch.setattr(m,'datetime',Clock)
    monkeypatch.setattr(m.core,'load_manifest',lambda _:pytest.fail('manifest read after deadline'))
    with pytest.raises(m.core.GuardError,match='installation_deadline_passed'):m.prepare('a'*64)


@pytest.mark.parametrize('defect',[None,'pin','known_manifest','installed','staged'])
def test_retired_inputs_are_exact_preserved_v1_package(m,monkeypatch,defect):
    c=m.core
    originals={p:('old:'+str(p)).encode() for p in m.OLD_OWNERS}
    old_manifest={'new_files':{key:c.sha(originals[path]) for key,path in m.OLD_NEW.items()}}
    for key,path in m.OLD_NEW.items(): originals[m.OLD_INSTALL/'payload'/key]=originals[path]
    originals[m.OLD_INSTALL/'manifest.json']=c.raw(old_manifest)
    monkeypatch.setattr(m,'OLD_MANIFEST_SHA256',c.sha(originals[m.OLD_INSTALL/'manifest.json']))
    if defect=='installed':originals[next(iter(m.OLD_NEW.values()))]+=b'x'
    if defect=='staged':originals[m.OLD_INSTALL/'payload'/'executor']+=b'x'
    doc={'old_files':{str(p):c.sha(data) for p,data in originals.items()}}
    if defect=='pin':doc['old_files'][str(next(iter(originals))) ]='0'*64
    if defect=='known_manifest':monkeypatch.setattr(m,'OLD_MANIFEST_SHA256','0'*64)
    seen=[]
    def read(p,*,owner):
        seen.append((p,owner));return originals[p]
    monkeypatch.setattr(c,'trusted_read',read)
    if defect:
        with pytest.raises(c.GuardError):m.old_inputs(doc)
    else:
        assert m.old_inputs(doc)==originals
        assert dict(seen)==m.OLD_OWNERS


@pytest.mark.parametrize('defect',[None,'timer_active','timer_enabled','service_active','receipt','journal'])
def test_activation_v2_retirement_guard(a_retirement,defect):
    a,paths,command,states=a_retirement
    if defect=='timer_active':states['ActiveState']='active'
    if defect=='timer_enabled':states['UnitFileState']='enabled'
    if defect=='service_active':states['service']='active'
    if defect in ('receipt','journal'):paths[0 if defect=='receipt' else 3][0].write_bytes(b'consumed')
    if defect:
        with pytest.raises(a.GuardError):a.retired_oct19_v1_quiescent(command)
    else:a.retired_oct19_v1_quiescent(command)


@pytest.fixture
def a_retirement(tmp_path,monkeypatch):
    a=activation_module()
    paths=tuple((tmp_path/name,1000 if index>1 else 0) for index,name in
                enumerate(('activation.receipt','status.json','startup.receipt','engine.jsonl')))
    monkeypatch.setattr(a,'RETIRED_OCT19_ATTEMPTS',paths,raising=False)
    monkeypatch.setattr(a,'trusted_dir',lambda *a:None)
    states={'ActiveState':'inactive','UnitFileState':'disabled','service':'inactive'}
    def command(argv):
        assert argv[:2]==['/usr/bin/systemctl','show']
        return states['service'] if argv[-1].endswith('.service') else states[argv[2].split('=')[1]]
    return a,paths,command,states


@pytest.mark.parametrize('failure',[None,'check','static','enable','post_enable','backup_write',
    'disk_before','disk_locked','disk_arm','disarm','old_consumed','old_rearmed','old_changed'])
def test_v2_transaction_preserves_v1_and_never_reuses_attempt(m,tmp_path,monkeypatch,failure):
    import os
    c=m.core
    for name in ('INSTALL','BACKUP','INPUT','ENGINE_STATE','STATE','DROPIN','LOCK'):
        monkeypatch.setattr(c,name,tmp_path/name)
    monkeypatch.setattr(c,'NEW',{key:tmp_path/('new-'+key) for key in c.NEW})
    old=tmp_path/'old';old.mkdir()
    monkeypatch.setattr(m,'OLD_NEW',{key:old/key for key in m.OLD_NEW})
    monkeypatch.setattr(m,'OLD_INSTALL',old/'install');m.OLD_INSTALL.mkdir()
    (m.OLD_INSTALL/'payload').mkdir()
    owners={p:0 for p in m.OLD_NEW.values()}
    owners.update({m.OLD_INSTALL/'payload'/key:0 for key in m.OLD_NEW})
    owners[m.OLD_INSTALL/'manifest.json']=0
    monkeypatch.setattr(m,'OLD_OWNERS',owners)
    originals={p:('old-'+p.name).encode() for p in owners}
    for key,path in m.OLD_NEW.items(): originals[m.OLD_INSTALL/'payload'/key]=originals[path]
    oldmanifest={'new_files':{key:c.sha(originals[path]) for key,path in m.OLD_NEW.items()}}
    originals[m.OLD_INSTALL/'manifest.json']=c.raw(oldmanifest)
    monkeypatch.setattr(m,'OLD_MANIFEST_SHA256',c.sha(originals[m.OLD_INSTALL/'manifest.json']))
    for p,data in originals.items():p.write_bytes(data)
    old_receipt=old/'activation.receipt'
    monkeypatch.setattr(m,'OLD_ABSENT',{old_receipt:0,c.DROPIN:0})
    c.INSTALL.mkdir();(c.INSTALL/'payload').mkdir()
    files={key:('new-'+key).encode() for key in c.NEW}
    for key,data in files.items():(c.INSTALL/'payload'/key).write_bytes(data)
    c.LOCK.write_bytes(b'');c.LOCK.chmod(0o644)
    doc={'old_files':{str(p):c.sha(data) for p,data in originals.items()}}
    monkeypatch.setattr(c,'load_manifest',lambda _:doc)
    monkeypatch.setattr(c,'trusted_read',lambda p,**kw:Path(p).read_bytes())
    monkeypatch.setattr(c,'validate_payload',lambda *_:None)
    monkeypatch.setattr(c,'runtime_guards',lambda *a,**k:None)
    monkeypatch.setattr(c,'mkdir_new',lambda p,owner=0:p.mkdir(mode=0o700))
    monkeypatch.setattr(c,'open_dir',lambda p,owner=0:os.open(p,os.O_RDONLY))
    original_write=c.write_new
    def write(p,data,mode=0o600,owner=0):
        if failure=='backup_write' and p.name=='attempt.json':raise OSError('injected')
        original_write(p,data,mode,os.geteuid())
    monkeypatch.setattr(c,'write_new',write)
    real_fstat=os.fstat
    class LockStat:st_mode=0o100644;st_uid=1000;st_nlink=1
    monkeypatch.setattr(os,'fstat',lambda fd:LockStat() if os.readlink('/proc/self/fd/'+str(fd))==str(c.LOCK) else real_fstat(fd))
    class Host:
        enabled=False;old_enabled=False;disk_checks=0
        def __init__(self):self.commands=[]
        def show(self,unit,prop):
            if unit in (m.OLD_TIMER,m.OLD_SERVICE):
                return ('enabled' if self.old_enabled else 'disabled') if prop=='UnitFileState' else 'inactive'
            if prop=='ActiveState':return 'active' if self.enabled and unit==c.ACT_TIMER else 'inactive'
            if prop=='UnitFileState':return 'enabled' if self.enabled else 'disabled'
            if prop=='Unit':return c.ACT_SERVICE
            if prop=='FragmentPath':return str(c.NEW['activation_timer' if unit==c.ACT_TIMER else 'activation_service'])
            if prop=='DropInPaths':return ''
            if prop=='NextElapseUSecRealtime':return 'Mon 2026-10-19 09:25:00 KST'
            raise AssertionError((unit,prop))
        def disk_available(self,path):
            self.disk_checks+=1
            return 0 if self.disk_checks=={'disk_before':1,'disk_locked':2,'disk_arm':3}.get(failure) else c.MIN_DISK_AVAILABLE_BYTES
        def static(self):
            if failure=='static':raise c.GuardError('injected')
        def run(self,args):
            self.commands.append(args)
            if 'enable' in args:
                self.enabled=True
                if failure in ('enable','disarm'):raise OSError('enable failed')
                if failure=='old_consumed':old_receipt.write_bytes(b'consumed')
                if failure=='old_rearmed':self.old_enabled=True
                if failure=='old_changed':next(iter(originals)).write_bytes(b'changed')
            if 'disable' in args:
                if failure=='disarm':raise OSError('disable failed')
                self.enabled=False
    host=Host()
    original_runtime=m.runtime_guards
    def runtime(*a,**kw):
        original_runtime(*a,**kw)
        if failure=='post_enable' and host.enabled:raise c.GuardError('post-enable injected')
    monkeypatch.setattr(m,'runtime_guards',runtime)
    if failure in ('disk_before','disk_locked'):
        with pytest.raises(c.GuardError,match='journal_disk_capacity_insufficient'):m.prepare('a'*64,apply=True,host=host)
        assert not c.BACKUP.exists() and not host.commands
    elif failure not in (None,'check'):
        expected='preparation_disarm_failed' if failure=='disarm' else 'preparation_partial_disarmed'
        with pytest.raises(c.GuardError,match=expected):m.prepare('a'*64,apply=True,host=host)
        assert host.enabled is (failure=='disarm')
        assert c.document((c.BACKUP/'failed.json').read_bytes())['new_timer_disarmed'] is (failure!='disarm')
        with pytest.raises(c.GuardError,match='preparation_already_attempted'):m.prepare('a'*64,apply=True,host=host)
    else:
        result=m.prepare('a'*64,apply=failure!='check',host=host)
        assert result['applied'] is (failure!='check') and result['memory_safety_verified'] is False
        if failure=='check':assert not c.BACKUP.exists() and not host.commands
        else:
            assert host.enabled and (c.BACKUP/'complete.json').exists()
            index=c.document((c.BACKUP/'index.json').read_bytes())
            for p,data in originals.items():assert (c.BACKUP/index[str(p)]['file']).read_bytes()==data
            with pytest.raises(c.GuardError,match='preparation_already_attempted'):m.prepare('a'*64,apply=True,host=host)
    if failure!='old_changed':
        for p,data in originals.items():assert p.read_bytes()==data
    assert not c.DROPIN.exists()
    assert not any(word in args for args in host.commands for word in ('restart','start',c.TOSS,m.OLD_TIMER,m.OLD_SERVICE))


from test_entry_capture_activate import setup


@pytest.mark.parametrize('setup',['20261019-llm-window-v2'],indirect=True)
@pytest.mark.parametrize('failure',[None,'pre_receipt','before_restart','consumed_before_restart'])
def test_activation_v2_predecessor_gate_before_consumption_and_restart(setup,monkeypatch,failure):
    import os
    from test_entry_capture_activate import predeployed_run
    s=setup
    old=s.tmp_path/'retired';old.mkdir(mode=0o700)
    attempts=tuple((old/name,os.getuid()) for name in ('activation.receipt','status.json','startup.receipt','engine.jsonl'))
    monkeypatch.setattr(s.m,'RETIRED_OCT19_ATTEMPTS',attempts)
    s.source.write_bytes(b'new\n');s.anchors.update(projection())
    retired_calls=[]
    state={'armed':failure=='pre_receipt'}
    def runner(argv,**kwargs):
        if argv[-1] in ('qwq-entry-capture-20261019.timer','qwq-entry-capture-20261019.service'):
            retired_calls.append((tuple(argv),(s.state/'activation.receipt').exists()))
            value='disabled' if '--property=UnitFileState' in argv else 'inactive'
            if state['armed'] and argv[-1].endswith('.timer') and '--property=UnitFileState' in argv:value='enabled'
            return SimpleNamespace(returncode=0,stdout=value)
        if 'daemon-reload' in argv and not any('restart' in e for e in s.events):
            if failure=='before_restart':state['armed']=True
            if failure=='consumed_before_restart':attempts[0][0].write_bytes(b'old-consumed')
        return s.runner(argv,**kwargs)
    result=predeployed_run(s,runner=runner)
    assert not any('checkout' in e or 'start' in e for e in s.events)
    assert retired_calls and retired_calls[0][1] is False
    if failure=='pre_receipt':
        assert result['status']=='rejected' and not (s.state/'activation.receipt').exists()
        assert not s.events
    elif failure:
        assert result['status']=='failed' and (s.state/'activation.receipt').exists()
        assert not any('restart' in e for e in s.events)
        assert not s.dropin.exists()
    else:
        assert result['status']=='complete'
        assert sum('restart' in e for e in s.events)==1
        assert any(consumed for _,consumed in retired_calls)
        count=len(s.events)
        assert predeployed_run(s,runner=runner)['status']=='rejected'
        assert len(s.events)==count
    if failure=='consumed_before_restart':assert attempts[0][0].read_bytes()==b'old-consumed'


@pytest.mark.parametrize('defect',[None,'deploy','old_paths','old_epoch'])
def test_v2_activation_config_pins_only_reviewed_predeployed_contract(m,monkeypatch,defect):
    a=activation_module();doc,files=v1.payloads(m.core);raw=m.core.document(files['activation'])
    if defect=='deploy':raw['new_head']='b'*40
    if defect=='old_paths':raw['once_path']=raw['once_path'].replace('-v2/','-v1/')
    if defect=='old_epoch':raw['evaluation_epoch']='2026-10-19-engine-llm-window-v1'
    monkeypatch.setattr(a.os,'geteuid',lambda:0)
    monkeypatch.setattr(a,'trusted_dir',lambda *a:None)
    monkeypatch.setattr(a,'read_file',lambda *a,**kw:m.core.raw(raw))
    if defect:
        with pytest.raises(a.GuardError):a.load_config(m.core.PROFILE)
    else:
        cfg=a.load_config(m.core.PROFILE)
        assert cfg.old_head==cfg.new_head==doc['engine_head'] and cfg.evaluation_epoch==m.core.EPOCH


@pytest.mark.parametrize('argv,accepted',[(['--profile','20261019-llm-window-v2'],True),
    (['--profile','20261019-llm-window-v2','--date','2026-10-20'],False),
    (['--profile','20261019-llm-window-v4'],False)])
def test_v2_activation_cli_narrow(monkeypatch,capsys,argv,accepted):
    a=activation_module();selected=[]
    monkeypatch.setattr(a.sys,'argv',[a.__file__,*argv])
    monkeypatch.setattr(a,'load_config',lambda profile:selected.append(profile) or profile)
    monkeypatch.setattr(a,'activate',lambda cfg:{'status':'complete'})
    assert a.main()==(0 if accepted else 1)
    assert selected==(['20261019-llm-window-v2'] if accepted else [])


@pytest.mark.asyncio
async def test_v2_real_once_and_runtime_readiness(m,tmp_path,monkeypatch):
    import json
    from src.analytics.entry_observation_runtime import CapturePlan, ObservationRuntime
    from src.analytics.entry_observation_startup import claim_once_manifest
    from src.dashboard.kr_api import KRAPIHandler
    from test_entry_observation_readiness import request
    from test_market_state_pilot_runtime import bot_with
    c=m.core;now=datetime(2026,10,19,0,25,tzinfo=timezone.utc)
    monkeypatch.setattr(c,'ENGINE_STATE',tmp_path/'state');c.ENGINE_STATE.mkdir(mode=0o700)
    monkeypatch.setattr(c,'NEW',{**c.NEW,**{key:tmp_path/name for key,name in
        (('study','study.json'),('engine_manifest','manifest.json'),('once','once.json'))}})
    doc,files=v1.payloads(c);c.validate_payload(doc,files)
    for key in ('study','engine_manifest','once'):
        c.NEW[key].write_bytes(files[key]);c.NEW[key].chmod(0o600)
    selected=claim_once_manifest(c.NEW['once'],now=now)
    assert selected==str(c.NEW['engine_manifest'])
    assert claim_once_manifest(c.NEW['once'],now=now) is None
    plan=CapturePlan.load(selected,now=now)
    bot=bot_with([f'{100000+i}' for i in range(8)])
    bot.engine=SimpleNamespace(risk_manager=SimpleNamespace(_entry_observation_owner=bot))
    runtime=await ObservationRuntime.install(bot,plan,now=lambda:now)
    try:
        response=await KRAPIHandler(SimpleNamespace(bot=bot)).get_entry_observation_readiness(request())
        assert response.status==200
        data=json.loads(response.text)
        assert data['evaluation_epoch']==c.EPOCH and data['study_sha256']==plan.study_sha256
        assert activation_module().capture_prepared(SimpleNamespace(engine_only=True,evaluation_epoch=c.EPOCH),data)
        assert not bot.ws_feed._quote_subscription_owner._leases
    finally:await runtime.close('test_finished')
