#!/usr/bin/python3
"""One-shot, engine-only October 22 installation; never deploys or restarts.

Stage manifest.json and payload/<key> under INSTALL, pin the manifest with
--manifest-sha256, then --check or --apply. Failed attempts require manual review.
No Toss configuration, grant, release, retention, or original evidence is changed.
--check validates the pinned payload only; the final runtime loader runs before
timer arming in --apply. Neither path proves memory safety or economic outcomes.

This development candidate does not certify the KRX trading calendar or resource safety.
Explicit resource proof remains required before any installation.

This fixed-date copy deliberately preserves the consumed October 21 pregate installer.
Its privileged transaction stays standalone; a shared/general-date refactor would
change the reviewed historical boundary. Only October 21 pregate evidence may retire
its drop-in, and only the reviewed October 22 paths and times may be installed.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
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

PROFILE = '20261022-pregate-v1'
EPOCH = '2026-10-22-engine-pregate-v1'
INSTALL = Path('/var/lib/qwq-entry-capture/install-20261022')
BACKUP = Path('/var/backups/qwq-entry-capture-20261022')
REPO = Path('/home/ubuntu/projects/qwq-ai-trader')
INPUT = Path('/home/ubuntu/.local/share/qwq-entry-observation') / PROFILE
ENGINE_STATE = Path('/home/ubuntu/.local/state/qwq-entry-observation') / PROFILE
STATE = Path('/var/lib/qwq-entry-capture') / PROFILE
CONFIG = Path('/etc/qwq-entry-capture')
UNITS = Path('/etc/systemd/system')
OLD_ONCE = INPUT.parent / '20261021-pregate-v1/once.json'
OLD_RECEIPT = ENGINE_STATE.parent / '20261021-pregate-v1/startup.receipt'
OLD_STATUS = STATE.parent / '20261021-pregate-v1/status.json'
DROPIN = UNITS / 'qwq-ai-trader.service.d/entry-capture.conf'
OLD = (OLD_ONCE, OLD_RECEIPT, OLD_STATUS, DROPIN)
KILL = Path('/home/ubuntu/.cache/ai_trader/KILL_SWITCH_KR')
LOCK = Path('/tmp/qwq-ai-trader-deploy.lock')
BOT = 'qwq-ai-trader.service'
TOSS = 'qwq-toss-observer.service'
OLD_ACT_SERVICE = 'qwq-entry-capture-20261021.service'
OLD_ACT_TIMER = 'qwq-entry-capture-20261021.timer'
ACT_SERVICE = 'qwq-entry-capture-20261022.service'
ACT_TIMER = 'qwq-entry-capture-20261022.timer'
NOT_BEFORE = datetime(2026,10,22,0,25,tzinfo=timezone.utc)
INSTALL_DEADLINE = datetime(2026,10,22,0,20,tzinfo=timezone.utc)
PROTECTED = ('config/default.yml', 'config/evolved_overrides.yml')
EXECUTOR_SOURCE = 'scripts/ops/entry_capture_activate.py'
NEW = dict(executor=Path('/usr/libexec/qwq-entry-capture/activate-20261022.py'),
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
    return ('[Unit]\nDescription=October 22 engine observation activation\n[Timer]\n'
            'OnCalendar=2026-10-22 09:25:00 Asia/Seoul\nPersistent=false\nAccuracySec=1s\n'
            f'Unit={ACT_SERVICE}\n[Install]\nWantedBy=timers.target\n').encode()


def service_bytes():
    return ('[Unit]\nDescription=October 22 engine observation activation\n[Service]\nType=oneshot\n'
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
            not_before='2026-10-22T00:25:00+00:00',latest_start_at='2026-10-22T00:27:00+00:00'),
            'once_contract_mismatch')
    require(document(files['engine_manifest'])==dict(study_path=str(NEW['study']),study_sha256=sha(files['study'])),
            'study_binding_mismatch')
    expected=dict(old_head=m['engine_head'],new_head=m['engine_head'],source_hashes=m['source_hashes'],
                  protected_hashes=m['protected_hashes'],input_hashes={str(NEW[k]):sha(files[k]) for k in
                  ('study','engine_manifest','once')},dropin_sha256=sha(files['staged_dropin']),
                  not_before='2026-10-22T00:25:00+00:00',latest_start_at='2026-10-22T00:27:00+00:00',
                  toss_uid=997,toss_gid=987,study_sha256=sha(files['study']),evaluation_epoch=EPOCH)
    expected.update({k:str(v) for k,v in activation_paths().items()})
    require(document(files['activation'])==expected,'activation_contract_mismatch')
    study=document(files['study'])
    validate_study(study,m['engine_head'])
    # Fingerprint the exact protected-file inventory (canonical JSON + newline),
    # whose individual file hashes runtime_guards checks against the deployed tree.
    require(study['capture']['configuration_ref']=='sha256:'+sha(raw(m['protected_hashes'])),
            'study_configuration_binding_mismatch')


def _fields(value, names, code='study_contract_mismatch'):
    require(type(value) is dict and set(value)==set(names.split()),code)


def _reference(value):
    require(type(value) is str and 1<=len(value)<=200 and value==value.strip()
            and all(ord(c)>=32 and ord(c)!=127 for c in value),'study_reference_invalid')


def _numeric(value, *, minimum=0, maximum=86400, positive=False):
    require(type(value) in (str,int,float) and len(str(value))<=32,'economic_number_invalid')
    if type(value) is str:
        require(re.fullmatch(r'-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?',value),
                'economic_number_invalid')
    try:
        number=Decimal(str(value))
    except InvalidOperation:
        raise GuardError('economic_number_invalid') from None
    require(number.is_finite() and minimum<=number<=maximum
            and (not positive or number>0),'economic_number_invalid')
    return number


def _fixed_at(value):
    require(type(value) is str and re.fullmatch(
        r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?\+00:00',value),
        'policy_fixed_at_invalid')
    try:
        when=datetime.fromisoformat(value)
    except ValueError:
        raise GuardError('policy_fixed_at_invalid') from None
    require(when<NOT_BEFORE,'policy_fixed_at_invalid')


def validate_study(study,head):
    """고정 관측 범위와 명시 숫자만 검사한다. 실제 경제성 근거를 생성하지 않는다.

    runtime의 전체 의미 검사는 설치된 artifact를 ubuntu CapturePlan.load로 읽는
    Host.static 단계가 담당한다. 이 검사는 원문 비용/자본의 사실성을 인증하지 않는다.
    """
    _fields(study,'schema_version dataset_kind evaluation_epoch as_of data_basis outcome_basis '
            'fee_model_ref fees policy received_policy capital_policy markout_policy capture '
            'observed_regular_flow_policy')
    require(type(study['schema_version']) is int and study['schema_version']==1
            and study['dataset_kind']=='observed' and study['evaluation_epoch']==EPOCH
            and study['as_of']=='2026-10-22T00:51:00+00:00'
            and study['data_basis']=='received_snapshot'
            and study['outcome_basis']=='fixed_horizon_bid_markout','study_contract_mismatch')
    capture=study['capture']
    _fields(capture,'version study_ref start_at admission_end_at end_at scan_admission_ref '
        'source_version_ref configuration_ref fee_evidence_ref capital_evidence_ref capacity_evidence_ref '
        'journal_path buffer_capacity queue_capacity batch_size max_bytes max_record_bytes '
        'open_timeout_seconds close_timeout_seconds channels selection_basis entry_gate_trace '
        'frame_diagnostics record_allocations record_max_bytes journal_budget_version')
    require(capture['journal_budget_version']=='bounded-envelope-v1','journal_budget_version_invalid')
    require(capture['version']=='runner-first-scan-v4' and capture['study_ref']==EPOCH
            and capture['journal_path']==str(ENGINE_STATE/'engine.jsonl'),'study_contract_mismatch')
    for key in ('study_ref','scan_admission_ref','source_version_ref','configuration_ref',
                'fee_evidence_ref','capital_evidence_ref','capacity_evidence_ref'):
        _reference(capture[key])
    require(capture['source_version_ref']=='commit:'+head,'study_source_mismatch')
    require(re.fullmatch(r'sha256:[0-9a-f]{64}',capture['configuration_ref']),
            'study_configuration_reference_invalid')
    for key,value in dict(start_at='2026-10-22T00:30:00+00:00',
            admission_end_at='2026-10-22T00:35:00+00:00',end_at=study['as_of']).items():
        require(capture[key]==value,'capture_window_mismatch')
    fixed=dict(buffer_capacity=100713,queue_capacity=100713,batch_size=100,
               max_record_bytes=65536,max_bytes=240144543)
    require(all(type(capture[k]) is int and capture[k]==v for k,v in fixed.items()),
            'capture_capacity_invalid')
    for key,value in (('open_timeout_seconds',5),('close_timeout_seconds',10)):
        require(type(capture[key]) in (int,float) and capture[key]==value,'capture_timeout_invalid')
    channels=capture['channels']
    _fields(channels,'registration_cap external_reserved operational_headroom max_candidates '
            'lease_seconds evidence_ref market_state_evidence market_state_max_records','channel_contract_mismatch')
    fixed=dict(registration_cap=41,external_reserved=0,operational_headroom=10,max_candidates=5,
               lease_seconds=1262,market_state_max_records=65000)
    require(all(type(channels[k]) is int and channels[k]==v for k,v in fixed.items())
            and channels['market_state_evidence'] is True,'channel_capacity_invalid')
    _reference(channels['evidence_ref'])
    allocations=capture['record_allocations']
    _fields(allocations,'ws_quote market_state other','capture_capacity_invalid')
    require(all(type(allocations[k]) is int and allocations[k]==v for k,v in
            dict(ws_quote=35000,market_state=65000,other=512).items()),'capture_capacity_invalid')
    sizes=capture['record_max_bytes']
    _fields(sizes,'scan_metadata ws_quote market_state other','capture_capacity_invalid')
    require(all(type(sizes[k]) is int and sizes[k]==v for k,v in
            dict(scan_metadata=65536,ws_quote=2048,market_state=2048,other=4096).items()),
            'capture_capacity_invalid')
    selection=capture['selection_basis']
    _fields(selection,'version max_candidates max_source_terms max_modifier_stages')
    require(selection['version']=='selection-basis-v4'
            and type(selection['max_candidates']) is int and selection['max_candidates']==100
            and type(selection['max_source_terms']) is int and selection['max_source_terms']==16
            and type(selection['max_modifier_stages']) is int and selection['max_modifier_stages']==24,
            'selection_contract_invalid')
    gate=capture['entry_gate_trace']
    _fields(gate,'version policy_ref max_candidates source_version_ref configuration_ref')
    require(gate['version']=='entry-gate-trace-v1' and gate['policy_ref']=='kr-live-screening-gates-v1'
            and type(gate['max_candidates']) is int and gate['max_candidates']==100
            and all(gate[k]==capture[k] for k in ('source_version_ref','configuration_ref')),
            'gate_contract_invalid')
    require(capture['frame_diagnostics']=={'version':'kis-frame-diagnostics-v1'},'frame_contract_invalid')
    _reference(study['fee_model_ref'])
    require(study['fee_model_ref']==capture['fee_evidence_ref'],'fee_reference_mismatch')
    fees=study['fees']
    _fields(fees,'buy_commission_rate sell_commission_rate sell_tax_rate')
    rates={k:_numeric(v,maximum=1) for k,v in fees.items()}
    require(rates['buy_commission_rate']<1 and rates['sell_commission_rate']+rates['sell_tax_rate']<1,
            'fee_rate_invalid')
    policy=study['policy']
    _fields(policy,'max_quote_age_seconds max_decision_delay_seconds entry_slippage_bps exit_slippage_bps')
    for key,value in policy.items():
        number=_numeric(value)
        if key.endswith('_bps'): require(number<10000,'slippage_invalid')
    received=study['received_policy']
    _fields(received,'version max_rest_rtt_seconds max_target_age_seconds')
    require(received['version']=='received-v1','received_policy_invalid')
    for key in ('max_rest_rtt_seconds','max_target_age_seconds'): _numeric(received[key])
    capital=study['capital_policy']
    _fields(capital,'version policy_ref fixed_at source_version_ref configuration_ref '
            'max_snapshot_age_seconds risk_per_trade_pct max_position_pct')
    require(capital['version']=='pre-pending-guards-v1'
            and capital['policy_ref']==capture['capital_evidence_ref']
            and all(capital[k]==capture[k] for k in ('source_version_ref','configuration_ref')),
            'capital_contract_invalid')
    _reference(capital['policy_ref']); _fixed_at(capital['fixed_at'])
    _numeric(capital['max_snapshot_age_seconds'])
    for key in ('risk_per_trade_pct','max_position_pct'): _numeric(capital[key],maximum=100,positive=True)
    markout=study['markout_policy']
    _fields(markout,'version policy_ref fixed_at horizon_seconds max_delay_seconds anchor')
    require(markout['version']=='fixed-horizon-bid-v1'
            and type(markout['horizon_seconds']) is int and markout['horizon_seconds']==900
            and _numeric(markout['max_delay_seconds'])==5
            and markout['anchor']=='entry_quote_observed_at','markout_contract_invalid')
    _reference(markout['policy_ref']); _fixed_at(markout['fixed_at'])
    observed=study['observed_regular_flow_policy']
    _fields(observed,'version fixed_at preparation_seconds state_max_age_seconds '
            'horizon_seconds max_markout_delay_seconds','observed_flow_policy_invalid')
    fixed=dict(preparation_seconds=5,state_max_age_seconds=5,horizon_seconds=900,
               max_markout_delay_seconds=5)
    require(observed['version']=='pregate-observed-regular-flow-v1'
            and all(type(observed[k]) is int and observed[k]==v for k,v in fixed.items()),
            'observed_flow_policy_invalid')
    _fixed_at(observed['fixed_at'])


MIN_DISK_AVAILABLE_BYTES = 1336233984


def check_disk_capacity(host):
    available=host.disk_available(ENGINE_STATE)
    require(type(available) is int and available>=MIN_DISK_AVAILABLE_BYTES,
            'journal_disk_capacity_insufficient')



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
    def disk_available(self,path):
        """원장과 같은 파일시스템의 비예약 가용 공간; symlink 없는 fd 기준."""
        path=Path(path)
        target=path if path.exists() or path.is_symlink() else path.parent
        fd=open_dir(target,1000)
        try:
            info=os.fstatvfs(fd)
            return info.f_bavail*info.f_frsize
        finally:
            os.close(fd)
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
    require(datetime.now(timezone.utc)<INSTALL_DEADLINE,'activation_window_started')
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
    # Future INPUT/STATE directories are created by this transaction; every
    # other parent must already be trusted before consuming its one attempt.
    created={BACKUP:0,INPUT:1000,ENGINE_STATE:1000,STATE:0}
    parents={p.parent:owner for p,owner in created.items()}
    parents.update({p.parent:0 for p in NEW.values() if p.parent not in created})
    for parent,owner in parents.items():
        try:
            fd=open_dir(parent,owner)
            os.close(fd)
        except (OSError,GuardError):
            raise GuardError('destination_parent_unavailable') from None
    for p in (BACKUP,INPUT,ENGINE_STATE,STATE,*NEW.values()):
        require(not p.exists() and not p.is_symlink(),'new_destination_exists')
    require(host.show(ACT_TIMER,'ActiveState') in ('inactive','')
            and host.show(ACT_TIMER,'UnitFileState') in ('','disabled','not-found'),'new_timer_already_armed')
    require(host.show(ACT_SERVICE,'ActiveState') in ('inactive',''),'new_service_not_inactive')


def verify_next_elapse(value):
    require(value in ('Thu 2026-10-22 09:25:00 KST','Thu 2026-10-22 00:25:00 UTC'),
            'timer_next_elapse_mismatch')


def verify_loaded_units(host):
    for key,unit in (('activation_service',ACT_SERVICE),('activation_timer',ACT_TIMER)):
        require(host.show(unit,'FragmentPath')==str(NEW[key]) and host.show(unit,'DropInPaths')=='',
                'loaded_unit_override')


def predecessor_guards(host, *, retired=False):
    # 소비된 October 21 프로필만 퇴역한다. 종료된 타이머도 enabled/active 상태일 수 있다.
    require(host.show(OLD_ACT_SERVICE,'ActiveState')=='inactive','predecessor_service_active')
    require(host.show(OLD_ACT_TIMER,'FragmentPath')==str(UNITS/OLD_ACT_TIMER)
            and host.show(OLD_ACT_TIMER,'DropInPaths')==''
            and host.show(OLD_ACT_TIMER,'Unit')==OLD_ACT_SERVICE,'predecessor_timer_changed')
    active,enabled=host.show(OLD_ACT_TIMER,'ActiveState'),host.show(OLD_ACT_TIMER,'UnitFileState')
    require((active=='inactive' and enabled=='disabled') if retired else
            (active in ('active','inactive') and enabled in ('enabled','disabled')),
            'predecessor_timer_not_retired')


def retire_dropin(original):
    parent=open_dir(DROPIN.parent)
    try:
        before=os.stat(DROPIN.name,dir_fd=parent,follow_symlinks=False)
        require(trusted_read(DROPIN)==original,'consumed_dropin_changed')
        after=os.stat(DROPIN.name,dir_fd=parent,follow_symlinks=False)
        require((before.st_dev,before.st_ino,before.st_ctime_ns)==
                (after.st_dev,after.st_ino,after.st_ctime_ns),'consumed_dropin_changed')
        os.unlink(DROPIN.name,dir_fd=parent); os.fsync(parent)
    finally:
        os.close(parent)


def prepare(expected,*,apply=False,host=None):
    require(datetime.now(timezone.utc)<INSTALL_DEADLINE,'installation_deadline_passed')
    host=host or Host(); m=load_manifest(expected)
    require(not BACKUP.exists() and not BACKUP.is_symlink(),'preparation_already_attempted')
    originals=old_inputs(m); runtime_guards(m,host); predecessor_guards(host); check_destinations(host); check_disk_capacity(host)
    files={k:trusted_read(INSTALL/'payload'/k) for k in NEW}; validate_payload(m,files)
    if not apply:
        return dict(checked=True,applied=False,reason='payload_validated_pending_runtime_validation',
                    runtime_semantics_verified=False,memory_safety_verified=False)
    lock=os.open(LOCK,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        info=os.fstat(lock)
        require(stat.S_ISREG(info.st_mode) and info.st_uid==1000 and info.st_nlink==1
                and stat.S_IMODE(info.st_mode)==0o644,'unsafe_deployment_lock')
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        runtime_guards(m,host); originals=old_inputs(m); predecessor_guards(host); check_destinations(host); check_disk_capacity(host)
        mkdir_new(BACKUP)
        old_timer_disabled=False
        old_dropin_retired=False
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
            old_inputs(m); predecessor_guards(host)
            host.run(['/usr/bin/systemctl','disable','--now',OLD_ACT_TIMER])
            predecessor_guards(host,retired=True)
            old_timer_disabled=True
            runtime_guards(m,host); old_inputs(m)
            retire_dropin(originals[DROPIN])
            old_dropin_retired=True
            host.run(['/usr/bin/systemctl','daemon-reload'])
            runtime_guards(m,host,retired=True)
            verify_loaded_units(host)
            require(host.show(ACT_TIMER,'Unit')==ACT_SERVICE,'new_timer_target_mismatch')
            host.static()
            runtime_guards(m,host,retired=True)
            check_disk_capacity(host)
            host.run(['/usr/bin/systemctl','enable','--now',ACT_TIMER])
            runtime_guards(m,host,retired=True)
            predecessor_guards(host,retired=True)
            require(host.show(ACT_TIMER,'ActiveState')=='active'
                    and host.show(ACT_TIMER,'UnitFileState')=='enabled','timer_not_armed')
            verify_next_elapse(host.show(ACT_TIMER,'NextElapseUSecRealtime'))
            write_new(BACKUP/'complete.json',raw(dict(status='complete',manifest_sha256=expected)))
        except BaseException:
            disarmed=False
            try:
                host.run(['/usr/bin/systemctl','disable','--now',ACT_TIMER])
                disarmed=(host.show(ACT_TIMER,'ActiveState') in ('inactive','')
                          and host.show(ACT_TIMER,'UnitFileState')=='disabled')
            except BaseException:
                pass
            try:
                write_new(BACKUP/'failed.json',raw(dict(status='operator_review_required',new_timer_disarmed=disarmed,
                    old_timer_disabled=old_timer_disabled,old_dropin_retired=old_dropin_retired)))
            except BaseException:
                pass
            raise GuardError('preparation_partial_disarmed' if disarmed else 'preparation_disarm_failed') from None
    finally:
        os.close(lock)
    return dict(checked=True,applied=True,reason='future_timer_armed',bot_restarted=False,toss_started=False,
                runtime_semantics_verified=True,memory_safety_verified=False)


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
