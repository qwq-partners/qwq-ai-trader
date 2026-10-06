#!/usr/bin/python3
"""Fixed October 7 preparation. Never starts/restarts the bot or observer.

Root stages manifest.json, payload/<key>, artifact/ under INSTALL. The operator
pins manifest bytes with --manifest-sha256. A failed apply consumes the attempt:
inspect the private backup/status before any manual recovery; no automatic retry.
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

INSTALL = Path('/var/lib/qwq-entry-capture/install-20261007')
REPO = Path('/home/ubuntu/projects/qwq-ai-trader')
HEAD = '4384b2822b609736797e452cb6819f9b0211497d'
ARTIFACT = 'a88c4c3db8f7079ad2c15c0ea0a098e2b889b312d90a5732488b0809567cef93'
PROFILE = '20261007-pilot3'
ETC = Path('/etc/qwq-toss-observer')
UNITS = Path('/etc/systemd/system')
BACKUP = Path('/var/backups/qwq-entry-capture-20261007')
RELEASE = Path('/opt/qwq-toss-observer/releases') / ARTIFACT
LAUNCHER = Path('/usr/libexec/qwq-toss-observer/launcher.py')
RETENTION = LAUNCHER.with_name('retention.py')
INPUT = Path('/home/ubuntu/.local/share/qwq-entry-observation') / PROFILE
ENGINE_STATE = Path('/home/ubuntu/.local/state/qwq-entry-observation') / PROFILE
STATE = Path('/var/lib/qwq-entry-capture') / PROFILE
CONFIG = Path('/etc/qwq-entry-capture') / PROFILE
OLD_ONCE = INPUT.parent / '20261006-pilot2/once.json'
OLD_RECEIPT = ENGINE_STATE.parent / '20261006-pilot2/startup.receipt'
OLD_STATUS = STATE.parent / '20261006-pilot2/status.json'
DROPIN = UNITS / 'qwq-ai-trader.service.d/entry-capture.conf'
LOCK = Path('/tmp/qwq-ai-trader-deploy.lock')
KILL = Path('/home/ubuntu/.cache/ai_trader/KILL_SWITCH_KR')
BOT = 'qwq-ai-trader.service'
TOSS = 'qwq-toss-observer.service'
ACT_SERVICE = 'qwq-entry-capture-20261007.service'
ACT_TIMER = 'qwq-entry-capture-20261007.timer'
RET_SERVICE = 'qwq-toss-observer-retention-20261007.service'
RET_TIMER = 'qwq-entry-capture-retention-20261106.timer'
NOT_BEFORE = datetime(2026, 10, 6, 23, 55, tzinfo=timezone.utc)
SNAPSHOT = ETC / 'retention/20261007'
COHORTS = Path('/var/lib/qwq-toss-observer/cohorts')
SHARED = {k: ETC / (k+'.json') for k in ('deployment','plan','registry')}
NEW = dict(SHARED, executor=Path('/usr/libexec/qwq-entry-capture/activate-20261007.py'),
    study=INPUT/'study.json', engine_manifest=INPUT/'manifest.json', once=INPUT/'once.json',
    activation=CONFIG/'activation.json', staged_dropin=CONFIG/'entry-capture.conf',
    activation_service=UNITS/ACT_SERVICE, activation_timer=UNITS/ACT_TIMER,
    retention_service=UNITS/RET_SERVICE, retention_timer=UNITS/RET_TIMER)
FROZEN_NAMES = ('deployment.json','plan.json','registry.json','launcher.py','retention.py','hashes.json')
ENV = {'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LANG':'C','LC_ALL':'C','PYTHONDONTWRITEBYTECODE':'1'}

class GuardError(Exception):
    pass

def require(ok, code='preparation_guard_failed'):
    if not ok:
        raise GuardError(code)

def sha(data):
    return hashlib.sha256(data).hexdigest()

def raw(doc):
    return (json.dumps(doc, sort_keys=True, separators=(',',':'), ensure_ascii=True, allow_nan=False)+'\n').encode()

def document(data):
    def pairs(items):
        result = {}
        for k,v in items:
            require(k not in result, 'duplicate_json_key')
            result[k] = v
        return result
    return json.loads(data, object_pairs_hook=pairs,
                      parse_constant=lambda _:require(False, 'nonfinite_json'))

def hash_value(value):
    require(type(value) is str and re.fullmatch('[0-9a-f]{64}', value), 'invalid_hash')

def engine_file_path(path):
    try:
        name = str(path.relative_to(REPO))
    except ValueError:
        return False
    return name in ('config/default.yml','config/evolved_overrides.yml') or bool(
        re.fullmatch(r'(src|scripts)/[a-zA-Z0-9_./-]+\.py', name))

def trusted_read(path, owner=0, limit=64*1024*1024, *, expected_sha256=None):
    """Root files stay strict. Ubuntu source writes require a content pin.

    The fixed Ubuntu group was confirmed to contain only Ubuntu before install.
    Its established umask002 layout is permitted only within these read scopes;
    once/receipt leaves remain non-group-writable and all world writes fail.
    """
    path = Path(path)
    require(path.is_absolute() and '..' not in path.parts)
    source = engine_file_path(path)
    ubuntu_scope = owner == 1000 and (source or path in (OLD_ONCE,OLD_RECEIPT,KILL))
    if expected_sha256 is not None:
        hash_value(expected_sha256)
        require(owner == 1000 and source, 'invalid_pinned_read_scope')
    cursor = Path('/')
    fd = os.open('/', os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        for i, name in enumerate(path.parts[1:]):
            last = i == len(path.parts)-2
            child = os.open(name, os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|
                            (0 if last else os.O_DIRECTORY), dir_fd=fd)
            os.close(fd); fd = child
            info = os.fstat(fd)
            cursor = cursor/name
            ubuntu_group = ubuntu_scope and info.st_uid == info.st_gid == 1000
            group_write_ok = ubuntu_group and (
                (not last and cursor.is_relative_to('/home/ubuntu')) or
                (last and (expected_sha256 is not None or path == KILL)))
            require(info.st_uid in ((0,owner) if not last else (owner,))
                    and info.st_gid in ((0,owner) if not last else (owner,))
                    and not info.st_mode & (0o002 if group_write_ok else 0o022),
                    'unsafe_file_ownership')
            if last:
                require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= limit,
                        'unsafe_file_type')
            else:
                require(stat.S_ISDIR(info.st_mode), 'unsafe_parent')
        before = os.fstat(fd)
        with os.fdopen(os.dup(fd), 'rb') as stream:
            data = stream.read(limit+1)
        after = os.fstat(fd)
        require(len(data) <= limit and (before.st_size,before.st_mtime_ns,before.st_ctime_ns) ==
                (after.st_size,after.st_mtime_ns,after.st_ctime_ns), 'file_changed')
        if expected_sha256 is not None:
            require(sha(data) == expected_sha256, 'pinned_file_changed')
        return data
    finally:
        os.close(fd)

def trusted_dir(path, owner=0):
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == owner and not info.st_mode & 0o022,
            'unsafe_directory')

def sync_dir(path):
    fd = os.open(path, os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

def write_new(path, data, mode=0o644, owner=0, group=0):
    fd = os.open(path, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, mode)
    with os.fdopen(fd, 'wb') as stream:
        if os.geteuid() == 0:
            os.fchown(stream.fileno(), owner, group)
        os.fchmod(stream.fileno(), mode)
        stream.write(data); stream.flush(); os.fsync(stream.fileno())
    sync_dir(path.parent)

def mkdir_new(path, mode=0o755, owner=0, group=0):
    if path.parent == COHORTS:
        # The installed observer owns this one private state namespace.
        require(re.fullmatch('[0-9a-f]{64}', path.name) and
                (mode,owner,group) == (0o700,997,987), 'cohort_contract_mismatch')
        parent_fd = os.open(COHORTS, os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        child_fd = None
        try:
            parent = os.fstat(parent_fd)
            require(stat.S_ISDIR(parent.st_mode) and parent.st_uid == 997 and parent.st_gid == 987
                    and stat.S_IMODE(parent.st_mode) == 0o700, 'unsafe_cohorts_directory')
            os.mkdir(path.name, mode=0o700, dir_fd=parent_fd)
            child_fd = os.open(path.name, os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW, dir_fd=parent_fd)
            child = os.fstat(child_fd)
            require(child.st_uid == os.geteuid() and stat.S_IMODE(child.st_mode) == 0o700,
                    'cohort_creation_replaced')
            os.fchown(child_fd, 997, 987); os.fchmod(child_fd, 0o700); os.fsync(child_fd)
            visible = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            require(stat.S_ISDIR(visible.st_mode) and (visible.st_dev,visible.st_ino) ==
                    (child.st_dev,child.st_ino), 'cohort_creation_replaced')
            os.fsync(parent_fd)
            visible_parent = os.stat(COHORTS, follow_symlinks=False)
            require(stat.S_ISDIR(visible_parent.st_mode) and
                    (visible_parent.st_dev,visible_parent.st_ino) == (parent.st_dev,parent.st_ino),
                    'cohorts_namespace_replaced')
        finally:
            if child_fd is not None:
                os.close(child_fd)
            os.close(parent_fd)
        return
    else:
        trusted_dir(path.parent, owner if str(path).startswith('/home/ubuntu/') else 0)
    path.mkdir(mode=mode)
    os.chown(path, owner, group); os.chmod(path, mode); sync_dir(path.parent)

def replace_set(replacements, originals, validate, *, read=None):
    """Rollback includes a just-renamed file even when its directory fsync fails."""
    read = read or trusted_read
    swapped = []
    try:
        for path, data in replacements.items():
            require(read(path) == originals[path], 'old_config_changed')
            stage = path.with_name(path.name+'.20261007-stage')
            write_new(stage, data)
            require(read(path) == originals[path], 'old_config_changed')
            os.replace(stage, path); swapped.append(path); sync_dir(path.parent)
        validate()
    except BaseException:
        failed = False
        for path in reversed(swapped):
            try:
                stage = path.with_name(path.name+'.20261007-restore')
                write_new(stage, originals[path]); os.replace(stage, path); sync_dir(path.parent)
            except BaseException:
                failed = True
        if failed:
            raise GuardError('configuration_rollback_incomplete') from None
        raise

def verify_bindings(actual, expected):
    def parse(text):
        result = []
        for item in text.split():
            parts = item.split(':')
            require(len(parts) in (2,3) and parts[0].startswith('/') and parts[1].startswith('/')
                    and (len(parts) == 2 or parts[2] == 'rbind'), 'retention_binding_invalid')
            result.append(tuple(parts[:2]))
        require(len(result) == len(set(result)), 'retention_binding_duplicate')
        return set(result)
    require(parse(actual) == parse(expected), 'retention_bindings_changed')

def dropin(once):
    return ('[Service]\nExecStart=\nExecStart=/home/ubuntu/projects/qwq-ai-trader/venv/bin/python '
            'scripts/run_trader.py --market kr --config '
            '/home/ubuntu/projects/qwq-ai-trader/config/default.yml '
            f'--entry-observation-once {once}\n').encode()

def verify_consumed(once, receipt, status, old_dropin):
    req, rec, done = map(document, (once,receipt,status))
    require(req.get('version') == 'entry-observation-once-v1'
            and req.get('receipt_path') == str(OLD_RECEIPT)
            and req.get('manifest_path') == str(OLD_ONCE.with_name('manifest.json'))
            and rec.get('version') == 'entry-observation-once-receipt-v1'
            and rec.get('status') == 'attempt_consumed'
            and done.get('status') == 'complete' and done.get('restart_attempted') is True
            and old_dropin == dropin(OLD_ONCE), 'old_attempt_not_exactly_consumed')

def verify_window(plan, grant):
    require(plan.get('dates') == ['2026-10-07'], 'observation_date_mismatch')
    window = plan.get('websocket', {})
    for key,value in {'start_at':'2026-10-07T00:15:00+00:00',
                      'scan_until':'2026-10-07T00:17:30+00:00',
                      'end_at':'2026-10-07T00:33:00+00:00'}.items():
        require(window.get(key) == value, 'observation_window_mismatch')
    require(grant.get('not_before') == '2026-10-06T23:55:00+00:00' and
            grant.get('expires_at') == '2026-10-07T00:34:00+00:00', 'grant_window_mismatch')

def verify_registry(old, new):
    grants = old.get('grants')
    require(type(grants) is list and len(grants)>0 and new.get('schema_version') == old.get('schema_version')
            and type(new.get('grants')) is list and len(new['grants']) == len(grants)+1
            and new['grants'][:-1] == grants
            and new['grants'][-1].get('grant_id') == 'toss-entry-20261007-pilot3',
            'registry_history_changed')

def load_manifest(expected):
    hash_value(expected)
    data = trusted_read(INSTALL/'manifest.json', limit=1024*1024)
    require(sha(data) == expected, 'manifest_pin_mismatch')
    m = document(data)
    keys = {'schema_version','profile','engine_head','expected_pid','expected_nrestarts',
            'old_command_sha256','source_hashes','protected_hashes','old_files','new_files','old_retention'}
    require(set(m) == keys and m['schema_version'] == 1 and m['profile'] == PROFILE
            and m['engine_head'] == HEAD and m['expected_pid'] == '504793'
            and m['expected_nrestarts'] == '0', 'manifest_contract_mismatch')
    hash_value(m['old_command_sha256'])
    require(set(m['new_files']) == set(NEW), 'payload_inventory_mismatch')
    require(set(m['old_files']) == {str(p) for p in (*SHARED.values(), LAUNCHER, RETENTION,
                         OLD_ONCE, OLD_RECEIPT, OLD_STATUS, DROPIN)}, 'old_inventory_mismatch')
    require(set(m['protected_hashes']) == {'config/default.yml','config/evolved_overrides.yml'},
            'config_inventory_mismatch')
    require(len(m['source_hashes']) == 286 and 'scripts/run_trader.py' in m['source_hashes'],
            'engine_inventory_mismatch')
    for name in m['source_hashes']:
        require(re.fullmatch(r'(src|scripts)/[a-zA-Z0-9_./-]+\.py', name)
                and '..' not in Path(name).parts and str(Path(name)) == name, 'engine_path_invalid')
    for group in ('new_files','old_files','protected_hashes','source_hashes'):
        for value in m[group].values():
            hash_value(value)
    require(set(m['old_retention']) == {'20261002','20261006'}, 'retention_inventory_mismatch')
    for date, expiry in (('20261002','20261101'),('20261006','20261105')):
        entry = m['old_retention'][date]
        expected_paths = {str(ETC/'retention'/date/n) for n in FROZEN_NAMES} | {
            str(UNITS/f'qwq-toss-observer-retention-{date}.service'),
            str(UNITS/f'qwq-entry-capture-retention-{expiry}.timer')}
        require(set(entry) == {'files','bindings'} and set(entry['files']) == expected_paths,
                'retention_inventory_mismatch')
        verify_bindings(entry['bindings'], bindings(ETC/'retention'/date))
        for value in entry['files'].values():
            hash_value(value)
    return m

def bindings(bundle):
    targets = dict((name, ETC/name) for name in FROZEN_NAMES[:3])
    targets.update({'launcher.py':LAUNCHER, 'retention.py':RETENTION})
    return ' '.join(f'{bundle/name}:{path}' for name,path in targets.items())

class Host:
    """All external reads/commands live here so offline tests inject a fake host."""
    def run(self, argv):
        return subprocess.check_output(argv, env=ENV, text=True, timeout=45).strip()
    def show(self, unit, prop):
        return self.run(['/usr/bin/systemctl','show',unit,'-p',prop,'--value'])
    def head(self):
        return self.run(['/usr/sbin/runuser','-u','ubuntu','--','/usr/bin/git','-C',str(REPO),'rev-parse','HEAD'])
    def command(self, pid):
        return (Path('/proc')/pid/'cmdline').read_bytes()
    def pending(self):
        conn = http.client.HTTPConnection('127.0.0.1',8080,timeout=2)
        try:
            conn.request('GET','/api/health',headers={'Accept-Encoding':'identity'})
            response = conn.getresponse(); data = response.read(65537)
            require(response.status == 200 and len(data)<=65536
                    and response.getheader('Content-Encoding') in (None,'identity'), 'health_unavailable')
            doc = document(data); broker = doc.get('broker',{}); risk = doc.get('risk_manager',{})
            require(broker.get('connected') is True and all(type(x) is int and x==0 for x in
                    [broker.get('pending_orders'),risk.get('pending_orders'),risk.get('pending_quantities'),
                     risk.get('pending_sells')]), 'pending_not_clear')
        finally:
            conn.close()
    def static(self):
        self.run(['/usr/bin/systemd-analyze','verify',*[str(NEW[k]) for k in
            ('activation_service','activation_timer','retention_service','retention_timer')]])
        program = ("import pathlib; p=pathlib.Path('/usr/libexec/qwq-toss-observer/launcher.py'); "
                   "ns={'__file__':str(p)}; exec(compile(p.read_bytes(),str(p),'exec'),ns); "
                   "ns['load_verified_document'](pathlib.Path('/etc/qwq-toss-observer/deployment.json'))")
        self.run(['/usr/sbin/runuser','-u','qwq-toss-observer','-g','qwq-toss-observer','--',
                  '/usr/bin/env','-i','PATH=/usr/bin:/bin','PYTHONDONTWRITEBYTECODE=1',
                  '/usr/bin/python3','-I','-S','-B','-c',program])
        program = ("import pathlib,types,sys; p=pathlib.Path('/usr/libexec/qwq-entry-capture/activate-20261007.py'); "
                   "m=types.ModuleType('prepared_activation'); m.__file__=str(p); sys.modules[m.__name__]=m; "
                   "exec(compile(p.read_bytes(),str(p),'exec'),m.__dict__); m.load_config('20261007-pilot3')")
        self.run(['/usr/bin/python3','-I','-B','-c',program])

def runtime_guards(m, host, *, retired=False):
    require(datetime.now(timezone.utc) < NOT_BEFORE, 'activation_window_started')
    require(host.head() == HEAD, 'engine_head_changed')
    require(host.show(BOT,'ActiveState') == 'active' and host.show(TOSS,'ActiveState') == 'inactive',
            'service_not_ready')
    require(host.show(BOT,'MainPID') == m['expected_pid'] and
            host.show(BOT,'NRestarts') == m['expected_nrestarts'] and
            sha(host.command(m['expected_pid'])) == m['old_command_sha256'], 'bot_process_changed')
    require(host.show(BOT,'DropInPaths') == ('' if retired else str(DROPIN)), 'bot_dropins_changed')
    actual_sources = {str(p.relative_to(REPO)) for directory in ('src','scripts')
                      for p in (REPO/directory).rglob('*.py')}
    require(actual_sources == set(m['source_hashes']), 'engine_inventory_changed')
    for name, digest in {**m['source_hashes'], **m['protected_hashes']}.items():
        require(sha(trusted_read(REPO/name,owner=1000,expected_sha256=digest)) == digest, 'engine_bytes_changed')
    trusted_read(KILL,owner=1000)
    host.pending()

def old_retention(m, host):
    for date, expiry in (('20261002','20261101'),('20261006','20261105')):
        entry = m['old_retention'][date]
        for path, digest in entry['files'].items():
            require(sha(trusted_read(Path(path))) == digest, 'old_retention_changed')
        service = f'qwq-toss-observer-retention-{date}.service'
        require(host.show(f'qwq-entry-capture-retention-{expiry}.timer','Unit') == service,
                'old_retention_timer_changed')
        verify_bindings(host.show(service,'BindReadOnlyPaths'),entry['bindings'])

def inventory(root):
    trusted_dir(root)
    require(stat.S_IMODE(root.stat().st_mode) == 0o755, 'artifact_directory_mode')
    manifest_data = trusted_read(root/'manifest.json')
    manifest = document(manifest_data)
    require(sha(raw(manifest).rstrip(b'\n')) == ARTIFACT and
            manifest['release_id'] == 'kr-entry-20261007-v4', 'artifact_identity_mismatch')
    entries = manifest['files']; require(type(entries) is list and 0 < len(entries) <= 20000)
    indexed = {}
    for item in entries:
        name = item['path']; path = Path(name)
        require(not path.is_absolute() and '..' not in path.parts and str(path) == name
                and name not in indexed and item['mode'] == 0o644, 'artifact_path_invalid')
        data = trusted_read(root/path)
        require(stat.S_IMODE((root/path).stat().st_mode) == 0o644, 'artifact_file_mode')
        require(len(data)==item['size'] and sha(data)==item['sha256'], 'artifact_file_mismatch')
        indexed[name] = data
    actual = set()
    for path in root.rglob('*'):
        info = path.lstat()
        require(not stat.S_ISLNK(info.st_mode), 'artifact_symlink')
        if stat.S_ISREG(info.st_mode):
            actual.add(str(path.relative_to(root)))
        else:
            trusted_dir(path)
            require(stat.S_IMODE(info.st_mode) == 0o755, 'artifact_directory_mode')
    require(actual == set(indexed)|{'manifest.json'}, 'artifact_inventory_mismatch')
    return manifest_data, indexed

def payload(m, originals):
    files = {k:trusted_read(INSTALL/'payload'/k) for k in NEW}
    for key,data in files.items():
        require(sha(data)==m['new_files'][key], 'payload_pin_mismatch')
    old = document(originals[SHARED['deployment']]); new = document(files['deployment'])
    require(old['grant_id']=='toss-entry-20261006-pilot2-r2' and
            new['grant_id']=='toss-entry-20261007-pilot3' and new['release_id']=='kr-entry-20261007-v4'
            and new['artifact_sha256']==ARTIFACT and new['release_root']==str(RELEASE)
            and new['manifest_path']==str(RELEASE/'manifest.json')
            and new['retention_at']=='2026-11-06T00:34:00+00:00', 'deployment_contract_mismatch')
    for key in ('token_directory','sender_lock_path','receipt_directory','state_directory',
                'client_identity','host_identity','service_uid','service_gid','service_gids','policy'):
        require(new[key]==old[key], 'protected_deployment_changed')
    verify_registry(document(originals[SHARED['registry']]),document(files['registry']))
    verify_window(document(files['plan']), document(files['registry'])['grants'][-1])
    require(files['staged_dropin']==dropin(INPUT/'once.json'), 'new_dropin_mismatch')
    activation = document(files['activation'])
    require(activation['old_head']==activation['new_head']==HEAD and
            activation['source_hashes']==m['source_hashes'] and
            activation['protected_hashes']==m['protected_hashes'], 'activation_engine_mismatch')
    expected_inputs = {str(NEW[k]):sha(files[k]) for k in
                       ('study','engine_manifest','once','deployment','plan','registry')}
    expected_inputs[str(LAUNCHER)] = sha(originals[LAUNCHER])
    require(activation['input_hashes']==expected_inputs, 'activation_inputs_mismatch')
    require(b'OnCalendar=2026-10-07 08:55:00 Asia/Seoul\n' in files['activation_timer']
            and b'Persistent=false\n' in files['activation_timer']
            and f'Unit={ACT_SERVICE}\n'.encode() in files['activation_timer'], 'activation_timer_mismatch')
    require(b'OnCalendar=2026-11-06 09:34:10 Asia/Seoul\n' in files['retention_timer']
            and f'Unit={RET_SERVICE}\n'.encode() in files['retention_timer'], 'retention_timer_mismatch')
    require(('BindReadOnlyPaths='+bindings(SNAPSHOT)+'\n').encode() in files['retention_service'],
            'new_retention_bindings_mismatch')
    return files

def prepare(expected, *, apply=False, host=None):
    host = host or Host()
    m = load_manifest(expected)
    require(not BACKUP.exists() and not BACKUP.is_symlink(), 'preparation_already_attempted')
    originals = {}
    for path,digest in m['old_files'].items():
        p = Path(path); data = trusted_read(p,owner=1000 if p in (OLD_ONCE,OLD_RECEIPT) else 0)
        require(sha(data)==digest, 'old_pin_mismatch'); originals[p] = data
    verify_consumed(originals[OLD_ONCE], originals[OLD_RECEIPT], originals[OLD_STATUS], originals[DROPIN])
    runtime_guards(m,host); old_retention(m,host)
    files = payload(m,originals)
    manifest_data, release_files = inventory(INSTALL/'artifact')
    new_deployment = document(files['deployment'])
    hash_value(new_deployment['plan_canonical_hash'])
    cohort = Path(new_deployment['ledger_path']).parent
    require(cohort == COHORTS/new_deployment['plan_canonical_hash']
            and new_deployment['ledger_path']==str(cohort/'observations.jsonl')
            and new_deployment['status_path']==str(cohort/'status.json'), 'cohort_path_mismatch')
    for p in (INPUT,ENGINE_STATE,STATE,CONFIG,SNAPSHOT,RELEASE,cohort,
              *(p for k,p in NEW.items() if k not in SHARED)):
        require(not p.exists() and not p.is_symlink(), 'new_destination_exists')
    for timer in (ACT_TIMER,RET_TIMER):
        require(host.show(timer,'ActiveState') in ('inactive','') and
                host.show(timer,'UnitFileState') in ('','disabled','not-found'), 'new_timer_already_armed')
    if not apply:
        return {'checked':True,'applied':False,'reason':'ready'}
    lock = os.open(LOCK, os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        info = os.fstat(lock)
        require(stat.S_ISREG(info.st_mode) and info.st_uid==1000 and info.st_nlink==1
                and stat.S_IMODE(info.st_mode)==0o644, 'unsafe_deployment_lock')
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        runtime_guards(m,host); old_retention(m,host)
        mkdir_new(BACKUP,0o700)
        write_new(BACKUP/'attempt.json',raw({'status':'attempt_consumed','manifest_sha256':expected}),0o600)
        try:
            for i,(path,data) in enumerate(originals.items()):
                write_new(BACKUP/f'original-{i}',data,0o600)
            write_new(BACKUP/'index.json',raw({str(p):{'file':f'original-{i}','sha256':sha(b)}
                      for i,(p,b) in enumerate(originals.items())}),0o600)
            mkdir_new(RELEASE)
            for name,data in release_files.items():
                destination = RELEASE/name
                missing = []; parent = destination.parent
                while parent != RELEASE and not parent.exists():
                    missing.append(parent); parent = parent.parent
                for directory in reversed(missing):
                    mkdir_new(directory)
                write_new(destination,data)
            write_new(RELEASE/'manifest.json',manifest_data)
            inventory(RELEASE)
            mkdir_new(cohort,0o700,997,987)
            for directory,uid,gid in ((INPUT,1000,1000),(ENGINE_STATE,1000,1000),
                                      (STATE,0,0),(CONFIG,0,0)):
                mkdir_new(directory,0o700,uid,gid)
            for key,path in NEW.items():
                if key not in SHARED:
                    user = 1000 if key in ('study','engine_manifest','once') else 0
                    write_new(path, files[key],0o600 if key in ('study','engine_manifest','once',
                              'activation','staged_dropin') else 0o644,user,user)
            mkdir_new(SNAPSHOT)
            frozen = {k+'.json':files[k] for k in SHARED}
            frozen.update({'launcher.py':originals[LAUNCHER],'retention.py':originals[RETENTION]})
            for name,data in frozen.items():
                write_new(SNAPSHOT/name,data)
            write_new(SNAPSHOT/'hashes.json',raw({k:sha(v) for k,v in frozen.items()}))
            replacements = {SHARED[k]:files[k] for k in SHARED}
            def validate():
                old_retention(m,host); host.static(); runtime_guards(m,host)
            replace_set(replacements,originals,validate)
            # Retire only the pinned consumed drop-in; no other service file is removed.
            require(trusted_read(DROPIN)==originals[DROPIN], 'consumed_dropin_changed')
            write_new(BACKUP/'retired-20261006-entry-capture.conf',originals[DROPIN],0o600)
            DROPIN.unlink(); sync_dir(DROPIN.parent)
            host.run(['/usr/bin/systemctl','daemon-reload'])
            runtime_guards(m,host,retired=True); old_retention(m,host)
            verify_bindings(host.show(RET_SERVICE,'BindReadOnlyPaths'),bindings(SNAPSHOT))
            require(host.show(ACT_TIMER,'Unit')==ACT_SERVICE and host.show(RET_TIMER,'Unit')==RET_SERVICE,
                    'new_timer_target_mismatch')
            host.static()
            # Enable activation last, only while still before its fixed future window.
            runtime_guards(m,host,retired=True)
            host.run(['/usr/bin/systemctl','enable','--now',RET_TIMER])
            host.run(['/usr/bin/systemctl','enable','--now',ACT_TIMER])
            runtime_guards(m,host,retired=True); old_retention(m,host)
            require(host.show(ACT_TIMER,'ActiveState')=='active' and
                    host.show(RET_TIMER,'ActiveState')=='active' and
                    host.show(ACT_TIMER,'NextElapseUSecRealtime') not in ('','n/a'), 'timers_not_armed')
            write_new(BACKUP/'complete.json',raw({'status':'complete','manifest_sha256':expected}),0o600)
        except BaseException:
            # Only our new timers may be disabled. Never normalize failed Toss here.
            disarmed = True
            for timer in (ACT_TIMER,RET_TIMER):
                try:
                    host.run(['/usr/bin/systemctl','disable','--now',timer])
                except BaseException:
                    disarmed = False
            try:
                write_new(BACKUP/'failed.json',raw({'status':'operator_review_required',
                          'new_timers_disarmed':disarmed}),0o600)
            except BaseException:
                pass
            raise GuardError('preparation_partial_disarmed' if disarmed else 'preparation_disarm_failed') from None
    finally:
        os.close(lock)
    return {'checked':True,'applied':True,'reason':'future_timers_armed','bot_restarted':False,'toss_started':False}

def main(argv=None):
    os.umask(0o022); sys.dont_write_bytecode=True
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--check', action='store_true'); group.add_argument('--apply', action='store_true')
    parser.add_argument('--manifest-sha256', required=True)
    args = parser.parse_args(argv)
    try:
        require(os.geteuid()==0 and not sys.flags.optimize, 'root_nonoptimized_required')
        result = prepare(args.manifest_sha256, apply=args.apply)
    except Exception as exc:
        code = str(exc) if isinstance(exc, GuardError) else 'preparation_io_or_validation_failed'
        print(json.dumps({'ok':False,'reason':code})); return 1
    print(json.dumps(result)); return 0

if __name__ == '__main__':
    raise SystemExit(main())
