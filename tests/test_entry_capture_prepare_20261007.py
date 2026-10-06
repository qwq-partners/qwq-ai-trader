"""Offline preparation guards: never use production files or service operations."""
import importlib.util
from pathlib import Path
import pytest

SCRIPT = Path(__file__).parents[1] / 'scripts/ops/entry_capture_prepare_20261007.py'

@pytest.fixture
def m():
    spec = importlib.util.spec_from_file_location('prepare_oct7', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

@pytest.mark.parametrize('bad', ['', '/a:/wrong', '/a:/b /x:/y', '-/a:/b', '/a:/b:nope', '/a:/b /a:/b'])
def test_wrong_retention_bindings_rejected(m, bad):
    with pytest.raises(m.GuardError):
        m.verify_bindings(bad, '/a:/b')

def test_rbind_representation_is_equivalent(m):
    m.verify_bindings('/a:/b:rbind', '/a:/b')

@pytest.mark.parametrize('failure', ['validation', 'fsync'])
def test_published_files_restored_after_failure(m, tmp_path, monkeypatch, failure):
    a, b = tmp_path/'a', tmp_path/'b'
    a.write_bytes(b'old-a'); b.write_bytes(b'old-b')
    original_sync = m.sync_dir
    if failure == 'fsync':
        failed = False
        def sync(path):
            nonlocal failed
            if a.read_bytes() == b'new-a' and not failed:
                failed = True
                raise OSError('injected sync failure')
            original_sync(path)
        monkeypatch.setattr(m, 'sync_dir', sync)
    def validate():
        if failure == 'validation':
            raise m.GuardError('validation_failed')
    with pytest.raises((m.GuardError, OSError)):
        m.replace_set({a:b'new-a', b:b'new-b'}, {a:b'old-a', b:b'old-b'}, validate,
                      read=lambda p:p.read_bytes())
    assert a.read_bytes() == b'old-a'
    assert b.read_bytes() == b'old-b'

def test_expected_old_bytes_mismatch_never_overwrites(m, tmp_path):
    p = tmp_path/'config'; p.write_bytes(b'changed')
    with pytest.raises(m.GuardError):
        m.replace_set({p:b'new'}, {p:b'old'}, lambda:None, read=lambda p:p.read_bytes())
    assert p.read_bytes() == b'changed'

def test_wrong_consumed_request_rejected(m):
    request = {'receipt_path':str(m.OLD_RECEIPT)}
    receipt = {'status':'attempt_consumed', 'request_sha256':'0'*64}
    status = {'status':'complete', 'restart_attempted':True}
    with pytest.raises(m.GuardError):
        m.verify_consumed(m.raw(request), m.raw(receipt), m.raw(status), m.dropin(m.OLD_ONCE))

def test_registry_preserves_all_historical_grants(m):
    old = {'schema_version':1,'grants':[{'grant_id':'oct2'},{'grant_id':'oct6-r2'}]}
    new = {'schema_version':1,'grants':[{'grant_id':'oct6-r2'},{'grant_id':'toss-entry-20261007-pilot3'}]}
    with pytest.raises(m.GuardError):
        m.verify_registry(old, new)

def test_successful_replace_publishes_all_before_validation(m, tmp_path):
    a, b = tmp_path/'a', tmp_path/'b'
    a.write_bytes(b'old-a'); b.write_bytes(b'old-b')
    def validate():
        assert (a.read_bytes(),b.read_bytes()) == (b'new-a',b'new-b')
    m.replace_set({a:b'new-a',b:b'new-b'},{a:b'old-a',b:b'old-b'},validate,read=lambda p:p.read_bytes())
    assert (a.read_bytes(),b.read_bytes()) == (b'new-a',b'new-b')

@pytest.mark.parametrize('kind',['symlink','hardlink','writable'])
def test_unsafe_files_rejected(m, tmp_path, kind):
    import os
    p = tmp_path/'file'; p.write_bytes(b'hello'); p.chmod(0o600)
    if kind=='symlink':
        target = tmp_path/'symlink'; target.symlink_to(p); p = target
    elif kind=='hardlink':
        os.link(p,tmp_path/'hardlink')
    else:
        p.chmod(0o666)
    with pytest.raises((m.GuardError,OSError)):
        m.trusted_read(p,owner=os.getuid())

def test_consumed_matching_request_and_history_accepted(m):
    request = {'version':'entry-observation-once-v1','receipt_path':str(m.OLD_RECEIPT),
               'manifest_path':str(m.OLD_ONCE.with_name('manifest.json'))}
    receipt = {'version':'entry-observation-once-receipt-v1','status':'attempt_consumed'}
    status = {'status':'complete','restart_attempted':True}
    m.verify_consumed(m.raw(request),m.raw(receipt),m.raw(status),m.dropin(m.OLD_ONCE))
    request['receipt_path'] = '/wrong-request/startup.receipt'
    with pytest.raises(m.GuardError):
        m.verify_consumed(m.raw(request),m.raw(receipt),m.raw(status),m.dropin(m.OLD_ONCE))

def test_old_namespace_binding_change_rejected_even_when_hashes_match(m,monkeypatch):
    manifest = {'old_retention':{date:{'files':{'/frozen':m.sha(b'unchanged')},
                'bindings':'/snapshot:/config'} for date in ('20261002','20261006')}}
    monkeypatch.setattr(m,'trusted_read',lambda p:b'unchanged')
    class Host:
        def show(self,unit,prop):
            if prop=='Unit':
                return 'qwq-toss-observer-retention-20261002.service'
            return '/live:/config'
    with pytest.raises(m.GuardError,match='retention_bindings_changed'):
        m.old_retention(manifest,Host())

@pytest.mark.parametrize('failure',[None,'check','invalid_cohort','static','enable_activation','post_enable'])
def test_apply_transaction_offline(m,tmp_path,monkeypatch,failure):
    """Real filesystem publication; only trust/host boundaries use controlled doubles."""
    import os
    for name in ('INSTALL','REPO','ETC','UNITS','BACKUP','RELEASE','LAUNCHER','RETENTION','INPUT',
                 'ENGINE_STATE','STATE','CONFIG','OLD_ONCE','OLD_RECEIPT','OLD_STATUS','DROPIN','LOCK','SNAPSHOT','COHORTS'):
        monkeypatch.setattr(m,name,tmp_path/name)
    shared = {k:tmp_path/(k+'.json') for k in ('deployment','plan','registry')}
    monkeypatch.setattr(m,'SHARED',shared)
    new = dict(shared, executor=tmp_path/'executor', study=m.INPUT/'study.json',
               engine_manifest=m.INPUT/'manifest.json',once=m.INPUT/'once.json',
               activation=m.CONFIG/'activation.json',staged_dropin=m.CONFIG/'entry-capture.conf',
               activation_service=tmp_path/'activation.service',activation_timer=tmp_path/'activation.timer',
               retention_service=tmp_path/'retention.service',retention_timer=tmp_path/'retention.timer')
    monkeypatch.setattr(m,'NEW',new)
    cohort_digest = '../outside' if failure=='invalid_cohort' else 'a'*64
    cohort = m.COHORTS/cohort_digest
    data = {k:b'new-'+k.encode() for k in new}
    data['deployment'] = m.raw({'ledger_path':str(cohort/'observations.jsonl'),
               'status_path':str(cohort/'status.json'),'plan_canonical_hash':cohort_digest})
    originals = {p:b'old-'+str(p.name).encode() for p in
                 (*shared.values(),m.LAUNCHER,m.RETENTION,m.OLD_ONCE,m.OLD_RECEIPT,m.OLD_STATUS,m.DROPIN)}
    for p,b in originals.items(): p.write_bytes(b)
    m.LOCK.write_bytes(b''); m.LOCK.chmod(0o644)
    manifest={'old_files':{str(p):m.sha(b) for p,b in originals.items()}}
    monkeypatch.setattr(m,'load_manifest',lambda _:manifest)
    monkeypatch.setattr(m,'trusted_read',lambda p,**kw:Path(p).read_bytes())
    monkeypatch.setattr(m,'verify_consumed',lambda *args:None)
    monkeypatch.setattr(m,'payload',lambda *args:data)
    monkeypatch.setattr(m,'inventory',lambda *args:(b'{}',{'app/code.py':b'code'}))
    checks = []
    monkeypatch.setattr(m,'old_retention',lambda *args:checks.append('old_retention'))
    def runtime(*args,**kwargs):
        if failure=='post_enable' and host.enabled=={m.ACT_TIMER,m.RET_TIMER}:
            raise m.GuardError('injected')
    monkeypatch.setattr(m,'runtime_guards',runtime)
    def mkdir(p,mode=0o755,owner=0,group=0):
        if p==cohort: return
        p.mkdir(mode=mode)
    monkeypatch.setattr(m,'mkdir_new',mkdir)
    # The lock's ownership check runs against synthetic owner metadata.
    real_fstat = os.fstat
    class LockStat:
        st_mode=0o100644; st_uid=1000; st_nlink=1
    monkeypatch.setattr(m.os,'fstat',lambda fd:LockStat() if
                        os.readlink('/proc/self/fd/'+str(fd))==str(m.LOCK) else real_fstat(fd))
    class Host:
        def __init__(self): self.enabled=set(); self.commands=[]
        def show(self,unit,prop):
            if prop=='ActiveState':return 'active' if unit in self.enabled else 'inactive'
            if prop=='UnitFileState':return 'disabled'
            if prop=='BindReadOnlyPaths':return m.bindings(m.SNAPSHOT)
            if prop=='Unit':return m.ACT_SERVICE if unit==m.ACT_TIMER else m.RET_SERVICE
            if prop=='NextElapseUSecRealtime':return 'Wed 2026-10-07 08:55:00 KST'
            raise AssertionError((unit,prop))
        def run(self,args):
            self.commands.append(args)
            assert args[:2] in (['/usr/bin/systemctl','daemon-reload'],
                  ['/usr/bin/systemctl','enable'],['/usr/bin/systemctl','disable'])
            if args[1]=='enable':
                self.enabled.add(args[-1])
                if failure=='enable_activation' and args[-1]==m.ACT_TIMER:
                    raise OSError('injected partial enable')
            if args[1]=='disable': self.enabled.discard(args[-1])
        def static(self):
            if failure=='static':raise m.GuardError('injected')
    host=Host()
    if failure=='invalid_cohort':
        with pytest.raises(m.GuardError,match='invalid_hash'):
            m.prepare('pin',apply=True,host=host)
        assert not m.BACKUP.exists() and not host.commands
        return
    if failure=='check':
        assert m.prepare('pin',apply=False,host=host)['applied'] is False
        assert not host.commands and not m.BACKUP.exists()
        assert all(p.read_bytes()==b for p,b in originals.items())
        return
    if failure:
        with pytest.raises(m.GuardError,match='preparation_partial_disarmed'):
            m.prepare('pin',apply=True,host=host)
        assert not host.enabled
        assert (m.BACKUP/'failed.json').exists()
        if failure=='static':
            assert all(p.read_bytes()==originals[p] for p in shared.values())
    else:
        assert m.prepare('pin',apply=True,host=host)['applied'] is True
        assert host.enabled=={m.ACT_TIMER,m.RET_TIMER}
        assert not m.DROPIN.exists()
        assert (m.BACKUP/'retired-20261006-entry-capture.conf').read_bytes()==originals[m.DROPIN]
        assert len(checks)>=4
        assert all(p.read_bytes()==data[k] for k,p in shared.items())
    with pytest.raises(m.GuardError,match='preparation_already_attempted'):
        m.prepare('pin',apply=True,host=host)

@pytest.mark.parametrize('field,value',[('scan_until','2026-10-07T00:18:00+00:00'),
                                       ('end_at','2026-10-07T00:34:00+00:00')])
def test_wrong_observation_window_rejected(m,field,value):
    plan={'dates':['2026-10-07'],'websocket':{'start_at':'2026-10-07T00:15:00+00:00',
          'scan_until':'2026-10-07T00:17:30+00:00','end_at':'2026-10-07T00:33:00+00:00'}}
    grant={'not_before':'2026-10-06T23:55:00+00:00','expires_at':'2026-10-07T00:34:00+00:00'}
    m.verify_window(plan,grant)
    plan['websocket'][field]=value
    with pytest.raises(m.GuardError):m.verify_window(plan,grant)

@pytest.mark.parametrize('bad',[None,'uid','gid','mode','symlink'])
def test_service_owned_cohorts_parent_creates_real_directory(m,tmp_path,monkeypatch,bad):
    import os, stat
    root=tmp_path/'cohorts';root.mkdir(mode=0o700)
    monkeypatch.setattr(m,'COHORTS',root)
    real_lstat=Path.lstat
    class ParentStat:
        st_uid=998 if bad=='uid' else 997
        st_gid=988 if bad=='gid' else 987
        st_dev=root.stat().st_dev; st_ino=root.stat().st_ino
        st_mode=(stat.S_IFLNK|0o777) if bad=='symlink' else (stat.S_IFDIR|(0o755 if bad=='mode' else 0o700))
    monkeypatch.setattr(Path,'lstat',lambda p,*a,**kw:ParentStat() if p==root else real_lstat(p,*a,**kw))
    real_fstat=os.fstat
    monkeypatch.setattr(m.os,'fstat',lambda fd:ParentStat() if
                        os.readlink('/proc/self/fd/'+str(fd))==str(root) else real_fstat(fd))
    ownership=[]
    monkeypatch.setattr(m.os,'fchown',lambda fd,u,g:ownership.append((Path(os.readlink('/proc/self/fd/'+str(fd))),u,g)))
    child=root/('a'*64)
    if bad:
        with pytest.raises(m.GuardError):m.mkdir_new(child,0o700,997,987)
        assert not child.exists()
    else:
        m.mkdir_new(child,0o700,997,987)
        assert child.is_dir() and stat.S_IMODE(child.stat().st_mode)==0o700
        assert ownership==[(child,997,987)]

@pytest.mark.parametrize('stage',['mkdir','after_open','parent_move'])
def test_cohort_swap_cannot_change_outside_file(m,tmp_path,monkeypatch,stage):
    import os, stat
    root=tmp_path/'cohorts';root.mkdir(mode=0o700)
    monkeypatch.setattr(m,'COHORTS',root)
    victim=tmp_path/'outside';victim.write_bytes(b'untouched');victim.chmod(0o600)
    before=victim.stat()
    real_lstat=Path.lstat
    class ParentStat:
        st_uid=997;st_gid=987;st_mode=stat.S_IFDIR|0o700
        st_dev=root.stat().st_dev; st_ino=root.stat().st_ino
    monkeypatch.setattr(Path,'lstat',lambda p,*a,**kw:ParentStat() if p==root else real_lstat(p,*a,**kw))
    real_fstat=os.fstat
    monkeypatch.setattr(m.os,'fstat',lambda fd:ParentStat() if
                        os.readlink('/proc/self/fd/'+str(fd))==str(root) else real_fstat(fd))
    monkeypatch.setattr(m.os,'chown',lambda *a,**kw:None)
    real_mkdir=os.mkdir
    child=root/('b'*64)
    def swapped(path,mode=0o777,*,dir_fd=None):
        real_mkdir(path,mode,dir_fd=dir_fd)
        if stage=='mkdir' and (Path(path)==child or path==child.name):
            child.rename(root/'stolen')
            child.symlink_to(victim)
    monkeypatch.setattr(m.os,'mkdir',swapped)
    def chown_fd(fd,uid,gid):
        if stage=='parent_move':
            root.rename(tmp_path/'stolen-parent')
            root.mkdir(mode=0o700)
        if stage=='after_open':
            child.rename(root/'stolen')
            child.symlink_to(victim)
    monkeypatch.setattr(m.os,'fchown',chown_fd)
    with pytest.raises((m.GuardError,OSError)):
        m.mkdir_new(child,0o700,997,987)
    assert victim.read_bytes()==b'untouched'
    after=victim.stat()
    assert (after.st_uid,after.st_gid,stat.S_IMODE(after.st_mode)) == (
            before.st_uid,before.st_gid,0o600)
