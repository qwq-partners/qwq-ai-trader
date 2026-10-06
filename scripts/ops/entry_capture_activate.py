#!/usr/bin/python3
"""검토·준비된 명시 날짜의 관측을 좁은 시간 안에 한 번만 활성화한다.

루트 전용 고정 JSON을 읽는다. 준비/발급/다운로드/전체 테스트/재시도는 하지 않는다.
재시작 요청 이후의 오류는 복구 재시작 없이 운영자 확인 상태로 남긴다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import stat
import sys
import subprocess
import time

REPO = Path('/home/ubuntu/projects/qwq-ai-trader')
CONFIG_PATH = Path('/etc/qwq-entry-capture/activation.json')
STATE_DIR = Path('/var/lib/qwq-entry-capture')
ONCE_PATH = Path('/home/ubuntu/.local/share/qwq-entry-observation/20261002-pilot1/once.json')
LAUNCHER_PATH = Path('/usr/libexec/qwq-toss-observer/launcher.py')
DEPLOYMENT_PATH = Path('/etc/qwq-toss-observer/deployment.json')
DROPIN_PATH = Path('/etc/systemd/system/qwq-ai-trader.service.d/entry-capture.conf')
STAGED_DROPIN = Path('/etc/qwq-entry-capture/entry-capture.conf')
KILL_PATH = Path('/home/ubuntu/.cache/ai_trader/KILL_SWITCH_KR')
LOCK_PATH = Path('/tmp/qwq-ai-trader-deploy.lock')
BOT_UNIT = 'qwq-ai-trader.service'
TOSS_UNIT = 'qwq-toss-observer.service'
ENV = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C', 'LC_ALL': 'C'}


class GuardError(Exception):
    """세부 운영 자료를 출력하지 않는 경계 실패."""


@dataclass(frozen=True)
class ActivationProfile:
    config_path: Path
    state_dir: Path
    once_path: Path
    staged_dropin: Path
    evaluation_epoch: str
    not_before: datetime
    latest_start_at: datetime
    allow_predeployed: bool = False


def activation_profile(name):
    """날짜/경로를 입력받지 않는다. 검토된 세 고정 계약만 선택한다."""
    if name == '20261002':
        return ActivationProfile(CONFIG_PATH, STATE_DIR, ONCE_PATH, STAGED_DROPIN,
            'kr-entry-20261002-firstscan-v1',
            datetime(2026,10,1,23,55,tzinfo=timezone.utc),
            datetime(2026,10,1,23,57,tzinfo=timezone.utc))
    if name == '20261006-pilot2':
        return ActivationProfile(
            Path('/etc/qwq-entry-capture/20261006-pilot2/activation.json'),
            Path('/var/lib/qwq-entry-capture/20261006-pilot2'),
            Path('/home/ubuntu/.local/share/qwq-entry-observation/20261006-pilot2/once.json'),
            Path('/etc/qwq-entry-capture/20261006-pilot2/entry-capture.conf'),
            'kr-entry-20261006-firstscan-v4',
            datetime(2026,10,5,23,55,tzinfo=timezone.utc),
            datetime(2026,10,5,23,57,tzinfo=timezone.utc), allow_predeployed=True)
    if name == '20261007-pilot3':
        return ActivationProfile(
            Path('/etc/qwq-entry-capture/20261007-pilot3/activation.json'),
            Path('/var/lib/qwq-entry-capture/20261007-pilot3'),
            Path('/home/ubuntu/.local/share/qwq-entry-observation/20261007-pilot3/once.json'),
            Path('/etc/qwq-entry-capture/20261007-pilot3/entry-capture.conf'),
            'kr-entry-20261007-firstscan-v4',
            datetime(2026,10,6,23,55,tzinfo=timezone.utc),
            datetime(2026,10,6,23,57,tzinfo=timezone.utc), allow_predeployed=True)
    raise GuardError()


@dataclass(frozen=True)
class Config:
    repo: Path
    old_head: str
    new_head: str
    source_hashes: dict
    protected_hashes: dict
    input_hashes: dict
    once_path: Path
    launcher_path: Path
    deployment_path: Path
    staged_dropin: Path
    dropin_path: Path
    dropin_sha256: str
    kill_path: Path
    state_dir: Path
    lock_path: Path
    not_before: datetime
    latest_start_at: datetime
    toss_uid: int
    toss_gid: int
    study_sha256: str
    evaluation_epoch: str


def digest(value):
    return hashlib.sha256(value).hexdigest()


def read_file(path, *, owner=None, limit=4*1024*1024):
    """심볼릭 링크·특수 파일·크기 초과를 거부한다."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_size > limit or (owner is not None and
                    (info.st_uid != owner or info.st_mode & 0o022))):
            raise GuardError()
        value = stream.read(limit+1)
        if len(value) > limit:
            raise GuardError()
        return value


