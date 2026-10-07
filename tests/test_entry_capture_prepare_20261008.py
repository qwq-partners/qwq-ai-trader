"""Offline fixed-date installation; no live state or host operations."""
import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / 'scripts/ops/entry_capture_prepare_20261008.py'

@pytest.fixture
def m():
    assert SCRIPT.exists(), 'October 8 installer is not implemented'
    spec = importlib.util.spec_from_file_location('prepare_oct8', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def manifest(m):
    return dict(schema_version=1, profile=m.PROFILE, engine_head='a'*40,
                expected_pid='123', expected_nrestarts='0', old_command_sha256='b'*64,
                source_hashes={'scripts/run_trader.py':'c'*64,
                               'scripts/ops/entry_capture_activate.py':'d'*64},
                protected_hashes={k:'e'*64 for k in m.PROTECTED},
                old_files={str(k):'f'*64 for k in m.OLD},
                new_files={k:'1'*64 for k in m.NEW})


def test_manifest_accepts_reviewed_deployed_head_not_fixed_old_head(m):
    m.validate_manifest(manifest(m))


@pytest.mark.parametrize('field,value', [
    ('engine_head','a'*39), ('engine_head','A'*40), ('expected_pid','0'),
    ('expected_nrestarts','1'), ('profile','20261007-pilot3'), ('schema_version',True),
    ('new_files',{'executor':'a'*64}), ('old_files',{}),
    ('protected_hashes',{'config/default.yml':'e'*64}),
    ('source_hashes',{'scripts/../outside.py':'a'*64}),
])
def test_manifest_rejects_contract_expansion(m,field,value):
    doc=manifest(m); doc[field]=value
    with pytest.raises(m.GuardError): m.validate_manifest(doc)


def test_consumed_oct7_receipt_required(m):
    once=m.raw(dict(version='entry-observation-once-v1',receipt_path=str(m.OLD_RECEIPT),
                    manifest_path=str(m.OLD_ONCE.with_name('manifest.json'))))
    rec=m.raw(dict(version='entry-observation-once-receipt-v1',status='attempt_consumed'))
    status=m.raw(dict(status='complete',restart_attempted=True))
    m.verify_consumed({m.OLD_ONCE:once,m.OLD_RECEIPT:rec,m.OLD_STATUS:status,m.DROPIN:m.dropin(m.OLD_ONCE)})
    with pytest.raises(m.GuardError):
        m.verify_consumed({m.OLD_ONCE:once,m.OLD_RECEIPT:rec,m.OLD_STATUS:m.raw({'status':'failed'}),
                           m.DROPIN:m.dropin(m.OLD_ONCE)})


def test_units_are_exact_future_timer_and_engine_only_service(m):
    timer=m.timer_bytes(); service=m.service_bytes()
    assert b'OnCalendar=2026-10-08 08:55:00 Asia/Seoul\n' in timer
    assert b'Persistent=false\nAccuracySec=1s\n' in timer
    assert b'activate-20261008.py --profile 20261008-signal-window-v1' in service
    assert b'Restart=no\n' in service
    assert b'toss' not in timer+service


def test_duplicate_json_keys_rejected(m):
    with pytest.raises(m.GuardError): m.document(b'{"a":1,"a":2}')


def test_exclusive_write_never_overwrites_or_follows_leaf(m,tmp_path,monkeypatch):
    monkeypatch.setattr(m,'open_dir',lambda p,owner=0:__import__('os').open(p,__import__('os').O_RDONLY))
    p=tmp_path/'file'; p.write_bytes(b'original')
    with pytest.raises(FileExistsError): m.write_new(p,b'new',owner=__import__('os').geteuid())
    link=tmp_path/'link'; link.symlink_to(p)
    with pytest.raises(FileExistsError): m.write_new(link,b'new',owner=__import__('os').geteuid())
    assert p.read_bytes()==b'original'


def payloads(m):
    doc=manifest(m)
    capture=dict(version='runner-signal-window-v1',study_ref=m.EPOCH,
                 start_at='2026-10-08T01:00:00+00:00',admission_end_at='2026-10-08T01:50:00+00:00',
                 end_at='2026-10-08T02:10:00+00:00',max_scans=12,quote_admission='signal_created',
                 journal_path=str(m.ENGINE_STATE/'engine.jsonl'),source_version_ref='commit:'+doc['engine_head'])
    files=dict(executor=b'# verified executor\n',study=m.raw(dict(evaluation_epoch=m.EPOCH,capture=capture,
                   data_basis='received_snapshot',capital_policy={'version':'pre-pending-guards-v1'})),
               once=m.raw(dict(version='entry-observation-once-v1',manifest_path=str(m.NEW['engine_manifest']),
                              receipt_path=str(m.ENGINE_STATE/'startup.receipt'),
                              not_before='2026-10-07T23:55:00+00:00',latest_start_at='2026-10-07T23:57:00+00:00')),
               staged_dropin=m.dropin(m.NEW['once']),activation_service=m.service_bytes(),activation_timer=m.timer_bytes())
    files['engine_manifest']=m.raw(dict(study_path=str(m.NEW['study']),study_sha256=m.sha(files['study'])))
    activation=dict(old_head=doc['engine_head'],new_head=doc['engine_head'],source_hashes=doc['source_hashes'],
                    protected_hashes=doc['protected_hashes'],input_hashes={str(m.NEW[k]):m.sha(files[k]) for k in
                    ('study','once','engine_manifest')},study_sha256=m.sha(files['study']),evaluation_epoch=m.EPOCH,
                    dropin_sha256=m.sha(files['staged_dropin']),not_before='2026-10-07T23:55:00+00:00',
                    latest_start_at='2026-10-07T23:57:00+00:00',toss_uid=997,toss_gid=987)
    activation.update({k:str(v) for k,v in m.activation_paths().items()})
    doc['source_hashes'][m.EXECUTOR_SOURCE]=m.sha(files['executor'])
    files['activation']=m.raw(activation)
    doc['new_files']={k:m.sha(v) for k,v in files.items()}
    return doc,files


def test_payload_exact_frozen_bindings(m):
    doc,files=payloads(m); m.validate_payload(doc,files)


@pytest.mark.parametrize('key,change',[
    ('once',{'receipt_path':'/tmp/receipt'}),('once',{'latest_start_at':'2026-10-08T23:57:00+00:00'}),
    ('activation',{'new_head':'b'*40}),('activation',{'input_hashes':{}}),
    ('activation',{'staged_dropin':'/tmp/evil'}),('engine_manifest',{'study_path':'/tmp/study'}),
    ('study',{'evaluation_epoch':'old'}),('study',{'capture':{'version':'runner-first-scan-v4'}}),
])
def test_payload_rejects_even_rehashed_wrong_contract(m,key,change):
    doc,files=payloads(m)
    value=m.document(files[key]); value.update(change); files[key]=m.raw(value)
    doc['new_files'][key]=m.sha(files[key])
    with pytest.raises(m.GuardError): m.validate_payload(doc,files)


@pytest.mark.parametrize('key', ['activation_timer','activation_service','executor','staged_dropin'])
def test_rehashed_additional_execution_or_timer_is_rejected(m,key):
    doc,files=payloads(m); files[key]+=b'ExecStart=/bin/false\n'; doc['new_files'][key]=m.sha(files[key])
    with pytest.raises(m.GuardError):m.validate_payload(doc,files)


def test_static_validation_invokes_existing_runtime_loader(m,monkeypatch):
    commands=[]
    monkeypatch.setattr(m.Host,'run',lambda self,args:commands.append(args))
    m.Host().static()
    program=commands[-1][-1]
    assert 'import CapturePlan;' in program
    assert 'CapturePlan.load(' in program


@pytest.mark.parametrize('failure',[None,'check','static','enable','post_enable','backup_write'])
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
            if prop=='NextElapseUSecRealtime':return 'Thu 2026-10-08 08:55:00 KST'
            raise AssertionError((unit,prop))
        def static(self):
            if failure=='static':raise m.GuardError('injected')
        def run(self,args):
            self.commands.append(args)
            if 'enable' in args:
                self.enabled=True
                if failure=='enable':raise OSError('enable failed after activation')
            if 'disable' in args:self.enabled=False
    host=Host()
    def runtime(*a,**kw):
        if failure=='post_enable' and host.enabled:raise m.GuardError('post-enable injected')
    monkeypatch.setattr(m,'runtime_guards',runtime)
    if failure in ('static','enable','post_enable','backup_write'):
        with pytest.raises(m.GuardError,match='preparation_partial_disarmed'):
            m.prepare('a'*64,apply=True,host=host)
        assert not host.enabled
        assert m.document((m.BACKUP/'failed.json').read_bytes())['new_timer_disarmed'] is True
        with pytest.raises(m.GuardError):m.prepare('a'*64,apply=True,host=host)
    else:
        result=m.prepare('a'*64,apply=failure!='check',host=host)
        assert result['applied']==(failure!='check')
        if failure=='check':
            assert not m.BACKUP.exists() and not host.commands
        else:
            assert host.enabled and (m.BACKUP/'complete.json').exists()
            assert not m.DROPIN.exists()
            assert host.commands[-1]==['/usr/bin/systemctl','enable','--now',m.ACT_TIMER]
    for p,data in originals.items():
        if p!=m.DROPIN:assert p.read_bytes()==data
    assert not any(word in command for command in host.commands for word in ('restart','start',m.TOSS))


@pytest.mark.parametrize('value,valid',[
    ('Thu 2026-10-08 08:55:00 KST',True),('Wed 2026-10-07 23:55:00 UTC',True),
    ('Thu 2026-10-08 08:54:00 KST',False),('Fri 2026-10-09 08:55:00 KST',False),('n/a',False)])
def test_timer_next_elapse_is_exact(m,value,valid):
    if valid:m.verify_next_elapse(value)
    else:
        with pytest.raises(m.GuardError):m.verify_next_elapse(value)


@pytest.mark.parametrize('prop,value',[('FragmentPath','/run/systemd/system/evil'),('DropInPaths','/etc/extra.conf')])
def test_loaded_units_reject_overriding_fragments_or_dropins(m,prop,value):
    class Host:
        def show(self,unit,name):
            if name==prop:return value
            return str(m.UNITS/unit) if name=='FragmentPath' else ''
    with pytest.raises(m.GuardError):m.verify_loaded_units(Host())
