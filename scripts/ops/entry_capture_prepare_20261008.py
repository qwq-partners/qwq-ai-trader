#!/usr/bin/python3
"""One-shot, engine-only October 8 installation; never deploys or restarts.

Stage manifest.json and payload/<key> under INSTALL, pin the manifest with
--manifest-sha256, then --check or --apply. Failed attempts require manual review.
No Toss configuration, grant, release, retention, or original evidence is changed.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

PROFILE = '20261008-signal-window-v1'
EPOCH = '2026-10-08-engine-entry-signal-window-v1'
INSTALL = Path('/var/lib/qwq-entry-capture/install-20261008')
BACKUP = Path('/var/backups/qwq-entry-capture-20261008')
REPO = Path('/home/ubuntu/projects/qwq-ai-trader')
INPUT = Path('/home/ubuntu/.local/share/qwq-entry-observation') / PROFILE
ENGINE_STATE = Path('/home/ubuntu/.local/state/qwq-entry-observation') / PROFILE
STATE = Path('/var/lib/qwq-entry-capture') / PROFILE
CONFIG = Path('/etc/qwq-entry-capture')
UNITS = Path('/etc/systemd/system')
OLD_ONCE = INPUT.parent / '20261007-pilot3/once.json'
OLD_RECEIPT = ENGINE_STATE.parent / '20261007-pilot3/startup.receipt'
OLD_STATUS = STATE.parent / '20261007-pilot3/status.json'
DROPIN = UNITS / 'qwq-ai-trader.service.d/entry-capture.conf'
OLD = (OLD_ONCE, OLD_RECEIPT, OLD_STATUS, DROPIN)
KILL = Path('/home/ubuntu/.cache/ai_trader/KILL_SWITCH_KR')
LOCK = Path('/tmp/qwq-ai-trader-deploy.lock')
BOT = 'qwq-ai-trader.service'
TOSS = 'qwq-toss-observer.service'
ACT_SERVICE = 'qwq-entry-capture-20261008.service'
ACT_TIMER = 'qwq-entry-capture-20261008.timer'
NOT_BEFORE = datetime(2026,10,7,23,55,tzinfo=timezone.utc)
PROTECTED = ('config/default.yml', 'config/evolved_overrides.yml')
EXECUTOR_SOURCE = 'scripts/ops/entry_capture_activate.py'
NEW = dict(executor=Path('/usr/libexec/qwq-entry-capture/activate-20261008.py'),
           study=INPUT/'study.json', engine_manifest=INPUT/'manifest.json', once=INPUT/'once.json',
           activation=CONFIG/(PROFILE+'.json'), staged_dropin=CONFIG/(PROFILE+'-entry-capture.conf'),
           activation_service=UNITS/ACT_SERVICE, activation_timer=UNITS/ACT_TIMER)
ENV = {'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LANG':'C','LC_ALL':'C','PYTHONDONTWRITEBYTECODE':'1'}

class GuardError(Exception):
    pass


def require(ok, code='preparation_guard_failed'):
    if not ok:
        raise GuardError(code)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def raw(doc):
    return (json.dumps(doc,sort_keys=True,separators=(',',':'),allow_nan=False)+'\n').encode()


def document(data):
    def pairs(items):
        result = {}
        for k,v in items:
            require(k not in result,'duplicate_json_key'); result[k] = v
        return result
    return json.loads(data,object_pairs_hook=pairs,parse_constant=lambda _:require(False,'nonfinite_json'))


def hash_value(value):
    require(type(value) is str and re.fullmatch('[0-9a-f]{64}',value),'invalid_hash')


def source_name(name):
    return (type(name) is str and re.fullmatch(r'(src|scripts)/[a-zA-Z0-9_./-]+\.py',name)
            and '..' not in Path(name).parts and str(Path(name)) == name)


def open_dir(path, owner=0):
    """Walk without symlinks. Ubuntu's existing private group is scoped to home."""
    path = Path(path)
    require(path.is_absolute() and '..' not in path.parts,'invalid_path')
    fd = os.open('/',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    cursor = Path('/')
    try:
        for name in path.parts[1:]:
            child = os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            os.close(fd); fd=child; cursor=cursor/name
            info=os.fstat(fd)
            user_parent = owner==1000 and info.st_uid==info.st_gid==1000 and cursor.is_relative_to('/home/ubuntu')
            require(info.st_uid in (0,owner) and info.st_gid in (0,owner)
                    and not info.st_mode & (0o002 if user_parent else 0o022),'unsafe_directory')
        info=os.fstat(fd)
        require(info.st_uid==owner and info.st_gid==owner,'unsafe_directory_owner')
        return fd
    except BaseException:
        os.close(fd); raise


def trusted_read(path, owner=0, limit=4*1024*1024, *, expected_sha256=None):
    path=Path(path)
    source = path.is_relative_to(REPO) and (source_name(str(path.relative_to(REPO)))
                                         or str(path.relative_to(REPO)) in PROTECTED)
    if expected_sha256 is not None:
        hash_value(expected_sha256)
        require(owner==1000 and source,'invalid_pinned_read_scope')
    parent=open_dir(path.parent,owner)
    try:
        fd=os.open(path.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
        with os.fdopen(fd,'rb') as stream:
            before=os.fstat(stream.fileno())
            group_ok=owner==1000 and before.st_gid==1000 and (expected_sha256 is not None or path==KILL)
            require(stat.S_ISREG(before.st_mode) and before.st_nlink==1 and before.st_uid==owner
                    and before.st_gid==owner and before.st_size<=limit
                    and not before.st_mode & (0o002 if group_ok else 0o022),'unsafe_file')
            data=stream.read(limit+1); after=os.fstat(stream.fileno())
            require(len(data)<=limit and (before.st_size,before.st_mtime_ns,before.st_ctime_ns)==
                    (after.st_size,after.st_mtime_ns,after.st_ctime_ns),'file_changed')
            require(expected_sha256 is None or sha(data)==expected_sha256,'pinned_file_changed')
            return data
    finally:
        os.close(parent)


def write_new(path,data,mode=0o600,owner=0):
    parent=open_dir(path.parent,owner)
    try:
        fd=os.open(path.name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,mode,dir_fd=parent)
        with os.fdopen(fd,'wb') as stream:
            os.fchown(stream.fileno(),owner,owner); os.fchmod(stream.fileno(),mode)
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.fsync(parent)
    finally:
        os.close(parent)


def mkdir_new(path,owner=0):
    parent=open_dir(path.parent,owner)
    try:
        os.mkdir(path.name,0o700,dir_fd=parent)
        fd=os.open(path.name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
        try:
            os.fchown(fd,owner,owner); os.fchmod(fd,0o700); os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(parent)
    finally:
        os.close(parent)


def dropin(once):
    return ('[Service]\nExecStart=\nExecStart=/home/ubuntu/projects/qwq-ai-trader/venv/bin/python '
            'scripts/run_trader.py --market kr --config '
            '/home/ubuntu/projects/qwq-ai-trader/config/default.yml '
            f'--entry-observation-once {once}\n').encode()


def timer_bytes():
    return ('[Unit]\nDescription=October 8 engine observation activation\n[Timer]\n'
            'OnCalendar=2026-10-08 08:55:00 Asia/Seoul\nPersistent=false\nAccuracySec=1s\n'
            f'Unit={ACT_SERVICE}\n[Install]\nWantedBy=timers.target\n').encode()


def service_bytes():
    return ('[Unit]\nDescription=October 8 engine observation activation\n[Service]\nType=oneshot\n'
            'User=root\nGroup=root\nUMask=0077\n'
            f'ExecStart=/usr/bin/python3 -I -B {NEW["executor"]} --profile {PROFILE}\n'
            'Restart=no\nTimeoutStartSec=120\n').encode()


def validate_manifest(m):
    keys={'schema_version','profile','engine_head','expected_pid','expected_nrestarts',
          'old_command_sha256','source_hashes','protected_hashes','old_files','new_files'}
    require(type(m) is dict and set(m)==keys and type(m['schema_version']) is int
            and m['schema_version']==1 and m['profile']==PROFILE,'manifest_contract_mismatch')
    require(type(m['engine_head']) is str and re.fullmatch('[0-9a-f]{40}',m['engine_head'])
            and type(m['expected_pid']) is str and re.fullmatch('[1-9][0-9]*',m['expected_pid'])
            and m['expected_nrestarts']=='0','engine_identity_invalid')
    hash_value(m['old_command_sha256'])
    for key in ('source_hashes','protected_hashes','old_files','new_files'):
        require(type(m[key]) is dict and 0<len(m[key])<=1000,'inventory_invalid')
        for value in m[key].values(): hash_value(value)
    require(set(m['protected_hashes'])==set(PROTECTED) and set(m['old_files'])=={str(p) for p in OLD}
            and set(m['new_files'])==set(NEW),'inventory_mismatch')
    require({'scripts/run_trader.py',EXECUTOR_SOURCE}<=set(m['source_hashes'])
            and all(source_name(n) for n in m['source_hashes']),'source_inventory_invalid')


def load_manifest(expected):
    hash_value(expected)
    data=trusted_read(INSTALL/'manifest.json',limit=1024*1024)
    require(sha(data)==expected,'manifest_pin_mismatch')
    m=document(data); validate_manifest(m); return m


def verify_consumed(originals):
    once,receipt,status=(document(originals[p]) for p in OLD[:3])
    require(once.get('version')=='entry-observation-once-v1'
            and once.get('receipt_path')==str(OLD_RECEIPT)
            and once.get('manifest_path')==str(OLD_ONCE.with_name('manifest.json'))
            and receipt.get('version')=='entry-observation-once-receipt-v1'
            and receipt.get('status')=='attempt_consumed' and status.get('status')=='complete'
            and status.get('restart_attempted') is True and originals[DROPIN]==dropin(OLD_ONCE),
            'old_attempt_not_exactly_consumed')


def activation_paths():
    return dict(repo=REPO,once_path=NEW['once'],launcher_path=Path('/usr/libexec/qwq-toss-observer/launcher.py'),
                deployment_path=Path('/etc/qwq-toss-observer/deployment.json'),staged_dropin=NEW['staged_dropin'],
                dropin_path=DROPIN,kill_path=KILL,state_dir=STATE,lock_path=LOCK)


def validate_payload(m,files):
    require(set(files)==set(NEW),'payload_inventory_mismatch')
    for k,data in files.items():
        require(sha(data)==m['new_files'][k],'payload_pin_mismatch')
    require(sha(files['executor'])==m['source_hashes'][EXECUTOR_SOURCE],'executor_source_mismatch')
    compile(files['executor'],str(NEW['executor']),'exec')
    for k,expected in dict(staged_dropin=dropin(NEW['once']),activation_service=service_bytes(),
                           activation_timer=timer_bytes()).items():
        require(files[k]==expected,'unit_or_dropin_mismatch')
    require(document(files['once'])==dict(version='entry-observation-once-v1',
            manifest_path=str(NEW['engine_manifest']),receipt_path=str(ENGINE_STATE/'startup.receipt'),
            not_before='2026-10-07T23:55:00+00:00',latest_start_at='2026-10-07T23:57:00+00:00'),
            'once_contract_mismatch')
    require(document(files['engine_manifest'])==dict(study_path=str(NEW['study']),study_sha256=sha(files['study'])),
            'study_binding_mismatch')
    expected=dict(old_head=m['engine_head'],new_head=m['engine_head'],source_hashes=m['source_hashes'],
                  protected_hashes=m['protected_hashes'],input_hashes={str(NEW[k]):sha(files[k]) for k in
                  ('study','engine_manifest','once')},dropin_sha256=sha(files['staged_dropin']),
                  not_before='2026-10-07T23:55:00+00:00',latest_start_at='2026-10-07T23:57:00+00:00',
                  toss_uid=997,toss_gid=987,study_sha256=sha(files['study']),evaluation_epoch=EPOCH)
    expected.update({k:str(v) for k,v in activation_paths().items()})
    require(document(files['activation'])==expected,'activation_contract_mismatch')
    study=document(files['study']); capture=study.get('capture',{})
    require(study.get('evaluation_epoch')==EPOCH and study.get('data_basis')=='received_snapshot'
            and type(study.get('capital_policy')) is dict
            and capture.get('version')=='runner-signal-window-v1' and capture.get('study_ref')==EPOCH
            and capture.get('quote_admission')=='signal_created'
            and capture.get('journal_path')==str(ENGINE_STATE/'engine.jsonl')
            and type(capture.get('max_scans')) is int and 1<=capture['max_scans']<=100
            and capture.get('source_version_ref','').split(';')[0]=='commit:'+m['engine_head'],
            'study_contract_mismatch')
    for key,value in dict(start_at='2026-10-08T01:00:00+00:00',admission_end_at='2026-10-08T01:50:00+00:00',
                          end_at='2026-10-08T02:10:00+00:00').items():
        require(capture.get(key)==value,'capture_window_mismatch')


class Host:
    """Only this boundary can invoke host commands or loopback health."""
    def run(self,argv):
        return subprocess.check_output(argv,env=ENV,text=True,timeout=45)
    def git(self,*args):
        return self.run(['/usr/sbin/runuser','-u','ubuntu','--','/usr/bin/env','-i','PATH=/usr/bin:/bin',
                         'HOME=/home/ubuntu','/usr/bin/git','-C',str(REPO),*args])
    def show(self,unit,prop):
        return self.run(['/usr/bin/systemctl','show',unit,'-p',prop,'--value']).strip()
    def command(self,pid):
        return (Path('/proc')/pid/'cmdline').read_bytes()
    def pending(self):
        conn=http.client.HTTPConnection('127.0.0.1',8080,timeout=2)
        try:
            conn.request('GET','/api/health',headers={'Accept-Encoding':'identity'})
            response=conn.getresponse(); data=response.read(65537)
            require(response.status==200 and len(data)<=65536
                    and response.getheader('Content-Encoding') in (None,'identity'),'health_unavailable')
            doc=document(data); broker=doc.get('broker',{}); risk=doc.get('risk_manager',{})
            require(broker.get('connected') is True and all(type(v) is int and v==0 for v in
                    (broker.get('pending_orders'),risk.get('pending_orders'),risk.get('pending_quantities'),
                     risk.get('pending_sells'))),'pending_not_clear')
        finally:
            conn.close()
    def static(self):
        self.run(['/usr/bin/systemd-analyze','verify',str(NEW['activation_service']),str(NEW['activation_timer'])])
        program=("import pathlib,types,sys; p=pathlib.Path("+repr(str(NEW['executor']))+"); "
                 "m=types.ModuleType('prepared_activation'); m.__file__=str(p); sys.modules[m.__name__]=m; "
                 "exec(compile(p.read_bytes(),str(p),'exec'),m.__dict__); m.load_config("+repr(PROFILE)+")")
        self.run(['/usr/bin/python3','-I','-B','-c',program])
        # Complete study semantics are validated by the pinned deployed runtime, as ubuntu.
        program=("import sys; sys.path.insert(0,"+repr(str(REPO))+"); "
                 "from src.analytics.entry_observation_runtime import CapturePlan; "
                 "CapturePlan.load("+repr(str(NEW['engine_manifest']))+")")
        self.run(['/usr/sbin/runuser','-u','ubuntu','--','/usr/bin/env','-i','PATH=/usr/bin:/bin',
                  'PYTHONDONTWRITEBYTECODE=1',str(REPO/'venv/bin/python'),'-I','-B','-c',program])


def runtime_guards(m,host,*,retired=False):
    require(datetime.now(timezone.utc)<NOT_BEFORE,'activation_window_started')
    require(host.git('rev-parse','HEAD').strip()==m['engine_head'],'engine_head_changed')
    require(host.git('status','--porcelain','--untracked-files=all') in
            ('',' M config/evolved_overrides.yml\n'),'unexpected_dirty_tree')
    require(host.show(BOT,'ActiveState')=='active' and host.show(TOSS,'ActiveState')=='inactive','service_not_ready')
    require(host.show(BOT,'MainPID')==m['expected_pid'] and host.show(BOT,'NRestarts')==m['expected_nrestarts']
            and sha(host.command(m['expected_pid']))==m['old_command_sha256'],'bot_process_changed')
    require(host.show(BOT,'DropInPaths')==('' if retired else str(DROPIN)),'bot_dropins_changed')
    actual={str(p.relative_to(REPO)) for directory in ('src','scripts') for p in (REPO/directory).rglob('*.py')}
    require(actual==set(m['source_hashes']),'engine_inventory_changed')
    for name,digest in {**m['source_hashes'],**m['protected_hashes']}.items():
        trusted_read(REPO/name,owner=1000,expected_sha256=digest)
    trusted_read(KILL,owner=1000); host.pending()


def old_inputs(m):
    originals={}
    for p in OLD:
        data=trusted_read(p,owner=1000 if p in (OLD_ONCE,OLD_RECEIPT) else 0)
        require(sha(data)==m['old_files'][str(p)],'old_pin_mismatch'); originals[p]=data
    verify_consumed(originals); return originals


def check_destinations(host):
    for p in (BACKUP,INPUT,ENGINE_STATE,STATE,*NEW.values()):
        require(not p.exists() and not p.is_symlink(),'new_destination_exists')
    require(host.show(ACT_TIMER,'ActiveState') in ('inactive','')
            and host.show(ACT_TIMER,'UnitFileState') in ('','disabled','not-found'),'new_timer_already_armed')
    require(host.show(ACT_SERVICE,'ActiveState') in ('inactive',''),'new_service_not_inactive')


def verify_next_elapse(value):
    require(value in ('Thu 2026-10-08 08:55:00 KST','Wed 2026-10-07 23:55:00 UTC'),
            'timer_next_elapse_mismatch')


def verify_loaded_units(host):
    for key,unit in (('activation_service',ACT_SERVICE),('activation_timer',ACT_TIMER)):
        require(host.show(unit,'FragmentPath')==str(NEW[key]) and host.show(unit,'DropInPaths')=='',
                'loaded_unit_override')


def retire_dropin(original):
    parent=open_dir(DROPIN.parent)
    try:
        require(trusted_read(DROPIN)==original,'consumed_dropin_changed')
        os.unlink(DROPIN.name,dir_fd=parent); os.fsync(parent)
    finally:
        os.close(parent)


def prepare(expected,*,apply=False,host=None):
    host=host or Host(); m=load_manifest(expected)
    require(not BACKUP.exists() and not BACKUP.is_symlink(),'preparation_already_attempted')
    originals=old_inputs(m); runtime_guards(m,host); check_destinations(host)
    files={k:trusted_read(INSTALL/'payload'/k) for k in NEW}; validate_payload(m,files)
    if not apply:
        return dict(checked=True,applied=False,reason='ready')
    lock=os.open(LOCK,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        info=os.fstat(lock)
        require(stat.S_ISREG(info.st_mode) and info.st_uid==1000 and info.st_nlink==1
                and stat.S_IMODE(info.st_mode)==0o644,'unsafe_deployment_lock')
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        runtime_guards(m,host); originals=old_inputs(m); check_destinations(host)
        mkdir_new(BACKUP)
        try:
            write_new(BACKUP/'attempt.json',raw(dict(status='attempt_consumed',manifest_sha256=expected)))
            for i,(path,data) in enumerate(originals.items()): write_new(BACKUP/f'original-{i}',data)
            write_new(BACKUP/'index.json',raw({str(p):dict(file=f'original-{i}',sha256=sha(data))
                      for i,(p,data) in enumerate(originals.items())}))
            for path,owner in ((INPUT,1000),(ENGINE_STATE,1000),(STATE,0)): mkdir_new(path,owner)
            for key,path in NEW.items():
                owner=1000 if key in ('study','engine_manifest','once') else 0
                write_new(path,files[key],0o644 if key in ('executor','activation_service','activation_timer') else 0o600,owner)
            host.static(); runtime_guards(m,host)
            for key,path in NEW.items():
                require(trusted_read(path,owner=1000 if key in ('study','engine_manifest','once') else 0)==files[key],
                        'installed_input_changed')
            old_inputs(m)
            retire_dropin(originals[DROPIN])
            host.run(['/usr/bin/systemctl','daemon-reload'])
            runtime_guards(m,host,retired=True)
            verify_loaded_units(host)
            require(host.show(ACT_TIMER,'Unit')==ACT_SERVICE,'new_timer_target_mismatch')
            host.static()
            runtime_guards(m,host,retired=True)
            host.run(['/usr/bin/systemctl','enable','--now',ACT_TIMER])
            runtime_guards(m,host,retired=True)
            require(host.show(ACT_TIMER,'ActiveState')=='active','timer_not_armed')
            verify_next_elapse(host.show(ACT_TIMER,'NextElapseUSecRealtime'))
            write_new(BACKUP/'complete.json',raw(dict(status='complete',manifest_sha256=expected)))
        except BaseException:
            disarmed=False
            try:
                host.run(['/usr/bin/systemctl','disable','--now',ACT_TIMER])
                disarmed=host.show(ACT_TIMER,'ActiveState') in ('inactive','')
            except BaseException:
                pass
            try:
                write_new(BACKUP/'failed.json',raw(dict(status='operator_review_required',new_timer_disarmed=disarmed)))
            except BaseException:
                pass
            raise GuardError('preparation_partial_disarmed' if disarmed else 'preparation_disarm_failed') from None
    finally:
        os.close(lock)
    return dict(checked=True,applied=True,reason='future_timer_armed',bot_restarted=False,toss_started=False)


def main(argv=None):
    os.umask(0o077); sys.dont_write_bytecode=True
    parser=argparse.ArgumentParser(description=__doc__)
    group=parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--check',action='store_true'); group.add_argument('--apply',action='store_true')
    parser.add_argument('--manifest-sha256',required=True); args=parser.parse_args(argv)
    try:
        require(os.geteuid()==0 and not sys.flags.optimize,'root_nonoptimized_required')
        result=prepare(args.manifest_sha256,apply=args.apply)
    except Exception as exc:
        print(json.dumps(dict(ok=False,reason=str(exc) if isinstance(exc,GuardError) else 'preparation_io_or_validation_failed')))
        return 1
    print(json.dumps(result)); return 0


if __name__=='__main__':
    raise SystemExit(main())