def trusted_dir(path, owner):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != owner or info.st_mode & 0o022:
        raise GuardError()


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_new(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    sync_dir(path.parent)


def dropin_content(once_path):
    return ('[Service]\nExecStart=\nExecStart=/home/ubuntu/projects/qwq-ai-trader/venv/bin/python '
            'scripts/run_trader.py --market kr --config '
            '/home/ubuntu/projects/qwq-ai-trader/config/default.yml '
            f'--entry-observation-once {once_path}\n')


def parse_time(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset().total_seconds() != 0:
        raise GuardError()
    return result


def load_config(profile='20261002'):
    """CLI에는 경로/명령/시간 변경 인자가 없다. 선택 날짜의 루트 준비본만 허용한다."""
    selected = activation_profile(profile)
    if os.geteuid() != 0:
        raise GuardError()
    trusted_dir(selected.config_path.parent, 0)
    raw = json.loads(read_file(selected.config_path, owner=0, limit=1024*1024))
    paths = dict(repo=REPO, once_path=selected.once_path, launcher_path=LAUNCHER_PATH,
                 deployment_path=DEPLOYMENT_PATH, staged_dropin=selected.staged_dropin,
                 dropin_path=DROPIN_PATH, kill_path=KILL_PATH,
                 state_dir=selected.state_dir, lock_path=LOCK_PATH)
    keys = {'old_head','new_head','source_hashes','protected_hashes','input_hashes',
            'dropin_sha256','not_before','latest_start_at','toss_uid','toss_gid',
            'study_sha256','evaluation_epoch'} | set(paths)
    if type(raw) is not dict or set(raw) != keys:
        raise GuardError()
    if any(raw[name] != str(path) for name, path in paths.items()):
        raise GuardError()
    for name in ('old_head','new_head'):
        if type(raw[name]) is not str or not re.fullmatch('[0-9a-f]{40}', raw[name]):
            raise GuardError()
    if raw['old_head'] == raw['new_head'] and not selected.allow_predeployed:
        raise GuardError()
    for name in ('source_hashes','protected_hashes','input_hashes'):
        manifest = raw[name]
        if type(manifest) is not dict or not manifest or len(manifest) > 1000:
            raise GuardError()
        for path, sha in manifest.items():
            if type(sha) is not str or not re.fullmatch('[0-9a-f]{64}', sha):
                raise GuardError()
            if name == 'source_hashes' and not (path.startswith(('src/','scripts/')) and path.endswith('.py')):
                raise GuardError()
            if name != 'input_hashes':
                if (Path(path).is_absolute() or '..' in Path(path).parts
                        or not re.fullmatch(r'[a-zA-Z0-9_./-]+', path)):
                    raise GuardError()
            elif (not Path(path).is_absolute() or '..' in Path(path).parts
                  or not (path.startswith('/etc/qwq-toss-observer/')
                          or path == str(LAUNCHER_PATH)
                          or path.startswith(str(selected.once_path.parent)+'/'))):
                raise GuardError()
    if set(raw['protected_hashes']) != {'config/default.yml','config/evolved_overrides.yml'}:
        raise GuardError()
    expected_inputs = {str(selected.once_path), str(selected.once_path.with_name('study.json')),
                       str(selected.once_path.with_name('manifest.json')), str(LAUNCHER_PATH),
                       str(DEPLOYMENT_PATH), '/etc/qwq-toss-observer/plan.json',
                       '/etc/qwq-toss-observer/registry.json'}
    if set(raw['input_hashes']) != expected_inputs:
        raise GuardError()
    if 'scripts/run_trader.py' not in raw['source_hashes']:
        raise GuardError()
    for name in ('dropin_sha256','study_sha256'):
        if type(raw[name]) is not str or not re.fullmatch('[0-9a-f]{64}',raw[name]):
            raise GuardError()
    if raw['study_sha256'] != raw['input_hashes'][str(selected.once_path.with_name('study.json'))]:
        raise GuardError()
    if raw['evaluation_epoch'] != selected.evaluation_epoch:
        raise GuardError()
    if (type(raw['toss_uid']) is not int or raw['toss_uid'] != 997
            or type(raw['toss_gid']) is not int or raw['toss_gid'] != 987):
        raise GuardError()
    raw.update(paths)
    raw['not_before'] = parse_time(raw['not_before'])
    raw['latest_start_at'] = parse_time(raw['latest_start_at'])
    if (raw['not_before'] != selected.not_before
            or raw['latest_start_at'] != selected.latest_start_at):
        raise GuardError()
    return Config(**raw)


def run_command(argv, *, timeout):
    return subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, env=ENV, cwd='/',
                          timeout=timeout, check=False)


def fetch_loopback(path, *, timeout):
    if path not in ('/api/health','/api/internal/entry-anchors'):
        raise GuardError()
    conn = http.client.HTTPConnection('127.0.0.1', 8080, timeout=timeout)
    try:
        conn.request('GET',path,headers={'X-QWQ-Observation':'1', 'Accept-Encoding':'identity'})
        response = conn.getresponse()
        if response.status != 200 or response.getheader('Content-Encoding') not in (None,'identity'):
            raise GuardError()
        body = response.read(65537)
        if len(body) > 65536:
            raise GuardError()
        return json.loads(body)
    finally:
        conn.close()


def pending_clear(health):
    if type(health) is not dict:
        return False
    broker, risk = health.get('broker'), health.get('risk_manager')
    if type(broker) is not dict or type(risk) is not dict or broker.get('connected') is not True:
        return False
    return all(type(value) is int and value == 0 for value in (
        broker.get('pending_orders'),risk.get('pending_orders'),
        risk.get('pending_quantities'),risk.get('pending_sells')))


def activate(cfg, *, runner=run_command, clock=lambda:datetime.now(timezone.utc),
             fetch=fetch_loopback, pending_check=pending_clear, sleep=time.sleep,
             trusted_uid=0, lock_uid=1000, engine_input_uid=1000):
    """읽기/프로세스 경계를 주입할 수 있는 한 번 실행 상태 기계."""
    phase = 'window'
    receipt = False
    changed = False
    installed = False
    dropin_identity = None
    restart_attempted = False
    toss_attempted = False
    lock_fd = None
    rollback = 'not_needed'

    def window():
        now = clock()
        if (now.tzinfo is None or not cfg.not_before <= now < cfg.latest_start_at
                or not 0 < (cfg.latest_start_at-cfg.not_before).total_seconds() <= 120):
            raise GuardError()

    def command(argv, *, codes=(0,), rollback_command=False):
        if not rollback_command:
            window()
        remaining = max(.1, (cfg.latest_start_at-clock()).total_seconds())
        result = runner(argv, timeout=min(15, remaining) if not rollback_command else 10)
        if result.returncode not in codes:
            raise GuardError()
        return result.stdout.decode() if isinstance(result.stdout, bytes) else result.stdout

    def git(*args, rollback_command=False):
        return command(['/usr/sbin/runuser','-u','ubuntu','--','/usr/bin/env','-i',
                        'PATH=/usr/bin:/bin','HOME=/home/ubuntu','/usr/bin/git',
                        '-C',str(cfg.repo),*args],
                       rollback_command=rollback_command)

    def protected():
        if not stat.S_ISREG(cfg.kill_path.lstat().st_mode):
            raise GuardError()
        for relative, sha in cfg.protected_hashes.items():
            if digest(read_file(cfg.repo / relative)) != sha:
                raise GuardError()

    def inputs():
        for path, sha in cfg.input_hashes.items():
            owner = trusted_uid if Path(path) in (cfg.launcher_path,cfg.deployment_path) or path.startswith('/etc/') else None
            if path.startswith(str(cfg.once_path.parent)+'/'):
                owner = engine_input_uid
            if digest(read_file(Path(path),owner=owner)) != sha:
                raise GuardError()

    def active(unit, expected):
        value = command(['/usr/bin/systemctl','is-active',unit],codes=(0,3)).strip()
        if value != expected:
            raise GuardError()

    try:
        window()
        phase = 'receipt'
        trusted_dir(cfg.state_dir, trusted_uid)
        write_new(cfg.state_dir/'activation.receipt',json.dumps({
            'new_head':cfg.new_head,'attempted_at':clock().isoformat()}).encode())
        receipt = True
        phase = 'deploy_lock'
        lock_fd = os.open(cfg.lock_path,os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(lock_fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != lock_uid
                or info.st_nlink != 1 or info.st_mode & 0o022):
            raise GuardError()
        fcntl.flock(lock_fd,fcntl.LOCK_EX | fcntl.LOCK_NB)
        phase = 'preflight'
        protected()
        inputs()
        trusted_dir(cfg.dropin_path.parent,trusted_uid)
        staged = read_file(cfg.staged_dropin,owner=trusted_uid)
        if (digest(staged) != cfg.dropin_sha256
                or staged != dropin_content(str(cfg.once_path)).encode()
                or cfg.dropin_path.exists() or cfg.dropin_path.is_symlink()):
            raise GuardError()
        if git('rev-parse','HEAD').strip() != cfg.old_head:
            raise GuardError()
        dirty = git('status','--porcelain','--untracked-files=all')
        if dirty not in ('',' M config/evolved_overrides.yml\n'):
            raise GuardError()
        if git('diff','--name-only',cfg.old_head,cfg.new_head,'--',
               'config/default.yml','config/evolved_overrides.yml').strip():
            raise GuardError()
        for path, sha in cfg.source_hashes.items():
            # git show 출력은 내부 해시 계산 외에는 사용하지 않는다.
            value = git('show',f'{cfg.new_head}:{path}')
            if digest(value.encode()) != sha:
                raise GuardError()
        active(BOT_UNIT,'active')
        active(TOSS_UNIT,'inactive')
        if command(['/usr/bin/systemctl','show','--property=DropInPaths','--value',BOT_UNIT]).strip():
            raise GuardError()
        if not pending_check(fetch('/api/health',timeout=min(2,(cfg.latest_start_at-clock()).total_seconds()))):
            raise GuardError()
        phase = 'launcher_check'
        # 설치된 런처가 요구하는 단일 서비스 그룹만 유지하고 환경을 비운다.
        command(['/usr/bin/setpriv',f'--reuid={cfg.toss_uid}',f'--regid={cfg.toss_gid}',
                 f'--groups={cfg.toss_gid}','/usr/bin/env','-i','TOSS_API=1','/usr/bin/python3',
                 '-I','-S',str(cfg.launcher_path),'--deployment',str(cfg.deployment_path),'--check'])
        phase = 'checkout'
        if cfg.old_head != cfg.new_head:
            changed = True  # 부분 실패도 원래 HEAD 복구를 시도한다.
            git('checkout','-q','--detach',cfg.new_head)
        if git('rev-parse','HEAD').strip() != cfg.new_head:
            raise GuardError()
        for path, sha in cfg.source_hashes.items():
            if digest(read_file(cfg.repo/path)) != sha:
                raise GuardError()
        protected()
        inputs()
        phase = 'dropin'
        # 배타 생성 뒤 실패하면 파일이 남을 수 있으므로 자체 정리 대상으로 표시한다.
        fd = os.open(cfg.dropin_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o644)
        installed = True
        created = os.fstat(fd)
        dropin_identity = (created.st_dev, created.st_ino)
        with os.fdopen(fd,'wb') as stream:
            stream.write(staged)
            stream.flush()
            os.fsync(stream.fileno())
        sync_dir(cfg.dropin_path.parent)
        command(['/usr/bin/systemctl','daemon-reload'])
        phase = 'before_restart'
        window()
        protected()
        inputs()
        active(BOT_UNIT,'active')
        active(TOSS_UNIT,'inactive')
        if not pending_check(fetch('/api/health',timeout=2)):
            raise GuardError()
        window()
        phase = 'restart'
        restart_attempted = True  # 요청 결과가 불명이어도 재요청하지 않는다.
        command(['/usr/bin/systemctl','--no-block','restart',BOT_UNIT])
        phase = 'readiness'
        ready = False
        while clock() < cfg.latest_start_at:
            window()
            try:
                active(BOT_UNIT,'active')
                if not pending_check(fetch('/api/health',timeout=min(2,(cfg.latest_start_at-clock()).total_seconds()))):
                    raise GuardError()
                window()
                projection = fetch('/api/internal/entry-anchors',timeout=min(2,(cfg.latest_start_at-clock()).total_seconds()))
                if (type(projection) is not dict
                        or projection.get('schema_version') != 'entry-anchor-projection-v2'
                        or projection.get('study_sha256') != cfg.study_sha256
                        or projection.get('evaluation_epoch') != cfg.evaluation_epoch
                        or type(projection.get('capture_id')) is not str
                        or not 1 <= len(projection['capture_id']) <= 120
                        or projection.get('capture_closed') is not False):
                    raise GuardError()
                ready = True
                break
            except Exception:
                window()
                sleep(min(1,max(0,(cfg.latest_start_at-clock()).total_seconds())))
        if not ready:
            raise GuardError()
        phase = 'before_toss'
        protected()
        inputs()
        active(TOSS_UNIT,'inactive')
        window()
        phase = 'toss_start'
        toss_attempted = True
        command(['/usr/bin/systemctl','start',TOSS_UNIT])
        active(TOSS_UNIT,'active')
        phase = 'complete'
        status = 'complete'
    except Exception:
        status = 'failed' if receipt else 'rejected'
        if (changed or installed) and not restart_attempted:
            rollback = 'failed'
            try:
                if changed:
                    git('checkout','-q','--detach',cfg.old_head,rollback_command=True)
                if installed:
                    visible = cfg.dropin_path.lstat()
                    if (not stat.S_ISREG(visible.st_mode) or visible.st_uid != trusted_uid
                            or visible.st_nlink != 1 or visible.st_mode & 0o022
                            or (visible.st_dev, visible.st_ino) != dropin_identity):
                        raise GuardError()
                    cfg.dropin_path.unlink()
                    sync_dir(cfg.dropin_path.parent)
                    command(['/usr/bin/systemctl','daemon-reload'],rollback_command=True)
                protected()
                rollback = 'complete'
            except Exception:
                pass
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
    result = dict(status=status,phase=phase,restart_attempted=restart_attempted,
                  toss_attempted=toss_attempted,rollback=rollback)
    if receipt:
        try:
            write_new(cfg.state_dir/'status.json',json.dumps(result,sort_keys=True).encode())
        except Exception:
            result['status'] = 'failed'
            result['phase'] = 'status_write'
    return result


def main():
    try:
        if sys.argv[1:] == []:
            cfg = load_config()
        elif sys.argv[1:] == ['--profile', '20261006-pilot2']:
            cfg = load_config('20261006-pilot2')
        elif sys.argv[1:] == ['--profile', '20261007-pilot3']:
            cfg = load_config('20261007-pilot3')
        else:
            raise GuardError()
        result = activate(cfg)
    except Exception:
        result = {'status':'rejected','phase':'configuration'}
    print(json.dumps(result,sort_keys=True))
    return 0 if result['status'] == 'complete' else 1


if __name__ == '__main__':
    raise SystemExit(main())
