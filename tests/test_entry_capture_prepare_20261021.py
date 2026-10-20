"""October 21 candidate only; all artifacts and host operations are synthetic."""
import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import pytest
import test_entry_capture_prepare_20261016 as historical

SCRIPT = Path(__file__).parents[1] / 'scripts/ops/entry_capture_prepare_20261021.py'

def test_candidate_installer_available():
    assert SCRIPT.exists(), 'October 21 candidate installer is missing'


@pytest.fixture
def m():
    assert SCRIPT.exists(), 'October 21 candidate installer is missing'
    spec = importlib.util.spec_from_file_location('prepare_oct21', SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def payloads(m, head='a'*40):
    # Historical helpers return fresh dictionaries; historical artifacts are never edited.
    doc, files = historical.payloads(m, head)
    for key in ('once', 'activation'):
        value = m.document(files[key])
        value.update(not_before='2026-10-21T00:25:00+00:00', latest_start_at='2026-10-21T00:27:00+00:00')
        files[key] = m.raw(value)
    study = m.document(files['study'])
    study['as_of'] = '2026-10-21T00:51:00+00:00'
    study['capture'].update(start_at='2026-10-21T00:30:00+00:00',
        admission_end_at='2026-10-21T00:35:00+00:00', end_at=study['as_of'])
    s = study['capture']
    for key in ('max_scans','quote_admission','rest_market_state_evidence'): s.pop(key)
    s.update(version='runner-first-scan-v4', buffer_capacity=40713, queue_capacity=40713,
        batch_size=100, max_bytes=262492160, record_allocations=dict(ws_quote=21000,market_state=19000,other=512),
        record_max_bytes=dict(scan_metadata=65536,ws_quote=4096,market_state=4096,other=4096))
    s['channels'].update(lease_seconds=1262,market_state_max_records=19000)
    study['capture']['selection_basis'] = dict(version='selection-basis-v4', max_candidates=100,
        max_source_terms=16, max_modifier_stages=24)
    study['observed_regular_flow_policy'] = dict(version='pregate-observed-regular-flow-v1',
        fixed_at='2026-10-20T12:00:00+00:00', preparation_seconds=5, state_max_age_seconds=5,
        horizon_seconds=900, max_markout_delay_seconds=5)
    historical.repin_study(m, doc, files, study)
    return doc, files


def test_exact_candidate_payload_and_historical_consumption_paths(m):
    doc, files = payloads(m)
    m.validate_payload(doc, files)
    assert m.PROFILE == '20261021-pregate-v1'
    assert m.EPOCH == '2026-10-21-engine-pregate-v1'
    assert m.NOT_BEFORE == datetime(2026, 10, 21, 0, 25, tzinfo=timezone.utc)
    assert str(m.OLD_ONCE).endswith('/20261020-pregate-v1/once.json')
    assert str(m.OLD_RECEIPT).endswith('/20261020-pregate-v1/startup.receipt')
    assert str(m.OLD_STATUS).endswith('/20261020-pregate-v1/status.json')
    assert b'OnCalendar=2026-10-21 09:25:00 Asia/Seoul\n' in files['activation_timer']
    assert b'activate-20261021.py --profile 20261021-pregate-v1' in files['activation_service']


@pytest.mark.parametrize('scope,key,value', [
    ('capture','start_at','2026-10-16T00:30:00+00:00'),
    ('capture','start_at','2026-10-21T00:29:59+00:00'),
    ('capture','admission_end_at','2026-10-21T00:39:00+00:00'),
    ('capture','end_at','2026-10-21T00:56:00+00:00'),
    ('channels','lease_seconds',1210), ('channels','lease_seconds',1260),
    ('channels','lease_seconds',1262.0), ('channels','lease_seconds',1560),
    ('channels','lease_seconds',1800.0),
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
def test_only_consumed_oct20_receipt_may_retire_old_dropin(m,date):
    once = dict(version='entry-observation-once-v1', receipt_path=str(m.OLD_RECEIPT).replace('20261020',date),
        manifest_path=str(m.OLD_ONCE.with_name('manifest.json')))
    originals = {m.OLD_ONCE:m.raw(once), m.OLD_RECEIPT:m.raw(dict(version='entry-observation-once-receipt-v1',status='attempt_consumed')),
        m.OLD_STATUS:m.raw(dict(status='complete',restart_attempted=True)), m.DROPIN:m.dropin(m.OLD_ONCE)}
    with pytest.raises(m.GuardError,match='old_attempt_not_exactly_consumed'): m.verify_consumed(originals)


def test_exact_oct20_consumption_required(m):
    historical.test_consumed_oct15_receipt_required(m)


@pytest.mark.parametrize('instant',['2026-10-21T00:25:00+00:00','2026-10-21T09:25:01+09:00'])
def test_installation_deadline_rejects_before_host_access(m,monkeypatch,instant):
    historical.test_installation_at_or_after_activation_fails_before_host_access(m,monkeypatch,instant)


@pytest.mark.parametrize('value,valid', [('Wed 2026-10-21 09:25:00 KST',True),
    ('Wed 2026-10-21 00:25:00 UTC',True), ('Fri 2026-10-16 09:10:00 KST',False),
    ('Wed 2026-10-21 09:27:00 KST',False), ('n/a',False)])
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
    plan = CapturePlan.load(m.NEW['engine_manifest'],now=datetime(2026,10,21,0,25,tzinfo=timezone.utc))
    assert plan.settings['selection_basis']['version'] == 'selection-basis-v4'
    assert plan.settings['selection_basis']['max_modifier_stages'] == 24
    assert plan.end_at == datetime(2026,10,21,0,51,0,tzinfo=timezone.utc)
    assert not (m.ENGINE_STATE/'engine.jsonl').exists()


# The standalone transaction has a date-specific fake timer; retain fault injection.
@pytest.mark.parametrize('failure',[None,'check','static','enable','post_enable','backup_write','disk_before','disk_locked','disk_arm','disarm','partial_disable'])
def test_offline_transaction_preserves_old_data_and_disarms_partial_failure(m,tmp_path,monkeypatch,failure):
    import os
    class PreDeadlineClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 21, 0, 19, 59, tzinfo=timezone.utc).astimezone(tz)
    monkeypatch.setattr(m, 'datetime', PreDeadlineClock)
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
        old_enabled=True
        partial_enabled=False
        commands=[]
        def show(self,unit,prop):
            if unit==m.OLD_ACT_SERVICE:return 'inactive'
            if unit==m.OLD_ACT_TIMER:
                return {'ActiveState':'active' if self.old_enabled else 'inactive',
                    'UnitFileState':'enabled' if self.old_enabled else 'disabled',
                    'FragmentPath':str(m.UNITS/m.OLD_ACT_TIMER),'DropInPaths':'',
                    'Unit':m.OLD_ACT_SERVICE}[prop]
            if prop=='ActiveState':return 'active' if self.enabled and unit==m.ACT_TIMER else 'inactive'
            if prop=='UnitFileState':return 'enabled' if self.enabled or self.partial_enabled else 'disabled'
            if prop=='Unit':return m.ACT_SERVICE
            if prop=='FragmentPath':return str(m.NEW['activation_timer' if unit==m.ACT_TIMER else 'activation_service'])
            if prop=='DropInPaths':return ''
            if prop=='NextElapseUSecRealtime':return 'Wed 2026-10-21 09:25:00 KST'
            raise AssertionError((unit,prop))
        disk_checks=0
        def disk_available(self,path):
            self.disk_checks+=1
            fail_at={'disk_before':1,'disk_locked':2,'disk_arm':3}.get(failure)
            return 1336233983 if self.disk_checks==fail_at else 1336233984
        def static(self):
            if failure=='static':raise m.GuardError('injected')
        def run(self,args):
            self.commands.append(args)
            if 'enable' in args:
                self.enabled=True
                if failure in ('enable','disarm','partial_disable'):raise OSError('enable failed after activation')
            if 'disable' in args and m.OLD_ACT_TIMER in args:
                self.old_enabled=False
                return
            if 'disable' in args:
                if failure=='disarm':raise OSError('disable failed')
                self.enabled=False
                if failure=='partial_disable':self.partial_enabled=True
    host=Host()
    def runtime(*a,**kw):
        if failure=='post_enable' and host.enabled:raise m.GuardError('post-enable injected')
    monkeypatch.setattr(m,'runtime_guards',runtime)
    if failure in ('disk_before','disk_locked'):
        with pytest.raises(m.GuardError,match='journal_disk_capacity_insufficient'):
            m.prepare('a'*64,apply=True,host=host)
        assert not m.BACKUP.exists() and not host.commands
    elif failure in ('disarm','partial_disable'):
        with pytest.raises(m.GuardError,match='preparation_disarm_failed'):
            m.prepare('a'*64,apply=True,host=host)
        assert host.enabled if failure=='disarm' else host.partial_enabled
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
            assert not host.old_enabled
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


@pytest.mark.parametrize('defect', [None, 'service_active', 'timer_override', 'wrong_fragment', 'wrong_target', 'unretired'])
def test_spent_oct20_timer_identity_and_retirement(m, defect):
    class Host:
        def show(self, unit, prop):
            if unit == m.OLD_ACT_SERVICE:
                return 'active' if defect == 'service_active' else 'inactive'
            values = {'ActiveState': 'active', 'UnitFileState': 'enabled',
                      'FragmentPath': str(m.UNITS/m.OLD_ACT_TIMER), 'DropInPaths': '',
                      'Unit': m.OLD_ACT_SERVICE}
            if defect == 'timer_override': values['DropInPaths'] = '/other.conf'
            if defect == 'wrong_fragment': values['FragmentPath'] = '/other.timer'
            if defect == 'wrong_target': values['Unit'] = 'other.service'
            return values[prop]
    if defect:
        with pytest.raises(m.GuardError): m.predecessor_guards(Host(), retired=defect == 'unretired')
    else:
        m.predecessor_guards(Host())
        with pytest.raises(m.GuardError): m.predecessor_guards(Host(), retired=True)


def test_retire_dropin_never_unlinks_replaced_inode(m, tmp_path, monkeypatch):
    import os
    p = tmp_path/'entry-capture.conf'; p.write_bytes(b'owned')
    monkeypatch.setattr(m, 'DROPIN', p)
    monkeypatch.setattr(m, 'open_dir', lambda p,owner=0: os.open(p,os.O_RDONLY))
    def replaced(path):
        old = p.with_suffix('.old'); p.rename(old); p.write_bytes(b'owned')
        return old.read_bytes()
    monkeypatch.setattr(m, 'trusted_read', replaced)
    with pytest.raises(m.GuardError, match='consumed_dropin_changed'):
        m.retire_dropin(b'owned')
    assert p.read_bytes() == b'owned'


@pytest.mark.parametrize('instant',['2026-10-21T00:20:00+00:00','2026-10-21T00:24:59+00:00'])
def test_installation_margin_rejects_before_manifest_read(m, monkeypatch, instant):
    class Clock:
        @staticmethod
        def now(tz): return datetime.fromisoformat(instant)
    monkeypatch.setattr(m, 'datetime', Clock)
    monkeypatch.setattr(m, 'load_manifest', lambda _: pytest.fail('마감 뒤 manifest 접근 금지'))
    with pytest.raises(m.GuardError, match='installation_deadline_passed'):
        m.prepare('a'*64)


@pytest.mark.parametrize('defect', [
    'missing_policy', 'extra_policy_field', 'missing_policy_field', 'null_policy',
    'version', 'fixed_at_missing_zone', 'fixed_at_kst', 'fixed_at_invalid',
    'fixed_at_activation', 'fixed_at_after_activation', 'fixed_at_bool',
    'preparation_seconds', 'state_max_age_seconds', 'horizon_seconds',
    'max_markout_delay_seconds', 'bool_number', 'float_number', 'string_number',
])
def test_rehashed_policy_must_be_complete_exact_and_prospective(m, defect):
    doc, files = payloads(m)
    study = m.document(files['study'])
    policy = study['observed_regular_flow_policy']
    if defect == 'missing_policy':
        study.pop('observed_regular_flow_policy')
    elif defect == 'extra_policy_field':
        policy['unknown'] = 1
    elif defect == 'missing_policy_field':
        policy.pop('horizon_seconds')
    elif defect == 'null_policy':
        study['observed_regular_flow_policy'] = None
    elif defect.startswith('fixed_at_'):
        policy['fixed_at'] = {
            'fixed_at_missing_zone': '2026-10-20T12:00:00',
            'fixed_at_kst': '2026-10-20T21:00:00+09:00',
            'fixed_at_invalid': '2026-02-30T12:00:00+00:00',
            'fixed_at_activation': '2026-10-21T00:25:00+00:00',
            'fixed_at_after_activation': '2026-10-21T00:25:01+00:00',
            'fixed_at_bool': True,
        }[defect]
    elif defect == 'version':
        policy['version'] = 'pregate-observed-regular-flow-v2'
    elif defect in ('bool_number', 'float_number', 'string_number'):
        policy['preparation_seconds'] = {
            'bool_number': True, 'float_number': 5.0, 'string_number': '5',
        }[defect]
    else:
        policy[defect] += 1
    historical.repin_study(m, doc, files, study)
    with pytest.raises(m.GuardError):
        m.validate_payload(doc, files)


@pytest.mark.parametrize('fixed_at', [
    '2026-10-20T12:00:00+00:00', '2026-10-21T00:24:59.999999+00:00',
])
def test_policy_fixed_at_strictly_before_activation_accepts_utc(m, fixed_at):
    doc, files = payloads(m)
    study = m.document(files['study'])
    study['observed_regular_flow_policy']['fixed_at'] = fixed_at
    historical.repin_study(m, doc, files, study)
    m.validate_payload(doc, files)


@pytest.mark.parametrize('artifact,key,value', [
    ('once', 'receipt_path', '/home/ubuntu/.local/state/qwq-entry-observation/20261019-llm-window-v5/startup.receipt'),
    ('once', 'manifest_path', '/home/ubuntu/.local/share/qwq-entry-observation/20261019-llm-window-v5/manifest.json'),
    ('receipt', 'status', 'unconsumed'), ('receipt', 'version', 'unsupported'),
    ('status', 'status', 'failed'), ('status', 'restart_attempted', False),
    ('status', 'restart_attempted', 1),
])
def test_consumed_evidence_rejects_other_profile_or_incomplete_attempt(m, artifact, key, value):
    historical.test_old_llm_evidence_must_be_consumed_and_successful(m, artifact, key, value)


def test_predecessor_unit_names_are_bound_to_consumed_oct20(m):
    assert m.OLD_ACT_SERVICE == 'qwq-entry-capture-20261020.service'
    assert m.OLD_ACT_TIMER == 'qwq-entry-capture-20261020.timer'


@pytest.mark.parametrize('defect', ['missing', 'changed', 'future'])
def test_invalid_policy_aborts_apply_before_lock_or_any_mutation(m, tmp_path, monkeypatch, defect):
    doc, files = payloads(m)
    study = m.document(files['study'])
    if defect == 'missing':
        study.pop('observed_regular_flow_policy')
    elif defect == 'changed':
        study['observed_regular_flow_policy']['preparation_seconds'] = 6
    else:
        study['observed_regular_flow_policy']['fixed_at'] = '2026-10-21T00:25:01+00:00'
    historical.repin_study(m, doc, files, study)
    class Clock(datetime):
        @staticmethod
        def now(tz): return datetime(2026, 10, 21, 0, 19, 59, tzinfo=timezone.utc)
    monkeypatch.setattr(m, 'datetime', Clock)
    monkeypatch.setattr(m, 'BACKUP', tmp_path/'absent-backup')
    monkeypatch.setattr(m, 'load_manifest', lambda _: doc)
    monkeypatch.setattr(m, 'old_inputs', lambda _: {})
    monkeypatch.setattr(m, 'runtime_guards', lambda *a, **kw: None)
    monkeypatch.setattr(m, 'predecessor_guards', lambda *a, **kw: None)
    monkeypatch.setattr(m, 'check_destinations', lambda *a: None)
    monkeypatch.setattr(m, 'check_disk_capacity', lambda *a: None)
    # No filesystem/host boundary may be reached after payload validation fails.
    monkeypatch.setattr(m, 'trusted_read', lambda p, **kw: files[p.name])
    monkeypatch.setattr(m.os, 'open', lambda *a, **kw: pytest.fail('invalid policy reached filesystem mutation boundary'))
    with pytest.raises(m.GuardError):
        m.prepare('a'*64, apply=True, host=object())
