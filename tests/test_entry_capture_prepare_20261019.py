"""October 19 candidate only; all artifacts and host operations are synthetic."""
import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import pytest
import test_entry_capture_prepare_20261016 as historical

SCRIPT = Path(__file__).parents[1] / 'scripts/ops/entry_capture_prepare_20261019.py'

def test_candidate_installer_available():
    assert SCRIPT.exists(), 'October 19 candidate installer is missing'


@pytest.fixture
def m():
    assert SCRIPT.exists(), 'October 19 candidate installer is missing'
    spec = importlib.util.spec_from_file_location('prepare_oct19', SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def payloads(m, head='a'*40):
    # Historical helpers return fresh dictionaries; historical artifacts are never edited.
    doc, files = historical.payloads(m, head)
    for key in ('once', 'activation'):
        value = m.document(files[key])
        value.update(not_before='2026-10-19T00:25:00+00:00', latest_start_at='2026-10-19T00:27:00+00:00')
        files[key] = m.raw(value)
    study = m.document(files['study'])
    study['as_of'] = '2026-10-19T01:00:00+00:00'
    study['capture'].update(start_at='2026-10-19T00:30:00+00:00',
        admission_end_at='2026-10-19T00:40:00+00:00', end_at=study['as_of'])
    study['capture']['channels']['lease_seconds'] = 1800
    study['capture']['selection_basis'] = dict(version='selection-basis-v3', max_candidates=100,
        max_source_terms=16, max_modifier_stages=24)
    historical.repin_study(m, doc, files, study)
    return doc, files


def test_exact_candidate_payload_and_historical_consumption_paths(m):
    doc, files = payloads(m)
    m.validate_payload(doc, files)
    assert m.PROFILE == '20261019-llm-window-v1'
    assert m.EPOCH == '2026-10-19-engine-llm-window-v1'
    assert m.NOT_BEFORE == datetime(2026, 10, 19, 0, 25, tzinfo=timezone.utc)
    assert str(m.OLD_ONCE).endswith('/20261016-llm-window-v1/once.json')
    assert str(m.OLD_RECEIPT).endswith('/20261016-llm-window-v1/startup.receipt')
    assert str(m.OLD_STATUS).endswith('/20261016-llm-window-v1/status.json')
    assert b'OnCalendar=2026-10-19 09:25:00 Asia/Seoul\n' in files['activation_timer']
    assert b'activate-20261019.py --profile 20261019-llm-window-v1' in files['activation_service']


@pytest.mark.parametrize('scope,key,value', [
    ('capture','start_at','2026-10-16T00:30:00+00:00'),
    ('capture','start_at','2026-10-19T00:29:59+00:00'),
    ('capture','admission_end_at','2026-10-19T00:39:00+00:00'),
    ('capture','end_at','2026-10-19T00:56:00+00:00'),
    ('channels','lease_seconds',1560), ('channels','lease_seconds',1800.0),
    ('channels','max_candidates',6), ('capture','max_bytes',7434242049),
    ('record_allocations','other',8001),
    ('selection_basis','version','selection-basis-v2'),
    ('selection_basis','max_candidates',99), ('selection_basis','max_source_terms',17),
    ('selection_basis','max_modifier_stages',23), ('selection_basis','max_modifier_stages',True),
    ('selection_basis','extra',1), ('capture','capacity_evidence_ref',''),
])
def test_rehashed_candidate_rejects_wrong_date_window_caps_and_selection(m,scope,key,value):
    doc, files = payloads(m)
    study = m.document(files['study'])
    target = study['capture'] if scope == 'capture' else study['capture'][scope]
    target[key] = value
    historical.repin_study(m, doc, files, study)
    with pytest.raises(m.GuardError): m.validate_payload(doc, files)


def test_missing_v3_modifier_bound_rejected(m):
    doc, files = payloads(m)
    study = m.document(files['study'])
    study['capture']['selection_basis'].pop('max_modifier_stages')
    historical.repin_study(m, doc, files, study)
    with pytest.raises(m.GuardError): m.validate_payload(doc, files)


@pytest.mark.parametrize('key', ['study','executor','once','activation','engine_manifest'])
def test_corrupted_pin_rejected(m,key):
    doc, files = payloads(m)
    files[key] += b' '
    with pytest.raises(m.GuardError, match='payload_pin_mismatch'): m.validate_payload(doc, files)


@pytest.mark.parametrize('date',['20261015','20261014'])
def test_only_consumed_oct16_receipt_may_retire_old_dropin(m,date):
    once = dict(version='entry-observation-once-v1', receipt_path=str(m.OLD_RECEIPT).replace('20261016',date),
        manifest_path=str(m.OLD_ONCE.with_name('manifest.json')))
    originals = {m.OLD_ONCE:m.raw(once), m.OLD_RECEIPT:m.raw(dict(version='entry-observation-once-receipt-v1',status='attempt_consumed')),
        m.OLD_STATUS:m.raw(dict(status='complete',restart_attempted=True)), m.DROPIN:m.dropin(m.OLD_ONCE)}
    with pytest.raises(m.GuardError,match='old_attempt_not_exactly_consumed'): m.verify_consumed(originals)


def test_exact_oct16_consumption_required(m):
    historical.test_consumed_oct15_receipt_required(m)


@pytest.mark.parametrize('instant',['2026-10-19T00:25:00+00:00','2026-10-19T09:25:01+09:00'])
def test_installation_deadline_rejects_before_host_access(m,monkeypatch,instant):
    historical.test_installation_at_or_after_activation_fails_before_host_access(m,monkeypatch,instant)


@pytest.mark.parametrize('value,valid', [('Mon 2026-10-19 09:25:00 KST',True),
    ('Mon 2026-10-19 00:25:00 UTC',True), ('Fri 2026-10-16 09:10:00 KST',False),
    ('Mon 2026-10-19 09:27:00 KST',False), ('n/a',False)])
def test_next_elapse_exact(m,value,valid):
    historical.test_timer_next_elapse_is_exact(m,value,valid)


@pytest.mark.parametrize('defect',[None,'extra_source','missing_source','source_changed','config_changed',
    'old_head','dirty_tree','pid','nrestarts','process','dropin','toss_active','bot_inactive'])
def test_source_config_process_pins_preserved(m,tmp_path,monkeypatch,defect):
    historical.test_runtime_rejects_changed_source_inventory_protected_config_and_process(m,tmp_path,monkeypatch,defect)


def test_full_runtime_loads_unmodified_candidate_artifact(m,tmp_path,monkeypatch):
    from src.analytics.entry_observation_runtime import CapturePlan
    monkeypatch.setattr(m,'ENGINE_STATE',tmp_path/'state')
    monkeypatch.setattr(m,'NEW',{**m.NEW,'study':tmp_path/'study.json','engine_manifest':tmp_path/'manifest.json'})
    doc, files = payloads(m)
    m.validate_payload(doc,files)
    for key in ('study','engine_manifest'): m.NEW[key].write_bytes(files[key])
    plan = CapturePlan.load(m.NEW['engine_manifest'],now=datetime(2026,10,19,0,25,tzinfo=timezone.utc))
    assert plan.settings['selection_basis']['version'] == 'selection-basis-v3'
    assert plan.settings['selection_basis']['max_modifier_stages'] == 24
    assert plan.end_at == datetime(2026,10,19,1,0,tzinfo=timezone.utc)
    assert not (m.ENGINE_STATE/'engine.jsonl').exists()


# The standalone transaction has a date-specific fake timer; retain fault injection.
@pytest.mark.parametrize('failure',[None,'check','static','enable','post_enable','backup_write','disk_before','disk_locked','disk_arm','disarm'])
def test_offline_transaction_preserves_old_data_and_disarms_partial_failure(m,tmp_path,monkeypatch,failure):
    import os
    for name in ('INSTALL','BACKUP','INPUT','ENGINE_STATE','STATE','OLD_ONCE','OLD_RECEIPT','OLD_STATUS','DROPIN','LOCK'):
        monkeypatch.setattr(m,name,tmp_path/name)
    old=(m.OLD_ONCE,m.OLD_RECEIPT,m.OLD_STATUS,m.DROPIN); monkeypatch.setattr(m,'OLD',old)
    new={k:tmp_path/k for k in m.NEW}; monkeypatch.setattr(m,'NEW',new)
    m.INSTALL.mkdir(); (m.INSTALL/'payload').mkdir()
    files={k:('new-'+k).encode() for k in new}
    for k,data in files.items(): (m.INSTALL/'payload'/k).write_bytes(data)
    originals={p:('old-'+p.name).encode() for p in old}
    for p,data in originals.items(): p.write_bytes(data)
    m.LOCK.write_bytes(b'');m.LOCK.chmod(0o644)
    doc={'old_files':{str(p):m.sha(data) for p,data in originals.items()}}
    monkeypatch.setattr(m,'load_manifest',lambda _:doc)
    monkeypatch.setattr(m,'trusted_read',lambda p,**kw:Path(p).read_bytes())
    monkeypatch.setattr(m,'verify_consumed',lambda *_:None)
    monkeypatch.setattr(m,'validate_payload',lambda *_:None)
    monkeypatch.setattr(m,'mkdir_new',lambda p,owner=0:p.mkdir(mode=0o700))
    monkeypatch.setattr(m,'open_dir',lambda p,owner=0:os.open(p,os.O_RDONLY))
    original_write=m.write_new
    def write(p,data,mode=0o600,owner=0):
        if failure=='backup_write' and p.name=='attempt.json':raise OSError('injected')
        original_write(p,data,mode,os.geteuid())
    monkeypatch.setattr(m,'write_new',write)
    real_fstat=os.fstat
    class LockStat:
        st_mode=0o100644;st_uid=1000;st_nlink=1
    monkeypatch.setattr(m.os,'fstat',lambda fd:LockStat() if os.readlink('/proc/self/fd/'+str(fd))==str(m.LOCK)
                        else real_fstat(fd))
    class Host:
        enabled=False
        commands=[]
        def show(self,unit,prop):
            if prop=='ActiveState':return 'active' if self.enabled and unit==m.ACT_TIMER else 'inactive'
            if prop=='UnitFileState':return 'disabled'
            if prop=='Unit':return m.ACT_SERVICE
            if prop=='FragmentPath':return str(m.NEW['activation_timer' if unit==m.ACT_TIMER else 'activation_service'])
            if prop=='DropInPaths':return ''
            if prop=='NextElapseUSecRealtime':return 'Mon 2026-10-19 09:25:00 KST'
            raise AssertionError((unit,prop))
        disk_checks=0
        def disk_available(self,path):
            self.disk_checks+=1
            fail_at={'disk_before':1,'disk_locked':2,'disk_arm':3}.get(failure)
            return 8507983871 if self.disk_checks==fail_at else 8507983872
        def static(self):
            if failure=='static':raise m.GuardError('injected')
        def run(self,args):
            self.commands.append(args)
            if 'enable' in args:
                self.enabled=True
                if failure in ('enable','disarm'):raise OSError('enable failed after activation')
            if 'disable' in args:
                if failure=='disarm':raise OSError('disable failed')
                self.enabled=False
    host=Host()
    def runtime(*a,**kw):
        if failure=='post_enable' and host.enabled:raise m.GuardError('post-enable injected')
    monkeypatch.setattr(m,'runtime_guards',runtime)
    if failure in ('disk_before','disk_locked'):
        with pytest.raises(m.GuardError,match='journal_disk_capacity_insufficient'):
            m.prepare('a'*64,apply=True,host=host)
        assert not m.BACKUP.exists() and not host.commands
    elif failure=='disarm':
        with pytest.raises(m.GuardError,match='preparation_disarm_failed'):
            m.prepare('a'*64,apply=True,host=host)
        assert host.enabled
        assert m.document((m.BACKUP/'failed.json').read_bytes())['new_timer_disarmed'] is False
        with pytest.raises(m.GuardError,match='preparation_already_attempted'):
            m.prepare('a'*64,apply=True,host=host)
    elif failure in ('static','enable','post_enable','backup_write','disk_arm'):
        with pytest.raises(m.GuardError,match='preparation_partial_disarmed'):
            m.prepare('a'*64,apply=True,host=host)
        assert not host.enabled
        assert m.document((m.BACKUP/'failed.json').read_bytes())['new_timer_disarmed'] is True
        with pytest.raises(m.GuardError):m.prepare('a'*64,apply=True,host=host)
    else:
        result=m.prepare('a'*64,apply=failure!='check',host=host)
        assert result['applied']==(failure!='check')
        assert result['memory_safety_verified'] is False
        assert result['runtime_semantics_verified'] is (failure!='check')
        if failure=='check':
            assert result['reason']=='payload_validated_pending_runtime_validation'
        if failure=='check':
            assert not m.BACKUP.exists() and not host.commands
        else:
            assert host.enabled and (m.BACKUP/'complete.json').exists()
            assert not m.DROPIN.exists()
            backup_index=m.document((m.BACKUP/'index.json').read_bytes())
            for path,original in originals.items():
                entry=backup_index[str(path)]
                assert (m.BACKUP/entry['file']).read_bytes()==original
                assert entry['sha256']==m.sha(original)
            assert host.commands[-1]==['/usr/bin/systemctl','enable','--now',m.ACT_TIMER]
            with pytest.raises(m.GuardError,match='preparation_already_attempted'):
                m.prepare('a'*64,apply=True,host=host)
    for p,data in originals.items():
        if p!=m.DROPIN:assert p.read_bytes()==data
    assert not any(word in command for command in host.commands for word in ('restart','start',m.TOSS))
